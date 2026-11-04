from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import uvicorn
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from trading_agent.api.app import create_app
from trading_agent.config import load_config
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import json_value
from trading_agent.runtime.doctor import doctor_sync
from trading_agent.runtime.factory import build_runtime
from trading_agent.runtime.logging import configure_logging


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        if "--json" in sys.argv[1:]:
            print(
                json.dumps(
                    {
                        "error": {
                            "code": "CLI_ARGUMENT_INVALID",
                            "message": "命令参数不完整或格式无效",
                        }
                    },
                    ensure_ascii=False,
                )
            )
            raise SystemExit(2)
        super().error(message)


def leaf(parent, name):
    parser = parent.add_parser(name)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    return parser


def parser():
    root = CommandParser(prog="ta", description="Structural prediction trading runtime")
    root.add_argument("--config", type=Path, default=Path("config.yaml"))
    root.add_argument("--json", action="store_true")
    actions = root.add_subparsers(dest="command", required=True)
    leaf(actions, "config").add_argument("action", choices=("check", "show"))
    for name in ("serve", "migrate", "status"):
        leaf(actions, name)
    leaf(actions, "doctor").add_argument("--notify", action="store_true")
    forecast = leaf(actions, "forecast").add_subparsers(dest="action", required=True)
    leaf(forecast, "latest")
    leaf(forecast, "show").add_argument("id", type=UUID)
    predictions = leaf(actions, "prediction").add_subparsers(dest="action", required=True)
    latest = leaf(predictions, "latest")
    latest.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), required=True)
    latest.add_argument("--timeframe", choices=("1h", "4h", "6h", "12h"), default="1h")
    latest.add_argument("--child", action="store_true")
    leaf(predictions, "show").add_argument("id", type=UUID)
    intents = leaf(actions, "intent").add_subparsers(dest="action", required=True)
    leaf(intents, "show").add_argument("id", type=UUID)
    jobs = leaf(actions, "job").add_subparsers(dest="action", required=True)
    leaf(jobs, "show").add_argument("id", type=UUID)
    reviews = leaf(actions, "review").add_subparsers(dest="action", required=True)
    leaf(reviews, "show").add_argument("id", type=UUID)
    strategies = leaf(actions, "strategy").add_subparsers(dest="action", required=True)
    leaf(strategies, "list")
    experiments = leaf(actions, "experiment").add_subparsers(dest="action", required=True)
    leaf(experiments, "show").add_argument("id", type=UUID)
    schedule = leaf(actions, "schedule").add_subparsers(dest="action", required=True)
    leaf(schedule, "pause")
    leaf(schedule, "resume")
    return root


def configuration_status(config):
    fields = [
        ("market.inst_id", config.market.inst_id),
        ("market.contract_type", config.market.contract_type),
        ("market.settlement_asset", config.market.settlement_asset),
        ("hermes.executable", config.hermes.executable),
        ("hermes.python_executable", config.hermes.python_executable),
        ("hermes.home", config.hermes.home),
        ("hermes.repository", config.hermes.repository),
        ("hermes.controller_profile", config.hermes.controller_profile),
        ("hermes.provider", config.hermes.provider),
        ("hermes.model", config.hermes.model),
        ("sandbox.base_url", config.sandbox.base_url),
        ("sandbox.template", config.sandbox.template),
        ("sandbox.sdk_version", config.sandbox.sdk_version),
        ("sandbox.proxy_node_ip", config.sandbox.proxy_node_ip),
        ("sandbox.isolation_report", config.sandbox.isolation_report),
        ("notification.target", config.notification.target),
    ]
    missing = [name for name, value in fields if value is None]
    if not config.sandbox.isolation_verified:
        missing.append("sandbox.isolation_verified")
    return {
        "valid": True,
        "configuration_complete": not missing,
        "missing": missing,
        "engine_config_hash": config.engine_config_hash,
    }


def credential(operator=False):
    token = os.environ.get("TA_OPERATOR_TOKEN" if operator else "TA_CONTROLLER_TOKEN", "")
    if not token or len(token) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise DomainError("CLI_CREDENTIAL_MISSING", "人工命令环境缺少对应API凭据", 401)
    return token


