import importlib.util
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
source = Path(__file__).resolve().parents[2] / "integrations/hermes/trading-cube/__init__.py"
spec = importlib.util.spec_from_file_location("cube_fixture_adapter", source)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
root = Path(sys.argv[2])
destroyed = []


class Commands:
    def run(self, command, **options):
        result = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            cwd=options.get("cwd", root),
            timeout=options.get("timeout", 10),
            capture_output=True,
            text=True,
        )
        return SimpleNamespace(
            stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode
        )


def environment(name):
    # Bypass unrelated BaseEnvironment shell discovery and remote creation;
    # exercise the actual command wrapper and Hermes process handle.
    obj = object.__new__(adapter.CubeEnvironment)
    obj.workdir = str(root / name)
    Path(obj.workdir).mkdir()
    obj.sandbox = SimpleNamespace(commands=Commands())
    obj.user = "fixture"
    obj.max_processes = 128
    obj.max_output = 4096
    obj.cancel = lambda: destroyed.append(True)
    return obj


first = environment("one")
second = environment("two")
a = first._run_bash("sleep 8; echo should-not-complete", timeout=10)
b = second._run_bash("sleep .4; echo sibling-completed", timeout=3)
# _ThreadedProcessHandle starts execution on construction.
time.sleep(0.1)
a.kill()
# Handle wait API is pinned Hermes, exercised here without a remote sandbox.
ra = a.wait(timeout=5)
rb = b.wait(timeout=5)
outa = a.stdout.read()
outb = b.stdout.read()
assert "should-not-complete" not in outa, ra
assert "sibling-completed" in outb and rb == 0, rb
assert not destroyed
print("command cancellation stopped its child group; sibling completed; no shared sandbox deletion")
