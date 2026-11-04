"""Rebuildable SQLite/FTS5 projection. Never authorizes a version or rewrites PG."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from trading_agent.domain.serialization import canonical_json


def terms(text: str):
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower())
    result = []
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            result.extend(word[i : i + 2] for i in range(max(1, len(word) - 1)))
        else:
            result.append(word)
    return list(dict.fromkeys(result))


class KnowledgeIndex:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=2)
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS versions (
                id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, body TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS summaries USING fts5(
                version_id UNINDEXED, search_text, tokenize='unicode61');
        """)
        return connection

    def upsert(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            for row in rows:
                ident = str(row["id"])
                existing = connection.execute(
                    "SELECT content_hash FROM versions WHERE id=?", (ident,)
                ).fetchone()
                if existing and existing[0] != row["content_hash"]:
                    raise ValueError("SQLite projection conflicts with immutable version")
                connection.execute(
                    "INSERT OR IGNORE INTO versions VALUES (?,?,?)",
                    (ident, row["content_hash"], canonical_json(row)),
                )
                connection.execute("DELETE FROM summaries WHERE version_id=?", (ident,))
                searchable = " ".join(
                    str(row.get(k) or "")
                    for k in ("title", "summary", "symbol", "timeframe", "structure", "stage")
                )
                connection.execute(
                    "INSERT INTO summaries VALUES (?,?)", (ident, " ".join(terms(searchable)))
                )

    def rank(self, rows, query: str):
        """Fail if projection lacks ANY eligible version, so lag cannot hide evidence."""
        if not self.path.is_file():
            raise OSError("Knowledge index is unavailable")
        identifiers = {str(r["id"]): r["content_hash"] for r in rows}
        with closing(self._connect()) as connection:
            placeholders = ",".join("?" for _ in identifiers) or "NULL"
            indexed = dict(
                connection.execute(
                    f"SELECT id,content_hash FROM versions WHERE id IN ({placeholders})",
                    tuple(identifiers),
                )
            )
            if any(indexed.get(k) != v for k, v in identifiers.items()):
                raise OSError("Knowledge index is behind PostgreSQL")
            tokens = terms(query)
            if not tokens:
                return list(identifiers)
            match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
            return [
                r[0]
                for r in connection.execute(
                    "SELECT version_id FROM summaries WHERE summaries MATCH ? "
                    f"AND version_id IN ({placeholders}) ORDER BY bm25(summaries),version_id",
                    (match, *identifiers),
                )
                if r[0] in identifiers
            ]

    def rebuild(self, rows):
        # Replace projection contents in one SQLite transaction; readers see old or new set.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM summaries")
            connection.execute("DELETE FROM versions")
            for row in rows:
                ident = str(row["id"])
                searchable = " ".join(
                    str(row.get(k) or "")
                    for k in ("title", "summary", "symbol", "timeframe", "structure", "stage")
                )
                connection.execute(
                    "INSERT INTO versions VALUES (?,?,?)",
                    (ident, row["content_hash"], canonical_json(row)),
                )
                connection.execute(
                    "INSERT INTO summaries VALUES (?,?)", (ident, " ".join(terms(searchable)))
                )
