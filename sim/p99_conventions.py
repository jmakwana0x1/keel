"""Which percentile convention produced which p99 of the saturation ratio (naive / true depth).

data/phase0_results.md tabulates with pandas' default, LINEAR interpolation. A different convention on the same
cells gives different numbers; the one that circulated in review (4.175x) is the long-tail ±10% cell under
nearest-rank. This script commits that diagnosis and asserts it, so the registry entry in docs/CLAIMS.md (row 4)
traces to a file and fails if the data ever stop supporting it.

Output: data/phase0_p99_conventions.json
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import model

DATA = Path(__file__).resolve().parent.parent / "data"
CONVENTIONS = {"linear (pandas default, used in phase0_results.md)": "linear",
               "nearest-rank, ceil(0.99 n) (numpy inverted_cdf)": "inverted_cdf",
               "round(0.99 (n-1)) (numpy nearest)": "nearest"}


def p99(v, method):
    return round(float(np.quantile(np.sort(v.values), 0.99, method=method)), 4)


def main():
    pools = json.loads((DATA / "phase0_universe.json").read_text())["pools"]
    days = pd.read_csv(DATA / "phase0_pool_days.csv")
    cls = model.classify(pools, days)
    s = pd.read_csv(DATA / "phase0_saturation_books.csv", dtype={"x_pct": str}).dropna(subset=["ratio"])
    s["cls"] = s.pool.map(cls)
    # the ratio is naive / true
    row = s.iloc[0]
    assert math.isclose(float(row.ratio), float(row.naive_raw) / float(row.true_raw), rel_tol=1e-5), "ratio is not naive/true"

    cells = {"long-tail ±10%": s[(s.cls == "long-tail") & (s.x_pct == "10")].ratio,
             "pooled ±10%": s[s.x_pct == "10"].ratio,
             "pooled, all widths": s.ratio}
    out = {"ratio": "naive / true depth", "percentile": 0.99, "cells": {}}
    for name, v in cells.items():
        out["cells"][name] = {"n": int(len(v)), **{k: p99(v, m) for k, m in CONVENTIONS.items()}}

    lt = out["cells"]["long-tail ±10%"]
    # the claims this artifact exists to back; it fails rather than regenerates if they stop holding
    assert lt["linear (pandas default, used in phase0_results.md)"] == 3.9145, "committed table value moved"
    assert lt["nearest-rank, ceil(0.99 n) (numpy inverted_cdf)"] == 4.1754, "4.175 is no longer this cell"
    for k, v in out["cells"]["pooled ±10%"].items():
        if k != "n":
            assert v < 4.175, "4.175 should exceed every pooled p99"
    (DATA / "phase0_p99_conventions.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
