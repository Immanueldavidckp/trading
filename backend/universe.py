"""
universe.py — pick the day's tradeable universe: NSE EQ stocks priced <= Rs.300,
ranked by TODAY's traded value (turnover) — "today's volume leaders" (the user's
choice). Primary source is the NSE EOD bhavcopy (one download for the whole
market, compendium §28); a curated liquid-seed list fetched via Upstox is the
fallback when the bhavcopy can't be reached.

Every row also carries its NSE macro `sector` (see sectors.py), which is what
the plan pages group the 200-name universe by.

Compendium §27 hygiene applied where we can: EQ series only, price floor Rs.30
(below that tick-size % cost + operator risk), price cap Rs.300 (user), and a
liquidity sort. ASM/GSM exclusion is a TODO (needs the daily surveillance lists).
"""
from __future__ import annotations
from typing import List, Dict, Optional
import io
import zipfile
import datetime as _dt

import requests

import sectors as _sectors

MAX_PRICE = 300.0
MIN_PRICE = 30.0
TOP_N = 200

# ETFs / mutual-fund units / debt instruments trade on the EQ series but are NOT
# stocks — exclude them (compendium §27: the universe is cash equities only).
_ETF_SET = {"LIQUIDCASE", "LIQUIDADD", "LIQUIDBEES", "CASH", "MONExT"}
def _is_etf(sym: str) -> bool:
    s = (sym or "").upper()
    return (s.endswith("BEES") or s.endswith("ETF") or s.endswith("IETF")
            or s.endswith("GSEC") or s.endswith("SDL") or s.endswith("LIQUID")
            or s.endswith("MOM50") or s in _ETF_SET)

