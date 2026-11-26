# KEEL — verification and harness spec

How the claims are established. Referenced by `CLAUDE.md` and `docs/BUILD-PLAN.md`.

The method is carried from Hydra, which scored Unique Execution 4.5/5 with it. The parts that earned that score were not the test count. They were: proving the prover fires, catching an invariant that asserted nothing, and diffing against the pool at zero tolerance.

---

## 1. Layout

```
src/
  KeelHook.sol                  the hook: bitmap, anchor, skim, lifecycle entry points
  libraries/
    DepthLib.sol                THE PRIMITIVE — the meter + closed-form contribution
    MandateLib.sol              lifecycle state machine, pure transitions
    BondLib.sol                 BOND_SALT placement, slash-accrued-value-only
    ReserveLib.sol              R* from k_arb, sigma, D, x
    PlacementLib.sol            from hydra — the only modifyLiquidity sites
    TwapLib.sol                 from hydra — 64-slot ring + variance estimator
  adapters/
    V4TickSource.sol            extsload implementation of ITickSource
    ShapedTickSource.sol        real book ± a shape, for markout only
  interfaces/
    ITickSource.sol             two functions, pool-key-agnostic

test/
  unit/                         behaviour, one concern per contract
  fuzz/                         properties, aimed across input-domain boundaries
  differential/                 vs the real PoolManager, and vs Python references
  invariant/                    stateful, with afterInvariant premise checks
  halmos/                       symbolic, at the calldata boundary
  demo/                         the economic test
  artifacts/                    artifacts assert their own claims

sim/
  model.py                      regenerates every modelled number in data/
  depth_reference.py            independent tick-walk, written from the spec
  twap_reference.py             independent ring + variance, from the spec
  Derivations.md                the math, with units stated per quantity

data/                           every emitted number, CI-diffed
docs/                           architecture, build plan, prior art, manifest
demo/run.sh                     proof, demo, and the absence
```

---

## 2. foundry.toml

```toml
[profile.default]
solc = "0.8.26"
evm_version = "cancun"
optimizer = true
optimizer_runs = 44_000_000
via_ir = true
ffi = true                      # required for the Python differential references

# Libraries are `internal`, so their code compiles INTO the hook.
# One per-path restriction governs the entire deployed artifact.
# Hydra compiled to 25,759 bytes against EIP-170's 24,576 and no test catches that.
additional_compiler_profiles = [
  { name = "size", via_ir = true, optimizer = true, optimizer_runs = 200 }
]
compilation_restrictions = [
  { paths = "src/KeelHook.sol", optimizer_runs = 200 }
]

fuzz = { runs = 10_000 }

[profile.default.invariant]
runs = 192
depth = 64
fail_on_revert = false

[profile.ci]
fuzz = { runs = 100_000 }

[profile.ci.invariant]
runs = 1024
depth = 128

[profile.halmos]
# Do NOT override `out` here. Halmos scans the default out/ regardless, so a
# custom out dir sends it to artifacts written by ordinary forge test runs,
# which carry no AST. It skips every one of them and reports "No tests" while
# exiting 0 — success-shaped silence. See data/verification.md.
```

---

## 3. The differential layer

### 3.1 Against the real PoolManager, zero tolerance

The meter's entire value is exactness. An approximation means the retainer is paid off an approximation and every property downstream inherits the error.

```solidity
function test_meterMatchesPool(uint128 amt, bool zeroForOne) public {
    // 1. snapshot pool state
    // 2. run the swap through the real PoolManager
    // 3. run DepthLib.measure from the same start with the limit at the reached price
    // 4. assertEq, not assertApproxEq
}
```

**Zero tolerance, not a bound.** Hydra's downward-walk bug was a boundary case where a zero-consumption step at an uninitialized tick looked like a stall but is actually how the pool makes progress through empty ranges. The replay silently produced short counterfactuals in one direction. A test with any tolerance absorbs that.

Cover: both directions, all four fee tiers, several tick spacings, books with and without gaps above spot.

### 3.2 Against an independent Python reference

`sim/depth_reference.py` and `sim/twap_reference.py` are written **from the specification, not from the Solidity**. That is the whole point: two implementations that share no author's assumptions.

Driven over FFI from `test/differential/`:

| Reference | Tolerance |
|---|---|
| Depth walk | exact in integer arithmetic; where the reference uses floats, within 1 wei after rounding |
| TWAP ring | within 1 tick |
| Variance estimator | within a stated relative bound, with the bound in the test name |

### 3.3 Closed-form contribution

```solidity
function test_contributionMatchesTwoWalks(uint128 L, int24 lo, int24 hi) public {
    uint256 viaWalks = measure(book) - measure(bookMinusBand(lo, hi, L));
    uint256 viaClosed = DepthLib.contribution(L, lo, hi, anchor, target);
    assertEq(viaClosed, viaWalks);
}
```

