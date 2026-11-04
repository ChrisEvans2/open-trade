from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from pydantic import ValidationError

from trading_core.config import MarketAdvisorConfig
from trading_core.schemas import (
    MarketAdvisorAudit,
    MarketAdvisorDecision,
    MarketAdvisorResult,
    MarketSnapshot,
    MetaMarketAdvisorDecision,
    StrategyAction,
    StrategySignal,
)


ADVISOR_PROMPTS = {
    "macro_regime": """你是 12h 大级别行情环境分析器，不是交易执行器。
你只能根据输入的脱敏 K 线摘要判断 macro regime、方向偏向、风险等级、关键支撑/压力。
你不能要求访问账户、文件、密钥、工具或本地路径；不能下单、不能提高仓位或杠杆。
最终是否开仓由本地代码和 RiskEngine 决定。
必须输出字段：judgement, confidence, macro_regime, direction_bias, risk_level, major_resistance_nearby, major_support_nearby, reason。
judgement 只能是 confirm, warn, block, neutral。macro_regime 只能是 clean_trend, choppy_trend, range, reversal, shock, unknown。
direction_bias 只能是 bullish, bearish, neutral, unknown。risk_level 只能是 low, medium, high, unknown。
必须只输出一个 JSON 对象。reason 用中文短句。""",
    "structure_bias": """你是 4h 中期结构分析器，不是交易执行器。
你只能判断结构方向、趋势健康度、关键结构位和失效位。
你不能要求访问账户、文件、密钥、工具或本地路径；不能下单、不能提高仓位或杠杆。
最终是否开仓由本地代码和 RiskEngine 决定。
必须输出字段：judgement, confidence, structure_bias, trend_health, structure_level, invalidation_price, reason。
judgement 只能是 confirm, warn, block, neutral。structure_bias 只能是 up, down, range, unclear。
trend_health 只能是 strong, weak, exhausted, unknown。
必须只输出一个 JSON 对象。reason 用中文短句。""",
    "early_candidate": """你是 1h 提前结构候选分析器，不是交易执行器。
当代码策略为 HOLD 时，你可以判断是否存在 early_long_candidate 或 early_short_candidate。
你只能提出候选，不能直接开仓；候选必须由本地结构突破、成交量、大盘确认、止损距离和 RiskEngine 二次确认。
你不能要求访问账户、文件、密钥、工具或本地路径；不能下单、不能提高仓位或杠杆。
必须输出字段：judgement, confidence, key_level, invalidation_price, reason。
judgement 只能是 early_long_candidate, early_short_candidate, confirm, warn, block, neutral。
必须只输出一个 JSON 对象。reason 用中文短句。""",
    "execution_quality": """你是 15m 入场质量分析器，不判断大方向。
你只能判断当前入场是否追价、是否容易被插针/扫损、是否应等待回踩。
你不能要求访问账户、文件、密钥、工具或本地路径；不能下单、不能提高仓位或杠杆。
最终是否开仓由本地代码和 RiskEngine 决定。
必须输出字段：judgement, confidence, execution_quality, entry_timing, overextended, liquidity_sweep_risk, reason。
judgement 只能是 confirm, warn, block, neutral。execution_quality 只能是 good, acceptable, bad, unknown。
entry_timing 只能是 now, wait_pullback, avoid, unknown。
必须只输出一个 JSON 对象。reason 用中文短句。""",
}


META_ADVISOR_PROMPT = """你是 MetaMarketAdvisor，多周期行情裁定器，不是交易执行器。
你会收到 12h/4h/1h/15m 四个 advisor 的结构化判断，以及脱敏的策略信号和权限边界。
你的任务是合并多周期观点，输出是否允许 early candidate、是否等待回踩、是否拒绝或降低风险。
不要简单投票；重点判断：1h 是否提出候选，4h/12h 是否支持或至少不强烈反向，15m 是否适合入场。
你不能下单，不能要求访问文件、账户、密钥、工具或本地路径，不能提高全局风险。
CodeSafetyGate 拥有一票否决权，RiskEngine 是最终放行者。
risk_budget_multiplier 用来降低风险，不得超过 1。强共振可用 0.5，普通通过 0.3，轻微冲突 0.2，等待回踩/拒绝用 0。
max_leverage_override 不指定时必须为 null；指定时必须是大于等于 1 的数值。等待回踩/拒绝不能用 0 表示杠杆，仍用 null。
必须只输出一个 JSON 对象，字段固定：
status, request_id, symbol, timeframe_set, decision, direction, confidence,
risk_budget_multiplier, max_leverage_override, entry_timing, main_reason,
supporting_timeframes, conflicting_timeframes, risk_flags, required_code_checks。
decision 只能是 allow_candidate, wait_pullback, reject, reduce_risk, neutral。
direction 只能是 long, short, none。
entry_timing 只能是 now, wait_pullback, avoid, unknown。
required_code_checks 只能从这些值中选择：
breakout_confirmed, volume_confirmed, btc_eth_confirmed, stop_loss_valid,
rr_valid, not_overextended, daily_limit_ok, risk_engine_approved。
main_reason 用中文短句。"""


