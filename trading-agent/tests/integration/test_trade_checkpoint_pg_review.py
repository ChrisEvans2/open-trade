"""Independent PostgreSQL regressions for checkpoint atomicity and audit scheduling."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
import sqlalchemy as sa
from psycopg import sql
from test_prediction_pipeline import prediction_system as prediction_system

from trading_agent.config import MarketConfig, TradeMinuteConfig
from trading_agent.domain.errors import DomainError
from trading_agent.market.models import Observation
from trading_agent.market.okx import OKXClient
from trading_agent.market.trade_collector import TradeMinuteCollector
from trading_agent.market.trades import MINUTE_SOURCE, capture_minute
from trading_agent.runtime.ownership import RuntimeOwnership
from trading_agent.storage import schema as s
from trading_agent.storage.database import Database
from trading_agent.storage.market import persist_observation
from trading_agent.storage.permissions import provision_app_permissions, verify_app_permissions
from trading_agent.storage.trade_checkpoints import clear_checkpoint, load_checkpoint, save_page

pytestmark = pytest.mark.postgres
TEST_ROOT = Path(__file__).resolve().parents[2]
INST = "BTC-USDT-SWAP"
META = dict(
    instId=INST,
    instType="SWAP",
    ctType="linear",
    settleCcy="USDT",
    ctValCcy="BTC",
    ctVal="0.01",
    ctMult="1",
)


def row(start, ident, seconds, side="buy", size="100"):
    return dict(
        instId=INST,
        tradeId=str(ident),
        ts=str(int((start + timedelta(seconds=seconds)).timestamp() * 1000)),
        side=side,
        sz=size,
        px="100",
        source="0",
    )


def checkpoint_state(start, pages, cursor):
    return dict(
        pages=pages,
        cursor=cursor,
        complete=False,
        contract_hash="fixture",
        candle={"close_boundary": (start + timedelta(minutes=1)).isoformat()},
    )


async def test_page_insert_failure_rolls_back_cursor_in_same_transaction(prediction_system):
    db, clock, jobs, _, _ = prediction_system
    start = clock.value.replace(second=0) - timedelta(minutes=10)
    first = [row(start, 4, 40)]
    await save_page(db, jobs.ownership, INST, start, checkpoint_state(start, 1, 4), first)
    async with db.transaction() as conn:
        await conn.execute(
            sa.text(
                "CREATE FUNCTION review_fail_second_page() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN IF NEW.page_no=2 THEN RAISE EXCEPTION 'review injected page failure'; "
                "END IF; RETURN NEW; END $$"
            )
        )
        await conn.execute(
            sa.text(
                "CREATE TRIGGER review_fail_page BEFORE INSERT ON trade_minute_pages "
                "FOR EACH ROW EXECUTE FUNCTION review_fail_second_page()"
            )
        )
    with pytest.raises(sa.exc.DBAPIError):
        await save_page(
            db,
            jobs.ownership,
            INST,
            start,
            checkpoint_state(start, 2, 3),
            [row(start, 3, 20)],
        )
    loaded = await load_checkpoint(db, INST, start)
    assert loaded["cursor"] == 4 and loaded["pages"] == 1
    assert loaded["rows"] == first
    async with db.transaction() as conn:
        await conn.execute(sa.text("DROP TRIGGER review_fail_page ON trade_minute_pages"))
    await save_page(
        db,
        jobs.ownership,
        INST,
        start,
        checkpoint_state(start, 2, 3),
        [row(start, 3, 20)],
    )
    assert (await load_checkpoint(db, INST, start))["pages"] == 2
    async with db.transaction() as conn:
        await clear_checkpoint(conn, INST, start)
        assert await conn.scalar(sa.select(sa.func.count()).select_from(s.trade_minute_pages)) == 0


class ReviewClient:
    parse_bar = OKXClient.parse_bar

    def __init__(self, clock):
        self.clock, self.start, self.pages = clock, None, []

    def now(self):
        return self.clock()

    async def get(self, path, params, deadline):
        if path.endswith("history-candles"):
            return [[str(int(self.start.timestamp() * 1000)), *["100"] * 4, "600", "6", "600", "1"]]
        return self.pages.pop(0)


async def test_paused_audit_keeps_priority_until_its_checkpoint_completes(prediction_system):
    db, clock, jobs, _, _ = prediction_system
    config = MarketConfig(trade_minutes=TradeMinuteConfig(bootstrap_days=1))
    client = ReviewClient(clock)
    collector = TradeMinuteCollector(db, jobs.ownership, client, config, inst_ids=(INST,))
    start = clock.value.replace(second=0) - timedelta(minutes=10)
    other = start - timedelta(minutes=10)

    async def capture(at, pages, **options):
        client.start, client.pages = at, pages
        return await capture_minute(
            client,
            INST,
            at,
            clock.value + timedelta(seconds=30),
            collector.settings,
            META,
            **options,
        )

    def all_rows(at):
        return [
            row(at, 4, 50, "sell", "300"),
            row(at, 3, 30, "buy", "200"),
            row(at, 2, 0),
            row(at, 1, -1),
        ]

    for at in (start, other):
        await collector.record(await capture(at, [all_rows(at)]))
    async with db.transaction() as conn:
        await conn.execute(
            sa.update(s.trade_minute_work).values(
                updated_at=clock.value - timedelta(hours=1),
            )
        )

    async def save(state, rows):
        await save_page(db, jobs.ownership, INST, start, state, rows)

    for page in ([all_rows(start)[0]], [all_rows(start)[1]]):
        result = await capture(
            start,
            [page],
            resume=await load_checkpoint(db, INST, start),
            on_page=save,
            page_limit=1,
        )
        assert result.observation.payload["reason"] == "TRADE_PAGE_SLICE"
        await collector.record(result, audit=True)
        clock.value += timedelta(seconds=1)
        collector._rotation = 2  # The next round offers the bounded audit slot first.
        planned = await collector.plan()
        audit = next(item for item, is_audit in planned if is_audit)
        assert audit["open_at"] == start
        async with db.transaction() as conn:
            assert (
                await conn.scalar(
                    sa.select(sa.func.count()).select_from(
                        s.market_observations,
                    )
                )
                == 2
            )  # A pause never creates an invalidating market revision.

    final = await capture(
        start,
        [all_rows(start)[2:]],
        resume=await load_checkpoint(db, INST, start),
        on_page=save,
        page_limit=1,
    )
    assert final.observation.quality == "OK"
    assert final.observation.payload["trade_count"] == 3
    await collector.record(final, audit=True)
    assert await load_checkpoint(db, INST, start) is None
    async with db.transaction() as conn:
        assert (
            await conn.scalar(
                sa.select(s.trade_minute_work.c.state).where(
                    s.trade_minute_work.c.inst_id == INST,
                    s.trade_minute_work.c.open_at == start,
                )
            )
            == "VERIFIED"
        )


@pytest.fixture
def review_upgrade_url(postgres_admin_url):
    """A separate empty database, so the test truly starts from revision 0011."""
    name = "ta_test_review_upgrade_" + uuid4().hex
    parsed = urlsplit(postgres_admin_url)
    url = urlunsplit((parsed.scheme, parsed.netloc, "/" + name, parsed.query, parsed.fragment))
    raw_admin = postgres_admin_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(raw_admin, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield url
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def migrate_review_database(url, revision):
    process = await asyncio.create_subprocess_exec(
        str(TEST_ROOT / ".venv" / "bin" / "alembic"),
        "upgrade",
        revision,
        cwd=TEST_ROOT,
        env={"PATH": os.environ["PATH"], "TA_MIGRATION_DATABASE_URL": url},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()
    assert process.returncode == 0, "隔离测试库迁移失败，连接信息及原始输出不公开"


async def test_existing_0011_facts_upgrade_and_restricted_role_checkpoint_access(
    review_upgrade_url,
    anchor,
):
    await migrate_review_database(review_upgrade_url, "0011_trade_minutes")
    db = Database(review_upgrade_url, clock=lambda: anchor)
    role = "ta_review_checkpoint_" + uuid4().hex[:12]
    owner = RuntimeOwnership(db)
    role_created = False
    start = anchor - timedelta(minutes=10)
    try:
        with pytest.raises(DomainError) as migration_error:
            await db.require_migration()
        assert migration_error.value.code == "MIGRATION_REQUIRED"
        await owner.acquire()
        async with db.transaction() as conn:
            await owner.guard(conn)
            assert (
                await conn.scalar(sa.text("SELECT to_regclass('trade_minute_checkpoints')")) is None
            )
            await persist_observation(
                conn,
                Observation(
                    source=MINUTE_SOURCE,
                    inst_id=INST,
                    kind="taker_volume",
                    event_at=start + timedelta(minutes=1),
                    available_at=anchor,
                    coverage_start=start,
                    coverage_end=start + timedelta(minutes=1),
                    unit="base_quantity",
                    payload={"buy": "2", "sell": "3", "timeframe": "1m", "raw_digest": "old"},
                ),
            )
            await conn.execute(
                sa.insert(s.trade_minute_work).values(
                    inst_id=INST,
                    open_at=start,
                    state="VERIFIED",
                    attempts=0,
                    next_retry_at=anchor,
                    updated_at=anchor,
                    reason=None,
                )
            )
            old_fact = dict((await conn.execute(sa.select(s.market_observations))).mappings().one())
            old_work = dict((await conn.execute(sa.select(s.trade_minute_work))).mappings().one())
        # Model a stopped old runtime before running the real additive migration.
        await owner.release()
        await migrate_review_database(review_upgrade_url, "0012_trade_checkpoints")
        await db.require_migration()
        await owner.acquire()
        async with db.transaction() as conn:
            assert (
                dict((await conn.execute(sa.select(s.market_observations))).mappings().one())
                == old_fact
            )
            assert (
                dict((await conn.execute(sa.select(s.trade_minute_work))).mappings().one())
                == old_work
            )
            await conn.execute(
                sa.text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE')
            )
            await provision_app_permissions(conn, role)
        role_created = True

        class RestrictedDatabase:
            now = db.now

            @asynccontextmanager
            async def transaction(self):
                async with db.transaction() as conn:
                    await conn.execute(sa.text(f'SET LOCAL ROLE "{role}"'))
                    yield conn

        app = RestrictedDatabase()
        async with app.transaction() as conn:
            assert await verify_app_permissions(conn)
        await save_page(app, owner, INST, start, checkpoint_state(start, 1, 4), [row(start, 4, 40)])
        await save_page(app, owner, INST, start, checkpoint_state(start, 2, 3), [row(start, 3, 20)])
        restored = await load_checkpoint(app, INST, start)
        assert restored["pages"] == 2 and len(restored["rows"]) == 2
        async with app.transaction() as conn:
            await owner.guard(conn)
            await clear_checkpoint(conn, INST, start)
            assert (
                await conn.scalar(sa.select(sa.func.count()).select_from(s.trade_minute_pages)) == 0
            )
            assert (
                await conn.scalar(
                    sa.select(sa.func.count()).select_from(s.trade_minute_checkpoints)
                )
                == 0
            )
        with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
            async with app.transaction() as conn:
                await conn.execute(sa.delete(s.market_observations))
        async with db.transaction() as conn:
            assert (
                dict((await conn.execute(sa.select(s.market_observations))).mappings().one())
                == old_fact
            )
            assert (
                dict((await conn.execute(sa.select(s.trade_minute_work))).mappings().one())
                == old_work
            )
    finally:
        await owner.release()
        if role_created:
            async with db.transaction() as conn:
                await conn.execute(sa.text(f'DROP OWNED BY "{role}"'))
                await conn.execute(sa.text(f'DROP ROLE "{role}"'))
        await db.close()
