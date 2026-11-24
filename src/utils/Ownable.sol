// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

/// @title Minimal ownership helper
/// @notice Keeps the dependency surface small so the contracts can be reviewed in one sitting.
abstract contract Ownable {
    error NotOwner(address caller_);
    error ZeroAddress();

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    address public owner;

    constructor(address owner_) {
        if (owner_ == address(0)) revert ZeroAddress();
        owner = owner_;
        emit OwnershipTransferred(address(0), owner_);
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner(msg.sender);
        _;
    }

    function transferOwnership(address newOwner_) external onlyOwner {
        if (newOwner_ == address(0)) revert ZeroAddress();
        emit OwnershipTransferred(owner, newOwner_);
        owner = newOwner_;
    }
}
