from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa

from trading_agent.domain.time import TIMEFRAMES
from trading_agent.market.models import Observation
from trading_agent.market.snapshots import build_snapshot
from trading_agent.storage import schema as s
from trading_agent.storage.market import get_snapshot, persist_bar, persist_observation


class MarketService:
    def __init__(self, jobs, client, forecasts, memory, engine_id, *, sampler=None):
        self.jobs, self.client, self.forecasts = jobs, client, forecasts
        self.memory, self.engine_id = memory, engine_id
        self.sampler = sampler

    async def prepare(self, attempt):
        async with self.jobs.transaction(attempt) as connection:
            existing = await connection.scalar(
                sa.select(s.job_inputs.c.snapshot_id).where(s.job_inputs.c.job_id == attempt.job_id)
            )
            if existing:
                return await get_snapshot(connection, existing)
        anchor = datetime.fromisoformat(attempt.payload["anchor_at"])
        deadline = anchor + timedelta(minutes=2)
        await self.client.check_clock(deadline)
        instrument = None
        if hasattr(self.client, "instrument"):
            instrument = await self.client.instrument(deadline)
        groups = await asyncio.gather(
            *(self.client.candles(anchor, timeframe, deadline) for timeframe in TIMEFRAMES)
        )
        observations = (
            await self.client.observations(deadline) if hasattr(self.client, "observations") else ()
        )
        data = {}
        async with self.jobs.transaction(attempt) as connection:
            for timeframe, bars in zip(TIMEFRAMES, groups, strict=True):
                data[timeframe] = tuple([await persist_bar(connection, bar) for bar in bars])
            observations = tuple([await persist_observation(connection, o) for o in observations])
            decision_at = await self.jobs.database.now(connection)
            if self.sampler:
                await self.sampler.windows(connection, anchor, decision_at, instrument=instrument)
                decision_at = await self.jobs.database.now(connection)
            # Recent retained observations matter for long-period cutoffs. A new 1h publication
            # must not erase older funding facts that were available for 4h/6h/12h context.
            latest = (
                sa.select(s.market_observations)
                .where(
                    s.market_observations.c.inst_id == self.forecasts.config.market.inst_id,
                    s.market_observations.c.available_at <= decision_at,
                    s.market_observations.c.event_at >= anchor - timedelta(days=40),
                    ~s.market_observations.c.source.startswith(
                        "okx_liquidation_raw:", autoescape=True
                    ),
                    s.market_observations.c.source != "okx_liquidation_stream:v1",
                )
                .distinct(
                    s.market_observations.c.source,
                    s.market_observations.c.kind,
                    s.market_observations.c.event_at,
                )
                .order_by(
                    s.market_observations.c.source,
                    s.market_observations.c.kind,
                    s.market_observations.c.event_at,
                    s.market_observations.c.revision_no.desc(),
                )
                .subquery()
            )
            ranked = sa.select(
                latest,
                sa.func.row_number()
                .over(
                    partition_by=(latest.c.source, latest.c.kind), order_by=latest.c.event_at.desc()
                )
                .label("recency"),
            ).subquery()
            rows = await connection.execute(sa.select(ranked).where(ranked.c.recency <= 100))
            observations = tuple(
                Observation.model_validate({key: row[key] for key in Observation.model_fields})
                for row in rows.mappings()
            )
        snapshot = await asyncio.to_thread(
            build_snapshot, anchor, decision_at, data, self.forecasts.config.features, observations
        )
        context = await self.memory.retrieve(
            UUID(str(self.engine_id)), self.forecasts.config.market.symbol, decision_at
        )
        await self.forecasts.prepare_input(
            attempt,
            snapshot,
            self.engine_id,
            case_ids=context.case_ids,
            observation_ids=context.observation_ids,
            stats=context.stats,
            context_text=context.text,
            strategy_id=UUID(attempt.payload["strategy_id"])
            if attempt.payload.get("strategy_id")
            else None,
        )
        return snapshot
