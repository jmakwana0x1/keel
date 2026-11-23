"""Phase 0.4 — does depth withdraw when volatility spikes?

Replays each pool's book (same snapshot + events as measure.py) and, at every hourly sample, records
effective liquidity at ±1% and ±5% for two books:

  actual  — the book as it is at this sample
  frozen  — the previous sample's book (no mints/burns since), re-priced to this sample's price

Effective liquidity L_eff = D / Δ√p for the exact walk to ±x (average of up and down sides). It is
price-neutral: it changes only when the book's shape around spot changes, not when the token reprices.

Decomposition of the hour's change in L_eff:
  passive = frozen − previous actual   (price moved through a fixed book)
  active  = actual − frozen            (liquidity minted or burned during the hour)
Self-check: in any hour with no Mint/Burn of non-zero amount, frozen must equal actual exactly. The run
asserts it, so a wrong re-pricing cannot pass silently.

Output: data/phase0_vol_depth.csv, one row per pool-hour.
"""
from __future__ import annotations

import csv
import json
import math
from fractions import Fraction
from pathlib import Path

from depth_reference import Q96, Book, target_sqrt, walk
from measure import EV, SAMPLE_EVERY, SPAN, iter_events, window
from snapshot import calldata, multicall, signed, snapshot, words

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
XS = {"1": Fraction(1, 100), "5": Fraction(1, 20)}


def l_eff(book: Book, x: Fraction) -> float:
    sp = book.sqrt_price
    up_lim, dn_lim = target_sqrt(sp, x, True), target_sqrt(sp, x, False)
    up = walk(book, up_lim)[0].amount_in
    dn = walk(book, dn_lim)[0].amount_in
    l_up = up * Q96 / (up_lim - sp)                      # amount1 = L·Δ√p
    l_dn = dn * sp * dn_lim / (Q96 * (sp - dn_lim))       # amount0 = L·Δ(1/√p)
    return (l_up + l_dn) / 2


def reprice(book: Book, sqrt_new: int, tick_new: int) -> Book:
    """The same tick set at a new price: active liquidity adjusted by every initialized tick crossed."""
    liq = book.liquidity
    a, b = book.tick, tick_new
    if b > a:
        for t, (g, n) in book.ticks.items():
            if a < t <= b:
                liq += n
    elif b < a:
        for t, (g, n) in book.ticks.items():
            if b < t <= a:
                liq -= n
    assert liq >= 0
    return Book(sqrt_price=sqrt_new, tick=tick_new, liquidity=liq, tick_spacing=book.tick_spacing, fee=book.fee,
                ticks=book.ticks)


def run(pool: dict) -> list[dict]:
    b0, b_end = window()
    addr = pool["pool"]
    ev_path = EV / f"{addr}.jsonl"
    tmin = tmax = None
    for e in iter_events(ev_path):
        if e[2] == "S":
            tmin = e[7] if tmin is None else min(tmin, e[7])
            tmax = e[7] if tmax is None else max(tmax, e[7])
    t0 = signed(words(multicall([(addr, calldata("slot0"))], b0 - 1)[0][1])[1])
    lo = min(t0, tmin if tmin is not None else t0) - SPAN
    hi = max(t0, tmax if tmax is not None else t0) + SPAN
    book = snapshot(pool, b0 - 1, lo, hi)
    ts = pool["tick_spacing"]
    wlo, whi = book.word_range
    in_range = lambda t: wlo <= ((t // ts) >> 8) <= whi

    rows, prev = [], None
    events = iter_events(ev_path)
    peek = next(events, None)
    checked = 0
    for idx, s in enumerate(range(b0, b_end, SAMPLE_EVERY)):
        touched = False
        while peek is not None and peek[0] < s:
            e = peek
            if e[2] == "S":
                book.sqrt_price, book.liquidity, book.tick = e[5], e[6], e[7]
            else:
                _, _, kind, tl, tu, amt = e
                if amt:
                    touched = True
                    sgn = 1 if kind == "M" else -1
                    for t, dn in ((tl, sgn * amt), (tu, -sgn * amt)):
                        if in_range(t):
                            book.touch(t, sgn * amt, dn)
                    if tl <= book.tick < tu:
                        book.liquidity += sgn * amt
            peek = next(events, None)
        row = {"pool": addr, "i": idx, "block": s, "sqrt": str(book.sqrt_price), "mintburn": int(touched)}
        for lab, x in XS.items():
            row[f"leff{lab}"] = f"{l_eff(book, x):.10e}"
        if prev is not None:
            fr = reprice(prev, book.sqrt_price, book.tick)
            for lab, x in XS.items():
                row[f"frozen{lab}"] = f"{l_eff(fr, x):.10e}"
            if not touched:
                assert fr.liquidity == book.liquidity, (addr, s, fr.liquidity, book.liquidity)
                assert row["frozen1"] == row["leff1"] and row["frozen5"] == row["leff5"], (addr, s)
                checked += 1
        else:
            for lab in XS:
                row[f"frozen{lab}"] = ""
        rows.append(row)
        prev = Book(sqrt_price=book.sqrt_price, tick=book.tick, liquidity=book.liquidity,
                    tick_spacing=book.tick_spacing, fee=book.fee,
                    ticks={t: list(v) for t, v in book.ticks.items()})
    print(f'{pool["base"]}/{pool["quote"]} {pool["fee"]}: {len(rows)} hours, '
          f'{checked} untouched hours where frozen == actual exactly', flush=True)
    return rows


def main():
    pools = json.loads((DATA / "phase0_universe.json").read_text())["pools"]
    out = []
    for p in pools:
        out += run(p)
    with open(DATA / "phase0_vol_depth.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(out)


if __name__ == "__main__":
    main()
