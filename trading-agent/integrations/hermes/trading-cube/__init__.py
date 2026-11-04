import hashlib
import importlib.metadata
import json
import os
import shlex
import stat
import threading
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx
from agent.terminal_env_provider import TerminalEnvironmentProvider
from tools.environments.base import BaseEnvironment
from tools.environments.base_output import _ThreadedProcessHandle


def kill_owned_sandbox(record):
    # SDK 0.7.0 kill() has no HTTP timeout. Use a bounded control request instead.
    config = record["config"]
    ident = quote(record["sandbox"]["sandboxID"], safe="")
    with httpx.Client(
        base_url=config["api_url"],
        headers={"X-API-Key": config["api_key"]},
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(min(config["request_timeout"], 8), connect=3),
    ) as client:
        response = client.get("/sandboxes/" + ident)
        if response.status_code == 404:
            return
        response.raise_for_status()
        metadata = response.json().get("metadata") or {}
        if any(metadata.get(key) != value for key, value in record["sandbox"]["metadata"].items()):
            raise PermissionError("sandbox ownership changed; cancellation refused")
        response = client.delete("/sandboxes/" + ident)
        if response.status_code != 404:
            response.raise_for_status()


class CubeEnvironment(BaseEnvironment):
    _stdin_mode = "payload"

    def __init__(self, sandbox, settings, timeout, cancel, task_id="default"):
        self.sandbox = sandbox
        self.cancel = cancel
        self.user = settings["execution_user"]
        self.max_output = settings["max_output_bytes"]
        self.max_processes = settings["max_processes"]
        self.workdir = (
            "/workspace/scratch/session-" + hashlib.sha256(str(task_id).encode()).hexdigest()[:24]
        )
        sandbox.commands.run(
            "mkdir -p -m700 " + shlex.quote(self.workdir), user=self.user, timeout=5
        )
        super().__init__(cwd=self.workdir, timeout=min(timeout, 120), env={})
        self.init_session()

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        tag = uuid4().hex
        command_dir = self.workdir + "/.command-" + tag
        cancelled = threading.Event()

        def cancel_command():
            cancelled.set()
            # A random command-owned cancellation file avoids PID reuse and never
            # destroys the shared sandbox or a sibling's command.
            self.sandbox.commands.run(
                "test ! -d "
                + shlex.quote(command_dir)
                + " || touch "
                + shlex.quote(command_dir + "/cancel"),
                user=self.user,
                timeout=5,
            )

        def execute():
            self.sandbox.commands.run(
                "mkdir -m700 " + shlex.quote(command_dir), user=self.user, timeout=5
            )
            if cancelled.is_set():
                self.sandbox.commands.run(
                    "rmdir " + shlex.quote(command_dir), user=self.user, timeout=5
                )
                return "cancelled before launch", 130
            command = cmd_string
            if stdin_data is not None:
                path = command_dir + "/stdin"
                self.sandbox.files.write(path, stdin_data, user=self.user)
                command = f"({command}) < {shlex.quote(path)}"
            shell = "bash " + ("-l " if login else "") + "-c " + shlex.quote(command)
            # This process owns and reaps the child, so its PID cannot be reused
            # while a cancellation signal is being sent to that process group.
            supervisor = """
import os, signal, subprocess, sys, time
from pathlib import Path
folder, command, seconds, cap = sys.argv[1:]
folder = Path(folder)
with (folder / 'output').open('wb') as output:
    child = subprocess.Popen(command, shell=True, executable='/bin/bash',
        stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    until = time.monotonic() + float(seconds)
    try:
        while os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            if (folder / 'cancel').exists() or time.monotonic() >= until:
                os.killpg(child.pid, signal.SIGKILL)
                break
            time.sleep(.05)
    finally:
        # waitid(WNOWAIT) observes completion without reaping, retaining the PID
        # until this owner finishes group cancellation. No PID reuse window.
        try: os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        child.wait()
        sys.stdout.buffer.write((folder / 'output').read_bytes()[:int(cap)])
sys.exit(child.returncode if child.returncode >= 0 else 128 - child.returncode)
"""
            wrapped = (
                f"ulimit -u {self.max_processes}; ulimit -f {(self.max_output + 1023) // 1024}; "
                "python3 -c "
                + shlex.quote(supervisor)
                + " "
                + shlex.quote(command_dir)
                + " "
                + shlex.quote(shell)
                + f" {max(1, min(timeout, 120))} {self.max_output}; rc=$?; rm -rf -- "
                + shlex.quote(command_dir)
                + "; exit $rc"
            )
            result = self.sandbox.commands.run(
                wrapped,
                cwd=self.workdir,
                user=self.user,
                envs={},
                timeout=max(1, min(timeout, 120)) + 8,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return output[: self.max_output], result.exit_code

        return _ThreadedProcessHandle(execute, cancel_fn=cancel_command)

    def cleanup(self):
        self.sandbox.close()

    def fetch_file(self, remote_path, local_dest, max_bytes=1_048_576):
        resolved = self.fetch_realpath(remote_path)
        if not resolved or not resolved.startswith("/workspace/scratch/"):
            raise PermissionError("file bridge is restricted to attempt scratch")
        return super().fetch_file(resolved, local_dest, max_bytes=min(max_bytes, self.max_output))


class CubeProvider(TerminalEnvironmentProvider):
    name = "cubesandbox"
    display_name = "Trading CubeSandbox"
    is_remote = True
    is_container = True
    session_isolated_when_nonpersistent = True

    @property
    def cache_path_base(self):
        # Never synchronize Hermes home/cache into the guest.
        return None

    @property
    def strip_env_keys(self):
        return frozenset(
            {
                "TA_RESEARCH_TOKEN",
                "TA_CONTROLLER_TOKEN",
                "TA_OPERATOR_TOKEN",
                "TA_CUBE_SANDBOX_FILE",
                "TA_INPUT_DIR",
                "TA_DATABASE_URL",
                "TA_MIGRATION_DATABASE_URL",
                "CUBE_API_KEY",
                "CUBE_API_URL",
                "TA_ATTEMPT_ID",
                "TA_JOB_ID",
                "TA_FENCE",
            }
        )

    def is_available(self):
        try:
            return (
                bool(os.environ.get("TA_CUBE_SANDBOX_FILE"))
                and importlib.metadata.version("cubesandbox") == "0.7.0"
            )
        except importlib.metadata.PackageNotFoundError:
            return False

    def create_environment(
        self, *, cwd, timeout, task_id="default", image=None, container_config=None, **kwargs
    ):
        from cubesandbox import Config, Sandbox

        if not self.is_available():
            raise RuntimeError("attempt-owned CubeSandbox is unavailable; local fallback forbidden")
        path = Path(os.environ["TA_CUBE_SANDBOX_FILE"])
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
                raise PermissionError("unsafe sandbox credential file")
            record = json.load(handle)
        metadata = record["sandbox"]["metadata"]
        if any(
            metadata[key] != os.environ[name]
            for key, name in (
                ("attempt_id", "TA_ATTEMPT_ID"),
                ("job_id", "TA_JOB_ID"),
                ("fence", "TA_FENCE"),
            )
        ):
            raise PermissionError("sandbox belongs to another attempt")
        settings = record["settings"]
        if settings["execution_user"] in {"root", "0"}:
            raise PermissionError("ordinary execution user required")
        sandbox = Sandbox(record["sandbox"], Config(**record["config"]))
        return CubeEnvironment(
            sandbox, settings, timeout, lambda: kill_owned_sandbox(record), task_id
        )


def register(ctx):
    ctx.register_terminal_environment_provider(CubeProvider())
