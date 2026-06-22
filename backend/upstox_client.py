"""
Upstox API v2 client — historical candle data for all NSE stocks.

Setup (one-time):
  1. Go to https://developer.upstox.com/developer/apps
  2. Click "Create New App"
     - App name: anything (e.g. "TradingApp")
     - Redirect URL: http://127.0.0.1:8000/api/upstox/callback
  3. Copy the API Key and API Secret shown
  4. Add to backend/.env:
       UPSTOX_API_KEY=your_api_key_here
       UPSTOX_API_SECRET=your_api_secret_here
  5. Start your backend, open browser:
       http://127.0.0.1:8000/api/upstox/login_url
     Click the link → login with Upstox → token saved automatically
  6. Done! Fetch candles via /api/upstox/candles?tsym=RELIANCE&interval=1d

Notes:
  - Access token expires every day. Re-visit /api/upstox/login_url next morning.
  - Upstox native intervals: 1m, 30m, 1d, 1w, 1mo
  - Derived intervals (aggregated from 1m): 5m, 15m, 1h, 4h
"""

import os
import json
import gzip
import threading
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

import requests
import db as _db

UPSTOX_API_KEY    = os.environ.get("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET = os.environ.get("UPSTOX_API_SECRET", "")
UPSTOX_REDIRECT   = os.environ.get("UPSTOX_REDIRECT_URI", "http://127.0.0.1:8000/api/upstox/callback")

BASE       = "https://api.upstox.com/v2"
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "local_data", "upstox_token.json")
INST_FILE  = os.path.join(os.path.dirname(__file__), "local_data", "upstox_instruments.json")

# Upstox native interval strings
_NATIVE = {
    "1m":  "1minute",
    "30m": "30minute",
    "1d":  "1day",
    "1w":  "1week",
    "1mo": "1month",
}

# Derived intervals — fetched as 1m candles then aggregated
_DERIVED = {
    "5m":  5,
    "15m": 15,
    "1h":  60,
    "4h":  240,
}

ALL_INTERVALS = list(_NATIVE.keys()) + list(_DERIVED.keys())

# ── DB schema ────────────────────────────────────────────────────────────────

_CANDLES_SQLITE = """
CREATE TABLE IF NOT EXISTS candles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tsym           TEXT    NOT NULL,
    instrument_key TEXT,
    interval       TEXT    NOT NULL,
    ts             INTEGER NOT NULL,
    open           REAL,
    high           REAL,
    low            REAL,
    close          REAL,
    volume         INTEGER,
    oi             INTEGER,
    source         TEXT DEFAULT 'upstox',
    fetched_at     TEXT,
    UNIQUE(tsym, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_sym ON candles(tsym, interval, ts);
"""

