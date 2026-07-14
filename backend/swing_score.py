"""
swing_score.py — replays a swing_engine plan against the ACTUAL daily candles
that followed it and scores it honestly, net of delivery costs.

Fill rules (deliberately conservative; every rule an adversarial review forced):
  • Every setup is a next-session BUY-STOP with a hard LIMIT CAP. The trigger
    must fire within the entry window (2 sessions) or the plan is STALE = no
    trade (not a loss).
  • Fill price = max(open, trigger) — a gap over the trigger fills WORSE, like
    reality. If the day OPENS beyond the limit cap the stop-limit cannot fill:
    no trade that day (and gap_skipped if it never fills in the window). No
    fantasy fills at prices that never traded.
  • After entry, walk DAILY bars: if a bar opens beyond the stop, the exit is
    the OPEN (gaps trade through stops — losses can exceed 1R). If one bar
    spans both stop and target, the STOP is assumed first.
  • T1 is a SINGLE FULL exit — partial booking on ₹15–25k positions violates
    the DP-fee floor, so the plan and the replay both exit all at T1.
  • No-progress rule (S1): checked at the close of session N; the exit happens
    at the NEXT session's open — you cannot observe a close and then trade at
    that same close (NSE's close is the last-30-min VWAP).
  • Time stop: date known in advance, so exiting at that session's close is
    legitimate (the order works the closing session).
  • If the data simply ran out, the position is "open" with unrealized P&L —
    never counted as a win.

Nothing here places orders; profit is only ever a measured, cost-adjusted
replay — not a promise.
"""
from __future__ import annotations
from typing import List, Dict, Optional

from swing_engine import SWING_ROUND_TRIP_COST_PCT


def score_setup(setup: Dict, candles: List[dict]) -> Dict:
    """Replay ONE swing setup over the daily candles AFTER the plan date
    (oldest-first)."""
    res = {"name": setup.get("name"), "side": setup.get("side"), "type": setup.get("type"),
           "quality": setup.get("quality"), "status": "not_triggered",
           "entry": None, "exit": None, "entry_day": None, "exit_day": None,
           "days_held": None, "gross_pct": None, "net_pct": None,
           "r_multiple": None, "reached": None, "max_favorable_pct": None}
    tp = setup.get("trigger_price")
    limit = setup.get("limit_price") or (tp * 1.02 if tp else None)
    stop = setup.get("stop")
    targets = setup.get("targets") or []
    window = int(setup.get("entry_window_sessions") or 2)
    time_stop = int(setup.get("time_stop_sessions") or 10)
    noprog = setup.get("no_progress")
    if tp is None or stop is None or not candles:
        return res

    # ── entry: stop-limit inside the window ──
    fi = None; fill = None; gapped = False
    for i, c in enumerate(candles[:window]):
        if c["o"] > limit:
            gapped = True          # opened beyond the cap — order can't fill today
            continue
        if c["h"] >= tp:
            fill = max(c["o"], tp)          # gap over trigger fills worse
            if fill > limit:                # paranoid: cap is a hard ceiling
                gapped = True
                continue
            fi = i
            break
    if fi is None:
        res["status"] = "gap_skipped" if gapped else "stale"
        if gapped:
            res["reached"] = "opened beyond the limit cap — no chase, no trade"
        return res

    res["status"] = "filled"
    res["entry"] = round(fill, 2)
    res["entry_day"] = candles[fi]["t"]
    risk = abs(fill - stop) or 1e-9
    t1 = targets[0] if targets else None

    exit_px, reached, xi = None, None, None
    best = fill
    pending_open_exit = None       # set when a close-conditioned rule fires
    for j in range(fi, len(candles)):
        c = candles[j]
        held = j - fi
        if pending_open_exit:
            exit_px, reached, xi = c["o"], pending_open_exit, j
            break
        best = max(best, c["h"])
        if c["o"] <= stop:                                   # gap through the stop
            exit_px, reached, xi = c["o"], "gap through stop", j
            break
        hit_stop = c["l"] <= stop
        hit_t1 = t1 is not None and c["h"] >= t1
        if hit_stop and hit_t1:
            exit_px, reached, xi = stop, "stop (ambiguous bar → stop first)", j
            break
        if hit_stop:
            exit_px, reached, xi = stop, "stop", j
            break
        if hit_t1:
            exit_px, reached, xi = t1, "T1 (full exit)", j
            break
        if held >= time_stop:                                # date-based, known ex ante
            exit_px, reached, xi = c["c"], f"time stop ({time_stop} sessions)", j
            break
        if (noprog and held == int(noprog.get("after_sessions") or 0)
                and (c["c"] - fill) / fill * 100 < float(noprog.get("min_gain_pct") or 0)):
            pending_open_exit = f"no progress by day {held} — exit next open"

    if exit_px is None:
        res["status"] = "open"
        last = candles[-1]["c"]
        gross = (last - fill) / fill * 100
        res.update({"exit": round(last, 2),
                    "reached": (pending_open_exit or "still open") + " (unrealized)",
                    "exit_day": candles[-1]["t"], "days_held": len(candles) - 1 - fi,
                    "gross_pct": round(gross, 3),
                    "net_pct": round(gross - SWING_ROUND_TRIP_COST_PCT, 3),
                    "r_multiple": round((last - fill) / risk, 2),
                    "max_favorable_pct": round((best - fill) / fill * 100, 3)})
        return res

    gross = (exit_px - fill) / fill * 100
    res.update({"exit": round(exit_px, 2), "reached": reached,
                "exit_day": candles[xi]["t"], "days_held": xi - fi,
                "gross_pct": round(gross, 3),
                "net_pct": round(gross - SWING_ROUND_TRIP_COST_PCT, 3),
                "r_multiple": round((exit_px - fill) / risk, 2),
                "max_favorable_pct": round((best - fill) / fill * 100, 3)})
    return res


