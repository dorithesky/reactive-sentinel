// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { Script, console } from "forge-std/Script.sol";

import { SentinelReactive } from "../src/SentinelReactive.sol";

/// @title DeployReactive
/// @notice Deploys the reactive contract on Reactive Lasna (chain ID 5318007).
///
/// The constructor subscribes to `RiskOracle.RiskSignal` on Base Sepolia, so the oracle must already be
/// deployed. Fund the contract with lREACT right after deployment: it pays for its own reactive transactions,
/// and a contract in debt stops receiving events.
///
/// Usage:
///   forge script script/DeployReactive.s.sol:DeployReactive \
///     --rpc-url $LASNA_RPC --private-key $REACTIVE_PRIVATE_KEY --broadcast
///
/// Environment:
///   RISK_ORACLE        RiskOracle address on Base Sepolia
///   OWNER_ADDRESS      owner of the reactive contract
///   ORIGIN_CHAIN_ID    optional; defaults to 84532 (Base Sepolia)
///   DEST_CHAIN_ID      optional; defaults to 84532 (Base Sepolia)
contract DeployReactive is Script {
    function run() external returns (SentinelReactive reactive) {
        uint256 privateKey = vm.envUint("REACTIVE_PRIVATE_KEY");
        address oracle = vm.envAddress("RISK_ORACLE");
        address owner = vm.envAddress("OWNER_ADDRESS");
        uint256 originChainId = vm.envOr("ORIGIN_CHAIN_ID", uint256(84532));
        uint256 destinationChainId = vm.envOr("DEST_CHAIN_ID", uint256(84532));

        vm.startBroadcast(privateKey);
        reactive = new SentinelReactive(originChainId, oracle, destinationChainId, owner);
        vm.stopBroadcast();

        console.log("SentinelReactive", address(reactive));
        console.log("origin chain    ", originChainId);
        console.log("destination     ", destinationChainId);
        console.log("risk oracle     ", oracle);
        console.log("");
        console.log("next: fund this address with lREACT, then call setVault(vault) from the owner");
    }
}
