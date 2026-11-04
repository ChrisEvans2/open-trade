import os
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql

from trading_agent.domain.time import INTERVAL_SECONDS, boundary
from trading_agent.market.models import Bar
from trading_agent.storage.database import Database


@pytest.fixture
def anchor():
    return datetime(2026, 10, 7, 8, tzinfo=UTC)


def bars_at(anchor, timeframe="1h", *, flat=False):
    end = boundary(anchor, timeframe)
    step = timedelta(seconds=INTERVAL_SECONDS[timeframe])
    result = []
    for i in range(80):
        close = Decimal(100) if flat else Decimal(100 + i)
        high, low = (close, close) if flat else (close + 2, close - 2)
        start = end - (80 - i) * step
        result.append(
            Bar(
                id=uuid5(NAMESPACE_URL, f"fixture:{timeframe}:{start.isoformat()}:{flat}"),
                inst_id="BTC-USDT-SWAP",
                timeframe=timeframe,
                open_at=start,
                close_boundary=start + step,
                open=close,
                high=high,
                low=low,
                close=close,
                volume_contracts=100 + i,
                volume_base=Decimal("1") + Decimal(i) / 100,
                turnover_quote=(Decimal("1") + Decimal(i) / 100) * close,
                available_at=anchor + timedelta(seconds=60),
            )
        )
    return tuple(result)


@pytest.fixture(scope="session")
def postgres_admin_url():
    configured = os.environ.get("TA_TEST_DATABASE_URL")
    if configured:
        return configured
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [
            str(root / ".venv" / "bin" / "python"),
            str(root / "deploy" / "local-postgres.py"),
            "start",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip().splitlines()[-1]


@pytest.fixture
def postgres_url(postgres_admin_url):
    root = Path(__file__).resolve().parent.parent
    name = "ta_test_" + uuid4().hex
    raw_url = postgres_admin_url.replace("postgresql+psycopg://", "postgresql://", 1)
    parsed = urlsplit(postgres_admin_url)
    url = urlunsplit((parsed.scheme, parsed.netloc, "/" + name, parsed.query, parsed.fragment))
    with psycopg.connect(raw_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            migration = subprocess.run(
                [str(root / ".venv" / "bin" / "alembic"), "upgrade", "head"],
                cwd=root,
                env={"PATH": os.environ["PATH"], "TA_MIGRATION_DATABASE_URL": url},
                check=False,
                capture_output=True,
                text=True,
            )
            if migration.returncode:
                pytest.fail("隔离测试库迁移失败；诊断输出未公开以避免泄露连接凭据", pytrace=False)
            yield url
        finally:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest_asyncio.fixture
async def database(postgres_url):
    database = Database(postgres_url)
    await database.require_migration()
    try:
        yield database
    finally:
        await database.close()
