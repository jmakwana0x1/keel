"""Export the 2,070 Phase 0 day-start books for the Solidity saturation reproduction (Gate 1).

Replays every pool exactly as measure.py does and, at each pool's first hourly sample of each day (the rows of
data/phase0_saturation_books.csv), writes the book's initialized ticks within ±WINDOW ticks of spot, the ten target
sqrt-prices (±0.5/1/2/5/10%, up and down), and Phase 0's recorded pre-fee depth and fee for each walk.

Ticks outside the window cannot change a ±10% walk (954 ticks): a walk never steps past its target, and an
initialized tick beyond the target inside the same bitmap word only moves a step target that is clamped to the
limit anyway.

Output: data/phase1_saturation_books.bin, abi.encode of
  (uint32[] pool, uint160[] sqrt, int24[] tick, uint128[] liq, int24[] spacing, uint24[] fee,
   uint32[] tickStart, uint32[] tickCount, int24[] ticks, int128[] nets,
   uint160[] targets, uint256[] expDepth, uint256[] expFee)
with 10 targets/expectations per book in the fixed order u0.5,u1,u2,u5,u10,d0.5,d1,d2,d5,d10.
"""
from __future__ import annotations

import bisect
import json
from pathlib import Path

from aggregate import jsonl
from depth_reference import target_sqrt
from measure import EV, OUT, SAMPLE_EVERY, SPAN, XS, XLAB, iter_events, window
from snapshot import calldata, multicall, signed, snapshot, words

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WINDOW = 1000
KEYS = [("u" if up else "d") + lab for up in (True, False) for lab in XLAB]


def books_for(pool: dict, days: list[int]):
    b0, b_end = window()
    addr = pool["pool"]
    ev_path = EV / f"{addr}.jsonl"
    tmin = tmax = None
    for e in iter_events(ev_path):
        if e[2] == "S":
            tmin = e[7] if tmin is None else min(tmin, e[7])
            tmax = e[7] if tmax is None else max(tmax, e[7])
    t0 = signed(words(multicall([(addr, calldata("slot0"))], b0 - 1)[0][1])[1])
    book = snapshot(pool, b0 - 1, min(t0, tmin if tmin is not None else t0) - SPAN,
                    max(t0, tmax if tmax is not None else t0) + SPAN)
    ts = pool["tick_spacing"]
    wlo, whi = book.word_range
    in_range = lambda t: wlo <= ((t // ts) >> 8) <= whi
    recorded = {s["block"]: s for s in jsonl(OUT / f"{addr}.samples.jsonl")}
    want, seen = [], set()
    for s in range(b0, b_end, SAMPLE_EVERY):
        d = bisect.bisect_right(days, s) - 1
        if 0 <= d < len(days) - 1 and d not in seen:
            seen.add(d)
            want.append(s)
    want_set = set(want)
    events = iter_events(ev_path)
    peek = next(events, None)
    for s in range(b0, b_end, SAMPLE_EVERY):
        while peek is not None and peek[0] < s:
            e = peek
            if e[2] == "S":
                book.sqrt_price, book.liquidity, book.tick = e[5], e[6], e[7]
            else:
                _, _, kind, tl, tu, amt = e
                if amt:
                    sgn = 1 if kind == "M" else -1
                    for t, dn in ((tl, sgn * amt), (tu, -sgn * amt)):
                        if in_range(t):
                            book.touch(t, sgn * amt, dn)
                    if tl <= book.tick < tu:
                        book.liquidity += sgn * amt
            peek = next(events, None)
        if s in want_set:
            rec = recorded[s]
            assert int(rec["sqrt"]) == book.sqrt_price and int(rec["L"]) == book.liquidity, "replay drifted from Phase 0"
            ticks = sorted((t, n) for t, (g, n) in book.ticks.items() if abs(t - book.tick) <= WINDOW)
            targets = [target_sqrt(book.sqrt_price, x, up) for up in (True, False) for x in XS]
            exp = [(int(rec["w"][k][0]), int(rec["w"][k][1])) for k in KEYS]
            yield s, book.sqrt_price, book.tick, book.liquidity, ticks, targets, exp


def enc_array(kind: str, vals: list[int]) -> str:
    out = format(len(vals), "064x")
    for v in vals:
        out += format(v % (1 << 256), "064x")  # two's complement for signed types
    return out


def main():
    pools = json.loads((DATA / "phase0_universe.json").read_text())["pools"]
    days = [d["block"] for d in json.loads((DATA / "phase0_window.json").read_text())["day_start_blocks"]]
    cols = {k: [] for k in ("pool", "sqrt", "tick", "liq", "spacing", "fee", "tickStart", "tickCount", "ticks", "nets",
                            "targets", "expDepth", "expFee")}
    for pi, p in enumerate(pools):
        n = 0
        for s, sq, tk, L, ticks, targets, exp in books_for(p, days):
            cols["pool"].append(pi)
            cols["sqrt"].append(sq)
            cols["tick"].append(tk)
            cols["liq"].append(L)
            cols["spacing"].append(p["tick_spacing"])
            cols["fee"].append(p["fee"])
            cols["tickStart"].append(len(cols["ticks"]))
            cols["tickCount"].append(len(ticks))
            for t, nt in ticks:
                cols["ticks"].append(t)
                cols["nets"].append(nt)
            cols["targets"] += targets
            cols["expDepth"] += [e[0] for e in exp]
            cols["expFee"] += [e[1] for e in exp]
            n += 1
        print(f'{p["base"]}/{p["quote"]} {p["fee"]}: {n} books', flush=True)
    order = list(cols)
    head, tail, off = "", "", 32 * len(order)
    for k in order:
        a = enc_array(k, cols[k])
        head += format(off, "064x")
        tail += a
        off += len(a) // 2
    (DATA / "phase1_saturation_books.bin").write_bytes(bytes.fromhex(head + tail))
    print(f'{len(cols["pool"])} books, {len(cols["ticks"])} ticks, {(len(head) + len(tail)) // 2:,} bytes')


if __name__ == "__main__":
    main()
