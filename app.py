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
import spreads as sp

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
     ws.OTM_MAX, ws.USE_YIELD_OVER_IV, ws.USE_TIERED_YIELD) = c


def apply_spread_criteria(sc):
    """Independent criteria for the multi-leg (spread) engine."""
    (sp.ROR_ANN_MIN, sp.SPREAD_POP_MIN,
     sp.SPREAD_DTE_MIN, sp.SPREAD_DTE_MAX, sp.SPREAD_WIDTH_PCT) = sc


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
    # rank within each ticker by composite Score (AnnYield * POP^a * (365/DTE)^b)
    return ws._df(rows, ws.PUT_COLS, sort_by=("Ticker", "Score"), asc=(True, False)), errs


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
    # rank within each ticker by composite Score, same as puts
    return ws._df(rows, ws.CALL_COLS, sort_by=("Ticker", "Score"), asc=(True, False)), errs


@st.cache_data(ttl=600, show_spinner=True)
def scan_spreads(tickers, sc):
    apply_spread_criteria(sc)
    rows, errs = [], []
    for t in tickers:
        try:
            rows += sp.screen_spreads(t)
        except Exception as e:
            errs.append(f"{t}: {e}")
    return sp._df(rows), errs


@st.cache_data(ttl=600, show_spinner=True)
def scan_lookup(ticker, kind, smin, smax, estart, eend):
    rows = ws.lookup_contracts(ticker, kind, smin, smax, estart, eend)
    return ws._df(rows, ws.LOOKUP_COLS, sort_by=("Expiration", "Strike"), asc=(True, True))


@st.cache_data(ttl=600)
def cached_vix():
    return ws.get_vix()


@st.cache_data(ttl=600, show_spinner="Scanning the market for high-open-interest tickers...")
def scan_discover():
    """Live, same cadence as everything else (30-min auto-refresh / on-demand,
    ttl=600 like the other scan_* functions). Scans a broad non-watchlist
    universe rather than just ~20 tickers, so this is much slower than the
    other sections -- falls back to the last committed volume_leaders.json
    snapshot (from the daily GitHub Action) if the live scan errors."""
    import discover
    try:
        return discover.run_discovery(), None
    except Exception as e:
        d = ws.load_volume_leaders()
        if d:
            return d, str(e)
        raise


