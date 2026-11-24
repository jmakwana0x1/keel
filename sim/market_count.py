"""How many Uniswap v3/v4 pools on Ethereum, Base, Arbitrum and Unichain meet the D0.3 mid-cap definition?

Source: DefiLlama yields API (https://yields.llama.fi/pools), a third-party aggregator. NOT chain-measured.
Proxy: D0.3 uses median daily volume over 90 days; this uses volumeUsd7d / 7. DefiLlama also omits pools below its
own TVL floor, so counts are lower bounds. The proxy is checked against the 15 non-blue-chip pools whose class was
measured on-chain in Phase 0 (agreement reported by model.py).

`python3 market_count.py fetch` writes data/phase0_market_pools.json (a filtered, dated snapshot); model.py counts
from that file, so the count regenerates offline.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
CHAINS = {"Ethereum", "Base", "Arbitrum", "Unichain"}
PROJECTS = {"uniswap-v3", "uniswap-v4"}


def fetch():
    req = urllib.request.Request("https://yields.llama.fi/pools", headers={"User-Agent": "keel-sim/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    keep = ["project", "chain", "symbol", "poolMeta", "tvlUsd", "volumeUsd1d", "volumeUsd7d", "stablecoin",
            "underlyingTokens", "pool"]
    rows = [{k: p.get(k) for k in keep} for p in d["data"] if p["project"] in PROJECTS and p["chain"] in CHAINS]
    rows.sort(key=lambda p: (p["project"], p["chain"], p["pool"]))
    out = {"source": "https://yields.llama.fi/pools", "fetched_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "filter": "project in uniswap-v3/v4, chain in Ethereum/Base/Arbitrum/Unichain", "pools": rows}
    (DATA / "phase0_market_pools.json").write_text(json.dumps(out, indent=0) + "\n")
    print(len(rows), "pools written")


if __name__ == "__main__" and sys.argv[1:] == ["fetch"]:
    fetch()
