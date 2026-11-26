// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {KeelHook} from "../../src/KeelHook.sol";
import {KeelHarness} from "../harness/KeelHarness.sol";
import {MandateHandlerBase} from "./MandateHandlerBase.sol";

/// @notice Biased to books where the curator's band holds: two pushes in three keep the ±1% range inside the band,
/// and the curator reshapes around the price. The third push leaves the band, so breaches still occur (§5 premise).
contract ComplianceHandler is MandateHandlerBase {
    constructor(Env memory e) MandateHandlerBase(e) {}

    function roll(uint256 k) external {
        _roll(k);
    }

    function pushAndProve(uint256 seed, bool byCurator) external {
        seed = _mix(seed);
        int24 t = seed % 3 == 0
            ? (seed % 2 == 0 ? int24(1) : int24(-1)) * int24(int256(400 + (seed >> 8) % 1100))
            : int24(int256((seed >> 8) % 401)) - 200;
        _pushTo(t);
        _prove(byCurator, 1);
    }

    function prove(bool byCurator) external {
        _prove(byCurator, 1);
    }

    /// The curator follows the price with a ±300 band around the current tick. Three times in four it does so at
    /// the start of a fresh block, as an honest curator would; once in four in the same block as earlier proofs,
    /// which voids their credit (D2.2) and keeps the ghost's void branch exercised.
    function reshape(uint256 liq) external {
        if (_mix(liq) % 4 != 0) _roll(1);
        int24 c = _tick() / 60 * 60;
        KeelHook.Band[] memory b = new KeelHook.Band[](1);
        b[0] = KeelHook.Band({lower: c - 300, upper: c + 300, liquidity: uint128(bound(liq, 1e20, 3e20))});
        _reshape(b);
    }
}

/// @title Compliance invariants (VERIFICATION.md §5)
/// @notice pay ⊆ proven, as the D2.2 ghost model; bond monotone; escrow + accrued conserved while ACTIVE, so value
/// moves from escrow only against a credited compliant proof.
/// forge-config: default.invariant.depth = 128
contract ComplianceInvariantTest is KeelHarness {
    ComplianceHandler handler;

    function setUp() public {
        _setUpKeel();
        // _standard()'s book, with N at its maximum: slashing is the Breach suite's job, and an early slash here
        // would end a run before compliance was exercised
        _lp(-6000, 6000, 1e18);
        KeelHook.Terms memory t = _terms(4e17, 4e17);
        t.stalenessBlocks = 7_200;
        t.termBlocks = 100_000;
        uint256 id = _post(t);
        _accept(id, _bands1(-300, 300, 1e20));
        handler = new ComplianceHandler(
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
        sels[0] = ComplianceHandler.roll.selector;
        sels[1] = ComplianceHandler.pushAndProve.selector;
        sels[2] = ComplianceHandler.prove.selector;
        sels[3] = ComplianceHandler.reshape.selector;
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
        assertGt(handler.swapsDone(), 0, "vacuous: no swap ever executed");
    }
}
