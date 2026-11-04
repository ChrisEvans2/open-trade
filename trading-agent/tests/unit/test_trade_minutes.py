from datetime import timedelta
from decimal import Decimal

import pytest

from trading_agent.config import AppConfig, MarketConfig, TradeMinuteConfig
from trading_agent.market import trades
from trading_agent.market.okx import OKXClient

INST = "BTC-USDT-SWAP"


def metadata(inst=INST):
    return dict(
        instId=inst,
        instType="SWAP",
        ctType="linear",
        settleCcy="USDT",
        ctValCcy=inst.split("-")[0],
        ctVal="0.01",
        ctMult="1",
    )


def trade(at, ident, side="buy", size="200", source="0", inst=INST):
    return dict(
        instId=inst,
        tradeId=str(ident),
        ts=str(int(at.timestamp() * 1000)),
        side=side,
        sz=size,
        px="100",
        source=source,
    )


class Client:
    parse_bar = OKXClient.parse_bar

    def __init__(self, start, pages=None, *, volume="5"):
        self.start = start
        self.current = start + timedelta(minutes=2)
        self.config = MarketConfig(inst_id=INST, contract_type="perpetual", settlement_asset="USDT")
        self.pages = (
            list(pages)
            if pages is not None
            else [
                [
                    trade(start + timedelta(seconds=40), 3, "sell", "300", "1"),
                    trade(start + timedelta(seconds=1), 2),
                    trade(start - timedelta(seconds=1), 1),
                ]
            ]
        )
        self.volume = volume
        self.calls = []

    def now(self):
        return self.current

    async def get(self, path, params, deadline):
        self.calls.append((path, dict(params)))
        if path.endswith("history-candles"):
            return [
                [
                    str(int(self.start.timestamp() * 1000)),
                    "100",
                    "100",
                    "100",
                    "100",
                    str(Decimal(self.volume) * 100),
                    self.volume,
                    str(Decimal(self.volume) * 100),
                    "1",
                ]
            ]
        return self.pages.pop(0)


async def capture(client, **settings):
    return await trades.capture_minute(
        client,
        INST,
        client.start,
        client.now() + timedelta(seconds=30),
        TradeMinuteConfig(**settings),
        metadata(),
    )


async def test_exact_minute_sides_sources_and_boundary_dedup(anchor):
    start = anchor - timedelta(minutes=1)
    inside = trade(start + timedelta(seconds=40), 3, "sell", "300", "1")
    client = Client(
        start,
        [
            [trade(anchor, 4, "buy", "999"), inside],
            [inside, trade(start, 2), trade(start - timedelta(milliseconds=1), 1)],
        ],
    )
    result = await capture(client)
    assert result.observation.quality == "OK"
    p = result.observation.payload
    assert (p["buy"], p["sell"], p["trade_count"]) == (Decimal(2), Decimal(3), 2)
    assert p["by_source"]["1"]["sell"] == 3
    assert client.calls[-1][1]["after"] == "3"
    assert client.calls[-1][1]["type"] == "1"
    assert len(result.rows) == 2


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("side", "unknown", "TRADE_CLASSIFICATION"),
        ("source", "9", "TRADE_CLASSIFICATION"),
        ("instId", "ETH-USDT-SWAP", "TRADE_SCOPE"),
        ("sz", "-1", "TRADE_FIELDS_INVALID"),
        ("sz", "NaN", "TRADE_SOURCE_INVALID"),
        ("tradeId", "abc", "TRADE_ID_INVALID"),
    ],
)
async def test_bad_raw_fields_do_not_silently_disappear(anchor, field, value, reason):
    client = Client(anchor - timedelta(minutes=1))
    client.pages[0][0][field] = value
    result = await capture(client)
    assert result.observation.quality == "MISSING"
    assert result.observation.payload["reason"] == reason


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("empty", "TRADE_SOURCE_EMPTY"),
        ("limit", "TRADE_PAGE_LIMIT"),
        ("stalled", "TRADE_PAGINATION_STALLED"),
        ("conflict", "TRADE_DUPLICATE_CONFLICT"),
    ],
)
async def test_incomplete_or_conflicting_pagination_is_not_valid(anchor, mode, reason):
    start = anchor - timedelta(minutes=1)
    row = trade(start, 3)
    pages = {
        "empty": [[]],
        "limit": [[row]],
        "stalled": [[row], [row]],
        "conflict": [[row], [{**row, "sz": "300"}]],
    }[mode]
    result = await capture(
        Client(start, pages, volume="2"), max_pages_per_minute=1 if mode == "limit" else 2
    )
    assert result.observation.quality == "MISSING"
    assert result.observation.payload["reason"] == reason


async def test_total_mismatch_keeps_raw_and_does_not_allocate_gap(anchor):
    result = await capture(Client(anchor - timedelta(minutes=1), volume="6"))
    assert result.observation.payload["reason"] == "TRADE_CANDLE_VOLUME_MISMATCH"
    assert result.observation.payload["buy"] == 2
    assert result.observation.payload["sell"] == 3
    assert len(result.rows) == 2


async def test_swapped_sides_detected_even_with_correct_total(anchor, monkeypatch):
    original = trades.aggregate

    def wrong(rows, face):
        values = original(rows, face)
        values["buy"], values["sell"] = values["sell"], values["buy"]
        return values

    monkeypatch.setattr(trades, "aggregate", wrong)
    result = await capture(Client(anchor - timedelta(minutes=1)))
    assert result.observation.payload["reason"] == "TRADE_SIDE_CHECK_FAILED"
    assert result.observation.quality == "MISSING"


async def test_zero_volume_requires_pagination_boundary_and_zero_candle(anchor):
    start = anchor - timedelta(minutes=1)
    result = await capture(Client(start, [[trade(start - timedelta(seconds=1), 1)]], volume="0"))
    assert result.observation.quality == "OK"
    assert result.observation.payload["buy"] == result.observation.payload["sell"] == 0


def test_collector_policy_changes_engine_identity():
    config = AppConfig()
    legacy = config.model_copy(
        update={"market": config.market.model_copy(update={"taker_source": "rubik"})}
    )
    assert config.engine_config_hash != legacy.engine_config_hash
    assert config.market.trade_minutes.raw_retention_hours == 24


async def test_minute_mode_never_requests_defective_taker_statistics(anchor):
    from trading_agent.market.observations import collect_statistics

    client = Client(anchor - timedelta(minutes=1))
    result = await collect_statistics(client, client.now() + timedelta(seconds=10))
    assert not client.calls
    assert not any(o.kind == "taker_volume" for o in result)
