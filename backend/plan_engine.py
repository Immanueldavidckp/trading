"""
plan_engine.py — turns candles into a full, machine-checkable intraday day-plan.

Implements the India intraday framework layer from the compendium
(short_term_trading_compendium_india.md): previous-day levels, CPR (narrow/wide),
Camarilla, classic pivots, gap logic, ATR/RVOL, ORB, open-type -> day-type
selection, VWAP-reversion, break-&-retest, and an SMC/FVG confluence overlay
(reusing smc_engine). Every setup carries NUMERIC trigger/entry/stop/targets so
plan_score.py can replay it against the next day's actual candles.

A plan is generated in the EVENING for the NEXT session, so tomorrow's open is
unknown. The plan is therefore a DECISION TREE keyed off where price opens
relative to today's levels — exactly how a real pre-market plan works.

Nothing here executes orders. Everything is analysis; profit is only ever
measured after the fact, net of costs, by plan_score.py.
"""
from __future__ import annotations
from typing import List, Dict, Optional
import math
import datetime as _dt

import smc_engine

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def _ist_date(ms) -> str:
    try:
        return _dt.datetime.fromtimestamp(ms / 1000, _IST).strftime("%Y-%m-%d")
    except Exception:
        return "—"

# ---- cost model (compendium §3): round-trip all-in for a liquid <=Rs.300 name.
# explicit ~8 bps + slippage tier (liquid midcap/F&O) ~ 12-40 bps. Use 25 bps.
ROUND_TRIP_COST_PCT = 0.25          # % of notional, per round trip
DEFAULT_RISK_RUPEES = 1000.0        # illustrative per-trade risk for sizing hints


# ── small indicator helpers (Appendix A formulas) ───────────────────────────

def _ema(vals: List[float], n: int) -> Optional[float]:
    if not vals or len(vals) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def _atr(candles: List[dict], n: int = 14) -> Optional[float]:
    if len(candles) < n + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Wilder smoothing
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr


def _round_levels(price: float) -> List[float]:
    """Nearest behavioural round numbers (Rs.10/5/1 grid depending on price)."""
    step = 10.0 if price >= 200 else 5.0 if price >= 50 else 1.0
    base = math.floor(price / step) * step
    return [round(base, 2), round(base + step, 2)]


def _fmt(x) -> Optional[float]:
    try:
        return round(float(x), 2)
    except Exception:
        return None


# ── the standard pre-market level sets ──────────────────────────────────────

def cpr(H: float, L: float, C: float) -> Dict:
    """Central Pivot Range (compendium §7.2). Narrow CPR -> trend-day odds."""
    P = (H + L + C) / 3.0
    BC = (H + L) / 2.0
    TC = 2 * P - BC
    top, bot = max(TC, BC), min(TC, BC)
    width = top - bot
    return {"P": _fmt(P), "TC": _fmt(top), "BC": _fmt(bot),
            "width": _fmt(width), "width_pct": _fmt(width / C * 100)}


def camarilla(H: float, L: float, C: float) -> Dict:
    """Camarilla bands (§7.2). Fade inside H3-L3; breakout beyond H4/L4."""
    r = H - L
    return {"H5": _fmt(C + r * 1.1 / 2 * 1.168), "H4": _fmt(C + r * 1.1 / 2),
            "H3": _fmt(C + r * 1.1 / 4), "L3": _fmt(C - r * 1.1 / 4),
            "L4": _fmt(C - r * 1.1 / 2), "L5": _fmt(C - r * 1.1 / 2 * 1.168)}


def classic_pivots(H: float, L: float, C: float) -> Dict:
    P = (H + L + C) / 3.0
    return {"P": _fmt(P), "R1": _fmt(2 * P - L), "S1": _fmt(2 * P - H),
            "R2": _fmt(P + (H - L)), "S2": _fmt(P - (H - L)),
            "R3": _fmt(H + 2 * (P - L)), "S3": _fmt(L - 2 * (H - P))}


