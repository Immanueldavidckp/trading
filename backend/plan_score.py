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
import datetime as _dt

import db as _db
from plan_engine import ROUND_TRIP_COST_PCT

_UTC = _dt.timezone.utc


# ── the improved-strategy gates (evidence from 07-08 / 07-10 scorecards) ────
#
# Measured on this system's own replays:
#   • trend setups on a WIDE-CPR day:   18% win, −1.25%/trade   → OFF
#   • fade setups on a NARROW-CPR day:  42% win, −0.11%/trade   → OFF
#   • trend setups WITH the daily bias: 60% win  vs 47% against → against-bias OFF
#   • wide CPR + RVOL<0.5 stocks:       28% win (25/43 of the 07-10 book!) → stand down
# These are exactly the compendium's §7.2/§7.4 playbook-selection rules — the
# plan engine already *said* them in valid_when/no_trade; the scorer now
# ENFORCES them instead of taking every armed setup.

def _gate_setup(setup: Dict, ctx: Dict) -> Optional[str]:
    """Return a skip-reason when this setup must NOT be traded today, else None."""
    cls = ctx.get("cpr_class"); bias = ctx.get("bias"); o = ctx.get("open")
    cam = ctx.get("cam") or {}; cpr = ctx.get("cpr") or {}
    if ctx.get("stand_down"):
        return "no-trade profile: RVOL<0.5 — no volume, breakouts fail and fades drift; stand down"
    dt = setup.get("day_type")
    if dt == "trend" and cls == "wide":
        return "wide CPR → breakout setups OFF (fade day)"
    if dt == "range" and cls == "narrow":
        return "narrow CPR → fade setups OFF (trend day)"
    if dt == "trend":
        if bias == "bullish" and setup.get("side") == "SHORT":
            return "against daily bias (bullish) — trend shorts OFF"
        if bias == "bearish" and setup.get("side") == "LONG":
            return "against daily bias (bearish) — trend longs OFF"
        TC, BC = cpr.get("TC"), cpr.get("BC")
        if o is not None and BC is not None and setup.get("side") == "LONG" and o < BC:
            return "gapped down below CPR — long breakout OFF"
        if o is not None and TC is not None and setup.get("side") == "SHORT" and o > TC:
            return "gapped up above CPR — short breakout OFF"
    if "Camarilla fade" in (setup.get("name") or ""):
        H3, L3 = cam.get("H3"), cam.get("L3")
        if o is not None and H3 is not None and L3 is not None and not (L3 <= o <= H3):
            return f"opened at {o}, outside H3–L3 — fade plan invalid"
    return None


def _tick_gate(tsym: str, bar_ms: int, side: str) -> Dict:
    """Order-flow confirmation from the stored tick-by-tick market_depth:
    around the entry bar, the book imbalance and tick direction must support
    the trade (LONG: imbalance ≥55% & up-ticks ≥ down-ticks; SHORT mirrored).
    Where no depth was recorded (before the universe recorder started, or feed
    down) → 'no_data' and the trade is allowed — the gate only ever removes
    trades the order flow actively contradicts."""
    try:
        t0 = _dt.datetime.fromtimestamp(bar_ms / 1000 - 60, _UTC).replace(tzinfo=None).isoformat(timespec="milliseconds")
        t1 = _dt.datetime.fromtimestamp(bar_ms / 1000 + 240, _UTC).replace(tzinfo=None).isoformat(timespec="milliseconds")
        PH = _db.PLACE
        conn = _db.connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT total_buy_qty,total_sell_qty,ltp FROM market_depth "
                        f"WHERE tsym={PH} AND received_at>={PH} AND received_at<={PH} ORDER BY id",
                        [tsym, t0, t1])
            rows = cur.fetchall(); cur.close()
        finally:
            conn.close()
    except Exception:
        return {"status": "no_data"}
    if len(rows) < 5:
        return {"status": "no_data"}
    buy = sum(r[0] or 0 for r in rows); sell = sum(r[1] or 0 for r in rows)
    tot = buy + sell
    imb = (buy / tot) if tot else 0.5
    ups = downs = 0; prev = None
    for _, _, lp in rows:
        if lp is not None and prev is not None:
            if lp > prev: ups += 1
            elif lp < prev: downs += 1
        if lp is not None: prev = lp
    ok = (imb >= 0.55 and ups >= downs) if side == "LONG" else (imb <= 0.45 and downs >= ups)
    return {"status": "passed" if ok else "failed",
            "imbalance": round(imb, 3), "up_ticks": ups, "down_ticks": downs}


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