def api(config, method, route, payload=None, *, operator=False, version="v1"):
    with httpx.Client(
        base_url=f"http://127.0.0.1:{config.runtime.api_port}/{version}",
        trust_env=False,
        timeout=httpx.Timeout(10, connect=3),
        follow_redirects=False,
        headers={
            "Authorization": "Bearer " + credential(operator),
            "Idempotency-Key": str(uuid4()),
        },
    ) as client:
        response = client.request(method, route, json=payload)
        try:
            result = response.json()
        except ValueError:
            raise DomainError("CLI_RESPONSE_INVALID", "领域API未返回有效JSON", 503) from None
        if response.is_error:
            error = result.get("error", {})
            raise DomainError(
                error.get("code", "CLI_REQUEST_FAILED"),
                error.get("message", "领域API请求失败"),
                response.status_code,
            )
        return result


def migration_root():
    packaged = Path(__file__).resolve().parent / "resources/database"
    return packaged if packaged.exists() else Path(__file__).resolve().parents[2]


async def migrate(config):
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    from trading_agent.runtime.ownership import LOCK_ID, LOCK_NAMESPACE

    url = os.environ.get("TA_MIGRATION_DATABASE_URL", "")
    if not url.startswith("postgresql+psycopg://"):
        raise DomainError(
            "MIGRATION_CREDENTIAL_MISSING", "迁移只能使用人工环境的PostgreSQL迁移凭据", 401
        )
    engine = create_async_engine(url, hide_parameters=True, connect_args={"connect_timeout": 3})
    try:
        async with engine.connect() as connection:
            # A dedicated DB lock detects runtime on any API port or host.
            held = await connection.scalar(
                sa.select(sa.func.pg_try_advisory_lock(LOCK_NAMESPACE, LOCK_ID))
            )
            if not held:
                raise DomainError("RUNTIME_MUST_STOP", "数据库仍有runtime持锁，拒绝迁移", 409)
            await asyncio.to_thread(
                command.upgrade, Config(str(migration_root() / "alembic.ini")), "head"
            )
            await connection.execute(sa.select(sa.func.pg_advisory_unlock(LOCK_NAMESPACE, LOCK_ID)))
    finally:
        await engine.dispose()
    return {"migrated": True}


def execute(args, config):
    if args.command == "config":
        return json_value(config) if args.action == "show" else configuration_status(config)
    if args.command == "serve":
        listener = configure_logging(config.runtime)
        try:
            uvicorn.run(
                create_app(runtime_factory=lambda: build_runtime(config)),
                host=config.runtime.api_host,
                port=config.runtime.api_port,
                workers=1,
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=20,
            )
        finally:
            listener.stop()
        return {"stopped": True}
    if args.command == "migrate":
        return asyncio.run(migrate(config))
    if args.command == "doctor":
        return doctor_sync(config, credential(), args.notify)
    if args.command == "status":
        return api(config, "GET", "/status")
    if args.command == "prediction":
        route = (
            f"/predictions/latest?symbol={args.symbol}&timeframe={args.timeframe}"
            f"&parent={str(not args.child).lower()}"
            if args.action == "latest"
            else f"/predictions/{args.id}"
        )
        return api(config, "GET", route, version="v2")
    if args.command == "intent":
        return api(config, "GET", f"/intents/{args.id}", version="v2")
    if args.command == "forecast":
        route = "/forecasts/latest" if args.action == "latest" else f"/forecasts/{args.id}"
        return api(config, "GET", route)
    if args.command == "job":
        return api(config, "GET", f"/jobs/{args.id}")
    if args.command == "experiment":
        return api(config, "GET", f"/experiments/{args.id}")
    if args.command == "review":
        return api(config, "GET", f"/reviews/{args.id}")
    if args.command == "schedule":
        return api(config, "POST", "/schedule/" + args.action, operator=True)
    if args.command == "strategy" and args.action == "list":
        return api(config, "GET", "/strategies")
    raise DomainError("CLI_COMMAND_INVALID", "不支持的命令", 422)


def main():
    args = parser().parse_args()
    try:
        config = load_config(args.config)
        result = execute(args, config)
        success = result.get("configuration_complete", result.get("ready", True))
        exit_code = 0 if success else 1
    except DomainError as exc:
        result, exit_code = {"error": {"code": exc.code, "message": exc.message}}, 1
    except (
        ValidationError,
        ValueError,
        OSError,
        TimeoutError,
        subprocess.SubprocessError,
        httpx.HTTPError,
        SQLAlchemyError,
    ):
        result, exit_code = (
            {
                "error": {
                    "code": "CLI_OPERATION_FAILED",
                    "message": "配置或连接无效；未输出敏感诊断",
                }
            },
            1,
        )
    print(json.dumps(json_value(result), ensure_ascii=False, indent=None if args.json else 2))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
