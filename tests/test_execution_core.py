from __future__ import annotations

import pytest
from trading_core.execution.live import OKXLiveExecutor
from trading_core.market_data.okx_client import OKXClientError
from trading_core.risk.engine import RiskEngine
from trading_core.schemas import (
    AccountState,
    AIAnalysisResult,
    ExecutionMode,
    FinalAction,
    MarketSnapshot,
    RiskMode,
    RuntimeState,
    StrategyAction,
    StrategySignal,
)


def signal(action=StrategyAction.LONG):
    return StrategySignal(
        symbol="BTCUSDT",
        timeframe="1h",
        signal=action,
        confidence=0.7,
        entry_reason=["test"],
        suggested_position_pct=0.1,
        stop_loss_pct=0.02,
    )


def ai():
    return AIAnalysisResult(
        asset="BTC",
        sentiment="neutral",
        event_risk="low",
        should_block_trade=False,
        position_multiplier=1,
        confidence=0.7,
        reason="test",
    )


def market():
    return MarketSnapshot(
        symbol="BTCUSDT",
        timeframe="1h",
        timestamp="2026-06-16T08:00:00Z",
        open=100000,
        high=101000,
        low=99000,
        close=100000,
        volume=1,
    )


class OrderClient:
    def __init__(self):
        self.leverage_calls = []
        self.order_calls = []

    async def get_instruments(self, inst_type="SWAP", inst_id=None):
        return [{"instId": inst_id, "ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01"}]

    async def set_leverage(self, inst_id, lever, mgn_mode="isolated"):
        self.leverage_calls.append((inst_id, lever, mgn_mode))
        return {"code": "0"}

    async def place_order(self, **kwargs):
        self.order_calls.append(kwargs)
        return {
            "code": "0",
            "data": [{"ordId": "1", "clOrdId": kwargs["client_order_id"]}],
        }

    async def place_algo_order(self, body):
        self.order_calls.append({"algo_order": body})
        return {"code": "0", "data": [{"algoId": "algo-1"}]}

    async def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        return {"ordId": ord_id, "avgPx": "100001", "accFillSz": "0.1"}

    async def get_fills(self, inst_id, ord_id=None, limit=100):
        return [{"fillPx": "100001", "fillSz": "0.1", "fee": "-0.01", "fillPnl": "0"}]


class RejectingOrderClient(OrderClient):
    async def place_order(self, **kwargs):
        raise OKXClientError(
            "OKX API error code=1 msg=All operations failed",
            {
                "code": "1",
                "msg": "All operations failed",
                "data": [{"sCode": "51008", "sMsg": "Insufficient balance"}],
            },
        )


class SlippedOrderClient(OrderClient):
    async def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        return {"ordId": ord_id, "avgPx": "101000", "accFillSz": "0.1"}


def test_emergency_blocks_new_entries():
    decision = RiskEngine().evaluate(
        signal(StrategyAction.LONG),
        ai(),
        AccountState(),
        RuntimeState(
            mode=RiskMode.EMERGENCY, execution_mode=ExecutionMode.LIVE, emergency=True
        ),
    )
    assert decision.allow_trade is False
    assert decision.final_action == FinalAction.HOLD


def test_emergency_allows_reduce_only_close():
    decision = RiskEngine().evaluate(
        signal(StrategyAction.CLOSE),
        ai(),
        AccountState(
            open_positions={"BTCUSDT": 0.05}, open_position_leverage={"BTCUSDT": 3.0}
        ),
        RuntimeState(
            mode=RiskMode.EMERGENCY, execution_mode=ExecutionMode.LIVE, emergency=True
        ),
    )
    assert decision.allow_trade is True
    assert decision.final_action == FinalAction.CLOSE
    assert decision.final_position_pct == 0.05
    assert decision.final_leverage == 3.0


