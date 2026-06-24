"""
Upstox real-time market feed.

Polls the Upstox **full market-quote** endpoint (/v2/market-quote/quotes) for
every symbol on the watchlist, ~once a second, and persists:

  * price_changes  — LTP / bid / ask / OHLC / volume / change (reuses the
                     existing table so the /ws/prices socket, KPI cards and
                     tick table keep working — now sourced from Upstox).
  * market_depth   — full 5-level order-book snapshot (bid/ask price+qty+orders)
                     plus volume, OI and total buy/sell quantity, for analysis.

This replaces the Yahoo poller. One REST call returns up to 500 instruments,
including the 5-level depth, so a 1-second poll gives near-real-time LTP +
order book without the protobuf websocket. (A true tick-by-tick websocket feed
can be layered on later for sub-second granularity.)
"""

import os
import json
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Callable, List, Optional

import requests
import db as _db

BASE = "https://api.upstox.com/v2"
WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "local_data", "watchlist.json")

# ── market_depth table ──────────────────────────────────────────────────────

_DEPTH_SQLITE = """
CREATE TABLE IF NOT EXISTS market_depth (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at    TEXT NOT NULL,
    tsym           TEXT,
    instrument_key TEXT,
    ltp            REAL,
    volume         INTEGER,
    oi             INTEGER,
    atp            REAL,
    total_buy_qty  INTEGER,
    total_sell_qty INTEGER,
    buy_depth      TEXT,
    sell_depth     TEXT
);
CREATE INDEX IF NOT EXISTS idx_md_sym_ts ON market_depth(tsym, received_at);
"""