with st.sidebar:
    st.header("Watchlist")
    puts_txt = st.text_area("Put tickers (comma-separated)",
                            ", ".join(ws.PUT_TICKERS), height=90)
    holds_txt = st.text_area("Covered-call holdings  (one per line:  TICKER, avg cost)",
                             "\n".join(f"{k}, {v}" for k, v in ws.HOLDINGS.items()), height=160)

    with st.expander("Puts / Calls criteria", expanded=False):
        def _d(name, default):            # safe read (works even if engine file is older)
            return getattr(ws, name, default)
        min_yield = st.number_input("Min annualized yield %", 0, 500,
                                    int(_d("MIN_ANN_YIELD", 0.25) * 100), 5)
        use_tier = st.checkbox("Tiered OTM->yield  (>=5%:25%, >=10%:15%, >=15%:10%)",
                               value=bool(_d("USE_TIERED_YIELD", False)))
        otm_max = st.number_input("OTM max %", 0, 100, int(_d("OTM_MAX", 1.0) * 100))
        st.caption("OTM min is automatic (all strategies): 5% for SPY/QQQ/DIA, 10% for other tickers.")
        dte_min = st.number_input("DTE min", 0, 365, int(_d("DTE_MIN", 7)))
        dte_max = st.number_input("DTE max", 0, 365, int(_d("DTE_MAX", 90)))
        dte_cut = st.number_input("Short-DTE cutoff (days)", 1, 365, int(_d("DTE_SHORT_CUTOFF", 21)))
        yiv_s = st.number_input("<= cutoff: yield must beat this % of IV", 0, 300,
                                int(_d("YIELD_OVER_IV_SHORT", 1.0) * 100), 5)
        yiv_l = st.number_input("> cutoff: yield must beat this % of IV", 0, 300,
                                int(_d("YIELD_OVER_IV_LONG", 0.7) * 100), 5)
        use_yiv = st.checkbox("Apply the yield-vs-IV filter above",
                              value=bool(_d("USE_YIELD_OVER_IV", False)))
        pop_min = st.number_input("POP min %", 0, 100, int(_d("POP_MIN", 0.65) * 100))
        pop_max = st.number_input("POP max %", 0, 100, int(_d("POP_MAX", 0.75) * 100))

    with st.expander("Multi-leg (spreads) criteria", expanded=False):
        def _ds(name, default):
            return getattr(sp, name, default)
        s_min_ror = st.number_input("Min annualized ROR %", 0, 2000, int(_ds("ROR_ANN_MIN", 0.25) * 100), 5)
        s_pop_min = st.number_input("Spread POP min % (no upper cap)", 0, 100,
                                    int(_ds("SPREAD_POP_MIN", 0.70) * 100))
        s_dte_min = st.number_input("Spread DTE min", 0, 365, int(_ds("SPREAD_DTE_MIN", 7)))
        s_dte_max = st.number_input("Spread DTE max", 0, 365, int(_ds("SPREAD_DTE_MAX", 90)))
        s_width = st.number_input("Spread width %", 1, 50, int(_ds("SPREAD_WIDTH_PCT", 0.05) * 100))

    st.markdown("---")
    st.caption("Adjust thresholds in 'Criteria (adjustable)' above; changes re-run automatically. "
               "No earnings in window (always on).")
    if st.button("Refresh data (clear cache)"):
        st.cache_data.clear()

puts = parse_puts(puts_txt)
holds = parse_holdings(holds_txt)

CRITERIA = (min_yield / 100, pop_min / 100, pop_max / 100, int(dte_min), int(dte_max),
            int(dte_cut), yiv_s / 100, yiv_l / 100, otm_max / 100,
            bool(use_yiv), bool(use_tier))
apply_criteria(CRITERIA)
SPREAD_CRITERIA = (s_min_ror / 100, s_pop_min / 100,
                   int(s_dte_min), int(s_dte_max), s_width / 100)
apply_spread_criteria(SPREAD_CRITERIA)

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

st.subheader("Covered Calls")
if len(dc):
    st.dataframe(ws._fmt(dc), hide_index=True, use_container_width=True)
    st.download_button("Download calls (CSV)", dc.to_csv(index=False),
                       "calls.csv", "text/csv")
else:
    st.write("None qualify right now.")
if ec:
    st.caption("Skipped: " + " | ".join(ec))

st.markdown("---")
st.header("Contract Lookup")
st.caption("Look up ANY ticker's SELLABLE contracts in a strike/expiration range - out-of-the-money puts "
           "(cash-secured puts) or calls (covered calls, as if you held the shares) - even ones that don't pass "
           "the criteria above. Puts show strikes below the price, calls above it. Same stats (Score, yields, IV, liquidity).")
with st.form("lookup_form"):
    _c1, _c2, _c3, _c4 = st.columns([1.2, 1, 1, 1])
    lk_ticker = _c1.text_input("Ticker", "NVDA").strip().upper()
    lk_kind = _c2.radio("Type", ["put", "call"], horizontal=True)
    lk_smin = _c3.number_input("Strike min (0 = any)", min_value=0.0, value=0.0, step=1.0)
    lk_smax = _c4.number_input("Strike max (0 = any)", min_value=0.0, value=0.0, step=1.0)
    _d1, _d2 = st.columns(2)
    _today = datetime.now().date()
    lk_start = _d1.date_input("Expiration from", _today)
    lk_end = _d2.date_input("Expiration to", _today + timedelta(days=90))
    lk_go = st.form_submit_button("Search")

