"""Load existing risk/account rules without importing a CLI or strategy engine."""

from pathlib import Path

from trading_core.parameters import TradeParameters
from trading_core.risk.engine import RiskLimits
from trading_core.schemas import AccountState, ExecutionMode


def build_risk_limits(
    risk_config: dict, mode, execution_mode: ExecutionMode | str = ExecutionMode.PAPER
) -> RiskLimits:
    values = dict(risk_config.get("defaults", {}))
    values.update(risk_config.get("execution_modes", {}).get(str(execution_mode), {}))
    values.update(risk_config.get("modes", {}).get(str(mode), {}))
    allowed = set(RiskLimits.__dataclass_fields__)
    return RiskLimits(**{key: value for key, value in values.items() if key in allowed})


def load_account_state(path: Path) -> AccountState:
    if not path.exists():
        return AccountState()
    return AccountState.model_validate_json(path.read_text(encoding="utf-8"))


def load_trade_parameters(strategy_config: dict) -> TradeParameters:
    raw = strategy_config.get("freqtrade_style", {})
    if not raw.get("enabled", True):
        raise ValueError("预测执行需要现有 ATR 与区间风险参数")
    # Preserve the original loader's conversions and configuration defaults.
    return TradeParameters(
        startup_candle_count=int(raw.get("startup_candle_count", 80)),
        ema_fast=int(raw.get("ema_fast", 12)),
        ema_slow=int(raw.get("ema_slow", 26)),
        rsi_period=int(raw.get("rsi_period", 14)),
        atr_period=int(raw.get("atr_period", 14)),
        rsi_long_min=float(raw.get("rsi_long_min", 52)),
        rsi_short_max=float(raw.get("rsi_short_max", 48)),
        atr_min_pct=float(raw.get("atr_min_pct", 0.001)),
        volume_window=int(raw.get("volume_window", 20)),
        min_volume_ratio=float(raw.get("min_volume_ratio", 0.8)),
        adx_period=int(raw.get("adx_period", 14)),
        min_adx=float(raw.get("min_adx", 20)),
        ema_slope_window=int(raw.get("ema_slope_window", 3)),
        min_ema_slope_pct=float(raw.get("min_ema_slope_pct", 0.001)),
        min_ema_separation_pct=float(raw.get("min_ema_separation_pct", 0.001)),
        max_price_extension_atr=float(raw.get("max_price_extension_atr", 2.0)),
        default_position_pct=float(raw.get("default_position_pct", 0.12)),
        regime_filter_enabled=bool(raw.get("regime_filter_enabled", True)),
        min_trend_efficiency=float(raw.get("min_trend_efficiency", 0.35)),
        short_rebound_guard_enabled=bool(raw.get("short_rebound_guard_enabled", True)),
        short_min_trend_efficiency=float(raw.get("short_min_trend_efficiency", 0.45)),
        short_min_regime_confidence=float(raw.get("short_min_regime_confidence", 0.65)),
        range_trading_enabled=bool(raw.get("range_trading_enabled", True)),
        range_position_pct=float(raw.get("range_position_pct", 0.04)),
        range_min_confidence=float(raw.get("range_min_confidence", 0.65)),
        range_wide_min_confidence=float(raw.get("range_wide_min_confidence", 0.72)),
        range_quality_threshold=float(raw.get("range_quality_threshold", 0.65)),
        trend_min_bars_before_entry=int(raw.get("trend_min_bars_before_entry", 2)),
        min_bars_before_entry=int(raw.get("min_bars_before_entry", 3)),
        range_wide_cooldown_bars=int(raw.get("range_wide_cooldown_bars", 3)),
        range_rsi_long_max=float(raw.get("range_rsi_long_max", 38)),
        range_rsi_short_min=float(raw.get("range_rsi_short_min", 62)),
        range_edge_threshold=float(raw.get("range_edge_threshold", 0.22)),
        late_breakout_max_range_quality=float(
            raw.get("late_breakout_max_range_quality", 0.25)
        ),
        core_breakout_enabled=bool(raw.get("core_breakout_enabled", True)),
        core_breakout_symbols=tuple(
            str(item).upper()
            for item in raw.get("core_breakout_symbols", ["BTCUSDT", "ETHUSDT"])
        ),
        core_breakout_position_pct=float(raw.get("core_breakout_position_pct", 0.03)),
        core_breakout_min_volume_ratio=float(
            raw.get("core_breakout_min_volume_ratio", 1.0)
        ),
        core_breakout_min_adx=float(raw.get("core_breakout_min_adx", 25.0)),
        core_breakout_max_price_extension_atr=float(
            raw.get("core_breakout_max_price_extension_atr", 3.0)
        ),
    )
