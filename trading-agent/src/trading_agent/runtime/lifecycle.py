from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy as sa

from trading_agent.api.app import APIServices
from trading_agent.domain.errors import DomainError, StaleAttempt
from trading_agent.execution.service import ExecutionService
from trading_agent.execution.settings import load_execution
from trading_agent.jobs.service import JobService
from trading_agent.knowledge.service import KnowledgeService
from trading_agent.notifications.outbox import HermesSender, Notifier, enqueue, is_context_refresh
from trading_agent.prediction.horizons import HorizonService
from trading_agent.prediction.market import PredictionMarket
from trading_agent.prediction.rounds import AnalysisRounds, coordinated
from trading_agent.prediction.scheduler import PredictionScheduler
from trading_agent.prediction.service import PredictionService
from trading_agent.runtime.agent_runner import AgentRunner
from trading_agent.runtime.logging import attempt_event, logging_status
from trading_agent.runtime.ownership import RuntimeOwnership
from trading_agent.runtime.processes import terminate_owned_orphan
from trading_agent.runtime.prompts import load_prompts, prompt_hashes
from trading_agent.storage import schema as s

logger = logging.getLogger(__name__)
DETERMINISTIC = {
    "PREDICT_VERIFY",
    "PREDICT_EXECUTE",
    "HORIZON_LEARN",
    "KNOWLEDGE_INDEX",
}
CORRECTABLE_PREDICTION_ERRORS = {
    "REQUEST_INVALID",
    "EVIDENCE_REFERENCE",
    "BOUNDARY_REFERENCE",
    "PREDICTION_GEOMETRY",
    "CHILD_REFERENCE",
    "KNOWLEDGE_REFERENCE",
    "HORIZON_RULE",
    "STRUCTURE_REFERENCE",
}


@dataclass
class LoopState:
    period: float
    last_progress: float = 0
    failure_code: str | None = None
    task: asyncio.Task | None = None


@dataclass
class Execution:
    attempt: object
    task: asyncio.Task


