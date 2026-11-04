"""Owned background REST collector. Models never schedule or perform this I/O."""

from __future__ import annotations

import asyncio
import json
import zlib
from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from trading_agent.domain.errors import DomainError, StaleAttempt
from trading_agent.domain.serialization import json_value
from trading_agent.domain.time import boundary
from trading_agent.market.models import Observation
from trading_agent.market.observations import timestamp
from trading_agent.market.trades import MINUTE_SOURCE, capture_minute, face_value
from trading_agent.storage import schema as s
from trading_agent.storage.market import persist_bar, persist_observation

TRANSIENT = {
    "MARKET_UNAVAILABLE",
    "MARKET_DEADLINE",
    "MARKET_REJECTED",
    "TRADE_CYCLE_BUDGET",
    "TRADE_SOURCE_EMPTY",
    "TRADE_CANDLE_UNAVAILABLE",
}


class TradeMinuteCollector:
    def __init__(self, database, ownership, client, config, *, inst_ids, on_fatal=None):
        self.database, self.ownership, self.client = database, ownership, client
        self.settings = config.trade_minutes
        self.inst_ids = tuple(inst_ids)
        if not self.inst_ids or set(self.inst_ids) - {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}:
            raise ValueError("逐笔采集仅支持已配置BTC/ETH USDT永续")
        self.on_fatal, self.task = on_fatal, None
        self._cycle_lock = asyncio.Lock()
        self.last_audit, self.last_progress = {}, None
        self.failure_code = None
        self.last_cycle = {}
        self._rotation = 0

    async def start(self):
        self.task = asyncio.create_task(self.run(), name="trade-minute-collector")

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def health(self):
        return {
            "running": self.task is not None and not self.task.done(),
            "failure_code": self.failure_code,
            "last_progress": self.last_progress.isoformat() if self.last_progress else None,
            "last_cycle": self.last_cycle,
        }

    async def run(self):
        while True:
            try:
                await self.tick()
            except StaleAttempt:
                self.failure_code = "RUNTIME_OWNERSHIP_LOST"
                if self.on_fatal:
                    self.on_fatal(self.failure_code)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failure_code = (
                    exc.code if isinstance(exc, DomainError) else "TRADE_COLLECTOR_FAILED"
                )
            await asyncio.sleep(self.settings.poll_seconds)

    async def plan(self):
        """Only scheduling/retention writes; never network calls inside this transaction."""
        work = s.trade_minute_work
        async with self.database.transaction() as connection:
            await self.ownership.guard(connection)
            now = await self.database.now(connection)
            latest = boundary(
                now - timedelta(seconds=self.settings.settle_delay_seconds), "1m"
            ) - timedelta(minutes=1)
            cutoff = latest - timedelta(days=self.settings.repair_lookback_days)
            queues = []
            audits = []
            offset = self._rotation % len(self.inst_ids)
            ordered = self.inst_ids[offset:] + self.inst_ids[:offset]
            self._rotation += 1
            for inst in ordered:
                previous = await connection.scalar(
                    sa.select(sa.func.max(work.c.open_at)).where(work.c.inst_id == inst)
                )
                start = max(
                    previous + timedelta(minutes=1)
                    if previous
                    else latest
                    - timedelta(days=self.settings.bootstrap_days)
                    + timedelta(minutes=1),
                    cutoff + timedelta(minutes=1),
                )
                if start <= latest:
                    await connection.execute(
                        sa.text(
                            "INSERT INTO trade_minute_work"
                            "(inst_id,open_at,state,attempts,next_retry_at,updated_at) "
                            "SELECT :inst, t, 'PENDING', 0, :now, :now FROM "
                            "generate_series(CAST(:start AS timestamptz), "
                            "CAST(:end AS timestamptz), interval '1 minute') t "
                            "ON CONFLICT(inst_id,open_at) DO NOTHING"
                        ),
                        {"inst": inst, "start": start, "end": latest, "now": now},
                    )
                await connection.execute(
                    sa.update(work)
                    .where(
                        work.c.inst_id == inst,
                        work.c.open_at <= cutoff,
                        work.c.state.in_(("PENDING", "ERROR")),
                    )
                    .values(state="EXPIRED", reason="REPAIR_LOOKBACK_EXCEEDED", updated_at=now)
                )
                rows = (
                    (
                        await connection.execute(
                            sa.select(work)
                            .where(
                                work.c.inst_id == inst,
                                work.c.state.in_(("PENDING", "ERROR")),
                                work.c.next_retry_at <= now,
                                work.c.open_at > cutoff,
                            )
                            .order_by(
                                sa.case(
                                    (work.c.open_at >= latest - timedelta(minutes=2), 0), else_=1
                                ),
                                sa.case((work.c.state == "ERROR", 0), else_=1),
                                work.c.open_at.desc(),
                            )
                            .limit(self.settings.max_minutes_per_cycle)
                        )
                    )
                    .mappings()
                    .all()
                )
                queues.append(list(rows))
                if now - self.last_audit.get(inst, now - timedelta(days=1)) >= timedelta(
                    minutes=self.settings.audit_interval_minutes
                ):
                    audit = (
                        (
                            await connection.execute(
                                sa.select(work)
                                .where(
                                    work.c.inst_id == inst,
                                    work.c.state == "VERIFIED",
                                    work.c.open_at < latest - timedelta(minutes=5),
                                    work.c.open_at > cutoff,
                                )
                                .order_by(work.c.updated_at, work.c.open_at)
                                .limit(1)
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if audit:
                        audits.append((audit, True))
            # Delete only the explicitly disposable cache, never observations/bars/inputs.
            await connection.execute(
                sa.delete(s.trade_raw_cache).where(s.trade_raw_cache.c.expires_at <= now)
            )
        due = [
            (queue[i], False)
            for i in range(self.settings.max_minutes_per_cycle)
            for queue in queues
            if i < len(queue)
        ]
        # Rotate assets across cycles; due audits must survive a busy first minute.
        plan = audits + due
        return plan[: self.settings.max_minutes_per_cycle]

    async def tick(self):
        async with self._cycle_lock:
            return await self._tick()

    async def _tick(self):
        plan = await self.plan()
        deadline = self.client.now() + timedelta(seconds=self.settings.cycle_budget_seconds)
        requests, completed, invalid = 0, 0, 0

        async def get(path, params, until):
            nonlocal requests
            if requests >= self.settings.max_requests_per_cycle:
                raise DomainError("TRADE_CYCLE_BUDGET", "逐笔采集已达本轮请求预算", 503)
            requests += 1
            return await self.client.get(path, params, until)

        if plan:
            before = self.client.now()
            clock = await get("/api/v5/public/time", {}, deadline)
            midpoint = before + (self.client.now() - before) / 2
            if (
                abs((timestamp(clock[0]["ts"]) - midpoint).total_seconds())
                > self.client.config.clock_skew_seconds
            ):
                raise DomainError("CLOCK_SKEW", "逐笔采集时钟偏差超限", 503)
        metadata = {}
        for item, audit in plan:
            if requests >= self.settings.max_requests_per_cycle or self.client.now() >= deadline:
                break
            inst = item["inst_id"]
            if inst not in metadata:
                records = await get(
                    "/api/v5/public/instruments", {"instType": "SWAP", "instId": inst}, deadline
                )
                found = [r for r in records if r.get("instId") == inst]
                if len(found) != 1:
                    raise DomainError("TRADE_CONTRACT_UNSUPPORTED", "逐笔合约元数据缺失", 503)
                face_value(found[0], inst)
                metadata[inst] = found[0]
            capture = await capture_minute(
                self.client, inst, item["open_at"], deadline, self.settings, metadata[inst], get
            )
            await self.record(capture, audit=audit)
            if audit:
                self.last_audit[inst] = self.client.now()
            completed += capture.observation.quality == "OK"
            invalid += capture.observation.quality != "OK"
        self.last_progress = self.client.now()
        self.failure_code = None
        self.last_cycle = {"requests": requests, "verified": completed, "incomplete": invalid}
        return self.last_cycle

    async def record(self, capture, *, audit=False):
        o = capture.observation
        if o.inst_id not in self.inst_ids or o.source != MINUTE_SOURCE:
            raise DomainError("TRADE_SCOPE", "逐笔采集结果范围错误", 503)
        async with self.database.transaction() as connection:
            await self.ownership.guard(connection)
            now = await self.database.now(connection)
            if o.available_at > now or o.coverage_end > now:
                raise DomainError("TRADE_FUTURE_DATA", "逐笔结果不可提前落库", 503)
            work = s.trade_minute_work
            clause = (work.c.inst_id == o.inst_id) & (work.c.open_at == o.coverage_start)
            previous_work = (
                (await connection.execute(sa.select(work).where(clause).with_for_update()))
                .mappings()
                .first()
            )
            previous = (
                (
                    await connection.execute(
                        sa.select(s.market_observations)
                        .where(
                            s.market_observations.c.source == MINUTE_SOURCE,
                            s.market_observations.c.inst_id == o.inst_id,
                            s.market_observations.c.event_at == o.event_at,
                        )
                        .order_by(s.market_observations.c.revision_no.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            retain_verified = bool(
                audit
                and o.payload.get("reason") in TRANSIENT
                and previous
                and previous["quality"] == "OK"
            )
            if capture.rows:
                body = json.dumps(
                    json_value(capture.rows), separators=(",", ":"), sort_keys=True
                ).encode()
                await connection.execute(
                    insert(s.trade_raw_cache)
                    .values(
                        inst_id=o.inst_id,
                        open_at=o.coverage_start,
                        digest=o.payload["raw_digest"],
                        received_at=o.available_at,
                        expires_at=now + timedelta(hours=self.settings.raw_retention_hours),
                        body_zlib=zlib.compress(body),
                        trade_count=len(capture.rows),
                    )
                    .on_conflict_do_nothing()
                )
            if capture.candle:
                await persist_bar(
                    connection, capture.candle, o.inst_id.replace("-", "").removesuffix("SWAP")
                )
            if not retain_verified:
                await persist_observation(connection, o)
            if audit:
                prior = previous["payload"] if previous else {}
                audit_payload = {
                    "reason": o.payload.get("reason"),
                    "previous_id": str(previous["id"]) if previous else None,
                    "current_digest": o.payload["raw_digest"],
                    "previous_digest": prior.get("raw_digest"),
                    "allocation_changed": o.quality == "OK"
                    and bool(previous)
                    and any(
                        prior.get(k) is None or Decimal(str(prior[k])) != Decimal(str(o.payload[k]))
                        for k in ("buy", "sell")
                    ),
                    "prior_verified_retained": retain_verified,
                }
                await persist_observation(
                    connection,
                    Observation(
                        source="okx_trade_minute_audit:v1",
                        inst_id=o.inst_id,
                        kind="trade_audit",
                        event_at=now,
                        available_at=now,
                        coverage_start=o.coverage_start,
                        coverage_end=o.coverage_end,
                        unit="audit",
                        quality=o.quality,
                        payload=audit_payload,
                    ),
                )
            attempts = (previous_work["attempts"] if previous_work else 0) + 1
            valid = o.quality == "OK" or retain_verified
            values = dict(
                state="VERIFIED" if valid else "ERROR",
                attempts=attempts,
                updated_at=now,
                next_retry_at=now
                + timedelta(
                    seconds=min(3600, self.settings.retry_seconds * 2 ** min(attempts - 1, 6))
                ),
                reason=o.payload.get("reason"),
            )
            await connection.execute(
                insert(work)
                .values(inst_id=o.inst_id, open_at=o.coverage_start, **values)
                .on_conflict_do_update(
                    index_elements=[work.c.inst_id, work.c.open_at],
                    set_=values,
                )
            )
