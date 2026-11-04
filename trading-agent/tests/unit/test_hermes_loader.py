import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ADAPTER = Path(__file__).resolve().parents[2] / "integrations/hermes/run_hermes.py"


class ImportingModules(dict):
    def __iter__(self):
        for key in super().__iter__():
            self["background.import"] = object()
            yield key


def test_module_eviction_survives_concurrent_import_and_keeps_sibling_namespace():
    modules = ImportingModules(
        {"hermes_plugins.a": 1, "hermes_plugins.a.child": 2, "hermes_plugins.ab": 3, "other": 4}
    )
    with pytest.raises(RuntimeError, match="dictionary changed size"):
        list(name for name in modules if name.startswith("hermes_plugins.a"))
    runpy.run_path(str(ADAPTER))["purge_modules"]("hermes_plugins.a", modules)
    assert modules == {
        "hermes_plugins.ab": 3,
        "other": 4,
        "background.import": modules["background.import"],
    }


def test_both_native_eviction_bindings_use_the_fix(monkeypatch):
    loader = SimpleNamespace(_evict_modules=lambda name: None)
    plugins = SimpleNamespace(_evict_modules=lambda name: None)
    monkeypatch.setitem(
        sys.modules, "hermes_cli", SimpleNamespace(plugins=plugins, plugins_loader=loader)
    )
    runpy.run_path(str(ADAPTER))["install_loader_fix"]()
    assert loader._evict_modules is plugins._evict_modules
    monkeypatch.setitem(sys.modules, "hermes_plugins.fixture", object())
    monkeypatch.setitem(sys.modules, "hermes_plugins.fixture.child", object())
    loader._evict_modules("hermes_plugins.fixture")
    assert "hermes_plugins.fixture" not in sys.modules
    assert "hermes_plugins.fixture.child" not in sys.modules
