// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PoolHarness} from "../harness/PoolHarness.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";

/// @notice The closed-form contribution, against an exact rational reference (sim/ffi_contribution.py, Fraction
/// arithmetic on the spec tick prices) and against the two-walk difference measure(book + band) − measure(book) on the
/// real PoolManager (D1.2).
///
/// Ground truth T is the real-valued contribution. The closed form rounds ONCE, down: it must equal floor(T). Each walk
/// rounds every step up, so the two-walk difference lies in (T − steps(without), T + steps(with)): it is the noisier
/// estimator, and |difference − closed| <= max(steps(without) − 1, steps(with)) <= MAX_STEPS. If linearity broke (a
/// different path with and without the band) the error would scale with the band, not the step count, and the
/// two-walk test fails loudly.
contract ContributionTest is PoolHarness {
    using StateLibrary for IPoolManager;

    struct Stats {
        uint256 cases;
        uint256 closedIsFloor;
        uint256 exactToTheUnit; // two-walk difference == closed
        uint256 nonZero;
        uint256 maxErrVsClosed; // |two-walk − closed|
        uint256 maxErrVsTruth; // ceil |two-walk − T|
        uint256 maxProvenBound; // max(steps(without) − 1, steps(with)) of the case where it was largest
        uint256 maxSteps;
        uint256 bandPastTarget;
        uint256 bandOutside;
    }

    struct Band {
        int24 lo;
        int24 hi;
        uint128 liq;
        int24 anchor;
        int24 target;
    }

    function setUp() public {
        _setUpManager();
    }

    // ---------------------------------------------------------------- the reference

    function _truth(uint128 L, int24 lo, int24 hi, int24 anchor, int24 target)
        internal
        returns (uint256 floorT, bool integral)
    {
        string[] memory cmd = new string[](4);
        cmd[0] = "python3";
        cmd[1] = "sim/ffi_contribution.py";
        cmd[2] = "c";
        cmd[3] = string.concat(
            vm.toString(uint256(L)), ",", vm.toString(int256(lo)), ",", vm.toString(int256(hi)), ",",
            vm.toString(int256(anchor)), ",", vm.toString(int256(target))
        );
        (floorT, integral) = abi.decode(vm.ffi(cmd), (uint256, bool));
    }

    function _truthWalk(Band memory b, uint256 diff, uint256 sWithout, uint256 sWith)
        internal
        returns (uint256 floorT, bool above, bool below, uint256 errVsTruth)
    {
        string[] memory cmd = new string[](4);
        cmd[0] = "python3";
        cmd[1] = "sim/ffi_contribution.py";
        cmd[2] = "w";
        cmd[3] = string.concat(
            vm.toString(uint256(b.liq)), ",", vm.toString(int256(b.lo)), ",", vm.toString(int256(b.hi)), ",",
            vm.toString(int256(b.anchor)), ",", vm.toString(int256(b.target)), ",", vm.toString(diff), ",",
            vm.toString(sWithout), ",", vm.toString(sWith)
        );
        (floorT,, above, below, errVsTruth) = abi.decode(vm.ffi(cmd), (uint256, bool, bool, bool, uint256));
    }

    function _bandArgs(uint128 L, int24 a, int24 b, int24 anchor, int24 target)
        internal
        pure
        returns (uint128, int24, int24, int24, int24)
    {
        L = uint128(bound(L, 1, type(uint128).max));
        a = int24(bound(a, TickMath.MIN_TICK, TickMath.MAX_TICK));
        b = int24(bound(b, TickMath.MIN_TICK, TickMath.MAX_TICK));
        if (a > b) (a, b) = (b, a);
        if (a == b) b = a == TickMath.MAX_TICK ? b : b + 1;
        if (a == b) a = a - 1;
        anchor = int24(bound(anchor, TickMath.MIN_TICK, TickMath.MAX_TICK));
        target = int24(bound(target, TickMath.MIN_TICK, TickMath.MAX_TICK));
        return (L, a, b, anchor, target);
    }

    // ---------------------------------------------------------------- the closed form against ground truth

    /// @notice The property a mandate depends on: the closed form never credits a curator with more depth than the
    /// band really provides. Fails by name if either `false` rounding flag in DepthLib.contribution is flipped.
    /// Asserted against the rational reference, NOT against the two-walk difference: that difference can itself land
    /// below truth, so contribution <= difference does not follow and would be the wrong test.
    /// forge-config: default.fuzz.runs = 500
    /// forge-config: ci.fuzz.runs = 2000
    function testFuzz_contributionNeverOverCredits(uint128 L, int24 a, int24 b, int24 anchor, int24 target) public {
        (L, a, b, anchor, target) = _bandArgs(L, a, b, anchor, target);
        uint256 closed = DepthLib.contribution(L, a, b, anchor, target);
        (uint256 floorT,) = _truth(L, a, b, anchor, target);
        assertLe(closed, floorT, "contribution over-credits: closed form exceeds the real-valued contribution");
    }

    /// @notice Exact to one rounding: |closed − T| < 1. Deliberately direction-blind, and so it keeps passing if the
    /// rounding flags are flipped (closed = ceil(T) is also within one unit). That is why the direction has its own
    /// test above; the mutation transcript shows the split.
    /// forge-config: default.fuzz.runs = 500
    /// forge-config: ci.fuzz.runs = 2000
    function testFuzz_contributionWithinOneUnitOfTruth(uint128 L, int24 a, int24 b, int24 anchor, int24 target)
        public
    {
        (L, a, b, anchor, target) = _bandArgs(L, a, b, anchor, target);
        uint256 closed = DepthLib.contribution(L, a, b, anchor, target);
        (uint256 floorT, bool integral) = _truth(L, a, b, anchor, target);
        if (integral) assertEq(closed, floorT, "closed form != T where T is an integer");
        else assertTrue(closed == floorT || closed == floorT + 1, "closed form more than one unit from T");
    }

    /// @notice The sqrt-price form prove() calls, with the anchor at spot: never over-credits, over the whole
    /// protocol sqrt-price domain and full uint128 liquidity.
    /// forge-config: default.fuzz.runs = 500
    /// forge-config: ci.fuzz.runs = 2000
    function testFuzz_contributionAtNeverOverCredits(uint128 L, uint160 a, uint160 b, uint160 anchor, uint160 target)
        public
    {
        L = uint128(bound(L, 1, type(uint128).max));
        a = uint160(bound(a, TickMath.MIN_SQRT_PRICE, TickMath.MAX_SQRT_PRICE - 1));
        b = uint160(bound(b, TickMath.MIN_SQRT_PRICE, TickMath.MAX_SQRT_PRICE - 1));
        if (a > b) (a, b) = (b, a);
        if (a == b) b = b + 1;
        anchor = uint160(bound(anchor, TickMath.MIN_SQRT_PRICE, TickMath.MAX_SQRT_PRICE - 1));
        target = uint160(bound(target, TickMath.MIN_SQRT_PRICE, TickMath.MAX_SQRT_PRICE - 1));
        uint256 closed = DepthLib.contributionAt(L, a, b, anchor, target);
        string[] memory cmd = new string[](4);
        cmd[0] = "python3";
        cmd[1] = "sim/ffi_contribution.py";
        cmd[2] = "s";
        cmd[3] = string.concat(
            vm.toString(uint256(L)), ",", vm.toString(uint256(a)), ",", vm.toString(uint256(b)), ",",
            vm.toString(uint256(anchor)), ",", vm.toString(uint256(target))
        );
        (uint256 floorT,) = abi.decode(vm.ffi(cmd), (uint256, bool));
        assertLe(closed, floorT, "contributionAt over-credits: closed form exceeds the real-valued contribution");
        assertEq(closed, floorT, "contributionAt is not floor(T)");
    }

    // ---------------------------------------------------------------- the two-walk difference on the real pool

    function _shallowBook(uint256 seed) internal returns (Band memory b, bool up) {
        _setUpManager();
        _newPool(seed % 4, 0);
        int24 sp = pkey.tickSpacing;
        _randomBook(seed, 1 + seed % 6, 300 * sp);
        up = uint256(keccak256(abi.encode(seed, "dir"))) % 2 == 0;
        uint256 r = uint256(keccak256(abi.encode(seed, "band")));
        (, b.anchor,,) = manager.getSlot0(pid); // initialised exactly at tick 0: spot is a tick price
        int24 dist = int24(int256(1 + r % 200)) * sp;
        b.target = up ? b.anchor + dist : b.anchor - dist;
        // anywhere in [-250, 250]·sp: may straddle the anchor or the target, lie past the target, or miss entirely
        b.lo = _align(int24(int256(r >> 16) % 500) * sp - 250 * sp, sp);
        b.hi = b.lo + int24(int256(1 + (r >> 48) % 200)) * sp;
        b.liq = uint128(1e15 + (r >> 80) % 1e21);
    }

    /// @dev A ladder of `k` one-spacing rungs between spot and the target, k in [50, 116]: the walk takes ~k steps,
    /// close to MAX_STEPS = 128 but under it, so both walks complete. These are the cases the shallow books never reach.
    function _deepBook(uint256 seed) internal returns (Band memory b, bool up) {
        _setUpManager();
        _newPool(seed % 4, 0);
        int24 sp = pkey.tickSpacing;
        uint256 k = 50 + seed % 67;
        up = uint256(keccak256(abi.encode(seed, "dir"))) % 2 == 0;
        _addPosition(-2 * sp, 2 * sp, 1e18);
        for (uint256 i; i < k; i++) {
            uint256 ri = uint256(keccak256(abi.encode(seed, i)));
            int24 a = up ? int24(int256(2 + i)) * sp : -int24(int256(3 + i)) * sp;
            _addPosition(a, a + sp, uint128(1e17 + ri % 1e21));
        }
        b.anchor = 0;
        int24 span = int24(int256(k)) + 4;
        b.target = up ? span * sp : -(span + 1) * sp;
        uint256 r = uint256(keccak256(abi.encode(seed, "band")));
        b.lo = int24(int256(r % uint256(int256(2 * span + 20)))) * sp - (span + 10) * sp;
        b.hi = b.lo + int24(int256(1 + (r >> 32) % uint256(int256(span)))) * sp;
        b.liq = uint128(1e15 + (r >> 80) % 1e21);
    }

    function _twoWalk(Band memory b, bool up, Stats memory st) internal {
        uint160 tSqrt = TickMath.getSqrtPriceAtTick(b.target);
        DepthLib.Measurement memory w0 = DepthLib.walk(_params(tSqrt, 0));
        _addPosition(b.lo, b.hi, b.liq);
        DepthLib.Measurement memory w1 = DepthLib.walk(_params(tSqrt, 0));
        assertTrue(w0.reachedPrice && w1.reachedPrice, "setup: walk did not complete");
        // _addPosition aligns: use exactly the band the pool holds
        int24 sp = pkey.tickSpacing;
        b.lo = _align(b.lo, sp);
        b.hi = _align(b.hi, sp);
        if (b.hi <= b.lo) b.hi = b.lo + sp;

        uint256 closed = DepthLib.contribution(b.liq, b.lo, b.hi, b.anchor, b.target);
        uint256 diff = w1.depth - w0.depth;
        (uint256 floorT, bool above, bool below, uint256 errT) = _truthWalk(b, diff, w0.steps, w1.steps);
        assertTrue(above && below, "two-walk difference outside (T - steps(without), T + steps(with)): linearity broke");
        uint256 err = diff > closed ? diff - closed : closed - diff;
        uint256 bnd = w0.steps - 1 > w1.steps ? w0.steps - 1 : w1.steps;
        assertLe(err, bnd, "|two-walk - closed| exceeds max(steps(without) - 1, steps(with))");

        st.cases++;
        if (closed == floorT) st.closedIsFloor++;
        if (err == 0) st.exactToTheUnit++;
        if (closed > 0) st.nonZero++;
        if (err > st.maxErrVsClosed) st.maxErrVsClosed = err;
        if (errT > st.maxErrVsTruth) st.maxErrVsTruth = errT;
        if (bnd > st.maxProvenBound) st.maxProvenBound = bnd;
        if (w1.steps > st.maxSteps) st.maxSteps = w1.steps;
        if (up ? b.hi > b.target : b.lo < b.target) st.bandPastTarget++;
        if (up ? (b.lo >= b.target || b.hi <= b.anchor) : (b.hi <= b.target || b.lo >= b.anchor)) st.bandOutside++;
    }

    /// forge-config: default.fuzz.runs = 300
    /// forge-config: ci.fuzz.runs = 1000
    function testFuzz_twoWalkWithinProvenBound(uint256 seed) public {
        Stats memory st;
        (Band memory b, bool up) = _shallowBook(seed);
        _twoWalk(b, up, st);
    }

    /// forge-config: default.fuzz.runs = 50
    /// forge-config: ci.fuzz.runs = 200
    function testFuzz_twoWalkWithinProvenBound_deep(uint256 seed) public {
        Stats memory st;
        (Band memory b, bool up) = _deepBook(seed);
        _twoWalk(b, up, st);
    }

    function _json(string memory name, Stats memory s) internal pure returns (string memory) {
        return string.concat(
            '"', name, '":{"cases":', vm.toString(s.cases), ',"closed_form_equals_floor_of_truth":',
            vm.toString(s.closedIsFloor), ',"non_zero_contribution":', vm.toString(s.nonZero),
            ',"two_walk_equals_closed_to_the_unit":', vm.toString(s.exactToTheUnit),
            ',"max_abs_two_walk_minus_closed":', vm.toString(s.maxErrVsClosed),
            ',"max_abs_two_walk_minus_truth_ceil":', vm.toString(s.maxErrVsTruth),
            ',"max_proven_bound_in_set":', vm.toString(s.maxProvenBound), ',"max_steps_with_band":',
            vm.toString(s.maxSteps), ',"bands_past_target":', vm.toString(s.bandPastTarget),
            ',"bands_outside_range":', vm.toString(s.bandOutside), "}"
        );
    }

    /// @notice Deterministic sweep that writes data/phase1_contribution.json. Asserts its own claims and coverage: the
    /// closed form equals floor(T) in every case, the deep set really is deep (steps above 100), and both band
    /// geometries that could hide a defect (past the target, outside the walked range) occur.
    function test_emitContributionArtifact() public {
        Stats memory sh;
        Stats memory dp;
        for (uint256 seed; seed < 300; seed++) {
            (Band memory b, bool up) = _shallowBook(seed);
            _twoWalk(b, up, sh);
        }
        for (uint256 seed; seed < 100; seed++) {
            (Band memory b, bool up) = _deepBook(seed);
            _twoWalk(b, up, dp);
        }
        assertEq(sh.closedIsFloor, sh.cases, "closed form != floor(T) on a shallow case");
        assertEq(dp.closedIsFloor, dp.cases, "closed form != floor(T) on a deep case");
        assertGt(dp.maxSteps, 100, "vacuous: the deep set is not deep");
        assertGt(sh.bandPastTarget + dp.bandPastTarget, 40, "vacuous: no bands past the target");
        assertGt(sh.bandOutside + dp.bandOutside, 40, "vacuous: no bands outside the range");
        assertGt(sh.nonZero + dp.nonZero, (sh.cases + dp.cases) / 3, "vacuous: contributions mostly zero");
        string memory j = string.concat(
            "{", _json("shallow", sh), ",", _json("deep", dp), ',"max_steps":', vm.toString(DepthLib.MAX_STEPS),
            ',"proven":{"closed_form":"floor(T): one rounding, down, never over-credits",',
            '"two_walk":"in (T - steps(without), T + steps(with))",',
            '"two_walk_vs_closed":"|diff - closed| <= max(steps(without) - 1, steps(with)) <= MAX_STEPS = 128 units"},',
            '"reference":"sim/ffi_contribution.py, Fraction arithmetic on spec tick prices","decision":"D1.2"}'
        );
        vm.writeFile("data/phase1_contribution.json", string.concat(j, "\n"));
    }
}
