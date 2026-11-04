"""Causal structure candidates. Geometric evidence is not an entry decision."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from trading_quant.contracts import BarInput, CalculationError
from trading_quant.spec import INTERVAL_SECONDS

STRUCTURE_VERSION = "confirmed_pivots_v1"


@dataclass(frozen=True)
class Pivot:
    index: int
    kind: str
    at: datetime
    confirmed_at: datetime
    price: Decimal


@dataclass(frozen=True)
class Boundary:
    at: datetime
    price: Decimal
    slope_per_second: Decimal = Decimal(0)

    def value_at(self, at: datetime) -> Decimal:
        return self.price + self.slope_per_second * Decimal(str((at - self.at).total_seconds()))


@dataclass(frozen=True)
class StructureCandidate:
    id: str
    kind: str
    stage: str
    timeframe: str
    inst_id: str
    pivots: tuple[Pivot, ...]
    upper: Boundary
    lower: Boundary
    tolerance: Decimal
    atr: Decimal
    measured_height: Decimal
    observed_at: datetime
    version: str = STRUCTURE_VERSION


def _validate(bars: tuple[BarInput, ...], as_of: datetime):
    if as_of.tzinfo is None:
        raise CalculationError("INVALID_TIME", "结构计算时点必须带时区")
    if len(bars) < 80:
        raise CalculationError("INSUFFICIENT_HISTORY", "结构计算需要80根已收盘K线")
    if len({(b.inst_id, b.timeframe) for b in bars}) != 1:
        raise CalculationError("MIXED_MARKET", "结构输入不能混合标的或周期")
    step = INTERVAL_SECONDS[bars[-1].timeframe]
    for i, bar in enumerate(bars):
        if bar.available_at > as_of or bar.close_boundary > as_of:
            raise CalculationError("FUTURE_DATA", "结构包含尚不可用K线")
        if any(not v.is_finite() or v <= 0 for v in (bar.high, bar.low, bar.close)):
            raise CalculationError("INVALID_PRICE", "结构输入价格必须为有限正数")
        if not bar.low <= bar.close <= bar.high:
            raise CalculationError("INVALID_PRICE", "结构输入OHLC关系错误")
        if i and (bar.close_boundary - bars[i - 1].close_boundary).total_seconds() != step:
            raise CalculationError("BAR_GAP", "结构输入K线不连续")


def confirmed_pivots(bars: tuple[BarInput, ...], as_of: datetime) -> tuple[Pivot, ...]:
    """Two closed bars on BOTH sides; ties use the first extremum in a plateau."""
    _validate(bars, as_of)
    result = []
    for i in range(2, len(bars) - 2):
        left, right, bar = bars[i - 2 : i], bars[i + 1 : i + 3], bars[i]
        confirmed = max(b.available_at for b in bars[i - 2 : i + 3])
        for kind, attr, better, no_worse in (
            ("HIGH", "high", lambda a, b: a > b, lambda a, b: a >= b),
            ("LOW", "low", lambda a, b: a < b, lambda a, b: a <= b),
        ):
            value = getattr(bar, attr)
            if all(better(value, getattr(b, attr)) for b in left) and all(
                no_worse(value, getattr(b, attr)) for b in right
            ):
                result.append(Pivot(i, kind, bar.close_boundary, confirmed, value))
    return tuple(result)


def _line(a: Pivot, b: Pivot) -> Boundary:
    seconds = Decimal(str((b.at - a.at).total_seconds()))
    return Boundary(b.at, b.price, (b.price - a.price) / seconds)


def _stage(bars, upper, lower, tolerance, kind):
    """Stage describes observed closed bars; it never forces a trade."""
    recent = bars[-12:]
    above = [b.close > upper.value_at(b.close_boundary) + tolerance for b in recent]
    below = [b.close < lower.value_at(b.close_boundary) - tolerance for b in recent]
    if kind == "W_BOTTOM" and below[-1]:
        return "INVALIDATED"
    if above[-1]:
        touched = recent[-1].low <= upper.value_at(recent[-1].close_boundary) + tolerance
        if any(above[:-1]) and touched:
            return "RETEST_UP"
        return "CONFIRMED_UP" if above[-2] else "BREAKOUT_UP"
    if below[-1]:
        touched = recent[-1].high >= lower.value_at(recent[-1].close_boundary) - tolerance
        if any(below[:-1]) and touched:
            return "RETEST_DOWN"
        return "CONFIRMED_DOWN" if below[-2] else "BREAKOUT_DOWN"
    if above[-2] or below[-2]:
        return "FAILED_BREAKOUT"
    return "FORMING"


def detect_structures(
    bars: tuple[BarInput, ...],
    as_of: datetime,
    *,
    tolerance_atr: Decimal = Decimal("0.25"),
) -> tuple[StructureCandidate, ...]:
    """Return independently supported candidates; an empty tuple means UNKNOWN.

    Geometry uses confirmed pivots from the last 60 bars of the 80-bar calculation
    window. Candidate ids cover frozen evidence, not just the pattern's name.
    """
    if not tolerance_atr.is_finite() or not 0 < tolerance_atr <= 1:
        raise CalculationError("INVALID_CONFIG", "结构容差必须在(0,1]ATR内")
    pivots = confirmed_pivots(bars, as_of)
    ranges = [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
        for a, b in zip(bars, bars[1:], strict=False)
    ]
    atr = sum(ranges[:14]) / 14
    for value in ranges[14:]:
        atr = (13 * atr + value) / 14
    if atr <= 0:
        return ()
    tolerance = atr * tolerance_atr
    recent = tuple(p for p in pivots if p.index >= len(bars) - 60)
    highs, lows = [p for p in recent if p.kind == "HIGH"], [p for p in recent if p.kind == "LOW"]
    result = []

    def add(kind, points, upper, lower):
        at = bars[-1].close_boundary
        width = upper.value_at(at) - lower.value_at(at)
        if width <= 2 * tolerance or lower.value_at(at) <= 0:
            return
        stage = _stage(bars, upper, lower, tolerance, kind)
        facts = dict(
            kind=kind,
            stage=stage,
            timeframe=bars[-1].timeframe,
            inst_id=bars[-1].inst_id,
            pivots=tuple(points),
            upper=upper,
            lower=lower,
            tolerance=tolerance,
            atr=atr,
            measured_height=width,
            observed_at=at,
            version=STRUCTURE_VERSION,
        )
        encoded = json.dumps(
            facts,
            sort_keys=True,
            default=lambda o: asdict(o) if hasattr(o, "__dataclass_fields__") else str(o),
        )
        result.append(StructureCandidate(id=hashlib.sha256(encoded.encode()).hexdigest(), **facts))

    if len(highs) >= 2 and len(lows) >= 2:
        h1, h2, l1, l2 = highs[-2], highs[-1], lows[-2], lows[-1]
        upper, lower = _line(h1, h2), _line(l1, l2)
        flat_high = abs(h2.price - h1.price) <= tolerance
        flat_low = abs(l2.price - l1.price) <= tolerance
        if flat_high and flat_low:
            add(
                "BOX",
                (h1, h2, l1, l2),
                Boundary(h2.at, max(h1.price, h2.price)),
                Boundary(l2.at, min(l1.price, l2.price)),
            )
        if flat_high and l2.price > l1.price + tolerance:
            add(
                "ASCENDING_TRIANGLE",
                (h1, h2, l1, l2),
                Boundary(h2.at, max(h1.price, h2.price)),
                lower,
            )
        if h2.price < h1.price - tolerance and l2.price > l1.price + tolerance:
            add("CONVERGING_TRIANGLE", (h1, h2, l1, l2), upper, lower)
    if len(lows) >= 2:
        a, b = lows[-2:]
        necks = [h for h in highs if a.index < h.index < b.index]
        if 5 <= b.index - a.index <= 40 and abs(a.price - b.price) <= 2 * tolerance and necks:
            neck = max(necks, key=lambda h: h.price)
            if neck.price - max(a.price, b.price) >= atr:
                add(
                    "W_BOTTOM",
                    (a, neck, b),
                    Boundary(neck.at, neck.price),
                    Boundary(b.at, min(a.price, b.price)),
                )
    return tuple(result)
