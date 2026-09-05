"""
sectors.py — map an NSE trading symbol to its macro-economic sector, so the
plan pages can group a 200-name universe into sectors instead of one endless
ranked list.

Two sources, in order:

1. **NSE index constituent CSVs** (live). Every `ind_*_list.csv` NSE publishes
   carries an ``Industry`` column with NSE's own macro-sector classification.
   Pulling the broad-market lists (Total Market 750 + Microcap 250 + 500)
   covers ~1000 symbols — comfortably more than the sub-Rs.300 universe. The
   merged map is cached to ``local_data/sector_map.json`` and refreshed weekly.
2. **Bundled static map** (offline fallback). Hand-maintained for the liquid
   names that show up in the universe most often, so grouping still works on a
   box that cannot reach NSE.

Anything neither source knows comes back as ``Unclassified`` — never an error;
sector grouping is a presentation feature and must not be able to fail a build.
"""
from __future__ import annotations
from typing import Dict, List, Optional
import json
import os
import datetime as _dt

import requests

_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "local_data", "sector_map.json")
_CACHE_TTL_DAYS = 7
UNCLASSIFIED = "Unclassified"

# NSE broad-market constituent lists — each row is
# "Company Name,Industry,Symbol,Series,ISIN Code". Ordered widest-first; later
# files only fill in symbols the earlier ones did not carry.
_INDEX_FILES = [
    "ind_niftytotalmarket_list.csv",
    "ind_niftymicrocap250_list.csv",
    "ind_nifty500list.csv",
    "ind_niftysmallcap250list.csv",
    "ind_niftymidcap150list.csv",
]
_BASE = "https://nsearchives.nseindia.com/content/indices/"

# NSE spells a few sectors differently across files — fold them together so the
# UI does not show "Oil Gas & Consumable Fuels" and "Oil, Gas & Consumable
# Fuels" as two separate groups.
_CANON = {
    "oil gas & consumable fuels": "Oil, Gas & Consumable Fuels",
    "oil, gas & consumable fuels": "Oil, Gas & Consumable Fuels",
    "fast moving consumer goods": "Fast Moving Consumer Goods",
    "fmcg": "Fast Moving Consumer Goods",
    "information technology": "Information Technology",
    "it": "Information Technology",
    "financial services": "Financial Services",
    "metals & mining": "Metals & Mining",
    "metals and mining": "Metals & Mining",
    "automobile and auto components": "Automobile & Auto Components",
    "automobile & auto components": "Automobile & Auto Components",
    "healthcare": "Healthcare",
    "capital goods": "Capital Goods",
    "consumer durables": "Consumer Durables",
    "consumer services": "Consumer Services",
    "construction materials": "Construction Materials",
    "construction": "Construction",
    "chemicals": "Chemicals",
    "power": "Power",
    "realty": "Realty",
    "services": "Services",
    "telecommunication": "Telecommunication",
    "media entertainment & publication": "Media, Entertainment & Publication",
    "media, entertainment & publication": "Media, Entertainment & Publication",
    "textiles": "Textiles",
    "diversified": "Diversified",
    "forest materials": "Forest Materials",
}


def canon_sector(name: str) -> str:
    n = (name or "").strip()
    if not n:
        return UNCLASSIFIED
    return _CANON.get(n.lower(), n)


