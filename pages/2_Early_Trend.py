"""
Early Trend Screener -- Streamlit page, separate from the wheel screener
(1_Options_Screener.py). Looks for stocks breaking OUT of a base on volume with relative-
strength acceleration, while capping how far price is already past that
breakout -- the goal is catching a trend early, not after a momentum
screener would already show it up 50-100%+. See early_trend.py for the full
rules and backtest_early_trend.py for validating them against history before
trusting live output here.
"""
import os
import datetime as dt

import pandas as pd
import streamlit as st

# Load the Tradier token from Streamlit "Secrets" into the environment BEFORE
# importing anything that touches it -- Streamlit's classic multi-page
# mechanism re-runs each page script independently, so this can't rely on
# 1_Options_Screener.py having already run in the same session.
try:
    if "TRADIER_TOKEN" in st.secrets:
        os.environ["TRADIER_TOKEN"] = str(st.secrets["TRADIER_TOKEN"])
    if "TRADIER_BASE" in st.secrets:
        os.environ["TRADIER_BASE"] = str(st.secrets["TRADIER_BASE"])
except Exception:
    pass

import early_trend as et

st.set_page_config(page_title="Early Trend Screener", layout="wide")
st.title("Early Trend Screener")
st.caption(
    "Flags stocks breaking OUT of a multi-week base on volume, with relative-strength "
    "ACCELERATION vs their own recent past and vs SPY -- and caps how far price is already "
    "past that breakout, to try to catch a trend early rather than after a plain momentum "
    "screener would already show it up sharply. Tuned for GROWTH/short-term movers, not "
    "long-term/defensive names -- a volatility floor excludes calm names outright, and the "
    "base-range/extension/breakout-band thresholds scale up for a more volatile stock instead "
    "of applying one fixed number to every name. Educational only, NOT financial advice -- "
    "no rules-based screen can guarantee catching a trend before it runs. Run "
    "`backtest_early_trend.py` locally to see how these exact rules performed historically "
    "before trusting the table below."
)
if not os.environ.get("TRADIER_TOKEN"):
    st.error("No Tradier token found. Add TRADIER_TOKEN in the app's Settings -> Secrets, then Rerun.")

with st.expander("Legend - how to read this table", expanded=False):
    st.markdown(
        "- **Score** -- the ranking number: combines volume confirmation, how much the RS "
        "acceleration exceeds zero (weighted higher vs. SPY than vs. the stock's own past), the "
        "stock's own volatility (higher = more of a growth-style mover, scored higher), and "
        "distance from the 52wk high (farther = scored higher, see below), then nudged by the "
        "options tilt if available. Higher = a better setup by these specific rules. Only "
        "meaningful for ranking *within* one scan -- it isn't a probability or a return "
        "forecast, and isn't comparable across different criteria settings. (This formula has "
        "been revised more than once after backtest evidence directly contradicted an intuitive "
        "assumption: a v1 factor rewarding being barely past the pivot was actually INVERTED -- "
        "worse-scored flags outperformed better-scored ones; freshness was dropped after showing "
        "~zero correlation with outcome; and when % of 52wk High first replaced a hard pass/fail "
        "cutoff, the FIRST version scored proximity-to-high as a positive -- a full backtest run "
        "showed that was backwards too. Re-check this with backtest_early_trend.py before "
        "trusting Score too heavily.)\n"
        "- **Above Pivot %** -- how far price has already run past the pivot (the prior base's "
        "high, the level it had to close above to count as a breakout at all). This is the main "
        "\"don't chase it\" gauge: capped by \"Max % above pivot\" in the sidebar (plus a small "
        "fixed 6-point grace margin found by backtest to cut real near-misses without letting "
        "genuinely extended names back in), so nothing already far past its breakout shows up.\n"
        "- **Days Since Breakout** -- trading days since price first closed above the pivot. "
        "Lower = fresher. Only breakouts within the sidebar's \"Breakout must be within\" window "
        "show up here at all -- older ones age out.\n"
        "- **Base Range %** -- how tight the prior consolidation was (high-low range as a % of "
        "the low) before it broke out. Tighter/lower generally reads as a more coiled, "
        "higher-quality base, not a random bounce. The allowed max scales up for a more "
        "volatile stock (see Volatility % below) instead of one fixed number for every name.\n"
        "- **Volatility %** -- the stock's own annualized realized volatility (trailing 20 "
        "days). Must clear \"Min volatility %\" in the sidebar to appear at all -- this is what "
        "excludes calm, defensive names (utilities, insurers, staples) and keeps this a "
        "growth/short-term screen rather than a slow-compounder one. It also scales up the base "
        "range, extension caps, and breakout band for names more volatile than a 30% baseline, "
        "and feeds directly into Score above.\n"
        "- **% of 52wk High** -- how close price is to its own trailing-year high (100% = a "
        "fresh year high right now). Not a hard cutoff, and NOT what you'd assume -- a "
        "backtest run showed names farther below their own high actually did BETTER going "
        "forward than names already sitting near it (more room to re-rate, not yet as widely "
        "recognized), so Score rewards distance from the high here, not closeness to it. "
        "EXCEPTION: this penalty is skipped entirely for a name whose move is SUSTAINED (see "
        "Notes below) -- a persistently re-rating name is basically always near its own high, "
        "and a full-universe check found that population did roughly 2x better than an ordinary "
        "near-high flag (median peak gain +58% vs +28.6%, real examples: MU +251%, STX +298%, "
        "TNGX +246%), so penalizing it the same way as a name that's simply topping out was "
        "actively wrong, not just imprecise.\n"
        "- **Notes** -- a plain-English readout of whatever else is actually driving that row's "
        "Score: the 4-week acceleration vs. its own prior 4 weeks and vs. SPY, volume "
        "confirmation (no longer a hard requirement, so light volume shows up here rather than "
        "as a rejection), whether the move is SUSTAINED -- spread out over months rather than a "
        "single spike, which also exempts it from the % of 52wk High penalty above -- and any "
        "options-positioning tilt toward calls when the chain data is usable. Built only from "
        "numbers the scan already computes -- not a probability or a forecast of anything. The "
        "full detail behind every one of these signals (SMA trend, raw volume ratio, trailing "
        "3mo/6mo returns, options OI/IV skew) lives in `early_trend.py`'s own scan output even "
        "where it's not broken out as a separate table column here."
    )


