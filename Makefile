.PHONY: help lint test check smoke test-live

PYTHON ?= python3

help: ## 查看所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

lint: ## 运行 ruff 代码检查
	$(PYTHON) -m ruff check --config pyproject.toml .

test: ## 运行 pytest（排除 live 标记的测试）
	$(PYTHON) -m pytest

check: lint test ## 运行 lint + test

smoke: ## 快速导入检查（验证核心模块可正常导入）
	$(PYTHON) -c "from src.args import Args; print('✓ args')"
	$(PYTHON) -c "from src.prep import Prep; print('✓ prep')"
	$(PYTHON) -c "from src.trackersetup import TRACKER_SETUP; print('✓ trackersetup')"
	$(PYTHON) -c "from src.trackerhandle import process_trackers; print('✓ trackerhandle')"
	@echo "All smoke checks passed."

test-live: ## 运行实时集成测试（需要 data/cookies + data/config.py）
	$(PYTHON) -m pytest -m live -v --tb=short
