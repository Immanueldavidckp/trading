"""
swing_engine.py — turns DAILY candles into a next-session SWING plan (2–15
trading-day holds, cash/delivery, LONG-only).

Built every evening after the close, exactly like plan_engine.py builds the
intraday plan — but from the daily timeframe. Every setup carries NUMERIC
trigger/entry/stop/targets plus an entry window, a limit cap (no chasing gaps),
a time stop and an expected holding period, so swing_score.py can replay it
honestly against the actual daily candles that follow.

The calculation was designed from a multi-source research pass (global + India)
and then adversarially reviewed (look-ahead, India microstructure, statistics).
The load-bearing decisions that review forced:

  • LONG-only. Retail cannot hold cash-segment shorts overnight in India; SLB is
    institutional; most sub-₹300 names have no futures (and SEBI's ₹15–20L
    contract minimum dwarfs swing sizing). Bearish names → "stand aside".
  • Setups (3, all evidence-based, all computable from OHLCV at the evening close):
      S1 52-week-high momentum breakout   — George & Hwang (2004) nearness effect
                                            + Minervini/Weinstein Stage-2 gate.
                                            Unfiltered breakouts FAIL in India
                                            (documented); the RS + nearness +
                                            volume filters are the point.
      S2 Trend pullback resumption        — Landry-style: strength returning,
                                            never catching the falling dip.
      S3 Volatility-compression breakout  — Crabel NR7/inside-day + BB-squeeze
                                            (volatility is autocorrelated; the
                                            directional edge comes from the
                                            Stage-2 + RS gates, and is honestly
                                            labeled untested).
    A Connors-style mean-reversion setup was evaluated and REJECTED: its
    documented +1.18%/trade edge belongs to close-entry on a US index ETF; with
    next-day entry mechanics and Indian delivery costs the expectancy is ~zero.
  • Score weights are PRIORS, not fitted values — the swing scorecard exists to
    measure them against reality (§ swing_score.aggregate_scorecard).
  • Expected entry/exit dates are HEURISTICS (net progress ≈ 0.35×ATR/day toward
    target — deliberately conservative; verified 0.6 was ~2× optimistic). The
    hard number is always exit_by_date (the time stop).
  • Costs: delivery on Upstox ≈ statutory 0.22% + ₹20/order brokerage ×2 +
    DP charge on sell + slippage ⇒ 0.65% round trip on a ₹25k position. Minimum
    viable first target = 3% (≈ 5–6× cost). Positions under ₹15k are
    uneconomical (flat DP fee) — the plan says so instead of pretending.
  • NOT AVAILABLE as data feeds (flagged in every plan, never silently passed):
    ASM/GSM/ESM lists, daily price bands, F&O membership, earnings calendar.
    OHLCV proxies are used where honest (locked-circuit-close detection,
    gap-frequency gate); everything else is a visible warning.

Nothing here places orders. Analysis only; profit is only ever measured after
the fact, net of costs, by swing_score.py.
"""
from __future__ import annotations
from typing import List, Dict, Optional
import datetime as _dt

from plan_engine import _ema, _atr, _fmt
import swing_smc

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

SWING_ROUND_TRIP_COST_PCT = 0.65    # % of notional, delivery round trip (Upstox, ₹25k pos)
DEFAULT_RISK_RUPEES = 1000.0        # illustrative per-trade risk for sizing
MIN_POSITION_RUPEES = 15000         # below this the flat DP fee alone is >0.12%

MIN_DAILY_CANDLES = 252             # 52-week levels need a full year of history
MIN_TURNOVER_CR_20D = 10.0          # median 20d turnover (₹cr) to swing delivery safely
ENTRY_WINDOW_SESSIONS = 2           # armed buy-stops live T+1 & T+2, then stale
ZONE_WINDOW_SESSIONS = 5            # a resting LIMIT at a zone waits longer than a buy-stop
MAX_HOLD_SESSIONS = 22              # ~1 calendar month — the outer time stop
SCALE_OUT_PCT = 50                  # % of the position taken off at T1
TRAIL_ATR_MULT = 2.5                # chandelier trail once T1 prints
MIN_T1_GAIN_PCT = 3.0               # first target must clear ≈5–6× round-trip cost
MIN_STOP_PCT = 1.2                  # tighter stops make fixed costs + gaps huge in R terms
MAX_STOP_PCT = 8.0                  # wider than 8% = wrong candidate for a swing
BREADTH_REGIME_MIN = 0.40           # <40% of universe above SMA200 → no new entries


def _ist_date(ms) -> str:
    try:
        return _dt.datetime.fromtimestamp(ms / 1000, _IST).strftime("%Y-%m-%d")
    except Exception:
        return "—"


