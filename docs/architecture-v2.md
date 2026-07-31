# 아키텍처 v2 — orthus 지식 시스템 (central node + optional personal nodes)

> status: deep architecture 참조 문서 (대회 공개판 — 내부 진행 이력·엣지/배포
> 섹션은 제거했다). 시스템 개요·현행 진입점은 `README.md`.

이 문서는 아카식+비서를 **central 회사 node + 개인 local nodes**로 확장하는 target 아키텍처를 담는다.

핵심 정정(2026-05-28): 개인과 회사는 같은 LLM wiki/runtime을 공유하지 않는다. `scope=company|personal` 필터로 privacy를 해결하지 않는다. 같은 코드베이스는 재사용하되 DB/corpus/vector/wiki-store/agent/FE는 node별로 분리하고, personal→central 이동은 publish/promote 게이트로만 한다.
질의 정정(2026-06-05): personal `/ask`와 wiki query는 기본 `all` scope에서 own personal + central company 결과를 read-only로 fan-out/join한다. 이는 storage 공유나 personal→central import가 아니다.
P3 정정(2026-06-05): 자율 실행은 legacy LangGraph/persona/confidence routing 부활이 아니라 `docs/p3-autonomous-agent-loop.md`의 typed action/policy gate 기반 loop다. P6 이후 LLM action judgment와 policy memory는 bounded policy input으로 쓸 수 있지만 LLM-only 실행은 금지한다.
P4 정정(2026-06-07): P4 milestone은 route consolidation이 아니라 `/wiki`를 primary workspace home으로 재프레이밍하고 `/ask` Assistant, `/agent-work` review queue를 cross-link하는 unified shell/inbox/citation layer다.
P8 방향 전환(2026-06-10): 내부 문서(비공개)가 차기 target 아키텍처로 spec-lock됐다. central 단일 런타임(FE/API/storage/wiki compile) + 로그인별 merged view + thin personal collector(outbound-only) + owner-only row-level personal 경계로 재구조화한다. 본 문서의 federated local-first 구조는 **현재 배포/운영 상태의 기준**으로 유효하며, P8 cutover(P8.8) 완료 시점에 deep architecture 기준 문서를 교체한다. 2026-06-12 기준 central의 **개인** navigator에는 `/connectors/personal`이 있고, 이는 personal node 전용 FE가 아니라 로그인 유저 owner-scope 개인 connector config surface다. 로컬 수집 실행은 여전히 Desktop Collector/collector daemon 경계에서만 한다.

---

## 0. 한 그림 (화이트보드)

```
                 ┌────────────────────────────────────┐
                 │ central company node                │
                 │ company DB/corpus/vector/wiki-store │
                 │ company agent/router + FE           │
                 └───────────────▲────────────────────┘
                                 │ publish/promote only
                                 │ redaction + staging + approval
      ┌──────────────────────────┼──────────────────────────┐
      │                          │                          │
┌─────┴────────────────┐ ┌───────┴──────────────┐ ┌────────┴─────────────┐
│ personal Mac mini A  │ │ personal Mac mini B  │ │ personal Mac mini C  │
│ local DB/wiki/agent  │ │ local DB/wiki/agent  │ │ local DB/wiki/agent  │
│ local FE + collectors│ │ local FE + collectors│ │ local FE + collectors│
└──────────────────────┘ └──────────────────────┘ └──────────────────────┘
```

- central node는 회사 Notion 기반 corpus/wiki/structured store와 회사 agent/router/FE를 가진다.
- personal node는 개인 Mac mini에서 별도 DB/corpus/vector/wiki-store와 개인 agent/router/FE를 가진다.
- 개인 원문·corpus·wiki는 central에 자동 동기화하지 않는다.
- personal query plane은 central company read-only endpoint를 함께 조회해 개인+회사 근거를 합친다.
- 공유 필요분만 personal node에서 export/publish 후보 생성 → redaction/sanitize → central staging → 승인 → central corpus/wiki에 import/consolidate.

---

## 1. 질의 substrate (우리 것 — Notion DB 직접질의 아님)

확장 대비, 질의는 **우리 자체 백엔드 3종 + 자동 라우터**로 한다. Notion은 *소스/뷰*일 뿐 질의 substrate가 아니다.

| 백엔드 | 용도 | 상태 |
|---|---|---|
| **wiki** (LLM wiki, W0–W4) | 지식형/semantic 질의, grounding | 보유(재활용) |
| **structured (PostgreSQL)** | KPI/티켓/계획 등 **구조화 집계·필터** (제너릭 JSONB row store + 재사용 read-only NL→SQL 게이트) | 신규 |
| **KG** (graph) | 관계·다홉 추론 | **K-series 진행 가능**(K0 spec-lock 2026-06-10, `docs/kg-model.md`; central 단일 Neo4j rebuildable index, v1 projection company scope만 → K7 owner-scope 확장) |

