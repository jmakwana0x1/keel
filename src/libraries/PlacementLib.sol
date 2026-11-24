// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";

/// @title PlacementLib — the single positive-liquidity site
/// @notice From Hydra's PlacementLib, cut to the one function KEEL needs. `mintAt` is the ONLY call in this codebase
/// that adds liquidity. Its liquidity argument is a `uint128`, widened to `int256` exactly once, here. No caller,
/// present or future, can hand it a negative value: the type is the guarantee (CLAUDE.md rule 2).
/// The negative site is not here. It lives in KeelHook._burnMandate, next to the check that gates it.
library PlacementLib {
    /// @notice The bond salt. Positions under it are mint-only forever (D2.5).
    bytes32 internal constant BOND_SALT = keccak256("KEEL.BOND");

    /// @notice The salt of a mandate's reshapeable bands.
    function mandateSalt(uint256 mandateId) internal pure returns (bytes32) {
        return keccak256(abi.encode(mandateId));
    }

    /// @notice THE MINT SITE. `internal` rather than `private` so the symbolic layer can call it at the calldata
    /// boundary (VERIFICATION.md §6); widening visibility adds no risk because the parameter is a uint128.
    function mintAt(
        IPoolManager manager,
        PoolKey memory key,
        int24 tickLower,
        int24 tickUpper,
        uint128 liquidity,
        bytes32 salt
    ) internal returns (BalanceDelta delta, BalanceDelta feesAccrued) {
        (delta, feesAccrued) = manager.modifyLiquidity(
            key,
            ModifyLiquidityParams({
                tickLower: tickLower,
                tickUpper: tickUpper,
                liquidityDelta: int256(uint256(liquidity)),
                salt: salt
            }),
            ""
        );
    }
}
