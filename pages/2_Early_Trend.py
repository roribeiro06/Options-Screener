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

try:
    results = scan_early_trend(crit)
    if results:
        shown = results[:int(top_n)]
        for r in shown:
            r["notes"] = et.build_note(r)
        df = pd.DataFrame(shown)[list(et.DISPLAY_COLS.keys()) + ["notes"]] \
            .rename(columns={**et.DISPLAY_COLS, "notes": "Notes"})
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
