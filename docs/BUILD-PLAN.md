# KEEL — build plan

**Build freeze: December 3. Demo Day: December 11.** Today is November 23, so ten days to freeze.

Eight phases, each with an **exit gate**. Do not cross a gate without meeting it. The plan is ordered so that **the project is submittable from the end of Phase 2 onward**: every phase after that adds score, none is load-bearing for shipping something coherent.

---

## Ship-order principle

If time runs out, what exists must be a complete thing rather than a partial everything.

| After phase | What you have | Submittable |
|---|---|---|
| 0 | Measured economics, no code | no |
| 1 | The meter, proven exact | no |
| 2 | **Mandate, prove, bond, end to end on testnet** | **yes** |
| 3 | Funding: the pool buys its own depth | yes, stronger |
| 4 | Auction: price discovery | yes, stronger |
| 5 | The Cannot manifest | yes, much stronger |
| 6 | Real-data program | yes, strongest |
| 7 | Deploy, demo, exhibition | final |

Phases 5 and 6 carry more score than 3 and 4. If you are behind at December 1, **skip 4, keep 5**.

---

## Phase 0 — Economics go/no-go

**Nothing is built in this phase.** Three numbers decide what the rest of the project is. Run them first because the architecture and the pitch both branch on the answers.

### 0.1 — ε, volume elasticity with respect to depth

The single most important number in the project. Self-funding is only positive-sum for LPs iff

```
ε · f · V · (1 − φ)  >  s · F  +  f · V · φ
```

**Method.** Pull historical swap and liquidity events for 20 to 40 v4/v3 pools across three classes (blue-chip, mid-cap, long-tail). For each pool-day compute realised depth at ±1%, ±5% by tick walk, and realised volume. Regress `log(volume)` on `log(depth)` with pool fixed effects, controlling for realised volatility and for market-wide volume. Report ε by pool class with confidence intervals.

**Branch:**

| ε | Consequence |
|---|---|
| **> 0.5** | Fee skim is positive-sum at reasonable share caps. Phase 3 is the skim. Self-funding leads the Impact story. |
| **0.3 to 0.5** | Skim survives only at low curator share. Tighten `MAX_CURATOR_SHARE_BPS` to the measured break-even and state the region. |
| **< 0.3** | **Skim is dead.** Phase 3 becomes MEV-funded: route captured value into the mandate escrow using Hydra's channel-2 replay, which takes from extractors rather than from LPs and has no ε dependency. |

Write the branch decision into `docs/ARCHITECTURE.md` with the measured number next to it, whichever way it goes.

### 0.2 — `k_arb`, realised markout over theoretical LVR

The reserve formula validates on ETH/USDC (fees 2.02× LVR, reserve negative) and over-prices long-tail by 3× to 15×. `k_arb` is the correction, and the replay engine measures it directly.

**Method.** For a set of pools, compute realised LP markout against a reference price at a fixed horizon, and divide by theoretical `σ²L√p/4` over the same window. Report by pool class. Expect 0.2 to 0.4 on long-tail from the back-solve against observed market-making deal pricing.

**Exit:** `k_arb` per class, with the back-solve from real deal pricing shown alongside as a sanity check.

### 0.3 — Saturation on a real forked book

The headline finding. On a book with liquidity at spot and a void above, true depth saturates while the naive sum grows linearly in `x`, so the ratio is unbounded.

**Method.** Fork mainnet or Unichain at a block. Pick a real long-tail pool. Compute naive depth and tick-walked depth at ±0.5%, ±1%, ±2%, ±5%, ±10%. Report the ratio. Repeat across 100+ pool-blocks and report the distribution.

**Exit:** a measured saturation table from a real book, committed to `data/`. **This is the single highest-value artifact in the project.** One measured number outweighs every modelled one.

### Gate 0

- [ ] ε measured, branch decided and written down
- [ ] `k_arb` measured per pool class
- [ ] Saturation measured on a real forked book, artifact committed
- [ ] `sim/model.py` regenerates all three, CI diffs them

