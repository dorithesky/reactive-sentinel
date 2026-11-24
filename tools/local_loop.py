#!/usr/bin/env python3
"""Local two-chain harness for the Sentinel loop.

It stands up the same shape the production loop has, on two anvil nodes:

    chain A (84532, 0.2s blocks)  = "Base Sepolia": pool, RiskOracle, callback proxy, SentinelVault
    chain B (5318007)             = "Reactive Lasna": system contract (mock, etched at 0x8888) + SentinelReactive

and then plays the part of the Reactive Network node itself:

    1. watch origin logs, match them against the subscription the reactive contract registered
    2. hand a LogRecord to `react()` as the system contract
    3. take the `requestCallbackV_1_0` request and deliver it through the callback proxy

Because every step is a real transaction on a real EVM, the latency numbers this script prints are the loop's
latency, not a simulation of it. What it cannot reproduce is the real thing's infrastructure latency (the
validator set, the signer's own queue), so treat the result as a lower bound.

Usage:
    python tools/local_loop.py                     # full run, fresh anvil pair
    python tools/local_loop.py --keep              # leave the nodes running for manual poking
    python tools/local_loop.py --scenario defend   # stop at severity 2 instead of a full drain
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from eth_abi import decode as abi_decode, encode as abi_encode
from eth_utils import to_checksum_address

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.chain import Chain, encode_call, topic_of  # noqa: E402

SYSTEM_ADDR = "0x8888888888888888888888888888888888888888"
ORIGIN_CHAIN_ID = 84532  # Base Sepolia
REACTIVE_CHAIN_ID = 5_318_007  # Reactive Lasna
BASE_SEPOLIA_CALLBACK_PROXY = "0x476F44b11F5C942c58f4c8E3d0300E5710743585"

#: Wildcard sentinel the Reactive system contract uses for topic positions (and 0 for chain/address).
REACTIVE_IGNORE = 0xA65F96FC951C35EAD38878E0F0B7A3C744A6F5CCC1476B313353CE31712313AD

RISK_SIGNAL_TOPIC = topic_of("RiskSignal(bytes32,uint8,uint256,uint64,bytes32,bytes)")
CALLBACK_REQUESTED_TOPIC = topic_of("CallbackRequested(address,uint256,address,uint64,bytes)")
DEFENSE_EXECUTED_TOPIC = topic_of("DefenseExecuted(bytes32,uint8,uint256,bytes32,uint64,uint256,uint256,uint256)")

LOG_RECORD_TYPE = (
    "(uint256,address,uint256,uint256,uint256,uint256,bytes,uint256,uint256,uint256,uint256,uint256)"
)
REACT_SIG = f"react({LOG_RECORD_TYPE})"


# ------------------------------------------------------------------------------------------------------------
# Infrastructure
# ------------------------------------------------------------------------------------------------------------


def _free_port(start: int) -> int:
    port = start
    while port < start + 50:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
        port += 1
    raise RuntimeError("no free port")


class Anvil:
    def __init__(self, port: int, chain_id: int, block_time: float, log_path: Path) -> None:
        self.port = port
        self.chain_id = chain_id
        self.block_time = block_time
        self.log_path = log_path
        self.proc: Optional[subprocess.Popen] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.log_path.open("w")
        self.proc = subprocess.Popen(
            [
                "anvil",
                "--port",
                str(self.port),
                "--chain-id",
                str(self.chain_id),
                "--block-time",
                str(self.block_time),
                "--accounts",
                "10",
                "--balance",
                "100000",
                "--quiet",
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 20
        chain = Chain(self.url, sender="0x0000000000000000000000000000000000000000")
        while time.time() < deadline:
            try:
                if chain.chain_id == self.chain_id:
                    return
            except Exception:  # noqa: BLE001 - node not up yet
                time.sleep(0.1)
        raise RuntimeError(f"anvil on {self.url} did not become ready")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def artifact_path(contract: str) -> Path:
    matches = list((ROOT / "out").glob(f"*/{contract}.json"))
    if not matches:
        raise FileNotFoundError(f"artifact for {contract} not found - run `forge build` first")
    return matches[0]


def artifact(contract: str) -> dict:
    return json.loads(artifact_path(contract).read_text())


def bytecode(contract: str) -> str:
    return artifact(contract)["bytecode"]["object"]


def deploy(
    chain: Chain,
    contract: str,
    types: tuple = (),
    values: tuple = (),
    *,
    gas: int = 8_000_000,
) -> str:
    data = bytecode(contract)
    if types:
        data += abi_encode(list(types), list(values)).hex()
    receipt = chain.send(None, data, gas=gas)
    if receipt.get("status") != "0x1":
        raise RuntimeError(f"deployment of {contract} failed: {receipt}")
    return to_checksum_address(receipt["contractAddress"])


def send(chain: Chain, to: str, signature: str, types: list[str], values: list, *, gas: int = 2_000_000) -> dict:
    receipt = chain.send(to, encode_call(signature, types, values), gas=gas)
    if receipt.get("status") != "0x1":
        raise RuntimeError(f"{signature} failed: {receipt}")
    return receipt


# ------------------------------------------------------------------------------------------------------------
# Reactive node simulator
# ------------------------------------------------------------------------------------------------------------


@dataclass
class Stage:
    name: str
    at: float  # seconds since harness start (monotonic)
    epoch: float  # absolute wall-clock time, so the agent's own log can be correlated
    detail: str = ""


@dataclass
class Timeline:
    stages: list[Stage] = field(default_factory=list)
    _t0: float = field(default_factory=time.monotonic)

    def mark(self, name: str, detail: str = "") -> float:
        now = time.monotonic()
        self.stages.append(Stage(name=name, at=now - self._t0, epoch=time.time(), detail=detail))
        return now

    def table(self) -> list[dict]:
        return [asdict(stage) for stage in self.stages]


class ReactiveNodeSimulator(threading.Thread):
    """Plays the Reactive Network node: log matching, `react()`, and callback delivery."""

    def __init__(
        self,
        origin: Chain,
        reactive_chain: Chain,
        reactive_addr: str,
        proxy_addr: str,
        vault_addr: str,
        timeline: Timeline,
        *,
        poll: float = 0.05,
    ) -> None:
        super().__init__(daemon=True)
        self.origin = origin
        self.reactive_chain = reactive_chain
        self.reactive_addr = reactive_addr
        self.proxy_addr = proxy_addr
        self.vault_addr = vault_addr
        self.timeline = timeline
        self.poll = poll
        # `react()` must arrive from the system contract, so transactions are sent from an impersonated
        # 0x8888 account rather than from the harness EOA.
        self.system_signer = Chain(reactive_chain.rpc.url, sender=SYSTEM_ADDR)
        self.stop_event = threading.Event()
        self.cursor_origin = origin.block_number()
        self.cursor_reactive = reactive_chain.block_number()
        self.subscriptions: list[dict] = []
        #: Per-signal wall-clock timestamps: signal_mined -> react_mined -> callback_requested -> callback_mined.
        self.timings: dict[str, dict[str, float]] = {}
        self.log: list[dict] = []
        self.deliveries: list[dict] = []
        self.errors: list[str] = []

    # -- subscription ---------------------------------------------------------------------------
    def load_subscriptions(self) -> None:
        system = self.reactive_chain
        count = int(
            abi_decode(
                ["uint256"],
                bytes.fromhex(
                    system.call(
                        SYSTEM_ADDR, encode_call("subscriptionCount(address)", ["address"], [self.reactive_addr])
                    )[2:]
                ),
            )[0]
        )
        for index in range(count):
            raw = system.call(
                SYSTEM_ADDR,
                encode_call("subscriptionAt(address,uint256)", ["address", "uint256"], [self.reactive_addr, index]),
            )
            chain_id, contract_addr, t0, t1, t2, t3 = abi_decode(
                ["uint256", "address", "uint256", "uint256", "uint256", "uint256"], bytes.fromhex(raw[2:])
            )
            self.subscriptions.append(
                {
                    "chain_id": chain_id,
                    "contract": contract_addr,
                    "topics": [t0, t1, t2, t3],
                }
            )
        self.log.append({"event": "subscriptions-loaded", "subscriptions": self.subscriptions})

    def _matches(self, log_entry: dict) -> bool:
        topics = [int(topic, 16) for topic in log_entry["topics"]]
        emitter = log_entry["address"].lower()
        for sub in self.subscriptions:
            if sub["chain_id"] not in (0, ORIGIN_CHAIN_ID):
                continue
            if sub["contract"] != "0x0000000000000000000000000000000000000000":
                if sub["contract"].lower() != emitter:
                    continue
            for index, expected in enumerate(sub["topics"]):
                if expected in (0, REACTIVE_IGNORE):  # wildcard for this topic position
                    continue
                if index >= len(topics) or topics[index] != expected:
                    break
            else:
                return True
        return False

    # -- loop -----------------------------------------------------------------------------------
    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._scan_origin()
                self._scan_reactive()
            except Exception as exc:  # noqa: BLE001 - keep the node alive, record the fault
                self.errors.append(f"{type(exc).__name__}: {exc}")
            time.sleep(self.poll)

    def _scan_origin(self) -> None:
        head = self.origin.block_number()
        if head < self.cursor_origin:
            return
        logs = self.origin.get_logs(
            from_block=self.cursor_origin, to_block=head, topics=[[RISK_SIGNAL_TOPIC]]
        )
        for entry in logs:
            if not self._matches(entry):
                continue
            signal_id = "0x" + format(int(entry["topics"][1], 16), "064x")
            self.timings.setdefault(signal_id, {})["signal_mined"] = time.time()
            self.timeline.mark("origin:signal-mined", f"{signal_id[:10]} block {int(entry['blockNumber'], 16)}")
            self._react(entry)
        self.cursor_origin = max(self.cursor_origin, head + 1)

    def _react(self, entry: dict) -> None:
        topics = [int(topic, 16) for topic in entry["topics"]]
        record = (
            ORIGIN_CHAIN_ID,
            to_checksum_address(entry["address"]),
            topics[0],
            topics[1] if len(topics) > 1 else 0,
            topics[2] if len(topics) > 2 else 0,
            topics[3] if len(topics) > 3 else 0,
            bytes.fromhex(entry["data"][2:]),
            int(entry["blockNumber"], 16),
            0,
            int(entry["blockHash"], 16),
            int(entry["transactionHash"], 16),
            int(entry["logIndex"], 16),
        )
        self.reactive_chain.impersonate(SYSTEM_ADDR)
        try:
            sent = time.monotonic()
            calldata = encode_call(REACT_SIG, [LOG_RECORD_TYPE], [record])
            receipt = self.system_signer.send(self.reactive_addr, calldata, gas=2_500_000)
            mined = time.monotonic()
            signal_id = "0x" + format(topics[1], "064x")
            self.timings.setdefault(signal_id, {})["react_mined"] = time.time()
            self.timeline.mark("reactive-node:react-mined", f"{signal_id[:10]} gas {int(receipt['gasUsed'], 16)}")
            entry = {
                "event": "react",
                "tx": receipt["transactionHash"],
                "status": receipt["status"],
                "gas_used": int(receipt.get("gasUsed", "0x0"), 16),
                "send_ms": round((mined - sent) * 1000, 1),
            }
            if receipt["status"] != "0x1":
                entry["revert"] = self._revert_reason(calldata, int(receipt["blockNumber"], 16))
                try:
                    entry["trace"] = self.reactive_chain.rpc.call(
                        "debug_traceTransaction", [receipt["transactionHash"], {"tracer": "callTracer"}]
                    )
                except Exception as exc:  # noqa: BLE001
                    entry["trace_error"] = str(exc)
            self.log.append(entry)
        finally:
            self.reactive_chain.stop_impersonating(SYSTEM_ADDR)

    def _revert_reason(self, calldata: str, block: int) -> str:
        try:
            return str(
                self.reactive_chain.rpc.call(
                    "eth_call",
                    [{"from": SYSTEM_ADDR, "to": self.reactive_addr, "data": calldata}, hex(block)],
                )
            )
        except Exception as exc:  # noqa: BLE001 - the message is the point of this helper
            return str(exc)

    def _scan_reactive(self) -> None:
        head = self.reactive_chain.block_number()
        if head < self.cursor_reactive:
            return
        logs = self.reactive_chain.get_logs(
            from_block=self.cursor_reactive, to_block=head, topics=[[CALLBACK_REQUESTED_TOPIC]]
        )
        for entry in logs:
            topics = [int(topic, 16) for topic in entry["topics"]]
            chain_id = topics[2]
            recipient = to_checksum_address("0x" + format(topics[3], "040x"))
            gas_limit, payload = abi_decode(["uint64", "bytes"], bytes.fromhex(entry["data"][2:]))
            try:
                # `payload` is full calldata: strip the 4-byte `defend(...)` selector first.
                _, payload_signal, severity, _, _, _ = abi_decode(
                    ["address", "bytes32", "uint8", "uint256", "uint64", "bytes32"], payload[4:]
                )
                payload_signal_id = "0x" + payload_signal.hex()
            except Exception:  # noqa: BLE001 - the payload is our own, but never crash the node on decode
                payload_signal_id = None
                severity = None
            if payload_signal_id:
                self.timings.setdefault(payload_signal_id, {})["callback_requested"] = time.time()
            self.timeline.mark(
                "reactive-node:callback-requested", f"{str(payload_signal_id)[:10]} chain {chain_id} gas {gas_limit}"
            )
            if chain_id != ORIGIN_CHAIN_ID:
                self.errors.append(f"callback for unexpected chain {chain_id}")
                continue
            receipt = self.origin.send(
                self.proxy_addr,
                encode_call("deliver(address,address,bytes)", ["address", "address", "bytes"], [
                    self.reactive_addr,
                    recipient,
                    payload,
                ]),
                gas=1_500_000,
            )
            if payload_signal_id:
                self.timings.setdefault(payload_signal_id, {})["callback_mined"] = time.time()
            self.timeline.mark("reactive-node:callback-mined", receipt["transactionHash"])
            self.deliveries.append(
                {
                    "signal_id": payload_signal_id,
                    "severity": severity,
                    "recipient": recipient,
                    "chain_id": chain_id,
                    "payload_bytes": len(payload),
                    "tx": receipt["transactionHash"],
                    "status": receipt["status"],
                    "gas_used": int(receipt.get("gasUsed", "0x0"), 16),
                }
            )
        self.cursor_reactive = max(self.cursor_reactive, head + 1)


# ------------------------------------------------------------------------------------------------------------
# Deployment
# ------------------------------------------------------------------------------------------------------------


@dataclass
class Deployment:
    pool: str
    base_token: str
    quote_token: str
    oracle: str
    proxy: str
    vault: str
    reactive: str
    harbor: str
    owner: str
    agent: str
    trader: str
    user: str


def deploy_all(origin: Chain, reactive_chain: Chain, accounts: list[str], timeline: Timeline) -> Deployment:
    owner, agent, trader, user = accounts[0], accounts[1], accounts[2], accounts[3]
    harbor = accounts[4]

    origin_owner = Chain(origin.rpc.url, sender=owner)
    origin_agent = Chain(origin.rpc.url, sender=agent)
    origin_trader = Chain(origin.rpc.url, sender=trader)
    origin_user = Chain(origin.rpc.url, sender=user)
    reactive_owner = Chain(reactive_chain.rpc.url, sender=owner)

    timeline.mark("deploy:start")

    # --- origin chain: tokens and pool -----------------------------------------------------------
    base_token = deploy(origin_owner, "MockERC20", ("string", "string", "uint8"), ("Test USD", "tUSD", 18))
    quote_token = deploy(origin_owner, "MockERC20", ("string", "string", "uint8"), ("Mock USD", "mUSD", 18))
    pool = deploy(origin_owner, "MockPool", ("address", "address"), (base_token, quote_token))

    send(origin_owner, base_token, "mint(address,uint256)", ["address", "uint256"], [owner, 5_000_000 * 10**18])
    send(origin_owner, quote_token, "mint(address,uint256)", ["address", "uint256"], [owner, 15_000_000 * 10**18])
    send(origin_owner, base_token, "approve(address,uint256)", ["address", "uint256"], [pool, 2**256 - 1])
    send(origin_owner, quote_token, "approve(address,uint256)", ["address", "uint256"], [pool, 2**256 - 1])
    send(
        origin_owner,
        pool,
        "addLiquidity(uint256,uint256)",
        ["uint256", "uint256"],
        [1_000_000 * 10**18, 3_000_000 * 10**18],
    )
    timeline.mark("deploy:pool-ready")

    # --- origin chain: AI oracle -----------------------------------------------------------------
    oracle = deploy(origin_owner, "RiskOracle", ("address", "address"), (owner, agent))
    timeline.mark("deploy:risk-oracle")

    # --- reactive chain: system contract + reactive contract -------------------------------------
    temp_system = deploy(Chain(reactive_chain.rpc.url, sender=owner), "MockSystemContract")
    code = reactive_chain.get_code(temp_system)
    reactive_chain.set_code(SYSTEM_ADDR, code)
    reactive_chain.set_balance(SYSTEM_ADDR, 10**20)
    timeline.mark("deploy:system-contract-etched")

    topic0 = int(topic_of("RiskSignal(bytes32,uint8,uint256,uint64,bytes32,bytes)"), 16)
    reactive_addr = deploy(
        reactive_owner,
        "SentinelReactive",
        ("uint256", "address", "uint256", "address"),
        (ORIGIN_CHAIN_ID, oracle, ORIGIN_CHAIN_ID, owner),
    )
    reactive_chain.set_balance(reactive_addr, 10**20)  # lREACT for reactive transactions
    timeline.mark("deploy:reactive")

    # --- origin chain: callback proxy + vault ----------------------------------------------------
    proxy = deploy(origin_owner, "MockCallbackProxy")
    vault = deploy(
        origin_owner,
        "SentinelVault",
        ("address", "address", "address", "address", "address"),
        (proxy, reactive_addr, base_token, harbor, owner),
    )
    timeline.mark("deploy:vault")

    # wire the vault into the reactive contract (deployment order required by the callback auth model)
    send(
        reactive_owner,
        reactive_addr,
        "setVault(address)",
        ["address"],
        [vault],
    )
    timeline.mark("deploy:vault-wired")

    # --- origin chain: a user funds the vault ----------------------------------------------------
    send(origin_owner, base_token, "mint(address,uint256)", ["address", "uint256"], [user, 1_000 * 10**18])
    send(origin_user, base_token, "approve(address,uint256)", ["address", "uint256"], [vault, 2**256 - 1])
    send(origin_user, vault, "deposit(uint256)", ["uint256"], [1_000 * 10**18])
    timeline.mark("deploy:vault-funded")

    # --- trader gets ammunition to move the market -----------------------------------------------
    send(origin_owner, base_token, "mint(address,uint256)", ["address", "uint256"], [trader, 2_000_000 * 10**18])
    send(origin_trader, base_token, "approve(address,uint256)", ["address", "uint256"], [pool, 2**256 - 1])

    return Deployment(
        pool=pool,
        base_token=base_token,
        quote_token=quote_token,
        oracle=oracle,
        proxy=proxy,
        vault=vault,
        reactive=reactive_addr,
        harbor=harbor,
        owner=owner,
        agent=agent,
        trader=trader,
        user=user,
    )


# ------------------------------------------------------------------------------------------------------------
# Market scenario
# ------------------------------------------------------------------------------------------------------------


def run_market_crash(
    chain: Chain,
    dep: Deployment,
    *,
    steps: int,
    size_bps: int,
    growth_bps: int,
    pause: float,
    stop_when=None,
) -> list[str]:
    """Sells base into the pool in growing size, the way a real unwind looks from the outside.

    Stops as soon as `stop_when()` returns True, so the demo does not overshoot into a 90% drawdown, and keeps
    selling if the defence has not happened yet, so a run never ends half-crashed for lack of market pressure.
    """
    trader = Chain(chain.rpc.url, sender=dep.trader)
    hashes = []
    for step in range(steps):
        if stop_when is not None and stop_when():
            break
        reference = int(
            abi_decode(
                ["uint112", "uint112"],
                bytes.fromhex(chain.call(dep.pool, encode_call("reserves()", [], []))[2:]),
            )[0]
        )
        amount = reference * (size_bps + growth_bps * step) // 10_000
        receipt = trader.send(
            dep.pool,
            encode_call("swap(bool,uint256)", ["bool", "uint256"], [True, amount]),
            gas=500_000,
        )
        hashes.append(receipt["transactionHash"])
        time.sleep(pause)
    return hashes


def nudge_market(chain: Chain, dep: Deployment, *, rounds: int, pause: float) -> None:
    """Small two-way flow so the agent's feature window fills with a calm market first."""
    trader = Chain(chain.rpc.url, sender=dep.trader)
    for step in range(rounds):
        reference = int(
            abi_decode(
                ["uint112", "uint112"],
                bytes.fromhex(chain.call(dep.pool, encode_call("reserves()", [], []))[2:]),
            )[0]
        )
        amount = reference * (3 + step) // 10_000
        base_in = step % 2 == 0
        trader.send(
            dep.pool, encode_call("swap(bool,uint256)", ["bool", "uint256"], [base_in, amount]), gas=400_000
        )
        time.sleep(pause)


