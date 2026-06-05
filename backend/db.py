"""
Tiny DB abstraction: uses MySQL when DATABASE_URL is set, else SQLite.

DATABASE_URL examples:
    mysql://user:pass@localhost:3306/trading
    (unset)  -> local sqlite at backend/local_data/ticks.db
"""

import os
import sqlite3
from urllib.parse import urlparse

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_MYSQL = DATABASE_URL.startswith("mysql://") or DATABASE_URL.startswith("mysql+pymysql://")

if USE_MYSQL:
    import mysql.connector  # type: ignore
    _u = urlparse(DATABASE_URL.replace("mysql+pymysql://", "mysql://"))
    _CONN_KWARGS = dict(
        host=_u.hostname or "localhost",
        port=_u.port or 3306,
        user=_u.username,
        password=_u.password,
        database=(_u.path or "/").lstrip("/"),
        autocommit=False,
    )

# Placeholder used in parameterised SQL
PLACE = "%s" if USE_MYSQL else "?"

# Local SQLite path (used only when DATABASE_URL is unset)
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_PATH = os.path.join(_BACKEND_DIR, "local_data", "ticks.db")


def connect():
    if USE_MYSQL:
        return mysql.connector.connect(**_CONN_KWARGS)
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    return sqlite3.connect(SQLITE_PATH)


# ============================================================
# Schema — dialect specific
# ============================================================
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT    NOT NULL,
    bar_time    TEXT,
    tsym        TEXT,
    yahoo_sym   TEXT,
    lp          REAL,
    bid         REAL,
    ask         REAL,
    bid_size    INTEGER,
    ask_size    INTEGER,
    day_open    REAL,
    day_high    REAL,
    day_low     REAL,
    prev_close  REAL,
    change      REAL,
    change_pct  REAL,
    volume      INTEGER,
    source      TEXT
);
CREATE INDEX IF NOT EXISTS idx_pc_symbol_ts ON price_changes(tsym, received_at);
"""

MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_changes (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    received_at VARCHAR(40) NOT NULL,
    bar_time    VARCHAR(30),
    tsym        VARCHAR(50),
    yahoo_sym   VARCHAR(50),
    lp          DOUBLE,
    bid         DOUBLE,
    ask         DOUBLE,
    bid_size    BIGINT,
    ask_size    BIGINT,
    day_open    DOUBLE,
    day_high    DOUBLE,
    day_low     DOUBLE,
    prev_close  DOUBLE,
    `change`    DOUBLE,
    change_pct  DOUBLE,
    volume      BIGINT,
    source      VARCHAR(30),
    INDEX idx_pc_symbol_ts (tsym, received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# Columns we may need to add to old DBs (post-launch additions)
_NEW_COLS = [
    ("bid", "DOUBLE"),
    ("ask", "DOUBLE"),
    ("bid_size", "BIGINT"),
    ("ask_size", "BIGINT"),
]


def ensure_schema():
    """Create tables + idempotent ALTERs for both dialects."""
    conn = connect()
    cur = conn.cursor()
    try:
        if USE_MYSQL:
            for stmt in [s.strip() for s in MYSQL_SCHEMA.split(";") if s.strip()]:
                cur.execute(stmt)
            cur.execute("SHOW COLUMNS FROM price_changes")
            existing = {row[0] for row in cur.fetchall()}
            for col, typ in _NEW_COLS:
                if col not in existing:
                    cur.execute(f"ALTER TABLE price_changes ADD COLUMN {col} {typ}")
        else:
            for stmt in [s.strip() for s in SQLITE_SCHEMA.split(";") if s.strip()]:
                cur.execute(stmt)
            cur.execute("PRAGMA table_info(price_changes)")
            existing = {row[1] for row in cur.fetchall()}
            for col, typ in _NEW_COLS:
                if col not in existing:
                    # SQLite types differ — map back
                    sqlite_typ = "REAL" if typ == "DOUBLE" else "INTEGER"
                    cur.execute(f"ALTER TABLE price_changes ADD COLUMN {col} {sqlite_typ}")
        conn.commit()
    finally:
        cur.close()
        conn.close()


def dict_rows(cursor):
    """Convert sqlite/mysql cursor rows to list of dicts."""
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def quote_col(name: str) -> str:
    """Quote a column name in a dialect-safe way (e.g. reserved word `change` in MySQL)."""
    if USE_MYSQL:
        return f"`{name}`"
    return f'"{name}"'
