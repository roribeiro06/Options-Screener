#!/usr/bin/env python3
"""
backtest_early_trend.py -- validates early_trend.py's breakout rules against
history before trusting the live "Early Trend" page. Walks each ticker's price
history (sampled every STEP trading days to keep runtime sane), re-running the
EXACT SAME rule function as the live scan (early_trend._evaluate_breakout_at),
using only data available as of that day -- no lookahead.

Seven reports plus a closing caveat, in order:

  1. FORWARD RETURN vs. SPY (same historical windows) -- the original check:
     did flagged stocks outrun a same-period SPY buy-and-hold at 1/3/6 months.

  2. OUTCOME TRACKING (precision) -- for every DE-DUPLICATED flag (see below),
     walks the ACTUAL future price path (this part intentionally looks ahead
     -- that's the whole point, unlike the no-lookahead rule check itself)
     and answers: did it actually go up afterward, how many trading days
     until it first cleared +5%, how long did it stay above the flag price
     before giving that back (if it ever did), how big was the best move and
     how long did that take, and where does it stand today.

  3. BOOM RECALL -- independently of anything the screener did, scans each
     ticker's own price history for "booms" (a >=BOOM_THRESHOLD rise within
     BOOM_WINDOW trading days), then checks whether the screener ever flagged
     that ticker during the boom's run-up (trough to peak). "Of the real
     booms that happened, how many did we catch (and how early), which did we
     miss." Tickers that never boomed are simply not counted.

  4. SCORE CORRELATION -- does the Score column's ranking actually separate
     better outcomes from worse ones, or is it not adding information beyond
     a bare flag/no-flag.

  5. TOP FLAGGED TICKERS -- which names fire most often, as a check for
     concentration in one sector/style rather than broad, genuine trend-finding.

  6. BOOM PRECISION -- the flip side of report 3: of the flags raised, what %
     were actually part of a real boom (same trough..peak definition), a
     stricter bar than report 2's +5% outcome check.

  7. YEAR-BY-YEAR -- whether any edge holds up across different periods or is
     concentrated in one regime.

  8. MISSED-BOOM DIAGNOSIS -- for every missed boom that lasted long enough
     to be a genuine developing trend (>= MIN_TREND_DURATION_DAYS, excluding
     single-catalyst-style spikes no chart pattern could catch), samples days
     across its trough-to-peak run and finds the day that came CLOSEST to
     firing a flag (fewest failing checks), then reports which SPECIFIC rule
     condition was most often the blocker. Turns "the rules miss most real
     trends" from a known fact into "here's specifically which gate is doing
     that," so any future loosening is evidence-based, not a guess.

  9. COMPONENT CORRELATIONS -- for every raw signal early_trend.py computes
     (extension %, base range %, volatility %, volume ratio, RS acceleration,
     RS vs. SPY, days since breakout, 3mo/6mo return, and the current Score
     itself), correlates it against actual outcome (peak gain, current
     return). Same purpose as report 4 but one level deeper: report 4 checks
     whether the Score COMPOSITE predicts outcome; this checks which
     INGREDIENTS of that composite actually carry signal, so a Score rework
     is evidence-based rather than a guess at which factors matter.

  Flags are sampled every STEP trading days and a fresh breakout stays
  flagged for up to BREAKOUT_RECENT_DAYS, so a single real breakout usually
  fires several times in a row. Reports 2 and 4-6 de-duplicate to the FIRST
  flag of each such run per ticker (see _dedupe_first_appearance) -- the
  actual moment something "appeared" -- rather than over-counting the same
  event. Reports 1, 3, and 7 intentionally use the raw (non-deduped) flags,
  since they're either keyed off distinct historical windows anyway (1, 7)
  or checking ticker-level boom coverage regardless of how many times a
  ticker fired (3).

  A closing caveat notes that stage 3 of early_trend.py (live options
  positioning) has no historical equivalent and is not validated by anything
  here.

  MARKET-CAP FILTERING (found and fixed after 7 rounds of otherwise-validated
  tuning): the live run_scan() rejects anything below MIN_MARKET_CAP at Stage
  2b, but this backtest was evaluating every ticker's Stage-2 rules with NO
  cap check at all -- meaning it had been measuring "how good are the rules
  in isolation," not "how good is the live app end-to-end." Checked directly:
  3 of 8 spot-checked best-outcome examples (MNPR, RCAT, UAMY) were sub-$2B
  and could never have appeared on the live page regardless of rule quality.
  Now checked once per ticker (cached, not per flag) and applied to every
  report -- flags from sub-floor tickers are excluded from all_hits (so they
  never count toward recall/precision/Score stats), while boom DETECTION
  still runs on the full universe so report 3 can show the cap floor's own
  opportunity cost as a separate, explicit number from rule-tuning misses.
  (Checked the Stage 1 liquidity floor too, MIN_DOLLAR_VOLUME -- the same 8
  examples all cleared it comfortably, so it wasn't added; a spot check, not
  an exhaustive one.) Stage 1's actual candidate-pool SELECTION (today's
  volume surge, live-only) remains untested here and would need a much larger
  effort to backtest properly (simulating what the daily candidate list would
  have looked like on every past trading day) -- a known, open limitation,
  not one this backtest can currently answer.

This is a RULES backtest, not a portfolio backtest -- no position sizing, no
slippage, it never actually buys anything. A positive edge here is evidence
the screen isn't just noise; it doesn't guarantee the edge repeats going
forward. A null/negative edge is a strong reason to tune early_trend.py's
thresholds before trusting the live page.

Universe: S&P 500 + Russell 2500 (sp500_tickers.py + russell2500_tickers.py),
~2,900 tickers combined -- NOT S&P 500 alone. S&P 500 membership is a
LAGGING signal (a stock usually joins well after most of its growth story
has already played out), so backtesting only against S&P 500 constituents
biases the sample toward exactly the population this screen ISN'T trying to
find. Russell 2500 (small/mid-cap) adds the population where an early trend
would actually still be findable. See russell2500_tickers.py's own docstring
for how that list was built and its point-in-time-snapshot caveat (same
limitation sp500_tickers.py already has). Full ~7,000-ticker live universe
would multiply runtime further for a backtest whose whole point is
validation, not completeness -- this combined ~2,900 is the practical middle
ground.

Needs no Tradier token (yfinance-only, via wheel_screener._ticker). Takes a
while to run (thousands of tickers x years of history) -- run it locally,
not as part of the live app.
"""
import sys
import statistics as stats
from collections import Counter, defaultdict

