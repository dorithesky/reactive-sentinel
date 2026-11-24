# How the latency is measured

The brief for *Every Second Counts* asks for a full-loop latency number **and the method**. This document is the
method, so the number can be argued with rather than believed.

## What counts as "the loop"

```
t0  the agent decides (RiskSignal calldata signed and handed to the RPC)
t1  the RiskSignal transaction is mined on the origin chain        <- t1 - t0 = agent + origin block
t2  the reactive transaction is mined on Lasna                     <- t2 - t1 = detection + reactive block
t3  the callback transaction is mined on the origin chain          <- t3 - t2 = signer + origin block
```

`t3 - t0` is the number quoted in the README. Anything before `t0` (feature extraction, model inference) is not
counted as loop latency, but it is not free either: the harness records the agent's own publish latency
(`latency_ms` in `runs/agent-decisions-*.jsonl`, typically 100–200 ms because it includes waiting for the origin
block).

## Where each timestamp comes from

| Timestamp | Source | Resolution |
|---|---|---|
| `t0` | the agent's decision log (`at`, UTC ISO-8601 with milliseconds) | 1 ms |
| `t1` | harness clock, the moment the log appears in `eth_getLogs` | 1 ms (plus the node's poll interval, 50 ms) |
| `t2` | harness clock, right after the `react()` receipt | 1 ms |
| `t3` | harness clock, right after the callback receipt | 1 ms |

The harness (`tools/local_loop.py`) plays the Reactive node itself, so it can stamp `t1`–`t3` on one clock. All
three transactions carry the same `signalId` in their calldata/logs (`signal_timings` in the report), so the
correlation cannot silently mix two different signals — a mistake an earlier version of the harness did make.

## What is included and what is not

Included:
* the origin block time (200 ms, matching Base Sepolia's real 200 ms blocks),
* the reactive block time (500 ms in the harness; production Lasna Omni is ~1 s),
* the destination block time (200 ms),
* two RPC round-trips per hop.

Not included:
* network latency between the chains and the operator (local nodes, loopback),
* the validator set's finality and the signer's internal queue,
* the harness's own 50 ms polling interval (it appears on both sides of the measurement, partly cancelling out).

Two consecutive runs of the same scenario measured **378 ms** and **578 ms**. The difference is entirely block
alignment: a 500 ms Lasna block means the reactive transaction can land in the next block or the one after it.
Report the range, not a single run — a single number invites the question of how it was picked.

So **378 ms is a floor**, not a promise. A production loop of the same shape should be expected to land in the
0.8–2.0 s range, which is what other Omni builds report (a comparable project measured a 1.6 s median loop).

## Reproducing

```bash
python tools/local_loop.py
python - <<'PY'
import json, glob
report = json.load(open(sorted(glob.glob("runs/local-loop-report-*.json"))[-1]))
print(report["latency"])
PY
```

Each run writes `runs/local-loop-report-<UTC>.json` with:

* `signal_timings` — per-signal `signal_mined` / `react_mined` / `callback_requested` / `callback_mined`
* `latency` — the deltas quoted above for the signal that actually executed
* `node_log` — every `react()` and delivery, with gas used, so a failed attempt is visible rather than quietly missing
* `timeline` — the full ordered stage list (deployment, warm-up, crash, defence)

## Why not measure on chain only

The `DefenseExecuted` event carries `observedAt` (when the agent observed the market) and `executedAt`, so a
second-resolution figure is derivable from chain data alone. It is too coarse to be interesting — the whole loop
fits inside one second — which is exactly why the harness clock is the primary source and the on-chain field is
kept as a cross-check.
