"""
positions.py -- tracks your OPEN and recently-CLOSED positions
(wheel_screener.OPEN_POSITIONS / CLOSED_POSITIONS), all assumed sold-to-open
(cash-secured puts, covered calls, credit spreads -- what this whole app
screens for).

OPEN positions: live-quotes the exact contract(s) and computes
  - current cost to close (buy back the short leg(s), at the ASK -- the real,
    conservative price you'd actually pay right now)
  - CurrentPrice -- the underlying STOCK's live price (not the option's),
    via wheel_screener.td_quote, shown next to Strike
  - Unrealized G/L -- entry credit minus the ASK cost to close, the number
    you'd actually realize if you closed right now

CLOSED positions: pure arithmetic against the recorded exit price (no live
quotes -- the trade is already settled), shown for a rolling window (default
30 days) after the exit date so the list doesn't grow forever.

Both tables also carry a MaxLoss column (last column in both) -- same
convention as the rest of the app (wheel_screener.py/spreads.py): strike -
premium for puts, cost basis - premium for covered calls (needs the ticker
in wheel_screener.HOLDINGS, else undefined/"-"), width - credit for spreads.
Three Financials tables roll this up: build_open_financials (unrealized,
Open Positions only), build_closed_financials (realized, Closed Positions
only), and build_combined_financials (the two summed together) -- each
broken out by strategy type, with accumulated/peak-day risk, premium
collected, and Return on Risk.

Single-leg positions ("put"/"call") need a "strike"; spreads ("put_spread"/
"call_spread") need "short_strike" and "long_strike" instead -- the short
leg is what you sold, the long leg is the protective leg you bought (for a
put_spread, both are puts; for a call_spread, both are calls). Reuses
spreads.py's _leg_at() to find each exact contract in the chain.
"""
import sys
import datetime as dt

import wheel_screener as ws
import spreads as sp

TYPE_LABELS = {"put": "Put", "call": "Covered Call",
              "put_spread": "Put Credit Spread", "call_spread": "Call Credit Spread"}

POSITIONS_COLS = ["Ticker", "Type", "Strike", "CurrentPrice", "Expiration", "DTE", "DaysHeld", "Opened",
                  "Contracts", "EntryCredit", "CostToClose", "UnrealizedGL_$", "UnrealizedGL_%", "MaxLoss"]
CLOSED_COLS = ["Ticker", "Type", "Strike", "Expiration", "Opened", "Closed", "DaysHeld",
              "Contracts", "EntryCredit", "ExitCost", "RealizedGL_$", "RealizedGL_%", "MaxLoss"]
PCT_COLS = {"UnrealizedGL_%", "RealizedGL_%"}


def _leg_prices(chain, kind, strike):
    """Live (bid, ask, prevclose) for one leg, or None if the contract isn't
    in the chain (e.g. a typo'd strike, or the expiration has since passed)."""
    leg = sp._leg_at(chain, kind, strike)
    if not leg:
        return None
    return (leg.get("bid") or 0, leg.get("ask") or 0, leg.get("prevclose") or 0)


def _strikes_display(pos):
    kind = pos["type"]
    if kind in ("put", "call"):
        return f"{pos['strike']:g}"
    if kind in ("put_spread", "call_spread"):
        return f"{pos['short_strike']:g}/{pos['long_strike']:g}"
    raise RuntimeError(f"unknown position type {kind!r} for {pos.get('ticker', '?')}")


def _max_loss_per_share(pos):
    """The real net worst-case loss for this position -- premium already
    collected always reduces it, since you keep that regardless of what the
    stock does. Stock-to-zero is only assumed for covered calls, where it's
    genuinely the worst case against a known cost basis.
      - put: strike - premium -- assigned, then stock to zero, net of the
        premium you already banked (NOT the raw cash-secured collateral,
        which would be the strike alone -- this column is the worst-case
        loss, not the collateral requirement)
      - call (covered): cost basis - premium (needs the ticker in
        wheel_screener.HOLDINGS, else NaN -- undefined/unbounded risk, same
        as a naked call in Contract Lookup)
      - put_spread/call_spread: width - credit -- already the true worst
        case for a defined-risk spread, capped by the long leg regardless
        of how far the stock moves, so no stock-to-zero assumption applies"""
    kind = pos["type"]
    credit = pos["entry_credit"]
    if kind == "put":
        return pos["strike"] - credit
    if kind == "call":
        cost_basis = ws.HOLDINGS.get(pos["ticker"])
        return (cost_basis - credit) if cost_basis is not None else float("nan")
    if kind in ("put_spread", "call_spread"):
        width = abs(pos["short_strike"] - pos["long_strike"])
        return width - credit
    raise RuntimeError(f"unknown position type {kind!r} for {pos.get('ticker', '?')}")


