"""
plan_score.py — replays a plan_engine plan against the day's ACTUAL candles and
scores it honestly, net of costs (compendium §32 fill rules, §33 metrics).

Fill rules (§32):
  • Trigger: for a "above" setup the trigger is hit when a bar's HIGH ≥ trigger;
    for "below" when a bar's LOW ≤ trigger.
  • Entry fills only if price actually trades at the entry on/after the trigger bar
    (a limit entry that never gets tagged = NO TRADE — not a free fill).
  • After entry, walk bars: stop and target checked each bar. If one bar spans BOTH
    (ambiguous intrabar path) we assume the STOP filled first — the conservative,
    non-self-flattering choice. Gaps trade THROUGH stops (fill at the bar, not the
    stop price) — losses can exceed 1R, exactly like reality.
  • If neither stop nor final target is hit, exit at the session CLOSE.

Profit is reported as per-trade net return %, never as a promise. No orders are
placed anywhere in this system.
"""
from __future__ import annotations
from typing import List, Dict, Optional

from plan_engine import ROUND_TRIP_COST_PCT


def _first_trigger_idx(candles: List[dict], price: float, direction: str) -> Optional[int]:
    for i, c in enumerate(candles):
        if direction == "above" and c["h"] >= price:
            return i
        if direction == "below" and c["l"] <= price:
            return i
    return None


def _entry_fill_idx(candles: List[dict], start: int, entry: float) -> Optional[int]:
    """First bar at/after `start` whose range contains the entry price."""
    for i in range(start, len(candles)):
        if candles[i]["l"] <= entry <= candles[i]["h"]:
            return i
    return None


def score_setup(setup: Dict, candles: List[dict]) -> Dict:
    """Replay ONE setup over the day's intraday candles (oldest-first)."""
    res = {"name": setup["name"], "side": setup["side"], "quality": setup["quality"],
           "status": "not_triggered", "entry": None, "exit": None,
           "gross_pct": None, "net_pct": None, "r_multiple": None,
           "max_favorable_pct": None, "reached": None}
    tp, td = setup.get("trigger_price"), setup.get("trigger_dir")
    entry, stop = setup.get("entry"), setup.get("stop")
    targets = setup.get("targets") or []
    if tp is None or entry is None or stop is None or not candles:
        return res

    ti = _first_trigger_idx(candles, tp, td)
    if ti is None:
        return res
    res["status"] = "triggered"
    fi = _entry_fill_idx(candles, ti, entry)
    if fi is None:
        res["status"] = "triggered_no_fill"     # setup armed but limit never tagged
        return res

    long = setup["side"] == "LONG"
    res["status"] = "filled"
    res["entry"] = entry
    risk = abs(entry - stop) or 1e-9
    final_t = targets[-1] if targets else None
    t1 = targets[0] if targets else None

    exit_px, reached = None, "open"
    best = entry
    for c in candles[fi:]:
        best = max(best, c["h"]) if long else min(best, c["l"])
        hit_stop = (c["l"] <= stop) if long else (c["h"] >= stop)
        hit_t1 = (t1 is not None) and ((c["h"] >= t1) if long else (c["l"] <= t1))
        if hit_stop and hit_t1:
            exit_px, reached = stop, "stop (ambiguous bar → stop first)"
            break
        if hit_stop:
            exit_px, reached = stop, "stop"
            break
        if hit_t1:
            # ride toward final target within the same/next bars, else book T1
            exit_px, reached = t1, "T1"
            if final_t is not None and t1 != final_t:
                for c2 in candles[fi:]:
                    if (c2["h"] >= final_t) if long else (c2["l"] <= final_t):
                        exit_px, reached = final_t, "final target"
                        break
            break
    if exit_px is None:
        exit_px, reached = candles[-1]["c"], "session close"

    gross = ((exit_px - entry) / entry * 100.0) * (1 if long else -1)
    net = gross - ROUND_TRIP_COST_PCT
    mfe = ((best - entry) / entry * 100.0) * (1 if long else -1)
    res.update({"exit": round(exit_px, 2), "reached": reached,
                "gross_pct": round(gross, 3), "net_pct": round(net, 3),
                "r_multiple": round((exit_px - entry) * (1 if long else -1) / risk, 2),
                "max_favorable_pct": round(mfe, 3)})
    return res


def _day_summary(candles: List[dict]) -> Dict:
    o = candles[0]["o"]; c = candles[-1]["c"]
    h = max(x["h"] for x in candles); l = min(x["l"] for x in candles)
    rng = h - l
    return {"open": o, "high": h, "low": l, "close": c,
            "range": round(rng, 2),
            "return_pct": round((c - o) / o * 100, 2) if o else None,
            # trendiness = |close-open| / range: >=0.5 reads as a trend day,
            # below as rotation/chop (simple, defensible day-character proxy)
            "trendiness": round(abs(c - o) / rng, 2) if rng else None}


