"""Derived SQLite index of iteration records; never a source of verified trading profit."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from legacy_research.compat.contracts import IterationRequest, iteration_payload, project_path
from legacy_research.compat.evidence import storage_root, write_once
from legacy_research.compat.experience_contracts import ExperiencePage, ExperienceQuery
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import canonical_json, content_hash, json_value
from trading_agent.domain.time import utc

MAX_DOCUMENT_BYTES = 2_000_000
MAX_RECALL_BYTES = 12_000_000
TERMINAL = {"APPLIED", "VERIFIED_CANDIDATE", "TEST_FAILED", "ROLLED_BACK"}
TOPICS = {
    "entry_filter": ("入场", "准入", "突破", "breakout", "entry", "confirmation", "cooldown"),
    "exit_management": ("止损", "止盈", "退出", "trailing", "stop-loss", "takeprofit", "exit"),
    "market_regime": ("行情", "趋势", "震荡", "regime", "trend", "range", "waterfall"),
    "position_sizing": ("仓位", "保证金", "杠杆", "position", "margin", "leverage", "sizing"),
    "execution_safety": ("执行", "订单", "权限", "execution", "order", "schema", "safety"),
    "data_quality": ("数据", "统计", "采集", "data", "funding", "timestamp", "diagnostic"),
}
BOUNDARY = "历史材料仅供假设与验证参考；测试通过不证明收益改善，不作为执行指令"


def normalize(value):
    return unicodedata.normalize("NFKC", value).casefold()


def summary_view(summary):
    """Bound model-facing metadata too; full labels remain available to SQL filters."""
    paths, symbols = summary["strategy_paths"], summary["symbols"]
    return {
        **summary,
        "strategy_paths": [p[:240] for p in paths[:8]],
        "symbols": symbols[:10],
        "metadata_counts": {"strategy_paths": len(paths), "symbols": len(symbols)},
        "metadata_truncated": len(paths) > 8
        or len(symbols) > 10
        or any(len(p) > 240 for p in paths[:8]),
    }


def _summary(text):
    title = next(
        (line.lstrip("# ") for line in text.splitlines() if line.startswith("# ")), "历史策略迭代"
    )
    match = re.search(
        r"^##\s+(?:策略假设|Strategy Hypothesis)\s*\n(.*?)(?=^##|\Z)",
        text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    body = match.group(1) if match else text.removeprefix(text.splitlines()[0] if text else "")
    body = " ".join(line.strip("# -*`") for line in body.splitlines() if line.strip())
    return (title[:120] + "：" + body)[:320]


class ExperienceStore:
    def __init__(self, policy):
        self.policy = policy
        self.root = storage_root(policy)

    @contextmanager
    def _lock(self):
        path = project_path(self.policy.repository, "workspaces/v1-compat/experience.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DomainError("EXPERIENCE_BUSY", "另一个经验索引操作正在执行", 409) from None
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _database(self):
        relative = "workspaces/v1-compat/experiences.sqlite"
        path = project_path(self.policy.repository, relative)
        for suffix in ("-journal", "-wal", "-shm"):
            project_path(self.policy.repository, relative + suffix)
        connection = None
        try:
            connection = sqlite3.connect(path, timeout=3)
            os.chmod(path, 0o600)
            connection.row_factory = sqlite3.Row
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise DomainError("EXPERIENCE_SCHEMA_INVALID", "经验索引版本不兼容", 409)
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS experiences (
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL, version INTEGER NOT NULL,
                    source_hash TEXT NOT NULL, available_at TEXT NOT NULL,
                    summary TEXT NOT NULL, content TEXT NOT NULL, search_text TEXT NOT NULL,
                    UNIQUE(source_key, version)
                );
                CREATE INDEX IF NOT EXISTS experience_source ON experiences(source_key, version);
                CREATE VIRTUAL TABLE IF NOT EXISTS experience_fts
                    USING fts5(id UNINDEXED, search_text, tokenize='trigram');
                PRAGMA user_version=1;
            """)
            yield connection
        except sqlite3.Error:
            raise DomainError(
                "EXPERIENCE_INDEX_UNAVAILABLE", "经验索引需要SQLite FTS5/trigram或索引需核查", 503
            ) from None
        finally:
            if connection:
                connection.close()

    def _collect(self, as_of):
        """Read authorized records before opening a SQLite write transaction."""
        documents, covered, cache = [], set(), {}
        total = 0

        def read(relative):
            nonlocal total
            if relative in cache:
                return cache[relative]
            path = project_path(self.policy.repository, relative)
            bound = (
                self.policy.max_source_bytes
                if relative.startswith("workspaces/v1-compat/evidence/")
                else MAX_DOCUMENT_BYTES
            )
            if path.stat().st_size > bound:
                raise DomainError("EXPERIENCE_TOO_LARGE", "单条历史材料超过经验读取限额", 413)
            raw = path.read_bytes()
            total += len(raw)
            if len(raw) > bound or total > self.policy.max_source_bytes:
                raise DomainError("EXPERIENCE_TOO_LARGE", "历史经验索引输入超过读取限额", 413)
            cache[relative] = raw.decode("utf-8")
            return cache[relative]

        iterations = project_path(self.policy.repository, "workspaces/v1-compat/iterations")
        if iterations.exists():
            folders = sorted(iterations.iterdir())
            if len(folders) > 2000:
                raise DomainError("EXPERIENCE_TOO_LARGE", "历史迭代超过索引输入限额", 413)
            for folder in folders:
                project_path(
                    self.policy.repository, str(folder.relative_to(self.policy.repository))
                )
                if not folder.is_dir():
                    continue
                try:
                    UUID(folder.name)
                except ValueError:
                    raise DomainError("EXPERIENCE_SOURCE_INVALID", "迭代记录目录ID无效") from None
                base = str(folder.relative_to(self.policy.repository))
                event_files = sorted(folder.glob("event-*.json"))
                if len(event_files) > 100:
                    raise DomainError("EXPERIENCE_TOO_LARGE", "单次迭代事件过多", 413)
                events = [json.loads(read(f"{base}/{path.name}")) for path in event_files]
                result_event = next((e for e in reversed(events) if e.get("result")), None)
                if result_event is None or result_event["status"] not in TERMINAL:
                    continue  # Partial journals are not completed experiences.
                wrapped = json.loads(read(base + "/request.json"))
                request = IterationRequest.model_validate(wrapped["request"])
                if str(request.iteration_id) != folder.name or wrapped["hash"] != content_hash(
                    iteration_payload(request)
                ):
                    raise DomainError("EXPERIENCE_SOURCE_INVALID", "历史迭代请求hash或ID不一致")
                tested = next((e for e in reversed(events) if e["status"] == "TESTED"), None)
                if tested is None or type(tested["exit_code"]) is not int:
                    raise DomainError("EXPERIENCE_SOURCE_INVALID", "历史迭代缺少程序测试结果")
                status = result_event["status"]
                if (status == "TEST_FAILED") != (
                    tested["exit_code"] != 0
                ) and status != "ROLLED_BACK":
                    raise DomainError("EXPERIENCE_SOURCE_INVALID", "历史迭代状态与测试结果不一致")
                snapshot = json.loads(
                    read(f"workspaces/v1-compat/evidence/{request.evidence_snapshot_id}.json")
                )
                snapshot_hash = snapshot.pop("content_hash")
                if content_hash(snapshot) != snapshot_hash or snapshot["snapshot_id"] != str(
                    request.evidence_snapshot_id
                ):
                    raise DomainError("EXPERIENCE_SOURCE_INVALID", "历史成交证据hash不一致")
                plan = json.loads(read(base + "/plan.json"))
                expected_changes = {item.path: item for item in request.changes}
                if (
                    plan["iteration_id"] != str(request.iteration_id)
                    or len(plan["changes"]) != len(expected_changes)
                    or {item["path"] for item in plan["changes"]} != set(expected_changes)
                    or any(
                        item["after"] != expected_changes[item["path"]].content
                        or (
                            hashlib.sha256(item["before"].encode()).hexdigest()
                            if item["before"] is not None
                            else None
                        )
                        != expected_changes[item["path"]].before_sha256
                        for item in plan["changes"]
                    )
                ):
                    raise DomainError("EXPERIENCE_SOURCE_INVALID", "历史补丁与冻结请求不一致")
                record = result_event["result"].get("record_path")
                if record:
                    # Records may have moved with the repository; only the filename is used.
                    covered.add(str(record).replace("\\", "/").rsplit("/", 1)[-1])
                completed = utc(datetime.fromisoformat(result_event["at"]))
                if completed > as_of:
                    continue
                metrics = {mode: value["metrics"] for mode, value in snapshot["groups"].items()}
                content = canonical_json(
                    {
                        "hypothesis": request.hypothesis,
                        "risk": request.risk,
                        "rollback": request.rollback,
                        "changes": plan["changes"],
                        "test": {
                            key: tested[key]
                            for key in ("exit_code", "command", "output", "sandbox_id")
                        },
                        "status": status,
                        "evidence_snapshot_id": request.evidence_snapshot_id,
                        "evidence_hash": snapshot_hash,
                        "evidence_metrics": metrics,
                        "completed_at": completed,
                        "boundary": BOUNDARY,
                        "deployed": False,
                    }
                )
                documents.append(
                    {
                        "source_key": "iteration:" + folder.name,
                        "source_kind": "structured_iteration",
                        "source_path": base,
                        "summary": request.hypothesis[:320],
                        "content": content,
                        "status": status,
                        "test_exit_code": tested["exit_code"],
                        "strategy_paths": [
                            c.path for c in request.changes if c.path.startswith("src/")
                        ],
                        "symbols": sorted(
                            {symbol for m in metrics.values() for symbol in m.get("by_symbol", {})}
                        ),
                        "sample_counts": {
                            mode: m["closed_trade_count"] for mode, m in metrics.items()
                        },
                    }
                )
        records = project_path(self.policy.repository, "Strategy-iteration")
        if records.exists():
            paths = sorted(records.glob("*.md"))
            if len(paths) + len(documents) > 2000:
                raise DomainError("EXPERIENCE_TOO_LARGE", "历史记录超过索引输入限额", 413)
            for path in paths:
                relative = "Strategy-iteration/" + path.name
                project_path(self.policy.repository, relative)
                if path.name in covered:
                    continue
                text = read(relative)
                paths = sorted(set(re.findall(r"src/trading_system/strategy/[\w/.-]+\.py", text)))
                documents.append(
                    {
                        "source_key": "markdown:" + relative,
                        "source_kind": "legacy_markdown",
                        "source_path": relative,
                        "summary": _summary(text),
                        "content": text,
                        "status": "UNVERIFIED_RECORD",
                        "test_exit_code": None,
                        "strategy_paths": paths,
                        "symbols": sorted(
                            set(re.findall(r"\b[A-Z0-9]{2,20}(?:USDT|USDC)\b", text))
                        ),
                        "sample_counts": None,
                    }
                )
        if len(documents) > 2000:
            raise DomainError("EXPERIENCE_TOO_LARGE", "历史经验超过2000条索引输入限额", 413)
        for doc in documents:
            text = normalize(doc["summary"] + " " + doc["content"])
            doc["categories"] = [
                key for key, words in TOPICS.items() if any(w in text for w in words)
            ] or ["general"]
            doc["classification_version"] = "iteration_topics_v1"
            doc["source_hash"] = content_hash(doc)
            doc["search_text"] = text
            if len(doc["content"].encode()) > MAX_DOCUMENT_BYTES:
                raise DomainError("EXPERIENCE_TOO_LARGE", "经验正文超过限额", 413)
        return documents

    def search(self, query: ExperienceQuery, *, now=None):
        query = ExperienceQuery.model_validate(query)
        with self._lock():
            try:
                documents = self._collect(utc(now or datetime.now(UTC)))
            except (ValueError, KeyError, TypeError, OSError):
                raise DomainError(
                    "EXPERIENCE_SOURCE_INVALID", "历史经验来源格式无效，未建立召回清单"
                ) from None
            observed = utc(now or datetime.now(UTC))
            available = json_value(observed)
            with self._database() as connection:
                with connection:
                    for doc in documents:
                        prior = connection.execute(
                            "SELECT version, source_hash FROM experiences WHERE source_key=? "
                            "ORDER BY version DESC LIMIT 1",
                            (doc["source_key"],),
                        ).fetchone()
                        if prior and prior["source_hash"] == doc["source_hash"]:
                            continue
                        version = prior["version"] + 1 if prior else 1
                        ident = str(uuid4())
                        summary = {
                            key: value
                            for key, value in doc.items()
                            if key not in {"content", "search_text"}
                        }
                        summary.update(experience_id=ident, version=version, available_at=available)
                        summary["content_sha256"] = hashlib.sha256(
                            doc["content"].encode()
                        ).hexdigest()
                        summary["boundary"] = BOUNDARY
                        connection.execute(
                            "INSERT INTO experiences VALUES (?,?,?,?,?,?,?,?)",
                            (
                                ident,
                                doc["source_key"],
                                version,
                                doc["source_hash"],
                                available,
                                canonical_json(summary),
                                doc["content"],
                                doc["search_text"],
                            ),
                        )
                        connection.execute(
                            "INSERT INTO experience_fts VALUES (?,?)", (ident, doc["search_text"])
                        )
                clauses = [
                    "d.available_at <= ?",
                    "NOT EXISTS (SELECT 1 FROM experiences n WHERE n.source_key=d.source_key "
                    "AND n.version>d.version AND n.available_at<=?)",
                ]
                parameters = [available, available]
                terms = list(dict.fromkeys(normalize(query.query).split()))
                long_terms = [term for term in terms if len(term) >= 3]
                join, ordering = "", "d.available_at DESC, d.id"
                if long_terms:
                    join = " JOIN experience_fts ON experience_fts.id=d.id"
                    clauses.append("experience_fts MATCH ?")
                    parameters.append(
                        " AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
                    )
                    ordering = "bm25(experience_fts), " + ordering
                for term in terms:
                    clauses.append("instr(d.search_text, ?) > 0")
                    parameters.append(term)
                for key, values in (
                    ("categories", query.categories),
                    ("strategy_paths", query.strategy_paths),
                    ("symbols", query.symbols),
                ):
                    if values:
                        placeholders = ",".join("?" for _ in values)
                        clauses.append(
                            f"EXISTS (SELECT 1 FROM json_each(d.summary, '$.{key}') "
                            f"WHERE value IN ({placeholders}))"
                        )
                        parameters.extend(values)
                rows = connection.execute(
                    "SELECT d.* FROM experiences d"
                    + join
                    + " WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY "
                    + ordering,
                    parameters,
                ).fetchall()
            current = {doc["source_key"] for doc in documents}
            selected = [row for row in rows if row["source_key"] in current][: query.limit]
            entries = {
                row["id"]: {"summary": json.loads(row["summary"]), "content": row["content"]}
                for row in selected
            }
            recall = {
                "recall_id": uuid4(),
                "decision_at": observed,
                "query": query,
                "entries": entries,
            }
            recall["content_hash"] = content_hash(recall)
            if len(canonical_json(recall).encode()) > MAX_RECALL_BYTES:
                raise DomainError("EXPERIENCE_TOO_LARGE", "冻结经验清单超过限额", 413)
            write_once(self.root / "memory-recalls" / f"{recall['recall_id']}.json", recall)
            return json_value(
                {
                    "recall_id": recall["recall_id"],
                    "decision_at": observed,
                    "summaries": [summary_view(entry["summary"]) for entry in entries.values()],
                    "match_basis": "主题/币种/路径筛选；关键词全部匹配；FTS5排序；短词子串匹配",
                    "boundary": BOUNDARY,
                }
            )

    def recall(self, ident):
        ident = UUID(str(ident))
        path = project_path(
            self.policy.repository, f"workspaces/v1-compat/memory-recalls/{ident}.json"
        )
        if not path.is_file():
            raise DomainError("EXPERIENCE_RECALL_NOT_FOUND", "冻结经验清单不存在", 404)
        if path.stat().st_size > MAX_RECALL_BYTES:
            raise DomainError("EXPERIENCE_TOO_LARGE", "冻结经验清单超过限额", 413)
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_RECALL_BYTES:
                raise DomainError("EXPERIENCE_TOO_LARGE", "冻结经验清单超过限额", 413)
            value = json.loads(raw)
            digest = value.pop("content_hash")
            if content_hash(value) != digest or value["recall_id"] != str(ident):
                raise DomainError("EXPERIENCE_RECALL_CHANGED", "冻结经验清单校验失败", 409)
            for key, entry in value["entries"].items():
                if (
                    key != entry["summary"]["experience_id"]
                    or hashlib.sha256(entry["content"].encode()).hexdigest()
                    != entry["summary"]["content_sha256"]
                ):
                    raise DomainError("EXPERIENCE_RECALL_CHANGED", "经验正文与摘要不一致", 409)
        except (ValueError, KeyError, TypeError):
            raise DomainError("EXPERIENCE_RECALL_CHANGED", "冻结经验清单格式无效", 409) from None
        return {**value, "content_hash": digest}

    def read(self, recall_id, experience_id, *, page=None):
        page = ExperiencePage.model_validate(page or {})
        recall = self.recall(recall_id)
        entry = recall["entries"].get(str(UUID(str(experience_id))))
        if entry is None:
            raise DomainError("EXPERIENCE_NOT_AUTHORIZED", "经验ID不在此冻结清单中", 403)
        content = entry["content"]
        if page.offset > len(content):
            raise DomainError("EXPERIENCE_PAGE_INVALID", "正文偏移超过内容长度", 400)
        end = min(page.offset + page.limit, len(content))
        return {
            "recall_id": str(recall_id),
            "summary": summary_view(entry["summary"]),
            "content": content[page.offset : end],
            "offset": page.offset,
            "next_offset": end if end < len(content) else None,
            "total_chars": len(content),
            "content_format": "json"
            if entry["summary"]["source_kind"] == "structured_iteration"
            else "markdown",
        }

    def references(self, recall_id, experience_ids):
        recall = self.recall(recall_id)
        if utc(datetime.fromisoformat(recall["decision_at"])) > datetime.now(UTC):
            raise DomainError("EXPERIENCE_RECALL_CHANGED", "经验清单时点晚于当前时间", 409)
        if not set(map(str, experience_ids)) <= set(recall["entries"]):
            raise DomainError("EXPERIENCE_NOT_AUTHORIZED", "迭代引用了清单外经验", 403)
        return {
            "recall_id": str(recall_id),
            "decision_at": recall["decision_at"],
            "recall_hash": recall["content_hash"],
            "entries": {str(i): recall["entries"][str(i)] for i in experience_ids},
        }
