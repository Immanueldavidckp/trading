"""
build_jobs.py — run one long plan build per kind in a background thread.

Building plans for 200 stocks fetches candles for 200 symbols; done inline in
the request it blocks the event loop for minutes and the browser (or nginx)
gives up with a 504 long before it finishes. So the UI starts the build here
and polls `status()` for live progress instead.

One job per kind ('day' / 'swing') at a time — a second start while one is
running returns the running job rather than doubling the Upstox load.
"""
from __future__ import annotations
from typing import Callable, Dict, Optional
import threading
import datetime as _dt
import traceback

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

_LOCK = threading.Lock()
_JOBS: Dict[str, Dict] = {}     # kind -> job state


def _now() -> str:
    return _dt.datetime.now(IST).isoformat(timespec="seconds")


def _blank(kind: str) -> Dict:
    return {"kind": kind, "state": "idle", "started_at": None, "finished_at": None,
            "done": 0, "total": 0, "current": None, "result": None, "error": None}


def status(kind: str) -> Dict:
    with _LOCK:
        job = dict(_JOBS.get(kind) or _blank(kind))
    job["running"] = job["state"] == "running"
    tot, done = job.get("total") or 0, job.get("done") or 0
    job["percent"] = round(done / tot * 100, 1) if tot else (100.0 if job["state"] == "done" else 0.0)
    return job


def _progress(kind: str) -> Callable[[int, int, Optional[str]], None]:
    def cb(done: int, total: int, current: Optional[str] = None) -> None:
        with _LOCK:
            job = _JOBS.get(kind)
            if job is not None:
                job["done"], job["total"], job["current"] = done, total, current
    return cb


def start(kind: str, fn: Callable, **kwargs) -> Dict:
    """Start `fn(progress=..., **kwargs)` in a thread unless one is already
    running for this kind. Returns the job status either way."""
    with _LOCK:
        cur = _JOBS.get(kind)
        if cur and cur.get("state") == "running":
            return {**status(kind), "already_running": True}
        _JOBS[kind] = {**_blank(kind), "state": "running", "started_at": _now()}

    def _run():
        try:
            res = fn(progress=_progress(kind), **kwargs)
            with _LOCK:
                _JOBS[kind].update(state="done", finished_at=_now(), result=res,
                                   current=None)
        except Exception as e:                                  # noqa: BLE001
            traceback.print_exc()
            with _LOCK:
                _JOBS[kind].update(state="error", finished_at=_now(),
                                   error=str(e)[:400], current=None)

    threading.Thread(target=_run, name=f"build-{kind}", daemon=True).start()
    return {**status(kind), "already_running": False}
