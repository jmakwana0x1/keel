"""Fetch Swap/Mint/Burn logs for every universe pool over the window, decode, and store compact
per-pool event streams under .cache/events/. Raw responses are cached by rpc.py."""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from abi import TOPIC, signed, words
from rpc import get_logs

ROOT = Path(__file__).resolve().parent.parent
EV = ROOT / ".cache" / "events"


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


def main(workers: int = 3):
    pools, frm, to = load()
    addrs = [p["pool"] for p in pools]
    topics = [TOPIC["Swap"], TOPIC["Mint"], TOPIC["Burn"]]
    chunks = [(b, min(b + 1999, to)) for b in range(frm - frm % 2000, to + 1, 2000)]
    done = [0]

    def job(c):
        logs = get_logs(addrs, topics, max(c[0], frm), c[1])
        done[0] += 1
        if done[0] % 25 == 0:
            print(f"{done[0]}/{len(chunks)}", flush=True)
        return logs

    with ThreadPoolExecutor(workers) as ex:
        all_logs = [l for part in ex.map(job, chunks) for l in part]
    per = {a: [] for a in addrs}
    for l in all_logs:
        if l.get("removed"):
            continue
        per[l["address"].lower()].append(decode(l))
    EV.mkdir(parents=True, exist_ok=True)
    for a, evs in per.items():
        evs.sort(key=lambda e: (e[0], e[1]))
        (EV / f"{a}.json").write_text(json.dumps(evs))
        print(a, len(evs), sum(1 for e in evs if e[2] == "S"), "swaps")


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
