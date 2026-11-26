// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Position} from "@uniswap/v4-core/src/libraries/Position.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {ModifyLiquidityParams, SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {PoolModifyLiquidityTest} from "@uniswap/v4-core/src/test/PoolModifyLiquidityTest.sol";
import {KeelHook} from "../../src/KeelHook.sol";
import {MandateLib} from "../../src/libraries/MandateLib.sol";
import {PlacementLib} from "../../src/libraries/PlacementLib.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";

/// @notice Shared machinery for the mandate invariant suites (VERIFICATION.md §5). Every check that needs the
/// before and after of a single call is made here and recorded as the first named violation; the suites assert there
/// is none. Subclasses supply the bias: which prices they push to, how they reshape, what they do to the book.
///
/// The payment ghost is D2.2 stated independently of the hook's storage: a block is paid iff some proof in it
/// returned COMPLIANT with no reshape earlier in that block, and no reshape followed in the same block.
abstract contract MandateHandlerBase is Test {
    using StateLibrary for IPoolManager;

    struct Env {
        KeelHook hook;
        IPoolManager manager;
        PoolSwapTest swapRouter;
        PoolModifyLiquidityTest lpRouter;
        PoolKey key;
        uint256 id;
        address curator;
        address watcher;
        address trader; // holds tokens approved to the routers (the test contract)
        Currency pay;
        uint256 blk;
    }

    KeelHook internal immutable hook;
    IPoolManager internal immutable manager;
    PoolSwapTest internal immutable swapRouter;
    PoolModifyLiquidityTest internal immutable lpRouter;
    PoolKey internal key;
    PoolId internal immutable pid;
    uint256 internal immutable id;
    address internal immutable curator;
    address internal immutable watcher;
    address internal immutable trader;
    Currency internal immutable pay;
    uint256 internal immutable initialEscrow;
    uint256 internal immutable retainer;

    uint256 public blk;

    // ---- the three premise counters of VERIFICATION.md §5
    uint256 public provedBlocks; // blocks paid on a credited compliant proof (ghost model)
    uint256 public breachesObserved; // proofs that returned BREACH

    // ---- ghost model and observations
    uint256 public ghostPaid;
    uint256 internal lastPaidBlock;
    uint256 internal lastReshapeBlock;
    uint128 internal lastBond;
    bool internal slashed;
    KeelHook.Band[] internal frozenBands;
    uint128[] internal frozenLiq;
    uint256 public slashes;
    uint256 public bountyPaid;
    uint256 public accruedAtSlash;

    string public violation;
    uint256 public violations; // every violation counted, so a mutation that trips several checks shows it
    uint256 public swapsDone; // swaps that EXECUTED through the hook (a no-op push does not count)
    uint256 internal nonce;
    uint256 public attempts;
    uint256 public inactiveSkips;
    uint256 public compliantSeen;
    uint256 public uncreditedSeen;

    constructor(Env memory e) {
        hook = e.hook;
        manager = e.manager;
        swapRouter = e.swapRouter;
        lpRouter = e.lpRouter;
        key = e.key;
        pid = e.key.toId();
        id = e.id;
        curator = e.curator;
        watcher = e.watcher;
        trader = e.trader;
        pay = e.pay;
        blk = e.blk;
        (,, uint128 esc,,,,) = e.hook.mandate(e.id);
        initialEscrow = esc;
        retainer = e.hook.terms(e.id).retainerPerBlock;
        lastBond = bondLiquidity();
    }

    // ------------------------------------------------------------------ views the suites read

    function bondLiquidity() public view returns (uint128) {
        (int24 lo, int24 hi,) = hook.bondOf(id);
        return manager.getPositionLiquidity(pid, Position.calculatePositionKey(address(hook), lo, hi, PlacementLib.BOND_SALT));
    }

    function status() public view returns (MandateLib.Status s) {
        (s,,,,,,) = hook.mandate(id);
    }

    // ------------------------------------------------------------------ helpers

    function _fail(string memory why) internal {
        violations++;
        if (bytes(violation).length == 0) violation = why;
    }

    function _bandLiq(KeelHook.Band memory b) internal view returns (uint128) {
        return manager.getPositionLiquidity(
            pid, Position.calculatePositionKey(address(hook), b.lower, b.upper, PlacementLib.mandateSalt(id))
        );
    }

    /// Fuzzer inputs repeat dictionary values, so a bias read straight off them is not the stated bias. Mixing in a
    /// handler nonce makes each decision fresh whatever the inputs look like.
    function _mix(uint256 seed) internal returns (uint256) {
        return uint256(keccak256(abi.encode(seed, ++nonce)));
    }

    function _tick() internal view returns (int24 t) {
        (, t,,) = manager.getSlot0(pid);
    }

    function _active() internal view returns (bool) {
        if (status() != MandateLib.Status.ACTIVE) return false;
        (,,,,,, uint64 endBlock) = hook.mandate(id);
        return blk <= endBlock;
    }

    /// Checks that hold after every call, whatever it was.
    function _after() internal {
        uint128 bond = bondLiquidity();
        if (bond < lastBond) _fail("bond liquidity decreased");
        lastBond = bond;
        (MandateLib.Status s,, uint128 esc, uint128 acc, uint64 paid,,) = hook.mandate(id);
        if (paid != ghostPaid) _fail("paid blocks != ghost-model paid blocks (pay not within proven)");
        if (s == MandateLib.Status.ACTIVE) {
            if (uint256(esc) + acc != initialEscrow) _fail("escrow + accrued not conserved while ACTIVE");
            if (acc != uint256(paid) * retainer) _fail("accrued != paid blocks * retainer");
        }
        // solvency in claims: everything the hook owes in the pay currency is held as ERC-6909 claims (D2.4),
        // including a donation parked because the pool had no active liquidity
        (uint256 p0, uint256 p1) = hook.pendingDonation(pid);
        uint256 owed = uint256(esc) + acc + (pay == key.currency0 ? p0 : p1);
        if (manager.balanceOf(address(hook), pay.toId()) < owed) _fail("hook claims below what it owes");
        if (slashed) {
            for (uint256 i; i < frozenBands.length; i++) {
                if (_bandLiq(frozenBands[i]) != frozenLiq[i]) _fail("a slashed curator's liquidity was reduced");
            }
        }
    }

    function _roll(uint256 k) internal {
        blk += bound(k, 1, 20);
        vm.roll(blk);
        _after();
    }

    function _pushTo(int24 t) internal {
        (, int24 now_,,) = manager.getSlot0(pid);
        if (t == now_) return;
        // Rule 3: nothing on the swap path may revert. Invariant runs discard reverting calls
        // (fail_on_revert = false), so an uncaught revert here would vanish; it is recorded as a violation instead.
        vm.prank(trader);
        try swapRouter.swap(
            key,
            SwapParams({
                zeroForOne: t < now_,
                amountSpecified: -int256(uint256(type(uint128).max)),
                sqrtPriceLimitX96: TickMath.getSqrtPriceAtTick(t)
            }),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        ) {
            swapsDone++;
        } catch {
            _fail("a swap reverted on the hook path");
        }
    }

    function _lp(int24 lower, int24 upper, int256 delta) internal {
        vm.prank(trader);
        lpRouter.modifyLiquidity(
            key, ModifyLiquidityParams({tickLower: lower, tickUpper: upper, liquidityDelta: delta, salt: 0}), ""
        );
    }

    /// A proof, with every per-call check. Returns the verdict (UNUSABLE when nothing was proved).
    function _prove(bool byCurator, uint256 segs) internal returns (DepthLib.Verdict v) {
        attempts++;
        if (!_active()) {
            inactiveSkips++;
            return DepthLib.Verdict.UNUSABLE;
        }
        address who = byCurator ? curator : watcher;
        (,,, uint128 accBefore, uint64 paidBefore, uint64 openedBefore,) = hook.mandate(id);
        uint256 claimsBefore = manager.balanceOf(who, pay.toId());
        KeelHook.Band[] memory bands = hook.bandsOf(id);
        uint128[] memory liqBefore = new uint128[](bands.length);
        for (uint256 i; i < bands.length; i++) {
            liqBefore[i] = _bandLiq(bands[i]);
        }

        vm.prank(who);
        v = hook.prove(id, segs);

        bool credited = lastReshapeBlock != blk;
        if (v == DepthLib.Verdict.COMPLIANT) compliantSeen++;
        if (v == DepthLib.Verdict.COMPLIANT && !credited) uncreditedSeen++;
        if (v == DepthLib.Verdict.COMPLIANT && credited && lastPaidBlock != blk) {
            ghostPaid++;
            provedBlocks++;
            lastPaidBlock = blk;
        }
        if (v == DepthLib.Verdict.BREACH) breachesObserved++;
        (,,,, uint64 paidAfter,,) = hook.mandate(id);
        if (paidAfter > paidBefore && v != DepthLib.Verdict.COMPLIANT) _fail("paid on a non-compliant proof");
        for (uint256 i; i < bands.length; i++) {
            if (_bandLiq(bands[i]) != liqBefore[i]) _fail("a proof changed position liquidity");
        }

        if (status() == MandateLib.Status.SLASHED && !slashed) {
            slashed = true;
            slashes++;
            if (v != DepthLib.Verdict.BREACH) _fail("slashed on a non-breach proof");
            if (openedBefore == 0 || blk - openedBefore < hook.terms(id).stalenessBlocks) {
                _fail("slashed before the staleness window elapsed");
            }
            uint256 bounty = manager.balanceOf(who, pay.toId()) - claimsBefore;
            bountyPaid = bounty;
            accruedAtSlash = accBefore;
            if (bounty > accBefore) _fail("slash took more than accrued");
            if (accBefore != 0 && bounty >= accBefore - bounty) _fail("bounty not below donation");
            for (uint256 i; i < bands.length; i++) {
                frozenBands.push(bands[i]);
                frozenLiq.push(_bandLiq(bands[i]));
            }
        }
        _after();
    }

    function _reshape(KeelHook.Band[] memory b) internal {
        if (!_active()) return;
        vm.prank(curator);
        try hook.reshape(id, b) {
            if (lastPaidBlock == blk) {
                ghostPaid--; // the credit taken earlier in this block is void (D2.2)
                provedBlocks--;
                lastPaidBlock = 0;
            }
            lastReshapeBlock = blk;
        } catch {}
        _after();
    }
}
