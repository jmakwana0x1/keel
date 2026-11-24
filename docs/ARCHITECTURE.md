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

## D0.8 — A mandate band has no positive reserve on the pools KEEL targets (2026-11-23)

**Question.** Does a depth mandate band have a positive reserve `R*_band = k_arb · LVR_band − fees_band` anywhere? Pool-level fees/LVR, which is blue-chip 0.58, mid-cap 1.15 and long-tail 2.10 (verified from `phase0_pool_days.csv`), is only a proxy, so the band was replayed directly (`sim/band_economics.py`).

**Band model.**
- The band sits at ±x around spot, re-centred at the first hourly sample of each UTC day, as a curator reshaping the mandate position would.
- It is sized so that it alone supplies notional D of depth to +x: `L = D / (√p₀·(√(1+x) − 1))`. D is **$1M for blue-chip pools and $100k for the rest**.
- It is price-taking: the band is assumed not to alter the observed price path.
- LVR accrues at hourly samples while spot is inside the band.
- Fees use v3's per-position identity, `f/(1−f)·L·Δ√p` (token0 leg: `Δ(1/√p)`), over the part of each swap's path inside the band. This matches the exact integer walk to a relative 3.4e-12 over 400 random bands, and the check fails on a planted `f` in place of `f/(1−f)` (`sim/test_band_fee_identity.py`).
- Both legs are linear in L, so **the sign of R\* does not depend on D**. D sets only the dollar magnitudes.
- `k_arb` is the pool's own value at h=25 where measured, else the class median.

**Result** (`data/phase0_results.md`, "Band economics"):

| Class | Pools R* > 0 at ±1% | at ±5% | Median R*/LVR (±1% / ±5%) |
|---|---|---|---|
| blue-chip | 5 of 8 | 7 of 8 | +0.08 / +0.15 |
| mid-cap | 0 of 4 | 0 of 4 | −0.36 / −0.47 |
| long-tail | 2 of 11 | 2 of 11 | −0.40 / −0.70 |

- **Long-tail exceptions:** the two positive long-tail pools are COMP/USDC 1% and MKR/WETH 0.3%. These are stale books where the price moves with little fee-paying flow.
- **Horizon sensitivity:** under `k_arb` at h=300 long-tail rises to 4–5 of 11 and mid-cap to 1 of 4, still a minority. Blue-chip's positive count is fragile, falling to 0–2 of 8 at both h=1 and h=300.

**What it means, stated plainly.** On this window, on the mid-cap and long-tail pools a depth mandate is meant for, a concentrated band at ±1% or ±5% earned more in fees than the adverse selection it bore, without being paid. A retainer priced from `R*` clears at **zero** there, so the auction's reserve has nothing to sell. The thesis "the issuer pays a retainer for depth" has no measured price in the target market. Where R\* is positive, on blue-chip, it is about 10% of LVR, holds only at one horizon, and sits in the pools least in need of a mandate.

**Two caveats that cut in opposite directions:**
1. The D0.6(4) circularity: `k_arb` against the pool's own mid *understates* external LVR on stale books, so true R\* there is higher than measured. The marginal pools (SPX −0.10, LDO −0.16, ENA −0.16 at ±1%) would turn positive if their true `k_arb` were 0.10–0.16 higher. This is the one direction in which the finding could be wrong, and it would take a CEX reference series to settle.
2. Price-taking overstates a large band's fees on a thin book, because real swaps would move a deeper book less. That makes R\* on thin pools *lower* than measured.

**What this does not say.** It does not say no one would pay for depth. It says a profitable LP is already being paid for *supplying* it. What a profitable LP will not do is **promise** to be there during a drawdown, and nothing today lets them make that promise credibly. If KEEL has a product on these numbers, it is enforceable *commitment*: a small retainer, with the bond doing the work. That is a redesign question, so it is **recorded here and deliberately not acted on in this session**.

**Decision.** Phase 1 does not start until this is resolved with the project owner. Phase 3 (funding) and Phase 4 (auction) are on hold, because both assume a positive retainer.

## D0.9 — A falsified prediction: the gap is not largest on long-tail books (2026-11-23)

**The prediction (pre-measurement, made by the project owner from a synthetic model).** The gap between naive and true depth would be largest on long-tail books, because long-tail books are lumpy.

**The measurement.** The opposite.

| Class | Median ratio at ±10% |
|---|---|
| blue-chip | **1.105** |
| mid-cap | 1.039 |
| long-tail | **1.000**, at every width from ±0.5% to ±10% |

Blue-chip also has the heaviest body of the tail: at ±10%, 15.6% of its books are above 1.5 and p90 is 1.604.

**Why the model was wrong.** The synthetic generator built long-tail books as sparse **and lumpy**. Real long-tail books are sparse **and wide**. Their liquidity comes mostly from full-range or near-full-range LPs, so active liquidity really is constant across ±10% and the naive formula `L·Δ√p` is exact. It is the concentrated blue-chip books, built from many narrow positions stacked near spot, where liquidity falls off with distance and naive depth overstates.

**What survives on long-tail is the extreme tail, not the body:** p99 at ±10% is 3.914, and the maximum is 8.6 × 10⁹ on a two-tick book.

**Kept as written, next to its correction.** The prediction is not deleted or reworded. It stays here because a project that keeps its falsified predictions in view earns belief for the ones that survive.

**Correction to a related claim.** It was suggested that zero-liquidity paths, the 30 walks QuoterV2 declined, are a case of "true depth exactly 0, naive finite". They are not. True depth is 0 only when the whole path, spot included, has no liquidity, so naive depth is 0 as well. All 423 zero-true rows in the saturation table have zero naive depth. The reverse case does exist: 17 rows are empty at spot with liquidity beyond, so naive depth is 0 while true depth is positive. That is another instance of understatement.

**Consequence for the pitch.** "Everyone measures depth wrong" is false on central tendency and does not appear in this repository. The claim that survives is the one in `data/phase0_results.md`: **naive depth is an excellent point estimate and a worthless guarantee.** A contract that pays on depth is priced on the tail, and in the tail the naive figure fails in both directions.

## D0.10 — The ε branch rule was applied to an associational quantity (2026-11-23)

**Amends D0.5 and D0.7. Neither is edited in place.**

**What happened.** D0.5 states that ε is an association, not a causal elasticity. D0.7 nevertheless read the CI lower bound against the branch thresholds as if it were causal, and selected 3a. The thresholds in `BUILD-PLAN` §0.1 come from a self-funding condition that needs the **causal** effect of depth on volume. An association can contain any amount of reverse causality, with volume attracting liquidity. A near-unit value is its usual signature, and the lagged specification (0.78) barely moves it. Taking an association as causal because its CI happens to clear a threshold is a guess. Rule 1 forbids that.

**Sensitivity** (`data/phase0_results.md`, "Sensitivity: ε discounted for reverse causality"). The non-blue-chip estimate, 0.91 with CI [0.81, 1.10], is multiplied by a discount d, which stands in for the unknown causal fraction:

