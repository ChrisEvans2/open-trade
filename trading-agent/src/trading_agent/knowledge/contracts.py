from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from trading_agent.config import StrictModel
from trading_agent.prediction.contracts import Stage, StructureKind, Symbol, Timeframe


class KnowledgeCase(StrictModel):
    prediction_id: UUID
    evaluation_id: UUID
    relation: Literal["SUPPORT", "COUNTEREXAMPLE", "UNRESOLVED", "CORRECTION"]


class KnowledgeChange(StrictModel):
    operation: Literal["NEW", "REVISE", "MERGE", "RETIRE"]
    concept_id: UUID | None = None
    previous_version_ids: tuple[UUID, ...] = Field(default=(), max_length=5)
    symbol: Symbol | None = None
    timeframe: Timeframe | None = None
    structure: StructureKind | None = None
    stage: Stage | None = None
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=1200)
    applicability: str = Field(min_length=1, max_length=1500)
    limitations: str = Field(min_length=1, max_length=1500)
    content: str = Field(min_length=1, max_length=12000)
    procedure: tuple[Annotated[str, Field(min_length=1, max_length=600)], ...] = Field(
        min_length=1, max_length=12
    )
    evidence_status: Literal["UNVERIFIED", "HYPOTHESIS", "EVIDENCE_SUPPORTED", "REJECTED"]
    cases: tuple[KnowledgeCase, ...] = Field(default=(), max_length=20)
    rationale: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def references(self):
        n = len(self.previous_version_ids)
        if len(set(self.previous_version_ids)) != n:
            raise ValueError("前序版本不可重复")
        if self.operation == "NEW" and (n or self.concept_id):
            raise ValueError("新增知识不能冒用已有标识")
        if self.operation in {"REVISE", "RETIRE"} and (n != 1 or self.concept_id is None):
            raise ValueError("修订或退役需要一个前序版本及知识点标识")
        if self.operation == "MERGE" and (n < 2 or self.concept_id):
            raise ValueError("合并需要至少两个前序版本，并生成新知识点")
        if self.evidence_status == "EVIDENCE_SUPPORTED" and not self.cases:
            raise ValueError("有证据支持的知识必须关联案例")
        if len({c.prediction_id for c in self.cases}) != len(self.cases):
            raise ValueError("每个案例只可关联一次")
        return self


class KnowledgeReview(StrictModel):
    schema_version: Literal[3] = 3
    narrative: str = Field(min_length=1, max_length=6000)
    changes: tuple[KnowledgeChange, ...] = Field(default=(), max_length=10)
