"""
swing_pipeline.py — nightly orchestration for the SWING plan (mirror of
plan_pipeline.py, but on the daily timeframe):

  build_swing_plans(for_date)  : select universe → fetch daily candles → build a
                                 swing plan per stock → cross-sectional RS rank →
                                 store in `swing_plans` (plan_date = first live session).
  score_swing_plans(plan_date) : load that date's plans → replay vs the daily
                                 candles that actually followed → store results +
                                 a book scorecard (open positions stay "open").

Plans are stored as JSON. Nothing here places orders.
"""
from __future__ import annotations
from typing import Dict, List, Optional
import json
import datetime as _dt

import db as _db
import swing_engine
import swing_score
import universe as _universe
from plan_pipeline import (_ensure_candles, _truncate, _ist_date,
                           next_trading_day, prev_trading_day)

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def _now_iso():
    return _dt.datetime.now(IST).isoformat(timespec="seconds")


# ── schema ──────────────────────────────────────────────────────────────────

def ensure_tables():
    conn = _db.connect()
    try:
        cur = conn.cursor()
        txt = "LONGTEXT" if _db.USE_MYSQL else "TEXT"
        cur.execute(f"""CREATE TABLE IF NOT EXISTS swing_plans (
            plan_date   VARCHAR(10) NOT NULL,
            tsym        VARCHAR(40) NOT NULL,
            rank_n      INT,
            score       INT,
            conviction  VARCHAR(12),
            bias        VARCHAR(12),
            plan_json   {txt},
            created_at  VARCHAR(32),
            PRIMARY KEY (plan_date, tsym)
        )""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS swing_results (
            plan_date   VARCHAR(10) NOT NULL,
            tsym        VARCHAR(40) NOT NULL,
            outcome     VARCHAR(12),
            net_pct     DOUBLE,
            result_json {txt},
            created_at  VARCHAR(32),
            PRIMARY KEY (plan_date, tsym)
        )""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS swing_scorecard (
            plan_date  VARCHAR(10) NOT NULL,
            agg_json   {txt},
            created_at VARCHAR(32),
            PRIMARY KEY (plan_date)
        )""")
        conn.commit(); cur.close()
    finally:
        conn.close()


# ── cross-sectional pass (tonight's universe as the peer group) ─────────────

