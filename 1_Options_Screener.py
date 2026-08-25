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
import positions

st.set_page_config(page_title="Wheel Screener", layout="wide")
st.title("Wheel Screener")
st.caption("Cash-secured puts & covered calls. Live quotes from Tradier. "
           "Educational only - NOT financial advice. Verify every quote in your broker before trading.")
st.caption("Tip: click any row in a contract table below for a copy-paste summary (Ticker / # of contracts / "
           "DTE / strike(s) / premium) to send to your financial advisor.")
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


def _leg_lines(text):
    """Splits a leg-description string like 'sell 67.5P / buy 65P' into
    ['Sell $67.5 Put', 'Buy $65 Put'] -- one leg per line, spelled-out Put/Call
    instead of the table's P/C shorthand (printable summary only, the table
    itself keeps the shorthand)."""
    lines = []
    for leg in text.split(" / "):
        leg = leg.strip()
        action, _, rest = leg.partition(" ")
        if rest.endswith("P"):
            num, opt = rest[:-1], "Put"
        elif rest.endswith("C"):
            num, opt = rest[:-1], "Call"
        else:
            num, opt = rest, ""
        lines.append(f"{action.capitalize()} ${num} {opt}".strip())
    return lines


def _avg_max_range(lo, hi):
    """'$avg-$hi' -- the target premium to quote an advisor: the midpoint
    between worst- and best-case, not the conservative worst case alone, but
    capped at the best case rather than assuming you'll always get it. E.g.
    bid/ask $0.90/$1.10 -> "$1.00-$1.10". Degrades to a single number if only
    one side is available (or they're equal)."""
    if pd.isna(hi):
        return f"${lo:.2f}" if pd.notna(lo) else "-"
    if pd.isna(lo):
        return f"${hi:.2f}"
    avg = (lo + hi) / 2
    return f"${hi:.2f}" if avg == hi else f"${avg:.2f}-${hi:.2f}"


def _contract_summary(row):
    """Copy-paste summary for a financial advisor: Ticker / # of contracts /
    expiration date / Sell/Buy $strike Put or Call line(s) / premium. Built
    from the RAW (unformatted) row so the numbers are exact dollar figures,
    not the display-formatted range strings."""
    n = row.get("# of contracts")
    n_int = int(n) if pd.notna(n) else None
    expiration = row.get("Expiration")
    exp_txt = str(expiration) if pd.notna(expiration) else "-"
    header = [str(row["Ticker"]),
             f"{n_int if n_int is not None else '-'} contract{'s' if n_int != 1 else ''}",
             f"Expiration: {exp_txt}"]

    # Iron condor: the table keeps it as one combined row, but the user's
    # broker has no native iron condor order type -- it has to be placed as
    # two separate spread orders, so format the summary that way too, each
    # side with its own independent worst-to-best target premium.
    if pd.notna(row.get("Put Max Profit (Best)")) and pd.notna(row.get("Call Max Profit (Best)")):
        return "\n".join(header + [
            "", "Put Spread", *_leg_lines(str(row["Put Legs"])),
            f"Premium: {_avg_max_range(row.get('Put Max Profit'), row['Put Max Profit (Best)'])}",
            "", "Call Spread", *_leg_lines(str(row["Call Legs"])),
            f"Premium: {_avg_max_range(row.get('Call Max Profit'), row['Call Max Profit (Best)'])}",
        ])

    if "Strike" in row.index and pd.notna(row.get("Strike")):
        # Single-leg (cash-secured put / covered call): always a sell-to-open,
        # and always OTM in the direction that matches its type (puts strictly
        # below spot, calls strictly above -- the screener never shows ITM),
        # so Strike vs CurrentPrice alone tells us which one it is without
        # needing a separate type column (Contract Lookup doesn't carry one).
        # The rest of the app is worst-case (bid) throughout, but this summary
        # is a target to quote the advisor -- the bid-to-ask midpoint through
        # the ask, not the conservative bid used elsewhere.
        is_call = pd.notna(row.get("CurrentPrice")) and row["Strike"] > row["CurrentPrice"]
        strike_lines = [f"Sell ${row['Strike']:g} {'Call' if is_call else 'Put'}"]
        prem_txt = _avg_max_range(row.get("Premium"), row.get("Ask"))
    else:
        # Multi-leg: one leg per line (vertical), spelled-out Put/Call.
        strike_lines = []
        for c in ("Put Legs", "Call Legs"):
            v = row.get(c)
            if v:
                strike_lines.extend(_leg_lines(str(v)))
        if not strike_lines:
            strike_lines = ["-"]
        if pd.notna(row.get("Max Profit (Best)")):
            # Put/call credit spread: worst-to-best target credit.
            prem_txt = _avg_max_range(row.get("Max Profit"), row["Max Profit (Best)"])
        else:
            # Long straddle/strangle: MaxLoss IS the debit paid, a single
            # number, not a worst/best range -- nothing to average.
            per_share = row.get("MaxLoss")
            prem_txt = f"${per_share:.2f}" if pd.notna(per_share) else "-"
    return "\n".join(header + strike_lines + [f"Premium: {prem_txt}"])