import pandas as pd

import early_trend as et
import discover
from sp500_tickers import SP500_TICKERS
from russell2500_tickers import RUSSELL2500_TICKERS

BACKTEST_UNIVERSE = sorted(set(SP500_TICKERS) | set(RUSSELL2500_TICKERS))

PERIOD = "3y"    # history window pulled per ticker
STEP = 5          # sample every STEP trading days (weekly) when re-running the
                  # rule check -- daily would be ~5x the runtime for little
                  # extra signal, since a fresh breakout stays flagged for
                  # BREAKOUT_RECENT_DAYS anyway
HORIZONS = {"1mo": 21, "3mo": 63, "6mo": 126}   # trading days forward

# -- Outcome tracking (report 2) --
OUTCOME_CAP_DAYS = 252        # how far forward to track each flag (~1yr)
OUTCOME_UP_THRESHOLD = 0.05   # "actually increased" = closed at least 5% above the flag price
MIN_TRACK_DAYS = 63           # a flag needs at least this much forward data (~3mo) before
                              # its outcome counts toward the aggregate stats below -- a flag
                              # from last week hasn't had time to prove anything either way

# -- Boom recall (report 3) --
BOOM_THRESHOLD = 0.50   # a rise of at least this much counts as a "boom"
BOOM_WINDOW = 126        # ...within this many trading days (~6 months)
BOOM_MERGE_GAP = 10      # merge qualifying days into one episode if this close together

# -- Missed-boom diagnosis (report 8) --
MIN_TREND_DURATION_DAYS = 40   # ~8 weeks -- a boom shorter than this (e.g. DRUG: +5709% in
                               # 83d, BNAI: +5150% in 21d) is almost certainly a single binary
                               # catalyst event (FDA approval, buyout, squeeze), not a
                               # developing TREND -- no chart-pattern rule could plausibly
                               # catch that, so diagnosing why it was missed would be noise.
                               # Only booms that had enough time to show real technical
                               # structure count as "should this rule set have caught it."


def _forward_returns(closes, i):
    price0 = float(closes.iloc[i])
    out = {}
    for name, days in HORIZONS.items():
        j = i + days
        if j >= len(closes) or pd.isna(price0):
            out[name] = None
            continue
        price_j = float(closes.iloc[j])
        out[name] = price_j / price0 - 1 if pd.notna(price_j) else None
    return out


def _spy_forward_returns(spy_closes, asof_date):
    """SPY's OWN forward return starting from the SAME calendar date as a given
    flag -- not SPY's return as of today. Comparing a flag's forward return to
    SPY's trailing return (as of whenever the backtest happens to run) mixes up
    unrelated time periods and isn't a fair baseline; this aligns them."""
    pos = spy_closes.index.get_indexer([asof_date], method="pad")[0]
    if pos < 0:
        return {}
    price0 = float(spy_closes.iloc[pos])
    out = {}
    for name, days in HORIZONS.items():
        j = pos + days
        if j >= len(spy_closes) or pd.isna(price0):
            out[name] = None
            continue
        price_j = float(spy_closes.iloc[j])
        out[name] = price_j / price0 - 1 if pd.notna(price_j) else None
    return out


