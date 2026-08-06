#!/usr/bin/env python3
"""
build_volume_leaders.py -- find S&P 500 tickers OUTSIDE your manual watchlist
(PUT_TICKERS in wheel_screener.py) whose options are trading heavy volume right
now, then screen them for cash-secured puts AND covered calls (evaluated
hypothetically, same as the app's Contract Lookup does for any ticker -- "as if
you held the shares") using the same criteria as the main screener. Lets a name
like PG surface on its own if one of its contracts is both liquid and qualifies
-- you don't have to add it to the watchlist first.

Tradier has no "most active options" endpoint, so this works in two cheap stages:
  1. One batched quote call per ~100 tickers across the S&P 500 (sp500_tickers.py)
     ranks candidates by today's STOCK volume (a fast, free proxy for options interest).
  2. The top CANDIDATE_POOL of those get one real options-chain lookup (nearest
     expiration in the DTE window) to sum actual OPTION contract volume, which
     ranks the final TOP_N tickers to deep-scan with screen_puts().

Output: volume_leaders.json (committed; the Streamlit app reads it -- no live
Tradier calls needed on page load). Run daily via GitHub Actions, ideally after
the close so volume is representative of the full session. Needs TRADIER_TOKEN.
"""
import os
import sys
import json
import datetime as dt

import wheel_screener as ws
from sp500_tickers import SP500_TICKERS

CANDIDATE_POOL = 40   # how many top-stock-volume names get a real options-chain check
TOP_N = 5             # how many of those (by actual option volume) get deep-scanned
CHUNK = 100           # tickers per batched /markets/quotes call
OUT = "volume_leaders.json"
DISCOVER_MIN_OI = 5000  # OI floor for this section only (higher than the main
                        # screener's MIN_OPEN_INTEREST) -- these are unfamiliar
                        # tickers, so lean toward their most liquid contracts.


def batched_stock_volume(tickers):
    """One /markets/quotes call per CHUNK tickers -> {symbol: volume}."""
    out = {}
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        try:
            j = ws._td_get("/markets/quotes", {"symbols": ",".join(chunk)})
        except Exception as e:
            print(f"quotes batch {i}: ERROR {e}", file=sys.stderr)
            continue
        for q in ws._as_list((j.get("quotes") or {}).get("quote")):
            sym = q.get("symbol")
            if sym:
                out[sym] = int(q.get("volume") or 0)
    return out


def option_volume(symbol, today):
    """Sum contract volume (puts+calls) across the nearest expiration inside the
    screener's DTE window. Returns 0 if the ticker has no expirations in range."""
    windowed = ws._expirations_in_window(symbol, today)
    if not windowed:
        return 0
    exp, _, _ = min(windowed, key=lambda w: w[2])   # nearest DTE
    try:
        chain = ws.td_chain(symbol, exp)
    except Exception as e:
        print(f"{symbol}: chain ERROR {e}", file=sys.stderr)
        return 0
    return sum(int(o.get("volume") or 0) for o in chain)


def main():
    if not os.environ.get("TRADIER_TOKEN"):
        print("TRADIER_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    today = dt.date.today()
    watchlist = set(ws.PUT_TICKERS)
    universe = [t for t in SP500_TICKERS if t not in watchlist]

    print(f"Stage 1: batched quotes for {len(universe)} tickers...")
    stock_vol = batched_stock_volume(universe)
    ranked = sorted(stock_vol.items(), key=lambda kv: kv[1], reverse=True)
    candidates = [sym for sym, _ in ranked[:CANDIDATE_POOL]]
    print(f"Stage 1 done: {len(candidates)} candidates by stock volume.")

    print("Stage 2: options-chain volume for candidates...")
    opt_vol = {}
    for sym in candidates:
        try:
            opt_vol[sym] = option_volume(sym, today)
        except Exception as e:
            print(f"{sym}: ERROR {e}", file=sys.stderr)
            opt_vol[sym] = 0
    leaders = sorted(opt_vol.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N]
    print("Top by option volume:", leaders)

    print("Stage 3: screening leaders for qualifying puts and calls...")
    rows = []
    leader_meta = []
    ws.MIN_OPEN_INTEREST = DISCOVER_MIN_OI
    for sym, ovol in leaders:
        leader_meta.append({"ticker": sym, "stock_volume": stock_vol.get(sym, 0),
                            "option_volume": ovol})
        try:
            put_passers, _ = ws.screen_puts(sym)
            for r in put_passers:
                r["Type"] = "Put"
            call_passers, _ = ws.screen_calls(sym, None)
            for r in call_passers:
                r["Type"] = "Call"
            rows += put_passers + call_passers
            print(f"{sym}: {len(put_passers)} puts, {len(call_passers)} calls qualifying")
        except Exception as e:
            print(f"{sym}: screen ERROR {e}", file=sys.stderr)

    out = {"_meta": {"built": today.isoformat(),
                     "candidate_pool": CANDIDATE_POOL, "top_n": TOP_N},
           "leaders": leader_meta,
           "contracts": rows}
    with open(OUT, "w") as f:
        json.dump(out, f, default=str)
    print(f"wrote {OUT}: {len(leader_meta)} leaders, {len(rows)} qualifying contracts")


if __name__ == "__main__":
    main()
