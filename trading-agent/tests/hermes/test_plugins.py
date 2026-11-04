import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from trading_agent.prediction.contracts import ParentDecision, PredictionReport

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = Path(os.environ.get("TA_HERMES_REFERENCE", ROOT / ".local/hermes-reference"))
pytestmark = pytest.mark.hermes


@pytest.mark.parametrize(
    "mode",
    ["bridge", "structural", "cube_cancel", "dialogue"],
)
def test_real_pinned_native_discovery_and_delegate_wrapper(tmp_path, mode):
    if not REFERENCE.exists() or importlib.util.find_spec("dotenv") is None:
        pytest.skip("Requires pinned reference and uv run --extra cube --group hermes-test")
    home = tmp_path / "profile"
    home.mkdir()
    names = ["trading-bridge"] if mode == "bridge" else ["trading-research", "trading-cube"]
    for name in names:
        source = ROOT / "integrations/hermes"
        shutil.copytree(source / name, home / "plugins" / name)
    configuration = (
        "plugins:\n  enabled: " + str(names) + "\ndelegation:\n  max_iterations: 6\n"
        "  max_concurrent_children: 4\n  oneshot_max_children: 4\n  max_spawn_depth: 1\n"
    )
    if mode == "dialogue":
        configuration += (
            "tools:\n  tool_search:\n    enabled: 'off'\n"
            "platform_toolsets:\n  cli: [trading_research, terminal]\n  telegram: []\n"
            "known_plugin_toolsets:\n  telegram: [trading_research]\n"
        )
    (home / "config.yaml").write_text(configuration, encoding="utf-8")
    inputs = home / "inputs"
    inputs.mkdir()
    for name, model in (("prediction", PredictionReport), ("decision", ParentDecision)):
        (inputs / f"{name}-schema.json").write_text(json.dumps(model.model_json_schema()))
    for tf in ["1h", "4h", "6h", "12h"]:
        prompts = ROOT / "prompts" / "structural"
        shutil.copyfile(prompts / f"{tf}.md", inputs / f"prompt-{tf}.txt")
    # The test never opens the user's Hermes profiles, .env files, model credentials or databases.
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "HERMES_HOME": str(home),
        "HERMES_ENABLE_PROJECT_PLUGINS": "0",
        "PYTHONNOUSERSITE": "1",
    }
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tests/hermes/probe.py"), str(REFERENCE), mode],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
