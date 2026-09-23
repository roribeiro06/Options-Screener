"""
positions.py -- tracks your OPEN and recently-CLOSED positions
(wheel_screener.OPEN_POSITIONS / CLOSED_POSITIONS), all assumed sold-to-open
(cash-secured puts, covered calls, credit spreads -- what this whole app
screens for).

OPEN positions: live-quotes the exact contract(s) and computes
  - current cost to close (buy back the short leg(s)), as a bid-to-ask range
    -- CostToCloseBid is the optimistic/best case (bid basis), CostToClose
    is the conservative/worst case (ASK basis, the real price you'd actually
    pay if forced to close right now)
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

Auto-close (see _is_expired_unclosed/_auto_closed_entry): an OPEN_POSITIONS
entry whose expiration is 1+ day in the past, with no matching CLOSED_POSITIONS
entry, is treated as closed automatically -- moved out of Open Positions and
into Closed Positions (and every Financials/Concentration table) everywhere,
with exit_cost=0 (expired worthless, full premium kept), since that's the only
default possible without being told otherwise. If a position actually finished
ITM/assigned instead, add the real CLOSED_POSITIONS entry with its true
exit_cost -- an explicit entry always overrides this assumption.
"""
import re
import sys
import datetime as dt

import wheel_screener as ws
import spreads as sp

TYPE_LABELS = {"put": "Put", "call": "Covered Call",
              "put_spread": "Put Credit Spread", "call_spread": "Call Credit Spread"}

# Row order within a tie on the primary sort (DTE / Closed date): a ticker's
# positions stay together, and put-side sorts before call-side. That keeps the
# two halves of an iron condor -- tracked as a separate put spread and call
# spread on the same ticker/expiration -- directly on top of each other, instead
# of another ticker's contract landing between them. Display order only.
_TYPE_ORDER = {"Put": 0, "Put Credit Spread": 1, "Call Credit Spread": 2, "Covered Call": 3}


def _sort_grouped(df, primary):
    return (df.assign(_o=df["Type"].map(_TYPE_ORDER).fillna(9))
              .sort_values([primary, "Ticker", "Expiration", "_o"], kind="stable")
              .drop(columns="_o"))


POSITIONS_COLS = ["Ticker", "Type", "Strike", "CurrentPrice", "Expiration", "DTE", "DaysHeld", "Opened",
                  "Contracts", "EntryCredit", "CostToCloseBid", "CostToClose", "UnrealizedGL_$",
                  "UnrealizedGL_%", "MaxLoss"]
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


def _pos_key(pos):
    """Identity for matching an OPEN_POSITIONS entry to a CLOSED_POSITIONS
    one -- ticker/type/expiration/entry_date plus whichever strike field(s)
    this type uses."""
    base = (pos["ticker"], pos["type"], pos["expiration"], pos.get("entry_date"))
    if pos["type"] in ("put", "call"):
        return base + (pos["strike"],)
    return base + (pos["short_strike"], pos["long_strike"])


def _has_explicit_close(pos):
    key = _pos_key(pos)
    return any(_pos_key(c) == key for c in ws.CLOSED_POSITIONS)


def _is_expired_unclosed(pos, today):
    """True once an OPEN_POSITIONS entry's expiration is at least 1 day in
    the past and no CLOSED_POSITIONS entry already accounts for it -- rather
    than requiring you to explicitly report every expiration, it's assumed
    closed automatically. No exit price was given, so the only reasonable
    default is exit_cost=0 (expired worthless, full premium kept) -- if a
    position actually finished ITM/assigned instead, tell me so it can get a
    real CLOSED_POSITIONS entry with the correct exit_cost; that explicit
    entry then takes priority over this assumption (see _has_explicit_close)."""
    exp_date = dt.date.fromisoformat(pos["expiration"])
    if (today - exp_date).days < 1:
        return False
    return not _has_explicit_close(pos)


def _auto_closed_entry(pos):
    """Synthesize a CLOSED_POSITIONS-shaped dict for an expired-but-unclosed
    OPEN_POSITIONS entry -- see _is_expired_unclosed."""
    return {**pos, "exit_cost": 0, "exit_date": pos["expiration"]}


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
        bid, ask, _prevclose = leg
        cost_to_close = ask                                  # worst case (guaranteed fill)
        cost_to_close_bid = bid                              # best case (optimistic fill)
    else:  # put_spread / call_spread
        opt_type = "put" if kind == "put_spread" else "call"
        short_strike, long_strike = pos["short_strike"], pos["long_strike"]
        short_leg = _leg_prices(chain, opt_type, short_strike)
        long_leg = _leg_prices(chain, opt_type, long_strike)
        if not (short_leg and long_leg):
            raise RuntimeError(f"leg(s) not found: {ticker} {short_strike:g}/{long_strike:g}{opt_type[0].upper()} {exp}")
        s_bid, s_ask, _s_prev = short_leg
        l_bid, l_ask, _l_prev = long_leg
        cost_to_close = s_ask - l_bid                        # worst case: buy back short at ask, sell long at bid
        cost_to_close_bid = s_bid - l_ask                    # best case: buy back short at bid, sell long at ask

    current_price = ws.td_quote(ticker)   # the underlying stock's live price, not the option's

    unrealized_pl = entry_credit - cost_to_close
    unrealized_pl_pct = (unrealized_pl / entry_credit) if entry_credit else float("nan")

    # Open cash-secured puts only: MaxLoss here is how far the stock has already
    # fallen through the strike, net of the premium collected (strike - current
    # price - entry credit, floored at 0 -- $0 while OTM or still within the
    # premium cushion), not the stock-to-zero worst case _max_loss_per_share
    # gives everything else. Deliberately local to this table -- the pivot/
    # Financials tables use their own _pivot_max_loss_per_share, unchanged.
    if kind == "put" and current_price:
        max_loss = max(0.0, pos["strike"] - float(current_price) - entry_credit)
    else:
        max_loss = _max_loss_per_share(pos)

    return {"Ticker": ticker, "Type": TYPE_LABELS.get(kind, kind), "Strike": _strikes_display(pos),
            "CurrentPrice": (round(current_price, 2) if current_price else float("nan")),
            "Expiration": exp, "DTE": dte, "DaysHeld": days_held, "Opened": entry_date_str or "-",
            "Contracts": contracts, "EntryCredit": entry_credit,
            "CostToCloseBid": round(cost_to_close_bid, 2), "CostToClose": round(cost_to_close, 2),
            "UnrealizedGL_$": round(unrealized_pl * 100 * contracts, 2),
            "UnrealizedGL_%": unrealized_pl_pct, "MaxLoss": round(max_loss, 2)}


