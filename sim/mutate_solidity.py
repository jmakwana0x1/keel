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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORGE = os.path.expanduser("~/.foundry/bin/forge")
SUITES = "MeterVsPoolTest|MeterVsPythonTest|DepthBoundaryTest|ContributionTest|DepthLibUnitTest|SaturationReproductionTest"
ARTIFACTS = ["data/phase1_saturation_solidity.csv", "data/phase1_contribution.json"]

SPM = "lib/v4-core/src/libraries/SqrtPriceMath.sol"
SRC = "src/adapters/V4TickSource.sol"
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
}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_suites() -> dict[str, str]:
    r = subprocess.run([FORGE, "test", "--match-contract", SUITES, "--fuzz-runs", "100", "--json"],
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


def main():
    saved = {a: (ROOT / a).read_bytes() for a in ARTIFACTS if (ROOT / a).exists()}
    results = {"baseline": run_suites()}
    for name, (path, edits) in MUTATIONS.items():
        f = ROOT / path
        original = f.read_bytes()
        h = sha(f)
        text = original.decode()
        for old, new in edits:
            assert text.count(old) == 1, f"{name}: anchor occurs {text.count(old)} times"
            text = text.replace(old, new)
        f.write_text(text)
        try:
            results[name] = run_suites()
        finally:
            f.write_bytes(original)
            assert sha(f) == h, f"{name}: restore failed"
        print(name, sum(v == "FAIL" for v in results[name].values()), "failing", flush=True)
    results["after_restore"] = run_suites()
    for a, b in saved.items():
        (ROOT / a).write_bytes(b)
    (ROOT / "data" / "phase1_mutations.json").write_text(json.dumps(results, indent=1) + "\n")
    assert all(v == "pass" for v in results["baseline"].values()), "baseline not green"
    assert results["after_restore"] == results["baseline"], "not green after restore"


if __name__ == "__main__":
    main()
