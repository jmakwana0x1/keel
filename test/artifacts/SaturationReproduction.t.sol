// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import "forge-std/Test.sol";
import {DepthLib} from "../../src/libraries/DepthLib.sol";
import {ITickSource} from "../../src/interfaces/ITickSource.sol";
import {BookTickSource} from "../harness/BookTickSource.sol";

/// @notice Gate 1: saturation reproduced in Solidity. Replays the 2,070 Phase 0 day-start books (real mainnet v3
/// state, data/phase1_saturation_books.bin, exported by sim/export_books.py) through DepthLib and requires every one
/// of the 20,700 walks to equal Phase 0's recorded pre-fee depth and fee exactly. Writes the Solidity results to
/// data/phase1_saturation_solidity.csv, from which sim/check_phase1_saturation.py recomputes the per-class medians
/// and the naive-zero/true-positive count independently. Asserts its own claims: exact agreement everywhere, and
/// exactly 17 walks with zero liquidity at spot and positive true depth.
contract SaturationReproductionTest is Test {
    struct Books {
        uint32[] pool;
        uint160[] sqrt;
        int24[] tick;
        uint128[] liq;
        int24[] spacing;
        uint24[] fee;
        uint32[] tickStart;
        uint32[] tickCount;
        int24[] ticks;
        int128[] nets;
        uint160[] targets;
        uint256[] expDepth;
        uint256[] expFee;
    }

    function _load() internal view returns (Books memory b) {
        bytes memory raw = vm.readFileBinary("data/phase1_saturation_books.bin");
        (b.pool, b.sqrt, b.tick, b.liq, b.spacing, b.fee, b.tickStart, b.tickCount, b.ticks, b.nets, b.targets, b.expDepth,
            b.expFee) = abi.decode(
            raw,
            (uint32[], uint160[], int24[], uint128[], int24[], uint24[], uint32[], uint32[], int24[], int128[], uint160[], uint256[], uint256[])
        );
    }

    function test_reproduceSaturationFromRealBooks() public {
        Books memory b = _load();
        BookTickSource src = new BookTickSource();
        string memory out = "data/phase1_saturation_solidity.csv";
        vm.writeFile(out, "book,pool,walk,depth,fee,spot_liquidity_zero\n");
        uint256 walks;
        uint256 naiveZeroTruePositive;
        for (uint256 i; i < b.pool.length; i++) {
            int24[] memory t = new int24[](b.tickCount[i]);
            int128[] memory n = new int128[](b.tickCount[i]);
            for (uint256 j; j < t.length; j++) {
                t[j] = b.ticks[b.tickStart[i] + j];
                n[j] = b.nets[b.tickStart[i] + j];
            }
            src.load(b.spacing[i], t, n);
            for (uint256 k; k < 10; k++) {
                DepthLib.Measurement memory m = DepthLib.walk(
                    DepthLib.DepthParams({
                        sqrtPriceStartX96: b.sqrt[i],
                        tickStart: b.tick[i],
                        liquidityStart: b.liq[i],
                        sqrtPriceTargetX96: b.targets[i * 10 + k],
                        depthTarget: 0,
                        feePips: b.fee[i],
                        tickSpacing: b.spacing[i],
                        source: ITickSource(address(src))
                    })
                );
                assertFalse(m.capped || m.anchorMismatch || m.exhausted, "real-book walk did not complete");
                assertEq(m.depth, b.expDepth[i * 10 + k], "Solidity depth != Phase 0");
                assertEq(m.fee, b.expFee[i * 10 + k], "Solidity fee != Phase 0");
                walks++;
                if (b.liq[i] == 0 && m.depth > 0) naiveZeroTruePositive++;
                vm.writeLine(
                    out,
                    string.concat(vm.toString(i), ",", vm.toString(uint256(b.pool[i])), ",", vm.toString(k), ",",
                        vm.toString(m.depth), ",", vm.toString(m.fee), ",", b.liq[i] == 0 ? "1" : "0")
                );
            }
        }
        assertEq(walks, 20_700, "not every Phase 0 walk was replayed");
        assertEq(naiveZeroTruePositive, 17, "naive-zero / true-positive count differs from Phase 0");
    }
}
