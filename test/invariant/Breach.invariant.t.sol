// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {KeelHook} from "../../src/KeelHook.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {KeelHarness} from "../harness/KeelHarness.sol";
import {MandateHandlerBase} from "./MandateHandlerBase.sol";
import {MandateLib} from "../../src/libraries/MandateLib.sol";

/// @notice Biased to books thinned at the price. Two pushes in three leave the curator's band; the background LP is
/// withdrawn in steps; and `breachEpisode` runs a whole breach (open, N blocks of silence, finalise) once something
/// has accrued, because a slash with nothing accrued tests nothing about the split.
contract BreachHandler is MandateHandlerBase {
    using StateLibrary for IPoolManager;

    int24 internal constant BG_LO = -6000;
    int24 internal constant BG_HI = 6000;
    uint128 internal background = 1e18; // the harness's background LP, owned by the trader

    constructor(Env memory e) MandateHandlerBase(e) {}

    function roll(uint256 k) external {
        _roll(k);
    }

    function pushAndProve(uint256 seed, bool byCurator) external {
        seed = _mix(seed);
        int24 t = seed % 3 != 0
            ? (seed % 2 == 0 ? int24(1) : int24(-1)) * int24(int256(400 + (seed >> 8) % 1100))
            : int24(int256((seed >> 8) % 401)) - 200;
        _pushTo(t);
        _prove(byCurator, 1);
    }

    /// Withdraw part of the background liquidity: the book thins under the price.
    function thin(uint256 seed) external {
        uint128 cut = uint128(bound(seed, 1e15, background / 4 + 1e15)); // the test router asserts a nonzero delta
        if (cut >= background) return;
        background -= cut;
        _lp(BG_LO, BG_HI, -int256(uint256(cut)));
        _after();
    }

    uint256 public termProbes; // post-term finalisation attempts made (and undone)
    uint256 public zeroLiquiditySwaps; // swaps that left the pool with no active liquidity

    /// A whole breach: open a window, probe the term guard, wait N, finalise. Half the episodes push the price past
    /// every position, so the pool has zero active liquidity: the state where an unguarded `donate` reverts.
    function breachEpisode(uint256 seed) external {
        if (provedBlocks == 0) return;
        seed = _mix(seed);
        _roll(1);
        int24 dir = seed % 2 == 0 ? int24(1) : int24(-1);
        _pushTo(dir * ((seed >> 4) % 2 == 0 ? int24(7_000) : int24(int256(600 + (seed >> 8) % 900))));
        if (manager.getLiquidity(pid) == 0) zeroLiquiditySwaps++;
        _prove(false, 1);
        _probeTermGuard();
        blk += hook.terms(id).stalenessBlocks;
        vm.roll(blk);
        _prove(false, 1);
    }

    /// D2.3: proofs after endBlock are refused, so a window open at the end never finalises. Under a snapshot, jump
    /// past the term with the window long enough open, try to finalise, then undo everything. The finding is recorded
    /// after the revert, because a violation written inside the snapshot would be rolled back with it.
    function _probeTermGuard() internal {
        (,,,,, uint64 opened, uint64 endBlock) = hook.mandate(id);
        if (opened == 0 || status() != MandateLib.Status.ACTIVE) return;
        uint256 snap = vm.snapshotState();
        vm.roll(uint256(endBlock) + hook.terms(id).stalenessBlocks + 1);
        vm.prank(watcher);
        bool finalised;
        try hook.prove(id) {
            finalised = status() == MandateLib.Status.SLASHED;
        } catch {}
        vm.revertToState(snap);
        vm.roll(blk);
        termProbes++;
        if (finalised) _fail("a window open at endBlock finalised after the term");
    }
}

/// @title Breach invariants (VERIFICATION.md §5)
/// @notice slash ≤ accrued, bounty < donation, liquidity never reduced (no proof changes a position; a slashed
/// curator's bands never move), slash only after N; plus the shared checks (pay ⊆ proven, bond monotone, escrow
/// conserved, solvent in claims); swaps never revert, including at zero active liquidity; a window open at
/// endBlock never finalises (probed under a snapshot). `slashes() > 0` is added to §5's three premises: without a slash, every slash check is empty.
/// forge-config: default.invariant.depth = 128
contract BreachInvariantTest is KeelHarness {
    BreachHandler handler;

    function setUp() public {
        _setUpKeel();
        // _standard()'s book, with N at its maximum so ordinary rolls (<= 20 blocks, 128 calls) can never finalise:
        // only breachEpisode slashes, and only once something has accrued
        _lp(-6000, 6000, 1e18);
        KeelHook.Terms memory t = _terms(4e17, 4e17);
        t.stalenessBlocks = 7_200;
        t.termBlocks = 100_000;
        uint256 id = _post(t);
        _accept(id, _bands1(-300, 300, 1e20));
        handler = new BreachHandler(
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
        bytes4[] memory sels = new bytes4[](4);
        sels[0] = BreachHandler.roll.selector;
        sels[1] = BreachHandler.pushAndProve.selector;
        sels[2] = BreachHandler.thin.selector;
        sels[3] = BreachHandler.breachEpisode.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: sels}));
    }

    function invariant_noViolation() public view {
        assertEq(handler.violation(), "", handler.violation());
    }

    function invariant_bountyBelowDonation() public view {
        uint256 a = handler.accruedAtSlash();
        uint256 b = handler.bountyPaid();
        assertLe(b, a, "slash took more than accrued");
        if (a != 0) assertLt(b, a - b, "bounty not below donation");
    }

    function afterInvariant() public view {
        // The premise must have been exercised, or the run proves nothing.
        assertGt(handler.provedBlocks(), 0, "vacuous: nothing was ever proved");
        assertGt(handler.breachesObserved(), 0, "vacuous: no breach path exercised");
        assertGt(handler.bondLiquidity(), 0, "vacuous: bond never placed");
        assertGt(handler.slashes(), 0, "vacuous: no slash, so no slash check was exercised");
        assertGt(handler.swapsDone(), 0, "vacuous: no swap ever executed");
        assertGt(handler.termProbes(), 0, "vacuous: the term guard was never probed");
    }
}
