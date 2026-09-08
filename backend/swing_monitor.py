"""
swing_monitor.py — tracks every live swing setup through three phases and
raises one alert per transition.

The plan hands you a resting BUY LIMIT with a 5-session window. That window is
the whole problem: watch it too loosely and price fills and runs before you
have acted; watch it too eagerly and you buy before price ever reaches the
zone; watch it after the fact and you buy a zone price has already closed
through — which is no longer support, it is a broken level. This module
answers, continuously, "which of those is happening right now?"

Phases
------
  MONITOR   the plan is live and the window is open
            · watching      price above the zone, not yet close
            · approaching   within APPROACH_ATR of the entry   → alert "get ready"
            · at_entry      price is AT / inside the zone       → alert "entry now"
  ENTRY     the limit filled (session low reached the entry)   → alert "filled"
            · in_trade      managing: stop / T1 / trail
            · closed        stop or target hit, or time-stopped (from the scorer)
  NO_ENTRY  terminal. Exactly why:
            · broken        a daily CLOSE below the stop before any fill — the zone
                            was run through, not respected (too late to buy it)
            · expired       the entry window closed and price never came back
            · ran_away      price closed > RUN_AWAY_ATR above the entry and the
                            window ended — the move left without you (missed)
            · superseded    a newer plan exists for this symbol

A setup only ever moves forward: MONITOR → ENTRY or MONITOR → NO_ENTRY. State
and every transition are persisted, so an alert fires once, survives restarts,
and the history explains itself later.

Fill rule (shared with swing_score so the monitor and scorecard agree):
  LIMIT   fills the first session whose LOW <= entry, at min(open, entry) — a
          gap DOWN through the limit fills at the open, better than asked.
  STOP    fills the first session whose HIGH >= trigger, at max(open, trigger),
          never beyond the limit cap (the original rule).
"""
from __future__ import annotations
from typing import Dict, List, Optional, Tuple
import datetime as _dt
import json
import threading
import traceback

import db as _db
import trading_calendar as cal

IST = cal.IST

APPROACH_ATR = 0.75        # within this many ATR of the entry → "approaching"
RUN_AWAY_ATR = 2.0         # closed this far ABOVE entry, window over → "ran away"
TICK_SECONDS = 60
MARKET_OPEN = _dt.time(9, 10)
MARKET_CLOSE = _dt.time(15, 45)


def _now() -> _dt.datetime:
    return _dt.datetime.now(IST)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _is_limit(setup: Dict) -> bool:
    return str(setup.get("entry_type") or "").upper().startswith("LIMIT")


# ── schema ──────────────────────────────────────────────────────────────────

def ensure_tables() -> None:
    conn = _db.connect()
    try:
        cur = conn.cursor()
        txt = "LONGTEXT" if _db.USE_MYSQL else "TEXT"
        cur.execute(f"""CREATE TABLE IF NOT EXISTS swing_monitor (
            plan_date   VARCHAR(10) NOT NULL,
            tsym        VARCHAR(40) NOT NULL,
            setup_name  VARCHAR(160) NOT NULL,
            phase       VARCHAR(12),
            state       VARCHAR(16),
            reason      {txt},
            fill_price  DOUBLE,
            fill_date   VARCHAR(10),
            snapshot    {txt},
            history     {txt},
            updated_at  VARCHAR(32),
            PRIMARY KEY (plan_date, tsym, setup_name)
        )""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS swing_alerts (
            id          {'BIGINT AUTO_INCREMENT PRIMARY KEY' if _db.USE_MYSQL else 'INTEGER PRIMARY KEY AUTOINCREMENT'},
            at          VARCHAR(32),
            plan_date   VARCHAR(10),
            tsym        VARCHAR(40),
            setup_name  VARCHAR(160),
            phase       VARCHAR(12),
            state       VARCHAR(16),
            level       VARCHAR(8),
            title       VARCHAR(200),
            body        {txt},
            acked       INT DEFAULT 0
        )""")
        conn.commit(); cur.close()
    finally:
        conn.close()


# ── data ────────────────────────────────────────────────────────────────────

def _latest_plan_date() -> Optional[str]:
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(plan_date) FROM swing_plans")
        r = cur.fetchone(); cur.close()
        return r[0] if r and r[0] else None
    finally:
        conn.close()


