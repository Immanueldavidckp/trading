"""
Yahoo Finance price poller (free, no API key, no broker account).

Polls Yahoo's quote endpoint every N seconds and records a row in SQLite
EACH TIME the last price actually changes. Also supports backfilling the
full 1-minute intraday series for a trading day.

LIMITATIONS (be honest):
  - Yahoo's finest interval is 1 minute. It does NOT provide bid/ask depth.
  - During live market hours Yahoo data is ~15 minutes delayed.
  - So "every change" here means "every change Yahoo reports", which is
    roughly once per minute, NOT per-tick and NOT microsecond.
For true tick + 5-level depth you need a broker WebSocket (Shoonya/Dhan).

Data is written to the same backend/local_data/ticks.db used by the
Shoonya tick recorder, into a separate `price_changes` table.
"""

import os
import json
import time
import sqlite3
import threading
from datetime import datetime
from typing import Optional

import requests

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_data", "ticks.db")
WATCHLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_data", "watchlist.json")

# Map a Shoonya-style trading symbol to a Yahoo ticker.
# RELIANCE-EQ on NSE -> RELIANCE.NS
DEFAULT_SYMBOL = "RELIANCE-EQ"
DEFAULT_YAHOO = "RELIANCE.NS"

# Default watchlist if none saved yet. Each entry: {"tsym": label, "yahoo": ticker}
DEFAULT_WATCHLIST = [
    {"tsym": "RELIANCE-EQ", "yahoo": "RELIANCE.NS", "name": "Reliance Industries"},
]

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
COOKIE_URL = "https://fc.yahoo.com"


class YahooQuoteClient:
    """Session-managed Yahoo quote client. Handles cookie + crumb auth and refreshes on 401."""
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(_HTTP)
        self.crumb: Optional[str] = None
        self._lock = threading.Lock()

    def _refresh_crumb(self):
        try:
            self.session.get(COOKIE_URL, timeout=10)
            # NOTE: getcrumb returns 406 if Accept=application/json; send text/plain instead.
            r = self.session.get(CRUMB_URL, timeout=10,
                                 headers={"Accept": "text/plain,*/*;q=0.8"})
            if r.status_code == 200 and r.text and "<" not in r.text:
                self.crumb = r.text.strip()
                return True
        except Exception:
            pass
        return False

    def quote(self, yahoo_symbols: list) -> Optional[dict]:
        """Return {sym: {lp, bid, ask, bid_size, ask_size, ...}} for the requested tickers."""
        if not yahoo_symbols:
            return {}
        with self._lock:
            if not self.crumb and not self._refresh_crumb():
                return None
            params = {"symbols": ",".join(yahoo_symbols), "crumb": self.crumb}
            try:
                r = self.session.get(QUOTE_URL, params=params, timeout=10)
            except Exception:
                return None
            if r.status_code == 401:
                if not self._refresh_crumb():
                    return None
                params["crumb"] = self.crumb
                try:
                    r = self.session.get(QUOTE_URL, params=params, timeout=10)
                except Exception:
                    return None
            if r.status_code != 200:
                return None
            try:
                results = r.json()["quoteResponse"]["result"]
            except (KeyError, TypeError, ValueError):
                return None
        out = {}
        for q in results:
            out[q.get("symbol")] = {
                "lp": q.get("regularMarketPrice"),
                "bid": q.get("bid"),
                "ask": q.get("ask"),
                "bid_size": q.get("bidSize"),
                "ask_size": q.get("askSize"),
                "day_open": q.get("regularMarketOpen"),
                "day_high": q.get("regularMarketDayHigh"),
                "day_low": q.get("regularMarketDayLow"),
                "prev_close": q.get("regularMarketPreviousClose"),
                "volume": q.get("regularMarketVolume"),
                "market_state": q.get("marketState"),
            }
        return out

_HTTP = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json",
}