---

## Phase 1 — The meter

### Scope

`src/libraries/DepthLib.sol`. One new entry point on Hydra's existing walk, with the two bounds swapped: pass an unbounded exact-input amount, set `sqrtPriceLimitX96` to the target tick, accumulate input until the price arrives.

```solidity
function measure(DepthParams memory p) internal view
    returns (uint256 depth, bool proven, bool capped);
```

Plus the closed-form contribution, which is **not** a second walk:

```solidity
/// contribution = L_c · ( √p(min(target, upper)) − √p(max(anchor, lower)) )
function contribution(uint128 liquidity, int24 lower, int24 upper, int24 anchor, int24 target)
    internal pure returns (uint256);
```

### Required behaviour

- **Early termination.** Stop the instant accumulated input reaches target. This is what makes the compliant path cheap.
- **Capped walk returns `capped = true`.** The caller must treat `proven && capped` as compliant and `!proven && capped` as inconclusive. Never as a breach.
- **Zero-liquidity gaps traverse at zero cost.** That is v4 semantics and it is what produces saturation. An earlier model got this wrong and invented an "unreachable" state that does not exist.
- **Anchor mismatch** returns the sentinel, as in Hydra's `_cross`.

### Tests

| Kind | What |
|---|---|
| Differential | `measure()` against the real `PoolManager` at **zero tolerance**, both directions, across fee tiers and tick spacings |
| Differential | `contribution()` against `measure(book) − measure(book minus band)`, zero tolerance, across band geometries including bands past the target and bands entirely above it |
| Fuzz | Swap sizes spanning the `MAX_STEPS` boundary, both swap directions, both exact-in and exact-out shapes |
| Unit | Capped walk proves compliance; capped walk never breaches; zero-liquidity gap is free; saturation holds |
| Gas | Table at 1, 3, 9, 64 crossings, committed to `data/` |

### Gate 1

- [ ] Differential against `PoolManager` passes at zero tolerance
- [ ] Closed-form contribution matches the two-walk difference to zero error
- [ ] Capped-walk asymmetry has a test that fails if the direction is flipped
- [ ] Gas table committed, per-crossing marginal cost published
- [ ] Saturation reproduced in Solidity, matching the Phase 0 Python result

---

## Phase 2 — Mandate, prove, bond

**This is the phase that makes the project submittable.** After this you have a complete, demonstrable mechanism.

### Scope

- `Mandate` struct and lifecycle: POSTED → ACTIVE → SETTLED, with EXPIRED and SLASHED.
- `prove(mandateId)` — permissionless, any block. Runs the meter, computes contribution, records compliance or breach, settles.
- Bond posted under `BOND_SALT` through `PlacementLib`, mint-only.
- Retainer streamed as ERC-6909 claims on proved-compliant blocks.
- Slash takes **accrued value only, never liquidity**, donated to in-range LPs.
- Bounty to the `prove()` caller on a breach, strictly smaller than the LP donation.
- **Fixed-rate mandate for now.** The auction is Phase 4.

### The three rules that keep enforcement healthy

1. Bounty strictly smaller than the LP donation, so the dominant outcome of a breach is LPs made whole.
2. A failed `prove()` costs only gas, never a penalty, or nobody watches.
3. One breach per block maximum, so a single lapse cannot be drained by repeated calls.

### Tests

| Kind | What |
|---|---|
| Unit | Each lifecycle transition, including every refusal |
| Unit | Curator proves to earn; watcher proves to slash; both paths settle correctly |
| Unit | Slash never reduces bond liquidity, only accrued value |
| Unit | Bond sizing: default is unprofitable at `PENALTY_MULTIPLE` |
| Fuzz | Breach and compliance across the `MAX_BANDS` and `MAX_STEPS` boundaries |
| Invariant | Bond liquidity is monotonically non-decreasing, with an `afterInvariant` premise check |
| Invariant | Paid blocks ⊆ proved-compliant blocks, always |
| Adversary | Add depth at the sample and pull it after; wide band that looks large on position accounting; thin the book to grief an honest curator |

