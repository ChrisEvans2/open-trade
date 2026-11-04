from datetime import datetime, timezone

from trading_core.execution.paper_risk import PaperPositionRiskManager
from trading_core.risk.engine import RiskLimits
from trading_core.schemas import AccountState, MarketSnapshot


def market(close: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        timeframe="1h",
        timestamp=datetime(2026, 5, 25, tzinfo=timezone.utc),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1,
    )


def account() -> AccountState:
    return AccountState(
        open_positions={"BTCUSDT": 0.08},
        open_position_leverage={"BTCUSDT": 2.0},
        open_position_entry_price={"BTCUSDT": 100.0},
        open_position_high_watermark={"BTCUSDT": 100.0},
        open_position_low_watermark={"BTCUSDT": 100.0},
    )


def test_paper_stop_loss_triggers_on_leveraged_loss():
    decision = PaperPositionRiskManager(RiskLimits(stop_loss_pct=0.015)).evaluate_close(account(), market(99.0))
    assert decision.should_close is True
    assert "止损" in decision.reason


def test_paper_take_profit_triggers_on_leveraged_profit():
    decision = PaperPositionRiskManager(RiskLimits(take_profit_pct=0.03)).evaluate_close(account(), market(102.0))
    assert decision.should_close is True
    assert "止盈" in decision.reason


def test_position_specific_stop_loss_overrides_fixed_paper_stop():
    acct = account()
    acct.open_position_stop_loss_pct["BTCUSDT"] = 0.05

    decision = PaperPositionRiskManager(RiskLimits(stop_loss_pct=0.015)).evaluate_close(acct, market(99.0))

    assert decision.should_close is False


def test_position_specific_take_profit_overrides_fixed_paper_take_profit():
    acct = account()
    acct.open_position_take_profit_pct["BTCUSDT"] = 0.05

    decision = PaperPositionRiskManager(RiskLimits(take_profit_pct=0.03)).evaluate_close(acct, market(102.0))

    assert decision.should_close is False


def test_trailing_take_profit_requires_meaningful_profit():
    acct = account()
    acct.open_position_high_watermark["BTCUSDT"] = 102.0
    acct.open_position_stop_loss_pct["BTCUSDT"] = 0.05
    acct.open_position_take_profit_pct["BTCUSDT"] = 0.10

    decision = PaperPositionRiskManager(RiskLimits(trailing_stop_pct=0.012)).evaluate_close(acct, market(101.0))

    assert decision.should_close is False


def test_position_specific_trailing_stop_allows_trend_to_run():
    acct = account()
    acct.open_position_high_watermark["BTCUSDT"] = 103.0
    acct.open_position_take_profit_pct["BTCUSDT"] = 0.10
    acct.open_position_trailing_stop_pct["BTCUSDT"] = 0.025
    acct.open_position_trailing_activation_pct["BTCUSDT"] = 0.03

    decision = PaperPositionRiskManager(RiskLimits(trailing_stop_pct=0.010)).evaluate_close(acct, market(102.0))

    assert decision.should_close is False


def test_price_unit_trailing_requires_activation_before_closing():
    acct = account()
    acct.open_position_leverage["BTCUSDT"] = 20.0
    acct.open_position_high_watermark["BTCUSDT"] = 101.5
    acct.open_position_take_profit_pct["BTCUSDT"] = 0.10
    acct.open_position_trailing_stop_pct["BTCUSDT"] = 0.004
    acct.open_position_trailing_activation_pct["BTCUSDT"] = 0.008

    decision = PaperPositionRiskManager(RiskLimits()).evaluate_close(acct, market(100.9))

    assert decision.should_close is True
    assert "价格回撤" in decision.reason


def test_price_unit_trailing_does_not_use_leverage_for_activation():
    acct = account()
    acct.open_position_leverage["BTCUSDT"] = 20.0
    acct.open_position_high_watermark["BTCUSDT"] = 101.0
    acct.open_position_take_profit_pct["BTCUSDT"] = 0.10
    acct.open_position_trailing_stop_pct["BTCUSDT"] = 0.004
    acct.open_position_trailing_activation_pct["BTCUSDT"] = 0.008

    decision = PaperPositionRiskManager(RiskLimits()).evaluate_close(acct, market(100.4))

    assert decision.should_close is False


def test_range_mean_reversion_closes_at_range_mid():
    acct = account()
    acct.open_position_strategy_type["BTCUSDT"] = "range_mean_reversion"
    acct.open_position_range_mid["BTCUSDT"] = 101.0
    acct.open_position_take_profit_pct["BTCUSDT"] = 0.10

    decision = PaperPositionRiskManager(RiskLimits()).evaluate_close(acct, market(101.0))

    assert decision.should_close is True
    assert "区间" in decision.reason


def test_range_mean_reversion_tightens_stop_after_half_r():
    acct = account()
    acct.open_position_strategy_type["BTCUSDT"] = "range_mean_reversion"
    acct.open_position_stop_loss_pct["BTCUSDT"] = 0.02
    acct.open_position_take_profit_pct["BTCUSDT"] = 0.10

    decision = PaperPositionRiskManager(RiskLimits()).evaluate_close(acct, market(101.0))

    assert decision.should_close is False
    assert acct.open_position_stop_loss_pct["BTCUSDT"] == 0.0005
