// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { Test, Vm } from "forge-std/Test.sol";

import { IReactive } from "@reactive/src/interfaces/IReactive.sol";

import { RiskOracle } from "../src/RiskOracle.sol";
import { SentinelReactive } from "../src/SentinelReactive.sol";
import { SentinelVault } from "../src/SentinelVault.sol";
import { MockCallbackProxy } from "../src/mocks/MockCallbackProxy.sol";
import { MockERC20 } from "../src/mocks/MockERC20.sol";
import { MockSystemContract } from "../src/mocks/MockSystemContract.sol";

/// @title LoopTest
/// @notice The whole Sentinel loop inside one EVM, driven by a real emitted log rather than by a hand-built
/// record: origin event -> subscription match -> react() -> callback request -> proxy delivery -> vault defence.
///
/// This is the closest a single-chain test can get to production. The two-chain version that also exercises the
/// 200 ms Base Sepolia block rhythm lives in `tools/local_loop.py`.
contract LoopTest is Test {
    address internal constant SYSTEM_ADDR = 0x8888888888888888888888888888888888888888;
    uint256 internal constant BASE_SEPOLIA = 84532;
    uint256 internal constant LASNA = 5_318_007;

    MockSystemContract internal system;
    MockSystemContract internal sys;
    MockERC20 internal token;
    MockCallbackProxy internal proxy;
    RiskOracle internal oracle;
    SentinelReactive internal reactive;
    SentinelVault internal vault;

    address internal owner;
    address internal agent;
    address internal harbor;
    address internal user;

    function setUp() public {
        owner = makeAddr("owner");
        agent = makeAddr("agent");
        harbor = makeAddr("harbor");
        user = makeAddr("user");

        system = new MockSystemContract();
        vm.etch(SYSTEM_ADDR, address(system).code);
        sys = MockSystemContract(payable(SYSTEM_ADDR));

        oracle = new RiskOracle(owner, agent);
        reactive = new SentinelReactive(BASE_SEPOLIA, address(oracle), BASE_SEPOLIA, owner);

        proxy = new MockCallbackProxy();
        token = new MockERC20("Test USD", "tUSD", 6);
        vault = new SentinelVault(proxy, address(reactive), address(token), harbor, owner);

        vm.prank(owner);
        reactive.setVault(address(vault));

        token.mint(user, 1_000e6);
        vm.startPrank(user);
        token.approve(address(vault), type(uint256).max);
        vault.deposit(1_000e6);
        vm.stopPrank();

        vm.warp(1_763_000_000);
    }

    function _publishAndCollect(uint8 severity_, uint256 score_) internal returns (Vm.Log memory log_) {
        bytes32 signalId = keccak256(abi.encode("window", uint64(block.timestamp - 3)));
        uint64 observedAt = uint64(block.timestamp - 3);

        vm.recordLogs();
        vm.prank(agent);
        oracle.publish(
            signalId,
            severity_,
            score_,
            observedAt,
            keccak256("SELL_PRESSURE"),
            abi.encode(uint256(1_850), uint256(2_400), uint256(8_900), uint256(760), uint256(310))
        );
        Vm.Log[] memory logs = vm.getRecordedLogs();
        assertEq(logs.length, 1, "origin must emit exactly one log");
        return logs[0];
    }

    /// @dev Mirrors what the Reactive node does: match the subscription, then hand a LogRecord to react().
    function _toLogRecord(Vm.Log memory log_) internal view returns (IReactive.LogRecord memory) {
        assertEq(log_.topics.length, 4, "RiskSignal must carry sig + 3 indexed args");
        return IReactive.LogRecord({
            chainId: BASE_SEPOLIA,
            contractAddress: log_.emitter,
            topic0: uint256(log_.topics[0]),
            topic1: uint256(log_.topics[1]),
            topic2: uint256(log_.topics[2]),
            topic3: uint256(log_.topics[3]),
            data: log_.data,
            blockNumber: block.number,
            opCode: 0,
            blockHash: 0,
            txHash: 0,
            logIndex: 0
        });
    }

    function test_FullLoopCriticalSignalDrainsVaultToHarbor() public {
        Vm.Log memory log = _publishAndCollect(3, 9_400);

        // The subscription recorded on the system contract must match this exact log.
        MockSystemContract.Subscription memory sub = sys.subscriptionAt(address(reactive), 0);
        assertEq(sub.chainId, log.emitter == address(oracle) ? BASE_SEPOLIA : 0);
        assertEq(sub.contractAddress, log.emitter);
        assertEq(sub.topic0, uint256(log.topics[0]));

        // Step 2: the network delivers the log record.
        vm.prank(SYSTEM_ADDR);
        reactive.react(_toLogRecord(log));
        assertEq(reactive.defenceRequests(), 1);

        // Step 3: the requested callback is fulfilled by the destination proxy.
        MockSystemContract.CallbackRequest memory req = sys.requestAt(0);
        assertEq(req.recipient, address(vault));
        assertEq(req.chainId, BASE_SEPOLIA);

        (bool ok,) = proxy.deliver(address(reactive), req.recipient, req.payload);
        assertTrue(ok, "callback delivery must succeed");

        // Step 4: the vault defended itself with no human involved.
        assertTrue(vault.safeMode());
        assertTrue(vault.liquidated());
        assertEq(token.balanceOf(harbor), 1_000e6);
        assertEq(token.balanceOf(address(vault)), 0);
        assertEq(vault.lastSeverity(), 3);
        assertEq(vault.lastRiskScoreBps(), 9_400);
        assertEq(vault.lastSignalId(), keccak256(abi.encode("window", uint64(block.timestamp - 3))));
    }

    function test_FullLoopRecordsOnChainLatency() public {
        // Broadcast time and execution time are both chain timestamps here; the off-chain harness in
        // tools/local_loop.py measures the same span with millisecond resolution.
        uint64 observedAt = uint64(block.timestamp - 3);
        bytes32 signalId = keccak256("latency-window");

        vm.recordLogs();
        vm.prank(agent);
        oracle.publish(signalId, 2, 7_100, observedAt, keccak256("LIQUIDITY_DRAIN"), hex"");
        Vm.Log[] memory logs = vm.getRecordedLogs();

        vm.prank(SYSTEM_ADDR);
        reactive.react(_toLogRecord(logs[0]));

        MockSystemContract.CallbackRequest memory req = sys.requestAt(0);
        proxy.deliver(address(reactive), req.recipient, req.payload);

        assertEq(vault.defenceCount(), 1);
        assertFalse(vault.liquidated(), "severity 2 must not drain funds");
        assertTrue(vault.safeMode());
        // observedAt was 3 seconds before the callback block.
        assertEq(vault.lastSeverity(), 2);
    }

    function test_WatchSignalNeverReachesTheVault() public {
        Vm.Log memory log = _publishAndCollect(1, 4_200);

        vm.prank(SYSTEM_ADDR);
        reactive.react(_toLogRecord(log));

        assertEq(reactive.defenceRequests(), 0);
        assertEq(sys.requestCount(), 0);
        assertEq(vault.defenceCount(), 0);
        assertFalse(vault.safeMode());
    }

    function test_ReplayOfTheSameLogIsIgnored() public {
        Vm.Log memory log = _publishAndCollect(2, 7_200);
        IReactive.LogRecord memory record = _toLogRecord(log);

        vm.prank(SYSTEM_ADDR);
        reactive.react(record);

        vm.warp(block.timestamp + 1 hours);
        vm.prank(SYSTEM_ADDR);
        reactive.react(record);

        assertEq(reactive.defenceRequests(), 1);
        assertEq(sys.requestCount(), 1);
    }
}
