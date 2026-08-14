#!/usr/bin/env python3
"""
backtest_early_trend.py -- validates early_trend.py's breakout rules against
history before trusting the live "Early Trend" page. Walks each ticker's price
history (sampled every STEP trading days to keep runtime sane), re-running the
EXACT SAME rule function as the live scan (early_trend._evaluate_breakout_at),
using only data available as of that day -- no lookahead. Every day the rules
flag a breakout, records the forward 1mo/3mo/6mo return and compares the
aggregate to a same-horizon SPY buy-and-hold baseline.

This is a RULES backtest, not a portfolio backtest -- it does not model
position sizing, overlapping trades, or slippage, and it never actually buys
anything. It only asks: "historically, after this rule fired, did the stock
tend to keep going?" A positive edge here is evidence the screen isn't just
noise; it doesn't guarantee the edge repeats going forward. A null/negative
edge is a strong reason to tune early_trend.py's thresholds before trusting
the live page at all.

Universe: S&P 500 (sp500_tickers.py) to start -- broadening to early_trend's
full ~7,000-ticker live universe would multiply runtime for a backtest whose
whole point is validation, not completeness.

Needs no Tradier token (yfinance-only, via wheel_screener._ticker). Takes a
while to run (hundreds of tickers x years of history) -- run it locally, not
as part of the live app.
"""
import sys
import statistics as stats

import early_trend as et
from sp500_tickers import SP500_TICKERS

PERIOD = "3y"    # history window pulled per ticker
STEP = 5          # sample every STEP trading days (weekly) -- daily would be
                  # ~5x the runtime for little extra signal, since a fresh
                  # breakout stays flagged for BREAKOUT_RECENT_DAYS anyway
HORIZONS = {"1mo": 21, "3mo": 63, "6mo": 126}   # trading days forward


def _forward_returns(closes, i):
    price0 = float(closes.iloc[i])
    out = {}
    for name, days in HORIZONS.items():
        j = i + days
        out[name] = float(closes.iloc[j]) / price0 - 1 if j < len(closes) else None
    return out


def backtest_ticker(sym, spy_closes):
    df = et._history_df(sym, period=PERIOD)
    if df is None or len(df) < 300:
        return []
    closes, volumes = df["Close"], df["Volume"]
    max_i = len(closes) - HORIZONS["6mo"] - 1   # leave room for the longest forward window
    hits = []
    for i in range(0, max_i, STEP):
        r = et._evaluate_breakout_at(sym, closes.iloc[:i + 1], volumes.iloc[:i + 1], spy_closes)
        if r:
            r["forward"] = _forward_returns(closes, i)
            hits.append(r)
    return hits


def main():
    print(f"Backtest universe: {len(SP500_TICKERS)} S&P 500 tickers, period={PERIOD}, step={STEP}d")
    spy_df = et._history_df("SPY", period=PERIOD)
    if spy_df is None:
        print("Could not fetch SPY history -- aborting", file=sys.stderr)
        sys.exit(1)
    spy_closes = spy_df["Close"]

    all_hits = []
    for idx, sym in enumerate(SP500_TICKERS):
        try:
            hits = backtest_ticker(sym, spy_closes)
            all_hits.extend(hits)
            if hits:
                print(f"[{idx + 1}/{len(SP500_TICKERS)}] {sym}: {len(hits)} historical flags")
        except Exception as e:
            print(f"{sym}: ERROR {e}", file=sys.stderr)

    print(f"\nTotal historical flags: {len(all_hits)} across {len(SP500_TICKERS)} tickers\n")
    if not all_hits:
        print("No historical flags at all -- thresholds are too strict to evaluate. Loosen "
              "early_trend.py's BASE_RANGE_PCT / VOLUME_MULT / BREAKOUT_BAND_PCT and re-run.")
        return

    for name, days in HORIZONS.items():
        rets = [h["forward"][name] for h in all_hits if h["forward"][name] is not None]
        if not rets:
            continue
        spy_ret = float(spy_closes.iloc[-1]) / float(spy_closes.iloc[max(0, len(spy_closes) - 1 - days)]) - 1
        hit_rate = sum(1 for r in rets if r > 0) / len(rets)
        print(f"{name:>4} forward return (n={len(rets)}): "
              f"median {stats.median(rets) * 100:+.1f}%  mean {stats.mean(rets) * 100:+.1f}%  "
              f"hit-rate {hit_rate * 100:.0f}%   [SPY trailing {name}: {spy_ret * 100:+.1f}%]")


if __name__ == "__main__":
    main()
