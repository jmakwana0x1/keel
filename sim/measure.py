"""Replay each pool's book across the window and measure, at every hourly sample block:
true depth (exact walk) and naive depth at ±0.5/1/2/5/10%, both directions. Also emits the per-swap
quantities needed for volume and markout. Output: .cache/measure/<pool>.json (derived, large)."""
from __future__ import annotations

import bisect
import json
import sys
from fractions import Fraction
from pathlib import Path

from depth_reference import Q96, Book, naive, target_sqrt, walk
from snapshot import snapshot

ROOT = Path(__file__).resolve().parent.parent
EV, OUT = ROOT / ".cache" / "events", ROOT / ".cache" / "measure"
XS = [Fraction(1, 200), Fraction(1, 100), Fraction(1, 50), Fraction(1, 20), Fraction(1, 10)]
XLAB = ["0.5", "1", "2", "5", "10"]
SAMPLE_EVERY = 300  # blocks, ~1 hour
SPAN = 1100  # ticks beyond the window's extreme ticks; 10% is 954 ticks


def window():
    win = json.loads((ROOT / "data" / "phase0_window.json").read_text())["day_start_blocks"]
    return win[0]["block"], win[-1]["block"]  # [start, end)


def replay(pool: dict, verbose=True) -> dict:
    b0, b_end = window()
    evs = json.loads((EV / f'{pool["pool"]}.json').read_text())
    swap_ticks = [e[7] for e in evs if e[2] == "S"]
    from snapshot import multicall, calldata, words, signed
    t0 = signed(words(multicall([(pool["pool"], calldata("slot0"))], b0 - 1)[0][1])[1])
    lo, hi = min(swap_ticks + [t0]) - SPAN, max(swap_ticks + [t0]) + SPAN
    book = snapshot(pool, b0 - 1, lo, hi)
    ts = pool["tick_spacing"]
    wlo, whi = book.word_range
    in_range = lambda t: wlo <= ((t // ts) >> 8) <= whi

    samples, swaps = [], []
    nxt = b0
    i = 0
    skipped_out_of_range = 0

    def take(s):
        row = {"block": s, "sqrt": str(book.sqrt_price), "tick": book.tick, "L": str(book.liquidity), "w": {}}
        for up in (True, False):
            for x, lab in zip(XS, XLAB):
                lim = target_sqrt(book.sqrt_price, x, up)
                wk, capped = walk(book, lim)
                assert not capped
                nv = naive(book, lim)
                row["w"][("u" if up else "d") + lab] = [str(wk.amount_in), str(wk.fee), wk.steps, wk.crossings,
                                                         wk.boundary_steps, f"{float(nv):.6e}"]
        samples.append(row)

    while nxt < b_end:
        while i < len(evs) and evs[i][0] < nxt:
            e = evs[i]
            if e[2] == "S":
                _, _, _, a0, a1, sp, liq, tick = e
                book.sqrt_price, book.liquidity, book.tick = sp, liq, tick
                swaps.append([e[0], a0, a1, str(sp), str(liq)])
            else:
                _, _, kind, tl, tu, amt = e
                if amt:
                    sgn = 1 if kind == "M" else -1
                    if in_range(tl): book.touch(tl, sgn * amt, sgn * amt)
                    else: skipped_out_of_range += 1
                    if in_range(tu): book.touch(tu, sgn * amt, -sgn * amt)
                    else: skipped_out_of_range += 1
                    if tl <= book.tick < tu:
                        book.liquidity += sgn * amt
            i += 1
        take(nxt)
        nxt += SAMPLE_EVERY
    # remaining events (blocks before b_end) only matter for swaps / markout horizon
    while i < len(evs):
        e = evs[i]
        if e[2] == "S":
            swaps.append([e[0], e[3], e[4], str(e[5]), str(e[6])])
        i += 1
    if verbose:
        print(f'{pool["base"]}/{pool["quote"]} {pool["fee"]}: {len(samples)} samples, {len(swaps)} swaps, '
              f'{len(book.ticks)} ticks in book, {skipped_out_of_range} out-of-range tick updates ignored', flush=True)
    return {"pool": pool["pool"], "word_range": [wlo, whi], "samples": samples, "swaps": swaps}


def main(only=None):
    pools = json.loads((ROOT / "data" / "phase0_universe.json").read_text())["pools"]
    OUT.mkdir(parents=True, exist_ok=True)
    for p in pools:
        if only and p["pool"] not in only and p["base"] not in only:
            continue
        r = replay(p)
        (OUT / f'{p["pool"]}.json').write_text(json.dumps(r))


if __name__ == "__main__":
    main(sys.argv[1:] or None)
