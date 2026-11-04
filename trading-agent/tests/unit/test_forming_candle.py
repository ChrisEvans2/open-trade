from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from conftest import bars_at
from pydantic import ValidationError
from trading_quant import QuantConfig
from trading_quant import compute_features as calculate
from trading_quant.spec import CORE_FEATURES

from trading_agent.config import FeatureConfig, MarketConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.time import INTERVAL_SECONDS, OKX_BARS, boundary
from trading_agent.market.features import compute_features, compute_provisional_features
from trading_agent.market.models import Bar, FormingCandle
from trading_agent.market.okx import OKXClient

NOW = datetime(2026, 10, 30, 7, 23, tzinfo=UTC)  # 15:23 Beijing


def snapshot(timeframe="4h", **changes):
    start = boundary(NOW, timeframe)
    return FormingCandle(
        **{
            "inst_id": "BTC-USDT-SWAP",
            "timeframe": timeframe,
            "open_at": start,
            "close_boundary": start + timedelta(seconds=INTERVAL_SECONDS[timeframe]),
            "open": "179",
            "high": "183",
            "low": "129",
            "close": "130",
            "volume_contracts": "10",
            "volume_base": "0.1",
            "turnover_quote": "15",
            "requested_at": NOW - timedelta(seconds=1),
            "available_at": NOW,
            **changes,
        }
    )


def raw_row(timeframe="4h", confirm="0"):
    return [
        str(int(boundary(NOW, timeframe).timestamp() * 1000)),
        "179",
        "183",
        "129",
        "130",
        "10",
        "0.1",
        "15",
        confirm,
    ]


@pytest.mark.parametrize("inst_id", ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
@pytest.mark.parametrize("timeframe", ["1h", "4h", "6h", "12h"])
async def test_current_candle_uses_public_current_endpoint_and_exact_scope(inst_id, timeframe):
    seen = []

    def respond(request):
        seen.append(request)
        assert request.url.path == "/api/v5/market/candles"
        assert dict(request.url.params) == {
            "instId": inst_id,
            "bar": OKX_BARS[timeframe],
            "limit": "2",
        }
        assert not any(k.startswith("ok-access-") for k in request.headers)
        return httpx.Response(200, json={"code": "0", "data": [raw_row(timeframe)]})

    client = OKXClient(MarketConfig(), transport=httpx.MockTransport(respond), now=lambda: NOW)
    try:
        value = await client.forming_candle(timeframe, NOW + timedelta(seconds=30), inst_id=inst_id)
        assert len(seen) == 1
        assert value.inst_id == inst_id and value.timeframe == timeframe
        assert value.status == "PROVISIONAL" and value.confirm == "0"
        assert value.close == 130 and value.volume_base == Decimal("0.1")
        assert value.open_at <= value.requested_at == value.available_at < value.close_boundary
        if timeframe == "4h":
            assert value.open_at.hour == 4 and value.close_boundary.hour == 8
            assert value.elapsed_fraction_at_receipt == pytest.approx(203 / 240)
        # This row must never enter the confirmed-candle collector.
        assert client.parse_bar(raw_row(timeframe), timeframe, NOW, inst_id=inst_id) is None
    finally:
        await client.close()


@pytest.mark.parametrize(
    "case", ["empty", "closed", "previous", "future", "duplicate", "invalid", "nan"]
)
async def test_no_final_future_stale_or_invalid_row_is_accepted(case):
    row = raw_row()
    rows = [row]
    if case == "empty":
        rows = []
    elif case == "closed":
        row[-1] = "1"
    elif case in {"previous", "future"}:
        row[0] = str(int(row[0]) + (1 if case == "future" else -1) * 14400000)
    elif case == "duplicate":
        rows.append(row.copy())
    elif case == "invalid":
        row[2] = "1"
    else:
        row[4] = "NaN"
    client = OKXClient(
        MarketConfig(),
        now=lambda: NOW,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"code": "0", "data": rows})
        ),
    )
    try:
        if case in {"duplicate", "invalid", "nan"}:
            with pytest.raises(DomainError) as caught:
                await client.forming_candle(
                    "4h", NOW + timedelta(seconds=30), inst_id="BTC-USDT-SWAP"
                )
            assert caught.value.code == "INVALID_FORMING_CANDLE"
        else:
            assert (
                await client.forming_candle(
                    "4h", NOW + timedelta(seconds=30), inst_id="BTC-USDT-SWAP"
                )
                is None
            )
    finally:
        await client.close()


async def test_request_crossing_close_boundary_is_explicitly_rejected():
    now = NOW.replace(hour=7, minute=59, second=59)

    def respond(request):
        nonlocal now
        now += timedelta(seconds=2)
        return httpx.Response(200, json={"code": "0", "data": [raw_row()]})

    client = OKXClient(MarketConfig(), now=lambda: now, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(DomainError) as caught:
            await client.forming_candle("4h", now + timedelta(seconds=30), inst_id="BTC-USDT-SWAP")
        assert caught.value.code == "FORMING_CANDLE_BOUNDARY"
    finally:
        await client.close()


@pytest.mark.parametrize("timeframe", ["1h", "4h", "6h", "12h"])
def test_provisional_reuses_all_core_formulas_without_changing_confirmed(timeframe):
    bars = bars_at(boundary(NOW, "1h"), timeframe)
    current = snapshot(timeframe)
    config = FeatureConfig()
    before = compute_features(bars, config, decision_at=NOW)
    values = compute_provisional_features(bars, current, config, NOW)
    assert len(values) == len(CORE_FEATURES) == 26
    assert all(v.status == "PROVISIONAL" and v.event_at == NOW for v in values)
    assert all(v.source.startswith("provisional:") and not v.observation_ids for v in values)
    assert compute_features(bars, config, decision_at=NOW) == before
    # A separate synthetic *closed* fixture with the same OHLCV gives identical
    # numbers from the existing library. Never use this final fixture in live input.
    closed_equivalent = Bar(
        **current.model_dump(
            exclude={
                "status",
                "confirm",
                "requested_at",
                "available_at",
                "elapsed_fraction_at_receipt",
            }
        ),
        available_at=current.close_boundary,
    )
    expected = calculate(
        (*bars, closed_equivalent), QuantConfig(), decision_at=current.close_boundary
    )
    assert {v.name: v.value for v in values} == {
        v.name: v.value for v in expected if v.name in CORE_FEATURES
    }
    by_name = {v.name: v for v in values}
    assert by_name["return_1"].value == pytest.approx(130 / 179 - 1)
    assert by_name["rsi"].value < next(v.value for v in before if v.name == "rsi")
    for name in ("rvol", "volume_zscore"):
        assert by_name[name].quality == "PARTIAL_COVERAGE"
        assert by_name[name].caveat == "PARTIAL_VOLUME_VS_FULL_HISTORY_NOT_TIME_NORMALIZED"
    assert by_name["breakout_strength"].caveat == "INTRABAR_NOT_CLOSE_CONFIRMED"


def test_snapshot_cannot_be_promoted_to_confirmed_or_calculated_at_a_past_decision():
    current = snapshot()
    with pytest.raises(ValidationError):
        snapshot(confirm="1")
    with pytest.raises(ValidationError):
        snapshot(available_at=current.close_boundary)
    with pytest.raises(ValidationError):
        snapshot(requested_at=NOW + timedelta(seconds=1))
    with pytest.raises(DomainError) as caught:
        compute_provisional_features(
            bars_at(boundary(NOW), "4h"), current, FeatureConfig(), NOW - timedelta(seconds=1)
        )
    assert caught.value.code == "FORMING_CANDLE_SCOPE"
