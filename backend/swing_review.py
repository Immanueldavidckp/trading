"""
Post-mortem of finished swing trades: why did we lose, on which stocks, what
did the plan think of them at the time, and what would have avoided it.

Reads the same rows the COMPLETED board shows (swing_monitor, phase ENTRY /
state closed) joined back to the plan they came from (swing_plans), so every
loss is seen with the score, conviction, quality and levels the plan gave it
on the evening it was built — not with hindsight.

Everything here is descriptive. A bucket with fewer than MIN_N trades is shown
but never used to draw a conclusion; the findings say so.

    python3 swing_review.py            # text report, last 60 days
    python3 swing_review.py --days 30
    GET /api/swing/review?days=60
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional

import db as _db
import trading_calendar as cal
import swing_monitor as M

IST = cal.IST
MIN_N = 5                 # smallest bucket a finding may rest on
SWING_ROUND_TRIP_COST_PCT = 0.65


# ── load ─────────────────────────────────────────────────────────────────────

def _closed_rows(days: int) -> List[Dict]:
    """Closed trades from the last `days`, each with its plan's meta and setup."""
    st = M.status(refresh=False)
    rows = [r for r in st.get("rows", []) if r.get("phase") == "COMPLETED"]
    cut = (cal.today_ist() - _dt.timedelta(days=days)).isoformat()
    rows = [r for r in rows if (r.get("exit_date") or r.get("updated_at") or "")[:10] >= cut]
    # plan-level context the board does not carry
    conn = _db.connect()
    try:
        cur = conn.cursor(); PH = _db.PLACE
        keys = sorted({(r["plan_date"], r["tsym"]) for r in rows})
        meta = {}
        for pd_, t in keys:
            cur.execute(f"SELECT rank_n,bias,plan_json FROM swing_plans WHERE plan_date={PH} AND tsym={PH}", [pd_, t])
            got = cur.fetchone()
            if not got:
                continue
            try:
                pl = json.loads(got[2] or "{}")
            except Exception:
                pl = {}
            meta[(pd_, t)] = {"rank_n": got[0], "bias": got[1], "stage": pl.get("stage"),
                              "trend": pl.get("trend"), "sideways": pl.get("sideways"),
                              "downtrend": pl.get("downtrend"), "gates_failed": pl.get("gates_failed"),
                              "primary": pl.get("primary")}
        cur.close()
    finally:
        conn.close()
    for r in rows:
        r.update(meta.get((r["plan_date"], r["tsym"]), {}))
    return rows


# ── per-trade facts ──────────────────────────────────────────────────────────

def _setup_type(r: Dict) -> str:
    st = r.get("setup") or {}
    if st.get("type"):
        return str(st["type"])
    m = re.search(r"\((S\d)\)", r.get("setup_name") or "")
    return m.group(1) if m else "other"


def _sessions_between(a: Optional[str], b: Optional[str]) -> Optional[int]:
    """Trading sessions from a to b (a exclusive, b inclusive)."""
    if not a or not b:
        return None
    try:
        d0 = _dt.date.fromisoformat(a[:10]); d1 = _dt.date.fromisoformat(b[:10])
    except ValueError:
        return None
    if d1 <= d0:
        return 0
    n, d = 0, d0
    while d < d1 and n < 60:
        d = cal.next_trading_day(d); n += 1
    return n


