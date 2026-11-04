import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_agent.config import AppConfig, HermesConfig
from trading_agent.domain.errors import DomainError
from trading_agent.runtime import deployment
from trading_agent.runtime.deployment import DeploymentCheck
from trading_agent.runtime.processes import ProcessResult


@pytest.mark.parametrize("outcome", ["ready", "invalid", "timeout", "start_failure"])
async def test_credential_probe_is_bounded_cached_and_fails_closed(monkeypatch, outcome):
    calls = []
    terminated = []

    class Process:
        async def wait(self, seconds):
            assert seconds == deployment.MODEL_CHECK_TIMEOUT_SECONDS
            if outcome == "timeout":
                raise TimeoutError
            payload = json.dumps({"configured": True}).encode() if outcome == "ready" else b"bad"
            return ProcessResult(0, payload, b"", False)

        async def terminate(self):
            terminated.append(True)

    async def start(*args, **kwargs):
        calls.append(True)
        assert "TA_DATABASE_URL" not in kwargs["env"]
        if outcome == "start_failure":
            raise OSError("fixture startup failure")
        return Process()

    monkeypatch.setattr(deployment.ManagedProcess, "start", start)
    config = AppConfig(hermes=HermesConfig(python_executable=Path(sys.executable)))
    check = DeploymentCheck(config, None)
    try:
        results = await asyncio.gather(check.check_model(), check.check_model())
        assert results == [outcome == "ready"] * 2
        assert len(calls) == 1
        assert bool(terminated) is (outcome != "start_failure")
    finally:
        await check.close()


async def test_readiness_refreshes_in_background_without_extending_cache(monkeypatch):
    clock = SimpleNamespace(monotonic=lambda: 100)
    monkeypatch.setattr(deployment, "time", clock)

    @asynccontextmanager
    async def connect():
        yield None

    async def permissions(connection):
        return True

    monkeypatch.setattr(deployment, "verify_app_permissions", permissions)
    database = SimpleNamespace(engine=SimpleNamespace(connect=connect))
    check = DeploymentCheck(AppConfig(), database)
    check.files = lambda: True
    check.git_checked = True
    check.model_ready = True
    check.model_checked_at = 79
    entered, canceled = asyncio.Event(), asyncio.Event()

    async def refresh():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    check._check_model = refresh
    try:
        assert await asyncio.wait_for(check.ready(), 0.1)
        await entered.wait()
        original_task = check.model_refresh
        assert await check.ready()
        assert check.model_refresh is original_task
        # Expired success cannot keep readiness green while a probe is pending.
        check.model_checked_at = 69
        assert not await check.ready()
        check.model_checked_at = 99

        def changed_files():
            raise DomainError("DEPLOYMENT_LOCK_MISMATCH", "fixture lock changed", 503)

        check.files = changed_files
        assert not await check.ready()
    finally:
        await check.close()
    assert canceled.is_set()


async def test_successful_start_keeps_refreshing_without_readiness_requests(monkeypatch):
    @asynccontextmanager
    async def connect():
        yield None

    async def permissions(connection):
        return True

    monkeypatch.setattr(deployment, "verify_app_permissions", permissions)
    monkeypatch.setattr(deployment, "MODEL_REFRESH_SECONDS", 0.01)
    check = DeploymentCheck(AppConfig(), SimpleNamespace(engine=SimpleNamespace(connect=connect)))
    check.files = lambda: True
    check.git_checked = True
    refreshed = asyncio.Event()

    async def probe(*, force=False):
        check.model_ready = True
        check.model_checked_at = deployment.time.monotonic()
        if force:
            refreshed.set()
        return True

    check.check_model = probe
    try:
        assert await check()
        await asyncio.wait_for(refreshed.wait(), 0.5)
        assert await check.ready()
    finally:
        await check.close()
    assert check.model_refresh.done()
