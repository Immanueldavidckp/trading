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

from shoonya_client import ShoonyaClient
from ai_agent import AIAgent
from tick_recorder import TickRecorder
from yahoo_poller import YahooPoller, YahooMultiPoller
from flattrade_client import FlattradeClient
from fastapi.responses import RedirectResponse, HTMLResponse

app = FastAPI(title="Shoonya AI Trading Backend")

# Enable CORS for React frontend (Vite defaults to 5173 or 5174)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For local ease, we'll allow all. Can restrict to http://localhost:5173
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

# Pydantic schemas for request validation
class CredentialsModel(BaseModel):
    user_id: str
    password: str
    vendor_code: str
    api_key: str
    totp_secret: str
    imei: Optional[str] = "abc1234"
    gemini_api_key: Optional[str] = ""
    mock_mode: Optional[bool] = True

class OrderModel(BaseModel):
    exch: str
    tsym: str
    qty: int
    prc: float
    trantype: str # B / S
    prctyp: str # MKT / LMT
    prd: str # I / C
    token: Optional[str] = None

class CancelOrderModel(BaseModel):
    orderno: str

# In-memory singletons
client = None
ai_agent = None
recorder: Optional[TickRecorder] = None
yahoo: Optional[YahooMultiPoller] = None
flattrade: Optional[FlattradeClient] = None

# Default watchlist for tick recorder. Add more here as you go.
DEFAULT_WATCHLIST = [
    {"exch": "NSE", "token": "2885", "tsym": "RELIANCE-EQ"},
]


class RecorderStartModel(BaseModel):
    symbols: Optional[List[dict]] = None  # [{"exch":"NSE","token":"...","tsym":"..."}]


class YahooStartModel(BaseModel):
    tsym: Optional[str] = "RELIANCE-EQ"
    interval_sec: Optional[float] = 1.0


class SymbolAddModel(BaseModel):
    query: str  # stock name or symbol, e.g. "ola electric", "TCS", "INFY"


class SymbolRemoveModel(BaseModel):
    tsym: str

def get_initial_credentials():
    """
    Attempts to load credentials from:
    1. Environment variables (.env file)
    2. config.json
    3. Fallback to shoonya_app.py in root folder
    4. Empty defaults
    """
    # 1. Try environment variables (.env)
    env_creds = {
        "user_id": os.environ.get("SHOONYA_USER_ID", ""),
        "password": os.environ.get("SHOONYA_PASSWORD", ""),
        "vendor_code": os.environ.get("SHOONYA_VENDOR_CODE", ""),
        "api_key": os.environ.get("SHOONYA_API_KEY", ""),
        "totp_secret": os.environ.get("SHOONYA_TOTP_SECRET", ""),
        "imei": os.environ.get("SHOONYA_IMEI", "abc1234"),
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        "mock_mode": os.environ.get("MOCK_MODE", "true").lower() in ("true", "1", "yes"),
    }
    if all(env_creds[k] for k in ["user_id", "password", "vendor_code", "api_key", "totp_secret"]):
        return env_creds

    # 2. Try config.json
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                # Let env override gemini_api_key if .env provides it
                if env_creds["gemini_api_key"] and not cfg.get("gemini_api_key"):
                    cfg["gemini_api_key"] = env_creds["gemini_api_key"]
                return cfg
        except Exception:
            pass

    # 2. Try to parse from shoonya_app.py
    shoonya_app_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "shoonya_app.py")
    if os.path.exists(shoonya_app_path):
        try:
            creds = {}
            with open(shoonya_app_path, "r") as f:
                lines = f.readlines()
                for line in lines:
                    if "SHOONYA_USER_ID =" in line:
                        creds["user_id"] = line.split("=")[1].strip().strip('"').strip("'")
                    elif "SHOONYA_PASSWORD =" in line:
                        creds["password"] = line.split("=")[1].strip().strip('"').strip("'")
                    elif "SHOONYA_VENDOR_CODE =" in line:
                        creds["vendor_code"] = line.split("=")[1].strip().strip('"').strip("'")
                    elif "SHOONYA_IMEI =" in line:
                        creds["imei"] = line.split("=")[1].strip().strip('"').strip("'")
                    elif "SHOONYA_API_KEY =" in line:
                        creds["api_key"] = line.split("=")[1].strip().strip('"').strip("'")
                    elif "SHOONYA_TOTP_SECRET =" in line:
                        creds["totp_secret"] = line.split("=")[1].strip().strip('"').strip("'")
            
            # Add defaults for other fields
            creds["gemini_api_key"] = os.environ.get("GEMINI_API_KEY", "")
            creds["mock_mode"] = True # Default to paper trading for safety
            
            if all(k in creds for k in ["user_id", "password", "vendor_code", "api_key", "totp_secret"]):
                # Save to config.json for future use
                with open(CONFIG_FILE, "w") as f:
                    json.dump(creds, f, indent=4)
                return creds
        except Exception as e:
            print(f"Error parsing shoonya_app.py credentials: {e}")

    # 3. Default empty settings
    return {
        "user_id": "",
        "password": "",
        "vendor_code": "",
        "api_key": "",
        "totp_secret": "",
        "imei": "abc1234",
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        "mock_mode": True
    }

