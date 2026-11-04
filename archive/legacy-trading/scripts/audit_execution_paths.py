"""离线执行契约审计。所有交易所调用均为内存假对象，不读取密钥、不联网、不下单。

输出 violation=true 表示成功复现安全契约缺陷，不表示真实账户发生过该故障。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from trading_system.ai.market_advisor import CodeSafetyGate
from trading_core.config import MarketAdvisorConfig, load_yaml
from trading_core.execution.live import OKXLiveExecutor
from trading_core.risk.engine import RiskEngine, RiskLimits
from trading_system.scheduler import TradingScheduler
from trading_core.schemas import (
    AIAnalysisResult, AccountState, MarketSnapshot, MetaMarketAdvisorDecision,
    MarketAdvisorResult, MarketRegimeSnapshot, RiskDecision, RuntimeState, StrategySignal,
)


class FakeExchange:
    def __init__(self, state="filled", filled=None, protection_failure=False):
        self.state = state
        self.filled = filled
        self.protection_failure = protection_failure
        self.submissions = []
        self.protections = []
        self.positions = []

    async def get_instruments(self, *args):
        return [{"instId": "BTC-USDT-SWAP", "ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01"}]

    async def set_leverage(self, *args):
        return {"code": "0"}

    async def place_order(self, **kwargs):
        self.submissions.append(kwargs)
        return {"code": "0", "data": [{"ordId": str(len(self.submissions)), "sCode": "0"}]}

    async def get_order(self, *args, **kwargs):
        filled = self.filled if self.filled is not None else self.submissions[-1]["size"]
        return {"state": self.state, "avgPx": "100000" if float(filled) else "", "accFillSz": filled}

    async def get_fills(self, *args, **kwargs):
        return []

    async def place_algo_order(self, body):
        if self.protection_failure:
            raise RuntimeError("模拟：成交后保护单接口失败")
        self.protections.append(body)
        return {"code": "0", "data": [{"algoId": "fake-protection", "sCode": "0"}]}

    async def get_positions(self, *args, **kwargs):
        return self.positions

    async def get_pending_orders(self, *args, **kwargs):
        return []


def snapshot(price=100000):
    return MarketSnapshot(
        symbol="BTCUSDT", timeframe="1h", timestamp="2026-09-29T06:00:00Z",
        open=price, high=price, low=price, close=price, volume=10,
    )


def decision(action="open_long"):
    return RiskDecision(allow_trade=True, final_action=action, final_position_pct=0.1,
                        final_leverage=1, risk_mode="normal", risk_checks={}, reason="离线假信号")


def scheduler(account, client=None):
    return TradingScheduler(
        market_data=None, social_monitor=None, social_filter=None, ai_analyzer=None,
        strategy=None, risk_engine=None, paper_risk=None, executor=None,
        audit_logger=None, reporter=None, account_state=account,
        runtime_state=RuntimeState(mode="normal", execution_mode="live"),
        live_reconcile_client=client,
    )


async def audit():
    findings = []
    for state in ("live", "canceled"):
        client = FakeExchange(state=state, filled="0")
        result = await OKXLiveExecutor(client).execute(decision(), snapshot(), AccountState(equity=100))
        findings.append({"case": f"zero_fill_{state}", "violation": result.success,
                         "reported_success": result.success, "invented_fill_price": result.fill_price,
                         "invented_contracts": result.metadata.get("exchange_contracts")})

    client = FakeExchange(protection_failure=True)
    executor = OKXLiveExecutor(client)
    first = await executor.execute(decision(), snapshot(), AccountState(equity=100), stop_loss_pct=0.02, take_profit_pct=0.04)
    second = await executor.execute(decision(), snapshot(), AccountState(equity=100), stop_loss_pct=0.02, take_profit_pct=0.04)
    findings.append({"case": "filled_but_protection_failed", "violation": not first.success and first.order is None,
                     "exchange_has_fill": True, "reported_success": first.success,
                     "returned_order_missing": first.order is None, "protection_count": len(client.protections)})
    findings.append({"case": "retry_after_protection_failure", "violation": len(client.submissions) > 1,
                     "open_submissions_same_client_id": len(client.submissions),
                     "same_id": client.submissions[0]["client_order_id"] == client.submissions[-1]["client_order_id"]})

    account = AccountState(equity=100, open_positions={"BTCUSDT": 0.1},
                           open_position_leverage={"BTCUSDT": 1},
                           open_position_entry_price={"BTCUSDT": 100000},
                           open_position_contracts={"BTCUSDT": 0.01},
                           open_position_contract_value={"BTCUSDT": 0.01})
    executor = OKXLiveExecutor(FakeExchange())
    try:
        order = executor._build_order(decision("close"), snapshot(110000), account, "BTC-USDT-SWAP",
                                      (await executor._instrument("BTC-USDT-SWAP")), "fake-close")
        close_result = {"quantity": order.quantity, "violation": order.quantity != 0.01}
    except ValueError as exc:
        close_result = {"error": str(exc), "violation": True}
    findings.append({"case": "close_minimum_lot_after_price_rise", "actual_contracts": 0.01, **close_result})

    account.open_position_contracts["BTCUSDT"] = 0.1
    account.open_positions["BTCUSDT"] = 0.1
    partial = await OKXLiveExecutor(FakeExchange(state="partially_filled", filled="0.01")).execute(
        decision("close"), snapshot(10000), account,
    )
    sched = scheduler(account)
    sched._apply_paper_execution(partial.action, "BTCUSDT", 0, 1, partial.fill_price or 10000,
                                 partial.success, execution_metadata=partial.metadata)
    findings.append({"case": "partial_close_erases_entire_local_position",
                     "violation": "BTCUSDT" not in account.open_positions,
                     "actual_closed_contracts": 0.01, "expected_remaining_contracts": 0.09,
                     "local_position_present": "BTCUSDT" in account.open_positions})

    for pos in ("-0.1", "0.01"):
        account = AccountState(open_positions={"BTCUSDT": 0.1}, open_position_contracts={"BTCUSDT": 0.1})
        client = FakeExchange()
        client.positions = [{"instId": "BTC-USDT-SWAP", "pos": pos, "posSide": "net"}]
        reconciled = await scheduler(account, client)._reconcile_live_state()
        findings.append({"case": "reconcile_direction" if pos.startswith("-") else "reconcile_size",
                         "violation": reconciled, "local_long_contracts": 0.1, "exchange_contracts": pos,
                         "reconcile_reported_ok": reconciled})

    # Positive control: the actual gates can reach the executor when valid data is supplied.
    config_dir = Path(__file__).resolve().parents[1] / "config"
    cfg = MarketAdvisorConfig.model_validate(load_yaml(config_dir / "app.yaml")["market_advisor"])
    risk_config = load_yaml(config_dir / "risk.yaml")
    limits = RiskLimits(**{
        **risk_config["defaults"],
        **risk_config["execution_modes"]["live"],
        **risk_config["modes"]["normal"],
    })
    # Probe advertised regime protection and directional invalidation checks.
    rows = [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 100} for _ in range(21)]
    rows.append({"open": 101, "high": 103, "low": 100.8, "close": 101.7, "volume": 150})
    candidate = StrategySignal(
        symbol="BTCUSDT", timeframe="1h", signal="hold", confidence=0,
        suggested_position_pct=0, atr_pct=0.01,
        market_regime=MarketRegimeSnapshot(regime="range", confidence=0.8),
    )
    candidate_meta = MetaMarketAdvisorDecision(
        symbol="BTCUSDT", status="ok", decision="allow_candidate", direction="long",
        confidence=0.8, entry_timing="now", risk_budget_multiplier=0.5,
    )
    for invalidation in (100.6, 102.8):
        candidate_results = {
            item.timeframe: MarketAdvisorResult(symbol="BTCUSDT", timeframe=item.timeframe,
                                                advisor_type=item.advisor_type, status="ok")
            for item in cfg.advisors
        }
        candidate_results["1h"] = MarketAdvisorResult(
            symbol="BTCUSDT", timeframe="1h", advisor_type="early_candidate", status="ok",
            judgement="early_long_candidate", confidence=0.8, invalidation_price=invalidation,
        )
        candidate.market_regime.regime = "range" if invalidation == 100.6 else "trend_up"
        updated, candidate_audit = CodeSafetyGate(cfg).apply(
            candidate, snapshot(101.7), {"1h": rows}, candidate_results, candidate_meta, {},
        )
        findings.append({
            "case": "configured_range_block_unused" if invalidation == 100.6 else "long_invalidation_above_entry",
            "violation": candidate_audit.arbiter_decision.accepted,
            "code_gate_accepted": candidate_audit.arbiter_decision.accepted,
            "configured_block_regimes": cfg.block_regimes, "regime": candidate.market_regime.regime,
            "entry": 101.7, "invalidation": invalidation, "result_signal": updated.signal,
            "rejection_reasons": candidate_audit.arbiter_decision.reject_reasons,
        })
    signal = StrategySignal(symbol="BTCUSDT", timeframe="1h", signal="long", confidence=0.8,
                            suggested_position_pct=0.1, stop_loss_pct=0.02, take_profit_pct=0.04)
    results = {tf: MarketAdvisorResult(symbol="BTCUSDT", timeframe=tf, advisor_type=item.advisor_type, status="ok")
               for item in cfg.advisors for tf in [item.timeframe]}
    meta = MetaMarketAdvisorDecision(symbol="BTCUSDT", status="ok", decision="allow_candidate",
                                     direction="long", confidence=0.8, entry_timing="now", risk_budget_multiplier=0.5)
    approved, audit_result = CodeSafetyGate(cfg).apply(signal, snapshot(), {}, results, meta, {})
    ai = AIAnalysisResult(asset="BTC", sentiment="neutral", event_risk="low", should_block_trade=False,
                          position_multiplier=1, confidence=0.5, reason="离线验证")
    account = AccountState(equity=100)
    sched = scheduler(account)
    sched.market_advisor_config = cfg
    sched.market_advisor_client = object()
    risk = RiskEngine(limits).evaluate(approved, ai, account, sched.runtime_state)
    risk = sched._enforce_market_advisor_entry_gate(risk, audit_result)
    client = FakeExchange()
    result = await OKXLiveExecutor(client).execute(risk, snapshot(), account, stop_loss_pct=0.02, take_profit_pct=0.04)
    return {"offline_only": True, "findings": findings,
            "positive_control": {"gates_allow_trade": risk.allow_trade, "fake_submissions": len(client.submissions),
                                 "success": result.success, "fake_protections": len(client.protections),
                                 "decision_reason": risk.reason}}


if __name__ == "__main__":
    with patch("httpx.AsyncClient", side_effect=AssertionError("离线审计禁止网络请求")):
        print(json.dumps(asyncio.run(audit()), ensure_ascii=False, indent=2))
