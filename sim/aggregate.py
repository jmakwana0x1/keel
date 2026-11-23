"""Reduce the per-block replay (.cache/measure/) to the committed Phase 0 inputs in data/:

  phase0_pool_days.csv        one row per pool-day: volume, depth, naive depth, sigma, price, markout, LVR, fees
  phase0_saturation_books.csv one row per (pool, day-start sample, x, side): true vs naive depth
  phase0_quoter_diff.json     the zero-tolerance differential of the replayed books against QuoterV2,
                              and the planted mutations run against the same books

These are measured from chain data. sim/model.py turns them into results without the network.
"""
from __future__ import annotations

import bisect
import csv
import json
import math
from fractions import Fraction
from pathlib import Path

import numpy as np

import depth_reference as dr
from depth_reference import Q96

ROOT = Path(__file__).resolve().parent.parent
MEAS = ROOT / ".cache" / "measure"
DATA = ROOT / "data"
XLAB = ["0.5", "1", "2", "5", "10"]
XS = dict(zip(XLAB, [Fraction(1, 200), Fraction(1, 100), Fraction(1, 50), Fraction(1, 20), Fraction(1, 10)]))
HORIZONS = [1, 25, 300]
SAMPLE_EVERY = 300


def load_meta():
    pools = json.loads((DATA / "phase0_universe.json").read_text())["pools"]
    days = json.loads((DATA / "phase0_window.json").read_text())["day_start_blocks"]
    return pools, days


def jsonl(path):
    with open(path) as f:
        for line in f:
            yield json.loads(line)


def raw_price(sqrt: int) -> float:
    """token1 raw units per token0 raw unit."""
    return (sqrt / Q96) ** 2


def frac(s: str) -> Fraction:
    n, d = s.split("/")
    return Fraction(int(n), int(d))


class UsdOracle:
    """ETH/USD at each hourly sample, from the replayed WETH/USDC 0.05% book."""

    def __init__(self, pools):
        p = next(q for q in pools if q["fee"] == 500 and {q["token0"], q["token1"]} == {"USDC", "WETH"})
        self.blocks, self.eth = [], []
        for s in jsonl(MEAS / f'{p["pool"]}.samples.jsonl'):
            pr = raw_price(int(s["sqrt"])) * 10 ** (p["decimals0"] - p["decimals1"])  # token1 per token0, human
            self.blocks.append(s["block"])
            self.eth.append(pr if p["token0"] == "WETH" else 1 / pr)

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


