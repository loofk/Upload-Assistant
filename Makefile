.PHONY: help lint test check smoke smoke-ptcli smoke-legacy test-live

PYTHON ?= python3

help: ## 查看所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

lint: ## 运行 ruff 代码检查
	$(PYTHON) -m ruff check --config pyproject.toml .

test: ## 运行 pytest（排除 live 标记的测试）
	$(PYTHON) -m pytest

check: lint test ## 运行 lint + test

smoke: smoke-ptcli ## 快速导入检查（默认验证聚焦版 PT CLI）

smoke-legacy: ## 迁移期 legacy/full UA 导入检查
	$(PYTHON) -c "from src.args import Args; print('✓ args')"
	$(PYTHON) -c "from src.prep import Prep; print('✓ prep')"
	$(PYTHON) -c "from src.trackersetup import TRACKER_SETUP; print('✓ trackersetup')"
	$(PYTHON) -c "from src.trackerhandle import process_trackers; print('✓ trackerhandle')"
	@echo "Legacy/full UA smoke checks passed."

smoke-ptcli: ## 快速导入检查（验证聚焦版 PT CLI 可正常导入）
	$(PYTHON) -c "from src.ptcli.cli import build_parser, main; build_parser(); print('✓ ptcli.cli')"
	$(PYTHON) -c "from src.ptcli.mainland import CHINESE_PT_TRACKERS; assert 'MTEAM' in CHINESE_PT_TRACKERS; print('✓ ptcli.mainland')"
	$(PYTHON) -c "from src.ptcli.source import fetch_source_info; from src.ptcli.target import build_mteam_upload_preflight; print('✓ ptcli.source/target')"
	@echo "PT CLI smoke checks passed."

test-live: ## 运行实时集成测试（需要 data/cookies + data/config.py）
	$(PYTHON) -m pytest -m live -v --tb=short
