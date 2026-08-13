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
SPREAD_DTE_MAX   = 40      # no long-dated contracts
SPREAD_MIN_OTM_OVER_IV = 0.15  # each short leg's OTM must be >= this fraction of its IV. 0 to disable.
SPREAD_CASH_TARGET = 25000  # capital target for "# of contracts" -- lower than wheel_screener's
                            # CASH_TARGET (40,000) since multi-leg risk is defined/capped per contract.

SPREAD_COLS = ["Ticker", "CurrentPrice", "Strategy", "Put Legs", "Call Legs", "Expiration", "DTE",
               "OTM_%", "Width", "Width_%", "Max Profit", "Max Profit (Best)", "AvgPremium",
               "AnnROR_%", "ROR_%", "POP_%", "IV", "Score", "# of contracts", "MaxLoss",
               "OpenInt", "EarningsDate"]
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
    # Max Profit: "$worst-$best (total credit range across the # of contracts that reach the
    # cash target)". worst = sell short at bid / buy long at ask; best = sell short at ask /
    # buy long at bid -- this range folds in what the old separate Spread_$ liquidity column
    # used to convey, so that column is gone.
    if "Max Profit" in d.columns and "# of contracts" in d.columns:
        def _mp(lo, hi, n):
            if pd.isna(lo):
                return "-"
            hi = hi if pd.notna(hi) else lo
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
