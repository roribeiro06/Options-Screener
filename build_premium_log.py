#!/usr/bin/env python3
"""
build_premium_log.py -- daily snapshot of what the market ACTUALLY pays.

For every watchlist ticker (PUT_TICKERS + covered-call HOLDINGS + open-position
tickers) it reads the live option chain and, for the expiration nearest each
DTE target (7, 14, 21, 30, 45, 60, 90), records at each OTM level on the grid
(5% ... 25% in 2.5-point steps) the annualized yield of the BID (premium / strike
for puts, / spot for calls -- the same convention evaluate_put/evaluate_call use)
and the implied volatility there, interpolated between the two listed strikes
that bracket the level. Written to premium_log/YYYY-MM.jsonl (see premium_log.py),
which the AvgPremium column reads once enough days have accumulated.

Skipped: expirations whose window contains a KNOWN earnings date (the screener
never trades across earnings, so those premiums would flatter the history);
strikes with no bid or open interest below MIN_OI; non-trading days (the market
must have printed a daily bar for today -- set FORCE=1 to bypass for testing).

Run once a day shortly before the close (log-premiums.yml). Needs Schwab (or
Tradier) credentials, like the other build scripts.
"""
import datetime as dt
import os
import sys

import premium_log as PL
import wheel_screener as ws

DTE_TARGETS = [7, 14, 21, 30, 45, 60, 90]
DTE_TOL_MIN, DTE_TOL_FRAC = 3, 0.15     # an expiration counts for a target if within +-max(3d, 15%)
MIN_OI = 10
MAX_STRIKE_GAP = 0.06                   # bracketing strikes may be at most 6 OTM points apart
LEVELS = [l / 100.0 for l in PL.OTM_LEVELS]


def observations(chain, spot, dte, kind):
    """[[otm_pct, dte, iv_pct, ann_yield_pct], ...] for one expiration's chain:
    for each grid OTM level, linearly interpolate the bid yield and IV between the
    two liquid strikes that bracket it. `kind` is 'put' or 'call'."""
    pts = []
    for o in chain:
        if o["type"] != kind or not o["strike"] or not spot:
            continue
        bid, iv, oi = o.get("bid") or 0, o.get("iv") or 0, o.get("oi") or 0
        if bid <= 0 or iv <= 0 or oi < MIN_OI:
            continue
        k = o["strike"]
        otm = (spot - k) / spot if kind == "put" else (k - spot) / spot
        if otm <= 0:
            continue
        yld = (bid / (k if kind == "put" else spot)) * 365.0 / dte
        pts.append((otm, iv, yld))
    pts.sort()
    out = []
    for lvl in LEVELS:
        for (o1, iv1, y1), (o2, iv2, y2) in zip(pts, pts[1:]):
            if o1 <= lvl <= o2 and (o2 - o1) <= MAX_STRIKE_GAP:
                f = 0.0 if o2 == o1 else (lvl - o1) / (o2 - o1)
                out.append([round(lvl * 100, 1), int(dte), round((iv1 + (iv2 - iv1) * f) * 100, 1),
                            round((y1 + (y2 - y1) * f) * 100, 1)])
                break
    return out


def pick_expirations(exps, today):
    """{expiration_iso: dte} -- for each DTE target the nearest expiration within
    tolerance (one expiration can serve two close targets)."""
    dtes = [(e, (dt.date.fromisoformat(e) - today).days) for e in exps]
    chosen = {}
    for t in DTE_TARGETS:
        tol = max(DTE_TOL_MIN, DTE_TOL_FRAC * t)
        cands = [(abs(d - t), e, d) for e, d in dtes if d > 0 and abs(d - t) <= tol]
        if cands:
            _, e, d = min(cands)
            chosen[e] = d
    return chosen


EST_QUARTER_DAYS = 91       # next report ~ last reported + a quarter
EST_SLACK_DAYS = 10         # an estimated date is only good to about +-10 days


def earnings_status(symbol, today):
    """(label, date, slack_days) -- NEVER silent about earnings:
      'YYYY-MM-DD'      a real date (manual override > Yahoo > stockanalysis.com)
      'none (ETF)'      fund with no earnings to span
      'est YYYY-MM-DD'  Yahoo/stockanalysis unreachable: estimated from Schwab's last reported
                        date + ~91 days, with +-EST_SLACK_DAYS of slack when excluding expirations
      'not found'       nothing worked -- recorded as such; premium_log ignores those records"""
    if symbol in ws.NO_EARNINGS_TICKERS:
        return "none (ETF)", None, 0
    try:
        d = ws.get_earnings_date(symbol)
    except Exception:
        d = None
    if d:
        return d.isoformat(), d, 0
    try:
        import schwab_data
        if ws.use_schwab():
            is_etf, last = schwab_data.earnings_info(symbol)
            if is_etf:
                return "none (ETF)", None, 0
            if last:
                est = last + dt.timedelta(days=EST_QUARTER_DAYS)
                while est < today - dt.timedelta(days=EST_SLACK_DAYS):
                    est += dt.timedelta(days=EST_QUARTER_DAYS)
                return f"est {est.isoformat()}", est, EST_SLACK_DAYS
    except Exception as e:
        print(f"{symbol}: Schwab earnings estimate failed ({e})", file=sys.stderr)
    return "not found", None, 0


