"""Reduce the per-block replay (.cache/measure/) to the committed Phase 0 inputs in data/:

  phase0_pool_days.csv        one row per pool-day: volume, depth, naive depth, sigma, price, markout, LVR, fees
  phase0_saturation_books.csv one row per (pool, day-start sample, x, side): true vs naive depth
  phase0_quoter_diff.json     the zero-tolerance differential of the replayed walk against QuoterV2

These are measured from chain data. sim/model.py turns them into results without the network.
"""
from __future__ import annotations

import bisect
import csv
import json
import math
import random
from pathlib import Path

from depth_reference import Q96

ROOT = Path(__file__).resolve().parent.parent
MEAS = ROOT / ".cache" / "measure"
DATA = ROOT / "data"
XLAB = ["0.5", "1", "2", "5", "10"]
HORIZONS = [1, 25, 300]
SAMPLE_EVERY = 300


def load_meta():
    pools = json.loads((DATA / "phase0_universe.json").read_text())["pools"]
    days = json.loads((DATA / "phase0_window.json").read_text())["day_start_blocks"]
    return pools, days


def raw_price(sqrt: int) -> float:
    """token1 raw units per token0 raw unit."""
    return (sqrt / Q96) ** 2


class UsdOracle:
    """ETH/USD at each hourly sample, from the replayed WETH/USDC 0.05% book."""

    def __init__(self, pools):
        p = next(q for q in pools if q["fee"] == 500 and {q["token0"], q["token1"]} == {"USDC", "WETH"})
        m = json.loads((MEAS / f'{p["pool"]}.json').read_text())
        self.blocks, self.eth = [], []
        for s in m["samples"]:
            pr = raw_price(int(s["sqrt"])) * 10 ** (p["decimals0"] - p["decimals1"])  # token1 per token0, human
            eth = pr if p["token0"] == "WETH" else 1 / pr
            self.blocks.append(s["block"])
            self.eth.append(eth)

    def eth_usd(self, block: int) -> float:
        i = bisect.bisect_right(self.blocks, block) - 1
        return self.eth[max(i, 0)]


def token_usd(pool, sqrt: int, block: int, oracle: UsdOracle) -> tuple[float, float]:
    """USD per *raw* unit of token0 and token1 at this price."""
    q_usd = oracle.eth_usd(block) if pool["quote"] == "WETH" else 1.0
    pr_human = raw_price(sqrt) * 10 ** (pool["decimals0"] - pool["decimals1"])  # token1 per token0
    if pool["quote"] == pool["token1"]:
        t1, t0 = q_usd, pr_human * q_usd
    else:
        t0, t1 = q_usd, q_usd / pr_human
    return t0 / 10 ** pool["decimals0"], t1 / 10 ** pool["decimals1"]


def day_index(days, block):
    starts = [d["block"] for d in days]
    return bisect.bisect_right(starts, block) - 1


def pool_rows(pool, days, oracle):
    m = json.loads((MEAS / f'{pool["pool"]}.json').read_text())
    starts = [d["block"] for d in days]
    end_block = starts[-1]
    nd = len(days) - 1
    acc = [dict(n=0, d1=0.0, d5=0.0, n1=0.0, n5=0.0, base=0.0, r2=0.0, nr=0, lvr=0.0, vol=0.0, swaps=0, fees=0.0,
                **{f"loss_h{h}": 0.0 for h in HORIZONS}, **{f"mk_n_h{h}": 0 for h in HORIZONS}) for _ in range(nd)]
    sat = []
    prev_lp = None
    samples = m["samples"]
    s_blocks = [s["block"] for s in samples]
    first_of_day = set()
    seen = set()
    for s in samples:
        di = day_index(days, s["block"])
        if di not in seen:
            seen.add(di)
            first_of_day.add(s["block"])
    for s in samples:
        b, sq, L = s["block"], int(s["sqrt"]), int(s["L"])
        di = day_index(days, b)
        if di < 0 or di >= nd:
            continue
        u0, u1 = token_usd(pool, sq, b, oracle)
        a = acc[di]
        a["n"] += 1
        for lab, key_d, key_n in (("1", "d1", "n1"), ("5", "d5", "n5")):
            up, dn = s["w"]["u" + lab], s["w"]["d" + lab]
            a[key_d] += (int(up[0]) * u1 + int(dn[0]) * u0) / 2
            a[key_n] += (float(up[5]) * u1 + float(dn[5]) * u0) / 2
        a["base"] += (u0 * 10 ** pool["decimals0"]) if pool["base"] == pool["token0"] else (u1 * 10 ** pool["decimals1"])
        lp = math.log(raw_price(sq))
        if prev_lp is not None:
            a["r2"] += (lp - prev_lp) ** 2
            a["nr"] += 1
        prev_lp = lp
        # LVR over the following sample interval, using this day's realised variance (filled in below)
        a.setdefault("lvr_terms", []).append(L * (sq / Q96) / 4 * SAMPLE_EVERY * u1)
        if b in first_of_day:
            for side in ("u", "d"):
                for lab in XLAB:
                    w = s["w"][side + lab]
                    true, nv = int(w[0]), float(w[5])
                    usd = u1 if side == "u" else u0
                    sat.append({"pool": pool["pool"], "date": days[di]["date"], "block": b, "x_pct": lab,
                                "side": "up" if side == "u" else "down",
                                "true_raw": w[0], "naive_raw": f"{nv:.6e}", "true_usd": f"{true * usd:.2f}",
                                "naive_usd": f"{nv * usd:.2f}", "ratio": (f"{nv / true:.6f}" if true else ""),
                                "steps": w[2], "crossings": w[3]})
    # variance per block, per day, then LVR
    for di, a in enumerate(acc):
        blocks_in_day = starts[di + 1] - starts[di]
        var_blk = a["r2"] / blocks_in_day if a["nr"] else 0.0
        a["sigma_day"] = math.sqrt(a["r2"])
        a["lvr"] = var_blk * sum(a.pop("lvr_terms", []))

    # swaps: volume, fees, markout
    sw = m["swaps"]
    sw_blocks = [x[0] for x in sw]
    sw_sqrt = [int(x[3]) for x in sw]
    fee = pool["fee"] / 1e6
    quote_is_0 = pool["quote"] == pool["token0"]
    for k, (blk, a0, a1, sq, _) in enumerate(sw):
        di = day_index(days, blk)
        if di < 0 or di >= nd:
            continue
        si = max(bisect.bisect_right(s_blocks, blk) - 1, 0)
        u0, u1 = token_usd(pool, int(samples[si]["sqrt"]), samples[si]["block"], oracle)
        a = acc[di]
        a["swaps"] += 1
        a["vol"] += abs(a0) * u0 if quote_is_0 else abs(a1) * u1
        f0, f1 = (a0 * fee if a0 > 0 else 0.0), (a1 * fee if a1 > 0 else 0.0)
        a["fees"] += f0 * u0 + f1 * u1
        for h in HORIZONS:
            if blk + h >= end_block:
                continue
            j = bisect.bisect_right(sw_blocks, blk + h) - 1
            p_fut = raw_price(sw_sqrt[j])
            gain_t1 = (a0 - f0) * p_fut + (a1 - f1)
            a[f"loss_h{h}"] += -gain_t1 * u1
            a[f"mk_n_h{h}"] += 1
    rows = []
    for di, a in enumerate(acc):
        if a["n"] == 0:
            continue
        rows.append({"pool": pool["pool"], "date": days[di]["date"], "samples": a["n"], "swaps": a["swaps"],
                     "volume_usd": f'{a["vol"]:.2f}', "depth1_usd": f'{a["d1"] / a["n"]:.2f}',
                     "depth5_usd": f'{a["d5"] / a["n"]:.2f}', "naive1_usd": f'{a["n1"] / a["n"]:.2f}',
                     "naive5_usd": f'{a["n5"] / a["n"]:.2f}', "sigma_day": f'{a["sigma_day"]:.6e}',
                     "base_usd": f'{a["base"] / a["n"]:.6e}', "fees_usd": f'{a["fees"]:.2f}',
                     "lvr_usd": f'{a["lvr"]:.2f}',
                     **{f"loss_h{h}_usd": f'{a[f"loss_h{h}"]:.2f}' for h in HORIZONS}})
    return rows, sat


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
        wr.writeheader()
        wr.writerows(rows)


