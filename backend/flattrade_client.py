"""
Flattrade (PiConnect / Noren) trading client.

Auth model: OAuth-style flow.
  1. Redirect user's browser to:
       https://auth.flattrade.in/?app_key={API_KEY}
  2. After login, Flattrade redirects to the registered Redirect URL with
       ?code=<request_code>&client=<USER_ID>
  3. Backend exchanges (api_key, request_code, sha256(api_key+request_code+api_secret))
     for a one-day userSession token at
       https://authapi.flattrade.in/trade/apitoken
  4. Use that token for all PiConnect API calls at
       https://piconnect.flattrade.in/PiConnectTP/<endpoint>

The token is valid for the trading day; needs daily re-auth.
"""

import os
import json
import hashlib
import threading
import time
from datetime import datetime
from typing import Optional

import requests

AUTH_URL_TMPL = "https://auth.flattrade.in/?app_key={api_key}"
TOKEN_URL = "https://authapi.flattrade.in/trade/apitoken"
API_BASE = "https://piconnect.flattrade.in/PiConnectTP"

SESSION_CACHE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "local_data", "flattrade_session.json"
)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class FlattradeClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("FLATTRADE_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("FLATTRADE_API_SECRET", "")
        self.user_id = user_id or os.environ.get("FLATTRADE_USER_ID", "")
        self.session_token: Optional[str] = None
        self.actid: Optional[str] = None
        self.uname: Optional[str] = None
        self.email: Optional[str] = None
        self.broker: Optional[str] = None
        self.logged_in_at: Optional[str] = None
        self._lock = threading.Lock()
        # Try to restore a cached session (valid for the trading day)
        self._restore_session()

    # ---------- Config / Helpers ----------
    def has_creds(self) -> bool:
        return bool(self.api_key and self.api_secret and self.user_id)

    def login_url(self) -> str:
        return AUTH_URL_TMPL.format(api_key=self.api_key)

    # ---------- Session persistence ----------
    def _save_session(self):
        os.makedirs(os.path.dirname(SESSION_CACHE), exist_ok=True)
        try:
            with open(SESSION_CACHE, "w") as f:
                json.dump(
                    {
                        "session_token": self.session_token,
                        "actid": self.actid,
                        "uname": self.uname,
                        "email": self.email,
                        "broker": self.broker,
                        "logged_in_at": self.logged_in_at,
                    },
                    f,
                )
        except Exception:
            pass

    def _restore_session(self):
        if not os.path.exists(SESSION_CACHE):
            return
        try:
            with open(SESSION_CACHE) as f:
                d = json.load(f)
            # Session valid for trading day; expire if not from today (IST roughly)
            stamp = d.get("logged_in_at")
            if stamp:
                try:
                    stamp_dt = datetime.fromisoformat(stamp)
                    if stamp_dt.date() != datetime.now().date():
                        return  # stale, force re-auth
                except Exception:
                    pass
            self.session_token = d.get("session_token")
            self.actid = d.get("actid")
            self.uname = d.get("uname")
            self.email = d.get("email")
            self.broker = d.get("broker")
            self.logged_in_at = stamp
        except Exception:
            pass

    def logout(self):
        with self._lock:
            self.session_token = None
            self.actid = None
            self.uname = None
            self.email = None
            self.broker = None
            self.logged_in_at = None
            try:
                if os.path.exists(SESSION_CACHE):
                    os.remove(SESSION_CACHE)
            except Exception:
                pass

    # ---------- OAuth: exchange request_code for userSession token ----------
    def exchange_code(self, request_code: str) -> dict:
        if not self.has_creds():
            return {"ok": False, "error": "Missing FLATTRADE_API_KEY / FLATTRADE_API_SECRET / FLATTRADE_USER_ID in .env"}
        try:
            secret_hash = _sha256(self.api_key + request_code + self.api_secret)
            payload = {
                "api_key": self.api_key,
                "request_code": request_code,
                "api_secret": secret_hash,
            }
            r = requests.post(TOKEN_URL, json=payload, timeout=15)
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            j = r.json()
            if j.get("stat") != "Ok" or not j.get("token"):
                return {"ok": False, "error": j.get("emsg", "Token exchange failed"), "raw": j}
            with self._lock:
                self.session_token = j["token"]
                self.actid = self.user_id
                self.logged_in_at = datetime.now().isoformat(timespec="seconds")
            # Pull user details so we show name/broker in UI
            details = self.user_details()
            if isinstance(details, dict) and details.get("stat") == "Ok":
                with self._lock:
                    self.uname = details.get("uname") or self.uname
                    self.email = details.get("email") or self.email
                    self.broker = details.get("brkname") or self.broker
                    self.actid = details.get("actid") or self.actid
            self._save_session()
            return {
                "ok": True,
                "actid": self.actid,
                "uname": self.uname,
                "email": self.email,
                "broker": self.broker,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- Accept an externally-obtained token ----------
    def set_token(self, token: str) -> dict:
        """
        Accept a userSession token obtained elsewhere (e.g. exchanged on a
        local machine because Flattrade blocks datacenter IPs for apitoken).
        Validates the token by calling UserDetails before accepting it.
        """
        with self._lock:
            self.session_token = token
            self.actid = self.user_id
            self.logged_in_at = datetime.now().isoformat(timespec="seconds")
        details = self.user_details()
        if isinstance(details, dict) and details.get("stat") == "Ok":
            with self._lock:
                self.uname = details.get("uname")
                self.email = details.get("email")
                self.broker = details.get("brkname")
                self.actid = details.get("actid") or self.actid
            self._save_session()
            return {"ok": True, "uname": self.uname, "actid": self.actid, "broker": self.broker}
        # Token didn't validate — roll back
        with self._lock:
            self.session_token = None
            self.logged_in_at = None
        return {"ok": False, "error": details.get("emsg", "Token validation failed"), "raw": details}

    # ---------- Low-level API call ----------
    def _call(self, endpoint: str, payload: dict):
        if not self.session_token:
            return {"stat": "Not_Ok", "emsg": "Not logged in. Visit /api/flattrade/login_url first."}
        payload.setdefault("uid", self.user_id)
        if endpoint not in ("UserDetails", "SearchScrip", "GetQuotes", "TPSeries"):
            payload.setdefault("actid", self.actid or self.user_id)
        url = f"{API_BASE}/{endpoint}"
        data = {"jData": json.dumps(payload), "jKey": self.session_token}
        try:
            r = requests.post(url, data=data, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return {"stat": "Not_Ok", "emsg": f"HTTP {r.status_code}: {r.text[:160]}"}
            try:
                return r.json()
            except Exception:
                return {"stat": "Not_Ok", "emsg": "Non-JSON response", "raw": r.text[:200]}
        except Exception as e:
            return {"stat": "Not_Ok", "emsg": str(e)}

    # ---------- Convenience wrappers ----------
    def user_details(self):
        return self._call("UserDetails", {})

    def get_limits(self):
        return self._call("Limits", {})

    def get_holdings(self):
        res = self._call("Holdings", {"prd": "C"})
        return res if isinstance(res, list) else (res or [])

    def get_positions(self):
        res = self._call("PositionBook", {})
        return res if isinstance(res, list) else []

    def get_orderbook(self):
        res = self._call("OrderBook", {})
        return res if isinstance(res, list) else []

    def get_tradebook(self):
        res = self._call("TradeBook", {})
        return res if isinstance(res, list) else []

    def get_quote(self, exch: str, token: str):
        return self._call("GetQuotes", {"exch": exch, "token": token})

    def search_scrip(self, exch: str, search_text: str):
        res = self._call("SearchScrip", {"stext": search_text, "exch": exch})
        return res

    def place_order(self, exch, tsym, qty, prc, trantype, prctyp, prd, ret="DAY"):
        """
        exch:     "NSE" | "BSE" | "NFO" | "MCX"
        tsym:     trading symbol e.g. "RELIANCE-EQ"
        qty:      integer
        prc:      float (use 0 for MKT orders)
        trantype: "B" buy | "S" sell
        prctyp:   "MKT" | "LMT" | "SL-LMT" | "SL-MKT"
        prd:      "C" CNC delivery | "I" MIS intraday | "M" NRML overnight derivative
        ret:      "DAY" | "EOS" | "IOC"
        """
        return self._call("PlaceOrder", {
            "exch": exch,
            "tsym": tsym,
            "qty": str(qty),
            "dscqty": "0",
            "prc": str(prc),
            "prd": prd,
            "trantype": trantype,
            "prctyp": prctyp,
            "ret": ret,
        })

    def cancel_order(self, orderno: str):
        return self._call("CancelOrder", {"norenordno": orderno})

    def status(self) -> dict:
        return {
            "configured": self.has_creds(),
            "logged_in": bool(self.session_token),
            "user_id": self.user_id,
            "actid": self.actid,
            "uname": self.uname,
            "email": self.email,
            "broker": self.broker,
            "logged_in_at": self.logged_in_at,
            "login_url": self.login_url() if self.has_creds() else None,
        }
