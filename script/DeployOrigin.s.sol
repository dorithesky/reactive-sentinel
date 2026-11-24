// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { Script, console } from "forge-std/Script.sol";
import { IPayable } from "@reactive/src/interfaces/IPayable.sol";

import { RiskOracle } from "../src/RiskOracle.sol";

/// @title DeployOrigin
/// @notice Deploys the origin-side contract (the AI judgement log) on Base Sepolia.
///
/// Usage:
///   forge script script/DeployOrigin.s.sol:DeployOrigin \
///     --rpc-url $BASE_SEPOLIA_RPC --private-key $ORIGIN_PRIVATE_KEY --broadcast
///
/// Environment:
///   OWNER_ADDRESS  owner of the oracle (multisig or deployer)
///   AGENT_ADDRESS  address of the AI risk agent's key
contract DeployOrigin is Script {
    function run() external returns (RiskOracle oracle) {
        uint256 privateKey = vm.envUint("ORIGIN_PRIVATE_KEY");
        address owner = vm.envAddress("OWNER_ADDRESS");
        address agent = vm.envAddress("AGENT_ADDRESS");

        vm.startBroadcast(privateKey);
        oracle = new RiskOracle(owner, agent);
        vm.stopBroadcast();

        console.log("RiskOracle", address(oracle));
        console.log("owner     ", owner);
        console.log("agent     ", agent);
    }
}
