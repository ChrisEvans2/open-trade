"""Fail closed before this host's paper-only service can initialize a live transport."""

import argparse
from pathlib import Path

import yaml

from trading_agent.config import load_config
from trading_agent.execution.settings import load_execution


def require_paper(path):
    config = load_config(path)
    state_path = Path(config.prediction.legacy_config_dir) / "runtime_state.yaml"
    state = yaml.safe_load(state_path.read_text())
    if state.get("execution_mode") != "paper":
        raise ValueError("Paper service refuses a non-paper execution mode")
    settings, transport = load_execution(config)
    if settings.runtime.execution_mode.value != "paper" or transport.mode != "paper":
        raise ValueError("Paper service requires PaperTransport")
    if config.paper.enabled or config.experiment.enabled:
        raise ValueError("Legacy experiment entry is not allowed in this service")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        require_paper(args.config)
    except Exception:
        parser.exit(1, "Paper-only startup guard refused configuration.\n")
    print("paper_only_guard_passed")
