"""
spreads.py -- multi-leg strategy screener (reuses wheel_screener's Tradier data).
All anchored at Options Alpha's 70% POP:
  * Put/Call credit spreads : short leg ~0.30 delta (~70% POP), defined risk.
  * Iron condor             : short put + short call ~0.15 delta each (combined ~0.30), defined risk.
Undefined-risk strategies (short strangles/straddles) are excluded. Max Profit = net credit received.
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
SCAN_TOL       = 0.04                        # tolerance when matching a target delta
SPREAD_WIDTH_PCT = 0.05    # long strike ~5% from the short strike
MIN_CREDIT       = 0.05    # ignore trivial credits
ROR_ANN_MIN      = 0.25    # defined-risk: min annualized return-on-risk
SPREAD_POP_MIN   = 0.70    # at least 70% POP (own band, independent of puts/calls)
SPREAD_POP_MAX   = 1.0     # no upper cap
SPREAD_DTE_MIN   = 7       # spreads have their OWN expiration window
SPREAD_DTE_MAX   = 90
SPREAD_MIN_OTM_OVER_IV = 0.15  # each short leg's OTM must be >= this fraction of its IV. 0 to disable.

SPREAD_COLS = ["Ticker", "CurrentPrice", "Strategy", "Put Legs", "Call Legs", "Expiration", "DTE",
               "OTM_%", "Width", "Width_%", "Max Profit", "AvgPremium", "MaxLoss", "ROR_%", "AnnROR_%",
               "POP_%", "IV", "Score", "Cash/Contract", "# of contracts",
               "Spread_$", "OpenInt", "EarningsDate"]
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
    credit = (short["bid"] or 0) - (lng["ask"] or 0)
    width = abs(short["strike"] - ls)
    if credit < MIN_CREDIT or width <= 0:
        return None
    max_loss = width - credit
    if max_loss <= 0:
        return None
    return {"short": short, "long": lng, "long_strike": ls, "credit": credit,
            "width": width, "max_loss": max_loss}


def _leg_liquidity(*legs):
    """Combined liquidity for a spread: min open interest across legs, and total
    round-trip bid-ask across legs (illiquidity cost you pay to get in and out)."""
    ois = [int(l.get("oi") or 0) for l in legs]
    bidask = sum((l.get("ask") or 0) - (l.get("bid") or 0) for l in legs)
    return (min(ois) if ois else 0), bidask


def _avg_credit_range(sym, dte, kind, short_strike, long_strike, spot):
    """Estimated typical net credit [low, high] for one credit spread, from the
    historical per-leg premium table (short premium minus long premium)."""
    so = (spot - short_strike) / spot if kind == "put" else (short_strike - spot) / spot
    lo = (spot - long_strike) / spot if kind == "put" else (long_strike - spot) / spot
    s = ws.avg_premium_range(sym, so, dte, kind)
    l = ws.avg_premium_range(sym, lo, dte, kind)
    if not (s and l):
        return None
    return (max(0.0, s[0] - l[1]), max(0.0, s[1] - l[0]))


def _defined_row(sym, spot, exp, dte, earn, strat, put_legs, call_legs,
                 credit, width, max_loss, pop, iv, otm, oi=0, leg_bidask=0.0, avg_credit=None):
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
            "Max Profit": round(credit, 2), "MaxLoss": round(max_loss, 2),
            "ROR_%": ror, "AnnROR_%": ann, "POP_%": pop, "IV": iv,
            "Score": (round(score, 2) if score == score else float("nan")),
            "AvgPremium": (f"${avg_credit[0]:.2f}-${avg_credit[1]:.2f}" if avg_credit else "-"),
            "Cash/Contract": round(max_loss * 100, 0),
            "# of contracts": ws.contracts_for_target(max_loss * 100),
            "Spread_$": round(leg_bidask, 2),
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
            oi, ba = _leg_liquidity(s["short"], s["long"])
            acr = _avg_credit_range(sym, dte, opt_type, sk, s["long_strike"], spot)
            r = _defined_row(sym, spot, exp, dte, earn, strat, pl, cl,
                             s["credit"], s["width"], s["max_loss"], pop,
                             s["short"].get("iv") or 0, otm, oi, ba, acr)
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
        oi, ba = _leg_liquidity(ps["short"], ps["long"], cs["short"], cs["long"])
        _pac = _avg_credit_range(sym, dte, "put", ps["short"]["strike"], ps["long_strike"], spot)
        _cac = _avg_credit_range(sym, dte, "call", cs["short"]["strike"], cs["long_strike"], spot)
        acr = (_pac[0] + _cac[0], _pac[1] + _cac[1]) if (_pac and _cac) else None
        r = _defined_row(sym, spot, exp, dte, earn, "Iron condor",
                         f"sell {ps['short']['strike']:g}P / buy {ps['long_strike']:g}P",
                         f"sell {cs['short']['strike']:g}C / buy {cs['long_strike']:g}C",
                         credit, width, max_loss, pop, iv, min(p_otm, c_otm), oi, ba, acr)
        if r["AnnROR_%"] >= ROR_ANN_MIN:
            seen.add(key)
            out.append(r)
    return out


def _spread_expirations(symbol, today):
    out = []
    for exp in ws.td_expirations(symbol):
        try:
            d = dt.date.fromisoformat(exp)
        except Exception:
            continue
        dte = (d - today).days
        if SPREAD_DTE_MIN <= dte <= SPREAD_DTE_MAX:
            out.append((exp, d, dte))
    return out


def screen_spreads(symbol):
    price = ws.td_quote(symbol)
    if not price:
        raise RuntimeError("no quote")
    price = float(price)
    earnings = ws.get_earnings_date(symbol)
    today = dt.date.today()
    rows = []
    for exp, exp_date, dte in _spread_expirations(symbol, today):
        if ws.earnings_blocks(symbol, earnings, today, exp_date):
            continue
        rows += _for_expiration(symbol, price, exp, dte, earnings, ws.td_chain(symbol, exp))
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
    # Max Profit: "$/share (total credit across the # of contracts that reach the cash target)"
    if "Max Profit" in d.columns and "# of contracts" in d.columns:
        def _mp(v, n):
            if pd.isna(v):
                return "-"
            if pd.isna(n):
                return f"${v:.2f}"
            return f"${v:.2f} (${v * 100 * int(n):,.0f})"
        d["Max Profit"] = [_mp(v, n) for v, n in zip(d["Max Profit"], d["# of contracts"])]
    elif "Max Profit" in d.columns:
        d["Max Profit"] = d["Max Profit"].apply(lambda v: f"${v:.2f}" if pd.notna(v) else "-")
    for c in ("MaxLoss", "CurrentPrice", "Width", "Spread_$"):
        if c in d.columns:
            d[c] = d[c].apply(lambda v: f"${v:.2f}" if pd.notna(v) else "-")
    if "Cash/Contract" in d.columns and "# of contracts" in d.columns:
        def _cash(v, n):
            if pd.isna(v):
                return "-"
            if pd.isna(n):
                return f"${v:,.0f}"
            return f"${v:,.0f} (${v * int(n):,.0f})"
        d["Cash/Contract"] = [_cash(v, n) for v, n in zip(d["Cash/Contract"], d["# of contracts"])]
    elif "Cash/Contract" in d.columns:
        d["Cash/Contract"] = d["Cash/Contract"].apply(lambda v: f"${v:,.0f}" if pd.notna(v) else "-")
    return d
