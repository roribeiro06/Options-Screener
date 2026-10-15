#!/usr/bin/env python3
"""
build_etf_holdings.py -- refresh each screened ETF's TOP-5 holdings.

The screener leaves an ETF out of every window in which one of its top-5 holdings
reports earnings (wheel_screener.earnings_blocks / ETF_TOP_HOLDINGS). Holdings drift,
so this rebuilds etf_holdings.json from stockanalysis.com's holdings pages; the
screener loads that file at import and falls back to the hard-coded lists in
wheel_screener.py if it is missing or unreadable. Run quarterly by
build-etf-holdings.yml (and on demand).

Which ETFs: every key already in ETF_TOP_HOLDINGS, plus any ticker in PUT_TICKERS that
Schwab classifies as an ETF/ETN (so adding a new ETF to the watchlist is picked up here
automatically; skipped quietly when no Schwab login is available).

Safety: an ETF whose page can't be fetched/parsed (or yields fewer than 5 usable US/KRX
symbols) KEEPS its previous list and is reported loudly; if every ETF fails the run
exits non-zero and the file is not touched. Korean KRX codes become Yahoo ".KS" symbols.
"""
import datetime as dt
import json
import os
import re
import sys
import time

import requests

import wheel_screener as ws

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etf_holdings.json")
URL = "https://stockanalysis.com/etf/{}/holdings/"
HEADERS = {"User-Agent": "Mozilla/5.0 (options-screener holdings refresh)"}
TOP_N = 5
_US = re.compile(r"^[A-Z]{1,5}([.\-][A-Z])?$")
_KRX = re.compile(r"^KRX:(\d{6})$")
_NOT_EQUITY = {"USD", "CASH", "EUR", "KRW", "JPY", "GBP", "TWD", "HKD", "CAD", "CHF", "AUD"}


def parse_holdings(html, top_n=TOP_N):
    """Top `top_n` usable symbols, in rank order, from a stockanalysis holdings page.
    US tickers stay as-is; 'KRX:005930' -> '005930.KS'; cash, futures, swaps and anything
    else that isn't a listed equity is skipped (it has no earnings date to check)."""
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = re.findall(r'<td[^>]*class="([^"]*)"[^>]*>(.*?)</td>', row, re.S)
        sym = next((re.sub(r"<[^>]+>", "", v).strip() for c, v in cells if c.startswith("sym")), "")
        if not sym:
            continue
        m = _KRX.match(sym)
        if m:
            sym = m.group(1) + ".KS"
        elif not _US.match(sym) or sym in _NOT_EQUITY:
            continue
        if sym not in out:
            out.append(sym)
        if len(out) == top_n:
            break
    return out


def fetch_etf(etf, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(URL.format(etf.lower()), headers=HEADERS, timeout=30)
            r.raise_for_status()
            top = parse_holdings(r.text)
            if len(top) == TOP_N:
                return top
            last = f"only {len(top)} usable symbols parsed"
        except Exception as e:
            last = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(last)


def detect_new_etfs(known):
    """PUT_TICKERS entries Schwab calls ETFs that aren't in `known` yet (best effort)."""
    found = []
    try:
        import schwab_data
        if not ws.use_schwab():
            return found
        for t in ws.PUT_TICKERS:
            if t in known:
                continue
            try:
                if schwab_data.earnings_info(t)[0]:
                    found.append(t)
            except Exception:
                continue
    except Exception as e:
        print(f"(ETF auto-detect skipped: {e})", file=sys.stderr)
    return found


def main():
    previous = dict(ws.ETF_TOP_HOLDINGS)          # file contents if present, else the built-ins
    etfs = list(previous) + detect_new_etfs(previous)
    new, failed = {}, {}
    for etf in etfs:
        try:
            new[etf] = fetch_etf(etf)
            print(f"{etf}: {', '.join(new[etf])}")
        except Exception as e:
            failed[etf] = str(e)
            if etf in previous:
                new[etf] = previous[etf]            # keep the last good list
            print(f"::warning::{etf}: holdings refresh FAILED ({e})" + (" -- kept previous list" if etf in previous else " -- not added"),
                  file=sys.stderr)
    if len(failed) == len(etfs):
        print("every ETF failed; file not written", file=sys.stderr)
        sys.exit(1)
    changes = []
    for etf, top in new.items():
        old = previous.get(etf)
        if old != top:
            changes.append((etf, old, top))
    out = {"_meta": {"fetched": dt.date.today().isoformat(), "source": "stockanalysis.com", "top_n": TOP_N,
                     "failed": sorted(failed)},
           "etfs": new}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    report(changes, failed)


def report(changes, failed):
    nl = chr(10)
    lines = ["### ETF top-5 holdings refresh", ""]
    if changes:
        lines += ["| ETF | Before | After |", "|---|---|---|"]
        lines += [f"| {e} | {', '.join(o) if o else '(new)'} | {', '.join(n)} |" for e, o, n in changes]
    else:
        lines += ["No changes -- every ETF's top 5 is the same as before."]
    if failed:
        lines += ["", "**Refresh FAILED (previous list kept):** " + ", ".join(f"{k} ({v})" for k, v in failed.items())]
    print(nl.join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(nl.join(lines) + nl)


if __name__ == "__main__":
    main()
