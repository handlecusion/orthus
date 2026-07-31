.PHONY: install up down nuke migrate seed test test-db-setup nuke-test demo-assistant api web dev-up dev-down fmt docs-check kg-bootstrap kg-rebuild kg-sync node-kg-bootstrap node-kg-rebuild node-kg-sync node-kg-entity-normalize node-wiki-backfill-headline node-kg-erase node-kg-erase-owner node-kg-erase-owners kg-cross-scope-inventory kg-monitor node-kg-monitor node-kg-monitor-scheduler-install kg-test-up pg-test-up pg-test-down test-fast wiki-rebuild wiki-reembed wiki-eval decompose-measure node-decompose-measure ask-cache-gc node-ask-cache-gc slack-structured-backfill slack-raw-structured-refresh import-notion node-bootstrap node-migrate node-api node-web node-import-notion node-sync node-slack-structured-backfill node-slack-raw-structured-refresh node-sync-due node-sync-cycle node-scheduler-install node-scheduler-uninstall node-wiki-rebuild node-wiki-reembed node-purge-board-wiki node-wiki-eval node-golden-eval node-smoke node-auth-allowlist collector-token compile-personal mail-pull-ingest mcp-smoke p8-export-personal p8-import-personal pr

install:
	uv sync --extra dev

up:
	docker compose up -d
	@echo "waiting for postgres healthy..."
	@until [ "$$(docker inspect -f '{{.State.Health.Status}}' orthus_pg 2>/dev/null)" = "healthy" ]; do sleep 1; done
	@echo "postgres ready on localhost:5433"
	@echo "waiting for neo4j healthy..."
	@until [ "$$(docker inspect -f '{{.State.Health.Status}}' orthus_neo4j 2>/dev/null)" = "healthy" ]; do sleep 1; done
	@echo "neo4j ready on 127.0.0.1:7687 (bolt) / 127.0.0.1:7474 (http)"

down:
	docker compose down

nuke:
	docker compose down -v

migrate:
	uv run alembic upgrade head

seed:
	uv run python -m seeds.dev.seed

test-db-setup:
	bash scripts/setup_test_db.sh

test: test-db-setup
	ORTHUS_PG_DSN=postgresql+psycopg://orthus:orthus@localhost:5433/orthus_test \
	ORTHUS_PG_DSN_READONLY=postgresql+psycopg://orthus_ro:orthus_ro@localhost:5433/orthus_test \
	ORTHUS_EMBEDDING=mock ORTHUS_LLM=mock ORTHUS_MODEL_ORCHESTRATION_ENABLED=false uv run pytest

nuke-test:
	docker exec orthus_pg psql -U orthus -d postgres -c "DROP DATABASE IF EXISTS orthus_test;"

demo-assistant:
	uv run python -m seeds.dev.demo

# KG (K-series, docs/kg-model.md §1). kg-* root target은 root .env 기준의
# local dev 전용이다. prod central은 company 데이터가 node DB(orthus_company)에
# 있으므로 반드시 node-kg-* 변형을 쓴다 — root/node env를 섞으면 두 SoR이 같은
# 그래프에 쓰여 rebuild prune이 상대 쪽을 삭제한다 (docs/operations.md §2.1).
kg-bootstrap:
	uv run python -m orthus.kg.bootstrap

kg-rebuild:
	uv run python -m orthus.kg.rebuild

kg-sync:
	uv run python -m orthus.kg.sync

node-kg-bootstrap:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-kg-bootstrap NODE=company"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.kg.bootstrap

node-kg-rebuild:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-kg-rebuild NODE=company"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.kg.rebuild

node-kg-sync:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-kg-sync NODE=company"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.kg.sync

# E-N1b — entity 정규화 backfill(내부 문서(비공개)). 기존 적재된
# entity의 Slack 표기/불투명 ID를 E-N1a 규칙으로 재정규화·병합·드롭한다(LLM 0회, 멱등).
# 기본은 DRY-RUN(카운트만, 쓰기 0). APPLY=1일 때만 실제 쓰기(APPLY=0 등 다른 값은 dry-run).
# apply 후 반드시 `make node-kg-rebuild NODE=company`로 Neo4j 재투영(full-rebuild-only, E-N1c).
node-kg-entity-normalize:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-kg-entity-normalize NODE=company [APPLY=1]"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.kg.entity_normalize $(if $(filter 1,$(APPLY)),--apply,--dry-run)

