"""
plan_pipeline.py — nightly orchestration:
  build_daily_plans(for_date)  : select universe → fetch candles → build a plan per
                                 stock → store in `daily_plans` (plan_date = target session).
  score_daily_plans(plan_date) : load that session's plans → replay vs actual candles
                                 → store per-stock results + a book scorecard.

Plans are stored as JSON. Nothing here places orders.
"""
from __future__ import annotations
from typing import List, Dict, Optional
import json
import datetime as _dt

import db as _db
import plan_engine
import plan_score
import universe as _universe

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


# ── schema ──────────────────────────────────────────────────────────────────

def ensure_tables():
    conn = _db.connect()
    try:
        cur = conn.cursor()
        txt = "LONGTEXT" if _db.USE_MYSQL else "TEXT"
        cur.execute(f"""CREATE TABLE IF NOT EXISTS daily_plans (
            plan_date   VARCHAR(10) NOT NULL,
            tsym        VARCHAR(40) NOT NULL,
            rank_n      INT,
            score       INT,
            conviction  VARCHAR(12),
            bias        VARCHAR(12),
            plan_json   {txt},
            created_at  VARCHAR(32),
            {'PRIMARY KEY (plan_date, tsym)' if _db.USE_MYSQL else 'PRIMARY KEY (plan_date, tsym)'}
        )""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS plan_results (
            plan_date    VARCHAR(10) NOT NULL,
            tsym         VARCHAR(40) NOT NULL,
            plan_correct INT,
            book_net_pct DOUBLE,
            result_json  {txt},
            created_at   VARCHAR(32),
            PRIMARY KEY (plan_date, tsym)
        )""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS plan_scorecard (
            plan_date  VARCHAR(10) NOT NULL,
            agg_json   {txt},
            created_at VARCHAR(32),
            PRIMARY KEY (plan_date)
        )""")
        conn.commit(); cur.close()
    finally:
        conn.close()


def _now_iso():
    return _dt.datetime.now(IST).isoformat(timespec="seconds")


def next_trading_day(d: _dt.date) -> _dt.date:
    nd = d + _dt.timedelta(days=1)
    while nd.weekday() >= 5:            # Sat/Sun → Monday (holidays handled by empty data)
        nd += _dt.timedelta(days=1)
    return nd


def prev_trading_day(d: _dt.date) -> _dt.date:
    pd = d - _dt.timedelta(days=1)
    while pd.weekday() >= 5:
        pd -= _dt.timedelta(days=1)
    return pd


def _ist_date(ms: int) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000, IST).strftime("%Y-%m-%d")


def _truncate(candles: List[dict], as_of: _dt.date) -> List[dict]:
    """Keep only candles on/before `as_of` (IST) — point-in-time, no look-ahead."""
    cut = as_of.isoformat()
    return [c for c in candles if _ist_date(c["t"]) <= cut]


# ── candle helpers ──────────────────────────────────────────────────────────

def _ensure_candles(tsym: str, interval: str, min_rows: int,
                    fresh_until: Optional[_dt.date] = None) -> List[dict]:
    """Return stored candles; fetch from Upstox when too few are stored OR the
    latest stored candle is older than `fresh_until` (so stocks with plentiful but
    STALE history get refreshed — this is what fixes weeks-old prev-day levels)."""
    from upstox_client import UpstoxClient as _UC
    rows = _UC.query(tsym, interval, limit=400)
    stale = bool(fresh_until and rows
                 and _ist_date(rows[-1]["ts"]) < fresh_until.isoformat())
    if len(rows) < min_rows or stale:
        try:
            from main import _upstox
            _upstox().fetch_candles(tsym=tsym, interval=interval)
        except Exception:
            pass
        rows = _UC.query(tsym, interval, limit=400)
    return [{"t": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"],
             "c": r["c"], "v": r["v"]} for r in rows]


