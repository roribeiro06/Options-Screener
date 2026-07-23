"""
Wheel Screener -- Streamlit web app.
Shows your default watchlist; visitors can add tickers/holdings and re-run.
Reuses the engine in wheel_screener.py. Deploy free on Streamlit Community Cloud.
"""
import os
import pandas as pd
import streamlit as st

# Load the Tradier token from Streamlit "Secrets" into the environment
# BEFORE importing the engine, so it can reach the API.
try:
    if "TRADIER_TOKEN" in st.secrets:
        os.environ["TRADIER_TOKEN"] = str(st.secrets["TRADIER_TOKEN"])
    if "TRADIER_BASE" in st.secrets:
        os.environ["TRADIER_BASE"] = str(st.secrets["TRADIER_BASE"])
except Exception:
    pass

import wheel_screener as ws

st.set_page_config(page_title="Wheel Screener", layout="wide")
st.title("Wheel Screener")
st.caption("Cash-secured puts & covered calls. Live quotes from Tradier. "
           "Educational only - NOT financial advice. Verify every quote in your broker before trading.")
if not os.environ.get("TRADIER_TOKEN"):
    st.error("No Tradier token found. Add TRADIER_TOKEN in the app's Settings -> Secrets, then Rerun.")

# --- Auto-refresh every 30 min while the tab is open, during US market hours ---
from datetime import datetime, time as _clock
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None


def market_open_now():
    if _ET is None:
        return False
    now = datetime.now(_ET)
    if now.weekday() >= 5:            # Saturday / Sunday
        return False
    return _clock(9, 30) <= now.time() <= _clock(16, 0)


if market_open_now():
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=30 * 60 * 1000, key="mkt_refresh")  # 30 minutes
        st.caption("Auto-refreshing every 30 min while open (US market hours, ET).")
    except Exception:
        pass
else:
    st.caption("Market closed - data holds until 9:30am ET, when auto-refresh resumes.")


def parse_puts(txt):
    return [s.strip().upper() for s in txt.replace("\n", ",").split(",") if s.strip()]


def parse_holdings(txt):
    out = []
    for line in txt.splitlines():
        parts = [p.strip() for p in line.replace("\t", ",").split(",") if p.strip()]
        if len(parts) >= 2:
            try:
                out.append((parts[0].upper(), float(parts[1].replace("$", "").replace(",", ""))))
            except ValueError:
                pass
    return tuple(out)


@st.cache_data(ttl=600, show_spinner=True)
def scan_puts(tickers):
    rows, errs = [], []
    for t in tickers:
        try:
            passers, _ = ws.screen_puts(t)
            rows += passers
        except Exception as e:
            errs.append(f"{t}: {e}")
    return ws._df(rows, ws.PUT_COLS), errs


@st.cache_data(ttl=600, show_spinner=True)
def scan_calls(holdings):
    rows, errs = [], []
    for t, cost in holdings:
        try:
            passers, _ = ws.screen_calls(t, cost)
            rows += passers
        except Exception as e:
            errs.append(f"{t}: {e}")
    return ws._df(rows, ws.CALL_COLS), errs


@st.cache_data(ttl=600)
def cached_vix():
    return ws.get_vix()


with st.sidebar:
    st.header("Watchlist")
    puts_txt = st.text_area("Put tickers (comma-separated)",
                            ", ".join(ws.PUT_TICKERS), height=90)
    holds_txt = st.text_area("Covered-call holdings  (one per line:  TICKER, avg cost)",
                             "\n".join(f"{k}, {v}" for k, v in ws.HOLDINGS.items()), height=160)
    st.markdown("---")
    st.caption("Rules: ~70% POP (0.30 delta) - yield >= 25% - OTM% - "
               "DTE 7-90 - no earnings in window. Edit these in wheel_screener.py.")
    if st.button("Refresh data (clear cache)"):
        st.cache_data.clear()

puts = parse_puts(puts_txt)
holds = parse_holdings(holds_txt)

vix = cached_vix()
if vix is not None:
    st.info(f"**VIX {vix}** - {ws.vix_regime(vix)}")
else:
    st.info("VIX unavailable right now.")

dp, ep = scan_puts(tuple(puts))
dc, ec = scan_calls(holds)

st.subheader("Cash-Secured Puts")
if len(dp):
    st.dataframe(ws._fmt(dp), hide_index=True, use_container_width=True)
    st.download_button("Download puts (CSV)", dp.to_csv(index=False),
                       "puts.csv", "text/csv")
else:
    st.write("None qualify right now - nothing pays enough; stay in T-bills.")
if ep:
    st.caption("Skipped: " + " | ".join(ep))

st.subheader("Covered Calls  (strike above your cost)")
if len(dc):
    st.dataframe(ws._fmt(dc), hide_index=True, use_container_width=True)
    st.download_button("Download calls (CSV)", dc.to_csv(index=False),
                       "calls.csv", "text/csv")
else:
    st.write("None qualify right now - no call above your cost pays enough.")
if ec:
    st.caption("Skipped: " + " | ".join(ec))

st.markdown("---")
st.caption("Delta_% = chance of keeping the premium (1 - delta). YieldNeeded_% = 25% - OTM%. "
           "Prices are live but unofficial (Yahoo); always confirm in your broker. Not financial advice.")
