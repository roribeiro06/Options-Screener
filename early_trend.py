"""
early_trend.py -- shared logic for an EARLY-STAGE trend/breakout screener,
distinct from a standard momentum screener (which ranks trailing return and,
by definition, only lights up after a move has already happened). This looks
for a stock breaking OUT of a long base on volume, with relative-strength
ACCELERATION vs both its own recent past and the broader market -- and caps
how far price is allowed to already be past that breakout before it's too
extended to count as "early" anymore.

Tuned for GROWTH/short-term movers, not long-term/defensive names. A backtest
run (see backtest_early_trend.py) with fixed, one-size-fits-all thresholds
showed exactly the failure mode you'd expect from that: flags clustered in
calm, low-volatility financials/utilities/insurers, while missing nearly the
entire semiconductor/AI-hardware supercycle (SNDK, SMCI, MU, INTC, AMD...) --
because a volatile grower routinely blows past a fixed base-range/extension/
breakout-band threshold just by being itself, while a sleepy defensive name
glides under the SAME numbers on a meaningless wiggle. MIN_VOLATILITY_PCT now
excludes low-volatility names outright, and the base-range/extension/breakout-
band thresholds scale UP for a more-volatile-than-average stock (proportionally
more room) rather than applying one fixed number to every stock regardless of
its own character. The Score formula was also reworked after the same
backtest run showed the original was actually INVERTED (see its comment
below) -- retune/re-validate anything here with backtest_early_trend.py
rather than assuming it works.

REVISED SCOPE DECISION (2026-08-19, supersedes the 2026-08-18 note below):
initially decided to accept missing SNDK-style parabolic moves as out of
scope, since its realized volatility (107-125% annualized) and 6-month
return (870-1294%) blew through even the volatility-scaled extension caps,
and raising MAX_VOL_SCALE further looked like it would just trade this miss
for letting through already-exhausted blow-off-tops instead of early moves.
Revisited on request: the real distinction isn't the SIZE of the move, it's
whether it was a single-catalyst spike (DRUG: 75% of its whole +5709% gain
happened in ONE week -- a Pfizer-vaccine-announcement-style overnight
re-rating) or a genuinely SUSTAINED multi-month climb the market kept
re-rating over time (SNDK's actual path: $199->$409->$576->$635->$692->
$1187->$1761->$2335 over ~6 months -- memory-chip/AI-supercycle names like
this, not a one-day gap). See MAX_WEEK_CONCENTRATION_PCT and
_is_sustained_move() below -- a move that clears the extension caps but is
demonstrably NOT concentrated in any single week now gets an explicit
exception instead of automatic rejection. Verified: SNDK now passes multiple
times during its real April 2026 climb (ret_6mo up to 551%); DRUG and BNAI
(confirmed single-catalyst spikes) still never pass during their actual
spike windows -- DRUG does pass on some LATER, unrelated dates in 2025-2026,
which is correct, not a leak: those are genuinely different, later setups in
the same ticker's history, not the original spike being re-flagged.

Original decision (2026-08-18, kept for context, no longer current): accept
SNDK-style misses as out of scope rather than chase them by raising
MAX_VOL_SCALE, since that would trade this miss for letting through already-
exhausted blow-off-tops instead of early moves -- MAX_VOL_SCALE itself is
UNCHANGED by the revision above; the fix was adding a duration/concentration-
based exception, not loosening the existing magnitude-based caps further.

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
SMA_TREND_LOOKBACK = 10   # trading days back to confirm the SMA itself is rising -- shortened
                          # from 20 after backtest evidence (report 8) showed sma_rising was
                          # the #2 blocker (37% of genuine trend misses) once window_high was
                          # loosened. Traced WAL (2025-08-25): its 150-day SMA had already
                          # bottomed and been rising for 9 straight trading days (price already
                          # $85 vs SMA $76, a real move well underway) but a 20-day-back
                          # comparison point still fell before the inflection, since a 150-day
                          # SMA is slow enough that a full month's lookback can still reach past
                          # a turn that already happened -- directly at odds with "catch it early."
SMA_RISING_TOLERANCE_PCT = 0.015   # allow the SMA to be flat-to-barely-declining, not just
                                    # strictly higher -- traced LUMN (2024-07-24, missed a
                                    # +922% move): its SMA was still declining at the earliest
                                    # catchable day, just barely (-0.98% over 10 days), because a
                                    # 150-day average genuinely hasn't inflected yet this early in
                                    # a fresh move, no matter how short the comparison window is.
                                    # Rather than keep shortening SMA_TREND_LOOKBACK (diminishing
                                    # returns) or shortening SMA_WINDOW itself (which would abandon
                                    # the actual "established Stage-2 uptrend" character this check
                                    # exists to require, not just how strictly it's checked), this
                                    # treats "no longer meaningfully declining" as passing while a
                                    # clearly still-declining SMA still fails.
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
WINDOW_HIGH_TOLERANCE_PCT = 0.30   # price must be within this % of its own 52-week high --
                                   # loosened from a hard 2% after backtest evidence (report 8
                                   # in backtest_early_trend.py) showed the tight 2% band was
                                   # the single most common blocker on genuine multi-month
                                   # trend misses (61% of them, e.g. MNPR +2248%, ABAT +1011%,
                                   # PDYN +805%) -- a stock recovering from a real drawdown
                                   # into a new uptrend shouldn't have to fully reclaim its
                                   # whole-year high before counting as "early." First widened
                                   # to 15%, which cut its blocking share to 44% but left it
                                   # still #1 by a wide margin over #2 (sma_rising, 30%) --
                                   # widened further to 30%. Keeps the 52-week LOOKBACK itself
                                   # (that part fixed the ZBH-style "clawing back to an old
                                   # pre-crash level" false positive); only the tolerance band
                                   # widened. Re-verified against ZBH directly (2026-08-18): it
                                   # clears window_high easily at either 15% or 30% (93.3% of
                                   # its 52wk high) but is STILL
                                   # correctly rejected -- by MIN_VOLATILITY_PCT instead, since
                                   # a calm recovery like ZBH's just doesn't clear the growth-
                                   # focus volatility floor. The two independent changes end up
                                   # covering for each other on this specific case.
VOLUME_MULT = 1.35         # breakout-window peak volume vs 50-day average, required.
                           # Lowered from 1.5 after report 8 (post volatility-floor fix) showed
                           # volume co-leading the remaining blockers (16%, near-tied with
                           # window_high). Sampled 25 cap-eligible single-blocker examples: no
                           # clean gap like volatility had -- a fairly even spread from 1.09x to
                           # 1.48x -- but 11/25 clustered tightly at 1.40-1.48x (near-boundary
                           # misses, e.g. MXL 1.47x/+714%, TSLA 1.40x/+164%) vs a weaker tail
                           # down to 1.09x (e.g. SEZL 1.02x -- barely any volume pickup at all).
                           # Given the 25% volatility-floor overshoot cost 11pp of precision,
                           # deliberately conservative here: 1.35 captures the clear cluster plus
                           # a bit more (~14/25 examples) without reaching into the weak tail.
EXTENSION_LOOKBACK_DAYS = 63   # ~3 months
EXTENSION_CAP_PCT = 0.40   # exclude names already up more than this over the last
                           # EXTENSION_LOOKBACK_DAYS -- the explicit "don't chase an
                           # already-spiked name" filter
EXTENSION_LOOKBACK_DAYS_LONG = 126   # ~6 months -- catches names whose LAST 3 months
                                     # looks fine on its own but that already had a big
                                     # multi-month run before that; the 3-month cap alone
                                     # only protects against chasing the most recent leg.
EXTENSION_CAP_PCT_LONG = 0.50        # starting default -- retune with backtest_early_trend.py

# ---- Sustained trend exception (a gradual multi-month climb, not a spike) --
# The extension caps above exist to reject a name that's "already spiked" --
# but a stock that grinds up for months (memory-chip/AI-supercycle names:
# SNDK, MU, WDC) is a fundamentally different pattern from one that spikes on
# a single catalyst (DRUG: 75% of its whole move happened in ONE week; a
# Pfizer-vaccine-announcement-style overnight gap). A flat return-over-window
# number can't tell these apart -- decided (2026-08-19) to build that
# distinction in, rather than treat every big multi-month gain as "already
# spiked." WEEK_CONCENTRATION_DAYS/MAX_WEEK_CONCENTRATION check what fraction
# of the total EXTENSION_LOOKBACK_DAYS_LONG gain is attributable to any single
# best week -- verified on real cases: DRUG 75%, BNAI 17% (spikes) vs SNDK 5%,
# LUMN 12% (already-accepted genuine trend). 0.15 sits with real margin on
# both sides of that gap. A move clearing this bar is treated as SUSTAINED --
# the extension caps are skipped for it entirely (not just loosened), since
# the whole premise of those caps (someone is chasing a name AFTER it already
# moved fast) doesn't apply to a trend the market has been re-rating for
# months. A concentrated spike still gets rejected by the caps, unchanged.
WEEK_CONCENTRATION_DAYS = 5          # ~1 trading week
MAX_WEEK_CONCENTRATION_PCT = 0.15    # no single week may explain more than this
                                      # fraction of the total gain, to count as sustained
RS_WEEKS = 4
RS_DAYS = RS_WEEKS * 5
BENCHMARK = "SPY"

# ---- Growth vs. defensive: volatility floor + threshold scaling ------------
# A fixed BASE_RANGE_PCT/EXTENSION_CAP_PCT/BREAKOUT_BAND_PCT calibrated for a
# "typical" stock is easy for a calm, low-volatility name (utilities, insurers,
# staples) to clear on a meaningless wiggle, while a genuinely volatile growth
# name routinely blows past the SAME fixed numbers just by being itself -- a
# backtest run showed exactly this: flags clustered in slow financials/
# utilities while missing nearly the entire semiconductor/AI-hardware
# supercycle (SNDK, SMCI, MU, INTC, AMD...). MIN_VOLATILITY_PCT excludes
# low-vol names outright; the other three thresholds above scale UP for a
# stock more volatile than REFERENCE_VOLATILITY_PCT (proportionally more room)
# and scale DOWN for a calmer one (held to a tighter bar), rather than one
# fixed number applied to every stock regardless of its own character.
VOLATILITY_WINDOW = 20            # trading days for the realized-vol calc
REFERENCE_VOLATILITY_PCT = 0.30   # "typical" stock's annualized realized vol -- the
                                   # baseline the fixed thresholds above are calibrated for
MIN_VOLATILITY_PCT = 0.30         # hard floor -- excludes low-vol defensive names outright.
                                   # Lowered from 0.35 after backtest report 8 (post market-cap
                                   # fix) showed it as the #1 blocker on genuine, cap-eligible
                                   # trend misses (22%). Traced 5 examples (FCX, DBD, HALO,
                                   # CGNX, ADUS): all sat at 30.8-34.8% realized vol. First cut
                                   # went to 0.25 for "safety margin" against ZBH (16.6%) -- too
                                   # far: the very next full backtest showed precision drop from
                                   # 56% to 45%, the single biggest quality cost of any change
                                   # this session, because 0.25 opened the gate to a whole 25-30%
                                   # band never actually verified, not just the traced examples.
                                   # Dialed back to 0.30 -- the tightest floor that still clears
                                   # all 5 traced examples (lowest is ADUS at 30.8%) without the
                                   # extra, unverified margin. Still sits at, not below,
                                   # REFERENCE_VOLATILITY_PCT; growth_score (not this hard gate)
                                   # is what should downweight a merely-average-vol name relative
                                   # to a more explosive one. Re-validate precision after this.
MAX_VOL_SCALE = 3.0                # cap on how much extra room an extreme-vol name gets --
                                   # deliberately NOT raised further to chase true parabolic
                                   # melt-ups (SNDK-style, 800%+ in 6mo); see module docstring


def _realized_vol(closes, window=VOLATILITY_WINDOW):
    """Annualized realized volatility of simple daily returns over the
    trailing `window` days -- a stock's own recent volatility, used both as a
    hard floor (MIN_VOLATILITY_PCT) and to scale the base-range/extension/
    breakout-band thresholds to that stock's own character."""
    rets = closes.pct_change().iloc[-window:]
    vol = rets.std()
    return float(vol * (252 ** 0.5)) if pd.notna(vol) else 0.0