- **자동 라우터:** 질문 종류 판정 → 구조화 집계면 structured(PG), 지식형이면 wiki, 관계형이면 KG(graph — K4b에서 결합). 라우터는 KG를 **나중에 끼울 수 있는 인터페이스**로 설계해 뒀고, K-series가 그 슬롯을 채운다(`docs/kg-model.md`).
- **통합 인텐트 판정 (`router.classify_intent`):** "명령이냐 질문이냐"(command-intake)와 읽기 백엔드 선택을 **한 번의 결정**으로 합친다. 결정론 fast-path가 명확한 경우(명시적 위임 prefix→`agent_task`, 키워드-명확 명령, 규칙-명확 읽기)를 LLM 없이 처리하고, **애매한 중간만** 기존 `classify` LLM 콜 1개를 3-way 읽기 enum → 7-way `{읽기 라우트 | command-family}`로 넓혀 판정한다(/ask당 LLM 콜 수 불변; prod LLM은 codex 서브프로세스라 hot path는 결정론 유지). 요약/설명/목록/정의 같은 ASK는 항상 읽기로 가고(명령 아님), 모호하면 읽기로 bias한다. LLM은 인텐트 **입력**만 내고 실행은 P3 policy gate가 결정한다(설계원칙 1, confidence routing 아님). `agent_task`는 명시적 prefix로만 결정론 감지한다.
- **inline agentic `/ask` (flag `ORTHUS_AGENTIC_ASK_ENABLED`, fail-closed, `docs/inline-agentic-ask.md`):** 켜지고 Solar 키가 설정돼 있으면, 고정 라우터 ladder 대신 in-process Solar **function-calling loop**(`max_turns` 캡)가 돈다. 위 3 백엔드는 그대로 **tool**(`team_schedule`/`wiki_ask`/`structured`)로 살아 sqlglot 검증 게이트·compiled-wiki 그라운딩·read-only 실행을 보존한다(LLM은 tool 선택만, SQL 실행은 결정론 게이트가 결정 — 설계원칙 1·4). 기본 chat 슬롯(`get_chat_model`)과 분리된 별도 엔진 슬롯(`get_agent_chat_model`)을 쓴다. flag off/키 없음/federated scope면 legacy ladder로 fail-closed fall-through. graph framework·persona·drift·confidence routing이 아니다.
- **structured 백엔드 안전:** 비서의 read-only NL→SQL **검증게이트를 재사용**한다(당초 "제거"에서 수정). 대상만 외부 DSN → **우리 PG의 제너릭 JSONB row store**(`notion_rows`, Slack 등 비-Notion 추출 fact용 `structured_rows`)로 바뀐다. sqlglot SELECT-only + schema_ok(JSONB store 논리 스키마) + read_only + EXPLAIN + LIMIT 게이트 그대로, 이중방어(앱검증 + read-only 롤) + 5-reject 회귀 유지.

---

## 2. Node 경계 (personal vs central)

동일 코드베이스와 파이프라인을 **node 단위**로 재사용한다. 동일 런타임에서 `scope`만 나눠 개인 privacy를 보장하지 않는다.

- **central node:** 회사 DB/corpus/vector/wiki-store/agent/router/FE. 회사 Notion과 회사 structured store가 authority.
- **personal node:** 개인 Mac mini의 local DB/corpus/vector/wiki-store/agent/router/FE. 개인 수집기와 개인 raw/corpus/wiki가 local authority.
- **공유 금지:** personal raw/corpus/wiki를 central DB/wiki-store/vector에 자동 저장하지 않는다. 단, 회사 도메인 메일(@nova.example/@acme.example)은 P6 통합 메일(내부 문서(비공개) §5-A)에 따라 personal node를 거치지 않고 company-scope source로 직접 흡수한다. *(P6.7 개정 예정: 회사 도메인 메일 ingest scope를 메일함별 owner 선택 — default owner-scope, 명시 opt-in 시 company-scope — 으로 확장한다. 내부 문서(비공개) §12. 구현 전까지 현행 company-scope 직접 흡수가 유효.)* 진짜 개인 소스(개인 Gmail 등)의 personal→central 자동 저장 금지는 그대로 유지된다.
- **기존 `scope` 컬럼:** P2.1에서 추가된 central schema hardening/호환 장치다. personal boundary로 쓰지 않는다. central query fail-closed, project 필터, 테스트에는 유지한다.
- **query scope:** company node의 public `/ask`와 `/wiki/ask`는 company scope만
  사용한다. personal node의 `/ask`와 wiki query는 기본 `all`(company + own
  personal)을 사용하며, central company read-only 결과와 local personal 결과를
  fan-out/join한다.
