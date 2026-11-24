// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { Ownable } from "./utils/Ownable.sol";

/// @title RiskOracle
/// @notice Origin-chain contract of the Sentinel loop, deployed on Base Sepolia (chain ID 84532).
///
/// The off-chain AI risk agent analyses pool / price features and, when it judges that a position is in
/// danger, writes its judgement here as a `RiskSignal` log. The reactive contract on Reactive Lasna
/// subscribes to exactly this event and turns it into a cross-chain callback.
///
/// Design notes
/// - The oracle stores *judgements*, never the defensive action itself. Keeping the origin contract free of
///   destination-side logic is what makes the loop auditable: one log entry per AI decision, one callback per
///   accepted log entry.
/// - Severity is capped against a per-severity score floor so an operator mistake (or a compromised agent key)
///   cannot publish `Critical` for a mild score. The floor is owner-configurable.
/// - `signalId` is unique. It is derived by the agent from the observation window, which makes publishing
///   idempotent per window and gives the reactive contract a cheap replay guard.
contract RiskOracle is Ownable {
    /// @notice Severity ladder shared by every component of the loop.
    /// @dev 0 is invalid on purpose: `None` means "no event", so it must never be published.
    enum Severity {
        None,
        Watch,
        Defend,
        Critical
    }

    /// @notice An AI risk judgement as it was recorded on chain.
    struct Signal {
        bytes32 signalId;
        uint8 severity;
        uint256 riskScoreBps;
        uint64 observedAt;
        bytes32 reasonCode;
        address publishedBy;
        uint256 publishedAtBlock;
    }

    /// @notice Emitted once per AI judgement. Topic layout is part of the public interface:
    /// topic0 = signature, topic1 = signalId, topic2 = severity, topic3 = riskScoreBps.
    /// @param signalId      Unique identifier of the observation window.
    /// @param severity      Judged severity (1 Watch, 2 Defend, 3 Critical).
    /// @param riskScoreBps  Calibrated risk probability in basis points (0..10000).
    /// @param observedAt    Unix seconds of the market observation the judgement is based on.
    /// @param reasonCode    keccak256 of the dominant contributing feature, for on-chain explainability.
    /// @param metrics       abi.encode(uint256 priceDropBps, uint256 liquidityDropBps, uint256 sellPressureBps,
    ///                      uint256 volatilityBps, uint256 twapDeviationBps)
    event RiskSignal(
        bytes32 indexed signalId,
        uint8 indexed severity,
        uint256 indexed riskScoreBps,
        uint64 observedAt,
        bytes32 reasonCode,
        bytes metrics
    );

    event AgentUpdated(address indexed agent, bool allowed);
    event ScoreFloorUpdated(uint8 indexed severity, uint256 minScoreBps);

    error NotAgent(address caller_);
    error DuplicateSignal(bytes32 signalId);
    error InvalidSeverity(uint8 severity);
    error ScoreOutOfRange(uint256 scoreBps);
    error ScoreBelowFloor(uint8 severity, uint256 scoreBps, uint256 floorBps);
    error ReasonCodeRequired();
    error TimestampOutOfRange(uint64 observedAt, uint256 now_);

    /// @dev Tolerated clock skew between the agent host and the chain when stamping `observedAt`.
    uint256 public constant MAX_CLOCK_SKEW = 5 minutes;

    address public agent;

    /// @notice Per-severity minimum score, in basis points.
    mapping(uint8 severity => uint256 minScoreBps) public scoreFloor;

    mapping(bytes32 signalId => bool seen) public signalSeen;

    Signal[] internal _signals;

    constructor(address owner_, address agent_) Ownable(owner_) {
        scoreFloor[uint8(Severity.Watch)] = 4_000;
        scoreFloor[uint8(Severity.Defend)] = 6_500;
        scoreFloor[uint8(Severity.Critical)] = 8_500;
        _setAgent(agent_, true);
    }

    modifier onlyAgent() {
        if (msg.sender != agent) revert NotAgent(msg.sender);
        _;
    }

    // --------------------------------------------------------------------------------------------
    // Administration
    // --------------------------------------------------------------------------------------------

    /// @notice Authorises or revokes an AI agent key. Only one agent is active at a time.
    function setAgent(address agent_, bool allowed_) external onlyOwner {
        _setAgent(agent_, allowed_);
    }

    /// @notice Sets the score floor that a given severity requires.
    function setScoreFloor(uint8 severity_, uint256 minScoreBps_) external onlyOwner {
        if (severity_ == 0 || severity_ > uint8(Severity.Critical)) revert InvalidSeverity(severity_);
        if (minScoreBps_ > 10_000) revert ScoreOutOfRange(minScoreBps_);
        scoreFloor[severity_] = minScoreBps_;
        emit ScoreFloorUpdated(severity_, minScoreBps_);
    }

    function _setAgent(address agent_, bool allowed_) internal {
        agent = allowed_ ? agent_ : address(0);
        emit AgentUpdated(agent_, allowed_);
    }

    // --------------------------------------------------------------------------------------------
    // Publishing
    // --------------------------------------------------------------------------------------------

    /// @notice Publishes one AI risk judgement.
    /// @dev Only the registered agent may publish. Re-publishing the same `signalId` reverts, so a retried
    ///      transaction (same nonce) is safe while a genuinely duplicated judgement is rejected.
    /// @return index_ Position of the signal in the history array.
    function publish(
        bytes32 signalId_,
        uint8 severity_,
        uint256 riskScoreBps_,
        uint64 observedAt_,
        bytes32 reasonCode_,
        bytes calldata metrics_
    ) external onlyAgent returns (uint256 index_) {
        if (severity_ == 0 || severity_ > uint8(Severity.Critical)) revert InvalidSeverity(severity_);
        if (riskScoreBps_ > 10_000) revert ScoreOutOfRange(riskScoreBps_);
        if (riskScoreBps_ < scoreFloor[severity_]) {
            revert ScoreBelowFloor(severity_, riskScoreBps_, scoreFloor[severity_]);
        }
        if (reasonCode_ == bytes32(0)) revert ReasonCodeRequired();
        if (signalSeen[signalId_]) revert DuplicateSignal(signalId_);
        if (observedAt_ > block.timestamp + MAX_CLOCK_SKEW) {
            revert TimestampOutOfRange(observedAt_, block.timestamp);
        }

        signalSeen[signalId_] = true;

        _signals.push(
            Signal({
                signalId: signalId_,
                severity: severity_,
                riskScoreBps: riskScoreBps_,
                observedAt: observedAt_,
                reasonCode: reasonCode_,
                publishedBy: msg.sender,
                publishedAtBlock: block.number
            })
        );
        index_ = _signals.length - 1;

        emit RiskSignal(signalId_, severity_, riskScoreBps_, observedAt_, reasonCode_, metrics_);
    }

    // --------------------------------------------------------------------------------------------
    // Views
    // --------------------------------------------------------------------------------------------

    function signalCount() external view returns (uint256) {
        return _signals.length;
    }

    function signalAt(uint256 index_) external view returns (Signal memory) {
        return _signals[index_];
    }

    function latestSignal() external view returns (Signal memory) {
        return _signals[_signals.length - 1];
    }

    /// @notice Topic value the reactive contract must subscribe to (topic0 of `RiskSignal`).
    function riskSignalTopic0() external pure returns (uint256) {
        return uint256(RiskSignal.selector);
    }
}