# claim display_title(사람이 읽는 헤드라인) 백필. USER_ID=<uuid> 필수(저작 소유자).
# DRY_RUN=1이면 대상만 세고 쓰지 않음(LLM 미호출). NO_LLM=1이면 결정론 폴백만.
# LIMIT=<n>으로 소규모 확인. CONCURRENCY=<n>은 LLM headline 생성 동시성(쓰기 직렬).
# 백필 후 `make node-kg-rebuild NODE=company`로 재투영.
# (변수명은 USER_ID — make의 ambient `$USER`(셸 사용자명)와 충돌하지 않게.)
node-wiki-backfill-headline:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-wiki-backfill-headline NODE=company USER_ID=<uuid>"; exit 2; fi
	@if [ -z "$(USER_ID)" ]; then echo "USER_ID required (authoring user_id), e.g. make node-wiki-backfill-headline NODE=company USER_ID=<uuid> [DRY_RUN=1] [LIMIT=n] [NO_LLM=1]"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.wiki.backfill_claim_headline \
		--user-id "$(USER_ID)" \
		$(if $(DRY_RUN),--dry-run,) \
		$(if $(NO_LLM),--no-llm,) \
		$(if $(LIMIT),--limit $(LIMIT),) \
		$(if $(CONCURRENCY),--concurrency $(CONCURRENCY),)

# K7.3 §4.6 — cross-scope edge inventory (머지 전 게이트 아티팩트). PG-side 집계라
# KG flag/Neo4j 무관. OWNER=<uuid> 지정 시 단일 owner, 미지정 시 회사-집계만.
kg-cross-scope-inventory:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make kg-cross-scope-inventory NODE=company [OWNER=<uuid>]"; exit 2; fi
	OWNER="$(OWNER)" bash scripts/node/run_python.sh "$(NODE)" -m orthus.kg.inventory

# K6 — 운영자 erasure 전파(operations.md §8.4). PAGES는 공백 구분 wiki page id 목록.
node-kg-erase:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-kg-erase NODE=company PAGES=\"<page_id> ...\""; exit 2; fi
	@if [ -z "$(PAGES)" ]; then echo "PAGES required, e.g. make node-kg-erase NODE=company PAGES=\"<page_id> ...\""; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.kg.erase $(PAGES)

# K7.5 — owner-scope KG erasure(그래프 표시 제거; 실제 PG 삭제는 P8). node env 래퍼 필수 —
# root .env로 직접 돌리면 dev DB를 가리켜 SoR-mixing(operations.md §2.1). 실행은
# --confirm <OWNER> 정확 일치가 필요하고, DRY_RUN=1이면 footprint만 보고 삭제 0.
node-kg-erase-owner:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-kg-erase-owner NODE=company OWNER=<id>"; exit 2; fi
	@if [ -z "$(OWNER)" ]; then echo "OWNER required, e.g. make node-kg-erase-owner NODE=company OWNER=<id> [DRY_RUN=1]"; exit 2; fi
	@if [ -n "$(DRY_RUN)" ]; then \
		bash scripts/node/run_python.sh "$(NODE)" -m orthus.kg.erase --owner "$(OWNER)" --dry-run; \
	else \
		bash scripts/node/run_python.sh "$(NODE)" -m orthus.kg.erase --owner "$(OWNER)" --confirm "$(OWNER)"; \
	fi

# K7.5 (M-r2) — 배치 owner erasure(단일 HOLD 윈도우). OWNERS_FILE은 owner-id 한 줄당 하나.
node-kg-erase-owners:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-kg-erase-owners NODE=company OWNERS_FILE=<f>"; exit 2; fi
	@if [ -z "$(OWNERS_FILE)" ]; then echo "OWNERS_FILE required, e.g. make node-kg-erase-owners NODE=company OWNERS_FILE=<f> [DRY_RUN=1]"; exit 2; fi
	@if [ -n "$(DRY_RUN)" ]; then \
		bash scripts/node/run_python.sh "$(NODE)" -m orthus.kg.erase --owners-file "$(OWNERS_FILE)" --dry-run; \
	else \
		bash scripts/node/run_python.sh "$(NODE)" -m orthus.kg.erase --owners-file "$(OWNERS_FILE)" --confirm-batch; \
	fi

