#!/usr/bin/env python3
"""
build_volume_leaders.py -- offline fallback generator for volume_leaders.json.

The live app now runs discover.run_discovery() directly (cached, ttl=600 --
refreshes on the same 30-min-auto/on-demand cadence as the rest of the
screener) instead of reading a precomputed file. This script + its daily
GitHub Action remain as a safety net: if the live scan ever errors inside the
app (Tradier hiccup, the universe source going down, etc.), 1_Options_Screener.py
falls back to whatever this last wrote. Needs TRADIER_TOKEN.
"""
import os
import sys
import json

import discover

OUT = "volume_leaders.json"


def main():
    if not os.environ.get("TRADIER_TOKEN"):
        print("TRADIER_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    out = discover.run_discovery()
    with open(OUT, "w") as f:
        json.dump(out, f, default=str)
    print(f"wrote {OUT}: {len(out['leaders'])} leaders, {len(out['puts'])} qualifying puts, "
          f"{len(out['call_spreads'])} qualifying call credit spreads")


if __name__ == "__main__":
    main()