| d | ε | CI lower bound | Branch by D0.5 | Break-even cells whose lower bound clears |
|---|---|---|---|---|
| 1.0 | 0.91 | 0.81 | 3a | all with φ ≤ 0.25 |
| 0.5 | 0.46 | 0.40 | 3a-low-share | (s, φ) = (0.05, 0.10), (0.05, 0.25), (0.10, 0.10), (0.20, 0.10) |
| 0.33 | 0.30 | 0.27 | 3b | (0.05, 0.10), (0.10, 0.10) |
| 0.25 | 0.23 | 0.20 | 3b | (0.05, 0.10) only |

Break-even is `ε* = (s + φ)/(1 − φ)`, using the formula as specified for this analysis. That formula takes the skim base equal to fee revenue.

**Decision.**
1. **The ε branch is undetermined on this design.** The panel cannot bound the causal fraction. The branch flips from 3a to 3a-low-share at d = 0.5 and to 3b at d = 0.33, and nothing in the data rules out either discount. The case for 3a would need evidence that most of the association is causal: an instrument, or an exogenous liquidity shock. The window provides neither, so the case is not made.
2. **If a skim is ever built, its caps come from the discount-robust cell.** The only (s, φ) whose lower bound clears at every discount tested is **s = 5%, φ = 10%**. So `MAX_CURATOR_SHARE_BPS = 1000` and maximum skim = 500 bps, not the headline-driven values.
3. **The question is downstream of D0.8.** A skim funds a retainer, and D0.8 found the retainer has no measured price in the target market. The funding branch is moot until D0.8 is resolved, and Phase 3 stays on hold.

**Rejected:** keeping 3a on the grounds that the pre-registered rule selected it. The rule was pre-registered to stop thresholds moving after the result. It was never meant to turn an association into a cause. Following its letter against its own stated caveat would be the guess rule 1 exists to prevent.

## D0.11 — Phase 0.4: depth is not withdrawn when volatility spikes. It is left behind. (2026-11-23)

**Question.** Option A (raised in review after D0.8) reprices the mandate as an **abandonment option**. A profitable LP gives up the right to withdraw, and that right is worth most when volatility spikes, which is exactly when depth is needed and, on the hypothesis, exactly when it leaves. So: does depth leave when volatility spikes? The measurement was run before committing to option A.

**Method** (`sim/vol_depth.py`, `data/phase0_vol_depth.csv`, summary in `data/phase0_results.md` §0.4).
- **Depth measure:** effective liquidity `L_eff = D/Δ√p` from the exact walk to ±1% and ±5%, every hour, for 23 pools over 90 days. It is price-neutral, so a token repricing does not register as withdrawal.
- **Decomposition:** each hour's change is split by walking the *previous* hour's book at the *current* price. *Passive* is price moving through a fixed book; *active* is mint and burn. In all 36,000+ hours without a Mint or Burn the two books agree exactly, which is asserted, and a planted crossing-sign bug fails that assert on the first tick crossed.
- **Placebo:** an event study of declustered top-decile volatility hours against a calm-hour placebo (|r| ≤ pool median).
- **Not pre-registered:** the thresholds (decile, 24 h baseline, 95% recovery, 72 h horizon) were chosen for this analysis.

**The window contained real stress.** WETH/USDT 0.05% shows a worst peak-to-trough of −44% and hourly moves up to 20%. ENA, ONDO and APE fell 37–47%. "LPs did not flee" is tested against a drawdown, not a calm quarter.

**Results.**
1. **LPs do not materially withdraw in volatile hours.** In top-decile hours net mint/burn is small, and positive in every class and width except one (ratio of sums of baseline: blue-chip +0.11%, long-tail +0.71% at ±1%; mid-cap +0.24% at ±5% but **−0.32% at ±1%**, a small net withdrawal). *Correction: an earlier wording of this line said positive or zero in every class, and the mid-cap ±1% figure contradicts it. The commit message for `5cf976f` repeats the overstatement.* Median depth in top-decile hours is within 1% of baseline, and only 12 of 23 pools show lower median depth in volatile hours, a coin flip.
2. **Depth does fall in the tail of a volatile hour, passively.** p10 depth in top-decile hours is −24% (blue-chip) and −27% (mid-cap), against −6% and −9% in other hours. In the volatile hours that end more than 20% below baseline, the hour's own change (median −6% to −7%) is **drift**: the median active share of that loss is 0. Price leaves the concentrated ranges, and the liquidity stays where it was.
3. **The 24-hour drawdown risk is real but not volatility-specific.**
   - **How often:** a drawdown of more than 20% within 24 h follows 20.5% of volatile hours and 16.4% of calm hours on blue-chip; 18.5% against 16.8% on mid-cap; 7.8% against 7.7% on long-tail. None of these differences is significant (the largest is about 1.6 SE).
   - **How long it lasts:** given a >5% drawdown, median recovery to 95% of baseline is 12–22 h, and 16–32% of drawdowns have not recovered at 72 h. This holds after calm and volatile hours alike.

**What this does to option A.**
- **The abandonment-option thesis, as stated, is not supported at hourly resolution.** LPs did not exercise a withdrawal option when volatility spiked. Even through a 44% ETH drawdown they added liquidity on net. A product priced as "the option to flee" prices something LPs were not observed to do.
- **What the data shows instead is that depth is *left behind*.** After a large move, depth around the *new* price is thin because positions are concentrated around the *old* one. Nobody re-centres quickly: recovery takes most of a day, and a fifth to a third of drawdowns have not recovered after three days. That gap is real, it is measured, and it is exactly what a mandate enforces. `prove()` measures depth at the **current** price, so a curator who does not follow the price is in breach.
- **So the surviving product is a *follow* commitment, not a *stay* commitment.** The issuer is not buying "do not leave". They are buying "be where the price is now, within N blocks". The curator's cost is the cost of re-centring promptly: gas, rebalancing loss, and giving up the choice of *when* to re-centre. That cost is not `R*`, and it is not an abandonment option either. It has not been measured.

**Limits.**
- Hourly sampling nets out intra-hour burn-and-remint. A withdrawal that returns within the hour, such as JIT behaviour, is invisible here.
- "Needed" is proxied by realised volatility. A large one-directional move with modest hourly variance could be missed.
- One 90-day window with one major ETH drawdown is one stress episode, not a distribution of them.

**Status.** The finding is recorded, and the redesign is not acted on. Two directions are open for the project owner: price the *follow* commitment (the re-centring cost, which is measurable from this data), or accept that option A fails as originally framed. Phase 1 stays on hold. The meter, `prove()`, the bond and the Cannot manifest are unaffected by either outcome.

## D0.12 — Phase 0.5 pre-registration: the re-centring gap (2026-11-23, before any computation)

Written and pushed **before** any Phase 0.5 code was run on data. The commit that adds this section precedes every commit containing a Phase 0.5 result. Nothing in this section may be edited after results exist. Amendments go in a new numbered decision.

