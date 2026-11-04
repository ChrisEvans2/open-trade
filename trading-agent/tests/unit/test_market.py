from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from conftest import bars_at
from pydantic import ValidationError

from trading_agent.config import AppConfig, FeatureConfig, MarketConfig, PaperConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash
from trading_agent.domain.time import boundary, window
from trading_agent.market.features import compute_features
from trading_agent.market.okx import OKXClient
from trading_agent.market.snapshots import build_snapshot, validate_bars


def test_utc_boundaries_and_window(anchor):
    assert boundary(anchor, "6h") == datetime(2026, 10, 7, 6, tzinfo=UTC)
    assert boundary(anchor, "12h") == datetime(2026, 10, 7, 0, tzinfo=UTC)
    assert window(anchor) == (anchor + timedelta(minutes=5), anchor + timedelta(hours=1, minutes=5))


def test_snapshot_counts_and_cache_hash_stability(anchor):
    data = {tf: bars_at(anchor, tf) for tf in ("1h", "4h", "6h", "12h")}
    first = build_snapshot(anchor, anchor + timedelta(seconds=65), data, FeatureConfig())
    second = build_snapshot(anchor, anchor + timedelta(seconds=70), data, FeatureConfig())
    assert len(first.group("12h").bars) == 80
    assert len(first.group("12h").model_bar_ids) == 30
    assert first.group("12h").input_content_hash == second.group("12h").input_content_hash
    assert first.payload_hash != second.payload_hash  # Evidence freezing time is retained.
    revised = list(data["12h"])
    revised[-1] = revised[-1].model_copy(update={"revision_no": 2})
    third = build_snapshot(
        anchor, anchor + timedelta(seconds=70), {**data, "12h": tuple(revised)}, FeatureConfig()
    )
    assert third.group("12h").input_content_hash != first.group("12h").input_content_hash


def test_no_gap_duplicate_or_future_data(anchor):
    bars = bars_at(anchor)
    decision = anchor + timedelta(seconds=65)
    for broken in (bars[:-1], bars[:40] + bars[41:] + bars[-1:], bars[::-1]):
        with pytest.raises(DomainError):
            validate_bars(broken, anchor, "1h", decision)
    with pytest.raises(DomainError, match="冻结"):
        validate_bars(bars, anchor, "1h", anchor)
    with pytest.raises(ValidationError):
        bars[-1].model_copy().model_validate({**bars[-1].model_dump(), "high": "1"})


def test_hand_calculated_features(anchor):
    bars = bars_at(anchor)
    values = {f.name: f for f in compute_features(bars, FeatureConfig())}
    assert values["return_20"].value == pytest.approx(179 / 159 - 1)
    assert values["roc"].value == values["return_20"].value
    assert values["rsi"].value == 100
    assert values["plus_di"].value == 25
    assert values["minus_di"].value == 0
    assert values["adx"].value == 100
    assert values["atr_pct"].value == pytest.approx(4 / 179)
    assert values["regression_slope"].value == pytest.approx(1 / 179)
    assert values["regression_r2"].value == 1
    assert values["efficiency_ratio"].value == 1
    assert values["distance_to_high"].value == pytest.approx(1 / 180)
    assert values["btc_correlation"].quality == "NOT_APPLICABLE"
    assert values["cvd"].value is None


def test_degenerate_data_not_invented(anchor):
    features = {f.name: f for f in compute_features(bars_at(anchor, flat=True), FeatureConfig())}
    assert features["rsi"].value == 50
    assert features["atr_pct"].value == 0
    for name in ("adx", "close_position", "regression_r2", "efficiency_ratio", "volatility_zscore"):
        assert features[name].quality == "DEGENERATE"
        assert features[name].value is None


def test_config_fail_closed_and_hash():
    with pytest.raises(ValueError):
        AppConfig().market.require_instrument()
    with pytest.raises(ValidationError):
        PaperConfig(enabled=True)
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"runtime": {"api_workers": 2}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"surprise": "config"})
    assert content_hash({"amount": Decimal("1.000")}) == content_hash({"amount": Decimal("1")})
    with pytest.raises(ValueError):
        content_hash({"x": float("nan")})


async def test_okx_mapping_confirm_and_exact_pricing(anchor):
    seen = []

    async def respond(request):
        seen.append(request)
        ts = int((boundary(anchor, "6h") - timedelta(hours=6)).timestamp() * 1000)
        row = [str(ts), "100", "102", "98", "101", "20", "2", "200", "1"]
        return httpx.Response(200, json={"code": "0", "data": [row, [*row[:-1], "0"]]})

    client = OKXClient(
        MarketConfig(inst_id="BTC-USDT-SWAP", contract_type="perpetual", settlement_asset="USDT"),
        transport=httpx.MockTransport(respond),
        now=lambda: anchor + timedelta(minutes=2),
    )
    try:
        bars = await client.candles(anchor, "6h", anchor + timedelta(minutes=3))
        assert seen[0].url.params["bar"] == "6Hutc"
        assert len(bars) == 1 and bars[0].volume_base == Decimal(2)
        assert await client.pricing_bar(anchor, anchor + timedelta(minutes=3)) is None
        with pytest.raises(DomainError, match="公共"):
            await client.get("/api/v5/trade/order", {}, anchor + timedelta(minutes=3))
    finally:
        await client.close()


@pytest.mark.parametrize(
    "field,value", [("instType", "FUTURES"), ("settleCcy", "BTC"), ("state", "suspend")]
)
async def test_instrument_metadata_must_match_contract_configuration(anchor, field, value):
    row = {"instId": "BTC-USDT-SWAP", "instType": "SWAP", "settleCcy": "USDT", "state": "live"}
    row[field] = value
    client = OKXClient(
        MarketConfig(inst_id="BTC-USDT-SWAP", contract_type="perpetual", settlement_asset="USDT"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": "0", "data": [row]})
        ),
        now=lambda: anchor,
    )
    try:
        with pytest.raises(DomainError) as caught:
            await client.instrument(anchor + timedelta(seconds=5))
        assert caught.value.code in {"INSTRUMENT_MISMATCH", "INSTRUMENT_UNAVAILABLE"}
    finally:
        await client.close()
