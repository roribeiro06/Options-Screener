#!/usr/bin/env python3
"""
schwab_auth.py -- one-time (then roughly weekly) Schwab login for the Market Data API.

Schwab doesn't hand out a single fixed key like Tradier: you log in once in a
browser, Schwab redirects to your callback URL with a one-time code, and this
script swaps that code for tokens (a short-lived ACCESS token, ~30 min, and a
REFRESH token, which as far as I know lasts ~7 days -- check Schwab's docs).
Tokens are saved to schwab_tokens.json (git-ignored, never commit it).

Usage (from the project folder):
    .venv\\Scripts\\python.exe schwab_auth.py          # first login / after the refresh token expires
    .venv\\Scripts\\python.exe schwab_auth.py refresh  # just renew the access token

Your Client ID / Client Secret are read from the environment variables
SCHWAB_CLIENT_ID / SCHWAB_CLIENT_SECRET, or typed in when prompted (secret hidden) --
never hardcoded here; the secret is never printed (the Client ID, being public, is echoed so you can check it).

Other modules: `get_access_token()` returns a valid access token, refreshing it
automatically when it's about to expire.
"""
import base64
import getpass
import json
import os
import sys
import time
import urllib.parse
import webbrowser

import requests

CALLBACK_URL = "https://127.0.0.1:8182"      # must match the app's Callback URL on the Schwab developer portal EXACTLY
AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schwab_tokens.json")
REFRESH_MARGIN_S = 120                       # renew the access token this many seconds before it expires


def _credentials():
    # The Client ID is the PUBLIC half of the key (it's in the login URL anyway), so
    # it's typed visibly and echoed back -- a hidden prompt made a bad paste
    # impossible to spot ("invalid_client"). Only the secret is hidden.
    cid = (os.environ.get("SCHWAB_CLIENT_ID") or input("Schwab Client ID: ")).strip()
    if cid:
        print(f"  using Client ID {cid[:4]}...{cid[-4:]}  ({len(cid)} characters)")
    secret = (os.environ.get("SCHWAB_CLIENT_SECRET") or getpass.getpass("Schwab Client Secret (hidden): ")).strip()
    if not (cid and secret):
        raise SystemExit("Client ID and Client Secret are both required.")
    return cid, secret


def _basic_auth(cid, secret):
    return "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()


def login_url(client_id):
    return f"{AUTH_URL}?client_id={urllib.parse.quote(client_id)}&redirect_uri={urllib.parse.quote(CALLBACK_URL, safe='')}"


def extract_code(pasted):
    """The authorization code from the URL Schwab redirected the browser to
    (https://127.0.0.1:8182/?code=...&session=...). Accepts the full URL or just
    the code; URL-decodes it (the code usually ends in %40, which must become @)."""
    pasted = pasted.strip()
    if "code=" in pasted:
        qs = urllib.parse.urlparse(pasted).query or pasted.split("?", 1)[-1]
        code = urllib.parse.parse_qs(qs).get("code", [""])[0]
    else:
        code = pasted
    return urllib.parse.unquote(code)


def _token_request(cid, secret, data):
    r = requests.post(TOKEN_URL, data=data, timeout=30,
                      headers={"Authorization": _basic_auth(cid, secret),
                               "Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code != 200:
        raise RuntimeError(f"token request failed: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def _save(tok, prev=None):
    now = int(time.time())
    out = {"access_token": tok["access_token"],
           "refresh_token": tok.get("refresh_token") or (prev or {}).get("refresh_token"),
           "access_expires_at": now + int(tok.get("expires_in", 1800)),
           # the refresh token's own age is what matters weekly: only reset it on a fresh login
           "refresh_issued_at": (now if tok.get("refresh_token") and not prev else (prev or {}).get("refresh_issued_at", now))}
    with open(TOKEN_FILE, "w") as f:
        json.dump(out, f)
    return out


def login():
    cid, secret = _credentials()
    url = login_url(cid)
    print("\n1) Your browser will open Schwab's login page. Log in and approve the app.")
    print("2) You'll then land on an error-looking page (can't connect to 127.0.0.1) -- that's expected.")
    print("3) Copy the FULL address from the browser's address bar and paste it below.\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("If the browser didn't open, paste this into it yourself:\n" + url + "\n")
    code = extract_code(input("Pasted address: "))
    tok = _token_request(cid, secret, {"grant_type": "authorization_code", "code": code,
                                       "redirect_uri": CALLBACK_URL})
    _save(tok)
    print("Logged in. Tokens saved to schwab_tokens.json (git-ignored).")


def refresh():
    cid, secret = _credentials()
    prev = load_tokens()
    if not prev or not prev.get("refresh_token"):
        raise SystemExit("No saved refresh token -- run schwab_auth.py (no argument) to log in first.")
    tok = _token_request(cid, secret, {"grant_type": "refresh_token", "refresh_token": prev["refresh_token"]})
    _save(tok, prev)
    print("Access token refreshed.")


def load_tokens():
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def get_access_token():
    """A valid access token, refreshed automatically if it's within REFRESH_MARGIN_S
    of expiring. Raises if there are no saved tokens or the refresh token no longer
    works (then run schwab_auth.py again to log in)."""
    tok = load_tokens()
    if not tok:
        raise RuntimeError("no Schwab tokens -- run schwab_auth.py to log in")
    if tok["access_expires_at"] - time.time() > REFRESH_MARGIN_S:
        return tok["access_token"]
    cid = os.environ.get("SCHWAB_CLIENT_ID")
    secret = os.environ.get("SCHWAB_CLIENT_SECRET")
    if not (cid and secret):
        raise RuntimeError("SCHWAB_CLIENT_ID / SCHWAB_CLIENT_SECRET not set; can't refresh the access token")
    new = _token_request(cid, secret, {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    return _save(new, tok)["access_token"]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        refresh()
    else:
        login()
