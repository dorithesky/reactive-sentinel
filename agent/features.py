"""Feature extraction for the Sentinel risk model.

Every feature is expressed in basis points so it is independent of token decimals, pool size and price level.
The only input is a rolling window of on-chain reserve snapshots, which is exactly what a bot watching a DEX
pool has: price, depth and the direction of the flow that moved them.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

FEATURE_NAMES = [
    "price_drop_bps",  # distance below the window's high
    "ret_short_bps",  # 5-tick return
    "liquidity_drop_bps",  # distance below the window's deepest liquidity
    "sell_pressure_bps",  # share of quote volume that was selling
    "volatility_bps",  # standard deviation of per-tick returns
    "twap_deviation_bps",  # spot below the window TWAP
]

#: Minimum snapshots before the model is allowed to speak. Below this the window is too short for a
#: meaningful volatility estimate, so the agent reports "warmup" instead of guessing.
MIN_WINDOW = 8


@dataclass(frozen=True)
class PoolSnapshot:
    """One observation of the origin pool."""

    timestamp: float
    base_reserve: float
    quote_reserve: float
    base_volume: float = 0.0  # base units that flowed in (+) or out (-) since the previous snapshot
    quote_volume: float = 0.0  # quote units that flowed in (+) or out (-)

    @property
    def price(self) -> float:
        if self.base_reserve <= 0:
            return 0.0
        return self.quote_reserve / self.base_reserve

    @property
    def depth(self) -> float:
        """Liquidity in quote units: what a seller can actually take out before the price gapes."""
        return self.quote_reserve


@dataclass
class FeatureVector:
    values: dict[str, float]
    price: float
    depth: float
    window: int

    def as_list(self) -> list[float]:
        return [self.values[name] for name in FEATURE_NAMES]


class FeatureWindow:
    """Rolling window of pool snapshots producing `FeatureVector`s."""

    def __init__(self, size: int = 60) -> None:
        self._snapshots: Deque[PoolSnapshot] = deque(maxlen=size)

    def __len__(self) -> int:
        return len(self._snapshots)

    def add(self, snapshot: PoolSnapshot) -> None:
        self._snapshots.append(snapshot)

    def ready(self) -> bool:
        return len(self._snapshots) >= MIN_WINDOW

    def snapshot(self, index: int = -1) -> PoolSnapshot:
        return list(self._snapshots)[index]

    def features(self) -> Optional[FeatureVector]:
        if not self.ready():
            return None

        snaps = list(self._snapshots)
        prices = [s.price for s in snaps if s.price > 0]
        depths = [s.depth for s in snaps]
        if len(prices) < MIN_WINDOW:
            return None

        latest = prices[-1]
        peak = max(prices)
        price_drop_bps = 0.0 if peak <= 0 else max(0.0, (peak - latest) / peak * 10_000)

        anchor = prices[-6] if len(prices) >= 6 else prices[0]
        ret_short_bps = 0.0 if anchor <= 0 else (latest / anchor - 1.0) * 10_000

        deepest = max(depths)
        liquidity_drop_bps = 0.0 if deepest <= 0 else max(0.0, (deepest - depths[-1]) / deepest * 10_000)

        sell = 0.0
        buy = 0.0
        for s in snaps[1:]:
            if s.quote_volume < 0:  # quote left the pool: someone sold base into it
                sell += -s.quote_volume
            else:
                buy += s.quote_volume
        total_flow = sell + buy
        sell_pressure_bps = 5_000.0 if total_flow <= 0 else sell / total_flow * 10_000

        returns = [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices)) if prices[i - 1] > 0]
        if len(returns) >= 2:
            mean = sum(returns) / len(returns)
            var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
            volatility_bps = math.sqrt(var) * 10_000
        else:
            volatility_bps = 0.0

        twap = sum(prices) / len(prices)
        twap_deviation_bps = 0.0 if twap <= 0 else (twap - latest) / twap * 10_000

        return FeatureVector(
            values={
                "price_drop_bps": price_drop_bps,
                "ret_short_bps": ret_short_bps,
                "liquidity_drop_bps": liquidity_drop_bps,
                "sell_pressure_bps": sell_pressure_bps,
                "volatility_bps": volatility_bps,
                "twap_deviation_bps": twap_deviation_bps,
            },
            price=latest,
            depth=depths[-1],
            window=len(snaps),
        )
