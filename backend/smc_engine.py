"""
Smart Money Concepts (SMC / ICT) detection engine.

Pure-Python, no external deps. Operates on OHLCV candles (oldest-first):
    candles = [{"t": ms_epoch, "o":, "h":, "l":, "c":, "v":}, ...]

Detects:
  - Swing points (fractal pivots)        -> structure foundation
  - BOS / CHoCH (Break of Structure /    -> market structure & trend
    Change of Character; MSS == CHoCH on the execution timeframe)
  - FVG (Fair Value Gap / imbalance)      -> 3-candle inefficiency, fill status
  - Order Blocks (bullish/bearish)        -> last opposite candle before a BOS
  - Liquidity sweeps (BSL/SSL turtle      -> stop hunts: wick past swing, close back
    soup) + inducement
  - Premium / Discount / Equilibrium      -> fib of the current dealing range
    + OTE (0.618-0.79)

Everything returns plain dicts/lists so it serialises straight to JSON and the
front-end can draw it. Prices are floats; `t` values are ms epochs that line up
with the candle timestamps the chart already uses.
"""

from typing import List, Dict, Optional


# ── swing points (fractals) ────────────────────────────────────────────────

def swing_points(candles: List[dict], left: int = 2, right: int = 2) -> List[dict]:
    """Fractal swing highs/lows: a high is a swing high if it is the strict max
    of the `left` bars before and `right` bars after it (mirrored for lows).
    Returns chronologically ordered [{idx, t, price, kind:'H'|'L'}]."""
    out: List[dict] = []
    n = len(candles)
    for i in range(left, n - right):
        hi = candles[i]["h"]
        lo = candles[i]["l"]
        is_high = all(candles[j]["h"] < hi for j in range(i - left, i)) and \
                  all(candles[j]["h"] <= hi for j in range(i + 1, i + right + 1))
        is_low = all(candles[j]["l"] > lo for j in range(i - left, i)) and \
                 all(candles[j]["l"] >= lo for j in range(i + 1, i + right + 1))
        if is_high:
            out.append({"idx": i, "t": candles[i]["t"], "price": hi, "kind": "H"})
        if is_low:
            out.append({"idx": i, "t": candles[i]["t"], "price": lo, "kind": "L"})
    out.sort(key=lambda s: s["idx"])
    return out


def classify_swings(swings: List[dict]) -> List[dict]:
    """Label each swing HH / HL / LH / LL by comparing to the previous same-kind
    swing. The first H is 'H'; the first L is 'L' (no prior to compare).
    HH = Higher High, HL = Higher Low, LH = Lower High, LL = Lower Low."""
    last_h: Optional[float] = None
    last_l: Optional[float] = None
    for s in swings:
        if s["kind"] == "H":
            if last_h is None:
                s["label"] = "H"
            elif s["price"] > last_h:
                s["label"] = "HH"
            else:
                s["label"] = "LH"
            last_h = s["price"]
        else:
            if last_l is None:
                s["label"] = "L"
            elif s["price"] > last_l:
                s["label"] = "HL"
            else:
                s["label"] = "LL"
            last_l = s["price"]
    return swings


# ── market structure: BOS / CHoCH ──────────────────────────────────────────

def market_structure(candles: List[dict], swings: List[dict]) -> Dict:
    """Walk candles against the most recent un-broken swing high/low.

    A close above the active swing high is a bullish break; a close below the
    active swing low is a bearish break. The break is a BOS when it continues
    the prevailing bias, or a CHoCH when it flips the bias (the first reversal
    signal). Returns {events:[...], bias, last_high, last_low}."""
    events: List[dict] = []
    bias: Optional[str] = None          # 'bull' | 'bear'
    active_high: Optional[dict] = None  # most recent swing H not yet broken
    active_low: Optional[dict] = None   # most recent swing L not yet broken

    # index swings by the candle index at which they become "known" (idx+right
    # already baked into swing idx; they are usable from that bar onward).
    swings_by_idx: Dict[int, List[dict]] = {}
    for s in swings:
        swings_by_idx.setdefault(s["idx"], []).append(s)

    for i, c in enumerate(candles):
        close = c["c"]
        # bullish break of the active swing high
        if active_high and close > active_high["price"]:
            kind = "BOS" if bias == "bull" else "CHoCH"
            events.append({"t": c["t"], "idx": i, "kind": kind, "dir": "bull",
                           "level": active_high["price"], "broke_t": active_high["t"]})
            bias = "bull"
            active_high = None
        # bearish break of the active swing low
        elif active_low and close < active_low["price"]:
            kind = "BOS" if bias == "bear" else "CHoCH"
            events.append({"t": c["t"], "idx": i, "kind": kind, "dir": "bear",
                           "level": active_low["price"], "broke_t": active_low["t"]})
            bias = "bear"
            active_low = None

        # register swings that formed at this bar as the new active references
        for s in swings_by_idx.get(i, []):
            if s["kind"] == "H":
                active_high = s
            else:
                active_low = s

    return {"events": events, "bias": bias,
            "last_high": active_high, "last_low": active_low}


