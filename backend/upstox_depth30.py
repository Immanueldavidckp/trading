"""
upstox_depth30.py — 30-level order-book stream (Upstox Plus, Market Data Feed V3).

The account's Upstox Plus plan unlocks the websocket `full_d30` mode:
  • 30 bid + 30 ask levels per instrument (vs 5 from the REST quote poll)
  • up to 50 instruments per websocket connection, 5 connections per user

Implementation note — why this talks the raw protocol instead of using the
SDK's MarketDataStreamerV3 wrapper: the pip package is ALSO named
`upstox_client`, and this repo's own backend/upstox_client.py shadows it when
the app runs from backend/. So we do the three steps ourselves:
  1. GET /v3/feed/market-data-feed/authorize  → one-time wss:// url
  2. websocket-client connection; subscriptions are BINARY frames of JSON:
     {"guid": ..., "method": "sub", "data": {"mode": "full_d30", "instrumentKeys": [...]}}
  3. messages are protobuf FeedResponse — decoded with the SDK's generated
     MarketDataFeedV3_pb2 module loaded DIRECTLY BY FILE PATH from
     site-packages (generated pb2 files import only google.protobuf, so the
     name collision never comes into play).

The latest 30-level book per symbol is held in memory only: the 1s REST poll
in upstox_feed.py keeps writing the 5-level snapshots to `market_depth`
exactly as before (tick replay / order-flow gates depend on that history) —
this stream is a pure live-view upgrade, and /api/upstox/depth30 falls back
to the recorded book whenever the stream is stale.

Subscription policy: watchlist + day-plan universe, capped at D30_MAX_KEYS;
`ensure()` adds chart symbols on demand, LRU-evicting non-watchlist names.

D30 payload note: `bidAskQuote` carries bidQ/bidP/askQ/askP per level — no
per-level order counts (those exist only in the 5-level REST book), and
quantities arrive as protobuf-int64 STRINGS. Both are normalized here.
"""
from __future__ import annotations
from typing import Dict, List, Optional
from collections import deque
import glob
import importlib.util
import json
import os
import sys
import threading
import time
import uuid

import requests

D30_MAX_KEYS = 50            # Upstox Plus: full_d30 cap per websocket connection
FRESH_SECS = 15              # book older than this = stale (fall back to REST 5-level)
AUTH_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"

# Heatmap history: a RAM ring buffer per recorded symbol (the box has ~1.9 GB
# total and market_depth is already >3 GB on disk, so book history is
# deliberately NOT persisted — 1 snapshot/sec × 40 min × 8 symbols ≈ 25 MB).
HIST_SECS = 2400             # ~40 minutes of book history per symbol
HIST_MAX_SYMBOLS = 8         # LRU: symbols whose books get recorded
HIST_MIN_GAP = 1.0           # seconds between recorded snapshots (feed is faster)


