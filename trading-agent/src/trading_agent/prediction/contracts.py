from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from trading_agent.config import StrictModel
from trading_agent.domain.time import INTERVAL_SECONDS, utc

Symbol = Literal["BTCUSDT", "ETHUSDT"]
Timeframe = Literal["1h", "4h", "6h", "12h"]
Direction = Literal["UP", "DOWN"]
StructureKind = Literal["ASCENDING_TRIANGLE", "W_BOTTOM", "BOX", "CONVERGING_TRIANGLE", "UNKNOWN"]
Stage = Literal[
    "FORMING",
    "BREAKOUT_UP",
    "BREAKOUT_DOWN",
    "CONFIRMED_UP",
    "CONFIRMED_DOWN",
    "RETEST_UP",
    "RETEST_DOWN",
    "FAILED_BREAKOUT",
    "INVALIDATED",
    "UNKNOWN",
]
Probability = Annotated[Decimal, Field(ge=0, le=1)]
Price = Annotated[Decimal, Field(gt=0, max_digits=28, decimal_places=12)]
HorizonBars = Literal[2, 3, 4, 6, 8, 12]


class PriceLine(StrictModel):
    at: datetime
    price: Price
    slope_per_second: Decimal = Decimal(0)

    @field_validator("at")
    @classmethod
    def aware(cls, value):
        return utc(value)

    def value_at(self, at: datetime) -> Decimal:
        return self.price + self.slope_per_second * Decimal(
            str((utc(at) - self.at).total_seconds())
        )


class PriceCondition(StrictModel):
    behavior: Literal["CLOSE_ABOVE", "CLOSE_BELOW", "TOUCH", "SUPPORT", "RESISTANCE"]
    line: PriceLine
    tolerance: Decimal = Field(ge=0)
    confirmation_bars: int = Field(default=1, ge=1, le=5)


class PriceRange(StrictModel):
    low: Price
    high: Price

    @model_validator(mode="after")
    def ordered(self):
        if self.low > self.high:
            raise ValueError("目标区间下沿不能高于上沿")
        return self


