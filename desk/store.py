"""SQLite ledger: every post (planned, published, cancelled), calendar
confirmations, the signals the planner has already seen, and small state
(mourning period, last planner run)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,          -- news|occasion|condolence|celebration|insight|event
    occasion_key    TEXT NOT NULL DEFAULT '',
    story_id        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,          -- awaiting_approval|scheduled|published|failed|cancelled|rejected
    urgent          INTEGER NOT NULL DEFAULT 0,
    scheduled_at    TEXT,
    published_at    TEXT,
    text            TEXT NOT NULL,
    hashtags        TEXT NOT NULL DEFAULT '[]',
    format          TEXT NOT NULL,          -- text|image|multi_image|article
    images          TEXT NOT NULL DEFAULT '[]',  -- [{path, alt_text, credit, source, download_ping}]
    link_url        TEXT NOT NULL DEFAULT '',
    link_title      TEXT NOT NULL DEFAULT '',
    tone            TEXT NOT NULL DEFAULT '',
    opening_style   TEXT NOT NULL DEFAULT '',
    length_band     TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    facts           TEXT NOT NULL DEFAULT '[]',
    critic          TEXT NOT NULL DEFAULT '{}',
    linkedin_urn    TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    attempts        INTEGER NOT NULL DEFAULT 0,
    run_id          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_status ON posts(status, scheduled_at);
CREATE INDEX IF NOT EXISTS posts_occasion ON posts(occasion_key);

CREATE TABLE IF NOT EXISTS calendar_confirmed (
    key         TEXT NOT NULL,              -- occasion key, e.g. eid-al-fitr
    year        INTEGER NOT NULL,
    date        TEXT NOT NULL,              -- YYYY-MM-DD (first day)
    end_date    TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    source_url  TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (key, year)
);

CREATE TABLE IF NOT EXISTS calendar_discovered (
    key         TEXT PRIMARY KEY,           -- slug, e.g. gitex-global-2027
    name        TEXT NOT NULL,
    date        TEXT NOT NULL,
    end_date    TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT 'event',
    why         TEXT NOT NULL DEFAULT '',
    source_url  TEXT NOT NULL DEFAULT '',
    found_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals_seen (
    story_id    TEXT PRIMARY KEY,
    item_count  INTEGER NOT NULL,
    decision    TEXT NOT NULL DEFAULT '',
    seen_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    report      TEXT NOT NULL DEFAULT '{}'
);
"""

JSON_COLS = {"hashtags", "images", "facts", "critic"}
LIVE = "('scheduled','awaiting_approval','published','dry_published')"


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.db:
            yield self.db

    # ---- state ------------------------------------------------------------

    def get_state(self, key: str, default: Any = None) -> Any:
        r = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_state(self, key: str, value: Any) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value, default=str)))

    # ---- posts ------------------------------------------------------------

    @staticmethod
    def post_row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in JSON_COLS:
            d[k] = json.loads(d[k]) if d.get(k) else ([] if k != "critic" else {})
        return d

    def add_post(self, **fields: Any) -> int:
        fields.setdefault("created_at", iso(now_utc()))
        for k in JSON_COLS:
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        cols = ",".join(fields)
        marks = ",".join("?" * len(fields))
        with self.tx() as db:
            cur = db.execute(f"INSERT INTO posts ({cols}) VALUES ({marks})", tuple(fields.values()))
            return int(cur.lastrowid)

    def update_post(self, post_id: int, **fields: Any) -> None:
        for k in JSON_COLS:
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        sets = ",".join(f"{k}=?" for k in fields)
        with self.tx() as db:
            db.execute(f"UPDATE posts SET {sets} WHERE id=?", (*fields.values(), post_id))

    def get_post(self, post_id: int) -> dict | None:
        return self.post_row(self.db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone())

    def posts(self, where: str = "1=1", params: tuple = (), order: str = "COALESCE(published_at, scheduled_at) DESC",
              limit: int = 50) -> list[dict]:
        rows = self.db.execute(f"SELECT * FROM posts WHERE {where} ORDER BY {order} LIMIT ?", (*params, limit))
        return [self.post_row(r) for r in rows]

    def published(self, limit: int = 30, include_dry: bool = False) -> list[dict]:
        statuses = "('published','dry_published')" if include_dry else "('published')"
        return self.posts(f"status IN {statuses}", order="published_at DESC", limit=limit)

    def pending(self) -> list[dict]:
        return self.posts("status IN ('scheduled','awaiting_approval')", order="scheduled_at ASC", limit=100)

    def occasion_handled(self, key: str) -> bool:
        r = self.db.execute(
            f"SELECT 1 FROM posts WHERE occasion_key=? AND status IN {LIVE}", (key,)).fetchone()
        return r is not None

    def story_handled(self, story_id: str) -> bool:
        r = self.db.execute(
            f"SELECT 1 FROM posts WHERE story_id=? AND status IN {LIVE}",
            (story_id,)).fetchone()
        return r is not None

    # ---- signals seen -----------------------------------------------------

    def seen_signals(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute("SELECT story_id, item_count FROM signals_seen")}

    def mark_seen(self, story_id: str, item_count: int, decision: str) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO signals_seen VALUES (?,?,?,?)",
                       (story_id, item_count, decision[:300], iso(now_utc())))

    # ---- runs -------------------------------------------------------------

    def start_run(self, run_id: str, kind: str) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO runs (id, kind, started_at) VALUES (?,?,?)", (run_id, kind, iso(now_utc())))

    def finish_run(self, run_id: str, report: dict) -> None:
        with self.tx() as db:
            db.execute("UPDATE runs SET finished_at=?, report=? WHERE id=?",
                       (iso(now_utc()), json.dumps(report, ensure_ascii=False, default=str)[:200000], run_id))
