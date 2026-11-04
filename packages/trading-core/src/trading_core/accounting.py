"""Deterministic position accounting extracted from the retired scheduler.

The runtime persists the resulting account and events in its own guarded transaction.
This object has no scheduling, model, exchange, or filesystem write capability.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from trading_core.schemas import (
    FinalAction,
    PaperTradeEvent,
    StrategySignal,
    utc_now,
)


class AccountBook:
    def __init__(
        self, account_state, runtime_state, risk_engine, paper_risk, audit_logger
    ):
        self.account_state = account_state
        self.runtime_state = runtime_state
        self.risk_engine = risk_engine
        self.paper_risk = paper_risk
        self.audit_logger = audit_logger
        self.account_state_path = None

    def _persist_account_state(self):
        # Persistence belongs to the caller; never write a parallel file ledger.
        return None

    def _apply_paper_execution(
        self,
        action: FinalAction,
        symbol: str,
        position_pct: float,
        leverage: float,
        fill_price: float,
        success: bool,
        event_source: str = "strategy_cycle",
        timeframe: str | None = None,
        reason: str = "",
        stop_loss_pct: float | None = None,
        take_profit_pct: float | None = None,
        trailing_stop_pct: float | None = None,
        trailing_activation_pct: float | None = None,
        execution_metadata: dict | None = None,
    ) -> PaperTradeEvent | None:
        if not success:
            return None
        event: PaperTradeEvent | None = None
        execution_metadata = execution_metadata or {}
        if (
            action in {FinalAction.OPEN_LONG, FinalAction.OPEN_SHORT}
            and position_pct > 0
        ):
            self.account_state.open_positions[symbol] = (
                position_pct if action == FinalAction.OPEN_LONG else -position_pct
            )
            self.account_state.open_position_leverage[symbol] = leverage
            self.account_state.open_position_entry_price[symbol] = fill_price
            self.account_state.open_position_mark_price[symbol] = fill_price
            self.account_state.open_position_unrealized_pnl_pct[symbol] = 0
            contracts = self._positive_float(
                execution_metadata.get("exchange_contracts")
            )
            contract_value = self._positive_float(
                execution_metadata.get("contract_value")
            )
            actual_notional = (
                contracts * contract_value * fill_price
                if contracts and contract_value
                else None
            )
            self.account_state.open_position_principal[symbol] = (
                actual_notional / leverage
                if actual_notional is not None and leverage > 0
                else self.account_state.equity * position_pct
            )
            self.account_state.open_position_notional[symbol] = (
                actual_notional or self.account_state.equity * position_pct * leverage
            )
            if contracts and contract_value:
                self.account_state.open_position_contracts[symbol] = contracts
                self.account_state.open_position_contract_value[symbol] = contract_value
            entry_fee = self._optional_float(
                execution_metadata.get("exchange_fee_amount")
            )
            if entry_fee:
                self.account_state.open_position_entry_fee_amount[symbol] = entry_fee
            self.account_state.open_position_unrealized_pnl_amount[symbol] = 0
            if stop_loss_pct is not None:
                self.account_state.open_position_stop_loss_pct[symbol] = stop_loss_pct
            if take_profit_pct is not None:
                self.account_state.open_position_take_profit_pct[symbol] = (
                    take_profit_pct
                )
            if trailing_stop_pct is not None:
                self.account_state.open_position_trailing_stop_pct[symbol] = (
                    trailing_stop_pct
                )
            if trailing_activation_pct is not None:
                self.account_state.open_position_trailing_activation_pct[symbol] = (
                    trailing_activation_pct
                )
            self.account_state.open_position_funding_rate[symbol] = None
            self.account_state.open_position_next_funding_rate[symbol] = None
            self.account_state.open_position_funding_time[symbol] = None
            self.account_state.open_position_funding_inst_id[symbol] = None
            self.account_state.open_position_last_funding_time[symbol] = None
            self.account_state.open_position_cumulative_funding_amount[symbol] = 0
            self.account_state.open_position_high_watermark[symbol] = fill_price
            self.account_state.open_position_low_watermark[symbol] = fill_price
            self.account_state.open_position_signal_source[symbol] = str(
                execution_metadata.get("signal_source") or "unknown"
            )
            self.account_state.open_position_technical_signal[symbol] = str(
                execution_metadata.get("technical_signal") or ""
            )
            self.account_state.open_position_social_alpha_signal[symbol] = str(
                execution_metadata.get("social_alpha_signal") or ""
            )
            self.account_state.open_position_social_contribution_reason[symbol] = str(
                execution_metadata.get("social_contribution_reason") or ""
            )
            self.account_state.open_position_entry_reasons[symbol] = list(
                execution_metadata.get("entry_reasons") or []
            )
            self.account_state.open_position_strategy_type[symbol] = str(
                execution_metadata.get("strategy_type") or ""
            )
            self.account_state.open_position_regime[symbol] = str(
                execution_metadata.get("regime") or ""
            )
            self.account_state.open_position_detailed_regime[symbol] = str(
                execution_metadata.get("detailed_regime") or ""
            )
            range_quality = self._optional_float(
                execution_metadata.get("range_quality_score")
            )
            if range_quality is not None:
                self.account_state.open_position_range_quality_score[symbol] = (
                    range_quality
                )
            risk_flags = execution_metadata.get("risk_flags")
            if isinstance(risk_flags, list):
                self.account_state.open_position_risk_flags[symbol] = [
                    str(item) for item in risk_flags
                ]
            range_mid = self._positive_float(execution_metadata.get("range_mid"))
            range_low = self._positive_float(execution_metadata.get("range_low"))
            range_high = self._positive_float(execution_metadata.get("range_high"))
            if range_mid:
                self.account_state.open_position_range_mid[symbol] = range_mid
            if range_low:
                self.account_state.open_position_range_low[symbol] = range_low
            if range_high:
                self.account_state.open_position_range_high[symbol] = range_high
            event = self._record_paper_trade_event(
                action.value,
                symbol,
                timeframe,
                fill_price,
                reason,
                event_source,
                execution_metadata=execution_metadata,
            )
        elif action == FinalAction.CLOSE:
            exchange_gross_pnl = execution_metadata.get("exchange_gross_pnl_amount")
            exchange_fee = execution_metadata.get("exchange_fee_amount")
            exchange_funding_fee = execution_metadata.get("exchange_funding_fee_amount")
            exchange_net_pnl = execution_metadata.get("exchange_net_pnl_amount")
            if exchange_net_pnl is not None:
                exchange_net_pnl = self._optional_float(exchange_net_pnl)
            elif exchange_gross_pnl is not None:
                exchange_net_pnl = (
                    self._optional_float(exchange_gross_pnl)
                    + self.account_state.open_position_entry_fee_amount.get(symbol, 0)
                    + self._optional_float(exchange_fee)
                    + self._optional_float(exchange_funding_fee)
                )
            self._apply_realized_pnl(symbol, fill_price, exchange_net_pnl)
            event = self._record_paper_trade_event(
                action.value,
                symbol,
                timeframe,
                fill_price,
                reason,
                event_source,
                exchange_gross_pnl_amount=self._optional_float(exchange_gross_pnl)
                if exchange_gross_pnl is not None
                else None,
                exchange_fee_amount=(
                    self._optional_float(exchange_fee)
                    if exchange_gross_pnl is not None
                    else None
                ),
                exchange_funding_fee_amount=(
                    self._optional_float(exchange_funding_fee)
                    if exchange_funding_fee is not None
                    else None
                ),
                exchange_net_pnl_amount=exchange_net_pnl,
            )
            self.account_state.open_positions.pop(symbol, None)
            self.account_state.open_position_leverage.pop(symbol, None)
            self.account_state.open_position_entry_price.pop(symbol, None)
            self.account_state.open_position_mark_price.pop(symbol, None)
            self.account_state.open_position_unrealized_pnl_pct.pop(symbol, None)
            self.account_state.open_position_principal.pop(symbol, None)
            self.account_state.open_position_notional.pop(symbol, None)
            self.account_state.open_position_contracts.pop(symbol, None)
            self.account_state.open_position_contract_value.pop(symbol, None)
            self.account_state.open_position_entry_fee_amount.pop(symbol, None)
            self.account_state.open_position_unrealized_pnl_amount.pop(symbol, None)
            self.account_state.open_position_stop_loss_pct.pop(symbol, None)
            self.account_state.open_position_take_profit_pct.pop(symbol, None)
            self.account_state.open_position_trailing_stop_pct.pop(symbol, None)
            self.account_state.open_position_trailing_activation_pct.pop(symbol, None)
            self.account_state.open_position_funding_rate.pop(symbol, None)
            self.account_state.open_position_next_funding_rate.pop(symbol, None)
            self.account_state.open_position_funding_time.pop(symbol, None)
            self.account_state.open_position_funding_inst_id.pop(symbol, None)
            self.account_state.open_position_last_funding_time.pop(symbol, None)
            self.account_state.open_position_cumulative_funding_amount.pop(symbol, None)
            self.account_state.open_position_high_watermark.pop(symbol, None)
            self.account_state.open_position_low_watermark.pop(symbol, None)
            self.account_state.open_position_signal_source.pop(symbol, None)
            self.account_state.open_position_technical_signal.pop(symbol, None)
            self.account_state.open_position_social_alpha_signal.pop(symbol, None)
            self.account_state.open_position_social_contribution_reason.pop(
                symbol, None
            )
            self.account_state.open_position_entry_reasons.pop(symbol, None)
            self.account_state.open_position_strategy_type.pop(symbol, None)
            self.account_state.open_position_regime.pop(symbol, None)
            self.account_state.open_position_detailed_regime.pop(symbol, None)
            self.account_state.open_position_range_quality_score.pop(symbol, None)
            self.account_state.open_position_risk_flags.pop(symbol, None)
            self.account_state.open_position_range_mid.pop(symbol, None)
            self.account_state.open_position_range_low.pop(symbol, None)
            self.account_state.open_position_range_high.pop(symbol, None)
            self._recalculate_total_unrealized()
        if self.account_state_path:
            self.account_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.account_state_path.write_text(
                self.account_state.model_dump_json(indent=2), encoding="utf-8"
            )
        return event

    def _record_paper_trade_event(
        self,
        event: str,
        symbol: str,
        timeframe: str | None,
        fill_price: float,
        reason: str,
        event_source: str = "mark_sync",
        execution_metadata: dict | None = None,
        exchange_gross_pnl_amount: float | None = None,
        exchange_fee_amount: float | None = None,
        exchange_funding_fee_amount: float | None = None,
        exchange_net_pnl_amount: float | None = None,
    ) -> PaperTradeEvent | None:
        if self.audit_logger is None:
            return None
        execution_metadata = execution_metadata or {}
        signed_margin = self.account_state.open_positions.get(symbol)
        if signed_margin is None:
            return None
        margin_pct = abs(signed_margin)
        side_multiplier = 1 if signed_margin > 0 else -1
        side = "long" if signed_margin > 0 else "short"
        entry = self.account_state.open_position_entry_price.get(symbol)
        leverage = self.account_state.open_position_leverage.get(symbol, 1.0)
        max_favorable_excursion_pct = None
        max_adverse_excursion_pct = None
        if entry and entry > 0:
            high_watermark = self.account_state.open_position_high_watermark.get(
                symbol, entry
            )
            low_watermark = self.account_state.open_position_low_watermark.get(
                symbol, entry
            )
            if side == "long":
                max_favorable_excursion_pct = max(0.0, (high_watermark - entry) / entry)
                max_adverse_excursion_pct = max(0.0, (entry - low_watermark) / entry)
            else:
                max_favorable_excursion_pct = max(0.0, (entry - low_watermark) / entry)
                max_adverse_excursion_pct = max(0.0, (high_watermark - entry) / entry)
        position_pnl_pct = None
        account_pnl_pct = None
        pnl_amount = None
        if entry and entry > 0:
            contracts = self.account_state.open_position_contracts.get(symbol)
            contract_value = self.account_state.open_position_contract_value.get(symbol)
            if contracts and contract_value:
                pnl_amount = (
                    contracts * contract_value * (fill_price - entry) * side_multiplier
                )
                notional = self.account_state.open_position_notional.get(symbol, 0)
                position_pnl_pct = pnl_amount / notional if notional else None
                account_pnl_pct = (
                    pnl_amount / self.account_state.equity
                    if self.account_state.equity
                    else None
                )
            else:
                position_pnl_pct = (
                    ((fill_price - entry) / entry) * side_multiplier * leverage
                )
                account_pnl_pct = position_pnl_pct * margin_pct
                pnl_amount = self.account_state.equity * account_pnl_pct
        if exchange_net_pnl_amount is not None:
            pnl_amount = exchange_net_pnl_amount
            account_pnl_pct = (
                pnl_amount / self.account_state.equity
                if self.account_state.equity
                else None
            )
        signal_source = str(
            execution_metadata.get("signal_source")
            or self.account_state.open_position_signal_source.get(symbol)
            or "unknown"
        )
        technical_signal = execution_metadata.get(
            "technical_signal"
        ) or self.account_state.open_position_technical_signal.get(symbol)
        social_alpha_signal = execution_metadata.get(
            "social_alpha_signal"
        ) or self.account_state.open_position_social_alpha_signal.get(symbol)
        social_contribution_reason = str(
            execution_metadata.get("social_contribution_reason")
            or self.account_state.open_position_social_contribution_reason.get(symbol)
            or ""
        )
        entry_reasons = list(
            execution_metadata.get("entry_reasons")
            or self.account_state.open_position_entry_reasons.get(symbol)
            or []
        )
        strategy_type = execution_metadata.get(
            "strategy_type"
        ) or self.account_state.open_position_strategy_type.get(symbol)
        regime = execution_metadata.get(
            "regime"
        ) or self.account_state.open_position_regime.get(symbol)
        detailed_regime = execution_metadata.get(
            "detailed_regime"
        ) or self.account_state.open_position_detailed_regime.get(symbol)
        risk_flags = (
            execution_metadata.get("risk_flags")
            or self.account_state.open_position_risk_flags.get(symbol)
            or []
        )
        range_quality_score = execution_metadata.get("range_quality_score")
        if range_quality_score is None:
            range_quality_score = (
                self.account_state.open_position_range_quality_score.get(symbol)
            )
        range_mid = execution_metadata.get(
            "range_mid"
        ) or self.account_state.open_position_range_mid.get(symbol)
        range_low = execution_metadata.get(
            "range_low"
        ) or self.account_state.open_position_range_low.get(symbol)
        range_high = execution_metadata.get(
            "range_high"
        ) or self.account_state.open_position_range_high.get(symbol)
        trade_event = PaperTradeEvent(
            event_source=event_source,
            event=event,
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            reason=reason,
            entry_price=entry,
            fill_price=fill_price,
            mark_price=self.account_state.open_position_mark_price.get(
                symbol, fill_price
            ),
            margin_pct=margin_pct,
            margin_amount=self.account_state.open_position_principal.get(
                symbol, self.account_state.equity * margin_pct
            ),
            leverage=leverage,
            notional_amount=self.account_state.open_position_notional.get(
                symbol, self.account_state.equity * margin_pct * leverage
            ),
            stop_loss_pct=self.account_state.open_position_stop_loss_pct.get(symbol),
            take_profit_pct=self.account_state.open_position_take_profit_pct.get(
                symbol
            ),
            trailing_stop_pct=self.account_state.open_position_trailing_stop_pct.get(
                symbol
            ),
            trailing_activation_pct=self.account_state.open_position_trailing_activation_pct.get(
                symbol
            ),
            max_favorable_excursion_pct=max_favorable_excursion_pct,
            max_adverse_excursion_pct=max_adverse_excursion_pct,
            position_pnl_pct=position_pnl_pct,
            account_pnl_pct=account_pnl_pct,
            pnl_amount=pnl_amount,
            equity=self.account_state.equity,
            realized_pnl_pct_after=self.account_state.realized_pnl_pct,
            realized_pnl_amount_after=self.account_state.realized_pnl_amount,
            total_unrealized_pnl_pct_after=self.account_state.total_unrealized_pnl_pct,
            total_unrealized_pnl_amount_after=self.account_state.total_unrealized_pnl_amount,
            funding_rate=self.account_state.open_position_funding_rate.get(symbol),
            next_funding_rate=self.account_state.open_position_next_funding_rate.get(
                symbol
            ),
            funding_time=self.account_state.open_position_funding_time.get(symbol),
            funding_inst_id=self.account_state.open_position_funding_inst_id.get(
                symbol
            ),
            cumulative_funding_amount=self.account_state.open_position_cumulative_funding_amount.get(
                symbol
            ),
            exchange_gross_pnl_amount=exchange_gross_pnl_amount,
            exchange_fee_amount=exchange_fee_amount,
            exchange_funding_fee_amount=exchange_funding_fee_amount,
            exchange_net_pnl_amount=exchange_net_pnl_amount,
            signal_source=signal_source,
            technical_signal=technical_signal or None,
            social_alpha_signal=social_alpha_signal or None,
            social_changed_decision=execution_metadata.get("social_changed_decision"),
            social_contribution_reason=social_contribution_reason,
            entry_reasons=entry_reasons,
            strategy_type=strategy_type or None,
            advisor_judgement=execution_metadata.get("advisor_judgement"),
            advisor_confidence=self._optional_float(
                execution_metadata.get("advisor_confidence")
            )
            if execution_metadata.get("advisor_confidence") is not None
            else None,
            advisor_confirmed_by_code=execution_metadata.get(
                "advisor_confirmed_by_code"
            ),
            advisor_timeframes=[
                str(item) for item in execution_metadata.get("advisor_timeframes", [])
            ],
            regime=regime or None,
            detailed_regime=detailed_regime or None,
            risk_flags=[str(item) for item in risk_flags]
            if isinstance(risk_flags, list)
            else [],
            range_quality_score=self._optional_float(range_quality_score)
            if range_quality_score is not None
            else None,
            range_mid=self._optional_float(range_mid)
            if range_mid is not None
            else None,
            range_low=self._optional_float(range_low)
            if range_low is not None
            else None,
            range_high=self._optional_float(range_high)
            if range_high is not None
            else None,
        )
        self.audit_logger.save_trade_event(
            trade_event, self.runtime_state.execution_mode
        )
        return trade_event

    def _settle_due_funding(self, symbol: str) -> PaperTradeEvent | None:
        signed_margin = self.account_state.open_positions.get(symbol)
        if signed_margin is None:
            return None
        funding_rate = self.account_state.open_position_funding_rate.get(symbol)
        funding_time = self.account_state.open_position_funding_time.get(symbol)
        if funding_rate is None or funding_time is None:
            return None
        funding_time = (
            funding_time
            if funding_time.tzinfo
            else funding_time.replace(tzinfo=ZoneInfo("UTC"))
        )
        if utc_now() < funding_time:
            return None
        if (
            self.account_state.open_position_last_funding_time.get(symbol)
            == funding_time
        ):
            return None

        side_multiplier = 1 if signed_margin > 0 else -1
        notional = self.account_state.open_position_notional.get(symbol)
        if notional is None:
            margin_pct = abs(signed_margin)
            leverage = self.account_state.open_position_leverage.get(symbol, 1.0)
            notional = self.account_state.equity * margin_pct * leverage
            self.account_state.open_position_notional[symbol] = notional

        funding_fee = -side_multiplier * notional * funding_rate
        account_pnl_pct = (
            funding_fee / self.account_state.equity
            if self.account_state.equity > 0
            else 0
        )
        self.account_state.realized_pnl_pct += account_pnl_pct
        self.account_state.realized_pnl_amount = (
            self.account_state.equity * self.account_state.realized_pnl_pct
        )
        self.account_state.open_position_last_funding_time[symbol] = funding_time
        self.account_state.open_position_cumulative_funding_amount[symbol] = (
            self.account_state.open_position_cumulative_funding_amount.get(symbol, 0)
            + funding_fee
        )
        event = self._record_funding_fee_event(symbol, funding_fee, account_pnl_pct)
        self._persist_account_state()
        return event

    def _record_funding_fee_event(
        self, symbol: str, funding_fee: float, account_pnl_pct: float
    ) -> PaperTradeEvent | None:
        if self.audit_logger is None:
            return None
        signed_margin = self.account_state.open_positions.get(symbol)
        if signed_margin is None:
            return None
        margin_pct = abs(signed_margin)
        side = "long" if signed_margin > 0 else "short"
        trade_event = PaperTradeEvent(
            event_source="funding_sync",
            event="funding_fee",
            symbol=symbol,
            side=side,
            reason="模拟资金费率结算",
            entry_price=self.account_state.open_position_entry_price.get(symbol),
            fill_price=self.account_state.open_position_mark_price.get(symbol),
            mark_price=self.account_state.open_position_mark_price.get(symbol),
            margin_pct=margin_pct,
            margin_amount=self.account_state.open_position_principal.get(
                symbol, self.account_state.equity * margin_pct
            ),
            leverage=self.account_state.open_position_leverage.get(symbol, 1.0),
            notional_amount=self.account_state.open_position_notional.get(symbol),
            account_pnl_pct=account_pnl_pct,
            pnl_amount=funding_fee,
            equity=self.account_state.equity,
            realized_pnl_pct_after=self.account_state.realized_pnl_pct,
            realized_pnl_amount_after=self.account_state.realized_pnl_amount,
            total_unrealized_pnl_pct_after=self.account_state.total_unrealized_pnl_pct,
            total_unrealized_pnl_amount_after=self.account_state.total_unrealized_pnl_amount,
            funding_rate=self.account_state.open_position_funding_rate.get(symbol),
            next_funding_rate=self.account_state.open_position_next_funding_rate.get(
                symbol
            ),
            funding_time=self.account_state.open_position_funding_time.get(symbol),
            funding_inst_id=self.account_state.open_position_funding_inst_id.get(
                symbol
            ),
            funding_fee_amount=funding_fee,
            cumulative_funding_amount=self.account_state.open_position_cumulative_funding_amount.get(
                symbol
            ),
        )
        self.audit_logger.save_trade_event(
            trade_event, self.runtime_state.execution_mode
        )
        return trade_event

    def _entry_trailing_stop_pct(self, signal: StrategySignal) -> float | None:
        if self.paper_risk is None:
            return None
        if signal.market_regime is not None and signal.market_regime.regime == "range":
            return 0.0
        atr_pct = self._entry_atr_pct(signal)
        return max(
            self.paper_risk.limits.trailing_drawdown_min_pct,
            atr_pct * self.paper_risk.limits.trailing_drawdown_atr_multiplier,
        )

    def _entry_trailing_activation_pct(self, signal: StrategySignal) -> float | None:
        if self.paper_risk is None:
            return None
        if signal.market_regime is not None and signal.market_regime.regime == "range":
            return 0.0
        atr_pct = self._entry_atr_pct(signal)
        stop_loss_pct = signal.stop_loss_pct or self.paper_risk.limits.stop_loss_pct
        activation = max(
            self.paper_risk.limits.trailing_activation_min_pct,
            atr_pct * self.paper_risk.limits.trailing_activation_atr_multiplier,
            stop_loss_pct * self.paper_risk.limits.trailing_activation_r_multiple,
        )
        return min(activation, self.paper_risk.limits.trailing_activation_max_pct)

    def _entry_atr_pct(self, signal: StrategySignal) -> float:
        if signal.atr_pct is not None and signal.atr_pct > 0:
            return signal.atr_pct
        stop_loss_pct = signal.stop_loss_pct or self.paper_risk.limits.stop_loss_pct
        return max(stop_loss_pct / 2, 0.000001)

    def _mark_to_market(self, symbol: str, mark_price: float) -> None:
        signed_margin = self.account_state.open_positions.get(symbol)
        entry = self.account_state.open_position_entry_price.get(symbol)
        if signed_margin is None or not entry:
            return
        side = 1 if signed_margin > 0 else -1
        leverage = self.account_state.open_position_leverage.get(symbol, 1.0)
        contracts = self.account_state.open_position_contracts.get(symbol)
        contract_value = self.account_state.open_position_contract_value.get(symbol)
        pnl_amount = None
        if contracts and contract_value:
            pnl_amount = contracts * contract_value * (mark_price - entry) * side
            pnl_pct = (
                pnl_amount / self.account_state.equity
                if self.account_state.equity
                else 0
            )
        else:
            pnl_pct = (
                ((mark_price - entry) / entry) * side * leverage * abs(signed_margin)
            )
        margin_pct = abs(signed_margin)
        self.account_state.open_position_mark_price[symbol] = mark_price
        self.account_state.open_position_unrealized_pnl_pct[symbol] = pnl_pct
        if not (contracts and contract_value):
            self.account_state.open_position_principal[symbol] = (
                self.account_state.equity * margin_pct
            )
            self.account_state.open_position_notional[symbol] = (
                self.account_state.equity * margin_pct * leverage
            )
        self.account_state.open_position_unrealized_pnl_amount[symbol] = (
            pnl_amount
            if pnl_amount is not None
            else self.account_state.equity * pnl_pct
        )
        self._touch_mark_update()
        self._recalculate_total_unrealized()
        if self.account_state_path:
            self.account_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.account_state_path.write_text(
                self.account_state.model_dump_json(indent=2), encoding="utf-8"
            )

    def _recalculate_total_unrealized(self) -> None:
        self.account_state.total_unrealized_pnl_pct = sum(
            self.account_state.open_position_unrealized_pnl_pct.values()
        )
        self.account_state.total_unrealized_pnl_amount = sum(
            self.account_state.open_position_unrealized_pnl_amount.values()
        )

    def _apply_realized_pnl(
        self,
        symbol: str,
        fill_price: float,
        exchange_net_pnl_amount: float | None = None,
    ) -> None:
        signed_margin = self.account_state.open_positions.get(symbol)
        entry = self.account_state.open_position_entry_price.get(symbol)
        if signed_margin is None or not entry:
            return
        side = 1 if signed_margin > 0 else -1
        if exchange_net_pnl_amount is None:
            contracts = self.account_state.open_position_contracts.get(symbol)
            contract_value = self.account_state.open_position_contract_value.get(symbol)
            if contracts and contract_value:
                exchange_net_pnl_amount = (
                    contracts * contract_value * (fill_price - entry) * side
                )
            else:
                leverage = self.account_state.open_position_leverage.get(symbol, 1.0)
                exchange_net_pnl_amount = self.account_state.equity * (
                    ((fill_price - entry) / entry)
                    * side
                    * leverage
                    * abs(signed_margin)
                )
        self.account_state.realized_pnl_amount += exchange_net_pnl_amount
        self.account_state.realized_pnl_pct = (
            self.account_state.realized_pnl_amount / self.account_state.equity
            if self.account_state.equity
            else 0
        )

    def _update_position_market_metadata(self, symbol: str, market) -> None:
        if symbol not in self.account_state.open_positions:
            return
        self.account_state.open_position_funding_rate[symbol] = market.funding_rate
        self.account_state.open_position_next_funding_rate[symbol] = (
            market.next_funding_rate
        )
        self.account_state.open_position_funding_time[symbol] = market.funding_time
        self.account_state.open_position_funding_inst_id[symbol] = (
            market.funding_inst_id
        )

    @staticmethod
    def _optional_float(value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _positive_float(cls, value) -> float | None:
        parsed = cls._optional_float(value)
        return parsed if parsed > 0 else None

    def _touch_mark_update(self) -> None:
        updated = utc_now()
        self.account_state.last_mark_update = updated
        self.account_state.last_mark_update_timezone = "Asia/Shanghai"
        self.account_state.last_mark_update_local = updated.astimezone(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%Y-%m-%d %H:%M:%S")
