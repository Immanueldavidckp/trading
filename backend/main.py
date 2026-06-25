import os
import json
import asyncio
import sqlite3
from fastapi import FastAPI, HTTPException, Body, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
from dotenv import load_dotenv

# Load .env from backend dir and project root (project root takes lower priority)
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_BACKEND_DIR)
load_dotenv(os.path.join(_ROOT_DIR, ".env"))
load_dotenv(os.path.join(_BACKEND_DIR, ".env"), override=True)

from upstox_client import UpstoxClient, ALL_INTERVALS
from upstox_feed import UpstoxQuoteFeed
from fastapi.responses import RedirectResponse, HTMLResponse

app = FastAPI(title="Upstox Trading Backend")

# Enable CORS for React frontend (Vite defaults to 5173 or 5174)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For local ease, we'll allow all. Can restrict to http://localhost:5173
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory singletons — Upstox only
upstox: Optional[UpstoxClient] = None
feed: Optional[UpstoxQuoteFeed] = None


# Pydantic schemas
class SymbolAddModel(BaseModel):
    query: str  # NSE stock symbol, e.g. "TCS", "INFY", "SUZLON"


class SymbolRemoveModel(BaseModel):
    tsym: str


# Initialize singletons on startup
@app.on_event("startup")
def startup_event():
    global upstox, feed
    # Initialise Upstox client (loads cached token + instruments if available)
    try:
        upstox = UpstoxClient()
        st = upstox.status()
        print(f"Upstox: configured={st['configured']} logged_in={st['logged_in']} instruments={st['instruments_cached']}")
    except Exception as e:
        print(f"Upstox init failed: {e}")
        return

    # If the daily token has expired, try the automated TOTP login (no manual
    # OTP). Silently skips if the auto-login credentials aren't configured.
    if not upstox.access_token:
        try:
            import upstox_autologin
            if upstox_autologin.configured():
                res = upstox_autologin.get_token()
                if res.get("ok"):
                    upstox._save_token(res["access_token"])
                    print(f"Upstox auto-login OK as {res.get('user')}")
                else:
                    print(f"Upstox auto-login failed: {res.get('error')}")
        except Exception as e:
            print(f"Upstox auto-login error: {e}")

    # Auto-start the Upstox real-time feed (full-quote poll -> price_changes +
    # market_depth). Replaces the old Yahoo poller as the single data source.
    try:
        feed = UpstoxQuoteFeed(upstox, interval_sec=1.0)
        res = feed.start()
        print(f"Upstox feed auto-start: {res.get('ok')} (symbols={res.get('symbols')})")
    except Exception as e:
        print(f"Upstox feed auto-start failed: {e}")

# ---------- Watchlist + live quotes (Upstox feed) ----------
def _feed() -> UpstoxQuoteFeed:
    global feed
    if feed is None:
        feed = UpstoxQuoteFeed(_upstox(), interval_sec=1.0)
        feed.start()
    return feed


# Watchlist endpoints kept at the /api/yahoo/symbols* paths so the existing
# frontend keeps working — now backed entirely by the Upstox feed.
@app.get("/api/yahoo/symbols")
def list_symbols():
    """List watchlist stocks with their latest Upstox price."""
    return {"stat": "Ok", "symbols": _feed().list_symbols()}


@app.post("/api/yahoo/symbols/add")
def add_symbol(req: SymbolAddModel):
    """Add an NSE stock by symbol (resolved against the Upstox instrument master)."""
    return _feed().add_symbol(req.query)


@app.post("/api/yahoo/symbols/remove")
def remove_symbol(req: SymbolRemoveModel):
    """Remove a tracked stock by its symbol."""
    return _feed().remove_symbol(req.tsym)


@app.get("/api/feed/status")
def feed_status():
    """Upstox real-time feed status (running, interval, watchlist)."""
    return _feed().status()


