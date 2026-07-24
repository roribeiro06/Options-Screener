#!/usr/bin/env python3
"""
wheel_screener.py -- Income options screener (cash-secured puts + covered calls).

Data source: TRADIER (reliable on servers). Set an access token via env var
TRADIER_TOKEN (sandbox token is free). Yahoo is used only as a light fallback
for VIX and earnings dates.

Run locally:  set TRADIER_TOKEN=your_token   then   py wheel_screener.py
On Streamlit: put the token in the app's Secrets (see DEPLOY.md).
Not financial advice. Screens candidates; you decide.
"""

import os
import math
import time as _time
import datetime as dt

import pandas as pd
from scipy.stats import norm

# ============================== CONFIG ==============================
PUT_TICKERS = ["SPY", "QQQ", "DIA", "MSFT", "GOOG", "NVDA", "AVGO", "SMH", "MRVL", "MU", "IGV"]

HOLDINGS = {
    "DKNG": 18.4505,
    "KHC": 32.9834,
    "CMCSA": 35.12,
    "PS": 25.25,
}

# 70% POP anchor (Options Alpha): POP = 1 - |delta|, so ~70% POP ~= 0.30 delta.
POP_MIN           = 0.65     # accept POP 65-75% (delta ~0.25-0.35), centered on 70%
POP_MAX           = 0.75
DTE_MIN           = 7      # include short weeklies
DTE_MAX           = 90     # Options Alpha: longer duration allowed
YIELD_HURDLE_BASE = 0.25     # (informational; the active yield rule is the two lines below)
MIN_ANN_YIELD     = 0.15     # flat floor: contracts must pay >= this annualized (when tiered rule off)
USE_TIERED_YIELD  = False    # ON: use the tiered OTM->yield rule below instead of the flat floor
TIERED_YIELD = [(0.15, 0.10), (0.10, 0.15), (0.05, 0.25)]  # (min OTM, required ann. yield), high OTM first
DTE_SHORT_CUTOFF    = 21     # <=21 days = "3 weeks and under"
YIELD_OVER_IV_SHORT = 1.0    #   short-dated (<=21 DTE): annualized yield must be > 100% of IV
YIELD_OVER_IV_LONG  = 0.7    # 22+ DTE: annualized yield must be > 70% of IV
USE_YIELD_OVER_IV   = False  # OFF: don't require yield to beat IV (set True to re-enable)
REQUIRE_STRIKE_ABOVE_COST = False  # OFF: covered-call strike need NOT be above your cost basis
OTM_MIN             = 0.10   # min % out-of-the-money
OTM_MAX             = 1.0    # max % out-of-the-money (1.0 = 100%, effectively off)
USE_TBILL_SPREAD  = False    # your old "beat T-bill by 5pts" rule (off; set True to re-enable)
MIN_RISK_PREMIUM  = 0.05
IVR_MIN           = 0.50
USE_IVR           = False    # Tradier gives current IV but NOT IV rank (needs IV history)

RISK_FREE         = 0.043
PREMIUM_BASIS     = "bid"
TBILL_LADDER = [(35, 0.0370), (56, 0.0372), (100, 0.0379), (190, 0.0390), (99999, 0.0398)]
OUTPUT_FILE = "qualifying_contracts.xlsx"
# ===================================================================


