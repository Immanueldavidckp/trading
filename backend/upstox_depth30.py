"""
upstox_depth30.py — 30-level order-book stream (Upstox Plus, Market Data Feed V3).

The account's Upstox Plus plan unlocks the websocket `full_d30` mode:
  • 30 bid + 30 ask levels per instrument (vs 5 from the REST quote poll)
  • up to 50 instruments per websocket connection, 5 connections per user

This module keeps ONE streamer connection via the official SDK
(`upstox-python-sdk`, MarketDataStreamerV3 — it decodes the protobuf feed to
dicts) and holds the LATEST 30-level book per symbol in memory. Nothing is
persisted: the 1s REST poll in upstox_feed.py keeps writing the 5-level
snapshots to `market_depth` exactly as before (the tick-replay/order-flow
gates depend on that history), so this stream is a pure live-view upgrade.

Subscription policy: watchlist + day-plan universe, capped at D30_MAX_KEYS.
When the chart asks for a symbol that isn't subscribed, `ensure()` adds it,
evicting the least-recently-viewed non-watchlist symbol if the cap is hit.

D30 payload note: `bidAskQuote` carries bidQ/bidP/askQ/askP per level —
NO per-level order counts (that exists only in the 5-level REST book), and
quantities arrive as protobuf-int64 STRINGS. Both are normalized here.
"""
from __future__ import annotations
from typing import Dict, List, Optional
import threading
import time
import json

D30_MAX_KEYS = 50            # Upstox Plus: full_d30 cap per websocket connection
FRESH_SECS = 15              # book older than this = stale (fall back to REST 5-level)


def _int(v):
    try:
        return int(float(v)) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


class Depth30Feed:
    """One MarketDataStreamerV3 connection in full_d30 mode + in-memory books."""

    def __init__(self, upstox_client):
        self.ux = upstox_client
        self.books: Dict[str, dict] = {}       # tsym -> {buy, sell, ltp, ts}
        self._key2sym: Dict[str, str] = {}
        self._sym2key: Dict[str, str] = {}
        self._watch_syms: set = set()          # protected from eviction
        self._last_seen: Dict[str, float] = {} # tsym -> last ensure() (LRU)
        self._streamer = None
        self._lock = threading.Lock()
        self._started = False
        self._connected = False
        self._last_msg = 0.0
        self._error: Optional[str] = None
        self._token_used: Optional[str] = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self, symbols: List[str]) -> dict:
        """Connect and subscribe `symbols` (deduped, capped). Safe to re-call —
        reconnects if the daily token changed or the socket died."""
        with self._lock:
            token = getattr(self.ux, "access_token", None)
            if not token:
                self._error = "no Upstox token"
                return {"ok": False, "error": self._error}
            if self._started and self._connected and token == self._token_used:
                return {"ok": True, "already": True, "subscribed": len(self._key2sym)}
            self._teardown()
            try:
                from upstox_client import MarketDataStreamerV3, ApiClient, Configuration
            except ImportError as e:
                self._error = f"upstox-python-sdk not installed: {e}"
                return {"ok": False, "error": self._error}

            keys, watch = [], set()
            for tsym in symbols:
                t = (tsym or "").upper()
                if not t or t in self._sym2key:
                    continue
                k = self.ux.instrument_key(t)
                if k:
                    self._sym2key[t] = k
                    self._key2sym[k] = t
                    watch.add(t)
                    keys.append(k)
                if len(keys) >= D30_MAX_KEYS:
                    break
            self._watch_syms = watch
            if not keys:
                self._error = "no resolvable symbols"
                return {"ok": False, "error": self._error}

            cfg = Configuration()
            cfg.access_token = token
            self._token_used = token
            s = MarketDataStreamerV3(ApiClient(cfg), keys, "full_d30")
            s.on("message", self._on_message)
            s.on("error", self._on_error)
            s.on("open", lambda *a: self._set_connected(True))
            s.on("close", lambda *a: self._set_connected(False))
            self._streamer = s
            threading.Thread(target=self._run, daemon=True,
                             name="d30-streamer").start()
            self._started = True
            self._error = None
            return {"ok": True, "subscribed": len(keys)}

    def _run(self):
        try:
            self._streamer.connect()
        except Exception as e:
            self._error = f"connect: {e}"
            self._connected = False

    def _teardown(self):
        try:
            if self._streamer:
                self._streamer.disconnect()
        except Exception:
            pass
        self._streamer = None
        self._connected = False
        self._key2sym.clear()
        self._sym2key.clear()

    def _set_connected(self, up: bool):
        self._connected = up

    # ── feed handlers ───────────────────────────────────────────────────────

    def _on_error(self, err):
        self._error = str(err)[:300]

    def _on_message(self, m):
        try:
            if isinstance(m, (bytes, str)):
                m = json.loads(m)
            feeds = (m or {}).get("feeds") or {}
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
                # drop zero/empty levels (book can be shorter than 30 in illiquid names)
                buy = [x for x in buy if x["price"]]
                sell = [x for x in sell if x["price"]]
                ltpc = ff.get("ltpc") or {}
                self.books[tsym] = {
                    "buy": buy, "sell": sell,
                    "ltp": ltpc.get("ltp"), "ts": now,
                    "total_buy_qty": sum(x["quantity"] for x in buy),
                    "total_sell_qty": sum(x["quantity"] for x in sell),
                }
            if feeds:
                self._last_msg = now
        except Exception as e:
            self._error = f"parse: {str(e)[:200]}"

    # ── subscription management ─────────────────────────────────────────────

    def ensure(self, tsym: str) -> bool:
        """Make sure `tsym` is in the D30 subscription (LRU-evicting if full).
        Returns True if it is (or now is) subscribed."""
        t = (tsym or "").upper()
        if not t or not self._started or not self._streamer:
            return False
        self._last_seen[t] = time.time()
        if t in self._sym2key:
            return True
        k = self.ux.instrument_key(t)
        if not k:
            return False
        with self._lock:
            try:
                if len(self._sym2key) >= D30_MAX_KEYS:
                    # evict the least-recently-viewed non-watchlist symbol
                    cands = [s for s in self._sym2key if s not in self._watch_syms]
                    if not cands:
                        return False
                    victim = min(cands, key=lambda s: self._last_seen.get(s, 0))
                    vk = self._sym2key.pop(victim)
                    self._key2sym.pop(vk, None)
                    self.books.pop(victim, None)
                    try:
                        self._streamer.unsubscribe([vk])
                    except Exception:
                        pass
                self._streamer.subscribe([k], "full_d30")
                self._sym2key[t] = k
                self._key2sym[k] = t
                return True
            except Exception as e:
                self._error = f"subscribe {t}: {str(e)[:150]}"
                return False

    # ── reads ───────────────────────────────────────────────────────────────

    def get_book(self, tsym: str) -> Optional[dict]:
        b = self.books.get((tsym or "").upper())
        if not b or (time.time() - b["ts"]) > FRESH_SECS:
            return None
        return b

    def status(self) -> dict:
        return {
            "started": self._started, "connected": self._connected,
            "subscribed": sorted(self._sym2key.keys()),
            "n_subscribed": len(self._sym2key), "cap": D30_MAX_KEYS,
            "books_live": sum(1 for b in self.books.values()
                              if time.time() - b["ts"] <= FRESH_SECS),
            "last_msg_age_s": round(time.time() - self._last_msg, 1) if self._last_msg else None,
            "error": self._error,
        }
