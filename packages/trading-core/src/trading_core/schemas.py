from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

MAX_SINGLE_TRADE_MARGIN_PCT = 0.10


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RiskMode(StrEnum):
    PAPER = "paper"
    CAUTIOUS = "cautious"
    NORMAL = "normal"
    PAUSED = "paused"
    EMERGENCY = "emergency"


class ExecutionMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class SocialRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    MIXED = "mixed"


class StrategyAction(StrEnum):
    LONG = "long"
    SHORT = "short"
    HOLD = "hold"
    CLOSE = "close"


class FinalAction(StrEnum):
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    CLOSE = "close"
    HOLD = "hold"


class MarketSnapshot(BaseModel):
    symbol: str
    timeframe: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    funding_rate: float | None = None
    next_funding_rate: float | None = None
    funding_time: datetime | None = None
    funding_inst_id: str | None = None
    open_interest: float | None = None

    @model_validator(mode="after")
    def validate_prices(self) -> "MarketSnapshot":
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("prices must be positive")
        if self.low > self.high:
            raise ValueError("low cannot exceed high")
        return self


class SocialItem(BaseModel):
    source: str
    source_type: str
    time: datetime
    asset: str | None = None
    text: str = Field(min_length=1, max_length=5000)
    url: HttpUrl | str | None = None
    engagement: int = 0
    source_trust: float = Field(default=0.5, ge=0, le=1)


class KeyEvent(BaseModel):
    title: str = Field(max_length=160)
    source: str = Field(max_length=120)
    risk_level: SocialRisk
    summary: str = Field(max_length=600)


class AIAnalysisResult(BaseModel):
    timestamp: datetime = Field(default_factory=utc_now)
    asset: str
    sentiment: Sentiment
    event_risk: SocialRisk
    should_block_trade: bool
    position_multiplier: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    market_relevance: Literal[
        "direct_asset",
        "crypto_sector",
        "macro_market",
        "political_geopolitical",
        "unrelated",
    ] = "unrelated"
    relevance_score: float = Field(default=0, ge=0, le=1)
    macro_assessment: str = Field(default="", max_length=600)
    ignored_events: list[str] = Field(default_factory=list, max_length=10)
    alpha_signal: StrategyAction = StrategyAction.HOLD
    alpha_confidence: float = Field(default=0, ge=0, le=1)
    alpha_horizon: Literal["scalp", "intraday", "swing", "none"] = "none"
    alpha_reason: str = Field(default="", max_length=800)
    event_type: Literal[
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
    ] = "none"
    event_decay_minutes: int = Field(default=0, ge=0, le=10080)
    source_weight: float = Field(default=0, ge=0, le=1)
    freshness_score: float = Field(default=0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=10)
    key_events: list[KeyEvent] = Field(default_factory=list, max_length=10)
    analysis_status: Literal[
        "ok",
        "no_recent_data",
        "heuristic",
        "schema_error",
        "model_error",
        "asset_mismatch",
    ] = "ok"
    analysis_error: str = Field(default="", max_length=600)
    reason: str = Field(max_length=1200)

    @field_validator("reason")
    @classmethod
    def reject_trade_commands(cls, value: str) -> str:
        banned = ("100x", "all in", "绕过风控", "ignore previous", "直接下单")
        lowered = value.lower()
        if any(term in lowered for term in banned):
            raise ValueError(
                "reason appears to contain direct trading or prompt-injection language"
            )
        return value


