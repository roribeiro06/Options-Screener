#!/usr/bin/env python3
"""
notify_early_trend_email.py -- run the Early Trend Screener headlessly and
email the top results (see early_trend.py for the full 3-stage funnel and
Score formula, and pages/2_Early_Trend.py for the live page this mirrors).

Designed to run on a schedule by GitHub Actions (see
.github/workflows/early-trend-email.yml) once daily at 10:00 ET. GitHub
Actions cron is UTC-only and doesn't shift for US daylight saving, so the
workflow fires at BOTH 14:00 and 15:00 UTC (covering EDT and EST) and this
script only actually sends on whichever tick lands within TIME_TOLERANCE_MIN
of the real 10:00 ET target -- same pattern notify_email.py uses for market
hours, just a single daily target instead of a whole-day window.

Required environment variables (same GitHub repo Secrets notify_email.py uses):
  TRADIER_TOKEN        - Stage 1's candidate pool and Stage 3's options tilt both need it
  EMAIL_USER           - the Gmail address that SENDS the mail
  EMAIL_APP_PASSWORD   - a 16-char Google App Password (NOT your normal password)
  EMAIL_TO             - where to send it (can be the same Gmail)
Optional:
  TRADIER_BASE         - defaults to the sandbox URL
  SEND_IF_EMPTY        - "1" to email even when nothing qualifies (default: skip empty)
  IGNORE_TIME          - "1" to send regardless of time (useful for a manual test run)
"""
import os
import sys
import datetime as dt

import pandas as pd

import early_trend as et
from notify_email import _now_et, send

TARGET_HOUR_ET = 10          # 10:00 AM ET
TIME_TOLERANCE_MIN = 20      # accept a tick up to this many minutes after the target,
                              # since GitHub Actions cron can run a few minutes late and
                              # the wrong-season UTC tick needs to be rejected outright
TOP_N = 10   # a bit more context than the live page's adjustable default (5) -- there's
             # no sidebar in an email to loosen this after the fact


def _at_target_time(now_et):
    if now_et.weekday() >= 5:
        return False
    mins_since_target = (now_et.hour * 60 + now_et.minute) - (TARGET_HOUR_ET * 60)
    return 0 <= mins_since_target <= TIME_TOLERANCE_MIN


def _fmt(df):
    d = df.copy()
    d["Price"] = "$" + d["Price"].round(2).astype(str)
    d["Score"] = d["Score"].round(2)
    for c in ("Above Pivot %", "Base Range %", "Volatility %", "% of 52wk High"):
        d[c] = d[c].round(1).astype(str) + "%"
    return d


def html_email(df, total_qualifying, now_et):
    style = ("<style>body{font-family:Arial,Helvetica,sans-serif;color:#111}"
             "h1{border-bottom:2px solid #1F3864;padding-bottom:4px}"
             "table{border-collapse:collapse;width:100%;font-size:12px;margin-top:6px}"
             "th,td{border:1px solid #ccc;padding:5px 8px;text-align:left;vertical-align:top}"
             "th{background:#1F3864;color:#fff}"
             "td:nth-child(9){max-width:420px}"
             "p.empty{color:#888}</style>")
    if df is None or not len(df):
        body = "<p class='empty'>No tickers currently qualify.</p>"
    else:
        body = df.to_html(index=False, border=0)
    return (f"<html><head>{style}</head><body>"
            f"<h1>Early Trend Screener</h1>"
            f"<p>{now_et:%A %b %d, %Y  %I:%M %p ET}. {total_qualifying} tickers qualify, "
            f"showing top {len(df) if df is not None else 0} by Score. Educational only, not "
            f"financial advice -- no rules-based screen can guarantee catching a trend before "
            f"it runs. See backtest_early_trend.py for how these exact rules performed "
            f"historically.</p>"
            f"{body}"
            "</body></html>")


def main():
    now_et = _now_et()
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    ignore_time = manual or os.environ.get("IGNORE_TIME") == "1"
    send_if_empty = manual or os.environ.get("SEND_IF_EMPTY") == "1"

    if not ignore_time and not _at_target_time(now_et):
        print(f"Not the 10:00 ET target window at {now_et:%Y-%m-%d %H:%M ET}; skipping.")
        return

    user = os.environ.get("EMAIL_USER")
    pw = os.environ.get("EMAIL_APP_PASSWORD")
    to = os.environ.get("EMAIL_TO") or user
    if not (user and pw):
        print("EMAIL_USER / EMAIL_APP_PASSWORD not set; cannot send.", file=sys.stderr)
        sys.exit(1)

    results = et.run_scan()
    if not results and not send_if_empty:
        print("Nothing qualifies; not sending (set SEND_IF_EMPTY=1 to override).")
        return

    shown = results[:TOP_N]
    for r in shown:
        r["notes"] = et.build_note(r)
    df = (pd.DataFrame(shown)[list(et.DISPLAY_COLS.keys()) + ["notes"]]
          .rename(columns={**et.DISPLAY_COLS, "notes": "Notes"}))
    df = _fmt(df) if len(df) else df

    subject = f"Early Trend Screener: {len(results)} qualify, top {len(shown)} by Score - {now_et:%b %d %I:%M %p ET}"
    send(subject, html_email(df, len(results), now_et), user, pw, to)
    print(f"Sent: {subject} -> {to}")


if __name__ == "__main__":
    main()
