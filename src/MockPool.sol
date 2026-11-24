// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { IERC20 } from "./interfaces/IERC20.sol";

/// @title MockPool
/// @notice A minimal constant-product pool on the origin chain. It exists so the AI risk agent has a real
/// on-chain source of price, liquidity and flow instead of a local CSV: the agent polls `reserves()` and derives
/// its features from the same numbers any DEX would expose.
///
/// It is deliberately simple (no fees, no LP tokens) because the target of the experiment is the reactive loop,
/// not the AMM.
contract MockPool {
    IERC20 public immutable base; // the asset the vault holds (tUSD)
    IERC20 public immutable quote; // the unit of account (mUSD)

    uint112 public baseReserve;
    uint112 public quoteReserve;

    event Swap(address indexed trader, bool baseIn, uint256 amountIn, uint256 amountOut, uint112 baseReserve, uint112 quoteReserve);
    event LiquidityAdded(uint256 baseAmount, uint256 quoteAmount);
    event LiquidityRemoved(uint256 baseAmount, uint256 quoteAmount);

    error InsufficientLiquidity();
    error ZeroAmount();

    constructor(address base_, address quote_) {
        base = IERC20(base_);
        quote = IERC20(quote_);
    }

    /// @notice Seeds the pool. Amounts are pulled from the caller.
    function addLiquidity(uint256 baseAmount_, uint256 quoteAmount_) external {
        if (baseAmount_ == 0 || quoteAmount_ == 0) revert ZeroAmount();
        baseReserve += uint112(baseAmount_);
        quoteReserve += uint112(quoteAmount_);
        require(base.transferFrom(msg.sender, address(this), baseAmount_), "BASE_TRANSFER_FAILED");
        require(quote.transferFrom(msg.sender, address(this), quoteAmount_), "QUOTE_TRANSFER_FAILED");
        emit LiquidityAdded(baseAmount_, quoteAmount_);
    }

    /// @notice Pulls liquidity out. Anyone can call it with the pool's own reserves in this mock.
    function removeLiquidity(uint256 baseAmount_, uint256 quoteAmount_) external {
        if (baseAmount_ > baseReserve || quoteAmount_ > quoteReserve) revert InsufficientLiquidity();
        baseReserve -= uint112(baseAmount_);
        quoteReserve -= uint112(quoteAmount_);
        require(base.transfer(msg.sender, baseAmount_), "BASE_TRANSFER_FAILED");
        require(quote.transfer(msg.sender, quoteAmount_), "QUOTE_TRANSFER_FAILED");
        emit LiquidityRemoved(baseAmount_, quoteAmount_);
    }

    /// @notice Sells `amountIn_` of `baseIn` for the other side, constant product, no fee.
    /// @return amountOut_ Amount of the output asset sent to the caller.
    function swap(bool baseIn_, uint256 amountIn_) external returns (uint256 amountOut_) {
        if (amountIn_ == 0) revert ZeroAmount();

        // Reserves are updated before any token is moved (checks-effects-interactions), so a reentrant
        // token cannot trade against stale reserves.
        if (baseIn_) {
            uint256 newBase = uint256(baseReserve) + amountIn_;
            amountOut_ = uint256(quoteReserve) - ((uint256(baseReserve) * uint256(quoteReserve)) / newBase);
            if (amountOut_ == 0 || amountOut_ >= quoteReserve) revert InsufficientLiquidity();

            baseReserve = uint112(newBase);
            quoteReserve = uint112(uint256(quoteReserve) - amountOut_);

            require(base.transferFrom(msg.sender, address(this), amountIn_), "BASE_TRANSFER_FAILED");
            require(quote.transfer(msg.sender, amountOut_), "QUOTE_TRANSFER_FAILED");
        } else {
            uint256 newQuote = uint256(quoteReserve) + amountIn_;
            amountOut_ = uint256(baseReserve) - ((uint256(baseReserve) * uint256(quoteReserve)) / newQuote);
            if (amountOut_ == 0 || amountOut_ >= baseReserve) revert InsufficientLiquidity();

            quoteReserve = uint112(newQuote);
            baseReserve = uint112(uint256(baseReserve) - amountOut_);

            require(quote.transferFrom(msg.sender, address(this), amountIn_), "QUOTE_TRANSFER_FAILED");
            require(base.transfer(msg.sender, amountOut_), "BASE_TRANSFER_FAILED");
        }

        emit Swap(msg.sender, baseIn_, amountIn_, amountOut_, baseReserve, quoteReserve);
    }

    /// @notice Reserve snapshot, the single input of the agent's feature extractor.
    function reserves() external view returns (uint112 baseReserve_, uint112 quoteReserve_) {
        return (baseReserve, quoteReserve);
    }

    /// @notice Spot price of one base unit expressed in quote units, with 1e18 fixed point.
    function spotPrice() external view returns (uint256) {
        if (baseReserve == 0) return 0;
        return (uint256(quoteReserve) * 1e18) / uint256(baseReserve);
    }
}
