// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

/// @notice The only pool knowledge CounterfactualLib needs.
///
/// Kept as an interface, rather than a PoolKey or a PoolManager reference, so
/// the counterfactual walk is usable against any concentrated-liquidity pool:
/// v4, v3, a fork, or a test double. An implementation answers two questions
/// about the tick structure and nothing else.
interface ITickSource {
    /// @notice Next initialized tick at or beyond `tick`, searching within one
    /// bitmap word, in the direction given by `lte`.
    /// @param lte True when searching downward (zeroForOne).
    /// @return next The tick found, or the word boundary if none is initialized.
    /// @return initialized Whether `next` is an initialized tick.
    function nextInitializedTickWithinOneWord(int24 tick, int24 tickSpacing, bool lte)
        external
        view
        returns (int24 next, bool initialized);

    /// @notice Net liquidity change on crossing `tick` upward.
    function liquidityNetAt(int24 tick) external view returns (int128);
}
