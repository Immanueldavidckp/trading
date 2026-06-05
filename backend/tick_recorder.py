"""
Shoonya WebSocket tick recorder.

Logs in via ShoonyaClient, opens the NorenWSTP WebSocket, subscribes to
configured symbols, and writes every tick (with 5-level bid/ask depth)
to a SQLite database at backend/local_data/ticks.db.

Designed to run as a background thread inside FastAPI.
"""

import os
import json
import time
import sqlite3
import threading
from datetime import datetime
from typing import Optional, List, Dict

import websocket  # websocket-client

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_data", "ticks.db")
WS_URL = "wss://api.shoonya.com/NorenWSTP/"


def _ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        conn.commit()

# SQL schema. One row per tick. Indexed by (symbol, ts) for fast range queries.
SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT    NOT NULL,
    feed_time   INTEGER,
    exch        TEXT,
    token       TEXT,
    tsym        TEXT,
    lp          REAL,
    v           INTEGER,
    oi          INTEGER,
    bp1 REAL, bq1 INTEGER, bp2 REAL, bq2 INTEGER, bp3 REAL, bq3 INTEGER,
    bp4 REAL, bq4 INTEGER, bp5 REAL, bq5 INTEGER,
    sp1 REAL, sq1 INTEGER, sp2 REAL, sq2 INTEGER, sp3 REAL, sq3 INTEGER,
    sp4 REAL, sq4 INTEGER, sp5 REAL, sq5 INTEGER,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON ticks(tsym, received_at);
