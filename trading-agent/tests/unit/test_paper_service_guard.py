import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_agent.config import AppConfig, PredictionConfig

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "mode,transport_mode", [("live", "paper"), ("paper", "live"), ("paper", "paper")]
)
def test_paper_service_refuses_live_before_transport_initialization(tmp_path, mode, transport_mode):
    namespace = runpy.run_path(str(ROOT / "deploy/require-paper.py"))
    require = namespace["require_paper"]
    (tmp_path / "runtime_state.yaml").write_text("execution_mode: " + mode + "\n")
    config = AppConfig(prediction=PredictionConfig(legacy_config_dir=tmp_path))
    calls = []

    def load(settings):
        calls.append(True)
        assert mode == "paper", "Live state must be rejected before transport initialization"
        return (
            SimpleNamespace(runtime=SimpleNamespace(execution_mode=SimpleNamespace(value=mode))),
            SimpleNamespace(mode=transport_mode),
        )

    require.__globals__["load_config"] = lambda path: config
    require.__globals__["load_execution"] = load
    if mode == "paper" and transport_mode == "paper":
        require(tmp_path / "config.yaml")
    else:
        with pytest.raises(ValueError):
            require(tmp_path / "config.yaml")
    assert bool(calls) is (mode == "paper")
