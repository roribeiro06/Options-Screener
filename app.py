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

# --- Auto-refresh aligned to the market clock (9:30, 10:00, ... 4:00 ET) ---
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None


def next_refresh_ms():
    """Milliseconds until the next :00/:30 mark within market hours, else None.
    Anchored to the 9:30am open, independent of when you opened the page."""
    if _ET is None:
        return None
    now = datetime.now(_ET)
    if now.weekday() >= 5:                        # weekend
        return None
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now < open_t:
        nxt = open_t                              # before the bell -> first update at 9:30
    elif now <= close_t:
        if now.minute < 30:
            nxt = now.replace(minute=30, second=0, microsecond=0)
        else:
            nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if nxt > close_t:
            nxt = close_t
    else:
        return None                               # after the close
    if nxt <= now:
        nxt = now + timedelta(seconds=1)
    return max(int((nxt - now).total_seconds() * 1000), 1000)


_ms = next_refresh_ms()
if _ms is not None:
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=_ms, key="mkt_refresh")
        st.caption("Auto-updates on the half hour (9:30, 10:00 ... 4:00 ET) while open. "
                   "Use 'Refresh data' anytime for an on-demand update.")
    except Exception:
        pass
else:
    st.caption("Market closed - auto-updates resume at 9:30am ET, then every half hour.")


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


def apply_criteria(c):
    """Push the sidebar criteria into the engine's module globals."""
    (ws.MIN_ANN_YIELD, ws.POP_MIN, ws.POP_MAX, ws.DTE_MIN, ws.DTE_MAX,
     ws.DTE_SHORT_CUTOFF, ws.YIELD_OVER_IV_SHORT, ws.YIELD_OVER_IV_LONG,
     ws.OTM_MIN, ws.OTM_MAX) = c


@st.cache_data(ttl=600, show_spinner=True)
def scan_puts(tickers, crit):
    apply_criteria(crit)
    rows, errs = [], []
    for t in tickers:
        try:
            passers, _ = ws.screen_puts(t)
            rows += passers
        except Exception as e:
            errs.append(f"{t}: {e}")
    return ws._df(rows, ws.PUT_COLS), errs


@st.cache_data(ttl=600, show_spinner=True)
def scan_calls(holdings, crit):
    apply_criteria(crit)
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

    with st.expander("Criteria (adjustable)", expanded=False):
        def _d(name, default):            # safe read (works even if engine file is older)
            return getattr(ws, name, default)
        min_yield = st.number_input("Min annualized yield %", 0, 500,
                                    int(_d("MIN_ANN_YIELD", 0.25) * 100), 5)
        otm_min = st.number_input("OTM min %", 0, 100, int(_d("OTM_MIN", 0.0) * 100))
        otm_max = st.number_input("OTM max %", 0, 100, int(_d("OTM_MAX", 1.0) * 100))
        dte_min = st.number_input("DTE min", 0, 365, int(_d("DTE_MIN", 7)))
        dte_max = st.number_input("DTE max", 0, 365, int(_d("DTE_MAX", 90)))
        dte_cut = st.number_input("Short-DTE cutoff (days)", 1, 365, int(_d("DTE_SHORT_CUTOFF", 21)))
        yiv_s = st.number_input("<= cutoff: yield must beat this % of IV", 0, 300,
                                int(_d("YIELD_OVER_IV_SHORT", 1.0) * 100), 5)
        yiv_l = st.number_input("> cutoff: yield must beat this % of IV", 0, 300,
                                int(_d("YIELD_OVER_IV_LONG", 0.7) * 100), 5)
        pop_min = st.number_input("POP min %", 0, 100, int(_d("POP_MIN", 0.65) * 100))
        pop_max = st.number_input("POP max %", 0, 100, int(_d("POP_MAX", 0.75) * 100))

    st.markdown("---")
    st.caption("Adjust thresholds in 'Criteria (adjustable)' above; changes re-run automatically. "
               "No earnings in window (always on).")
    if st.button("Refresh data (clear cache)"):
        st.cache_data.clear()

puts = parse_puts(puts_txt)
holds = parse_holdings(holds_txt)

CRITERIA = (min_yield / 100, pop_min / 100, pop_max / 100, int(dte_min), int(dte_max),
            int(dte_cut), yiv_s / 100, yiv_l / 100, otm_min / 100, otm_max / 100)
apply_criteria(CRITERIA)

vix = cached_vix()
if vix is not None:
    st.info(f"**VIX {vix}** - {ws.vix_regime(vix)}")
else:
    st.info("VIX unavailable right now.")

dp, ep = scan_puts(tuple(puts), CRITERIA)
dc, ec = scan_calls(holds, CRITERIA)

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
