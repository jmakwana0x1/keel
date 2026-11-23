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
- **Target rounding:** the target sqrt-price is `isqrt(√p²·(1+x))` (up) or `isqrt(√p²/(1+x))` (down), floored in both directions. Floor makes the up target fractionally *below* the exact `p·(1+x)` and the down target fractionally *beyond* `p/(1+x)`. So up-depth is understated and down-depth overstated by under one ulp of `√p` before the two are averaged. The QuoterV2 differential uses the identical integer limit, so this does not affect the zero-tolerance check. It is disclosed here because both sides feed the headline average.
- **No step cap in Phase 0:** the reference walk runs uncapped (`max_steps=None`), so no Phase 0 walk is a lower bound and the capped/proven distinction does not arise. The capped-walk discipline belongs to the on-chain meter (Phase 1).
- **USD:** `D_up` is valued in token1 and `D_down` in token0, both converted at that block's spot via the pool and the WETH/USDC 0.05% pool.

## D0.5 — How the Phase 0 numbers decide the branches (2026-11-23, before results)

Written before any ε or `k_arb` figure existed, so the rule cannot be fitted to the answer.

**ε.** Estimated by OLS of `log(volume_usd)` on `log(depth_usd)` on the pool-day panel, with pool and day fixed effects, controlling for `log(realised σ)` and `log(base-token USD price)`. Standard errors are clustered by pool. Day fixed effects absorb market-wide volume and the ETH price, so no separate market-volume control is needed. Depth is the mean over hourly samples of `(D_up + D_down)/2` in USD, at ±1% and ±5%.

- The branch is decided on the **non-blue-chip** pools (mid-cap + long-tail), because those are where a depth mandate is bought. Blue-chip is reported alongside.
- If the 95% CI straddles a branch threshold (0.3 or 0.5), the **more conservative** branch is taken. This is rule 1, decline rather than guess, applied to a design decision.
- The estimate is an association, not a causal elasticity. Volume attracts liquidity as much as liquidity attracts volume, and the panel cannot separate the two. A specification on lagged depth is reported as a check, and it does not fix the problem. A causal ε would need an instrument this window does not provide. This caveat travels with the number.

**`k_arb`.** Defined as realised LP markout excluding fees, against the pool's own mid at horizon `h`, divided by theoretical LVR `Σ σ²_block · L · √p / 4`. σ comes from hourly pool-price returns, the same source the on-chain `TwapLib` uses. Horizons are 1, 25 and 300 blocks. The 25-block figure is the headline. Reported per pool (window totals) and as the median per class.

## D0.6 — Amendments after review, before results (2026-11-23)

An external review of the pipeline came in after D0.5 was written. No ε or `k_arb` figure had been computed when these changes were made. They are recorded as an amendment so the original text of D0.5 stands as written.

**1. The ε CI now includes a wild cluster bootstrap. The branch reads the wider interval.** The panel has 23 clusters (pools). Cluster-robust (CRV1) variance is biased downward below roughly 40 clusters, and `use_t` corrects the reference distribution but not the estimator. A too-narrow CI raises its lower bound, and the branch rule reads the lower bound, so the bias pushes toward the *less* conservative branch. That is the opposite of D0.5's intent.

- **Fix:** a wild cluster restricted (WCR) bootstrap with Webb six-point weights, B = 9,999, seeded, with the CI found by test inversion. The branch reads the lower bound of the wider of the CRV1-t and WCR intervals.
- **Modelled check** (`sim/test_wild_bootstrap.py`, 200 synthetic panels, 23 heterogeneous clusters, known slope): CRV1-t covers **0.82**, WCR covers **0.94**, against a nominal 0.95. `[modelled]`
- **Implementation check** (`sim/test_wcr_equivalence.py`): the vectorised affine-in-β₀ p-value agrees with an independent per-draw re-estimation. The direct p-value crosses 0.05 within ±1e-6 of each vectorised bound.
- **Rejected:** a cluster jackknife (CRV3). It is also conservative with few clusters, but it has no test-inversion CI and is less studied at G≈23.

