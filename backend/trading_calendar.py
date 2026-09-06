"""
trading_calendar.py — is the NSE open on a given date?

Everything downstream needed this and nobody had it. `next_trading_day()` in
plan_pipeline skipped Saturdays and Sundays and nothing else, with a comment
saying holidays were "handled by empty data" — which really meant a plan would
be built for Diwali, find no candles, and quietly produce nothing.

Three sources, best first, each cached to local_data/nse_holidays.json:

1. **NSE's holiday master API** — the authoritative trading-holiday list for the
   calendar year. Refreshed weekly.
2. **Bhavcopy probe** — if the EOD bhavcopy exists for a date, the market
   certainly traded that date. Used to confirm today and to learn holidays the
   API missed. Note the asymmetry: a bhavcopy that IS there proves the market
   was open; one that is NOT there proves nothing on its own (NSE could simply
   be unreachable, or it may not be published yet), so a missing bhavcopy is
   never by itself treated as a holiday.
3. **Weekend rule** — the floor. Always applied.

When the holiday list cannot be fetched at all, `is_trading_day` still answers
using weekends only but reports `confident=False`, so a caller can choose to
wait rather than act on a guess.
"""
from __future__ import annotations
from typing import Dict, List, Optional, Set
import datetime as _dt
import json
import os

import requests

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "local_data", "nse_holidays.json")
_TTL_DAYS = 7
_HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"


def today_ist() -> _dt.date:
    return _dt.datetime.now(IST).date()


def now_ist() -> _dt.datetime:
    return _dt.datetime.now(IST)


# ── cache ───────────────────────────────────────────────────────────────────

def _read_cache() -> Dict:
    try:
        with open(_CACHE, "r", encoding="utf-8") as f:
            j = json.load(f)
        if isinstance(j, dict):
            return j
    except Exception:
        pass
    return {}


