# Sentinel — an AI that decides when to pull the money out

**Reactive Omni Buildathon · track: Reactive Meets AI** (the latency work also serves *Every Second Counts*)

[**Demo video (2:28)**](https://github.com/dorithesky/reactive-sentinel/releases/download/demo-v1/demo.mp4) ·
[Live on Base Sepolia + Lasna](#live-on-the-public-testnets) · 48 Foundry + 21 Python tests passing

A cross-chain defence system where a **trained risk model** — not a rule, not a prompt — judges whether a pool is
breaking, publishes that judgement on Base Sepolia as an event, and a **Reactive contract on Lasna** turns it into
an on-chain callback that puts the vault into a defensive state. No keeper, no bot, no human in the loop. Once the
signal is on chain, the agent can be switched off and the defence still executes.

```
             BASE SEPOLIA (84532, 200 ms blocks)                    REACTIVE LASNA (5318007)
 ┌────────────────────────────────────────────────┐          ┌──────────────────────────────────────┐
 │  MockPool ──► AI risk agent (Python)           │          │  SentinelReactive                    │
 │     ▲            │ features → model → severity  │          │   · subscribed to RiskSignal          │
 │     │            ▼                             │          │   · re-checks chain, emitter, severity│
 │     │      RiskOracle  ── RiskSignal ─────────┼── event ─►│   · de-dupes, rate-limits by timestamp│
 │     │      (origin log)                        │  match   │   · SYSTEM.requestCallbackV_1_0(...)  │
 │     │                                          │          └───────────────┬──────────────────────┘
 │     │                                          │                          │ callback
 │     │        callback proxy ───────────────────┼──────────────────────────┘
 │     │            │  (injects reactive address) │
 │     │            ▼                             │
 │  SentinelVault ──► safe mode (severity 2)      │
 │               └──► funds to safe harbour (3)   │
 └────────────────────────────────────────────────┘
```

## What it actually does

The demo scenario, which `tools/local_loop.py` reproduces on two real EVM chains:

| # | Step | Where | Evidence |
|---|------|-------|----------|
| 1 | The agent polls pool reserves, computes six features, scores risk at **7,108 bps → DEFEND** and publishes `RiskSignal` | Base Sepolia | tx on `RiskOracle` (topic1 = signalId, topic2 = severity, topic3 = score) |
| 2 | The Reactive contract matches the log, re-validates it, and requests a callback with a `defend(...)` payload | Lasna | tx to `SentinelReactive` (`requestCallbackV_1_0`) |
| 3 | The callback proxy injects the reactive contract's address and calls the vault; the vault closes deposits and keeps exits open | Base Sepolia | `DefenseExecuted(severity=2, score=7108)` |
| 4 | The market keeps sliding; the model escalates to **9,593 bps → CRITICAL**; the escalation bypasses the cooldown and the vault moves **1,000 tUSD to the safe harbour** | both | `DefenseExecuted(severity=3, fundsMoved=1000e18)` |

### Measured full-loop latency (local two-chain harness)

| Leg | DEFEND signal | CRITICAL signal |
|---|---|---|
| agent decision → RiskSignal mined | **171 ms** | ~350 ms |
| signal mined → reactive tx mined | **103 ms** | 203 ms |
| reactive tx → vault updated | **104 ms** | 204 ms |
| **total decision → vault updated** | **378 ms** | ~760 ms |

Source: `runs/local-loop-report-20261124T095539Z.json` and `runs/local-loop-report-20261124T095652Z.json`,
both committed with this repository. A second run measured 170 / 204 / 205 ms = **578 ms**, and a third
212 / 204 / 205 ms = **620 ms** (`runs/local-loop-report-20261124T100446Z.json`): the spread between runs is block
alignment — whether the reactive transaction catches the next block or the one after it — not a change in the
system. Measured how:
the harness stamps every stage with `time.time()` on the machine that drives both chains; per-signal timestamps
live in `signal_timings`, keyed by the signal id that appears in all three transactions. The on-chain
`DefenseExecuted` event additionally carries `observedAt`/`executedAt` (second resolution) so the figure is
reproducible from chain data alone.

**Honest caveat:** the local nodes have 200 ms (origin) and 500 ms (Lasna) block times and zero network latency
between them, so this is a *lower bound*. A production loop adds the validator set's own finality, the signer's
queue, and real RPC latency; the reactive-side block time for Lasna Omni is ~1 s. Quote a range, not a single
number: 378–620 ms locally, and expect more on public networks (the live testnet loop above measured 1 s
cross-chain, 6 s from the agent's observation).

## Why the AI is part of the running system

The track rules say AI must be *inside* the system, not the tool that wrote it. Here the model sits in the
decision path and nothing else can take its place: the reactive contract only knows severities and scores that
this model produced.

**The model** (`agent/models/risk_model.json`, trained by `agent/train_model.py`)

| | |
|---|---|
| Type | logistic regression over 6 scale-free pool features, standardised inputs |
| Features | price drop from window high, 5-tick return, liquidity drop, sell pressure, return volatility, spot-vs-TWAP deviation |
| Label | "does the price fall another ≥ 300 bps within 15 ticks?" — the question a stop-loss must answer |
| Training data | 4,000 windows from a simulated constant-product pool: 20% sustained sell-off (20% of which fail), 22% fake-out (deep dip, V-shaped recovery), 13% chop, 45% calm |
| Held-out metrics | **AUC 0.961**, precision 0.84 / recall 0.63 at the DEFEND threshold |
| Inference | pure Python, microseconds, deterministic — the model is never the slow part of the loop |
| Explainability | every signal carries a `reasonCode` = keccak256 of the dominant contributing feature (`VOLATILITY_SPIKE`, `SELL_PRESSURE`, …), computed from the model's own signed contributions |

Reproduce: `python -m agent.train_model --seed 20261124` writes byte-identical weights.

Two design choices worth pointing at:

* **A big drop alone is not the signal.** The learned weight on `price_drop_bps` is slightly *negative*: in the
  training market a sharp dip is as often a fake-out as a breakdown. What separates them is one-sided flow plus
  rising volatility. That is a judgement a threshold cannot make and the reason this is a model.
* **The chain, not the agent, has the final say.** If the model is wrong, or the agent's key is stolen and spams
  `Critical`, the reactive contract still enforces its own policy before any money moves.

## The on-chain safety policy (why this is not a scripted demo)

`SentinelReactive.react()` refuses to act unless *all* of these hold:

| Guard | Rejects |
|---|---|
| `onlySystem` | any caller except the system contract `0x8888…8888` (a forged log record) |
| `log.chainId == originChainId` | logs from any other chain |
| `log.contractAddress == riskOracle` | events with the same signature emitted by another contract |
| `topic2 <= uint8.max` | a subtly truncated severity |
| `!handled[signalId]` | replays |
| `severity >= minSeverity` | `Watch` (informational, origin chain only) |
| timestamp cooldown | a second defence of the same severity within `cooldownSeconds` |
| **escalation exception** | never delays a *higher* severity than the last one |

Cooldown is measured in **timestamps, not blocks**: Base Sepolia produces 200 ms blocks, so a block-count cooldown
would be meaningless.

On the destination side, `SentinelVault.defend()` requires `onlyServiceProvider` (the callback really came through
the proxy) **and** `onlyCallbackSender` (the injected address is *our* reactive contract). Both are needed: the
first stops anyone calling the vault directly, the second stops any other reactive contract from driving our
vault through the legitimate proxy. `_CALLBACK_SENDER` is immutable — a wrong address means redeploying, which is
why the deployment order is documented and scripted.

The defence ladder never traps users: **deposits close, withdrawals stay open**, and a critical defence moves idle
funds to a pre-configured safe harbour rather than freezing them.

## Repository layout

```
src/
  RiskOracle.sol        origin: AI judgements with severity/score floors, replay guard, clock-skew check
  SentinelReactive.sol  Lasna: subscription, policy, callback request (requestCallbackV_1_0)
  SentinelVault.sol     destination: deposits/withdrawals, defence ladder, callback authentication
  MockPool.sol          origin: constant-product pool the agent observes
  mocks/                offline system contract, callback proxy, ERC-20
test/                   48 Foundry tests, incl. a full-loop test driven by a real emitted log
script/                 DeployOrigin / DeployVault / DeployReactive / PublishSmokeSignal
agent/
  features.py           six scale-free features from reserve snapshots
  model.py              model load, calibration, severity mapping, explainability
  train_model.py        dataset generator + trainer (deterministic)
  pool.py               RPC pool feed and synthetic feed
  chain.py              minimal JSON-RPC client with EIP-1559 signing
  sentinel_agent.py     the loop: observe → judge → publish, with its own rate limit
tools/local_loop.py     two-anvil harness that also plays the Reactive node
tests/                  21 pytest tests (features, model contract, agent decisions)
docs/                   latency method, demo script, deployment runbook
```

## Quickstart

Requirements: [Foundry](https://book.getfoundry.sh/) (forge/anvil/cast ≥ 1.0), Python ≥ 3.11,
`pip install eth-account eth-abi eth-utils pytest`.

```bash
forge build            # solidity 0.8.29, contracts vendored from reactive-lib-omni
forge test             # 48 tests
pytest tests -q        # 21 tests
```

Run the whole loop on two local chains (no faucet, no keys, no internet):

```bash
python tools/local_loop.py
```

It deploys everything, starts the agent as a separate process, crashes the pool, and prints the three
transactions, the vault state and the latency breakdown. `--scenario defend` stops at severity 2;
`--keep` leaves both nodes running for manual poking with `cast`.

Offline, chain-free, to see a decision on its own:

```bash
python -m agent.sentinel_agent --feed synthetic --dry-run --once
```

## Live on the public testnets

Deployed 2026-11-24 with a fresh testnet-only key (no mainnet funds anywhere near it). Every value below was read
back from the chain and is also stored in [`deployments/testnet.json`](deployments/testnet.json).

| Contract | Network | Address |
|---|---|---|
| `RiskOracle` | Base Sepolia (84532) | [`0xf833A74eEf68D47ccc09aeC1c26a947cc2Ee0bf6`](https://sepolia.basescan.org/address/0xf833A74eEf68D47ccc09aeC1c26a947cc2Ee0bf6) |
| `SentinelReactive` | Reactive Lasna (5318007) | [`0xDE6EF66DAD15C6071be1eFA39F08b3b89eb82E43`](https://lasna.reactscan.net/address/0xDE6EF66DAD15C6071be1eFA39F08b3b89eb82E43) |
| `SentinelVault` | Base Sepolia | [`0xbf0981de544CF16E1D46e5C646648CFD2E1132Ad`](https://sepolia.basescan.org/address/0xbf0981de544CF16E1D46e5C646648CFD2E1132Ad) |

One signal through the real Reactive Network (severity 2 / 7,200 bps, the vault holding 1,000 tUSD):

| # | Step | Transaction |
|---|---|---|
| 1 | `RiskSignal` published by the agent on Base Sepolia (block 62968843, gas 233,199) | [`0xdfcdb38d…8d64ce`](https://sepolia.basescan.org/tx/0xdfcdb38d5b4eaea7d5e91c3b1c01568e6e97904f6c41130fbe93670f3c8d64ce) |
| 2 | `react()` → `requestCallbackV_1_0` on Lasna (block 9129685, gas 160,796) | [`0xbb244492…1386d`](https://lasna.reactscan.net/tx/0xbb24449210130ac1a192c2cf7269fc020d76a944b0b605158502f0ec7d21386d) |
| 3 | callback through the proxy → `safeMode = true` (block 62968850, gas 184,077) | [`0xb9b65c54…63f465`](https://sepolia.basescan.org/tx/0xb9b65c5448d589a8b8f5f9ae82f62ca70407f15d73ef3c2ac1ef4a6fcb63f465) |

State after the loop: vault `safeMode()=true`, `defenceCount()=1`, `lastRiskScoreBps()=7200`, deposits untouched;
reactive contract `defenceRequests()=1`, `ignoredSignals()=0`.

**What the public testnets measured, honestly.** Block timestamps are second-resolution, so the cross-chain part
reads as 1 s (origin block 10:20:37 → callback block 10:20:38 UTC). The agent's own `observedAt` (10:20:32) to the
vault's `executedAt` (10:20:38) is **6 s**, of which about 5 s is the origin transaction waiting for its Base
Sepolia block through a public RPC. That is the number to quote for this deployment; the 378–620 ms below is the
local harness's floor with no network or signer queue in the path.

## Deploying to the testnets

Base Sepolia is the origin **and** destination; Reactive Lasna hosts the reactive contract. Full runbook in
[`docs/DEPLOY.md`](docs/DEPLOY.md); `bash tools/deploy_testnet.sh` runs the whole sequence and writes
`deployments/testnet.json` (it resumes where it stopped if a faucet wait is interrupted). Short version:

1. **Base Sepolia ETH** — any Sepolia faucet (Alchemy, QuickNode, Coinbase, `faucet.quicknode.com/base/sepolia`).
2. **lREACT on Lasna** — send Base Sepolia ETH to the faucet contract `0x2afaFD298b23b62760711756088F75B7409f5967`
   (`request(address)`), **1 ETH → 100 lREACT, max 5 ETH per transaction**; more than 5 ETH in one transfer is
   lost. The reactive contract pays its own gas in lREACT.
3. Deploy in this order — the callback-authentication cycle forces it:
   `RiskOracle` (Base Sepolia) → `SentinelReactive` (Lasna) → `SentinelVault` (Base Sepolia, with the reactive
   address as `callbackSender`) → `reactive.setVault(vault)`. On Base Sepolia the vault uses Reactive Network's
   real callback proxy `0x476F44b11F5C942c58f4c8E3d0300E5710743585`.
4. Fund the reactive contract with lREACT and the vault with a little Base Sepolia ETH (the *recipient* of a
   callback pays for its execution).
5. Point the agent at the pool and oracle and run it.

## Hackathon requirements → where they are

| Requirement | Where |
|---|---|
| Reactive contract that really does the work | `src/SentinelReactive.sol` — subscription, policy, callback request |
| Origin event → callback transaction hashes | live above, and in `deployments/testnet.json`; reproduced locally by `tools/local_loop.py` |
| Deployed contract addresses | live above (`deployments/testnet.json`), printed by the deploy scripts |
| Full-loop latency and how it was measured | table above, `docs/LATENCY.md`, `runs/local-loop-report-*.json` |
| Public repo + README that a stranger can follow | this file + `docs/` |
| Demo video ≤ 3 min | [release asset `demo.mp4`](https://github.com/dorithesky/reactive-sentinel/releases/download/demo-v1/demo.mp4) (2:28); script in `docs/DEMO.md`, rendered by `tools/build_demo.py` |

## What is deliberately not here

* **No LLM in the hot path.** A prompt is neither deterministic nor auditable, and it cannot be replayed by a
  judge. The model is small enough to recompute any decision offline; that property is the point.
* **No mainnet deployment.** The vault holds a mock ERC-20, there is no oracle price feed, and `MockPool` has no
  fees. A production version needs a real pool, a TWAP-resistant price source, and a withdrawal queue.
* **The callback proxy is mocked locally** (`MockCallbackProxy`) because the real one exists only on public
  testnets. On Base Sepolia the real proxy `0x476F44b11F5C942c58f4c8E3d0300E5710743585` is used by
  `script/DeployVault.s.sol`.
* **The Reactive node is simulated locally** by `tools/local_loop.py` (log matching → `react()` → callback
  delivery). It is the only piece that cannot be reproduced offline; it is also the piece that the on-chain
  transactions prove happened for real once deployed.

## Sources verified while building this

Everything about the Reactive Omni API in this repo was checked against the official material, not assumed:

| Claim | How it was checked |
|---|---|
| System contract is `0x8888888888888888888888888888888888888888` on both Lasna and mainnet | <https://dev.reactive.network/reactive-mainnet>; `cast codesize` returns 239 bytes on Lasna |
| `subscribe` / `unsubscribe` take six criteria, `REACTIVE_IGNORE` = `0xa65f…13ad` | <https://dev.reactive.network/reactive-contracts> and the vendored `AbstractReactive.sol` |
| `requestCallbackV_1_0(CallbackConfiguration_V_1_0)` with `{chainId, recipient, gasLimit, payload}` | <https://dev.reactive.network/> and the vendored `ISystemContract.sol` |
| The `vm` modifier and `emit Callback(...)` are deprecated in Omni | <https://dev.reactive.network/> migration notes |
| `react()` must be guarded as system-only, and the contract pays for its own transactions | `AbstractReactive` / `AbstractPayer`, docs |
| Callback payload's first `address` argument is overwritten by the proxy | <https://dev.reactive.network/callback-contracts> |
| Base Sepolia callback proxy `0x476F…3585`, Lasna chain ID `5318007` | <https://dev.reactive.network/origins-and-destinations>; live `eth_chainId` and `debt(address)` calls |
| lREACT faucet `0x2afa…5967` on Base Sepolia, 1 ETH→100 lREACT, max 5 ETH | <https://dev.reactive.network/reactive-mainnet>; contract code present (1,534 bytes) |

## Licence

MIT.