**2. The QuoterV2 differential and the mutation evidence now run on the same books.** Previously the mutations ran on fresh snapshots at one hard-coded block, and the published differential ran on replayed books at a seeded sample. `walks_with_word_boundary_steps > 0` showed that the published sample *contained* boundary steps, not that it would *catch* a boundary bug there. Skipping a word boundary changes the total only when two per-step round-ups differ from one, which is at most 1 raw unit per boundary and can vanish.

- **Fix:** `measure.py` now writes the replayed book at each differential sample point. `aggregate.quoter_diff` runs the baseline walk and every planted mutation on those books against the same QuoterV2 result. The mismatch count per mutation goes into `phase0_quoter_diff.json`, and the artifact fails if any mutation goes undetected.

**3. The differential always includes both window endpoints.** Replay error is cumulative, so the last sample is the most informative. Eight random draws leave the final stretch unverified in expectation, so the first and last hourly samples are added to every pool's set.

**4. The `k_arb` circularity is stated.** Markout is taken against the pool's own future mid, and σ in the LVR denominator comes from the same pool-price series.

- **What it measures:** `k_arb` is the fraction of *pool-price* variance that LPs trade through at a loss. It is **not** the fraction of external (CEX) LVR that is realised.
- **Why it is the right input here:** an LP's adverse selection is realised only when the pool price moves, and the on-chain reserve formula takes σ from the same pool via `TwapLib`.
- **Where it diverges from external LVR:** exactly on stale long-tail books, where the pool price lags the reference market. That is the case `k_arb` exists to price. On those pools `k_arb` understates external LVR by however much the pool fails to track the reference.
- An external-reference `k_arb` would need a CEX price series this pipeline does not ingest. This caveat travels with every `k_arb` figure, as D0.5's does with ε.

**Smaller fixes:**
- `naive` is now stored as an exact rational, and the saturation ratio is computed exactly before rounding. Previously it was a 7-significant-figure float.
- The vacuous `assert not capped` is removed. D0.4 states why no Phase 0 walk can be capped.
- Target rounding is disclosed in D0.4.
- `measure.py` and `aggregate.py` now stream, so peak replay memory is the book itself: 29 MB, down from 601 MB.

## D0.7 — Phase 0 results and the decisions they force (2026-11-23)

Every figure below is in `data/phase0_results.md`, regenerated from committed inputs by `sim/model.py`. All are **measured** (Uniswap v3, Ethereum mainnet, 90 days to 2026-11-23). The replayed books pass a zero-tolerance differential against QuoterV2: 2,500 walks, 0 mismatches, and every planted mutation detected.

### ε → branch 3a (fee skim), taken by rule, with the caveat at full strength

| | ε | CI the rule reads (wider of CRV1-t and WCR) |
|---|---|---|
| non-blue-chip, ±1% depth | 0.91 | [0.81, 1.10] |
| non-blue-chip, ±5% depth | 0.82 | [0.65, 1.08] |
| non-blue-chip, lagged ±1% | 0.78 | — |

The CI's lower bound clears 0.5, so D0.5 as amended selects **3a: fee skim**. Phase 3 builds the skim, and 3b (MEV-funded) is not built.

Inconvenient part, stated next to the decision. An ε near 1 is implausibly large as a *causal* elasticity: it would mean doubling depth doubles volume. The more likely reading is that much of the association is LPs following volume. The lagged specification drops it only to 0.78, which does not separate the two. So the skim's positive-sum condition rests on an association. Phase 3 must say so wherever it claims the skim is positive-sum for LPs, and must size `MAX_CURATOR_SHARE_BPS` at the break-even computed from the *lower* CI bound, not the point estimate.

