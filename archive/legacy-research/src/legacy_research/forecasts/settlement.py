from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from trading_agent.config import AppConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash
from legacy_research.forecasts.contracts import Probabilities
from legacy_research.forecasts.scoring import brier, realized_label
from trading_agent.jobs.service import Attempt, JobService
from trading_agent.market.models import Bar
from trading_agent.runtime.logging import attempt_event
from trading_agent.storage import schema as s
from trading_agent.storage.market import content_lock, persist_bar, row_bar


class SettlementService:
    def __init__(self, jobs: JobService, market, config: AppConfig):
        self.jobs, self.database, self.market, self.config = jobs, jobs.database, market, config

    async def _forecast(self, connection, attempt):
        if attempt.kind not in {"SETTLE", "PRICE_REFRESH"}:
            raise DomainError("FORBIDDEN", "只有定价任务可以访问结算证据", 403)
        row = (
            (
                await connection.execute(
                    sa.select(
                        s.forecasts,
                        s.snapshots.c.payload.label("snapshot_payload"),
                        s.engines.c.config_json.label("engine_config"),
                    )
                    .join(s.engines, s.engines.c.id == s.forecasts.c.engine_id)
                    .join(s.context_bundles, s.context_bundles.c.id == s.forecasts.c.bundle_id)
                    .join(s.snapshots, s.snapshots.c.id == s.context_bundles.c.snapshot_id)
                    .where(s.forecasts.c.id == UUID(attempt.payload["forecast_id"]))
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise DomainError("FORECAST_NOT_FOUND", "预测不存在", 404)
        return row

    async def _stored_price(self, connection, forecast_id, endpoint, explicit_id=None):
        query = (
            sa.select(s.market_bars)
            .join(s.pricing_records, s.pricing_records.c.bar_id == s.market_bars.c.id)
            .where(
                s.pricing_records.c.forecast_id == forecast_id,
                s.pricing_records.c.endpoint == endpoint,
            )
        )
        if explicit_id:
            query = query.where(s.market_bars.c.id == UUID(explicit_id))
        row = (
            (await connection.execute(query.order_by(s.market_bars.c.revision_no.desc()).limit(1)))
            .mappings()
            .first()
        )
        return row_bar(row) if row else None

    async def save_price(self, attempt: Attempt, endpoint: str, bar: Bar) -> Bar:
        bar = Bar.model_validate(bar)
        if endpoint not in {"START", "END"}:
            raise DomainError("INVALID_ENDPOINT", "定价端点错误")
        async with self.jobs.transaction(attempt) as connection:
            forecast = await self._forecast(connection, attempt)
            expected = forecast["start_at" if endpoint == "START" else "end_at"]
            group = forecast["snapshot_payload"]["groups"][0]
            source_bar = group["bars"][-1]
            now = await self.database.now(connection)
            if (
                bar.timeframe != "1m"
                or bar.open_at != expected
                or bar.inst_id != source_bar["inst_id"]
                or bar.source != source_bar["source"]
                or bar.close_boundary > now
                or bar.available_at > now
            ):
                raise DomainError("PRICING_MISMATCH", "定价必须是本预测的确切已收盘分钟与合约")
            stored = await persist_bar(connection, bar)
            # Recheck authority after revision allocation may have waited for another writer.
            await self.jobs.guard(connection, attempt)
            await connection.execute(
                insert(s.pricing_records)
                .values(
                    forecast_id=forecast["id"],
                    endpoint=endpoint,
                    bar_id=stored.id,
                    created_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        s.pricing_records.c.forecast_id,
                        s.pricing_records.c.endpoint,
                        s.pricing_records.c.bar_id,
                    ]
                )
            )
            await connection.execute(
                sa.update(s.jobs)
                .where(s.jobs.c.id == attempt.job_id)
                .values(
                    stage="ENTRY_PRICED" if endpoint == "START" else "EXIT_PRICED",
                    updated_at=now,
                )
            )
            return stored

    async def finalize(self, attempt: Attempt) -> UUID:
        if attempt.kind != "SETTLE":
            raise DomainError("FORBIDDEN", "只有结算任务可以追加评分", 403)
        async with self.jobs.transaction(attempt) as connection:
            forecast = await self._forecast(connection, attempt)
            await content_lock(connection, f"settlement:{forecast['id']}")
            start = await self._stored_price(
                connection, forecast["id"], "START", attempt.payload.get("start_bar_id")
            )
            end = await self._stored_price(
                connection, forecast["id"], "END", attempt.payload.get("end_bar_id")
            )
            if start is None or end is None:
                raise DomainError("PRICING_MISSING", "两个最终端点价格尚未齐备", 409)
            now = await self.database.now(connection)
            if end.close_boundary > now:
                raise DomainError("PRICING_NOT_DUE", "预测窗口终点尚未收盘", 409)
            previous = (
                (
                    await connection.execute(
                        sa.select(s.settlements)
                        .where(s.settlements.c.forecast_id == forecast["id"])
                        .order_by(s.settlements.c.revision_no.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            existing = await connection.scalar(
                sa.select(s.settlements.c.id).where(
                    s.settlements.c.forecast_id == forecast["id"],
                    s.settlements.c.start_bar_id == start.id,
                    s.settlements.c.end_bar_id == end.id,
                )
            )
            if existing:
                await self.jobs.finish_in_transaction(
                    connection, attempt, result_id=existing, stage="SETTLED"
                )
                return existing
            # An explicit older revision cannot supersede newer prices.
            if previous:
                old_rows = (
                    (
                        await connection.execute(
                            sa.select(s.market_bars).where(
                                s.market_bars.c.id.in_(
                                    (previous["start_bar_id"], previous["end_bar_id"])
                                )
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                revisions = {r["open_at"]: r["revision_no"] for r in old_rows}
                if (
                    start.revision_no < revisions[start.open_at]
                    or end.revision_no < revisions[end.open_at]
                ):
                    raise DomainError("OLD_PRICING_REVISION", "旧定价不能取代较新结算", 409)
            change, actual = realized_label(
                start.open,
                end.open,
                Decimal(forecast["engine_config"]["forecast"]["label_threshold"]),
            )
            probabilities = Probabilities.model_validate(
                {key: forecast["payload"][key] for key in ("p_up", "p_flat", "p_down")}
            )
            score = brier(probabilities, actual)
            facts = dict(
                forecast_id=forecast["id"],
                revision_no=previous["revision_no"] + 1 if previous else 1,
                start_bar_id=start.id,
                end_bar_id=end.id,
                realized_return=change,
                actual_label=actual,
                correct=forecast["label"] == actual,
                brier=score,
                supersedes_id=previous["id"] if previous else None,
            )
            ident = uuid4()
            await connection.execute(
                insert(s.settlements).values(
                    id=ident,
                    **facts,
                    payload_hash=content_hash(facts),
                    settled_at=now,
                )
            )
            await connection.execute(
                insert(s.audit_events).values(
                    actor_type="runtime",
                    actor_id=str(attempt.id),
                    action="FORECAST_SETTLED",
                    object_type="settlement",
                    object_id=ident,
                    after_hash=content_hash(facts),
                    request_id=str(attempt.id),
                )
            )
            day = forecast["end_at"].replace(hour=0, minute=0, second=0, microsecond=0)
            await connection.execute(
                insert(s.review_refreshes)
                .values(
                    range_start=day,
                    range_end=day + timedelta(days=1),
                )
                .on_conflict_do_update(
                    index_elements=[s.review_refreshes.c.range_start],
                    set_={"revision_counter": s.review_refreshes.c.revision_counter + 1},
                )
            )
            await self.jobs.finish_in_transaction(
                connection, attempt, result_id=ident, stage="SETTLED"
            )
        attempt_event("forecast_settled", attempt, forecast_id=str(forecast["id"]), stage="SETTLED")
        return ident

    async def _wait(self, attempt, next_at: datetime, stage: str, *, missing=False, code=None):
        async with self.jobs.transaction(attempt) as connection:
            job, _, now = await self.jobs.guard(connection, attempt)
            if missing:
                first = job["first_data_missing_at"] or now
                delays = (60, 300, 900, 3600)
                next_at = now + timedelta(seconds=delays[min(job["data_retry_count"], 3)])
                await connection.execute(
                    sa.update(s.jobs)
                    .where(s.jobs.c.id == attempt.job_id)
                    .values(
                        data_retry_count=job["data_retry_count"] + 1,
                        first_data_missing_at=first,
                    )
                )
                if now >= first + timedelta(hours=24) and self.config.notification.target:
                    key = f"pricing-missing:{attempt.job_id}:{now.date()}"
                    event_id = content_hash(key)[:12]
                    await connection.execute(
                        insert(s.outbox)
                        .values(
                            event_key=key,
                            target=self.config.notification.target,
                            body=(
                                f"结算行情持续缺失，请检查任务 {attempt.job_id}。 "
                                f"[event:{event_id}]"
                            ),
                            next_attempt_at=now,
                        )
                        .on_conflict_do_nothing(index_elements=[s.outbox.c.event_key])
                    )
            await self.jobs.finish_in_transaction(
                connection,
                attempt,
                state="AWAITING_DATA",
                next_run_at=next_at,
                stage=stage,
                error={"code": code or "PRICING_MISSING"} if missing else None,
            )

    async def run(self, attempt: Attempt) -> UUID | None:
        async with self.jobs.transaction(attempt) as connection:
            forecast = await self._forecast(connection, attempt)
        for endpoint, time_key, id_key in (
            ("START", "start_at", "start_bar_id"),
            ("END", "end_at", "end_bar_id"),
        ):
            async with self.jobs.transaction(attempt) as connection:
                stored = await self._stored_price(
                    connection, forecast["id"], endpoint, attempt.payload.get(id_key)
                )
                now = await self.database.now(connection)
            if stored:
                continue
            due = forecast[time_key] + timedelta(minutes=1)
            stage = "WAIT_ENTRY_PRICE" if endpoint == "START" else "WAIT_EXIT_PRICE"
            if now < due:
                await self._wait(attempt, due, stage)
                return None
            if attempt.payload.get(id_key):
                async with self.jobs.transaction(attempt) as connection:
                    row = (
                        (
                            await connection.execute(
                                sa.select(s.market_bars).where(
                                    s.market_bars.c.id == UUID(attempt.payload[id_key])
                                )
                            )
                        )
                        .mappings()
                        .first()
                    )
                bar = row_bar(row) if row else None
            else:
                try:
                    bar = await self.market.pricing_bar(
                        forecast[time_key],
                        now + timedelta(seconds=45),
                        inst_id=forecast["snapshot_payload"]["groups"][0]["bars"][-1]["inst_id"],
                    )
                except DomainError as exc:
                    await self._wait(attempt, now, stage, missing=True, code=exc.code)
                    return None
            if bar is None:
                await self._wait(attempt, now, stage, missing=True)
                return None
            await self.save_price(attempt, endpoint, bar)
        return await self.finalize(attempt)