def _plans_for(plan_date: str) -> List[Dict]:
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        cur.execute(f"SELECT plan_json FROM swing_plans WHERE plan_date={PH}", [plan_date])
        out = [json.loads(r[0]) for r in cur.fetchall()]
        cur.close(); return out
    finally:
        conn.close()


def _daily_since(tsym: str, start: _dt.date) -> List[dict]:
    """Completed daily candles from `start` inclusive, oldest-first. Today's bar
    is excluded here and supplied live from the feed instead."""
    from upstox_client import UpstoxClient as _UC
    s_s = int(_dt.datetime.combine(start, _dt.time(0, 0), IST).timestamp())
    rows = _UC.query(tsym, "1d", limit=60, from_ts=s_s)
    cut = cal.today_ist().isoformat()
    return [{"t": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]}
            for r in rows
            if _dt.datetime.fromtimestamp(r["ts"] / 1000, IST).date().isoformat() < cut]


def _live_snapshot(tsym: str) -> Optional[Dict]:
    try:
        from main import _feed
        return (_feed().latest or {}).get(tsym)
    except Exception:
        return None


def _market_open_now(now: Optional[_dt.datetime] = None) -> bool:
    now = now or _now()
    if not cal.is_trading_day(now.date())["open"]:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


# ── the state machine ───────────────────────────────────────────────────────

def _fill_on(setup: Dict, bar: Dict) -> Optional[float]:
    """Return the fill price if this bar fills the setup, else None."""
    entry = setup.get("entry"); tp = setup.get("trigger_price")
    if _is_limit(setup):
        if entry is None:
            return None
        if bar["o"] <= entry:
            return bar["o"]                          # gapped down through the limit
        if bar["l"] <= entry:
            return entry
        return None
    limit = setup.get("limit_price") or (tp * 1.02 if tp else None)
    if tp is None:
        return None
    if bar["o"] > limit:
        return None                                  # opened beyond the cap: no chase
    if bar["h"] >= tp:
        fill = max(bar["o"], tp)
        return fill if fill <= limit else None
    return None


