"""Phase 0.5 control group (D0.12): positions that DO re-centre.

Owner = Mint/Burn `owner` (topic 1). The NonfungiblePositionManager is excluded: every NFT position shares its address.
Re-centre event: the same owner burns liquidity > 0 from a range and mints into a different range within 10 blocks.
Managed owner: >= 5 re-centre events on that pool in the window.

For each managed owner, replayed over the window:
  - re-centre latency: blocks from spot leaving the burned range (while the position was live) to the re-mint.
    A re-centre whose burned range still contained spot is "proactive" (latency undefined).
  - share of the pool's active liquidity held by the owner's in-range positions, at hourly samples
  - fee yield: fees by the per-position identity f/(1-f)·L·Δ√p over in-range swap paths, per $ of position value
    per day (value sampled hourly).
Known limit: positions opened before the window are tracked only from their first in-window event.

Output: data/phase0_managed.csv, one row per (pool, managed owner).
"""
from __future__ import annotations

import bisect
import csv
import json
from collections import defaultdict
from pathlib import Path

from aggregate import UsdOracle, token_usd
from depth_reference import Q96, sqrt_at_tick
from measure import EV, SAMPLE_EVERY, iter_events, window

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
NPM = "0xc36442b4a4522e871399cd717abdd847ab11fe88"
WITHIN, MIN_EVENTS = 10, 5


def recentre_events(owner_rows):
    """Per owner: list of (burn_block, (lo, hi), mint_block, (lo2, hi2))."""
    last_burn = {}
    ev = defaultdict(list)
    for blk, idx, kind, lo, hi, amt, owner, tx in owner_rows:
        if owner == NPM or amt == 0:
            continue
        if kind == "B":
            last_burn[owner] = (blk, (lo, hi))
        elif owner in last_burn:
            bb, rng = last_burn[owner]
            if (lo, hi) != rng and blk - bb <= WITHIN:
                ev[owner].append((bb, rng, blk, (lo, hi)))
                del last_burn[owner]
    return ev


def run(pool: dict, oracle: UsdOracle) -> list[dict]:
    addr = pool["pool"]
    owner_rows = [tuple(r) for r in iter_events(EV / f"{addr}.owners.jsonl")]
    events = recentre_events(owner_rows)
    managed = {o for o, e in events.items() if len(e) >= MIN_EVENTS}
    if not managed:
        print(f'{pool["base"]}/{pool["quote"]} {pool["fee"]}: 0 managed owners', flush=True)
        return [{"pool": addr, "owner": "", "recentres": 0, "reactive": 0, "median_latency_blocks": "",
                 "median_active_share": "", "fees_usd": "", "value_days_usd": "", "fee_yield_per_day": ""}]
    owner_of = {(r[0], r[1]): r[6] for r in owner_rows}
    b0, b_end = window()
    samples = list(range(b0, b_end, SAMPLE_EVERY))
    fr = pool["fee"] / (1e6 - pool["fee"])

    pos = {}                     # (owner, lo, hi) -> L
    tick, liq, sp = None, 0, None
    fees = defaultdict(float)
    value_days = defaultdict(float)
    shares = defaultdict(list)
    latencies = defaultdict(list)
    si = 0
    for e in iter_events(EV / f"{addr}.jsonl"):
        while si < len(samples) and samples[si] <= e[0]:
            if sp is not None:
                u0, u1 = token_usd(pool, int(sp * Q96), samples[si], oracle)
                for (o, lo, hi), L in pos.items():
                    sa, sb = sqrt_at_tick(lo) / Q96, sqrt_at_tick(hi) / Q96
                    s = min(max(sp, sa), sb)
                    value_days[o] += (L * (1 / s - 1 / sb) * u0 + L * (s - sa) * u1) / 24
                if liq:
                    for o in managed:
                        mine = sum(L for (oo, lo, hi), L in pos.items() if oo == o and lo <= tick < hi)
                        shares[o].append(mine / liq)
            si += 1
        if e[2] == "S":
            new = e[5] / Q96
            if sp is not None:
                u0, u1 = token_usd(pool, e[5], e[0], oracle)
                for (o, lo, hi), L in pos.items():
                    sa, sb = sqrt_at_tick(lo) / Q96, sqrt_at_tick(hi) / Q96
                    a, b = max(min(sp, new), sa), min(max(sp, new), sb)
                    if b > a:
                        fees[o] += fr * L * ((b - a) * u1 if new > sp else (1 / a - 1 / b) * u0)
            sp, liq, tick = new, e[6], e[7]
            continue
        _, _, kind, lo, hi, amt = e
        o = owner_of.get((e[0], e[1]))
        if o not in managed or amt == 0:
            continue
        k = (o, lo, hi)
        if kind == "M":
            pos[k] = pos.get(k, 0) + amt
        elif k in pos:  # positions opened before the window are not tracked (known limit)
            pos[k] -= amt
            if pos[k] <= 0:
                del pos[k]
    # latency per re-centre event, from the burn's recorded exit block
    rows = []
    for o in sorted(managed):
        lat, reactive = [], 0
        # reactive = the burned range no longer contained spot, and the exit is observed inside the window
        for bb, (lo, hi), mb, _ in events[o]:
            ex = _exit_block(addr, lo, hi, bb)
            if ex is not None:
                reactive += 1
                lat.append(mb - ex)
        lat.sort()
        sh = sorted(shares[o])
        rows.append({"pool": addr, "owner": o, "recentres": len(events[o]), "reactive": reactive,
                     "median_latency_blocks": lat[len(lat) // 2] if lat else "",
                     "median_active_share": f"{sh[len(sh) // 2]:.6f}" if sh else "",
                     "fees_usd": f"{fees[o]:.4f}", "value_days_usd": f"{value_days[o]:.4f}",
                     "fee_yield_per_day": f"{fees[o] / value_days[o]:.8f}" if value_days[o] > 0 else ""})
    print(f'{pool["base"]}/{pool["quote"]} {pool["fee"]}: {len(managed)} managed owners, '
          f'{sum(r["recentres"] for r in rows)} re-centre events', flush=True)
    return rows


_TAPE: dict[str, tuple[list[int], list[int]]] = {}


def _exit_block(addr: str, lo: int, hi: int, burn_block: int) -> int | None:
    """Block at which the tick last left [lo, hi) before `burn_block`, or None if the tick was inside at the burn."""
    if addr not in _TAPE:
        blocks, ticks = [], []
        for e in iter_events(EV / f"{addr}.jsonl"):
            if e[2] == "S":
                blocks.append(e[0])
                ticks.append(e[7])
        _TAPE[addr] = (blocks, ticks)
    blocks, ticks = _TAPE[addr]
    j = bisect.bisect_left(blocks, burn_block) - 1
    if j < 0 or lo <= ticks[j] < hi:
        return None
    while j > 0 and not (lo <= ticks[j - 1] < hi):
        j -= 1
    return blocks[j] if j > 0 else None


def main():
    pools = json.loads((DATA / "phase0_universe.json").read_text())["pools"]
    oracle = UsdOracle(pools)
    out = []
    for p in pools:
        out += run(p, oracle)
        _TAPE.clear()
    with open(DATA / "phase0_managed.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(out)


if __name__ == "__main__":
    main()
