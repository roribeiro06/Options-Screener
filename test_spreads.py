import datetime as dt
import pandas as pd
import wheel_screener as ws
import spreads as sp

def P(s,d,b,a): return {"type":"put","strike":s,"delta":d,"bid":b,"ask":a,"iv":0.50,"oi":5000}
def C(s,d,b,a): return {"type":"call","strike":s,"delta":d,"bid":b,"ask":a,"iv":0.50,"oi":5000}
CHAIN = [
    P(360,-0.38,10,10.4), P(355,-0.30,8,8.3), P(345,-0.22,6,6.3), P(340,-0.15,4,4.3),
    P(335,-0.12,3,3.3), P(325,-0.08,2,2.2), P(320,-0.06,1.5,1.7),
    C(395,0.30,8,8.3), C(405,0.22,6,6.3), C(410,0.15,4,4.3), C(415,0.12,3.5,3.8), C(430,0.08,1.8,2.0),
]
EXP = (dt.date.today() + dt.timedelta(45)).isoformat()
ws.td_quote = lambda s: 375.0
ws.get_earnings_date = lambda s: None
ws.td_expirations = lambda s: [EXP]         # spreads now use their own expiration path
ws.td_chain = lambda s, exp: CHAIN
# This fixture's strikes predate the OTM-floor / open-interest filters added to wheel_screener.py
# since this test was written; relax them here so the synthetic chain still exercises all three
# strategy types (production code / config is untouched).
ws.OTM_MIN_OTHER = 0.05
# spread criteria defaults
sp.SPREAD_POP_MIN, sp.SPREAD_POP_MAX, sp.SPREAD_DTE_MIN, sp.SPREAD_DTE_MAX, sp.ROR_ANN_MIN = 0.65, 0.75, 7, 90, 0.25

df = sp._df(sp.screen_spreads("AVGO"))
pd.set_option("display.width", 220, "display.max_columns", 20)
print(df[["Strategy","Put Legs","Call Legs","Max Profit","MaxLoss","ROR_%","AnnROR_%","POP_%"]].to_string(index=False))
assert set(df["Strategy"]) == {"Put credit spread","Call credit spread","Iron condor"}, set(df["Strategy"])
print("\nspreads engine OK (independent POP/DTE window)")
