from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa

from trading_agent.config import MemoryConfig
from trading_agent.domain.serialization import json_value
from trading_agent.domain.time import utc
from trading_agent.storage import schema as s


def latest_settlements(as_of: datetime):
    """As-of revisions are selected before aggregating, so future corrections cannot leak."""
    return (
        sa.select(s.settlements)
        .where(s.settlements.c.settled_at <= as_of)
        .distinct(s.settlements.c.forecast_id)
        .order_by(s.settlements.c.forecast_id, s.settlements.c.revision_no.desc())
        .subquery()
    )


def case_metrics(rows) -> dict:
    size = len(rows)
    classes = {
        label: sum(r["actual_label"] == label for r in rows) for label in ("UP", "FLAT", "DOWN")
    }
    flat_correct = classes["FLAT"]
    return dict(
        sample_count=size,
        correct_count=sum(r["correct"] for r in rows),
        direction_accuracy=Decimal(sum(r["correct"] for r in rows)) / size if size else None,
        mean_brier=sum((r["brier"] for r in rows), Decimal(0)) / size if size else None,
        class_distribution=classes,
        uniform_baseline_brier=Decimal(2) / 3,
        flat_baseline_accuracy=Decimal(flat_correct) / size if size else None,
        flat_baseline_brier=Decimal(2 * (size - flat_correct)) / size if size else None,
        small_sample=size < 30,
        small_sample_reference=30,
    )


@dataclass(frozen=True)
class RetrievedContext:
    case_ids: tuple[UUID, ...]
    observation_ids: tuple[UUID, ...]
    stats: dict
    text: str


class MemoryService:
    def __init__(self, database, config: MemoryConfig | None = None):
        self.database, self.config = database, config or MemoryConfig()

    async def retrieve(
        self, engine_id: UUID, symbol: str, decision_at: datetime
    ) -> RetrievedContext:
        decision_at = utc(decision_at)
        latest = latest_settlements(decision_at)
        async with self.database.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.select(
                            s.forecasts.c.id,
                            s.forecasts.c.published_at,
                            s.forecasts.c.reason,
                            s.forecasts.c.label,
                            latest.c.id.label("settlement_id"),
                            latest.c.correct,
                            latest.c.brier,
                            latest.c.actual_label,
                            latest.c.realized_return,
                            latest.c.settled_at,
                        )
                        .join(latest, latest.c.forecast_id == s.forecasts.c.id)
                        .where(
                            s.forecasts.c.engine_id == engine_id,
                            s.forecasts.c.symbol == symbol,
                            s.forecasts.c.end_at - s.forecasts.c.start_at == timedelta(hours=1),
                            s.forecasts.c.published_at <= decision_at,
                            s.forecasts.c.published_at
                            >= decision_at - timedelta(days=self.config.case_lookback_days),
                        )
                        .order_by(latest.c.settled_at.desc(), s.forecasts.c.id)
                    )
                )
                .mappings()
                .all()
            )
            # Regime classification is not specified; no inferred state matching is applied.
            wrong, right = [r for r in rows if not r["correct"]], [r for r in rows if r["correct"]]
            chosen = wrong[:3] + right[:2]
            ids = {r["id"] for r in chosen}
            chosen += [r for r in rows if r["id"] not in ids][: self.config.max_cases - len(chosen)]
            observations = (
                (
                    await connection.execute(
                        sa.select(s.observations)
                        .where(
                            s.observations.c.engine_id == engine_id,
                            s.observations.c.kind == "observation",
                            s.observations.c.verification_status == "VERIFIED",
                            s.observations.c.regime.is_(None),
                            s.observations.c.available_at <= decision_at,
                            s.observations.c.available_at
                            > decision_at - timedelta(days=self.config.observation_ttl_days),
                            s.observations.c.expires_at > decision_at,
                        )
                        .order_by(s.observations.c.available_at.desc(), s.observations.c.id)
                        .limit(self.config.max_observations)
                    )
                )
                .mappings()
                .all()
            )
        stats = case_metrics(rows)
        evidence = {
            "as_of": decision_at,
            "stats": stats,
            "cases": [
                {
                    k: r[k]
                    for k in (
                        "id",
                        "settlement_id",
                        "published_at",
                        "settled_at",
                        "label",
                        "actual_label",
                        "realized_return",
                        "brier",
                        "correct",
                    )
                }
                for r in chosen
            ],
            "observations": [
                {
                    "id": r["id"],
                    "available_at": r["available_at"],
                    "expires_at": r["expires_at"],
                    "stats": r["stats"],
                }
                for r in observations
            ],
        }
        # Preserve numbers, ids and timestamps first; only explanation text may be truncated.
        text = json.dumps(json_value(evidence), ensure_ascii=False, separators=(",", ":"))
        remaining = self.config.context_max_chars - len(text)
        explanations = "\n" + "\n".join(
            [f"case {r['id']}: {r['reason']}" for r in chosen]
            + [f"observation {r['id']}: {r['text']}" for r in observations]
        )
        if remaining > 0:
            text += explanations[:remaining]
        if len(text) > self.config.context_max_chars:
            # Config bounds and fixed record limits normally keep the numeric section below 6k.
            # Arbitrarily large stored observation metrics must not silently truncate evidence.
            evidence["observations"] = [
                {k: r[k] for k in ("id", "available_at", "expires_at")} for r in observations
            ]
            text = json.dumps(json_value(evidence), ensure_ascii=False, separators=(",", ":"))
        if len(text) > self.config.context_max_chars:
            raise ValueError("Structured memory evidence exceeds the fixed context budget")
        return RetrievedContext(
            tuple(r["id"] for r in chosen),
            tuple(r["id"] for r in observations),
            json_value(stats),
            text,
        )