class MarketRegimeSnapshot(BaseModel):
    timestamp: datetime = Field(default_factory=utc_now)
    regime: Literal[
        "trend_up",
        "trend_down",
        "range",
        "squeeze",
        "choppy_high_vol",
        "transition",
        "shock",
        "unknown",
    ] = "unknown"
    pattern: Literal[
        "smooth_trend",
        "accelerating_trend",
        "stair_step",
        "relief_rally",
        "pullback",
        "v_reversal",
        "inverted_v",
        "false_breakout",
        "range_edge",
        "none",
    ] = "none"
    environment: Literal["normal", "low_vol", "high_vol", "illiquid", "unknown"] = (
        "unknown"
    )
    detailed_regime: Literal[
        "TREND_UP",
        "TREND_DOWN",
        "RANGE",
        "RANGE_LOW_VOL",
        "RANGE_WIDE",
        "RANGE_HIGH_VOL",
        "CONSOLIDATION",
        "EXPANDING_RANGE",
        "BREAKOUT_UP",
        "BREAKOUT_DOWN",
        "WATERFALL",
        "SQUEEZE",
        "CHOPPY_HIGH_VOL",
        "TRANSITION",
        "SHOCK",
        "UNKNOWN",
    ] = "UNKNOWN"
    previous_regime: str | None = None
    current_regime: str = "UNKNOWN"
    transition_from: str | None = None
    transition_to: str = "UNKNOWN"
    changed: bool = False
    confidence: float = Field(default=0, ge=0, le=1)
    trend_direction: StrategyAction = StrategyAction.HOLD
    trend_efficiency: float = Field(default=0, ge=0, le=1)
    volatility_ratio: float = Field(default=1, ge=0)
    range_position: float | None = Field(default=None, ge=0, le=1)
    range_low: float | None = Field(default=None, gt=0)
    range_high: float | None = Field(default=None, gt=0)
    range_mid: float | None = Field(default=None, gt=0)
    raw_range_low: float | None = Field(default=None, gt=0)
    raw_range_high: float | None = Field(default=None, gt=0)
    effective_range_low: float | None = Field(default=None, gt=0)
    effective_range_high: float | None = Field(default=None, gt=0)
    range_quality_score: float = Field(default=0, ge=0, le=1)
    range_width_expansion: float = Field(default=1, ge=0)
    recent_high_break_count: int = Field(default=0, ge=0)
    recent_low_break_count: int = Field(default=0, ge=0)
    bars_in_current_regime: int = Field(default=0, ge=0)
    risk_flags: list[str] = Field(default_factory=list, max_length=20)
    block_reasons: list[str] = Field(default_factory=list, max_length=20)
    change_point_score: float = Field(default=0, ge=0)
    reason: list[str] = Field(default_factory=list, max_length=20)


class SocialContributionAudit(BaseModel):
    technical_signal_before_social: StrategyAction
    final_signal_after_social: StrategyAction
    social_changed_decision: bool = False
    social_blocked_or_degraded: bool = False
    social_created_event_signal: bool = False
    social_aligned_with_technical: bool | None = None
    event_risk: SocialRisk
    market_relevance: Literal[
        "direct_asset",
        "crypto_sector",
        "macro_market",
        "political_geopolitical",
        "unrelated",
    ]
    alpha_signal: StrategyAction
    alpha_confidence: float = Field(ge=0, le=1)
    alpha_horizon: Literal["scalp", "intraday", "swing", "none"]
    event_type: str = "none"
    source_weight: float = Field(ge=0, le=1)
    freshness_score: float = Field(ge=0, le=1)
    contribution_reason: str = Field(default="", max_length=800)


class StrategySignal(BaseModel):
    timestamp: datetime = Field(default_factory=utc_now)
    symbol: str
    timeframe: str
    signal: StrategyAction
    confidence: float = Field(ge=0, le=1)
    entry_reason: list[str] = Field(default_factory=list)
    invalid_reason: list[str] = Field(default_factory=list)
    suggested_position_pct: float = Field(
        ge=0,
        le=MAX_SINGLE_TRADE_MARGIN_PCT,
        description="Suggested account capital/margin allocation. Must not exceed 10% per trade.",
    )
    stop_loss_pct: float | None = Field(default=None, gt=0, le=1)
    take_profit_pct: float | None = Field(default=None, gt=0, le=1)
    atr_pct: float | None = Field(default=None, gt=0, le=1)
    risk_reward_ratio: float | None = Field(default=None, gt=0)
    defensive_social_short: bool = False
    market_regime: MarketRegimeSnapshot | None = None
    social_audit: SocialContributionAudit | None = None
    advisor_judgement: str | None = Field(default=None, max_length=80)
    advisor_confidence: float | None = Field(default=None, ge=0, le=1)
    advisor_timeframes: list[str] = Field(default_factory=list, max_length=10)
    advisor_confirmed_by_code: bool = False
    advisor_reject_reasons: list[str] = Field(default_factory=list, max_length=20)
    risk_budget_pct: float | None = Field(default=None, ge=0, le=1)
    max_leverage_override: float | None = Field(default=None, ge=1)


