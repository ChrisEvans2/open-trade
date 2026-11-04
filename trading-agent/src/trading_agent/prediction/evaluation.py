"""Pure replay/incremental verification over complete, contiguous minute candles.

Forecast eligibility can end before its probability observation window. Direction
and excursions still collect through the ORIGINAL expiry; lifecycle never revives.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field

from trading_agent.config import StrictModel
from trading_agent.domain.errors import DomainError
from trading_agent.domain.time import boundary, utc
from trading_agent.market.models import Bar
from trading_agent.prediction.contracts import PriceCondition, PublishedPrediction


class ScenarioState(StrictModel):
    triggered_at: datetime | None = None
    terminal_at: datetime | None = None
    outcome: Literal["WAITING", "ACTIVE", "ACHIEVED", "INVALIDATED", "AMBIGUOUS", "EXPIRED"] = (
        "WAITING"
    )
    trigger_streak: int = 0
    invalidation_streak: int = 0
    highest: Decimal | None = None
    lowest: Decimal | None = None
    target_distance: Decimal | None = None
    boundary_counts: dict[str, dict[str, int]] = Field(default_factory=dict)


class EvaluationState(StrictModel):
    forecast_id: UUID
    verified_until: datetime
    lifecycle: Literal["ACTIVE", "ACHIEVED", "INVALIDATED", "AMBIGUOUS", "EXPIRED"] = "ACTIVE"
    ended_at: datetime | None = None
    first_direction: Literal["UP", "DOWN", "NONE", "AMBIGUOUS"] | None = None
    first_direction_at: datetime | None = None
    highest: Decimal | None = None
    lowest: Decimal | None = None
    scenarios: dict[str, ScenarioState]
    observation_complete: bool = False


class EvaluationEvent(StrictModel):
    kind: str
    at: datetime
    direction: str | None = None
    bar_id: UUID | None = None
    details: dict = Field(default_factory=dict)


class EvaluationResult(StrictModel):
    state: EvaluationState
    events: tuple[EvaluationEvent, ...]
    data_quality: Literal["OK", "AWAITING_DATA"]
    missing_at: datetime | None = None


def raw_condition(condition: PriceCondition, bar: Bar) -> bool:
    line, tol = condition.line.value_at(bar.close_boundary), condition.tolerance
    touched = bar.low <= line + tol and bar.high >= line - tol
    return {
        "CLOSE_ABOVE": bar.close > line + tol,
        "CLOSE_BELOW": bar.close < line - tol,
        "TOUCH": touched,
        "SUPPORT": touched and bar.close > line + tol,
        "RESISTANCE": touched and bar.close < line - tol,
    }[condition.behavior]


def condition_matches(condition: PriceCondition, bars: tuple[Bar, ...]) -> bool:
    """Used also for position-bound structural risk after a forecast has expired."""
    tail = bars[-condition.confirmation_bars :]
    return (
        len(tail) == condition.confirmation_bars
        and all(b.timeframe == "1m" for b in tail)
        and len({b.inst_id for b in tail}) == 1
        and all(a.close_boundary == b.open_at for a, b in zip(tail, tail[1:], strict=False))
        and all(raw_condition(condition, b) for b in tail)
    )


def _boundaries(scenario, bar, counts):
    counts = {k: dict(v) for k, v in counts.items()}
    for index, condition in enumerate(scenario.boundary_expectations):
        item = counts.setdefault(str(index), {"tested": 0, "held": 0, "failed": 0, "streak": 0})
        line, tol = condition.line.value_at(bar.close_boundary), condition.tolerance
        touched = bar.low <= line + tol and bar.high >= line - tol
        match = raw_condition(condition, bar)
        item["streak"] = item["streak"] + 1 if match else 0
        if touched or match:
            item["tested"] += 1
        if item["streak"] == condition.confirmation_bars:
            item["held"] += 1
        failed = (condition.behavior in {"SUPPORT", "CLOSE_ABOVE"} and bar.close < line - tol) or (
            condition.behavior in {"RESISTANCE", "CLOSE_BELOW"} and bar.close > line + tol
        )
        if failed:
            item["failed"] += 1
    return counts


def evaluate(
    prediction: PublishedPrediction,
    bars: tuple[Bar, ...],
    as_of: datetime,
    previous: EvaluationState | None = None,
) -> EvaluationResult:
    """Never fill a gap, use an unclosed candle, or score price action before publication."""
    as_of = utc(as_of)
    if as_of < prediction.published_at:
        raise DomainError("FUTURE_DATA", "验证时点不能早于预测发布")
    state = previous or EvaluationState(
        forecast_id=prediction.id,
        verified_until=prediction.observation_start,
        scenarios={s.direction: ScenarioState() for s in prediction.report.scenarios},
    )
    if state.forecast_id != prediction.id or set(state.scenarios) != {
        s.direction for s in prediction.report.scenarios
    }:
        raise DomainError("EVALUATION_SCOPE", "验证状态不属于当前预测")
    if (
        state.verified_until.tzinfo is None
        or state.verified_until.timestamp() % 60
        or not prediction.observation_start
        <= state.verified_until
        <= boundary(prediction.expires_at, "1m")
    ):
        raise DomainError("EVALUATION_CURSOR", "验证游标必须在原窗口内且对齐分钟")
    if any(b.timeframe != "1m" or b.inst_id != prediction.inst_id for b in bars):
        raise DomainError("EVALUATION_SCOPE", "验证行情必须是当前合约的分钟K线")
    if any(b.available_at > as_of or b.close_boundary > as_of for b in bars):
        raise DomainError("FUTURE_DATA", "验证不能使用尚不可用行情")
    if any(a.open_at >= b.open_at for a, b in zip(bars, bars[1:], strict=False)):
        raise DomainError("BAR_ORDER", "验证K线必须去重并按时间升序排列")
    stop = min(boundary(as_of, "1m"), boundary(prediction.expires_at, "1m"))
    events = []
    cursor = state.verified_until
    for bar in bars:
        if bar.open_at < cursor or bar.close_boundary > stop:
            continue
        if bar.open_at != cursor:
            break
        next_scenarios, triggered = {}, []
        for scenario in prediction.report.scenarios:
            current = state.scenarios[scenario.direction]
            changes = dict(
                trigger_streak=current.trigger_streak + 1
                if raw_condition(scenario.trigger, bar)
                else 0,
                invalidation_streak=current.invalidation_streak + 1
                if raw_condition(scenario.invalidation, bar)
                else 0,
            )
            trigger = changes["trigger_streak"] >= scenario.trigger.confirmation_bars
            invalid = changes["invalidation_streak"] >= scenario.invalidation.confirmation_bars
            # Direction is observed even after this scenario became ineligible. That cannot
            # resurrect its lifecycle or turn an invalidated target prediction into success.
            if trigger:
                triggered.append(scenario.direction)
            if current.outcome in {"WAITING", "ACTIVE"}:
                if current.outcome == "WAITING" and trigger and not invalid:
                    changes.update(triggered_at=bar.close_boundary, outcome="ACTIVE")
                    events.append(
                        EvaluationEvent(
                            kind="TRIGGERED",
                            direction=scenario.direction,
                            at=bar.close_boundary,
                            bar_id=bar.id,
                        )
                    )
                target = bar.high >= scenario.target.low and bar.low <= scenario.target.high
                # A CLOSE/SUPPORT trigger is confirmed at minute end. Intraminute highs/lows
                # of that SAME candle cannot be evidence of a subsequent target touch.
                target = target and current.triggered_at is not None
                outcome = (
                    "AMBIGUOUS"
                    if invalid and target
                    else ("INVALIDATED" if invalid else "ACHIEVED" if target else None)
                )
                if outcome:
                    changes.update(outcome=outcome, terminal_at=bar.close_boundary)
                    events.append(
                        EvaluationEvent(
                            kind=outcome,
                            direction=scenario.direction,
                            at=bar.close_boundary,
                            bar_id=bar.id,
                        )
                    )
                if current.triggered_at is not None:
                    highest = max(current.highest or bar.high, bar.high)
                    lowest = min(current.lowest or bar.low, bar.low)
                    distance = max(
                        Decimal(0), scenario.target.low - bar.high, bar.low - scenario.target.high
                    )
                    changes.update(
                        highest=highest,
                        lowest=lowest,
                        target_distance=min(current.target_distance, distance)
                        if current.target_distance is not None
                        else distance,
                        boundary_counts=_boundaries(scenario, bar, current.boundary_counts),
                    )
            next_scenarios[scenario.direction] = current.model_copy(update=changes)
        update = dict(
            scenarios=next_scenarios,
            verified_until=bar.close_boundary,
            highest=max(state.highest or bar.high, bar.high),
            lowest=min(state.lowest or bar.low, bar.low),
        )
        if state.first_direction is None and triggered:
            direction = triggered[0] if len(triggered) == 1 else "AMBIGUOUS"
            update.update(first_direction=direction, first_direction_at=bar.close_boundary)
            events.append(
                EvaluationEvent(
                    kind="DIRECTION", direction=direction, at=bar.close_boundary, bar_id=bar.id
                )
            )
        primary = prediction.report.primary_direction or update.get(
            "first_direction", state.first_direction
        )
        if state.lifecycle == "ACTIVE" and primary in next_scenarios:
            chosen = next_scenarios[primary]
            if chosen.outcome in {"ACHIEVED", "INVALIDATED", "AMBIGUOUS"}:
                update.update(lifecycle=chosen.outcome, ended_at=bar.close_boundary)
                events.append(
                    EvaluationEvent(
                        kind="LIFECYCLE_ENDED",
                        at=bar.close_boundary,
                        details={"outcome": chosen.outcome},
                    )
                )
        state = state.model_copy(update=update)
        cursor = bar.close_boundary
    complete = cursor >= boundary(prediction.expires_at, "1m") and as_of >= prediction.expires_at
    if complete and not state.observation_complete:
        updates = dict(observation_complete=True)
        if state.first_direction is None:
            updates.update(first_direction="NONE", first_direction_at=prediction.expires_at)
        if state.lifecycle == "ACTIVE":
            updates.update(lifecycle="EXPIRED", ended_at=prediction.expires_at)
            events.append(
                EvaluationEvent(
                    kind="LIFECYCLE_ENDED", at=prediction.expires_at, details={"outcome": "EXPIRED"}
                )
            )
        scenarios = {}
        for direction, current in state.scenarios.items():
            if current.outcome in {"ACTIVE", "WAITING"}:
                current = current.model_copy(
                    update={"outcome": "EXPIRED", "terminal_at": prediction.expires_at}
                )
            scenarios[direction] = current
        updates["scenarios"] = scenarios
        state = state.model_copy(update=updates)
        events.append(EvaluationEvent(kind="OBSERVATION_COMPLETE", at=prediction.expires_at))
    missing = cursor if cursor < stop else None
    return EvaluationResult(
        state=state,
        events=tuple(events),
        data_quality="AWAITING_DATA" if missing else "OK",
        missing_at=missing,
    )


def scores(prediction: PublishedPrediction, state: EvaluationState) -> dict:
    probabilities = {
        "UP": prediction.report.p_up,
        "DOWN": prediction.report.p_down,
        "NONE": prediction.report.p_none,
    }
    label = state.first_direction
    direction_score = (
        sum((p - Decimal(label == k)) ** 2 for k, p in probabilities.items())
        if label in probabilities
        else None
    )
    result = {}
    for scenario in prediction.report.scenarios:
        current = state.scenarios[scenario.direction]
        known = (
            current.outcome in {"ACHIEVED", "INVALIDATED", "EXPIRED"}
            and current.triggered_at is not None
        )
        result[scenario.direction] = {
            "outcome": current.outcome,
            "triggered_at": current.triggered_at,
            "terminal_at": current.terminal_at,
            "target_brier": (scenario.target_probability - Decimal(current.outcome == "ACHIEVED"))
            ** 2
            if known
            else None,
            "elapsed_seconds": (current.terminal_at - prediction.published_at).total_seconds()
            if current.terminal_at
            else None,
            "target_distance": current.target_distance,
            "highest": current.highest,
            "lowest": current.lowest,
            "boundary_counts": current.boundary_counts,
        }
    return {
        "direction_label": label,
        "direction_brier": direction_score,
        "scenarios": result,
        "highest": state.highest,
        "lowest": state.lowest,
        "observation_complete": state.observation_complete,
    }


def holding_action(*, has_position: bool, lifecycle: str, reversed_direction: bool) -> str:
    """Expiry alone NEVER requests a close; reversal requires parent synthesis."""
    if has_position and (lifecycle == "INVALIDATED" or reversed_direction):
        return "CLOSE"
    return "HOLD"
