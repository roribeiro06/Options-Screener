#!/usr/bin/env python3
"""
build_history.py -- precompute a lookup of TYPICAL annualized yield per ticker,
bucketed by OTM%, DTE, and (where enough data exists) the volatility regime that
was in effect, from ~1 year of Tradier historical STOCK prices.

We only have stock-price history (free via Tradier), so we estimate yields from
REALIZED volatility. Realized vol runs a bit below the implied vol that actually
prices options, so a single number would understate the true yield. Instead we
report a RANGE:
    low  = 25th-percentile modeled annualized yield over the year (realized-vol basis)
    high = 75th-percentile modeled yield x IV_UPLIFT (nudged toward implied vol)

Yield, not dollar premium: a Black-Scholes premium as a fraction of strike (puts)
or spot (calls) -- the same "per_yld" convention evaluate_put/evaluate_call use --
depends ONLY on OTM%, time, and volatility, never on the stock's actual price
level (options pricing is scale-invariant in spot/strike). Percentile-ing yields
therefore isn't distorted by the stock having trended up or down over the lookback
year, the way percentile-ing raw dollar premiums would be -- a name that ran from
$50 to $150 over the year would otherwise mix "expensive because vol was high"
with "expensive because the price level was just higher that month". It's also
simpler: the yield ratio doesn't depend on the daily price series at all, only on
the trailing realized-vol path, so there's no need to pair prices with vols.

Vol-conditioned buckets: for each (OTM%, DTE) combo, the plain "otm|dte" band is
computed from every day's yield regardless of what volatility that day had --
mixing calm-market days with turbulent ones. We ALSO compute the same band
conditioned on the day's own realized vol falling in one of IV_BUCKETS (e.g.
"5|45|50" = 5% OTM, 45 DTE, ~50% vol regime), so a live lookup can ask for the
typical yield GIVEN the vol environment right now, not averaged across every vol
environment the ticker saw all year. This needs enough days in that vol bucket to
be a reliable percentile estimate (MIN_VOL_BUCKET_SAMPLES) -- calm ETFs may never
see a 60%+ vol day, volatile growth names may rarely see a sub-25% one, so most
tickers only get vol-conditioned data across the vol range they actually trade in.
Below that threshold, or when the live caller doesn't have an IV to condition on,
avg_premium_range() falls back to the plain "otm|dte" band -- this is purely
additive, an older file (or one that hasn't been rebuilt since this was added)
just lacks the "otm|dte|vol" keys and every lookup falls back automatically.

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
IV_UPLIFT     = 1.25         # lift realized-vol yield toward implied-vol level (~25% VRP)
OTM_BUCKETS   = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
DTE_BUCKETS   = [7, 14, 21, 30, 45, 60, 90]
IV_BUCKETS    = [0.15, 0.25, 0.35, 0.50, 0.70, 0.90, 1.20]  # realized-vol regimes
MIN_VOL_BUCKET_SAMPLES = 20   # below this many days in a vol bucket, the percentile
                              # is too noisy to trust -- fall back to the unconditional band
OUT           = "history_premiums.json"
METRIC        = "ann_yield"   # schema marker -- lets the screener detect and ignore
                              # an older $-premium-format file rather than misread it

_N = NormalDist()


def _d12_ratio(K_over_S, T, sigma, r):
    """d1, d2 for a given strike/spot RATIO -- independent of the actual price
    level, since only ln(S/K) = ln(1/K_over_S) matters, not S or K individually."""
    d1 = (math.log(1.0 / K_over_S) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return d1, d1 - sigma * math.sqrt(T)


def put_yield(otm, T, sigma, r):
    """BS put premium as a fraction of STRIKE -- matches evaluate_put's own
    per_yld = premium / strike convention, so this is directly comparable to
    the live AnnYield_% column."""
    if T <= 0 or sigma <= 0 or otm >= 1:
        return 0.0
    K_over_S = 1.0 - otm
    d1, d2 = _d12_ratio(K_over_S, T, sigma, r)
    p_over_s = K_over_S * math.exp(-r * T) * _N.cdf(-d2) - _N.cdf(-d1)
    return max(0.0, p_over_s / K_over_S)


def call_yield(otm, T, sigma, r):
    """BS call premium as a fraction of SPOT -- matches evaluate_call's own
    per_yld = premium / spot convention."""
    if T <= 0 or sigma <= 0:
        return 0.0
    K_over_S = 1.0 + otm
    d1, d2 = _d12_ratio(K_over_S, T, sigma, r)
    c_over_s = _N.cdf(d1) - K_over_S * math.exp(-r * T) * _N.cdf(d2)
    return max(0.0, c_over_s)


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


def _band(yields):
    ys = sorted(yields)
    return [round(percentile(ys, 0.25), 4), round(percentile(ys, 0.75) * IV_UPLIFT, 4)]


def build_ticker(symbol):
    closes = daily_closes(symbol)
    if len(closes) < RV_WINDOW + 20:
        return None
    rv = rolling_rv(closes, RV_WINDOW)

    # Group the days by which vol bucket their OWN realized vol falls nearest to,
    # once per ticker (not per OTM/DTE -- vol-bucket membership doesn't depend on
    # either). Buckets a calm ticker never visits (or a volatile one rarely visits
    # at the low end) simply stay empty and get skipped below.
    by_vol_bucket = {}
    for sigma in rv:
        vb = ws._nearest(sigma, IV_BUCKETS)
        by_vol_bucket.setdefault(vb, []).append(sigma)
    usable_buckets = {vb: sigs for vb, sigs in by_vol_bucket.items()
                      if len(sigs) >= MIN_VOL_BUCKET_SAMPLES}

    put_t, call_t = {}, {}
    for otm in OTM_BUCKETS:
        for dte in DTE_BUCKETS:
            T = dte / 365.0
            ann = 365.0 / dte
            key = f"{int(otm * 100)}|{dte}"
            # Yield ratio depends only on OTM%, T, and that day's vol -- not on the
            # stock's price level -- so we only need the vol series, not the prices.
            put_t[key] = _band(put_yield(otm, T, sigma, ws.RISK_FREE) * ann for sigma in rv)
            call_t[key] = _band(call_yield(otm, T, sigma, ws.RISK_FREE) * ann for sigma in rv)
            for vb, sigs in usable_buckets.items():
                vkey = f"{key}|{int(round(vb * 100))}"
                put_t[vkey] = _band(put_yield(otm, T, sigma, ws.RISK_FREE) * ann for sigma in sigs)
                call_t[vkey] = _band(call_yield(otm, T, sigma, ws.RISK_FREE) * ann for sigma in sigs)
    return {"put": put_t, "call": call_t}


def main():
    if not os.environ.get("TRADIER_TOKEN"):
        print("TRADIER_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    tickers = list(dict.fromkeys(list(ws.PUT_TICKERS) + list(ws.HOLDINGS.keys())))
    out = {"_meta": {"built": dt.date.today().isoformat(),
                     "metric": METRIC,
                     "otm_buckets": [int(o * 100) for o in OTM_BUCKETS],
                     "dte_buckets": DTE_BUCKETS,
                     "iv_buckets": [int(round(b * 100)) for b in IV_BUCKETS],
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
