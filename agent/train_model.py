"""Trains the Sentinel risk model on a simulated market and writes `agent/models/risk_model.json`.

Run:  python -m agent.train_model

The simulation is intentionally simple but not trivial: a constant-product pool is driven by random trades, and
a fraction of the windows contain a sustained sell-off. The label the model learns is *forward looking*: "does
the price fall another 3% within the next 15 ticks?" That is the question a stop-loss has to answer, and it is
why a model rather than a fixed threshold decides on chain.

Everything is seeded, so the artefact is reproducible: `--seed` reproduces byte-identical weights.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .features import FEATURE_NAMES, FeatureWindow, PoolSnapshot
from .model import DEFAULT_MODEL_PATH, REASON_LABELS, keccak32


@dataclass
class Sample:
    features: dict[str, float]
    label: int
    regime: str


def _swap(pool: list[float], base_in: float, rng: random.Random) -> float:
    """Applies a base-in swap to the pool and returns the quote volume that left it (negative flow)."""
    base, quote = pool
    if base_in <= 0:
        return 0.0
    k = base * quote
    new_base = base + base_in
    new_quote = k / new_base
    out = quote - new_quote
    pool[0], pool[1] = new_base, new_quote
    return -out


def _buy(pool: list[float], quote_in: float, rng: random.Random) -> float:
    base, quote = pool
    if quote_in <= 0:
        return 0.0
    k = base * quote
    new_quote = quote + quote_in
    new_base = k / new_quote
    pool[0], pool[1] = new_base, new_quote
    return quote_in


def _simulate_window(rng: random.Random, regime: str) -> Sample:
    """Builds 60 snapshots up to a decision point, then 15 more to label the decision.

    The three regimes overlap deliberately. A "crash" and a "fakeout" look almost identical at the decision
    point; what separates them is the flow that follows, and whether the selling is one-sided or gets absorbed.
    A model that simply fires on "price already dropped" scores badly here, which is the point.
    """
    pool = [1_000_000.0, 3_000_000.0]
    window = FeatureWindow(size=60)
    price_path: list[float] = []

    crash_start = 50 if regime in ("crash", "fakeout", "chop") else None
    failed_breakdown = regime == "crash" and rng.random() < 0.2

    for tick in range(60):
        quote_volume = 0.0
        if crash_start is not None and tick >= crash_start:
            if regime == "chop":
                # Two-way violence: big sells and big buys, price ends roughly where it started.
                if tick % 2 == 0:
                    quote_volume = _swap(pool, pool[0] * rng.uniform(0.015, 0.040), rng)
                else:
                    quote_volume = _buy(pool, pool[1] * rng.uniform(0.012, 0.035), rng)
            elif regime == "fakeout":
                if tick < crash_start + 4:
                    quote_volume = _swap(pool, pool[0] * rng.uniform(0.015, 0.035), rng)
                else:
                    quote_volume = _buy(pool, pool[1] * rng.uniform(0.010, 0.030), rng)
            else:
                size = pool[0] * rng.uniform(0.008, 0.030) * (1.0 + 0.30 * (tick - crash_start))
                quote_volume = _swap(pool, size, rng)
                if failed_breakdown and tick > crash_start + 2:
                    quote_volume += _buy(pool, pool[1] * rng.uniform(0.004, 0.012), rng)
        else:
            if rng.random() < 0.5:
                quote_volume = _swap(pool, pool[0] * abs(rng.gauss(0.0008, 0.0011)), rng)
            else:
                quote_volume = _buy(pool, pool[1] * abs(rng.gauss(0.0008, 0.0011)), rng)

        # Polling is not perfect: a little measurement noise on the quote reserve.
        pool[1] *= 1.0 + rng.gauss(0, 0.0006)
        price_path.append(pool[1] / pool[0])
        window.add(
            PoolSnapshot(
                timestamp=float(tick),
                base_reserve=pool[0],
                quote_reserve=pool[1],
                base_volume=0.0,
                quote_volume=quote_volume,
            )
        )

    feature_vector = window.features()
    if feature_vector is None:  # pragma: no cover - window is always full here
        raise RuntimeError("window not ready")

    decision_price = price_path[-1]

    for tick in range(15):
        if regime == "crash" and not failed_breakdown:
            _swap(pool, pool[0] * rng.uniform(0.006, 0.022), rng)
        elif regime == "crash":  # failed breakdown: dip buyers arrive
            _buy(pool, pool[1] * rng.uniform(0.006, 0.018), rng)
        elif regime == "fakeout":
            _buy(pool, pool[1] * rng.uniform(0.008, 0.020), rng)
        elif regime == "chop":
            if tick % 2 == 0:
                _swap(pool, pool[0] * rng.uniform(0.008, 0.025), rng)
            else:
                _buy(pool, pool[1] * rng.uniform(0.008, 0.025), rng)
        else:
            if rng.random() < 0.5:
                _swap(pool, pool[0] * abs(rng.gauss(0.0005, 0.0009)), rng)
            else:
                _buy(pool, pool[1] * abs(rng.gauss(0.0005, 0.0009)), rng)
        pool[1] *= 1.0 + rng.gauss(0, 0.0005)

    final_price = pool[1] / pool[0]
    drawdown_bps = (decision_price - final_price) / decision_price * 10_000
    label = 1 if drawdown_bps >= 300 else 0
    return Sample(features=feature_vector.values, label=label, regime=regime)


def build_dataset(samples: int, seed: int) -> list[Sample]:
    rng = random.Random(seed)
    data: list[Sample] = []
    for _ in range(samples):
        roll = rng.random()
        if roll < 0.20:
            regime = "crash"
        elif roll < 0.42:
            regime = "fakeout"
        elif roll < 0.55:
            regime = "chop"
        else:
            regime = "calm"
        data.append(_simulate_window(rng, regime))
    return data


def standardise(train: list[Sample]):
    mean = [sum(s.features[name] for s in train) / len(train) for name in FEATURE_NAMES]
    std = []
    for idx, name in enumerate(FEATURE_NAMES):
        var = sum((s.features[name] - mean[idx]) ** 2 for s in train) / (len(train) - 1)
        std.append(math.sqrt(var) or 1.0)
    return mean, std


def train_logistic(
    train: list[Sample], mean: list[float], std: list[float], *, epochs: int = 900, lr: float = 0.35, l2: float = 1e-3
):
    weights = [0.0] * len(FEATURE_NAMES)
    bias = 0.0

    def z(sample: Sample) -> list[float]:
        return [(sample.features[name] - m) / s for name, m, s in zip(FEATURE_NAMES, mean, std)]

    n = len(train)
    for epoch in range(epochs):
        grad_w = [0.0] * len(weights)
        grad_b = 0.0
        for sample in train:
            zs = z(sample)
            logit = bias + sum(w * zi for w, zi in zip(weights, zs))
            pred = 1.0 / (1.0 + math.exp(-logit))
            error = pred - sample.label
            for i, zi in enumerate(zs):
                grad_w[i] += error * zi
            grad_b += error
        for i in range(len(weights)):
            weights[i] -= lr * (grad_w[i] / n + l2 * weights[i])
        bias -= lr * (grad_b / n)
        if epoch % 300 == 0:
            lr *= 0.6
    return weights, bias


def _scores(samples: list[Sample], weights, bias, mean, std) -> list[float]:
    out = []
    for sample in samples:
        zs = [(sample.features[name] - m) / s for name, m, s in zip(FEATURE_NAMES, mean, std)]
        logita = bias + sum(w * zi for w, zi in zip(weights, zs))
        out.append(1.0 / (1.0 + math.exp(-logita)))
    return out


def roc_auc(labels: list[int], scores: list[float]) -> float:
    pairs = sorted(zip(scores, labels))
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return float("nan")
    rank_sum = 0.0
    for rank, (_, label) in enumerate(pairs, start=1):
        if label == 1:
            rank_sum += rank
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def band_metrics(labels: list[int], scores: list[float], threshold: float) -> dict:
    tp = fp = fn = 0
    for label, score in zip(labels, scores):
        predicted = score >= threshold
        if predicted and label:
            tp += 1
        elif predicted and not label:
            fp += 1
        elif not predicted and label:
            fn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "threshold": threshold,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "flagged": tp + fp,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the Sentinel risk model")
    parser.add_argument("--samples", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=20_261_124)
    parser.add_argument("--out", type=Path, default=DEFAULT_MODEL_PATH)
    args = parser.parse_args()

    data = build_dataset(args.samples, args.seed)
    cut = int(len(data) * 0.7)
    train, test = data[:cut], data[cut:]

    mean, std = standardise(train)
    weights, bias = train_logistic(train, mean, std)

    train_scores = _scores(train, weights, bias, mean, std)
    test_scores = _scores(test, weights, bias, mean, std)
    train_labels = [s.label for s in train]
    test_labels = [s.label for s in test]

    thresholds = {"watch": 0.4, "defend": 0.65, "critical": 0.85}
    metrics = {
        "auc_train": round(roc_auc(train_labels, train_scores), 4),
        "auc_test": round(roc_auc(test_labels, test_scores), 4),
        "positive_rate_test": round(sum(test_labels) / len(test_labels), 4),
        "bands_test": {name: band_metrics(test_labels, test_scores, value) for name, value in thresholds.items()},
    }

    payload = {
        "version": 1,
        "feature_names": FEATURE_NAMES,
        "weights": [round(w, 6) for w in weights],
        "bias": round(bias, 6),
        "mean": [round(m, 4) for m in mean],
        "std": [round(s, 4) for s in std],
        "thresholds": thresholds,
        "metrics": metrics,
        "metadata": {
            "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seed": args.seed,
            "samples": args.samples,
            "generator": "constant-product pool; 20% crash (20% of which fail), 22% fakeout, 13% chop, 45% calm",
            "label": "price falls >= 300 bps within 15 ticks after the decision point",
            "reason_codes": {name: "0x" + keccak32(label).hex() for name, label in REASON_LABELS.items()},
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"wrote {args.out}")
    print(json.dumps({k: v for k, v in payload.items() if k in ("weights", "bias", "metrics")}, indent=2))


if __name__ == "__main__":
    main()
