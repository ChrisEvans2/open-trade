import httpx
import pytest
import sqlalchemy as sa
from test_prediction_knowledge import knowledge, review_task
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import report, task

from trading_agent.api.app import APIServices, create_app
from trading_agent.api.auth import CredentialHashes
from trading_agent.domain.serialization import json_value
from trading_agent.jobs.service import token_hash
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


async def test_v2_auth_frozen_knowledge_and_publication(prediction_system, tmp_path):
    system = prediction_system
    service = knowledge(system, tmp_path)
    await service.seed()
    attempt = await task(system)
    credentials = CredentialHashes(token_hash("fixture-controller"), token_hash("fixture-operator"))
    services = APIServices(
        system[3].config,
        credentials,
        system[2],
        system[4],
        None,
        predictions=system[3],
        knowledge=service,
    )
    headers = {"Authorization": "Bearer " + attempt.token}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://fixture"
    ) as client:
        assert (await client.get("/v2/research/context")).status_code == 401
        assert (
            await client.get("/v2/research/context?symbol=ETHUSDT", headers=headers)
        ).status_code == 403
        context = await client.get("/v2/research/context", headers=headers)
        assert context.status_code == 200 and context.json()["symbol"] == "BTCUSDT"
        invalid = await client.post("/v2/research/decision", headers=headers, json={})
        assert invalid.status_code == 403  # Role authorization precedes body parsing.
        cards = await client.post(
            "/v2/research/knowledge/search", headers=headers, json={"structure": "W_BOTTOM"}
        )
        assert cards.status_code == 200 and cards.json()["degraded"]
        card = cards.json()["items"][0]
        content = await client.get(
            "/v2/research/knowledge/content",
            headers=headers,
            params={"version_id": card["id"], "limit": 100},
        )
        assert content.status_code == 200 and content.json()["next_offset"] == 100
        assert (
            await client.post("/v2/research/knowledge/search", headers=headers, json={"limit": 6})
        ).status_code == 422
        assert (
            await client.post("/v2/research/review", headers=headers, json={})
        ).status_code == 403
        assert (await client.post("/v2/execution", headers=headers, json={})).status_code == 403
        response = await client.post(
            "/v2/research/prediction",
            headers=headers,
            json=json_value(await report(system, attempt)),
        )
        assert response.status_code == 200
        controller = {"Authorization": "Bearer fixture-controller"}
        history = await client.get(
            "/v2/predictions/" + response.json()["prediction_id"], headers=controller
        )
        assert history.status_code == 200 and history.json()["role"] == "TIMEFRAME"
        latest = await client.get(
            "/v2/predictions/latest",
            headers=controller,
            params={"symbol": "BTCUSDT", "parent": False},
        )
        assert latest.status_code == 200 and latest.json()["data_quality"] == "OK"
        assert (await client.get("/v2/research/context", headers=controller)).status_code == 403
        reviewer = await review_task(system, service)
        review_headers = {"Authorization": "Bearer " + reviewer.token}
        assert (await client.get("/v2/research/context", headers=review_headers)).status_code == 200
        review = await client.post(
            "/v2/research/review",
            headers=review_headers,
            json={"narrative": "样本不足，保留初始规则", "changes": []},
        )
        assert review.status_code == 200 and "review_id" in review.json()


@pytest.mark.parametrize("preserve", [False, True])
async def test_only_operator_resume_can_preserve_or_clear_durable_account_halt(
    prediction_system, tmp_path, preserve
):
    system = prediction_system
    async with system[0].transaction() as c:
        await c.execute(
            sa.insert(s.trading_accounts).values(
                account_key="fixture", body={"risk_halt": {"reason": "fixture"}, "account": {}}
            )
        )
        await c.execute(sa.update(s.runtime_control).values(paused=True))
    services = APIServices(
        system[3].config,
        CredentialHashes(token_hash("c"), token_hash("o")),
        system[2],
        system[4],
        None,
        predictions=system[3],
        knowledge=knowledge(system, tmp_path),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://fixture"
    ) as client:
        assert (
            await client.post(
                "/v1/schedule/resume",
                headers={"Authorization": "Bearer c", "Idempotency-Key": "reject"},
                params={"preserve_risk_halts": True},
            )
        ).status_code == 403
        response = await client.post(
            "/v1/schedule/resume",
            headers={"Authorization": "Bearer o", "Idempotency-Key": "resume"},
            params={"preserve_risk_halts": True} if preserve else {},
        )
        assert response.status_code == 200
        assert (
            await client.post(
                "/v1/schedule/resume",
                headers={"Authorization": "Bearer o", "Idempotency-Key": "resume"},
                params={"preserve_risk_halts": not preserve},
            )
        ).status_code == 409
    async with system[0].engine.connect() as c:
        body = await c.scalar(sa.select(s.trading_accounts.c.body))
        assert body == (
            {"risk_halt": {"reason": "fixture"}, "account": {}} if preserve else {"account": {}}
        )
        assert not await c.scalar(sa.select(s.runtime_control.c.paused))
        assert await c.scalar(sa.select(s.trading_accounts.c.revision)) == (1 if preserve else 2)
