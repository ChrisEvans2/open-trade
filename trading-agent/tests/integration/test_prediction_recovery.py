import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from test_prediction_knowledge import knowledge
from test_prediction_market import MarketFixture
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import publish_child, report, task
from test_prediction_runner import configure_runner

from trading_agent.api.app import APIServices, create_app
from trading_agent.api.auth import CredentialHashes
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import json_value
from trading_agent.jobs.service import token_hash
from trading_agent.prediction.contracts import ParentDecision
from trading_agent.prediction.market import PredictionMarket
from trading_agent.runtime.lifecycle import Runtime
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


def runtime(system):
    db, _, jobs, predictions, engine = system
    return SimpleNamespace(
        database=db,
        jobs=jobs,
        ownership=jobs.ownership,
        predictions=predictions,
        engine_id=engine,
        runner=SimpleNamespace(_result=AsyncMock(return_value=None)),
        config=predictions.config.model_copy(
            update={
                "notification": predictions.config.notification.model_copy(
                    update={"target": "fixture"}
                )
            }
        ),
        agent_health={"healthy": True, "error_code": None},
    )


class FreshMarket(MarketFixture):
    async def check_clock(self, deadline):
        pass

    async def instrument(self, deadline, *, inst_id):
        return {}

    async def observations(self, deadline, **scope):
        return ()


async def parent_task(system):
    await publish_child(system)
    attempt = await system[2].claim(("PREDICT_PARENT",))
    await system[3].freeze_parent(attempt, system[4])
    return attempt


async def submission(system, attempt):
    value = await report(system, attempt)
    if attempt.kind == "PREDICT_PARENT":
        context = await system[3].context(attempt)
        return ParentDecision(
            prediction=value,
            action="HOLD",
            trade_style="TREND",
            selected_direction="UP",
            child_prediction_ids=[child["id"] for child in context["children"]],
            context_revision=context["context_revision"],
            relationship="fixture synthesis",
        )
    return value


@pytest.mark.parametrize("kind", ["PREDICT_PARENT", "PREDICT_TIMEFRAME"])
async def test_failed_analysis_waits_then_freezes_new_data_and_can_publish(
    prediction_system, tmp_path, kind
):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    attempt = (
        await parent_task(system) if kind == "PREDICT_PARENT" else await task(system, kind=kind)
    )
    original = await predictions.context(attempt)
    clock.value += timedelta(seconds=30)
    await Runtime._failure(runtime(system), attempt, DomainError("AGENT_TIMEOUT", "fixture", 503))
    async with db.engine.connect() as c:
        old = (
            (await c.execute(sa.select(s.jobs).where(s.jobs.c.id == attempt.job_id)))
            .mappings()
            .one()
        )
        assert old["state"] == "FAILED" and old["last_error"] == {"code": "AGENT_TIMEOUT"}
        assert await c.scalar(sa.select(s.job_attempts.c.token_revoked)) is True
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.outbox)) == 0
        saved = await c.scalar(
            sa.select(s.prediction_inputs.c.body).where(
                s.prediction_inputs.c.job_id == attempt.job_id
            )
        )
        assert {
            k: v
            for k, v in saved.items()
            if kind != "PREDICT_PARENT"
            or k not in {"reference_price", "reference_price_source", "execution_basis"}
        } == {
            k: v
            for k, v in original.items()
            if k not in {"input_id", "knowledge_recalls", "submission_references"}
        }
    assert await jobs.claim((kind,)) is None
    clock.value += timedelta(seconds=60)
    retry = await jobs.claim((kind,))
    assert retry.job_id != attempt.job_id and retry.id != attempt.id
    assert retry.payload["analysis_retry_of"] == str(attempt.job_id)
    assert retry.payload["analysis_retry_count"] == 1
    async with db.engine.connect() as c:
        link = (
            (
                await c.execute(
                    sa.select(s.audit_events).where(
                        s.audit_events.c.action == "ANALYSIS_RETRY_SCHEDULED"
                    )
                )
            )
            .mappings()
            .one()
        )
        assert link["actor_id"] == str(attempt.id) and link["object_id"] == retry.job_id
    client = FreshMarket(system)
    market = PredictionMarket(jobs, client, predictions, knowledge(system, tmp_path), engine)
    await market.prepare(retry)
    new = await predictions.context(retry)
    assert new["input_id"] != original["input_id"]
    assert new["decision_at"] != original["decision_at"]
    assert len(client.candle_calls) == (0 if kind == "PREDICT_PARENT" else 1)
    ident = await predictions.publish(retry, await submission(system, retry))
    await jobs.finish(retry, result_id=ident)
    async with db.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_inputs)) == (
            3 if kind == "PREDICT_PARENT" else 2
        )
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == (
            2 if kind == "PREDICT_PARENT" else 1
        )


