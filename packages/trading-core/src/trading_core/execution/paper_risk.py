from __future__ import annotations

from dataclasses import dataclass

from trading_core.risk.engine import RiskLimits
from trading_core.schemas import (
    AccountState,
    ExecutionResult,
    FinalAction,
    MarketSnapshot,
    RiskDecision,
)


@dataclass(frozen=True)
class PaperCloseDecision:
    should_close: bool
    reason: str = ""
    pnl_pct: float = 0


class PaperPositionRiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def evaluate_close(
        self, account: AccountState, market: MarketSnapshot
    ) -> PaperCloseDecision:
        signed_margin = account.open_positions.get(market.symbol)
        if signed_margin is None:
            return PaperCloseDecision(False)
        entry = account.open_position_entry_price.get(market.symbol)
        if not entry or entry <= 0:
            return PaperCloseDecision(False)

        side = 1 if signed_margin > 0 else -1
        leverage = account.open_position_leverage.get(market.symbol, 1.0)
        price_move_pct = ((market.close - entry) / entry) * side
        pnl_pct = price_move_pct * leverage
        self._update_watermarks(account, market.symbol, market.close, side)

        stop_threshold = self._position_pnl_threshold(
            account.open_position_stop_loss_pct.get(market.symbol),
            leverage,
            self.limits.stop_loss_pct,
        )
        take_threshold = self._position_pnl_threshold(
            account.open_position_take_profit_pct.get(market.symbol),
            leverage,
            self.limits.take_profit_pct,
        )

        if pnl_pct <= -stop_threshold:
            return PaperCloseDecision(True, f"触发模拟止损，PnL {pnl_pct:.2%}", pnl_pct)
        range_protection = self._range_position_protection(
            account, market.symbol, market.close, side, entry, price_move_pct
        )
        if range_protection.should_close:
            return PaperCloseDecision(True, range_protection.reason, pnl_pct)
        if pnl_pct >= take_threshold:
            return PaperCloseDecision(True, f"触发模拟止盈，PnL {pnl_pct:.2%}", pnl_pct)

        trailing_stop_pct, trailing_activation_pct = self._trailing_levels(
            account, market.symbol
        )
        trailing = self._trailing_drawdown(
            account, market.symbol, market.close, side, entry
        )
        if (
            trailing_stop_pct > 0
            and trailing_activation_pct > 0
            and trailing >= trailing_stop_pct
            and price_move_pct >= trailing_activation_pct
        ):
            return PaperCloseDecision(
                True,
                f"触发移动止盈，价格回撤 {trailing:.2%}，价格盈利 {price_move_pct:.2%}",
                pnl_pct,
            )
        return PaperCloseDecision(False, pnl_pct=pnl_pct)

    def _range_position_protection(
        self,
        account: AccountState,
        symbol: str,
        close: float,
        side: int,
        entry: float,
        price_move_pct: float,
    ) -> PaperCloseDecision:
        if account.open_position_strategy_type.get(symbol) != "range_mean_reversion":
            return PaperCloseDecision(False)
        range_mid = account.open_position_range_mid.get(symbol)
        if range_mid and (
            (side > 0 and close >= range_mid) or (side < 0 and close <= range_mid)
        ):
            return PaperCloseDecision(True, "区间均值回归到达中线，触发区间止盈")

        stop_loss_pct = account.open_position_stop_loss_pct.get(symbol)
        if not stop_loss_pct or stop_loss_pct <= 0:
            return PaperCloseDecision(False)
        unrealized_r = price_move_pct / stop_loss_pct
        if unrealized_r >= 0.5:
            fee_buffer_pct = 0.0005
            account.open_position_stop_loss_pct[symbol] = min(
                stop_loss_pct, fee_buffer_pct
            )
        return PaperCloseDecision(False)

    def close_result(self, market: MarketSnapshot, reason: str) -> ExecutionResult:
        return ExecutionResult(
            action=FinalAction.CLOSE,
            symbol=market.symbol,
            success=True,
            fill_price=market.close,
            reason=reason,
        )

    def close_decision(self, reason: str) -> RiskDecision:
        return RiskDecision(
            allow_trade=True,
            final_action=FinalAction.CLOSE,
            final_position_pct=0,
            final_leverage=1,
            risk_mode="paper",
            risk_checks={"paper_exit": "triggered"},
            reason=reason,
        )

    @staticmethod
    def _update_watermarks(
        account: AccountState, symbol: str, price: float, side: int
    ) -> None:
        if side > 0:
            account.open_position_high_watermark[symbol] = max(
                account.open_position_high_watermark.get(symbol, price), price
            )
        else:
            account.open_position_low_watermark[symbol] = min(
                account.open_position_low_watermark.get(symbol, price), price
            )

    @staticmethod
    def _trailing_drawdown(
        account: AccountState, symbol: str, price: float, side: int, entry: float
    ) -> float:
        if side > 0:
            high = account.open_position_high_watermark.get(symbol, price)
            return max(0, (high - price) / entry)
        low = account.open_position_low_watermark.get(symbol, price)
        return max(0, (price - low) / entry)

    def _trailing_levels(
        self, account: AccountState, symbol: str
    ) -> tuple[float, float]:
        trailing_stop = account.open_position_trailing_stop_pct.get(symbol)
        activation = account.open_position_trailing_activation_pct.get(symbol)
        if trailing_stop is not None and activation is not None:
            return trailing_stop, activation

        # Existing positions predate the price-unit trailing fields. Their technical
        # stop is 2 ATR in the active strategy, so use it to migrate safely in place.
        atr_pct = (
            account.open_position_stop_loss_pct.get(symbol, self.limits.stop_loss_pct)
            / 2
        )
        trailing_stop = max(
            self.limits.trailing_drawdown_min_pct,
            atr_pct * self.limits.trailing_drawdown_atr_multiplier,
        )
        activation = min(
            max(
                self.limits.trailing_activation_min_pct,
                atr_pct * self.limits.trailing_activation_atr_multiplier,
            ),
            self.limits.trailing_activation_max_pct,
        )
        return trailing_stop, activation

    @staticmethod
    def _position_pnl_threshold(
        position_price_pct: float | None,
        leverage: float,
        fallback_position_pnl_pct: float,
    ) -> float:
        if position_price_pct and position_price_pct > 0:
            return position_price_pct * leverage
        return fallback_position_pnl_pct