def plan_checks(plan: Dict, candles: List[dict]) -> Dict:
    """Grade the PLAN's predictions against the actual session — independent of
    whether any simulated trade filled. Each check: predicted vs actual with a
    RIGHT / WRONG verdict (NA/INFO rows don't count toward the rate)."""
    ds = _day_summary(candles)
    L = plan.get("levels") or {}
    pd = L.get("prev_day") or {}; cpr = L.get("cpr") or {}
    piv = L.get("pivots") or {}; cam = L.get("camarilla") or {}
    checks = []

    def add(aspect, predicted, actual, ok):
        checks.append({"aspect": aspect, "predicted": predicted, "actual": actual,
                       "verdict": "RIGHT" if ok else "WRONG"})

    # 1) bias direction vs how the day actually closed
    bias = plan.get("bias")
    up = ds["close"] >= ds["open"]
    if bias in ("bullish", "bearish"):
        add("Bias direction", bias.upper(),
            f"day closed {'UP' if up else 'DOWN'} ({ds['return_pct']:+}% open→close)",
            (bias == "bullish") == up)
    else:
        checks.append({"aspect": "Bias direction", "predicted": "NEUTRAL (no call)",
                       "actual": f"{ds['return_pct']:+}% open→close", "verdict": "NA"})

    # 2) day type predicted from CPR width
    cls = cpr.get("class")
    trendy = ds["trendiness"] is not None and ds["trendiness"] >= 0.5
    if cls == "narrow":
        add("Day type (narrow CPR)", "TREND day",
            f"trendiness {ds['trendiness']} → {'trend' if trendy else 'chop'}", trendy)
    elif cls == "wide":
        add("Day type (wide CPR)", "CHOP / range day",
            f"trendiness {ds['trendiness']} → {'trend' if trendy else 'chop'}", not trendy)

    # 3) which open scenario happened, and did the playbook direction work
    TC, BC = cpr.get("TC"), cpr.get("BC")
    if TC is not None and BC is not None:
        o = ds["open"]
        if o > TC:
            sc, ok = f"gapped up above CPR-top {TC}", ds["close"] > o
            expect = "trend-up day (longs)"
        elif o < BC:
            sc, ok = f"gapped down below CPR-bottom {BC}", ds["close"] < o
            expect = "trend-down day (shorts)"
        else:
            sc, ok = f"opened inside CPR {BC}–{TC}", not trendy
            expect = "balance / range day (fade edges)"
        add("Open scenario", expect, f"{sc}; close {ds['close']} vs open {o}", ok)

    # 4) which planned reaction levels actually traded (informational)
    touched, untouched = [], []
    for name, val in [("PDH", pd.get("PDH")), ("PDL", pd.get("PDL")),
                      ("R1", piv.get("R1")), ("S1", piv.get("S1")),
                      ("H3", cam.get("H3")), ("L3", cam.get("L3"))]:
        if val is None:
            continue
        (touched if ds["low"] <= val <= ds["high"] else untouched).append(f"{name} {val}")
    checks.append({"aspect": "Levels that traded", "predicted": "reaction expected at key levels",
                   "actual": ("touched: " + ", ".join(touched) if touched else "no key level touched")
                             + (f" · missed: {', '.join(untouched)}" if untouched else ""),
                   "verdict": "INFO"})

    right = sum(1 for c in checks if c["verdict"] == "RIGHT")
    wrong = sum(1 for c in checks if c["verdict"] == "WRONG")
    return {"day": ds, "checks": checks, "right": right, "wrong": wrong,
            "success_rate": round(right / (right + wrong), 2) if (right + wrong) else None}


def score_plan(plan: Dict, candles: List[dict]) -> Dict:
    """Score every setup in a plan against the actual day. `candles` = that day's
    intraday series (5m/15m, oldest-first)."""
    if not plan.get("ok"):
        return {"ok": False, "tsym": plan.get("tsym"), "error": "bad plan"}
    setups = [score_setup(s, candles) for s in plan.get("setups", [])]
    filled = [s for s in setups if s["status"] == "filled"]
    triggered = [s for s in setups if s["status"] in ("filled", "triggered", "triggered_no_fill")]

    wins = [s for s in filled if s["net_pct"] is not None and s["net_pct"] > 0]
    losses = [s for s in filled if s["net_pct"] is not None and s["net_pct"] <= 0]
    net_list = [s["net_pct"] for s in filled if s["net_pct"] is not None]

    # "was the plan correct?" = the highest-quality FILLED setup was a net winner
    qorder = {"high": 0, "medium": 1, "low": 2}
    primary = sorted(filled, key=lambda s: qorder.get(s["quality"], 3))[0] if filled else None

    summary = {
        "n_setups": len(setups), "n_triggered": len(triggered),
        "n_filled": len(filled), "n_win": len(wins), "n_loss": len(losses),
        "hit_rate": round(len(wins) / len(filled), 3) if filled else None,
        "avg_win_pct": round(sum(s["net_pct"] for s in wins) / len(wins), 3) if wins else None,
        "avg_loss_pct": round(sum(s["net_pct"] for s in losses) / len(losses), 3) if losses else None,
        "expectancy_pct": round(sum(net_list) / len(net_list), 3) if net_list else None,
        "book_net_pct": round(sum(net_list), 3) if net_list else 0.0,   # equal-unit book
        "profit_factor": _profit_factor(filled),
        "primary": primary,
        "plan_correct": (primary is not None and primary["net_pct"] is not None
                         and primary["net_pct"] > 0),
    }
    return {"ok": True, "tsym": plan["tsym"], "conviction": plan.get("conviction"),
            "score": plan.get("score"), "setups": setups, "summary": summary,
            "analysis": plan_checks(plan, candles)}


