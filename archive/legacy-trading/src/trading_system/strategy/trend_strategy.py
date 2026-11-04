from __future__ import annotations

from trading_core.schemas import MAX_SINGLE_TRADE_MARGIN_PCT, MarketSnapshot, StrategyAction, StrategySignal, utc_now


class TrendStrategy:
    def __init__(self, default_position_pct: float = 0.10):
        self.default_position_pct = default_position_pct

    def generate_signal(self, market: MarketSnapshot) -> StrategySignal:
        change = (market.close - market.open) / market.open
        reasons: list[str] = []
        invalid: list[str] = []
        if market.volume <= 0:
            invalid.append("missing_volume")
            return StrategySignal(
                timestamp=utc_now(),
                symbol=market.symbol,
                timeframe=market.timeframe,
                signal=StrategyAction.HOLD,
                confidence=0,
                entry_reason=[],
                invalid_reason=invalid,
                suggested_position_pct=0,
            )

        if change > 0.003:
            signal = StrategyAction.LONG
            confidence = min(0.85, 0.55 + abs(change) * 20)
            reasons.append("positive_closed_kline")
        elif change < -0.003:
            signal = StrategyAction.SHORT
            confidence = min(0.85, 0.55 + abs(change) * 20)
            reasons.append("negative_closed_kline")
        else:
            signal = StrategyAction.HOLD
            confidence = 0.5
            reasons.append("no_clear_trend")

        return StrategySignal(
            timestamp=utc_now(),
            symbol=market.symbol,
            timeframe=market.timeframe,
            signal=signal,
            confidence=confidence,
            entry_reason=reasons,
            invalid_reason=invalid,
            suggested_position_pct=min(self.default_position_pct, MAX_SINGLE_TRADE_MARGIN_PCT) if signal != StrategyAction.HOLD else 0,
        )
