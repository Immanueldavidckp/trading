"""
Analysis + Suggestion engines.

analysis_ticks(tsym)  — tick-by-tick replay from the stored market_depth: for
                        each tick, who was the aggressor (buyer/seller), the
                        traded volume, the order-book imbalance, and WHY price
                        reacted there (which SMC zone — FVG / Order Block — it
                        sits in). Plus a "who's in control" summary.

suggestions(tsym)     — multi-timeframe directional call (1m/5m/15m/1h/4h/1d):
                        trend, next move, an expected target and a pullback
                        entry derived from the nearest SMC levels.
"""

from datetime import datetime, timezone, timedelta
from typing import List, Optional

import db as _db
import smc_engine
from upstox_client import UpstoxClient

IST = timezone(timedelta(hours=5, minutes=30))
SUGGEST_TFS = ["1m", "5m", "15m", "1h", "4h", "1d"]


def _ist_hms(received_at: str) -> str:
    try:
        dt = datetime.fromisoformat(str(received_at).replace("Z", ""))
        return dt.replace(tzinfo=timezone.utc).astimezone(IST).strftime("%H:%M:%S")
    except Exception:
        return str(received_at)


def _candles(tsym: str, interval: str, limit: int = 400) -> list:
    rows = UpstoxClient.query(tsym, interval, limit=limit)
    return [{"t": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"],
             "c": r["c"], "v": r["v"]} for r in rows]


def _zones(tsym: str, intervals=("1m", "15m", "1h"), recent_n: int = 150) -> list:
    """Unmitigated Order Blocks + FVGs across a few TFs, for tick context."""
    out = []
    for iv in intervals:
        res = smc_engine.analyze(_candles(tsym, iv), recent_n=recent_n)
        if not res.get("ok"):
            continue
        for o in res["order_blocks"]:
            if not o["mitigated"]:
                out.append({"tf": iv, "kind": "OB", "dir": o["dir"] if "dir" in o else o["kind"],
                            "top": o["top"], "bottom": o["bottom"]})
        for g in res["fvg"]:
            if not g["mitigated"]:
                out.append({"tf": iv, "kind": "FVG", "dir": g["kind"],
                            "top": g["top"], "bottom": g["bottom"]})
    return out


def _zone_at(price: float, zones: list) -> Optional[dict]:
    """Smallest zone containing the price (tightest context wins)."""
    hits = [z for z in zones if z["bottom"] <= price <= z["top"]]
    if not hits:
        return None
    return min(hits, key=lambda z: z["top"] - z["bottom"])


# ── tick-by-tick analysis ───────────────────────────────────────────────────

