"""
spreads.py -- multi-leg strategy screener (reuses wheel_screener's Tradier data).
All anchored at Options Alpha's 70% POP:
  * Put/Call credit spreads : short leg ~0.30 delta (~70% POP), defined risk.
  * Iron condor             : short put + short call ~0.15 delta each (combined ~0.30), defined risk.
  * Long strangle/straddle  : long put + long call. The straddle (same strike) is picked by literal
    proximity to spot, not delta -- a 50-delta strike can drift meaningfully above spot in a
    high-IV name. Strangle legs (different strikes) are picked symmetrically -- the same % away
    from spot on each side -- then filtered to combined POP 0.70-0.90 (matching-delta legs
    independently would drift asymmetrically for the same reason the straddle strike does).
    Defined risk too (max loss = the debit paid), but the opposite side of the trade from
    everything else here: you're BUYING both legs, POP is "finishes beyond either strike" (not
    "stays between them"), and -- unlike every other strategy -- these REQUIRE a confirmed catalyst
    date (a real reason to expect the stock to move), not just permission to span one -- buying
    premium with nothing scheduled to move it is a pure bet against time decay. That catalyst can be
    this ticker's OWN earnings, or a related ticker's -- see wheel_screener.PEER_TICKERS and
    _catalyst_dates -- since a chip equipment maker's earnings can move a memory maker via demand
    read-through, a direct competitor's results can move its rival, etc.; every known catalyst date
    is tried and whichever produces the closest expiration wins. Only the SINGLE expiration closest
    to (on or after) that catalyst date is used, not every later expiration that also happens to span
    it -- a catalyst trade should concentrate exposure around the event, not scatter near-duplicate
    candidates across every weekly that comes after it. That expiration search isn't bounded by
    SPREAD_DTE_MIN/MAX like every other strategy here -- a catalyst falling further out than
    SPREAD_DTE_MAX still gets a long candidate at the nearest expiration after it -- but it is capped
    at LONG_DTE_MAX (90 days): a catalyst further out than that is too far away to be worth tying up
    capital in a long-premium position waiting for it. And within that one expiration,
    at most one straddle AND one strangle are kept per ticker (not one overall winner) -- whichever
    strangle width needs the smallest move to breakeven, not whichever has the tightest-OTM strikes
    (those can differ: a tighter strike often costs enough extra debit to push its own breakeven
    further away than a cheaper, slightly-wider alternative).
Undefined-risk SHORT strangles/straddles are excluded. Max Profit = net credit received for the
credit strategies; for the long strangle/straddle it's an IV-implied expected-move estimate, not a
guaranteed number (there's no real cap on a long strangle's upside).
"""
import datetime as dt
import math
import pandas as pd
import wheel_screener as ws

SHORT_DELTA      = 0.30    # one-sided credit-spread short leg
SHORT_DELTA_TOL  = 0.08    # accept 0.22-0.38
IC_LEG_DELTA     = 0.15    # iron condor / strangle: each short leg
IC_LEG_TOL       = 0.08    # accept 0.07-0.23 each (combined ~0.30 -> ~70% POP)
SHORT_DELTAS   = [0.30, 0.25, 0.20, 0.15]   # scan credit-spread shorts -> POP 70-85%
IC_LEG_DELTAS  = [0.15, 0.12, 0.10]         # scan iron-condor legs -> combined POP 70-80%
LONG_STRANGLE_OTM_PCTS = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]
                                            # scan long STRANGLE legs -- symmetric %-from-spot on
                                            # each side (NOT delta-matched independently, which
                                            # drifts asymmetrically -- see _long_strangle). The ATM
                                            # straddle is picked separately, by spot proximity --
                                            # see _atm_straddle. POP (checked after selection) still
                                            # needs to clear the same 70-90% floor as everywhere else.
SCAN_TOL       = 0.04                        # tolerance when matching a target delta
SPREAD_WIDTH_PCT = 0.05    # long strike ~5% from the short strike
MIN_CREDIT       = 0.05    # ignore trivial credits
MIN_DEBIT        = 0.05    # ignore trivial debits (long strangle/straddle)
ROR_ANN_MIN      = 0.25    # defined-risk: min annualized return-on-risk
SPREAD_POP_MIN   = 0.70    # at least 70% POP (own band, independent of puts/calls)
SPREAD_POP_MAX   = 1.0     # no upper cap
SPREAD_DTE_MIN   = 7       # spreads have their OWN expiration window
SPREAD_DTE_MAX   = 40      # no long-dated contracts
LONG_DTE_MAX     = 90      # long strangle/straddle's earnings-catalyst expiration can run past
                           # SPREAD_DTE_MAX (see screen_spreads), but not indefinitely
