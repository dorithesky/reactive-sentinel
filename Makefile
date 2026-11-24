SHELL := /bin/bash
PYTHON ?= python3
PYTEST ?= pytest

.PHONY: help build test test-solidity test-python loop loop-defend agent-dry train fmt clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

build: ## Compile contracts and scripts
	forge build

test: test-solidity test-python ## All tests

test-solidity: ## 48 Foundry tests
	forge test -vv

test-python: ## 21 pytest tests
	$(PYTEST) tests -q

loop: ## Full Sentinel loop on two local anvil chains
	$(PYTHON) tools/local_loop.py

loop-defend: ## Same, stopping at the severity-2 (safe mode) defence
	$(PYTHON) tools/local_loop.py --scenario defend

agent-dry: ## One offline decision from the scripted market
	$(PYTHON) -m agent.sentinel_agent --feed synthetic --dry-run --once --log /tmp/sentinel-dry.jsonl

train: ## Retrain the risk model (deterministic seed)
	$(PYTHON) -m agent.train_model

fmt: ## Format Solidity and Python
	forge fmt
	$(PYTHON) -m black agent tests tools 2>/dev/null || true

clean: ## Remove build artefacts
	forge clean
	rm -rf runs/agent-decisions-*.jsonl