def spans_earnings(today, exp_date, earn, slack):
    """True when [today, exp_date] contains the (possibly estimated) earnings date."""
    if earn is None:
        return False
    return today <= earn + dt.timedelta(days=slack) and exp_date >= earn - dt.timedelta(days=slack)


def snapshot_ticker(symbol, today):
    """Two records (put, call) for one ticker, or [] when nothing could be read.
    Each record carries "e": the earnings status (see earnings_status)."""
    spot = ws.td_quote(symbol)
    if not spot:
        return []
    spot = float(spot)
    label, earn, slack = earnings_status(symbol, today)
    puts, calls = [], []
    for e, d in sorted(pick_expirations(ws.td_expirations(symbol), today).items()):
        if spans_earnings(today, dt.date.fromisoformat(e), earn, slack):
            continue
        chain = ws.td_chain(symbol, e)
        if not chain:
            continue
        puts += observations(chain, spot, d, "put")
        calls += observations(chain, spot, d, "call")
    recs = []
    for k, rows in (("p", puts), ("c", calls)):
        if rows:
            recs.append({"d": today.isoformat(), "t": symbol, "k": k, "s": round(spot, 2), "e": label, "r": rows})
    return recs


def _today_et():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return dt.date.today()


def market_traded_today(today):
    """True when SPY has a daily bar dated today (false on weekends/holidays)."""
    try:
        j = ws._td_get("/markets/history", {"symbol": "SPY", "interval": "daily",
                                            "start": (today - dt.timedelta(days=7)).isoformat(),
                                            "end": today.isoformat()})
        days = (j.get("history") or {}).get("day") or []
        days = [days] if isinstance(days, dict) else days
        return bool(days) and days[-1]["date"] == today.isoformat()
    except Exception as e:
        print(f"couldn't confirm a trading day ({e}); proceeding", file=sys.stderr)
        return True


def main():
    if not (os.environ.get("TRADIER_TOKEN") or ws.use_schwab()):
        print("neither SCHWAB_REFRESH_TOKEN (+ client id/secret) nor TRADIER_TOKEN is set", file=sys.stderr)
        sys.exit(1)
    today = _today_et()
    if not os.environ.get("FORCE") and not market_traded_today(today):
        print(f"{today}: market not open today; nothing logged")
        return
    tickers = list(dict.fromkeys(list(ws.PUT_TICKERS) + list(ws.HOLDINGS.keys())
                                 + [p["ticker"] for p in ws.OPEN_POSITIONS]))
    records, failed, earn_status = [], [], {}
    for t in tickers:
        try:
            recs = snapshot_ticker(t, today)
            records += recs
            earn_status[t] = recs[0]["e"] if recs else "-"
            print(f"{t}: {sum(len(r['r']) for r in recs)} observations (earnings: {earn_status[t]})")
            if not recs:
                failed.append(t)
        except Exception as e:
            failed.append(t)
            print(f"{t}: ERROR {e}", file=sys.stderr)
    if not records:
        print("no observations collected; nothing written", file=sys.stderr)
        sys.exit(1)
    PL.write_day(today, records)
    print(f"logged {len(records)} ticker/side records for {today}"
          + (f"; no data for: {', '.join(failed)}" if failed else ""))
    report_earnings(earn_status)


def report_earnings(status):
    """Say out loud which tickers' earnings dates were estimated or not found --
    printed, raised as a GitHub warning annotation, and added to the run summary."""
    est = sorted(t for t, s in status.items() if s.startswith("est "))
    missing = sorted(t for t, s in status.items() if s == "not found")
    if est:
        print(f"::warning::Earnings date ESTIMATED (Yahoo/stockanalysis unreachable; Schwab last report + 91d, +-10d "
              f"excluded) for: {', '.join(est)}")
    if missing:
        print(f"::warning::Earnings date NOT FOUND for: {', '.join(missing)} -- logged but ignored by AvgPremium "
              f"until a date is found (add it to EARNINGS_DATES in wheel_screener.py to fix)")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        nl = chr(10)
        lines = ["### Earnings dates used for the premium log", "", "| Ticker | Earnings |", "|---|---|"]
        lines += [f"| {t} | {s} |" for t, s in sorted(status.items())]
        if missing:
            lines += ["", f"**NOT FOUND (logged but ignored by AvgPremium):** {', '.join(missing)}"]
        if est:
            lines += ["", f"**Estimated:** {', '.join(est)}"]
        with open(summary, "a", encoding="utf-8") as f:
            f.write(nl.join(lines) + nl)


if __name__ == "__main__":
    main()
