// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {KeelHarness} from "../harness/KeelHarness.sol";
import {KeelHook} from "../../src/KeelHook.sol";
import {MandateLib} from "../../src/libraries/MandateLib.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";
import {PlacementLib} from "../../src/libraries/PlacementLib.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {MockERC20} from "solmate/src/test/utils/mocks/MockERC20.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";

/// @notice Phase 2 lifecycle: every transition, every refusal, both proof paths, the staleness rule, the timing
/// watcher, the JIT adversary, and where value goes on a slash.
contract KeelHookTest is KeelHarness {
    using StateLibrary for IPoolManager;

    function setUp() public {
        _setUpKeel();
    }

    // ================================================================ deployment

    /// @notice The three bits, and every liquidity-lifecycle bit zero, read off the deployed address.
    function test_permissionBitsOnDeployedAddress() public view {
        IHooks h = IHooks(address(hook));
        assertTrue(Hooks.hasPermission(h, Hooks.AFTER_INITIALIZE_FLAG));
        assertTrue(Hooks.hasPermission(h, Hooks.BEFORE_SWAP_FLAG));
        assertTrue(Hooks.hasPermission(h, Hooks.BEFORE_SWAP_RETURNS_DELTA_FLAG));
        uint160 lifecycle = Hooks.BEFORE_ADD_LIQUIDITY_FLAG | Hooks.AFTER_ADD_LIQUIDITY_FLAG
            | Hooks.BEFORE_REMOVE_LIQUIDITY_FLAG | Hooks.AFTER_REMOVE_LIQUIDITY_FLAG
            | Hooks.AFTER_ADD_LIQUIDITY_RETURNS_DELTA_FLAG | Hooks.AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA_FLAG;
        assertEq(uint160(address(hook)) & lifecycle, 0, "a liquidity-lifecycle bit is set");
        assertEq(uint160(address(hook)) & Hooks.ALL_HOOK_MASK, FLAGS, "bits other than the three are set");
    }

    // ================================================================ post

    function test_postEscrowsAsClaimsAndHoldsNoTokens() public {
        uint256 id = _post(_terms(4e17, 4e17));
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.POSTED));
        assertEq(_claims(address(hook), currency1), 2e18, "escrow not held as ERC-6909 claims");
        assertEq(MockERC20(Currency.unwrap(currency1)).balanceOf(address(hook)), 0, "hook holds tokens");
        assertEq(hook.liveMandate(pid), id);
    }

    function test_postRefusals() public {
        KeelHook.Terms memory t = _terms(4e17, 4e17);
        vm.startPrank(issuer);
        t.stalenessBlocks = 299;
        vm.expectRevert(KeelHook.BadTerms.selector);
        hook.post(pkey, t);
        t.stalenessBlocks = 7_201;
        vm.expectRevert(KeelHook.BadTerms.selector);
        hook.post(pkey, t);
        t = _terms(4e17, 4e17);
        t.xBps = 0;
        vm.expectRevert(KeelHook.BadTerms.selector);
        hook.post(pkey, t);
        t.xBps = 5_001;
        vm.expectRevert(KeelHook.BadTerms.selector);
        hook.post(pkey, t);
        t = _terms(0, 0);
        vm.expectRevert(KeelHook.BadTerms.selector);
        hook.post(pkey, t);
        t = _terms(4e17, 4e17);
        t.retainerPerBlock = 0;
        vm.expectRevert(KeelHook.BadTerms.selector);
        hook.post(pkey, t);
        t = _terms(4e17, 4e17);
        t.termBlocks = 0;
        vm.expectRevert(KeelHook.BadTerms.selector);
        hook.post(pkey, t);
        t = _terms(4e17, 4e17);
        t.payCurrency = Currency.wrap(address(0xBEEF));
        vm.expectRevert(KeelHook.BadTerms.selector);
        hook.post(pkey, t);
        t = _terms(4e17, 4e17);
        t.acceptDeadline = uint64(blk - 1);
        vm.expectRevert(KeelHook.BadTerms.selector);
        hook.post(pkey, t);
        PoolKey memory other = pkey;
        other.fee = 500;
        other.tickSpacing = 10;
        vm.expectRevert(KeelHook.UnknownPool.selector);
        hook.post(other, _terms(4e17, 4e17));
        hook.post(pkey, _terms(4e17, 4e17));
        vm.expectRevert(KeelHook.PoolBusy.selector);
        hook.post(pkey, _terms(4e17, 4e17));
        vm.stopPrank();
    }

    // ================================================================ accept

    function test_acceptMintsBandsAndBond() public {
        uint256 id = _standard();
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.ACTIVE));
        assertEq(_posLiq(-300, 300, PlacementLib.mandateSalt(id)), 1e20, "mandate band missing");
        assertEq(_bondLiq(id), 1e18, "bond missing");
        (int24 lo, int24 hi,) = hook.bondOf(id);
        assertEq(lo, -300, "bond not at the first band");
        assertEq(hi, 300, "bond not at the first band");
    }

    function test_acceptRefusals() public {
        uint256 id = _post(_terms(4e17, 4e17));
        vm.startPrank(curator);
        vm.expectRevert(KeelHook.BadTerms.selector);
        hook.accept(id, _bands1(-300, 300, 1e20), 1e18 - 1); // bond below minimum
        vm.expectRevert(KeelHook.BadBands.selector);
        hook.accept(id, _bands1(-301, 300, 1e20), 1e18); // unaligned
        vm.expectRevert(KeelHook.BadBands.selector);
        hook.accept(id, _bands1(300, -300, 1e20), 1e18); // inverted
        vm.expectRevert(KeelHook.BadBands.selector);
        hook.accept(id, _bands1(-300, 300, 0), 1e18); // empty
        vm.expectRevert(KeelHook.BadBands.selector);
        hook.accept(id, new KeelHook.Band[](0), 1e18); // none
        KeelHook.Band[] memory five = new KeelHook.Band[](5);
        for (uint256 i; i < 5; i++) {
            five[i] = KeelHook.Band({lower: -300, upper: 300, liquidity: 1e18});
        }
        vm.expectRevert(KeelHook.BadBands.selector);
        hook.accept(id, five, 1e18); // above MAX_BANDS
        vm.stopPrank();
        _roll(101);
        vm.prank(curator);
        vm.expectRevert(KeelHook.BadState.selector);
        hook.accept(id, _bands1(-300, 300, 1e20), 1e18); // past deadline
    }

    function test_expireRefundsIssuer() public {
        uint256 id = _post(_terms(4e17, 4e17));
        vm.expectRevert(KeelHook.TooEarly.selector);
        hook.expire(id);
        _roll(101);
        hook.expire(id);
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.EXPIRED));
        assertEq(_claims(issuer, currency1), 2e18, "escrow not refunded");
        assertEq(_claims(address(hook), currency1), 0);
        assertEq(hook.liveMandate(pid), 0);
    }

    // ================================================================ prove: the curator earns

    function test_curatorProvesToEarn_oncePerBlock() public {
        uint256 id = _standard();
        _roll(1);
        assertEq(uint8(_prove(curator, id)), uint8(DepthLib.Verdict.COMPLIANT));
        assertEq(_accrued(id), 1e15);
        assertEq(uint8(_prove(watcher, id)), uint8(DepthLib.Verdict.COMPLIANT)); // same block
        assertEq(_accrued(id), 1e15, "paid twice in one block");
        _roll(1);
        _prove(watcher, id); // anyone's compliant proof pays the curator
        assertEq(_accrued(id), 2e15);
        assertEq(_paidBlocks(id), 2);
        assertEq(_escrow(id), 2e18 - 2e15);
    }

    /// @notice Depth that is in the pool but not under the mandate's salt pays nothing and slashes nobody.
    function test_poolDeepThroughOthersIsInconclusive() public {
        _lp(-6000, 6000, 1e21); // a third party makes the pool deep
        uint256 id = _post(_terms(4e17, 4e17));
        _accept(id, _bands1(3000, 3600, 1e20)); // curator's band is nowhere near spot
        _roll(1);
        assertEq(uint8(_prove(watcher, id)), uint8(DepthLib.Verdict.INCONCLUSIVE));
        assertEq(_accrued(id), 0, "paid for someone else's depth");
        assertEq(_openedAt(id), 0, "opened a window while the pool was deep");
    }

    function test_failedProveCostsOnlyGas() public {
        _lp(-6000, 6000, 1e21);
        uint256 id = _post(_terms(4e17, 4e17));
        _accept(id, _bands1(3000, 3600, 1e20));
        _roll(1);
        uint256 b0 = MockERC20(Currency.unwrap(currency0)).balanceOf(watcher);
        uint256 b1 = MockERC20(Currency.unwrap(currency1)).balanceOf(watcher);
        _prove(watcher, id);
        assertEq(MockERC20(Currency.unwrap(currency0)).balanceOf(watcher), b0);
        assertEq(MockERC20(Currency.unwrap(currency1)).balanceOf(watcher), b1);
        assertEq(_claims(watcher, currency1), 0);
    }

    // ================================================================ breach and the staleness rule

    function _breachSetup() internal returns (uint256 id) {
        id = _standard();
        _roll(1);
        _prove(curator, id);
        _roll(1);
        _prove(curator, id);
        _roll(1);
        _prove(curator, id); // accrued 3e15
        _pushTo(900); // the curator's ±300 band is left behind
        _roll(1);
    }

    function test_breachNeedsTwoProofsNApart() public {
        uint256 id = _breachSetup();
        uint64 opened = uint64(blk);
        assertEq(uint8(_prove(watcher, id)), uint8(DepthLib.Verdict.BREACH));
        assertEq(_openedAt(id), opened, "window not opened");
        _roll(N - 1);
        _prove(watcher, id);
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.ACTIVE), "finalised before N");
        _roll(1);
        _prove(watcher, id);
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.SLASHED), "not finalised at N");
    }

    function test_slashSplitsAccruedAndRefundsIssuer() public {
        uint256 id = _breachSetup();
        _prove(watcher, id);
        _roll(N);
        (uint256 g0, uint256 g1) = manager.getFeeGrowthGlobals(pid);
        _prove(watcher, id);
        assertEq(_claims(watcher, currency1), 3e15 * 2_000 / 10_000, "bounty");
        assertEq(_claims(issuer, currency1), 2e18 - 3e15, "issuer refund");
        assertEq(_claims(address(hook), currency1), 0, "value left in the hook");
        (uint256 p0, uint256 p1) = hook.pendingDonation(pid);
        assertEq(p0 + p1, 0, "donation not made");
        (uint256 h0, uint256 h1) = manager.getFeeGrowthGlobals(pid);
        assertEq(h0, g0);
        assertGt(h1, g1, "donation did not reach in-range LPs");
    }

    /// @notice Gate 2 adversary row (D1.3): the timing watcher. A window opened, a compliant stretch proved inside
    /// it, and a closing proof at open + N must NOT slash.
    function test_timingWatcher_failsWhenAnyCompliantProofInside() public {
        uint256 id = _breachSetup();
        uint64 opened = uint64(blk);
        _prove(watcher, id);
        _roll(10);
        vm.prank(curator);
        hook.reshape(id, _bands1(600, 1200, 1e20)); // follow the price
        _roll(1);
        assertEq(uint8(_prove(curator, id)), uint8(DepthLib.Verdict.COMPLIANT));
        assertEq(_openedAt(id), 0, "compliant proof did not close the window");
        _roll(1);
        vm.prank(curator);
        hook.reshape(id, _bands1(-300, 300, 1e20)); // ... and later leave again
        _roll(opened + N - blk); // the watcher's closing proof, exactly N after its opening
        _prove(watcher, id);
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.ACTIVE), "slashed across a proved-compliant stretch");
    }

    /// @notice ... and succeeds only when no compliant proof exists inside the window. A compliant stretch nobody
    /// proved is the stated liveness assumption (D1.3), not a defect.
    function test_timingWatcher_succeedsOnlyWhenNoneInside() public {
        uint256 id = _breachSetup();
        _prove(watcher, id);
        _roll(10);
        vm.prank(curator);
        hook.reshape(id, _bands1(600, 1200, 1e20)); // compliant, but never proved
        _roll(10);
        vm.prank(curator);
        hook.reshape(id, _bands1(-300, 300, 1e20));
        _roll(N);
        _prove(watcher, id);
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.SLASHED));
    }

    function test_inconclusiveNeitherOpensNorCloses() public {
        uint256 id = _breachSetup();
        _prove(watcher, id);
        uint64 opened = _openedAt(id);
        _lp(-6000, 6000, 1e21); // a third party fills the pool: the next proof is INCONCLUSIVE
        _roll(1);
        assertEq(uint8(_prove(watcher, id)), uint8(DepthLib.Verdict.INCONCLUSIVE));
        assertEq(_openedAt(id), opened, "an inconclusive proof moved the window");
    }

    // ================================================================ the JIT adversary (D2.2)

    /// @notice Depth added at the sample is not credited in the block it arrives.
    function test_jit_reshapeThenProveSameBlockIsUncredited() public {
        _lp(-6000, 6000, 1e18);
        uint256 id = _post(_terms(4e17, 4e17));
        _accept(id, _bands1(3000, 3600, 1e20)); // away from spot
        _roll(1);
        vm.prank(curator);
        hook.reshape(id, _bands1(-300, 300, 1e20)); // JIT in
        assertEq(uint8(_prove(curator, id)), uint8(DepthLib.Verdict.COMPLIANT));
        assertEq(_accrued(id), 0, "credited depth added in the same block");
    }

    /// @notice ... and depth pulled after the sample voids the credit it earned.
    function test_jit_proveThenPullSameBlockIsVoided() public {
        uint256 id = _standard();
        _roll(1);
        _prove(curator, id);
        assertEq(_accrued(id), 1e15);
        vm.prank(curator);
        hook.reshape(id, _bands1(3000, 3600, 1e20)); // JIT out
        assertEq(_accrued(id), 0, "credit survived the pull");
        assertEq(_escrow(id), 2e18, "escrow not restored");
        assertEq(_paidBlocks(id), 0);
    }

    /// @notice ... and a window closed by a proof whose depth is pulled in the same block is reopened at its
    /// original block, so the watcher still finalises at open + N.
    function test_jit_voidedCloseRestoresWindow() public {
        uint256 id = _breachSetup();
        uint64 opened = uint64(blk);
        _prove(watcher, id);
        _roll(10);
        vm.prank(curator);
        hook.reshape(id, _bands1(600, 1200, 1e20)); // in
        _roll(1);
        _prove(curator, id); // closes
        assertEq(_openedAt(id), 0);
        vm.prank(curator);
        hook.reshape(id, _bands1(-300, 300, 1e20)); // out, same block
        assertEq(_openedAt(id), opened, "window not restored");
        _roll(N - 11);
        _prove(watcher, id);
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.SLASHED));
    }

    // ================================================================ positions after a slash

    function test_slashNeverReducesLiquidity_bandsFrozen() public {
        uint256 id = _breachSetup();
        uint128 bond = _bondLiq(id);
        uint128 band = _posLiq(-300, 300, PlacementLib.mandateSalt(id));
        _prove(watcher, id);
        _roll(N);
        _prove(watcher, id);
        assertEq(_bondLiq(id), bond, "bond liquidity changed");
        assertEq(_posLiq(-300, 300, PlacementLib.mandateSalt(id)), band, "mandate band liquidity changed");
        vm.prank(curator);
        vm.expectRevert(KeelHook.BadState.selector);
        hook.reshape(id, _bands1(600, 1200, 1e20));
    }

    function test_onlyCuratorReshapes() public {
        uint256 id = _standard();
        vm.prank(stranger);
        vm.expectRevert(KeelHook.NotCurator.selector);
        hook.reshape(id, _bands1(600, 1200, 1e20));
    }

    // ================================================================ donation guard (the Hydra defect)

    function test_donationParksWithZeroActiveLiquidity() public {
        _lp(-600, 600, 1e18);
        uint256 id = _post(_terms(0, 4e17)); // up side only
        _accept(id, _bands1(-300, 300, 1e20));
        _roll(1);
        _prove(curator, id);
        _pushTo(1200); // past every position: active liquidity is zero
        assertEq(manager.getLiquidity(pid), 0, "setup: liquidity in range");
        _roll(1);
        _prove(watcher, id);
        _roll(N);
        _prove(watcher, id); // must not revert
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.SLASHED));
        (, uint256 p1) = hook.pendingDonation(pid);
        assertEq(p1, 1e15 - 1e15 * 2_000 / 10_000, "donation not parked");
        _lp(1140, 1260, 1e18); // someone returns to the price
        hook.flushDonation(pid);
        (, p1) = hook.pendingDonation(pid);
        assertEq(p1, 0, "parked donation not flushed");
        assertEq(_claims(address(hook), currency1), 0);
    }

    // ================================================================ settlement

    function test_settleByCurator() public {
        uint256 id = _standard();
        _roll(1);
        _prove(curator, id);
        vm.prank(curator);
        vm.expectRevert(KeelHook.TooEarly.selector);
        hook.settle(id);
        _roll(2_000);
        vm.expectRevert(KeelHook.TermOver.selector);
        _prove(watcher, id);
        uint256 t0 = MockERC20(Currency.unwrap(currency0)).balanceOf(curator);
        vm.prank(curator);
        hook.settle(id);
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.SETTLED));
        assertEq(_claims(curator, currency1), 1e15, "accrued not vested");
        assertEq(_claims(issuer, currency1), 2e18 - 1e15, "escrow not refunded");
        assertEq(_posLiq(-300, 300, PlacementLib.mandateSalt(id)), 0, "bands not withdrawn");
        assertGt(MockERC20(Currency.unwrap(currency0)).balanceOf(curator), t0, "principal not returned");
        assertEq(_bondLiq(id), 1e18, "bond touched at settlement");
        assertEq(hook.liveMandate(pid), 0);
    }

    function test_settleByOtherOnlyAfterGrace_bandsFrozen() public {
        uint256 id = _standard();
        _roll(2_001);
        vm.prank(stranger);
        vm.expectRevert(KeelHook.TooEarly.selector);
        hook.settle(id);
        _roll(7_200);
        vm.prank(stranger);
        hook.settle(id);
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.SETTLED));
        assertEq(_posLiq(-300, 300, PlacementLib.mandateSalt(id)), 1e20, "bands moved by a stranger");
        vm.prank(curator);
        vm.expectRevert(KeelHook.BadState.selector);
        hook.reshape(id, _bands1(-300, 300, 1e20));
    }
}
