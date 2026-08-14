"""
early_trend.py -- shared logic for an EARLY-STAGE trend/breakout screener,
distinct from a standard momentum screener (which ranks trailing return and,
by definition, only lights up after a move has already happened). This looks
for a stock breaking OUT of a long base on volume, with relative-strength
ACCELERATION vs both its own recent past and the broader market -- and caps
how far price is allowed to already be past that breakout before it's too
extended to count as "early" anymore.

No rules-based signal can guarantee catching a trend before it runs -- if it
reliably could, it would get arbitraged away. backtest_early_trend.py re-runs
the exact rule function below (_evaluate_breakout_at) against history so the
thresholds can be validated (or tuned) before the live page's output is
trusted, rather than assumed to work.

Three-stage funnel, same shape as discover.py's (for the same reason: Tradier
has no batched "which stocks are breaking out" endpoint, and pulling a year of
price history per ticker isn't cheap enough to run against the whole ~7,000-
ticker universe):
  1. CANDIDATE POOL (cheap) -- discover.fetch_universe() + discover.
     batched_stock_stats() give price/volume/avg_volume/change_pct for the
     whole universe in one batched pass. Today's volume surge (relative to the
     ticker's OWN average) and today's up-moves are both weak proxies for
     "something breakout-relevant happened recently" -- not a guarantee. A
     breakout from a few days ago that's since gone quiet on volume can be
     missed here; this is the same tradeoff discover.py accepts for the same
     reason (see its module docstring) rather than paying for a full-universe
     history pull.
  2. BREAKOUT SCAN (mid-cost, candidates only) -- ~1yr OHLCV per candidate via
     yfinance (ws._ticker(...).history()), reusing wheel_screener's existing
     curl_cffi session. Stage-2-style rules (Weinstein basis): rising 150-day
     SMA, a prior base that was range-bound for BASE_WEEKS, a fresh breakout
     (within BREAKOUT_RECENT_DAYS) to a multi-month high on above-average
     volume, price still close to the breakout pivot (BREAKOUT_BAND_PCT -- the
     "not already extended" filter), 4-week return accelerating vs both the
     stock's own prior 4 weeks and SPY's 4 weeks, and NOT already up more than
     EXTENSION_CAP_PCT over the last ~3 months (the explicit "don't chase an
     already-spiked name" filter -- this is the thing a plain momentum
     screener doesn't have). Market cap filter (discover.market_cap()) applies
     only to survivors here, same reasoning as discover.py.
  3. OPTIONS-TILT CONFIRMATION (expensive, survivors only) -- nearest in-
     window Tradier chain (mirrors discover.option_open_interest()'s pattern),
     call-vs-put open-interest balance and ATM IV skew folded into the score
     as a secondary confirmation/tiebreaker, never a hard filter (sandbox
     chain data can be thin/delayed for less-liquid names).

Called live from pages/2_Early_Trend.py (cached, ttl=600, same cadence as the
rest of the app) and from backtest_early_trend.py (which re-runs stage 2's
rule function against historical data instead of live quotes).
"""
import sys
import datetime as dt

import pandas as pd

import wheel_screener as ws
import discover

# ---- Stage 1: candidate pool ----------------------------------------------
CANDIDATE_POOL = 200   # top-by-volume-surge names that get the breakout scan
MOVER_POOL = 100        # top today's-up-movers added to the same pool
MIN_DOLLAR_VOLUME = discover.MIN_DOLLAR_VOLUME  # same liquidity floor as Discover

# ---- Stage 2: breakout rules ------------------------------------------------
MIN_MARKET_CAP = 2_000_000_000   # $2B -- lower than Discover's $10B; early-stage
                                 # trends are more often still mid-cap, not yet mega-cap
SMA_WINDOW = 150          # ~30 trading weeks, Weinstein "Stage 2" basis
SMA_TREND_LOOKBACK = 20   # trading days back to confirm the SMA itself is rising
BASE_WEEKS = 8
BASE_DAYS = BASE_WEEKS * 5
BASE_RANGE_PCT = 0.30     # the prior base must be range-bound within this high-low band
BREAKOUT_RECENT_DAYS = 10  # a breakout must have happened within this many trading
                           # days to still count as "fresh"