class Runtime:
    """One process owns API, durable execution, renewal, recovery and delivery."""

    def __init__(
        self,
        config,
        database,
        credentials,
        resources,
        market_client,
        deployment_check,
        *,
        sender=None,
        runner_factory=None,
        fatal_exit=None,
        loop_periods=None,
        sampler_factory=None,
        trade_collector_factory=None,
        execution_factory=None,
    ):
        if config.prediction.mode != "structural":
            raise DomainError("LEGACY_FLOW_RETIRED", "仅允许结构预测 runtime", 409)
        self.config, self.database, self.credentials = config, database, credentials
        self.resources, self.client, self.deployment_check = (
            resources,
            market_client,
            deployment_check,
        )
        self.ownership = RuntimeOwnership(database)
        self.sampler = (
            sampler_factory(
                database,
                self.ownership,
                config.market,
                on_fatal=self._fatal,
                inst_ids=tuple(
                    symbol.removesuffix("USDT") + "-USDT-SWAP"
                    for symbol in config.prediction.symbols
                ),
            )
            if sampler_factory
            else None
        )
        self.jobs = JobService(database, self.ownership)
        self.trade_collector = (
            trade_collector_factory(
                database,
                self.ownership,
                market_client,
                config.market,
                inst_ids=tuple(
                    symbol.removesuffix("USDT") + "-USDT-SWAP"
                    for symbol in config.prediction.symbols
                ),
                on_fatal=self._fatal,
            )
            if trade_collector_factory and config.market.taker_source == "trade_minutes"
            else None
        )
        self.jobs.enforce_slots = True
        if hasattr(resources, "jobs"):
            resources.jobs = self.jobs
        self.predictions = PredictionService(self.jobs, config)
        self.knowledge = KnowledgeService(self.jobs, config)
        self.horizons = HorizonService(self.jobs, config, self.predictions)
        self.execution = None
        self.execution_factory = execution_factory
        self.prompts = load_prompts(mode=config.prediction.mode)
        factory = runner_factory or AgentRunner
        self.runner = factory(
            self.jobs, self.predictions, resources, self.prompts, knowledge=self.knowledge
        )
        self.notifier = Notifier(
            database,
            self.ownership,
            sender or HermesSender(config),
            config,
            on_progress=lambda: self.progress("notifier"),
        )
        periods = loop_periods or {
            "scheduler": config.runtime.scheduler_tick_seconds,
            "executor": config.runtime.executor_poll_seconds,
            "notifier": config.runtime.notifier_poll_seconds,
            "recovery": config.runtime.recovery_tick_seconds,
        }
        self.loops = {name: LoopState(value) for name, value in periods.items()}
        if set(self.loops) != {"scheduler", "executor", "notifier", "recovery"}:
            raise ValueError("All four critical runtime loops are required")
        self.fatal_exit = fatal_exit or (lambda: os.kill(os.getpid(), signal.SIGTERM))
        self.active = {}
        self.started, self.stopping, self.fatal = False, False, False
        self.deployment_ready = False
        self.engine_id = None
        self.agent_health = {"healthy": True, "error_code": None}

    async def start(self):
        try:
            if self.config.paper.enabled or self.config.experiment.enabled:
                raise DomainError(
                    "CONTRACT_IMPLEMENTATION_NOT_READY", "合约账户实现仍受规格门禁保护", 503
                )
            await self.database.require_migration()
            self.deployment_ready = bool(await self.deployment_check())
            if not self.deployment_ready:
                raise DomainError(
                    "DEPLOYMENT_NOT_VERIFIED", "部署配置、身份或隔离证据尚未通过校验", 503
                )
            await self.ownership.acquire()
            self.engine_id = await self.predictions.register_engine(prompt_hashes(self.prompts))
            await self.knowledge.seed()
            self.scheduler = PredictionScheduler(
                self.jobs, self.config, self.engine_id, self.predictions
            )
            self.market = PredictionMarket(
                self.jobs,
                self.client,
                self.predictions,
                self.knowledge,
                self.engine_id,
                sampler=self.sampler,
            )
            self.rounds = AnalysisRounds(self.predictions, self.market)
            settings, transport = (
                (self.execution_factory or load_execution)(self.config)
                if self.config.prediction.execution_enabled
                else (None, None)
            )
            self.execution = ExecutionService(
                self.jobs, self.config, self.predictions, self.engine_id, settings, transport
            )
            await self.execution.validate_engine()
            await self.resources.recover()
            await self.recover_jobs()
            await self.reap_processes()
            await self.jobs.recover_model_slots()
            await self.scheduler.tick()
            if self.sampler:
                await self.sampler.start()
            if self.trade_collector:
                await self.trade_collector.start()
            for name, action in {
                "scheduler": self.scheduler.tick,
                "executor": self.execute_tick,
                "notifier": self.notifier.tick,
                "recovery": self.recovery_tick,
            }.items():
                self.loops[name].task = asyncio.create_task(self._loop(name, action), name=name)
            self.started = True
            logger.info(
                "runtime_started", extra={"runtime_instance_id": str(self.ownership.instance_id)}
            )
        except BaseException as exc:
            logger.error(
                "runtime_start_failed",
                extra={
                    "error_code": exc.code
                    if isinstance(exc, DomainError)
                    else "RUNTIME_START_FAILED"
                },
            )
            await self.stop()
            raise

    def api_services(self):
        return APIServices(
            self.config,
            self.credentials,
            self.jobs,
            self.engine_id,
            self.ready,
            predictions=self.predictions,
            knowledge=self.knowledge,
        )

    def progress(self, name):
        if name in self.loops:
            self.loops[name].last_progress = time.monotonic()

    async def _loop(self, name, action):
        state = self.loops[name]
        self.progress(name)
        try:
            while not self.stopping:
                try:
                    await action()
                    state.failure_code = None
                    self.progress(name)
                except StaleAttempt:
                    self._fatal("RUNTIME_OWNERSHIP_LOST")
                    return
                except (sa.exc.SQLAlchemyError, DomainError) as exc:
                    state.failure_code = (
                        exc.code if isinstance(exc, DomainError) else "DATABASE_UNAVAILABLE"
                    )
                    logger.warning(
                        "runtime_loop_retry",
                        extra={"event": name, "error_code": state.failure_code},
                    )
                # Align the normal scheduler to UTC minute boundaries after its startup scan.
                if name == "scheduler" and state.period == 60:
                    delay = 60 - (time.time() % 60)
                else:
                    delay = state.period
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            state.failure_code = "LOOP_CRASHED"
            logger.error(
                "runtime_loop_crashed", extra={"event": name, "error_code": "LOOP_CRASHED"}
            )
            self._fatal("LOOP_CRASHED")

    def _fatal(self, code):
        if self.fatal or self.stopping:
            return
        self.fatal = True
        logger.error("runtime_fatal", extra={"error_code": code})
        for execution in self.active.values():
            if not execution.task.done():
                execution.task.cancel()
        for state in self.loops.values():
            if state.task and state.task is not asyncio.current_task() and not state.task.done():
                state.task.cancel()
        self.fatal_exit()

    async def ready(self):
        checks = {
            "started": self.started and not self.stopping and not self.fatal,
            "deployment": self.deployment_ready,
        }
        try:
            await self.database.require_migration()
            checks["database"] = True
            checks["instance_lock"] = await self.ownership.probe()
            async with asyncio.timeout(2):
                check = getattr(self.deployment_check, "ready", self.deployment_check)
                checks["deployment"] = bool(await check())
            async with asyncio.timeout(2):
                checks["sandbox"] = bool(await self.resources.ready())
        except (DomainError, sa.exc.SQLAlchemyError, TimeoutError, OSError):
            checks["database"] = checks["instance_lock"] = checks["sandbox"] = False
        now = time.monotonic()
        logs = logging_status()
        checks["logging"] = logs["healthy"]
        analysis = []
        if rounds := getattr(self, "rounds", None):
            try:
                async with self.database.engine.connect() as c:
                    analysis = await rounds.summary(c, self.engine_id)
            except sa.exc.SQLAlchemyError:
                checks["database"] = False
        checks["agent"] = self.agent_health["healthy"] and not any(
            r["label"] == "error" for r in analysis
        )
        loops = {}
        for name, state in self.loops.items():
            healthy = (
                state.task is not None
                and not state.task.done()
                and state.failure_code is None
                and now - state.last_progress
                <= state.period * self.config.runtime.loop_stale_multiplier
            )
            checks[name] = healthy
            loops[name] = {
                "healthy": healthy,
                "failure_code": state.failure_code,
                "seconds_since_progress": round(now - state.last_progress, 3),
            }
        return {
            "ready": all(checks.values()),
            "checks": checks,
            "loops": loops,
            "logging": logs,
            "agent": self.agent_health,
            "analysis": analysis,
            "liquidation_sampler": {
                "configured": self.sampler is not None,
                "connected": bool(self.sampler and self.sampler.connected),
                "failure_code": self.sampler.failure_code if self.sampler else None,
            },
            "trade_minute_collector": (
                self.trade_collector.health() if self.trade_collector else {"running": False}
            ),
        }

    async def execute_tick(self):
        if self.fatal or self.stopping:
            return
        for ident, execution in tuple(self.active.items()):
            if execution.task.done():
                # _execute records errors; retrieving the result also consumes cancellation.
                await asyncio.gather(execution.task, return_exceptions=True)
                self.active.pop(ident)
        if rounds := getattr(self, "rounds", None):
            await rounds.flush(self.engine_id)
        deterministic = sum(e.attempt.kind in DETERMINISTIC for e in self.active.values())
        for _ in range(self.config.runtime.deterministic_concurrency - deterministic):
            attempt = await self.jobs.claim(("PREDICT_EXECUTE", "PREDICT_VERIFY"))
            if attempt is None:
                attempt = await self.jobs.claim(("KNOWLEDGE_INDEX", "HORIZON_LEARN"))
            if attempt is None:
                break
            self._dispatch(attempt)
        models = sum(e.attempt.kind not in DETERMINISTIC for e in self.active.values())
        for _ in range(self.config.runtime.parent_agent_concurrency - models):
            attempt = await self.jobs.claim(
                ("PREDICT_PARENT", "PREDICT_TIMEFRAME", "PREDICT_REVIEW")
            )
            if attempt is None:
                break
            self._dispatch(attempt)

    def _dispatch(self, attempt):
        task = asyncio.create_task(self._execute(attempt), name=f"job:{attempt.job_id}")
        self.active[attempt.id] = Execution(attempt, task)

    async def _execute(self, attempt):
        try:
            if attempt.kind in {"PREDICT_TIMEFRAME", "PREDICT_PARENT", "PREDICT_REVIEW"}:
                if attempt.payload.get("engine_id") != str(self.engine_id):
                    raise DomainError("ENGINE_CHANGED", "任务版本与当前部署不一致", 409)
                result = await self.runner._result(attempt)
                if result is None:
                    if coordinated(attempt):
                        attempt = await self.rounds.start(attempt)
                    else:
                        await self.market.prepare(attempt)
                    result = await self.runner.run(attempt)
                await self.jobs.finish(attempt, result_id=result)
                self.agent_health = {"healthy": True, "error_code": None}
            elif attempt.kind == "PREDICT_VERIFY":
                verification = await self.market.verify(attempt)
                await self.execution.monitor(attempt)
                if verification["data_quality"] == "AWAITING_DATA":
                    # A just-closed public candle may appear after the minute
                    # boundary. Keep the durable job pending until its gap is
                    # actually filled, instead of staying one minute behind.
                    async with self.jobs.transaction(attempt) as connection:
                        now = await self.database.now(connection)
                    await self.jobs.finish(
                        attempt,
                        state="AWAITING_DATA",
                        error={"code": "MINUTE_DATA_PENDING"},
                        next_run_at=now + timedelta(seconds=5),
                    )
                else:
                    await self.jobs.finish(attempt)
            elif attempt.kind == "PREDICT_EXECUTE":
                result = await self.execution.run(attempt)
                await self.jobs.finish(attempt, result_id=result)
            elif attempt.kind == "HORIZON_LEARN":
                await self.horizons.run(attempt)
                await self.jobs.finish(attempt)
            elif attempt.kind == "KNOWLEDGE_INDEX":
                await self.knowledge.sync(attempt)
            else:
                raise DomainError("LEGACY_FLOW_RETIRED", "该任务不属于结构预测 runtime", 409)
        except asyncio.CancelledError:
            await self._failure(attempt, DomainError("RUNTIME_STOPPING", "执行已终止", 503))
            raise
        except DomainError as exc:
            await self._failure(attempt, exc)
        except (OSError, sa.exc.SQLAlchemyError):
            await self._failure(
                attempt, DomainError("DEPENDENCY_UNAVAILABLE", "任务依赖暂不可用", 503)
            )
        except Exception:
            logger.error(
                "job_unexpected_error",
                extra={
                    "job_id": str(attempt.job_id),
                    "attempt_id": str(attempt.id),
                    "error_code": "UNEXPECTED_ERROR",
                },
                exc_info=True,
            )
            await self._failure(attempt, DomainError("UNEXPECTED_ERROR", "任务执行异常", 503))
        finally:
            if attempt.kind in {
                "PREDICT_PARENT",
                "PREDICT_TIMEFRAME",
                "PREDICT_REVIEW",
            } and getattr(self.jobs, "enforce_slots", False):
                cleaned = False
                try:
                    cleaned = await self.resources.cleanup(attempt) is not False
                except (DomainError, OSError, sa.exc.SQLAlchemyError):
                    logger.warning("model_slot_cleanup_pending")
                await self.jobs.release_model_slot(attempt, cleaned=cleaned)

    async def _failure(self, attempt, error):
        if coordinated(attempt):
            await self.rounds.failed(attempt, error)
            published = bool(await self.rounds.result(attempt))
            self.agent_health = {
                "healthy": published,
                "error_code": None if published else error.code,
            }
            return
        if error.code in {
            "AGENT_START_FAILED",
            "AGENT_NO_FORMAL_RESULT",
            "AGENT_TIMEOUT",
            *CORRECTABLE_PREDICTION_ERRORS,
        }:
            self.agent_health = {"healthy": False, "error_code": error.code}
        try:
            # A committed publication wins over process, cleanup and transport errors.
            result = (
                await self.runner._result(attempt) if attempt.kind not in DETERMINISTIC else None
            )
            if result:
                await self.jobs.finish(attempt, result_id=result)
                self.agent_health = {"healthy": True, "error_code": None}
                return
            async with self.database.engine.connect() as connection:
                now = await self.database.now(connection)
                job = (
                    (
                        await connection.execute(
                            sa.select(s.jobs).where(s.jobs.c.id == attempt.job_id)
                        )
                    )
                    .mappings()
                    .one()
                )
                frozen = await connection.scalar(
                    sa.select(s.prediction_inputs.c.id).where(
                        s.prediction_inputs.c.job_id == attempt.job_id
                    )
                )
            state = "FAILED"
            if attempt.kind in {
                "PREDICT_TIMEFRAME",
                "PREDICT_PARENT",
                "PREDICT_REVIEW",
            }:
                if (
                    (attempt.deadline_at is not None and attempt.deadline_at < now)
                    or "DEADLINE" in error.code
                    or error.code == "ENGINE_CHANGED"
                ):
                    state = "SKIPPED"
                elif job["attempts"] < 2 and error.status >= 500:
                    state = "RETRY_WAIT"
            elif (
                attempt.kind
                in {
                    "PREDICT_EXECUTE",
                    "KNOWLEDGE_INDEX",
                    "HORIZON_LEARN",
                    "PREDICT_VERIFY",
                }
                and error.status >= 500
            ):
                state = "RETRY_WAIT"
            elif error.code == "RUNTIME_STOPPING":
                state = "RETRY_WAIT"
            waiting_for_data = (
                error.code == "MINUTE_DATA_PENDING"
                and attempt.kind == "PREDICT_PARENT"
                and frozen is None
                and state != "SKIPPED"
            )
            if waiting_for_data:
                state = "AWAITING_DATA"
            superseded = error.code == "ANALYSIS_SUPERSEDED"
            if superseded:
                state = "SKIPPED"
            refresh_context = is_context_refresh(attempt.kind, error.code)
            retry_fresh = (
                frozen is not None
                and attempt.kind in {"PREDICT_PARENT", "PREDICT_TIMEFRAME"}
                and state != "SKIPPED"
                and attempt.payload.get("analysis_retry_count", 0) < 1
                and (error.status >= 500 or error.code in CORRECTABLE_PREDICTION_ERRORS)
            )
            recover_context = refresh_context and state != "SKIPPED"
            successor = None
            async with self.jobs.transaction(
                attempt, check_deadline=False, check_token_expiry=False
            ) as connection:
                # Publication also locks this job. Recheck after taking that
                # lock: a submission may have committed after the first read.
                publication_query = None
                if attempt.kind in {"PREDICT_PARENT", "PREDICT_TIMEFRAME"}:
                    publication_query = sa.select(s.prediction_facts.c.id).where(
                        s.prediction_facts.c.job_id == attempt.job_id
                    )
                elif attempt.kind == "PREDICT_REVIEW":
                    publication_query = (
                        sa.select(s.reviews.c.id)
                        .join(s.job_attempts, s.job_attempts.c.id == s.reviews.c.attempt_id)
                        .where(s.job_attempts.c.job_id == attempt.job_id)
                        .limit(1)
                    )
                committed = (
                    await connection.scalar(publication_query)
                    if publication_query is not None
                    else None
                )
                if committed:
                    await self.jobs.finish_in_transaction(connection, attempt, result_id=committed)
                    self.agent_health = {"healthy": True, "error_code": None}
                    return
                now = await self.database.now(connection)
                if attempt.deadline_at is not None and attempt.deadline_at < now:
                    state, retry_fresh, recover_context = "SKIPPED", False, False
                if (
                    frozen is not None
                    and attempt.kind in {"PREDICT_PARENT", "PREDICT_TIMEFRAME"}
                    and state == "RETRY_WAIT"
                ) or recover_context:
                    # A frozen analysis never waits to retry the same old input.
                    state = "FAILED"
                await self.jobs.finish_in_transaction(
                    connection,
                    attempt,
                    state=state,
                    error={"code": error.code},
                    next_run_at=now + timedelta(seconds=5 if waiting_for_data else 60),
                )
                if retry_fresh or recover_context:
                    successor = await self.predictions.queue_retry(
                        connection,
                        attempt,
                        now,
                        delay_seconds=0 if refresh_context else 60,
                        recovery=refresh_context,
                    )
                    if successor:
                        await connection.execute(
                            sa.insert(s.audit_events).values(
                                actor_type="attempt",
                                actor_id=str(attempt.id),
                                action="ANALYSIS_RETRY_SCHEDULED",
                                object_type="job",
                                object_id=successor,
                                request_id=f"retry:{attempt.job_id}",
                            )
                        )
                if (
                    state in {"FAILED", "SKIPPED"}
                    and not refresh_context
                    and not successor
                    and not superseded
                ):
                    await enqueue(
                        connection,
                        key=f"job-failed:{attempt.job_id}:{error.code}",
                        target=self.config.notification.target,
                        body=f"交易任务{state}：{attempt.kind}\njob_id={attempt.job_id}\n"
                        f"error_code={error.code}\n请通过主控查询任务状态。",
                        now=now,
                    )
            attempt_event("job_finished", attempt, delivery_status=state, error_code=error.code)
        except (DomainError, sa.exc.SQLAlchemyError):
            # Lost ownership cannot be repaired by this executor; the next owner recovers facts.
            logger.warning(
                "job_result_deferred",
                extra={"job_id": str(attempt.job_id), "error_code": error.code},
            )

    async def recovery_tick(self):
        if not await self.ownership.probe():
            raise StaleAttempt()
        for execution in tuple(self.active.values()):
            if not execution.task.done() and not await self.jobs.renew(execution.attempt):
                execution.task.cancel()
        await self.recover_jobs()
        await self.reap_processes()
        await self.resources.recover()
        await self.jobs.recover_model_slots()

    async def reap_processes(self):
        async with self.database.transaction() as connection:
            await self.ownership.guard(connection)
            rows = (
                (
                    await connection.execute(
                        sa.select(s.job_attempts)
                        .where(
                            s.job_attempts.c.process_id.is_not(None),
                            sa.or_(
                                s.job_attempts.c.status != "RUNNING",
                                s.job_attempts.c.runtime_instance_id != self.ownership.instance_id,
                            ),
                        )
                        .limit(16)
                    )
                )
                .mappings()
                .all()
            )

        async def reap(row):
            root = (self.config.runtime.data_dir / "attempts" / str(row["id"])).resolve()
            status = await terminate_owned_orphan(
                row["process_id"],
                row["process_started_at"],
                attempt_id=row["id"],
                token_digest=row["token_hash"],
                cwd=root,
                grace=self.config.runtime.kill_grace_seconds,
            )
            if status == "UNVERIFIED":
                return
            async with self.database.transaction() as connection:
                await self.ownership.guard(connection)
                usage = dict(row["usage_json"] or {})
                usage["process_recovery"] = {
                    "pid": row["process_id"],
                    "created": row["process_started_at"],
                    "status": status,
                }
                await connection.execute(
                    sa.update(s.job_attempts)
                    .where(
                        s.job_attempts.c.id == row["id"],
                        s.job_attempts.c.process_id == row["process_id"],
                    )
                    .values(process_id=None, usage_json=usage)
                )

        await asyncio.gather(*(reap(row) for row in rows))

    async def recover_jobs(self):
        async with self.database.transaction() as connection:
            await self.ownership.guard(connection)
            now = await self.database.now(connection)
            rows = (
                (
                    await connection.execute(
                        sa.select(s.jobs)
                        .where(
                            s.jobs.c.kind.in_(
                                (
                                    "FORECAST",
                                    "SETTLE",
                                    "PRICE_REFRESH",
                                    "REVIEW_DAILY",
                                    "EXPLAIN",
                                    "EXPERIMENT",
                                    "PREDICT_TIMEFRAME",
                                    "PREDICT_PARENT",
                                    "PREDICT_REVIEW",
                                )
                            ),
                            s.jobs.c.state.in_(
                                ("QUEUED", "RUNNING", "RETRY_WAIT", "AWAITING_DATA")
                            ),
                            sa.func.coalesce(
                                s.jobs.c.payload["analysis_managed"].as_boolean(), False
                            ).is_(False),
                            sa.or_(s.jobs.c.state != "RUNNING", s.jobs.c.lease_until <= now),
                        )
                        .order_by(s.jobs.c.next_run_at)
                        .limit(168)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                if row["state"] == "RUNNING" and row["lease_until"] <= now:
                    await connection.execute(
                        sa.update(s.job_attempts)
                        .where(
                            s.job_attempts.c.job_id == row["id"],
                            s.job_attempts.c.status == "RUNNING",
                        )
                        .values(status="EXPIRED", token_revoked=True, ended_at=now)
                    )
                if row["payload"].get("analysis_round"):
                    result = await connection.scalar(
                        sa.select(s.analysis_rounds.c.parent_result_id).where(
                            s.analysis_rounds.c.job_id == row["id"]
                        )
                    )
                    code = None
                    if result:
                        await connection.execute(
                            sa.update(s.jobs)
                            .where(s.jobs.c.id == row["id"])
                            .values(
                                state="SUCCEEDED",
                                result_id=result,
                                lease_owner=None,
                                lease_until=None,
                                updated_at=now,
                            )
                        )
                        await connection.execute(
                            sa.update(s.job_attempts)
                            .where(
                                s.job_attempts.c.job_id == row["id"],
                                s.job_attempts.c.status == "RUNNING",
                            )
                            .values(status="SUCCEEDED", token_revoked=True, ended_at=now)
                        )
                    elif row["deadline_at"] and row["deadline_at"] <= now:
                        code = "ANALYSIS_DEADLINE"
                        await connection.execute(
                            sa.update(s.jobs)
                            .where(s.jobs.c.id == row["id"])
                            .values(
                                state="FAILED",
                                last_error={"code": code, "label": "error"},
                                lease_owner=None,
                                lease_until=None,
                                updated_at=now,
                            )
                        )
                        await self.predictions.rounds.expire(connection, row, now)
                    continue
                if (
                    row["kind"] in {"PREDICT_TIMEFRAME", "PREDICT_PARENT"}
                    and not row["payload"].get("analysis_managed")
                    and getattr(self.predictions, "rounds", None)
                ):
                    await connection.execute(
                        sa.update(s.jobs)
                        .where(s.jobs.c.id == row["id"])
                        .values(
                            state="SKIPPED",
                            last_error={"code": "WORKFLOW_REPLACED"},
                            lease_owner=None,
                            lease_until=None,
                            updated_at=now,
                        )
                    )
                    continue
                retired = not row["kind"].startswith("PREDICT_")
                if row["kind"] in {"PREDICT_REVIEW", "REVIEW_DAILY", "EXPLAIN"}:
                    result = await connection.scalar(
                        sa.select(s.reviews.c.id)
                        .join(s.job_attempts, s.job_attempts.c.id == s.reviews.c.attempt_id)
                        .where(s.job_attempts.c.job_id == row["id"])
                        .limit(1)
                    )
                else:
                    result = await connection.scalar(
                        sa.select(s.prediction_facts.c.id).where(
                            s.prediction_facts.c.job_id == row["id"]
                        )
                        if row["kind"].startswith("PREDICT_")
                        else sa.select(s.forecasts.c.id).where(s.forecasts.c.job_id == row["id"])
                    )
                if result:
                    state, reason = "SUCCEEDED", None
                elif (row["deadline_at"] is not None and row["deadline_at"] < now) or (retired):
                    state, reason = (
                        "SKIPPED",
                        "LEGACY_FLOW_RETIRED" if retired else "FORECAST_DEADLINE",
                    )
                else:
                    continue
                await connection.execute(
                    sa.update(s.jobs)
                    .where(s.jobs.c.id == row["id"])
                    .values(
                        state=state,
                        result_id=result,
                        lease_until=None,
                        lease_owner=None,
                        last_error={"code": reason} if reason else None,
                        updated_at=now,
                    )
                )
                await connection.execute(
                    sa.update(s.job_attempts)
                    .where(
                        s.job_attempts.c.job_id == row["id"], s.job_attempts.c.status == "RUNNING"
                    )
                    .values(
                        status="SUCCEEDED" if result else "EXPIRED",
                        token_revoked=True,
                        ended_at=now,
                    )
                )
                if reason:
                    await connection.execute(
                        sa.update(s.schedule_slots)
                        .where(s.schedule_slots.c.job_id == row["id"])
                        .values(status="SKIPPED", reason=reason)
                    )

    async def stop(self):
        if self.stopping:
            return
        self.stopping, self.started = True, False
        loops = [state.task for state in self.loops.values() if state.task]
        for task in loops:
            task.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        tasks = [execution.task for execution in self.active.values()]
        if tasks:
            _, pending = await asyncio.wait(
                tasks, timeout=self.config.runtime.shutdown_grace_seconds
            )
            for task in pending:
                task.cancel()
        await self.runner.stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.sampler:
            await self.sampler.stop()
        if self.trade_collector:
            await self.trade_collector.stop()
        try:
            await self.ownership.release()
        finally:
            if hasattr(self.deployment_check, "close"):
                await self.deployment_check.close()
            await self.client.close()
            if hasattr(self.resources, "close"):
                await self.resources.close()
            await self.database.close()
        logger.info(
            "runtime_stopped", extra={"runtime_instance_id": str(self.ownership.instance_id)}
        )
