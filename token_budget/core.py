"""Normalized token usage ledger backed by SQLite."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Any


FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_creation_input_tokens", "reasoning_tokens", "total_tokens")


@dataclass(frozen=True)
class UsageEvent:
    source: str
    occurred_at: str | None
    session_id: str | None
    model: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    ordinal: int | None = None
    event_key: str | None = None
    request_id: str | None = None
    revision: int = 0
    revision_key: str = ""

    def identity(self) -> str:
        value = {"source": self.source, "session_id": self.session_id, "event_key": self.event_key}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Ledger:
    def __init__(self, database: str | Path):
        self.database = str(database)
        self.connection = sqlite3.connect(self.database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("""CREATE TABLE IF NOT EXISTS usage_events (
            event_id TEXT PRIMARY KEY, source TEXT NOT NULL, occurred_at TEXT,
            session_id TEXT, model TEXT, input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL,
            reasoning_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
            ordinal INTEGER, event_key TEXT, request_id TEXT, revision INTEGER NOT NULL DEFAULT 0,
            revision_key TEXT NOT NULL DEFAULT ''
        )""")
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(usage_events)")}
        if "cache_creation_input_tokens" not in columns:
            self.connection.execute("ALTER TABLE usage_events ADD COLUMN cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0")
        if "request_id" not in columns:
            self.connection.execute("ALTER TABLE usage_events ADD COLUMN request_id TEXT")
        if "revision" not in columns:
            self.connection.execute("ALTER TABLE usage_events ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        if "revision_key" not in columns:
            self.connection.execute("ALTER TABLE usage_events ADD COLUMN revision_key TEXT NOT NULL DEFAULT ''")
        self.connection.commit()

    def add(self, event: UsageEvent) -> bool:
        event_id = event.identity()
        values = asdict(event)
        existing = self.connection.execute("SELECT revision, revision_key FROM usage_events WHERE event_id = ?", (event_id,)).fetchone()
        if existing is not None:
            if event.revision_key:
                if existing[1] and existing[1] >= event.revision_key:
                    return False
            elif not existing[1] and existing[0] >= event.revision:
                return False
        cursor = self.connection.execute("""INSERT OR IGNORE INTO usage_events
            (event_id, source, occurred_at, session_id, model, input_tokens,
             output_tokens, cached_input_tokens, reasoning_tokens, total_tokens,
             cache_creation_input_tokens, ordinal, event_key, request_id, revision, revision_key)
            VALUES (:event_id, :source, :occurred_at, :session_id, :model,
                    :input_tokens, :output_tokens, :cached_input_tokens,
                    :reasoning_tokens, :total_tokens, :cache_creation_input_tokens,
                    :ordinal, :event_key, :request_id, :revision, :revision_key)""",
            {"event_id": event_id, **values})
        if cursor.rowcount == 0:
            cursor = self.connection.execute("""UPDATE usage_events SET
                source=:source, occurred_at=:occurred_at, session_id=:session_id,
                model=:model, input_tokens=:input_tokens, output_tokens=:output_tokens,
                cached_input_tokens=:cached_input_tokens,
                cache_creation_input_tokens=:cache_creation_input_tokens,
                reasoning_tokens=:reasoning_tokens, total_tokens=:total_tokens,
                ordinal=:ordinal, event_key=:event_key, request_id=:request_id,
                revision=:revision, revision_key=:revision_key
                WHERE event_id=:event_id AND
                  ((:revision_key != '' AND (revision_key = '' OR revision_key < :revision_key)) OR
                   (:revision_key = '' AND revision_key = '' AND revision < :revision))""",
                {"event_id": event_id, **values})
        inserted = cursor.rowcount == 1
        self.connection.commit()
        return inserted

    def add_many(self, events: Iterable[UsageEvent]) -> tuple[int, int]:
        added = skipped = 0
        for event in events:
            if self.add(event): added += 1
            else: skipped += 1
        return added, skipped

    def summary(self) -> dict[str, Any]:
        totals = {"count": 0, "input_tokens": 0, "output_tokens": 0,
                  "cached_input_tokens": 0, "cache_creation_input_tokens": 0,
                  "reasoning_tokens": 0, "total_tokens": 0}
        source_totals: dict[str, dict[str, int | str]] = {}
        rows = self.connection.execute("""SELECT source, input_tokens, output_tokens,
            cached_input_tokens, cache_creation_input_tokens, reasoning_tokens,
            total_tokens FROM usage_events""")
        for row in rows:
            totals["count"] += 1
            for key in ("input_tokens", "output_tokens", "cached_input_tokens",
                        "cache_creation_input_tokens", "reasoning_tokens", "total_tokens"):
                totals[key] += row[key]
            source = row["source"]
            grouped = source_totals.setdefault(source, {"source": source, "count": 0,
                "cache_creation_input_tokens": 0, "total_tokens": 0})
            grouped["count"] += 1
            grouped["cache_creation_input_tokens"] += row["cache_creation_input_tokens"]
            grouped["total_tokens"] += row["total_tokens"]
        return {**totals, "by_source": [source_totals[key] for key in sorted(source_totals)]}

    def close(self) -> None:
        self.connection.close()