SPREAD_MIN_OTM_OVER_IV = 0.15  # each short leg's OTM must be >= this fraction of its IV. 0 to disable.
SPREAD_CASH_TARGET = 25000  # capital target for "# of contracts" -- lower than wheel_screener's
                            # CASH_TARGET (40,000) since multi-leg risk is defined/capped per contract.

SPREAD_COLS = ["Ticker", "CurrentPrice", "Strategy", "Put Legs", "Call Legs", "Expiration", "DTE",
               "OTM_%", "Width", "Width_%", "Max Profit", "Max Profit (Best)", "Breakeven",
               "AvgPremium", "AnnROR_%", "ROR_%", "POP_%", "IV", "Score", "# of contracts", "MaxLoss",
               "OpenInt", "EarningsDate",
               # Iron condor only -- each side's own best-case credit, NaN everywhere else.
               # Not for display (always dropped from the visible table in
               # 1_Options_Screener.py): exists purely so the click-to-copy summary can
               # quote the put spread and call spread as two independent legs, since the
               # user's broker has no native iron condor order type and has to place them
               # as two separate spread orders anyway.
               "Put Max Profit (Best)", "Call Max Profit (Best)"]
PCT_COLS = {"OTM_%", "Width_%", "ROR_%", "AnnROR_%", "POP_%", "IV"}


def _find_by_delta(chain, opt_type, target, tol):
    best, bestdiff = None, 1e9
    for o in chain:
        if o["type"] != opt_type or o.get("delta") is None:
            continue
        if (o.get("bid") or 0) <= 0:
            continue
        diff = abs(abs(o["delta"]) - target)
        if diff < bestdiff:
            best, bestdiff = o, diff
    if best is not None and abs(abs(best["delta"]) - target) <= tol:
        return best
    return None


def _leg_at(chain, opt_type, strike):
    for o in chain:
        if o["type"] == opt_type and abs(o["strike"] - strike) < 1e-6:
            return o
    return None


def _long_strike(chain, opt_type, short_strike):
    strikes = sorted({o["strike"] for o in chain if o["type"] == opt_type})
    if opt_type == "put":
        target = short_strike * (1 - SPREAD_WIDTH_PCT)
        cand = [s for s in strikes if s < short_strike]
    else:
        target = short_strike * (1 + SPREAD_WIDTH_PCT)
        cand = [s for s in strikes if s > short_strike]
    return min(cand, key=lambda s: abs(s - target)) if cand else None


def _credit_spread(chain, opt_type, target_delta, tol):
    short = _find_by_delta(chain, opt_type, target_delta, tol)
    if not short:
        return None
    ls = _long_strike(chain, opt_type, short["strike"])
    if ls is None:
        return None
    lng = _leg_at(chain, opt_type, ls)
    if not lng:
        return None
    if ws.MIN_OPEN_INTEREST > 0:
        for leg in (short, lng):
            if (leg.get("oi") or 0) < ws.MIN_OPEN_INTEREST:
                return None
    credit = (short["bid"] or 0) - (lng["ask"] or 0)          # worst case: sell at bid, buy at ask
    credit_best = (short["ask"] or 0) - (lng["bid"] or 0)      # best case: sell at ask, buy at bid
    width = abs(short["strike"] - ls)
    if credit < MIN_CREDIT or width <= 0:
        return None
    max_loss = width - credit
    if max_loss <= 0:
        return None
    return {"short": short, "long": lng, "long_strike": ls, "credit": credit,
            "credit_best": credit_best, "width": width, "max_loss": max_loss}


def _leg_liquidity(*legs):
    """Minimum open interest across a spread's legs."""
    ois = [int(l.get("oi") or 0) for l in legs]
    return min(ois) if ois else 0


