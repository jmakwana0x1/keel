// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PoolHarness} from "../harness/PoolHarness.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";

contract TwiceProbe {
    function run(DepthLib.DepthParams memory p) external view returns (uint256 first, uint256 second) {
        uint256 g0 = gasleft();
        DepthLib.walk(p);
        first = g0 - gasleft();
        g0 = gasleft();
        DepthLib.walk(p);
        second = g0 - gasleft();
    }
}

/// @notice Guards the gas table's "priced cold" label. Two identical walks inside ONE call: the second is certainly
/// warm, so if the first is not materially dearer, the table's reads were warm and its numbers understate prove().
/// Observed when written: 94,644 (first) vs 69,640 (second) at 9 crossings.
contract GasColdnessTest is PoolHarness {
    function test_gasTableReadsAreCold() public {
        _setUpManager();
        _newPool(0, 0);
        _addPosition(-1, 1, 1e18);
        for (uint256 i = 1; i < 9; i++) _addPosition(int24(int256(i)), int24(int256(i + 1)), 1e18);
        TwiceProbe t = new TwiceProbe();
        (uint256 first, uint256 second) = t.run(_params(_target(12, true), 0));
        assertGt(first, second + 18 * 1000, "first walk not priced cold: gas table would understate prove()");
    }
}
