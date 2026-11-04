from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from decimal import Decimal

from trading_quant.contracts import (
    BarInput,
    CalculationError,
    FeatureValue,
    ObservationInput,
    QuantConfig,
)
from trading_quant.spec import CORE_FEATURES, EXTRA_FEATURES, INTERVAL_SECONDS


def _wilder(values: list[float], window: int) -> list[float]:
    if len(values) < window:
        return []
    result = [statistics.fmean(values[:window])]
    for value in values[window:]:
        result.append((result[-1] * (window - 1) + value) / window)
    return result


def _zscore(current: float, previous: list[float]) -> float | None:
    deviation = statistics.pstdev(previous)
    return None if deviation == 0 else (current - statistics.fmean(previous)) / deviation


def compute_features(
    bars: tuple[BarInput, ...],
    config: QuantConfig,
    observations: tuple[ObservationInput, ...] = (),
    decision_at: datetime | None = None,
    *,
    benchmark_bars: tuple[BarInput, ...] = (),
) -> tuple[FeatureValue, ...]:
    """Only the last closed bar's feature vector; no library-dependent initialization."""
    if len(bars) < 80:
        raise CalculationError("INSUFFICIENT_HISTORY", "指标基础窗口不足80根")
    last = bars[-1]
    decision_at = decision_at or max(bar.available_at for bar in bars)
    if any(bar.available_at > decision_at for bar in bars):
        raise CalculationError("FUTURE_DATA", "指标包含冻结时点之后的数据")
    observation_cutoff = decision_at if last.timeframe == "1h" else last.close_boundary
    observations = tuple(
        o
        for o in observations
        if o.inst_id == last.inst_id
        and o.available_at <= decision_at
        and o.event_at
        <= (
            decision_at
            if o.kind == "funding" and "published_rate" in o.payload
            else observation_cutoff
        )
    )
    close = [float(bar.close) for bar in bars]
    high = [float(bar.high) for bar in bars]
    low = [float(bar.low) for bar in bars]
    volume = [float(bar.volume_base) for bar in bars]
    returns = [b / a - 1 for a, b in zip(close, close[1:], strict=False)]
    features: dict[str, FeatureValue] = {}

    def put(
        name,
        value=None,
        *,
        unit="ratio",
        window=None,
        quality="OK",
        reason=None,
        source="closed_ohlcv",
        refs=(),
        at=None,
    ):
        if value is None and quality == "OK":
            quality, reason = "DEGENERATE", reason or "分母或总体标准差为零"
        features[name] = FeatureValue(
            name=name,
            value=value,
            unit=unit,
            window=window,
            source=source,
            event_at=at or last.close_boundary,
            available_at=decision_at,
            quality=quality,
            missing_reason=reason,
            observation_ids=refs,
        )

    for k in (1, 3, 5, 10, 20):
        put(f"return_{k}", close[-1] / close[-1 - k] - 1, window=k)
    put("log_return", math.log(close[-1] / close[-2]), window=1)
    tr = [
        max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        for i in range(1, len(bars))
    ]
    plus_dm, minus_dm = [], []
    for i in range(1, len(bars)):
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    atr = _wilder(tr, 14)
    p_dm, m_dm = _wilder(plus_dm, 14), _wilder(minus_dm, 14)
    p_di = [100 * p / t if t else 0.0 for p, t in zip(p_dm, atr, strict=False)]
    m_di = [100 * m / t if t else 0.0 for m, t in zip(m_dm, atr, strict=False)]
    dx = [100 * abs(p - m) / (p + m) if p + m else 0.0 for p, m in zip(p_di, m_di, strict=False)]
    if atr[-1] == 0:
        for name in ("adx", "plus_di", "minus_di"):
            put(name, window=14, reason="价格全平，无方向运动或真实波动")
    else:
        put("adx", _wilder(dx, 14)[-1], unit="index_0_100", window=14)
        put("plus_di", p_di[-1], unit="index_0_100", window=14)
        put("minus_di", m_di[-1], unit="index_0_100", window=14)
    put("atr_pct", atr[-1] / close[-1], window=14)
    y = close[-20:]
    xmean, ymean = 9.5, statistics.fmean(y)
    slope = sum((i - xmean) * (v - ymean) for i, v in enumerate(y)) / sum(
        (i - xmean) ** 2 for i in range(20)
    )
    variance = sum((v - ymean) ** 2 for v in y)
    residual = sum((v - (ymean + slope * (i - xmean))) ** 2 for i, v in enumerate(y))
    put("regression_slope", slope / close[-1], unit="ratio_per_bar", window=20)
    put(
        "regression_r2",
        None if variance == 0 else max(0.0, min(1.0, 1 - residual / variance)),
        window=20,
        reason="常数序列没有可定义的R²" if variance == 0 else None,
    )
    distance = sum(abs(b - a) for a, b in zip(close[-21:-1], close[-20:], strict=False))
    put("efficiency_ratio", abs(close[-1] - close[-21]) / distance if distance else None, window=20)
    price_range = max(high[-14:]) - min(low[-14:])
    put(
        "choppiness_index",
        100 * math.log10(sum(tr[-14:]) / price_range) / math.log10(14)
        if price_range and sum(tr[-14:])
        else None,
        unit="index_0_100",
        window=14,
    )
    deltas = [b - a for a, b in zip(close, close[1:], strict=False)]
    gains = _wilder([max(d, 0) for d in deltas], 14)[-1]
    losses = _wilder([max(-d, 0) for d in deltas], 14)[-1]
    rsi = (
        50.0
        if gains == losses == 0
        else 100.0
        if losses == 0
        else (0.0 if gains == 0 else 100 - 100 / (1 + gains / losses))
    )
    put("rsi", rsi, unit="index_0_100", window=14)
    put("roc", features["return_20"].value, window=20, source="shared_return_20")
    put("momentum_acceleration", returns[-1] - returns[-2], window=2)
    put("bb_width", 4 * statistics.pstdev(close[-20:]) / statistics.fmean(close[-20:]), window=20)
    volatility = [statistics.pstdev(returns[i - 9 : i + 1]) for i in range(9, len(returns))]
    put("volatility_zscore", _zscore(volatility[-1], volatility[-61:-1]), window=60)
    previous_volume = volume[-21:-1]
    average_volume = statistics.fmean(previous_volume)
    put("rvol", volume[-1] / average_volume if average_volume else None, window=20)
    put("volume_zscore", _zscore(volume[-1], previous_volume), window=20)
    base = sum((bar.volume_base for bar in bars[-20:]), Decimal(0))
    turnover = sum((bar.turnover_quote for bar in bars[-20:]), Decimal(0))
    put(
        "vwap_deviation",
        float(last.close / (turnover / base) - 1) if base and turnover else None,
        window=20,
        source="quote_turnover/base_volume",
    )
    hh, ll = max(high[-21:-1]), min(low[-21:-1])
    put("distance_to_high", (hh - close[-1]) / hh, window=20)
    put("distance_to_low", (close[-1] - ll) / ll, window=20)
    breakout = (close[-1] - hh) if close[-1] > hh else (close[-1] - ll) if close[-1] < ll else 0
    put("breakout_strength", breakout / atr[-1] if atr[-1] else None, unit="atr_units", window=20)
    put(
        "close_position",
        (close[-1] - low[-1]) / (high[-1] - low[-1]) if high[-1] != low[-1] else None,
        window=1,
    )

    for name in EXTRA_FEATURES:
        put(
            name,
            quality="MISSING",
            reason="没有满足范围、时间和覆盖契约的外部观测",
            source="external_observation",
        )
    relative_names = ("btc_correlation", "btc_beta", "relative_strength_vs_btc")
    if last.inst_id.startswith("BTC-"):
        for name in relative_names:
            put(name, quality="NOT_APPLICABLE", reason="BTC对自身不提供独立信息", source="BTC")
    elif benchmark_bars:
        # Twenty simultaneous simple returns; never interpolate or shift another clock.
        sample, base = bars[-21:], benchmark_bars[-21:]
        valid = (
            len(base) == 21
            and all(
                b.inst_id == "BTC-USDT-SWAP"
                and b.timeframe == last.timeframe
                and b.close_boundary == a.close_boundary
                and b.available_at <= decision_at
                and b.close_boundary <= decision_at
                and b.close > 0
                for a, b in zip(sample, base, strict=False)
            )
            and all(
                (b.close_boundary - a.close_boundary).total_seconds()
                == INTERVAL_SECONDS[last.timeframe]
                for a, b in zip(base, base[1:], strict=False)
            )
        )
        if valid:
            btc = [b.close for b in base]
            x = [float(b / a - 1) for a, b in zip(btc, btc[1:], strict=False)]
            y = [float(b.close / a.close - 1) for a, b in zip(sample, sample[1:], strict=False)]
            vx, vy = statistics.pvariance(x), statistics.pvariance(y)
            # Return noise below 1e-12 cannot provide a meaningful variance denominator.
            # Decimal ratios also avoid cancellation from converting prices before division.
            vx = vx if vx > 1e-24 else 0.0
            vy = vy if vy > 1e-24 else 0.0
            cov = statistics.fmean(
                (a - statistics.fmean(x)) * (b - statistics.fmean(y))
                for a, b in zip(x, y, strict=True)
            )
            put(
                "btc_correlation",
                max(-1.0, min(1.0, cov / math.sqrt(vx * vy))) if vx and vy else None,
                source="BTC_ALIGNED_RETURNS_WEAK",
                window=20,
            )
            put("btc_beta", cov / vx if vx else None, source="BTC_ALIGNED_RETURNS_WEAK", window=20)
            put(
                "relative_strength_vs_btc",
                float((sample[-1].close / sample[0].close) / (btc[-1] / btc[0]) - 1),
                source="BTC_ALIGNED_RETURNS_WEAK",
                window=20,
            )
        else:
            for name in relative_names:
                put(
                    name,
                    quality="MISSING",
                    reason="BTC基准需要21根同周期同边界已收盘K线，且在决策前可用",
                    source="BTC_ALIGNED_RETURNS_WEAK",
                    window=20,
                )

    def matches(kind):
        return sorted(
            (
                o
                for o in observations
                if o.kind == kind
                and o.quality == "OK"
                and o.payload.get("timeframe") in {None, last.timeframe}
            ),
            key=lambda o: (o.event_at, o.revision_no),
        )

    def decimal_value(o, key):
        result = Decimal(str(o.payload[key]))
        if not result.is_finite():
            raise ValueError("外部观测非有限数")
        return result

    def external(name, value, obs, *, window=None, quality="OK", reason=None, unit="ratio"):
        put(
            name,
            value,
            source=obs[-1].source,
            refs=tuple(o.id for o in obs),
            window=window,
            quality=quality,
            reason=reason,
            unit=unit,
        )

    # An observed taker interval must be contiguous and match the complete 20-bar window.
    taker = matches("taker_volume")
    if taker:
        start, end = (
            last.close_boundary - timedelta(seconds=20 * INTERVAL_SECONDS[last.timeframe]),
            last.close_boundary,
        )
        selected = [
            o
            for o in taker
            if o.coverage_start is not None
            and o.coverage_start >= start
            and o.coverage_end <= end
            and o.unit == "base_quantity"
        ]
        selected.sort(key=lambda o: o.coverage_start)
        continuous = (
            bool(selected)
            and selected[0].coverage_start == start
            and (selected[-1].coverage_end == end)
            and all(
                a.coverage_end == b.coverage_start
                for a, b in zip(selected, selected[1:], strict=False)
            )
        )
        if continuous:
            buy = sum((decimal_value(o, "buy") for o in selected), Decimal(0))
            sell = sum((decimal_value(o, "sell") for o in selected), Decimal(0))
            if buy < 0 or sell < 0:
                raise CalculationError("INVALID_OBSERVATION", "主动成交量不可为负")
            # Where each statistic covers exactly one calculation candle, compare source
            # totals. Never invent the buy/sell split from an OHLCV volume discrepancy.
            candle_volumes = {b.close_boundary: b.volume_base for b in bars[-20:]}
            comparable = len(selected) == 20 and all(
                (o.coverage_end - o.coverage_start).total_seconds()
                == INTERVAL_SECONDS[last.timeframe]
                and o.coverage_end in candle_volumes
                for o in selected
            )
            mismatched = comparable and any(
                abs(
                    decimal_value(o, "buy")
                    + decimal_value(o, "sell")
                    - candle_volumes[o.coverage_end]
                )
                > max(
                    Decimal("0.00000001"), abs(candle_volumes[o.coverage_end]) * Decimal("0.000001")
                )
                for o in selected
            )
            quality = "PARTIAL_COVERAGE" if mismatched else "OK"
            reason = (
                "主动成交统计总量与同区间K线成交量不一致；保留API统计值，不能视为完整成交差额"
                if mismatched
                else None
            )
            external(
                "cvd",
                buy - sell,
                selected,
                unit="base_quantity",
                window=20,
                quality=quality,
                reason=reason,
            )
            external(
                "taker_buy_sell_ratio",
                float(buy / sell) if sell else None,
                selected,
                window=20,
                quality=quality if sell else "DEGENERATE",
                reason=reason if sell else "主动卖出量为零，买卖比无定义",
            )
        else:
            for name in ("cvd", "taker_buy_sell_ratio"):
                put(
                    name,
                    quality="PARTIAL_COVERAGE",
                    reason="主动买卖观测区间不完整或单位不一致",
                    source="external_observation",
                )
    oi = matches("oi")
    expected = (
        last.close_boundary - timedelta(seconds=INTERVAL_SECONDS[last.timeframe]),
        last.close_boundary,
    )
    paired = [
        next((o for o in reversed(oi) if o.event_at == t and o.unit == "contracts"), None)
        for t in expected
    ]
    if all(paired):
        values = [decimal_value(o, "quantity") for o in paired]
        if min(values) < 0:
            raise CalculationError("INVALID_OBSERVATION", "OI数量不可为负")
        external(
            "oi_change", float(values[1] / values[0] - 1) if values[0] else None, paired, window=1
        )
    funding = matches("funding")
    if funding:
        published = [o for o in funding if "published_rate" in o.payload]
        if published:
            latest = published[-1]
            put(
                "funding_rate",
                decimal_value(latest, "published_rate"),
                source=latest.source,
                refs=(latest.id,),
                at=latest.event_at,
            )
        settled_by_time = {}
        for o in funding:
            if o.payload.get("event_type") == "settled" and "realized_rate" in o.payload:
                settled_by_time[o.event_at] = o
        settled = [settled_by_time[t] for t in sorted(settled_by_time)]
        if len(settled) >= 31:
            sample = settled[-31:]
            values = [float(decimal_value(o, "realized_rate")) for o in sample]
            external("funding_zscore", _zscore(values[-1], values[:-1]), sample, window=30)
        else:
            put(
                "funding_zscore",
                quality="INSUFFICIENT_HISTORY",
                source="funding_events",
                reason="需要31个独立已结算资金费事件",
                window=30,
            )
    ratios = matches("long_short_ratio")
    if ratios:
        o = ratios[-1]
        if o.payload.get("scope") == "contract_accounts" and o.unit == "ratio":
            external("long_short_ratio", decimal_value(o, "ratio"), [o])
    liquidations = sorted(
        (
            o
            for o in observations
            if o.kind == "liquidation"
            and o.payload.get("timeframe") in {None, last.timeframe}
            and o.payload.get("coverage") == "public_sample"
        ),
        key=lambda o: (o.event_at, o.revision_no),
    )
    if liquidations:
        o = liquidations[-1]
        if o.coverage_end == last.close_boundary and o.payload.get("coverage") == "public_sample":
            if o.quality != "OK":
                put(
                    "liquidation_imbalance",
                    quality="MISSING",
                    source=o.source,
                    reason=o.payload.get("missing_reason") or "公开清算采样不可用",
                    refs=(o.id,),
                )
                o = None
        else:
            o = None
        if o is not None:
            shorts, longs = decimal_value(o, "short_quote"), decimal_value(o, "long_quote")
            total = shorts + longs
            if total > 0:
                external(
                    "liquidation_imbalance",
                    float((shorts - longs) / total),
                    [o],
                    quality="PARTIAL_COVERAGE",
                    reason="OKX公开采样，不是全市场强平总量"
                    + (
                        f"；连接覆盖含{len(o.payload['gaps'])}处缺口"
                        if o.payload.get("gaps")
                        else ""
                    ),
                )
    baskets = matches("market_breadth")
    if baskets and config.market_basket:
        o = baskets[-1]
        if tuple(o.payload.get("basket", [])) == config.market_basket:
            covered = int(o.payload["covered"])
            above = int(o.payload["above_sma20"])
            if not 0 <= above <= covered <= len(config.market_basket):
                raise CalculationError("INVALID_OBSERVATION", "市场篮子覆盖数量错误")
            if Decimal(covered) / len(config.market_basket) >= config.market_basket_min_coverage:
                partial = covered < len(config.market_basket)
                external(
                    "market_breadth",
                    above / covered,
                    [o],
                    window=20,
                    quality="PARTIAL_COVERAGE" if partial else "OK",
                    reason=f"市场篮子有效成员{covered}/{len(config.market_basket)}"
                    if partial
                    else None,
                )
            else:
                put(
                    "market_breadth",
                    quality="PARTIAL_COVERAGE",
                    source=o.source,
                    reason=f"市场篮子有效成员{covered}/{len(config.market_basket)}，低于覆盖门槛",
                    window=20,
                )
    missing = [name for name in config.required_features if features[name].quality != "OK"]
    if missing:
        raise CalculationError("REQUIRED_FEATURE_MISSING", "必需特征不可用: " + ", ".join(missing))
    return tuple(
        features[name]
        for name in CORE_FEATURES + EXTRA_FEATURES
        if name in config.required_features + config.optional_features
    )