def _cpr_class(width_pct: float, hist_daily: List[dict]) -> str:
    """Narrow if this CPR width is in the bottom ~40% of the last 60 sessions."""
    widths = []
    for i in range(1, len(hist_daily)):
        d = hist_daily[i]
        pv = hist_daily[i - 1]
        w = abs((2 * ((d["h"] + d["l"] + d["c"]) / 3) - (d["h"] + d["l"]) / 2)
                - (d["h"] + d["l"]) / 2) / d["c"] * 100 if d["c"] else 0
        widths.append(w)
    widths = sorted(widths[-60:])
    if not widths:
        return "unknown"
    q40 = widths[int(len(widths) * 0.40)]
    q70 = widths[int(len(widths) * 0.70)]
    if width_pct <= q40:
        return "narrow"      # compression -> higher trend-day odds
    if width_pct >= q70:
        return "wide"        # prior day rangey -> chop/inside-day expected
    return "normal"


# ── daily bias (compendium §8 mechanical definition) ────────────────────────

def daily_bias(daily: List[dict]) -> Dict:
    closes = [c["c"] for c in daily]
    e20, e50 = _ema(closes, 20), _ema(closes, 50)
    last = closes[-1]
    smc = smc_engine.analyze(daily, swing_lookback=2, recent_n=60)
    struct_bias = smc.get("bias") if smc.get("ok") else None
    if e20 and e50:
        if last > e20 > e50:
            bias = "bullish"
        elif last < e20 < e50:
            bias = "bearish"
        else:
            bias = "neutral"
    else:
        bias = struct_bias or "neutral"
    return {"bias": bias, "ema20": _fmt(e20), "ema50": _fmt(e50),
            "struct_bias": struct_bias, "zone": smc.get("price_zone"),
            "smc": smc}


# ── the plan builder ────────────────────────────────────────────────────────

def build_plan(tsym: str, daily: List[dict],
               intraday: Optional[List[dict]] = None,
               risk_rupees: float = DEFAULT_RISK_RUPEES) -> Dict:
    """Full next-session plan for one symbol. `daily` oldest-first (needs >=20);
    `intraday` optional (5m/15m) for the SMC zone overlay."""
    if not daily or len(daily) < 20:
        return {"ok": False, "tsym": tsym, "error": "need >=20 daily candles"}

    prev = daily[-1]                      # today's completed candle = "previous day" for tomorrow
    PDH, PDL, PDC = prev["h"], prev["l"], prev["c"]
    prev_range = PDH - PDL
    atr = _atr(daily, 14) or prev_range
    bias = daily_bias(daily)
    _cpr = cpr(PDH, PDL, PDC)
    _cpr["class"] = _cpr_class(_cpr["width_pct"], daily)
    _cam = camarilla(PDH, PDL, PDC)
    _piv = classic_pivots(PDH, PDL, PDC)
    rounds = _round_levels(PDC)

    # relative volume of the last day vs its 20-day average (event detector)
    vols = [c.get("v") or 0 for c in daily]
    avg_v = (sum(vols[-21:-1]) / 20.0) if len(vols) >= 21 else (sum(vols) / len(vols))
    rvol = (vols[-1] / avg_v) if avg_v else 1.0

    # SMC zones on the intraday series if present, else on daily
    zbase = intraday if (intraday and len(intraday) >= 20) else daily
    smc_intraday = smc_engine.analyze(zbase, swing_lookback=2, recent_n=150)
    zones = _nearest_zones(smc_intraday, PDC)

    levels = {
        "prev_day": {"PDH": _fmt(PDH), "PDL": _fmt(PDL), "PDC": _fmt(PDC),
                     "range": _fmt(prev_range), "date": _ist_date(prev["t"])},
        "atr14": _fmt(atr), "rvol": _fmt(rvol),
        "cpr": _cpr, "camarilla": _cam, "pivots": _piv,
        "round_numbers": rounds,
        "smc_zones": zones,
    }

    setups = _build_setups(tsym, PDH, PDL, PDC, atr, bias, _cpr, _cam, _piv, zones)
    scenarios = _open_scenarios(PDH, PDL, PDC, _cpr, _cam, bias)
    score = _confluence_score(bias, _cpr, rvol, zones, atr, PDC)

    # sizing hint (compendium §29): shares = risk / stop_distance for the tightest setup
    for s in setups:
        if s.get("stop") is not None and s.get("entry") is not None:
            dist = abs(s["entry"] - s["stop"])
            s["shares_for_risk"] = int(risk_rupees / dist) if dist > 0 else 0
            s["risk_rupees"] = risk_rupees

    return {
        "ok": True, "tsym": tsym.upper(), "last_close": _fmt(PDC),
        "bias": bias["bias"], "bias_detail": {k: bias[k] for k in
                 ("ema20", "ema50", "struct_bias", "zone")},
        "conviction": score["label"], "score": score["score"],
        "score_parts": score["parts"],
        "levels": levels,
        "day_type_hint": _day_type_hint(_cpr, rvol),
        "headline": _headline(bias, _cpr, rvol, setups, _fmt(PDH), _fmt(PDL), _fmt(PDC)),
        "open_scenarios": scenarios,
        "setups": setups,
        "num_setups": len(setups),
        "max_trades": min(3, len([s for s in setups if s["quality"] != "low"])),
        "swing_outlook": _swing_outlook(bias, PDC, atr, _piv, zones),
        "no_trade": _no_trade_rules(_cpr, rvol),
        "risk_rules": _RISK_RULES,
        "cost_note": f"All P&L scored net of ~{ROUND_TRIP_COST_PCT}% round-trip cost "
                     f"(compendium §3). Analysis only — no orders are placed.",
    }


