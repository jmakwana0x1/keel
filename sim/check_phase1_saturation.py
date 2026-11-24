"""Gate 1 check: the per-class saturation medians and the naive-zero / true-positive count, recomputed from the
SOLIDITY walk results (data/phase1_saturation_solidity.csv, written by test/artifacts/SaturationReproduction.t.sol)
and the exported books, must equal Phase 0's committed results. Fails on any difference; writes nothing."""
import csv
import json
from fractions import Fraction
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
Q96 = 1 << 96
XLAB = ["0.5", "1", "2", "5", "10"]


def decode_arrays(raw: bytes, n: int):
    h = raw.hex()
    word = lambda i: int(h[64 * i: 64 * i + 64], 16)
    out = []
    for k in range(n):
        off = word(k) // 32
        ln = word(off)
        out.append([word(off + 1 + j) for j in range(ln)])
    return out


def main():
    pool, sqrt, tick, liq, spacing, fee, ts, tc, ticks, nets, targets, exp_d, exp_f = decode_arrays(
        (DATA / "phase1_saturation_books.bin").read_bytes(), 13)
    universe = json.loads((DATA / "phase0_universe.json").read_text())["pools"]
    res = json.loads((DATA / "phase0_results.json").read_text())
    name = lambda p: f'{p["base"]}/{p["quote"]} {p["fee"] / 1e4:g}%'
    cls_of_idx = [res["classes"][name(p)] for p in universe]
    rows = list(csv.DictReader(open(DATA / "phase1_saturation_solidity.csv")))
    assert len(rows) == 20_700, len(rows)
    recs = []
    zero_true_pos = 0
    for r in rows:
        b, k, depth = int(r["book"]), int(r["walk"]), int(r["depth"])
        s, L, t = sqrt[b], liq[b], targets[b * 10 + k]
        naive = Fraction(L * (t - s), Q96) if t > s else Fraction(L * Q96 * (s - t), s * t)
        if L == 0 and depth > 0:
            zero_true_pos += 1
        ratio = round(float(naive / depth), 6) if depth else None
        recs.append({"cls": cls_of_idx[pool[b]], "x": XLAB[k % 5], "ratio": ratio})
    d = pd.DataFrame(recs).dropna()
    want = res["saturation"]["by_class_x10"]
    for c in ("blue-chip", "mid-cap", "long-tail"):
        for x in XLAB:
            got = round(float(d[(d.cls == c) & (d.x == x)].ratio.median()), 4)
            assert got == want[c][x]["median"], (c, x, got, want[c][x]["median"])
    assert zero_true_pos == 17, zero_true_pos
    print("saturation reproduced from Solidity: per-class medians at all five widths equal Phase 0 "
          f"(±10%: blue-chip {want['blue-chip']['10']['median']}, mid-cap {want['mid-cap']['10']['median']}, "
          f"long-tail {want['long-tail']['10']['median']}); naive-zero/true-positive walks: {zero_true_pos}")


if __name__ == "__main__":
    main()
