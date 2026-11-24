// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { IPayable } from "@reactive/src/interfaces/IPayable.sol";

/// @title MockCallbackProxy
/// @notice Offline stand-in for the Reactive Network callback proxy of a destination chain
/// (Base Sepolia: `0x476F44b11F5C942c58f4c8E3d0300E5710743585`).
///
/// It reproduces the one behaviour the destination contract depends on: the first argument of the payload is
/// overwritten with the address of the reactive contract that requested the callback, so the destination can
/// authenticate the call. Everything else (billing, gas withholding) is out of scope for a local run.
contract MockCallbackProxy is IPayable {
    event CallbackDelivered(address indexed reactive, address indexed target, bool success, bytes returnData);

    error NotAContract(address target_);
    error MalformedPayload(uint256 length);

    receive() external payable override { }

    /// @notice Delivers `payload_` to `target_` with `reactiveSender_` injected as the first argument.
    /// @dev Payload layout is `selector(4) || arg0(32) || rest`. The proxy replaces arg0 with the sender address;
    /// in a correctly built payload arg0 is a zero-filled `address` slot, so writing the whole word is
    /// equivalent to overwriting its low 160 bits.
    function deliver(address reactiveSender_, address target_, bytes calldata payload_)
        external
        returns (bool ok, bytes memory returnData)
    {
        if (target_.code.length == 0) revert NotAContract(target_);
        if (payload_.length < 36) revert MalformedPayload(payload_.length);

        bytes memory patched =
            abi.encodePacked(payload_[0:4], bytes32(uint256(uint160(reactiveSender_))), payload_[36:]);

        (ok, returnData) = target_.call(patched);
        emit CallbackDelivered(reactiveSender_, target_, ok, returnData);
    }

    function debt(address) external pure returns (uint256) {
        return 0;
    }
}
