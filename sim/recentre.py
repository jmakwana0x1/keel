"""Phase 0.5 — the re-centring gap. Pre-registered in docs/ARCHITECTURE.md D0.12; this file implements that spec.

Two kinds of curator run over the same replayed tape, per pool, per band half-width x:
  P    — deploys a ±x band at the window's first hourly sample and never touches it
  R_N  — the same band, re-centred N blocks after spot leaves it (if spot is still outside then)

Re-centring withdraws the band at the current price, swaps the excess token to the new band's ratio through the
pool's actual book with exact v3 math (sim.depth_reference.exact_in, diffed against QuoterV2 at zero tolerance), and
mints a ±x band centred at the post-swap price. Fees accrue by the per-position identity f/(1-f)·L·Δ√p in range
(price-taking), are kept in kind and valued at terminal prices. Gas: 500,000 per re-centre at the day's base fee.

Output: data/phase0_recentre.csv, one row per (pool, x, curator).
"""
from __future__ import annotations

import bisect
import csv
import json
from pathlib import Path

from aggregate import UsdOracle, token_usd
from depth_reference import Q96, exact_in
from measure import EV, SAMPLE_EVERY, SPAN, iter_events, window
from rpc import call
from snapshot import calldata, multicall, signed, snapshot, words

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
XS = {"1": 0.01, "5": 0.05}
NS = [25, 300, 1800, 7200]
CAPITAL = {"blue": 1_000_000.0, "other": 100_000.0}
GAS_PER_RECENTRE = 500_000
SMALL_FRACTION = 0.01  # D0.13: band L = 1% of the pool's median hourly active liquidity


class Curator:
    def __init__(self, x: float, n: int | None):
        self.x, self.n, self.g = x, n, (1 + x) ** 0.5
        self.sa = self.sb = self.L = 0.0
        self.cash0 = self.cash1 = self.fee0 = self.fee1 = 0.0
        self.armed = None
        self.n_re = self.skipped = 0
        self.gas_usd = self.rebal_usd = 0.0

    def name(self):
        return "P" if self.n is None else f"R_{self.n}"

    def unit(self, s: float) -> tuple[float, float]:
        """Token amounts per unit L of a band centred at s (raw units)."""
        return 1 / s - 1 / (s * self.g), s - s / self.g

    def place(self, s: float, a0: float, a1: float):
        t0, t1 = self.unit(s)
        self.L = min(a0 / t0, a1 / t1)
        self.cash0, self.cash1 = a0 - self.L * t0, a1 - self.L * t1
        self.sa, self.sb = s / self.g, s * self.g

    def amounts(self, s: float) -> tuple[float, float]:
        L, sa, sb = self.L, self.sa, self.sb
        if s <= sa:
            a0, a1 = L * (1 / sa - 1 / sb), 0.0
        elif s >= sb:
            a0, a1 = 0.0, L * (sb - sa)
        else:
            a0, a1 = L * (1 / s - 1 / sb), L * (s - sa)
        return a0 + self.cash0, a1 + self.cash1

    def accrue(self, prev: float, cur: float, fr: float):
        lo, hi = max(min(prev, cur), self.sa), min(max(prev, cur), self.sb)
        if hi > lo:
            if cur > prev:
                self.fee1 += fr * self.L * (hi - lo)
            else:
                self.fee0 += fr * self.L * (1 / lo - 1 / hi)


def rebalance(book, a0: float, a1: float, cur: Curator, s: float):
    """Swap through the real book to the ratio a band centred at the post-swap price needs.
    Returns (b0, b1, s_after, zero_for_one, amount_in, amount_out) or None if the book cannot fill it."""
    t0, t1 = cur.unit(s)
    zf1 = a0 * t1 > a1 * t0  # excess token0 -> sell token0
    have = int(a0 if zf1 else a1)
    if have <= 0:
        return None

    def state(d):
        out, sp = exact_in(book, d, zf1)
        sf = sp / Q96
        u0, u1 = cur.unit(sf)
        b0, b1 = (a0 - d, a1 + out) if zf1 else (a0 + out, a1 - d)
        return b0 * u1 - b1 * u0, b0, b1, sf, out

    err_hi = state(have)[0]
    if (err_hi > 0) if zf1 else (err_hi < 0):  # cannot reach the target ratio through this book
        return None
    lo, hi = 0, have
    while hi - lo > 1:
        mid = (lo + hi) // 2
        e = state(mid)[0]
        if (e > 0) if zf1 else (e < 0):
            lo = mid
        else:
            hi = mid
    _, b0, b1, sf, out = state(hi)
    return b0, b1, sf, zf1, hi, out