def _is_sustained_move(closes_window, total_gain):
    """True if no single WEEK_CONCENTRATION_DAYS-day stretch within
    `closes_window` explains more than MAX_WEEK_CONCENTRATION_PCT of
    `total_gain` -- a gradual multi-month climb, not a single-catalyst spike.
    See the module-level comment above MAX_WEEK_CONCENTRATION_PCT."""
    if total_gain <= 0:
        return False
    vals = closes_window.values
    n = len(vals)
    if n <= WEEK_CONCENTRATION_DAYS:
        return False   # window too short to judge -- don't grant the exception
    max_week_gain = 0.0
    for i in range(n - WEEK_CONCENTRATION_DAYS):
        r = vals[i + WEEK_CONCENTRATION_DAYS] / vals[i] - 1
        if r > max_week_gain:
            max_week_gain = r
    return (max_week_gain / total_gain) <= MAX_WEEK_CONCENTRATION_PCT


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

    volatility_pct = _realized_vol(closes)
    if volatility_pct < MIN_VOLATILITY_PCT:
        return None   # too calm to be a growth/short-term setup -- see module docstring
    vol_scale = min(volatility_pct / REFERENCE_VOLATILITY_PCT, MAX_VOL_SCALE)
    eff_base_range = BASE_RANGE_PCT * vol_scale
    eff_breakout_band = BREAKOUT_BAND_PCT * vol_scale
    eff_extension_cap = EXTENSION_CAP_PCT * vol_scale
    eff_extension_cap_long = EXTENSION_CAP_PCT_LONG * vol_scale

    sma = closes.rolling(SMA_WINDOW).mean()
    sma_now, sma_then = sma.iloc[-1], sma.iloc[-1 - SMA_TREND_LOOKBACK]
    if pd.isna(sma_now) or pd.isna(sma_then) or sma_now < sma_then * (1 - SMA_RISING_TOLERANCE_PCT):
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
    if base_lo <= 0 or (base_hi - base_lo) / base_lo > eff_base_range:
        return None
    pivot = base_hi

    recent = closes.iloc[base_end:]
    broke = recent[recent > pivot]
    if broke.empty:
        return None   # base hasn't actually been broken out of yet
    days_since = len(recent) - 1 - recent.index.get_loc(broke.index[0])

    extension = (price_now - pivot) / pivot
    if not (0 <= extension <= eff_breakout_band):
        return None   # either hasn't broken out, or already run too far past it

    lookback_n = min(BREAKOUT_LOOKBACK_DAYS, n)
    window_high = float(closes.iloc[-lookback_n:].max())
    if price_now < window_high * (1 - WINDOW_HIGH_TOLERANCE_PCT):
        return None   # not close enough yet to a meaningful multi-month high

    avg_vol_50 = float(volumes.iloc[-50:].mean())
    recent_vol_peak = float(volumes.iloc[base_end:].max())
    if avg_vol_50 <= 0 or recent_vol_peak < VOLUME_MULT * avg_vol_50:
        return None

    ret_3mo = None
    if n > EXTENSION_LOOKBACK_DAYS:
        window_3mo = closes.iloc[-EXTENSION_LOOKBACK_DAYS:]
        ret_3mo = price_now / float(window_3mo.iloc[0]) - 1
        if ret_3mo > eff_extension_cap and not _is_sustained_move(window_3mo, ret_3mo):
            return None   # already spiked -- exactly what this screen is trying NOT to catch

    ret_6mo = None
    if n > EXTENSION_LOOKBACK_DAYS_LONG:
        window_6mo = closes.iloc[-EXTENSION_LOOKBACK_DAYS_LONG:]
        ret_6mo = price_now / float(window_6mo.iloc[0]) - 1
        if ret_6mo > eff_extension_cap_long and not _is_sustained_move(window_6mo, ret_6mo):
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

    # Score, v3: backtest report 9 (raw-signal-vs-actual-outcome correlations,
    # not just the composite) showed v2 had the weighting backwards in two
    # places. (1) freshness correlated ~0 with outcome (-0.01) despite v2
    # letting it swing score by up to 2x -- dropped as a score factor entirely
    # (BREAKOUT_RECENT_DAYS already handles freshness as a hard FILTER; this
    # is only about whether it should also drive ranking within that window --
    # it shouldn't). (2) volume_ratio, v2's dominant multiplicative factor, was
    # one of the WEAKEST correlates (+0.07) -- de-emphasized (cap lowered,
    # 3.0 -> 1.5). RS-vs-SPY correlated meaningfully more than RS-vs-own-past
    # (+0.19 vs +0.15) -- weighted higher instead of an unweighted sum.
    # growth_score (volatility) was already well-justified (+0.19) and is
    # unchanged. NOTE: base_range_pct showed the single strongest raw
    # correlation (+0.23) but is mechanically linked to volatility_pct (both
    # scale with vol_scale) -- risk of double-counting the same underlying
    # signal rather than adding an independent one, so deliberately NOT added
    # here; would need a proper multi-variate check first.
    volume_score = min(recent_vol_peak / avg_vol_50 / VOLUME_MULT, 1.5)
    rs_score = 1.5 * max(ret_4w - (spy_ret_4w or 0.0), 0.0) + max(ret_4w - ret_prior_4w, 0.0)
    growth_score = min(volatility_pct / REFERENCE_VOLATILITY_PCT, 3.0)
    score = volume_score * (1 + rs_score) * growth_score

    asof = closes.index[-1]
    return {
        "ticker": sym,
        "asof": str(asof.date()) if hasattr(asof, "date") else str(asof),
        "price": round(price_now, 2),
        "pivot": round(pivot, 2),
        "days_since_breakout": int(days_since),
        "extension_pct": round(extension * 100, 2),
        "base_range_pct": round((base_hi - base_lo) / base_lo * 100, 2),
        "volatility_pct": round(volatility_pct * 100, 2),
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
