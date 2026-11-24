"""Sentinel risk agent.

The agent is the "judgement" half of the loop:

    pool reserves  ->  features  ->  learned risk model  ->  severity  ->  RiskSignal on Base Sepolia

Everything downstream (subscription, callback request, vault defence) happens without this process: once the
signal is on chain, the reactive contract takes over. That separation is what makes the demo honest — the agent
can be killed right after publishing and the defence still executes.

Usage
    python -m agent.sentinel_agent --help

Offline (no chain, scripted market, prints the decision it would publish):
    python -m agent.sentinel_agent --feed synthetic --dry-run --once

Against the local two-chain harness the harness starts this module with `--feed rpc`.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from .chain import Chain, encode_call
from .features import FEATURE_NAMES, FeatureWindow, PoolSnapshot
from .model import RiskModel
from .pool import RpcPoolFeed, SyntheticFeed

PUBLISH_SIGNATURE = "publish(bytes32,uint8,uint256,uint64,bytes32,bytes)"
PUBLISH_TYPES = ["bytes32", "uint8", "uint256", "uint64", "bytes32", "bytes"]

SEVERITY_NAMES = {0: "NONE", 1: "WATCH", 2: "DEFEND", 3: "CRITICAL"}


@dataclass
class Decision:
    at: str
    signal_id: str
    severity: int
    severity_name: str
    score_bps: int
    reason: str
    observed_at: int
    features: dict
    action: str  # "published" | "skipped" | "dry-run" | "error"
    tx_hash: Optional[str] = None
    block_number: Optional[int] = None
    error: Optional[str] = None
    latency_ms: Optional[float] = None
    explanation: dict = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _metrics_blob(values: dict) -> bytes:
    """On-chain evidence blob: the five market features, in basis points."""
    ordered = [
        int(round(values["price_drop_bps"])),
        int(round(values["liquidity_drop_bps"])),
        int(round(values["sell_pressure_bps"])),
        int(round(values["volatility_bps"])),
        int(round(values["twap_deviation_bps"])),
    ]
    return abi_encode(["uint256", "uint256", "uint256", "uint256", "uint256"], ordered)


class SentinelAgent:
    def __init__(
        self,
        *,
        chain: Optional[Chain],
        oracle: Optional[str],
        model: RiskModel,
        feed,
        window_size: int = 60,
        min_interval: float = 3.0,
        log_path: Optional[Path] = None,
        verbose: bool = True,
    ) -> None:
        self.chain = chain
        self.oracle = to_checksum_address(oracle) if oracle else None
        self.model = model
        self.feed = feed
        self.window = FeatureWindow(size=window_size)
        self.min_interval = min_interval
        self.log_path = log_path
        self.verbose = verbose
        self._last_publish = 0.0
        self._last_severity = 0
        self._signal_counter = 0

    # -- decisions ------------------------------------------------------------------------------
    def next_signal_id(self, observed_at: int, score_bps: int) -> bytes:
        self._signal_counter += 1
        payload = f"{observed_at}:{score_bps}:{self._signal_counter}".encode()
        return keccak(payload)

    def observe(self, snapshot: PoolSnapshot) -> None:
        self.window.add(snapshot)

    def evaluate(self) -> Optional[dict]:
        vector = self.window.features()
        if vector is None:
            return None
        explanation = self.model.explain(vector.values)
        explanation["price"] = vector.price
        explanation["depth"] = vector.depth
        explanation["window"] = vector.window
        return explanation

    def should_publish(self, explanation: dict, now: float) -> tuple[bool, str]:
        severity = explanation["severity"]
        if severity == 0:
            return False, "below-watch"
        if now - self._last_publish < self.min_interval:
            return False, "agent-cooldown"
        return True, "ok"

    # -- publishing -----------------------------------------------------------------------------
    def publish(self, explanation: dict, *, dry_run: bool = False) -> Decision:
        observed_at = int(time.time())
        score_bps = int(explanation["score_bps"])
        severity = int(explanation["severity"])
        signal_id = self.next_signal_id(observed_at, score_bps)
        reason_code = self.model.reason_code(explanation["features"])

        decision = Decision(
            at=_now_iso(),
            signal_id="0x" + signal_id.hex(),
            severity=severity,
            severity_name=SEVERITY_NAMES[severity],
            score_bps=score_bps,
            reason=explanation["reason"],
            observed_at=observed_at,
            features=explanation["features"],
            action="dry-run" if dry_run else "published",
            explanation={
                "top_contributions": explanation["top_contributions"],
                "price": round(explanation.get("price", 0.0), 6),
                "depth": round(explanation.get("depth", 0.0), 2),
            },
        )

        if dry_run or self.chain is None or self.oracle is None:
            decision.action = "dry-run"
            self._log(decision)
            return decision

        calldata = encode_call(
            PUBLISH_SIGNATURE,
            PUBLISH_TYPES,
            [signal_id, severity, score_bps, observed_at, reason_code, _metrics_blob(explanation["features"])],
        )
        started = time.monotonic()
        try:
            receipt = self.chain.send(self.oracle, calldata)
            decision.tx_hash = receipt["transactionHash"]
            decision.block_number = int(receipt["blockNumber"], 16)
            decision.latency_ms = round((time.monotonic() - started) * 1000, 1)
            decision.action = "published"
            self._last_publish = time.monotonic()
            self._last_severity = severity
        except Exception as exc:  # noqa: BLE001 - the loop must keep running
            decision.action = "error"
            decision.error = f"{type(exc).__name__}: {exc}"
        self._log(decision)
        return decision

    # -- loop -----------------------------------------------------------------------------------
    def run(self, *, once: bool = False, dry_run: bool = False, interval: float = 0.25, max_polls: int = 0) -> int:
        published = 0
        polls = 0
        stop = False

        def _handle(_signum, _frame):
            nonlocal stop
            stop = True

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

        while not stop:
            polls += 1
            try:
                snapshot = self.feed.snapshot()
            except Exception as exc:  # noqa: BLE001 - RPC hiccups must not kill the agent
                if self.verbose:
                    print(f"[agent] feed error: {exc}", file=sys.stderr)
                time.sleep(interval)
                continue

            self.observe(snapshot)
            explanation = self.evaluate()
            if explanation is not None:
                ok, _reason = self.should_publish(explanation, time.monotonic())
                if ok or dry_run:
                    # In dry-run mode a sub-threshold judgement is still worth showing: it is the difference
                    # between "the model saw nothing" and "the model saw something too mild to act on".
                    decision = self.publish(explanation, dry_run=dry_run or not ok)
                    published += 1 if decision.action == "published" else 0
                    if self.verbose:
                        print(
                            f"[agent] {decision.severity_name:<8} score={decision.score_bps:>5}bps "
                            f"reason={decision.reason:<16} action={decision.action}"
                            + (f" tx={decision.tx_hash}" if decision.tx_hash else ""),
                            flush=True,
                        )
                if once:
                    break
            if max_polls and polls >= max_polls:
                break
            time.sleep(interval)

        return published

    def _log(self, decision: Decision) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as handle:
            handle.write(json.dumps(asdict(decision), default=str) + "\n")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sentinel AI risk agent")
    parser.add_argument("--rpc", default=os.environ.get("ORIGIN_RPC", "http://127.0.0.1:8545"))
    parser.add_argument("--chain-id", type=int, default=None)
    parser.add_argument("--pool", default=os.environ.get("POOL_ADDRESS"), help="origin pool address")
    parser.add_argument("--oracle", default=os.environ.get("ORACLE_ADDRESS"), help="RiskOracle address")
    parser.add_argument("--agent-key", default=os.environ.get("AGENT_PRIVATE_KEY"), help="agent signing key")
    parser.add_argument("--sender", default=os.environ.get("AGENT_ADDRESS"), help="unlocked account (local node)")
    parser.add_argument("--feed", choices=["rpc", "synthetic"], default="rpc")
    parser.add_argument("--model", type=Path, default=None)
    parser.add_argument("--interval", type=float, default=0.25, help="seconds between polls")
    parser.add_argument("--min-interval", type=float, default=3.0, help="seconds between publications")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-polls", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log", default="runs/agent-decisions.jsonl")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    model = RiskModel.load(args.model)

    chain: Optional[Chain] = None
    feed = None
    if args.feed == "rpc":
        if not args.pool:
            print("--pool is required for the rpc feed", file=sys.stderr)
            return 2
        chain = Chain(args.rpc, private_key=args.agent_key, sender=args.sender, chain_id=args.chain_id)
        feed = RpcPoolFeed(chain=chain, pool=to_checksum_address(args.pool))
    else:
        feed = SyntheticFeed()

    if not args.dry_run and (chain is None or not args.oracle):
        print("--oracle is required to publish (or pass --dry-run)", file=sys.stderr)
        return 2

    agent = SentinelAgent(
        chain=chain,
        oracle=args.oracle,
        model=model,
        feed=feed,
        min_interval=args.min_interval,
        log_path=Path(args.log),
        verbose=not args.quiet,
    )
    published = agent.run(
        once=args.once, dry_run=args.dry_run, interval=args.interval, max_polls=args.max_polls
    )
    if args.quiet:
        print(json.dumps({"published": published}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
