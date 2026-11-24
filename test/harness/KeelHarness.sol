// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Deployers} from "@uniswap/v4-core/test/utils/Deployers.sol";
import {MockERC20} from "solmate/src/test/utils/mocks/MockERC20.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Position} from "@uniswap/v4-core/src/libraries/Position.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {ModifyLiquidityParams, SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {KeelHook} from "../../src/KeelHook.sol";
import {KeelMeter} from "../../src/KeelMeter.sol";
import {PlacementLib} from "../../src/libraries/PlacementLib.sol";
import {MandateLib} from "../../src/libraries/MandateLib.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";

/// @notice A real PoolManager, a KeelHook mined at an address carrying exactly its three permission bits, a 0.3%
/// pool on it, and funded actors. Blocks are advanced with explicit counters, never vm.roll(block.number + 1) in a
/// loop (CLAUDE.md toolchain pitfall).
abstract contract KeelHarness is Deployers {
    using StateLibrary for IPoolManager;

    uint160 internal constant FLAGS =
        uint160(Hooks.AFTER_INITIALIZE_FLAG | Hooks.BEFORE_SWAP_FLAG | Hooks.BEFORE_SWAP_RETURNS_DELTA_FLAG);
    uint32 internal constant N = 300;

    KeelHook internal hook;
    KeelMeter internal meter;
    PoolKey internal pkey;
    PoolId internal pid;

    address internal issuer = makeAddr("issuer");
    address internal curator = makeAddr("curator");
    address internal watcher = makeAddr("watcher");
    address internal stranger = makeAddr("stranger");

    uint256 internal blk; // explicit block counter

    function _setUpKeel() internal {
        deployFreshManagerAndRouters();
        deployMintAndApprove2Currencies();
        meter = new KeelMeter(manager);
        address at = address(FLAGS | (uint160(0x4444) << 144));
        deployCodeTo("src/KeelHook.sol:KeelHook", abi.encode(manager, meter, uint32(300), uint32(7_200)), at);
        hook = KeelHook(at);
        (pkey, pid) = initPool(currency0, currency1, IHooks(at), 3000, 60, SQRT_PRICE_1_1);
        address[3] memory actors = [issuer, curator, watcher];
        for (uint256 i; i < actors.length; i++) {
            MockERC20(Currency.unwrap(currency0)).mint(actors[i], 1e30);
            MockERC20(Currency.unwrap(currency1)).mint(actors[i], 1e30);
            vm.startPrank(actors[i]);
            MockERC20(Currency.unwrap(currency0)).approve(at, type(uint256).max);
            MockERC20(Currency.unwrap(currency1)).approve(at, type(uint256).max);
            vm.stopPrank();
        }
        blk = block.number;
    }

    function _roll(uint256 k) internal {
        blk += k;
        vm.roll(blk);
    }

    // ---------------------------------------------------------------- book and price

    function _lp(int24 lower, int24 upper, uint128 liq) internal {
        modifyLiquidityRouter.modifyLiquidity(
            pkey, ModifyLiquidityParams({tickLower: lower, tickUpper: upper, liquidityDelta: int256(uint256(liq)), salt: 0}), ""
        );
    }

    /// @notice Push the price to `tick` with an exact-input swap limited there.
    function _pushTo(int24 tick) internal {
        (, int24 now_,,) = manager.getSlot0(pid);
        bool zeroForOne = tick < now_;
        swapRouter.swap(
            pkey,
            SwapParams({
                zeroForOne: zeroForOne,
                amountSpecified: -int256(uint256(type(uint128).max)),
                sqrtPriceLimitX96: TickMath.getSqrtPriceAtTick(tick)
            }),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
    }

    // ---------------------------------------------------------------- mandate helpers

    function _terms(uint128 depth0, uint128 depth1) internal view returns (KeelHook.Terms memory t) {
        t = KeelHook.Terms({
            payCurrency: currency1,
            xBps: 100,
            depth0: depth0,
            depth1: depth1,
            stalenessBlocks: N,
            retainerPerBlock: 1e15,
            termBlocks: 2_000,
            acceptDeadline: uint64(blk + 100),
            minBondLiquidity: 1e18
        });
    }

    function _bands1(int24 lower, int24 upper, uint128 liq) internal pure returns (KeelHook.Band[] memory b) {
        b = new KeelHook.Band[](1);
        b[0] = KeelHook.Band({lower: lower, upper: upper, liquidity: liq});
    }

    function _post(KeelHook.Terms memory t) internal returns (uint256 id) {
        vm.prank(issuer);
        id = hook.post(pkey, t);
    }

    function _accept(uint256 id, KeelHook.Band[] memory b) internal {
        vm.prank(curator);
        hook.accept(id, b, 1e18);
    }

    /// @notice The standard book: thin background over ±6000, a mandate for 4e17 each side at ±1%, accepted with a
    /// 1e20 band at ±300 (contribution ≈ 5e17 each side at spot).
    function _standard() internal returns (uint256 id) {
        _lp(-6000, 6000, 1e18);
        id = _post(_terms(4e17, 4e17));
        _accept(id, _bands1(-300, 300, 1e20));
    }

    function _prove(address who, uint256 id) internal returns (DepthLib.Verdict v) {
        vm.prank(who);
        v = hook.prove(id);
    }

    function _status(uint256 id) internal view returns (MandateLib.Status s) {
        (s,,,,,,) = hook.mandate(id);
    }

    function _accrued(uint256 id) internal view returns (uint128 a) {
        (,,, a,,,) = hook.mandate(id);
    }

    function _escrow(uint256 id) internal view returns (uint128 e) {
        (,, e,,,,) = hook.mandate(id);
    }

    function _paidBlocks(uint256 id) internal view returns (uint64 p) {
        (,,,, p,,) = hook.mandate(id);
    }

    function _openedAt(uint256 id) internal view returns (uint64 o) {
        (,,,,, o,) = hook.mandate(id);
    }

    function _claims(address who, Currency c) internal view returns (uint256) {
        return manager.balanceOf(who, c.toId());
    }

    function _posLiq(int24 lower, int24 upper, bytes32 salt) internal view returns (uint128) {
        return manager.getPositionLiquidity(pid, Position.calculatePositionKey(address(hook), lower, upper, salt));
    }

    function _bondLiq(uint256 id) internal view returns (uint128) {
        (int24 lo, int24 hi,) = hook.bondOf(id);
        return _posLiq(lo, hi, PlacementLib.BOND_SALT);
    }
}
