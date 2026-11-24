"""Minimal JSON-RPC chain client for the Sentinel agent and the local harness.

Only the handful of calls the loop needs are implemented, on purpose: the agent is hot-path code and a small,
auditable dependency surface is worth more than a full SDK. Signing uses EIP-1559 when a private key is given;
when it is not (local anvil), transactions are sent through `eth_sendTransaction` from an unlocked account.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from eth_account import Account

__all__ = ["RpcError", "Rpc", "Chain", "keccak_hex", "topic_of", "encode_call", "decode_log_data"]


class RpcError(RuntimeError):
    """Raised when the node answers with a JSON-RPC error."""


def keccak_hex(text: str) -> str:
    from eth_utils import keccak

    return "0x" + keccak(text=text).hex()


def topic_of(signature: str) -> str:
    return keccak_hex(signature)


def encode_call(signature: str, types: Iterable[str], values: Iterable[Any]) -> str:
    from eth_abi import encode as abi_encode

    return keccak_hex(signature)[:10] + abi_encode(list(types), list(values)).hex()


def decode_log_data(types: list[str], data: str) -> tuple:
    from eth_abi import decode as abi_decode

    raw = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    return abi_decode(types, raw)


@dataclass
class Rpc:
    url: str
    timeout: float = 20.0
    _next_id: int = field(default=0, repr=False)

    def call(self, method: str, params: Optional[list] = None) -> Any:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or []}
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:  # node answered, but not with JSON-RPC success
            raise RpcError(f"{method}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RpcError(f"{method}: {exc.reason}") from exc

        if body.get("error"):
            raise RpcError(f"{method}: {body['error']}")
        return body.get("result")


class Chain:
    """A chain endpoint plus (optionally) a signing account."""

    def __init__(
        self,
        rpc_url: str,
        *,
        private_key: Optional[str] = None,
        sender: Optional[str] = None,
        chain_id: Optional[int] = None,
    ) -> None:
        self.rpc = Rpc(rpc_url)
        self._chain_id = chain_id
        self.account = Account.from_key(private_key) if private_key else None
        self.sender = self.account.address if self.account else sender

    # -- basics ---------------------------------------------------------------------------------
    @property
    def chain_id(self) -> int:
        if self._chain_id is None:
            self._chain_id = int(self.rpc.call("eth_chainId"), 16)
        return self._chain_id

    def block_number(self) -> int:
        return int(self.rpc.call("eth_blockNumber"), 16)

    def get_code(self, address: str) -> str:
        return self.rpc.call("eth_getCode", [address, "latest"])

    def balance(self, address: str) -> int:
        return int(self.rpc.call("eth_getBalance", [address, "latest"]), 16)

    def call(self, to: str, data: str) -> str:
        return self.rpc.call("eth_call", [{"to": to, "data": data}, "latest"])

    # -- local-node helpers (anvil) -------------------------------------------------------------
    def set_code(self, address: str, code: str) -> None:
        self.rpc.call("anvil_setCode", [address, code])

    def set_balance(self, address: str, wei: int) -> None:
        self.rpc.call("anvil_setBalance", [address, hex(wei)])

    def impersonate(self, address: str) -> None:
        self.rpc.call("anvil_impersonateAccount", [address])

    def stop_impersonating(self, address: str) -> None:
        self.rpc.call("anvil_stopImpersonatingAccount", [address])

    # -- transactions ---------------------------------------------------------------------------
    def estimate_gas(self, to: Optional[str], data: str, value: int = 0, sender: Optional[str] = None) -> int:
        tx: dict[str, Any] = {"data": data}
        if to:
            tx["to"] = to
        if value:
            tx["value"] = hex(value)
        src = sender or self.sender
        if src:
            tx["from"] = src
        try:
            return int(self.rpc.call("eth_estimateGas", [tx]), 16)
        except RpcError:
            return 3_000_000

    def send(
        self,
        to: Optional[str],
        data: str,
        *,
        value: int = 0,
        gas: Optional[int] = None,
        wait: bool = True,
        timeout: float = 90.0,
    ) -> dict:
        """Sends a transaction and (by default) returns its receipt."""
        gas = gas or self.estimate_gas(to, data, value)
        tx_hash = self._broadcast(to, data, value, gas)
        if not wait:
            return {"transactionHash": tx_hash}
        return self.wait_receipt(tx_hash, timeout=timeout)

    def _broadcast(self, to: Optional[str], data: str, value: int, gas: int) -> str:
        if self.account is None:
            tx: dict[str, Any] = {"from": self.sender, "data": data, "gas": hex(gas)}
            if to:
                tx["to"] = to
            if value:
                tx["value"] = hex(value)
            return self.rpc.call("eth_sendTransaction", [tx])

        block = self.rpc.call("eth_getBlockByNumber", ["latest", False])
        base_fee = int(block["baseFeePerGas"], 16) if block and block.get("baseFeePerGas") else 0
        try:
            tip = int(self.rpc.call("eth_maxPriorityFeePerGas"), 16)
        except RpcError:
            tip = 1_000_000_000
        max_fee = base_fee * 2 + tip

        tx = {
            "nonce": int(self.rpc.call("eth_getTransactionCount", [self.account.address, "pending"]), 16),
            "gas": gas,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": tip,
            "chainId": self.chain_id,
            "type": 2,
            "value": value,
            "data": data,
        }
        if to:
            tx["to"] = to
        signed = Account.sign_transaction(tx, self.account.key)
        raw = "0x" + bytes(signed.raw_transaction).hex()
        return self.rpc.call("eth_sendRawTransaction", [raw])

    def wait_receipt(self, tx_hash: str, *, timeout: float = 90.0, poll: float = 0.1) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            receipt = self.rpc.call("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                return receipt
            time.sleep(poll)
        raise TimeoutError(f"no receipt for {tx_hash} within {timeout}s")

    # -- logs -----------------------------------------------------------------------------------
    def get_logs(
        self,
        *,
        from_block: int,
        to_block: int | str = "latest",
        address: Optional[str] = None,
        topics: Optional[list] = None,
    ) -> list[dict]:
        flt: dict[str, Any] = {
            "fromBlock": hex(from_block),
            "toBlock": to_block if isinstance(to_block, str) else hex(to_block),
        }
        if address:
            flt["address"] = address
        if topics:
            flt["topics"] = topics
        return self.rpc.call("eth_getLogs", [flt]) or []

    def block_timestamp(self, block_number: int) -> int:
        block = self.rpc.call("eth_getBlockByNumber", [hex(block_number), False])
        return int(block["timestamp"], 16)
