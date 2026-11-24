// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ITickSource} from "../../src/interfaces/ITickSource.sol";
import {BitMath} from "@uniswap/v4-core/src/libraries/BitMath.sol";
import {TickBitmap} from "@uniswap/v4-core/src/libraries/TickBitmap.sol";

/// @notice An ITickSource over a book loaded from a file, held in this contract's own storage. The word search is
/// V4TickSource's, reading `bitmap` instead of the PoolManager. Used to replay the Phase 0 mainnet books.
contract BookTickSource is ITickSource {
    mapping(int16 => uint256) public bitmap;
    mapping(int24 => int128) public nets;
    int16[] internal words_;
    int24[] internal ticks_;

    function load(int24 tickSpacing, int24[] calldata ticks, int128[] calldata liqNets) external {
        for (uint256 i; i < words_.length; i++) delete bitmap[words_[i]];
        for (uint256 i; i < ticks_.length; i++) delete nets[ticks_[i]];
        delete words_;
        delete ticks_;
        for (uint256 i; i < ticks.length; i++) {
            int24 c = TickBitmap.compress(ticks[i], tickSpacing);
            (int16 w, uint8 b) = TickBitmap.position(c);
            if (bitmap[w] == 0) words_.push(w);
            bitmap[w] |= (uint256(1) << b);
            nets[ticks[i]] = liqNets[i];
            ticks_.push(ticks[i]);
        }
    }

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
                uint256 masked = bitmap[wordPos] & mask;
                initialized = masked != 0;
                next = initialized
                    ? (compressed - int24(uint24(bitPos - BitMath.mostSignificantBit(masked)))) * tickSpacing
                    : (compressed - int24(uint24(bitPos))) * tickSpacing;
            } else {
                (int16 wordPos, uint8 bitPos) = TickBitmap.position(++compressed);
                uint256 mask = ~((1 << bitPos) - 1);
                uint256 masked = bitmap[wordPos] & mask;
                initialized = masked != 0;
                next = initialized
                    ? (compressed + int24(uint24(BitMath.leastSignificantBit(masked) - bitPos))) * tickSpacing
                    : (compressed + int24(uint24(type(uint8).max - bitPos))) * tickSpacing;
            }
        }
    }

    function liquidityNetAt(int24 tick) external view returns (int128) {
        return nets[tick];
    }
}
