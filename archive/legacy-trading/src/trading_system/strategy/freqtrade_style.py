from __future__ import annotations

from trading_core.parameters import TradeParameters as EmaRsiAtrConfig

import pandas as pd

from trading_core.schemas import MarketSnapshot, StrategyAction, StrategySignal, utc_now
from trading_system.strategy.market_regime import MarketRegimeClassifier
from trading_core.strategy.risk_parameters import range_exit_parameters, technical_stop_loss, trend_parameters




class FreqtradeStyleStrategy:
    """
    Minimal Freqtrade-style adapter.

    It keeps the familiar populate_indicators / populate_entry_trend shape, while
    returning this framework's StrategySignal instead of placing trades.
    """

    def __init__(self, config: EmaRsiAtrConfig | None = None):
        self.config = config or EmaRsiAtrConfig()
        self.regime_classifier = MarketRegimeClassifier()

    def generate_signal_from_candles(self, candles: list[dict], market: MarketSnapshot) -> StrategySignal:
        if len(candles) < self.config.startup_candle_count:
            return self._hold(market, ["insufficient_candles"])
        dataframe = pd.DataFrame(candles)
        dataframe = self.populate_indicators(dataframe)
        dataframe = self.populate_entry_trend(dataframe)
        latest = dataframe.iloc[-1]
        regime = self.regime_classifier.classify(candles)

        range_signal = self._range_signal(market, latest, regime)
        if range_signal is not None:
            return range_signal

        if bool(latest.get("enter_long")):
            invalid = self._regime_invalid_reasons(StrategyAction.LONG, regime)
            if invalid:
                return self._hold(market, invalid, regime)
            return self._signal(
                market,
                StrategyAction.LONG,
                confidence=self._confidence(latest, long=True),
                reasons=[
                    "ema_trend_up",
                    "ema_slope_up",
                    "adx_trend_confirmed",
                    "rsi_momentum",
                    "atr_volatility_ok",
                    "volume_confirmed",
                    "price_not_overextended",
                ],
                latest=latest,
                regime=regime,
            )
        if bool(latest.get("enter_short")):
            invalid = self._regime_invalid_reasons(StrategyAction.SHORT, regime)
            if invalid:
                return self._hold(market, invalid, regime)
            return self._signal(
                market,
                StrategyAction.SHORT,
                confidence=self._confidence(latest, long=False),
                reasons=[
                    "ema_trend_down",
                    "ema_slope_down",
                    "adx_trend_confirmed",
                    "rsi_weakness",
                    "atr_volatility_ok",
                    "volume_confirmed",
                    "price_not_overextended",
                ],
                latest=latest,
                regime=regime,
            )
        core_breakout_signal = self._core_breakout_signal(market, latest, regime)
        if core_breakout_signal is not None:
            return core_breakout_signal
        return self._hold(market, ["freqtrade_style_no_entry"], regime)

    def populate_indicators(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        cfg = self.config
        df = dataframe.copy()
        df["ema_fast"] = df["close"].ewm(span=cfg.ema_fast, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=cfg.ema_slow, adjust=False).mean()
        df["rsi"] = self._rsi(df["close"], cfg.rsi_period)
        df["atr"] = self._atr(df, cfg.atr_period)
        df["atr_pct"] = df["atr"] / df["close"]
        df["volume_mean"] = df["volume"].rolling(cfg.volume_window).mean()
        df["volume_ratio"] = df["volume"] / df["volume_mean"]
        df["adx"] = self._adx(df, cfg.adx_period)
        df["ema_separation_pct"] = (df["ema_fast"] - df["ema_slow"]).abs() / df["close"]
        df["ema_fast_slope_pct"] = (df["ema_fast"] - df["ema_fast"].shift(cfg.ema_slope_window)) / df["close"]
        df["price_extension_atr"] = (df["close"] - df["ema_fast"]).abs() / df["atr"]
        return df

    def populate_entry_trend(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        cfg = self.config
        df = dataframe.copy()
        df["enter_long"] = (
            (df["ema_fast"] > df["ema_slow"])
            & (df["close"] > df["ema_fast"])
            & (df["rsi"] >= cfg.rsi_long_min)
            & (df["atr_pct"] >= cfg.atr_min_pct)
            & (df["volume_ratio"] >= cfg.min_volume_ratio)
            & (df["adx"] >= cfg.min_adx)
            & (df["ema_separation_pct"] >= cfg.min_ema_separation_pct)
            & (df["ema_fast_slope_pct"] >= cfg.min_ema_slope_pct)
            & (df["price_extension_atr"] <= cfg.max_price_extension_atr)
        )
        df["enter_short"] = (
            (df["ema_fast"] < df["ema_slow"])
            & (df["close"] < df["ema_fast"])
            & (df["rsi"] <= cfg.rsi_short_max)
            & (df["atr_pct"] >= cfg.atr_min_pct)
            & (df["volume_ratio"] >= cfg.min_volume_ratio)
            & (df["adx"] >= cfg.min_adx)
            & (df["ema_separation_pct"] >= cfg.min_ema_separation_pct)
            & (df["ema_fast_slope_pct"] <= -cfg.min_ema_slope_pct)
            & (df["price_extension_atr"] <= cfg.max_price_extension_atr)
        )
        return df

    def _signal(
        self,
        market: MarketSnapshot,
        action: StrategyAction,
        confidence: float,
        reasons: list[str],
        latest: pd.Series,
        regime=None,
        position_pct: float | None = None,
        risk_reward_ratio: float = 2.0,
    ) -> StrategySignal:
        parameters = trend_parameters(
            float(latest.get("atr_pct", self.config.atr_min_pct)), self.config.atr_min_pct,
            position_pct if position_pct is not None else self.config.default_position_pct, risk_reward_ratio,
        )
        return StrategySignal(
            timestamp=utc_now(),
            symbol=market.symbol,
            timeframe=market.timeframe,
            signal=action,
            confidence=confidence,
            entry_reason=reasons,
            invalid_reason=[],
            **{**parameters.signal_fields(), "atr_pct": round(float(latest.get("atr_pct", 0)), 6) or None},
            market_regime=regime,
        )

    @staticmethod
    def _hold(market: MarketSnapshot, invalid_reasons: list[str], regime=None) -> StrategySignal:
        return StrategySignal(
            timestamp=utc_now(),
            symbol=market.symbol,
            timeframe=market.timeframe,
            signal=StrategyAction.HOLD,
            confidence=0,
            entry_reason=[],
            invalid_reason=invalid_reasons,
            suggested_position_pct=0,
            market_regime=regime,
        )

    def _range_signal(self, market: MarketSnapshot, latest: pd.Series, regime) -> StrategySignal | None:
        cfg = self.config
        if not cfg.range_trading_enabled:
            return None
        if regime.regime != "range" or regime.pattern != "range_edge" or regime.range_position is None:
            return None
        rsi = float(latest.get("rsi", 50))
        atr_pct = float(latest.get("atr_pct", cfg.atr_min_pct))
        if regime.range_position <= cfg.range_edge_threshold and rsi <= cfg.range_rsi_long_max:
            invalid = self._range_invalid_reasons(StrategyAction.LONG, regime)
            if invalid:
                return self._hold(market, invalid, regime)
            signal = self._signal(
                market,
                StrategyAction.LONG,
                confidence=min(0.68, max(0.5, regime.confidence - 0.05)),
                reasons=["range_regime", "lower_range_edge", "rsi_oversold", "mean_reversion_entry"],
                latest=latest,
                regime=regime,
                position_pct=cfg.range_position_pct,
                risk_reward_ratio=1.2,
            )
            if regime.range_low is None or regime.range_mid is None:
                return signal
            return signal.model_copy(update=range_exit_parameters(
                StrategyAction.LONG, market.close, atr_pct, cfg.atr_min_pct,
                regime.range_low, regime.range_high, regime.range_mid,
            ))
        if regime.range_position >= 1 - cfg.range_edge_threshold and rsi >= cfg.range_rsi_short_min:
            invalid = self._range_invalid_reasons(StrategyAction.SHORT, regime)
            if invalid:
                return self._hold(market, invalid, regime)
            signal = self._signal(
                market,
                StrategyAction.SHORT,
                confidence=min(0.68, max(0.5, regime.confidence - 0.05)),
                reasons=["range_regime", "upper_range_edge", "rsi_overbought", "mean_reversion_entry"],
                latest=latest,
                regime=regime,
                position_pct=cfg.range_position_pct,
                risk_reward_ratio=1.2,
            )
            if regime.range_high is None or regime.range_mid is None:
                return signal
            return signal.model_copy(update=range_exit_parameters(
                StrategyAction.SHORT, market.close, atr_pct, cfg.atr_min_pct,
                regime.range_low, regime.range_high, regime.range_mid,
            ))
        return None

    def _core_breakout_signal(self, market: MarketSnapshot, latest: pd.Series, regime) -> StrategySignal | None:
        cfg = self.config
        if not cfg.core_breakout_enabled:
            return None
        if market.symbol.upper() not in {symbol.upper() for symbol in cfg.core_breakout_symbols}:
            return None
        detailed_regime = str(getattr(regime, "detailed_regime", "") or "")
        risk_flags = set(getattr(regime, "risk_flags", []) or [])
        range_position = getattr(regime, "range_position", None)
        high_breaks = int(getattr(regime, "recent_high_break_count", 0) or 0)
        low_breaks = int(getattr(regime, "recent_low_break_count", 0) or 0)

        action: StrategyAction | None = None
        if detailed_regime == "BREAKOUT_UP" or (
            detailed_regime == "EXPANDING_RANGE"
            and (
                "fresh_upper_break" in risk_flags
                or high_breaks >= 2
                or (range_position is not None and range_position >= 0.90)
            )
        ):
            action = StrategyAction.LONG
        elif detailed_regime == "BREAKOUT_DOWN" or (
            detailed_regime == "EXPANDING_RANGE"
            and (
                "fresh_lower_break" in risk_flags
                or low_breaks >= 2
                or (range_position is not None and range_position <= 0.10)
            )
        ):
            action = StrategyAction.SHORT
        if action is None:
            return None

        if not self._core_breakout_indicators_confirm(action, latest):
            return None
        if not self._core_breakout_regime_confirm(action, regime):
            return None

        reasons = [
            "core_breakout_probe",
            "core_symbol",
            detailed_regime.lower(),
            "volume_confirmed",
            "adx_confirmed",
        ]
        if action == StrategyAction.LONG:
            reasons.extend(["fresh_upper_break", "ema_direction_up"])
        else:
            reasons.extend(["fresh_lower_break", "ema_direction_down"])
        return self._signal(
            market,
            action,
            confidence=min(0.72, max(0.56, float(getattr(regime, "confidence", 0.52) or 0.52))),
            reasons=reasons,
            latest=latest,
            regime=regime,
            position_pct=cfg.core_breakout_position_pct,
            risk_reward_ratio=1.5,
        )

    def _core_breakout_indicators_confirm(self, action: StrategyAction, latest: pd.Series) -> bool:
        cfg = self.config
        close = float(latest.get("close", 0) or 0)
        ema_fast = float(latest.get("ema_fast", 0) or 0)
        ema_slow = float(latest.get("ema_slow", 0) or 0)
        rsi = float(latest.get("rsi", 50) or 50)
        volume_ratio = float(latest.get("volume_ratio", 0) or 0)
        adx = float(latest.get("adx", 0) or 0)
        atr_pct = float(latest.get("atr_pct", 0) or 0)
        price_extension_atr = float(latest.get("price_extension_atr", 999) or 999)
        if atr_pct < cfg.atr_min_pct:
            return False
        if volume_ratio < cfg.core_breakout_min_volume_ratio:
            return False
        if adx < cfg.core_breakout_min_adx:
            return False
        if price_extension_atr > cfg.core_breakout_max_price_extension_atr:
            return False
        if action == StrategyAction.LONG:
            return ema_fast > ema_slow and close > ema_fast and rsi >= 52
        return ema_fast < ema_slow and close < ema_fast and rsi <= 48

    def _core_breakout_regime_confirm(self, action: StrategyAction, regime) -> bool:
        range_position = getattr(regime, "range_position", None)
        risk_flags = set(getattr(regime, "risk_flags", []) or [])
        high_breaks = int(getattr(regime, "recent_high_break_count", 0) or 0)
        low_breaks = int(getattr(regime, "recent_low_break_count", 0) or 0)
        if action == StrategyAction.LONG:
            return "fresh_upper_break" in risk_flags or high_breaks >= 2 or (
                range_position is not None and range_position >= 0.90
            )
        return "fresh_lower_break" in risk_flags or low_breaks >= 2 or (
            range_position is not None and range_position <= 0.10
        )

    def _range_invalid_reasons(self, action: StrategyAction, regime) -> list[str]:
        if not self.config.regime_filter_enabled:
            return []
        cfg = self.config
        reasons: list[str] = []
        detailed_regime = getattr(regime, "detailed_regime", "UNKNOWN")
        risk_flags = set(getattr(regime, "risk_flags", []) or [])
        block_reasons = set(getattr(regime, "block_reasons", []) or [])
        bars_in_regime = int(getattr(regime, "bars_in_current_regime", 0) or 0)
        range_quality = float(getattr(regime, "range_quality_score", 1.0) or 0.0)
        high_breaks = int(getattr(regime, "recent_high_break_count", 0) or 0)
        low_breaks = int(getattr(regime, "recent_low_break_count", 0) or 0)

        if regime.environment == "high_vol":
            reasons.append("range_high_vol_block")
        if detailed_regime in {"RANGE_HIGH_VOL", "BREAKOUT_UP", "BREAKOUT_DOWN", "WATERFALL", "EXPANDING_RANGE"}:
            reasons.append(f"{detailed_regime.lower()}_block")
        if "range_expanding" in risk_flags or "range_expanding_block" in block_reasons:
            reasons.append("range_expanding_block")
        if regime.confidence < cfg.range_min_confidence:
            reasons.append("range_low_confidence")
        if range_quality < cfg.range_quality_threshold:
            reasons.append("low_range_quality")
        if 0 < bars_in_regime < cfg.min_bars_before_entry:
            reasons.append("regime_not_stable")
        if detailed_regime == "RANGE_WIDE":
            if regime.confidence < cfg.range_wide_min_confidence:
                reasons.append("range_wide_low_confidence")
            if 0 < bars_in_regime < cfg.range_wide_cooldown_bars:
                reasons.append("range_wide_cooldown")
            if range_quality < cfg.range_quality_threshold:
                reasons.append("range_wide_low_quality")
        if action == StrategyAction.SHORT and ("fresh_upper_break" in risk_flags or high_breaks >= 2):
            reasons.append("fresh_upper_break")
        if action == StrategyAction.LONG and ("fresh_lower_break" in risk_flags or low_breaks >= 2):
            reasons.append("fresh_lower_break")
        return sorted(set(reasons), key=reasons.index)

    def _regime_invalid_reasons(self, action: StrategyAction, regime) -> list[str]:
        if not self.config.regime_filter_enabled:
            return []
        reasons: list[str] = []
        detailed_regime = getattr(regime, "detailed_regime", "UNKNOWN")
        previous_regime = str(getattr(regime, "previous_regime", "") or "")
        bars_in_regime = int(getattr(regime, "bars_in_current_regime", 0) or 0)
        unstable_prior_regimes = {
            "TRANSITION",
            "EXPANDING_RANGE",
            "SHOCK",
            "CHOPPY_HIGH_VOL",
            "RANGE_HIGH_VOL",
            "WATERFALL",
        }
        if regime.regime in {"shock", "choppy_high_vol", "transition"}:
            reasons.append(f"regime_block_{regime.regime}")
        if detailed_regime in {"WATERFALL", "EXPANDING_RANGE", "RANGE_HIGH_VOL", "SHOCK", "CHOPPY_HIGH_VOL", "TRANSITION"}:
            reasons.append(f"regime_block_{detailed_regime.lower()}")
        if (
            regime.regime in {"trend_up", "trend_down"}
            and previous_regime in unstable_prior_regimes
            and 0 < bars_in_regime < self.config.trend_min_bars_before_entry
        ):
            reasons.append("regime_not_stable")
        if regime.regime == "range":
            reasons.append("regime_block_trend_entry_in_range")
        if regime.regime == "squeeze":
            reasons.append("regime_block_wait_for_squeeze_breakout")
        if regime.regime == "transition" and regime.pattern in {"v_reversal", "inverted_v", "false_breakout", "none"}:
            reasons.append(f"regime_block_transition_{regime.pattern}")
        if regime.regime in {"trend_up", "trend_down"} and regime.trend_efficiency < self.config.min_trend_efficiency:
            reasons.append("regime_block_low_trend_efficiency")
        self._append_late_breakout_reasons(action, regime, reasons)
        if action == StrategyAction.SHORT and self.config.short_rebound_guard_enabled:
            if regime.regime == "trend_down" and regime.trend_efficiency < self.config.short_min_trend_efficiency:
                reasons.append("short_guard_low_trend_efficiency")
            if regime.regime == "trend_down" and regime.confidence < self.config.short_min_regime_confidence:
                reasons.append("short_guard_low_regime_confidence")
            if regime.regime == "trend_down" and regime.trend_direction != StrategyAction.SHORT:
                reasons.append("short_guard_direction_not_confirmed")
        if action == StrategyAction.SHORT and regime.pattern in {"relief_rally", "v_reversal"}:
            reasons.append(f"regime_block_short_during_{regime.pattern}")
        if action == StrategyAction.LONG and regime.pattern in {"pullback", "inverted_v"} and regime.regime != "trend_up":
            reasons.append(f"regime_block_long_during_{regime.pattern}")
        if action == StrategyAction.LONG and regime.regime == "trend_down":
            reasons.append("regime_direction_conflict_trend_down")
        if action == StrategyAction.SHORT and regime.regime == "trend_up":
            reasons.append("regime_direction_conflict_trend_up")
        if action == StrategyAction.LONG and detailed_regime == "BREAKOUT_DOWN":
            reasons.append("regime_direction_conflict_breakout_down")
        if action == StrategyAction.SHORT and detailed_regime == "BREAKOUT_UP":
            reasons.append("regime_direction_conflict_breakout_up")
        return reasons

    def _append_late_breakout_reasons(self, action: StrategyAction, regime, reasons: list[str]) -> None:
        if regime.regime not in {"trend_up", "trend_down"} or regime.range_position is None:
            return
        cfg = self.config
        risk_flags = set(getattr(regime, "risk_flags", []) or [])
        range_position = float(regime.range_position)
        range_quality = float(getattr(regime, "range_quality_score", 1.0) or 0.0)
        high_breaks = int(getattr(regime, "recent_high_break_count", 0) or 0)
        low_breaks = int(getattr(regime, "recent_low_break_count", 0) or 0)
        previous_regime = str(getattr(regime, "previous_regime", "") or "")

        low_quality_break = range_quality <= cfg.late_breakout_max_range_quality
        if (
            action == StrategyAction.SHORT
            and low_quality_break
            and range_position <= cfg.range_edge_threshold
            and (
                "fresh_lower_break" in risk_flags
                or low_breaks >= 2
                or (previous_regime == "BREAKOUT_DOWN" and low_breaks >= 1)
            )
        ):
            reasons.append("late_short_lower_edge_break")
        if (
            action == StrategyAction.LONG
            and low_quality_break
            and range_position >= 1 - cfg.range_edge_threshold
            and (
                "fresh_upper_break" in risk_flags
                or high_breaks >= 2
                or (previous_regime == "BREAKOUT_UP" and high_breaks >= 1)
            )
        ):
            reasons.append("late_long_upper_edge_break")

    def _technical_stop_loss_pct(self, row: pd.Series) -> float:
        atr_pct = float(row.get("atr_pct", self.config.atr_min_pct))
        return technical_stop_loss(atr_pct, self.config.atr_min_pct)

    @staticmethod
    def _rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gains = delta.clip(lower=0)
        losses = -delta.clip(upper=0)
        avg_gain = gains.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = losses.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100)
        rsi = rsi.mask((avg_gain == 0) & (avg_loss > 0), 0)
        return rsi.fillna(50)

    @staticmethod
    def _atr(dataframe: pd.DataFrame, period: int) -> pd.Series:
        high_low = dataframe["high"] - dataframe["low"]
        high_close = (dataframe["high"] - dataframe["close"].shift()).abs()
        low_close = (dataframe["low"] - dataframe["close"].shift()).abs()
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    @staticmethod
    def _adx(dataframe: pd.DataFrame, period: int) -> pd.Series:
        up_move = dataframe["high"].diff()
        down_move = -dataframe["low"].diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        atr = FreqtradeStyleStrategy._atr(dataframe, period)
        plus_di = 100 * plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr
        minus_di = 100 * minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr
        denominator = plus_di + minus_di
        dx = 100 * (plus_di - minus_di).abs() / denominator.where(denominator != 0)
        return dx.astype("float64").ewm(alpha=1 / period, min_periods=period, adjust=False).mean().fillna(0)

    @staticmethod
    def _confidence(row: pd.Series, long: bool) -> float:
        trend_strength = abs(float(row["ema_fast"]) - float(row["ema_slow"])) / float(row["close"])
        rsi = float(row["rsi"])
        momentum = max(0, rsi - 50) / 50 if long else max(0, 50 - rsi) / 50
        volume = min(0.2, max(0, float(row.get("volume_ratio", 1)) - 1) * 0.1)
        return round(min(0.9, 0.55 + trend_strength * 10 + momentum * 0.2 + volume), 4)