def _avg_credit_dollars(sym, dte, kind, short_strike, long_strike, spot, short_iv=None, long_iv=None):
    """Estimated typical PERIOD (not yet annualized) net credit [low, high] in
    dollars for one side (put or call) of a spread. wheel_screener.avg_premium_range
    now returns an ANNUALIZED YIELD fraction per leg (conditioned on that leg's own
    live IV where enough historical data exists at that vol regime), so each leg's
    yield is de-annualized and converted back to a dollar premium at its own live
    strike (spot, for calls -- matches evaluate_call's own basis) before netting.

    Pairs each leg's SAME percentile rank (low-with-low, high-with-high), not
    opposite extremes. Both legs are driven by the same underlying's realized-vol
    series (build_history.py computes one rv per ticker, shared by every strike),
    so a rich-vol day tends to make BOTH legs richer together -- not one leg rich
    while the other happens to be at its calmest, which essentially never happens
    on any real day. Pairing opposite extremes combines day-pairs that never
    actually co-occurred, wildly overstating the range (this produced unusable
    bands in practice, e.g. "0.0%-8877.9%" on a live iron condor)."""
    so = (spot - short_strike) / spot if kind == "put" else (short_strike - spot) / spot
    lo = (spot - long_strike) / spot if kind == "put" else (long_strike - spot) / spot
    s_yield = ws.avg_premium_range(sym, so, dte, kind, iv=short_iv)
    l_yield = ws.avg_premium_range(sym, lo, dte, kind, iv=long_iv)
    if not (s_yield and l_yield) or dte <= 0:
        return None
    period = dte / 365.0

    def _prem(y, own_strike):
        basis = own_strike if kind == "put" else spot
        return y * period * basis

    credit_lo = max(0.0, _prem(s_yield[0], short_strike) - _prem(l_yield[0], long_strike))
    credit_hi = max(0.0, _prem(s_yield[1], short_strike) - _prem(l_yield[1], long_strike))
    return (credit_lo, credit_hi)


def _ann_ror_range(credit_range, width, dte):
    """Converts a period $ credit range into an annualized ROR range against
    `width` -- same math the live Max Profit/AnnROR_% uses (ROR = credit /
    (width - credit), annualized by 365/dte). Directly comparable to AnnROR_%."""
    if not credit_range or width <= 0 or dte <= 0:
        return None
    credit_lo, credit_hi = credit_range
    max_loss_lo = width - credit_lo   # low credit -> high max loss -> worst (lowest) ROR
    max_loss_hi = width - credit_hi   # high credit -> low max loss -> best (highest) ROR
    if max_loss_lo <= 0 or max_loss_hi <= 0:
        return None
    ann = 365.0 / dte
    return ((credit_lo / max_loss_lo) * ann, (credit_hi / max_loss_hi) * ann)


def _defined_row(sym, spot, exp, dte, earn, strat, put_legs, call_legs,
                 credit, credit_best, width, max_loss, pop, iv, otm, oi=0, avg_credit=None):
    ror = credit / max_loss if max_loss > 0 else float("nan")
    ann = ror * 365.0 / dte if dte else float("nan")
    # Same composite score as puts/calls, with AnnROR standing in for AnnYield.
    score = (ann / (iv ** ws.SCORE_IV_EXP) * (pop ** ws.SCORE_POP_EXP) * ((365.0 / dte) ** ws.SCORE_DTE_EXP)
             if (dte and dte > 0 and iv and iv > 0 and ann == ann and pop == pop)
             else float("nan"))
    return {"Ticker": sym, "CurrentPrice": round(spot, 2), "Strategy": strat,
            "Put Legs": put_legs, "Call Legs": call_legs,
            "Expiration": exp, "DTE": dte, "OTM_%": otm,
            "Width": round(width, 2), "Width_%": (width / spot if spot else float("nan")),
            "Max Profit": round(credit, 2), "Max Profit (Best)": round(credit_best, 2),
            "Breakeven": "-",
            "MaxLoss": round(max_loss, 2),
            "ROR_%": ror, "AnnROR_%": ann, "POP_%": pop, "IV": iv,
            "Score": (round(score, 2) if score == score else float("nan")),
            "AvgPremium": (f"{avg_credit[0]*100:.1f}%-{avg_credit[1]*100:.1f}%" if avg_credit else "-"),
            "# of contracts": ws.contracts_for_target(max_loss * 100, target=SPREAD_CASH_TARGET),
            "OpenInt": int(oi), "EarningsDate": earn}


def _ok_defined(r, pmin, pmax):
    return (pmin <= r["POP_%"] <= pmax) and (r["AnnROR_%"] >= ROR_ANN_MIN)