**Question.** D0.11 found depth is left behind at the old price after moves, and that recovery takes most of a day. Is staying centred worth more than it costs? Re-centring crystallises the move: you sell the depreciated side at the new price. Not re-centring keeps the option that price comes back, which D0.11's recovery figures say often pays. The product depends on the **gap** between the benefit and the cost.

### Simulation (`sim/recentre.py`)

- **Pools and target classes.** All 23 pools are simulated. Classification reads the **15 target pools** (mid-cap + long-tail, D0.3).
- **Capital.** C = $1,000,000 for blue-chip and $100,000 otherwise, the D0.8 notionals. It is deployed at the window's first hourly sample as a band of ±x centred at spot, with x ∈ {1%, 5%}.
- **Curators**, run over the same tape:
  - **P** never re-centres.
  - **R_N** re-centres N blocks after spot leaves its band, for N ∈ {25, 300, 1,800, 7,200}.
  - **Headline: R_300 vs P, at x = 5%.** Everything else is sensitivity.
- **Trigger.** A trigger arms at the first swap that leaves spot outside the band. The re-centre executes at the first swap with block ≥ trigger + N, if spot is still outside the band. If spot is back inside first, the trigger clears.
- **Re-centre mechanics.**
  1. Withdraw the band at the current price.
  2. Swap the excess token to the ratio the new band needs, through **the pool's actual book at that moment**, with exact v3 swap math: real price impact plus the pool fee. The curator's own band is excluded from that book.
  3. Mint a new ±x band centred at the post-swap spot with all resulting value.
  - The exact-input swap function is diffed against QuoterV2 `quoteExactInputSingle` at zero tolerance before use.
- **Fees.** Accrued by the per-position identity, `f/(1−f)·L·Δ√p` in range (D0.8, verified against the exact walk). Price-taking: the band does not alter the observed path. Fees are kept in kind, not compounded, and valued at terminal prices.
- **Gas.** 500,000 gas per re-centre (burn + collect + swap + mint), priced at the base fee of the day's first block, with no priority fee, at ETH/USD at the time.
- **Terminal.** At the window's last hourly sample: position value at final spot plus fees, minus gas, all in USD.

**Decomposition**, R − P, as % of C over the 90-day window:
- **benefit** = fees_R − fees_P
- **rebalance cost** = Σ over re-centres of (pool fee + price impact) on the rebalancing swap
- **gas** = Σ gas
- **market** = the residual inventory difference, i.e. the crystallised move against holding the option
- **cost** = gas + rebalance cost − market
- **gap** = benefit − cost = total_R − total_P

### Outcome classification (target pools, headline configuration)

- **Coordination failure (benefit > cost):** median gap > **+0.5% of C**, and the gap is positive on at least **2/3** of target pools.
- **Priced (benefit < cost):** median gap < **−0.5% of C**, and the gap is negative on at least 2/3 of target pools.
- **Thin:** anything else.
- **Kill condition** (overrides all three): if the median |benefit| < **1% of C** **and** the median |cost| < 1% of C, nothing is left on the table in either direction and **the mandate has no economic content** on this measurement.

0.5% of C over 90 days is about 2% annualised. Below that, a gap is within the noise of a single window and too small to fund a bond's opportunity cost. 1% of C over 90 days is about 4% annualised, roughly one typical LP fee yield. A re-centring effect smaller than that in both directions is immaterial.

### Control group: managed positions (`sim/managed.py`)

- **Owner.** Taken from Mint/Burn `owner` (topic 1). Positions held through the NonfungiblePositionManager (`0xC36442b4a4522E871399CD717aBDD847Ab11FE88`) are excluded, because their owner is shared.
- **Re-centre event.** The same owner burns liquidity > 0 from a range and mints into a **different** range within **10 blocks**.
- **Managed owner.** At least **5** re-centre events on that pool in the window.
- **Reported per pool:** number of managed owners; their share of active liquidity at hourly samples; their re-centre latency (blocks from spot leaving their band to their re-mint); and their fee yield (fees per $ of position value per day, by the per-position identity), against the P and R_300 simulations at ±5%.
- **Reading.** The control group converts "nobody re-centres" from an absence into a comparison. It is **descriptive**: classification is decided by the simulation, not by the control group.
- **Known limit.** Positions opened before the window are tracked only from their first in-window event.

**Nothing here is a result.** Results go in D0.13.

## D0.13 — Amendment to D0.12: the registered band is larger than the pool on 12 of 15 target pools (2026-11-24, before the full run)

**Amends D0.12, which is not edited.** Written after a two-pool smoke test and a sizing diagnostic, and **before** the full Phase 0.5 run or any classification.

**Disclosure of what had been seen.** A smoke test ran the registered spec on CRV/WETH 1% (long-tail, target) and WETH/USDC 0.3% (blue-chip). On CRV every re-centring curator went from $100k to under $1k. A trace showed why: the first rebalancing swap, $22k into a pool whose active liquidity is 1/200 of the band's, moved price **+1,008%**, and the registered rule then centred the new band at that impacted price. No other Phase 0.5 output had been seen.

**The defect.** D0.12 reused the D0.8 notionals ($1M blue-chip, $100k otherwise). In D0.8 that was harmless, because price-taking made the sign of R\* independent of size. D0.12 added real price impact on the rebalancing swap, which makes size matter, and the registration did not check the band against the pool. Diagnostic, band L ÷ pool active L at deployment, ±5%:

- **Long-tail:** 1.8× (ONDO) to 619× (1INCH) on all 11 pools. COMP has almost no liquidity at spot (ratio 3 × 10¹⁷).
- **Mid-cap:** ENA 1.2×; AAVE, LINK and UNI are below 0.3.
- **Blue-chip:** DAI/WETH 2.2×, all others below 0.75.

Where the ratio is ≥ 1, both legs of the registered measurement are invalid:
- **P's fees** come from the price-taking identity, which assumes the band does not move prices. A band larger than the pool obviously does.
- **R's rebalancing** pays impact no curator would accept, and is then centred at a price the market immediately arbitrages away.

**Amendment.**
1. **The registered run is still executed and published in full** (`sizing = registered`), with the size diagnostic beside every row. Its classification is reported as registered, labelled *outside its validity on 12 of 15 target pools*.
2. **An amended run is added** (`sizing = small`). Band liquidity = **1% of the pool's median active liquidity over the window's hourly samples**, at deployment and at every re-centre. That is the price-taking limit the fee identity assumes. In that limit the post-swap price is the tape price to within impact on 1% of the pool, so D0.12's "centred at the post-swap price" rule becomes harmless. All other spec terms are unchanged.
3. **Gas in the amended run.** In the small limit C is an arbitrary scale, so gas as a % of C is an artefact of the chosen 1%. The amended gap is therefore classified **excluding gas**. Gas is reported separately as USD per re-centre, with the capital at which it would cross the 0.5% threshold.
4. **Classification is read off the amended run**, with D0.12's thresholds and kill condition unchanged: ±0.5% of C, a 2/3 sign majority, and the 1%/1% kill. The registered run's classification is reported next to it.
5. **Pools whose median active liquidity is zero** cannot host a 1%-of-pool band. They are declined and named (rule 1), not resized.