def _after_exit(r: Dict) -> Dict:
    """What price did after we were taken out — the 'stop too tight' test.

    For a stopped trade: did the stock come back to the entry, or all the way
    to T1, before the plan's own hard-exit date? If so the level was right and
    the stop was too close (or the fill was on a shakeout day)."""
    out = {"recovered_entry": None, "recovered_t1": None, "gap_through_pct": None,
           "low_after_pct": None}
    if r.get("exit_kind") != "stop" or not r.get("exit_date"):
        return out
    st = r.get("setup") or {}
    entry, stop = st.get("entry", r.get("entry")), st.get("stop", r.get("stop"))
    t1 = (st.get("targets") or r.get("targets") or [None])[0]
    until = st.get("exit_by_date") or r.get("exit_by")
    try:
        start = _dt.date.fromisoformat(r["exit_date"][:10])
        bars = M._daily_since(r["tsym"], start)
    except Exception:
        return out
    if not bars:
        return out
    day = [b for b in bars if _dt.datetime.fromtimestamp(b["t"] / 1000, IST).date() == start]
    if day and stop:
        out["gap_through_pct"] = round((stop - day[0]["l"]) / stop * 100, 2)
    later = [b for b in bars if _dt.datetime.fromtimestamp(b["t"] / 1000, IST).date() > start
             and (not until or _dt.datetime.fromtimestamp(b["t"] / 1000, IST).date().isoformat() <= until)]
    if later:
        hi = max(b["h"] for b in later); lo = min(b["l"] for b in later)
        if entry:
            out["recovered_entry"] = bool(hi >= entry)
            out["low_after_pct"] = round((lo - entry) / entry * 100, 2)
        if t1:
            out["recovered_t1"] = bool(hi >= t1)
    return out


def _enrich(r: Dict, with_candles: bool) -> Dict:
    st = r.get("setup") or {}
    entry = st.get("entry", r.get("entry")); stop = st.get("stop", r.get("stop"))
    atr = r.get("atr14")
    pct = r.get("realized_pct")
    e = {
        "tsym": r["tsym"], "plan_date": r["plan_date"], "setup": r.get("setup_name"),
        "type": _setup_type(r), "sector": r.get("sector"),
        "score": r.get("score"), "conviction": r.get("conviction"), "quality": st.get("quality"),
        "source": st.get("source"), "zone_kind": ((st.get("zone") or {}).get("kind")),
        "entry_type": r.get("entry_type"), "primary": (r.get("primary") == r.get("setup_name")) if r.get("primary") else None,
        "stage": r.get("stage"), "trend": r.get("trend"), "bias": r.get("bias"),
        "entry": entry, "stop": stop, "t1": (st.get("targets") or r.get("targets") or [None])[0],
        "rr_t1": st.get("rr_t1"),
        "stop_pct": round((entry - stop) / entry * 100, 2) if (entry and stop is not None) else None,
        "stop_atr": round((entry - stop) / atr, 2) if (entry and stop is not None and atr) else None,
        "fill_price": r.get("fill_price"), "fill_date": r.get("fill_date"),
        "fill_lag": _sessions_between(r.get("plan_date"), r.get("fill_date")),
        "exit_kind": r.get("exit_kind"), "exit_price": r.get("exit_price"), "exit_date": r.get("exit_date"),
        "held": r.get("held_sessions"),
        "pct": pct, "r": r.get("realized_r"), "rupees": r.get("realized_rupees"),
        "win": (pct > 0) if pct is not None else None,
        "broke_zone": bool(r.get("broke_zone")),
        "reason": r.get("reason"),
    }
    e.update(_after_exit(r) if with_candles else
             {"recovered_entry": None, "recovered_t1": None, "gap_through_pct": None, "low_after_pct": None})
    return e


# ── aggregation ──────────────────────────────────────────────────────────────

def _stats(rows: List[Dict]) -> Dict:
    pr = [x for x in rows if x["pct"] is not None]
    wins = [x for x in pr if x["win"]]; losses = [x for x in pr if not x["win"]]
    rs = [x["r"] for x in pr if x["r"] is not None]
    wr = [x["r"] for x in wins if x["r"] is not None]; lr = [x["r"] for x in losses if x["r"] is not None]
    rup = [x["rupees"] for x in pr if x["rupees"] is not None]
    avg_w = sum(wr) / len(wr) if wr else None
    avg_l = sum(lr) / len(lr) if lr else None
    payoff = (avg_w / abs(avg_l)) if (avg_w is not None and avg_l) else None
    return {"n": len(rows), "priced": len(pr), "wins": len(wins), "losses": len(losses),
            "hit": round(len(wins) / len(pr) * 100, 1) if pr else None,
            "sum_r": round(sum(rs), 2) if rs else None,
            "exp_r": round(sum(rs) / len(rs), 3) if rs else None,
            "avg_win_r": round(avg_w, 2) if avg_w is not None else None,
            "avg_loss_r": round(avg_l, 2) if avg_l is not None else None,
            "payoff": round(payoff, 2) if payoff else None,
            "breakeven_hit": round(100 / (1 + payoff), 1) if payoff else None,
            "rupees": round(sum(rup)) if rup else None,
            "avg_pct": round(sum(x["pct"] for x in pr) / len(pr), 2) if pr else None}


