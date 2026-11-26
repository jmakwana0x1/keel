"""Phase 1 mutation transcript: the three Python mutations (sim/aggregate.py MUTATIONS) ported to Solidity.

Each mutation is one planted edit at the one site where that defect could be introduced. The runner applies it
(anchor must occur exactly once), runs every Phase 1 suite, records which tests fail and which keep passing, then
restores the file byte-for-byte (hash-checked). Artifact files the suites write are snapshotted and restored too.
Writes data/phase1_mutations.json; data/verification.md is written from it.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORGE = os.path.expanduser("~/.foundry/bin/forge")
SUITES = "MeterVsPoolTest|MeterVsPythonTest|DepthBoundaryTest|ContributionTest|DepthLibUnitTest|SaturationReproductionTest"
ARTIFACTS = ["data/phase1_saturation_solidity.csv", "data/phase1_contribution.json"]

SPM = "lib/v4-core/src/libraries/SqrtPriceMath.sol"
SRC = "src/adapters/V4TickSource.sol"
DL = "src/libraries/DepthLib.sol"
HK = "src/KeelHook.sol"
ML = "src/libraries/MandateLib.sol"
PL = "src/libraries/PlacementLib.sol"
MUTATIONS = {
    "amount1 rounds down": (SPM, [("amount1 := add(amount1, and(gt(mulmod(_liquidity, numerator, denominator), 0), roundUp))",
                                   "amount1 := add(amount1, and(gt(mulmod(_liquidity, numerator, denominator), 0), 0))")]),
    "amount0 rounds down": (SPM, [("            return roundUp\n                ? UnsafeMath.divRoundingUp(",
                                   "            return false\n                ? UnsafeMath.divRoundingUp(")]),
    "skip word-boundary steps": (SRC, [(
        "                uint256 masked = manager.getTickBitmap(poolId, wordPos) & mask;\n\n                initialized = masked != 0;\n                next = initialized\n                    ? (compressed - int24(uint24(bitPos - BitMath.mostSignificantBit(masked)))) * tickSpacing",
        "                uint256 masked = manager.getTickBitmap(poolId, wordPos) & mask;\n                for (uint256 k; k < 64 && masked == 0; k++) { wordPos--; bitPos = 255; compressed = int24(wordPos) * 256 + 255; mask = type(uint256).max; masked = manager.getTickBitmap(poolId, wordPos); }\n\n                initialized = masked != 0;\n                next = initialized\n                    ? (compressed - int24(uint24(bitPos - BitMath.mostSignificantBit(masked)))) * tickSpacing"), (
        "                uint256 masked = manager.getTickBitmap(poolId, wordPos) & mask;\n\n                initialized = masked != 0;\n                next = initialized\n                    ? (compressed + int24(uint24(BitMath.leastSignificantBit(masked) - bitPos))) * tickSpacing",
        "                uint256 masked = manager.getTickBitmap(poolId, wordPos) & mask;\n                for (uint256 k; k < 64 && masked == 0; k++) { wordPos++; bitPos = 0; compressed = int24(wordPos) * 256; mask = type(uint256).max; masked = manager.getTickBitmap(poolId, wordPos); }\n\n                initialized = masked != 0;\n                next = initialized\n                    ? (compressed + int24(uint24(BitMath.leastSignificantBit(masked) - bitPos))) * tickSpacing")]),
    # Phase 1 additions (no Phase 0 twin): the contribution's single rounding flipped to UP, one token at a time
    "contribution rounds up (token1)": (DL, [(
        "return SqrtPriceMath.getAmount1Delta(lo, hi, liquidity, false);",
        "return SqrtPriceMath.getAmount1Delta(lo, hi, liquidity, true);")]),
    "contribution rounds up (token0)": (DL, [(
        "return SqrtPriceMath.getAmount0Delta(lo, hi, liquidity, false);",
        "return SqrtPriceMath.getAmount0Delta(lo, hi, liquidity, true);")]),
}

# Phase 2: one planted edit per lifecycle guarantee, at the one site where it lives.
SUITES2 = "KeelHookTest|NegativeSiteTest|SegmentedTest|MandateLibTest|DepthLibUnitTest|DepthBoundaryTest"
MUTATIONS2 = {
    "negative site ungated": (HK, [(
        "if (actor != m.curator || m.status != MandateLib.Status.ACTIVE) revert NotCurator();",
        "if (false) revert NotCurator();")]),
    "uncredited proof pays": (ML, [("if (!credited) return e;", "")]),
    "others' depth pays the curator": (ML, [(
        "if (v == DepthLib.Verdict.COMPLIANT) return DepthLib.Verdict.INCONCLUSIVE;",
        "if (false) return DepthLib.Verdict.INCONCLUSIVE;")]),
    "capped walk breaches": (DL, [(
        "if (m.capped || m.exhausted) return Verdict.INCONCLUSIVE;",
        "if (m.capped || m.exhausted) return Verdict.BREACH;")]),
    "staleness ignored": (ML, [(
        "} else if (blockNumber - shortfallOpenedAt >= stalenessBlocks) {", "} else {")]),
    "pull does not void credit": (HK, [("if (m.lastCreditedBlock == b) {", "if (false) {")]),
    "donate unguarded": (HK, [("if (poolManager.getLiquidity(pid) == 0) {", "if (false) {")]),
    "mint widening negated": (PL, [(
        "liquidityDelta: int256(uint256(liquidity)),", "liquidityDelta: -int256(uint256(liquidity)),")]),
    "bounty above donation": (ML, [(
        "uint256 internal constant BOUNTY_BPS = 2_000;", "uint256 internal constant BOUNTY_BPS = 6_000;")]),
    "partial coverage breaches": (DL, [("            t.capped = s.capped;\n", "            t.capped = false;\n")]),
    "segments restart at the anchor": (DL, [(
        "            p.sqrtPriceStartX96 = s.sqrtPriceEnd;\n            p.tickStart = s.tickEnd;\n            p.liquidityStart = s.liquidityEnd;\n",
        "")]),
}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_suites(suites: str = SUITES) -> dict[str, str]:
    r = subprocess.run([FORGE, "test", "--match-contract", suites, "--fuzz-runs", "100", "--json"],
                       cwd=ROOT, capture_output=True, text=True)
    out = {}
    try:
        data = json.loads(r.stdout[r.stdout.index("{"):])
    except ValueError:
        return {"<compile>": "FAILED: " + (r.stderr or r.stdout)[-300:]}
    for suite, s in data.items():
        for test, t in s["test_results"].items():
            out[f'{suite.split(":")[-1]}.{test.split("(")[0]}'] = "pass" if t["status"] == "Success" else "FAIL"
    return dict(sorted(out.items()))


def main(phase: str):
    muts, suites, out = (MUTATIONS, SUITES, "phase1_mutations.json") if phase == "1" else \
        (MUTATIONS2, SUITES2, "phase2_mutations.json")
    saved = {a: (ROOT / a).read_bytes() for a in ARTIFACTS if (ROOT / a).exists()}
    results = {"baseline": run_suites(suites)}
    for name, (path, edits) in muts.items():
        f = ROOT / path
        original = f.read_bytes()
        h = sha(f)
        text = original.decode()
        for old, new in edits:
            assert text.count(old) == 1, f"{name}: anchor occurs {text.count(old)} times"
            text = text.replace(old, new)
        f.write_text(text)
        try:
            results[name] = run_suites(suites)
        finally:
            f.write_bytes(original)
            assert sha(f) == h, f"{name}: restore failed"
        print(name, sum(v == "FAIL" for v in results[name].values()), "failing", flush=True)
    results["after_restore"] = run_suites(suites)
    for a, b in saved.items():
        (ROOT / a).write_bytes(b)
    (ROOT / "data" / out).write_text(json.dumps(results, indent=1) + "\n")
    assert all(v == "pass" for v in results["baseline"].values()), "baseline not green"
    assert results["after_restore"] == results["baseline"], "not green after restore"


def probe(names: list[str]):
    """Run only the named Phase 2 mutations and print what fails. Writes nothing; the transcript comes from main."""
    for name in names:
        path, edits = MUTATIONS2[name]
        f = ROOT / path
        original = f.read_bytes()
        h = sha(f)
        text = original.decode()
        for old, new in edits:
            assert text.count(old) == 1, f"{name}: anchor occurs {text.count(old)} times"
            text = text.replace(old, new)
        f.write_text(text)
        try:
            r = run_suites(SUITES2)
        finally:
            f.write_bytes(original)
            assert sha(f) == h, f"{name}: restore failed"
        print(name, "->", [k for k, v in r.items() if v != "pass"], flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "probe":
        probe(sys.argv[2:])
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else "1")
