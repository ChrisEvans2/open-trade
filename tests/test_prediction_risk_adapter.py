from datetime import UTC, datetime

import pytest
from trading_core.parameters import TradeParameters
from trading_core.prediction_adapter import PredictionRiskAdapter
from trading_core.risk.engine import RiskLimits
from trading_core.schemas import (
    AccountState,
    AIAnalysisResult,
    ExecutionResult,
    FinalAction,
    MarketSnapshot,
    RuntimeState,
    StrategyAction,
)
from trading_core.strategy.risk_parameters import prediction_parameters


def market(price=100):
    return MarketSnapshot(
        symbol="BTCUSDT",
        timeframe="1h",
        timestamp=datetime(2026, 10, 11, tzinfo=UTC),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1,
    )


def social(block=False):
    return AIAnalysisResult(
        asset="BTC",
        sentiment="neutral",
        event_risk="high" if block else "low",
        should_block_trade=block,
        position_multiplier=1,
        confidence=0,
        reason="fixture risk evidence",
    )


def adapter(account=None):
    return PredictionRiskAdapter(
        RiskLimits(max_leverage=3),
        TradeParameters(),
        account or AccountState(equity=1000),
        RuntimeState(),
    )


def test_preserved_trend_breakout_and_range_risk_parameters():
    cfg = TradeParameters()
    trend = prediction_parameters(StrategyAction.LONG, "TREND", 100, 0.01, cfg)
    assert (
        trend.stop_loss_pct,
        trend.take_profit_pct,
        trend.suggested_position_pct,
    ) == (0.02, 0.04, 0.1)
    breakout = prediction_parameters(StrategyAction.SHORT, "BREAKOUT", 100, 0.01, cfg)
    assert (
        breakout.stop_loss_pct,
        breakout.take_profit_pct,
        breakout.suggested_position_pct,
    ) == (0.02, 0.03, 0.03)
    ranged = prediction_parameters(
        StrategyAction.LONG, "RANGE", 100, 0.01, cfg, range_low=95, range_high=115
    )
    assert (
        ranged.stop_loss_pct,
        ranged.take_profit_pct,
        ranged.risk_reward_ratio,
        ranged.suggested_position_pct,
    ) == (0.05475, 0.05, 0.913242, 0.04)
    with pytest.raises(ValueError, match="positive reward"):
        prediction_parameters(
            StrategyAction.LONG, "RANGE", 110, 0.01, cfg, range_low=95, range_high=115
        )


def test_low_probability_parent_still_reaches_original_risk_budget():
    risk = adapter()
    p = risk.prepare(
        "OPEN_LONG",
        "TREND",
        market(),
        atr_pct=0.01,
        confidence=0.01,
        reason="fixture parent decision",
        social_risk=social(),
    )
    assert p.decision.allow_trade
    assert p.decision.final_position_pct == 0.1 and p.decision.final_leverage == 2.5
    assert (
        p.decision.final_position_pct
        * p.decision.final_leverage
        * p.signal.stop_loss_pct
        == 0.005
    )
    assert p.trailing_stop_pct == 0.006 and p.trailing_activation_pct == 0.02
    risk.apply_fill(
        p,
        ExecutionResult(
            action=FinalAction.OPEN_LONG,
            symbol="BTCUSDT",
            success=True,
            fill_price=100,
            reason="fixture filled",
        ),
        market(),
    )
    assert risk.account.open_position_stop_loss_pct["BTCUSDT"] == 0.02
    assert risk.account.open_position_take_profit_pct["BTCUSDT"] == 0.04
    assert risk.account.open_position_signal_source["BTCUSDT"] == "prediction_parent"
    assert len(risk.audit.events) == 1
    assert risk.monitor(market(97)).should_close
    assert not risk.prepare(
        "OPEN_LONG",
        "TREND",
        market(),
        atr_pct=0.01,
        confidence=0.99,
        reason="duplicate",
        social_risk=social(),
    ).decision.allow_trade


def test_existing_account_limits_and_social_risk_are_preserved():
    risk = adapter()
    assert not risk.prepare(
        "OPEN_LONG",
        "TREND",
        market(),
        atr_pct=0.01,
        confidence=0.99,
        reason="fixture",
        social_risk=social(True),
    ).decision.allow_trade
    risk.account.daily_pnl_pct = -0.03
    p = risk.prepare(
        "OPEN_SHORT",
        "TREND",
        market(),
        atr_pct=0.01,
        confidence=0.99,
        reason="fixture",
        social_risk=social(),
    )
    assert (
        not p.decision.allow_trade
        and p.decision.risk_checks["daily_loss_limit"] == "fail"
    )
    risk.account.open_positions["BTCUSDT"] = 0.1
    p = risk.prepare(
        "CLOSE",
        "TREND",
        market(),
        atr_pct=0.01,
        confidence=0,
        reason="invalidated",
        social_risk=social(True),
    )
    assert p.decision.allow_trade and p.decision.final_action == FinalAction.CLOSE


def test_range_midpoint_exit_and_no_trailing_are_retained():
    risk = adapter()
    p = risk.prepare(
        "OPEN_LONG",
        "RANGE",
        market(),
        atr_pct=0.01,
        confidence=0.5,
        reason="range",
        social_risk=social(),
        range_low=95,
        range_high=115,
    )
    assert p.trailing_stop_pct == 0 and p.trailing_activation_pct == 0
    risk.apply_fill(
        p,
        ExecutionResult(
            action=FinalAction.OPEN_LONG,
            symbol="BTCUSDT",
            success=True,
            fill_price=100,
            reason="fixture filled",
        ),
        market(),
    )
    assert risk.account.open_position_range_mid["BTCUSDT"] == 105
    close = risk.monitor(market(105))
    assert close.should_close and "区间" in close.reason