def score_plan(plan: Dict, candles: List[dict]) -> Dict:
    """Score a whole swing plan. `candles` = completed DAILY candles from the
    plan date onward (oldest-first). One PRIMARY setup per stock decides the
    outcome — the others are informational, exactly like the day system."""
    if not plan.get("ok"):
        return {"ok": False, "tsym": plan.get("tsym"), "error": "bad plan"}
    setups = [score_setup(s, candles) for s in (plan.get("setups") or [])]

    qorder = {"high": 0, "medium": 1, "low": 2}
    ranked = sorted(zip(plan.get("setups") or [], setups),
                    key=lambda p: qorder.get(p[0].get("quality"), 3))
    primary = ranked[0][1] if ranked else None
    for s in setups:
        s["selected"] = primary is not None and s is primary

    if primary is None:
        outcome = "no-plan"
    elif primary["status"] in ("stale", "not_triggered", "gap_skipped"):
        outcome = "no-trade"
    elif primary["status"] == "open":
        outcome = "open"
    elif primary["status"] == "filled":
        outcome = "win" if (primary.get("net_pct") or 0) > 0 else "loss"
    else:
        outcome = "no-trade"

    # plan-quality checks: did the stated read verify over the replayed window?
    checks = []
    if candles and plan.get("bias") in ("bullish", "bearish"):
        last = candles[-1]["c"]; ref = plan.get("last_close") or candles[0]["o"]
        drift = (last - ref) / ref * 100 if ref else 0
        ok = (drift > 0) == (plan["bias"] == "bullish")
        checks.append({"aspect": "Swing bias direction",
                       "predicted": plan["bias"].upper(),
                       "actual": f"{drift:+.2f}% over {len(candles)} session(s)",
                       "verdict": "RIGHT" if ok else "WRONG"})
    if primary and primary["status"] == "filled" and ranked:
        exp = ranked[0][0].get("expected_hold_sessions")
        if exp and primary.get("days_held") is not None:
            checks.append({"aspect": "Holding period (heuristic)",
                           "predicted": f"~{exp} sessions to target",
                           "actual": f"{primary['days_held']} sessions ({primary['reached']})",
                           "verdict": "INFO"})
    right = sum(1 for c in checks if c["verdict"] == "RIGHT")
    wrong = sum(1 for c in checks if c["verdict"] == "WRONG")

    return {"ok": True, "tsym": plan["tsym"], "conviction": plan.get("conviction"),
            "score": plan.get("score"), "outcome": outcome,
            "setups": setups, "primary": primary,
            "net_pct": primary.get("net_pct") if primary else None,
            "analysis": {"checks": checks, "right": right, "wrong": wrong,
                         "sessions_scored": len(candles)}}


def aggregate_scorecard(scored: List[Dict]) -> Dict:
    """Book-level swing metrics: one primary trade per stock; stale/gap-skipped
    plans are no-trades, not losses; open positions reported separately. This is
    also how the score weights (which are priors) get measured against reality."""
    ok = [s for s in scored if s.get("ok")]
    wins = [s for s in ok if s["outcome"] == "win"]
    losses = [s for s in ok if s["outcome"] == "loss"]
    opens = [s for s in ok if s["outcome"] == "open"]
    nots = [s for s in ok if s["outcome"] in ("no-trade", "no-plan")]
    taken = wins + losses
    nets = [s["net_pct"] for s in taken if s.get("net_pct") is not None]
    holds = [s["primary"]["days_held"] for s in taken
             if s.get("primary") and s["primary"].get("days_held") is not None]

    gains = sum(x for x in nets if x > 0)
    lost = -sum(x for x in nets if x <= 0)
    pf = (round(gains / lost, 2) if lost > 0 else (999.0 if gains > 0 else None))

    right = sum(s["analysis"]["right"] for s in ok if s.get("analysis"))
    wrong = sum(s["analysis"]["wrong"] for s in ok if s.get("analysis"))

    return {
        "n_plans": len(ok),
        "n_trades": len(taken), "n_win": len(wins), "n_loss": len(losses),
        "n_open": len(opens), "n_no_trade": len(nots),
        "hit_rate": round(len(wins) / len(taken), 3) if taken else None,
        "avg_win_pct": round(sum(s["net_pct"] for s in wins) / len(wins), 3) if wins else None,
        "avg_loss_pct": round(sum(s["net_pct"] for s in losses) / len(losses), 3) if losses else None,
        "expectancy_pct": round(sum(nets) / len(nets), 3) if nets else None,
        "book_net_pct": round(sum(nets), 3) if nets else 0.0,
        "profit_factor": pf,
        "avg_days_held": round(sum(holds) / len(holds), 1) if holds else None,
        "open_unrealized_pct": round(sum(s["net_pct"] for s in opens
                                         if s.get("net_pct") is not None), 3) if opens else None,
        "checks_right": right, "checks_wrong": wrong,
        "checks_success": round(right / (right + wrong), 3) if (right + wrong) else None,
        "cost_pct_per_trade": SWING_ROUND_TRIP_COST_PCT,
        "honesty_note": ("One primary swing per stock; stale/gap-skipped plans are no-trades, "
                         "not losses; open positions are unrealized, never wins. Net of "
                         f"~{SWING_ROUND_TRIP_COST_PCT}% delivery costs. The confluence-score "
                         "weights are priors — this scorecard is how they get validated. "
                         "Small samples prove nothing."),
    }
