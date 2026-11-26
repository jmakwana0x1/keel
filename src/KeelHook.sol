// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {BaseHook} from "./base/BaseHook.sol";
import {DepthLib} from "./libraries/DepthLib.sol";
import {MandateLib} from "./libraries/MandateLib.sol";
import {PlacementLib} from "./libraries/PlacementLib.sol";
import {KeelMeter} from "./KeelMeter.sol";
import {ITickSource} from "./interfaces/ITickSource.sol";

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency, CurrencyLibrary} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {BeforeSwapDelta, BeforeSwapDeltaLibrary} from "@uniswap/v4-core/src/types/BeforeSwapDelta.sol";
import {ModifyLiquidityParams, SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {FixedPointMathLib} from "solmate/src/utils/FixedPointMathLib.sol";
import {SafeTransferLib} from "solmate/src/utils/SafeTransferLib.sol";
import {ERC20} from "solmate/src/tokens/ERC20.sol";

/// @title KeelHook — depth as a contract
/// @notice A curator commits depth at ±x around the live price, is paid one block's retainer per proved-compliant
/// block, and is slashed when the pool is proved short for N blocks with no compliant proof in between.
/// No owner, no proxy, no pause, no rescue. Every constant is immutable at deploy.
///
/// Capability sites (CLAUDE.md rule 2):
///   - positive liquidity: PlacementLib.mintAt, one site, uint128 widened once
///   - negative liquidity: _burnMandate, one site, gated on (actor == curator && ACTIVE) at the site itself
///   - value leaving escrow: _release, one site
contract KeelHook is BaseHook, IUnlockCallback {
    using StateLibrary for IPoolManager;
    using PoolIdLibrary for PoolKey;
    using CurrencyLibrary for Currency;

    // ------------------------------------------------------------------ constants (immutable, none governable)

    uint256 public constant MAX_BANDS = 4;
    uint256 public constant BOUNTY_BPS = MandateLib.BOUNTY_BPS; // < 5_000: bounty strictly below donation (D2.4)
    uint256 public constant SETTLE_GRACE_BLOCKS = 7_200;
    uint256 public constant MAX_X_BPS = 5_000;
    uint256 public constant MAX_TERM_BLOCKS = 10_000_000;

    /// @notice Staleness bounds in BLOCKS of this chain. D0.15 measured them as 300 and 7,200 mainnet blocks
    /// (1 h and 24 h); the deploy converts those durations at the chain's block time (D2.3).
    uint32 public immutable MIN_STALENESS_BLOCKS;
    uint32 public immutable MAX_STALENESS_BLOCKS;

    /// @notice The meter (D2.7): DepthLib.walk in its own immutable contract, fixed at deploy.
    KeelMeter public immutable meter;

    // ------------------------------------------------------------------ types

    struct Band {
        int24 lower;
        int24 upper;
        uint128 liquidity;
    }

    struct Terms {
        Currency payCurrency; // one of the pool's two currencies
        uint16 xBps; // half-width of the measured move
        uint128 depth0; // token0 needed to push the price DOWN by x (0 = side not mandated)
        uint128 depth1; // token1 needed to push the price UP by x (0 = side not mandated)
        uint32 stalenessBlocks; // N
        uint128 retainerPerBlock;
        uint32 termBlocks;
        uint64 acceptDeadline;
        uint128 minBondLiquidity;
    }

    struct Mandate {
        PoolId poolId;
        address issuer;
        Terms terms;
        MandateLib.Status status;
        address curator;
        uint64 startBlock;
        uint64 endBlock;
        uint128 escrow;
        uint128 accrued;
        uint64 paidBlocks;
        uint64 shortfallOpenedAt;
        uint64 lastCreditedBlock;
        uint64 lastPaidBlock;
        uint64 voidRestoreOpenedAt;
        uint64 lastReshapeBlock;
        uint8 bandCount;
        Band[MAX_BANDS] bands;
        int24 bondLower;
        int24 bondUpper;
        uint128 bondLiquidity;
    }

    // ------------------------------------------------------------------ state

    mapping(PoolId => PoolKey) internal keys;
    mapping(PoolId => bool) public known;
    mapping(PoolId => ITickSource) public tickSource;
    mapping(PoolId => uint256) public liveMandate;
    mapping(PoolId => uint256[2]) internal pending; // pendingDonation, by currency index
    mapping(uint256 => Mandate) internal mandates;
    uint256 public nextMandateId = 1;

    // ------------------------------------------------------------------ events and errors

    event Posted(uint256 indexed id, PoolId indexed poolId, address indexed issuer, uint256 escrow);
    event Accepted(uint256 indexed id, address indexed curator, uint64 endBlock, uint128 bondLiquidity);
    event Proved(
        uint256 indexed id, address indexed prover, DepthLib.Verdict verdict, uint256 contributionUp,
        uint256 contributionDown, bool credited, bool paid
    );
    event ShortfallOpened(uint256 indexed id, uint64 atBlock);
    event ShortfallClosed(uint256 indexed id, uint64 openedAt, uint64 closedAt);
    event CreditVoided(uint256 indexed id, uint64 atBlock, bool refunded, uint64 windowRestoredTo);
    event Reshaped(uint256 indexed id, uint8 bandCount);
    event Slashed(uint256 indexed id, address indexed prover, uint256 bounty, uint256 donation, uint256 refund);
    event Settled(uint256 indexed id, uint256 vested, uint256 refund, bool byCurator);
    event Expired(uint256 indexed id, uint256 refund);
    event DonationParked(PoolId indexed poolId, uint256 amount0, uint256 amount1);
    event Donated(PoolId indexed poolId, uint256 amount0, uint256 amount1);

    error UnknownPool();
    error PoolBusy();
    error BadTerms();
    error BadBands();
    error BadState();
    error NotCurator();
    error TermOver();
    error TooEarly();

    uint8 internal constant A_DEPOSIT = 1;
    uint8 internal constant A_POSITIONS = 2;
    uint8 internal constant A_DONATE = 3;

    constructor(IPoolManager manager, KeelMeter _meter, uint32 minStalenessBlocks, uint32 maxStalenessBlocks)
        BaseHook(manager)
    {
        if (minStalenessBlocks == 0 || maxStalenessBlocks < minStalenessBlocks) revert BadTerms();
        if (_meter.manager() != manager) revert BadTerms();
        meter = _meter;
        MIN_STALENESS_BLOCKS = minStalenessBlocks;
        MAX_STALENESS_BLOCKS = maxStalenessBlocks;
    }

    function getHookPermissions() public pure override returns (Hooks.Permissions memory p) {
        p.afterInitialize = true;
        p.beforeSwap = true;
        p.beforeSwapReturnDelta = true;
    }

    // ------------------------------------------------------------------ hook callbacks (nothing here can revert)

    function afterInitialize(address, PoolKey calldata key, uint160, int24) external override onlyPoolManager returns (bytes4) {
        PoolId pid = key.toId();
        keys[pid] = key;
        known[pid] = true;
        return this.afterInitialize.selector;
    }

    /// @notice O(1) and revert-free. The skim (D1.4) is Phase 3; the bit is set now because bits are fixed at mining.
    function beforeSwap(address, PoolKey calldata, SwapParams calldata, bytes calldata)
        external
        view
        override
        onlyPoolManager
        returns (bytes4, BeforeSwapDelta, uint24)
    {
        return (this.beforeSwap.selector, BeforeSwapDeltaLibrary.ZERO_DELTA, 0);
    }

    // ------------------------------------------------------------------ lifecycle

    /// @notice Post a fixed-rate mandate and fund its full escrow (retainerPerBlock · termBlocks) in `payCurrency`.
    function post(PoolKey calldata key, Terms calldata t) external returns (uint256 id) {
        PoolId pid = key.toId();
        if (!known[pid]) revert UnknownPool();
        if (liveMandate[pid] != 0) revert PoolBusy();
        if (key.currency0.isAddressZero()) revert BadTerms(); // ERC-20 pools only (D2.6)
        if (!(t.payCurrency == key.currency0 || t.payCurrency == key.currency1)) revert BadTerms();
        if (t.stalenessBlocks < MIN_STALENESS_BLOCKS || t.stalenessBlocks > MAX_STALENESS_BLOCKS) revert BadTerms();
        if (t.xBps == 0 || t.xBps > MAX_X_BPS) revert BadTerms();
        // every accepted range is coverable by one segmented proof, whatever the book holds (D2.10)
        if (!meter.coverable(t.xBps, key.tickSpacing)) revert BadTerms();
        if (t.depth0 == 0 && t.depth1 == 0) revert BadTerms();
        if (t.retainerPerBlock == 0 || t.termBlocks == 0 || t.termBlocks > MAX_TERM_BLOCKS) revert BadTerms();
        if (t.acceptDeadline < block.number) revert BadTerms();
        uint256 escrow = uint256(t.retainerPerBlock) * t.termBlocks;
        if (escrow > type(uint128).max) revert BadTerms();

        id = nextMandateId++;
        Mandate storage m = mandates[id];
        m.poolId = pid;
        m.issuer = msg.sender;
        m.terms = t;
        m.status = MandateLib.Status.POSTED;
        m.escrow = uint128(escrow);
        liveMandate[pid] = id;
        if (address(tickSource[pid]) == address(0)) tickSource[pid] = meter.source(pid);

        poolManager.unlock(abi.encode(A_DEPOSIT, abi.encode(msg.sender, t.payCurrency, escrow)));
        emit Posted(id, pid, msg.sender, escrow);
    }

    /// @notice Become the curator: mint the mandate bands and the bond (at the first band, D2.5).
    function accept(uint256 id, Band[] calldata bands, uint128 bondLiquidity) external {
        Mandate storage m = mandates[id];
        if (m.status != MandateLib.Status.POSTED || block.number > m.terms.acceptDeadline) revert BadState();
        if (bondLiquidity < m.terms.minBondLiquidity) revert BadTerms();
        _checkBands(keys[m.poolId].tickSpacing, bands);
        m.curator = msg.sender;
        m.status = MandateLib.Status.ACTIVE;
        m.startBlock = uint64(block.number);
        m.endBlock = uint64(block.number) + m.terms.termBlocks;
        m.bondLower = bands[0].lower;
        m.bondUpper = bands[0].upper;
        m.bondLiquidity = bondLiquidity;
        poolManager.unlock(abi.encode(A_POSITIONS, abi.encode(id, msg.sender, false, bands, bondLiquidity)));
        _storeBands(m, bands);
        emit Accepted(id, msg.sender, m.endBlock, bondLiquidity);
    }

    /// @notice Move the mandate's depth (follow the price). Curator only, ACTIVE only. A reshape voids a credit
    /// already taken in this block, and makes later proofs in this block uncredited (D2.2).
    function reshape(uint256 id, Band[] calldata bands) external {
        Mandate storage m = mandates[id];
        if (m.status != MandateLib.Status.ACTIVE) revert BadState();
        if (msg.sender != m.curator) revert NotCurator();
        _checkBands(keys[m.poolId].tickSpacing, bands);
        uint64 b = uint64(block.number);
        if (m.lastCreditedBlock == b) {
            bool refunded = m.lastPaidBlock == b;
            if (refunded) {
                m.escrow += m.terms.retainerPerBlock;
                m.accrued -= m.terms.retainerPerBlock;
                m.paidBlocks -= 1;
                m.lastPaidBlock = 0;
            }
            uint64 restore = m.voidRestoreOpenedAt;
            if (restore != 0) m.shortfallOpenedAt = restore;
            m.lastCreditedBlock = 0;
            m.voidRestoreOpenedAt = 0;
            emit CreditVoided(id, b, refunded, restore);
        }
        m.lastReshapeBlock = b;
        poolManager.unlock(abi.encode(A_POSITIONS, abi.encode(id, msg.sender, true, bands, uint128(0))));
        _storeBands(m, bands);
        emit Reshaped(id, uint8(bands.length));
    }

    /// @notice Permissionless measurement. Never charges the caller anything but gas (rule: a failed prove costs
    /// only gas). Returns the combined verdict (D2.1) and applies the staleness rule (D2.3).
    function prove(uint256 id) external returns (DepthLib.Verdict v) {
        return _prove(id, 1);
    }

    /// @notice The same proof, with a capped walk continued for up to `segments` segments (D2.10). Anyone may pay
    /// for the extra coverage; a range fragmented to cap one walk is still decidable.
    function prove(uint256 id, uint256 segments) external returns (DepthLib.Verdict v) {
        return _prove(id, segments);
    }

    function _prove(uint256 id, uint256 segments) internal returns (DepthLib.Verdict v) {
        Mandate storage m = mandates[id];
        if (m.status != MandateLib.Status.ACTIVE) revert BadState();
        if (block.number > m.endBlock) revert TermOver();
        uint256 cUp;
        uint256 cDown;
        (v, cUp, cDown) = _measure(m, segments);

        uint64 b = uint64(block.number);
        bool credited = m.lastReshapeBlock != b;
        MandateLib.ProofEffect memory e = MandateLib.applyProof(
            m.shortfallOpenedAt, m.terms.stalenessBlocks, b, v, credited, m.lastCreditedBlock == b
        );
        if (v == DepthLib.Verdict.COMPLIANT && credited) {
            if (m.lastCreditedBlock != b) {
                m.lastCreditedBlock = b;
                m.voidRestoreOpenedAt = 0;
            }
            if (e.closed && m.voidRestoreOpenedAt == 0) m.voidRestoreOpenedAt = m.shortfallOpenedAt;
        }
        if (e.closed) emit ShortfallClosed(id, m.shortfallOpenedAt, b);
        if (e.opened) emit ShortfallOpened(id, b);
        m.shortfallOpenedAt = e.shortfallOpenedAt;

        bool paid;
        if (e.pay && m.escrow >= m.terms.retainerPerBlock) {
            m.escrow -= m.terms.retainerPerBlock;
            m.accrued += m.terms.retainerPerBlock;
            m.paidBlocks += 1;
            m.lastPaidBlock = b;
            paid = true;
        }
        emit Proved(id, msg.sender, v, cUp, cDown, credited, paid);
        if (e.finalise) _slash(id, m, msg.sender);
    }

    /// @notice After the term: the curator settles (withdrawing its bands, the one negative site) any time; anyone
    /// may settle after the grace period, which leaves the curator's bands frozen (D2.5).
    function settle(uint256 id) external {
        Mandate storage m = mandates[id];
        if (m.status != MandateLib.Status.ACTIVE) revert BadState();
        if (block.number <= m.endBlock) revert TooEarly();
        bool byCurator = msg.sender == m.curator;
        if (!byCurator && block.number <= uint256(m.endBlock) + SETTLE_GRACE_BLOCKS) revert TooEarly();
        if (byCurator) {
            poolManager.unlock(abi.encode(A_POSITIONS, abi.encode(id, msg.sender, true, new Band[](0), uint128(0))));
            m.bandCount = 0;
        }
        m.status = MandateLib.Status.SETTLED;
        liveMandate[m.poolId] = 0;
        uint256 vested = m.accrued;
        uint256 refund = m.escrow;
        m.accrued = 0;
        m.escrow = 0;
        _release(m.curator, m.terms.payCurrency, vested);
        _release(m.issuer, m.terms.payCurrency, refund);
        emit Settled(id, vested, refund, byCurator);
    }

    /// @notice A posted mandate nobody accepted in time: refund the issuer.
    function expire(uint256 id) external {
        Mandate storage m = mandates[id];
        if (m.status != MandateLib.Status.POSTED) revert BadState();
        if (block.number <= m.terms.acceptDeadline) revert TooEarly();
        m.status = MandateLib.Status.EXPIRED;
        liveMandate[m.poolId] = 0;
        uint256 refund = m.escrow;
        m.escrow = 0;
        _release(m.issuer, m.terms.payCurrency, refund);
        emit Expired(id, refund);
    }

    /// @notice Donate parked value once the pool has active liquidity again. Anyone.
    function flushDonation(PoolId pid) external {
        _flush(pid);
    }

    // ------------------------------------------------------------------ measurement

    function _measure(Mandate storage m, uint256 segments) internal view returns (DepthLib.Verdict v, uint256 cUp, uint256 cDown) {
        PoolId pid = m.poolId;
        (uint160 sp, int24 tick,, uint24 lpFee) = poolManager.getSlot0(pid);
        uint128 liq = poolManager.getLiquidity(pid);
        DepthLib.Verdict up;
        DepthLib.Verdict down;
        bool mu = m.terms.depth1 != 0;
        bool md = m.terms.depth0 != 0;
        if (mu) (up, cUp) = _side(m, sp, tick, liq, lpFee, true, segments);
        if (md) (down, cDown) = _side(m, sp, tick, liq, lpFee, false, segments);
        v = MandateLib.combine(mu, up, md, down);
    }

    function _side(Mandate storage m, uint160 sp, int24 tick, uint128 liq, uint24 lpFee, bool up, uint256 segments)
        internal
        view
        returns (DepthLib.Verdict, uint256 c)
    {
        uint160 target;
        {
            uint256 x = m.terms.xBps;
            uint256 f = FixedPointMathLib.sqrt((up ? 10_000 + x : 10_000 - x) * 1e32); // √(1 ± x) · 1e18
            uint256 t = FullMath.mulDiv(sp, f, 1e18);
            // a target clamped to the protocol bounds is not at the stated x: decline (rule 1)
            if (t <= TickMath.MIN_SQRT_PRICE || t >= TickMath.MAX_SQRT_PRICE || t == sp) {
                return (DepthLib.Verdict.UNUSABLE, 0);
            }
            target = uint160(t);
        }
        uint256 depthTarget = up ? m.terms.depth1 : m.terms.depth0;
        for (uint256 i; i < m.bandCount; i++) {
            Band storage bd = m.bands[i];
            c += DepthLib.contributionAt(
                bd.liquidity, TickMath.getSqrtPriceAtTick(bd.lower), TickMath.getSqrtPriceAtTick(bd.upper), sp, target
            );
        }
        if (c >= depthTarget) return (DepthLib.Verdict.COMPLIANT, c);
        (DepthLib.Measurement memory w,) = meter.walkSegments(
            DepthLib.DepthParams({
                sqrtPriceStartX96: sp,
                tickStart: tick,
                liquidityStart: liq,
                sqrtPriceTargetX96: target,
                depthTarget: depthTarget,
                feePips: lpFee,
                tickSpacing: keys[m.poolId].tickSpacing,
                source: tickSource[m.poolId]
            }),
            segments
        );
        return (MandateLib.sideVerdict(false, w), c);
    }

    // ------------------------------------------------------------------ slash, release, donate

    function _slash(uint256 id, Mandate storage m, address prover) internal {
        m.status = MandateLib.Status.SLASHED;
        liveMandate[m.poolId] = 0;
        (uint256 bounty, uint256 donation) = MandateLib.slashSplit(m.accrued);
        uint256 refund = m.escrow;
        m.accrued = 0;
        m.escrow = 0;
        _release(prover, m.terms.payCurrency, bounty);
        _release(m.issuer, m.terms.payCurrency, refund);
        PoolKey memory key = keys[m.poolId];
        uint256 idx = m.terms.payCurrency == key.currency0 ? 0 : 1;
        pending[m.poolId][idx] += donation;
        emit Slashed(id, prover, bounty, donation, refund);
        _flush(m.poolId);
    }

    /// @notice THE EXIT SITE. The only function that moves the hook's ERC-6909 claims to anyone.
    function _release(address to, Currency c, uint256 amount) internal {
        if (amount == 0) return;
        poolManager.transfer(to, c.toId(), amount);
    }

    /// @notice Donate parked value to in-range LPs. `donate` reverts when active liquidity is zero (the Hydra
    /// defect), so that case parks instead of reverting.
    function _flush(PoolId pid) internal {
        uint256[2] storage p = pending[pid];
        if (p[0] == 0 && p[1] == 0) return;
        if (poolManager.getLiquidity(pid) == 0) {
            emit DonationParked(pid, p[0], p[1]);
            return;
        }
        poolManager.unlock(abi.encode(A_DONATE, abi.encode(pid)));
    }

    // ------------------------------------------------------------------ the unlock callback

    function unlockCallback(bytes calldata data) external onlyPoolManager returns (bytes memory) {
        (uint8 action, bytes memory args) = abi.decode(data, (uint8, bytes));
        if (action == A_DEPOSIT) {
            (address payer, Currency c, uint256 amount) = abi.decode(args, (address, Currency, uint256));
            _pull(c, payer, amount);
            poolManager.mint(address(this), c.toId(), amount);
        } else if (action == A_POSITIONS) {
            (uint256 id, address actor, bool burnExisting, Band[] memory bands, uint128 bond) =
                abi.decode(args, (uint256, address, bool, Band[], uint128));
            _positions(id, actor, burnExisting, bands, bond);
        } else if (action == A_DONATE) {
            PoolId pid = abi.decode(args, (PoolId));
            PoolKey memory key = keys[pid];
            uint256 a0 = pending[pid][0];
            uint256 a1 = pending[pid][1];
            pending[pid][0] = 0;
            pending[pid][1] = 0;
            if (a0 != 0) poolManager.burn(address(this), key.currency0.toId(), a0);
            if (a1 != 0) poolManager.burn(address(this), key.currency1.toId(), a1);
            poolManager.donate(key, a0, a1, "");
            emit Donated(pid, a0, a1);
        }
        return "";
    }

    function _positions(uint256 id, address actor, bool burnExisting, Band[] memory bands, uint128 bond) internal {
        Mandate storage m = mandates[id];
        PoolKey memory key = keys[m.poolId];
        bytes32 salt = PlacementLib.mandateSalt(id);
        int256 d0;
        int256 d1;
        if (burnExisting) {
            for (uint256 i; i < m.bandCount; i++) {
                Band storage bd = m.bands[i];
                BalanceDelta cd = _burnMandate(id, actor, bd.lower, bd.upper, bd.liquidity);
                d0 += cd.amount0();
                d1 += cd.amount1();
            }
        }
        for (uint256 i; i < bands.length; i++) {
            (BalanceDelta cd,) = PlacementLib.mintAt(poolManager, key, bands[i].lower, bands[i].upper, bands[i].liquidity, salt);
            d0 += cd.amount0();
            d1 += cd.amount1();
        }
        if (bond != 0) {
            (BalanceDelta cd, BalanceDelta fees) =
                PlacementLib.mintAt(poolManager, key, m.bondLower, m.bondUpper, bond, PlacementLib.BOND_SALT);
            // principal is the curator's to pay; fees surfaced from merged bond positions belong to LPs (D2.5)
            d0 += cd.amount0() - fees.amount0();
            d1 += cd.amount1() - fees.amount1();
            _park(m.poolId, key, fees);
        }
        _resolve(key.currency0, actor, d0);
        _resolve(key.currency1, actor, d1);
    }

    /// @notice THE NEGATIVE SITE. The only call in this codebase that removes liquidity. The gate is here, at the
    /// site, not at the entry points: actor must be the mandate's curator and the mandate must be ACTIVE.
    function _burnMandate(uint256 id, address actor, int24 lower, int24 upper, uint128 liquidity)
        internal
        returns (BalanceDelta cd)
    {
        Mandate storage m = mandates[id];
        if (actor != m.curator || m.status != MandateLib.Status.ACTIVE) revert NotCurator();
        (cd,) = poolManager.modifyLiquidity(
            keys[m.poolId],
            ModifyLiquidityParams({
                tickLower: lower,
                tickUpper: upper,
                liquidityDelta: -int256(uint256(liquidity)),
                salt: PlacementLib.mandateSalt(id)
            }),
            ""
        );
    }

    function _park(PoolId pid, PoolKey memory key, BalanceDelta fees) internal {
        int128 f0 = fees.amount0();
        int128 f1 = fees.amount1();
        if (f0 > 0) {
            poolManager.mint(address(this), key.currency0.toId(), uint256(uint128(f0)));
            pending[pid][0] += uint256(uint128(f0));
        }
        if (f1 > 0) {
            poolManager.mint(address(this), key.currency1.toId(), uint256(uint128(f1)));
            pending[pid][1] += uint256(uint128(f1));
        }
    }

    /// @dev Negative: the actor owes the pool, pulled from the actor. Positive: the pool owes the actor, taken to it.
    function _resolve(Currency c, address actor, int256 d) internal {
        if (d < 0) _pull(c, actor, uint256(-d));
        else if (d > 0) poolManager.take(c, actor, uint256(d));
    }

    function _pull(Currency c, address payer, uint256 amount) internal {
        poolManager.sync(c);
        SafeTransferLib.safeTransferFrom(ERC20(Currency.unwrap(c)), payer, address(poolManager), amount);
        poolManager.settle();
    }

    function _checkBands(int24 spacing, Band[] calldata bands) internal pure {
        if (bands.length == 0 || bands.length > MAX_BANDS) revert BadBands();
        int24 minT = TickMath.minUsableTick(spacing);
        int24 maxT = TickMath.maxUsableTick(spacing);
        for (uint256 i; i < bands.length; i++) {
            Band calldata b = bands[i];
            if (b.lower >= b.upper || b.lower < minT || b.upper > maxT) revert BadBands();
            if (b.lower % spacing != 0 || b.upper % spacing != 0 || b.liquidity == 0) revert BadBands();
        }
    }

    function _storeBands(Mandate storage m, Band[] calldata bands) internal {
        for (uint256 i; i < bands.length; i++) {
            m.bands[i] = bands[i];
        }
        m.bandCount = uint8(bands.length);
    }

    // ------------------------------------------------------------------ views

    function mandate(uint256 id)
        external
        view
        returns (
            MandateLib.Status status,
            address curator,
            uint128 escrow,
            uint128 accrued,
            uint64 paidBlocks,
            uint64 shortfallOpenedAt,
            uint64 endBlock
        )
    {
        Mandate storage m = mandates[id];
        return (m.status, m.curator, m.escrow, m.accrued, m.paidBlocks, m.shortfallOpenedAt, m.endBlock);
    }

    function terms(uint256 id) external view returns (Terms memory) {
        return mandates[id].terms;
    }

    function bandsOf(uint256 id) external view returns (Band[] memory out) {
        Mandate storage m = mandates[id];
        out = new Band[](m.bandCount);
        for (uint256 i; i < m.bandCount; i++) {
            out[i] = m.bands[i];
        }
    }

    function bondOf(uint256 id) external view returns (int24 lower, int24 upper, uint128 liquidity) {
        Mandate storage m = mandates[id];
        return (m.bondLower, m.bondUpper, m.bondLiquidity);
    }

    function pendingDonation(PoolId pid) external view returns (uint256, uint256) {
        return (pending[pid][0], pending[pid][1]);
    }

    /// @notice Measure without effects: what prove() would decide now.
    function preview(uint256 id) external view returns (DepthLib.Verdict v, uint256 cUp, uint256 cDown) {
        return _measure(mandates[id], 1);
    }
}
