from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa
from conftest import bars_at
from test_prediction_market import MarketFixture
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import task

from trading_agent.config import MarketConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.domain.time import boundary
from trading_agent.market.features import compute_features
from trading_agent.market.models import Bar, FormingCandle
from trading_agent.market.okx import OKXClient
from trading_agent.prediction.market import PredictionMarket
from trading_agent.prediction.submissions import model_context
from trading_agent.storage import schema as s
from trading_agent.storage.market import persist_bar

pytestmark = pytest.mark.postgres


class CurrentMarket(MarketFixture):
    def __init__(self, system):
        super().__init__(system)
        self.price = "130"
        self.failure = None
        self.calls = 0
        # RuntimeOwnership holds a session advisory-lock connection for the fixture.
        self.baseline_checkouts = system[0].engine.pool.checkedout()
        self.http = OKXClient(
            MarketConfig(), transport=httpx.MockTransport(self.respond), now=self.now
        )

    def respond(self, request):
        # The adapter's external I/O must happen outside the guarded DB transaction.
        assert self.system[0].engine.pool.checkedout() == self.baseline_checkouts
        self.calls += 1
        if self.failure == "empty":
            return httpx.Response(200, json={"code": "0", "data": []})
        if self.failure == "rejected":
            return httpx.Response(403, json={})
        timeframe = {"1H": "1h", "4H": "4h", "6Hutc": "6h", "12Hutc": "12h"}[
            request.url.params["bar"]
        ]
        ts = str(int(boundary(self.now(), timeframe).timestamp() * 1000))
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [[ts, "179", "183", "129", self.price, "10", "0.1", "15", "0"]],
            },
        )

    async def check_clock(self, deadline):
        pass

    async def instrument(self, deadline, **scope):
        return {}

    async def observations(self, deadline, **scope):
        return ()

    async def forming_candle(self, timeframe, deadline, *, inst_id):
        return await self.http.forming_candle(timeframe, deadline, inst_id=inst_id)