def evaluate_position(pos, today):
    """Live-prices one OPEN_POSITIONS entry. Raises on a missing quote/contract
    so callers can report which position failed rather than silently skip it."""
    ticker = pos["ticker"]
    kind = pos["type"]
    exp = pos["expiration"]
    exp_date = dt.date.fromisoformat(exp)
    dte = (exp_date - today).days
    contracts = pos["contracts"]
    entry_credit = pos["entry_credit"]
    entry_date_str = pos.get("entry_date")
    days_held = (today - dt.date.fromisoformat(entry_date_str)).days if entry_date_str else float("nan")

    chain = ws.td_chain(ticker, exp)
    if not chain:
        raise RuntimeError(f"no option chain for {ticker} {exp} (expired or invalid expiration?)")

    if kind in ("put", "call"):
        strike = pos["strike"]
        leg = _leg_prices(chain, kind, strike)
        if not leg:
            raise RuntimeError(f"contract not found: {ticker} {strike:g}{kind[0].upper()} {exp}")
        _bid, ask, _prevclose = leg
        cost_to_close = ask
    else:  # put_spread / call_spread
        opt_type = "put" if kind == "put_spread" else "call"
        short_strike, long_strike = pos["short_strike"], pos["long_strike"]
        short_leg = _leg_prices(chain, opt_type, short_strike)
        long_leg = _leg_prices(chain, opt_type, long_strike)
        if not (short_leg and long_leg):
            raise RuntimeError(f"leg(s) not found: {ticker} {short_strike:g}/{long_strike:g}{opt_type[0].upper()} {exp}")
        _s_bid, s_ask, _s_prev = short_leg
        l_bid, _l_ask, _l_prev = long_leg
        cost_to_close = s_ask - l_bid                       # buy back short, sell long

    current_price = ws.td_quote(ticker)   # the underlying stock's live price, not the option's

    unrealized_pl = entry_credit - cost_to_close
    unrealized_pl_pct = (unrealized_pl / entry_credit) if entry_credit else float("nan")

    return {"Ticker": ticker, "Type": TYPE_LABELS.get(kind, kind), "Strike": _strikes_display(pos),
            "CurrentPrice": (round(current_price, 2) if current_price else float("nan")),
            "Expiration": exp, "DTE": dte, "DaysHeld": days_held, "Opened": entry_date_str or "-",
            "Contracts": contracts, "EntryCredit": entry_credit, "CostToClose": round(cost_to_close, 2),
            "UnrealizedGL_$": round(unrealized_pl * 100 * contracts, 2),
            "UnrealizedGL_%": unrealized_pl_pct, "MaxLoss": round(_max_loss_per_share(pos), 2)}


def build_positions_table():
    """Evaluates every OPEN_POSITIONS entry. Returns (dataframe, errors) --
    a position that fails (bad ticker, expired, contract not found) is
    reported as an error string rather than silently dropped, same pattern
    as screen_puts/screen_calls's error handling."""
    import pandas as pd
    today = dt.date.today()
    rows, errs = [], []
    for pos in ws.OPEN_POSITIONS:
        try:
            rows.append(evaluate_position(pos, today))
        except Exception as e:
            errs.append(f"{pos.get('ticker', '?')}: {e}")
            print(f"POSITION {pos.get('ticker', '?')}: ERROR {e}", file=sys.stderr)
    if not rows:
        return pd.DataFrame(columns=POSITIONS_COLS), errs
    df = pd.DataFrame(rows)[POSITIONS_COLS].sort_values("DTE", ascending=True)
    return df, errs


