"""
orderflow.py — institutional-style order-flow analytics for NSE cash equities.

Four products, all built from data this app already has:

  heatmap(tsym)          Bookmap-style LIQUIDITY HEATMAP — resting book size as a
                         time × price grid (from the 30-level D30 ring buffer),
                         with traded prints overlaid and book events detected
                         (walls, pulled liquidity, absorption, iceberg refills).

  volume_profile(tsym)   VPVR — traded volume per price with POC, 70% Value Area
                         (one-row expansion, matching TradingView/CQG; the
                         classic CBOT two-row variant is available too),
                         HVN/LVN nodes, session VWAP with ±1σ/±2σ bands,
                         buy/sell DELTA per price, and a CVD series.

  footprint(tsym)        BID×ASK FOOTPRINT per candle — per-price bid/ask volume,
                         candle delta, per-candle POC, diagonal imbalances
                         (3× rule), stacked imbalances, unfinished auctions.

  tape(tsym)             Classified time & sales (the raw feed the above use).

Aggressor classification is CLNV (Chakrabarty, Li, Nguyen & Van Ness 2007):
the quote rule decides in the outer 30% of the spread on each side, and the
tick rule (uptick = buy, downtick = sell, zero-tick inherits) handles only the
middle 40%. It measures more accurately than plain Lee-Ready on electronic
venues, and since bid/ask are stored on every tick row the quote branch — the
accurate part — does most of the work.

DATA HONESTY: the tick source is a ~1 s REST poll, not a true trade-by-trade
feed. Each row's volume delta is the volume traded SINCE the previous poll,
assigned to the last-traded price of that poll. So per-price volumes are a
faithful approximation, not exchange trade-by-trade truth — every response
carries `approximation` saying so. The book side (heatmap) IS true 30-level
data, just sampled at 1 s.
"""
from __future__ import annotations
from typing import List, Dict, Optional, Tuple
import datetime as _dt
import math

import db as _db

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
_UTC = _dt.timezone.utc

# The dashboard polls every ~5 s but a session volume profile scans thousands of
# tick rows — cache the DB-heavy products briefly (the heatmap reads RAM and is
# never cached, so the live book stays live).
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL = 15.0


def _cached(key: str, ttl: float, fn):
    import time as _t
    hit = _CACHE.get(key)
    now = _t.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    if len(_CACHE) > 200:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:100]:
            _CACHE.pop(k, None)
    return val

WALL_MULT = 3.0          # level qty ≥ 3× the book's median level = a wall
PULL_FRAC = 0.70         # wall losing ≥70% of size = pulled/consumed
ABSORB_TICKS = 3         # price held inside N ticks while heavy volume printed
ABSORB_WIN_S = 30        # local window the displacement is measured over
ICEBERG_MIN_REFILLS = 2  # one refill is just a new order arriving
IMBALANCE_MULT = 3.0     # footprint diagonal imbalance threshold (300%)
STACK_MIN = 3            # consecutive imbalanced rows = a stacked imbalance
VA_PCT = 0.70            # value area = 70% of volume (Steidlmayer standard)
MAX_FOOT_ROWS = 40       # footprint rows per candle before ticks get grouped


# ── time helpers ────────────────────────────────────────────────────────────

def _utc_iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _UTC).replace(tzinfo=None).isoformat(
        timespec="milliseconds")


def _parse_iso(s) -> float:
    try:
        return _dt.datetime.fromisoformat(str(s).replace("Z", "")).replace(
            tzinfo=_UTC).timestamp()
    except Exception:
        return 0.0