def evaluate_setup(setup: Dict, plan: Dict, plan_date: _dt.date, prior: Optional[Dict],
                   daily: List[dict], live: Optional[Dict], now: _dt.datetime,
                   superseded: bool) -> Dict:
    """Pure: given everything known, return the setup's current classification.
    `prior` is the persisted row (may be None). Terminal states stick."""
    name = setup.get("name") or "?"
    entry = setup.get("entry"); stop = setup.get("stop")
    tgts = setup.get("targets") or []
    window = int(setup.get("entry_window_sessions") or 2)
    atr = float((plan.get("profile") or {}).get("atr14") or 0) or (entry or 1) * 0.02
    out = {"setup_name": name, "entry": entry, "stop": stop, "targets": tgts,
           "entry_type": "LIMIT" if _is_limit(setup) else "STOP",
           "zone": setup.get("zone"), "window_sessions": window,
           "entry_by": setup.get("entry_by"), "exit_by": setup.get("exit_by_date")}

    # terminal states are final — never re-evaluate a decided setup
    if prior and prior.get("phase") in ("NO_ENTRY",) :
        return {**out, "phase": prior["phase"], "state": prior["state"], "reason": prior["reason"],
                "changed": False}
    if prior and prior.get("phase") == "ENTRY" and prior.get("state") == "closed":
        return {**out, "phase": "ENTRY", "state": "closed", "reason": prior["reason"], "changed": False}

    # sessions: completed bars since (and including) the first live session
    sessions_done = len(daily)
    today = now.date()
    today_is_session = cal.is_trading_day(today)["open"] and today >= plan_date
    window_last = setup.get("entry_by")
    window_open = (today.isoformat() <= window_last) if window_last else (sessions_done < window)

    # ── already filled earlier (persisted) → manage the trade ──
    if prior and prior.get("phase") == "ENTRY":
        fill = prior.get("fill_price") or entry
        px = (live or {}).get("lp")
        lo_today = (live or {}).get("day_low"); hi_today = (live or {}).get("day_high")
        reason = f"in trade from {prior.get('fill_date')} @ {fill}"
        state = "in_trade"
        # stop / target on completed bars after the fill date, then today's live bar
        for b in daily:
            bdate = _dt.datetime.fromtimestamp(b["t"] / 1000, IST).date().isoformat()
            if prior.get("fill_date") and bdate <= prior["fill_date"]:
                continue
            if stop is not None and b["l"] <= stop:
                return {**out, "phase": "ENTRY", "state": "closed", "fill_price": fill,
                        "reason": f"stopped out @ {stop} on {bdate}", "changed": True}
            if tgts and b["h"] >= tgts[0]:
                return {**out, "phase": "ENTRY", "state": "closed", "fill_price": fill,
                        "reason": f"T1 {tgts[0]} reached on {bdate}", "changed": True}
        if today_is_session and lo_today is not None and stop is not None and lo_today <= stop:
            return {**out, "phase": "ENTRY", "state": "closed", "fill_price": fill,
                    "reason": f"stop {stop} hit today (low {lo_today})", "changed": True}
        if today_is_session and hi_today is not None and tgts and hi_today >= tgts[0]:
            return {**out, "phase": "ENTRY", "state": "closed", "fill_price": fill,
                    "reason": f"T1 {tgts[0]} reached today (high {hi_today})", "changed": True}
        unreal = ((px - fill) / fill * 100) if (px and fill) else None
        return {**out, "phase": "ENTRY", "state": state, "fill_price": fill,
                "ltp": px, "unrealized_pct": round(unreal, 2) if unreal is not None else None,
                "reason": reason, "changed": False}

    # ── not yet filled: superseded? ──
    if superseded:
        return {**out, "phase": "NO_ENTRY", "state": "superseded",
                "reason": "a newer swing plan exists for this symbol", "changed": True}

    # ── replay completed sessions inside the window for a fill / a break ──
    for i, b in enumerate(daily):
        bdate = _dt.datetime.fromtimestamp(b["t"] / 1000, IST).date().isoformat()
        in_window = (bdate <= window_last) if window_last else (i < window)
        if in_window:
            fp = _fill_on(setup, b)
            if fp is not None:
                # a fill on a bar that also closed below the stop is a broken zone,
                # but it IS a fill — the position exists and was stopped
                if stop is not None and b["c"] < stop:
                    return {**out, "phase": "ENTRY", "state": "closed", "fill_price": round(fp, 2),
                            "fill_date": bdate, "broke_zone": True,
                            "reason": f"filled @ {round(fp,2)} then closed below stop {stop} the same "
                                      f"session ({bdate}) — zone broke. A resting limit cannot be "
                                      "skipped through: this is a -1R loss, not a no-entry.",
                            "changed": True}
                return {**out, "phase": "ENTRY", "state": "in_trade", "fill_price": round(fp, 2),
                        "fill_date": bdate,
                        "reason": (f"LIMIT filled @ {round(fp,2)} on {bdate}" if _is_limit(setup)
                                   else f"buy-stop triggered @ {round(fp,2)} on {bdate}"),
                        "changed": True}
        # closed through the stop without ever filling → the level failed
        if stop is not None and b["c"] < stop:
            return {**out, "phase": "NO_ENTRY", "state": "broken",
                    "reason": f"closed {b['c']} below the stop {stop} on {bdate} before filling — "
                              "the zone was run through, not respected. Too late to buy it.",
                    "changed": True}

    # ── today, live ──
    lp = (live or {}).get("lp")
    lo = (live or {}).get("day_low"); op = (live or {}).get("day_open")
    if today_is_session and window_open and live and lp is not None and entry is not None:
        todaybar = {"o": op if op is not None else lp, "h": (live.get("day_high") or lp),
                    "l": lo if lo is not None else lp, "c": lp}
        fp = _fill_on(setup, todaybar)
        if fp is not None:
            return {**out, "phase": "ENTRY", "state": "in_trade", "fill_price": round(fp, 2),
                    "fill_date": today.isoformat(), "ltp": lp,
                    "reason": (f"LIMIT filled today @ {round(fp,2)} (session low {todaybar['l']})"
                               if _is_limit(setup) else
                               f"buy-stop triggered today @ {round(fp,2)} (session high {todaybar['h']})"),
                    "changed": True}
        if stop is not None and lp < stop:
            # trading below the stop intraday without a fill can only happen on a
            # gap through both — the zone did not hold
            return {**out, "phase": "NO_ENTRY", "state": "broken", "ltp": lp,
                    "reason": f"trading {lp} below the stop {stop} — gapped through the zone",
                    "changed": True}
        dist_atr = (lp - entry) / atr if atr else 99
        dist_pct = (lp - entry) / lp * 100
        if _is_limit(setup):
            if lp <= (setup.get("zone") or {}).get("top", entry) * 1.002:
                st = "at_entry"
            elif dist_atr <= APPROACH_ATR:
                st = "approaching"
            else:
                st = "watching"
        else:
            tp = setup.get("trigger_price") or entry
            if lp >= tp:
                st = "at_entry"
            elif (tp - lp) / atr <= APPROACH_ATR:
                st = "approaching"
            else:
                st = "watching"
        sessions_left = 0
        if window_last:
            d = today
            while d.isoformat() <= window_last:
                if cal.is_trading_day(d)["open"]:
                    sessions_left += 1
                d += _dt.timedelta(days=1)
        return {**out, "phase": "MONITOR", "state": st, "ltp": lp,
                "dist_pct": round(dist_pct, 2), "dist_atr": round(dist_atr, 2),
                "sessions_left": sessions_left,
                "reason": {"watching": f"{round(dist_pct,2)}% above the entry — {sessions_left} session(s) left in the window",
                           "approaching": f"within {round(dist_atr,2)} ATR of the entry {entry} — get the order in",
                           "at_entry": f"price {lp} is AT the zone — the limit should be resting NOW"}[st],
                "changed": (prior or {}).get("state") != st}

    # ── window over, never filled ──
    if not window_open:
        last_close = daily[-1]["c"] if daily else lp
        if last_close is not None and entry is not None and (last_close - entry) / atr >= RUN_AWAY_ATR:
            return {**out, "phase": "NO_ENTRY", "state": "ran_away",
                    "reason": f"window closed; price is {round(last_close,2)}, "
                              f"{round((last_close-entry)/atr,1)} ATR above the entry — the move left "
                              "without a fill. Missed, not wrong.", "changed": True}
        return {**out, "phase": "NO_ENTRY", "state": "expired",
                "reason": f"entry window ended {window_last or ''} with no fill — the setup is stale, "
                          "rebuild rather than chase", "changed": True}

    # market shut / no live price yet → keep last known monitor state
    prev_state = (prior or {}).get("state") or "watching"
    return {**out, "phase": "MONITOR", "state": prev_state if prev_state in ("watching","approaching","at_entry") else "watching",
            "ltp": lp, "sessions_left": None,
            "reason": (prior or {}).get("reason") or "waiting for the first live session",
            "changed": False}