def evaluate_closed_position(pos):
    """Realized P&L for one CLOSED_POSITIONS entry -- pure arithmetic against
    the recorded exit price, no live quotes needed since the trade is done."""
    ticker = pos["ticker"]
    kind = pos["type"]
    contracts = pos["contracts"]
    entry_credit = pos["entry_credit"]
    exit_cost = pos["exit_cost"]
    entry_date = dt.date.fromisoformat(pos["entry_date"])
    exit_date = dt.date.fromisoformat(pos["exit_date"])

    realized_pl = entry_credit - exit_cost
    realized_pl_pct = (realized_pl / entry_credit) if entry_credit else float("nan")

    return {"Ticker": ticker, "Type": TYPE_LABELS.get(kind, kind), "Strike": _strikes_display(pos),
            "Expiration": pos["expiration"], "Opened": pos["entry_date"], "Closed": pos["exit_date"],
            "DaysHeld": (exit_date - entry_date).days, "Contracts": contracts,
            "EntryCredit": entry_credit, "ExitCost": exit_cost,
            "RealizedGL_$": round(realized_pl * 100 * contracts, 2),
            "RealizedGL_%": realized_pl_pct, "MaxLoss": round(_max_loss_per_share(pos), 2)}


def build_closed_positions_table(window_days=30):
    """Closed positions with an exit_date within the last `window_days`
    (default 30) of today. Returns (dataframe, errors), same error-reporting
    pattern as build_positions_table -- a malformed entry is reported, not
    silently dropped."""
    import pandas as pd
    today = dt.date.today()
    rows, errs = [], []
    for pos in ws.CLOSED_POSITIONS:
        try:
            exit_date = dt.date.fromisoformat(pos["exit_date"])
            if (today - exit_date).days > window_days:
                continue
            rows.append(evaluate_closed_position(pos))
        except Exception as e:
            errs.append(f"{pos.get('ticker', '?')}: {e}")
            print(f"CLOSED POSITION {pos.get('ticker', '?')}: ERROR {e}", file=sys.stderr)
    if not rows:
        return pd.DataFrame(columns=CLOSED_COLS), errs
    df = pd.DataFrame(rows)[CLOSED_COLS].sort_values("Opened", ascending=True)
    return df, errs


def _fmt(df):
    d = df.copy()
    for c in PCT_COLS:
        if c in d.columns:
            d[c] = d[c].apply(lambda v: f"{v*100:.1f}%" if v == v else "-")
    # EntryCredit: "$/share (total premium across Contracts)", same convention as Premium/MaxLoss elsewhere.
    if "EntryCredit" in d.columns and "Contracts" in d.columns:
        d["EntryCredit"] = [f"${v:.2f} (${v * 100 * int(n):,.2f})" if v == v else "-"
                            for v, n in zip(d["EntryCredit"], d["Contracts"])]
    elif "EntryCredit" in d.columns:
        d["EntryCredit"] = d["EntryCredit"].apply(lambda v: f"${v:.2f}" if v == v else "-")
    for c in ("CostToClose", "ExitCost", "CurrentPrice"):
        if c in d.columns:
            d[c] = d[c].apply(lambda v: f"${v:.2f}" if v == v else "-")
    # MaxLoss: "$/share (total across Contracts)", same convention as wheel_screener.py/spreads.py.
    if "MaxLoss" in d.columns and "Contracts" in d.columns:
        d["MaxLoss"] = [f"${v:.2f} (${v * 100 * int(n):,.2f})" if v == v else "-"
                        for v, n in zip(d["MaxLoss"], d["Contracts"])]
    elif "MaxLoss" in d.columns:
        d["MaxLoss"] = d["MaxLoss"].apply(lambda v: f"${v:.2f}" if v == v else "-")
    for c in ("UnrealizedGL_$", "RealizedGL_$"):
        if c in d.columns:
            d[c] = d[c].apply(lambda v: (f"+${v:,.2f}" if v > 0 else f"-${abs(v):,.2f}") if v == v else "-")
    return d


def _fmt_dollar_signed(v):
    if v != v:
        return "-"
    return f"+${v:,.2f}" if v > 0 else (f"-${abs(v):,.2f}" if v < 0 else "$0.00")


def _fmt_dollar(v):
    return f"${v:,.2f}" if v == v else "-"


def _fmt_pct(v):
    return f"{v*100:.1f}%" if v == v else "-"


def _closed_in_window(window_days, today):
    return [pos for pos in ws.CLOSED_POSITIONS
            if (today - dt.date.fromisoformat(pos["exit_date"])).days <= window_days]


PIVOT_COLS = ["Put", "Call", "Multi-Leg"]
_TYPE_LABEL_TO_PIVOT = {"Put": "Put", "Covered Call": "Call", "Put Credit Spread": "Multi-Leg",
                        "Call Credit Spread": "Multi-Leg", "Iron Condor": "Multi-Leg"}


