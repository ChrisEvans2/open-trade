import os
import shutil
import subprocess
import sys

import pytest
from test_plugins import REFERENCE, ROOT

pytestmark = pytest.mark.hermes


def test_pinned_plugin_discovery_under_background_imports(tmp_path):
    if not REFERENCE.exists():
        pytest.skip("Requires pinned Hermes reference")
    home = tmp_path / "profile"
    home.mkdir()
    for name in ("trading-research", "trading-cube"):
        shutil.copytree(ROOT / "integrations/hermes" / name, home / "plugins" / name)
    (home / "config.yaml").write_text("plugins:\n  enabled: [trading-research, trading-cube]\n")
    code = """
import sys,threading,types,time
sys.path[:0]=sys.argv[1:3]
from run_hermes import install_loader_fix
install_loader_fix()
from hermes_cli.plugins import get_plugin_manager
manager=get_plugin_manager()
stop=threading.Event()
def imports():
    while not stop.is_set():
        for i in range(30):
            name='_acceptance_import_'+str(i)
            sys.modules[name]=types.ModuleType(name)
            time.sleep(.0001)
            sys.modules.pop(name,None)
thread=threading.Thread(target=imports)
thread.start()
try:
    for _ in range(12):
        manager.discover_and_load(force=True)
        rows={r['name']:r for r in manager.list_plugins()}
        for name in ('trading-research','trading-cube'):
            assert rows[name]['enabled'] and not rows[name]['error'],rows[name]
finally:
    stop.set();thread.join()
print('12 native discovery rounds passed during background imports')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(REFERENCE), str(ROOT / "integrations/hermes")],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "HERMES_HOME": str(home),
            "HERMES_ENABLE_PROJECT_PLUGINS": "0",
            "PYTHONNOUSERSITE": "1",
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert result.returncode == 0, result.stdout + result.stderr