def _for_expiration(sym, spot, exp, dte, earn, chain):
    out, seen = [], set()
    pmin = SPREAD_POP_MIN            # floor only - no upper POP cap
    omin = ws.otm_min_for(sym)

    # Credit spreads: scan several short-leg deltas so POP ranges from the floor upward
    for td in SHORT_DELTAS:
        for opt_type in ("put", "call"):
            s = _credit_spread(chain, opt_type, td, SCAN_TOL)
            if not s:
                continue
            sk = s["short"]["strike"]
            otm = (spot - sk) / spot if opt_type == "put" else (sk - spot) / spot
            if otm < omin:
                continue
            siv = s["short"].get("iv") or 0
            if SPREAD_MIN_OTM_OVER_IV > 0 and siv > 0 and otm < SPREAD_MIN_OTM_OVER_IV * siv:
                continue
            pop = 1 - abs(s["short"]["delta"])
            if pop < pmin:
                continue
            key = (opt_type, sk, s["long_strike"])
            if key in seen:
                continue
            if opt_type == "put":
                strat, pl, cl = "Put credit spread", f"sell {sk:g}P / buy {s['long_strike']:g}P", ""
            else:
                strat, pl, cl = "Call credit spread", "", f"sell {sk:g}C / buy {s['long_strike']:g}C"
            oi = _leg_liquidity(s["short"], s["long"])
            _acd = _avg_credit_dollars(sym, dte, opt_type, sk, s["long_strike"], spot,
                                       short_iv=s["short"].get("iv"), long_iv=s["long"].get("iv"))
            acr = _ann_ror_range(_acd, s["width"], dte)
            r = _defined_row(sym, spot, exp, dte, earn, strat, pl, cl,
                             s["credit"], s["credit_best"], s["width"], s["max_loss"], pop,
                             s["short"].get("iv") or 0, otm, oi, acr)
            if r["AnnROR_%"] >= ROR_ANN_MIN:
                seen.add(key)
                out.append(r)

    # Iron condors: scan several per-leg deltas -> combined POP from the floor upward
    for td in IC_LEG_DELTAS:
        ps = _credit_spread(chain, "put", td, SCAN_TOL)
        cs = _credit_spread(chain, "call", td, SCAN_TOL)
        if not (ps and cs):
            continue
        p_otm = (spot - ps["short"]["strike"]) / spot
        c_otm = (cs["short"]["strike"] - spot) / spot
        if p_otm < omin or c_otm < omin:
            continue
        p_iv = ps["short"].get("iv") or 0
        c_iv = cs["short"].get("iv") or 0
        if SPREAD_MIN_OTM_OVER_IV > 0:
            if (p_iv > 0 and p_otm < SPREAD_MIN_OTM_OVER_IV * p_iv) or \
               (c_iv > 0 and c_otm < SPREAD_MIN_OTM_OVER_IV * c_iv):
                continue
        credit = ps["credit"] + cs["credit"]
        credit_best = ps["credit_best"] + cs["credit_best"]
        width = max(ps["width"], cs["width"])
        max_loss = width - credit
        if max_loss <= 0:
            continue
        pop = 1 - (abs(ps["short"]["delta"]) + abs(cs["short"]["delta"]))
        if pop < pmin:
            continue
        key = ("ic", ps["short"]["strike"], cs["short"]["strike"])
        if key in seen:
            continue
        iv = ((ps["short"].get("iv") or 0) + (cs["short"].get("iv") or 0)) / 2
        oi = _leg_liquidity(ps["short"], ps["long"], cs["short"], cs["long"])
        _pcd = _avg_credit_dollars(sym, dte, "put", ps["short"]["strike"], ps["long_strike"], spot,
                                   short_iv=ps["short"].get("iv"), long_iv=ps["long"].get("iv"))
        _ccd = _avg_credit_dollars(sym, dte, "call", cs["short"]["strike"], cs["long_strike"], spot,
                                   short_iv=cs["short"].get("iv"), long_iv=cs["long"].get("iv"))
        if _pcd and _ccd:
            _combined_credit = (_pcd[0] + _ccd[0], _pcd[1] + _ccd[1])
            acr = _ann_ror_range(_combined_credit, width, dte)
        else:
            acr = None
        r = _defined_row(sym, spot, exp, dte, earn, "Iron condor",
                         f"sell {ps['short']['strike']:g}P / buy {ps['long_strike']:g}P",
                         f"sell {cs['short']['strike']:g}C / buy {cs['long_strike']:g}C",
                         credit, credit_best, width, max_loss, pop, iv, min(p_otm, c_otm), oi, acr)
        # Each side's own best-case credit (see SPREAD_COLS) -- not displayed,
        # just carried through for the click-to-copy summary.
        r["Put Max Profit (Best)"] = round(ps["credit_best"], 2)
        r["Call Max Profit (Best)"] = round(cs["credit_best"], 2)
        if r["AnnROR_%"] >= ROR_ANN_MIN:
            seen.add(key)
            out.append(r)
    return out


