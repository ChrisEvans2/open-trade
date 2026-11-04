import importlib.metadata
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from trading_agent.cli import parser
from trading_agent.config import PredictionConfig
from trading_agent.domain.errors import DomainError
from trading_agent.execution.settings import require_trade_parameters
from trading_agent.runtime.prompts import load_prompts


def test_only_structural_mode_and_current_cli_are_available():
    with pytest.raises(ValidationError):
        PredictionConfig(mode="legacy")
    with pytest.raises(ValueError, match="structural"):
        load_prompts(mode="legacy")
    for words in (["legacy", "review"], ["review", "request"], ["fills", "import"]):
        with pytest.raises(SystemExit):
            parser().parse_args(words)
    assert parser().parse_args(["serve"]).command == "serve"


def test_disabled_legacy_parameter_config_keeps_existing_rejection():
    with pytest.raises(DomainError) as caught:
        require_trade_parameters({"freqtrade_style": {"enabled": False}})
    assert caught.value.code == "RISK_ADAPTER_UNSUPPORTED"
    assert caught.value.status == 503
    parameters = require_trade_parameters(
        {"freqtrade_style": {"atr_min_pct": "0.002", "range_position_pct": "0.03"}}
    )
    assert parameters.atr_min_pct == 0.002
    assert parameters.range_position_pct == 0.03


def test_installed_runtime_imports_without_legacy_code_or_cli(tmp_path):
    # A fresh process outside the repository has no test-only archive paths.
    code = """
import importlib.metadata, importlib.util, json, sys
from trading_agent.runtime.factory import build_runtime
from trading_agent.execution.settings import load_execution
from trading_core.prediction_adapter import PredictionRiskAdapter
print(json.dumps({
    'old_framework': importlib.util.find_spec('trading_system') is not None,
    'old_research': importlib.util.find_spec('legacy_research') is not None,
    'loaded_old_modules': [
        n for n in sys.modules if n.startswith(('trading_system', 'legacy_research'))
    ],
    'old_cli': [
        e.name for e in importlib.metadata.entry_points(group='console_scripts')
        if e.name == 'trade-agent'
    ],
}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "old_framework": False,
        "old_research": False,
        "loaded_old_modules": [],
        "old_cli": [],
    }
    requirements = importlib.metadata.requires("trading-agent")
    assert any(req.startswith("trading-core") for req in requirements)
    assert not any(req.startswith("ai-trading-system") for req in requirements)