# Liquid sub-Rs.300 NSE names — the fallback universe when bhavcopy is unreachable.
# The price cap is still enforced on live data, so entries that drift above 300
# simply drop out. Not exhaustive; the bhavcopy path covers the whole market.
SEED = [
    # ── financials
    "IDEA", "YESBANK", "IDFCFIRSTB", "PNB", "IOB", "BANKBARODA", "UNIONBANK", "CANBK",
    "BANDHANBNK", "FEDERALBNK", "IDBI", "UCOBANK", "CENTRALBK", "MAHABANK", "PSB",
    "J&KBANK", "SOUTHBANK", "DCBBANK", "EQUITASBNK", "UJJIVANSFB", "SURYODAY", "FINOPB",
    "PFC", "RECLTD", "HUDCO", "IRFC", "IFCI", "MANAPPURAM", "PAYTM", "EDELWEISS",
    "PAISALO", "CGCL", "SPANDANA", "SATIN", "UGROCAP", "POONAWALLA", "NIACL", "GICRE",
    "IIFL", "GEOJITFSL", "5PAISA", "JIOFIN", "TFCILTD",
    # ── power / energy
    "NHPC", "SJVN", "TATAPOWER", "JPPOWER", "RPOWER", "IREDA", "NLCINDIA", "SUZLON",
    "INOXWIND", "ORIENTGREEN", "PTC", "GIPCL", "RTNPOWER", "GAIL", "ONGC", "IOC",
    "BPCL", "HINDPETRO", "OIL", "COALINDIA", "MRPL", "CHENNPETRO", "ATGL", "DEEPINDS",
    # ── metals & mining
    "SAIL", "TATASTEEL", "NMDC", "VEDL", "NATIONALUM", "HINDCOPPER", "MOIL", "GMDCLTD",
    "KIOCL", "SHYAMMETL", "JTLIND", "SURYAROSNI", "GPIL", "WELCORP",
    # ── capital goods / infra / defence
    "BHEL", "RVNL", "NBCC", "IRB", "IRCON", "ITDCEM", "NCC", "ASHOKA", "GPTINFRA",
    "RPPINFRA", "PATELENG", "TITAGARH", "TEXRAIL", "JWL", "GENUSPOWER", "SALASAR",
    "APOLLO", "JINDWORLD", "OLECTRA", "TRIL", "ELECON", "KIRLOSBROS", "HBLENGINE",
    "ITI", "HFCL", "STLTECH", "RAILTEL", "MTNL", "GTLINFRA", "TEJASNET",
    # ── autos & components
    "ASHOKLEY", "MOTHERSON", "EXIDEIND", "OLAELEC", "TATAMOTORS", "TMPV", "MSUMI",
    "JAMNAAUTO", "GABRIEL", "SUBROS", "LUMAXTECH", "FIEMIND", "SETCO", "GREAVESCOT",
    "ATULAUTO", "RACLGEAR", "MINDACORP",
    # ── healthcare / pharma
    "MOREPENLAB", "ASTERDM", "GRANULES", "LAURUSLABS", "MARKSANS", "SEQUENT",
    "AARTIDRUGS", "INDOCO", "BLISSGVS", "PANACEABIO", "KOPRAN", "LINCOLN", "SMSPHARMA",
    "ORCHPHARMA", "SHILPAMED", "WOCKPHARMA",
    # ── chemicals / fertilisers
    "GSFC", "CHAMBLFERT", "GNFC", "RCF", "NFL", "MADRASFERT", "PARADEEP", "DCW",
    "NOCIL", "IGPL", "TANFACIND", "BHAGCHEM", "GHCL", "TATACHEM",
    # ── FMCG / consumer
    "ITC", "DABUR", "PATANJALI", "KRBL", "LTFOODS", "HERITGFOOD", "PARAGMILK",
    "AVANTIFEED", "VENKEYS", "BAJAJCON", "ZYDUSWELL",
    # ── consumer services / retail / hospitality
    "ZOMATO", "MEESHO", "ABFRL", "SHOPERSTOP", "VMART", "LEMONTREE", "MAHINDHOL",
    "THOMASCOOK", "SPECIALITY", "BARBEQUE", "RESTAURANT", "DELHIVERY", "REDINGTON",
    # ── textiles
    "TRIDENT", "WELSPUNLIV", "CUPID", "GOKEX", "NITINSPIN", "SUTLEJTEX", "FILATEX",
    "SPTL", "INDOCOUNT", "SIYSIL", "RSWM", "ARVIND", "RAYMOND",
    # ── it / media / telecom
    "WIPRO", "MOSCHIP", "SAGILITY", "63MOONS", "RSYSTEMS", "NUCLEUS", "SASKEN",
    "ZENSARTECH", "FIRSTSOURCE", "TANLA", "TV18BRDCST", "ZEEL", "NETWORK18", "DISHTV",
    "SAREGAMA", "HTMEDIA", "DBCORP", "JAGRAN", "NDTV", "BALAJITELE", "UFO",
    "INDUSTOWER", "ONMOBILE",
    # ── realty / cement / building materials
    "ANANTRAJ", "HUBTOWN", "ARVSMART", "KOLTEPATIL", "ASHIANA", "INDIACEM",
    "KESORAMIND", "STARCEMENT", "SAGCEM", "PRISMJOHNS", "ORIENTCEM", "HINDWAREAP",
    "SOMANYCERA", "GREENPLY", "CENTURYPLY",
    # ── logistics / misc
    "GMRAIRPORT", "GATI", "TCI", "VRLLOG", "SNOWMAN", "SHREYAS", "ESSARSHPNG",
    "SCI", "MMTC", "STCINDIA", "JKPAPER", "WSTCSTPAPR", "ANDHRAPAP", "ORIENTPPR",
    "KUANTUM", "SATIA", "SAFARI", "KHADIM", "BALMLAWRIE",
]


def _bhavcopy_url(d: _dt.date) -> str:
    return (f"https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")