BREAKOUT_BAND_PCT = 0.08   # ...and price must still be within this % above the pivot
                           # (the "not already extended past the breakout" filter)
BREAKOUT_LOOKBACK_WEEKS = 52   # a full year, so "new high" means a genuine new high --
                               # not just a new high relative to a shorter window. A
                               # shorter lookback can mistake a stock clawing back toward
                               # an OLDER high (e.g. recovering from an earnings-gap crash
                               # that happened just outside the window) for a fresh breakout.
BREAKOUT_LOOKBACK_DAYS = BREAKOUT_LOOKBACK_WEEKS * 5
VOLUME_MULT = 1.5          # breakout-window peak volume vs 50-day average, required
EXTENSION_LOOKBACK_DAYS = 63   # ~3 months
EXTENSION_CAP_PCT = 0.40   # exclude names already up more than this over the last
                           # EXTENSION_LOOKBACK_DAYS -- the explicit "don't chase an
                           # already-spiked name" filter
EXTENSION_LOOKBACK_DAYS_LONG = 126   # ~6 months -- catches names whose LAST 3 months
                                     # looks fine on its own but that already had a big
                                     # multi-month run before that; the 3-month cap alone
                                     # only protects against chasing the most recent leg.
EXTENSION_CAP_PCT_LONG = 0.50        # starting default -- retune with backtest_early_trend.py
RS_WEEKS = 4
RS_DAYS = RS_WEEKS * 5
BENCHMARK = "SPY"


def _candidate_pool(stats, universe):
    have_avg = sum(1 for s in stats.values() if s.get("avg_volume", 0) > 0) >= len(stats) * 0.5

    def dollar_vol(s):
        vol = s["avg_volume"] if have_avg and s["avg_volume"] > 0 else s["volume"]
        return vol * s["price"]

    liquid = [t for t in universe if t in stats and dollar_vol(stats[t]) >= MIN_DOLLAR_VOLUME]
    if have_avg:
        by_surge = sorted((t for t in liquid if stats[t]["avg_volume"] > 0),
                          key=lambda t: stats[t]["volume"] / stats[t]["avg_volume"], reverse=True)
    else:
        print("average_volume unavailable this run -- candidate pool falling back to raw volume",
              file=sys.stderr)
        by_surge = sorted(liquid, key=lambda t: stats[t]["volume"], reverse=True)
    by_move = sorted((t for t in liquid if stats[t]["change_pct"] > 0),
                     key=lambda t: stats[t]["change_pct"], reverse=True)
    return sorted(set(by_surge[:CANDIDATE_POOL]) | set(by_move[:MOVER_POOL]))


def _history_df(symbol, period="1y"):
    try:
        df = ws._ticker(symbol).history(period=period)
        return df if df is not None and not df.empty else None
    except Exception as e:
        print(f"{symbol}: history ERROR {e}", file=sys.stderr)
        return None