def _pivot_max_loss_per_share(pos):
    """Max Loss per share for the pivoted Put/Call/Multi-Leg/Total Financials
    tables -- deliberately different from the MaxLoss column shown in the
    Open/Closed Positions tables above them:
      - put: scaled to a more realistic tail-risk estimate -- 20% of the
        existing strike-minus-premium worst case, minus the premium (you're
        getting that back regardless of what the stock does, same
        subtract-the-premium-you-collected logic as spreads below)
      - call (covered): NaN -- no max loss at all. A covered call's
        stock-to-zero worst case is unrealistic enough that these tables
        exclude it outright (shows "-"), not just discount it
      - put_spread/call_spread/iron_condor ("Multi-Leg"): unchanged --
        width - credit is already the max loss minus the premium you're
        getting back, exactly the same principle as puts above"""
    kind = pos["type"]
    if kind == "put":
        return _max_loss_per_share(pos) * 0.20 - pos["entry_credit"]
    if kind == "call":
        return float("nan")
    return _max_loss_per_share(pos)


def _pivot_gl(df, gl_col):
    """{'Put'/'Call'/'Multi-Leg': summed $} from an already-computed
    positions/closed dataframe's G/L column, using the real (unadjusted)
    figures -- only the MaxLoss-derived rows use _pivot_max_loss_per_share."""
    out = {b: 0.0 for b in PIVOT_COLS}
    if len(df):
        for t, grp in df.groupby("Type"):
            b = _TYPE_LABEL_TO_PIVOT.get(t)
            if b:
                out[b] += grp[gl_col].sum()
    return out


def _pivot_entries(positions_list, end_date_fn, today):
    """(entry_date, end_date_inclusive, pivot_loss_$, premium_$, bucket) per
    position, ready for _pivot_table. end_date_fn(pos) -> today for open
    positions, exit_date for closed ones."""
    out = []
    for pos in positions_list:
        b = _TYPE_LABEL_TO_PIVOT.get(TYPE_LABELS.get(pos["type"]))
        if not b:
            continue
        loss = _pivot_max_loss_per_share(pos)
        loss_total = loss * 100 * pos["contracts"] if loss == loss else float("nan")
        premium = pos["entry_credit"] * 100 * pos["contracts"]
        entry = dt.date.fromisoformat(pos["entry_date"]) if pos.get("entry_date") else today
        out.append((entry, end_date_fn(pos), loss_total, premium, b))
    return out


def _pivot_sum(entries, idx, bucket=None):
    """Flat sum of entries[idx] (2=loss, 3=premium), skipping NaN (the Call
    bucket's loss is always NaN, so it never contributes)."""
    return sum(e[idx] for e in entries if e[idx] == e[idx] and (bucket is None or e[4] == bucket))


def _pivot_peak(entries, bucket=None):
    """Same interval-overlap sweep as elsewhere in this module, scoped to one
    bucket (or all of them, for the Total column) -- the largest sum of
    pivot-loss $ among positions open on the same day."""
    valid = [(e[0], e[1], e[2]) for e in entries if e[2] == e[2] and (bucket is None or e[4] == bucket)]
    if not valid:
        return float("nan")
    return max(sum(v for s2, e2, v in valid if s2 <= s <= e2) for s, _, _ in valid)


def _pivot_table(gl_row_label, gl_by_bucket, entries):
    """One Put/Call/Multi-Leg/Total pivoted Financials table. `gl_by_bucket`
    is the row-1 G/L figure (Unrealized, Realized, or their sum) per bucket,
    already live-quoted/computed elsewhere. `entries` comes from
    _pivot_entries(). ROR% is measured against that same G/L figure -- the
    real money made/lost, not the theoretical Potential Profit Acc. (premium
    collected). The Call column's Max Loss/ROR% is always "-" (see
    _pivot_max_loss_per_share) -- Total still nets out correctly since NaN
    entries are skipped, not zeroed."""
    import pandas as pd
    cols = PIVOT_COLS + ["Total"]

    gl = dict(gl_by_bucket)
    gl["Total"] = sum(gl_by_bucket.values())

    premium = {c: _pivot_sum(entries, 3, None if c == "Total" else c) for c in cols}
    loss_accum = {c: _pivot_sum(entries, 2, None if c == "Total" else c) for c in cols}
    loss_1d = {c: _pivot_peak(entries, None if c == "Total" else c) for c in cols}
    loss_accum["Call"] = float("nan")   # no max loss for covered calls -- always "-"
    loss_1d["Call"] = float("nan")

    def _ror(loss):
        return {c: _fmt_pct((gl[c] / loss[c]) if loss[c] == loss[c] and loss[c] else float("nan"))
               for c in cols}
    ror_accum, ror_1d = _ror(loss_accum), _ror(loss_1d)

    rows = [
        (gl_row_label, *[_fmt_dollar_signed(gl[c]) for c in cols]),
        ("Potential Profit Acc. ($)", *[_fmt_dollar(premium[c]) for c in cols]),
        ("Max Loss Accumulated ($)", *[_fmt_dollar(loss_accum[c]) for c in cols]),
        ("Max Loss 1D ($)", *[_fmt_dollar(loss_1d[c]) for c in cols]),
        ("ROR % (Accumulated)", *[ror_accum[c] for c in cols]),
        ("ROR % (1D)", *[ror_1d[c] for c in cols]),
    ]
    return pd.DataFrame(rows, columns=["Metric"] + cols)