def _intraday_for_day(tsym: str, day: _dt.date,
                      intervals=("15m", "5m")) -> List[dict]:
    """Actual intraday candles for one IST day (oldest-first). Tries each interval
    in order (15m is already stored by build → no extra fetch); fetches once if a
    tried interval isn't stored for that day yet."""
    from upstox_client import UpstoxClient as _UC
    start = _dt.datetime.combine(day, _dt.time(0, 0), IST)
    s_s = int(start.timestamp()); e_s = s_s + 86400
    for iv in intervals:
        rows = _UC.query(tsym, iv, limit=100000, from_ts=s_s, to_ts=e_s)
        if len(rows) < 3:
            try:
                from main import _upstox
                _upstox().fetch_candles(tsym=tsym, interval=iv)
            except Exception:
                pass
            rows = _UC.query(tsym, iv, limit=100000, from_ts=s_s, to_ts=e_s)
        if len(rows) >= 3:
            return [{"t": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"],
                     "c": r["c"], "v": r["v"]} for r in rows]
    return []


# ── build ───────────────────────────────────────────────────────────────────

def build_daily_plans(for_date: Optional[str] = None, top_n: int = 50) -> Dict:
    """Build plans for a target session. `for_date` (YYYY-MM-DD) overrides the
    target; default = next trading day after today (IST).

    Point-in-time discipline (§27.2): the plan uses only candles up to `as_of` =
    the session BEFORE the target. For the live case (target = tomorrow) that is
    today's close; for a backtest (target in the past) history is truncated to the
    prior session so there is NO look-ahead."""
    ensure_tables()
    today_ist = _dt.datetime.now(IST).date()
    target = (_dt.date.fromisoformat(for_date) if for_date
              else next_trading_day(today_ist))
    as_of = prev_trading_day(target) if target <= today_ist else today_ist

    uni = _universe.select_universe(as_of, top_n=top_n)
    built, skipped = [], []
    conn = _db.connect()
    try:
        cur = conn.cursor()
        PH = _db.PLACE
        for row in uni["rows"]:
            sym = row["sym"]
            try:
                daily = _truncate(_ensure_candles(sym, "1d", 20, fresh_until=as_of), as_of)
                if len(daily) < 20:
                    skipped.append({"sym": sym, "why": "insufficient daily candles"}); continue
                # Staleness guard: the prev-day levels must come from a candle at/near
                # the as_of session. If the latest available candle is >4 days stale,
                # the stock isn't trading normally — skip rather than show wrong levels.
                last_d = _dt.date.fromisoformat(_ist_date(daily[-1]["t"]))
                if (as_of - last_d).days > 4:
                    skipped.append({"sym": sym, "why": f"stale data (last {last_d})"}); continue
                intra = _truncate(_ensure_candles(sym, "15m", 20, fresh_until=as_of), as_of)
                plan = plan_engine.build_plan(sym, daily, intra)
                if not plan.get("ok"):
                    skipped.append({"sym": sym, "why": plan.get("error")}); continue
                plan["universe_rank"] = row.get("rank")
                plan["turnover_cr"] = row.get("turnover_cr")
                plan["plan_date"] = target.isoformat()
                cur.execute(
                    f"""INSERT INTO daily_plans
                        (plan_date,tsym,rank_n,score,conviction,bias,plan_json,created_at)
                        VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})
                        ON DUPLICATE KEY UPDATE score=VALUES(score),conviction=VALUES(conviction),
                        bias=VALUES(bias),plan_json=VALUES(plan_json),created_at=VALUES(created_at)"""
                    if _db.USE_MYSQL else
                    f"""INSERT OR REPLACE INTO daily_plans
                        (plan_date,tsym,rank_n,score,conviction,bias,plan_json,created_at)
                        VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})""",
                    [target.isoformat(), sym, row.get("rank"), plan.get("score"),
                     plan.get("conviction"), plan.get("bias"),
                     json.dumps(plan), _now_iso()])
                built.append(sym)
            except Exception as e:
                skipped.append({"sym": sym, "why": str(e)[:120]})
        conn.commit(); cur.close()
    finally:
        conn.close()
    return {"ok": True, "plan_date": target.isoformat(), "universe_source": uni["source"],
            "built": len(built), "skipped": len(skipped),
            "symbols": built, "skipped_detail": skipped[:20]}


