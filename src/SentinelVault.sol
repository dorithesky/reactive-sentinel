// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { AbstractCallback } from "@reactive/src/base/AbstractCallback.sol";
import { IPayable } from "@reactive/src/interfaces/IPayable.sol";

import { IERC20 } from "./interfaces/IERC20.sol";
import { Ownable } from "./utils/Ownable.sol";

/// @title SentinelVault
/// @notice Destination-chain contract of the Sentinel loop, deployed on Base Sepolia (chain ID 84532).
///
/// It is a single-asset vault with one unusual ability: a Reactive Network callback can force it into a
/// defensive state without any human in the loop.
///
/// Defence ladder
/// - `Defend`   (severity 2): safe mode. New deposits stop, withdrawals stay open for everyone.
/// - `Critical` (severity 3): safe mode plus a full move of idle funds to the pre-configured safe harbour
///   address. The vault never blocks user exits — protecting users must not mean trapping them.
///
/// Authentication
/// Callbacks are only trusted when they arrive through the callback proxy (`onlyServiceProvider`) and carry the
/// address of the Sentinel reactive contract that the proxy injected as the first argument
/// (`onlyCallbackSender`). Both checks are required: the first proves the call came through the network, the
/// second proves it came from *our* reactive contract.
contract SentinelVault is AbstractCallback, Ownable {
    /// @notice Severity values must match `RiskOracle.Severity`.
    uint8 public constant SEVERITY_WATCH = 1;
    uint8 public constant SEVERITY_DEFEND = 2;
    uint8 public constant SEVERITY_CRITICAL = 3;

    IERC20 public immutable asset;

    /// @notice Cold / insurance address that receives funds on a critical defence.
    address public safeHarbor;

    /// @notice True from the first `Defend` callback onwards: deposits are closed.
    bool public safeMode;

    /// @notice True once funds have been moved to the safe harbour.
    bool public liquidated;

    uint256 public totalDeposits;
    mapping(address account => uint256 amount) public balanceOf;

    /// @notice Last defensive callback that was executed (kept flat so it is easy to read in an explorer).
    bytes32 public lastSignalId;
    uint8 public lastSeverity;
    uint256 public lastRiskScoreBps;
    uint256 public defenceCount;

    event Deposited(address indexed account, uint256 amount);
    event Withdrawn(address indexed account, uint256 amount);
    event SafeModeEntered(uint8 indexed severity, uint256 riskScoreBps, bytes32 indexed signalId);
    event FundsMovedToSafeHarbor(address indexed harbor, uint256 amount);
    event DefenseExecuted(
        bytes32 indexed signalId,
        uint8 indexed severity,
        uint256 riskScoreBps,
        bytes32 reasonCode,
        uint64 observedAt,
        uint256 executedAt,
        uint256 fundsMoved,
        uint256 onChainLatencySeconds
    );
    event SafeHarborUpdated(address indexed harbor);

    error DepositsClosedInSafeMode();
    error InsufficientBalance(uint256 requested, uint256 available);
    error SeverityTooLow(uint8 severity);
    error ZeroAmount();

    constructor(IPayable callbackProxy_, address callbackSender_, address asset_, address safeHarbor_, address owner_)
        AbstractCallback(callbackProxy_, callbackSender_)
        Ownable(owner_)
    {
        if (asset_ == address(0) || safeHarbor_ == address(0)) revert ZeroAddress();
        asset = IERC20(asset_);
        safeHarbor = safeHarbor_;
    }

    // --------------------------------------------------------------------------------------------
    // User flows
    // --------------------------------------------------------------------------------------------

    /// @notice Deposits `amount_` of the vault asset. Closed once the vault is in safe mode.
    function deposit(uint256 amount_) external {
        if (safeMode) revert DepositsClosedInSafeMode();
        if (amount_ == 0) revert ZeroAmount();

        // Credit the actual amount received so a fee-on-transfer asset cannot break the accounting.
        uint256 before = asset.balanceOf(address(this));
        require(asset.transferFrom(msg.sender, address(this), amount_), "TRANSFER_FROM_FAILED");
        uint256 received = asset.balanceOf(address(this)) - before;

        balanceOf[msg.sender] += received;
        totalDeposits += received;
        emit Deposited(msg.sender, received);
    }

    /// @notice Withdraws `amount_` of the vault asset. Never blocked, including in safe mode.
    function withdraw(uint256 amount_) external {
        if (amount_ == 0) revert ZeroAmount();
        uint256 available = balanceOf[msg.sender];
        if (amount_ > available) revert InsufficientBalance(amount_, available);

        balanceOf[msg.sender] = available - amount_;
        totalDeposits -= amount_;
        require(asset.transfer(msg.sender, amount_), "TRANSFER_FAILED");
        emit Withdrawn(msg.sender, amount_);
    }

    // --------------------------------------------------------------------------------------------
    // Reactive callback
    // --------------------------------------------------------------------------------------------

    /// @notice Executes the AI-judged defence. Called by the callback proxy, never by a user.
    /// @dev The first argument is not supplied by the reactive contract: the proxy overwrites it with the
    ///      address of the reactive contract that requested the callback.
    /// @param reactiveSender_ Address of the Sentinel reactive contract (injected by the proxy).
    /// @param signalId_        Identifier of the origin risk signal.
    /// @param severity_        Judged severity (2 Defend, 3 Critical).
    /// @param riskScoreBps_    Judged risk probability in basis points.
    /// @param observedAt_      Unix seconds of the origin observation, used for the on-chain latency figure.
    /// @param reasonCode_      Dominant feature hash, kept for auditability.
    function defend(
        address reactiveSender_,
        bytes32 signalId_,
        uint8 severity_,
        uint256 riskScoreBps_,
        uint64 observedAt_,
        bytes32 reasonCode_
    ) external onlyServiceProvider onlyCallbackSender(reactiveSender_) {
        if (severity_ < SEVERITY_DEFEND) revert SeverityTooLow(severity_);

        uint256 moved = 0;

        // Idempotent: an escalated signal after a full exit only refreshes the record, it never moves funds twice.
        if (!liquidated) {
            if (!safeMode) {
                safeMode = true;
                emit SafeModeEntered(severity_, riskScoreBps_, signalId_);
            }

            if (severity_ >= SEVERITY_CRITICAL) {
                moved = asset.balanceOf(address(this));
                liquidated = true;
                if (moved > 0) {
                    require(asset.transfer(safeHarbor, moved), "TRANSFER_FAILED");
                    emit FundsMovedToSafeHarbor(safeHarbor, moved);
                }
            }
        }

        lastSignalId = signalId_;
        lastSeverity = severity_;
        lastRiskScoreBps = riskScoreBps_;
        defenceCount += 1;

        emit DefenseExecuted(
            signalId_,
            severity_,
            riskScoreBps_,
            reasonCode_,
            observedAt_,
            block.timestamp,
            moved,
            block.timestamp > observedAt_ ? block.timestamp - observedAt_ : 0
        );
    }

    // --------------------------------------------------------------------------------------------
    // Administration
    // --------------------------------------------------------------------------------------------

    /// @notice Manually clears safe mode after the risk has passed. Deliberately owner-gated and slow:
    /// a human must confirm before deposits reopen.
    function clearSafeMode() external onlyOwner {
        safeMode = false;
    }

    function setSafeHarbor(address harbor_) external onlyOwner {
        if (harbor_ == address(0)) revert ZeroAddress();
        safeHarbor = harbor_;
        emit SafeHarborUpdated(harbor_);
    }

    /// @notice Settles outstanding callback debt so callbacks keep being delivered.
    function settle() external {
        _coverDebt();
    }

    /// @notice Assets still held by the vault (excludes anything already moved to the safe harbour).
    function totalAssets() external view returns (uint256) {
        return asset.balanceOf(address(this));
    }
}