async def test_fresh_retry_is_bounded_and_retains_real_failure(prediction_system, tmp_path):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    failed = await task(system)
    rt = runtime(system)
    await Runtime._failure(rt, failed, DomainError("BOUNDARY_REFERENCE", "fixture", 409))
    clock.value += timedelta(seconds=60)
    retry = await jobs.claim(("PREDICT_TIMEFRAME",))
    market = PredictionMarket(
        jobs, FreshMarket(system), predictions, knowledge(system, tmp_path), engine
    )
    await market.prepare(retry)
    await Runtime._failure(rt, retry, DomainError("AGENT_TIMEOUT", "fixture", 503))
    clock.value += timedelta(minutes=10)
    assert await jobs.claim(("PREDICT_TIMEFRAME",)) is None
    async with db.engine.connect() as c:
        rows = (await c.execute(sa.select(s.jobs.c.state, s.jobs.c.last_error))).all()
        assert sorted((state, error["code"]) for state, error in rows) == [
            ("FAILED", "AGENT_TIMEOUT"),
            ("FAILED", "BOUNDARY_REFERENCE"),
        ]
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.outbox)) == 1


async def test_failure_and_replacement_are_atomic_and_do_not_bypass_fence(prediction_system):
    system = prediction_system
    db, _, jobs, predictions, _ = system
    attempt = await task(system)
    rt = runtime(system)
    rt.predictions = SimpleNamespace(queue_retry=AsyncMock(side_effect=DomainError("FIXTURE", "x")))
    await Runtime._failure(rt, attempt, DomainError("AGENT_TIMEOUT", "fixture", 503))
    async with db.engine.connect() as c:
        assert await c.scalar(sa.select(s.jobs.c.state)) == "RUNNING"
        assert await c.scalar(sa.select(s.job_attempts.c.token_revoked)) is False
    await jobs.finish(attempt, state="FAILED", error={"code": "fixture-ended"})
    await Runtime._failure(runtime(system), attempt, DomainError("AGENT_TIMEOUT", "fixture", 503))
    async with db.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.jobs)) == 1
        assert await c.scalar(sa.select(s.jobs.c.last_error)) == {"code": "fixture-ended"}


async def test_existing_fresh_job_is_reused_without_duplicate_analysis(prediction_system):
    system = prediction_system
    db, clock, _, predictions, engine = system
    attempt = await task(system)
    async with db.transaction() as c:
        pending = await predictions.queue_timeframe(
            c, engine, "BTCUSDT", "1h", "new-event", clock.value
        )
        assert (
            await predictions.queue_timeframe(c, engine, "BTCUSDT", "1h", "another", clock.value)
            == pending
        )
    await Runtime._failure(runtime(system), attempt, DomainError("AGENT_TIMEOUT", "fixture", 503))
    async with db.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.jobs)) == 2