if lk_go and lk_ticker:
    try:
        _res = scan_lookup(lk_ticker, lk_kind,
                           lk_smin or None, lk_smax or None, lk_start, lk_end)
        if len(_res):
            _label = "cash-secured put" if lk_kind == "put" else "covered call"
            st.write(f"**{len(_res)} {_label} contracts** for {lk_ticker} ({lk_start} to {lk_end}).")
            st.dataframe(ws._fmt(_res), hide_index=True, use_container_width=True)
            st.download_button("Download lookup (CSV)", _res.to_csv(index=False),
                               f"{lk_ticker}_{lk_kind}_lookup.csv", "text/csv")
        else:
            st.write("No sellable contracts in that range (for puts try strikes below the price; for calls, above).")
    except Exception as _e:
        st.error(f"Lookup failed: {_e}")

st.markdown("---")
st.header("Multi-Leg Strategies  (70% POP)")
ds, es = scan_spreads(tuple(puts), SPREAD_CRITERIA)
_SPREAD_SECTIONS = [
    ("Put credit spread",  "Put Credit Spreads  (bullish, defined risk)"),
    ("Call credit spread", "Call Credit Spreads  (bearish, defined risk)"),
    ("Iron condor",        "Iron Condors  (neutral, defined risk)"),
]
for _key, _title in _SPREAD_SECTIONS:
    st.subheader(_title)
    _sub = ds[ds["Strategy"] == _key] if len(ds) else ds
    if len(_sub):
        _disp = _sub.drop(columns=["Strategy"])
        for _lc in ("Put Legs", "Call Legs"):     # hide the empty side for one-sided spreads
            if _lc in _disp.columns and (_disp[_lc].fillna("") == "").all():
                _disp = _disp.drop(columns=[_lc])
        st.dataframe(sp._fmt(_disp), hide_index=True, use_container_width=True)
        st.download_button(f"Download (CSV)", _sub.to_csv(index=False),
                           f"{_key.replace(' ', '_')}.csv", "text/csv", key=f"dl_{_key}")
    else:
        st.write("None qualify right now.")
if es:
    st.caption("Skipped: " + " | ".join(es))

st.markdown("---")
st.subheader("Discover: High-Open-Interest Contracts (outside your watchlist)")
st.caption("Live scan of a broad US-listed universe (not limited to the S&P 500) for the tickers "
           "carrying the heaviest options open interest right now, screened with the same criteria as "
           "above plus a higher open-interest floor (5,000, vs 1,000 elsewhere) -- so a name you didn't "
           "add to the watchlist can still surface if one of its most liquid contracts qualifies. Calls "
           "here are call credit spreads, not naked/covered calls -- these are tickers you don't hold "
           "shares of, so a spread caps the risk instead of leaving the upside uncovered. Same refresh "
           "cadence as the rest of the app (30-min auto-refresh / 'Refresh data' on demand) -- placed "
           "last on the page since it scans far more tickers and is slower than the sections above.")
try:
    _vl, _dfallback = scan_discover()
    if _dfallback:
        st.caption(f"Live scan failed ({_dfallback}) -- showing the last daily-Action snapshot instead.")
    if _vl:
        _leaders = _vl.get("leaders", [])
        if _leaders:
            _lead_txt = " | ".join(f"{l['ticker']} ({l['option_open_interest']:,} OI)" for l in _leaders)
            st.caption(f"Scanned {_vl.get('_meta', {}).get('built', '?')} - highest open interest: {_lead_txt}")

        st.markdown("**Puts**")
        _dp = ws._df(_vl.get("puts", []), ws.PUT_COLS, sort_by=("Ticker", "Score"), asc=(True, False))
        if len(_dp):
            st.dataframe(ws._fmt(_dp), hide_index=True, use_container_width=True)
            st.download_button("Download discovered puts (CSV)", _dp.to_csv(index=False),
                               "volume_leaders_puts.csv", "text/csv")
        else:
            st.write("None of today's highest-open-interest tickers currently qualify.")

        st.markdown("**Call Credit Spreads (defined-risk)**")
        _dc = sp._df(_vl.get("call_spreads", []))
        if len(_dc):
            _disp = _dc.drop(columns=["Strategy", "Put Legs"])
            st.dataframe(sp._fmt(_disp), hide_index=True, use_container_width=True)
            st.download_button("Download discovered call spreads (CSV)", _dc.to_csv(index=False),
                               "volume_leaders_call_spreads.csv", "text/csv")
        else:
            st.write("None of today's highest-open-interest tickers currently qualify.")
    else:
        st.write("Scan unavailable right now, and no fallback snapshot exists yet.")