def _long_strangle(chain, spot, otm_pct):
    """Buys an OTM put + OTM call, each `otm_pct` away from spot -- a
    genuinely symmetric strangle. NOT matched by delta independently per
    leg: that was the original approach, but delta and OTM% aren't
    symmetric between puts and calls (same root cause as the ATM straddle
    picking a drifted strike -- see _atm_straddle's docstring -- the
    same-delta strike sits near the forward price, not spot, and that
    drift compounds differently for a put than a call), which produced
    strangles with wildly uneven OTM% on each side (e.g. a put 0.3% OTM
    paired with a call 4.5% OTM). Finds the nearest LISTED strike to each
    symmetric target instead; POP is still computed from the resulting
    legs' actual deltas afterward, same as before."""
    put_strikes = [o["strike"] for o in chain if o["type"] == "put"]
    call_strikes = [o["strike"] for o in chain if o["type"] == "call"]
    if not (put_strikes and call_strikes):
        return None
    put_strike = min(put_strikes, key=lambda k: abs(k - spot * (1 - otm_pct)))
    call_strike = min(call_strikes, key=lambda k: abs(k - spot * (1 + otm_pct)))
    if put_strike >= call_strike:   # collapsed to (or past) the ATM straddle -- not a strangle
        return None
    p, c = _leg_at(chain, "put", put_strike), _leg_at(chain, "call", call_strike)
    if not (p and c and (p.get("bid") or 0) > 0 and (c.get("bid") or 0) > 0):
        return None
    if ws.MIN_OPEN_INTEREST > 0:
        for leg in (p, c):
            if (leg.get("oi") or 0) < ws.MIN_OPEN_INTEREST:
                return None
    debit = (p["ask"] or 0) + (c["ask"] or 0)          # worst case: buy both at ask
    debit_best = (p["bid"] or 0) + (c["bid"] or 0)      # best case: buy both at bid
    if debit < MIN_DEBIT:
        return None
    return {"put": p, "call": c, "debit": debit, "debit_best": debit_best}


def _atm_straddle(chain, spot):
    """The literal ATM straddle: put and call at whichever listed strike is
    closest to the current spot price -- NOT the strike closest to 0.50
    delta. Those aren't the same thing: a 50-delta option sits at roughly
    the forward price (spot drifted by the option's own 0.5*IV^2*T term),
    which in a high-IV name can land noticeably above spot (e.g. an 84% IV
    name can drift several percent above spot over just a month) -- not
    what "ATM straddle" means to a trader looking at the strike relative to
    today's price."""
    put_strikes = {o["strike"] for o in chain if o["type"] == "put"}
    call_strikes = {o["strike"] for o in chain if o["type"] == "call"}
    common = put_strikes & call_strikes
    if not common:
        return None
    strike = min(common, key=lambda k: abs(k - spot))
    p, c = _leg_at(chain, "put", strike), _leg_at(chain, "call", strike)
    if not (p and c and (p.get("bid") or 0) > 0 and (c.get("bid") or 0) > 0):
        return None
    if ws.MIN_OPEN_INTEREST > 0:
        for leg in (p, c):
            if (leg.get("oi") or 0) < ws.MIN_OPEN_INTEREST:
                return None
    debit = (p["ask"] or 0) + (c["ask"] or 0)
    debit_best = (p["bid"] or 0) + (c["bid"] or 0)
    if debit < MIN_DEBIT:
        return None
    return {"put": p, "call": c, "debit": debit, "debit_best": debit_best}


