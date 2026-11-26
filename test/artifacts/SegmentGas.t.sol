// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {KeelHook} from "../../src/KeelHook.sol";
import {MandateLib} from "../../src/libraries/MandateLib.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";
import {KeelHarness} from "../harness/KeelHarness.sol";

interface VmCoolS {
    function cool(address target) external;
}

/// @notice The worst segmented proof an accepted mandate can need (D2.10), committed to data/phase2_segments.json.
/// Spacing 1, the widest x the post-time bound accepts, a 1-wei position on EVERY tick of both sides of the range,
/// and a curator short on both sides, so both sides walk to the end. Storage cooled before each measured call.
/// Asserts its claim: the worst-case proof fits under the EIP-7825 per-transaction cap of 2^24 gas, so no accepted
/// mandate can be fragmented beyond what one transaction can prove.
contract SegmentGasTest is KeelHarness {
    uint256 internal constant TX_GAS_CAP = 1 << 24;

    function setUp() public {
        _setUpKeel();
        (pkey, pid) = initPool(currency0, currency1, IHooks(address(hook)), 500, 1, SQRT_PRICE_1_1);
    }

    function _cool() internal {
        VmCoolS(address(vm)).cool(address(hook));
        VmCoolS(address(vm)).cool(address(meter));
        VmCoolS(address(vm)).cool(address(hook.tickSource(pid)));
        VmCoolS(address(vm)).cool(address(manager));
    }

    function _gas(uint256 id, uint256 segs) internal returns (uint256 g, DepthLib.Verdict v) {
        _cool();
        vm.prank(watcher);
        uint256 g0 = gasleft();
        v = hook.prove(id, segs);
        g = g0 - gasleft();
    }

    function test_emitSegmentGas() public {
        // the widest coverable x at spacing 1
        uint256 x = 1;
        while (meter.coverable(x + 1, 1)) x++;
        uint256 bound = MandateLib.stepBound(x, 1);
        assertLe(bound, DepthLib.MAX_SEGMENTS * DepthLib.MAX_STEPS);

        _lp(-6000, 6000, 1e18);
        KeelHook.Terms memory t = _terms(4e17, 4e17);
        t.xBps = uint16(x);
        uint256 id = _post(t);
        _accept(id, _bands1(-30, 30, 1e18));
        int24 r = int24(int256(bound)) + 2;
        for (int24 k = -r; k < r; k++) {
            _lp(k, k + 1, 1);
        }
        _roll(1);

        (uint256 gOne, DepthLib.Verdict v1) = _gas(id, 1);
        assertEq(uint8(v1), uint8(DepthLib.Verdict.INCONCLUSIVE), "one segment should cap");
        (uint256 gOpen, DepthLib.Verdict v2) = _gas(id, DepthLib.MAX_SEGMENTS);
        assertEq(uint8(v2), uint8(DepthLib.Verdict.BREACH), "a covered worst-case range did not breach");
        _roll(N);
        (uint256 gFinal,) = _gas(id, DepthLib.MAX_SEGMENTS);
        assertEq(uint8(_status(id)), uint8(MandateLib.Status.SLASHED));

        assertLt(gOpen, TX_GAS_CAP, "worst-case segmented proof exceeds one transaction");
        assertLt(gFinal, TX_GAS_CAP, "worst-case finalising proof exceeds one transaction");
        string memory j = string.concat(
            '{"measured":"forge test, real PoolManager, spacing 1, 1-wei position on every tick of both sides, storage cooled",',
            '"max_x_bps_spacing_1":', vm.toString(x), ',"step_bound":', vm.toString(bound),
            ',"max_segments":', vm.toString(DepthLib.MAX_SEGMENTS), ',"max_steps":', vm.toString(DepthLib.MAX_STEPS),
            ',"one_segment_capped":', vm.toString(gOne), ',"segmented_breach_opens_window":', vm.toString(gOpen),
            ',"segmented_breach_finalises":', vm.toString(gFinal), ',"tx_gas_cap_eip7825":', vm.toString(TX_GAS_CAP), "}"
        );
        vm.writeFile("data/phase2_segments.json", string.concat(j, "\n"));
    }
}
