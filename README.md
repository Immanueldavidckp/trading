# Trading — Live Price Capture + AI Trade Copilot

A FastAPI backend + zero-dependency web UI that captures **live stock price changes (with best bid / best ask)** to a local SQLite database, plus a Shoonya broker integration for actual trading.

Built around two free data pipelines:

1. **Yahoo Finance poller** — works everywhere, gives best bid + best ask, ~10–12 sec updates, ~15 min delayed in live market. Auto-starts on boot.
2. **Shoonya WebSocket tick recorder** — true tick-by-tick + 5-level depth (requires a Shoonya account and the server being up).

See **[PROJECT_LOG.txt](./PROJECT_LOG.txt)** for the full build log, design decisions, and known limits.

## Quick start

```bash
# 1. Install deps
cd backend
pip install -r requirements.txt

# 2. Configure
cp .env.example .env       # fill in your Shoonya creds (or leave blank for paper mode)

# 3. Run
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Then open:
- **http://127.0.0.1:8000/live.html** — live price + bid/ask page (add/remove stocks from UI)
- **http://127.0.0.1:8000/** — original full trading dashboard

## Features

- Multi-stock watchlist, persisted to `local_data/watchlist.json`
- Add stocks by name OR symbol (Yahoo search auto-resolves: `ola electric` → `OLAELEC.NS`)
- Records a row to SQLite whenever `lp`, `bid`, or `ask` changes
- Millisecond-precision capture timestamps
- Live WebSocket stream to the browser (`/ws/prices?tsym=…`)
- 1-min historical backfill (1d / 5d / 1mo)
- Nightly day + swing plans over the **top 200 NSE names ≤₹300** by turnover
  (`/api/plan/build`, `/api/swing/build` — pass `background=1` and poll
  `…/build_status`, since a 200-symbol build takes minutes)
- Plans are grouped by **NSE macro sector** on the plan pages: sectors collapse
  to one row each, open one to list its stocks. Order is **entries first, then
  confluence score** — a stock with a live entry always outranks a
  higher-scoring one without. Sector tags come from NSE's index constituent
  lists (cached weekly in `local_data/sector_map.json`, `sectors.py`), with a
  bundled static map as the offline fallback
- AI trade recommendations via Gemini, with rule-based fallback (RSI/MACD/SMA/EMA)
- Paper trading mode (default) and live Shoonya mode

## Stack

- **Backend**: FastAPI · uvicorn · SQLite · python-dotenv · pandas · websocket-client
- **Frontend**: vanilla HTML/JS served from `backend/static/` (no build step). Optional Vite/React app in `frontend/`.
- **Data sources**: Yahoo Finance `v7/quote` (crumb-authed) for bid/ask · Shoonya NorenWS for ticks · Google Gemini for AI

## Project structure

```
backend/
  main.py               FastAPI app: REST + /ws/prices WebSocket
  yahoo_poller.py       Multi-symbol poller, Yahoo crumb client, SQLite writes
  tick_recorder.py      Shoonya WebSocket tick + 5-level depth capture
  shoonya_client.py     Shoonya REST trading client
  ai_agent.py           Gemini + rule-based analyzer
  universe.py           Nightly top-200 <=Rs.300 universe by turnover, sector-tagged
  sectors.py            Symbol -> NSE macro sector (live NSE lists + static fallback)
  build_jobs.py         Background runner for the long 200-symbol plan builds
  static/
    index.html          Original full trading dashboard
    live.html           Live price-change page (this is what you want)
  .env.example          Copy to .env and fill in
frontend/                React/Vite app (alternative UI — needs npm install)
PROJECT_LOG.txt          Full session build log
```

## Known limits

- Yahoo data is **~15 min delayed** during live market hours
- Yahoo gives best bid + best ask only (1 level), not 5-level depth
- Bid/ask **sizes** are usually 0/None for Indian stocks on Yahoo
- True tick-by-tick + 5-level depth requires the Shoonya WebSocket (or another broker)
- Microsecond-resolution tick data is not available from any free source on the internet
