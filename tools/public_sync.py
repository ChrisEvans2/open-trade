"""Publish an independently sanitized file tree; never transfer source Git history."""

from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class Blocked(Exception):
    """Only fixed reason codes and already-public paths may reach the journal."""


NEW_PYTHON = (
    "packages/trading-core/src/",
    "packages/trading-quant/src/",
    "trading-agent/src/",
    "trading-agent/tests/",
    "trading-agent/integrations/",
    "trading-agent/migrations/",
    "tests/",
)
PUBLIC_ONLY = {
    "trading-agent/deploy/hermes-paper-runtime.service",
    "trading-agent/config.yaml",
    "changelog.md",
    *(
        f"config/{name}.yaml"
        for name in (
            "app",
            "exchange",
            "risk",
            "runtime_state",
            "social_sources",
            "strategy",
            "symbols",
        )
    ),
}
BAD_PARTS = {".git", ".local", ".venv", "__pycache__", "secrets", "state", "logs"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def clean_env(home):
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONNOUSERSITE": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=yes",
    }


def command(args, cwd, *, env=None, log=None, timeout=180, accepted=(0,)):
    result = subprocess.run(
        [str(a) for a in args],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if log:
        log.write_bytes(result.stdout + result.stderr)
        log.chmod(0o600)
    if result.returncode not in accepted:
        raise Blocked("COMMAND_FAILED:" + Path(str(args[0])).name)
    return result


def git(root, *args, env=None, accepted=(0,)):
    try:
        return command(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            root,
            env=env,
            accepted=accepted,
        ).stdout
    except Blocked:
        raise Blocked("GIT_FAILED:" + args[0]) from None


def head(root, env=None):
    result = command(
        ["git", "rev-parse", "--verify", "HEAD"], root, env=env, accepted=(0, 128)
    )
    return result.stdout.decode().strip() if result.returncode == 0 else None


def safe_path(name):
    p = Path(name)
    if (
        p.is_absolute()
        or ".." in p.parts
        or any(part in BAD_PARTS for part in p.parts)
        or not re.fullmatch(r"[\w./-]+", name)
    ):
        raise Blocked("UNSAFE_PATH")
    return p


def read_file(root, name):
    p = root / safe_path(name)
    for ancestor in (p, *p.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise Blocked("SYMLINK:" + name)
    data = p.read_bytes()
    if len(data) > 2_000_000 or b"\0" in data:
        raise Blocked("NON_TEXT_OR_OVERSIZE:" + name)
    data.decode("utf-8")
    return data


def public_tree(root, env=None):
    names = git(
        root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", env=env
    )
    return {
        name: read_file(root, name)
        for name in sorted(set(filter(None, names.decode().split("\0"))))
        if (root / safe_path(name)).exists() or (root / safe_path(name)).is_symlink()
    }


def discover_source(root, managed, env=None):
    names = git(
        root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", env=env
    )
    selected = set(managed)
    for name in filter(None, names.decode().split("\0")):
        if name.startswith(NEW_PYTHON) and name.endswith(".py"):
            selected.add(name)
        if name.startswith("trading-agent/prompts/structural/") and name.endswith(
            ".md"
        ):
            selected.add(name)
    return {
        name: read_file(root, name)
        for name in sorted(selected - PUBLIC_ONLY)
        if (root / safe_path(name)).exists() or (root / safe_path(name)).is_symlink()
    }


def hashes(tree):
    return {name: digest(data) for name, data in tree.items()}


def signature(tree):
    return digest(json.dumps(hashes(tree), sort_keys=True).encode())


def merge_content(public_base, source_base, source_new, folder):
    if public_base == source_base:
        return source_new
    folder.mkdir(parents=True, exist_ok=True)
    paths = [folder / name for name in ("public", "ancestor", "source")]
    for p, data in zip(paths, (public_base, source_base, source_new), strict=True):
        p.write_bytes(data)
    result = command(["git", "merge-file", "-p", *paths], folder, accepted=(0, 1, 2))
    if result.returncode:
        raise Blocked("SANITIZATION_CONFLICT")
    return result.stdout


def plan(source_base, public_base, source_new, current_public, folder):
    if hashes(current_public) != hashes(public_base):
        raise Blocked("PUBLIC_WORKTREE_CHANGED")
    result = dict(public_base)
    changed = []
    for name in sorted(set(source_base) | set(source_new)):
        old, new = source_base.get(name), source_new.get(name)
        if old == new:
            continue
        if old is None:
            if name in result:
                raise Blocked("NEW_PATH_COLLISION:" + name)
            result[name] = new
        elif new is None:
            result.pop(name, None)
        else:
            result[name] = merge_content(
                public_base[name], old, new, folder / digest(name.encode())[:16]
            )
        changed.append(name)
    return result, changed


def privacy_scan(tree, *, source_root=None, blocked=()):
    patterns = (
        r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
        (r"/" r"Users/|/" r"root/|/data/" r"hermes-trade|/data/" r"trade"),
        r"https?://(?:app\.notion\.com|(?:www\.)?notion\.so)/",
        r"\b(?:ghp_|github_pat_|sk-proj-)[A-Za-z0-9_-]{16,}",
        r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b",
        r"\bsleepy(?:-okx|@)",
        r"(?:auth\.json|(?:id_rsa|id_ed25519)(?:\.pub)?)$",
    )
    for name, data in tree.items():
        safe_path(name)
        if name.startswith(".gitleaks"):
            raise Blocked("UNAPPROVED_SCAN_CONFIG")
        text = data.decode("utf-8")
        if any(re.search(p, text) for p in patterns[:-1]):
            raise Blocked("PRIVACY_PATTERN:" + name)
        if re.search(patterns[-1], name):
            raise Blocked("CREDENTIAL_FILENAME")
        if source_root and str(source_root) in text:
            raise Blocked("SOURCE_PATH:" + name)
        if any(value and value in text for value in blocked):
            raise Blocked("PERSONAL_IDENTITY:" + name)
        if name.endswith((".md", ".html", ".json")):
            if re.search(
                r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", text, re.IGNORECASE
            ):
                raise Blocked("PRIVATE_FACT_IDENTIFIER:" + name)
            if re.search(r'"narrative"\s*:\s*"[^"\n]{16,}', text):
                raise Blocked("RAW_MODEL_NARRATIVE:" + name)
            if re.search(
                r'"(?:account_equity|realized_pnl|unrealized_pnl|account_balance)"\s*:\s*-?\d',
                text,
            ):
                raise Blocked("RAW_ACCOUNT_FACT:" + name)
            if re.search(
                r"当前(?:模拟)?账户持仓数\s*为\s*\d|[两一二三四五六七八九\d]+个模拟持仓均\s*(?:CLOSED|OPEN)",
                text,
            ):
                raise Blocked("PRIVATE_POSITION_STATUS:" + name)
            if re.search(r"(?:库内|原业务事实计数)[^\n]*\d+\s*条交易意图", text):
                raise Blocked("PRIVATE_EXECUTION_HISTORY:" + name)
        literal = re.compile(
            r"""(?im)(?:^|[\s{,])["']?(?:[A-Z0-9_]*(?:API_KEY|API_SECRET|BOT_TOKEN|"""
            r"""ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|PASSPHRASE)|api_key|api_secret|password|"""
            r"""passphrase|access_token|refresh_token|bot_token)["']?\s*[:=]\s*["']([^"'\n]+)["']"""
        )
        for match in literal.finditer(text):
            value = match.group(1)
            if not re.match(
                r"(?i)^(fixture(?:[-_]|$)|dummy$|CHANGE_ME$|your[-_]|example[-_])",
                value,
            ):
                raise Blocked("CREDENTIAL_LITERAL:" + name)
        for match in re.finditer(
            r"postgres(?:ql)?(?:\+\w+)?://[^\s/:]+:([^\s/@]+)@", text
        ):
            if not re.match(r"(?i)^(fixture[-_]|CHANGE_ME$|your[-_])", match.group(1)):
                raise Blocked("DATABASE_PASSWORD:" + name)
        if name.endswith(".py"):
            try:
                ast.parse(text, filename=name)
            except SyntaxError:
                raise Blocked("INVALID_PYTHON:" + name) from None


def write_tree(root, tree):
    for name, data in tree.items():
        p = root / safe_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def read_base(ops, kind, names):
    return {name: read_file(ops / "baseline" / kind, name) for name in names}


def store_baseline(ops, source, public):
    write_tree(ops / "baseline/source", source)
    write_tree(ops / "baseline/public", public)


def root_guard(config, env):
    source, target = Path(config["source"]), Path(config["target"])
    if (
        source.resolve() == target.resolve()
        or source in target.parents
        or target in source.parents
    ):
        raise Blocked("ROOTS_NOT_INDEPENDENT")
    for root in (source, target):
        actual = git(root, "rev-parse", "--show-toplevel", env=env).decode().strip()
        if Path(actual).resolve() != root.resolve() or (root / ".git").is_symlink():
            raise Blocked("UNEXPECTED_REPOSITORY")
    if config.get("source_branch"):
        branch = (
            git(source, "symbolic-ref", "--short", "HEAD", env=env).decode().strip()
        )
        if branch != config["source_branch"]:
            raise Blocked("SOURCE_BRANCH_CHANGED")
    if config.get("source_remote"):
        url = git(source, "remote", "get-url", "origin", env=env).decode().strip()
        if url != config["source_remote"]:
            raise Blocked("SOURCE_REMOTE_CHANGED")
    if (
        git(target, "symbolic-ref", "--short", "HEAD", env=env).decode().strip()
        != "main"
    ):
        raise Blocked("WRONG_PUBLIC_BRANCH")
    for option in ((), ("--push",)):
        url = (
            git(target, "remote", "get-url", *option, "origin", env=env)
            .decode()
            .strip()
        )
        if url not in config["remote_urls"]:
            raise Blocked("REMOTE_CHANGED")
    name = git(target, "config", "--local", "user.name", env=env).decode().strip()
    email = git(target, "config", "--local", "user.email", env=env).decode().strip()
    if name != config["user_name"] or digest(email.encode()) != config["email_hash"]:
        raise Blocked("PUBLIC_IDENTITY_CHANGED")
    return source, target, name, email


def index_guard(target, expected_head, env):
    if expected_head:
        result = command(
            ["git", "diff", "--cached", "--quiet"], target, env=env, accepted=(0, 1)
        )
        if result.returncode:
            raise Blocked("PUBLIC_INDEX_CHANGED")
    else:
        names = git(target, "ls-files", "-z", env=env).decode().split("\0")
        for name in filter(None, names):
            if git(target, "show", ":" + name, env=env) != read_file(target, name):
                raise Blocked("PUBLIC_INDEX_CHANGED")


def remote_head(target, env):
    rows = (
        git(target, "ls-remote", "origin", "refs/heads/main", env=env)
        .decode()
        .splitlines()
    )
    return rows[0].split()[0] if rows else None


def push(config, state, ops, env):
    target = Path(config["target"])
    if head(target, env) != state["head"]:
        raise Blocked("PUBLIC_HEAD_CHANGED")
    remote = remote_head(target, env)
    if remote not in (state.get("pushed"), state["head"]):
        raise Blocked("REMOTE_DIVERGED")
    if remote != state["head"]:
        git(target, "push", "origin", "main:main", env=env)
    if remote_head(target, env) != state["head"]:
        raise Blocked("PUSH_NOT_CONFIRMED")
    state["pushed"] = state["head"]
    save(ops / "state.json", state)


def scan_with_gitleaks(config, tree, run, env):
    if (
        config.get("gitleaks_hash")
        and digest(Path(config["gitleaks"]).read_bytes()) != config["gitleaks_hash"]
    ):
        raise Blocked("SCANNER_CHANGED")
    snapshot = run / "scan"
    write_tree(snapshot, tree)
    command(
        [
            config["gitleaks"],
            "dir",
            snapshot,
            "--no-banner",
            "--redact=100",
            "--report-format=json",
            "--report-path=" + str(run / "gitleaks.json"),
        ],
        run,
        env=env,
        log=run / "gitleaks.log",
        accepted=(0, 1),
    )
    if json.loads((run / "gitleaks.json").read_text()):
        raise Blocked("SECRET_SCAN_FINDINGS")


def run_validation(config, candidate, run, env):
    """Fresh editable installs ensure subprocesses also load the candidate, not production."""
    import pwd
    import socket
    import tempfile

    gate_env = dict(env)
    gate_env["UV_CACHE_DIR"] = config["uv_cache"]
    for cwd, extra in [
        (candidate, ["--extra", "dev"]),
        (candidate / "trading-agent", ["--extra", "cube", "--group", "hermes-test"]),
    ]:
        command(
            [
                config["uv"],
                "sync",
                "--frozen",
                "--offline",
                "--python",
                config["python"],
                *extra,
            ],
            cwd,
            env=gate_env,
            log=run / ("install-" + cwd.name + ".log"),
        )
    runtime = candidate / "trading-agent"
    python = runtime / ".venv/bin/python"
    command(
        [runtime / ".venv/bin/ruff", "check", "src", "tests", "integrations", "deploy"],
        runtime,
        env=gate_env,
        log=run / "ruff.log",
    )
    command(
        [
            runtime / ".venv/bin/ruff",
            "format",
            "--check",
            "src",
            "tests",
            "integrations",
            "deploy",
        ],
        runtime,
        env=gate_env,
        log=run / "format.log",
    )
    gate_env["TA_HERMES_REFERENCE"] = config["hermes_reference"]
    pinned = (
        git(Path(config["hermes_reference"]), "rev-parse", "HEAD", env=env)
        .decode()
        .strip()
    )
    if pinned != "71cb7d60f8469768e597e39d9ddb682a4e20dbfc":
        raise Blocked("HERMES_REFERENCE_CHANGED")
    results = {}

    def pytest_group(label, cwd, executable, paths, timeout=600):
        output = command(
            [executable, "-m", "pytest", "-q", *paths, "--tb=short"],
            cwd,
            env=gate_env,
            log=run / (label + ".log"),
            timeout=timeout,
        ).stdout
        matches = re.findall(rb"(\d+) passed", output)
        if not matches or re.search(rb"\d+ skipped", output):
            raise Blocked("TESTS_INCOMPLETE:" + label)
        results[label] = int(matches[-1])

    pytest_group("core", candidate, candidate / ".venv/bin/python", ["tests"])
    if (candidate / "tools/tests/test_public_sync.py").exists():
        pytest_group(
            "maintenance", candidate, candidate / ".venv/bin/python", ["tools/tests"]
        )
    pytest_group("runtime", runtime, python, ["tests/unit", "tests/hermes"])
    port = config["pg_port"]
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise Blocked("TEST_POSTGRES_PORT_BUSY")
    pg = Path(tempfile.mkdtemp(prefix="open-trade-sync-pg-"))
    owner = pwd.getpwnam(config["pg_os_user"])
    os.chown(pg, owner.pw_uid, owner.pw_gid)
    pg_bin = Path(config["pg_bin"])
    prefix = ["/usr/sbin/runuser", "-u", config["pg_os_user"], "--"]
    pg_env = {**env, "LC_ALL": "C", "PATH": str(pg_bin) + ":" + env["PATH"]}
    started = False
    try:
        command(
            [
                *prefix,
                pg_bin / "initdb",
                "-D",
                pg / "data",
                "-U",
                "public_sync_test",
                "-A",
                "trust",
                "--no-locale",
                "--encoding=UTF8",
            ],
            run,
            env=pg_env,
            log=run / "pg-init.log",
        )
        command(
            [
                *prefix,
                pg_bin / "pg_ctl",
                "-D",
                pg / "data",
                "-l",
                pg / "server.log",
                "-o",
                f"-p {port} -h 127.0.0.1 -k {pg} -c timezone=UTC -c jit=off",
                "-w",
                "start",
            ],
            run,
            env=pg_env,
            log=run / "pg-start.log",
        )
        started = True
        gate_env["PATH"] = pg_env["PATH"]
        gate_env["TA_TEST_DATABASE_URL"] = (
            f"postgresql+psycopg://public_sync_test@127.0.0.1:{port}/postgres"
        )
        baseline = set(config["pg_tests"])
        baseline.update(
            p
            for p in config.get("changed", [])
            if p.startswith("trading-agent/tests/integration/")
            and p.endswith(".py")
            and (candidate / p).exists()
        )
        pytest_group(
            "postgres",
            runtime,
            python,
            [str(Path(p).relative_to("trading-agent")) for p in sorted(baseline)],
            900,
        )
        check = (
            "import os,psycopg; "
            "u=os.environ['TA_TEST_DATABASE_URL']; "
            "u=u.replace('postgresql+psycopg://','postgresql://'); "
            "c=psycopg.connect(u,autocommit=True); "
            'n=c.execute("SELECT count(*) FROM pg_database WHERE '
            "datname LIKE 'ta_test_%' OR datname LIKE 'ta_restore_test_%'\").fetchone()[0]; "
            "assert n==0; c.close(); print('temporary databases: 0')"
        )
        command([python, "-c", check], run, env=gate_env, log=run / "pg-cleanup.log")
    finally:
        if started:
            command(
                [
                    *prefix,
                    pg_bin / "pg_ctl",
                    "-D",
                    pg / "data",
                    "-m",
                    "fast",
                    "-w",
                    "stop",
                ],
                run,
                env=pg_env,
                log=run / "pg-stop.log",
            )
        if (pg / "server.log").exists():
            shutil.copyfile(pg / "server.log", run / "pg-server.log")
        shutil.rmtree(pg)
    save(run / "validation.json", results)
    return results


def initialize(config, ops):
    """Adopt only the previously reviewed, independently exported working tree."""
    if (ops / "state.json").exists():
        raise Blocked("ALREADY_INITIALIZED")
    env = clean_env(ops / "home")
    (ops / "home").mkdir(exist_ok=True)
    source, target, _, _ = root_guard(config, env)
    if head(target, env) or remote_head(target, env):
        raise Blocked("BOOTSTRAP_REQUIRES_EMPTY_PUBLIC_HISTORY")
    public = public_tree(target, env)
    original = json.loads(Path(config["export_manifest"]).read_text())
    source_base = {}
    for row in original:
        name = row["path"]
        if name in PUBLIC_ONLY:
            continue
        data = read_file(source, name)
        if digest(data) != row["source_sha256"]:
            data = git(
                source, "show", config["export_source_commit"] + ":" + name, env=env
            )
        if digest(data) != row["source_sha256"] or name not in public:
            raise Blocked("EXPORT_BASELINE_UNAVAILABLE:" + name)
        source_base[name] = data
    privacy_scan(public, source_root=source, blocked=config.get("blocked", []))
    store_baseline(ops, source_base, public)
    state = {
        "source": hashes(source_base),
        "public": hashes(public),
        "head": None,
        "pushed": None,
        "bootstrap_feature_hash": digest((target / "Feature.md").read_bytes()),
    }
    save(ops / "state.json", state)


def status(ops, phase, **details):
    value = {
        "phase": phase,
        "at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        **details,
    }
    save(ops / "status.json", value)
    print(json.dumps(value, ensure_ascii=False), flush=True)


def commit_and_install(config, state, ops, run, candidate, source_new, tree, env):
    source, target, name, email = root_guard(config, env)
    if (
        head(target, env) != state["head"]
        or hashes(public_tree(target, env)) != state["public"]
    ):
        raise Blocked("PUBLIC_CHANGED_DURING_VALIDATION")
    index_guard(target, state["head"], env)
    current_source = discover_source(source, source_new, env)
    if signature(current_source) != signature(source_new):
        raise Blocked("SOURCE_CHANGED_DURING_VALIDATION")
    if remote_head(target, env) != state.get("pushed"):
        raise Blocked("REMOTE_CHANGED_DURING_VALIDATION")
    git(candidate, "config", "user.name", name, env=env)
    git(candidate, "config", "user.email", email, env=env)
    git(candidate, "add", "--all", env=env)
    git(candidate, "diff", "--cached", "--check", env=env)
    message = run / "commit-message.txt"
    validation = json.loads((run / "validation.json").read_text())
    message.write_text(
        (
            "docs: publish reviewed project sources and complete documentation\n\n"
            if config.get("reviewed_export")
            else "sync: publish verified public framework\n\n"
        )
        + "Apply the reviewed project allowlist and preserve public adaptations.\n"
        "Retain full technical documentation and archived sources where reviewed; "
        "operator evidence is redacted and examples stay paused.\n"
        "Secret and identity scans passed.\n"
        f"Verification: core {validation['core']}; runtime/native {validation['runtime']}; "
        f"targeted real PostgreSQL {validation['postgres']} passed.\n"
        "No private commit ancestry, operator data or deployment credentials are included.\n"
        "Live model, order execution and profitability remain outside this validation.\n"
    )
    git(candidate, "commit", "-F", str(message), env=env)
    published = head(candidate, env)
    parent = (
        git(candidate, "show", "-s", "--format=%P", "HEAD", env=env).decode().strip()
    )
    if parent != (state["head"] or ""):
        raise Blocked("UNEXPECTED_PUBLIC_ANCESTRY")
    # Persist recovery data before touching the public ref. A push failure never undoes a commit.
    journal = {
        "source": hashes(source_new),
        "public": hashes(tree),
        "head": published,
        "pushed": state.get("pushed"),
        "candidate": str(candidate),
    }
    save(ops / "install-journal.json", journal)
    write_tree(run / "next-source", source_new)
    write_tree(run / "next-public", tree)
    git(target, "fetch", "--no-tags", str(candidate), "main", env=env)
    if state["head"]:
        git(target, "merge", "--ff-only", "FETCH_HEAD", env=env)
    else:
        # The initial index is the exact previously approved export; CAS creates a new root.
        git(target, "read-tree", "--reset", "-u", published, env=env)
        git(target, "update-ref", "refs/heads/main", published, "0" * 40, env=env)
    if hashes(public_tree(target, env)) != hashes(tree):
        raise Blocked("INSTALL_TREE_MISMATCH")
    store_baseline(ops, source_new, tree)
    state.update(source=hashes(source_new), public=hashes(tree), head=published)
    save(ops / "state.json", state)
    (ops / "install-journal.json").unlink()
    clear_worklog(target, run, state)
    status(ops, "COMMITTED", commit=published[:12])
    push(config, state, ops, env)
    return state


def recover_install(config, state, ops, env):
    journal_path = ops / "install-journal.json"
    if not journal_path.exists():
        return state
    journal = json.loads(journal_path.read_text())
    target = Path(config["target"])
    actual_head = head(target, env)
    actual_tree = hashes(public_tree(target, env))
    if actual_head == state["head"] and actual_tree == state["public"]:
        # No working files/ref changed. The validated candidate can be rebuilt safely.
        journal_path.unlink()
        return state
    if actual_tree != journal["public"]:
        raise Blocked("INSTALL_RECOVERY_CONFLICT")
    if actual_head == state["head"]:
        git(
            target,
            "update-ref",
            "refs/heads/main",
            journal["head"],
            state["head"] or "0" * 40,
            env=env,
        )
    elif actual_head != journal["head"]:
        raise Blocked("INSTALL_RECOVERY_HEAD_CONFLICT")
    run = Path(journal["candidate"]).parent
    source = read_base_from_tree(run / "next-source", journal["source"])
    public = read_base_from_tree(run / "next-public", journal["public"])
    store_baseline(ops, source, public)
    state.update(
        source=journal["source"], public=journal["public"], head=journal["head"]
    )
    save(ops / "state.json", state)
    journal_path.unlink()
    clear_worklog(target, run, state)
    return state


def read_base_from_tree(root, expected):
    tree = {name: read_file(root, name) for name in expected}
    if hashes(tree) != expected:
        raise Blocked("RECOVERY_BASELINE_CORRUPT")
    return tree


def worklog(target, run, paths):
    feature = target / "Feature.md"
    before = feature.read_bytes() if feature.exists() else b""
    (run / "Feature.before").write_bytes(before)
    block = (
        f"\n<!-- public-sync:{run.name} -->\n"
        f"## 自动公开同步（未提交）\n\n"
        f"- 本轮处理{len(paths)}个白名单框架变更，重新应用公开适配。\n"
        "- 候选树须通过密钥/身份扫描、单元/原生契约、隔离真实PG和暂存检查；"
        "不运行模型、真实订单或业务服务。验证失败保留记录，不发布。\n"
        "- 回滚采用公开仓反向提交，保留业务事实；不得同步私人Git历史。\n"
        "<!-- /public-sync -->\n"
    )
    feature.write_bytes(before + block.encode())


def clear_worklog(target, run, state):
    feature = target / "Feature.md"
    before = (run / "Feature.before").read_bytes()
    content = re.sub(
        r"\n<!-- public-sync:[^>]+ -->.*?<!-- /public-sync -->\n",
        "",
        feature.read_text(),
        flags=re.DOTALL,
    )
    initial = before.decode()
    if digest(before) == state.get("bootstrap_feature_hash") and content.startswith(
        initial
    ):
        content = content[len(initial) :]
    feature.write_text(
        content.strip() + "\n" if content.strip() else "当前无待提交改动\n"
    )


def prune_scratch(ops):
    """Keep logs/results; remove only this tool's disposable candidate environments."""
    runs = ops / "runs"
    if not runs.exists():
        return
    folders = sorted(p for p in runs.iterdir() if p.is_dir())
    protected = set(folders[-1:])
    journal = ops / "install-journal.json"
    if journal.exists():
        protected.add(Path(json.loads(journal.read_text())["candidate"]).parent)
    for folder in folders:
        if folder in protected or not re.fullmatch(
            r"\d{8}T\d{6}-[0-9a-f]{8}", folder.name
        ):
            continue
        for name in (
            "candidate",
            "scan",
            "merge",
            "next-source",
            "next-public",
            "home",
        ):
            path = folder / name
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)


def add_changelog(tree, changed, results, bootstrap, reviewed=False):
    tree = dict(tree)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    text = tree["changelog.md"].decode()
    if bootstrap:
        text = text.replace("尚未commit/push", "本次形成独立初始公开提交")
    entry = (
        f"\n## 自动同步 {now}\n\n"
        f"- {'建立独立初始公开版本' if bootstrap else '扩展已审查项目文件与完整技术文档' if reviewed else '更新白名单项目文件'}；"
        f"本轮{len(changed)}个源文件变化，保留公开文档、示例和脱敏适配。\n"
        f"- 验证：共享核心{results['core']}、单元/固定Hermes{results['runtime']}、"
        f"隔离真实PG{results['postgres']}项通过；密钥/身份扫描与暂存检查通过。\n"
        + (
            f"- 同步维护测试{results['maintenance']}项通过。\n"
            if "maintenance" in results
            else ""
        )
        + (
            "- 保留完整README、需求/设计/使用说明、测试目录和退役源码；"
            "删除个人标识、业务事实快照和私有验收原文，现用配置继续采用暂停示例。\n"
            if reviewed
            else ""
        )
        + "- 不含私人提交祖先、账户记录或部署凭据。未运行真实模型、订单或业务服务；"
        "不将本轮定向回归等同收益或外部live验收。\n"
        "- 如需回滚，反向提交本批公开代码；数据库事实保留并向前修复。\n\n"
    )
    tree["changelog.md"] = (
        text.replace("# Changelog\n", "# Changelog\n" + entry, 1).rstrip() + "\n"
    ).encode()
    return tree


def reviewed_tree(spec_path, ops, state, source, env):
    """Explicit review binds exact source bytes and public base; all gates still run."""
    spec_path = Path(spec_path)
    if not spec_path.resolve().is_relative_to(ops.resolve()):
        raise Blocked("REVIEW_OUTSIDE_OPERATIONS_DIRECTORY")
    spec = json.loads(spec_path.read_text())
    if spec["head"] != state["head"] or spec["public_base"] != state["public"]:
        raise Blocked("STALE_REVIEW_PUBLIC_BASE")
    if not set(state["source"]).issubset(spec["source"]):
        raise Blocked("REVIEW_DROPS_MANAGED_SOURCE")
    source_new = discover_source(source, spec["source"], env)
    if hashes(source_new) != spec["source"]:
        raise Blocked("STALE_REVIEW_SOURCE")
    tree = read_base_from_tree(spec_path.parent / "tree", spec["public"])
    if not set(state["public"]).issubset(tree):
        raise Blocked("REVIEW_DROPS_PUBLIC_FILE")
    changed = sorted(
        name
        for name in set(state["source"]) | set(source_new)
        if state["source"].get(name) != spec["source"].get(name)
    )
    return source_new, tree, changed


def run_once(config, ops, *, now=False, review=None):
    prune_scratch(ops)
    env = clean_env(ops / "home")
    source, target, _, _ = root_guard(config, env)
    state = json.loads((ops / "state.json").read_text())
    state = recover_install(config, state, ops, env)
    if head(target, env) != state["head"]:
        raise Blocked("PUBLIC_HEAD_CHANGED")
    index_guard(target, state["head"], env)
    if hashes(public_tree(target, env)) != state["public"]:
        raise Blocked("PUBLIC_WORKTREE_CHANGED")
    if state["head"] != state.get("pushed"):
        status(ops, "RETRYING_PUSH", commit=state["head"][:12])
        push(config, state, ops, env)
        status(ops, "PUSHED", commit=state["head"][:12])
    if remote_head(target, env) != state.get("pushed"):
        raise Blocked("REMOTE_DIVERGED")
    if review:
        source_new, reviewed, changed = reviewed_tree(review, ops, state, source, env)
        config = {**config, "reviewed_export": True}
    else:
        source_new = discover_source(source, state["source"], env)
    if not review and hashes(source_new) == state["source"] and state["head"]:
        status(ops, "UP_TO_DATE", commit=state["head"][:12])
        return
    stamp = signature(source_new)
    observation_path = ops / "observation.json"
    observation = (
        json.loads(observation_path.read_text()) if observation_path.exists() else {}
    )
    if observation.get("signature") != stamp:
        observation = {"signature": stamp, "since": time.time()}
        save(observation_path, observation)
    if not now and time.time() - observation["since"] < config.get(
        "quiet_seconds", 120
    ):
        status(ops, "WAITING_FOR_STABLE_FILES")
        return
    failure_path = ops / "failure.json"
    failure = json.loads(failure_path.read_text()) if failure_path.exists() else {}
    if (
        not now
        and failure.get("signature") == stamp
        and time.time() - failure.get("at", 0) < config.get("retry_seconds", 900)
    ):
        status(ops, "RETRY_BACKOFF")
        return
    run = (
        ops
        / "runs"
        / (
            datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%S")
            + "-"
            + os.urandom(4).hex()
        )
    )
    run.mkdir(parents=True, mode=0o700)
    (run / "home").mkdir(mode=0o700)
    status(ops, "PREPARING", run=run.name)
    candidate = run / "candidate"
    source_base = read_base(ops, "source", state["source"])
    public_base = read_base(ops, "public", state["public"])
    if review:
        tree = reviewed
    else:
        tree, changed = plan(
            source_base,
            public_base,
            source_new,
            public_tree(target, env),
            run / "merge",
        )
    blocked = list(config.get("blocked", []))
    for line in (
        git(source, "log", "--format=%an%x00%ae%x00%ce", env=env).decode().splitlines()
    ):
        values = line.split("\0")
        blocked.extend(
            v for v in values if "@" in v or re.search(r"[\u4e00-\u9fff]", v)
        )
    privacy_scan(tree, source_root=source, blocked=blocked)
    scan_with_gitleaks(config, tree, run, env)
    candidate.mkdir()
    if state["head"]:
        git(
            run,
            "clone",
            "--shared",
            "--no-hardlinks",
            str(target),
            str(candidate),
            env=env,
        )
        for name in public_tree(candidate, env):
            if name not in tree:
                (candidate / safe_path(name)).unlink()
    else:
        git(candidate, "init", "-b", "main", env=env)
    write_tree(candidate, tree)
    for name in tree:
        original = target / name
        if not original.exists():
            original = source / name
        executable = original.exists() and original.stat().st_mode & 0o111
        (candidate / name).chmod(0o755 if executable else 0o644)
    worklog(target, run, changed)
    status(ops, "VALIDATING", run=run.name, changed_files=len(changed))
    validation_env = clean_env(run / "home")
    results = run_validation(
        {**config, "changed": changed}, candidate, run, validation_env
    )
    save(run / "validation.json", results)
    if hashes(public_tree(candidate, env)) != hashes(tree):
        raise Blocked("TESTS_CHANGED_CANDIDATE_FILES")
    tree = add_changelog(tree, changed, results, not state["head"], bool(review))
    write_tree(candidate, tree)
    privacy_scan(tree, source_root=source, blocked=blocked)
    # The second scan includes the generated commit record, with no stale scan files.
    if (run / "scan").exists():
        shutil.rmtree(run / "scan")
    scan_with_gitleaks(config, tree, run, env)
    state = commit_and_install(
        config, state, ops, run, candidate, source_new, tree, env
    )
    status(ops, "PUSHED", commit=state["head"][:12], validation=results)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument(
        "--now", action="store_true", help="Bypass debounce, never validation"
    )
    parser.add_argument("--status", action="store_true")
    parser.add_argument(
        "--reviewed-export", type=Path, help="Exact explicitly reviewed export manifest"
    )
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    if (
        config.get("runner_hash")
        and digest(Path(__file__).read_bytes()) != config["runner_hash"]
    ):
        print('{"phase":"BLOCKED","reason":"RUNNER_CHANGED"}')
        return 1
    ops = args.config.resolve().parent
    ops.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.status:
        print(
            (ops / "status.json").read_text()
            if (ops / "status.json").exists()
            else "not run"
        )
        return 0
    with (ops / "sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"phase":"ALREADY_RUNNING"}')
            return 0
        try:
            if args.initialize:
                initialize(config, ops)
                status(ops, "INITIALIZED")
            else:
                run_once(config, ops, now=args.now, review=args.reviewed_export)
            return 0
        except Blocked as exc:
            report_failure(ops, "BLOCKED", str(exc))
        except Exception as exc:  # noqa: BLE001 -- redact unexpected errors at the CLI boundary
            # Never render a traceback/subprocess output containing source or credentials.
            report_failure(ops, "ERROR", type(exc).__name__)
        return 1


def report_failure(ops, phase, reason):
    previous = (
        json.loads((ops / "status.json").read_text())
        if (ops / "status.json").exists()
        else {}
    )
    observation = (
        json.loads((ops / "observation.json").read_text())
        if (ops / "observation.json").exists()
        else {}
    )
    save(
        ops / "failure.json",
        {
            "signature": observation.get("signature"),
            "at": time.time(),
            "reason": reason,
        },
    )
    details = {"run": previous["run"]} if previous.get("run") else {}
    status(ops, phase, reason=reason, **details)


if __name__ == "__main__":
    sys.exit(main())