def _long_row(sym, spot, exp, dte, earn, s, pop):
    """No "Max Profit" here -- a long strangle's upside is genuinely
    open-ended (unlike the credit strategies' net credit, a real cap), so
    showing a single number under that label would misleadingly imply one.
    **Breakeven** replaces it: the two prices the stock actually needs to
    clear (strike +/- the debit paid) to be profitable at expiration, with
    the % move required from spot in parentheses -- the real, hard number
    this strategy is about, not an estimate.

    ROR_%/AnnROR_%/Score are tied to that same Breakeven, not a raw
    expected-move guess: profit is estimated as whatever the IV-implied
    expected move (spot * IV * sqrt(DTE/365)) clears ABOVE the CLOSER
    (minimum-distance) breakeven -- not the move itself, which would
    ignore that you make nothing until you're past breakeven in the first
    place. 0 if the expected move doesn't even reach it. MaxLoss is real
    and defined: the debit paid, worst case."""
    p, c = s["put"], s["call"]
    width = abs(c["strike"] - p["strike"])
    strat = "Long Straddle" if width < 0.01 else "Long Strangle"
    iv = ((p.get("iv") or 0) + (c.get("iv") or 0)) / 2
    otm = 0.0 if width < 0.01 else min((spot - p["strike"]) / spot, (c["strike"] - spot) / spot)
    expected_move = spot * iv * math.sqrt(dte / 365.0) if (iv and dte) else float("nan")
    debit = s["debit"]
    oi = _leg_liquidity(p, c)
    be_low, be_high = p["strike"] - debit, c["strike"] + debit
    be_low_pct = (spot - be_low) / spot if spot else float("nan")
    be_high_pct = (be_high - spot) / spot if spot else float("nan")
    breakeven = (f"${be_low:.2f} (-{be_low_pct*100:.1f}%) / ${be_high:.2f} (+{be_high_pct*100:.1f}%)"
                if (be_low_pct == be_low_pct and be_high_pct == be_high_pct) else "-")
    closer_be_dist = min(spot - be_low, be_high - spot) if spot else float("nan")
    profit_est = (max(0.0, expected_move - closer_be_dist)
                 if (expected_move == expected_move and closer_be_dist == closer_be_dist) else float("nan"))
    ror = (profit_est / debit) if (debit > 0 and profit_est == profit_est) else float("nan")
    ann = ror * 365.0 / dte if (dte and ror == ror) else float("nan")
    score = (ann / (iv ** ws.SCORE_IV_EXP) * (pop ** ws.SCORE_POP_EXP) * ((365.0 / dte) ** ws.SCORE_DTE_EXP)
             if (dte and dte > 0 and iv and iv > 0 and ann == ann and pop == pop) else float("nan"))
    return {"Ticker": sym, "CurrentPrice": round(spot, 2), "Strategy": strat,
            "Put Legs": f"buy {p['strike']:g}P", "Call Legs": f"buy {c['strike']:g}C",
            "Expiration": exp, "DTE": dte, "OTM_%": otm,
            "Width": round(width, 2), "Width_%": (width / spot if spot else float("nan")),
            "Max Profit": float("nan"), "Max Profit (Best)": float("nan"),
            "Breakeven": breakeven,
            "MaxLoss": round(debit, 2),
            "ROR_%": ror, "AnnROR_%": ann, "POP_%": pop, "IV": iv,
            "Score": (round(score, 2) if score == score else float("nan")),
            "AvgPremium": "-",
            "# of contracts": ws.contracts_for_target(debit * 100, target=SPREAD_CASH_TARGET),
            "OpenInt": int(oi), "EarningsDate": earn}


def _for_expiration_long(sym, spot, exp, dte, earn, chain):
    """Long strangle/straddle candidates. screen_spreads() only calls this
    ONCE per ticker -- for the single expiration closest to a confirmed
    earnings date, not every expiration that happens to span one. Several
    near-duplicate rows for the same catalyst (one per later expiration)
    would just be noise and dilute the point of a catalyst-driven trade:
    you want exposure concentrated right around the event, not spread
    across every weekly that comes after it. POP is each leg's own delta
    ADDED together (mirrors the iron condor's leg-combination technique,
    just flipped: profit here means finishing BEYOND either strike, not
    staying between them).

    The straddle (put and call at the same strike) is picked by literal
    proximity to spot -- see _atm_straddle. Strangle legs (different
    strikes) are scanned by LONG_STRANGLE_OTM_PCTS -- symmetric %-from-spot
    on each side, NOT matched by delta independently per leg (see
    _long_strangle's docstring for why that produced lopsided strangles).
    POP is still checked afterward against whatever legs each rung actually
    lands on, from a tight 1% out to a modest 10%, since that's roughly the
    range where put_delta + call_delta can clear the same 70% POP floor
    used everywhere else here -- deep-OTM legs like the iron condor's wings
    would never reach it; being long premium needs to be much closer to the
    money to have a high chance of finishing past either side.

    No OTM floor / OTM-over-IV cushion here, unlike credit spreads/iron
    condor -- those exist to keep a SHORT leg meaningfully far from the
    money. A long strangle's legs are, by construction, close to the money
    (that's what makes the 70% POP floor reachable at all -- see above), so
    requiring the credit-spread OTM floor too is nearly impossible to
    satisfy at once except in extreme-IV names: even at 50% IV and 40 DTE,
    a 0.35-delta leg is only ~8% OTM, still short of the 10% floor. POP is
    the real gate for this strategy.

    At most ONE straddle AND ONE strangle are returned per ticker (on top
    of only one expiration -- see screen_spreads) -- not one overall
    winner. Within each of those two groups, whichever passing strike/width
    actually needs the SMALLEST move to breakeven wins (there's only ever
    one straddle candidate -- the ATM strike -- so that part only matters
    for picking among the strangle rungs). Smallest-breakeven, not
    tightest-OTM: those aren't the same thing -- a tighter strike often
    costs more debit, which can push its breakeven further away than a
    slightly-further-OTM, cheaper alternative (e.g. 2%-OTM strikes but an
    8% breakeven vs. 3%-OTM strikes with a 5% breakeven -- the second is
    the easier trade despite the "wider" strikes)."""
    candidates = []
    seen = set()
    pmin = SPREAD_POP_MIN

    def _try(s):
        if not s:
            return
        p, c = s["put"], s["call"]
        pop = abs(p["delta"]) + abs(c["delta"])
        if not (pmin <= pop <= SPREAD_POP_MAX):
            return
        key = (p["strike"], c["strike"])
        if key in seen:
            return
        r = _long_row(sym, spot, exp, dte, earn, s, pop)
        if r["AnnROR_%"] == r["AnnROR_%"] and r["AnnROR_%"] >= ROR_ANN_MIN:
            seen.add(key)
            debit = s["debit"]
            be_low, be_high = p["strike"] - debit, c["strike"] + debit
            closer_be_pct = min((spot - be_low) / spot, (be_high - spot) / spot) if spot else float("inf")
            candidates.append((closer_be_pct, r))

    _try(_atm_straddle(chain, spot))
    for otm_pct in LONG_STRANGLE_OTM_PCTS:
        _try(_long_strangle(chain, spot, otm_pct))

    out = []
    for strat in ("Long Straddle", "Long Strangle"):
        matches = [cand for cand in candidates if cand[1]["Strategy"] == strat]
        if matches:
            out.append(min(matches, key=lambda t: t[0])[1])
    return out