def build_open_financials(dpos_df):
    """Pivoted Financials for OPEN_POSITIONS only (row 1 = Unrealized G/L,
    also ROR%'s basis). See _pivot_table/_pivot_max_loss_per_share for the
    Put/Call/Multi-Leg breakdown and the per-type Max Loss adjustment."""
    today = dt.date.today()
    gl = _pivot_gl(dpos_df, "UnrealizedGL_$")
    entries = _pivot_entries(ws.OPEN_POSITIONS, lambda pos: today, today)
    return _pivot_table("G/L (Unrealized)", gl, entries)


def build_closed_financials(dclosed_df, window_days=30):
    """Pivoted Financials for CLOSED_POSITIONS within `window_days` (row 1 =
    Realized G/L, also ROR%'s basis) -- see _pivot_table."""
    today = dt.date.today()
    closed_list = _closed_in_window(window_days, today)
    gl = _pivot_gl(dclosed_df, "RealizedGL_$")
    entries = _pivot_entries(closed_list, lambda pos: dt.date.fromisoformat(pos["exit_date"]), today)
    return _pivot_table("G/L (Realized)", gl, entries)


def build_combined_financials(dpos_df, dclosed_df, window_days=30):
    """Pivoted Financials across every OPEN_POSITIONS entry plus every
    CLOSED_POSITIONS entry within `window_days` (row 1 = Unrealized +
    Realized G/L). Unlike the two tables above, Max Loss 1D here is a fresh
    interval-overlap sweep across the combined open+closed timeline, not a
    sum of the two tables' own peak-day figures -- positions from both
    tables can genuinely overlap on the same calendar day."""
    today = dt.date.today()
    closed_list = _closed_in_window(window_days, today)
    gl_open = _pivot_gl(dpos_df, "UnrealizedGL_$")
    gl_closed = _pivot_gl(dclosed_df, "RealizedGL_$")
    gl = {b: gl_open[b] + gl_closed[b] for b in PIVOT_COLS}
    entries = (_pivot_entries(ws.OPEN_POSITIONS, lambda pos: today, today)
              + _pivot_entries(closed_list, lambda pos: dt.date.fromisoformat(pos["exit_date"]), today))
    return _pivot_table("G/L (Unrealized + Realized)", gl, entries)


CONCENTRATION_ROWS = ["Tech", "Non-Tech"]
# Unlike PIVOT_COLS (Put/Call/Multi-Leg, used by the Financials tables above,
# which keeps spreads as their own bucket), this table groups by directional
# side instead -- a put spread is still bullish-put-side risk, so it joins
# plain puts under "Put", and a call spread joins plain calls under "Call".
# Same mapping open_position_sides() uses to gate the screener by side.
CONCENTRATION_COLS = ["Put", "Call"]
_TYPE_TO_CONCENTRATION_BUCKET = {"put": "Put", "put_spread": "Put",
                                 "call": "Call", "call_spread": "Call"}


