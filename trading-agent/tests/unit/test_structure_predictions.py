from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from trading_quant import CalculationError
from trading_quant.structures import confirmed_pivots, detect_structures

from trading_agent.domain.errors import DomainError
from trading_agent.market.models import Bar
from trading_agent.prediction.contracts import (
    ParentDecision,
    PredictionReport,
    PriceCondition,
    PriceLine,
    PriceRange,
    PublishedPrediction,
    Scenario,
)
from trading_agent.prediction.evaluation import evaluate, holding_action, scores

START = datetime(2026, 10, 11, 8, tzinfo=UTC)


def structure_bars(highs=None, lows=None, last_close=105):
    highs, lows = highs or {}, lows or {}
    return tuple(
        SimpleNamespace(
            inst_id="BTC-USDT-SWAP",
            timeframe="1h",
            high=Decimal(highs.get(i, max(106, last_close) if i == 79 else 106)),
            low=Decimal(lows.get(i, min(104, last_close) if i == 79 else 104)),
            close=Decimal(last_close if i == 79 else 105),
            close_boundary=START - timedelta(hours=79 - i),
            available_at=START,
        )
        for i in range(80)
    )


def condition(price, behavior="CLOSE_ABOVE", **kwargs):
    return PriceCondition(
        line=PriceLine(at=START, price=price), behavior=behavior, tolerance=0, **kwargs
    )


def prediction(*, invalidation=None, primary="UP"):
    report = PredictionReport(
        symbol="BTCUSDT",
        timeframe="1h",
        structure="UNKNOWN",
        stage="UNKNOWN",
        horizon_rule_id=uuid4(),
        horizon_bars=2,
        primary_direction=primary,
        p_up="0.7",
        p_down=0,
        p_none="0.3",
        reason="fixture upward scenario",
        scenarios=(
            Scenario(
                direction="UP",
                trigger=condition(101),
                invalidation=invalidation or condition(95, "CLOSE_BELOW"),
                target=PriceRange(low=104, high=105),
                target_probability="0.6",
                boundary_expectations=(condition(101, "SUPPORT"),),
            ),
        ),
    )
    return PublishedPrediction(
        id=uuid4(),
        engine_id=uuid4(),
        snapshot_id=uuid4(),
        inst_id="BTC-USDT-SWAP",
        role="TIMEFRAME",
        published_at=START,
        observation_start=START,
        expires_at=START + timedelta(hours=2),
        reference_price=100,
        report=report,
    )


def minute(index, *, close=102, high=None, low=None, inst_id="BTC-USDT-SWAP"):
    close = Decimal(str(close))
    at = START + timedelta(minutes=index)
    return Bar(
        inst_id=inst_id,
        timeframe="1m",
        open_at=at,
        close_boundary=at + timedelta(minutes=1),
        available_at=at + timedelta(minutes=1),
        open=close,
        close=close,
        high=Decimal(str(high)) if high is not None else close,
        low=Decimal(str(low)) if low is not None else close,
        volume_base=1,
        volume_contracts=1,
        turnover_quote=close,
    )


def test_triangle_preserves_sloped_support_and_causal_pivots():
    bars = structure_bars({55: 110, 65: 110}, {58: 95, 68: 99})
    triangle = next(c for c in detect_structures(bars, START) if c.kind == "ASCENDING_TRIANGLE")
    assert triangle.lower.value_at(START + timedelta(hours=1)) > triangle.lower.value_at(START)
    assert triangle.upper.value_at(START + timedelta(hours=1)) == 110
    assert triangle.stage == "FORMING"
    assert all(p.index <= 77 for p in triangle.pivots)
    # An unconfirmed newest trough cannot become a pivot in a prediction.
    latest = structure_bars({55: 110, 65: 110}, {58: 95, 68: 99, 79: 90})
    assert not any(p.index == 79 for p in confirmed_pivots(latest, START))


def test_w_bottom_stages_use_neckline_not_pattern_name_for_entry():
    bars = structure_bars({60: 110}, {55: 95, 65: 95})
    forming = next(c for c in detect_structures(bars, START) if c.kind == "W_BOTTOM")
    broken = structure_bars({60: 110}, {55: 95, 65: 95}, last_close=114)
    breakout = next(c for c in detect_structures(broken, START) if c.kind == "W_BOTTOM")
    failed = structure_bars({60: 110}, {55: 95, 65: 95}, last_close=90)
    invalid = next(c for c in detect_structures(failed, START) if c.kind == "W_BOTTOM")
    assert (forming.stage, breakout.stage, invalid.stage) == (
        "FORMING",
        "BREAKOUT_UP",
        "INVALIDATED",
    )
    retest_bars = list(structure_bars({60: 110}, {55: 95, 65: 95}, last_close=113))
    retest_bars[-2] = SimpleNamespace(
        **{
            **vars(retest_bars[-2]),
            "close": Decimal(114),
            "high": Decimal(115),
            "low": Decimal(112),
        }
    )
    retest_bars[-1] = SimpleNamespace(**{**vars(retest_bars[-1]), "low": Decimal(110)})
    retest = next(c for c in detect_structures(tuple(retest_bars), START) if c.kind == "W_BOTTOM")
    assert retest.stage == "RETEST_UP"
    assert forming.upper.price == 110 and forming.lower.price == 95
    assert asdict(forming)["version"] == "confirmed_pivots_v1"


