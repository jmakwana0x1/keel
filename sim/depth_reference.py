"""Independent tick-walk depth reference, written from the concentrated-liquidity specification
(Uniswap v3 whitepaper §6, and the published swap-step / bitmap-stepping semantics), in exact
integer arithmetic. It shares no code with the Solidity in src/.

Measured quantity (docs/ARCHITECTURE.md D0.4): the input needed to move the price from spot to a
target sqrt-price. The walk runs as an exact-output swap with an unbounded amount, so every step
reaches its step target, so its per-step input and fee equal those of an exact-input swap that
stops at the same price limit.

Zero-liquidity ranges cost nothing to cross. That is pool semantics and it is what produces
saturation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from math import isqrt

Q96 = 1 << 96
MIN_TICK, MAX_TICK = -887272, 887272
MIN_SQRT = 4295128739
MAX_SQRT = 1461446703485210103287273052203988822378723970342

# getSqrtRatioAtTick: the canonical constants, sqrt(1.0001)^(-2^i) in Q128.
_RATIOS = [
    0xfffcb933bd6fad37aa2d162d1a594001, 0xfff97272373d413259a46990580e213a, 0xfff2e50f5f656932ef12357cf3c7fdcc,
    0xffe5caca7e10e4e61c3624eaa0941cd0, 0xffcb9843d60f6159c9db58835c926644, 0xff973b41fa98c081472e6896dfb254c0,
    0xff2ea16466c96a3843ec78b326b52861, 0xfe5dee046a99a2a811c461f1969c3053, 0xfcbe86c7900a88aedcffc83b479aa3a4,
    0xf987a7253ac413176f2b074cf7815e54, 0xf3392b0822b70005940c7a398e4b70f3, 0xe7159475a2c29b7443b29c7fa6e889d9,
    0xd097f3bdfd2022b8845ad8f792aa5825, 0xa9f746462d870fdf8a65dc1f90e061e5, 0x70d869a156d2a1b890bb3df62baf32f7,
    0x31be135f97d08fd981231505542fcfa6, 0x9aa508b5b7a84e1c677de54f3e99bc9, 0x5d6af8dedb81196699c329225ee604,
    0x2216e584f5fa1ea926041bedfe98, 0x48a170391f7dc42444e8fa2,
]
_UINT256_MAX = (1 << 256) - 1


def sqrt_at_tick(tick: int) -> int:
    a = abs(tick)
    assert a <= MAX_TICK
    ratio = _RATIOS[0] if a & 1 else 1 << 128
    for i in range(1, 20):
        if a & (1 << i):
            ratio = (ratio * _RATIOS[i]) >> 128
    if tick > 0:
        ratio = _UINT256_MAX // ratio
    return (ratio >> 32) + (1 if ratio & 0xFFFFFFFF else 0)


def _div_up(a: int, b: int) -> int:
    return -(-a // b)


def amount0_delta(sa: int, sb: int, liq: int, up: bool) -> int:
    if sa > sb:
        sa, sb = sb, sa
    n1, n2 = liq << 96, sb - sa
    if up:
        return _div_up(_div_up(n1 * n2, sb), sa)
    return (n1 * n2 // sb) // sa


def amount1_delta(sa: int, sb: int, liq: int, up: bool) -> int:
    if sa > sb:
        sa, sb = sb, sa
    return _div_up(liq * (sb - sa), Q96) if up else liq * (sb - sa) // Q96


@dataclass
class Book:
    """Pool state sufficient to walk: price, current tick, active liquidity, and per-tick
    (liquidityGross, liquidityNet). A tick is initialized iff its gross is non-zero."""
    sqrt_price: int
    tick: int
    liquidity: int
    tick_spacing: int
    fee: int  # pips
    ticks: dict[int, list[int]] = field(default_factory=dict)  # tick -> [gross, net]
    _bitmap: dict[int, int] | None = None

    def bitmap(self) -> dict[int, int]:
        if self._bitmap is None:
            bm: dict[int, int] = {}
            for t, (g, _) in self.ticks.items():
                if g:
                    c = t // self.tick_spacing
                    bm[c >> 8] = bm.get(c >> 8, 0) | (1 << (c & 0xFF))
            self._bitmap = bm
        return self._bitmap

    def touch(self, t: int, dgross: int, dnet: int):
        g, n = self.ticks.get(t, [0, 0])
        g, n = g + dgross, n + dnet
        assert g >= 0, (t, g)
        if g == 0:
            assert n == 0, (t, n)
            self.ticks.pop(t, None)
        else:
            self.ticks[t] = [g, n]
        self._bitmap = None

    def next_initialized(self, tick: int, lte: bool) -> tuple[int, bool]:
        """Next initialized tick within one 256-bit bitmap word, else the word boundary."""
        ts = self.tick_spacing
        c = tick // ts  # floor division == round toward negative infinity
        bm = self.bitmap()
        if lte:
            word, bit = c >> 8, c & 0xFF
            masked = bm.get(word, 0) & ((1 << (bit + 1)) - 1)
            if masked:
                return (c - (bit - (masked.bit_length() - 1))) * ts, True
            return (c - bit) * ts, False
        c += 1
        word, bit = c >> 8, c & 0xFF
        masked = bm.get(word, 0) & ~((1 << bit) - 1) & ((1 << 256) - 1)
        if masked:
            lsb = (masked & -masked).bit_length() - 1
            return (c + (lsb - bit)) * ts, True
        return (c + (255 - bit)) * ts, False


@dataclass
class Walk:
    amount_in: int  # pre-fee input
    fee: int
    steps: int
    crossings: int  # initialized ticks crossed
    boundary_steps: int = 0  # steps ending on an uninitialized word boundary with liquidity > 0

    @property
    def gross(self) -> int:
        return self.amount_in + self.fee


def walk(book: Book, limit: int, max_steps: int | None = None) -> tuple[Walk, bool]:
    """Walk from spot to sqrt-price `limit`. Returns (walk, capped). A capped walk is a lower bound."""
    zero_for_one = limit < book.sqrt_price
    assert limit != book.sqrt_price
    assert (MIN_SQRT < limit < book.sqrt_price) if zero_for_one else (book.sqrt_price < limit < MAX_SQRT)
    sp, tick, liq = book.sqrt_price, book.tick, book.liquidity
    a_in = fee_total = steps = crossings = boundary = 0
    fpips = book.fee
    while sp != limit:
        if max_steps is not None and steps >= max_steps:
            return Walk(a_in, fee_total, steps, crossings, boundary), True
        nxt, init = book.next_initialized(tick, zero_for_one)
        nxt = max(MIN_TICK, min(MAX_TICK, nxt))
        s_next = sqrt_at_tick(nxt)
        target = limit if (s_next < limit if zero_for_one else s_next > limit) else s_next
        step_in = amount0_delta(target, sp, liq, True) if zero_for_one else amount1_delta(sp, target, liq, True)
        step_fee = _div_up(step_in * fpips, 1_000_000 - fpips)
        a_in += step_in
        fee_total += step_fee
        sp = target
        steps += 1
        if sp == s_next:
            if init:
                net = book.ticks[nxt][1]
                liq += -net if zero_for_one else net
                assert liq >= 0
                crossings += 1
            elif liq:
                boundary += 1
            tick = nxt - 1 if zero_for_one else nxt
    return Walk(a_in, fee_total, steps, crossings, boundary), False


def target_sqrt(sqrt_price: int, x: Fraction, up: bool) -> int:
    """sqrt-price of p·(1+x) (up) or p/(1+x) (down), floored to an integer Q96 value."""
    r = (1 + x) if up else 1 / (1 + x)
    return isqrt(sqrt_price * sqrt_price * r.numerator // r.denominator)


def naive(book: Book, limit: int) -> Fraction:
    """Active liquidity at spot, extended as if constant. Exact rational, pre-fee, same token as walk()."""
    sp, liq = book.sqrt_price, book.liquidity
    if limit > sp:
        return Fraction(liq * (limit - sp), Q96)
    return Fraction(liq * Q96 * (sp - limit), sp * limit)


# ---- exact-input swap (Phase 0.5: the re-centring curator's rebalancing swap) --------------------

def _mul_div_up(a: int, b: int, d: int) -> int:
    return -(-(a * b) // d)


def _next_sqrt_from_amount0_up(sp: int, liq: int, amount: int) -> int:
    """Price after adding `amount` of token0 (price falls), rounded up."""
    if amount == 0:
        return sp
    n1 = liq << 96
    product = amount * sp
    if product < (1 << 256):  # the spec's non-overflow branch
        denom = n1 + product
        if denom >= n1:
            return _mul_div_up(n1, sp, denom)
    return _div_up(n1, n1 // sp + amount)


def _next_sqrt_from_amount1_down(sp: int, liq: int, amount: int) -> int:
    """Price after adding `amount` of token1 (price rises), rounded down."""
    return sp + (amount << 96) // liq


def exact_in(book: Book, amount: int, zero_for_one: bool) -> tuple[int, int]:
    """Exact-input swap of `amount` (gross, fee included) through the book, to the protocol price bound.
    Returns (amount_out, sqrt_price_after). The book is not mutated."""
    sp, tick, liq = book.sqrt_price, book.tick, book.liquidity
    limit = MIN_SQRT + 1 if zero_for_one else MAX_SQRT - 1
    rem, out, f = amount, 0, book.fee
    while rem != 0 and sp != limit:
        nxt, init = book.next_initialized(tick, zero_for_one)
        nxt = max(MIN_TICK, min(MAX_TICK, nxt))
        s_next = sqrt_at_tick(nxt)
        target = limit if (s_next < limit if zero_for_one else s_next > limit) else s_next
        rem_less_fee = rem * (1_000_000 - f) // 1_000_000
        a_in = amount0_delta(target, sp, liq, True) if zero_for_one else amount1_delta(sp, target, liq, True)
        if rem_less_fee >= a_in:
            s_new = target
        else:
            s_new = (_next_sqrt_from_amount0_up(sp, liq, rem_less_fee) if zero_for_one
                     else _next_sqrt_from_amount1_down(sp, liq, rem_less_fee))
        if s_new != target:
            a_in = amount0_delta(s_new, sp, liq, True) if zero_for_one else amount1_delta(sp, s_new, liq, True)
        a_out = amount1_delta(s_new, sp, liq, False) if zero_for_one else amount0_delta(sp, s_new, liq, False)
        fee_amt = rem - a_in if s_new != target else _div_up(a_in * f, 1_000_000 - f)
        rem -= a_in + fee_amt
        out += a_out
        sp = s_new
        if sp == s_next:
            if init:
                net = book.ticks[nxt][1]
                liq += -net if zero_for_one else net
            tick = nxt - 1 if zero_for_one else nxt
        # a step that stops inside a range leaves rem == 0, so the loop ends and the tick is not needed
    return out, sp
