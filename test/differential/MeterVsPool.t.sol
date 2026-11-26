// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PoolHarness} from "../harness/PoolHarness.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";

/// @notice DepthLib.walk against the real PoolManager, ZERO tolerance: the meter's depth + fee must equal the gross
/// input the PoolManager charges for an exact-input swap with the limit at the target, and the pool must end at the
/// target. Both directions are separate tests so a defect localises by direction. All four fee tiers and tick
/// spacings, books with gaps, swap sizes from 1 bp to 20%.
contract MeterVsPoolTest is PoolHarness {
    using StateLibrary for IPoolManager;

    function setUp() public {
        _setUpManager();
    }

    function _run(uint256 seed, bool up) internal {
        uint256 tier = seed % 4;
        int24 start = int24(int256(uint256(keccak256(abi.encode(seed, "t"))) % 200_001)) - 100_000;
        _newPool(tier, start);
        uint256 n = 1 + uint256(keccak256(abi.encode(seed, "n"))) % 12;
        int24 span = int24(int256(50 + uint256(keccak256(abi.encode(seed, "s"))) % 4000));
        _randomBook(seed, n, span);
        uint256 bps = 1 + uint256(keccak256(abi.encode(seed, "x"))) % 2000;
        uint160 target = _target(bps, up);

        DepthLib.Measurement memory m = DepthLib.walk(_params(target, 0));
        uint256 gross = _poolGrossIn(target);
        (uint160 after_,,,) = manager.getSlot0(pid);

        if (m.capped) {
            // a capped walk is a lower bound on the pool's own input, never more
            assertLe(m.depth + m.fee, gross, "capped walk exceeded the pool");
            assertEq(uint8(DepthLib.verdict(m)), uint8(DepthLib.Verdict.INCONCLUSIVE), "capped walk must be inconclusive");
            return;
        }
        assertFalse(m.anchorMismatch, "anchor mismatch on a consistent book");
        assertTrue(m.reachedPrice, "meter did not reach the target");
        assertEq(after_, target, "pool did not reach the target");
        assertEq(m.depth + m.fee, gross, "meter != PoolManager");
    }

    /// forge-config: default.fuzz.runs = 2000
    /// forge-config: ci.fuzz.runs = 10000
    function testFuzz_meterMatchesPool_up(uint256 seed) public {
        _run(seed, true);
    }

    /// forge-config: default.fuzz.runs = 2000
    /// forge-config: ci.fuzz.runs = 10000
    function testFuzz_meterMatchesPool_down(uint256 seed) public {
        _run(seed, false);
    }

    /// @notice Rule 6: the fuzz suites above prove nothing if their inputs never exercise the cases that matter.
    /// Replays fixed seeds against the PoolManager and asserts that tick crossings, word-boundary steps and
    /// zero-liquidity gaps all occur, and that every one of those walks matched the pool exactly.
    function test_differentialCoverage() public {
        uint256 walks;
        uint256 withCrossings;
        uint256 withBoundarySteps;
        uint256 withGap;
        for (uint256 seed; seed < 120; seed++) {
            for (uint256 dir; dir < 2; dir++) {
                _setUpManager();
                _newPool(seed % 4, int24(int256(uint256(keccak256(abi.encode(seed, "t"))) % 200_001)) - 100_000);
                _randomBook(seed, 1 + uint256(keccak256(abi.encode(seed, "n"))) % 12,
                    int24(int256(50 + uint256(keccak256(abi.encode(seed, "s"))) % 4000)));
                uint160 target = _target(1 + uint256(keccak256(abi.encode(seed, "x"))) % 2000, dir == 0);
                DepthLib.Measurement memory m = DepthLib.walk(_params(target, 0));
                if (m.capped) continue;
                assertEq(m.depth + m.fee, _poolGrossIn(target), "coverage walk != pool");
                walks++;
                if (m.crossings > 0) withCrossings++;
                if (m.steps > m.crossings + 1) withBoundarySteps++;
                if (m.depth == 0 || _hadGap(m)) withGap++;
            }
        }
        emit log_named_uint("walks", walks);
        emit log_named_uint("with tick crossings", withCrossings);
        emit log_named_uint("with word-boundary steps", withBoundarySteps);
        emit log_named_uint("with a zero-liquidity stretch", withGap);
        assertGt(walks, 200, "vacuous: too few complete walks");
        assertGt(withCrossings, walks / 4, "vacuous: crossings rarely exercised");
        assertGt(withBoundarySteps, walks / 4, "vacuous: word-boundary steps rarely exercised");
        assertGt(withGap, 10, "vacuous: zero-liquidity gaps never exercised");
    }

    /// @dev A walk crossed a zero-liquidity stretch if the pool's active liquidity is zero at its end price.
    function _hadGap(DepthLib.Measurement memory) internal view returns (bool) {
        return manager.getLiquidity(pid) == 0;
    }

    /// @dev Deterministic coverage of every fee tier, both directions, a void above and below spot.
    function test_meterMatchesPool_allTiers_withGap() public {
        for (uint256 tier; tier < 4; tier++) {
            for (uint256 dir; dir < 2; dir++) {
                _setUpManager(); // fresh manager: the same pool key cannot be initialised twice
                _newPool(tier, 0);
                int24 sp = pkey.tickSpacing;
                _addPosition(-10 * sp, 10 * sp, 1e18); // liquidity at spot
                _addPosition(400 * sp, 500 * sp, 5e18); // far above, behind a void
                _addPosition(-500 * sp, -400 * sp, 5e18); // far below, behind a void
                uint160 target = _target(1500, dir == 0);
                DepthLib.Measurement memory m = DepthLib.walk(_params(target, 0));
                assertEq(m.depth + m.fee, _poolGrossIn(target), "tier/gap mismatch");
            }
        }
    }

    /// @notice D2.10, zero tolerance: a walk too long for one segment, continued across segments, equals the
    /// PoolManager's own charge for the same price move. Every tick inside the range is initialised (a dust ladder on
    /// spacing 1), so one segment always caps; the test fails if it ever does not, which keeps it from passing on
    /// books where stitching never happened.
    /// forge-config: default.fuzz.runs = 200
    /// forge-config: ci.fuzz.runs = 1000
    function testFuzz_stitchedMatchesPool(uint256 seed) public {
        bool up = seed % 2 == 0;
        _newPool(0, int24(int256(uint256(keccak256(abi.encode(seed, "t"))) % 20_001)) - 10_000);
        _randomBook(seed, 1 + seed % 6, 2000);
        (, int24 spot,,) = manager.getSlot0(pid);
        uint256 width = 140 + uint256(keccak256(abi.encode(seed, "w"))) % 600; // 140..739 ticks: 2..6 segments
        for (uint256 k; k < width; k++) {
            int24 t = up ? spot + int24(int256(k)) : spot - int24(int256(k)) - 1;
            _addPosition(t, t + 1, 1 + uint128(uint256(keccak256(abi.encode(seed, k))) % 1e6));
        }
        uint160 target = TickMath.getSqrtPriceAtTick(up ? spot + int24(int256(width)) : spot - int24(int256(width)));

        DepthLib.Measurement memory one = DepthLib.walk(_params(target, 0));
        assertTrue(one.capped, "premise: one segment did not cap");
        (DepthLib.Measurement memory m, uint256 used) = DepthLib.walkSegments(_params(target, 0), DepthLib.MAX_SEGMENTS);
        assertGt(used, 1, "premise: no stitching happened");
        assertFalse(m.capped, "six segments did not cover a range under the step bound");
        assertTrue(m.reachedPrice, "stitched walk did not reach the target");
        assertEq(m.depth + m.fee, _poolGrossIn(target), "stitched meter != PoolManager");
    }
}