def quoter_diff(pools, days, per_pool=8, boundary_extra=4, seed=20261123):
    """Re-execute a seeded sample of replayed walks against the deployed QuoterV2 at the same block."""
    from fractions import Fraction

    from snapshot import quote_gross_in
    xs = dict(zip(XLAB, [Fraction(1, 200), Fraction(1, 100), Fraction(1, 50), Fraction(1, 20), Fraction(1, 10)]))
    from depth_reference import target_sqrt
    rng = random.Random(seed)
    n = mism = boundary_cases = 0
    bad = []
    per = {}
    for p in pools:
        m = json.loads((MEAS / f'{p["pool"]}.json').read_text())
        S = m["samples"]
        pick = set(rng.sample(range(len(S)), per_pool))
        wb = [i for i, s in enumerate(S) if any(w[4] > 0 for w in s["w"].values())]
        pick |= set(rng.sample(wb, min(boundary_extra, len(wb))))
        pc = 0
        for i in sorted(pick):
            s = S[i]
            sq = int(s["sqrt"])
            for key, w in s["w"].items():
                up = key[0] == "u"
                lim = target_sqrt(sq, xs[key[1:]], up)
                q = quote_gross_in(p, s["block"] - 1, lim, up)
                mine = int(w[0]) + int(w[1])
                n += 1
                pc += 1
                boundary_cases += w[4] > 0
                if q != mine:
                    mism += 1
                    bad.append({"pool": p["pool"], "block": s["block"], "walk": key, "quoter": str(q), "replay": str(mine)})
        per[p["pool"]] = pc
        print(f'quoter diff {p["base"]}/{p["quote"]} {p["fee"]}: {pc} walks, running mismatches {mism}', flush=True)
    out = {"method": "QuoterV2.quoteExactOutputSingle(amount=2^200, sqrtPriceLimitX96=target) by eth_call at block-1, "
                     "vs replayed-book reference walk gross input (pre-fee input + fee). Tolerance: zero.",
           "quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e", "seed": seed, "walks": n, "mismatches": mism,
           "walks_with_word_boundary_steps": boundary_cases, "per_pool_walks": per, "mismatch_detail": bad}
    (DATA / "phase0_quoter_diff.json").write_text(json.dumps(out, indent=1) + "\n")
    assert mism == 0, f"{mism} replay walks disagree with QuoterV2"
    assert boundary_cases > 0, "differential never exercised a word-boundary step"
    return out


def main():
    pools, days = load_meta()
    oracle = UsdOracle(pools)
    all_rows, all_sat = [], []
    for p in pools:
        r, s = pool_rows(p, days, oracle)
        all_rows += r
        all_sat += s
        print(f'{p["base"]}/{p["quote"]} {p["fee"]}: {len(r)} days', flush=True)
    write_csv(DATA / "phase0_pool_days.csv", all_rows)
    write_csv(DATA / "phase0_saturation_books.csv", all_sat)
    quoter_diff(pools, days)


if __name__ == "__main__":
    main()
