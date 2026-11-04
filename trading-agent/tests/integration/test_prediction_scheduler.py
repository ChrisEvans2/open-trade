from datetime import timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import publish_child

from trading_agent.prediction.scheduler import PredictionScheduler
from trading_agent.runtime.lifecycle import Runtime
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


async def test_overdue_child_gets_model_slot_before_new_parent_but_not_risk_or_controller(
    prediction_system,
):
    db, clock, jobs, _, engine = prediction_system
    common = {"engine_id": str(engine), "symbol": "BTCUSDT", "timeframe": "1h"}
    async with db.transaction() as c:
        child = await jobs.create(
            c, "PREDICT_TIMEFRAME", "overdue-child", common, clock.value - timedelta(minutes=6)
        )
        parent = await jobs.create(c, "PREDICT_PARENT", "new-parent", common, clock.value)
        verify = await jobs.create(c, "PREDICT_VERIFY", "protect-first", common, clock.value)
        controller = await jobs.create(
            c,
            "PREDICT_REVIEW",
            "controller-next",
            {**common, "requested_by": "fixture-controller", "request_kind": "review"},
            clock.value,
        )
    kinds = ("PREDICT_PARENT", "PREDICT_TIMEFRAME", "PREDICT_REVIEW", "PREDICT_VERIFY")
    for ident in (verify, controller, child, parent):
        attempt = await jobs.claim(kinds)
        assert attempt.job_id == ident
        await jobs.finish(attempt)


async def test_controller_review_gets_next_model_slot_while_monitoring_stays_first(
    prediction_system,
):
    db, clock, jobs, service, engine = prediction_system
    common = {"engine_id": str(engine), "symbol": "BTCUSDT"}
    async with db.transaction() as connection:
        for kind, key, payload in (
            ("PREDICT_VERIFY", "verify", common),
            ("PREDICT_TIMEFRAME", "timeframe", {**common, "timeframe": "1h"}),
            ("PREDICT_PARENT", "parent", {**common, "timeframe": "1h"}),
            ("PREDICT_REVIEW", "scheduled", common),
            (
                "PREDICT_REVIEW",
                "requested",
                {**common, "requested_by": "fixture-controller", "request_kind": "review"},
            ),
        ):
            await jobs.create(connection, kind, key, payload, clock.value)
    dispatched = []
    runtime = SimpleNamespace(
        fatal=False,
        stopping=False,
        active={},
        config=service.config,
        jobs=jobs,
        _dispatch=dispatched.append,
    )
    for expected in (
        ["PREDICT_VERIFY", "PREDICT_REVIEW", "PREDICT_PARENT"],
        ["PREDICT_TIMEFRAME", "PREDICT_REVIEW"],
    ):
        await Runtime.execute_tick(runtime)
        assert [attempt.kind for attempt in dispatched] == expected
        requested = [a for a in dispatched if a.payload.get("requested_by")]
        if "PREDICT_VERIFY" in expected:
            assert requested[0] == dispatched[1]
        else:
            assert not requested
        for attempt in dispatched:
            await jobs.finish(attempt)
        dispatched.clear()


async def test_startup_independent_pending_jobs_and_pause(prediction_system):
    db, clock, jobs, service, engine = prediction_system
    scheduler = PredictionScheduler(jobs, service.config, engine, service)
    await scheduler.tick()
    await scheduler.tick()
    async with db.engine.connect() as conn:
        rows = (await conn.execute(sa.select(s.jobs))).mappings().all()
    assert sum(r["kind"] == "PREDICT_TIMEFRAME" for r in rows) == 8
    assert sum(r["kind"] == "PREDICT_VERIFY" for r in rows) == 2
    assert not any(r["kind"] == "FORECAST" for r in rows)
    # Pausing analysis must leave verification/position-protection scheduling alive.
    async with db.transaction() as conn:
        await conn.execute(sa.update(s.runtime_control).values(paused=True))
        await conn.execute(
            sa.update(s.jobs).where(s.jobs.c.kind == "PREDICT_VERIFY").values(state="SUCCEEDED")
        )
    clock.value += timedelta(minutes=1)
    await scheduler.tick()
    async with db.engine.connect() as conn:
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.jobs)
                .where(s.jobs.c.kind == "PREDICT_TIMEFRAME")
            )
            == 8
        )
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.jobs)
                .where(s.jobs.c.kind == "PREDICT_VERIFY")
            )
            == 4
        )


async def test_continuous_child_updates_coalesce_before_parent_input_freezes(prediction_system):
    db, _, _, _, _ = prediction_system
    await publish_child(prediction_system)
    await publish_child(prediction_system, "4h")
    await publish_child(prediction_system, "12h")
    async with db.engine.connect() as conn:
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.jobs)
                .where(s.jobs.c.kind == "PREDICT_PARENT")
            )
            == 1
        )
