from __future__ import annotations

import sys
from datetime import timezone
from typing import Any

from pydantic import ValidationError
from trading_core.ai.litellm_client import LiteLLMClient
from trading_core.ai.prompts import SOCIAL_ANALYZER_SYSTEM_PROMPT
from trading_core.schemas import (
    AIAnalysisResult,
    MarketSnapshot,
    Sentiment,
    SocialItem,
    SocialRisk,
    StrategyAction,
    utc_now,
)


class SocialAIAnalyzer:
    def __init__(
        self,
        client: LiteLLMClient | None,
        model: str,
        failure_policy: str = "technical_reduced_size",
    ):
        self.client = client
        self.model = model
        self.failure_policy = failure_policy

    async def analyze(
        self, symbol: str, items: list[SocialItem], market_snapshot: MarketSnapshot
    ) -> AIAnalysisResult:
        asset = symbol.removesuffix("USDT")
        if not items:
            return self._no_recent_data(asset)
        if self.client is None:
            return self._heuristic(asset, items)

        payload = {
            "task": "analyze_social_event_risk",
            "symbol": symbol,
            "asset": asset,
            "market_snapshot": market_snapshot.model_dump(mode="json"),
            "items": [item.model_dump(mode="json") for item in items],
            "permission_boundaries": {
                "analyzer_has_no_execution_permission": True,
                "analyzer_must_not_recommend_increased_position": True,
                "output_json_only": True,
            },
        }
        try:
            raw = await self.client.chat_json(
                self.model, SOCIAL_ANALYZER_SYSTEM_PROMPT, payload
            )
            raw = self._normalize_model_output(raw)
            result = AIAnalysisResult.model_validate(raw)
        except Exception as exc:
            detail = str(exc).splitlines()[0][:240]
            status = (
                "schema_error"
                if isinstance(exc, (TypeError, ValueError, ValidationError))
                else "model_error"
            )
            print(
                f"social AI analysis failed for {symbol}: {exc.__class__.__name__}: {detail}",
                file=sys.stderr,
                flush=True,
            )
            return self._fallback(
                asset,
                f"AI 分析失败，按 ai_failure_policy={self.failure_policy} 降级处理；不代表真实 high 风险：{exc.__class__.__name__}: {detail}",
                status=status,
                error=f"{exc.__class__.__name__}: {detail}",
            )

        if result.asset.upper() not in {asset.upper(), symbol.upper()}:
            return self._fallback(
                asset,
                f"AI 输出资产不匹配，按 ai_failure_policy={self.failure_policy} 降级处理；不代表真实 high 风险。",
                status="asset_mismatch",
                error=f"expected {asset}/{symbol}, got {result.asset}",
            )
        return self._suppress_passive_allocation_alpha(result, items)

    def _no_recent_data(self, asset: str) -> AIAnalysisResult:
        return AIAnalysisResult(
            asset=asset,
            sentiment=Sentiment.NEUTRAL,
            event_risk=SocialRisk.LOW,
            should_block_trade=False,
            position_multiplier=0.8,
            confidence=0.15,
            market_relevance="unrelated",
            relevance_score=0,
            macro_assessment="过去窗口内没有新的高可信社媒/公告消息。",
            ignored_events=[],
            alpha_signal=StrategyAction.HOLD,
            alpha_confidence=0,
            alpha_horizon="none",
            alpha_reason="没有可评估的消息面 alpha。",
            event_type="none",
            event_decay_minutes=0,
            source_weight=0,
            freshness_score=0,
            evidence=[],
            key_events=[],
            analysis_status="no_recent_data",
            analysis_error="",
            reason="过去窗口内没有新的高可信社媒/公告消息；按低置信度中性处理，不单独阻断交易。",
        )

    @staticmethod
    def _normalize_model_output(raw: Any) -> dict:
        normalized = SocialAIAnalyzer._unwrap_model_object(raw)
        confidence = normalized.get("confidence")
        if isinstance(confidence, str):
            normalized["confidence"] = {"low": 0.25, "medium": 0.5, "high": 0.75}.get(
                confidence.lower(), 0
            )

        multiplier = normalized.get("position_multiplier")
        if isinstance(multiplier, str):
            try:
                normalized["position_multiplier"] = float(multiplier)
            except ValueError:
                normalized["position_multiplier"] = 0

        if isinstance(normalized.get("reason"), str):
            normalized["reason"] = normalized["reason"][:1200]

        relevance = str(
            normalized.get("market_relevance") or normalized.get("relevance") or ""
        ).lower()
        allowed_relevance = {
            "direct_asset",
            "crypto_sector",
            "macro_market",
            "political_geopolitical",
            "unrelated",
        }
        normalized["market_relevance"] = (
            relevance if relevance in allowed_relevance else "unrelated"
        )

        relevance_score = normalized.get(
            "relevance_score", normalized.get("market_relevance_score", 0)
        )
        if isinstance(relevance_score, str):
            try:
                relevance_score = float(relevance_score)
            except ValueError:
                relevance_score = 0
        normalized["relevance_score"] = min(
            1,
            max(0, relevance_score if isinstance(relevance_score, (int, float)) else 0),
        )

        if isinstance(normalized.get("macro_assessment"), str):
            normalized["macro_assessment"] = normalized["macro_assessment"][:600]
        else:
            normalized["macro_assessment"] = ""

        ignored = normalized.get("ignored_events")
        if isinstance(ignored, list):
            normalized["ignored_events"] = [
                str(item)[:180] for item in ignored if item
            ][:10]
        elif isinstance(ignored, str) and ignored.strip():
            normalized["ignored_events"] = [ignored[:180]]
        else:
            normalized["ignored_events"] = []

        alpha_signal = str(
            normalized.get("alpha_signal") or normalized.get("signal") or "hold"
        ).lower()
        normalized["alpha_signal"] = (
            alpha_signal if alpha_signal in {"long", "short", "hold"} else "hold"
        )
        normalized["alpha_confidence"] = SocialAIAnalyzer._clamp_float(
            normalized.get("alpha_confidence", 0)
        )
        alpha_horizon = str(
            normalized.get("alpha_horizon") or normalized.get("horizon") or "none"
        ).lower()
        normalized["alpha_horizon"] = (
            alpha_horizon
            if alpha_horizon in {"scalp", "intraday", "swing", "none"}
            else "none"
        )
        if isinstance(normalized.get("alpha_reason"), str):
            normalized["alpha_reason"] = normalized["alpha_reason"][:800]
        else:
            normalized["alpha_reason"] = ""

        event_type = str(normalized.get("event_type") or "none").lower()
        allowed_event_types = {
            "listing",
            "delisting",
            "hack",
            "regulation",
            "macro",
            "etf_flow",
            "onchain_flow",
            "funding",
            "partnership",
            "upgrade",
            "market_structure",
            "other",
            "none",
        }
        normalized["event_type"] = (
            event_type if event_type in allowed_event_types else "other"
        )
        try:
            normalized["event_decay_minutes"] = min(
                10080, max(0, int(normalized.get("event_decay_minutes", 0)))
            )
        except (TypeError, ValueError):
            normalized["event_decay_minutes"] = 0
        normalized["source_weight"] = SocialAIAnalyzer._clamp_float(
            normalized.get("source_weight", 0)
        )
        normalized["freshness_score"] = SocialAIAnalyzer._clamp_float(
            normalized.get("freshness_score", 0)
        )
        evidence = normalized.get("evidence")
        if isinstance(evidence, list):
            normalized["evidence"] = [str(item)[:300] for item in evidence if item][:10]
        elif isinstance(evidence, str) and evidence.strip():
            normalized["evidence"] = [evidence[:300]]
        else:
            normalized["evidence"] = []

        key_events = normalized.get("key_events")
        if isinstance(key_events, list):
            normalized["key_events"] = [
                SocialAIAnalyzer._normalize_key_event(event)
                for event in key_events
                if isinstance(event, dict)
            ]
        elif key_events is None:
            normalized["key_events"] = []
        return normalized

    @staticmethod
    def _unwrap_model_object(raw: Any) -> dict:
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], dict):
            return dict(raw[0])
        raise TypeError("social AI output must be one JSON object")

    @staticmethod
    def _clamp_float(value) -> float:
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                value = 0
        if not isinstance(value, (int, float)):
            return 0
        return min(1, max(0, float(value)))

    @staticmethod
    def _normalize_key_event(event: dict) -> dict:
        risk = str(event.get("risk_level") or event.get("risk") or "low").lower()
        if risk not in {"low", "medium", "high", "critical"}:
            risk = "low"
        return {
            "title": str(event.get("title") or event.get("event") or "market update")[
                :160
            ],
            "source": str(event.get("source") or "model")[:120],
            "risk_level": risk,
            "summary": str(event.get("summary") or event.get("description") or "")[
                :600
            ],
        }

    def _fallback(
        self,
        asset: str,
        reason: str,
        *,
        status: str = "model_error",
        error: str = "",
    ) -> AIAnalysisResult:
        block_new_entries = self.failure_policy == "block_new_entries"
        return AIAnalysisResult(
            asset=asset,
            sentiment=Sentiment.NEUTRAL,
            event_risk=SocialRisk.MEDIUM,
            should_block_trade=block_new_entries,
            position_multiplier=0 if block_new_entries else 0.5,
            confidence=0,
            market_relevance="macro_market",
            relevance_score=0,
            macro_assessment="AI 分析失败；这是系统故障状态，不代表真实消息面 high 风险。",
            ignored_events=[],
            alpha_signal=StrategyAction.HOLD,
            alpha_confidence=0,
            alpha_horizon="none",
            alpha_reason="AI 分析失败，禁用消息面 alpha；技术单按 ai_failure_policy 降级。",
            event_type="none",
            event_decay_minutes=0,
            source_weight=0,
            freshness_score=0,
            evidence=[],
            key_events=[],
            analysis_status=status,
            analysis_error=error[:600],
            reason=reason,
        )

    def _heuristic(self, asset: str, items: list[SocialItem]) -> AIAnalysisResult:
        joined = "\n".join(item.text.lower() for item in items)
        critical_terms = (
            "hack",
            "exploit",
            "sec investigation",
            "lawsuit",
            "delist",
            "rug",
            "暂停提现",
            "被盗",
        )
        positive_terms = (
            "etf approval",
            "partnership",
            "listing",
            "mainnet",
            "upgrade",
            "上线",
        )
        political_terms = (
            "war",
            "sanction",
            "election",
            "minister",
            "president",
            "伊朗",
            "俄罗斯",
            "乌克兰",
            "制裁",
            "总统",
        )
        macro_short_terms = (
            "战争升级",
            "热冲突",
            "打击伊朗",
            "伊朗",
            "能源通膨",
            "能源通胀",
            "cpi",
            "升息",
            "加息",
            "etf净流出",
            "etf 净流出",
            "减仓",
            "卖出",
        )
        source_weight = max((item.source_trust for item in items), default=0)
        freshness_score = max(
            (self._freshness_score(item) for item in items), default=0
        )
        alpha_signal = StrategyAction.HOLD
        alpha_confidence = 0.0
        alpha_horizon = "none"
        alpha_reason = "未形成可交易的消息面 alpha。"
        event_type = "none"
        event_decay_minutes = 0
        evidence: list[str] = []
        if self._is_passive_allocation_context(items):
            risk = SocialRisk.LOW
            sentiment = Sentiment.NEUTRAL
            block = False
            multiplier = 1
            relevance = "crypto_sector"
            relevance_score = 0.25
            macro_assessment = (
                "定投、长期配置或长期收益类消息属于慢变量背景，不具备短线入场传导路径。"
            )
            ignored_events = [
                item.text[:180]
                for item in items
                if self._contains_passive_allocation_text(item.text)
            ][:5]
            reason = "启发式识别为定投/长期配置叙事；过滤为低相关背景，不产生 long 或 short alpha。"
        elif any(term in joined for term in critical_terms):
            risk = SocialRisk.HIGH
            sentiment = Sentiment.NEGATIVE
            block = True
            multiplier = 0
            relevance = "direct_asset"
            relevance_score = 0.8
            macro_assessment = "启发式命中重大负面关键词，可能直接影响交易风险。"
            ignored_events: list[str] = []
            alpha_signal = StrategyAction.SHORT
            alpha_confidence = min(
                0.85, 0.45 + source_weight * 0.25 + freshness_score * 0.2
            )
            alpha_horizon = "intraday"
            alpha_reason = "重大负面事件可能形成短线下行动量，但风险过滤仍会优先阻断或要求人工复核。"
            event_type = self._event_type(joined)
            event_decay_minutes = 240
            evidence = [
                item.text[:300]
                for item in items
                if any(term in item.text.lower() for term in critical_terms)
            ][:5]
            reason = "启发式分析发现潜在重大负面关键词，阻止新开仓。"
        elif any(term in joined for term in positive_terms):
            risk = SocialRisk.LOW
            sentiment = Sentiment.POSITIVE
            block = False
            multiplier = 1
            relevance = "crypto_sector"
            relevance_score = 0.45
            macro_assessment = (
                "启发式命中偏正面 crypto 关键词，但未构成需要放大仓位的依据。"
            )
            ignored_events = []
            alpha_signal = StrategyAction.LONG
            alpha_confidence = min(
                0.8, 0.35 + source_weight * 0.25 + freshness_score * 0.2
            )
            alpha_horizon = "intraday"
            alpha_reason = (
                "偏正面 crypto 事件可能形成短线买盘，但需要策略合成器和风控确认。"
            )
            event_type = self._event_type(joined)
            event_decay_minutes = 180
            evidence = [
                item.text[:300]
                for item in items
                if any(term in item.text.lower() for term in positive_terms)
            ][:5]
            reason = "启发式分析发现偏正面关键词，未发现重大负面风险。"
        elif any(term in joined for term in macro_short_terms):
            risk = SocialRisk.MEDIUM
            sentiment = Sentiment.NEGATIVE
            block = False
            multiplier = 0.6
            relevance = "macro_market"
            relevance_score = 0.65
            macro_assessment = "启发式命中地缘冲突、能源/通胀、ETF 流出或减仓关键词，可能压制 BTC/ETH 与风险资产。"
            ignored_events = []
            alpha_signal = StrategyAction.SHORT
            alpha_confidence = min(
                0.72, 0.38 + source_weight * 0.2 + freshness_score * 0.2
            )
            alpha_horizon = "intraday"
            alpha_reason = (
                "宏观/地缘风险存在明确市场传导路径，可作为技术 short 信号的顺势确认。"
            )
            event_type = self._event_type(joined)
            event_decay_minutes = 360
            evidence = [
                item.text[:300]
                for item in items
                if any(term in item.text.lower() for term in macro_short_terms)
            ][:5]
            reason = "启发式分析发现宏观/地缘风险传导，标记为中等风险和潜在 short alpha，但不单独阻断交易。"
        elif any(term in joined for term in political_terms):
            risk = SocialRisk.LOW
            sentiment = Sentiment.NEUTRAL
            block = False
            multiplier = 0.8
            relevance = "political_geopolitical"
            relevance_score = 0.25
            macro_assessment = "政治/地缘消息存在，但未识别到对当前资产、美元流动性或主要风险资产的明确传导路径。"
            ignored_events = [
                item.text[:180]
                for item in items
                if any(term in item.text.lower() for term in political_terms)
            ][:5]
            reason = "启发式分析发现政治/地缘类消息，但缺少明确市场传导路径；按低相关度处理，不阻断交易。"
        else:
            risk = SocialRisk.LOW
            sentiment = Sentiment.NEUTRAL
            block = False
            multiplier = 1
            relevance = "unrelated"
            relevance_score = 0.1
            macro_assessment = "未发现直接相关或宏观传导明确的消息。"
            ignored_events = []
            reason = "启发式分析未发现重大风险。"
        return AIAnalysisResult(
            timestamp=utc_now().astimezone(timezone.utc),
            asset=asset,
            sentiment=sentiment,
            event_risk=risk,
            should_block_trade=block,
            position_multiplier=multiplier,
            confidence=0.35,
            market_relevance=relevance,
            relevance_score=relevance_score,
            macro_assessment=macro_assessment,
            ignored_events=ignored_events,
            alpha_signal=alpha_signal,
            alpha_confidence=round(alpha_confidence, 4),
            alpha_horizon=alpha_horizon,
            alpha_reason=alpha_reason,
            event_type=event_type,
            event_decay_minutes=event_decay_minutes,
            source_weight=round(source_weight, 4),
            freshness_score=round(freshness_score, 4),
            evidence=evidence,
            key_events=[],
            analysis_status="heuristic",
            analysis_error="",
            reason=reason,
        )

    @staticmethod
    def _contains_passive_allocation_text(text: str) -> bool:
        lowered = text.lower()
        passive_terms = (
            "定投",
            "dca",
            "长期持有",
            "长期配置",
            "长期收益",
            "长线收益",
            "长期投资",
            "长线看好",
            "长期看好",
            "退休账户",
            "储蓄型",
            "每周买入",
            "每月买入",
            "passive allocation",
            "long-term allocation",
            "long term allocation",
            "long-term holder",
            "long term holder",
        )
        return any(term in lowered for term in passive_terms)

    @staticmethod
    def _contains_tradable_catalyst_text(text: str) -> bool:
        lowered = text.lower()
        catalyst_terms = (
            "hack",
            "exploit",
            "delist",
            "listing",
            "etf approval",
            "etf inflow",
            "etf outflow",
            "sec investigation",
            "lawsuit",
            "mainnet",
            "upgrade",
            "清算",
            "爆仓",
            "被盗",
            "攻击",
            "暂停提现",
            "上线",
            "下架",
            "监管",
            "起诉",
            "批准",
            "净流入",
            "净流出",
            "战争",
            "热冲突",
            "制裁",
            "原油",
            "能源",
            "通胀",
            "cpi",
            "pce",
            "美联储",
            "加息",
            "降息",
            "链上异动",
            "大额转入",
            "大额转出",
        )
        return any(term in lowered for term in catalyst_terms)

    @classmethod
    def _is_passive_allocation_context(cls, items: list[SocialItem]) -> bool:
        if not any(cls._contains_passive_allocation_text(item.text) for item in items):
            return False
        return not any(
            cls._contains_tradable_catalyst_text(item.text) for item in items
        )

    @classmethod
    def _suppress_passive_allocation_alpha(
        cls, result: AIAnalysisResult, items: list[SocialItem]
    ) -> AIAnalysisResult:
        if not cls._is_passive_allocation_context(items):
            return result
        ignored = list(result.ignored_events)
        for item in items:
            if cls._contains_passive_allocation_text(item.text):
                snippet = item.text[:180]
                if snippet not in ignored:
                    ignored.append(snippet)
        return result.model_copy(
            update={
                "sentiment": Sentiment.NEUTRAL,
                "event_risk": SocialRisk.LOW,
                "should_block_trade": False,
                "position_multiplier": min(1, max(0.8, result.position_multiplier)),
                "market_relevance": "crypto_sector",
                "relevance_score": min(result.relevance_score, 0.3),
                "macro_assessment": "定投、长期配置或长期收益类消息属于慢变量背景，不具备短线入场传导路径。",
                "ignored_events": ignored[:10],
                "alpha_signal": StrategyAction.HOLD,
                "alpha_confidence": 0,
                "alpha_horizon": "none",
                "alpha_reason": "定投/长期配置叙事不提供短线方向，已过滤为 hold。",
                "event_type": "none",
                "event_decay_minutes": 0,
                "evidence": [],
                "key_events": [],
                "reason": "社媒消息属于定投/长期配置/长期收益叙事；不作为多空入场依据。",
            }
        )

    @staticmethod
    def _freshness_score(item: SocialItem) -> float:
        item_time = (
            item.time if item.time.tzinfo else item.time.replace(tzinfo=timezone.utc)
        )
        age_minutes = max(
            0,
            (
                utc_now().astimezone(timezone.utc) - item_time.astimezone(timezone.utc)
            ).total_seconds()
            / 60,
        )
        return max(0, min(1, 1 - age_minutes / 360))

    @staticmethod
    def _event_type(text: str) -> str:
        mapping = (
            ("delisting", ("delist", "下架", "退市")),
            ("listing", ("listing", "上线", "上架")),
            ("hack", ("hack", "exploit", "被盗", "攻击", "漏洞")),
            ("regulation", ("sec", "cftc", "regulation", "lawsuit", "监管", "起诉")),
            ("etf_flow", ("etf", "流入", "流出")),
            ("upgrade", ("mainnet", "upgrade", "主网", "升级")),
            ("partnership", ("partnership", "合作")),
            ("macro", ("fed", "cpi", "pce", "非农", "美联储", "利率")),
        )
        for event_type, terms in mapping:
            if any(term in text for term in terms):
                return event_type
        return "other"
