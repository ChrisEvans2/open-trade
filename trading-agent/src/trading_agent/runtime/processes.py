from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import signal
from dataclasses import dataclass
from pathlib import Path

import psutil


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool


def safe_environment(*, extra=None):
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}
    result = {k: v for k, v in os.environ.items() if k in allowed}
    result.update(extra or {})
    return result


def same_process(pid: int, created: float) -> bool:
    try:
        return abs(psutil.Process(pid).create_time() - created) < 0.01
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


async def terminate_owned_orphan(pid, created, *, attempt_id, token_digest, cwd, grace=3):
    """Never signal a reused PID or a process outside the recorded attempt's trust boundary."""

    descendants = []

    root = await asyncio.to_thread(Path(cwd).resolve)

    def owned(process):
        # Inspect cwd before environment, so unrelated process secrets are never read.
        if Path(process.cwd()).resolve() != root:
            return False
        env = process.environ()
        return env.get("TA_ATTEMPT_ID") == str(attempt_id) and hmac.compare_digest(
            hashlib.sha256(env.get("TA_RESEARCH_TOKEN", "").encode()).hexdigest(), token_digest
        )

    def scan_detached():
        uncertain = False
        for candidate in psutil.process_iter(["pid", "create_time"]):
            try:
                if candidate.pid == pid or (
                    created is not None and candidate.info["create_time"] < created
                ):
                    continue
                if candidate.uids().real != os.getuid():
                    continue
                if owned(candidate):
                    descendants.append((candidate.pid, candidate.info["create_time"]))
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError):
                # An inaccessible process cannot be claimed or signalled.
                uncertain = True
        return "DETACHED" if descendants else "UNVERIFIED" if uncertain else "ALREADY_GONE"

    def inspect():
        if created is None:
            # The short-lived leader was already reaped. Claim descendants only by
            # their task token, cwd and uid; never infer identity from its reused PID.
            return scan_detached()
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - created) >= 0.01:
                detached = scan_detached()
                return detached if detached == "DETACHED" else "NOT_OWNED"
            if not owned(process) or os.getpgid(pid) != pid:
                return "NOT_OWNED"
            descendants.extend(
                (child.pid, child.create_time()) for child in process.children(recursive=True)
            )
            return "OWNED"
        except psutil.NoSuchProcess:
            return scan_detached()
        except (psutil.AccessDenied, OSError):
            return "UNVERIFIED"

    status = await asyncio.to_thread(inspect)
    if status not in {"OWNED", "DETACHED"}:
        return status
    if status == "OWNED" and same_process(pid, created):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for child_pid, child_created in descendants:
        if same_process(child_pid, child_created):
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    await asyncio.sleep(grace)
    if status == "OWNED" and same_process(pid, created):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for child_pid, child_created in descendants:
        if same_process(child_pid, child_created):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return "TERMINATED"


async def terminate_group(process, grace=3.0, *, created=None):
    if created is None:
        # No observed birth time means no authority to signal this PID/group.
        if process.returncode is None:
            await asyncio.wait_for(process.wait(), grace)
        return
    # Even if the parent exited, its process group can contain native child processes.
    if (
        psutil.pid_exists(process.pid)
        and created is not None
        and not same_process(process.pid, created)
    ):
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), grace)
        except TimeoutError:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        await asyncio.wait_for(process.wait(), grace)


class ManagedProcess:
    def __init__(self, process, *, limit=16384, kill_grace=3.0, orphan_identity=None):
        self.process, self.limit, self.kill_grace = process, limit, kill_grace
        self.truncated = False
        try:
            self.created = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess:
            self.created = None
        self.orphan_identity = orphan_identity
        self.stdout = asyncio.create_task(self._drain(process.stdout))
        self.stderr = asyncio.create_task(self._drain(process.stderr))

    @classmethod
    async def start(
        cls, argv, *, env=None, cwd=None, input_bytes=None, limit=16384, kill_grace=3.0
    ):
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            cwd=cwd,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE
            if input_bytes is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            identity = None
            if env and env.get("TA_ATTEMPT_ID") and env.get("TA_RESEARCH_TOKEN"):
                identity = {
                    "attempt_id": env["TA_ATTEMPT_ID"],
                    "token_digest": hashlib.sha256(env["TA_RESEARCH_TOKEN"].encode()).hexdigest(),
                    "cwd": cwd or os.getcwd(),
                }
            managed = cls(process, limit=limit, kill_grace=kill_grace, orphan_identity=identity)
        except BaseException:
            await terminate_group(process, kill_grace)
            raise
        if input_bytes is not None:
            try:
                process.stdin.write(input_bytes)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
        return managed

    async def _drain(self, stream):
        result = bytearray()
        while chunk := await stream.read(65536):
            if len(result) + len(chunk) > self.limit:
                self.truncated = True
            result.extend(chunk[: max(0, self.limit - len(result))])
        return bytes(result)

    async def wait(self, wall_seconds):
        try:
            async with asyncio.timeout(wall_seconds):
                await self.process.wait()
                stdout, stderr = await asyncio.gather(self.stdout, self.stderr)
                return ProcessResult(self.process.returncode, stdout, stderr, self.truncated)
        except BaseException:
            await self.terminate()
            raise
        finally:
            # Detached descendants cannot outlive a completed foreground Hermes run.
            await self._terminate_group()

    async def _terminate_group(self):
        await terminate_group(self.process, self.kill_grace, created=self.created)
        if self.created is None and self.orphan_identity:
            await terminate_owned_orphan(
                self.process.pid, None, grace=self.kill_grace, **self.orphan_identity
            )

    async def terminate(self):
        await self._terminate_group()
        for reader in (self.stdout, self.stderr):
            if not reader.done():
                reader.cancel()
        await asyncio.gather(self.stdout, self.stderr, return_exceptions=True)
