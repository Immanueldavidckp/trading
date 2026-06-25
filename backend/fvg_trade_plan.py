"""
FVG trade-plan engine — entry / stop / target / profit / time / win-rate.

Every number is either a deterministic geometric construction from the current
unmitigated FVG, or an EMPIRICAL, look-ahead-free backtest statistic from
replaying the identical rule over the stored candles. No guessed probabilities.

Methodology (designed + adversarially reviewed for statistical honesty):
  - FVG detection re-implemented CAUSALLY here (never reuse smc_engine's
    `mitigated`, which scans the future). Outcomes read only bars after a gap
    forms — appending future candles can never change a recorded WIN/LOSS.
  - Same-bar ambiguity resolves AGAINST you: stop+target same bar => LOSS;
    a target tagged on the fill bar itself is NOT a win.
  - Overlapping same-direction gaps deduped (IoU>=0.5, keep earliest) and a
    one-open-trade temporal lock => near-independent trials, so the CI is honest.
  - NO_FILL / TIMEOUT excluded from the win-rate, reported as separate rates.
  - Hard sample gates: n<20 => no win-rate; n<30 => LOW_CONFIDENCE; ETA needs
    >=8 winning trades. Win-rate always shown as p_hat + Wilson 95% CI + n.
  - One frozen headline config (R-multiple 2.0, fill at gap mid). Other cells
    are sensitivity-only, never advertised.
"""

import math
from typing import List, Optional

CFG = dict(
    RMULT=2.0,        # headline target = entry + 2R (fixed a-priori, all TFs)
    BETA_FRAC=0.10,   # stop buffer = 0.10 * gap_size beyond the far edge
    OVL_TOL=0.50,     # same-dir FVGs with IoU >= 0.5 merged (keep earliest)
    ENTRY_WINDOW=50,  # max bars a resting limit waits to fill, else NO_FILL
    HOLD_MAX=200,     # max bars to hold, else TIMEOUT
    Z=1.96,           # Wilson 95%
    N_MIN=20,         # below this: no numeric win-rate
    N_LOWCONF=30,     # below this: LOW_CONFIDENCE flag
    N_WINS_MIN=8,     # below this: no ETA
    TRADING_SECONDS_PER_DAY=22500,  # NSE 09:15-15:30
)


# ── causal FVG detection (formation-time geometry only) ─────────────────────

def detect_causal_fvgs(C: List[dict]) -> List[dict]:
    out = []
    for i in range(1, len(C) - 1):
        p, n3 = C[i - 1], C[i + 1]
        if n3["l"] > p["h"]:                 # bullish imbalance
            top, bottom, d = n3["l"], p["h"], 1
        elif n3["h"] < p["l"]:               # bearish imbalance
            top, bottom, d = p["l"], n3["h"], -1
        else:
            continue
        gap = top - bottom
        mid = (top + bottom) / 2.0
        if mid <= 0 or gap <= 0:
            continue
        out.append({"dir": d, "top": top, "bottom": bottom, "size": gap,
                    "mid": mid, "f": i + 1})   # formation bar = i+1
    return out


def _dedupe(fvgs: List[dict]) -> List[dict]:
    kept: List[dict] = []
    for g in fvgs:
        dup = False
        for k in kept:
            if k["dir"] != g["dir"]:
                continue
            overlap = max(0.0, min(k["top"], g["top"]) - max(k["bottom"], g["bottom"]))
            union = max(k["top"], g["top"]) - min(k["bottom"], g["bottom"])
            if union > 0 and overlap / union >= CFG["OVL_TOL"]:
                dup = True
                break
        if not dup:
            kept.append(g)
    return kept


def _geometry(g: dict):
    """fill (mid), stop, R1, target — identical in backtest and live plan."""
    d = g["dir"]
    fill = g["mid"]
    stop = (g["bottom"] - CFG["BETA_FRAC"] * g["size"]) if d > 0 \
        else (g["top"] + CFG["BETA_FRAC"] * g["size"])
    R1 = abs(fill - stop)
    target = fill + d * CFG["RMULT"] * R1
    return fill, stop, R1, target


# ── bias-free forward backtest ──────────────────────────────────────────────

