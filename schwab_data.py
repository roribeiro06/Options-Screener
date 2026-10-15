#!/usr/bin/env python3
"""
schwab_data.py -- Schwab Market Data adapter that returns the SAME shapes the
Tradier layer in wheel_screener.py returns, so the rest of the app is unchanged:

    quote(symbol)                -> last price (float) or None
    expirations(symbol)          -> ["2026-10-16", ...]
    chain(symbol, expiration)    -> [{"type","strike","bid","ask","prevclose","delta","iv","oi","volume"}, ...]
    history(symbol, start, end)  -> Tradier-shaped {"history": {"day": [{"date","close",...}]}}
    quotes(symbols)              -> Tradier-shaped {"quotes": {"quote": [{"symbol","last",...}]}}

Auth comes from schwab_auth.get_access_token(). Calls are throttled and retried on 429.
Option chains are fetched ONCE per symbol for the whole expiration window and cached briefly,
because the screeners ask for one expiration at a time (dozens of calls per ticker otherwise).
"""
import datetime as dt
import math
import threading
import time

import requests

import schwab_auth

BASE = "https://api.schwabapi.com/marketdata/v1"
MIN_INTERVAL_S = 0.6          # ~100 calls/min, safely under Schwab's 120/min cap
CHAIN_DAYS = 100              # one bulk fetch covers today..today+100d (spreads reach 60d, long legs 90d)
CHAIN_TTL_S = 60
_SYMBOL_MAP = {"VIX": "$VIX", "SPX": "$SPX", "NDX": "$NDX", "RUT": "$RUT", "DJX": "$DJX"}

_lock = threading.Lock()
_delayed = {}                 # what Schwab flagged as delayed this run: symbol -> where it was flagged
_last_call = [0.0]
_chain_cache = {}             # symbol -> (fetched_at, horizon_date, {exp_iso: [contract dicts]})


def _sym(symbol):
    return _SYMBOL_MAP.get(symbol.upper(), symbol.upper())


