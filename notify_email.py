#!/usr/bin/env python3
"""
notify_email.py -- run the FULL screener headlessly and email the results
(cash-secured puts, covered calls, multi-leg spreads, Discover -- the live
scan for high-open-interest tickers outside the watchlist, see discover.py
-- and your Open/Closed Positions plus their Financials tables, see
positions.py). Discover's live scan
is by far the slowest part of this script (a broad ~7,000-ticker universe
plus a yfinance market-cap check per surviving candidate) -- expect this to
noticeably lengthen every run, which happens every 30 min during market
hours per the schedule below. Open Positions adds one live options-chain
call per tracked position (cheap next to Discover, but still real API load
on every run).

Designed to be run on a schedule by GitHub Actions (see .github/workflows/screener-email.yml).
Only emails during US market hours (9:30-16:00 ET, weekdays); silently exits otherwise, so
extra cron ticks don't spam you.

Required environment variables (set as GitHub repo Secrets):
  TRADIER_TOKEN        - your Tradier sandbox token (same one the website uses)
  EMAIL_USER           - the Gmail address that SENDS the mail (e.g. you@gmail.com)
  EMAIL_APP_PASSWORD   - a 16-char Google App Password (NOT your normal password)
  EMAIL_TO             - where to send it (can be the same Gmail)
Optional:
  TRADIER_BASE         - defaults to the sandbox URL
  SEND_IF_EMPTY        - "1" to email even when nothing qualifies (default: skip empty)
  IGNORE_MARKET_HOURS  - "1" to send regardless of time (useful for a manual test run)
"""
import os
import sys
import smtplib
import datetime as dt
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import wheel_screener as ws
import spreads as sp
import discover
import positions

_SPREAD_SECTIONS = [
    ("Put credit spread",  "Put Credit Spreads (bullish, defined risk)"),
    ("Call credit spread", "Call Credit Spreads (bearish, defined risk)"),
    ("Iron condor",        "Iron Condors (neutral, defined risk)"),
    ("Long Straddle",      "Long Straddles (big-move bet, defined risk, requires confirmed earnings in-window)"),
    ("Long Strangle",      "Long Strangles (big-move bet, defined risk, requires confirmed earnings in-window)"),
]


def _now_et():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return dt.datetime.utcnow() - dt.timedelta(hours=4)   # crude EDT fallback


def _market_open(now_et):
    if now_et.weekday() >= 5:
        return False
    mins = now_et.hour * 60 + now_et.minute
    return (9 * 60 + 30) <= mins <= (16 * 60)


def build_puts():
    rows = []
    for t in ws.PUT_TICKERS:
        try:
            passers, _ = ws.screen_puts(t)
            rows += passers
        except Exception as e:
            print(f"PUT {t}: ERROR {e}", file=sys.stderr)
    return ws._df(rows, ws.PUT_COLS, sort_by=("Ticker", "Score"), asc=(True, False))


def build_calls():
    rows = []
    for t, cost in ws.HOLDINGS.items():
        try:
            passers, _ = ws.screen_calls(t, cost)
            rows += passers
        except Exception as e:
            print(f"CALL {t}: ERROR {e}", file=sys.stderr)
    return ws._df(rows, ws.CALL_COLS, sort_by=("Ticker", "Score"), asc=(True, False))


def build_spreads():
    rows = []
    for t in ws.PUT_TICKERS:
        try:
            rows += sp.screen_spreads(t)
        except Exception as e:
            print(f"SPREAD {t}: ERROR {e}", file=sys.stderr)
    return sp._df(rows)


def build_discover():
    """Live scan of tickers outside the watchlist (see discover.py); falls back
    to the daily-Action snapshot if the live scan errors, same as app.py."""
    try:
        d = discover.run_discovery()
    except Exception as e:
        print(f"DISCOVER: live scan ERROR {e}", file=sys.stderr)
        d = ws.load_volume_leaders()
    if not d:
        return ws._df([], ws.PUT_COLS), sp._df([])
    dp = ws._df(d.get("puts", []), ws.PUT_COLS, sort_by=("Ticker", "Score"), asc=(True, False))
    dspreads = sp._df(d.get("call_spreads", []))
    return dp, dspreads


def build_positions():
    """Live-quotes every OPEN_POSITIONS entry (see positions.py) -- the exact
    same contract lookup and math as the app's Open Positions section, so the
    numbers here always match what you'd see if you opened the app right now."""
    df, errs = positions.build_positions_table()
    for e in errs:
        print(f"POSITION: {e}", file=sys.stderr)
    return df


def build_closed_positions():
    df, errs = positions.build_closed_positions_table()
    for e in errs:
        print(f"CLOSED POSITION: {e}", file=sys.stderr)
    return df


def _table(df, fmt):
    return fmt(df).to_html(index=False, border=0)


def _spreads_html(ds):
    out = ""
    for key, title in _SPREAD_SECTIONS:
        sub = ds[ds["Strategy"] == key] if len(ds) else ds
        if not len(sub):
            continue
        disp = sub.drop(columns=["Strategy"])
        for lc in ("Put Legs", "Call Legs"):     # hide the empty side for one-sided spreads
            if lc in disp.columns and (disp[lc].fillna("") == "").all():
                disp = disp.drop(columns=[lc])
        for mc in ("Max Profit", "Max Profit (Best)"):   # unlimited-upside strategies -- see app.py
            if mc in disp.columns and disp[mc].isna().all():
                disp = disp.drop(columns=[mc])
        if "Breakeven" in disp.columns and (disp["Breakeven"] == "-").all():
            disp = disp.drop(columns=["Breakeven"])
        out += f"<h3>{title}</h3>{_table(disp, sp._fmt)}"
    return out


