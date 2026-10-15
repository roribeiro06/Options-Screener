#!/usr/bin/env python3
"""
premium_log.py -- the observed-premium log behind the AvgPremium column.

build_premium_log.py snapshots every watchlist ticker's REAL option chain once a
day and appends what the market actually paid (annualized yield at the bid) at
fixed OTM levels and expirations, together with the implied volatility at that
strike. This module stores those snapshots and answers the question the
AvgPremium column asks:

    "What has this ticker's {put|call} actually paid, annualized, at about this
     OTM%, this IV and this DTE?"   ->  a [25th, 75th percentile] yield band.

Storage: premium_log/YYYY-MM.jsonl, one JSON object per (date, ticker, kind):
    {"d": "2026-10-15", "t": "MRVL", "k": "p", "s": 123.45,
     "r": [[otm_pct, dte, iv_pct, ann_yield_pct], ...]}
(all percentages, 1 decimal). Re-running a day replaces that day's lines.

Until a ticker has enough distinct days of data near a query's OTM/IV/DTE
(MIN_DAYS / MIN_OBS), observed_band() returns None and the caller falls back to
the modeled estimate from history_premiums.json.
"""
import bisect
import datetime as dt
import json
import os
import time

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "premium_log")
KEEP_DAYS = 400

OTM_LEVELS = [5.0, 7.5, 10.0, 12.5, 15.0, 17.5, 20.0, 22.5, 25.0]   # OTM %, the grid snapshots are taken on
OTM_TOL = 1.5                  # a query uses grid levels within +-1.5 OTM points
DTE_TOL_MIN, DTE_TOL_FRAC = 4, 0.20     # +-max(4 days, 20%) around the query's DTE
IV_TOL_MIN, IV_TOL_FRAC = 5.0, 0.15     # +-max(5 vol points, 15% relative) around the query's IV
MIN_DAYS = 8                   # distinct trading days that must back a band
MIN_OBS = 8
CACHE_TTL_S = 6 * 3600

_index = None                  # {(ticker, kind): {level_index: ([dte...] sorted, [(dte, iv, yld, day)...])}}
_loaded_at = 0.0
_memo = {}


def _shard(day):
    return os.path.join(LOG_DIR, f"{str(day)[:7]}.jsonl")


def write_day(day, records):
    """Replace every line for `day` in its monthly shard with `records`
    (dicts with d/t/k/s/r). Safe to re-run the same day."""
    os.makedirs(LOG_DIR, exist_ok=True)
    path = _shard(day)
    kept = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("d") != str(day):
                        kept.append(line)
                except ValueError:
                    continue
    with open(path, "w", encoding="utf-8") as f:
        for line in kept:
            f.write(line + "\n")
        for r in records:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def _load(force=False):
    """Index every record from the last KEEP_DAYS days by (ticker, kind) and
    OTM grid level, each level's observations sorted by DTE for bisect lookups."""
    global _index, _loaded_at
    if _index is not None and not force and time.time() - _loaded_at < CACHE_TTL_S:
        return _index
    cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    raw = {}
    if os.path.isdir(LOG_DIR):
        for name in sorted(os.listdir(LOG_DIR)):
            if not name.endswith(".jsonl"):
                continue
            with open(os.path.join(LOG_DIR, name), encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("d", "") < cutoff:
                        continue
                    for otm, dte, iv, yld in rec.get("r", []):
                        try:
                            li = OTM_LEVELS.index(float(otm))
                        except ValueError:
                            continue
                        raw.setdefault((rec["t"], rec["k"]), {}).setdefault(li, []).append(
                            (float(dte), float(iv), float(yld), rec["d"]))
    idx = {}
    for key, levels in raw.items():
        idx[key] = {}
        for li, obs in levels.items():
            obs.sort(key=lambda o: o[0])
            idx[key][li] = ([o[0] for o in obs], obs)
    _index, _loaded_at = idx, time.time()
    _memo.clear()
    return idx


def reset():
    """Drop the cache (tests, or right after a snapshot is written)."""
    global _index
    _index = None
    _memo.clear()


def _pct(sorted_vals, p):
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def observed_band(symbol, kind, otm, dte, iv=None):
    """[low, high] annualized-yield FRACTIONS (25th-75th percentile of what this
    ticker's `kind` ('put'/'call') options actually paid at about this OTM
    (fraction), DTE and IV (fraction)), or None if fewer than MIN_DAYS distinct
    days / MIN_OBS observations back it. `iv` None -> no IV filter."""
    k = "p" if kind == "put" else "c"
    key = (symbol, k, round(otm, 3), int(dte), round(iv, 3) if iv else None)
    if key in _memo:
        return _memo[key]
    idx = _load().get((symbol, k))
    out = None
    if idx and otm and dte and dte > 0:
        otm_pct = otm * 100
        dtol = max(DTE_TOL_MIN, DTE_TOL_FRAC * dte)
        ivp = iv * 100 if iv else None
        itol = max(IV_TOL_MIN, IV_TOL_FRAC * ivp) if ivp else None
        ylds, days = [], set()
        for li, level in enumerate(OTM_LEVELS):
            if abs(level - otm_pct) > OTM_TOL or li not in idx:
                continue
            dtes, obs = idx[li]
            for o in obs[bisect.bisect_left(dtes, dte - dtol):bisect.bisect_right(dtes, dte + dtol)]:
                if ivp is not None and abs(o[1] - ivp) > itol:
                    continue
                ylds.append(o[2])
                days.add(o[3])
        if len(ylds) >= MIN_OBS and len(days) >= MIN_DAYS:
            ylds.sort()
            out = [_pct(ylds, 0.25) / 100.0, _pct(ylds, 0.75) / 100.0]
    _memo[key] = out
    return out


def coverage(symbol=None):
    """{(ticker, kind): distinct days logged} -- a quick look at how much data exists."""
    days = {}
    for (t, k), levels in _load(force=True).items():
        if symbol and t != symbol:
            continue
        days[(t, k)] = len({o[3] for _, obs in levels.values() for o in obs})
    return days
