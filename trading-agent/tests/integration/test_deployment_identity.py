import asyncio
import subprocess
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from trading_agent.config import AppConfig, HermesConfig
from trading_agent.runtime.deployment import DeploymentCheck
from trading_agent.storage.database import Database
from trading_agent.storage.permissions import provision_app_permissions

pytestmark = pytest.mark.postgres


async def test_actual_git_revision_and_restricted_database_role_are_checked(
    database, postgres_url, tmp_path
):
    role = "ta_deployment_test_" + uuid4().hex[:12]

    def repository():
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "fixture",
            ],
            check=True,
        )
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
        ).strip()

    revision = await asyncio.to_thread(repository)
    config = AppConfig(hermes=HermesConfig(repository=tmp_path, reference_commit=revision))
    async with database.transaction() as connection:
        await connection.execute(sa.text(f'CREATE ROLE "{role}" LOGIN'))
        await provision_app_permissions(connection, role)
    app_db = Database(
        make_url(postgres_url).set(username=role).render_as_string(hide_password=False)
    )
    try:
        check = DeploymentCheck(config, app_db)
        check.files = lambda: (
            True
        )  # File gates have separate tests; this checks Git and real PG role.

        async def verified_model_fixture():
            return True

        check.check_model = verified_model_fixture
        assert await check()
        assert check.git_checked
        bad = DeploymentCheck(
            config.model_copy(
                update={"hermes": config.hermes.model_copy(update={"reference_commit": "0" * 40})}
            ),
            app_db,
        )
        bad.files = lambda: True
        assert not await bad()
        # Correct Git revision is insufficient when runtime connects as superuser.
        admin = DeploymentCheck(config, database)
        admin.files = lambda: True
        assert not await admin()
    finally:
        await app_db.close()
        async with database.transaction() as connection:
            await connection.execute(sa.text(f'DROP OWNED BY "{role}"'))
            await connection.execute(sa.text(f'DROP ROLE "{role}"'))
