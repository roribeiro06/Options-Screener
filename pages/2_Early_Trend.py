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
    "screener would already show it up sharply. Educational only, NOT financial advice -- "
    "no rules-based screen can guarantee catching a trend before it runs. Run "
    "`backtest_early_trend.py` locally to see how these exact rules performed historically "
    "before trusting the table below."
)
if not os.environ.get("TRADIER_TOKEN"):
    st.error("No Tradier token found. Add TRADIER_TOKEN in the app's Settings -> Secrets, then Rerun.")

with st.expander("Legend - how to read this table", expanded=False):
    st.markdown(
        "- **Pivot** -- the prior base's high; the level price had to close above to count as a "
        "breakout at all. The pivot must also be close to a genuine 52-week high, not just a "
        "high relative to a shorter window -- otherwise a stock recovering toward an OLDER high "
        "(e.g. clawing back after an earnings-gap crash) could look like a fresh breakout when "
        "it's really just returning to a level it's already failed at before.\n"
        "- **Days Since Breakout** -- trading days since price first closed above the pivot. "
        "Lower = fresher. Only breakouts within the sidebar's \"Breakout must be within\" window "
        "show up here at all -- older ones age out.\n"
        "- **Above Pivot %** -- how far price has already run past the pivot. This is the main "
        "\"don't chase it\" gauge: capped by \"Max % above pivot\" in the sidebar, so nothing "
        "already far past its breakout shows up.\n"
        "- **Base Range %** -- how tight the prior consolidation was (high-low range as a % of "
        "the low) before it broke out. Tighter/lower generally reads as a more coiled, "
        "higher-quality base, not a random bounce.\n"
        "- **Volume x Avg** -- the breakout window's peak volume vs. the 50-day average. Must "
        "clear \"Breakout volume >= X times 50-day avg\" -- confirms real buying interest, not "
        "just drift on light volume.\n"
        "- **4wk Return % / Prior 4wk %** -- the acceleration check: the last ~4 weeks' return "
        "must beat the 4 weeks before that. This is what separates *speeding up* from merely "
        "*being up* -- a plain momentum screener only checks the second one.\n"
        "- **SPY 4wk %** -- the S&P 500 ETF's own 4-week return, for comparison. The stock's 4wk "
        "return must beat this too -- real relative strength vs. the market, not just vs. its own "
        "past.\n"
        "- **3mo Return % / 6mo Return %** -- trailing returns over both windows, shown for "
        "context. These are what \"Exclude if already up more than % (3mo)\" and \"...(6mo)\" cap "
        "-- the main filters against catching a name that's already had its spike. The 3mo cap "
        "alone can miss a name whose LAST few months look fine but that already ran hard before "
        "that; the 6mo cap catches that case.\n"
        "- **Call OI Skew** -- from the nearest live options-chain expiration: call open interest "
        "as a % of total (call+put) open interest. Above 50% means the options market is "
        "positioned more toward calls than puts -- a secondary confirmation only, never a hard "
        "filter (sandbox chain data can be thin for less-liquid names).\n"
        "- **ATM IV Skew (C-P)** -- the at-the-money call's implied vol minus the at-the-money "
        "put's, same chain. Positive means calls are pricing relatively richer than puts near the "
        "money -- another options-positioning tell, same caveat as above.\n"
        "- **Score** -- the ranking number: combines breakout freshness, volume confirmation, how "
        "much the RS acceleration exceeds zero, and how close price still is to the pivot, then "
        "nudged by the options tilt if available. Higher = a better setup by these specific rules. "
        "Only meaningful for ranking *within* one scan -- it isn't a probability or a return "
        "forecast, and isn't comparable across different criteria settings."
    )


def apply_criteria(c):
    (et.BASE_WEEKS, et.BASE_DAYS, et.BASE_RANGE_PCT, et.BREAKOUT_RECENT_DAYS,
     et.BREAKOUT_BAND_PCT, et.VOLUME_MULT, et.EXTENSION_CAP_PCT, et.EXTENSION_CAP_PCT_LONG,
     et.MIN_MARKET_CAP, et.CANDIDATE_POOL, et.MOVER_POOL) = c


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
        vol_mult = st.number_input("Breakout volume >= X times 50-day avg", 1.0, 5.0, et.VOLUME_MULT, 0.1)
        ext_cap = st.number_input("Exclude if already up more than %% (3mo)", 5, 200,
                                  int(et.EXTENSION_CAP_PCT * 100))
        ext_cap_long = st.number_input("Exclude if already up more than %% (6mo)", 5, 300,
                                       int(et.EXTENSION_CAP_PCT_LONG * 100))
        min_cap = st.number_input("Min market cap ($B)", 0, 500, int(et.MIN_MARKET_CAP / 1_000_000_000))
        pool = st.number_input("Candidate pool size (volume surge)", 20, 500, et.CANDIDATE_POOL)
        mover_pool = st.number_input("Candidate pool size (today's movers)", 10, 300, et.MOVER_POOL)

    st.markdown("---")
    if st.button("Refresh data (clear cache)"):
        st.cache_data.clear()

crit = (int(base_weeks), int(base_weeks) * 5, base_range / 100.0, int(breakout_recent),
        breakout_band / 100.0, float(vol_mult), ext_cap / 100.0, ext_cap_long / 100.0,
        int(min_cap) * 1_000_000_000, int(pool), int(mover_pool))

DISPLAY_COLS = {
    "ticker": "Ticker", "price": "Price", "pivot": "Pivot", "days_since_breakout": "Days Since Breakout",
    "extension_pct": "Above Pivot %", "base_range_pct": "Base Range %", "volume_ratio": "Volume x Avg",
    "ret_4w_pct": "4wk Return %", "ret_prior_4w_pct": "Prior 4wk %", "spy_ret_4w_pct": "SPY 4wk %",
    "ret_3mo_pct": "3mo Return %", "ret_6mo_pct": "6mo Return %", "oi_skew": "Call OI Skew",
    "iv_skew": "ATM IV Skew (C-P)", "score": "Score",
}

try:
    results = scan_early_trend(crit)
    if results:
        df = pd.DataFrame(results)[list(DISPLAY_COLS.keys())].rename(columns=DISPLAY_COLS)
        st.caption(f"Scanned {dt.date.today().isoformat()} -- {len(results)} tickers qualify, ranked by Score.")
        st.dataframe(df, hide_index=True, use_container_width=True)
        st.download_button("Download (CSV)", df.to_csv(index=False), "early_trend.csv", "text/csv")
    else:
        st.write("No tickers currently qualify -- try loosening the criteria in the sidebar.")
except Exception as e:
    st.error(f"Scan failed: {e}")
