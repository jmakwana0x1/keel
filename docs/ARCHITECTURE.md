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