# ------------------------------------------------------------------------------------------------------------
# Reporting
# ------------------------------------------------------------------------------------------------------------


def read_vault_state(origin: Chain, dep: Deployment) -> dict:
    def read(signature: str):
        raw = origin.call(dep.vault, encode_call(signature, [], []))
        return int(raw, 16)

    return {
        "safe_mode": bool(read("safeMode()")),
        "liquidated": bool(read("liquidated()")),
        "defence_count": read("defenceCount()"),
        "last_severity": read("lastSeverity()"),
        "last_risk_score_bps": read("lastRiskScoreBps()"),
        "last_signal_id": "0x" + format(read("lastSignalId()"), "064x"),
        "vault_balance": int(
            abi_decode(
                ["uint256"],
                bytes.fromhex(
                    origin.call(
                        dep.base_token, encode_call("balanceOf(address)", ["address"], [dep.vault])
                    )[2:]
                ),
            )[0]
        ),
        "harbor_balance": int(
            abi_decode(
                ["uint256"],
                bytes.fromhex(
                    origin.call(
                        dep.base_token, encode_call("balanceOf(address)", ["address"], [dep.harbor])
                    )[2:]
                ),
            )[0]
        ),
    }


def collect_defence_events(origin: Chain, dep: Deployment, from_block: int) -> list[dict]:
    logs = origin.get_logs(from_block=from_block, to_block="latest", address=dep.vault, topics=[[DEFENSE_EXECUTED_TOPIC]])
    events = []
    for entry in logs:
        topics = [int(topic, 16) for topic in entry["topics"]]
        risk_score, reason, observed_at, executed_at, funds_moved, latency_seconds = abi_decode(
            ["uint256", "bytes32", "uint64", "uint256", "uint256", "uint256"], bytes.fromhex(entry["data"][2:])
        )
        events.append(
            {
                "signal_id": "0x" + format(topics[1], "064x"),
                "severity": topics[2],
                "risk_score_bps": risk_score,
                "reason_code": "0x" + reason.hex(),
                "observed_at": observed_at,
                "executed_at": executed_at,
                "funds_moved": funds_moved,
                "on_chain_latency_seconds": latency_seconds,
                "tx": entry["transactionHash"],
                "block": int(entry["blockNumber"], 16),
            }
        )
    return events