# ── bundled fallback: liquid NSE names by macro sector ──────────────────────
# Not exhaustive (the live NSE lists are), but enough that grouping stays
# meaningful with no network. Keep symbols upper-case and NSE-canonical.
_STATIC: Dict[str, List[str]] = {
    "Financial Services": [
        "YESBANK", "IDFCFIRSTB", "PNB", "IOB", "BANKBARODA", "UNIONBANK", "CANBK",
        "BANDHANBNK", "FEDERALBNK", "IDBI", "UCOBANK", "CENTRALBK", "MAHABANK",
        "PFC", "RECLTD", "HUDCO", "IRFC", "IFCI", "SOUTHBANK", "MANAPPURAM",
        "PAYTM", "SBIN", "AXISBANK", "ICICIBANK", "HDFCBANK", "KOTAKBANK",
        "INDUSINDBK", "PSB", "J&KBANK", "KARURVYSYA", "CSBBANK", "DCBBANK",
        "EQUITASBNK", "UJJIVANSFB", "SURYODAY", "FINOPB", "CAPITALSFB",
        "SHRIRAMFIN", "BAJFINANCE", "BAJAJFINSV", "CHOLAFIN", "MUTHOOTFIN",
        "LICHSGFIN", "PNBHOUSING", "REPCOHOME", "IIFL", "MOTILALOFS", "ANGELONE",
        "EDELWEISS", "PAISALO", "CGCL", "SPANDANA", "CREDITACC", "ARMANFIN",
        "SATIN", "MASFIN", "UGROCAP", "POONAWALLA", "ABCAPITAL", "LICI",
        "SBILIFE", "HDFCLIFE", "ICICIPRULI", "ICICIGI", "GICRE", "NIACL",
        "STARHEALTH", "BSE", "MCX", "CDSL", "KFINTECH", "CAMS", "IEX",
        "JIOFIN", "IRB", "SBICARD", "TFCILTD", "GEOJITFSL", "5PAISA",
    ],
    "Information Technology": [
        "WIPRO", "TCS", "INFY", "HCLTECH", "TECHM", "LTIM", "MPHASIS", "COFORGE",
        "PERSISTENT", "OFSS", "SONATSOFTW", "MASTEK", "ZENSARTECH", "CYIENT",
        "BIRLASOFT", "TANLA", "ROUTE", "HAPPSTMNDS", "NEWGEN", "INTELLECT",
        "RATEGAIN", "KPITTECH", "TATAELXSI", "ECLERX", "FIRSTSOURCE", "TATATECH",
        "MOSCHIP", "SAGILITY", "63MOONS", "RSYSTEMS", "NUCLEUS", "SASKEN",
    ],
    "Oil, Gas & Consumable Fuels": [
        "ONGC", "IOC", "BPCL", "HINDPETRO", "GAIL", "OIL", "COALINDIA", "PETRONET",
        "IGL", "MGL", "GUJGASLTD", "GSPL", "AEGISLOG", "CASTROLIND", "CHENNPETRO",
        "MRPL", "RELIANCE", "ATGL", "GULFOILLUB", "DEEPINDS",
    ],
    "Metals & Mining": [
        "SAIL", "TATASTEEL", "NMDC", "VEDL", "NATIONALUM", "HINDCOPPER", "JSWSTEEL",
        "HINDALCO", "JINDALSTEL", "JSL", "APLAPOLLO", "WELCORP", "RATNAMANI",
        "MOIL", "GRAVITA", "SHYAMMETL", "KIOCL", "GMDCLTD", "HINDZINC", "SANDUMA",
        "JTLIND", "SURYAROSNI", "MAITHANALL", "GPIL", "TATASTLLP",
    ],
    "Power": [
        "NHPC", "SJVN", "TATAPOWER", "JPPOWER", "RPOWER", "NTPC", "POWERGRID",
        "TORNTPOWER", "CESC", "ADANIPOWER", "ADANIGREEN", "JSWENERGY", "IREDA",
        "NLCINDIA", "INOXWIND", "SUZLON", "ORIENTGREEN", "KPIGREEN", "WAAREEENER",
        "PTC", "GIPCL", "RTNPOWER", "INDRENEW",
    ],
    "Capital Goods": [
        "BHEL", "OLECTRA", "TRIVENI", "THERMAX", "CUMMINSIND", "ABB", "SIEMENS",
        "BEL", "HAL", "BDL", "MAZDOCK", "GRSE", "COCHINSHIP", "AIAENG", "SKFINDIA",
        "TIMKEN", "GRINDWELL", "CARBORUNIV", "ELECON", "KIRLOSENG", "KIRLOSBROS",
        "PRAJIND", "TDPOWERSYS", "HBLENGINE", "JYOTICNC", "AZAD", "SANSERA",
        "HONAUT", "GMMPFAUDLR", "ISGEC", "TITAGARH", "TEXRAIL", "RVNL",
        "PATELENG", "JWL", "GENUSPOWER", "SALASAR", "APOLLO", "JINDWORLD",
        "SHAKTIPUMP", "KSB", "JASH", "PGEL", "VGUARD", "CGPOWER", "TRIL",
    ],
    "Automobile & Auto Components": [
        "TATAMOTORS", "ASHOKLEY", "MOTHERSON", "EXIDEIND", "OLAELEC", "TMPV",
        "M&M", "MARUTI", "BAJAJ-AUTO", "HEROMOTOCO", "TVSMOTOR", "EICHERMOT",
        "BOSCHLTD", "BHARATFORG", "SUNDRMFAST", "ENDURANCE", "MINDACORP",
        "UNOMINDA", "SUBROS", "JAMNAAUTO", "GABRIEL", "LUMAXTECH", "FIEMIND",
        "AMARAJABAT", "ARE&M", "SETCO", "GREAVESCOT", "FORCEMOT", "ESCORTS",
        "SMLISUZU", "ATULAUTO", "RACLGEAR", "WHEELS", "MSUMI", "TIINDIA",
    ],
    "Healthcare": [
        "MOREPENLAB", "ASTERDM", "SUNPHARMA", "CIPLA", "DRREDDY", "LUPIN",
        "AUROPHARMA", "ZYDUSLIFE", "TORNTPHARM", "ALKEM", "GLENMARK", "IPCALAB",
        "NATCOPHARM", "AJANTPHARM", "GRANULES", "LAURUSLABS", "STAR", "WOCKPHARMA",
        "CAPLIPOINT", "SUVENPHAR", "SEQUENT", "MARKSANS", "SHILPAMED", "JBCHEPHARM",
        "APOLLOHOSP", "FORTIS", "MAXHEALTH", "NH", "KIMS", "RAINBOW", "METROPOLIS",
        "DRLALPATHLABS", "THYROCARE", "POLYMED", "INDOCO", "BLISSGVS", "PANACEABIO",
        "AARTIDRUGS", "ORCHPHARMA", "KOPRAN", "LINCOLN", "SMSPHARMA",
    ],
    "Fast Moving Consumer Goods": [
        "ITC", "DABUR", "HINDUNILVR", "BRITANNIA", "NESTLEIND", "MARICO",
        "GODREJCP", "COLPAL", "EMAMILTD", "TATACONSUM", "VBL", "RADICO",
        "UBL", "MCDOWELL-N", "JUBLFOOD", "ZYDUSWELL", "BAJAJCON", "GILLETTE",
        "PATANJALI", "HATSUN", "DODLA", "PARAGMILK", "AVANTIFEED", "KRBL",
        "LTFOODS", "GODREJAGRO", "VENKEYS", "TASTYBITE", "HERITGFOOD",
    ],
    "Chemicals": [
        "GSFC", "CHAMBLFERT", "TRIDENT", "PIDILITIND", "SRF", "AARTIIND",
        "DEEPAKNTR", "NAVINFLUOR", "ATUL", "VINATIORGA", "FINEORG", "ALKYLAMINE",
        "BALAMINES", "GNFC", "RCF", "NFL", "COROMANDEL", "PIIND", "UPL",
        "SUMICHEM", "BASF", "TATACHEM", "GHCL", "DCW", "NOCIL", "IGPL",
        "JUBLINGREA", "CLEAN", "ROSSARI", "NEOGEN", "GALAXYSURF", "TANFACIND",
        "BHAGCHEM", "PARADEEP", "MADRASFERT", "KHAITANCHM", "ORIENTCEM",
    ],
    "Construction": [
        "NBCC", "IRB", "GMRAIRPORT", "LT", "NCC", "PNCINFRA", "KNRCON",
        "HGINFRA", "IRCON", "RITES", "ENGINERSIN", "ITDCEM", "ASHOKA",
        "GPTINFRA", "JKIL", "CAPACITE", "AHLUCONT", "GRINFRA", "WELSPUNIND",
        "DBL", "RPPINFRA", "UDS", "CEIGALL", "MANINFRA",
    ],
    "Construction Materials": [
        "ULTRACEMCO", "AMBUJACEM", "ACC", "SHREECEM", "DALBHARAT", "JKCEMENT",
        "RAMCOCEM", "JKLAKSHMI", "HEIDELBERG", "BIRLACORPN", "INDIACEM",
        "STARCEMENT", "SAGCEM", "PRISMJOHNS", "NUVOCO", "KESORAMIND",
    ],
    "Realty": [
        "DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "BRIGADE", "PHOENIXLTD",
        "SOBHA", "MAHLIFE", "SUNTECK", "ANANTRAJ", "KOLTEPATIL", "ASHIANA",
        "PURVA", "HUBTOWN", "ARVSMART", "NBCC",
    ],
    "Telecommunication": [
        "IDEA", "INDUSTOWER", "BHARTIARTL", "TATACOMM", "HFCL", "ITI",
        "TEJASNET", "STLTECH", "RAILTEL", "ONMOBILE", "GTLINFRA", "MTNL",
    ],
    "Media, Entertainment & Publication": [
        "TV18BRDCST", "ZEEL", "SUNTV", "PVRINOX", "SAREGAMA", "NAZARA",
        "NETWORK18", "DISHTV", "TIPSMUSIC", "HTMEDIA", "DBCORP", "JAGRAN",
        "NDTV", "BALAJITELE", "UFO",
    ],
    "Textiles": [
        "WELSPUNLIV", "TRIDENT", "VARDHMAN", "KPRMILL", "ARVIND", "RAYMOND",
        "GOKEX", "NITINSPIN", "SUTLEJTEX", "FILATEX", "SPAL", "GARFIBRES",
        "INDOCOUNT", "SIYSIL", "RSWM", "AMBIKCO", "SPTL", "CUPID",
    ],
    "Consumer Durables": [
        "HAVELLS", "VOLTAS", "CROMPTON", "BAJAJELEC", "WHIRLPOOL", "BLUESTARCO",
        "DIXON", "AMBER", "TITAN", "KAJARIACER", "SOMANYCERA", "CERA",
        "HINDWAREAP", "ORIENTELEC", "SYMPHONY", "TTKPRESTIG", "BUTTERFLY",
        "STOVEKRAFT", "LAOPALA", "GREENPLY", "CENTURYPLY", "RAJESHEXPO",
        "SAFARI", "VIPIND", "RELAXO", "BATAINDIA", "CAMPUS", "KHADIM",
    ],
    "Consumer Services": [
        "ZOMATO", "ETERNAL", "SWIGGY", "NYKAA", "MEESHO", "TRENT", "DMART",
        "ABFRL", "SHOPERSTOP", "VMART", "INDHOTEL", "EIHOTEL", "LEMONTREE",
        "CHALET", "MAHINDHOL", "IRCTC", "THOMASCOOK", "WONDERLA", "SPECIALITY",
        "DEVYANI", "SAPPHIRE", "WESTLIFE", "BARBEQUE", "RESTAURANT",
    ],
    "Services": [
        "CONCOR", "GATI", "TCI", "BLUEDART", "VRLLOG", "MAHLOG", "ALLCARGO",
        "SCI", "GPPL", "JSWINFRA", "ADANIPORTS", "TEAMLEASE", "QUESS",
        "SIS", "TRANSINDIA", "SNOWMAN", "ESSARSHPNG", "SHREYAS", "DELHIVERY",
        "REDINGTON", "MMTC", "STCINDIA",
    ],
    "Diversified": [
        "ADANIENT", "GRASIM", "3MINDIA", "DCMSHRIRAM", "BALMLAWRIE", "GODREJIND",
    ],
    "Forest Materials": [
        "JKPAPER", "WSTCSTPAPR", "SESHAPAPER", "TNPL", "ANDHRAPAP", "ORIENTPPR",
        "KUANTUM", "SATIA",
    ],
}