### Gate 2

- [ ] End to end on Unichain Sepolia: mandate posted, bond placed, block proved compliant, price pushed, breach proved, value slashed and donated
- [ ] Every row of the adversary table closed by a named test that fails if the door reopens
- [ ] Permission bits asserted on-chain against the deployed address
- [ ] No revert reachable on the swap path, with a sweep documented

---

## Phase 3 — Funding

**What this phase is depends on Phase 0.1.** Build one, not both.

### 3a — Fee skim (if ε ≥ 0.3)

- `s = S_MAX · clamp((D_target − D_measured)/D_target, 0, 1)`, a pure function of the measured deficit. No block number, no governance, no chooser.
- Routed through `beforeSwapReturnDelta`. `O(1)` on the swap path, no metering.
- `MAX_CURATOR_SHARE_BPS` immutable, set to the measured break-even. A mandate that would push the curator past it is refused at post time.

### 3b — MEV-funded (if ε < 0.3)

- Route captured value from Hydra's channel-2 counterfactual seizure into the mandate escrow instead of into placement.
- No positive-sum condition to satisfy, because the money comes from extractors, not from LPs.
- The thesis tightens: the pool taxes its attackers and spends the proceeds hiring a market maker.

### Gate 3

- [ ] Funding routes into escrow, settled inside the unlock
- [ ] Swap-path cost measured and published; must be `O(1)`
- [ ] If 3a: share cap enforced, with a test that fails if it is removed
- [ ] If 3b: seizure-to-escrow path has the same capped-walk discipline as Hydra's

---

## Phase 4 — Auction

**Cut this before Phase 5 if time is short.**

Ascending clock, `rate(t) = reserve · min(1, (t − postBlock)/AUCTION_BLOCKS)`. First curator to `accept()` takes the mandate at the current rate and posts the bond in the same transaction. Nobody submits a number; they choose a moment. No commit-reveal.

Reserve computed on-chain: `R* = k_arb · σ²D / (4(√(1+x) − 1)) − E[fees]`, with σ from the `TwapLib` ring, clamped floor and ceiling, and `k_arb` from the Phase 0 measurement as the prior.

**Document the single-bidder case explicitly.** The clock runs to the reserve and the sole curator accepts there, which is still better than opaque bilateral pricing because the reserve is computed from measurable inputs. Thin participation is the first objection to any hackathon auction. Answer it before it is asked.

### Gate 4

- [ ] Clock is a pure function of block number
- [ ] Reserve computed on-chain, σ clamped, clamp stated
- [ ] Single-bidder degeneration documented and tested
- [ ] Front-running an `accept()` is unprofitable, with a test

---

## Phase 5 — The Cannot manifest

**Higher value than Phase 4. Do not skip this.**

### Scope: four properties, not eight

An eight-line manifest in this timeline is not credible. Prove the four that carry the pitch:

```
CANNOT  emit a negative liquidityDelta under BOND_SALT, on any reachable path
CANNOT  emit a negative liquidityDelta under a mandate salt unless msg.sender
          is that mandate's curator and the mandate is ACTIVE
CANNOT  pay a curator for a block in which the meter did not prove the line
CANNOT  declare a breach from a capped walk
```

List the rest as intended, not as proved.

### Three layers, as in Hydra

| Layer | Mechanism | Catches |
|---|---|---|
| Text | greps `src/` every commit, **filtering comment lines first** | a second `modifyLiquidity` site, a negative delta literal, a foreign salt, `selfdestruct`/`delegatecall`/`onlyOwner`/`sweep`/`rescue`, inline assembly |
| Semantics | Halmos over all inputs, at the calldata boundary | a negative delta reachable on an input no fuzzer would generate |
| Runtime | invariant fuzzing over real swaps and blocks | bond liquidity decreasing in practice; paid blocks drifting from proved blocks |