# K6 — read-only 운영 요약(outbox/query_runs/KgMeta/graph). 신규 route 없음.
kg-monitor:
	uv run python -m orthus.kg.monitor

node-kg-monitor:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-kg-monitor NODE=company"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.kg.monitor

# K7.5 (W3) — launchd 주기 스케줄로 KG boundary/health monitor cycle 설치(Mac mini operator).
# plist 존재 = 활성화 acceptance artifact. INTERVAL_SECONDS 기본 3600(1h).
node-kg-monitor-scheduler-install:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-kg-monitor-scheduler-install NODE=company [INTERVAL_SECONDS=3600]"; exit 2; fi
	bash scripts/node/install_kg_monitor_scheduler.sh "$(NODE)" "$(INTERVAL_SECONDS)"

kg-test-up:
	docker compose --profile test up -d neo4j-test

# 테스트 전용 tmpfs PG(:5434). 하네스가 테스트마다 전 테이블 TRUNCATE를 돌려
# 디스크 볼륨의 orthus_pg에서는 fsync 대기로 전체 스위트가 ~4시간이 된다.
# `make test-fast`가 이 컨테이너를 기동/부트스트랩하고 스위트를 돌린다.
pg-test-up:
	docker compose --profile test up -d --wait postgres-test
	PG_CONTAINER=orthus_pg_test PG_PORT=5434 bash scripts/setup_test_db.sh

pg-test-down:
	docker compose --profile test down postgres-test

# tmpfs PG에서 전체 스위트 실행. `make test`와 동일한 mock 환경/게이트이며
# DB만 :5434 throwaway를 쓴다. 컨테이너를 내리면 데이터는 전부 사라진다.
test-fast: pg-test-up
	ORTHUS_PG_DSN=postgresql+psycopg://orthus:orthus@localhost:5434/orthus_test \
	ORTHUS_PG_DSN_READONLY=postgresql+psycopg://orthus_ro:orthus_ro@localhost:5434/orthus_test \
	PG_CONTAINER=orthus_pg_test \
	ORTHUS_EMBEDDING=mock ORTHUS_LLM=mock ORTHUS_MODEL_ORCHESTRATION_ENABLED=false uv run pytest

wiki-rebuild:
	uv run python -m orthus.wiki.rebuild

wiki-reembed:
	uv run python -m orthus.wiki.reembed

wiki-eval:
	uv run python -m orthus.wiki.eval_retrieval

decompose-measure:
	uv run python -m orthus.router.decompose_measure $(ARGS)

node-decompose-measure:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-decompose-measure NODE=company ARGS=--from-audit"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.router.decompose_measure $(ARGS)

ask-cache-gc:   # ask_cache TTL/stale 행 GC (MA.7c, docs/company-agent-orchestration.md §P3A.10)
	uv run python -m orthus.router.cache gc

node-ask-cache-gc:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-ask-cache-gc NODE=company"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.router.cache gc

slack-structured-backfill:
	uv run python -m orthus.structured.slack_backfill

slack-raw-structured-refresh:
	uv run python -m orthus.structured.slack_refresh

import-notion:
	uv run python -m orthus.connectors.notion_import

api:
	uv run uvicorn orthus.api.main:app --reload --host 127.0.0.1 --port 8820

web:
	cd web && pnpm install && pnpm dev --hostname 127.0.0.1 --port 3820

# 로컬 dev 원클릭: 컨테이너 + API/web을 detached tmux로 띄우고 ready까지 대기.
# auth URL override는 .env가 prod public 주소를 갖고 있어도 demo 모드 로컬
# 기동이 가능하게 하는 dev 전용 우회다. 서버 로그: tmux attach -t orthus-api|-web
dev-up: up
	@tmux has-session -t orthus-api 2>/dev/null || tmux new-session -d -s orthus-api -c "$(CURDIR)" \
		'ORTHUS_AUTH_PUBLIC_BASE_URL=http://localhost:8820 ORTHUS_AUTH_WEB_BASE_URL=http://localhost:3820 make api'
	@tmux has-session -t orthus-web 2>/dev/null || tmux new-session -d -s orthus-web -c "$(CURDIR)" 'make web'
	@echo "waiting for api on :8820..."
	@until curl -fsS http://localhost:8820/auth/config >/dev/null 2>&1; do sleep 2; done
	@echo "waiting for web on :3820..."
	@until curl -fsS -o /dev/null http://localhost:3820 2>/dev/null; do sleep 2; done
	@echo "dev ready: http://localhost:3820 (api http://localhost:8820)"

