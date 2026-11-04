from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import bars_at
from test_prediction_knowledge import knowledge, review_task
from test_prediction_pipeline import minute, publish_child, report, task
from test_prediction_pipeline import prediction_system as prediction_system

from trading_agent.domain.errors import DomainError
from trading_agent.knowledge.contracts import KnowledgeReview
from trading_agent.prediction.contracts import ParentDecision
from trading_agent.prediction.market import PredictionMarket
from trading_agent.runtime.lifecycle import Runtime
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


class MarketFixture:
    def __init__(self, system):
        self.system = system
        self.missing = set()
        self.invalid = False
        self.invalid_at = None
        self.candle_calls = []

    def now(self):
        return self.system[1].value

    async def minute_window(self, inst, start, end, deadline):
        values = []
        cursor = start
        while cursor < end:
            if cursor not in self.missing:
                bad = (
                    self.invalid and cursor + timedelta(minutes=1) == end
                ) or cursor == self.invalid_at
                values.append(
                    minute(
                        inst,
                        cursor,
                        high="100" if bad else "185",
                        low="90" if bad else "177",
                        close="95" if bad else "182",
                    )
                )
            cursor += timedelta(minutes=1)
        return tuple(values)

    async def candles(self, anchor, timeframe, deadline, *, inst_id):
        self.candle_calls.append((inst_id, timeframe))
        return tuple(
            b.model_copy(update={"inst_id": inst_id, "id": uuid4()})
            for b in bars_at(anchor, timeframe)
        )

    async def forming_candle(self, timeframe, deadline, *, inst_id):
        # Existing tests exercise confirmed-only fallback; snapshot coverage is separate.
        return None


async def verifier(system):
    return await task(system, kind="PREDICT_VERIFY", freeze=False)


@pytest.mark.parametrize("age_seconds", [41, 181])
async def test_retry_rejects_expired_frozen_input_before_market_or_model_io(
    prediction_system, tmp_path, age_seconds
):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    attempt = await task(system)
    context = await predictions.context(attempt)
    await jobs.finish(
        attempt, state="RETRY_WAIT", next_run_at=clock.value + timedelta(seconds=age_seconds)
    )
    clock.value += timedelta(seconds=age_seconds)
    retry = await jobs.claim(("PREDICT_TIMEFRAME",))
    assert retry.job_id == attempt.job_id and retry.id != attempt.id
    client = MarketFixture(system)
    market = PredictionMarket(jobs, client, predictions, knowledge(system, tmp_path), engine)
    with pytest.raises(DomainError) as caught:
        await market.prepare(retry)
    assert caught.value.code == "CONTEXT_EXPIRED"
    assert not client.candle_calls
    async with db.engine.connect() as connection:
        rows = (await connection.execute(sa.select(s.prediction_inputs.c.id))).scalars().all()
        assert list(map(str, rows)) == [context["input_id"]]


async def test_delayed_review_retry_preserves_its_historical_frozen_context(
    prediction_system, tmp_path
):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    reviews = knowledge(system, tmp_path)
    attempt = await review_task(system, reviews)
    original = await reviews.review_context(attempt)
    await jobs.finish(attempt, state="RETRY_WAIT", next_run_at=clock.value + timedelta(seconds=181))
    clock.value += timedelta(seconds=181)
    retry = await jobs.claim(("PREDICT_REVIEW",))
    client = MarketFixture(system)
    market = PredictionMarket(jobs, client, predictions, reviews, engine)
    assert str(await market.prepare(retry)) == original["input_id"]
    assert await reviews.review_context(retry) == original
    assert not client.candle_calls
    ident = await reviews.submit(
        retry, KnowledgeReview(narrative="fixture frozen review", changes=[])
    )
    async with db.engine.connect() as connection:
        assert await connection.scalar(sa.select(s.reviews.c.id)) == ident


