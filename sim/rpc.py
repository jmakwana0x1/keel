"""Minimal JSON-RPC client with endpoint rotation, retries and a content-addressed disk cache.

Historical chain data is immutable, so every response for a request that names an explicit
past block is cached under .cache/rpc/ and replayed byte-identically on later runs. That is what
makes the measured artifacts in data/ regenerable without the network once fetched.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "rpc"

ARCHIVE = ["https://eth.drpc.org", "https://eth-mainnet.public.blastapi.io"]
LOGS = ["https://mainnet.gateway.tenderly.co", "https://gateway.tenderly.co/public/mainnet"]  # 5000-block spans
LOGS_MID = ["https://rpc.mevblocker.io", "https://rpc.flashbots.net"]  # 1000-block spans
LOGS_FALLBACK = "https://eth.drpc.org"  # 100-block getLogs limit on the free plan

UA = {"Content-Type": "application/json", "User-Agent": "keel-sim/0.1"}


class RpcError(Exception):
    pass


def _post(url: str, payload: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": {"code": e.code, "message": e.read()[:300].decode(errors="replace")}}
    except Exception as e:  # network-level
        return {"error": {"code": -1, "message": str(e)[:300]}}


def call(method: str, params: list, urls: list[str] | None = None, cache: bool = True, tries: int = 8):
    urls = urls or ARCHIVE
    key = hashlib.sha256(json.dumps([method, params], sort_keys=True).encode()).hexdigest()
    path = CACHE / key[:2] / f"{key}.json"
    if cache and path.exists():
        return json.loads(path.read_text())
    last = None
    for i in range(tries):
        url = urls[i % len(urls)]
        r = _post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "result" in r and r["result"] is not None:
            if cache:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(r["result"]))
                os.replace(tmp, path)
            return r["result"]
        last = r.get("error")
        # execution reverts are deterministic; do not retry them
        if isinstance(last, dict) and "revert" in str(last.get("message", "")).lower():
            raise RpcError(f"{method}: {last}")
        time.sleep(min(2 ** i * 0.5, 20))
    raise RpcError(f"{method} failed after {tries} tries: {last}")


def eth_call(to: str, data: str, block: int, gas: int | None = None) -> str:
    tx = {"to": to, "data": data}
    if gas:
        tx["gas"] = hex(gas)
    return call("eth_call", [tx, hex(block)])


def block(num: int) -> dict:
    b = call("eth_getBlockByNumber", [hex(num), False])
    return {"number": int(b["number"], 16), "timestamp": int(b["timestamp"], 16)}


def get_logs(addresses: list[str], topics0: list[str], frm: int, to: int, step: int = 2000) -> list[dict]:
    """Logs for [frm, to]. Tenderly gateways first (2000-block chunks); on failure the chunk is split
    into 1000-block spans (mevblocker/flashbots), then 100-block spans (drpc)."""
    def q(urls, a, b, tries):
        return call("eth_getLogs", [{"fromBlock": hex(a), "toBlock": hex(b), "address": addresses, "topics": [topics0]}],
                    urls=urls, tries=tries)
    out = []
    b = frm
    while b <= to:
        e = min(b + step - 1, to)
        try:
            out += q(LOGS, b, e, 6)
        except RpcError:
            for mb in range(b, e + 1, 1000):
                me = min(mb + 999, e)
                try:
                    out += q(LOGS_MID, mb, me, 6)
                except RpcError:
                    for sb in range(mb, me + 1, 100):
                        out += q([LOGS_FALLBACK], sb, min(sb + 99, me), 10)
        b = e + 1
    return out
