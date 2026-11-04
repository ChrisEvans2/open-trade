"""Controlled inputs exercise an isolated paper policy; never real-model evidence."""

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from test_prediction_execution import fresh_market, opening, records
from test_prediction_pipeline import prediction_system as prediction_system
from test_prediction_pipeline import report, task

from trading_agent.execution.service import ExecutionService
from trading_agent.execution.settings import load_execution
from trading_agent.execution.transport import PaperTransport
from trading_agent.prediction.contracts import ParentDecision

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT"])
async def test_controlled_paper_roundtrip_with_example_policy(prediction_system, symbol, tmp_path):
    configured = os.environ.get("TA_ACCEPTANCE_CONFIG")
    from trading_agent.config import AppConfig, PredictionConfig, load_config

    if configured:
        deployed = load_config(Path(configured))
    else:
        import yaml

        policy = tmp_path / "policy"
        policy.mkdir()
        for name, body in {
            "app.yaml": {"runtime": {"social_ai_enabled": False}},
            "risk.yaml": {"defaults": {"min_position_margin": 1}},
            "strategy.yaml": {"freqtrade_style": {"enabled": True}},
            "runtime_state.yaml": {
                "mode": "normal",
                "execution_mode": "paper",
                "global_paused": False,
            },
        }.items():
            (policy / name).write_text(yaml.safe_dump(body))
        deployed = AppConfig(
            prediction=PredictionConfig(execution_enabled=True, legacy_config_dir=policy)
        )
    system = prediction_system
    settings, transport = load_execution(deployed)
    assert type(transport) is PaperTransport and transport.mode == "paper"
    assert settings.runtime.execution_mode.value == "paper"
    service = ExecutionService(system[2], deployed, system[3], system[4], settings, transport)
    forecast = await opening(system, symbol)
    attempt = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(attempt)
    await service.run(attempt)  # Retried accounting must be idempotent.
    await system[2].finish(attempt)
    opened = await records(system)
    assert opened["trade_intent_states"][0]["status"] == "FILLED", opened["trade_intent_states"]
    position = opened["trading_positions"][0]
    assert position["status"] == "OPEN"
    parent = await task(system, symbol=symbol, kind="PREDICT_PARENT")
    ctx = await system[3].context(parent)
    decision = ParentDecision(
        prediction=(await report(system, parent)).model_copy(update={"primary_direction": None}),
        action="CLOSE",
        trade_style="TREND",
        selected_direction="DOWN",
        child_prediction_ids=[c["id"] for c in ctx["children"]],
        context_revision=ctx["context_revision"],
        position_id=position["position_id"],
        relationship="受控验收输入：父判断转向，只申请绑定持仓平仓，不反手",
    )
    await system[3].publish(parent, decision)
    await system[2].finish(parent)
    await fresh_market(system, symbol)
    closing = await system[2].claim(("PREDICT_EXECUTE",))
    await service.run(closing)
    await service.run(closing)
    await system[2].finish(closing)
    closed = await records(system)
    assert closed["trading_positions"][0]["status"] == "CLOSED"
    assert closed["trading_accounts"][0]["body"]["account"]["open_positions"] == {}
    assert [i["action"] for i in closed["trade_intents"]] == ["OPEN_LONG", "CLOSE"]
    fills = [e for e in closed["execution_events"] if e["body"]["kind"] == "FILL_ACCOUNTING"]
    assert len(fills) == 2
    output = os.environ.get("TA_ACCEPTANCE_EVIDENCE_DIR")
    if output:
        folder = Path(output)
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True, mode=0o700)
        policy = Path(deployed.prediction.legacy_config_dir)
        data = {
            "source": "CONTROLLED_FIXTURE_NOT_REAL_MODEL",
            "symbol": symbol,
            "forecast_id": str(forecast),
            "transport": "PaperTransport",
            "policy_hashes": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (policy / n for n in ["risk.yaml", "strategy.yaml", "runtime_state.yaml"])
            },
            "after_open": {k: [dict(r) for r in v] for k, v in opened.items()},
            "after_close": {k: [dict(r) for r in v] for k, v in closed.items()},
        }
        path = folder / (symbol + ".json")
        path.write_text(json.dumps(data, default=str, ensure_ascii=False, indent=2) + "\n")
        path.chmod(0o600)
