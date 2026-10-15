"""Offline tests for build_premium_log.py / premium_log.py / the AvgPremium wrapper."""
import datetime as dt
import json
import math
import os
import tempfile

import build_premium_log as B
import premium_log as PL
import wheel_screener as ws

PL.LOG_DIR = tempfile.mkdtemp()
today = dt.date(2026, 10, 15)

# --- observations(): a smooth synthetic put chain, spot 100, 30 DTE
def chain(spot=100.0, dte=30, iv=0.50):
    out = []
    for k in range(50, 151):
        otm_put = (spot - k) / spot
        bid = max(0.05, spot * 0.4 * iv * math.sqrt(dte / 365) * math.exp(-abs(k - spot) / (spot * 0.12)))
        out.append({"type": "put" if k < spot else "call", "strike": float(k), "bid": round(bid, 2), "ask": round(bid + .05, 2),
                    "iv": iv + 0.002 * abs(k - spot) / 10, "oi": 500, "volume": 10, "delta": -0.2, "prevclose": bid})
    return out

obs = B.observations(chain(), 100.0, 30, "put")
assert [o[0] for o in obs] == [5.0, 7.5, 10.0, 12.5, 15.0, 17.5, 20.0, 22.5, 25.0], obs
assert all(o[1] == 30 for o in obs)
o15 = [o for o in obs if o[0] == 15.0][0]
k85 = [c for c in chain() if c["strike"] == 85.0][0]
assert abs(o15[3] - k85["bid"] / 85.0 * 365 / 30 * 100) < 0.2, (o15, k85)          # 15% OTM put == the 85 strike
assert abs(o15[2] - k85["iv"] * 100) < 0.15, (o15, k85)
calls = B.observations(chain(), 100.0, 30, "call")
assert len(calls) == 9 and calls[0][0] == 5.0
# illiquid / no-bid strikes are skipped; a big strike gap prevents interpolation across it
thin = [c for c in chain() if c["strike"] in (80.0, 95.0)]                         # 15-point hole between them
assert [o for o in B.observations(thin, 100.0, 30, "put") if 7.5 <= o[0] <= 17.5] == []
zero = [dict(c, bid=0) for c in chain()]
assert B.observations(zero, 100.0, 30, "put") == []
lowoi = [dict(c, oi=3) for c in chain()]
assert B.observations(lowoi, 100.0, 30, "put") == []

# --- pick_expirations
exps = [(today + dt.timedelta(days=d)).isoformat() for d in (2, 6, 8, 13, 15, 22, 29, 31, 45, 58, 61, 66, 94, 120)]
ch = B.pick_expirations(exps, today)
assert sorted(ch.values()) == [6, 13, 22, 29, 45, 61, 94], ch
assert 2 not in ch.values() and 120 not in ch.values()

# --- write_day is idempotent per day and keeps other days
rec = lambda d, y: {"d": d, "t": "TST", "k": "p", "s": 100.0, "r": [[15.0, 30, 50.0, y]]}
PL.write_day("2026-10-14", [rec("2026-10-14", 30.0)])
PL.write_day("2026-10-15", [rec("2026-10-15", 31.0)])
PL.write_day("2026-10-15", [rec("2026-10-15", 32.0)])            # re-run same day: replaces, not duplicates
lines = open(os.path.join(PL.LOG_DIR, "2026-10.jsonl")).read().strip().split("\n")
assert len(lines) == 2 and json.loads(lines[1])["r"][0][3] == 32.0, lines

# --- observed_band: needs MIN_DAYS distinct days, filters by OTM / DTE / IV
for i in range(10):
    d = (dt.date.today() - dt.timedelta(days=i + 1)).isoformat()
    yld = 28.0 + i                                   # 28 ... 37
    PL.write_day(d, [{"d": d, "t": "TST", "k": "p", "s": 100.0,
                      "r": [[15.0, 30, 50.0, yld], [15.0, 45, 50.0, 20.0], [10.0, 30, 50.0, 60.0], [15.0, 30, 90.0, 80.0]]}])
