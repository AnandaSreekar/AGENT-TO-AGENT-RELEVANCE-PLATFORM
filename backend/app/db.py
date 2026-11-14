import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_name TEXT NOT NULL UNIQUE,
  industry TEXT NOT NULL,
  interest_manifesto TEXT NOT NULL,
  api_key TEXT NOT NULL UNIQUE,              -- SHA-256 hash of the key; plaintext is never stored
  webhook_url TEXT,
  webhook_secret TEXT,
  trust_score REAL NOT NULL DEFAULT 0.7,
  active INTEGER NOT NULL DEFAULT 1,
  autonomy_mode TEXT NOT NULL DEFAULT 'auto',
  blocked_industries TEXT NOT NULL DEFAULT '[]',
  is_system INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  author_id INTEGER NOT NULL REFERENCES agents(id),
  content TEXT NOT NULL,
  topics TEXT NOT NULL DEFAULT '[]',
  funnel TEXT,
  engine TEXT,
  routed INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER NOT NULL REFERENCES posts(id),
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PUBLISHED',   -- PUBLISHED | PENDING | REJECTED
  source TEXT NOT NULL DEFAULT 'manual',      -- manual | llm | rule-based
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS subscriptions (
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  topic TEXT NOT NULL,
  PRIMARY KEY (agent_id, topic)
);
CREATE TABLE IF NOT EXISTS routing_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER NOT NULL REFERENCES posts(id),
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  rule_pass INTEGER NOT NULL,
  semantic_score REAL,
  llm_score REAL,
  base_score REAL,
  feedback_multiplier REAL NOT NULL DEFAULT 1.0,
  final_score REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL,                       -- NOTIFIED | ENGAGED | IGNORED | REJECTED
  reason TEXT NOT NULL,
  engine TEXT,
  judged INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (post_id, agent_id)
);
CREATE TABLE IF NOT EXISTS topic_stats (
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  topic TEXT NOT NULL,
  notified INTEGER NOT NULL DEFAULT 0,
  engaged INTEGER NOT NULL DEFAULT 0,
  ignored INTEGER NOT NULL DEFAULT 0,
  streak INTEGER NOT NULL DEFAULT 0,          -- consecutive-ignore pressure (recovers on engagement)
  simulated INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent_id, topic)
);
CREATE TABLE IF NOT EXISTS security_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  agent_id INTEGER,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER, agent_id INTEGER,
  status TEXT NOT NULL, attempts INTEGER NOT NULL, error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_keys (
  agent_id INTEGER PRIMARY KEY REFERENCES agents(id),
  api_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_routing_post ON routing_logs(post_id);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    path = settings.database_path
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as c:
        c.executescript(SCHEMA)


def jl(v, default=None):
    if v is None:
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def get_threshold(conn) -> float:
    row = conn.execute("SELECT value FROM settings WHERE key='threshold'").fetchone()
    return float(row["value"]) if row else settings.default_threshold


def set_threshold(conn, value: float) -> None:
    conn.execute("INSERT INTO settings(key,value) VALUES('threshold',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))
