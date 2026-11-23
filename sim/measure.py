"""Replay each pool's book across the window and measure, at every hourly sample block:
true depth (exact walk) and naive depth at ±0.5/1/2/5/10%, both directions.

Streams end to end: events are read forward from disk with a one-element peek; samples, swaps and
the books kept for the differential are written as JSONL as they are produced. Peak memory is the
book itself, bounded by its tick count.

Outputs, under .cache/measure/ (derived, large):
  <pool>.samples.jsonl  one line per hourly sample
  <pool>.swaps.jsonl    one line per swap: [block, amount0, amount1, sqrtPriceX96 after]
  <pool>.books.jsonl    the replayed book at the differential's sample points (see pick_samples)
"""
from __future__ import annotations

import json
import os
import random
import sys
from fractions import Fraction
from pathlib import Path

from depth_reference import naive, target_sqrt, walk
from snapshot import calldata, multicall, signed, snapshot, words

ROOT = Path(__file__).resolve().parent.parent
EV, OUT = ROOT / ".cache" / "events", ROOT / ".cache" / "measure"
XS = [Fraction(1, 200), Fraction(1, 100), Fraction(1, 50), Fraction(1, 20), Fraction(1, 10)]
XLAB = ["0.5", "1", "2", "5", "10"]
SAMPLE_EVERY = 300  # blocks, ~1 hour
SPAN = 1100  # ticks beyond the window's extreme ticks; 10% is 954 ticks
DIFF_SEED, DIFF_RANDOM, DIFF_BOUNDARY = 20261123, 8, 4


def window():
    win = json.loads((ROOT / "data" / "phase0_window.json").read_text())["day_start_blocks"]
    return win[0]["block"], win[-1]["block"]  # [start, end)


def pick_samples(pool_addr: str, n_samples: int) -> set[int]:
    """Sample indices whose book is kept for the QuoterV2 differential: seeded random draws plus both
    window endpoints. Replay error is cumulative, so the last sample is the most informative one."""
    rng = random.Random(f"{DIFF_SEED}:{pool_addr}")
    return set(rng.sample(range(n_samples), DIFF_RANDOM)) | {0, n_samples - 1}


def iter_events(path: Path):
    with open(path) as f:
        for line in f:
            yield json.loads(line)


