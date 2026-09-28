"""
Keep the tick tables from filling the disk.

`market_depth` and `price_changes` gain a row every time a watched stock's
price or quotes move — for ~60 symbols, all session, with the full order book
on each depth row. Nothing ever removed them, and on a 58 GB box they filled
the disk, which stalls MySQL and with it every page that reads the database.

Nothing in the app reads far back:
  * the day-plan scorer's order-flow gate reads a few minutes around one entry
    bar of the session being scored, and treats missing data as "allowed";
  * the chart backlog, order-flow view and tick analysis read the current
    session or the last few hundred rows.
So a few days is plenty. Default 5 days; TICK_RETENTION_DAYS overrides, 0 = off.

prune(days)
    Deletes rows older than `days` in primary-key ranges, a few thousand at a
    time, so the live feed's inserts are never blocked for long. InnoDB reuses
    the freed pages: the files stop growing. It does NOT hand space back to the
    operating system — see reclaim_sql().

reclaim_sql(days)
    The statements that do give the space back on MySQL: copy the rows being
    kept into a fresh table, swap it in, drop the old one. Printed, not run —
    they are meant for `sudo mysql`, which can switch off binary logging for
    the copy so the copy itself does not fill the disk again.

CLI
    python3 db_retention.py --report              sizes, age of the data, and the reclaim SQL
    python3 db_retention.py --days 5              prune now
    python3 db_retention.py --days 5 --dry-run    what a prune would delete
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import threading
import time
from typing import Dict, List, Optional

import db as _db

TABLES = ("market_depth", "price_changes")
DEFAULT_DAYS = 5
CHUNK = 5000                  # rows per DELETE — short locks against the live feed
PAUSE_S = 0.05                # breathe between chunks
EVERY_S = 6 * 3600            # background prune cadence
FIRST_DELAY_S = 300           # let the app finish booting first


def retention_days() -> int:
    try:
        return max(0, int(os.environ.get("TICK_RETENTION_DAYS", DEFAULT_DAYS)))
    except ValueError:
        return DEFAULT_DAYS


def _cutoff_iso(days: int, now: Optional[_dt.datetime] = None) -> str:
    """received_at is stored as UTC-naive ISO text, so the cutoff is too."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return (now - _dt.timedelta(days=days)).replace(tzinfo=None).isoformat(timespec="milliseconds")


def _exists(cur, table: str) -> bool:
    try:
        cur.execute(f"SELECT 1 FROM {table} LIMIT 1")
        cur.fetchall()
        return True
    except Exception:
        return False


def _minmax(cur, table: str):
    cur.execute(f"SELECT MIN(id), MAX(id) FROM {table}")     # O(1) on the primary key
    return cur.fetchone()


def _first_at_or_after(cur, table: str, i: int):
    PH = _db.PLACE
    cur.execute(f"SELECT id, received_at FROM {table} WHERE id >= {PH} ORDER BY id LIMIT 1", [i])
    return cur.fetchone()


def keep_from_id(cur, table: str, cutoff: str) -> Optional[int]:
    """The first id whose received_at is at or after `cutoff`, found by binary
    search on the primary key (~40 indexed lookups on a billion-row table),
    because received_at alone is not indexed. Ids have gaps; the search steps
    to the first real row at or after each probe. Returns max_id + 1 when every
    row is older than the cutoff, and None for an empty table."""
    mn, mx = _minmax(cur, table)
    if mn is None:
        return None
    lo, hi, ans = int(mn), int(mx), int(mx) + 1
    while lo <= hi:
        mid = (lo + hi) // 2
        r = _first_at_or_after(cur, table, mid)
        if r is None:
            hi = mid - 1
            continue
        fid, ts = int(r[0]), str(r[1] or "")
        if ts >= cutoff:
            ans = fid
            hi = mid - 1
        else:
            lo = fid + 1
    return ans


def prune(days: Optional[int] = None, dry_run: bool = False, tables=TABLES,
          now: Optional[_dt.datetime] = None) -> Dict:
    days = retention_days() if days is None else days
    if days <= 0:
        return {"ok": True, "skipped": "retention disabled (TICK_RETENTION_DAYS=0)"}
    cutoff = _cutoff_iso(days, now)
    out = {"ok": True, "days": days, "cutoff_utc": cutoff, "tables": {}}
    for t in tables:
        conn = _db.connect()
        try:
            cur = conn.cursor()
            if not _exists(cur, t):
                out["tables"][t] = {"skipped": "no such table"}
                continue
            mn, mx = _minmax(cur, t)
            if mn is None:
                out["tables"][t] = {"deleted": 0, "note": "empty"}
                continue
            keep = keep_from_id(cur, t, cutoff)
            span = max(0, keep - int(mn))
            info = {"min_id": int(mn), "max_id": int(mx), "keep_from_id": keep, "old_id_span": span}
            if dry_run or span == 0:
                info["deleted"] = 0
                out["tables"][t] = info
                continue
            PH = _db.PLACE
            deleted, lo = 0, int(mn)
            while lo < keep:
                hi = min(lo + CHUNK, keep)
                cur.execute(f"DELETE FROM {t} WHERE id >= {PH} AND id < {PH}", [lo, hi])
                deleted += max(0, cur.rowcount or 0)
                conn.commit()
                lo = hi
                time.sleep(PAUSE_S)
            info["deleted"] = deleted
            out["tables"][t] = info
            cur.close()
        except Exception as e:
            out["ok"] = False
            out["tables"][t] = {"error": str(e)}
        finally:
            conn.close()
    return out


