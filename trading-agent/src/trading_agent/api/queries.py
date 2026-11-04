from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa

from trading_agent.api.metrics import performance
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import json_value
from trading_agent.storage import schema as s


class Queries:
    def __init__(self, database):
        self.database = database

    async def forecast(self, ident: UUID | None = None):
        async with self.database.engine.connect() as connection:
            query = sa.select(s.forecasts)
            query = (
                query.where(s.forecasts.c.id == ident)
                if ident
                else query.order_by(s.forecasts.c.anchor_at.desc()).limit(1)
            )
            row = (await connection.execute(query)).mappings().first()
            if row is None:
                raise DomainError("FORECAST_NOT_FOUND", "尚无对应正式预测", 404)
            revisions = (
                (
                    await connection.execute(
                        sa.select(s.settlements)
                        .where(s.settlements.c.forecast_id == row["id"])
                        .order_by(s.settlements.c.revision_no.desc())
                    )
                )
                .mappings()
                .all()
            )
            validation = (
                (
                    await connection.execute(
                        sa.select(
                            s.jobs.c.id,
                            s.jobs.c.state,
                            s.jobs.c.stage,
                            s.jobs.c.next_run_at,
                            s.jobs.c.last_error,
                        )
                        .where(
                            s.jobs.c.kind == "SETTLE",
                            s.jobs.c.payload["forecast_id"].astext == str(row["id"]),
                        )
                        .order_by(s.jobs.c.updated_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            snapshot = (
                (
                    await connection.execute(
                        sa.select(s.snapshots)
                        .join(
                            s.context_bundles, s.context_bundles.c.snapshot_id == s.snapshots.c.id
                        )
                        .where(s.context_bundles.c.id == row["bundle_id"])
                    )
                )
                .mappings()
                .one()
            )
            active = await connection.scalar(
                sa.select(s.strategy_activations.c.strategy_id)
                .where(s.strategy_activations.c.effective_anchor_at <= sa.func.clock_timestamp())
                .order_by(s.strategy_activations.c.effective_anchor_at.desc())
                .limit(1)
            )
        result = dict(row)
        # Read actual child probabilities from the immutable payload, not rounded SQL projections.
        result.update({k: row["payload"][k] for k in ("p_up", "p_flat", "p_down")})
        result.update(
            latest_settlement=dict(revisions[0]) if revisions else None,
            settlement_revisions=[dict(r) for r in revisions],
            validation_job=dict(validation) if validation else None,
            validation_status="SETTLED"
            if revisions
            else "AWAITING_DATA"
            if validation and validation["state"] == "AWAITING_DATA"
            else "PENDING",
            current_strategy_id=active,
            snapshot={
                "id": snapshot["id"],
                "decision_at": snapshot["decision_at"],
                "payload_hash": snapshot["payload_hash"],
                "last_close_by_timeframe": snapshot["last_close_by_timeframe"],
                "calculation_count": 80,
                "model_count": 30,
            },
        )
        return json_value(result)

    async def job(self, ident: UUID):
        async with self.database.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        sa.select(
                            s.jobs.c.id,
                            s.jobs.c.kind,
                            s.jobs.c.state,
                            s.jobs.c.stage,
                            s.jobs.c.attempts,
                            s.jobs.c.next_run_at,
                            s.jobs.c.deadline_at,
                            s.jobs.c.result_id,
                            s.jobs.c.last_error,
                            s.jobs.c.created_at,
                            s.jobs.c.updated_at,
                        ).where(s.jobs.c.id == ident)
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise DomainError("JOB_NOT_FOUND", "任务不存在", 404)
        return json_value(dict(row))

    async def object(self, table, ident: UUID, code: str):
        async with self.database.engine.connect() as connection:
            row = (
                (await connection.execute(sa.select(table).where(table.c.id == ident)))
                .mappings()
                .first()
            )
        if row is None:
            raise DomainError(code, "记录不存在", 404)
        return json_value(dict(row))

    async def strategies(self, cursor: int | None = None, limit=20):
        async with self.database.engine.connect() as connection:
            query = sa.select(s.strategy_versions).order_by(s.strategy_versions.c.version.desc())
            if cursor is not None:
                query = query.where(s.strategy_versions.c.version < cursor)
            rows = (await connection.execute(query.limit(limit + 1))).mappings().all()
        return json_value(
            {
                "items": [dict(r) for r in rows[:limit]],
                "next_cursor": rows[limit - 1]["version"] if len(rows) > limit else None,
            }
        )

    async def status(self):
        async with self.database.engine.connect() as connection:
            now = await self.database.now(connection)
            control = (
                (
                    await connection.execute(
                        sa.select(
                            s.runtime_control.c.paused, s.runtime_control.c.forecast_cursor
                        ).where(s.runtime_control.c.id == 1)
                    )
                )
                .mappings()
                .one()
            )
            jobs = (
                (
                    await connection.execute(
                        sa.select(s.jobs.c.state, sa.func.count().label("count")).group_by(
                            s.jobs.c.state
                        )
                    )
                )
                .mappings()
                .all()
            )
            slots = (
                (
                    await connection.execute(
                        sa.select(s.schedule_slots.c.status, sa.func.count().label("count"))
                        .where(s.schedule_slots.c.kind == "FORECAST")
                        .group_by(s.schedule_slots.c.status)
                    )
                )
                .mappings()
                .all()
            )
            notifications = (
                (
                    await connection.execute(
                        sa.select(s.outbox.c.status, sa.func.count().label("count")).group_by(
                            s.outbox.c.status
                        )
                    )
                )
                .mappings()
                .all()
            )
            measurements = await performance(connection, now)
        return json_value(
            {
                **dict(control),
                "jobs": {r["state"]: r["count"] for r in jobs},
                "forecast_slots": {r["status"]: r["count"] for r in slots},
                "notifications": {r["status"]: r["count"] for r in notifications},
                "server_time": now,
                "performance": measurements,
            }
        )
