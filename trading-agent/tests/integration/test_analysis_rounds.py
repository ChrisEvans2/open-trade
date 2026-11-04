import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import ValidationError
from test_prediction_knowledge import knowledge
from test_prediction_market import MarketFixture
from test_prediction_pipeline import prediction_system as prediction_system

from trading_agent.api.app import APIServices, create_app
from trading_agent.api.auth import CredentialHashes
from trading_agent.domain.errors import DomainError
from trading_agent.prediction.market import PredictionMarket
from trading_agent.prediction.rounds import ROLES, AnalysisRounds
from trading_agent.prediction.submissions import DecisionContent, PredictionContent
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


class Market(MarketFixture):
    async def check_clock(self, deadline):
        pass

    async def instrument(self, deadline, **scope):
        return {}

    async def observations(self, deadline, **scope):
        return ()


@pytest_asyncio.fixture
async def round_system(prediction_system, tmp_path):
    db, clock, jobs, predictions, engine = prediction_system
    market = Market(prediction_system)
    reviews = knowledge(prediction_system, tmp_path)
    collector = PredictionMarket(jobs, market, predictions, reviews, engine)
    rounds = AnalysisRounds(predictions, collector)
    return SimpleNamespace(
        db=db,
        clock=clock,
        jobs=jobs,
        predictions=predictions,
        engine=engine,
        market=market,
        knowledge=reviews,
        rounds=rounds,
    )


async def advance(sys, seconds, main=None):
    while seconds:
        step = min(seconds, 40)
        sys.clock.value += timedelta(seconds=step)
        seconds -= step
        if main:
            assert await sys.jobs.renew(main)


async def start(sys, symbol="BTCUSDT"):
    async with sys.db.transaction() as c:
        await sys.rounds.event(
            c, sys.engine, symbol, ROLES, "BOOTSTRAP", "initial", sys.clock.value
        )
    await advance(sys, 30)
    async with sys.db.transaction() as c:
        await sys.rounds.tick(c, sys.engine, symbol, sys.clock.value)
    attempt = await sys.jobs.claim(("PREDICT_PARENT",))
    assert attempt and attempt.payload["analysis_round"]
    return await sys.rounds.start(attempt)


async def tick(sys):
    async with sys.db.transaction() as c:
        await sys.rounds.tick(c, sys.engine, "BTCUSDT", sys.clock.value)


def content(context):
    rule = next(r for r in context["horizon_rules"] if r["structure"] == "UNKNOWN")
    if context.get("input_kind") == "CHILD_SYNTHESIS":
        options = context["boundary_options"]
        trigger = next(o for o in options if o["behavior"] == "CLOSE_ABOVE")
        invalidation = next(o for o in options if o["behavior"] == "CLOSE_BELOW")
    else:
        trigger = dict(
            behavior="CLOSE_ABOVE",
            line=context["fallback_lines"][1],
            tolerance=context["fallback_tolerance"],
        )
        invalidation = dict(
            behavior="CLOSE_BELOW",
            line=context["fallback_lines"][0],
            tolerance=context["fallback_tolerance"],
        )
    return PredictionContent(
        structure_ref="structure.unknown",
        horizon_ref=rule["ref"],
        primary_direction="UP",
        p_up=".7",
        p_down=0,
        p_none=".3",
        scenarios=[
            dict(
                direction="UP",
                trigger={k: v for k, v in trigger.items() if k != "source_child_ref"},
                invalidation={k: v for k, v in invalidation.items() if k != "source_child_ref"},
                target=dict(low="200", high="210"),
                target_probability=".6",
            )
        ],
        reason="fixture conditional forecast",
        evidence_refs=[
            "synthesis.current"
            if context.get("input_kind") == "CHILD_SYNTHESIS"
            else "market.current"
        ],
    )


async def publish(sys, main, stage):
    draft = content(stage["context"])
    if stage["role"] == "parent":
        draft = DecisionContent(
            prediction=draft,
            action="HOLD",
            trade_style="TREND",
            child_refs=["child.1h"],
            relationship="all four considered; explicit selected evidence",
        )
    await sys.rounds.complete_execution(main, stage["run"])
    return await sys.rounds.submit(main, stage["run"], draft)


async def four(sys, main):
    stages = [await sys.rounds.begin(main, r) for r in ROLES]
    for stage in stages:
        await publish(sys, main, stage)
    return stages


