// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { Test } from "forge-std/Test.sol";

import { RiskOracle } from "../src/RiskOracle.sol";
import { Ownable } from "../src/utils/Ownable.sol";

contract RiskOracleTest is Test {
    event RiskSignal(
        bytes32 indexed signalId,
        uint8 indexed severity,
        uint256 indexed riskScoreBps,
        uint64 observedAt,
        bytes32 reasonCode,
        bytes metrics
    );

    RiskOracle internal oracle;
    address internal owner;
    address internal agent;
    address internal stranger;

    bytes32 internal constant REASON = keccak256("SELL_PRESSURE");

    function setUp() public {
        owner = makeAddr("owner");
        agent = makeAddr("agent");
        stranger = makeAddr("stranger");
        oracle = new RiskOracle(owner, agent);
        vm.warp(1_763_000_000);
    }

    function _metrics() internal pure returns (bytes memory) {
        return abi.encode(uint256(1_850), uint256(2_400), uint256(8_900), uint256(760), uint256(310));
    }

    function test_ConstructorWiresAgentAndFloors() public view {
        assertEq(oracle.owner(), owner);
        assertEq(oracle.agent(), agent);
        assertEq(oracle.scoreFloor(1), 4_000);
        assertEq(oracle.scoreFloor(2), 6_500);
        assertEq(oracle.scoreFloor(3), 8_500);
    }

    function test_EventSelectorMatchesPublishedTopic() public view {
        // The reactive contract hard-codes this signature; a drift here would silently break the subscription.
        assertEq(
            uint256(keccak256("RiskSignal(bytes32,uint8,uint256,uint64,bytes32,bytes)")), oracle.riskSignalTopic0()
        );
    }

    function test_PublishStoresSignalAndEmitsEvent() public {
        bytes32 signalId = keccak256("window-1");

        vm.expectEmit(true, true, true, true, address(oracle));
        emit RiskSignal(signalId, 2, 7_200, uint64(1_762_999_998), REASON, _metrics());

        vm.prank(agent);
        uint256 index = oracle.publish(signalId, 2, 7_200, 1_762_999_998, REASON, _metrics());

        assertEq(index, 0);
        assertEq(oracle.signalCount(), 1);
        assertTrue(oracle.signalSeen(signalId));

        RiskOracle.Signal memory stored = oracle.latestSignal();
        assertEq(stored.signalId, signalId);
        assertEq(stored.severity, 2);
        assertEq(stored.riskScoreBps, 7_200);
        assertEq(stored.observedAt, 1_762_999_998);
        assertEq(stored.reasonCode, REASON);
        assertEq(stored.publishedBy, agent);
        assertEq(stored.publishedAtBlock, block.number);
    }

    function test_RevertWhen_NotAgent() public {
        vm.expectRevert(abi.encodeWithSelector(RiskOracle.NotAgent.selector, stranger));
        vm.prank(stranger);
        oracle.publish(keccak256("w"), 2, 7_000, uint64(block.timestamp), REASON, _metrics());
    }

    function test_RevertWhen_DuplicateSignal() public {
        bytes32 signalId = keccak256("window-1");
        vm.startPrank(agent);
        oracle.publish(signalId, 2, 7_000, uint64(block.timestamp), REASON, _metrics());
        vm.expectRevert(abi.encodeWithSelector(RiskOracle.DuplicateSignal.selector, signalId));
        oracle.publish(signalId, 2, 7_000, uint64(block.timestamp), REASON, _metrics());
        vm.stopPrank();
    }

    function test_RevertWhen_ScoreBelowSeverityFloor() public {
        vm.expectRevert(
            abi.encodeWithSelector(RiskOracle.ScoreBelowFloor.selector, uint8(2), uint256(5_000), uint256(6_500))
        );
        vm.prank(agent);
        oracle.publish(keccak256("w"), 2, 5_000, uint64(block.timestamp), REASON, _metrics());
    }

    function test_RevertWhen_SeverityOutOfRange() public {
        vm.startPrank(agent);
        vm.expectRevert(abi.encodeWithSelector(RiskOracle.InvalidSeverity.selector, uint8(0)));
        oracle.publish(keccak256("a"), 0, 9_000, uint64(block.timestamp), REASON, _metrics());

        vm.expectRevert(abi.encodeWithSelector(RiskOracle.InvalidSeverity.selector, uint8(4)));
        oracle.publish(keccak256("b"), 4, 9_000, uint64(block.timestamp), REASON, _metrics());
        vm.stopPrank();
    }

    function test_RevertWhen_ScoreOutOfRange() public {
        vm.expectRevert(abi.encodeWithSelector(RiskOracle.ScoreOutOfRange.selector, uint256(10_001)));
        vm.prank(agent);
        oracle.publish(keccak256("w"), 3, 10_001, uint64(block.timestamp), REASON, _metrics());
    }

    function test_RevertWhen_ReasonCodeMissing() public {
        vm.expectRevert(RiskOracle.ReasonCodeRequired.selector);
        vm.prank(agent);
        oracle.publish(keccak256("w"), 3, 9_000, uint64(block.timestamp), bytes32(0), _metrics());
    }

    function test_RevertWhen_ObservedAtTooFarInFuture() public {
        uint64 future = uint64(block.timestamp + 5 minutes + 1);
        vm.expectRevert(abi.encodeWithSelector(RiskOracle.TimestampOutOfRange.selector, future, block.timestamp));
        vm.prank(agent);
        oracle.publish(keccak256("w"), 3, 9_000, future, REASON, _metrics());
    }

    function test_OwnerCanChangeAgentAndFloors() public {
        address nextAgent = makeAddr("nextAgent");

        vm.prank(owner);
        oracle.setAgent(nextAgent, true);
        assertEq(oracle.agent(), nextAgent);

        vm.prank(nextAgent);
        oracle.publish(keccak256("w"), 3, 9_000, uint64(block.timestamp), REASON, _metrics());

        vm.prank(owner);
        oracle.setScoreFloor(3, 5_000);
        assertEq(oracle.scoreFloor(3), 5_000);

        // With the floor lowered, a milder score is now acceptable for the same severity.
        vm.prank(nextAgent);
        oracle.publish(keccak256("w2"), 3, 6_000, uint64(block.timestamp), REASON, _metrics());
        assertEq(oracle.signalCount(), 2);

        vm.prank(owner);
        oracle.setAgent(nextAgent, false);
        assertEq(oracle.agent(), address(0));
    }

    function test_RevertWhen_NonOwnerAdministers() public {
        vm.expectRevert(abi.encodeWithSelector(Ownable.NotOwner.selector, stranger));
        vm.prank(stranger);
        oracle.setScoreFloor(2, 1);

        vm.expectRevert(abi.encodeWithSelector(Ownable.NotOwner.selector, stranger));
        vm.prank(stranger);
        oracle.setAgent(stranger, true);
    }
}
