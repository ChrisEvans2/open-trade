"""Independent numeric and temporal examples for the external-indicator repair."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from conftest import bars_at

from trading_agent.config import FeatureConfig, MarketConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import json_value
from trading_agent.domain.time import INTERVAL_SECONDS, boundary
from trading_agent.market.context import BTC_RELATIVE, funding_context, indicator_policy
from trading_agent.market.features import compute_features
from trading_agent.market.models import Observation
from trading_agent.market.observations import collect_statistics, parse_statistics
from trading_agent.market.okx import OKXClient
from trading_agent.prediction.submissions import model_context


def series(anchor, inst_id, returns):
    bars = list(bars_at(anchor))
    price = Decimal("100")
    for index in range(80):
        if index >= 60:
            price *= 1 + returns[index - 60]
        bars[index] = bars[index].model_copy(
            update=dict(
                id=uuid4(),
                inst_id=inst_id,
                open=price,
                close=price,
                high=price + 1,
                low=price - 1,
            )
        )
    return tuple(bars)


@pytest.mark.parametrize("beta", [Decimal("2"), Decimal("-0.5")])
def test_known_affine_returns_have_expected_beta_correlation_and_relative_return(anchor, beta):
    x = [Decimal(v) for v in ("-.02", ".01", ".03", "-.01")] * 5
    y = [beta * value + Decimal(".001") for value in x]
    btc, eth = series(anchor, "BTC-USDT-SWAP", x), series(anchor, "ETH-USDT-SWAP", y)
    values = {f.name: f for f in compute_features(eth, FeatureConfig(), benchmark_bars=btc)}
    assert values["btc_beta"].value == pytest.approx(float(beta), abs=1e-12)
    assert values["btc_correlation"].value == pytest.approx(1 if beta > 0 else -1, abs=1e-12)
    asset_growth = benchmark_growth = Decimal(1)
    for asset_return, benchmark_return in zip(y, x, strict=True):
        asset_growth *= 1 + asset_return
        benchmark_growth *= 1 + benchmark_return
    assert values["relative_strength_vs_btc"].value == pytest.approx(
        float(asset_growth / benchmark_growth - 1), abs=1e-12
    )
    assert all(values[name].window == 20 for name in BTC_RELATIVE)


@pytest.mark.parametrize("defect", ["future", "shifted", "foreign", "short", "absent"])
def test_benchmark_cannot_use_future_misaligned_foreign_or_missing_data(anchor, defect):
    x = [Decimal(".01"), Decimal("-.01")] * 10
    btc, eth = series(anchor, "BTC-USDT-SWAP", x), series(anchor, "ETH-USDT-SWAP", x)
    if defect == "future":
        btc = (*btc[:-1], btc[-1].model_copy(update={"available_at": anchor + timedelta(hours=1)}))
    elif defect == "shifted":
        btc = (
            *btc[:-1],
            btc[-1].model_copy(update={"close_boundary": anchor - timedelta(hours=1)}),
        )
    elif defect == "foreign":
        btc = tuple(b.model_copy(update={"inst_id": "SOL-USDT-SWAP"}) for b in btc)
    elif defect == "short":
        btc = btc[-20:]
    else:
        btc = ()
    values = {f.name: f for f in compute_features(eth, FeatureConfig(), benchmark_bars=btc)}
    assert all(
        values[name].quality == "MISSING" and values[name].value is None for name in BTC_RELATIVE
    )


def test_constant_benchmark_is_degenerate_but_self_comparison_remains_inapplicable(anchor):
    btc = series(anchor, "BTC-USDT-SWAP", [Decimal(0)] * 20)
    eth = series(anchor, "ETH-USDT-SWAP", [Decimal(".01"), Decimal("-.01")] * 10)
    values = {f.name: f for f in compute_features(eth, FeatureConfig(), benchmark_bars=btc)}
    assert values["btc_beta"].quality == values["btc_correlation"].quality == "DEGENERATE"
    assert values["relative_strength_vs_btc"].quality == "OK"
    self_values = {f.name: f for f in compute_features(btc, FeatureConfig(), benchmark_bars=btc)}
    assert all(self_values[name].quality == "NOT_APPLICABLE" for name in BTC_RELATIVE)


@pytest.mark.parametrize("rate", [Decimal(".002"), Decimal(".003"), Decimal(".005")])
def test_constant_compound_benchmark_return_does_not_turn_roundoff_into_huge_beta(anchor, rate):
    btc = series(anchor, "BTC-USDT-SWAP", [rate] * 20)
    eth = series(anchor, "ETH-USDT-SWAP", [Decimal(".02"), Decimal("-.01")] * 10)
    values = {f.name: f for f in compute_features(eth, FeatureConfig(), benchmark_bars=btc)}
    for name in ("btc_correlation", "btc_beta"):
        assert values[name].quality == "DEGENERATE"
        assert values[name].value is None
    assert values["relative_strength_vs_btc"].quality == "OK"


def funding(at, available, rate, *, published=False, revision=1):
    return Observation(
        source="review:funding",
        inst_id="BTC-USDT-SWAP",
        kind="funding",
        event_at=at,
        available_at=available,
        unit="rate",
        revision_no=revision,
        payload={
            "event_type": "published" if published else "settled",
            "published_rate" if published else "realized_rate": str(rate),
        },
    )


def test_long_candle_current_funding_and_closed_history_have_separate_cutoffs(anchor):
    decision = anchor + timedelta(hours=1)
    history = tuple(
        funding(anchor - timedelta(hours=8 * i), decision, Decimal(i) / 10000)
        for i in range(30, -1, -1)
    )
    published = funding(anchor + timedelta(minutes=30), decision, ".002", published=True)
    after_close_settlement = funding(anchor + timedelta(minutes=20), decision, ".1")
    future_available = funding(
        anchor + timedelta(minutes=40), decision + timedelta(seconds=1), ".3", published=True
    )
    bars = bars_at(anchor, "4h")
    before = {f.name: f for f in compute_features(bars, FeatureConfig(), history, decision)}
    after = {
        f.name: f
        for f in compute_features(
            bars,
            FeatureConfig(),
            (*history, published, after_close_settlement, future_available),
            decision,
        )
    }
    assert after["funding_rate"].value == Decimal(".002")
    assert after["funding_rate"].event_at == published.event_at
    assert after["funding_rate"].observation_ids == (published.id,)
    assert after["funding_zscore"] == before["funding_zscore"]


def test_funding_context_deduplicates_revisions_and_excludes_future_events(anchor):
    history = tuple(
        funding(anchor - timedelta(hours=4 * i), anchor, ".001") for i in range(8, -1, -1)
    )
    revision = funding(anchor, anchor, ".002", revision=2)
    future = funding(anchor + timedelta(hours=4), anchor + timedelta(hours=4), "100")
    result = funding_context((*history, future, revision), anchor)
    assert result["quality"] == "OK" and len(result["settled_history"]) == 8
    assert result["settled_history"][-1]["observation_id"] == revision.id
    assert result["settled_history"][-1]["realized_rate"] == Decimal(".002")
    assert result["latest_change_per_settlement"] == Decimal(".001")
    assert result["settlement_interval_verified"] is False


def test_missing_settlement_does_not_halve_rate_by_assuming_a_double_interval(anchor):
    # Nine rows alone cannot prove that all intervening settlement events exist.
    history = tuple(
        funding(anchor - timedelta(hours=8 * i), anchor, ".001") for i in range(9, -1, -1) if i != 1
    )
    result = funding_context(history, anchor)
    assert len(result["settled_history"]) == 8
    assert result["quality"] == "PARTIAL_COVERAGE"
    assert result["settled_history"][-1]["observed_event_spacing_hours"] == 16
    assert result["settled_history"][-1]["realized_rate"] == Decimal(".001")
    assert result["mean_per_settlement_rate"] is None
    assert result["latest_change_per_settlement"] is None
    assert result["settlement_interval_verified"] is False
    assert "equivalent_8h_rate" not in result["settled_history"][-1]


@pytest.mark.parametrize("timeframe", ["4h", "6h", "12h"])
def test_open_timestamp_statistics_complete_long_period_with_correct_direction(anchor, timeframe):
    end = boundary(anchor, timeframe)
    span = timedelta(seconds=INTERVAL_SECONDS[timeframe])
    available = anchor + timedelta(seconds=65)

    def ms(at):
        return str(int(at.timestamp() * 1000))

    taker_rows = [[ms(end - span * i), "2", "5"] for i in range(20, 0, -1)]
    taker_rows.append([ms(end), "0", "900000"])
    oi_rows = [
        [ms(end - span * 2), "100", "1", "100"],
        [ms(end - span), "125", "1.25", "125"],
        [ms(end), "900000", "9000", "900000"],
    ]
    ratio_rows = [[ms(end - span), "1.4"], [ms(end), "99"]]
    observations = tuple(
        o
        for source, rows in (
            ("taker-volume-contract", taker_rows),
            ("contracts/open-interest-history", oi_rows),
            ("contracts/long-short-account-ratio-contract", ratio_rows),
        )
        for o in parse_statistics(source, "BTC-USDT-SWAP", timeframe, "open", rows, available)
    )
    values = {
        f.name: f
        for f in compute_features(
            bars_at(anchor, timeframe), FeatureConfig(), observations, available
        )
    }
    assert values["cvd"].value == 60
    assert values["cvd"].unit == "base_quantity"
    assert values["taker_buy_sell_ratio"].value == 2.5
    assert values["oi_change"].value == 0.25
    assert values["long_short_ratio"].value == Decimal("1.4")
    assert all(o.event_at <= end for o in observations)


async def test_single_timeframe_fetch_keeps_requested_anchor_and_limits_http_fanout(anchor):
    calls = []
    config = MarketConfig(
        inst_id="BTC-USDT-SWAP",
        contract_type="perpetual",
        settlement_asset="USDT",
        taker_source="rubik",
        taker_timestamp="open",
        oi_history_timestamp="open",
        long_short_timestamp="open",
    )
    basket = tuple(f"{symbol}-USDT-SWAP" for symbol in ("BTC", "ETH", "SOL", "XRP", "DOGE"))

    def response(request):
        calls.append(request)
        return httpx.Response(200, json={"code": "0", "data": []})

    now = anchor + timedelta(hours=5)
    client = OKXClient(
        config,
        feature_config=FeatureConfig(market_basket=basket),
        transport=httpx.MockTransport(response),
        now=lambda: now,
    )
    try:
        observed = await client.observations(
            now + timedelta(seconds=20),
            inst_id="ETH-USDT-SWAP",
            symbol="ETHUSDT",
            timeframes=("4h",),
            anchor=anchor,
        )
    finally:
        await client.close()
    assert len(calls) == 11  # 3 public + 3 statistics + 5 basket members, one period only.
    statistics = [r for r in calls if "/rubik/" in r.url.path]
    assert len(statistics) == 3
    assert all(r.url.params["period"] == "4H" for r in statistics)
    assert all(r.url.params["instId"] == "ETH-USDT-SWAP" for r in statistics)
    assert all(r.url.params["end"] == str(int(anchor.timestamp() * 1000) + 1) for r in statistics)
    assert all(o.inst_id == "ETH-USDT-SWAP" for o in observed)


@pytest.mark.parametrize(
    "failure, expected",
    [
        ("empty", "SOURCE_EMPTY"),
        ("open_bucket", "NO_COMPLETED_STATISTICS_BUCKET"),
        ("invalid_number", "SOURCE_INVALID"),
        ("deadline", "MARKET_DEADLINE"),
    ],
)
async def test_unavailable_statistics_keep_distinct_diagnostic_causes(anchor, failure, expected):
    class Client:
        config = MarketConfig(
            inst_id="BTC-USDT-SWAP",
            contract_type="perpetual",
            settlement_asset="USDT",
            taker_source="rubik",
            taker_timestamp="open",
            oi_history_timestamp="open",
            long_short_timestamp="open",
        )

        def now(self):
            return anchor + timedelta(seconds=65)

        async def get(self, path, params, deadline):
            if failure == "deadline":
                raise DomainError("MARKET_DEADLINE", "fixture exhausted budget")
            if not path.endswith("taker-volume-contract") or failure == "empty":
                return []
            at = anchor if failure == "open_bucket" else anchor - timedelta(hours=1)
            return [
                [
                    str(int(at.timestamp() * 1000)),
                    "1",
                    "bad" if failure == "invalid_number" else "2",
                ]
            ]

    observations = await collect_statistics(
        Client(), anchor + timedelta(minutes=2), timeframes=("1h",), anchor=anchor
    )
    taker = next(o for o in observations if o.kind == "taker_volume")
    assert taker.quality == "MISSING" and taker.payload["reason"] == expected


def test_weak_evidence_is_explicit_for_child_and_parent_has_no_new_market_input(anchor):
    features = compute_features(
        series(anchor, "ETH-USDT-SWAP", [Decimal(".01")] * 20), FeatureConfig()
    )
    context = dict(
        input_id=str(uuid4()),
        structures=[],
        horizon_rules=[],
        bar_ids=[],
        symbol="ETHUSDT",
        timeframe="4h",
        role="CHILD",
        features=json_value(features),
        indicator_policy=indicator_policy(()),
    )
    child = model_context(context)
    assert all(
        f["evidence_strength"] == "WEAK" for f in child["features"] if f["name"] in BTC_RELATIVE
    )
    assert "不得单独" in child["indicator_policy"]["btc_relative"]["prohibited_use"]
    parent = model_context(
        {
            **context,
            "role": "PARENT",
            "input_kind": "CHILD_SYNTHESIS",
            "boundary_options": [],
            "missing_timeframes": [],
        }
    )
    assert (
        "features" not in parent and "model_bars" not in parent and "reference_price" not in parent
    )
    assert parent["indicator_policy"]["btc_relative"]["evidence_strength"] == "WEAK"


def taker_matching_bars(bars):
    return tuple(
        Observation(
            source="review:matched_taker",
            inst_id=bar.inst_id,
            kind="taker_volume",
            event_at=bar.close_boundary,
            available_at=bars[-1].available_at,
            coverage_start=bar.open_at,
            coverage_end=bar.close_boundary,
            unit="base_quantity",
            payload={
                "timeframe": bar.timeframe,
                "buy": bar.volume_base * Decimal(".6"),
                "sell": bar.volume_base * Decimal(".4"),
            },
        )
        for bar in bars[-20:]
    )


def test_complete_taker_with_per_bucket_volume_agreement_remains_valid(anchor):
    bars = bars_at(anchor)
    observed = taker_matching_bars(bars)
    values = {f.name: f for f in compute_features(bars, FeatureConfig(), observed)}
    assert values["cvd"].quality == values["taker_buy_sell_ratio"].quality == "OK"
    assert values["cvd"].value == Decimal("6.78")
    assert values["taker_buy_sell_ratio"].value == 1.5


@pytest.mark.parametrize("defect", ["missing_half_bucket", "cancelling_bucket_errors"])
def test_taker_volume_disagreement_is_partial_without_inventing_missing_buy_sell(anchor, defect):
    bars = bars_at(anchor)
    observed = list(taker_matching_bars(bars))
    if defect == "missing_half_bucket":
        observed[0] = observed[0].model_copy(
            update={
                "payload": {
                    **observed[0].payload,
                    "buy": observed[0].payload["buy"] / 2,
                    "sell": observed[0].payload["sell"] / 2,
                }
            }
        )
        expected_cvd = Decimal("6.62")
    else:
        # Aggregate volume still agrees exactly; individual candle buckets do not.
        for index, adjustment in ((0, Decimal(".1")), (1, Decimal("-.1"))):
            observed[index] = observed[index].model_copy(
                update={
                    "payload": {
                        **observed[index].payload,
                        "sell": observed[index].payload["sell"] + adjustment,
                    }
                }
            )
        expected_cvd = Decimal("6.78")
    values = {f.name: f for f in compute_features(bars, FeatureConfig(), tuple(observed))}
    for name in ("cvd", "taker_buy_sell_ratio"):
        assert values[name].quality == "PARTIAL_COVERAGE"
        assert "成交量" in values[name].missing_reason
        assert values[name].observation_ids == tuple(o.id for o in observed)
    assert values["cvd"].value == expected_cvd
    assert values["taker_buy_sell_ratio"].value == 1.5