def test_box_and_converging_triangle_and_unknown():
    box = structure_bars({55: 110, 65: 110}, {58: 95, 68: 95})
    assert "BOX" in {c.kind for c in detect_structures(box, START)}
    triangle = structure_bars({55: 115, 65: 112}, {58: 95, 68: 99})
    assert "CONVERGING_TRIANGLE" in {c.kind for c in detect_structures(triangle, START)}
    crossed = structure_bars({55: 115, 65: 110}, {58: 95, 68: 99})
    assert "CONVERGING_TRIANGLE" not in {c.kind for c in detect_structures(crossed, START)}
    assert detect_structures(structure_bars(), START) == ()


def test_structure_rejects_future_missing_and_mixed_market():
    bars = structure_bars()
    with pytest.raises(CalculationError, match="尚不可用"):
        detect_structures(bars, START - timedelta(seconds=1))
    with pytest.raises(CalculationError, match="80"):
        detect_structures(bars[:-1], START)
    bars[-1].inst_id = "ETH-USDT-SWAP"
    with pytest.raises(CalculationError, match="标的"):
        detect_structures(bars, START)


def test_pre_trigger_high_does_not_count_as_future_target():
    p = prediction()
    result = evaluate(p, (minute(0, high=110),), START + timedelta(minutes=1))
    assert result.state.scenarios["UP"].outcome == "ACTIVE"
    assert result.state.first_direction == "UP"
    result = evaluate(
        p, (minute(1, close=104, high=105),), START + timedelta(minutes=2), result.state
    )
    assert result.state.lifecycle == "ACHIEVED"
    assert scores(p, result.state)["scenarios"]["UP"]["target_brier"] == Decimal("0.16")


def test_invalidation_cannot_be_rewritten_by_later_target():
    p = prediction()
    bars = (minute(0, close=94), minute(1, close=102), minute(2, close=105))
    state = evaluate(p, bars, START + timedelta(minutes=3)).state
    assert state.lifecycle == "INVALIDATED"
    assert state.scenarios["UP"].outcome == "INVALIDATED"
    assert state.scenarios["UP"].triggered_at is None
    assert state.first_direction == "UP"  # Separately scored direction is allowed to differ.
    assert scores(p, state)["scenarios"]["UP"]["target_brier"] is None


def test_target_and_invalidation_same_minute_is_ambiguous():
    p = prediction()
    result = evaluate(
        p, (minute(0), minute(1, close=94, high=105, low=94)), START + timedelta(minutes=2)
    )
    assert result.state.lifecycle == "AMBIGUOUS"
    assert scores(p, result.state)["scenarios"]["UP"]["target_brier"] is None


def test_expiry_does_not_close_and_risk_reversal_does():
    p = prediction()
    result = evaluate(p, tuple(minute(i) for i in range(120)), p.expires_at)
    assert result.state.lifecycle == "EXPIRED" and result.state.observation_complete
    assert (
        holding_action(has_position=True, lifecycle="EXPIRED", reversed_direction=False) == "HOLD"
    )
    assert (
        holding_action(has_position=True, lifecycle="INVALIDATED", reversed_direction=False)
        == "CLOSE"
    )
    assert (
        holding_action(has_position=True, lifecycle="EXPIRED", reversed_direction=True) == "CLOSE"
    )
    assert (
        holding_action(has_position=False, lifecycle="INVALIDATED", reversed_direction=True)
        == "HOLD"
    )


def test_gap_is_not_filled_or_expired_without_evidence():
    p = prediction()
    result = evaluate(p, (minute(1), minute(2)), p.expires_at)
    assert result.missing_at == START and result.data_quality == "AWAITING_DATA"
    assert result.state.lifecycle == "ACTIVE" and not result.state.observation_complete


def test_incremental_is_identical_to_replay_and_retries_are_idempotent():
    p = prediction()
    bars = tuple(minute(i, close=104 if i == 3 else 102) for i in range(120))
    replay = evaluate(p, bars, p.expires_at)
    initial = evaluate(p, bars[:20], START + timedelta(minutes=20))
    later = evaluate(p, bars, p.expires_at, initial.state)
    assert later.state == replay.state
    repeat = evaluate(p, bars, p.expires_at, later.state)
    assert repeat.state == replay.state and repeat.events == ()


def test_validation_never_uses_other_instrument_or_future_bars():
    p = prediction()
    with pytest.raises(DomainError, match="当前合约"):
        evaluate(p, (minute(0, inst_id="ETH-USDT-SWAP"),), START + timedelta(minutes=1))
    with pytest.raises(DomainError, match="尚不可用"):
        evaluate(p, (minute(0),), START)


def test_parent_is_free_to_synthesize_but_must_bind_action_and_scope():
    p = prediction()
    decision = ParentDecision(
        prediction=p.report,
        action="OPEN_LONG",
        selected_direction="UP",
        trade_style="TREND",
        context_revision="a" * 64,
        relationship="4h上升结构内的1h回踩完成",
        child_prediction_ids=(uuid4(),),
    )
    assert decision.prediction.p_up == Decimal("0.7")
    with pytest.raises(ValidationError, match="同方向"):
        ParentDecision(**{**decision.model_dump(), "action": "OPEN_SHORT"})
    with pytest.raises(ValidationError, match="具体持仓"):
        ParentDecision(**{**decision.model_dump(), "action": "CLOSE"})
    with pytest.raises(ValidationError, match="合约"):
        PublishedPrediction.model_validate({**p.model_dump(), "inst_id": "ETH-USDT-SWAP"})
