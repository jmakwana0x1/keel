"""Economics of a synthetic mandate band, replayed over the Phase 0 window (BUILD question: does the
retainer have a price?). Writes data/phase0_band_economics.csv: one row per (pool, x, day) with the band's
own LVR and fee income in USD. sim/model.py turns it into R*_band = k_arb·LVR_band − fees_band.

Band (docs/ARCHITECTURE.md D0.4 / D0.8):
  - re-centred at the first hourly sample of each UTC day to [p0/(1+x), p0·(1+x)], as a curator reshaping
    the mandate position daily would;
  - liquidity L = D / (√p0·(√(1+x) − 1)), with D the class notional in raw token1 at that sample, i.e. the
    band alone supplies depth D to +x (D0.4 definition, up side);
  - price-taking: the band is assumed not to alter the observed price path.

LVR_band = Σ over hourly samples with spot inside the band of σ²_block · L · √p / 4 · 300 blocks.
fees_band: per swap, over the part of its price path [√p_before, √p_after] inside the band,
  up (token1 in):   f/(1−f) · L · Δ√p           raw token1
  down (token0 in): f/(1−f) · L · Δ(1/√p)       raw token0
which is v3's per-position fee identity (fee × the position's share of every step's input).
Both legs are linear in L, so the sign of R*_band does not depend on D.
"""
from __future__ import annotations

import bisect
import csv
import json
import math
from pathlib import Path

from aggregate import Q96, UsdOracle, jsonl, load_meta, token_usd

ROOT = Path(__file__).resolve().parent.parent
MEAS = ROOT / ".cache" / "measure"
DATA = ROOT / "data"
XS = {"1": 0.01, "5": 0.05}
SAMPLE_EVERY = 300
NOTIONAL_USD = {"blue": 1_000_000.0, "other": 100_000.0}  # D per band, by universe group (D0.8)


def run_pool(pool, days, oracle):
    starts = [d["block"] for d in days]
    nd = len(days) - 1
    day_of = lambda b: bisect.bisect_right(starts, b) - 1
    samples = [s for s in jsonl(MEAS / f'{pool["pool"]}.samples.jsonl') if 0 <= day_of(s["block"]) < nd]
    s_blocks = [s["block"] for s in samples]
    sp = [int(s["sqrt"]) / Q96 for s in samples]
    usd = [token_usd(pool, int(s["sqrt"]), s["block"], oracle) for s in samples]
    # per-day realised variance per block, hourly returns (identical to aggregate.py)
    r2 = [0.0] * nd
    for k in range(1, len(samples)):
        r2[day_of(s_blocks[k])] += (2 * math.log(sp[k] / sp[k - 1])) ** 2
    var_blk = [r2[d] / (starts[d + 1] - starts[d]) for d in range(nd)]
    first = {}
    for k, b in enumerate(s_blocks):
        first.setdefault(day_of(b), k)
    band_start_blocks = [s_blocks[first[d]] for d in range(nd)]
    f = pool["fee"] / 1e6
    fr = f / (1 - f)
    D_usd = NOTIONAL_USD["blue" if pool["group"] == "blue" else "other"]

    rows = []
    swaps = list(jsonl(MEAS / f'{pool["pool"]}.swaps.jsonl'))
    sw_blocks = [w[0] for w in swaps]
    for lab, x in XS.items():
        g = math.sqrt(1 + x)
        bands = []
        for d in range(nd):
            k = first[d]
            p0 = sp[k]
            u1 = usd[k][1]
            L = (D_usd / u1) / (p0 * (g - 1))  # raw liquidity units
            bands.append((p0 / g, p0 * g, L))
        lvr = [0.0] * nd
        inr = [0] * nd
        cnt = [0] * nd
        for k, b in enumerate(s_blocks):
            d = bisect.bisect_right(band_start_blocks, b) - 1
            if d < 0:
                continue
            lo, hi, L = bands[d]
            cnt[d] += 1
            if lo < sp[k] < hi:
                inr[d] += 1
                lvr[d] += var_blk[day_of(b)] * L * sp[k] / 4 * SAMPLE_EVERY * usd[k][1]
        fees = [0.0] * nd
        # running price: state at the first band's sample, then every swap after it
        j0 = bisect.bisect_left(sw_blocks, band_start_blocks[0])
        prev = sp[0] if j0 == 0 else int(swaps[j0 - 1][3]) / Q96
        prev = sp[first[0]] if j0 == 0 else prev
        for w in swaps[j0:]:
            b = w[0]
            if b >= starts[nd]:
                break
            cur = int(w[3]) / Q96
            d = bisect.bisect_right(band_start_blocks, b) - 1
            lo, hi, L = bands[d]
            a, c = min(prev, cur), max(prev, cur)
            ol, oh = max(a, lo), min(c, hi)
            if oh > ol:
                si = max(bisect.bisect_right(s_blocks, b) - 1, 0)
                u0, u1 = usd[si]
                if cur > prev:
                    fees[d] += fr * L * (oh - ol) * u1
                else:
                    fees[d] += fr * L * (1 / ol - 1 / oh) * u0
            prev = cur
        for d in range(nd):
            rows.append({"pool": pool["pool"], "x_pct": lab, "date": days[d]["date"], "notional_usd": f"{D_usd:.0f}",
                         "samples": cnt[d], "in_band_samples": inr[d],
                         "lvr_band_usd": f"{lvr[d]:.4f}", "fees_band_usd": f"{fees[d]:.4f}"})
    return rows


def main():
    pools, days = load_meta()
    oracle = UsdOracle(pools)
    out = []
    for p in pools:
        r = run_pool(p, days, oracle)
        out += r
        for lab in XS:
            rr = [x for x in r if x["x_pct"] == lab]
            lv = sum(float(x["lvr_band_usd"]) for x in rr)
            fe = sum(float(x["fees_band_usd"]) for x in rr)
            inb = sum(x["in_band_samples"] for x in rr) / max(sum(x["samples"] for x in rr), 1)
            print(f'{p["base"]}/{p["quote"]} {p["fee"]:5} ±{lab}%: LVR ${lv:,.0f}  fees ${fe:,.0f}  '
                  f'fees/LVR {fe / lv if lv else float("nan"):.3f}  in-band {inb:.2f}', flush=True)
    with open(DATA / "phase0_band_economics.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(out)


if __name__ == "__main__":
    main()
