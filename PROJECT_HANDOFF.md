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
  (sell-side, any ticker), Multi-Leg Strategies (put/call credit spreads, iron condors), Discover
  (placed last -- see below), Legend. Loads TRADIER_TOKEN from st.secrets; 30-min market-clock
  auto-refresh; editable criteria panels.
- **`wheel_screener.py`** — the engine. Tradier data funcs, Black-Scholes, `evaluate_put`/`evaluate_call`,
  `screen_puts`/`screen_calls`, `lookup_contracts`, the Score, liquidity, cash/target columns,
  historical AvgPremium lookup, all config constants at the top.
- **`spreads.py`** — multi-leg engine (credit spreads + iron condors). Reuses wheel_screener.
- **`discover.py`** — shared logic (`run_discovery()`) for finding tickers OUTSIDE `PUT_TICKERS`, from
  a broad ~7,000-ticker US-listed universe (not just the S&P 500), via one batched-quotes pass (price,
  volume, average volume, 1-day % change -- all free in that same call) feeding TWO candidate-selection
  mechanisms: a **surge pool** (top `CANDIDATE_POOL` = 40 tickers by volume/average-volume, today's
  activity relative to the ticker's OWN normal -- otherwise the same mega-caps crowd out everything
  every day; this is what surfaces names that are simply seeing heavy options activity, moved or not)
  and a **movers pool** (top `MOVER_POOL` = 10 tickers by 1-day % move, up and down separately). Both
  mechanisms require `MIN_DOLLAR_VOLUME` = \$25M/day (price x avg volume, or price x today's volume if
  Tradier didn't return an average) -- a liquidity floor on the STOCK itself, so a thin name can't get
  in on a high ratio or a big % move alone; a cheap stock can clear a raw SHARE-count floor on trivial
  real dollar activity, which is why this is priced, not a share count. Both ALSO require a live market
  cap >= `MIN_MARKET_CAP` = \$10B, via yfinance (Tradier's quotes don't include it) -- checked only
  against the already-ranked candidates (up to `CANDIDATE_POOL` + 2 x `MOVER_POOL` names), never the
  full universe, since a per-ticker cap lookup isn't cheap enough to run against ~7,000 tickers. This
  catches thinly-capitalized/speculative names that a pure liquidity floor can't -- a stock can be
  perfectly liquid while still being small (e.g. HTZ, ~\$900M market cap, was surfacing before this
  existed despite clearing the old volume floor easily on its low share price). Every surviving
  candidate then gets one real options-chain lookup to sum actual open interest, ranking the surge
  pool's top `TOP_N` = 5 and each movers direction's top `TOP_N_MOVERS` = 3 (a ticker already picked
  via one mechanism isn't OI-checked or listed again via the other). **Routing is decided purely by
  each leader's OWN 1-day % change today, not by which mechanism found it**: up (>= 0%) gets screened
  for puts only (selling downside protection into strength, where a real move plus likely-elevated IV
  give more cushion); down (< 0%) gets screened for call credit spreads only (capping upside into a
  name that just sold off, IV often still elevated) -- via `screen_puts`/`spreads.screen_spreads` (calls
  are call credit spreads -- defined risk, since these aren't real holdings), same criteria as the main
  screener plus a higher OI floor (`DISCOVER_MIN_OI` = 5,000). This direction-based routing is what
  keeps a ticker from ever appearing in both output tables at once, even though the surge pool itself
  isn't a directional signal -- every candidate, surge- or mover-sourced, still only has one real
  direction today. Keeps only the single highest-OI qualifying put and call spread per ticker. Each
  leader's `source` field (`"surge"` or `"mover"`) records which mechanism found it, display-only, no
  effect on routing. This is how a ticker you never added (e.g. a large name simply seeing heavy
  options activity, or one that just moved hard on real volume) can surface on its own.
  The universe comes from a community-maintained GitHub mirror of Nasdaq/NYSE's listed-securities
  directory (`UNIVERSE_URL`); if that's unreachable it falls back to the static `sp500_tickers.py`
  snapshot (Wikipedia; refresh manually if stale). `app.py` calls `discover.run_discovery()` **live**,
  cached at `ttl=600` like every other scan (30-min auto-refresh / "Refresh data" on demand) -- it's
  placed last on the page since it scans ~7,000 tickers vs. ~20 for the sections above, so it's much
  slower and shouldn't block the rest of the page from rendering first. Previously measured at ~170s
  (under 3 min) with a 40-candidate pool; the movers pools add up to ~20 more options-chain lookups in
  Stage 2, so expect noticeably more than that now -- not re-measured yet, worth timing after the next
  live run. Because `st.cache_data` is shared across all users/sessions (not per-visitor), only the
  first page load after the 10-min cache expires pays that cost -- everyone else in that window gets
  the cached result instantly. `ws.MIN_OPEN_INTEREST` is temporarily overridden during the scan and
  restored in a `finally` block; this matters because the code now runs inside the long-lived
  Streamlit process, not a one-shot script -- a leftover override would corrupt the Puts/Calls
  sections' own screening on the next script rerun.
- **`build_history.py`** — offline job: pulls ~1yr of Tradier stock prices, models typical put/call
  **annualized yield** (not dollar premium) per ticker by OTM%/DTE bucket, reported as a low-high
  range (realized-vol basis, 25th-75th percentile, high end nudged up by `IV_UPLIFT` toward implied
  vol), writes `history_premiums.json`. Yield, not dollars, because a Black-Scholes premium as a
  fraction of strike (puts) or spot (calls) is scale-invariant -- it depends only on OTM%/time/vol,
  never the stock's price level -- so it isn't distorted by the stock having trended over the
  lookback year the way raw dollar premiums would be, and it's directly comparable to the live
  AnnYield_%/AnnROR_% columns. `_meta.metric` in the JSON marks this format (`"ann_yield"`); the
  screener treats an older $-format file (or one missing that marker) as unavailable rather than
  misreading it. Also conditions each OTM/DTE band on the realized-vol regime that was in effect
  (`IV_BUCKETS`), stored as extra `"otm|dte|volbucket"` keys alongside the plain `"otm|dte"` one --
  a live lookup with an IV to condition on (`avg_premium_range(..., iv=...)`) gets "typical yield
  given today's vol", not averaged across every vol regime the ticker saw all year, PROVIDED that
  vol bucket had enough historical days (`MIN_VOL_BUCKET_SAMPLES` = 20) to trust the percentile;
  otherwise it falls back to the unconditional band automatically. Purely additive -- a file built
  before this existed just lacks the vol-conditioned keys and every lookup falls back cleanly.
- **`build_volume_leaders.py`** — thin CLI wrapper around `discover.run_discovery()`, writing
  `volume_leaders.json`. No longer read by the live app in the normal path (that now scans live) --
  kept only as a **fallback snapshot**: if the live scan errors inside the app, it falls back to
  whatever this last wrote.
- **`notify_email.py`** — headless run that emails all strategies (Gmail SMTP); market-hours guarded.
- **`history_premiums.json`** — the precomputed annualized-yield table (committed; refreshed weekly).
- **`volume_leaders.json`** — fallback-only snapshot for Discover (committed; refreshed daily after
  close by the Action below). Only used if the live in-app scan fails.
- **`.github/workflows/screener-email.yml`** — emails every 30 min during market hours (weekdays).
- **`.github/workflows/build-history.yml`** — rebuilds history_premiums.json weekly, commits it.
- **`.github/workflows/build-volume-leaders.yml`** — rebuilds the fallback `volume_leaders.json`
  daily after the close (weekdays 20:15 UTC), commits it. Has a manual "Run workflow" button too.
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
- CASH_TARGET = 40000 (drives "# of contracts" and the totals shown on Premium / MaxLoss for puts/calls).
- Spreads: ~5% width, POP >= 70%, annualized ROR >= 25%, OTM >= 15% of IV per short leg, OI >= 1000.
  SPREAD_CASH_TARGET = 25000 (lower than CASH_TARGET since a spread's risk per contract is capped).
- Earnings: contracts spanning an earnings date are excluded; manual dates in EARNINGS_DATES override.
- Watchlist (PUT_TICKERS) and holdings (HOLDINGS with cost bases) are lists at the top.

## Columns on the tables
Ticker, price, strike, expiration, DTE, OTM%, Premium (shown as a $worst-$best bid-ask range,
i.e. sell-at-bid/buy-at-ask vs sell-at-ask/buy-at-bid, with the total across # of contracts in
parentheses for each end), AvgPremium (historical ANNUALIZED YIELD as a low%-high% band, directly
comparable to AnnYield_%/AnnROR_% -- NOT a dollar amount, see `build_history.py`), yields, Delta/POP,
IV, Score, MaxLoss ($/share + total, same format: strike - premium for puts, cost basis - premium for
covered calls with a known cost basis else "-", width - credit for spreads), # of contracts (to reach
CASH_TARGET for puts/calls, SPREAD_CASH_TARGET for spreads), OpenInt, Volume. Spreads show Max
Profit the same way as Premium (as a $worst-$best range) plus ROR / AnnROR / Width. No separate
Cash/Contract column anymore (MaxLoss covers that role) and no separate Spread_$ liquidity column
(the bid-ask range on Premium/Max Profit conveys the same info).

## Recent open items / ideas
- AvgPremium's format changed from $ to annualized yield % (see `build_history.py`) -- the "Build
  history table" Action has already been re-run once for this, so `history_premiums.json` is in
  the new format. It just picked up a SECOND enhancement (IV-conditioned buckets, same file) which
  is purely additive -- no rebuild is strictly required (falls back to the unconditional band
  automatically), but re-running the Action again gets the vol-conditioned bands populated so
  AvgPremium reflects the ticker's current vol regime instead of its whole-year average.
- Discover now scans live inside the app (see `discover.py` above) instead of only reading a daily
  snapshot -- it's much slower than the other sections (a full ~7,000-ticker scan; see timing note
  in `discover.py`'s section above) since it runs the same broad scan the old offline Action did,
  just inside the cached Streamlit call instead of a nightly job. The "Find high-volume tickers"
  Action still runs daily and keeps `volume_leaders.json` fresh purely as a fallback in case the
  live scan errors (Tradier hiccup, universe source down, etc.) -- no manual run needed for the app
  to work, but running it once after a fresh clone gives a fallback ready from day one.
- Discovery covers puts and call credit spreads (calls are credit spreads, not naked/covered calls,
  since discovered tickers aren't real holdings). Could extend the same up/down-mover routing to put
  credit spreads / iron condors later (currently only single-leg puts and call spreads are routed by
  mover direction).
- Discovery's pools are narrow by design (cost/speed/liquidity tradeoff): `CANDIDATE_POOL` = 40 surge
  names, `MOVER_POOL` = 10 per direction, `TOP_N` = 5 and `TOP_N_MOVERS` = 3 by open interest,
  `MIN_DOLLAR_VOLUME` = \$25M/day and `MIN_MARKET_CAP` = \$10B as the liquidity/size floors on both
  mechanisms. A ticker outside these is never screened even if it would otherwise qualify. If it
  doesn't surface much (markets have quiet days), consider loosening `MIN_DOLLAR_VOLUME`/
  `MIN_MARKET_CAP` or widening `CANDIDATE_POOL`/`MOVER_POOL` before removing either floor entirely --
  they're there specifically to keep illiquid or thinly-capitalized names out (see `discover.py`'s
  section above for the HTZ example that prompted adding `MIN_MARKET_CAP`). The surge pool was briefly
  removed in favor of a purely mover-based design (so every result was explained by an actual
  directional move), then brought back after user feedback wanting the previously-familiar
  high-volume names (e.g. large names simply seeing heavy options activity) back in the mix --
  reconciled by routing EVERY candidate (surge or mover alike) by its own actual 1-day % change, so a
  ticker still never shows up in both output tables even with the non-directional surge pool active.
- The market-cap filter costs one yfinance call per surviving candidate (up to `CANDIDATE_POOL` + 2 x
  `MOVER_POOL` = 60 in the worst case), adding real time to an already-slow scan on top of the
  options-chain lookups -- worth re-timing a live run and updating the timing note above if it's grown
  enough to matter.
- Optional: index OTM floor could be tightened below 5% to surface richer index puts (safety trade-off).
- Optional: extend AvgPremium to a paid historical-IV source for exactness (currently realized-vol estimate).

## Workflow reminders
- Always commit `wheel_screener.py`, `spreads.py`, `app.py` together when a change spans them
  (a "module has no attribute X" error = files out of sync).
- The .yml workflow files must contain YAML, not Python (a past mistake). They start with `name:`.
- GitHub scheduled runs can be delayed/dropped; the email workflow also has a manual "Run workflow".

Not financial advice — this is a personal research tool.
