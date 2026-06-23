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
    "1d":  "day",
    "1w":  "week",
    "1mo": "month",
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
                seg = item.get("segment", "")
                itype = item.get("instrument_type", "")
                # NSE cash-market equities: segment NSE_EQ, type EQ (normal) or BE (trade-to-trade)
                if sym and key and seg == "NSE_EQ" and itype in ("EQ", "BE"):
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
        today_only: bool = False,
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

        # Historical candles stop at the previous trading day; today's forming
        # candles come from the separate intraday endpoint. We fetch both and
        # merge so the chart shows continuous data through the current minute.
        # today_only=True skips the (heavy) history fetch — used for live polling.
        if interval in _DERIVED:
            base = []
            if not today_only:
                raw = self._fetch_raw(key, "1minute", from_date, to_dt)
                if not raw["ok"]:
                    return raw
                base = raw["candles"]
            base = self._dedupe(base + self._fetch_intraday(key, "1minute"))
            candles = self._aggregate(base, _DERIVED[interval])
        elif interval in _NATIVE:
            ui = _NATIVE[interval]
            candles = []
            if not today_only:
                raw = self._fetch_raw(key, ui, from_date, to_dt)
                if not raw["ok"]:
                    return raw
                candles = raw["candles"]
            if ui in ("1minute", "30minute"):
                candles = self._dedupe(candles + self._fetch_intraday(key, ui))
            elif ui == "day":
                # synthesise today's still-forming daily candle from 1m intraday
                today1m = self._fetch_intraday(key, "1minute")
                if today1m:
                    candles = self._dedupe(candles + [self._daily_from_1m(today1m)])
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
        """Call Upstox historical-candle endpoint.

        Upstox v2 caps the 1minute interval at ~30 days per request, so we
        split longer ranges into <=28-day windows and concatenate. Daily/
        weekly/monthly have no such limit and run in a single request.
        """
        k = urllib.parse.quote(instrument_key, safe="")

        # Build the list of (from, to) windows to request.
        if upstox_interval == "1minute":
            windows = []
            start = datetime.fromisoformat(from_date)
            end   = datetime.fromisoformat(to_date)
            cur   = start
            while cur <= end:
                win_end = min(cur + timedelta(days=27), end)
                windows.append((cur.strftime("%Y-%m-%d"), win_end.strftime("%Y-%m-%d")))
                cur = win_end + timedelta(days=1)
        else:
            windows = [(from_date, to_date)]

        all_candles: list = []
        try:
            for w_from, w_to in windows:
                url = f"{BASE}/historical-candle/{k}/{upstox_interval}/{w_to}/{w_from}"
                r = requests.get(url, headers=self._headers(), timeout=30)
                d = r.json()
                if d.get("status") != "success":
                    # Skip empty windows (e.g. before listing date) but fail hard otherwise.
                    msg = str(d.get("errors", d))
                    if "Invalid date range" in msg or "No data" in msg:
                        continue
                    return {"ok": False, "error": str(d)}
                all_candles.extend(d["data"]["candles"])
            # Upstox returns newest-first; merge windows then sort oldest-first by timestamp.
            all_candles.sort(key=lambda c: c[0])
            return {"ok": True, "candles": all_candles}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _fetch_intraday(self, instrument_key: str, upstox_interval: str) -> list:
        """Today's (still-forming) candles. Upstox intraday supports only
        1minute and 30minute; other intervals return []. Never raises."""
        if upstox_interval not in ("1minute", "30minute"):
            return []
        k = urllib.parse.quote(instrument_key, safe="")
        url = f"{BASE}/historical-candle/intraday/{k}/{upstox_interval}"
        try:
            r = requests.get(url, headers=self._headers(), timeout=20)
            d = r.json()
            if d.get("status") == "success":
                cs = list(d["data"]["candles"])
                cs.sort(key=lambda c: c[0])
                return cs
        except Exception:
            pass
        return []

    @staticmethod
    def _dedupe(candles: list) -> list:
        """Merge candle lists, keyed by timestamp string. Later entries win
        (intraday is appended after history, so today's data overrides), and
        the result is sorted oldest-first."""
        by: dict = {}
        for c in candles:
            by[c[0]] = c
        return [by[t] for t in sorted(by)]

    @staticmethod
    def _daily_from_1m(cs_1m: list) -> list:
        """Build today's single daily candle from today's 1m intraday candles."""
        o = cs_1m[0][1]
        h = max(c[2] for c in cs_1m)
        l = min(c[3] for c in cs_1m)
        c_ = cs_1m[-1][4]
        v = sum((c[5] or 0) for c in cs_1m)
        ts = cs_1m[0][0][:10] + "T00:00:00+05:30"   # match historical daily ts
        return [ts, o, h, l, c_, v, 0]

    # IST is UTC+5:30. Aligning bucket boundaries to UTC produces ugly off-clock
    # candles (a "1h" candle stamped 08:30 IST rather than 09:00 IST), which is
    # what you'd never see on a broker chart. Shift by this offset before
    # flooring and shift back, so buckets snap to IST wall-clock boundaries.
    _IST_OFFSET = 5 * 3600 + 30 * 60

    def _aggregate(self, candles_1m: list, minutes: int) -> list:
        """
        Aggregate raw 1-minute Upstox candles into N-minute candles, IST-aligned.
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
            b = ((ts + self._IST_OFFSET) // bucket_secs) * bucket_secs - self._IST_OFFSET
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
                f"INSERT INTO candles "
                f"(tsym,instrument_key,`interval`,ts,open,high,low,close,volume,oi,fetched_at) "
                f"VALUES ({','.join([PH]*11)}) "
                f"ON DUPLICATE KEY UPDATE open=VALUES(open),high=VALUES(high),"
                f"low=VALUES(low),close=VALUES(close),volume=VALUES(volume),"
                f"fetched_at=VALUES(fetched_at)"
            )
        else:
            sql = (
                f"INSERT INTO candles "
                f"(tsym,instrument_key,interval,ts,open,high,low,close,volume,oi,fetched_at) "
                f"VALUES ({','.join([PH]*11)}) "
                f"ON CONFLICT(tsym,interval,ts) DO UPDATE SET open=excluded.open,"
                f"high=excluded.high,low=excluded.low,close=excluded.close,"
                f"volume=excluded.volume,fetched_at=excluded.fetched_at"
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
