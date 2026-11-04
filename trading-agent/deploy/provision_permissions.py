"""Grant the runtime role append-only permissions; does not create roles or accept passwords."""

import argparse
import asyncio
import os

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from trading_agent.storage.permissions import provision_app_permissions


async def run(role, database_name):
    url = os.environ.get("TA_MIGRATION_DATABASE_URL", "")
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("人工迁移环境缺少PostgreSQL连接")
    engine = create_async_engine(url, hide_parameters=True, connect_args={"connect_timeout": 3})
    try:
        async with engine.begin() as connection:
            if await connection.scalar(sa.text("SELECT current_database()")) != database_name:
                raise ValueError("数据库名称与显式目标不一致")
            row = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                            "rolreplication, rolbypassrls "
                            "FROM pg_roles WHERE rolname=:role"
                        ),
                        {"role": role},
                    )
                )
                .mappings()
                .first()
            )
            if (
                row is None
                or not row["rolcanlogin"]
                or any(
                    row[k]
                    for k in (
                        "rolsuper",
                        "rolcreatedb",
                        "rolcreaterole",
                        "rolreplication",
                        "rolbypassrls",
                    )
                )
            ):
                raise ValueError("目标必须是已创建的低权限应用登录角色")
            await provision_app_permissions(connection, role)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.role, args.database))
        print("应用表权限已配置；不包含迁移和事实修改权限。")
    except Exception:
        parser.exit(1, "权限配置失败，请检查显式目标与人工迁移连接。\n")
