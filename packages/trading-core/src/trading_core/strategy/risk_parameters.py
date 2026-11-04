"""Shared deterministic exit parameters, independent of direction selection."""

from __future__ import annotations

from dataclasses import dataclass

from trading_core.schemas import MAX_SINGLE_TRADE_MARGIN_PCT, StrategyAction


@dataclass(frozen=True)
class SignalRiskParameters:
    suggested_position_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    atr_pct: float | None
    risk_reward_ratio: float

    def signal_fields(self) -> dict:
        return dict(self.__dict__)


def technical_stop_loss(atr_pct: float, atr_min_pct: float) -> float:
    return round(max(atr_pct * 2, atr_min_pct), 6)


def trend_parameters(
    atr_pct: float,
    atr_min_pct: float,
    position_pct: float,
    risk_reward_ratio: float = 2.0,
) -> SignalRiskParameters:
    stop = technical_stop_loss(atr_pct, atr_min_pct)
    return SignalRiskParameters(
        suggested_position_pct=min(position_pct, MAX_SINGLE_TRADE_MARGIN_PCT),
        stop_loss_pct=stop,
        take_profit_pct=round(stop * risk_reward_ratio, 6),
        atr_pct=round(atr_pct, 6) or None,
        risk_reward_ratio=risk_reward_ratio,
    )


def range_exit_parameters(
    action: StrategyAction,
    close: float,
    atr_pct: float,
    atr_min_pct: float,
    range_low: float | None,
    range_high: float | None,
    range_mid: float | None,
) -> dict:
    """Preserve the existing raw-ratio rounding and boundary buffer exactly."""
    buffer_pct = max(atr_min_pct, atr_pct * 0.5)
    if (
        action == StrategyAction.LONG
        and range_low is not None
        and range_mid is not None
    ):
        stop = max(0.000001, (close - (range_low * (1 - buffer_pct))) / close)
        take = max(0.000001, (range_mid - close) / close)
    elif (
        action == StrategyAction.SHORT
        and range_high is not None
        and range_mid is not None
    ):
        stop = max(0.000001, ((range_high * (1 + buffer_pct)) - close) / close)
        take = max(0.000001, (close - range_mid) / close)
    else:
        return {}
    return {
        "stop_loss_pct": round(stop, 6),
        "take_profit_pct": round(take, 6),
        "risk_reward_ratio": round(take / stop, 6),
    }


def prediction_parameters(
    action: StrategyAction,
    style: str,
    close: float,
    atr_pct: float,
    config,
    *,
    range_low=None,
    range_high=None,
) -> SignalRiskParameters:
    """Reuse legacy risk math without its entry signal or Advisor direction gate."""
    import math

    if action not in {StrategyAction.LONG, StrategyAction.SHORT}:
        raise ValueError("Entry direction required")
    if not all(math.isfinite(v) and v > 0 for v in (close, atr_pct)):
        raise ValueError("Valid positive price and ATR required")
    if style == "TREND":
        return trend_parameters(
            atr_pct, config.atr_min_pct, config.default_position_pct
        )
    if style == "BREAKOUT":
        return trend_parameters(
            atr_pct, config.atr_min_pct, config.core_breakout_position_pct, 1.5
        )
    if style != "RANGE":
        raise ValueError("Unknown trade style")
    if (
        range_low is None
        or range_high is None
        or not math.isfinite(range_low)
        or not math.isfinite(range_high)
        or not 0 < range_low < close < range_high
    ):
        raise ValueError("Range entry requires valid enclosing boundaries")
    midpoint = (range_low + range_high) / 2
    if (action == StrategyAction.LONG and midpoint <= close) or (
        action == StrategyAction.SHORT and midpoint >= close
    ):
        raise ValueError(
            "Range midpoint must provide positive reward in the selected direction"
        )
    base = trend_parameters(atr_pct, config.atr_min_pct, config.range_position_pct, 1.2)
    fields = range_exit_parameters(
        action, close, atr_pct, config.atr_min_pct, range_low, range_high, midpoint
    )
    return SignalRiskParameters(**{**base.signal_fields(), **fields})