async def test_api_rejections_are_trusted_safe_and_publication_wins(prediction_system, tmp_path):
    system = prediction_system
    db, _, jobs, predictions, engine = system
    attempt = await task(system)
    services = APIServices(
        predictions.config,
        CredentialHashes(token_hash("c"), token_hash("o")),
        jobs,
        engine,
        None,
        predictions=predictions,
        knowledge=knowledge(system, tmp_path),
    )
    runner, resources = configure_runner(predictions, tmp_path, "raise SystemExit(1)")
    headers = {"Authorization": "Bearer " + attempt.token}
    value = json_value(await report(system, attempt))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://fixture"
    ) as client:
        bad = {**value, "p_up": "PRIVATE_SUBMITTED_VALUE", "PRIVATE_FIELD_NAME": "secret"}
        rejected = await client.post("/v2/research/prediction", headers=headers, json=bad)
        assert rejected.status_code == 422
        assert "PRIVATE_" not in rejected.text and "secret" not in rejected.text
        assert {"path": ["p_up"], "type": "decimal_parsing"} in rejected.json()["error"]["issues"]
        assert await jobs.last_submission_error(attempt) == "REQUEST_INVALID"
        bad = {**value, "evidence_ids": [str(uuid4())]}
        rejected = await client.post("/v2/research/prediction", headers=headers, json=bad)
        assert rejected.json()["error"]["code"] == "EVIDENCE_REFERENCE"
        assert await jobs.last_submission_error(attempt) == "EVIDENCE_REFERENCE"
        accepted = await client.post("/v2/research/prediction", headers=headers, json=value)
        assert accepted.status_code == 200
        assert str(await runner.run(attempt)) == accepted.json()["prediction_id"]
        assert resources.prepared == []
    async with db.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.audit_events)) == 2
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == 1
    await jobs.finish(attempt)
    with pytest.raises(DomainError):
        await jobs.record_submission_rejection(attempt, "CONTEXT_CHANGED", "fixture-revoked")


async def test_runner_preserves_rejection_instead_of_no_formal_result(prediction_system, tmp_path):
    _, _, jobs, predictions, _ = prediction_system
    attempt = await task(prediction_system)
    await jobs.record_submission_rejection(attempt, "CONTEXT_CHANGED", "fixture-rejection")
    runner, _ = configure_runner(predictions, tmp_path, "print('AGENT_NO_FORMAL_RESULT')")
    with pytest.raises(DomainError) as caught:
        await runner.run(attempt)
    assert caught.value.code == "CONTEXT_CHANGED"


async def test_monitor_cursor_does_not_change_decision_but_trade_revision_does(prediction_system):
    db, clock, jobs, predictions, engine = prediction_system
    async with db.transaction() as c:
        await c.execute(
            sa.insert(s.trading_positions).values(
                engine_id=engine,
                symbol="BTCUSDT",
                position_id=uuid4(),
                status="OPEN",
                body={
                    "direction": "UP",
                    "contracts": "1",
                    "structure_verified_until": clock.value.isoformat(),
                },
            )
        )
        _, _, original = await predictions._children(c, engine, "BTCUSDT", clock.value)
    attempt = await task(prediction_system, kind="PREDICT_PARENT", freeze=False)
    async with jobs.transaction(attempt) as c:
        await c.execute(
            sa.update(s.trading_positions).values(
                updated_at=clock.value + timedelta(minutes=1),
                body={
                    "direction": "UP",
                    "contracts": "1",
                    "structure_verified_until": (clock.value + timedelta(minutes=1)).isoformat(),
                },
            )
        )
        _, _, unchanged = await predictions._children(c, engine, "BTCUSDT", clock.value)
        assert unchanged == original
        await c.execute(sa.update(s.trading_positions).values(revision=2))
        _, _, changed = await predictions._children(c, engine, "BTCUSDT", clock.value)
        assert changed != original


async def test_parent_waits_for_known_minute_gap_before_freezing_or_starting_model(
    prediction_system, tmp_path
):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    attempt = await task(system, kind="PREDICT_PARENT", freeze=False)
    async with db.transaction() as c:
        await c.execute(
            sa.insert(s.prediction_streams).values(
                engine_id=engine,
                symbol="BTCUSDT",
                cursor=clock.value,
                body={"data_quality": "AWAITING_DATA"},
                updated_at=clock.value,
            )
        )
    client = FreshMarket(system)
    market = PredictionMarket(jobs, client, predictions, knowledge(system, tmp_path), engine)
    with pytest.raises(DomainError) as caught:
        await market.prepare(attempt)
    assert caught.value.code == "MINUTE_DATA_PENDING" and not client.candle_calls
    await Runtime._failure(runtime(system), attempt, caught.value)
    async with db.engine.connect() as c:
        assert await c.scalar(sa.select(s.jobs.c.state)) == "AWAITING_DATA"
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_inputs)) == 0
    async with db.transaction() as c:
        assert await predictions.queue_parent(c, engine, "BTCUSDT", clock.value) == attempt.job_id
    assert await jobs.claim(("PREDICT_PARENT",)) is None
    clock.value += timedelta(seconds=5)
    async with db.transaction() as c:
        await c.execute(sa.update(s.prediction_streams).values(body={"data_quality": "OK"}))
    retry = await jobs.claim(("PREDICT_PARENT",))
    assert retry.job_id == attempt.job_id
    await publish_child(system)  # Recovery now requires a valid child report.
    await market.prepare(retry)
    ident = await predictions.publish(retry, await submission(system, retry))
    await jobs.finish(retry, result_id=ident)


