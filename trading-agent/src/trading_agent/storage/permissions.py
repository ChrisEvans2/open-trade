from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from trading_agent.storage.schema import IMMUTABLE_TABLES, metadata


async def provision_app_permissions(connection: AsyncConnection, role: str):
    """Migration/operator-only helper; never called by ta serve or the model."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError("应用角色名称不合法")
    await connection.execute(sa.text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
    for name in metadata.tables:
        await connection.execute(sa.text(f'REVOKE ALL PRIVILEGES ON TABLE "{name}" FROM "{role}"'))
        privileges = "SELECT, INSERT" if name in IMMUTABLE_TABLES else "SELECT, INSERT, UPDATE"
        if name == "trade_raw_cache":
            privileges += ", DELETE"
        await connection.execute(sa.text(f'GRANT {privileges} ON TABLE "{name}" TO "{role}"'))
    await connection.execute(sa.text(f'GRANT SELECT ON alembic_version TO "{role}"'))


async def verify_app_permissions(connection: AsyncConnection) -> bool:
    role = await connection.scalar(sa.text("SELECT current_user"))
    if await connection.scalar(
        sa.text(
            "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
            "FROM pg_roles WHERE rolname=:name"
        ),
        {"name": role},
    ):
        return False
    for name in IMMUTABLE_TABLES:
        for privilege in ("UPDATE", "DELETE", "TRUNCATE", "TRIGGER"):
            if await connection.scalar(
                sa.text("SELECT has_table_privilege(:name, :privilege)"),
                {"name": name, "privilege": privilege},
            ):
                return False
    for name in metadata.tables:
        privileges = (
            ("SELECT", "INSERT") if name in IMMUTABLE_TABLES else ("SELECT", "INSERT", "UPDATE")
        )
        if name == "trade_raw_cache":
            privileges += ("DELETE",)
        for privilege in privileges:
            if not await connection.scalar(
                sa.text("SELECT has_table_privilege(:name, :privilege)"),
                {"name": name, "privilege": privilege},
            ):
                return False
    if not await connection.scalar(
        sa.text("SELECT has_table_privilege('alembic_version', 'SELECT')")
    ):
        return False
    return True
