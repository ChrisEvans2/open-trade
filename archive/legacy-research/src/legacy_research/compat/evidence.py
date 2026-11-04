from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from legacy_research.compat.contracts import LegacyPolicy, project_path, review_range
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import canonical_json, content_hash, json_value
from trading_agent.domain.time import utc

EVENT_FIELDS = (
    "timestamp",
    "event",
    "symbol",
    "side",
    "strategy_type",
    "regime",
    "reason",
    "pnl_amount",
    "exchange_net_pnl_amount",
    "funding_fee_amount",
    "entry_price",
    "fill_price",
    "max_favorable_excursion_pct",
    "max_adverse_excursion_pct",
)
ACCOUNT_FIELDS = (
    "equity",
    "realized_pnl_amount",
    "total_unrealized_pnl_amount",
    "margin_used_pct",
    "positions",
    "open_position_prices",
    "open_position_margin_amount",
    "open_position_leverage",
    "last_mark_update",
)


def storage_root(policy: LegacyPolicy) -> Path:
    root = project_path(policy.repository, "workspaces/v1-compat")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    return root


def write_once(path: Path, value):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(canonical_json(value))
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_snapshot(policy, ident):
    path = storage_root(policy) / "evidence" / f"{ident}.json"
    if path.is_symlink() or not path.is_file():
        raise DomainError("LEGACY_EVIDENCE_NOT_FOUND", "成交复盘证据快照不存在", 404)
    result = json.loads(path.read_text())
    digest = result.pop("content_hash")
    if digest != content_hash(result):
        raise DomainError("LEGACY_EVIDENCE_CHANGED", "成交证据快照校验失败", 409)
    return {**result, "content_hash": digest}


def _amount(value):
    if value is None:
        return None
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("成交金额不是有限数")
    return result


def metrics(events):
    closed = [event for event in events if event["event"] == "close"]
    values = [
        _amount(event.get("exchange_net_pnl_amount"))
        if event.get("exchange_net_pnl_amount") is not None
        else _amount(event.get("pnl_amount"))
        for event in closed
    ]
    known = [value for value in values if value is not None]
    complete = len(known) == len(closed)
    profit = sum((value for value in known if value > 0), Decimal(0))
    loss = -sum((value for value in known if value < 0), Decimal(0))
    return {
        "closed_trade_count": len(closed),
        "pnl_sample_count": len(known),
        "missing_pnl_count": len(closed) - len(known),
        "known_pnl_subtotal": sum(known, Decimal(0)),
        "reported_closed_pnl": sum(known, Decimal(0)) if complete and closed else None,
        "win_rate": Decimal(sum(value > 0 for value in known)) / len(known) if known else None,
        "profit_factor": profit / loss if loss else None,
        "funding_event_count": sum(event["event"] == "funding_fee" for event in events),
        "pnl_basis": "exchange_net_pnl_amount优先，否则使用原日志pnl_amount；不重复加减资金费",
    }


def capture_review(policy: LegacyPolicy, start, end, *, decision=None):
    start, end, decision = review_range(start, end, decision or datetime.now(UTC))
    groups = {}
    for mode in ("live", "paper"):
        path = project_path(policy.repository, f"logs/{mode}_trade_events.jsonl")
        events, duplicates, partial, seen = [], 0, 0, set()
        quality = "MISSING"
        if path.exists():
            if path.stat().st_size > policy.max_source_bytes:
                raise DomainError("LEGACY_SOURCE_TOO_LARGE", "成交日志超过本次读取限额", 413)
            raw = path.read_bytes()
            if len(raw) > policy.max_source_bytes:
                raise DomainError("LEGACY_SOURCE_TOO_LARGE", "成交日志读取期间超过限额", 413)
            lines = raw.splitlines(keepends=True)
            for line_no, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                if line_no == len(lines) and not line.endswith(b"\n"):
                    partial += 1  # The append-only writer may still be writing this record.
                    continue
                try:
                    source = json.loads(line, parse_float=Decimal)
                    timestamp = utc(datetime.fromisoformat(source["timestamp"]))
                    if not start <= timestamp < end:
                        continue
                    digest = content_hash(source)
                    if digest in seen:
                        duplicates += 1
                        continue
                    seen.add(digest)
                    item = {key: source[key] for key in EVENT_FIELDS if key in source}
                    if not isinstance(item.get("event"), str) or not isinstance(
                        item.get("symbol"), str
                    ):
                        raise ValueError("成交事件缺少类型或交易对")
                    item.update(
                        evidence_id=str(uuid5(NAMESPACE_URL, f"legacy:{mode}:{digest}")),
                        source_line=line_no,
                        source_hash=digest,
                    )
                    events.append(item)
                except (KeyError, ValueError, TypeError):
                    raise DomainError("LEGACY_SOURCE_INVALID", "完整成交日志行格式无效") from None
            quality = "PARTIAL_COVERAGE" if partial else "OK"
        account_path = project_path(policy.repository, f"state/{mode}_account.json")
        account, account_quality = None, "MISSING"
        if account_path.exists():
            if account_path.stat().st_size > 1_000_000:
                raise DomainError("LEGACY_SOURCE_TOO_LARGE", "账户状态超过读取限额", 413)
            source = json.loads(account_path.read_text(), parse_float=Decimal)
            updated = source.get("last_mark_update")
            if updated and utc(datetime.fromisoformat(updated)) <= decision:
                account = {key: source[key] for key in ACCOUNT_FIELDS if key in source}
                account_quality = "CURRENT_SNAPSHOT"
        stats = metrics(events)
        stats["by_symbol"] = {
            symbol: metrics([event for event in events if event["symbol"] == symbol])
            for symbol in sorted({event["symbol"] for event in events})
        }
        stats["by_side"] = {
            side: metrics([event for event in events if event.get("side") == side])
            for side in ("long", "short")
        }
        groups[mode] = {
            "source": str(path.relative_to(policy.repository)),
            "quality": quality,
            "ignored_duplicate_lines": duplicates,
            "ignored_incomplete_tail": partial,
            "events": events,
            "metrics": stats,
            "account": account,
            "account_quality": account_quality,
        }
    ident = uuid4()
    snapshot = json_value(
        {
            "snapshot_id": ident,
            "kind": "legacy_trade_evidence_v1",
            "range_start": start,
            "range_end": end,
            "captured_at": decision,
            "groups": groups,
            "attribution": (
                "live/paper分别统计；mark_update不是平仓；不自动关联v1预测或作为预测案例"
            ),
            "time_basis": "事件时间来自旧日志；首次可用时间未知，此快照仅用于当前兼容复盘",
        }
    )
    snapshot["content_hash"] = content_hash(snapshot)
    write_once(storage_root(policy) / "evidence" / f"{ident}.json", snapshot)
    return snapshot


def review_view(snapshot):
    """Keep the full frozen evidence on disk; bound the model-facing event examples."""
    groups = {}
    for mode, group in snapshot["groups"].items():
        closed = [event for event in group["events"] if event["event"] == "close"]
        groups[mode] = {
            **group,
            "events": closed[-30:],
            "event_examples_limit": 30,
            "total_frozen_event_count": len(group["events"]),
        }
    return {
        **snapshot,
        "groups": groups,
        "content_hash_scope": "完整磁盘快照；此视图仅缩减事件示例",
    }
