// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PoolHarness} from "../harness/PoolHarness.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";

/// @notice DepthLib.walk against the spec-derived Python reference (sim/depth_reference.py, itself verified against
/// QuoterV2 on 2,500 real mainnet walks), ZERO tolerance, on the same books the PoolManager differential builds.
/// The Python side shares no code with v4-core: a rounding defect inside v4-core's SwapMath/SqrtPriceMath would be
/// invisible to the PoolManager differential (the pool uses the same library) and visible here.
contract MeterVsPythonTest is PoolHarness {
    function setUp() public {
        _setUpManager();
    }

    function _run(uint256 seed, bool up) internal {
        _newPool(seed % 4, int24(int256(uint256(keccak256(abi.encode(seed, "t"))) % 200_001)) - 100_000);
        _randomBook(seed, 1 + uint256(keccak256(abi.encode(seed, "n"))) % 12,
            int24(int256(50 + uint256(keccak256(abi.encode(seed, "s"))) % 4000)));
        uint160 target = _target(1 + uint256(keccak256(abi.encode(seed, "x"))) % 2000, up);
        DepthLib.Measurement memory m = DepthLib.walk(_params(target, 0));
        if (m.capped) return; // the reference is uncapped; capped walks are covered by the PoolManager suite
        (uint256 depth, uint256 fee, uint256 steps, uint256 crossings) = _python(target);
        assertEq(m.depth, depth, "pre-fee depth != reference");
        assertEq(m.fee, fee, "fee != reference");
        assertEq(m.steps, steps, "step count != reference");
        assertEq(m.crossings, crossings, "crossings != reference");
    }

    /// forge-config: default.fuzz.runs = 400
    /// forge-config: ci.fuzz.runs = 2000
    function testFuzz_meterMatchesPython_up(uint256 seed) public {
        _run(seed, true);
    }

    /// forge-config: default.fuzz.runs = 400
    /// forge-config: ci.fuzz.runs = 2000
    function testFuzz_meterMatchesPython_down(uint256 seed) public {
        _run(seed, false);
    }
}