def pool_rows(pool, days, oracle):
    starts = [d["block"] for d in days]
    end_block = starts[-1]
    nd = len(days) - 1
    day_of = lambda b: bisect.bisect_right(starts, b) - 1
    acc = [dict(n=0, d1=0.0, d5=0.0, n1=0.0, n5=0.0, base=0.0, r2=0.0, nr=0, lvr_terms=0.0, vol=0.0, swaps=0,
                fees=0.0, **{f"loss_h{h}": 0.0 for h in HORIZONS}) for _ in range(nd)]
    sat = []
    s_blocks, s_u0, s_u1 = [], [], []
    prev_lp, seen_days = None, set()
    for s in jsonl(MEAS / f'{pool["pool"]}.samples.jsonl'):
        b, sq, L = s["block"], int(s["sqrt"]), int(s["L"])
        di = day_of(b)
        if not 0 <= di < nd:
            continue
        u0, u1 = token_usd(pool, sq, b, oracle)
        s_blocks.append(b); s_u0.append(u0); s_u1.append(u1)
        a = acc[di]
        a["n"] += 1
        for lab, kd, kn in (("1", "d1", "n1"), ("5", "d5", "n5")):
            up, dn = s["w"]["u" + lab], s["w"]["d" + lab]
            a[kd] += (int(up[0]) * u1 + int(dn[0]) * u0) / 2
            a[kn] += (float(frac(up[5])) * u1 + float(frac(dn[5])) * u0) / 2
        a["base"] += (u0 * 10 ** pool["decimals0"]) if pool["base"] == pool["token0"] else (u1 * 10 ** pool["decimals1"])
        lp = math.log(raw_price(sq))
        if prev_lp is not None:
            a["r2"] += (lp - prev_lp) ** 2
            a["nr"] += 1
        prev_lp = lp
        # LVR over the following sample interval, scaled by the day's realised variance below
        a["lvr_terms"] += L * (sq / Q96) / 4 * SAMPLE_EVERY * u1
        if di not in seen_days:  # first sample of each day goes into the saturation table
            seen_days.add(di)
            for side in ("u", "d"):
                for lab in XLAB:
                    w = s["w"][side + lab]
                    true, nv = int(w[0]), frac(w[5])
                    usd = u1 if side == "u" else u0
                    sat.append({"pool": pool["pool"], "date": days[di]["date"], "block": b, "x_pct": lab,
                                "side": "up" if side == "u" else "down", "true_raw": w[0],
                                "naive_raw": f"{float(nv):.12e}", "true_usd": f"{true * usd:.2f}",
                                "naive_usd": f"{float(nv) * usd:.2f}",
                                "ratio": (f"{float(nv / true):.6f}" if true else ""),  # exact rational, then rounded
                                "steps": w[2], "crossings": w[3]})
    for di, a in enumerate(acc):
        var_blk = a["r2"] / (starts[di + 1] - starts[di]) if a["nr"] else 0.0
        a["sigma_day"] = math.sqrt(a["r2"])
        a["lvr"] = var_blk * a["lvr_terms"]

    # swaps: volume, fees, markout — numpy arrays, a few MB even for the busiest pool
    rows = list(jsonl(MEAS / f'{pool["pool"]}.swaps.jsonl'))
    blk = np.array([r[0] for r in rows], dtype=np.int64)
    a0 = np.array([float(r[1]) for r in rows])
    a1 = np.array([float(r[2]) for r in rows])
    px = np.array([raw_price(int(r[3])) for r in rows])
    del rows
    fee = pool["fee"] / 1e6
    f0, f1 = np.where(a0 > 0, a0 * fee, 0.0), np.where(a1 > 0, a1 * fee, 0.0)
    si = np.maximum(np.searchsorted(np.array(s_blocks), blk, side="right") - 1, 0)
    u0, u1 = np.array(s_u0)[si], np.array(s_u1)[si]
    di = np.searchsorted(np.array(starts), blk, side="right") - 1
    ok = (di >= 0) & (di < nd)
    quote_is_0 = pool["quote"] == pool["token0"]
    vol = np.abs(a0) * u0 if quote_is_0 else np.abs(a1) * u1
    fees = f0 * u0 + f1 * u1
    for d in np.unique(di[ok]):
        m = di == d
        acc[d]["swaps"] += int(m.sum())
        acc[d]["vol"] += float(vol[m].sum())
        acc[d]["fees"] += float(fees[m].sum())
    for h in HORIZONS:
        j = np.searchsorted(blk, blk + h, side="right") - 1
        valid = ok & (blk + h < end_block)
        loss = -((a0 - f0) * px[j] + (a1 - f1)) * u1
        for d in np.unique(di[valid]):
            acc[d][f"loss_h{h}"] += float(loss[valid & (di == d)].sum())
    out = []
    for d, a in enumerate(acc):
        if a["n"] == 0:
            continue
        out.append({"pool": pool["pool"], "date": days[d]["date"], "samples": a["n"], "swaps": a["swaps"],
                    "volume_usd": f'{a["vol"]:.2f}', "depth1_usd": f'{a["d1"] / a["n"]:.2f}',
                    "depth5_usd": f'{a["d5"] / a["n"]:.2f}', "naive1_usd": f'{a["n1"] / a["n"]:.2f}',
                    "naive5_usd": f'{a["n5"] / a["n"]:.2f}', "sigma_day": f'{a["sigma_day"]:.6e}',
                    "base_usd": f'{a["base"] / a["n"]:.6e}', "fees_usd": f'{a["fees"]:.2f}',
                    "lvr_usd": f'{a["lvr"]:.2f}', **{f"loss_h{h}_usd": f'{a[f"loss_h{h}"]:.2f}' for h in HORIZONS}})
    return out, sat


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
        wr.writeheader()
        wr.writerows(rows)


# ---- the differential, and the mutations run against the same books ------------------------------

_ORIG = {"a0": dr.amount0_delta, "a1": dr.amount1_delta, "next": dr.Book.next_initialized}


def _skip_word_boundaries(self, tick, lte):
    """Planted bug: treat an uninitialized word boundary as if it were not a step."""
    t = tick
    while True:
        n, init = _ORIG["next"](self, t, lte)
        if init or abs(n) >= dr.MAX_TICK:
            return n, init
        t = n - 1 if lte else n


MUTATIONS = {
    "amount1 rounds down": {"a1": lambda a, b, l, up: _ORIG["a1"](a, b, l, False)},
    "amount0 rounds down": {"a0": lambda a, b, l, up: _ORIG["a0"](a, b, l, False)},
    "skip word-boundary steps": {"next": _skip_word_boundaries},
}


