#!/usr/bin/env python3
"""Offline unit test of the rule engine (no network). Also writes a formatted SAMPLE."""
import pandas as pd
import wheel_screener as ws

# (label, ticker, spot, strike, premium, iv, dte, earnings_in_window)
CASES = [
    ("AVGO 300P (from your feed)",  "AVGO", 374.46, 300, 7.23, 0.40, 64, True),
    ("SPY 735P (2% OTM)",           "SPY",  750.0, 735, 8.00, 0.167, 35, False),
    ("SPY 711P (the one you sold)", "SPY",  750.0, 711, 3.87, 0.150, 36, False),
    ("XYZ 90P (high-vol, clean)",   "XYZ",  100.0,  90, 2.10, 0.55,  40, False),
]

full = []
for label, tkr, spot, strike, prem, iv, dte, earn in CASES:
    res = ws.evaluate_put({"strike": strike, "premium": prem, "iv": iv}, spot, dte, earn,
                          otm_min=ws.otm_min_for(tkr), is_index=(tkr in ws.INDEX_TICKERS))
    rec = {"Case": label, "Ticker": tkr, "Spot": spot, "Strike": strike,
           "Expiration": "(example)", "DTE": dte, "EarningsDate": earn, **res}
    full.append(rec)

show = ["Case", "OTM_%", "AnnYield_%", "YieldNeeded_%", "Delta_%", "RiskPrem_%", "PASS", "Reasons"]
print(pd.DataFrame(full)[show].to_string(index=False))

by = {r["Case"]: r for r in full}
assert by["AVGO 300P (from your feed)"]["PASS"] is False
assert "spans earnings" in by["AVGO 300P (from your feed)"]["Reasons"]
assert by["SPY 735P (2% OTM)"]["PASS"] is False
assert by["SPY 711P (the one you sold)"]["PASS"] is False
assert by["XYZ 90P (high-vol, clean)"]["PASS"] is True
print("\nAll assertions passed.")

cols = ["Ticker", "Spot", "Strike", "Expiration", "DTE", "OTM_%", "Premium",
        "AnnYield_%", "YieldNeeded_%", "Delta_%", "Tbill_%", "RiskPrem_%",
        "IV", "EarningsDate", "Reasons"]
fdf = pd.DataFrame(full)
df_pass = fdf[fdf["PASS"]][cols]
df_near = fdf[~fdf["PASS"]][cols]
settings = pd.DataFrame({
    "Setting": ["POP min", "POP max", "DTE min", "DTE max",
                "Yield hurdle base (25-OTM)", "Min risk premium vs T-bill",
                "IV Rank min", "Use IV Rank", "Premium basis"],
    "Value": [ws.POP_MIN, ws.POP_MAX, ws.DTE_MIN, ws.DTE_MAX,
              ws.YIELD_HURDLE_BASE, ws.MIN_RISK_PREMIUM, ws.IVR_MIN,
              ws.USE_IVR, ws.PREMIUM_BASIS]})
ws.write_report([("Qualifying", df_pass), ("Near Misses", df_near)], settings,
               "qualifying_contracts_SAMPLE.xlsx")
print("Wrote qualifying_contracts_SAMPLE.xlsx")
