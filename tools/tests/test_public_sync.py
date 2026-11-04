"""Private maintenance tests; no user credentials, model APIs or trading runtime."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "public_sync", Path(__file__).parents[1] / "public_sync.py"
)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def g(root, *args):
    return (
        subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


def put(root, name, text):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def pair(tmp_path, monkeypatch):
    source, target, remote, ops = [
        tmp_path / p for p in ("private", "public", "remote.git", "ops")
    ]
    for p in (source, target, ops):
        p.mkdir()
    for p in (source, target):
        g(p, "init", "-b", "main")
        g(p, "config", "user.name", "fixture")
        g(p, "config", "user.email", "fixture@example.test")
    g(tmp_path, "init", "--bare", str(remote))
    name = "trading-agent/src/pkg/example.py"
    put(source, name, "value = 1\n")
    put(source, "private-record.md", "fixture confidential accounting\n")
    g(source, "add", ".")
    g(source, "commit", "-m", "private fixture history")
    put(target, name, "value = 1\n")
    put(target, ".gitignore", "Feature.md\n.local/\n")
    put(target, "README.md", "# Public fixture\n")
    put(target, "changelog.md", "# Changelog\n")
    put(target, "Feature.md", "Initial framework, not committed.\n")
    g(target, "add", ".")
    g(target, "remote", "add", "origin", str(remote))
    manifest = ops / "export.json"
    sync.save(
        manifest,
        [{"path": name, "source_sha256": sync.digest((source / name).read_bytes())}],
    )
    config = {
        "source": str(source),
        "target": str(target),
        "remote_urls": [str(remote)],
        "user_name": "fixture",
        "email_hash": sync.digest(b"fixture@example.test"),
        "export_manifest": str(manifest),
        "export_source_commit": g(source, "rev-parse", "HEAD"),
        "quiet_seconds": 0,
    }
    monkeypatch.setattr(sync, "scan_with_gitleaks", lambda *a, **k: None)
    monkeypatch.setattr(
        sync, "run_validation", lambda *a, **k: {"core": 1, "runtime": 2, "postgres": 3}
    )
    sync.initialize(config, ops)
    return source, target, remote, ops, config, name


def test_nonoverlapping_merge_preserves_public_redaction(tmp_path):
    base = b"one\nprivate_path = 'operator'\nthree\nfour\nvalue = 1\n"
    public = base.replace(b"'operator'", b"'example'")
    updated = base.replace(b"value = 1", b"value = 2")
    merged = sync.merge_content(public, base, updated, tmp_path)
    assert (
        b"'example'" in merged
        and b"value = 2" in merged
        and b"'operator'" not in merged
    )


def test_overlap_blocks_instead_of_restoring_private_content(tmp_path):
    with pytest.raises(sync.Blocked, match="SANITIZATION_CONFLICT"):
        sync.merge_content(
            b"value = 'example'\n",
            b"value = 'private'\n",
            b"value = 'different-private'\n",
            tmp_path,
        )


@pytest.mark.parametrize(
    "name", ["../secret.py", "/tmp/secret.py", "src/.git/config", "src/secrets/key.py"]
)
def test_unsafe_paths_block(name):
    with pytest.raises(sync.Blocked):
        sync.safe_path(name)


@pytest.mark.parametrize(
    "content",
    [
        "/data/" + "hermes-trade/operator",
        "/" + "Users/fixture/private",
        "https://app." + "notion.com/p/fixture",
        "-----BEGIN " + "PRIVATE KEY-----",
        "fixture@private.example",
    ],
)
def test_privacy_gate_blocks_candidate(content):
    with pytest.raises(sync.Blocked):
        sync.privacy_scan(
            {"README.md": content.encode()}, blocked=["fixture@private.example"]
        )


def test_symlink_cannot_escape_source_root(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
    put(tmp_path, "real/file.py", "value = 1\n")
    with pytest.raises(sync.Blocked, match="SYMLINK"):
        sync.read_file(tmp_path, "linked/file.py")


def test_manifest_ignores_private_docs_env_and_local_tools(pair):
    source, _, _, _, _, name = pair
    put(source, "docs/private-report.md", "private\n")
    put(source, ".env", "fixture\n")
    put(source, "tools/private_sync.py", "value = 1\n")
    put(source, "trading-agent/src/pkg/new.py", "value = 2\n")
    selected = sync.discover_source(source, {name: "old"})
    assert set(selected) == {name, "trading-agent/src/pkg/new.py"}


def test_plan_deletes_only_unchanged_managed_file(tmp_path):
    result, changed = sync.plan(
        {"a.py": b"a = 1\n"},
        {"a.py": b"a = 1\n", "README.md": b"public"},
        {},
        {"a.py": b"a = 1\n", "README.md": b"public"},
        tmp_path,
    )
    assert result == {"README.md": b"public"} and changed == ["a.py"]


def test_public_local_edit_is_never_overwritten(tmp_path):
    with pytest.raises(sync.Blocked, match="PUBLIC_WORKTREE_CHANGED"):
        sync.plan(
            {"a.py": b"a = 1\n"},
            {"a.py": b"a = 1\n"},
            {"a.py": b"a = 2\n"},
            {"a.py": b"a = 3\n"},
            tmp_path,
        )


def test_new_path_collision_blocks(tmp_path):
    with pytest.raises(sync.Blocked, match="NEW_PATH_COLLISION"):
        sync.plan(
            {},
            {"a.py": b"a = 1\n"},
            {"a.py": b"a = 2\n"},
            {"a.py": b"a = 1\n"},
            tmp_path,
        )


def test_initial_publish_has_independent_history_and_clears_committed_feature(pair):
    source, target, remote, ops, config, _ = pair
    sync.run_once(config, ops, now=True)
    assert g(target, "rev-list", "--count", "HEAD") == "1"
    assert g(target, "show", "-s", "--format=%P", "HEAD") == ""
    assert "private-record.md" not in g(target, "ls-tree", "-r", "--name-only", "HEAD")
    assert g(source, "rev-parse", "HEAD") != g(target, "rev-parse", "HEAD")
    assert g(remote, "rev-parse", "refs/heads/main") == g(target, "rev-parse", "HEAD")
    assert (target / "Feature.md").read_text() == "当前无待提交改动\n"


def test_uncommitted_source_changes_sync_and_unchanged_tick_does_not_commit(pair):
    source, target, _, ops, config, name = pair
    sync.run_once(config, ops, now=True)
    put(source, name, "value = 2\n")
    sync.run_once(config, ops, now=True)
    assert (target / name).read_text() == "value = 2\n"
    assert g(target, "rev-list", "--count", "HEAD") == "2"
    sync.run_once(config, ops, now=True)
    assert g(target, "rev-list", "--count", "HEAD") == "2"


def test_validation_failure_never_changes_public_git(pair, monkeypatch):
    _, target, _, ops, config, _ = pair
    before = sync.hashes(sync.public_tree(target))

    def fail(*args):
        raise sync.Blocked("TEST_FAILED")

    monkeypatch.setattr(sync, "run_validation", fail)
    with pytest.raises(sync.Blocked, match="TEST_FAILED"):
        sync.run_once(config, ops, now=True)
    assert sync.head(target) is None and sync.hashes(sync.public_tree(target)) == before


def test_source_edit_during_validation_aborts_publication(pair, monkeypatch):
    source, target, _, ops, config, name = pair

    def mutate(*args):
        put(source, name, "value = 9\n")
        return {"core": 1, "runtime": 2, "postgres": 3}

    monkeypatch.setattr(sync, "run_validation", mutate)
    with pytest.raises(sync.Blocked, match="SOURCE_CHANGED_DURING_VALIDATION"):
        sync.run_once(config, ops, now=True)
    assert sync.head(target) is None


def test_public_edit_during_validation_is_preserved(pair, monkeypatch):
    _, target, _, ops, config, _ = pair

    def mutate(*args):
        put(target, "README.md", "user's local edit\n")
        return {"core": 1, "runtime": 2, "postgres": 3}

    monkeypatch.setattr(sync, "run_validation", mutate)
    with pytest.raises(sync.Blocked, match="PUBLIC_CHANGED_DURING_VALIDATION"):
        sync.run_once(config, ops, now=True)
    assert (target / "README.md").read_text() == "user's local edit\n"


def test_failed_push_retries_existing_commit_without_recommitting(pair, monkeypatch):
    _, target, remote, ops, config, _ = pair
    original = sync.git

    def fail_push(root, *args, **kwargs):
        if args and args[0] == "push":
            raise sync.Blocked("NETWORK_UNAVAILABLE")
        return original(root, *args, **kwargs)

    monkeypatch.setattr(sync, "git", fail_push)
    with pytest.raises(sync.Blocked, match="NETWORK_UNAVAILABLE"):
        sync.run_once(config, ops, now=True)
    sha = sync.head(target)
    assert sha and json.loads((ops / "state.json").read_text())["pushed"] is None
    monkeypatch.setattr(sync, "git", original)
    sync.run_once(config, ops, now=True)
    assert sync.head(target) == sha and g(target, "rev-list", "--count", "HEAD") == "1"
    assert g(remote, "rev-parse", "refs/heads/main") == sha


def test_user_staged_content_is_not_discarded(pair):
    _, target, _, ops, config, name = pair
    put(target, name, "value = 6\n")
    g(target, "add", name)
    put(target, name, "value = 1\n")
    with pytest.raises(sync.Blocked, match="PUBLIC_INDEX_CHANGED"):
        sync.run_once(config, ops, now=True)
    assert "value = 6" in g(target, "show", ":" + name)


def test_stability_window_defers_even_bootstrap(pair):
    _, target, _, ops, config, _ = pair
    config["quiet_seconds"] = 120
    sync.run_once(config, ops)
    assert sync.head(target) is None
    assert (
        json.loads((ops / "status.json").read_text())["phase"]
        == "WAITING_FOR_STABLE_FILES"
    )


def test_clean_test_environment_does_not_inherit_exchange_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OKX_API_KEY", "fixture-secret")
    monkeypatch.setenv("TA_DATABASE_URL", "fixture-business-database")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fixture-notification")
    env = sync.clean_env(tmp_path)
    assert not {"OKX_API_KEY", "TA_DATABASE_URL", "TELEGRAM_BOT_TOKEN"} & env.keys()


def test_new_python_and_deletion_propagate_without_private_documents(pair):
    source, target, _, ops, config, name = pair
    sync.run_once(config, ops, now=True)
    put(source, "trading-agent/src/pkg/new.py", "added = True\n")
    put(source, "docs/new-private.md", "private business facts\n")
    (source / name).unlink()
    sync.run_once(config, ops, now=True)
    assert not (target / name).exists()
    assert (target / "trading-agent/src/pkg/new.py").read_text() == "added = True\n"
    assert not (target / "docs/new-private.md").exists()


def test_remote_advance_blocks_instead_of_overwriting_public_work(pair, tmp_path):
    source, target, remote, ops, config, name = pair
    sync.run_once(config, ops, now=True)
    clone = tmp_path / "other"
    g(tmp_path, "clone", str(target), str(clone))
    g(clone, "config", "user.name", "fixture")
    g(clone, "config", "user.email", "fixture@example.test")
    put(clone, "README.md", "remote maintainer's work\n")
    g(clone, "add", ".")
    g(clone, "commit", "-m", "public change")
    g(clone, "push", str(remote), "main")
    previous = sync.head(target)
    put(source, name, "value = 3\n")
    with pytest.raises(sync.Blocked, match="REMOTE_DIVERGED"):
        sync.run_once(config, ops, now=True)
    assert sync.head(target) == previous


def test_commit_install_recovery_does_not_duplicate_commit(pair, monkeypatch):
    _, target, remote, ops, config, _ = pair
    original = sync.store_baseline

    def fail(*args):
        raise OSError("fixture interrupted after ref installation")

    monkeypatch.setattr(sync, "store_baseline", fail)
    with pytest.raises(OSError):
        sync.run_once(config, ops, now=True)
    sha = sync.head(target)
    assert sha and (ops / "install-journal.json").exists()
    monkeypatch.setattr(sync, "store_baseline", original)
    sync.run_once(config, ops, now=True)
    assert sync.head(target) == sha
    assert g(target, "rev-list", "--count", "HEAD") == "1"
    assert g(remote, "rev-parse", "refs/heads/main") == sha
    assert (target / "Feature.md").read_text() == "当前无待提交改动\n"


def test_user_feature_note_added_during_validation_survives(pair, monkeypatch):
    _, target, _, ops, config, _ = pair

    def note(*args):
        with (target / "Feature.md").open("a") as f:
            f.write("\nUser's unrelated pending note.\n")
        return {"core": 1, "runtime": 2, "postgres": 3}

    monkeypatch.setattr(sync, "run_validation", note)
    sync.run_once(config, ops, now=True)
    assert (target / "Feature.md").read_text() == "User's unrelated pending note.\n"


def test_source_branch_switch_blocks_publication(pair):
    source, target, _, ops, config, _ = pair
    config["source_branch"] = "main"
    g(source, "checkout", "-b", "experiment")
    with pytest.raises(sync.Blocked, match="SOURCE_BRANCH_CHANGED"):
        sync.run_once(config, ops, now=True)
    assert sync.head(target) is None


def test_failed_snapshot_is_backed_off_without_rerunning_tests(pair, monkeypatch):
    source, target, _, ops, config, _ = pair
    current = sync.discover_source(
        source, json.loads((ops / "state.json").read_text())["source"]
    )
    sync.save(
        ops / "observation.json", {"signature": sync.signature(current), "since": 0}
    )
    sync.save(
        ops / "failure.json",
        {"signature": sync.signature(current), "at": __import__("time").time()},
    )
    config["retry_seconds"] = 900
    sync.run_once(config, ops)
    assert sync.head(target) is None
    assert json.loads((ops / "status.json").read_text())["phase"] == "RETRY_BACKOFF"


@pytest.mark.parametrize(
    "text",
    [
        'password' + ' = "short"\n',
        'API_KEY' + ' = "short-key"\n',
        'url = "postgresql://' + 'operator:short@localhost/db"\n',
    ],
)
def test_short_credentials_are_blocked_even_without_provider_prefix(text):
    with pytest.raises(sync.Blocked):
        sync.privacy_scan({"source.py": text.encode()})


def test_explicit_fixture_credentials_do_not_disable_the_gate():
    sync.privacy_scan({"test_source.py": b'API_KEY = "fixture-key"\n'})


def reviewed_fixture(pair):
    source, target, _, ops, config, _ = pair
    sync.run_once(config, ops, now=True)
    name = "docs/技术设计.md"
    put(source, name, "# 技术设计\n\n个人安装信息\n\n## 技术主体\n一\n二\n三\n四\n")
    state = json.loads((ops / "state.json").read_text())
    files = sync.discover_source(source, {*state["source"], name})
    tree = sync.public_tree(target)
    tree[name] = "# 技术设计\n\n通用安装说明\n\n## 技术主体\n一\n二\n三\n四\n".encode()
    tree["README.md"] = "# 完整公共说明\n".encode()
    folder = ops / "reviews/current"
    sync.write_tree(folder / "tree", tree)
    spec = {
        "head": state["head"],
        "public_base": state["public"],
        "source": sync.hashes(files),
        "public": sync.hashes(tree),
    }
    sync.save(folder / "review.json", spec)
    return folder / "review.json", spec, name


def test_reviewed_docs_use_exact_redaction_then_sync_future_local_edits(pair):
    source, target, _, ops, config, _ = pair
    path, _, name = reviewed_fixture(pair)
    sync.run_once(config, ops, now=True, review=path)
    assert (
        target / name
    ).read_text() == "# 技术设计\n\n通用安装说明\n\n## 技术主体\n一\n二\n三\n四\n"
    assert (target / "README.md").read_text() == "# 完整公共说明\n"
    put(
        source,
        name,
        "# 技术设计\n\n个人安装信息\n\n## 技术主体\n一\n二\n三\n四\n\n新功能说明\n",
    )
    sync.run_once(config, ops, now=True)
    assert "新功能说明" in (target / name).read_text()
    assert "个人安装信息" not in (target / name).read_text()


@pytest.mark.parametrize("stale", ["source", "head", "public_base", "tree"])
def test_reviewed_export_rejects_stale_or_tampered_inputs(pair, stale):
    source, target, _, ops, config, _ = pair
    path, spec, name = reviewed_fixture(pair)
    before = sync.head(target)
    if stale == "source":
        put(source, name, "changed after review\n")
    elif stale == "tree":
        put(path.parent / "tree", name, "tampered reviewed content\n")
    else:
        spec[stale] = "wrong" if stale == "head" else {}
        sync.save(path, spec)
    with pytest.raises(sync.Blocked):
        sync.run_once(config, ops, now=True, review=path)
    assert sync.head(target) == before


def test_reviewed_export_does_not_bypass_privacy_checks(pair):
    _, target, _, ops, config, _ = pair
    path, spec, name = reviewed_fixture(pair)
    secret = b'API_KEY' + b' = "short-private-credential"\n'
    put(path.parent / "tree", name, secret.decode())
    spec["public"][name] = sync.digest(secret)
    sync.save(path, spec)
    before = sync.head(target)
    with pytest.raises(sync.Blocked, match="CREDENTIAL_LITERAL"):
        sync.run_once(config, ops, now=True, review=path)
    assert sync.head(target) == before


def test_reviewed_export_rechecks_newly_managed_source_after_validation(
    pair, monkeypatch
):
    source, target, _, ops, config, _ = pair
    path, _, name = reviewed_fixture(pair)
    before = sync.head(target)

    def mutate(*args):
        put(source, name, "changed during validation\n")
        return {"core": 1, "runtime": 2, "postgres": 3}

    monkeypatch.setattr(sync, "run_validation", mutate)
    with pytest.raises(sync.Blocked, match="SOURCE_CHANGED_DURING_VALIDATION"):
        sync.run_once(config, ops, now=True, review=path)
    assert sync.head(target) == before


def test_archive_is_included_only_when_explicitly_reviewed(tmp_path):
    g(tmp_path, "init", "-b", "main")
    put(tmp_path, "archive/legacy/src/example.py", "legacy = True\n")
    assert sync.discover_source(tmp_path, {}) == {}
    assert list(sync.discover_source(tmp_path, {"archive/legacy/src/example.py"})) == [
        "archive/legacy/src/example.py"
    ]


def test_broken_source_symlink_cannot_silently_be_treated_as_deletion(tmp_path):
    g(tmp_path, "init", "-b", "main")
    (tmp_path / "file.py").symlink_to(tmp_path / "missing")
    with pytest.raises(sync.Blocked, match="SYMLINK"):
        sync.discover_source(tmp_path, {"file.py"})


@pytest.mark.parametrize(
    "text",
    [
        '{"prediction_id":"11111111-2222-4333-8444-555555555555"}',
        '{"narrative":"private model analysis retained in the operator database"}',
        '{"account_equity":1234.56}',
        '{"realized_pnl":-12.34}',
        "当前模拟账户持仓数为 0",
        "两个模拟持仓均CLOSED",
        "本次库内保留 616 条预测事实、37 条交易意图",
    ],
)
def test_document_updates_cannot_publish_raw_business_facts(text):
    with pytest.raises(sync.Blocked):
        sync.privacy_scan({"docs/report.md": text.encode()})


def test_documentation_can_describe_fields_without_publishing_account_values():
    sync.privacy_scan(
        {
            "docs/design.md": b"Fields: account_equity, realized_pnl; values stay private.\n"
        }
    )


def test_private_account_configuration_and_changelog_never_become_managed_sources(
    tmp_path,
):
    g(tmp_path, "init", "-b", "main")
    managed = {
        "config/risk.yaml",
        "config/symbols.yaml",
        "trading-agent/config.yaml",
        "changelog.md",
    }
    for name in managed:
        put(tmp_path, name, "private deployment or business record\n")
    assert sync.discover_source(tmp_path, managed) == {}