def _apply(m: dict):
    dr.amount0_delta = m.get("a0", _ORIG["a0"])
    dr.amount1_delta = m.get("a1", _ORIG["a1"])
    dr.Book.next_initialized = m.get("next", _ORIG["next"])


def quoter_diff(pools):
    from snapshot import quote_gross_in
    walks = mism = mism_record = declined = 0
    bad, per, why_counts = [], {}, {}
    boundary_walks = 0
    mut = {k: {"failing": 0, "failing_boundary_walks": 0, "still_passing_up": 0, "still_passing_down": 0}
           for k in MUTATIONS}
    for p in pools:
        recorded = {}
        for s in jsonl(MEAS / f'{p["pool"]}.samples.jsonl'):
            recorded[s["i"]] = s
        pc = 0
        for bk in jsonl(MEAS / f'{p["pool"]}.books.jsonl'):
            why_counts[bk["why"]] = why_counts.get(bk["why"], 0) + 1
            book = dr.Book(sqrt_price=int(bk["sqrt"]), tick=bk["tick"], liquidity=int(bk["L"]),
                           tick_spacing=p["tick_spacing"], fee=p["fee"],
                           ticks={t: [int(g), int(n)] for t, g, n in bk["ticks"]})
            rec = recorded[bk["idx"]]
            for key, w in rec["w"].items():
                up = key[0] == "u"
                lim = dr.target_sqrt(book.sqrt_price, XS[key[1:]], up)
                q = quote_gross_in(p, bk["block"] - 1, lim, up)
                _apply({})
                mine = dr.walk(book, lim)[0].gross
                rec_gross = int(w[0]) + int(w[1])
                walks += 1
                pc += 1
                is_boundary = w[4] > 0
                if q is None:
                    # oracle declined: only legitimate if the walk crossed zero liquidity only, i.e. input 0
                    if mine == 0 and rec_gross == 0:
                        declined += 1
                    else:
                        mism += 1
                        bad.append({"pool": p["pool"], "block": bk["block"], "walk": key, "quoter": "declined",
                                    "book_walk": str(mine), "recorded": str(rec_gross)})
                    continue
                boundary_walks += is_boundary
                if q != mine or q != rec_gross:
                    mism += 1
                    mism_record += q != rec_gross
                    bad.append({"pool": p["pool"], "block": bk["block"], "walk": key, "quoter": str(q),
                                "book_walk": str(mine), "recorded": str(rec_gross)})
                for name, m in MUTATIONS.items():
                    _apply(m)
                    g = dr.walk(book, lim)[0].gross
                    if g != q:
                        mut[name]["failing"] += 1
                        mut[name]["failing_boundary_walks"] += is_boundary
                    else:
                        mut[name]["still_passing_up" if up else "still_passing_down"] += 1
                _apply({})
        per[p["pool"]] = pc
        print(f'quoter diff {p["base"]}/{p["quote"]} {p["fee"]}: {pc} walks, running mismatches {mism}', flush=True)
    out = {"method": "QuoterV2.quoteExactOutputSingle(amount=2^200, sqrtPriceLimitX96=target) by eth_call at block-1, "
                     "vs the replayed book's reference walk (gross = pre-fee input + fee), and vs the gross recorded "
                     "during the replay. Tolerance: zero.",
           "quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
           "sample": "per pool: 8 seeded random hourly samples + first and last sample of the window + "
                     "up to 4 seeded reservoir-sampled books whose walks include word-boundary steps",
           "books_by_reason": why_counts, "walks": walks, "mismatches": mism,
           "quoter_declined_zero_depth": declined,
           "declined_note": "QuoterV2 reverts when a swap moves price only through zero liquidity (both deltas 0). "
                            "Counted as declined only where the replayed walk is also exactly 0; otherwise a mismatch. "
                            "Declined walks are excluded from the mutation counts.",
           "mismatches_vs_recorded": mism_record, "walks_with_word_boundary_steps": boundary_walks,
           "mutations_on_same_sample": mut, "per_pool_walks": per, "mismatch_detail": bad}
    (DATA / "phase0_quoter_diff.json").write_text(json.dumps(out, indent=1) + "\n")
    assert mism == 0, f"{mism} replay walks disagree with QuoterV2"
    assert boundary_walks > 0, "differential never exercised a word-boundary step"
    for name, r in mut.items():
        assert r["failing"] > 0, f"differential cannot see planted mutation: {name}"
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
    quoter_diff(pools)


if __name__ == "__main__":
    main()
