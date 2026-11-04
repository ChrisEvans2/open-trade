from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy as sa

from legacy_research.api.app import APIServices
from trading_agent.domain.errors import DomainError, StaleAttempt
from trading_agent.execution.service import ExecutionService
from trading_agent.execution.settings import load_execution
from legacy_research.forecasts.service import ForecastService
from legacy_research.forecasts.settlement import SettlementService
from trading_agent.jobs.service import JobService
from trading_agent.knowledge.service import KnowledgeService
from legacy_research.market.refresh import PriceRefreshService
from legacy_research.market.service import MarketService
from legacy_research.memory.retrieval import MemoryService
from trading_agent.notifications.outbox import HermesSender, Notifier
from trading_agent.prediction.horizons import HorizonService
from trading_agent.prediction.market import PredictionMarket
from trading_agent.prediction.scheduler import PredictionScheduler
from trading_agent.prediction.service import PredictionService
from legacy_research.reviews.service import ReviewService
from legacy_research.runtime.agent_runner import AgentRunner
from trading_agent.runtime.logging import logging_status
from trading_agent.runtime.ownership import RuntimeOwnership
from trading_agent.runtime.processes import terminate_owned_orphan
from legacy_research.runtime.prompts import load_prompts, prompt_hashes
from legacy_research.runtime.scheduler import Scheduler, low_priority_allowed
from trading_agent.storage import schema as s

