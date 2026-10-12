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
import datetime as dt
import getpass
import http.server
import ipaddress
import json
import os
import ssl
import sys
import tempfile
import threading
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
        # split by hand, not parse_qs: parse_qs would turn a literal "+" in the code into a space
        code = next((part[5:] for part in qs.split("&") if part.startswith("code=")), "")
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


def _self_signed_cert(directory):
    """A throwaway self-signed certificate for 127.0.0.1 (needs the `cryptography`
    package: .venv\\Scripts\\python.exe -m pip install cryptography). Written to
    `directory`; returns (certfile, keyfile)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                                                        x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    certfile, keyfile = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
    with open(certfile, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(keyfile, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    return certfile, keyfile


def capture_code(timeout=240, port=8182, started=None):
    """Listen on https://127.0.0.1:<port> and return the authorization code the
    moment Schwab redirects the browser there -- so the code is swapped for
    tokens instantly instead of waiting on a human to copy/paste it (Schwab's
    codes expire within seconds). The certificate is self-signed, so the browser
    shows a one-time "not private" warning: Advanced -> Proceed to 127.0.0.1.
    Returns the decoded code, or None if it can't listen (port busy, missing
    `cryptography`) or nothing arrives within `timeout` seconds -- the caller
    then falls back to copy/paste. `started` (an Event) is set once listening."""
    box, got = {}, threading.Event()
    try:
        tmp = tempfile.TemporaryDirectory()
        certfile, keyfile = _self_signed_cert(tmp.name)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
    except ImportError:
        print("  (can't auto-capture: run  .venv\\Scripts\\python.exe -m pip install cryptography  first)")
        return None

    class Handler(http.server.BaseHTTPRequestHandler):
        def setup(self):                                   # TLS handshake per connection, in its own thread
            self.request = ctx.wrap_socket(self.request, server_side=True)
            super().setup()

        def do_GET(self):
            q = urllib.parse.urlparse(self.path).query
            code = extract_code("?" + q) if "code=" in q else ""
            body = (b"<html><body style='font-family:sans-serif'><h3>Login captured - you can close this tab "
                    b"and go back to PowerShell.</h3></body></html>" if code else b"<html><body>waiting...</body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            if code:
                box["code"] = code
                got.set()

        def log_message(self, *a):
            pass

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):   # browsers abort the first, untrusted handshake -- ignore
            pass

    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"  (can't auto-capture: port {port} unavailable: {e})")
        tmp.cleanup()
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if started is not None:
        started.set()
    try:
        got.wait(timeout)
    finally:
        server.shutdown()
        server.server_close()
        tmp.cleanup()
    return box.get("code")


def login():
    cid, secret = _credentials()
    url = login_url(cid)
    print("\n1) Your browser will open Schwab's login page. Log in and approve the app.")
    print("2) You'll then land on a 'connection is not private' warning for 127.0.0.1. That's your own")
    print("   computer catching the login: click Advanced -> Proceed to 127.0.0.1 (unsafe).")
    print("3) The page then says 'Login captured' and this window finishes by itself.\n")
    print("If the browser didn't open, paste this into it yourself:\n" + url + "\n")
    started = threading.Event()
    result = {}
    t = threading.Thread(target=lambda: result.update(code=capture_code(started=started)), daemon=True)
    t.start()
    started.wait(10)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("Waiting for the login (up to 4 minutes)...")
    t.join()
    code = result.get("code")
    if code:
        print("Got the login code; exchanging it for tokens...")
    else:
        print("\nCouldn't capture the login automatically. Fallback: log in, then copy the FULL address from the")
        print("browser's address bar and paste it here -- be QUICK, Schwab's code expires within seconds.\n")
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
