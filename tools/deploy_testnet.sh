#!/usr/bin/env bash
# Deploys the Sentinel loop to the public testnets and writes deployments/testnet.json.
#
#   Base Sepolia (84532)      MockERC20 · RiskOracle · SentinelVault
#   Reactive Lasna (5318007)  SentinelReactive
#
# Reads .env.testnet (testnet-only key, chmod 600, gitignored). Every address and hash it writes is
# read back from the chain, and each finished step is stored in deployments/state.json, so the script
# can be run again and continues where it stopped (a faucet wait, a slow block, an interrupted run).
#
#   bash tools/deploy_testnet.sh
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="${HOME}/.foundry/bin:${PATH}"
set -a; . ./.env.testnet; set +a

mkdir -p deployments
STATE=deployments/state.json
[ -f "$STATE" ] || echo '{}' > "$STATE"

log() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
get() { python3 -c "import json;print(json.load(open('$STATE')).get('$1',''))"; }
put() { python3 -c "
import json,sys
d=json.load(open('$STATE')); d['$1']=sys.argv[1]
json.dump(d,open('$STATE','w'),indent=2)" "$2"; }

addr_from_run() { # $1 = script file stem, $2 = chain id
  python3 - "$1" "$2" <<'PY'
import json, sys
name, chain = sys.argv[1], sys.argv[2]
d = json.load(open(f"broadcast/{name}.s.sol/{chain}/run-latest.json"))
print([t["contractAddress"] for t in d["transactions"] if t.get("contractAddress")][-1])
PY
}

tx_from_run() { # $1 = script file stem, $2 = chain id
  python3 - "$1" "$2" <<'PY'
import json, sys
name, chain = sys.argv[1], sys.argv[2]
d = json.load(open(f"broadcast/{name}.s.sol/{chain}/run-latest.json"))
print(d["transactions"][-1]["hash"] if d["transactions"] else d["receipts"][-1]["transactionHash"])
PY
}

# ---------------------------------------------------------------------------- 1. asset
if [ -z "$(get asset)" ]; then
  log "1/8 MockERC20 (tUSD) on Base Sepolia — the token the vault holds"
  # --constructor-args is variadic: the connection flags must come before it
  forge create src/mocks/MockERC20.sol:MockERC20 \
    --rpc-url "$BASE_SEPOLIA_RPC" --private-key "$ORIGIN_PRIVATE_KEY" --broadcast \
    --constructor-args "Test USD" "tUSD" 18 > build/tmp/mockerc20.txt 2>&1
  ASSET=$(grep -oE 'Deployed to: 0x[0-9a-fA-F]{40}' build/tmp/mockerc20.txt | grep -oE '0x[0-9a-fA-F]{40}' | head -1)
  put asset "$ASSET"
else
  ASSET=$(get asset); log "1/8 asset already deployed: $ASSET"
fi
echo "tUSD = $ASSET"

# ---------------------------------------------------------------------------- 2. oracle
if [ -z "$(get risk_oracle)" ]; then
  log "2/8 RiskOracle on Base Sepolia — the AI judgement log"
  forge script script/DeployOrigin.s.sol:DeployOrigin \
    --rpc-url "$BASE_SEPOLIA_RPC" --private-key "$ORIGIN_PRIVATE_KEY" --broadcast > build/tmp/deploy_origin.txt 2>&1
  ORACLE=$(addr_from_run DeployOrigin 84532)
  put risk_oracle "$ORACLE"
else
  ORACLE=$(get risk_oracle); log "2/8 RiskOracle already deployed: $ORACLE"
fi
echo "RiskOracle = $ORACLE"

# ---------------------------------------------------------------------------- 3. lREACT
if [ "$(cast balance "$OWNER_ADDRESS" --rpc-url "$LASNA_RPC")" = "0" ]; then
  log "3/8 lREACT — 0.01 Base Sepolia ETH -> Reactive faucet 0x2afa…5967 (100:1)"
  cast send 0x2afaFD298b23b62760711756088F75B7409f5967 "request(address)" "$OWNER_ADDRESS" \
    --value 0.01ether --rpc-url "$BASE_SEPOLIA_RPC" --private-key "$ORIGIN_PRIVATE_KEY" > build/tmp/faucet.txt 2>&1
  grep -oE 'transactionHash +0x[0-9a-fA-F]{64}' build/tmp/faucet.txt | head -1 || true
  echo -n "waiting for lREACT on Lasna"
  for _ in $(seq 1 60); do
    [ "$(cast balance "$OWNER_ADDRESS" --rpc-url "$LASNA_RPC")" != "0" ] && break
    echo -n "."; sleep 5
  done
  echo " -> $(cast balance "$OWNER_ADDRESS" --rpc-url "$LASNA_RPC" | xargs -I{} cast --from-wei {} 2>/dev/null || echo '?') lREACT"
else
  log "3/8 lREACT already funded"
fi

# ---------------------------------------------------------------------------- 4. reactive
if [ -z "$(get sentinel_reactive)" ]; then
  log "4/8 SentinelReactive on Lasna — subscribes in its constructor"
  RISK_ORACLE="$ORACLE" forge script script/DeployReactive.s.sol:DeployReactive \
    --rpc-url "$LASNA_RPC" --private-key "$REACTIVE_PRIVATE_KEY" --broadcast > build/tmp/deploy_reactive.txt 2>&1
  REACTIVE=$(addr_from_run DeployReactive 5318007)
  put sentinel_reactive "$REACTIVE"
else
  REACTIVE=$(get sentinel_reactive); log "4/8 SentinelReactive already deployed: $REACTIVE"
fi
echo "SentinelReactive = $REACTIVE"

# ---------------------------------------------------------------------------- 5. gas
if [ -z "$(get reactive_funded)" ]; then
  log "5/8 funding SentinelReactive — it pays for its own reactive transactions in lREACT"
  cast send "$REACTIVE" --value 0.3ether --rpc-url "$LASNA_RPC" --private-key "$REACTIVE_PRIVATE_KEY" > build/tmp/fund.txt 2>&1
  put reactive_funded yes
  tail -1 build/tmp/fund.txt || true
else
  log "5/8 SentinelReactive already funded"
fi

# ---------------------------------------------------------------------------- 6. vault
if [ -z "$(get sentinel_vault)" ]; then
  log "6/8 SentinelVault on Base Sepolia"
  RISK_ORACLE="$ORACLE" REACTIVE_CONTRACT="$REACTIVE" VAULT_ASSET="$ASSET" \
    forge script script/DeployVault.s.sol:DeployVault \
    --rpc-url "$BASE_SEPOLIA_RPC" --private-key "$ORIGIN_PRIVATE_KEY" --broadcast > build/tmp/deploy_vault.txt 2>&1
  VAULT=$(addr_from_run DeployVault 84532)
  put sentinel_vault "$VAULT"
else
  VAULT=$(get sentinel_vault); log "6/8 SentinelVault already deployed: $VAULT"
fi
echo "SentinelVault = $VAULT"

# ---------------------------------------------------------------------------- 7. wire + fund
if [ -z "$(get wired)" ]; then
  log "7/8 setVault on Lasna, then mint + deposit 1,000 tUSD into the vault"
  cast send "$REACTIVE" "setVault(address)" "$VAULT" --rpc-url "$LASNA_RPC" --private-key "$REACTIVE_PRIVATE_KEY" > build/tmp/setvault.txt 2>&1
  cast send "$ASSET" "mint(address,uint256)" "$OWNER_ADDRESS" 1000ether --rpc-url "$BASE_SEPOLIA_RPC" --private-key "$ORIGIN_PRIVATE_KEY" > /dev/null
  cast send "$ASSET" "approve(address,uint256)" "$VAULT" 1000ether --rpc-url "$BASE_SEPOLIA_RPC" --private-key "$ORIGIN_PRIVATE_KEY" > /dev/null
  cast send "$VAULT" "deposit(uint256)" 1000ether --rpc-url "$BASE_SEPOLIA_RPC" --private-key "$ORIGIN_PRIVATE_KEY" > /dev/null
  put wired yes
else
  log "7/8 vault already wired and funded"
fi
echo "reactive.vault()    = $(cast call "$REACTIVE" 'vault()(address)' --rpc-url "$LASNA_RPC")"
echo "vault safeMode      = $(cast call "$VAULT" 'safeMode()(bool)' --rpc-url "$BASE_SEPOLIA_RPC")"
echo "vault tUSD balance  = $(cast call "$ASSET" 'balanceOf(address)(uint256)' "$VAULT" --rpc-url "$BASE_SEPOLIA_RPC")"

# ---------------------------------------------------------------------------- 8. the loop
if [ -z "$(get tx_origin)" ]; then
  log "8/8 first signal — the agent publishes, Lasna reacts, the vault defends"
  ORIGIN_FROM=$(cast block-number --rpc-url "$BASE_SEPOLIA_RPC")
  REACTIVE_FROM=$(cast block-number --rpc-url "$LASNA_RPC")
  RISK_ORACLE="$ORACLE" SEVERITY=2 SCORE_BPS=7200 \
    forge script script/PublishSmokeSignal.s.sol:PublishSmokeSignal \
    --rpc-url "$BASE_SEPOLIA_RPC" --private-key "$AGENT_PRIVATE_KEY" --broadcast > build/tmp/smoke.txt 2>&1
  TX_ORIGIN=$(tx_from_run PublishSmokeSignal 84532)
  put tx_origin "$TX_ORIGIN"
  put from_origin_block "$ORIGIN_FROM"
  put from_reactive_block "$REACTIVE_FROM"
else
  log "8/8 first signal already published"; TX_ORIGIN=$(get tx_origin)
fi
echo "origin tx = $TX_ORIGIN"

ORIGIN_FROM=$(get from_origin_block); REACTIVE_FROM=$(get from_reactive_block)
echo -n "waiting for the callback to reach the vault"
for _ in $(seq 1 60); do
  [ "$(cast call "$VAULT" 'safeMode()(bool)' --rpc-url "$BASE_SEPOLIA_RPC")" = "true" ] && break
  echo -n "."; sleep 4
done
echo " -> safeMode = $(cast call "$VAULT" 'safeMode()(bool)' --rpc-url "$BASE_SEPOLIA_RPC")"

[ -n "$(get tx_reactive)" ] || put tx_reactive "$(cast logs --address "$REACTIVE" --from-block "$REACTIVE_FROM" --rpc-url "$LASNA_RPC" \
  'DefenceRequested(bytes32,uint8,uint256,bytes32,uint64,uint256,address,uint64)' 2>/dev/null | grep -oE 'transactionHash: +0x[0-9a-fA-F]{64}' | head -1 | sed -E 's/.*: +//')"
[ -n "$(get tx_callback)" ] || put tx_callback "$(cast logs --address "$VAULT" --from-block "$ORIGIN_FROM" --rpc-url "$BASE_SEPOLIA_RPC" \
  'DefenseExecuted(bytes32,uint8,uint256,bytes32,uint64,uint256,uint256,uint256)' 2>/dev/null | grep -oE 'transactionHash: +0x[0-9a-fA-F]{64}' | head -1 | sed -E 's/.*: +//')"

python3 - "$STATE" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
o, r, v = s["risk_oracle"], s["sentinel_reactive"], s["sentinel_vault"]
d = {
    "chain_base_sepolia": 84532,
    "chain_reactive_lasna": 5318007,
    "risk_oracle": o,
    "sentinel_reactive": r,
    "sentinel_vault": v,
    "vault_asset": s["asset"],
    "tx_origin": s.get("tx_origin"),
    "tx_reactive": s.get("tx_reactive"),
    "tx_callback": s.get("tx_callback"),
    "explorer": {
        "risk_oracle": f"https://sepolia.basescan.org/address/{o}",
        "sentinel_reactive": f"https://lasna.reactscan.net/address/{r}",
        "sentinel_vault": f"https://sepolia.basescan.org/address/{v}",
        "tx_origin": f"https://sepolia.basescan.org/tx/{s.get('tx_origin')}",
        "tx_reactive": f"https://lasna.reactscan.net/tx/{s.get('tx_reactive')}",
        "tx_callback": f"https://sepolia.basescan.org/tx/{s.get('tx_callback')}",
    },
}
json.dump(d, open("deployments/testnet.json", "w"), indent=2)
print(json.dumps(d, indent=2))
PY
