// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PoolHarness} from "../harness/PoolHarness.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";

/// @notice Inputs aimed across the MAX_STEPS boundary, not drawn from a range that happens to sit under it
/// (VERIFICATION.md §4). A ladder of `k` one-tick positions above or below spot gives exactly k+1 crossings, with k
/// fuzzed over [96, 160] so it straddles MAX_STEPS = 128. Both swap directions, and both swap shapes against the pool.
contract DepthBoundaryTest is PoolHarness {
    using StateLibrary for IPoolManager;

    function setUp() public {
        _setUpManager();
    }

    function _ladder(uint256 k, bool up) internal returns (uint160 target) {
        _newPool(0, 0); // tick spacing 1
        _addPosition(-2, 2, 1e18);
        for (uint256 i; i < k; i++) {
            int24 a = up ? int24(int256(2 + i)) : -int24(int256(3 + i));
            _addPosition(a, a + 1, 1e18 + uint128(i) * 1e15);
        }
        // target beyond the ladder
        target = _target(up ? 10 * k + 300 : 10 * k + 300, up);
    }

    function _boundary(uint256 k, bool up, bool exactOutShape) internal {
        k = bound(k, 96, 160);
        uint160 target = _ladder(k, up);
        DepthLib.Measurement memory m = DepthLib.walk(_params(target, 0));
        uint256 gross = exactOutShape ? _poolGrossInExactOut(target) : _poolGrossIn(target);
        if (m.steps >= DepthLib.MAX_STEPS && !m.reachedPrice) {
            assertTrue(m.capped, "hit the cap without flagging it");
            // a lower bound, which may be tight: here the untraversed tail is a void and costs nothing
            assertLe(m.depth + m.fee, gross, "capped walk exceeded the pool");
            assertEq(uint8(DepthLib.verdict(m)), uint8(DepthLib.Verdict.INCONCLUSIVE), "capped walk breached");
        } else {
            assertFalse(m.capped, "flagged capped below the cap");
            assertEq(m.depth + m.fee, gross, "meter != pool below the cap");
        }
    }

    /// forge-config: default.fuzz.runs = 300
    /// forge-config: ci.fuzz.runs = 1000
    function testFuzz_spansMaxSteps_up_exactIn(uint256 k) public {
        _boundary(k, true, false);
    }

    /// forge-config: default.fuzz.runs = 300
    /// forge-config: ci.fuzz.runs = 1000
    function testFuzz_spansMaxSteps_down_exactIn(uint256 k) public {
        _boundary(k, false, false);
    }

    /// forge-config: default.fuzz.runs = 300
    /// forge-config: ci.fuzz.runs = 1000
    function testFuzz_spansMaxSteps_up_exactOutShape(uint256 k) public {
        _boundary(k, true, true);
    }

    /// forge-config: default.fuzz.runs = 300
    /// forge-config: ci.fuzz.runs = 1000
    function testFuzz_spansMaxSteps_down_exactOutShape(uint256 k) public {
        _boundary(k, false, true);
    }

    /// @notice testFuzz_cappedWalkCanProve: on a book that caps, a depth target the lower bound already clears
    /// terminates early, proven, COMPLIANT. A compliant curator is never denied payment by the cap.
    /// forge-config: default.fuzz.runs = 300
    /// forge-config: ci.fuzz.runs = 1000
    function testFuzz_cappedWalkCanProve(uint256 k, uint256 frac, bool up) public {
        k = bound(k, 140, 160);
        uint160 target = _ladder(k, up);
        DepthLib.Measurement memory full = DepthLib.walk(_params(target, 0));
        assertTrue(full.capped, "setup: book must cap");
        uint256 depthTarget = 1 + full.depth * bound(frac, 1, 99) / 100; // inside what the capped walk already saw
        DepthLib.Measurement memory m = DepthLib.walk(_params(target, depthTarget));
        assertTrue(m.proven, "cleared lower bound did not prove");
        assertFalse(m.capped, "proof reported as capped");
        assertLt(m.steps, DepthLib.MAX_STEPS, "early termination did not stop early");
        assertEq(uint8(DepthLib.verdict(m)), uint8(DepthLib.Verdict.COMPLIANT), "proven walk not compliant");
    }

    /// @notice testFuzz_cappedWalkNeverBreaches: on a book that caps, a depth target the lower bound does NOT clear
    /// is INCONCLUSIVE, never BREACH, even though the walk did fall short.
    /// forge-config: default.fuzz.runs = 300
    /// forge-config: ci.fuzz.runs = 1000
    function testFuzz_cappedWalkNeverBreaches(uint256 k, bool up) public {
        k = bound(k, 140, 160);
        uint160 target = _ladder(k, up);
        DepthLib.Measurement memory full = DepthLib.walk(_params(target, 0));
        DepthLib.Measurement memory m = DepthLib.walk(_params(target, full.depth + 1));
        assertTrue(m.capped, "setup: walk must cap short of the target");
        assertFalse(m.proven, "setup: target must not be cleared");
        assertTrue(DepthLib.verdict(m) != DepthLib.Verdict.BREACH, "capped walk declared a breach");
        assertEq(uint8(DepthLib.verdict(m)), uint8(DepthLib.Verdict.INCONCLUSIVE), "capped short walk not inconclusive");
    }
}