# ── Fair Value Gaps (imbalance) ────────────────────────────────────────────

def fair_value_gaps(candles: List[dict], min_pct: float = 0.0) -> List[dict]:
    """3-candle imbalance. Bullish FVG when low[i+1] > high[i-1]; bearish when
    high[i+1] < low[i-1]. Marks `mitigated` if any later bar trades back into
    the gap. `min_pct` filters out tiny gaps (% of price)."""
    out: List[dict] = []
    n = len(candles)
    for i in range(1, n - 1):
        prev_, nxt = candles[i - 1], candles[i + 1]
        # bullish gap
        if nxt["l"] > prev_["h"]:
            top, bottom = nxt["l"], prev_["h"]
            if _gap_ok(top, bottom, min_pct):
                out.append(_mk_fvg("bull", top, bottom, candles, i))
        # bearish gap
        elif nxt["h"] < prev_["l"]:
            top, bottom = prev_["l"], nxt["h"]
            if _gap_ok(top, bottom, min_pct):
                out.append(_mk_fvg("bear", top, bottom, candles, i))
    return out


def _gap_ok(top: float, bottom: float, min_pct: float) -> bool:
    if min_pct <= 0:
        return True
    mid = (top + bottom) / 2.0
    return mid > 0 and (top - bottom) / mid * 100.0 >= min_pct


def _mk_fvg(kind: str, top: float, bottom: float, candles: List[dict], i: int) -> dict:
    mitigated, mit_t = False, None
    for j in range(i + 2, len(candles)):
        c = candles[j]
        if kind == "bull" and c["l"] <= bottom:   # price traded back down into gap
            mitigated, mit_t = True, c["t"]; break
        if kind == "bear" and c["h"] >= top:       # price traded back up into gap
            mitigated, mit_t = True, c["t"]; break
    return {"kind": kind, "top": top, "bottom": bottom,
            "t_start": candles[i - 1]["t"], "t_mid": candles[i]["t"],
            "mitigated": mitigated, "mit_t": mit_t}


# ── Order Blocks ───────────────────────────────────────────────────────────

def order_blocks(candles: List[dict], structure: Dict) -> List[dict]:
    """For each structural break, the order block is the last opposite-colour
    candle before the impulse leg that caused the break.
      bullish BOS/CHoCH -> last down-close candle before the up-move
      bearish BOS/CHoCH -> last up-close candle before the down-move
    Zone = full range (low..high) of that candle. `mitigated` if price later
    returned into the zone."""
    out: List[dict] = []
    for ev in structure["events"]:
        bi = ev["idx"]
        if ev["dir"] == "bull":
            ob = _last_match(candles, bi, lambda c: c["c"] < c["o"])
            kind = "bull"
        else:
            ob = _last_match(candles, bi, lambda c: c["c"] > c["o"])
            kind = "bear"
        if ob is None:
            continue
        j, c = ob
        top, bottom = c["h"], c["l"]
        mitigated, mit_t = False, None
        for k in range(bi + 1, len(candles)):
            cc = candles[k]
            if kind == "bull" and cc["l"] <= top and cc["l"] >= bottom:
                mitigated, mit_t = True, cc["t"]; break
            if kind == "bear" and cc["h"] >= bottom and cc["h"] <= top:
                mitigated, mit_t = True, cc["t"]; break
        out.append({"kind": kind, "top": top, "bottom": bottom, "t": c["t"],
                    "from_event": ev["kind"], "mitigated": mitigated, "mit_t": mit_t})
    return out


def _last_match(candles, before_idx, pred):
    for j in range(before_idx - 1, max(-1, before_idx - 30), -1):
        if pred(candles[j]):
            return j, candles[j]
    return None


