// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {FixedPointMathLib} from "solmate/src/utils/FixedPointMathLib.sol";
import {KeelHook} from "../../src/KeelHook.sol";
import {KeelMeter} from "../../src/KeelMeter.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";
import {MandateLib} from "../../src/libraries/MandateLib.sol";
import {KeelHarness} from "../harness/KeelHarness.sol";
import {MandateHandlerBase} from "./MandateHandlerBase.sol";

/// @notice D2.10's adversary: dust on every tick of the mandated range (laid in setUp: the range is already
/// fragmented), against a curator who goes short and recovers. Before every proof the handler measures each short side itself, at the proof's segment budget, through the
/// same meter: when every short side is capped, the proof must not breach and must not move the window toward a
/// slash. A proof that breaches where one segment would have capped is the covered case D2.10 exists for.
contract FragmentationHandler is MandateHandlerBase {
    using StateLibrary for IPoolManager;

    uint256 public cappedProofs; // proofs where every short side was capped at the budget used
    uint256 public coveredBreaches; // breaches that needed more than one segment

    constructor(Env memory e) MandateHandlerBase(e) {}

    function roll(uint256 k) external {
        _roll(k);
    }

    /// Jump N blocks once a window is open (and the run's other premises are met), so capped proofs after N are checked for not finalising, and covered
    /// ones can finalise. Ordinary rolls never get there with N at its maximum.
    function skip() external {
        (,,,,, uint64 opened,) = hook.mandate(id);
        // a slash ends the run's mandate, so finalisation is explored only after the run has paid, been capped and
        // breached under coverage; otherwise an early slash leaves every other premise unexercised
        if (opened == 0 || provedBlocks == 0 || cappedProofs == 0 || coveredBreaches == 0) return;
        blk += hook.terms(id).stalenessBlocks;
        vm.roll(blk);
        _after();
    }

    function strengthen() external {
        _roll(1);
        KeelHook.Band[] memory b = new KeelHook.Band[](1);
        b[0] = KeelHook.Band({lower: -400, upper: 400, liquidity: 1e20});
        _reshape(b);
        _roll(1);
        _prove(true, 1); // an honest curator proves once its depth is back
    }

    /// The curator goes short; a watcher reacts in the next block, half the time with one walk (capped by the dust)
    /// and half with every segment (covered).
    function weaken(uint256 seed) external {
        _roll(1);
        KeelHook.Band[] memory b = new KeelHook.Band[](1);
        b[0] = KeelHook.Band({lower: -30, upper: 30, liquidity: 1e18});
        _reshape(b);
        _roll(1);
        _proveChecked(_mix(seed) % 2 == 0 ? 1 : DepthLib.MAX_SEGMENTS, false);
    }

    function proveSegments(uint256 seed, bool byCurator) external {
        uint256 r = _mix(seed);
        _proveChecked(r % 2 == 0 ? 1 : 1 + (r >> 8) % DepthLib.MAX_SEGMENTS, byCurator); // half single-walk
    }

    function _proveChecked(uint256 segs, bool byCurator) internal {
        if (status() != MandateLib.Status.ACTIVE) return;
        (, uint256 cUp, uint256 cDown) = hook.preview(id);
        KeelHook.Terms memory t = hook.terms(id);
        bool shortUp = cUp < t.depth1;
        bool shortDown = cDown < t.depth0;
        bool cappedUp = shortUp && _capped(true, segs);
        bool cappedDown = shortDown && _capped(false, segs);
        bool allShortCapped = (shortUp || shortDown) && (!shortUp || cappedUp) && (!shortDown || cappedDown);
        bool oneSegCaps = (shortUp && _capped(true, 1)) || (shortDown && _capped(false, 1));
        (,,,,, uint64 openedBefore,) = hook.mandate(id);

        DepthLib.Verdict v = _prove(byCurator, segs);

        if (allShortCapped) {
            cappedProofs++;
            (,,,,, uint64 openedAfter,) = hook.mandate(id);
            if (v == DepthLib.Verdict.BREACH) _fail("a proof whose short sides were all capped breached");
            if (openedBefore == 0 && openedAfter != 0) _fail("a capped proof opened a window");
            if (status() == MandateLib.Status.SLASHED) _fail("a capped proof finalised a breach");
        } else if (v == DepthLib.Verdict.BREACH && segs > 1 && oneSegCaps) {
            coveredBreaches++;
        }
    }

    /// The same walk prove() runs for one side, measured here at `segs` segments.
    function _capped(bool up, uint256 segs) internal view returns (bool) {
        (uint160 sp, int24 tick,, uint24 fee) = manager.getSlot0(pid);
        KeelHook.Terms memory t = hook.terms(id);
        uint256 f = FixedPointMathLib.sqrt((up ? 10_000 + uint256(t.xBps) : 10_000 - uint256(t.xBps)) * 1e32);
        uint160 target = uint160(FullMath.mulDiv(sp, f, 1e18));
        (DepthLib.Measurement memory m,) = KeelMeter(address(hook.meter())).walkSegments(
            DepthLib.DepthParams({
                sqrtPriceStartX96: sp,
                tickStart: tick,
                liquidityStart: manager.getLiquidity(pid),
                sqrtPriceTargetX96: target,
                depthTarget: up ? t.depth1 : t.depth0,
                feePips: fee,
                tickSpacing: key.tickSpacing,
                source: hook.tickSource(pid)
            }),
            segs
        );
        return m.capped;
    }
}

