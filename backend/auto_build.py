"""
auto_build.py — build tomorrow's plans by itself, every evening, forever.

The rules, exactly as specified:

  * The market traded today  →  build the plan for the NEXT TRADING DAY.
  * Tomorrow is a holiday    →  the target skips it and lands on the next day
                                the market is actually open.
  * Today was a holiday      →  do nothing at all. Yesterday's plan is still the
                                live plan; rebuilding it against unchanged data
                                would only churn it.

Mechanics: a daemon thread ticks once a minute and does nothing until the
configured IST build time. Stdlib only — the box enforces PEP 668 so a
scheduler dependency cannot be installed, and this needs none.

Everything is idempotent on `last_built_for`: the build fires at most once per
target date no matter how often the process restarts or the thread ticks. A
pm2 restart at 19:05 will not rebuild a plan that already went out at 19:00.
"""
from __future__ import annotations
from typing import Dict, Optional
import datetime as _dt
import json
import os
import threading
import traceback

import trading_calendar as cal

IST = cal.IST

_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "local_data", "auto_build.json")

# 19:00 IST: the close is 15:30 and NSE publishes the EOD bhavcopy around
# 18:00–19:00. Building before it lands means select_universe silently falls
# back to YESTERDAY's turnover ranking — a stale universe that looks fine.
BUILD_HOUR = int(os.getenv("AUTO_BUILD_HOUR", "19"))
BUILD_MINUTE = int(os.getenv("AUTO_BUILD_MINUTE", "0"))
RETRY_MINUTES = 30           # if a build fails or the bhavcopy is late, try again
MAX_ATTEMPTS = 6             # ...up to 3 hours past the build time
TICK_SECONDS = 60
TOP_N = int(os.getenv("AUTO_BUILD_TOP_N", "200"))
ENABLED = os.getenv("AUTO_BUILD", "1") not in ("0", "false", "False", "no")


# ── state ───────────────────────────────────────────────────────────────────

def _read() -> Dict:
    try:
        with open(_STATE, "r", encoding="utf-8") as f:
            j = json.load(f)
        if isinstance(j, dict):
            return j
    except Exception:
        pass
    return {}


def _write(j: Dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE), exist_ok=True)
        with open(_STATE, "w", encoding="utf-8") as f:
            json.dump(j, f, indent=1)
    except Exception:
        pass


def _now() -> _dt.datetime:
    return _dt.datetime.now(IST)


# ── the decision ────────────────────────────────────────────────────────────

def decide(now: Optional[_dt.datetime] = None) -> Dict:
    """Should a build run right now, and for which session? Pure — no side
    effects — so it can be unit-tested and shown in the status endpoint."""
    now = now or _now()
    today = now.date()
    st = _read()

    day = cal.is_trading_day(today)
    if not day["open"]:
        return {"run": False, "reason": f"market closed today ({day['why']}) — "
                                        "nothing to rebuild", "today_open": False}
    if not day["confident"]:
        return {"run": False, "reason": "cannot confirm today is a trading day "
                                        "(NSE holiday list unavailable) — holding off "
                                        "rather than building against a guess",
                "today_open": None}

    target = cal.next_trading_day(today)

    if st.get("last_built_for") == target.isoformat() and st.get("last_status") == "ok":
        return {"run": False, "reason": f"plan for {target} already built at "
                                        f"{st.get('last_run_at')}", "target": target.isoformat(),
                "today_open": True}

    due = now.replace(hour=BUILD_HOUR, minute=BUILD_MINUTE, second=0, microsecond=0)
    if now < due:
        return {"run": False, "reason": f"waiting until {BUILD_HOUR:02d}:{BUILD_MINUTE:02d} IST",
                "target": target.isoformat(), "due_at": due.isoformat(timespec="minutes"),
                "today_open": True}

    attempts = st.get("attempts_for", {}).get(target.isoformat(), 0)
    if attempts >= MAX_ATTEMPTS:
        return {"run": False, "reason": f"gave up after {attempts} failed attempts for "
                                        f"{target} — see last_error", "target": target.isoformat(),
                "today_open": True}
    if attempts and st.get("last_attempt_at"):
        try:
            last = _dt.datetime.fromisoformat(st["last_attempt_at"])
            if (now - last).total_seconds() < RETRY_MINUTES * 60:
                return {"run": False, "reason": f"retry {attempts + 1}/{MAX_ATTEMPTS} waits "
                                                f"{RETRY_MINUTES} min after the last failure",
                        "target": target.isoformat(), "today_open": True}
        except Exception:
            pass

    return {"run": True, "reason": "market traded today and no plan exists for the next "
                                   "session yet", "target": target.isoformat(),
            "attempt": attempts + 1, "today_open": True}


# ── the build ───────────────────────────────────────────────────────────────