class MarketAdvisorClient:
    def __init__(self, config: MarketAdvisorConfig):
        self.config = config

    async def judge(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(self.config.endpoint, json=payload)
            response.raise_for_status()
        return response.json()


class CodexCliMarketAdvisorClient:
    def __init__(self, config: MarketAdvisorConfig, cwd: str | Path = "/srv/open-trade-deployment"):
        self.config = config
        self.cwd = Path(cwd)

    async def judge(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or uuid4().hex)
        advisor_requests = payload.get("advisor_requests") or []
        results: dict[str, dict[str, Any]] = {}
        tasks = []
        for request in advisor_requests:
            tasks.append(self._judge_one(request_id, request))
        for request, result in zip(advisor_requests, await asyncio.gather(*tasks)):
            key = str(request.get("timeframe") or request.get("name") or len(results))
            results[key] = result
        meta_decision = await self._judge_meta(request_id, payload, results)
        return {"request_id": request_id, "advisor_results": results, "meta_decision": meta_decision}

    async def _judge_one(self, request_id: str, request: dict[str, Any]) -> dict[str, Any]:
        advisor_type = str(request.get("advisor_type") or "early_candidate")
        prompt = (
            ADVISOR_PROMPTS.get(advisor_type, ADVISOR_PROMPTS["early_candidate"])
            + "\n\n只根据下面 JSON 判断行情。不要读取文件，不要调用工具，不要下单。最终只输出 JSON 对象。\n"
            + json.dumps(request, ensure_ascii=False)
        )
        try:
            raw = await self._run_codex_json(prompt)
            normalized = MarketAdvisorGateway._normalize(request_id, request, raw)
            MarketAdvisorResult.model_validate(normalized)
            return normalized
        except Exception as exc:
            return MarketAdvisorGateway._neutral(request_id, request, f"{exc.__class__.__name__}: {str(exc)[:180]}")

    async def _judge_meta(self, request_id: str, payload: dict[str, Any], advisor_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        meta_payload = {
            "request_id": request_id,
            "symbol": payload.get("symbol") or MarketAdvisorGateway._payload_symbol(payload),
            "timeframe_set": sorted(advisor_results.keys()),
            "strategy_signal": payload.get("strategy_signal"),
            "advisor_results": advisor_results,
            "local_regime_summary": payload.get("local_regime_summary", {}),
            "trigger_reasons": payload.get("trigger_reasons", []),
            "instruction": "四个周期由子分析完成。请作为主行情分析 agent 综合判断，不要用固定周期权重或简单投票；重点判断右侧进场质量、周期冲突、是否等待回踩。",
            "permission_boundaries": {
                "can_trade": False,
                "can_only_decide_candidate": True,
                "code_safety_gate_has_veto": True,
                "risk_engine_final_approval": True,
                "cannot_access_account": True,
                "cannot_access_files": True,
                "cannot_change_risk": True,
            },
        }
        prompt = META_ADVISOR_PROMPT + "\n\n最终只输出 JSON 对象。\n" + json.dumps(meta_payload, ensure_ascii=False)
        try:
            raw = await self._run_codex_json(prompt)
            normalized = MarketAdvisorGateway._normalize_meta(request_id, meta_payload, raw)
            MetaMarketAdvisorDecision.model_validate(normalized)
            return normalized
        except Exception as exc:
            return MarketAdvisorGateway._neutral_meta(request_id, meta_payload, f"{exc.__class__.__name__}: {str(exc)[:180]}")

    async def _run_codex_json(self, prompt: str) -> dict[str, Any]:
        schema = self._codex_output_schema()
        with tempfile.TemporaryDirectory(prefix="trade-codex-advisor-") as tmpdir:
            tmp = Path(tmpdir)
            schema_path = tmp / "schema.json"
            output_path = tmp / "last_message.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            command = [
                self.config.codex_command,
                "exec",
            ]
            if self.config.codex_model:
                command.extend(["-m", self.config.codex_model])
            command.extend(
                [
                "--cd",
                str(self.cwd),
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
                ]
            )
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=self.config.codex_timeout_seconds,
            )
            if proc.returncode != 0:
                detail = (stderr or stdout).decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"codex exec failed: {detail}")
            text = output_path.read_text(encoding="utf-8") if output_path.exists() else stdout.decode("utf-8", errors="replace")
            return self._extract_json_object(text)

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any]:
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                raise
            data = json.loads(text[start : end + 1])
        if not isinstance(data, dict):
            raise TypeError("codex output must be a JSON object")
        return data

    @staticmethod
    def _codex_output_schema() -> dict[str, Any]:
        nullable_string = {"type": ["string", "null"]}
        nullable_number = {"type": ["number", "null"]}
        nullable_boolean = {"type": ["boolean", "null"]}
        nullable_string_array = {"type": ["array", "null"], "items": {"type": "string"}}
        judgement_enum = ["early_long_candidate", "early_short_candidate", "confirm", "warn", "block", "neutral", None]
        properties = {
            "status": {"enum": ["ok", "error"]},
            "request_id": nullable_string,
            "symbol": nullable_string,
            "timeframe": nullable_string,
            "timeframe_set": nullable_string_array,
            "advisor_type": nullable_string,
            "macro_regime": {"enum": ["clean_trend", "choppy_trend", "range", "reversal", "shock", "unknown", None]},
            "direction_bias": {"enum": ["bullish", "bearish", "neutral", "unknown", None]},
            "risk_level": {"enum": ["low", "medium", "high", "unknown", None]},
            "major_resistance_nearby": nullable_boolean,
            "major_support_nearby": nullable_boolean,
            "structure_bias": {"enum": ["up", "down", "range", "unclear", None]},
            "trend_health": {"enum": ["strong", "weak", "exhausted", "unknown", None]},
            "judgement": {"enum": judgement_enum},
            "confidence": nullable_number,
            "structure_level": nullable_number,
            "key_level": nullable_number,
            "invalidation_price": nullable_number,
            "execution_quality": {"enum": ["good", "acceptable", "bad", "unknown", None]},
            "entry_timing": {"enum": ["now", "wait_pullback", "avoid", "unknown", None]},
            "overextended": nullable_boolean,
            "liquidity_sweep_risk": nullable_boolean,
            "decision": nullable_string,
            "direction": nullable_string,
            "risk_budget_multiplier": nullable_number,
            "max_leverage_override": {
                "type": ["number", "null"],
                "minimum": 1,
                "description": "不指定杠杆上限时使用 null（包括等待或拒绝）；指定时必须 >= 1，不能用 0。",
            },
            "main_reason": nullable_string,
            "supporting_timeframes": nullable_string_array,
            "conflicting_timeframes": nullable_string_array,
            "risk_flags": nullable_string_array,
            "required_code_checks": nullable_string_array,
            "fallback": nullable_string,
            "reason": nullable_string,
        }
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }


