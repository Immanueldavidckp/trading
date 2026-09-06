"""
swing_smc.py — the Smart-Money-Concepts + volume-profile layer for the SWING
plan (2–22 session holds, i.e. up to about a month).

The momentum engine in swing_engine.py answers "is this stock strong enough to
own?". It does not answer "where exactly do I get in, and where do I get out?"
— its entries are all buy-stops above today's high and its targets are flat R
multiples. That is what this module adds, on the DAILY timeframe:

  * Order Blocks     — the last opposite-colour candle before the impulse that
                       broke structure; the zone institutions left behind.
  * Fair Value Gaps  — 3-candle imbalance. Carries its CE (consequent
                       encroachment, the 50% line) because a deep swing pullback
                       usually rebalances to CE rather than the full gap.
  * Refined zones    — an OB that OVERLAPS an FVG. The overlap is the highest-
                       probability pocket and gives a much tighter stop.
  * Breaker blocks   — a supply OB that price closed through; it flips to
                       support ("bullish breaker").
  * Dealing range    — premium / equilibrium / discount, plus the OTE band
                       (0.618–0.79). Longs are only taken at a DISCOUNT: this
                       is the filter that separates an OB entry from chasing.
  * Liquidity sweeps — SSL (sell-side liquidity) grabs below a prior swing low
                       that close back above it: a stop hunt, and the classic
                       swing long trigger.
  * Volume profile   — a COMPOSITE profile built from daily candles (the one in
                       orderflow.py is tick-based and intraday-only, so it can
                       never see a swing's price history). Gives POC, Value Area
                       (VAH/VAL), HVN shelves for stop placement, LVNs that
                       price travels through fast, and naked/virgin POCs — a
                       prior period's POC never traded back to, which is one of
                       the better swing targets there is.
  * HTF alignment    — weekly Weinstein stage, so a month-long hold is not taken
                       against the weekly trend.

Everything here is point-in-time: it reads only the daily candles handed to it.
Nothing places orders.
"""
from __future__ import annotations
from typing import List, Dict, Optional, Tuple
import datetime as _dt

import smc_engine
from orderflow import _poc_index, _value_area, _hvn_lvn

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

VP_LOOKBACK = 126           # ~6 months of sessions in the composite profile
VP_ROWS = 60                # price rows in the profile
SMC_RECENT = 90             # SMC decays — only the last ~4.5 months of structure
NPOC_PERIOD = 21            # one "month" of sessions per naked-POC candidate
SWEEP_FRESH_SESSIONS = 5    # a liquidity sweep older than this is not a trigger
ZONE_MAX_DIST_ATR = 4.0     # a zone further than this below price is not tradeable
MIN_ZONE_WIDTH_ATR = 0.15   # thinner than this is noise, not a zone (see below)


def _fmt(v, d=2):
    try:
        return round(float(v), d)
    except Exception:
        return None


# ── weekly resample (higher-timeframe alignment) ────────────────────────────

def weekly_candles(daily: List[dict]) -> List[dict]:
    """Daily → weekly OHLCV, bucketed by ISO week. Oldest-first, and the final
    (possibly partial) week is kept — it is the live week."""
    out: List[dict] = []
    cur_key = None
    for c in daily:
        d = _dt.datetime.fromtimestamp(c["t"] / 1000, IST).date()
        key = d.isocalendar()[:2]                     # (iso year, iso week)
        if key != cur_key:
            out.append({"t": c["t"], "o": c["o"], "h": c["h"], "l": c["l"],
                        "c": c["c"], "v": c.get("v") or 0})
            cur_key = key
        else:
            w = out[-1]
            w["h"] = max(w["h"], c["h"])
            w["l"] = min(w["l"], c["l"])
            w["c"] = c["c"]
            w["v"] += c.get("v") or 0
    return out


