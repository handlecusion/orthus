# AGENTS.md

> **대회 빌드 (Solar 단일 백엔드).** 이 레포는 Upstage Solar를 유일한 LLM
> 백엔드로 쓰는 에이전틱 지식 시스템이다. 프로젝트 개요·아키텍처는
> [`README.md`](README.md), 측정 근거는
> [`experiments/fugu-ko/RESULTS.md`](experiments/fugu-ko/RESULTS.md) 참조.

## 프로젝트

**LLM wiki 아카식**(모든 지식의 단일 저장소 + 에이전트 self-authoring 그라운딩
레이어)과 **비서/라우터**(자연어로 묻고, 검증된 질의만 실행해 답하는 계층), 그리고
그 위의 **지식그래프(KG)·MCP/CLI 표면**.

- 아카식 = BlockNote 에디터 + 소스 커넥터 임포트 + corpus 인덱싱 + **LLM wiki**
  (distill→consolidate 자기저작).
- 비서/라우터 = 자연어 → 자동 라우터(`/ask`) → wiki grounding(지식형) 또는
  structured NL→SQL(집계형) 또는 KG graph 분기(관계형). 검증 게이트 + read-only 실행.
- 에이전틱 검색 = `ORTHUS_AGENTIC_ASK_ENABLED=true`일 때 /ask가 Solar 네이티브
  function-calling 루프로 결정론 도구(wiki 검색/grounded QA/structured 게이트/KG
  템플릿)를 오케스트레이션한다 (`orthus/router/agentic/`).
- 오케스트레이터 = agent-work 채팅(`POST /agent-work/chats/{id}/orchestrate`)이
  복합 질문 decompose·read→act·명령 intake를 수행하고, typed action은 결정론
  policy gate가 `auto_execute|draft_for_review|request_more_data|reject`로 분기.
- MCP = `orthus mcp serve` stdio 서버 (wiki_search/wiki_ask/wiki_page/kg_relations
  등) — 외부 에이전트(Claude Code/Codex 등)가 지식 베이스를 도구로 쓴다.
- CLI = `orthus` (wiki/ask/ticket/mcp 등).

데이터 흐름: `소스 → corpus(chunk→embed→pgvector, raw 레이어) → [distill→consolidate]
→ LLM wiki(claims+pages, 메모리 레이어) → /ask 그라운딩`, 그리고
`wiki → 결정론 투영 → Neo4j KG(:Page/:Claim/:Entity) → 템플릿 게이트 질의`.

## 모델 정책 (Solar 단일)

- chat/compile 슬롯: `ORTHUS_LLM=solar` (`solar-pro`, OpenAI 호환 프로토콜).
  `mock`은 결정론 오프라인 슬롯(테스트/CI 고정).
- embedding 슬롯: `ORTHUS_EMBEDDING=solar` (`embedding-passage`를 인덱스/질의
  **양쪽에 대칭** 사용 — 비대칭 query/passage 분리는 실측에서 4/4 조건 유의하게
  나빴다. 되돌리지 말 것. `experiments/fugu-ko/embedding/`).
- 작업별 배정은 `orthus/models/orchestration.py`의 **결정론 상수 테이블**(전 슬롯
  solar). 확신도 라우팅·학습 셀렉터 금지 — 실측에서 상수 테이블에 패배했다.
- SVC(구조화 검증 캐스케이드) 2차 모델은 같은 벤더의 다른 모델
  (`ORTHUS_LLM_FALLBACK_PROVIDER=solar`, `ORTHUS_LLM_FALLBACK_MODEL=solar-mini`).
- 키는 env로만: `ORTHUS_LLM_SOLAR_API_KEY` (Upstage 콘솔에서 발급). 평문 커밋 금지.

## 명령어 (macOS/Linux 로컬)

```bash
make install        # uv sync + 의존성 (.venv)
make up             # docker: postgres+pgvector(:5433) + neo4j(loopback 7474/7687)
make migrate        # alembic upgrade head
make seed           # 데모 유저 + 데모 시드
make test           # pytest — orthus_test DB 격리, mock 슬롯 고정
make wiki-rebuild   # corpus → LLM wiki distill+consolidate (ORTHUS_LLM/ORTHUS_EMBEDDING 사용)
make kg-bootstrap   # KG constraints/index 부트스트랩
make kg-rebuild     # KG full projection + prune
make api            # FastAPI dev (:8820)
make web            # Next.js dev (:3820)
make fmt            # ruff format + check --fix
```

## 설계 원칙 (위반 시 PR 거부)

1. **LLM은 컴파일/추론/초안/작업 후보 생성과 제한된 action judgment만 한다. 실행
   안전은 typed guard가 통제한다.** LLM-only 실행 금지 — bounded policy gate가
   outcome을 최종 결정한다.
