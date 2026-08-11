"""
discover.py -- shared logic for finding tickers OUTSIDE your manual watchlist
(PUT_TICKERS in wheel_screener.py) carrying heavy options open interest right
now, not limited to the S&P 500, then screens them for cash-secured puts
(if the ticker is up today) or call credit spreads (if it's down -- defined
risk, since these are tickers you don't hold shares of, so a naked/covered
call would carry uncapped upside risk) using the same criteria as the main
screener.

Tradier has no "most active options" or batched-open-interest endpoint, so
this works in four cheap stages:
  1. One batched quote call per ~100 tickers across a broad US-listed universe
     (fetch_universe() below) pulls stock price, volume, average volume, and
     1-day % change for every name -- all in the same free batched call. From
     that, TWO candidate-selection mechanisms feed the same pipeline:
       - a SURGE pool: top CANDIDATE_POOL tickers by volume/average-volume,
         i.e. today's activity relative to the ticker's OWN normal, not raw
         share count -- otherwise the same handful of mega-caps (AAPL, TSLA,
         NVDA-scale raw volume) would crowd out every other name every single
         day regardless of whether anything unusual is actually happening.
         This is what surfaces names like large tech/consumer tickers that
         are simply seeing heavy options activity, whether or not they moved
         much today.
       - a MOVERS pool: top MOVER_POOL tickers by 1-day % move up, and
         separately by % move down.
     BOTH mechanisms require MIN_DOLLAR_VOLUME -- price x volume, not a raw
     share count. A cheap stock can clear a large SHARE count on trivial real
     trading activity (500,000 shares/day of a $2 stock is ~$1M, vs. $250M
     for a $500 stock doing the same share count) -- dollar volume is what
     actually measures liquidity, not share count alone. If Tradier doesn't
     return average_volume for a batch, the floor falls back to today's raw
     volume rather than being skipped -- a real (if less robust) liquidity
     check always applies, never none.
  2. Both mechanisms ALSO require a live market cap >= MIN_MARKET_CAP (via
     yfinance -- Tradier's quotes don't include it). This is a company SIZE
     filter, distinct from liquidity: a stock can be perfectly liquid (tight
     spreads, real volume) while still being a small, thinly-capitalized,
     more speculative name -- market cap catches that where a pure liquidity
     floor can't. Checked only against the already-ranked candidates (up to
     CANDIDATE_POOL + 2 x MOVER_POOL names), never the full ~7,000-ticker
     universe, since a per-ticker cap lookup isn't cheap enough to run against
     everything -- a candidate whose cap comes back unknown (lookup error,
     rate limit) is treated as failing the filter rather than getting the
     benefit of the doubt.
  3. Every surviving candidate gets one real options-chain lookup (nearest
     expiration in the DTE window) to sum actual OPEN INTEREST -- the real
     liquidity gate for the OPTIONS specifically, since stock volume is only
     ever a proxy for it. The surge pool's top CANDIDATE_POOL by that OI take
     TOP_N slots; each movers direction's top MOVER_POOL take TOP_N_MOVERS
     slots (a ticker already picked via one mechanism isn't OI-checked or
     listed again via the other).
  4. Routing is decided PURELY by each leader's OWN 1-day % change today --
     not by which mechanism found it. Up (>= 0%) gets screened for puts only
     (selling downside protection into strength, where a real move plus
     likely-elevated IV give more cushion for the premium); down (< 0%) gets
     screened for call credit spreads only (capping upside into a name that
     just sold off, where IV is often still elevated). This is what keeps a
     ticker from ever appearing in both output tables at once, even though
     the surge pool itself isn't a directional signal -- every candidate,
     surge- or mover-sourced, still only ever has one real direction today.
     Every candidate still needs real contract-level open interest
     (MIN_OPEN_INTEREST, overridden to DISCOVER_MIN_OI for this section) to
     actually qualify in this stage; a big price move never bypasses that
     check.
A ticker with high total OI but modest volume today, below MIN_MARKET_CAP, or
outside the top pools above can still be missed -- this is the tradeoff for
not running an options-chain call (or a market-cap lookup) against every
ticker in the universe.

Called live from app.py (cached, ttl=600 -- refreshes on the same 30-min-auto/
on-demand cadence as the rest of the screener) and from build_volume_leaders.py
(an offline CLI wrapper that writes volume_leaders.json as a fallback snapshot
app.py falls back to if the live scan ever errors).
"""
import sys
import datetime as dt