# flatten once at import: SYMBOL -> sector
STATIC_MAP: Dict[str, str] = {}
for _sec, _syms in _STATIC.items():
    for _s in _syms:
        STATIC_MAP.setdefault(_s.upper(), _sec)


# ── live NSE map, cached on disk ────────────────────────────────────────────

def _read_cache() -> Optional[Dict]:
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            j = json.load(f)
        if isinstance(j, dict) and isinstance(j.get("map"), dict):
            return j
    except Exception:
        pass
    return None


def _write_cache(mapping: Dict[str, str]) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"fetched": _dt.date.today().isoformat(),
                       "count": len(mapping), "map": mapping}, f)
    except Exception:
        pass


def _cache_is_fresh(j: Optional[Dict]) -> bool:
    if not j:
        return False
    try:
        age = (_dt.date.today() - _dt.date.fromisoformat(j.get("fetched", ""))).days
    except Exception:
        return False
    return 0 <= age < _CACHE_TTL_DAYS


def fetch_nse_sector_map(timeout: int = 25) -> Dict[str, str]:
    """Download NSE's broad-market constituent lists and merge their Industry
    column into {SYMBOL: sector}. Returns {} if NSE can't be reached."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/csv,text/html,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        s.get("https://www.nseindia.com", timeout=timeout)   # prime cookies
    except Exception:
        pass

    out: Dict[str, str] = {}
    for fname in _INDEX_FILES:
        try:
            r = s.get(_BASE + fname, timeout=timeout)
            if r.status_code != 200 or not r.text:
                continue
            lines = r.text.splitlines()
            if len(lines) < 2:
                continue
            hdr = [h.strip().strip('"').lower() for h in lines[0].split(",")]
            try:
                i_sym = hdr.index("symbol")
                i_ind = hdr.index("industry")
            except ValueError:
                continue
            for ln in lines[1:]:
                p = [c.strip().strip('"') for c in ln.split(",")]
                if len(p) <= max(i_sym, i_ind):
                    continue
                sym = p[i_sym].upper()
                sec = canon_sector(p[i_ind])
                if sym and sec != UNCLASSIFIED:
                    out.setdefault(sym, sec)
        except Exception:
            continue
    return out


def refresh(force: bool = False) -> Dict:
    """Refresh the on-disk sector cache from NSE. Safe to call any time."""
    cached = _read_cache()
    if not force and _cache_is_fresh(cached):
        return {"ok": True, "source": "cache", "fetched": cached.get("fetched"),
                "count": len(cached.get("map") or {})}
    live = fetch_nse_sector_map()
    if live:
        _write_cache(live)
        return {"ok": True, "source": "nse", "fetched": _dt.date.today().isoformat(),
                "count": len(live)}
    return {"ok": False, "source": "static_only", "error": "NSE index lists unreachable",
            "count": len(cached.get("map") or {}) if cached else 0}


_MEM: Optional[Dict[str, str]] = None


def load_map(auto_refresh: bool = True) -> Dict[str, str]:
    """{SYMBOL: sector} — live NSE map layered over the bundled static map."""
    global _MEM
    if _MEM is not None:
        return _MEM
    cached = _read_cache()
    if auto_refresh and not _cache_is_fresh(cached):
        live = fetch_nse_sector_map()
        if live:
            _write_cache(live)
            cached = {"map": live}
    merged = dict(STATIC_MAP)
    merged.update((k.upper(), v) for k, v in (cached or {}).get("map", {}).items())
    _MEM = merged
    return merged


def invalidate() -> None:
    """Drop the in-process map so the next lookup re-reads disk/NSE."""
    global _MEM
    _MEM = None


def sector_for(sym: str, auto_refresh: bool = True) -> str:
    return load_map(auto_refresh).get((sym or "").upper(), UNCLASSIFIED)


def attach(rows: List[Dict], key: str = "sym", field: str = "sector") -> List[Dict]:
    """Stamp a `sector` onto each row in place. Never raises."""
    try:
        m = load_map()
    except Exception:
        m = STATIC_MAP
    for r in rows:
        try:
            r[field] = m.get(str(r.get(key, "")).upper(), UNCLASSIFIED)
        except Exception:
            r[field] = UNCLASSIFIED
    return rows


def summary(symbols: List[str]) -> Dict:
    """{sector: [symbols]} for an arbitrary symbol list — used by the API."""
    m = load_map()
    out: Dict[str, List[str]] = {}
    for s in symbols:
        out.setdefault(m.get((s or "").upper(), UNCLASSIFIED), []).append(s)
    return {k: sorted(v) for k, v in sorted(out.items(), key=lambda kv: -len(kv[1]))}
