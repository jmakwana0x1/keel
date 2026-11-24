"""FFI bridge: the exact real-valued contribution of a curator band, as a Fraction, for the Solidity contribution suite.

Ground truth is the real-valued quantity on the protocol's own Q96 tick prices (sqrt_at_tick is depth_reference.py's,
written from the TickMath spec and verified against QuoterV2). Clamping and the formula are written here from the
specification (CLAUDE.md), not from DepthLib:

    up   (target > anchor):  T = L · (S(min(target, upper)) − S(max(anchor, lower))) / 2^96
    down (target < anchor):  T = L · 2^96 · (S(hi) − S(lo)) / (S(hi) · S(lo)),
                             lo = max(target, lower), hi = min(anchor, upper)
    empty range or target == anchor:  T = 0

argv[1] = "c", argv[2] = "L,lower,upper,anchor,target"
    stdout: abi.encode(uint256 floor(T), bool T is an integer)
argv[1] = "s", argv[2] = "L,sqrtLower,sqrtUpper,sqrtAnchor,sqrtTarget"   (Q96 sqrt prices, same output as "c")
argv[1] = "w", argv[2] = "L,lower,upper,anchor,target,diff,stepsWithout,stepsWith"
    diff is the two-walk difference measure(book + band) − measure(book). Every step of a walk ends on a tick price or
    the target and rounds its input UP once, so walk = Σ ceil(x_i) ∈ [Σ x_i, Σ x_i + steps), and therefore
    diff − T ∈ (−stepsWithout, stepsWith).
    stdout: abi.encode(uint256 floor(T), bool integral, bool diff > T − stepsWithout, bool diff < T + stepsWith,
                       uint256 ceil(|diff − T|))
"""
import math
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from depth_reference import sqrt_at_tick  # noqa: E402

Q96 = 1 << 96


def truth(liq: int, lower: int, upper: int, anchor: int, target: int) -> Fraction:
    if target > anchor:
        lo, hi = max(anchor, lower), min(target, upper)
        if hi <= lo:
            return Fraction(0)
        return Fraction(liq * (sqrt_at_tick(hi) - sqrt_at_tick(lo)), Q96)
    if target < anchor:
        lo, hi = max(target, lower), min(anchor, upper)
        if hi <= lo:
            return Fraction(0)
        a, b = sqrt_at_tick(lo), sqrt_at_tick(hi)
        return Fraction(liq * Q96 * (b - a), a * b)
    return Fraction(0)


def truth_sqrt(liq: int, s_lower: int, s_upper: int, s_anchor: int, s_target: int) -> Fraction:
    """The same quantity on sqrt prices, for an anchor at spot (not a tick price)."""
    if s_target > s_anchor:
        lo, hi = max(s_anchor, s_lower), min(s_target, s_upper)
        return Fraction(liq * (hi - lo), Q96) if hi > lo else Fraction(0)
    if s_target < s_anchor:
        lo, hi = max(s_target, s_lower), min(s_anchor, s_upper)
        return Fraction(liq * Q96 * (hi - lo), lo * hi) if hi > lo else Fraction(0)
    return Fraction(0)


def enc(*vals) -> str:
    return "0x" + "".join(format(int(v), "064x") for v in vals)


def main(mode: str, arg: str):
    v = [int(x) for x in arg.split(",")]
    t = truth_sqrt(*v[:5]) if mode == "s" else truth(*v[:5])
    fl = math.floor(t)
    if mode in ("c", "s"):
        sys.stdout.write(enc(fl, t.denominator == 1))
    elif mode == "w":
        diff, s_without, s_with = v[5:]
        sys.stdout.write(enc(fl, t.denominator == 1, diff > t - s_without, diff < t + s_with, math.ceil(abs(diff - t))))
    else:
        raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
