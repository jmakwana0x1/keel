"""FFI bridge: the Solidity differential suites hand a book to the spec-derived reference walk (depth_reference.py,
verified against QuoterV2 on 2,500 real walks) and get the walk back ABI-encoded.

argv[1]: "sqrt,tick,L,spacing,fee,target" then ";tick:gross:net" per initialized tick
stdout:  0x + abi.encode(uint256 depth, uint256 fee, uint256 steps, uint256 crossings)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from depth_reference import Book, walk  # noqa: E402


def main(arg: str):
    head, *ticks = arg.split(";")
    sqrt, tick, liq, spacing, fee, target = (int(v) for v in head.split(","))
    book = Book(sqrt_price=sqrt, tick=tick, liquidity=liq, tick_spacing=spacing, fee=fee,
                ticks={int(t): [int(g), int(n)] for t, g, n in (x.split(":") for x in ticks if x)})
    w, _ = walk(book, target)
    sys.stdout.write("0x" + "".join(format(v, "064x") for v in (w.amount_in, w.fee, w.steps, w.crossings)))


if __name__ == "__main__":
    main(sys.argv[1])