@app.get("/api/upstox/quotes")
def upstox_quotes(symbols: str):
    """Full market quote — LTP / OHLC / volume / OI / 5-level depth — for up to
    500 comma-separated NSE symbols in a single call."""
    u = _upstox()
    if not u.access_token:
        return {"stat": "Not_Ok", "emsg": "Not logged in to Upstox"}
    import requests as _rq
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    key_to_sym = {}
    for s in syms:
        k = u.instrument_key(s)
        if k:
            key_to_sym[k] = s
    out = {}
    ik_list = list(key_to_sym.keys())
    for i in range(0, len(ik_list), 500):
        batch = ik_list[i:i + 500]
        try:
            r = _rq.get("https://api.upstox.com/v2/market-quote/quotes",
                        headers=u._headers(),
                        params={"instrument_key": ",".join(batch)}, timeout=10)
            d = r.json()
        except Exception as e:
            return {"stat": "Not_Ok", "emsg": str(e)}
        if d.get("status") == "success":
            for _, q in (d.get("data") or {}).items():
                ik = q.get("instrument_token")
                out[key_to_sym.get(ik, q.get("symbol"))] = q
    return {"stat": "Ok", "quotes": out}


@app.get("/api/upstox/depth")
def upstox_depth(tsym: str):
    """Latest stored 5-level order book snapshot for a symbol."""
    d = UpstoxQuoteFeed.latest_depth(tsym)
    if not d:
        return {"stat": "Not_Ok", "emsg": "No depth recorded yet for " + tsym}
    return {"stat": "Ok", "tsym": tsym.upper(), **d}


# ---------- Analysis + Suggestion modes ----------
@app.get("/api/analysis/ticks")
def analysis_ticks(tsym: str, limit: int = 400):
    """Tick-by-tick replay: aggressor (buyer/seller), traded volume, order-book
    imbalance, and the SMC zone (FVG/OB) each tick reacted at — plus a
    who's-in-control summary."""
    import analysis
    return analysis.analysis_ticks(tsym, limit=max(20, min(int(limit), 2000)))


@app.get("/api/analysis/candle")
def analysis_candle(tsym: str, interval: str, start: int):
    """Every tick inside ONE candle (interval starting at `start` ms epoch),
    each with traded volume, aggressor, imbalance and full 5-level order book."""
    import analysis
    return analysis.candle_ticks(tsym, interval, int(start))


@app.get("/api/suggestion")
def api_suggestion(tsym: str):
    """Multi-timeframe directional call (1m/5m/15m/1h/4h/1d) with target + entry."""
    import analysis
    return analysis.suggestions(tsym)


@app.get("/api/fvg_plan")
def api_fvg_plan(tsym: str):
    """Backtested FVG trade plans per timeframe: entry/stop/target ranges,
    profit, win-rate (Wilson CI), expected time-to-target, EV."""
    import analysis
    return analysis.fvg_plans(tsym)


