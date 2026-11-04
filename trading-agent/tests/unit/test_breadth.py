from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from conftest import bars_at

from trading_agent.config import FeatureConfig, MarketConfig
from trading_agent.domain.serialization import content_hash
from trading_agent.domain.time import INTERVAL_SECONDS, OKX_BARS, TIMEFRAMES, boundary
from trading_agent.market.breadth import collect_breadth, member_history
from trading_agent.market.features import compute_features
from trading_agent.market.okx import OKXClient


def candles(end, tf, *, above=True):
    rows = []
    for i in range(20):
        at = end - timedelta(seconds=INTERVAL_SECONDS[tf] * (20 - i))
        price = 100 + i if above else 200 - i
        rows.append(
            [
                str(int(at.timestamp() * 1000)),
                str(price),
                str(price + 1),
                str(price - 1),
                str(price),
                "10",
                "1",
                str(price),
                "1",
            ]
        )
    return rows


async def test_http_breadth_tracks_members_coverage_and_utc_periods(anchor):
    basket = ("AAA-USDT", "BBB-USDT-SWAP", "CCC-USDT", "DDD-USDT-SWAP", "EEE-USDT")
    features = FeatureConfig(market_basket=basket)
    now = anchor + timedelta(seconds=65)
    requests = []

    def handler(request):
        requests.append(request)
        inst_id = request.url.params["instId"]
        tf = next(tf for tf in TIMEFRAMES if OKX_BARS[tf] == request.url.params["bar"])
        rows = (
            []
            if inst_id.startswith("EEE")
            else candles(boundary(anchor, tf), tf, above=inst_id.startswith(("AAA", "BBB")))
        )
        return httpx.Response(200, json={"code": "0", "data": rows})

    client = OKXClient(
        MarketConfig(inst_id="BTC-USDT-SWAP", contract_type="perpetual", settlement_asset="USDT"),
        transport=httpx.MockTransport(handler),
        now=lambda: now,
        feature_config=features,
    )
    try:
        records = await collect_breadth(client, now + timedelta(seconds=5))
    finally:
        await client.close()
    assert len(requests) == 20 and len(records) == 4
    for tf in TIMEFRAMES:
        record = next(o for o in records if o.payload["timeframe"] == tf)
        assert record.event_at == boundary(anchor, tf) and record.available_at == now
        assert record.payload["covered"] == 4 and record.payload["above_sma20"] == 2
        assert record.payload["members"][-1]["quality"] == "MISSING"
        result = {f.name: f for f in compute_features(bars_at(anchor, tf), features, records, now)}[
            "market_breadth"
        ]
        assert result.value == 0.5 and result.quality == "PARTIAL_COVERAGE"
        assert "4/5" in result.missing_reason
        stricter = features.model_copy(update={"market_basket_min_coverage": Decimal("1")})
        rejected = {
            f.name: f for f in compute_features(bars_at(anchor, tf), stricter, records, now)
        }["market_breadth"]
        assert rejected.value is None and rejected.quality == "PARTIAL_COVERAGE"


def test_member_history_rejects_gaps_open_buckets_and_bad_prices(anchor):
    rows = candles(anchor, "1h")
    good = member_history("AAA-USDT", "1h", rows, anchor + timedelta(seconds=60), anchor)
    assert good["market_type"] == "spot" and good["above_sma20"]
    for bad in (
        rows[1:],
        [*rows, rows[-1]],
        [*rows[:-1], [*rows[-1][:-1], "0"]],
        [*rows[:-1], [rows[-1][0], "1", "2", "0", "1", "10", "1", "10", "1"]],
    ):
        with pytest.raises(ValueError):
            member_history("AAA-USDT", "1h", bad, anchor + timedelta(seconds=60), anchor)
    # Acquisition time doesn't become market content and won't refresh an unchanged long trend.
    again = member_history("AAA-USDT", "1h", rows, anchor + timedelta(hours=1), anchor)
    assert content_hash(good) == content_hash(again)


async def test_empty_basket_never_fetches_or_invents_members(anchor):
    class Client:
        feature_config = FeatureConfig()

        async def get(self, *args):
            raise AssertionError("empty basket must not fetch")

    assert await collect_breadth(Client(), anchor) == ()
