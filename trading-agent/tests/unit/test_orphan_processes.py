import asyncio
import sys

import psutil
import pytest

from trading_agent.jobs.service import token_hash
from trading_agent.runtime.processes import ManagedProcess, safe_environment, terminate_owned_orphan


async def test_recovery_checks_time_token_and_cwd_before_signalling(tmp_path):
    process = await ManagedProcess.start(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env=safe_environment(
            extra={"TA_ATTEMPT_ID": "fixture-attempt", "TA_RESEARCH_TOKEN": "fixture-token"}
        ),
    )
    options = {
        "attempt_id": "fixture-attempt",
        "token_digest": token_hash("fixture-token"),
        "cwd": tmp_path,
        "grace": 0.05,
    }
    try:
        assert (
            await terminate_owned_orphan(process.process.pid, process.created + 100, **options)
            == "NOT_OWNED"
        )
        invalid = {**options, "token_digest": token_hash("another-token")}
        assert (
            await terminate_owned_orphan(process.process.pid, process.created, **invalid)
            == "NOT_OWNED"
        )
        assert process.process.returncode is None
        assert (
            await terminate_owned_orphan(process.process.pid, process.created, **options)
            == "TERMINATED"
        )
        await process.process.wait()
    finally:
        await process.terminate()


@pytest.mark.parametrize("unknown_birth", [False, True])
async def test_recovery_finds_detached_child_after_original_leader_exits(tmp_path, unknown_birth):
    record = tmp_path / "detached-pid"
    code = (
        "import pathlib,subprocess; "
        f"p=subprocess.Popen([{sys.executable!r},'-c','import time; time.sleep(60)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(record)!r}).write_text(str(p.pid))"
    )
    leader = await ManagedProcess.start(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=safe_environment(
            extra={"TA_ATTEMPT_ID": "detached-attempt", "TA_RESEARCH_TOKEN": "owned"}
        ),
    )
    unrelated = await ManagedProcess.start(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env=safe_environment(
            extra={"TA_ATTEMPT_ID": "detached-attempt", "TA_RESEARCH_TOKEN": "other"}
        ),
    )
    child = None
    try:
        await asyncio.wait_for(leader.process.wait(), 3)
        child = int(record.read_text())
        assert psutil.pid_exists(child)
        assert (
            await terminate_owned_orphan(
                leader.process.pid,
                None if unknown_birth else leader.created,
                attempt_id="detached-attempt",
                token_digest=token_hash("owned"),
                cwd=tmp_path,
                grace=0.1,
            )
            == "TERMINATED"
        )
        await asyncio.sleep(0.05)
        assert (
            not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE
        )
        assert unrelated.process.returncode is None
    finally:
        if (
            child
            and psutil.pid_exists(child)
            and psutil.Process(child).status() != psutil.STATUS_ZOMBIE
        ):
            psutil.Process(child).kill()
        await leader.terminate()
        await unrelated.terminate()
