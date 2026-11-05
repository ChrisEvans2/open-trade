from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa
from test_analysis_rounds import advance, four, publish, start
from test_analysis_rounds import round_system as round_system
from test_cube_lifecycle import CubeAPI
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_runner import configure_runner

from trading_agent.config import SandboxConfig
from trading_agent.domain.errors import DomainError
from trading_agent.runtime.sandboxes import CubeResources
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


def resources_for_round(x, tmp_path):
    config = x.predictions.config.model_copy(
        update={
            "runtime": x.predictions.config.runtime.model_copy(
                update={"data_dir": tmp_path / "runtime"}
            ),
            "sandbox": SandboxConfig(
                base_url="http://127.0.0.1:3000",
                template="fixture-template",
                sdk_version="0.7.0",
                proxy_node_ip="127.0.0.1",
            ),
        }
    )
    api = CubeAPI()
    resources = CubeResources(
        config,
        "fixture-cube-key",
        client=httpx.AsyncClient(
            base_url="http://127.0.0.1:3000", transport=httpx.MockTransport(api)
        ),
    )
    resources.jobs = x.jobs
    resources._populate = lambda raw, root: None  # fixture transport, not guest isolation
    return resources, api


async def test_round_survives_150_seconds_but_parent_still_has_its_own_deadline(
    round_system, tmp_path
):
    x = round_system
    main = await start(x)
    await x.rounds.launch(main)
    resources, api = resources_for_round(x, tmp_path)
    try:
        await resources.prepare(main, tmp_path)
        async with x.jobs.transaction(main) as c:
            await c.execute(
                sa.update(s.job_attempts)
                .where(s.job_attempts.c.id == main.id)
                .values(model_started_at=x.clock.value)
            )
        await advance(x, 200, main)
        await resources.renew(main, 1000)
        assert api.renewed[-1][1] == resources.config.sandbox.idle_timeout_seconds
        await four(x, main)
        parent = await x.rounds.begin(main, "parent")
        assert 119 <= await x.rounds.phase_remaining(main) <= 120
        await advance(x, 121, main)
        assert await x.rounds.phase_remaining(main) < 0
        with pytest.raises(DomainError, match="超过执行期限"):
            await x.rounds.complete_execution(main, parent["run"])
        assert await resources.cleanup(main)
    finally:
        await resources.close()


async def test_normal_task_budget_still_rejects_renewal_past_150_seconds(round_system, tmp_path):
    x = round_system
    resources, api = resources_for_round(x, tmp_path)
    try:
        async with x.db.transaction() as c:
            await x.jobs.create(c, "PREDICT_REVIEW", "fixture:ordinary", {}, x.clock.value)
        attempt = await x.jobs.claim(("PREDICT_REVIEW",))
        await resources.prepare(attempt, tmp_path)
        async with x.jobs.transaction(attempt) as c:
            await c.execute(
                sa.update(s.job_attempts)
                .where(s.job_attempts.c.id == attempt.id)
                .values(model_started_at=x.clock.value - timedelta(seconds=160))
            )
        with pytest.raises(DomainError) as error:
            await resources.renew(attempt, 90)
        assert error.value.code == "AGENT_TIMEOUT" and not api.renewed
        assert await resources.cleanup(attempt)
    finally:
        await resources.close()


async def test_real_cube_lifecycle_restarts_parent_after_error_without_restarting_valid_children(
    round_system, tmp_path
):
    x = round_system
    main = await start(x)
    runner, _ = configure_runner(x.predictions, tmp_path, "raise SystemExit(0)")
    resources, api = resources_for_round(x, tmp_path)
    runner.resources = resources
    calls = []

    async def model(attempt, root, prompt, seconds):
        calls.append(True)
        if len(calls) == 1:
            await four(x, main)
            parent = await x.rounds.begin(main, "parent")
            await x.rounds.reject(main, parent["run"], "BOUNDARY_REFERENCE")
            return
        assert (await x.rounds.state(main))["needed"] == []
        await publish(x, main, await x.rounds.begin(main, "parent"))

    runner._spawn = model
    try:
        assert await runner.run(main) == await x.rounds.result(main)
        assert len(calls) == len(api.created) == len(api.deleted) == 2
        assert not api.items
        assert not list(tmp_path.rglob(".cube.secrets.json"))
        async with x.db.engine.connect() as c:
            counts = await c.scalar(sa.select(s.analysis_rounds.c.counts))
            assert counts == {"1h": 1, "4h": 1, "6h": 1, "12h": 1, "parent": 2, "coordinator": 2}
    finally:
        await resources.close()
