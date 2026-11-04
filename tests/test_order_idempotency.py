import pytest

from trading_core.execution.paper import PaperExecutor
from trading_core.schemas import FinalAction, MarketSnapshot, RiskDecision


@pytest.mark.asyncio
async def test_paper_executor_rejects_duplicate_order_id():
    market = MarketSnapshot(
        symbol="BTCUSDT",
        timeframe="1h",
        timestamp="2026-05-23T10:00:00Z",
        open=100,
        high=101,
        low=99,
        close=100.5,
        volume=10,
    )
    decision = RiskDecision(
        allow_trade=True,
        final_action=FinalAction.OPEN_LONG,
        final_position_pct=0.1,
        risk_mode="paper",
        risk_checks={},
        reason="test",
    )
    executor = PaperExecutor()
    first = await executor.execute(decision, market)
    second = await executor.execute(decision, market)
    assert first.success is True
    assert second.success is False
