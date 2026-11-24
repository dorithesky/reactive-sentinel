// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { ISystemContract } from "@reactive/src/interfaces/ISystemContract.sol";

/// @title MockSystemContract
/// @notice Offline stand-in for the Reactive Network system contract at
/// `0x8888888888888888888888888888888888888888`.
///
/// The real system contract is a closed-source node component, so tests and the local two-chain harness etch
/// this contract's runtime code at the system address. It records every subscription and every callback request
/// so a test (or the simulated reactive node) can read exactly what the reactive contract asked the network to do.
///
/// It is intentionally "dumb": it never delivers callbacks itself. Delivery is the job of the callback proxy
/// (`MockCallbackProxy`), mirroring how Reactive Signer posts the destination transaction in production.
contract MockSystemContract is ISystemContract {
    struct Subscription {
        uint256 chainId;
        address contractAddress;
        uint256 topic0;
        uint256 topic1;
        uint256 topic2;
        uint256 topic3;
    }

    struct CallbackRequest {
        address requester;
        uint256 chainId;
        address recipient;
        uint64 gasLimit;
        bytes payload;
        uint256 requestedAtBlock;
    }

    error SubscriptionNotFound();

    event Subscribed(
        address indexed reactive,
        uint256 chainId,
        address contractAddress,
        uint256 topic0,
        uint256 topic1,
        uint256 topic2,
        uint256 topic3
    );
    event Unsubscribed(
        address indexed reactive,
        uint256 chainId,
        address contractAddress,
        uint256 topic0,
        uint256 topic1,
        uint256 topic2,
        uint256 topic3
    );
    event CallbackRequested(
        address indexed requester, uint256 indexed chainId, address indexed recipient, uint64 gasLimit, bytes payload
    );

    mapping(address reactive => Subscription[]) internal _subscriptions;
    CallbackRequest[] public requests;

    receive() external payable override { }

    // --------------------------------------------------------------------------------------------
    // Subscriptions
    // --------------------------------------------------------------------------------------------

    function subscribe(
        uint256 chainId_,
        address contract_,
        uint256 topic0_,
        uint256 topic1_,
        uint256 topic2_,
        uint256 topic3_
    ) external {
        _subscriptions[msg.sender].push(
            Subscription({
                chainId: chainId_,
                contractAddress: contract_,
                topic0: topic0_,
                topic1: topic1_,
                topic2: topic2_,
                topic3: topic3_
            })
        );
        emit Subscribed(msg.sender, chainId_, contract_, topic0_, topic1_, topic2_, topic3_);
    }

    function unsubscribe(
        uint256 chainId_,
        address contract_,
        uint256 topic0_,
        uint256 topic1_,
        uint256 topic2_,
        uint256 topic3_
    ) external {
        Subscription[] storage subs = _subscriptions[msg.sender];
        for (uint256 i = 0; i < subs.length; i++) {
            Subscription storage s = subs[i];
            if (
                s.chainId == chainId_ && s.contractAddress == contract_ && s.topic0 == topic0_ && s.topic1 == topic1_
                    && s.topic2 == topic2_ && s.topic3 == topic3_
            ) {
                subs[i] = subs[subs.length - 1];
                subs.pop();
                emit Unsubscribed(msg.sender, chainId_, contract_, topic0_, topic1_, topic2_, topic3_);
                return;
            }
        }
        revert SubscriptionNotFound();
    }

    function subscriptionCount(address reactive_) external view returns (uint256) {
        return _subscriptions[reactive_].length;
    }

    function subscriptionAt(address reactive_, uint256 index_) external view returns (Subscription memory) {
        return _subscriptions[reactive_][index_];
    }

    // --------------------------------------------------------------------------------------------
    // Callback requests
    // --------------------------------------------------------------------------------------------

    function requestCallback(CallbackVersion version_, bytes memory config_) external {
        require(version_ == CallbackVersion.V_1_0, "UNSUPPORTED_VERSION");
        CallbackConfiguration_V_1_0 memory cfg = abi.decode(config_, (CallbackConfiguration_V_1_0));
        _record(cfg);
    }

    function requestCallbackV_1_0(CallbackConfiguration_V_1_0 memory config_) public {
        _record(config_);
    }

    function _record(CallbackConfiguration_V_1_0 memory cfg) internal {
        requests.push(
            CallbackRequest({
                requester: msg.sender,
                chainId: cfg.chainId,
                recipient: cfg.recipient,
                gasLimit: cfg.gasLimit,
                payload: cfg.payload,
                requestedAtBlock: block.number
            })
        );
        emit CallbackRequested(msg.sender, cfg.chainId, cfg.recipient, cfg.gasLimit, cfg.payload);
    }

    function requestCount() external view returns (uint256) {
        return requests.length;
    }

    function requestAt(uint256 index_) external view returns (CallbackRequest memory) {
        return requests[index_];
    }

    // --------------------------------------------------------------------------------------------
    // Billing surface (no charges locally)
    // --------------------------------------------------------------------------------------------

    function debt(address) external pure returns (uint256) {
        return 0;
    }
}
