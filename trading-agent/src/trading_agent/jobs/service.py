from __future__ import annotations

import hashlib
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from trading_agent.domain.errors import DomainError, StaleAttempt
from trading_agent.domain.serialization import content_hash, json_value
from trading_agent.runtime.logging import attempt_event
from trading_agent.runtime.ownership import RuntimeOwnership
from trading_agent.storage import schema as s
from trading_agent.storage.database import Database
from trading_agent.storage.market import content_lock


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Attempt:
    id: UUID
    job_id: UUID
    kind: str
    fence: int
    runtime_instance_id: UUID
    runtime_generation: int
    deadline_at: datetime | None
    stage: str
    payload: dict
    token: str = field(repr=False)


class JobService:
    def __init__(self, database: Database, ownership: RuntimeOwnership):
        self.database, self.ownership = database, ownership

    async def create(
        self,
        connection,
        kind: str,
        dedupe_key: str,
        payload: dict,
        next_run_at: datetime,
        deadline_at: datetime | None = None,
    ) -> UUID:
        record = json_value(payload)
        ident = await connection.scalar(
            insert(s.jobs)
            .values(
                kind=kind,
                dedupe_key=dedupe_key,
                payload=record,
                next_run_at=next_run_at,
                deadline_at=deadline_at,
            )
            .on_conflict_do_nothing(index_elements=[s.jobs.c.dedupe_key])
            .returning(s.jobs.c.id)
        )
        if ident is not None:
            return ident
        existing = (
            (await connection.execute(sa.select(s.jobs).where(s.jobs.c.dedupe_key == dedupe_key)))
            .mappings()
            .one()
        )
        if (
            existing["kind"] != kind
            or content_hash(existing["payload"]) != content_hash(record)
            or existing["deadline_at"] != deadline_at
        ):
            raise DomainError("JOB_CONFLICT", "相同任务键对应不同内容", 409)
        return existing["id"]

    async def claim(self, kinds: tuple[str, ...] | None = None) -> Attempt | None:
        async with self.database.transaction() as connection:
            await self.ownership.guard(connection)
            now = await self.database.now(connection)
            eligible = sa.or_(
                sa.and_(
                    s.jobs.c.state.in_(("QUEUED", "RETRY_WAIT", "AWAITING_DATA")),
                    s.jobs.c.next_run_at <= now,
                ),
                sa.and_(s.jobs.c.state == "RUNNING", s.jobs.c.lease_until <= now),
            )
            if kinds:
                eligible = sa.and_(eligible, s.jobs.c.kind.in_(kinds))
            eligible = sa.and_(
                eligible,
                sa.func.coalesce(s.jobs.c.payload["analysis_managed"].as_boolean(), False).is_(
                    False
                ),
            )
            job = (
                (
                    await connection.execute(
                        sa.select(s.jobs)
                        .where(eligible)
                        .order_by(
                            sa.case(
                                (s.jobs.c.kind.in_(("FORECAST", "PREDICT_EXECUTE")), 0),
                                (s.jobs.c.kind.in_(("PREDICT_VERIFY", "SETTLE")), 1),
                                (
                                    sa.and_(
                                        s.jobs.c.kind == "PREDICT_REVIEW",
                                        s.jobs.c.payload["requested_by"].as_string().is_not(None),
                                        s.jobs.c.payload["request_kind"]
                                        .as_string()
                                        .in_(("review", "explain")),
                                    ),
                                    2,
                                ),
                                (
                                    sa.and_(
                                        s.jobs.c.kind.in_(("PREDICT_TIMEFRAME", "PREDICT_REVIEW")),
                                        s.jobs.c.next_run_at <= now - timedelta(minutes=5),
                                    ),
                                    3,
                                ),
                                (s.jobs.c.kind == "PREDICT_PARENT", 3),
                                (s.jobs.c.kind == "PREDICT_TIMEFRAME", 4),
                                else_=5,
                            ),
                            s.jobs.c.next_run_at,
                        )
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if job is None:
                return None
            slot = None
            if getattr(self, "enforce_slots", False) and job["kind"] in {
                "PREDICT_PARENT",
                "PREDICT_TIMEFRAME",
                "PREDICT_REVIEW",
            }:
                await content_lock(connection, "model-slot-capacity")
                # Reclaiming a lease does not prove the previous sandbox/process
                # stopped. Wait for confirmed cleanup before restarting this job.
                previous_resource = await connection.scalar(
                    sa.select(s.model_slots.c.slot)
                    .join(s.job_attempts, s.job_attempts.c.id == s.model_slots.c.attempt_id)
                    .where(s.job_attempts.c.job_id == job["id"])
                    .limit(1)
                )
                if previous_resource is not None:
                    return None
                slot = await connection.scalar(
                    sa.select(s.model_slots.c.slot)
                    .where(s.model_slots.c.state == "FREE")
                    .order_by(s.model_slots.c.slot)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if slot is None:
                    return None
            now = await self.database.now(connection)
            await connection.execute(
                sa.update(s.job_attempts)
                .where(
                    s.job_attempts.c.job_id == job["id"],
                    s.job_attempts.c.status == "RUNNING",
                )
                .values(status="EXPIRED", token_revoked=True, ended_at=now)
            )
            if job["deadline_at"] is not None and job["deadline_at"] <= now:
                if job["payload"].get("analysis_round"):
                    # The recovery loop closes the durable round and schedules
                    # bounded error recovery; legacy SKIPPED would strand scope.
                    return None
                await connection.execute(
                    sa.update(s.jobs)
                    .where(s.jobs.c.id == job["id"])
                    .values(
                        state="SKIPPED",
                        last_error={"code": "FORECAST_DEADLINE"},
                        lease_until=None,
                        lease_owner=None,
                        updated_at=now,
                    )
                )
                await connection.execute(
                    sa.update(s.schedule_slots)
                    .where(s.schedule_slots.c.job_id == job["id"])
                    .values(status="SKIPPED", reason="FORECAST_DEADLINE")
                )
                return None
            if job["kind"] == "FORECAST" and job["attempts"] >= 2:
                await connection.execute(
                    sa.update(s.jobs)
                    .where(s.jobs.c.id == job["id"])
                    .values(
                        state="FAILED",
                        last_error={"code": "ATTEMPTS_EXHAUSTED"},
                        lease_until=None,
                        lease_owner=None,
                        updated_at=now,
                    )
                )
                await connection.execute(
                    sa.update(s.schedule_slots)
                    .where(s.schedule_slots.c.job_id == job["id"])
                    .values(status="FAILED", reason="ATTEMPTS_EXHAUSTED")
                )
                return None
            ident, fence, token = uuid4(), job["fence"] + 1, secrets.token_urlsafe(32)
            expiration = now + timedelta(
                seconds=1200 if job["payload"].get("analysis_round") else 150
            )
            if job["deadline_at"] is not None:
                expiration = min(expiration, job["deadline_at"])
            await connection.execute(
                sa.update(s.jobs)
                .where(s.jobs.c.id == job["id"])
                .values(
                    state="RUNNING",
                    attempts=job["attempts"] + 1,
                    fence=fence,
                    lease_owner=self.ownership.instance_id,
                    lease_until=now + timedelta(seconds=60),
                    runtime_generation=self.ownership.generation,
                    updated_at=now,
                )
            )
            await connection.execute(
                insert(s.job_attempts).values(
                    id=ident,
                    job_id=job["id"],
                    fence=fence,
                    status="RUNNING",
                    runtime_instance_id=self.ownership.instance_id,
                    runtime_generation=self.ownership.generation,
                    token_hash=token_hash(token),
                    token_expires_at=expiration,
                    started_at=now,
                )
            )
            if slot is not None:
                await connection.execute(
                    sa.update(s.model_slots)
                    .where(s.model_slots.c.slot == slot)
                    .values(attempt_id=ident, state="OCCUPIED", updated_at=now)
                )
            claimed = Attempt(
                ident,
                job["id"],
                job["kind"],
                fence,
                self.ownership.instance_id,
                self.ownership.generation,
                job["deadline_at"],
                job["stage"],
                job["payload"],
                token,
            )
        attempt_event("job_claimed", claimed)
        return claimed

    async def guard(
        self, connection, attempt: Attempt, *, check_deadline=True, check_token_expiry=True
    ):
        if attempt.payload.get("analysis_managed"):
            coordinator = await self.attempt_record(
                connection, UUID(attempt.payload["coordinator_attempt_id"])
            )
            await self.guard(
                connection,
                coordinator,
                check_deadline=check_deadline,
                check_token_expiry=check_token_expiry,
            )
        await self.ownership.guard(connection)
        job = (
            (
                await connection.execute(
                    sa.select(s.jobs).where(s.jobs.c.id == attempt.job_id).with_for_update()
                )
            )
            .mappings()
            .first()
        )
        record = (
            (
                await connection.execute(
                    sa.select(s.job_attempts)
                    .where(s.job_attempts.c.id == attempt.id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        now = await self.database.now(connection)  # Read after every potentially waiting lock.
        if (
            job is None
            or record is None
            or job["state"] != "RUNNING"
            or job["lease_owner"] != attempt.runtime_instance_id
            or job["runtime_generation"] != attempt.runtime_generation
            or job["fence"] != attempt.fence
            or record["fence"] != attempt.fence
            or record["status"] != "RUNNING"
            or record["token_revoked"]
            or job["lease_until"] is None
            or job["lease_until"] <= now
        ):
            attempt_event("stale_attempt_rejected", attempt, error_code="STALE_ATTEMPT")
            raise StaleAttempt()
        if check_deadline and job["deadline_at"] is not None and job["deadline_at"] < now:
            raise DomainError("FORECAST_DEADLINE", "结果超过任务发布截止时间", 409)
        if check_token_expiry and record["token_expires_at"] < now:
            raise StaleAttempt()
        return job, record, now

    async def attempt_record(self, connection, ident):
        row = (
            (
                await connection.execute(
                    sa.select(
                        s.job_attempts,
                        s.jobs.c.kind,
                        s.jobs.c.payload,
                        s.jobs.c.deadline_at,
                        s.jobs.c.stage,
                    )
                    .join(s.jobs, s.jobs.c.id == s.job_attempts.c.job_id)
                    .where(s.job_attempts.c.id == ident)
                )
            )
            .mappings()
            .one()
        )
        return Attempt(
            row["id"],
            row["job_id"],
            row["kind"],
            row["fence"],
            row["runtime_instance_id"],
            row["runtime_generation"],
            row["deadline_at"],
            row["stage"],
            row["payload"],
            "",
        )

    async def release_model_slot(self, attempt, *, cleaned):
        async with self.database.transaction() as c:
            await self.ownership.guard(c)
            await c.execute(
                sa.update(s.model_slots)
                .where(s.model_slots.c.attempt_id == attempt.id)
                .values(
                    state="FREE" if cleaned else "QUARANTINED",
                    attempt_id=None if cleaned else attempt.id,
                    updated_at=await self.database.now(c),
                )
            )

    async def recover_model_slots(self):
        async with self.database.transaction() as c:
            await self.ownership.guard(c)
            rows = (
                (
                    await c.execute(
                        sa.select(
                            s.model_slots,
                            s.job_attempts.c.status,
                            s.job_attempts.c.process_id,
                            s.job_attempts.c.sandbox_ref,
                        )
                        .join(s.job_attempts, s.job_attempts.c.id == s.model_slots.c.attempt_id)
                        .where(s.job_attempts.c.status != "RUNNING")
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                sandbox = row["sandbox_ref"] or {}
                if row["process_id"] is None and sandbox.get("status") in {
                    None,
                    "CLEANED",
                    "DESTROYED",
                }:
                    await c.execute(
                        sa.update(s.model_slots)
                        .where(s.model_slots.c.slot == row["slot"])
                        .values(
                            state="FREE", attempt_id=None, updated_at=await self.database.now(c)
                        )
                    )

    @asynccontextmanager
    async def transaction(self, attempt: Attempt, *, check_deadline=True, check_token_expiry=True):
        async with self.database.transaction() as connection:
            await self.guard(
                connection,
                attempt,
                check_deadline=check_deadline,
                check_token_expiry=check_token_expiry,
            )
            yield connection

    async def record_session(self, attempt: Attempt, session: str):
        if not 1 <= len(session) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in session):
            raise DomainError("SESSION_ID_INVALID", "Hermes会话ID格式无效", 422)
        async with self.transaction(attempt) as connection:
            record = (
                (
                    await connection.execute(
                        sa.select(s.job_attempts).where(s.job_attempts.c.id == attempt.id)
                    )
                )
                .mappings()
                .one()
            )
            usage = dict(record["usage_json"] or {})
            sessions = list(usage.get("parent_sessions", []))
            if session not in sessions:
                if len(sessions) >= (12 if attempt.payload.get("analysis_round") else 2):
                    raise DomainError("SESSION_LIMIT", "一个attempt最多首轮与修复两个父会话", 409)
                sessions.append(session)
                usage["parent_sessions"] = sessions
                await connection.execute(
                    sa.update(s.job_attempts)
                    .where(s.job_attempts.c.id == attempt.id)
                    .values(
                        agent_session_id=record["agent_session_id"] or session, usage_json=usage
                    )
                )

    async def record_submission_rejection(self, attempt: Attempt, code: str, request_id: str):
        """Trusted API diagnostics only; never persist rejected bodies or model stdout."""
        async with self.transaction(attempt) as connection:
            await connection.execute(
                insert(s.audit_events).values(
                    actor_type="attempt",
                    actor_id=str(attempt.id),
                    action=f"PREDICTION_REJECTED:{code}",
                    object_type="job",
                    object_id=attempt.job_id,
                    request_id=request_id,
                )
            )

    async def last_submission_error(self, attempt: Attempt):
        async with self.database.engine.connect() as connection:
            action = await connection.scalar(
                sa.select(s.audit_events.c.action)
                .where(
                    s.audit_events.c.actor_type == "attempt",
                    s.audit_events.c.actor_id == str(attempt.id),
                    s.audit_events.c.object_id == attempt.job_id,
                    s.audit_events.c.action.startswith("PREDICTION_REJECTED:"),
                )
                .order_by(s.audit_events.c.created_at.desc(), s.audit_events.c.id.desc())
                .limit(1)
            )
        return action.partition(":")[2] if action else None

    async def record_model_usage(self, attempt: Attempt, runs):
        """Native telemetry is bounded and never enters forecasts, memory or scoring."""
        async with self.transaction(attempt) as connection:
            record = (
                (
                    await connection.execute(
                        sa.select(s.job_attempts).where(s.job_attempts.c.id == attempt.id)
                    )
                )
                .mappings()
                .one()
            )
            usage = dict(record["usage_json"] or {})
            models = dict(usage.get("model_runs", {}))
            parents = list(usage.get("parent_sessions", []))
            for run in runs:
                value = json_value(run)
                previous = models.get(run.session_id)
                if previous and previous != value:
                    if run.role != "parent" or previous["role"] != "parent":
                        raise DomainError("USAGE_CONFLICT", "子任务usage已保存，不能改写", 409)
                    for field in ("api_calls", "input_tokens", "output_tokens"):
                        old, new = previous.get(field), value.get(field)
                        if old is not None and (new is None or new < old):
                            raise DomainError("USAGE_CONFLICT", "父会话累计usage不能减少", 409)
                if run.role == "parent" and run.session_id not in parents:
                    parents.append(run.session_id)
                models[run.session_id] = value
            children = [run for run in models.values() if run["role"] != "parent"]
            coordinated = attempt.payload.get("analysis_round")
            if (
                len(parents) > (12 if coordinated else 2)
                or len(children) > (12 if coordinated else 4)
                or (not coordinated and len({run["role"] for run in children}) != len(children))
            ):
                raise DomainError("USAGE_LIMIT", "usage超过固定父子任务预算", 409)
            if attempt.kind not in {"FORECAST", "PREDICT_TIMEFRAME", "PREDICT_PARENT"} and children:
                raise DomainError("FORBIDDEN", "复盘任务没有周期子分析", 403)
            usage.update(
                model_runs=models, parent_sessions=parents, usage_source="native_hermes_v1"
            )
            await connection.execute(
                sa.update(s.job_attempts)
                .where(s.job_attempts.c.id == attempt.id)
                .values(
                    usage_json=usage,
                    agent_session_id=record["agent_session_id"]
                    or (parents[0] if parents else None),
                )
            )
        for run in runs:
            attempt_event(
                "model_usage_recorded",
                attempt,
                agent_session_id=run.session_id,
                stage=run.role,
                delivery_status=run.status,
                duration_ms=run.duration_seconds * 1000
                if run.duration_seconds is not None
                else None,
                api_calls=run.api_calls,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
            )

    async def renew(self, attempt: Attempt) -> bool:
        try:
            async with self.transaction(attempt) as connection:
                now = await self.database.now(connection)
                await connection.execute(
                    sa.update(s.jobs)
                    .where(s.jobs.c.id == attempt.job_id)
                    .values(lease_until=now + timedelta(seconds=60), updated_at=now)
                )
            return True
        except DomainError:
            return False

    async def authenticate(self, token: str, action: str) -> Attempt:
        if len(token) > 256:
            raise DomainError("UNAUTHENTICATED", "任务令牌无效", 401)
        async with self.database.transaction() as connection:
            record = (
                (
                    await connection.execute(
                        sa.select(s.job_attempts).where(
                            s.job_attempts.c.token_hash == token_hash(token)
                        )
                    )
                )
                .mappings()
                .first()
            )
            if record is None:
                raise DomainError("UNAUTHENTICATED", "任务令牌无效", 401)
            job = (
                (await connection.execute(sa.select(s.jobs).where(s.jobs.c.id == record["job_id"])))
                .mappings()
                .one()
            )
            attempt = Attempt(
                record["id"],
                job["id"],
                job["kind"],
                record["fence"],
                record["runtime_instance_id"],
                record["runtime_generation"],
                job["deadline_at"],
                job["stage"],
                job["payload"],
                token,
            )
            _, record, now = await self.guard(connection, attempt)
            if record["token_expires_at"] < now:
                raise StaleAttempt()
            allowed = {
                "FORECAST": {"context", "timeframes", "forecast", "usage"},
                "EXPLAIN": {"context", "review", "usage"},
                "REVIEW_DAILY": {"context", "review", "usage"},
                "PREDICT_TIMEFRAME": {
                    "prediction_context",
                    "prediction_submit",
                    "knowledge",
                    "usage",
                },
                "PREDICT_PARENT": {
                    "prediction_context",
                    "decision_submit",
                    "knowledge",
                    "usage",
                    "analysis",
                },
                "PREDICT_REVIEW": {"prediction_context", "knowledge_review", "knowledge", "usage"},
            }
            if action not in allowed.get(job["kind"], set()):
                raise DomainError("FORBIDDEN", "该任务无权执行此操作", 403)
            return attempt

    async def finish(
        self,
        attempt: Attempt,
        *,
        state="SUCCEEDED",
        result_id=None,
        error: dict | None = None,
        next_run_at: datetime | None = None,
    ):
        if state not in {"SUCCEEDED", "FAILED", "SKIPPED", "RETRY_WAIT", "AWAITING_DATA"}:
            raise ValueError("Unknown job state")
        async with self.transaction(
            attempt, check_deadline=False, check_token_expiry=False
        ) as connection:
            await self.finish_in_transaction(
                connection,
                attempt,
                state=state,
                result_id=result_id,
                error=error,
                next_run_at=next_run_at,
            )
        attempt_event(
            "job_finished",
            attempt,
            delivery_status=state,
            error_code=error.get("code") if error else None,
        )

    async def finish_in_transaction(
        self,
        connection,
        attempt: Attempt,
        *,
        state="SUCCEEDED",
        result_id=None,
        error=None,
        next_run_at=None,
        stage=None,
    ):
        """Finalize a job atomically with its facts; caller owns the guarded transaction."""
        if state not in {"SUCCEEDED", "FAILED", "SKIPPED", "RETRY_WAIT", "AWAITING_DATA"}:
            raise ValueError("Unknown job state")
        await self.guard(connection, attempt, check_deadline=False, check_token_expiry=False)
        now = await self.database.now(connection)
        values = dict(
            state=state,
            result_id=result_id,
            last_error=json_value(error),
            updated_at=now,
            lease_until=None,
            lease_owner=None,
            next_run_at=next_run_at or now,
        )
        if stage is not None:
            values["stage"] = stage
        await connection.execute(
            sa.update(s.jobs).where(s.jobs.c.id == attempt.job_id).values(**values)
        )
        await connection.execute(
            sa.update(s.job_attempts)
            .where(s.job_attempts.c.id == attempt.id)
            .values(
                status="SUCCEEDED" if state == "SUCCEEDED" else "FAILED",
                token_revoked=True,
                ended_at=now,
                error_code=error.get("code") if error else None,
            )
        )
        if attempt.kind == "FORECAST" and state in {"FAILED", "SKIPPED"}:
            await connection.execute(
                sa.update(s.schedule_slots)
                .where(s.schedule_slots.c.job_id == attempt.job_id)
                .values(status=state, reason=error.get("code") if error else state)
            )
