import importlib.util
import os
import shutil
import subprocess
import sys

import pytest
from test_plugins import REFERENCE, ROOT

pytestmark = pytest.mark.hermes


@pytest.mark.parametrize("mode", ["retry", "timeout", "expanded", "pending", "large"])
def test_pinned_public_lifecycle_four_parallel_and_partial_retry(tmp_path, mode):
    if not REFERENCE.exists() or importlib.util.find_spec("dotenv") is None:
        pytest.skip("Requires pinned Hermes and hermes-test dependencies")
    home = tmp_path / "profile"
    home.mkdir()
    for name in ["trading-research", "trading-cube"]:
        shutil.copytree(ROOT / "integrations/hermes" / name, home / "plugins" / name)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [trading-research, trading-cube]\n"
        "delegation:\n  max_concurrent_children: 4\n  max_spawn_depth: 1\n"
    )
    inputs = home / "inputs"
    inputs.mkdir()
    for role in ("1h", "4h", "6h", "12h"):
        shutil.copyfile(ROOT / "prompts/structural" / f"{role}.md", inputs / f"prompt-{role}.txt")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "HERMES_HOME": str(home),
        "HERMES_ENABLE_PROJECT_PLUGINS": "0",
        "PYTHONNOUSERSITE": "1",
    }
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tests/hermes/round_probe.py"), str(REFERENCE), mode],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
