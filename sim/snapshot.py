"""Archive snapshot of a v3 pool's book, and the QuoterV2 oracle used for the zero-tolerance diff."""
from __future__ import annotations

from abi import addr, calldata, multicall, signed, w, words
from depth_reference import Book
from rpc import RpcError, eth_call

QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"


def snapshot(pool: dict, block: int, tick_lo: int, tick_hi: int) -> Book:
    """State as of the end of `block`. Ticks are read for every initialized tick in [tick_lo, tick_hi]
    (rounded out to whole bitmap words)."""
    p, ts = pool["pool"], pool["tick_spacing"]
    (ok0, s0), (ok1, l1) = multicall([(p, calldata("slot0")), (p, calldata("liquidity"))], block)
    sw = words(s0)
    wlo, whi = (tick_lo // ts) >> 8, (tick_hi // ts) >> 8
    bm = multicall([(p, calldata("tickBitmap", w(i))) for i in range(wlo, whi + 1)], block)
    inited = []
    for wi, (ok, d) in zip(range(wlo, whi + 1), bm):
        v = int(d, 16)
        while v:
            bit = (v & -v).bit_length() - 1
            inited.append(((wi << 8) + bit) * ts)
            v &= v - 1
    td = multicall([(p, calldata("ticks", w(t))) for t in inited], block, chunk=300)
    ticks = {}
    for t, (ok, d) in zip(inited, td):
        x = words(d)
        ticks[t] = [x[0], signed(x[1])]
        assert x[0] > 0 and x[7] == 1, (p, t)
    b = Book(sqrt_price=sw[0], tick=signed(sw[1]), liquidity=words(l1)[0], tick_spacing=ts, fee=pool["fee"], ticks=ticks)
    b.word_range = (wlo, whi)
    return b


def quote_gross_in(pool: dict, block: int, limit: int, up: bool) -> int | None:
    """Gross input (fee included) the deployed pool charges to move spot to `limit`, by eth_call of
    QuoterV2.quoteExactOutputSingle with an unbounded amount, executed on the state at end of `block`."""
    tin, tout = (pool["token1_address"], pool["token0_address"]) if up else (pool["token0_address"], pool["token1_address"])
    data = calldata("quoteExactOutputSingle", addr(tin), addr(tout), w(1 << 200), w(pool["fee"]), w(limit))
    try:
        return words(eth_call(QUOTER_V2, data, block, gas=200_000_000))[0]
    except RpcError as e:
        # QuoterV2's callback requires amount0Delta > 0 || amount1Delta > 0: a swap that moves price only
        # through zero-liquidity ranges is unquotable. The oracle declines; callers must not treat it as 0.
        if "Unexpected error" in str(e):
            return None
        raise
