from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from trading_agent.config import StrictModel
from trading_agent.domain.time import utc
from legacy_research.forecasts.contracts import OneHourAnalysis, TrendAnalysis


class JobRequest(StrictModel):
    kind: Literal["explain", "review", "experiment"]
    forecast_id: UUID | None = None
    range_start: datetime | None = None
    range_end: datetime | None = None

    @model_validator(mode="after")
    def check_shape(self):
        if self.kind == "explain":
            if self.forecast_id is None or self.range_start or self.range_end:
                raise ValueError("解释请求仅接受forecast_id")
        elif self.kind == "review":
            if self.forecast_id or self.range_start is None or self.range_end is None:
                raise ValueError("复盘请求必须提供起止时间")
            if utc(self.range_start) >= utc(self.range_end):
                raise ValueError("复盘区间必须为非空半开区间")
        elif self.forecast_id or self.range_start or self.range_end:
            raise ValueError("实验请求不接受自定义数据范围")
        return self


class ChildTrend(StrictModel):
    child_id: str = Field(min_length=1, max_length=128)
    report: TrendAnalysis


class ChildOneHour(StrictModel):
    child_id: str = Field(min_length=1, max_length=128)
    report: OneHourAnalysis


class TimeframesRequest(StrictModel):
    trends: tuple[ChildTrend, ...] = ()
    freeze: bool = False
    one_hour: ChildOneHour | None = None

    @model_validator(mode="after")
    def check_stages(self):
        if len(self.trends) > 3 or len({t.report.timeframe for t in self.trends}) != len(
            self.trends
        ):
            raise ValueError("每批最多三份不同周期趋势报告")
        if not self.trends and not self.freeze and self.one_hour is None:
            raise ValueError("提交必须包含趋势、冻结或当前1h报告")
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
