# KEEL — architecture decisions

Each decision records the alternative that was rejected and why. Newest last.

---

## D0.1 — Phase 0 measures Uniswap v3 pools on Ethereum mainnet (2026-11-23)

**Decision.** The Phase 0 economics (ε, `k_arb`, saturation) are measured on Uniswap **v3** pools on Ethereum mainnet, over the 90 days ending 2026-11-23 00:00 UTC.

**Rejected: v4 pools on Unichain.** KEEL is a v4 hook, but the quantities measured are properties of concentrated-liquidity books and their order flow. v3 and v4 share the swap math exactly (`SwapMath`, `SqrtPriceMath`, tick bitmap stepping), and v3 mainnet has an order of magnitude more pools with months of history across all three classes. A v4-only sample would be dominated by a few pools and would be thin on long-tail. The cost is that v4-specific flow (hooked pools, dynamic fees) is not represented. That is stated wherever the numbers are used.

**Data access.** No paid provider. Archive state from public `eth.drpc.org` / `blastapi`; logs from `rpc.flashbots.net` (1000-block spans), with `drpc` (100-block spans) as fallback. Every response for a historical block is cached content-addressed under `.cache/rpc/`, so a re-run is byte-identical and offline once fetched.

## D0.2 — Book state is reconstructed from one snapshot plus events (2026-11-23)

**Decision.** For each pool: one archive snapshot of the tick book (bitmap + `ticks()`), taken at the window start, then rolled forward by applying `Mint`/`Burn` (liquidity per tick, gross and net) and `Swap` (price, tick, active liquidity) logs. This yields the exact book at every block in the window.

**Rejected: an archive snapshot per sample point.** About 30 pools × 90 days × several samples a day is tens of thousands of archive multicalls on free endpoints. It is also strictly less information: it samples the book, where replay has it at every block.

**Check that keeps the reconstruction honest.** At every sampled block the reconstructed book's walk is diffed against Uniswap's deployed `QuoterV2`, executed by `eth_call` at that block, at **zero tolerance**. A reconstruction error and a walk error both fail that diff.

## D0.3 — Pool classes are fixed before the data is seen (2026-11-23)

**Decision.** Written before any volume or depth figure was fetched:

- **Blue-chip:** both tokens ∈ {WETH, WBTC, cbBTC, USDC, USDT, DAI}, excluding stable/stable pairs.
- **Mid-cap:** any other pool with median daily volume ≥ **$500k** over the window.
- **Long-tail:** any other pool with median daily volume < $500k.

Pegged pairs (USDC/USDT, wstETH/WETH and similar) are **excluded**: depth at ±1% and ±5% is not a meaningful quantity on a book that never moves that far, and it would dominate the saturation tail for the wrong reason.

**Rejected: assigning classes by judgement after looking.** It would let the class boundaries move with the result.

## D0.4 — Definitions of the measured quantities (2026-11-23)

- **True depth `D(x)`, side `up`:** token1 input (pre-fee) needed to move the pool price from spot to `p·(1+x)` by an exact v3 swap walk. **Side `down`:** token0 input to reach `p/(1+x)`. Zero-liquidity gaps cost nothing to cross. The gross figure, fee included, is also recorded, because it is what the PoolManager charges.
- **Naive depth `N(x)`:** active liquidity at spot, extended as if constant: `L·(√p_target − √p)` up, `L·(1/√p_target − 1/√p)` down. This is the "sum liquidity in range at spot" figure that dashboards report, and it is linear in `x` to first order.
- **Saturation ratio:** `N(x) / D(x)`, both pre-fee, same token. A ratio of 1 means the naive figure is right. Above 1, the naive figure overstates depth.
- **USD:** `D_up` is valued in token1 and `D_down` in token0, both converted at that block's spot via the pool and the WETH/USDC 0.05% pool.

## D0.5 — How the Phase 0 numbers decide the branches (2026-11-23, before results)

Written before any ε or `k_arb` figure existed, so the rule cannot be fitted to the answer.

**ε.** Estimated by OLS of `log(volume_usd)` on `log(depth_usd)` on the pool-day panel, with pool and day fixed effects, controlling for `log(realised σ)` and `log(base-token USD price)`. Standard errors are clustered by pool. Day fixed effects absorb market-wide volume and the ETH price, so no separate market-volume control is needed. Depth is the mean over hourly samples of `(D_up + D_down)/2` in USD, at ±1% and ±5%.

- The branch is decided on the **non-blue-chip** pools (mid-cap + long-tail), because those are where a depth mandate is bought. Blue-chip is reported alongside.
- If the 95% CI straddles a branch threshold (0.3 or 0.5), the **more conservative** branch is taken. This is rule 1, decline rather than guess, applied to a design decision.
- The estimate is an association, not a causal elasticity. Volume attracts liquidity as much as liquidity attracts volume, and the panel cannot separate the two. A specification on lagged depth is reported as a check, and it does not fix the problem. A causal ε would need an instrument this window does not provide. This caveat travels with the number.

**`k_arb`.** Defined as realised LP markout excluding fees, against the pool's own mid at horizon `h`, divided by theoretical LVR `Σ σ²_block · L · √p / 4`. σ comes from hourly pool-price returns, the same source the on-chain `TwapLib` uses. Horizons are 1, 25 and 300 blocks. The 25-block figure is the headline. Reported per pool (window totals) and as the median per class.