- **publish/promote:** personal→central 이동은 P2.5 게이트(redaction + staging + 승인 + central import/consolidate)만 허용한다.
- **외부 노출:** central은 Cloudflare for SaaS custom hostname +
  Cloudflare Tunnel + Mac mini loopback Caddy 뒤에서 공개한다. Route53은
  `orthus-central.example.com`을 Cloudflare provider host
  `orthus-team.example.com`으로 CNAME한다. Router port-forward +
  public Caddy는 fallback만 허용한다. personal public exposure는 별도 결정 전까지
  out of scope다. 두 node는 host, session cookie, allowlist, DB/wiki-store를
  공유하지 않는다.

---

## 3. 소스 (수집)

소스 비종속 connector 인터페이스(기존 원칙 5)로만 붙인다. C0 이후 connector
구현은 company/personal로 나누지 않고 account policy로 분기한다.

- **connector code:** 공통. `connector_slug`별 fetch/normalize만 소유.
- **connector account:** `connector_accounts.account_kind`가 `company|personal`.
  `company`는 `scope=company, owner_id=NULL`, `personal`은
  `scope=personal, owner_id=<user_id>`.
- **connector config:** `/connectors` web config는 token/repos/channels 같은 필수값만
  받는다. Codex/Claude/local dropbox 계열은 default path를 사용해 설정 없이 sync한다.
  secret value는 DB에 저장하지 않고 local secret backend에 저장한다. `.env`는
  bootstrap/dev fallback이다.
- **idempotency:** document upsert는 `(source, source_account_id,
  source_external_id)` 기준. 같은 Gmail message id라도 회사 계정과 개인 계정은
  다른 문서다.
- **state:** cursor/seen/budget/run history는 account 단위
  (`connector_sync_state`, `connector_items`, `connector_runs`).
- **policy:** company connector는 project 분류 후 company corpus/wiki/structured에
  쓴다. legacy personal node는 personal connector를 local DB/wiki에만 쓰지만, P8
  owner-scope 경로에서는 central `/connectors/personal`이 로그인 유저의 personal
  connector config/state를 저장하고 collector push 문서를 central owner-scope로 받는다.
  `/connectors` 기본 company route는 계속 company connector만 보여준다.
  personal→company는 여전히 P2.5 promote 게이트만 허용한다.
- **retrieval boundary:** company scope에 stale personal-only connector row가 남아도
  `retrieve()`는 connector manifest policy와 wiki provenance graph(claim/page→source)를
  이용해 해당 compiled wiki page를 grounding 후보에서 제외한다. row 삭제는 별도
  cleanup 승인 작업이다.

- **회사 소스 = 아크메 Notion 워크스페이스** (확정). 36개 DB + 다수 페이지. → central corpus → central wiki + central structured(PG).
- **개인 소스 (Q2 결정 / P8 보정):** legacy personal node는 개인 Notion + 이메일 export + Claude/Codex 세션 데이터 + chat export(ChatGPT/Claude) + GitHub + GWS Gmail/Drive + FE 입력을 지원했다. P8 thin collector cutover 범위는 local files, Claude/Codex sessions, GitHub, GWS Gmail/Drive로 제한한다(chat export, email export, personal Notion 제외). central **개인** navigator의 `/connectors/personal`은 이 personal source들의 config/state surface이며, sync command 실행은 central 서버가 직접 하지 않는다.
  - 참조 패턴: `<local>/wiki`의 conversation-ingest 컬렉터(claude-code/codex/chat-export)와 동형.
  - **privacy(하드):** 개인 소스는 민감(이메일·대화). legacy에서는 수집/인덱싱을 personal node local 경계에서 수행했고, P8에서는 collector가 수집한 문서를 central owner-scope 경계로 push한다. company scope publish 시 추가 redaction/sanitize/approval은 계속 필수다.

---

## 4. Notion = 양방향 (소스 + 뷰)

(Q 결정: 양방향)

- **소스(ingest):** 아크메 DB(행)+페이지 → markdown 렌더 → corpus → wiki, **그리고** typed 행 → structured(PG)에 적재.
- **뷰(write-back):** personal/central FE 뷰의 Notion write-back은 /board status 같은 검증된 경로만 유지한다. daily plan은 드롭됨(system-spec out-of-scope). FE ↔ Notion 동기화는 실제 구현된 scope에서만 확장한다.

