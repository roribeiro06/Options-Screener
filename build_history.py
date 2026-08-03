#!/usr/bin/env python3
"""
build_history.py -- precompute a lookup of TYPICAL put premiums per ticker,
bucketed by OTM% and DTE, from ~1 year of Tradier historical STOCK prices.

We only have stock-price history (free via Tradier), so we estimate premiums from
REALIZED volatility. Realized vol runs a bit below the implied vol that actually
prices options, so a single number would understate the true premium. Instead we
report a RANGE:
    low  = 25th-percentile modeled premium over the year (realized-vol basis)
    high = 75th-percentile modeled premium x IV_UPLIFT (nudged toward implied vol)

Output: history_premiums.json (committed to the repo; the screener reads it).
Run weekly via GitHub Actions. Needs TRADIER_TOKEN in the environment.
"""
import os
import sys
import json
import math
import datetime as dt
from statistics import NormalDist

import wheel_screener as ws

LOOKBACK_DAYS = 365
RV_WINDOW     = 30            # rolling trading days for realized volatility
IV_UPLIFT     = 1.25         # lift realized-vol premium toward implied-vol level (~25% VRP)
OTM_BUCKETS   = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
DTE_BUCKETS   = [7, 14, 21, 30, 45, 60, 90]
OUT           = "history_premiums.json"

_N = NormalDist()


def bs_put(S, K, T, sigma, r):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _N.cdf(-d2) - S * _N.cdf(-d1)


def daily_closes(symbol):
    end = dt.date.today()
    start = end - dt.timedelta(days=LOOKBACK_DAYS + 15)
    j = ws._td_get("/markets/history", {"symbol": symbol, "interval": "daily",
                                        "start": start.isoformat(), "end": end.isoformat()})
    days = (j.get("history") or {}).get("day") or []
    if isinstance(days, dict):
        days = [days]
    return [float(d["close"]) for d in days if d.get("close")]


def rolling_rv(closes, window):
    """Annualized realized vol series; out[k] aligns to closes[window + k]."""
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    out = []
    for i in range(window, len(rets) + 1):
        w = rets[i - window:i]
        m = sum(w) / len(w)
        var = sum((x - m) ** 2 for x in w) / (len(w) - 1)
        out.append(math.sqrt(var) * math.sqrt(252))
    return out


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def build_ticker(symbol):
    closes = daily_closes(symbol)
    if len(closes) < RV_WINDOW + 20:
        return None
    rv = rolling_rv(closes, RV_WINDOW)
    prices = closes[RV_WINDOW:RV_WINDOW + len(rv)]
    table = {}
    for otm in OTM_BUCKETS:
        for dte in DTE_BUCKETS:
            T = dte / 365.0
            prems = sorted(bs_put(S, S * (1 - otm), T, sigma, ws.RISK_FREE)
                           for S, sigma in zip(prices, rv))
            low = percentile(prems, 0.25)
            high = percentile(prems, 0.75) * IV_UPLIFT
            table[f"{int(otm * 100)}|{dte}"] = [round(low, 2), round(high, 2)]
    return table


def main():
    if not os.environ.get("TRADIER_TOKEN"):
        print("TRADIER_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    tickers = list(dict.fromkeys(list(ws.PUT_TICKERS) + list(ws.HOLDINGS.keys())))
    out = {"_meta": {"built": dt.date.today().isoformat(),
                     "otm_buckets": [int(o * 100) for o in OTM_BUCKETS],
                     "dte_buckets": DTE_BUCKETS,
                     "iv_uplift": IV_UPLIFT,
                     "lookback_days": LOOKBACK_DAYS},
           "tickers": {}}
    for t in tickers:
        try:
            tbl = build_ticker(t)
            if tbl:
                out["tickers"][t] = tbl
                print(f"{t}: ok")
            else:
                print(f"{t}: insufficient history")
        except Exception as e:
            print(f"{t}: ERROR {e}", file=sys.stderr)
    with open(OUT, "w") as f:
        json.dump(out, f)
    print(f"wrote {OUT} with {len(out['tickers'])} tickers")


if __name__ == "__main__":
    main()
