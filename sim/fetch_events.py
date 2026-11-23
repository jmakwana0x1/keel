"""Fetch Swap/Mint/Burn logs for every universe pool over the window, decode, and store compact
per-pool event streams under .cache/events/. Raw responses are cached by rpc.py."""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from abi import TOPIC, signed, words
from rpc import get_logs

ROOT = Path(__file__).resolve().parent.parent
EV = ROOT / ".cache" / "events"
STAGE = ROOT / ".cache" / "events.staging"  # partial output; only complete files are moved into EV


def load():
    u = json.loads((ROOT / "data" / "phase0_universe.json").read_text())["pools"]
    win = json.loads((ROOT / "data" / "phase0_window.json").read_text())["day_start_blocks"]
    return u, win[0]["block"], win[-1]["block"] - 1


def decode(log: dict):
    t0 = log["topics"][0]
    d = words(log["data"])
    blk, idx = int(log["blockNumber"], 16), int(log["logIndex"], 16)
    if t0 == TOPIC["Swap"]:
        a0, a1, sp, liq, tick = signed(d[0]), signed(d[1]), d[2], d[3], signed(d[4])
        return [blk, idx, "S", a0, a1, sp, liq, tick]
    lo, hi = signed(int(log["topics"][2], 16)), signed(int(log["topics"][3], 16))
    amt = d[1] if t0 == TOPIC["Mint"] else d[0]
    return [blk, idx, "M" if t0 == TOPIC["Mint"] else "B", lo, hi, amt]


def main(workers: int = 3, batch: int = 6):
    """Streams: chunks are fetched `batch` at a time, decoded, appended per pool, and discarded.
    Peak memory is a few chunks (~2.5 MB of JSON each), not the whole window."""
    pools, frm, to = load()
    addrs = [p["pool"] for p in pools]
    topics = [TOPIC["Swap"], TOPIC["Mint"], TOPIC["Burn"]]
    chunks = [(b, min(b + 1999, to)) for b in range(frm - frm % 2000, to + 1, 2000)]
    EV.mkdir(parents=True, exist_ok=True)
    STAGE.mkdir(parents=True, exist_ok=True)
    tmp = {a: open(STAGE / f"{a}.jsonl", "w") for a in addrs}
    counts = {a: [0, 0] for a in addrs}
    with ThreadPoolExecutor(workers) as ex:
        for k in range(0, len(chunks), batch):
            part = chunks[k:k + batch]
            # in-order results, so per-pool streams stay sorted across chunks
            for logs in ex.map(lambda c: get_logs(addrs, topics, max(c[0], frm), c[1]), part):
                rows = sorted((decode(l), l["address"].lower()) for l in logs if not l.get("removed"))
                for ev, a in rows:
                    tmp[a].write(json.dumps(ev) + "\n")
                    counts[a][0] += 1
                    counts[a][1] += ev[2] == "S"
                del logs, rows
            print(f"{min(k + batch, len(chunks))}/{len(chunks)}", flush=True)
    for a, f in tmp.items():
        f.close()
        os.replace(STAGE / f"{a}.jsonl", EV / f"{a}.jsonl")
        print(a, counts[a][0], counts[a][1], "swaps")


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
