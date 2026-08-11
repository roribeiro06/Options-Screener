"""
discover.py -- shared logic for finding tickers OUTSIDE your manual watchlist
(PUT_TICKERS in wheel_screener.py) carrying heavy options open interest right
now -- not limited to the S&P 500, so a large but not-yet-indexed name (a
recent IPO, for example) can still surface -- then screens them for
cash-secured puts AND call credit spreads (defined-risk -- these are tickers
you don't hold shares of, so a naked/covered call would carry uncapped upside
risk) using the same criteria as the main screener.

Tradier has no "most active options" or batched-open-interest endpoint, so
this works in three cheap stages:
  1. One batched quote call per ~100 tickers across a broad US-listed universe
     (fetch_universe() below) pulls stock volume, average volume, and 1-day
     % change for every name -- all in the same free batched call. From that:
       - a SURGE pool: top CANDIDATE_POOL tickers by volume/average-volume,
         i.e. today's activity relative to the ticker's OWN normal, not raw
         share count -- otherwise the same handful of mega-caps (AAPL, TSLA,
         NVDA-scale raw volume) would crowd out every other name every single
         day regardless of whether anything unusual is actually happening.
       - a MOVERS pool: top MOVER_POOL tickers by 1-day % move up, and
         separately by % move down.
     BOTH pools require MIN_AVG_VOLUME -- a liquidity floor on the STOCK
     itself, not just a ranking. A high surge RATIO on a thin stock is still
     a thin stock (a name that rarely trades doing 8x its own tiny average is
     not liquid just because the ratio is big); a big % move on a thin name
     is usually noise, not signal. A big move or big ratio on a genuinely
     liquid name is a real volatility signal (richer option premiums) -- the
     floor is what tells those two cases apart. If Tradier doesn't return
     average_volume for a batch, the floor falls back to raw daily volume
     rather than being skipped -- a real (if less robust) liquidity check
     always applies, never none.
  2. Every candidate from both pools gets one real options-chain lookup
     (nearest expiration in the DTE window) to sum actual OPEN INTEREST --
     the real liquidity gate, since stock volume is only ever a cheap proxy
     for it.
  3. The SURGE pool's top TOP_N (by option OI) get screened for BOTH puts and
     call credit spreads, same as before. The MOVERS pools each contribute up
     to TOP_N_MOVERS more (also ranked by option OI): up-movers are screened
     for puts only (selling downside protection into strength, where the
     move plus likely-elevated IV give more cushion for the premium),
     down-movers are screened for call credit spreads only (capping upside
     into a name that just sold off, where IV is often still elevated). A
     ticker already covered by the surge pool is not screened or listed
     twice. Every candidate -- surge or mover alike -- still needs real
     contract-level open interest (MIN_OPEN_INTEREST, overridden to
     DISCOVER_MIN_OI for this section) to actually qualify in this stage; a
     big price move never bypasses that check.
A ticker with high total OI but modest volume today (or outside the top pools
above) can still be missed -- this is the tradeoff for not running an
options-chain call against every ticker in the universe.

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
MIN_AVG_VOLUME = 500_000  # liquidity floor (avg daily shares, or today's volume if avg is
                          # unavailable) for a ticker to qualify as a MOVER candidate at all
CHUNK = 100           # tickers per batched /markets/quotes call
DISCOVER_MIN_OI = 5000  # OI floor for this section only (higher than the main
                        # screener's MIN_OPEN_INTEREST) -- these are unfamiliar
                        # tickers, so lean toward their most liquid contracts.
                        # Applies identically to surge and mover candidates alike.


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
    """One /markets/quotes call per CHUNK tickers -> {symbol: {"volume",
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
                out[sym] = {"volume": int(q.get("volume") or 0),
                            "avg_volume": int(q.get("average_volume") or 0),
                            "change_pct": float(q.get("change_percentage") or 0) / 100.0}
    return out


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
    batch. If it didn't (field missing/renamed upstream, etc.), surge ranking
    and the mover liquidity floor both fall back to raw volume instead of
    silently going empty."""
    if not stats:
        return False
    with_avg = sum(1 for s in stats.values() if s["avg_volume"] > 0)
    return with_avg >= len(stats) * 0.5


def _rank_surge(stats, universe, have_avg_vol):
    """Top CANDIDATE_POOL tickers by today's volume relative to their OWN
    average volume -- otherwise the same mega-caps win every day regardless
    of anything unusual happening. Falls back to raw volume (the old
    behavior) if average_volume isn't available this run. Also requires
    MIN_AVG_VOLUME regardless -- a high ratio alone isn't liquidity; a thin
    stock trading a rare 8x its own tiny average is still a thin stock."""
    if have_avg_vol:
        scored = [(sym, stats[sym]["volume"] / stats[sym]["avg_volume"])
                  for sym in universe if sym in stats and stats[sym]["avg_volume"] >= MIN_AVG_VOLUME]
    else:
        print("average_volume unavailable this run -- surge pool falling back to raw volume",
              file=sys.stderr)
        scored = [(sym, stats[sym]["volume"]) for sym in universe
                  if sym in stats and stats[sym]["volume"] >= MIN_AVG_VOLUME]
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return [sym for sym, _ in scored[:CANDIDATE_POOL]]


def _rank_movers(stats, universe, direction, have_avg_vol):
    """Top MOVER_POOL tickers by 1-day % move in `direction` ("up" or "down"),
    restricted to names clearing MIN_AVG_VOLUME -- a thin name spiking on no
    real volume never gets in, move size alone is never enough."""
    scored = []
    for sym in universe:
        s = stats.get(sym)
        if not s:
            continue
        liquidity = s["avg_volume"] if have_avg_vol and s["avg_volume"] > 0 else s["volume"]
        if liquidity < MIN_AVG_VOLUME:
            continue
        pct = s["change_pct"]
        if direction == "up" and pct > 0:
            scored.append((sym, pct))
        elif direction == "down" and pct < 0:
            scored.append((sym, -pct))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return [sym for sym, _ in scored[:MOVER_POOL]]


def run_discovery():
    """Runs the full 3-stage scan and returns the same dict shape that used to
    be (and, via build_volume_leaders.py, still can be) written to
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

    # A ticker in more than one pool keeps only its first tag below (surge
    # takes priority) -- Stage 3 uses this to decide puts-only / spreads-only
    # / both, and a surge leader that also happens to be a mover still gets
    # screened for both sides, same as any other surge leader.
    tag = {}
    for sym in surge_candidates:
        tag[sym] = "surge"
    for sym in up_candidates:
        tag.setdefault(sym, "up")
    for sym in down_candidates:
        tag.setdefault(sym, "down")

    print("Stage 2: options-chain open interest for candidates...")
    opt_oi = {}
    for sym in tag:
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
            reason = tag.get(sym, "surge")
            leader_meta.append({"ticker": sym, "stock_volume": stats.get(sym, {}).get("volume", 0),
                                "option_open_interest": ooi, "reason": reason,
                                "change_pct": stats.get(sym, {}).get("change_pct", 0.0)})
            try:
                put_passers, call_spreads = [], []
                if reason in ("surge", "up"):
                    put_passers, _ = ws.screen_puts(sym)
                if reason in ("surge", "down"):
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
                      "candidate_pool": CANDIDATE_POOL, "top_n": TOP_N,
                      "mover_pool": MOVER_POOL, "top_n_movers": TOP_N_MOVERS,
                      "min_avg_volume": MIN_AVG_VOLUME},
            "leaders": leader_meta,
            "puts": put_rows,
            "call_spreads": spread_rows}
