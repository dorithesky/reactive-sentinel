// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

/// @title Minimal ERC-20 surface used by the vault
interface IERC20 {
    function totalSupply() external view returns (uint256);
    function balanceOf(address account_) external view returns (uint256);
    function transfer(address to_, uint256 amount_) external returns (bool);
    function transferFrom(address from_, address to_, uint256 amount_) external returns (bool);
    function approve(address spender_, uint256 amount_) external returns (bool);
    function decimals() external view returns (uint8);
    function symbol() external view returns (string memory);
}
