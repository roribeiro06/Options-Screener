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
in wheel_screener.HOLDINGS, else undefined/"-"), width - credit for spreads
-- rolled up across every OPEN + recently-CLOSED position into one combined
Financials table (see build_combined_financials): G/L broken out by
strategy type, accumulated risk/premium, and Return on Risk.

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


FINANCIALS_COLS = ["Metric", "Value"]


def _fmt_dollar_signed(v):
    if v != v:
        return "-"
    return f"+${v:,.2f}" if v > 0 else (f"-${abs(v):,.2f}" if v < 0 else "$0.00")


def _fmt_dollar(v):
    return f"${v:,.2f}" if v == v else "-"


def _fmt_pct(v):
    return f"{v*100:.1f}%" if v == v else "-"


def _peak_concurrent_loss(intervals):
    """intervals: list of (start_date, end_date_inclusive, loss_dollars) --
    the total dollar Max Loss "at risk" between when each position opened
    and when it closed (or today, if still open). Returns the largest sum of
    loss_dollars among positions simultaneously open on the same day -- e.g.
    if 4 positions were open together on one day, their combined loss is one
    candidate. Only every interval's OWN start date needs checking, since the
    running total can only increase right after a position opens (adding a
    position never decreases the sum for that instant). NaN entries
    (undefined max loss, e.g. a covered call with no cost basis on file) are
    excluded from every day's sum -- can't bound an unknown risk."""
    valid = [(s, e, v) for s, e, v in intervals if v == v]
    if not valid:
        return float("nan")
    return max(sum(v for s2, e2, v in valid if s2 <= s <= e2) for s, _, _ in valid)


TYPE_BUCKETS = {"Put": "Put Contracts", "Covered Call": "Call Contracts",
                "Put Credit Spread": "Put Spread Contracts", "Call Credit Spread": "Call Spread Contracts",
                "Iron Condor": "Iron Condor Contracts"}
BUCKET_ORDER = ["Put Contracts", "Call Contracts", "Put Spread Contracts",
               "Call Spread Contracts", "Iron Condor Contracts"]


def build_combined_financials(dpos_df, dclosed_df, window_days=30):
    """One Financials table covering every OPEN_POSITIONS entry plus every
    CLOSED_POSITIONS entry within `window_days` (matching what's actually
    shown in the two tables above it):
      - G/L by strategy type (Put/Call/Put Spread/Call Spread/Iron Condor
        Contracts), unrealized (open) and realized (closed) separately
      - Max Profit Accumulated -- total premium collected across every one
        of those positions, plus the actual Unrealized/Realized G/L totals
      - Max Loss Accumulated -- the summed MaxLoss across every position
        (their combined worst case, if every single one hit simultaneously)
      - Max Loss 1D -- the highest that combined MaxLoss actually got on any
        ONE day, via an interval-overlap sweep across every position's
        entry-to-exit (or entry-to-today, if still open) window
      - Return on Risk (ROR%) for both loss figures -- premium collected
        divided by each
    dpos_df/dclosed_df are the already-computed tables from
    build_positions_table()/build_closed_positions_table(window_days) --
    reused here for their G/L sums rather than re-quoting every contract."""
    import pandas as pd
    today = dt.date.today()

    unreal_by_bucket = {b: 0.0 for b in BUCKET_ORDER}
    real_by_bucket = {b: 0.0 for b in BUCKET_ORDER}
    if len(dpos_df):
        for t, grp in dpos_df.groupby("Type"):
            b = TYPE_BUCKETS.get(t)
            if b:
                unreal_by_bucket[b] += grp["UnrealizedGL_$"].sum()
    if len(dclosed_df):
        for t, grp in dclosed_df.groupby("Type"):
            b = TYPE_BUCKETS.get(t)
            if b:
                real_by_bucket[b] += grp["RealizedGL_$"].sum()

    rows = []
    for b in BUCKET_ORDER:
        rows.append((f"{b} -- Unrealized G/L", _fmt_dollar_signed(unreal_by_bucket[b])))
        rows.append((f"{b} -- Realized G/L", _fmt_dollar_signed(real_by_bucket[b])))

    premium_total, unbounded = 0.0, False
    intervals = []
    for pos in ws.OPEN_POSITIONS:
        premium_total += pos["entry_credit"] * 100 * pos["contracts"]
        loss = _max_loss_per_share(pos)
        loss_total = loss * 100 * pos["contracts"] if loss == loss else float("nan")
        unbounded = unbounded or (loss_total != loss_total)
        entry = dt.date.fromisoformat(pos["entry_date"]) if pos.get("entry_date") else today
        intervals.append((entry, today, loss_total))
    for pos in ws.CLOSED_POSITIONS:
        exit_date = dt.date.fromisoformat(pos["exit_date"])
        if (today - exit_date).days > window_days:
            continue
        premium_total += pos["entry_credit"] * 100 * pos["contracts"]
        loss = _max_loss_per_share(pos)
        loss_total = loss * 100 * pos["contracts"] if loss == loss else float("nan")
        unbounded = unbounded or (loss_total != loss_total)
        entry = dt.date.fromisoformat(pos["entry_date"])
        intervals.append((entry, exit_date, loss_total))

    loss_accumulated = sum(v for _, _, v in intervals if v == v)
    peak_loss = _peak_concurrent_loss(intervals)
    ror_accum = (premium_total / loss_accumulated) if loss_accumulated else float("nan")
    ror_peak = (premium_total / peak_loss) if peak_loss else float("nan")

    total_unrealized = dpos_df["UnrealizedGL_$"].sum() if len(dpos_df) else 0.0
    total_realized = dclosed_df["RealizedGL_$"].sum() if len(dclosed_df) else 0.0

    rows += [
        ("Max Profit Accumulated ($)", _fmt_dollar(premium_total)),
        ("Max Profit -- Unrealized G/L (Open)", _fmt_dollar_signed(total_unrealized)),
        ("Max Profit -- Realized G/L (Closed)", _fmt_dollar_signed(total_realized)),
        ("Max Loss Accumulated ($)", _fmt_dollar(loss_accumulated)),
        ("Max Loss Accumulated (ROR%)", _fmt_pct(ror_accum)),
        ("Max Loss 1D ($)", _fmt_dollar(peak_loss)),
        ("Max Loss 1D (ROR%)", _fmt_pct(ror_peak)),
    ]
    if unbounded:
        rows.append(("Note", "One or more positions have no cost basis in wheel_screener.HOLDINGS -- "
                             "excluded from Max Loss Accumulated/1D above (risk undefined)."))
    return pd.DataFrame(rows, columns=FINANCIALS_COLS)
