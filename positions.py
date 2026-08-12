"""
positions.py -- tracks your OPEN and recently-CLOSED positions
(wheel_screener.OPEN_POSITIONS / CLOSED_POSITIONS), all assumed sold-to-open
(cash-secured puts, covered calls, credit spreads -- what this whole app
screens for).

OPEN positions: live-quotes the exact contract(s) and computes
  - current cost to close (buy back the short leg(s), at the ASK -- the real,
    conservative price you'd actually pay right now)
  - Day $/% -- today's move using MID (bid+ask)/2 vs. the contract's own
    prevclose, matching how a brokerage shows daily P&L (not a worst-case
    execution price, just "did this get cheaper or richer today")
  - Unrealized G/L -- entry credit minus the ASK cost to close, the number
    you'd actually realize if you closed right now

CLOSED positions: pure arithmetic against the recorded exit price (no live
quotes -- the trade is already settled), shown for a rolling window (default
30 days) after the exit date so the list doesn't grow forever.

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

POSITIONS_COLS = ["Ticker", "Type", "Strikes", "Expiration", "DTE", "Opened", "Contracts",
                  "EntryCredit", "CostToClose", "Day_$", "Day_%", "UnrealizedGL_$", "UnrealizedGL_%"]
CLOSED_COLS = ["Ticker", "Type", "Strikes", "Expiration", "Opened", "Closed", "DaysHeld",
              "Contracts", "EntryCredit", "ExitCost", "RealizedGL_$", "RealizedGL_%"]
PCT_COLS = {"Day_%", "UnrealizedGL_%", "RealizedGL_%"}


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

    chain = ws.td_chain(ticker, exp)
    if not chain:
        raise RuntimeError(f"no option chain for {ticker} {exp} (expired or invalid expiration?)")

    strikes_display = _strikes_display(pos)
    if kind in ("put", "call"):
        strike = pos["strike"]
        leg = _leg_prices(chain, kind, strike)
        if not leg:
            raise RuntimeError(f"contract not found: {ticker} {strike:g}{kind[0].upper()} {exp}")
        bid, ask, prevclose = leg
        cost_to_close = ask
        mid_now = (bid + ask) / 2
        prev_mid = prevclose
    else:  # put_spread / call_spread
        opt_type = "put" if kind == "put_spread" else "call"
        short_strike, long_strike = pos["short_strike"], pos["long_strike"]
        short_leg = _leg_prices(chain, opt_type, short_strike)
        long_leg = _leg_prices(chain, opt_type, long_strike)
        if not (short_leg and long_leg):
            raise RuntimeError(f"leg(s) not found: {ticker} {short_strike:g}/{long_strike:g}{opt_type[0].upper()} {exp}")
        s_bid, s_ask, s_prev = short_leg
        l_bid, l_ask, l_prev = long_leg
        cost_to_close = s_ask - l_bid                       # buy back short, sell long
        mid_now = (s_bid + s_ask) / 2 - (l_bid + l_ask) / 2
        prev_mid = (s_prev - l_prev) if (s_prev and l_prev) else 0

    unrealized_pl = entry_credit - cost_to_close
    unrealized_pl_pct = (unrealized_pl / entry_credit) if entry_credit else float("nan")

    day_pl = (prev_mid - mid_now) if prev_mid else float("nan")   # cheaper today = gain on a short position
    day_pl_pct = (day_pl / prev_mid) if prev_mid else float("nan")

    return {"Ticker": ticker, "Type": TYPE_LABELS.get(kind, kind), "Strikes": strikes_display,
            "Expiration": exp, "DTE": dte, "Opened": pos.get("entry_date", "-"),
            "Contracts": contracts, "EntryCredit": entry_credit, "CostToClose": round(cost_to_close, 2),
            "Day_$": (round(day_pl * 100 * contracts, 2) if day_pl == day_pl else float("nan")),
            "Day_%": day_pl_pct,
            "UnrealizedGL_$": round(unrealized_pl * 100 * contracts, 2),
            "UnrealizedGL_%": unrealized_pl_pct}


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

    return {"Ticker": ticker, "Type": TYPE_LABELS.get(kind, kind), "Strikes": _strikes_display(pos),
            "Expiration": pos["expiration"], "Opened": pos["entry_date"], "Closed": pos["exit_date"],
            "DaysHeld": (exit_date - entry_date).days, "Contracts": contracts,
            "EntryCredit": entry_credit, "ExitCost": exit_cost,
            "RealizedGL_$": round(realized_pl * 100 * contracts, 2),
            "RealizedGL_%": realized_pl_pct}


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
    df = pd.DataFrame(rows)[CLOSED_COLS].sort_values("Opened", ascending=False)
    return df, errs


def _fmt(df):
    d = df.copy()
    for c in PCT_COLS:
        if c in d.columns:
            d[c] = d[c].apply(lambda v: f"{v*100:.1f}%" if v == v else "-")
    for c in ("EntryCredit", "CostToClose", "ExitCost"):
        if c in d.columns:
            d[c] = d[c].apply(lambda v: f"${v:.2f}" if v == v else "-")
    for c in ("Day_$", "UnrealizedGL_$", "RealizedGL_$"):
        if c in d.columns:
            d[c] = d[c].apply(lambda v: (f"+${v:,.2f}" if v > 0 else f"-${abs(v):,.2f}") if v == v else "-")
    return d