def impulse_order_blocks(candles: List[dict], min_pct: float = 0.25,
                         lookahead: int = 6) -> List[dict]:
    """Find ALL order blocks, not just those tied to a structural break.

    Rule: the last opposite-colour candle before an impulse leg that travels at
    least `min_pct` of price within the next `lookahead` bars. Catches many more
    institutional zones than the BOS-only detector. Each OB gets a `strength`
    score = the size of the impulse leg as a percentage of price."""
    out: List[dict] = []
    n = len(candles)
    for i in range(1, n - 1):
        c = candles[i]
        base = c["c"]
        if base <= 0:
            continue

        # Bullish OB: down-close candle followed by a strong UPward leg.
        if c["c"] < c["o"]:
            top_n = max(
                (candles[j]["h"] for j in range(i + 1, min(i + lookahead + 1, n))),
                default=base,
            )
            rise = (top_n - c["h"]) / base * 100.0
            if rise >= min_pct:
                top, bottom = c["h"], c["l"]
                mitigated, mit_t = _zone_mitigation(candles, i + 1, "bull", top, bottom)
                out.append({"kind": "bull", "top": top, "bottom": bottom,
                            "t": c["t"], "from_event": "impulse",
                            "strength": round(rise, 3),
                            "mitigated": mitigated, "mit_t": mit_t})

        # Bearish OB: up-close candle followed by a strong DOWNward leg.
        elif c["c"] > c["o"]:
            bot_n = min(
                (candles[j]["l"] for j in range(i + 1, min(i + lookahead + 1, n))),
                default=base,
            )
            drop = (c["l"] - bot_n) / base * 100.0
            if drop >= min_pct:
                top, bottom = c["h"], c["l"]
                mitigated, mit_t = _zone_mitigation(candles, i + 1, "bear", top, bottom)
                out.append({"kind": "bear", "top": top, "bottom": bottom,
                            "t": c["t"], "from_event": "impulse",
                            "strength": round(drop, 3),
                            "mitigated": mitigated, "mit_t": mit_t})
    return out


def _zone_mitigation(candles, start_idx, kind, top, bottom):
    """A zone is mitigated when price RE-ENTERS it (first touch back).
    Bullish zone (price gapped up out of it) → mitigated when later low <= top.
    Bearish zone (price dropped from it) → mitigated when later high >= bottom."""
    for j in range(start_idx, len(candles)):
        c = candles[j]
        if kind == "bull" and c["l"] <= top:
            return True, c["t"]
        if kind == "bear" and c["h"] >= bottom:
            return True, c["t"]
    return False, None


def _dedupe_zones(zones: List[dict], tol_pct: float = 0.05) -> List[dict]:
    """Drop zones whose top+bottom both land within tol_pct of an earlier one —
    BOS-OBs and impulse-OBs often coincide at the same candle."""
    out: List[dict] = []
    for z in zones:
        mid = (z["top"] + z["bottom"]) / 2.0
        dupe = False
        for kept in out:
            kmid = (kept["top"] + kept["bottom"]) / 2.0
            if kept["kind"] == z["kind"] and abs(mid - kmid) / max(mid, 1e-9) * 100 < tol_pct:
                dupe = True
                break
        if not dupe:
            out.append(z)
    return out


# ── liquidity sweeps (turtle soup) + inducement ────────────────────────────

def liquidity_sweeps(candles: List[dict], swings: List[dict]) -> List[dict]:
    """A swing high is swept (BSL grab) when a later bar's high pierces it but
    the bar closes back below it — a stop hunt. Mirrored for swing lows (SSL)."""
    out: List[dict] = []
    highs = [s for s in swings if s["kind"] == "H"]
    lows = [s for s in swings if s["kind"] == "L"]
    for i, c in enumerate(candles):
        for s in highs:
            if s["idx"] < i and c["h"] > s["price"] and c["c"] < s["price"]:
                out.append({"kind": "BSL", "price": s["price"], "t": c["t"],
                            "swept_t": s["t"]})
                break
        for s in lows:
            if s["idx"] < i and c["l"] < s["price"] and c["c"] > s["price"]:
                out.append({"kind": "SSL", "price": s["price"], "t": c["t"],
                            "swept_t": s["t"]})
                break
    return out


# ── premium / discount / equilibrium + OTE ─────────────────────────────────

