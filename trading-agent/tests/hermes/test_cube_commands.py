"""Run only literal fixture shell commands; no remote guest or user environment."""

import os
import subprocess
from pathlib import Path

import pytest
from test_plugins import REFERENCE

pytestmark = pytest.mark.hermes


def test_command_cancel_keeps_sibling_and_uses_distinct_session_directory(tmp_path):
    # Load the adapter in the same isolated subprocess used by the native tests.
    import sys

    script = Path(__file__).with_name("cube_command_probe.py")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONNOUSERSITE": "1"}
    completed = subprocess.run(
        [sys.executable, str(script), str(REFERENCE), str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