def _sma(vals: List[float], n: int) -> Optional[float]:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def htf_context(daily: List[dict]) -> Dict:
    """Weekly Weinstein read. A 3-week-to-a-month hold taken against the weekly
    trend is a different (worse) trade than the daily chart suggests."""
    wk = weekly_candles(daily)
    closes = [c["c"] for c in wk]
    sma10 = _sma(closes, 10)                          # ~10 weeks
    sma30 = _sma(closes, 30)                          # Weinstein's 30-week line
    last = closes[-1] if closes else None
    stage, aligned = "unknown", None
    if last and sma30:
        rising30 = bool(len(closes) >= 34 and sma30 > (_sma(closes[:-4], 30) or sma30))
        if last > sma30 and rising30:
            stage = "stage2_advancing"
        elif last > sma30:
            stage = "stage1_basing_above"
        elif last < sma30 and not rising30:
            stage = "stage4_declining"
        else:
            stage = "stage3_topping"
        aligned = stage in ("stage2_advancing", "stage1_basing_above")
    return {"weeks": len(wk), "close": _fmt(last),
            "sma10w": _fmt(sma10), "sma30w": _fmt(sma30),
            "stage": stage, "aligned_for_long": aligned,
            "note": "weekly Weinstein stage — a month-long hold should not fight it"}


# ── composite volume profile from DAILY candles ─────────────────────────────

def volume_profile(daily: List[dict], lookback: int = VP_LOOKBACK,
                   rows: int = VP_ROWS) -> Dict:
    """Volume-by-price over the last `lookback` sessions. Each day's volume is
    spread uniformly across that day's high–low range (the standard daily-candle
    approximation — without ticks there is no finer truth available).

    Returns POC / VAH / VAL / HVN / LVN plus `naked_poc`: prior-month POCs that
    price has never traded back through. Those act as magnets and make good
    swing targets."""
    win = daily[-lookback:] if len(daily) > lookback else daily
    if len(win) < 20:
        return {"ok": False, "error": f"only {len(win)} sessions"}

    p_lo = min(c["l"] for c in win)
    p_hi = max(c["h"] for c in win)
    if p_hi <= p_lo:
        return {"ok": False, "error": "flat range"}
    row = (p_hi - p_lo) / rows
    vol = [0] * rows

    for c in win:
        v = c.get("v") or 0
        if v <= 0:
            continue
        lo_i = max(0, min(rows - 1, int((c["l"] - p_lo) / row)))
        hi_i = max(0, min(rows - 1, int((c["h"] - p_lo) / row)))
        span = hi_i - lo_i + 1
        share = v / span
        for i in range(lo_i, hi_i + 1):
            vol[i] += share

    vol = [int(v) for v in vol]
    total = float(sum(vol)) or 1.0
    poc_i = _poc_index(vol)
    va_lo, va_hi = _value_area(vol, poc_i, total)
    nodes = _hvn_lvn(vol, p_lo, row)

    last = win[-1]["c"]
    poc = p_lo + (poc_i + 0.5) * row
    val = p_lo + va_lo * row
    vah = p_lo + (va_hi + 1) * row

    return {
        "ok": True, "sessions": len(win), "rows": rows,
        "price_low": _fmt(p_lo), "price_high": _fmt(p_hi), "row_size": _fmt(row, 4),
        "poc": _fmt(poc), "vah": _fmt(vah), "val": _fmt(val),
        "in_value": bool(val <= last <= vah),
        "position": ("above_value" if last > vah else
                     "below_value" if last < val else "inside_value"),
        "hvn": nodes["hvn"], "lvn": nodes["lvn"],
        "naked_poc": naked_pocs(daily, lookback),
        "note": ("composite profile from daily candles — each session's volume "
                 "spread across its H–L range"),
    }


def naked_pocs(daily: List[dict], lookback: int = VP_LOOKBACK,
               period: int = NPOC_PERIOD) -> List[Dict]:
    """Naked (virgin) POCs: the POC of a prior `period`-session block that price
    has NOT traded back through since. Unfinished business — price returns to
    them far more often than chance, which makes them targets."""
    win = daily[-lookback:] if len(daily) > lookback else daily
    out: List[Dict] = []
    n = len(win)
    for start in range(0, n - period, period):
        block = win[start:start + period]
        later = win[start + period:]
        if len(block) < period // 2 or not later:
            continue
        b_lo = min(c["l"] for c in block)
        b_hi = max(c["h"] for c in block)
        if b_hi <= b_lo:
            continue
        rows = 30
        row = (b_hi - b_lo) / rows
        vol = [0.0] * rows
        for c in block:
            v = c.get("v") or 0
            if v <= 0:
                continue
            lo_i = max(0, min(rows - 1, int((c["l"] - b_lo) / row)))
            hi_i = max(0, min(rows - 1, int((c["h"] - b_lo) / row)))
            share = v / (hi_i - lo_i + 1)
            for i in range(lo_i, hi_i + 1):
                vol[i] += share
        poc = b_lo + (_poc_index([int(x) for x in vol]) + 0.5) * row
        # naked = no later session's range contains it
        if not any(c["l"] <= poc <= c["h"] for c in later):
            out.append({"price": _fmt(poc),
                        "from_t": block[0]["t"], "to_t": block[-1]["t"],
                        "sessions_ago": len(later)})
    last = win[-1]["c"]
    for p in out:
        p["side"] = "above" if (p["price"] or 0) > last else "below"
    return sorted(out, key=lambda p: abs((p["price"] or 0) - last))[:6]