import wheel_screener as ws
import spreads as sp
from sp500_tickers import SP500_TICKERS

# Mirror of Nasdaq/NYSE/etc.'s combined listed-securities directory (~7,000
# tickers) -- broader than the S&P 500 so recent IPOs / not-yet-indexed names
# (e.g. SPCX, SOFI) can surface. Community-maintained; if it's unreachable or
# looks truncated, fall back to the static S&P 500 snapshot rather than fail.
UNIVERSE_URL = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/all/all_tickers.txt"

CANDIDATE_POOL = 40   # top-by-volume-SURGE names that get a real options-chain check
TOP_N = 5             # how many of those (by actual open interest) get deep-scanned
MOVER_POOL = 10       # top up-movers / down-movers (by 1D %) that get an options-chain check, per side
TOP_N_MOVERS = 3      # how many of those (by actual open interest) get deep-scanned, per side
MIN_DOLLAR_VOLUME = 25_000_000  # liquidity floor: price x avg daily volume (or today's volume
                                # if avg is unavailable), not a raw share count -- see module
                                # docstring for why a share-count floor lets cheap stocks through
MIN_MARKET_CAP = 10_000_000_000  # $10B -- company-size filter, separate from liquidity (see
                                 # module docstring); via yfinance, checked only against the
                                 # already-ranked candidates, never the full universe
CHUNK = 100           # tickers per batched /markets/quotes call
DISCOVER_MIN_OI = 5000  # OI floor for this section only (higher than the main
                        # screener's MIN_OPEN_INTEREST) -- these are unfamiliar
                        # tickers, so lean toward their most liquid contracts.


def fetch_universe():
    """Broad US-listed ticker universe. Falls back to the S&P 500 snapshot
    (sp500_tickers.py) if the remote list is unreachable or looks wrong."""
    import requests
    try:
        r = requests.get(UNIVERSE_URL, timeout=20)
        r.raise_for_status()
        tickers = sorted({line.strip() for line in r.text.splitlines() if line.strip()})
        if len(tickers) < 1000:
            raise ValueError(f"only {len(tickers)} tickers, looks truncated")
        print(f"Universe: {len(tickers)} tickers from {UNIVERSE_URL}")
        return tickers
    except Exception as e:
        print(f"universe fetch ERROR: {e} -- falling back to S&P 500", file=sys.stderr)
        return SP500_TICKERS


def batched_stock_stats(tickers):
    """One /markets/quotes call per CHUNK tickers -> {symbol: {"price", "volume",
    "avg_volume", "change_pct"}}. change_pct is a fraction (0.062 = +6.2%),
    matching the rest of the codebase's convention (e.g. POP_MIN = 0.70)."""
    out = {}
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        try:
            j = ws._td_get("/markets/quotes", {"symbols": ",".join(chunk)})
        except Exception as e:
            print(f"quotes batch {i}: ERROR {e}", file=sys.stderr)
            continue
        for q in ws._as_list((j.get("quotes") or {}).get("quote")):
            sym = q.get("symbol")
            if sym:
                out[sym] = {"price": float(q.get("last") or q.get("close") or q.get("prevclose") or 0),
                            "volume": int(q.get("volume") or 0),
                            "avg_volume": int(q.get("average_volume") or 0),
                            "change_pct": float(q.get("change_percentage") or 0) / 100.0}
    return out


def market_cap(symbol):
    """Live market cap via yfinance (Tradier's quotes don't include it). None
    if unavailable -- callers treat that as failing the size filter, not as a
    pass, since an unknown cap shouldn't get the benefit of the doubt."""
    try:
        mc = ws._ticker(symbol).fast_info.market_cap
        return float(mc) if mc else None
    except Exception as e:
        print(f"{symbol}: market cap ERROR {e}", file=sys.stderr)
        return None


def option_open_interest(symbol, today):
    """Sum open interest (puts+calls) across the nearest expiration inside the
    screener's DTE window. Returns 0 if the ticker has no expirations in range."""
    windowed = ws._expirations_in_window(symbol, today)
    if not windowed:
        return 0
    exp, _, _ = min(windowed, key=lambda w: w[2])   # nearest DTE
    try:
        chain = ws.td_chain(symbol, exp)
    except Exception as e:
        print(f"{symbol}: chain ERROR {e}", file=sys.stderr)
        return 0
    return sum(int(o.get("oi") or 0) for o in chain)