PL.reset()
b = PL.observed_band("TST", "put", 0.15, 30, 0.50)
assert b and abs(b[0] - 0.3025) < 0.005 and abs(b[1] - 0.3475) < 0.005, b          # 25th-75th pct of 28..37
assert PL.observed_band("TST", "put", 0.15, 30, 0.50)[0] == b[0]                    # memoized
assert abs(PL.observed_band("TST", "put", 0.15, 45, 0.50)[0] - 0.20) < 1e-9        # other DTE -> its own obs
assert abs(PL.observed_band("TST", "put", 0.10, 30, 0.50)[0] - 0.60) < 1e-9        # other OTM -> its own obs
assert abs(PL.observed_band("TST", "put", 0.15, 30, 0.90)[0] - 0.80) < 1e-9        # other IV -> its own obs
assert PL.observed_band("TST", "put", 0.15, 30, 0.20) is None                      # no obs near IV 20%
assert PL.observed_band("TST", "call", 0.15, 30, 0.50) is None                     # no call data
assert PL.observed_band("TST", "put", 0.30, 30, 0.50) is None                      # off the grid
assert PL.observed_band("NOPE", "put", 0.15, 30, 0.50) is None
# too few distinct days -> None
for i in range(10):                                   # wipe and keep only 5 days
    pass
PL.reset(); PL.LOG_DIR = tempfile.mkdtemp()
for i in range(5):
    d = (dt.date.today() - dt.timedelta(days=i + 1)).isoformat()
    PL.write_day(d, [{"d": d, "t": "FEW", "k": "p", "s": 1.0, "r": [[15.0, 30, 50.0, 30.0], [15.0, 31, 50.0, 31.0]]}])
PL.reset()
assert PL.observed_band("FEW", "put", 0.15, 30, 0.50) is None, "5 days < MIN_DAYS must fall back"

# --- the AvgPremium wrapper: observed -> plain text, otherwise modeled -> '~'
PL.reset(); PL.LOG_DIR = tempfile.mkdtemp()
for i in range(10):
    d = (dt.date.today() - dt.timedelta(days=i + 1)).isoformat()
    PL.write_day(d, [{"d": d, "t": "MRVL", "k": "p", "s": 1.0, "r": [[15.0, 30, 60.0, 28.0 + i]]}])
PL.reset()
band = ws.avg_premium_range("MRVL", 0.15, 30, "put", iv=0.60)
assert band.observed and ws.fmt_avg_band(band) == "30.2%-34.8%", ws.fmt_avg_band(band)
mod = ws.avg_premium_range("MRVL", 0.15, 30, "call", iv=0.60)         # no call log -> modeled (history_premiums.json)
if mod:
    assert not mod.observed and ws.fmt_avg_band(mod).startswith("~"), ws.fmt_avg_band(mod)
assert ws.fmt_avg_band(None) == "-"

# --- real Schwab-shaped chain (saved by schwab_probe.py), if present
sample = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schwab_sample_AAPL.json")
if os.path.exists(sample):
    import schwab_data
    j = json.load(open(sample))["chains"]
    by_exp = schwab_data._contracts(j["callExpDateMap"], "call")
    for e, lst in schwab_data._contracts(j["putExpDateMap"], "put").items():
        by_exp.setdefault(e, []).extend(lst)
    spot = j["underlyingPrice"]
    n = 0
    for e, lst in by_exp.items():
        d = (dt.date.fromisoformat(e) - dt.date(2026, 10, 12)).days
        if d >= 5:
            for kind in ("put", "call"):
                for o in B.observations(lst, spot, d, kind):
                    assert 0 < o[3] < 1000 and 0 < o[2] < 300, o
                    n += 1
    print("real AAPL chain observations:", n)

print("ALL PREMIUM-LOG TESTS PASSED")

