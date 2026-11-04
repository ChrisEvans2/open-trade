from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


HORIZON_HOURS = {
    "15m": 1,
    "1h": 4,
    "4h": 12,
    "12h": 24,
}


def update_market_advisor_feedback(log_dir: str | Path = "/srv/open-trade-deployment/logs", state_dir: str | Path = "/srv/open-trade-deployment/state") -> dict[str, Any]:
    log_path = Path(log_dir)
    predictions_path = log_path / "advisor_predictions.jsonl"
    feedback_path = log_path / "advisor_feedback.jsonl"
    if not predictions_path.exists():
        return {"evaluated": 0, "reason": "missing_predictions"}

    existing_ids = _existing_feedback_ids(feedback_path)
    markets = _load_market_closes(log_path)
    evaluated: list[dict[str, Any]] = []
    for line in predictions_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        prediction = json.loads(line)
        request_id = str(prediction.get("request_id") or "")
        if not request_id or request_id in existing_ids:
            continue
        item = _evaluate_prediction(prediction, markets)
        if item is not None:
            evaluated.append(item)

    if evaluated:
        with feedback_path.open("a", encoding="utf-8") as handle:
            for item in evaluated:
                handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    summary = _write_accuracy_state(feedback_path, Path(state_dir) / "market_advisor_accuracy.json")
    summary["evaluated"] = len(evaluated)
    return summary


def _existing_feedback_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                ids.add(str(json.loads(line).get("request_id") or ""))
            except json.JSONDecodeError:
                continue
    return ids


def _load_market_closes(log_dir: Path) -> dict[tuple[str, datetime], float]:
    closes: dict[tuple[str, datetime], float] = {}
    for path in log_dir.glob("**/market.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            symbol = str(data["symbol"])
            timestamp = datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00")).astimezone(timezone.utc)
            closes[(symbol, timestamp)] = float(data["close"])
        except Exception:
            continue
    return closes


def _evaluate_prediction(prediction: dict[str, Any], markets: dict[tuple[str, datetime], float]) -> dict[str, Any] | None:
    meta = prediction.get("meta_decision") or {}
    direction = str(meta.get("direction") or "none")
    if direction not in {"long", "short"}:
        return None
    symbol = str(prediction.get("symbol") or "")
    start = datetime.fromisoformat(str(prediction["timestamp"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    start_close = float(prediction.get("close") or 0)
    if not symbol or start_close <= 0:
        return None

    horizons = {}
    for label, hours in HORIZON_HOURS.items():
        cutoff = start.timestamp() + hours * 3600
        future = sorted(
            (ts, close)
            for (item_symbol, ts), close in markets.items()
            if item_symbol == symbol and start.timestamp() < ts.timestamp() <= cutoff
        )
        if not future:
            continue
        closes = [close for _, close in future]
        end_close = closes[-1]
        signed_return = (end_close - start_close) / start_close
        if direction == "short":
            signed_return = -signed_return
        mfe = (max(closes) - start_close) / start_close
        mae = (min(closes) - start_close) / start_close
        if direction == "short":
            mfe, mae = -mae, -mfe
        horizons[label] = {
            "samples": len(closes),
            "end_close": end_close,
            "signed_return": signed_return,
            "correct": signed_return > 0,
            "mfe": mfe,
            "mae": mae,
        }
    if not horizons:
        return None
    return {
        "request_id": prediction["request_id"],
        "timestamp": prediction["timestamp"],
        "symbol": symbol,
        "direction": direction,
        "confidence": meta.get("confidence", 0),
        "decision": meta.get("decision", "neutral"),
        "horizons": horizons,
    }


def _write_accuracy_state(feedback_path: Path, state_path: Path) -> dict[str, Any]:
    stats: dict[str, Counter] = defaultdict(Counter)
    if feedback_path.exists():
        for line in feedback_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            for horizon, result in (item.get("horizons") or {}).items():
                key = f"{item.get('symbol')}:{horizon}"
                stats[key]["total"] += 1
                if result.get("correct"):
                    stats[key]["correct"] += 1
    summary = {
        key: {
            "total": value["total"],
            "correct": value["correct"],
            "accuracy": round(value["correct"] / value["total"], 4) if value["total"] else 0,
        }
        for key, value in sorted(stats.items())
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"accuracy": summary}
