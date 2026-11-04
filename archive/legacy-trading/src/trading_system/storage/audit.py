from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from trading_core.schemas import CycleArtifacts, ExecutionMode, PaperTradeEvent


class AuditLogger:
    def __init__(self, log_dir: str | Path = "logs"):
        self.log_dir = Path(log_dir)

    def save_cycle(self, artifacts: CycleArtifacts) -> Path:
        ts = artifacts.market.timestamp
        cycle_dir = self.log_dir / ts.strftime("%Y-%m-%d") / f"{ts.strftime('%H-%M')}-{artifacts.market.symbol}"
        cycle_dir.mkdir(parents=True, exist_ok=True)
        self._write(cycle_dir / "market.json", artifacts.market)
        self._write_lines(cycle_dir / "social_raw.jsonl", artifacts.social_raw)
        self._write(cycle_dir / "social_filtered.json", artifacts.social_filtered)
        self._write(cycle_dir / "ai_analysis.json", artifacts.ai_analysis)
        self._write(cycle_dir / "strategy_signal.json", artifacts.strategy_signal)
        if artifacts.market_advisor is not None:
            self._write(cycle_dir / "market_advisor.json", artifacts.market_advisor)
            self._append_market_advisor_prediction(artifacts)
        self._write(cycle_dir / "risk_decision.json", artifacts.risk_decision)
        self._write(cycle_dir / "execution_result.json", artifacts.execution_result)
        (cycle_dir / "report.md").write_text(self._report(artifacts), encoding="utf-8")
        return cycle_dir

    def save_paper_trade_event(self, event: PaperTradeEvent) -> Path:
        return self.save_trade_event(event, ExecutionMode.PAPER)

    def save_trade_event(self, event: PaperTradeEvent, execution_mode: ExecutionMode | str = ExecutionMode.PAPER) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        mode = ExecutionMode(execution_mode)
        path = self.log_dir / f"{mode.value}_trade_events.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        return path

    def _append_market_advisor_prediction(self, artifacts: CycleArtifacts) -> None:
        advisor = artifacts.market_advisor
        if advisor is None:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / "advisor_predictions.jsonl"
        meta = advisor.meta_decision
        payload = {
            "request_id": advisor.request_id,
            "timestamp": artifacts.market.timestamp.isoformat(),
            "symbol": artifacts.market.symbol,
            "timeframe": artifacts.market.timeframe,
            "close": artifacts.market.close,
            "advisor_provider": advisor.advisor_provider,
            "trigger_reasons": advisor.trigger_reasons,
            "strategy_signal_before_advisor": advisor.strategy_signal,
            "meta_decision": meta.model_dump(mode="json") if meta is not None else None,
            "arbiter_decision": advisor.arbiter_decision.model_dump(mode="json"),
            "risk_sizing": advisor.risk_sizing,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    @staticmethod
    def _write(path: Path, value: BaseModel | list[BaseModel]) -> None:
        if isinstance(value, list):
            path.write_text(
                "[\n" + ",\n".join(item.model_dump_json(indent=2) for item in value) + "\n]\n",
                encoding="utf-8",
            )
        else:
            path.write_text(value.model_dump_json(indent=2), encoding="utf-8")

    @staticmethod
    def _write_lines(path: Path, values: list[BaseModel]) -> None:
        path.write_text("".join(item.model_dump_json() + "\n" for item in values), encoding="utf-8")

    @staticmethod
    def _report(artifacts: CycleArtifacts) -> str:
        regime = artifacts.strategy_signal.market_regime
        social_audit = artifacts.strategy_signal.social_audit
        return (
            f"# Cycle Report {artifacts.market.symbol} {artifacts.market.timeframe}\n\n"
            f"- Close: {artifacts.market.close}\n"
            f"- Funding rate: {artifacts.market.funding_rate}\n"
            f"- Next funding rate: {artifacts.market.next_funding_rate}\n"
            f"- Funding time: {artifacts.market.funding_time}\n"
            f"- Market regime: {regime.regime if regime else 'unknown'}"
            f" / {regime.pattern if regime else 'none'}"
            f" / confidence {regime.confidence if regime else 0}\n"
            f"- Social risk: {artifacts.ai_analysis.event_risk}\n"
            f"- Social changed decision: {social_audit.social_changed_decision if social_audit else False}\n"
            f"- Social contribution: {social_audit.contribution_reason if social_audit else 'none'}\n"
            f"- Market advisor: "
            f"{artifacts.market_advisor.arbiter_decision.candidate if artifacts.market_advisor else 'none'}"
            f" / accepted {artifacts.market_advisor.arbiter_decision.accepted if artifacts.market_advisor else False}\n"
            f"- Strategy: {artifacts.strategy_signal.signal}\n"
            f"- Risk decision: {artifacts.risk_decision.final_action}\n"
            f"- Position pct: {artifacts.risk_decision.final_position_pct}\n"
            f"- Execution: {artifacts.execution_result.success}\n\n"
            f"Reason: {artifacts.risk_decision.reason}\n"
        )
