from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from conftest import bars_at

from trading_agent.config import AppConfig, FeatureConfig, MarketConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.time import INTERVAL_SECONDS, OKX_BARS, TIMEFRAMES, boundary
from trading_agent.market.features import compute_features
from trading_agent.market.observations import parse_statistics
from trading_agent.market.okx import OKXClient
from trading_agent.market.snapshots import build_snapshot


def milliseconds(at):
    return str(int(at.timestamp() * 1000))


def test_taker_direction_unit_closed_window_and_timestamp_contract(anchor):
    available = anchor + timedelta(seconds=65)
    rows = [[milliseconds(anchor - timedelta(hours=i)), "1.25", "2.75"] for i in range(20, 0, -1)]
    # Latest open bucket must be excluded even though its timestamp is before decision_at.
    rows.append([milliseconds(anchor), "99", "999"])
    observed = parse_statistics(
        "taker-volume-contract", "BTC-USDT-SWAP", "1h", "open", rows, available
    )
    assert len(observed) == 20
    assert observed[-1].coverage_end == anchor and observed[-1].available_at == available
    assert observed[-1].payload["sell"] == Decimal("1.25")
    features = {
        f.name: f for f in compute_features(bars_at(anchor), FeatureConfig(), observed, available)
    }
    assert features["cvd"].value == Decimal("30")
    assert features["taker_buy_sell_ratio"].value == pytest.approx(2.2)
    closed_rows = [
        [milliseconds(anchor - timedelta(hours=i)), "1.25", "2.75"] for i in range(19, -1, -1)
    ]
    closed = parse_statistics(
        "taker-volume-contract", "BTC-USDT-SWAP", "1h", "close", closed_rows, available
    )
    assert [o.coverage_start for o in closed] == [o.coverage_start for o in observed]
    assert closed[-1].event_at == observed[-1].event_at


def test_statistics_cannot_mix_timeframe_or_estimated_oi_boundary(anchor):
    available = anchor + timedelta(seconds=65)
    rows = [
        [milliseconds(anchor - timedelta(hours=1)), "100", "1", "10000"],
        [milliseconds(anchor), "110", "1.1", "11000"],
    ]
    oi = parse_statistics(
        "contracts/open-interest-history", "BTC-USDT-SWAP", "1h", "instant", rows, available
    )
    values = {f.name: f for f in compute_features(bars_at(anchor), FeatureConfig(), oi, available)}
    assert values["oi_change"].value == pytest.approx(0.1)
    # Retain the exchange timestamp; don't interpolate a nearby point onto the boundary.
    nearby = [*rows[:1], [milliseconds(anchor - timedelta(seconds=1)), "110", "1.1", "11000"]]
    samples = parse_statistics(
        "contracts/open-interest-history", "BTC-USDT-SWAP", "1h", "instant", nearby, available
    )
    assert {
        f.name: f for f in compute_features(bars_at(anchor), FeatureConfig(), samples, available)
    }["oi_change"].quality == "MISSING"
    ratio = parse_statistics(
        "contracts/long-short-account-ratio-contract",
        "BTC-USDT-SWAP",
        "4h",
        "close",
        [[milliseconds(anchor), "1.25"]],
        available,
    )
    assert {f.name: f for f in compute_features(bars_at(anchor), FeatureConfig(), ratio)}[
        "long_short_ratio"
    ].quality == "MISSING"
    assert {
        f.name: f
        for f in compute_features(bars_at(anchor, "4h"), FeatureConfig(), ratio, available)
    }["long_short_ratio"].value == Decimal("1.25")


def test_hourly_statistics_cannot_invalidate_unclosed_long_background(anchor):
    at = anchor + timedelta(seconds=65)
    earlier = anchor - timedelta(days=1)
    first = parse_statistics(
        "contracts/long-short-account-ratio-contract",
        "BTC-USDT-SWAP",
        "1h",
        "instant",
        [[milliseconds(earlier), "1.2"]],
        at,
    )
    changed = parse_statistics(
        "contracts/long-short-account-ratio-contract",
        "BTC-USDT-SWAP",
        "1h",
        "instant",
        [[milliseconds(earlier), "1.5"]],
        at,
    )
    data = {tf: bars_at(anchor, tf) for tf in TIMEFRAMES}
    before = build_snapshot(anchor, at, data, FeatureConfig(), first)
    after = build_snapshot(anchor, at, data, FeatureConfig(), changed)
    assert before.group("1h").input_content_hash != after.group("1h").input_content_hash
    for tf in ("4h", "6h", "12h"):
        assert not before.group(tf).observation_ids
        assert before.group(tf).input_content_hash == after.group(tf).input_content_hash


