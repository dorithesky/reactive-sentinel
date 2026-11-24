// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { Test } from "forge-std/Test.sol";

import { AbstractPayer } from "@reactive/src/base/AbstractPayer.sol";
import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { ISystemContract } from "@reactive/src/interfaces/ISystemContract.sol";

import { RiskOracle } from "../src/RiskOracle.sol";
import { SentinelReactive } from "../src/SentinelReactive.sol";
import { Ownable } from "../src/utils/Ownable.sol";
import { MockSystemContract } from "../src/mocks/MockSystemContract.sol";

contract SentinelReactiveTest is Test {
    /// @dev The address the reactive base contract hard-codes as `SYSTEM`.
    address internal constant SYSTEM_ADDR = 0x8888888888888888888888888888888888888888;
    uint256 internal constant REACTIVE_IGNORE =
        0xa65f96fc951c35ead38878e0f0b7a3c744a6f5ccc1476b313353ce31712313ad;

    uint256 internal constant BASE_SEPOLIA = 84532;
    uint256 internal constant LASNA = 5_318_007;

    MockSystemContract internal system;
    /// @dev Reads must go through the etched address: state written by the reactive contract lives there.
    MockSystemContract internal sys;
    SentinelReactive internal reactive;

    address internal owner;
    address internal oracle;
    address internal vault;

    bytes32 internal constant REASON = keccak256("SELL_PRESSURE");

    function setUp() public {
        owner = makeAddr("owner");
        oracle = makeAddr("riskOracle");
        vault = makeAddr("vault");

        // Put the mock system contract's runtime code at the real system address.
        system = new MockSystemContract();
        vm.etch(SYSTEM_ADDR, address(system).code);
        sys = MockSystemContract(payable(SYSTEM_ADDR));

        reactive = new SentinelReactive(BASE_SEPOLIA, oracle, BASE_SEPOLIA, owner);
        vm.prank(owner);
        reactive.setVault(vault);

        vm.warp(1_763_000_000);
    }

    // ------------------------------------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------------------------------------

    function _log(
        uint256 chainId_,
        address emitter_,
        bytes32 signalId_,
        uint8 severity_,
        uint256 scoreBps_,
        uint64 observedAt_,
        bytes32 reason_,
        bytes memory metrics_
    ) internal pure returns (IReactive.LogRecord memory) {
        return IReactive.LogRecord({
            chainId: chainId_,
            contractAddress: emitter_,
            topic0: uint256(RiskOracle.RiskSignal.selector),
            topic1: uint256(signalId_),
            topic2: uint256(uint8(severity_)),
            topic3: scoreBps_,
            data: abi.encode(observedAt_, reason_, metrics_),
            blockNumber: 42,
            opCode: 0,
            blockHash: 0,
            txHash: 0,
            logIndex: 0
        });
    }

    function _defaultLog(bytes32 signalId_, uint8 severity_, uint256 scoreBps_) internal view returns (IReactive.LogRecord memory) {
        return _log(
            BASE_SEPOLIA,
            oracle,
            signalId_,
            severity_,
            scoreBps_,
            uint64(block.timestamp - 1),
            REASON,
            abi.encode(uint256(1_200), uint256(2_000), uint256(8_100), uint256(600), uint256(250))
        );
    }

    function _deliver(IReactive.LogRecord memory record_) internal {
        vm.prank(SYSTEM_ADDR);
        reactive.react(record_);
    }

    // ------------------------------------------------------------------------------------------------
    // Subscription
    // ------------------------------------------------------------------------------------------------

    function test_ConstructorSubscribesToOracleOnOriginChain() public view {
        assertEq(sys.subscriptionCount(address(reactive)), 1);
        MockSystemContract.Subscription memory sub = sys.subscriptionAt(address(reactive), 0);
        assertEq(sub.chainId, BASE_SEPOLIA);
        assertEq(sub.contractAddress, oracle);
        assertEq(sub.topic0, uint256(RiskOracle.RiskSignal.selector));
        assertEq(sub.topic1, REACTIVE_IGNORE);
        assertEq(sub.topic2, REACTIVE_IGNORE);
        assertEq(sub.topic3, REACTIVE_IGNORE);
    }

    function test_TopicConstantMatchesOracleSelector() public {
        // A drift between the reactive contract and the origin event would silently disable the whole loop.
        assertEq(reactive.riskSignalTopic0(), uint256(RiskOracle.RiskSignal.selector));
        assertEq(reactive.riskSignalTopic0(), uint256(keccak256("RiskSignal(bytes32,uint8,uint256,uint64,bytes32,bytes)")));
    }

    function test_UnsubscribeAndResubscribe() public {
        vm.prank(owner);
        reactive.unsubscribe();
        assertEq(sys.subscriptionCount(address(reactive)), 0);

        vm.prank(owner);
        reactive.resubscribe();
        assertEq(sys.subscriptionCount(address(reactive)), 1);
    }

    // ------------------------------------------------------------------------------------------------
    // react() guards
    // ------------------------------------------------------------------------------------------------

    function test_ReactIsSystemOnly() public {
        IReactive.LogRecord memory record = _defaultLog(keccak256("s"), 3, 9_000);
        vm.expectRevert(abi.encodeWithSelector(AbstractPayer.NotAuthorized.selector, address(this), SYSTEM_ADDR));
        reactive.react(record);
    }

    function test_MismatchedChainIsIgnored() public {
        IReactive.LogRecord memory record = _log(
            LASNA, oracle, keccak256("s"), 3, 9_000, uint64(block.timestamp - 1), REASON, _metricsEmpty()
        );

        vm.expectEmit(true, true, true, true, address(reactive));
        emit SentinelReactive.SignalIgnored(keccak256("s"), 3, 9_000, SentinelReactive.IgnoreReason.ChainMismatch);
        _deliver(record);

        assertEq(sys.requestCount(), 0);
        assertEq(reactive.ignoredSignals(), 1);
    }

    function test_MismatchedEmitterIsIgnored() public {
        address fakeOracle = makeAddr("fakeOracle");
        IReactive.LogRecord memory record =
            _log(BASE_SEPOLIA, fakeOracle, keccak256("s"), 3, 9_000, uint64(block.timestamp - 1), REASON, _metricsEmpty());

        vm.expectEmit(true, true, true, true, address(reactive));
        emit SentinelReactive.SignalIgnored(keccak256("s"), 3, 9_000, SentinelReactive.IgnoreReason.CallerMismatch);
        _deliver(record);

        assertEq(sys.requestCount(), 0);
    }

    function test_TruncatedSeverityIsTreatedAsMalformed() public {
        IReactive.LogRecord memory record = _defaultLog(keccak256("s"), 2, 9_000);
        record.topic2 = 300;

        vm.expectEmit(true, true, true, true, address(reactive));
        emit SentinelReactive.SignalIgnored(keccak256("s"), 0, 9_000, SentinelReactive.IgnoreReason.Malformed);
        _deliver(record);

        assertEq(sys.requestCount(), 0);
    }

    function test_ShortPayloadIsTreatedAsMalformed() public {
        IReactive.LogRecord memory record = _defaultLog(keccak256("s"), 3, 9_000);
        record.data = hex"1234";

        vm.expectEmit(true, true, true, true, address(reactive));
        emit SentinelReactive.SignalIgnored(keccak256("s"), 3, 9_000, SentinelReactive.IgnoreReason.Malformed);
        _deliver(record);

        assertEq(sys.requestCount(), 0);
    }

    function test_WatchSeverityIsBelowThreshold() public {
        vm.expectEmit(true, true, true, true, address(reactive));
        emit SentinelReactive.SignalIgnored(
            keccak256("s"), 1, 4_500, SentinelReactive.IgnoreReason.BelowMinSeverity
        );
        _deliver(_defaultLog(keccak256("s"), 1, 4_500));

        assertEq(sys.requestCount(), 0);
    }

    // ------------------------------------------------------------------------------------------------
    // Callback request
    // ------------------------------------------------------------------------------------------------

    function test_DefendSignalRequestsCallbackOnDestinationChain() public {
        bytes32 signalId = keccak256("window-7");
        uint64 observedAt = uint64(block.timestamp - 2);

        vm.expectEmit(true, true, true, true, address(reactive));
        emit SentinelReactive.DefenceRequested(
            signalId, 2, 7_300, REASON, observedAt, BASE_SEPOLIA, vault, 400_000
        );
        _deliver(_log(BASE_SEPOLIA, oracle, signalId, 2, 7_300, observedAt, REASON, _metricsEmpty()));

        assertEq(sys.requestCount(), 1);
        MockSystemContract.CallbackRequest memory req = sys.requestAt(0);
        assertEq(req.requester, address(reactive));
        assertEq(req.chainId, BASE_SEPOLIA);
        assertEq(req.recipient, vault);
        assertEq(req.gasLimit, 400_000);
        assertEq(reactive.defenceRequests(), 1);
        assertTrue(reactive.handled(signalId));

        // Payload must start with an address placeholder; the proxy overwrites it at delivery time.
        (address placeholder, bytes32 decodedId, uint8 severity, uint256 score, uint64 decodedObserved, bytes32 reason)
        = abi.decode(_slice(req.payload, 4), (address, bytes32, uint8, uint256, uint64, bytes32));
        assertEq(placeholder, address(0));
        assertEq(decodedId, signalId);
        assertEq(severity, 2);
        assertEq(score, 7_300);
        assertEq(decodedObserved, observedAt);
        assertEq(reason, REASON);
    }

    function test_DuplicateSignalIsIgnored() public {
        bytes32 signalId = keccak256("window-7");
        _deliver(_defaultLog(signalId, 2, 7_300));
        vm.warp(block.timestamp + 100);

        vm.expectEmit(true, true, true, true, address(reactive));
        emit SentinelReactive.SignalIgnored(signalId, 2, 7_300, SentinelReactive.IgnoreReason.Duplicate);
        _deliver(_defaultLog(signalId, 2, 7_300));

        assertEq(sys.requestCount(), 1);
    }

    function test_CooldownBlocksRepeatButEscalationPasses() public {
        _deliver(_defaultLog(keccak256("w1"), 2, 7_000));
        assertEq(sys.requestCount(), 1);

        // Same severity inside the 30s cooldown window: ignored.
        vm.warp(block.timestamp + 10);
        vm.expectEmit(true, true, true, true, address(reactive));
        emit SentinelReactive.SignalIgnored(keccak256("w2"), 2, 7_100, SentinelReactive.IgnoreReason.Cooldown);
        _deliver(_defaultLog(keccak256("w2"), 2, 7_100));
        assertEq(sys.requestCount(), 1);

        // An escalation to Critical is never delayed by the cooldown.
        _deliver(_defaultLog(keccak256("w3"), 3, 9_400));
        assertEq(sys.requestCount(), 2);
        assertEq(reactive.lastSeverity(), 3);
    }

    function test_CooldownExpiresAfterConfiguredSeconds() public {
        _deliver(_defaultLog(keccak256("w1"), 2, 7_000));

        vm.prank(owner);
        reactive.setCooldown(5);
        vm.warp(block.timestamp + 5);

        _deliver(_defaultLog(keccak256("w2"), 2, 7_100));
        assertEq(sys.requestCount(), 2);
    }

    function test_NoCallbackWhenVaultNotConfigured() public {
        vm.prank(owner);
        SentinelReactive fresh = new SentinelReactive(BASE_SEPOLIA, oracle, BASE_SEPOLIA, owner);
        assertEq(fresh.vault(), address(0));

        vm.expectEmit(true, true, true, true, address(fresh));
        emit SentinelReactive.SignalIgnored(keccak256("w"), 3, 9_000, SentinelReactive.IgnoreReason.VaultNotSet);
        IReactive.LogRecord memory record = _defaultLog(keccak256("w"), 3, 9_000);
        vm.prank(SYSTEM_ADDR);
        fresh.react(record);
    }

    function test_AdminSetters() public {
        vm.startPrank(owner);
        reactive.setMinSeverity(3);
        assertEq(reactive.minSeverity(), 3);
        reactive.setCallbackGasLimit(900_000);
        assertEq(reactive.callbackGasLimit(), 900_000);
        vm.stopPrank();

        // Watch and Defend are now below the minimum, only Critical passes.
        _deliver(_defaultLog(keccak256("w1"), 1, 9_000));
        _deliver(_defaultLog(keccak256("w2"), 2, 9_000));
        assertEq(sys.requestCount(), 0);

        _deliver(_defaultLog(keccak256("w3"), 3, 9_000));
        assertEq(sys.requestCount(), 1);
        assertEq(sys.requestAt(0).gasLimit, 900_000);
    }

    function test_NonOwnerCannotAdminister() public {
        address stranger = makeAddr("stranger");
        vm.startPrank(stranger);
        vm.expectRevert(abi.encodeWithSelector(Ownable.NotOwner.selector, stranger));
        reactive.setVault(stranger);
        vm.expectRevert(abi.encodeWithSelector(Ownable.NotOwner.selector, stranger));
        reactive.setCooldown(1);
        vm.expectRevert(abi.encodeWithSelector(Ownable.NotOwner.selector, stranger));
        reactive.unsubscribe();
        vm.stopPrank();
    }

    // ------------------------------------------------------------------------------------------------
    // Utilities
    // ------------------------------------------------------------------------------------------------

    function _metricsEmpty() internal pure returns (bytes memory) {
        return abi.encode(uint256(1), uint256(2), uint256(3), uint256(4), uint256(5));
    }

    function _slice(bytes memory data_, uint256 start_) internal pure returns (bytes memory out) {
        out = new bytes(data_.length - start_);
        for (uint256 i = 0; i < out.length; i++) {
            out[i] = data_[start_ + i];
        }
    }

    receive() external payable { }
}
