import hashlib
import json
import shutil

import pytest
import yaml

from trading_agent.config import (
    AppConfig,
    HermesConfig,
    MarketConfig,
    NotificationConfig,
    SandboxConfig,
)
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash
from trading_agent.runtime.deployment import (
    REQUIRED_ISOLATION_CHECKS,
    STABLE_MEMORY,
    DeploymentCheck,
    bridge_settings,
    expected_lock,
    integration_root,
    profile_home,
    trading_profile_config,
)


def configured_files(tmp_path):
    config = AppConfig(
        market=MarketConfig(
            inst_id="fixture-contract", contract_type="perpetual", settlement_asset="USDT"
        ),
        hermes=HermesConfig(
            home=tmp_path,
            provider="fixture",
            model="fixture",
            controller_profile="fixture-controller",
            deployment_lock=tmp_path / "lock.json",
        ),
        sandbox=SandboxConfig(
            template="fixture",
            sdk_version="0.7.0",
            isolation_verified=True,
            isolation_report=tmp_path / "fixture-structured-report.json",
        ),
        notification=NotificationConfig(target="fixture-target"),
    )
    home = profile_home(config)
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(yaml.safe_dump(trading_profile_config(config)))
    (home / "memories").mkdir()
    for name, value in STABLE_MEMORY.items():
        (home / "memories" / name).write_text(value)
    for name in ("trading-research", "trading-cube"):
        shutil.copytree(integration_root() / name, home / "plugins" / name)
    shutil.copytree(
        integration_root() / "trading-bridge",
        tmp_path / "profiles/fixture-controller/plugins/trading-bridge",
    )
    (tmp_path / "profiles/fixture-controller/config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": ["trading-bridge"],
                    "entries": {"trading-bridge": {"settings": bridge_settings(config)}},
                },
            }
        )
    )
    lock = expected_lock(config)
    # Tests exercise metadata validation only; this structure is never deployment evidence.
    report = {
        "kind": "live",
        "template": "fixture",
        "sdk_version": "0.7.0",
        "isolation_config_hash": content_hash(lock["cube"]),
        "checks": dict.fromkeys(REQUIRED_ISOLATION_CHECKS, True),
    }
    return config, lock, report


def save(config, lock, report):
    config.sandbox.isolation_report.write_text(json.dumps(report))
    lock["isolation_report_hash"] = hashlib.sha256(
        config.sandbox.isolation_report.read_bytes()
    ).hexdigest()
    config.hermes.deployment_lock.write_text(json.dumps(lock))


@pytest.mark.parametrize("change", ["fixture", "config_hash", "incomplete", "file_hash"])
def test_deployment_rejects_unverified_or_stale_isolation_artifacts(tmp_path, change):
    config, lock, report = configured_files(tmp_path)
    save(config, lock, report)
    check = DeploymentCheck(config, None)
    assert check.files()
    if change == "fixture":
        report["kind"] = "fixture"
    elif change == "config_hash":
        report["isolation_config_hash"] = "wrong-config"
    elif change == "incomplete":
        report["checks"]["no_local_fallback"] = False
    save(config, lock, report)
    if change == "file_hash":
        config.sandbox.isolation_report.write_text("{}")
    with pytest.raises(DomainError) as caught:
        check.files()
    assert caught.value.code == "CUBE_ISOLATION_NOT_VERIFIED"


def test_deployment_checks_native_memory_location_and_model_lock(tmp_path):
    config, lock, report = configured_files(tmp_path)
    save(config, lock, report)
    changed = config.model_copy(
        update={"hermes": config.hermes.model_copy(update={"model": "another"})}
    )
    with pytest.raises(DomainError, match="部署清单"):
        DeploymentCheck(changed, None).files()
    (profile_home(config) / "memories/USER.md").write_text("changed preferences")
    with pytest.raises(DomainError) as caught:
        DeploymentCheck(config, None).files()
    assert caught.value.code == "PROFILE_LOCK_MISMATCH"


@pytest.mark.parametrize(
    "settings,section,key,changed_value",
    [
        (
            {"model_base_url": "https://model.example/v1"},
            "model",
            "base_url",
            "https://other.example/v1",
        ),
        ({"reasoning_effort": "high"}, "agent", "reasoning_effort", "low"),
    ],
)
def test_manual_model_settings_change_identity_and_cannot_drift_from_lock(
    tmp_path, settings, section, key, changed_value
):
    original, original_lock, report = configured_files(tmp_path)
    config = original.model_copy(update={"hermes": original.hermes.model_copy(update=settings)})
    profile = trading_profile_config(config)
    assert profile[section][key] == next(iter(settings.values()))
    assert profile["delegation"][key] == profile[section][key]
    assert config.engine_config_hash != original.engine_config_hash
    lock = expected_lock(config)
    assert lock["engine_hash"] != original_lock["engine_hash"]
    path = profile_home(config) / "config.yaml"
    path.write_text(yaml.safe_dump(profile))
    save(config, lock, report)
    assert DeploymentCheck(config, None).files()

    profile[section][key] = changed_value
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(DomainError) as caught:
        DeploymentCheck(config, None).files()
    assert caught.value.code == "PROFILE_LOCK_MISMATCH"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret@model.example/v1",
        "https://model.example/v1?api_key=secret",
        "https://model.example/v1#secret",
        "file:///local/file",
    ],
)
def test_model_endpoint_cannot_embed_credentials_in_deployment_artifacts(endpoint):
    with pytest.raises(ValueError):
        HermesConfig(model_base_url=endpoint)


def test_prediction_telegram_is_locked_without_changing_prediction_identity(tmp_path):
    original, original_lock, report = configured_files(tmp_path)
    config = original.model_copy(
        update={"hermes": original.hermes.model_copy(update={"trading_telegram_enabled": True})}
    )
    profile = trading_profile_config(config)
    assert profile["platform_toolsets"]["cli"] == ["trading_research", "terminal"]
    assert profile["platform_toolsets"]["telegram"] == []
    assert config.engine_config_hash == original.engine_config_hash
    lock = expected_lock(config)
    assert lock["engine_hash"] == original_lock["engine_hash"]
    assert lock["profile_hash"] != original_lock["profile_hash"]
    path = profile_home(config) / "config.yaml"
    path.write_text(yaml.safe_dump(profile))
    save(config, lock, report)
    assert DeploymentCheck(config, None).files()
    # Explicitly exposing research in chat must invalidate deployment, not grant an attempt.
    profile["platform_toolsets"]["telegram"] = ["trading_research"]
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(DomainError) as caught:
        DeploymentCheck(config, None).files()
    assert caught.value.code == "PROFILE_LOCK_MISMATCH"
