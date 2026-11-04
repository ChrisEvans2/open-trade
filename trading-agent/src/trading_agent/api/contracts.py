from datetime import timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from trading_agent.config import StrictModel
from trading_agent.domain.time import utc


class JobRequest(StrictModel):
    kind: Literal["explain", "review", "experiment"]
    prediction_id: UUID | None = None
    forecast_id: UUID | None = None
    symbol: Literal["BTCUSDT", "ETHUSDT"] | None = None
    range_start: AwareDatetime | None = None
    range_end: AwareDatetime | None = None

    @field_validator("range_start", "range_end")
    @classmethod
    def normalize_time(cls, value):
        return utc(value) if value is not None else None

    @model_validator(mode="after")
    def check_shape(self):
        targets = int(self.prediction_id is not None) + int(self.forecast_id is not None)
        if self.kind == "explain":
            if targets != 1 or self.range_start or self.range_end or self.symbol:
                raise ValueError("解释请求仅接受一个prediction_id或forecast_id")
        elif self.kind == "review":
            if targets or self.range_start is None or self.range_end is None:
                raise ValueError("复盘请求必须提供起止时间，可选币种")
            if not timedelta(0) < self.range_end - self.range_start <= timedelta(days=90):
                raise ValueError("复盘区间必须为非空半开区间，最长90天")
        elif targets or self.range_start or self.range_end or self.symbol:
            raise ValueError("实验请求不接受自定义范围")
        return self


class ModelRun(StrictModel):
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[!-~]+$")
    role: Literal["parent", "1h", "4h", "6h", "12h"]
    status: Literal[
        "running", "completed", "failed", "error", "interrupted", "truncated", "invalid", "unknown"
    ]
    duration_seconds: float | None = Field(default=None, ge=0, le=3600)
    api_calls: int | None = Field(default=None, ge=0, le=10_000_000, strict=True)
    input_tokens: int | None = Field(default=None, ge=0, le=10**12, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, le=10**12, strict=True)


class ModelUsage(StrictModel):
    runs: tuple[ModelRun, ...] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def distinct_sessions(self):
        if len({run.session_id for run in self.runs}) != len(self.runs):
            raise ValueError("同一批usage不能重复会话")
        return self