def _bucket(rows: List[Dict], key, label_order: Optional[List[str]] = None) -> List[Dict]:
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for x in rows:
        groups[str(key(x))].append(x)
    labels = label_order or sorted(groups, key=lambda k: -len(groups[k]))
    out = []
    for lab in labels:
        if lab in groups:
            out.append({"bucket": lab, **_stats(groups[lab])})
    for lab in groups:
        if label_order and lab not in label_order:
            out.append({"bucket": lab, **_stats(groups[lab])})
    return out


def _score_bucket(s) -> str:
    if s is None:
        return "n/a"
    s = float(s)
    return "90+" if s >= 90 else "80-89" if s >= 80 else "70-79" if s >= 70 else "60-69" if s >= 60 else "<60"


def _stop_atr_bucket(v) -> str:
    if v is None:
        return "n/a"
    return "<0.75 ATR" if v < 0.75 else "0.75-1.5 ATR" if v < 1.5 else "1.5-2.5 ATR" if v < 2.5 else ">2.5 ATR"


def _fill_lag_bucket(v) -> str:
    if v is None:
        return "n/a"
    return "day 1" if v <= 1 else "day 2" if v == 2 else "day 3+"


def _findings(t: Dict, rows: List[Dict], stops: List[Dict], by: Dict, clusters: List[Dict]) -> List[Dict]:
    """Plain statements a trader can act on, each tied to the number behind it.
    Nothing is asserted from a bucket smaller than MIN_N."""
    F: List[Dict] = []
    add = lambda sev, title, detail, fix=None: F.append({"severity": sev, "title": title, "detail": detail, "fix": fix})
    if t["priced"] < MIN_N:
        add("info", "Too few finished trades to conclude anything",
            f"{t['priced']} priced trades; the tables below are shown for reference only.")
        return F

    # 1 · is the problem how often we win, or how much we lose when we do?
    if t["hit"] is not None and t["breakeven_hit"] is not None:
        if t["hit"] < t["breakeven_hit"]:
            add("high", "Losses outnumber wins beyond what the payoff can carry",
                f"Hit rate {t['hit']}% against a break-even of {t['breakeven_hit']}% at the current payoff "
                f"({t['avg_win_r']}R average win vs {t['avg_loss_r']}R average loss). The wins are the right size; "
                f"there are not enough of them.",
                "Fewer, better-filtered entries. See which buckets below carry the hit rate down, and stop taking those.")
        else:
            add("info", "Hit rate clears break-even at this payoff",
                f"{t['hit']}% vs break-even {t['breakeven_hit']}%.")
    cost_r = None
    if rows and rows[0].get("entry") and rows[0].get("stop_pct"):
        # cost in R at the plan's typical stop: 0.65% of notional ÷ stop%
        sp = [x["stop_pct"] for x in rows if x["stop_pct"]]
        if sp:
            med = sorted(sp)[len(sp) // 2]
            cost_r = round(SWING_ROUND_TRIP_COST_PCT / med, 2) if med else None
    if t["exp_r"] is not None and cost_r is not None:
        net = round(t["exp_r"] - cost_r, 3)
        add("high" if net < 0 else "info",
            "Expectancy after costs is " + ("negative" if net < 0 else "positive"),
            f"{t['exp_r']}R per trade before costs; a round trip costs about {cost_r}R at the plan's median stop, "
            f"leaving {net}R per trade.",
            None if net >= 0 else "Do not trade this live until this number is positive over 60+ trades.")

    # 2 · stops: gapped through, or too tight?
    if stops:
        gap = [x["gap_through_pct"] for x in stops if x["gap_through_pct"] is not None]
        big = [g for g in gap if g > 0.5]
        if gap and len(big) / len(gap) >= 0.3:
            add("med", "Stops are being gapped through, not touched",
                f"{len(big)} of {len(gap)} stops saw the day's low more than 0.5% below the stop "
                f"(average overshoot {round(sum(gap)/len(gap),2)}%). A GTT at the stop fills at the open, not the level.",
                "Budget the loss as stop + 1 ATR, and skip setups whose stop sits within 0.5 ATR of a round number or the prior day's low.")
        rec_t1 = [x for x in stops if x["recovered_t1"]]
        rec_e = [x for x in stops if x["recovered_entry"]]
        known = [x for x in stops if x["recovered_entry"] is not None]
        if known and len(rec_e) / len(known) >= 0.4:
            add("high", "Many stops were shakeouts — the level was right, the stop was too close",
                f"{len(rec_e)} of {len(known)} stopped trades came back above the entry before the hard-exit date, "
                f"and {len(rec_t1)} reached T1. Names: {', '.join(x['tsym'] for x in rec_e[:8])}.",
                "Place the stop beyond the zone by at least 0.5 ATR (the engine uses 0.25), or size at half and add on reclaim.")
        sdb = [x for x in stops if x["broke_zone"]]
        if len(sdb) >= 3 and len(sdb) / len(stops) >= 0.25:
            add("med", "Zones broke on the very day they filled",
                f"{len(sdb)} of {len(stops)} stops filled and closed below the stop in the same session — "
                f"the zone was run through, not respected. Names: {', '.join(x['tsym'] for x in sdb[:8])}.",
                "Do not rest the limit in the zone on a down day; require the first touch to hold (a close back above the zone) before entering.")

    # 3 · were the losses one bad market week?
    if clusters and stops:
        top3 = sum(c["stops"] for c in clusters[:3])
        if top3 / len(stops) >= 0.5 and len(stops) >= MIN_N:
            add("high", "The losses came in clusters — market days, not stock picks",
                f"{top3} of {len(stops)} stops happened on just three dates: "
                + "; ".join(f"{c['date']} ({c['stops']}: {', '.join(c['names'][:5])}{'…' if len(c['names'])>5 else ''})" for c in clusters[:3])
                + ". Long-only swings all fail together when the index falls.",
                "Add a market gate: no new fills when the index closed below its 20-day average or breadth is negative; "
                "and cap concurrent positions at 5 (the rulebook) so one bad day cannot hit 19 names.")

    # 4 · buckets that carry the hit rate down. A bucket is only named when it
    #     is clearly worse than the whole — with a 27% hit rate overall, "below
    #     25%" would flag everything and say nothing. Worst four by money lost.
    base_hit = t["hit"] if t["hit"] is not None else 50.0
    base_exp = t["exp_r"] if t["exp_r"] is not None else 0.0
    weak = []
    for dim, key in (("Setup", "type"), ("Conviction", "conviction"), ("Quality", "quality"),
                     ("Stop distance", "stop_atr"), ("Score", "score"), ("Fill day", "fill_lag"),
                     ("Entry type", "entry_type"), ("Zone", "zone_kind"), ("Sector", "sector")):
        for b in by[key]:
            if (b["priced"] >= MIN_N and b["hit"] is not None and b["exp_r"] is not None and b["sum_r"] is not None
                    and b["hit"] <= base_hit - 8 and b["exp_r"] <= base_exp - 0.15 and b["sum_r"] < 0
                    and b["bucket"] not in ("n/a", "none", "other")):
                weak.append((dim, b))
    weak.sort(key=lambda db: db[1]["sum_r"])
    for dim, b in weak[:4]:
        add("med", f"{dim} = {b['bucket']} is where the money goes",
            f"{b['wins']}W / {b['losses']}L ({b['hit']}% hit vs {base_hit}% overall; {b['exp_r']}R per trade, "
            f"{b['sum_r']}R total) across {b['priced']} trades.",
            f"Stop taking {dim} = {b['bucket']} until it proves itself on paper.")
    # 5 · and the one that works, if any — again only if clearly better than the whole
    best = [b for dim in ("type", "conviction", "stop_atr", "zone_kind", "fill_lag") for b in by[dim]
            if b["priced"] >= MIN_N and b["exp_r"] is not None and b["hit"] is not None
            and b["exp_r"] >= base_exp + 0.3 and b["hit"] >= base_hit + 10 and b["bucket"] not in ("n/a", "none", "other")]
    if best:
        b = max(best, key=lambda x: x["exp_r"])
        add("info", f"What is working: {b['bucket']}",
            f"{b['wins']}W / {b['losses']}L, {b['hit']}% hit vs {base_hit}% overall, {b['exp_r']}R per trade over {b['priced']} trades.",
            "Concentrate the small account here first.")
    # 6 · did the score mean anything?
    sc = [b for b in by["score"] if b["priced"] >= MIN_N and b["hit"] is not None]
    if len(sc) >= 2:
        hi, lo = sc[0], sc[-1]
        if hi["hit"] <= lo["hit"]:
            add("med", "The score did not separate winners from losers",
                f"Score {hi['bucket']}: {hi['hit']}% hit over {hi['priced']}; score {lo['bucket']}: {lo['hit']}% over {lo['priced']}. "
                "Ranking by score is not adding information yet.",
                "Weight the score toward whichever dimensions above do separate them (setup type, stop distance, fill day).")
    return F


def review(days: int = 60, with_candles: bool = True) -> Dict:
    raw = _closed_rows(days)
    rows = [_enrich(r, with_candles) for r in raw]
    rows.sort(key=lambda x: (x["r"] if x["r"] is not None else 0))
    stops = [x for x in rows if x["exit_kind"] == "stop"]
    t = _stats(rows)
    by = {
        "exit_kind": _bucket(rows, lambda x: x["exit_kind"] or "n/a", ["target", "stop", "time"]),
        "type": _bucket(rows, lambda x: x["type"]),
        "conviction": _bucket(rows, lambda x: x["conviction"] or "n/a", ["high", "medium", "low"]),
        "quality": _bucket(rows, lambda x: x["quality"] or "n/a", ["high", "medium", "low"]),
        "score": _bucket(rows, lambda x: _score_bucket(x["score"]), ["90+", "80-89", "70-79", "60-69", "<60"]),
        "stop_atr": _bucket(rows, lambda x: _stop_atr_bucket(x["stop_atr"]), ["<0.75 ATR", "0.75-1.5 ATR", "1.5-2.5 ATR", ">2.5 ATR"]),
        "fill_lag": _bucket(rows, lambda x: _fill_lag_bucket(x["fill_lag"]), ["day 1", "day 2", "day 3+"]),
        "entry_type": _bucket(rows, lambda x: x["entry_type"] or "n/a", ["LIMIT", "STOP"]),
        "zone_kind": _bucket(rows, lambda x: x["zone_kind"] or "none"),
        "sector": _bucket(rows, lambda x: x["sector"] or "n/a"),
        "stage": _bucket(rows, lambda x: x["stage"] or "n/a"),
    }
    # loss clustering by exit date
    cl = defaultdict(list)
    for x in stops:
        cl[(x["exit_date"] or "")[:10]].append(x["tsym"])
    clusters = sorted(({"date": d, "stops": len(n), "names": n} for d, n in cl.items() if d),
                      key=lambda c: -c["stops"])
    # how many were open at once (the rulebook says 5)
    opens = Counter()
    for x in rows:
        if x["fill_date"] and x["exit_date"]:
            d = _dt.date.fromisoformat(x["fill_date"][:10]); end = _dt.date.fromisoformat(x["exit_date"][:10])
            while d <= end:
                opens[d.isoformat()] += 1
                d = cal.next_trading_day(d)
    peak = max(opens.values()) if opens else 0
    return {"ok": True, "at": M._now_iso(), "days": days, "with_candles": with_candles,
            "totals": t, "by": by, "clusters": clusters[:6], "peak_concurrent": peak,
            "stopped": stops, "trades": rows,
            "findings": _findings(t, rows, stops, by, clusters),
            "min_n": MIN_N, "cost_pct": SWING_ROUND_TRIP_COST_PCT}


# ── text report ──────────────────────────────────────────────────────────────

def _f(v, d=2, suf=""):
    return "—" if v is None else (f"{v:+.{d}f}{suf}" if isinstance(v, float) and suf == "R" else f"{v}{suf}")


def format_text(d: Dict) -> str:
    t = d["totals"]; L = []
    L.append(f"SWING POST-MORTEM · last {d['days']} days · {t['n']} finished trades · {d['at'][:16]}")
    L.append("=" * 78)
    L.append(f"wins {t['wins']} · losses {t['losses']} · hit {t['hit']}% · break-even hit {t['breakeven_hit']}%")
    L.append(f"avg win {t['avg_win_r']}R · avg loss {t['avg_loss_r']}R · payoff {t['payoff']} · "
             f"expectancy {t['exp_r']}R/trade · total {t['sum_r']}R · ₹{t['rupees']}")
    L.append(f"peak positions open at once: {d['peak_concurrent']} (rulebook: 5)")
    L.append("")
    L.append("FINDINGS")
    L.append("-" * 78)
    for f in d["findings"]:
        L.append(f"[{f['severity'].upper():4}] {f['title']}")
        L.append(f"       {f['detail']}")
        if f.get("fix"):
            L.append(f"       → {f['fix']}")
    L.append("")
    L.append("STOPPED OUT — worst first")
    L.append("-" * 78)
    L.append(f"{'stock':10} {'score':>5} {'conv':6} {'setup':6} {'qual':6} {'stop%':>6} {'stopATR':>7} {'fill':>5} {'exit':10} {'R':>6} {'gap%':>5} {'back?':6}")
    for x in d["stopped"]:
        back = ("T1" if x["recovered_t1"] else "entry" if x["recovered_entry"] else "no") if x["recovered_entry"] is not None else "n/a"
        L.append(f"{x['tsym']:10} {str(x['score'] or ''):>5} {str(x['conviction'] or '')[:6]:6} {x['type']:6} {str(x['quality'] or '')[:6]:6} "
                 f"{_f(x['stop_pct']):>6} {_f(x['stop_atr']):>7} {('d'+str(x['fill_lag'])) if x['fill_lag'] is not None else '':>5} "
                 f"{(x['exit_date'] or '')[:10]:10} {_f(x['r'],2,'R'):>6} {_f(x['gap_through_pct']):>5} {back:6}"
                 + ("  zone-broke-same-day" if x["broke_zone"] else ""))
    L.append("")
    L.append("LOSS DAYS — stops per exit date")
    L.append("-" * 78)
    for c in d["clusters"]:
        L.append(f"{c['date']}  {c['stops']:2} stops  {', '.join(c['names'])}")
    L.append("")
    for dim, title in (("exit_kind", "HOW TRADES ENDED"), ("type", "BY SETUP TYPE"), ("conviction", "BY CONVICTION"),
                       ("quality", "BY QUALITY"), ("score", "BY SCORE"), ("stop_atr", "BY STOP DISTANCE"),
                       ("fill_lag", "BY FILL DAY"), ("entry_type", "BY ENTRY TYPE"), ("zone_kind", "BY ZONE KIND"),
                       ("sector", "BY SECTOR")):
        L.append(title)
        L.append(f"{'bucket':16} {'n':>3} {'W':>3} {'L':>3} {'hit%':>6} {'exp R':>7} {'sum R':>7} {'₹':>8}")
        for b in d["by"][dim]:
            flag = "" if b["priced"] >= d["min_n"] else "  (small)"
            L.append(f"{b['bucket'][:16]:16} {b['priced']:>3} {b['wins']:>3} {b['losses']:>3} {_f(b['hit']):>6} "
                     f"{_f(b['exp_r'],3):>7} {_f(b['sum_r']):>7} {_f(b['rupees']):>8}{flag}")
        L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    days = 60
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    print(format_text(review(days=days, with_candles="--no-candles" not in sys.argv)))
