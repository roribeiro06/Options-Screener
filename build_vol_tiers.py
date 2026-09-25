#!/usr/bin/env python3
"""
build_vol_tiers.py -- writes vol_tiers.json, the daily volatility snapshot the app
reads to tier each ticker (see wheel_screener.vol_tier / the "Volatility tiers"
block there).

For every ticker in the universe (watchlist + holdings + open/closed positions +
the S&P 500) it stores {hv30, hv1y, days, as_of}: annualized close-to-close
volatility over the last 30 and last 252 trading days (wheel_screener.vol_metrics
-- same formula as build_history.rolling_rv), the number of closes it had, and
today's date. The tier itself (low/medium/high) is NOT stored -- it's derived at
read time from the thresholds in wheel_screener.py, so retuning VOL_LOW/VOL_HIGH
needs no rebuild.

Data: yfinance daily closes (split/dividend-adjusted), batched 100 tickers per
call. A ticker that fails this run keeps its previous entry, whose old `as_of`
lets the app ignore it once it's older than VOL_MAX_AGE_DAYS (then it's computed
live, and failing that treated as HIGH) -- a dead updater can't leave a stale
"low" behind. If EVERY ticker fails (e.g. blocked), the file is left untouched
and the script exits non-zero so the Action run shows as failed.

Run by .github/workflows/build-vol-tiers.yml on weekdays after the close.
"""
import datetime as dt
import json
import os
import sys

import yfinance as yf

import wheel_screener as ws
from sp500_tickers import SP500_TICKERS

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ws.VOL_TIERS_FILE)
CHUNK = 100


def universe():
    t = list(ws.PUT_TICKERS) + list(ws.HOLDINGS) + list(ws.HOLDINGS_SHARES)
    t += [p["ticker"] for p in ws.OPEN_POSITIONS + ws.CLOSED_POSITIONS]
    t += list(SP500_TICKERS)
    return list(dict.fromkeys(t))


def closes_by_ticker(chunk):
    yf_sym = {t: t.replace("/", "-") for t in chunk}     # Yahoo spells share classes BRK-B, not BRK/B
    df = yf.download(list(yf_sym.values()), period="13mo", auto_adjust=True, group_by="ticker",
                     progress=False, threads=True)
    out = {}
    for t in chunk:
        try:
            col = df["Close"] if len(chunk) == 1 else df[yf_sym[t]]["Close"]
            out[t] = [float(x) for x in col.dropna()]
        except Exception:
            out[t] = []
    return out


def main():
    try:
        with open(OUT) as f:
            prev = json.load(f).get("tickers", {})
    except Exception:
        prev = {}
    tickers = universe()
    today = dt.date.today().isoformat()
    new, failed = {}, []
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        try:
            closes = closes_by_ticker(chunk)
        except Exception as e:
            print(f"chunk {i // CHUNK}: ERROR {e}", file=sys.stderr)
            closes = {t: [] for t in chunk}
        for t in chunk:
            hv30, hv1y, n = ws.vol_metrics(closes.get(t, []))
            if hv30 is None or hv1y is None:
                failed.append(t)
                if t in prev:
                    new[t] = prev[t]
                continue
            new[t] = {"hv30": round(hv30, 4), "hv1y": round(hv1y, 4), "days": n, "as_of": today}
    fresh = [t for t in tickers if t in new and new[t].get("as_of") == today]
    print(f"{len(fresh)}/{len(tickers)} tickers updated; {len(failed)} failed: {failed[:20]}")
    if not fresh:
        print("nothing fetched -- leaving the file untouched", file=sys.stderr)
        sys.exit(1)
    counts = {"low": 0, "medium": 0, "high": 0}
    for t in fresh:
        e = new[t]
        v = max(e["hv30"], e["hv1y"])
        tier = ("high" if e["days"] < ws.VOL_MIN_DAYS or v >= ws.VOL_HIGH
                else "low" if v < ws.VOL_LOW else "medium")
        counts[tier] += 1
    print("tiers today:", counts)
    with open(OUT, "w") as f:
        json.dump({"_meta": {"source": "yfinance", "generated": today,
                             "note": "hv30/hv1y annualized; tier derived in wheel_screener.vol_tier"},
                   "tickers": dict(sorted(new.items()))}, f, indent=0, sort_keys=False)
        f.write("\n")


if __name__ == "__main__":
    main()