# ── score ───────────────────────────────────────────────────────────────────

def score_daily_plans(plan_date: str) -> Dict:
    """Replay every plan whose plan_date == the given session against that day's
    actual candles; store per-stock results and the book scorecard."""
    ensure_tables()
    day = _dt.date.fromisoformat(plan_date)
    conn = _db.connect()
    try:
        cur = conn.cursor()
        PH = _db.PLACE
        cur.execute(f"SELECT tsym, plan_json FROM daily_plans WHERE plan_date={PH}", [plan_date])
        plans = [(r[0], json.loads(r[1])) for r in cur.fetchall()]
        if not plans:
            cur.close()
            return {"ok": False, "error": f"no plans stored for {plan_date}"}

        scored = []
        for sym, plan in plans:
            candles = _intraday_for_day(sym, day)
            if len(candles) < 3:
                continue
            sc = plan_score.score_plan(plan, candles)
            if not sc.get("ok"):
                continue
            scored.append(sc)
            summ = sc["summary"]
            cur.execute(
                f"""INSERT INTO plan_results (plan_date,tsym,plan_correct,book_net_pct,result_json,created_at)
                    VALUES ({PH},{PH},{PH},{PH},{PH},{PH})
                    ON DUPLICATE KEY UPDATE plan_correct=VALUES(plan_correct),
                    book_net_pct=VALUES(book_net_pct),result_json=VALUES(result_json),
                    created_at=VALUES(created_at)"""
                if _db.USE_MYSQL else
                f"""INSERT OR REPLACE INTO plan_results
                    (plan_date,tsym,plan_correct,book_net_pct,result_json,created_at)
                    VALUES ({PH},{PH},{PH},{PH},{PH},{PH})""",
                [plan_date, sym, 1 if summ.get("plan_correct") else 0,
                 summ.get("book_net_pct") or 0.0, json.dumps(sc), _now_iso()])

        agg = plan_score.aggregate_scorecard(scored)
        cur.execute(
            f"""INSERT INTO plan_scorecard (plan_date,agg_json,created_at) VALUES ({PH},{PH},{PH})
                ON DUPLICATE KEY UPDATE agg_json=VALUES(agg_json),created_at=VALUES(created_at)"""
            if _db.USE_MYSQL else
            f"""INSERT OR REPLACE INTO plan_scorecard (plan_date,agg_json,created_at)
                VALUES ({PH},{PH},{PH})""",
            [plan_date, json.dumps(agg), _now_iso()])
        conn.commit(); cur.close()
    finally:
        conn.close()
    return {"ok": True, "plan_date": plan_date, "scored": len(scored), "scorecard": agg}


# ── read APIs (for the report endpoints) ────────────────────────────────────

def get_plan_report(plan_date: str) -> Dict:
    ensure_tables()
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        cur.execute(f"""SELECT tsym, rank_n, score, conviction, bias, plan_json
                        FROM daily_plans WHERE plan_date={PH} ORDER BY score DESC, rank_n ASC""",
                    [plan_date])
        plans = [json.loads(r[5]) for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    return {"ok": True, "plan_date": plan_date, "count": len(plans), "plans": plans}


def get_scorecard(plan_date: str) -> Dict:
    ensure_tables()
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        cur.execute(f"SELECT agg_json FROM plan_scorecard WHERE plan_date={PH}", [plan_date])
        row = cur.fetchone()
        agg = json.loads(row[0]) if row else None
        cur.execute(f"""SELECT tsym, plan_correct, book_net_pct, result_json
                        FROM plan_results WHERE plan_date={PH} ORDER BY book_net_pct DESC""",
                    [plan_date])
        results = [json.loads(r[3]) for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    return {"ok": True, "plan_date": plan_date, "scorecard": agg,
            "count": len(results), "results": results}


def list_plan_dates() -> Dict:
    ensure_tables()
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT plan_date FROM daily_plans ORDER BY plan_date DESC")
        pdates = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT plan_date FROM plan_scorecard ORDER BY plan_date DESC")
        sdates = [r[0] for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    return {"plan_dates": pdates, "scored_dates": sdates}