def build_positions_table():
    """Evaluates every OPEN_POSITIONS entry that isn't auto-closed (see
    _is_expired_unclosed -- an expiration 1+ day in the past with no explicit
    CLOSED_POSITIONS entry moves to build_closed_positions_table() instead).
    Returns (dataframe, errors) -- a position that fails (bad ticker, expired,
    contract not found) is reported as an error string rather than silently
    dropped, same pattern as screen_puts/screen_calls's error handling."""
    import pandas as pd
    today = dt.date.today()
    rows, errs = [], []
    for pos in ws.OPEN_POSITIONS:
        if _is_expired_unclosed(pos, today):
            continue
        try:
            rows.append(evaluate_position(pos, today))
        except Exception as e:
            errs.append(f"{pos.get('ticker', '?')}: {e}")
            print(f"POSITION {pos.get('ticker', '?')}: ERROR {e}", file=sys.stderr)
    if not rows:
        return pd.DataFrame(columns=POSITIONS_COLS), errs
    df = _sort_grouped(pd.DataFrame(rows)[POSITIONS_COLS], "DTE")
    return df, errs


# --- Credit-spread management rules (Options Alpha style), see build_spread_actions_table ---
SPREAD_PROFIT_TARGET = {1: 0.50, 2: 0.65, 3: 0.50}   # take profit once this share of the credit is captured, by group
SPREAD_STOP_MULT = 2.0      # stop once buy-back reaches 2x the credit
G1_MAX_DTE = 20             # Group 1: entered under 21 DTE
G2_MAX_DTE = 45             # Group 2: entered 21-45 DTE (core); Group 3: over 45
G1_EXIT_DTE = 5             # Group 1: close by 5 DTE
G2_EXIT_DTE = 7             # Group 2: close by 7 DTE
CHECK_21_DTE = 21           # Group 2's "21 DTE check"
CHECK_21_WINDOW = 2         # ...also applied for 2 extra days (21-23 DTE) so a weekend can't skip it
CLOSE_PROFIT_21 = 0.40      # at the 21 DTE check, close if already > 40% profitable
TESTED_PCT = 0.02           # short strike is "tested" if the stock is through it or within 2% of it
DEAD_TRADE_BAND = 0.10      # Group 3 "flat": buy-back still within 10% of the original credit
ROLL_CHECK_TEXT = "21 DTE CHECK: short strike tested"
SPREAD_ACTIONS_COLS = ["Ticker", "Type", "Strike", "CurrentPrice", "DTE", "Group", "Contracts",
                       "EntryCredit", "CostToClose", "TargetBTC", "StopBTC", "UnrealizedGL", "Action"]
SPREAD_ACTIONS_RAW_COLS = ["Ticker", "Type", "Strike", "Contracts", "Expiration", "CostToCloseBid",
                           "CostToClose", "CurrentPrice", "Kind", "RollExp", "RollSell", "RollBuy",
                           "RollNetLow", "RollNetHigh", "CStrike", "CContracts",
                           "CCostToCloseBid", "CCostToClose"]


def _spread_group(dte, days_held):
    """Rule group, set by the DTE the trade was ENTERED at (a management plan
    is chosen at entry -- otherwise Group 2's 21-DTE check and 7-DTE exit
    could never fire, since a 30-DTE trade is under 21 DTE by then). A Group 3
    trade (entered > 45 DTE) switches to Group 2 once it reaches 21 DTE. With
    no entry date, falls back to current DTE."""
    orig = dte + days_held if days_held == days_held else dte
    if orig <= G1_MAX_DTE:
        return 1
    if orig <= G2_MAX_DTE:
        return 2
    return 3 if dte > CHECK_21_DTE else 2


def spread_action(kind, short_strike, price, dte, days_held, credit, cost):
    """(rank, action text) for one credit spread, or None if no rule fires.
    kind is "put" or "call" (which side the spread is on). credit is the
    entry credit per share (C); cost is the live ASK to close per share, the
    same conservative basis UnrealizedGL uses. Rules in priority order:
    stop, time exit, Group 1 ITM short strike, profit target, 21-DTE check,
    Group 3 dead-trade check. Roll suggestions are advisory only -- no live
    next-month quote is fetched."""
    if not credit or credit <= 0 or dte is None or price != price:
        return None
    if kind == "put":
        itm = price < short_strike
        tested = price <= short_strike * (1 + TESTED_PCT)
    else:
        itm = price > short_strike
        tested = price >= short_strike * (1 - TESTED_PCT)
    return _apply_rules(dte, days_held, credit, cost, itm, tested)