def _flag_outcome(closes, i, flag_price):
    """Walks the ACTUAL forward price path from flag day `i` (intentionally
    looking ahead -- this is outcome tracking, not the rule check) up to
    OUTCOME_CAP_DAYS later or however much data is actually available,
    whichever is shorter. Returns None only if there's no forward data at all
    (a flag on the very last available day)."""
    track = closes.iloc[i:i + OUTCOME_CAP_DAYS + 1].dropna()
    n = len(track)
    if n < 2:
        return None

    first_up = None
    for k in range(1, n):
        if float(track.iloc[k]) >= flag_price * (1 + OUTCOME_UP_THRESHOLD):
            first_up = k
            break

    peak_offset = int(track.iloc[1:].values.argmax()) + 1
    peak_price = float(track.iloc[peak_offset])

    days_stayed_above, reverted = None, False
    if first_up is not None:
        last_above = first_up
        for k in range(first_up, n):
            if float(track.iloc[k]) >= flag_price:
                last_above = k
            else:
                reverted = True
                break
        days_stayed_above = last_above - first_up + 1

    current_price = float(track.iloc[-1])
    return {
        "days_tracked": n - 1,
        "ever_increased_5pct": first_up is not None,
        "days_to_first_increase": first_up,
        "peak_gain_pct": round((peak_price / flag_price - 1) * 100, 2),
        "days_to_peak": peak_offset,
        "days_stayed_above_flag_price": days_stayed_above,
        "reverted_below_flag_price": reverted,
        "current_return_pct": round((current_price / flag_price - 1) * 100, 2),
    }


def scan_flags(sym, closes, volumes, spy_closes):
    max_i = len(closes) - HORIZONS["6mo"] - 1
    hits = []
    for i in range(0, max_i, STEP):
        r = et._evaluate_breakout_at(sym, closes.iloc[:i + 1], volumes.iloc[:i + 1], spy_closes)
        if r:
            r["i"] = i
            r["forward"] = _forward_returns(closes, i)
            r["spy_forward"] = _spy_forward_returns(spy_closes, closes.index[i])
            r["outcome"] = _flag_outcome(closes, i, r["price"])
            hits.append(r)
    return hits


def find_boom_episodes(closes):
    """Every day whose close is >= BOOM_THRESHOLD above the trailing
    BOOM_WINDOW-day low counts as part of a boom; consecutive qualifying days
    (allowing gaps up to BOOM_MERGE_GAP) are grouped into one episode, whose
    peak is the highest close in the group and whose trough is the lowest
    close in the BOOM_WINDOW days before that peak. Independent of anything
    the screener did -- this only looks at where the ticker actually went."""
    roll_min = closes.rolling(window=BOOM_WINDOW + 1, min_periods=1).min()
    vals, mins = closes.values, roll_min.values
    n = len(vals)
    qualifies = [mins[j] > 0 and (vals[j] / mins[j] - 1) >= BOOM_THRESHOLD for j in range(n)]
    idxs = [j for j in range(n) if qualifies[j]]
    if not idxs:
        return []

    runs = []
    run_start = prev = idxs[0]
    for j in idxs[1:]:
        if j - prev <= BOOM_MERGE_GAP:
            prev = j
        else:
            runs.append((run_start, prev))
            run_start = prev = j
    runs.append((run_start, prev))

    episodes = []
    for run_start, run_end in runs:
        seg = vals[run_start:run_end + 1]
        peak_idx = run_start + int(seg.argmax())
        lo = max(0, peak_idx - BOOM_WINDOW)
        pretrough = vals[lo:peak_idx + 1]
        # last occurrence of the min, not the first -- if price sat flat at the
        # low for a while before climbing, we want the day right before it took
        # off, not the earliest day that happened to touch the same low.
        trough_idx = lo + (len(pretrough) - 1 - int(pretrough[::-1].argmin()))
        gain = vals[peak_idx] / vals[trough_idx] - 1
        episodes.append({"trough_idx": trough_idx, "peak_idx": peak_idx, "gain_pct": round(gain * 100, 2)})
    return episodes


