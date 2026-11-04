from datetime import UTC, datetime

import pytest
from trading_core.accounting import AccountBook
from trading_core.execution.paper_risk import PaperPositionRiskManager
from trading_core.prediction_adapter import EventBuffer
from trading_core.risk.engine import RiskEngine, RiskLimits
from trading_core.schemas import AccountState, FinalAction, MarketSnapshot, RuntimeState


def book(account):
    limits = RiskLimits()
    return AccountBook(
        account,
        RuntimeState(),
        RiskEngine(limits),
        PaperPositionRiskManager(limits),
        EventBuffer(),
    )


@pytest.mark.parametrize("direction,fee", [(1, -0.01), (-1, 0.01)])
def test_funding_applies_once_and_records_in_memory(direction, fee):
    account = AccountState(
        equity=100,
        open_positions={"BTCUSDT": direction * 0.1},
        open_position_leverage={"BTCUSDT": 10},
        open_position_entry_price={"BTCUSDT": 100},
        open_position_mark_price={"BTCUSDT": 100},
        open_position_principal={"BTCUSDT": 10},
        open_position_notional={"BTCUSDT": 100},
        open_position_funding_rate={"BTCUSDT": 0.0001},
        open_position_funding_time={"BTCUSDT": datetime(2026, 1, 1, tzinfo=UTC)},
    )
    ledger = book(account)
    assert ledger._settle_due_funding("BTCUSDT") is not None
    assert ledger._settle_due_funding("BTCUSDT") is None
    assert account.realized_pnl_amount == pytest.approx(fee)
    assert account.open_position_cumulative_funding_amount["BTCUSDT"] == pytest.approx(
        fee
    )
    assert len(ledger.audit_logger.events) == 1
    assert ledger.audit_logger.events[0]["event"][
        "funding_fee_amount"
    ] == pytest.approx(fee)


def test_market_mark_and_funding_metadata_use_shared_account():
    account = AccountState(
        equity=100,
        open_positions={"BTCUSDT": 0.1},
        open_position_leverage={"BTCUSDT": 10},
        open_position_entry_price={"BTCUSDT": 100},
        open_position_notional={"BTCUSDT": 100},
    )
    ledger = book(account)
    market = MarketSnapshot(
        symbol="BTCUSDT",
        timeframe="1h",
        timestamp=datetime(2026, 10, 22, tzinfo=UTC),
        open=101,
        high=101,
        low=101,
        close=101,
        volume=1,
        funding_rate=0.0002,
        funding_inst_id="BTC-USDT-SWAP",
    )
    ledger._update_position_market_metadata("BTCUSDT", market)
    ledger._mark_to_market("BTCUSDT", market.close)
    assert account.open_position_funding_rate["BTCUSDT"] == 0.0002
    assert account.open_position_unrealized_pnl_amount["BTCUSDT"] == pytest.approx(1)
    assert account.total_unrealized_pnl_amount == pytest.approx(1)


def test_confirmed_fills_record_events_without_parallel_file_ledger():
    account = AccountState(equity=100)
    ledger = book(account)
    ledger._apply_paper_execution(
        FinalAction.OPEN_SHORT,
        "BTCUSDT",
        0.08,
        5,
        100,
        success=True,
        event_source="prediction_intent",
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
    )
    opened = ledger.audit_logger.events[0]["event"]
    assert opened["margin_amount"] == 8
    assert opened["notional_amount"] == 40
    assert opened["stop_loss_pct"] == 0.02
    ledger._apply_paper_execution(FinalAction.CLOSE, "BTCUSDT", 0, 1, 95, success=True)
    assert not account.open_positions
    assert account.realized_pnl_amount == pytest.approx(2)
    assert len(ledger.audit_logger.events) == 2
    assert ledger.account_state_path is None


@pytest.mark.parametrize("direction,adverse", [(1, 0.05), (-1, 0.06)])
def test_exit_event_keeps_unlevered_excursions(direction, adverse):
    account = AccountState(
        equity=100,
        open_positions={"BTCUSDT": direction * 0.05},
        open_position_leverage={"BTCUSDT": 2},
        open_position_entry_price={"BTCUSDT": 100},
        open_position_high_watermark={"BTCUSDT": 110 if direction == 1 else 106},
        open_position_low_watermark={"BTCUSDT": 95 if direction == 1 else 90},
    )
    ledger = book(account)
    ledger._apply_paper_execution(FinalAction.CLOSE, "BTCUSDT", 0, 1, 100, success=True)
    event = ledger.audit_logger.events[0]["event"]
    assert event["max_favorable_excursion_pct"] == pytest.approx(0.1)
    assert event["max_adverse_excursion_pct"] == pytest.approx(adverse)
