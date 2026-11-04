from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from trading_agent.config import StrictModel


class StatisticClaim(StrictModel):
    sample_count: int = Field(ge=1)
    correct_count: int = Field(ge=0)
    direction_accuracy: Decimal = Field(ge=0, le=1)
    mean_brier: Decimal = Field(ge=0, le=2)


class ObservationCandidate(StrictModel):
    text: str = Field(min_length=1, max_length=800)
    evidence_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=100)]
    stats: StatisticClaim


class HypothesisCandidate(StrictModel):
    text: str = Field(min_length=1, max_length=800)
    evidence_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=100)]


class ReviewSubmission(StrictModel):
    schema_version: Literal[1] = 1
    narrative: str = Field(min_length=1, max_length=8000)
    evidence_ids: Annotated[tuple[UUID, ...], Field(max_length=100)] = ()
    observations: Annotated[tuple[ObservationCandidate, ...], Field(max_length=3)] = ()
    hypotheses: Annotated[tuple[HypothesisCandidate, ...], Field(max_length=3)] = ()

    @model_validator(mode="after")
    def unique_references(self):
        for item in (self, *self.observations, *self.hypotheses):
            if len(set(item.evidence_ids)) != len(item.evidence_ids):
                raise ValueError("证据ID不得重复")
        return self