def _backtest(C: List[dict]) -> List[dict]:
    fvgs = _dedupe(detect_causal_fvgs(C))
    trades = []
    next_free = 0
    n = len(C)

    for g in fvgs:
        d = g["dir"]
        fill, stop, R1, target = _geometry(g)
        if R1 <= 0:
            continue

        # entry: earliest bar (after formation, after any open trade) whose
        # range crosses the fill price.
        e_start = max(g["f"] + 1, next_free)
        entry_idx = None
        for k in range(e_start, min(e_start + CFG["ENTRY_WINDOW"] + 1, n)):
            if C[k]["l"] <= fill <= C[k]["h"]:
                entry_idx = k
                break
        if entry_idx is None:
            trades.append({"outcome": "NO_FILL"})
            continue

        # resolve forward: stop-first on same-bar; fill-bar target not counted.
        outcome, exit_idx = None, None
        for k in range(entry_idx, min(entry_idx + CFG["HOLD_MAX"] + 1, n)):
            if d > 0:
                ht, hs = (C[k]["h"] >= target), (C[k]["l"] <= stop)
            else:
                ht, hs = (C[k]["l"] <= target), (C[k]["h"] >= stop)
            if k == entry_idx:
                if hs:
                    outcome, exit_idx = "LOSS", k
                    break
                continue
            if ht and hs:
                outcome, exit_idx = "LOSS", k      # stop-first
                break
            if hs:
                outcome, exit_idx = "LOSS", k
                break
            if ht:
                outcome, exit_idx = "WIN", k
                break
        if outcome is None:
            outcome, exit_idx = "TIMEOUT", min(entry_idx + CFG["HOLD_MAX"], n - 1)

        next_free = exit_idx if outcome in ("WIN", "LOSS") else entry_idx + 1
        trades.append({"outcome": outcome, "entry_idx": entry_idx, "exit_idx": exit_idx,
                       "R1": R1, "bars": (exit_idx - entry_idx) if exit_idx is not None else None})
    return trades


def _wilson(w: int, n: int):
    if n == 0:
        return (0.0, 0.0)
    z = CFG["Z"]
    p = w / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _pct(arr, q):
    if not arr:
        return None
    s = sorted(arr)
    idx = max(0, min(len(s) - 1, int(round((q / 100.0) * (len(s) - 1)))))
    return s[idx]


def _summarize(trades: List[dict], n_candles: int) -> dict:
    resolved = [t for t in trades if t["outcome"] in ("WIN", "LOSS")]
    n = len(resolved)
    w = sum(1 for t in resolved if t["outcome"] == "WIN")
    total = len(trades)
    no_fill = sum(1 for t in trades if t["outcome"] == "NO_FILL")
    timeout = sum(1 for t in trades if t["outcome"] == "TIMEOUT")

    base = {
        "n": n, "wins": w, "total": total,
        "fill_rate": round((total - no_fill) / total, 3) if total else 0.0,
        "timeout_rate": round(timeout / total, 3) if total else 0.0,
        "trades_per_100_bars": round(total / (n_candles / 100.0), 2) if n_candles else 0.0,
    }
    if n < CFG["N_MIN"]:
        base["flag"] = "INSUFFICIENT_SAMPLE"
        base["win_rate"] = None
        return base

    p = w / n
    ci_lo, ci_hi = _wilson(w, n)
    win_bars = [t["bars"] for t in resolved if t["outcome"] == "WIN" and t["bars"] is not None]
    eta = None
    if len(win_bars) >= CFG["N_WINS_MIN"]:
        eta = {"bars_p50": _pct(win_bars, 50), "bars_p25": _pct(win_bars, 25),
               "bars_p75": _pct(win_bars, 75), "n_wins": len(win_bars)}
    base.update({
        "win_rate": round(p, 3), "ci": [round(ci_lo, 3), round(ci_hi, 3)],
        "ev_R": round(p * CFG["RMULT"] - (1 - p), 3),
        "flag": "LOW_CONFIDENCE" if n < CFG["N_LOWCONF"] else "OK",
        "_eta": eta,
    })
    return base


# ── live FVG selection ──────────────────────────────────────────────────────

def select_live_fvg(C: List[dict]) -> Optional[dict]:
    """Freshest unmitigated FVG as-of the last bar (legit for a trade taken
    now). Mitigated = price already traded through the far edge."""
    last = C[-1]["c"]
    best = None
    for g in detect_causal_fvgs(C):
        mit = False
        for j in range(g["f"] + 1, len(C)):
            if (g["dir"] > 0 and C[j]["l"] <= g["bottom"]) or \
               (g["dir"] < 0 and C[j]["h"] >= g["top"]):
                mit = True
                break
        if mit:
            continue
        dist = 0.0 if g["bottom"] <= last <= g["top"] else min(abs(last - g["top"]), abs(last - g["bottom"]))
        key = (g["f"], -dist)   # freshest, then nearest to price
        if best is None or key > best[0]:
            best = (key, g)
    return best[1] if best else None


# ── time humanizer (session-aware, NSE) ─────────────────────────────────────

def _humanize(seconds: float) -> str:
    if seconds is None:
        return "—"
    spd = CFG["TRADING_SECONDS_PER_DAY"]
    if seconds < spd:
        m = seconds / 60.0
        if m < 60:
            return f"~{int(round(m / 5.0) * 5) or 5} min"
        return f"~{round(m / 60.0, 1)} hours"
    days = seconds / spd
    return f"~{round(days, 1)} trading day" + ("s" if round(days, 1) != 1 else "")


# ── structural target (live only — smc_engine allowed) ──────────────────────