def _evaluate_breakout_at(sym, closes, volumes, spy_closes):
    """The rules, evaluated using only `closes`/`volumes` up to and including
    their LAST row (no lookahead) -- this is the single source of truth for
    the breakout screen, shared by both the live scan (closes ends at the most
    recent trading day) and backtest_early_trend.py (closes ends at each
    sampled historical day). `spy_closes`, if given, is a full-length,
    date-indexed SPY close series; only the portion up to `closes`' own last
    date is used, so it's safe to pass the same multi-year series in for every
    call during a backtest."""
    n = len(closes)
    if n < SMA_WINDOW + BASE_DAYS + BREAKOUT_RECENT_DAYS:
        return None

    sma = closes.rolling(SMA_WINDOW).mean()
    sma_now, sma_then = sma.iloc[-1], sma.iloc[-1 - SMA_TREND_LOOKBACK]
    if pd.isna(sma_now) or pd.isna(sma_then) or not (sma_now > sma_then):
        return None

    price_now = float(closes.iloc[-1])
    if not (price_now > sma_now):
        return None

    base_end = n - BREAKOUT_RECENT_DAYS
    base_start = base_end - BASE_DAYS
    if base_start < 0:
        return None
    base_slice = closes.iloc[base_start:base_end]
    base_hi, base_lo = float(base_slice.max()), float(base_slice.min())
    if base_lo <= 0 or (base_hi - base_lo) / base_lo > BASE_RANGE_PCT:
        return None
    pivot = base_hi

    recent = closes.iloc[base_end:]
    broke = recent[recent > pivot]
    if broke.empty:
        return None   # base hasn't actually been broken out of yet
    days_since = len(recent) - 1 - recent.index.get_loc(broke.index[0])

    extension = (price_now - pivot) / pivot
    if not (0 <= extension <= BREAKOUT_BAND_PCT):
        return None   # either hasn't broken out, or already run too far past it

    lookback_n = min(BREAKOUT_LOOKBACK_DAYS, n)
    window_high = float(closes.iloc[-lookback_n:].max())
    if price_now < window_high * 0.98:
        return None   # pivot isn't actually a meaningful multi-month high

    avg_vol_50 = float(volumes.iloc[-50:].mean())
    recent_vol_peak = float(volumes.iloc[base_end:].max())
    if avg_vol_50 <= 0 or recent_vol_peak < VOLUME_MULT * avg_vol_50:
        return None

    ret_3mo = None
    if n > EXTENSION_LOOKBACK_DAYS:
        ret_3mo = price_now / float(closes.iloc[-EXTENSION_LOOKBACK_DAYS]) - 1
        if ret_3mo > EXTENSION_CAP_PCT:
            return None   # already spiked -- exactly what this screen is trying NOT to catch

    ret_6mo = None
    if n > EXTENSION_LOOKBACK_DAYS_LONG:
        ret_6mo = price_now / float(closes.iloc[-EXTENSION_LOOKBACK_DAYS_LONG]) - 1
        if ret_6mo > EXTENSION_CAP_PCT_LONG:
            return None   # last 3 months looked fine, but it already had its bigger run before that

    if n <= 2 * RS_DAYS:
        return None
    ret_4w = price_now / float(closes.iloc[-RS_DAYS]) - 1
    ret_prior_4w = float(closes.iloc[-RS_DAYS]) / float(closes.iloc[-2 * RS_DAYS]) - 1
    if not (ret_4w > ret_prior_4w):
        return None   # not actually accelerating, just "up"

    spy_ret_4w = None
    if spy_closes is not None:
        spy_upto = spy_closes.loc[:closes.index[-1]]
        if len(spy_upto) >= RS_DAYS:
            spy_ret_4w = float(spy_upto.iloc[-1]) / float(spy_upto.iloc[-RS_DAYS]) - 1
            if not (ret_4w > spy_ret_4w):
                return None   # not actually outperforming the market

    freshness = max(0.0, 1 - days_since / BREAKOUT_RECENT_DAYS)
    volume_score = min(recent_vol_peak / avg_vol_50 / VOLUME_MULT, 2.0)
    rs_accel = (ret_4w - ret_prior_4w) + (ret_4w - (spy_ret_4w or 0.0))
    extension_penalty = max(1 - extension / BREAKOUT_BAND_PCT, 0.05)
    score = freshness * volume_score * (1 + max(rs_accel, 0.0)) * extension_penalty

    asof = closes.index[-1]
    return {
        "ticker": sym,
        "asof": str(asof.date()) if hasattr(asof, "date") else str(asof),
        "price": round(price_now, 2),
        "pivot": round(pivot, 2),
        "days_since_breakout": int(days_since),
        "extension_pct": round(extension * 100, 2),
        "base_range_pct": round((base_hi - base_lo) / base_lo * 100, 2),
        "volume_ratio": round(recent_vol_peak / avg_vol_50, 2),
        "ret_4w_pct": round(ret_4w * 100, 2),
        "ret_prior_4w_pct": round(ret_prior_4w * 100, 2),
        "spy_ret_4w_pct": round(spy_ret_4w * 100, 2) if spy_ret_4w is not None else None,
        "ret_3mo_pct": round(ret_3mo * 100, 2) if ret_3mo is not None else None,
        "ret_6mo_pct": round(ret_6mo * 100, 2) if ret_6mo is not None else None,
        "score": round(score, 4),
    }