async def finish(sys, main):
    await sys.jobs.finish(main, result_id=await sys.rounds.result(main))
    await sys.jobs.release_model_slot(main, cleaned=True)


async def test_fixed_window_merges_real_events_without_sliding_or_duplicate_facts(round_system):
    x = round_system
    async with x.db.transaction() as c:
        await x.rounds.event(c, x.engine, "BTCUSDT", ["1h"], "UPDATE", "one", x.clock.value)
    await advance(x, 18)
    async with x.db.transaction() as c:
        await x.rounds.event(c, x.engine, "BTCUSDT", ["4h"], "UPDATE", "four", x.clock.value)
        await x.rounds.event(c, x.engine, "BTCUSDT", ["4h"], "UPDATE", "four", x.clock.value)
        await x.rounds.event(c, x.engine, "ETHUSDT", ["1h"], "UPDATE", "eth", x.clock.value)
    await advance(x, 11)
    await x.rounds.flush(x.engine)
    assert await x.jobs.claim(("PREDICT_PARENT",)) is None
    await advance(x, 1)
    await x.rounds.flush(x.engine)
    main = await x.jobs.claim(("PREDICT_PARENT",))
    assert main.payload["symbol"] == "BTCUSDT"
    async with x.db.engine.connect() as c:
        row = (
            (
                await c.execute(
                    sa.select(s.analysis_scopes).where(s.analysis_scopes.c.symbol == "BTCUSDT")
                )
            )
            .mappings()
            .one()
        )
        assert row["pending_roles"] == ["1h", "4h"]
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.analysis_events)) == 3


async def test_four_roles_share_one_round_publish_once_and_bind_ids(round_system):
    x = round_system
    main = await start(x)
    stages = await four(x, main)
    parent = await x.rounds.begin(main, "parent")
    assert len(parent["context"]["children"]) == 4
    assert not {"input_id", "context_revision", "position_id"} & parent["context"].keys()
    assert all("id" not in child for child in parent["context"]["children"])
    await publish(x, main, parent)
    ident = await x.rounds.result(main)
    assert ident
    async with x.db.engine.connect() as c:
        facts = (await c.execute(sa.select(s.prediction_facts))).mappings().all()
        assert len(facts) == 5
        final = next(f for f in facts if f["id"] == ident)
        assert len(final["body"]["submission"]["child_prediction_ids"]) == 4
        assert len({f["input_id"] for f in facts}) == 5
        assert await x.rounds.parent_available(c, x.engine, "BTCUSDT")
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.analysis_rounds)) == 1
        selected = await c.scalar(
            sa.select(s.analysis_events.c.body).where(
                s.analysis_events.c.source_key == f"published:{parent['run']}"
            )
        )
        assert selected["selected_child_refs"] == ["child.1h"]
    assert {s["role"] for s in stages} == set(ROLES)


