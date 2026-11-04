"""Opt-in component acceptance. Never marks the full deployment/isolation gate verified."""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import yaml

from trading_agent.config import AppConfig, SandboxConfig
from trading_agent.runtime.sandboxes import CubeResources, ownership_metadata, sandbox_policy

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]


def test_real_cube_native_terminal_and_timeout_update(tmp_path):
    template = os.environ.get("TA_CUBE_LIVE_TEMPLATE")
    reference = Path(os.environ.get("TA_HERMES_REFERENCE", ROOT / ".local/hermes-reference"))
    if not template or not reference.is_dir() or not os.environ.get("CUBE_API_KEY"):
        pytest.skip(
            "Set TA_CUBE_LIVE_TEMPLATE, TA_HERMES_REFERENCE and CUBE_API_KEY for acceptance"
        )
    settings = SandboxConfig(
        base_url="http://127.0.0.1:3000",
        template=template,
        sdk_version="0.7.0",
        proxy_node_ip="127.0.0.1",
        proxy_port=80,
        execution_user=os.environ.get("TA_CUBE_LIVE_USER", "user"),
    )
    # Keep the probe independent of production credentials and deployment gates.
    # The complete isolation report is produced by the separate full acceptance suite.
    key = os.environ.get("CUBE_API_KEY")
    sdk = {
        "api_url": settings.base_url,
        "api_key": key,
        "template_id": template,
        "proxy_node_ip": settings.proxy_node_ip,
        "proxy_port": settings.proxy_port,
        "sandbox_domain": settings.sandbox_domain,
        "request_timeout": settings.management_timeout_seconds,
    }
    metadata = {
        "project": "cube-component-acceptance",
        "attempt_id": str(uuid4()),
        "job_id": str(uuid4()),
        "fence": "1",
    }
    headers = {"X-API-Key": key} if key else {}
    with httpx.Client(
        base_url=settings.base_url, headers=headers, trust_env=False, timeout=20
    ) as client:
        response = client.post(
            "/sandboxes",
            json={
                "templateID": template,
                "timeout": 15,
                "metadata": metadata,
                "envVars": {},
                "volumeMounts": [],
                "lifecycle": {"onTimeout": "kill", "autoResume": False},
                **sandbox_policy(settings, "PREDICT_TIMEFRAME"),
            },
        )
        response.raise_for_status()
        raw = response.json()
        raw["metadata"] = metadata
        ident = raw["sandboxID"]
        created = time.monotonic()
        bootstrap = tmp_path / ".cube.secrets.json"
        try:
            initial = client.get("/sandboxes/" + ident)
            initial.raise_for_status()
            # Renew before setup, then again after setup while retaining the same sandbox ID.
            updated = client.post("/sandboxes/" + ident + "/timeout", json={"timeout": 45})
            assert updated.status_code == 204
            inputs = tmp_path / "input"
            inputs.mkdir()
            (inputs / "context.json").write_text('{"acceptance_probe":true}')
            (inputs / "task.txt").write_text("Component acceptance; no prediction or order.")
            # Exercise the real task input/permission setup without claiming production auth.
            CubeResources._populate(
                SimpleNamespace(config=AppConfig(sandbox=settings), sdk_settings=sdk), raw, tmp_path
            )
            updated = client.post("/sandboxes/" + ident + "/timeout", json={"timeout": 60})
            assert updated.status_code == 204
            current = client.get("/sandboxes/" + ident)
            current.raise_for_status()
            assert current.json()["sandboxID"] == ident

            def end(response):
                return datetime.fromisoformat(response.json()["endAt"].replace("Z", "+00:00"))

            assert (end(current) - end(initial)).total_seconds() >= 40
            # Wait beyond the original 15-second window; no guest traffic refreshes it here.
            time.sleep(max(0, 17 - (time.monotonic() - created)))
            assert client.get("/sandboxes/" + ident).status_code == 200
            home = tmp_path / "hermes-home"
            home.mkdir()
            shutil.copytree(
                ROOT / "integrations/hermes/trading-cube", home / "plugins/trading-cube"
            )
            (home / "config.yaml").write_text(
                yaml.safe_dump({"plugins": {"enabled": ["trading-cube"]}})
            )
            fd = os.open(bootstrap, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(
                    {
                        "config": sdk,
                        "sandbox": raw,
                        "settings": {
                            "execution_user": settings.execution_user,
                            "max_output_bytes": settings.max_output_bytes,
                            "max_processes": settings.max_processes,
                        },
                    },
                    handle,
                )
            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(home),
                "HERMES_HOME": str(home),
                "HERMES_ENABLE_PROJECT_PLUGINS": "0",
                "PYTHONNOUSERSITE": "1",
                "TA_CUBE_SANDBOX_FILE": str(bootstrap),
                "TA_ATTEMPT_ID": metadata["attempt_id"],
                "TA_JOB_ID": metadata["job_id"],
                "TA_FENCE": "1",
            }
            probe = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tests/hermes/cube_live_probe.py"),
                    str(reference),
                    str(tmp_path / "fetched.txt"),
                ],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert probe.returncode == 0, probe.stdout + probe.stderr
        finally:
            bootstrap.unlink(missing_ok=True)
            # Only reclaim this probe's sandbox; preserve all other sandboxes and templates.
            check = client.get("/sandboxes/" + ident)
            if check.status_code != 404:
                check.raise_for_status()
                assert ownership_metadata(check.json()) == metadata
                assert client.delete("/sandboxes/" + ident).status_code == 204
            assert client.get("/sandboxes/" + ident).status_code == 404
