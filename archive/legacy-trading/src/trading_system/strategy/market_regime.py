from __future__ import annotations

import pandas as pd

from trading_core.schemas import MarketRegimeSnapshot, StrategyAction, utc_now


class MarketRegimeClassifier:
    """
    Lightweight regime classifier for live trading.

    This deliberately avoids heavyweight model dependencies. It mirrors the
    HMM/LSTM repo design at the interface level: a dedicated regime layer that
    strategy routing can use, while keeping the first implementation explainable
    and safe to run inside the daemon.
    """

    def __init__(
        self,
        lookback: int = 36,
        range_window: int = 24,
        fast_ema: int = 12,
        slow_ema: int = 26,
        adx_period: int = 14,
        shock_return_z: float = 2.5,
        shock_range_z: float = 2.5,
        range_width_expansion_threshold: float = 1.25,
        recent_break_lookback: int = 3,
    ):
        self.lookback = lookback
        self.range_window = range_window
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.adx_period = adx_period
        self.shock_return_z = shock_return_z
        self.shock_range_z = shock_range_z
        self.range_width_expansion_threshold = range_width_expansion_threshold
        self.recent_break_lookback = recent_break_lookback
        self._previous_regime: str | None = None
        self._bars_in_current_regime = 0

    def classify(self, candles: list[dict]) -> MarketRegimeSnapshot:
        if len(candles) < max(self.lookback, self.slow_ema, self.adx_period) + 5:
            return self._with_transition(
                MarketRegimeSnapshot(
                    timestamp=utc_now(),
                    regime="unknown",
                    pattern="none",
                    environment="unknown",
                    detailed_regime="UNKNOWN",
                    confidence=0,
                    reason=["insufficient_regime_candles"],
                )
            )

        df = pd.DataFrame(candles).copy()
        df["return"] = df["close"].pct_change().fillna(0)
        df["abs_return"] = df["return"].abs()
        df["ema_fast"] = df["close"].ewm(span=self.fast_ema, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self.slow_ema, adjust=False).mean()
        df["atr"] = self._atr(df, self.adx_period)
        df["atr_pct"] = (df["atr"] / df["close"]).fillna(0)
        df["adx"] = self._adx(df, self.adx_period)

        latest = df.iloc[-1]
        window = df.tail(self.lookback)
        recent = df.tail(6)
        range_window = df.tail(self.range_window)
        close = float(latest["close"])
        fast = float(latest["ema_fast"])
        slow = float(latest["ema_slow"])
        adx = float(latest["adx"])
        atr_pct = float(latest["atr_pct"])

        net_move = abs(float(window["close"].iloc[-1]) - float(window["close"].iloc[0]))
        path_move = float(window["close"].diff().abs().sum())
        trend_efficiency = min(1.0, net_move / path_move) if path_move > 0 else 0.0

        short_atr = float(df["atr_pct"].tail(8).mean())
        long_atr = float(df["atr_pct"].tail(self.lookback).mean()) or short_atr or 1e-9
        volatility_ratio = short_atr / long_atr if long_atr > 0 else 1.0

        raw_high = float(range_window["high"].max())
        raw_low = float(range_window["low"].min())
        effective_high = float(range_window["high"].quantile(0.90))
        effective_low = float(range_window["low"].quantile(0.10))
        if effective_high <= effective_low:
            effective_high = raw_high
            effective_low = raw_low
        high = effective_high
        low = effective_low
        range_span = high - low
        range_span_pct = range_span / close if close > 0 else 0.0
        range_position = (close - low) / range_span if range_span > 0 else None
        range_position = min(1.0, max(0.0, range_position)) if range_position is not None else None

        previous_range_window = df.iloc[-(self.range_window * 2) : -self.range_window] if len(df) >= self.range_window * 2 else df.iloc[-self.range_window - 6 : -6]
        previous_width = 0.0
        if not previous_range_window.empty:
            previous_width = float(previous_range_window["high"].max() - previous_range_window["low"].min())
        current_width = max(raw_high - raw_low, 0.0)
        range_width_expansion = current_width / previous_width if previous_width > 0 else 1.0
        recent_break = df.tail(self.recent_break_lookback)
        recent_high_break_count = int((recent_break["high"] > effective_high).sum()) if range_span > 0 else 0
        recent_low_break_count = int((recent_break["low"] < effective_low).sum()) if range_span > 0 else 0

        short_mean = float(df["return"].tail(6).mean())
        long_mean = float(df["return"].tail(self.lookback).mean())
        long_std = float(df["return"].tail(self.lookback).std()) or 1e-9
        change_point_score = min(5.0, abs(short_mean - long_mean) / long_std)

        last_range_pct = (float(latest["high"]) - float(latest["low"])) / close
        median_range_pct = float(((df["high"] - df["low"]) / df["close"]).tail(self.lookback).median()) or 1e-9
        last_return_z = float(latest["abs_return"]) / (float(df["abs_return"].tail(self.lookback).median()) or 1e-9)
        range_z = last_range_pct / median_range_pct

        risk_flags: list[str] = []
        block_reasons: list[str] = []
        reasons: list[str] = [
            f"adx={adx:.2f}",
            f"efficiency={trend_efficiency:.2f}",
            f"vol_ratio={volatility_ratio:.2f}",
            f"change_score={change_point_score:.2f}",
        ]

        environment = "normal"
        if volatility_ratio >= 1.6:
            environment = "high_vol"
        elif volatility_ratio <= 0.65:
            environment = "low_vol"
        if float(latest.get("volume", 0)) <= 0:
            environment = "illiquid"

        ema_gap_pct = abs(fast - slow) / close
        slope_pct = (fast - float(df["ema_fast"].iloc[-4])) / close
        trend_up = fast > slow and slope_pct > 0.0008 and trend_efficiency >= 0.28 and adx >= 18
        trend_down = fast < slow and slope_pct < -0.0008 and trend_efficiency >= 0.28 and adx >= 18
        range_like = trend_efficiency <= 0.24 and adx <= 23 and range_position is not None
        squeezed = volatility_ratio <= 0.72 and ema_gap_pct <= 0.004 and adx <= 22
        shock = last_return_z >= self.shock_return_z or range_z >= self.shock_range_z
        transition = change_point_score >= 1.8 and not shock

        pattern = "none"
        regime = "unknown"
        detailed_regime = "UNKNOWN"
        trend_direction = StrategyAction.HOLD
        confidence = 0.35

        recent_return = float(recent["close"].iloc[-1] / recent["close"].iloc[0] - 1)
        prior_return = float(df["close"].iloc[-7] / df["close"].iloc[-13] - 1) if len(df) >= 13 else 0
        previous_high = float(df["high"].iloc[-self.range_window - 1 : -1].max())
        previous_low = float(df["low"].iloc[-self.range_window - 1 : -1].min())
        latest_bullish = float(latest["close"]) > float(latest["open"])
        latest_bearish = float(latest["close"]) < float(latest["open"])
        breakout_up = close > previous_high and latest_bullish and recent_return > 0
        breakout_down = close < previous_low and latest_bearish and recent_return < 0
        waterfall = (
            (shock or volatility_ratio >= 1.6)
            and recent_return < -max(atr_pct * 3.0, 0.025)
            and close < slow
        )
        expanding_range = (
            range_width_expansion >= self.range_width_expansion_threshold
            and volatility_ratio >= 1.15
            and trend_efficiency <= 0.35
            and not breakout_up
            and not breakout_down
        )
        wide_range = range_span_pct >= max(long_atr * 5.0, 0.035)

        range_quality_score = self._range_quality_score(
            range_window,
            latest=latest,
            close=close,
            effective_high=effective_high,
            effective_low=effective_low,
            raw_high=raw_high,
            raw_low=raw_low,
            range_width_expansion=range_width_expansion,
            shock=shock,
            expanding_range=expanding_range,
            recent_high_break_count=recent_high_break_count,
            recent_low_break_count=recent_low_break_count,
            atr_pct=atr_pct,
        )
        if shock:
            risk_flags.append("shock")
            block_reasons.append("shock_block")
        if transition:
            risk_flags.append("transition")
            block_reasons.append("transition_state_block")
        if expanding_range:
            risk_flags.append("range_expanding")
            block_reasons.append("range_expanding_block")
        if recent_high_break_count >= 2:
            risk_flags.append("fresh_upper_break")
        if recent_low_break_count >= 2:
            risk_flags.append("fresh_lower_break")
        if shock:
            regime = "shock"
            confidence = 0.8
            reasons.append(f"shock_return_z={last_return_z:.2f}")
            reasons.append(f"shock_range_z={range_z:.2f}")
        elif squeezed:
            regime = "squeeze"
            confidence = 0.68
            reasons.append("volatility_squeeze")
        elif transition:
            regime = "transition"
            confidence = min(0.78, 0.45 + change_point_score * 0.12)
            reasons.append("recent_distribution_shift")
        elif trend_up:
            regime = "trend_up"
            trend_direction = StrategyAction.LONG
            confidence = min(0.88, 0.48 + trend_efficiency * 0.35 + min(adx, 40) / 160)
        elif trend_down:
            regime = "trend_down"
            trend_direction = StrategyAction.SHORT
            confidence = min(0.88, 0.48 + trend_efficiency * 0.35 + min(adx, 40) / 160)
        elif range_like:
            regime = "range"
            confidence = min(0.78, 0.45 + (0.28 - trend_efficiency) + max(0, 23 - adx) / 80)
            if range_position is not None and (range_position <= 0.22 or range_position >= 0.78):
                pattern = "range_edge"
                reasons.append(f"range_position={range_position:.2f}")
        else:
            regime = "choppy_high_vol" if environment == "high_vol" else "transition"
            confidence = 0.52
            reasons.append("mixed_or_low_quality_structure")

        if regime == "trend_down" and recent_return > max(atr_pct * 2.0, 0.012) and close < slow:
            pattern = "relief_rally"
            confidence = max(confidence, 0.72)
            reasons.append(f"relief_rally_recent_return={recent_return:.4f}")
        elif regime == "trend_up" and recent_return < -max(atr_pct * 2.0, 0.012) and close > slow:
            pattern = "pullback"
            confidence = max(confidence, 0.72)
            reasons.append(f"pullback_recent_return={recent_return:.4f}")
        elif prior_return < -max(atr_pct * 4.0, 0.025) and recent_return > max(atr_pct * 2.5, 0.018):
            pattern = "v_reversal"
            regime = "transition"
            confidence = max(confidence, 0.76)
            reasons.append("sharp_drop_then_fast_rebound")
        elif prior_return > max(atr_pct * 4.0, 0.025) and recent_return < -max(atr_pct * 2.5, 0.018):
            pattern = "inverted_v"
            regime = "transition"
            confidence = max(confidence, 0.76)
            reasons.append("sharp_rally_then_fast_fade")
        elif close > previous_high and float(latest["close"]) < float(latest["open"]):
            pattern = "false_breakout"
            regime = "transition"
            confidence = max(confidence, 0.66)
            reasons.append("failed_upper_breakout")
        elif close < previous_low and float(latest["close"]) > float(latest["open"]):
            pattern = "false_breakout"
            regime = "transition"
            confidence = max(confidence, 0.66)
            reasons.append("failed_lower_breakdown")
        elif pattern == "none" and regime in {"trend_up", "trend_down"}:
            pattern = "accelerating_trend" if volatility_ratio >= 1.35 else "smooth_trend"

        if waterfall:
            detailed_regime = "WATERFALL"
        elif breakout_up:
            detailed_regime = "BREAKOUT_UP"
        elif breakout_down:
            detailed_regime = "BREAKOUT_DOWN"
        elif expanding_range:
            detailed_regime = "EXPANDING_RANGE"
        elif regime == "range":
            if environment == "high_vol":
                detailed_regime = "RANGE_HIGH_VOL"
            elif environment == "low_vol":
                detailed_regime = "RANGE_LOW_VOL"
            elif wide_range:
                detailed_regime = "RANGE_WIDE"
            else:
                detailed_regime = "RANGE"
        elif squeezed:
            detailed_regime = "CONSOLIDATION"
        elif regime == "trend_up":
            detailed_regime = "TREND_UP"
        elif regime == "trend_down":
            detailed_regime = "TREND_DOWN"
        elif regime == "squeeze":
            detailed_regime = "SQUEEZE"
        elif regime == "choppy_high_vol":
            detailed_regime = "CHOPPY_HIGH_VOL"
        elif regime == "transition":
            detailed_regime = "TRANSITION"
        elif regime == "shock":
            detailed_regime = "SHOCK"

        reasons.append(f"detailed_regime={detailed_regime}")
        reasons.append(f"range_quality={range_quality_score:.2f}")
        reasons.append(f"range_width_expansion={range_width_expansion:.2f}")
        if regime == "range" and range_quality_score < 0.65:
            block_reasons.append("low_range_quality")

        return self._with_transition(
            MarketRegimeSnapshot(
                timestamp=utc_now(),
                regime=regime,
                pattern=pattern,
                environment=environment,
                detailed_regime=detailed_regime,
                confidence=round(confidence, 4),
                trend_direction=trend_direction,
                trend_efficiency=round(trend_efficiency, 4),
                volatility_ratio=round(volatility_ratio, 4),
                range_position=round(range_position, 4) if range_position is not None else None,
                range_low=round(low, 8),
                range_high=round(high, 8),
                range_mid=round((high + low) / 2, 8),
                raw_range_low=round(raw_low, 8),
                raw_range_high=round(raw_high, 8),
                effective_range_low=round(effective_low, 8),
                effective_range_high=round(effective_high, 8),
                range_quality_score=round(range_quality_score, 4),
                range_width_expansion=round(range_width_expansion, 4),
                recent_high_break_count=recent_high_break_count,
                recent_low_break_count=recent_low_break_count,
                risk_flags=risk_flags,
                block_reasons=block_reasons,
                change_point_score=round(change_point_score, 4),
                reason=reasons,
            )
        )

    def _with_transition(self, snapshot: MarketRegimeSnapshot) -> MarketRegimeSnapshot:
        current = snapshot.detailed_regime
        previous = self._previous_regime
        snapshot.previous_regime = previous
        snapshot.current_regime = current
        snapshot.transition_from = previous
        snapshot.transition_to = current
        snapshot.changed = previous is not None and previous != current
        if previous == current:
            self._bars_in_current_regime += 1
        else:
            self._bars_in_current_regime = 1
        snapshot.bars_in_current_regime = self._bars_in_current_regime
        if snapshot.changed:
            snapshot.reason.append(f"transition={previous}->{current}")
        self._previous_regime = current
        return snapshot

    @staticmethod
    def _range_quality_score(
        range_window: pd.DataFrame,
        *,
        latest: pd.Series,
        close: float,
        effective_high: float,
        effective_low: float,
        raw_high: float,
        raw_low: float,
        range_width_expansion: float,
        shock: bool,
        expanding_range: bool,
        recent_high_break_count: int,
        recent_low_break_count: int,
        atr_pct: float,
    ) -> float:
        span = effective_high - effective_low
        if span <= 0 or close <= 0:
            return 0.0
        edge_buffer = max(span * 0.08, close * max(atr_pct, 0.001))
        upper_touches = int((range_window["high"] >= effective_high - edge_buffer).sum())
        lower_touches = int((range_window["low"] <= effective_low + edge_buffer).sum())
        touch_score = min(1.0, min(upper_touches, lower_touches) / 3)

        mid = (effective_high + effective_low) / 2
        above_mid = range_window["close"] > mid
        mid_crosses = int((above_mid != above_mid.shift()).sum())
        midline_score = min(1.0, mid_crosses / 6)

        width_stability_score = max(0.0, 1.0 - min(abs(range_width_expansion - 1.0) / 0.35, 1.0))

        spike_pct = max(raw_high - effective_high, effective_low - raw_low, 0.0) / close
        spike_penalty = min(0.30, spike_pct / max(atr_pct * 2.0, 0.01) * 0.30)
        shock_penalty = 0.20 if shock else 0.0
        expansion_penalty = 0.25 if expanding_range else 0.0
        break_penalty = min(0.30, max(recent_high_break_count, recent_low_break_count) * 0.10)
        close_near_extreme_penalty = 0.10 if float(latest["close"]) >= effective_high or float(latest["close"]) <= effective_low else 0.0

        score = (
            touch_score * 0.35
            + midline_score * 0.25
            + width_stability_score * 0.40
            - spike_penalty
            - shock_penalty
            - expansion_penalty
            - break_penalty
            - close_near_extreme_penalty
        )
        return min(1.0, max(0.0, score))

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
        atr = MarketRegimeClassifier._atr(dataframe, period)
        plus_di = 100 * plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr
        minus_di = 100 * minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr
        denominator = plus_di + minus_di
        dx = 100 * (plus_di - minus_di).abs() / denominator.where(denominator != 0)
        return dx.astype("float64").ewm(alpha=1 / period, min_periods=period, adjust=False).mean().fillna(0)
