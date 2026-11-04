from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from trading_quant.spec import CORE_FEATURES, EXTRA_FEATURES


class CalculationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class BarInput(Protocol):
    inst_id: str
    timeframe: str
    close_boundary: datetime
    available_at: datetime
    high: Decimal
    low: Decimal
    close: Decimal
    volume_base: Decimal
    turnover_quote: Decimal


class ObservationInput(Protocol):
    id: UUID
    source: str
    inst_id: str
    kind: str
    event_at: datetime
    available_at: datetime
    coverage_start: datetime | None
    coverage_end: datetime | None
    unit: str
    quality: str
    revision_no: int
    payload: dict


@dataclass(frozen=True)
class FeatureValue:
    name: str
    value: Decimal | float | None
    unit: str
    window: int | None
    source: str
    event_at: datetime
    available_at: datetime
    quality: str
    missing_reason: str | None
    observation_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class QuantConfig:
    required_features: tuple[str, ...] = ("return_1", "return_20", "atr_pct", "rsi")
    optional_features: tuple[str, ...] = tuple(
        name
        for name in CORE_FEATURES + EXTRA_FEATURES
        if name not in {"return_1", "return_20", "atr_pct", "rsi"}
    )
    market_basket: tuple[str, ...] = ()
    market_basket_min_coverage: Decimal = Decimal("0.8")

    def __post_init__(self):
        selected = self.required_features + self.optional_features
        if len(set(selected)) != len(selected) or set(selected) - set(
            CORE_FEATURES + EXTRA_FEATURES
        ):
            raise CalculationError("INVALID_CONFIG", "特征集合重复或含未知特征")
        if len(set(self.market_basket)) != len(self.market_basket):
            raise CalculationError("INVALID_CONFIG", "市场篮子不可重复")
        if not 0 < self.market_basket_min_coverage <= 1:
            raise CalculationError("INVALID_CONFIG", "市场篮子覆盖门槛无效")
