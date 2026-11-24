// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { Script, console } from "forge-std/Script.sol";

import { RiskOracle } from "../src/RiskOracle.sol";

/// @title PublishSmokeSignal
/// @notice Publishes one hand-made risk judgement on Base Sepolia. Useful to produce the origin transaction hash
/// of the demo loop without waiting for the agent to see a real crash, and to check the subscription end to end.
///
/// Usage:
///   forge script script/PublishSmokeSignal.s.sol:PublishSmokeSignal \
///     --rpc-url $BASE_SEPOLIA_RPC --private-key $AGENT_PRIVATE_KEY --broadcast
///
/// Environment:
///   RISK_ORACLE   RiskOracle address on Base Sepolia
///   SEVERITY      optional; 2 (Defend) or 3 (Critical). Defaults to 2
///   SCORE_BPS     optional; defaults to 7200
contract PublishSmokeSignal is Script {
    function run() external {
        uint256 privateKey = vm.envUint("AGENT_PRIVATE_KEY");
        RiskOracle oracle = RiskOracle(vm.envAddress("RISK_ORACLE"));
        uint8 severity = uint8(vm.envOr("SEVERITY", uint256(2)));
        uint256 scoreBps = vm.envOr("SCORE_BPS", uint256(7_200));

        // A signal id that is unique per run, so re-running this script does not revert as a duplicate.
        bytes32 signalId = keccak256(abi.encode("sentinel-smoke", block.timestamp, scoreBps));
        bytes memory metrics = abi.encode(uint256(1_850), uint256(2_400), uint256(8_900), uint256(760), uint256(310));

        vm.startBroadcast(privateKey);
        oracle.publish(signalId, severity, scoreBps, uint64(block.timestamp), keccak256("SMOKE_TEST"), metrics);
        vm.stopBroadcast();

        console.log("signalId", vm.toString(signalId));
        console.log("severity", severity);
        console.log("scoreBps", scoreBps);
    }
}
