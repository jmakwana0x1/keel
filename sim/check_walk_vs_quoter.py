"""Smoke differential: reference walk on an archive snapshot vs deployed QuoterV2, zero tolerance."""
import json, sys
from fractions import Fraction
from depth_reference import target_sqrt, walk
from snapshot import quote_gross_in, snapshot

pools = json.load(open("../data/phase0_universe.json"))["pools"]
block = int(sys.argv[1]) if len(sys.argv) > 1 else 26_400_000
xs = [Fraction(1, 200), Fraction(1, 100), Fraction(1, 50), Fraction(1, 20), Fraction(1, 10)]
bad = n = 0
for p in pools:
    span = 1100  # ~±11.6% in ticks
    from snapshot import multicall, calldata, words, signed
    t = signed(words(multicall([(p["pool"], calldata("slot0"))], block)[0][1])[1])
    b = snapshot(p, block, t - span - p["tick_spacing"] * 256, t + span + p["tick_spacing"] * 256)
    for up in (True, False):
        for x in xs:
            lim = target_sqrt(b.sqrt_price, x, up)
            wk, capped = walk(b, lim)
            q = quote_gross_in(p, block, lim, up)
            n += 1
            if q != wk.gross:
                bad += 1
                print("MISMATCH", p["base"], p["quote"], p["fee"], up, x, q, wk.gross)
    print(f'{p["base"]:6}/{p["quote"]:4} {p["fee"]:5} ticks={len(b.ticks):4} last steps={wk.steps} cross={wk.crossings}', flush=True)
print(f"{n} walks, {bad} mismatches")
