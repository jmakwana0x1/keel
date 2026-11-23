"""Resolve the Phase 0 pool universe on-chain. Candidate tokens are listed by address; every
address is checked against its on-chain symbol, and pools come from the v3 factory, never typed in."""
from __future__ import annotations

import json
from pathlib import Path

from abi import addr, calldata, decode_string, multicall, w, words

FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
REF_BLOCK = 26_470_000  # resolution block, inside the window's last day
OUT = Path(__file__).resolve().parent.parent / "data" / "phase0_universe.json"

QUOTES = {"WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
          "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7"}
BLUE = {"WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F"}
OTHER = {
    "LINK": "0x514910771AF9Ca656af840dff83E8264EcF986CA", "UNI": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
    "AAVE": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9", "MKR": "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2",
    "LDO": "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32", "PEPE": "0x6982508145454Ce325dDbE47a25d4ec3d2311933",
    "ENA": "0x57e114B691Db790C35207b2e685D4A43181e6061", "ONDO": "0xfAbA6f8e4a5E8Ab82F62fe7C39859FA577269BE3",
    "SHIB": "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE", "MOG": "0xaaeE1A9723aaDB7afA2810263653A34bA2C21C7a",
    "SPX": "0xE0f63A424a4439cBE457D80E4f4b51aD25b2c56C", "TURBO": "0xA35923162C49cF95e6BF26623385eb431ad920D3",
    "FLOKI": "0xcf0C122c6b73ff809C693DB761e7BaeBe62b6a2E", "APE": "0x4d224452801ACEd8B2F0aebE155379bb5D594381",
    "ENS": "0xC18360217D8F7Ab5e7c516566761Ea12Ce7F9D72", "CRV": "0xD533a949740bb3306d119CC777fa900bA034cd52",
    "SNX": "0xC011a73ee8576Fb46F5E1c5751cA3B9Fe0af2a6F", "COMP": "0xc00e94Cb662C3520282E6f5717214004A7f26888",
    "1INCH": "0x111111111117dC0aa78b770fA6A738034120C302", "RPL": "0xD33526068D116cE69F19A9ee46F0bd304F21A51f",
    "GRT": "0xc944E90C64B2c07662A292be6244BDf05Cda44a7", "POL": "0x455e53CBB86018Ac2B8092FdCd39d8444aFFC3F6",
    "FXS": "0x3432B6A60D23Ca0dFCa7761B7ab56459D9C964D0", "SAND": "0x3845badAde8e6dFF049820680d1F14bD3903a5d0",
    "MANA": "0x0F5D2fB29fb7d3CFeE444a200298f468908cC942", "BAL": "0xba100000625a3754423978a60c9317c58a424e3D",
    "YFI": "0x0bc529c00C6401aEF6D220BE8C6Ea1667F6Ad93e", "SUSHI": "0x6B3595068778DD592e39A122f4f5a5cF09C90fE2",
}
FEES = [100, 500, 3000, 10000]
BALANCE_OF = "70a08231"


def main():
    tokens = {**QUOTES, **BLUE, **OTHER}
    meta = multicall([(a, calldata(f)) for a in tokens.values() for f in ("symbol", "decimals")], REF_BLOCK)
    info = {}
    for i, (sym, a) in enumerate(tokens.items()):
        (ok1, s), (ok2, d) = meta[2 * i], meta[2 * i + 1]
        onchain = decode_string(s) if ok1 and len(s) > 2 else None
        if not ok2 or onchain is None or onchain.upper().replace("SPX6900", "SPX") != sym.upper():
            print(f"  drop {sym}: on-chain symbol {onchain!r}")
            continue
        info[sym] = {"address": a.lower(), "decimals": words(d)[0]}

    pairs = []  # (base, quote, cls_hint)
    for b in ["WBTC", "cbBTC", "DAI"]:
        pairs += [(b, "WETH", "blue"), (b, "USDC", "blue")]
    pairs += [("WETH", "USDC", "blue"), ("WETH", "USDT", "blue")]
    pairs += [(t, q, "other") for t in OTHER for q in ("WETH", "USDC")]
    pairs = [p for p in pairs if p[0] in info and p[1] in info and not (p[0] == "DAI" and p[1] == "USDC")]

    calls, idx = [], []
    for base, quote, cls in pairs:
        a, b = sorted([info[base]["address"], info[quote]["address"]])
        for f in FEES:
            calls.append((FACTORY, calldata("getPool", addr(a), addr(b), w(f))))
            idx.append((base, quote, cls, f))
    res = multicall(calls, REF_BLOCK)
    found = [(i, "0x" + d[-40:]) for i, (ok, d) in zip(idx, res) if ok and int(d, 16) != 0]
    # quote-token balance held by each pool = TVL proxy for picking pools
    bal = multicall([(info[i[1]]["address"], "0x" + BALANCE_OF + addr(p)) for i, p in found], REF_BLOCK)
    rows = []
    for (i, p), (ok, d) in zip(found, bal):
        base, quote, cls, fee = i
        q = int(d, 16) / 10 ** info[quote]["decimals"]
        rows.append({"base": base, "quote": quote, "fee": fee, "pool": p, "quote_balance": q, "group": cls})
    eth_usd = 3000.0  # only used to rank pools of different quote tokens against each other
    for r in rows:
        r["tvl_proxy_usd"] = r["quote_balance"] * (eth_usd if r["quote"] == "WETH" else 1.0)

    chosen = []
    # blue-chip: every pool with a meaningful book; mid/long candidates: the deepest pool per base token
    for r in rows:
        if r["group"] == "blue" and r["tvl_proxy_usd"] > 2e6:
            chosen.append(r)
    best = {}
    for r in rows:
        if r["group"] == "other" and r["tvl_proxy_usd"] > 5e4:
            if r["base"] not in best or r["tvl_proxy_usd"] > best[r["base"]]["tvl_proxy_usd"]:
                best[r["base"]] = r
    chosen += list(best.values())

    # pool metadata straight from the pool
    pm = multicall([(r["pool"], calldata(f)) for r in chosen for f in ("token0", "token1", "fee", "tickSpacing")], REF_BLOCK)
    out = []
    for k, r in enumerate(chosen):
        t0, t1, fee, ts = ("0x" + pm[4 * k][1][-40:], "0x" + pm[4 * k + 1][1][-40:], words(pm[4 * k + 2][1])[0], words(pm[4 * k + 3][1])[0])
        sym = {v["address"]: s for s, v in info.items()}
        assert fee == r["fee"]
        out.append({"pool": r["pool"], "token0": sym[t0], "token1": sym[t1], "token0_address": t0, "token1_address": t1,
                    "decimals0": info[sym[t0]]["decimals"], "decimals1": info[sym[t1]]["decimals"],
                    "fee": fee, "tick_spacing": ts, "base": r["base"], "quote": r["quote"],
                    "group": r["group"], "quote_balance_at_ref": round(r["quote_balance"], 2)})
    out.sort(key=lambda r: (r["group"], r["base"], r["fee"]))
    OUT.write_text(json.dumps({"ref_block": REF_BLOCK, "factory": FACTORY, "pools": out}, indent=1) + "\n")
    for r in out:
        print(f'{r["group"]:5} {r["base"]:6}/{r["quote"]:4} {r["fee"]:5} ts={r["tick_spacing"]:3} {r["pool"]} qbal={r["quote_balance_at_ref"]:,.0f}')
    print(len(out), "pools")


if __name__ == "__main__":
    main()