The composition is the proof. Neither layer claims the whole property alone.

### Mutation transcript, one per line

For each manifest line, plant the edit that would break it, run all three layers, record what fires and **what correctly keeps passing**. Then revert and record green. Commit to `data/verification.md`.

A manifest without mutation evidence is a comment.

### Gate 5

- [ ] Four properties discharge
- [ ] Mutation transcript per line, with localisation noted
- [ ] `afterInvariant` premise checks on every invariant
- [ ] `out/` not overridden in any profile

---

## Phase 6 — Real-data program

**Higher value than Phase 4. This is where Impact is won.**

- Replay a real swap tape across three pool classes over months, not days. UHI10's theme prize went to a project that replayed 55,822 real swaps. Beat that and commit the dataset.
- Produce the saturation distribution on real books, which is the headline.
- Back-test the mandate: which historical blocks would a market maker have been paid for, and which would have cost them the bond.
- Commit the dataset and a regeneration script. CI regenerates and fails on drift.

### Gate 6

- [ ] Dataset committed with regeneration script
- [ ] Saturation distribution on real books
- [ ] Mandate back-test over real history
- [ ] Every README number traces to a `data/` artifact

---

## Phase 7 — Deploy, demo, exhibition

### Deploy

Mined CREATE2 address, bits asserted on-chain, source verified, `forge build --sizes` under EIP-170 with the per-path size profile.

### The demo, ninety seconds

One command, `./demo/run.sh`, running in order: the proof, the mandate lifecycle, and the absence (grepping `src/` for `withdraw`, `selfdestruct`, `delegatecall`, `onlyOwner`, `sweep`, `rescue`, all returning nothing). Exits 0, byte-identical artifacts twice.

Live on screen: curator accepts, block proved compliant, price pushed past the band, watcher slashes, value donated to LPs. All real Sepolia transactions.

### The pitch, three beats

1. **Nobody can check depth.** Coffin ships, then Stream. Everyone sums liquidity in range.
2. **So measure it properly.** Replay the swap that moves the price. Saturation table. *Largest block of airtime: this is the novelty.*
3. **Now you can sell it.** Someone commits, gets paid per block it holds, is slashed when it does not.

Four clauses, one each, no mechanism explained: the auction, the funding, the bounty, the bond.

Then the mutation kill, forty seconds: delete one manifest line, three layers fail, restore.

### Gate 7

- [ ] Deployed, verified, bits asserted
- [ ] `./demo/run.sh` exits 0 twice with byte-identical artifacts
- [ ] Video under the time limit, audio clean
- [ ] README cut to the three beats plus the evidence

---

## Schedule against the real deadline

| Dates | Phase |
|---|---|
| Nov 23–24 | **0** — economics, go/no-go |
| Nov 25–26 | **1** — the meter |
| Nov 27–29 | **2** — mandate, prove, bond. **Submittable from here.** |
| Nov 30 | **3** — funding, per the Phase 0 branch |
| Dec 1 | **5** — manifest and mutation *(ahead of 4 deliberately)* |
| Dec 2 | **4** — auction, if time |
| Dec 2–3 | **6** — real-data program |
| Dec 3 | **7** — deploy, freeze |
| Dec 4–11 | Pitch, record, rehearse |

**If behind on December 1: skip Phase 4.** The auction is an Original Idea differentiator, but the manifest and the real-data program carry more weight and are more defensible.

---

## Standing risks

| Risk | Mitigation |
|---|---|
| ε comes back low | Phase 3 branches to MEV funding. Decided in Phase 0, not discovered in Phase 3. |
| Saturation is small on real books | The meter is still the honest measure. Lead on enforceability instead. Know by end of Phase 0. |
| A reachable revert on the swap path | Dedicated sweep in Gate 2, with a test per external call |
| Scope creep into a curated vault | The crowded quadrant. Refuse it. |
| Over-claiming | The claim discipline in `CLAUDE.md`. Every superlative needs a test. |