def condor_action(put_short, call_short, price, dte, days_held, credit, cost):
    """Same rules as spread_action, applied to an iron condor as ONE position:
    credit and cost are the COMBINED figures (any consistent unit -- the rules
    only use ratios, so the table passes dollar totals, which also handles a
    condor whose two sides have different contract counts). "ITM" / "tested"
    mean EITHER short strike is through / within TESTED_PCT of the price."""
    if not credit or credit <= 0 or dte is None or price != price:
        return None
    itm = price < put_short or price > call_short
    tested = price <= put_short * (1 + TESTED_PCT) or price >= call_short * (1 - TESTED_PCT)
    return _apply_rules(dte, days_held, credit, cost, itm, tested)


def _apply_rules(dte, days_held, credit, cost, itm, tested):
    group = _spread_group(dte, days_held)
    profit_frac = (credit - cost) / credit

    if cost >= SPREAD_STOP_MULT * credit:
        return 0, f"STOP: close (cost to close >= {SPREAD_STOP_MULT:g}x credit)"
    exit_dte = G1_EXIT_DTE if group == 1 else G2_EXIT_DTE if group == 2 else None
    if exit_dte is not None and dte <= exit_dte:
        return 1, f"TIME EXIT: close by {exit_dte} DTE" + (" (short strike ITM)" if itm else "")
    if group == 1 and itm:
        return 1, "CLOSE: don't hold an ITM short strike into the last days"
    target = SPREAD_PROFIT_TARGET[group]
    if profit_frac >= target:
        return 2, f"TAKE PROFIT: buy back (>= {target:.0%} of credit captured)"
    if CHECK_21_DTE <= dte <= CHECK_21_DTE + CHECK_21_WINDOW and group == 2:
        if profit_frac > CLOSE_PROFIT_21:
            return 3, f"21 DTE CHECK: close (> {CLOSE_PROFIT_21:.0%} profit)"
        if profit_frac < 0 and tested:
            return 3, ROLL_CHECK_TEXT
    orig_dte = dte + days_held if days_held == days_held else None
    if (orig_dte is not None and orig_dte > G2_MAX_DTE and days_held >= orig_dte / 2
            and dte > CHECK_21_DTE and abs(cost - credit) <= DEAD_TRADE_BAND * credit):
        return 4, "DEAD TRADE: halfway through and flat -- consider closing"
    return None


def _find_roll(ticker, exp, kind, cost, cost_bid, contracts):
    """Best roll for a losing credit spread -> (text, roll), where roll is a dict
    for the chosen roll or None if there isn't one (text then says why).
    A roll = buy the current spread back at the ASK (`cost`, conservative, same
    basis as the rest of the table) and sell a NEW spread on the same side, in a
    LATER expiration, for a NET CREDIT (worst-case bid/ask credit, same basis
    the screener uses) -- never a net debit. The new spread must be one the
    screener itself would show: candidates come from spreads._for_expiration,
    the exact function behind the Multi-Leg tab, so every screener criterion
    already applies (POP, OTM incl. the tech 15%/10%+$5k gate, AnnROR, OTM vs
    IV, the $1,000 premium floor, open interest, earnings-window exclusion,
    SPREAD_DTE_MIN..MAX). Deliberately NOT gated by open_position_sides -- the
    spread being rolled is itself what would block it. Same contract count as
    the position. Among qualifying rolls, the highest screener Score wins.
    roll = {exp, dte, sell, buy (strikes), net_low (worst case: new credit at
    the bid/ask minus buy-back at the ask), net_high (best case: new credit at
    the ask/bid minus buy-back at the bid)}, all per share."""
    strat = "Put credit spread" if kind == "put" else "Call credit spread"
    today = dt.date.today()
    cur = dt.date.fromisoformat(exp)
    price = ws.td_quote(ticker)
    if not price:
        raise RuntimeError("no quote")
    price = float(price)
    earnings = ws.get_earnings_date(ticker)
    cands = []
    for e, d, dte in sp._all_expirations(ticker, today):
        if d <= cur or not (sp.SPREAD_DTE_MIN <= dte <= sp.SPREAD_DTE_MAX):
            continue
        if ws.earnings_blocks(ticker, earnings, today, d):
            continue
        chain = ws.td_chain(ticker, e)
        if chain:
            cands += [r for r in sp._for_expiration(ticker, price, e, dte, earnings, chain)
                      if r["Strategy"] == strat]
    if not cands:
        return "no spread in a later expiration passes the screener criteria", None
    scored = [(r["Max Profit"] - cost, r) for r in cands]
    viable = [(net, r) for net, r in scored if net > 0]
    if not viable:
        best = max(net for net, _ in scored)
        return (f"{len(cands)} later spread(s) pass the screener but the best would be a "
                f"${abs(best):.2f}/sh net debit"), None
    net, r = max(viable, key=lambda t: t[1]["Score"] if t[1]["Score"] == t[1]["Score"] else float("-inf"))
    legs = r["Put Legs"] or r["Call Legs"]
    sell, buy = (float(x) for x in re.search(r"sell ([\d.]+)[PC] / buy ([\d.]+)[PC]", legs).groups())
    roll = {"exp": r["Expiration"], "dte": r["DTE"], "sell": sell, "buy": buy,
            "net_low": net, "net_high": r["Max Profit (Best)"] - cost_bid}
    return (f"ROLL to {r['Expiration']} ({r['DTE']} DTE): {legs} for ${r['Max Profit']:.2f} credit "
            f"-> net +${net:.2f}/sh (+${net * 100 * contracts:,.0f}), OTM {r['OTM_%']:.1%}, "
            f"POP {r['POP_%']:.0%}, AnnROR {r['AnnROR_%']:.0%}"), roll