def _diagnose_breakout_at(sym, closes, volumes, spy_closes):
    """Mirrors early_trend._evaluate_breakout_at's checks WITHOUT short-
    circuiting on the first failure -- diagnostic use only, never a source of
    truth (that stays _evaluate_breakout_at). Returns a dict of
    check_name -> True/False, omitting checks that genuinely can't be
    evaluated yet (not enough history for that specific window), or None if
    there isn't even enough history to evaluate anything at all."""
    n = len(closes)
    if n < et.SMA_WINDOW + et.BASE_DAYS + et.BREAKOUT_RECENT_DAYS:
        return None

    checks = {}
    volatility_pct = et._realized_vol(closes)
    checks["volatility_floor"] = volatility_pct >= et.MIN_VOLATILITY_PCT
    vol_scale = min(volatility_pct / et.REFERENCE_VOLATILITY_PCT, et.MAX_VOL_SCALE) if volatility_pct > 0 else 0.0
    eff_base_range = et.BASE_RANGE_PCT * vol_scale
    eff_breakout_band = et.BREAKOUT_BAND_PCT * vol_scale
    eff_extension_cap = et.EXTENSION_CAP_PCT * vol_scale
    eff_extension_cap_long = et.EXTENSION_CAP_PCT_LONG * vol_scale

    sma = closes.rolling(et.SMA_WINDOW).mean()
    sma_now, sma_then = sma.iloc[-1], sma.iloc[-1 - et.SMA_TREND_LOOKBACK]
    checks["sma_rising"] = bool(pd.notna(sma_now) and pd.notna(sma_then)
                                and sma_now >= sma_then * (1 - et.SMA_RISING_TOLERANCE_PCT))

    price_now = float(closes.iloc[-1])
    checks["price_above_sma"] = bool(pd.notna(sma_now) and price_now > sma_now)

    base_end = n - et.BREAKOUT_RECENT_DAYS
    base_start = base_end - et.BASE_DAYS
    base_slice = closes.iloc[base_start:base_end]
    base_hi, base_lo = float(base_slice.max()), float(base_slice.min())
    checks["base_range"] = bool(base_lo > 0 and (base_hi - base_lo) / base_lo <= eff_base_range)
    pivot = base_hi

    recent = closes.iloc[base_end:]
    checks["broke_out"] = bool((recent > pivot).any())

    extension = (price_now - pivot) / pivot if pivot > 0 else None
    checks["extension_band"] = bool(extension is not None and 0 <= extension <= eff_breakout_band)

    lookback_n = min(et.BREAKOUT_LOOKBACK_DAYS, n)
    window_high = float(closes.iloc[-lookback_n:].max())
    checks["window_high"] = price_now >= window_high * (1 - et.WINDOW_HIGH_TOLERANCE_PCT)

    avg_vol_50 = float(volumes.iloc[-50:].mean())
    recent_vol_peak = float(volumes.iloc[base_end:].max())
    checks["volume"] = bool(avg_vol_50 > 0 and recent_vol_peak >= et.VOLUME_MULT * avg_vol_50)

    if n > et.EXTENSION_LOOKBACK_DAYS:
        ret_3mo = price_now / float(closes.iloc[-et.EXTENSION_LOOKBACK_DAYS]) - 1
        checks["ext_3mo"] = ret_3mo <= eff_extension_cap
    if n > et.EXTENSION_LOOKBACK_DAYS_LONG:
        ret_6mo = price_now / float(closes.iloc[-et.EXTENSION_LOOKBACK_DAYS_LONG]) - 1
        checks["ext_6mo"] = ret_6mo <= eff_extension_cap_long

    if n > 2 * et.RS_DAYS:
        ret_4w = price_now / float(closes.iloc[-et.RS_DAYS]) - 1
        ret_prior_4w = float(closes.iloc[-et.RS_DAYS]) / float(closes.iloc[-2 * et.RS_DAYS]) - 1
        checks["rs_accel"] = ret_4w > ret_prior_4w
        if spy_closes is not None:
            spy_upto = spy_closes.loc[:closes.index[-1]]
            if len(spy_upto) >= et.RS_DAYS:
                spy_ret_4w = float(spy_upto.iloc[-1]) / float(spy_upto.iloc[-et.RS_DAYS]) - 1
                checks["rs_vs_spy"] = ret_4w > spy_ret_4w

    return checks


def diagnose_missed_boom(sym, closes, volumes, spy_closes, boom):
    """For one missed boom, samples days across [trough_idx, peak_idx] and
    finds the day that came CLOSEST to firing (fewest failing checks),
    returning (n_failing, [failing check names], day index). None if no
    sampled day had enough history to diagnose at all."""
    best = None
    lo, hi = boom["trough_idx"], boom["peak_idx"]
    for i in range(lo, hi + 1, STEP):
        checks = _diagnose_breakout_at(sym, closes.iloc[:i + 1], volumes.iloc[:i + 1], spy_closes)
        if checks is None:
            continue
        failing = [k for k, v in checks.items() if v is False]
        if best is None or len(failing) < best[0]:
            best = (len(failing), failing, i)
    return best


DEDUPE_GAP_DAYS = 15   # a bit more than BREAKOUT_RECENT_DAYS -- flags for the same ticker
                       # closer together than this are almost certainly the SAME breakout
                       # re-appearing across consecutive weekly samples, not independent signals


def _dedupe_first_appearance(hits):
    """Flags are sampled every STEP trading days and a fresh breakout stays
    flagged for up to BREAKOUT_RECENT_DAYS -- so a single real breakout often
    fires several times in a row. Keeps only the FIRST flag of each such run
    per ticker, i.e. the actual moment it "appeared" on the screener."""
    by_ticker = defaultdict(list)
    for h in hits:
        by_ticker[h["ticker"]].append(h)
    first_only = []
    for hs in by_ticker.values():
        hs.sort(key=lambda h: h["i"])
        last_i = None
        for h in hs:
            if last_i is None or h["i"] - last_i > DEDUPE_GAP_DAYS:
                first_only.append(h)
            last_i = h["i"]
    return first_only