---

## 5. 기능 delta (사라지는 것 / 들어오는 것)

**제거 (사라짐):**
- **독립 비서 제품 프레이밍** — 외부 `data_sources`(DSN) 레지스트리 + 독립 `/assistant/query` route. 조회 진입점은 라우터로 단일화.
- raw-chunk RAG는 이미 W3에서 제거됨.

**재사용·repurpose (당초 "비서 전체 제거"에서 수정):**
- **비서 read-only NL→SQL 검증게이트** (`assistant/compile.py`·`validate.py`·`execute.py`, sqlglot, `query_runs`) → **structured 백엔드로 repurpose**: 외부 DB가 아니라 우리 PG **JSONB row store(`notion_rows`)** 대상 NL→SQL. 5-reject 회귀 + 이중방어 유지.

**유지·재활용:**
- corpus 파이프라인(chunk/embed/pgvector) — node별로 같은 구현 재사용.
- **LLM wiki(W0–W4)** — central wiki와 personal wiki가 같은 코드를 쓰되 같은 store/runtime을 공유하지 않는다.
- audit/redact, BlockNote 에디터(개인 뷰), Notion connector(페이지→DB로 확장), `make` 컨벤션, source-agnostic connector 인터페이스, canonical 스키마 규약.
- P2 `/ask`, `/wiki`, `/wiki/tasks`, `/gaps`, `/promote`, `/connectors`는 P3
  Agent Work source로 재사용한다.

**신규 (들어옴):**
- personal Mac mini node 패키징 + central/personal 런타임 경계 + publish/promote 흐름.
- **Notion DB connector** (database query/rollup; 페이지 전용 → DB 행 수집).
- **structured(PostgreSQL) 백엔드** = 제너릭 JSONB row store(`notion_rows`: Notion DB row, `structured_rows`: Slack/contact/action/event/decision/link fact) + repurpose된 NL→SQL 게이트. + **자동 라우터**(wiki/structured; KG graph 분기는 K4b에서 결합 — `docs/kg-model.md`).
- **Notion write-back** (FE 뷰 → Notion).
- personal local 다중소스 connector (email / AI 세션 / chat export).
- **FE 대시보드** (§6).
- **P3 Agent Work loop**: `/ask` 기반 Assistant command surface,
  독립 `/agent-work` review UI, deterministic policy gate, typed action handlers,
  node-local policy memory.

---

## 6. 개인 orthus.ai FE 기능 (요청 명세)

| 기능 | 백엔드/소스 | 비고 |
|---|---|---|
| ~~daily plan (Sunsama식)~~ | *(드롭 - system-spec out-of-scope)* | *(재착수 시 별도 milestone 필요)* |
| wiki 검색 (range 설정) | local personal wiki + central company wiki | personal 기본 all(joined) + 기간/소스/scope 필터 |
| wiki 정리 task | wiki tasks(기존 WikiTask) + 개인 | 미해결/모순/stale |
| unified wiki workspace | `/wiki` + Assistant(`/ask`) + Agent Work(`/agent-work`) | P4: `/wiki` primary home, route 유지, cross-link/inbox |
| assistant 자연어 명령 | AgentWorkItem candidate + policy gate | P3 완료, P4에서 wiki context/citation bridge 추가 |
| agent-work review | data_gaps + WikiTask + promote + connector_runs + action drafts | P3 완료, P4에서 related wiki links 추가 |
| 팀원/정보 | 회사 wiki + Notion `팀원`/`직원`/`파트너(사)`/`컨택·인물` | 뷰 |
| 외부일정(캘린더) | Notion `외부 일정`/`배포 일정` | 소스 확인 필요(§9) |
| 티켓(project task) | structured + Notion `티켓`/`협업업무표`/`씬 작업 트래커`/`피드백·수정요청` | 집계→라우터 |
| ~~KPI 트래킹~~ | structured + Notion `KPI`/`목표 항목`/`월간 목표` | *(hold - KPI dashboard: metric-shaped claims 미생성)* |
| 주간/월간 계획+회고 | Notion `주간/월간 계획`·`주간/월간 회고` | ingest+뷰 |
| 사무실 정보 | 미확인(DB 없음) | §9 |

---

## 7. 단계별 계획 (Phase 2)

> MVP = **Notion 아크메 시드부터** (Q 결정). KG는 K-series로 진행(`docs/kg-model.md`).