@pytest.mark.parametrize(
    "rows",
    [
        [["fixture-invalid-time", "1", "2"]],
        [[True, "1", "2"]],
        [[1.25, "1", "2"]],
        [["999999999999999999999999", "1", "2"]],
        [["0", "broken", "2"]],
        [["0", "1", "NaN"]],
        [["0", "-1", "2"]],
        [["0", "1"]],
        [["0", "1", "2"], ["0", "1", "2"]],
    ],
)
def test_invalid_statistics_never_become_valid_observations(anchor, rows):
    with pytest.raises((DomainError, ValueError)):
        parse_statistics("taker-volume-contract", "fixture", "1h", "open", rows, anchor)


async def test_http_statistics_request_contract_and_four_period_coverage(anchor):
    now = anchor + timedelta(seconds=65)
    calls = []
    config = MarketConfig(
        inst_id="BTC-USDT-SWAP",
        contract_type="perpetual",
        settlement_asset="USDT",
        taker_source="rubik",
        taker_timestamp="open",
        oi_history_timestamp="instant",
        long_short_timestamp="close",
    )

    def handler(request):
        calls.append(request)
        path = request.url.path
        params = request.url.params
        if path.startswith("/api/v5/rubik/stat/"):
            tf = next(tf for tf, period in OKX_BARS.items() if period == params["period"])
            end = boundary(anchor, tf)
            span = timedelta(seconds=INTERVAL_SECONDS[tf])
            if path.endswith("taker-volume-contract"):
                assert params["unit"] == "0"
                rows = [[milliseconds(end - span * i), "1", "2"] for i in range(20, 0, -1)]
            elif path.endswith("open-interest-history"):
                rows = [
                    [milliseconds(end - span), "100", "1", "10000"],
                    [milliseconds(end), "110", "1.1", "11000"],
                ]
            else:
                rows = [[milliseconds(end), "1.4"]]
        elif path.endswith("open-interest"):
            rows = [
                {
                    "instId": config.inst_id,
                    "ts": milliseconds(now),
                    "oi": "120",
                    "oiCcy": "1.2",
                    "oiUsd": "12000",
                }
            ]
        elif path.endswith("funding-rate-history"):
            rows = []
        else:
            rows = [
                {
                    "instId": config.inst_id,
                    "ts": milliseconds(now),
                    "fundingTime": milliseconds(anchor + timedelta(hours=8)),
                    "fundingRate": "0.0001",
                }
            ]
        return httpx.Response(200, json={"code": "0", "data": rows})

    client = OKXClient(config, transport=httpx.MockTransport(handler), now=lambda: now)
    try:
        records = await client.observations(now + timedelta(seconds=5))
    finally:
        await client.close()
    assert len([r for r in calls if r.url.path.startswith("/api/v5/rubik/")]) == 12
    for tf in TIMEFRAMES:
        selected = tuple(o for o in records if o.payload.get("timeframe") == tf)
        features = {
            f.name: f for f in compute_features(bars_at(anchor, tf), FeatureConfig(), selected, now)
        }
        assert features["cvd"].value == 20
        assert features["oi_change"].value == pytest.approx(0.1)
        assert features["long_short_ratio"].value == Decimal("1.4")


async def test_undeclared_time_contract_skips_stats_and_reports_missing(anchor):
    class Client:
        config = MarketConfig(
            inst_id="fixture",
            contract_type="delivery",
            settlement_asset="USD",
            taker_source="rubik",
        )
        calls = []

        def now(self):
            return anchor + timedelta(seconds=65)

        async def get(self, path, params, deadline):
            self.calls.append(path)
            return []

    from trading_agent.market.observations import collect

    client = Client()
    records = await collect(client, anchor + timedelta(minutes=2))
    assert client.calls == ["/api/v5/public/open-interest"]
    stats = [o for o in records if o.source.startswith("okx_rubik:")]
    assert len(stats) == 12 and all(
        o.payload["reason"] == "UNVERIFIED_TIMESTAMP_CONTRACT" for o in stats
    )
    assert [o.payload["reason"] for o in records if o.source.startswith("okx_public:")] == [
        "SOURCE_EMPTY"
    ]
    before = AppConfig(market=client.config)
    after = before.model_copy(
        update={"market": client.config.model_copy(update={"taker_timestamp": "open"})}
    )
    assert before.engine_config_hash != after.engine_config_hash