2. **canonical 슬롯 스키마(Pydantic v2) 유지.** 거대 통합 스키마 금지.
3. **감사는 호출 단위 `audit()` context manager가 primary.** `correlation_id` 전파.
4. **라우터(/ask)는 read-only + 검증 게이트 통과 없이는 절대 실행 금지.**
   structured 분기는 sqlglot SELECT-only + schema_ok + read_only + EXPLAIN + LIMIT
   게이트 유지.
5. **수집은 소스 비종속 connector 인터페이스로만.**
6. **corpus는 단일 인덱싱 경로.** (chunk → embed → pgvector) 우회 금지.
7. **LLM wiki는 메모리 레이어, corpus는 raw 레이어.** 답변 그라운딩은 compiled
   wiki page 전용 — raw-chunk RAG 경로 (재)생성 금지. 모순은 silent overwrite
   금지 → WikiTask로 가시화. markdown이 SoR, Postgres는 인덱스.
8. **KG는 결정론 투영 + 템플릿 게이트 읽기 전용.** raw Cypher 입력 경로 금지.
   `ORTHUS_KG_ENABLED=false` fail-closed 기본.

## 절대 규칙 (hard constraints)

- 비서가 SQL write/DDL/DML을 실행하지 않게 한다. 검증 게이트 우회 경로 금지.
- LangGraph / persona / drift / confidence routing 코드 금지 (빈 stub도 금지).
- 시크릿 평문 금지 — `.env.example`에는 키 이름만.
- PII redaction 우회 금지 — `query_runs`/wiki page 저장 전 `redact_pii_text()` 통과.
- 답변 그라운딩은 compiled wiki page 전용.
- Solar 외 LLM 벤더 슬롯을 추가하지 않는다 (mock 제외).

## 코드 컨벤션

- Python 3.12 / Pydantic v2 / SQLAlchemy 2.x / Alembic / ruff. 프론트는
  Next.js(App Router) + BlockNote + Tailwind v4.
- 식별자 UUID v4, 시각 컬럼 `TIMESTAMPTZ` UTC.
- canonical 모델은 `orthus/schemas/`. 외부 호출(LLM/임베딩/DB)은 `audit()` span.
- 문서/주석 한국어 가능, 식별자는 영어.
- 테스트는 pytest. **검증 게이트 reject 5종 회귀 테스트 필수**
  (`tests/unit/test_validate.py`, `test_structured.py`).

## Worktree / PR

- 기능 작업은 `.worktrees/<topic>` feature 브랜치. `.env` 등 local secret은
  복사해도 되지만 절대 commit하지 않는다.
- PR 제목에 슬라이스 ID, body에 Risk/QA Evidence 명시.

## 저장소 위치

```
orthus/
├─ api/routes/     FastAPI: ask · wiki · kg · documents · agent_work · …
├─ router/         자동 라우터 + agentic/ (Solar function-calling 루프)
├─ structured/     NL→SQL 게이트 (JSONB store 대상)
├─ assistant/      sqlglot compile/validate/execute (게이트 코어)
├─ wiki/           LLM wiki: distill · consolidate · retrieve · qa · gap
├─ kg/             결정론 KG 투영 · outbox · 템플릿 게이트 · monitor
├─ corpus/         chunk → embed → pgvector
├─ connectors/     소스 비종속 커넥터
├─ agentwork/      agent-work 오케스트레이터 + policy gate
├─ mcp/            orthus-mcp stdio 서버
├─ models/         Solar/Mock 어댑터 + 작업별 배정 테이블
├─ audit/          audit() span + redact_pii
└─ schemas/        canonical Pydantic v2
experiments/fugu-ko/   측정 하네스 + RESULTS.md
wiki-store/            LLM wiki markdown SoR
web/                   Next.js FE (/wiki · /ask · /agent-work · 그래프 탐색기)
```

## 문서 포인터

| 파일 | 내용 |
|---|---|
| `docs/llm-wiki.md` | LLM wiki self-authoring 레이어 설계 |
| `docs/kg-model.md` / `docs/kg-implementation-spec.md` | 지식그래프 모델·구현 |
| `docs/inline-agentic-ask.md` | 인라인 에이전틱 /ask (Solar tool-use 루프) |
| `docs/company-agent-orchestration.md` | agent-work 채팅 오케스트레이터 |
| `docs/p3-autonomous-agent-loop.md` | policy gate 기반 typed action loop |
| `docs/model-orchestration.md` | 모델 슬롯 측정·배정 SoR |
| `docs/data-model.md` | Postgres + pgvector DDL |
| `experiments/fugu-ko/RESULTS.md` | 측정 결과·경향성 요약 (살균판) |
