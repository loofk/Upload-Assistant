.PHONY: help lint lint-ptcli test test-ptcli test-legacy check check-ptcli verify-ptcli-local verify-ptcli-seedbox-handoff smoke smoke-ptcli smoke-legacy agent-skills-check agent-skills-sync test-live go-fmt go-lint go-test go-build go-check go-compose-config

PYTHON ?= python3
GO ?= go

help: ## 查看所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

lint: ## 运行 ruff 代码检查
	$(PYTHON) -m ruff check --config pyproject.toml .

lint-ptcli: ## 只检查 focused PTCLI v1 代码和验收脚本
	$(PYTHON) -m ruff check --config pyproject.toml ptcli.py src/ptcli tests/ptcli_v1 scripts/sync_agent_skill_templates.py scripts/verify_ptcli_local_ready.py

test: test-ptcli ## 默认运行快速、无网络的 focused PTCLI v1 测试

test-ptcli: ## 运行 focused PTCLI v1 LOCAL_READY 测试
	$(PYTHON) -m pytest -q tests/ptcli_v1

test-legacy: ## 运行 legacy/full UA 测试；需安装 requirements-legacy-dev.txt
	$(PYTHON) -m pytest -m 'not live' --ignore=tests/ptcli_v1

check: check-ptcli ## 默认执行 focused PTCLI v1 门禁

check-ptcli: lint-ptcli test-ptcli agent-skills-check ## focused lint + test + agent manifest 同步检查

verify-ptcli-local: check-ptcli ## 构建并启动 focused 镜像，生成 LOCAL_READY JSON 报告
	$(PYTHON) scripts/verify_ptcli_local_ready.py

verify-ptcli-seedbox-handoff: check-ptcli ## 构建并启动镜像，验证 compact 运维契约和非执行 live 交接包
	$(PYTHON) scripts/verify_ptcli_local_ready.py --level seedbox-handoff --report tmp/ptcli-seedbox-handoff-ready.json

agent-skills-check: ## 校验 OpenClaw/Hermes 静态 skill manifest 与服务契约同步
	$(PYTHON) scripts/sync_agent_skill_templates.py

agent-skills-sync: ## 生成/同步 OpenClaw/Hermes 静态 skill manifest
	$(PYTHON) scripts/sync_agent_skill_templates.py --write

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

go-fmt: ## 格式化 Go 源码
	gofmt -w cmd internal migrations

go-lint: ## 检查 Go 格式和静态问题
	test -z "$$(gofmt -l cmd internal migrations)"
	$(GO) vet ./...

go-test: ## 运行 Go 单元测试
	$(GO) test ./...

go-build: ## 构建 Go 服务
	$(GO) build -o tmp/upload-assistant-v2 ./cmd/upload-assistant

go-compose-config: ## 校验 Go/PostgreSQL Compose 配置
	docker compose -f docker-compose.go.yml config --quiet

go-check: go-lint go-test go-build ## 执行 Go 基础门禁