async def test_durable_cursor_waits_for_gap_then_recovers(prediction_system, tmp_path):
    system = prediction_system
    await publish_child(system)
    client = MarketFixture(system)
    market = PredictionMarket(system[2], client, system[3], knowledge(system, tmp_path), system[4])
    start = system[1].value.replace(second=0)
    system[1].value += timedelta(minutes=3)
    client.missing.add(start + timedelta(minutes=1))
    attempt = await verifier(system)
    result = await market.verify(attempt)
    assert result["data_quality"] == "AWAITING_DATA"
    async with system[0].engine.connect() as conn:
        cursor = await conn.scalar(sa.select(s.prediction_streams.c.cursor))
        assert cursor == start + timedelta(minutes=1)
        assert await conn.scalar(sa.select(s.prediction_states.c.data_quality)) == "AWAITING_DATA"
    client.missing.clear()
    result = await market.verify(attempt)
    assert result["data_quality"] == "OK"
    async with system[0].engine.connect() as conn:
        assert await conn.scalar(sa.select(s.prediction_states.c.data_quality)) == "OK"
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.market_bars)
                .where(s.market_bars.c.interval == "1m")
            )
            == 3
        )


@pytest.mark.parametrize("request_during_gap", [False, True])
@pytest.mark.parametrize("with_position", [False, True])
async def test_runtime_retries_public_candle_gap_before_next_minute(
    prediction_system, tmp_path, request_during_gap, with_position
):
    system = prediction_system
    db, clock, jobs, service, engine = system
    child = await publish_child(system)
    if with_position:
        async with db.transaction() as connection:
            await connection.execute(
                sa.insert(s.trading_positions).values(
                    engine_id=engine,
                    symbol="BTCUSDT",
                    position_id=uuid4(),
                    status="OPEN",
                    body={
                        "contracts": "1",
                        "direction": "UP",
                        "structure_prediction_id": str(child),
                    },
                    updated_at=clock.value,
                )
            )

    async def publish_parent(parent=None):
        parent = parent or await jobs.claim(("PREDICT_PARENT",))
        assert parent is not None
        anchor = clock.value.replace(minute=0, second=0, microsecond=0)
        await service.freeze(parent, engine, bars_at(anchor))
        context = await service.context(parent)
        ident = await service.publish(
            parent,
            ParentDecision(
                prediction=await report(system, parent),
                action="HOLD",
                trade_style="TREND",
                child_prediction_ids=[c["id"] for c in context["children"]],
                context_revision=context["context_revision"],
                position_id=context["position"]["position_id"] if context["position"] else None,
                relationship="fixture context recovery",
            ),
        )
        await jobs.finish(parent, result_id=ident)

    await publish_parent()
    client = MarketFixture(system)
    missing = clock.value.replace(second=0) + timedelta(minutes=1)
    clock.value += timedelta(minutes=3)
    client.missing.add(missing)
    market = PredictionMarket(jobs, client, service, knowledge(system, tmp_path), engine)
    monitor = AsyncMock()
    runtime = SimpleNamespace(
        market=market, execution=SimpleNamespace(monitor=monitor), jobs=jobs, database=db
    )
    attempt = await verifier(system)
    await Runtime._execute(runtime, attempt)
    async with db.engine.connect() as connection:
        row = (
            (await connection.execute(sa.select(s.jobs).where(s.jobs.c.id == attempt.job_id)))
            .mappings()
            .one()
        )
        assert row["state"] == "AWAITING_DATA"
        assert row["next_run_at"] == clock.value + timedelta(seconds=5)
        assert (
            await connection.scalar(sa.select(s.prediction_states.c.data_quality))
            == "AWAITING_DATA"
        )
    assert await jobs.claim(("PREDICT_VERIFY",)) is None
    async with db.engine.connect() as connection:
        assert not await connection.scalar(
            sa.select(s.jobs.c.id).where(
                s.jobs.c.kind == "PREDICT_PARENT", s.jobs.c.state == "QUEUED"
            )
        )
    if request_during_gap:
        async with db.transaction() as connection:
            await jobs.create(
                connection,
                "PREDICT_PARENT",
                str(uuid4()),
                {"engine_id": str(engine), "symbol": "BTCUSDT", "timeframe": "1h"},
                clock.value,
            )
        waiting_parent = await jobs.claim(("PREDICT_PARENT",))
        with pytest.raises(DomainError) as blocked:
            await market.prepare(waiting_parent)
        assert blocked.value.code == "MINUTE_DATA_PENDING"
        # No empty synthesis replaces the valid parent while every child awaits data.
        with pytest.raises(DomainError) as empty:
            await service.freeze_parent(waiting_parent, engine)
        assert empty.value.code == "MISSING_CONTEXT"
        await jobs.finish(
            waiting_parent,
            state="AWAITING_DATA",
            error={"code": "MINUTE_DATA_PENDING"},
            next_run_at=clock.value + timedelta(seconds=5),
        )
    client.missing.clear()
    clock.value += timedelta(seconds=5)
    retry = await jobs.claim(("PREDICT_VERIFY",))
    assert retry.job_id == attempt.job_id and retry.id != attempt.id
    await Runtime._execute(runtime, retry)
    async with db.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(s.jobs.c.state).where(s.jobs.c.id == attempt.job_id))
            == "SUCCEEDED"
        )
        assert await connection.scalar(sa.select(s.prediction_states.c.data_quality)) == "OK"
        pending_parent = await connection.scalar(
            sa.select(s.jobs.c.id).where(
                s.jobs.c.kind == "PREDICT_PARENT", s.jobs.c.state == "QUEUED"
            )
        )
        assert not pending_parent
    if request_during_gap:
        recovered = await jobs.claim(("PREDICT_PARENT",))
        assert recovered.job_id == waiting_parent.job_id
        await market.prepare(recovered)
        await publish_parent(recovered)
    assert monitor.await_count == 2


