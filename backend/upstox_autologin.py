"""
Automated daily Upstox login via TOTP — no manual OTP.

Upstox access tokens expire every day (~3:30 AM IST) and there is no refresh
token (SEBI rule). This uses the maintained `upstox-totp` package to perform
the full login flow (mobile + password + PIN + TOTP) and return a fresh token,
so the dashboard can stay live without a manual login each morning.

Credentials are read from env (set in backend/.env on the server, gitignored):
    UPSTOX_USERNAME      10-digit mobile number
    UPSTOX_PASSWORD      login password
    UPSTOX_PIN_CODE      6-digit PIN
    UPSTOX_TOTP_SECRET   TOTP secret seed (base32)
Client id/secret/redirect are reused from the existing UPSTOX_API_KEY /
UPSTOX_API_SECRET / UPSTOX_REDIRECT_URI.
"""

import os

_REQUIRED = ["UPSTOX_USERNAME", "UPSTOX_PASSWORD", "UPSTOX_PIN_CODE",
             "UPSTOX_TOTP_SECRET", "UPSTOX_CLIENT_ID", "UPSTOX_CLIENT_SECRET",
             "UPSTOX_REDIRECT_URI"]


def configured() -> bool:
    _map_env()
    return all(os.environ.get(k) for k in _REQUIRED)


def _map_env():
    # the library expects CLIENT_ID/CLIENT_SECRET; we already store these as
    # UPSTOX_API_KEY / UPSTOX_API_SECRET.
    if not os.environ.get("UPSTOX_CLIENT_ID"):
        os.environ["UPSTOX_CLIENT_ID"] = os.environ.get("UPSTOX_API_KEY", "")
    if not os.environ.get("UPSTOX_CLIENT_SECRET"):
        os.environ["UPSTOX_CLIENT_SECRET"] = os.environ.get("UPSTOX_API_SECRET", "")


def get_token() -> dict:
    """Return {ok, access_token, user} or {ok:False, error}."""
    _map_env()
    missing = [k for k in _REQUIRED if not os.environ.get(k)]
    if missing:
        return {"ok": False, "error": "missing env vars: " + ", ".join(missing)}
    try:
        from upstox_totp import UpstoxTOTP
    except Exception as e:
        return {"ok": False, "error": f"upstox-totp not installed ({e})"}
    try:
        upx = UpstoxTOTP()
        resp = upx.app_token.get_access_token()
        if getattr(resp, "success", False) and getattr(resp, "data", None):
            return {"ok": True,
                    "access_token": resp.data.access_token,
                    "user": getattr(resp.data, "user_name", "") or getattr(resp.data, "user_id", "")}
        return {"ok": False, "error": str(getattr(resp, "error", None) or "login failed")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