_DEPTH_MYSQL = """
CREATE TABLE IF NOT EXISTS market_depth (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    received_at    VARCHAR(40) NOT NULL,
    tsym           VARCHAR(50),
    instrument_key VARCHAR(80),
    ltp            DOUBLE,
    volume         BIGINT,
    oi             BIGINT,
    atp            DOUBLE,
    total_buy_qty  BIGINT,
    total_sell_qty BIGINT,
    buy_depth      TEXT,
    sell_depth     TEXT,
    INDEX idx_md_sym_ts (tsym, received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def ensure_depth_table():
    conn = _db.connect()
    cur = conn.cursor()
    try:
        schema = _DEPTH_MYSQL if _db.USE_MYSQL else _DEPTH_SQLITE
        for stmt in [s.strip() for s in schema.split(";") if s.strip()]:
            cur.execute(stmt)
        conn.commit()
    finally:
        cur.close()
        conn.close()


def _utc_now_iso() -> str:
    # Stored as UTC-naive ISO (the front-end appends 'Z' and renders IST).
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="milliseconds")


# ── watchlist (replaces the Yahoo-managed list) ─────────────────────────────

def load_watchlist() -> List[dict]:
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    # sensible default
    return [
        {"tsym": "MEESHO", "name": "MEESHO LIMITED"},
        {"tsym": "M&M", "name": "MAHINDRA & MAHINDRA"},
        {"tsym": "OLAELEC", "name": "OLA ELECTRIC"},
        {"tsym": "SUZLON", "name": "SUZLON ENERGY"},
    ]


def save_watchlist(items: List[dict]):
    os.makedirs(os.path.dirname(WATCHLIST_FILE), exist_ok=True)
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(items, f, indent=2)


# ── feed ────────────────────────────────────────────────────────────────────

class UpstoxQuoteFeed:
    """Background poller that turns Upstox full-quote snapshots into rows."""

    def __init__(self, upstox_client, interval_sec: float = 1.0):
        self.ux = upstox_client            # UpstoxClient: token + instrument map
        self.interval = max(0.5, interval_sec)
        self.latest: dict = {}             # tsym -> latest quote dict (in-memory)
        self._prev: dict = {}              # tsym -> (lp,bid,ask) for change-only writes
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._watch = load_watchlist()
        ensure_depth_table()

    # -- lifecycle --
    def start(self) -> dict:
        if self._thread and self._thread.is_alive():
            return {"ok": True, "already": True}
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return {"ok": True, "symbols": [w["tsym"] for w in self._watch]}

    def stop(self) -> dict:
        self._stop.set()
        return {"ok": True}

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "interval": self.interval,
            "symbols": [w["tsym"] for w in self._watch],
            "logged_in": bool(getattr(self.ux, "access_token", None)),
        }

    # -- watchlist management --
    def list_symbols(self) -> List[dict]:
        out = []
        for w in self._watch:
            q = self.latest.get(w["tsym"], {})
            out.append({**w, "last_price": q.get("lp")})
        return out

    def add_symbol(self, query: str) -> dict:
        q = (query or "").strip().upper()
        if not q:
            return {"ok": False, "error": "empty query"}
        # resolve against the Upstox instrument master
        key = self.ux.instrument_key(q) or self.ux.instrument_key(q.replace("-EQ", ""))
        name = None
        if not key:
            match = self._search_instruments(q)
            if match:
                q, key, name = match["tsym"], match["key"], match["name"]
        if not key:
            return {"ok": False, "error": f"'{query}' not found in NSE equities"}
        if any(w["tsym"] == q for w in self._watch):
            return {"ok": True, "already": True, "added": {"tsym": q}}
        item = {"tsym": q, "name": name or q}
        self._watch.append(item)
        save_watchlist(self._watch)
        return {"ok": True, "added": item}

    def remove_symbol(self, tsym: str) -> dict:
        tsym = (tsym or "").strip().upper()
        self._watch = [w for w in self._watch if w["tsym"] != tsym]
        save_watchlist(self._watch)
        self.latest.pop(tsym, None)
        return {"ok": True}

    def _search_instruments(self, q: str) -> Optional[dict]:
        """Find an NSE equity by trading symbol or company name substring."""
        with self.ux._inst_lock:
            inst = dict(self.ux._inst)
        # exact trading-symbol hit first
        if q in inst:
            return {"tsym": q, "key": inst[q], "name": q}
        # otherwise scan names from the raw instruments file
        try:
            raw = json.load(open(self.ux.__class__.__dict__.get("INST_FILE", "")))  # noqa
        except Exception:
            raw = None
        return None  # name search handled by instrument_key fallback for now

    # -- poll loop --
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                print(f"[upstox_feed] poll error: {e}")
            self._stop.wait(self.interval)

    def _poll_once(self):
        if not getattr(self.ux, "access_token", None):
            return
        # resolve watchlist tsyms -> instrument keys
        key_to_sym = {}
        for w in self._watch:
            k = self.ux.instrument_key(w["tsym"])
            if k:
                key_to_sym[k] = w["tsym"]
        if not key_to_sym:
            return

        ik_list = list(key_to_sym.keys())
        for i in range(0, len(ik_list), 500):
            batch = ik_list[i:i + 500]
            try:
                r = requests.get(
                    f"{BASE}/market-quote/quotes",
                    headers=self.ux._headers(),
                    params={"instrument_key": ",".join(batch)},
                    timeout=10,
                )
                d = r.json()
            except Exception as e:
                print(f"[upstox_feed] request failed: {e}")
                continue
            if d.get("status") != "success":
                continue
            for _, q in (d.get("data") or {}).items():
                ik = q.get("instrument_token") or q.get("instrument_key")
                tsym = key_to_sym.get(ik) or q.get("symbol")
                if tsym:
                    self._handle(tsym, ik, q)

    def _handle(self, tsym: str, ik: str, q: dict):
        ohlc = q.get("ohlc") or {}
        depth = q.get("depth") or {}
        buy = depth.get("buy") or []
        sell = depth.get("sell") or []

        lp = _num(q.get("last_price"))
        bid = _num(buy[0].get("price")) if buy else None
        ask = _num(sell[0].get("price")) if sell else None
        bid_sz = _int(buy[0].get("quantity")) if buy else None
        ask_sz = _int(sell[0].get("quantity")) if sell else None
        vol = _int(q.get("volume"))
        oi = _int(q.get("oi"))
        atp = _num(q.get("average_price"))
        change = _num(q.get("net_change"))
        # ohlc.close is the LIVE (today) close — equals last_price during market
        # hours, NOT the previous close. Derive the real prev close from change.
        prev_close = (lp - change) if (lp is not None and change is not None) \
            else _num(ohlc.get("close"))
        if change is None and lp is not None and prev_close:
            change = lp - prev_close
        change_pct = (change / prev_close * 100.0) if (change is not None and prev_close) else None

        snap = {
            "tsym": tsym, "lp": lp, "bid": bid, "ask": ask,
            "bid_size": bid_sz, "ask_size": ask_sz, "volume": vol, "oi": oi,
            "day_open": _num(ohlc.get("open")), "day_high": _num(ohlc.get("high")),
            "day_low": _num(ohlc.get("low")), "prev_close": prev_close,
            "change": change, "change_pct": change_pct, "atp": atp,
            "total_buy_qty": _int(q.get("total_buy_quantity")),
            "total_sell_qty": _int(q.get("total_sell_quantity")),
            "buy_depth": buy, "sell_depth": sell,
        }
        self.latest[tsym] = snap

        now = _utc_now_iso()
        # price_changes: write only when LTP/bid/ask move (keeps the table lean)
        sig = (lp, bid, ask)
        if self._prev.get(tsym) != sig:
            self._prev[tsym] = sig
            self._store_price_change(tsym, ik, snap, now)
            self._store_depth(tsym, ik, snap, now)

    # -- persistence --
    def _store_price_change(self, tsym, ik, s, now):
        PH = _db.PLACE
        cols = ("received_at,tsym,yahoo_sym,lp,bid,ask,bid_size,ask_size,"
                "day_open,day_high,day_low,prev_close,"
                + ("`change`" if _db.USE_MYSQL else "change") +
                ",change_pct,volume,source")
        sql = f"INSERT INTO price_changes ({cols}) VALUES ({','.join([PH]*16)})"
        vals = [now, tsym, ik, s["lp"], s["bid"], s["ask"], s["bid_size"], s["ask_size"],
                s["day_open"], s["day_high"], s["day_low"], s["prev_close"],
                s["change"], s["change_pct"], s["volume"], "upstox"]
        self._exec(sql, vals)

    def _store_depth(self, tsym, ik, s, now):
        PH = _db.PLACE
        sql = (f"INSERT INTO market_depth (received_at,tsym,instrument_key,ltp,volume,oi,"
               f"atp,total_buy_qty,total_sell_qty,buy_depth,sell_depth) "
               f"VALUES ({','.join([PH]*11)})")
        vals = [now, tsym, ik, s["lp"], s["volume"], s["oi"], s["atp"],
                s["total_buy_qty"], s["total_sell_qty"],
                json.dumps(s["buy_depth"]), json.dumps(s["sell_depth"])]
        self._exec(sql, vals)

    @staticmethod
    def _exec(sql, vals):
        conn = _db.connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, vals)
            conn.commit()
            cur.close()
        except Exception as e:
            print(f"[upstox_feed] db write failed: {e}")
        finally:
            conn.close()

    # -- depth query for analysis / UI --
    @staticmethod
    def latest_depth(tsym: str) -> Optional[dict]:
        PH = _db.PLACE
        iv = "received_at"
        sql = (f"SELECT received_at,ltp,volume,oi,atp,total_buy_qty,total_sell_qty,"
               f"buy_depth,sell_depth FROM market_depth WHERE tsym={PH} "
               f"ORDER BY {iv} DESC LIMIT 1")
        conn = _db.connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, [tsym.upper()])
            row = cur.fetchone()
            cur.close()
        finally:
            conn.close()
        if not row:
            return None
        return {
            "received_at": row[0], "ltp": row[1], "volume": row[2], "oi": row[3],
            "atp": row[4], "total_buy_qty": row[5], "total_sell_qty": row[6],
            "buy": json.loads(row[7] or "[]"), "sell": json.loads(row[8] or "[]"),
        }


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(float(v)) if v is not None else None
    except (TypeError, ValueError):
        return None