| ID | 내용 | verify |
|---|---|---|
| **P2.0 (MVP)** ✅ | Notion connector를 **DB 수집**으로 확장 → 아크메 36 DB(행)+페이지를 회사 corpus로 ingest → 회사 wiki author. `/wiki/ask`가 아크메 내용에 grounding. | 완료 (commit `b1a6f76`) |
| P2.1 ✅ | central `scope` 컬럼 hardening + project 차원 + 검색 필터 + project override. 단, personal runtime boundary로 사용하지 않음. | 완료 (commits `682ee21`–`bea937d`) |
| P2.2 ✅ | 비서를 structured 백엔드로 **repurpose**(JSONB row store `notion_rows` + 재사용 NL→SQL 게이트) + 외부 data_sources·독립 /assistant route 제거 + **자동 라우터**(wiki/structured) + X-User-Id 트러스트 바운더리 | 완료 (commits `ef6c4d9`, `96b3bba`, `22fe80d`) |
| L0 ✅ | Mac mini side-by-side node bootstrap: `company` + `personal-a` 별도 env/DB/wiki-store, host-native API/FE scripts, Docker는 DB-only | `node-smoke` company/personal 통과 |
| P2.3-C0 ✅ | company/personal 공통 connector substrate: connector_accounts/state/items/runs + account-scoped import | migration 0009 + connector substrate tests |
| P2.3-C1 ✅ | 기존 Notion import를 connector account substrate로 이관. legacy rows adopt 후 account-scoped import | Notion account tests + 기존 Notion tests |
| P2.3-C2 ✅ | OpenHuman식 connector registry/manifest + shared runner substrate. company/personal 공통 확장 지점 확정 | `manifest.py`/`registry.py`/`runner.py` + C2 tests |
| P2.3-C3 ✅ | personal `local_files` connector: allowlisted roots, hidden/symlink/binary/oversize skip, path-hash external id, manual node sync | local_files focused tests |
| P2.3-C4 ✅ | personal AI session connectors: Codex/Claude JSON/JSONL logs, user/assistant text only, tool payload skip, path-hash external id, manual node sync | ai_sessions focused tests |
| P2.3-C5 ✅ | personal chat export connector + FE connector surface: ChatGPT/Claude export JSON/ZIP, user/assistant text only, manifest-driven `/connectors` attach/sync | chat_exports + connector API/FE tests |
| P2.3-C6 ✅ | Notion generic connector surface: `/connectors` ensure/sync + FE card에서 company/personal Notion 실행 | connector API tests |
| P2.3-C7 ✅ | personal email export connector: `.eml`/`.mbox`/JSON, attachment skip, PII/secret redaction, manual node sync | email_exports focused tests |
| P2.3-C8 ✅ | GitHub connector: personal repos issues/PRs, web-configured token/repos 우선, env fallback, manifest-driven FE attach/sync | github focused tests |
| P2.3-C9 ✅ | Slack connector: company channel/thread sync, web-configured token/channels 우선, env fallback, bot/system/file payload skip | slack focused tests |
| GWS Gmail/Drive ✅ | node-local gws CLI auth를 사용해 Gmail/Drive를 personal node corpus/wiki로 sync. orthus에는 Google token 저장 없음 | gws_cli unit tests + connector API focused tests |
| Connector config ✅ | `/connectors`에서 token/path/repo/channel 저장. secret은 local secret backend, DB는 secret ref/redacted settings만 보관 | connector API focused tests + FE lint/build |
| P2.3-C10+ *(OPEN — 보류)* | 추가 connector 확장. Linear connector는 사용자 결정으로 제외 | 소스별 ingest + redaction |
| P2.4 ✅ | **FE 대시보드**: /projects ✅, /board(Nova 칸반 + Notion `상태` write-back) ✅, override clear ✅. daily plan 드롭. | /projects + /board 동작 확인 + board write-back tests |
| P2.5 ✅ | personal→central **publish/promote** 흐름 + FE: `/promote/export` → redaction/sanitize → `promote_staging` → approve/reject → central import/consolidate; web `/promote` review UI | `tests/integration/test_promote.py` + browser QA |
| WikiTask UI ✅ | distill open questions + conflict tasks를 node-local wiki task로 저장하고 `/wiki/tasks`에서 review/resolve | wiki authoring/task API tests + browser QA |
| S1 ✅ | Cloudflare for SaaS custom hostname + Tunnel + Mac mini loopback Caddy edge, Google OAuth + invite allowlist + session mode. central은 `orthus-central.example.com`, personal은 후속 별도 edge/subdomain. TTL 90일/renewable 3650일. router port-forward는 fallback smoke only. | auth integration tests + browser QA + GitHub Actions `S1 Public Smoke` + 내부 문서(비공개) evidence |
| P3.0 ✅ | Autonomous Agent Work Loop spec: Assistant command surface, 독립 `/agent-work`, policy gate, policy memory | `docs/p3-autonomous-agent-loop.md` + docs-check |
| P3.1a ✅ | AgentWorkItem first spine: node-local queue, deterministic policy gate, data_gaps adapter, `/agent-work` list/detail | agent_work tests + browser QA |
| P3.1b ✅ | Reviewer decision endpoint + append-only `agent_work_decisions`, no external runner | transition/auth/redaction/audit tests |
| P3.1c ✅ | WikiTask adapter, then promote/connector source expansion after side-effect review gates | source adapter tests |
| P3.1d ✅ | pending `promote_staging` + failed `connector_runs` Agent Work source expansion, no promote import/connector retry | source adapter/API tests |
| P3.2a ✅ | `/agent-work` FE review controls: source sync buttons, approve/dismiss/request-data, returned decision feedback | browser QA + FE build |
| P3.2b ✅ | node-local `agent_policy_observations` + `/agent-work/policy-memory` bucket summaries, no gate escalation | policy memory tests |
| P3.2c ✅ | read-only `policy_memory` context attached to Agent Work payloads, `used_for_outcome=false` | boundary + context tests |
| P3.2 ✅ | `/ask` Assistant command intake queues `assistant_command` Agent Work candidates, no action runner | assistant/API tests |
| P3.1 ✅ | Full Agent Work substrate + existing queues as sources | agent_work source adapter tests |
| P3.3a ✅ | cleanup-only WikiTask auto execute resolves stale/dedup/provenance tasks, no company wiki knowledge write | auto-execute task tests |
| P3.3b ✅ | configured Assistant connector sync auto executes `run_connector_account_sync`, records command audit + run history; no failed-run retry | ask/connector tests |
| P3.3e ✅ | failed `connector_runs` source items carry retry-guard evidence and reviewer decisions do not retry connectors | retry-guard tests |
| P3.3c ✅ | personal board reversible cleanup archives done tasks only on personal nodes, no delete/external write | ask/board tests |
| P3.3d ✅ | data_gap reviewer approve/dismiss writes source `data_gaps.status`; request_more_data leaves source open | decision write-back tests |
| P3.3 ✅ | First auto actions: connector sync, WikiTask cleanup, board cleanup, data-gap write-back, connector retry guard | action policy tests + node smoke |
| P3.4a ✅ | `document_draft` creates editor-visible `agent_draft` rows, no corpus/wiki authoring | draft handler tests |
| P3.4b ✅ | `agent_draft` save stays draft-only; explicit publish flips to `editor` and runs corpus/wiki authoring | publish boundary tests + editor UI |
| P3.4c ✅ | email commands with recipient hints create reviewable `payload.email_draft`; missing recipient becomes `request_more_data` | ask/email draft tests |
| P3.4d ✅ | canonical `EmailDraftPayload` allowlist forbids SMTP/send/provider extra fields | schema guard tests |
| P3.5b ✅ | explicit `no_edit=true` email approval telemetry exposes recent 60-day no-edit threshold metrics, still `used_for_outcome=false` | policy memory metric tests |
| P3.5c ✅ | email auto-send preflight records exact-bucket/actor/recipient/domain/template/rate-limit/sensitive/attachment checks, still no send/outcome escalation | preflight tests |
| P3.5d ✅ | server preserves `no_edit` telemetry only for `draft_for_review` exact email bucket approvals | direct API guard tests |
| P3.6a ✅ | email sender boundary: default `none`, `fake` sender only, hash-only `email_send_log`, personal owner/admin + eligible gate + idempotency/rate-limit checks, no real provider | email sender boundary tests |
| P3.4 ✅ | Draft actions: editor-backed document draft + email draft review payloads; real email provider remains out of scope | review-flow tests + browser QA |
| P3.5a ✅ | policy-memory bucket summaries can write deterministic wiki summary page, `used_for_outcome=false` | policy summary API/FE tests |
| P3.5 ✅ | Policy memory wiki summary, no-edit threshold evidence, preflight gate, server guard | policy memory tests |
| P4.0 ✅ | Unified wiki-first workspace spec lock: 내부 문서(비공개) + canonical docs alignment, no code | docs-check + operator review |
| P4.1 ✅ | Wiki-first shell/nav: `/wiki` primary entry, persistent Assistant launcher, Agent Work badge | company/personal browser QA, mobile 390x844 |
| P4.2 ✅ | Assistant-Wiki citation bridge: `wiki_links`, `/ask?context_wiki_slug=...`, command context payload | API/UI tests + browser QA |
| P4.3 ✅ | Wiki-Agent Work cross-link: derived `agent_work_wiki_link`, related work panel/detail links | source projection tests + browser QA |
| P4.4 ✅ | Wiki-home unified inbox: read-only `/agent-work/inbox-summary` for pending knowledge work | deterministic count tests + node/browser smoke |
| P4.5a ✅ | Data-gap wiki page link: `data_gaps.context_wiki_slug`, related data-gap panel on `/wiki/{slug}` | gap persistence tests + browser QA |
| P4.5b ✅ | WikiTask embed + page-specific tasks inside `/wiki`, direct `/wiki/tasks` route preserved | page task tests + browser QA both nodes |
| P5.0 ✅ | Mobile parity spec lock: 내부 문서(비공개), 390x844/360x780 QA baseline, current `<760px` compact-shell threshold documented | docs-check + tests |
| P5.1 ✅ | Agent Work mobile detail navigation + dense toolbar collapse, `<760px` threshold unchanged | browser QA both nodes |
| P5.2 ✅ | Wiki Ask mobile controls + filter disclosure, `<760px` threshold unchanged | browser QA both nodes |
| P5.3 ✅ | Wiki page related panels mobile polish, long slug/unsupported state overflow polish, `<760px` threshold unchanged | browser QA both nodes |
| P5.4 ✅ | WikiTask review mobile parity, local-state phone list/detail switcher, 44px controls, `<760px` threshold unchanged | browser QA both nodes |
| P5.5 ✅ | Mobile QA evidence sweep across `/wiki`, `/wiki?tab=tasks`, `/wiki/{slug}`, `/wiki/tasks`, `/ask`, `/agent-work`; `/ask` submit 44px regression fix; `<760px` threshold unchanged | company/personal 390x844 + 360x780 sweep, personal federated page, 768px threshold smoke |
| **K-series** *(착수 가능)* | **KG(graph)** 백엔드 + 라우터에 KG 슬롯 결합 | K0 spec-lock 완료(2026-06-10) — `docs/kg-model.md` K1–K7, P8 정합(central 단일 Neo4j, v1 company scope → K7 owner-scope) |

