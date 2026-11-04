from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from conftest import bars_at

from trading_agent.config import AppConfig, HermesConfig
from trading_agent.domain.errors import DomainError
from trading_agent.jobs.service import JobService
from trading_agent.market.models import Bar
from trading_agent.prediction.contracts import ParentDecision, PredictionReport
from trading_agent.prediction.service import PredictionService
from trading_agent.runtime.ownership import RuntimeOwnership
from trading_agent.storage import schema as s
from trading_agent.storage.database import Database

pytestmark = pytest.mark.postgres


@dataclass
class Clock:
    value: datetime

    def __call__(self):
        return self.value


@pytest_asyncio.fixture
async def prediction_system(postgres_url):
    clock = Clock(datetime(2026, 10, 7, 8, 1, 5, tzinfo=UTC))
    db = Database(postgres_url, clock=clock)
    owner = RuntimeOwnership(db)
    await owner.acquire()
    jobs = JobService(db, owner)
    config = AppConfig(hermes=HermesConfig(provider="fixture", model="fixture"))
    service = PredictionService(jobs, config)
    engine = await service.register_engine(
        {k: "fixture" for k in ("parent", "1h", "4h", "6h", "12h", "review")}
    )
    try:
        yield db, clock, jobs, service, engine
    finally:
        await owner.release()
        await db.close()


async def task(system, *, symbol="BTCUSDT", timeframe="1h", kind="PREDICT_TIMEFRAME", freeze=True):
    db, clock, jobs, service, engine = system
    async with db.transaction() as conn:
        await jobs.create(
            conn,
            kind,
            str(uuid4()),
            {"engine_id": str(engine), "symbol": symbol, "timeframe": timeframe},
            clock.value,
        )
    attempt = await jobs.claim((kind,))
    assert attempt.payload["symbol"] == symbol and attempt.payload["timeframe"] == timeframe
    if freeze:
        inst = symbol.removesuffix("USDT") + "-USDT-SWAP"
        anchor = clock.value.replace(minute=0, second=0, microsecond=0)
        bars = tuple(
            b.model_copy(update={"id": uuid4(), "inst_id": inst})
            for b in bars_at(anchor, timeframe)
        )
        await service.freeze(attempt, engine, bars)
    return attempt


async def report(system, attempt):
    context = await system[3].context(attempt)
    rule = next(r for r in context["horizon_rules"] if r["structure"] == "UNKNOWN")
    # Preserve Decimal semantics of the production boundary validator.
    from decimal import Decimal

    if context.get("input_kind") == "CHILD_SYNTHESIS":
        options = context["boundary_options"]
        trigger = next(o for o in options if o["behavior"] == "CLOSE_ABOVE")
        invalidation = next(o for o in options if o["behavior"] == "CLOSE_BELOW")
    else:
        tolerance = (
            Decimal(next(f["value"] for f in context["features"] if f["name"] == "atr_pct"))
            * Decimal(context["reference_price"])
            * Decimal(context["tolerance_atr"])
        )
        assert Decimal(context["fallback_tolerance"]) == tolerance
        lines = context["fallback_lines"]
        trigger = dict(behavior="CLOSE_ABOVE", line=lines[1], tolerance=tolerance)
        invalidation = dict(behavior="CLOSE_BELOW", line=lines[0], tolerance=tolerance)
    trigger = {k: v for k, v in trigger.items() if k != "source_child_ref"}
    invalidation = {k: v for k, v in invalidation.items() if k != "source_child_ref"}
    return PredictionReport(
        symbol=context["symbol"],
        timeframe=context["timeframe"],
        structure="UNKNOWN",
        stage="UNKNOWN",
        horizon_rule_id=rule["id"],
        horizon_bars=rule["bars"],
        primary_direction="UP",
        p_up="0.7",
        p_down=0,
        p_none="0.3",
        scenarios=[
            dict(
                direction="UP",
                trigger=trigger,
                invalidation=invalidation,
                target=dict(low="200", high="210"),
                target_probability="0.6",
            )
        ],
        reason="测试多根K线趋势，不是下一根K线",
        evidence_ids=[context["input_id"]],
    )


