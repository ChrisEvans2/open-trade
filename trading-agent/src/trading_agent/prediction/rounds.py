"""Durable parent-coordinated analysis. External collection never holds row locks."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert

from trading_agent.domain.errors import DomainError
from trading_agent.domain.time import boundary
from trading_agent.jobs.service import Attempt, token_hash
from trading_agent.prediction.evaluation import condition_matches
from trading_agent.prediction.service import instrument, scope_filter
from trading_agent.prediction.submissions import bind_decision, bind_prediction, model_context
from trading_agent.storage import schema as s
from trading_agent.storage.market import persist_bar

ROLES = ("1h", "4h", "6h", "12h")
RECOVERY_DELAYS = (60, 180, 600)


def coordinated(attempt):
    return bool(attempt.payload.get("analysis_round")) and not attempt.payload.get(
        "analysis_managed"
    )


class AnalysisRounds:
    def __init__(self, predictions, market=None):
        self.predictions = predictions
        self.jobs = predictions.jobs
        self.database = predictions.database
        self.config = predictions.config
        self.market = market
        predictions.rounds = self
        self.jobs.rounds = self
        self.jobs.enforce_slots = True

    async def scope(self, c, engine, symbol, now):
        await self.predictions._lock_symbol(c, engine, symbol)
        await c.execute(
            insert(s.analysis_scopes)
            .values(engine_id=engine, symbol=symbol, parent_available=True, updated_at=now)
            .on_conflict_do_nothing()
        )
        return (
            (
                await c.execute(
                    sa.select(s.analysis_scopes)
                    .where(scope_filter(s.analysis_scopes, engine, symbol))
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )

    async def event(self, c, engine, symbol, roles, kind, source, now):
        row = await self.scope(c, engine, symbol, now)
        key = f"{engine}:{symbol}:{source}"
        inserted = await c.scalar(
            insert(s.analysis_events)
            .values(
                engine_id=engine,
                symbol=symbol,
                source_key=key,
                kind=kind,
                body={"roles": sorted(set(roles))},
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[s.analysis_events.c.source_key])
            .returning(s.analysis_events.c.id)
        )
        if not inserted:
            return row["active_round_id"]
        values = {
            "pending_roles": sorted(set(row["pending_roles"]) | set(roles)),
            "updated_at": now,
        }
        if roles or kind == "PARENT_ENDED":
            values["parent_available"] = False
        if row["window_until"] is None or (row["window_until"] <= now and row["state"] != "WINDOW"):
            values["window_until"] = now + timedelta(seconds=30)
        if row["active_round_id"] is None and row["state"] != "ERROR":
            values.update(state="WINDOW", resume_at=None)
            if kind != "BOOTSTRAP":
                values.update(recoveries=0, error_group=None)
        await c.execute(
            sa.update(s.analysis_scopes)
            .where(scope_filter(s.analysis_scopes, engine, symbol))
            .values(**values)
        )
        return inserted

    async def missing(self, c, engine, symbol, now):
        rows = (
            (
                await c.execute(
                    sa.select(
                        s.prediction_heads.c.timeframe,
                        s.prediction_facts.c.expires_at,
                        s.prediction_states.c.body,
                        s.prediction_states.c.data_quality,
                    )
                    .join(
                        s.prediction_facts,
                        s.prediction_facts.c.id == s.prediction_heads.c.prediction_id,
                    )
                    .join(
                        s.prediction_states,
                        s.prediction_states.c.prediction_id == s.prediction_facts.c.id,
                    )
                    .where(scope_filter(s.prediction_heads, engine, symbol, role="TIMEFRAME"))
                )
            )
            .mappings()
            .all()
        )
        existing = {r["timeframe"]: r for r in rows}
        needed, pending = [], []
        for role in ROLES:
            r = existing.get(role)
            if r is None or r["expires_at"] <= now or r["body"]["lifecycle"] != "ACTIVE":
                needed.append(role)
            elif r["data_quality"] != "OK" or datetime.fromisoformat(
                r["body"]["verified_until"]
            ) < boundary(now, "1m"):
                pending.append(role)
        return needed, pending

    async def tick(self, c, engine, symbol, now):
        row = await self.scope(c, engine, symbol, now)
        if row["state"] == "IDLE":
            needed, _ = await self.missing(c, engine, symbol, now)
            parent = await c.scalar(
                sa.select(s.prediction_heads.c.prediction_id).where(
                    scope_filter(s.prediction_heads, engine, symbol, "1h", "PARENT")
                )
            )
            if needed or parent is None:
                await self.event(c, engine, symbol, needed, "BOOTSTRAP", "bootstrap", now)
                row = await self.scope(c, engine, symbol, now)
        if row["active_round_id"] is not None:
            return None
        recovery = (
            row["state"] == "ERROR" and row["resume_at"] is not None and row["resume_at"] <= now
        )
        ready = row["state"] == "WINDOW" and row["window_until"] <= now
        if not (ready or recovery):
            return None
        index = row["recoveries"] + 1 if recovery else 0
        ident = uuid4()
        job = await self.jobs.create(
            c,
            "PREDICT_PARENT",
            f"analysis-round:{ident}",
            {
                "engine_id": str(engine),
                "symbol": symbol,
                "timeframe": "1h",
                "analysis_round": str(ident),
            },
            now,
        )
        await c.execute(
            sa.insert(s.analysis_rounds).values(
                id=ident,
                job_id=job,
                engine_id=engine,
                symbol=symbol,
                state="QUEUED",
                recovery_index=index,
                created_at=now,
            )
        )
        await c.execute(
            sa.update(s.analysis_scopes)
            .where(scope_filter(s.analysis_scopes, engine, symbol))
            .values(
                active_round_id=ident,
                state="ACTIVE",
                recoveries=index,
                resume_at=None,
                updated_at=now,
            )
        )
        return job

    async def flush(self, engine):
        async with self.database.transaction() as c:
            await self.jobs.ownership.guard(c)
            if await c.scalar(
                sa.select(s.runtime_control.c.paused).where(s.runtime_control.c.id == 1)
            ):
                return
            now = await self.database.now(c)
            for symbol in self.config.prediction.symbols:
                await self.tick(c, engine, symbol, now)

    async def guard(self, c, attempt, *, deadline=True):
        await self.jobs.guard(c, attempt, check_deadline=deadline, check_token_expiry=deadline)
        ident = UUID(attempt.payload["analysis_round"])
        r = (
            (
                await c.execute(
                    sa.select(s.analysis_rounds)
                    .where(s.analysis_rounds.c.id == ident)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if (
            r["job_id"] != attempt.job_id
            or str(r["engine_id"]) != attempt.payload["engine_id"]
            or r["symbol"] != attempt.payload["symbol"]
        ):
            raise DomainError("ANALYSIS_SCOPE", "轮次不属于本次任务", 403)
        now = await self.database.now(c)
        if deadline and r["state"] not in {"QUEUED", "RUNNING"}:
            raise DomainError("ANALYSIS_FINISHED", "轮次已经结束", 409)
        if deadline and r["deadline_at"] and now >= r["deadline_at"]:
            raise DomainError("ANALYSIS_DEADLINE", "分析轮次总时限已用尽", 409)
        return r, now

    async def start(self, attempt):
        async with self.jobs.transaction(attempt) as c:
            r, now = await self.guard(c, attempt)
            if r["started_at"] is None:
                deadline = now + timedelta(seconds=1200)
                await c.execute(
                    sa.update(s.analysis_rounds)
                    .where(s.analysis_rounds.c.id == r["id"])
                    .values(started_at=now, deadline_at=deadline, state="RUNNING")
                )
                await c.execute(
                    sa.update(s.jobs)
                    .where(s.jobs.c.id == attempt.job_id)
                    .values(deadline_at=deadline)
                )
                await c.execute(
                    sa.update(s.job_attempts)
                    .where(s.job_attempts.c.id == attempt.id)
                    .values(token_expires_at=deadline)
                )
            else:
                deadline = r["deadline_at"]
                await c.execute(
                    sa.update(s.job_attempts)
                    .where(s.job_attempts.c.id == attempt.id)
                    .values(token_expires_at=deadline)
                )
            # Fence unfinished runs from a crashed coordinator; their inputs are retained.
            runs = (
                (
                    await c.execute(
                        sa.select(s.analysis_runs).where(
                            s.analysis_runs.c.round_id == r["id"],
                            s.analysis_runs.c.coordinator_attempt_id != attempt.id,
                            s.analysis_runs.c.state.in_(("PREPARING", "RUNNING", "SUBMITTING")),
                        )
                    )
                )
                .mappings()
                .all()
            )
            for run in runs:
                await self._end_unowned(c, run, now, "COORDINATOR_RESTARTED")
            return replace(attempt, deadline_at=deadline)

    async def _end_unowned(self, c, run, now, code):
        await c.execute(
            sa.update(s.analysis_runs)
            .where(s.analysis_runs.c.id == run["id"])
            .values(state="FAILED", error_code=code, finished_at=now)
        )
        await c.execute(
            sa.update(s.jobs)
            .where(s.jobs.c.id == run["job_id"])
            .values(
                state="FAILED",
                last_error={"code": code},
                lease_until=None,
                lease_owner=None,
                updated_at=now,
            )
        )
        await c.execute(
            sa.update(s.job_attempts)
            .where(s.job_attempts.c.id == run["attempt_id"])
            .values(status="FAILED", token_revoked=True, error_code=code, ended_at=now)
        )

    async def result(self, attempt):
        async with self.database.engine.connect() as c:
            return await c.scalar(
                sa.select(s.analysis_rounds.c.parent_result_id).where(
                    s.analysis_rounds.c.job_id == attempt.job_id
                )
            )

    async def state(self, attempt):
        async with self.jobs.transaction(attempt) as c:
            r, now = await self.guard(c, attempt)
            scope = await self.scope(c, r["engine_id"], r["symbol"], now)
            needed, pending = await self.missing(c, r["engine_id"], r["symbol"], now)
            needed = sorted(set(needed) | set(scope["pending_roles"]))
            runs = (
                (
                    await c.execute(
                        sa.select(s.analysis_runs)
                        .where(s.analysis_runs.c.round_id == r["id"])
                        .order_by(s.analysis_runs.c.started_at)
                    )
                )
                .mappings()
                .all()
            )
            wait = (
                max(0, (scope["window_until"] - now).total_seconds())
                if needed and scope["window_until"]
                else 0
            )
            return {
                "model_contract": 4,
                "role": "COORDINATOR",
                "symbol": r["symbol"],
                "needed": needed,
                "awaiting_data": pending,
                "wait_seconds": min(wait, 30),
                "remaining_seconds": max(0, (r["deadline_at"] - now).total_seconds()),
                "counts": r["counts"],
                "limit": 1 if r["recovery_index"] else 3,
                "runs": [
                    {"role": x["role"], "state": x["state"], "error_code": x["error_code"]}
                    for x in runs
                ],
            }

    def _stage_attempt(self, row, main):
        payload = {
            "engine_id": main.payload["engine_id"],
            "symbol": main.payload["symbol"],
            "timeframe": row["role"] if row["role"] != "parent" else "1h",
            "analysis_managed": True,
            "coordinator_attempt_id": str(main.id),
            "analysis_round": main.payload["analysis_round"],
        }
        return Attempt(
            row["attempt_id"],
            row["job_id"],
            "PREDICT_PARENT" if row["role"] == "parent" else "PREDICT_TIMEFRAME",
            1,
            main.runtime_instance_id,
            main.runtime_generation,
            row["deadline_at"],
            "ANALYSIS",
            payload,
            "",
        )

    async def resolve(self, main, run_id):
        try:
            ident = UUID(str(run_id))
        except (ValueError, TypeError, AttributeError):
            raise DomainError("ANALYSIS_SCOPE", "无效的角色绑定", 403) from None
        async with self.jobs.transaction(main) as c:
            r, now = await self.guard(c, main)
            row = (
                (await c.execute(sa.select(s.analysis_runs).where(s.analysis_runs.c.id == ident)))
                .mappings()
                .first()
            )
            if (
                row is None
                or row["round_id"] != r["id"]
                or row["coordinator_attempt_id"] != main.id
            ):
                raise DomainError("ANALYSIS_SCOPE", "角色不属于本次授权轮次", 403)
            if (
                row["state"] not in {"RUNNING", "SUBMITTING"}
                or row["execution_deadline"] is None
                or now
                >= (row["execution_deadline"] if row["state"] == "RUNNING" else row["deadline_at"])
            ):
                raise DomainError("ROLE_EXPIRED", "角色已结束或超过执行期限", 409)
            return self._stage_attempt(row, main)

    async def complete_execution(self, main, run_id):
        # Validate and transition under the same coordinator/run locks. A prior
        # resolve is insufficient when cancellation or the clock advances.
        async with self.jobs.transaction(main) as c:
            r, now = await self.guard(c, main)
            row = (
                (
                    await c.execute(
                        sa.select(s.analysis_runs)
                        .where(s.analysis_runs.c.id == UUID(str(run_id)))
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if not row or row["round_id"] != r["id"] or row["coordinator_attempt_id"] != main.id:
                raise DomainError("ANALYSIS_SCOPE", "角色不属于本次授权轮次", 403)
            if row["state"] not in {"RUNNING", "SUBMITTING"} or now >= (
                row["execution_deadline"] if row["state"] == "RUNNING" else row["deadline_at"]
            ):
                raise DomainError("ROLE_EXPIRED", "角色已结束或超过执行期限", 409)
            await c.execute(
                sa.update(s.analysis_runs)
                .where(s.analysis_runs.c.id == row["id"])
                .values(
                    state="SUBMITTING", execution_finished_at=row["execution_finished_at"] or now
                )
            )

    async def phase_remaining(self, main):
        async with self.database.engine.connect() as c:
            now = await self.database.now(c)
            # A parent cannot stay alive with unconfirmed child cancellation.
            # Five seconds is cleanup grace, never additional publication time.
            unsafe = await c.scalar(
                sa.select(s.analysis_runs.c.id)
                .join(s.analysis_rounds, s.analysis_rounds.c.id == s.analysis_runs.c.round_id)
                .where(
                    s.analysis_runs.c.coordinator_attempt_id == main.id,
                    s.analysis_runs.c.launch_index
                    == sa.func.coalesce(s.analysis_rounds.c.counts["coordinator"].as_integer(), 0),
                    sa.or_(
                        s.analysis_runs.c.error_code.in_(
                            ("CHILD_CANCELLATION_PENDING", "CHILD_BATCH_INTERRUPTED")
                        ),
                        sa.and_(
                            s.analysis_runs.c.role != "parent",
                            s.analysis_runs.c.state == "RUNNING",
                            s.analysis_runs.c.execution_deadline <= now - timedelta(seconds=5),
                        ),
                        sa.and_(
                            s.analysis_runs.c.role != "parent",
                            s.analysis_runs.c.state.in_(("PREPARING", "SUBMITTING")),
                            s.analysis_runs.c.deadline_at <= now,
                        ),
                    ),
                )
                .limit(1)
            )
            if unsafe:
                return 0
            row = (
                (
                    await c.execute(
                        sa.select(s.analysis_runs)
                        .join(
                            s.analysis_rounds, s.analysis_rounds.c.id == s.analysis_runs.c.round_id
                        )
                        .where(
                            s.analysis_runs.c.launch_index
                            == sa.func.coalesce(
                                s.analysis_rounds.c.counts["coordinator"].as_integer(), 0
                            ),
                            s.analysis_runs.c.coordinator_attempt_id == main.id,
                            s.analysis_runs.c.role == "parent",
                        )
                        .order_by(s.analysis_runs.c.number.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            now = await self.database.now(c)
            if row is not None and row["state"] == "FAILED":
                return 0
            return (
                None
                if row is None or row["state"] == "PUBLISHED"
                else (
                    (row["execution_deadline"] if row["state"] == "RUNNING" else row["deadline_at"])
                    - now
                ).total_seconds()
            )

    async def interrupt_runs(self, main, code):
        async with self.jobs.transaction(main) as c:
            r, now = await self.guard(c, main)
            rows = (
                (
                    await c.execute(
                        sa.select(s.analysis_runs).where(
                            s.analysis_runs.c.round_id == r["id"],
                            s.analysis_runs.c.state.in_(("PREPARING", "RUNNING", "SUBMITTING")),
                        )
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                await self._end_unowned(c, row, now, code)

    async def launch(self, main):
        async with self.jobs.transaction(main) as c:
            r, _ = await self.guard(c, main)
            counts = dict(r["counts"])
            counts["coordinator"] = counts.get("coordinator", 0) + 1
            if counts["coordinator"] > (1 if r["recovery_index"] else 3):
                raise DomainError("COORDINATOR_RETRIES_EXHAUSTED", "父会话恢复次数已用尽", 409)
            await c.execute(
                sa.update(s.analysis_rounds)
                .where(s.analysis_rounds.c.id == r["id"])
                .values(counts=counts)
            )

    async def refresh(self, main):
        # Reuse is conditional on newly closed minute data, not merely on an
        # old OK flag. Only the deterministic verifier evaluates these facts.
        async with self.jobs.transaction(main) as c:
            r, now = await self.guard(c, main)
            rows = (
                (
                    await c.execute(
                        sa.select(
                            s.prediction_states.c.body,
                            s.prediction_states.c.data_quality,
                            s.prediction_facts.c.expires_at,
                        )
                        .join(
                            s.prediction_facts,
                            s.prediction_facts.c.id == s.prediction_states.c.prediction_id,
                        )
                        .join(
                            s.prediction_heads,
                            s.prediction_heads.c.prediction_id == s.prediction_facts.c.id,
                        )
                        .where(scope_filter(s.prediction_heads, r["engine_id"], r["symbol"]))
                    )
                )
                .mappings()
                .all()
            )
            stale = any(
                x["body"]["lifecycle"] == "ACTIVE"
                and (
                    x["expires_at"] <= now
                    or x["data_quality"] != "OK"
                    or datetime.fromisoformat(x["body"]["verified_until"]) < boundary(now, "1m")
                )
                for x in rows
            )
        if stale:
            async with asyncio.timeout(20):
                for index in range(2):
                    try:
                        await self.market.verify(main, coordinator=True)
                        break
                    except DomainError as exc:
                        if exc.code != "CURSOR_CHANGED" or index:
                            raise

    async def begin(self, main, role):
        if role not in (*ROLES, "parent"):
            raise DomainError("ANALYSIS_ROLE", "非法分析角色", 422)
        await self.refresh(main)
        async with self.jobs.transaction(main) as c:
            r, now = await self.guard(c, main)
            scope = await self.scope(c, r["engine_id"], r["symbol"], now)
            needed, pending = await self.missing(c, r["engine_id"], r["symbol"], now)
            if pending:
                raise DomainError("MINUTE_DATA_PENDING", "分钟核验待补齐", 503)
            needed = set(needed) | set(scope["pending_roles"])
            if role == "parent" and needed:
                raise DomainError("MISSING_TIMEFRAME", "必须先补齐所需周期", 409)
            if role != "parent" and role not in needed:
                return {"reused": True, "role": role}
            if role != "parent" and scope["window_until"] and scope["window_until"] > now:
                raise DomainError("ANALYSIS_WINDOW", "结构事件归并窗口尚未结束", 409)
            previous = (
                (
                    await c.execute(
                        sa.select(s.analysis_runs)
                        .where(
                            s.analysis_runs.c.round_id == r["id"], s.analysis_runs.c.role == role
                        )
                        .order_by(s.analysis_runs.c.number.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if previous and previous["state"] in ("PREPARING", "RUNNING", "SUBMITTING"):
                raise DomainError("ROLE_ALREADY_RUNNING", "该周期已有运行尝试", 409)
            counts = dict(r["counts"])
            number = counts.get(role, 0) + 1
            if number > (1 if r["recovery_index"] else 3):
                raise DomainError("ROLE_RETRIES_EXHAUSTED", "本角色重试次数已用尽", 409)
            counts[role] = number
            await c.execute(
                sa.update(s.analysis_rounds)
                .where(s.analysis_rounds.c.id == r["id"])
                .values(counts=counts)
            )
            ident, job_id, attempt_id = uuid4(), uuid4(), uuid4()
            deadline = min(r["deadline_at"], now + timedelta(seconds=150))
            row = {
                "id": ident,
                "round_id": r["id"],
                "coordinator_attempt_id": main.id,
                "role": role,
                "number": number,
                "launch_index": r["counts"].get("coordinator", 0),
                "job_id": job_id,
                "attempt_id": attempt_id,
                "state": "PREPARING",
                "started_at": now,
                "deadline_at": deadline,
            }
            stage = self._stage_attempt(row, main)
            await c.execute(
                sa.insert(s.jobs).values(
                    id=job_id,
                    kind=stage.kind,
                    payload=stage.payload,
                    dedupe_key=f"analysis-role:{ident}",
                    state="RUNNING",
                    attempts=1,
                    fence=1,
                    lease_owner=main.runtime_instance_id,
                    runtime_generation=main.runtime_generation,
                    lease_until=deadline,
                    next_run_at=now,
                    deadline_at=deadline,
                )
            )
            await c.execute(
                sa.insert(s.job_attempts).values(
                    id=attempt_id,
                    job_id=job_id,
                    fence=1,
                    status="RUNNING",
                    runtime_instance_id=main.runtime_instance_id,
                    runtime_generation=main.runtime_generation,
                    token_hash=token_hash(str(uuid4())),
                    token_expires_at=deadline,
                    started_at=now,
                )
            )
            await c.execute(sa.insert(s.analysis_runs).values(**row))
        try:
            async with asyncio.timeout(25):
                await self.market.prepare(stage)
            context = await self.predictions.context(stage)
            async with self.jobs.transaction(main) as c:
                _, now = await self.guard(c, main)
                execution_deadline = min(deadline, now + timedelta(seconds=120))
                await c.execute(
                    sa.update(s.analysis_runs)
                    .where(s.analysis_runs.c.id == ident)
                    .values(
                        state="RUNNING",
                        input_id=UUID(context["input_id"]),
                        execution_started_at=now,
                        execution_deadline=execution_deadline,
                    )
                )
            return {
                "run": str(ident),
                "role": role,
                "number": number,
                "context": model_context(context),
                "execution_seconds": max(0, (execution_deadline - now).total_seconds()),
            }
        except BaseException as exc:
            code = exc.code if isinstance(exc, DomainError) else "ROLE_PREPARATION_FAILED"
            async with self.jobs.transaction(main) as c:
                _, now = await self.guard(c, main)
                await self._end_unowned(c, row, now, code)
                await c.execute(
                    sa.update(s.analysis_scopes)
                    .where(scope_filter(s.analysis_scopes, r["engine_id"], r["symbol"]))
                    .values(parent_available=False, error_code=code, updated_at=now)
                )
            raise

    async def reject(self, main, run_id, code):
        if (
            not isinstance(code, str)
            or not code
            or len(code) > 64
            or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789" for ch in code)
        ):
            raise DomainError("ERROR_CODE", "错误类型无效", 422)
        async with self.jobs.transaction(main) as c:
            r, now = await self.guard(c, main)
            row = (
                (
                    await c.execute(
                        sa.select(s.analysis_runs)
                        .where(s.analysis_runs.c.id == UUID(str(run_id)))
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if not row or row["round_id"] != r["id"] or row["coordinator_attempt_id"] != main.id:
                raise DomainError("ANALYSIS_SCOPE", "角色不属于本次任务", 403)
            if row["state"] == "PUBLISHED":
                return {"published": True}
            await self._end_unowned(c, row, now, code)
            await c.execute(
                sa.update(s.analysis_scopes)
                .where(scope_filter(s.analysis_scopes, r["engine_id"], r["symbol"]))
                .values(parent_available=False, error_code=code, updated_at=now)
            )
            return {"published": False}

    async def check_conditions(self, stage, report):
        async with self.jobs.transaction(stage) as c:
            row = await self.predictions._input(c, stage)
            now = await self.database.now(c)
        start = boundary(row["decision_at"], "1m") + timedelta(minutes=1)
        stop = boundary(now, "1m")
        if start >= stop:
            return
        fetched = await self.market.client.minute_window(
            instrument(row["symbol"]),
            start,
            stop,
            min(stage.deadline_at, now + timedelta(seconds=10)),
        )
        async with self.jobs.transaction(stage) as c:
            # Check all returned bars BEFORE appending even one source fact.
            if any(
                b.inst_id != instrument(row["symbol"])
                or b.timeframe != "1m"
                or b.available_at > now
                or b.open_at < start
                or b.close_boundary > stop
                for b in fetched
            ) or any(a.open_at >= b.open_at for a, b in zip(fetched, fetched[1:], strict=False)):
                raise DomainError("MINUTE_SCOPE", "提交核验行情范围不正确", 422)
            for b in fetched:
                await persist_bar(c, b, row["symbol"])
        bars = await self.predictions.minute_bars(instrument(row["symbol"]), start, stop, now)
        cursor = start
        for index, b in enumerate(bars):
            if b.open_at != cursor:
                raise DomainError("MINUTE_DATA_PENDING", "分析区间行情尚未补齐", 503)
            cursor = b.close_boundary
            for scenario in report.scenarios:
                if (
                    report.primary_direction is None
                    or scenario.direction == report.primary_direction
                ) and condition_matches(scenario.invalidation, bars[: index + 1]):
                    raise DomainError("STRUCTURE_CHANGED", "分析期间声明的结构已经失效", 409)
        if cursor < stop:
            raise DomainError("MINUTE_DATA_PENDING", "分析区间行情尚未补齐", 503)

    async def submit(self, main, run_id, content):
        stage = await self.resolve(main, run_id)
        context = await self.predictions.context(stage)
        parent = stage.kind == "PREDICT_PARENT"
        from trading_agent.prediction.submissions import DecisionContent

        if parent != isinstance(content, DecisionContent):
            raise DomainError("REPORT_ROLE", "报告不属于本次绑定角色", 403)
        if parent:
            await self.refresh(main)
        try:
            report = (
                bind_decision(context, content) if parent else bind_prediction(context, content)
            )
        except ValidationError:
            raise DomainError("REQUEST_INVALID", "预测内容不满足字段或情景约束", 422) from None
        await self.check_conditions(stage, report.prediction if parent else report)

        async def committed(c, ident):
            r, now = await self.guard(c, main)
            active = (
                (
                    await c.execute(
                        sa.select(s.analysis_runs)
                        .where(s.analysis_runs.c.id == UUID(str(run_id)))
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            limit = (
                active["execution_deadline"]
                if active["state"] == "RUNNING"
                else active["deadline_at"]
            )
            if active["state"] not in {"RUNNING", "SUBMITTING"} or now >= limit:
                raise DomainError("ROLE_EXPIRED", "发布时角色已结束或超过执行期限", 409)
            await c.execute(
                sa.update(s.analysis_runs)
                .where(s.analysis_runs.c.id == UUID(str(run_id)))
                .values(state="PUBLISHED", result_id=ident, finished_at=now)
            )
            await self.jobs.finish_in_transaction(c, stage, result_id=ident)
            scope = await self.scope(c, r["engine_id"], r["symbol"], now)
            values = {"updated_at": now}
            if parent:
                needed, pending = await self.missing(c, r["engine_id"], r["symbol"], now)
                if needed or pending or scope["pending_roles"]:
                    raise DomainError("MISSING_TIMEFRAME", "父发布时仍有未完成的周期更新", 409)
                await c.execute(
                    sa.update(s.analysis_rounds)
                    .where(s.analysis_rounds.c.id == r["id"])
                    .values(state="SUCCEEDED", parent_result_id=ident, error_code=None)
                )
                values.update(
                    active_round_id=None,
                    state="IDLE",
                    parent_available=True,
                    error_code=None,
                    error_group=None,
                    recoveries=0,
                    resume_at=None,
                )
            else:
                values["pending_roles"] = [
                    x for x in scope["pending_roles"] if x != stage.payload["timeframe"]
                ]
            await c.execute(
                sa.update(s.analysis_scopes)
                .where(scope_filter(s.analysis_scopes, r["engine_id"], r["symbol"]))
                .values(**values)
            )
            await c.execute(
                sa.insert(s.audit_events).values(
                    actor_type="attempt",
                    actor_id=str(main.id),
                    action="ANALYSIS_ROLE_PUBLISHED",
                    object_type="prediction",
                    object_id=ident,
                    request_id=str(run_id),
                )
            )
            await c.execute(
                sa.insert(s.analysis_events).values(
                    engine_id=r["engine_id"],
                    symbol=r["symbol"],
                    source_key=f"published:{run_id}",
                    kind="ROLE_PUBLISHED",
                    body={
                        "role": context["role"],
                        "selected_child_refs": list(content.child_refs) if parent else [],
                        "prediction_id": str(ident),
                    },
                    created_at=now,
                )
            )

        await self.predictions.publish(stage, report, on_published=committed)
        return {"published": True, "role": context["role"]}

    async def failed(self, main, error):
        async with self.jobs.transaction(main, check_deadline=False, check_token_expiry=False) as c:
            r, now = await self.guard(c, main, deadline=False)
            if r["parent_result_id"]:
                await self.jobs.finish_in_transaction(c, main, result_id=r["parent_result_id"])
                return
            scope = await self.scope(c, r["engine_id"], r["symbol"], now)
            runs = (
                (
                    await c.execute(
                        sa.select(s.analysis_runs).where(
                            s.analysis_runs.c.round_id == r["id"],
                            s.analysis_runs.c.state.in_(("PREPARING", "RUNNING", "SUBMITTING")),
                        )
                    )
                )
                .mappings()
                .all()
            )
            for row in runs:
                await self._end_unowned(c, row, now, error.code)
            index = scope["recoveries"]
            recoverable = error.code not in {
                "ENGINE_CHANGED",
                "FORBIDDEN",
                "UNAUTHENTICATED",
                "PREDICTION_SCOPE",
                "API_CONFIGURATION_INVALID",
                "CUBE_CONFIGURATION_INVALID",
                "CUBE_CONFIGURATION_INCOMPLETE",
                "CUBE_TRANSPORT_UNPROTECTED",
                "CUBE_SDK_MISMATCH",
                "CUBE_OWNERSHIP_INVALID",
                "CUBE_RESOURCE_LIMITS",
                "CUBE_ISOLATION_FAILED",
                "CUBE_GUEST_AUTH_FAILED",
                "HERMES_NOT_CONFIGURED",
                "SANDBOX_INPUT_INVALID",
            }
            resume = (
                now + timedelta(seconds=RECOVERY_DELAYS[index])
                if index < 3 and recoverable
                else None
            )
            await c.execute(
                sa.update(s.analysis_scopes)
                .where(scope_filter(s.analysis_scopes, r["engine_id"], r["symbol"]))
                .values(
                    active_round_id=None,
                    parent_available=False,
                    state="ERROR" if resume else "EXHAUSTED",
                    resume_at=resume,
                    error_group=scope["error_group"] or uuid4(),
                    error_code=error.code,
                    updated_at=now,
                )
            )
            await c.execute(
                sa.update(s.analysis_rounds)
                .where(s.analysis_rounds.c.id == r["id"])
                .values(state="ERROR", error_code=error.code)
            )
            await self.jobs.finish_in_transaction(
                c, main, state="FAILED", error={"code": error.code, "label": "error"}
            )
            if scope["error_group"] is None or resume is None:
                from trading_agent.notifications.outbox import enqueue

                await enqueue(
                    c,
                    key=f"analysis-error:{scope['error_group'] or r['id']}:"
                    f"{'final' if resume is None else 'initial'}",
                    target=self.config.notification.target,
                    body=f"预测分析error：{r['symbol']}\nerror_code={error.code}\n旧父结论已停用；"
                    + ("恢复已停止。" if resume is None else "将按有界退避恢复。"),
                    now=now,
                )
            await c.execute(
                sa.insert(s.analysis_events).values(
                    engine_id=r["engine_id"],
                    symbol=r["symbol"],
                    source_key=f"error:{r['id']}",
                    kind="ERROR",
                    body={"code": error.code, "recovery_index": r["recovery_index"]},
                    created_at=now,
                )
            )

    async def expire(self, c, job, now):
        """Owner-only recovery of a dead coordinator; no expired attempt authorization."""
        await self.jobs.ownership.guard(c)
        ident = UUID(job["payload"]["analysis_round"])
        scope = await self.scope(
            c, UUID(job["payload"]["engine_id"]), job["payload"]["symbol"], now
        )
        if scope["active_round_id"] != ident:
            return
        code = "ANALYSIS_DEADLINE"
        rows = (
            (
                await c.execute(
                    sa.select(s.analysis_runs).where(
                        s.analysis_runs.c.round_id == ident,
                        s.analysis_runs.c.state.in_(("PREPARING", "RUNNING", "SUBMITTING")),
                    )
                )
            )
            .mappings()
            .all()
        )
        for run in rows:
            await self._end_unowned(c, run, now, code)
        delay = RECOVERY_DELAYS[scope["recoveries"]] if scope["recoveries"] < 3 else None
        group = scope["error_group"] or uuid4()
        await c.execute(
            sa.update(s.analysis_rounds)
            .where(s.analysis_rounds.c.id == ident)
            .values(state="ERROR", error_code=code)
        )
        await c.execute(
            sa.update(s.analysis_scopes)
            .where(
                scope_filter(
                    s.analysis_scopes, UUID(job["payload"]["engine_id"]), job["payload"]["symbol"]
                )
            )
            .values(
                active_round_id=None,
                state="ERROR" if delay else "EXHAUSTED",
                error_group=group,
                error_code=code,
                parent_available=False,
                resume_at=now + timedelta(seconds=delay) if delay else None,
                updated_at=now,
            )
        )
        await c.execute(
            insert(s.analysis_events)
            .values(
                engine_id=scope["engine_id"],
                symbol=scope["symbol"],
                source_key=f"error:{ident}",
                kind="ERROR",
                body={"code": code, "recovery_index": scope["recoveries"]},
                created_at=now,
            )
            .on_conflict_do_nothing()
        )
        if scope["error_group"] is None or delay is None:
            from trading_agent.notifications.outbox import enqueue

            await enqueue(
                c,
                key=f"analysis-error:{group}:{'final' if delay is None else 'initial'}",
                target=self.config.notification.target,
                body=f"预测分析error：{scope['symbol']}\nerror_code={code}\n旧父结论已停用；"
                + ("恢复已停止。" if delay is None else "将按有界退避恢复。"),
                now=now,
            )

    async def summary(self, c, engine):
        rows = (
            (
                await c.execute(
                    sa.select(s.analysis_scopes).where(s.analysis_scopes.c.engine_id == engine)
                )
            )
            .mappings()
            .all()
        )
        return [
            {
                "symbol": r["symbol"],
                "state": r["state"],
                "label": "error" if r["error_code"] else None,
                "error_code": r["error_code"],
                "parent_available": r["parent_available"],
                "pending_roles": r["pending_roles"],
                "recoveries": r["recoveries"],
                "resume_at": r["resume_at"],
                "window_until": r["window_until"],
            }
            for r in rows
        ]

    async def parent_available(self, c, engine, symbol):
        row = (
            (
                await c.execute(
                    sa.select(s.analysis_scopes).where(
                        scope_filter(s.analysis_scopes, engine, symbol)
                    )
                )
            )
            .mappings()
            .first()
        )
        return row is None or row["parent_available"]