각 단계 회귀: 기존 wiki E2E(62 tests) 불변 + central scope/connector/라우팅 테스트 + personal node bootstrap/publish gate 테스트.

---

## 7.1 P4 Cross-Link Architecture

P4는 existing routes와 existing source visibility를 연결하는 projection layer다.
새로운 grounding path나 write path가 아니다.

| Link | Shape | Source of truth | Boundary |
|---|---|---|---|
| Assistant answer -> wiki page | `wiki_links: [{slug,title,scope}]` | existing compiled wiki answer grounding | empty/missing links do not alter answer |
| Wiki page -> Assistant | `/ask?context_wiki_slug=<slug>` | wiki page slug, backend scope clamp | prefill/context only, no role/policy bypass |
| Assistant command -> Agent Work | `payload.context.wiki_slug` | queued `assistant_command` payload | additive metadata, no outcome change |
| Agent Work -> wiki page | logical derived `agent_work_wiki_link(item_id,wiki_slug,relation_kind)` projection | `wiki_task`, provenance-bearing `document_draft`, or command context | prefer API/view projection; persisted table requires explicit P4.3 approval, no page-side review mutation |
| Wiki home -> pending work | `GET /agent-work/inbox-summary` | existing WikiTask/promote/data_gap/Agent Work list visibility | read-only counts + last-N, no auto-execute |
| Data gap -> wiki page | `data_gaps.context_wiki_slug`, `GET /wiki/pages/{slug:path}/data-gaps` | explicit `context_wiki_slug` or top `WikiSourceRef.page_slug` for weak/insufficient gaps | nullable, no backfill, no page-side status mutation |
| WikiTask -> wiki page | `GET /wiki/pages/{slug:path}/tasks` | strict current node-local page slug in `WikiTask.related` | open tasks only, no claim expansion, no page-side resolve |