def resolve_symbol(query: str, prefer_exch: str = "NSI"):
    """
    Resolve a free-text query (name or symbol) to a Yahoo ticker via Yahoo search.
    Returns dict {tsym, yahoo, name, exch} or None.
    prefer_exch: 'NSI' = NSE, 'BSE' = BSE.
    """
    q = (query or "").strip()
    if not q:
        return None
    try:
        r = requests.get(SEARCH_URL, params={"q": q, "quotesCount": 10, "newsCount": 0},
                         headers=_HTTP, timeout=10)
        if r.status_code != 200:
            return None
        quotes = [x for x in r.json().get("quotes", []) if x.get("symbol")]
    except Exception:
        return None
    if not quotes:
        return None

    # Prefer equities on the preferred exchange (NSE), then any .NS, then first result
    def pick(pred):
        for x in quotes:
            if pred(x):
                return x
        return None

    chosen = (
        pick(lambda x: x.get("exchange") == prefer_exch and x.get("quoteType") == "EQUITY")
        or pick(lambda x: str(x.get("symbol", "")).endswith(".NS"))
        or pick(lambda x: x.get("exchange") == "BSE")
        or quotes[0]
    )
    sym = chosen["symbol"]                     # e.g. OLAELEC.NS
    base = sym.split(".")[0]                    # e.g. OLAELEC
    return {
        "tsym": base,
        "yahoo": sym,
        "name": chosen.get("shortname") or chosen.get("longname") or base,
        "exch": chosen.get("exchange"),
    }