class Scenario(StrictModel):
    direction: Direction
    trigger: PriceCondition
    invalidation: PriceCondition
    target: PriceRange
    target_probability: Probability
    # Additional observable boundary behavior, independently scored from direction.
    boundary_expectations: tuple[PriceCondition, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def executable_direction(self):
        allowed = {"UP": {"CLOSE_ABOVE", "SUPPORT"}, "DOWN": {"CLOSE_BELOW", "RESISTANCE"}}
        invalid = {
            "UP": {"CLOSE_BELOW", "RESISTANCE", "TOUCH"},
            "DOWN": {"CLOSE_ABOVE", "SUPPORT", "TOUCH"},
        }
        if self.trigger.behavior not in allowed[self.direction]:
            raise ValueError("情景触发条件必须与预测方向一致")
        if self.invalidation.behavior not in invalid[self.direction]:
            raise ValueError("失效条件必须表达该方向失效")
        return self


class DirectionProbabilities(StrictModel):
    p_up: Probability
    p_down: Probability
    p_none: Probability

    @model_validator(mode="after")
    def normalized(self):
        if abs(self.p_up + self.p_down + self.p_none - 1) > Decimal("0.000001"):
            raise ValueError("首次上行/下行/均未确认概率之和必须为1")
        return self


class PredictionReport(DirectionProbabilities):
    schema_version: Literal[3] = 3
    symbol: Symbol
    timeframe: Timeframe
    structure_id: str | None = Field(default=None, min_length=64, max_length=64)
    structure: StructureKind
    stage: Stage
    horizon_rule_id: UUID
    horizon_bars: HorizonBars = 4
    primary_direction: Direction | None = None
    scenarios: tuple[Scenario, ...] = Field(default=(), max_length=2)
    reason: str = Field(min_length=1, max_length=2000)
    risk_notes: tuple[Annotated[str, Field(max_length=300)], ...] = Field(default=(), max_length=5)
    evidence_ids: tuple[UUID, ...] = Field(default=(), max_length=30)
    knowledge_version_ids: tuple[UUID, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def scenarios_match(self):
        directions = [s.direction for s in self.scenarios]
        if len(set(directions)) != len(directions):
            raise ValueError("同一方向只能发布一个可评分情景")
        if self.p_up > 0 and "UP" not in directions:
            raise ValueError("看涨概率需要上行情景")
        if self.p_down > 0 and "DOWN" not in directions:
            raise ValueError("看跌概率需要下行情景")
        if self.primary_direction is not None and self.primary_direction not in directions:
            raise ValueError("主预测方向必须有对应情景")
        if (self.structure == "UNKNOWN") != (self.structure_id is None):
            raise ValueError("已识别结构必须引用程序候选，UNKNOWN不引用结构ID")
        if self.structure == "UNKNOWN" and self.stage != "UNKNOWN":
            raise ValueError("未知结构必须明确未知阶段")
        for refs in (self.evidence_ids, self.knowledge_version_ids):
            if len(set(refs)) != len(refs):
                raise ValueError("证据引用不可重复")
        return self


class PublishedPrediction(StrictModel):
    id: UUID
    engine_id: UUID
    snapshot_id: UUID
    inst_id: Literal["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    role: Literal["TIMEFRAME", "PARENT"]
    published_at: datetime
    expires_at: datetime
    observation_start: datetime
    reference_price: Price
    report: PredictionReport
    supersedes_id: UUID | None = None

    @field_validator("published_at", "expires_at", "observation_start")
    @classmethod
    def aware(cls, value):
        return utc(value)

    @model_validator(mode="after")
    def frozen_window(self):
        expected = self.published_at + timedelta(
            seconds=INTERVAL_SECONDS[self.report.timeframe] * self.report.horizon_bars
        )
        if self.expires_at != expected:
            raise ValueError("预测期限必须匹配发布时冻结的规则")
        # Start at the NEXT complete minute; an intraminute publication cannot see its past.
        if (
            not self.published_at
            <= self.observation_start
            < self.published_at + timedelta(minutes=1)
        ):
            raise ValueError("验证起点必须为发布后的首个完整分钟")
        if self.observation_start.timestamp() % 60:
            raise ValueError("验证起点必须对齐分钟")
        if self.inst_id != self.report.symbol.removesuffix("USDT") + "-USDT-SWAP":
            raise ValueError("预测币种与合约不一致")
        if self.role == "PARENT" and self.report.timeframe != "1h":
            raise ValueError("父Agent使用1h交易尺度")
        for scenario in self.report.scenarios:
            trigger = scenario.trigger.line.value_at(self.published_at)
            if scenario.direction == "UP" and scenario.target.low <= trigger:
                raise ValueError("上涨目标必须高于触发边界")
            if scenario.direction == "DOWN" and scenario.target.high >= trigger:
                raise ValueError("下跌目标必须低于触发边界")
            for condition in (
                scenario.trigger,
                scenario.invalidation,
                *scenario.boundary_expectations,
            ):
                if (
                    min(condition.line.value_at(t) for t in (self.published_at, self.expires_at))
                    <= 0
                ):
                    raise ValueError("整个预测期限内的价格边界必须为正")
        return self


class ParentDecision(StrictModel):
    schema_version: Literal[3] = 3
    prediction: PredictionReport
    action: Literal["OPEN_LONG", "OPEN_SHORT", "HOLD", "CLOSE"]
    trade_style: Literal["TREND", "RANGE", "BREAKOUT"]
    selected_direction: Direction | None = None
    child_prediction_ids: tuple[UUID, ...] = Field(default=(), max_length=4)
    context_revision: str = Field(min_length=64, max_length=64)
    position_id: UUID | None = None
    relationship: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def actionable(self):
        if self.prediction.timeframe != "1h":
            raise ValueError("综合预测使用1h交易尺度")
        expected = {"OPEN_LONG": "UP", "OPEN_SHORT": "DOWN"}.get(self.action)
        if expected and (
            self.selected_direction != expected
            or self.prediction.primary_direction != expected
            or expected not in {s.direction for s in self.prediction.scenarios}
        ):
            raise ValueError("开仓意图必须绑定同方向预测情景")
        if self.action == "CLOSE" and self.position_id is None:
            raise ValueError("平仓意图必须绑定具体持仓")
        if len(set(self.child_prediction_ids)) != len(self.child_prediction_ids):
            raise ValueError("综合引用不可重复")
        return self
