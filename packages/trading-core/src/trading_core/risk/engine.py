from __future__ import annotations

from dataclasses import dataclass

from trading_core.schemas import (
    MAX_SINGLE_TRADE_MARGIN_PCT,
    AccountState,
    AIAnalysisResult,
    FinalAction,
    RiskDecision,
    RiskMode,
    RuntimeState,
    SocialRisk,
    StrategyAction,
    StrategySignal,
)


@dataclass(frozen=True)
class RiskLimits:
    max_position_pct: float = 0.10
    min_position_margin: float = 5.0
    min_leverage: float = 1.0
    max_leverage: float = 1.0
    max_daily_loss_pct: float = 0.02
    max_single_trade_loss_pct: float = 0.005
    max_drawdown_pct: float = 0.05
    max_consecutive_losses: int = 3
    max_open_positions: int = 1
    max_same_direction_positions: int = 2
    max_same_direction_margin_pct: float = 0.20
    max_notional_pct: float = 0.60
    max_slippage_pct: float = 0.001
    stop_loss_pct: float = 0.015
    take_profit_pct: float = 0.03
    trailing_stop_pct: float = 0.012
    trailing_activation_min_pct: float = 0.008
    trailing_activation_max_pct: float = 0.020
    trailing_activation_atr_multiplier: float = 1.0
    trailing_activation_r_multiple: float = 1.0
    trailing_drawdown_min_pct: float = 0.004
    trailing_drawdown_atr_multiplier: float = 0.6
    allow_short: bool = True
    allow_new_entries: bool = True


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()

    def evaluate(
        self,
        signal: StrategySignal,
        ai: AIAnalysisResult,
        account: AccountState,
        runtime_state: RuntimeState,
    ) -> RiskDecision:
        checks: dict[str, str] = {}
        mode = RiskMode.EMERGENCY if runtime_state.emergency else runtime_state.mode
        if (
            signal.signal == StrategyAction.CLOSE
            and signal.symbol in account.open_positions
        ):
            checks["reduce_only_close"] = "pass"
            return RiskDecision(
                allow_trade=True,
                final_action=FinalAction.CLOSE,
                final_position_pct=abs(account.open_positions[signal.symbol]),
                final_leverage=account.open_position_leverage.get(signal.symbol, 1.0),
                risk_mode=mode,
                risk_checks=checks,
                reason="只允许 reduce-only/close 平仓。",
            )

        if runtime_state.global_paused or mode in {RiskMode.PAUSED, RiskMode.EMERGENCY}:
            checks["runtime_mode"] = "block_new_entries"
            return self._hold(
                mode, checks, "系统处于暂停或 emergency 模式，只允许人工处理/平仓。"
            )
        if signal.symbol in runtime_state.paused_symbols:
            checks["symbol_pause"] = "fail"
            return self._hold(mode, checks, f"{signal.symbol} 已暂停交易。")
        if not account.exchange_healthy or not account.data_healthy:
            checks["health"] = "fail"
            return self._hold(mode, checks, "交易所或数据健康检查失败。")
        if account.daily_pnl_pct <= -self.limits.max_daily_loss_pct:
            checks["daily_loss_limit"] = "fail"
            return self._hold(mode, checks, "触发单日最大亏损限制。")
        if account.drawdown_pct >= self.limits.max_drawdown_pct:
            checks["drawdown_limit"] = "fail"
            return self._hold(mode, checks, "触发最大回撤限制。")
        if account.consecutive_losses >= self.limits.max_consecutive_losses:
            checks["consecutive_losses"] = "fail"
            return self._hold(mode, checks, "连续亏损次数达到限制。")
        if signal.signal == StrategyAction.HOLD:
            checks["strategy_signal"] = "hold"
            if "advisor_blocked_technical_signal" in signal.invalid_reason:
                checks["advisor_review"] = "blocked_technical_signal"
                judgement = signal.advisor_judgement or "unknown"
                checks["advisor_decision"] = judgement
                label = {
                    "wait_pullback": "等待回踩",
                    "reject": "拒绝入场",
                    "reduce_risk": "降低风险但存在入场冲突",
                }.get(judgement, judgement)
                return self._hold(
                    mode,
                    checks,
                    f"技术策略曾发出开仓信号，但被 Market Advisor 否决（{label}）；本轮不新开仓。",
                )
            return self._hold(mode, checks, "代码策略无交易信号。")

        is_new_entry = signal.signal in {StrategyAction.LONG, StrategyAction.SHORT}
        if is_new_entry and not self.limits.allow_new_entries:
            checks["new_entries"] = "fail"
            return self._hold(mode, checks, "当前风控配置禁止新开仓。")
        if signal.signal == StrategyAction.SHORT and not self.limits.allow_short:
            checks["short_allowed"] = "fail"
            return self._hold(mode, checks, "当前风控配置禁止做空。")
        if (
            is_new_entry
            and len(account.open_positions) >= self.limits.max_open_positions
            and signal.symbol not in account.open_positions
        ):
            checks["max_open_positions"] = "fail"
            return self._hold(mode, checks, "同时持仓数量达到上限。")
        if is_new_entry and signal.symbol in account.open_positions:
            checks["duplicate_order_check"] = "fail"
            return self._hold(mode, checks, "已有同 symbol 持仓，禁止重复开仓。")
        if is_new_entry:
            target_side = 1 if signal.signal == StrategyAction.LONG else -1
            same_direction = [
                value
                for value in account.open_positions.values()
                if value * target_side > 0
            ]
            same_direction_margin = sum(abs(value) for value in same_direction)
            if len(same_direction) >= self.limits.max_same_direction_positions:
                checks["same_direction_positions"] = "fail"
                return self._hold(
                    mode, checks, "同方向持仓数量达到上限，避免相关币种集中暴露。"
                )
            if same_direction_margin >= self.limits.max_same_direction_margin_pct:
                checks["same_direction_margin"] = "fail"
                return self._hold(
                    mode, checks, "同方向保证金占比达到上限，避免相关币种集中暴露。"
                )

        defensive_social_short = (
            signal.defensive_social_short and signal.signal == StrategyAction.SHORT
        )
        if ai.should_block_trade or ai.event_risk in {
            SocialRisk.HIGH,
            SocialRisk.CRITICAL,
        }:
            if not defensive_social_short:
                checks["social_risk_filter"] = "blocked"
                return self._hold(
                    mode, checks, f"社媒风险为 {ai.event_risk}，阻止新开仓。"
                )
            checks["social_risk_filter"] = "defensive_short_reduced_position"
            multiplier = 0.5
        else:
            multiplier = self._social_multiplier(ai, checks)

        sizing = self._risk_sizing(signal, mode, multiplier, account)
        position_pct = sizing["margin_pct"]
        leverage = sizing["leverage"]
        if position_pct <= 0:
            checks["position_size"] = "below_min_margin"
            return self._hold(
                mode,
                checks,
                f"动态仓位低于最低保证金 {self.limits.min_position_margin:.2f} USDT，跳过新开仓。",
            )

        checks.setdefault("daily_loss_limit", "pass")
        checks.setdefault("max_position_limit", "pass")
        checks.setdefault("duplicate_order_check", "pass")
        checks["dynamic_sizing"] = "pass"
        action = (
            FinalAction.OPEN_LONG
            if signal.signal == StrategyAction.LONG
            else FinalAction.OPEN_SHORT
        )
        return RiskDecision(
            allow_trade=True,
            final_action=action,
            final_position_pct=position_pct,
            final_leverage=leverage,
            risk_mode=mode,
            risk_checks=checks,
            reason=(
                f"代码策略 {signal.signal} 成立，社媒风险 {ai.event_risk}，"
                f"技术止损 {sizing['stop_loss_pct']:.2%}，风险预算 {sizing['risk_budget_pct']:.2%}，"
                f"保证金 {position_pct:.2%}，名义仓位 {sizing['notional_pct']:.2%}，动态杠杆 {leverage:.2f}x。"
            ),
        )

    def _risk_sizing(
        self,
        signal: StrategySignal,
        mode: RiskMode,
        multiplier: float,
        account: AccountState,
    ) -> dict[str, float]:
        stop_loss_pct = signal.stop_loss_pct or self.limits.stop_loss_pct
        stop_loss_pct = max(stop_loss_pct, 0.000001)
        signal_risk_budget = signal.risk_budget_pct
        base_risk_budget_pct = (
            min(self.limits.max_single_trade_loss_pct, signal_risk_budget)
            if signal_risk_budget is not None
            else self.limits.max_single_trade_loss_pct
        )
        risk_budget_pct = max(0, base_risk_budget_pct * multiplier)
        min_margin_pct = (
            self.limits.min_position_margin / account.equity
            if account.equity > 0
            else 0
        )
        max_margin_pct = (
            min(
                signal.suggested_position_pct,
                self.limits.max_position_pct,
                self._mode_position_cap(mode),
                MAX_SINGLE_TRADE_MARGIN_PCT,
            )
            * multiplier
        )
        max_margin_pct = max(0, max_margin_pct)
        target_notional_pct = min(
            risk_budget_pct / stop_loss_pct if stop_loss_pct > 0 else 0,
            self.limits.max_notional_pct,
        )
        if (
            target_notional_pct <= 0
            or max_margin_pct <= 0
            or min_margin_pct > max_margin_pct
        ):
            return {
                "margin_pct": 0,
                "leverage": 1.0,
                "notional_pct": 0,
                "stop_loss_pct": stop_loss_pct,
                "risk_budget_pct": risk_budget_pct,
                "min_margin_pct": min_margin_pct,
            }

        signal_leverage_cap = signal.max_leverage_override
        max_leverage_limit = (
            min(self.limits.max_leverage, signal_leverage_cap)
            if signal_leverage_cap is not None
            else self.limits.max_leverage
        )
        max_leverage = max(1.0, max_leverage_limit)
        min_leverage = min(max(1.0, self.limits.min_leverage), max_leverage)
        min_notional_pct = min_margin_pct * min_leverage
        if target_notional_pct < min_notional_pct:
            return {
                "margin_pct": 0,
                "leverage": 1.0,
                "notional_pct": 0,
                "stop_loss_pct": stop_loss_pct,
                "risk_budget_pct": risk_budget_pct,
                "min_margin_pct": min_margin_pct,
            }
        if target_notional_pct <= max_margin_pct * min_leverage:
            leverage = min_leverage
            margin_pct = target_notional_pct / leverage
            notional_pct = target_notional_pct
        else:
            required_leverage = target_notional_pct / max_margin_pct
            leverage = min(max_leverage, max(min_leverage, required_leverage))
            margin_pct = min(
                max_margin_pct, max(min_margin_pct, target_notional_pct / leverage)
            )
            notional_pct = margin_pct * leverage

        return {
            "margin_pct": round(margin_pct, 6),
            "leverage": round(leverage, 6),
            "notional_pct": round(notional_pct, 6),
            "stop_loss_pct": stop_loss_pct,
            "risk_budget_pct": risk_budget_pct,
            "min_margin_pct": min_margin_pct,
        }

    def _social_multiplier(self, ai: AIAnalysisResult, checks: dict[str, str]) -> float:
        if ai.event_risk == SocialRisk.LOW:
            checks["social_risk_filter"] = "pass"
            return min(1.0, ai.position_multiplier)
        if ai.event_risk == SocialRisk.MEDIUM:
            checks["social_risk_filter"] = "reduced_position"
            return min(0.5, ai.position_multiplier)
        return 0

    @staticmethod
    def _mode_position_cap(mode: RiskMode) -> float:
        if mode == RiskMode.CAUTIOUS:
            return 0.03
        if mode == RiskMode.PAPER:
            return 0.10
        if mode == RiskMode.NORMAL:
            return 0.10
        return 0

    @staticmethod
    def _hold(mode: RiskMode, checks: dict[str, str], reason: str) -> RiskDecision:
        return RiskDecision(
            allow_trade=False,
            final_action=FinalAction.HOLD,
            final_position_pct=0,
            final_leverage=1.0,
            risk_mode=mode,
            risk_checks=checks,
            reason=reason,
        )
