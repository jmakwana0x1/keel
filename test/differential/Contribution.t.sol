// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PoolHarness} from "../harness/PoolHarness.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";

/// @notice The closed-form contribution against the two-walk difference measure(book + band) − measure(book), on the
/// real PoolManager. Exact in real numbers; in integers each walk rounds every step UP and the closed form rounds
/// once DOWN, so the asserted bound is |difference − closed| <= steps(without) + steps(with)  (D1.2). If linearity
/// broke (a different path with and without the band) the error would scale with the band, not with the step
/// count, and this fails loudly.
contract ContributionTest is PoolHarness {
    using StateLibrary for IPoolManager;

    struct Stats {
        uint256 cases;
        uint256 exact;
        uint256 maxAbsErr;
        uint256 maxSteps;
        uint256 nonZeroContrib;
        uint256 exactNonZero;
        uint256 bandPastTarget;
        uint256 bandOutside;
    }

    function setUp() public {
        _setUpManager();
    }

    function _case(uint256 seed, bool up, Stats memory st) internal {
        _setUpManager();
        _newPool(seed % 4, 0);
        int24 sp = pkey.tickSpacing;
        _randomBook(seed, 1 + seed % 6, 300 * sp);
        (, int24 anchor,,) = manager.getSlot0(pid); // pool initialised exactly at tick 0: spot is a tick price
        uint256 r = uint256(keccak256(abi.encode(seed, "band")));
        int24 dist = int24(int256(1 + r % 200)) * sp;
        int24 target = up ? anchor + dist : anchor - dist;
        // band geometry: anywhere in [-250, 250]·sp around the anchor, so it can straddle the anchor, the target,
        // lie past the target, or sit entirely outside the walked range
        int24 lo = _align(int24(int256(r >> 16) % 500) * sp - 250 * sp, sp);
        int24 hi = lo + int24(int256(1 + (r >> 48) % 200)) * sp;
        uint128 Lc = uint128(1e15 + (r >> 80) % 1e21);

        uint160 tSqrt = TickMath.getSqrtPriceAtTick(target);
        DepthLib.Measurement memory a = DepthLib.walk(_params(tSqrt, 0));
        _addPosition(lo, hi, Lc);
        DepthLib.Measurement memory b = DepthLib.walk(_params(tSqrt, 0));
        assertTrue(a.reachedPrice && b.reachedPrice, "setup: walk did not complete");

        uint256 closed = DepthLib.contribution(Lc, lo, hi, anchor, target);
        uint256 viaWalks = b.depth - a.depth;
        uint256 err = viaWalks > closed ? viaWalks - closed : closed - viaWalks;
        assertLe(err, a.steps + b.steps, "contribution off by more than the rounding bound: linearity broke");

        st.cases++;
        if (err == 0) st.exact++;
        if (err > st.maxAbsErr) st.maxAbsErr = err;
        if (b.steps > st.maxSteps) st.maxSteps = b.steps;
        if (closed > 0) st.nonZeroContrib++;
        if (closed > 0 && err == 0) st.exactNonZero++;
        if (up ? hi > target : lo < target) st.bandPastTarget++;
        if (up ? (lo >= target || hi <= anchor) : (hi <= target || lo >= anchor)) st.bandOutside++;
    }

    /// forge-config: default.fuzz.runs = 2000
    function testFuzz_contributionIsClosedForm(uint256 seed, bool up) public {
        Stats memory st;
        _case(seed, up, st);
    }

    /// @notice Deterministic sweep that writes the artifact. Asserts its own coverage: bands past the target and
    /// bands entirely outside the walked range must both occur, and most contributions must be non-zero.
    function test_emitContributionArtifact() public {
        Stats memory st;
        for (uint256 seed; seed < 200; seed++) {
            _case(seed, true, st);
            _case(seed, false, st);
        }
        assertGt(st.bandPastTarget, 20, "vacuous: no bands past the target");
        assertGt(st.bandOutside, 20, "vacuous: no bands outside the range");
        assertGt(st.nonZeroContrib, st.cases / 3, "vacuous: contributions mostly zero");
        string memory j = string.concat(
            '{"cases":', vm.toString(st.cases), ',"exact_to_the_unit":', vm.toString(st.exact),
            ',"exact_among_non_zero_contributions":', vm.toString(st.exactNonZero),
            ',"max_abs_error_units":', vm.toString(st.maxAbsErr), ',"max_steps_with_band":', vm.toString(st.maxSteps),
            ',"non_zero_contribution":', vm.toString(st.nonZeroContrib),
            ',"bands_past_target":', vm.toString(st.bandPastTarget), ',"bands_outside_range":', vm.toString(st.bandOutside),
            ',"bound":"|two-walk difference - closed form| <= steps(without band) + steps(with band), D1.2"}'
        );
        vm.writeFile("data/phase1_contribution.json", string.concat(j, "\n"));
    }
}
