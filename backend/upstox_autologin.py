"""
Automated daily Upstox login via TOTP — no manual OTP.

Upstox access tokens expire every day (~3:30 AM IST) and there is no refresh
token (SEBI rule). This uses the maintained `upstox-totp` package to perform
the full login flow (mobile + password + PIN + TOTP) and return a fresh token,
so the dashboard can stay live without a manual login each morning.

Upstox login is passwordless — mobile + TOTP + PIN (the upstox-totp flow only
calls otp/generate -> otp-totp/verify (TOTP) -> 2fa (PIN); the library's
`password` field is never sent, so we just default it to the PIN).

Credentials are read from env (set in backend/.env on the server, gitignored):
    UPSTOX_USERNAME      10-digit mobile number
    UPSTOX_PIN_CODE      6-digit PIN
    UPSTOX_TOTP_SECRET   TOTP secret seed (base32)
Client id/secret/redirect are reused from the existing UPSTOX_API_KEY /
UPSTOX_API_SECRET / UPSTOX_REDIRECT_URI.
"""

import os

# Only these are actually needed from the user. `password` is required by the
# library's config model but never used in the TOTP flow, so we set it = PIN.
_REQUIRED = ["UPSTOX_USERNAME", "UPSTOX_PIN_CODE", "UPSTOX_TOTP_SECRET",
             "UPSTOX_CLIENT_ID", "UPSTOX_CLIENT_SECRET", "UPSTOX_REDIRECT_URI"]


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
    # password is unused by the TOTP flow but required by the lib's model — use PIN
    if not os.environ.get("UPSTOX_PASSWORD"):
        os.environ["UPSTOX_PASSWORD"] = os.environ.get("UPSTOX_PIN_CODE", "")


def get_token() -> dict:
    """Return {ok, access_token, user} or {ok:False, error}.

    We drive the library's login flow up to the OAuth `code`, then do the final
    token exchange ourselves. The library's high-level get_access_token() parses
    the response into a model that REQUIRES `poa`/`is_active`, which Upstox
    sometimes omits — that would raise even though the token was obtained.
    """
    _map_env()
    missing = [k for k in _REQUIRED if not os.environ.get(k)]
    if missing:
        return {"ok": False, "error": "missing env vars: " + ", ".join(missing)}
    try:
        from upstox_totp import UpstoxTOTP
        from urllib.parse import urlparse, parse_qs
        import requests
    except Exception as e:
        return {"ok": False, "error": f"upstox-totp not installed ({e})"}
    try:
        upx = UpstoxTOTP()
        # full login: generate_otp -> validate_otp(TOTP) -> submit_pin(PIN) -> authorize
        oauth = upx.app_token.oauth_authorization()
        redirect_uri = getattr(getattr(oauth, "data", None), "redirectUri", None)
        if not redirect_uri:
            return {"ok": False, "error": "login ok but no redirect URI: " + str(oauth)[:200]}
        codes = parse_qs(urlparse(redirect_uri).query).get("code")
        if not codes:
            return {"ok": False, "error": "no auth code in redirect"}
        r = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            data={
                "code": codes[0],
                "client_id": os.environ["UPSTOX_CLIENT_ID"],
                "client_secret": os.environ["UPSTOX_CLIENT_SECRET"],
                "redirect_uri": os.environ["UPSTOX_REDIRECT_URI"],
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        d = r.json()
        token = d.get("access_token")
        if token:
            return {"ok": True, "access_token": token,
                    "user": d.get("user_name") or d.get("user_id", "")}
        return {"ok": False, "error": str(d)[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
