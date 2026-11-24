// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Deployers} from "@uniswap/v4-core/test/utils/Deployers.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {ModifyLiquidityParams, SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {V4TickSource} from "../../src/adapters/V4TickSource.sol";
import {ITickSource} from "../../src/interfaces/ITickSource.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";

/// @notice Real PoolManager + a book built from recorded positions, so the same book can be measured by
/// DepthLib, executed by the PoolManager, and handed to the Python reference.
abstract contract PoolHarness is Deployers {
    using StateLibrary for IPoolManager;

    PoolKey internal pkey;
    PoolId internal pid;
    V4TickSource internal src;
    int24[] internal bookTicks; // every position boundary ever written (may repeat)

    uint24[4] internal FEES = [uint24(100), 500, 3000, 10000];
    int24[4] internal SPACINGS = [int24(1), 10, 60, 200];

    function _setUpManager() internal {
        deployFreshManagerAndRouters();
        deployMintAndApprove2Currencies();
    }

    function _newPool(uint256 tier, int24 startTick) internal {
        int24 sp = SPACINGS[tier % 4];
        startTick = startTick / sp * sp;
        (pkey, pid) = initPool(currency0, currency1, IHooks(address(0)), FEES[tier % 4], sp, TickMath.getSqrtPriceAtTick(startTick));
        src = new V4TickSource(manager, pid);
        delete bookTicks;
    }

    function _addPosition(int24 lower, int24 upper, uint128 liq) internal {
        int24 sp = pkey.tickSpacing;
        lower = _align(lower, sp);
        upper = _align(upper, sp);
        if (upper <= lower) upper = lower + sp;
        modifyLiquidityRouter.modifyLiquidity(
            pkey, ModifyLiquidityParams({tickLower: lower, tickUpper: upper, liquidityDelta: int256(uint256(liq)), salt: 0}), ""
        );
        bookTicks.push(lower);
        bookTicks.push(upper);
    }

    function _align(int24 t, int24 sp) internal pure returns (int24) {
        int24 minT = TickMath.minUsableTick(sp);
        int24 maxT = TickMath.maxUsableTick(sp);
        t = t / sp * sp;
        if (t < minT) t = minT;
        if (t > maxT) t = maxT;
        return t;
    }

    /// @dev A book of `n` positions around spot, with deliberate gaps: positions are scattered, some far from spot.
    function _randomBook(uint256 seed, uint256 n, int24 span) internal {
        (, int24 tick,,) = manager.getSlot0(pid);
        for (uint256 i; i < n; i++) {
            uint256 r = uint256(keccak256(abi.encode(seed, i)));
            int24 a = tick + int24(int256(r % uint24(2 * span))) - span;
            int24 w = int24(int256((r >> 32) % uint24(span))) + 1;
            uint128 liq = uint128(1e15 + (r >> 64) % 1e21);
            _addPosition(a, a + w, liq);
        }
    }

    function _params(uint160 target, uint256 depthTarget) internal view returns (DepthLib.DepthParams memory p) {
        (uint160 sqrtP, int24 tick,,) = manager.getSlot0(pid);
        p = DepthLib.DepthParams({
            sqrtPriceStartX96: sqrtP,
            tickStart: tick,
            liquidityStart: manager.getLiquidity(pid),
            sqrtPriceTargetX96: target,
            depthTarget: depthTarget,
            feePips: pkey.fee,
            tickSpacing: pkey.tickSpacing,
            source: ITickSource(address(src))
        });
    }

    /// @dev Target sqrt price at spot·(1 ± bps/1e4), clamped inside the protocol bounds.
    function _target(uint256 bps, bool up) internal view returns (uint160) {
        (uint160 sqrtP,,,) = manager.getSlot0(pid);
        uint256 s = up ? uint256(sqrtP) * _isqrt(1e8 + bps * 1e4) / 1e4 : uint256(sqrtP) * 1e4 / _isqrt(1e8 + bps * 1e4);
        if (s <= TickMath.MIN_SQRT_PRICE) s = TickMath.MIN_SQRT_PRICE + 1;
        if (s >= TickMath.MAX_SQRT_PRICE) s = TickMath.MAX_SQRT_PRICE - 1;
        if (s == sqrtP) s = up ? s + 1 : s - 1;
        return uint160(s);
    }

    /// @notice What the PoolManager actually charges to move the price to `target`: an exact-input swap with an
    /// unbounded amount and the limit at the target. Returns the gross input.
    function _poolGrossIn(uint160 target) internal returns (uint256) {
        (uint160 sqrtP,,,) = manager.getSlot0(pid);
        bool zeroForOne = target < sqrtP;
        BalanceDelta d = swapRouter.swap(
            pkey,
            SwapParams({zeroForOne: zeroForOne, amountSpecified: -int256(uint256(type(uint128).max)), sqrtPriceLimitX96: target}),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
        int128 a = zeroForOne ? d.amount0() : d.amount1();
        return uint256(uint128(-a));
    }

    /// @notice The same price move as an EXACT-OUTPUT swap with an unbounded output and the limit at the target.
    /// Every step then reaches its step target, so the gross input must equal the exact-input shape's.
    function _poolGrossInExactOut(uint160 target) internal returns (uint256) {
        (uint160 sqrtP,,,) = manager.getSlot0(pid);
        bool zeroForOne = target < sqrtP;
        BalanceDelta d = swapRouter.swap(
            pkey,
            SwapParams({zeroForOne: zeroForOne, amountSpecified: int256(uint256(type(uint128).max)), sqrtPriceLimitX96: target}),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
        int128 a = zeroForOne ? d.amount0() : d.amount1();
        return uint256(uint128(-a));
    }

    /// @notice The current book, serialised for sim/ffi_walk.py.
    function _bookArg(uint160 target) internal view returns (string memory s) {
        (uint160 sqrtP, int24 tick,,) = manager.getSlot0(pid);
        s = string.concat(
            vm.toString(uint256(sqrtP)), ",", vm.toString(int256(tick)), ",", vm.toString(uint256(manager.getLiquidity(pid))),
            ",", vm.toString(int256(pkey.tickSpacing)), ",", vm.toString(uint256(pkey.fee)), ",", vm.toString(uint256(target))
        );
        for (uint256 i; i < bookTicks.length; i++) {
            int24 t = bookTicks[i];
            bool dup;
            for (uint256 j; j < i; j++) if (bookTicks[j] == t) dup = true;
            if (dup) continue;
            (uint128 gross, int128 net) = manager.getTickLiquidity(pid, t);
            if (gross == 0) continue;
            s = string.concat(s, ";", vm.toString(int256(t)), ":", vm.toString(uint256(gross)), ":", vm.toString(int256(net)));
        }
    }

    function _python(uint160 target) internal returns (uint256 depth, uint256 fee, uint256 steps, uint256 crossings) {
        string[] memory cmd = new string[](3);
        cmd[0] = "python3";
        cmd[1] = "sim/ffi_walk.py";
        cmd[2] = _bookArg(target);
        (depth, fee, steps, crossings) = abi.decode(vm.ffi(cmd), (uint256, uint256, uint256, uint256));
    }

    function _isqrt(uint256 x) internal pure returns (uint256 y) {
        if (x == 0) return 0;
        y = x;
        uint256 z = (x + 1) / 2;
        while (z < y) {
            y = z;
            z = (x / z + z) / 2;
        }
    }
}
