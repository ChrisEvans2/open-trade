"""Independent PostgreSQL checks for scoped sampled liquidations and frozen benchmarks."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import bars_at
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import task

from trading_agent.config import MarketConfig
from trading_agent.domain.errors import DomainError, StaleAttempt
from trading_agent.domain.serialization import content_hash
from trading_agent.market.liquidations import RAW_PREFIX, STREAM_SOURCE, LiquidationSampler
from trading_agent.prediction.submissions import model_context
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


def metadata(symbol):
    return dict(
        instId=f"{symbol}-USDT-SWAP",
        instType="SWAP",
        instFamily=f"{symbol}-USDT",
        settleCcy="USDT",
        state="live",
        ctType="linear",
        ctVal="0.1" if symbol == "ETH" else "0.01",
        ctMult="1",
        ctValCcy=symbol,
    )


def event(symbol, at, side, quantity, price):
    return {
        "instId": f"{symbol}-USDT-SWAP",
        "instType": "SWAP",
        "details": [
            dict(
                side=side,
                posSide="long" if side == "sell" else "short",
                sz=quantity,
                bkPx=price,
                bkLoss="0",
                ccy="",
                ts=str(int(at.timestamp() * 1000)),
            )
        ],
    }


def sample(system):
    db, _, jobs, _, _ = system
    return LiquidationSampler(
        db,
        jobs.ownership,
        MarketConfig(inst_id="BTC-USDT-SWAP", contract_type="perpetual", settlement_asset="USDT"),
        inst_ids=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
    )


async def test_one_sampler_preserves_two_assets_and_formal_freeze_uses_only_eth_window(
    prediction_system,
):
    db, clock, jobs, service, engine = prediction_system
    anchor = clock.value.replace(minute=0, second=0, microsecond=0)
    decision_at = clock.value
    sampler = sample(prediction_system)
    clock.value = anchor - timedelta(hours=1)
    await sampler._marker("OPEN")
    clock.value = decision_at
    await sampler._marker("COVERAGE")
    await sampler.record_frame(
        {
            "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
            "data": [
                event("BTC", anchor - timedelta(minutes=10), "sell", "900", "60000"),
                event("ETH", anchor - timedelta(minutes=10), "sell", "2", "3000"),
                event("ETH", anchor - timedelta(minutes=11), "buy", "3", "4000"),
            ],
        }
    )
    attempt = await task(prediction_system, symbol="ETHUSDT", freeze=False)
    eth = tuple(
        b.model_copy(update={"id": uuid4(), "inst_id": "ETH-USDT-SWAP"}) for b in bars_at(anchor)
    )
    btc = bars_at(anchor)
    ident = await service.freeze(
        attempt,
        engine,
        eth,
        benchmark_bars=btc,
        liquidation_sampler=sampler,
        instrument_metadata=metadata("ETH"),
    )
    context = await service.context(attempt)
    liquidation = next(f for f in context["features"] if f["name"] == "liquidation_imbalance")
    assert Decimal(str(liquidation["value"])) == pytest.approx(Decimal(1) / 3)
    assert liquidation["quality"] == "PARTIAL_COVERAGE"
    assert context["btc_benchmark"]["quality"] == "OK"
    assert len(context["btc_benchmark"]["bar_ids"]) == 80
    assert len(context["btc_benchmark"]["calculation_bars"]) == 21
    model = model_context(context)
    assert model["btc_benchmark"]["calculation_bars_count"] == 21
    assert "calculation_bars" not in model["btc_benchmark"]
    assert "bar_ids" not in model["btc_benchmark"]
    assert (
        next(f for f in model["features"] if f["name"] == "btc_correlation")["evidence_strength"]
        == "WEAK"
    )
    async with db.engine.connect() as connection:
        rows = (await connection.execute(sa.select(s.market_observations))).mappings().all()
        raw = [r for r in rows if r["source"].startswith(RAW_PREFIX)]
        assert len(raw) == 3
        assert {r["inst_id"] for r in rows if r["source"] == STREAM_SOURCE} == {
            "BTC-USDT-SWAP",
            "ETH-USDT-SWAP",
        }
        windows = [r for r in rows if r["payload"].get("record") == "WINDOW"]
        assert len(windows) == 1 and windows[0]["inst_id"] == "ETH-USDT-SWAP"
        assert Decimal(windows[0]["payload"]["long_quote"]) == 600
        assert Decimal(windows[0]["payload"]["short_quote"]) == 1200
        assert str(windows[0]["id"]) in context["observation_ids"]
        input_row = (
            (
                await connection.execute(
                    sa.select(s.prediction_inputs).where(s.prediction_inputs.c.id == ident)
                )
            )
            .mappings()
            .one()
        )
        original_hash = input_row["content_hash"]
        assert original_hash == content_hash(input_row["body"])
        assert await connection.scalar(sa.select(sa.func.count()).select_from(s.market_bars)) == 160
    # A late sampled event must never mutate an already frozen input or its feature.
    clock.value += timedelta(seconds=1)
    await sampler.record_frame(
        {
            "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
            "data": [event("ETH", anchor - timedelta(minutes=9), "sell", "100", "3000")],
        }
    )
    assert (
        await service.freeze(
            attempt,
            engine,
            eth,
            benchmark_bars=btc,
            liquidation_sampler=sampler,
            instrument_metadata=metadata("ETH"),
        )
        == ident
    )
    async with db.engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(s.prediction_inputs.c.content_hash).where(
                    s.prediction_inputs.c.id == ident
                )
            )
            == original_hash
        )


@pytest.mark.parametrize("defect", ["foreign", "future"])
async def test_invalid_benchmark_rolls_back_entire_frozen_input(prediction_system, defect):
    db, clock, _, service, engine = prediction_system
    anchor = clock.value.replace(minute=0, second=0, microsecond=0)
    attempt = await task(prediction_system, symbol="ETHUSDT", freeze=False)
    eth = tuple(
        b.model_copy(update={"id": uuid4(), "inst_id": "ETH-USDT-SWAP"}) for b in bars_at(anchor)
    )
    update = (
        {"inst_id": "SOL-USDT-SWAP"}
        if defect == "foreign"
        else {"available_at": clock.value + timedelta(seconds=1)}
    )
    benchmark = tuple(b.model_copy(update=update) for b in bars_at(anchor))
    with pytest.raises(DomainError):
        await service.freeze(
            attempt,
            engine,
            eth,
            benchmark_bars=benchmark,
            liquidation_sampler=sample(prediction_system),
            instrument_metadata=metadata("ETH"),
        )
    async with db.engine.connect() as connection:
        for table in (s.prediction_inputs, s.market_bars, s.market_observations):
            assert await connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0


async def test_expired_attempt_cannot_write_benchmark_or_liquidation_window(prediction_system):
    db, clock, jobs, service, engine = prediction_system
    anchor = clock.value.replace(minute=0, second=0, microsecond=0)
    attempt = await task(prediction_system, symbol="ETHUSDT", freeze=False)
    await jobs.finish(attempt, state="SUCCEEDED")
    eth = tuple(
        b.model_copy(update={"id": uuid4(), "inst_id": "ETH-USDT-SWAP"}) for b in bars_at(anchor)
    )
    with pytest.raises(StaleAttempt):
        await service.freeze(
            attempt,
            engine,
            eth,
            benchmark_bars=bars_at(anchor),
            liquidation_sampler=sample(prediction_system),
            instrument_metadata=metadata("ETH"),
        )
    async with db.engine.connect() as connection:
        for table in (s.prediction_inputs, s.market_bars, s.market_observations):
            assert await connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0