def _nearest_zones(smc: Dict, price: float) -> Dict:
    """Nearest unmitigated FVG + OB above & below price, plus dealing range."""
    out = {"above": [], "below": [], "range": None}
    if not smc.get("ok"):
        return out
    for kind, key in (("FVG", "fvg"), ("OB", "order_blocks")):
        for z in smc.get(key, []):
            if z.get("mitigated"):
                continue
            top, bot = z.get("top"), z.get("bottom")
            if top is None or bot is None:
                continue
            item = {"kind": kind, "dir": z.get("kind") or z.get("dir"),
                    "top": _fmt(top), "bottom": _fmt(bot)}
            if bot > price:
                out["above"].append(item)
            elif top < price:
                out["below"].append(item)
    out["above"].sort(key=lambda z: z["bottom"])
    out["below"].sort(key=lambda z: -z["top"])
    out["above"] = out["above"][:3]
    out["below"] = out["below"][:3]
    r = smc.get("range")
    if r:
        out["range"] = {"high": _fmt(r.get("high")), "low": _fmt(r.get("low")),
                        "eq": _fmt(r.get("eq")), "zone": smc.get("price_zone")}
    return out


def _rr(entry, stop, target):
    try:
        risk = abs(entry - stop)
        return round(abs(target - entry) / risk, 2) if risk else None
    except Exception:
        return None


def _mk(name, side, day_type, trigger_txt, trigger_price, trigger_dir,
        entry, stop, context, valid_when, skip_when, quality, source,
        rr=(1.0, 2.0, 3.0)):
    """Targets are R-multiples of the stop distance (always correct-side and
    monotonic → clean R:R). `context` = nearby structural levels (pivots/PDH…)
    that the targets align with, kept only on the trade's side, for the report."""
    long = side == "LONG"
    R = abs(entry - stop) or 1e-9
    tgts = [entry + m * R for m in rr] if long else [entry - m * R for m in rr]
    ctx = [c for c in (context or [])
           if c is not None and ((c > entry) if long else (c < entry))]
    ctx = sorted(set(ctx), reverse=not long)[:3]
    return {
        "name": name, "side": side, "day_type": day_type,
        "trigger": trigger_txt, "trigger_price": _fmt(trigger_price),
        "trigger_dir": trigger_dir,                 # "above" / "below"
        "entry": _fmt(entry), "stop": _fmt(stop),
        "targets": [_fmt(t) for t in tgts],
        "context_targets": [_fmt(c) for c in ctx],
        "rr_t1": rr[0], "rr_final": rr[-1],
        "risk_per_share": _fmt(R),
        "valid_when": valid_when, "skip_when": skip_when,
        "quality": quality, "source": source,
    }