def _have_avg_volume(stats):
    """True if Tradier actually returned usable average_volume for most of this
    batch. If it didn't (field missing/renamed upstream, etc.), the liquidity
    floor falls back to raw volume instead of silently going empty."""
    if not stats:
        return False
    with_avg = sum(1 for s in stats.values() if s["avg_volume"] > 0)
    return with_avg >= len(stats) * 0.5


def _dollar_volume(s, have_avg_vol):
    """price x avg daily volume (or today's volume if avg is unavailable) --
    the actual liquidity measure, not a raw share count (see module docstring
    for why that distinction matters)."""
    vol = s["avg_volume"] if have_avg_vol and s["avg_volume"] > 0 else s["volume"]
    return vol * s["price"]


def _rank_surge(stats, universe, have_avg_vol):
    """Top CANDIDATE_POOL tickers by today's volume relative to their OWN
    average volume -- otherwise the same mega-caps win every day regardless
    of anything unusual happening. Falls back to raw volume (the old
    behavior) if average_volume isn't available this run. Also requires
    MIN_DOLLAR_VOLUME regardless -- a high ratio alone isn't liquidity; a thin
    stock trading a rare 8x its own tiny average is still a thin stock."""
    if have_avg_vol:
        scored = [(sym, stats[sym]["volume"] / stats[sym]["avg_volume"])
                  for sym in universe if sym in stats and stats[sym]["avg_volume"] > 0
                  and _dollar_volume(stats[sym], have_avg_vol) >= MIN_DOLLAR_VOLUME]
    else:
        print("average_volume unavailable this run -- surge pool falling back to raw volume",
              file=sys.stderr)
        scored = [(sym, stats[sym]["volume"]) for sym in universe
                  if sym in stats and _dollar_volume(stats[sym], have_avg_vol) >= MIN_DOLLAR_VOLUME]
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return [sym for sym, _ in scored[:CANDIDATE_POOL]]


def _rank_movers(stats, universe, direction, have_avg_vol):
    """Top MOVER_POOL tickers by 1-day % move in `direction` ("up" or "down"),
    restricted to names clearing MIN_DOLLAR_VOLUME -- a thin name spiking on
    no real (dollar) volume never gets in, move size alone is never enough."""
    scored = []
    for sym in universe:
        s = stats.get(sym)
        if not s or _dollar_volume(s, have_avg_vol) < MIN_DOLLAR_VOLUME:
            continue
        pct = s["change_pct"]
        if direction == "up" and pct > 0:
            scored.append((sym, pct))
        elif direction == "down" and pct < 0:
            scored.append((sym, -pct))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return [sym for sym, _ in scored[:MOVER_POOL]]