@app.get("/api/candles_db")
def candles_db(tsym: str, tf: int = 60, limit: int = 500, day: Optional[str] = None, full: int = 0):
    """
    OHLCV candles for ONE IST trading day, aggregated from stored ticks.
      tf    = candle seconds (0 = raw per-tick rows, else 1..3600)
      limit = max candles/ticks returned (most recent first trimmed)
      day   = IST date 'YYYY-MM-DD' (default: today IST) — saved-data browsing
      full  = 1 to include pre/post market rows (default: 09:00-15:40 IST only)
    All rows come straight from the price_changes table — no synthetic data.
    """
    import db as _dbm
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    IST = _tz(_td(hours=5, minutes=30))
    tf = max(0, min(int(tf), 3600))
    limit = max(10, min(int(limit), 4000))

    now_ist = _dt.now(_tz.utc).astimezone(IST)
    try:
        d = _dt.strptime(day, "%Y-%m-%d").date() if day else now_ist.date()
    except ValueError:
        d = now_ist.date()

    if full:
        start_ist = _dt(d.year, d.month, d.day, 0, 0, tzinfo=IST)
        end_ist = start_ist + _td(days=1)
    else:
        # NSE session window (incl. pre-open + small buffer)
        start_ist = _dt(d.year, d.month, d.day, 9, 0, tzinfo=IST)
        end_ist = _dt(d.year, d.month, d.day, 15, 40, tzinfo=IST)
    start_utc = start_ist.astimezone(_tz.utc).replace(tzinfo=None).isoformat(timespec="milliseconds")
    end_utc = end_ist.astimezone(_tz.utc).replace(tzinfo=None).isoformat(timespec="milliseconds")

    sql = (f"SELECT received_at, lp, volume FROM price_changes "
           f"WHERE tsym = {_dbm.PLACE} AND received_at >= {_dbm.PLACE} AND received_at < {_dbm.PLACE} "
           f"AND lp IS NOT NULL ORDER BY id ASC")
    conn = _dbm.connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, [tsym, start_utc, end_utc])
        raw = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    out: list = []
    if tf == 0:
        # Raw tick rows, each with per-tick volume delta
        prev_vol = None
        for received_at, lp, vol in raw:
            try:
                ts = _dt.fromisoformat(received_at).timestamp()
            except (ValueError, TypeError):
                continue
            dv = max(0, vol - prev_vol) if (vol is not None and prev_vol is not None) else 0
            if vol is not None:
                prev_vol = vol
            out.append({"t": int(ts * 1000), "o": lp, "h": lp, "l": lp, "c": lp, "v": dv})
        out = out[-limit:]
    else:
        candles: dict = {}
        order: list = []
        for received_at, lp, vol in raw:
            try:
                ts = _dt.fromisoformat(received_at).timestamp()
            except (ValueError, TypeError):
                continue
            b = int(ts // tf) * tf
            c = candles.get(b)
            if c is None:
                candles[b] = {"t": b * 1000, "o": lp, "h": lp, "l": lp, "c": lp,
                              "v0": vol or 0, "v1": vol or 0}
                order.append(b)
            else:
                if lp > c["h"]: c["h"] = lp
                if lp < c["l"]: c["l"] = lp
                c["c"] = lp
                if vol is not None: c["v1"] = vol
        for b in order[-limit:]:
            c = candles[b]
            out.append({"t": c["t"], "o": c["o"], "h": c["h"], "l": c["l"],
                        "c": c["c"], "v": max(0, (c["v1"] or 0) - (c["v0"] or 0))})

    return {"stat": "Ok", "tsym": tsym, "tf": tf, "day": str(d), "candles": out}


@app.get("/api/analysis_db")
def analysis_db(tsym: str, tf: int = 60, day: Optional[str] = None, limit: int = 400):
    """
    Per-bucket analysis table from stored ticks (default 1-minute buckets):
      - open / high / low / close
      - range (high-low) and % move (close vs open)
      - volume traded in that bucket
      - number of recorded price changes in that bucket
    Session hours 09:00-15:40 IST. 100% from price_changes (no synthetic data).
    """
    import db as _dbm
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    IST = _tz(_td(hours=5, minutes=30))
    tf = max(1, min(int(tf), 3600))
    limit = max(10, min(int(limit), 2000))

    now_ist = _dt.now(_tz.utc).astimezone(IST)
    try:
        dd = _dt.strptime(day, "%Y-%m-%d").date() if day else now_ist.date()
    except ValueError:
        dd = now_ist.date()

    start_ist = _dt(dd.year, dd.month, dd.day, 9, 0, tzinfo=IST)
    end_ist = _dt(dd.year, dd.month, dd.day, 15, 40, tzinfo=IST)
    start_utc = start_ist.astimezone(_tz.utc).replace(tzinfo=None).isoformat(timespec="milliseconds")
    end_utc = end_ist.astimezone(_tz.utc).replace(tzinfo=None).isoformat(timespec="milliseconds")

    sql = (f"SELECT received_at, lp, volume FROM price_changes "
           f"WHERE tsym = {_dbm.PLACE} AND received_at >= {_dbm.PLACE} AND received_at < {_dbm.PLACE} "
           f"AND lp IS NOT NULL ORDER BY id ASC")
    conn = _dbm.connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, [tsym, start_utc, end_utc])
        raw = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    buckets: dict = {}
    order: list = []
    for received_at, lp, vol in raw:
        try:
            ts = _dt.fromisoformat(received_at).timestamp()
        except (ValueError, TypeError):
            continue
        b = int(ts // tf) * tf
        x = buckets.get(b)
        if x is None:
            buckets[b] = {"t": b * 1000, "o": lp, "h": lp, "l": lp, "c": lp,
                          "v0": vol or 0, "v1": vol or 0, "n": 1}
            order.append(b)
        else:
            if lp > x["h"]: x["h"] = lp
            if lp < x["l"]: x["l"] = lp
            x["c"] = lp
            if vol is not None: x["v1"] = vol
            x["n"] += 1

    rows_out = []
    for b in order[-limit:]:
        x = buckets[b]
        rng = round(x["h"] - x["l"], 2)
        move = round(x["c"] - x["o"], 2)
        move_pct = round((move / x["o"]) * 100, 3) if x["o"] else 0.0
        rows_out.append({
            "t": x["t"], "o": x["o"], "h": x["h"], "l": x["l"], "c": x["c"],
            "range": rng, "move": move, "move_pct": move_pct,
            "volume": max(0, (x["v1"] or 0) - (x["v0"] or 0)),
            "changes": x["n"],
        })

    # session totals
    tot_vol = sum(r["volume"] for r in rows_out)
    tot_chg = sum(r["changes"] for r in rows_out)
    day_hi = max((r["h"] for r in rows_out), default=None)
    day_lo = min((r["l"] for r in rows_out), default=None)
    return {
        "stat": "Ok", "tsym": tsym, "tf": tf, "day": str(dd),
        "rows": list(reversed(rows_out)),   # newest first for table
        "summary": {
            "buckets": len(rows_out), "total_volume": tot_vol, "total_changes": tot_chg,
            "day_high": day_hi, "day_low": day_lo,
            "day_range": round(day_hi - day_lo, 2) if (day_hi and day_lo) else None,
        },
    }


# ---------- Live WebSocket price stream ----------
import db as _db
PH = _db.PLACE
COL_CHANGE = _db.quote_col("change")


def _fetch_price_rows_after(last_id: int, tsym: Optional[str] = None, limit: int = 50):
    """Return new price_changes rows with id > last_id, oldest-first."""
    sql = ("SELECT id, received_at, bar_time, tsym, lp, bid, ask, bid_size, ask_size, "
           f"{COL_CHANGE} as `change`, change_pct, volume, source FROM price_changes WHERE id > " + PH)
    params: list = [last_id]
    if tsym:
        sql += f" AND tsym = {PH}"
        params.append(tsym)
    sql += f" ORDER BY id ASC LIMIT {PH}"
    params.append(limit)
    try:
        conn = _db.connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = _db.dict_rows(cur)
            cur.close()
            return rows
        finally:
            conn.close()
    except Exception:
        return []


def _fetch_recent_rows(tsym: Optional[str], n: int):
    """Return the most-recent n price_changes rows, oldest-first (for backlog)."""
    n = max(1, min(int(n), 5000))
    sql = ("SELECT id, received_at, bar_time, tsym, lp, bid, ask, bid_size, ask_size, "
           f"{COL_CHANGE} as `change`, change_pct, volume, source FROM price_changes")
    params: list = []
    if tsym:
        sql += f" WHERE tsym = {PH}"
        params.append(tsym)
    sql += f" ORDER BY id DESC LIMIT {PH}"   # newest first...
    params.append(n)
    try:
        conn = _db.connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = _db.dict_rows(cur)
            cur.close()
            return list(reversed(rows))      # ...then flip to oldest-first
        finally:
            conn.close()
    except Exception:
        return []


def _latest_price_id() -> int:
    try:
        conn = _db.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT MAX(id) FROM price_changes")
            row = cur.fetchone()
            cur.close()
            return (row[0] or 0) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


@app.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket):
    """
    Streams every new price change to the client as it is recorded.
    Optional query param ?tsym=RELIANCE-EQ to filter, ?backlog=N to send last N first.
    """
    await websocket.accept()
    tsym = websocket.query_params.get("tsym")
    try:
        backlog = int(websocket.query_params.get("backlog", "30"))
    except ValueError:
        backlog = 30

    # 1. Send a backlog of the genuinely most-recent rows so the UI isn't empty
    last_id = 0
    if backlog > 0:
        recent = _fetch_recent_rows(tsym, backlog)
        if recent:
            last_id = recent[-1]["id"]
            await websocket.send_json({"type": "backlog", "rows": recent})
        else:
            last_id = _latest_price_id()
    else:
        last_id = _latest_price_id()

    # 2. Poll for new rows and push them live
    try:
        while True:
            rows = _fetch_price_rows_after(last_id, tsym=tsym, limit=100)
            if rows:
                last_id = rows[-1]["id"]
                await websocket.send_json({"type": "update", "rows": rows})
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass


# ---------- Upstox candle data endpoints ----------

class UpstoxFetchModel(BaseModel):
    tsym:      str
    interval:  Optional[str] = "1d"
    from_date: Optional[str] = None
    to_date:   Optional[str] = None


def _upstox() -> UpstoxClient:
    global upstox
    if upstox is None:
        upstox = UpstoxClient()
    return upstox


@app.get("/api/upstox/status")
def upstox_status():
    """Check if Upstox is configured and logged in."""
    return _upstox().status()


@app.get("/api/upstox/login_url")
def upstox_login_url(redirect: int = 1):
    """
    Sends you to the Upstox login page. By default (redirect=1) the browser is
    bounced straight to Upstox so you just log in — no JSON to copy. Pass
    ?redirect=0 to get the raw URL as JSON instead.
    After approving, Upstox redirects back to /api/upstox/callback automatically.
    """
    u = _upstox()
    if not u.has_creds():
        raise HTTPException(
            status_code=400,
            detail="UPSTOX_API_KEY and UPSTOX_API_SECRET are not set in backend/.env"
        )
    url = u.login_url()
    if redirect:
        return RedirectResponse(url)
    return {
        "login_url": url,
        "instruction": "Open this URL in your browser, log in with your Upstox account, then approve access.",
    }


@app.post("/api/upstox/autologin")
@app.get("/api/upstox/autologin")
def upstox_do_autologin():
    """Run the automated TOTP login and save a fresh token. Called by the daily
    cron (and on startup). Returns the new login status."""
    import upstox_autologin
    if not upstox_autologin.configured():
        return {"ok": False, "error": "auto-login not configured (set UPSTOX_USERNAME/PASSWORD/PIN_CODE/TOTP_SECRET in .env)"}
    res = upstox_autologin.get_token()
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    u = _upstox()
    u._save_token(res["access_token"])
    global feed
    if feed is None:
        feed = UpstoxQuoteFeed(u, interval_sec=1.0)
        feed.start()
    return {"ok": True, "user": res.get("user"), "logged_in": True}