def _build_setups(tsym, PDH, PDL, PDC, atr, bias, _cpr, _cam, _piv, zones) -> List[Dict]:
    """The concrete, machine-checkable setups. Each is a conditional entry keyed
    off tomorrow's price action relative to today's levels."""
    S = []
    b = bias["bias"]
    buf = 0.5 * atr                          # break/stop buffer

    # 1) ORB — first 15m range break (direction of HTF bias favoured)
    S.append(_mk(
        "Opening Range Breakout (long)", "LONG", "trend",
        f"break & 5m-hold above the first-15m high with RVOL>1.5", PDH, "above",
        entry=PDH + 0.02, stop=PDH - buf,
        context=[_piv["R1"], PDH + prev_ext(PDH, PDL, 1.0), _piv["R2"]],
        valid_when="narrow CPR / gap-up in trend / index aligned up",
        skip_when="wide CPR + no gap (range day) · first 5 min · RVOL<1.2",
        quality="high" if (b != "bearish" and _cpr["class"] == "narrow") else "medium",
        source="ORB §7.1"))
    S.append(_mk(
        "Opening Range Breakout (short)", "SHORT", "trend",
        f"break & 5m-hold below the first-15m low with RVOL>1.5", PDL, "below",
        entry=PDL - 0.02, stop=PDL + buf,
        context=[_piv["S1"], PDL - prev_ext(PDH, PDL, 1.0), _piv["S2"]],
        valid_when="narrow CPR / gap-down in trend / index aligned down",
        skip_when="wide CPR + no gap · first 5 min · RVOL<1.2",
        quality="high" if (b != "bullish" and _cpr["class"] == "narrow") else "medium",
        source="ORB §7.1"))

    # 2) Camarilla fade — open inside H3..L3 (range day)
    S.append(_mk(
        "Camarilla fade — short H3", "SHORT", "range",
        f"reject H3 {_cam['H3']} after opening inside H3–L3", _cam["H3"], "below",
        entry=_cam["H3"], stop=_cam["H4"],
        context=[_cpr["P"], _cam["L3"]],
        valid_when="opens inside H3–L3, wide CPR, low RVOL (range day)",
        skip_when="trend day / breaks H4 with volume (flip to breakout)",
        quality="medium", source="Camarilla §7.2"))
    S.append(_mk(
        "Camarilla fade — long L3", "LONG", "range",
        f"reject L3 {_cam['L3']} after opening inside H3–L3", _cam["L3"], "above",
        entry=_cam["L3"], stop=_cam["L4"],
        context=[_cpr["P"], _cam["H3"]],
        valid_when="opens inside H3–L3, wide CPR, low RVOL (range day)",
        skip_when="trend day / breaks L4 with volume",
        quality="medium", source="Camarilla §7.2"))

    # 3) Break-&-retest of PDH / PDL (higher quality, limit entry)
    S.append(_mk(
        "PDH break-&-retest (long)", "LONG", "trend",
        f"break PDH {_fmt(PDH)}, pull back, HOLD it from above", PDH, "above",
        entry=PDH, stop=PDH - buf,
        context=[_piv["R1"], _piv["R2"]],
        valid_when="bias not bearish · holds on retest",
        skip_when="loses PDH on the retest (failed break)",
        quality="high" if b == "bullish" else "medium", source="Break-retest §5.3"))
    S.append(_mk(
        "PDL break-&-retest (short)", "SHORT", "trend",
        f"break PDL {_fmt(PDL)}, pull back, REJECT it from below", PDL, "below",
        entry=PDL, stop=PDL + buf,
        context=[_piv["S1"], _piv["S2"]],
        valid_when="bias not bullish · rejects on retest",
        skip_when="reclaims PDL on the retest",
        quality="high" if b == "bearish" else "medium", source="Break-retest §5.3"))

    # 4) SMC zone entries — nearest unmitigated demand (long) / supply (short)
    zb = zones.get("below") or []
    za = zones.get("above") or []
    if zb:
        z = zb[0]
        mid = (z["top"] + z["bottom"]) / 2
        S.append(_mk(
            f"SMC demand reaction (long) — {z['kind']}", "LONG", "either",
            f"tag {z['bottom']}–{z['top']} and show a rejection/absorption", z["top"], "below",
            entry=mid, stop=z["bottom"] - 0.3 * atr,
            context=[PDC, _piv["R1"]],
            valid_when="price dips into the zone with a bullish reaction (§10.3 absorption)",
            skip_when="zone slices through on heavy sell aggression",
            quality="medium", source="SMC/FVG §12"))
    if za:
        z = za[0]
        mid = (z["top"] + z["bottom"]) / 2
        S.append(_mk(
            f"SMC supply reaction (short) — {z['kind']}", "SHORT", "either",
            f"tag {z['bottom']}–{z['top']} and show rejection", z["bottom"], "above",
            entry=mid, stop=z["top"] + 0.3 * atr,
            context=[PDC, _piv["S1"]],
            valid_when="price rallies into the zone and stalls",
            skip_when="zone breaks on heavy buy aggression",
            quality="medium", source="SMC/FVG §12"))
    return S


