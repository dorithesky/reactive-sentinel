"""Sources of pool observations.

`RpcPoolFeed` is the production path: it reads the origin pool's reserves over JSON-RPC. `SyntheticFeed`
replays a scripted market with no chain at all, which keeps the model and the agent testable offline.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterator, Optional

from .chain import Chain, decode_log_data, encode_call
from .features import PoolSnapshot

RESERVES_SIGNATURE = "reserves()"


@dataclass
class RpcPoolFeed:
    """Reads `reserves()` from a constant-product pool and turns reserve deltas into flow."""

    chain: Chain
    pool: str
    base_decimals: int = 18
    quote_decimals: int = 18

    def __post_init__(self) -> None:
        self._selector = encode_call(RESERVES_SIGNATURE, [], [])
        self._prev: Optional[tuple[float, float]] = None

    def snapshot(self, timestamp: Optional[float] = None) -> PoolSnapshot:
        raw = self.chain.call(self.pool, self._selector)
        base_raw, quote_raw = decode_log_data(["uint112", "uint112"], raw)
        base = base_raw / 10**self.base_decimals
        quote = quote_raw / 10**self.quote_decimals

        base_volume = quote_volume = 0.0
        if self._prev is not None:
            base_volume = base - self._prev[0]
            quote_volume = quote - self._prev[1]
        self._prev = (base, quote)

        import time

        return PoolSnapshot(
            timestamp=time.time() if timestamp is None else timestamp,
            base_reserve=base,
            quote_reserve=quote,
            base_volume=base_volume,
            quote_volume=quote_volume,
        )


@dataclass
class SyntheticFeed:
    """Scripted constant-product market: calm drift, then a one-sided sell-off.

    Used by `--feed synthetic` demos and by the unit tests. Shares the swap maths with the training generator so
    the model sees the same kind of market it was trained on.
    """

    ticks: int = 80
    crash_at: int = 58
    seed: int = 7
    start_base: float = 1_000_000.0
    start_quote: float = 3_000_000.0

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._pool = [self.start_base, self.start_quote]
        self._tick = 0

    def _swap(self, base_in: float) -> float:
        base, quote = self._pool
        k = base * quote
        self._pool = [base + base_in, k / (base + base_in)]
        return -(quote - self._pool[1])

    def _buy(self, quote_in: float) -> float:
        base, quote = self._pool
        k = base * quote
        self._pool = [k / (quote + quote_in), quote + quote_in]
        return quote_in

    def snapshot(self, timestamp: Optional[float] = None) -> PoolSnapshot:
        tick = self._tick
        self._tick += 1

        if tick >= self.crash_at:
            step = tick - self.crash_at
            flow = self._swap(self._pool[0] * (0.012 + 0.004 * step))
        elif self._rng.random() < 0.5:
            flow = self._swap(self._pool[0] * abs(self._rng.gauss(0.0008, 0.0010)))
        else:
            flow = self._buy(self._pool[1] * abs(self._rng.gauss(0.0008, 0.0010)))

        return PoolSnapshot(
            timestamp=float(tick) if timestamp is None else timestamp,
            base_reserve=self._pool[0],
            quote_reserve=self._pool[1],
            base_volume=0.0,
            quote_volume=flow,
        )

    def __iter__(self) -> Iterator[PoolSnapshot]:
        for _ in range(self.ticks):
            yield self.snapshot()


def synthetic_price_path(seed: int = 7, ticks: int = 80) -> list[float]:
    """Convenience for notebooks and the README figure."""
    feed = SyntheticFeed(seed=seed, ticks=ticks)
    return [snap.quote_reserve / snap.base_reserve for snap in feed]