# Initialize singletons on startup
@app.on_event("startup")
def startup_event():
    global client, ai_agent, yahoo, flattrade
    # Flattrade — OAuth based (no password stored server-side)
    try:
        flattrade = FlattradeClient()
        print(
            f"Flattrade: configured={flattrade.has_creds()} "
            f"logged_in={bool(flattrade.session_token)} "
            f"as={flattrade.uname or '(not logged in)'}"
        )
    except Exception as e:
        print(f"Flattrade init failed: {e}")
    creds = get_initial_credentials()
    client = ShoonyaClient(creds)
    ai_agent = AIAgent(creds.get("gemini_api_key"))

    # Auto-login if we have session info or are in mock mode
    try:
        client.login()
    except Exception as e:
        print(f"Auto-login failed: {e}")

    # Auto-start the free Yahoo multi-symbol price poller so data always flows
    try:
        yahoo = YahooMultiPoller(interval_sec=1.0)
        res = yahoo.start()
        print(f"Yahoo multi-poller auto-start: {res.get('ok')} (symbols={[s['tsym'] for s in res.get('symbols', [])]})")
    except Exception as e:
        print(f"Yahoo poller auto-start failed: {e}")

@app.get("/api/config")
def get_config():
    """Returns current configuration (with sensitive data masked)"""
    creds = get_initial_credentials()
    # Mask credentials
    masked = creds.copy()
    if masked.get("password"):
        masked["password"] = "********"
    if masked.get("api_key"):
        masked["api_key"] = "********"
    if masked.get("totp_secret"):
        masked["totp_secret"] = "********"
    if masked.get("gemini_api_key"):
        masked["gemini_api_key"] = "********" if len(masked["gemini_api_key"]) > 4 else ""
    return masked

@app.post("/api/config")
def update_config(creds: CredentialsModel):
    """Updates config.json and re-initializes clients"""
    global client, ai_agent
    
    # Read current config to restore masked fields if user didn't change them
    current = get_initial_credentials()
    
    save_data = creds.dict()
    
    # If the user sent masked placeholders, preserve the original value
    if save_data["password"] == "********":
        save_data["password"] = current.get("password", "")
    if save_data["api_key"] == "********":
        save_data["api_key"] = current.get("api_key", "")
    if save_data["totp_secret"] == "********":
        save_data["totp_secret"] = current.get("totp_secret", "")
    if save_data["gemini_api_key"] == "********":
        save_data["gemini_api_key"] = current.get("gemini_api_key", "")

    # Save to file
    with open(CONFIG_FILE, "w") as f:
        json.dump(save_data, f, indent=4)
        
    # Re-initialize
    client = ShoonyaClient(save_data)
    ai_agent.set_api_key(save_data.get("gemini_api_key"))
    
    # Perform login
    login_res = client.login()
    
    return {
        "success": login_res.get("stat") == "Ok",
        "login_status": login_res
    }