def _selectable_table(raw_df, disp_df, key):
    """Same interactive/sortable table as before, plus click-a-row to get an
    advisor-ready summary box underneath. raw_df must be in the same row
    order as disp_df (disp_df is normally just _fmt(raw_df), maybe with a
    few columns dropped) so the click maps back to the real, unformatted
    numbers rather than the display strings."""
    event = st.dataframe(disp_df, hide_index=True, use_container_width=True,
                         on_select="rerun", selection_mode="single-row", key=key)
    sel = event.selection.rows if event is not None else []
    if sel:
        st.code(_contract_summary(raw_df.iloc[sel[0]]), language=None)


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
    if kind in ("put", "call"):
        rows = ws.lookup_contracts(ticker, kind, smin, smax, estart, eend)
        return ws._df(rows, ws.LOOKUP_COLS, sort_by=("Expiration", "Strike"), asc=(True, True))
    rows = sp.lookup_spreads(ticker, kind, smin, smax, estart, eend)
    return sp._df(rows)


@st.cache_data(ttl=600)
def cached_vix():
    return ws.get_vix()


@st.cache_data(ttl=600, show_spinner=True)
def scan_positions():
    return positions.build_positions_table()


@st.cache_data(ttl=3600, show_spinner=True)
def scan_concentration():
    return positions.build_concentration_table()


@st.cache_data(ttl=600, show_spinner=True)
def scan_closed_positions():
    return positions.build_closed_positions_table()


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
    _selectable_table(dp, ws._fmt(dp), "puts_tbl")
    st.download_button("Download puts (CSV)", dp.to_csv(index=False),
                       "puts.csv", "text/csv")
else:
    st.write("None qualify right now - nothing pays enough; stay in T-bills.")
if ep:
    st.caption("Skipped: " + " | ".join(ep))

st.subheader("Covered Calls")
if len(dc):
    _selectable_table(dc, ws._fmt(dc), "calls_tbl")
    st.download_button("Download calls (CSV)", dc.to_csv(index=False),
                       "calls.csv", "text/csv")
else:
    st.write("None qualify right now.")
if ec:
    st.caption("Skipped: " + " | ".join(ec))

st.markdown("---")
st.header("Contract Lookup")
st.caption("Look up ANY ticker's SELLABLE contracts in a strike/expiration range - out-of-the-money puts "
           "(cash-secured puts), calls (covered calls, as if you held the shares), or put/call credit "
           "spreads - even ones that don't pass the criteria above. Puts/put spreads show strikes below "
           "the price, calls/call spreads above it. For spreads, Strike min/max filters the SHORT leg; "
           "the long leg is auto-picked at the live Spread width % (sidebar). Same stats (Score, yields, "
           "IV, liquidity) as everywhere else.")
with st.form("lookup_form"):
    _c1, _c2, _c3, _c4 = st.columns([1.2, 1.3, 1, 1])
    lk_ticker = _c1.text_input("Ticker", "NVDA").strip().upper()
    lk_kind = _c2.radio("Type", ["put", "call", "put_spread", "call_spread"], horizontal=True,
                        format_func=lambda k: {"put": "Put", "call": "Call",
                                               "put_spread": "Put Spread",
                                               "call_spread": "Call Spread"}[k])
    lk_smin = _c3.number_input("Strike min (0 = any)", min_value=0.0, value=0.0, step=1.0)
    lk_smax = _c4.number_input("Strike max (0 = any)", min_value=0.0, value=0.0, step=1.0)
    _d1, _d2 = st.columns(2)
    _today = datetime.now().date()
    lk_start = _d1.date_input("Expiration from", _today)
    lk_end = _d2.date_input("Expiration to", _today + timedelta(days=90))
    lk_go = st.form_submit_button("Search")