def _report_score_correlation(dedup_hits):
    print("\n=== 4. Does the Score column actually predict better outcomes? ===")
    scored = [h for h in dedup_hits if h["outcome"] is not None
              and h["outcome"]["days_tracked"] >= MIN_TRACK_DAYS]
    if len(scored) < 10:
        print("Not enough tracked, de-duplicated flags to check this meaningfully.")
        return
    scores = pd.Series([h["score"] for h in scored])
    peaks = pd.Series([h["outcome"]["peak_gain_pct"] for h in scored])
    currents = pd.Series([h["outcome"]["current_return_pct"] for h in scored])
    print(f"Correlation between Score and peak gain: {scores.corr(peaks):+.2f}   "
          f"between Score and current return: {scores.corr(currents):+.2f}  "
          f"(near 0 means Score isn't actually telling better setups from worse ones)")
    med = stats.median(scores)
    top = [h for h in scored if h["score"] >= med]
    bot = [h for h in scored if h["score"] < med]
    if top and bot:
        print(f"Top half by Score (n={len(top)}): median peak gain "
              f"{stats.median(h['outcome']['peak_gain_pct'] for h in top):+.1f}%, "
              f"median current return {stats.median(h['outcome']['current_return_pct'] for h in top):+.1f}%")
        print(f"Bottom half by Score (n={len(bot)}): median peak gain "
              f"{stats.median(h['outcome']['peak_gain_pct'] for h in bot):+.1f}%, "
              f"median current return {stats.median(h['outcome']['current_return_pct'] for h in bot):+.1f}%")


def _report_component_correlations(dedup_hits):
    print("\n=== 9. Which raw signals actually correlate with outcome (informs Score) ===")
    tracked = [h for h in dedup_hits if h["outcome"] is not None
              and h["outcome"]["days_tracked"] >= MIN_TRACK_DAYS]
    if len(tracked) < 20:
        print("Not enough tracked, de-duplicated flags to check this meaningfully.")
        return
    rows = []
    for h in tracked:
        rs_accel = h["ret_4w_pct"] - h["ret_prior_4w_pct"]
        rs_vs_spy = (h["ret_4w_pct"] - h["spy_ret_4w_pct"]) if h.get("spy_ret_4w_pct") is not None else None
        rows.append({
            "extension_pct": h["extension_pct"],
            "base_range_pct": h["base_range_pct"],
            "volatility_pct": h["volatility_pct"],
            "volume_ratio": h["volume_ratio"],
            "ret_4w_pct": h["ret_4w_pct"],
            "rs_accel": rs_accel,
            "rs_vs_spy": rs_vs_spy,
            "days_since_breakout": h["days_since_breakout"],
            "ret_3mo_pct": h["ret_3mo_pct"],
            "ret_6mo_pct": h["ret_6mo_pct"],
            "current_score": h["score"],
            "peak_gain_pct": h["outcome"]["peak_gain_pct"],
            "current_return_pct": h["outcome"]["current_return_pct"],
        })
    df = pd.DataFrame(rows)
    print(f"n={len(df)} tracked flags. Correlation of each raw signal with what actually happened:")
    for target in ["peak_gain_pct", "current_return_pct"]:
        print(f"\n  vs. {target}:")
        corrs = df.drop(columns=["peak_gain_pct", "current_return_pct"]).corrwith(df[target])
        for name, c in corrs.reindex(corrs.abs().sort_values(ascending=False).index).items():
            print(f"    {name:<20} {c:+.3f}")


def _report_top_tickers(dedup_hits):
    print("\n=== 5. Most frequently flagged tickers (de-duplicated) -- watch for concentration ===")
    counts = Counter(h["ticker"] for h in dedup_hits)
    print(f"{len(counts)} distinct tickers ever flagged, out of {len(BACKTEST_UNIVERSE)} in the universe.")
    top = counts.most_common(15)
    print("Top 15 by flag count -- if this list is dominated by one sector/style "
          "(e.g. slow-moving utilities/insurers rather than growth names), the base+breakout "
          "rules may be tuned too loose for low-volatility names, which trivially clear a tight "
          "range check without representing a genuine emerging trend:")
    for sym, n in top:
        print(f"  {sym:<6} {n} distinct flags")


def _report_boom_precision(dedup_hits, all_booms):
    print("\n=== 6. Boom precision: of the flags raised, what % were part of a real boom ===")
    booms_by_ticker = defaultdict(list)
    for b in all_booms:
        booms_by_ticker[b["ticker"]].append(b)
    part_of_boom = sum(1 for h in dedup_hits
                        if any(b["trough_idx"] <= h["i"] <= b["peak_idx"] for b in booms_by_ticker.get(h["ticker"], [])))
    n = len(dedup_hits)
    if n:
        print(f"{part_of_boom}/{n} ({part_of_boom / n * 100:.0f}%) of distinct flags fell within an actual "
              f">= {BOOM_THRESHOLD:.0%} run-up (same trough..peak window as report 3's recall check). "
              f"This is a stricter bar than report 2's +{OUTCOME_UP_THRESHOLD:.0%} outcome check -- it's the "
              f"flip side of boom recall, using the exact same yardstick, so the two numbers are directly "
              f"comparable: this is 'precision', report 3 is 'recall'.")


