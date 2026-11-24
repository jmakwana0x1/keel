// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {KeelHook} from "../../src/KeelHook.sol";
import {KeelMeter} from "../../src/KeelMeter.sol";
import {MandateLib} from "../../src/libraries/MandateLib.sol";
import {KeelHarness} from "../harness/KeelHarness.sol";

/// @notice Exposes THE NEGATIVE SITE at its own boundary, bypassing every entry-point check.
contract KeelHookProbe is KeelHook {
    constructor(IPoolManager m, KeelMeter meter_, uint32 lo, uint32 hi) KeelHook(m, meter_, lo, hi) {}

    function probeBurn(uint256 id, address actor) external {
        Band storage bd = mandates[id].bands[0];
        _burnMandate(id, actor, bd.lower, bd.upper, bd.liquidity);
    }
}

/// @title The negative site's gate, tested at the site
/// @notice Today both callers of `_burnMandate` (curator `reshape`, curator `settle`) check curator and ACTIVE
/// before reaching it, so removing the gate at the site is invisible through the public API: the Phase 2
/// mutation transcript recorded "negative site ungated" passing all 40 suite tests. The site gate exists for the
/// entry point nobody has written yet. These tests call the site directly, outside an unlock: a gated rejection
/// reverts NotCurator; a request that passes the gate reaches the PoolManager and reverts ManagerLocked. The
/// distinction between those two selectors is what makes the tests sensitive to the gate itself.
contract NegativeSiteTest is KeelHarness {
    function _hookArtifact() internal pure override returns (string memory) {
        return "test/unit/NegativeSite.t.sol:KeelHookProbe";
    }

    function setUp() public {
        _setUpKeel();
    }

    function _probe() internal view returns (KeelHookProbe) {
        return KeelHookProbe(address(hook));
    }

    /// Control: the curator of an ACTIVE mandate passes the gate (and only then meets the lock).
    function test_curatorWhileActivePassesGate() public {
        uint256 id = _standard();
        vm.expectRevert(IPoolManager.ManagerLocked.selector);
        _probe().probeBurn(id, curator);
    }

    function testFuzz_nonCuratorRejectedAtSite(address actor) public {
        uint256 id = _standard();
        vm.assume(actor != curator);
        vm.expectRevert(KeelHook.NotCurator.selector);
        _probe().probeBurn(id, actor);
    }

    /// After settle-by-other the bands are frozen: the curator is rejected at the site, not just at the entry.
    function test_curatorRejectedAtSiteOnceNotActive() public {
        uint256 id = _standard();
        _roll(2_000 + 7_200 + 1);
        vm.prank(watcher);
        hook.settle(id);
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.SETTLED));
        vm.expectRevert(KeelHook.NotCurator.selector);
        _probe().probeBurn(id, curator);
    }
}