if lk_go and lk_ticker:
    # Persist the query, not just react to lk_go: a form_submit_button's True
    # value only lasts for the one script run it was clicked on. Selecting a
    # row below triggers its own on_select="rerun", and on that rerun lk_go
    # is False again -- gating this whole section on it directly made the
    # results (and the row you just clicked) disappear instead of showing
    # the summary. Keying off session_state instead survives any rerun.
    st.session_state["lookup_query"] = (lk_ticker, lk_kind, lk_smin or None,
                                        lk_smax or None, lk_start, lk_end)

_lq = st.session_state.get("lookup_query")
if _lq:
    _q_ticker, _q_kind, _q_smin, _q_smax, _q_start, _q_end = _lq
    try:
        _res = scan_lookup(_q_ticker, _q_kind, _q_smin, _q_smax, _q_start, _q_end)
        if len(_res):
            _label = {"put": "cash-secured put", "call": "covered call",
                      "put_spread": "put credit spread", "call_spread": "call credit spread"}[_q_kind]
            st.write(f"**{len(_res)} {_label} contracts** for {_q_ticker} ({_q_start} to {_q_end}).")
            if _q_kind in ("put", "call"):
                _selectable_table(_res, ws._fmt(_res), "lookup_tbl")
            else:
                _lk_disp = _res.drop(columns=["Strategy", "Put Max Profit (Best)", "Call Max Profit (Best)"])
                for _lk_lc in ("Put Legs", "Call Legs"):     # drop the always-empty side
                    if _lk_lc in _lk_disp.columns and (_lk_disp[_lk_lc].fillna("") == "").all():
                        _lk_disp = _lk_disp.drop(columns=[_lk_lc])
                if "Breakeven" in _lk_disp.columns and (_lk_disp["Breakeven"] == "-").all():
                    _lk_disp = _lk_disp.drop(columns=["Breakeven"])
                _selectable_table(_res, sp._fmt(_lk_disp), "lookup_tbl")
            st.download_button("Download lookup (CSV)", _res.to_csv(index=False),
                               f"{_q_ticker}_{_q_kind}_lookup.csv", "text/csv")
        else:
            st.write("No sellable contracts in that range (for puts/put spreads try strikes below the price; "
                     "for calls/call spreads, above).")
    except Exception as _e:
        st.error(f"Lookup failed: {_e}")

st.markdown("---")
st.header("Multi-Leg Strategies  (70% POP)")
ds, es = scan_spreads(tuple(puts), SPREAD_CRITERIA)
_SPREAD_SECTIONS = [
    ("Put credit spread",  "Put Credit Spreads  (bullish, defined risk)"),
    ("Call credit spread", "Call Credit Spreads  (bearish, defined risk)"),
    ("Iron condor",        "Iron Condors  (neutral, defined risk)"),
    ("Long Straddle",      "Long Straddles  (big-move bet, defined risk, requires confirmed earnings in-window)"),
    ("Long Strangle",      "Long Strangles  (big-move bet, defined risk, requires confirmed earnings in-window)"),
]
for _key, _title in _SPREAD_SECTIONS:
    st.subheader(_title)
    _sub = ds[ds["Strategy"] == _key] if len(ds) else ds
    if len(_sub):
        # Iron condor's per-side best-case credit columns exist only so the
        # click-to-copy summary can quote the put spread and call spread as
        # two independent legs (see spreads.py SPREAD_COLS) -- never shown
        # in the table itself, the row stays a single combined iron condor.
        _disp = _sub.drop(columns=["Strategy", "Put Max Profit", "Put Max Profit (Best)",
                                   "Call Max Profit", "Call Max Profit (Best)"])
        for _lc in ("Put Legs", "Call Legs"):     # hide the empty side for one-sided spreads
            if _lc in _disp.columns and (_disp[_lc].fillna("") == "").all():
                _disp = _disp.drop(columns=[_lc])
        # Long Straddle/Strangle have no real Max Profit (unlimited upside, not a
        # guaranteed number) -- drop the column entirely rather than show a
        # column of "-" for every row (Breakeven is the real number there instead).
        for _mc in ("Max Profit", "Max Profit (Best)"):
            if _mc in _disp.columns and _disp[_mc].isna().all():
                _disp = _disp.drop(columns=[_mc])
        # Breakeven, conversely, only means something for Long Straddle/Strangle --
        # drop it for credit spreads/iron condor, where it's always "-".
        if "Breakeven" in _disp.columns and (_disp["Breakeven"] == "-").all():
            _disp = _disp.drop(columns=["Breakeven"])
        _selectable_table(_sub, sp._fmt(_disp), f"spread_tbl_{_key}")
        st.download_button(f"Download (CSV)", _sub.to_csv(index=False),
                           f"{_key.replace(' ', '_')}.csv", "text/csv", key=f"dl_{_key}")
    else:
        st.write("None qualify right now.")