def _report_by_year(all_hits):
    print("\n=== 7. Year-by-year: does this hold up across different periods, or is it regime-dependent ===")
    by_year = defaultdict(list)
    for h in all_hits:
        by_year[h["asof"][:4]].append(h)
    for year in sorted(by_year):
        hs = by_year[year]
        paired = [(h["forward"]["3mo"], h.get("spy_forward", {}).get("3mo")) for h in hs
                  if h["forward"]["3mo"] is not None and h.get("spy_forward", {}).get("3mo") is not None]
        if not paired:
            print(f"  {year}: {len(hs)} flags, no resolved 3mo windows yet (too recent)")
            continue
        excess = [s - m for s, m in paired]
        beat = sum(1 for e in excess if e > 0) / len(excess)
        print(f"  {year}: {len(hs)} flags ({len(paired)} w/ a resolved 3mo window) -- "
              f"median 3mo excess vs SPY {stats.median(excess) * 100:+.1f}%, beat-SPY rate {beat * 100:.0f}%")


def _report_forward_vs_spy(all_hits):
    print("\n=== 1. Forward return vs. SPY (same historical windows) ===")
    for name in HORIZONS:
        paired = [(h["forward"][name], h.get("spy_forward", {}).get(name)) for h in all_hits
                  if h["forward"][name] is not None and h.get("spy_forward", {}).get(name) is not None]
        if not paired:
            continue
        rets = [p[0] for p in paired]
        spy_rets = [p[1] for p in paired]
        excess = [s - m for s, m in paired]
        hit_rate = sum(1 for r in rets if r > 0) / len(rets)
        beat_spy_rate = sum(1 for e in excess if e > 0) / len(excess)
        print(f"{name:>4} (n={len(rets)}): stock median {stats.median(rets) * 100:+.1f}% "
              f"mean {stats.mean(rets) * 100:+.1f}% hit-rate {hit_rate * 100:.0f}%  |  "
              f"SPY same-window median {stats.median(spy_rets) * 100:+.1f}% "
              f"mean {stats.mean(spy_rets) * 100:+.1f}%  |  "
              f"excess vs SPY median {stats.median(excess) * 100:+.1f}% "
              f"mean {stats.mean(excess) * 100:+.1f}%  beat-SPY rate {beat_spy_rate * 100:.0f}%")


def _report_outcomes(dedup_hits, n_raw):
    print("\n=== 2. Outcome tracking: of the flags raised, what actually happened ===")
    print(f"Using {len(dedup_hits)} de-duplicated, first-appearance flags (collapsed from "
          f"{n_raw} raw weekly samples -- the same breakout re-appearing across consecutive "
          f"samples is one event, not several; see report 4 onward for more on this set).")
    all_outcomes = [h for h in dedup_hits if h["outcome"] is not None]
    tracked = [h for h in all_outcomes if h["outcome"]["days_tracked"] >= MIN_TRACK_DAYS]
    print(f"{len(all_outcomes)} flags have any forward data; {len(tracked)} have >= {MIN_TRACK_DAYS} "
          f"trading days of it (the rest are too recent to judge yet and are excluded below).")
    if not tracked:
        return

    o = [h["outcome"] for h in tracked]
    ever_up = [x for x in o if x["ever_increased_5pct"]]
    days_to_up = [x["days_to_first_increase"] for x in ever_up]
    stayed = [x["days_stayed_above_flag_price"] for x in o if x["days_stayed_above_flag_price"] is not None]
    reverted = [x for x in o if x["reverted_below_flag_price"]]
    peak_gains = [x["peak_gain_pct"] for x in o]
    days_to_peak = [x["days_to_peak"] for x in o]

    print(f"Actually increased >= {OUTCOME_UP_THRESHOLD:.0%} at some point: "
          f"{len(ever_up)}/{len(tracked)} ({len(ever_up) / len(tracked) * 100:.0f}%)")
    if days_to_up:
        print(f"  Of those, days until it first crossed +{OUTCOME_UP_THRESHOLD:.0%}: "
              f"median {stats.median(days_to_up):.0f}  mean {stats.mean(days_to_up):.1f}")
    if stayed:
        print(f"  Days it then stayed above the flag price before giving it back (or through today): "
              f"median {stats.median(stayed):.0f}  mean {stats.mean(stayed):.1f}")
    print(f"  Reverted back below the flag price at some point: "
          f"{len(reverted)}/{len(tracked)} ({len(reverted) / len(tracked) * 100:.0f}%)")
    print(f"Best move reached (peak gain from flag price): "
          f"median {stats.median(peak_gains):+.1f}%  mean {stats.mean(peak_gains):+.1f}%, "
          f"taking median {stats.median(days_to_peak):.0f} trading days to get there")

    all_current = [h["outcome"]["current_return_pct"] for h in all_outcomes]
    above_now = [r for r in all_current if r > 0]
    print(f"How they're doing right now (ALL {len(all_outcomes)} flags incl. very recent ones): "
          f"{len(above_now)}/{len(all_current)} ({len(above_now) / len(all_current) * 100:.0f}%) still above "
          f"the flag price, median return since flag {stats.median(all_current):+.1f}%")

    best = sorted(tracked, key=lambda h: -h["outcome"]["peak_gain_pct"])[:5]
    worst = sorted(tracked, key=lambda h: h["outcome"]["current_return_pct"])[:5]
    print("Best-outcome examples (ticker, flag date, peak gain, days to peak):")
    for h in best:
        print(f"  {h['ticker']:<6} {h['asof']}  peak {h['outcome']['peak_gain_pct']:+.1f}% "
              f"in {h['outcome']['days_to_peak']}d, now {h['outcome']['current_return_pct']:+.1f}%")
    print("Worst-outcome examples (ticker, flag date, current return):")
    for h in worst:
        print(f"  {h['ticker']:<6} {h['asof']}  now {h['outcome']['current_return_pct']:+.1f}% "
              f"(peak was {h['outcome']['peak_gain_pct']:+.1f}%)")