Implementation guidance:

- Keep `/wiki`, `/ask`, `/agent-work`, and `/wiki/tasks` direct routes stable.
- Reuse P3 source adapters and role checks for inbox counts.
- Treat personal default wiki scope `all` as local personal + central company
  read-only fan-out; company default scope stays `company`.
- P4 UI may display policy memory context. P6 이후 mail action은 명시된 bounded
  policy input으로 policy memory를 사용할 수 있지만, P4 자체가 임의 outcome 변경을
  도입하지는 않는다.

---

## 8. Phase 1 → v2 hard-constraint 변경 (AGENTS.md 갱신 대상)

본 방향은 Phase-1 AGENTS.md의 일부 하드제약을 **Phase 2에서** 뒤집는다. **Phase-2 빌드 시작(P2.0) 전까지 Phase-1 제약은 유효**, 빌드 진입 시 AGENTS.md 정식 개정.

- "Neo4j/KG 코드 금지" → **K0 spec-lock(2026-06-10)으로 정식 개정 완료** — KG는 K-series 범위(`docs/kg-model.md`)에서 허용, `ORTHUS_KG_ENABLED=false` fail-closed 유지. 라우터는 KG-pluggable 설계.
- "비서 = 제품 본체, read-only + 검증게이트" → **비서 NL→SQL 제거**, 라우터(wiki/structured)로 대체.
- "단일 사용자" → **federated local-first**(central node + personal Mac mini nodes). `scope` 필터는 central hardening이지 personal privacy 모델이 아니다.
- "Notion 단방향 소스" → **양방향(소스+뷰)**.
- "confidence routing 코드 금지" → **자동 라우터 도입**(단, persona/drift는 여전히 비-목표).
- P3는 confidence routing을 도입하지 않는다. `auto_execute`는 LLM confidence-only가
  아니라 typed action handler와 bounded policy matrix가 허용한 경우만 뜻한다. P6 이후
  LLM action judgment는 matrix input으로 사용할 수 있다.