SCHEMA = """
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

# Columns added after initial release — migrate existing DBs idempotently.
_NEW_COLS = [
    ("bid", "REAL"),
    ("ask", "REAL"),
    ("bid_size", "INTEGER"),
    ("ask_size", "INTEGER"),
]


def _ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        existing = {r[1] for r in conn.execute("PRAGMA table_info(price_changes)").fetchall()}
        for col, typ in _NEW_COLS:
            if col not in existing:
                conn.execute(f"ALTER TABLE price_changes ADD COLUMN {col} {typ}")
        conn.commit()


def yahoo_ticker_for(tsym: str) -> str:
    """RELIANCE-EQ -> RELIANCE.NS ; pass through if already a yahoo ticker."""
    if tsym.endswith(".NS") or tsym.endswith(".BO"):
        return tsym
    base = tsym.replace("-EQ", "").replace("-BE", "").strip().upper()
    return f"{base}.NS"


class YahooPoller:
    def __init__(self, tsym: str = DEFAULT_SYMBOL, interval_sec: float = 1.0):
        self.tsym = tsym
        self.yahoo = yahoo_ticker_for(tsym)
        self.interval = max(0.5, float(interval_sec))
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "application/json",
        })
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self.last_price: Optional[float] = None
        self.change_count = 0
        self.poll_count = 0
        self.last_error: Optional[str] = None
        self.started_at: Optional[str] = None
        self._lock = threading.Lock()
        _ensure_db()

    # ---- fetch one snapshot from Yahoo ----
    def _fetch_quote(self) -> Optional[dict]:
        url = CHART_URL.format(ticker=self.yahoo)
        r = self.session.get(url, params={"interval": "1m", "range": "1d"}, timeout=10)
        if r.status_code != 200:
            self.last_error = f"Yahoo HTTP {r.status_code}"
            return None
        j = r.json()
        try:
            meta = j["chart"]["result"][0]["meta"]
        except (KeyError, IndexError, TypeError):
            self.last_error = "Unexpected Yahoo response shape"
            return None
        return {
            "lp": meta.get("regularMarketPrice"),
            "day_open": meta.get("regularMarketDayOpen") or meta.get("chartPreviousClose"),
            "day_high": meta.get("regularMarketDayHigh"),
            "day_low": meta.get("regularMarketDayLow"),
            "prev_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
            "volume": meta.get("regularMarketVolume"),
            "market_state": meta.get("marketState"),
        }

    def _save_change(self, q: dict):
        lp = q.get("lp")
        prev_close = q.get("prev_close")
        change = round(lp - prev_close, 2) if (lp is not None and prev_close) else None
        change_pct = round((change / prev_close) * 100, 2) if (change is not None and prev_close) else None
        row = (
            datetime.now().isoformat(timespec="milliseconds"),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            self.tsym,
            self.yahoo,
            lp,
            q.get("day_open"),
            q.get("day_high"),
            q.get("day_low"),
            prev_close,
            change,
            change_pct,
            q.get("volume"),
            "yahoo",
        )
        with self._lock:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    """INSERT INTO price_changes
                       (received_at, bar_time, tsym, yahoo_sym, lp, day_open, day_high,
                        day_low, prev_close, change, change_pct, volume, source)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                conn.commit()
        self.change_count += 1

    # ---- lifecycle ----
    def start(self) -> dict:
        if self.running:
            return {"ok": False, "error": "Yahoo poller already running"}
        # quick connectivity probe
        q = self._fetch_quote()
        if q is None:
            return {"ok": False, "error": self.last_error or "Failed to reach Yahoo"}

        self.running = True
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.last_price = None
        self.change_count = 0
        self.poll_count = 0

        def _run():
            while self.running:
                try:
                    q = self._fetch_quote()
                    self.poll_count += 1
                    if q and q.get("lp") is not None:
                        lp = q["lp"]
                        if self.last_price is None or lp != self.last_price:
                            self._save_change(q)
                            self.last_price = lp
                except Exception as e:
                    self.last_error = str(e)
                time.sleep(self.interval)

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()
        return {
            "ok": True,
            "tsym": self.tsym,
            "yahoo_sym": self.yahoo,
            "interval_sec": self.interval,
            "first_price": q.get("lp"),
            "market_state": q.get("market_state"),
            "note": "Records a row only when the price changes. Yahoo granularity ~1 min, ~15 min delayed in live market, no bid/ask depth.",
        }

    def stop(self) -> dict:
        if not self.running:
            return {"ok": False, "error": "Yahoo poller not running"}
        self.running = False
        return {"ok": True, "polls": self.poll_count, "changes_recorded": self.change_count}

    def status(self) -> dict:
        return {
            "running": self.running,
            "tsym": self.tsym,
            "yahoo_sym": self.yahoo,
            "interval_sec": self.interval,
            "poll_count": self.poll_count,
            "changes_recorded": self.change_count,
            "last_price": self.last_price,
            "started_at": self.started_at,
            "last_error": self.last_error,
        }

    # ---- backfill historical 1-min series ----
    def backfill(self, rng: str = "1d") -> dict:
        """Pull the full 1-minute series for the period and store each bar as a change row."""
        _ensure_db()
        url = CHART_URL.format(ticker=self.yahoo)
        r = self.session.get(url, params={"interval": "1m", "range": rng}, timeout=15)
        if r.status_code != 200:
            return {"ok": False, "error": f"Yahoo HTTP {r.status_code}"}
        j = r.json()
        try:
            res = j["chart"]["result"][0]
            meta = res["meta"]
            ts = res.get("timestamp", [])
            quote = res["indicators"]["quote"][0]
            closes = quote.get("close", [])
            vols = quote.get("volume", [])
        except (KeyError, IndexError, TypeError):
            return {"ok": False, "error": "Unexpected Yahoo response shape (market may be closed / no intraday data)"}

        prev_close = meta.get("chartPreviousClose")
        inserted = 0
        with self._lock:
            with sqlite3.connect(DB_PATH) as conn:
                for i in range(len(ts)):
                    c = closes[i] if i < len(closes) else None
                    if c is None:
                        continue
                    bar_time = datetime.fromtimestamp(ts[i]).strftime("%Y-%m-%d %H:%M:%S")
                    change = round(c - prev_close, 2) if prev_close else None
                    change_pct = round((change / prev_close) * 100, 2) if (change is not None and prev_close) else None
                    conn.execute(
                        """INSERT INTO price_changes
                           (received_at, bar_time, tsym, yahoo_sym, lp, day_open, day_high,
                            day_low, prev_close, change, change_pct, volume, source)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            datetime.now().isoformat(timespec="milliseconds"),
                            bar_time, self.tsym, self.yahoo, c,
                            meta.get("regularMarketDayOpen"), meta.get("regularMarketDayHigh"),
                            meta.get("regularMarketDayLow"), prev_close, change, change_pct,
                            vols[i] if i < len(vols) else None, "yahoo_backfill",
                        ),
                    )
                    inserted += 1
                conn.commit()
        return {"ok": True, "inserted": inserted, "range": rng, "yahoo_sym": self.yahoo}

    # ---- query ----
    @staticmethod
    def query(tsym: Optional[str] = None, limit: int = 100):
        _ensure_db()
        sql = ("SELECT received_at, bar_time, tsym, lp, bid, ask, bid_size, ask_size, "
               "change, change_pct, volume, source FROM price_changes")
        params: list = []
        if tsym:
            sql += " WHERE tsym = ?"
            params.append(tsym)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    @staticmethod
    def stats():
        _ensure_db()
        with sqlite3.connect(DB_PATH) as conn:
            total = conn.execute("SELECT COUNT(*) FROM price_changes").fetchone()[0]
            by_sym = conn.execute(
                "SELECT tsym, COUNT(*) n, MIN(bar_time) first, MAX(bar_time) last FROM price_changes GROUP BY tsym"
            ).fetchall()
            return {
                "total_rows": total,
                "by_symbol": [{"tsym": r[0], "count": r[1], "first": r[2], "last": r[3]} for r in by_sym],
                "db_path": DB_PATH,
            }


# ============================================================
# Multi-symbol poller: tracks a whole watchlist in one thread.
# ============================================================
class YahooMultiPoller:
    def __init__(self, interval_sec: float = 1.0):
        self.interval = max(0.5, float(interval_sec))
        self.symbols: dict = {}            # tsym -> {"yahoo":..., "name":...}
        self.last_price: dict = {}         # tsym -> float
        self.last_bid: dict = {}           # tsym -> float
        self.last_ask: dict = {}           # tsym -> float
        self.change_count: dict = {}       # tsym -> int
        self.quote_client = YahooQuoteClient()
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self.poll_count = 0
        self.last_error: Optional[str] = None
        self.started_at: Optional[str] = None
        self._lock = threading.Lock()
        _ensure_db()
        self._load_watchlist()

    # ---- watchlist persistence ----
    def _load_watchlist(self):
        wl = DEFAULT_WATCHLIST
        if os.path.exists(WATCHLIST_PATH):
            try:
                with open(WATCHLIST_PATH) as f:
                    wl = json.load(f) or DEFAULT_WATCHLIST
            except Exception:
                wl = DEFAULT_WATCHLIST
        for e in wl:
            self.symbols[e["tsym"]] = {"yahoo": e["yahoo"], "name": e.get("name", e["tsym"])}
            self.change_count.setdefault(e["tsym"], 0)

    def _save_watchlist(self):
        os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
        data = [{"tsym": k, "yahoo": v["yahoo"], "name": v["name"]} for k, v in self.symbols.items()]
        with open(WATCHLIST_PATH, "w") as f:
            json.dump(data, f, indent=2)

    def list_symbols(self):
        return [
            {"tsym": k, "yahoo": v["yahoo"], "name": v["name"],
             "last_price": self.last_price.get(k),
             "bid": self.last_bid.get(k), "ask": self.last_ask.get(k),
             "changes": self.change_count.get(k, 0)}
            for k, v in self.symbols.items()
        ]

    def add_symbol(self, query: str) -> dict:
        info = resolve_symbol(query)
        if not info:
            return {"ok": False, "error": f"Could not resolve '{query}' to a stock symbol."}
        tsym = info["tsym"]
        with self._lock:
            self.symbols[tsym] = {"yahoo": info["yahoo"], "name": info["name"]}
            self.change_count.setdefault(tsym, 0)
            self._save_watchlist()
        return {"ok": True, "added": {"tsym": tsym, "yahoo": info["yahoo"], "name": info["name"], "exch": info.get("exch")}}

    def remove_symbol(self, tsym: str) -> dict:
        with self._lock:
            if tsym in self.symbols:
                del self.symbols[tsym]
                self._save_watchlist()
                return {"ok": True, "removed": tsym}
        return {"ok": False, "error": f"'{tsym}' not in watchlist"}

    # ---- save one symbol's quote ----
    def _save(self, tsym: str, yahoo: str, q: dict):
        lp = q.get("lp")
        prev_close = q.get("prev_close")
        change = round(lp - prev_close, 2) if (lp is not None and prev_close) else None
        change_pct = round((change / prev_close) * 100, 2) if (change is not None and prev_close) else None
        row = (
            datetime.now().isoformat(timespec="milliseconds"),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            tsym, yahoo, lp,
            q.get("bid"), q.get("ask"), q.get("bid_size"), q.get("ask_size"),
            q.get("day_open"), q.get("day_high"), q.get("day_low"),
            prev_close, change, change_pct, q.get("volume"), "yahoo",
        )
        with self._lock:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    """INSERT INTO price_changes
                       (received_at, bar_time, tsym, yahoo_sym, lp,
                        bid, ask, bid_size, ask_size,
                        day_open, day_high, day_low, prev_close, change, change_pct, volume, source)
                       VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?,?,?,?,?)""", row)
                conn.commit()
        self.change_count[tsym] = self.change_count.get(tsym, 0) + 1

    # ---- lifecycle ----
    def start(self) -> dict:
        if self.running:
            return {"ok": False, "error": "Poller already running"}
        self.running = True
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.poll_count = 0

        def _run():
            while self.running:
                try:
                    snap = list(self.symbols.items())  # copy to avoid mutation
                    if not snap:
                        time.sleep(self.interval); continue
                    yahoo_list = [v["yahoo"] for _, v in snap]
                    quotes = self.quote_client.quote(yahoo_list) or {}
                    self.poll_count += 1
                    for tsym, info in snap:
                        q = quotes.get(info["yahoo"])
                        if not q or q.get("lp") is None:
                            continue
                        lp, bid, ask = q.get("lp"), q.get("bid"), q.get("ask")
                        # Record on any change of lp, bid, or ask
                        changed = (
                            self.last_price.get(tsym) != lp
                            or self.last_bid.get(tsym) != bid
                            or self.last_ask.get(tsym) != ask
                        )
                        if changed:
                            self._save(tsym, info["yahoo"], q)
                            self.last_price[tsym] = lp
                            self.last_bid[tsym] = bid
                            self.last_ask[tsym] = ask
                except Exception as e:
                    self.last_error = str(e)
                time.sleep(self.interval)

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()
        return {"ok": True, "symbols": self.list_symbols(), "interval_sec": self.interval}

    def stop(self) -> dict:
        if not self.running:
            return {"ok": False, "error": "Poller not running"}
        self.running = False
        return {"ok": True, "polls": self.poll_count}

    def status(self) -> dict:
        return {
            "running": self.running,
            "interval_sec": self.interval,
            "poll_count": self.poll_count,
            "symbols": self.list_symbols(),
            "started_at": self.started_at,
            "last_error": self.last_error,
        }

    def backfill(self, tsym: str, rng: str = "1d") -> dict:
        info = self.symbols.get(tsym)
        if not info:
            return {"ok": False, "error": f"'{tsym}' not in watchlist"}
        p = YahooPoller(tsym=tsym)
        p.yahoo = info["yahoo"]
        return p.backfill(rng=rng)