**Why 1%.** It is not tuned. It places the band well inside the price-taking regime that every fee figure in Phase 0 assumes (D0.8). The registered run already shows what happens at the opposite extreme. A size sweep is not added, because the decision is about whether staying centred pays at all, not about the optimal size.

**Rejected:** silently re-running with new notionals and presenting that as the registered result. That would be the third post-hoc rule, applied without disclosure, which is the pattern D0.12 was written to prevent.

## D0.14 — Phase 0.5 result: following the price has a real cost on the target pools (2026-11-24)

Pre-registered in D0.12, amended before the full run in D0.13. All figures are in `data/phase0_results.md` §0.5, regenerated by `sim/model.py` from `data/phase0_recentre.csv` and `data/phase0_managed.csv`.

**Verification behind it.**
- **Rebalancing swap:** `exact_in` matches QuoterV2 `quoteExactInputSingle` on 536 swaps (421 crossing initialized ticks) at zero tolerance, on both `amountOut` and the post-swap price. A planted rounding bug in the fee deduction breaks 506 of them.
- **Accounting:** over 8,157 small-run re-centres, value before − rebalance cost = value after to within 8.7e-5 (`sim/test_recentre_conservation.py`). Dropping the cost from that check is caught at 1.9e-3.

**Pre-registered verdict: priced (benefit < cost).** Headline: R_300 vs P, ±5%, the 15 target pools.

| Run | Median gap (% of capital, 90 days) | Gap > 0 | Median benefit | Median rebalance | Median market |
|---|---|---|---|---|---|
| **small** (D0.13, classifying) | **−7.60%** | 2 of 15 | +11.28% | 3.86% | −22.86% |
| registered (D0.12, outside validity on 12 of 15) | −67.66% | 1 of 15 | — | — | — |

- **Kill condition not met.** Median |benefit| is 11.3% and median |cost| is 26.9%. Staying centred earns a great deal and costs more, so a great deal is at stake in both directions.
- **Robust to swap venue.** With rebalancing cost set to zero, the median target-pool gap is still **−3.22%** (4 of 15 positive). The loss is the crystallised move, not the swap fee.
- **Robust to latency.** Every N from 25 to 7,200 blocks is negative at both widths, for both mid-cap and long-tail (±5%, long-tail: −13.4 / −6.0 / −7.1 / −4.8%).
- **Blue-chip is the opposite.** At ±5%, N = 300 the median gap is **+4.34% with 8 of 8 positive**, and it is largest when re-centring is slowest (N = 7,200: +9.25%).

**The control group agrees with the simulation, pool by pool.**

| Class | Pools with managed owners | Owners | Median owner latency |
|---|---|---|---|
| blue-chip | 5 of 8 | 11 | ≈ 6,000 blocks (≈ 20 h) |
| mid-cap | 3 of 4 | 4 | ≈ 7,500 blocks |
| long-tail | **0 of 11** | 0 | — |