async def test_expiry_holds_but_original_structure_still_protects_position(
    prediction_system, tmp_path
):
    system = prediction_system
    child = await publish_child(system)
    parent = await task(system, kind="PREDICT_PARENT")
    context = await system[3].context(parent)
    decision = ParentDecision(
        prediction=await report(system, parent),
        action="OPEN_LONG",
        trade_style="TREND",
        selected_direction="UP",
        child_prediction_ids=[child],
        context_revision=context["context_revision"],
        relationship="趋势延续",
    )
    ident = await system[3].publish(parent, decision)
    await system[2].finish(parent, result_id=ident)
    async with system[0].transaction() as conn:
        await conn.execute(
            sa.insert(s.trading_positions).values(
                engine_id=system[4],
                symbol="BTCUSDT",
                position_id=uuid4(),
                status="OPEN",
                body={"direction": "UP", "structure_prediction_id": str(ident)},
            )
        )
    client = MarketFixture(system)
    market = PredictionMarket(system[2], client, system[3], knowledge(system, tmp_path), system[4])
    system[1].value += timedelta(hours=4)
    first = await verifier(system)
    await market.verify(first)
    async with system[0].engine.connect() as conn:
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.trade_intents)
                .where(s.trade_intents.c.action == "CLOSE")
            )
            == 0
        )
        assert await conn.scalar(sa.select(s.trading_positions.c.status)) == "OPEN"
        state = await conn.scalar(
            sa.select(s.prediction_states.c.body).where(
                s.prediction_states.c.prediction_id == ident
            )
        )
        assert state["lifecycle"] == "EXPIRED"
    await system[2].finish(first)
    client.invalid_at = system[1].value.replace(second=0, microsecond=0) + timedelta(minutes=1)
    system[1].value += timedelta(minutes=4)
    # The boundary was breached in a missed minute; latest minute has already recovered.
    later = await verifier(system)
    await market.verify(later)
    await market.verify(later)
    async with system[0].engine.connect() as conn:
        closes = (
            (
                await conn.execute(
                    sa.select(s.trade_intents).where(s.trade_intents.c.action == "CLOSE")
                )
            )
            .mappings()
            .all()
        )
        assert len(closes) == 1 and closes[0]["position_id"] is not None
        assert closes[0]["body"]["details"]["after_prediction_expiry"] is True
        assert closes[0]["position_version"] == 1
