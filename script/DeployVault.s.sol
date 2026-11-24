// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { Script, console } from "forge-std/Script.sol";
import { IPayable } from "@reactive/src/interfaces/IPayable.sol";

import { SentinelVault } from "../src/SentinelVault.sol";

/// @title DeployVault
/// @notice Deploys the destination-side vault on Base Sepolia.
///
/// Deployment order matters: the reactive contract must exist first because the vault takes its address
/// immutably (`_CALLBACK_SENDER`), while the reactive contract takes the vault through a setter.
///
/// Usage:
///   forge script script/DeployVault.s.sol:DeployVault \
///     --rpc-url $BASE_SEPOLIA_RPC --private-key $ORIGIN_PRIVATE_KEY --broadcast
///
/// Environment:
///   REACTIVE_CONTRACT  address of SentinelReactive on Lasna
///   VAULT_ASSET        ERC-20 the vault holds on Base Sepolia
///   SAFE_HARBOR        address that receives funds on a critical defence
///   OWNER_ADDRESS      owner of the vault
///   CALLBACK_PROXY     optional; defaults to the Base Sepolia proxy
contract DeployVault is Script {
    /// @dev Base Sepolia callback proxy published by Reactive Network.
    address internal constant BASE_SEPOLIA_CALLBACK_PROXY = 0x476F44b11F5C942c58f4c8E3d0300E5710743585;

    function run() external returns (SentinelVault vault) {
        uint256 privateKey = vm.envUint("ORIGIN_PRIVATE_KEY");
        address reactive = vm.envAddress("REACTIVE_CONTRACT");
        address asset = vm.envAddress("VAULT_ASSET");
        address harbor = vm.envAddress("SAFE_HARBOR");
        address owner = vm.envAddress("OWNER_ADDRESS");
        address proxy = vm.envOr("CALLBACK_PROXY", BASE_SEPOLIA_CALLBACK_PROXY);

        vm.startBroadcast(privateKey);
        vault = new SentinelVault(IPayable(payable(proxy)), reactive, asset, harbor, owner);
        vm.stopBroadcast();

        console.log("SentinelVault      ", address(vault));
        console.log("callback proxy     ", proxy);
        console.log("authorised reactive", reactive);
        console.log("asset              ", asset);
        console.log("safe harbor        ", harbor);
    }
}
