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
    tried interval isn't stored for that day yet.

    A day that hasn't happened (or is still in progress) can NEVER have candles on
    Upstox, so the fetch-fallback is skipped for day >= today — without this guard,
    scoring a future date hammers Upstox up to len(intervals) times per symbol
    (e.g. 46 stocks x 2 intervals = 92 calls) purely to confirm "no data yet",
    which is slow enough to 504-timeout the request."""
    from upstox_client import UpstoxClient as _UC
    today_ist = _dt.datetime.now(IST).date()
    can_fetch = day < today_ist
    start = _dt.datetime.combine(day, _dt.time(0, 0), IST)
    s_s = int(start.timestamp()); e_s = s_s + 86400
    for iv in intervals:
        rows = _UC.query(tsym, iv, limit=100000, from_ts=s_s, to_ts=e_s)
        if len(rows) < 3 and can_fetch:
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
        # Clear this session's prior plans first, so the report reflects EXACTLY the
        # fresh universe — no ETFs or stale names left over from an earlier build.
        cur.execute(f"DELETE FROM daily_plans WHERE plan_date={PH}", [target.isoformat()])
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

    # Register the universe for tick-by-tick recording (live builds only — a
    # backtest of a past date must not change what the feed records today).
    recording = None
    if target > today_ist and built:
        recording = _register_tick_recording(built)

    return {"ok": True, "plan_date": target.isoformat(), "universe_source": uni["source"],
            "built": len(built), "skipped": len(skipped),
            "tick_recording": recording,
            "symbols": built, "skipped_detail": skipped[:20]}


def _register_tick_recording(symbols: List[str]):
    """Ensure the plan universe is recorded tick-by-tick from the next poll:
    hot-update the running feed AND persist to plan_universe.json (which the
    feed loads on startup, so a restart keeps recording)."""
    try:
        from main import _feed
        return _feed().set_extra_symbols(symbols)
    except Exception:
        try:
            from upstox_feed import save_extra_symbols
            save_extra_symbols(symbols)
            return {"ok": True, "recording": len(symbols), "note": "saved; feed picks up on restart"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}


# ── score ───────────────────────────────────────────────────────────────────

def score_daily_plans(plan_date: str) -> Dict:
    """Replay every plan whose plan_date == the given session against that day's
    actual candles; store per-stock results and the book scorecard."""
    ensure_tables()
    day = _dt.date.fromisoformat(plan_date)
    today_ist = _dt.datetime.now(IST).date()
    if day >= today_ist:
        # Fast, explicit answer instead of silently trying (and failing slowly)
        # to fetch candles for a session that hasn't happened yet.
        return {"ok": False, "error": "not_yet_traded",
                "message": f"{plan_date} hasn't happened yet — nothing to score."}
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


def _nearest_ob(zones: Dict, ltp: float, near_pct: float) -> Dict:
    """Nearest unmitigated ORDER BLOCK to the live price. OBs get priority in the
    monitor because they historically react harder than FVGs."""
    if ltp is None or not zones:
        return {"near": False, "touching": False, "approaching": False}
    obs = [z for k in ("above", "below") for z in (zones.get(k) or [])
           if z.get("kind") == "OB" and z.get("top") is not None and z.get("bottom") is not None]
    best = None
    for z in obs:
        top, bot = z["top"], z["bottom"]
        if bot <= ltp <= top:
            dist = 0.0
        elif ltp < bot:
            dist = (bot - ltp) / ltp * 100
        else:
            dist = (ltp - top) / ltp * 100
        if best is None or dist < best[0]:
            best = (dist, z)
    if best is None:
        return {"near": False, "touching": False, "approaching": False}
    dist, z = best
    return {"near": dist <= near_pct, "touching": dist == 0.0,
            "approaching": 0.0 < dist <= near_pct, "dist_pct": round(dist, 2),
            "zone": {"kind": z.get("kind"), "dir": z.get("dir"),
                     "top": z.get("top"), "bottom": z.get("bottom")}}


def _setup_live(s: Dict, ltp: float, near_pct: float) -> Dict:
    tp, td = s.get("trigger_price"), s.get("trigger_dir")
    out = {"name": s.get("name"), "side": s.get("side"), "quality": s.get("quality"),
           "trigger_price": tp, "trigger_dir": td, "entry": s.get("entry"),
           "stop": s.get("stop"), "targets": s.get("targets"),
           "state": "waiting", "dist_pct": None}
    if ltp is None or tp is None:
        return out
    triggered = (ltp >= tp) if td == "above" else (ltp <= tp)
    dist = abs(ltp - tp) / ltp * 100
    out["dist_pct"] = round(dist, 2)
    out["state"] = "triggered" if triggered else ("approaching" if dist <= near_pct else "waiting")
    return out


def live_plan_status(date: Optional[str] = None, near_pct: float = 0.5) -> Dict:
    """Live monitor of the active session's plan across all stocks: each stock's
    live price vs its planned setups, with Order-Block proximity flagged and
    sorted to the top (OB = higher-conviction reaction)."""
    ensure_tables()
    today = _dt.datetime.now(IST).date().isoformat()
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        if not date:
            cur.execute("SELECT DISTINCT plan_date FROM daily_plans ORDER BY plan_date")
            all_dates = [r[0] for r in cur.fetchall()]
            upcoming = [d for d in all_dates if d >= today]
            date = upcoming[0] if upcoming else (all_dates[-1] if all_dates else None)
        if not date:
            cur.close()
            return {"ok": False, "error": "no plans built yet"}
        cur.execute(f"SELECT tsym, plan_json FROM daily_plans WHERE plan_date={PH}", [date])
        plans = [(r[0], json.loads(r[1])) for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()

    latest = {}
    try:
        from main import _feed
        latest = dict(_feed().latest)
    except Exception:
        pass

    rows = []
    for tsym, plan in plans:
        if not plan.get("ok"):
            continue
        snap = latest.get(tsym) or {}
        ltp = snap.get("lp")
        live = ltp is not None
        if ltp is None:
            ltp = plan.get("last_close")
        L = plan.get("levels") or {}
        ob = _nearest_ob(L.get("smc_zones") or {}, ltp, near_pct)
        setups = [_setup_live(s, ltp, near_pct) for s in plan.get("setups", [])]
        in_play = [s for s in setups if s["state"] in ("triggered", "approaching")]
        dists = [s["dist_pct"] for s in setups if s["dist_pct"] is not None]
        rows.append({
            "tsym": tsym, "ltp": ltp, "live": live,
            "change_pct": snap.get("change_pct"),
            "bias": plan.get("bias"), "conviction": plan.get("conviction"),
            "score": plan.get("score"), "headline": (plan.get("headline") or {}).get("read"),
            "ob": ob, "setups": setups,
            "in_play": len(in_play), "triggered": sum(1 for s in setups if s["state"] == "triggered"),
            "nearest_dist": min(dists) if dists else 999,
            "priority": bool(ob.get("touching") or ob.get("approaching")),
        })

    # OB priority first, then whatever is closest to triggering
    rows.sort(key=lambda r: (0 if r["priority"] else 1,
                             0 if r["triggered"] else 1, r["nearest_dist"]))
    return {"ok": True, "plan_date": date, "is_today": date == today,
            "count": len(rows), "live_prices": any(r["live"] for r in rows),
            "rows": rows}


def _today_intraday(tsym: str, interval: str = "15m") -> List[dict]:
    """Today's forming intraday candles (fetches today_only so live bars are fresh)."""
    from upstox_client import UpstoxClient as _UC
    today = _dt.datetime.now(IST).date()
    start = _dt.datetime.combine(today, _dt.time(0, 0), IST)
    s_s = int(start.timestamp()); e_s = s_s + 86400
    try:
        from main import _upstox
        _upstox().fetch_candles(tsym=tsym, interval=interval, today_only=True)
    except Exception:
        pass
    rows = _UC.query(tsym, interval, limit=100000, from_ts=s_s, to_ts=e_s)
    return [{"t": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"],
             "c": r["c"], "v": r["v"]} for r in rows]