def apply_criteria(c):
    (et.BASE_WEEKS, et.BASE_DAYS, et.BASE_RANGE_PCT, et.BREAKOUT_RECENT_DAYS,
     et.BREAKOUT_BAND_PCT, et.VOLUME_MULT, et.EXTENSION_CAP_PCT, et.EXTENSION_CAP_PCT_LONG,
     et.MIN_MARKET_CAP, et.CANDIDATE_POOL, et.MOVER_POOL, et.MIN_VOLATILITY_PCT) = c


def _build_note(r):
    """Plain-English summary of what's actually driving a ticker's ranking, built only
    from the same numbers already shown in the table -- NOT a probability or prediction,
    just an explanation of the specific signals behind that row's Score. Prioritizes the
    raw signals backtest report 9 has repeatedly shown carry the most real correlation
    with outcome (base tightness, RS acceleration/vs-SPY, volatility, distance from the
    52wk high) over the weaker ones (volume), so the story matches what's actually been
    validated to matter rather than reciting every column."""
    parts = []

    spy4w = r.get("spy_ret_4w_pct")
    if spy4w is not None:
        vs_spy = r["ret_4w_pct"] - spy4w
        parts.append(
            f"Up {r['ret_4w_pct']:.0f}% over the last 4 weeks (accelerating from "
            f"{r['ret_prior_4w_pct']:.0f}% the 4 weeks before) and beating SPY's {spy4w:.0f}% "
            f"by {vs_spy:.0f} points"
        )
    else:
        parts.append(
            f"Up {r['ret_4w_pct']:.0f}% over the last 4 weeks, accelerating from "
            f"{r['ret_prior_4w_pct']:.0f}% the 4 weeks before"
        )

    br = r["base_range_pct"]
    if br <= 20:
        parts.append(f"broke out of a tightly coiled {br:.0f}% base")
    elif br <= 40:
        parts.append(f"broke out of a {br:.0f}% base")
    else:
        parts.append(f"broke out of a wider ({br:.0f}%) base -- typical for how volatile this name runs")

    wh = r.get("window_high_pct")
    if wh is not None:
        if wh < 70:
            parts.append(f"still {100 - wh:.0f}% below its own 52-week high, with real room left to re-rate")
        elif wh >= 95:
            parts.append("already sitting right at a fresh 52-week high")

    vol_pct = r["volatility_pct"]
    if vol_pct >= 60:
        parts.append(f"a genuinely explosive mover ({vol_pct:.0f}% annualized volatility)")
    elif vol_pct < 35:
        parts.append(f"calmer than most names that clear this screen ({vol_pct:.0f}% volatility)")

    vr = r["volume_ratio"]
    if vr >= 1.5:
        parts.append(f"volume ran {vr:.1f}x its 50-day average on the breakout -- real confirmation")
    elif vr < 1.0:
        parts.append(f"volume hasn't confirmed yet (only {vr:.1f}x average) -- moving on light "
                     f"interest, a softer sign since this stopped being a hard requirement")

    if r.get("sustained_move"):
        ret6mo = r.get("ret_6mo_pct")
        gain_note = f"up {ret6mo:.0f}% over 6 months" if ret6mo is not None else "up sharply"
        parts.append(f"already {gain_note}, but no single week explains much of that -- a gradual, "
                     f"persistent re-rating rather than a spike, which is why it still qualifies and "
                     f"isn't marked down for sitting near its high the way a typical flag would be")

    oi_skew = r.get("oi_skew")
    if oi_skew is not None and oi_skew > 0.55:
        parts.append(f"options positioning leans toward calls ({oi_skew * 100:.0f}% of open interest)")

    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:]


@st.cache_data(ttl=600, show_spinner="Scanning for early-stage breakouts (candidate pool, then price history per candidate)...")
def scan_early_trend(crit):
    apply_criteria(crit)
    return et.run_scan()