async def publish_child(system, timeframe="1h", symbol="BTCUSDT"):
    attempt = await task(system, timeframe=timeframe, symbol=symbol)
    ident = await system[3].publish(attempt, await report(system, attempt))
    await system[2].finish(attempt, result_id=ident)
    return ident


@pytest.mark.parametrize("direction", ["UP", "DOWN"])
async def test_target_on_wrong_side_is_domain_rejection_and_can_be_corrected(
    prediction_system, direction
):
    system = prediction_system
    db, _, _, service, _ = system
    attempt = await task(system)
    valid = await report(system, attempt)
    payload = valid.model_dump(mode="python")
    scenario = payload["scenarios"][0]
    if direction == "UP":
        scenario["target"] = {"low": "150", "high": "160"}
    else:
        context = await service.context(attempt)
        payload.update(primary_direction="DOWN", p_up=0, p_down="0.7")
        scenario["direction"] = "DOWN"
        scenario["trigger"].update(behavior="CLOSE_BELOW", line=context["fallback_lines"][0])
        scenario["invalidation"].update(behavior="CLOSE_ABOVE", line=context["fallback_lines"][-1])
        scenario["target"] = {"low": "100", "high": "110"}
    with pytest.raises(DomainError) as caught:
        await service.publish(attempt, PredictionReport.model_validate(payload))
    assert caught.value.code == "PREDICTION_GEOMETRY" and caught.value.status == 422
    async with db.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(s.prediction_facts)) == 0
        )
    corrected = await service.publish(attempt, valid)
    async with db.engine.connect() as connection:
        assert await connection.scalar(sa.select(s.prediction_facts.c.id)) == corrected


def minute(inst, start, high="185", low="177", close="182"):
    return Bar(
        inst_id=inst,
        timeframe="1m",
        open_at=start,
        close_boundary=start + timedelta(minutes=1),
        available_at=start + timedelta(minutes=1),
        open=close,
        high=high,
        low=low,
        close=close,
        volume_contracts=1,
        volume_base=1,
        turnover_quote=100,
    )


async def test_frozen_scopes_independent_reuse_and_parent_not_copy_one_hour(prediction_system):
    system = prediction_system
    first = await publish_child(system)
    second = await publish_child(system, "12h")
    eth = await publish_child(system, symbol="ETHUSDT")
    attempt = await task(system, kind="PREDICT_PARENT")
    context = await system[3].context(attempt)
    assert {c["id"] for c in context["children"]} == {str(first), str(second)}
    assert str(eth) not in str(context)
    assert context["missing_timeframes"] == ["4h", "6h"]
    own_report = (await report(system, attempt)).model_copy(update={"p_up": 0.4, "p_none": 0.6})
    decision = ParentDecision(
        prediction=own_report,
        action="OPEN_LONG",
        trade_style="TREND",
        selected_direction="UP",
        child_prediction_ids=[first, second],
        context_revision=context["context_revision"],
        relationship="12h趋势中的1h回踩",
    )
    parent = await system[3].publish(attempt, decision)
    async with system[0].engine.connect() as conn:
        intent = (await conn.execute(sa.select(s.trade_intents))).mappings().one()
        assert intent["action"] == "OPEN_LONG" and intent["prediction_id"] == parent
        facts = (await conn.execute(sa.select(s.prediction_facts))).mappings().all()
        assert len(facts) == 4
        parent_fact = next(f for f in facts if f["id"] == parent)
        assert parent_fact["expires_at"] - parent_fact["published_at"] == timedelta(hours=4)
        assert parent_fact["body"]["prediction"]["report"]["p_up"] == "0.4"