# ── SMC zones on the daily timeframe ────────────────────────────────────────

def _overlap(a_top: float, a_bot: float, b_top: float, b_bot: float):
    top, bot = min(a_top, b_top), max(a_bot, b_bot)
    return (top, bot) if top > bot else None


def _atr14(daily: List[dict]) -> float:
    trs = []
    for i in range(1, len(daily)):
        p, c = daily[i - 1], daily[i]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
    return (sum(trs[-14:]) / 14) if len(trs) >= 14 else 0.0


def zones(daily: List[dict], recent_n: int = SMC_RECENT) -> Dict:
    """Unmitigated demand/supply on the daily chart, plus the dealing range and
    any fresh liquidity sweep."""
    smc = smc_engine.analyze(daily, swing_lookback=2, recent_n=recent_n)
    if not smc.get("ok"):
        return {"ok": False, "error": smc.get("error")}

    last = daily[-1]["c"]
    atr = _atr14(daily) or last * 0.02
    min_w = MIN_ZONE_WIDTH_ATR * atr
    obs = smc.get("order_blocks") or []
    fvgs = smc.get("fvg") or []

    def _z(z, kind):
        top, bot = z["top"], z["bottom"]
        return {"top": _fmt(top), "bottom": _fmt(bot), "mid": _fmt((top + bot) / 2),
                "kind": kind, "source": z.get("from_event", "smc"),
                "strength": z.get("strength"), "t": z.get("t"),
                "dist_pct": _fmt((last - (top + bot) / 2) / last * 100)}

    demand = sorted([_z(z, "OB") for z in obs
                     if z["kind"] == "bull" and not z.get("mitigated") and z["top"] < last
                     and (z["top"] - z["bottom"]) >= min_w],
                    key=lambda z: z["dist_pct"] or 99)          # nearest below first
    supply = sorted([_z(z, "OB") for z in obs
                     if z["kind"] == "bear" and not z.get("mitigated") and z["bottom"] > last],
                    key=lambda z: -(z["dist_pct"] or -99))      # nearest above first

    bull_fvg = []
    for f in fvgs:
        if (f["kind"] == "bull" and not f.get("mitigated") and f["top"] < last
                and (f["top"] - f["bottom"]) >= min_w):
            d = _z(f, "FVG")
            d["ce"] = _fmt((f["top"] + f["bottom"]) / 2)   # consequent encroachment
            bull_fvg.append(d)
    bear_fvg = []
    for f in fvgs:
        if f["kind"] == "bear" and not f.get("mitigated") and f["bottom"] > last:
            d = _z(f, "FVG")
            d["ce"] = _fmt((f["top"] + f["bottom"]) / 2)
            bear_fvg.append(d)

    # Refined zone = OB ∩ FVG. Tighter pocket, tighter stop, higher conviction.
    # The overlap must still be a real zone: a sliver a few paise wide is where
    # two ranges happen to graze, and taking it as a zone would hand back an
    # absurdly tight stop and a fantasy R:R. Below MIN_ZONE_WIDTH_ATR the
    # overlap is discarded and the parent order block is used instead.
    bull_fvg.sort(key=lambda z: z["dist_pct"] or 99)
    bear_fvg.sort(key=lambda z: -(z["dist_pct"] or -99))

    refined = []
    for ob in demand:
        for fv in bull_fvg:
            ov = _overlap(ob["top"], ob["bottom"], fv["top"], fv["bottom"])
            if not ov:
                continue
            top, bot = ov
            if (top - bot) < min_w:
                continue
            refined.append({"top": _fmt(top), "bottom": _fmt(bot),
                            "mid": _fmt((top + bot) / 2), "kind": "OB+FVG",
                            "ce": _fmt((top + bot) / 2),
                            "source": "refined (order block overlapping the imbalance)",
                            "ob": ob, "fvg": fv,
                            "width_atr": _fmt((top - bot) / atr, 2),
                            "dist_pct": _fmt((last - (top + bot) / 2) / last * 100)})
    refined.sort(key=lambda z: z["dist_pct"] or 99)

    breakers = bullish_breakers(daily, obs, last)

    rng = smc.get("range") or {}
    sweeps = smc.get("sweeps") or []
    fresh_ssl = None
    if sweeps:
        cut = daily[max(0, len(daily) - 1 - SWEEP_FRESH_SESSIONS)]["t"]
        ssl = [s for s in sweeps if s["kind"] == "SSL" and s["t"] >= cut]
        if ssl:
            fresh_ssl = ssl[-1]

    return {
        "ok": True,
        "atr14": _fmt(atr), "min_zone_width": _fmt(min_w),
        "bias": smc.get("bias"),
        "price_zone": smc.get("price_zone"),        # premium / equilibrium / discount
        "dealing_range": {k: _fmt(v) for k, v in rng.items()
                          if k in ("high", "low", "eq", "ote_top", "ote_bottom",
                                   "fib_618", "fib_705", "fib_79")} if rng else None,
        "in_discount": smc.get("price_zone") == "discount",
        "in_ote": bool(rng and rng.get("ote_bottom") is not None
                       and rng["ote_bottom"] <= last <= rng["ote_top"]),
        "demand_ob": demand[:4], "supply_ob": supply[:4],
        "bull_fvg": bull_fvg[:4], "bear_fvg": bear_fvg[:4],
        "refined": refined[:3],
        "breakers": breakers[:3],
        "fresh_ssl_sweep": fresh_ssl,
        "structure_events": (smc.get("structure") or [])[-4:],
    }