async def test_local_retry_fetches_new_input_without_running_valid_roles(round_system):
    x = round_system
    main = await start(x)
    await four(x, main)
    await publish(x, main, await x.rounds.begin(main, "parent"))
    await finish(x, main)
    async with x.db.engine.connect() as c:
        old_heads = dict(
            (r.timeframe, r.prediction_id)
            for r in (
                await c.execute(
                    sa.select(
                        s.prediction_heads.c.timeframe, s.prediction_heads.c.prediction_id
                    ).where(s.prediction_heads.c.role == "TIMEFRAME")
                )
            )
        )
    async with x.db.transaction() as c:
        await x.rounds.event(c, x.engine, "BTCUSDT", ["4h"], "UPDATE", "new-four", x.clock.value)
    await advance(x, 30)
    await tick(x)
    main = await x.rounds.start(await x.jobs.claim(("PREDICT_PARENT",)))
    assert (await x.rounds.begin(main, "1h"))["reused"]
    first = await x.rounds.begin(main, "4h")
    await x.rounds.reject(main, first["run"], "AGENT_TIMEOUT")
    await advance(x, 5, main)
    second = await x.rounds.begin(main, "4h")
    assert second["number"] == 2
    await publish(x, main, second)
    async with x.db.engine.connect() as c:
        runs = (
            (
                await c.execute(
                    sa.select(s.analysis_runs).where(
                        s.analysis_runs.c.round_id == UUID(main.payload["analysis_round"])
                    )
                )
            )
            .mappings()
            .all()
        )
        assert [r["role"] for r in runs] == ["4h", "4h"]
        assert runs[0]["input_id"] != runs[1]["input_id"]
        contexts = (
            (
                await c.execute(
                    sa.select(s.prediction_inputs).where(
                        s.prediction_inputs.c.id.in_([r["input_id"] for r in runs])
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len({r["decision_at"] for r in contexts}) == 2
        heads = dict(
            (r.timeframe, r.prediction_id)
            for r in (
                await c.execute(
                    sa.select(
                        s.prediction_heads.c.timeframe, s.prediction_heads.c.prediction_id
                    ).where(s.prediction_heads.c.role == "TIMEFRAME")
                )
            )
        )
        assert all(heads[r] == old_heads[r] for r in ("1h", "6h", "12h"))
    assert len(x.market.candle_calls) == 6  # Four initial children plus two 4h attempts.


async def test_two_retries_and_three_recoveries_are_persistent_and_stop(round_system):
    x = round_system
    main = await start(x)
    for number in range(1, 4):
        stage = await x.rounds.begin(main, "1h")
        assert stage["number"] == number
        await x.rounds.reject(main, stage["run"], "AGENT_TIMEOUT")
    with pytest.raises(DomainError, match="重试次数"):
        await x.rounds.begin(main, "1h")
    for index, delay in enumerate((60, 180, 600, None)):
        await x.rounds.failed(main, DomainError("ROLE_RETRIES_EXHAUSTED", "fixture", 503))
        await x.jobs.release_model_slot(main, cleaned=True)
        async with x.db.engine.connect() as c:
            scope = (
                (
                    await c.execute(
                        sa.select(s.analysis_scopes).where(s.analysis_scopes.c.symbol == "BTCUSDT")
                    )
                )
                .mappings()
                .one()
            )
            assert not scope["parent_available"]
            assert scope["recoveries"] == index
        if delay is None:
            assert scope["state"] == "EXHAUSTED"
            await advance(x, 3600)
            await tick(x)
            assert await x.jobs.claim(("PREDICT_PARENT",)) is None
            break
        await advance(x, delay - 1)
        await tick(x)
        assert await x.jobs.claim(("PREDICT_PARENT",)) is None
        await advance(x, 1)
        await tick(x)
        main = await x.rounds.start(await x.jobs.claim(("PREDICT_PARENT",)))
        stage = await x.rounds.begin(main, "1h")
        await x.rounds.reject(main, stage["run"], "AGENT_TIMEOUT")
        with pytest.raises(DomainError, match="重试次数"):
            await x.rounds.begin(main, "1h")


async def test_two_slots_are_atomic_and_cleanup_unknown_keeps_capacity(round_system):
    x = round_system
    async with x.db.transaction() as c:
        for i in range(3):
            await x.jobs.create(
                c,
                "PREDICT_REVIEW",
                f"review:{i}",
                {"engine_id": str(x.engine), "symbol": "BTCUSDT"},
                x.clock.value,
            )
    claimed = await asyncio.gather(*(x.jobs.claim(("PREDICT_REVIEW",)) for _ in range(3)))
    attempts = [a for a in claimed if a]
    assert len(attempts) == 2
    await x.jobs.finish(attempts[0], state="FAILED", error={"code": "UNKNOWN"})
    await x.jobs.release_model_slot(attempts[0], cleaned=False)
    assert await x.jobs.claim(("PREDICT_REVIEW",)) is None
    await x.jobs.release_model_slot(attempts[0], cleaned=True)
    assert await x.jobs.claim(("PREDICT_REVIEW",))


async def test_role_auth_scope_and_content_reject_fixed_ids(round_system):
    x = round_system
    main = await start(x)
    stage = await x.rounds.begin(main, "1h")
    with pytest.raises(DomainError, match="绑定"):
        await x.rounds.resolve(main, "not-a-uuid")
    with pytest.raises(DomainError, match="授权"):
        await x.rounds.resolve(main, uuid4())
    with pytest.raises(ValidationError):
        PredictionContent.model_validate(
            {**content(stage["context"]).model_dump(), "input_id": str(uuid4())}
        )
    invalid = content(stage["context"]).model_copy(update={"evidence_refs": ("child.1h",)})
    await x.rounds.complete_execution(main, stage["run"])
    with pytest.raises(DomainError, match="引用"):
        await x.rounds.submit(main, stage["run"], invalid)
    async with x.db.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == 0


async def test_sub_execution_deadline_is_not_extended_by_late_submission(round_system):
    x = round_system
    main = await start(x)
    stage = await x.rounds.begin(main, "1h")
    await advance(x, 121, main)
    with pytest.raises(DomainError, match="执行期限"):
        await x.rounds.complete_execution(main, stage["run"])
    async with x.db.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == 0


async def test_parent_wait_excludes_child_time_and_only_parent_retries(round_system):
    x = round_system
    main = await start(x)
    await four(x, main)
    await advance(x, 220, main)
    assert await x.rounds.phase_remaining(main) is None
    parent = await x.rounds.begin(main, "parent")
    assert parent["execution_seconds"] == 120
    await x.rounds.reject(main, parent["run"], "AGENT_TIMEOUT")
    await advance(x, 5, main)
    parent = await x.rounds.begin(main, "parent")
    await publish(x, main, parent)
    assert len(x.market.candle_calls) == 4  # Parent retries never collect a new candle window.
    async with x.db.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == 5
        assert await c.scalar(sa.select(s.analysis_rounds.c.counts)) == {
            **dict.fromkeys(ROLES, 1),
            "parent": 2,
        }


async def test_parent_error_unavailable_in_latest_but_history_remains(round_system):
    x = round_system
    main = await start(x)
    await four(x, main)
    await publish(x, main, await x.rounds.begin(main, "parent"))
    ident = await x.rounds.result(main)
    await finish(x, main)
    async with x.db.transaction() as c:
        await x.rounds.event(
            c, x.engine, "BTCUSDT", [], "PARENT_ENDED", "parent-ended", x.clock.value
        )
    await advance(x, 30)
    await tick(x)
    main = await x.rounds.start(await x.jobs.claim(("PREDICT_PARENT",)))
    await x.rounds.failed(main, DomainError("AGENT_TIMEOUT", "fixture", 503))
    import hashlib

    credentials = CredentialHashes(
        hashlib.sha256(b"controller").hexdigest(), hashlib.sha256(b"operator").hexdigest()
    )
    app = create_app(
        APIServices(
            x.predictions.config, credentials, x.jobs, x.engine, None, x.predictions, x.knowledge
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://local",
        headers={"Authorization": "Bearer controller"},
    ) as client:
        latest = await client.get("/v2/predictions/latest", params={"symbol": "BTCUSDT"})
        assert latest.status_code == 503
        assert latest.json()["error"]["code"] == "PREDICTION_UNAVAILABLE"
        historical = await client.get(f"/v2/predictions/{ident}")
        assert historical.status_code == 200


async def test_publication_failure_rolls_back_fact_and_run_state(round_system, monkeypatch):
    x = round_system
    main = await start(x)
    stage = await x.rounds.begin(main, "1h")
    await x.rounds.complete_execution(main, stage["run"])
    original = x.rounds.scope

    async def failed_scope(*args, **kwargs):
        raise RuntimeError("fixture failure after forecast insert")

    monkeypatch.setattr(x.rounds, "scope", failed_scope)
    with pytest.raises(RuntimeError):
        await x.rounds.submit(main, stage["run"], content(stage["context"]))
    monkeypatch.setattr(x.rounds, "scope", original)
    async with x.db.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == 0
        assert await c.scalar(sa.select(s.analysis_runs.c.state)) == "SUBMITTING"
    await x.rounds.submit(main, stage["run"], content(stage["context"]))


async def test_event_after_closed_window_cannot_postpone_ready_round(round_system):
    x = round_system
    async with x.db.transaction() as c:
        await x.rounds.event(c, x.engine, "BTCUSDT", ["1h"], "UPDATE", "first", x.clock.value)
    await advance(x, 31)
    async with x.db.transaction() as c:
        await x.rounds.event(c, x.engine, "BTCUSDT", ["4h"], "UPDATE", "after-close", x.clock.value)
        ident = await x.rounds.tick(c, x.engine, "BTCUSDT", x.clock.value)
    assert ident


@pytest.mark.parametrize("invalid", [False, True])
async def test_analysis_interval_checks_structure_without_price_noise_restarts(
    round_system, invalid
):
    x = round_system
    main = await start(x)
    stage = await x.rounds.begin(main, "1h")
    # There is one complete minute wholly after the freeze, still inside 120s.
    await advance(x, 90, main)
    x.market.invalid = invalid
    await x.rounds.complete_execution(main, stage["run"])
    if invalid:
        with pytest.raises(DomainError, match="结构已经失效"):
            await x.rounds.submit(main, stage["run"], content(stage["context"]))
    else:
        await x.rounds.submit(main, stage["run"], content(stage["context"]))
    async with x.db.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == (
            0 if invalid else 1
        )


async def test_restart_fences_submitting_run_without_resetting_round_counts(round_system):
    x = round_system
    main = await start(x)
    stage = await x.rounds.begin(main, "1h")
    await x.rounds.complete_execution(main, stage["run"])
    # Lease expiry does not release a slot or allow another live copy of this job.
    await advance(x, 151)
    assert await x.jobs.claim(("PREDICT_PARENT",)) is None
    async with x.db.transaction() as c:
        await c.execute(
            sa.update(s.job_attempts)
            .where(s.job_attempts.c.id == main.id)
            .values(status="EXPIRED", token_revoked=True)
        )
    await x.jobs.release_model_slot(main, cleaned=True)
    replacement = await x.rounds.start(await x.jobs.claim(("PREDICT_PARENT",)))
    assert replacement.job_id == main.job_id
    assert (await x.rounds.state(replacement))["counts"]["1h"] == 1
    with pytest.raises(DomainError):
        await x.rounds.resolve(main, stage["run"])
    second = await x.rounds.begin(replacement, "1h")
    assert second["number"] == 2
    async with x.db.engine.connect() as c:
        assert (
            await c.scalar(
                sa.select(s.analysis_runs.c.state).where(s.analysis_runs.c.id == UUID(stage["run"]))
            )
            == "FAILED"
        )


async def test_new_role_event_during_error_preserves_backoff_and_recovery_group(round_system):
    x = round_system
    main = await start(x)
    await x.rounds.failed(main, DomainError("AGENT_TIMEOUT", "fixture", 503))
    await x.jobs.release_model_slot(main, cleaned=True)
    async with x.db.engine.connect() as c:
        before = (
            (
                await c.execute(
                    sa.select(s.analysis_scopes).where(s.analysis_scopes.c.symbol == "BTCUSDT")
                )
            )
            .mappings()
            .one()
        )
    await advance(x, 10)
    async with x.db.transaction() as c:
        await x.rounds.event(
            c, x.engine, "BTCUSDT", ["4h"], "UPDATE", "new-dependency", x.clock.value
        )
    await tick(x)
    async with x.db.engine.connect() as c:
        after = (
            (
                await c.execute(
                    sa.select(s.analysis_scopes).where(s.analysis_scopes.c.symbol == "BTCUSDT")
                )
            )
            .mappings()
            .one()
        )
        assert after["state"] == "ERROR"
        assert after["resume_at"] == before["resume_at"]
        assert after["error_group"] == before["error_group"]
        assert after["recoveries"] == 0
    assert await x.jobs.claim(("PREDICT_PARENT",)) is None


async def test_reused_child_is_rechecked_before_parent_retry_without_waiting_scheduler(
    round_system,
):
    x = round_system
    main = await start(x)
    await four(x, main)
    parent = await x.rounds.begin(main, "parent")
    await x.rounds.reject(main, parent["run"], "AGENT_TIMEOUT")
    x.market.invalid = True
    await advance(x, 90, main)
    with pytest.raises(DomainError, match="先补齐"):
        await x.rounds.begin(main, "parent")
    state = await x.rounds.state(main)
    assert set(state["needed"]) == set(ROLES)
    assert state["counts"] == {**dict.fromkeys(ROLES, 1), "parent": 1}
    async with x.db.engine.connect() as c:
        assert not await x.rounds.parent_available(c, x.engine, "BTCUSDT")
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == 4


async def test_crash_past_deadline_closes_scope_and_cannot_be_skipped_by_claim(round_system):
    from trading_agent.runtime.lifecycle import Runtime

    x = round_system
    main = await start(x)
    stage = await x.rounds.begin(main, "1h")
    await x.rounds.complete_execution(main, stage["run"])
    await advance(x, 1201)
    # A lingering occupied slot never licenses a new copy; recover verifies cleanup.
    await x.jobs.release_model_slot(main, cleaned=True)
    assert await x.jobs.claim(("PREDICT_PARENT",)) is None
    async with x.db.engine.connect() as c:
        assert (
            await c.scalar(sa.select(s.jobs.c.state).where(s.jobs.c.id == main.job_id)) == "RUNNING"
        )
    rt = SimpleNamespace(
        database=x.db, jobs=x.jobs, ownership=x.jobs.ownership, predictions=x.predictions
    )
    await Runtime.recover_jobs(rt)
    async with x.db.engine.connect() as c:
        scope = (
            (
                await c.execute(
                    sa.select(s.analysis_scopes).where(s.analysis_scopes.c.symbol == "BTCUSDT")
                )
            )
            .mappings()
            .one()
        )
        assert scope["state"] == "ERROR" and scope["resume_at"] == x.clock.value + timedelta(
            seconds=60
        )
        assert await c.scalar(sa.select(s.analysis_runs.c.state)) == "FAILED"
        assert await c.scalar(sa.select(s.jobs.c.last_error).where(s.jobs.c.id == main.job_id)) == {
            "code": "ANALYSIS_DEADLINE",
            "label": "error",
        }


async def test_host_restarts_only_parent_and_old_launch_cannot_expire_new_session(
    round_system, tmp_path
):
    from test_prediction_runner import configure_runner

    x = round_system
    main = await start(x)
    runner, resources = configure_runner(x.predictions, tmp_path, "raise SystemExit(99)")
    launches = []

    async def native_fixture(attempt, root, prompt, seconds):
        assert seconds > 0
        launches.append(True)
        assert await x.rounds.phase_remaining(main) is None
        if len(launches) == 1:
            await four(x, main)
            parent = await x.rounds.begin(main, "parent")
            await x.rounds.reject(main, parent["run"], "MODEL_CONNECTION_FAILED")
            assert await x.rounds.phase_remaining(main) == 0
            raise TimeoutError
        await advance(x, 5, main)
        assert (await x.rounds.state(main))["needed"] == []
        await publish(x, main, await x.rounds.begin(main, "parent"))

    runner._spawn = native_fixture
    assert await runner.run(main) == await x.rounds.result(main)
    assert len(launches) == 2 and len(resources.cleaned) == 2
    async with x.db.engine.connect() as c:
        assert await c.scalar(sa.select(s.analysis_rounds.c.counts)) == {
            **dict.fromkeys(ROLES, 1),
            "parent": 2,
            "coordinator": 2,
        }
        rows = (
            (
                await c.execute(
                    sa.select(s.analysis_runs)
                    .where(s.analysis_runs.c.role == "parent")
                    .order_by(s.analysis_runs.c.number)
                )
            )
            .mappings()
            .all()
        )
        assert [r["launch_index"] for r in rows] == [1, 2]
        assert rows[0]["input_id"] != rows[1]["input_id"]
        assert rows[1]["execution_finished_at"] and rows[1]["finished_at"]


async def test_host_zero_exit_without_roles_is_bounded_by_durable_launch_count(
    round_system, tmp_path
):
    from test_prediction_runner import configure_runner

    x = round_system
    main = await start(x)
    runner, resources = configure_runner(x.predictions, tmp_path, "raise SystemExit(0)")

    async def no_result(*args):
        return None

    runner._spawn = no_result
    with pytest.raises(DomainError) as caught:
        await runner.run(main)
    assert caught.value.code == "COORDINATOR_RETRIES_EXHAUSTED"
    assert len(resources.prepared) == len(resources.cleaned) == 3
    async with x.db.engine.connect() as c:
        assert await c.scalar(sa.select(s.analysis_rounds.c.counts)) == {"coordinator": 3}
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == 0


async def test_unconfirmed_child_cancel_stops_host_but_not_future_launch(round_system):
    x = round_system
    main = await start(x)
    await x.rounds.launch(main)
    stage = await x.rounds.begin(main, "1h")
    await x.rounds.reject(main, stage["run"], "CHILD_CANCELLATION_PENDING")
    assert await x.rounds.phase_remaining(main) == 0
    await x.rounds.launch(main)
    assert await x.rounds.phase_remaining(main) is None
    stage = await x.rounds.begin(main, "1h")
    await advance(x, 126, main)
    # Even if the plugin cannot report cancellation, host deadline supervision
    # sees an unacknowledged child. Formal writes were already closed at 120s.
    assert await x.rounds.phase_remaining(main) == 0
    with pytest.raises(DomainError):
        await x.rounds.complete_execution(main, stage["run"])
