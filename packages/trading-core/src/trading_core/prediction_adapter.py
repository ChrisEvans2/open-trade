"""Prediction entry into existing risk and accounting code; no model or exchange I/O.

The caller owns the durable intent journal, authorization, market freshness and
order transport. This adapter never calls a technical entry strategy or Advisor.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_core.accounting import AccountBook
from trading_core.execution.paper_risk import PaperPositionRiskManager
from trading_core.parameters import TradeParameters
from trading_core.risk.engine import RiskEngine, RiskLimits
from trading_core.schemas import (
    AccountState,
    AIAnalysisResult,
    ExecutionResult,
    MarketRegimeSnapshot,
    MarketSnapshot,
    RiskDecision,
    RuntimeState,
    StrategyAction,
    StrategySignal,
)
from trading_core.strategy.risk_parameters import prediction_parameters


@dataclass(frozen=True)
class PreparedPrediction:
    signal: StrategySignal
    decision: RiskDecision
    trailing_stop_pct: float | None
    trailing_activation_pct: float | None


class EventBuffer:
    def __init__(self):
        self.events = []

    def save_trade_event(self, event, execution_mode):
        self.events.append(
            {
                "event": event.model_dump(mode="json"),
                "execution_mode": str(execution_mode),
            }
        )


class PredictionRiskAdapter:
    def __init__(
        self,
        limits: RiskLimits,
        strategy_config: TradeParameters,
        account: AccountState,
        runtime: RuntimeState,
    ):
        self.limits, self.strategy_config, self.account, self.runtime = (
            limits,
            strategy_config,
            account,
            runtime,
        )
        self.audit = EventBuffer()
        # Reuse the existing account application, funding, mark and trailing calculations.
        # No data/AI/exchange clients or file paths are attached to this accounting instance.
        self.book = AccountBook(
            account,
            runtime,
            RiskEngine(limits),
            PaperPositionRiskManager(limits),
            self.audit,
        )

    def prepare(
        self,
        action: str,
        style: str,
        market: MarketSnapshot,
        *,
        atr_pct: float,
        confidence: float,
        reason: str,
        social_risk: AIAnalysisResult,
        range_low: float | None = None,
        range_high: float | None = None,
    ) -> PreparedPrediction:
        mapped = {
            "OPEN_LONG": StrategyAction.LONG,
            "OPEN_SHORT": StrategyAction.SHORT,
            "CLOSE": StrategyAction.CLOSE,
            "HOLD": StrategyAction.HOLD,
        }[action]
        fields = {"suggested_position_pct": 0}
        regime = None
        if mapped in {StrategyAction.LONG, StrategyAction.SHORT}:
            fields = prediction_parameters(
                mapped,
                style,
                market.close,
                atr_pct,
                self.strategy_config,
                range_low=range_low,
                range_high=range_high,
            ).signal_fields()
            if style == "RANGE":
                regime = MarketRegimeSnapshot(
                    regime="range",
                    pattern="range_edge",
                    range_low=range_low,
                    range_high=range_high,
                    range_mid=(range_low + range_high) / 2,
                )
        signal = StrategySignal(
            timestamp=market.timestamp,
            symbol=market.symbol,
            timeframe=market.timeframe,
            signal=mapped,
            confidence=confidence,
            entry_reason=["prediction_parent", reason],
            market_regime=regime,
            **fields,
        )
        decision = self.book.risk_engine.evaluate(
            signal, social_risk, self.account, self.runtime
        )
        decision = decision.model_copy(
            update={"reason": decision.reason.replace("代码策略", "父Agent预测")}
        )
        return PreparedPrediction(
            signal,
            decision,
            self.book._entry_trailing_stop_pct(signal),
            self.book._entry_trailing_activation_pct(signal),
        )

    def apply_fill(
        self,
        prepared: PreparedPrediction,
        execution: ExecutionResult,
        market: MarketSnapshot,
    ):
        """Apply a confirmed fill once; caller deduplicates cumulative exchange observations."""
        signal = prepared.signal
        metadata = {
            **execution.metadata,
            "signal_source": "prediction_parent",
            "technical_signal": "",
            "entry_reasons": signal.entry_reason,
            "strategy_type": "range_mean_reversion"
            if signal.market_regime and signal.market_regime.regime == "range"
            else "trend",
        }
        if signal.market_regime and signal.market_regime.regime == "range":
            metadata.update(
                {
                    k: getattr(signal.market_regime, k)
                    for k in ("range_low", "range_high", "range_mid")
                }
            )
        self.book._apply_paper_execution(
            execution.action,
            market.symbol,
            prepared.decision.final_position_pct,
            prepared.decision.final_leverage,
            execution.fill_price or market.close,
            execution.success,
            event_source="prediction_intent",
            timeframe=market.timeframe,
            reason=execution.reason,
            stop_loss_pct=signal.stop_loss_pct,
            take_profit_pct=signal.take_profit_pct,
            trailing_stop_pct=prepared.trailing_stop_pct,
            trailing_activation_pct=prepared.trailing_activation_pct,
            execution_metadata=metadata,
        )
        return self.account

    def monitor(self, market: MarketSnapshot):
        self.book._settle_due_funding(market.symbol)
        self.book._update_position_market_metadata(market.symbol, market)
        self.book._mark_to_market(market.symbol, market.close)
        return self.book.paper_risk.evaluate_close(self.account, market)

    def apply_increment(
        self,
        prepared,
        execution,
        market,
        *,
        fraction: float,
        total_open_fraction: float = 1.0,
    ):
        """Apply an incremental confirmed fill; keep the unfilled position intact.

        fraction is a fraction of the originally requested order. For close orders
        the caller supplies the fraction of the CURRENT remaining position instead.
        Prices and fees in execution describe this increment, not cumulative fills.
        """
        from dataclasses import replace

        from trading_core.schemas import FinalAction

        if not 0 < fraction <= 1 or not 0 < total_open_fraction <= 1:
            raise ValueError("Invalid fill fraction")
        symbol = market.symbol
        before = self.account.model_copy(deep=True)
        if execution.action != FinalAction.CLOSE:
            adjusted = replace(
                prepared,
                decision=prepared.decision.model_copy(
                    update={
                        "final_position_pct": prepared.decision.final_position_pct
                        * fraction
                    }
                ),
            )
            self.apply_fill(adjusted, execution, market)
            if symbol in before.open_positions:
                # Merge this increment without resetting the existing position's path.
                added = self.account.model_copy(deep=True)
                for key in self.account.__class__.model_fields:
                    old, new = getattr(before, key), getattr(added, key)
                    if not isinstance(old, dict) or not key.startswith("open_position"):
                        continue
                    if symbol in old:
                        getattr(self.account, key)[symbol] = old[symbol]
                for key in (
                    "open_positions",
                    "open_position_principal",
                    "open_position_notional",
                    "open_position_contracts",
                    "open_position_entry_fee_amount",
                ):
                    old, new = getattr(before, key), getattr(added, key)
                    if symbol in new:
                        getattr(self.account, key)[symbol] = (
                            old.get(symbol, 0) + new[symbol]
                        )
                old_qty = before.open_position_contracts.get(
                    symbol, abs(before.open_positions[symbol])
                )
                new_qty = added.open_position_contracts.get(
                    symbol, abs(added.open_positions[symbol])
                )
                self.account.open_position_entry_price[symbol] = (
                    before.open_position_entry_price[symbol] * old_qty
                    + execution.fill_price * new_qty
                ) / (old_qty + new_qty)
            return self.account
        # The legacy close accounting correctly accounts for the sliced position.
        scalable = (
            "open_positions",
            "open_position_principal",
            "open_position_notional",
            "open_position_contracts",
            "open_position_entry_fee_amount",
            "open_position_unrealized_pnl_amount",
            "open_position_cumulative_funding_amount",
        )
        for key in scalable:
            values = getattr(self.account, key)
            if symbol in values:
                values[symbol] *= fraction
        self.apply_fill(prepared, execution, market)
        if fraction < 1:
            for key in self.account.__class__.model_fields:
                values = getattr(before, key)
                if (
                    isinstance(values, dict)
                    and key.startswith("open_position")
                    and symbol in values
                ):
                    getattr(self.account, key)[symbol] = (
                        values[symbol] * (1 - fraction)
                        if key in scalable
                        else values[symbol]
                    )
        self.book._recalculate_total_unrealized()
        return self.account