@app.post("/api/login")
def login_shoonya():
    """Triggers login with Shoonya and returns status"""
    if not client:
        raise HTTPException(status_code=500, detail="Client not initialized")
    res = client.login()
    if res.get("stat") == "Ok":
        return {"success": True, "message": "Login successful", "details": res}
    return {"success": False, "message": res.get("emsg", "Login failed")}

@app.get("/api/search")
def search_stock(q: str, exch: Optional[str] = "NSE"):
    """Search for stock scrips"""
    if not client:
        raise HTTPException(status_code=500, detail="Client not initialized")
    if not q or len(q) < 2:
        return {"stat": "Ok", "values": []}
    res = client.search_scrip(exch, q)
    return res

@app.get("/api/quote")
def get_quote(exch: str, token: str):
    """Get quote details for a symbol"""
    if not client:
        raise HTTPException(status_code=500, detail="Client not initialized")
    res = client.get_quotes(exch, token)
    return res

@app.get("/api/candles")
def get_candles(exch: str, token: str, interval: Optional[int] = 5, days: Optional[int] = 3):
    """Fetch candles and calculate technical indicators"""
    if not client or not ai_agent:
        raise HTTPException(status_code=500, detail="Client not initialized")
    
    # 1. Fetch candles
    candles_res = client.get_candles(exch, token, interval, days)
    if candles_res.get("stat") != "Ok":
        return candles_res
        
    candles = candles_res.get("candles", [])
    
    # 2. Calculate indicators
    df = ai_agent.calculate_indicators(candles)
    
    # 3. Format result for chart
    chart_data = []
    if df is not None:
        for _, row in df.iterrows():
            chart_data.append({
                "time": row["time"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "sma_20": row["sma_20"],
                "ema_50": row["ema_50"],
                "rsi_14": row["rsi_14"],
                "macd": row["macd"],
                "macd_signal": row["macd_signal"],
                "macd_hist": row["macd_hist"]
            })
    else:
        # Fallback to raw candles if indicators fail
        for c in candles:
            chart_data.append({
                "time": c.get("time"),
                "open": float(c.get("into", 0)),
                "high": float(c.get("inth", 0)),
                "low": float(c.get("intl", 0)),
                "close": float(c.get("intc", 0)),
                "volume": float(c.get("v", 0)),
                "sma_20": None,
                "ema_50": None,
                "rsi_14": None,
                "macd": None,
                "macd_signal": None,
                "macd_hist": None
            })
            
    return {"stat": "Ok", "candles": chart_data}

@app.get("/api/ai-analyze")
def analyze_stock_ai(exch: str, token: str, interval: Optional[int] = 5):
    """Run AI Technical Analysis on selected scrip"""
    if not client or not ai_agent:
        raise HTTPException(status_code=500, detail="Client not initialized")
        
    # 1. Get current quote
    quote = client.get_quotes(exch, token)
    if quote.get("stat") != "Ok":
        return {"stat": "Not_Ok", "emsg": f"Failed to get quote: {quote.get('emsg')}"}
        
    # 2. Get recent historical candles
    candles_res = client.get_candles(exch, token, interval=interval, days=3)
    if candles_res.get("stat") != "Ok":
        return {"stat": "Not_Ok", "emsg": f"Failed to get candles: {candles_res.get('emsg')}"}
        
    candles = candles_res.get("candles", [])
    
    # 3. Analyze
    analysis = ai_agent.analyze_stock(quote, candles)
    return analysis

@app.post("/api/order")
def place_order(order: OrderModel):
    """Places an order (Live or Paper trading depending on config)"""
    if not client:
        raise HTTPException(status_code=500, detail="Client not initialized")
    
    res = client.place_order(
        exchange=order.exch,
        tsym=order.tsym,
        qty=order.qty,
        prc=order.prc,
        trantype=order.trantype,
        prctyp=order.prctyp,
        prd=order.prd,
        token=order.token
    )
    return res

@app.post("/api/order/cancel")
def cancel_order(req: CancelOrderModel):
    """Cancels a pending order"""
    if not client:
        raise HTTPException(status_code=500, detail="Client not initialized")
    res = client.cancel_order(req.orderno)
    return res

@app.get("/api/portfolio")
def get_portfolio():
    """Fetches user orders, net positions, holdings and cash balance"""
    if not client:
        raise HTTPException(status_code=500, detail="Client not initialized")
        
    orders = client.get_order_book()
    positions = client.get_positions()
    holdings = client.get_holdings()
    balance = client.get_balance()
    
    return {
        "stat": "Ok",
        "orders": orders,
        "positions": positions,
        "holdings": holdings,
        "balance": balance,
        "mock_mode": client.mock_mode
    }

# ---------- Tick Recorder Endpoints ----------
@app.post("/api/recorder/start")
def recorder_start(req: RecorderStartModel):
    """
    Start streaming ticks from Shoonya WebSocket into local SQLite DB.
    Requires MOCK_MODE=false and a successful Shoonya login.
    """
    global recorder
    if not client:
        raise HTTPException(status_code=500, detail="Client not initialized")
    if recorder and recorder.running:
        return {"ok": False, "error": "Recorder already running", "status": recorder.status()}

    symbols = req.symbols or DEFAULT_WATCHLIST
    recorder = TickRecorder(client, symbols)
    return recorder.start()


@app.post("/api/recorder/stop")
def recorder_stop():
    global recorder
    if not recorder:
        return {"ok": False, "error": "Recorder was never started"}
    return recorder.stop()


@app.get("/api/recorder/status")
def recorder_status():
    if not recorder:
        return {"running": False, "tick_count": 0, "symbols": DEFAULT_WATCHLIST, "note": "Recorder not started yet"}
    return recorder.status()


@app.get("/api/ticks")
def ticks_query(tsym: Optional[str] = None, limit: int = 100, since: Optional[str] = None):
    """Read recent ticks from local SQLite store."""
    return {"stat": "Ok", "rows": TickRecorder.query_ticks(tsym=tsym, limit=limit, since=since)}


@app.get("/api/ticks/stats")
def ticks_stats():
    """Summary of stored ticks: total count, per-symbol counts, time range."""
    return TickRecorder.stats()


# ---------- Yahoo Finance Multi-Symbol Price Poller (free, no broker) ----------
def _ensure_yahoo():
    global yahoo
    if yahoo is None:
        yahoo = YahooMultiPoller(interval_sec=1.0)
    return yahoo


@app.post("/api/yahoo/start")
def yahoo_start(req: YahooStartModel):
    """Start the multi-symbol poller; records a row each time any tracked price changes."""
    y = _ensure_yahoo()
    if y.running:
        return {"ok": False, "error": "Poller already running", "status": y.status()}
    return y.start()


@app.post("/api/yahoo/stop")
def yahoo_stop():
    if not yahoo:
        return {"ok": False, "error": "Poller never started"}
    return yahoo.stop()


@app.get("/api/yahoo/status")
def yahoo_status():
    if not yahoo:
        return {"running": False, "note": "Poller not started yet"}
    return yahoo.status()


# ----- Watchlist (stock) management -----
@app.get("/api/yahoo/symbols")
def yahoo_symbols():
    """List the stocks currently being tracked."""
    return {"stat": "Ok", "symbols": _ensure_yahoo().list_symbols()}


@app.post("/api/yahoo/symbols/add")
def yahoo_symbols_add(req: SymbolAddModel):
    """Add a stock by name or symbol (auto-resolved via Yahoo search). e.g. 'ola electric', 'TCS'."""
    return _ensure_yahoo().add_symbol(req.query)


@app.post("/api/yahoo/symbols/remove")
def yahoo_symbols_remove(req: SymbolRemoveModel):
    """Remove a tracked stock by its tsym label."""
    return _ensure_yahoo().remove_symbol(req.tsym)


@app.post("/api/yahoo/backfill")
def yahoo_backfill(tsym: str = "RELIANCE-EQ", rng: str = "1d"):
    """Backfill the full 1-minute price series for a tracked symbol (1d, 5d, 1mo)."""
    return _ensure_yahoo().backfill(tsym=tsym, rng=rng)


@app.get("/api/yahoo/prices")
def yahoo_prices(tsym: Optional[str] = None, limit: int = 100):
    """Read recorded price changes from local SQLite store."""
    return {"stat": "Ok", "rows": YahooPoller.query(tsym=tsym, limit=limit)}


@app.get("/api/yahoo/stats")
def yahoo_stats():
    return YahooPoller.stats()


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

    # 1. Send a backlog of the most recent rows so the UI isn't empty
    last_id = 0
    if backlog > 0:
        recent = _fetch_price_rows_after(0, tsym=tsym, limit=10_000)
        recent = recent[-backlog:]
        if recent:
            last_id = recent[-1]["id"]
            await websocket.send_json({"type": "backlog", "rows": recent})
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


# ---------- Flattrade OAuth + trading endpoints ----------
class FtOrderModel(BaseModel):
    exch: str = "NSE"        # NSE / BSE / NFO / MCX
    tsym: str                # e.g. "RELIANCE-EQ"
    qty: int
    prc: float = 0.0         # 0 for MKT orders
    trantype: str            # "B" buy / "S" sell
    prctyp: str = "MKT"      # MKT / LMT / SL-LMT / SL-MKT
    prd: str = "C"           # C = CNC delivery, I = MIS intraday
    ret: str = "DAY"
    confirm: bool = False    # Must be true to actually place — safety gate


class FtCancelModel(BaseModel):
    orderno: str


class FtSetTokenModel(BaseModel):
    token: str
    api_secret: str  # must match server's secret — acts as auth for this endpoint


@app.get("/api/flattrade/status")
def ft_status():
    if not flattrade:
        return {"configured": False, "logged_in": False, "error": "Client not initialized"}
    return flattrade.status()


@app.get("/api/flattrade/login_url")
def ft_login_url():
    if not flattrade or not flattrade.has_creds():
        raise HTTPException(status_code=400, detail="Flattrade not configured on server")
    return {"login_url": flattrade.login_url()}


@app.get("/api/flattrade/logout")
def ft_logout():
    if flattrade:
        flattrade.logout()
    return {"ok": True}


@app.post("/api/flattrade/set_token")
def ft_set_token(req: FtSetTokenModel):
    """
    Accept a session token exchanged on a local (Indian) machine.
    Flattrade's apitoken endpoint rejects datacenter IPs (INVALID_IP),
    so the exchange happens locally and the token is pushed here.
    Authorized by requiring the matching API secret.
    """
    if not flattrade:
        raise HTTPException(status_code=500, detail="Client not initialized")
    if req.api_secret != flattrade.api_secret:
        raise HTTPException(status_code=403, detail="Bad api_secret")
    return flattrade.set_token(req.token)


@app.get("/api/flattrade/user")
def ft_user():
    if not flattrade or not flattrade.session_token:
        raise HTTPException(status_code=401, detail="Not logged in")
    return flattrade.user_details()


@app.get("/api/flattrade/portfolio")
def ft_portfolio():
    if not flattrade or not flattrade.session_token:
        raise HTTPException(status_code=401, detail="Not logged in")
    return {
        "stat": "Ok",
        "orders":    flattrade.get_orderbook(),
        "positions": flattrade.get_positions(),
        "holdings":  flattrade.get_holdings(),
        "limits":    flattrade.get_limits(),
        "trades":    flattrade.get_tradebook(),
    }


@app.get("/api/flattrade/quote")
def ft_quote(exch: str, token: str):
    if not flattrade or not flattrade.session_token:
        raise HTTPException(status_code=401, detail="Not logged in")
    return flattrade.get_quote(exch, token)


@app.get("/api/flattrade/search")
def ft_search(q: str, exch: str = "NSE"):
    if not flattrade or not flattrade.session_token:
        raise HTTPException(status_code=401, detail="Not logged in")
    return flattrade.search_scrip(exch, q)


@app.post("/api/flattrade/order")
def ft_place_order(order: FtOrderModel):
    if not flattrade or not flattrade.session_token:
        raise HTTPException(status_code=401, detail="Not logged in")
    # Safety: explicit confirm flag required to actually place a REAL order.
    if not order.confirm:
        return {
            "stat": "Not_Ok",
            "emsg": "Safety gate: pass confirm=true to actually place this order.",
            "preview": order.dict(),
        }
    res = flattrade.place_order(
        exch=order.exch, tsym=order.tsym, qty=order.qty, prc=order.prc,
        trantype=order.trantype, prctyp=order.prctyp, prd=order.prd, ret=order.ret,
    )
    return res


@app.post("/api/flattrade/cancel")
def ft_cancel(req: FtCancelModel):
    if not flattrade or not flattrade.session_token:
        raise HTTPException(status_code=401, detail="Not logged in")
    return flattrade.cancel_order(req.orderno)


# ---------- Root handler: OAuth callback OR redirect to /live.html ----------
_LOGGED_IN_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Flattrade login</title>
<style>body{{font-family:system-ui,sans-serif;background:#07090d;color:#e5ecf5;
margin:0;display:grid;place-items:center;height:100vh}}
.box{{background:#12161f;border:1px solid #232a38;border-radius:14px;padding:32px 36px;max-width:480px}}
h1{{margin:0 0 12px;font-size:22px}}
.ok{{color:#00d68f}}.err{{color:#ff5677}}
a{{color:#5e8eff;text-decoration:none}}
.kv{{font-family:'JetBrains Mono',monospace;font-size:13px;color:#9ba6b8;margin-top:14px;line-height:1.7}}
</style></head><body><div class="box">
<h1 class="{cls}">{title}</h1>
<div>{msg}</div>
<div class="kv">{details}</div>
<p style="margin-top:24px"><a href="/live.html">→ Go to live dashboard</a></p>
</div></body></html>"""


@app.get("/")
def root(code: Optional[str] = None, client: Optional[str] = None):
    """
    If Flattrade redirects here with ?code=...&client=..., exchange for token.
    Otherwise redirect to the live dashboard.
    """
    if code:
        if not flattrade or not flattrade.has_creds():
            return HTMLResponse(_LOGGED_IN_HTML.format(
                cls="err", title="✗ Flattrade not configured on server",
                msg="Backend is missing FLATTRADE_API_KEY / FLATTRADE_API_SECRET.",
                details="",
            ), status_code=500)
        res = flattrade.exchange_code(code)
        if res.get("ok"):
            return HTMLResponse(_LOGGED_IN_HTML.format(
                cls="ok",
                title="✓ Connected to Flattrade",
                msg=f"You're logged in as <b>{flattrade.uname or flattrade.user_id}</b>.",
                details=(
                    f"Account ID: {flattrade.actid or '-'}<br>"
                    f"Broker: {flattrade.broker or '-'}<br>"
                    f"Logged in at: {flattrade.logged_in_at}"
                ),
            ))
        err = res.get("error", "Token exchange failed")
        hint = ""
        if "INVALID_IP" in str(err):
            hint = (
                "Flattrade blocks token exchange from server IPs.<br>"
                "<b>Fix:</b> copy the FULL URL from your address bar (it contains "
                "<code>?code=...</code>) and run <code>python flattrade_local_login.py</code> "
                "on your PC — or paste the URL to Claude. Codes expire in ~2 minutes."
            )
        return HTMLResponse(_LOGGED_IN_HTML.format(
            cls="err",
            title="✗ Login failed",
            msg=err,
            details=hint or f"Error details: {res}",
        ), status_code=400)
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