def analysis_ticks(tsym: str, limit: int = 400) -> dict:
    tsym = tsym.upper()
    PH = _db.PLACE
    sql = (f"SELECT received_at, ltp, volume, total_buy_qty, total_sell_qty "
           f"FROM market_depth WHERE tsym={PH} ORDER BY id DESC LIMIT {PH}")
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, [tsym, int(limit)])
        rows = list(reversed(cur.fetchall()))   # oldest-first
        cur.close()
    finally:
        conn.close()

    if not rows:
        return {"ok": False, "error": "no tick data stored yet for " + tsym}

    zones = _zones(tsym)
    events: List[dict] = []
    prev_ltp = prev_vol = None
    buy_vol = sell_vol = 0

    for received_at, ltp, vol, tbq, tsq in rows:
        ltp = float(ltp) if ltp is not None else None
        vol = int(vol) if vol is not None else None
        vol_delta = max(0, (vol - prev_vol)) if (vol is not None and prev_vol is not None) else 0

        tot = (tbq or 0) + (tsq or 0)
        imb = round((tbq or 0) / tot * 100, 1) if tot > 0 else 50.0

        # Aggressor: tick rule (uptick = buyer, downtick = seller); flat ticks
        # broken by resting-order imbalance.
        if prev_ltp is None or ltp == prev_ltp:
            agg = ("BUY" if imb > 55 else "SELL" if imb < 45 else "FLAT") if prev_ltp is not None else "—"
        else:
            agg = "BUY" if ltp > prev_ltp else "SELL"

        if agg == "BUY":
            buy_vol += vol_delta
        elif agg == "SELL":
            sell_vol += vol_delta

        zone = _zone_at(ltp, zones) if ltp is not None else None
        events.append({
            "t": _ist_hms(received_at), "ltp": ltp, "vol": vol_delta,
            "agg": agg, "imb": imb,
            "zone": (f"{zone['tf']} {zone['dir']} {zone['kind']}" if zone else None),
        })
        prev_ltp, prev_vol = ltp, vol

    total = buy_vol + sell_vol
    controller = "BUYERS" if buy_vol > sell_vol else "SELLERS" if sell_vol > buy_vol else "BALANCED"

    # Notable events: zone touches, aggressor flips, and volume spikes — these
    # are the "why did price move here" moments. Keep the most recent ~90.
    notable = []
    prev_agg = None
    vols = [e["vol"] for e in events if e["vol"] > 0]
    vbig = (sorted(vols)[int(len(vols) * 0.85)] if vols else 0)
    for e in events:
        flip = (e["agg"] in ("BUY", "SELL") and prev_agg in ("BUY", "SELL") and e["agg"] != prev_agg)
        if e["zone"] or flip or (e["vol"] >= vbig and vbig > 0):
            e2 = dict(e); e2["flip"] = flip
            notable.append(e2)
        if e["agg"] in ("BUY", "SELL"):
            prev_agg = e["agg"]
    notable = notable[-90:]

    return {
        "ok": True, "tsym": tsym, "ticks": len(rows),
        "buy_vol": buy_vol, "sell_vol": sell_vol,
        "buy_pct": round(buy_vol / total * 100, 1) if total else 50.0,
        "controller": controller,
        "last_agg": events[-1]["agg"] if events else "—",
        "zones": zones,
        "events": notable,
    }


# ── multi-timeframe suggestion ──────────────────────────────────────────────

def suggestions(tsym: str, intervals: Optional[list] = None) -> dict:
    tsym = tsym.upper()
    out = []
    for iv in (intervals or SUGGEST_TFS):
        res = smc_engine.analyze(_candles(tsym, iv), recent_n=150)
        if not res.get("ok"):
            out.append({"interval": iv, "ok": False, "error": res.get("error")})
            continue
        f = res.get("forecast") or {}
        last = res["last_price"]

        sup, resist = [], []
        for o in res["order_blocks"]:
            if o["mitigated"]:
                continue
            if o["top"] < last:
                sup.append(o["top"])
            elif o["bottom"] > last:
                resist.append(o["bottom"])
        for g in res["fvg"]:
            if g["mitigated"]:
                continue
            if g["top"] < last:
                sup.append(g["top"])
            elif g["bottom"] > last:
                resist.append(g["bottom"])
        for s in res["swings"]:
            if s["kind"] == "H" and s["price"] > last:
                resist.append(s["price"])
            elif s["kind"] == "L" and s["price"] < last:
                sup.append(s["price"])
        rng = res.get("range") or {}
        if rng.get("high") and rng["high"] > last:
            resist.append(rng["high"])
        if rng.get("low") and rng["low"] < last:
            sup.append(rng["low"])

        nearest_sup = max(sup) if sup else None     # closest support below
        nearest_res = min(resist) if resist else None  # closest resistance above
        trend = f.get("trend", "unclear")

        if trend == "bullish":
            direction, target, entry = "UP", nearest_res, nearest_sup
        elif trend == "bearish":
            direction, target, entry = "DOWN", nearest_sup, nearest_res
        else:
            direction, target, entry = "RANGE", nearest_res, nearest_sup

        out.append({
            "interval": iv, "ok": True,
            "trend": trend, "bias": res["bias"], "zone": res.get("price_zone"),
            "next_move": f.get("next_move"), "direction": direction,
            "last_price": last, "target": target, "entry": entry,
            "support": nearest_sup, "resistance": nearest_res,
            "hl": f"{f.get('last_high_label','—')}/{f.get('last_low_label','—')}",
            "rationale": f.get("rationale"),
        })
    return {"ok": True, "tsym": tsym, "suggestions": out}