def _int(v):
    try:
        return int(float(v)) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def _load_pb2():
    """Load the SDK's generated MarketDataFeed pb2 module by file path,
    sidestepping the upstox_client name collision."""
    backend = os.path.dirname(os.path.abspath(__file__))
    hits = []
    for p in sys.path:
        try:
            if not p or os.path.abspath(p) == backend:
                continue
            # SDK layout has varied: upstox_client/proto/… and
            # upstox_client/feeder/proto/… — search the package recursively
            hits += glob.glob(os.path.join(p, "upstox_client", "**",
                                           "MarketDataFeed*_pb2.py"),
                              recursive=True)
        except Exception:
            continue
    # prefer the V3 proto if both generations are present
    hits.sort(key=lambda h: "V3" not in os.path.basename(h))
    if not hits:
        raise ImportError("upstox-python-sdk proto module not found — "
                          "pip install upstox-python-sdk")
    spec = importlib.util.spec_from_file_location("upstox_d30_pb2", hits[0])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Depth30Feed:
    """One raw websocket connection in full_d30 mode + in-memory books."""

    def __init__(self, upstox_client):
        self.ux = upstox_client
        self.books: Dict[str, dict] = {}       # tsym -> {buy, sell, ltp, ts, totals}
        self._key2sym: Dict[str, str] = {}
        self._sym2key: Dict[str, str] = {}
        self._watch_syms: set = set()          # protected from LRU eviction
        self._last_seen: Dict[str, float] = {}
        # heatmap history: tsym -> deque[(ts, bid_p, bid_q, ask_p, ask_q, ltp)]
        self._hist: Dict[str, deque] = {}
        self._hist_last: Dict[str, float] = {}
        self._pb2 = None
        self._ws = None                        # live WebSocketApp (when open)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connected = False
        self._last_msg = 0.0
        self._error: Optional[str] = None
        self._token_used: Optional[str] = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self, symbols: List[str]) -> dict:
        """Connect and subscribe `symbols` (deduped, capped at 50). Safe to
        re-call — reconnects when the daily token changed or the socket died."""
        with self._lock:
            token = getattr(self.ux, "access_token", None)
            if not token:
                self._error = "no Upstox token"
                return {"ok": False, "error": self._error}
            if (self._thread and self._thread.is_alive() and self._connected
                    and token == self._token_used):
                return {"ok": True, "already": True, "subscribed": len(self._sym2key)}
            try:
                self._pb2 = self._pb2 or _load_pb2()
            except ImportError as e:
                self._error = str(e)
                return {"ok": False, "error": self._error}

            self._teardown_locked()
            keys = []
            self._watch_syms = set()
            for tsym in symbols:
                t = (tsym or "").upper()
                if not t or t in self._sym2key:
                    continue
                k = self.ux.instrument_key(t)
                if k:
                    self._sym2key[t] = k
                    self._key2sym[k] = t
                    self._watch_syms.add(t)
                    keys.append(k)
                if len(keys) >= D30_MAX_KEYS:
                    break
            if not keys:
                self._error = "no resolvable symbols"
                return {"ok": False, "error": self._error}

            self._token_used = token
            self._stop.clear()
            self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                            name="d30-streamer")
            self._thread.start()
            self._error = None
            return {"ok": True, "subscribed": len(keys)}

    def _teardown_locked(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        self._connected = False
        self._key2sym.clear()
        self._sym2key.clear()
        self.books.clear()

    # ── connection loop (re-authorizes on every reconnect) ─────────────────

    def _authorize(self) -> Optional[str]:
        try:
            r = requests.get(AUTH_URL, headers=self.ux._headers(), timeout=10)
            d = r.json()
            uri = ((d.get("data") or {}).get("authorized_redirect_uri")
                   or (d.get("data") or {}).get("authorizedRedirectUri"))
            if not uri:
                self._error = f"authorize: {str(d)[:200]}"
            return uri
        except Exception as e:
            self._error = f"authorize: {str(e)[:200]}"
            return None

    def _run_loop(self):
        import websocket  # websocket-client
        backoff = 2
        while not self._stop.is_set():
            uri = self._authorize()
            if not uri:
                if self._stop.wait(min(backoff, 60)):
                    return
                backoff *= 2
                continue

            def on_open(ws):
                self._connected = True
                self._error = None
                keys = list(self._key2sym.keys())
                if keys:
                    self._send(ws, "sub", keys)

            def on_message(ws, message):
                self._handle_frame(message)

            def on_error(ws, err):
                self._error = str(err)[:300]

            def on_close(ws, *a):
                self._connected = False

            ws = websocket.WebSocketApp(uri, on_open=on_open, on_message=on_message,
                                        on_error=on_error, on_close=on_close)
            self._ws = ws
            try:
                ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception as e:
                self._error = f"ws: {str(e)[:200]}"
            self._ws = None
            self._connected = False
            if self._stop.is_set():
                return
            backoff = 2 if self._last_msg and time.time() - self._last_msg < 120 else min(backoff * 2, 60)
            if self._stop.wait(backoff):
                return

    def _send(self, ws, method: str, keys: List[str], mode: str = "full_d30"):
        import websocket
        frame = json.dumps({
            "guid": uuid.uuid4().hex[:16],
            "method": method,
            "data": {"mode": mode, "instrumentKeys": keys},
        }).encode("utf-8")
        ws.send(frame, opcode=websocket.ABNF.OPCODE_BINARY)

    # ── frame decode ────────────────────────────────────────────────────────

    def _handle_frame(self, message):
        try:
            if isinstance(message, str):
                d = json.loads(message)          # rare text frames (market_info)
            else:
                from google.protobuf.json_format import MessageToDict
                resp = self._pb2.FeedResponse()
                resp.ParseFromString(message)
                d = MessageToDict(resp)
            feeds = (d or {}).get("feeds") or {}
            now = time.time()
            for k, v in feeds.items():
                tsym = self._key2sym.get(k)
                if not tsym:
                    continue
                ff = (v.get("fullFeed") or {}).get("marketFF") or {}
                baq = (ff.get("marketLevel") or {}).get("bidAskQuote") or []
                if not baq:
                    continue
                buy = [{"price": l.get("bidP"), "quantity": _int(l.get("bidQ")),
                        "orders": None} for l in baq]
                sell = [{"price": l.get("askP"), "quantity": _int(l.get("askQ")),
                         "orders": None} for l in baq]
                buy = [x for x in buy if x["price"]]
                sell = [x for x in sell if x["price"]]
                ltpc = ff.get("ltpc") or {}
                self.books[tsym] = {
                    "buy": buy, "sell": sell,
                    "ltp": ltpc.get("ltp"), "ts": now,
                    "total_buy_qty": sum(x["quantity"] for x in buy),
                    "total_sell_qty": sum(x["quantity"] for x in sell),
                }
                # ring-buffer the ladder for the liquidity heatmap (throttled)
                h = self._hist.get(tsym)
                if h is not None and (now - self._hist_last.get(tsym, 0)) >= HIST_MIN_GAP:
                    self._hist_last[tsym] = now
                    h.append((round(now, 2),
                              tuple(x["price"] for x in buy),
                              tuple(x["quantity"] for x in buy),
                              tuple(x["price"] for x in sell),
                              tuple(x["quantity"] for x in sell),
                              ltpc.get("ltp")))
            if feeds:
                self._last_msg = now
        except Exception as e:
            self._error = f"parse: {str(e)[:200]}"

    # ── subscription management ─────────────────────────────────────────────

    def ensure(self, tsym: str) -> bool:
        """Make sure `tsym` is in the D30 subscription (LRU-evicting a
        non-watchlist name if the 50-key cap is hit)."""
        t = (tsym or "").upper()
        if not t:
            return False
        self._last_seen[t] = time.time()
        if t in self._sym2key:
            return True
        ws = self._ws
        if not ws or not self._connected:
            return False
        k = self.ux.instrument_key(t)
        if not k:
            return False
        with self._lock:
            try:
                if len(self._sym2key) >= D30_MAX_KEYS:
                    cands = [s for s in self._sym2key if s not in self._watch_syms]
                    if not cands:
                        return False
                    victim = min(cands, key=lambda s: self._last_seen.get(s, 0))
                    vk = self._sym2key.pop(victim)
                    self._key2sym.pop(vk, None)
                    self.books.pop(victim, None)
                    try:
                        self._send(ws, "unsub", [vk])
                    except Exception:
                        pass
                self._send(ws, "sub", [k])
                self._sym2key[t] = k
                self._key2sym[k] = t
                return True
            except Exception as e:
                self._error = f"subscribe {t}: {str(e)[:150]}"
                return False

    def record(self, tsym: str) -> bool:
        """Start ring-buffering `tsym`'s ladder for the heatmap (LRU-capped at
        HIST_MAX_SYMBOLS). Idempotent; called by the order-flow endpoints."""
        t = (tsym or "").upper()
        if not t:
            return False
        self.ensure(t)
        if t in self._hist:
            return True
        if len(self._hist) >= HIST_MAX_SYMBOLS:
            victim = min(self._hist, key=lambda s: self._last_seen.get(s, 0))
            if victim == t:
                return True
            self._hist.pop(victim, None)
            self._hist_last.pop(victim, None)
        self._hist[t] = deque(maxlen=int(HIST_SECS / HIST_MIN_GAP))
        return True

    def history(self, tsym: str, seconds: int = 1800) -> List[tuple]:
        """Recorded ladder snapshots for the last `seconds` (oldest-first)."""
        h = self._hist.get((tsym or "").upper())
        if not h:
            return []
        cut = time.time() - seconds
        return [s for s in h if s[0] >= cut]

    # ── reads ───────────────────────────────────────────────────────────────

    def get_book(self, tsym: str) -> Optional[dict]:
        b = self.books.get((tsym or "").upper())
        if not b or (time.time() - b["ts"]) > FRESH_SECS:
            return None
        return b

    def status(self) -> dict:
        return {
            "started": bool(self._thread and self._thread.is_alive()),
            "connected": self._connected,
            "subscribed": sorted(self._sym2key.keys()),
            "n_subscribed": len(self._sym2key), "cap": D30_MAX_KEYS,
            "books_live": sum(1 for b in self.books.values()
                              if time.time() - b["ts"] <= FRESH_SECS),
            "recording": {s: len(h) for s, h in self._hist.items()},
            "last_msg_age_s": round(time.time() - self._last_msg, 1) if self._last_msg else None,
            "error": self._error,
        }