def median_active_liquidity(pool: dict) -> int:
    from aggregate import jsonl
    from measure import OUT
    ls = sorted(int(s["L"]) for s in jsonl(OUT / f'{pool["pool"]}.samples.jsonl'))
    return ls[len(ls) // 2]


def run(pool: dict, oracle: UsdOracle, basefee: dict[int, int], day_starts: list[int], sizing: str) -> list[dict]:
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
    fr = pool["fee"] / (1e6 - pool["fee"])
    C_registered = CAPITAL["blue" if pool["group"] == "blue" else "other"]
    med_L = median_active_liquidity(pool)
    if sizing == "small" and med_L == 0:
        print(f'{pool["base"]}/{pool["quote"]} {pool["fee"]}: declined (median active liquidity is 0)', flush=True)
        return [{"pool": addr, "sizing": sizing, "x_pct": k, "curator": "declined", "capital_usd": "",
                 "size_ratio_at_deploy": "", "recentres": "", "skipped": "", "fees_usd": "", "inventory_usd": "",
                 "gas_usd": "", "rebalance_cost_usd": "", "total_usd": ""} for k in XS]
    s_last = list(range(b0, b_end, SAMPLE_EVERY))[-1]

    curators = [Curator(x, n) for x in XS.values() for n in [None] + NS]
    s0 = book.sqrt_price / Q96
    u0, u1 = token_usd(pool, book.sqrt_price, b0, oracle)
    C_of = {}
    for c in curators:
        t0_, t1_ = c.unit(s0)
        # registered: notional C; small: band liquidity = 1% of the pool's median active liquidity (D0.13).
        # At re-centres every curator redeploys all of its value, so L stays near the deployed size.
        L = C_registered / (t0_ * u0 + t1_ * u1) if sizing == "registered" else SMALL_FRACTION * med_L
        C_of[id(c)] = L * (t0_ * u0 + t1_ * u1)
        c.size_ratio = L / max(book.liquidity, 1)
        c.place(s0, L * t0_, L * t1_)
    prev = s0
    for e in iter_events(ev_path):
        if e[0] >= s_last:
            break
        if e[2] == "S":
            book.sqrt_price, book.liquidity, book.tick = e[5], e[6], e[7]
            cur = e[5] / Q96
            for c in curators:
                c.accrue(prev, cur, fr)
            prev = cur
            for c in curators:
                if c.n is None:
                    continue
                inside = c.sa < cur < c.sb
                if c.armed is None:
                    if not inside:
                        c.armed = e[0]
                elif inside:
                    c.armed = None
                elif e[0] >= c.armed + c.n:
                    a0, a1 = c.amounts(cur)
                    r = rebalance(book, a0, a1, c, cur)
                    if r is None:
                        c.skipped += 1
                        continue  # stays armed; retried at the next swap
                    nb0, nb1, s_after, zf1, d, out = r
                    u0, u1 = token_usd(pool, e[5], e[0], oracle)
                    c.rebal_usd += (d * u0 - out * u1) if zf1 else (d * u1 - out * u0)
                    day = bisect.bisect_right(day_starts, e[0]) - 1
                    eth_usd = oracle.eth_usd(e[0])
                    c.gas_usd += GAS_PER_RECENTRE * basefee[day] / 1e18 * eth_usd
                    c.place(s_after, nb0, nb1)
                    c.n_re += 1
                    c.armed = None
        else:
            _, _, kind, tl, tu, amt = e
            if amt:
                sgn = 1 if kind == "M" else -1
                for t, dn in ((tl, sgn * amt), (tu, -sgn * amt)):
                    if in_range(t):
                        book.touch(t, sgn * amt, dn)
                if tl <= book.tick < tu:
                    book.liquidity += sgn * amt
    uT0, uT1 = token_usd(pool, book.sqrt_price, s_last, oracle)
    sT = book.sqrt_price / Q96
    rows = []
    for c in curators:
        a0, a1 = c.amounts(sT)
        inv = a0 * uT0 + a1 * uT1
        fees = c.fee0 * uT0 + c.fee1 * uT1
        rows.append({"pool": addr, "sizing": sizing, "x_pct": [k for k, v in XS.items() if v == c.x][0],
                     "curator": c.name(), "capital_usd": f"{C_of[id(c)]:.6f}",
                     "size_ratio_at_deploy": f"{c.size_ratio:.6g}", "recentres": c.n_re, "skipped": c.skipped,
                     "fees_usd": f"{fees:.6f}", "inventory_usd": f"{inv:.6f}", "gas_usd": f"{c.gas_usd:.6f}",
                     "rebalance_cost_usd": f"{c.rebal_usd:.6f}", "total_usd": f"{inv + fees - c.gas_usd:.6f}"})
    hdr = " ".join(f'{r["curator"]}:{r["recentres"]}' for r in rows if r["x_pct"] == "5")
    print(f'{pool["base"]}/{pool["quote"]} {pool["fee"]} [{sizing}]: re-centres at ±5% {hdr}', flush=True)
    return rows


def main():
    pools = json.loads((DATA / "phase0_universe.json").read_text())["pools"]
    days = json.loads((DATA / "phase0_window.json").read_text())["day_start_blocks"]
    day_starts = [d["block"] for d in days]
    basefee = {i: int(call("eth_getBlockByNumber", [hex(b), False])["baseFeePerGas"], 16) for i, b in enumerate(day_starts)}
    oracle = UsdOracle(pools)
    out = []
    for sizing in ("registered", "small"):
        for p in pools:
            out += run(p, oracle, basefee, day_starts, sizing)
    with open(DATA / "phase0_recentre.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(out)


if __name__ == "__main__":
    main()
