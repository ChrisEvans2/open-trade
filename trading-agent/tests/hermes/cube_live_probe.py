"""Real Hermes terminal provider against an existing acceptance sandbox; no model calls."""

import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])

from agent.terminal_env_registry import get_provider  # noqa: E402
from hermes_cli.plugins import get_plugin_manager  # noqa: E402

get_plugin_manager().discover_and_load()
provider = get_provider("cubesandbox")
assert provider and provider.is_available()
environment = provider.create_environment(cwd="/workspace/scratch", timeout=10)
try:
    response = environment.execute(
        "test $(id -u) -ne 0 && test ! -w /workspace/input/context.json "
        "&& python3 -c 'print(6 * 7)' && printf 'cube-live-bridge' > result.txt",
        timeout=10,
    )
    assert response["returncode"] == 0, "sandbox terminal failed"
    assert "42" in response["output"], "sandbox output missing"
    output = Path(sys.argv[2])
    environment.fetch_file("/workspace/scratch/result.txt", output, max_bytes=64)
    assert output.read_text() == "cube-live-bridge"
    try:
        environment.fetch_file("/workspace/input/context.json", output, max_bytes=64)
    except PermissionError:
        pass
    else:
        raise AssertionError("file bridge escaped scratch")
finally:
    environment.cleanup()
print(json.dumps({"native_provider": True, "ordinary_user": True, "scratch_file_bridge": True}))