def replay(pool: dict, verbose=True) -> None:
    b0, b_end = window()
    addr = pool["pool"]
    ev_path = EV / f"{addr}.jsonl"
    # pass 1 (streaming): extreme ticks, to size the snapshot
    tmin = tmax = None
    for e in iter_events(ev_path):
        if e[2] == "S":
            tmin = e[7] if tmin is None else min(tmin, e[7])
            tmax = e[7] if tmax is None else max(tmax, e[7])
    t0 = signed(words(multicall([(addr, calldata("slot0"))], b0 - 1)[0][1])[1])
    lo, hi = min(t0, tmin if tmin is not None else t0) - SPAN, max(t0, tmax if tmax is not None else t0) + SPAN
    book = snapshot(pool, b0 - 1, lo, hi)
    ts = pool["tick_spacing"]
    wlo, whi = book.word_range
    in_range = lambda t: wlo <= ((t // ts) >> 8) <= whi

    n_samples = len(range(b0, b_end, SAMPLE_EVERY))
    picks = pick_samples(addr, n_samples)
    rng_b = random.Random(f"{DIFF_SEED}:{addr}:boundary")
    reservoir: list[dict] = []  # seeded reservoir sample of books whose walks include word-boundary steps
    n_boundary_samples = 0

    OUT.mkdir(parents=True, exist_ok=True)
    f_s = open(OUT / f"{addr}.samples.jsonl.tmp", "w")
    f_w = open(OUT / f"{addr}.swaps.jsonl.tmp", "w")
    f_b = open(OUT / f"{addr}.books.jsonl.tmp", "w")
    skipped = n_swaps = 0

    def dump_book(idx, s, why):
        return {"idx": idx, "block": s, "why": why, "sqrt": str(book.sqrt_price), "tick": book.tick,
                "L": str(book.liquidity), "ticks": [[t, str(g), str(n)] for t, (g, n) in sorted(book.ticks.items())]}

    def take(idx, s):
        nonlocal n_boundary_samples
        row = {"i": idx, "block": s, "sqrt": str(book.sqrt_price), "tick": book.tick, "L": str(book.liquidity), "w": {}}
        any_boundary = False
        for up in (True, False):
            for x, lab in zip(XS, XLAB):
                lim = target_sqrt(book.sqrt_price, x, up)
                wk, _ = walk(book, lim)  # Phase 0 reference walk is uncapped; there is no MAX_STEPS here
                nv = naive(book, lim)
                any_boundary |= wk.boundary_steps > 0
                row["w"][("u" if up else "d") + lab] = [str(wk.amount_in), str(wk.fee), wk.steps, wk.crossings,
                                                         wk.boundary_steps, f"{nv.numerator}/{nv.denominator}"]
        f_s.write(json.dumps(row) + "\n")
        if idx in picks:
            f_b.write(json.dumps(dump_book(idx, s, "random" if idx not in (0, n_samples - 1) else "endpoint")) + "\n")
        elif any_boundary:
            n_boundary_samples += 1
            if len(reservoir) < DIFF_BOUNDARY:
                reservoir.append(dump_book(idx, s, "boundary"))
            else:
                j = rng_b.randrange(n_boundary_samples)
                if j < DIFF_BOUNDARY:
                    reservoir[j] = dump_book(idx, s, "boundary")

    events = iter_events(ev_path)
    peek = next(events, None)
    for idx, s in enumerate(range(b0, b_end, SAMPLE_EVERY)):
        while peek is not None and peek[0] < s:
            e = peek
            if e[2] == "S":
                _, _, _, a0, a1, sp, liq, tick = e
                book.sqrt_price, book.liquidity, book.tick = sp, liq, tick
                f_w.write(json.dumps([e[0], a0, a1, str(sp)]) + "\n")
                n_swaps += 1
            else:
                _, _, kind, tl, tu, amt = e
                if amt:
                    sgn = 1 if kind == "M" else -1
                    for t, dn in ((tl, sgn * amt), (tu, -sgn * amt)):
                        if in_range(t):
                            book.touch(t, sgn * amt, dn)
                        else:
                            skipped += 1
                    if tl <= book.tick < tu:
                        book.liquidity += sgn * amt
            peek = next(events, None)
        take(idx, s)
    # swaps after the last sample still matter for the markout horizon
    while peek is not None:
        if peek[2] == "S":
            f_w.write(json.dumps([peek[0], peek[3], peek[4], str(peek[5])]) + "\n")
            n_swaps += 1
        peek = next(events, None)
    for b in sorted(reservoir, key=lambda r: r["idx"]):
        f_b.write(json.dumps(b) + "\n")
    for f in (f_s, f_w, f_b):
        f.close()
    for kind in ("samples", "swaps", "books"):
        os.replace(OUT / f"{addr}.{kind}.jsonl.tmp", OUT / f"{addr}.{kind}.jsonl")
    if verbose:
        print(f'{pool["base"]}/{pool["quote"]} {pool["fee"]}: {n_samples} samples, {n_swaps} swaps, '
              f'{len(book.ticks)} ticks in book, {skipped} out-of-range tick updates ignored, '
              f'{n_boundary_samples} samples with word-boundary steps', flush=True)


def main(only=None):
    pools = json.loads((ROOT / "data" / "phase0_universe.json").read_text())["pools"]
    for p in pools:
        if only and p["pool"] not in only and p["base"] not in only:
            continue
        replay(p)


if __name__ == "__main__":
    main(sys.argv[1:] or None)
