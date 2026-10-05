"""Chat history for the UI: a local SQLite file used by Chainlit's SQLAlchemy data layer.

    data/askgraph.db      tables: users, threads, steps, elements, feedbacks

The table/column names match what chainlit 2.12's `chainlit.data.sql_alchemy.SQLAlchemyDataLayer`
reads and writes (it quotes camelCase names, so they are quoted here too). Types are SQLite-
friendly: JSON is stored as TEXT, timestamps as ISO-8601 TEXT. No passwords are stored here.
Wipe all history by deleting the file; it is recreated on the next start.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import ROOT

DB_PATH = ROOT / "data" / "askgraph.db"

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        "id"         TEXT PRIMARY KEY,
        "identifier" TEXT NOT NULL UNIQUE,
        "metadata"   TEXT NOT NULL DEFAULT '{}',
        "createdAt"  TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS threads (
        "id"             TEXT PRIMARY KEY,
        "createdAt"      TEXT,
        "name"           TEXT,
        "userId"         TEXT REFERENCES users("id") ON DELETE CASCADE,
        "userIdentifier" TEXT,
        "tags"           TEXT,
        "metadata"       TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS steps (
        "id"            TEXT PRIMARY KEY,
        "name"          TEXT NOT NULL,
        "type"          TEXT NOT NULL,
        "threadId"      TEXT NOT NULL,
        "parentId"      TEXT,
        "command"       TEXT,
        "modes"         TEXT,
        "streaming"     BOOLEAN,
        "waitForAnswer" BOOLEAN,
        "isError"       BOOLEAN,
        "metadata"      TEXT,
        "tags"          TEXT,
        "input"         TEXT,
        "output"        TEXT,
        "createdAt"     TEXT,
        "start"         TEXT,
        "end"           TEXT,
        "generation"    TEXT,
        "showInput"     TEXT,
        "defaultOpen"   BOOLEAN,
        "autoCollapse"  BOOLEAN,
        "language"      TEXT,
        "indent"        INTEGER,
        "icon"          TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS elements (
        "id"           TEXT PRIMARY KEY,
        "threadId"     TEXT,
        "type"         TEXT,
        "url"          TEXT,
        "chainlitKey"  TEXT,
        "name"         TEXT NOT NULL,
        "display"      TEXT,
        "objectKey"    TEXT,
        "size"         TEXT,
        "page"         INTEGER,
        "language"     TEXT,
        "forId"        TEXT,
        "mime"         TEXT,
        "props"        TEXT,
        "autoPlay"     BOOLEAN,
        "playerConfig" TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS feedbacks (
        "id"       TEXT PRIMARY KEY,
        "forId"    TEXT NOT NULL,
        "threadId" TEXT,
        "value"    INTEGER NOT NULL,
        "comment"  TEXT
    )""",
    'CREATE INDEX IF NOT EXISTS steps_thread_idx ON steps ("threadId")',
    'CREATE INDEX IF NOT EXISTS threads_user_idx ON threads ("userId")',
]

TABLES = ("users", "threads", "steps", "elements", "feedbacks")


def init_db(path: Path | str = DB_PATH) -> Path:
    """Create the folder and tables if missing (idempotent). Returns the db path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        for stmt in SCHEMA:
            con.execute(stmt)
    return path


def conninfo(path: Path | str = DB_PATH) -> str:
    """SQLAlchemy async URL for the data layer."""
    return f"sqlite+aiosqlite:///{Path(path).resolve()}"


def _meta(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v:
        try:
            out = json.loads(v)
            return out if isinstance(out, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


FOOTER_SEP = "\n\n---\n"


def turns_from_steps(steps: list[dict]) -> list[dict]:
    """Rebuild the agent's plain-text conversation (user / assistant turns) from a thread's
    persisted steps. Assistant answers carry the raw answer in metadata["answer"]; older rows
    fall back to the message text minus the model/usage footer. Notices and tool steps are skipped."""
    turns: list[dict] = []
    for st in sorted(steps, key=lambda x: x.get("createdAt") or ""):
        kind = st.get("type")
        text = st.get("output") or ""
        if kind == "user_message":
            if text.strip():
                turns.append({"role": "user", "content": text})
        elif kind == "assistant_message":
            meta = _meta(st.get("metadata"))
            if "answer" in meta:
                answer = meta["answer"]
            elif meta.get("kind") == "notice" or st.get("name") == "system":
                continue
            else:
                answer = text.rsplit(FOOTER_SEP, 1)[0] if FOOTER_SEP in text else text
            if answer.strip():
                turns.append({"role": "assistant", "content": answer})
    return turns
