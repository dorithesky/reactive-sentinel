# Testnet deployment runbook

Everything below is what you run once, in order. The order is not ceremonial: the vault stores the reactive
contract's address **immutably**, and the reactive contract takes the vault through a setter, so the reactive
contract must exist first.

Networks:

| Role | Network | Chain ID | RPC |
|---|---|---|---|
| Origin + destination | Base Sepolia | 84532 | `https://sepolia.base.org` (or any Base Sepolia RPC) |
| Reactive | Reactive Lasna | 5318007 | `https://lasna-rpc.rnk.dev/` |

## 0. What you need

| Item | How to get it |
|---|---|
| Base Sepolia ETH (deployer, agent, vault) | any Base Sepolia faucet (Alchemy / QuickNode / Coinbase) |
| lREACT on Lasna | send Base Sepolia ETH to the faucet contract `0x2afaFD298b23b62760711756088F75B7409f5967` with `request(address)`, **1 ETH → 100 lREACT, max 5 ETH per transaction**. Amounts above 5 ETH are lost. |
| An ERC-20 to hold in the vault | any token you control on Base Sepolia, or deploy `MockERC20` (`forge create src/mocks/MockERC20.sol:MockERC20` with `("Test USD","tUSD",18)`) and mint yourself some. |

Reference rates for planning: a reactive transaction costs gas on Lasna paid in lREACT, and a callback costs gas
on Base Sepolia paid **by the vault** (the recipient pays, not the requester). Fund the vault with ~0.01–0.05 ETH
so it never goes into debt, which would silently stop callbacks.

## 1. Environment

```bash
export BASE_SEPOLIA_RPC="https://sepolia.base.org"
export LASNA_RPC="https://lasna-rpc.rnk.dev/"

export ORIGIN_PRIVATE_KEY=0x...        # deploys on Base Sepolia, owns the oracle and the vault
export REACTIVE_PRIVATE_KEY=0x...      # deploys on Lasna, owns the reactive contract
export AGENT_ADDRESS=0x...             # the AI agent's key (can be a fresh EOA)
export OWNER_ADDRESS=0x...             # usually the deployer

export RISK_ORACLE=0x...               # filled in after step 2
export REACTIVE_CONTRACT=0x...         # filled in after step 3
export VAULT_ASSET=0x...               # the ERC-20 the vault holds
export SAFE_HARBOR=0x...               # where funds go on a critical defence
```

## 2. Origin: the AI judgement log (Base Sepolia)

```bash
forge script script/DeployOrigin.s.sol:DeployOrigin \
  --rpc-url $BASE_SEPOLIA_RPC --private-key $ORIGIN_PRIVATE_KEY --broadcast
```

Copy the printed `RiskOracle` address into `RISK_ORACLE`.

## 3. Reactive: the policy (Lasna)

```bash
forge script script/DeployReactive.s.sol:DeployReactive \
  --rpc-url $LASNA_RPC --private-key $REACTIVE_PRIVATE_KEY --broadcast
```

The constructor calls `SYSTEM.subscribe(...)` on `0x8888888888888888888888888888888888888888`, so the
subscription is live the moment it lands. Copy the address into `REACTIVE_CONTRACT`.

**Fund it immediately** — a contract with no lREACT receives nothing:

```bash
cast send $REACTIVE_CONTRACT --value 1ether \
  --rpc-url $LASNA_RPC --private-key $REACTIVE_PRIVATE_KEY
```

## 4. Destination: the vault (Base Sepolia)

```bash
forge script script/DeployVault.s.sol:DeployVault \
  --rpc-url $BASE_SEPOLIA_RPC --private-key $ORIGIN_PRIVATE_KEY --broadcast
```

This uses the real Base Sepolia callback proxy `0x476F44b11F5C942c58f4c8E3d0300E5710743585` unless
`CALLBACK_PROXY` is set. Note the printed `SentinelVault`.

## 5. Wire them together

```bash
cast send $REACTIVE_CONTRACT "setVault(address)" $VAULT \
  --rpc-url $LASNA_RPC --private-key $REACTIVE_PRIVATE_KEY

# sanity checks
cast call $REACTIVE_CONTRACT "vault()(address)"        --rpc-url $LASNA_RPC
cast call $REACTIVE_CONTRACT "minSeverity()(uint8)"    --rpc-url $LASNA_RPC   # 2
cast call $VAULT            "safeMode()(bool)"        --rpc-url $BASE_SEPOLIA_RPC  # false
```

## 6. Put money in the vault

```bash
cast send $VAULT_ASSET "mint(address,uint256)" $YOU 1000ether  --rpc-url $BASE_SEPOLIA_RPC --private-key $ORIGIN_PRIVATE_KEY  # mock token only
cast send $VAULT_ASSET "approve(address,uint256)" $VAULT 1000ether --rpc-url $BASE_SEPOLIA_RPC --private-key $YOU_KEY
cast send $VAULT "deposit(uint256)" 1000ether                  --rpc-url $BASE_SEPOLIA_RPC --private-key $YOU_KEY
```

## 7. First signal (the loop, without waiting for a crash)

```bash
export AGENT_PRIVATE_KEY=0x...
forge script script/PublishSmokeSignal.s.sol:PublishSmokeSignal \
  --rpc-url $BASE_SEPOLIA_RPC --private-key $AGENT_PRIVATE_KEY --broadcast
```

That produces:

1. the **origin** transaction (RiskSignal with severity 2 / 7200 bps),
2. a **reactive** transaction on Lasna (subscribed log → `react()` → `requestCallbackV_1_0`),
3. a **callback** transaction on Base Sepolia that flips the vault into safe mode.

Find (2) and (3) on the explorers:

* Lasna: <https://lasna.reactscan.net/> → your reactive contract → transactions
* Base Sepolia: <https://sepolia.basescan.org/> → the vault → transactions (`safeMode()` now returns `true`)

These three hashes are exactly what the DoraHacks submission asks for.

## 8. Run the real agent

```bash
export POOL_ADDRESS=0x...   # a pool on Base Sepolia, or a MockPool you deployed
export ORACLE_ADDRESS=$RISK_ORACLE
export AGENT_PRIVATE_KEY=0x...

python -m agent.sentinel_agent --feed rpc --pool $POOL_ADDRESS --oracle $ORACLE_ADDRESS \
  --rpc $BASE_SEPOLIA_RPC --agent-key $AGENT_PRIVATE_KEY --interval 0.5 --min-interval 5
```

Without a pool on a public testnet, drive it with `MockPool`:

```bash
forge create src/MockPool.sol:MockPool --constructor-args $BASE $QUOTE --rpc-url $BASE_SEPOLIA_RPC --private-key $ORIGIN_PRIVATE_KEY
```

then `swap(true, amount)` from a funded account to create the sell-off.

## Hardening before any real money

* `SentinelVault` has no owner-withdrawal path on purpose, but `clearSafeMode()` and `setSafeHarbor()` are
  owner-gated: put the owner behind a multisig.
* Replace `MockPool` with a real pool and do not trust a single spot price: the model should read a TWAP, or the
  callback payload should carry a price that the vault re-checks against its own oracle.
* `RiskOracle` allows one agent key. Rotate it with `setAgent(newAgent, true)`, and consider a threshold
  signature scheme if the key guards real value.
* Set `callbackGasLimit` with headroom: too low fails with `InsufficientGas` before the vault method runs, and the
  recipient still pays for the attempt.