# ---------------- earnings status (never silent) ----------------
import build_premium_log as B2
t0 = dt.date(2026, 10, 15)
# ETF
assert B2.earnings_status("SPY", t0) == ("none (ETF)", None, 0)
# real date from Yahoo/stockanalysis
ws.get_earnings_date = lambda s: dt.date(2026, 11, 18)
lab, d, sl = B2.earnings_status("MRVL", t0); assert (lab, d, sl) == ("2026-11-18", dt.date(2026, 11, 18), 0)
# no date anywhere -> estimated from Schwab's last report, with slack
ws.get_earnings_date = lambda s: None
ws.use_schwab = lambda: True
import schwab_data
schwab_data.earnings_info = lambda s: (False, dt.date(2026, 7, 30))
lab, d, sl = B2.earnings_status("AAPL", t0); assert lab == "est 2026-10-29" and sl == B2.EST_SLACK_DAYS, (lab, d)
# a last date long ago rolls forward by quarters
schwab_data.earnings_info = lambda s: (False, dt.date(2026, 1, 28))
lab, d, sl = B2.earnings_status("AAPL", t0); assert d >= t0 - dt.timedelta(days=10), (lab, d)
# fund detected by Schwab -> no earnings
schwab_data.earnings_info = lambda s: (True, None)
assert B2.earnings_status("XLK", t0)[0] == "none (ETF)"
# nothing at all -> explicitly 'not found'
schwab_data.earnings_info = lambda s: (False, None)
assert B2.earnings_status("ZZZ", t0) == ("not found", None, 0)
def boom(s): raise RuntimeError("down")
schwab_data.earnings_info = boom
assert B2.earnings_status("ZZZ", t0)[0] == "not found"
# window logic
e = dt.date(2026, 10, 29)
assert B2.spans_earnings(t0, dt.date(2026, 11, 6), e, 0) and not B2.spans_earnings(t0, dt.date(2026, 10, 23), e, 0)
assert B2.spans_earnings(t0, dt.date(2026, 10, 20), e, 10)          # estimate +-10d -> 10/19+ is excluded
assert not B2.spans_earnings(t0, dt.date(2026, 10, 16), e, 10)
assert not B2.spans_earnings(dt.date(2026, 11, 20), dt.date(2026, 12, 18), e, 10)   # report already past
assert not B2.spans_earnings(t0, dt.date(2026, 12, 18), None, 0)
# snapshot carries the label; 'not found' records are written but ignored by lookups
ws.td_quote = lambda s: 100.0
ws.td_expirations = lambda s: [(t0 + dt.timedelta(days=30)).isoformat()]
ws.td_chain = lambda s, e: chain()
ws.get_earnings_date = lambda s: None
schwab_data.earnings_info = lambda s: (False, None)
recs = B2.snapshot_ticker("ZZZ", t0)
assert recs and all(r["e"] == "not found" for r in recs), recs
PL.reset(); PL.LOG_DIR = tempfile.mkdtemp()
for i in range(10):
    dd = (dt.date.today() - dt.timedelta(days=i + 1)).isoformat()
    PL.write_day(dd, [dict(recs[0], d=dd)])
PL.reset()
assert PL.observed_band("ZZZ", "put", 0.15, 30, 0.50) is None, "'not found' days must be ignored"
for i in range(10):
    dd = (dt.date.today() - dt.timedelta(days=i + 1)).isoformat()
    PL.write_day(dd, [dict(recs[0], d=dd, e="2026-11-18")])
PL.reset()
assert PL.observed_band("ZZZ", "put", 0.15, 30, 0.50) is not None
# the summary/annotations
os.environ["GITHUB_STEP_SUMMARY"] = os.path.join(tempfile.mkdtemp(), "s.md")
B2.report_earnings({"AAA": "2026-11-18", "BBB": "est 2026-10-29", "CCC": "not found", "SPY": "none (ETF)"})
txt = open(os.environ["GITHUB_STEP_SUMMARY"]).read()
assert "| CCC | not found |" in txt and "NOT FOUND" in txt and "Estimated:** BBB" in txt, txt
print("ALL EARNINGS-STATUS TESTS PASSED")
