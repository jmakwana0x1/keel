"""Plant single bugs in the reference walk and confirm the QuoterV2 diff catches each one.
Records which walks keep passing (localisation)."""
import json
from fractions import Fraction
import depth_reference as dr
from snapshot import quote_gross_in, snapshot, multicall, calldata, words, signed

BLOCK = 26_400_000
pools = [p for p in json.load(open("../data/phase0_universe.json"))["pools"] if p["base"] in ("WETH", "PEPE", "APE")]
xs = [Fraction(1, 200), Fraction(1, 20)]
cases = []
for p in pools:
    t = signed(words(multicall([(p["pool"], calldata("slot0"))], BLOCK)[0][1])[1])
    b = dr.snapshot_book = snapshot(p, BLOCK, t - 1100 - p["tick_spacing"] * 256, t + 1100 + p["tick_spacing"] * 256)
    for up in (True, False):
        for x in xs:
            lim = dr.target_sqrt(b.sqrt_price, x, up)
            cases.append((p, b, lim, up, x, quote_gross_in(p, BLOCK, lim, up)))

orig_a0, orig_a1, orig_next = dr.amount0_delta, dr.amount1_delta, dr.Book.next_initialized
def skip_word_boundaries(self, tick, lte):   # treat word boundaries as if they were not steps
    t = tick
    for _ in range(4000):
        n, init = orig_next(self, t, lte)
        if init or abs(n) >= dr.MAX_TICK: return n, init
        t = n - 1 if lte else n
    return n, init
mutations = {
    "none": {},
    "amount1 rounds down": {"amount1_delta": lambda a, b, l, up: orig_a1(a, b, l, False)},
    "amount0 rounds down": {"amount0_delta": lambda a, b, l, up: orig_a0(a, b, l, False)},
    "skip word-boundary steps": {"next": skip_word_boundaries},
}
for name, m in mutations.items():
    dr.amount0_delta = m.get("amount0_delta", orig_a0); dr.amount1_delta = m.get("amount1_delta", orig_a1)
    dr.Book.next_initialized = m.get("next", orig_next)
    fails = {}
    for p, b, lim, up, x, q in cases:
        k = f'{p["base"]}/{p["quote"]} {p["fee"]} {"up" if up else "down"}'
        fails.setdefault(k, 0)
        if dr.walk(b, lim)[0].gross != q: fails[k] += 1
    tot = sum(fails.values())
    passing = [k for k, v in fails.items() if v == 0]
    print(f"{name:26} failing {tot}/{len(cases)}; still passing: {passing}")
