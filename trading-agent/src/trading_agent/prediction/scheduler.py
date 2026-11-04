"""Minute verification and event-driven model scheduling in the existing runtime."""

from datetime import timedelta

import sqlalchemy as sa

from trading_agent.domain.serialization import json_value
from trading_agent.domain.time import boundary
from trading_agent.storage import schema as s
from trading_agent.storage.market import content_lock

PENDING = ("QUEUED", "RUNNING", "RETRY_WAIT", "AWAITING_DATA")


class PredictionScheduler:
    def __init__(self, jobs, config, engine_id, predictions):
        self.jobs, self.database, self.config = jobs, jobs.database, config
        self.engine_id, self.predictions = engine_id, predictions

    async def pending(self, connection, kind, symbol, timeframe=None):
        filters = [
            s.jobs.c.kind == kind,
            s.jobs.c.state.in_(PENDING),
            s.jobs.c.payload["engine_id"].as_string() == str(self.engine_id),
            s.jobs.c.payload["symbol"].as_string() == symbol,
        ]
        if timeframe:
            filters.append(s.jobs.c.payload["timeframe"].as_string() == timeframe)
        return await connection.scalar(sa.select(s.jobs.c.id).where(*filters).limit(1))

    async def tick(self):
        async with self.database.transaction() as connection:
            await self.jobs.ownership.guard(connection)
            # Serialize ticks without queuing UPDATE on the global fence row
            # ahead of unrelated task/lease writers holding SHARE.
            await content_lock(connection, f"prediction-scheduler:{self.engine_id}")
            control = (
                (
                    await connection.execute(
                        sa.select(s.runtime_control).where(s.runtime_control.c.id == 1)
                    )
                )
                .mappings()
                .one()
            )
            now = await self.database.now(connection)
            minute = boundary(now, "1m")
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            for symbol in self.config.prediction.symbols:
                # Continue verification and position protection even when analysis is paused.
                if not await self.pending(connection, "PREDICT_VERIFY", symbol):
                    await self.jobs.create(
                        connection,
                        "PREDICT_VERIFY",
                        f"verify:{self.engine_id}:{symbol}:{json_value(minute)}",
                        {"engine_id": str(self.engine_id), "symbol": symbol},
                        now,
                    )
                if control["paused"]:
                    continue
                if rounds := getattr(self.predictions, "rounds", None):
                    await rounds.tick(connection, self.engine_id, symbol, now)
                else:
                    rows = (
                        (
                            await connection.execute(
                                sa.select(
                                    s.prediction_heads.c.timeframe,
                                    s.prediction_heads.c.role,
                                    s.prediction_facts.c.expires_at,
                                    s.prediction_states.c.body,
                                )
                                .join(
                                    s.prediction_facts,
                                    s.prediction_facts.c.id == s.prediction_heads.c.prediction_id,
                                )
                                .join(
                                    s.prediction_states,
                                    s.prediction_states.c.prediction_id == s.prediction_facts.c.id,
                                )
                                .where(
                                    s.prediction_heads.c.engine_id == self.engine_id,
                                    s.prediction_heads.c.symbol == symbol,
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    active = {
                        (r["timeframe"], r["role"])
                        for r in rows
                        if r["body"]["lifecycle"] == "ACTIVE" and r["expires_at"] > now
                    }
                    for timeframe in self.config.prediction.timeframes:
                        if (timeframe, "TIMEFRAME") not in active and not await self.pending(
                            connection, "PREDICT_TIMEFRAME", symbol, timeframe
                        ):
                            await self.predictions.queue_timeframe(
                                connection,
                                self.engine_id,
                                symbol,
                                timeframe,
                                f"bootstrap:{json_value(minute)}",
                                now,
                            )
                    if ("1h", "PARENT") not in active and any(
                        role == "TIMEFRAME" for _, role in active
                    ):
                        if not await self.pending(connection, "PREDICT_PARENT", symbol):
                            await self.predictions.queue_parent(
                                connection, self.engine_id, symbol, now
                            )
                if now >= today + timedelta(minutes=20):
                    await self.jobs.create(
                        connection,
                        "PREDICT_REVIEW",
                        f"prediction-review:{self.engine_id}:{symbol}:{today.date()}",
                        {"engine_id": str(self.engine_id), "symbol": symbol},
                        today + timedelta(minutes=20),
                    )
            rules = (
                (
                    await connection.execute(
                        sa.select(s.horizon_heads.c.rule_id).where(
                            s.horizon_heads.c.engine_id == self.engine_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            for rule_id in rules:
                # Hourly deterministic learning; no LLM needed for replay/promote/rollback.
                await self.jobs.create(
                    connection,
                    "HORIZON_LEARN",
                    f"horizon:{rule_id}:{json_value(boundary(now, '1h'))}",
                    {"engine_id": str(self.engine_id), "rule_id": str(rule_id)},
                    now,
                )