Verified to zero relative error in modelling across liquidity sizes and band geometries, including bands extending past the target and bands entirely above it. If this ever fails, linearity has broken and the closed form must be reverted to a second walk. Make the failure loud.

---

## 4. The fuzz layer, aimed at boundaries

All three of Hydra's review defects lived at an **input-domain boundary**: capped walks, non-gas-token pools, exact-output swaps. None was a logic error in the interior of a function.

So the handler spans boundaries rather than generating comfortable inputs:

- Swap sizes that **span the `MAX_STEPS` threshold**, not sizes drawn from a range that happens to sit under it.
- Band counts spanning `MAX_BANDS`.
- Bands at, above, below and straddling both the anchor and the target.
- Both swap directions, both exact-in and exact-out shapes, on every call.
- Books with a void immediately above spot, because that is the saturation case and the normal long-tail shape.

Representative properties:

| Property | What breaks if it fails |
|---|---|
| `testFuzz_cappedWalkNeverBreaches` | An honest curator is slashed on an inconclusive measurement |
| `testFuzz_cappedWalkCanProve` | A compliant curator is denied payment on a lower bound that already cleared |
| `testFuzz_contributionIsClosedForm` | The gas optimisation is silently wrong |
| `testFuzz_paidImpliesProven` | The escrow pays for a block nobody proved |
| `testFuzz_slashNeverReducesBondLiquidity` | A depth failure is punished by removing depth |
| `testFuzz_bountyBelowDonation` | Watchers are enriched ahead of LPs |
| `testFuzz_zeroLiquidityGapIsFree` | Saturation is mismodelled, as it was in the first pass |
| `testFuzz_shareCapRefusesOversizedMandate` | The skim goes negative-sum for LPs |

---

## 5. The invariant layer, and the vacuity trap

### The trap, verbatim from Hydra

The ratchet invariant passed immediately. It should have been suspicious and was not. Every fuzzed swap large enough to be interesting dislocated the TWAP past `MAX_TWAP_DEVIATION`, so placement skipped every time, hook liquidity stayed at zero for the entire run, and "never decreases" was comparing zero to zero. **The invariant was true and completely empty.**

### The fix, mandatory on every invariant here

```solidity
function afterInvariant() public view {
    // The premise must have been exercised, or the run proves nothing.
    assertGt(handler.provedBlocks(), 0, "vacuous: nothing was ever proved");
    assertGt(handler.breachesObserved(), 0, "vacuous: no breach path exercised");
    assertGt(handler.bondLiquidity(), 0, "vacuous: bond never placed");
}
```

### Structural lesson

Capture and placement want contradictory worlds, so they got separate suites in Hydra. The same applies here: **compliance and breach want contradictory books.** A single handler that tries to fuzz both produces runs where one never happens. Two suites.

| Suite | Handler bias |
|---|---|
| `Compliance.invariant.t.sol` | Books where the curator's band holds; asserts pay ⊆ proven, bond monotone |
| `Breach.invariant.t.sol` | Books thinned at the price; asserts slash ≤ accrued, bounty < donation, liquidity never reduced |

---

## 6. The symbolic layer

### Where the assertion goes

At the calldata boundary, in a fake PoolManager, because that is where the property lives:

```solidity
contract DeltaObserver {
    int256 public minDeltaSeen = type(int256).max;
    bool public sawForeignSalt;

    function modifyLiquidity(PoolKey memory, ModifyLiquidityParams memory p, bytes calldata)
        external returns (BalanceDelta, BalanceDelta)
    {
        if (p.liquidityDelta < minDeltaSeen) minDeltaSeen = p.liquidityDelta;
        if (p.salt != BondLib.BOND_SALT && !isMandateSalt(p.salt)) sawForeignSalt = true;
        return (toBalanceDelta(0,0), toBalanceDelta(0,0));
    }
}
```

### Keep the search small

Use a `FlatTickSource` with no branching so the solver's time goes into the property rather than into tick-bitmap arithmetic. The `ITickSource` seam, which exists so the library is reusable, is also what makes the symbolic layer tractable. Good seams pay twice.

### Scope notes to state in the proof document

- `mintAt` is `internal` rather than `private` so the prover can call it at the boundary. Widening visibility introduces no risk because the parameter is a `uint128`: no caller, present or future, can pass a negative value. The text layer pins it to one call site.
- The proof quantifies over the **codomain** of the liquidity computation, not the computation. Every value `mulDiv` could produce is some `uint128`, so the check ranges over all of them. That is a decomposition, not a narrowing.

### Four properties, not eight

```
check_bondDeltaNonNegative(uint128, int24, int24)
check_mandateDeltaRequiresActiveCurator(address, uint256, int256)
check_payImpliesProven(uint256, uint64)
check_cappedWalkNeverBreaches(uint256, bool)
```

