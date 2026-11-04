from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from legacy_research.compat.contracts import (
    IterationRequest,
    iteration_payload,
    project_path,
    strategy_path,
)
from legacy_research.compat.evidence import load_snapshot, storage_root, write_once
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class TestResult:
    exit_code: int
    output: str
    command: tuple[str, ...]
    sandbox_id: str


class Journal:
    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, mode=0o700, exist_ok=True)

    def append(self, status, **data):
        number = len(list(self.root.glob("event-*.json")))
        record = {"status": status, "at": datetime.now(UTC), **data}
        write_once(self.root / f"event-{number:04d}.json", record)
        return record

    def events(self):
        return [json.loads(path.read_text()) for path in sorted(self.root.glob("event-*.json"))]

    def result(self):
        results = [event["result"] for event in self.events() if "result" in event]
        return results[-1] if results else None


@contextmanager
def iteration_lock(policy):
    lock = storage_root(policy) / "iteration.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DomainError("LEGACY_ITERATION_BUSY", "另一个兼容迭代正在执行", 409) from None
        yield
    finally:
        os.close(fd)


def source_file(policy, name):
    if not strategy_path(name):
        raise DomainError("LEGACY_PATH_FORBIDDEN", "模型只能读取授权策略源码", 403)
    path = project_path(policy.repository, name)
    if path.stat().st_size > 100_000:
        raise DomainError("LEGACY_SOURCE_TOO_LARGE", "策略文件超过读取限额", 413)
    data = path.read_bytes()
    return {"path": name, "sha256": file_hash(data), "content": data.decode()}


