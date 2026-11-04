from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, computed_field, field_validator, model_validator

from trading_agent.config import StrictModel
from trading_agent.domain.serialization import content_hash
from trading_agent.domain.time import INTERVAL_SECONDS, utc

Money = Annotated[Decimal, Field(max_digits=28, decimal_places=12)]


class Bar(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    source: str = "okx_contract_public"
    inst_id: str = Field(min_length=1)
    timeframe: Literal["1m", "1h", "4h", "6h", "12h"]
    open_at: datetime
    close_boundary: datetime
    open: Money = Field(gt=0)
    high: Money = Field(gt=0)
    low: Money = Field(gt=0)
    close: Money = Field(gt=0)
    volume_contracts: Money = Field(ge=0)
    volume_base: Money = Field(ge=0)
    turnover_quote: Money = Field(ge=0)
    available_at: datetime
    revision_no: int = Field(default=1, ge=1)

    @field_validator("open_at", "close_boundary", "available_at")
    @classmethod
    def normalize_time(cls, value):
        return utc(value)

    @model_validator(mode="after")
    def validate_candle(self):
        if self.close_boundary != self.open_at + timedelta(
            seconds=INTERVAL_SECONDS[self.timeframe]
        ):
            raise ValueError("K线时间跨度错误")
        if self.open_at.timestamp() % INTERVAL_SECONDS[self.timeframe] != 0:
            raise ValueError("K线未按UTC对齐")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError("K线OHLC关系错误")
        if self.available_at < self.close_boundary:
            raise ValueError("未收盘K线不能成为可用数据")
        return self

    @property
    def market_content_hash(self) -> str:
        return content_hash(self.model_dump(exclude={"id", "available_at", "revision_no"}))


class FormingCandle(StrictModel):
    """One observed, unfinished candle; never a confirmed Bar or settlement input."""

    source: Literal["okx_contract_public"] = "okx_contract_public"
    inst_id: Literal["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    timeframe: Literal["1h", "4h", "6h", "12h"]
    status: Literal["PROVISIONAL"] = "PROVISIONAL"
    confirm: Literal["0"] = "0"
    open_at: datetime
    close_boundary: datetime
    open: Money = Field(gt=0)
    high: Money = Field(gt=0)
    low: Money = Field(gt=0)
    close: Money = Field(gt=0)
    volume_contracts: Money = Field(ge=0)
    volume_base: Money = Field(ge=0)
    turnover_quote: Money = Field(ge=0)
    requested_at: datetime
    available_at: datetime

    @field_validator("open_at", "close_boundary", "requested_at", "available_at")
    @classmethod
    def normalize_time(cls, value):
        return utc(value)

    @model_validator(mode="after")
    def validate_snapshot(self):
        step = INTERVAL_SECONDS[self.timeframe]
        if self.open_at.timestamp() % step or self.close_boundary != self.open_at + timedelta(
            seconds=step
        ):
            raise ValueError("未收盘快照的周期边界错误")
        if not self.open_at <= self.requested_at <= self.available_at < self.close_boundary:
            raise ValueError("未收盘快照必须在当前周期内采集，不能使用最终收盘值")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError("未收盘快照OHLC关系错误")
        return self

    @computed_field
    @property
    def elapsed_fraction_at_receipt(self) -> float:
        # Receipt time is known; the exchange does not provide a last-update timestamp.
        return (self.available_at - self.open_at).total_seconds() / INTERVAL_SECONDS[self.timeframe]


Quality = Literal[
    "OK", "MISSING", "INSUFFICIENT_HISTORY", "PARTIAL_COVERAGE", "DEGENERATE", "NOT_APPLICABLE"
]


class Observation(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    source: str
    inst_id: str
    kind: Literal[
        "taker_volume",
        "oi",
        "funding",
        "long_short_ratio",
        "liquidation",
        "market_breadth",
        "trade_audit",
    ]
    event_at: datetime
    available_at: datetime
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    unit: str
    quality: Quality = "OK"
    revision_no: int = Field(default=1, ge=1)
    payload: dict

    @field_validator("event_at", "available_at", "coverage_start", "coverage_end")
    @classmethod
    def normalize_time(cls, value):
        return utc(value) if value is not None else value

    @model_validator(mode="after")
    def valid_coverage(self):
        if (self.coverage_start is None) != (self.coverage_end is None):
            raise ValueError("覆盖区间必须完整")
        if self.coverage_start is not None and self.coverage_start >= self.coverage_end:
            raise ValueError("覆盖区间无效")
        content_hash(self.payload)  # Reject NaN/Infinity even in provider-specific payloads.
        return self


class Feature(StrictModel):
    name: str
    value: Decimal | float | None = None
    unit: str = "ratio"
    window: int | None = None
    source: str
    event_at: datetime
    available_at: datetime
    quality: Quality = "OK"
    missing_reason: str | None = None
    observation_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def quality_matches_value(self):
        utc(self.event_at)
        utc(self.available_at)
        if self.quality == "OK" and self.value is None:
            raise ValueError("OK特征必须有值")
        if self.quality not in {"OK", "PARTIAL_COVERAGE"} and self.value is not None:
            raise ValueError("无效特征不能附带数值")
        if self.quality != "OK" and not self.missing_reason:
            raise ValueError("非OK特征必须说明原因")
        return self


class ProvisionalFeature(Feature):
    status: Literal["PROVISIONAL"] = "PROVISIONAL"
    caveat: str | None = None