def _discover_html(dp, dspreads):
    def _section(title, df, fmt):
        if not len(df):
            return f"<h3>{title}</h3><p class='empty'>None qualify right now.</p>"
        return f"<h3>{title}</h3>{_table(df, fmt)}"

    disp = dspreads.drop(columns=["Strategy", "Put Legs"]) if len(dspreads) else dspreads
    return (_section("Puts", dp, ws._fmt)
            + _section("Call Credit Spreads (defined-risk)", disp, sp._fmt))


def html_email(puts, calls, spreads, discover_puts, discover_spreads, open_pos, closed_pos, now_et):
    style = ("<style>body{font-family:Arial,Helvetica,sans-serif;color:#111}"
             "h2{border-bottom:2px solid #1F3864;padding-bottom:4px;margin-top:26px}"
             "h3{margin:16px 0 4px}"
             "table{border-collapse:collapse;width:100%;font-size:12px;margin-top:6px}"
             "th,td{border:1px solid #ccc;padding:5px 8px;text-align:right}"
             "th{background:#1F3864;color:#fff}"
             "td:first-child,th:first-child{text-align:left}"
             "p.empty{color:#888}</style>")

    def section(title, df, fmt):
        if df is None or not len(df):
            return f"<h2>{title}</h2><p class='empty'>None qualify right now.</p>"
        return f"<h2>{title}</h2>{_table(df, fmt)}"

    spreads_body = _spreads_html(spreads) if len(spreads) else "<p class='empty'>None qualify right now.</p>"
    discover_body = _discover_html(discover_puts, discover_spreads)

    def empty_section(title, df, fmt, empty_msg):
        if df is None or not len(df):
            return f"<h2>{title}</h2><p class='empty'>{empty_msg}</p>"
        return f"<h2>{title}</h2>{_table(df, fmt)}"

    def financials_html(title, df):
        if df is None or not len(df):
            return f"<h3>{title}</h3><p class='empty'>No data.</p>"
        return f"<h3>{title}</h3>{df.to_html(index=False, border=0)}"

    open_fin = positions.build_open_financials(open_pos)
    closed_fin = positions.build_closed_financials(closed_pos)
    combined_fin = positions.build_combined_financials(open_pos, closed_pos)

    return (f"<html><head>{style}</head><body>"
            f"<h1>Options Screener</h1>"
            f"<p>{now_et:%A %b %d, %Y  %I:%M %p ET}. Educational only, not financial advice. "
            f"Prices via Tradier sandbox (~15 min delayed).</p>"
            f"{section('Cash-Secured Puts', puts, ws._fmt)}"
            f"{section('Covered Calls', calls, ws._fmt)}"
            f"<h2>Multi-Leg Strategies</h2>{spreads_body}"
            f"<h2>Discover: High-Open-Interest Contracts (outside your watchlist)</h2>{discover_body}"
            f"{empty_section('Open Positions', open_pos, positions._fmt, 'No open positions tracked.')}"
            f"{financials_html('Financials (unrealized)', open_fin)}"
            f"{empty_section('Closed Positions (last 30 days)', closed_pos, positions._fmt, 'No closed positions in the last 30 days.')}"
            f"{financials_html('Financials (realized)', closed_fin)}"
            f"<h2>Financials (Open + Closed combined)</h2>{combined_fin.to_html(index=False, border=0)}"
            "</body></html>")


def send(subject, html, user, pw, to):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText("Open in an HTML-capable client to see the tables.", "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())


def main():
    now_et = _now_et()
    # A manual "Run workflow" (workflow_dispatch) always sends, ignoring market hours
    # and the empty-list skip, so you can test it any time.
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    ignore_hours = manual or os.environ.get("IGNORE_MARKET_HOURS") == "1"
    send_if_empty = manual or os.environ.get("SEND_IF_EMPTY") == "1"

    if not ignore_hours and not _market_open(now_et):
        print(f"Market closed at {now_et:%Y-%m-%d %H:%M ET}; skipping.")
        return

    user = os.environ.get("EMAIL_USER")
    pw   = os.environ.get("EMAIL_APP_PASSWORD")
    to   = os.environ.get("EMAIL_TO") or user
    if not (user and pw):
        print("EMAIL_USER / EMAIL_APP_PASSWORD not set; cannot send.", file=sys.stderr)
        sys.exit(1)

    puts     = build_puts()
    calls    = build_calls()
    spreads  = build_spreads()
    d_puts, d_spreads = build_discover()
    open_pos = build_positions()
    closed_pos = build_closed_positions()
    # Open/closed positions count toward "is there anything worth sending" too --
    # your portfolio status is reason enough to send even on a quiet screener day.
    total    = (len(puts) + len(calls) + len(spreads) + len(d_puts) + len(d_spreads)
               + len(open_pos) + len(closed_pos))
    if total == 0 and not send_if_empty:
        print("Nothing qualifies anywhere; not sending (set SEND_IF_EMPTY=1 to override).")
        return

    subject = (f"Screener: {len(puts)} puts, {len(calls)} calls, {len(spreads)} spreads, "
               f"{len(d_puts) + len(d_spreads)} discovered, {len(open_pos)} open positions "
               f"- {now_et:%b %d %I:%M %p ET}")
    send(subject, html_email(puts, calls, spreads, d_puts, d_spreads, open_pos, closed_pos, now_et),
        user, pw, to)
    print(f"Sent: {subject} -> {to}")


if __name__ == "__main__":
    main()
