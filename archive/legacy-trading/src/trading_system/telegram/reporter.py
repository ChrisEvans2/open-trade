from __future__ import annotations

import os
import sys

import httpx

from trading_core.schemas import CycleArtifacts, ExecutionMode, PaperTradeEvent, RuntimeState


class TelegramReporter:
    def __init__(self, bot_token: str | None = None, chat_id: str | None = None, enabled: bool = False):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_HOME_CHANNEL")
        self.enabled = enabled and bool(self.bot_token and self.chat_id)

    async def send_cycle_report(self, artifacts: CycleArtifacts) -> None:
        pass
    async def send_paper_trade_event(self, event: PaperTradeEvent) -> None:
        await self.send_trade_event(event, ExecutionMode.PAPER)

    async def send_trade_event(self, event: PaperTradeEvent, execution_mode: ExecutionMode | str) -> None:
        text = self.render_paper_trade_event(event)
        if ExecutionMode(execution_mode) == ExecutionMode.LIVE:
            text = text.replace("模拟", "实盘")
        await self._send_message(text)

    async def send_hard_stop_event(self, reason: str, state: RuntimeState) -> None:
        await self._send_message(self.render_hard_stop_event(reason, state))

    async def _send_message(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(url, json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"})
                if response.status_code >= 400:
                    print(
                        f"Telegram send failed: HTTP {response.status_code}: {response.text[:300]}",
                        file=sys.stderr,
                        flush=True,
                    )
        except httpx.HTTPError as exc:
            print(f"Telegram send failed: {exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)

    @staticmethod
    def render_cycle_report(artifacts: CycleArtifacts) -> str:
        lines = [
            f"*{artifacts.market.symbol} {artifacts.market.timeframe}*\n"
            f"Close: `{artifacts.market.close}`\n"
            f"Funding: `{artifacts.market.funding_rate}` next `{artifacts.market.next_funding_rate}`\n"
            f"Social: `{artifacts.ai_analysis.event_risk}` / `{artifacts.ai_analysis.sentiment}`\n"
            f"Advisor: `{artifacts.market_advisor.arbiter_decision.candidate if artifacts.market_advisor else 'none'}` "
            f"`accepted={artifacts.market_advisor.arbiter_decision.accepted if artifacts.market_advisor else False}`\n"
            f"Signal: `{artifacts.strategy_signal.signal}`\n"
            f"Decision: `{artifacts.risk_decision.final_action}` "
            f"`{artifacts.risk_decision.final_position_pct}` "
            f"`{artifacts.risk_decision.final_leverage}x`\n"
            f"Execution: `{artifacts.execution_result.success}`\n",
            f"Reason: {artifacts.risk_decision.reason}",
        ]
        if not artifacts.execution_result.success:
            lines.append(f"Execution error: {artifacts.execution_result.reason}")
            error_response = artifacts.execution_result.metadata.get("okx_error_response")
            if error_response:
                details = error_response.get("data") or []
                if details:
                    detail = details[0]
                    lines.append(f"OKX detail: `{detail.get('sCode', '-')}` {detail.get('sMsg', '-')}")
        return "\n".join(lines)

    @staticmethod
    def render_paper_trade_event(event: PaperTradeEvent) -> str:
        title = {
            "open_long": "模拟开多",
            "open_short": "模拟开空",
            "close": "模拟平仓",
            "funding_fee": "模拟资金费结算",
        }.get(event.event, "模拟持仓更新")
        lines = [
            f"*{title}: {event.symbol}*",
            f"来源: `{event.event_source}` / `{event.timeframe or '-'}`",
        ]
        if event.side:
            lines.append(f"方向: `{event.side}`")
        source_label = {
            "technical": "技术策略",
            "social": "消息面",
            "mixed": "技术+消息",
            "manual": "人工/交易所",
            "unknown": "未知",
        }.get(event.signal_source, event.signal_source)
        if event.event in {"open_long", "open_short", "close"}:
            lines.append(f"开仓来源: `{source_label}`")
        if event.technical_signal:
            lines.append(f"技术信号: `{event.technical_signal}`")
        if event.social_alpha_signal and str(event.social_alpha_signal) != "hold":
            lines.append(f"社媒信号: `{event.social_alpha_signal}`")
        if event.social_contribution_reason:
            lines.append(f"社媒影响: `{TelegramReporter._social_contribution_text(event.social_contribution_reason)}`")
        if event.entry_reasons:
            lines.append(f"入场依据: `{', '.join(event.entry_reasons[:4])}`")
        if event.advisor_judgement:
            lines.append(
                f"行情Advisor: `{event.advisor_judgement}` "
                f"置信度 `{event.advisor_confidence if event.advisor_confidence is not None else 0:.2f}`"
            )
        if event.margin_amount is not None and event.margin_pct is not None:
            lines.append(f"保证金: `{event.margin_amount:.2f} USDT` (`{event.margin_pct:.2%}`)")
        if event.leverage is not None:
            lines.append(f"杠杆: `{event.leverage:.2f}x`")
        if event.notional_amount is not None:
            lines.append(f"名义仓位: `{event.notional_amount:.2f} USDT`")
        if event.stop_loss_pct is not None:
            lines.append(f"策略止损: `{event.stop_loss_pct:.2%}`")
        if event.take_profit_pct is not None:
            lines.append(f"策略止盈: `{event.take_profit_pct:.2%}`")
        if event.trailing_stop_pct is not None:
            lines.append(f"移动止盈回撤: `{event.trailing_stop_pct:.2%}`")
        if event.entry_price is not None:
            lines.append(f"开仓价: `{event.entry_price}`")
        if event.event == "close" and event.fill_price is not None:
            lines.append(f"平仓价: `{event.fill_price}`")
        elif event.mark_price is not None:
            lines.append(f"标记价: `{event.mark_price}`")
        if event.exchange_net_pnl_amount is not None:
            pnl_pct = event.position_pnl_pct if event.position_pnl_pct is not None else 0
            lines.append(f"本仓盈亏(OKX): `{event.exchange_net_pnl_amount:+.2f} USDT` (`{pnl_pct:+.2%}`)")
            if event.exchange_gross_pnl_amount is not None:
                lines.append(f"OKX价格盈亏: `{event.exchange_gross_pnl_amount:+.4f} USDT`")
            if event.exchange_fee_amount is not None:
                lines.append(f"OKX手续费: `{event.exchange_fee_amount:+.4f} USDT`")
            if event.exchange_funding_fee_amount is not None:
                lines.append(f"OKX资金费: `{event.exchange_funding_fee_amount:+.4f} USDT`")
        elif event.pnl_amount is not None:
            pnl_pct = event.position_pnl_pct if event.position_pnl_pct is not None else 0
            lines.append(f"本仓盈亏: `{event.pnl_amount:+.2f} USDT` (`{pnl_pct:+.2%}`)")
        if event.funding_fee_amount is not None:
            lines.append(f"资金费收支: `{event.funding_fee_amount:+.4f} USDT`")
        if event.cumulative_funding_amount is not None:
            lines.append(f"累计资金费: `{event.cumulative_funding_amount:+.4f} USDT`")
        if event.realized_pnl_amount_after is not None and event.realized_pnl_pct_after is not None:
            lines.append(f"累计已实现: `{event.realized_pnl_amount_after:+.2f} USDT` (`{event.realized_pnl_pct_after:+.2%}`)")
        if event.funding_rate is not None:
            lines.append(f"资金费率: `{event.funding_rate:.6%}`")
        if event.reason:
            lines.append(f"原因: {event.reason}")
        return "\n".join(lines)

    @staticmethod
    def _social_contribution_text(reason: str) -> str:
        labels = {
            "social_alpha_observed_no_decision_change": "未改写技术信号",
            "social_alpha_created_small_event_signal": "消息面创建小仓位信号",
            "social_alpha_short_blocked_in_low_efficiency_non_downtrend_regime": "社媒短空被低效率/非下跌结构阻断",
            "social_alpha_long_blocked_low_efficiency_non_uptrend": "社媒做多被低效率/非上涨结构阻断",
            "social_alpha_long_blocked_in_low_efficiency_non_uptrend_regime": "社媒做多被低效率/非上涨结构阻断",
            "social_risk_delegated_to_risk_engine": "社媒风险交由风控处理",
        }
        return labels.get(reason, reason)

    @staticmethod
    def render_hard_stop_event(reason: str, state: RuntimeState) -> str:
        return "\n".join(
            [
                "*实盘硬停止触发*",
                f"原因: {reason}",
                f"execution_mode: `{state.execution_mode}`",
                f"mode: `{state.mode}`",
                f"global_paused: `{state.global_paused}`",
                f"emergency: `{state.emergency}`",
                "恢复: 人工复盘后使用 `!resume ALL`",
            ]
        )