def _verdict(status: str, reached: Optional[str], net: Optional[float]) -> Dict:
    """Map a score_setup status → live plan verdict (green ✓ / red ✗ / pending)."""
    r = (reached or "").lower()
    if status == "filled":
        if "stop" in r:
            return {"verdict": "fail", "icon": "✗", "label": "hit stop"}
        if "target" in r or (net is not None and net > 0):
            return {"verdict": "ok", "icon": "✓", "label": "hit target"}
        return {"verdict": "pending", "icon": "◐", "label": "in trade"}
    if status == "skipped_by_filter":
        return {"verdict": "fail", "icon": "✗", "label": "not valid today"}
    if status == "tick_gate_failed":
        return {"verdict": "fail", "icon": "✗", "label": "order flow against"}
    if status in ("triggered", "triggered_no_fill"):
        return {"verdict": "pending", "icon": "◐", "label": "triggered — watching"}
    return {"verdict": "pending", "icon": "○", "label": "waiting for trigger"}


def live_setup_status(tsym: str) -> Dict:
    """Full live evaluation of ONE stock's plan for the chart overlay: every setup's
    entry/stop/targets with a live ✓/✗/pending verdict (reusing the gated scorer),
    the plan's own Order Blocks, and the open-type check. Backend does all the maths."""
    import plan_score
    ensure_tables()
    tsym = tsym.upper()
    today = _dt.datetime.now(IST).date().isoformat()
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        cur.execute(f"SELECT plan_date, plan_json FROM daily_plans WHERE tsym={PH} ORDER BY plan_date", [tsym])
        rows = [(r[0], r[1]) for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    if not rows:
        return {"ok": False, "error": f"no plan stored for {tsym}"}
    upcoming = [r for r in rows if r[0] >= today]
    date, raw = (upcoming[0] if upcoming else rows[-1])
    plan = json.loads(raw)
    is_today = date == today

    latest = {}
    try:
        from main import _feed
        latest = dict(_feed().latest)
    except Exception:
        pass
    snap = latest.get(tsym) or {}
    ltp = snap.get("lp") if snap.get("lp") is not None else plan.get("last_close")
    day_open = snap.get("day_open")

    candles = _today_intraday(tsym) if is_today else _intraday_for_day(tsym, _dt.date.fromisoformat(date))
    if candles:
        day_open = candles[0]["o"]

    L = plan.get("levels") or {}
    cpr = L.get("cpr") or {}
    ctx = {"cpr_class": cpr.get("class"), "bias": plan.get("bias"),
           "open": (candles[0]["o"] if candles else (day_open if day_open is not None else ltp)),
           "cam": L.get("camarilla"), "cpr": cpr,
           "stand_down": (L.get("rvol") or 1.0) < 0.5}

    setups = []
    for s in plan.get("setups", []):
        sc = plan_score.score_setup(s, candles, ctx, tsym)
        v = _verdict(sc["status"], sc.get("reached"), sc.get("net_pct"))
        dist = None
        if ltp and s.get("trigger_price"):
            dist = round(abs(ltp - s["trigger_price"]) / ltp * 100, 2)
        setups.append({
            "name": s.get("name"), "side": s.get("side"), "quality": s.get("quality"),
            "source": s.get("source"), "day_type": s.get("day_type"),
            "trigger_price": s.get("trigger_price"), "trigger_dir": s.get("trigger_dir"),
            "entry": s.get("entry"), "stop": s.get("stop"), "targets": s.get("targets"),
            "valid_when": s.get("valid_when"), "skip_when": s.get("skip_when"),
            "status": sc["status"], "reached": sc.get("reached"),
            "net_pct": sc.get("net_pct"), "filter_reason": sc.get("filter_reason"),
            "dist_pct": dist, **v,
        })

    obs = []
    zones = L.get("smc_zones") or {}
    for k in ("above", "below"):
        for z in (zones.get(k) or []):
            if z.get("kind") == "OB":
                obs.append({"top": z.get("top"), "bottom": z.get("bottom"), "dir": z.get("dir")})

    # open-type check (req 3: did it gap the right way and follow?)
    pdc = (L.get("prev_day") or {}).get("PDC")
    open_eval = {"verdict": "pending", "text": "waiting for the open"}
    if candles and day_open is not None and pdc:
        gap = (day_open - pdc) / pdc * 100
        gtype = "gap-up" if gap > 0.3 else "gap-down" if gap < -0.3 else "flat"
        cur_c = candles[-1]["c"]
        if gtype == "gap-up":
            ok = cur_c >= day_open
            open_eval = {"verdict": "ok" if ok else "fail",
                         "text": f"{gtype} {gap:+.2f}% — {'holding above open (trend-up)' if ok else 'faded back below open'}"}
        elif gtype == "gap-down":
            ok = cur_c <= day_open
            open_eval = {"verdict": "ok" if ok else "fail",
                         "text": f"{gtype} {gap:+.2f}% — {'holding below open (trend-down)' if ok else 'reclaimed above open'}"}
        else:
            open_eval = {"verdict": "pending",
                         "text": f"flat open ({gap:+.2f}%) — let the first range set, then follow the break"}

    decision = plan_score.decision_flow(plan, candles, ctx, setups)
    for s in setups:
        s["selected"] = (s["name"] == decision.get("selected"))

    return {"ok": True, "tsym": tsym, "plan_date": date, "is_today": is_today,
            "ltp": ltp, "day_open": day_open, "change_pct": snap.get("change_pct"),
            "bias": plan.get("bias"), "conviction": plan.get("conviction"),
            "score": plan.get("score"), "cpr_class": cpr.get("class"),
            "headline": (plan.get("headline") or {}),
            "day_type_hint": plan.get("day_type_hint"),
            "open_scenarios": plan.get("open_scenarios"),
            "open_eval": open_eval, "setups": setups, "obs": obs,
            "decision": decision, "outcome": decision.get("outcome")}


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
