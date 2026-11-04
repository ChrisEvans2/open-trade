"""Existing deterministic trade parameters; no technical entry strategy."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TradeParameters:
    startup_candle_count: int = 80
    ema_fast: int = 12
    ema_slow: int = 26
    rsi_period: int = 14
    atr_period: int = 14
    rsi_long_min: float = 52
    rsi_short_max: float = 48
    atr_min_pct: float = 0.001
    volume_window: int = 20
    min_volume_ratio: float = 0.8
    adx_period: int = 14
    min_adx: float = 20.0
    ema_slope_window: int = 3
    min_ema_slope_pct: float = 0.001
    min_ema_separation_pct: float = 0.001
    max_price_extension_atr: float = 2.0
    default_position_pct: float = 0.12
    regime_filter_enabled: bool = True
    min_trend_efficiency: float = 0.35
    short_rebound_guard_enabled: bool = True
    short_min_trend_efficiency: float = 0.45
    short_min_regime_confidence: float = 0.65
    range_trading_enabled: bool = True
    range_position_pct: float = 0.04
    range_min_confidence: float = 0.65
    range_wide_min_confidence: float = 0.72
    range_quality_threshold: float = 0.65
    trend_min_bars_before_entry: int = 2
    min_bars_before_entry: int = 3
    range_wide_cooldown_bars: int = 3
    range_rsi_long_max: float = 38
    range_rsi_short_min: float = 62
    range_edge_threshold: float = 0.22
    late_breakout_max_range_quality: float = 0.25
    core_breakout_enabled: bool = True
    core_breakout_symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    core_breakout_position_pct: float = 0.03
    core_breakout_min_volume_ratio: float = 1.0
    core_breakout_min_adx: float = 25.0
    core_breakout_max_price_extension_atr: float = 3.0
