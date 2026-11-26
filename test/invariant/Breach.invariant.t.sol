// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {KeelHook} from "../../src/KeelHook.sol";
import {KeelHarness} from "../harness/KeelHarness.sol";
import {MandateHandlerBase} from "./MandateHandlerBase.sol";

/// @notice Biased to books thinned at the price. Two pushes in three leave the curator's band; the background LP is
/// withdrawn in steps; and `breachEpisode` runs a whole breach (open, N blocks of silence, finalise) once something
/// has accrued, because a slash with nothing accrued tests nothing about the split.
contract BreachHandler is MandateHandlerBase {
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

    function breachEpisode(uint256 seed) external {
        if (provedBlocks == 0) return;
        seed = _mix(seed);
        _roll(1);
        _pushTo((seed % 2 == 0 ? int24(1) : int24(-1)) * int24(int256(600 + (seed >> 8) % 900)));
        _prove(false, 1);
        blk += hook.terms(id).stalenessBlocks;
        vm.roll(blk);
        _prove(false, 1);
    }
}

/// @title Breach invariants (VERIFICATION.md §5)
/// @notice slash ≤ accrued, bounty < donation, liquidity never reduced (no proof changes a position; a slashed
/// curator's bands never move), slash only after N; plus the shared checks (pay ⊆ proven, bond monotone, escrow
/// conserved). `slashes() > 0` is added to §5's three premises: without a slash, every slash check is empty.
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
    }
}
