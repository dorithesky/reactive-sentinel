"""Feature extraction tests. Run with `pytest` from the project root."""

from __future__ import annotations

import math

import pytest

from agent.features import FEATURE_NAMES, MIN_WINDOW, FeatureWindow, PoolSnapshot


def _flat_snapshots(count: int, price: float = 3.0) -> list[PoolSnapshot]:
    base = 1_000_000.0
    return [
        PoolSnapshot(timestamp=float(i), base_reserve=base, quote_reserve=base * price, quote_volume=0.0)
        for i in range(count)
    ]


def test_window_is_not_ready_below_minimum() -> None:
    window = FeatureWindow(size=60)
    for snapshot in _flat_snapshots(MIN_WINDOW - 1):
        window.add(snapshot)
    assert window.features() is None


def test_flat_market_has_no_distress_signal() -> None:
    window = FeatureWindow(size=60)
    for snapshot in _flat_snapshots(MIN_WINDOW):
        window.add(snapshot)

    vector = window.features()
    assert vector is not None
    assert vector.values["price_drop_bps"] == pytest.approx(0.0)
    assert vector.values["liquidity_drop_bps"] == pytest.approx(0.0)
    assert vector.values["twap_deviation_bps"] == pytest.approx(0.0)
    assert vector.values["sell_pressure_bps"] == pytest.approx(5_000.0)
    assert vector.values["volatility_bps"] == pytest.approx(0.0)


def test_price_drop_is_measured_against_the_window_high() -> None:
    window = FeatureWindow(size=60)
    for snapshot in _flat_snapshots(MIN_WINDOW):
        window.add(snapshot)
    # A 5% drop from the high, with the same depth.
    base = 1_000_000.0
    window.add(PoolSnapshot(timestamp=99.0, base_reserve=base, quote_reserve=base * 3.0 * 0.95, quote_volume=-1000.0))

    vector = window.features()
    assert vector is not None
    assert vector.values["price_drop_bps"] == pytest.approx(500.0, rel=1e-6)
    assert vector.values["ret_short_bps"] < 0
    assert vector.values["twap_deviation_bps"] > 0


def test_sell_pressure_uses_only_the_window_flow() -> None:
    window = FeatureWindow(size=60)
    base = 1_000_000.0
    flows = [0.0, -100.0, -100.0, -100.0, 50.0, 50.0, 50.0, 50.0]
    for index, flow in enumerate(flows):
        window.add(
            PoolSnapshot(timestamp=float(index), base_reserve=base, quote_reserve=base * 3.0, quote_volume=flow)
        )

    vector = window.features()
    assert vector is not None
    # 300 of 500 total quote flow was selling.
    assert vector.values["sell_pressure_bps"] == pytest.approx(300 / 500 * 10_000)


def test_volatility_grows_with_returns() -> None:
    calm = FeatureWindow(size=60)
    wild = FeatureWindow(size=60)
    base = 1_000_000.0
    for index in range(MIN_WINDOW):
        calm.add(PoolSnapshot(index, base, base * (3.0 + 0.0001 * index), 0.0))
        wild.add(PoolSnapshot(index, base, base * (3.0 + 0.05 * index), 0.0))

    calm_vector = calm.features()
    wild_vector = wild.features()
    assert calm_vector and wild_vector
    assert wild_vector.values["volatility_bps"] > calm_vector.values["volatility_bps"] * 10


def test_feature_vector_order_is_stable() -> None:
    window = FeatureWindow(size=60)
    for snapshot in _flat_snapshots(MIN_WINDOW):
        window.add(snapshot)
    vector = window.features()
    assert vector is not None
    assert list(vector.values.keys()) == FEATURE_NAMES
    assert vector.as_list() == [vector.values[name] for name in FEATURE_NAMES]


def test_window_is_bounded() -> None:
    window = FeatureWindow(size=10)
    for index in range(100):
        window.add(PoolSnapshot(float(index), 1_000_000.0, 3_000_000.0 * (1 + index * 1e-5), 0.0))
    assert len(window) == 10
    vector = window.features()
    assert vector is not None
    assert vector.window == 10


def test_no_nan_in_output() -> None:
    window = FeatureWindow(size=60)
    for snapshot in _flat_snapshots(MIN_WINDOW):
        window.add(snapshot)
    vector = window.features()
    assert vector is not None
    for name, value in vector.values.items():
        assert not math.isnan(value), name
