from __future__ import annotations

from trading_core.schemas import (
    AccountState,
    ExecutionResult,
    FinalAction,
    MarketSnapshot,
    OrderRequest,
    RiskDecision,
)


class PaperExecutor:
    def __init__(self):
        self.executed_ids: set[str] = set()

    async def execute(
        self,
        decision: RiskDecision,
        market: MarketSnapshot,
        account: AccountState | None = None,
        stop_loss_pct: float | None = None,
        take_profit_pct: float | None = None,
        trailing_stop_pct: float | None = None,
        trailing_activation_pct: float | None = None,
    ) -> ExecutionResult:
        if not decision.allow_trade or decision.final_action == FinalAction.HOLD:
            return ExecutionResult(
                action=FinalAction.HOLD,
                symbol=market.symbol,
                success=True,
                reason=decision.reason,
            )

        side = "buy" if decision.final_action == FinalAction.OPEN_LONG else "sell"
        order = OrderRequest(
            symbol=market.symbol,
            side=side,
            order_type="market",
            position_pct=decision.final_position_pct,
            leverage=decision.final_leverage,
            reduce_only=decision.final_action == FinalAction.CLOSE,
            client_order_id=self._client_order_id(decision, market),
        )
        if order.client_order_id in self.executed_ids:
            return ExecutionResult(
                action=FinalAction.HOLD,
                symbol=market.symbol,
                success=False,
                reason="重复 client_order_id，模拟执行已拒绝。",
                metadata={"client_order_id": order.client_order_id},
            )
        self.executed_ids.add(order.client_order_id)
        return ExecutionResult(
            action=decision.final_action,
            symbol=market.symbol,
            success=True,
            order=order,
            fill_price=market.close,
            reason="paper order filled",
        )

    @staticmethod
    def _client_order_id(decision: RiskDecision, market: MarketSnapshot) -> str:
        stamp = market.timestamp.strftime("%Y%m%d%H%M%S")
        return f"paper_{stamp}_{market.symbol}_{decision.final_action}"