# ── alerts ──────────────────────────────────────────────────────────────────

_ALERT_LEVEL = {
    ("MONITOR", "approaching"): "info",
    ("MONITOR", "at_entry"): "high",
    ("ENTRY", "in_trade"): "high",
    ("ENTRY", "closed"): "info",
    ("NO_ENTRY", "broken"): "warn",
    ("NO_ENTRY", "expired"): "low",
    ("NO_ENTRY", "ran_away"): "warn",
    ("NO_ENTRY", "superseded"): "low",
}
_ALERT_TITLE = {
    ("MONITOR", "approaching"): "Approaching entry",
    ("MONITOR", "at_entry"): "AT ENTRY — act now",
    ("ENTRY", "in_trade"): "FILLED",
    ("ENTRY", "closed"): "Trade closed",
    ("NO_ENTRY", "broken"): "No entry — zone broken",
    ("NO_ENTRY", "expired"): "No entry — window expired",
    ("NO_ENTRY", "ran_away"): "No entry — missed, ran away",
    ("NO_ENTRY", "superseded"): "No entry — plan superseded",
}


def _record(cur, PH, plan_date: str, tsym: str, res: Dict, prior: Optional[Dict]) -> Optional[Dict]:
    """Persist state; if the (phase,state) changed, append history + raise an
    alert. Returns the alert dict if one was raised."""
    key = (res["phase"], res["state"])
    prev_key = ((prior or {}).get("phase"), (prior or {}).get("state"))
    hist = []
    try:
        hist = json.loads((prior or {}).get("history") or "[]")
    except Exception:
        hist = []
    alert = None
    if key != prev_key:
        hist.append({"at": _now_iso(), "phase": res["phase"], "state": res["state"],
                     "reason": res.get("reason"), "ltp": res.get("ltp")})
        hist = hist[-40:]
        if key in _ALERT_TITLE and not (prior is None and key == ("MONITOR", "watching")):
            alert = {"at": _now_iso(), "plan_date": plan_date, "tsym": tsym,
                     "setup_name": res["setup_name"], "phase": res["phase"], "state": res["state"],
                     "level": _ALERT_LEVEL.get(key, "info"),
                     "title": f"{tsym} — {_ALERT_TITLE[key]}",
                     "body": res.get("reason") or ""}
            cur.execute(
                f"""INSERT INTO swing_alerts (at,plan_date,tsym,setup_name,phase,state,level,title,body,acked)
                    VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},0)""",
                [alert["at"], plan_date, tsym, alert["setup_name"], alert["phase"], alert["state"],
                 alert["level"], alert["title"], alert["body"]])
    snap = {k: res.get(k) for k in ("ltp", "dist_pct", "dist_atr", "sessions_left", "unrealized_pct",
                                    "entry", "stop", "targets", "entry_type", "zone", "entry_by", "exit_by",
                                    "broke_zone")}
    fill_price = res.get("fill_price", (prior or {}).get("fill_price"))
    fill_date = res.get("fill_date", (prior or {}).get("fill_date"))
    cur.execute(
        f"""INSERT INTO swing_monitor (plan_date,tsym,setup_name,phase,state,reason,fill_price,fill_date,snapshot,history,updated_at)
            VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})
            ON DUPLICATE KEY UPDATE phase=VALUES(phase),state=VALUES(state),reason=VALUES(reason),
            fill_price=VALUES(fill_price),fill_date=VALUES(fill_date),snapshot=VALUES(snapshot),
            history=VALUES(history),updated_at=VALUES(updated_at)"""
        if _db.USE_MYSQL else
        f"""INSERT OR REPLACE INTO swing_monitor
            (plan_date,tsym,setup_name,phase,state,reason,fill_price,fill_date,snapshot,history,updated_at)
            VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})""",
        [plan_date, tsym, res["setup_name"], res["phase"], res["state"], res.get("reason"),
         fill_price, fill_date, json.dumps(snap), json.dumps(hist), _now_iso()])
    return alert