def run_once(force: bool = False, now: Optional[_dt.datetime] = None) -> Dict:
    """Do one evening's work: confirm today traded, build both plans for the next
    trading session, and score the session that just finished."""
    now = now or _now()
    d = decide(now)
    if not d.get("run") and not force:
        return {"ok": True, "built": False, **d}

    today = now.date()
    target = _dt.date.fromisoformat(d["target"]) if d.get("target") else cal.next_trading_day(today)
    st = _read()
    st["last_attempt_at"] = now.isoformat(timespec="seconds")
    att = st.setdefault("attempts_for", {})
    att[target.isoformat()] = att.get(target.isoformat(), 0) + 1
    _write(st)

    out: Dict = {"ok": True, "built": True, "target": target.isoformat(),
                 "started_at": now.isoformat(timespec="seconds")}

    # Confirm today's bhavcopy exists — without it the universe silently ranks on
    # yesterday's turnover. Also teaches the calendar that today really traded.
    try:
        import universe as _u
        rows = _u.fetch_bhavcopy(today)
        out["bhavcopy_today"] = bool(rows)
        cal.note_observed(today, bool(rows))
        if not rows and not force:
            st["last_status"] = "waiting_bhavcopy"
            st["last_error"] = "today's bhavcopy is not published yet"
            _write(st)
            return {**out, "built": False, "ok": True,
                    "reason": "today's bhavcopy is not out yet — will retry in "
                              f"{RETRY_MINUTES} min rather than rank on stale turnover"}
    except Exception as e:                                       # noqa: BLE001
        out["bhavcopy_today"] = None
        out["bhavcopy_error"] = str(e)[:150]

    errors = {}
    try:
        import plan_pipeline
        out["day"] = plan_pipeline.build_daily_plans(for_date=target.isoformat(), top_n=TOP_N)
    except Exception as e:                                       # noqa: BLE001
        traceback.print_exc(); errors["day"] = str(e)[:250]
    try:
        import swing_pipeline
        out["swing"] = swing_pipeline.build_swing_plans(for_date=target.isoformat(), top_n=TOP_N)
    except Exception as e:                                       # noqa: BLE001
        traceback.print_exc(); errors["swing"] = str(e)[:250]

    # Keep the scorecard current without anyone clicking "Score date".
    #
    # It scores the PREVIOUS session, not today's, and that is deliberate: both
    # scorers reject a date >= today with `not_yet_traded`, and the candle
    # fetchers exclude today's still-forming bar. Asking for today at 19:00
    # would fail every single night. So the scorecard always trails the plan by
    # one session — today's result lands in tomorrow evening's run.
    try:
        import plan_pipeline, swing_pipeline
        prev = cal.prev_trading_day(today)
        out["scored_for"] = prev.isoformat()
        out["scored_day"] = plan_pipeline.score_daily_plans(prev.isoformat())
        out["scored_swing"] = swing_pipeline.score_swing_plans(prev.isoformat())
    except Exception as e:                                       # noqa: BLE001
        errors["score"] = str(e)[:250]

    ok = not errors.get("day") and not errors.get("swing")
    st = _read()
    st["last_run_at"] = _now().isoformat(timespec="seconds")
    st["last_status"] = "ok" if ok else "error"
    st["last_error"] = json.dumps(errors)[:500] if errors else None
    if ok:
        st["last_built_for"] = target.isoformat()
        st.setdefault("attempts_for", {}).pop(target.isoformat(), None)
        st["history"] = ([{"target": target.isoformat(),
                           "at": st["last_run_at"],
                           "day_built": (out.get("day") or {}).get("built"),
                           "swing_built": (out.get("swing") or {}).get("built")}]
                         + (st.get("history") or []))[:30]
    _write(st)
    out["errors"] = errors or None
    out["ok"] = ok
    return out


# ── the loop ────────────────────────────────────────────────────────────────

_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _loop():
    # Let the app finish booting before the first probe.
    _stop.wait(45)
    while not _stop.is_set():
        try:
            d = decide()
            if d.get("run"):
                print(f"[auto_build] building plans for {d.get('target')} "
                      f"(attempt {d.get('attempt')})", flush=True)
                res = run_once()
                print(f"[auto_build] done: ok={res.get('ok')} built={res.get('built')} "
                      f"day={(res.get('day') or {}).get('built')} "
                      f"swing={(res.get('swing') or {}).get('built')} "
                      f"errors={res.get('errors')}", flush=True)
        except Exception:                                        # noqa: BLE001
            traceback.print_exc()
        _stop.wait(TICK_SECONDS)


def start() -> Dict:
    """Start the nightly builder. Called once from the app's startup hook."""
    global _thread
    if not ENABLED:
        return {"ok": True, "running": False, "reason": "AUTO_BUILD=0"}
    if _thread and _thread.is_alive():
        return {"ok": True, "running": True, "already": True}
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="auto-build", daemon=True)
    _thread.start()
    return {"ok": True, "running": True,
            "build_time_ist": f"{BUILD_HOUR:02d}:{BUILD_MINUTE:02d}", "top_n": TOP_N}


def stop() -> Dict:
    _stop.set()
    return {"ok": True, "running": False}


def status() -> Dict:
    st = _read()
    return {
        "enabled": ENABLED,
        "running": bool(_thread and _thread.is_alive()),
        "build_time_ist": f"{BUILD_HOUR:02d}:{BUILD_MINUTE:02d}",
        "top_n": TOP_N,
        "retry_minutes": RETRY_MINUTES, "max_attempts": MAX_ATTEMPTS,
        "last_run_at": st.get("last_run_at"),
        "last_built_for": st.get("last_built_for"),
        "last_status": st.get("last_status"),
        "last_error": st.get("last_error"),
        "history": (st.get("history") or [])[:10],
        "decision_now": decide(),
        "calendar": cal.status(),
    }