def prev_ext(PDH, PDL, mult):
    return (PDH - PDL) * mult


def _open_scenarios(PDH, PDL, PDC, _cpr, _cam, bias) -> List[Dict]:
    """The decision tree: what to do given WHERE price opens tomorrow."""
    TC, BC, P = _cpr["TC"], _cpr["BC"], _cpr["P"]
    return [
        {"open_where": f"gaps up & holds above CPR-top {TC}",
         "expect": "trend-up day (open-drive)",
         "do": "only longs; buy first pullback to TC/PDH, target R1/R2",
         "avoid": "chasing minute-1 spike — wait for the first pullback"},
        {"open_where": f"opens inside CPR {BC}–{TC}",
         "expect": "balance / range day",
         "do": "fade the edges (Camarilla H3/L3) back toward pivot P",
         "avoid": "breakout trades until it leaves the range on volume"},
        {"open_where": f"gaps down & holds below CPR-bottom {BC}",
         "expect": "trend-down day",
         "do": "only shorts; sell first pullback to BC/PDL, target S1/S2",
         "avoid": "catching the falling knife at the open"},
        {"open_where": f"opens near flat at PDC {PDC}",
         "expect": "undecided — let the first 15–30 min define range",
         "do": "trade the ORB break in the bias direction",
         "avoid": "pre-positioning before the opening range is set"},
    ]


def _headline(bias, _cpr, rvol, setups, PDH, PDL, PDC) -> Dict:
    """A plain-English 'today's read' + primary play: the single clearest
    IF→THEN for the day, so the plan is actionable at a glance."""
    b = bias["bias"]; cls = _cpr["class"]
    order = {"high": 0, "medium": 1, "low": 2}
    prim = sorted(setups, key=lambda s: order.get(s["quality"], 3))[0] if setups else None
    ifthen = []
    if cls == "narrow":
        ifthen.append("Narrow CPR → expect a TREND day. Trade the breakout; don't fade.")
    elif cls == "wide":
        ifthen.append("Wide CPR → expect CHOP. Fade the edges back to pivot, or stand down. Don't chase breakouts.")
    else:
        ifthen.append("Normal CPR → let the first 15–30 min set the range, then trade the break in the bias direction.")
    if b == "bullish":
        ifthen.append(f"Bias is UP → prefer LONGS. Cleanest entry: break & hold above PDH {PDH}, target the pivots above.")
    elif b == "bearish":
        ifthen.append(f"Bias is DOWN → prefer SHORTS. Cleanest entry: break & reject below PDL {PDL}, target the pivots below.")
    else:
        ifthen.append("No daily bias → take only the strongest signal; skip marginal setups.")
    if rvol is not None and rvol < 0.8:
        ifthen.append(f"Volume is light (RVOL {round(rvol,2)}×) → breakouts may fail; size down or wait for volume.")
    elif rvol is not None and rvol >= 1.5:
        ifthen.append(f"Volume is heavy (RVOL {round(rvol,2)}×) → moves have fuel; breakouts more reliable.")
    play = None
    if prim:
        t1 = prim["targets"][0] if prim.get("targets") else None
        play = (f"{prim['name']} ({prim['side']}): enter {prim['entry']}, stop {prim['stop']}, "
                f"first target {t1} ({prim['rr_t1']}R). Skip if: {prim['skip_when']}")
    read = (f"{b.upper()} bias · {cls} CPR · RVOL {round(rvol,2) if rvol is not None else '—'}× → "
            + ("trend/breakout day" if cls == "narrow" else "range/fade day" if cls == "wide" else "wait-for-open day"))
    return {"read": read, "primary_play": play, "if_then": ifthen}


