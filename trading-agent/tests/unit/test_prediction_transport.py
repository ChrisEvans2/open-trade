from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from trading_core.ai.analyzer import SocialAIAnalyzer
from trading_core.parameters import TradeParameters as EmaRsiAtrConfig
from trading_core.prediction_adapter import PredictionRiskAdapter
from trading_core.risk.engine import RiskLimits
from trading_core.schemas import AccountState, MarketSnapshot, RuntimeState

from trading_agent.execution.transport import OKXTransport, OrderObservation, client_id


class Exchange:
    def __init__(self):
        self.calls = []
        self.row = None

    async def get_instruments(self, kind, inst):
        return [{"instId": inst, "ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01"}]

    async def set_leverage(self, *args):
        self.calls.append(("leverage", args))

    async def place_order(self, **kw):
        self.calls.append(("place", kw))
        self.row = dict(
            instId=kw["inst_id"],
            clOrdId=kw["client_order_id"],
            state="partially_filled",
            accFillSz="1",
            avgPx="100",
            fee="-0.01",
            feeCcy="USDT",
            ordId="fixture",
        )

    async def get_order(self, inst, *, cl_ord_id):
        self.calls.append(("query", (inst, cl_ord_id)))
        return self.row

    async def cancel_order(self, inst, *, cl_ord_id):
        self.calls.append(("cancel", (inst, cl_ord_id)))
        self.row["state"] = "canceled"


def prepared(action="OPEN_LONG", account=None):
    account = account or AccountState(equity=1000)
    market = MarketSnapshot(
        symbol="BTCUSDT",
        timeframe="1h",
        timestamp=datetime.now(UTC),
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1,
    )
    adapter = PredictionRiskAdapter(RiskLimits(), EmaRsiAtrConfig(), account, RuntimeState())
    decision = adapter.prepare(
        action,
        "TREND",
        market,
        atr_pct=0.01,
        confidence=0.1,
        reason="fixture",
        social_risk=SocialAIAnalyzer(None, "fixture")._no_recent_data("BTC"),
    )
    return decision, market, account


async def test_open_protection_stable_id_query_and_cancel_without_resubmit():
    exchange = Exchange()
    transport = OKXTransport(exchange)
    ident = uuid4()
    order = await transport.build(ident, *prepared())
    assert order["client_id"] == client_id(ident) and len(order["client_id"]) == 32
    result = await transport.submit(order)
    assert result.filled == 1 and result.fee == Decimal("-.01")
    payload = next(v for k, v in exchange.calls if k == "place")
    assert payload["attach_algo_orders"][0]["slTriggerPx"] == "98.00000000"
    assert payload["attach_algo_orders"][0]["tpTriggerPx"] == "104.00000000"
    assert payload["reduce_only"] is False
    await OKXTransport(exchange).lookup(order)
    result = await transport.cancel(order)
    assert result.state == "canceled" and result.filled == 1
    assert [k for k, _ in exchange.calls].count("place") == 1
    exchange.row = None
    assert await transport.lookup(order) is None
    assert [k for k, _ in exchange.calls].count("place") == 1


async def test_close_uses_remaining_contracts_even_when_equity_sizing_would_fail():
    account = AccountState(
        equity=0.001,
        open_positions={"BTCUSDT": 0.00001},
        open_position_contracts={"BTCUSDT": 0.37},
        open_position_leverage={"BTCUSDT": 2},
    )
    exchange = Exchange()
    transport = OKXTransport(exchange)
    order = await transport.build(uuid4(), *prepared("CLOSE", account))
    assert Decimal(order["quantity"]) == Decimal(".37")
    await transport.submit(order)
    payload = next(v for k, v in exchange.calls if k == "place")
    assert payload["side"] == "sell" and payload["reduce_only"]
    assert payload["attach_algo_orders"] is None
    assert not any(k == "leverage" for k, _ in exchange.calls)


async def test_unrelated_orders_and_unsupported_fees_are_rejected():
    exchange = Exchange()
    transport = OKXTransport(exchange)
    order = await transport.build(uuid4(), *prepared())
    await transport.submit(order)
    for field, value in (("instId", "ETH-USDT-SWAP"), ("clOrdId", "other"), ("feeCcy", "BTC")):
        original = dict(exchange.row)
        exchange.row[field] = value
        with pytest.raises(ValueError):
            await transport.lookup(order)
        exchange.row = original
    with pytest.raises(ValueError):
        OrderObservation(state="filled", filled=1, average_price="NaN")