def _try_roll(ticker, exp, kind, cost, cost_bid, n, action, label="", closing="CLOSE"):
    """Roll-before-close for a LOSING position whose rule says to get out:
    -> (new action text, roll dict or None). Falls back to `closing` (with the
    reason) when there is no qualifying roll, or the search itself fails."""
    try:
        text, roll = _find_roll(ticker, exp, kind, cost, cost_bid, n)
    except Exception as e:
        print(f"ROLL SEARCH {ticker}: ERROR {e}", file=sys.stderr)
        return f"{action} -> {closing} (roll search failed: {e})", None
    if roll:
        return f"{action} -> {label}{text}", roll
    return f"{action} -> {closing} (no roll: {text})", None


def build_spread_actions_table(df):
    """Credit spreads from build_positions_table()'s raw output that currently
    trip a management rule (see spread_action), with the action to take.
    Only positions that need action appear -- an empty result means nothing is
    triggered. Priced from the rows already quoted, plus (only for a LOSING
    position whose rule says to get out) the roll search in _find_roll -- a
    roll for a net credit that passes the screener beats a plain CLOSE.

    IRON CONDORS are evaluated as ONE position: a put spread and a call spread
    on the same ticker and expiration (exactly one of each, nothing else on
    that ticker/expiry) are combined into a single "Iron Condor" row, judged on
    the combined dollar credit vs the combined dollar cost to close (see
    condor_action). Dollar totals, not per-share, because the two sides can
    have different contract counts (e.g. 60 put spreads / 28 call spreads).
    The condor's group comes from its OLDEST leg's entry (when the trade was
    opened; a later leg completing the condor doesn't restart the plan). A
    condor's roll search runs on its worse-losing side only -- the other side
    stays open. Open Positions itself still lists the two sides separately.

    Returns (display_df, raw_df), same row order: display_df is the formatted
    table; raw_df carries the unformatted numbers the click-to-copy summaries
    need (Kind is "roll" only when a roll was found, else "close" -- covers
    STOP/TIME EXIT/TAKE PROFIT/CLOSE/DEAD TRADE alike, all a buy-back; a
    condor close row carries both legs, a condor roll row just the rolled side)."""
    import pandas as pd
    PUT, CALL = TYPE_LABELS["put_spread"], TYPE_LABELS["call_spread"]
    spreads = [r for _, r in df.iterrows() if r["Type"] in (PUT, CALL)]
    groups = {}
    for r in spreads:
        groups.setdefault((r["Ticker"], r["Expiration"]), []).append(r)
    condors = {k for k, g in groups.items()
               if len(g) == 2 and {x["Type"] for x in g} == {PUT, CALL}}
    rows = []
    nan = float("nan")

    def _raw(r, n, kind, roll, condor_call=None):
        d = {"Ticker": r["Ticker"], "Type": r["Type"], "Strike": r["Strike"], "Contracts": n,
             "Expiration": r["Expiration"], "CostToCloseBid": r["CostToCloseBid"],
             "CostToClose": r["CostToClose"], "CurrentPrice": r["CurrentPrice"], "Kind": kind,
             "RollExp": roll["exp"] if roll else None,
             "RollSell": roll["sell"] if roll else nan, "RollBuy": roll["buy"] if roll else nan,
             "RollNetLow": roll["net_low"] if roll else nan,
             "RollNetHigh": roll["net_high"] if roll else nan,
             "CStrike": None, "CContracts": nan, "CCostToCloseBid": nan, "CCostToClose": nan}
        if condor_call is not None:
            c, cn = condor_call
            d.update({"Type": "Iron Condor", "CStrike": c["Strike"], "CContracts": cn,
                      "CCostToCloseBid": c["CostToCloseBid"], "CCostToClose": c["CostToClose"]})
        return d

    for r in spreads:
        if (r["Ticker"], r["Expiration"]) in condors:
            continue
        kind = "put" if r["Type"] == PUT else "call"
        credit, cost, n = r["EntryCredit"], r["CostToClose"], int(r["Contracts"])
        hit = spread_action(kind, float(str(r["Strike"]).split("/")[0]), r["CurrentPrice"],
                            r["DTE"], r["DaysHeld"], credit, cost)
        if not hit:
            continue
        rank, action = hit
        # Rolling takes priority over closing for a LOSING spread: whenever a rule
        # says to get out (STOP, TIME EXIT, Group 1 ITM close, or the 21 DTE check
        # on a tested short strike), look for a net-credit roll that passes the
        # screener first (_find_roll); only if there is none is it a CLOSE.
        # Winners (TAKE PROFIT, 21 DTE close above 40%) and DEAD TRADE never roll.
        roll = None
        if cost > credit and (rank in (0, 1) or action == ROLL_CHECK_TEXT):
            action, roll = _try_roll(r["Ticker"], r["Expiration"], kind, cost,
                                     r["CostToCloseBid"], n, action)
        group = _spread_group(r["DTE"], r["DaysHeld"])
        rows.append((rank, r["DTE"], {
            "Ticker": r["Ticker"], "Type": r["Type"], "Strike": r["Strike"],
            "CurrentPrice": f"${r['CurrentPrice']:.2f}", "DTE": int(r["DTE"]),
            "Group": {1: "1 (entered <21 DTE)", 2: "2 (entered 21-45)",
                      3: "3 (entered >45)"}[group],
            "Contracts": str(n), "EntryCredit": f"${credit:.2f}", "CostToClose": f"${cost:.2f}",
            "TargetBTC": f"${credit * (1 - SPREAD_PROFIT_TARGET[group]):.2f}",
            "StopBTC": f"${credit * SPREAD_STOP_MULT:.2f}",
            "UnrealizedGL": f"{_fmt_dollar_signed(r['UnrealizedGL_$'])} ({_fmt_pct_signed(r['UnrealizedGL_%'])})",
            "Action": action}, _raw(r, n, "roll" if roll else "close", roll)))

    for key in sorted(condors):
        put = next(x for x in groups[key] if x["Type"] == PUT)
        call = next(x for x in groups[key] if x["Type"] == CALL)
        n_p, n_c = int(put["Contracts"]), int(call["Contracts"])
        credit_p, credit_c = put["EntryCredit"] * 100 * n_p, call["EntryCredit"] * 100 * n_c
        cost_p, cost_c = put["CostToClose"] * 100 * n_p, call["CostToClose"] * 100 * n_c
        credit, cost = credit_p + credit_c, cost_p + cost_c
        held = [x for x in (put["DaysHeld"], call["DaysHeld"]) if x == x]
        days_held = max(held) if held else nan
        dte, price = put["DTE"], put["CurrentPrice"]
        hit = condor_action(float(str(put["Strike"]).split("/")[0]),
                            float(str(call["Strike"]).split("/")[0]),
                            price, dte, days_held, credit, cost)
        if not hit:
            continue
        rank, action = hit
        roll, rolled = None, None
        if cost > credit and (rank in (0, 1) or action == ROLL_CHECK_TEXT):
            # Only the worse-losing side is rolled; the other side stays open.
            side, srow, sn = max((("Put", put, n_p), ("Call", call, n_c)),
                                 key=lambda t: t[1]["CostToClose"] * 100 * t[2]
                                 - t[1]["EntryCredit"] * 100 * t[2])
            action, roll = _try_roll(put["Ticker"], put["Expiration"], side.lower(),
                                     srow["CostToClose"], srow["CostToCloseBid"], sn, action,
                                     label=f"{side} side: ", closing="CLOSE both sides")
            rolled = (srow, sn) if roll else None
        group = _spread_group(dte, days_held)
        gl = credit - cost
        disp = {
            "Ticker": put["Ticker"], "Type": "Iron Condor",
            "Strike": f"{put['Strike']} | {call['Strike']}",
            "CurrentPrice": f"${price:.2f}", "DTE": int(dte),
            "Group": {1: "1 (entered <21 DTE)", 2: "2 (entered 21-45)",
                      3: "3 (entered >45)"}[group],
            "Contracts": f"{n_p}P / {n_c}C", "EntryCredit": f"${credit:,.0f} total",
            "CostToClose": f"${cost:,.0f} total",
            "TargetBTC": f"${credit * (1 - SPREAD_PROFIT_TARGET[group]):,.0f} total",
            "StopBTC": f"${credit * SPREAD_STOP_MULT:,.0f} total",
            "UnrealizedGL": f"{_fmt_dollar_signed(gl)} ({_fmt_pct_signed(gl / credit)})",
            "Action": action}
        if rolled:
            srow, sn = rolled
            raw = _raw(srow, sn, "roll", roll)
        else:
            raw = _raw(put, n_p, "close", None, condor_call=(call, n_c))
        rows.append((rank, dte, disp, raw))

    if not rows:
        return pd.DataFrame(columns=SPREAD_ACTIONS_COLS), pd.DataFrame(columns=SPREAD_ACTIONS_RAW_COLS)
    rows.sort(key=lambda t: (t[0], t[1]))
    return (pd.DataFrame([t[2] for t in rows])[SPREAD_ACTIONS_COLS],
            pd.DataFrame([t[3] for t in rows])[SPREAD_ACTIONS_RAW_COLS])


