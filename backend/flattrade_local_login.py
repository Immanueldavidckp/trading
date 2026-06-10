"""
Flattrade daily login helper — run this on YOUR PC, not the server.

Why: Flattrade's token-exchange API (authapi.flattrade.in/trade/apitoken)
rejects requests from datacenter IPs with "Invalid Input : INVALID_IP".
So the exchange must happen from a regular Indian connection (your PC),
and the resulting session token is then pushed to the trading server.

Usage:
    python flattrade_local_login.py

It will:
  1. Open the Flattrade login page in your browser
  2. Ask you to paste the URL you land on after logging in
  3. Exchange the code for a session token (from your local IP)
  4. Push the token to the server (set_token endpoint)
"""

import hashlib
import json
import os
import re
import sys
import webbrowser
from urllib.parse import urlparse, parse_qs

import requests
from dotenv import load_dotenv

# ---- Config: read from backend/.env (never hardcode secrets here) ----
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
API_KEY = os.environ.get("FLATTRADE_API_KEY", "")
API_SECRET = os.environ.get("FLATTRADE_API_SECRET", "")
SERVER = os.environ.get("TRADING_SERVER", "https://trade.scratchforge.online")

if not API_KEY or not API_SECRET:
    print("✗ FLATTRADE_API_KEY / FLATTRADE_API_SECRET missing in backend/.env")
    sys.exit(1)

AUTH_URL = f"https://auth.flattrade.in/?app_key={API_KEY}"
TOKEN_URL = "https://authapi.flattrade.in/trade/apitoken"


def main():
    print("=" * 60)
    print("  Flattrade daily login")
    print("=" * 60)
    print(f"\n1. Opening login page:\n   {AUTH_URL}\n")
    webbrowser.open(AUTH_URL)

    print("2. Log in with your Flattrade ID + password + OTP.")
    print("   You will land on a page like:")
    print("   https://trade.scratchforge.online/?code=XXXX&client=FZ46051")
    print("   (the page may say 'Login failed - INVALID_IP' — that's fine,")
    print("    we only need the URL from the address bar)\n")

    url = input("3. Paste that FULL URL here and press Enter:\n> ").strip()

    # Extract code from URL (or accept a bare code)
    code = None
    if "code=" in url:
        qs = parse_qs(urlparse(url).query)
        code = (qs.get("code") or [None])[0]
    elif re.fullmatch(r"[0-9a-fA-F-]{30,40}", url):
        code = url
    if not code:
        print("✗ Could not find ?code=... in that URL.")
        sys.exit(1)

    print(f"\n   code = {code}")
    print("4. Exchanging code for session token (from YOUR IP)...")
    h = hashlib.sha256((API_KEY + code + API_SECRET).encode()).hexdigest()
    r = requests.post(
        TOKEN_URL,
        json={"api_key": API_KEY, "request_code": code, "api_secret": h},
        timeout=20,
    )
    j = r.json()
    if j.get("stat") != "Ok" or not j.get("token"):
        print(f"✗ Exchange failed: {j}")
        print("  (codes expire in ~1-2 minutes — log in again and retry quickly)")
        sys.exit(1)
    token = j["token"]
    print(f"   ✓ got token: {token[:18]}...")

    print("5. Pushing token to the trading server...")
    r2 = requests.post(
        f"{SERVER}/api/flattrade/set_token",
        json={"token": token, "api_secret": API_SECRET},
        timeout=20,
    )
    j2 = r2.json()
    if j2.get("ok"):
        print(f"\n✓ DONE — server logged in as: {j2.get('uname')} (acct {j2.get('actid')})")
        print(f"  Dashboard: {SERVER}/live.html")
    else:
        print(f"\n✗ Server rejected token: {j2}")
        sys.exit(1)


if __name__ == "__main__":
    main()
