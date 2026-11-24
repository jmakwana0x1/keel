"""Phase 0.5 accounting audit: at every small-run re-centre, value(before, tape) − rebalance cost must equal
value(after, tape) to within rounding + impact on a 1%-of-pool band. Observed: 8,157 re-centres, worst 8.7e-5.
Dropping the rebalance cost from the audit (a planted leak) raises it to 1.9e-3 on UNI/WETH alone."""
import json
import recentre as rc
from aggregate import UsdOracle, token_usd
from depth_reference import Q96
from rpc import call

TOL = 5e-4
pools = json.load(open("../data/phase0_universe.json"))["pools"]
ds = [d["block"] for d in json.load(open("../data/phase0_window.json"))["day_start_blocks"]]
bf = {i: int(call("eth_getBlockByNumber", [hex(b), False])["baseFeePerGas"], 16) for i, b in enumerate(ds)}
o, worst, n, ctx = UsdOracle(pools), [0.0], [0], {}
op, orb = rc.Curator.place, rc.rebalance


def place(self, s, a0, a1):
    op(self, s, a0, a1)
    if "check" in ctx:
        s_t, before, cost, u0, u1 = ctx.pop("check")
        b0, b1 = self.amounts(s_t)
        worst[0] = max(worst[0], abs(before - cost - (b0 * u0 + b1 * u1)) / before)
        n[0] += 1


def reb(book, a0, a1, cur, s):
    r = orb(book, a0, a1, cur, s)
    if r:
        u0, u1 = token_usd(ctx["pool"], int(s * Q96), ds[45], o)
        _, _, _, zf1, d, out = r
        ctx["check"] = (s, a0 * u0 + a1 * u1, (d * u0 - out * u1) if zf1 else (d * u1 - out * u0), u0, u1)
    return r


rc.Curator.place, rc.rebalance = place, reb
for p in pools:
    ctx["pool"] = p
    rc.run(p, o, bf, ds, "small")
print(f"{n[0]} re-centres, worst relative leak {worst[0]:.2e}")
assert worst[0] < TOL, "value leaks at re-centre"
