# CLAUDE.md — KEEL

Standing principles for every session in this repository. Read before writing code.

**Project:** a Uniswap v4 hook that makes depth a contract. Measured by tick-walk replay, sold by auction, paid per block it holds, slashed when it does not.

**Lineage:** reuses `CounterfactualLib`, `PlacementLib` and `TwapLib` from `jmakwana0x1/hydra`, which scored 4.45/5 at UHI10. The verification method transfers with the code. So does the discipline below, which is why it scored.

---

## The eight rules

### 1. Decline rather than guess

Every measurement has an unusable state. Name it, return it, and never price off it.

- A capped walk is a **lower bound** on depth. A lower bound that clears the target **proves compliance**. A lower bound that falls short **proves nothing**. Therefore a capped walk can never declare a breach.
- An anchor mismatch yields no measurement at all. Return the sentinel, do not clamp.
- A pool with no history has no `k_arb`. Use the stated prior and mark it as a prior.

Every one of Hydra's three review defects was a case where the system guessed instead of declining. Each is now a rule with a test attached. Do the same here.

### 2. Every capability is exactly one call site

This is what makes the invariants provable in milliseconds instead of intractable.

- `modifyLiquidity` with a positive delta: exactly one site, taking a `uint128`, widened to `int256` once.
- `modifyLiquidity` with a negative delta: exactly one site, reachable only by the mandate's curator while the mandate is ACTIVE.
- Value leaving escrow: exactly one site, gated on a proof.

If you need a second call site, stop and restructure. A proof that needs to enumerate paths is a proof that will not discharge.

### 3. Nothing on the swap path may revert

A hook that can revert a swap is a hook that can censor one.

- Every guard **skips and emits a reason**. No guard reverts.
- Every external call made inside `beforeSwap` is checked for its own revert conditions, with a test that fails if the check is removed.
- Hydra shipped with an unguarded `donate` that reverts when active liquidity is zero. That class of defect is the one thing that would undo this project. Sweep for it deliberately.

### 4. No number without an artifact

- Every published figure traces to a committed file in `data/`.
- CI regenerates model-derived artifacts and **fails the build on any `git diff`**.
- Artifacts assert their own claims. An artifact that stops demonstrating its claim **fails; it does not regenerate**.
- Figures read off tool output rather than machine-diffed are stated as measured, not as machine-checked. Keep the distinction visible in the README.
- Modelled figures are tagged `[modelled]` until a measured run replaces them. Never let a hypothesis read as a result.

### 5. Everything is bounded

- Every loop bounded by a compile-time constant. `MAX_STEPS`, `MAX_BANDS`, no exceptions.
- No unbounded array growth in storage.
- Position count bounded by grid quantization, not by call count.
- Publish the gas table in the README unprompted. The UHI10 judge's single improvement note was a gas defect he found by reading the source. Find yours first.

### 6. Test the tests

A check that passes for the wrong reason is worse than no check, because it consumes the attention you would have spent looking.

- A passing prover run counts as evidence **only after the same prover has been observed failing** on a planted bug.
- Every invariant carries an `afterInvariant` premise check, so a vacuous run fails instead of passing.
- Text guards filter comment lines before matching. A codebase that documents its own virtues will satisfy a naive grep on documentation alone.
- Watch which tests **keep passing** under a mutation. Localisation is evidence. A suite that fails uniformly tells you less.

### 7. Prefer a second implementation to a tolerance

- The meter is diffed against the real `PoolManager` at **zero tolerance**. Any tolerance absorbs the bug that a tolerance would have caught.
- The TWAP ring and the variance estimator have a Python reference in `sim/`, written from the specification rather than from the Solidity.
- Where two implementations agree to the wei, say so. Where they agree to a bound, say which bound.

### 8. Publish the inconvenient measurement

Hydra published that 98.41% of transactions on its target chain paid zero priority fee, which killed half its mechanism. It also published a theorem of its own that its demo falsified. Both were volunteered before any reviewer could find them, and both are why the rest of its numbers were believed.

If a number undermines a claim, it goes in the repository with the claim next to it. A disclosed weakness costs a fraction of a discovered one.

---

## Architectural invariants

These are settled. Do not relitigate them mid-session.

| | |
|---|---|
| **Ownership** | No owner, no proxy, no upgrade path, no pause, no rescue, no sweep, no `selfdestruct`, no `delegatecall`, no inline assembly in `src/` |
| **Constants** | All immutable at deploy. None governable. |
| **Permission bits** | Three: `afterInitialize`, `beforeSwap`, `beforeSwapReturnDelta`. Every liquidity-lifecycle bit is **zero**, asserted on-chain against the deployed address. |
| **Positions** | Hook-owned, two salts. `BOND_SALT` is mint-only forever. `keccak(mandateId)` is reshapeable by that curator only, only while ACTIVE. |
| **Measurement** | All metering happens in `prove()`. The swap path carries only the `O(1)` skim. |
| **Settlement** | Flash accounting and ERC-6909 claims. Nothing held between transactions. |

---

## Two corrections already applied

Both were found in review after the design was written. Do not reintroduce either.

**Contribution is closed-form, not a second walk.** The meter targets a fixed *price*, so the same ticks are traversed with and without the curator, and each step's input is linear in that step's liquidity. In integers the closed form rounds once, down: it equals `floor` of the real-valued contribution in every tested case (`data/phase1_contribution.json`, against an exact rational reference), so it never over-credits. The two-walk difference is the noisier estimator: one rounding per step, worst observed 7 units from truth on deep walks, proven bound `MAX_STEPS = 128` units against the closed form. Relative error below 1e-16 at amounts near 1e18; **not** zero absolute error (D1.2):

```
contribution = L_c · ( √p(min(target, bandUpper)) − √p(max(anchor, bandLower)) )
```

`ShapedTickSource` survives only for realised-markout work, where the question is genuinely counterfactual. The replay is the **pool-level depth meter**, not the curator isolator. Do not let the README imply otherwise.

**JIT immunity comes from scoring hook-owned positions under a known salt**, not from the replay. Credit the right mechanism.

---

## The claim discipline

Use these words only where a test backs them.

- **"Exact"** — only for the replay against `PoolManager` at zero tolerance, because that test exists.
- **"Proved"** — only for a Halmos property that has been observed failing on a planted bug.
- **"Impossible"** — only for a structural property, meaning no code path exists. Not for a property that is merely expensive to violate.
- **"Measured"** — only for a number from a real chain or a real fork. Modelled numbers say modelled.

Everything else is scoped: "under these conditions", "in the region we measured", "for pools of this shape".

---

## Toolchain pitfalls, carried forward

- `vm.roll(block.number + 1)` in a loop advances once and then silently no-ops on this forge version. Use explicit counters in every multi-block test.
- Libraries here are `internal`, so their code compiles **into** the hook. One per-path compilation restriction governs the whole deployed artifact. Hydra compiled to 25,759 bytes, over EIP-170's 24,576, and no test catches that. Run `forge build --sizes` in CI from day one.
- Halmos scans the default `out/` regardless of profile. Do **not** override `out`, or the prover reads AST-less artifacts, skips every test and exits 0. Success-shaped silence.

---

## Session hygiene

- Read this file and `docs/BUILD-PLAN.md` before starting.
- One phase per session where possible. Phases have exit gates; do not cross a gate without meeting it.
- When a design decision is made mid-session, write it into `docs/ARCHITECTURE.md` in the same session, with the alternative that was rejected and why. A considered alternative scores better than an unexamined choice.
- When a number changes, regenerate the artifact in the same session. CI will catch drift, but catching it yourself is cheaper.
