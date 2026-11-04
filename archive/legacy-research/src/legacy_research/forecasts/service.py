from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from trading_agent.config import AppConfig
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.domain.time import INTERVAL_SECONDS, TRENDS, require_anchor, window
from legacy_research.forecasts.contracts import (
    ForecastSubmission,
    OneHourAnalysis,
    TrendAnalysis,
    require_same_probabilities,
)
from trading_agent.jobs.service import Attempt, JobService
from trading_agent.market.snapshots import Snapshot
from trading_agent.runtime.logging import attempt_event
from trading_agent.storage import schema as s
from trading_agent.storage.market import get_snapshot, persist_snapshot


class ForecastService:
    def __init__(self, jobs: JobService, config: AppConfig):
        self.jobs, self.database, self.config = jobs, jobs.database, config

    async def register_engine(self, prompt_hashes: dict[str, str]) -> UUID:
        if not self.config.hermes.provider or not self.config.hermes.model:
            raise DomainError("MODEL_NOT_CONFIGURED", "交易Profile模型未锁定", 503)
        if set(prompt_hashes) != {"parent", "1h", "4h", "6h", "12h", "review"}:
            raise ValueError("All versioned research prompts must be hashed")
        config = json_value(self.config)
        digest = content_hash({"config": self.config.engine_config_hash, "prompts": prompt_hashes})
        async with self.database.transaction() as connection:
            ident = await connection.scalar(
                insert(s.engines)
                .values(
                    hermes_commit=self.config.hermes.reference_commit,
                    provider=self.config.hermes.provider,
                    model=self.config.hermes.model,
                    prompt_hash=content_hash(prompt_hashes),
                    feature_version=self.config.features.spec_version,
                    config_json=config,
                    engine_hash=digest,
                )
                .on_conflict_do_nothing(index_elements=[s.engines.c.engine_hash])
                .returning(s.engines.c.id)
            )
            return ident or await connection.scalar(
                sa.select(s.engines.c.id).where(s.engines.c.engine_hash == digest)
            )

    async def _input(self, connection, attempt):
        record = (
            (
                await connection.execute(
                    sa.select(s.job_inputs).where(s.job_inputs.c.job_id == attempt.job_id)
                )
            )
            .mappings()
            .first()
        )
        if record is None:
            raise DomainError("INPUT_NOT_FROZEN", "本次任务尚未冻结输入", 409)
        return record, await get_snapshot(connection, record["snapshot_id"])

    async def prepare_input(
        self,
        attempt: Attempt,
        snapshot: Snapshot,
        engine_id: UUID,
        *,
        strategy_id: UUID | None = None,
        case_ids=(),
        observation_ids=(),
        stats: dict | None = None,
        context_text: str = "",
    ) -> UUID:
        if attempt.kind != "FORECAST":
            raise DomainError("FORBIDDEN", "只有预测任务可以冻结预测输入", 403)
        if attempt.payload.get("engine_id") and attempt.payload["engine_id"] != str(engine_id):
            raise DomainError("ENGINE_MISMATCH", "任务和冻结输入的engine不同", 409)
        anchor = require_anchor(datetime.fromisoformat(attempt.payload["anchor_at"]))
        if snapshot.anchor_at != anchor or len(context_text) > 6000:
            raise DomainError("INPUT_MISMATCH", "快照时段或历史摘要长度不合格")
        if (
            len(case_ids) > 5
            or len(observation_ids) > 3
            or len(set(case_ids)) != len(case_ids)
            or len(set(observation_ids)) != len(observation_ids)
        ):
            raise DomainError("EVIDENCE_LIMIT", "历史案例与经验超过限额或重复")
        async with self.jobs.transaction(attempt) as connection:
            now = await self.database.now(connection)
            existing = (
                (
                    await connection.execute(
                        sa.select(s.job_inputs).where(s.job_inputs.c.job_id == attempt.job_id)
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["engine_id"] != engine_id:
                    raise DomainError("ENGINE_MISMATCH", "不能替换已经冻结输入的engine", 409)
                return existing["snapshot_id"]  # Recovery always reuses the saved input.
            if (
                now < anchor + timedelta(minutes=1)
                or now > anchor + timedelta(minutes=2)
                or snapshot.decision_at > now
            ):
                raise DomainError("SNAPSHOT_DEADLINE", "快照冻结不满足采集截止时间", 409)
            if self.config.market.inst_id is not None and any(
                group.bars[-1].inst_id != self.config.market.inst_id for group in snapshot.groups
            ):
                raise DomainError("INSTRUMENT_MISMATCH", "快照标的与锁定配置不同")
            for ident in case_ids:
                eligible = await connection.scalar(
                    sa.select(s.forecasts.c.id)
                    .join(s.settlements, s.settlements.c.forecast_id == s.forecasts.c.id)
                    .where(
                        s.forecasts.c.id == ident,
                        s.forecasts.c.engine_id == engine_id,
                        s.forecasts.c.symbol == self.config.market.symbol,
                        s.forecasts.c.end_at - s.forecasts.c.start_at == timedelta(hours=1),
                        s.forecasts.c.published_at <= snapshot.decision_at,
                        s.forecasts.c.published_at >= snapshot.decision_at - timedelta(days=90),
                        s.settlements.c.settled_at <= snapshot.decision_at,
                    )
                )
                if eligible is None:
                    raise DomainError("FUTURE_EVIDENCE", "历史案例未在决策前验证或engine不一致")
            for ident in observation_ids:
                eligible = await connection.scalar(
                    sa.select(s.observations.c.id).where(
                        s.observations.c.id == ident,
                        s.observations.c.kind == "observation",
                        s.observations.c.verification_status == "VERIFIED",
                        s.observations.c.engine_id == engine_id,
                        s.observations.c.available_at <= snapshot.decision_at,
                        s.observations.c.available_at > snapshot.decision_at - timedelta(days=30),
                        s.observations.c.expires_at > snapshot.decision_at,
                    )
                )
                if eligible is None:
                    raise DomainError("FUTURE_EVIDENCE", "经验未验证、已失效或在决策之后产生")
            snapshot_id = await persist_snapshot(connection, snapshot)
            await connection.execute(
                insert(s.job_inputs).values(
                    job_id=attempt.job_id,
                    snapshot_id=snapshot_id,
                    engine_id=engine_id,
                    strategy_id=strategy_id,
                    case_ids=json_value(case_ids),
                    observation_ids=json_value(observation_ids),
                    stats=json_value(stats or {}),
                    context_text=context_text,
                )
            )
            await connection.execute(
                sa.update(s.jobs)
                .where(s.jobs.c.id == attempt.job_id)
                .values(stage="SNAPSHOT_FROZEN", updated_at=now)
            )
        attempt_event("snapshot_frozen", attempt, snapshot_id=str(snapshot_id))
        return snapshot_id

    async def start_analysis(self, attempt: Attempt):
        async with self.jobs.transaction(attempt) as connection:
            job, record, now = await self.jobs.guard(connection, attempt)
            _, snapshot = await self._input(connection, attempt)
            if job["first_model_started_at"] is None:
                if now > snapshot.anchor_at + timedelta(minutes=2):
                    raise DomainError("ANALYSIS_START_DEADLINE", "首个模型分析阶段开始过晚", 409)
                await connection.execute(
                    sa.update(s.jobs)
                    .where(s.jobs.c.id == attempt.job_id)
                    .values(first_model_started_at=now, updated_at=now)
                )
            if record["model_started_at"] is None:
                expires = min(now + timedelta(seconds=120), job["deadline_at"])
                await connection.execute(
                    sa.update(s.job_attempts)
                    .where(s.job_attempts.c.id == attempt.id)
                    .values(model_started_at=now, token_expires_at=expires)
                )
        attempt_event("model_analysis_started", attempt)

    async def _trends(self, connection, attempt, inputs, snapshot):
        reports = {}
        for timeframe in TRENDS:
            group = snapshot.group(timeframe)
            report = (
                (
                    await connection.execute(
                        sa.select(s.trend_analyses).where(
                            s.trend_analyses.c.source == group.bars[-1].source,
                            s.trend_analyses.c.inst_id == group.bars[-1].inst_id,
                            s.trend_analyses.c.timeframe == timeframe,
                            s.trend_analyses.c.last_close_boundary == group.bars[-1].close_boundary,
                            s.trend_analyses.c.input_content_hash == group.input_content_hash,
                            s.trend_analyses.c.engine_id == inputs["engine_id"],
                            s.trend_analyses.c.valid_before > snapshot.anchor_at,
                            sa.or_(
                                s.trend_analyses.c.available_at <= snapshot.decision_at,
                                s.trend_analyses.c.producer_job_id == attempt.job_id,
                            ),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if report:
                reports[timeframe] = dict(report)
            attempt_event(
                "trend_cache_hit" if report else "trend_cache_miss",
                attempt,
                timeframe=timeframe,
                input_hash=group.input_content_hash,
            )
        return reports

    async def context(self, attempt: Attempt) -> dict:
        async with self.jobs.transaction(attempt) as connection:
            inputs, snapshot = await self._input(connection, attempt)
            trends = await self._trends(connection, attempt, inputs, snapshot)
            bundle = (
                (
                    await connection.execute(
                        sa.select(s.context_bundles).where(
                            s.context_bundles.c.job_id == attempt.job_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            one_hour = (
                (
                    await connection.execute(
                        sa.select(s.timeframe_analyses).where(
                            s.timeframe_analyses.c.attempt_id == attempt.id
                        )
                    )
                )
                .mappings()
                .first()
            )
            return json_value(
                {
                    "attempt_id": attempt.id,
                    "snapshot": {
                        "id": snapshot.id,
                        "anchor_at": snapshot.anchor_at,
                        "decision_at": snapshot.decision_at,
                        "payload_hash": snapshot.payload_hash,
                        "feature_spec_version": snapshot.feature_spec_version,
                        "groups": [
                            {
                                "timeframe": group.timeframe,
                                "calculation_bar_ids": tuple(bar.id for bar in group.bars),
                                "model_bars": group.bars[-30:],
                                "features": group.features,
                                "observation_ids": group.observation_ids,
                                "input_content_hash": group.input_content_hash,
                                "last_close_boundary": group.bars[-1].close_boundary,
                            }
                            for group in snapshot.groups
                        ],
                    },
                    "engine_id": inputs["engine_id"],
                    "case_ids": inputs["case_ids"],
                    "observation_ids": inputs["observation_ids"],
                    "stats": inputs["stats"],
                    "context_text": inputs["context_text"],
                    "trend_reports": {
                        tf: {"id": r["id"], "analysis": r["payload"]} for tf, r in trends.items()
                    },
                    "missing_trends": [tf for tf in TRENDS if tf not in trends],
                    "bundle": dict(bundle) if bundle else None,
                    "one_hour_analysis": dict(one_hour) if one_hour else None,
                }
            )

    async def submit_trend(self, attempt: Attempt, report: TrendAnalysis, child_id: str) -> UUID:
        report = TrendAnalysis.model_validate(report)
        async with self.jobs.transaction(attempt) as connection:
            job, _, now = await self.jobs.guard(connection, attempt)
            if job["first_model_started_at"] is None:
                raise DomainError("ANALYSIS_NOT_STARTED", "未登记模型阶段开始时间", 409)
            frozen = await connection.scalar(
                sa.select(s.context_bundles.c.id).where(
                    s.context_bundles.c.job_id == attempt.job_id
                )
            )
            inputs, snapshot = await self._input(connection, attempt)
            if frozen:
                cached = (await self._trends(connection, attempt, inputs, snapshot)).get(
                    report.timeframe
                )
                if cached and cached["payload_hash"] == content_hash(report):
                    return cached["id"]
                raise DomainError("CONTEXT_FROZEN", "最终上下文冻结后不能新增趋势", 409)
            group = snapshot.group(report.timeframe)
            if report.last_close_boundary != group.bars[-1].close_boundary:
                raise DomainError("TREND_BOUNDARY", "趋势报告收盘边界不一致")
            allowed = {snapshot.id, *group.observation_ids}
            if not set(report.evidence_ids) <= allowed:
                raise DomainError("EVIDENCE_FORBIDDEN", "趋势只能引用该周期冻结行情与外部观测", 403)
            payload = json_value(report)
            values = dict(
                source=group.bars[-1].source,
                inst_id=group.bars[-1].inst_id,
                timeframe=report.timeframe,
                last_close_boundary=report.last_close_boundary,
                input_content_hash=group.input_content_hash,
                engine_id=inputs["engine_id"],
            )
            ident = await connection.scalar(
                insert(s.trend_analyses)
                .values(
                    **values,
                    input_snapshot_id=snapshot.id,
                    producer_job_id=attempt.job_id,
                    producer_attempt_id=attempt.id,
                    hermes_child_id=child_id,
                    trend_state=report.trend_state,
                    trend_strength=report.trend_strength,
                    notes={
                        "structure_notes": report.structure_notes,
                        "risk_notes": report.risk_notes,
                    },
                    evidence_ids=json_value(report.evidence_ids),
                    available_at=now,
                    valid_before=group.bars[-1].close_boundary
                    + timedelta(seconds=INTERVAL_SECONDS[report.timeframe]),
                    payload=payload,
                    payload_hash=content_hash(payload),
                )
                .on_conflict_do_nothing(
                    index_elements=[s.trend_analyses.c[name] for name in values]
                )
                .returning(s.trend_analyses.c.id)
            )
            if ident is not None:
                return ident
            existing = (
                (
                    await connection.execute(
                        sa.select(s.trend_analyses).where(
                            *(s.trend_analyses.c[name] == value for name, value in values.items())
                        )
                    )
                )
                .mappings()
                .one()
            )
            if existing["producer_attempt_id"] == attempt.id and existing[
                "payload_hash"
            ] != content_hash(payload):
                raise DomainError("TREND_ALREADY_SUBMITTED", "同一attempt趋势报告不可改写", 409)
            if (
                existing["producer_job_id"] != attempt.job_id
                and existing["available_at"] > snapshot.decision_at
            ):
                raise DomainError("FUTURE_TREND", "其他任务的趋势在本次决策后才产生")
            return existing["id"]

    async def freeze_bundle(self, attempt: Attempt) -> UUID:
        async with self.jobs.transaction(attempt) as connection:
            inputs, snapshot = await self._input(connection, attempt)
            existing = await connection.scalar(
                sa.select(s.context_bundles.c.id).where(
                    s.context_bundles.c.job_id == attempt.job_id
                )
            )
            if existing:
                await connection.execute(
                    sa.update(s.job_attempts)
                    .where(s.job_attempts.c.id == attempt.id)
                    .values(bundle_id=existing)
                )
                return existing
            trends = await self._trends(connection, attempt, inputs, snapshot)
            if set(trends) != set(TRENDS):
                raise DomainError("TRENDS_MISSING", "三份有效趋势齐备后才能冻结最终上下文", 409)
            evidence = {str(snapshot.id), *inputs["case_ids"], *inputs["observation_ids"]}
            for report in trends.values():
                evidence.update(report["evidence_ids"])
                evidence.update((str(report["id"]), str(report["input_snapshot_id"])))
            trend_ids = {tf: str(report["id"]) for tf, report in trends.items()}
            ident = uuid4()
            payload = {
                "job_id": str(attempt.job_id),
                "snapshot_id": str(snapshot.id),
                "evidence_ids": sorted(evidence),
                "observation_ids": inputs["observation_ids"],
                "trend_analysis_ids": trend_ids,
                "stats": inputs["stats"],
                "rendered_text": inputs["context_text"],
            }
            await connection.execute(
                insert(s.context_bundles).values(
                    id=ident, **payload, content_hash=content_hash(payload)
                )
            )
            await connection.execute(
                insert(s.bundle_trends),
                [
                    {"bundle_id": ident, "timeframe": tf, "trend_id": report["id"]}
                    for tf, report in trends.items()
                ],
            )
            await connection.execute(
                sa.update(s.job_attempts)
                .where(s.job_attempts.c.id == attempt.id)
                .values(bundle_id=ident)
            )
            await connection.execute(
                sa.update(s.jobs)
                .where(s.jobs.c.id == attempt.job_id)
                .values(stage="CONTEXT_FROZEN")
            )
        attempt_event("bundle_frozen", attempt, bundle_id=str(ident))
        return ident

    async def submit_one_hour(
        self, attempt: Attempt, report: OneHourAnalysis, child_id: str
    ) -> UUID:
        report = OneHourAnalysis.model_validate(report)
        async with self.jobs.transaction(attempt) as connection:
            job, record, _ = await self.jobs.guard(connection, attempt)
            bundle = (
                (
                    await connection.execute(
                        sa.select(s.context_bundles).where(
                            s.context_bundles.c.job_id == attempt.job_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if bundle is None or record["bundle_id"] != bundle["id"]:
                raise DomainError("CONTEXT_NOT_FROZEN", "1h报告必须绑定本attempt的最终上下文", 409)
            if job["first_model_started_at"] is None:
                raise DomainError("ANALYSIS_NOT_STARTED", "未登记模型阶段开始时间", 409)
            if not {str(i) for i in report.evidence_ids} <= set(bundle["evidence_ids"]):
                raise DomainError("EVIDENCE_FORBIDDEN", "1h报告引用了未授权证据", 403)
            digest = content_hash(report)
            ident = await connection.scalar(
                insert(s.timeframe_analyses)
                .values(
                    attempt_id=attempt.id,
                    bundle_id=bundle["id"],
                    timeframe="1h",
                    hermes_child_id=child_id,
                    p_up=report.p_up,
                    p_flat=report.p_flat,
                    p_down=report.p_down,
                    reason=report.reason,
                    risk_notes=json_value(report.risk_notes),
                    evidence_ids=json_value(report.evidence_ids),
                    schema_valid=True,
                    payload=json_value(report),
                    payload_hash=digest,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        s.timeframe_analyses.c.attempt_id,
                        s.timeframe_analyses.c.timeframe,
                    ]
                )
                .returning(s.timeframe_analyses.c.id)
            )
            if ident is None:
                existing = (
                    (
                        await connection.execute(
                            sa.select(s.timeframe_analyses).where(
                                s.timeframe_analyses.c.attempt_id == attempt.id
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                if existing["payload_hash"] != digest:
                    raise DomainError("ANALYSIS_ALREADY_SUBMITTED", "1h报告不可改写", 409)
                ident = existing["id"]
            await connection.execute(
                sa.update(s.jobs)
                .where(s.jobs.c.id == attempt.job_id)
                .values(stage="ONE_HOUR_ANALYZED")
            )
        attempt_event("one_hour_analysis_saved", attempt, child_id=child_id, result_hash=digest)
        return ident

    async def publish(self, attempt: Attempt, submission: ForecastSubmission) -> UUID:
        submission = ForecastSubmission.model_validate(submission)
        async with self.jobs.transaction(attempt) as connection:
            inputs, snapshot = await self._input(connection, attempt)
            existing = (
                (
                    await connection.execute(
                        sa.select(s.forecasts).where(s.forecasts.c.job_id == attempt.job_id)
                    )
                )
                .mappings()
                .first()
            )
            digest = content_hash(submission)
            if existing:
                if existing["submission_hash"] != digest:
                    raise DomainError("FORECAST_ALREADY_PUBLISHED", "正式预测已经发布不同内容", 409)
                return existing["id"]
            bundle = (
                (
                    await connection.execute(
                        sa.select(s.context_bundles).where(
                            s.context_bundles.c.job_id == attempt.job_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            analysis = (
                (
                    await connection.execute(
                        sa.select(s.timeframe_analyses).where(
                            s.timeframe_analyses.c.id == submission.one_hour_analysis_id,
                            s.timeframe_analyses.c.attempt_id == attempt.id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if (
                bundle is None
                or analysis is None
                or analysis["bundle_id"] != bundle["id"]
                or {k: str(v) for k, v in submission.trend_analysis_ids.items()}
                != bundle["trend_analysis_ids"]
            ):
                raise DomainError("ANALYSIS_MISMATCH", "报告不属于当前attempt或冻结bundle", 403)
            report = OneHourAnalysis.model_validate(analysis["payload"])
            require_same_probabilities(submission, report)
            if not {str(i) for i in submission.evidence_ids} <= set(bundle["evidence_ids"]):
                raise DomainError("EVIDENCE_FORBIDDEN", "正式预测引用了未授权证据", 403)
            _, record, now = await self.jobs.guard(connection, attempt)
            if record["bundle_id"] != bundle["id"]:
                raise DomainError("BUNDLE_MISMATCH", "当前任务令牌未绑定该上下文", 403)
            start, end = window(snapshot.anchor_at)
            probabilities = {key: getattr(report, key) for key in ("p_up", "p_flat", "p_down")}
            payload = {**submission.model_dump(), **probabilities}
            ident = uuid4()
            await connection.execute(
                insert(s.forecasts).values(
                    id=ident,
                    job_id=attempt.job_id,
                    symbol=self.config.market.symbol,
                    anchor_at=snapshot.anchor_at,
                    start_at=start,
                    end_at=end,
                    engine_id=inputs["engine_id"],
                    strategy_id=inputs["strategy_id"],
                    bundle_id=bundle["id"],
                    one_hour_analysis_id=submission.one_hour_analysis_id,
                    trend_analysis_ids=json_value(submission.trend_analysis_ids),
                    **probabilities,
                    label=report.label,
                    reason=submission.reason,
                    risk_notes=json_value(submission.risk_notes),
                    evidence_ids=json_value(submission.evidence_ids),
                    payload=json_value(payload),
                    payload_hash=content_hash(payload),
                    submission_payload=json_value(submission),
                    submission_hash=digest,
                    published_at=now,
                )
            )
            await connection.execute(
                insert(s.forecast_trends),
                [
                    {"forecast_id": ident, "timeframe": tf, "trend_id": trend_id}
                    for tf, trend_id in submission.trend_analysis_ids.items()
                ],
            )
            await connection.execute(
                insert(s.audit_events).values(
                    actor_type="research_attempt",
                    actor_id=str(attempt.id),
                    action="FORECAST_PUBLISHED",
                    object_type="forecast",
                    object_id=ident,
                    after_hash=content_hash(payload),
                    request_id=str(attempt.id),
                )
            )
            await self.jobs.create(
                connection,
                "SETTLE",
                f"settle:{ident}:initial",
                {"forecast_id": str(ident)},
                start + timedelta(minutes=1),
            )
            await connection.execute(
                sa.update(s.jobs)
                .where(s.jobs.c.id == attempt.job_id)
                .values(stage="PUBLISHED", result_id=ident, updated_at=now)
            )
            await connection.execute(
                sa.update(s.schedule_slots)
                .where(s.schedule_slots.c.job_id == attempt.job_id)
                .values(status="PUBLISHED")
            )
        attempt_event("forecast_published", attempt, forecast_id=str(ident), result_hash=digest)
        return ident
