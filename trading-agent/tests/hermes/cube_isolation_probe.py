"""Native provider isolation probes against a real attempt; never calls a model."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from agent.terminal_env_registry import get_provider  # noqa: E402
from hermes_cli.plugins import get_plugin_manager  # noqa: E402

get_plugin_manager().discover_and_load()
provider = get_provider("cubesandbox")
assert provider and provider.is_available()
first = provider.create_environment(cwd="/workspace/scratch", timeout=5, task_id="parent")
second = provider.create_environment(cwd="/workspace/scratch", timeout=5, task_id="child")
try:
    result = first.execute(
        "printf shared > shared.txt; ln -s /workspace/input/context.json escape", timeout=5
    )
    assert result["returncode"] == 0
    first.cleanup()
    result = second.execute(
        "test $(cat shared.txt) = shared && printf native > result.txt", timeout=5
    )
    assert result["returncode"] == 0
    output = Path(sys.argv[2])
    second.fetch_file("/workspace/scratch/result.txt", output, max_bytes=32)
    assert output.read_text() == "native"
    for path in ("/workspace/input/context.json", "/workspace/scratch/escape", "/etc/passwd"):
        try:
            second.fetch_file(path, output, max_bytes=32)
        except PermissionError:
            pass
        else:
            raise AssertionError("file bridge escaped scratch")
    fence = os.environ["TA_FENCE"]
    os.environ["TA_FENCE"] = str(int(fence) + 1)
    try:
        provider.create_environment(cwd="/workspace/scratch", timeout=5)
    except PermissionError:
        pass
    else:
        raise AssertionError("changed fence was accepted")
    os.environ["TA_FENCE"] = fence
    path = os.environ.pop("TA_CUBE_SANDBOX_FILE")
    try:
        assert not provider.is_available()
        try:
            provider.create_environment(cwd="/workspace/scratch", timeout=5)
        except RuntimeError:
            pass
        else:
            raise AssertionError("missing attempt context accepted")
    finally:
        os.environ["TA_CUBE_SANDBOX_FILE"] = path
finally:
    first.cleanup()
    second.cleanup()
print(
    json.dumps(
        dict.fromkeys(
            [
                "shared_child_lifetime",
                "file_bridge_bounds",
                "stale_attempt_denied",
                "no_local_fallback",
            ],
            True,
        )
    )
)
