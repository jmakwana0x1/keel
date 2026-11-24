// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import "forge-std/Test.sol";
import {MandateLib} from "../../src/libraries/MandateLib.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";

/// @notice The pure transitions, over their whole input domain.
contract MandateLibTest is Test {
    function _v(uint8 x) internal pure returns (DepthLib.Verdict) {
        return DepthLib.Verdict(x % 4);
    }

    /// @notice Pay implies a credited COMPLIANT verdict. Nothing else pays.
    function testFuzz_payImpliesCreditedCompliant(uint64 opened, uint32 n, uint64 b, uint8 v, bool credited, bool paid)
        public
        pure
    {
        b = uint64(bound(b, opened, type(uint64).max));
        MandateLib.ProofEffect memory e = MandateLib.applyProof(opened, n, b, _v(v), credited, paid);
        if (e.pay) {
            assertEq(uint8(_v(v)), uint8(DepthLib.Verdict.COMPLIANT), "paid on a non-compliant verdict");
            assertTrue(credited, "paid an uncredited proof");
            assertFalse(paid, "paid twice in a block");
        }
    }

    /// @notice Finalisation needs an open window at least N old and a BREACH verdict.
    function testFuzz_finaliseNeedsBreachAndN(uint64 opened, uint32 n, uint64 b, uint8 v, bool credited, bool paid)
        public
        pure
    {
        b = uint64(bound(b, opened, type(uint64).max));
        MandateLib.ProofEffect memory e = MandateLib.applyProof(opened, n, b, _v(v), credited, paid);
        if (e.finalise) {
            assertEq(uint8(_v(v)), uint8(DepthLib.Verdict.BREACH), "finalised on a non-breach verdict");
            assertTrue(opened != 0, "finalised with no window");
            assertGe(b - opened, n, "finalised before N");
        }
    }

    /// @notice INCONCLUSIVE and UNUSABLE never move the window, pay, or finalise.
    function testFuzz_undecidedChangesNothing(uint64 opened, uint32 n, uint64 b, bool unusable, bool credited, bool paid)
        public
        pure
    {
        b = uint64(bound(b, opened, type(uint64).max));
        DepthLib.Verdict v = unusable ? DepthLib.Verdict.UNUSABLE : DepthLib.Verdict.INCONCLUSIVE;
        MandateLib.ProofEffect memory e = MandateLib.applyProof(opened, n, b, v, credited, paid);
        assertEq(e.shortfallOpenedAt, opened);
        assertFalse(e.pay || e.finalise || e.opened || e.closed);
    }

    /// @notice A capped side never breaches, however the curator's contribution falls (rule 1, end to end through
    /// the hook's side logic).
    function testFuzz_cappedSideNeverBreaches(bool proven, bool exhausted, bool covers) public pure {
        DepthLib.Measurement memory m;
        m.capped = true;
        m.proven = proven;
        m.exhausted = exhausted;
        assertTrue(MandateLib.sideVerdict(covers, m) != DepthLib.Verdict.BREACH, "capped walk breached");
    }

    /// @notice A pool made deep by others never pays the curator.
    function test_poolDeepThroughOthersIsNotCompliant() public pure {
        DepthLib.Measurement memory m;
        m.proven = true;
        assertEq(uint8(MandateLib.sideVerdict(false, m)), uint8(DepthLib.Verdict.INCONCLUSIVE));
    }

    function testFuzz_combinePrecedence(bool mu, uint8 u, bool md, uint8 d) public pure {
        DepthLib.Verdict up = _v(u);
        DepthLib.Verdict dn = _v(d);
        DepthLib.Verdict c = MandateLib.combine(mu, up, md, dn);
        bool anyUnusable = (mu && up == DepthLib.Verdict.UNUSABLE) || (md && dn == DepthLib.Verdict.UNUSABLE);
        bool anyBreach = (mu && up == DepthLib.Verdict.BREACH) || (md && dn == DepthLib.Verdict.BREACH);
        if (anyUnusable) assertEq(uint8(c), uint8(DepthLib.Verdict.UNUSABLE));
        else if (anyBreach) assertEq(uint8(c), uint8(DepthLib.Verdict.BREACH));
        if (c == DepthLib.Verdict.COMPLIANT) {
            assertTrue(!mu || up == DepthLib.Verdict.COMPLIANT);
            assertTrue(!md || dn == DepthLib.Verdict.COMPLIANT);
        }
    }

    /// @notice testFuzz_bountyBelowDonation: watchers are never enriched ahead of LPs.
    function testFuzz_bountyBelowDonation(uint256 accrued) public pure {
        accrued = bound(accrued, 1, type(uint128).max);
        (uint256 bounty, uint256 donation) = MandateLib.slashSplit(accrued);
        assertLt(bounty, donation, "bounty >= donation");
        assertEq(bounty + donation, accrued, "slash created or destroyed value");
    }
}