`check_cappedWalkNeverBreaches` is stated **per segment** (D2.10): for any book and any segment budget, a measurement whose last segment stopped capped maps to INCONCLUSIVE, never BREACH. A capped segment is a lower bound and cannot breach on its own. Only a proof whose segments cover the whole range contiguously can breach, and contiguity holds by construction, because each segment resumes from the previous one's exact end state inside one call. The property and the stitched breach do not contradict each other: the first is about a segment, the second about full coverage.

List the remaining manifest lines as intended, not as proved. An eight-property manifest in this timeline is not credible and a judge will say so.

---

## 7. Mutation testing

### The standing rule

> A passing prover run counts as evidence **only after the same prover has been observed failing** on a planted bug.

### Protocol, per manifest line

1. Plant the single edit that would break it. Keep it to one character or one line where possible, at the one site where the defect could actually be introduced.
2. Run all three layers.
3. Record which fire, **and which correctly keep passing**. A layer that fails everything uniformly tells you less. Localisation is information.
4. Revert.
5. Record all three green.
6. Commit the transcript to `data/verification.md`.

### The four mutations

| Line | Planted edit |
|---|---|
| Bond non-negativity | Negate the widening at the single `mintAt` site |
| Mandate salt gating | Remove the `msg.sender == curator` check |
| Pay implies proven | Remove the `proven` requirement from the settle branch |
| Capped never breaches | Flip `capped` handling from inconclusive to breach |

### Expected localisation

The bond mutation should leave the mandate-salt property passing, and the capped-walk mutation should leave both delta properties passing. **If a mutation fails everything, the layers are not independent and the composition argument weakens.** Record that if it happens; it is a finding, not a failure to hide.

---

## 8. Artifacts that assert their own claims

An artifact that stops demonstrating its claim **fails; it does not regenerate**.

```solidity
// test/artifacts/SaturationTrace.t.sol
function test_emitSaturationTrace() public {
    // ... build rows ...
    assertGt(rows.length, 50, "trace must cover enough book states");
    assertTrue(_trueDepthSaturates(rows), "trace no longer demonstrates saturation");
    assertTrue(_naiveGrowsLinearly(rows), "trace no longer demonstrates divergence");
    vm.writeJson(json, "./data/saturation_trace.json");
}
```

The same pattern for the compliance trace (spans N distinct blocks, every proved-compliant row shows contribution ≥ target) and the breach trace (every slash ≤ accrued value).

---

## 9. CI

| Job | What it does | Fails on |
|---|---|---|
| `build` | `forge build --sizes` | any contract over EIP-170 |
| `test` | `forge test` under `profile.ci` | any failure |
| `halmos` | `FOUNDRY_PROFILE=halmos halmos --contract CannotTest` | an unproved property, **or zero tests found** |
| `numbers` | `python3 sim/model.py` then `git diff --exit-code data/` | any drift between code and published number |
| `absence` | greps `src/` for `withdraw`, `selfdestruct`, `delegatecall`, `onlyOwner`, `sweep`, `rescue`, inline assembly | any hit |
| `reproduce` | `./demo/run.sh` twice, diff the artifacts | non-identical output |

**The `halmos` job must fail on zero tests found, not just on a failed property.** A prover that is not attached exits 0. That is the exact failure Hydra hit and it is why this line exists.

---

## 10. The economic test

`test/demo/Mandate.t.sol` is not a unit test. It is the question of whether the mechanism works.

Three pools, **identical** fee tier, tick spacing and seed liquidity, so the only difference is the mechanism:

| | A | B | C |
|---|---|---|---|
| Mechanism | plain pool | unenforced agreement: a curator who provides depth when convenient | KEEL mandate, enforced |
| What it tests | baseline | the status quo being replaced | the claim |

Over N blocks with a replayed real swap tape, measure realised depth at ±x on every block and report the distribution, not the mean. **B is the argument.** An unenforced agreement looks identical to an enforced one on a dashboard and differs exactly in the tail, which is when depth is needed.

The headline: realised depth at the 5th percentile, A versus B versus C.

### Make the adversary adapt

Hydra's demo adversary traded a fixed size at a fixed tip every block regardless of what the hook took from it, and that was a fair criticism of its headline number. Here the equivalent is a curator who optimises: provides depth when cheap, withdraws when volatility spikes, and in pool C weighs the retainer against the slash. Model the decision. If the number moves, publish both.

---

## 11. Reproduce

```bash
forge test                                   # the suite
halmos --contract CannotTest                 # the manifest
python3 sim/model.py                         # every modelled number
./demo/run.sh                                # proof, demo, absence
```

`./demo/run.sh` exits 0 and produces byte-identical artifacts twice, or it is broken.

To reproduce the mutation transcript, apply each planted edit in `data/verification.md` and run the three commands recorded beside it. All layers should fail, and the properties noted as localised should not.