CREATE INDEX IF NOT EXISTS idx_ticks_token_ts  ON ticks(token,  received_at);
"""


def _to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except Exception:
        return None


def _to_int(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except Exception:
        return None


class TickRecorder:
    def __init__(self, shoonya_client, symbols: List[Dict[str, str]]):
        """
        symbols: list of {"exch": "NSE", "token": "2885", "tsym": "RELIANCE-EQ"}
        """
        self.client = shoonya_client
        self.symbols = symbols
        self.ws: Optional[websocket.WebSocketApp] = None
        self.thread: Optional[threading.Thread] = None
        self.connected = False
        self.running = False
        self.last_error: Optional[str] = None
        self.tick_count = 0
        self.started_at: Optional[str] = None

        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._init_db()
        self._db_lock = threading.Lock()

    def _init_db(self):
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    # ---- WebSocket callbacks ----
    def _on_open(self, ws):
        # Shoonya auth message
        auth = {
            "t": "c",
            "uid": self.client.config.get("user_id"),
            "actid": self.client.actid or self.client.config.get("user_id"),
            "susertoken": self.client.session_token,
            "source": "API",
        }
        ws.send(json.dumps(auth))

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except Exception:
            return

        t = data.get("t")

        # Connect ack -> now subscribe to symbols
        if t == "ck":
            if data.get("s") == "OK":
                self.connected = True
                scrips = "#".join(f"{s['exch']}|{s['token']}" for s in self.symbols)
                sub = {"t": "d", "k": scrips}  # 'd' = touchline + depth subscribe
                ws.send(json.dumps(sub))
            else:
                self.last_error = f"Connect ack failed: {data}"
            return

        # Tick message (df = depth feed, tf = touchline feed, both contain bid/ask in 'd')
        if t in ("df", "tf", "dk", "tk"):
            self._save_tick(data)

    def _on_error(self, ws, error):
        self.last_error = str(error)

    def _on_close(self, ws, code, msg):
        self.connected = False

    # ---- Persist tick ----
    def _save_tick(self, d: dict):
        row = (
            datetime.now().isoformat(timespec="milliseconds"),
            _to_int(d.get("ft")),
            d.get("e"),
            d.get("tk"),
            d.get("ts"),
            _to_float(d.get("lp")),
            _to_int(d.get("v")),
            _to_int(d.get("oi")),
            _to_float(d.get("bp1")), _to_int(d.get("bq1")),
            _to_float(d.get("bp2")), _to_int(d.get("bq2")),
            _to_float(d.get("bp3")), _to_int(d.get("bq3")),
            _to_float(d.get("bp4")), _to_int(d.get("bq4")),
            _to_float(d.get("bp5")), _to_int(d.get("bq5")),
            _to_float(d.get("sp1")), _to_int(d.get("sq1")),
            _to_float(d.get("sp2")), _to_int(d.get("sq2")),
            _to_float(d.get("sp3")), _to_int(d.get("sq3")),
            _to_float(d.get("sp4")), _to_int(d.get("sq4")),
            _to_float(d.get("sp5")), _to_int(d.get("sq5")),
            json.dumps(d),
        )
        with self._db_lock:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    """INSERT INTO ticks (
                        received_at, feed_time, exch, token, tsym, lp, v, oi,
                        bp1,bq1,bp2,bq2,bp3,bq3,bp4,bq4,bp5,bq5,
                        sp1,sq1,sp2,sq2,sp3,sq3,sp4,sq4,sp5,sq5,
                        raw) VALUES (?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?, ?)""",
                    row,
                )
                conn.commit()
        self.tick_count += 1

    # ---- Lifecycle ----
    def start(self) -> dict:
        if self.running:
            return {"ok": False, "error": "Recorder already running"}

        # Ensure we have a Shoonya session
        if self.client.mock_mode:
            return {"ok": False, "error": "Cannot record ticks in mock mode. Set MOCK_MODE=false and ensure Shoonya is reachable."}
        if not self.client.session_token:
            login_res = self.client.login()
            if login_res.get("stat") != "Ok":
                return {"ok": False, "error": f"Shoonya login failed: {login_res.get('emsg')}"}

        self.last_error = None
        self.tick_count = 0
        self.started_at = datetime.now().isoformat(timespec="seconds")

        def _run():
            self.ws = websocket.WebSocketApp(
                WS_URL,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self.running = True
            try:
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            finally:
                self.running = False
                self.connected = False

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()

        # Give it a moment to connect/auth
        time.sleep(2.0)
        return {
            "ok": True,
            "connected": self.connected,
            "subscribed": self.symbols,
            "db_path": DB_PATH,
            "last_error": self.last_error,
        }

    def stop(self) -> dict:
        if not self.running:
            return {"ok": False, "error": "Recorder is not running"}
        try:
            if self.ws:
                self.ws.close()
        except Exception as e:
            self.last_error = str(e)
        return {"ok": True, "tick_count": self.tick_count, "stopped_at": datetime.now().isoformat(timespec="seconds")}

    def status(self) -> dict:
        return {
            "running": self.running,
            "connected": self.connected,
            "tick_count": self.tick_count,
            "started_at": self.started_at,
            "symbols": self.symbols,
            "last_error": self.last_error,
            "db_path": DB_PATH,
        }

    # ---- Query ----
    @staticmethod
    def query_ticks(tsym: Optional[str] = None, limit: int = 100, since: Optional[str] = None):
        _ensure_db()
        sql = "SELECT received_at, feed_time, tsym, lp, v, bp1, bq1, sp1, sq1, bp2, bq2, sp2, sq2 FROM ticks"
        clauses = []
        params: list = []
        if tsym:
            clauses.append("tsym = ?")
            params.append(tsym)
        if since:
            clauses.append("received_at >= ?")
            params.append(since)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))

        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def stats():
        _ensure_db()
        with sqlite3.connect(DB_PATH) as conn:
            total = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
            by_sym = conn.execute(
                "SELECT tsym, COUNT(*) as n, MIN(received_at) as first, MAX(received_at) as last FROM ticks GROUP BY tsym"
            ).fetchall()
            return {
                "total_ticks": total,
                "by_symbol": [
                    {"tsym": r[0], "count": r[1], "first": r[2], "last": r[3]} for r in by_sym
                ],
                "db_path": DB_PATH,
            }
