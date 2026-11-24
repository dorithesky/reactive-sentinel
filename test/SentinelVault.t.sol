// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { Test } from "forge-std/Test.sol";

import { AbstractPayer } from "@reactive/src/base/AbstractPayer.sol";
import { AbstractCallback } from "@reactive/src/base/AbstractCallback.sol";

import { SentinelVault } from "../src/SentinelVault.sol";
import { Ownable } from "../src/utils/Ownable.sol";
import { MockCallbackProxy } from "../src/mocks/MockCallbackProxy.sol";
import { MockERC20 } from "../src/mocks/MockERC20.sol";

contract SentinelVaultTest is Test {
    SentinelVault internal vault;
    MockCallbackProxy internal proxy;
    MockERC20 internal token;

    address internal owner;
    address internal reactive;
    address internal harbor;
    address internal alice;

    bytes32 internal constant SIGNAL = keccak256("signal-1");
    bytes32 internal constant REASON = keccak256("LIQUIDITY_DRAIN");

    function setUp() public {
        owner = makeAddr("owner");
        reactive = makeAddr("reactive");
        harbor = makeAddr("harbor");
        alice = makeAddr("alice");

        proxy = new MockCallbackProxy();
        token = new MockERC20("Test USD", "tUSD", 6);
        vault = new SentinelVault(proxy, reactive, address(token), harbor, owner);

        token.mint(alice, 1_000e6);
        vm.prank(alice);
        token.approve(address(vault), type(uint256).max);
    }

    // ------------------------------------------------------------------------------------------------
    // Deposit / withdraw
    // ------------------------------------------------------------------------------------------------

    function test_DepositAndWithdraw() public {
        vm.startPrank(alice);
        vault.deposit(400e6);
        assertEq(vault.balanceOf(alice), 400e6);
        assertEq(vault.totalDeposits(), 400e6);
        assertEq(token.balanceOf(address(vault)), 400e6);

        vault.withdraw(150e6);
        assertEq(vault.balanceOf(alice), 250e6);
        assertEq(vault.totalDeposits(), 250e6);
        assertEq(token.balanceOf(alice), 750e6);
        vm.stopPrank();
    }

    function test_WithdrawMoreThanBalanceReverts() public {
        vm.startPrank(alice);
        vault.deposit(100e6);
        vm.expectRevert(abi.encodeWithSelector(SentinelVault.InsufficientBalance.selector, 101e6, 100e6));
        vault.withdraw(101e6);
        vm.stopPrank();
    }

    function test_ZeroAmountReverts() public {
        vm.expectRevert(SentinelVault.ZeroAmount.selector);
        vm.prank(alice);
        vault.deposit(0);
    }

    // ------------------------------------------------------------------------------------------------
    // Authentication
    // ------------------------------------------------------------------------------------------------

    function test_DefendRejectedWhenCallerIsNotTheProxy() public {
        vm.expectRevert(abi.encodeWithSelector(AbstractPayer.NotAuthorized.selector, alice, address(proxy)));
        vm.prank(alice);
        vault.defend(reactive, SIGNAL, 3, 9_200, uint64(block.timestamp), REASON);
    }

    function test_DefendRejectedWhenProxyInjectsAnotherReactiveAddress() public {
        address rogue = makeAddr("rogueReactive");
        vm.expectRevert(
            abi.encodeWithSelector(AbstractCallback.CallbackNotAuthorized.selector, rogue, reactive)
        );
        vm.prank(address(proxy));
        vault.defend(rogue, SIGNAL, 3, 9_200, uint64(block.timestamp), REASON);
    }

    function test_DefendRejectedWhenSeverityTooLow() public {
        vm.expectRevert(abi.encodeWithSelector(SentinelVault.SeverityTooLow.selector, uint8(1)));
        vm.prank(address(proxy));
        vault.defend(reactive, SIGNAL, 1, 4_500, uint64(block.timestamp), REASON);
    }

    // ------------------------------------------------------------------------------------------------
    // Defence ladder
    // ------------------------------------------------------------------------------------------------

    function test_DefendSeverityTwoEntersSafeModeButKeepsExitsOpen() public {
        vm.prank(alice);
        vault.deposit(500e6);

        vm.warp(1_763_000_000);
        vm.expectEmit(true, true, true, true, address(vault));
        emit SentinelVault.DefenseExecuted(SIGNAL, 2, 7_100, REASON, 1_762_999_500, 1_763_000_000, 0, 500);
        vm.prank(address(proxy));
        vault.defend(reactive, SIGNAL, 2, 7_100, 1_762_999_500, REASON);

        assertTrue(vault.safeMode());
        assertFalse(vault.liquidated());
        assertEq(vault.defenceCount(), 1);
        assertEq(vault.lastRiskScoreBps(), 7_100);

        // Deposits are closed ...
        vm.expectRevert(SentinelVault.DepositsClosedInSafeMode.selector);
        vm.prank(alice);
        vault.deposit(1e6);

        // ... but a user can still exit, which is the whole point of a stop-loss.
        vm.prank(alice);
        vault.withdraw(500e6);
        assertEq(token.balanceOf(alice), 1_000e6);
    }

    function test_DefendSeverityThreeMovesFundsToSafeHarbor() public {
        vm.prank(alice);
        vault.deposit(500e6);
        token.mint(address(vault), 20e6); // exogenous yield sitting in the vault

        vm.prank(address(proxy));
        vault.defend(reactive, SIGNAL, 3, 9_400, uint64(block.timestamp), REASON);

        assertTrue(vault.safeMode());
        assertTrue(vault.liquidated());
        assertEq(token.balanceOf(harbor), 520e6);
        assertEq(token.balanceOf(address(vault)), 0);
        assertEq(vault.totalAssets(), 0);
    }

    function test_EscalationAfterExitIsIdempotent() public {
        vm.prank(alice);
        vault.deposit(500e6);

        vm.prank(address(proxy));
        vault.defend(reactive, SIGNAL, 3, 9_400, uint64(block.timestamp), REASON);
        assertEq(token.balanceOf(harbor), 500e6);

        // A second, different signal must not move the (now empty) vault again nor double count funds.
        vm.prank(address(proxy));
        vault.defend(reactive, keccak256("signal-2"), 3, 9_900, uint64(block.timestamp), REASON);

        assertEq(token.balanceOf(harbor), 500e6);
        assertEq(vault.defenceCount(), 2);
        assertEq(vault.lastSignalId(), keccak256("signal-2"));
    }

    function test_ProxyDeliveryInjectsReactiveAddress() public {
        vm.prank(alice);
        vault.deposit(100e6);

        bytes memory payload = abi.encodeWithSignature(
            "defend(address,bytes32,uint8,uint256,uint64,bytes32)",
            address(0),
            SIGNAL,
            3,
            uint256(9_000),
            uint64(block.timestamp),
            REASON
        );

        (bool ok,) = proxy.deliver(reactive, address(vault), payload);
        assertTrue(ok);
        assertTrue(vault.liquidated());
        assertEq(token.balanceOf(harbor), 100e6);

        // The same payload delivered on behalf of another reactive contract must fail.
        (bool rogueOk,) = proxy.deliver(makeAddr("rogue"), address(vault), payload);
        assertFalse(rogueOk);
    }

    function test_OnlyOwnerClearsSafeMode() public {
        vm.prank(address(proxy));
        vault.defend(reactive, SIGNAL, 2, 7_000, uint64(block.timestamp), REASON);

        vm.expectRevert(abi.encodeWithSelector(Ownable.NotOwner.selector, alice));
        vm.prank(alice);
        vault.clearSafeMode();

        vm.prank(owner);
        vault.clearSafeMode();
        assertFalse(vault.safeMode());
    }

    function test_SettleWithZeroDebtDoesNotRevert() public {
        vault.settle();
    }
}