async def test_explicit_deadline_is_not_extended_by_recovery(prediction_system):
    system = prediction_system
    db, clock, jobs, _, _ = system
    attempt = await task(system)
    async with db.transaction() as c:
        await c.execute(
            sa.update(s.jobs)
            .where(s.jobs.c.id == attempt.job_id)
            .values(deadline_at=clock.value - timedelta(seconds=1))
        )
    from dataclasses import replace

    attempt = replace(attempt, deadline_at=clock.value - timedelta(seconds=1))
    await Runtime._failure(runtime(system), attempt, DomainError("CONTEXT_EXPIRED", "fixture", 409))
    async with db.engine.connect() as c:
        assert await c.scalar(sa.select(s.jobs.c.state)) == "SKIPPED"
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.jobs)) == 1


async def test_closed_position_is_not_presented_as_current_position(prediction_system):
    db, _, _, predictions, engine = prediction_system
    async with db.transaction() as c:
        await c.execute(
            sa.insert(s.trading_positions).values(
                engine_id=engine,
                symbol="BTCUSDT",
                position_id=uuid4(),
                status="CLOSED",
                body={"direction": "UP", "contracts": "0"},
            )
        )
    attempt = await parent_task(prediction_system)
    context = await predictions.context(attempt)
    assert context["position"] is None
    assert context["submission_references"]["position_id"] is None
    await predictions.publish(attempt, await submission(prediction_system, attempt))
    async with db.engine.connect() as c:
        assert await c.scalar(sa.select(s.trading_positions.c.status)) == "CLOSED"
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.trade_intents)) == 0


@pytest.mark.parametrize("kind", ["PREDICT_PARENT", "PREDICT_TIMEFRAME"])
async def test_old_duplicate_covered_by_newer_active_prediction_does_not_call_model(
    prediction_system, tmp_path, kind
):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    published = (
        await parent_task(system) if kind == "PREDICT_PARENT" else await task(system, kind=kind)
    )
    ident = await predictions.publish(published, await submission(system, published))
    await jobs.finish(published, result_id=ident)
    async with db.transaction() as c:
        duplicate = await jobs.create(
            c, kind, "fixture-old-duplicate", published.payload, clock.value
        )
        await c.execute(
            sa.update(s.jobs)
            .where(s.jobs.c.id == duplicate)
            .values(created_at=clock.value - timedelta(seconds=1))
        )
    attempt = await jobs.claim((kind,))
    client = FreshMarket(system)
    market = PredictionMarket(jobs, client, predictions, knowledge(system, tmp_path), engine)
    with pytest.raises(DomainError) as caught:
        await market.prepare(attempt)
    assert caught.value.code == "ANALYSIS_SUPERSEDED" and not client.candle_calls
    await Runtime._failure(runtime(system), attempt, caught.value)
    async with db.engine.connect() as c:
        assert (
            await c.scalar(sa.select(s.jobs.c.state).where(s.jobs.c.id == duplicate)) == "SKIPPED"
        )
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_inputs)) == (
            2 if kind == "PREDICT_PARENT" else 1
        )
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.outbox)) == 0


async def test_parent_duplicate_must_reanalyse_when_children_changed(prediction_system, tmp_path):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    published = await parent_task(system)
    ident = await predictions.publish(published, await submission(system, published))
    await jobs.finish(published, result_id=ident)
    async with db.transaction() as c:
        duplicate = await jobs.create(
            c, published.kind, "fixture-old-parent", published.payload, clock.value
        )
        await c.execute(
            sa.update(s.jobs)
            .where(s.jobs.c.id == duplicate)
            .values(created_at=clock.value - timedelta(seconds=1))
        )
    await publish_child(system)
    attempt = await jobs.claim(("PREDICT_PARENT",))
    assert attempt.job_id == duplicate
    client = FreshMarket(system)
    market = PredictionMarket(jobs, client, predictions, knowledge(system, tmp_path), engine)
    await market.prepare(attempt)
    assert not client.candle_calls  # Re-freeze child reports, without parent market I/O.
    await predictions.publish(attempt, await submission(system, attempt))