def run_discovery():
    """Runs the full scan and returns the same dict shape that used to be
    (and, via build_volume_leaders.py, still can be) written to
    volume_leaders.json."""
    today = dt.date.today()
    watchlist = set(ws.PUT_TICKERS)
    universe = [t for t in fetch_universe() if t not in watchlist]

    print(f"Stage 1: batched quotes for {len(universe)} tickers...")
    stats = batched_stock_stats(universe)
    have_avg_vol = _have_avg_volume(stats)

    surge_candidates = _rank_surge(stats, universe, have_avg_vol)
    up_candidates = _rank_movers(stats, universe, "up", have_avg_vol)
    down_candidates = _rank_movers(stats, universe, "down", have_avg_vol)
    print(f"Stage 1 done: {len(surge_candidates)} by volume surge, "
          f"{len(up_candidates)} up-movers, {len(down_candidates)} down-movers.")

    print(f"Stage 1b: market-cap filter (>= ${MIN_MARKET_CAP:,.0f}) for "
          f"{len(surge_candidates) + len(up_candidates) + len(down_candidates)} candidates...")
    surge_candidates = [s for s in surge_candidates if (market_cap(s) or 0) >= MIN_MARKET_CAP]
    up_candidates = [s for s in up_candidates if (market_cap(s) or 0) >= MIN_MARKET_CAP]
    down_candidates = [s for s in down_candidates if (market_cap(s) or 0) >= MIN_MARKET_CAP]
    print(f"Stage 1b done: {len(surge_candidates)} surge, {len(up_candidates)} up-movers, "
          f"{len(down_candidates)} down-movers survive the market-cap filter.")

    # Tracks WHICH mechanism(s) surfaced a ticker -- display-only (the leaders
    # caption), it does NOT affect routing (see run_discovery's docstring).
    source = {}
    for sym in surge_candidates:
        source[sym] = "surge"
    for sym in up_candidates + down_candidates:
        source.setdefault(sym, "mover")

    print("Stage 2: options-chain open interest for candidates...")
    opt_oi = {}
    for sym in source:
        try:
            opt_oi[sym] = option_open_interest(sym, today)
        except Exception as e:
            print(f"{sym}: ERROR {e}", file=sys.stderr)
            opt_oi[sym] = 0

    def _top(symbols, n, exclude=frozenset()):
        ranked = sorted(((s, opt_oi.get(s, 0)) for s in symbols if s not in exclude),
                        key=lambda kv: kv[1], reverse=True)
        return ranked[:n]

    surge_leaders = _top(surge_candidates, TOP_N)
    covered = {s for s, _ in surge_leaders}
    up_leaders = _top(up_candidates, TOP_N_MOVERS, exclude=covered)
    covered |= {s for s, _ in up_leaders}
    down_leaders = _top(down_candidates, TOP_N_MOVERS, exclude=covered)
    leaders = surge_leaders + up_leaders + down_leaders
    print("Top by open interest:", leaders)

    print("Stage 3: screening leaders for qualifying puts and call credit spreads...")
    put_rows, spread_rows, leader_meta = [], [], []
    # Temporary override, always restored -- this runs inside the long-lived
    # Streamlit process too, and a leftover MIN_OPEN_INTEREST would corrupt the
    # Puts/Calls sections' own screening on the next script rerun.
    _orig_min_oi = ws.MIN_OPEN_INTEREST
    ws.MIN_OPEN_INTEREST = DISCOVER_MIN_OI
    try:
        for sym, ooi in leaders:
            pct = stats.get(sym, {}).get("change_pct", 0.0)
            # Routing is by the ticker's OWN actual move today, regardless of
            # which mechanism (surge or mover) surfaced it as a candidate --
            # this is what guarantees a ticker never appears in both output
            # tables at once, even with the (non-directional) surge pool back.
            direction = "up" if pct >= 0 else "down"
            leader_meta.append({"ticker": sym, "stock_volume": stats.get(sym, {}).get("volume", 0),
                                "option_open_interest": ooi, "reason": direction,
                                "source": source.get(sym, "mover"), "change_pct": pct})
            try:
                put_passers, call_spreads = [], []
                if direction == "up":
                    put_passers, _ = ws.screen_puts(sym)
                else:
                    call_spreads = [r for r in sp.screen_spreads(sym) if r["Strategy"] == "Call credit spread"]
                # Only the single highest-open-interest qualifying put, and the single
                # highest-open-interest qualifying call credit spread, per ticker --
                # keeps each table to one row per ticker instead of every contract
                # that passes.
                if put_passers:
                    best_put = max(put_passers, key=lambda r: r.get("OpenInt") or 0)
                    put_rows.append(best_put)
                if call_spreads:
                    best_spread = max(call_spreads, key=lambda r: r.get("OpenInt") or 0)
                    spread_rows.append(best_spread)
                print(f"{sym} [{direction}/{source.get(sym, 'mover')}]: {len(put_passers)} puts, "
                      f"{len(call_spreads)} call credit spreads qualifying (showing top-OI each)")
            except Exception as e:
                print(f"{sym}: screen ERROR {e}", file=sys.stderr)
    finally:
        ws.MIN_OPEN_INTEREST = _orig_min_oi

    return {"_meta": {"built": today.isoformat(),
                      "candidate_pool": CANDIDATE_POOL, "top_n": TOP_N,
                      "mover_pool": MOVER_POOL, "top_n_movers": TOP_N_MOVERS,
                      "min_dollar_volume": MIN_DOLLAR_VOLUME, "min_market_cap": MIN_MARKET_CAP},
            "leaders": leader_meta,
            "puts": put_rows,
            "call_spreads": spread_rows}
