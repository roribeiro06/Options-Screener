#!/usr/bin/env python3
"""
build_volume_leaders.py -- find tickers OUTSIDE your manual watchlist
(PUT_TICKERS in wheel_screener.py) carrying heavy options open interest right
now -- not limited to the S&P 500, so a large but not-yet-indexed name (a
recent IPO, for example) can still surface -- then screen them for
cash-secured puts AND covered calls (evaluated hypothetically, same as the
app's Contract Lookup does for any ticker -- "as if you held the shares")
using the same criteria as the main screener.

Tradier has no "most active options" or batched-open-interest endpoint, so
this works in two cheap stages:
  1. One batched quote call per ~100 tickers across a broad US-listed universe
     (fetch_universe() below) ranks candidates by today's STOCK volume -- a
     fast, free proxy, since OI isn't available without a per-ticker
     options-chain call.
  2. The top CANDIDATE_POOL of those get one real options-chain lookup (nearest
     expiration in the DTE window) to sum actual OPEN INTEREST, which ranks the
     final TOP_N tickers to deep-scan with screen_puts()/screen_calls().
A ticker with high total OI but modest volume today (or that trades outside the
top CANDIDATE_POOL by stock volume) can still be missed by stage 1 -- this is
the tradeoff for not running an options-chain call against every ticker.

Output: volume_leaders.json (committed; the Streamlit app reads it -- no live
Tradier calls needed on page load). Run daily via GitHub Actions, ideally after
the close so open interest reflects the full session. Needs TRADIER_TOKEN.
"""
import os
import sys
import json
import datetime as dt

import wheel_screener as ws
from sp500_tickers import SP500_TICKERS

# Mirror of Nasdaq/NYSE/etc.'s combined listed-securities directory (~7,000
# tickers) -- broader than the S&P 500 so recent IPOs / not-yet-indexed names
# (e.g. SPCX, SOFI) can surface. Community-maintained; if it's unreachable or
# looks truncated, fall back to the static S&P 500 snapshot rather than fail.
UNIVERSE_URL = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/all/all_tickers.txt"

CANDIDATE_POOL = 40   # how many top-stock-volume names get a real options-chain check
TOP_N = 5             # how many of those (by actual open interest) get deep-scanned
CHUNK = 100           # tickers per batched /markets/quotes call
OUT = "volume_leaders.json"
DISCOVER_MIN_OI = 5000  # OI floor for this section only (higher than the main
                        # screener's MIN_OPEN_INTEREST) -- these are unfamiliar
                        # tickers, so lean toward their most liquid contracts.


def fetch_universe():
    """Broad US-listed ticker universe. Falls back to the S&P 500 snapshot
    (sp500_tickers.py) if the remote list is unreachable or looks wrong."""
    import requests
    try:
        r = requests.get(UNIVERSE_URL, timeout=20)
        r.raise_for_status()
        tickers = sorted({line.strip() for line in r.text.splitlines() if line.strip()})
        if len(tickers) < 1000:
            raise ValueError(f"only {len(tickers)} tickers, looks truncated")
        print(f"Universe: {len(tickers)} tickers from {UNIVERSE_URL}")
        return tickers
    except Exception as e:
        print(f"universe fetch ERROR: {e} -- falling back to S&P 500", file=sys.stderr)
        return SP500_TICKERS


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


def option_open_interest(symbol, today):
    """Sum open interest (puts+calls) across the nearest expiration inside the
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
    return sum(int(o.get("oi") or 0) for o in chain)


def main():
    if not os.environ.get("TRADIER_TOKEN"):
        print("TRADIER_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    today = dt.date.today()
    watchlist = set(ws.PUT_TICKERS)
    universe = [t for t in fetch_universe() if t not in watchlist]

    print(f"Stage 1: batched quotes for {len(universe)} tickers...")
    stock_vol = batched_stock_volume(universe)
    ranked = sorted(stock_vol.items(), key=lambda kv: kv[1], reverse=True)
    candidates = [sym for sym, _ in ranked[:CANDIDATE_POOL]]
    print(f"Stage 1 done: {len(candidates)} candidates by stock volume.")

    print("Stage 2: options-chain open interest for candidates...")
    opt_oi = {}
    for sym in candidates:
        try:
            opt_oi[sym] = option_open_interest(sym, today)
        except Exception as e:
            print(f"{sym}: ERROR {e}", file=sys.stderr)
            opt_oi[sym] = 0
    leaders = sorted(opt_oi.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N]
    print("Top by open interest:", leaders)

    print("Stage 3: screening leaders for qualifying puts and calls...")
    rows = []
    leader_meta = []
    ws.MIN_OPEN_INTEREST = DISCOVER_MIN_OI
    for sym, ooi in leaders:
        leader_meta.append({"ticker": sym, "stock_volume": stock_vol.get(sym, 0),
                            "option_open_interest": ooi})
        try:
            put_passers, _ = ws.screen_puts(sym)
            call_passers, _ = ws.screen_calls(sym, None)
            # Only the single highest-open-interest qualifying put and call per
            # ticker, not every contract that passes -- keeps the table to one
            # put row + one call row per ticker.
            if put_passers:
                best_put = max(put_passers, key=lambda r: r.get("OpenInt") or 0)
                best_put["Type"] = "Put"
                rows.append(best_put)
            if call_passers:
                best_call = max(call_passers, key=lambda r: r.get("OpenInt") or 0)
                best_call["Type"] = "Call"
                rows.append(best_call)
            print(f"{sym}: {len(put_passers)} puts, {len(call_passers)} calls qualifying "
                  f"(showing top-OI each)")
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
