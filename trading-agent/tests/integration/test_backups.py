import asyncio
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
import sqlalchemy as sa
from psycopg import sql
from test_prediction_pipeline import minute, publish_child, task
from test_prediction_pipeline import prediction_system as prediction_system

from trading_agent.runtime.ownership import LOCK_ID, LOCK_NAMESPACE
from trading_agent.storage import backups as backup_tools
from trading_agent.storage import schema as s
from trading_agent.storage.backups import backup, restore
from trading_agent.storage.database import Database

pytestmark = pytest.mark.postgres


async def test_real_backup_restores_structural_facts_and_evaluation_into_empty_database(
    prediction_system, postgres_url, postgres_admin_url, tmp_path, monkeypatch
):
    database, clock, jobs, predictions, _ = prediction_system
    ident = await publish_child(prediction_system)
    original_at = clock.value
    verifier = await task(prediction_system, kind="PREDICT_VERIFY", freeze=False)
    first_bar = minute("BTC-USDT-SWAP", original_at.replace(second=0) + timedelta(minutes=1))
    # Renew through the supported API while advancing to the first full minute.
    for seconds in (40, 80):
        clock.value = original_at + timedelta(seconds=seconds)
        assert await jobs.renew(verifier)
    clock.value = first_bar.close_boundary
    await predictions.verify(verifier, ident, (first_bar,))
    async with database.engine.connect() as connection:
        fact = (await connection.execute(sa.select(s.prediction_facts))).mappings().one()
        evaluation = (
            (await connection.execute(sa.select(s.prediction_evaluations))).mappings().one()
        )
        original_fact, original_evaluation = dict(fact), dict(evaluation)
    tools = Path("/opt/homebrew/opt/postgresql@16/bin")
    if not await asyncio.to_thread(tools.exists):
        tools = None  # Linux CI uses PostgreSQL16 on PATH.
    result = await asyncio.to_thread(backup, postgres_url, tmp_path, bin_dir=tools)
    target = "ta_restore_test_" + uuid4().hex
    parsed = urlsplit(postgres_admin_url)
    target_url = urlunsplit(
        (parsed.scheme, parsed.netloc, "/" + target, parsed.query, parsed.fragment)
    )
    target_raw = target_url.replace("postgresql+psycopg://", "postgresql://", 1)
    original_run_pg = backup_tools.run_pg
    restore_lock_checked = False

    def check_restore_boundary(argv, env):
        nonlocal restore_lock_checked
        if "--dbname" in argv:
            with psycopg.connect(target_raw, autocommit=True) as probe:
                state = probe.execute(
                    "SELECT state, xact_start FROM pg_stat_activity "
                    "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                    "AND backend_type='client backend'"
                ).fetchall()
                assert state == [("idle", None)]
                assert not probe.execute(
                    "SELECT pg_try_advisory_lock(%s,%s)", (LOCK_NAMESPACE, LOCK_ID)
                ).fetchone()[0]
            restore_lock_checked = True
        return original_run_pg(argv, env)

    monkeypatch.setattr(backup_tools, "run_pg", check_restore_boundary)
    admin = postgres_admin_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
        restored = None
        try:
            report = await asyncio.to_thread(
                restore,
                target_url,
                Path(result["archive"]),
                expected_database=target,
                bin_dir=tools,
            )
            assert report["restored"] and not report["runtime_started"]
            assert restore_lock_checked
            restored = Database(target_url)
            await restored.require_migration()
            async with restored.engine.connect() as copy:
                fact = (await copy.execute(sa.select(s.prediction_facts))).mappings().one()
                evaluation = (
                    (await copy.execute(sa.select(s.prediction_evaluations))).mappings().one()
                )
                assert dict(fact) == original_fact
                assert dict(evaluation) == original_evaluation
                assert fact["id"] == ident
                assert await copy.scalar(sa.select(sa.func.count()).select_from(s.forecasts)) == 0
                assert (
                    await copy.scalar(sa.select(sa.func.count()).select_from(s.prediction_inputs))
                    == 1
                )
            with pytest.raises(sa.exc.IntegrityError, match="immutable"):
                async with restored.transaction() as copy:
                    await copy.execute(sa.update(s.prediction_facts).values(body={}))
            with pytest.raises(ValueError, match="空库"):
                await asyncio.to_thread(
                    restore,
                    target_url,
                    Path(result["archive"]),
                    expected_database=target,
                    bin_dir=tools,
                )
        finally:
            if restored:
                await restored.close()
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(target))
            )
