from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import bars_at
from test_prediction_knowledge import knowledge
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import task

from trading_agent.domain.errors import DomainError
from trading_agent.domain.time import TIMEFRAMES, boundary
from trading_agent.prediction.market import PredictionMarket
from trading_agent.storage import schema as s
from trading_agent.storage.market import persist_bar

pytestmark = pytest.mark.postgres


class ReviewMarket:
    def __init__(self):
        self.calls = []

    async def check_clock(self, deadline):
        pass

    async def instrument(self, deadline, *, inst_id):
        return {}

    async def candles(self, anchor, timeframe, deadline, *, inst_id):
        self.calls.append((inst_id, timeframe))
        return tuple(
            b.model_copy(update={"inst_id": inst_id, "id": uuid4()})
            for b in bars_at(anchor, timeframe)
        )


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT"])
async def test_review_320_prediction_120_and_immutable_retries(prediction_system, tmp_path, symbol):
    system = prediction_system
    service = knowledge(system, tmp_path)
    prediction_inputs = []
    for timeframe in TIMEFRAMES:
        attempt = await task(system, symbol=symbol, timeframe=timeframe)
        context = await system[3].context(attempt)
        assert len(context["model_bars"]) == 30
        assert len(context["bar_ids"]) == 80
        prediction_inputs.append((attempt, context))
    client = ReviewMarket()
    market = PredictionMarket(system[2], client, system[3], service, system[4])
    review = await task(system, symbol=symbol, kind="PREDICT_REVIEW", freeze=False)
    input_id = await market.prepare(review)
    context = await service.review_context(review)
    windows = context["review_market"]["bars_by_timeframe"]
    inst = symbol.removesuffix("USDT") + "-USDT-SWAP"
    assert context["review_market"]["total_bars"] == 320
    assert set(windows) == set(TIMEFRAMES)
    assert all(len(bars) == 80 for bars in windows.values())
    assert all(b["inst_id"] == inst for bars in windows.values() for b in bars)
    ids = [UUID(b["id"]) for bars in windows.values() for b in bars]
    async with system[0].engine.connect() as conn:
        assert (
            await conn.scalar(
                sa.select(sa.func.count())
                .select_from(s.market_bars)
                .where(s.market_bars.c.id.in_(ids))
            )
            == 320
        )
    # Source revisions after review freeze must not change either model's original input.
    revised = bars_at(boundary(system[1].value))[0].model_copy(
        update={"id": uuid4(), "inst_id": inst, "volume_contracts": Decimal(999)}
    )
    async with system[0].transaction() as conn:
        await persist_bar(conn, revised, symbol)
    system[1].value += timedelta(seconds=1)
    assert await market.prepare(review) == input_id
    assert len(client.calls) == 4  # Frozen retry does not refetch or trim the windows.
    assert await service.review_context(review) == context
    for attempt, original in prediction_inputs:
        assert await system[3].context(attempt) == original


@pytest.mark.parametrize(
    "fault,code",
    [
        ("missing_timeframe", "REVIEW_TIMEFRAMES"),
        ("short_window", "BAR_COUNT"),
        ("mixed_symbol", "REVIEW_MARKET_SCOPE"),
        ("gap", "BAR_GAP"),
        ("wrong_boundary", "BAR_BOUNDARY"),
        ("future_available", "FUTURE_DATA"),
        ("future_anchor", "FUTURE_DATA"),
    ],
)
async def test_invalid_review_data_never_freezes_or_saves_partial_windows(
    prediction_system, tmp_path, fault, code
):
    system = prediction_system
    service = knowledge(system, tmp_path)
    review = await task(system, kind="PREDICT_REVIEW", freeze=False)
    anchor = boundary(system[1].value)
    data = {tf: bars_at(anchor, tf) for tf in TIMEFRAMES}
    # Corrupt the last window: earlier valid windows must not be partially persisted.
    bars = data["12h"]
    if fault == "missing_timeframe":
        del data["12h"]
    elif fault == "short_window":
        data["12h"] = bars[-30:]
    elif fault == "mixed_symbol":
        data["12h"] = tuple(b.model_copy(update={"inst_id": "ETH-USDT-SWAP"}) for b in bars)
    elif fault == "gap":
        data["12h"] = bars[:1] + bars[:-1]
    elif fault == "wrong_boundary":
        data["12h"] = bars_at(anchor - timedelta(hours=12), "12h")
    elif fault == "future_available":
        data["12h"] = bars[:-1] + (
            bars[-1].model_copy(update={"available_at": system[1].value + timedelta(seconds=1)}),
        )
    else:
        anchor += timedelta(hours=1)
    with pytest.raises(DomainError) as caught:
        await service.freeze_review(review, system[4], data, anchor=anchor)
    assert caught.value.code == code
    async with system[0].engine.connect() as conn:
        for table in (s.market_bars, s.prediction_inputs):
            assert await conn.scalar(sa.select(sa.func.count()).select_from(table)) == 0