def _all_expirations(symbol, today):
    """Every expiration Tradier lists for this ticker that hasn't already
    passed (dte >= 0) -- no SPREAD_DTE_MIN/MAX filtering. Used to find the
    long strangle/straddle's earnings-catalyst expiration, which is allowed
    to fall outside that window (see screen_spreads); the normal window
    (_for_expiration's credit spreads/iron condor) filters this further."""
    out = []
    for exp in ws.td_expirations(symbol):
        try:
            d = dt.date.fromisoformat(exp)
        except Exception:
            continue
        dte = (d - today).days
        if dte >= 0:
            out.append((exp, d, dte))
    return out


def _catalyst_dates(symbol):
    """[(date, source_ticker), ...] -- this ticker's own confirmed earnings
    plus any ws.PEER_TICKERS entry's confirmed earnings. A related ticker's
    report (a chip equipment maker moving a memory maker via demand
    read-through, a direct competitor, etc.) can be a real catalyst even
    with nothing on this ticker's own calendar. Fully live -- every date
    comes from the same get_earnings_date() call used everywhere else, no
    manual entry needed beyond the PEER_TICKERS relationship itself."""
    out = []
    own = ws.get_earnings_date(symbol)
    if own is not None:
        out.append((own, symbol))
    for peer in ws.PEER_TICKERS.get(symbol, []):
        try:
            d = ws.get_earnings_date(peer)
        except Exception:
            d = None
        if d is not None:
            out.append((d, peer))
    return out


