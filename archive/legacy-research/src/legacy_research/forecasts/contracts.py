from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from trading_agent.config import StrictModel
from trading_agent.domain.errors import DomainError
from trading_agent.domain.time import TRENDS

PROBABILITY_TOLERANCE = Decimal("0.000001")
Label = Literal["UP", "FLAT", "DOWN"]


class Probabilities(StrictModel):
    p_up: Decimal = Field(ge=0, le=1)
    p_flat: Decimal = Field(ge=0, le=1)
    p_down: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def normalized(self):
        if abs(self.p_up + self.p_flat + self.p_down - 1) > PROBABILITY_TOLERANCE:
            raise ValueError("三类概率之和必须为1，允许误差0.000001")
        return self

    @property
    def label(self) -> Label:
        values = {"FLAT": self.p_flat, "UP": self.p_up, "DOWN": self.p_down}
        return max(values, key=values.get)


class OneHourAnalysis(Probabilities):
    reason: str = Field(min_length=1, max_length=800)
    risk_notes: tuple[str, ...] = Field(default=(), max_length=3)
    evidence_ids: tuple[UUID, ...]

    @model_validator(mode="after")
    def validate_notes(self):
        if any(len(note) > 150 for note in self.risk_notes):
            raise ValueError("风险说明最多150字")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("证据引用不可重复")
        return self


class ForecastSubmission(OneHourAnalysis):
    schema_version: Literal[2]
    one_hour_analysis_id: UUID
    trend_analysis_ids: dict[str, UUID]

    @model_validator(mode="after")
    def validate_trends(self):
        if set(self.trend_analysis_ids) != set(TRENDS):
            raise ValueError("必须引用4h/6h/12h趋势报告")
        return self


class TrendAnalysis(StrictModel):
    timeframe: Literal["4h", "6h", "12h"]
    last_close_boundary: datetime
    trend_state: Literal["UP", "RANGE", "DOWN"]
    trend_strength: Decimal = Field(ge=0, le=1)
    structure_notes: str = Field(min_length=1, max_length=800)
    risk_notes: tuple[str, ...] = Field(default=(), max_length=3)
    evidence_ids: tuple[UUID, ...]

    @model_validator(mode="after")
    def validate_fields(self):
        if self.last_close_boundary.tzinfo is None:
            raise ValueError("趋势边界必须带时区")
        if any(len(note) > 150 for note in self.risk_notes):
            raise ValueError("风险说明最多150字")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("证据引用不可重复")
        return self


def require_same_probabilities(submission: Probabilities, analysis: Probabilities):
    if any(
        abs(getattr(submission, key) - getattr(analysis, key)) > PROBABILITY_TOLERANCE
        for key in ("p_up", "p_flat", "p_down")
    ):
        raise DomainError("PROBABILITY_MISMATCH", "正式概率必须与当前attempt的1h报告一致")
