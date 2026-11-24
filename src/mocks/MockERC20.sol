// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { IERC20 } from "../interfaces/IERC20.sol";

/// @notice Test-only ERC-20 with open minting. Stands in for USDC on Base Sepolia.
contract MockERC20 is IERC20 {
    string public name;
    string public symbol;
    uint8 public immutable decimals;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    error InsufficientBalance();
    error InsufficientAllowance();

    constructor(string memory name_, string memory symbol_, uint8 decimals_) {
        name = name_;
        symbol = symbol_;
        decimals = decimals_;
    }

    function mint(address to_, uint256 amount_) external {
        totalSupply += amount_;
        balanceOf[to_] += amount_;
    }

    function transfer(address to_, uint256 amount_) external returns (bool) {
        _transfer(msg.sender, to_, amount_);
        return true;
    }

    function transferFrom(address from_, address to_, uint256 amount_) external returns (bool) {
        uint256 allowed = allowance[from_][msg.sender];
        if (allowed != type(uint256).max) {
            if (allowed < amount_) revert InsufficientAllowance();
            allowance[from_][msg.sender] = allowed - amount_;
        }
        _transfer(from_, to_, amount_);
        return true;
    }

    function approve(address spender_, uint256 amount_) external returns (bool) {
        allowance[msg.sender][spender_] = amount_;
        return true;
    }

    function _transfer(address from_, address to_, uint256 amount_) internal {
        uint256 fromBalance = balanceOf[from_];
        if (fromBalance < amount_) revert InsufficientBalance();
        unchecked {
            balanceOf[from_] = fromBalance - amount_;
        }
        balanceOf[to_] += amount_;
    }
}
