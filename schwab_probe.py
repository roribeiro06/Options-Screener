#!/usr/bin/env python3
"""
schwab_probe.py -- after schwab_auth.py has logged you in, fetch a quote, the
list of option expirations and one option chain for a ticker, and write what
Schwab actually returns to schwab_sample_<ticker>.json (git-ignored).

Purpose: the screener needs, per contract, bid / ask / delta / IV / open
interest / volume. This prints which of those Schwab provides (and how they're
named) so the app's data layer can be written against real data, not guesses.
It prints NO tokens or secrets -- only market data, safe to share.

    .venv\\Scripts\\python.exe schwab_probe.py AAPL
"""
import datetime as dt
import json
import os
import sys

import requests

import schwab_auth

BASE = "https://api.schwabapi.com/marketdata/v1"


def get(path, params=None):
    r = requests.get(BASE + path, params=params, timeout=30,
                     headers={"Authorization": f"Bearer {schwab_auth.get_access_token()}",
                              "Accept": "application/json"})
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:500])


def main(symbol):
    out, today = {}, dt.date.today()

    st, quote = get("/quotes", {"symbols": symbol})
    print(f"quotes           -> HTTP {st}")
    out["quote"] = quote

    st, exps = get("/expirationchain", {"symbol": symbol})
    print(f"expirationchain  -> HTTP {st}")
    out["expirationchain"] = exps

    st, chain = get("/chains", {"symbol": symbol, "contractType": "ALL", "includeUnderlyingQuote": "true",
                                "fromDate": today.isoformat(), "toDate": (today + dt.timedelta(days=21)).isoformat()})
    print(f"chains           -> HTTP {st}")
    out["chains"] = chain

    # what does one contract carry? (the fields the screener depends on)
    try:
        side = chain["putExpDateMap"]
        exp_key = sorted(side)[0]
        strike_key = sorted(side[exp_key], key=float)[len(side[exp_key]) // 2]
        contract = side[exp_key][strike_key][0]
        print(f"\nsample put contract {exp_key} strike {strike_key}:")
        for k in ("bid", "ask", "delta", "volatility", "openInterest", "totalVolume", "daysToExpiration",
                  "strikePrice", "putCall", "closePrice", "quoteTimeInLong"):
            print(f"  {k:18s} {contract.get(k, '<MISSING>')}")
        print("  all keys:", sorted(contract))
    except Exception as e:
        print("couldn't pull a sample contract out of the chain response:", e)

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"schwab_sample_{symbol}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"\nfull responses saved to {os.path.basename(path)} (git-ignored)")


if __name__ == "__main__":
    main((sys.argv[1] if len(sys.argv) > 1 else "AAPL").upper())