dev-down:
	-tmux kill-session -t orthus-api 2>/dev/null || true
	-tmux kill-session -t orthus-web 2>/dev/null || true
	-fuser -ks 8820/tcp 2>/dev/null || true
	-fuser -ks 3820/tcp 2>/dev/null || true
	@echo "dev servers stopped (containers still up — stop with make down)"

fmt:
	uv run ruff format orthus tests seeds scripts
	uv run ruff check --fix orthus tests seeds scripts

docs-check:
	uv run python scripts/check_docs_spec.py

node-bootstrap:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-bootstrap NODE=company"; exit 2; fi
	bash scripts/node/bootstrap_db.sh "$(NODE)"

node-migrate:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-migrate NODE=personal-a"; exit 2; fi
	bash scripts/node/migrate_db.sh "$(NODE)"

node-api:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-api NODE=company"; exit 2; fi
	ORTHUS_NODE_ENV="$(ORTHUS_NODE_ENV)" ORTHUS_NODE_PROFILE="$(ORTHUS_NODE_PROFILE)" bash scripts/node/run_api.sh "$(NODE)" "$(PORT)"

node-web:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-web NODE=personal-a"; exit 2; fi
	ORTHUS_NODE_ENV="$(ORTHUS_NODE_ENV)" ORTHUS_NODE_PROFILE="$(ORTHUS_NODE_PROFILE)" bash scripts/node/run_web.sh "$(NODE)" "$(WEB_PORT)"

node-import-notion:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-import-notion NODE=company"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.connectors.notion_import

node-sync:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-sync NODE=personal-a CONNECTOR=local_files"; exit 2; fi
	@if [ -z "$(CONNECTOR)" ]; then echo "CONNECTOR required, e.g. make node-sync NODE=personal-a CONNECTOR=local_files"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.connectors.sync "$(CONNECTOR)"

node-slack-structured-backfill:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-slack-structured-backfill NODE=company"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.structured.slack_backfill

node-slack-raw-structured-refresh:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-slack-raw-structured-refresh NODE=company"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.structured.slack_refresh

node-sync-due:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-sync-due NODE=personal-a CONNECTORS='gws_gmail gws_drive'"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.connectors.tick $(CONNECTORS)

node-sync-cycle:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-sync-cycle NODE=personal-a CONNECTORS='gws_gmail gws_drive codex_sessions claude_sessions'"; exit 2; fi
	bash scripts/node/sync_cycle.sh "$(NODE)" $(CONNECTORS)

node-scheduler-install:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-scheduler-install NODE=personal-a"; exit 2; fi
	bash scripts/node/install_launchd_scheduler.sh "$(NODE)" "$(INTERVAL_SECONDS)"

node-scheduler-uninstall:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-scheduler-uninstall NODE=personal-a"; exit 2; fi
	bash scripts/node/uninstall_launchd_scheduler.sh "$(NODE)"

node-wiki-rebuild:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-wiki-rebuild NODE=personal-a"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.wiki.rebuild

node-wiki-reembed:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-wiki-reembed NODE=company"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.wiki.reembed $(ARGS)

# Remove stale personal-board journal todo/event claims from the LLM wiki
# (owner-scoped). Dry-run by default; APPLY=1 writes. See 내부 문서(비공개)
node-purge-board-wiki:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-purge-board-wiki NODE=company [OWNER=<uuid>] [APPLY=1] [NO_REGEN=1]"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m scripts.wiki.purge_personal_board_wiki \
		$(if $(OWNER),--owner "$(OWNER)") $(if $(APPLY),--apply) $(if $(NO_REGEN),--no-regen)

node-wiki-eval:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-wiki-eval NODE=company"; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.wiki.eval_retrieval $(ARGS)

