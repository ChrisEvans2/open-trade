import json
from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa

from trading_agent.config import AppConfig, SandboxConfig
from trading_agent.domain.errors import DomainError
from trading_agent.jobs.service import JobService
from trading_agent.runtime.ownership import RuntimeOwnership
from trading_agent.runtime.sandboxes import PROJECT, CubeResources
from trading_agent.storage import schema as s

pytestmark = pytest.mark.postgres


class CubeAPI:
    def __init__(self):
        self.items, self.created, self.deleted, self.renewed = {}, [], [], []
        self.limits = {"cpuCount": 2, "memoryMB": 2048, "diskSizeMB": 4096}

    def __call__(self, request):
        path = request.url.path
        if path == "/sandboxes" and request.method == "POST":
            payload = json.loads(request.content)
            self.created.append(payload)
            raw = {
                "sandboxID": "fixture-" + str(len(self.created)),
                "metadata": payload["metadata"],
                "templateID": payload["templateID"],
                **self.limits,
            }
            self.items[raw["sandboxID"]] = raw
            # Real Cube create omits metadata; GET adds internal template labels.
            raw["metadata"] = {**payload["metadata"], "cube.internal.fixture": "template"}
            return httpx.Response(201, json={"sandboxID": raw["sandboxID"]})
        if path == "/sandboxes":
            return httpx.Response(200, json=list(self.items.values()))
        if path.endswith("/timeout"):
            ident = path.split("/")[-2]
            if ident not in self.items:
                return httpx.Response(404)
            self.renewed.append((ident, json.loads(request.content)["timeout"]))
            return httpx.Response(204)
        ident = path.rsplit("/", 1)[-1]
        if ident not in self.items:
            return httpx.Response(404)
        if request.method == "DELETE":
            self.deleted.append(ident)
            del self.items[ident]
            return httpx.Response(204)
        return httpx.Response(200, json=self.items[ident])


async def resources_for(database):
    pytest.importorskip("cubesandbox")
    owner = RuntimeOwnership(database)
    await owner.acquire()
    jobs = JobService(database, owner)
    api = CubeAPI()
    config = AppConfig(
        sandbox=SandboxConfig(
            base_url="http://127.0.0.1:3000",
            template="fixture-template",
            sdk_version="0.7.0",
            proxy_node_ip="127.0.0.1",
        )
    )
    client = httpx.AsyncClient(base_url="http://127.0.0.1:3000", transport=httpx.MockTransport(api))
    resources = CubeResources(config, "fixture-cube-key", client=client)
    resources.jobs = jobs
    # Lifecycle/ownership test only; a transport fixture cannot prove guest isolation.
    resources._populate = lambda raw, root: None
    return owner, jobs, resources, api


async def new_attempt(database, jobs):
    async with database.transaction() as connection:
        await jobs.create(
            connection, "EXPLAIN", "fixture:renew", {}, await database.now(connection)
        )
    return await jobs.claim(("EXPLAIN",))


async def test_renewal_is_bounded_by_authorization_and_rechecks_after_external_io(
    database, tmp_path
):
    owner, jobs, resources, api = await resources_for(database)
    try:
        attempt = await new_attempt(database, jobs)
        await resources.prepare(attempt, tmp_path)
        async with jobs.transaction(attempt) as connection:
            now = await database.now(connection)
            await connection.execute(
                sa.update(s.job_attempts)
                .where(s.job_attempts.c.id == attempt.id)
                .values(token_expires_at=now + timedelta(seconds=35), model_started_at=now)
            )
        await resources.renew(attempt, 90)
        assert 45 <= api.renewed[-1][1] <= 50  # 35s authorization plus 15s cleanup margin.
        await resources.renew(attempt, 10)
        assert api.renewed[-1][1] == 25

        # A separate transaction can change the fence during HTTP I/O: no DB lock is held.
        request = resources.request

        async def invalidate(method, path, payload=None, **kwargs):
            result = await request(method, path, payload, **kwargs)
            if path.endswith("/timeout"):
                async with database.transaction() as connection:
                    await connection.execute(
                        sa.update(s.jobs).where(s.jobs.c.id == attempt.job_id).values(fence=2)
                    )
            return result

        resources.request = invalidate
        with pytest.raises(DomainError) as caught:
            await resources.renew(attempt, 10)
        assert caught.value.code == "ATTEMPT_EXPIRED"
        assert await resources.cleanup(attempt)
    finally:
        await resources.close()
        await owner.release()


@pytest.mark.parametrize("change", ["fence", "revoked", "deadline", "missing", "foreign"])
async def test_renewal_refuses_stale_missing_and_foreign_resources(database, tmp_path, change):
    owner, jobs, resources, api = await resources_for(database)
    try:
        attempt = await new_attempt(database, jobs)
        await resources.prepare(attempt, tmp_path)
        async with database.transaction() as connection:
            if change == "fence":
                await connection.execute(
                    sa.update(s.jobs).where(s.jobs.c.id == attempt.job_id).values(fence=2)
                )
            elif change == "revoked":
                await connection.execute(
                    sa.update(s.job_attempts)
                    .where(s.job_attempts.c.id == attempt.id)
                    .values(token_revoked=True)
                )
            elif change == "deadline":
                await connection.execute(
                    sa.update(s.jobs)
                    .where(s.jobs.c.id == attempt.job_id)
                    .values(deadline_at=await database.now(connection) - timedelta(seconds=1))
                )
        if change == "missing":
            del api.items["fixture-1"]
        elif change == "foreign":
            api.items["fixture-1"]["metadata"] = {"project": "foreign"}
        with pytest.raises(DomainError):
            await resources.renew(attempt, 60)
        assert api.renewed == []
    finally:
        await resources.close()
        await owner.release()