/// @title Fragmentation invariants (D2.10)
/// @notice No proof whose short sides are all capped breaches, opens a window or finalises; contiguous coverage can
/// breach. Plus the shared checks. Premises: §5's three, verbatim, and two of its own: a capped proof was checked,
/// and a breach that needed segments happened.
/// forge-config: default.invariant.depth = 128
/// forge-config: ci.invariant.runs = 192
contract FragmentationInvariantTest is KeelHarness {
    FragmentationHandler handler;

    function setUp() public {
        _setUpKeel();
        (pkey, pid) = initPool(currency0, currency1, IHooks(address(hook)), 500, 1, SQRT_PRICE_1_1);
        _lp(-6000, 6000, 1e18);
        KeelHook.Terms memory t = _terms(4e17, 4e17);
        t.xBps = 300;
        t.stalenessBlocks = 7_200;
        t.termBlocks = 100_000;
        uint256 id = _post(t);
        _accept(id, _bands1(-400, 400, 1e20)); // starts compliant; `weaken` makes it short
        // the adversary's dust: a 1-wei position on every tick of ±320, so one walk always caps
        for (int24 k = -320; k < 320; k++) {
            _lp(k, k + 1, 1);
        }
        _roll(1);
        handler = new FragmentationHandler(
            MandateHandlerBase.Env({
                hook: hook,
                manager: manager,
                swapRouter: swapRouter,
                lpRouter: modifyLiquidityRouter,
                key: pkey,
                id: id,
                curator: curator,
                watcher: watcher,
                trader: address(this),
                pay: currency1,
                blk: blk
            })
        );
        targetContract(address(handler));
        bytes4[] memory sels = new bytes4[](5);
        sels[0] = FragmentationHandler.roll.selector;
        sels[1] = FragmentationHandler.skip.selector;
        sels[2] = FragmentationHandler.strengthen.selector;
        sels[3] = FragmentationHandler.weaken.selector;
        sels[4] = FragmentationHandler.proveSegments.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: sels}));
    }

    function handlerAddr() public view returns (address) {
        return address(handler);
    }

    function invariant_noViolation() public view {
        assertEq(handler.violation(), "", handler.violation());
    }

    function invariant_payWithinProven() public view {
        (,,,, uint64 paid,,) = hook.mandate(1);
        assertEq(paid, handler.ghostPaid(), "paid blocks diverge from the D2.2 ghost model");
    }

    function afterInvariant() public view {
        // The premise must have been exercised, or the run proves nothing.
        assertGt(handler.provedBlocks(), 0, "vacuous: nothing was ever proved");
        assertGt(handler.breachesObserved(), 0, "vacuous: no breach path exercised");
        assertGt(handler.bondLiquidity(), 0, "vacuous: bond never placed");
        assertGt(handler.cappedProofs(), 0, "vacuous: no proof was ever capped by the dust");
        assertGt(handler.coveredBreaches(), 0, "vacuous: no breach needed segments");
    }
}