def _sma(vals: List[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _rsi(closes: List[float], n: int = 14) -> Optional[float]:
    """Wilder RSI on closes (oldest-first)."""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def add_trading_days(d: _dt.date, n: int) -> _dt.date:
    """d + n trading days, weekends AND NSE holidays skipped.

    These dates are printed as hard instructions — "enter by", "hard exit by" —
    so counting a holiday as a session quietly shortens the real window. Falls
    back to the weekend-only count if the calendar can't be loaded."""
    try:
        import trading_calendar as _cal
        _open = lambda x: _cal.is_trading_day(x)["open"]          # noqa: E731
    except Exception:
        _open = lambda x: x.weekday() < 5                         # noqa: E731
    cur = d
    step = 1 if n >= 0 else -1
    left = abs(n)
    guard = 0
    while left > 0 and guard < 400:
        cur += _dt.timedelta(days=step)
        guard += 1
        if _open(cur):
            left -= 1
    return cur


def _roc(closes: List[float], n: int) -> Optional[float]:
    if len(closes) <= n or not closes[-n - 1]:
        return None
    return (closes[-1] / closes[-n - 1] - 1.0) * 100.0


# ── trend / stage classification (Minervini template + Weinstein stage) ─────

def trend_state(daily: List[dict]) -> Dict:
    closes = [c["c"] for c in daily]
    last = closes[-1]
    e20 = _ema(closes, 20)
    s50, s150, s200 = _sma(closes, 50), _sma(closes, 150), _sma(closes, 200)
    s200_prev = _sma(closes[:-21], 200) if len(closes) >= 221 else None

    hi52 = max(c["h"] for c in daily[-252:])
    lo52 = min(c["l"] for c in daily[-252:])
    nearness = (last / hi52) if hi52 else None          # George-Hwang nearness
    pct_from_hi = (nearness - 1.0) * 100 if nearness else None
    off_low = (last / lo52 - 1.0) * 100 if lo52 else None

    # the 7 template checks — window baselines end BEFORE the value being tested
    checks = {
        "close_gt_sma50": bool(s50 and last > s50),
        "close_gt_sma150_200": bool(s150 and s200 and last > s150 and last > s200),
        "sma150_gt_sma200": bool(s150 and s200 and s150 > s200),
        "sma200_rising_1m": bool(s200 and s200_prev and s200 > s200_prev),
        "sma50_gt_sma150": bool(s50 and s150 and s50 > s150),
        "gte_1p25x_52w_low": bool(off_low is not None and off_low >= 25),
        "within_25pct_of_52w_high": bool(nearness is not None and nearness >= 0.75),
    }
    passes = sum(1 for v in checks.values() if v)
    template_pass = passes == 7
    stage2 = passes >= 5                                # eligibility floor

    dn = bool(e20 and s50 and last < e20 and e20 < s50)
    stage = ("uptrend" if stage2 else "downtrend" if dn else "sideways")

    # daily-return volatility, annualized (for vol-normalized momentum)
    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(max(1, len(closes) - 63), len(closes))
            if closes[i - 1]]
    import math
    sigma63 = None
    if len(rets) >= 20:
        m = sum(rets) / len(rets)
        var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
        sigma63 = (var ** 0.5) * math.sqrt(252) * 100   # % annualized

    return {
        "stage": stage, "template_pass": template_pass, "template_passes": passes,
        "checks": checks,
        "ema20": _fmt(e20), "sma50": _fmt(s50), "sma150": _fmt(s150), "sma200": _fmt(s200),
        "week52_high": _fmt(hi52), "week52_low": _fmt(lo52),
        "nearness_52w": _fmt(nearness), "pct_from_52w_high": _fmt(pct_from_hi),
        "pct_above_52w_low": _fmt(off_low),
        "ret_21d": _fmt(_roc(closes, 21)), "ret_63d": _fmt(_roc(closes, 63)),
        "ret_126d": _fmt(_roc(closes, 126)),
        "sigma63_ann_pct": _fmt(sigma63),
    }


# ── liquidity / risk profile ────────────────────────────────────────────────

def profile(daily: List[dict]) -> Dict:
    closes = [c["c"] for c in daily]
    last = closes[-1]
    atr = _atr(daily, 14) or 0.0
    atr_pct = atr / last * 100 if last else None

    vols = [c.get("v") or 0 for c in daily]
    # baseline windows end at T-1 so the signal day can't dilute its own baseline
    avg_v50 = _sma(vols[:-1], 50) or _sma(vols[:-1], 20) or 0
    rvol = (vols[-1] / avg_v50) if avg_v50 else 1.0
    turns = sorted((c.get("v") or 0) * c["c"] for c in daily[-20:])
    med_turnover_cr = turns[len(turns) // 2] / 1e7 if turns else 0.0

    upv = dnv = 0.0
    for i in range(max(1, len(daily) - 20), len(daily)):
        v = daily[i].get("v") or 0
        if daily[i]["c"] >= daily[i - 1]["c"]:
            upv += v
        else:
            dnv += v
    udr = (upv / dnv) if dnv else None

    # gap frequency: sessions with |open gap| > 3% in the last 20 (event-driven proxy)
    gap3 = 0
    for i in range(max(1, len(daily) - 20), len(daily)):
        pc = daily[i - 1]["c"]
        if pc and abs(daily[i]["o"] / pc - 1.0) > 0.03:
            gap3 += 1

    # locked-circuit proxy: close==high==low AND |move| near a 5/10/20% band
    def _locked(c, pc):
        if not pc or c["h"] != c["l"]:
            return False
        mv = abs(c["c"] / pc - 1.0) * 100
        return any(abs(mv - b) <= 0.6 for b in (5, 10, 20))
    locked60 = sum(1 for i in range(max(1, len(daily) - 60), len(daily))
                   if _locked(daily[i], daily[i - 1]["c"]))

    # compression flags (Crabel / Bollinger) — the same latent variable is only
    # credited once per level (bar-level, window-level) in the score
    rng = [c["h"] - c["l"] for c in daily]
    nr7 = len(rng) >= 7 and rng[-1] == min(rng[-7:])
    inside = (len(daily) >= 2 and daily[-1]["h"] < daily[-2]["h"]
              and daily[-1]["l"] > daily[-2]["l"])
    nr4 = len(rng) >= 4 and rng[-1] == min(rng[-4:])
    a14_now = _atr(daily, 14)
    a14_prev = _atr(daily[:-20], 14) if len(daily) > 40 else None
    atr_contracting = bool(a14_now and a14_prev and a14_now < 0.75 * a14_prev)
    bbw_low = _bbw_at_126_low(closes)

    hi20 = max(c["h"] for c in daily[-20:])
    lo10 = min(c["l"] for c in daily[-10:])

    return {"atr14": _fmt(atr), "atr_pct": _fmt(atr_pct), "rvol": _fmt(rvol),
            "avg_vol_50d": int(avg_v50) if avg_v50 else None,
            "med_turnover_cr_20d": _fmt(med_turnover_cr),
            "updown_vol_ratio_20d": _fmt(udr),
            "gap3_count_20d": gap3, "locked_circuit_60d": locked60,
            "nr7": bool(nr7), "inside_nr4": bool(inside and nr4),
            "atr_contracting": atr_contracting, "bbw_126_low": bbw_low,
            "hi20": _fmt(hi20), "lo10": _fmt(lo10)}


def _bbw_at_126_low(closes: List[float]) -> bool:
    """20-day Bollinger BandWidth at its lowest of the last 126 sessions."""
    if len(closes) < 146:
        return False
    def bw(upto):
        w = closes[upto - 20:upto]
        m = sum(w) / 20
        sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
        return (4 * sd / m) if m else 0
    now = bw(len(closes))
    hist = [bw(i) for i in range(len(closes) - 126, len(closes))]
    return bool(hist) and now <= min(hist) + 1e-12


# ── hard gates ──────────────────────────────────────────────────────────────

def hard_gates(trend: Dict, prof: Dict, n_candles: int) -> List[str]:
    """Reasons this stock gets NO swing setups tonight (plan still reports why)."""
    out = []
    if n_candles < MIN_DAILY_CANDLES:
        out.append(f"only {n_candles} daily candles — 52-week analysis needs {MIN_DAILY_CANDLES}")
    if (prof.get("med_turnover_cr_20d") or 0) < MIN_TURNOVER_CR_20D:
        out.append(f"illiquid for delivery swing: median 20d turnover "
                   f"₹{prof.get('med_turnover_cr_20d')}cr < ₹{MIN_TURNOVER_CR_20D}cr")
    ap = prof.get("atr_pct") or 0
    if ap > 6.0:
        out.append(f"too wild: ATR {ap}%/day — an overnight stop cannot be honoured")
    if 0 < ap < 0.8:
        out.append(f"too quiet: ATR {ap}%/day can't reach a 3% target inside the time stop")
    if (prof.get("gap3_count_20d") or 0) >= 3:
        out.append(f"event-driven: {prof['gap3_count_20d']} overnight gaps >3% in 20 sessions "
                   "— stops don't protect through gaps")
    if (prof.get("locked_circuit_60d") or 0) >= 2:
        out.append(f"circuit-prone: {prof['locked_circuit_60d']} locked-limit closes in 60 "
                   "sessions — entry/exit can simply not fill")
    if trend["template_passes"] < 5:
        out.append(f"not a Stage-2 stock ({trend['template_passes']}/7 template checks) — "
                   "no counter-trend longs, no exceptions")
    return out


# ── setups ──────────────────────────────────────────────────────────────────

def _mk_setup(name, stype, trigger_txt, trigger_price, entry, limit_price, stop,
              targets, context, quality, source, time_stop, atr, valid_when,
              skip_when, no_progress=None):
    R = abs(entry - stop) or 1e-9
    tgts = [t for t in targets if t is not None]
    hold = _expected_hold(entry, tgts[0], atr, time_stop) if tgts else time_stop
    return {
        "name": name, "side": "LONG", "type": stype,
        "trigger": trigger_txt, "trigger_price": _fmt(trigger_price),
        "trigger_dir": "above",
        "entry": _fmt(entry),
        "limit_price": _fmt(limit_price),   # never chase beyond this — plan self-cancels
        "stop": _fmt(stop),
        "targets": [_fmt(t) for t in tgts],
        "context_targets": [_fmt(c) for c in (context or []) if c is not None and c > entry][:3],
        "rr_t1": round(abs(tgts[0] - entry) / R, 2) if tgts else None,
        "rr_final": round(abs(tgts[-1] - entry) / R, 2) if tgts else None,
        "risk_per_share": _fmt(R),
        "entry_window_sessions": ENTRY_WINDOW_SESSIONS,
        "time_stop_sessions": time_stop,
        "expected_hold_sessions": hold,
        "no_progress": no_progress,         # e.g. {"after_sessions":8,"min_gain_pct":2}
        "quality": quality, "source": source,
        "valid_when": valid_when, "skip_when": skip_when,
    }


def _expected_hold(entry: float, target: float, atr: float, time_stop: int) -> int:
    """HEURISTIC sessions-to-target: distance ÷ (0.35 × ATR of net progress/day).
    Daily bars overlap — a stock does not advance a full ATR toward target every
    day; ~1/3 ATR of net progress is the conservative planning number (an
    optimistic 0.6 was rejected in review). Clamped to [2, time_stop]."""
    if not atr:
        return min(5, time_stop)
    days = abs(target - entry) / (0.35 * atr)
    return int(max(2, min(time_stop, round(days))))


def _exit_plan(entry: float, stop: float, targets: List[float], atr: float,
               picked: List[Dict], vp: Dict) -> Dict:
    """The exit half of the trade, stated up front. A swing is lost far more
    often by managing the way out badly than by picking the wrong entry."""
    R = entry - stop
    lvns = swing_smc.lvn_between(vp, entry, targets[-1]) if targets else []
    return {
        "t1_action": (f"sell {SCALE_OUT_PCT}% at T1 {_fmt(targets[0])}, "
                      f"move the stop on the rest to break-even {_fmt(entry)}") if targets else None,
        "runner_action": (f"trail the remainder {TRAIL_ATR_MULT}×ATR "
                          f"(≈{_fmt(TRAIL_ATR_MULT * atr)}) under the highest close, "
                          "or under each new higher swing low — whichever is tighter"),
        "target_basis": [f"{p['price']} — {p['why']}" for p in picked] or
                        ["no overhead structure: targets are 2R/3R multiples"],
        "lvn_ahead": lvns,
        "lvn_note": ("low-volume nodes between entry and target — price travels through "
                     "these fast, so a target set just BEYOND one fills more often than "
                     "one set just short of it") if lvns else None,
        "invalidation": (f"a daily CLOSE below {_fmt(stop)} ends the trade regardless of "
                         "the intraday stop — the reason for the trade is gone"),
        "r_per_share": _fmt(R),
    }


def build_smc_setups(daily: List[dict], trend: Dict, prof: Dict, ctx: Dict) -> List[Dict]:
    """Order-block / FVG / volume-profile entries — the 'where exactly do I buy'
    half that the momentum setups (S1–S3) do not answer.

    These are LIMIT entries into a zone price has left behind, not buy-stops
    above the high. They are only taken in the DISCOUNT half of the dealing
    range: buying an order block at a premium is just chasing with extra steps."""
    smc = ctx.get("smc") or {}
    vp = ctx.get("volume_profile") or {}
    if not smc.get("ok"):
        return []

    closes = [c["c"] for c in daily]
    last = closes[-1]
    atr = float(prof.get("atr14") or 0) or (last * 0.02)
    S: List[Dict] = []

    def _viable(entry, stop, t1):
        risk_pct = (entry - stop) / entry * 100
        gain_pct = (t1 - entry) / entry * 100
        return (MIN_STOP_PCT <= risk_pct <= MAX_STOP_PCT
                and gain_pct >= MIN_T1_GAIN_PCT)

    # ── S4: demand-zone limit entry (refined OB+FVG > OB > FVG > breaker) ──
    # Rank by quality: the refined pocket (order block overlapping the imbalance)
    # is the highest-probability zone and gives the tightest stop.
    cands = ([(z, "refined OB+FVG pocket", "high") for z in (smc.get("refined") or [])]
             + [(z, "demand order block", "high") for z in (smc.get("demand_ob") or [])]
             + [(z, "bullish FVG rebalance", "medium") for z in (smc.get("bull_fvg") or [])]
             + [(z, "bullish breaker (flipped supply)", "medium") for z in (smc.get("breakers") or [])])

    for z, label, qual in cands:
        top, bot = z.get("top"), z.get("bottom")
        if not top or not bot:
            continue
        dist_atr = (last - top) / atr if atr else 99
        if dist_atr > swing_smc.ZONE_MAX_DIST_ATR or top >= last:
            continue                      # too far below, or price is already in it
        # Enter at the zone's proximal edge; for an FVG use CE (consequent
        # encroachment) — a swing pullback usually rebalances to the 50% line,
        # not the whole gap.
        entry = z.get("ce") if (z.get("kind") == "FVG" and z.get("ce")) else top
        sup = swing_smc.support_ladder(smc, vp, entry)
        struct_stop = sup[0]["price"] if sup else None
        stop = min(bot - 0.25 * atr,
                   struct_stop - 0.1 * atr if struct_stop else bot - 0.25 * atr)
        if (entry - stop) / entry * 100 > MAX_STOP_PCT:
            stop = entry * (1 - MAX_STOP_PCT / 100)
        ladder = swing_smc.resistance_ladder(smc, vp, trend, entry)
        tgts, picked = swing_smc.pick_targets(ladder, entry, stop, MIN_T1_GAIN_PCT)
        if not tgts or not _viable(entry, stop, tgts[0]):
            continue

        disc = smc.get("in_discount")
        ote = smc.get("in_ote")
        S.append(_mk_setup(
            f"Demand-zone limit entry — {label} (S4)", "smc_zone",
            f"resting BUY LIMIT at {_fmt(entry)} — {label} {_fmt(bot)}–{_fmt(top)}, "
            f"unmitigated, {_fmt(z.get('dist_pct'))}% below spot"
            + (" · price is in DISCOUNT" if disc else "")
            + (" · inside the OTE 0.618–0.79 band" if ote else ""),
            entry, entry, _fmt(top + 0.15 * atr), stop, tgts,
            [trend.get("week52_high")],
            "high" if (qual == "high" and disc) else "medium",
            "SMC order block / fair value gap + composite volume profile",
            time_stop=MAX_HOLD_SESSIONS, atr=atr,
            valid_when=("zone still unmitigated when price arrives · daily close stays above "
                        f"{_fmt(stop)} · weekly stage not declining"),
            skip_when=("price closes THROUGH the zone (it is broken, not support) · "
                       "the zone is touched a second time (a mitigated block is spent) · "
                       f"limit unfilled after {ZONE_WINDOW_SESSIONS} sessions"),
            no_progress={"after_sessions": 10, "min_gain_pct": 2.0}))
        S[-1]["entry_window_sessions"] = ZONE_WINDOW_SESSIONS
        S[-1]["entry_type"] = "LIMIT (resting at the zone)"
        S[-1]["zone"] = {"top": top, "bottom": bot, "kind": z.get("kind"),
                         "source": z.get("source"), "ce": z.get("ce")}
        S[-1]["exit_plan"] = _exit_plan(entry, stop, tgts, atr, picked, vp)
        break                                   # one zone entry per stock

    # ── S5: liquidity sweep reclaim (turtle soup) ──
    # Price took out a prior swing low (stop hunt below sell-side liquidity),
    # then closed back above it. Buy the reclaim, stop under the sweep wick.
    sweep = smc.get("fresh_ssl_sweep")
    if sweep and trend["template_passes"] >= 4:
        swept = sweep.get("price")
        lows = [c["l"] for c in daily[-SWEEP_LOOKBACK:]]
        wick = min(lows) if lows else None
        if swept and wick and last > swept:
            entry = max(daily[-1]["h"], daily[-2]["h"]) + 0.05 * atr
            stop = wick - 0.25 * atr
            if (entry - stop) / entry * 100 > MAX_STOP_PCT:
                stop = entry * (1 - MAX_STOP_PCT / 100)
            ladder = swing_smc.resistance_ladder(smc, vp, trend, entry)
            tgts, picked = swing_smc.pick_targets(ladder, entry, stop, MIN_T1_GAIN_PCT)
            if tgts and _viable(entry, stop, tgts[0]):
                S.append(_mk_setup(
                    "Liquidity sweep reclaim (S5)", "sweep",
                    f"sell-side liquidity below {_fmt(swept)} was swept to {_fmt(wick)} and "
                    f"reclaimed — buy-stop the continuation over {_fmt(entry)}",
                    entry, entry, _fmt(entry + 0.5 * atr), stop, tgts,
                    [trend.get("week52_high")],
                    "medium", "SMC liquidity sweep / stop-hunt reversal",
                    time_stop=MAX_HOLD_SESSIONS, atr=atr,
                    valid_when="price holds back above the swept low · sweep is <5 sessions old",
                    skip_when="a daily close back BELOW the swept low (the sweep was real "
                              "distribution, not a hunt) · trigger unfilled in 2 sessions",
                    no_progress={"after_sessions": 8, "min_gain_pct": 2.0}))
                S[-1]["exit_plan"] = _exit_plan(entry, stop, tgts, atr, picked, vp)
                S[-1]["swept_level"] = _fmt(swept)
    return S


SWEEP_LOOKBACK = 10


def build_setups(daily: List[dict], trend: Dict, prof: Dict,
                 ctx: Optional[Dict] = None) -> List[Dict]:
    ctx = ctx or {}
    _smc = ctx.get("smc") or {}
    _vp = ctx.get("volume_profile") or {}
    closes = [c["c"] for c in daily]
    last = closes[-1]
    atr = float(prof.get("atr14") or 0) or (last * 0.02)
    hi52 = trend.get("week52_high")
    hi20_prior = max(c["h"] for c in daily[-21:-1])     # 20d high ENDING T-1
    nearness = trend.get("nearness_52w") or 0
    rvol = prof.get("rvol") or 0
    S = []

    day = daily[-1]
    rng_ok = day["h"] > day["l"]
    close_pos = ((day["c"] - day["l"]) / (day["h"] - day["l"])) if rng_ok else 1.0
    roc5 = _roc(closes, 5) or 0

    def _viable(entry, stop, t1):
        risk_pct = (entry - stop) / entry * 100
        gain_pct = (t1 - entry) / entry * 100
        return (MIN_STOP_PCT <= risk_pct <= MAX_STOP_PCT
                and gain_pct >= MIN_T1_GAIN_PCT)

    # ── S1: 52-week-high momentum breakout (primary) ──
    # Signal day T must itself be the breakout: closed over the prior 20d high,
    # on ≥1.5× volume, closing strong. Tomorrow's order is a buy-stop with a
    # hard limit cap — a gap beyond the cap = no trade, never a chase.
    if (trend["template_pass"] and nearness >= 0.90
            and day["c"] > hi20_prior and rvol >= 1.5 and close_pos >= 0.6
            and day["c"] <= 1.05 * hi20_prior and roc5 <= 12):
        trigger = day["h"] + 0.1 * atr
        limit = trigger + 0.5 * atr
        stop = max(trigger - 2.0 * atr, day["l"])
        if (trigger - stop) / trigger * 100 > MAX_STOP_PCT:
            stop = trigger * (1 - MAX_STOP_PCT / 100)
        t1 = trigger + 2.0 * (trigger - stop)
        t2 = trigger + 3.0 * (trigger - stop)
        if _viable(trigger, stop, t1):
            S.append(_mk_setup(
                "52-week-high momentum breakout (S1)", "breakout",
                f"broke the 20-day high {_fmt(hi20_prior)} today on {_fmt(rvol)}× volume — "
                f"buy-stop the continuation",
                trigger, trigger, limit, stop, [t1, t2], [hi52],
                "high", "George-Hwang 52w-high + Minervini gate",
                time_stop=MAX_HOLD_SESSIONS, atr=atr,
                valid_when="strict Stage-2 template · within 10% of the 52w high · RS leader · volume ≥1.5×",
                skip_when=f"opens beyond the limit cap {_fmt(limit)} (no chasing) · trigger not hit in 2 sessions",
                no_progress={"after_sessions": 8, "min_gain_pct": 2.0}))

    # ── S2: Trend pullback resumption ──
    # Anchor: the highest high of the last 43 sessions happened in the last 10;
    # since then a 3–8% quiet pullback holding RSI≥40; buy strength RETURNING.
    if trend["template_passes"] >= 5 and len(daily) >= 60:
        win43 = daily[-43:]
        hh = max(c["h"] for c in win43)
        hh_idx = len(daily) - 43 + max(i for i, c in enumerate(win43) if c["h"] == hh)
        sessions_since = len(daily) - 1 - hh_idx
        if 3 <= sessions_since <= 10:
            pull = daily[hh_idx + 1:]
            pull_low = min(c["l"] for c in pull) if pull else day["l"]
            depth = (hh - pull_low) / hh * 100
            max_depth = min(8.0, 2.5 * atr / last * 100)
            vols = [c.get("v") or 0 for c in daily]
            base_vol = _sma(vols[:hh_idx], 20)
            pull_vol = (sum((c.get("v") or 0) for c in pull) / len(pull)) if pull else None
            quiet = bool(base_vol and pull_vol is not None and pull_vol < 0.8 * base_vol)
            rsi_ok = all((_rsi(closes[:hh_idx + 2 + i], 14) or 0) >= 40
                         for i in range(len(pull))) if pull else False
            e20 = trend.get("ema20")
            near_e20 = bool(e20 and abs(last - e20) <= 1.0 * atr)
            if 3.0 <= depth <= max_depth and quiet and rsi_ok and near_e20:
                trigger = max(day["h"], daily[-2]["h"]) + 0.002 * last
                limit = trigger + 0.5 * atr
                stop = min(pull_low - 0.05 * atr, trigger - 1.5 * atr)
                t1 = hh                                   # prior swing high
                t2 = trigger + 2.5 * (trigger - stop)
                if _viable(trigger, stop, t1) and (trigger - stop) / trigger * 100 <= 6.0:
                    S.append(_mk_setup(
                        "Trend pullback resumption (S2)", "pullback",
                        f"quiet {depth:.1f}% pullback from {_fmt(hh)} — buy strength returning "
                        f"over {_fmt(trigger)}",
                        trigger, trigger, limit, stop, [t1, t2], [hi52],
                        "high" if depth <= 5 else "medium",
                        "Landry pullback / continuation in liquid NSE momentum",
                        time_stop=18, atr=atr,
                        valid_when="Stage-2 intact · pullback quiet (vol <0.8×) · RSI held ≥40 · near EMA20",
                        skip_when="pullback deepens past 8% (it's a reversal, not a dip) · gap over the limit cap"))

    # ── S3: Volatility-compression breakout ──
    # ≥2 tightness signals (bar-level NR7 / inside+NR4; window-level BB-squeeze /
    # ATR contraction) inside a Stage-2 name near its highs.
    flags = [prof.get("nr7"), prof.get("inside_nr4"),
             prof.get("bbw_126_low"), prof.get("atr_contracting")]
    if (trend["template_passes"] >= 5 and nearness >= 0.85
            and sum(1 for f in flags if f) >= 2 and roc5 <= 12):
        trigger = day["h"] + 0.001 * last
        limit = trigger + 0.75 * atr
        stop = min(day["l"], trigger * (1 - MIN_STOP_PCT / 100))  # ≥1.2% risk — tiny
        # stops are a trap: fixed costs + overnight gaps are huge in R terms
        t1 = trigger + 2.0 * (trigger - stop)
        t2 = trigger + 3.0 * (trigger - stop)
        if _viable(trigger, stop, t1):
            S.append(_mk_setup(
                "Volatility-compression breakout (S3)", "compression",
                f"range compressed ({sum(1 for f in flags if f)} tightness flags) — "
                f"buy-stop the expansion over {_fmt(trigger)}",
                trigger, trigger, limit, stop, [t1, t2], [_fmt(hi52)],
                "medium", "Crabel NR7/inside-day + BB-squeeze (direction from the Stage-2 gate)",
                time_stop=12, atr=atr,
                valid_when="≥2 compression flags · Stage-2 · within 15% of 52w high",
                skip_when="gap beyond the limit cap · no expansion within 2 sessions (stale)"))

    # Momentum setups keep their R-multiple targets as a FLOOR, but if the chart
    # offers real overhead structure inside that distance, take the structural
    # level instead — a target at a supply block fills; a target at 3R for no
    # reason does not. Every setup also gets its exit half stated.
    for st in S:
        try:
            e, sp = st["entry"], st["stop"]
            if not (e and sp) or not _smc.get("ok"):
                st["exit_plan"] = _exit_plan(e, sp, st.get("targets") or [], atr, [], _vp)
                continue
            ladder = swing_smc.resistance_ladder(_smc, _vp, trend, e)
            tg, picked = swing_smc.pick_targets(ladder, e, sp, MIN_T1_GAIN_PCT)
            if tg and picked:
                st["targets"] = [_fmt(t) for t in tg]
                R = abs(e - sp) or 1e-9
                st["rr_t1"] = round(abs(tg[0] - e) / R, 2)
                st["rr_final"] = round(abs(tg[-1] - e) / R, 2)
                st["expected_hold_sessions"] = _expected_hold(
                    e, tg[0], atr, st["time_stop_sessions"])
            st["exit_plan"] = _exit_plan(e, sp, st.get("targets") or [], atr, picked, _vp)
        except Exception:                                    # noqa: BLE001
            pass
    return S


# ── confluence score (0–100; weights are priors, measured by the scorecard) ─

def confluence_score(trend: Dict, prof: Dict, setups: List[Dict],
                     xsec: Optional[Dict] = None, ctx: Optional[Dict] = None) -> Dict:
    """xsec = cross-sectional stats injected by the pipeline after all plans are
    built tonight: {momentum_percentile: 0..100, ret63_gt_median: bool,
    ret21_gt_median: bool}. Without it, momentum falls back to absolute checks.

    ctx = the swing_smc context (SMC zones, volume profile, weekly stage). The
    weights below were rebalanced when it was added: 52-week nearness dropped
    15→10 and compression 10→5 to fund the 10-point SMC bucket, because for a
    zone entry "how close to the high" matters less than "am I buying at a
    discount into unmitigated demand". Weights remain priors — the scorecard is
    what actually measures them."""
    parts = {}
    x = xsec or {}

    # 1) Universe momentum rank — 25 (vol-normalized blended ROC percentile)
    mp = x.get("momentum_percentile")
    if mp is not None:
        parts["momentum_rank"] = round(25 * mp / 100)
    else:
        parts["momentum_rank"] = 12 if (trend.get("ret_63d") or 0) > 0 else 4

    # 2) 52-week-high proximity — 10 (linear ramp 0.75 → 1.00)
    nearness = trend.get("nearness_52w")
    if nearness is None or nearness < 0.75:
        parts["nearness_52w"] = 0
    else:
        parts["nearness_52w"] = round(min(10, 10 * (nearness - 0.75) / 0.25))

    # 3) Stage-2 template — 15 (pass fraction of the 7 checks)
    parts["trend_template"] = round(15 * trend["template_passes"] / 7)

    # 4) RS vs the universe median — 10
    rs = 0
    if x.get("ret63_gt_median"):
        rs += 5
    if x.get("ret21_gt_median"):
        rs += 5
    parts["rel_strength"] = rs

    # 5) Volume confirmation / accumulation — 10
    v = 0
    rvol = prof.get("rvol") or 0
    v += round(min(5, max(0, (rvol - 1.0) / 0.5 * 5)))           # 1.0× → 0 … 1.5× → 5
    udr = prof.get("updown_vol_ratio_20d")
    if udr is not None:
        v += round(min(5, max(0, (udr - 1.0) * 5)))              # 1.0 → 0 … 2.0 → 5
    parts["volume"] = min(10, v)

    # 6) Volatility compression — 5 (one credit per LEVEL, not per flag)
    bar_level = prof.get("nr7") or prof.get("inside_nr4")
    win_level = prof.get("bbw_126_low") or prof.get("atr_contracting")
    parts["compression"] = (3 if bar_level else 0) + (2 if win_level else 0)

    # 7) Risk geometry of the best setup — 10
    g = 0
    if setups:
        best = min(setups, key=lambda s: (s["entry"] - s["stop"]) / s["entry"])
        stop_pct = (best["entry"] - best["stop"]) / best["entry"] * 100
        g = 10 if stop_pct <= 3 else 7 if stop_pct <= 4.5 else 4 if stop_pct <= 6 else 1
    parts["risk_geometry"] = g

    # 8) Liquidity safety — 5 (band data unavailable → turnover + lock proxy only)
    l = 0
    t20 = prof.get("med_turnover_cr_20d") or 0
    l += 2 if t20 >= 25 else 1 if t20 >= 10 else 0
    l += 2 if (prof.get("locked_circuit_60d") or 0) == 0 else 0
    l += 1 if (prof.get("gap3_count_20d") or 0) == 0 else 0
    parts["liquidity"] = min(5, l)

    # 9) SMC / volume-profile confluence — 10
    c = ctx or {}
    smc, vp, htf = c.get("smc") or {}, c.get("volume_profile") or {}, c.get("htf") or {}
    sc = 0
    if smc.get("ok"):
        if smc.get("in_discount"):
            sc += 3                                  # buying the discount half
        if smc.get("in_ote"):
            sc += 2                                  # inside the 0.618–0.79 band
        if smc.get("refined"):
            sc += 3                                  # OB overlapping an FVG
        elif smc.get("demand_ob") or smc.get("bull_fvg"):
            sc += 2                                  # unmitigated demand below
    if htf.get("aligned_for_long"):
        sc += 1                                      # weekly stage not fighting it
    if vp.get("ok") and vp.get("position") in ("inside_value", "above_value"):
        sc += 1                                      # accepted at/above value
    parts["smc_confluence"] = min(10, sc)

    total = int(sum(parts.values()))
    if trend["template_passes"] < 5:      # stage gate: no counter-trend longs
        total = min(total, 30)
    label = "high" if total >= 70 else "medium" if total >= 45 else "low"
    return {"score": total, "label": label, "parts": parts}


# ── the swing plan builder ──────────────────────────────────────────────────

def build_swing_plan(tsym: str, daily: List[dict], plan_date: _dt.date,
                     risk_rupees: float = DEFAULT_RISK_RUPEES) -> Dict:
    """Full swing plan for one symbol. `daily` oldest-first; `plan_date` = the
    next session (first day the armed orders are live). Uses ONLY candles up to
    the evening close — no look-ahead anywhere."""
    if not daily or len(daily) < 60:
        return {"ok": False, "tsym": tsym,
                "error": f"need >=60 daily candles (have {len(daily or [])})"}

    trend = trend_state(daily)
    prof = profile(daily)
    gates = hard_gates(trend, prof, len(daily))
    last = daily[-1]["c"]

    # daily-timeframe SMC zones + composite volume profile + weekly alignment
    ctx = swing_smc.context(daily)
    smc, vp, htf = ctx.get("smc") or {}, ctx.get("volume_profile") or {}, ctx.get("htf") or {}

    setups = [] if gates else (build_setups(daily, trend, prof, ctx)
                               + build_smc_setups(daily, trend, prof, ctx))
    score = confluence_score(trend, prof, setups, None, ctx)

    for s in setups:
        dist = abs(s["entry"] - s["stop"]) if (s.get("entry") and s.get("stop")) else 0
        s["shares_for_risk"] = int(risk_rupees / dist) if dist > 0 else 0
        s["risk_rupees"] = risk_rupees
        s["entry_by"] = add_trading_days(plan_date, s["entry_window_sessions"] - 1).isoformat()
        s["expected_exit_date"] = add_trading_days(
            plan_date, s["expected_hold_sessions"]).isoformat()
        s["exit_by_date"] = add_trading_days(plan_date, s["time_stop_sessions"]).isoformat()

    qorder = {"high": 0, "medium": 1, "low": 2}
    # On equal quality prefer the zone entry: a limit AT support risks less per
    # share than a stop above the high, so the same rupee risk buys more size.
    primary = sorted(setups, key=lambda s: (qorder.get(s["quality"], 3),
                                            0 if s["type"] in ("smc_zone", "sweep") else 1)
                     )[0] if setups else None

    # the entry window follows the PRIMARY setup: a limit resting at a zone is
    # given longer to fill than a buy-stop chasing a breakout
    _win = (primary or {}).get("entry_window_sessions", ENTRY_WINDOW_SESSIONS)
    _otype = "LIMIT" if (primary or {}).get("entry_type", "").startswith("LIMIT") else "STOP"

    bias = ("bullish" if trend["stage"] == "uptrend"
            else "bearish" if trend["stage"] == "downtrend" else "neutral")

    if gates:
        headline = f"NO SWING — {gates[0]}"
    elif not setups:
        headline = {"uptrend": "Stage-2 uptrend but no clean entry tonight — wait, don't chase.",
                    "downtrend": "Downtrend — stand aside (no shorting delivery in India).",
                    "sideways": "No trend edge — basing/undecided. Wait for a real breakout."}[trend["stage"]]
    else:
        p = primary
        verb = ("buy-limit" if p.get("entry_type", "").startswith("LIMIT") else "buy-stop")
        headline = (f"{p['name']}: {verb} {p['trigger_price']} (cap {p['limit_price']}), "
                    f"stop {p['stop']}, T1 {p['targets'][0] if p['targets'] else '—'} · "
                    f"~{p['expected_hold_sessions']} sessions (heuristic) · "
                    f"hard exit by {p['exit_by_date']}")

    return {
        "ok": True, "tsym": tsym.upper(), "mode": "swing",
        "plan_date": plan_date.isoformat(),
        "last_close": _fmt(last),
        "bias": bias, "stage": trend["stage"],
        "conviction": score["label"], "score": score["score"], "score_parts": score["parts"],
        "trend": trend, "profile": prof,
        "smc": smc, "volume_profile": vp, "htf": htf,
        "headline": headline,
        "primary": primary["name"] if primary else None,
        "setups": setups, "num_setups": len(setups),
        "entry_window": {
            "first_session": plan_date.isoformat(),
            "last_session": add_trading_days(plan_date, _win - 1).isoformat(),
            "sessions": _win,
            "order_type": _otype,
            "note": (f"the resting BUY LIMIT at the zone stays live {_win} sessions — if price "
                     "never comes back to the zone there is simply no trade, which is the point"
                     if _otype == "LIMIT" else
                     f"buy-stops live {_win} sessions, then the plan is STALE — rebuild, never chase"),
        },
        "gates_failed": gates,
        "no_trade": _no_trade_rules(trend, prof, gates),
        "risk_rules": _RISK_RULES,
        "data_warnings": _DATA_WARNINGS,
        "estimate_note": ("Expected hold/exit dates are HEURISTICS (net progress ≈ 0.35×ATR/day "
                          "toward target, uncalibrated). The hard number is the time-stop date."),
        "cost_note": (f"Delivery round trip ≈ {SWING_ROUND_TRIP_COST_PCT}% of notional on Upstox "
                      f"(STT 0.1%×2 + ₹20/order×2 + stamp + DP fee + slippage, ₹25k position). "
                      f"First target must clear {MIN_T1_GAIN_PCT}%. Positions under "
                      f"₹{MIN_POSITION_RUPEES:,} are uneconomical (flat DP fee). All replays are "
                      "scored net of this. Analysis only — no orders are placed."),
    }


def _no_trade_rules(trend: Dict, prof: Dict, gates: List[str]) -> List[str]:
    rules = list(gates)
    rules += [
        "Gap opens beyond the setup's limit cap: NO trade — the planned R:R no longer exists.",
        "Trigger not hit within the entry window: plan is STALE — rebuild, don't chase.",
        "Board meeting / results inside the holding window: exit or halve before the event — gaps jump ANY stop.",
        "Stock enters ASM/GSM/ESM after the build: exit-only mindset; margins jump, liquidity dies.",
        "Never carry full size into a known macro event (Budget/RBI/Fed).",
    ]
    if trend["stage"] == "downtrend":
        rules.insert(0, "Downtrend: NO long swing. Shorting delivery isn't possible for retail — stand aside.")
    return rules


_RISK_RULES = [
    "Size from the stop: shares = risk_₹ ÷ (entry−stop). Risk ≤ 0.75% of capital per swing.",
    f"Position value: ≥ ₹{MIN_POSITION_RUPEES:,} (DP-fee floor) and ≤ 20% of capital, ≤ 1% of the stock's daily turnover.",
    "Stops are GTT orders, but overnight gaps fill THROUGH them — budget worst case ≈ stop + 1 ATR.",
    "Max 4–5 concurrent swings; correlated names count as one position.",
    "Time stop is not optional: dead money is risk. Exit at the time-stop date regardless.",
    "Never average a loser; a stopped-out plan is finished until a fresh evening plan says otherwise.",
]

_DATA_WARNINGS = [
    "ASM/GSM/ESM surveillance lists NOT checked (no feed) — verify on nseindia.com before entry.",
    "Daily price bands NOT checked (no feed) — a 5%-band stock can lock limit-up/down through your levels.",
    "Earnings/board-meeting calendar NOT checked (no feed) — verify no results fall inside the holding window.",
]