async def test_reject_stale_parent_unknown_references_and_cross_symbol(prediction_system):
    system = prediction_system
    child = await publish_child(system)
    attempt = await task(system, kind="PREDICT_PARENT")
    context = await system[3].context(attempt)
    parent_report = await report(system, attempt)
    decision = ParentDecision(
        prediction=parent_report,
        action="HOLD",
        trade_style="TREND",
        child_prediction_ids=[child],
        context_revision=context["context_revision"],
        relationship="等待确认",
    )
    await publish_child(system, "4h")
    with pytest.raises(DomainError, match="子预测或持仓"):
        await system[3].publish(attempt, decision)
    child_attempt = await task(system)
    own = await report(system, child_attempt)
    with pytest.raises(DomainError, match="币种周期"):
        await system[3].publish(child_attempt, own.model_copy(update={"symbol": "ETHUSDT"}))
    with pytest.raises(DomainError, match="知识引用"):
        await system[3].publish(
            child_attempt, own.model_copy(update={"knowledge_version_ids": (uuid4(),)})
        )
    altered = own.scenarios[0].trigger.model_copy(update={"tolerance": 0})
    with pytest.raises(DomainError, match="边界和容差"):
        await system[3].publish(
            child_attempt,
            own.model_copy(
                update={"scenarios": (own.scenarios[0].model_copy(update={"trigger": altered}),)}
            ),
        )


async def test_immutable_facts_retry_and_expired_attempt(prediction_system):
    system = prediction_system
    attempt = await task(system)
    own = await report(system, attempt)
    first = await system[3].publish(attempt, own)
    assert first == await system[3].publish(attempt, own)
    with pytest.raises(DomainError, match="已发布内容"):
        await system[3].publish(attempt, own.model_copy(update={"reason": "改写"}))
    with pytest.raises(sa.exc.DBAPIError):
        async with system[0].transaction() as conn:
            await conn.execute(sa.update(s.prediction_facts).values(body={}))
    system[1].value += timedelta(seconds=61)
    with pytest.raises(DomainError, match="资格已失效"):
        await system[3].publish(attempt, own)


async def test_minute_gaps_invalidation_history_and_no_child_direct_close(prediction_system):
    system = prediction_system
    ident = await publish_child(system)
    attempt = await task(system, kind="PREDICT_VERIFY", freeze=False)
    system[1].value = system[1].value.replace(minute=4, second=0)
    # Renew fixture lease in the same supported API before advancing further.
    async with system[0].transaction() as conn:
        await conn.execute(
            sa.update(s.jobs)
            .where(s.jobs.c.id == attempt.job_id)
            .values(lease_until=system[1].value + timedelta(seconds=60))
        )
        await conn.execute(
            sa.update(s.job_attempts)
            .where(s.job_attempts.c.id == attempt.id)
            .values(token_expires_at=system[1].value + timedelta(seconds=120))
        )
    start = system[1].value.replace(minute=2)
    gap = minute("BTC-USDT-SWAP", start + timedelta(minutes=1))
    result = await system[3].verify(attempt, ident, (gap,))
    assert result.data_quality == "AWAITING_DATA"
    invalid = minute("BTC-USDT-SWAP", start, high="100", low="90", close="95")
    result = await system[3].verify(attempt, ident, (invalid, gap))
    assert result.data_quality == "OK" and result.state.lifecycle == "INVALIDATED"
    assert result.state.first_direction == "UP"
    await system[3].verify(attempt, ident, (invalid, gap))
    async with system[0].engine.connect() as conn:
        assert await conn.scalar(sa.select(sa.func.count()).select_from(s.trade_intents)) == 0
        assert (
            await conn.scalar(sa.select(sa.func.count()).select_from(s.prediction_evaluations)) == 2
        )
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.jobs)
                .where(s.jobs.c.kind == "PREDICT_TIMEFRAME")
            )
            == 2
        )
