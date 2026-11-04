from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa
from test_prediction_knowledge import knowledge
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import publish_child, report, task

from trading_agent.api.app import APIServices, create_app
from trading_agent.api.auth import CredentialHashes
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash
from trading_agent.knowledge.review_inputs import build_case_inputs, collect_minutes
from trading_agent.prediction.contracts import ParentDecision
from trading_agent.prediction.market import PredictionMarket
from trading_agent.prediction.submissions import model_context
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


class NoMarket:
    def __getattr__(self, name):
        raise AssertionError(f"Parent must not collect its own market data: {name}")


async def test_parent_without_one_hour_child_uses_four_hour_boundaries_and_one_hour_horizon(
    prediction_system,
):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    child = await publish_child(system, "4h")
    parent = await task(system, kind="PREDICT_PARENT", freeze=False)
    market = PredictionMarket(
        jobs, NoMarket(), predictions, SimpleNamespace(search=AsyncMock()), engine
    )
    ident = await market.prepare(parent)
    context = await predictions.context(parent)
    view = model_context(context)
    assert context["missing_timeframes"] == ["1h", "6h", "12h"]
    assert "model_bars" not in context and "features" not in context
    assert "execution_basis" not in context and "reference_price" not in view
    assert {b["source_child_ref"] for b in view["boundary_options"]} == {"child.4h"}
    assert await market.prepare(parent) == ident
    own = await report(system, parent)
    decision = ParentDecision(
        prediction=own,
        action="HOLD",
        trade_style="TREND",
        child_prediction_ids=[child],
        context_revision=context["context_revision"],
        relationship="4h结构约束下的1h尺度综合；1h子报告缺失",
    )
    bad = own.scenarios[0].trigger.model_copy(
        update={"tolerance": own.scenarios[0].trigger.tolerance + 1}
    )
    with pytest.raises(DomainError, match="父边界和容差"):
        await predictions.publish(
            parent,
            decision.model_copy(
                update={
                    "prediction": own.model_copy(
                        update={
                            "scenarios": (own.scenarios[0].model_copy(update={"trigger": bad}),)
                        }
                    )
                }
            ),
        )
    published = await predictions.publish(parent, decision)
    async with db.engine.connect() as c:
        row = (
            (
                await c.execute(
                    sa.select(s.prediction_inputs).where(s.prediction_inputs.c.id == ident)
                )
            )
            .mappings()
            .one()
        )
        assert row["body"]["execution_basis"] is None
        assert row["body"]["bar_ids"] == [] and content_hash(row["body"]) == row["content_hash"]
        fact = (
            (
                await c.execute(
                    sa.select(s.prediction_facts).where(s.prediction_facts.c.id == published)
                )
            )
            .mappings()
            .one()
        )
        assert fact["expires_at"] - fact["published_at"] == timedelta(hours=own.horizon_bars)
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.market_bars)) == 80
        case = {
            "prediction_id": published,
            "prediction": fact["body"],
            "evaluation_id": "fixture",
            "evaluation": {},
            "original_input": {
                "id": ident,
                "body": row["body"],
                "content_hash": row["content_hash"],
                "decision_at": row["decision_at"],
                "timeframe": "1h",
            },
        }
        assert await collect_minutes([case], NoMarket(), clock.value) == {}
        summaries, details = await build_case_inputs(c, [case], {}, predictions.config, clock.value)
        assert summaries[0]["candle_evidence"] == {
            "kind": "CHILD_SYNTHESIS",
            "quality": "NOT_APPLICABLE",
        }
        assert details[str(published)]["candle_reconstruction"] is None


async def test_parent_drilldown_is_frozen_scoped_and_lease_guarded(prediction_system, tmp_path):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    await publish_child(system, "4h")
    parent = await task(system, kind="PREDICT_PARENT")
    before = deepcopy(await predictions.child_evidence(parent, "child.4h"))
    assert len(before["context"]["model_bars"]) == 30
    await publish_child(system, "4h")
    await publish_child(system, "1h", symbol="ETHUSDT")
    assert await predictions.child_evidence(parent, "child.4h") == before
    app = create_app(
        APIServices(
            predictions.config,
            CredentialHashes("a" * 64, "b" * 64),
            jobs,
            engine,
            None,
            predictions=predictions,
            knowledge=knowledge(system, tmp_path),
        )
    )
    headers = {"Authorization": "Bearer " + parent.token}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get("/v2/research/context?child_ref=child.4h", headers=headers)
        assert response.status_code == 200 and response.json() == before
        for query in (
            "child_ref=child.1h",
            "child_ref=child.4h&symbol=ETHUSDT",
            "child_ref=child.4h&child_ref=child.4h",
            "prediction_id=arbitrary",
        ):
            assert (
                await client.get("/v2/research/context?" + query, headers=headers)
            ).status_code == 403
        child_attempt = await task(system)
        assert (
            await client.get(
                "/v2/research/context?child_ref=child.4h",
                headers={"Authorization": "Bearer " + child_attempt.token},
            )
        ).status_code == 403
    clock.value += timedelta(minutes=10)
    with pytest.raises(DomainError):
        await predictions.child_evidence(parent, "child.4h")