---

## 9. 미해결 질문 / 결정

- ~~structured 백엔드 안전 모델~~ → **결정**: 비서 read-only NL→SQL 게이트 재사용(대상=PG JSONB store `notion_rows`). 저장 형태=제너릭 JSONB row store. (남은 세부: NL→SQL이 JSONB 연산자 `properties->>` 생성하도록 catalog/프롬프트 구성, db_name/property-key 화이트리스트.)
- **캘린더(`외부 일정`)**: Notion-native 관리인가, 외부(Google 등) 동기화 소스인가? *(여전히 OPEN)*
- **사무실 정보**: 전용 DB 없음 — 페이지? 신규 생성? *(여전히 OPEN)*
- ~~개인 Mac mini node 패키징~~ → **결정됨(L0 완료)**: host-native scripts,
  node env profiles, `~/.orthus/nodes/<node>/` DB/wiki-store 경계로 운영한다.
  서비스화/패키징 개선은 별도 운영 milestone이다.
- ~~개인 소스 privacy~~ → **결정됨(P2.3 완료)**: 개인 소스는 personal
  node local-only ingest, source별 redaction, central 이동은 publish/promote
  gate만 허용한다.
- **publish/promote 게이트**: 기본 export/stage/approve/reject, FE review,
  run history, sanitize diff는 P2.5/P2.6에서 완료. 대량 승인 UX만 필요 시 후속.
- ~~S1 auth 세부~~ → **결정됨(S1 완료)**: central admin allowlist, session
  TTL 90일/renewable 3650일, host-native FastAPI behind Caddy 확정. personal public
  subdomain/edge는 별도 결정 전 out of scope.
- **daily plan** 모델: 드롭됨. 재착수 시 별도 milestone/acceptance criteria 필요. *(system-spec out-of-scope)*
- ~~KG 도입 시점/범위~~ → **결정됨(K0 spec-lock 2026-06-10)**: K-series로
  진행(`docs/kg-model.md`). P8 정합으로 central 단일 Neo4j rebuildable index,
  v1(K2–K6) projection은 company scope만이며 K7에서 owner-scope graph를
  개방한다(전 템플릿 owner 술어 + 경로 가시성 규칙).

---

## 10. 현재 코드 재활용 맵

| 신규 필요 | 재활용 대상(현 코드) |
|---|---|
| 회사 corpus ingest | `orthus/connectors/notion.py`(페이지) → DB 확장, `orthus/corpus/pipeline.py` |
| central/personal wiki | `orthus/wiki/*`(W0–W4: store/distill/consolidate/retrieve/qa/author) 재사용. store/runtime은 node별 분리 |
| structured(PG) | 기존 SQLAlchemy/Alembic 패턴, `orthus/db.py` |
| 라우터 | ✅ `orthus/router/` — canonical 스키마로 결과 통합 (P2.2b) |
| personal local source connector | source-agnostic 인터페이스(`orthus/connectors/base.py`) + local node 설정 |
| FE 대시보드 | ✅ `web/`(Next.js + BlockNote): /projects, /board, /ask, /editor, /wiki (P2.4-FE) |
| 감사/redaction | `orthus/audit/*` |
