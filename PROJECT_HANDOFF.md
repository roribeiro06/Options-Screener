# Options Screener — Project Handoff

A personal options-income screener (cash-secured puts, covered calls, and multi-leg
credit spreads / iron condors) that runs as a free Streamlit web app, pulls live data
from Tradier, emails results on a schedule, and precomputes historical premium bands.

## Where everything lives
- **GitHub repo:** `roribeiro06/Options-Screener` (public). This is the source of truth.
- **Live app:** deployed on Streamlit Community Cloud (share.streamlit.io) from that repo.
- **Data:** Tradier sandbox API (free). Token stored as a GitHub/Streamlit secret, never in code.

Nothing is tied to any one computer — edit the repo from anywhere and Streamlit redeploys.

## To continue on another computer
- **Claude Code (recommended):** `git clone https://github.com/roribeiro06/Options-Screener`,
  then run Claude Code in that folder. It edits files directly and can commit/push.
- **Claude.ai chat:** paste files or describe changes; copy edits back into GitHub.
- After editing `wheel_screener.py`/`spreads.py`/`app.py`, Streamlit auto-redeploys on commit,
  but a code change to an already-imported module needs a **Reboot** (Manage app -> Reboot) to load.

## Files
- **`app.py`** — Streamlit UI. Sections: Cash-Secured Puts, Covered Calls, Contract Lookup
  (sell-side, any ticker), Multi-Leg Strategies (put/call credit spreads, iron condors), Legend.
  Loads TRADIER_TOKEN from st.secrets; 30-min market-clock auto-refresh; editable criteria panels.
- **`wheel_screener.py`** — the engine. Tradier data funcs, Black-Scholes, `evaluate_put`/`evaluate_call`,
  `screen_puts`/`screen_calls`, `lookup_contracts`, the Score, liquidity, cash/target columns,
  historical AvgPremium lookup, all config constants at the top.
- **`spreads.py`** — multi-leg engine (credit spreads + iron condors). Reuses wheel_screener.
- **`build_history.py`** — offline job: pulls ~1yr of Tradier stock prices, models typical put/call
  premiums per ticker by OTM%/DTE bucket (realized-vol based, reported as a low-high range),
  writes `history_premiums.json`.
- **`notify_email.py`** — headless run that emails all strategies (Gmail SMTP); market-hours guarded.
- **`history_premiums.json`** — the precomputed premium table (committed; refreshed weekly).
- **`.github/workflows/screener-email.yml`** — emails every 30 min during market hours (weekdays).
- **`.github/workflows/build-history.yml`** — rebuilds history_premiums.json weekly, commits it.
- **`requirements.txt`** — streamlit, streamlit-autorefresh, yfinance, scipy, pandas, curl_cffi, requests.

## GitHub Secrets (Settings -> Secrets and variables -> Actions)
- `TRADIER_TOKEN` — Tradier sandbox token
- `EMAIL_USER` — sending Gmail address
- `EMAIL_APP_PASSWORD` — 16-char Google App Password
- `EMAIL_TO` — recipient(s), comma-separated for multiple
Streamlit app Secrets need `TRADIER_TOKEN` (and optionally `TRADIER_BASE`).

## Key config (top of wheel_screener.py) — current values
- Puts/calls: POP >= 70%; DTE 7-90; annualized-yield floor 15% for stocks,
  **10% for broad indexes** (SPY/QQQ/DIA, which also skip the per-share premium floors);
  premium >= 1.5% of strike (stocks); OTM floor 10% (5% for SPY/QQQ/DIA);
  OTM >= 15% of IV (puts and covered calls); MIN_OPEN_INTEREST 1000.
- Score = (AnnYield / IV^SCORE_IV_EXP) x POP^SCORE_POP_EXP x (365/DTE)^SCORE_DTE_EXP
  (defaults 1.0 / 1.0 / 0.5); spreads use AnnROR in place of AnnYield. Ranks every table.
- CASH_TARGET = 40000 (drives "# of contracts" and the totals shown on Premium / Cash/Contract).
- Spreads: ~5% width, POP >= 70%, annualized ROR >= 25%, OTM >= 15% of IV per short leg, OI >= 1000.
- Earnings: contracts spanning an earnings date are excluded; manual dates in EARNINGS_DATES override.
- Watchlist (PUT_TICKERS) and holdings (HOLDINGS with cost bases) are lists at the top.

## Columns on the tables
Ticker, price, strike, expiration, DTE, OTM%, Premium ($/share + total across # of contracts),
AvgPremium (historical low-high band), yields, Delta/POP, IV, Score, Cash/Contract (per + total),
# of contracts (to reach CASH_TARGET), Spread_$ (bid-ask), OpenInt, Volume. Spreads also show
Max Profit / MaxLoss / ROR / AnnROR / Width.

## Recent open items / ideas
- After changing build_history.py to add call premiums, RE-RUN the "Build history table"
  Action so history_premiums.json has both put and call tables (calls/spreads AvgPremium need it).
- Optional: index OTM floor could be tightened below 5% to surface richer index puts (safety trade-off).
- Optional: extend AvgPremium to a paid historical-IV source for exactness (currently realized-vol estimate).

## Workflow reminders
- Always commit `wheel_screener.py`, `spreads.py`, `app.py` together when a change spans them
  (a "module has no attribute X" error = files out of sync).
- The .yml workflow files must contain YAML, not Python (a past mistake). They start with `name:`.
- GitHub scheduled runs can be delayed/dropped; the email workflow also has a manual "Run workflow".

Not financial advice — this is a personal research tool.