def bullish_breakers(daily: List[dict], obs: List[dict], last: float) -> List[Dict]:
    """A bullish breaker is a SUPPLY order block that price closed decisively
    above — the sellers there were taken out, so the zone flips to support."""
    out = []
    for z in obs:
        if z["kind"] != "bear":
            continue
        top = z["top"]
        broke = any(c["c"] > top for c in daily[-SMC_RECENT:])
        if broke and top < last:
            out.append({"top": _fmt(top), "bottom": _fmt(z["bottom"]),
                        "mid": _fmt((top + z["bottom"]) / 2), "kind": "breaker",
                        "source": "supply order block flipped to support",
                        "t": z.get("t"),
                        "dist_pct": _fmt((last - (top + z["bottom"]) / 2) / last * 100)})
    return sorted(out, key=lambda z: -(z["mid"] or 0))


# ── structure-based targets (the exit side) ─────────────────────────────────

def resistance_ladder(ctx: Dict, vp: Dict, trend: Dict, entry: float) -> List[Dict]:
    """Every real level above `entry`, nearest first, each labelled with WHY it
    is a level. This is what replaces a flat 2R/3R target."""
    out: List[Dict] = []

    def add(price, why, kind):
        p = _fmt(price)
        if p and p > entry * 1.002:
            out.append({"price": p, "why": why, "kind": kind,
                        "gain_pct": _fmt((p - entry) / entry * 100)})

    for z in (ctx.get("supply_ob") or []):
        add(z["bottom"], f"supply order block {z['bottom']}–{z['top']}", "supply_ob")
    for f in (ctx.get("bear_fvg") or []):
        add(f["bottom"], f"unmitigated bearish FVG {f['bottom']}–{f['top']}", "bear_fvg")
    for p in (vp.get("naked_poc") or []):
        if p.get("side") == "above":
            add(p["price"], f"naked POC from {p['sessions_ago']} sessions ago", "naked_poc")
    if vp.get("ok"):
        add(vp.get("vah"), "value-area high (VAH)", "vah")
        add(vp.get("poc"), "composite POC", "poc")
        for h in (vp.get("hvn") or [])[:2]:
            add(h["price"], "high-volume node (price agreed here before)", "hvn")
    dr = ctx.get("dealing_range") or {}
    add(dr.get("high"), "dealing-range high (liquidity resting above)", "range_high")
    add(trend.get("week52_high"), "52-week high", "52w_high")

    # nearest-first, de-duplicated to ~0.5% buckets
    out.sort(key=lambda x: x["price"])
    dedup: List[Dict] = []
    for lv in out:
        if dedup and abs(lv["price"] - dedup[-1]["price"]) / max(lv["price"], 1e-9) * 100 < 0.5:
            continue
        dedup.append(lv)
    return dedup