class CodeSafetyGate:
    def __init__(self, config: MarketAdvisorConfig):
        self.config = config

    def apply(
        self,
        signal: StrategySignal,
        market: MarketSnapshot,
        candles_by_timeframe: dict[str, list[dict]],
        advisor_results: dict[str, MarketAdvisorResult],
        meta_decision: MetaMarketAdvisorDecision | None,
        btc_eth_direction: dict[str, str],
        early_trades_today: int = 0,
    ) -> tuple[StrategySignal, MarketAdvisorAudit]:
        request_id = uuid4().hex
        decision = MarketAdvisorDecision(candidate="none")
        audit = MarketAdvisorAudit(
            request_id=request_id,
            symbol=signal.symbol,
            strategy_signal=str(signal.signal),
            advisor_results=advisor_results,
            meta_decision=meta_decision,
            arbiter_decision=decision,
        )

        if meta_decision is None or meta_decision.status != "ok":
            decision.reject_reasons.append("missing_or_invalid_meta_decision")
            return signal, audit

        if signal.signal != StrategyAction.HOLD:
            return self._apply_to_technical_signal(signal, meta_decision, audit)

        if meta_decision.decision not in {"allow_candidate", "reduce_risk"}:
            decision.candidate = meta_decision.decision
            decision.reject_reasons.append(f"meta_decision_{meta_decision.decision}")
            return signal, audit

        if meta_decision.direction not in {"long", "short"}:
            decision.candidate = meta_decision.decision
            decision.reject_reasons.append("meta_direction_none")
            return signal, audit

        if meta_decision.entry_timing in {"avoid", "wait_pullback"}:
            decision.candidate = meta_decision.decision
            decision.reject_reasons.append(f"meta_entry_timing_{meta_decision.entry_timing}")
            return signal, audit

        candidate_side = StrategyAction.LONG if meta_decision.direction == "long" else StrategyAction.SHORT
        decision.candidate = f"meta_{meta_decision.decision}_{meta_decision.direction}"
        if meta_decision.confidence < self.config.min_model_confidence:
            decision.reject_reasons.append("model_confidence_too_low")
            return signal, audit

        if early_trades_today >= self.config.max_early_trades_per_symbol_per_day:
            decision.reject_reasons.append("daily_early_trade_limit")
            return signal, audit

        main_candles = candles_by_timeframe.get(signal.timeframe) or candles_by_timeframe.get("1h") or []
        result_1h = self._result_by_type_or_timeframe(advisor_results, "early_candidate", signal.timeframe)
        sizing = self._run_code_safety_checks(
            candidate_side,
            signal,
            market,
            main_candles,
            result_1h,
            meta_decision,
            btc_eth_direction,
            decision,
        )
        audit.risk_sizing = sizing

        if decision.reject_reasons:
            return signal, audit

        stop_loss_pct = sizing.get("stop_pct") or signal.stop_loss_pct or self.config.max_stop_loss_pct
        take_profit_pct = round(stop_loss_pct * self.config.min_reward_risk_ratio, 6)
        decision.accepted = True
        decision.passed_rules.append("code_safety_gate_accepted")
        risk_budget_pct = self.config.early_candidate_risk_pct * sizing.get("risk_budget_multiplier", 0)
        leverage_cap = sizing.get("leverage_cap") or self.config.early_candidate_max_leverage
        return signal.model_copy(
            update={
                "signal": candidate_side,
                "confidence": meta_decision.confidence,
                "entry_reason": [
                    "meta_market_advisor_candidate",
                    f"meta_decision={meta_decision.decision}",
                    f"supporting_timeframes={','.join(meta_decision.supporting_timeframes)}",
                    "code_safety_gate_passed",
                ],
                "invalid_reason": signal.invalid_reason,
                "suggested_position_pct": sizing.get("margin_pct", 0),
                "stop_loss_pct": round(stop_loss_pct, 6),
                "take_profit_pct": take_profit_pct,
                "atr_pct": signal.atr_pct or sizing.get("atr_pct"),
                "risk_reward_ratio": self.config.min_reward_risk_ratio,
                "market_regime": signal.market_regime,
                "advisor_judgement": meta_decision.decision,
                "advisor_confidence": meta_decision.confidence,
                "advisor_timeframes": sorted(advisor_results.keys()),
                "advisor_confirmed_by_code": True,
                "advisor_reject_reasons": [],
                "risk_budget_pct": round(risk_budget_pct, 8),
                "max_leverage_override": leverage_cap,
            }
        ), audit

    def _apply_to_technical_signal(
        self,
        signal: StrategySignal,
        meta_decision: MetaMarketAdvisorDecision,
        audit: MarketAdvisorAudit,
    ) -> tuple[StrategySignal, MarketAdvisorAudit]:
        decision = audit.arbiter_decision
        signal_side = "long" if signal.signal == StrategyAction.LONG else "short"
        opposite = meta_decision.direction in {"long", "short"} and meta_decision.direction != signal_side
        high_conflict = meta_decision.confidence >= self.config.min_model_confidence and (
            opposite
            or meta_decision.decision == "reject"
            or meta_decision.entry_timing in {"avoid", "wait_pullback"}
        )
        decision.candidate = f"technical_{signal_side}_advisor_review"
        if high_conflict:
            reason = "advisor_blocked_technical_signal"
            decision.reject_reasons.append(reason)
            invalid = list(signal.invalid_reason or [])
            invalid.append(reason)
            return signal.model_copy(
                update={
                    "signal": StrategyAction.HOLD,
                    "confidence": 0,
                    "suggested_position_pct": 0,
                    "invalid_reason": invalid,
                    "advisor_judgement": meta_decision.decision,
                    "advisor_confidence": meta_decision.confidence,
                    "advisor_timeframes": list(meta_decision.timeframe_set),
                    "advisor_confirmed_by_code": False,
                    "advisor_reject_reasons": [reason],
                }
            ), audit
        if meta_decision.decision == "reduce_risk" and meta_decision.confidence >= self.config.min_model_confidence:
            multiplier = min(max(meta_decision.risk_budget_multiplier or 0.5, 0.1), 1.0)
            decision.passed_rules.append("advisor_reduced_technical_signal")
            return signal.model_copy(
                update={
                    "suggested_position_pct": round(signal.suggested_position_pct * multiplier, 6),
                    "advisor_judgement": meta_decision.decision,
                    "advisor_confidence": meta_decision.confidence,
                    "advisor_timeframes": list(meta_decision.timeframe_set),
                    "advisor_confirmed_by_code": False,
                }
            ), audit
        decision.passed_rules.append("advisor_no_technical_change")
        return signal, audit

    @staticmethod
    def _result_by_type_or_timeframe(
        results: dict[str, MarketAdvisorResult], advisor_type: str, timeframe: str
    ) -> MarketAdvisorResult | None:
        for result in results.values():
            if result.advisor_type == advisor_type:
                return result
        return results.get(timeframe)

    def _run_code_safety_checks(
        self,
        side: StrategyAction,
        signal: StrategySignal,
        market: MarketSnapshot,
        candles: list[dict],
        result_1h: MarketAdvisorResult | None,
        meta_decision: MetaMarketAdvisorDecision,
        btc_eth_direction: dict[str, str],
        decision: MarketAdvisorDecision,
    ) -> dict[str, float]:
        sizing: dict[str, float] = {}
        passed_checks: set[str] = set()
        lookback = self.config.breakout_lookback_bars
        if len(candles) < lookback + 1:
            decision.reject_reasons.append("insufficient_candles")
            return sizing

        previous = candles[-lookback - 1 : -1]
        latest = candles[-1]
        entry_price = float(latest.get("close") or market.close)
        atr_pct = signal.atr_pct or self._atr_pct(candles)
        atr_abs = max(entry_price * atr_pct, 0.000001)
        buffer_abs = atr_abs * self.config.breakout_buffer_atr
        structure_high = max(float(row["high"]) for row in previous)
        structure_low = min(float(row["low"]) for row in previous)
        structure_level = structure_high if side == StrategyAction.LONG else structure_low
        sizing.update({"entry_price": entry_price, "atr_pct": atr_pct, "structure_level": structure_level})

        if side == StrategyAction.LONG and entry_price <= structure_high + buffer_abs:
            decision.reject_reasons.append("breakout_not_confirmed")
        if side == StrategyAction.SHORT and entry_price >= structure_low - buffer_abs:
            decision.reject_reasons.append("breakdown_not_confirmed")
        if (side == StrategyAction.LONG and entry_price > structure_high + buffer_abs) or (
            side == StrategyAction.SHORT and entry_price < structure_low - buffer_abs
        ):
            passed_checks.add("breakout_confirmed")

        volume_mean = sum(float(row.get("volume") or 0) for row in previous) / max(len(previous), 1)
        volume_ratio = float(latest.get("volume") or 0) / volume_mean if volume_mean > 0 else 0
        sizing["volume_ratio"] = volume_ratio
        if volume_ratio < self.config.min_volume_ratio:
            decision.reject_reasons.append("volume_not_confirmed")
        else:
            passed_checks.add("volume_confirmed")

        if self.config.btc_eth_confirmation_required and signal.symbol not in {"BTCUSDT", "ETHUSDT"}:
            btc = btc_eth_direction.get("BTCUSDT", "unknown")
            eth = btc_eth_direction.get("ETHUSDT", "unknown")
            if side == StrategyAction.LONG and btc == "down" and eth == "down":
                decision.reject_reasons.append("btc_eth_downtrend_conflict")
            if side == StrategyAction.SHORT and btc == "up" and eth == "up":
                decision.reject_reasons.append("btc_eth_uptrend_conflict")
            if not (
                (side == StrategyAction.LONG and btc == "down" and eth == "down")
                or (side == StrategyAction.SHORT and btc == "up" and eth == "up")
            ):
                passed_checks.add("btc_eth_confirmed")
        else:
            passed_checks.add("btc_eth_confirmed")

        invalidation = result_1h.invalidation_price if result_1h is not None else None
        if invalidation is None or invalidation <= 0:
            decision.reject_reasons.append("missing_invalidation_price")
            return sizing
        stop_distance = abs(entry_price - invalidation)
        stop_pct = stop_distance / entry_price if entry_price > 0 else 0
        stop_atr_ratio = stop_distance / atr_abs
        extension_atr = abs(entry_price - structure_level) / atr_abs
        sizing.update(
            {
                "stop_pct": stop_pct,
                "stop_atr_ratio": stop_atr_ratio,
                "extension_atr": extension_atr,
                "invalidation_price": invalidation,
            }
        )
        if stop_pct <= 0:
            decision.reject_reasons.append("invalid_stop_distance")
        if stop_atr_ratio < self.config.min_stop_loss_atr:
            decision.reject_reasons.append("stop_loss_too_close")
        if stop_atr_ratio > self.config.max_stop_loss_atr:
            decision.reject_reasons.append("stop_loss_too_far")
        if stop_pct > self.config.max_stop_loss_pct:
            decision.reject_reasons.append("stop_loss_pct_too_wide")
        if (
            stop_pct > 0
            and self.config.min_stop_loss_atr <= stop_atr_ratio <= self.config.max_stop_loss_atr
            and stop_pct <= self.config.max_stop_loss_pct
        ):
            passed_checks.add("stop_loss_valid")
        if extension_atr > self.config.max_extension_atr_from_structure:
            decision.reject_reasons.append("price_overextended_from_structure")
        else:
            passed_checks.add("not_overextended")

        passed_checks.add("rr_valid")
        passed_checks.add("daily_limit_ok")
        self._enforce_required_checks(meta_decision.required_code_checks, passed_checks, decision)

        risk_multiplier = min(
            max(meta_decision.risk_budget_multiplier, 0),
            self.config.max_meta_risk_budget_multiplier,
            1.0,
        )
        if risk_multiplier <= 0:
            decision.reject_reasons.append("risk_budget_multiplier_zero")
        leverage_cap = min(
            meta_decision.max_leverage_override or self.config.early_candidate_max_leverage,
            self.config.early_candidate_max_leverage,
        )
        margin_pct = self._risk_based_margin_pct(entry_price, stop_pct, self.config.early_candidate_risk_pct * risk_multiplier, leverage_cap)
        sizing["margin_pct"] = margin_pct
        sizing["risk_budget_multiplier"] = risk_multiplier
        sizing["leverage_cap"] = leverage_cap
        if margin_pct <= 0:
            decision.reject_reasons.append("early_position_size_zero")
        decision.passed_rules.extend(sorted(passed_checks))
        return sizing

    @staticmethod
    def _enforce_required_checks(required: list[str], passed: set[str], decision: MarketAdvisorDecision) -> None:
        deferred = {"risk_engine_approved"}
        allowed = {
            "breakout_confirmed",
            "volume_confirmed",
            "btc_eth_confirmed",
            "stop_loss_valid",
            "rr_valid",
            "not_overextended",
            "daily_limit_ok",
            "risk_engine_approved",
        }
        core = {"breakout_confirmed", "volume_confirmed", "stop_loss_valid", "rr_valid", "not_overextended", "daily_limit_ok"}
        for check in sorted(core | set(required or [])):
            if check in deferred:
                continue
            if check not in allowed:
                decision.reject_reasons.append(f"unsupported_required_check_{check}")
            elif check not in passed:
                decision.reject_reasons.append(f"required_check_failed_{check}")

    def _risk_based_margin_pct(self, entry_price: float, stop_pct: float, risk_pct: float, leverage_cap: float) -> float:
        if entry_price <= 0 or stop_pct <= 0:
            return 0
        notional_pct = risk_pct / stop_pct
        margin_pct = notional_pct / max(leverage_cap, 1.0)
        return round(min(margin_pct, self.config.max_early_margin_pct), 6)

    @staticmethod
    def _atr_pct(candles: list[dict], period: int = 14) -> float:
        if len(candles) < 2:
            return 0.001
        rows = candles[-(period + 1) :]
        true_ranges: list[float] = []
        for idx in range(1, len(rows)):
            high = float(rows[idx]["high"])
            low = float(rows[idx]["low"])
            prev_close = float(rows[idx - 1]["close"])
            true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        close = float(rows[-1]["close"])
        return max(sum(true_ranges) / max(len(true_ranges), 1) / close, 0.000001)