def _profit_factor(filled: List[Dict]) -> Optional[float]:
    gains = sum(s["net_pct"] for s in filled if s.get("net_pct", 0) > 0)
    loss = -sum(s["net_pct"] for s in filled if s.get("net_pct", 0) <= 0)
    if loss <= 0:
        return None if gains <= 0 else 999.0
    return round(gains / loss, 2)


def aggregate_scorecard(scored: List[Dict]) -> Dict:
    """Book-level §33 metrics for the day. ONE trade per stock — the highest-quality
    setup that actually triggered+filled (the 'primary'). Conditional long/short
    variants of the same stock are alternatives, never additive, so summing them
    all would fabricate a book; the primary is the honest one-trade-per-name view."""
    primaries = [sc["summary"].get("primary") for sc in scored
                 if sc.get("ok") and sc["summary"].get("primary")]
    all_filled = [p for p in primaries if p and p.get("net_pct") is not None]
    # how many setups filled in total across the book (context, not P&L)
    total_filled = sum(1 for sc in scored if sc.get("ok")
                       for s in sc["setups"] if s["status"] == "filled")
    # plan-quality (checks) aggregates — computed even when no trade filled,
    # because grading the PLAN doesn't require a simulated fill
    succ = [sc["analysis"]["success_rate"] for sc in scored
            if sc.get("ok") and sc.get("analysis")
            and sc["analysis"].get("success_rate") is not None]
    checks_right = sum(sc["analysis"]["right"] for sc in scored
                       if sc.get("ok") and sc.get("analysis"))
    checks_wrong = sum(sc["analysis"]["wrong"] for sc in scored
                       if sc.get("ok") and sc.get("analysis"))
    plan_quality = {
        "avg_plan_success": round(sum(succ) / len(succ), 3) if succ else None,
        "checks_right": checks_right, "checks_wrong": checks_wrong,
        "checks_success": round(checks_right / (checks_right + checks_wrong), 3)
                          if (checks_right + checks_wrong) else None,
    }

    if not all_filled:
        return {"n_plans": len(scored), "n_trades": 0,
                "n_setups_filled_total": total_filled,
                **plan_quality, "note": "no primary setups filled"}
    wins = [s for s in all_filled if s["net_pct"] > 0]
    losses = [s for s in all_filled if s["net_pct"] <= 0]
    nets = [s["net_pct"] for s in all_filled]
    correct = [sc for sc in scored if sc.get("ok") and sc["summary"].get("plan_correct")]
    n = len(all_filled)
    mean = sum(nets) / n
    var = sum((x - mean) ** 2 for x in nets) / n if n > 1 else 0.0
    sd = var ** 0.5
    # §33.2 sample-size gate: trades needed to detect this edge
    need = int((2.8 * sd / mean) ** 2) if mean else None
    return {
        "n_plans": len(scored),
        "n_plans_correct": len(correct),
        "plan_accuracy": round(len(correct) / len(scored), 3) if scored else None,
        **plan_quality,
        "n_setups_filled_total": total_filled,
        "n_trades": n, "n_win": len(wins), "n_loss": len(losses),
        "hit_rate": round(len(wins) / n, 3),
        "avg_win_pct": round(sum(s["net_pct"] for s in wins) / len(wins), 3) if wins else None,
        "avg_loss_pct": round(sum(s["net_pct"] for s in losses) / len(losses), 3) if losses else None,
        "expectancy_pct": round(mean, 3),
        "expectancy_bps": round(mean * 100, 1),
        "book_net_pct": round(sum(nets), 3),
        "profit_factor": _profit_factor(all_filled),
        "cost_pct_per_trade": ROUND_TRIP_COST_PCT,
        "trades_needed_for_significance": need,
        "significant_yet": (need is not None and n >= need and mean > 0),
        "honesty_note": ("Per-trade net expectancy, simulated from actual candles, "
                         "net of costs. Small samples prove nothing (§33.2) — the "
                         "'trades needed' figure is when this edge would be real."),
    }