node-golden-eval:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-golden-eval NODE=company ARGS=\"--cases path/to/golden.jsonl\""; exit 2; fi
	bash scripts/node/run_python.sh "$(NODE)" -m orthus.wiki.eval_qa $(ARGS)

node-smoke:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-smoke NODE=company"; exit 2; fi
	bash scripts/node/smoke.sh "$(NODE)"

node-auth-allowlist:
	@if [ -z "$(NODE)" ]; then echo "NODE required, e.g. make node-auth-allowlist NODE=company ACTION=list"; exit 2; fi
	@if [ -z "$(ACTION)" ]; then echo "ACTION required: list or upsert"; exit 2; fi
	@ACTION="$(ACTION)" EMAIL="$(EMAIL)" EMAIL_FILE="$(EMAIL_FILE)" ROLE="$(ROLE)" bash scripts/node/auth_allowlist.sh "$(NODE)"

collector-token:
	@if [ -z "$(USER_EMAIL)$(USER_ID)" ]; then echo "USER_EMAIL or USER_ID required, e.g. make collector-token NODE=company USER_EMAIL=owner@acme.example NAME=mac-mini"; exit 2; fi
	@uv run python scripts/ops/issue_collector_token.py $(if $(NODE),--node "$(NODE)") $(if $(USER_EMAIL),--user-email "$(USER_EMAIL)") $(if $(USER_ID),--user-id "$(USER_ID)") $(if $(NAME),--name "$(NAME)")

compile-personal:
	@if [ -z "$(USER_EMAIL)$(USER_ID)" ]; then echo "USER_EMAIL or USER_ID required, e.g. make compile-personal USER_EMAIL=owner@acme.example"; exit 2; fi
	@uv run python scripts/ops/compile_personal_docs.py $(if $(USER_EMAIL),--user-email "$(USER_EMAIL)") $(if $(USER_ID),--user-id "$(USER_ID)") $(if $(LIMIT),--limit "$(LIMIT)")

mail-pull-ingest:
	@uv run python scripts/ops/mail_pull_ingest.py

mcp-smoke:
	uv run --extra mcp python scripts/mcp/stdio_smoke.py

p8-export-personal:
	@if [ -z "$(DSN)" ]; then echo "DSN required, e.g. make p8-export-personal DSN=postgresql+psycopg://orthus:orthus@localhost:5433/orthus_personal NODE_ID=personal-a USER_ID=<uuid> OUT=/tmp/personal-bundle"; exit 2; fi
	@if [ -z "$(USER_ID)" ]; then echo "USER_ID required (owner user id on the personal node)"; exit 2; fi
	@if [ -z "$(NODE_ID)" ]; then echo "NODE_ID required (source personal node id, e.g. personal-a)"; exit 2; fi
	@if [ -z "$(OUT)" ]; then echo "OUT required (bundle output directory)"; exit 2; fi
	uv run python -m scripts.migration.export_personal_node --dsn "$(DSN)" --node-id "$(NODE_ID)" --user-id "$(USER_ID)" --out "$(OUT)" $(if $(ALLOW_LEGACY_NULL_OWNER_AGENT_WORK),--allow-legacy-null-owner-agent-work)

p8-import-personal:
	@if [ -z "$(BUNDLE)" ]; then echo "BUNDLE required, e.g. make p8-import-personal BUNDLE=/tmp/personal-bundle USER_ID=<central-uuid> COMPILE=1"; exit 2; fi
	uv run python -m scripts.migration.import_personal_bundle --bundle "$(BUNDLE)" $(if $(USER_ID),--user-id "$(USER_ID)") $(if $(NODE_ID),--node-id "$(NODE_ID)") $(if $(COMPILE),--compile)

pr:  ## Open a PR with the checklist template seeded: make pr T="[P6.7] title" [BASE=main]
	@if [ -z "$(T)" ]; then echo "T (title) required, e.g. make pr T='[P6.7] mail multi-account UI'"; exit 2; fi
	@git push -u origin HEAD && \
	gh pr create --title "$(T)" --base "$(if $(BASE),$(BASE),main)" --body-file .github/pull_request_template.md && \
	echo "PR opened — fill Risk / Protected Area / QA Evidence in the body"
