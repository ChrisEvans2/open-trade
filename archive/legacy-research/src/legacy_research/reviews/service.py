from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash, json_value
from legacy_research.memory.retrieval import case_metrics, latest_settlements
from trading_agent.notifications.outbox import enqueue
from legacy_research.reviews.contracts import ReviewSubmission
from trading_agent.storage import schema as s
from trading_agent.storage.market import content_lock


class ReviewService:
    def __init__(self, jobs, config):
        self.jobs, self.database, self.config = jobs, jobs.database, config

    async def _input(self, connection, attempt):
        if attempt.kind not in {"REVIEW_DAILY", "EXPLAIN"}:
            raise DomainError("FORBIDDEN", "只有复盘或解释任务可以读取此上下文", 403)
        row = (
            (
                await connection.execute(
                    sa.select(s.review_inputs).where(s.review_inputs.c.job_id == attempt.job_id)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise DomainError("INPUT_NOT_FROZEN", "复盘证据尚未冻结", 409)
        return row

    async def prepare(self, attempt):
        async with self.jobs.transaction(attempt) as connection:
            if await connection.scalar(
                sa.select(s.review_inputs.c.job_id).where(
                    s.review_inputs.c.job_id == attempt.job_id
                )
            ):
                return
            now = await self.database.now(connection)
            if attempt.kind == "EXPLAIN":
                forecast = (
                    (
                        await connection.execute(
                            sa.select(s.forecasts).where(
                                s.forecasts.c.id == UUID(attempt.payload["forecast_id"])
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if forecast is None:
                    raise DomainError("FORECAST_NOT_FOUND", "解释对象不存在", 404)
                start, end, engine = (
                    forecast["start_at"],
                    forecast["end_at"] + timedelta(microseconds=1),
                    forecast["engine_id"],
                )
                where = s.forecasts.c.id == forecast["id"]
                scope = "EXPLAIN"
            elif attempt.kind == "REVIEW_DAILY":
                start, end = map(
                    datetime.fromisoformat,
                    (attempt.payload["range_start"], attempt.payload["range_end"]),
                )
                if end > now or start >= end:
                    raise DomainError("INVALID_REVIEW_RANGE", "复盘区间必须是已发生的半开区间")
                engine = UUID(attempt.payload["engine_id"])
                where = sa.and_(s.forecasts.c.end_at >= start, s.forecasts.c.end_at < end)
                scope = attempt.payload.get("scope", "DAILY")
            else:
                raise DomainError("FORBIDDEN", "任务类型不能生成复盘", 403)
            source_counter = 0
            if scope == "DAILY":
                source_counter = (
                    await connection.scalar(
                        sa.select(s.review_refreshes.c.revision_counter)
                        .where(s.review_refreshes.c.range_start == start)
                        .with_for_update(read=True)
                    )
                ) or 0
            latest = latest_settlements(now)
            rows = (
                (
                    await connection.execute(
                        sa.select(
                            s.forecasts.c.id.label("forecast_id"),
                            s.forecasts.c.engine_id,
                            s.forecasts.c.label,
                            s.forecasts.c.published_at,
                            s.forecasts.c.start_at,
                            s.forecasts.c.end_at,
                            s.forecasts.c.payload,
                            latest.c.id.label("settlement_id"),
                            latest.c.correct,
                            latest.c.brier,
                            latest.c.actual_label,
                            latest.c.realized_return,
                            latest.c.settled_at,
                        )
                        .outerjoin(latest, latest.c.forecast_id == s.forecasts.c.id)
                        .where(where, s.forecasts.c.published_at <= now)
                        .order_by(s.forecasts.c.end_at)
                    )
                )
                .mappings()
                .all()
            )
            scored = [r for r in rows if r["settlement_id"]]
            engines = {r["engine_id"] for r in scored}
            metrics = {
                "by_engine": {
                    str(i): case_metrics([r for r in scored if r["engine_id"] == i])
                    for i in engines
                }
            }
            planned = await connection.scalar(
                sa.select(sa.func.count())
                .select_from(s.schedule_slots)
                .where(
                    s.schedule_slots.c.kind == "FORECAST",
                    s.schedule_slots.c.scheduled_at >= start,
                    s.schedule_slots.c.scheduled_at < end,
                    s.schedule_slots.c.scheduled_at <= now,
                )
            )
            published = await connection.scalar(
                sa.select(sa.func.count())
                .select_from(s.forecasts)
                .where(
                    s.forecasts.c.anchor_at >= start,
                    s.forecasts.c.anchor_at < end,
                    s.forecasts.c.published_at <= now,
                )
            )
            metrics.update(
                planned_slots=planned,
                published_forecasts=published,
                coverage=Decimal(published) / planned if planned else None,
                scored_count=len(scored),
                pending_count=len(rows) - len(scored),
                period_basis="forecast.end_at; coverage uses forecast.anchor_at",
                attribution="未导入合约成交；此报告只验证价格预测，不推断交易收益",
            )
            evidence = sorted(
                {str(r[k]) for r in rows for k in ("forecast_id", "settlement_id") if r[k]}
            )
            payload = dict(
                job_id=attempt.job_id,
                engine_id=engine,
                scope=scope,
                range_start=start,
                range_end=end,
                decision_at=now,
                source_revision_counter=source_counter,
                cases=[dict(r) for r in rows],
                evidence_ids=evidence,
                metrics=metrics,
            )
            await connection.execute(
                insert(s.review_inputs).values(
                    **json_value(payload), content_hash=content_hash(payload)
                )
            )
            await connection.execute(
                sa.update(s.jobs)
                .where(s.jobs.c.id == attempt.job_id)
                .values(stage="REVIEW_FROZEN", updated_at=now)
            )

    async def context(self, attempt):
        async with self.jobs.transaction(attempt) as connection:
            row = await self._input(connection, attempt)
            existing = (
                (
                    await connection.execute(
                        sa.select(s.reviews.c.id, s.reviews.c.submission_hash).where(
                            s.reviews.c.attempt_id == attempt.id
                        )
                    )
                )
                .mappings()
                .first()
            )
            return json_value(
                {
                    "attempt_id": attempt.id,
                    "review": dict(row),
                    "submitted_review": dict(existing) if existing else None,
                }
            )

    def _candidate_stats(self, candidate, inputs):
        selected = {str(i) for i in candidate.evidence_ids}
        rows = [
            r
            for r in inputs["cases"]
            if r["settlement_id"]
            and (r["forecast_id"] in selected or r["settlement_id"] in selected)
        ]
        if not rows or any(r["engine_id"] != str(inputs["engine_id"]) for r in rows):
            return None
        # JSON stores exact numeric strings; reconstitute program inputs before calculating.
        rows = [{**r, "brier": Decimal(r["brier"])} for r in rows]
        expected = case_metrics(rows)
        valid = (
            candidate.stats.sample_count == expected["sample_count"]
            and candidate.stats.correct_count == expected["correct_count"]
            and abs(candidate.stats.direction_accuracy - expected["direction_accuracy"])
            <= Decimal("0.000000000001")
            and abs(candidate.stats.mean_brier - expected["mean_brier"])
            <= Decimal("0.000000000001")
        )
        return expected if valid else None

    async def submit(self, attempt, submission: ReviewSubmission):
        submission = ReviewSubmission.model_validate(submission)
        repair_needed = False
        async with self.jobs.transaction(attempt) as connection:
            inputs = await self._input(connection, attempt)
            await content_lock(
                connection,
                f"review:{inputs['scope']}:{inputs['range_start']}:{inputs['range_end']}",
            )
            job, record, now = await self.jobs.guard(connection, attempt)
            digest = content_hash(submission)
            prior = (
                (
                    await connection.execute(
                        sa.select(s.reviews).where(s.reviews.c.attempt_id == attempt.id)
                    )
                )
                .mappings()
                .first()
            )
            if prior:
                if prior["submission_hash"] != digest:
                    raise DomainError("REVIEW_ALREADY_SUBMITTED", "本attempt已有不同复盘", 409)
                return {"review_id": str(prior["id"])}
            allowed = set(inputs["evidence_ids"])
            for item in (submission, *submission.observations, *submission.hypotheses):
                if not {str(i) for i in item.evidence_ids} <= allowed:
                    raise DomainError("EVIDENCE_FORBIDDEN", "复盘引用未授权证据", 403)
            checked = [self._candidate_stats(item, inputs) for item in submission.observations]
            invalid = any(value is None for value in checked)
            if invalid:
                await connection.execute(
                    sa.update(s.jobs)
                    .where(s.jobs.c.id == attempt.job_id)
                    .values(review_validation_failures=s.jobs.c.review_validation_failures + 1)
                )
                await connection.execute(
                    sa.update(s.job_attempts)
                    .where(s.job_attempts.c.id == attempt.id)
                    .values(review_validation_failures=record["review_validation_failures"] + 1)
                )
            if invalid and job["review_validation_failures"] == 0:
                repair_needed = True
            else:
                revision = (
                    await connection.scalar(
                        sa.select(sa.func.max(s.reviews.c.revision_no)).where(
                            s.reviews.c.scope == inputs["scope"],
                            s.reviews.c.range_start == inputs["range_start"],
                            s.reviews.c.range_end == inputs["range_end"],
                        )
                    )
                ) or 0
                ident = uuid4()
                await connection.execute(
                    insert(s.reviews).values(
                        id=ident,
                        scope=inputs["scope"],
                        range_start=inputs["range_start"],
                        range_end=inputs["range_end"],
                        revision_no=revision + 1,
                        evidence_ids=json_value(submission.evidence_ids),
                        metrics=inputs["metrics"],
                        narrative=submission.narrative,
                        attempt_id=attempt.id,
                        submission_hash=digest,
                        created_at=now,
                    )
                )
                for item, stats in zip(submission.observations, checked, strict=True):
                    await connection.execute(
                        insert(s.observations).values(
                            kind="observation",
                            engine_id=inputs["engine_id"],
                            regime=None,
                            evidence_ids=json_value(item.evidence_ids),
                            stats=json_value(stats or item.stats),
                            text=item.text,
                            verification_status="VERIFIED" if stats else "REJECTED",
                            available_at=now,
                            expires_at=now + timedelta(days=30),
                        )
                    )
                for item in submission.hypotheses:
                    await connection.execute(
                        insert(s.observations).values(
                            kind="hypothesis",
                            engine_id=inputs["engine_id"],
                            regime=None,
                            evidence_ids=json_value(item.evidence_ids),
                            stats={},
                            text=item.text,
                            verification_status="CANDIDATE",
                            available_at=now,
                            expires_at=now + timedelta(days=30),
                        )
                    )
                await connection.execute(
                    insert(s.audit_events).values(
                        actor_type="research_attempt",
                        actor_id=str(attempt.id),
                        action="REVIEW_SUBMITTED",
                        object_type="review",
                        object_id=ident,
                        after_hash=digest,
                        request_id=str(attempt.id),
                    )
                )
                if inputs["scope"] == "DAILY" and revision == 0:
                    body = (
                        f"UTC {inputs['range_start'].date()} 预测复盘："
                        f"按时发布 {inputs['metrics']['published_forecasts']}/"
                        f"{inputs['metrics']['planned_slots']}，"
                        f"已评分 {inputs['metrics']['scored_count']}，"
                        f"待验证 {inputs['metrics']['pending_count']}。\n" + submission.narrative
                    )
                    await enqueue(
                        connection,
                        key=f"review:{ident}",
                        target=self.config.notification.target,
                        body=body[: self.config.notification.daily_summary_max_chars - 26],
                        now=now,
                    )
                if inputs["scope"] == "DAILY":
                    await connection.execute(
                        sa.update(s.review_refreshes)
                        .where(s.review_refreshes.c.range_start == inputs["range_start"])
                        .values(
                            scheduled_counter=sa.func.greatest(
                                s.review_refreshes.c.scheduled_counter,
                                inputs["source_revision_counter"],
                            )
                        )
                    )
                await connection.execute(
                    sa.update(s.jobs)
                    .where(s.jobs.c.id == attempt.job_id)
                    .values(stage="REVIEW_SUBMITTED", result_id=ident, updated_at=now)
                )
                return {"review_id": str(ident)}
        if repair_needed:
            raise DomainError(
                "OBSERVATION_STATS_MISMATCH", "经验统计与冻结证据不一致；允许修正一次"
            )