def source_bundle(policy):
    result = subprocess.run(
        [
            "git",
            "-C",
            str(policy.repository),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode:
        raise DomainError("LEGACY_REPOSITORY_INVALID", "兼容迭代需要已登记的Git项目")
    files, total = {}, 0
    safe_configs = {
        "config/app.yaml",
        "config/risk.yaml",
        "config/strategy.yaml",
        "config/symbols.yaml",
        "config/runtime_state.yaml",
    }
    for name in result.stdout.decode().split("\0"):
        allowed = (
            (
                name.endswith(".py")
                and name.startswith(("src/", "tests/", "packages/trading-quant/src/"))
            )
            or name in safe_configs
            or name == "scripts/replay_advisor_veto.py"
            or name == "pyproject.toml"
        )
        if not name or not allowed:
            continue
        path = project_path(policy.repository, name)
        if not path.is_file():
            raise DomainError("LEGACY_SOURCE_CHANGED", "登记的测试或源码文件不存在", 409)
        total += path.stat().st_size
        if total > policy.max_source_bytes:
            raise DomainError("LEGACY_SOURCE_TOO_LARGE", "候选测试源码超过限额", 413)
        files[name] = path.read_bytes().decode("utf-8")
    if not any(name.startswith("tests/") for name in files):
        raise DomainError("LEGACY_TESTS_MISSING", "旧项目没有可用的原有测试")
    return files


def _current(policy, name):
    path = project_path(policy.repository, name)
    return path.read_bytes() if path.exists() else None


def _replace(policy, name, data):
    path = project_path(policy.repository, name)
    if data is None:
        if path.exists():
            path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            tmp = Path(temporary.name)
        try:
            # Check again before replacing, so a symlink cannot be applied as a strategy file.
            project_path(policy.repository, name)
            tmp.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o644)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def recover_files(policy, journal):
    events = journal.events()
    if not any(event["status"] == "APPLYING" for event in events) or journal.result():
        return
    plan = json.loads((journal.root / "plan.json").read_text())
    for change in plan["changes"]:
        before = change["before"].encode() if change["before"] is not None else None
        after = change["after"].encode()
        current = _current(policy, change["path"])
        if current not in (before, after):
            raise DomainError(
                "LEGACY_RECOVERY_CONFLICT", "中断后策略又被修改，需先核对恢复记录", 409
            )
    for change in reversed(plan["changes"]):
        if _current(policy, change["path"]) != (
            change["before"].encode() if change["before"] is not None else None
        ):
            _replace(
                policy,
                change["path"],
                change["before"].encode() if change["before"] is not None else None,
            )
    request = IterationRequest.model_validate(
        json.loads((journal.root / "request.json").read_text())["request"]
    )
    tested = next(event for event in reversed(events) if event["status"] == "TESTED")
    test = TestResult(
        tested["exit_code"], tested["output"], tuple(tested["command"]), tested["sandbox_id"]
    )
    record = _record_markdown(policy, journal, request, test, "ROLLED_BACK")
    result = {
        "iteration_id": plan["iteration_id"],
        "status": "ROLLED_BACK",
        "reason": "进程中断后恢复原始文件",
        "record_path": record,
    }
    journal.append("ROLLED_BACK", result=result)


def _record_markdown(policy, journal, request, test, status):
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M")
    # The stable suffix avoids overwriting another iteration made in the same minute.
    name = f"Vone-Strategy-Iteration-{stamp}-{request.iteration_id.hex[:8]}-{status.lower()}.md"
    path = project_path(policy.repository, "Strategy-iteration/" + name)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 新v1兼容策略迭代",
        "",
        f"状态：{status}",
        f"证据快照：{request.evidence_snapshot_id}",
        f"改进假设：{request.hypothesis}",
        f"风险与副作用：{request.risk}",
        f"回滚方案：{request.rollback}",
        *(
            [
                f"冻结经验清单：{request.memory_recall_id}",
                "引用经验：" + ", ".join(map(str, request.memory_experience_ids)),
            ]
            if request.memory_recall_id
            else []
        ),
        "",
        "修改文件：",
        *(f"- {item.path}" for item in request.changes),
        "",
        f"测试命令：{' '.join(test.command)}",
        f"测试退出码：{test.exit_code}",
        f"沙箱ID：{test.sandbox_id}",
        f"完整补丁与原文件：{journal.root / 'plan.json'}",
        "",
        "测试通过只证明该测试集通过，不证明收益改善；没有部署、重启或提交订单。",
        "",
        "```text",
        test.output.replace("```", "` ` `")[:16000],
        "```",
        "",
    ]
    content = "\n".join(lines)
    if path.exists():
        if path.read_text() != content:
            raise DomainError("LEGACY_RECORD_CONFLICT", "已有改动记录不能覆盖", 409)
        return str(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return str(path)


class IterationService:
    def __init__(self, policy, verifier):
        self.policy, self.verifier = policy, verifier

    async def run(self, request: IterationRequest):
        policy = self.policy
        with iteration_lock(policy):
            root = storage_root(policy) / "iterations"
            root.mkdir(mode=0o700, exist_ok=True)
            for folder in sorted(root.iterdir()):
                if folder.is_symlink():
                    raise DomainError("LEGACY_PATH_FORBIDDEN", "兼容记录目录不能是符号链接", 403)
                if folder.is_dir():
                    recover_files(policy, Journal(folder))
            journal = Journal(root / str(request.iteration_id))
            request_hash = content_hash(iteration_payload(request))
            request_path = journal.root / "request.json"
            if request_path.exists():
                if json.loads(request_path.read_text())["hash"] != request_hash:
                    raise DomainError(
                        "LEGACY_IDEMPOTENCY_CONFLICT", "同一迭代ID不能提交不同补丁", 409
                    )
                if result := journal.result():
                    return result
                raise DomainError(
                    "LEGACY_ITERATION_INTERRUPTED", "先核对未完成记录；使用新ID重新发起", 409
                )
            snapshot = load_snapshot(policy, request.evidence_snapshot_id)
            memory = None
            if request.memory_recall_id:
                from legacy_research.compat.experiences import ExperienceStore

                memory = ExperienceStore(policy).references(
                    request.memory_recall_id, request.memory_experience_ids
                )
            if not any(
                group["metrics"]["closed_trade_count"] for group in snapshot["groups"].values()
            ):
                raise DomainError(
                    "LEGACY_EVIDENCE_INSUFFICIENT", "没有闭环成交证据，不能执行策略迭代", 409
                )
            files = source_bundle(policy)
            before_files = dict(files)
            plan = []
            for change in request.changes:
                current = _current(policy, change.path)
                if change.path.startswith("tests/") and current is not None:
                    raise DomainError(
                        "LEGACY_TEST_POLICY_FORBIDDEN", "原有测试不能由迭代模型修改", 403
                    )
                if strategy_path(change.path) and current is None:
                    raise DomainError("LEGACY_PATH_FORBIDDEN", "策略修改须针对已有登记源码", 403)
                if current is not None and change.path not in files:
                    raise DomainError("LEGACY_PATH_FORBIDDEN", "策略文件未登记在Git项目", 403)
                if (file_hash(current) if current is not None else None) != change.before_sha256:
                    raise DomainError(
                        "LEGACY_SOURCE_CHANGED", "策略文件已变化，补丁原始hash不匹配", 409
                    )
                ast.parse(change.content, filename=change.path)
                files[change.path] = change.content
                plan.append(
                    {
                        "path": change.path,
                        "before": current.decode() if current is not None else None,
                        "after": change.content,
                    }
                )
            write_once(request_path, {"hash": request_hash, "request": request})
            if memory is not None:
                write_once(journal.root / "memory.json", memory)
            write_once(
                journal.root / "plan.json", {"iteration_id": request.iteration_id, "changes": plan}
            )
            journal.append(
                "PREPARED",
                evidence_hash=snapshot["content_hash"],
                baseline_hash=content_hash(before_files),
                candidate_hash=content_hash(files),
            )
            try:
                journal.append("TESTING")
                test = await self.verifier.run(files, policy, journal)
            except BaseException:
                journal.append("TEST_UNCONFIRMED")
                raise
            if test.exit_code != 0:
                status = "TEST_FAILED"
            elif not policy.apply_changes:
                status = "VERIFIED_CANDIDATE"
            else:
                status = "APPLIED"
            # Freeze the trusted test outcome before any source replacement.
            journal.append(
                "TESTED",
                exit_code=test.exit_code,
                sandbox_id=test.sandbox_id,
                command=test.command,
                output=test.output[:16000],
            )
            if content_hash(source_bundle(policy)) != content_hash(before_files):
                journal.append("SOURCE_CONFLICT")
                raise DomainError(
                    "LEGACY_SOURCE_CHANGED", "测试期间源码集合发生变化，拒绝写回", 409
                )
            for name, content in before_files.items():
                if _current(policy, name) != content.encode():
                    journal.append("SOURCE_CONFLICT")
                    raise DomainError(
                        "LEGACY_SOURCE_CHANGED", "测试期间旧源码或测试发生变化，拒绝写回", 409
                    )
            for change in plan:
                before = change["before"].encode() if change["before"] is not None else None
                if _current(policy, change["path"]) != before:
                    raise DomainError("LEGACY_SOURCE_CHANGED", "测试期间目标文件发生变化", 409)
            if status == "APPLIED":
                journal.append("APPLYING")
                try:
                    for change in plan:
                        _replace(policy, change["path"], change["after"].encode())
                    record = _record_markdown(policy, journal, request, test, status)
                except BaseException:
                    recover_files(policy, journal)
                    raise
            else:
                record = _record_markdown(policy, journal, request, test, status)
            result = {
                "iteration_id": str(request.iteration_id),
                "status": status,
                "test_exit_code": test.exit_code,
                "record_path": record,
                "deployed": False,
            }
            journal.append(status, result=result)
            return result