def build_concentration_table():
    """Concentration of Positions: every OPEN_POSITIONS entry's Max Loss --
    _pivot_max_loss_per_share, the SAME risk-scaled convention every
    Financials table on this page already uses: covered calls are NaN (a
    stock-to-zero worst case is unrealistic enough that they're excluded
    outright, not just discounted -- a covered call genuinely has no
    meaningful "max loss" in that sense), puts are scaled to a more
    realistic 20% tail estimate net of premium, spreads are unchanged
    (width - credit, already a real defined-risk worst case) -- broken out
    by sector (ws.get_sector_bucket -- Tech vs Non-Tech) x directional side
    (Put/Call, via _TYPE_TO_CONCENTRATION_BUCKET -- put spreads join plain
    puts, call spreads join plain calls, not a separate Multi-Leg bucket),
    each cell shown as "$total (X%)" of the grand total across every
    position. Total row/column included. A position with an undefined Max
    Loss (the covered calls, plus a put/call with no HOLDINGS cost basis
    where relevant) is excluded from every sum, same as the Financials
    tables above."""
    import pandas as pd
    cols = CONCENTRATION_COLS + ["Total"]
    rows_ = CONCENTRATION_ROWS + ["Total"]
    grid = {r: {c: 0.0 for c in cols} for r in rows_}

    for pos in ws.OPEN_POSITIONS:
        b = _TYPE_TO_CONCENTRATION_BUCKET.get(pos["type"])
        if not b:
            continue
        loss = _pivot_max_loss_per_share(pos)
        if loss != loss:
            continue
        loss_total = loss * 100 * pos["contracts"]
        sector = ws.get_sector_bucket(pos["ticker"])
        for r in (sector, "Total"):
            grid[r][b] += loss_total
            grid[r]["Total"] += loss_total

    grand_total = grid["Total"]["Total"]

    def _cell(v):
        pct = (v / grand_total) if grand_total else float("nan")
        return f"{_fmt_dollar(v)} ({_fmt_pct(pct)})"

    return pd.DataFrame([(r, *[_cell(grid[r][c]) for c in cols]) for r in rows_],
                        columns=["Sector"] + cols)


def build_premium_change_table():
    """Total premium (the contract's own price, NOT MaxLoss/risk) of open
    puts and open calls, today vs yesterday -- e.g. an NVDA 230 put priced
    at $3.50 yesterday and $3.00 today. Today = live ask (cost to buy the
    contract back right now, same basis as Open Positions' own CostToClose
    column); yesterday = that contract's own prevclose, straight from the
    same live chain fetch Tradier already returns -- no separate snapshot/
    database needed. A position opened TODAY has no real "yesterday" -- it's
    excluded from yesterday's total (but still counted in today's), so the
    two totals reflect what you actually held on each day, not a
    hypothetical same-basket comparison. Puts and calls only, not spreads --
    a multi-leg position has two legs, so there's no single contract price
    to compare the way there is for a plain put/call. A contract with no
    prevclose available (illiquid/newly listed) is skipped from that side's
    yesterday sum rather than counted as $0. Returns (dataframe, errors)."""
    import pandas as pd
    today = dt.date.today()
    totals = {"Put": {"today": 0.0, "yesterday": 0.0},
             "Call": {"today": 0.0, "yesterday": 0.0}}
    errs = []
    for pos in ws.OPEN_POSITIONS:
        kind = pos["type"]
        if kind not in ("put", "call"):
            continue
        bucket = "Put" if kind == "put" else "Call"
        ticker, strike, exp, contracts = pos["ticker"], pos["strike"], pos["expiration"], pos["contracts"]
        try:
            chain = ws.td_chain(ticker, exp)
            if not chain:
                raise RuntimeError("no option chain (expired or invalid expiration?)")
            leg = _leg_prices(chain, kind, strike)
            if not leg:
                raise RuntimeError("contract not found")
        except Exception as e:
            errs.append(f"{ticker} {strike:g}{kind[0].upper()} {exp}: {e}")
            continue
        _bid, ask, prevclose = leg
        totals[bucket]["today"] += ask * 100 * contracts
        entry_date_str = pos.get("entry_date")
        opened_today = bool(entry_date_str) and dt.date.fromisoformat(entry_date_str) == today
        if not opened_today and prevclose:
            totals[bucket]["yesterday"] += prevclose * 100 * contracts

    rows = []
    for bucket in ("Put", "Call"):
        y, t = totals[bucket]["yesterday"], totals[bucket]["today"]
        change = t - y
        pct = (change / y) if y else float("nan")
        rows.append((bucket, _fmt_dollar(y), _fmt_dollar(t), _fmt_dollar_signed(change), _fmt_pct(pct)))
    return pd.DataFrame(rows, columns=["Type", "Yesterday", "Today", "Change $", "Change %"]), errs
