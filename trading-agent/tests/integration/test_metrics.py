"""Native usage for current structural tasks, independent of legacy research fixtures."""

import httpx
import pytest
import sqlalchemy as sa
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import task

from trading_agent.api.app import APIServices, create_app
from trading_agent.api.auth import CredentialHashes
from trading_agent.api.contracts import ModelRun
from trading_agent.api.metrics import run_metrics
from trading_agent.domain.errors import DomainError
from trading_agent.jobs.service import token_hash
from trading_agent.knowledge.service import KnowledgeService
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


async def test_structural_usage_auth_idempotence_conflicts_and_missing_counters(prediction_system):
    database, _, jobs, service, engine = prediction_system
    attempt = await task(prediction_system, timeframe="4h")
    services = APIServices(
        service.config,
        CredentialHashes(token_hash("fixture-controller"), token_hash("fixture-operator")),
        jobs,
        engine,
        None,
        predictions=service,
        knowledge=KnowledgeService(jobs, service.config),
    )
    runs = [
        {
            "session_id": "native-parent",
            "role": "parent",
            "status": "running",
            "api_calls": 2,
            "input_tokens": 1000,
        },
        {
            "session_id": "native-4h",
            "role": "4h",
            "status": "failed",
            "duration_seconds": 3,
            "api_calls": 2,
            "input_tokens": 100,
        },
    ]
    route = "/v2/research/usage"
    headers = {"Authorization": "Bearer " + attempt.token}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://fixture"
    ) as client:
        assert (await client.post(route, json={"runs": runs})).status_code == 401
        assert (
            await client.post(
                route, json={"runs": runs}, headers={"Authorization": "Bearer fixture-controller"}
            )
        ).status_code == 403
        for _ in range(2):
            assert (
                await client.post(route, json={"runs": runs}, headers=headers)
            ).status_code == 200
        changed = {"runs": [{**runs[1], "input_tokens": 101}]}
        assert (await client.post(route, json=changed, headers=headers)).status_code == 409
        invalid = {"runs": [{**runs[1], "duration_seconds": "nan"}]}
        assert (await client.post(route, json=invalid, headers=headers)).status_code == 422
    async with database.engine.connect() as connection:
        usage = await connection.scalar(
            sa.select(s.job_attempts.c.usage_json).where(s.job_attempts.c.id == attempt.id)
        )
    summary = run_metrics([{"usage_json": usage}])
    assert summary["attempts_with_usage"] == 1
    assert summary["roles"]["4h"]["input_tokens"] == {"known_sessions": 1, "total": 100}
    assert summary["roles"]["4h"]["failure_rate"] == 1
    assert summary["roles"]["6h"]["input_tokens"] == {"known_sessions": 0, "total": None}
    assert summary["roles"]["12h"]["p95_seconds"] is None
    assert len(usage["model_runs"]) == 2
    await jobs.finish(attempt)
    with pytest.raises(DomainError):
        await jobs.record_model_usage(attempt, (ModelRun(**runs[0]),))


async def test_parent_usage_is_monotonic_and_child_budget_is_fixed(prediction_system):
    database, _, jobs, _, _ = prediction_system
    attempt = await task(prediction_system)
    parent = ModelRun(session_id="parent", role="parent", status="running", api_calls=3)
    await jobs.record_model_usage(attempt, (parent,))
    with pytest.raises(DomainError, match="累计"):
        await jobs.record_model_usage(attempt, (parent.model_copy(update={"api_calls": 2}),))
    await jobs.record_model_usage(attempt, (parent.model_copy(update={"api_calls": 4}),))
    await jobs.record_model_usage(
        attempt, (ModelRun(session_id="one", role="1h", status="completed"),)
    )
    with pytest.raises(DomainError, match="预算"):
        await jobs.record_model_usage(
            attempt, (ModelRun(session_id="two", role="1h", status="completed"),)
        )
    async with database.engine.connect() as connection:
        usage = await connection.scalar(
            sa.select(s.job_attempts.c.usage_json).where(s.job_attempts.c.id == attempt.id)
        )
    assert len(usage["model_runs"]) == 2
    assert usage["model_runs"]["parent"]["api_calls"] == 4