def _day_type_hint(_cpr, rvol) -> str:
    if _cpr["class"] == "narrow":
        return "Narrow CPR → higher odds of a TREND day. Favour breakouts/ORB."
    if _cpr["class"] == "wide":
        return "Wide CPR → prior day was rangey → expect CHOP/inside day. Favour fades, or stand down."
    return "Normal CPR → no strong day-type edge. Let the open type decide."


def _confluence_score(bias, _cpr, rvol, zones, atr, PDC) -> Dict:
    """Compendium §8.2 confluence scoring → a 0–100 conviction number."""
    parts = {}
    parts["htf_bias"] = 25 if bias["bias"] in ("bullish", "bearish") else 5
    parts["cpr"] = 20 if _cpr["class"] == "narrow" else 8 if _cpr["class"] == "normal" else 3
    parts["rvol"] = 20 if rvol >= 1.5 else 12 if rvol >= 1.0 else 4
    nz = len(zones.get("above", [])) + len(zones.get("below", []))
    parts["smc_zones"] = min(20, nz * 6)
    parts["zone_pos"] = 15 if (zones.get("range") and zones["range"].get("zone") in ("premium", "discount")) else 6
    total = sum(parts.values())
    label = "high" if total >= 70 else "medium" if total >= 45 else "low"
    return {"score": total, "label": label, "parts": parts}


def _swing_outlook(bias, PDC, atr, _piv, zones) -> Dict:
    """A 2-day positional read (the 'day after next day' layer). Not minute levels
    — a directional bias + the levels that would confirm/deny it over ~2 sessions."""
    b = bias["bias"]
    up_t = _fmt(PDC + 2 * atr)
    dn_t = _fmt(PDC - 2 * atr)
    if b == "bullish":
        txt = "Hold/longs favoured while above EMA20; add on dips to demand."
    elif b == "bearish":
        txt = "Rallies are to be sold while below EMA20; avoid bottom-fishing."
    else:
        txt = "No swing edge — wait for a daily close beyond the range."
    return {"bias": b, "note": txt,
            "invalidation": _fmt(bias.get("ema20") or PDC),
            "upside_2d": up_t, "downside_2d": dn_t,
            "key_levels": {"R1": _piv["R1"], "S1": _piv["S1"]}}


def _no_trade_rules(_cpr, rvol) -> List[str]:
    rules = [
        "First 5 minutes (09:15–09:20): widest spreads, fake prints — no fresh entries (§7.5).",
        "Lunch 12:30–13:30: RVOL trough, chop — fade-only or stand down.",
        "No fresh entries after 14:30; be flat by 15:10 before forced square-off (§29.3).",
        "Stock locked at circuit / on ASM-GSM: untradeable — skip (§1.3, §2.3).",
        "Event day (RBI/Budget/CPI/results): no entries ±30 min of the event (§30).",
    ]
    if _cpr["class"] == "wide" and rvol < 1.0:
        rules.insert(0, "TODAY'S PROFILE (wide CPR + low RVOL) = classic chop. Best trade is often NO trade.")
    return rules


_RISK_RULES = [
    "Size from the stop: shares = risk_₹ ÷ (entry−stop). Risk ≤ 0.5–1% of capital per trade (§29).",
    "Max 3 concurrent intraday trades; after 2 losers, halve size or stop for the day (§29.3, §30).",
    "Daily kill-switch: net −1.5% to −2% of equity → flatten, no new orders till tomorrow (§30).",
    "Never average a loser; never remove a resting stop.",
]
