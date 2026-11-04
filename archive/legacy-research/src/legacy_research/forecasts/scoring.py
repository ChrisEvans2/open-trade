from __future__ import annotations

from decimal import Decimal

from trading_agent.domain.errors import DomainError
from legacy_research.forecasts.contracts import Label, Probabilities


def realized_label(
    start: Decimal, end: Decimal, threshold=Decimal("0.005")
) -> tuple[Decimal, Label]:
    if not start.is_finite() or not end.is_finite() or start <= 0 or end <= 0:
        raise DomainError("INVALID_PRICE", "端点价格必须为有限正数")
    if not threshold.is_finite() or threshold <= 0:
        raise DomainError("INVALID_THRESHOLD", "标签阈值必须为有限正数")
    change = end / start - 1
    label = "UP" if change > threshold else "DOWN" if change < -threshold else "FLAT"
    return change, label


def brier(probabilities: Probabilities, actual: Label) -> Decimal:
    return sum(
        (
            (value - Decimal(label == actual)) ** 2
            for label, value in (
                ("UP", probabilities.p_up),
                ("FLAT", probabilities.p_flat),
                ("DOWN", probabilities.p_down),
            )
        ),
        Decimal(0),
    )