async def test_all_scopes_freeze_80_closed_plus_snapshot_and_26_tagged_values(prediction_system):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    clock.value = datetime(2026, 10, 30, 7, 23, tzinfo=UTC)
    source = CurrentMarket(system)
    collector = PredictionMarket(
        jobs, source, predictions, SimpleNamespace(search=AsyncMock()), engine
    )
    try:
        for symbol in ("BTCUSDT", "ETHUSDT"):
            for timeframe, kind in [(tf, "PREDICT_TIMEFRAME") for tf in ("1h", "4h", "6h", "12h")]:
                attempt = await task(
                    system, symbol=symbol, timeframe=timeframe, kind=kind, freeze=False
                )
                ident = await collector.prepare(attempt)
                context = await predictions.context(attempt)
                view = model_context(context)
                assert context["schema_version"] == 3
                assert context["candle_context_version"] == 1
                assert len(context["bar_ids"]) == 80 and len(view["model_bars"]) == 30
                assert (
                    view["forming_candle"]["inst_id"] == symbol.removesuffix("USDT") + "-USDT-SWAP"
                )
                assert view["forming_candle"]["timeframe"] == timeframe
                assert view["forming_candle"]["status"] == "PROVISIONAL"
                assert view["forming_candle"]["close"] == "130"
                assert view["reference_price"] == "179"  # Confirmed reference is not relabeled.
                assert len(view["provisional_features"]) == 26
                assert all(v["status"] == "PROVISIONAL" for v in view["provisional_features"])
                assert view["provisional_feature_basis"]["closed_bars"] == 80
                assert view["provisional_feature_basis"]["forming_bars"] == 1
                assert "market.current" in view["evidence_options"]
                assert context["forming_candle_quality"] == {
                    "quality": "OK",
                    "missing_reason": None,
                }
                bars = tuple(
                    b.model_copy(update={"inst_id": context["inst_id"]})
                    for b in bars_at(boundary(clock.value), timeframe)
                )
                assert context["features"] == json_value(
                    compute_features(
                        bars,
                        predictions.config.features,
                        decision_at=clock.value,
                        benchmark_bars=bars_at(boundary(clock.value), timeframe)
                        if symbol == "ETHUSDT"
                        else (),
                    )
                )
                calls = source.calls
                source.price = "180"
                assert await collector.prepare(attempt) == ident
                assert await predictions.context(attempt) == context
                assert source.calls == calls  # Retry uses the frozen observation, no repaint.
                source.price = "130"
                async with db.engine.connect() as connection:
                    row = (
                        (
                            await connection.execute(
                                sa.select(s.prediction_inputs).where(
                                    s.prediction_inputs.c.id == ident
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    assert row["content_hash"] == content_hash(row["body"])
                    assert not await connection.scalar(
                        sa.select(sa.func.count())
                        .select_from(s.market_bars)
                        .where(s.market_bars.c.close_boundary > clock.value)
                    )
                await jobs.finish(attempt)
    finally:
        await source.http.close()


@pytest.mark.parametrize(
    "failure,reason", [("empty", "CURRENT_CANDLE_NOT_RETURNED"), ("rejected", "MARKET_REJECTED")]
)
async def test_optional_failure_is_visible_without_fabricating_values(
    prediction_system, failure, reason
):
    system = prediction_system
    source = CurrentMarket(system)
    source.failure = failure
    collector = PredictionMarket(
        system[2], source, system[3], SimpleNamespace(search=AsyncMock()), system[4]
    )
    try:
        attempt = await task(system, timeframe="4h", freeze=False)
        await collector.prepare(attempt)
        context = await system[3].context(attempt)
        view = model_context(context)
        assert view["forming_candle"] is None and view["provisional_features"] == []
        assert view["forming_candle_quality"] == {"quality": "MISSING", "missing_reason": reason}
        assert len(view["features"]) == 37
        # Old frozen inputs remain readable, without inventing historical snapshots.
        old = {k: v for k, v in context.items() if not k.startswith(("forming_", "provisional_"))}
        old["schema_version"] = 3
        old_view = model_context(old)
        assert old_view["forming_candle"] is None
        assert old_view["forming_candle_quality"]["missing_reason"] == "LEGACY_INPUT"
    finally:
        await source.http.close()


@pytest.mark.parametrize("fault", ["symbol", "timeframe", "future", "stale", "period"])
async def test_mismatched_or_future_snapshot_cannot_freeze_partial_facts(prediction_system, fault):
    db, clock, _, predictions, engine = prediction_system
    clock.value = datetime(2026, 10, 30, 7, 23, tzinfo=UTC)
    attempt = await task(prediction_system, timeframe="4h", freeze=False)
    start = boundary(clock.value, "4h")
    values = dict(
        inst_id="BTC-USDT-SWAP",
        timeframe="4h",
        open_at=start,
        close_boundary=start + timedelta(hours=4),
        open=179,
        high=183,
        low=129,
        close=130,
        volume_contracts=10,
        volume_base=".1",
        turnover_quote=15,
        requested_at=clock.value,
        available_at=clock.value,
    )
    if fault == "symbol":
        values["inst_id"] = "ETH-USDT-SWAP"
    elif fault == "timeframe":
        values.update(
            timeframe="1h",
            open_at=boundary(clock.value),
            close_boundary=boundary(clock.value) + timedelta(hours=1),
        )
    elif fault == "future":
        values["available_at"] += timedelta(seconds=1)
    elif fault == "stale":
        values["requested_at"] -= timedelta(seconds=211)
    else:
        values = {
            k: (
                v - timedelta(hours=4)
                if k in {"open_at", "close_boundary", "requested_at", "available_at"}
                else v
            )
            for k, v in values.items()
        }
    forming = FormingCandle(**values)
    with pytest.raises(DomainError) as caught:
        await predictions.freeze(
            attempt, engine, bars_at(boundary(clock.value), "4h"), forming_candle=forming
        )
    assert caught.value.code == "FORMING_CANDLE_SCOPE"
    async with db.engine.connect() as connection:
        for table in (s.prediction_inputs, s.market_bars):
            assert await connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0


async def test_later_snapshot_and_final_candle_never_replace_original_input(prediction_system):
    system = prediction_system
    db, clock, jobs, predictions, engine = system
    source = CurrentMarket(system)
    collector = PredictionMarket(
        jobs, source, predictions, SimpleNamespace(search=AsyncMock()), engine
    )
    try:
        first = await task(system, timeframe="4h", freeze=False)
        first_id = await collector.prepare(first)
        original = await predictions.context(first)
        await jobs.finish(first)
        clock.value += timedelta(seconds=1)
        source.price = "180"
        second = await task(system, timeframe="4h", freeze=False)
        await collector.prepare(second)
        newer = await predictions.context(second)
        assert newer["forming_candle"]["close"] == "180"
        assert newer["provisional_features"] != original["provisional_features"]
        final = dict(newer["forming_candle"])
        for key in ("status", "confirm", "requested_at", "elapsed_fraction_at_receipt"):
            final.pop(key)
        final["available_at"] = final["close_boundary"]
        final["close"] = "181"
        async with db.transaction() as connection:
            await persist_bar(connection, Bar.model_validate(final))
        async with db.engine.connect() as connection:
            saved = (
                (
                    await connection.execute(
                        sa.select(s.prediction_inputs).where(s.prediction_inputs.c.id == first_id)
                    )
                )
                .mappings()
                .one()
            )
            assert saved["body"]["forming_candle"] == original["forming_candle"]
            assert saved["body"]["provisional_features"] == original["provisional_features"]
            assert saved["content_hash"] == content_hash(saved["body"])
    finally:
        await source.http.close()
