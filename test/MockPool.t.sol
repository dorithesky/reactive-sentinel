// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { Test } from "forge-std/Test.sol";

import { MockPool } from "../src/MockPool.sol";
import { MockERC20 } from "../src/mocks/MockERC20.sol";

/// @notice Sanity checks for the origin pool the AI agent observes.
contract MockPoolTest is Test {
    MockPool internal pool;
    MockERC20 internal base;
    MockERC20 internal quote;

    address internal lp;
    address internal trader;

    function setUp() public {
        base = new MockERC20("Test USD", "tUSD", 18);
        quote = new MockERC20("Mock USD", "mUSD", 18);
        pool = new MockPool(address(base), address(quote));

        lp = makeAddr("lp");
        trader = makeAddr("trader");

        base.mint(lp, 1_000_000e18);
        quote.mint(lp, 3_000_000e18);
        vm.startPrank(lp);
        base.approve(address(pool), type(uint256).max);
        quote.approve(address(pool), type(uint256).max);
        pool.addLiquidity(1_000_000e18, 3_000_000e18);
        vm.stopPrank();

        base.mint(trader, 10_000_000e18);
        vm.prank(trader);
        base.approve(address(pool), type(uint256).max);
    }

    function test_SpotPriceReflectsReserveRatio() public view {
        assertEq(pool.spotPrice(), 3e18);
    }

    function test_SellingBasePushesPriceDown() public {
        vm.startPrank(trader);
        pool.swap(true, 400_000e18);
        pool.swap(true, 400_000e18);
        vm.stopPrank();

        assertLt(pool.spotPrice(), 2e18, "two large sells should move the price by more than 30%");
    }

    function test_ReserveSnapshotIsVisibleToOffChainReaders() public {
        (uint112 baseReserve, uint112 quoteReserve) = pool.reserves();
        assertEq(uint256(baseReserve), 1_000_000e18);
        assertEq(uint256(quoteReserve), 3_000_000e18);
    }

    function test_RemoveLiquidityDrainsDepth() public {
        vm.prank(trader);
        pool.removeLiquidity(600_000e18, 0);
        (uint112 baseReserve,) = pool.reserves();
        assertEq(uint256(baseReserve), 400_000e18);
    }
}
