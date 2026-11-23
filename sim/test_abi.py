import sys; sys.path.insert(0, "sim")
from abi import *
B = 24_000_000
POOL = "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640"
r = multicall([(POOL, calldata("slot0")), (POOL, calldata("liquidity")), (POOL, calldata("token0")), ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", calldata("symbol"))], B)
for ok, d in r: print(ok, d[:80])
print(decode_string(r[3][1]))
