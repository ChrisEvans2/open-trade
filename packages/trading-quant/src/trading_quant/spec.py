"""Frozen v1 feature names and time spans; shared by both applications."""

CORE_FEATURES = (
    "return_1",
    "return_3",
    "return_5",
    "return_10",
    "return_20",
    "log_return",
    "adx",
    "plus_di",
    "minus_di",
    "regression_slope",
    "regression_r2",
    "efficiency_ratio",
    "choppiness_index",
    "rsi",
    "roc",
    "momentum_acceleration",
    "atr_pct",
    "bb_width",
    "volatility_zscore",
    "rvol",
    "volume_zscore",
    "vwap_deviation",
    "distance_to_high",
    "distance_to_low",
    "breakout_strength",
    "close_position",
)
EXTRA_FEATURES = (
    "cvd",
    "oi_change",
    "funding_rate",
    "funding_zscore",
    "long_short_ratio",
    "taker_buy_sell_ratio",
    "liquidation_imbalance",
    "btc_correlation",
    "btc_beta",
    "relative_strength_vs_btc",
    "market_breadth",
)


INTERVAL_SECONDS = {"1m": 60, "1h": 3600, "4h": 14400, "6h": 21600, "12h": 43200}