class AccountState(BaseModel):
    equity: float = 100
    daily_pnl_pct: float = 0
    drawdown_pct: float = 0
    consecutive_losses: int = 0
    open_positions: dict[str, float] = Field(default_factory=dict)
    open_position_leverage: dict[str, float] = Field(default_factory=dict)
    open_position_entry_price: dict[str, float] = Field(default_factory=dict)
    open_position_mark_price: dict[str, float] = Field(default_factory=dict)
    open_position_unrealized_pnl_pct: dict[str, float] = Field(default_factory=dict)
    open_position_principal: dict[str, float] = Field(default_factory=dict)
    open_position_notional: dict[str, float] = Field(default_factory=dict)
    open_position_contracts: dict[str, float] = Field(default_factory=dict)
    open_position_contract_value: dict[str, float] = Field(default_factory=dict)
    open_position_entry_fee_amount: dict[str, float] = Field(default_factory=dict)
    open_position_unrealized_pnl_amount: dict[str, float] = Field(default_factory=dict)
    open_position_stop_loss_pct: dict[str, float] = Field(default_factory=dict)
    open_position_take_profit_pct: dict[str, float] = Field(default_factory=dict)
    open_position_trailing_stop_pct: dict[str, float] = Field(default_factory=dict)
    open_position_trailing_activation_pct: dict[str, float] = Field(
        default_factory=dict
    )
    open_position_funding_rate: dict[str, float | None] = Field(default_factory=dict)
    open_position_next_funding_rate: dict[str, float | None] = Field(
        default_factory=dict
    )
    open_position_funding_time: dict[str, datetime | None] = Field(default_factory=dict)
    open_position_funding_inst_id: dict[str, str | None] = Field(default_factory=dict)
    open_position_last_funding_time: dict[str, datetime | None] = Field(
        default_factory=dict
    )
    open_position_cumulative_funding_amount: dict[str, float] = Field(
        default_factory=dict
    )
    open_position_high_watermark: dict[str, float] = Field(default_factory=dict)
    open_position_low_watermark: dict[str, float] = Field(default_factory=dict)
    open_position_signal_source: dict[str, str] = Field(default_factory=dict)
    open_position_technical_signal: dict[str, str] = Field(default_factory=dict)
    open_position_social_alpha_signal: dict[str, str] = Field(default_factory=dict)
    open_position_social_contribution_reason: dict[str, str] = Field(
        default_factory=dict
    )
    open_position_entry_reasons: dict[str, list[str]] = Field(default_factory=dict)
    open_position_strategy_type: dict[str, str] = Field(default_factory=dict)
    open_position_regime: dict[str, str] = Field(default_factory=dict)
    open_position_detailed_regime: dict[str, str] = Field(default_factory=dict)
    open_position_range_quality_score: dict[str, float] = Field(default_factory=dict)
    open_position_risk_flags: dict[str, list[str]] = Field(default_factory=dict)
    open_position_range_mid: dict[str, float] = Field(default_factory=dict)
    open_position_range_low: dict[str, float] = Field(default_factory=dict)
    open_position_range_high: dict[str, float] = Field(default_factory=dict)
    total_unrealized_pnl_pct: float = 0
    total_unrealized_pnl_amount: float = 0
    realized_pnl_pct: float = 0
    realized_pnl_amount: float = 0
    last_mark_update: datetime | None = None
    last_mark_update_local: str | None = None
    last_mark_update_timezone: str = "Asia/Shanghai"
    api_failure_count: int = 0
    exchange_healthy: bool = True
    data_healthy: bool = True


class RuntimeState(BaseModel):
    mode: RiskMode = RiskMode.PAPER
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    global_paused: bool = False
    emergency: bool = False
    paused_symbols: set[str] = Field(default_factory=set)
    hard_stop_reason: str | None = None
    hard_stop_at: datetime | None = None
    order_failure_count: int = 0