@pytest.mark.asyncio
async def test_live_executor_uses_swap_inst_id_and_reduce_only():
    client = OrderClient()
    executor = OKXLiveExecutor(client)
    decision = RiskEngine().evaluate(
        signal(StrategyAction.CLOSE),
        ai(),
        AccountState(
            open_positions={"BTCUSDT": -0.05}, open_position_leverage={"BTCUSDT": 5.0}
        ),
        RuntimeState(execution_mode=ExecutionMode.LIVE),
    )
    result = await executor.execute(
        decision,
        market(),
        AccountState(
            equity=100,
            open_positions={"BTCUSDT": -0.05},
            open_position_leverage={"BTCUSDT": 5.0},
        ),
    )
    assert result.success is True
    assert result.order.inst_id == "BTC-USDT-SWAP"
    assert result.order.reduce_only is True
    assert result.order.side == "buy"
    assert client.order_calls[-1]["inst_id"] == "BTC-USDT-SWAP"
    assert client.order_calls[-1]["reduce_only"] is True


@pytest.mark.asyncio
async def test_live_executor_records_exchange_fill_details():
    client = OrderClient()
    executor = OKXLiveExecutor(client)
    decision = RiskEngine().evaluate(
        signal(StrategyAction.LONG),
        ai(),
        AccountState(),
        RuntimeState(execution_mode=ExecutionMode.LIVE),
    )
    result = await executor.execute(decision, market(), AccountState(equity=100))
    assert result.fill_price == 100001
    assert result.metadata["exchange_contracts"] == 0.1
    assert result.metadata["contract_value"] == 0.01
    assert result.metadata["exchange_fee_amount"] == -0.01


@pytest.mark.asyncio
async def test_live_executor_preserves_okx_rejection_details():
    executor = OKXLiveExecutor(RejectingOrderClient())
    decision = RiskEngine().evaluate(
        signal(StrategyAction.LONG),
        ai(),
        AccountState(),
        RuntimeState(execution_mode=ExecutionMode.LIVE),
    )
    result = await executor.execute(decision, market(), AccountState(equity=100))
    assert result.success is False
    assert result.metadata["okx_error_response"]["data"][0]["sCode"] == "51008"


@pytest.mark.asyncio
async def test_live_entry_attaches_mark_price_take_profit_and_stop_loss():
    client = OrderClient()
    executor = OKXLiveExecutor(client)
    decision = RiskEngine().evaluate(
        signal(StrategyAction.LONG),
        ai(),
        AccountState(),
        RuntimeState(execution_mode=ExecutionMode.LIVE),
    )
    result = await executor.execute(
        decision,
        market(),
        AccountState(equity=100),
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
    )
    assert result.success is True
    fixed_exit = client.order_calls[-1]["algo_order"]
    assert fixed_exit["ordType"] == "oco"
    assert fixed_exit["slTriggerPxType"] == "mark"
    assert fixed_exit["tpTriggerPxType"] == "mark"
    assert fixed_exit["slOrdPx"] == "-1"
    assert fixed_exit["tpOrdPx"] == "-1"
    assert result.metadata["fixed_exit_response"]["code"] == "0"


@pytest.mark.asyncio
async def test_live_exit_algos_use_exchange_fill_price_not_signal_close():
    client = SlippedOrderClient()
    executor = OKXLiveExecutor(client)
    decision = RiskEngine().evaluate(
        signal(StrategyAction.LONG),
        ai(),
        AccountState(),
        RuntimeState(execution_mode=ExecutionMode.LIVE),
    )
    result = await executor.execute(
        decision,
        market(),
        AccountState(equity=100),
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        trailing_stop_pct=0.005,
        trailing_activation_pct=0.02,
    )
    assert result.success is True
    fixed_exit = client.order_calls[1]["algo_order"]
    trailing = client.order_calls[2]["algo_order"]
    assert fixed_exit["slTriggerPx"] == "98980.00000000"
    assert fixed_exit["tpTriggerPx"] == "105040.00000000"
    assert trailing["activePx"] == "103020.00000000"


@pytest.mark.asyncio
async def test_live_entry_places_native_trailing_stop():
    client = OrderClient()
    executor = OKXLiveExecutor(client)
    decision = RiskEngine().evaluate(
        signal(StrategyAction.LONG),
        ai(),
        AccountState(),
        RuntimeState(execution_mode=ExecutionMode.LIVE),
    )
    result = await executor.execute(
        decision,
        market(),
        AccountState(equity=100),
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        trailing_stop_pct=0.005,
        trailing_activation_pct=0.02,
    )
    assert result.success is True
    algo = client.order_calls[-1]["algo_order"]
    assert algo["ordType"] == "move_order_stop"
    assert algo["side"] == "sell"
    assert algo["reduceOnly"] is True
