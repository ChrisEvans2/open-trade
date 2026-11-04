import asyncio
import sys

import psutil
import pytest

from trading_agent.runtime.processes import ManagedProcess, safe_environment


async def test_large_stdout_and_stderr_are_drained_and_bounded():
    command = "import sys; sys.stdout.write('x'*2000000); sys.stderr.write('y'*2000000)"
    process = await ManagedProcess.start([sys.executable, "-c", command], limit=4096)
    result = await process.wait(5)
    assert result.returncode == 0 and result.truncated
    assert len(result.stdout) == len(result.stderr) == 4096


async def test_timeout_kills_group_including_child_and_does_not_block_loop(tmp_path):
    record = tmp_path / "child-pid"
    code = (
        "import subprocess,time,pathlib; "
        f"p=subprocess.Popen([{sys.executable!r},'-c','import time;time.sleep(60)']); "
        f"pathlib.Path({str(record)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    process = await ManagedProcess.start([sys.executable, "-c", code], kill_grace=0.2)
    beats = []

    async def heartbeat():
        for _ in range(10):
            beats.append(True)
            await asyncio.sleep(0.02)

    beat = asyncio.create_task(heartbeat())
    with pytest.raises(TimeoutError):
        await process.wait(0.3)
    await beat
    assert len(beats) == 10 and process.process.returncode is not None
    child = int(record.read_text())
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


def test_host_credentials_are_not_inherited(monkeypatch):
    for key in (
        "TA_DATABASE_URL",
        "TA_OPERATOR_TOKEN",
        "TA_CONTROLLER_TOKEN",
        "PGPASSWORD",
        "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(key, "fixture-secret")
    env = safe_environment(extra={"TA_RESEARCH_TOKEN": "fixture-attempt"})
    assert env["TA_RESEARCH_TOKEN"] == "fixture-attempt"
    assert "TA_OPERATOR_TOKEN" not in env and "PGPASSWORD" not in env
    assert "OPENAI_API_KEY" not in env


async def test_already_reaped_short_process_keeps_result_without_signalling_unknown_pid(
    monkeypatch,
):
    spawn = asyncio.create_subprocess_exec

    async def already_finished(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        await process.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", already_finished)
    process = await ManagedProcess.start([sys.executable, "-c", "print('short-lived')"])
    assert process.created is None

    def forbidden_signal(*args):
        raise AssertionError("Unknown process identity must never authorize a group signal")

    monkeypatch.setattr("trading_agent.runtime.processes.os.killpg", forbidden_signal)
    result = await process.wait(1)
    assert result.returncode == 0 and result.stdout == b"short-lived\n"
    await process.terminate()
