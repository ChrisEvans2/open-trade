"""Controller requests reach current prediction tasks, without trading or legacy workers."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from conftest import bars_at
from fact_fixtures import historical_forecast
from test_prediction_execution import executor, opening
from test_prediction_knowledge import change, knowledge
from test_prediction_pipeline import minute, publish_child, task
from test_prediction_pipeline import prediction_system as prediction_system

from trading_agent.api.app import APIServices, create_app
from trading_agent.api.auth import CredentialHashes
from trading_agent.config import NotificationConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.time import TIMEFRAMES, boundary
from trading_agent.jobs.service import token_hash
from trading_agent.knowledge.contracts import KnowledgeReview
from trading_agent.notifications.outbox import HermesSender, Notifier
from trading_agent.prediction.market import PredictionMarket
from trading_agent.runtime.lifecycle import Runtime
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres
AUTH = {"Authorization": "Bearer fixture-controller"}


def api(system, service):
    return create_app(
        APIServices(
            service.config,
            CredentialHashes(token_hash("fixture-controller"), token_hash("fixture-operator")),
            system[2],
            system[4],
            None,
            predictions=system[3],
            knowledge=service,
        )
    )


async def test_controller_request_authorization_idempotency_and_gates(prediction_system, tmp_path):
    system = prediction_system
    db, clock, jobs, _, _ = system
    service = knowledge(system, tmp_path)
    ident = await publish_child(system)
    payload = {"kind": "explain", "prediction_id": str(ident)}
    headers = {**AUTH, "Idempotency-Key": "fixture-command"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api(system, service)), base_url="http://fixture"
    ) as client:
        assert (await client.post("/v2/jobs", content=b"not-json")).status_code == 401
        researcher = await task(system, freeze=False)
        assert (
            await client.post(
                "/v2/jobs", json=payload, headers={"Authorization": "Bearer " + researcher.token}
            )
        ).status_code == 403
        created = await client.post("/v2/jobs", json=payload, headers=headers)
        assert created.status_code == 202, created.text
        replay = await client.post("/v1/jobs", json=payload, headers=headers)
        assert replay.status_code == 202 and replay.json() == created.json()
        conflict = await client.post(
            "/v2/jobs", json={"kind": "explain", "prediction_id": str(uuid4())}, headers=headers
        )
        assert conflict.status_code == 409
        assert (await client.post("/v2/jobs", json=payload, headers=AUTH)).status_code == 422
        assert (
            await client.post("/v2/jobs", json={**payload, "order": "BUY"}, headers=headers)
        ).status_code == 422
        bad = {
            "kind": "review",
            "range_start": (clock.value - timedelta(days=1)).isoformat(),
            "range_end": (clock.value + timedelta(seconds=1)).isoformat(),
        }
        assert (
            await client.post("/v2/jobs", json=bad, headers={**AUTH, "Idempotency-Key": "future"})
        ).status_code == 422
        disabled = await client.post(
            "/v2/jobs",
            json={"kind": "experiment"},
            headers={**AUTH, "Idempotency-Key": "experiment"},
        )
        assert (
            disabled.status_code == 409
            and disabled.json()["error"]["code"] == "EXPERIMENT_DISABLED"
        )
        for path in ("/v1/status", "/v2/positions"):
            assert (await client.get(path, headers=AUTH)).status_code == 200
            assert (await client.get(path)).status_code == 401
        async with db.transaction() as connection:
            await connection.execute(
                sa.update(s.runtime_control).where(s.runtime_control.c.id == 1).values(paused=True)
            )
        assert (
            await client.post("/v2/jobs", json=payload, headers=headers)
        ).json() == created.json()
        paused = await client.post(
            "/v2/jobs", json=payload, headers={**AUTH, "Idempotency-Key": "paused"}
        )
        assert paused.status_code == 409 and paused.json()["error"]["code"] == "SCHEDULE_PAUSED"
    async with db.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(sa.func.count())
                .select_from(s.audit_events)
                .where(s.audit_events.c.action == "controller_job_requested")
            )
            == 1
        )
        assert (
            await connection.scalar(
                sa.select(sa.func.count())
                .select_from(s.jobs)
                .where(s.jobs.c.kind == "PREDICT_REVIEW")
            )
            == 1
        )


@pytest.mark.parametrize("historical", [False, True])
async def test_controller_explanation_frozen_result_and_gateway_notification(
    prediction_system, tmp_path, monkeypatch, historical
):
    system = prediction_system
    db, _, jobs, _, engine = system
    service = knowledge(system, tmp_path)
    service.config = service.config.model_copy(
        update={
            "notification": NotificationConfig(target="telegram:fixture-target"),
            "hermes": service.config.hermes.model_copy(
                update={
                    "executable": tmp_path / "fixture-hermes",
                    "controller_profile": "fixture-controller",
                }
            ),
        }
    )
    if historical:
        history = await historical_forecast(system, with_score=True)
        ident = history.id
    else:
        ident = await publish_child(system)
    async with db.engine.connect() as connection:
        tables = [
            s.prediction_facts,
            s.prediction_evaluations,
            s.forecasts,
            s.settlements,
            s.trade_intents,
            s.execution_events,
        ]
        before = {
            table.name: (await connection.execute(sa.select(table))).mappings().all()
            for table in tables
        }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api(system, service)), base_url="http://fixture"
    ) as client:
        # Old field name and URL remain compatible with the existing controller tool contract.
        response = await client.post(
            "/v1/jobs",
            json={"kind": "explain", "forecast_id": str(ident)},
            headers={**AUTH, "Idempotency-Key": "explain"},
        )
        assert response.status_code == 202, response.text
        attempt = await jobs.claim(("PREDICT_REVIEW",))
        assert str(attempt.job_id) == response.json()["job_id"]
        market = PredictionMarket(jobs, None, system[3], service, engine)
        await market.prepare(attempt)  # No market client: explanation only uses original facts.
        context = await service.review_context(attempt)
        assert context["requested_prediction"]["fact"]["id"] == str(ident)
        assert context["original_input"]
        assert bool(context["evaluations"]) == historical
        with pytest.raises(DomainError, match="只能提交叙述"):
            await service.submit(attempt, KnowledgeReview(narrative="越权", changes=[change()]))
        review = KnowledgeReview(narrative="依据已发布预测及原始输入解释；没有补造验证数据。")
        result = await service.submit(attempt, review)
        assert await service.submit(attempt, review) == result
        await jobs.finish(attempt, result_id=result)
        state = await client.get(f"/v1/jobs/{attempt.job_id}", headers=AUTH)
        assert state.json()["result_id"] == str(result)
        assert (await client.get(f"/v1/reviews/{result}", headers=AUTH)).json()["evidence_ids"] == [
            str(ident)
        ]
    commands = []

    async def start(command, **options):
        commands.append((command, options))

        async def wait(_timeout):
            return SimpleNamespace(
                returncode=0, stdout=b'{"success":true,"message_id":"fixture-message"}'
            )

        return SimpleNamespace(wait=wait)

    monkeypatch.setattr("trading_agent.notifications.outbox.ManagedProcess.start", start)
    notifier = Notifier(db, jobs.ownership, HermesSender(service.config), service.config)
    await notifier.tick()
    assert len(commands) == 1
    command, options = commands[0]
    assert command[1:6] == ["-p", "fixture-controller", "send", "--to", "telegram:fixture-target"]
    assert str(result).encode() in options["input_bytes"]
    assert "TA_RESEARCH_TOKEN" not in options["env"]
    async with db.engine.connect() as connection:
        assert await connection.scalar(sa.select(s.outbox.c.status)) == "SENT"
        assert (
            await connection.scalar(sa.select(s.outbox.c.transport_message_id)) == "fixture-message"
        )
        for table in tables:
            assert (await connection.execute(sa.select(table))).mappings().all() == before[
                table.name
            ]


async def test_requested_review_half_open_range_and_symbol_fanout(prediction_system, tmp_path):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    service = knowledge(system, tmp_path)
    start = clock.value
    first = await publish_child(system)
    clock.value += timedelta(seconds=1)
    end = clock.value
    excluded = await publish_child(system, timeframe="4h")
    clock.value += timedelta(minutes=2)
    verifier = await task(system, kind="PREDICT_VERIFY", freeze=False)
    for ident in (first, excluded):
        await predictions.verify(
            verifier,
            ident,
            (minute("BTC-USDT-SWAP", start.replace(minute=2, second=0, microsecond=0)),),
        )
    await jobs.finish(verifier)
    payload = {"kind": "review", "range_start": start.isoformat(), "range_end": end.isoformat()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api(system, service)), base_url="http://fixture"
    ) as client:
        response = await client.post(
            "/v2/jobs", json=payload, headers={**AUTH, "Idempotency-Key": "review-range"}
        )
        assert response.status_code == 202 and len(response.json()["job_ids"]) == 2
    attempt = await jobs.claim(("PREDICT_REVIEW",))
    if attempt.payload["symbol"] != "BTCUSDT":
        await jobs.finish(attempt)
        attempt = await jobs.claim(("PREDICT_REVIEW",))
    assert attempt.payload["symbol"] == "BTCUSDT"
    anchor = boundary(clock.value)
    await service.freeze_review(
        attempt, engine, {tf: bars_at(anchor, tf) for tf in TIMEFRAMES}, anchor=anchor
    )
    context = await service.review_context(attempt)
    assert {case["prediction_id"] for case in context["cases"]} == {str(first)}
    result = await service.submit(attempt, KnowledgeReview(narrative="仅复盘指定半开区间内预测。"))
    async with db.engine.connect() as connection:
        row = (
            (await connection.execute(sa.select(s.reviews).where(s.reviews.c.id == result)))
            .mappings()
            .one()
        )
        assert row["range_start"] == start and row["range_end"] == end
        assert row["evidence_ids"] == [str(first)]


@pytest.mark.parametrize("rejected", [False, True])
async def test_execution_facts_queue_one_controller_notification(prediction_system, rejected):
    system = prediction_system
    await opening(system)
    service = executor(system)
    service.config = service.config.model_copy(
        update={"notification": NotificationConfig(target="telegram:fixture-target")}
    )
    if rejected:
        service.settings.limits = replace(service.settings.limits, allow_new_entries=False)
    attempt = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(attempt)
    await service.run(attempt)
    expected = "REJECTED" if rejected else "FILLED"
    async with system[0].engine.connect() as connection:
        assert await connection.scalar(sa.select(s.trade_intent_states.c.status)) == expected
        rows = (await connection.execute(sa.select(s.outbox))).mappings().all()
        assert len(rows) == 1 and expected in rows[0]["body"]
        assert rows[0]["target"] == "telegram:fixture-target"


async def test_terminal_job_failure_queues_existing_gateway_notification(prediction_system):
    system = prediction_system
    attempt = await task(system, kind="PREDICT_REVIEW", freeze=False)

    async def no_result(_attempt):
        return None

    runtime = SimpleNamespace(
        database=system[0],
        jobs=system[2],
        ownership=system[2].ownership,
        runner=SimpleNamespace(_result=no_result),
        agent_health={},
        config=system[3].config.model_copy(
            update={"notification": NotificationConfig(target="telegram:fixture-target")}
        ),
    )
    await Runtime._failure(runtime, attempt, DomainError("FIXTURE_FAILED", "明确的测试失败", 422))
    async with system[0].engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(s.jobs.c.state).where(s.jobs.c.id == attempt.job_id))
            == "FAILED"
        )
        assert "FIXTURE_FAILED" in await connection.scalar(sa.select(s.outbox.c.body))
