// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PoolHarness} from "../harness/PoolHarness.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";

contract DepthLibUnitTest is PoolHarness {
    using StateLibrary for IPoolManager;

    function setUp() public {
        _setUpManager();
    }

    function _m(bool proven, bool capped, bool exhausted, bool mismatch) internal pure returns (DepthLib.Measurement memory m) {
        m.proven = proven;
        m.capped = capped;
        m.exhausted = exhausted;
        m.anchorMismatch = mismatch;
    }

    /// @notice The complete decision table. A capped or exhausted walk that falls short is INCONCLUSIVE; flipping
    /// that row to BREACH (the Phase 5 planted mutation) fails this test by name.
    function test_verdictTable() public pure {
        assertEq(uint8(DepthLib.verdict(_m(true, false, false, false))), uint8(DepthLib.Verdict.COMPLIANT));
        assertEq(uint8(DepthLib.verdict(_m(false, false, false, false))), uint8(DepthLib.Verdict.BREACH));
        assertEq(uint8(DepthLib.verdict(_m(false, true, false, false))), uint8(DepthLib.Verdict.INCONCLUSIVE), "capped short");
        assertEq(uint8(DepthLib.verdict(_m(false, false, true, false))), uint8(DepthLib.Verdict.INCONCLUSIVE), "exhausted short");
        assertEq(uint8(DepthLib.verdict(_m(false, false, false, true))), uint8(DepthLib.Verdict.UNUSABLE), "anchor mismatch");
        assertEq(uint8(DepthLib.verdict(_m(true, false, false, true))), uint8(DepthLib.Verdict.UNUSABLE), "mismatch beats proof");
    }

    /// @notice testFuzz_zeroLiquidityGapIsFree: liquidity at spot, a void, then a target inside the void. The depth to
    /// any point in the void equals the depth to the void's near edge, exactly.
    function testFuzz_zeroLiquidityGapIsFree(uint256 tier, uint256 voidTicks, bool up) public {
        _newPool(tier, 0);
        int24 sp = pkey.tickSpacing;
        _addPosition(-20 * sp, 20 * sp, 3e18);
        int24 far = int24(int256(bound(voidTicks, 21, 4000))) * sp;
        uint160 edge = TickMath.getSqrtPriceAtTick(up ? 20 * sp : -20 * sp);
        uint160 inVoid = TickMath.getSqrtPriceAtTick(up ? far : -far);
        DepthLib.Measurement memory toEdge = DepthLib.walk(_params(edge, 0));
        DepthLib.Measurement memory intoVoid = DepthLib.walk(_params(inVoid, 0));
        assertEq(intoVoid.depth, toEdge.depth, "crossing the void cost something");
        assertEq(intoVoid.fee, toEdge.fee, "crossing the void charged a fee");
        assertTrue(intoVoid.reachedPrice, "walk stalled in the void");
    }

    /// @notice Anchor mismatch: a starting liquidity that does not belong to the starting price yields no measurement.
    function test_anchorMismatchIsUnusable() public {
        _newPool(2, 0);
        _addPosition(-600, 600, 1e18);
        DepthLib.DepthParams memory p = _params(_target(1500, false), 0);
        p.liquidityStart = 0; // wrong anchor: the book has 1e18 active at spot
        DepthLib.Measurement memory m = DepthLib.walk(p);
        assertTrue(m.anchorMismatch, "mismatch not detected");
        assertEq(uint8(DepthLib.verdict(m)), uint8(DepthLib.Verdict.UNUSABLE));
    }

    /// @notice Saturation on a book with liquidity at spot and a void above: naive depth (spot liquidity extended)
    /// grows with x, true depth stops growing at the void, so naive/true grows without bound.
    function test_saturation() public {
        _newPool(2, 0);
        _addPosition(-120, 120, 5e18); // ~±1.2% of liquidity, then nothing
        (uint160 sqrtP,,,) = manager.getSlot0(pid);
        uint128 L = manager.getLiquidity(pid);
        uint256 prevTrue;
        uint256 prevRatioX1e6;
        uint16[5] memory xs = [uint16(50), 100, 200, 500, 1000];
        for (uint256 i; i < 5; i++) {
            uint160 t = _target(xs[i], true);
            uint256 trueDepth = DepthLib.walk(_params(t, 0)).depth;
            uint256 naiveDepth = FullMath.mulDiv(L, t - sqrtP, 1 << 96);
            uint256 ratio = naiveDepth * 1e6 / trueDepth;
            // the band ends at ±1.2%: from ±2% on, every target is past the void
            if (i >= 3) assertEq(trueDepth, prevTrue, "true depth kept growing past the void");
            if (i >= 2) assertGt(ratio, prevRatioX1e6, "naive/true did not grow");
            prevTrue = trueDepth;
            prevRatioX1e6 = ratio;
        }
    }

    /// @notice Early termination: the walk stops the instant depth reaches the target; a lower target takes fewer
    /// steps, and the proof never overshoots by more than one step.
    function test_earlyTermination() public {
        _newPool(1, 0);
        for (int24 i; i < 40; i++) _addPosition(i * 10, (i + 1) * 10, 1e18);
        uint160 t = _target(2000, true);
        DepthLib.Measurement memory full = DepthLib.walk(_params(t, 0));
        DepthLib.Measurement memory half = DepthLib.walk(_params(t, full.depth / 2));
        assertTrue(half.proven);
        assertLt(half.steps, full.steps, "did not stop early");
        assertFalse(half.reachedPrice);
    }
}