def support_ladder(ctx: Dict, vp: Dict, entry: float) -> List[Dict]:
    """Levels BELOW entry — used to place a stop under real structure instead of
    an arbitrary ATR multiple."""
    out: List[Dict] = []

    def add(price, why, kind):
        p = _fmt(price)
        if p and p < entry * 0.998:
            out.append({"price": p, "why": why, "kind": kind})

    for z in (ctx.get("demand_ob") or []):
        add(z["bottom"], f"demand order block {z['bottom']}–{z['top']}", "demand_ob")
    for f in (ctx.get("bull_fvg") or []):
        add(f["bottom"], f"bullish FVG floor {f['bottom']}", "bull_fvg")
    for b in (ctx.get("breakers") or []):
        add(b["bottom"], "bullish breaker (flipped supply)", "breaker")
    if vp.get("ok"):
        add(vp.get("val"), "value-area low (VAL)", "val")
        add(vp.get("poc"), "composite POC", "poc")
        for h in (vp.get("hvn") or [])[:2]:
            add(h["price"], "high-volume node shelf", "hvn")
    dr = ctx.get("dealing_range") or {}
    add(dr.get("low"), "dealing-range low", "range_low")

    out.sort(key=lambda x: -x["price"])
    dedup: List[Dict] = []
    for lv in out:
        if dedup and abs(lv["price"] - dedup[-1]["price"]) / max(lv["price"], 1e-9) * 100 < 0.5:
            continue
        dedup.append(lv)
    return dedup


def pick_targets(ladder: List[Dict], entry: float, stop: float,
                 min_gain_pct: float, min_r: float = 1.5) -> Tuple[List[float], List[Dict]]:
    """T1 = the nearest structural level that clears both the cost floor and
    min_r; T2 = the next one up. Falls back to R multiples when the chart offers
    nothing (a stock at all-time highs has no overhead structure)."""
    R = entry - stop
    picked: List[Dict] = []
    for lv in ladder:
        if lv["price"] < entry + min_r * R:
            continue
        if (lv["price"] - entry) / entry * 100 < min_gain_pct:
            continue
        picked.append(lv)
        if len(picked) == 2:
            break
    if not picked:
        return [_fmt(entry + 2.0 * R), _fmt(entry + 3.0 * R)], []
    if len(picked) == 1:
        return [picked[0]["price"], _fmt(entry + 3.0 * R)], picked
    return [picked[0]["price"], picked[1]["price"]], picked


def lvn_between(vp: Dict, lo: float, hi: float) -> List[float]:
    """Low-volume nodes between entry and target. Price travels THROUGH an LVN
    quickly — a target just past one tends to get hit; a target just short of
    one tends to stall."""
    return [n["price"] for n in (vp.get("lvn") or []) if lo < n["price"] < hi]


# ── bundle ──────────────────────────────────────────────────────────────────

def context(daily: List[dict]) -> Dict:
    """Everything this module knows, for one symbol, as one dict."""
    try:
        z = zones(daily)
    except Exception as e:                                   # noqa: BLE001
        z = {"ok": False, "error": str(e)[:120]}
    try:
        vp = volume_profile(daily)
    except Exception as e:                                   # noqa: BLE001
        vp = {"ok": False, "error": str(e)[:120]}
    try:
        htf = htf_context(daily)
    except Exception as e:                                   # noqa: BLE001
        htf = {"stage": "unknown", "error": str(e)[:120]}
    return {"smc": z, "volume_profile": vp, "htf": htf}
