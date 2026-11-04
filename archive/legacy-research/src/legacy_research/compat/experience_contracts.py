from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, field_validator

from legacy_research.compat.contracts import strategy_path
from trading_agent.config import StrictModel

Category = Literal[
    "entry_filter",
    "exit_management",
    "market_regime",
    "position_sizing",
    "execution_safety",
    "data_quality",
    "general",
]


class ExperienceQuery(StrictModel):
    query: str = Field(default="", max_length=200)
    categories: Annotated[tuple[Category, ...], Field(max_length=7)] = ()
    strategy_paths: Annotated[
        tuple[Annotated[str, Field(max_length=240)], ...], Field(max_length=8)
    ] = ()
    symbols: Annotated[tuple[str, ...], Field(max_length=10)] = ()
    limit: int = Field(default=5, ge=1, le=5)

    @field_validator("strategy_paths")
    @classmethod
    def strategy_files(cls, values):
        for value in values:
            if (
                not strategy_path(value)
                or ".." in value.split("/")
                or "\\" in value
                or str(PurePosixPath(value)) != value
            ):
                raise ValueError("经验筛选只接受规范策略Python路径")
        return values

    @field_validator("symbols")
    @classmethod
    def symbol_names(cls, values):
        if any(not value.isascii() or not value.isalnum() or len(value) > 32 for value in values):
            raise ValueError("经验币种标签必须是ASCII字母数字")
        return tuple(value.upper() for value in values)


class ExperiencePage(StrictModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6000, ge=1, le=12000)
