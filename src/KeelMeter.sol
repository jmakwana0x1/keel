// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {DepthLib} from "./libraries/DepthLib.sol";
import {V4TickSource} from "./adapters/V4TickSource.sol";
import {ITickSource} from "./interfaces/ITickSource.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";

/// @title KeelMeter — the tick-walk meter as its own immutable contract
/// @notice Holds DepthLib.walk and deploys one V4TickSource per pool. Split out of KeelHook for EIP-170 (D2.7).
/// Stateless apart from the write-once tick-source registry; no owner, no upgrade path. The hook reaches it by a
/// plain CALL to an address fixed at the hook's deploy. The walk it runs is the code Phase 1 diffed at zero
/// tolerance, unchanged.
contract KeelMeter {
    IPoolManager public immutable manager;
    mapping(PoolId => ITickSource) public sourceOf;

    constructor(IPoolManager _manager) {
        manager = _manager;
    }

    /// @notice The pool's tick source, deployed on first use. Permissionless and idempotent.
    function source(PoolId pid) external returns (ITickSource s) {
        s = sourceOf[pid];
        if (address(s) == address(0)) {
            s = new V4TickSource(manager, pid);
            sourceOf[pid] = s;
        }
    }

    function walk(DepthLib.DepthParams memory p) external view returns (DepthLib.Measurement memory) {
        return DepthLib.walk(p);
    }
}
