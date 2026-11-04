import asyncio

import pytest
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import report, task

from trading_agent.domain.errors import DomainError
from trading_agent.runtime.agent_runner import AgentRunner, RunnerBudget
from trading_agent.runtime.prompts import load_prompts
from trading_agent.runtime.sandboxes import input_files

pytestmark = pytest.mark.postgres


class FixtureResources:
    def __init__(self):
        self.prepared, self.cleaned, self.renewed = [], [], []

    async def renew(self, attempt, remaining):
        assert remaining > 0
        self.renewed.append((attempt.id, remaining))

    async def prepare(self, attempt, root):
        self.prepared.append(attempt.id)
        assert input_files(root)
        folder = root / "input"
        assert folder.is_absolute() and (folder / "prediction-schema.json").exists()
        assert attempt.token not in (folder / "context.json").read_text()

    async def cleanup(self, attempt):
        self.cleaned.append(attempt.id)


def configure_runner(service, tmp_path, code, *, budget=None):
    executable = tmp_path / "fixture-hermes"
    executable.write_text("#!/usr/bin/env python3\n" + code + "\n")
    executable.chmod(0o700)
    service.config = service.config.model_copy(
        update={
            # AgentRunner now launches the pinned adapter through python_executable.
            # Keep this process-lifecycle fixture independent of the native Hermes CLI.
            "hermes": service.config.hermes.model_copy(
                update={"executable": executable, "python_executable": executable}
            ),
            "runtime": service.config.runtime.model_copy(update={"data_dir": tmp_path / "runtime"}),
        }
    )
    resources = FixtureResources()
    return AgentRunner(service.jobs, service, resources, load_prompts(), budget=budget), resources


async def test_stdout_and_zero_exit_cannot_publish_and_attempt_calls_are_deduplicated(
    prediction_system, tmp_path
):
    _, _, _, service, _ = prediction_system
    attempt = await task(prediction_system)
    marker = tmp_path / "executions"
    code = (
        "import pathlib,json,os; "
        f"p=pathlib.Path({str(marker)!r}); p.write_text(p.read_text()+'1' if p.exists() else '1'); "
        "print(json.dumps({'forecast_id':'invented','p_up':1})); print('x'*200000)"
    )
    runner, resources = configure_runner(service, tmp_path, code)
    results = await asyncio.gather(runner.run(attempt), runner.run(attempt), return_exceptions=True)
    assert all(isinstance(r, DomainError) and r.code == "AGENT_NO_FORMAL_RESULT" for r in results)
    assert marker.read_text() == "1"
    assert resources.prepared == resources.cleaned == [attempt.id]
    assert runner.active == {}


async def test_existing_publication_survives_cli_crash_and_avoids_relaunch(
    prediction_system, tmp_path
):
    _, _, _, service, _ = prediction_system
    attempt = await task(prediction_system)
    submission = await report(prediction_system, attempt)
    ident = await service.publish(attempt, submission)
    runner, resources = configure_runner(service, tmp_path, "raise SystemExit(1)")
    assert await runner.run(attempt) == ident
    assert resources.prepared == resources.cleaned == []


async def test_runner_timeout_cleans_process_and_resources(prediction_system, tmp_path):
    _, _, _, service, _ = prediction_system
    attempt = await task(prediction_system)
    runner, resources = configure_runner(
        service,
        tmp_path,
        "import time; time.sleep(60)",
        budget=RunnerBudget(initial_seconds=0.15, total_seconds=0.2),
    )
    with pytest.raises(DomainError) as error:
        await runner.run(attempt)
    assert error.value.code == "AGENT_TIMEOUT"
    assert resources.prepared == resources.cleaned == [attempt.id]
    assert not runner.active


async def test_renewal_failure_stops_running_agent_and_reclaims_sandbox(
    prediction_system, tmp_path
):
    _, _, _, service, _ = prediction_system
    attempt = await task(prediction_system)
    runner, resources = configure_runner(service, tmp_path, "import time; time.sleep(60)")
    # Accelerate the test clock only; production validation disallows this interval.
    runner.config = service.config = service.config.model_copy(
        update={
            "sandbox": service.config.sandbox.model_copy(update={"renew_interval_seconds": 0.03})
        }
    )
    renew = resources.renew

    async def fail_renewal(attempt, remaining):
        if resources.renewed:
            raise DomainError("CUBE_UNAVAILABLE", "fixture renewal failure", 503)
        await renew(attempt, remaining)

    resources.renew = fail_renewal
    with pytest.raises(DomainError) as caught:
        await runner.run(attempt)
    assert caught.value.code == "CUBE_UNAVAILABLE"
    assert resources.prepared == resources.cleaned == [attempt.id]
    assert not runner.active


async def test_running_agent_renews_repeatedly_and_stops_renewing_after_cleanup(
    prediction_system, tmp_path
):
    _, _, _, service, _ = prediction_system
    attempt = await task(prediction_system)
    runner, resources = configure_runner(service, tmp_path, "import time; time.sleep(0.15)")
    runner.config = service.config = service.config.model_copy(
        update={
            "sandbox": service.config.sandbox.model_copy(update={"renew_interval_seconds": 0.03})
        }
    )
    with pytest.raises(DomainError) as caught:
        await runner.run(attempt)
    assert caught.value.code == "AGENT_NO_FORMAL_RESULT"
    assert len(resources.renewed) >= 3
    count = len(resources.renewed)
    assert all(
        a > b for (_, a), (_, b) in zip(resources.renewed, resources.renewed[1:], strict=False)
    )
    await asyncio.sleep(0.06)
    assert len(resources.renewed) == count
    assert resources.cleaned == [attempt.id]