### `k_arb` per class (h = 25 blocks, median of measured pools)

| Class | `k_arb` | Pools measured |
|---|---|---|
| blue-chip | 0.67 | 8 of 8 |
| mid-cap | 0.53 | 4 of 4 |
| long-tail | 0.70 | 8 of 11 |

- **Against the plan's prior:** `BUILD-PLAN` expected 0.2–0.4 on long-tail, from a back-solve against market-making deal pricing. The measurement is higher. Subject to the D0.6(4) caveat, which runs the other way (pool-mid `k_arb` *understates* external LVR on stale books), the prior looks too low.
- **Back-solve not done.** The repository has no source for market-making deal pricing, and none is fabricated. That Gate 0 sub-item stays open until a source is supplied.
- **Post-hoc decline rule.** On pools with almost no LVR (1INCH $78, APE $242, SHIB $206 over 90 days) the ratio is noise over near-zero, for example 327 ± 326. Rule 1 applies: those pools get no `k_arb`. The criterion, standard error > 0.5, was chosen **after** seeing those values and is labelled post-hoc everywhere it appears. Declined pools fall back to the class prior.

### Fees vs LVR contradicts a figure in the plan

`BUILD-PLAN` §0.2 says "fees 2.02× LVR, reserve negative" on ETH/USDC. In this window, WETH/USDC 0.05% has fees/LVR of **0.73**, and the blue-chip median is 0.60. On these measurements the reserve formula is **positive** on blue-chip. The 2.02× figure came from a different window and method, and it is not reproduced here.

### Saturation is a tail phenomenon, not a median one

At the median, the naive figure is right: the ratio is 1.00 from ±0.5% to ±5% and 1.016 at ±10%. On long-tail pools the median is exactly 1.0 even at ±10%, because their books are dominated by wide or full-range positions. Median true depth grows ×9.62 from ±1% to ±10%, against ×9.79 naive.

What *is* true is that the naive error is two-sided and fat-tailed. At ±10%:

- **Overstates:** in 9.3% of pool-blocks naive exceeds true by more than 1.5×, and in 2.8% by more than 2×.
- **Unbounded maximum:** 8.6 × 10⁹, on a two-tick book.
- **Understates:** in 34% of pool-blocks naive is *below* true, because liquidity thickens away from spot.

**Decision (the build plan's standing risk, "saturation is small on real books", realised):** the pitch does not lead on "naive depth is badly wrong." It leads on enforceability. The meter's case is that the naive figure cannot be relied on in either direction, and that its failures concentrate in exactly the books and moments a depth guarantee is bought for. The saturation table is still published in full, tails included.

**Method note:** "fork" in `BUILD-PLAN` §0.3 is implemented as archive `eth_call` against the state at each block rather than an anvil fork. It reads the same state, and the QuoterV2 differential executes the deployed pool code on it.

### D0.7 addendum — no CI below 10 clusters (2026-11-23)

The first CI run of `model.py` drifted on the **mid-cap** wild-cluster bounds only. Mid-cap has 4 pools. With G = 4 and Webb weights there are 6⁴ = 1,296 distinct draws, so many bootstrap t-statistics tie exactly with the observed one, and BLAS rounding on a different machine flipped those comparisons. An interval that changes with the BLAS build is not a measurement.

- **Decision:** below 10 clusters no cluster-robust CI is published. Both CRV1-t and WCR are declined (rule 1). Blue-chip (8 pools) and mid-cap (4 pools) now report the point estimate with "CI declined". A relative tie tolerance of 1e-9 is added to the bootstrap comparison so float noise cannot decide ties in the subsets that remain.
- **Rejected:** enumerating all 6^G weight vectors. That makes the p-value exact but leaves it a coarse step function, and a CI from four clusters is not informative however it is computed.
- **Unchanged:** the branch decision reads non-blue-chip (G = 15). Its bounds were identical on both machines, and the branch is still 3a.