async def test_resources_record_create_before_io_and_child_owned_cleanup(database, tmp_path):
    owner, jobs, resources, api = await resources_for(database)
    try:
        async with database.transaction() as connection:
            ident = await jobs.create(
                connection,
                kind="EXPLAIN",
                dedupe_key="fixture:cube",
                payload={},
                next_run_at=await database.now(connection),
            )
        attempt = await jobs.claim(kinds=("EXPLAIN",))
        await resources.prepare(attempt, tmp_path)
        async with database.engine.connect() as connection:
            record = await connection.scalar(
                sa.select(s.job_attempts.c.sandbox_ref).where(s.job_attempts.c.job_id == ident)
            )
        assert record["status"] == "READY"
        assert api.created[0]["metadata"]["project"] == PROJECT
        assert api.created[0]["envVars"] == {} and api.created[0]["volumeMounts"] == []
        secret = tmp_path / ".cube.secrets.json"
        assert secret.stat().st_mode & 0o077 == 0
        assert await resources.cleanup(attempt) is True
        assert api.deleted == [record["sandbox_id"]]
        assert await resources.cleanup(attempt) is True
    finally:
        await resources.close()
        await owner.release()


async def test_recovery_deletes_only_known_expired_attempt_metadata(database, tmp_path):
    owner, jobs, resources, api = await resources_for(database)
    try:
        async with database.transaction() as connection:
            await jobs.create(
                connection,
                kind="EXPLAIN",
                dedupe_key="fixture:expired-cube",
                payload={},
                next_run_at=await database.now(connection),
            )
        attempt = await jobs.claim(kinds=("EXPLAIN",))
        await resources.prepare(attempt, tmp_path)
        api.items["foreign"] = {"sandboxID": "foreign", "metadata": {"project": "another-agent"}}
        api.items["unknown"] = {
            "sandboxID": "unknown",
            "metadata": {"project": PROJECT, "attempt_id": "unknown"},
        }
        await resources.recover()
        assert api.deleted == []
        await owner.release()
        next_owner = RuntimeOwnership(database)
        await next_owner.acquire()
        resources.jobs = JobService(database, next_owner)
        try:
            await resources.recover()
            assert api.deleted == ["fixture-1"]
            assert set(api.items) == {"foreign", "unknown"}
        finally:
            await next_owner.release()
    finally:
        await resources.close()
        await owner.release()


async def test_over_budget_template_is_rejected_and_owned_resource_can_be_cleaned(
    database, tmp_path
):
    owner, jobs, resources, api = await resources_for(database)
    try:
        api.limits["cpuCount"] = 3
        async with database.transaction() as connection:
            await jobs.create(
                connection,
                "EXPLAIN",
                "fixture:oversized-template",
                {},
                await database.now(connection),
            )
        attempt = await jobs.claim(("EXPLAIN",))
        with pytest.raises(DomainError) as caught:
            await resources.prepare(attempt, tmp_path)
        assert caught.value.code == "CUBE_RESOURCE_LIMITS"
        assert await resources.cleanup(attempt)
        assert api.deleted == ["fixture-1"]
    finally:
        await resources.close()
        await owner.release()


async def test_confirmed_cleanup_allows_same_attempt_to_prepare_another_sandbox(database, tmp_path):
    owner, jobs, resources, api = await resources_for(database)
    try:
        attempt = await new_attempt(database, jobs)
        await resources.prepare(attempt, tmp_path)
        secret = tmp_path / ".cube.secrets.json"
        first = json.loads(secret.read_text())["sandbox"]["sandboxID"]
        assert await resources.cleanup(attempt)
        assert not secret.exists()
        await resources.prepare(attempt, tmp_path)
        second = json.loads(secret.read_text())["sandbox"]["sandboxID"]
        assert second != first and set(api.items) == {second}
        assert secret.stat().st_mode & 0o077 == 0
        assert await resources.cleanup(attempt)
        assert not secret.exists()
        assert await resources.cleanup(attempt)  # idempotent cleanup
    finally:
        await resources.close()
        await owner.release()


async def test_unconfirmed_cleanup_keeps_bootstrap_until_remote_deletion_is_confirmed(
    database, tmp_path
):
    owner, jobs, resources, api = await resources_for(database)
    try:
        attempt = await new_attempt(database, jobs)
        await resources.prepare(attempt, tmp_path)
        secret = tmp_path / ".cube.secrets.json"
        original = resources._destroy

        async def pending(raw):
            return False

        resources._destroy = pending
        assert not await resources.cleanup(attempt)
        assert secret.exists() and len(api.items) == 1
        resources._destroy = original
        assert await resources.cleanup(attempt)
        assert not secret.exists() and api.items == {}
    finally:
        await resources.close()
        await owner.release()
