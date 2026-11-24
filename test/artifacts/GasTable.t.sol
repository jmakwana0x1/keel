// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PoolHarness} from "../harness/PoolHarness.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";

/// @dev forge 1.8 implements vm.cool; the forge-std vendored with v4-core 1.0.2 does not declare it.
interface VmCool {
    function cool(address target) external;
}

/// @dev External wrapper so a walk's gas is measured as one call, including its extsload reads through V4TickSource.
contract MeterProbe {
    function walk(DepthLib.DepthParams memory p) external view returns (DepthLib.Measurement memory) {
        return DepthLib.walk(p);
    }
}

/// @notice Gas table, committed to data/phase1_gas.json. Books are ladders of one-tick positions (tick spacing 1), so
/// a walk to just past the ladder crosses exactly the stated number of initialized ticks. Measured against the real
/// PoolManager through V4TickSource (cold extsload per call, as prove() will pay). The artifact asserts its own claims:
/// cost rises with crossings, the per-crossing marginal is positive and stable, and early termination makes the
/// compliant path cheaper than the full walk.
contract GasTableTest is PoolHarness {
    MeterProbe probe;

    function setUp() public {
        _setUpManager();
        probe = new MeterProbe();
    }

    function _ladderGas(uint256 crossings, uint256 depthFrac) internal returns (uint256 gas, uint256 steps, uint256 c) {
        _setUpManager();
        _newPool(0, 0);
        // a ladder of `crossings` initialized ticks above spot: positions [i, i+1] for i in [1, crossings-1] plus one
        // position covering spot
        _addPosition(-1, 1, 1e18);
        for (uint256 i = 1; i < crossings; i++) {
            _addPosition(int24(int256(i)), int24(int256(i + 1)), 1e18);
        }
        uint160 target = _target(crossings + 3, true); // just past the ladder, inside the first word
        DepthLib.DepthParams memory p = _params(target, 0);
        if (depthFrac != 0) {
            DepthLib.Measurement memory full = probe.walk(p);
            p.depthTarget = full.depth * depthFrac / 100;
        }
        // Foundry starts each top-level call with cold storage, so this call's extsloads are already priced cold
        // (asserted by GasColdnessTest). vm.cool is kept as a guard in case that behaviour changes.
        VmCool(address(vm)).cool(address(manager));
        uint256 g0 = gasleft();
        DepthLib.Measurement memory m = probe.walk(p);
        gas = g0 - gasleft();
        assertTrue(m.reachedPrice || m.proven, "gas row did not complete: capped or stalled");
        assertFalse(m.capped, "gas row capped");
        return (gas, m.steps, m.crossings);
    }

    function test_emitGasTable() public {
        uint16[5] memory ks = [uint16(1), 3, 9, 64, 127];
        uint256[5] memory gas;
        string memory rows;
        for (uint256 i; i < 5; i++) {
            (uint256 g, uint256 steps, uint256 c) = _ladderGas(ks[i], 0);
            assertEq(c, ks[i], "setup: wrong crossing count");
            gas[i] = g;
            if (i > 0) assertGt(g, gas[i - 1], "gas did not rise with crossings");
            rows = string.concat(rows, i == 0 ? "" : ",", '{"crossings":', vm.toString(c), ',"steps":', vm.toString(steps),
                ',"gas":', vm.toString(g), "}");
        }
        uint256 marginalLow = (gas[2] - gas[1]) / 6; // 3 -> 9
        uint256 marginalHigh = (gas[4] - gas[3]) / 63; // 64 -> 127
        assertGt(marginalLow, 0);
        // stable per-crossing cost: the marginal over 64->127 within 25% of the marginal over 3->9
        assertLt(marginalHigh * 100, marginalLow * 125, "per-crossing cost not stable (grows with depth)");
        assertGt(marginalHigh * 100, marginalLow * 75, "per-crossing cost not stable (shrinks with depth)");
        (uint256 gCompliant,,) = _ladderGas(127, 10); // compliant path: target met at ~10% of the full depth
        assertLt(gCompliant, gas[4] / 3, "early termination does not make the compliant path cheap");
        string memory j = string.concat(
            '{"measured":"forge test, real PoolManager via V4TickSource, tick spacing 1, PoolManager storage cooled (vm.cool) before each measured call, so reads are priced cold",',
            '"max_steps":', vm.toString(DepthLib.MAX_STEPS), ',"rows":[', rows, '],',
            '"marginal_gas_per_crossing_3_to_9":', vm.toString(marginalLow),
            ',"marginal_gas_per_crossing_64_to_127":', vm.toString(marginalHigh),
            ',"compliant_early_exit_at_10pct_of_127_crossings":', vm.toString(gCompliant), "}\n"
        );
        vm.writeFile("data/phase1_gas.json", j);
    }
}
