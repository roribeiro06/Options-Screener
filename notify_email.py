#!/usr/bin/env python3
"""
notify_email.py -- run the cash-secured-put screener headlessly and email the results.

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


def _now_et():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # crude fallback: assume EDT (UTC-4)
        return dt.datetime.utcnow() - dt.timedelta(hours=4)


def _market_open(now_et):
    if now_et.weekday() >= 5:              # Sat/Sun
        return False
    mins = now_et.hour * 60 + now_et.minute
    return (9 * 60 + 30) <= mins <= (16 * 60)   # 9:30 - 16:00 ET


def build_puts_df():
    rows = []
    for t in ws.PUT_TICKERS:
        try:
            passers, _ = ws.screen_puts(t)
            rows += passers
        except Exception as e:
            print(f"{t}: ERROR {e}", file=sys.stderr)
    return ws._df(rows, ws.PUT_COLS, sort_by=("Ticker", "Score"), asc=(True, False))


def html_email(df, now_et):
    table = ws._fmt(df).to_html(index=False, border=0)
    style = ("<style>body{font-family:Arial,Helvetica,sans-serif;color:#111}"
             "table{border-collapse:collapse;width:100%;font-size:13px}"
             "th,td{border:1px solid #ccc;padding:6px 9px;text-align:right}"
             "th{background:#1F3864;color:#fff}"
             "td:first-child,th:first-child{text-align:left}</style>")
    return (f"<html><head>{style}</head><body>"
            f"<h2>Cash-Secured Puts &mdash; {len(df)} qualifying</h2>"
            f"<p>{now_et:%A %b %d, %Y  %I:%M %p ET}. Ranked by Score "
            f"(AnnYield / IV^{getattr(ws,'SCORE_IV_EXP',1.0):g} "
            f"&times; POP^{getattr(ws,'SCORE_POP_EXP',1.0):g} "
            f"&times; (365/DTE)^{getattr(ws,'SCORE_DTE_EXP',0.5):g}).</p>"
            f"{table}"
            "<p style='color:#888;font-size:11px'>Educational only, not financial advice. "
            "Prices via Tradier sandbox (~15 min delayed). Verify every contract in your broker.</p>"
            "</body></html>")


def send(subject, html, user, pw, to):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText("Open in an HTML-capable client to see the table.", "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())


def main():
    now_et = _now_et()
    if os.environ.get("IGNORE_MARKET_HOURS") != "1" and not _market_open(now_et):
        print(f"Market closed at {now_et:%Y-%m-%d %H:%M ET}; skipping.")
        return

    user = os.environ.get("EMAIL_USER")
    pw   = os.environ.get("EMAIL_APP_PASSWORD")
    to   = os.environ.get("EMAIL_TO") or user
    if not (user and pw):
        print("EMAIL_USER / EMAIL_APP_PASSWORD not set; cannot send.", file=sys.stderr)
        sys.exit(1)

    df = build_puts_df()
    if not len(df) and os.environ.get("SEND_IF_EMPTY") != "1":
        print("No qualifying puts; not sending (set SEND_IF_EMPTY=1 to override).")
        return

    subject = f"Wheel screener: {len(df)} puts - {now_et:%b %d %I:%M %p ET}"
    send(subject, html_email(df, now_et), user, pw, to)
    print(f"Sent: {subject} -> {to}")


if __name__ == "__main__":
    main()
