"""Hand-rolled ABI encoding for the handful of calls Phase 0 needs. No web3 dependency."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from rpc import eth_call

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
SEL = {
    "getPool": "1698ee82", "slot0": "3850c7bd", "liquidity": "1a686502", "tickSpacing": "d0c93a7c",
    "fee": "ddca3f43", "token0": "0dfe1681", "token1": "d21220a7", "tickBitmap": "5339c296",
    "ticks": "f30dba93", "symbol": "95d89b41", "decimals": "313ce567", "aggregate3": "82ad56cb",
    "quoteExactOutputSingle": "bd21704a",
}
TOPIC = {
    "Swap": "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
    "Mint": "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde",
    "Burn": "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c",
}


def w(v: int) -> str:
    """One 32-byte word, two's complement for negatives."""
    return format(v % (1 << 256), "064x")


def addr(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def words(hexdata: str) -> list[int]:
    h = hexdata[2:] if hexdata.startswith("0x") else hexdata
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def signed(v: int, bits: int = 256) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def calldata(name: str, *args: str) -> str:
    return "0x" + SEL[name] + "".join(args)


def aggregate3(calls: list[tuple[str, str]], block: int) -> list[tuple[bool, str]]:
    """calls: [(target, calldata_hex)] -> [(success, returndata_hex)]."""
    n = len(calls)
    heads, tails, off = [], [], 32 * n
    for target, cd in calls:
        body = cd[2:]
        blen = len(body) // 2
        padded = body + "0" * ((64 - len(body) % 64) % 64)
        t = addr(target) + w(1) + w(0x60) + w(blen) + padded
        heads.append(w(off))
        tails.append(t)
        off += len(t) // 2
    data = "0x" + SEL["aggregate3"] + w(0x20) + w(n) + "".join(heads) + "".join(tails)
    ret = eth_call(MULTICALL3, data, block, gas=1_000_000_000)
    h = ret[2:]
    base = int(h[0:64], 16) * 2  # offset to array
    cnt = int(h[base:base + 64], 16)
    arr = base + 64
    out = []
    for i in range(cnt):
        o = arr + int(h[arr + 64 * i: arr + 64 * i + 64], 16) * 2
        ok = int(h[o:o + 64], 16) == 1
        bo = o + int(h[o + 64:o + 128], 16) * 2
        bl = int(h[bo:bo + 64], 16)
        out.append((ok, "0x" + h[bo + 64: bo + 64 + bl * 2]))
    return out


def multicall(calls: list[tuple[str, str]], block: int, chunk: int = 400, workers: int = 4) -> list[tuple[bool, str]]:
    parts = [calls[i:i + chunk] for i in range(0, len(calls), chunk)]
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(lambda p: aggregate3(p, block), parts))
    return [r for part in res for r in part]


def decode_string(hexdata: str) -> str:
    h = hexdata[2:]
    if len(h) == 64:  # bytes32 symbol (e.g. MKR)
        return bytes.fromhex(h).rstrip(b"\0").decode(errors="replace")
    ln = int(h[64:128], 16)
    return bytes.fromhex(h[128:128 + 2 * ln]).decode(errors="replace")