def _get(path, params):
    for attempt in range(4):
        with _lock:                                   # serialize + pace every call
            wait = MIN_INTERVAL_S - (time.time() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        r = requests.get(BASE + path, params=params, timeout=30,
                         headers={"Authorization": f"Bearer {schwab_auth.get_access_token()}",
                                  "Accept": "application/json"})
        if r.status_code == 429 and attempt < 3:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 401 and attempt < 1:      # token raced its expiry; force a refresh once
            schwab_auth.invalidate_access_token()
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Schwab request kept failing: " + path)


def delay_warning():
    """None while everything Schwab returned was real-time; otherwise a human-readable warning
    (Schwab flags delayed data per response: quotes carry `realtime`, chains carry `isDelayed`)."""
    if not _delayed:
        return None
    syms = ", ".join(sorted(_delayed)[:8]) + ("..." if len(_delayed) > 8 else "")
    return ("DELAYED DATA: Schwab flagged quotes/chains as delayed (not real-time) for " + syms +
            ". Check your Schwab market-data agreements / entitlements.")


def _flag(symbol, delayed, what):
    if delayed:
        _delayed[symbol.upper()] = what


def _num(x, default=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(v) else v


def quote(symbol):
    s = _sym(symbol)
    j = _get("/quotes", {"symbols": s, "fields": "quote"})
    _flag(s, (j.get(s) or {}).get("realtime") is False, "quote")
    q = (j.get(s) or {}).get("quote") or {}
    return _num(q.get("lastPrice")) or _num(q.get("mark")) or _num(q.get("closePrice"))


def earnings_info(symbol):
    """(is_etf, last_reported_earnings_date or None) from Schwab's quote fundamentals --
    used to ESTIMATE the next report (~91 days later) when Yahoo/stockanalysis can't be reached."""
    s = _sym(symbol)
    j = _get("/quotes", {"symbols": s, "fields": "fundamental"})
    e = j.get(s) or {}
    sub = str(e.get("assetSubType") or "").upper()
    is_etf = sub in ("ETF", "ETN", "ETC") or str(e.get("assetMainType") or "").upper() in ("MUTUAL_FUND", "INDEX")
    last = (e.get("fundamental") or {}).get("lastEarningsDate")
    try:
        last = dt.date.fromisoformat(str(last)[:10]) if last else None
    except ValueError:
        last = None
    return is_etf, last


def quotes(symbols):
    """Tradier-shaped bulk quotes (used by discover.py / early_trend.py)."""
    syms = [_sym(s) for s in symbols]
    j = _get("/quotes", {"symbols": ",".join(syms), "fields": "quote,fundamental"})
    out = []
    for s in syms:
        e = j.get(s)
        if not isinstance(e, dict):
            continue
        _flag(s, e.get("realtime") is False, "quote")
        q, f = e.get("quote") or {}, e.get("fundamental") or {}
        out.append({"symbol": s.lstrip("$") if s.startswith("$") else s,
                    "last": _num(q.get("lastPrice")) or _num(q.get("mark")),
                    "close": _num(q.get("closePrice")), "prevclose": _num(q.get("closePrice")),
                    "volume": int(_num(q.get("totalVolume"), 0)),
                    "average_volume": int(_num(f.get("avg10DaysVolume"), 0)),
                    "change_percentage": _num(q.get("netPercentChange"), 0.0)})
    return {"quotes": {"quote": out}}


def expirations(symbol):
    j = _get("/expirationchain", {"symbol": _sym(symbol)})
    return sorted({e["expirationDate"] for e in j.get("expirationList") or [] if e.get("expirationDate")})


def _contracts(side_map, kind):
    """Flatten one Schwab {'2026-10-16:4': {'335.0': [contract]}} map into per-expiration dicts."""
    out = {}
    for exp_key, strikes in (side_map or {}).items():
        exp = exp_key.split(":")[0]
        for _strike, lst in strikes.items():
            for c in lst:
                if c.get("nonStandard") or c.get("mini"):
                    continue                           # adjusted / mini contracts duplicate strikes
                delta = _num(c.get("delta"))
                if delta is not None and abs(delta) > 1.0:
                    delta = None                       # Schwab uses -999 for "no greeks"
                iv = _num(c.get("volatility"))
                iv = iv / 100.0 if iv is not None and 0 < iv < 1000 else 0   # percent -> fraction
                out.setdefault(exp, []).append({
                    "type": kind, "strike": float(c["strikePrice"]),
                    "bid": _num(c.get("bid"), 0) or 0, "ask": _num(c.get("ask"), 0) or 0,
                    "prevclose": _num(c.get("closePrice"), 0) or 0,
                    "delta": delta, "iv": iv,
                    "oi": int(_num(c.get("openInterest"), 0)), "volume": int(_num(c.get("totalVolume"), 0))})
    return out


def _fetch_chain(symbol, from_date, to_date):
    j = _get("/chains", {"symbol": _sym(symbol), "contractType": "ALL", "strategy": "SINGLE",
                         "fromDate": from_date.isoformat(), "toDate": to_date.isoformat()})
    _flag(symbol, j.get("isDelayed") is True or (j.get("underlying") or {}).get("delayed") is True, "chain")
    out = _contracts(j.get("callExpDateMap"), "call")
    for exp, lst in _contracts(j.get("putExpDateMap"), "put").items():
        out.setdefault(exp, []).extend(lst)
    return out


def chain(symbol, expiration):
    today = dt.date.today()
    exp_date = dt.date.fromisoformat(str(expiration)[:10])
    if exp_date > today + dt.timedelta(days=CHAIN_DAYS):       # beyond the bulk window: one-off fetch
        return _fetch_chain(symbol, exp_date, exp_date).get(exp_date.isoformat(), [])
    key = symbol.upper()
    hit = _chain_cache.get(key)
    if not hit or time.time() - hit[0] > CHAIN_TTL_S or hit[1] != today:
        hit = (time.time(), today, _fetch_chain(symbol, today, today + dt.timedelta(days=CHAIN_DAYS)))
        _chain_cache[key] = hit
    return list(hit[2].get(exp_date.isoformat(), []))


def history(symbol, start, end):
    """Daily candles as Tradier's /markets/history shape: {"history": {"day": [{"date","open","high","low","close","volume"}]}}."""
    to_ms = lambda d: int(dt.datetime.combine(d, dt.time()).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    j = _get("/pricehistory", {"symbol": _sym(symbol), "periodType": "year", "frequencyType": "daily",
                               "frequency": 1, "startDate": to_ms(start), "endDate": to_ms(end) + 86_400_000,
                               "needExtendedHoursData": "false"})
    days = []
    for c in j.get("candles") or []:
        # daily candles are stamped at midnight US Eastern (04:00/05:00 UTC), so the UTC date is the trading day
        d = dt.datetime.fromtimestamp(c["datetime"] / 1000, tz=dt.timezone.utc).date()
        days.append({"date": d.isoformat(), "open": c.get("open"), "high": c.get("high"),
                     "low": c.get("low"), "close": c.get("close"), "volume": c.get("volume")})
    return {"history": {"day": days}}
