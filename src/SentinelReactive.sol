// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { AbstractReactive } from "@reactive/src/base/AbstractReactive.sol";
import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { ISystemContract } from "@reactive/src/interfaces/ISystemContract.sol";

import { RiskOracle } from "./RiskOracle.sol";
import { Ownable } from "./utils/Ownable.sol";

/// @title SentinelReactive
/// @notice Reactive contract of the Sentinel loop, deployed on Reactive Lasna (chain ID 5318007).
///
/// It subscribes to `RiskOracle.RiskSignal` logs produced on Base Sepolia and, for judgements that pass its own
/// on-chain policy, requests a callback that makes `SentinelVault` defend itself.
///
/// Why the policy lives here and not in the AI agent
/// The agent is off chain and its key can be lost or coerced. The reactive contract is the last line: it
/// re-checks the signal chain and emitter, de-duplicates by `signalId`, enforces a minimum severity, and rate
/// limits repeated defences through a *timestamp* cooldown (Base Sepolia produces 200 ms blocks, so a block-count
/// cooldown would be meaningless). Escalation to a higher severity always bypasses the cooldown: a fresh, more
/// dangerous judgement must not be delayed by the cost of the previous one.
contract SentinelReactive is AbstractReactive, Ownable {
    /// @notice Why a matched log did not lead to a callback. Emitted so the decision is auditable.
    enum IgnoreReason {
        None,
        ChainMismatch,
        CallerMismatch,
        Malformed,
        Duplicate,
        BelowMinSeverity,
        Cooldown,
        VaultNotSet,
        VaultRejected
    }

    uint256 public immutable originChainId;
    address public immutable riskOracle;
    uint256 public immutable destinationChainId;
    uint256 public immutable riskSignalTopic0;

    /// @notice Sentinel vault on the destination chain.
    address public vault;

    /// @notice Minimum severity that triggers a callback (2 = Defend).
    uint8 public minSeverity = 2;

    /// @notice Minimum seconds between two defences of the same severity.
    uint64 public cooldownSeconds = 30;

    /// @notice Gas forwarded to the destination callback.
    uint64 public callbackGasLimit = 400_000;

    uint256 public defenceRequests;
    uint256 public ignoredSignals;
    uint64 public lastDefenceAt;
    uint8 public lastSeverity;
    mapping(bytes32 signalId => bool handled) public handled;

    event DefenceRequested(
        bytes32 indexed signalId,
        uint8 indexed severity,
        uint256 riskScoreBps,
        bytes32 reasonCode,
        uint64 observedAt,
        uint256 destinationChainId,
        address vault,
        uint64 gasLimit
    );
    event SignalIgnored(bytes32 indexed signalId, uint8 indexed severity, uint256 riskScoreBps, IgnoreReason reason);
    event VaultUpdated(address indexed vault, address indexed by);
    event CooldownUpdated(uint64 seconds_);
    event MinSeverityUpdated(uint8 severity_);
    event CallbackGasLimitUpdated(uint64 gasLimit_);
    event Subscribed(uint256 chainId, address contract_, uint256 topic0);
    event Unsubscribed(uint256 chainId, address contract_, uint256 topic0);

    error InvalidConfig();

    constructor(uint256 originChainId_, address riskOracle_, uint256 destinationChainId_, address owner_)
        payable
        Ownable(owner_)
    {
        if (riskOracle_ == address(0) || originChainId_ == 0 || destinationChainId_ == 0) revert InvalidConfig();

        originChainId = originChainId_;
        riskOracle = riskOracle_;
        destinationChainId = destinationChainId_;
        riskSignalTopic0 = uint256(RiskOracle.RiskSignal.selector);

        _subscribeToRiskOracle();
    }

    // --------------------------------------------------------------------------------------------
    // Reactive entry point
    // --------------------------------------------------------------------------------------------

    /// @inheritdoc IReactive
    /// @dev Only the system contract can deliver a log record (`onlySystem`), so a log cannot be forged.
    function react(LogRecord calldata log_) external onlySystem {
        bytes32 signalId = bytes32(log_.topic1);
        uint256 riskScoreBps = log_.topic3;

        // A truncated severity would silently become a different severity level.
        if (log_.topic2 > type(uint8).max) {
            _ignore(signalId, 0, riskScoreBps, IgnoreReason.Malformed);
            return;
        }
        uint8 severity = uint8(log_.topic2);

        if (log_.chainId != originChainId) {
            _ignore(signalId, severity, riskScoreBps, IgnoreReason.ChainMismatch);
            return;
        }
        if (log_.contractAddress != riskOracle) {
            _ignore(signalId, severity, riskScoreBps, IgnoreReason.CallerMismatch);
            return;
        }
        if (handled[signalId]) {
            _ignore(signalId, severity, riskScoreBps, IgnoreReason.Duplicate);
            return;
        }
        if (severity < minSeverity) {
            // Severity 1 (Watch) is informational: it is recorded on the origin chain only.
            _ignore(signalId, severity, riskScoreBps, IgnoreReason.BelowMinSeverity);
            return;
        }
        if (vault == address(0)) {
            _ignore(signalId, severity, riskScoreBps, IgnoreReason.VaultNotSet);
            return;
        }

        // Timestamp-based cooldown; an escalation bypasses it.
        bool escalation = severity > lastSeverity;
        if (!escalation && lastDefenceAt != 0 && block.timestamp < uint256(lastDefenceAt) + cooldownSeconds) {
            _ignore(signalId, severity, riskScoreBps, IgnoreReason.Cooldown);
            return;
        }

        // `RiskSignal` carries its non-indexed payload as (observedAt, reasonCode, metrics).
        if (log_.data.length < 96) {
            _ignore(signalId, severity, riskScoreBps, IgnoreReason.Malformed);
            return;
        }
        (uint64 observedAt, bytes32 reasonCode) = abi.decode(log_.data, (uint64, bytes32));

        // The payload deliberately starts with an `address` placeholder: the callback proxy overwrites it with
        // the address of *this* contract before the vault is called.
        bytes memory payload = abi.encodeWithSignature(
            "defend(address,bytes32,uint8,uint256,uint64,bytes32)",
            address(0),
            signalId,
            severity,
            riskScoreBps,
            observedAt,
            reasonCode
        );

        handled[signalId] = true;
        defenceRequests += 1;
        lastDefenceAt = uint64(block.timestamp);
        lastSeverity = severity;

        SYSTEM.requestCallbackV_1_0(
            ISystemContract.CallbackConfiguration_V_1_0({
                chainId: destinationChainId,
                recipient: vault,
                gasLimit: callbackGasLimit,
                payload: payload
            })
        );

        emit DefenceRequested(
            signalId, severity, riskScoreBps, reasonCode, observedAt, destinationChainId, vault, callbackGasLimit
        );
    }

    function _ignore(bytes32 signalId_, uint8 severity_, uint256 riskScoreBps_, IgnoreReason reason_) internal {
        ignoredSignals += 1;
        emit SignalIgnored(signalId_, severity_, riskScoreBps_, reason_);
    }

    // --------------------------------------------------------------------------------------------
    // Subscription management
    // --------------------------------------------------------------------------------------------

    function _subscribeToRiskOracle() internal {
        SYSTEM.subscribe(originChainId, riskOracle, riskSignalTopic0, REACTIVE_IGNORE, REACTIVE_IGNORE, REACTIVE_IGNORE);
        emit Subscribed(originChainId, riskOracle, riskSignalTopic0);
    }

    /// @notice Re-subscribes (used after settling debt or restoring a dropped subscription).
    function resubscribe() external onlyOwner {
        _coverDebt();
        _subscribeToRiskOracle();
    }

    function unsubscribe() external onlyOwner {
        SYSTEM.unsubscribe(originChainId, riskOracle, riskSignalTopic0, REACTIVE_IGNORE, REACTIVE_IGNORE, REACTIVE_IGNORE);
        emit Unsubscribed(originChainId, riskOracle, riskSignalTopic0);
    }

    // --------------------------------------------------------------------------------------------
    // Administration
    // --------------------------------------------------------------------------------------------

    /// @notice Browser-order step: the vault is deployed after this contract and needs its address immutably,
    /// so the destination is wired in afterwards.
    function setVault(address vault_) external onlyOwner {
        if (vault_ == address(0)) revert InvalidConfig();
        vault = vault_;
        emit VaultUpdated(vault_, msg.sender);
    }

    function setCooldown(uint64 seconds_) external onlyOwner {
        cooldownSeconds = seconds_;
        emit CooldownUpdated(seconds_);
    }

    function setMinSeverity(uint8 severity_) external onlyOwner {
        if (severity_ == 0 || severity_ > 3) revert InvalidConfig();
        minSeverity = severity_;
        emit MinSeverityUpdated(severity_);
    }

    function setCallbackGasLimit(uint64 gasLimit_) external onlyOwner {
        callbackGasLimit = gasLimit_;
        emit CallbackGasLimitUpdated(gasLimit_);
    }

    /// @notice Tops up the lREACT balance that pays for reactive transactions.
    function fund() external payable { }
}