def _report_boom_recall(all_booms):
    print("\n=== 3. Boom recall: of the real booms that happened, how many did we catch ===")
    if not all_booms:
        print(f"No booms >= {BOOM_THRESHOLD:.0%} within {BOOM_WINDOW}d found in this universe/period.")
        return
    caught = [b for b in all_booms if b["caught"]]
    missed = [b for b in all_booms if not b["caught"]]
    print(f"{len(all_booms)} booms found (>= {BOOM_THRESHOLD:.0%} within {BOOM_WINDOW} trading days). "
          f"Caught: {len(caught)} ({len(caught) / len(all_booms) * 100:.0f}%)  "
          f"Missed entirely: {len(missed)} ({len(missed) / len(all_booms) * 100:.0f}%)")
    cap_ok_booms = [b for b in all_booms if b.get("cap_ok")]
    if cap_ok_booms:
        cap_caught = [b for b in cap_ok_booms if b["caught"]]
        below_cap = len(all_booms) - len(cap_ok_booms)
        print(f"  Of those, {len(cap_ok_booms)} were in names that clear the live app's "
              f"${et.MIN_MARKET_CAP:,.0f} market-cap floor ({below_cap} were in sub-floor names the "
              f"live app could never show regardless of rule tuning). Recall WITHIN cap-eligible "
              f"names specifically (the fair comparison to what the live app could actually "
              f"achieve): {len(cap_caught)}/{len(cap_ok_booms)} "
              f"({len(cap_caught) / len(cap_ok_booms) * 100:.0f}%).")
    if caught:
        positions = [b["catch_position_pct"] for b in caught]
        print(f"  Of the caught ones, how far into the move the first flag fired "
              f"(0% = right at the bottom, 100% = right at the top): "
              f"median {stats.median(positions):.0f}%  mean {stats.mean(positions):.1f}%")
        earliest = sorted(caught, key=lambda b: b["catch_position_pct"])[:5]
        print("  Earliest catches (ticker, trough->peak dates, gain, caught at % into the move):")
        for b in earliest:
            print(f"    {b['ticker']:<6} {b['trough_date']} -> {b['peak_date']}  "
                  f"+{b['gain_pct']:.0f}% over {b['duration_days']}d, caught at {b['catch_position_pct']:.0f}%")
    if missed:
        biggest_missed = sorted(missed, key=lambda b: -b["gain_pct"])[:15]
        print(f"  Biggest MISSED booms (never flagged during the run-up), by size:")
        for b in biggest_missed:
            print(f"    {b['ticker']:<6} {b['trough_date']} -> {b['peak_date']}  "
                  f"+{b['gain_pct']:.0f}% over {b['duration_days']}d")


def _report_missed_boom_diagnosis(diagnosed):
    print(f"\n=== 8. Why are 'genuine trend' misses (duration >= {MIN_TREND_DURATION_DAYS}d) being missed ===")
    print(f"{len(diagnosed)} missed booms had enough time to develop into a real trend (excludes "
          f"single-catalyst-style spikes like DRUG/BNAI -- those aren't chart patterns any rule "
          f"set could plausibly catch, so including them would just be noise here).")
    resolved = [(sym, boom, best) for sym, boom, best in diagnosed if best is not None]
    if not resolved:
        print("No diagnosable days found.")
        return
    single_blocker = [(sym, boom, best) for sym, boom, best in resolved if best[0] == 1]
    print(f"{len(single_blocker)}/{len(resolved)} had a BEST (closest-call) day where only ONE "
          f"condition was blocking a flag -- everything else already lined up.")
    blocker_counts = Counter()
    for _, _, (n_fail, failing, _) in resolved:
        for f in failing:
            blocker_counts[f] += 1
    print("How often each condition appears among the failing checks on each boom's best day "
          f"(n={len(resolved)}):")
    for name, count in blocker_counts.most_common():
        print(f"  {name:<16} {count} ({count / len(resolved) * 100:.0f}%)")
    if single_blocker:
        print("Single-blocker examples (ticker, trough->peak, gain, the ONE thing in the way):")
        for sym, boom, (n_fail, failing, day_i) in sorted(single_blocker, key=lambda x: -x[1]["gain_pct"])[:10]:
            print(f"  {sym:<6} {boom['trough_date']} -> {boom['peak_date']}  "
                  f"+{boom['gain_pct']:.0f}% over {boom['duration_days']}d  blocked by: {failing[0]}")


