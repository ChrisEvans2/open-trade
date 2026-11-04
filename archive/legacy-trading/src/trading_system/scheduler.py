from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import yaml

from trading_core.config import MarketAdvisorConfig
from trading_core.ai.analyzer import SocialAIAnalyzer
from trading_system.ai.market_advisor import CodeSafetyGate, MarketAdvisorClient, normalize_gateway_response, normalize_meta_decision
from trading_core.execution.paper import PaperExecutor
from trading_core.execution.paper_risk import PaperPositionRiskManager
from trading_system.market_data.collector import MarketDataService
from trading_core.market_data.okx_client import OKXReadOnlyClient
from trading_core.risk.engine import RiskEngine
from trading_core.schemas import (
    AccountState,
    CycleArtifacts,
    ExecutionMode,
    FinalAction,
    MAX_SINGLE_TRADE_MARGIN_PCT,
    MarketAdvisorAudit,
    MarketSnapshot,
    PaperTradeEvent,
    RiskDecision,
    RiskMode,
    RuntimeState,
    SocialContributionAudit,
    StrategyAction,
    StrategySignal,
    utc_now,
)
from trading_core.social.filter import SocialFilter
from trading_core.social.monitor import SocialMonitor
from trading_system.storage.audit import AuditLogger
from trading_system.strategy.trend_strategy import TrendStrategy
from trading_system.telegram.reporter import TelegramReporter


from trading_core.accounting import AccountBook


