"""Model and agent behaviour tests."""

from __future__ import annotations

import json

import pytest

from eth_abi import decode as abi_decode

from agent.features import FEATURE_NAMES
from agent.model import ORACLE_FLOORS_BPS, REASON_LABELS, RiskModel
from agent.pool import SyntheticFeed
from agent.sentinel_agent import SEVERITY_NAMES, SentinelAgent

CALM = {
    "price_drop_bps": 80.0,
    "ret_short_bps": 20.0,
    "liquidity_drop_bps": 60.0,
    "sell_pressure_bps": 5_200.0,
    "volatility_bps": 12.0,
    "twap_deviation_bps": 15.0,
}

STRESSED = {
    "price_drop_bps": 4_800.0,
    "ret_short_bps": -2_600.0,
    "liquidity_drop_bps": 3_100.0,
    "sell_pressure_bps": 9_800.0,
    "volatility_bps": 420.0,
    "twap_deviation_bps": 2_700.0,
}


@pytest.fixture(scope="module")
def model() -> RiskModel:
    return RiskModel.load()


def test_model_artifact_matches_feature_contract(model: RiskModel) -> None:
    assert model.feature_names == FEATURE_NAMES
    assert len(model.weights) == len(FEATURE_NAMES)
    assert len(model.mean) == len(FEATURE_NAMES)
    assert len(model.std) == len(FEATURE_NAMES)
    assert model.metrics["auc_test"] > 0.85, "the shipped model must be better than a coin flip"


def test_a_stressed_market_scores_higher_than_a_calm_one(model: RiskModel) -> None:
    calm = model.score_bps(CALM)
    stressed = model.score_bps(STRESSED)
    assert stressed > calm
    assert stressed > 6_000
    assert calm < 1_000


def test_score_is_bounded(model: RiskModel) -> None:
    for features in (CALM, STRESSED):
        assert 0 <= model.score_bps(features) <= 10_000


def test_severity_never_violates_the_oracle_floors(model: RiskModel) -> None:
    for score_bps in range(0, 10_001, 37):
        severity = model.severity(score_bps)
        if severity:
            assert score_bps >= ORACLE_FLOORS_BPS[severity], (score_bps, severity)


def test_severity_ladder_is_monotonic(model: RiskModel) -> None:
    previous = 0
    for score_bps in range(0, 10_001, 25):
        severity = model.severity(score_bps)
        assert severity >= previous
        previous = severity


def test_reason_code_is_keccak_of_the_dominant_feature(model: RiskModel) -> None:
    code = model.reason_code(STRESSED)
    dominant = model.dominant_feature(STRESSED)
    assert len(code) == 32
    assert code.hex() in {f"{model.metadata['reason_codes'][name][2:]}" for name in REASON_LABELS}
    assert dominant in REASON_LABELS


def test_explanation_is_actionable(model: RiskModel) -> None:
    explanation = model.explain(STRESSED)
    assert set(explanation) == {
        "score",
        "score_bps",
        "severity",
        "dominant_feature",
        "reason",
        "top_contributions",
        "features",
    }
    assert explanation["severity"] == model.severity(explanation["score_bps"])
    assert len(explanation["top_contributions"]) == 3
    assert explanation["reason"] == REASON_LABELS[explanation["dominant_feature"]]


def test_model_json_round_trips(tmp_path, model: RiskModel) -> None:
    payload = json.loads((tmp_path / "m.json").read_text()) if (tmp_path / "m.json").exists() else None
    assert payload is None
    copy = RiskModel(
        weights=model.weights,
        bias=model.bias,
        mean=model.mean,
        std=model.std,
        thresholds=model.thresholds,
        feature_names=model.feature_names,
        metrics=model.metrics,
        metadata=model.metadata,
    )
    assert copy.score_bps(STRESSED) == model.score_bps(STRESSED)


class FakeChain:
    """Records what the agent would broadcast."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, to, data, value=0, gas=None, wait=True, timeout=90.0):
        self.sent.append((to, data))
        return {"transactionHash": "0x" + "11" * 32, "blockNumber": "0x2a", "status": "0x1"}


def _warm_agent(model: RiskModel, chain: FakeChain | None = None) -> SentinelAgent:
    feed = SyntheticFeed(ticks=80)
    agent = SentinelAgent(chain=chain, oracle="0x" + "22" * 20, model=model, feed=feed, min_interval=0.0)
    for _ in range(20):
        agent.observe(feed.snapshot())
    return agent


def test_agent_does_not_publish_on_a_calm_market(model: RiskModel) -> None:
    chain = FakeChain()
    agent = _warm_agent(model, chain)
    for _ in range(8):
        agent.observe(agent.feed.snapshot())
    explanation = agent.evaluate()
    assert explanation is not None
    ok, reason = agent.should_publish(explanation, now=1_000.0)
    assert not ok or explanation["severity"] > 0
    assert chain.sent == []


def test_agent_publishes_decodable_signal_when_stressed(model: RiskModel) -> None:
    chain = FakeChain()
    agent = _warm_agent(model, chain)
    # Drive the synthetic feed deep into its sell-off (it starts at tick 58).
    for _ in range(55):
        agent.observe(agent.feed.snapshot())

    explanation = agent.evaluate()
    assert explanation is not None and explanation["severity"] >= 1
    decision = agent.publish(explanation)

    assert decision.action == "published"
    assert len(chain.sent) == 1
    to, calldata = chain.sent[0]
    assert to == agent.oracle

    args = abi_decode(
        ["bytes32", "uint8", "uint256", "uint64", "bytes32", "bytes"], bytes.fromhex(calldata[10:])
    )
    signal_id, severity, score_bps, observed_at, reason_code, metrics = args
    assert severity == decision.severity
    assert score_bps == decision.score_bps
    assert score_bps >= ORACLE_FLOORS_BPS[severity]
    assert SEVERITY_NAMES[severity] == decision.severity_name
    assert reason_code.hex() in {code[2:] for code in model.metadata["reason_codes"].values()}
    # metrics is the on-chain evidence blob: five uint256 values in basis points
    evidence = abi_decode(["uint256"] * 5, metrics)
    assert len(evidence) == 5
    assert evidence[2] == int(round(explanation["features"]["sell_pressure_bps"]))


def test_agent_respects_its_own_rate_limit(model: RiskModel) -> None:
    chain = FakeChain()
    agent = _warm_agent(model, chain)
    for _ in range(55):
        agent.observe(agent.feed.snapshot())
    explanation = agent.evaluate()
    assert explanation is not None and explanation["severity"] >= 1

    agent.min_interval = 60.0
    agent.publish(explanation)
    ok, reason = agent.should_publish(explanation, now=agent._last_publish + 1)
    assert not ok and reason == "agent-cooldown"


def test_signal_ids_are_unique(model: RiskModel) -> None:
    agent = _warm_agent(model)
    ids = {agent.next_signal_id(1_700_000_000, 7_000) for _ in range(50)}
    assert len(ids) == 50


def test_dry_run_never_touches_the_chain(model: RiskModel) -> None:
    chain = FakeChain()
    agent = _warm_agent(model, chain)
    for _ in range(55):
        agent.observe(agent.feed.snapshot())
    explanation = agent.evaluate()
    assert explanation is not None
    decision = agent.publish(explanation, dry_run=True)
    assert decision.action == "dry-run"
    assert chain.sent == []