if es:
    st.caption("Skipped: " + " | ".join(es))

st.markdown("---")
st.subheader("Discover: High-Open-Interest Contracts (outside your watchlist)")
st.caption("Live scan of a broad US-listed universe (not limited to the S&P 500), screened with the "
           "same criteria as above plus a higher open-interest floor (5,000, vs 1,000 elsewhere) -- so "
           "a name you didn't add to the watchlist can still surface if one of its most liquid contracts "
           "qualifies. Candidates come from two mechanisms: tickers trading well above their own normal "
           "volume today (**surge**), and tickers that moved sharply in the last day on real dollar volume "
           "(**movers**). Both require at least \\$25M/day in dollar volume (price x volume, not just a "
           "share count -- a cheap stock can clear a share-count bar on trivial real activity) AND a market "
           "cap of at least \\$10B, so thinly-capitalized or speculative names don't surface just because "
           "they're liquid or moved a lot. However a ticker got in, it's then routed purely by its OWN "
           "1-day % change today -- up gets screened for puts only, down for call credit spreads only "
           "(fading the move, with likely-richer premium from the volatility) -- so a name is never screened "
           "for both, even a surge candidate that didn't move much either direction. Every candidate still "
           "needs real open interest to qualify -- none of the above skips that check. "
           "Calls here are call credit spreads, not naked/covered calls -- these are tickers you don't hold "
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
            def _leader_txt(l):
                pct = (l.get("change_pct") or 0) * 100
                sign = "+" if pct >= 0 else ""
                src = l.get("source", "mover")
                return f"{l['ticker']} ({l['option_open_interest']:,} OI, {sign}{pct:.1f}% today, {src})"
            _lead_txt = " | ".join(_leader_txt(l) for l in _leaders)
            st.caption(f"Scanned {_vl.get('_meta', {}).get('built', '?')} - highest open interest: {_lead_txt}")

        st.markdown("**Puts**")
        _dp = ws._df(_vl.get("puts", []), ws.PUT_COLS, sort_by=("Ticker", "Score"), asc=(True, False))
        if len(_dp):
            _selectable_table(_dp, ws._fmt(_dp), "discover_puts_tbl")
            st.download_button("Download discovered puts (CSV)", _dp.to_csv(index=False),
                               "volume_leaders_puts.csv", "text/csv")
        else:
            st.write("None of today's highest-open-interest tickers currently qualify.")

        st.markdown("**Call Credit Spreads (defined-risk)**")
        _dc = sp._df(_vl.get("call_spreads", []))
        if len(_dc):
            _disp = _dc.drop(columns=["Strategy", "Put Legs", "Put Max Profit",
                                      "Put Max Profit (Best)", "Call Max Profit",
                                      "Call Max Profit (Best)"])
            # Breakeven only means something for Long Straddle/Strangle -- this
            # section is call credit spreads only, so it's always "-"; drop it.
            if "Breakeven" in _disp.columns and (_disp["Breakeven"] == "-").all():
                _disp = _disp.drop(columns=["Breakeven"])
            _selectable_table(_dc, sp._fmt(_disp), "discover_spreads_tbl")
            st.download_button("Download discovered call spreads (CSV)", _dc.to_csv(index=False),
                               "volume_leaders_call_spreads.csv", "text/csv")
        else:
            st.write("None of today's highest-open-interest tickers currently qualify.")
    else:
        st.write("Scan unavailable right now, and no fallback snapshot exists yet.")
except Exception as _e:
    st.caption(f"(discover section unavailable: {_e})")

st.markdown("---")
st.header("Open Positions")
st.caption("Tracked positions you've SOLD to open (puts, covered calls, credit spreads) -- edited in "
           "`OPEN_POSITIONS` at the top of wheel_screener.py (same pattern as the Watchlist/Holdings "
           "defaults), not from this page, so they survive redeploys. Sorted by DTE (soonest expiration "
           "first). **CurrentPrice** is the underlying STOCK's live price (not the option's), shown next "
           "to **Strike**. **DaysHeld** = days since Opened. **CostToClose** = the live ASK to buy the "
           "position back right now (conservative -- what you'd actually pay). **EntryCredit** shows "
           "\\$/share with the total across Contracts in parentheses. **UnrealizedGL** = EntryCredit "
           "minus CostToClose, i.e. what you'd realize if you closed now. **MaxLoss** (last column) is "
           "the net worst-case loss (premium already collected always reduces it): strike - premium for "
           "puts (assigned, stock to zero), cost basis - premium for covered calls (needs the ticker in "
           "`HOLDINGS`, else undefined), width - credit for spreads (already capped by the long leg, no "
           "stock-to-zero assumption needed). Same refresh cadence as the rest of the app.")
try:
    _dpos, _epos = scan_positions()
    if len(_dpos):
        st.dataframe(positions._fmt(_dpos), hide_index=True, use_container_width=True)
        st.download_button("Download positions (CSV)", _dpos.to_csv(index=False),
                           "open_positions.csv", "text/csv")
        st.markdown("**Financials** (unrealized)")
        st.caption("Columns split by strategy: **Put**, **Call** (covered), **Multi-Leg** (all spreads/"
                   "condors), **Total**. Max Loss here is NOT the same figure as the MaxLoss column above "
                   "-- Puts use a scaled-down, more realistic estimate (20% of the usual strike-minus-"
                   "premium worst case, minus the premium -- you're getting that back regardless of what "
                   "the stock does); Calls show \"-\" (no max loss at all -- a covered call going to "
                   "\\$0 is unrealistic enough that it's excluded outright, not just discounted); "
                   "Multi-Leg is unchanged (width - credit, same subtract-the-premium-back logic as puts, "
                   "already a real defined-risk worst case). **ROR %** is against the actual Unrealized "
                   "G/L, not the theoretical Potential Profit Acc. (premium collected).")
        st.dataframe(positions.build_open_financials(_dpos), hide_index=True, use_container_width=True)
    else:
        st.write("No open positions tracked yet -- add them to `OPEN_POSITIONS` in wheel_screener.py.")
    if _epos:
        st.caption("Skipped: " + " | ".join(_epos))
except Exception as _e:
    st.caption(f"(positions unavailable: {_e})")

st.markdown("---")
st.header("Concentration of Positions")
st.caption("Every OPEN_POSITIONS entry's MaxLoss (same figure as the Open Positions table's own MaxLoss "
           "column above), broken out by sector -- **Tech** vs **Non-Tech**, via each ticker's yfinance "
           "GICS sector (Technology **and** Communication Services both count as Tech, since GOOG/META "
           "land in the latter under GICS but this app's own peer-correlation list already treats them as "
           "part of the same tech cluster as MSFT/AMZN/AAPL; ETFs like SMH have no sector and fall back to "
           "their category instead) -- by **Put** / **Call** / **Multi-Leg** (all spreads/condors), same "
           "split as the Financials tables. Each cell is $total (% of your total MaxLoss across every open "
           "position) -- how concentrated your worst-case risk is in one corner of the book. A covered call "
           "with no cost basis in `HOLDINGS` (undefined MaxLoss) is excluded from every sum, same as "
           "Financials. Sector lookups are cached for an hour since they barely change.")
try:
    st.dataframe(scan_concentration(), hide_index=True, use_container_width=True)
except Exception as _e:
    st.caption(f"(concentration unavailable: {_e})")

st.markdown("---")
st.header("Closed Positions (last 30 days)")
st.caption("Positions you've closed, edited in `CLOSED_POSITIONS` at the top of wheel_screener.py -- add "
           "`exit_cost` (what you paid to buy it back, 0 if it expired worthless / hit max profit) and "
           "`exit_date` to a copy of the position's entry. Sorted by Date Opened (oldest first). Pure "
           "arithmetic against the recorded exit price -- no live quotes needed since the trade is already "
           "settled. **EntryCredit** shows \\$/share with the total across Contracts in parentheses, same "
           "convention as Open Positions. **MaxLoss** (last column) uses the same convention as Open "
           "Positions too. Automatically rolls off this list 30 days after the exit date (the entry stays "
           "in the config either way, just stops showing here).")
try:
    _dclosed, _eclosed = scan_closed_positions()
    if len(_dclosed):
        st.dataframe(positions._fmt(_dclosed), hide_index=True, use_container_width=True)
        st.download_button("Download closed positions (CSV)", _dclosed.to_csv(index=False),
                           "closed_positions.csv", "text/csv")
        st.markdown("**Financials** (realized)")
        st.caption("Same Put/Call/Multi-Leg/Total split and Max Loss convention as Open Positions above. "
                   "**ROR %** here is against the actual Realized G/L (what you really walked away with), "
                   "not the theoretical premium collected.")
        st.dataframe(positions.build_closed_financials(_dclosed), hide_index=True, use_container_width=True)
    else:
        st.write("No closed positions in the last 30 days.")
    if _eclosed:
        st.caption("Skipped: " + " | ".join(_eclosed))
except Exception as _e:
    st.caption(f"(closed positions unavailable: {_e})")

st.markdown("---")
st.header("Financials (Open + Closed combined)")
st.caption("Every OPEN_POSITIONS entry plus every CLOSED_POSITIONS entry in the 30-day window, same Put/"
           "Call/Multi-Leg/Total split and Max Loss convention as the two tables above. Unlike those two, "
           "**Max Loss 1D** here is a fresh peak-day sweep across the combined open+closed timeline (not "
           "just the two tables' 1D figures added together) -- a still-open position and an already-closed "
           "one can genuinely have overlapped on the same real day, so this can differ meaningfully from "
           "Max Loss Accumulated even when nothing in either table alone would suggest it. **ROR %** is "
           "against the summed actual Unrealized + Realized G/L, not the theoretical premium collected.")
try:
    _dpos_fin, _ = scan_positions()
    _dclosed_fin, _ = scan_closed_positions()
    st.dataframe(positions.build_combined_financials(_dpos_fin, _dclosed_fin),
                hide_index=True, use_container_width=True)
except Exception as _e:
    st.caption(f"(financials unavailable: {_e})")

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
**AvgPremium** = the typical ANNUALIZED YIELD this ticker has carried at that OTM%/DTE over the past year (not a dollar amount),
estimated from realized volatility (so it runs a touch low vs actual implied vol) - a "low%-high%" band, directly comparable to
**AnnYield_%** (puts/calls) or **AnnROR_%** (spreads, built from each leg's yield band converted back to a credit at the live
strikes/width). Where there's enough history at the ticker's CURRENT vol level, the band is also conditioned on live IV - "typical
yield when vol was around what it is right now", not averaged across every vol regime the ticker saw all year - falling back to
the OTM/DTE-only band otherwise. Live AnnYield_%/AnnROR_% above the band = richer than normal right now, below = cheaper.
Refreshed weekly; "-" means no history yet.

**Capital:** **# of contracts** = whole contracts needed to reach at least \${getattr(ws, "CASH_TARGET", 40000):,} of collateral
for puts/covered calls (strike x 100 / shares x 100), or \${getattr(sp, "SPREAD_CASH_TARGET", 25000):,} of max loss for
spreads (max loss x 100) -- spreads use a lower target since their risk per contract is capped/defined.
**Premium** (puts/calls) and **Max Profit** (spreads) show a **\$worst-\$best range** - worst case is selling the short leg(s) at
the bid and buying any long leg(s) at the ask, best case is the reverse (ask on shorts, bid on longs) - with the total across
your # of contracts in parentheses for each end of the range. A wide range means a wide bid-ask spread (costly to trade);
narrow means a tight, liquid market. **MaxLoss** shows the same \$/share (+ total) format: strike - premium for puts (worst
case if assigned and the stock goes to zero); cost basis - premium for covered calls with a known cost basis ("-" without
one, e.g. Contract Lookup); width - credit for spreads.
**Liquidity:** OpenInt = open interest (contracts outstanding); Volume = contracts traded today. Higher OpenInt/Volume mean
easier fills and less slippage. Contracts (and every spread leg) must have **open interest >= {getattr(ws, "MIN_OPEN_INTEREST", 0):,}** to appear.
**Value** = (AnnYield / IV) x sqrt(DTE/365) for single-leg, (AnnROR / IV) x sqrt(DTE/365) for spreads. Equivalently, period premium
yield divided by the expected move over the holding period (IV x sqrt(DTE/365)) - i.e. how much of the expected move you're paid.
Term-neutral, so short- and long-dated contracts are comparable. Higher = richer premium for the risk.

**Cash-Secured Puts & Covered Calls**
- Probability of profit (POP): {ws.POP_MIN:.0%} to {ws.POP_MAX:.0%}  (about 0.30 delta = 70% POP)
- Minimum annualized yield: {ws.MIN_ANN_YIELD:.0%} for stocks, {getattr(ws, "MIN_ANN_YIELD_INDEX", ws.MIN_ANN_YIELD):.0%} for broad indexes (SPY/QQQ/DIA, which also skip the per-share premium floors since they're lower risk)
- OTM floor: {ws.OTM_MIN_INDEX:.0%} for SPY/QQQ/DIA, {ws.OTM_MIN_OTHER:.0%} for all other tickers  (OTM max {ws.OTM_MAX:.0%})
- Days to expiration: {ws.DTE_MIN} to {ws.DTE_MAX}; never spans an earnings report
- **Puts only** extra filters: {("premium >= \$%.0f/share; " % ws.PUT_MIN_PREMIUM) if ws.PUT_MIN_PREMIUM > 0 else ""}{("premium >= %.1f%% of strike; " % (getattr(ws, "PUT_MIN_PREMIUM_PCT", 0)*100)) if getattr(ws, "PUT_MIN_PREMIUM_PCT", 0) > 0 else ""}AnnYield >= {ws.PUT_MIN_YIELD_OVER_IV:.0%} of IV; OTM >= {ws.PUT_MIN_OTM_OVER_IV:.0%} of IV
- **Covered calls** extra filter: OTM >= {getattr(ws, "CALL_MIN_OTM_OVER_IV", 0):.0%} of IV (same volatility-scaled cushion as puts)
- **Score** (all strategies) = (AnnYield / IV^{getattr(ws, "SCORE_IV_EXP", 1.0):g}) x POP^{getattr(ws, "SCORE_POP_EXP", 1.0):g} x (365/DTE)^{getattr(ws, "SCORE_DTE_EXP", 0.5):g}. For spreads, annualized ROR replaces AnnYield. Dividing by IV stops high-volatility names from dominating on richer premium; higher POP and shorter DTE break ties. Every table is ranked by Score (highest first) within each ticker.
- **Puts diversity:** {"best-Score contract per expiration only (collapses each ticker's strike ladder)" if getattr(ws, "PUT_BEST_PER_EXPIRATION", False) else "showing all qualifying strikes"}
- Yield-must-beat-IV filter: **{_on(ws.USE_YIELD_OVER_IV)}**{_yiv}
- Tiered OTM-to-yield rule: **{_on(ws.USE_TIERED_YIELD)}**  |  Beat-T-bill rule: **{_on(ws.USE_TBILL_SPREAD)}**
- Covered calls - require strike above your cost: **{_on(ws.REQUIRE_STRIKE_ABOVE_COST)}**

**Multi-Leg - all defined-risk (short strangles/straddles excluded -- undefined risk)**

*Credit Spreads & Iron Condors (selling premium)*
- Structure: credit-spread short legs scanned from ~{sp.SHORT_DELTA:.2f} delta and further OTM; iron condors use two matched shorts. POP ranges from the floor up (safer variants included).
- Probability of profit (POP): {sp.SPREAD_POP_MIN:.0%} to {sp.SPREAD_POP_MAX:.0%}
- Minimum annualized ROR: {sp.ROR_ANN_MIN:.0%}
- Spread width: about {sp.SPREAD_WIDTH_PCT:.0%} of price (distance from short strike to long/protective strike)
- OTM floor on the short leg(s): {ws.OTM_MIN_INDEX:.0%} for index ETFs / {ws.OTM_MIN_OTHER:.0%} for other tickers
- Each short leg's OTM must also be >= {getattr(sp, "SPREAD_MIN_OTM_OVER_IV", 0):.0%} of its IV (same volatility-scaled cushion as puts/calls)
- Days to expiration: {sp.SPREAD_DTE_MIN} to {sp.SPREAD_DTE_MAX}; never spans earnings

*Long Straddles/Strangles (buying premium, betting on a big move)* -- the opposite side of the trade
from everything else above: max loss is the debit paid (defined risk), not a net credit. POP is each
leg's own delta ADDED together (finishing beyond EITHER strike, not staying between them), scanned
from ATM (a straddle) down to a modest strangle so it can actually clear the same POP floor. **Max
Profit is "-"** -- a long strangle's upside is genuinely open-ended, so showing a single number there
would misleadingly imply a cap that doesn't exist (unlike every other row in this section, where it's
a real, guaranteed credit). **Breakeven** replaces it: the two prices the stock actually needs to
clear (strike +/- the debit paid) to be profitable at expiration, with the % move required from the
current price in parentheses -- a real number, not an estimate. **ROR/AnnROR/Score are tied to that
same Breakeven**: profit is estimated as whatever the IV-implied expected move (spot x IV x
sqrt(DTE/365)) clears ABOVE the closer of the two breakevens -- not the raw move itself, which would
wrongly count the whole move as profit even though nothing is made until breakeven is cleared (0 if
the expected move doesn't even reach it). Still an estimate, not a promised return -- treat it as "is
this cheap relative to what IV implies" -- and AnnROR can still look large on a short-DTE trade
(linear-annualizing a lumpy, non-repeatable payoff), just less absurdly so than a raw expected-move
ratio would.
- Straddle strike (put = call): whichever listed strike is closest to the current price, not the closest-to-0.50-delta strike (those can diverge meaningfully in a high-IV name)
- Strangle strikes: symmetric % away from the current price on each side (not matched by delta independently per leg, which produces lopsided strangles -- see the straddle note above, same root cause). % scanned: {", ".join(f"{p:.0%}" for p in sp.LONG_STRANGLE_OTM_PCTS)}, filtered afterward to combined POP {sp.SPREAD_POP_MIN:.0%} to {sp.SPREAD_POP_MAX:.0%}, same floor as above
- **Requires a CONFIRMED catalyst date** (unlike every other strategy here, which never spans one) -- either this ticker's own earnings, or a **related ticker's** (see `PEER_TICKERS` in wheel_screener.py -- e.g. MU has no earnings on the calendar but AMAT/SK Hynix/SanDisk reporting can still move it), checked live via the same earnings lookup either way. No known catalyst at all -> no candidate, regardless of how the rest of the math looks. EarningsDate shows "(via TICKER)" when a peer's date is what qualified it, not the ticker's own
- **Only the single expiration closest to (on or after) that catalyst date** is used -- every known catalyst (own + every peer) is tried, whichever gives the closest expiration wins -- not every later one that also happens to span it, since a catalyst trade should concentrate exposure around the event, not scatter near-duplicate rows across every weekly that comes after it. **Not bounded by the {sp.SPREAD_DTE_MIN}-{sp.SPREAD_DTE_MAX} DTE window above** -- a catalyst falling further out than {sp.SPREAD_DTE_MAX} days still gets a candidate at the nearest expiration after it, even though credit spreads/iron condor stay capped at {sp.SPREAD_DTE_MAX} -- but capped at **{sp.LONG_DTE_MAX} days**: a catalyst further out than that isn't worth tying up capital waiting for
- **No OTM floor / OTM-over-IV cushion** (unlike credit spreads/iron condor above) -- those exist to
  keep a SHORT leg meaningfully far from the money, and are nearly impossible for a long strangle to
  also clear: even at 50% IV and 40 DTE, a 0.35-delta leg is only ~8% OTM, short of the 10% floor. POP
  is the real gate here.
- **At most one straddle AND one strangle kept per ticker**, not one overall winner -- among the strangle widths, whichever needs the smallest move to breakeven wins, not whichever has the tightest-OTM strikes (a tighter strike often costs enough extra debit to push its own breakeven further away than a cheaper, slightly-wider alternative -- e.g. 2%-OTM strikes with an 8% breakeven loses to 3%-OTM strikes with a 5% breakeven)

Prices are live via Tradier (sandbox data ~15 min delayed). Educational only - not financial advice; verify every contract in your broker.
""")
except Exception as _e:
    st.caption(f"(legend unavailable: {_e})")