with st.sidebar:
    st.header("Early Trend criteria")
    with st.expander("Breakout / base criteria", expanded=False):
        base_weeks = st.number_input("Base length (weeks)", 4, 26, et.BASE_WEEKS)
        base_range = st.number_input("Base range max %", 5, 60, int(et.BASE_RANGE_PCT * 100))
        breakout_recent = st.number_input("Breakout must be within (trading days)", 1, 30, et.BREAKOUT_RECENT_DAYS)
        breakout_band = st.number_input("Max %% above pivot (still counts as fresh)", 1, 50,
                                        int(et.BREAKOUT_BAND_PCT * 100))
        vol_mult = st.number_input("Breakout volume target (x 50-day avg, scores higher above this, not a cutoff)", 1.0, 5.0, et.VOLUME_MULT, 0.1)
        ext_cap = st.number_input("Exclude if already up more than %% (3mo)", 5, 200,
                                  int(et.EXTENSION_CAP_PCT * 100))
        ext_cap_long = st.number_input("Exclude if already up more than %% (6mo)", 5, 300,
                                       int(et.EXTENSION_CAP_PCT_LONG * 100))
        min_cap = st.number_input("Min market cap ($B)", 0, 500, int(et.MIN_MARKET_CAP / 1_000_000_000))
        pool = st.number_input("Candidate pool size (volume surge)", 20, 500, et.CANDIDATE_POOL)
        mover_pool = st.number_input("Candidate pool size (today's movers)", 10, 300, et.MOVER_POOL)
        min_vol = st.number_input("Min volatility % (annualized, excludes defensive names)", 0, 200,
                                  int(et.MIN_VOLATILITY_PCT * 100))
        st.caption("Base range / extension / breakout-band above scale UP automatically for a "
                   "stock more volatile than 30% annualized -- the numbers you set are the "
                   "baseline for a 'typical' stock, not a hard cap for every stock.")

    st.markdown("---")
    top_n = st.number_input(
        "Show top N by Score", 5, 200, 5,
        help="The rules find more real setups now than they used to (a backtest-validated "
             "accuracy improvement, not a bug) -- this just limits the table to the "
             "highest-ranked names instead of loosening the rules that found them."
    )
    if st.button("Refresh data (clear cache)"):
        st.cache_data.clear()

crit = (int(base_weeks), int(base_weeks) * 5, base_range / 100.0, int(breakout_recent),
        breakout_band / 100.0, float(vol_mult), ext_cap / 100.0, ext_cap_long / 100.0,
        int(min_cap) * 1_000_000_000, int(pool), int(mover_pool), min_vol / 100.0)

DISPLAY_COLS = {
    "ticker": "Ticker", "price": "Price", "score": "Score", "extension_pct": "Above Pivot %",
    "days_since_breakout": "Days Since Breakout", "base_range_pct": "Base Range %",
    "volatility_pct": "Volatility %", "window_high_pct": "% of 52wk High",
}
# Everything else the raw scan computes (pivot, SMA trend, volume ratio, 4wk/prior-4wk/SPY
# returns, 3mo/6mo returns, options tilt) is either narrated in Notes when it's actually
# notable for that row, or -- SMA Trend % only -- dropped outright: backtest report 9 found
# it barely correlated with outcome either way, so it was pure clutter with no signal behind
# it. Trimmed from 17 raw columns to this + Notes so the table reads at a glance instead of
# requiring a legend lookup for every header.

try:
    results = scan_early_trend(crit)
    if results:
        shown = results[:int(top_n)]
        for r in shown:
            r["notes"] = _build_note(r)
        df = pd.DataFrame(shown)[list(DISPLAY_COLS.keys()) + ["notes"]] \
            .rename(columns={**DISPLAY_COLS, "notes": "Notes"})
        st.caption(f"Scanned {dt.date.today().isoformat()} -- {len(results)} tickers qualify, "
                   f"showing top {len(shown)} by Score.")
        st.dataframe(
            df, hide_index=True, use_container_width=True,
            column_config={
                "Notes": st.column_config.TextColumn(width="large"),
                "Price": st.column_config.NumberColumn(format="$%.2f"),
                "Score": st.column_config.NumberColumn(format="%.2f"),
                "Above Pivot %": st.column_config.NumberColumn(format="%.1f%%"),
                "Base Range %": st.column_config.NumberColumn(format="%.0f%%"),
                "Volatility %": st.column_config.NumberColumn(format="%.0f%%"),
                "% of 52wk High": st.column_config.NumberColumn(format="%.0f%%"),
            },
        )
        st.caption(
            "Notes describe the specific signals behind that row's Score (built only from "
            "the same numbers in the table) -- not a probability or a forecast of anything."
        )
        st.download_button("Download (CSV)", df.to_csv(index=False), "early_trend.csv", "text/csv")
    else:
        st.write("No tickers currently qualify -- try loosening the criteria in the sidebar.")
except Exception as e:
    st.error(f"Scan failed: {e}")
