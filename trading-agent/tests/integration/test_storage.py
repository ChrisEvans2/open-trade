import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import bars_at

from trading_agent.config import FeatureConfig
from trading_agent.market.snapshots import build_snapshot
from trading_agent.storage import schema as s
from trading_agent.storage.market import get_snapshot, persist_bar, persist_snapshot
from trading_agent.storage.permissions import provision_app_permissions, verify_app_permissions

pytestmark = pytest.mark.postgres


async def test_revision_append_and_repeated_fetch_keep_original_time(database, anchor):
    bar = bars_at(anchor)[-1]
    async with database.transaction() as connection:
        original = await persist_bar(connection, bar)
    async with database.transaction() as connection:
        repeated = await persist_bar(
            connection,
            bar.model_copy(
                update={"id": uuid4(), "available_at": bar.available_at + timedelta(minutes=1)}
            ),
        )
        assert repeated.id == original.id and repeated.available_at == original.available_at
        revision = await persist_bar(
            connection, original.model_copy(update={"close": original.close + Decimal("0.1")})
        )
        assert revision.id != original.id and revision.revision_no == 2
    async with database.engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    sa.select(s.market_bars).order_by(s.market_bars.c.revision_no)
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 2
        assert rows[0]["c"] == original.close
        assert rows[1]["c"] == revision.close


async def test_concurrent_initial_bar_has_single_revision(database, anchor):
    bar = bars_at(anchor)[-1]

    async def submit():
        async with database.transaction() as connection:
            return await persist_bar(connection, bar.model_copy(update={"id": uuid4()}))

    results = await asyncio.gather(*(submit() for _ in range(20)))
    assert len({result.id for result in results}) == 1
    async with database.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(s.market_bars)) == 1


async def test_snapshot_roundtrip_and_fk(database, anchor):
    data = {}
    async with database.transaction() as connection:
        for timeframe in ("1h", "4h", "6h", "12h"):
            data[timeframe] = tuple(
                [await persist_bar(connection, b) for b in bars_at(anchor, timeframe)]
            )
        snapshot = build_snapshot(anchor, anchor + timedelta(seconds=65), data, FeatureConfig())
        ident = await persist_snapshot(connection, snapshot)
        assert (
            await persist_snapshot(connection, snapshot.model_copy(update={"id": uuid4()})) == ident
        )
    async with database.engine.connect() as connection:
        restored = await get_snapshot(connection, ident)
        assert restored.payload_hash == snapshot.payload_hash
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(s.snapshot_bars)) == 320
        )
    with pytest.raises(sa.exc.IntegrityError):
        async with database.transaction() as connection:
            await connection.execute(
                sa.insert(s.snapshot_bars).values(
                    snapshot_id=ident, timeframe="1h", position=0, bar_id=uuid4()
                )
            )


async def test_fact_update_delete_and_truncate_rejected(database, anchor):
    async with database.transaction() as connection:
        bar = await persist_bar(connection, bars_at(anchor)[-1])
    for statement in (
        sa.update(s.market_bars).where(s.market_bars.c.id == bar.id).values(c=1),
        sa.delete(s.market_bars).where(s.market_bars.c.id == bar.id),
        sa.text("TRUNCATE market_bars CASCADE"),
    ):
        with pytest.raises(sa.exc.IntegrityError, match="immutable"):
            async with database.transaction() as connection:
                await connection.execute(statement)
    async with database.engine.connect() as connection:
        assert await connection.scalar(sa.select(s.market_bars.c.c)) == bar.close


async def test_real_role_cannot_mutate_facts(database):
    role = "ta_app_test_" + uuid4().hex[:12]
    try:
        async with database.transaction() as connection:
            await connection.execute(sa.text(f'CREATE ROLE "{role}" NOLOGIN'))
            await provision_app_permissions(connection, role)
        async with database.transaction() as connection:
            await connection.execute(sa.text(f'SET LOCAL ROLE "{role}"'))
            assert await verify_app_permissions(connection)
            assert await connection.scalar(sa.select(s.runtime_control.c.generation)) == 0
        with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
            async with database.transaction() as connection:
                await connection.execute(sa.text(f'SET LOCAL ROLE "{role}"'))
                await connection.execute(sa.delete(s.snapshots))
    finally:
        async with database.transaction() as connection:
            await connection.execute(sa.text(f'DROP OWNED BY "{role}"'))
            await connection.execute(sa.text(f'DROP ROLE "{role}"'))