# ── one evaluation pass ─────────────────────────────────────────────────────

def evaluate(plan_date: Optional[str] = None, now: Optional[_dt.datetime] = None) -> Dict:
    """Evaluate every setup of `plan_date` (default: every plan date that still
    has undecided setups, so an older week's fills keep being managed after a
    new plan is built)."""
    ensure_tables()
    now = now or _now()
    latest = _latest_plan_date()
    if not latest:
        return {"ok": False, "error": "no swing plans built yet"}
    dates = [plan_date] if plan_date else _open_dates(latest)
    alerts: List[Dict] = []
    rows: List[Dict] = []
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        for pd_ in dates:
            pdate = _dt.date.fromisoformat(pd_)
            superseded = pd_ < latest
            cur.execute(f"SELECT tsym,setup_name,phase,state,reason,fill_price,fill_date,history "
                        f"FROM swing_monitor WHERE plan_date={PH}", [pd_])
            prior = {(r[0], r[1]): {"phase": r[2], "state": r[3], "reason": r[4], "fill_price": r[5],
                                    "fill_date": r[6], "history": r[7]} for r in cur.fetchall()}
            present = set()          # (tsym, setup_name) pairs the plan still carries
            for plan in _plans_for(pd_):
                setups = plan.get("setups") or []
                if not setups:
                    continue
                tsym = plan["tsym"]
                for s in setups:
                    present.add((tsym, s.get("name")))
                daily = _daily_since(tsym, pdate)
                live = _live_snapshot(tsym)
                for s in setups:
                    pr = prior.get((tsym, s.get("name")))
                    # a superseded plan whose setup already FILLED is still a live
                    # position — keep managing it; only unfilled ones get retired
                    sup = superseded and not (pr and pr.get("phase") == "ENTRY")
                    res = evaluate_setup(s, plan, pdate, pr, daily, live, now, sup)
                    a = _record(cur, PH, pd_, tsym, res, pr)
                    if a:
                        alerts.append(a)
                    rows.append({"plan_date": pd_, "tsym": tsym, "sector": plan.get("sector"),
                                 "score": plan.get("score"), "conviction": plan.get("conviction"),
                                 **res})
            # A plan rebuilt for the same date (Build next clicked twice) can drop
            # a setup. Its monitor row would otherwise dangle in MONITOR forever —
            # retire it. Filled positions are real and are left alone.
            for (t, nm), pr in prior.items():
                if (t, nm) in present or pr.get("phase") != "MONITOR":
                    continue
                res = {"setup_name": nm, "phase": "NO_ENTRY", "state": "superseded",
                       "reason": "this setup is no longer in the plan after a rebuild",
                       "changed": True}
                a = _record(cur, PH, pd_, t, res, pr)
                if a:
                    alerts.append(a)
        conn.commit(); cur.close()
    finally:
        conn.close()
    return {"ok": True, "evaluated": len(rows), "alerts_raised": len(alerts),
            "alerts": alerts, "at": _now_iso(), "market_open": _market_open_now(now)}


