"""The risk model: a small, inspectable logistic regression over pool features.

Why logistic regression instead of a neural net or an LLM call in the hot path?

* It is deterministic, so a judge (or an auditor) can recompute any on-chain decision offline.
* Inference costs microseconds, so the model never becomes the slow part of the loop; the latency budget belongs
  to the cross-chain hop, not to the model.
* It is a model, not a rule: the weights are *learned* by `train_model.py` from a simulated market and the
  decision boundary moves with the data, which is what the "Reactive Meets AI" track asks for — AI inside the
  running system, not AI as a code generator.

The trained artefact lives in `agent/models/risk_model.json`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .features import FEATURE_NAMES

DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "models" / "risk_model.json"

#: Hard floors demanded by `RiskOracle` (basis points). A severity is only ever published above its floor, so a
#: judgement can never be rejected on chain for being inconsistent with itself.
ORACLE_FLOORS_BPS = {1: 4_000, 2: 6_500, 3: 8_500}

#: Reason codes written on chain for the dominant contributing feature. keccak256 of these strings is what the
#: contracts store, and the README documents the mapping.
REASON_LABELS = {
    "price_drop_bps": "PRICE_DROP",
    "ret_short_bps": "SHORT_RETURN",
    "liquidity_drop_bps": "LIQUIDITY_DRAIN",
    "sell_pressure_bps": "SELL_PRESSURE",
    "volatility_bps": "VOLATILITY_SPIKE",
    "twap_deviation_bps": "TWAP_DEVIATION",
}


def keccak32(text: str) -> bytes:
    from eth_utils import keccak

    return keccak(text=text)


@dataclass
class RiskModel:
    weights: list[float]
    bias: float
    mean: list[float]
    std: list[float]
    thresholds: Dict[str, float]
    feature_names: list[str]
    metrics: Dict[str, float]
    metadata: Dict[str, object]

    # -- loading --------------------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "RiskModel":
        payload = json.loads(Path(path or DEFAULT_MODEL_PATH).read_text())
        return cls(
            weights=payload["weights"],
            bias=payload["bias"],
            mean=payload["mean"],
            std=payload["std"],
            thresholds=payload["thresholds"],
            feature_names=payload["feature_names"],
            metrics=payload.get("metrics", {}),
            metadata=payload.get("metadata", {}),
        )

    # -- inference ------------------------------------------------------------------------------
    def _z(self, features: Dict[str, float]) -> list[float]:
        return [(features[name] - mean) / std for name, mean, std in zip(self.feature_names, self.mean, self.std)]

    def logit(self, features: Dict[str, float]) -> float:
        z = self._z(features)
        return self.bias + sum(w * zi for w, zi in zip(self.weights, z))

    def score(self, features: Dict[str, float]) -> float:
        """Calibrated risk probability in [0, 1]."""
        value = self.logit(features)
        if value >= 0:
            return 1.0 / (1.0 + math.exp(-value))
        exp = math.exp(value)
        return exp / (1.0 + exp)

    def score_bps(self, features: Dict[str, float]) -> int:
        return int(round(self.score(features) * 10_000))

    # -- decision -------------------------------------------------------------------------------
    def severity(self, score_bps: int) -> int:
        """Maps a score to the severity ladder, honouring both the model's calibrated thresholds and the
        oracle's hard floors."""
        severity = 0
        for name, level in (("watch", 1), ("defend", 2), ("critical", 3)):
            threshold = max(self.thresholds.get(name, 1.0) * 10_000, ORACLE_FLOORS_BPS[level])
            if score_bps >= threshold:
                severity = level
        return severity

    def contributions(self, features: Dict[str, float]) -> Dict[str, float]:
        """Signed contribution of each feature to the logit, i.e. how much it pushed the decision."""
        z = self._z(features)
        return {name: w * zi for name, w, zi in zip(self.feature_names, self.weights, z)}

    def dominant_feature(self, features: Dict[str, float]) -> str:
        contributions = {name: value for name, value in self.contributions(features).items() if value > 0}
        if not contributions:
            return "price_drop_bps"
        return max(contributions.items(), key=lambda item: item[1])[0]

    def reason_code(self, features: Dict[str, float]) -> bytes:
        return keccak32(REASON_LABELS[self.dominant_feature(features)])

    def explain(self, features: Dict[str, float]) -> dict:
        score = self.score(features)
        contributions = self.contributions(features)
        ordered = sorted(contributions.items(), key=lambda item: abs(item[1]), reverse=True)
        return {
            "score": score,
            "score_bps": self.score_bps(features),
            "severity": self.severity(self.score_bps(features)),
            "dominant_feature": self.dominant_feature(features),
            "reason": REASON_LABELS[self.dominant_feature(features)],
            "top_contributions": [
                {"feature": name, "z_contribution": round(value, 4)} for name, value in ordered[:3]
            ],
            "features": {name: round(value, 2) for name, value in features.items()},
        }


def load_default() -> RiskModel:
    return RiskModel.load()