def dealing_range(candles: List[dict], structure: Dict) -> Optional[Dict]:
    """Current dealing range from the most recent opposing swings. Splits it
    into premium (>50%, sell zone), discount (<50%, buy zone) and equilibrium
    (50%). OTE is the 0.618-0.79 retracement band in the trade direction."""
    hi = structure.get("last_high")
    lo = structure.get("last_low")
    # fall back to absolute range of the window if no active swings
    if not hi or not lo:
        if not candles:
            return None
        hp = max(candles, key=lambda c: c["h"])
        lp = min(candles, key=lambda c: c["l"])
        hi = {"price": hp["h"], "t": hp["t"]}
        lo = {"price": lp["l"], "t": lp["t"]}
    high, low = hi["price"], lo["price"]
    if high <= low:
        return None
    rng = high - low
    eq = low + rng * 0.5
    bias = structure.get("bias")
    # OTE: for a long (discount), 0.618-0.79 retrace measured from high down;
    # for a short, mirror from the low up.
    if bias == "bear":
        ote_top = high - rng * 0.618
        ote_bottom = high - rng * 0.79
    else:
        ote_top = low + rng * 0.79
        ote_bottom = low + rng * 0.618
    return {"high": high, "low": low, "eq": eq,
            "ote_top": max(ote_top, ote_bottom), "ote_bottom": min(ote_top, ote_bottom),
            "premium_floor": eq, "discount_ceiling": eq,
            "fib_618": low + rng * 0.618, "fib_705": low + rng * 0.705,
            "fib_79": low + rng * 0.79, "bias": bias,
            "high_t": hi["t"], "low_t": lo["t"]}


# ── top-level ──────────────────────────────────────────────────────────────

def trend_forecast(swings: List[dict], structure_bias: Optional[str],
                   last_price: float, rng: Optional[Dict]) -> Dict:
    """Read the last few HH/HL/LH/LL labels and project a directional bias.

    Pattern → trend:
      HH + HL  = bullish (textbook uptrend)
      LH + LL  = bearish (textbook downtrend)
      HH + LL  = expansion (range widening, likely reversal soon)
      LH + HL  = compression (range tightening, breakout pending)

    Combines with structure bias and Premium/Discount zone for the next-move
    expectation. The forecast is plain English the chart can show directly."""
    highs = [s for s in swings if s["kind"] == "H"]
    lows = [s for s in swings if s["kind"] == "L"]
    last_h_lbl = highs[-1].get("label") if highs else None
    last_l_lbl = lows[-1].get("label") if lows else None

    trend = "unclear"
    if last_h_lbl in ("HH", "H") and last_l_lbl in ("HL", "L"):
        trend = "bullish"
    elif last_h_lbl in ("LH",) and last_l_lbl in ("LL",):
        trend = "bearish"
    elif last_h_lbl == "HH" and last_l_lbl == "LL":
        trend = "expansion"
    elif last_h_lbl == "LH" and last_l_lbl == "HL":
        trend = "compression"
    elif last_h_lbl and last_l_lbl:
        trend = "transition"

    zone = None
    if rng:
        span = rng["high"] - rng["low"]
        if span > 0:
            pct = (last_price - rng["low"]) / span * 100.0
            zone = "premium" if pct > 55 else "discount" if pct < 45 else "equilibrium"

    # Next-move expectation = trend × zone × bias confluence
    next_move = "wait"
    rationale = []
    if trend == "bullish" and zone == "discount":
        next_move = "buy bias"; rationale.append("uptrend + discount = expect bounce up")
    elif trend == "bearish" and zone == "premium":
        next_move = "sell bias"; rationale.append("downtrend + premium = expect drop")
    elif trend == "bullish" and zone == "premium":
        next_move = "wait (overbought)"; rationale.append("uptrend but premium — wait for pullback to HL")
    elif trend == "bearish" and zone == "discount":
        next_move = "wait (oversold)"; rationale.append("downtrend but discount — wait for retrace to LH")
    elif trend == "compression":
        next_move = "wait for break"; rationale.append("LH + HL = range tightening, breakout pending")
    elif trend == "expansion":
        next_move = "reversal watch"; rationale.append("HH + LL = volatility expanding; reversal likely")

    if structure_bias and structure_bias != trend.replace("ish", ""):
        rationale.append(f"structure bias is {structure_bias} (note: divergence)")

    return {
        "trend": trend,
        "last_high_label": last_h_lbl,
        "last_low_label": last_l_lbl,
        "zone": zone,
        "next_move": next_move,
        "rationale": "; ".join(rationale) if rationale else "no clean confluence",
    }


