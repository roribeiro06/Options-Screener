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

SPREAD_COLS = ["Ticker", "CurrentPrice", "Strategy", "Put Legs", "Call Legs", "Expiration", "DTE",
               "OTM_%", "Width", "Width_%", "Max Profit", "MaxLoss", "ROR_%", "AnnROR_%",
               "POP_%", "IV", "Value", "EarningsDate"]
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
    credit = (short["bid"] or 0) - (lng["ask"] or 0)
    width = abs(short["strike"] - ls)
    if credit < MIN_CREDIT or width <= 0:
        return None
    max_loss = width - credit
    if max_loss <= 0:
        return None
    return {"short": short, "long_strike": ls, "credit": credit,
            "width": width, "max_loss": max_loss}


def _defined_row(sym, spot, exp, dte, earn, strat, put_legs, call_legs,
                 credit, width, max_loss, pop, iv, otm):
    ror = credit / max_loss if max_loss > 0 else float("nan")
    ann = ror * 365.0 / dte if dte else float("nan")
    return {"Ticker": sym, "CurrentPrice": round(spot, 2), "Strategy": strat,
            "Put Legs": put_legs, "Call Legs": call_legs,
            "Expiration": exp, "DTE": dte, "OTM_%": otm,
            "Width": round(width, 2), "Width_%": (width / spot if spot else float("nan")),
            "Max Profit": round(credit, 2), "MaxLoss": round(max_loss, 2),
            "ROR_%": ror, "AnnROR_%": ann, "POP_%": pop, "IV": iv,
            "Value": (round(ann / iv * math.sqrt(dte / 365.0), 2)
                      if iv and dte and dte > 0 else float("nan")), "EarningsDate": earn}


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
            r = _defined_row(sym, spot, exp, dte, earn, strat, pl, cl,
                             s["credit"], s["width"], s["max_loss"], pop, s["short"].get("iv") or 0, otm)
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
        r = _defined_row(sym, spot, exp, dte, earn, "Iron condor",
                         f"sell {ps['short']['strike']:g}P / buy {ps['long_strike']:g}P",
                         f"sell {cs['short']['strike']:g}C / buy {cs['long_strike']:g}C",
                         credit, width, max_loss, pop, iv, min(p_otm, c_otm))
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
    return pd.DataFrame(rows).sort_values(["Ticker", "AnnROR_%"],
                                          ascending=[True, False])[SPREAD_COLS]


def _fmt(df):
    d = df.copy()
    for c in PCT_COLS:
        if c in d.columns:
            d[c] = d[c].apply(lambda v: f"{v*100:.1f}%" if pd.notna(v) else "-")
    for c in ("Max Profit", "MaxLoss", "CurrentPrice", "Width"):
        if c in d.columns:
            d[c] = d[c].apply(lambda v: f"${v:.2f}" if pd.notna(v) else "-")
    return d