def screen_spreads(symbol):
    price = ws.td_quote(symbol)
    if not price:
        raise RuntimeError("no quote")
    price = float(price)
    earnings = ws.get_earnings_date(symbol)
    today = dt.date.today()
    all_exps = _all_expirations(symbol, today)
    expirations = [c for c in all_exps if SPREAD_DTE_MIN <= c[2] <= SPREAD_DTE_MAX]

    # Long strangle/straddle only ever use the SINGLE expiration closest to
    # (on or after) a confirmed catalyst date -- this ticker's own earnings,
    # OR (see _catalyst_dates) a related ticker's -- the standard way to
    # actually play an earnings-driven move (concentrate IV-crush/gamma
    # exposure right around the event), not every later expiration that
    # also happens to span it. Every catalyst date is tried; whichever
    # produces the closest expiration wins. Searched past the
    # SPREAD_DTE_MIN/MAX window above -- a catalyst further than
    # SPREAD_DTE_MAX out still gets a long candidate at the nearest
    # expiration after it, even though every other strategy here stays
    # capped at SPREAD_DTE_MAX -- but only up to LONG_DTE_MAX; a catalyst
    # further out than that is too far away to be worth tying up capital in
    # a long-premium position waiting for it. No known catalyst at all (own
    # or peer) -> no expiration qualifies.
    long_exp, long_catalyst = None, None
    best = None
    for cat_date, source in _catalyst_dates(symbol):
        candidates = [c for c in all_exps if today <= cat_date <= c[1] and c[2] <= LONG_DTE_MAX]
        if not candidates:
            continue
        cand = min(candidates, key=lambda c: (c[1] - cat_date).days)
        dist = (cand[1] - cat_date).days
        if best is None or dist < best[0]:
            best = (dist, cand, cat_date, source)
    if best is not None:
        _, long_exp, cat_date, source = best
        long_catalyst = f"{cat_date.isoformat()}" if source == symbol else f"{cat_date.isoformat()} (via {source})"

    rows = []
    seen = set()
    for exp, exp_date, dte in expirations:
        seen.add(exp)
        chain = ws.td_chain(symbol, exp)
        if not ws.earnings_blocks(symbol, earnings, today, exp_date):
            rows += _for_expiration(symbol, price, exp, dte, earnings, chain)
        if long_exp and exp == long_exp[0]:
            rows += _for_expiration_long(symbol, price, exp, dte, long_catalyst, chain)

    # long_exp can fall outside SPREAD_DTE_MIN/MAX (see above) -- if so it
    # wasn't in `expirations`/fetched yet, so give it its own chain fetch.
    if long_exp and long_exp[0] not in seen:
        exp, exp_date, dte = long_exp
        chain = ws.td_chain(symbol, exp)
        rows += _for_expiration_long(symbol, price, exp, dte, long_catalyst, chain)

    return rows


def _df(rows):
    if not rows:
        return pd.DataFrame(columns=SPREAD_COLS)
    # rank within each ticker by composite Score, same system as puts/calls
    return pd.DataFrame(rows).sort_values(["Ticker", "Score"],
                                          ascending=[True, False])[SPREAD_COLS]


def _fmt(df):
    d = df.copy()
    for c in PCT_COLS:
        if c in d.columns:
            d[c] = d[c].apply(lambda v: f"{v*100:.1f}%" if pd.notna(v) else "-")
    # Max Profit: "$worst-$best (total credit range across the # of contracts that reach the
    # cash target)". worst = sell short at bid / buy long at ask; best = sell short at ask /
    # buy long at bid -- this range folds in what the old separate Spread_$ liquidity column
    # used to convey, so that column is gone. Long strangle/straddle leave this NaN entirely
    # (no real max profit -- see _long_row's docstring; use Breakeven instead), which the NaN
    # check below already handles as "-". lo==hi (no separate worst/best) collapses to one value.
    if "Max Profit" in d.columns and "# of contracts" in d.columns:
        def _mp(lo, hi, n):
            if pd.isna(lo):
                return "-"
            hi = hi if pd.notna(hi) else lo
            if lo == hi:
                return f"${lo:.2f}" if pd.isna(n) else f"${lo:.2f} (${lo * 100 * int(n):,.0f})"
            if pd.isna(n):
                return f"${lo:.2f}-${hi:.2f}"
            return f"${lo:.2f}-${hi:.2f} (${lo * 100 * int(n):,.0f}-${hi * 100 * int(n):,.0f})"
        hi_col = d["Max Profit (Best)"] if "Max Profit (Best)" in d.columns else d["Max Profit"]
        d["Max Profit"] = [_mp(v, h, n) for v, h, n in zip(d["Max Profit"], hi_col, d["# of contracts"])]
        if "Max Profit (Best)" in d.columns:
            d = d.drop(columns=["Max Profit (Best)"])
    elif "Max Profit" in d.columns:
        d["Max Profit"] = d["Max Profit"].apply(lambda v: f"${v:.2f}" if pd.notna(v) else "-")
    # MaxLoss: "$/share (total across the # of contracts that reach the cash target)", same as Max Profit
    if "MaxLoss" in d.columns and "# of contracts" in d.columns:
        def _ml(v, n):
            if pd.isna(v):
                return "-"
            if pd.isna(n):
                return f"${v:.2f}"
            return f"${v:.2f} (${v * 100 * int(n):,.0f})"
        d["MaxLoss"] = [_ml(v, n) for v, n in zip(d["MaxLoss"], d["# of contracts"])]
    elif "MaxLoss" in d.columns:
        d["MaxLoss"] = d["MaxLoss"].apply(lambda v: f"${v:.2f}" if pd.notna(v) else "-")
    for c in ("CurrentPrice", "Width"):
        if c in d.columns:
            d[c] = d[c].apply(lambda v: f"${v:.2f}" if pd.notna(v) else "-")
    return d
