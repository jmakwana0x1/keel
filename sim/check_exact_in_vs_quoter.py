"""exact_in() vs deployed QuoterV2.quoteExactInputSingle, zero tolerance, on the Phase 0 differential books."""
import json, random
from pathlib import Path
import depth_reference as dr
from abi import addr, calldata, w, words
from rpc import RpcError, eth_call
from snapshot import QUOTER_V2

ROOT = Path(__file__).resolve().parent.parent
pools = json.loads((ROOT / "data/phase0_universe.json").read_text())["pools"]
rng = random.Random(5)
n = bad = declined = crossing = 0
for p in pools:
    books = [json.loads(l) for l in open(ROOT / f".cache/measure/{p['pool']}.books.jsonl")]
    for bk in rng.sample(books, 3):
        book = dr.Book(sqrt_price=int(bk["sqrt"]), tick=bk["tick"], liquidity=int(bk["L"]), tick_spacing=p["tick_spacing"],
                       fee=p["fee"], ticks={t: [int(g), int(nn)] for t, g, nn in bk["ticks"]})
        for zf1 in (True, False):
            ref = dr.walk(book, dr.target_sqrt(book.sqrt_price, dr.Fraction(1, 100), not zf1))[0].gross
            if ref == 0:
                continue
            for k in (0.01, 0.3, 1.0, 3.0):
                amt = max(int(ref * k), 1)
                tin, tout = (p["token0_address"], p["token1_address"]) if zf1 else (p["token1_address"], p["token0_address"])
                try:
                    q = words(eth_call(QUOTER_V2, calldata("quoteExactInputSingle", addr(tin), addr(tout), w(amt), w(p["fee"]), w(0)),
                                       bk["block"] - 1, gas=200_000_000))
                except RpcError:
                    declined += 1
                    continue
                out, sp = dr.exact_in(book, amt, zf1)
                n += 1
                crossing += q[2] > 0
                if (out, sp) != (q[0], q[1]):
                    bad += 1
                    print("MISMATCH", p["base"], p["quote"], zf1, amt, out, q[0], sp, q[1])
print(f"{n} exact-input swaps vs QuoterV2: {bad} mismatches (amountOut and sqrtPriceAfter), "
      f"{crossing} crossed initialized ticks, {declined} declined by the quoter")