class MarketAdvisorGateway:
    def __init__(self, base_url: str, api_key: str, model: str, timeout_seconds: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def judge(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or uuid4().hex)
        advisor_requests = payload.get("advisor_requests") or []
        results: dict[str, dict[str, Any]] = {}
        for request in advisor_requests:
            key = str(request.get("timeframe") or request.get("name") or len(results))
            results[key] = self._judge_one(request_id, request)
        meta_decision = self._judge_meta(request_id, payload, results)
        return {"request_id": request_id, "advisor_results": results, "meta_decision": meta_decision}

    def _judge_one(self, request_id: str, request: dict[str, Any]) -> dict[str, Any]:
        advisor_type = str(request.get("advisor_type") or "early_candidate")
        system_prompt = ADVISOR_PROMPTS.get(advisor_type, ADVISOR_PROMPTS["early_candidate"])
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            raw = json.loads(content)
            normalized = self._normalize(request_id, request, raw)
            MarketAdvisorResult.model_validate(normalized)
            return normalized
        except Exception as exc:
            return self._neutral(request_id, request, f"{exc.__class__.__name__}: {str(exc)[:180]}")

    def _judge_meta(self, request_id: str, payload: dict[str, Any], advisor_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        meta_payload = {
            "request_id": request_id,
            "symbol": payload.get("symbol") or self._payload_symbol(payload),
            "timeframe_set": sorted(advisor_results.keys()),
            "strategy_signal": payload.get("strategy_signal"),
            "advisor_results": advisor_results,
            "permission_boundaries": {
                "can_trade": False,
                "can_only_decide_candidate": True,
                "code_safety_gate_has_veto": True,
                "risk_engine_final_approval": True,
                "cannot_access_account": True,
                "cannot_access_files": True,
                "cannot_change_risk": True,
            },
        }
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": META_ADVISOR_PROMPT},
                {"role": "user", "content": json.dumps(meta_payload, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            raw = json.loads(content)
            normalized = self._normalize_meta(request_id, meta_payload, raw)
            MetaMarketAdvisorDecision.model_validate(normalized)
            return normalized
        except Exception as exc:
            return self._neutral_meta(request_id, meta_payload, f"{exc.__class__.__name__}: {str(exc)[:180]}")

    @staticmethod
    def _normalize(request_id: str, request: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise TypeError("advisor output must be a JSON object")
        result = dict(raw)
        result["status"] = MarketAdvisorGateway._enum_value(result.get("status"), {}, {"ok", "error"}, "ok")
        result["request_id"] = request_id
        result["symbol"] = str(request.get("symbol"))
        result["timeframe"] = str(request.get("timeframe"))
        result["advisor_type"] = str(request.get("advisor_type"))
        result["judgement"] = MarketAdvisorGateway._enum_value(
            result.get("judgement"),
            {
                "long": "early_long_candidate",
                "bullish": "confirm",
                "short": "early_short_candidate",
                "bearish": "confirm",
                "avoid": "block",
                "wait_pullback": "warn",
                "reject": "block",
                "ok": "confirm",
            },
            {"early_long_candidate", "early_short_candidate", "confirm", "warn", "block", "neutral"},
            "neutral",
        )
        result["macro_regime"] = MarketAdvisorGateway._enum_value(
            result.get("macro_regime"),
            {
                "trend": "clean_trend",
                "uptrend": "clean_trend",
                "downtrend": "clean_trend",
                "clean": "clean_trend",
                "choppy": "choppy_trend",
                "volatile": "shock",
                "transition": "reversal",
                "unknown": "unknown",
            },
            {"clean_trend", "choppy_trend", "range", "reversal", "shock", "unknown"},
            "unknown",
        )
        result["direction_bias"] = MarketAdvisorGateway._enum_value(
            result.get("direction_bias"),
            {"up": "bullish", "long": "bullish", "down": "bearish", "short": "bearish", "sideways": "neutral"},
            {"bullish", "bearish", "neutral", "unknown"},
            "unknown",
        )
        result["risk_level"] = MarketAdvisorGateway._enum_value(
            result.get("risk_level"),
            {"medium_high": "high", "very_high": "high", "moderate": "medium"},
            {"low", "medium", "high", "unknown"},
            "unknown",
        )
        result["structure_bias"] = MarketAdvisorGateway._enum_value(
            result.get("structure_bias"),
            {"bullish": "up", "long": "up", "bearish": "down", "short": "down", "sideways": "range", "neutral": "unclear"},
            {"up", "down", "range", "unclear"},
            "unclear",
        )
        result["trend_health"] = MarketAdvisorGateway._enum_value(
            result.get("trend_health"),
            {"healthy": "strong", "good": "strong", "choppy": "weak", "neutral": "unknown"},
            {"strong", "weak", "exhausted", "unknown"},
            "unknown",
        )
        result["execution_quality"] = MarketAdvisorGateway._enum_value(
            result.get("execution_quality"),
            {"ok": "acceptable", "fair": "acceptable", "poor": "bad", "avoid": "bad"},
            {"good", "acceptable", "bad", "unknown"},
            "unknown",
        )
        result["entry_timing"] = MarketAdvisorGateway._enum_value(
            result.get("entry_timing"),
            {"pullback": "wait_pullback", "wait": "wait_pullback", "now_or_pullback": "wait_pullback", "bad": "avoid"},
            {"now", "wait_pullback", "avoid", "unknown"},
            "unknown",
        )
        result["confidence"] = MarketAdvisorGateway._bounded_float(result.get("confidence"), 0, 0, 1)
        result["major_resistance_nearby"] = bool(result.get("major_resistance_nearby") or False)
        result["major_support_nearby"] = bool(result.get("major_support_nearby") or False)
        result["overextended"] = bool(result.get("overextended") or False)
        result["liquidity_sweep_risk"] = bool(result.get("liquidity_sweep_risk") or False)
        result["risk_flags"] = result.get("risk_flags") if isinstance(result.get("risk_flags"), list) else []
        result["reason"] = str(result.get("reason") or "")
        return result

    @staticmethod
    def _enum_value(value: Any, aliases: dict[str, str], allowed: set[str], default: str) -> str:
        if value is None:
            return default
        text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
        text = aliases.get(text, text)
        return text if text in allowed else default

    @staticmethod
    def _bounded_float(value: Any, default: float, min_value: float, max_value: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return min(max(number, min_value), max_value)

    @staticmethod
    def _payload_symbol(payload: dict[str, Any]) -> str:
        requests = payload.get("advisor_requests") or []
        if requests and isinstance(requests[0], dict):
            return str(requests[0].get("symbol") or "")
        return ""

    @staticmethod
    def _normalize_meta(request_id: str, payload: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise TypeError("meta advisor output must be a JSON object")
        result = dict(raw)
        result["status"] = MarketAdvisorGateway._enum_value(result.get("status"), {}, {"ok", "error"}, "ok")
        result["request_id"] = request_id
        result["symbol"] = str(payload.get("symbol") or "")
        result["timeframe_set"] = result.get("timeframe_set") if isinstance(result.get("timeframe_set"), list) else list(payload.get("timeframe_set") or [])
        result["decision"] = MarketAdvisorGateway._enum_value(
            result.get("decision"),
            {"allow": "allow_candidate", "candidate": "allow_candidate", "wait": "wait_pullback", "avoid": "reject"},
            {"allow_candidate", "wait_pullback", "reject", "reduce_risk", "neutral"},
            "neutral",
        )
        result["direction"] = MarketAdvisorGateway._enum_value(
            result.get("direction"),
            {"bullish": "long", "bearish": "short", "neutral": "none"},
            {"long", "short", "none"},
            "none",
        )
        result["confidence"] = MarketAdvisorGateway._bounded_float(result.get("confidence"), 0, 0, 1)
        result["risk_budget_multiplier"] = MarketAdvisorGateway._bounded_float(result.get("risk_budget_multiplier"), 0, 0, 1)
        result["entry_timing"] = MarketAdvisorGateway._enum_value(
            result.get("entry_timing"),
            {"pullback": "wait_pullback", "wait": "wait_pullback", "bad": "avoid"},
            {"now", "wait_pullback", "avoid", "unknown"},
            "unknown",
        )
        result["main_reason"] = str(result.get("main_reason") or "")
        result["supporting_timeframes"] = result.get("supporting_timeframes") if isinstance(result.get("supporting_timeframes"), list) else []
        result["conflicting_timeframes"] = result.get("conflicting_timeframes") if isinstance(result.get("conflicting_timeframes"), list) else []
        result["risk_flags"] = result.get("risk_flags") if isinstance(result.get("risk_flags"), list) else []
        result["required_code_checks"] = result.get("required_code_checks") if isinstance(result.get("required_code_checks"), list) else []
        return result

    @staticmethod
    def _neutral(request_id: str, request: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            "status": "error",
            "request_id": request_id,
            "symbol": str(request.get("symbol")),
            "timeframe": str(request.get("timeframe")),
            "advisor_type": str(request.get("advisor_type")),
            "judgement": "neutral",
            "confidence": 0,
            "risk_flags": [],
            "reason": f"advisor fallback to neutral: {reason}",
            "fallback": "strategy_direction_used",
        }

    @staticmethod
    def _neutral_meta(request_id: str, payload: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            "status": "error",
            "request_id": request_id,
            "symbol": str(payload.get("symbol") or ""),
            "timeframe_set": list(payload.get("timeframe_set") or []),
            "decision": "neutral",
            "direction": "none",
            "confidence": 0,
            "risk_budget_multiplier": 0,
            "entry_timing": "unknown",
            "main_reason": f"meta advisor fallback to neutral: {reason}",
            "supporting_timeframes": [],
            "conflicting_timeframes": [],
            "risk_flags": [],
            "required_code_checks": [],
            "fallback": "strategy_direction_used",
        }


def build_gateway_from_env() -> MarketAdvisorGateway:
    load_dotenv()
    load_dotenv(Path("/srv/open-trade-deployment/.env"))
    return MarketAdvisorGateway(
        base_url=os.getenv("MARKET_ADVISOR_LLM_BASE_URL", os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")),
        api_key=os.getenv(
            "MARKET_ADVISOR_LLM_API_KEY",
            os.getenv("AICODEWITH_API_KEY", os.getenv("LITELLM_API_KEY", "dummy")),
        ),
        model=os.getenv("MARKET_ADVISOR_MODEL", os.getenv("SOCIAL_ANALYZER_MODEL", "vertex-gemini-3.1-pro-preview")),
        timeout_seconds=int(os.getenv("MARKET_ADVISOR_TIMEOUT_SECONDS", "30")),
    )


def run_gateway(args: argparse.Namespace) -> None:
    gateway = build_gateway_from_env()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._send({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})
                return
            self.send_error(404)

        def do_POST(self) -> None:
            if self.path != "/v1/judge":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                self._send(gateway.judge(payload))
            except Exception as exc:
                self._send({"status": "error", "reason": f"{exc.__class__.__name__}: {exc}"}, status=500)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"market advisor gateway listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


def normalize_gateway_response(payload: dict[str, Any]) -> dict[str, MarketAdvisorResult]:
    raw_results = payload.get("advisor_results") or {}
    results: dict[str, MarketAdvisorResult] = {}
    if not isinstance(raw_results, dict):
        return results
    for key, raw in raw_results.items():
        try:
            result = MarketAdvisorResult.model_validate(raw)
        except ValidationError:
            continue
        results[str(key)] = result
    return results


def normalize_meta_decision(payload: dict[str, Any]) -> MetaMarketAdvisorDecision | None:
    raw = payload.get("meta_decision")
    if not isinstance(raw, dict):
        return None
    try:
        return MetaMarketAdvisorDecision.model_validate(raw)
    except ValidationError:
        return None


MarketAdvisorArbiter = CodeSafetyGate