logger = logging.getLogger(__name__)
DETERMINISTIC = {
    "SETTLE",
    "PRICE_REFRESH",
    "PREDICT_VERIFY",
    "PREDICT_EXECUTE",
    "HORIZON_LEARN",
    "KNOWLEDGE_INDEX",
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
        execution_factory=None,
    ):
        self.config, self.database, self.credentials = config, database, credentials
        self.resources, self.client, self.deployment_check = (
            resources,
            market_client,
            deployment_check,
        )
        self.ownership = RuntimeOwnership(database)
        self.sampler = (
            sampler_factory(database, self.ownership, config.market, on_fatal=self._fatal)
            if sampler_factory
            else None
        )
        self.jobs = JobService(database, self.ownership)
        if hasattr(resources, "jobs"):
            resources.jobs = self.jobs
        self.forecasts = ForecastService(self.jobs, config)
        self.reviews = ReviewService(self.jobs, config)
        self.memory = MemoryService(database, config.memory)
        self.settlement = SettlementService(self.jobs, market_client, config)
        self.price_refresh = PriceRefreshService(self.jobs, market_client, self.settlement)
        self.structural = config.prediction.mode == "structural"
        self.predictions = PredictionService(self.jobs, config)
        self.knowledge = KnowledgeService(self.jobs, config)
        self.horizons = HorizonService(self.jobs, config, self.predictions)
        self.execution = None
        self.execution_factory = execution_factory
        self.prompts = load_prompts(mode=config.prediction.mode)
        factory = runner_factory or AgentRunner
        self.runner = factory(
            self.jobs,
            self.forecasts,
            resources,
            self.prompts,
            reviews=self.reviews,
            **(
                {"predictions": self.predictions, "knowledge": self.knowledge}
                if self.structural
                else {}
            ),
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
            if self.structural:
                self.engine_id = await self.predictions.register_engine(prompt_hashes(self.prompts))
                await self.knowledge.seed()
                self.scheduler = PredictionScheduler(
                    self.jobs, self.config, self.engine_id, self.predictions
                )
                self.market = PredictionMarket(
                    self.jobs, self.client, self.predictions, self.knowledge, self.engine_id
                )
                settings, transport = (
                    (self.execution_factory or load_execution)(self.config)
                    if self.config.prediction.execution_enabled
                    else (None, None)
                )
                self.execution = ExecutionService(
                    self.jobs, self.config, self.predictions, self.engine_id, settings, transport
                )
                await self.execution.validate_engine()
            else:
                self.engine_id = await self.forecasts.register_engine(prompt_hashes(self.prompts))
                self.scheduler = Scheduler(self.jobs, self.config, self.engine_id)
                self.market = MarketService(
                    self.jobs,
                    self.client,
                    self.forecasts,
                    self.memory,
                    self.engine_id,
                    sampler=self.sampler,
                )
            await self.resources.recover()
            await self.recover_jobs()
            await self.reap_processes()
            await self.scheduler.tick()
            if self.sampler:
                await self.sampler.start()
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
            self.forecasts,
            self.engine_id,
            self.ready,
            self.reviews,
            self.predictions if self.structural else None,
            self.knowledge if self.structural else None,
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
                checks["deployment"] = bool(await self.deployment_check())
            async with asyncio.timeout(2):
                checks["sandbox"] = bool(await self.resources.ready())
        except (DomainError, sa.exc.SQLAlchemyError, TimeoutError, OSError):
            checks["database"] = checks["instance_lock"] = checks["sandbox"] = False
        now = time.monotonic()
        logs = logging_status()
        checks["logging"] = logs["healthy"]
        checks["agent"] = self.agent_health["healthy"]
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
            "liquidation_sampler": {
                "configured": self.sampler is not None,
                "connected": bool(self.sampler and self.sampler.connected),
                "failure_code": self.sampler.failure_code if self.sampler else None,
            },
        }

    async def execute_tick(self):
        if self.fatal or self.stopping:
            return
        for ident, execution in tuple(self.active.items()):
            if execution.task.done():
                # _execute records errors; retrieving the result also consumes cancellation.
                await asyncio.gather(execution.task, return_exceptions=True)
                self.active.pop(ident)
        deterministic = sum(e.attempt.kind in DETERMINISTIC for e in self.active.values())
        for _ in range(self.config.runtime.deterministic_concurrency - deterministic):
            attempt = (
                await self.jobs.claim(("PREDICT_EXECUTE", "PREDICT_VERIFY", "SETTLE"))
                if self.structural
                else await self.jobs.claim(("SETTLE",))
            )
            if attempt is None:
                async with self.database.engine.connect() as connection:
                    now = await self.database.now(connection)
                if self.structural or low_priority_allowed(now):
                    attempt = (
                        await self.jobs.claim(("KNOWLEDGE_INDEX", "HORIZON_LEARN", "PRICE_REFRESH"))
                        if self.structural
                        else await self.jobs.claim(("PRICE_REFRESH",))
                    )
                if attempt is None:
                    break
            self._dispatch(attempt)
        if any(e.attempt.kind not in DETERMINISTIC for e in self.active.values()):
            return
        attempt = (
            await self.jobs.claim(("PREDICT_PARENT", "PREDICT_TIMEFRAME"))
            if self.structural
            else await self.jobs.claim(("FORECAST",))
        )
        if attempt is None:
            async with self.database.engine.connect() as connection:
                now = await self.database.now(connection)
            if self.structural or low_priority_allowed(now):
                attempt = (
                    await self.jobs.claim(("PREDICT_REVIEW",))
                    if self.structural
                    else await self.jobs.claim(("REVIEW_DAILY", "EXPLAIN"))
                )
        if attempt:
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
                    await self.market.prepare(attempt)
                    result = await self.runner.run(attempt)
                await self.jobs.finish(attempt, result_id=result)
                self.agent_health = {"healthy": True, "error_code": None}
            elif attempt.kind == "PREDICT_VERIFY":
                await self.market.verify(attempt)
                await self.execution.monitor(attempt)
                await self.jobs.finish(attempt)
            elif attempt.kind == "PREDICT_EXECUTE":
                result = await self.execution.run(attempt)
                await self.jobs.finish(attempt, result_id=result)
            elif attempt.kind == "HORIZON_LEARN":
                await self.horizons.run(attempt)
                await self.jobs.finish(attempt)
            elif attempt.kind == "KNOWLEDGE_INDEX":
                await self.knowledge.sync(attempt)
            elif attempt.kind == "FORECAST":
                if attempt.payload.get("engine_id") != str(self.engine_id):
                    raise DomainError("ENGINE_CHANGED", "任务版本与当前部署不一致", 409)
                result = await self.runner._result(attempt)
                if result is None:
                    await self.market.prepare(attempt)
                    result = await self.runner.run(attempt)
                await self.jobs.finish(attempt, result_id=result)
                self.agent_health = {"healthy": True, "error_code": None}
            elif attempt.kind == "SETTLE":
                await self.settlement.run(attempt)
            elif attempt.kind == "PRICE_REFRESH":
                await self.price_refresh.run(attempt)
            else:
                await self.reviews.prepare(attempt)
                result = await self.runner.run(attempt)
                await self.jobs.finish(attempt, result_id=result)
                self.agent_health = {"healthy": True, "error_code": None}
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

    async def _failure(self, attempt, error):
        if error.code in {"AGENT_START_FAILED", "AGENT_NO_FORMAL_RESULT", "AGENT_TIMEOUT"}:
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
            state = "FAILED"
            if attempt.kind in {
                "FORECAST",
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
                    "SETTLE",
                    "PREDICT_EXECUTE",
                    "KNOWLEDGE_INDEX",
                    "HORIZON_LEARN",
                    "PREDICT_VERIFY",
                }
                and error.status >= 500
            ):
                state = "RETRY_WAIT"
            elif attempt.kind == "PRICE_REFRESH" and job["attempts"] < 3 and error.status >= 500:
                state = "RETRY_WAIT"
            elif error.code == "RUNTIME_STOPPING":
                state = "RETRY_WAIT"
            await self.jobs.finish(
                attempt,
                state=state,
                error={"code": error.code},
                next_run_at=now + timedelta(seconds=2 if attempt.kind == "FORECAST" else 60),
            )
            if (
                self.structural
                and attempt.kind in {"PREDICT_PARENT", "PREDICT_TIMEFRAME"}
                and error.code in {"CONTEXT_EXPIRED", "CONTEXT_CHANGED", "POSITION_CHANGED"}
            ):
                async with self.database.transaction() as connection:
                    await self.ownership.guard(connection)
                    if attempt.kind == "PREDICT_PARENT":
                        await self.predictions.queue_parent(
                            connection, self.engine_id, attempt.payload["symbol"], now
                        )
                    else:
                        await self.predictions.queue_timeframe(
                            connection,
                            self.engine_id,
                            attempt.payload["symbol"],
                            attempt.payload["timeframe"],
                            f"refresh:{attempt.id}",
                            now,
                        )
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
                                    "PREDICT_TIMEFRAME",
                                    "PREDICT_PARENT",
                                    "PREDICT_REVIEW",
                                )
                            ),
                            s.jobs.c.state.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
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
                if row["kind"] == "PREDICT_REVIEW":
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
                elif (row["deadline_at"] is not None and row["deadline_at"] < now) or (
                    self.structural and row["kind"] == "FORECAST"
                ):
                    state, reason = "SKIPPED", "FORECAST_DEADLINE"
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
        try:
            await self.ownership.release()
        finally:
            await self.client.close()
            if hasattr(self.resources, "close"):
                await self.resources.close()
            await self.database.close()
        logger.info(
            "runtime_stopped", extra={"runtime_instance_id": str(self.ownership.instance_id)}
        )
