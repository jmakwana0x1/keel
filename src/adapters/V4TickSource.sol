// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ITickSource} from "../interfaces/ITickSource.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {BitMath} from "@uniswap/v4-core/src/libraries/BitMath.sol";
import {TickBitmap} from "@uniswap/v4-core/src/libraries/TickBitmap.sol";

/// @notice Binds DepthLib (and, in hydra, CounterfactualLib) to a live v4 pool. Carried unchanged from hydra.
///
/// The library itself takes no pool key by design; this is the v4 shim, and it
/// is the only place that knows about PoolManager. Reads go through extsload,
/// so no pool cooperation is required.
///
/// Deliberately not part of CounterfactualLib: an integrator on v3, on a fork,
/// or in a test writes their own twenty-line adapter instead of inheriting v4.
contract V4TickSource is ITickSource {
    using StateLibrary for IPoolManager;

    IPoolManager public immutable manager;
    PoolId public immutable poolId;

    constructor(IPoolManager _manager, PoolId _poolId) {
        manager = _manager;
        poolId = _poolId;
    }

    /// @inheritdoc ITickSource
    /// @dev Mirrors TickBitmap.nextInitializedTickWithinOneWord, reading the
    /// word through extsload rather than from a storage mapping.
    function nextInitializedTickWithinOneWord(int24 tick, int24 tickSpacing, bool lte)
        external
        view
        returns (int24 next, bool initialized)
    {
        unchecked {
            int24 compressed = TickBitmap.compress(tick, tickSpacing);

            if (lte) {
                (int16 wordPos, uint8 bitPos) = TickBitmap.position(compressed);
                uint256 mask = type(uint256).max >> (uint256(type(uint8).max) - bitPos);
                uint256 masked = manager.getTickBitmap(poolId, wordPos) & mask;

                initialized = masked != 0;
                next = initialized
                    ? (compressed - int24(uint24(bitPos - BitMath.mostSignificantBit(masked)))) * tickSpacing
                    : (compressed - int24(uint24(bitPos))) * tickSpacing;
            } else {
                (int16 wordPos, uint8 bitPos) = TickBitmap.position(++compressed);
                uint256 mask = ~((1 << bitPos) - 1);
                uint256 masked = manager.getTickBitmap(poolId, wordPos) & mask;

                initialized = masked != 0;
                next = initialized
                    ? (compressed + int24(uint24(BitMath.leastSignificantBit(masked) - bitPos))) * tickSpacing
                    : (compressed + int24(uint24(type(uint8).max - bitPos))) * tickSpacing;
            }
        }
    }

    /// @inheritdoc ITickSource
    function liquidityNetAt(int24 tick) external view returns (int128 net) {
        (, net) = manager.getTickLiquidity(poolId, tick);
    }
}
