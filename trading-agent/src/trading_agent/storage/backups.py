"""Human/backup-user operations, independent of runtime and model tools."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from trading_agent.runtime.ownership import LOCK_ID, LOCK_NAMESPACE


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            result.update(chunk)
    return result.hexdigest()


def connection_info(url):
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("备份或恢复环境缺少PostgreSQL连接")
    info = conninfo_to_dict(url.replace("postgresql+psycopg://", "postgresql://", 1))
    if not info.get("dbname") or info["dbname"] in {"postgres", "template0", "template1"}:
        raise ValueError("必须指定独立业务数据库")
    return info


@contextmanager
def postgres_environment(info):
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "HOME"}}
    names = {
        "host": "PGHOST",
        "port": "PGPORT",
        "user": "PGUSER",
        "dbname": "PGDATABASE",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
    }
    env.update({name: info[key] for key, name in names.items() if key in info})
    env["PGCONNECT_TIMEOUT"] = "3"
    # Never put a password/DSN in argv, PGPASSWORD, reports or runtime environment.
    with tempfile.TemporaryDirectory(prefix="ta-backup-credentials-") as folder:
        if info.get("password"):
            parts = [
                info.get("host", "localhost"),
                info.get("port", "5432"),
                info["dbname"],
                info.get("user", "*"),
                info["password"],
            ]
            if any("\n" in value or "\r" in value for value in parts):
                raise ValueError("密码文件字段不可包含换行")
            escaped = [value.replace("\\", "\\\\").replace(":", "\\:") for value in parts]
            path = Path(folder) / "pgpass"
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(":".join(escaped) + "\n")
            env["PGPASSFILE"] = str(path)
        yield env


def pg_binary(name, bin_dir=None):
    found = str(bin_dir / name) if bin_dir else shutil.which(name)
    if not found or not Path(found).is_file():
        raise ValueError("缺少PostgreSQL16备份工具；可显式指定--bin-dir")
    result = subprocess.run([found, "--version"], capture_output=True, timeout=5, check=True)
    if not re.search(rb"\(PostgreSQL\) 16\.", result.stdout):
        raise ValueError("备份工具必须为PostgreSQL16")
    return found


def run_pg(argv, env):
    # PostgreSQL diagnostics can contain SQL/source data; do not forward them to logs or CLI.
    subprocess.run(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=900,
        check=True,
    )


def retain(folder, prefix, count):
    pattern = re.compile(rf"{prefix}-\d{{4}}-\d{{2}}-\d{{2}}\.dump")
    files = sorted(
        (
            path
            for path in folder.iterdir()
            if pattern.fullmatch(path.name) and path.is_file() and not path.is_symlink()
        ),
        reverse=True,
    )
    for path in files[count:]:
        path.unlink()
        path.with_suffix(".sha256").unlink(missing_ok=True)


def backup(url, directory, *, bin_dir=None, at=None):
    info = connection_info(url)
    directory = directory.resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError("备份目录必须仅操作用户可读写")
    today = (at or datetime.now(UTC)).astimezone(UTC).date()
    target = directory / f"daily-{today.isoformat()}.dump"
    if target.exists() or target.is_symlink():
        raise ValueError("今日备份已存在，拒绝覆盖")
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=directory) as temp:
        pending = Path(temp) / "database.dump"
        with postgres_environment(info) as env:
            run_pg(
                [
                    pg_binary("pg_dump", bin_dir),
                    "--format=custom",
                    "--no-acl",
                    "--lock-wait-timeout=5000",
                    "--file",
                    str(pending),
                ],
                env,
            )
            run_pg([pg_binary("pg_restore", bin_dir), "--list", str(pending)], env)
        pending.chmod(0o600)
        checksum = digest(pending)
        with pending.open("rb") as handle:
            os.fsync(handle.fileno())
        os.link(pending, target)  # Atomic publication; never replaces an existing backup.
        target.with_suffix(".sha256").write_text(checksum + "\n", encoding="ascii")
        target.with_suffix(".sha256").chmod(0o600)
        if today.weekday() == 6:
            weekly = directory / f"weekly-{today.isoformat()}.dump"
            os.link(target, weekly)
            weekly.with_suffix(".sha256").write_text(checksum + "\n", encoding="ascii")
            weekly.with_suffix(".sha256").chmod(0o600)
    retain(directory, "daily", 7)
    retain(directory, "weekly", 4)
    return {"archive": str(target), "sha256": checksum, "daily_retention": 7, "weekly_retention": 4}


def restore(url, archive, *, expected_database, bin_dir=None):
    info = connection_info(url)
    if info["dbname"] != expected_database:
        raise ValueError("恢复连接与显式目标数据库不一致")
    if archive.is_symlink() or not stat.S_ISREG(archive.stat().st_mode):
        raise ValueError("恢复文件必须是普通备份文件")
    if digest(archive) != archive.with_suffix(".sha256").read_text(encoding="ascii").strip():
        raise ValueError("备份校验失败")
    with psycopg.connect(
        **info, connect_timeout=3, options="-c statement_timeout=5000 -c lock_timeout=2000"
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_NAMESPACE, LOCK_ID))
            if not cursor.fetchone()[0]:
                raise ValueError("恢复目标仍有runtime持锁")
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema') "
                "OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema')"
            )
            if cursor.fetchone()[0]:
                raise ValueError("只允许恢复到新建空库，禁止覆盖已有数据库")
        # This is a session advisory lock: committing the checks keeps the lock held
        # while avoiding an open database transaction across the external process.
        connection.commit()
        with postgres_environment(info) as env:
            run_pg(
                [
                    pg_binary("pg_restore", bin_dir),
                    "--dbname",
                    expected_database,
                    "--single-transaction",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    str(archive),
                ],
                env,
            )
    return {"restored": True, "database": expected_database, "runtime_started": False}


def main():
    parser = argparse.ArgumentParser(
        description="Independent PostgreSQL16 backup/empty-target restore"
    )
    parser.add_argument("--bin-dir", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("backup").add_argument("--directory", type=Path, required=True)
    item = sub.add_parser("restore")
    item.add_argument("--archive", type=Path, required=True)
    item.add_argument("--expected-database", required=True)
    args = parser.parse_args()
    try:
        if args.action == "backup":
            result = backup(
                os.environ.get("TA_BACKUP_DATABASE_URL", ""), args.directory, bin_dir=args.bin_dir
            )
        else:
            result = restore(
                os.environ.get("TA_RESTORE_DATABASE_URL", ""),
                args.archive,
                expected_database=args.expected_database,
                bin_dir=args.bin_dir,
            )
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        parser.exit(1, "备份或恢复失败；未输出连接凭据，未启动runtime。\n")


if __name__ == "__main__":
    main()