def _apply_cross_sectional(plans: List[Dict]) -> None:
    """After every stock is built: (1) vol-normalized blended-momentum percentile
    (Jegadeesh-Titman style, Nifty-Momentum-index style vol normalization),
    (2) RS vs the universe median, (3) market-breadth regime gate, (4) the
    leaders-only filter — swing longs are emitted only for RS-percentile ≥ 70
    names (the research is unambiguous: unfiltered breakouts fail in India;
    the leadership filter is load-bearing). Mutates the plans in place."""
    if not plans:
        return

    def _zfun(vals):
        xs = [v for v in vals if v is not None]
        if len(xs) < 2:
            return lambda v: 0.0
        m = sum(xs) / len(xs)
        sd = (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5 or 1e-9
        return lambda v: ((v - m) / sd) if v is not None else 0.0

    t = [p["trend"] for p in plans]
    z21 = _zfun([x.get("ret_21d") for x in t])
    z63 = _zfun([x.get("ret_63d") for x in t])
    z126 = _zfun([x.get("ret_126d") for x in t])

    raws = []
    for x in t:
        blend = (z21(x.get("ret_21d")) + z63(x.get("ret_63d")) + z126(x.get("ret_126d"))) / 3.0
        sig = x.get("sigma63_ann_pct")
        raws.append(blend / (sig / 100.0) if sig else blend)

    def _median(vals):
        xs = sorted(v for v in vals if v is not None)
        return xs[len(xs) // 2] if xs else None

    med63 = _median([x.get("ret_63d") for x in t])
    med21 = _median([x.get("ret_21d") for x in t])
    srt = sorted(raws)
    n = len(srt)

    above = with200 = 0
    for p in plans:
        s200 = p["trend"].get("sma200")
        if s200:
            with200 += 1
            if (p.get("last_close") or 0) > s200:
                above += 1
    breadth = (above / with200) if with200 else None
    regime_on = breadth is None or breadth >= swing_engine.BREADTH_REGIME_MIN

    for p, raw in zip(plans, raws):
        pct = round(sum(1 for x in srt if x < raw) / (n - 1) * 100, 1) if n > 1 else 50.0
        r63, r21 = p["trend"].get("ret_63d"), p["trend"].get("ret_21d")
        p["xsec"] = {"momentum_percentile": pct,
                     "ret63_gt_median": bool(med63 is not None and r63 is not None and r63 > med63),
                     "ret21_gt_median": bool(med21 is not None and r21 is not None and r21 > med21)}
        p["trend"]["momentum_percentile"] = pct
        p["market_regime"] = {"on": regime_on,
                              "breadth_above_sma200": round(breadth, 3) if breadth is not None else None,
                              "rule": f"no new swing entries when <{int(swing_engine.BREADTH_REGIME_MIN*100)}% "
                                      "of the universe closes above its 200-SMA"}
        if p["setups"] and not regime_on:
            p["no_trade"].insert(0, f"MARKET REGIME OFF: only {round((breadth or 0)*100)}% of the "
                                    "universe is above its 200-SMA — momentum entries drown in a "
                                    "weak tape. No new swings tonight.")
            p["setups"] = []; p["num_setups"] = 0; p["primary"] = None
            p["headline"] = "MARKET REGIME OFF — no new swing entries tonight (weak breadth)."
        elif p["setups"] and pct < 70:
            p["no_trade"].insert(0, f"RS percentile {pct} < 70 — not a leader tonight; swing "
                                    "longs are emitted only for the strongest names.")
            p["setups"] = []; p["num_setups"] = 0; p["primary"] = None
            p["headline"] = (f"Passes the trend gates but RS rank {pct} pctile < 70 — "
                             "watch, don't enter. Leaders only.")


# ── build ───────────────────────────────────────────────────────────────────

def build_swing_plans(for_date: Optional[str] = None, top_n: int = 50) -> Dict:
    """Build swing plans targeting a session. `for_date` (YYYY-MM-DD) overrides;
    default = next trading day after today (IST). Point-in-time: only candles up
    to `as_of` (the session before the target) are used — no look-ahead."""
    ensure_tables()
    today_ist = _dt.datetime.now(IST).date()
    target = (_dt.date.fromisoformat(for_date) if for_date
              else next_trading_day(today_ist))
    as_of = prev_trading_day(target) if target <= today_ist else today_ist

    uni = _universe.select_universe(as_of, top_n=top_n)
    built, skipped = [], []
    plans: List[Dict] = []
    for row in uni["rows"]:
        sym = row["sym"]
        try:
            daily = _truncate(_ensure_candles(sym, "1d", 252, fresh_until=as_of), as_of)
            if len(daily) < 60:
                skipped.append({"sym": sym, "why": f"only {len(daily)} daily candles"}); continue
            last_d = _dt.date.fromisoformat(_ist_date(daily[-1]["t"]))
            if (as_of - last_d).days > 4:
                skipped.append({"sym": sym, "why": f"stale data (last {last_d})"}); continue
            plan = swing_engine.build_swing_plan(sym, daily, target)
            if not plan.get("ok"):
                skipped.append({"sym": sym, "why": plan.get("error")}); continue
            plan["universe_rank"] = row.get("rank")
            plan["turnover_cr"] = row.get("turnover_cr")
            plans.append(plan)
        except Exception as e:
            skipped.append({"sym": sym, "why": str(e)[:120]})

    _apply_cross_sectional(plans)

    for p in plans:
        sc = swing_engine.confluence_score(p["trend"], p["profile"], p["setups"],
                                           p.get("xsec"))
        p["score"], p["conviction"], p["score_parts"] = sc["score"], sc["label"], sc["parts"]

    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        cur.execute(f"DELETE FROM swing_plans WHERE plan_date={PH}", [target.isoformat()])
        for p in plans:
            cur.execute(
                f"""INSERT INTO swing_plans
                    (plan_date,tsym,rank_n,score,conviction,bias,plan_json,created_at)
                    VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})
                    ON DUPLICATE KEY UPDATE score=VALUES(score),conviction=VALUES(conviction),
                    bias=VALUES(bias),plan_json=VALUES(plan_json),created_at=VALUES(created_at)"""
                if _db.USE_MYSQL else
                f"""INSERT OR REPLACE INTO swing_plans
                    (plan_date,tsym,rank_n,score,conviction,bias,plan_json,created_at)
                    VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})""",
                [target.isoformat(), p["tsym"], p.get("universe_rank"), p.get("score"),
                 p.get("conviction"), p.get("bias"), json.dumps(p), _now_iso()])
            built.append(p["tsym"])
        conn.commit(); cur.close()
    finally:
        conn.close()

    return {"ok": True, "plan_date": target.isoformat(), "universe_source": uni["source"],
            "built": len(built), "skipped": len(skipped),
            "symbols": built, "skipped_detail": skipped[:20]}


# ── score (replay vs the days that followed) ────────────────────────────────

def _daily_after(tsym: str, start: _dt.date) -> List[dict]:
    """Completed daily candles from `start` (inclusive) up to yesterday (IST).
    Today's forming candle is excluded — a swing bar only counts when closed."""
    from upstox_client import UpstoxClient as _UC
    today_ist = _dt.datetime.now(IST).date()
    s_s = int(_dt.datetime.combine(start, _dt.time(0, 0), IST).timestamp())
    rows = _UC.query(tsym, "1d", limit=400, from_ts=s_s)
    if not rows or _ist_date(rows[-1]["ts"]) < prev_trading_day(today_ist).isoformat():
        try:
            from main import _upstox
            _upstox().fetch_candles(tsym=tsym, interval="1d")
        except Exception:
            pass
        rows = _UC.query(tsym, "1d", limit=400, from_ts=s_s)
    cut = today_ist.isoformat()
    return [{"t": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]}
            for r in rows if _ist_date(r["ts"]) < cut]


def score_swing_plans(plan_date: str) -> Dict:
    """Replay every swing plan for `plan_date` against the daily candles that
    followed. Can be run any evening — positions not yet resolved come back as
    'open' and the scorecard says so."""
    ensure_tables()
    day = _dt.date.fromisoformat(plan_date)
    today_ist = _dt.datetime.now(IST).date()
    if day >= today_ist:
        return {"ok": False, "error": "not_yet_traded",
                "message": f"{plan_date} hasn't traded yet — nothing to score."}
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        cur.execute(f"SELECT tsym, plan_json FROM swing_plans WHERE plan_date={PH}", [plan_date])
        plans = [(r[0], json.loads(r[1])) for r in cur.fetchall()]
        if not plans:
            cur.close()
            return {"ok": False, "error": f"no swing plans stored for {plan_date}"}

        scored = []
        for sym, plan in plans:
            candles = _daily_after(sym, day)
            if not candles:
                continue
            sc = swing_score.score_plan(plan, candles)
            if not sc.get("ok"):
                continue
            scored.append(sc)
            cur.execute(
                f"""INSERT INTO swing_results (plan_date,tsym,outcome,net_pct,result_json,created_at)
                    VALUES ({PH},{PH},{PH},{PH},{PH},{PH})
                    ON DUPLICATE KEY UPDATE outcome=VALUES(outcome),net_pct=VALUES(net_pct),
                    result_json=VALUES(result_json),created_at=VALUES(created_at)"""
                if _db.USE_MYSQL else
                f"""INSERT OR REPLACE INTO swing_results
                    (plan_date,tsym,outcome,net_pct,result_json,created_at)
                    VALUES ({PH},{PH},{PH},{PH},{PH},{PH})""",
                [plan_date, sym, sc.get("outcome"), sc.get("net_pct") or 0.0,
                 json.dumps(sc), _now_iso()])

        agg = swing_score.aggregate_scorecard(scored)
        cur.execute(
            f"""INSERT INTO swing_scorecard (plan_date,agg_json,created_at) VALUES ({PH},{PH},{PH})
                ON DUPLICATE KEY UPDATE agg_json=VALUES(agg_json),created_at=VALUES(created_at)"""
            if _db.USE_MYSQL else
            f"""INSERT OR REPLACE INTO swing_scorecard (plan_date,agg_json,created_at)
                VALUES ({PH},{PH},{PH})""",
            [plan_date, json.dumps(agg), _now_iso()])
        conn.commit(); cur.close()
    finally:
        conn.close()
    return {"ok": True, "plan_date": plan_date, "scored": len(scored), "scorecard": agg}


# ── read APIs ───────────────────────────────────────────────────────────────

def get_swing_report(plan_date: str) -> Dict:
    ensure_tables()
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        cur.execute(f"""SELECT plan_json FROM swing_plans WHERE plan_date={PH}
                        ORDER BY score DESC, rank_n ASC""", [plan_date])
        plans = [json.loads(r[0]) for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    return {"ok": True, "plan_date": plan_date, "count": len(plans), "plans": plans}


def get_swing_scorecard(plan_date: str) -> Dict:
    ensure_tables()
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        cur.execute(f"SELECT agg_json FROM swing_scorecard WHERE plan_date={PH}", [plan_date])
        row = cur.fetchone()
        agg = json.loads(row[0]) if row else None
        cur.execute(f"""SELECT result_json FROM swing_results WHERE plan_date={PH}
                        ORDER BY net_pct DESC""", [plan_date])
        results = [json.loads(r[0]) for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    return {"ok": True, "plan_date": plan_date, "scorecard": agg,
            "count": len(results), "results": results}


def list_swing_dates() -> Dict:
    ensure_tables()
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT plan_date FROM swing_plans ORDER BY plan_date DESC")
        pdates = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT plan_date FROM swing_scorecard ORDER BY plan_date DESC")
        sdates = [r[0] for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    return {"plan_dates": pdates, "scored_dates": sdates}