def _evaluate_breakout(sym, spy_closes):
    df = _history_df(sym)
    if df is None:
        return None
    return _evaluate_breakout_at(sym, df["Close"], df["Volume"], spy_closes)


# ---- Stage 3: options-positioning confirmation ------------------------------
def _atm_iv_skew(chain, spot):
    if not chain or not spot:
        return None
    strikes = sorted({o["strike"] for o in chain})
    if not strikes:
        return None
    atm = min(strikes, key=lambda k: abs(k - spot))
    call_iv = next((o["iv"] for o in chain if o["type"] == "call" and o["strike"] == atm and o["iv"]), None)
    put_iv = next((o["iv"] for o in chain if o["type"] == "put" and o["strike"] == atm and o["iv"]), None)
    if not call_iv or not put_iv:
        return None
    return round(call_iv - put_iv, 4)


def options_tilt(symbol, today, spot):
    """Nearest in-window expiration's call-vs-put OI balance and ATM IV skew --
    a secondary confirmation only (see module docstring), not a hard filter."""
    windowed = ws._expirations_in_window(symbol, today)
    if not windowed:
        return None
    exp, _, _ = min(windowed, key=lambda w: w[2])
    try:
        chain = ws.td_chain(symbol, exp)
    except Exception as e:
        print(f"{symbol}: chain ERROR {e}", file=sys.stderr)
        return None
    if not chain:
        return None
    call_oi = sum(int(o.get("oi") or 0) for o in chain if o.get("type") == "call")
    put_oi = sum(int(o.get("oi") or 0) for o in chain if o.get("type") == "put")
    oi_skew = call_oi / (call_oi + put_oi) if (call_oi + put_oi) > 0 else None
    return {"oi_skew": oi_skew, "iv_skew": _atm_iv_skew(chain, spot),
            "call_oi": call_oi, "put_oi": put_oi}


def run_scan():
    """Runs the full 3-stage funnel and returns a score-ranked list of dicts,
    one per qualifying ticker."""
    today = dt.date.today()
    watchlist = set(ws.PUT_TICKERS)
    universe = [t for t in discover.fetch_universe() if t not in watchlist]

    print(f"Stage 1: batched quotes for {len(universe)} tickers...")
    stats = discover.batched_stock_stats(universe)
    candidates = _candidate_pool(stats, universe)
    print(f"Stage 1 done: {len(candidates)} candidates for the breakout scan.")

    print("Stage 2: breakout scan (1yr price history per candidate)...")
    spy_df = _history_df(BENCHMARK)
    spy_closes = spy_df["Close"] if spy_df is not None else None
    results = []
    for sym in candidates:
        try:
            r = _evaluate_breakout(sym, spy_closes)
            if r:
                results.append(r)
        except Exception as e:
            print(f"{sym}: breakout ERROR {e}", file=sys.stderr)
    print(f"Stage 2 done: {len(results)} pass the breakout rules.")

    print(f"Stage 2b: market-cap filter (>= ${MIN_MARKET_CAP:,.0f})...")
    results = [r for r in results if (discover.market_cap(r["ticker"]) or 0) >= MIN_MARKET_CAP]
    print(f"Stage 2b done: {len(results)} survive.")

    print("Stage 3: options-positioning tilt (confirmation, not a hard filter)...")
    for r in results:
        tilt = options_tilt(r["ticker"], today, r["price"])
        r["oi_skew"] = tilt["oi_skew"] if tilt else None
        r["iv_skew"] = tilt["iv_skew"] if tilt else None
        if tilt and tilt["oi_skew"] is not None:
            r["score"] = round(r["score"] * (1 + 0.25 * (tilt["oi_skew"] - 0.5)), 4)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results
