from __future__ import annotations

import asyncio
import shutil
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import sqlalchemy as sa

from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import canonical_json
from trading_agent.knowledge.contracts import KnowledgeReview
from trading_agent.prediction.contracts import ParentDecision, PredictionReport
from trading_agent.prediction.rounds import coordinated
from trading_agent.prediction.submissions import DecisionContent, PredictionContent
from trading_agent.runtime.deployment import integration_root
from trading_agent.runtime.logging import attempt_event
from trading_agent.runtime.processes import ManagedProcess, safe_environment
from trading_agent.storage import schema as s


@dataclass(frozen=True)
class RunnerBudget:
    initial_seconds: float = 120
    total_seconds: float = 150


class AgentRunner:
    def __init__(
        self,
        jobs,
        predictions,
        resources,
        prompts,
        *,
        budget=None,
        knowledge=None,
    ):
        self.jobs, self.predictions, self.resources, self.prompts = (
            jobs,
            predictions,
            resources,
            prompts,
        )
        self.config = predictions.config
        self.budget = budget or RunnerBudget()
        self.semaphore = asyncio.Semaphore(self.config.runtime.parent_agent_concurrency)
        self.active = {}
        self.executions = OrderedDict()
        self.stopping = False
        self.knowledge = knowledge

    async def _result(self, attempt):
        if coordinated(attempt):
            return await self.predictions.rounds.result(attempt)
        async with self.jobs.database.engine.connect() as connection:
            if attempt.kind in {"PREDICT_REVIEW"}:
                return await connection.scalar(
                    sa.select(s.reviews.c.id)
                    .join(s.job_attempts, s.job_attempts.c.id == s.reviews.c.attempt_id)
                    .where(s.job_attempts.c.job_id == attempt.job_id)
                    .order_by(s.reviews.c.created_at.desc())
                    .limit(1)
                )
            if attempt.kind not in {"PREDICT_TIMEFRAME", "PREDICT_PARENT"}:
                return None
            return await connection.scalar(
                sa.select(s.prediction_facts.c.id).where(
                    s.prediction_facts.c.job_id == attempt.job_id
                )
            )

    async def _inputs(self, attempt):
        root = (self.config.runtime.data_dir / "attempts" / str(attempt.id)).resolve()
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        folder = root / "input"
        folder.mkdir(mode=0o700, exist_ok=True)
        if attempt.kind in {"PREDICT_TIMEFRAME", "PREDICT_PARENT", "PREDICT_REVIEW"}:
            context = (
                await self.knowledge.review_context(attempt)
                if attempt.kind == "PREDICT_REVIEW"
                else await self.predictions.rounds.state(attempt)
                if coordinated(attempt)
                else await self.predictions.context(attempt)
            )
            parent_prompt = (
                "这是独立周期更新任务。读取research_context后调用timeframes_analyze，"
                "适配层只委派本次指定周期的原生子Agent并保存其报告；保存后即可结束。"
                "不要重新分析其他周期，不要发布综合决策，不要覆盖子报告。"
                if attempt.kind == "PREDICT_TIMEFRAME"
                else self.prompts["review" if attempt.kind == "PREDICT_REVIEW" else "parent"]
            )
            for name, contract in (
                ("prediction", PredictionContent if coordinated(attempt) else PredictionReport),
                ("decision", DecisionContent if coordinated(attempt) else ParentDecision),
                ("review", KnowledgeReview),
            ):
                path = folder / f"{name}-schema.json"
                path.write_text(canonical_json(contract.model_json_schema()), encoding="utf-8")
                path.chmod(0o400)
        else:
            raise DomainError("LEGACY_FLOW_RETIRED", "旧预测和复盘流程已归档", 409)
        for role in ("1h", "4h", "6h", "12h", "review"):
            path = folder / f"prompt-{role}.txt"
            path.write_text(self.prompts[role], encoding="utf-8")
            path.chmod(0o400)
        context_path = folder / "context.json"
        context_path.write_text(canonical_json(context), encoding="utf-8")
        context_path.chmod(0o400)
        prompt = folder / "task.txt"
        prompt.write_text(
            parent_prompt + "\n本任务的冻结输入位于/workspace/input；只能使用本次API授权的数据。\n",
            encoding="utf-8",
        )
        prompt.chmod(0o400)
        return root, folder, prompt

    async def _spawn(self, attempt, root, prompt, seconds):
        wall_deadline = time.monotonic() + seconds
        if self.stopping:
            raise DomainError("RUNTIME_STOPPING", "runtime正在关闭", 503)
        executable = self.config.hermes.executable
        python = self.config.hermes.python_executable
        if (
            executable is None
            or not executable.is_absolute()
            or python is None
            or not python.is_absolute()
        ):
            raise DomainError("HERMES_NOT_CONFIGURED", "Hermes绝对路径未配置", 503)
        env = safe_environment(
            extra={
                "TA_RESEARCH_TOKEN": attempt.token,
                "TA_ATTEMPT_ID": str(attempt.id),
                "TA_JOB_ID": str(attempt.job_id),
                "TA_FENCE": str(attempt.fence),
                "TA_API_URL": f"http://127.0.0.1:{self.config.runtime.api_port}/v2",
                "TA_INPUT_DIR": str(root / "input"),
                **({"TA_ANALYSIS_ROUND": "1"} if coordinated(attempt) else {}),
            }
        )
        if hasattr(self.resources, "environment"):
            env.update(await self.resources.environment(attempt))
        if self.config.hermes.home:
            env["HERMES_HOME"] = str(self.config.hermes.home.resolve())
        if hasattr(self.resources, "renew"):
            async with asyncio.timeout(max(0, wall_deadline - time.monotonic())):
                await self.resources.renew(attempt, wall_deadline - time.monotonic())
        try:
            process = await ManagedProcess.start(
                [
                    str(python),
                    str(integration_root() / "run_hermes.py"),
                    str(self.config.hermes.repository),
                    "-p",
                    self.config.hermes.trading_profile,
                    "chat",
                    "--query-file",
                    str(prompt),
                    "--oneshot",
                    "-Q",
                ],
                cwd=root,
                env=env,
                kill_grace=self.config.runtime.kill_grace_seconds,
            )
        except OSError as exc:
            raise DomainError("AGENT_START_FAILED", "Hermes研究进程无法启动", 503) from exc
        self.active[attempt.id] = process
        try:
            async with self.jobs.transaction(attempt) as connection:
                await connection.execute(
                    sa.update(s.job_attempts)
                    .where(s.job_attempts.c.id == attempt.id)
                    .values(
                        process_id=process.process.pid,
                        process_started_at=process.created,
                    )
                )
            attempt_event("agent_process_started", attempt)
            result = await self._wait_with_renewal(process, attempt, wall_deadline)
            attempt_event(
                "agent_process_finished",
                attempt,
                output_bytes=len(result.stdout) + len(result.stderr),
            )
            return result
        finally:
            await process.terminate()
            self.active.pop(attempt.id, None)
            attempt_event("agent_process_cleanup", attempt)

    async def _wait_with_renewal(self, process, attempt, deadline):
        if not hasattr(self.resources, "renew") and not coordinated(attempt):
            return await process.wait(max(0, deadline - time.monotonic()))

        async def maintain():
            last_renewal = time.monotonic()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                await asyncio.sleep(
                    min(
                        1 if coordinated(attempt) else self.config.sandbox.renew_interval_seconds,
                        remaining,
                    )
                )
                if process.process.returncode is not None:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if coordinated(attempt):
                    phase = await self.predictions.rounds.phase_remaining(attempt)
                    if phase is not None and phase <= 0:
                        raise TimeoutError
                if (
                    hasattr(self.resources, "renew")
                    and time.monotonic() - last_renewal
                    >= self.config.sandbox.renew_interval_seconds
                ):
                    async with asyncio.timeout(remaining):
                        await self.resources.renew(attempt, remaining)
                    last_renewal = time.monotonic()

        waiter = asyncio.create_task(process.wait(max(0, deadline - time.monotonic())))
        renewal = asyncio.create_task(maintain())
        try:
            done, _ = await asyncio.wait((waiter, renewal), return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal  # Propagate renewal failure; _spawn then terminates the process.
            return await waiter
        finally:
            for task in (renewal, waiter):
                task.cancel()
            await asyncio.gather(renewal, waiter, return_exceptions=True)

    async def run(self, attempt):
        task = self.executions.get(attempt.id)
        if task is None:
            task = asyncio.create_task(self._run(attempt), name=f"agent:{attempt.id}")
            self.executions[attempt.id] = task
            while len(self.executions) > 512:
                oldest, record = next(iter(self.executions.items()))
                if not record.done():
                    break
                self.executions.pop(oldest)
        return await task

    async def _run(self, attempt):
        async with self.semaphore:
            existing = await self._result(attempt)
            if existing:
                return existing
            if attempt.id in self.active:
                raise DomainError("ATTEMPT_ALREADY_RUNNING", "该attempt已有研究进程", 409)
            if coordinated(attempt):
                return await self._run_round(attempt)
            root, folder, prompt = await self._inputs(attempt)
            try:
                async with self.jobs.transaction(attempt) as connection:
                    now = await self.jobs.database.now(connection)
                preparation_budget = 20.0
                try:
                    async with asyncio.timeout(max(0, preparation_budget)):
                        await self.resources.prepare(attempt, root)
                except TimeoutError:
                    raise DomainError(
                        "SANDBOX_PREPARE_TIMEOUT", "沙箱准备超过启动预算", 503
                    ) from None
                attempt_event("sandbox_prepared", attempt)
                async with self.jobs.transaction(attempt) as connection:
                    _, record, now = await self.jobs.guard(connection, attempt)
                    if record["model_started_at"] is None:
                        await connection.execute(
                            sa.update(s.job_attempts)
                            .where(s.job_attempts.c.id == attempt.id)
                            .values(
                                model_started_at=now,
                                token_expires_at=now + timedelta(seconds=self.budget.total_seconds),
                            )
                        )
                async with self.jobs.transaction(attempt) as connection:
                    now = await self.jobs.database.now(connection)
                    started_at = await connection.scalar(
                        sa.select(s.job_attempts.c.model_started_at).where(
                            s.job_attempts.c.id == attempt.id
                        )
                    )
                remaining = min(
                    self.budget.total_seconds - (now - started_at).total_seconds(),
                    (attempt.deadline_at - now).total_seconds()
                    if attempt.deadline_at
                    else float("inf"),
                )
                if remaining <= 0:
                    raise DomainError("AGENT_TIMEOUT", "研究任务总时限已用尽", 409)
                timed_out = False
                try:
                    await self._spawn(
                        attempt, root, prompt, min(self.budget.initial_seconds, remaining)
                    )
                except TimeoutError:
                    timed_out = True
                    attempt_event("agent_timeout", attempt, error_code="AGENT_TIMEOUT")
                result = await self._result(attempt)
                if result:
                    return result
                rejection = await self.jobs.last_submission_error(attempt)
                if rejection:
                    raise DomainError(
                        rejection,
                        "正式预测提交被拒绝，具体原因已保留",
                        503 if rejection == "MINUTE_DATA_PENDING" else 409,
                    )
                if timed_out:
                    raise DomainError("AGENT_TIMEOUT", "研究任务超时且未保存正式预测", 503)
                raise DomainError("AGENT_NO_FORMAL_RESULT", "研究进程未保存正式预测", 503)
            finally:
                cleaned = await self.resources.cleanup(attempt)
                attempt_event(
                    "sandbox_cleanup",
                    attempt,
                    cleanup_status="PENDING" if cleaned is False else "CLEANED",
                )
                if cleaned is False:
                    raise DomainError("SANDBOX_CLEANUP_PENDING", "沙箱资源清理尚未确认", 503)
                await asyncio.to_thread(shutil.rmtree, root)

    async def _run_round(self, attempt):
        rounds = self.predictions.rounds
        root, _, prompt = await self._inputs(attempt)
        try:
            while True:
                await rounds.launch(attempt)
                try:
                    async with asyncio.timeout(20):
                        await self.resources.prepare(attempt, root)
                    async with self.jobs.transaction(attempt) as c:
                        now = await self.jobs.database.now(c)
                        await c.execute(
                            sa.update(s.job_attempts)
                            .where(
                                s.job_attempts.c.id == attempt.id,
                                s.job_attempts.c.model_started_at.is_(None),
                            )
                            .values(model_started_at=now)
                        )
                    remaining = (attempt.deadline_at - now).total_seconds()
                    if remaining <= 0:
                        raise DomainError("ANALYSIS_DEADLINE", "轮次期限已用尽", 409)
                    code = None
                    try:
                        await self._spawn(attempt, root, prompt, remaining)
                    except TimeoutError:
                        code = "AGENT_TIMEOUT"
                    result = await rounds.result(attempt)
                    if result:
                        return result
                    async with self.jobs.database.engine.connect() as c:
                        specific = await c.scalar(
                            sa.select(s.analysis_runs.c.error_code)
                            .join(
                                s.analysis_rounds,
                                s.analysis_rounds.c.id == s.analysis_runs.c.round_id,
                            )
                            .where(
                                s.analysis_runs.c.round_id
                                == UUID(attempt.payload["analysis_round"]),
                                s.analysis_runs.c.launch_index
                                == sa.func.coalesce(
                                    s.analysis_rounds.c.counts["coordinator"].as_integer(), 0
                                ),
                                s.analysis_runs.c.error_code.is_not(None),
                            )
                            .order_by(s.analysis_runs.c.started_at.desc())
                            .limit(1)
                        )
                    code = specific or code
                    code = code or "AGENT_NO_FORMAL_RESULT"
                    await rounds.interrupt_runs(attempt, code)
                    state = await rounds.state(attempt)
                    roles = state["needed"] or ["parent"]
                    if any(state["counts"].get(role, 0) >= state["limit"] for role in roles):
                        raise DomainError(code, "所需角色分析失败且重试已耗尽", 503)
                finally:
                    cleaned = await self.resources.cleanup(attempt)
                    if cleaned is False:
                        raise DomainError(
                            "SANDBOX_CLEANUP_PENDING", "回收未确认，名额继续隔离", 503
                        )
                await asyncio.sleep(5 if state["counts"].get("coordinator", 0) < 2 else 15)
        finally:
            await asyncio.to_thread(shutil.rmtree, root, True)

    async def stop(self):
        self.stopping = True
        for task in self.executions.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*(process.terminate() for process in tuple(self.active.values())))
        await asyncio.gather(*self.executions.values(), return_exceptions=True)
