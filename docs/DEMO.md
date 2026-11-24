# Demo video script (≤ 3 minutes)

The judges score four things equally: **it runs on chain**, **the reactive contract does real work**, **it solves
a real problem**, **it is complete and readable**. Two of those need the video to show the loop honestly, so the
script spends its time on the loop and on what makes this version different from the other stop-loss builds.

## Before recording

```bash
# terminal 1 — the whole loop on two local chains (about 40 s per run)
python tools/local_loop.py

# terminal 2 — the evidence you will show on screen
cast logs --address <vault> --rpc-url http://127.0.0.1:8545 'DefenseExecuted(bytes32,uint8,uint256,bytes32,uint64,uint256,uint256,uint256)'
```

Have these windows ready:

1. `README.md` (architecture diagram)
2. `runs/local-loop-report-*.json` opened at the `latency` block
3. the vault's `safeMode()` / `liquidated()` before and after: `cast call <vault> "safeMode()(bool)" --rpc-url http://127.0.0.1:8545`

## Shot list

| Time | On screen | Voice-over |
|---|---|---|
| 0:00–0:20 | README architecture diagram | "A stop-loss usually depends on someone's bot staying alive. Sentinel moves the decision on chain: a model judges the risk, a Reactive contract on Lasna enforces it, and the vault defends itself. Kill the agent and the defence still happens." |
| 0:20–0:50 | `python -m agent.train_model` output + `agent/model.py` | "The AI is not a prompt. It is a logistic regression trained on 4,000 simulated pool windows — AUC 0.96. Six scale-free features: price drop, short return, liquidity drop, sell pressure, volatility, TWAP deviation. It learned that a big drop alone means nothing — the weight on price drop is slightly *negative*, because dips fake out. What precedes a real slide is one-sided selling plus rising volatility. Every decision carries a `reasonCode` on chain so the judgement can be audited later." |
| 0:50–1:30 | `python tools/local_loop.py` running | "Two real EVM chains: Base Sepolia at 200 ms blocks, Lasna at 500 ms. The market sells into the pool. The agent sees the flow and scores it — 7,108 basis points, severity DEFEND — and publishes a RiskSignal. The Reactive contract matches it, re-checks the chain, the emitter, the severity, the replay guard, and requests a callback. The vault closes deposits, keeps withdrawals open." |
| 1:30–2:00 | the run's printed latency table | "Full loop, decision to vault update: 378 to 578 milliseconds across runs — about 170 to the origin block, 100 to 200 to the reactive transaction depending on which Lasna block it catches, and 100 to 200 to the callback. That is a floor, not a promise: local nodes have no network latency and Lasna is faster here than in production." |
| 2:00–2:30 | second defence event, funds moved | "The market keeps sliding. The model escalates to CRITICAL — and escalation bypasses the cooldown, because a worse signal must never wait behind a better one. The vault moves the whole balance to the safe harbour. No keeper touched this." |
| 2:30–2:50 | `forge test` summary, then repo tree | "48 Foundry tests, 21 Python tests, all passing. The reactive policy has its own tests for chain mismatch, emitter spoofing, replays, cooldown and escalation." |
| 2:50–3:00 | vault state after | "Deployed on Base Sepolia and Lasna, here are the three transaction hashes: origin, reactive, callback." |

## Talking points to hit (and traps to avoid)

* Say **what the reactive contract checks** before it acts. "It really does work" is judged by whether the
  contract could have refused; show the guards.
* Show the **failed/ignored** path at least once: a `Watch` signal that produces an `SignalIgnored` event and no
  callback. Judges reward honest behaviour over a happy path.
* Do not claim the 378 ms as production latency; say "floor, measured this way".
* Do not describe the model as an LLM. It is a trained classifier; claiming otherwise invites a fair challenge.