@app.get("/api/upstox/callback")
def upstox_callback(code: Optional[str] = None, error: Optional[str] = None):
    """
    OAuth2 redirect target. Upstox sends the authorization code here automatically.
    Exchanges the code for an access token and saves it for the day.
    """
    if error:
        return HTMLResponse(f"<h2>Upstox login failed</h2><p>{error}</p>", status_code=400)
    if not code:
        return HTMLResponse("<h2>No code received</h2><p>Something went wrong with the OAuth flow.</p>", status_code=400)

    res = _upstox().exchange_code(code)
    if res.get("ok"):
        return HTMLResponse("""
        <html><body style="font-family:system-ui;background:#07090d;color:#e5ecf5;
               display:grid;place-items:center;height:100vh;margin:0">
          <div style="text-align:center">
            <h2 style="color:#00d68f">Connected to Upstox</h2>
            <p>Access token saved. You can close this tab.</p>
            <a href="/live.html" style="color:#5e8eff">Go to dashboard</a>
          </div>
        </body></html>""")
    return HTMLResponse(f"<h2>Login failed</h2><p>{res.get('error')}</p>", status_code=400)


@app.post("/api/upstox/fetch")
def upstox_fetch(req: UpstoxFetchModel):
    """
    Fetch historical OHLCV candles for a symbol and store in local SQLite DB.

    interval options: 1m, 5m, 15m, 1h, 4h, 30m, 1d, 1w, 1mo
    Example body: {"tsym": "RELIANCE", "interval": "1d"}
    """
    return _upstox().fetch_candles(
        tsym=req.tsym,
        interval=req.interval,
        from_date=req.from_date,
        to_date=req.to_date,
    )


@app.get("/api/upstox/candles")
def upstox_candles(tsym: str, interval: str = "1d", limit: int = 500,
                   auto: int = 1, refresh: int = 0, today: int = 0):
    """
    Read stored candles for a symbol/interval. Oldest-first.

    auto=1   : if nothing is stored yet, fetch full history from Upstox.
    refresh=1: re-fetch full history + today before returning.
    today=1  : cheap — fetch only today's intraday candles and upsert them
               (used for live polling so the forming candle updates).

    Each candle is {t, o, h, l, c, v, oi} where t is a millisecond epoch —
    matching the shape the chart already renders.
    """
    from upstox_client import UpstoxClient as _UC
    rows = _UC.query(tsym=tsym, interval=interval, limit=limit)

    fetched = None
    if refresh or (auto and not rows):
        fetched = _upstox().fetch_candles(tsym=tsym, interval=interval)
        rows = _UC.query(tsym=tsym, interval=interval, limit=limit)
    elif today:
        fetched = _upstox().fetch_candles(tsym=tsym, interval=interval, today_only=True)
        rows = _UC.query(tsym=tsym, interval=interval, limit=limit)

    # query() returns {ts(ms), o, h, l, c, v, oi}; the chart wants `t`.
    candles = [{"t": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"],
                "c": r["c"], "v": r["v"], "oi": r["oi"]} for r in rows]

    resp = {"stat": "Ok", "tsym": tsym.upper(), "interval": interval, "candles": candles}
    if fetched is not None and not fetched.get("ok"):
        resp["fetch_error"] = fetched.get("error")
    return resp