def _write_cache(j: Dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
        with open(_CACHE, "w", encoding="utf-8") as f:
            json.dump(j, f, indent=1)
    except Exception:
        pass


def _fresh(j: Dict) -> bool:
    try:
        age = (today_ist() - _dt.date.fromisoformat(j.get("fetched", ""))).days
        return 0 <= age < _TTL_DAYS
    except Exception:
        return False


# ── NSE holiday master ──────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json,text/html,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def fetch_nse_holidays(timeout: int = 20) -> List[str]:
    """Trading holidays (ISO dates) from NSE's holiday master, or [] on failure."""
    s = _session()
    try:
        s.get("https://www.nseindia.com", timeout=timeout)          # prime cookies
        r = s.get(_HOLIDAY_URL, timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    out: Set[str] = set()
    # The payload is {segment: [{tradingDate: "26-Jan-2026", ...}, ...]}. Only the
    # cash segment ("CM") governs equity delivery; fall back to every segment if
    # the key is ever renamed.
    blocks = []
    if isinstance(data, dict):
        blocks = [data.get("CM")] if data.get("CM") else list(data.values())
    for block in blocks:
        if not isinstance(block, list):
            continue
        for row in block:
            if not isinstance(row, dict):
                continue
            raw = (row.get("tradingDate") or row.get("trading_date") or "").strip()
            for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
                try:
                    out.add(_dt.datetime.strptime(raw, fmt).date().isoformat())
                    break
                except ValueError:
                    continue
    return sorted(out)


def refresh(force: bool = False) -> Dict:
    """Refresh the holiday cache. Safe to call any time; never raises."""
    j = _read_cache()
    if not force and _fresh(j) and j.get("holidays"):
        return {"ok": True, "source": "cache", "fetched": j.get("fetched"),
                "count": len(j.get("holidays") or [])}
    hols = fetch_nse_holidays()
    if hols:
        j["holidays"] = hols
        j["fetched"] = today_ist().isoformat()
        _write_cache(j)
        return {"ok": True, "source": "nse", "fetched": j["fetched"], "count": len(hols)}
    return {"ok": False, "source": "unavailable",
            "error": "NSE holiday master unreachable",
            "count": len(j.get("holidays") or [])}


_MEM: Optional[Dict] = None


def _load(auto: bool = True) -> Dict:
    global _MEM
    if _MEM is not None:
        return _MEM
    j = _read_cache()
    if auto and not (_fresh(j) and j.get("holidays")):
        hols = fetch_nse_holidays()
        if hols:
            j["holidays"] = hols
            j["fetched"] = today_ist().isoformat()
            _write_cache(j)
    _MEM = j
    return j


def invalidate() -> None:
    global _MEM
    _MEM = None


def holidays(auto: bool = True) -> Set[str]:
    j = _load(auto)
    return set(j.get("holidays") or []) | set(j.get("observed_closed") or [])


def confirmed_open(auto: bool = True) -> Set[str]:
    """Dates a bhavcopy was actually found for — proof the market traded."""
    return set(_load(auto).get("observed_open") or [])


# ── the questions callers actually ask ──────────────────────────────────────

def is_trading_day(d: _dt.date, auto: bool = True) -> Dict:
    """{'open': bool, 'confident': bool, 'why': str}."""
    iso = d.isoformat()
    if d.weekday() >= 5:
        return {"open": False, "confident": True,
                "why": f"{d:%A} — the NSE cash market does not trade at weekends"}
    if iso in confirmed_open(auto):
        return {"open": True, "confident": True,
                "why": "bhavcopy published for this date — the market traded"}
    j = _load(auto)
    hols = holidays(auto)
    if iso in hols:
        return {"open": False, "confident": True, "why": "NSE trading holiday"}
    if not j.get("holidays"):
        # weekday, but we could not load the holiday list — say so rather than
        # pretending. Callers that must not act on a guess check `confident`.
        return {"open": True, "confident": False,
                "why": "weekday, but the NSE holiday list is unavailable — unverified"}
    return {"open": True, "confident": True, "why": "weekday, not on the NSE holiday list"}


def next_trading_day(d: _dt.date, auto: bool = True) -> _dt.date:
    """The next date the market is open AFTER d — weekends and NSE holidays skipped."""
    nd = d + _dt.timedelta(days=1)
    for _ in range(30):                       # a 30-day gap never happens; guard anyway
        if is_trading_day(nd, auto)["open"]:
            return nd
        nd += _dt.timedelta(days=1)
    return nd


def prev_trading_day(d: _dt.date, auto: bool = True) -> _dt.date:
    pd = d - _dt.timedelta(days=1)
    for _ in range(30):
        if is_trading_day(pd, auto)["open"]:
            return pd
        pd -= _dt.timedelta(days=1)
    return pd


def note_observed(d: _dt.date, traded: bool) -> None:
    """Record what a bhavcopy probe found, so the calendar learns from reality.

    Only `traded=True` is hard evidence. A miss is recorded separately and only
    treated as a closure once the date is safely in the past AND the holiday
    list also has no opinion — see `learn_closure`."""
    j = _load()
    key = "observed_open" if traded else "observed_missing"
    seen = set(j.get(key) or [])
    seen.add(d.isoformat())
    j[key] = sorted(seen)[-400:]
    _MEM_set(j)
    _write_cache(j)


def learn_closure(d: _dt.date) -> None:
    """Mark a past date as closed after repeated bhavcopy misses. Deliberately
    conservative: a transient NSE outage must never become a permanent hole in
    the calendar, so callers only invoke this for a date >=2 days old."""
    if (today_ist() - d).days < 2:
        return
    j = _load()
    closed = set(j.get("observed_closed") or [])
    closed.add(d.isoformat())
    j["observed_closed"] = sorted(closed)[-400:]
    _MEM_set(j)
    _write_cache(j)


def _MEM_set(j: Dict) -> None:
    global _MEM
    _MEM = j


def status() -> Dict:
    j = _load()
    t = today_ist()
    return {"fetched": j.get("fetched"),
            "holidays": len(j.get("holidays") or []),
            "observed_open": len(j.get("observed_open") or []),
            "observed_closed": len(j.get("observed_closed") or []),
            "today": t.isoformat(), "today_status": is_trading_day(t),
            "next_trading_day": next_trading_day(t).isoformat(),
            "upcoming_holidays": [h for h in sorted(j.get("holidays") or [])
                                  if h >= t.isoformat()][:8]}