def score_setup(setup: Dict, candles: List[dict],
                ctx: Optional[Dict] = None, tsym: Optional[str] = None) -> Dict:
    """Replay ONE setup over the day's intraday candles (oldest-first).
    With `ctx`, the playbook gates run first (a gated setup is never a trade);
    with `tsym`, the tick-by-tick order-flow gate runs at the entry bar."""
    res = {"name": setup["name"], "side": setup["side"], "quality": setup["quality"],
           "status": "not_triggered", "entry": None, "exit": None,
           "gross_pct": None, "net_pct": None, "r_multiple": None,
           "max_favorable_pct": None, "reached": None,
           "filter_reason": None, "tick_gate": None}
    if ctx is not None:
        reason = _gate_setup(setup, ctx)
        if reason:
            res["status"] = "skipped_by_filter"
            res["filter_reason"] = reason
            return res
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

    if tsym:
        tg = _tick_gate(tsym, candles[fi]["t"], setup["side"])
        res["tick_gate"] = tg
        if tg.get("status") == "failed":
            res["status"] = "tick_gate_failed"   # order flow contradicted the entry
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
    L = plan.get("levels") or {}
    ctx = {"cpr_class": (L.get("cpr") or {}).get("class"),
           "bias": plan.get("bias"),
           "open": candles[0]["o"] if candles else None,
           "cam": L.get("camarilla"), "cpr": L.get("cpr"),
           # RVOL<0.5 is a stand-down on ANY CPR class: 07-10 proved a dead-volume
           # day kills breakouts and fades alike (25/43 primaries, 28% win) —
           # narrow-CPR "trend days" without volume are failed-breakout factories.
           "stand_down": (L.get("rvol") or 1.0) < 0.5}
    setups = [score_setup(s, candles, ctx, plan.get("tsym")) for s in plan.get("setups", [])]
    filled = [s for s in setups if s["status"] == "filled"]
    triggered = [s for s in setups if s["status"] in ("filled", "triggered", "triggered_no_fill")]
    n_gated = sum(1 for s in setups if s["status"] == "skipped_by_filter")
    n_tickfail = sum(1 for s in setups if s["status"] == "tick_gate_failed")

    wins = [s for s in filled if s["net_pct"] is not None and s["net_pct"] > 0]
    losses = [s for s in filled if s["net_pct"] is not None and s["net_pct"] <= 0]
    net_list = [s["net_pct"] for s in filled if s["net_pct"] is not None]

    # "was the plan correct?" = the highest-quality FILLED setup was a net winner
    qorder = {"high": 0, "medium": 1, "low": 2}
    primary = sorted(filled, key=lambda s: qorder.get(s["quality"], 3))[0] if filled else None

    summary = {
        "n_setups": len(setups), "n_triggered": len(triggered),
        "n_skipped_filter": n_gated, "n_tick_gate_failed": n_tickfail,
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
    decision = decision_flow(plan, candles, ctx, setups)
    for s in setups:
        s["selected"] = (s["name"] == decision["selected"])
    return {"ok": True, "tsym": plan["tsym"], "conviction": plan.get("conviction"),
            "score": plan.get("score"), "setups": setups, "summary": summary,
            "decision": decision, "analysis": plan_checks(plan, candles)}


def _profit_factor(filled: List[Dict]) -> Optional[float]:
    gains = sum(s["net_pct"] for s in filled if s.get("net_pct", 0) > 0)
    loss = -sum(s["net_pct"] for s in filled if s.get("net_pct", 0) <= 0)
    if loss <= 0:
        return None if gains <= 0 else 999.0
    return round(gains / loss, 2)


def decision_flow(plan: Dict, candles: List[dict], ctx: Dict, scored: List[Dict]) -> Dict:
    """The day as a SEQUENTIAL decision (compendium §7.4), not 5 parallel bets:
      1. Opening — gap up / down / inside prev-day range.
      2. First 15 min — did it drive or rotate; where is price vs CPR.
      3. Playbook — pick the ONE setup the open+structure selects (or stand down).
      4. Execution — only that selected setup can be a LOSS. No selection, or a
         selected setup that never triggers, is NO-TRADE (not a loss).
    This is why 5 setups all 'failing' in the flat replay does NOT mean the day was
    a 100% loss — you only take the one the flow chooses."""
    L = plan.get("levels") or {}
    cpr = L.get("cpr") or {}
    TC, BC = cpr.get("TC"), cpr.get("BC")
    pd = L.get("prev_day") or {}
    pdc, pdh, pdl = pd.get("PDC"), pd.get("PDH"), pd.get("PDL")
    obs = [z for k in ("above", "below") for z in ((L.get("smc_zones") or {}).get(k) or [])
           if z.get("kind") == "OB" and z.get("top") is not None and z.get("bottom") is not None]
    cps = []
    def add(n, name, done, verdict, result):
        cps.append({"n": n, "name": name, "done": done, "verdict": verdict, "result": result})

    if not candles:
        for n, nm in [(1, "Opening"), (2, "First 15 min"), (3, "Playbook"), (4, "Execution")]:
            add(n, nm, False, "pending", "waiting for the session to open")
        return {"checkpoints": cps, "selected": None, "selected_setup": None,
                "daytype": None, "outcome": "waiting"}

    op, cur = candles[0]["o"], candles[-1]["c"]
    gap = ((op - pdc) / pdc * 100) if pdc else 0.0
    gtype = "gap-up" if gap > 0.3 else "gap-down" if gap < -0.3 else "flat"
    within = (pdl is not None and pdh is not None and pdl <= op <= pdh)
    add(1, "Opening", True, "info",
        f"{gtype} {gap:+.2f}% — opened {'inside' if within else 'outside'} prev-day range")

    b0 = candles[0]; or_dir = "up" if b0["c"] >= b0["o"] else "down"
    rng = b0["h"] - b0["l"]; drove = rng > 0 and abs(b0["c"] - b0["o"]) / rng > 0.55
    loc = "above CPR" if (TC and cur > TC) else "below CPR" if (BC and cur < BC) else "inside CPR"
    add(2, "First 15 min", True, "info",
        f"{'drove ' + or_dir if drove else 'rotated'} in the opening bar; price now {loc}")

    stand = ctx.get("stand_down")
    ob_hit = next((z for z in obs if z["bottom"] <= cur <= z["top"]), None)
    daytype = None; want_side = None; want_kind = None
    if stand:
        daytype = "stand-down"
    elif ob_hit:
        bull = str(ob_hit.get("dir", "")).lower().startswith("bull")
        daytype = "OB reaction"; want_kind = "SMC"; want_side = "LONG" if bull else "SHORT"
    elif (TC and cur > TC) and (gtype == "gap-up" or or_dir == "up"):
        daytype = "trend-up"; want_side = "LONG"; want_kind = "trend"
    elif (BC and cur < BC) and (gtype == "gap-down" or or_dir == "down"):
        daytype = "trend-down"; want_side = "SHORT"; want_kind = "trend"
    elif loc == "inside CPR" or ctx.get("cpr_class") == "wide":
        daytype = "range"; want_kind = "fade"
    else:
        daytype = "undecided"

    valid = [s for s in scored if s.get("status") != "skipped_by_filter"]
    selected = None
    if want_kind == "SMC":
        selected = next((s for s in valid if "SMC" in s["name"] and s["side"] == want_side), None)
    elif want_kind == "trend":
        selected = next((s for s in valid if s["side"] == want_side and
                         ("Breakout" in s["name"] or "break-&-retest" in s["name"])), None)
    elif want_kind == "fade":
        fades = sorted([s for s in valid if "Camarilla" in s["name"]],
                       key=lambda s: abs((s.get("entry") or cur) - cur))
        selected = fades[0] if fades else None
    sel_name = selected["name"] if selected else None
    add(3, "Playbook", True, "info",
        (f"{daytype} → take: {sel_name}" if selected else
         ("stand down — no trade today (not a loss)" if daytype in ("stand-down", "undecided")
          else f"{daytype} — no valid setup matches")))

    if not selected:
        outcome = "no-trade"
        add(4, "Execution", True, "ok", "no trade taken → cannot be a loss")
    else:
        stt = selected["status"]; reached = (selected.get("reached") or "").lower()
        net = selected.get("net_pct")
        if stt == "filled" and "stop" in reached:
            outcome = "loss"; add(4, "Execution", True, "fail", f"{selected['side']} taken → hit stop = LOSS")
        elif stt == "filled" and ("target" in reached or (net is not None and net > 0)):
            outcome = "win"; add(4, "Execution", True, "ok",
                                 f"{selected['side']} taken → WIN {net:+.2f}%" if net is not None else f"{selected['side']} → target")
        elif stt in ("filled", "triggered", "triggered_no_fill"):
            outcome = "pending"; add(4, "Execution", False, "pending", f"{selected['side']} in progress — not resolved")
        else:
            outcome = "waiting"; add(4, "Execution", False, "pending",
                                     f"waiting for {selected['side']} trigger at {selected.get('trigger_price')}")
    return {"checkpoints": cps, "selected": sel_name, "selected_setup": selected,
            "daytype": daytype, "outcome": outcome}


def aggregate_scorecard(scored: List[Dict]) -> Dict:
    """Book-level §33 metrics. ONE decision-flow trade per stock (open → 15m →
    playbook → execute). Only the SELECTED setup can win or lose; stocks the flow
    stood down on (no-trade) are NOT losses — that is the whole point: 5 setups
    all failing in the parallel replay is not a lost day when you'd only take one."""
    decs = [sc.get("decision") for sc in scored if sc.get("ok") and sc.get("decision")]
    def _net(d):
        ss = d.get("selected_setup") or {}
        return ss.get("net_pct")
    won = [d for d in decs if d.get("outcome") == "win"]
    lost = [d for d in decs if d.get("outcome") == "loss"]
    no_trade = [d for d in decs if d.get("outcome") == "no-trade"]
    pending = [d for d in decs if d.get("outcome") in ("pending", "waiting")]
    taken = [d for d in (won + lost) if _net(d) is not None]

    succ = [sc["analysis"]["success_rate"] for sc in scored
            if sc.get("ok") and sc.get("analysis") and sc["analysis"].get("success_rate") is not None]
    checks_right = sum(sc["analysis"]["right"] for sc in scored if sc.get("ok") and sc.get("analysis"))
    checks_wrong = sum(sc["analysis"]["wrong"] for sc in scored if sc.get("ok") and sc.get("analysis"))
    plan_quality = {
        "avg_plan_success": round(sum(succ) / len(succ), 3) if succ else None,
        "checks_right": checks_right, "checks_wrong": checks_wrong,
        "checks_success": round(checks_right / (checks_right + checks_wrong), 3)
                          if (checks_right + checks_wrong) else None,
        "n_no_trade": len(no_trade), "n_pending": len(pending),
        "n_skipped_filter": sum(sc["summary"].get("n_skipped_filter", 0) for sc in scored if sc.get("ok")),
    }

    if not taken:
        return {"n_plans": len(scored), "n_trades": 0, **plan_quality,
                "note": f"{len(no_trade)} stocks stood down (no-trade), {len(pending)} still pending — no completed trades"}

    nets = [_net(d) for d in taken]
    n = len(taken)
    mean = sum(nets) / n
    var = sum((x - mean) ** 2 for x in nets) / n if n > 1 else 0.0
    sd = var ** 0.5
    need = int((2.8 * sd / mean) ** 2) if mean else None
    return {
        "n_plans": len(scored),
        "n_trades": n, "n_win": len(won), "n_loss": len(lost),
        "hit_rate": round(len(won) / n, 3) if n else None,
        # plan_accuracy now = win rate of the ONE selected trade per stock that resolved
        "n_plans_correct": len(won),
        "plan_accuracy": round(len(won) / n, 3) if n else None,
        **plan_quality,
        "avg_win_pct": round(sum(_net(d) for d in won if _net(d) is not None) / max(1, len(won)), 3) if won else None,
        "avg_loss_pct": round(sum(_net(d) for d in lost if _net(d) is not None) / max(1, len(lost)), 3) if lost else None,
        "expectancy_pct": round(mean, 3),
        "expectancy_bps": round(mean * 100, 1),
        "book_net_pct": round(sum(nets), 3),
        "profit_factor": _profit_factor([{"net_pct": x} for x in nets]),
        "cost_pct_per_trade": ROUND_TRIP_COST_PCT,
        "trades_needed_for_significance": need,
        "significant_yet": (need is not None and n >= need and mean > 0),
        "honesty_note": ("One decision-flow trade per stock (open → 15m → playbook → execute); "
                         "stood-down stocks are no-trade, not losses. Net of costs; small samples "
                         "prove nothing (§33.2)."),
    }