@app.get("/api/smc/multi")
def smc_multi(tsym: str, intervals: str = "1m,15m,1h,4h", limit: int = 600,
              swing: int = 2, recent: int = 150):
    """Run SMC on multiple timeframes — auto-fetches any missing intervals.
    `recent` slices each TF to the most-recent N candles before analyzing
    (sharper swings + zones). 0 = use the full window.
    Returns {tsym, by_tf: {interval: <analyze result>, ...}}."""
    from upstox_client import UpstoxClient as _UC
    import smc_engine

    out = {}
    sw = max(1, min(int(swing), 5))
    rn = max(0, min(int(recent), 5000))
    for iv in [x.strip() for x in intervals.split(",") if x.strip()]:
        rows = _UC.query(tsym=tsym, interval=iv, limit=limit)
        if not rows:
            _upstox().fetch_candles(tsym=tsym, interval=iv)
            rows = _UC.query(tsym=tsym, interval=iv, limit=limit)
        candles = [{"t": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"],
                    "c": r["c"], "v": r["v"]} for r in rows]
        out[iv] = smc_engine.analyze(candles, swing_lookback=sw, recent_n=rn)
    return {"tsym": tsym.upper(), "by_tf": out, "recent_n": rn}


@app.get("/api/smc")
def smc_analyze(tsym: str, interval: str = "15m", limit: int = 500, auto: int = 1,
                swing: int = 2, recent: int = 150):
    """
    Run Smart Money Concepts analysis on a symbol/interval.
    Auto-fetches Upstox candles if none are stored yet.
    Returns swings, BOS/CHoCH structure, FVGs, order blocks, liquidity sweeps,
    premium/discount range + OTE, and live alert signals.
    """
    from upstox_client import UpstoxClient as _UC
    import smc_engine

    rows = _UC.query(tsym=tsym, interval=interval, limit=limit)
    if auto and not rows:
        _upstox().fetch_candles(tsym=tsym, interval=interval)
        rows = _UC.query(tsym=tsym, interval=interval, limit=limit)

    candles = [{"t": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"],
                "c": r["c"], "v": r["v"]} for r in rows]
    rn = max(0, min(int(recent), 5000))
    result = smc_engine.analyze(candles,
                                swing_lookback=max(1, min(int(swing), 5)),
                                recent_n=rn)
    # Use the same recent window for live signals so alerts fire on the
    # same candles the chart highlights.
    sig_window = candles[-rn:] if (rn > 0 and len(candles) > rn) else candles
    result["signals"] = smc_engine.live_signals(sig_window, result)
    result["tsym"] = tsym.upper()
    result["interval"] = interval
    result["recent_n"] = rn
    return result


@app.get("/api/upstox/symbols")
def upstox_symbols():
    """List all symbols and intervals currently stored in the candles table."""
    from upstox_client import UpstoxClient as _UC
    return {"stat": "Ok", "symbols": _UC.stored_symbols()}


@app.post("/api/upstox/refresh_instruments")
def upstox_refresh_instruments():
    """
    Download the latest NSE instruments master from Upstox CDN.
    Run this once after setup, or whenever new stocks are listed.
    """
    return _upstox().refresh_instruments()


@app.get("/api/upstox/intervals")
def upstox_intervals():
    """List all supported candle intervals."""
    return {"intervals": ALL_INTERVALS}


# ---------- Root handler: redirect to /live.html ----------
@app.get("/")
def root():
    """Redirect to the live dashboard."""
    return RedirectResponse(url="/live.html", status_code=302)


# Serve static dashboard files (live.html etc.)
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    # Start on port 8000
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True, reload_dirs=[backend_dir])

