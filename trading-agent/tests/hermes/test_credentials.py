import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = Path(os.environ.get("TA_HERMES_REFERENCE", ROOT / ".local/hermes-reference"))
pytestmark = pytest.mark.hermes


@pytest.mark.parametrize("configured", [False, True])
def test_native_credentials_preflight_never_outputs_keys_or_calls_a_model(tmp_path, configured):
    if not REFERENCE.exists() or importlib.util.find_spec("dotenv") is None:
        pytest.skip("Requires pinned Hermes reference and hermes-test dependencies")
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: fixture-model\n"
    )
    if configured:
        (tmp_path / ".env").write_text("OPENROUTER_API_KEY=fixture-profile\n")
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "integrations/hermes/verify_runtime.py"),
            str(REFERENCE),
            "openrouter",
            "fixture-model",
        ],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "HERMES_HOME": str(tmp_path),
            "HOME": str(tmp_path),
            "PYTHONNOUSERSITE": "1",
            "OPENROUTER_API_KEY": "fixture-ambient",
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert json.loads(completed.stdout) == {"configured": configured}
    assert completed.returncode == (0 if configured else 1)
    assert not completed.stderr and "fixture-profile" not in completed.stdout
