// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {KeelHarness} from "../harness/KeelHarness.sol";
import {KeelHook} from "../../src/KeelHook.sol";
import {MandateLib} from "../../src/libraries/MandateLib.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";

interface VmCoolP {
    function cool(address target) external;
}

/// @notice prove() gas by path, committed to data/phase2_gas.json. Storage of the hook, the meter, the pool's tick
/// source and the PoolManager is cooled before each measured call. Asserts its own claim (D2.7): the compliant path
/// runs no walk, so its gas is identical whatever the book holds (measured on the same state with and without a
/// ladder of initialised ticks inside the target range). It does NOT claim the compliant path is the cheapest: the first compliant proof pays zero-to-nonzero
/// writes on the payment slots and costs more than a breach proof whose walks are short (README, Phase 2 gas).
contract ProveGasTest is KeelHarness {
    function setUp() public {
        _setUpKeel();
    }

    function _cool() internal {
        VmCoolP(address(vm)).cool(address(hook));
        VmCoolP(address(vm)).cool(address(meter));
        VmCoolP(address(vm)).cool(address(hook.tickSource(pid)));
        VmCoolP(address(vm)).cool(address(manager));
    }

    function _gasProve(uint256 id) internal returns (uint256 g, DepthLib.Verdict v) {
        _cool();
        vm.prank(watcher);
        uint256 g0 = gasleft();
        v = hook.prove(id);
        g = g0 - gasleft();
    }

    function test_emitProveGas() public {
        // Base book only: no initialised tick inside the +-1% target range, so a walk would be one step.
        _lp(-6000, 6000, 1e18);
        uint256 id = _post(_terms(4e17, 4e17));
        KeelHook.Band[] memory four = new KeelHook.Band[](4);
        four[0] = KeelHook.Band({lower: -300, upper: 300, liquidity: 1e20});
        four[1] = KeelHook.Band({lower: -600, upper: -300, liquidity: 1e19});
        four[2] = KeelHook.Band({lower: 300, upper: 600, liquidity: 1e19});
        four[3] = KeelHook.Band({lower: -900, upper: 900, liquidity: 1e18});
        _accept(id, four);
        _roll(1);
        uint256 snap = vm.snapshotState();
        (uint256 gCompliant, DepthLib.Verdict v1) = _gasProve(id);
        assertEq(uint8(v1), uint8(DepthLib.Verdict.COMPLIANT));
        vm.revertToState(snap);
        // Same state plus a ladder every 60 ticks: a walk to either target would now cross initialised ticks.
        for (int24 t = -1200; t < 1200; t += 60) {
            _lp(t, t + 60, 1e16);
        }
        (uint256 gCompliantDense,) = _gasProve(id);
        assertEq(gCompliantDense, gCompliant, "compliant gas depends on the book");
        _roll(1);
        (uint256 gSteady, DepthLib.Verdict v1b) = _gasProve(id);
        assertEq(uint8(v1b), uint8(DepthLib.Verdict.COMPLIANT));

        _pushTo(1000); // every band left behind; walks cross ladder rungs
        _roll(1);
        (uint256 gOpen, DepthLib.Verdict v2) = _gasProve(id);
        assertEq(uint8(v2), uint8(DepthLib.Verdict.BREACH));
        _roll(N);
        (uint256 gSlash, DepthLib.Verdict v3) = _gasProve(id);
        assertEq(uint8(v3), uint8(DepthLib.Verdict.BREACH));
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.SLASHED));

        string memory j = string.concat(
            '{"measured":"forge test, real PoolManager, storage of hook/meter/tick source/manager cooled before each call",',
            '"bands":4,"compliant_first_proof":', vm.toString(gCompliant),
            ',"compliant_first_proof_plus_ladder_in_range":', vm.toString(gCompliantDense),
            ',"compliant_steady_state_next_block":', vm.toString(gSteady), ',"breach_opens_window_two_walks":',
            vm.toString(gOpen), ',"breach_finalises_slash_and_donate":', vm.toString(gSlash), "}"
        );
        vm.writeFile("data/phase2_gas.json", string.concat(j, "\n"));
    }
}
