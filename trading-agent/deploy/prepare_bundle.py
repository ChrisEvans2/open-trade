"""Prepare a reviewable deployment directory without installing into an existing Hermes home."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import yaml

from trading_agent.config import load_config
from trading_agent.runtime.deployment import (
    STABLE_MEMORY,
    bridge_settings,
    expected_lock,
    integration_root,
    trading_profile_config,
)


def prepare(config, output):
    if output.exists():
        raise ValueError("输出目录已存在，拒绝覆盖")
    output.mkdir(parents=True, mode=0o700)
    for name in ("trading-bridge", "trading-research", "trading-cube"):
        shutil.copytree(
            integration_root() / name,
            output / "plugins" / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    shutil.copyfile(integration_root() / "verify_runtime.py", output / "verify_runtime.py")
    shutil.copyfile(integration_root() / "run_hermes.py", output / "run_hermes.py")
    (output / "controller-settings.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": ["trading-bridge"],
                    "entries": {"trading-bridge": {"settings": bridge_settings(config)}},
                }
            }
        ),
        encoding="utf-8",
    )
    profile = output / "trading-profile"
    profile.mkdir(mode=0o700)
    (profile / "config.yaml").write_text(
        yaml.safe_dump(trading_profile_config(config), allow_unicode=True), encoding="utf-8"
    )
    memories = profile / "memories"
    memories.mkdir(mode=0o700)
    for name, value in STABLE_MEMORY.items():
        (memories / name).write_text(value, encoding="utf-8")
    for path in (profile / "config.yaml", *memories.iterdir()):
        path.chmod(0o400)
    manifest = expected_lock(config)
    if config.sandbox.isolation_report:
        manifest["isolation_report_hash"] = hashlib.sha256(
            config.sandbox.isolation_report.read_bytes()
        ).hexdigest()
    (output / "deployment.lock.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"bundle": str(output.resolve()), "installed": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(load_config(args.config), args.output), ensure_ascii=False))
    except Exception:
        parser.exit(1, "部署包生成失败；未安装到Hermes Profile。\n")
