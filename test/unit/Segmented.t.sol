// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {KeelHook} from "../../src/KeelHook.sol";
import {MandateLib} from "../../src/libraries/MandateLib.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";
import {KeelHarness} from "../harness/KeelHarness.sol";

/// @title The segmented breach proof (D2.10)
/// @notice The attack: dust at every initialisable tick inside the mandated range, so one walk caps at MAX_STEPS
/// and returns INCONCLUSIVE, which leaves the shortfall window untouched (D2.3) and the mandate unslashable.
/// The fix: a proof may continue a capped walk for up to MAX_SEGMENTS segments in one call. Runs on a spacing-1
/// pool, where a ±3% range spans ~305 initialisable ticks: three segments' worth.
contract SegmentedTest is KeelHarness {
    using StateLibrary for IPoolManager;

    uint256 id;

    function setUp() public {
        _setUpKeel();
        (pkey, pid) = initPool(currency0, currency1, IHooks(address(hook)), 500, 1, SQRT_PRICE_1_1);
        _lp(-6000, 6000, 1e18); // thin background: ~1.5e16 of depth over 3%, far below the 4e17 target
        KeelHook.Terms memory t = _terms(4e17, 4e17);
        t.xBps = 300;
        id = _post(t);
        _accept(id, _bands1(-30, 30, 1e18)); // a curator who is short: contributes ~3e15
        // the dust ladder: one 1-wei position on every tick of the range, both sides
        for (int24 k = -320; k < 320; k++) {
            _lp(k, k + 1, 1);
        }
        _roll(1);
    }

    function _proveN(address who, uint256 segs) internal returns (DepthLib.Verdict v) {
        vm.prank(who);
        v = hook.prove(id, segs);
    }

    function test_singleWalkIsCappedAndChangesNothing() public {
        assertEq(uint8(_prove(watcher, id)), uint8(DepthLib.Verdict.INCONCLUSIVE), "one walk should cap");
        assertEq(_openedAt(id), 0, "an inconclusive proof opened a window");
    }

    function test_segmentsReachBreachAndFinalise() public {
        assertEq(uint8(_proveN(watcher, 6)), uint8(DepthLib.Verdict.BREACH), "segments did not reach a breach");
        uint64 opened = _openedAt(id);
        assertEq(opened, blk, "window not opened");
        _roll(N);
        // a single walk still cannot finalise: capped, so it proves nothing
        assertEq(uint8(_prove(watcher, id)), uint8(DepthLib.Verdict.INCONCLUSIVE));
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.ACTIVE), "a capped walk finalised a breach");
        _proveN(watcher, 6);
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.SLASHED), "covered breach did not finalise");
    }

    /// Coverage, the rule-1 line: two segments (256 steps) cannot cover ~305 ticks. A proof that leaves part of the
    /// range unwalked is a lower bound short of the target and must stay INCONCLUSIVE. Fails if the stitched result
    /// reports coverage it did not achieve (mutation "partial coverage breaches").
    function test_partialCoverageStaysInconclusive() public {
        assertEq(uint8(_proveN(watcher, 2)), uint8(DepthLib.Verdict.INCONCLUSIVE), "partial coverage breached");
        assertEq(_openedAt(id), 0, "partial coverage opened a window");
    }

    /// Segments are clamped to MAX_SEGMENTS: asking for more walks no more. A walk far past the ladder needs more
    /// than six segments, so the clamp is what stops it.
    function test_segmentsClampedToMax() public view {
        (uint160 sp, int24 tick,,) = manager.getSlot0(pid);
        (DepthLib.Measurement memory m, uint256 used) = meter.walkSegments(
            DepthLib.DepthParams({
                sqrtPriceStartX96: sp,
                tickStart: tick,
                liquidityStart: manager.getLiquidity(pid),
                sqrtPriceTargetX96: TickMath.getSqrtPriceAtTick(-200_000),
                depthTarget: 0,
                feePips: 500,
                tickSpacing: 1,
                source: hook.tickSource(pid)
            }),
            1_000
        );
        assertEq(used, DepthLib.MAX_SEGMENTS, "segments not clamped");
        assertEq(m.steps, DepthLib.MAX_SEGMENTS * DepthLib.MAX_STEPS, "steps beyond the clamp");
        assertTrue(m.capped, "an uncovered walk must stay capped");
    }

    /// The post-time bound: every accepted range is coverable by one segmented proof on any book (D2.10).
    function test_postRefusesRangesNoProofCanCover() public {
        // free the pool: settle path is long; use a fresh spacing-1 pool instead
        (pkey, pid) = initPool(currency0, currency1, IHooks(address(hook)), 100, 1, SQRT_PRICE_1_1);
        KeelHook.Terms memory t = _terms(4e17, 4e17);
        t.xBps = 800; // ~834 ticks downward at spacing 1: over 6 * 128
        vm.prank(issuer);
        vm.expectRevert(KeelHook.BadTerms.selector);
        hook.post(pkey, t);
        t.xBps = 700; // ~726 ticks: coverable
        vm.prank(issuer);
        hook.post(pkey, t);
    }

    /// The bound is a bound: on a book with every tick initialised, no walk to x exceeds stepBound(x).
    /// forge-config: default.fuzz.runs = 256
    /// forge-config: ci.fuzz.runs = 1000
    function testFuzz_stepBoundHolds(uint256 xBps) public view {
        xBps = bound(xBps, 1, 300);
        uint256 b = MandateLib.stepBound(xBps, 1);
        (uint160 sp, int24 tick,,) = manager.getSlot0(pid);
        for (uint256 side; side < 2; side++) {
            uint256 f = _sqrtE18(side == 0 ? 10_000 + xBps : 10_000 - xBps);
            uint160 target = uint160(uint256(sp) * f / 1e18);
            (DepthLib.Measurement memory m,) = meter.walkSegments(
                DepthLib.DepthParams({
                    sqrtPriceStartX96: sp,
                    tickStart: tick,
                    liquidityStart: manager.getLiquidity(pid),
                    sqrtPriceTargetX96: target,
                    depthTarget: 0,
                    feePips: 500,
                    tickSpacing: 1,
                    source: hook.tickSource(pid)
                }),
                DepthLib.MAX_SEGMENTS
            );
            assertTrue(m.reachedPrice, "walk did not reach the target");
            assertLe(m.steps, b, "a walk exceeded the step bound");
        }
    }

    function _sqrtE18(uint256 bps) internal pure returns (uint256 y) {
        uint256 x = bps * 1e32;
        y = x;
        uint256 z = (x + 1) / 2;
        while (z < y) {
            y = z;
            z = (x / z + z) / 2;
        }
    }
}
