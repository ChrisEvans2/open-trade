"""Start/stop only this project's disposable development PostgreSQL cluster."""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--port", type=int, default=58916)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    data = root / ".local" / "postgres"
    data.parent.mkdir(mode=0o700, exist_ok=True)
    binary = shutil.which("pg_ctl")
    if binary is None:
        candidate = Path("/opt/homebrew/opt/postgresql@16/bin/pg_ctl")
        binary = str(candidate) if candidate.is_file() else None
    if binary is None:
        raise SystemExit("PostgreSQL 16 binaries required (or set PATH)")
    bindir = Path(binary).parent
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    if args.action == "start":
        if not (data / "PG_VERSION").exists():
            subprocess.run(
                [
                    str(bindir / "initdb"),
                    "-D",
                    str(data),
                    "-A",
                    "trust",
                    "--no-locale",
                    "--encoding=UTF8",
                ],
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
            )
        status = subprocess.run(
            [binary, "-D", str(data), "status"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if status.returncode != 0:
            options = f"-p {args.port} -h 127.0.0.1 -k /tmp -c timezone=UTC"
            subprocess.run(
                [
                    binary,
                    "-D",
                    str(data),
                    "-l",
                    str(data.parent / "postgres.log"),
                    "-o",
                    options,
                    "-w",
                    "start",
                ],
                env=env,
                check=True,
            )
        # Only a local trust-auth test URL, never print deployment credentials.
        print(f"postgresql+psycopg://{getpass.getuser()}@127.0.0.1:{args.port}/postgres")
    else:
        command = [binary, "-D", str(data), args.action]
        if args.action == "stop":
            command.extend(["-m", "fast", "-w"])
        raise SystemExit(subprocess.run(command, env=env).returncode)


if __name__ == "__main__":
    main()