def fetch_bhavcopy(d: _dt.date, timeout: int = 25) -> Optional[List[Dict]]:
    """Whole-market EOD OHLCV+turnover for one date, or None on failure."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        s.get("https://www.nseindia.com", timeout=timeout)   # prime cookies
        r = s.get(_bhavcopy_url(d), timeout=timeout)
        if r.status_code != 200 or not r.content:
            return None
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        name = zf.namelist()[0]
        text = zf.read(name).decode("utf-8", "replace")
    except Exception:
        return None

    lines = text.splitlines()
    if not lines:
        return None
    hdr = [h.strip() for h in lines[0].split(",")]
    idx = {h: i for i, h in enumerate(hdr)}

    def col(*names):
        for n in names:
            if n in idx:
                return idx[n]
        return None

    c_sym = col("TckrSymb", "SYMBOL"); c_ser = col("SctySrs", "SERIES")
    c_cls = col("ClsPric", "CLOSE"); c_vol = col("TtlTradgVol", "TOTTRDQTY")
    c_val = col("TtlTrfVal", "TOTTRDVAL"); c_tp = col("FinInstrmTp")
    if None in (c_sym, c_ser, c_cls, c_val):
        return None

    out = []
    for ln in lines[1:]:
        p = ln.split(",")
        if len(p) <= max(c_sym, c_ser, c_cls, c_val):
            continue
        series = p[c_ser].strip().upper()
        if series != "EQ":
            continue
        if c_tp is not None and c_tp < len(p) and p[c_tp].strip().upper() not in ("STK", ""):
            continue
        try:
            close = float(p[c_cls]); turnover = float(p[c_val])
            vol = float(p[c_vol]) if (c_vol is not None and p[c_vol]) else 0.0
        except Exception:
            continue
        out.append({"sym": p[c_sym].strip().upper(), "close": close,
                    "volume": vol, "turnover": turnover})
    return out or None


def _fallback_from_upstox(max_price: float) -> List[Dict]:
    """Rank the SEED list by latest daily-candle turnover via Upstox. Fetches the
    daily candle when nothing is stored, so it works even from an empty DB. Used
    only when the bhavcopy is unreachable."""
    from upstox_client import UpstoxClient as _UC
    try:
        from main import _upstox
    except Exception:
        _upstox = None
    rows = []
    for sym in SEED:
        try:
            data = _UC.query(sym, "1d", limit=2)
            if not data and _upstox:
                _upstox().fetch_candles(tsym=sym, interval="1d")
                data = _UC.query(sym, "1d", limit=2)
            if not data:
                continue
            last = data[-1]
            close = last["c"]; vol = last.get("v") or 0
            if close and MIN_PRICE <= close <= max_price:
                rows.append({"sym": sym, "close": close, "volume": vol,
                             "turnover": close * vol})
        except Exception:
            continue
    rows.sort(key=lambda r: r["turnover"], reverse=True)
    return rows


def select_universe(d: Optional[_dt.date] = None, max_price: float = MAX_PRICE,
                    top_n: int = TOP_N) -> Dict:
    """Return {source, date, sectors, rows:[{sym,close,volume,turnover,rank,sector}]}
    — the top_n <= max_price names by today's turnover, each tagged with its NSE
    macro sector."""
    d = d or _dt.date.today()
    source = "bhavcopy"
    rows = fetch_bhavcopy(d)
    if not rows:
        # try the previous session's bhavcopy (weekend/holiday/late publish)
        rows = fetch_bhavcopy(d - _dt.timedelta(days=1))
    if not rows:
        source = "upstox_seed_fallback"
        rows = _fallback_from_upstox(max_price)
    else:
        rows = [r for r in rows if MIN_PRICE <= r["close"] <= max_price]
        rows.sort(key=lambda r: r["turnover"], reverse=True)

    rows = [r for r in rows if not _is_etf(r["sym"])]   # drop ETFs/debt units
    top = rows[:top_n]
    for i, r in enumerate(top):
        r["rank"] = i + 1
        r["turnover_cr"] = round(r["turnover"] / 1e7, 2)
    _sectors.attach(top)                                # stamp {sector} on each row

    by_sector: Dict[str, int] = {}
    for r in top:
        by_sector[r["sector"]] = by_sector.get(r["sector"], 0) + 1
    return {"source": source, "date": d.isoformat(), "count": len(top),
            "sectors": dict(sorted(by_sector.items(), key=lambda kv: -kv[1])),
            "rows": top}