@pytest.mark.parametrize("lifecycle", ["ACTIVE", "INVALIDATED"])
async def test_parent_waits_outside_transaction_and_still_rejects_real_invalidation(
    prediction_system, monkeypatch, lifecycle
):
    system = prediction_system
    db, _, jobs, predictions, _ = system
    await publish_child(system)
    attempt = await task(system, kind="PREDICT_PARENT")
    payload = await submission(system, attempt)
    child_id = payload.child_prediction_ids[0]
    async with jobs.transaction(attempt) as c:
        await c.execute(
            sa.update(s.prediction_states)
            .where(s.prediction_states.c.prediction_id == child_id)
            .values(data_quality="AWAITING_DATA")
        )
    transaction = db.transaction
    depth = ContextVar("transaction_depth", default=0)

    @asynccontextmanager
    async def tracked_transaction():
        token = depth.set(depth.get() + 1)
        try:
            async with transaction() as c:
                yield c
        finally:
            depth.reset(token)

    slept = []

    async def verified_after_delay(seconds):
        assert depth.get() == 0  # waiting/I/O never holds the publishing transaction
        slept.append(seconds)
        async with transaction() as c:
            body = dict(
                await c.scalar(
                    sa.select(s.prediction_states.c.body).where(
                        s.prediction_states.c.prediction_id == child_id
                    )
                )
            )
            body["lifecycle"] = lifecycle
            await c.execute(
                sa.update(s.prediction_states)
                .where(s.prediction_states.c.prediction_id == child_id)
                .values(data_quality="OK", body=body)
            )
        await asyncio.sleep(0)

    monkeypatch.setattr(db, "transaction", tracked_transaction)
    monkeypatch.setattr("trading_agent.prediction.service.sleep", verified_after_delay)
    if lifecycle == "ACTIVE":
        await predictions.publish(attempt, payload)
    else:
        with pytest.raises(DomainError) as caught:
            await predictions.publish(attempt, payload)
        assert caught.value.code == "CONTEXT_CHANGED"
    assert slept == [1]


async def test_unresolved_minute_verification_is_not_misreported_as_context_change(
    prediction_system, monkeypatch
):
    system = prediction_system
    db, _, jobs, predictions, _ = system
    await publish_child(system)
    attempt = await task(system, kind="PREDICT_PARENT")
    payload = await submission(system, attempt)
    async with jobs.transaction(attempt) as c:
        await c.execute(sa.update(s.prediction_states).values(data_quality="AWAITING_DATA"))
    waited = []

    async def tick(seconds):
        waited.append(seconds)

    monkeypatch.setattr("trading_agent.prediction.service.sleep", tick)
    with pytest.raises(DomainError) as caught:
        await predictions.publish(attempt, payload)
    assert caught.value.code == "MINUTE_DATA_PENDING" and caught.value.status == 503
    assert waited == [1, 1, 1, 1]
    async with db.engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == 1


async def test_publication_committing_between_failure_reads_wins_atomically(prediction_system):
    system = prediction_system
    db, _, _, predictions, _ = system
    attempt = await parent_task(system)
    payload = await submission(system, attempt)
    ident = None

    async def stale_read(_):
        nonlocal ident
        ident = await predictions.publish(attempt, payload)
        return None  # initial result read raced with the authoritative publication

    rt = runtime(system)
    rt.runner._result = stale_read
    await Runtime._failure(rt, attempt, DomainError("AGENT_TIMEOUT", "fixture", 503))
    async with db.engine.connect() as c:
        row = (
            (await c.execute(sa.select(s.jobs).where(s.jobs.c.id == attempt.job_id)))
            .mappings()
            .one()
        )
        assert row["state"] == "SUCCEEDED" and row["result_id"] == ident
        assert row["last_error"] is None
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == 2
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.outbox)) == 0