def analyze(candles: List[dict], swing_lookback: int = 2,
            fvg_min_pct: float = 0.0) -> Dict:
    """Run the full SMC suite over a candle series (oldest-first)."""
    if not candles or len(candles) < 5:
        return {"ok": False, "error": "not enough candles", "n": len(candles)}

    swings = swing_points(candles, swing_lookback, swing_lookback)
    swings = classify_swings(swings)
    structure = market_structure(candles, swings)
    fvg = fair_value_gaps(candles, fvg_min_pct)

    # Merge BOS-anchored OBs with impulse-detected OBs (and de-dupe overlaps).
    obs_bos = order_blocks(candles, structure)
    obs_imp = impulse_order_blocks(candles)
    obs = _dedupe_zones(obs_bos + obs_imp)

    sweeps = liquidity_sweeps(candles, swings)
    rng = dealing_range(candles, structure)

    last_price = candles[-1]["c"]
    zone = None
    if rng:
        pct = (last_price - rng["low"]) / (rng["high"] - rng["low"]) * 100.0
        zone = "premium" if pct > 55 else "discount" if pct < 45 else "equilibrium"

    forecast = trend_forecast(swings, structure["bias"], last_price, rng)

    return {
        "ok": True,
        "n": len(candles),
        "bias": structure["bias"],
        "last_price": last_price,
        "price_zone": zone,
        "swings": swings,
        "structure": structure["events"],
        "fvg": fvg,
        "order_blocks": obs,
        "sweeps": sweeps,
        "range": rng,
        "forecast": forecast,
    }


# ── live signal extraction (for alerts) ────────────────────────────────────

def live_signals(candles: List[dict], result: Dict, recent_bars: int = 3,
                 touch_pct: float = 0.15) -> List[dict]:
    """Surface actionable events near the current bar for alerting:
      - a BOS/CHoCH that printed within the last `recent_bars`
      - a liquidity sweep within the last `recent_bars`
      - price currently sitting inside an unmitigated OB or FVG
      - price inside the OTE band
    Each signal has a stable `id` so the front-end can de-dupe."""
    if not result.get("ok"):
        return []
    sigs: List[dict] = []
    n = len(candles)
    last = candles[-1]
    price = last["c"]
    recent_ts = candles[max(0, n - recent_bars)]["t"]

    for ev in result["structure"]:
        if ev["t"] >= recent_ts:
            sigs.append({"id": f"struct:{ev['kind']}:{ev['dir']}:{ev['t']}",
                         "type": ev["kind"], "dir": ev["dir"],
                         "msg": f"{ev['kind']} ({ev['dir']}) @ {ev['level']:.2f}",
                         "t": ev["t"]})
    for s in result["sweeps"]:
        if s["t"] >= recent_ts:
            sigs.append({"id": f"sweep:{s['kind']}:{s['t']}",
                         "type": "Sweep", "dir": "bear" if s["kind"] == "BSL" else "bull",
                         "msg": f"{s['kind']} liquidity sweep @ {s['price']:.2f}",
                         "t": s["t"]})
    for ob in result["order_blocks"]:
        if not ob["mitigated"] and ob["bottom"] <= price <= ob["top"]:
            sigs.append({"id": f"ob:{ob['kind']}:{ob['t']}",
                         "type": "OB touch", "dir": ob["kind"],
                         "msg": f"price in {ob['kind']} order block {ob['bottom']:.2f}-{ob['top']:.2f}",
                         "t": last["t"]})
    for g in result["fvg"]:
        if not g["mitigated"] and g["bottom"] <= price <= g["top"]:
            sigs.append({"id": f"fvg:{g['kind']}:{g['t_mid']}",
                         "type": "FVG touch", "dir": g["kind"],
                         "msg": f"price in {g['kind']} FVG {g['bottom']:.2f}-{g['top']:.2f}",
                         "t": last["t"]})
    rng = result.get("range")
    if rng and rng["ote_bottom"] <= price <= rng["ote_top"]:
        sigs.append({"id": f"ote:{rng['high_t']}:{rng['low_t']}",
                     "type": "OTE", "dir": rng.get("bias") or "n/a",
                     "msg": f"price in OTE {rng['ote_bottom']:.2f}-{rng['ote_top']:.2f}",
                     "t": last["t"]})
    return sigs
