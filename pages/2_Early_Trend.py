"""
Early Trend Screener -- Streamlit page, separate from the wheel screener
(app.py). Looks for stocks breaking OUT of a base on volume with relative-
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
# app.py having already run in the same session.
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


def apply_criteria(c):
    (et.BASE_WEEKS, et.BASE_DAYS, et.BASE_RANGE_PCT, et.BREAKOUT_RECENT_DAYS,
     et.BREAKOUT_BAND_PCT, et.VOLUME_MULT, et.EXTENSION_CAP_PCT, et.MIN_MARKET_CAP,
     et.CANDIDATE_POOL, et.MOVER_POOL) = c


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
        min_cap = st.number_input("Min market cap ($B)", 0, 500, int(et.MIN_MARKET_CAP / 1_000_000_000))
        pool = st.number_input("Candidate pool size (volume surge)", 20, 500, et.CANDIDATE_POOL)
        mover_pool = st.number_input("Candidate pool size (today's movers)", 10, 300, et.MOVER_POOL)

    st.markdown("---")
    if st.button("Refresh data (clear cache)"):
        st.cache_data.clear()

crit = (int(base_weeks), int(base_weeks) * 5, base_range / 100.0, int(breakout_recent),
        breakout_band / 100.0, float(vol_mult), ext_cap / 100.0, int(min_cap) * 1_000_000_000,
        int(pool), int(mover_pool))

DISPLAY_COLS = {
    "ticker": "Ticker", "price": "Price", "pivot": "Pivot", "days_since_breakout": "Days Since Breakout",
    "extension_pct": "Above Pivot %", "base_range_pct": "Base Range %", "volume_ratio": "Volume x Avg",
    "ret_4w_pct": "4wk Return %", "ret_prior_4w_pct": "Prior 4wk %", "spy_ret_4w_pct": "SPY 4wk %",
    "ret_3mo_pct": "3mo Return %", "oi_skew": "Call OI Skew", "iv_skew": "ATM IV Skew (C-P)", "score": "Score",
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
