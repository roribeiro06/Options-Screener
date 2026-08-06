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
- **`build_volume_leaders.py`** — offline job: finds tickers OUTSIDE `PUT_TICKERS`, from a broad
  ~7,000-ticker US-listed universe (not just the S&P 500), carrying the heaviest options OPEN
  INTEREST today (batched stock-volume quotes narrow the universe to 40 candidates, then a real
  options-chain lookup sums actual open interest and ranks the top 5), screens those 5 with
  `screen_puts`/`screen_calls` (calls evaluated hypothetically, no owned shares needed) using the
  same criteria as the main screener plus a higher OI floor (`DISCOVER_MIN_OI` = 5,000), and keeps
  only the single highest-OI qualifying put and call per ticker. Writes `volume_leaders.json`. This
  is how a ticker you never added (e.g. PG, or a recent IPO not yet in any index) can surface on its
  own if a contract is both liquid and qualifying. The universe comes from a community-maintained
  GitHub mirror of Nasdaq/NYSE's listed-securities directory (`UNIVERSE_URL`); if that's unreachable
  it falls back to the static `sp500_tickers.py` snapshot (Wikipedia; refresh manually if stale).
- **`notify_email.py`** — headless run that emails all strategies (Gmail SMTP); market-hours guarded.
- **`history_premiums.json`** — the precomputed premium table (committed; refreshed weekly).
- **`volume_leaders.json`** — today's high-volume discovery results (committed; refreshed daily after close).
- **`.github/workflows/screener-email.yml`** — emails every 30 min during market hours (weekdays).
- **`.github/workflows/build-history.yml`** — rebuilds history_premiums.json weekly, commits it.
- **`.github/workflows/build-volume-leaders.yml`** — rebuilds volume_leaders.json daily after the
  close (weekdays 20:15 UTC), commits it. Has a manual "Run workflow" button too.
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
AvgPremium (historical low-high band), yields, Delta/POP, IV, Score, MaxLoss ($/share + total,
same format as Premium: strike - premium for puts, cost basis - premium for covered calls with a
known cost basis else "-"), # of contracts (to reach CASH_TARGET), Spread_$ (bid-ask), OpenInt,
Volume. Spreads also show Max Profit / MaxLoss (width - credit, same $/share + total format) /
ROR / AnnROR / Width. No separate Cash/Contract column anymore -- MaxLoss covers that role.

## Recent open items / ideas
- After changing build_history.py to add call premiums, RE-RUN the "Build history table"
  Action so history_premiums.json has both put and call tables (calls/spreads AvgPremium need it).
- "Find high-volume tickers" Action needs at least one manual "Run workflow" click after first
  deploy (Actions tab) so `volume_leaders.json` exists before its daily 20:15 UTC schedule fires;
  the app shows a friendly message in the meantime instead of erroring.
- Discovery covers puts and calls (calls are hypothetical -- discovered tickers aren't real
  holdings). Could extend the same two-stage ranking to multi-leg spreads later.
- Discovery's candidate pool (`CANDIDATE_POOL` = 40) and final leader count (`TOP_N` = 5) are both
  narrow by design (cost/speed tradeoff); a ticker outside the top 40 by stock volume, or outside
  the top 5 of those by open interest, is never screened even if it would otherwise qualify.
- Optional: index OTM floor could be tightened below 5% to surface richer index puts (safety trade-off).
- Optional: extend AvgPremium to a paid historical-IV source for exactness (currently realized-vol estimate).

## Workflow reminders
- Always commit `wheel_screener.py`, `spreads.py`, `app.py` together when a change spans them
  (a "module has no attribute X" error = files out of sync).
- The .yml workflow files must contain YAML, not Python (a past mistake). They start with `name:`.
- GitHub scheduled runs can be delayed/dropped; the email workflow also has a manual "Run workflow".

Not financial advice — this is a personal research tool.
