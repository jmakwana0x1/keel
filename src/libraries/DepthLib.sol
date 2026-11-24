// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ITickSource} from "../interfaces/ITickSource.sol";
import {SwapMath} from "@uniswap/v4-core/src/libraries/SwapMath.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {SqrtPriceMath} from "@uniswap/v4-core/src/libraries/SqrtPriceMath.sol";
import {LiquidityMath} from "@uniswap/v4-core/src/libraries/LiquidityMath.sol";

/// @title DepthLib — the depth meter
/// @notice Measures the input a swap must pay to move a pool's price from an anchor to a target, by replaying
/// the swap the pool would execute: an exact-input swap with an unbounded amount and the price limit set at the
/// target. Every step is v4-core's own SwapMath.computeSwapStep, in the pool's order, with the pool's rounding.
///
/// Measured quantity (docs/ARCHITECTURE.md D0.4): `depth` is the PRE-FEE input; `fee` is the fee on top.
/// depth + fee is what the PoolManager charges, and is what the PoolManager differential compares.
///
/// Zero-liquidity ranges cost nothing to cross. That is v4 semantics and it is what produces saturation.
///
/// Three unusable or partial states, each named and returned, never priced off (CLAUDE.md rule 1):
///   - capped:          MAX_STEPS reached before the target price or the depth target. depth is a LOWER bound.
///                      A lower bound that clears the target proves compliance; one that falls short proves
///                      nothing. A capped walk therefore never declares a breach.
///   - anchorMismatch:  a crossing would drive liquidity below zero, so the starting liquidity does not belong
///                      to the starting price. No measurement exists. (Hydra's _cross sentinel.)
///   - exhausted:       the unbounded input ran out before the target (unreachable for realistic pools); treated
///                      like capped: a lower bound.
library DepthLib {
    /// @notice Steps (word-boundary steps and tick crossings) the walk performs before giving up.
    /// @dev 128 covers 100% of the 20,700 Phase 0 walks on real books out to ±10% (max 97, p99 96). Hydra's 64
    /// would cap 8.6% of ±10% walks. The meter runs in prove(), off the swap path (docs/ARCHITECTURE.md D1.1).
    uint256 internal constant MAX_STEPS = 128;

    /// @dev Exact-input amount large enough that no real walk exhausts it, small enough that the fee mulDiv
    /// cannot overflow.
    int256 internal constant UNBOUNDED_IN = -int256(uint256(type(uint128).max));

    enum Verdict {
        COMPLIANT, // depth reached the target (possibly on a capped walk: a lower bound that clears proves it)
        BREACH, // a complete walk reached the target price with depth short of the target
        INCONCLUSIVE, // capped or exhausted short of the target: a lower bound that proves nothing
        UNUSABLE // anchor mismatch: no measurement exists
    }

    struct DepthParams {
        uint160 sqrtPriceStartX96; // anchor price
        int24 tickStart; // tick of the anchor price
        uint128 liquidityStart; // active liquidity at the anchor
        uint160 sqrtPriceTargetX96; // price the walk moves to; direction follows from target vs anchor
        uint256 depthTarget; // pre-fee depth that proves compliance; 0 = measure to the target price, no early stop
        uint24 feePips;
        int24 tickSpacing;
        ITickSource source;
    }

    struct Measurement {
        uint256 depth; // pre-fee input accumulated
        uint256 fee; // fee on top of depth
        uint256 steps; // loop iterations: tick crossings + word-boundary steps + the final partial step
        uint256 crossings; // initialized ticks crossed
        bool reachedPrice; // the walk arrived at the target price
        bool proven; // depthTarget != 0 and depth >= depthTarget
        bool capped; // stopped at MAX_STEPS short of both the price and the depth target
        bool exhausted; // the unbounded input ran out short of the target price
        bool anchorMismatch; // liquidity would go negative on a crossing
    }

    /// @notice The meter. Stops the instant accumulated depth reaches `depthTarget`, which is what makes the
    /// compliant path cheap.
    function measure(DepthParams memory p) internal view returns (uint256 depth, bool proven, bool capped) {
        Measurement memory m = walk(p);
        return (m.depth, m.proven, m.capped);
    }

    /// @notice Full measurement record.
    function walk(DepthParams memory p) internal view returns (Measurement memory m) {
        if (p.sqrtPriceTargetX96 == p.sqrtPriceStartX96) {
            m.reachedPrice = true;
            m.proven = p.depthTarget == 0 ? false : m.depth >= p.depthTarget;
            return m;
        }
        bool zeroForOne = p.sqrtPriceTargetX96 < p.sqrtPriceStartX96;
        uint160 sqrtPriceX96 = p.sqrtPriceStartX96;
        int24 tick = p.tickStart;
        uint128 liquidity = p.liquidityStart;
        int256 remaining = UNBOUNDED_IN;

        while (sqrtPriceX96 != p.sqrtPriceTargetX96) {
            if (p.depthTarget != 0 && m.depth >= p.depthTarget) break; // early termination: proven
            if (m.steps == MAX_STEPS) {
                m.capped = true;
                break;
            }
            if (remaining == 0) {
                m.exhausted = true;
                break;
            }
            (int24 tickNext, bool initialized) =
                p.source.nextInitializedTickWithinOneWord(tick, p.tickSpacing, zeroForOne);
            if (tickNext < TickMath.MIN_TICK) tickNext = TickMath.MIN_TICK;
            else if (tickNext > TickMath.MAX_TICK) tickNext = TickMath.MAX_TICK;
            uint160 sqrtPriceNextX96 = TickMath.getSqrtPriceAtTick(tickNext);
            uint160 stepTarget = zeroForOne
                ? (sqrtPriceNextX96 < p.sqrtPriceTargetX96 ? p.sqrtPriceTargetX96 : sqrtPriceNextX96)
                : (sqrtPriceNextX96 > p.sqrtPriceTargetX96 ? p.sqrtPriceTargetX96 : sqrtPriceNextX96);

            (uint160 sqrtPriceAfterX96, uint256 stepIn,, uint256 stepFee) =
                SwapMath.computeSwapStep(sqrtPriceX96, stepTarget, liquidity, remaining, p.feePips);
            unchecked {
                remaining += int256(stepIn + stepFee);
                m.depth += stepIn;
                m.fee += stepFee;
                m.steps++;
            }

            if (sqrtPriceAfterX96 == sqrtPriceNextX96) {
                if (initialized) {
                    int128 net = p.source.liquidityNetAt(tickNext);
                    if (zeroForOne) net = -net;
                    if (net < 0 && uint128(-net) > liquidity) {
                        m.anchorMismatch = true;
                        sqrtPriceX96 = sqrtPriceAfterX96;
                        break;
                    }
                    liquidity = LiquidityMath.addDelta(liquidity, net);
                    unchecked {
                        m.crossings++;
                    }
                }
                tick = zeroForOne ? tickNext - 1 : tickNext;
            } else if (sqrtPriceAfterX96 != stepTarget) {
                // stopped inside the step: only the unbounded input running out can do this
                sqrtPriceX96 = sqrtPriceAfterX96;
                m.exhausted = true;
                break;
            }
            sqrtPriceX96 = sqrtPriceAfterX96;
        }
        m.reachedPrice = sqrtPriceX96 == p.sqrtPriceTargetX96 && !m.anchorMismatch;
        m.proven = p.depthTarget != 0 && m.depth >= p.depthTarget;
        if (m.proven) m.capped = false; // a lower bound that clears the target is a proof, not a partial
    }

    /// @notice The only mapping from a measurement to a decision. A capped or exhausted walk never breaches.
    function verdict(Measurement memory m) internal pure returns (Verdict) {
        if (m.anchorMismatch) return Verdict.UNUSABLE;
        if (m.proven) return Verdict.COMPLIANT;
        if (m.capped || m.exhausted) return Verdict.INCONCLUSIVE;
        return Verdict.BREACH;
    }

    /// @notice A curator band's contribution to the pre-fee depth between `anchor` and `target`, in closed form.
    /// contribution = L_c · (√p(min(target, upper)) − √p(max(anchor, lower)))   (token1, upward)
    ///              = L_c · (1/√p(max(target, lower)) − 1/√p(min(anchor, upper)))   (token0, downward)
    /// Not a second walk: the meter targets a fixed price, so the same ticks are traversed with and without the
    /// band, and each step's input is linear in that step's liquidity. Rounded DOWN (never over-credits).
    /// @dev Integer caveat: the walk rounds every step UP, so the two-walk difference can exceed this closed form by
    /// at most one unit per step of the walk with the band (docs/ARCHITECTURE.md D1.2). It is exact in real numbers.
    function contribution(uint128 liquidity, int24 lower, int24 upper, int24 anchor, int24 target)
        internal
        pure
        returns (uint256)
    {
        if (target > anchor) {
            int24 lo = anchor > lower ? anchor : lower;
            int24 hi = target < upper ? target : upper;
            if (hi <= lo) return 0;
            return SqrtPriceMath.getAmount1Delta(
                TickMath.getSqrtPriceAtTick(lo), TickMath.getSqrtPriceAtTick(hi), liquidity, false
            );
        } else if (target < anchor) {
            int24 lo = target > lower ? target : lower;
            int24 hi = anchor < upper ? anchor : upper;
            if (hi <= lo) return 0;
            return SqrtPriceMath.getAmount0Delta(
                TickMath.getSqrtPriceAtTick(lo), TickMath.getSqrtPriceAtTick(hi), liquidity, false
            );
        }
        return 0;
    }
}
