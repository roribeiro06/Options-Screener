"""
discover.py -- shared logic for finding tickers OUTSIDE your manual watchlist
(PUT_TICKERS in wheel_screener.py) that moved sharply today, not limited to
the S&P 500, then screens them for cash-secured puts (up-movers) or call
credit spreads (down-movers -- defined-risk, since these are tickers you
don't hold shares of, so a naked/covered call would carry uncapped upside
risk) using the same criteria as the main screener.

Tradier has no "most active options" or batched-open-interest endpoint, so
this works in four cheap stages:
  1. One batched quote call per ~100 tickers across a broad US-listed universe
     (fetch_universe() below) pulls stock price, volume, average volume, and
     1-day % change for every name -- all in the same free batched call. From
     that: a MOVERS pool -- top MOVER_POOL tickers by 1-day % move up, and
     separately by % move down. Both directions require MIN_DOLLAR_VOLUME --
     price x volume, not a raw share count. A cheap stock can clear a large
     SHARE count on trivial real trading activity (500,000 shares/day of a $2
     stock is ~$1M, vs. $250M for a $500 stock doing the same share count) --
     dollar volume is what actually measures liquidity, not share count
     alone. If Tradier doesn't return average_volume for a batch, the floor
     falls back to today's raw volume rather than being skipped -- a real (if
     less robust) liquidity check always applies, never none.
  2. Both directions ALSO require a live market cap >= MIN_MARKET_CAP (via
     yfinance -- Tradier's quotes don't include it). This is a company SIZE
     filter, distinct from liquidity: a stock can be perfectly liquid (tight
     spreads, real volume) while still being a small, thinly-capitalized,
     more speculative name -- market cap catches that where a pure liquidity
     floor can't. Checked only against the already-ranked candidates (up to
     2 x MOVER_POOL names), never the full ~7,000-ticker universe, since a
     per-ticker cap lookup isn't cheap enough to run against everything -- a
     candidate whose cap comes back unknown (lookup error, rate limit) is
     treated as failing the filter rather than getting the benefit of the
     doubt.
  3. Every candidate that survives both filters gets one real options-chain
     lookup (nearest expiration in the DTE window) to sum actual OPEN
     INTEREST -- the real liquidity gate for the OPTIONS specifically, since
     stock volume is only ever a proxy for it.
  4. Each direction's top TOP_N_MOVERS (by that option OI) get screened:
     up-movers for puts only (selling downside protection into strength,
     where the move plus likely-elevated IV give more cushion for the
     premium), down-movers for call credit spreads only (capping upside into
     a name that just sold off, where IV is often still elevated). A ticker
     can't be both an up- and a down-mover on the same day, so there's no
     double-counting to worry about between the two. Every candidate still
     needs real contract-level open interest (MIN_OPEN_INTEREST, overridden
     to DISCOVER_MIN_OI for this section) to actually qualify in this stage;
     a big price move never bypasses that check.
A ticker with high total OI but a modest move today, below MIN_MARKET_CAP, or
outside the top MOVER_POOL per direction can still be missed -- this is the
tradeoff for not running an options-chain call (or a market-cap lookup)
against every ticker in the universe.

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
    """Runs the full mover-based scan and returns the same dict shape that
    used to be (and, via build_volume_leaders.py, still can be) written to
    volume_leaders.json."""
    today = dt.date.today()
    watchlist = set(ws.PUT_TICKERS)
    universe = [t for t in fetch_universe() if t not in watchlist]

    print(f"Stage 1: batched quotes for {len(universe)} tickers...")
    stats = batched_stock_stats(universe)
    have_avg_vol = _have_avg_volume(stats)

    up_candidates = _rank_movers(stats, universe, "up", have_avg_vol)
    down_candidates = _rank_movers(stats, universe, "down", have_avg_vol)
    print(f"Stage 1 done: {len(up_candidates)} up-movers, {len(down_candidates)} down-movers.")

    print(f"Stage 1b: market-cap filter (>= ${MIN_MARKET_CAP:,.0f}) for "
          f"{len(up_candidates) + len(down_candidates)} candidates...")
    up_candidates = [s for s in up_candidates if (market_cap(s) or 0) >= MIN_MARKET_CAP]
    down_candidates = [s for s in down_candidates if (market_cap(s) or 0) >= MIN_MARKET_CAP]
    print(f"Stage 1b done: {len(up_candidates)} up-movers, "
          f"{len(down_candidates)} down-movers survive the market-cap filter.")

    # A ticker can't move both up and down on the same day, so up_candidates
    # and down_candidates never overlap -- no dedup needed between them.
    tag = {sym: "up" for sym in up_candidates}
    tag.update({sym: "down" for sym in down_candidates})

    print("Stage 2: options-chain open interest for candidates...")
    opt_oi = {}
    for sym in tag:
        try:
            opt_oi[sym] = option_open_interest(sym, today)
        except Exception as e:
            print(f"{sym}: ERROR {e}", file=sys.stderr)
            opt_oi[sym] = 0

    def _top(symbols, n):
        ranked = sorted(((s, opt_oi.get(s, 0)) for s in symbols), key=lambda kv: kv[1], reverse=True)
        return ranked[:n]

    leaders = _top(up_candidates, TOP_N_MOVERS) + _top(down_candidates, TOP_N_MOVERS)
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
            reason = tag[sym]
            leader_meta.append({"ticker": sym, "stock_volume": stats.get(sym, {}).get("volume", 0),
                                "option_open_interest": ooi, "reason": reason,
                                "change_pct": stats.get(sym, {}).get("change_pct", 0.0)})
            try:
                put_passers, call_spreads = [], []
                if reason == "up":
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
                print(f"{sym} [{reason}]: {len(put_passers)} puts, {len(call_spreads)} call credit "
                      f"spreads qualifying (showing top-OI each)")
            except Exception as e:
                print(f"{sym}: screen ERROR {e}", file=sys.stderr)
    finally:
        ws.MIN_OPEN_INTEREST = _orig_min_oi

    return {"_meta": {"built": today.isoformat(),
                      "mover_pool": MOVER_POOL, "top_n_movers": TOP_N_MOVERS,
                      "min_dollar_volume": MIN_DOLLAR_VOLUME, "min_market_cap": MIN_MARKET_CAP},
            "leaders": leader_meta,
            "puts": put_rows,
            "call_spreads": spread_rows}