- **Managers operate where re-centring pays,** at about the slow latency the simulation favours, and are absent where it loses. Their share of active liquidity is small: median 0.03% on blue-chip and 0.4% on mid-cap.
- **"Nobody re-centres" on long-tail is therefore an equilibrium, not neglect.** On these books, following the price after a move sells the side that then recovers (D0.11's recovery times), and that costs more than the fees it captures.
- *Correction to D0.11's framing:* depth is left behind at the old price **because leaving it there is individually rational.**

**What it means: the retainer is real, and the size problem is new.**

1. **The follow-commitment has a measured, positive price.** At a small band it is a median 7.6% of committed capital per 90 days on target pools. This is the quantity D0.8's `R*` should have been: `R*` priced *supplying* depth, which is profitable to a curator who can stay put. A mandate sells *following*, which is not. D0.8 and D0.14 are consistent: supplying pays and committing costs.
2. **It does not scale on long-tail.** In USD, a ±5% band holding each long-tail pool's *entire* median active liquidity is $85 (APE) to $228k (ONDO). A retainer at the scale of current market-making agreements ($120k–$600k a year, figure supplied by the project owner, not measured here) would need committed bands far larger than these pools. *Retracted in D0.15: the $120k–$600k figure is an unmeasured anchor and is not carried into the pitch or the README.* At band ≫ pool the registered run shows the cost: −38% (COMP/USDC) to −91% (SHIB/WETH) of capital over 90 days on long-tail. *Correction: first written as −58% to −91%, which omitted COMP.* On mid-cap the pool scales are $324k (ENA) to $2.9M (LINK), so a band in the same range is about the size of the pool, short of the "band ≫ pool" case the registered run covers.
3. **Phase 0's reading for the design.** A KEEL mandate prices a real service: following the price on pools where following is privately unprofitable. Its natural market is **mid-cap**, where pool scale and retainer scale overlap. On long-tail, a mandate large enough to matter costs more than it can plausibly be paid, unless the issuer accepts paying close to the committed capital itself.

**Limits.**
- **One window.** Annualising a 90-day figure assumes the window is typical. It contained one major ETH drawdown.
- **Price-taking fees** in the small run, which is valid by construction (band = 1% of pool).
- **The rebalancing venue is the pool itself.** The venue-free bound above caps how much a better router could change.
- **The control group** is identified from direct pool calls only. NFT-managed vaults (through the NPM) are invisible to it.
- **Implementation reading of D0.13.** The 1% sizing applies at deployment. Each re-centre redeploys all of the curator's value rather than resetting L to exactly 1%, because holding L fixed would require injecting or withdrawing capital. L therefore stays within the curator's value drift of 1%.

**Status.** The verdict is recorded. `ReserveLib`'s input changes from `k_arb·LVR − fees` (supplying) to the measured re-centring gap (following), and the target narrows to mid-cap pools. Both are design changes for the project owner to accept before Phase 1 starts. The meter, `prove()`, the bond and the Cannot manifest are unaffected.

## D0.15 — Phase 0 closes: long-tail is unmeasurable, mid-cap is the market, staleness is a mandate parameter (2026-11-24)

Accepted by the project owner after review of D0.14. It supersedes D0.14's reasons and headline where they differ, and D0.14's figures stand as computed. Every number below is in `data/phase0_results.md` §0.5 and "Market size".

### 1. Long-tail is dropped because no valid measurement of it exists, not because re-centring loses there

D0.14 gave "re-centring loses on long-tail" as the reason. That is not established, because the measurement is invalid on long-tail in **both** runs, in opposite directions:

- **Registered sizing** ($100k) puts bands at 1.8× to 619× the pool, which breaks the price-taking assumption behind every fee figure and gives rebalancing absurd price impact.
- **Amended sizing** (1% of the pool) gives long-tail bands of **$1 to $2,280, median $65**. APE is a $0.85 position re-centred 29 times: its gas-included gap of −1,041% is gas, not economics. Across target pools the mean of (rebalance + gas) is 92.5% of capital against a median of 7.0%: fixed costs eating micro-positions.

**No band size is valid on these books.** Long-tail v3 pools are too small to host a mandate that is both large enough to matter and small enough not to move the price. Nine of the 11 long-tail pools fall below the size floor below. This reason is structural, and it does not depend on any parameter choice.

### 2. Headline: the subset where the simulation is valid

**Post-hoc, the owner's decision, and it does not change the sign.** The headline is restricted to target pools whose 1%-of-pool band is economically meaningful (capital ≥ $1,000), with gas included:

> **±5%, R_300 vs P: median gap −11.73% of capital over 90 days, 1 of 6 pools positive.** Mid-cap alone (4 pools): −11.73%, 1 positive.

Means on that subset, which add exactly: gap −11.91 = benefit 19.83 − rebalance 3.44 − gas 0.27 + market −28.03.

**Reconciliation of the earlier figures:**

| Figure | What it is |
|---|---|
| −7.60% | 15 target pools, ±5%, R_300, **gas excluded** (D0.13's classifying basis) |
| −14.24% | The same, gas included |
| −11.73% | The valid subset, gas included |
| −165% | 15 pools at ±1%, gas included. Not an economic quantity at that sizing. |

The D0.14 component figures (+11.3 / −7.0 / −26.9) are independent medians and do not sum. `model.py` now labels them so and reports additive means on the valid subset.

### 3. Delay is load-bearing, so staleness tolerance becomes a mandate parameter

| N (blocks) | Valid subset, median gap | Blue-chip, median gap | Blue-chip pools > 0 |
|---|---|---|---|
| 25 | −11.64% | **−4.99%** | 1 of 8 |
| 300 | −11.73% | +4.31% | 8 of 8 |
| 1,800 | −13.04% | +8.04% | 8 of 8 |
| 7,200 | −6.31% | +9.25% | 8 of 8 |

Re-centring too fast loses everywhere: blue-chip flips sign below N = 300. "Blue-chip is the reverse" holds only for N ≥ 300.

**Design decision (constrains Phase 1–2).** A mandate carries an explicit **staleness tolerance N**, and a breach requires depth to be short for N consecutive blocks, not at one sampled block. Without it, a curator is in breach the instant price ticks out of band. That is unmandateable, and the data says enforcing it destroys value.

- **Provenance:** N has measured support from two independent directions. The control group re-centres at a median of 5,956 (blue-chip) and 7,480 (mid-cap) blocks, and the delay curve shows value rising with patience up to 7,200.
- **Bounds:** N is chosen per mandate at post time, within immutable bounds `MIN_STALENESS_BLOCKS = 300` and `MAX_STALENESS_BLOCKS = 7,200`. Below 300 is the measured loss-everywhere regime, and 7,200 is the longest delay measured. It is a mandate parameter, not a governable constant, which is consistent with the architectural invariants.
- **Price schedule:** the delay curve is the retainer's price schedule. A shorter N is a stronger promise and costs more. `ReserveLib` prices from the measured re-centring gap at the mandate's N, replacing `k_arb·LVR − fees`.
- **Mechanism sketch for Phase 2:** `prove()` is sampled, so "short for N consecutive blocks" is evidenced by two short proofs at least N blocks apart with **no compliant proof between them**. The curator rebuts by proving compliance at any block in between, and it is already paid only on proved-compliant blocks, so it has the incentive to do so. This adds a row to the Gate 2 adversary table: *a watcher times two short proofs around a brief, unproved compliant interval.* The rule "one breach per block" still applies.

### 4. The retainer anchor is retracted

The $120k–$600k annual figure for market-making agreements was supplied in review, unmeasured, and the capital figures show it cannot be buying v3 long-tail depth, because those pools cannot absorb it. It does not go into the README or the pitch unless it is measured.

### 5. Market size: a lower bound, from third-party data

DefiLlama's yields API (fetched 2026-11-24, snapshot committed as `data/phase0_market_pools.json`) uses 7-day average daily volume as a proxy for D0.3's 90-day median. Checked against Phase 0's chain-measured classes, it agrees on **14 of 15** non-blue-chip pools. The miss is ENA: mid-cap by 90-day median, long-tail by the latest 7 days.

- **Counted:** **26 mid-cap pools** across Ethereum, Base, Arbitrum and Unichain (v3 + v4): 18 on Ethereum v3, 7 on Ethereum v4, 1 on Arbitrum v3.
- **Unknown:** DefiLlama reports **no volume** for Base, Arbitrum v4 and Unichain, and for 47% of Ethereum v4. Those pools are unclassified, not long-tail. Of them, **31** are non-blue-chip with TVL ≥ $1M, which makes them unverified candidates.
- **Reading:** the four-chain mid-cap market is at least 26 pools, plus up to 31 high-TVL candidates whose volume is unreported. That is not an upper bound, since lower-TVL unreported pools could also qualify. It is known well only for Ethereum v3 and Arbitrum v3. Settling Base needs that chain's swap logs. Four pools was the sample; this is the first estimate of the market, and it is small.

### 6. For the pitch: model and behaviour agree

The simulation says re-centring pays on blue-chip (N ≥ 300) and loses on the target pools. Looked for independently, owners who actually re-centre exist on **5 of 8 blue-chip, 3 of 4 mid-cap and 0 of 11 long-tail pools**, at about the slow latency the simulation favours. A model and revealed behaviour agreeing pool by pool is the strongest credibility item in Phase 0. It goes in the pitch directly after the finding.

**Phase 0 is closed. Phase 1 (the meter) may start.** It is unaffected by every amendment here. Phase 2 inherits the staleness tolerance, and `ReserveLib` inherits the re-centring-gap pricing.

---

# Phase 1 and the carried Phase 2 spec

## D1.1 — `MAX_STEPS = 128` (2026-11-24)

The meter's step cap counts word-boundary steps as well as tick crossings. Across the 20,700 Phase 0 walks on real books out to ±10%, the maximum was 97 steps and p99 was 96. Hydra's 64 would cap 8.6% of ±10% walks and every ±10% measurement on dense books. 128 covers all of them. The meter runs in `prove()`, off the swap path, so the larger budget does not touch swap gas. The gas table (`data/phase1_gas.json`) prices it.

**Rejected:** 64, inherited from Hydra. It was sized for a walk on the swap path and would turn a routine ±10% measurement into an inconclusive one.

## D1.2 — The closed form is the exact estimator: one rounding against ground truth; the two-walk difference is the noisy one (2026-11-24, revised at Gate 1)

**First framing, retracted.** The Gate 1 draft called the closed form "exact in real numbers, bounded in integers" and published a worst error of 2 units. Both were wrong in emphasis, and the 2 was misleading:
- The 2 came from a case set whose deepest walk had **9** steps. The bound that was asserted is per step, and `MAX_STEPS = 128` while Phase 0's real books need up to 97 steps at ±10%. A tight empirical figure from shallow cases does not stand in for the bound.
- The comparison was framed as "closed form approximates walks". It is the other way round.

**Ground truth.** T, the real-valued contribution on the protocol's own Q96 tick prices, computed in `Fraction` arithmetic by `sim/ffi_contribution.py`. That script uses the spec-derived `sqrt_at_tick` from `depth_reference.py` and its own clamping, written from this file's formula and not from `DepthLib`.

**What is shown, and what is merely argued.**

| Estimator | Roundings | Against T | Status |
|---|---|---|---|
| Closed form `contribution()` | one, **down** | `= floor(T)`: never over-credits, error < 1 unit | argued from the identities `floor(floor(x/b)/a) = floor(x/(ab))` and `mulDiv` rounding down; **tested** equal to `floor(T)` on all 400 artifact cases and in 2 × 500 fuzz runs (2,000 each under CI) over the full tick range and full `uint128` liquidity |
| Two-walk difference | one per step, **up**, in each walk | in `(T − steps(without), T + steps(with))` | argued: every step ends on a tick price or the target, so a walk is `Σ ceil(x_i)`; **tested** on every case |
| Two-walk vs closed | — | `|diff − closed| ≤ max(steps(without) − 1, steps(with)) ≤ MAX_STEPS = 128` units | follows from the two rows above; asserted per case |

Not "proved": no Halmos run backs these (the claim discipline reserves that word). The 512-bit products in the token0 comparison make them a solver stress case. Recorded for Phase 5.

**Measured** (`data/phase1_contribution.json`):
- **Shallow:** 300 random books, at most 8 steps. Closed form = floor(T) in 300 of 300. Two-walk vs closed: worst 2 units.
- **Deep:** 100 ladder books, 50–116 rungs, up to **119 steps**. Closed form = floor(T) in 100 of 100. Two-walk vs closed: worst **6** units; two-walk vs truth: worst **7**.
- The two-walk error grows with depth, as the bound says it should. The closed form's error does not grow.
- **Relative size:** at amounts near 1e18, 7 units is 7e-18. Even the 128-unit proven bound stays below 1e-16. Economically irrelevant, but it is the number published, not the 2.

**The direction test is what a mandate depends on.** `testFuzz_contributionNeverOverCredits` asserts `closed ≤ T`. It is asserted against the rational reference, not against the walks: the two-walk difference can itself land below T, so `closed ≤ diff` does not follow and would be the wrong test. `testFuzz_contributionWithinOneUnitOfTruth` is deliberately blind to direction, so it keeps passing when the rounding is flipped. The mutation transcript (`data/verification.md`) shows the split: flipping either `false` flag fails the direction test by name, while the one-unit test and the two-walk test keep passing.

**Rejected:** publishing the two-walk agreement as the accuracy figure. It measures the noisier estimator against the better one, and its empirical maximum depends on how deep the case set happens to go.

## D1.3 — Carried into the Phase 2 spec: staleness, breach and the timing adversary (2026-11-24)

Recorded now so Phase 2 needs no rework. Basis: D0.15 §3.

- **Mandate struct** gains `uint32 stalenessBlocks` (N), chosen at post time. It is bounded by immutable `MIN_STALENESS_BLOCKS = 300` and `MAX_STALENESS_BLOCKS = 7_200`, and a mandate outside the bounds is refused at post.
- **Compliance** is one proof: a `prove()` whose verdict is COMPLIANT.
- **Breach** needs two BREACH proofs at least N blocks apart, with **no COMPLIANT proof between them**. INCONCLUSIVE and UNUSABLE proofs neither open nor close the window. The first BREACH proof opens a *shortfall window* and emits an event. Any COMPLIANT proof inside the window closes it. A BREACH proof at least N blocks after the opening, with the window still open, finalises the breach. "One breach per block" still applies.
- **The retainer schedule is a function of N.** A shorter N is a stronger promise and prices higher. The measured basis is the delay curve (`data/phase0_recentre.csv`; summary in `phase0_results.md` §0.5).
- **New Gate 2 adversary row: the timing watcher.** A watcher times two short proofs around a brief compliant stretch that nobody proved.
  - **Mitigations, named now and built and tested in Phase 2:**
    - The opening proof emits `ShortfallOpened(mandateId, block)`, which gives the curator N blocks of notice.
    - `prove()` is permissionless, and cheap on the compliant path because of early termination, so a rebuttal costs the curator one cheap call.
    - The closing proof must be at least N after the opening. Any COMPLIANT proof inside the window resets it.
  - **Residual assumption, stated:** liveness. A curator who is compliant but never proves inside N blocks can be slashed for a stretch that was in fact compliant. The test: a watcher who opens a window, waits out a compliant stretch, and closes it, must fail whenever any compliant proof exists inside the window, and must succeed only when none does.

## D1.4 — Owner decision: the skim is restored, capped at the discount-robust configuration (2026-11-24)

**Amends D0.10 §3 and D0.15**, which called the funding branch moot because D0.8 had removed the retainer. Phase 0.5 restored the retainer: following the price costs a measured −11.73% of capital per quarter on the valid subset. So the premise of "moot" is gone.

**Decision.** Build the fee skim at the one configuration that clears self-funding break-even at **every** reverse-causality discount tested (D0.10: d down to 0.25, i.e. surviving a 75% discount of ε = 0.91): maximum skim **s = 5% (500 bps)** and curator share **φ = 10%**.
- `MAX_CURATOR_SHARE_BPS = 1000` and `MAX_SKIM_BPS = 500`, both immutable.
- A mandate that would push a curator's share past the cap is refused at post.
- The README states the measured region next to the feature: break-even 0.167 against an ε whose 95% CI lower bound is 0.81 undiscounted and 0.20 at a 75% discount. The estimate is associational, not causal.

**Conservative means capped, not deleted.** The ε caveat (D0.5, D0.10) travels with the feature.

## D1.5 — Owner decision: long-tail stays a supported parameter; its pricing gap is a stated limitation (2026-11-24)

**Amends D0.15 §1's scope.** D0.15 found that a 1%-of-pool *simulated* band cannot price a long-tail mandate: too big breaks price-taking, too small is eaten by gas. That is a limit of the measurement, not of the mechanism. A real long-tail mandate would be sized to what the issuer wants guaranteed, possibly half the pool. D0.11 also found the strongest use case on long-tail: after a large move, the whole book sits stranded at the old price for most of a day.

- The hook accepts long-tail pools.
- The README carries a **Limitations** section: *no measured price exists for long-tail mandates. The reserve there starts from a stated prior (D1.6) and is replaced by the pool's own realised cost as mandates complete.*

## D1.6 — The self-pricing reserve: the book prices its own commitment (2026-11-24, spec; built after Gate 1)

**Idea.** Every comparable design takes its pricing parameters from a deployer. This one derives them from the book it sits on: the reserve for the next mandate on a pool is what following the price actually cost the curators who did it there.

**Measurement per completed mandate epoch.**
- The mandate position (salt `keccak(mandateId)`) is the re-centring curator R.
- The bond position (`BOND_SALT`) is mint-only and never reshaped. **It is placed at the mandate's initial band**, which makes it exactly the passive curator P of Phase 0.5, live on the same pool over the same blocks.
- At settlement the hook computes, per unit of liquidity:
  - `realisedCost = [(V_P + F_P) − (V_R + F_R)] / V_0`, normalised per 7,200 blocks.
  - `F` is fees, read exactly from each live position's `feeGrowthInside` delta.
  - `V` is position value marked at the `TwapLib` price, not spot, so a single-block price push cannot move the mark.
  - `V_0` is the value at the epoch's opening.
- Both legs are in hook state, and no counterfactual replay is needed.

**Reserve.** For the next mandate on the pool, the reserve is the **median of `realisedCost` over the last `RESERVE_WINDOW = 7` completed, non-slashed mandates**, at most one per curator address. It is stored in a fixed 7-slot ring, so it is bounded (rule 5).

**Cold start.**
- With fewer than 4 samples, the reserve is a **published class prior**: an immutable table from Phase 0.5's delay curve at the mandate's N (mid-cap valid subset; long-tail uses the mid-cap figure as a stated proxy).
- It is returned with `isPrior = true` and emitted as such, so a prior is never presented as a measurement (rule 1).

**Properties.** It is a pure function of measured state: no block number, no chooser, no governance. It is the `SplitLib` governor pattern applied to pricing. It also closes the long-tail gap of D1.5 by construction: a pool too thin to simulate still reports what the commitment cost in it.

**Manipulation surface, noted now and closed by a test later:**
1. **Self-inflation.** A curator deliberately loses money (re-centres at the worst moments, or trades against its own band) to raise the next reserve, then bids for that mandate.
   - Bounded by: the median over 7 distinct curators, so moving it takes control of 4 samples; at most one sample per address; slashed mandates excluded; and the inflation costs real money, because the bond leg is real.
   - Test: an attacker controlling k of 7 samples moves the reserve by nothing for k < 4.
   - Residual: Sybil curators. Distinct addresses are cheap, so the real bound is the attacker's realised loss per sample against the retainer uplift it buys. The test must show that loss exceeds the uplift.
2. **Mark manipulation.** The TWAP mark inherits `TwapLib`'s manipulation cost (Hydra).
3. **Deflation by a competitor**, symmetric to 1. Same bound.

## D1.7 — The economic test ports Phase 0.5 to Foundry (2026-11-24, spec; built after Gate 1)

`test/demo/Mandate.t.sol` replays a real swap tape exported from Phase 0 (UNI/WETH 0.3%, mid-cap, 90 days) against a real PoolManager with three curators of equal capital:

- **passive:** band placed once, never moved.
- **re-centring:** voluntary, using the measured optimal delay.
- **mandated:** re-centres within the mandate's N, or is slashed by `prove()`.

It reports realised depth at ±x on every block as a **distribution**, with the **5th percentile** as the headline, not the mean, plus each curator's PnL. B (re-centring, unenforced) is the argument: it looks like C on a dashboard and differs in the tail. The curator's re-centring decision is modelled, not fixed (`VERIFICATION.md` §10).


## D1.8 — Two references are not redundant: a v4-core rounding defect is visible only to the Python one (2026-11-24)

From the Phase 1 mutation transcript (`data/verification.md`):
- **What happened:** the two rounding mutations sit in v4-core's `SqrtPriceMath`, which the PoolManager shares. On the side where the PoolManager differential is a pure value comparison, the mutated pool and the mutated meter agree, so that differential **keeps passing**. Only the spec-derived Python reference fails, on exactly the side its Phase 0 twin did.
- **A trap:** the PoolManager differential does fail under those mutations, but on the *other* side and by **reverting**. The same rounding governs liquidity deposits, the pool is left short of a token, and paying it out underflows (`Panic(0x11)`). A suite with only the PoolManager reference would have scored this mutation as caught, for the wrong reason.

**Consequence:** the Python differential is a required gate, not a cross-check. It runs in CI under `profile.ci`.

**Rejected:** dropping the FFI suite to save CI time. It is the only line of defence against a defect in code shared with the pool.

---

# Phase 2 — mandate, prove, bond

## D2.1 — What a proof decides: the curator's contribution pays, the pool meter breaches (2026-11-24)

A mandate names a pool, a half-width `x` (bps) and a depth target per side: `depth1` in token1 to push the price up by `x`, and `depth0` in token0 to push it down by `x`. A target of zero means that side is not mandated. The anchor is **spot at the proof block**. Targets are `spot·√(1 ± x)` in sqrt price. If clamping to the protocol bounds would move a target, the measurement is not at the stated `x`, and the proof is **UNUSABLE** (rule 1).

Per mandated side:
1. **Curator contribution `c`.** Computed by `DepthLib.contributionAt` over the mandate's hook-owned bands (salt `keccak(mandateId)`), from spot to the target. If `c ≥ target`, the side is **COMPLIANT**, and no walk runs. This is the cheap path.
2. **Otherwise the pool meter** (`DepthLib.walk`, `depthTarget = target`):
   - `proven`: the pool *is* deep enough, but not through this curator. The side is **INCONCLUSIVE**: the issuer has the depth, the curator is not paid for someone else's, and nobody is slashed.
   - `capped` or `exhausted`: **INCONCLUSIVE**. A lower bound that falls short proves nothing (rule 1).
   - anchor mismatch: **UNUSABLE**.
   - otherwise **BREACH**: the pool is short, and so is the curator.

Combined over the sides: any UNUSABLE side makes the proof UNUSABLE; otherwise any BREACH makes it BREACH; otherwise COMPLIANT only if every mandated side is COMPLIANT; otherwise INCONCLUSIVE.

**Why both.**
- Paying on contribution under a known salt is what makes payment JIT-immune (CLAUDE.md: credit the right mechanism). Depth someone adds at the sample from an unrelated address never pays this curator.
- Breaching on the pool meter means a curator is slashed only when the issuer actually lacks depth, measured exactly, with the capped-walk asymmetry intact.
- `c ≤ pool depth` always holds (closed form = floor of truth, which is at most the walk). So "curator compliant" and "pool breach" can never both be true.

**Rejected:**
- *Pool depth alone:* it pays a curator for others' depth and is JIT-able at the sample.
- *Contribution alone, including for breach:* it slashes a curator while the pool is in fact deep, which harms no issuer, and it discards the capped-walk asymmetry that Phase 1 built.

## D2.2 — Payment: one block's retainer per credited compliant proof (2026-11-24)

- A COMPLIANT proof at block b pays exactly **one** block's retainer, at most once per block per mandate. That is the literal form of the invariant *paid blocks ⊆ proved-compliant blocks*.
- **A compliant proof is credited only if the position was untouched in that block before it.** A proof in a block where the mandate was reshaped earlier neither pays nor closes a shortfall window. A BREACH proof in such a block still counts: the curator is short now.
- **A reshape after a credited proof in the same block voids that credit.** The block's retainer returns to escrow, and a shortfall window the proof had closed is reopened at its original block.
- Together these mean a credited block had the scored liquidity in place for the whole block. Depth added at the sample and pulled after (adversary row 1) earns nothing.

**Consequence, stated:** pay scales with proving cadence. A curator proves every block it wants paid. That is cheap on the target chain and cheap on the compliant path, which runs no walk (gas in the README).

**Rejected:** paying the interval since the previous proof. It pays for blocks nobody observed and breaks the invariant.

## D2.3 — Staleness and breach, as specified in D1.3 (2026-11-24)

Built as specified:
- `stalenessBlocks` N ∈ [`MIN_STALENESS_BLOCKS` = 300, `MAX_STALENESS_BLOCKS` = 7,200], checked at post.
- The first BREACH proof opens a shortfall window and emits `ShortfallOpened`.
- A credited COMPLIANT proof closes the window. INCONCLUSIVE and UNUSABLE proofs leave it unchanged.
- A BREACH proof at least N blocks after the window opened, with the window still open, finalises the breach.
- Proofs after `endBlock` are refused, so a window still open at the end never finalises.
- **The bounds are in blocks of the deploy chain.** D0.15 measured 300 and 7,200 *mainnet* blocks, which is 1 h and 24 h. On a 1-second chain, 300 blocks would be 5 minutes, inside the measured loss-everywhere regime. So the bounds are constructor immutables, `MIN_STALENESS_BLOCKS` and `MAX_STALENESS_BLOCKS`. The deploy script converts the measured durations at the chain's block time. They are still immutable at deploy and not governable.

## D2.4 — Where value sits and how it leaves (2026-11-24)

**Escrow.** The issuer's retainer is held as ERC-6909 claims owned by the hook inside the PoolManager. No token balance is held between transactions.
- **Accrual:** each credited block moves `retainerPerBlock` from `escrow` to `accrued`.
- **Vesting:** `accrued` vests to the curator only at SETTLED.

**Slash** (breach finalised) takes **accrued value only**:
- **Bounty:** `accrued · BOUNTY_BPS / 10_000` to the closing prover, with `BOUNTY_BPS = 2_000`. So the bounty is strictly below the donation whenever anything accrued.
- **Donation:** the rest is donated to in-range LPs through `PoolManager.donate`.
- **The Hydra defect is guarded.** `donate` reverts with zero active liquidity. If the pool has none, the amount is parked in `pendingDonation` and anyone can flush it later. `prove()` is off the swap path, but it follows the same rule.
- **Remaining escrow** is refunded to the issuer, who did not receive the service.

**One exit site.** Every outflow (vesting, bounty, refund) goes through `_release`, the only function that moves claims out of the hook. Each caller is a state transition reached by proofs or by the clock.

**Rejected:** streaming vested pay during the term plus a separately sized token bond (`PENALTY_MULTIPLE`). With vesting at settlement, a defaulting curator forfeits everything accrued. With D2.5's frozen principal, a default also forfeits the principal. That is a larger penalty than any oracle-sized bond, and it needs no price to size it.

## D2.5 — Positions, the bond, and what a slash leaves behind (2026-11-24)

**Mandate bands.**
- Up to `MAX_BANDS = 4` ranges under salt `keccak(mandateId)`, minted at accept.
- They are reshapeable by the curator while ACTIVE. Every negative delta goes through one function, `_burnMandate`, which checks curator and ACTIVE itself.
- **Settlement:**
  - After `endBlock` the curator calls `settle()`. That withdraws the bands (the one negative site), vests `accrued`, refunds the remaining escrow, and sets the mandate to SETTLED.
  - After `endBlock + SETTLE_GRACE_BLOCKS` (7,200) anyone may settle. The curator's bands are then frozen, because no negative path exists outside ACTIVE.

**What a slash leaves (flagged).** The architectural invariant allows negative deltas only while ACTIVE, so after SLASHED the curator's bands can never be withdrawn. They stay in the pool as permanent depth. The slash itself takes accrued value only and removes no liquidity from the pool, but the curator's principal is lost to them. That loss, not the accrued retainer, is the economic penalty. It follows from the settled invariant. The only way out is a second negative-delta path reachable outside ACTIVE, which rule 2 rules out.

**Bond.**
- Placed at accept under `BOND_SALT`, at the mandate's **first** band (D1.6: the passive comparator), sized at least the issuer's `minBondLiquidity`.
- Minted once through the single positive site and never touched again. Its principal is never returned. It is the keel: permanent depth bought as the price of entry.
- **Bond fees.** `BOND_SALT` positions at the same band merge across mandates. So fees that v4 surfaces when a later bond mints at the same band are not anyone's to keep, and go to `pendingDonation`. D1.6 reads bond fees from `feeGrowthInside`, not from collections.

## D2.6 — Phase 2 scope cuts, each with its phase (2026-11-24)

| Cut | Why | Lifted in |
|---|---|---|
| At most one live (POSTED or ACTIVE) mandate per pool | Bounded state, no cross-mandate scoring | Phase 4 (auction) |
| Escrow currencies must be ERC-20 (native refused at post) | One settlement path | if needed |
| `beforeSwap` returns a zero delta (permission bit set now, because bits are fixed at address mining) | Skim is Phase 3 (D1.4) | Phase 3 |
| No TWAP ring yet | Only D1.6's mark needs it | with D1.6 |
| Fixed retainer rate | — | Phase 4 |

## D2.7 — EIP-170: the meter becomes its own immutable contract (2026-11-24)

**What happened.** The first build of `KeelHook` was **32,004** bytes at `optimizer_runs = 44M`, against the 24,576-byte limit: the Hydra defect again, caught on day one by `forge build --sizes`. The per-path size profile (`optimizer_runs = 200` on `src/KeelHook.sol`) brought it to **23,705**. That left 871 bytes for the Phase 3 skim and the D1.6 reserve, which is not enough.

**Decision.**
- `DepthLib.walk` and the per-pool `V4TickSource` deployment move into **`KeelMeter`**: immutable, ownerless and stateless, apart from a write-once registry of tick sources. Its address is fixed in the hook's constructor, which checks that it points to the same PoolManager.
- The hook reaches it by plain `CALL`, so there is no `delegatecall` and no proxy.
- The closed-form contribution stays in the hook.

**Sizes after the split:** `KeelHook` **19,925** bytes (4,651 under EIP-170), `KeelMeter` 8,322.

**What did not change.** The walk is the same `DepthLib.walk` code that Phase 1 diffed at zero tolerance, called with the same parameters. Its cost is one extra external call (about 2.6k gas cold) on the breach path only. The compliant path never calls the meter.

**Rejected:**
- Fewer optimizer runs: already at 200.
- Stripping events and views: observability is how watchers find breaches.
- Proxies, diamonds and library linking: proxies and `delegatecall` are banned by the architectural invariants. Public-library linking is `delegatecall`.
