"""只读回放已存档的 AI 否决事件；不导入调度器、不联网、不下单。"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path
from statistics import mean, median

HOUR = timedelta(hours=1)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed


def replay_window(bars, completed_at, side, hours, stop_pct=None, take_pct=None):
    # Always start after the decision, never at the candle supplied to the model.
    start = completed_at.replace(minute=0, second=0, microsecond=0) + HOUR
    times = [start + i * HOUR for i in range(hours)]
    missing = [t.isoformat() for t in times if t not in bars]
    result = {"entry_time": start.isoformat(), "hours": hours}
    if missing:
        return {**result, "status": "missing_candles", "missing": missing}
    rows = [bars[t] for t in times]
    entry = rows[0]["open"]
    sign = 1 if side == "long" else -1
    favorable = [sign * (r["high" if sign == 1 else "low"] / entry - 1) for r in rows]
    adverse = [sign * (r["low" if sign == 1 else "high"] / entry - 1) for r in rows]
    touch = "neither"
    for fav, adv in zip(favorable, adverse):
        stop = stop_pct is not None and adv <= -stop_pct
        take = take_pct is not None and fav >= take_pct
        if stop or take:
            touch = "ambiguous_same_bar" if stop and take else ("stop_first" if stop else "take_first")
            break
    return {
        **result, "status": "ok", "entry_price": entry,
        "directional_return_pct": sign * (rows[-1]["close"] / entry - 1),
        "mfe_pct": max(0, *favorable), "mae_pct": max(0, *[-v for v in adverse]),
        "fixed_level_first_touch": touch,
    }


def analyze(log_dir: Path, start: datetime, end: datetime):
    bars = defaultdict(dict)
    cycles = []
    for p in sorted(log_dir.glob("2026-*/*/market.json")):
        market = json.loads(p.read_text())
        t = timestamp(market["timestamp"])
        if market.get("timeframe") != "1h" or not start - HOUR <= t < end:
            continue
        if t + HOUR <= end:
            bars[market["symbol"]][t] = market
        advisor = p.parent / "market_advisor.json"
        execution = p.parent / "execution_result.json"
        if not advisor.exists() or not execution.exists():
            continue
        a = json.loads(advisor.read_text())
        completed = timestamp(json.loads(execution.read_text())["timestamp"])
        if not start <= completed < end or a.get("strategy_signal") not in {"long", "short"}:
            continue
        if "advisor_blocked_technical_signal" not in a["arbiter_decision"]["reject_reasons"]:
            continue
        signal = json.loads((p.parent / "strategy_signal.json").read_text())
        cycles.append((completed, market["symbol"], a, signal, str(p.parent)))
    samples = []
    next_nonoverlap = {}
    for completed, symbol, a, signal, source in sorted(cycles, key=lambda row: (row[0], row[1])):
        key = (symbol, a["strategy_signal"])
        entry_time = completed.replace(minute=0, second=0, microsecond=0) + HOUR
        independent_window = key not in next_nonoverlap or entry_time >= next_nonoverlap[key]
        if independent_window:
            next_nonoverlap[key] = entry_time + timedelta(hours=24)
        samples.append({
            "source": source, "symbol": symbol, "side": a["strategy_signal"],
            "completed_at": completed.isoformat(), "nonoverlap_24h": independent_window,
            "meta_reason": (a.get("meta_decision") or {}).get("main_reason"),
            "windows": {str(h): replay_window(
                bars[symbol], completed, a["strategy_signal"], h,
                signal.get("stop_loss_pct"), signal.get("take_profit_pct"),
            ) for h in (4, 12, 24)},
        })
    summaries = {}
    for group in ("all", "nonoverlap_24h"):
        selected = [s for s in samples if group == "all" or s["nonoverlap_24h"]]
        summaries[group] = {}
        for h in (4, 12, 24):
            valid = [s["windows"][str(h)] for s in selected if s["windows"][str(h)]["status"] == "ok"]
            returns = [w["directional_return_pct"] for w in valid]
            summaries[group][str(h)] = {
                "valid": len(valid), "excluded": len(selected) - len(valid),
                "positive": sum(r > 0 for r in returns), "negative": sum(r < 0 for r in returns),
                "mean_return_pct": mean(returns) if returns else None,
                "median_return_pct": median(returns) if returns else None,
                "mean_mfe_pct": mean(w["mfe_pct"] for w in valid) if valid else None,
                "mean_mae_pct": mean(w["mae_pct"] for w in valid) if valid else None,
                "first_touch_counts": dict(Counter(w["fixed_level_first_touch"] for w in valid)),
            }
    return {"start": start.isoformat(), "end": end.isoformat(), "sample_count": len(samples), "summary": summaries, "samples": samples}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, default=Path("/srv/open-trade-deployment/logs"))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    print(json.dumps(analyze(args.log_dir, timestamp(args.start), timestamp(args.end)), ensure_ascii=False, indent=2))