async def test_parent_without_children_does_not_fabricate_market_context(prediction_system):
    system = prediction_system
    parent = await task(system, kind="PREDICT_PARENT", freeze=False)
    with pytest.raises(DomainError, match="没有有效子预测"):
        await system[3].freeze_parent(parent, system[4])
    async with system[0].engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.prediction_inputs)) == 0


async def test_parent_adopts_selected_four_hour_box_with_one_hour_rules(prediction_system):
    from decimal import Decimal

    from conftest import bars_at

    from trading_agent.prediction.contracts import PredictionReport
    from trading_agent.prediction.submissions import (
        DecisionContent,
        PredictionContent,
        bind_decision,
    )

    system = prediction_system
    db, clock, jobs, predictions, engine = system
    child = await task(system, timeframe="4h", freeze=False)
    bars = tuple(
        b.model_copy(
            update={
                "open": Decimal(105),
                "close": Decimal(105),
                "high": Decimal(110 if i in (55, 65) else 106),
                "low": Decimal(95 if i in (58, 68) else 104),
            }
        )
        for i, b in enumerate(bars_at(clock.value.replace(minute=0, second=0, microsecond=0), "4h"))
    )
    await predictions.freeze(child, engine, bars)
    ctx = await predictions.context(child)
    box = next(c for c in ctx["structures"] if c["kind"] == "BOX")
    rule = next(
        r for r in ctx["horizon_rules"] if r["structure"] == "BOX" and r["stage"] == box["stage"]
    )
    own = PredictionReport(
        symbol="BTCUSDT",
        timeframe="4h",
        structure_id=box["id"],
        structure="BOX",
        stage=box["stage"],
        horizon_rule_id=rule["id"],
        horizon_bars=rule["bars"],
        p_up="0.7",
        p_down=0,
        p_none="0.3",
        primary_direction="UP",
        reason="fixture 4h box",
        scenarios=[
            dict(
                direction="UP",
                trigger=dict(behavior="CLOSE_ABOVE", line=box["upper"], tolerance=box["tolerance"]),
                invalidation=dict(
                    behavior="CLOSE_BELOW", line=box["lower"], tolerance=box["tolerance"]
                ),
                target=dict(low=120, high=130),
                target_probability="0.6",
            )
        ],
    )
    child_id = await predictions.publish(child, own)
    await jobs.finish(child, result_id=child_id)
    parent = await task(system, kind="PREDICT_PARENT")
    context = await predictions.context(parent)
    view = model_context(context)
    selected = next(c for c in view["structures"] if c["kind"] == "BOX")
    assert selected["source_child_ref"] == "child.4h"
    parent_rule = next(r for r in view["horizon_rules"] if r["structure"] == "BOX")
    draft = DecisionContent(
        prediction=PredictionContent(
            structure_ref=selected["ref"],
            horizon_ref=parent_rule["ref"],
            p_up="0.7",
            p_down=0,
            p_none="0.3",
            primary_direction="UP",
            scenarios=own.scenarios,
            reason="4h箱体约束1h尺度后续",
            evidence_refs=["child.4h"],
        ),
        action="HOLD",
        trade_style="RANGE",
        child_refs=["child.4h"],
        relationship="4h结构作为主要限制",
    )
    decision = bind_decision(context, draft)
    assert decision.prediction.structure_id == box["id"]
    assert decision.prediction.timeframe == "1h"
    assert decision.prediction.horizon_rule_id != own.horizon_rule_id
    assert str(decision.prediction.evidence_ids[0]) == ctx["input_id"]
    await predictions.publish(parent, decision)


async def test_missing_one_hour_risk_basis_never_reaches_order_preparation(prediction_system):
    from test_prediction_execution import executor, fresh_market

    from trading_agent.execution.transport import PaperTransport

    class NoOrders(PaperTransport):
        async def build(self, *args, **kwargs):
            raise AssertionError("Missing risk data must not reach order preparation")

    system = prediction_system
    child_id = await publish_child(system, "4h")
    parent = await task(system, kind="PREDICT_PARENT")
    context = await system[3].context(parent)
    decision = ParentDecision(
        prediction=await report(system, parent),
        action="OPEN_LONG",
        trade_style="TREND",
        selected_direction="UP",
        child_prediction_ids=[child_id],
        context_revision=context["context_revision"],
        relationship="fixture no 1h risk input",
    )
    ident = await system[3].publish(parent, decision)
    await system[2].finish(parent, result_id=ident)
    await fresh_market(system)
    service = executor(system, NoOrders())
    execution = await system[2].claim(("PREDICT_EXECUTE",))
    with pytest.raises(DomainError) as error:
        await service.run(execution)
    assert error.value.code == "RISK_DATA_MISSING"
    async with system[0].engine.connect() as c:
        assert await c.scalar(sa.select(sa.func.count()).select_from(s.trading_positions)) == 0