# ------------------------- Tradier data ---------------------------
def _td_get(path, params):
    import requests
    token = os.environ.get("TRADIER_TOKEN", "")
    base = os.environ.get("TRADIER_BASE", "https://sandbox.tradier.com/v1")
    r = requests.get(base + path, params=params, timeout=20,
                     headers={"Authorization": "Bearer " + token,
                              "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def td_quote(symbol):
    j = _td_get("/markets/quotes", {"symbols": symbol})
    q = (j.get("quotes") or {}).get("quote")
    q = q[0] if isinstance(q, list) else q
    if not q:
        return None
    return q.get("last") or q.get("close") or q.get("prevclose")


def td_expirations(symbol):
    j = _td_get("/markets/options/expirations",
                {"symbol": symbol, "includeAllRoots": "true"})
    return _as_list((j.get("expirations") or {}).get("date"))


def td_chain(symbol, expiration):
    j = _td_get("/markets/options/chains",
                {"symbol": symbol, "expiration": expiration, "greeks": "true"})
    out = []
    for o in _as_list((j.get("options") or {}).get("option")):
        g = o.get("greeks") or {}
        out.append({"type": o.get("option_type"),
                    "strike": float(o.get("strike")),
                    "bid": o.get("bid") or 0,
                    "ask": o.get("ask") or 0,
                    "delta": g.get("delta"),
                    "iv": g.get("mid_iv") or g.get("smv_vol") or 0})
    return out


# ------------------- Yahoo fallback (VIX/earnings only) -----------
def _make_session():
    try:
        from curl_cffi import requests as _cffi
        return _cffi.Session(impersonate="chrome")
    except Exception:
        return None


_SESSION = _make_session()


def _ticker(sym):
    import yfinance as yf
    try:
        return yf.Ticker(sym, session=_SESSION) if _SESSION else yf.Ticker(sym)
    except Exception:
        return yf.Ticker(sym)


def tbill_for(dte):
    for max_dte, rate in TBILL_LADDER:
        if dte <= max_dte:
            return rate
    return TBILL_LADDER[-1][1]


def get_vix():
    try:
        q = td_quote("VIX")
        if q:
            return round(float(q), 2)
    except Exception:
        pass
    try:
        v = _ticker("^VIX")
        px = v.history(period="1d")["Close"].iloc[-1]
        return round(float(px), 2)
    except Exception:
        return None


def vix_regime(v):
    if v is None:
        return "unknown"
    if v < 15:
        return "low - thin premiums, mostly wait"
    if v < 20:
        return "below-average - selective"
    if v < 30:
        return "elevated - prime selling"
    return "high - rich but risky"


def _d1(S, K, T, sigma, r):
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def bs_put_delta(S, K, T, sigma, r):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return float("nan")
    return norm.cdf(_d1(S, K, T, sigma, r)) - 1.0


def bs_call_delta(S, K, T, sigma, r):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return float("nan")
    return norm.cdf(_d1(S, K, T, sigma, r))


def tiered_yield_needed(otm):
    """Required annualized yield by OTM bracket (further OTM -> less yield needed)."""
    for min_otm, req in TIERED_YIELD:      # ordered high OTM -> low
        if otm >= min_otm:
            return req
    return TIERED_YIELD[-1][1]              # closer than smallest tier -> strictest


def evaluate_put(row, spot, dte, earnings_in_window, iv_rank=None, delta=None):
    strike, premium, iv = row["strike"], row["premium"], row["iv"]
    otm     = (spot - strike) / spot
    per_yld = premium / strike if strike else float("nan")
    ann_yld = per_yld * 365.0 / dte if dte else float("nan")
    d = delta if delta is not None else bs_put_delta(spot, strike, dte / 365.0, iv, RISK_FREE)
    absdelta = abs(d)
    delta_pct = 1.0 - absdelta
    tbill    = tbill_for(dte)
    risk_prem = ann_yld - tbill
    needed   = YIELD_HURDLE_BASE - otm
    yiv      = YIELD_OVER_IV_SHORT if dte <= DTE_SHORT_CUTOFF else YIELD_OVER_IV_LONG
    req_yield = tiered_yield_needed(otm) if USE_TIERED_YIELD else MIN_ANN_YIELD
    tests = {
        "pop_target":   POP_MIN <= delta_pct <= POP_MAX,
        "min_yield":    ann_yld >= req_yield,
        "dte_window":   DTE_MIN <= dte <= DTE_MAX,
        "otm_range":    OTM_MIN <= otm <= OTM_MAX,
        "no_earnings":  not earnings_in_window,
    }
    if USE_YIELD_OVER_IV:
        tests["yield_over_iv"] = ann_yld > yiv * iv
    if USE_TBILL_SPREAD:
        tests["tbill_spread"] = risk_prem >= MIN_RISK_PREMIUM
    if USE_IVR:
        tests["iv_rank"] = (iv_rank is not None) and (iv_rank >= IVR_MIN)
    reasons = []
    if not tests["pop_target"]:
        reasons.append(f"POP {delta_pct:.0%} outside {POP_MIN:.0%}-{POP_MAX:.0%}")
    if not tests["min_yield"]:
        reasons.append(f"yield {ann_yld:.1%} < {req_yield:.0%} needed")
    if USE_YIELD_OVER_IV and not tests.get("yield_over_iv"):
        reasons.append(f"yield {ann_yld:.1%} below {yiv:.0%} of IV ({iv:.0%})")
    if USE_TBILL_SPREAD and not tests.get("tbill_spread"):
        reasons.append(f"only {risk_prem:.1%} over T-bill")
    if not tests["dte_window"]:
        reasons.append(f"DTE {dte} outside {DTE_MIN}-{DTE_MAX}")
    if not tests["otm_range"]:
        reasons.append(f"OTM {otm:.1%} outside {OTM_MIN:.0%}-{OTM_MAX:.0%}")
    if not tests["no_earnings"]:
        reasons.append("spans earnings")
    if USE_IVR and not tests.get("iv_rank"):
        reasons.append("IV Rank <50 or missing")
    return {"OTM_%": otm, "Premium": premium, "PeriodYield_%": per_yld,
            "AnnYield_%": ann_yld, "YieldNeeded_%": needed, "Delta_%": delta_pct,
            "IV": iv, "Tbill_%": tbill, "RiskPrem_%": risk_prem,
            "PASS": all(tests.values()), "Reasons": "; ".join(reasons)}


def evaluate_call(row, spot, dte, earnings_in_window, cost_basis, iv_rank=None, delta=None):
    strike, premium, iv = row["strike"], row["premium"], row["iv"]
    otm     = (strike - spot) / spot
    per_yld = premium / spot if spot else float("nan")
    ann_yld = per_yld * 365.0 / dte if dte else float("nan")
    cd = delta if delta is not None else bs_call_delta(spot, strike, dte / 365.0, iv, RISK_FREE)
    delta_pct = 1.0 - cd
    tbill    = tbill_for(dte)
    risk_prem = ann_yld - tbill
    needed   = YIELD_HURDLE_BASE - otm
    yiv      = YIELD_OVER_IV_SHORT if dte <= DTE_SHORT_CUTOFF else YIELD_OVER_IV_LONG
    req_yield = tiered_yield_needed(otm) if USE_TIERED_YIELD else MIN_ANN_YIELD
    tests = {
        "pop_target":   POP_MIN <= delta_pct <= POP_MAX,
        "min_yield":    ann_yld >= req_yield,
        "dte_window":   DTE_MIN <= dte <= DTE_MAX,
        "otm_range":    OTM_MIN <= otm <= OTM_MAX,
        "no_earnings":  not earnings_in_window,
    }
    if REQUIRE_STRIKE_ABOVE_COST:
        tests["above_cost"] = (cost_basis is None) or (strike >= cost_basis)
    if USE_YIELD_OVER_IV:
        tests["yield_over_iv"] = ann_yld > yiv * iv
    if USE_TBILL_SPREAD:
        tests["tbill_spread"] = risk_prem >= MIN_RISK_PREMIUM
    if USE_IVR:
        tests["iv_rank"] = (iv_rank is not None) and (iv_rank >= IVR_MIN)
    reasons = []
    if not tests["pop_target"]:
        reasons.append(f"POP {delta_pct:.0%} outside {POP_MIN:.0%}-{POP_MAX:.0%}")
    if not tests["min_yield"]:
        reasons.append(f"yield {ann_yld:.1%} < {req_yield:.0%} needed")
    if USE_YIELD_OVER_IV and not tests.get("yield_over_iv"):
        reasons.append(f"yield {ann_yld:.1%} below {yiv:.0%} of IV ({iv:.0%})")
    if USE_TBILL_SPREAD and not tests.get("tbill_spread"):
        reasons.append(f"only {risk_prem:.1%} over T-bill")
    if not tests["dte_window"]:
        reasons.append(f"DTE {dte} outside {DTE_MIN}-{DTE_MAX}")
    if not tests["otm_range"]:
        reasons.append(f"OTM {otm:.1%} outside {OTM_MIN:.0%}-{OTM_MAX:.0%}")
    if not tests["no_earnings"]:
        reasons.append("spans earnings")
    if REQUIRE_STRIKE_ABOVE_COST and not tests.get("above_cost"):
        reasons.append(f"strike below cost {cost_basis}")
    if USE_IVR and not tests.get("iv_rank"):
        reasons.append("IV Rank <50 or missing")
    return {"OTM_%": otm, "Premium": premium, "PeriodYield_%": per_yld,
            "AnnYield_%": ann_yld, "YieldNeeded_%": needed, "Delta_%": delta_pct,
            "IV": iv, "Tbill_%": tbill, "RiskPrem_%": risk_prem,
            "PASS": all(tests.values()), "Reasons": "; ".join(reasons)}


def get_earnings_date(symbol):
    """Next earnings date via Yahoo (two methods + one retry). None = none found.
    ETFs legitimately return None (no earnings), so None is treated as 'no earnings'."""
    tkr = _ticker(symbol)
    today = dt.date.today()
    for attempt in range(2):
        try:
            cal = tkr.get_earnings_dates(limit=8)
            if cal is not None and len(cal):
                fut = [d.date() for d in cal.index.to_pydatetime() if d.date() >= today]
                if fut:
                    return min(fut)
        except Exception:
            pass
        try:
            c = tkr.calendar
            ed = c.get("Earnings Date") if isinstance(c, dict) else None
            if ed:
                ed = ed[0] if isinstance(ed, (list, tuple)) else ed
                if hasattr(ed, "date"):
                    ed = ed.date()
                if isinstance(ed, dt.date) and ed >= today:
                    return ed
        except Exception:
            pass
        _time.sleep(0.6)
    return None


def _expirations_in_window(symbol, today):
    out = []
    for exp in td_expirations(symbol):
        try:
            d = dt.date.fromisoformat(exp)
        except Exception:
            continue
        dte = (d - today).days
        if DTE_MIN <= dte <= DTE_MAX:
            out.append((exp, d, dte))
    return out


def screen_puts(symbol):
    price = td_quote(symbol)
    if not price:
        raise RuntimeError("no quote")
    price = float(price)
    earnings = get_earnings_date(symbol)
    today = dt.date.today()
    passers, near = [], []
    for exp, exp_date, dte in _expirations_in_window(symbol, today):
        earn_win = bool(earnings and today <= earnings <= exp_date)
        for o in td_chain(symbol, exp):
            if o["type"] != "put" or o["strike"] >= price:
                continue
            bid = o["bid"] or 0
            if bid <= 0:
                continue
            premium = bid if PREMIUM_BASIS == "bid" else (bid + (o["ask"] or 0)) / 2
            res = evaluate_put({"strike": o["strike"], "premium": float(premium),
                                "iv": float(o["iv"] or 0)}, price, dte, earn_win,
                               delta=o["delta"])
            rec = {"Ticker": symbol, "CurrentPrice": round(price, 2), "Strike": o["strike"],
                   "Expiration": exp, "DTE": dte, "EarningsDate": earnings, **res}
            if res["PASS"]:
                passers.append(rec)
            elif res["Reasons"].count(";") == 0:
                near.append(rec)
    return passers, near


def screen_calls(symbol, cost_basis):
    price = td_quote(symbol)
    if not price:
        raise RuntimeError("no quote")
    price = float(price)
    earnings = get_earnings_date(symbol)
    today = dt.date.today()
    passers, near = [], []
    for exp, exp_date, dte in _expirations_in_window(symbol, today):
        earn_win = bool(earnings and today <= earnings <= exp_date)
        for o in td_chain(symbol, exp):
            if o["type"] != "call" or o["strike"] <= price:
                continue
            bid = o["bid"] or 0
            if bid <= 0:
                continue
            premium = bid if PREMIUM_BASIS == "bid" else (bid + (o["ask"] or 0)) / 2
            res = evaluate_call({"strike": o["strike"], "premium": float(premium),
                                 "iv": float(o["iv"] or 0)}, price, dte, earn_win,
                                cost_basis, delta=o["delta"])
            rec = {"Ticker": symbol, "CurrentPrice": round(price, 2), "CostBasis": cost_basis,
                   "Strike": o["strike"], "Expiration": exp, "DTE": dte,
                   "EarningsDate": earnings, **res}
            if res["PASS"]:
                passers.append(rec)
            elif res["Reasons"].count(";") == 0:
                near.append(rec)
    return passers, near


PUT_COLS = ["Ticker", "CurrentPrice", "Strike", "Expiration", "DTE", "OTM_%", "Premium",
            "PeriodYield_%", "AnnYield_%", "Delta_%", "IV", "EarningsDate"]
CALL_COLS = ["Ticker", "CurrentPrice", "CostBasis", "Strike", "Expiration", "DTE", "OTM_%",
             "Premium", "PeriodYield_%", "AnnYield_%", "Delta_%", "IV", "EarningsDate"]
PCT_COLS = {"OTM_%", "PeriodYield_%", "PeriodYield_%", "AnnYield_%", "Delta_%",
            "Tbill_%", "RiskPrem_%", "IV"}


def _df(rows, cols):
    if not rows:
        return pd.DataFrame(columns=cols)
    # display order: ticker A->Z, then strike high->low (no per-ticker cap)
    return pd.DataFrame(rows).sort_values(["Ticker", "Strike"],
                                          ascending=[True, False])[cols]


def write_report(sheets, settings, path=OUTPUT_FILE):
    from openpyxl.styles import Font, PatternFill, Alignment
    hdr_fill = PatternFill("solid", fgColor="1F3864")
    hdr_font = Font(name="Arial", bold=True, color="FFFFFF")
    green = PatternFill("solid", fgColor="C6EFCE")
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets + [("Settings", settings)]:
            df.to_excel(xw, sheet_name=name, index=False)
            sh = xw.sheets[name]
            for ci, col in enumerate(df.columns, 1):
                c = sh.cell(row=1, column=ci)
                c.fill, c.font = hdr_fill, hdr_font
                c.alignment = Alignment(horizontal="center")
                vals = [len(str(col))] + [len(str(v)) for v in df[col].head(30)]
                sh.column_dimensions[c.column_letter].width = max(10, min(24, max(vals) + 2))
                if col in PCT_COLS:
                    for ri in range(2, len(df) + 2):
                        sh.cell(row=ri, column=ci).number_format = "0.0%"
            sh.freeze_panes = "A2"
            if len(df):
                sh.auto_filter.ref = sh.dimensions
            if name != "Settings":
                for ri in range(2, len(df) + 2):
                    for ci in range(1, len(df.columns) + 1):
                        sh.cell(row=ri, column=ci).fill = green


def _fmt(df):
    d = df.copy()
    for c in PCT_COLS:
        if c in d.columns:
            d[c] = (d[c] * 100).round(1).astype(str) + "%"
    for c in ("Premium", "CostBasis", "CurrentPrice"):
        if c in d.columns:
            d[c] = "$" + d[c].round(2).astype(str)
    return d


def _tbl(df, empty):
    return _fmt(df).to_html(index=False, classes="q", border=0) if len(df) else f"<p>{empty}</p>"


def write_html(put_pass, call_pass, vix=None, path="qualifying_contracts.html"):
    style = ("<style>body{font-family:Arial,Helvetica,sans-serif;margin:24px;"
             "background:#0f1115;color:#e8e8e8}h1{font-size:20px}h2{margin-top:30px;"
             "border-bottom:2px solid #1F3864;padding-bottom:4px}"
             "p{color:#aaa}table{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}"
             "th,td{border:1px solid #333;padding:6px 9px;text-align:right}"
             "th{background:#1F3864;color:#fff}td:first-child,th:first-child{text-align:left}"
             ".q tbody td{background:#12331d}</style>")
    vtxt = f"VIX {vix} ({vix_regime(vix)})" if vix is not None else "VIX n/a"
    p = ["<html><head><meta charset='utf-8'>", style, "</head><body>",
         "<h1>Wheel Screener</h1>",
         f"<p>{vtxt}. Generated {dt.date.today()}. "
         "Delta_% = chance of keeping the premium (1 - delta). "
         "YieldNeeded_% = 25% - OTM%. Not financial advice.</p>",
         "<h2>Cash-Secured Puts</h2>",
         _tbl(put_pass, "None - nothing pays enough; stay in T-bills."),
         "<h2>Covered Calls (strike above your cost)</h2>",
         _tbl(call_pass, "None - no call above your cost pays enough."),
         "</body></html>"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(p))


def main():
    vix = get_vix()
    pp = []
    for t in PUT_TICKERS:
        try:
            a, _ = screen_puts(t)
            pp += a
            print(f"PUT  {t}: {len(a)} qualifying")
        except Exception as e:
            print(f"PUT  {t}: ERROR {e}")
    cp = []
    for t, cost in HOLDINGS.items():
        try:
            a, _ = screen_calls(t, cost)
            cp += a
            print(f"CALL {t}: {len(a)} qualifying")
        except Exception as e:
            print(f"CALL {t}: ERROR {e}")

    put_pass = _df(pp, PUT_COLS)
    call_pass = _df(cp, CALL_COLS)
    settings = pd.DataFrame({
        "Setting": ["POP min", "POP max", "DTE min", "DTE max",
                    "Yield needed base (25-OTM)", "Use T-bill spread", "Min risk premium",
                    "Use IV Rank", "Premium basis", "Current VIX", "VIX regime", "Run date"],
        "Value": [POP_MIN, POP_MAX, DTE_MIN, DTE_MAX, YIELD_HURDLE_BASE,
                  USE_TBILL_SPREAD, MIN_RISK_PREMIUM, USE_IVR, PREMIUM_BASIS,
                  vix, vix_regime(vix), dt.date.today().isoformat()]})
    write_report([("Puts", put_pass), ("Covered Calls", call_pass)], settings, OUTPUT_FILE)
    write_html(put_pass, call_pass, vix, "qualifying_contracts.html")
    print(f"\nVIX {vix} ({vix_regime(vix)}). Puts: {len(put_pass)} | Calls: {len(call_pass)}.")


if __name__ == "__main__":
    main()
