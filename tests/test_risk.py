from trading_core.risk.engine import RiskEngine, RiskLimits
from trading_core.schemas import AIAnalysisResult, AccountState, RuntimeState, StrategyAction, StrategySignal


def make_signal(action=StrategyAction.LONG, stop_loss_pct=None, suggested_position_pct=0.1):
    return StrategySignal(
        symbol="BTCUSDT",
        timeframe="1h",
        signal=action,
        confidence=0.7,
        entry_reason=["test"],
        invalid_reason=[],
        suggested_position_pct=suggested_position_pct,
        stop_loss_pct=stop_loss_pct,
    )


def make_ai(event_risk="low", block=False, multiplier=1):
    return AIAnalysisResult(
        asset="BTC",
        sentiment="neutral",
        event_risk=event_risk,
        should_block_trade=block,
        position_multiplier=multiplier,
        confidence=0.7,
        key_events=[],
        reason="test",
    )


def test_medium_social_risk_reduces_position():
    decision = RiskEngine().evaluate(make_signal(), make_ai("medium", multiplier=1), AccountState(), RuntimeState())
    assert decision.allow_trade is True
    assert decision.final_position_pct == 0.05
    assert decision.risk_checks["social_risk_filter"] == "reduced_position"


def test_high_social_risk_blocks_trade():
    decision = RiskEngine().evaluate(make_signal(), make_ai("high", block=True, multiplier=0), AccountState(), RuntimeState())
    assert decision.allow_trade is False
    assert decision.risk_checks["social_risk_filter"] == "blocked"


def test_dynamic_leverage_increases_when_stop_loss_is_narrow():
    engine = RiskEngine(RiskLimits(max_leverage=20, max_single_trade_loss_pct=0.01, max_position_pct=0.12))

    decision = engine.evaluate(make_signal(stop_loss_pct=0.01), make_ai(), AccountState(), RuntimeState())

    assert decision.allow_trade is True
    assert decision.final_position_pct == 0.1
    assert decision.final_leverage == 6.0
    assert "名义仓位 60.00%" in decision.reason


def test_dynamic_leverage_decreases_when_stop_loss_is_wide():
    engine = RiskEngine(RiskLimits(max_leverage=20, max_single_trade_loss_pct=0.01, max_position_pct=0.12))

    decision = engine.evaluate(make_signal(stop_loss_pct=0.05), make_ai(), AccountState(), RuntimeState())

    assert decision.allow_trade is True
    assert decision.final_position_pct == 0.1
    assert decision.final_leverage == 2.0
    assert "技术止损 5.00%" in decision.reason


def test_dynamic_leverage_respects_minimum_leverage():
    engine = RiskEngine(RiskLimits(min_leverage=3, max_leverage=20, max_single_trade_loss_pct=0.01, max_position_pct=0.12))

    decision = engine.evaluate(make_signal(stop_loss_pct=0.05), make_ai(), AccountState(), RuntimeState())

    assert decision.allow_trade is True
    assert decision.final_leverage == 3.0
    assert decision.final_position_pct == 0.066667
    assert "动态杠杆 3.00x" in decision.reason


def test_minimum_leverage_does_not_violate_minimum_margin_risk_budget():
    engine = RiskEngine(
        RiskLimits(
            min_leverage=3,
            max_leverage=20,
            max_single_trade_loss_pct=0.01,
            max_position_pct=0.10,
            min_position_margin=1.0,
        )
    )

    decision = engine.evaluate(make_signal(stop_loss_pct=0.50), make_ai(), AccountState(equity=100), RuntimeState())

    assert decision.allow_trade is False
    assert decision.risk_checks["position_size"] == "below_min_margin"


def test_margin_never_exceeds_ten_percent_even_if_config_allows_more():
    engine = RiskEngine(RiskLimits(max_leverage=20, max_single_trade_loss_pct=0.01, max_position_pct=0.12))

    decision = engine.evaluate(make_signal(stop_loss_pct=0.005, suggested_position_pct=0.1), make_ai(), AccountState(), RuntimeState())

    assert decision.allow_trade is True
    assert decision.final_position_pct <= 0.1
    assert decision.final_position_pct == 0.1


def test_margin_never_opens_below_one_usdt():
    engine = RiskEngine(
        RiskLimits(
            max_leverage=20,
            max_single_trade_loss_pct=0.005,
            max_position_pct=0.10,
            min_position_margin=1.0,
        )
    )

    decision = engine.evaluate(make_signal(stop_loss_pct=1.0), make_ai(), AccountState(equity=100), RuntimeState())

    assert decision.allow_trade is False
    assert decision.risk_checks["position_size"] == "below_min_margin"
    assert "最低保证金 1.00 USDT" in decision.reason


def test_open_trade_margin_is_at_least_one_usdt():
    engine = RiskEngine(
        RiskLimits(
            max_leverage=20,
            max_single_trade_loss_pct=0.01,
            max_position_pct=0.10,
            min_position_margin=1.0,
        )
    )

    decision = engine.evaluate(make_signal(stop_loss_pct=0.50), make_ai(), AccountState(equity=100), RuntimeState())

    assert decision.allow_trade is True
    assert decision.final_position_pct >= 0.01


def test_open_trade_respects_two_usdt_min_margin_with_one_percent_risk_budget():
    engine = RiskEngine(
        RiskLimits(
            min_leverage=3,
            max_leverage=20,
            max_single_trade_loss_pct=0.01,
            max_position_pct=0.10,
            min_position_margin=2.0,
        )
    )

    decision = engine.evaluate(make_signal(stop_loss_pct=0.018), make_ai(), AccountState(equity=100), RuntimeState())

    assert decision.allow_trade is True
    assert decision.final_position_pct >= 0.02


def test_same_direction_position_limit_blocks_concentrated_entries():
    engine = RiskEngine(RiskLimits(max_open_positions=5, max_same_direction_positions=2, max_same_direction_margin_pct=0.30))
    account = AccountState(open_positions={"ETHUSDT": -0.1, "SOLUSDT": -0.08})

    decision = engine.evaluate(make_signal(action=StrategyAction.SHORT), make_ai(), account, RuntimeState())

    assert decision.allow_trade is False
    assert decision.risk_checks["same_direction_positions"] == "fail"


def test_same_direction_margin_limit_blocks_concentrated_entries():
    engine = RiskEngine(RiskLimits(max_open_positions=5, max_same_direction_positions=3, max_same_direction_margin_pct=0.20))
    account = AccountState(open_positions={"ETHUSDT": -0.1, "SOLUSDT": -0.1})

    decision = engine.evaluate(make_signal(action=StrategyAction.SHORT), make_ai(), account, RuntimeState())

    assert decision.allow_trade is False
    assert decision.risk_checks["same_direction_margin"] == "fail"