def _open_dates(latest: str) -> List[str]:
    """Plan dates that still have something to watch: the latest plan, plus any
    older date with a setup still in MONITOR or in an open ENTRY."""
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT DISTINCT plan_date FROM swing_monitor
                       WHERE phase='MONITOR' OR (phase='ENTRY' AND state='in_trade')""")
        d = {r[0] for r in cur.fetchall()}
        cur.close()
    finally:
        conn.close()
    d.add(latest)
    return sorted(d)


# ── read API ────────────────────────────────────────────────────────────────

_PHASE_ORDER = {"ENTRY": 0, "MONITOR": 1, "NO_ENTRY": 2}
_STATE_ORDER = {"at_entry": 0, "in_trade": 0, "approaching": 1, "watching": 2, "closed": 3,
                "broken": 4, "ran_away": 5, "expired": 6, "superseded": 7}


def status(plan_date: Optional[str] = None, refresh: bool = True) -> Dict:
    """The board: every tracked setup grouped by phase, most urgent first."""
    ensure_tables()
    if refresh:
        try:
            evaluate(plan_date)
        except Exception:
            traceback.print_exc()
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        if plan_date:
            cur.execute(f"""SELECT plan_date,tsym,setup_name,phase,state,reason,fill_price,fill_date,
                                   snapshot,history,updated_at FROM swing_monitor WHERE plan_date={PH}""",
                        [plan_date])
        else:
            cur.execute("""SELECT plan_date,tsym,setup_name,phase,state,reason,fill_price,fill_date,
                                  snapshot,history,updated_at FROM swing_monitor
                           WHERE plan_date >= (SELECT MAX(plan_date) FROM swing_plans)
                              OR phase='MONITOR' OR (phase='ENTRY' AND state='in_trade')""")
        rows = []
        for r in cur.fetchall():
            snap = {}
            try:
                snap = json.loads(r[8] or "{}")
            except Exception:
                pass
            hist = []
            try:
                hist = json.loads(r[9] or "[]")
            except Exception:
                pass
            rows.append({"plan_date": r[0], "tsym": r[1], "setup_name": r[2], "phase": r[3],
                         "state": r[4], "reason": r[5], "fill_price": r[6], "fill_date": r[7],
                         **snap, "history": hist[-6:], "updated_at": r[10]})
        # sector / score live on the plan, not the monitor row — join them back so
        # the board reads like the plan page does
        meta = {}
        if rows:
            dates = sorted({r["plan_date"] for r in rows})
            marks = ",".join([PH] * len(dates))
            cur.execute(f"SELECT plan_date,tsym,score,conviction,plan_json FROM swing_plans "
                        f"WHERE plan_date IN ({marks})", dates)
            for pd_, t, sc, cv, pj in cur.fetchall():
                sec, setups, last_close, atr = None, {}, None, None
                try:
                    pl = json.loads(pj)
                    sec = pl.get("sector"); last_close = pl.get("last_close")
                    atr = (pl.get("profile") or {}).get("atr14")
                    setups = {x.get("name"): x for x in (pl.get("setups") or [])}
                except Exception:
                    pass
                meta[(pd_, t)] = {"score": sc, "conviction": cv, "sector": sec,
                                  "last_close": last_close, "atr14": atr, "_setups": setups}
        # The monitor row only carries a thin snapshot. The plan's full setup —
        # targets, R:R, size, window, exit plan, valid/skip rules — is what a
        # trader needs to act, so attach it by name.
        for r in rows:
            m = meta.get((r["plan_date"], r["tsym"]), {})
            r.update({k: v for k, v in m.items() if not k.startswith("_")})
            st = (m.get("_setups") or {}).get(r["setup_name"])
            if st:
                r["setup"] = st
                # live distances to stop / T1 for a filled position, in % and R
                fp, stop = r.get("fill_price"), st.get("stop")
                t1 = (st.get("targets") or [None])[0]
                # Outside market hours there is no LTP; the plan's last close is
                # the honest stand-in. Say which one the numbers are based on.
                lp = r.get("ltp"); basis = "ltp"
                if not lp and m.get("last_close"):
                    lp = m["last_close"]; basis = "last_close"
                if lp and fp and stop:
                    R = abs(fp - stop) or 1e-9
                    r["px_basis"] = basis
                    r["r_now"] = round((lp - fp) / R, 2)
                    r["to_stop_pct"] = round((lp - stop) / lp * 100, 2)
                    if t1:
                        r["to_t1_pct"] = round((t1 - lp) / lp * 100, 2)
                    if r.get("unrealized_pct") is None and r.get("phase") == "ENTRY":
                        r["unrealized_pct"] = round((lp - fp) / fp * 100, 2)
        cur.execute("SELECT COUNT(*) FROM swing_alerts WHERE acked=0")
        unacked = cur.fetchone()[0]
        cur.close()
    finally:
        conn.close()
    rows.sort(key=lambda x: (_PHASE_ORDER.get(x["phase"], 9), _STATE_ORDER.get(x["state"], 9),
                             x.get("dist_atr") if x.get("dist_atr") is not None else 99))
    counts = {"ENTRY": 0, "MONITOR": 0, "NO_ENTRY": 0}
    for x in rows:
        counts[x["phase"]] = counts.get(x["phase"], 0) + 1
    return {"ok": True, "at": _now_iso(), "market_open": _market_open_now(),
            "counts": counts, "unacked_alerts": unacked, "rows": rows,
            "rules": {"approach_atr": APPROACH_ATR, "run_away_atr": RUN_AWAY_ATR}}


def alerts(since_id: int = 0, limit: int = 50, unacked_only: bool = False) -> Dict:
    ensure_tables()
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        q = f"SELECT id,at,plan_date,tsym,setup_name,phase,state,level,title,body,acked FROM swing_alerts WHERE id>{PH}"
        args = [since_id]
        if unacked_only:
            q += " AND acked=0"
        q += f" ORDER BY id DESC LIMIT {int(limit)}"
        cur.execute(q, args)
        out = [{"id": r[0], "at": r[1], "plan_date": r[2], "tsym": r[3], "setup_name": r[4],
                "phase": r[5], "state": r[6], "level": r[7], "title": r[8], "body": r[9],
                "acked": bool(r[10])} for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    return {"ok": True, "alerts": out, "max_id": max([a["id"] for a in out], default=since_id)}


def ack(ids: Optional[List[int]] = None, all_: bool = False) -> Dict:
    ensure_tables()
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        if all_:
            cur.execute("UPDATE swing_alerts SET acked=1 WHERE acked=0")
        elif ids:
            for i in ids:
                cur.execute(f"UPDATE swing_alerts SET acked=1 WHERE id={PH}", [int(i)])
        n = cur.rowcount
        conn.commit(); cur.close()
    finally:
        conn.close()
    return {"ok": True, "acked": n}


# ── background loop ─────────────────────────────────────────────────────────

_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _loop():
    _stop.wait(60)
    while not _stop.is_set():
        try:
            # during the session evaluate every minute; outside it, every 15 min
            # is plenty (only completed-bar transitions can happen)
            if _market_open_now() or _now().minute % 15 == 0:
                r = evaluate()
                if r.get("alerts_raised"):
                    for a in r["alerts"]:
                        print(f"[swing_monitor] {a['level'].upper():5} {a['title']} — {a['body'][:100]}",
                              flush=True)
        except Exception:
            traceback.print_exc()
        _stop.wait(TICK_SECONDS)


def start() -> Dict:
    global _thread
    if _thread and _thread.is_alive():
        return {"ok": True, "running": True, "already": True}
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="swing-monitor", daemon=True)
    _thread.start()
    return {"ok": True, "running": True, "tick_seconds": TICK_SECONDS}


def running() -> bool:
    return bool(_thread and _thread.is_alive())