class TradingScheduler(AccountBook):
    EXIT_ALGO_ORDER_TYPES = ("oco", "conditional", "move_order_stop")

    def __init__(
        self,
        market_data: MarketDataService,
        social_monitor: SocialMonitor,
        social_filter: SocialFilter,
        ai_analyzer: SocialAIAnalyzer,
        strategy: TrendStrategy,
        risk_engine: RiskEngine,
        paper_risk: PaperPositionRiskManager,
        executor: PaperExecutor,
        audit_logger: AuditLogger,
        reporter: TelegramReporter,
        account_state: AccountState | None = None,
        runtime_state: RuntimeState | None = None,
        account_state_path: str | Path | None = None,
        runtime_state_path: str | Path | None = None,
        account_equity_client: OKXReadOnlyClient | None = None,
        live_reconcile_client: OKXReadOnlyClient | None = None,
        max_order_failures: int = 3,
        risk_limits_loader: Callable[[RuntimeState], object] | None = None,
        market_advisor_client: MarketAdvisorClient | None = None,
        market_advisor_config: MarketAdvisorConfig | None = None,
        social_ai_enabled: bool = True,
    ):
        self.market_data = market_data
        self.social_monitor = social_monitor
        self.social_filter = social_filter
        self.ai_analyzer = ai_analyzer
        self.strategy = strategy
        self.risk_engine = risk_engine
        self.paper_risk = paper_risk
        self.executor = executor
        self.audit_logger = audit_logger
        self.reporter = reporter
        self.account_state = account_state or AccountState()
        self.runtime_state = runtime_state or RuntimeState()
        self.account_state_path = Path(account_state_path) if account_state_path else None
        self.runtime_state_path = Path(runtime_state_path) if runtime_state_path else None
        self.account_equity_client = account_equity_client
        self.live_reconcile_client = live_reconcile_client
        self.max_order_failures = max_order_failures
        self.risk_limits_loader = risk_limits_loader
        self.market_advisor_client = market_advisor_client
        self.market_advisor_config = market_advisor_config or MarketAdvisorConfig()
        self.market_advisor_arbiter = CodeSafetyGate(self.market_advisor_config)
        self._market_advisor_trigger_history: deque[tuple[datetime, str, str]] = deque(maxlen=500)
        self.social_ai_enabled = social_ai_enabled

    def _persist_account_state(self) -> None:
        if self.account_state_path:
            self.account_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.account_state_path.write_text(self.account_state.model_dump_json(indent=2), encoding="utf-8")


    async def run_cycle(self, symbol: str, timeframe: str, lookback_hours: int = 1) -> CycleArtifacts:
        self._reload_runtime_state()
        await self._sync_account_equity()
        await self._sync_live_realized_pnl()
        await self._reconcile_live_state()
        market = await self.market_data.fetch_latest_closed_kline(symbol, timeframe)
        self._settle_due_funding(symbol)
        self._update_position_market_metadata(symbol, market)
        self._mark_to_market(symbol, market.close)
        social_raw = []
        social_filtered = []
        if self.social_ai_enabled:
            social_raw = await self.social_monitor.fetch_recent(symbol=symbol, lookback_hours=lookback_hours)
            social_filtered = self.social_filter.filter_items(social_raw, symbol=symbol, lookback_hours=lookback_hours)
            ai_analysis = await self.ai_analyzer.analyze(symbol=symbol, items=social_filtered, market_snapshot=market)
        else:
            ai_analysis = self.ai_analyzer._no_recent_data(symbol.removesuffix("USDT"))
        strategy_signal = await self._generate_strategy_signal(symbol, timeframe, market)
        strategy_signal = self._combine_event_alpha_signal(strategy_signal, ai_analysis)
        close = self.paper_risk.evaluate_close(self.account_state, market)
        market_advisor_audit = None
        if not close.should_close:
            strategy_signal, market_advisor_audit = await self._apply_market_advisor(
                symbol,
                timeframe,
                market,
                strategy_signal,
            )
        if close.should_close:
            risk_decision = self.paper_risk.close_decision(close.reason)
            if self.runtime_state.execution_mode == ExecutionMode.LIVE:
                execution_result = await self.executor.execute(risk_decision, market, self.account_state)
            else:
                execution_result = self.paper_risk.close_result(market, close.reason)
        else:
            risk_decision = self.risk_engine.evaluate(
                signal=strategy_signal,
                ai=ai_analysis,
                account=self.account_state,
                runtime_state=self.runtime_state,
            )
            risk_decision = self._enforce_market_advisor_entry_gate(risk_decision, market_advisor_audit)
            if market_advisor_audit is not None:
                market_advisor_audit.risk_engine_result = risk_decision.model_dump(mode="json")
            await self._maybe_trigger_hard_stop(risk_decision)
            execution_result = await self.executor.execute(
                risk_decision,
                market,
                self.account_state,
                stop_loss_pct=strategy_signal.stop_loss_pct,
                take_profit_pct=strategy_signal.take_profit_pct,
                trailing_stop_pct=self._entry_trailing_stop_pct(strategy_signal),
                trailing_activation_pct=self._entry_trailing_activation_pct(strategy_signal),
            )
        await self._record_order_failure_if_needed(execution_result)
        event = self._apply_paper_execution(
            execution_result.action,
            market.symbol,
            risk_decision.final_position_pct,
            risk_decision.final_leverage,
            execution_result.fill_price or market.close,
            execution_result.success,
            event_source="strategy_cycle",
            timeframe=market.timeframe,
            reason=execution_result.reason,
            stop_loss_pct=strategy_signal.stop_loss_pct,
            take_profit_pct=strategy_signal.take_profit_pct,
            trailing_stop_pct=self._entry_trailing_stop_pct(strategy_signal),
            trailing_activation_pct=self._entry_trailing_activation_pct(strategy_signal),
            execution_metadata=self._trade_event_metadata(execution_result.metadata, strategy_signal),
        )
        if (
            self.runtime_state.execution_mode == ExecutionMode.LIVE
            and execution_result.success
            and execution_result.action == FinalAction.CLOSE
        ):
            await self._cleanup_live_exit_algos(market.symbol)
        await self._send_paper_trade_event(event)
        artifacts = CycleArtifacts(
            market=market,
            social_raw=social_raw,
            social_filtered=social_filtered,
            ai_analysis=ai_analysis,
            strategy_signal=strategy_signal,
            risk_decision=risk_decision,
            execution_result=execution_result,
            market_advisor=market_advisor_audit,
        )
        self.audit_logger.save_cycle(artifacts)
        await self.reporter.send_cycle_report(artifacts)
        return artifacts

    async def sync_market_state(self, symbol: str, timeframe: str) -> None:
        self._reload_runtime_state()
        await self._sync_account_equity()
        await self._sync_live_realized_pnl()
        await self._reconcile_live_state()
        market = await self.market_data.fetch_mark_snapshot(symbol, timeframe)
        self._settle_due_funding(symbol)
        self._update_position_market_metadata(symbol, market)
        self._mark_to_market(symbol, market.close)
        self._record_paper_trade_event("mark_update", market.symbol, market.timeframe, market.close, "5min mark price refresh")
        if self.runtime_state.execution_mode == ExecutionMode.LIVE:
            return
        if self.paper_risk is None:
            return
        close = self.paper_risk.evaluate_close(self.account_state, market)
        if close.should_close:
            if self.runtime_state.execution_mode == ExecutionMode.LIVE:
                execution_result = await self.executor.execute(
                    self.paper_risk.close_decision(close.reason),
                    market,
                    self.account_state,
                )
            else:
                execution_result = self.paper_risk.close_result(market, close.reason)
            await self._record_order_failure_if_needed(execution_result)
            event = self._apply_paper_execution(
                execution_result.action,
                market.symbol,
                0,
                1,
                market.close,
                execution_result.success,
                event_source="mark_sync",
                timeframe=market.timeframe,
                reason=execution_result.reason,
            )
            await self._send_paper_trade_event(event)



    @staticmethod
    def _trade_event_metadata(metadata: dict | None, strategy_signal: StrategySignal | None) -> dict:
        result = dict(metadata or {})
        if strategy_signal is None:
            return result
        audit = strategy_signal.social_audit
        technical_signal = audit.technical_signal_before_social if audit else strategy_signal.signal
        social_alpha_signal = audit.alpha_signal if audit else StrategyAction.HOLD
        if audit and audit.social_created_event_signal:
            source = "social"
        elif audit and audit.social_changed_decision:
            source = "mixed"
        elif strategy_signal.signal != StrategyAction.HOLD:
            source = "technical"
        else:
            source = "unknown"
        result.update(
            {
                "signal_source": source,
                "technical_signal": technical_signal,
                "social_alpha_signal": social_alpha_signal,
                "social_changed_decision": audit.social_changed_decision if audit else False,
                "social_contribution_reason": audit.contribution_reason if audit else "",
                "entry_reasons": list(strategy_signal.entry_reason),
            }
        )
        if "mean_reversion_entry" in strategy_signal.entry_reason:
            result["strategy_type"] = "range_mean_reversion"
        if strategy_signal.advisor_confirmed_by_code:
            result["signal_source"] = "mixed"
            result["strategy_type"] = "llm_early_structure"
            result["advisor_judgement"] = strategy_signal.advisor_judgement
            result["advisor_confidence"] = strategy_signal.advisor_confidence
            result["advisor_confirmed_by_code"] = strategy_signal.advisor_confirmed_by_code
            result["advisor_timeframes"] = list(strategy_signal.advisor_timeframes)
        regime = strategy_signal.market_regime
        if regime is not None:
            result["regime"] = regime.regime
            result["detailed_regime"] = regime.detailed_regime
            result["risk_flags"] = list(regime.risk_flags)
            result["block_reasons"] = list(regime.block_reasons)
            result["range_quality_score"] = regime.range_quality_score
            result["range_mid"] = regime.range_mid
            result["range_low"] = regime.range_low
            result["range_high"] = regime.range_high
        return result



    async def _send_paper_trade_event(self, event: PaperTradeEvent | None) -> None:
        if event is None or self.reporter is None:
            return
        if event.event not in {"open_long", "open_short", "close"}:
            return
        await self.reporter.send_trade_event(event, self.runtime_state.execution_mode)







    async def _sync_account_equity(self) -> None:
        if self.account_equity_client is None:
            return
        try:
            equity = await self.account_equity_client.get_account_equity()
        except Exception:
            self.account_state.api_failure_count += 1
            self._persist_account_state()
            return
        if equity is None:
            return
        self.account_state.equity = equity
        self._refresh_position_amounts()
        self._persist_account_state()

    async def _sync_live_realized_pnl(self) -> None:
        """Backfill the bot's completed live trades from OKX fills, not local price marks."""
        if self.runtime_state.execution_mode != ExecutionMode.LIVE or self.account_equity_client is None:
            return
        get_fills_history = getattr(self.account_equity_client, "get_fills_history", None)
        get_positions_history = getattr(self.account_equity_client, "get_positions_history", None)
        try:
            if get_positions_history is not None:
                history = await get_positions_history("SWAP", limit=100)
                realized = self._completed_bot_position_history_pnl(history)
                if realized is not None:
                    self.account_state.realized_pnl_amount = realized
                    self.account_state.realized_pnl_pct = realized / self.account_state.equity if self.account_state.equity else 0
                    self._persist_account_state()
                    return
            if get_fills_history is None:
                return
            fills = await get_fills_history(begin_ms=self._live_trade_start_ms(), limit=100)
        except Exception:
            return
        realized = self._completed_bot_fill_pnl(fills)
        if realized is None:
            return
        self.account_state.realized_pnl_amount = realized
        self.account_state.realized_pnl_pct = realized / self.account_state.equity if self.account_state.equity else 0
        self._persist_account_state()

    def _live_trade_start_ms(self) -> int | None:
        if self.audit_logger is None:
            return None
        path = self.audit_logger.log_dir / "live_trade_events.jsonl"
        if not path.exists():
            return None
        try:
            first = json.loads(path.open(encoding="utf-8").readline())
            timestamp = datetime.fromisoformat(str(first["timestamp"]).replace("Z", "+00:00"))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None
        # OKX treats `begin` as an exclusive boundary; allow for the small delay
        # between exchange fill time and local audit-event persistence.
        return max(0, int(timestamp.astimezone(timezone.utc).timestamp() * 1000) - 60_000)

    def _completed_bot_position_history_pnl(self, rows: list[dict]) -> float | None:
        close_events = self._live_close_events_for_history_match()
        if not close_events:
            return None
        used: set[int] = set()
        total = 0.0
        for event in close_events:
            match = self._match_position_history_to_close_event(event, rows, used)
            if match is None:
                return None
            used.add(id(match))
            total += self._optional_float(match.get("realizedPnl"))
        return total

    def _live_close_events_for_history_match(self) -> list[dict]:
        if self.audit_logger is None:
            return []
        path = self.audit_logger.log_dir / "live_trade_events.jsonl"
        if not path.exists():
            return []
        events: list[dict] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("event") == "close" and row.get("symbol") and row.get("side"):
                        events.append(row)
        except OSError:
            return []
        return events

    def _match_position_history_to_close_event(
        self, event: dict, rows: list[dict], used: set[int]
    ) -> dict | None:
        symbol = str(event.get("symbol") or "")
        direction = str(event.get("side") or "").lower()
        entry = self._positive_float(event.get("entry_price"))
        close = self._positive_float(event.get("fill_price"))
        event_ts = self._event_timestamp_ms(event.get("timestamp"))
        candidates = []
        for row in rows:
            if id(row) in used:
                continue
            if row.get("instId") != self._swap_inst_id(symbol):
                continue
            if str(row.get("direction") or "").lower() != direction:
                continue
            if entry and not self._prices_close(self._positive_float(row.get("openAvgPx")), entry, tolerance=0.003):
                continue
            if close and not self._prices_close(self._positive_float(row.get("closeAvgPx")), close, tolerance=0.003):
                continue
            row_ts = self._positive_float(row.get("uTime"))
            if event_ts is not None and row_ts is not None and abs(row_ts - event_ts) > 30 * 60 * 1000:
                continue
            candidates.append(row)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda row: abs((self._positive_float(row.get("uTime")) or 0) - (event_ts or 0)),
        )

    @staticmethod
    def _event_timestamp_ms(value) -> int | None:
        try:
            timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return int(timestamp.astimezone(timezone.utc).timestamp() * 1000)

    @classmethod
    def _completed_bot_fill_pnl(cls, fills: list[dict]) -> float | None:
        if not any(str(row.get("clOrdId") or "").startswith("lv") for row in fills):
            return None
        fills = sorted(fills, key=lambda row: int(str(row.get("fillTime") or "0")))
        open_lots: dict[str, deque[dict[str, float]]] = {}
        total = 0.0
        for row in fills:
            inst_id = str(row.get("instId") or "")
            size = cls._positive_float(row.get("fillSz"))
            if not inst_id or size is None:
                continue
            side = str(row.get("side") or "").lower()
            direction = 1 if side == "buy" else -1 if side == "sell" else 0
            if not direction:
                continue
            lots = open_lots.setdefault(inst_id, deque())
            signed_open = sum(lot["direction"] * lot["size"] for lot in lots)
            fee = cls._optional_float(row.get("fee"))
            bot_order = str(row.get("clOrdId") or "").startswith("lv")
            # Native OCO/trailing exits receive an exchange-generated clOrdId.
            # Include them only when they reduce an existing bot-originated lot.
            if not bot_order and (not lots or signed_open * direction >= 0):
                continue
            if not lots or signed_open * direction >= 0:
                lots.append({"direction": float(direction), "size": size, "fee": fee})
                continue
            remaining = size
            exit_fee_remaining = fee
            while remaining > 0 and lots:
                lot = lots[0]
                matched = min(remaining, lot["size"])
                share = matched / size
                entry_fee = lot["fee"] * (matched / lot["size"])
                gross = cls._optional_float(row.get("fillPnl")) * share
                total += gross + entry_fee + exit_fee_remaining * share
                lot["size"] -= matched
                remaining -= matched
                if lot["size"] <= 1e-12:
                    lots.popleft()
            # A reversal can contain a fresh opening remainder; preserve its fee share.
            if remaining > 1e-12:
                lots.append({"direction": float(direction), "size": remaining, "fee": fee * (remaining / size)})
        return total

    def _refresh_position_amounts(self) -> None:
        for symbol, signed_margin in self.account_state.open_positions.items():
            margin_pct = abs(signed_margin)
            leverage = self.account_state.open_position_leverage.get(symbol, 1.0)
            pnl_pct = self.account_state.open_position_unrealized_pnl_pct.get(symbol, 0)
            if symbol not in self.account_state.open_position_contracts:
                self.account_state.open_position_principal[symbol] = self.account_state.equity * margin_pct
                self.account_state.open_position_notional[symbol] = self.account_state.equity * margin_pct * leverage
                self.account_state.open_position_unrealized_pnl_amount[symbol] = self.account_state.equity * pnl_pct
        self._recalculate_total_unrealized()
        # Older state files only persisted the percentage. Preserve their value once,
        # while live states with exchange-derived amounts remain amount-authoritative.
        if self.account_state.realized_pnl_amount == 0 and self.account_state.realized_pnl_pct:
            self.account_state.realized_pnl_amount = self.account_state.equity * self.account_state.realized_pnl_pct
        self.account_state.realized_pnl_pct = (
            self.account_state.realized_pnl_amount / self.account_state.equity if self.account_state.equity else 0
        )



    async def _reconcile_live_state(self) -> bool:
        if self.runtime_state.execution_mode != ExecutionMode.LIVE or self.live_reconcile_client is None:
            return True
        try:
            positions = await self.live_reconcile_client.get_positions("SWAP")
            pending_orders = await self.live_reconcile_client.get_pending_orders("SWAP")
        except Exception as exc:
            await self._enter_emergency(f"live reconcile failed: {exc.__class__.__name__}: {exc}")
            return False

        exchange_positions = self._open_exchange_positions(positions)
        local_positions = {symbol: value for symbol, value in self.account_state.open_positions.items() if abs(value) > 0}
        local_inst_ids = {self._swap_inst_id(symbol): symbol for symbol in local_positions}
        closed_on_exchange = set(local_inst_ids) - set(exchange_positions)
        for inst_id in closed_on_exchange:
            symbol = local_inst_ids[inst_id]
            exit_summary = await self._exchange_exit_summary(inst_id, symbol)
            event = self._apply_paper_execution(
                FinalAction.CLOSE,
                symbol,
                0,
                1,
                exit_summary["fill_price"],
                True,
                event_source="exchange_reconcile",
                reason=exit_summary["reason"],
                execution_metadata=exit_summary["metadata"],
            )
            await self._cleanup_live_exit_algos(symbol, inst_id=inst_id)
            await self._send_paper_trade_event(event)

        unexpected_positions = set(exchange_positions) - set(local_inst_ids)
        if unexpected_positions:
            await self._cancel_pending_orders(pending_orders)
            await self._enter_emergency(
                "live reconcile mismatch: local positions do not match OKX positions"
            )
            return False
        if pending_orders:
            await self._cancel_pending_orders(pending_orders)
            await self._enter_emergency("live reconcile found pending open orders")
            return False
        return True

    async def _exchange_exit_summary(self, inst_id: str, symbol: str) -> dict:
        """Use the exchange fill as the accounting source when a native exit closes a position."""
        fallback_price = self.account_state.open_position_mark_price.get(
            symbol, self.account_state.open_position_entry_price.get(symbol, 0)
        )
        fallback = {
            "fill_price": fallback_price,
            "reason": "OKX 持仓已关闭；未取得对应成交回报，暂以最后标记价同步。",
            "metadata": {},
        }
        if self.live_reconcile_client is None:
            return fallback
        signed_margin = self.account_state.open_positions.get(symbol, 0)
        history_summary = await self._exchange_position_history_exit_summary(inst_id, symbol, signed_margin)
        if history_summary:
            return history_summary
        exit_side = "sell" if signed_margin > 0 else "buy"
        try:
            fills = await self.live_reconcile_client.get_fills(inst_id, limit=100)
        except Exception:
            return fallback
        matching = [row for row in fills if str(row.get("side") or "").lower() == exit_side]
        if not matching:
            return fallback
        fill = matching[0]
        fill_price = self._positive_float(fill.get("fillPx")) or fallback_price
        contracts = self._positive_float(fill.get("fillSz")) or self.account_state.open_position_contracts.get(symbol)
        contract_value = self.account_state.open_position_contract_value.get(symbol)
        gross_pnl = self._optional_float(fill.get("fillPnl"))
        if not gross_pnl and contracts and contract_value:
            entry = self.account_state.open_position_entry_price.get(symbol, fill_price)
            gross_pnl = contracts * contract_value * (fill_price - entry) * (1 if signed_margin > 0 else -1)
        total_fee = self.account_state.open_position_entry_fee_amount.get(symbol, 0) + self._optional_float(fill.get("fee"))
        return {
            "fill_price": fill_price,
            "reason": "OKX 持仓已关闭，已按成交回报同步本地状态。",
            "metadata": {
                "exchange_contracts": contracts,
                "contract_value": contract_value,
                "exchange_gross_pnl_amount": gross_pnl,
                "exchange_fee_amount": total_fee,
                "exchange_net_pnl_amount": gross_pnl + total_fee,
            },
        }

    async def _exchange_position_history_exit_summary(
        self, inst_id: str, symbol: str, signed_margin: float
    ) -> dict | None:
        get_positions_history = getattr(self.live_reconcile_client, "get_positions_history", None)
        if get_positions_history is None:
            return None
        try:
            rows = await get_positions_history("SWAP", inst_id=inst_id, limit=20)
        except Exception:
            return None
        row = self._match_position_history_row(symbol, signed_margin, rows)
        if not row:
            return None
        fill_price = self._positive_float(row.get("closeAvgPx")) or self.account_state.open_position_mark_price.get(
            symbol, self.account_state.open_position_entry_price.get(symbol, 0)
        )
        return {
            "fill_price": fill_price,
            "reason": "OKX 持仓已关闭，已按 OKX 历史仓位 realizedPnl 同步本地状态。",
            "metadata": {
                "exchange_contracts": self._positive_float(row.get("closeTotalPos")),
                "exchange_gross_pnl_amount": self._optional_float(row.get("pnl")),
                "exchange_fee_amount": self._optional_float(row.get("fee")),
                "exchange_funding_fee_amount": self._optional_float(row.get("fundingFee")),
                "exchange_net_pnl_amount": self._optional_float(row.get("realizedPnl")),
            },
        }

    def _match_position_history_row(self, symbol: str, signed_margin: float, rows: list[dict]) -> dict | None:
        direction = "long" if signed_margin > 0 else "short"
        entry = self.account_state.open_position_entry_price.get(symbol)
        contracts = self.account_state.open_position_contracts.get(symbol)
        candidates = [row for row in rows if str(row.get("direction") or "").lower() == direction]
        if entry:
            price_matched = [
                row for row in candidates if self._prices_close(self._positive_float(row.get("openAvgPx")), entry)
            ]
            if price_matched:
                candidates = price_matched
        if contracts:
            size_matched = [
                row for row in candidates if self._sizes_close(self._positive_float(row.get("closeTotalPos")), contracts)
            ]
            if size_matched:
                candidates = size_matched
        if not candidates:
            return None
        return max(candidates, key=lambda row: int(str(row.get("uTime") or row.get("cTime") or "0")))

    @staticmethod
    def _prices_close(left: float | None, right: float, tolerance: float = 0.001) -> bool:
        if left is None or right <= 0:
            return False
        return abs(left - right) / right <= tolerance

    @staticmethod
    def _sizes_close(left: float | None, right: float, tolerance: float = 1e-9) -> bool:
        if left is None:
            return False
        return abs(left - right) <= max(tolerance, abs(right) * 1e-6)



    @staticmethod
    def _open_exchange_positions(rows: list[dict]) -> dict[str, float]:
        result: dict[str, float] = {}
        for row in rows:
            inst_id = str(row.get("instId") or "")
            try:
                size = float(row.get("pos") or 0)
            except (TypeError, ValueError):
                size = 0
            if inst_id and abs(size) > 0:
                result[inst_id] = size
        return result

    async def _cancel_pending_orders(self, rows: list[dict]) -> None:
        if self.live_reconcile_client is None:
            return
        for row in rows:
            inst_id = row.get("instId")
            if not inst_id:
                continue
            try:
                await self.live_reconcile_client.cancel_order(
                    str(inst_id),
                    ord_id=row.get("ordId"),
                    cl_ord_id=row.get("clOrdId"),
                )
            except Exception:
                continue

    async def _cleanup_live_exit_algos(self, symbol: str, inst_id: str | None = None) -> None:
        if self.live_reconcile_client is None:
            return
        inst_id = inst_id or self._swap_inst_id(symbol)
        rows = await self._pending_exit_algo_orders(inst_id)
        await self._cancel_pending_algo_orders(rows)

    async def _pending_exit_algo_orders(self, inst_id: str) -> list[dict]:
        get_pending_algo_orders = getattr(self.live_reconcile_client, "get_pending_algo_orders", None)
        if get_pending_algo_orders is None:
            return []
        rows: list[dict] = []
        for ord_type in self.EXIT_ALGO_ORDER_TYPES:
            try:
                rows.extend(await get_pending_algo_orders(ord_type, inst_id=inst_id))
            except Exception:
                continue
        return rows

    async def _cancel_pending_algo_orders(self, rows: list[dict]) -> None:
        if self.live_reconcile_client is None or not rows:
            return
        cancel_algo_orders = getattr(self.live_reconcile_client, "cancel_algo_orders", None)
        if cancel_algo_orders is None:
            return
        for index in range(0, len(rows), 10):
            try:
                await cancel_algo_orders(rows[index : index + 10])
            except Exception:
                continue

    async def _maybe_trigger_hard_stop(self, decision: RiskDecision) -> None:
        if self.runtime_state.emergency:
            return
        failed_checks = {
            "daily_loss_limit": "单日亏损达到上限",
            "drawdown_limit": "总回撤达到上限",
            "consecutive_losses": "连续亏损达到上限",
            "health": "交易所或数据状态异常",
        }
        for key, reason in failed_checks.items():
            if decision.risk_checks.get(key) == "fail":
                await self._enter_emergency(reason)
                return

    async def _record_order_failure_if_needed(self, execution_result) -> None:
        if execution_result.success:
            if execution_result.action != FinalAction.HOLD and self.runtime_state.order_failure_count:
                self.runtime_state.order_failure_count = 0
                self._persist_runtime_state()
            return
        self.runtime_state.order_failure_count += 1
        self._persist_runtime_state()
        if self.runtime_state.order_failure_count >= self.max_order_failures:
            await self._enter_emergency("订单失败次数达到上限")

    async def _enter_emergency(self, reason: str) -> None:
        self.runtime_state.mode = RiskMode.EMERGENCY
        self.runtime_state.global_paused = True
        self.runtime_state.emergency = True
        self.runtime_state.hard_stop_reason = reason
        self.runtime_state.hard_stop_at = utc_now()
        self._persist_runtime_state()
        if self.reporter is not None:
            await self.reporter.send_hard_stop_event(reason, self.runtime_state)

    def _persist_runtime_state(self) -> None:
        if self.runtime_state_path is None:
            return
        self.runtime_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_state_path.write_text(
            yaml.safe_dump(self.runtime_state.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def _reload_runtime_state(self) -> None:
        if self.runtime_state_path is None or not self.runtime_state_path.exists():
            return
        raw = yaml.safe_load(self.runtime_state_path.read_text(encoding="utf-8")) or {}
        loaded = RuntimeState.model_validate(raw)
        if loaded.execution_mode != self.runtime_state.execution_mode:
            raise RuntimeError(
                "execution_mode cannot change while the daemon is running; restart after changing execution mode"
            )
        self.runtime_state = loaded
        if self.risk_limits_loader is not None:
            limits = self.risk_limits_loader(loaded)
            if self.risk_engine is not None:
                self.risk_engine.limits = limits
            if self.paper_risk is not None:
                self.paper_risk.limits = limits

    @staticmethod
    def _swap_inst_id(symbol: str) -> str:
        if symbol.endswith("-SWAP"):
            return symbol
        if "-" in symbol:
            return f"{symbol}-SWAP"
        if symbol.endswith("USDT"):
            return f"{symbol[:-4]}-USDT-SWAP"
        return symbol


    async def _generate_strategy_signal(self, symbol: str, timeframe: str, market):
        if hasattr(self.strategy, "generate_signal_from_candles") and hasattr(self.market_data, "fetch_recent_candles"):
            candles = await self.market_data.fetch_recent_candles(symbol, timeframe)
            return self.strategy.generate_signal_from_candles(candles, market)
        return self.strategy.generate_signal(market)

    async def _apply_market_advisor(
        self,
        symbol: str,
        timeframe: str,
        market: MarketSnapshot,
        strategy_signal: StrategySignal,
    ) -> tuple[StrategySignal, object | None]:
        config = self.market_advisor_config
        if not config.enabled or self.market_advisor_client is None:
            return strategy_signal, None
        if self.runtime_state.execution_mode == ExecutionMode.LIVE and not config.early_candidate_live_enabled:
            return strategy_signal, None
        try:
            candles_by_timeframe = await self._advisor_candles(symbol, timeframe)
            direction_summary = await self._btc_eth_direction_summary(timeframe)
            should_trigger, trigger_reasons, trigger_details = self._market_advisor_trigger_reasons(
                symbol, strategy_signal, direction_summary
            )
            if not should_trigger:
                return strategy_signal, None
            payload = self._market_advisor_payload(symbol, strategy_signal, market, candles_by_timeframe, direction_summary)
            payload["trigger_reasons"] = trigger_reasons
            payload["trigger_details"] = trigger_details
            raw = await self.market_advisor_client.judge(payload)
            advisor_results = normalize_gateway_response(raw)
            meta_decision = normalize_meta_decision(raw)
            updated, audit = self.market_advisor_arbiter.apply(
                strategy_signal,
                market,
                candles_by_timeframe,
                advisor_results,
                meta_decision,
                direction_summary,
                early_trades_today=self._early_trades_today(symbol),
            )
            audit.request_id = str(raw.get("request_id") or payload["request_id"])
            audit.advisor_provider = config.provider
            audit.trigger_reasons = trigger_reasons
            audit.trigger_details = trigger_details
            self._record_market_advisor_trigger(symbol, trigger_reasons[0] if trigger_reasons else "unknown")
            if self.runtime_state.execution_mode == ExecutionMode.LIVE and not config.live_influence_enabled:
                audit.arbiter_decision.reject_reasons.append("live_influence_disabled")
                return strategy_signal, audit
            return updated, audit
        except Exception as exc:
            from trading_core.schemas import MarketAdvisorAudit, MarketAdvisorDecision

            return strategy_signal, MarketAdvisorAudit(
                symbol=symbol,
                strategy_signal=str(strategy_signal.signal),
                advisor_provider=config.provider,
                arbiter_decision=MarketAdvisorDecision(
                    candidate="none",
                    accepted=False,
                    reject_reasons=[f"market_advisor_error:{exc.__class__.__name__}"],
                ),
            )

    async def _advisor_candles(self, symbol: str, default_timeframe: str) -> dict[str, list[dict]]:
        candles: dict[str, list[dict]] = {}
        if not hasattr(self.market_data, "fetch_recent_candles"):
            return candles
        for advisor in self.market_advisor_config.advisors:
            timeframe = advisor.timeframe or default_timeframe
            try:
                rows = await self.market_data.fetch_recent_candles(symbol, timeframe, advisor.max_candles)
            except TypeError:
                rows = await self.market_data.fetch_recent_candles(symbol, timeframe)
            except Exception:
                rows = []
            candles[timeframe] = rows[-advisor.max_candles :]
        return candles

    async def _btc_eth_direction_summary(self, timeframe: str) -> dict[str, str]:
        if not self.market_advisor_config.btc_eth_confirmation_required:
            return {}
        summary: dict[str, str] = {}
        if not hasattr(self.market_data, "fetch_recent_candles"):
            return summary
        for major in ("BTCUSDT", "ETHUSDT"):
            try:
                rows = await self.market_data.fetch_recent_candles(major, timeframe, 30)
            except TypeError:
                rows = await self.market_data.fetch_recent_candles(major, timeframe)
            except Exception:
                rows = []
            summary[major] = self._direction_from_candles(rows)
        return summary

    def _market_advisor_payload(
        self,
        symbol: str,
        strategy_signal: StrategySignal,
        market: MarketSnapshot,
        candles_by_timeframe: dict[str, list[dict]],
        direction_summary: dict[str, str],
    ) -> dict:
        request_id = f"{symbol}-{market.timestamp.strftime('%Y%m%d%H%M%S')}"
        advisor_requests = []
        for advisor in self.market_advisor_config.advisors:
            candles = candles_by_timeframe.get(advisor.timeframe, [])[-advisor.max_candles :]
            advisor_requests.append(
                {
                    "request_id": request_id,
                    "symbol": symbol,
                    "timeframe": advisor.timeframe,
                    "advisor_type": advisor.advisor_type,
                    "strategy_signal": str(strategy_signal.signal),
                    "closed_candles": [self._safe_candle(row) for row in candles],
                    "market_snapshot": market.model_dump(mode="json"),
                    "local_regime_summary": self._regime_summary(strategy_signal),
                    "btc_eth_direction_summary": direction_summary,
                    "permission_boundaries": {
                        "can_trade": False,
                        "can_only_judge_market": True,
                        "cannot_access_account": True,
                        "cannot_access_files": True,
                        "cannot_change_risk": True,
                        "final_decision_by_code": True,
                    },
                }
            )
        return {
            "request_id": request_id,
            "symbol": symbol,
            "strategy_signal": str(strategy_signal.signal),
            "local_regime_summary": self._regime_summary(strategy_signal),
            "advisor_requests": advisor_requests,
        }

    def _market_advisor_trigger_reasons(
        self,
        symbol: str,
        strategy_signal: StrategySignal,
        direction_summary: dict[str, str],
    ) -> tuple[bool, list[str], dict]:
        reasons: list[str] = []
        details: dict = {}
        signal = strategy_signal.signal
        regime = strategy_signal.market_regime
        invalid = list(strategy_signal.invalid_reason or [])
        risk_flags = set(getattr(regime, "risk_flags", []) or []) if regime is not None else set()
        detailed = str(getattr(regime, "detailed_regime", "") or "")
        regime_name = str(getattr(regime, "regime", "") or "")
        pattern = str(getattr(regime, "pattern", "") or "")
        range_position = getattr(regime, "range_position", None) if regime is not None else None

        if signal in {StrategyAction.LONG, StrategyAction.SHORT}:
            reasons.append("pre_entry_signal")

        invalid_text = "|".join(invalid)
        if any(token in invalid_text for token in ("regime_block", "direction_conflict", "late_", "fresh_upper_break", "fresh_lower_break")):
            reasons.append("strategy_regime_conflict")

        if regime_name in {"transition", "shock", "choppy_high_vol"} or detailed in {
            "TRANSITION",
            "SHOCK",
            "CHOPPY_HIGH_VOL",
            "WATERFALL",
            "EXPANDING_RANGE",
            "BREAKOUT_UP",
            "BREAKOUT_DOWN",
            "RANGE_HIGH_VOL",
        }:
            reasons.append("regime_turn_or_danger")

        if signal == StrategyAction.HOLD:
            at_edge = isinstance(range_position, (int, float)) and (range_position <= 0.12 or range_position >= 0.88)
            fresh_break = bool(risk_flags & {"fresh_upper_break", "fresh_lower_break"})
            if at_edge or fresh_break:
                reasons.append("hold_near_structure")

        if symbol in self.account_state.open_positions and (
            regime_name in {"transition", "shock", "choppy_high_vol"} or risk_flags & {"shock", "transition", "range_expanding"}
        ):
            reasons.append("open_position_structure_review")

        btc = direction_summary.get("BTCUSDT", "unknown")
        eth = direction_summary.get("ETHUSDT", "unknown")
        if symbol not in {"BTCUSDT", "ETHUSDT"} and btc == eth and btc in {"up", "down"}:
            if (signal == StrategyAction.LONG and btc == "down") or (signal == StrategyAction.SHORT and btc == "up"):
                reasons.append("btc_eth_anchor_conflict")

        reasons = list(dict.fromkeys(reasons))
        details.update(
            {
                "signal": str(signal),
                "regime": regime_name,
                "detailed_regime": detailed,
                "pattern": pattern,
                "range_position": range_position,
                "risk_flags": sorted(risk_flags),
                "invalid_reason": invalid,
                "btc_eth_direction": direction_summary,
            }
        )
        if not reasons:
            return False, [], details
        allowed, limited_reason = self._market_advisor_rate_limit_ok(symbol, reasons[0])
        if not allowed:
            details["rate_limited_reason"] = limited_reason
            return False, reasons, details
        return True, reasons, details

    def _market_advisor_rate_limit_ok(self, symbol: str, reason: str) -> tuple[bool, str]:
        # When GPT availability is mandatory, every potential entry must make a
        # fresh model request. A cached/rate-limited advisor result cannot prove
        # that the model is still reachable at order time.
        if reason == "pre_entry_signal" and self.market_advisor_config.failure_policy == "block_new_entries":
            return True, ""
        now = utc_now()
        five_hours_ago = now - timedelta(hours=5)
        cooldown_ago = now - timedelta(minutes=self.market_advisor_config.trigger_cooldown_minutes)
        recent = [(ts, sym, item_reason) for ts, sym, item_reason in self._market_advisor_trigger_history if ts >= five_hours_ago]
        self._market_advisor_trigger_history = deque(recent, maxlen=500)
        if len(recent) >= self.market_advisor_config.max_triggers_global_5h:
            return False, "global_5h_limit"
        symbol_recent = [(ts, item_reason) for ts, sym, item_reason in recent if sym == symbol]
        quota_scope = symbol_recent
        if reason == "pre_entry_signal":
            quota_scope = [(ts, item_reason) for ts, item_reason in symbol_recent if item_reason == reason]
        if len(quota_scope) >= self.market_advisor_config.max_triggers_per_symbol_5h:
            return False, "symbol_5h_limit"
        if any(ts >= cooldown_ago and item_reason == reason for ts, item_reason in symbol_recent):
            return False, "symbol_reason_cooldown"
        return True, ""

    def _record_market_advisor_trigger(self, symbol: str, reason: str) -> None:
        self._market_advisor_trigger_history.append((utc_now(), symbol, reason))

    def _enforce_market_advisor_entry_gate(
        self,
        decision: RiskDecision,
        audit: MarketAdvisorAudit | None,
    ) -> RiskDecision:
        """Fail closed for new entries when the configured GPT advisor is unavailable.

        Close decisions intentionally bypass this gate so local/exchange risk exits
        remain available during a model, network, authentication, or gateway outage.
        """
        if decision.final_action not in {FinalAction.OPEN_LONG, FinalAction.OPEN_SHORT}:
            return decision
        if self.market_advisor_config.failure_policy != "block_new_entries":
            return decision

        failure_reason = self._market_advisor_failure_reason(audit)
        if failure_reason is None:
            return decision

        checks = dict(decision.risk_checks)
        checks["market_advisor_availability"] = "fail"
        if audit is not None:
            marker = f"ai_unavailable:{failure_reason}"
            if marker not in audit.arbiter_decision.reject_reasons:
                audit.arbiter_decision.reject_reasons.append(marker)
        return decision.model_copy(
            update={
                "allow_trade": False,
                "final_action": FinalAction.HOLD,
                "final_position_pct": 0,
                "final_leverage": 1,
                "risk_checks": checks,
                "reason": f"GPT/Market Advisor 不可用（{failure_reason}），已禁止新开仓；现有仓位仍可平仓。",
            }
        )

    def _market_advisor_failure_reason(self, audit: MarketAdvisorAudit | None) -> str | None:
        if not self.market_advisor_config.enabled:
            return "advisor_disabled"
        if self.market_advisor_client is None:
            return "advisor_client_missing"
        if audit is None:
            return "advisor_not_invoked"
        if audit.meta_decision is None:
            return "meta_decision_missing"
        if audit.meta_decision.status != "ok":
            return "meta_decision_error"

        expected_timeframes = {item.timeframe for item in self.market_advisor_config.advisors}
        actual_timeframes = set(audit.advisor_results)
        missing = sorted(expected_timeframes - actual_timeframes)
        if missing:
            return f"advisor_results_missing:{','.join(missing)}"
        failed = sorted(key for key, result in audit.advisor_results.items() if result.status != "ok")
        if failed:
            return f"advisor_results_error:{','.join(failed)}"
        return None

    @staticmethod
    def _safe_candle(row: dict) -> dict:
        timestamp = row.get("timestamp")
        if hasattr(timestamp, "isoformat"):
            timestamp = timestamp.isoformat()
        return {
            "timestamp": timestamp,
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("volume"),
        }

    @staticmethod
    def _regime_summary(strategy_signal: StrategySignal) -> dict:
        regime = strategy_signal.market_regime
        if regime is None:
            return {}
        return {
            "regime": regime.regime,
            "detailed_regime": regime.detailed_regime,
            "trend_direction": str(regime.trend_direction),
            "trend_efficiency": regime.trend_efficiency,
            "range_position": regime.range_position,
            "risk_flags": list(regime.risk_flags),
        }

    @staticmethod
    def _direction_from_candles(candles: list[dict]) -> str:
        if len(candles) < 6:
            return "unknown"
        first = float(candles[-6].get("close") or 0)
        last = float(candles[-1].get("close") or 0)
        if first <= 0:
            return "unknown"
        change = (last - first) / first
        if change > 0.003:
            return "up"
        if change < -0.003:
            return "down"
        return "flat"

    def _early_trades_today(self, symbol: str) -> int:
        if self.audit_logger is None:
            return 0
        path = self.audit_logger.log_dir / f"{self.runtime_state.execution_mode.value}_trade_events.jsonl"
        if not path.exists():
            return 0
        today = utc_now().date()
        count = 0
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("symbol") != symbol or row.get("strategy_type") != "llm_early_structure":
                        continue
                    if row.get("event") not in {"open_long", "open_short"}:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(row.get("timestamp")).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts.date() == today:
                        count += 1
        except OSError:
            return 0
        return count

    @staticmethod
    def _combine_event_alpha_signal(signal: StrategySignal, ai_analysis) -> StrategySignal:
        audit = SocialContributionAudit(
            technical_signal_before_social=signal.signal,
            final_signal_after_social=signal.signal,
            event_risk=ai_analysis.event_risk,
            market_relevance=ai_analysis.market_relevance,
            alpha_signal=ai_analysis.alpha_signal,
            alpha_confidence=ai_analysis.alpha_confidence,
            alpha_horizon=ai_analysis.alpha_horizon,
            event_type=ai_analysis.event_type,
            source_weight=ai_analysis.source_weight,
            freshness_score=ai_analysis.freshness_score,
            contribution_reason="social_alpha_observed_no_decision_change",
        )
        signal = signal.model_copy(update={"social_audit": audit})

        if signal.signal != StrategyAction.HOLD:
            if ai_analysis.alpha_signal in {StrategyAction.LONG, StrategyAction.SHORT}:
                aligned = ai_analysis.alpha_signal == signal.signal
                audit = audit.model_copy(
                    update={
                        "social_aligned_with_technical": aligned,
                        "contribution_reason": "social_alpha_aligned_with_technical"
                        if aligned
                        else "social_alpha_conflicts_with_technical",
                    }
                )
                if (
                    not aligned
                    and ai_analysis.alpha_confidence >= 0.70
                    and ai_analysis.source_weight >= 0.70
                    and ai_analysis.freshness_score >= 0.20
                    and ai_analysis.market_relevance
                    in {"direct_asset", "crypto_sector", "macro_market", "political_geopolitical"}
                ):
                    audit = audit.model_copy(
                        update={
                            "final_signal_after_social": StrategyAction.HOLD,
                            "social_changed_decision": True,
                            "social_blocked_or_degraded": True,
                            "contribution_reason": "high_confidence_social_alpha_conflict_degraded_technical_entry",
                        }
                    )
                    return signal.model_copy(
                        update={
                            "signal": StrategyAction.HOLD,
                            "confidence": 0,
                            "entry_reason": [],
                            "invalid_reason": signal.invalid_reason + ["social_alpha_conflict_degraded_entry"],
                            "suggested_position_pct": 0,
                            "social_audit": audit,
                        }
                    )
                return signal.model_copy(update={"social_audit": audit})
            return signal
        if TradingScheduler._is_defensive_social_short(ai_analysis, signal.market_regime):
            return TradingScheduler._defensive_social_short_signal(signal, ai_analysis, audit)
        if ai_analysis.should_block_trade or str(ai_analysis.event_risk) in {"high", "critical"}:
            if TradingScheduler._is_unconfirmed_defensive_social_short(ai_analysis, signal.market_regime):
                audit = audit.model_copy(
                    update={
                        "final_signal_after_social": StrategyAction.HOLD,
                        "social_changed_decision": True,
                        "social_blocked_or_degraded": True,
                        "contribution_reason": "social_alpha_short_blocked_unconfirmed_breakdown",
                    }
                )
                return signal.model_copy(
                    update={
                        "social_audit": audit,
                        "invalid_reason": signal.invalid_reason
                        + ["social_alpha_short_blocked_unconfirmed_breakdown"],
                    }
                )
            audit = audit.model_copy(update={"contribution_reason": "social_risk_delegated_to_risk_engine"})
            return signal.model_copy(update={"social_audit": audit})
        if ai_analysis.alpha_signal not in {StrategyAction.LONG, StrategyAction.SHORT}:
            return signal
        if ai_analysis.alpha_confidence < 0.65:
            return signal
        if ai_analysis.source_weight < 0.70 or ai_analysis.freshness_score < 0.20:
            return signal
        if ai_analysis.market_relevance not in {"direct_asset", "crypto_sector", "macro_market", "political_geopolitical"}:
            return signal

        regime = signal.market_regime
        if (
            ai_analysis.alpha_signal == StrategyAction.SHORT
            and regime is not None
            and regime.trend_direction != StrategyAction.SHORT
            and (
                regime.regime in {"range", "transition", "squeeze"}
                or str(getattr(regime, "detailed_regime", "")).upper() in {"TREND_UP", "BREAKOUT_UP"}
                or (
                    regime.regime == "shock"
                    and (
                        regime.trend_efficiency < 0.35
                        or getattr(regime, "range_position", 0.0) >= 0.65
                    )
                )
            )
        ):
            audit = audit.model_copy(
                update={
                    "final_signal_after_social": StrategyAction.HOLD,
                    "social_changed_decision": True,
                    "social_blocked_or_degraded": True,
                    "contribution_reason": "social_alpha_short_blocked_in_low_efficiency_non_downtrend_regime",
                }
            )
            return signal.model_copy(
                update={
                    "social_audit": audit,
                    "invalid_reason": signal.invalid_reason
                    + ["social_alpha_short_blocked_low_efficiency_non_downtrend"],
                }
            )

        if (
            ai_analysis.alpha_signal == StrategyAction.LONG
            and regime is not None
            and regime.trend_direction != StrategyAction.LONG
            and (
                regime.regime in {"range", "transition", "squeeze"}
                or str(getattr(regime, "detailed_regime", "")).upper() in {"TREND_DOWN", "BREAKOUT_DOWN", "WATERFALL"}
                or (
                    regime.regime == "shock"
                    and (
                        regime.trend_efficiency < 0.35
                        or getattr(regime, "range_position", 1.0) <= 0.35
                    )
                )
            )
        ):
            audit = audit.model_copy(
                update={
                    "final_signal_after_social": StrategyAction.HOLD,
                    "social_changed_decision": True,
                    "social_blocked_or_degraded": True,
                    "contribution_reason": "social_alpha_long_blocked_in_low_efficiency_non_uptrend_regime",
                }
            )
            return signal.model_copy(
                update={
                    "social_audit": audit,
                    "invalid_reason": signal.invalid_reason
                    + ["social_alpha_long_blocked_low_efficiency_non_uptrend"],
                }
            )

        risk_reward = 1.2
        atr_pct = signal.atr_pct or 0
        if ai_analysis.alpha_horizon == "scalp":
            stop_loss_pct = min(0.025, max(0.012, atr_pct * 1.5))
        else:
            stop_loss_pct = min(0.035, max(0.018, atr_pct * 2.0))
        audit = audit.model_copy(
            update={
                "final_signal_after_social": ai_analysis.alpha_signal,
                "social_changed_decision": True,
                "social_created_event_signal": True,
                "contribution_reason": "social_alpha_created_small_event_signal",
            }
        )
        return StrategySignal(
            timestamp=utc_now(),
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            signal=ai_analysis.alpha_signal,
            confidence=ai_analysis.alpha_confidence,
            entry_reason=[
                "event_alpha_signal",
                f"event_type={ai_analysis.event_type}",
                f"horizon={ai_analysis.alpha_horizon}",
                ai_analysis.alpha_reason,
            ],
            invalid_reason=signal.invalid_reason,
            suggested_position_pct=min(0.03, MAX_SINGLE_TRADE_MARGIN_PCT),
            stop_loss_pct=round(stop_loss_pct, 6),
            take_profit_pct=round(stop_loss_pct * risk_reward, 6),
            atr_pct=signal.atr_pct,
            risk_reward_ratio=risk_reward,
            market_regime=signal.market_regime,
            social_audit=audit,
        )

    @staticmethod
    def _is_defensive_social_short(ai_analysis, regime) -> bool:
        if ai_analysis.analysis_status not in {"ok", "heuristic"}:
            return False
        if ai_analysis.alpha_signal != StrategyAction.SHORT:
            return False
        if ai_analysis.alpha_confidence < 0.75 or ai_analysis.source_weight < 0.80 or ai_analysis.freshness_score < 0.50:
            return False
        if ai_analysis.market_relevance not in {"direct_asset", "crypto_sector", "macro_market", "political_geopolitical"}:
            return False
        detailed_regime = getattr(regime, "detailed_regime", "")
        if detailed_regime == "WATERFALL":
            return True
        if detailed_regime != "BREAKOUT_DOWN":
            return False
        risk_flags = set(getattr(regime, "risk_flags", []) or [])
        recent_low_break_count = int(getattr(regime, "recent_low_break_count", 0) or 0)
        return "fresh_lower_break" in risk_flags or recent_low_break_count >= 2

    @staticmethod
    def _is_unconfirmed_defensive_social_short(ai_analysis, regime) -> bool:
        if regime is None:
            return False
        if ai_analysis.alpha_signal != StrategyAction.SHORT:
            return False
        if getattr(regime, "detailed_regime", "") != "BREAKOUT_DOWN":
            return False
        return not TradingScheduler._is_defensive_social_short(ai_analysis, regime)

    @staticmethod
    def _defensive_social_short_signal(signal: StrategySignal, ai_analysis, audit: SocialContributionAudit) -> StrategySignal:
        atr_pct = signal.atr_pct or 0
        stop_loss_pct = min(0.035, max(0.012, atr_pct * 2.0))
        audit = audit.model_copy(
            update={
                "final_signal_after_social": StrategyAction.SHORT,
                "social_changed_decision": True,
                "social_created_event_signal": True,
                "contribution_reason": "defensive_social_short_confirmed_by_breakdown",
            }
        )
        return StrategySignal(
            timestamp=utc_now(),
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            signal=StrategyAction.SHORT,
            confidence=ai_analysis.alpha_confidence,
            entry_reason=[
                "defensive_social_short",
                f"event_type={ai_analysis.event_type}",
                f"structure={signal.market_regime.detailed_regime}",
                ai_analysis.alpha_reason,
            ],
            invalid_reason=signal.invalid_reason,
            suggested_position_pct=0.04,
            stop_loss_pct=round(stop_loss_pct, 6),
            take_profit_pct=round(stop_loss_pct * 1.5, 6),
            atr_pct=signal.atr_pct,
            risk_reward_ratio=1.5,
            defensive_social_short=True,
            market_regime=signal.market_regime,
            social_audit=audit,
        )
