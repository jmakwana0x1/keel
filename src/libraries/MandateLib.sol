// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {DepthLib} from "./DepthLib.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FixedPointMathLib} from "solmate/src/utils/FixedPointMathLib.sol";

/// @title MandateLib — the lifecycle as pure transitions
/// @notice Every decision the hook takes about a proof is a pure function here, so it can be unit-tested and stated
/// symbolically without a PoolManager. The hook only stores what these functions return.
library MandateLib {
    enum Status {
        NONE,
        POSTED,
        ACTIVE,
        SETTLED,
        EXPIRED,
        SLASHED
    }

    /// @notice A side's verdict and the pool-wide combination (D2.1).
    /// Per side: curator contribution >= target -> COMPLIANT (no walk); else the pool walk decides:
    /// proven (pool deep through others) -> INCONCLUSIVE; capped/exhausted -> INCONCLUSIVE;
    /// anchor mismatch -> UNUSABLE; otherwise BREACH.
    function sideVerdict(bool curatorCovers, DepthLib.Measurement memory poolWalk)
        internal
        pure
        returns (DepthLib.Verdict)
    {
        if (curatorCovers) return DepthLib.Verdict.COMPLIANT;
        DepthLib.Verdict v = DepthLib.verdict(poolWalk);
        // The pool clears the target, but not through this curator: the issuer has the depth, nobody is paid for
        // someone else's, nobody is slashed.
        if (v == DepthLib.Verdict.COMPLIANT) return DepthLib.Verdict.INCONCLUSIVE;
        return v;
    }

    /// @notice Combine the two sides. `mandatedUp`/`mandatedDown` false means that side carries no target and is
    /// ignored. Precedence: UNUSABLE > BREACH > (all COMPLIANT ? COMPLIANT : INCONCLUSIVE).
    function combine(bool mandatedUp, DepthLib.Verdict up, bool mandatedDown, DepthLib.Verdict down)
        internal
        pure
        returns (DepthLib.Verdict)
    {
        if ((mandatedUp && up == DepthLib.Verdict.UNUSABLE) || (mandatedDown && down == DepthLib.Verdict.UNUSABLE)) {
            return DepthLib.Verdict.UNUSABLE;
        }
        if ((mandatedUp && up == DepthLib.Verdict.BREACH) || (mandatedDown && down == DepthLib.Verdict.BREACH)) {
            return DepthLib.Verdict.BREACH;
        }
        bool okUp = !mandatedUp || up == DepthLib.Verdict.COMPLIANT;
        bool okDown = !mandatedDown || down == DepthLib.Verdict.COMPLIANT;
        return okUp && okDown ? DepthLib.Verdict.COMPLIANT : DepthLib.Verdict.INCONCLUSIVE;
    }

    struct ProofEffect {
        uint64 shortfallOpenedAt; // new value; 0 = no open window
        bool pay; // credit one block's retainer
        bool opened; // this proof opened a window
        bool closed; // this proof closed a window
        bool finalise; // this proof finalises a breach
    }

    /// @notice The staleness rule (D1.3, D2.3), as a pure transition.
    /// - A credited COMPLIANT proof closes any open window and pays one block, once per block.
    /// - A BREACH proof opens a window if none is open; finalises if one has been open for >= N blocks.
    /// - INCONCLUSIVE, UNUSABLE and uncredited COMPLIANT proofs change nothing.
    /// @param credited False when the mandate's bands were reshaped earlier in this block (D2.2).
    /// @param paidThisBlock True when a credited proof already paid this block.
    function applyProof(
        uint64 shortfallOpenedAt,
        uint32 stalenessBlocks,
        uint64 blockNumber,
        DepthLib.Verdict v,
        bool credited,
        bool paidThisBlock
    ) internal pure returns (ProofEffect memory e) {
        e.shortfallOpenedAt = shortfallOpenedAt;
        if (v == DepthLib.Verdict.COMPLIANT) {
            if (!credited) return e;
            e.closed = shortfallOpenedAt != 0;
            e.shortfallOpenedAt = 0;
            e.pay = !paidThisBlock;
        } else if (v == DepthLib.Verdict.BREACH) {
            if (shortfallOpenedAt == 0) {
                e.shortfallOpenedAt = blockNumber;
                e.opened = true;
            } else if (blockNumber - shortfallOpenedAt >= stalenessBlocks) {
                e.finalise = true;
            }
        }
    }

    /// @notice An upper bound on the steps a walk to `x` can take on ANY book in a pool of this spacing (D2.10).
    /// Every step of a walk stops at a multiple of the tick spacing (an initialised tick or a bitmap-word edge, both
    /// multiples) or at the target, and each stop is a distinct price. The downward side is the wider one
    /// (|ln(1-x)| > ln(1+x)), spanning R ticks, so a walk takes at most R/spacing + 3 steps (the +3 covers the
    /// partial ticks at either end and the final step). The book does not enter: dust cannot raise it.
    function stepBound(uint256 xBps, int24 tickSpacing) internal pure returns (uint256) {
        uint256 f = FixedPointMathLib.sqrt((10_000 - xBps) * 1e32); // sqrt(1 - x) * 1e18
        int24 t = TickMath.getTickAtSqrtPrice(uint160(f * (1 << 96) / 1e18));
        uint256 r = uint256(int256(-t)) + 1;
        return r / uint256(int256(tickSpacing)) + 3;
    }

    /// @notice The prover's share of a slash. Below 5_000, so the bounty is strictly below the donation (D2.4).
    uint256 internal constant BOUNTY_BPS = 2_000;

    /// @notice Split a slash (D2.4): bounty to the prover, the rest donated to in-range LPs.
    function slashSplit(uint256 accrued) internal pure returns (uint256 bounty, uint256 donation) {
        bounty = accrued * BOUNTY_BPS / 10_000;
        donation = accrued - bounty;
    }
}
