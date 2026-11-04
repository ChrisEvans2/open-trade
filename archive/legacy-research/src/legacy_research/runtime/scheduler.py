from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from trading_agent.config import AppConfig
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.domain.time import boundary
from trading_agent.jobs.service import JobService
from trading_agent.storage import schema as s


def low_priority_allowed(now: datetime) -> bool:
    anchor = boundary(now, "1h")
    return anchor + timedelta(minutes=4) < now < anchor + timedelta(minutes=55)


class Scheduler:
    def __init__(self, jobs: JobService, config: AppConfig, engine_id):
        self.jobs, self.database, self.config, self.engine_id = (
            jobs,
            jobs.database,
            config,
            engine_id,
        )

    async def tick(self):
        async with self.database.transaction() as connection:
            await self.jobs.ownership.guard(connection, exclusive=True)
            control = (
                (
                    await connection.execute(
                        sa.select(s.runtime_control)
                        .where(s.runtime_control.c.id == 1)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            now = await self.database.now(connection)
            current = boundary(now, "1h")
            cursor = control["forecast_cursor"] or current
            # Bounded batches catch up long outages without a long database transaction.
            for _ in range(168):
                if cursor > current:
                    break
                skipped = now > cursor + timedelta(minutes=2)
                status = "SKIPPED" if skipped else "PAUSED" if control["paused"] else "QUEUED"
                reason = (
                    "MISSED_COLLECT_WINDOW"
                    if skipped
                    else "SCHEDULE_PAUSED"
                    if control["paused"]
                    else None
                )
                job_id = None
                if status == "QUEUED":
                    job_id = await self.jobs.create(
                        connection,
                        "FORECAST",
                        f"forecast:{self.config.market.symbol}:{json_value(cursor)}",
                        {"anchor_at": json_value(cursor), "engine_id": str(self.engine_id)},
                        cursor + timedelta(minutes=1),
                        cursor + timedelta(minutes=4),
                    )
                await connection.execute(
                    insert(s.schedule_slots)
                    .values(
                        kind="FORECAST",
                        scheduled_at=cursor,
                        status=status,
                        reason=reason,
                        job_id=job_id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[s.schedule_slots.c.kind, s.schedule_slots.c.scheduled_at]
                    )
                )
                cursor += timedelta(hours=1)
            await connection.execute(
                sa.update(s.runtime_control)
                .where(s.runtime_control.c.id == 1)
                .values(forecast_cursor=cursor)
            )
            # Existing predictions remain eligible while prediction scheduling is paused.
            latest = (
                sa.select(
                    s.settlements.c.forecast_id,
                    sa.func.max(s.settlements.c.revision_no).label("revision"),
                )
                .group_by(s.settlements.c.forecast_id)
                .subquery()
            )
            forecasts = (
                (
                    await connection.execute(
                        sa.select(s.forecasts)
                        .outerjoin(latest, latest.c.forecast_id == s.forecasts.c.id)
                        .where(latest.c.forecast_id.is_(None))
                    )
                )
                .mappings()
                .all()
            )
            for forecast in forecasts:
                await self.jobs.create(
                    connection,
                    "SETTLE",
                    f"settle:{forecast['id']}:initial",
                    {"forecast_id": str(forecast["id"])},
                    forecast["start_at"] + timedelta(minutes=1),
                )
            await self._daily(connection, now)
            await self._refresh_reviews(connection, now)
            await self._pricing_revisions(connection, now)
            await self._price_refreshes(connection, now)
            if self.config.experiment.enabled:
                monday = now.replace(hour=0, minute=30, second=0, microsecond=0) - timedelta(
                    days=now.weekday()
                )
                if now >= monday:
                    await self.jobs.create(
                        connection,
                        "EXPERIMENT",
                        f"experiment:{self.engine_id}:{monday.date()}",
                        {"engine_id": str(self.engine_id)},
                        monday,
                    )

    async def _price_refreshes(self, connection, now):
        if not low_priority_allowed(now):
            return
        key = sa.func.concat(
            "price-refresh:", sa.cast(s.forecasts.c.id, sa.Text), ":", str(now.date())
        )
        candidates = (
            (
                await connection.execute(
                    sa.select(s.forecasts.c.id)
                    .where(
                        s.forecasts.c.anchor_at
                        >= now - timedelta(days=self.config.runtime.price_refresh_lookback_days),
                        s.forecasts.c.end_at + timedelta(minutes=1) <= now,
                        sa.exists(
                            sa.select(1).where(s.settlements.c.forecast_id == s.forecasts.c.id)
                        ),
                        ~sa.exists(sa.select(1).where(s.jobs.c.dedupe_key == key)),
                    )
                    .order_by(s.forecasts.c.anchor_at.desc())
                    .limit(self.config.runtime.price_refresh_batch_size)
                )
            )
            .scalars()
            .all()
        )
        for ident in candidates:
            await self.jobs.create(
                connection,
                "PRICE_REFRESH",
                f"price-refresh:{ident}:{now.date()}",
                {"forecast_id": str(ident)},
                now,
            )

    async def _daily(self, connection, now):
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now < today + timedelta(minutes=20):
            return
        key = f"review:{(today - timedelta(days=1)).date()}"
        if await connection.scalar(sa.select(s.jobs.c.id).where(s.jobs.c.dedupe_key == key)):
            return
        await self.jobs.create(
            connection,
            "REVIEW_DAILY",
            key,
            {
                "scope": "DAILY",
                "range_start": json_value(today - timedelta(days=1)),
                "range_end": json_value(today),
                "engine_id": str(self.engine_id),
            },
            today + timedelta(minutes=20),
        )

    async def _refresh_reviews(self, connection, now):
        requests = (
            (
                await connection.execute(
                    sa.select(s.review_refreshes)
                    .where(
                        s.review_refreshes.c.revision_counter
                        > s.review_refreshes.c.scheduled_counter,
                        s.review_refreshes.c.range_end + timedelta(minutes=20) <= now,
                    )
                    .order_by(s.review_refreshes.c.range_start)
                    .limit(168)
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        for row in requests:
            original = (
                (
                    await connection.execute(
                        sa.select(s.jobs).where(
                            s.jobs.c.dedupe_key == f"review:{row['range_start'].date()}"
                        )
                    )
                )
                .mappings()
                .first()
            )
            pending = (
                (await connection.execute(sa.select(s.jobs).where(s.jobs.c.id == row["job_id"])))
                .mappings()
                .first()
                if row["job_id"]
                else original
            )
            if (
                pending
                and pending["stage"] == "PLANNED"
                and pending["state"] in {"QUEUED", "RETRY_WAIT"}
            ):
                ident = pending["id"]
            else:
                key = (
                    f"review:{row['range_start'].date()}"
                    if original is None
                    else f"review:{row['range_start'].date()}:revision:{row['revision_counter']}"
                )
                ident = await self.jobs.create(
                    connection,
                    "REVIEW_DAILY",
                    key,
                    {
                        "scope": "DAILY",
                        "range_start": json_value(row["range_start"]),
                        "range_end": json_value(row["range_end"]),
                        "engine_id": str(self.engine_id),
                    },
                    now,
                )
            await connection.execute(
                sa.update(s.review_refreshes)
                .where(s.review_refreshes.c.range_start == row["range_start"])
                .values(job_id=ident, scheduled_counter=row["revision_counter"])
            )

    async def _pricing_revisions(self, connection, now):
        latest = (
            sa.select(s.market_bars)
            .where(s.market_bars.c.interval == "1m", s.market_bars.c.revision_no > 1)
            .distinct(s.market_bars.c.source, s.market_bars.c.inst_id, s.market_bars.c.open_at)
            .order_by(
                s.market_bars.c.source,
                s.market_bars.c.inst_id,
                s.market_bars.c.open_at,
                s.market_bars.c.revision_no.desc(),
            )
            .subquery()
        )
        original = s.market_bars.alias("original_pricing")
        changes = (
            (
                await connection.execute(
                    sa.select(
                        s.pricing_records.c.forecast_id,
                        s.pricing_records.c.endpoint,
                        latest.c.id.label("new_bar_id"),
                    )
                    .join(original, original.c.id == s.pricing_records.c.bar_id)
                    .join(
                        latest,
                        sa.and_(
                            latest.c.source == original.c.source,
                            latest.c.inst_id == original.c.inst_id,
                            latest.c.open_at == original.c.open_at,
                        ),
                    )
                    .where(latest.c.revision_no > original.c.revision_no)
                )
            )
            .mappings()
            .all()
        )
        affected = {}
        for row in changes:
            affected.setdefault(row["forecast_id"], {})[row["endpoint"]] = row["new_bar_id"]
        for forecast_id, endpoints in affected.items():
            settlement = (
                (
                    await connection.execute(
                        sa.select(s.settlements)
                        .where(s.settlements.c.forecast_id == forecast_id)
                        .order_by(s.settlements.c.revision_no.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if not settlement:
                continue
            start = endpoints.get("START", settlement["start_bar_id"])
            end = endpoints.get("END", settlement["end_bar_id"])
            if start == settlement["start_bar_id"] and end == settlement["end_bar_id"]:
                continue
            digest = content_hash({"start": start, "end": end})
            await self.jobs.create(
                connection,
                "SETTLE",
                f"settle:{forecast_id}:{digest}",
                {
                    "forecast_id": str(forecast_id),
                    "start_bar_id": str(start),
                    "end_bar_id": str(end),
                },
                now,
            )