def _structural_target(C: List[dict], d: int, fill: float) -> Optional[dict]:
    try:
        import smc_engine
        res = smc_engine.analyze(C, recent_n=0)
        rng = res.get("range") or {}
        levels = []
        if d > 0 and rng.get("high") and rng["high"] > fill:
            levels.append(("range_high", rng["high"]))
        if d < 0 and rng.get("low") and rng["low"] < fill:
            levels.append(("range_low", rng["low"]))
        # nearest opposing unmitigated zone beyond fill
        for o in res.get("order_blocks", []):
            if o["mitigated"]:
                continue
            if d > 0 and o["bottom"] > fill:
                levels.append(("ob", o["bottom"]))
            if d < 0 and o["top"] < fill:
                levels.append(("ob", o["top"]))
        if not levels:
            return None
        src, price = min(levels, key=lambda x: abs(x[1] - fill)) if d > 0 else \
            min(levels, key=lambda x: abs(x[1] - fill))
        return {"price": round(price, 2), "source": src}
    except Exception:
        return None


# ── assembler ───────────────────────────────────────────────────────────────

def fvg_trade_plan(candles: List[dict], bar_seconds: int) -> dict:
    D = bar_seconds
    if not candles or len(candles) < 30:
        return {"setup": False, "reason": "insufficient_history",
                "n": len(candles or []), "tf_seconds": D}

    stats = _summarize(_backtest(candles), len(candles))
    g = select_live_fvg(candles)
    if g is None:
        return {"setup": False, "reason": "no_unmitigated_aligned_fvg",
                "tf_seconds": D, "accuracy": _accuracy_block(stats, D)}

    d = g["dir"]
    fill, stop, R1, target = _geometry(g)
    if R1 <= 0:
        return {"setup": False, "reason": "degenerate_gap", "tf_seconds": D}

    def tp(mult):
        price = fill + d * mult * R1
        return {"price": round(price, 2), "profit_per_share": round(abs(price - fill), 2),
                "rr": round(mult, 2),
                "range": [round(price - CFG["BETA_FRAC"] * g["size"], 2),
                          round(price + CFG["BETA_FRAC"] * g["size"], 2)]}

    T1, T2, T3 = tp(1.0), tp(2.0), tp(3.0)
    struct = _structural_target(candles, d, fill)
    target_hi = max(T3["price"], struct["price"]) if struct else T3["price"]

    acc = _accuracy_block(stats, D)
    ev = None
    if acc.get("value") is not None:
        ev = {"ev_R": stats["ev_R"], "ev_per_share": round(stats["ev_R"] * R1, 2)}

    return {
        "setup": True, "tf_seconds": D,
        "direction": "bull" if d > 0 else "bear",
        "side": "LONG" if d > 0 else "SHORT",
        "fvg": {"top": round(g["top"], 2), "bottom": round(g["bottom"], 2),
                "mid": round(g["mid"], 2),
                "gap_pct": round(g["size"] / g["mid"] * 100, 3)},
        "entry": {"range": [round(g["bottom"], 2), round(g["top"], 2)],
                  "fill": round(fill, 2), "model": "gap mid (CE)"},
        "stop": round(stop, 2),
        "risk_per_share": round(R1, 2),
        "targets": {"T1": T1, "T2": T2, "T3": T3, "structural": struct, "headline_rr": CFG["RMULT"]},
        "target_range": [T1["price"], round(target_hi, 2)],
        "accuracy": acc,
        "ev": ev,
        "eta": _eta_block(stats, D),
        "disclaimer": _disclaimer(stats, bar_seconds),
    }


def _accuracy_block(stats: dict, D: int) -> dict:
    common = {"n": stats["n"], "wins": stats["wins"], "fill_rate": stats["fill_rate"],
              "timeout_rate": stats["timeout_rate"],
              "trades_per_100_bars": stats["trades_per_100_bars"]}
    if stats.get("flag") == "INSUFFICIENT_SAMPLE" or stats.get("win_rate") is None:
        return {**common, "value": None, "flag": "INSUFFICIENT_SAMPLE",
                "note": "not enough history to score this setup"}
    return {**common, "value": stats["win_rate"], "ci": stats["ci"], "flag": stats["flag"],
            "method": "walk-forward backtest of this exact FVG rule on stored candles",
            "caveats": ["fills modeled at level, no slippage",
                        "in-sample / regime-dependent", "trades not fully independent"]}


def _eta_block(stats: dict, D: int) -> dict:
    eta = stats.get("_eta")
    if not eta:
        return {"value": None, "note": "not enough winning trades to estimate time"}
    return {"p50_human": _humanize(eta["bars_p50"] * D),
            "p25_human": _humanize(eta["bars_p25"] * D),
            "p75_human": _humanize(eta["bars_p75"] * D),
            "median_bars": eta["bars_p50"], "n_wins": eta["n_wins"],
            "estimator": "empirical (median of winning trades)",
            "note": "conditional on the trade winning"}


def _disclaimer(stats: dict, bar_seconds: int) -> str:
    wr = stats.get("win_rate")
    if wr is None:
        return "Not enough historical FVG setups to score this timeframe yet."
    loss_pct = round((1 - wr) * 100)
    return (f"Historical backtest of stored candles. ~{loss_pct}% of these setups hit "
            f"the stop first. Fills modeled at level with no slippage; live results vary.")