def _fmt_pct_signed(v):
    return "-" if v != v else f"{v*100:+.1f}%"


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


def _all_closed():
    """CLOSED_POSITIONS plus every OPEN_POSITIONS entry that's auto-closed
    (see _is_expired_unclosed) -- the single source both
    build_closed_positions_table() and the Financials functions (via
    _closed_in_window) read from, so an expired position appears consistently
    across every view without needing an explicit CLOSED_POSITIONS entry."""
    today = dt.date.today()
    auto = [_auto_closed_entry(p) for p in ws.OPEN_POSITIONS if _is_expired_unclosed(p, today)]
    return list(ws.CLOSED_POSITIONS) + auto


def build_closed_positions_table(window_days=30):
    """Closed positions (recorded + auto-closed, see _all_closed) with an
    exit_date within the last `window_days` (default 30) of today, sorted by
    Closed date ascending (most recently closed last). Returns (dataframe,
    errors), same error-reporting pattern as build_positions_table -- a
    malformed entry is reported, not silently dropped."""
    import pandas as pd
    today = dt.date.today()
    rows, errs = [], []
    for pos in _all_closed():
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
    df = _sort_grouped(pd.DataFrame(rows)[CLOSED_COLS], "Closed")
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
    # CostToClose: shown as a bid-to-ask range, NEGATIVE -- what you'd pay/
    # lose to close, not a plain price -- "-$bid to -$ask (-$totBid to
    # -$totAsk)", same bid/ask-range shape every other table uses for its own
    # Premium/Max Profit column, just negated since this is money going out
    # instead of coming in. "to" (not a bare hyphen) separates the two ends
    # so a negative-negative pair doesn't read as a double-dash. Sign-aware
    # (not a blind "-$" prefix): a spread priced so closing it nets a credit
    # instead of a cost is a rare but real possibility (illiquid/wide legs),
    # and negating an already-negative value should show as a gain ("+$"),
    # not a broken "-$-X.XX".
    def _signed_cost(v):
        return f"-${v:,.2f}" if v >= 0 else f"+${abs(v):,.2f}"
    if "CostToClose" in d.columns and "CostToCloseBid" in d.columns and "Contracts" in d.columns:
        def _ctc_range(bid, ask, n):
            if bid != bid or ask != ask:
                return "-"
            if bid == ask:
                return f"{_signed_cost(ask)} ({_signed_cost(ask * 100 * n)})"
            return (f"{_signed_cost(bid)} to {_signed_cost(ask)} "
                   f"({_signed_cost(bid * 100 * n)} to {_signed_cost(ask * 100 * n)})")
        d["CostToClose"] = [_ctc_range(b, a, int(n)) for b, a, n
                            in zip(d["CostToCloseBid"], d["CostToClose"], d["Contracts"])]
        d = d.drop(columns=["CostToCloseBid"])
    elif "CostToClose" in d.columns:
        d["CostToClose"] = d["CostToClose"].apply(lambda v: _signed_cost(v) if v == v else "-")
    for c in ("ExitCost", "CurrentPrice"):
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
    return [pos for pos in _all_closed()
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


def _open_unclosed(today):
    """OPEN_POSITIONS minus whatever's auto-closed (see _is_expired_unclosed)
    -- what build_positions_table() itself shows, reused here so the
    Financials/Concentration tables stay consistent with it."""
    return [p for p in ws.OPEN_POSITIONS if not _is_expired_unclosed(p, today)]


def build_open_financials(dpos_df):
    """Pivoted Financials for OPEN_POSITIONS only (row 1 = Unrealized G/L,
    also ROR%'s basis). See _pivot_table/_pivot_max_loss_per_share for the
    Put/Call/Multi-Leg breakdown and the per-type Max Loss adjustment."""
    today = dt.date.today()
    gl = _pivot_gl(dpos_df, "UnrealizedGL_$")
    entries = _pivot_entries(_open_unclosed(today), lambda pos: today, today)
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
    entries = (_pivot_entries(_open_unclosed(today), lambda pos: today, today)
              + _pivot_entries(closed_list, lambda pos: dt.date.fromisoformat(pos["exit_date"]), today))
    return _pivot_table("G/L (Unrealized + Realized)", gl, entries)


MONTHLY_COLS = ["Month", "# Closed", "Premium Collected", "Realized G/L", "Realized G/L %", "Cumulative G/L"]


def build_monthly_realized_table():
    """One row per calendar month with at least one closed position, oldest
    first, plus a Total row -- a running ledger of realized P&L over time,
    NOT limited to build_closed_positions_table's 30-day display window
    (uses _all_closed(), the same permanent record -- recorded
    CLOSED_POSITIONS plus auto-closed expired OPEN_POSITIONS, see
    _is_expired_unclosed). Reuses evaluate_closed_position's own G/L math
    rather than recomputing it, so this always agrees with the Closed
    Positions table above for whatever's still in its 30-day window.
    Premium Collected is entry_credit x 100 x contracts, same basis
    "Potential Profit Acc." uses in the pivoted Financials tables. Months
    with a net realized LOSS still show a negative Realized G/L % against
    that month's own premium collected (can go below -100% if you gave back
    more than you collected)."""
    import pandas as pd
    by_month = {}
    errs = []
    for pos in _all_closed():
        try:
            exit_date = dt.date.fromisoformat(pos["exit_date"])
            r = evaluate_closed_position(pos)
        except Exception as e:
            errs.append(f"{pos.get('ticker', '?')}: {e}")
            continue
        month_key = exit_date.strftime("%Y-%m")
        b = by_month.setdefault(month_key, {"n": 0, "premium": 0.0, "gl": 0.0})
        b["n"] += 1
        b["premium"] += r["EntryCredit"] * 100 * r["Contracts"]
        b["gl"] += r["RealizedGL_$"]

    rows = []
    cum = 0.0
    for month_key in sorted(by_month):
        b = by_month[month_key]
        cum += b["gl"]
        month_label = dt.datetime.strptime(month_key, "%Y-%m").strftime("%B %Y")
        gl_pct = (b["gl"] / b["premium"]) if b["premium"] else float("nan")
        rows.append([month_label, b["n"], _fmt_dollar(b["premium"]),
                    _fmt_dollar_signed(b["gl"]), _fmt_pct(gl_pct), _fmt_dollar_signed(cum)])

    if rows:
        total_n = sum(by_month[m]["n"] for m in by_month)
        total_prem = sum(by_month[m]["premium"] for m in by_month)
        total_gl = sum(by_month[m]["gl"] for m in by_month)
        total_pct = (total_gl / total_prem) if total_prem else float("nan")
        rows.append(["Total", total_n, _fmt_dollar(total_prem),
                    _fmt_dollar_signed(total_gl), _fmt_pct(total_pct), _fmt_dollar_signed(total_gl)])

    return pd.DataFrame(rows, columns=MONTHLY_COLS), errs


CONCENTRATION_ROWS = ["Tech", "Non-Tech"]
# Unlike PIVOT_COLS (Put/Call/Multi-Leg, used by the Financials tables above,
# which keeps spreads as their own bucket), this table groups by directional
# side instead -- a put spread is still bullish-put-side risk, so it joins
# plain puts under "Put", and a call spread joins plain calls under "Call".
# Same mapping open_position_sides() uses to gate the screener by side.
CONCENTRATION_COLS = ["Put", "Call"]
_TYPE_TO_CONCENTRATION_BUCKET = {"put": "Put", "put_spread": "Put",
                                 "call": "Call", "call_spread": "Call"}


def build_concentration_history_table():
    """Concentration of Positions -- 1D All-Time High & Average (Max Loss):
    one row per sector (Tech/Non-Tech/Total) x directional side (Put/Call/
    Total, via _TYPE_TO_CONCENTRATION_BUCKET -- put spreads join plain
    puts, call spreads join plain calls, not a separate Multi-Leg bucket).
    Each cell shows three numbers side by side: "Now" (today's current Max
    Loss), "ATH" (the highest single-day
    Max Loss ever seen in that bucket), and "Avg" (the average single-day
    Max Loss across every day that bucket had at least one position open) --
    e.g. "Now $12,000 | ATH $18,500 | Avg $9,200" tells you today's risk in
    that corner of the book is above its historical average but below its
    peak. Covers every position EVER held, open or closed -- "in all our
    time doing options" -- using _pivot_max_loss_per_share throughout, the
    exact same risk-scaled convention (puts scaled to a 20% tail estimate,
    covered calls excluded/NaN, spreads unchanged) so ATH/Avg are on the
    same footing as "Now."

    "1D" means what "Max Loss 1D" already means in the Financials tables:
    the SUM of Max Loss across every position open on the same calendar day
    (an interval-overlap sweep), not any single position's own number. The
    sweep here is a plain brute-force day-by-day loop from the earliest
    entry_date on record to today -- this account's history is small enough
    (a handful of months x well under a hundred positions) that the
    days-times-positions cost is trivial; a day where a bucket's sum is $0
    is excluded from the Average so it reflects typical risk carried WHILE
    holding that kind of position, not diluted by days before that category
    ever existed. No live quotes needed -- pure arithmetic over
    OPEN_POSITIONS/CLOSED_POSITIONS, same as the Financials tables."""
    import pandas as pd
    today = dt.date.today()
    cols = CONCENTRATION_COLS + ["Total"]
    sectors = CONCENTRATION_ROWS + ["Total"]

    spans = []   # (start, end_inclusive, loss_total, sector, bucket) for every position ever
    now_val = {s: {c: 0.0 for c in cols} for s in sectors}

    for pos in _open_unclosed(today):
        bucket = _TYPE_TO_CONCENTRATION_BUCKET.get(pos["type"])
        if not bucket:
            continue
        loss = _pivot_max_loss_per_share(pos)
        if loss != loss:
            continue
        sector = ws.get_sector_bucket(pos["ticker"])
        loss_total = loss * 100 * pos["contracts"]
        entry = dt.date.fromisoformat(pos["entry_date"]) if pos.get("entry_date") else today
        spans.append((entry, today, loss_total, sector, bucket))
        for r in (sector, "Total"):
            now_val[r][bucket] += loss_total
            now_val[r]["Total"] += loss_total

    for pos in _all_closed():
        bucket = _TYPE_TO_CONCENTRATION_BUCKET.get(pos["type"])
        if not bucket:
            continue
        loss = _pivot_max_loss_per_share(pos)
        if loss != loss:
            continue
        sector = ws.get_sector_bucket(pos["ticker"])
        loss_total = loss * 100 * pos["contracts"]
        entry = dt.date.fromisoformat(pos["entry_date"]) if pos.get("entry_date") else today
        exit_ = dt.date.fromisoformat(pos["exit_date"])
        spans.append((entry, exit_, loss_total, sector, bucket))

    earliest = min((s[0] for s in spans), default=today)

    daily = {s: {c: [] for c in cols} for s in sectors}
    d = earliest
    while d <= today:
        day_total = {s: {c: 0.0 for c in cols} for s in sectors}
        for start, end, loss_total, sector, bucket in spans:
            if start <= d <= end:
                for r in (sector, "Total"):
                    day_total[r][bucket] += loss_total
                    day_total[r]["Total"] += loss_total
        for s in sectors:
            for c in cols:
                v = day_total[s][c]
                if v:
                    daily[s][c].append(v)
        d += dt.timedelta(days=1)

    def _cell(sector, col):
        vals = daily[sector][col]
        ath = max(vals) if vals else 0.0
        avg = (sum(vals) / len(vals)) if vals else 0.0
        return (f"Now {_fmt_dollar(now_val[sector][col])} | "
               f"ATH {_fmt_dollar(ath)} | Avg {_fmt_dollar(avg)}")

    rows = [(sector, *[_cell(sector, c) for c in cols]) for sector in sectors]
    return pd.DataFrame(rows, columns=["Sector"] + cols)


_TYPE_LABEL_TO_CONCENTRATION_BUCKET = {"Put": "Put", "Covered Call": "Call",
                                       "Put Credit Spread": "Put", "Call Credit Spread": "Call"}


def build_concentration_gl_table(dpos_df):
    """Concentration of Positions on Unrealized G/L: same Sector x Put/Call/
    Total grid, each cell packing three figures together -- Unrealized G/L
    $ and what % of "Potential Profit Acc." (total premium collected -- the
    theoretical max you could ever make if every position in that bucket
    captured its full premium, same basis the Financials tables already
    call "Potential Profit Acc.") that G/L represents, plus the 1-day
    contract premium % change -- e.g. "+$10,000 (20.0% of potential)     |
    +13.8% chg" means only 20%
    of the theoretical max has been captured so far (still 80% of the room
    left to run) AND that today's move was in your favor. The % change
    belongs here, not next to Max Loss, because it's this G/L number that
    it actually explains the movement of.

    G/L (left) reuses the already-live-quoted Open Positions dataframe
    (dpos_df, from build_positions_table()) instead of a fresh chain fetch
    -- Unrealized G/L and EntryCredit are already sitting right there. Put
    spreads join Put, call spreads join Call, same grouping as the other
    Concentration tables (via _TYPE_LABEL_TO_CONCENTRATION_BUCKET, the
    label-keyed twin of _TYPE_TO_CONCENTRATION_BUCKET since dpos_df's
    "Type" column already holds the display label, not the raw
    OPEN_POSITIONS type string).

    Premium change (right, "+X.X%"/"-X.X% chg", always signed) is your P&L
    DIRECTION on that contract, NOT the raw price move -- every position
    here is SHORT (sold to open), so a FALLING contract price is good for
    you (cheaper to buy back) and shows POSITIVE, while a RISING price is
    bad (costlier to close) and shows NEGATIVE. Needs its OWN live chain
    fetch per open position (today's ask vs prevclose, netted across both
    legs for a spread the same way Open Positions' own CostToClose prices
    one) -- unlike the G/L half, this can't be read off dpos_df, which
    doesn't carry yesterday's prices. A position opened TODAY has no real
    "yesterday" -- excluded from yesterday's total (still counted in
    today's). A leg with no prevclose available skips that position's
    yesterday contribution rather than counting it as $0.

    Total row and column included. Returns (dataframe, errors) -- a
    position whose contract/chain can't be found for the % change is
    skipped with an error string rather than silently dropped, same
    pattern as build_positions_table (the G/L half is unaffected since it
    doesn't need a fresh quote)."""
    import pandas as pd
    today = dt.date.today()
    cols = CONCENTRATION_COLS + ["Total"]
    sectors = CONCENTRATION_ROWS + ["Total"]
    grid = {s: {c: {"gl": 0.0, "potential": 0.0, "prem_y": 0.0, "prem_t": 0.0} for c in cols} for s in sectors}
    errs = []

    if len(dpos_df):
        for _, row in dpos_df.iterrows():
            bucket = _TYPE_LABEL_TO_CONCENTRATION_BUCKET.get(row["Type"])
            if not bucket:
                continue
            sector = ws.get_sector_bucket(row["Ticker"])
            potential = row["EntryCredit"] * 100 * row["Contracts"]
            gl = row["UnrealizedGL_$"]
            for r in (sector, "Total"):
                grid[r][bucket]["gl"] += gl
                grid[r]["Total"]["gl"] += gl
                grid[r][bucket]["potential"] += potential
                grid[r]["Total"]["potential"] += potential

    for pos in _open_unclosed(today):
        kind = pos["type"]
        bucket = _TYPE_TO_CONCENTRATION_BUCKET.get(kind)
        if not bucket:
            continue
        sector = ws.get_sector_bucket(pos["ticker"])
        contracts = pos["contracts"]
        ticker, exp = pos["ticker"], pos["expiration"]
        label = f"{ticker} {exp}"
        try:
            chain = ws.td_chain(ticker, exp)
            if not chain:
                raise RuntimeError("no option chain (expired or invalid expiration?)")
            if kind in ("put", "call"):
                strike = pos["strike"]
                leg = _leg_prices(chain, kind, strike)
                if not leg:
                    raise RuntimeError("contract not found")
                _bid, today_val, prevclose_val = leg
                label = f"{ticker} {strike:g}{kind[0].upper()} {exp}"
            else:
                opt_type = "put" if kind == "put_spread" else "call"
                short_strike, long_strike = pos["short_strike"], pos["long_strike"]
                short_leg = _leg_prices(chain, opt_type, short_strike)
                long_leg = _leg_prices(chain, opt_type, long_strike)
                if not (short_leg and long_leg):
                    raise RuntimeError("leg(s) not found")
                _s_bid, s_ask, s_prev = short_leg
                l_bid, _l_ask, l_prev = long_leg
                today_val = s_ask - l_bid
                prevclose_val = (s_prev - l_prev) if (s_prev and l_prev) else 0
                label = f"{ticker} {short_strike:g}/{long_strike:g}{opt_type[0].upper()} {exp}"
        except Exception as e:
            errs.append(f"{label}: {e}")
            continue

        today_total = today_val * 100 * contracts
        for r in (sector, "Total"):
            grid[r][bucket]["prem_t"] += today_total
            grid[r]["Total"]["prem_t"] += today_total

        entry_date_str = pos.get("entry_date")
        opened_today = bool(entry_date_str) and dt.date.fromisoformat(entry_date_str) == today
        if not opened_today and prevclose_val:
            y_total = prevclose_val * 100 * contracts
            for r in (sector, "Total"):
                grid[r][bucket]["prem_y"] += y_total
                grid[r]["Total"]["prem_y"] += y_total

    def _fmt_pct_signed(v):
        return f"{v*100:+.1f}%" if v == v else "-"

    def _cell(cell):
        gl, potential = cell["gl"], cell["potential"]
        pct = (gl / potential) if potential else float("nan")
        y, t = cell["prem_y"], cell["prem_t"]
        # Same flip as before: every position here is SHORT, so a falling
        # contract price is good for your P&L (positive) and a rising one
        # is bad (negative) -- the reverse of the raw price direction.
        chg_pct = ((y - t) / y) if y else float("nan")
        return (f"{_fmt_dollar_signed(gl)} ({_fmt_pct(pct)} of potential)"
               f"     |  {_fmt_pct_signed(chg_pct)} chg")

    rows = [(sector, *[_cell(grid[sector][c]) for c in cols]) for sector in sectors]
    return pd.DataFrame(rows, columns=["Sector"] + cols), errs