except Exception as _e:
    st.caption(f"(discover section unavailable: {_e})")

st.markdown("---")
st.header("Legend - criteria in effect")
try:
    def _on(b):
        return "ON" if b else "off"
    _yiv = ""
    if ws.USE_YIELD_OVER_IV:
        _yiv = (f"  (<={ws.DTE_SHORT_CUTOFF}d: yield > {ws.YIELD_OVER_IV_SHORT:.0%} of IV; "
                f">{ws.DTE_SHORT_CUTOFF}d: yield > {ws.YIELD_OVER_IV_LONG:.0%} of IV)")
    st.markdown(f"""
**What the columns mean** - OTM_% = how far the strike is out-of-the-money; POP_% = chance of keeping the premium (about 1 - delta).
For puts, AnnYield = income on the cash you secure. For multi-leg: **Max Profit** = net credit received (the most you can make),
MaxLoss = width - credit, **ROR** = Max Profit / MaxLoss, and **AnnROR** = ROR annualized.
**AvgPremium** = the typical premium range this ticker has carried at that OTM%/DTE over the past year, estimated from realized
volatility (so it runs a touch low vs actual prices) - a "$low-$high" band. Puts use the put premium, covered calls the call premium,
and spreads the estimated net credit (short leg minus long leg). Live Premium/Max Profit above the band = richer than normal, below =
cheaper. Refreshed weekly; "-" means no history yet.

**Capital:** **# of contracts** = whole contracts needed to reach at least ${getattr(ws, "CASH_TARGET", 40000):,} of collateral
for puts/covered calls (strike x 100 / shares x 100), or ${getattr(sp, "SPREAD_CASH_TARGET", 25000):,} of max loss for
spreads (max loss x 100) -- spreads use a lower target since their risk per contract is capped/defined.
Premium, Max Profit, and MaxLoss all show $/share and, in parentheses, the total across that many contracts.
MaxLoss = strike - premium for puts (worst case if assigned and the stock goes to zero); cost basis - premium for covered
calls with a known cost basis ("-" without one, e.g. Contract Lookup); width - credit for spreads.
**Liquidity:** Spread_$ = the bid-ask spread in dollars per share (ask - bid); for multi-leg it's the combined round-trip bid-ask
across all legs. Lower = tighter, cheaper to trade. OpenInt = open interest (contracts outstanding); Volume = contracts traded today.
Higher OpenInt/Volume and a smaller Spread_$ mean easier fills and less slippage.
Contracts (and every spread leg) must have **open interest >= {getattr(ws, "MIN_OPEN_INTEREST", 0):,}** to appear.
**Value** = (AnnYield / IV) x sqrt(DTE/365) for single-leg, (AnnROR / IV) x sqrt(DTE/365) for spreads. Equivalently, period premium
yield divided by the expected move over the holding period (IV x sqrt(DTE/365)) - i.e. how much of the expected move you're paid.
Term-neutral, so short- and long-dated contracts are comparable. Higher = richer premium for the risk.

**Cash-Secured Puts & Covered Calls**
- Probability of profit (POP): {ws.POP_MIN:.0%} to {ws.POP_MAX:.0%}  (about 0.30 delta = 70% POP)
- Minimum annualized yield: {ws.MIN_ANN_YIELD:.0%} for stocks, {getattr(ws, "MIN_ANN_YIELD_INDEX", ws.MIN_ANN_YIELD):.0%} for broad indexes (SPY/QQQ/DIA, which also skip the per-share premium floors since they're lower risk)
- OTM floor: {ws.OTM_MIN_INDEX:.0%} for SPY/QQQ/DIA, {ws.OTM_MIN_OTHER:.0%} for all other tickers  (OTM max {ws.OTM_MAX:.0%})
- Days to expiration: {ws.DTE_MIN} to {ws.DTE_MAX}; never spans an earnings report
- **Puts only** extra filters: {("premium >= $%.0f/share; " % ws.PUT_MIN_PREMIUM) if ws.PUT_MIN_PREMIUM > 0 else ""}{("premium >= %.1f%% of strike; " % (getattr(ws, "PUT_MIN_PREMIUM_PCT", 0)*100)) if getattr(ws, "PUT_MIN_PREMIUM_PCT", 0) > 0 else ""}AnnYield >= {ws.PUT_MIN_YIELD_OVER_IV:.0%} of IV; OTM >= {ws.PUT_MIN_OTM_OVER_IV:.0%} of IV
- **Covered calls** extra filter: OTM >= {getattr(ws, "CALL_MIN_OTM_OVER_IV", 0):.0%} of IV (same volatility-scaled cushion as puts)
- **Score** (all strategies) = (AnnYield / IV^{getattr(ws, "SCORE_IV_EXP", 1.0):g}) x POP^{getattr(ws, "SCORE_POP_EXP", 1.0):g} x (365/DTE)^{getattr(ws, "SCORE_DTE_EXP", 0.5):g}. For spreads, annualized ROR replaces AnnYield. Dividing by IV stops high-volatility names from dominating on richer premium; higher POP and shorter DTE break ties. Every table is ranked by Score (highest first) within each ticker.
- **Puts diversity:** {"best-Score contract per expiration only (collapses each ticker's strike ladder)" if getattr(ws, "PUT_BEST_PER_EXPIRATION", False) else "showing all qualifying strikes"}
- Yield-must-beat-IV filter: **{_on(ws.USE_YIELD_OVER_IV)}**{_yiv}
- Tiered OTM-to-yield rule: **{_on(ws.USE_TIERED_YIELD)}**  |  Beat-T-bill rule: **{_on(ws.USE_TBILL_SPREAD)}**
- Covered calls - require strike above your cost: **{_on(ws.REQUIRE_STRIKE_ABOVE_COST)}**

**Multi-Leg (Credit Spreads & Iron Condors) - all defined-risk**
- Structure: credit-spread short legs scanned from ~{sp.SHORT_DELTA:.2f} delta and further OTM; iron condors use two matched shorts. POP ranges from the floor up (safer variants included).
- Probability of profit (POP): {sp.SPREAD_POP_MIN:.0%} to {sp.SPREAD_POP_MAX:.0%}
- Minimum annualized ROR: {sp.ROR_ANN_MIN:.0%}
- Spread width: about {sp.SPREAD_WIDTH_PCT:.0%} of price (distance from short strike to long/protective strike)
- OTM floor on the short leg(s): {ws.OTM_MIN_INDEX:.0%} for index ETFs / {ws.OTM_MIN_OTHER:.0%} for other tickers
- Each short leg's OTM must also be >= {getattr(sp, "SPREAD_MIN_OTM_OVER_IV", 0):.0%} of its IV (same volatility-scaled cushion as puts/calls)
- Days to expiration: {sp.SPREAD_DTE_MIN} to {sp.SPREAD_DTE_MAX}; never spans earnings
- Excluded: short strangles and straddles (undefined risk)

Prices are live via Tradier (sandbox data ~15 min delayed). Educational only - not financial advice; verify every contract in your broker.
""")
except Exception as _e:
    st.caption(f"(legend unavailable: {_e})")
