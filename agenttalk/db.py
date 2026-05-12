"""SQLite-backed storage for agent registry and message mailboxes.

DB lives at ~/.agenttalk/db.sqlite so it is global across worktrees.
"""
import os
import pathlib
import sqlite3

DB_DIR = pathlib.Path(os.environ.get("AGENTTALK_HOME") or os.path.expanduser("~/.agenttalk"))
DB_PATH = DB_DIR / "db.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name          TEXT PRIMARY KEY,
    purpose       TEXT NOT NULL,
    session_id    TEXT,
    registered_at INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL,
    retired_at    INTEGER
);
CREATE INDEX IF NOT EXISTS agents_session ON agents(session_id);
CREATE INDEX IF NOT EXISTS agents_retired ON agents(retired_at);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent  TEXT NOT NULL,
    to_agent    TEXT NOT NULL,
    body        TEXT NOT NULL,
    sent_at     INTEGER NOT NULL,
    read_at     INTEGER,
    in_reply_to INTEGER
);
CREATE INDEX IF NOT EXISTS messages_to_unread ON messages(to_agent, read_at);
"""


def _migrate(conn):
    """Apply additive migrations for DBs created by older versions."""
    msg_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "in_reply_to" not in msg_cols:
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN in_reply_to INTEGER")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

    agent_cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
    if "retired_at" not in agent_cols:
        try:
            conn.execute("ALTER TABLE agents ADD COLUMN retired_at INTEGER")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def connect():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS messages_in_reply_to ON messages(in_reply_to)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS agents_retired ON agents(retired_at)"
    )
    return conn