class RiskDecision(BaseModel):
    allow_trade: bool
    final_action: FinalAction
    final_position_pct: float = Field(ge=0, le=1)
    final_leverage: float = Field(default=1.0, ge=1)
    risk_mode: RiskMode
    risk_checks: dict[str, str]
    reason: str


class OrderRequest(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"] = "market"
    position_pct: float = Field(gt=0, le=1)
    leverage: float = Field(default=1.0, ge=1)
    reduce_only: bool = False
    client_order_id: str
    inst_id: str | None = None
    td_mode: Literal["isolated", "cross", "cash"] | None = None
    quantity: float | None = Field(default=None, gt=0)


class ExecutionResult(BaseModel):
    timestamp: datetime = Field(default_factory=utc_now)
    action: FinalAction
    symbol: str
    success: bool
    order: OrderRequest | None = None
    fill_price: float | None = None
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class PaperTradeEvent(BaseModel):
    timestamp: datetime = Field(default_factory=utc_now)
    event_source: str
    event: str
    symbol: str
    timeframe: str | None = None
    side: Literal["long", "short"] | None = None
    signal_source: Literal[
        "technical", "social", "mixed", "manual", "unknown", "prediction_parent"
    ] = "unknown"
    technical_signal: StrategyAction | None = None
    social_alpha_signal: StrategyAction | None = None
    social_changed_decision: bool | None = None
    social_contribution_reason: str = ""
    entry_reasons: list[str] = Field(default_factory=list)
    strategy_type: str | None = None
    regime: str | None = None
    detailed_regime: str | None = None
    risk_flags: list[str] = Field(default_factory=list)
    range_quality_score: float | None = None
    range_mid: float | None = None
    range_low: float | None = None
    range_high: float | None = None
    reason: str = ""
    entry_price: float | None = None
    fill_price: float | None = None
    mark_price: float | None = None
    margin_pct: float | None = None
    margin_amount: float | None = None
    leverage: float | None = None
    notional_amount: float | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None
    trailing_activation_pct: float | None = None
    # Underlying-price excursions observed while the position was open; these
    # are intentionally unlevered so trade reviews can compare exits across
    # different dynamic leverage decisions.
    max_favorable_excursion_pct: float | None = Field(default=None, ge=0, le=1)
    max_adverse_excursion_pct: float | None = Field(default=None, ge=0, le=1)
    position_pnl_pct: float | None = None
    account_pnl_pct: float | None = None
    pnl_amount: float | None = None
    equity: float | None = None
    realized_pnl_pct_after: float | None = None
    realized_pnl_amount_after: float | None = None
    total_unrealized_pnl_pct_after: float | None = None
    total_unrealized_pnl_amount_after: float | None = None
    funding_rate: float | None = None
    next_funding_rate: float | None = None
    funding_time: datetime | None = None
    funding_inst_id: str | None = None
    funding_fee_amount: float | None = None
    cumulative_funding_amount: float | None = None
    exchange_gross_pnl_amount: float | None = None
    exchange_fee_amount: float | None = None
    exchange_funding_fee_amount: float | None = None
    exchange_net_pnl_amount: float | None = None
    advisor_judgement: str | None = None
    advisor_confidence: float | None = None
    advisor_confirmed_by_code: bool | None = None
    advisor_timeframes: list[str] = Field(default_factory=list)


class MarketAdvisorResult(BaseModel):
    status: Literal["ok", "error"] = "ok"
    request_id: str = ""
    symbol: str
    timeframe: str
    advisor_type: Literal[
        "macro_regime", "structure_bias", "early_candidate", "execution_quality"
    ]
    judgement: Literal[
        "early_long_candidate",
        "early_short_candidate",
        "confirm",
        "warn",
        "block",
        "neutral",
    ] = "neutral"
    confidence: float = Field(default=0, ge=0, le=1)
    regime: Literal[
        "clean_trend", "choppy_trend", "range", "reversal", "divergence", "unknown"
    ] = "unknown"
    macro_regime: Literal[
        "clean_trend", "choppy_trend", "range", "reversal", "shock", "unknown"
    ] = "unknown"
    direction_bias: Literal["bullish", "bearish", "neutral", "unknown"] = "unknown"
    risk_level: Literal["low", "medium", "high", "unknown"] = "unknown"
    major_resistance_nearby: bool = False
    major_support_nearby: bool = False
    structure_bias: Literal["up", "down", "range", "unclear"] = "unclear"
    trend_health: Literal["strong", "weak", "exhausted", "unknown"] = "unknown"
    execution_quality: Literal["good", "acceptable", "bad", "unknown"] = "unknown"
    entry_timing: Literal["now", "wait_pullback", "avoid", "unknown"] = "unknown"
    overextended: bool = False
    liquidity_sweep_risk: bool = False
    structure_level: float | None = None
    invalidation_price: float | None = None
    key_level: float | None = None
    invalidation_level: float | None = None
    risk_flags: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(default="", max_length=800)
    fallback: str | None = None

    @field_validator("reason")
    @classmethod
    def reject_tool_commands(cls, value: str) -> str:
        banned = (
            "读取",
            "read file",
            "cat ",
            ".env",
            "api_key",
            "shell",
            "下单",
            "提高杠杆",
            "绕过风控",
        )
        lowered = value.lower()
        if any(term in lowered for term in banned):
            raise ValueError(
                "advisor reason appears to contain tool, secret, or execution instructions"
            )
        return value


class MarketAdvisorDecision(BaseModel):
    candidate: str = "none"
    accepted: bool = False
    reject_reasons: list[str] = Field(default_factory=list)
    passed_rules: list[str] = Field(default_factory=list)


class MetaMarketAdvisorDecision(BaseModel):
    status: Literal["ok", "error"] = "ok"
    request_id: str = ""
    symbol: str
    timeframe_set: list[str] = Field(default_factory=list, max_length=10)
    decision: Literal[
        "allow_candidate", "wait_pullback", "reject", "reduce_risk", "neutral"
    ] = "neutral"
    direction: Literal["long", "short", "none"] = "none"
    confidence: float = Field(default=0, ge=0, le=1)
    risk_budget_multiplier: float = Field(default=0, ge=0, le=1)
    max_leverage_override: float | None = Field(default=None, ge=1)
    entry_timing: Literal["now", "wait_pullback", "avoid", "unknown"] = "unknown"
    main_reason: str = Field(default="", max_length=800)
    supporting_timeframes: list[str] = Field(default_factory=list, max_length=10)
    conflicting_timeframes: list[str] = Field(default_factory=list, max_length=10)
    risk_flags: list[str] = Field(default_factory=list, max_length=20)
    required_code_checks: list[str] = Field(default_factory=list, max_length=20)
    fallback: str | None = None

    @field_validator("main_reason")
    @classmethod
    def reject_tool_commands(cls, value: str) -> str:
        banned = (
            "读取",
            "read file",
            "cat ",
            ".env",
            "api_key",
            "shell",
            "下单",
            "提高杠杆",
            "绕过风控",
        )
        lowered = value.lower()
        if any(term in lowered for term in banned):
            raise ValueError(
                "meta advisor reason appears to contain tool, secret, or execution instructions"
            )
        return value


class MarketAdvisorAudit(BaseModel):
    request_id: str = Field(default_factory=lambda: uuid4().hex)
    symbol: str
    strategy_signal: str
    advisor_provider: str = "unknown"
    trigger_reasons: list[str] = Field(default_factory=list)
    trigger_details: dict[str, Any] = Field(default_factory=dict)
    advisor_results: dict[str, MarketAdvisorResult] = Field(default_factory=dict)
    meta_decision: MetaMarketAdvisorDecision | None = None
    arbiter_decision: MarketAdvisorDecision = Field(
        default_factory=MarketAdvisorDecision
    )
    risk_sizing: dict[str, Any] = Field(default_factory=dict)
    risk_engine_result: dict[str, Any] = Field(default_factory=dict)


class AgentMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=utc_now)
    sender: str
    recipient: str
    type: str
    payload: dict[str, Any]


class CycleArtifacts(BaseModel):
    market: MarketSnapshot
    social_raw: list[SocialItem]
    social_filtered: list[SocialItem]
    ai_analysis: AIAnalysisResult
    strategy_signal: StrategySignal
    risk_decision: RiskDecision
    execution_result: ExecutionResult
    market_advisor: MarketAdvisorAudit | None = None