def stage_deltas(
    agent_decisions: list[dict], defence_events: list[dict], timings: dict[str, dict[str, float]]
) -> dict:
    """Millisecond breakdown of the loop, per signal.

    `signal_mined` is when the RiskSignal transaction was included on the origin chain; `react_mined` is when the
    reactive transaction landed on Lasna; `callback_mined` is when the vault was actually updated. The agent's own
    publish time comes from its decision log, the only place the pre-chain part of the loop exists.
    """
    if not defence_events:
        return {"note": "no defence executed"}
    target = defence_events[0]["signal_id"]
    marks = timings.get(target, {})

    decision = next(
        (
            entry
            for entry in agent_decisions
            if entry.get("action") == "published" and entry.get("signal_id") == target
        ),
        None,
    )
    publish_epoch = None
    if decision and decision.get("at"):
        try:
            publish_epoch = datetime.fromisoformat(decision["at"]).timestamp()
        except ValueError:
            publish_epoch = None

    def delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
        return None if a is None or b is None else round((b - a) * 1000, 1)

    return {
        "signal_id": target,
        "agent_publish_to_signal_mined_ms": delta(publish_epoch, marks.get("signal_mined")),
        "signal_mined_to_react_mined_ms": delta(marks.get("signal_mined"), marks.get("react_mined")),
        "react_mined_to_callback_mined_ms": delta(marks.get("react_mined"), marks.get("callback_mined")),
        "total_publish_to_callback_ms": delta(publish_epoch, marks.get("callback_mined")),
        "note": "agent publish time from its decision log; the chain hops from the harness clock",
    }