def main():
    print(f"Backtest universe: {len(BACKTEST_UNIVERSE)} tickers "
          f"({len(SP500_TICKERS)} S&P 500 + {len(RUSSELL2500_TICKERS)} Russell 2500, deduped), "
          f"period={PERIOD}, step={STEP}d")
    spy_df = et._history_df("SPY", period=PERIOD)
    if spy_df is None:
        print("Could not fetch SPY history -- aborting", file=sys.stderr)
        sys.exit(1)
    spy_closes = spy_df["Close"]

    all_hits, all_booms, diagnosed = [], [], []
    skipped_by_cap = 0
    for idx, sym in enumerate(BACKTEST_UNIVERSE):
        try:
            df = et._history_df(sym, period=PERIOD)
            if df is None or len(df) < 300:
                continue
            closes, volumes = df["Close"], df["Volume"]

            # The live run_scan() rejects anything below MIN_MARKET_CAP at Stage 2b --
            # a flag from a sub-floor ticker could never actually appear on the live
            # page, so it doesn't belong in "how good is the live app" stats. Checked
            # here (once per ticker, not per flag) rather than skipping the ticker
            # outright, so boom DETECTION still runs on the full universe -- that's
            # what lets the recall report show the cap floor's own opportunity cost
            # separately from rule-tuning misses.
            mc = discover.market_cap(sym)
            cap_ok = bool(mc and mc >= et.MIN_MARKET_CAP)

            hits = scan_flags(sym, closes, volumes, spy_closes)
            for h in hits:
                h["ticker"] = sym
            if cap_ok:
                all_hits.extend(hits)
            else:
                skipped_by_cap += len(hits)

            booms = find_boom_episodes(closes)
            flag_idxs = [h["i"] for h in hits] if cap_ok else []
            for b in booms:
                b["ticker"] = sym
                b["cap_ok"] = cap_ok
                b["trough_date"] = str(closes.index[b["trough_idx"]].date())
                b["peak_date"] = str(closes.index[b["peak_idx"]].date())
                b["duration_days"] = b["peak_idx"] - b["trough_idx"]
                caught_flags = [i for i in flag_idxs if b["trough_idx"] <= i <= b["peak_idx"]]
                b["caught"] = bool(caught_flags)
                if caught_flags:
                    span = max(b["peak_idx"] - b["trough_idx"], 1)
                    b["catch_position_pct"] = round((min(caught_flags) - b["trough_idx"]) / span * 100, 1)
                else:
                    b["catch_position_pct"] = None
                if cap_ok and not b["caught"] and b["duration_days"] >= MIN_TREND_DURATION_DAYS:
                    best = diagnose_missed_boom(sym, closes, volumes, spy_closes, b)
                    diagnosed.append((sym, b, best))
            all_booms.extend(booms)

            if hits or booms:
                cap_note = "cap-ok" if cap_ok else f"BELOW ${et.MIN_MARKET_CAP:,.0f}, excluded"
                print(f"[{idx + 1}/{len(BACKTEST_UNIVERSE)}] {sym}: {len(hits)} flags ({cap_note}), "
                      f"{len(booms)} booms ({sum(1 for b in booms if b['caught'])} caught)")
        except Exception as e:
            print(f"{sym}: ERROR {e}", file=sys.stderr)

    print(f"\nTotal: {len(all_hits)} historical flags (market-cap-eligible only -- {skipped_by_cap} "
          f"more were found but excluded for being below the ${et.MIN_MARKET_CAP:,.0f} floor, same "
          f"as the live app would), {len(all_booms)} booms, across {len(BACKTEST_UNIVERSE)} tickers")
    if not all_hits:
        print("No historical flags at all -- thresholds are too strict to evaluate. Loosen "
              "early_trend.py's BASE_RANGE_PCT / VOLUME_MULT / BREAKOUT_BAND_PCT and re-run.")
        return

    dedup_hits = _dedupe_first_appearance(all_hits)

    _report_forward_vs_spy(all_hits)
    _report_outcomes(dedup_hits, len(all_hits))
    _report_boom_recall(all_booms)
    _report_score_correlation(dedup_hits)
    _report_top_tickers(dedup_hits)
    _report_boom_precision(dedup_hits, all_booms)
    _report_by_year(all_hits)
    _report_missed_boom_diagnosis(diagnosed)
    _report_component_correlations(dedup_hits)

    print("\n=== Caveat ===")
    print("This backtest can only validate stages 1-2 of early_trend.py (the price/volume "
          "breakout rules) -- stage 3 (call/put open-interest skew and ATM IV skew from the live "
          "Tradier chain) has no historical equivalent here and is NOT tested by anything above. "
          "A live scan's Score can differ from what this backtest would have ranked, since the "
          "live page applies that options-tilt nudge and this script never can.")


if __name__ == "__main__":
    main()