def _ist_hm(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def _ist_hms(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S")


def _session_bounds(day: Optional[str] = None) -> Tuple[float, float, str]:
    """UTC epoch bounds of an NSE session (09:15–15:30 IST) for an IST date."""
    d = _dt.date.fromisoformat(day) if day else _dt.datetime.now(IST).date()
    start = _dt.datetime.combine(d, _dt.time(9, 15), IST).timestamp()
    end = _dt.datetime.combine(d, _dt.time(15, 30), IST).timestamp()
    return start, end, d.isoformat()


def tick_size(price: float) -> float:
    """NSE cash-equity tick: ₹0.01 below ₹250, ₹0.05 at ₹250 and above (the
    2024 sub-₹250 one-paisa regime). The ₹250–₹300 slice of this universe is
    genuinely a 5-paisa instrument — using a 1-paisa grid there manufactures
    empty rows in every profile and footprint."""
    return 0.01 if price and price < 250 else 0.05


def _round_tick(p: float, tick: float) -> float:
    return round(round(p / tick) * tick, 4)


# ── classified tape (the shared primitive) ──────────────────────────────────

def _load_ticks(tsym: str, t0: float, t1: float, limit: int = 60000) -> List[dict]:
    """Rows from price_changes in [t0,t1) → prints with volume delta + side.

    price_changes is written only when LTP/bid/ask MOVE, so consecutive rows
    already are the interesting events; `volume` is the day's cumulative traded
    quantity, so the per-row delta is what traded since the previous row."""
    PH = _db.PLACE
    sql = (f"SELECT received_at, lp, bid, ask, volume FROM price_changes "
           f"WHERE tsym={PH} AND received_at>={PH} AND received_at<{PH} "
           f"ORDER BY id LIMIT {int(limit)}")
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, [tsym.upper(), _utc_iso(t0), _utc_iso(t1)])
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    out: List[dict] = []
    prev_lp = prev_vol = None
    prev_side = "BUY"
    for received_at, lp, bid, ask, vol in rows:
        try:
            lp = float(lp) if lp is not None else None
            vol = int(vol) if vol is not None else None
            bid = float(bid) if bid is not None else None
            ask = float(ask) if ask is not None else None
        except (TypeError, ValueError):
            continue
        if lp is None:
            continue
        dv = 0
        if vol is not None and prev_vol is not None and vol >= prev_vol:
            dv = vol - prev_vol
        if vol is not None:
            prev_vol = vol

        # CLNV (Chakrabarty-Li-Nguyen-Van-Ness): the quote rule decides in the
        # outer 30% of the spread on each side, the tick rule handles the
        # middle 40%. Measured more accurate than plain Lee-Ready on electronic
        # venues, and we have both quotes on every row so it costs nothing.
        side = None
        if bid is not None and ask is not None and ask > bid:
            sp = ask - bid
            if lp >= ask - 0.30 * sp:
                side = "BUY"
            elif lp <= bid + 0.30 * sp:
                side = "SELL"
        elif ask is not None and lp >= ask:
            side = "BUY"
        elif bid is not None and lp <= bid:
            side = "SELL"
        if side is None:                      # tick rule; zero tick inherits
            if prev_lp is not None and lp > prev_lp:
                side = "BUY"
            elif prev_lp is not None and lp < prev_lp:
                side = "SELL"
            else:
                side = prev_side
        prev_side = side
        prev_lp = lp

        if dv > 0:
            out.append({"t": _parse_iso(received_at), "price": lp, "qty": dv,
                        "side": side, "bid": bid, "ask": ask})
    return out


def tape(tsym: str, minutes: int = 30, limit: int = 400) -> Dict:
    """Classified time & sales, newest first."""
    t1 = _dt.datetime.now(_UTC).timestamp()
    t0 = t1 - minutes * 60
    tk = _load_ticks(tsym, t0, t1)
    big = sorted((x["qty"] for x in tk), reverse=True)
    thresh = big[max(0, int(len(big) * 0.05) - 1)] if big else 0
    rows = [{"time": _ist_hms(x["t"]), "price": x["price"], "qty": x["qty"],
             "side": x["side"], "large": x["qty"] >= thresh and thresh > 0}
            for x in tk[-limit:]][::-1]
    return {"ok": True, "tsym": tsym.upper(), "count": len(rows), "rows": rows,
            "large_threshold": thresh,
            "approximation": "1s-polled prints (volume delta at last price), CLNV classified"}


# ── 1. liquidity heatmap ────────────────────────────────────────────────────

def heatmap(tsym: str, minutes: int = 30, time_bins: int = 150,
            max_price_rows: int = 140) -> Dict:
    """Time × price grid of RESTING liquidity from the D30 ring buffer.

    Cells hold the AVERAGE size resting at that price during the time bin —
    liquidity is a level, not a flow, so averaging (never summing) is what
    makes a persistent wall read as one bright band instead of a growing one."""
    tsym = tsym.upper()
    try:
        from main import _depth30
        d30 = _depth30()
        d30.record(tsym)
        snaps = d30.history(tsym, seconds=minutes * 60)
    except Exception as e:
        return {"ok": False, "error": f"depth stream unavailable: {str(e)[:120]}"}

    if len(snaps) < 3:
        return {"ok": False, "error": "warming_up", "tsym": tsym,
                "message": ("Recording this symbol's 30-level book now — the heatmap "
                            "needs ~30s of history. It fills in as the session runs "
                            "(and only during market hours)."),
                "snapshots": len(snaps)}

    t_lo, t_hi = snaps[0][0], snaps[-1][0]
    span = max(1.0, t_hi - t_lo)
    nt = max(10, min(int(time_bins), 400))
    dt = span / nt

    prices = [p for s in snaps for p in (s[1] + s[3]) if p]
    if not prices:
        return {"ok": False, "error": "no book levels recorded yet"}
    p_lo, p_hi = min(prices), max(prices)
    tick = tick_size(p_hi)
    rows_needed = int(round((p_hi - p_lo) / tick)) + 1
    # keep the grid tick-exact when it fits; otherwise widen the row to fit the range
    row = tick if rows_needed <= max_price_rows else \
        _round_tick((p_hi - p_lo) / max_price_rows, tick) or tick
    np_ = int(round((p_hi - p_lo) / row)) + 1

    def pidx(p):
        return max(0, min(np_ - 1, int(round((p - p_lo) / row))))

    bid = [[0.0] * nt for _ in range(np_)]
    ask = [[0.0] * nt for _ in range(np_)]
    cnt = [0] * nt
    best_bid = [None] * nt
    best_ask = [None] * nt
    ltp_line = [None] * nt

    for s in snaps:
        ts, bp, bq, ap, aq, ltp = s
        ti = max(0, min(nt - 1, int((ts - t_lo) / dt)))
        cnt[ti] += 1
        for p, q in zip(bp, bq):
            if p:
                bid[pidx(p)][ti] += q
        for p, q in zip(ap, aq):
            if p:
                ask[pidx(p)][ti] += q
        # last-in-bin, not max/min across it — taking the extremes would draw
        # the widest spread observed rather than the one that prevailed
        if bp:
            best_bid[ti] = bp[0]
        if ap:
            best_ask[ti] = ap[0]
        if ltp is not None:
            ltp_line[ti] = ltp

    for ti in range(nt):
        c = cnt[ti] or 1
        for pi in range(np_):
            if bid[pi][ti]:
                bid[pi][ti] = round(bid[pi][ti] / c)
            if ask[pi][ti]:
                ask[pi][ti] = round(ask[pi][ti] / c)

    # trades in the same window, on the same grid (the print overlay)
    tk = _load_ticks(tsym, t_lo, t_hi + dt)
    trades = []
    for x in tk:
        ti = max(0, min(nt - 1, int((x["t"] - t_lo) / dt)))
        trades.append({"ti": ti, "pi": pidx(x["price"]), "qty": x["qty"],
                       "side": x["side"], "price": x["price"]})
    merged: Dict[tuple, dict] = {}
    for tr in trades:
        k = (tr["ti"], tr["pi"], tr["side"])
        m = merged.setdefault(k, {"ti": tr["ti"], "pi": tr["pi"], "side": tr["side"],
                                  "qty": 0, "price": tr["price"]})
        m["qty"] += tr["qty"]

    flat = [v for r in bid for v in r if v] + [v for r in ask for v in r if v]
    flat.sort()
    p99 = flat[int(len(flat) * 0.99)] if flat else 1
    p50 = flat[len(flat) // 2] if flat else 1

    return {
        "ok": True, "tsym": tsym, "source": "d30",
        "snapshots": len(snaps),
        "t_start": t_lo, "t_end": t_hi, "dt": dt,
        "time_labels": [_ist_hms(t_lo + i * dt) for i in range(nt)],
        "price_low": round(p_lo, 2), "price_row": row, "n_price": np_, "n_time": nt,
        "prices": [round(p_lo + i * row, 2) for i in range(np_)],
        "bid": bid, "ask": ask,
        "best_bid": best_bid, "best_ask": best_ask, "ltp": ltp_line,
        "trades": list(merged.values()),
        "scale": {"p50": p50, "p99": p99,
                  "note": "colour intensity: log-scaled, clipped at the 99th pct level size"},
        "events": book_events(snaps, tk, tick),
        "approximation": "book sampled ~1/s (true 30 levels); prints are 1s-polled volume deltas",
    }


def book_events(snaps: List[tuple], ticks: List[dict], tick: float) -> List[Dict]:
    """Detect what the heatmap is showing: walls, pulled liquidity (spoof-like),
    absorption, and iceberg refills. Thresholds are the practical ones desks
    use — every event carries its numbers so nothing is a black box."""
    if len(snaps) < 5:
        return []
    ev: List[Dict] = []

    sizes = [q for s in snaps for q in (s[2] + s[4]) if q]
    if not sizes:
        return []
    sizes.sort()
    med = sizes[len(sizes) // 2] or 1
    wall_min = med * WALL_MULT

    def book_at(s):
        d = {}
        for p, q in zip(s[1], s[2]):
            if p:
                d[(round(p, 2), "B")] = q
        for p, q in zip(s[3], s[4]):
            if p:
                d[(round(p, 2), "A")] = q
        return d

    # walls: a big level that PERSISTS (a one-snapshot spike is noise)
    seen: Dict[tuple, dict] = {}
    for s in snaps:
        for k, q in book_at(s).items():
            if q >= wall_min:
                w = seen.setdefault(k, {"first": s[0], "last": s[0], "max": q, "n": 0})
                w["last"] = s[0]
                w["max"] = max(w["max"], q)
                w["n"] += 1
    for (price, side), w in seen.items():
        if w["n"] >= 5:
            ev.append({"kind": "wall", "side": "bid" if side == "B" else "ask",
                       "price": price, "qty": w["max"],
                       "held_s": round(w["last"] - w["first"], 1),
                       "text": f"{'Bid' if side=='B' else 'Ask'} wall {w['max']:,} @ {price} "
                               f"held {round(w['last']-w['first'])}s ({round(w['max']/med,1)}× typical)"})

    # Volume traded at a price BETWEEN two instants. Whole-window totals would
    # make every busy level look pulled/refilled, so every test below is
    # strictly interval-scoped.
    import bisect
    by_px: Dict[float, dict] = {}
    for x in ticks:
        p = round(x["price"], 2)
        d = by_px.setdefault(p, {"t": [], "q": [], "cum": [0]})
        d["t"].append(x["t"])
        d["q"].append(x["qty"])
        d["cum"].append(d["cum"][-1] + x["qty"])

    def traded_between(price: float, t0: float, t1: float) -> int:
        d = by_px.get(round(price, 2))
        if not d:
            return 0
        a = bisect.bisect_right(d["t"], t0)
        b = bisect.bisect_right(d["t"], t1)
        return d["cum"][b] - d["cum"][a]

    # pulled liquidity: a wall vanishes WITHOUT trades eating it. On aggregated
    # depth this is "liquidity withdrawn", never "spoofing" — intent is not
    # observable, and one participant cancelling looks like many reducing.
    for i in range(1, len(snaps)):
        t0, t1 = snaps[i - 1][0], snaps[i][0]
        prev, cur = book_at(snaps[i - 1]), book_at(snaps[i])
        for k, q in prev.items():
            if q < wall_min:
                continue
            drop = q - cur.get(k, 0)
            if drop >= q * PULL_FRAC and traded_between(k[0], t0, t1) < drop * 0.2:
                ev.append({"kind": "pull", "side": "bid" if k[1] == "B" else "ask",
                           "price": k[0], "qty": drop, "t": t1,
                           "text": f"{drop:,} pulled from {'bid' if k[1]=='B' else 'ask'} "
                                   f"{k[0]} without trading — liquidity withdrawn, not consumed"})

    # absorption: heavy one-sided volume at a price while price fails to move.
    # Displacement is measured over a LOCAL window around that level's own
    # trades — a whole-window range says nothing about this level.
    by_price: Dict[float, dict] = {}
    for x in ticks:
        p = round(x["price"], 2)
        a = by_price.setdefault(p, {"qty": 0, "buy": 0, "sell": 0,
                                    "t0": x["t"], "t1": x["t"]})
        a["qty"] += x["qty"]
        a["buy" if x["side"] == "BUY" else "sell"] += x["qty"]
        a["t0"] = min(a["t0"], x["t"])
        a["t1"] = max(a["t1"], x["t"])
    if by_price and ticks:
        qs = sorted(a["qty"] for a in by_price.values())
        hi_q = qs[int(len(qs) * 0.9)] if qs else 0
        t_all = [x["t"] for x in ticks]
        for p, a in by_price.items():
            if a["qty"] < hi_q or hi_q <= 0:
                continue
            one_sided = abs(a["buy"] - a["sell"]) / a["qty"] if a["qty"] else 0
            if one_sided < 0.4:            # two-sided churn is rotation, not absorption
                continue
            lo_t, hi_t = a["t0"] - ABSORB_WIN_S, a["t1"] + ABSORB_WIN_S
            i0 = bisect.bisect_left(t_all, lo_t)
            i1 = bisect.bisect_right(t_all, hi_t)
            local = [x["price"] for x in ticks[i0:i1]]
            if not local:
                continue
            if (max(local) - min(local)) <= ABSORB_TICKS * tick:
                dom = "sellers" if a["sell"] > a["buy"] else "buyers"
                ev.append({"kind": "absorption", "price": p, "qty": a["qty"],
                           "text": f"{a['qty']:,} traded at {p} ({dom} aggressing) with price "
                                   f"held inside {ABSORB_TICKS} ticks — the passive side is absorbing"})

    # iceberg/refill: volume traded THROUGH a level between two snapshots, yet
    # the level comes back. Needs repeats — one refill is just a new order
    # arriving. Low confidence on aggregated depth (no order IDs): reported as
    # "replenished", never as a confirmed iceberg.
    refills: Dict[tuple, dict] = {}
    for i in range(1, len(snaps)):
        t0, t1 = snaps[i - 1][0], snaps[i][0]
        b0, b1 = book_at(snaps[i - 1]), book_at(snaps[i])
        for k, q0 in b0.items():
            if q0 < wall_min:
                continue
            q1 = b1.get(k, 0)
            through = traded_between(k[0], t0, t1)
            if through >= q0 * 0.5 and q1 >= q0 * 0.8:
                r = refills.setdefault(k, {"n": 0, "traded": 0, "qty": q1})
                r["n"] += 1
                r["traded"] += through
                r["qty"] = q1
    for k, r in refills.items():
        if r["n"] >= ICEBERG_MIN_REFILLS:
            ev.append({"kind": "iceberg", "side": "bid" if k[1] == "B" else "ask",
                       "price": k[0], "qty": r["qty"],
                       "text": f"Level {k[0]} replenished {r['n']}× — {r['traded']:,} traded "
                               f"through it yet ~{r['qty']:,} still resting (iceberg-like)"})

    # de-dupe by (kind, price), keep the biggest, newest first
    best: Dict[tuple, dict] = {}
    for e in ev:
        k = (e["kind"], e["price"])
        if k not in best or e.get("qty", 0) > best[k].get("qty", 0):
            best[k] = e
    return sorted(best.values(), key=lambda e: -e.get("qty", 0))[:24]


# ── 2. volume profile (VPVR) ────────────────────────────────────────────────

def volume_profile(tsym: str, day: Optional[str] = None, rows: int = 60,
                   minutes: Optional[int] = None) -> Dict:
    """Traded volume per price with POC / Value Area / HVN / LVN / VWAP bands
    and the buy-sell delta split. Session profile by default; `minutes` gives a
    rolling visible-range profile instead."""
    tsym = tsym.upper()
    if minutes:
        t1 = _dt.datetime.now(_UTC).timestamp()
        t0 = t1 - minutes * 60
        label = f"last {minutes}m"
        dstr = _dt.datetime.now(IST).date().isoformat()
    else:
        t0, t1, dstr = _session_bounds(day)
        label = f"session {dstr}"

    tk = _load_ticks(tsym, t0, t1)
    if len(tk) < 5:
        return {"ok": False, "error": "no_ticks", "tsym": tsym, "range": label,
                "message": ("No tick data recorded for this window. Ticks are captured "
                            "live during market hours for the watchlist + plan universe.")}

    p_lo = min(x["price"] for x in tk)
    p_hi = max(x["price"] for x in tk)
    tick = tick_size(p_hi)
    n = max(10, min(int(rows), 200))
    row = max(tick, _round_tick((p_hi - p_lo) / n, tick) or tick)
    nrow = int((p_hi - p_lo) / row) + 1

    vol = [0] * nrow
    buy = [0] * nrow
    sell = [0] * nrow
    for x in tk:
        i = max(0, min(nrow - 1, int((x["price"] - p_lo) / row)))
        vol[i] += x["qty"]
        if x["side"] == "BUY":
            buy[i] += x["qty"]
        else:
            sell[i] += x["qty"]

    total = sum(vol)
    if total <= 0:
        return {"ok": False, "error": "no volume in window", "tsym": tsym}

    poc_i = max(range(nrow), key=lambda i: vol[i])
    va_lo_i, va_hi_i = _value_area(vol, poc_i, total)

    # VWAP + volume-weighted σ bands
    vwap = sum(x["price"] * x["qty"] for x in tk) / total
    var = sum(x["qty"] * (x["price"] - vwap) ** 2 for x in tk) / total
    sd = math.sqrt(max(var, 0.0))

    nodes = _hvn_lvn(vol, p_lo, row)

    # CVD: cumulative signed volume, one point per minute
    cvd, per_min = [], {}
    for x in tk:
        m = int(x["t"] // 60) * 60
        d = per_min.setdefault(m, {"delta": 0, "vol": 0})
        d["delta"] += x["qty"] if x["side"] == "BUY" else -x["qty"]
        d["vol"] += x["qty"]
    run = 0
    for m in sorted(per_min):
        run += per_min[m]["delta"]
        cvd.append({"t": m, "time": _ist_hm(m), "delta": per_min[m]["delta"],
                    "cvd": run, "vol": per_min[m]["vol"]})

    prof = [{"price": round(p_lo + i * row, 2), "vol": vol[i],
             "buy": buy[i], "sell": sell[i], "delta": buy[i] - sell[i],
             "in_va": va_lo_i <= i <= va_hi_i, "poc": i == poc_i}
            for i in range(nrow)]

    tot_buy, tot_sell = sum(buy), sum(sell)
    return {
        "ok": True, "tsym": tsym, "range": label, "day": dstr,
        "row_size": row, "n_rows": nrow, "total_volume": total,
        "profile": prof,
        "poc": round(p_lo + poc_i * row, 2),
        "vah": round(p_lo + va_hi_i * row, 2),
        "val": round(p_lo + va_lo_i * row, 2),
        "va_pct": VA_PCT,
        "va_volume_pct": round(sum(vol[va_lo_i:va_hi_i + 1]) / total * 100, 1),
        "vwap": round(vwap, 2), "vwap_sd": round(sd, 3),
        "bands": {"p1": round(vwap + sd, 2), "m1": round(vwap - sd, 2),
                  "p2": round(vwap + 2 * sd, 2), "m2": round(vwap - 2 * sd, 2)},
        "hvn": nodes["hvn"], "lvn": nodes["lvn"],
        "buy_volume": tot_buy, "sell_volume": tot_sell,
        "delta": tot_buy - tot_sell,
        "delta_pct": round((tot_buy - tot_sell) / total * 100, 1),
        "cvd": cvd,
        "high": round(p_hi, 2), "low": round(p_lo, 2),
        "prints": len(tk),
        "approximation": "per-price volume from 1s-polled deltas; sides via CLNV quote+tick rule",
    }


def _poc_index(vol: List[int]) -> int:
    """Row with the most volume. On a tie the convention (CQG) is the row
    CLOSEST TO THE MIDDLE of the profile's price range — not simply the first."""
    mx = max(vol)
    cands = [i for i, v in enumerate(vol) if v == mx]
    if len(cands) == 1:
        return cands[0]
    mid = (len(vol) - 1) / 2.0
    return min(cands, key=lambda i: (abs(i - mid), i))


def _value_area(vol: List[int], poc_i: int, total: float,
                two_row: bool = False) -> Tuple[int, int]:
    """Value Area: grow out from the POC toward the heavier side until 70% of
    volume is enclosed.

    Two conventions exist and they give DIFFERENT VAH/VAL:
      • one row at a time  — TradingView, CQG, most modern platforms (default
        here, so the levels match what you see on a TradingView chart). Ties
        go to the row nearer the POC; still tied, the upper row wins.
      • two rows at a time — the classic CBOT / Steidlmayer / Dalton hand
        method (`two_row=True`). It can overshoot the target by a whole pair.
    """
    target = total * VA_PCT
    acc = vol[poc_i]
    lo = hi = poc_i
    n = len(vol)
    while acc < target and (lo > 0 or hi < n - 1):
        if two_row:
            up_ok, dn_ok = hi < n - 1, lo > 0
            up = (vol[hi + 1] if hi + 1 < n else 0) + (vol[hi + 2] if hi + 2 < n else 0)
            dn = (vol[lo - 1] if lo - 1 >= 0 else 0) + (vol[lo - 2] if lo - 2 >= 0 else 0)
            if up_ok and (up >= dn or not dn_ok):
                hi = min(n - 1, hi + 2); acc += up
            elif dn_ok:
                lo = max(0, lo - 2); acc += dn
            else:
                break
            continue
        up = vol[hi + 1] if hi < n - 1 else None
        dn = vol[lo - 1] if lo > 0 else None
        if up is None and dn is None:
            break
        if dn is None:
            take_up = True
        elif up is None:
            take_up = False
        elif up != dn:
            take_up = up > dn
        else:                                  # equal volume: nearer the POC wins
            d_up, d_dn = (hi + 1) - poc_i, poc_i - (lo - 1)
            take_up = d_up <= d_dn             # still equal → the higher row
        if take_up:
            hi += 1; acc += vol[hi]
        else:
            lo -= 1; acc += vol[lo]
    return lo, hi


def _hvn_lvn(vol: List[int], p_lo: float, row: float) -> Dict:
    """High/Low Volume Nodes = local extrema with real prominence. HVNs are
    where price agreed (support/resistance); LVNs are where it rejected fast —
    price tends to travel THROUGH an LVN rather than sit in it."""
    n = len(vol)
    if n < 5:
        return {"hvn": [], "lvn": []}
    mx = max(vol) or 1
    mean = sum(vol) / n
    hvn, lvn = [], []
    for i in range(1, n - 1):
        window = vol[max(0, i - 2):min(n, i + 3)]
        if vol[i] == max(window) and vol[i] >= mean * 1.5 and vol[i] >= mx * 0.35:
            hvn.append({"price": round(p_lo + i * row, 2), "vol": vol[i],
                        "strength": round(vol[i] / mx, 2)})
        if vol[i] == min(window) and vol[i] <= mean * 0.4 and vol[i] > 0:
            lvn.append({"price": round(p_lo + i * row, 2), "vol": vol[i]})
    # merge adjacent HVNs (one shelf = one node)
    merged = []
    for h in sorted(hvn, key=lambda x: x["price"]):
        if merged and abs(h["price"] - merged[-1]["price"]) <= row * 1.5:
            if h["vol"] > merged[-1]["vol"]:
                merged[-1] = h
        else:
            merged.append(h)
    return {"hvn": sorted(merged, key=lambda x: -x["vol"])[:6],
            "lvn": sorted(lvn, key=lambda x: x["vol"])[:6]}


# ── 3. footprint (bid × ask) ────────────────────────────────────────────────

_TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


def footprint(tsym: str, interval: str = "5m", bars: int = 12,
              day: Optional[str] = None) -> Dict:
    """Per-candle bid×ask ladder with diagonal imbalances and unfinished
    auctions — the microscope for how each candle actually got built."""
    tsym = tsym.upper()
    sec = _TF_SEC.get(interval, 300)
    t0s, t1s, dstr = _session_bounds(day)
    now = _dt.datetime.now(_UTC).timestamp()
    t1 = min(t1s, now)
    t0 = max(t0s, t1 - sec * max(1, int(bars)))
    tk = _load_ticks(tsym, t0, t1)
    if len(tk) < 5:
        return {"ok": False, "error": "no_ticks", "tsym": tsym,
                "message": "No tick data in this window (recorded live during market hours)."}

    tick = tick_size(max(x["price"] for x in tk))
    buckets: Dict[int, Dict[float, dict]] = {}
    meta: Dict[int, dict] = {}
    for x in tk:
        b = int(x["t"] // sec) * sec
        p = _round_tick(x["price"], tick)
        cell = buckets.setdefault(b, {}).setdefault(p, {"bid": 0, "ask": 0})
        # footprint convention: "ask volume" = buyer-initiated (lifted the ask)
        cell["ask" if x["side"] == "BUY" else "bid"] += x["qty"]
        m = meta.setdefault(b, {"o": x["price"], "h": x["price"], "l": x["price"],
                                "c": x["price"], "vol": 0,
                                "run": 0, "dmax": 0, "dmin": 0})
        m["h"] = max(m["h"], x["price"])
        m["l"] = min(m["l"], x["price"])
        m["c"] = x["price"]
        m["vol"] += x["qty"]
        # running delta within the bar → its high/low water marks. A bar that
        # closes at +50 after peaking at +900 is a very different bar.
        m["run"] += x["qty"] if x["side"] == "BUY" else -x["qty"]
        m["dmax"] = max(m["dmax"], m["run"])
        m["dmin"] = min(m["dmin"], m["run"])

    out = []
    for b in sorted(buckets)[-int(bars):]:
        levels = buckets[b]
        ps = sorted(levels)
        # Rows must be a CONTIGUOUS price grid: the diagonal imbalance compares
        # ask@P against bid@P−1 and stacked runs need genuine adjacency, so
        # untraded prices are zero-filled rather than skipped.
        lo_p, hi_p = ps[0], ps[-1]
        step = tick
        span = int(round((hi_p - lo_p) / tick)) + 1
        if span > MAX_FOOT_ROWS:
            step = _round_tick((hi_p - lo_p) / MAX_FOOT_ROWS, tick) or tick
        nrow = int(round((hi_p - lo_p) / step)) + 1
        grid = [{"price": _round_tick(lo_p + i * step, tick), "bid": 0, "ask": 0}
                for i in range(nrow)]
        for p in ps:
            i = max(0, min(nrow - 1, int(round((p - lo_p) / step))))
            grid[i]["bid"] += levels[p]["bid"]
            grid[i]["ask"] += levels[p]["ask"]
        rows = [{"price": g["price"], "bid": g["bid"], "ask": g["ask"],
                 "delta": g["ask"] - g["bid"], "total": g["bid"] + g["ask"]}
                for g in grid]
        # diagonal imbalance: ask@P vs bid@P-1 (and mirrored), 3× rule
        floor_q = max(1, sum(r["total"] for r in rows) // (len(rows) * 8 or 1))
        for i, r in enumerate(rows):
            # ascending rows, so the diagonal partner of ask@P is bid@P-1 (below)
            # and of bid@P is ask@P+1 (above); edges have no partner
            prev_bid = rows[i - 1]["bid"] if i > 0 else None
            next_ask = rows[i + 1]["ask"] if i < len(rows) - 1 else None
            r["buy_imb"] = bool(prev_bid is not None and r["ask"] >= floor_q
                                and r["ask"] >= IMBALANCE_MULT * max(prev_bid, 1))
            r["sell_imb"] = bool(next_ask is not None and r["bid"] >= floor_q
                                 and r["bid"] >= IMBALANCE_MULT * max(next_ask, 1))
        stacks = _stacked(rows)
        poc = rows[_poc_index([r["total"] for r in rows])] if rows else None
        m = meta[b]
        # Unfinished auction: the extreme row traded on BOTH sides, so nothing
        # was rejected there and it usually gets revisited. A strict "both > 0"
        # test flags nearly every bar on 1 s-polled data, so the weaker side
        # must be a real fraction of the stronger one.
        def _unfinished(r):
            hi_side, lo_side = max(r["bid"], r["ask"]), min(r["bid"], r["ask"])
            return bool(lo_side > 0 and lo_side >= 0.10 * hi_side)
        unf_hi = bool(rows) and _unfinished(rows[-1])
        unf_lo = bool(rows) and _unfinished(rows[0])
        delta = sum(r["delta"] for r in rows)
        out.append({
            "t": b, "time": _ist_hms(b), "rows": rows,
            "open": m["o"], "high": m["h"], "low": m["l"], "close": m["c"],
            "volume": m["vol"], "delta": delta,
            "delta_pct": round(delta / m["vol"] * 100, 1) if m["vol"] else 0,
            "max_delta": m["dmax"], "min_delta": m["dmin"],
            "poc": poc["price"] if poc else None,
            "stacked": stacks,
            "unfinished_high": unf_hi, "unfinished_low": unf_lo,
            "divergence": (m["c"] > m["o"] and delta < 0) or (m["c"] < m["o"] and delta > 0),
            # delta ran hard one way then gave most of it back = exhaustion
            "delta_faded": bool(m["dmax"] > 0 and delta < 0.35 * m["dmax"]
                                and m["dmax"] >= 0.15 * max(m["vol"], 1)),
        })
    return {"ok": True, "tsym": tsym, "interval": interval, "tick": tick,
            "bars": out,
            "legend": {
                "imbalance": f"{int(IMBALANCE_MULT*100)}% diagonal rule (ask@P vs bid@P−1)",
                "stacked": f"{STACK_MIN}+ consecutive imbalances = an initiative zone",
                "unfinished": "extreme row traded both sides — no rejection, usually revisited",
                "divergence": "candle closed against its own delta — the move lacked participation",
            },
            "approximation": "bid/ask split via CLNV on 1s-polled prints, not exchange trade-by-trade"}


def _stacked(rows: List[dict]) -> List[dict]:
    """Runs of ≥STACK_MIN consecutive imbalanced rows — where one side ran."""
    out, run, side = [], [], None
    for r in rows:
        s = "buy" if r.get("buy_imb") else "sell" if r.get("sell_imb") else None
        if s and s == side:
            run.append(r)
        else:
            if side and len(run) >= STACK_MIN:
                out.append({"side": side, "from": run[0]["price"], "to": run[-1]["price"],
                            "n": len(run)})
            run, side = ([r], s) if s else ([], None)
    if side and len(run) >= STACK_MIN:
        out.append({"side": side, "from": run[0]["price"], "to": run[-1]["price"], "n": len(run)})
    return out


# ── combined dashboard payload ──────────────────────────────────────────────

def dashboard(tsym: str, minutes: int = 30, interval: str = "5m") -> Dict:
    """Everything the Order Flow page needs in ONE round trip."""
    t = tsym.upper()
    hm = heatmap(t, minutes=minutes)                       # RAM — always fresh
    vp = _cached(f"vp:{t}", _CACHE_TTL, lambda: volume_profile(t))
    fp = _cached(f"fp:{t}:{interval}", _CACHE_TTL,
                 lambda: footprint(t, interval=interval, bars=10))
    return {"ok": True, "tsym": tsym.upper(), "heatmap": hm,
            "volume_profile": vp, "footprint": fp,
            "read": _read(hm, vp, fp)}


def _read(hm: Dict, vp: Dict, fp: Dict) -> List[str]:
    """Plain-English summary — the point of the whole page."""
    out = []
    if vp.get("ok"):
        d = vp["delta_pct"]
        out.append(f"Session delta {d:+.1f}% ({vp['buy_volume']:,} bought vs "
                   f"{vp['sell_volume']:,} sold) — "
                   + ("buyers in control" if d > 5 else "sellers in control" if d < -5
                      else "balanced, no clear aggressor"))
        out.append(f"POC {vp['poc']} · value area {vp['val']}–{vp['vah']} "
                   f"({vp['va_volume_pct']}% of volume) · VWAP {vp['vwap']}. "
                   "Price above the value area is acceptance higher; below it, lower.")
        if vp.get("lvn"):
            out.append("Low-volume gaps at " + ", ".join(str(x["price"]) for x in vp["lvn"][:3])
                       + " — price usually travels through these fast rather than resting.")
    if hm.get("ok"):
        walls = [e for e in hm.get("events", []) if e["kind"] == "wall"][:2]
        for w in walls:
            out.append(w["text"])
        for k in ("absorption", "iceberg", "pull"):
            e = next((e for e in hm.get("events", []) if e["kind"] == k), None)
            if e:
                out.append(e["text"])
    if fp.get("ok") and fp.get("bars"):
        last = fp["bars"][-1]
        if last.get("divergence"):
            out.append(f"Last {fp['interval']} candle closed against its delta "
                       f"({last['delta']:+,}) — the move is not backed by aggression.")
        if last.get("stacked"):
            s = last["stacked"][0]
            out.append(f"Stacked {s['side']} imbalances {s['from']}–{s['to']} on the last candle "
                       "— initiative activity, that zone tends to hold on a retest.")
        if last.get("delta_faded"):
            out.append(f"Last candle's delta peaked at {last['max_delta']:+,} and closed at "
                       f"{last['delta']:+,} — aggression came in and gave it back (exhaustion).")
        if last.get("unfinished_high") or last.get("unfinished_low"):
            where = "high" if last.get("unfinished_high") else "low"
            out.append(f"Unfinished auction at the last candle's {where} — both sides traded at "
                       "that extreme, so nothing was rejected there; it usually gets revisited.")
    return out