# ------------------------------------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Sentinel loop on a local two-chain harness")
    parser.add_argument("--origin-port", type=int, default=0)
    parser.add_argument("--reactive-port", type=int, default=0)
    parser.add_argument("--origin-block-time", type=float, default=0.2, help="Base Sepolia produces 200ms blocks")
    parser.add_argument("--reactive-block-time", type=float, default=0.5)
    parser.add_argument("--scenario", choices=["critical", "defend"], default="critical")
    parser.add_argument("--warmup-polls", type=int, default=14)
    parser.add_argument("--crash-steps", type=int, default=16)
    parser.add_argument("--start-size-bps", type=int, default=70, help="first sell size, in bps of reserves")
    parser.add_argument("--growth-bps", type=int, default=110, help="how much each sell grows, in bps")
    parser.add_argument("--crash-pause", type=float, default=0.4, help="seconds between crash trades")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--keep", action="store_true", help="leave the anvil nodes running")
    parser.add_argument("--out", default=None, help="report path prefix")
    args = parser.parse_args(argv)

    if shutil.which("anvil") is None:
        print("anvil not found on PATH (install Foundry)", file=sys.stderr)
        return 2
    if not (ROOT / "out").exists():
        print("artifacts missing - run `forge build` first", file=sys.stderr)
        return 2

    origin_port = args.origin_port or _free_port(8545)
    reactive_port = args.reactive_port or _free_port(9545)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = ROOT / "runs"
    run_dir.mkdir(exist_ok=True)

    origin_node = Anvil(origin_port, ORIGIN_CHAIN_ID, args.origin_block_time, run_dir / f"anvil-origin-{stamp}.log")
    reactive_node = Anvil(
        reactive_port, REACTIVE_CHAIN_ID, args.reactive_block_time, run_dir / f"anvil-reactive-{stamp}.log"
    )
    timeline = Timeline()

    origin_node.start()
    reactive_node.start()
    timeline.mark("harness:anvil-ready")

    try:
        origin_admin = Chain(origin_node.url, sender="0x0000000000000000000000000000000000000000")
        accounts = [to_checksum_address(a) for a in origin_admin.rpc.call("eth_accounts")][:6]
        origin = Chain(origin_node.url, sender=accounts[0])
        reactive_chain = Chain(reactive_node.url, sender=accounts[0])

        dep = deploy_all(origin, reactive_chain, accounts, timeline)
        start_block = origin.block_number()
        print(json.dumps({"deployment": asdict(dep)}, indent=2))

        simulator = ReactiveNodeSimulator(
            origin=origin,
            reactive_chain=reactive_chain,
            reactive_addr=dep.reactive,
            proxy_addr=dep.proxy,
            vault_addr=dep.vault,
            timeline=timeline,
        )
        simulator.load_subscriptions()
        simulator.start()
        timeline.mark("harness:reactive-node-running")

        # The agent: a separate process, exactly as it would run against a real RPC.
        agent_log = run_dir / f"agent-decisions-{stamp}.jsonl"
        timeline.mark("agent:start")
        agent_cmd = [
            sys.executable,
            "-m",
            "agent.sentinel_agent",
            "--rpc",
            origin_node.url,
            "--chain-id",
            str(ORIGIN_CHAIN_ID),
            "--pool",
            dep.pool,
            "--oracle",
            dep.oracle,
            "--sender",
            dep.agent,
            "--feed",
            "rpc",
            "--interval",
            "0.15",
            "--min-interval",
            "0.5",
            "--log",
            str(agent_log),
        ]
        agent_proc = subprocess.Popen(agent_cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        try:
            # Warm the feature window with ordinary flow before the crash.
            nudge_market(origin, dep, rounds=args.warmup_polls, pause=0.12)
            timeline.mark("market:warmup-done")
            time.sleep(1.0)

            print("\n--- market crash ---")
            def _defended() -> bool:
                events = collect_defence_events(origin, dep, start_block)
                if args.scenario == "critical":
                    return any(event["severity"] >= 3 for event in events)
                return bool(events)

            run_market_crash(
                origin,
                dep,
                steps=args.crash_steps,
                size_bps=args.start_size_bps,
                growth_bps=args.growth_bps,
                pause=args.crash_pause,
                stop_when=_defended,
            )
            timeline.mark("market:crash-done")

            # Wait for the vault to record a defence.
            deadline = time.time() + args.timeout
            defence_events: list[dict] = []
            while time.time() < deadline:
                defence_events = collect_defence_events(origin, dep, start_block)
                if defence_events:
                    timeline.mark("harness:defence-observed")
                    if args.scenario == "critical" and any(e["severity"] == 3 for e in defence_events):
                        break
                    if args.scenario == "defend" and defence_events:
                        break
                time.sleep(0.2)
        finally:
            agent_proc.terminate()
            try:
                agent_stdout = agent_proc.communicate(timeout=10)[0]
            except subprocess.TimeoutExpired:
                agent_proc.kill()
                agent_stdout = ""
            simulator.stop_event.set()
            simulator.join(timeout=5)

        vault_state = read_vault_state(origin, dep)
        decisions: list[dict] = []
        if agent_log.exists():
            for line in agent_log.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        decisions.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        deltas = stage_deltas(decisions, defence_events, simulator.timings)

        report = {
            "generated_at": stamp,
            "scenario": args.scenario,
            "chains": {
                "origin": {"chain_id": ORIGIN_CHAIN_ID, "rpc": origin_node.url, "block_time_s": args.origin_block_time},
                "reactive": {
                    "chain_id": REACTIVE_CHAIN_ID,
                    "rpc": reactive_node.url,
                    "block_time_s": args.reactive_block_time,
                },
            },
            "deployment": asdict(dep),
            "subscriptions": simulator.subscriptions,
            "signal_timings": simulator.timings,
            "callback_deliveries": simulator.deliveries,
            "defence_events": defence_events,
            "vault_state": vault_state,
            "latency": deltas,
            "timeline": timeline.table(),
            "node_log": simulator.log,
            "node_errors": simulator.errors,
            "agent_decisions": decisions,
            "agent_output": agent_stdout.strip().splitlines()[-25:],
            "agent_log": str(agent_log.relative_to(ROOT)),
        }

        out_path = Path(args.out) if args.out else run_dir / f"local-loop-report-{stamp}.json"
        out_path.write_text(json.dumps(report, indent=2) + "\n")

        print("\n================ Sentinel local loop ================")
        print(f"origin chain   : {origin_node.url} (id {ORIGIN_CHAIN_ID}, {args.origin_block_time}s blocks)")
        print(f"reactive chain : {reactive_node.url} (id {REACTIVE_CHAIN_ID})")
        print(f"subscription   : {simulator.subscriptions}")
        print(f"callbacks      : {simulator.deliveries}")
        for event in defence_events:
            print(
                f"defence        : severity={event['severity']} score={event['risk_score_bps']}bps "
                f"moved={event['funds_moved'] / 1e18:,.2f} tUSD on_chain_latency={event['on_chain_latency_seconds']}s"
            )
        print(f"vault state    : {vault_state}")
        print(f"latency        : {deltas}")
        print(f"report         : {out_path.relative_to(ROOT)}")
        if simulator.errors:
            print(f"node errors    : {simulator.errors[:3]}")
        success = vault_state["defence_count"] > 0
        print(f"RESULT         : {'PASS' if success else 'FAIL'}")
        return 0 if success else 1

    finally:
        if not args.keep:
            origin_node.stop()
            reactive_node.stop()
        else:
            print(f"\nnodes left running: {origin_node.url} / {reactive_node.url} (ctrl-c to stop)")


if __name__ == "__main__":
    raise SystemExit(main())
