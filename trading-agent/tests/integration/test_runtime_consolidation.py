"""Retiring writers must preserve historical facts and reject dormant legacy work."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from fact_fixtures import historical_forecast
from test_prediction_pipeline import prediction_system as prediction_system

from trading_agent.api.app import APIServices, create_app
from trading_agent.api.auth import CredentialHashes
from trading_agent.domain.serialization import content_hash
from trading_agent.jobs.service import token_hash
from trading_agent.runtime.lifecycle import Runtime
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


async def fact_digest(database):
    # Include every immutable fact table, including published forecasts, scores and audits.
    from trading_agent.storage.schema import IMMUTABLE_TABLES

    async with database.engine.connect() as connection:
        result = {}
        for name in sorted(IMMUTABLE_TABLES):
            table = s.metadata.tables[name]
            rows = (await connection.execute(sa.select(table))).mappings().all()
            result[name] = sorted(content_hash(dict(row)) for row in rows)
        return result


async def test_old_research_writers_retire_before_parsing_and_history_stays_readable(
    prediction_system,
):
    database, _, jobs, service, engine = prediction_system
    history = await historical_forecast(prediction_system)
    attempt, ident, payload = history.attempt, history.id, history.payload
    before = await fact_digest(database)
    services = APIServices(
        service.config,
        CredentialHashes(token_hash("fixture-controller"), token_hash("fixture-operator")),
        jobs,
        engine,
        None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://fixture"
    ) as client:
        controller = {"Authorization": "Bearer fixture-controller"}
        research_headers = {"Authorization": "Bearer " + attempt.token}
        assert (await client.post("/v1/jobs", content=b"not-json")).status_code == 401
        for method, path, headers in [
            ("GET", "/v1/research/context", research_headers),
            ("POST", "/v1/research/timeframes", research_headers),
            ("POST", "/v1/research/forecast", research_headers),
        ]:
            response = await client.request(method, path, headers=headers, content=b"not-json")
            assert response.status_code == 410, response.text
            assert response.json()["error"]["code"] == "LEGACY_FLOW_RETIRED"
        for path in ("/v1/forecasts/latest", f"/v1/forecasts/{ident}"):
            response = await client.get(path, headers=controller)
            assert response.status_code == 200
            assert response.json()["id"] == str(ident)
            assert response.json()["payload"] == payload
    assert await fact_digest(database) == before


async def test_recovery_retires_legacy_queue_without_revising_facts(prediction_system):
    database, clock, jobs, service, _ = prediction_system
    history = await historical_forecast(prediction_system)
    attempt, ident = history.attempt, history.id
    clock.value += timedelta(minutes=5)
    pending = {}
    async with database.transaction() as connection:
        for kind in (
            "FORECAST",
            "SETTLE",
            "PRICE_REFRESH",
            "REVIEW_DAILY",
            "EXPLAIN",
            "EXPERIMENT",
        ):
            pending[kind] = await jobs.create(
                connection,
                kind,
                "retirement:" + kind,
                {},
                clock.value,
                clock.value + timedelta(days=1),
            )
        await connection.execute(
            sa.update(s.jobs).where(s.jobs.c.id == pending["SETTLE"]).values(state="AWAITING_DATA")
        )
    before = await fact_digest(database)
    runtime = SimpleNamespace(database=database, ownership=jobs.ownership)
    await Runtime.recover_jobs(runtime)
    await Runtime.recover_jobs(runtime)
    async with database.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(s.jobs.c.result_id).where(s.jobs.c.id == attempt.job_id)
            )
            == ident
        )
        rows = (
            (await connection.execute(sa.select(s.jobs).where(s.jobs.c.id.in_(pending.values()))))
            .mappings()
            .all()
        )
        assert len(rows) == 6
        assert all(row["state"] == "SKIPPED" for row in rows)
        assert all(row["last_error"] == {"code": "LEGACY_FLOW_RETIRED"} for row in rows)
    assert await fact_digest(database) == before


async def test_existing_settlement_score_and_audit_survive_history_reads(prediction_system):
    database, _, jobs, service, engine = prediction_system
    history = await historical_forecast(prediction_system, with_score=True)
    ident, score_id = history.id, history.score_id
    before = await fact_digest(database)
    assert before["settlements"] and before["audit_events"]
    services = APIServices(
        service.config,
        CredentialHashes(token_hash("fixture-controller"), token_hash("fixture-operator")),
        jobs,
        engine,
        None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"/v1/forecasts/{ident}", headers={"Authorization": "Bearer fixture-controller"}
        )
        assert response.status_code == 200
        assert response.json()["latest_settlement"]["id"] == str(score_id)
        assert Decimal(response.json()["latest_settlement"]["brier"]) == Decimal("0.26")
    await Runtime.recover_jobs(SimpleNamespace(database=database, ownership=jobs.ownership))
    assert await fact_digest(database) == before