_CANDLES_MYSQL = """
CREATE TABLE IF NOT EXISTS candles (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    tsym           VARCHAR(50) NOT NULL,
    instrument_key VARCHAR(80),
    `interval`     VARCHAR(10) NOT NULL,
    ts             BIGINT NOT NULL,
    open           DOUBLE,
    high           DOUBLE,
    low            DOUBLE,
    close          DOUBLE,
    volume         BIGINT,
    oi             BIGINT,
    source         VARCHAR(20) DEFAULT 'upstox',
    fetched_at     VARCHAR(30),
    UNIQUE KEY uq_candle (tsym, `interval`, ts),
    INDEX idx_candles_sym (tsym, `interval`, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def ensure_candles_table():
    conn = _db.connect()
    cur = conn.cursor()
    try:
        schema = _CANDLES_MYSQL if _db.USE_MYSQL else _CANDLES_SQLITE
        for stmt in [s.strip() for s in schema.split(";") if s.strip()]:
            cur.execute(stmt)
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ── Client ───────────────────────────────────────────────────────────────────

class UpstoxClient:

    def __init__(self):
        self.api_key    = os.environ.get("UPSTOX_API_KEY", UPSTOX_API_KEY)
        self.api_secret = os.environ.get("UPSTOX_API_SECRET", UPSTOX_API_SECRET)
        self.redirect   = os.environ.get("UPSTOX_REDIRECT_URI", UPSTOX_REDIRECT)
        self.access_token: Optional[str] = None
        self._inst: dict = {}       # "RELIANCE" / "RELIANCE-EQ" -> instrument_key
        self._inst_lock = threading.Lock()
        ensure_candles_table()
        self._load_token()
        self._load_instruments()

    # ── credentials ──────────────────────────────────────────────────────────

    def has_creds(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _load_token(self):
        """Load today's access token from disk (tokens expire daily)."""
        try:
            with open(TOKEN_FILE) as f:
                d = json.load(f)
            if d.get("date") == datetime.now().strftime("%Y-%m-%d"):
                self.access_token = d.get("access_token")
        except Exception:
            pass

    def _save_token(self, token: str):
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            json.dump({
                "access_token": token,
                "date": datetime.now().strftime("%Y-%m-%d"),
            }, f)
        self.access_token = token

    def login_url(self) -> str:
        """Return the URL the user must open in their browser to log in."""
        return (
            "https://api.upstox.com/v2/login/authorization/dialog"
            f"?client_id={urllib.parse.quote(self.api_key)}"
            f"&redirect_uri={urllib.parse.quote(self.redirect)}"
            "&response_type=code&state=upstox"
        )

    def exchange_code(self, code: str) -> dict:
        """Exchange the OAuth authorization code for an access token."""
        try:
            r = requests.post(
                f"{BASE}/login/authorization/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "code":          code,
                    "client_id":     self.api_key,
                    "client_secret": self.api_secret,
                    "redirect_uri":  self.redirect,
                    "grant_type":    "authorization_code",
                },
                timeout=15,
            )
            d = r.json()
            token = d.get("access_token")
            if token:
                self._save_token(token)
                return {"ok": True, "message": "Logged in to Upstox successfully"}
            return {"ok": False, "error": str(d)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept":        "application/json",
        }

    # ── instruments master ────────────────────────────────────────────────────

    def _load_instruments(self):
        """Load cached instrument map from disk; download if missing."""
        if os.path.exists(INST_FILE):
            try:
                with open(INST_FILE) as f:
                    with self._inst_lock:
                        self._inst = json.load(f)
                return
            except Exception:
                pass
        self.refresh_instruments()

    def refresh_instruments(self) -> dict:
        """Download Upstox NSE instruments master and rebuild the symbol→key map."""
        url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
        try:
            r = requests.get(url, timeout=30)
            data = json.loads(gzip.decompress(r.content))
            inst = {}
            for item in data:
                sym = (item.get("tradingsymbol") or item.get("trading_symbol", "")).strip().upper()
                key = item.get("instrument_key", "")
                itype = item.get("instrument_type", "")
                if sym and key and itype == "EQUITY":
                    inst[sym]           = key   # e.g. "RELIANCE"
                    inst[sym + "-EQ"]   = key   # e.g. "RELIANCE-EQ"  (matches watchlist tsym)
            with self._inst_lock:
                self._inst = inst
            os.makedirs(os.path.dirname(INST_FILE), exist_ok=True)
            with open(INST_FILE, "w") as f:
                json.dump(inst, f)
            return {"ok": True, "instruments": len(inst) // 2}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def instrument_key(self, tsym: str) -> Optional[str]:
        """Return Upstox instrument key for a symbol like 'RELIANCE' or 'RELIANCE-EQ'."""
        with self._inst_lock:
            return self._inst.get(tsym.strip().upper())

    # ── fetch candles ─────────────────────────────────────────────────────────

    def fetch_candles(
        self,
        tsym: str,
        interval: str = "1d",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        store: bool = True,
    ) -> dict:
        """
        Fetch OHLCV candles from Upstox and (optionally) store in SQLite.

        tsym:      Stock symbol e.g. 'RELIANCE', 'TCS', 'INFY'
        interval:  '1m','5m','15m','1h','4h','30m','1d','1w','1mo'
        from_date: 'YYYY-MM-DD'  (default: 100 days ago for intraday, 3 years for daily)
        to_date:   'YYYY-MM-DD'  (default: today)
        """
        if not self.access_token:
            return {"ok": False, "error": "Not logged in. Visit /api/upstox/login_url first."}
        if not self.has_creds():
            return {"ok": False, "error": "UPSTOX_API_KEY / UPSTOX_API_SECRET not set in .env"}

        key = self.instrument_key(tsym)
        if not key:
            return {
                "ok": False,
                "error": f"Symbol '{tsym}' not found. Call /api/upstox/refresh_instruments first.",
            }

        to_dt   = to_date   or datetime.now().strftime("%Y-%m-%d")
        # Sensible default look-back per timeframe
        if not from_date:
            if interval in ("1d", "1w", "1mo"):
                from_date = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
            else:
                from_date = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")

        # Derived intervals need 1m raw data first
        if interval in _DERIVED:
            raw = self._fetch_raw(key, "1minute", from_date, to_dt)
            if not raw["ok"]:
                return raw
            candles = self._aggregate(raw["candles"], _DERIVED[interval])
        elif interval in _NATIVE:
            raw = self._fetch_raw(key, _NATIVE[interval], from_date, to_dt)
            if not raw["ok"]:
                return raw
            candles = raw["candles"]
        else:
            return {"ok": False, "error": f"Unknown interval '{interval}'. Choose from: {ALL_INTERVALS}"}

        stored = 0
        if store:
            stored = self._store(tsym.upper(), key, interval, candles)

        return {
            "ok":       True,
            "tsym":     tsym.upper(),
            "interval": interval,
            "fetched":  len(candles),
            "stored":   stored,
            "from":     from_date,
            "to":       to_dt,
        }

    def _fetch_raw(self, instrument_key: str, upstox_interval: str,
                   from_date: str, to_date: str) -> dict:
        """Call Upstox historical-candle endpoint."""
        k   = urllib.parse.quote(instrument_key, safe="")
        url = f"{BASE}/historical-candle/{k}/{upstox_interval}/{to_date}/{from_date}"
        try:
            r = requests.get(url, headers=self._headers(), timeout=20)
            d = r.json()
            if d.get("status") == "success":
                return {"ok": True, "candles": d["data"]["candles"]}
            return {"ok": False, "error": str(d)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _aggregate(self, candles_1m: list, minutes: int) -> list:
        """
        Aggregate raw 1-minute Upstox candles into N-minute candles.
        Upstox candle format: [timestamp_str, open, high, low, close, volume, oi]
        """
        buckets: dict = {}
        order:   list = []
        bucket_secs = minutes * 60

        for c in candles_1m:
            try:
                ts = int(datetime.fromisoformat(str(c[0])).timestamp())
            except Exception:
                continue
            b = (ts // bucket_secs) * bucket_secs
            if b not in buckets:
                buckets[b] = {
                    "ts": b, "o": c[1], "h": c[2], "l": c[3],
                    "c": c[4], "v": c[5] or 0, "oi": c[6] if len(c) > 6 else 0,
                }
                order.append(b)
            else:
                x = buckets[b]
                if c[2] > x["h"]: x["h"] = c[2]
                if c[3] < x["l"]: x["l"] = c[3]
                x["c"] = c[4]
                x["v"] = (x["v"] or 0) + (c[5] or 0)

        # Return in same format as raw Upstox candles [ts_str, o, h, l, c, v, oi]
        result = []
        for b in order:
            x = buckets[b]
            ts_str = datetime.fromtimestamp(x["ts"]).isoformat()
            result.append([ts_str, x["o"], x["h"], x["l"], x["c"], x["v"], x["oi"]])
        return result

    def _store(self, tsym: str, instrument_key: str, interval: str, candles: list) -> int:
        """Upsert candles into the candles table. Returns number of new rows inserted."""
        now = datetime.now().isoformat(timespec="seconds")
        PH  = _db.PLACE
        if _db.USE_MYSQL:
            sql = (
                f"INSERT IGNORE INTO candles "
                f"(tsym,instrument_key,`interval`,ts,open,high,low,close,volume,oi,fetched_at) "
                f"VALUES ({','.join([PH]*11)})"
            )
        else:
            sql = (
                f"INSERT OR IGNORE INTO candles "
                f"(tsym,instrument_key,interval,ts,open,high,low,close,volume,oi,fetched_at) "
                f"VALUES ({','.join([PH]*11)})"
            )

        conn = _db.connect()
        inserted = 0
        try:
            cur = conn.cursor()
            for c in candles:
                try:
                    ts = int(datetime.fromisoformat(str(c[0])).timestamp())
                    cur.execute(sql, [
                        tsym, instrument_key, interval, ts,
                        c[1], c[2], c[3], c[4],
                        c[5] if len(c) > 5 else 0,
                        c[6] if len(c) > 6 else 0,
                        now,
                    ])
                    inserted += cur.rowcount
                except Exception:
                    pass
            conn.commit()
            cur.close()
        finally:
            conn.close()
        return inserted

    # ── query stored candles ──────────────────────────────────────────────────

    @staticmethod
    def query(tsym: str, interval: str = "1d", limit: int = 500,
              from_ts: Optional[int] = None) -> list:
        """Return stored candles oldest-first as list of dicts with ms timestamps."""
        ensure_candles_table()
        PH  = _db.PLACE
        iv_col = "`interval`" if _db.USE_MYSQL else "interval"
        sql = (
            f"SELECT ts, open, high, low, close, volume, oi "
            f"FROM candles WHERE tsym={PH} AND {iv_col}={PH}"
        )
        params: list = [tsym.upper(), interval]
        if from_ts:
            sql += f" AND ts>={PH}"
            params.append(from_ts)
        sql += f" ORDER BY ts DESC LIMIT {PH}"
        params.append(limit)

        conn = _db.connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = [
                {"ts": r[0] * 1000, "o": r[1], "h": r[2],
                 "l": r[3], "c": r[4], "v": r[5], "oi": r[6]}
                for r in cur.fetchall()
            ]
            cur.close()
            return list(reversed(rows))
        finally:
            conn.close()

    @staticmethod
    def stored_symbols() -> list:
        """List all symbols and intervals currently stored in the candles table."""
        ensure_candles_table()
        iv_col = "`interval`" if _db.USE_MYSQL else "interval"
        sql = (
            f"SELECT tsym, {iv_col}, COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts "
            f"FROM candles GROUP BY tsym, {iv_col} ORDER BY tsym, {iv_col}"
        )
        conn = _db.connect()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = [
                {
                    "tsym": r[0], "interval": r[1], "candles": r[2],
                    "from": datetime.fromtimestamp(r[3]).strftime("%Y-%m-%d") if r[3] else None,
                    "to":   datetime.fromtimestamp(r[4]).strftime("%Y-%m-%d") if r[4] else None,
                }
                for r in cur.fetchall()
            ]
            cur.close()
            return rows
        finally:
            conn.close()

    # ── status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._inst_lock:
            inst_count = len(self._inst) // 2
        return {
            "configured":         self.has_creds(),
            "logged_in":          bool(self.access_token),
            "api_key":            (self.api_key[:6] + "***") if self.api_key else "(not set)",
            "instruments_cached": inst_count,
            "redirect_uri":       self.redirect,
        }