def report(days: Optional[int] = None) -> Dict:
    """What is in the tick tables, how old it is, and how big (MySQL)."""
    days = retention_days() if days is None else days
    cutoff = _cutoff_iso(max(days, 1))
    out = {"days": days, "cutoff_utc": cutoff, "tables": {}}
    conn = _db.connect()
    try:
        cur = conn.cursor()
        for t in TABLES:
            if not _exists(cur, t):
                continue
            mn, mx = _minmax(cur, t)
            row = {"min_id": mn, "max_id": mx}
            if mn is not None:
                row["oldest_utc"] = str(_first_at_or_after(cur, t, int(mn))[1])
                cur.execute(f"SELECT received_at FROM {t} WHERE id = {_db.PLACE}", [int(mx)])
                got = cur.fetchone()
                row["newest_utc"] = str(got[0]) if got else None
                row["keep_from_id"] = keep_from_id(cur, t, cutoff)
                span = int(mx) - int(mn) + 1
                row["share_older_than_cutoff"] = round(max(0, row["keep_from_id"] - int(mn)) / span, 3) if span else 0
            if _db.USE_MYSQL:
                cur.execute("SELECT data_length + index_length, table_rows FROM information_schema.tables "
                            "WHERE table_schema = DATABASE() AND table_name = %s", [t])
                got = cur.fetchone()
                if got:
                    row["bytes"] = int(got[0] or 0)
                    row["rows_estimate"] = int(got[1] or 0)
            out["tables"][t] = row
        cur.close()
    finally:
        conn.close()
    return out


def reclaim_sql(rep: Dict) -> List[str]:
    """Statements that return the old rows' space to the disk on MySQL.

    Copying only the rows being kept into a fresh table and dropping the old
    one frees the space at once; a DELETE never shrinks an InnoDB file. The
    session switch-off of binary logging stops the copy from writing all the
    kept rows into MySQL's change history as well — it needs root, hence
    `sudo mysql`. A few seconds of ticks that arrive during the swap can be
    lost; nothing else is."""
    if not _db.USE_MYSQL:
        return ["-- SQLite: run  VACUUM;  after a prune to shrink the file."]
    db_name = _db_name()
    L = [f"USE `{db_name}`;", "SET SESSION sql_log_bin = 0;"]
    for t, r in rep.get("tables", {}).items():
        k = r.get("keep_from_id")
        if k is None:
            continue
        L += [f"-- {t}: keep ids >= {k} (the last {rep.get('days')} days)",
              f"DROP TABLE IF EXISTS {t}__new;",
              f"CREATE TABLE {t}__new LIKE {t};",
              f"INSERT INTO {t}__new SELECT * FROM {t} WHERE id >= {k};",
              f"INSERT INTO {t}__new SELECT * FROM {t} WHERE id > (SELECT COALESCE(MAX(id), 0) FROM {t}__new);",
              f"RENAME TABLE {t} TO {t}__old, {t}__new TO {t};",
              f"DROP TABLE {t}__old;"]
    return L


def _db_name() -> str:
    url = getattr(_db, "DATABASE_URL", "") or ""
    tail = url.rsplit("/", 1)[-1] if "/" in url else ""
    return tail.split("?", 1)[0] or "trading"


# ── background ────────────────────────────────────────────────────────────────

_thread: Optional[threading.Thread] = None
_last: Dict = {}


def _loop():
    global _last
    time.sleep(FIRST_DELAY_S)
    while True:
        try:
            _last = {"at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), **prune()}
            done = {t: v.get("deleted") for t, v in _last.get("tables", {}).items()}
            print(f"[retention] pruned tick tables older than {_last.get('days')}d: {done}")
        except Exception as e:
            print(f"[retention] prune failed: {e}")
        time.sleep(EVERY_S)


def start() -> Dict:
    global _thread
    if retention_days() <= 0:
        return {"ok": True, "running": False, "why": "TICK_RETENTION_DAYS=0"}
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, name="tick-retention", daemon=True)
        _thread.start()
    return {"ok": True, "running": True, "days": retention_days(), "every_hours": EVERY_S // 3600}


def status() -> Dict:
    return {"days": retention_days(), "running": bool(_thread and _thread.is_alive()), "last": _last}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _gb(n) -> str:
    return "—" if n is None else f"{n / 1024**3:.2f} GB"


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(here), ".env"))
        load_dotenv(os.path.join(here, ".env"), override=True)
        import importlib
        importlib.reload(_db)
    except Exception:
        pass
    args = sys.argv[1:]
    days = int(args[args.index("--days") + 1]) if "--days" in args else retention_days()
    if "--report" in args:
        rep = report(days)
        print(f"tick tables · keeping the last {days} days (cutoff {rep['cutoff_utc']} UTC)")
        for t, r in rep["tables"].items():
            print(f"  {t:14} {_gb(r.get('bytes')):>10}  ~{r.get('rows_estimate', '?')} rows  "
                  f"{r.get('oldest_utc', '—')[:10]} → {str(r.get('newest_utc') or '—')[:10]}  "
                  f"older than cutoff: {round(100 * r.get('share_older_than_cutoff', 0))}%")
        print("\nTo hand the old rows' space back to the disk, run these as root:\n  sudo mysql <<'SQL'")
        for line in reclaim_sql(rep):
            print("  " + line)
        print("  SQL")
    else:
        r = prune(days, dry_run="--dry-run" in args)
        for t, v in r.get("tables", {}).items():
            print(f"{t:14} {v}")
        if not r.get("ok"):
            sys.exit(1)
