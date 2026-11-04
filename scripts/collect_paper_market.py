"""Collect public evidence while a paper deployment is being prepared.

This is a deployment-preparation collector, separate from the prediction runtime.
It can use the explicit GET-only credentials transport; no model, orders or DB writes.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from trading_agent.config import load_config
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import canonical_json
from trading_agent.domain.time import TIMEFRAMES, boundary
from trading_agent.market.authenticated import create_market_client
from trading_agent.market.features import compute_features
from trading_quant import CalculationError
from trading_quant.structures import detect_structures


def write_new(path, value):
    payload = canonical_json(value) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            # Atomic publication; an existing fact cannot be replaced.
            os.link(temporary, path)
        finally:
            temporary.unlink()


def status(output, **values):
    target = output / "collector-status.json"
    temporary = output / ".collector-status.tmp"
    temporary.write_text(
        canonical_json({"updated_at": datetime.now(UTC), **values}) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


async def capture(client, config, output):
    anchor = boundary(datetime.now(UTC), "1h")
    deadline = datetime.now(UTC) + timedelta(seconds=40)
    access = await client.check_clock(deadline)
    windows = []
    for symbol in config.prediction.symbols:
        inst = symbol.removesuffix("USDT") + "-USDT-SWAP"
        await client.instrument(deadline, inst_id=inst)
        for timeframe in TIMEFRAMES:
            bars = await client.candles(anchor, timeframe, deadline, inst_id=inst)
            as_of = datetime.now(UTC)
            structures = detect_structures(bars, as_of)
            features = compute_features(bars, config.features, decision_at=as_of)
            windows.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "as_of": as_of,
                    "bars": bars,
                    "features": features,
                    "structures": [asdict(value) for value in structures],
                }
            )
    value = {
        "schema_version": 1,
        "purpose": "PAPER_DEPLOYMENT_PREFLIGHT_ONLY",
        "source": "OKX_PUBLIC_REAL",
        "access_mode": "USER_API_GET_ONLY"
        if access is not None
        else "PUBLIC_UNAUTHENTICATED",
        "api_access": access,
        "anchor_at": anchor,
        "captured_at": datetime.now(UTC),
        "observation_policy": "optional derivatives observations not requested; missing stays missing",
        "not_formal_prediction_input": True,
        "windows": windows,
    }
    path = (
        output
        / "market"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex + ".json")
    )
    write_new(path, value)
    return {
        "snapshot": path.name,
        "windows": len(windows),
        "bars": sum(len(w["bars"]) for w in windows),
        "access_mode": value["access_mode"],
    }


async def run(args):
    config = load_config(args.config)
    output = args.output.resolve(strict=True)
    manifest = json.loads((output / "manifest.json").read_text())
    if manifest.get("mode") != "paper":
        raise SystemExit("Explicit paper session manifest required")
    (output / "market").mkdir(exist_ok=True)
    with (output / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("This session already has a collector") from None
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        client = create_market_client(config.market, feature_config=config.features)
        last_snapshot = None
        failures = 0
        try:
            while not stop.is_set():
                try:
                    last_snapshot = await capture(client, config, output)
                    state = "COLLECTING_PUBLIC_MARKET_ONLY"
                    error = None
                except (DomainError, CalculationError, ValueError) as exc:
                    failures += 1
                    state = "AWAITING_PUBLIC_MARKET"
                    error = getattr(exc, "code", type(exc).__name__)
                event = {
                    "at": datetime.now(UTC),
                    "state": state,
                    "error_code": error,
                    "last_snapshot": last_snapshot,
                    "trading_started": False,
                    "model_predictions_started": False,
                }
                with (output / "collector-events.jsonl").open(
                    "a", encoding="utf-8"
                ) as handle:
                    handle.write(canonical_json(event) + "\n")
                status(output, pid=os.getpid(), failures=failures, **event)
                print(canonical_json(event), flush=True)
                if args.once:
                    return 0 if error is None else 1
                try:
                    await asyncio.wait_for(stop.wait(), timeout=args.interval)
                except TimeoutError:
                    pass
        finally:
            await client.close()
            status(
                output,
                state="STOPPED",
                last_snapshot=last_snapshot,
                failures=failures,
                trading_started=False,
            )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=3600)
    args = parser.parse_args()
    if args.interval < 60:
        parser.error("interval must be at least 60 seconds")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
