# K0–K7 설계 (+ K8/K9 후속) — Neo4j 지식그래프(KG) rebuildable index

> status: K0 spec-lock + P8 정합 개정 + K7 owner-scope graph 확정 (2026-06-10), 구현-ready 상세 계약 확정 (2026-06-11).
> **구현 진행 (2026-07-05 확인): K1–K9 main 머지 완료. K7 owner-scope graph — K7.1(projection/outbox owner-scope, migration 0057, schema v2) main 머지 완료, K7.2(gate owner-scope resolve + owner-variant 템플릿 + boundary matrix + 와이어 `scope`/`is_own_personal` + tripwire)는 main 머지 완료(PR #340), K7.3(K4 endpoint + K5 패널 owner-scope: owner_footprint endpoint + '내 개인 메모' 패널) main 머지 완료(PR #345), K7.4(/ask graph owner two-framing + missing_link owner 확장) main 머지 완료(PR #360, `run_path_framings` + `RoutedAnswer.path_framings` + owner-inclusive missing_link L6), K7.5(owner erasure + ops runbook) main 머지 완료(PR #362). K8 모순 노출 main 머지 완료(PR #373). K9 엔티티 연결 발견 main 머지 완료(2026-06-24, PR #496 — `entity_neighbors`/`entity_mentions` 노출 + 패널 엔티티 다리 + `/ask` entity intent·entity star). 그래프 탐색기(kg-graph-explorer) E1 backend expand(PR #569) + E2 FE 인터랙티브 탐색기 d3-force(PR #584) + E3 모바일/a11y QA·탭타깃 보정(PR #586) main 머지 완료 — 내부 문서(비공개).** 상세 §5 마일스톤 표 상태 열 참조.
> 상위: 내부 문서(비공개)(§11, §K-series 수용 기준)·`docs/architecture-v2.md` §1 KG 슬롯. 본 문서는 K-series(K0–K7)의 canonical 상세이며, 이후 변경은 K-series milestone PR에서 본 문서와 함께 갱신한다. K8/K9는 본 문서 범위 밖 **후속 마일스톤**으로, 상세는 내부 문서(비공개)(design-only)에 있고 본 문서 §0·§5 표에 등재만 했다.
> K0에서 system-spec §11·Non-goals, roadmap hold, AGENTS hard constraint, data-model §1·§12를 함께 개정했다. 현행 `docker-compose.yml`은 postgres 단독이다(2026-06-10 확인) — K1은 기존 컨테이너 활용이 아니라 신규 추가다.
>
> **P8 정합 (2026-06-10):** K0 초안은 federated 2-node(노드별 Neo4j 인스턴스) 전제였다. 같은 날 spec-lock된 P8 central consolidation(내부 문서(비공개))이 구현에 들어갔고(P8.1/P8.2/P8.3/P8.5a/P8.7a merge 완료), P8 §8이 KG projection 대상을 central 단일 DB로 재배치했다. 본 문서는 그에 맞춰 개정됐다: **central 단일 Neo4j 인스턴스**, **v1(K2–K6) projection은 company scope row만**(personal owner-scope row는 그래프에 존재하지 않음 — fail-closed), K6 `/federation/kg/query` 폐기.
>
> **K7 확정 (2026-06-10 사용자 결정):** personal owner-scope row의 graph 투영은 단일 central 그래프 + 전 템플릿 owner 술어 강제 방식(§4)으로 **K7 slice로 확정**했다. K4 게이트/회귀 인프라가 검증된 뒤 착수하며, 그 전까지 v1 company-scope-only 경계가 유효하다.

핵심: KG는 LLM wiki 메모리 레이어 위의 **관계 인덱스**다. Postgres + wiki-store
markdown이 SoR로 남고, Neo4j는 embedding과 동급의 파생·재구축 가능(rebuildable)
인덱스다. 라우터는 이미 "KG를 나중에 끼울 수 있는 인터페이스"로 설계돼 있고
(`docs/architecture-v2.md` §1), K-series는 그 슬롯을 채우는 작업이다.

용어: 본 문서에서 **v1**은 K7 owner-scope 개방 *이전*의 그래프
전체(K2–K6의 스키마·projection 범위, company scope only)를 가리키는 단일
용어다. 동기화 아키텍처의 두 단계는 **batch(K2)**·**outbox(K3)**로,
entity layer는 **K6**으로 부른다 — 이들에 별도의 "v2" 번호를 쓰지 않는다.

---

## 0. 목표와 문제 정의

### 문제

현행 `/ask`는 wiki 분기(compiled page grounding)와 structured 분기(JSONB
집계) 두 경로뿐이라, **지식 사이의 관계 자체를 묻는 질문**을 1급으로 다루지
못한다:

- "A와 B는 무슨 관계인가" — 두 페이지를 잇는 다홉 경로 탐색이 없다.
- "이 주장과 충돌하는 주장은 무엇이고 해소됐는가" — 충돌은 `WikiTask`로
  가시화되지만 그래프로 추적·탐색할 수 없다.
- "이 답의 근거는 어떤 원본 문서에서 왔는가" — claim→source→document
  provenance 체인을 한 번의 질의로 따라갈 수 없다.
- "정보는 있는데 연결이 끊겨 답을 못 만드는" 경우를 구분하지 못한다(현행
  gap reason은 retrieval 점수 기반 — §5.2).

`wiki_links`/provenance FK/`structured_rows`에 관계 데이터는 이미 쌓여
있으므로, 문제는 데이터 부재가 아니라 **관계를 1급 연산으로 묻는 질의
모델의 부재**다.

### 완성 시 사용자가 할 수 있는 것

| Slice | 사용자 가치 |
|---|---|
| K4 | wiki page 주변 관계를 read-only API(`GET /wiki/pages/{slug:path}/graph`)로 조회 |
| K4 | 충돌 claim 추적(`conflicts_of`), 근거 체인 추적(`provenance_chain`) — §4 템플릿 |
| K4b | `/ask`에 "A와 B는 무슨 관계?" 류 질문 — graph 분기가 경로를 찾고 답은 wiki page로 grounding |
| K5 | `/wiki/{slug}`에서 관련 지식 1–2 hop 미니맵 패널 확인 |
| K6 | "연결이 끊겨 못 답한" 데이터 갭(`missing_link`) 자동 가시화 + entity(인물/조직/시스템) 그래프 substrate(탐색 표면은 K9에서 노출 — §5 K9 행) |
| K7 | owner 본인의 personal 지식 ↔ company 지식 관계 탐색(타 user/admin에게는 비노출) |
| K8 (후속) | 모순(상충 주장)을 `/ask`와 페이지 그래프 패널에서 발견·추적 — 내부 문서(비공개) |
| K9 (후속) | 인물/제품/프로젝트(엔티티)를 매개로 페이지를 가로지르는 연결 발견 — 내부 문서(비공개) |

### 범위 경계

- **포함**: K1 infra → K2 결정론 projection → K3 outbox → K4/K4b 읽기
  경로 → K5 FE 가시화 → K6 entity/hardening → K7 owner-scope graph(§5).
- **제외**: §6 비-목표 전부 — persona/confidence routing, Neo4j SoR화,
  raw 본문/chunk 저장, 새 central write path, K7 이전 owner-scope 투영과
  K7 계약 부분 구현.
- 본 문서는 **설계 전용**이다 — 계약 수준의 스키마/DDL/설정
  스니펫(compose YAML, Cypher 제약, reference 템플릿)은 포함하지만
  애플리케이션 구현 코드(Python/TS)는 담지 않는다. 각 slice PR이 코드 +
  Verify 테스트 + 본 문서 갱신을 함께 담는다(레포 slice 컨벤션).

### 포지셔닝

모든 설계 결정은 위 "관계 인덱스" 포지셔닝에서 나온다. 근거는 네 가지다.

1. **불변식 보존** — "답변 grounding은 compiled wiki page 전용"(system-spec
   불변식 5)을 깨지 않는다. KG는 어떤 wiki page를 근거로 쓸지 찾는 다홉 탐색을
   담당하고, 답변 본문은 항상 provenance로 resolve된 wiki page에서 나온다.
   raw-chunk RAG 부활이 아니다.
2. **fail-open 안정성** — Neo4j가 죽어도 `/ask`의 wiki/structured 분기는
   무영향이다. SoR이 아니므로 데이터 유실 리스크가 없고, 언제든
   `make kg-rebuild`로 재생성한다.
3. **재료 재사용** — `wiki_links(backlink/supports/conflicts/derived_from)`,
   WikiClaim→WikiSource→corpus chunk provenance 체인,
   `structured_rows(contact/action_item/event/decision/link)`가 사실상
   그래프다. v1은 LLM 호출 0회의 결정론 projection만으로 만들 수 있다
   (원칙 1 준수, gap detection이 LLM 0회로 들어간 것과 같은 패턴).
4. **도입 근거** — 현재 규모(wiki_pages 약 9천 행)에서 Postgres recursive
   CTE가 못 버티는 수준은 아니다. KG의 근거는 성능 위기가 아니라 **쿼리
   모델**이다: 가변 깊이 경로 탐색, 충돌 추적, provenance 체인을 1급 연산으로
   만들고, 향후 소스 증가에 대비한다.

---

## 1. 현행 계약과의 정합 체크

| 제약 (출처) | 본 계획의 대응 |
|---|---|
| Neo4j/KG 코드·stub 금지, hold (내부 문서(비공개) 금지/보류 목록) | **K0 spec-lock이 첫 milestone.** P4.0/P5.0/P6.0과 동일하게 docs-only PR로 system-spec/roadmap/AGENTS의 hard constraint를 개정한 뒤에만 코드 시작. architecture-v2 §8이 이미 "KG는 후속 Phase 자산, 빌드 진입 시 AGENTS 정식 개정"으로 예고해 둠 |
| personal 데이터 경계 — P8에서 owner-only row-level 경계로 개정 (내부 문서(비공개) §5) | **central 단일 Neo4j 인스턴스.** KG는 central runtime 전용 인프라이고, personal node(P8 thin collector)는 Neo4j를 갖지 않는다. **v1(K2–K6) projection은 `scope='company'` row만 읽는다** — personal owner-scope row는 그래프에 존재하지 않으므로 graph 경로 탐색이 owner 경계를 넘을 수 없다(fail-closed). owner-scope 투영은 **K7**에서 전 템플릿 owner 술어 + 경로 가시성 규칙과 함께 개방한다(§4) |
| personal→company scope 전환은 promote 게이트만 | central KG는 central Postgres/wiki-store의 company scope에서만 projection. promote 승인된 `promoted_personal` 문서가 company scope에 들어오면 그때 KG에 반영된다 — 별도 KG 동기화 경로를 만들지 않아 자동으로 게이트 준수 |
| LLM은 압축/추출만, 실행·검증은 결정론 코드 (원칙 1) | v1 projection은 LLM 0회. K6 entity 추출도 distill 확장으로 LLM은 추출만, dedupe/검증/충돌 처리는 코드. 모순(같은 `name_norm`, 다른 `entity_kind`)은 silent overwrite 금지 → `WikiTask(kind="entity_conflict")` (claim 충돌 `kind="conflict"`와 **별도 kind** — 같은 kind면 K2 conflict-task 인덱스가 entity task의 `related` 쌍을 CONFLICTS_WITH 엣지 속성으로 오염시킨다, 구현 명세 §9.5) |
| 검증 게이트 없는 실행 금지 (원칙 4) | KG 읽기는 **typed Cypher template allowlist + 파라미터 바인딩**만. LLM이 Cypher를 자유 생성하지 않음. `access_mode=READ` 세션 + timeout + LIMIT 주입의 이중 방어(structured 분기의 sqlglot 게이트와 동형 — §4 안전 모델) |
| PII redaction (`docs/operations.md` §8) | KG에는 **본문을 저장하지 않는다.** slug/title/메타/hash/ref UUID만. title은 wiki 저장 시 이미 redaction 통과한 값. 본문 조회는 항상 PG/wiki-store로 되돌아간다. **K6 예외(person-entity carve-out):** `:Entity{entity_kind=person}`의 `display_name`/`name_norm`은 사람 이름(operations §8.3 Direct PII)이라 `redact_pii`가 거르지 못한다. 회사 내부 지식 한정의 문서화된 carve-out으로 저장을 허용하되 — ① persist 전 entity 값에 `redact_pii` 적용(이름 안에 섞인 이메일/전화만 차단, 이름 자체는 회사지식으로 유지) ② company-scope-only + central-only(loopback) + 외부 노출 없음 ③ owner-erasure 경로 보장(operations §8.4) — 그리고 carve-out은 `entity_kind=person`에만 적용된다(org/project/system 이름은 PII 아님). 상세 operations §8 |
| app-internal port 미노출 | central bolt(`7687`)/HTTP(`7474`)는 loopback 전용, test 컨테이너는 별도 포트(`7688`). 금지 포트 목록 반영은 완료(`docs/operations.md` §1, 내부 문서(비공개) §11, AGENTS 절대 규칙). graph 데이터 접근은 central API 표면(K4 `GET /wiki/pages/{slug:path}/graph` 등)으로만 |

### 기술 스택과 기존 환경

| 영역 | 현행 | K-series 신규 |
|---|---|---|
| Backend | Python 3.11+ / FastAPI / SQLAlchemy 2.x / Alembic / Pydantic v2 / ruff | `orthus/kg/` 모듈(K2), `kg_outbox`(K3)·`kg_query_runs`(K4)·`kg_entities`/`kg_entity_mentions`(K6) migration |
| 저장소 | Postgres+pgvector(docker, :5433), node-local wiki-store markdown | Neo4j 5.x **Community** 컨테이너 1개 + test 컨테이너 1개 — docker-compose 개정(K1) |
| FE | Next.js(App Router) + Tailwind v4, `/wiki`·`/ask` 기존 surface | K5 related-graph panel — route 신설 없음 |
| 운영 | central Mac mini host-native API/web + docker postgres, launchd scheduler, Makefile target | `make kg-rebuild`/`kg-sync` target(K2), scheduler 합류 |
| 의존성 | uv 관리 | `neo4j` Python driver 1개만 추가. GDS 등 플러그인 도입은 별도 결정 |

기존 코드베이스에서 K-series가 읽거나 확장하는 지점:

```text
orthus/
├─ wiki/store.py          # frontmatter 로더(K2 재사용) + _persist commit 지점 — K3 enqueue hook (§3)
├─ wiki/consolidate.py    # K3 이벤트 원천 — 실제 쓰기는 store.py::_persist를 지난다
├─ router/                # K4b graph 분기 결합 지점 (K4까지는 무변경)
├─ structured/ assistant/ # 검증 게이트 동형 패턴의 원본 (§4)
├─ audit/                 # audit() span — kg.retrieve / kg.apply
├─ api/routes/wiki.py     # K4 GET /wiki/pages/{slug:path}/graph 추가 지점
└─ kg/                    # 신규 — K1: bootstrap.py / K2: schema.py·project.py·store.py
migrations/postgres/versions/  # K3 kg_outbox, K4 kg_query_runs
docker-compose.yml             # K1 neo4j 컨테이너 추가 (K1 머지 완료 — neo4j + neo4j-test 포함)
```

### K1 인프라 상세 계약

docker-compose 서비스(현행 `postgres` 서비스와 동일 파일에 추가; 값은 K1
PR에서 측정 후 조정 가능하되 **loopback 바인딩과 fail-closed는 계약**):

```yaml
neo4j:
  image: neo4j:5-community
  container_name: orthus_neo4j
  ports:
    - "127.0.0.1:7474:7474"   # HTTP — loopback 전용
    - "127.0.0.1:7687:7687"   # bolt — loopback 전용
  environment:
    NEO4J_AUTH: neo4j/${ORTHUS_KG_PASSWORD:?}
    NEO4J_server_memory_heap_initial__size: 256m
    NEO4J_server_memory_heap_max__size: 512m
    NEO4J_server_memory_pagecache_size: 256m
  volumes:
    - orthus_neo4j_data:/data
  healthcheck:
    test: ["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:7474 || exit 1"]

neo4j-test:                    # 테스트 전용 — orthus_test PG 분리 패턴과 동형
  profiles: [test]             # `make up`에는 미포함 — 테스트 실행 시에만 기동
  image: neo4j:5-community
  container_name: orthus_neo4j_test
  ports:
    - "127.0.0.1:7688:7687"
  environment:
    NEO4J_AUTH: neo4j/orthus-kg-test   # 테스트 고정값 — secret 아님
  tmpfs: [/data]               # 테스트 데이터는 휘발
```

`orthus/settings.py` `Settings` 추가 필드(기존 `env_prefix="ORTHUS_"` 규약 —
`owner_scope_enabled`와 동일 패턴):

```text
kg_enabled: bool = False                 # ORTHUS_KG_ENABLED — fail-closed
kg_uri: str = "bolt://127.0.0.1:7687"    # ORTHUS_KG_URI
kg_user: str = "neo4j"                   # ORTHUS_KG_USER
kg_password: str = ""                    # ORTHUS_KG_PASSWORD — keychain secret 우선, .env는 dev fallback
kg_owner_scope_enabled: bool = False     # ORTHUS_KG_OWNER_SCOPE_ENABLED — K7에서 도입
kg_query_timeout_ms: int = 2000          # ORTHUS_KG_QUERY_TIMEOUT_MS — K4 게이트 transaction timeout (§4 기본값)
kg_query_limit: int = 50                 # ORTHUS_KG_QUERY_LIMIT — K4 결과 LIMIT 주입 기본·상한 (§4)
kg_outbox_poll_seconds: int = 5          # ORTHUS_KG_OUTBOX_POLL_SECONDS — K3 worker poll 주기
```

Makefile target(`wiki-rebuild`/`node-wiki-rebuild` 패턴 동형 — root `kg-*`는
local dev 전용이고 **prod central은 `node-kg-* NODE=company`**를 쓴다. 그래프
하나의 SoR DB는 하나여야 하며, env 혼용 시 rebuild prune이 상대 SoR의 투영을
삭제한다. projection 쓰기 경로는 코드 레벨에서도 company node 전용이다 —
K2 리뷰 교정 2026-06-12, 구현 명세 §3.1/`docs/operations.md` §2.1):

```text
kg-bootstrap:  uv run python -m orthus.kg.bootstrap   # §2 constraints/index 멱등 적용
kg-rebuild:    uv run python -m orthus.kg.rebuild     # full projection (§3)
kg-sync:       uv run python -m orthus.kg.sync        # incremental (§3)
node-kg-bootstrap/-rebuild/-sync NODE=company        # prod central 경로 (node env)
```

`make node-smoke` 확장: KG off(`ORTHUS_KG_ENABLED=false`)에서 company/personal
모두 green, KG 관련 import가 lazy(driver 미설치/미기동 시에도 KG-off 경로
무영향)임을 확인한다.

---

## 2. 그래프 스키마 (v1)

본문 없는 "메타 + 관계" 그래프다. 모든 노드가 PG row UUID 또는 wiki slug로
SoR을 가리킨다.

### 노드 라벨

| Label | 설명 | 원천 | 주요 속성 |
|---|---|---|---|
| `:WikiPage` | LLM wiki의 compiled 지식 페이지. 답변 grounding의 단위이자 graph 탐색의 중심 노드 — 모든 읽기 경로 결과는 결국 이 노드로 resolve된다 | `wiki_pages(kind='page')` | `slug`, `title`, `scope`, `project`, `confidence`, `content_hash`, `page_id`, `updated_at`, `last_accessed_at`\* |
| `:WikiClaim` | wiki distill이 추출한 개별 주장. 근거(`SUPPORTS`)와 충돌(`CONFLICTS_WITH`) 관계의 주체로, 충돌 추적 질의가 시작되는 노드 | `wiki_pages(kind='claim')` | `slug`, `title`, `confidence`, `content_hash`, `page_id`, `last_accessed_at`\* |
| `:WikiSource` | claim/page의 근거가 된 wiki source 항목. provenance 체인(claim→source→document)의 중간 고리 | `wiki_pages(kind='source')` | `slug`, `title`, `source_type`, `source_ref`, `content_hash`, `page_id` |
| `:Document` | connector/에디터로 들어온 정규화 원본 문서. provenance 체인의 종점이며 raw 레이어로 되돌아가는 참조점 | `documents` | `doc_id`, `source`(notion/slack/promoted_personal/...), `project`, `source_db_name` |
| `:StructuredFact` | Slack 등 비-Notion 소스에서 추출된 typed fact(연락처/액션/일정/결정/링크). TTL(`valid_until`) 필터 대상 | `structured_rows` | `row_id`, `record_type`(contact/action_item/event/decision/link), `confidence`, `valid_until`\*\* |
| `:Entity` (K6) | LLM이 추출한 인물/조직/프로젝트/시스템 실체. "이 사람이 언급된 지식" 류 탐색의 진입점 — K6에서 substrate 도입(사용자 표면은 K9에서 노출 — PR #496) | LLM 추출 → PG `kg_entities`/`kg_entity_mentions` SoR 적재 후 결정론 투영(rebuild 시 LLM 재호출 없음 — 구현 명세 §9.1). entity sublayer는 **full-rebuild-only**: K3 outbox 경로가 없고 `kg-sync`(증분)도 entity를 건드리지 않는다(co-mention RELATES_TO가 page 전체 mention을 봐야 결정론적이라 부분 투영 회피) — 주기 `kg-rebuild`가 권위로 수렴시킨다. 60초 SLA는 wiki consolidate 전용 | `entity_key`(=`{entity_kind}:{name_norm}`, MERGE 키), `name_norm`, `display_name`(person은 `redact_pii` 통과 후 저장 — §1 carve-out), `entity_kind`(person/org/project/system), `first_seen`, `last_seen` |
| `:Project` | 프로젝트 enum 허브 노드. 프로젝트 단위 묶음 탐색(`IN_PROJECT`)의 기준점 | enum | `atlas`/`nova`/`orbit`/`company` |
| `:OutboxApplied` | K3 outbox 적용 멱등 마커. 지식 노드가 아니라 동기화 메타 — 읽기 경로 템플릿에 노출되지 않는다 | 동기화 멱등 마커 | `outbox_id` (K3에서) |

> \* `last_accessed_at`: 해당 노드가 KG 읽기 경로(K4 page graph API / K4b `/ask` graph 분기) 응답에서 마지막으로 사용된 시각.
> **관측(observability) 전용**이다 — 자율 망각/confidence decay 배치 입력으로 사용하지 않는다.
> KG query 실행 후 `kg_query_runs`에 span 기록 시 **그 응답에 포함된 노드들에** SET한다.
> 단, 템플릿 실행은 `access_mode=READ` 세션이므로(§4) 이 SET은 **질의 종료
> 후 별도의 짧은 WRITE 세션에서 best-effort로** 수행한다 — 실패해도 질의
> 결과에 영향 없고(관측 누락만 발생), 읽기 게이트의 READ 강제와 모순되지
> 않는다.
>
> \*\* `valid_until`: TTL이 있는 사실(일정, 이번 달 예산 등). 만료 후 query filter에서 제외한다 — 제외는 템플릿 Cypher가 아니라 게이트 결과 매퍼 단일 지점에서 수행한다(구현 명세 §6.1; 전 템플릿·향후 템플릿 일괄 적용).
> **자율 삭제·wiki-write 배치로 연결하지 않는다.** Postgres
> `structured_rows.properties->>'valid_until'`에서 projection된 값이다
> (`structured_rows`에 `meta` 컬럼은 없다 — extracted field는 `properties` JSONB).
>
> **`is_active`는 v1 스키마에서 제외한다.** 현행 SoR(`wiki_pages`,
> `structured_rows`)에 lifecycle/archive 컬럼이 존재하지 않으므로, projection
> 시점에 발명하지 않는다(아래 "엣지 저작 원칙"과 동일 원칙을 노드 속성에도
> 적용). SoR에 lifecycle 개념이 생기면 그때 속성을 추가하고 `kg-rebuild`로
> 무손실 반영한다.
>
> **`notion_rows`는 투영하지 않는다.** Notion DB JSONB row store는 structured
> 분기(NL→SQL)의 조회 대상이지 typed fact 추출 결과가 아니다. Notion 행의
> 관계가 그래프에 필요해지면 fact 추출을 거쳐 `structured_rows`로 먼저 적재한
> 뒤 `:StructuredFact`로 반영한다 — 별도 라벨을 만들지 않는다.

### 관계

```text
(:WikiClaim)-[:SUPPORTS]->(:WikiSource)        // wiki_links rel 그대로 이관
(:WikiPage)-[:BACKLINK]->(:WikiPage)
(:WikiClaim)-[:CONFLICTS_WITH {…}]->(:WikiClaim)   // 아래 엣지 속성 참조
(:WikiPage)-[:DERIVED_FROM]->(:WikiSource)
(:WikiSource)-[:EXTRACTED_FROM]->(:Document)   // provenance 체인 연장
(:StructuredFact)-[:EXTRACTED_FROM]->(:Document)
(:WikiPage|Document)-[:IN_PROJECT]->(:Project)
(:Entity)-[:MENTIONED_IN]->(:WikiClaim|WikiPage)          // K6
(:Entity)-[:RELATES_TO {kind, evidence_slug}]->(:Entity)  // K6, evidence 필수
// RELATES_TO는 같은 company page co-mention 쌍에서만 결정론 유도. kind="co_mention"
// (의미 관계를 LLM이 발명하지 않는다 — co-mention 사실만). 무방향 중복(A→B/B→A)
// 방지: entity_key 정렬 후 1방향만 emit. page당 co-mention 폭증은 정렬 prefix cap
// (상수는 구현 명세 §9.2). evidence_slug는 company page slug만(scope join — §3).
```

**`:CONFLICTS_WITH` 엣지 속성 (v1 — 전부 SoR에서 결정론 유도)**

| 속성 | 타입 | v1 원천 |
|---|---|---|
| `detected_at` | datetime | 매칭된 conflict `WikiTask.created_at` |
| `conflict_reason` | str \| null | 매칭된 conflict `WikiTask.description` |
| `status` | enum | `UNRESOLVED` \| `RESOLVED` — `WikiTask.resolved` bool에서 유도 |
| `resolved_favoring_id` | UUID \| null | **v1에서 항상 null.** 현행 `WikiTask`에 채택 claim 정보가 없다 |

SoR 규칙: 충돌은 wiki consolidate가 `wiki_links(rel='conflicts')` +
`WikiTask(kind="conflict")`로 가시화하고, KG는 그 둘을 join해 엣지를
projection한다. KG가 단독으로 충돌 판정을 내리지 않는다.

> **wire format 주의(K4):** 위 "null"은 논리적 의미다. Neo4j는 null 속성을
> 저장하지 않으므로 projection이 None으로 쓴 키(예: `resolved_favoring_id`,
> 매칭 task 없는 `detected_at`/`conflict_reason`)는 그래프에 존재하지 않고 K4
> graph 응답의 `edges[].properties`에서도 **키가 생략**된다(`null`로 나오지
> 않는다). 소비자는 `properties.get(key)`로 읽어야 한다(`KgGraphEdge` docstring).

K2 projection 매핑 규칙:

- 엣지 존재는 `wiki_links(rel='conflicts')`가 결정한다.
- 속성은 `related`에 양쪽 claim slug가 포함된 `WikiTask(kind="conflict")`에서
  채운다. 같은 claim 쌍에 task가 복수면 최신 `created_at` task 우선.
- task의 `kind`/`related`/`resolved`는 PG 컬럼이 아니라 wiki-store task
  markdown frontmatter가 SoR이다(PG `wiki_pages(kind='task')` row는 인덱스).
  K2는 기존 store 로더로 frontmatter를 결정론 파싱해 join한다 — markdown이
  SoR이라는 wiki 원칙(원칙 7) 그대로이며 LLM 호출은 없다.
- 매칭되는 task가 없으면 `status=UNRESOLVED`, `detected_at`/`conflict_reason`은
  null로 둔다. 속성을 발명하지 않는다.
- `RESOLVED_BY_USER`/`RESOLVED_BY_SYSTEM` 구분과 `resolved_favoring_id` 실값은
  `WikiTask` SoR에 해당 필드가 추가된 뒤에만 도입한다 — 그 WikiTask 확장
  자체가 별도 결정이며 K-series 범위가 아니다.

### 제약/인덱스 (멱등 보장 핵심)

```cypher
// 유니크 제약조건 — 정체성 키는 PG PK(page_id/doc_id/row_id)다.
// slug는 정체성 키가 아니다: PG 유니크가 (slug, scope, owner_id) 복합이라
// K7에서 같은 slug의 company/personal page가 공존할 수 있고, Neo4j
// Community에는 복합 유니크(node key)가 없다(Enterprise 전용).
CREATE CONSTRAINT page_node_id IF NOT EXISTS
  FOR (p:WikiPage) REQUIRE p.page_id IS UNIQUE;
CREATE CONSTRAINT claim_page_id IF NOT EXISTS
  FOR (c:WikiClaim) REQUIRE c.page_id IS UNIQUE;
CREATE CONSTRAINT source_page_id IF NOT EXISTS
  FOR (s:WikiSource) REQUIRE s.page_id IS UNIQUE;
CREATE CONSTRAINT doc_id IF NOT EXISTS
  FOR (d:Document) REQUIRE d.doc_id IS UNIQUE;
CREATE CONSTRAINT fact_row_id IF NOT EXISTS
  FOR (f:StructuredFact) REQUIRE f.row_id IS UNIQUE;
CREATE CONSTRAINT project_name IF NOT EXISTS
  FOR (j:Project) REQUIRE j.name IS UNIQUE;
CREATE CONSTRAINT kg_meta_id IF NOT EXISTS
  FOR (m:KgMeta) REQUIRE m.id IS UNIQUE;            // 싱글턴 — id='kg_meta'
CREATE CONSTRAINT outbox_marker IF NOT EXISTS
  FOR (o:OutboxApplied) REQUIRE o.outbox_id IS UNIQUE;
// Entity: 합성 키 entity_key = entity_kind + ':' + name_norm 단일 속성 (K6)
CREATE CONSTRAINT entity_key IF NOT EXISTS
  FOR (e:Entity) REQUIRE e.entity_key IS UNIQUE;        // K6 — additive, KG_SCHEMA_VERSION bump 없음(§4.7).
                                                        // 단 K6 배포 시 kg-bootstrap 선행 필요(신규 constraint)

// 쿼리 성능 인덱스
CREATE INDEX wiki_slug_idx IF NOT EXISTS
  FOR (p:WikiPage) ON (p.slug);                     // 조회용 — 유니크 아님
CREATE INDEX claim_slug_idx IF NOT EXISTS
  FOR (c:WikiClaim) ON (c.slug);
CREATE INDEX fact_record_type IF NOT EXISTS
  FOR (f:StructuredFact) ON (f.record_type);
CREATE INDEX fact_valid_until IF NOT EXISTS
  FOR (f:StructuredFact) ON (f.valid_until);        // TTL 관측/집계용 — v1 질의 TTL 필터는 게이트 결과 매퍼 단일 지점(구현 명세 §6.1)
CREATE INDEX last_access_idx IF NOT EXISTS
  FOR (p:WikiPage) ON (p.last_accessed_at);         // observability 집계용
// 상충 관계 탐색 최적화 (Neo4j 5.x 이상)
CREATE INDEX conflict_status_idx IF NOT EXISTS
  FOR ()-[c:CONFLICTS_WITH]-() ON (c.status);
```

규칙:

- 모든 쓰기는 `MERGE` + diff 키 비교다(라벨별 MERGE/diff 키는 §3 정책 표).
- v1에서 템플릿의 slug 파라미터는 company scope 내에서 resolve한다 —
  company row는 `owner_id` NULL이라 PG 복합 유니크의 부분집합 안에서 slug가
  사실상 유일하다. K7에서 같은 slug의 personal page가 들어오면 slug→노드
  resolve에 caller 네임스페이스(`scope`/`owner_id`)를 함께 적용한다(§4 K7
  계약).
- dangling `[[slug]]`는 wiki_links가 이미 허용하므로 placeholder 노드
  (`:WikiPage {slug, materialized:false}`, `page_id` 없음)로 표현하고 slug로
  MERGE한다. v1 company-only 네임스페이스에서는 안전하다. 이후 실체 page가
  생기면 projection은 **실체 노드를 `page_id`로 새로 만들기 전에 같은
  slug의 placeholder를 먼저 조회해, 그 노드에 `page_id`와 속성을 채우고
  `materialized:true`로 승격**한다 — 같은 slug의 placeholder/실체 노드가
  중복으로 공존하는 상태를 만들지 않는다.

### 엣지 저작 원칙 (제2 저작 경로 금지)

KG는 SoR에 이미 존재하는 관계만 비춘다: `wiki_links`의 rel 4종, documents/
structured_rows/wiki source의 provenance FK, project enum 귀속. SoR에 없는
의미적 엣지(예: "structured_rows의 Slack 결정이 특정 wiki claim을
SUPPORTS한다")를 projection 시점에 발명하지 않는다 — 그 순간 KG가 wiki
파이프라인을 우회하는 제2의 지식 저작 경로가 되기 때문이다. 그런 연결이
필요해지면 distill/consolidate 확장으로 `wiki_links`에 먼저 저작하고(LLM은
추출만, 충돌은 `WikiTask`로 가시화), KG는 그 결과를 비춘다. 엣지의 저작자는
언제나 wiki 파이프라인 하나다.

---

## 3. 동기화 아키텍처 — 2단계 진화

### 1단계 — batch (K2): 결정론 배치 projection

```text
Postgres(wiki_pages, wiki_links, structured_rows, documents) WHERE scope='company'
  + wiki-store markdown frontmatter — PG 인덱스에 없는 SoR 필드:
    task의 kind/related/resolved (CONFLICTS_WITH 속성, §2 매핑 규칙),
    source의 source_type/source_ref (:WikiSource 속성, EXTRACTED_FROM 엣지)
  → orthus/kg/project.py (순수 결정론, 기존 store 로더 재사용, LLM 0회)
  → 라벨별 diff 키 비교(아래 표) → MERGE batch (UNWIND, 1k rows/tx)
  → Neo4j (central 단일 인스턴스)
```

정책:

- v1(K2–K6)의 projection 입력은 `scope='company'` row만이다(P8 정합).
  personal owner-scope row는 SELECT 단계에서 제외하며, 이 필터 자체가
  boundary 회귀 테스트 대상이다. K7에서 owner-scope row까지 확장한다(§4).
- **owner 식별 매핑(현행 스키마 실측)**: `wiki_pages`/`structured_rows`는
  `owner_id` 컬럼, `documents`는 `owner_id` 컬럼이 **없고** personal
  row(`scope='personal'`)의 소유자는 `user_id`다. K7 owner 술어/속성
  projection은 이 매핑을 따른다(P8.1 실구현도 신규 컬럼 없이 기존 컬럼 +
  `ORTHUS_OWNER_SCOPE_ENABLED` flag 기반이다. P8.6 migration이 컬럼을
  정렬하면 그때 본 표를 갱신한다).
- `wiki_links`에는 scope 컬럼이 없다. 엣지의 company 필터는 `src_page_id`
  join으로 src page의 `scope='company'`를 따르고, dst slug도 company page로
  resolve될 때만 실체 노드에 연결한다(미해결 dst는 placeholder 규칙).
- **라벨별 MERGE 키 / 변경 감지(diff) 키**:

  | Label | MERGE 키 | diff 키 |
  |---|---|---|
  | `:WikiPage`/`:WikiClaim`/`:WikiSource` | `page_id` | `content_hash` |
  | `:WikiPage` placeholder | `slug` (실체 생성 시 승격) | — |
  | `:Document` | `doc_id` | `updated_at` |
  | `:StructuredFact` | `row_id` | `updated_at` |
  | `:Project` | `name` | — (enum 고정) |
  | 관계 전체 | (src 키, rel, dst 키) | 소스 row의 diff 키에 종속 |

- **삭제 의미론**: `kg-sync`는 upsert 전용이다(incremental SELECT는 삭제된
  row를 볼 수 없다). 삭제 반영은 ① 주기 `kg-rebuild`(full)가 권위로
  수렴시키고 — rebuild는 SoR에 없는 노드/엣지를 detach-delete한다 —
  ② K3 outbox가 delete 이벤트(`op='delete'`)로 준실시간 보완한다.
- `make kg-rebuild`(full) + `make kg-sync`(incremental,
  `updated_at > last_kg_sync`). 기존 `make wiki-rebuild` 운영 패턴과 동형
  (`uv run python -m orthus.kg.rebuild` / `orthus.kg.sync`, central 전용)이라
  central scheduler에 그대로 합류할 수 있다.
- `last_kg_sync` watermark는 Neo4j 안의 `:KgMeta` 싱글턴 노드
  (`{id:'kg_meta', last_sync_at, last_rebuild_at, kg_schema_version,
  rebuild_lock_until}`)에 저장한다.
  watermark 갱신 값은 **projection 스냅샷 시작 시각**(PG `now()`)이다 —
  처리 완료 시각이 아니다(`kg-rebuild`/`kg-sync` 동일). projection 도중
  commit된 row를 다음 sync가 놓치지 않게 하는 장치이고, 중복 재처리는
  MERGE 멱등으로 무해하다(구현 명세 §4.7 OVERLAP 보정 포함). Neo4j
  volume이 사라지면 watermark도 함께 사라져 자연히 full rebuild로
  수렴한다. Postgres에 KG 상태 컬럼/테이블을 만들지 않는다.
- 이 단계만으로 KG 읽기 경로(K4)까지 출시 가능하다. wiki rebuild가 batch인
  것과 동일한 수용 기준이다.

### 2단계 — outbox (K3): transactional outbox, 준실시간

legacy 내부 문서(비공개) §4.3의 outbox 설계를 재활용한다(이미 검토된
사내 설계 자산, 원천만 persona에서 wiki consolidate/document publish로 교체):

1. wiki consolidate / document publish / promote approve 변경이 **실제로
   PG에 commit되는 지점**에서 같은 트랜잭션으로 `kg_outbox` row를
   enqueue한다(신규 migration; legacy 동명 테이블은 현행 Alembic head에
   없으므로 재정의). 실측(2026-06-11) hook 지점은 wiki 쓰기 공통 commit
   지점인 `wiki/store.py::_persist`(consolidate의 source/claim/page/task
   쓰기 전부가 여길 지난다)와
   `documents.py::publish_agent_draft_document` /
   `documents.py::upsert_source_document`(promote approve는 doc row를
   직접 commit하지 않고 이 함수가 자기 세션에서 commit한다)다 — 구현
   명세 §5.2. enqueue는 `kg_enabled=false`면 no-op이며, flag off 기간의
   변경은 적재하지 않고 flag 활성화 시 rebuild 1회를 운영 절차로
   요구한다. K7 이전에는 enqueue도
   `scope='company'` 변경만 대상이다 — P8.4 이후 central이 owner-scope
   consolidate를 수행해도 그 이벤트는 enqueue하지 않는다(projection 필터와
   동일 경계). K7에서 owner-scope 변경까지 확장한다.
2. `KGOutboxWorker`가 `SELECT ... FOR UPDATE SKIP LOCKED` + lease로 claim한다.
3. 단일 Cypher 트랜잭션: `OutboxApplied(outbox_id)` 마커 존재 확인 → 있으면
   no-op → 변경 적용 → 마커 CREATE.
4. 5회 실패 → `status='dead'` + 운영 가시화. Neo4j 미가용으로 인한
   미적용은 이벤트 실패로 세지 않는다(attempts 미증가 — claim 해제 후
   재기동 시 drain, 구현 명세 §2.6). 크로스-DB 원자성은 두지 않는다 —
   Postgres 권위, Neo4j eventual convergence. 불일치는 언제든 `kg-rebuild`로
   수렴한다.

**`kg_outbox` 스키마 (K3 migration — legacy 동명 테이블과 무관한 신규 정의)**

| Column | Type | Notes |
|---|---|---|
| `outbox_id` | UUID PK | 이벤트 id |
| `entity_kind` | TEXT NOT NULL | `wiki_page` \| `document` \| `structured_row` |
| `entity_id` | UUID NOT NULL | 해당 PG row PK |
| `op` | TEXT NOT NULL | `upsert` \| `delete` |
| `status` | TEXT NOT NULL DEFAULT `pending` | `pending` \| `applied` \| `dead` |
| `attempts` | INT NOT NULL DEFAULT 0 | 실패 횟수 — 5회에서 `dead` |
| `lease_until` | TIMESTAMPTZ NULL | worker claim lease |
| `last_error` | TEXT NULL | 마지막 실패 사유 |
| `correlation_id` | UUID NULL | audit 전파 |
| `enqueued_at` | TIMESTAMPTZ DEFAULT now() | enqueue 시각 |

- Index: `idx_kg_outbox_status_enqueued(status, enqueued_at)`.
- `wiki_links` 변경은 별도 entity_kind가 아니다 — src page의 `wiki_page`
  upsert 이벤트 처리 시 그 page의 링크 엣지를 통째로 재투영한다(엣지 MERGE
  키가 (src, rel, dst)라 멱등).

### 장애 모드 (fail-open이 원칙)

| 상황 | 동작 |
|---|---|
| Neo4j down | `/ask` wiki/structured 분기 무영향. graph 분기만 "KG unavailable" 표시. outbox는 적체 후 재기동 시 drain |
| projection 도중 실패 | 트랜잭션 단위 롤백, 다음 tick 재시도. 부분 적용은 MERGE 멱등으로 무해 |
| 스키마 drift 의심 | `kg-rebuild`가 항상 ground truth 복원 |

---

## 4. 읽기 경로 — page graph API(K4) → 라우터 `graph` 분기(K4b)

### Cypher 안전 모델 (structured 게이트와 동형)

LLM의 자유 Cypher 생성은 하지 않는다. 대신:

1. **Typed query template registry** — 코드에 등록된 파라미터화 템플릿만 실행:
   - `neighbors(slug, depth≤2)` — 페이지 주변 관계
   - `path_between(slug_a, slug_b, max_hops≤4)` — "A와 B가 무슨 관계야?"
   - `conflicts_of(slug)` — 충돌 claim 추적
   - `provenance_chain(slug)` — claim→source→document 근거 체인
   - `entity_mentions(name_norm)` (K6)
   - (v1 이후 추가 등록 — 2026-07-05 현재 `orthus/kg/templates.py` registry)
     `path_between_company`(K7.4 two-framing, PR #360), `page_conflicts(slug)`
     (K8, PR #373), `entity_neighbors(slug)`(K9, PR #496),
     `expand_node(label,id)`/`expand_entity(entity_key)`(그래프 탐색기 E1,
     PR #569) + K7.2 owner-variant(전 owner 템플릿, PR #340)
2. LLM/라우터는 어떤 템플릿에 어떤 파라미터를 바인딩할지만 결정한다
   (structured 분기에서 NL→SQL 후 게이트 통과와 같은 역할 분담, 단 더 보수적).
3. 이중 방어: driver `access_mode=READ` 세션 강제 + 서버측 transaction
   timeout + 결과 LIMIT 주입 + depth/hop 상한 하드코딩. Neo4j Community
   Edition에는 PG `orthus_ro` 같은 role 기반 read-only 계정이 없으므로
   (role/grant는 Enterprise 전용), DB 레벨 계정 분리가 빠지는 만큼 게이트
   reject 회귀 세트(K4 Verify)가 이를 보완한다.
4. 모든 실행은 신규 `kg_query_runs` 테이블(K4 migration; 기존 structured
   `query_runs`는 확장하지 않는다)에 기록하고 `audit("kg.retrieve")` span +
   `correlation_id`를 전파한다(`audit(node, correlation_id=...)` 기존 시그니처
   재사용).

**`kg_query_runs` 스키마 (K4 migration)**

| Column | Type | Notes |
|---|---|---|
| `run_id` | UUID PK | 실행 id |
| `template_name` | TEXT NOT NULL | §4 registry의 템플릿 이름 |
| `params_redacted` | JSONB NOT NULL DEFAULT `{}` | `redact_pii_text()` 통과한 파라미터 |
| `status` | TEXT NOT NULL | `ok` \| `rejected` \| `timeout` \| `error` |
| `reject_reason` | TEXT NULL | reject 시 사유(게이트 회귀가 검증) |
| `duration_ms` | INT NULL | 실행 시간 |
| `result_count` | INT NULL | 반환 노드/행 수 |
| `user_id` | UUID NULL | 호출자 |
| `correlation_id` | UUID NULL | audit 전파 |
| `created_at` | TIMESTAMPTZ DEFAULT now() | 기록 시각 |

- Index: `idx_kg_query_runs_created(created_at)`,
  `idx_kg_query_runs_template(template_name)`.

### 템플릿 reference Cypher (v1)

아래는 계약 수준의 reference다 — 실제 실행은 공통 래퍼(READ 세션,
transaction timeout 기본 2초, 서버측 `$limit` 주입: 기본 50·상한 50,
depth/hop 상한 하드코딩)를 거친다. 기본 수치는 K4에서 측정 후 본 문서
갱신과 함께 조정할 수 있되 **상한 완화는 별도 결정**이다. Cypher는 가변 길이 패턴의 상한에 파라미터를 허용하지 않으므로,
`depth`/`max_hops`는 **사전 컴파일된 쿼리 문자열 중 선택**으로 구현한다
(상한 하드코딩 원칙과 합치).

```cypher
// neighbors(slug, depth ∈ {1,2}) — 아래는 depth=2 변형.
// depth=1은 [*1..1]로 사전 컴파일된 별도 쿼리 문자열이다(파라미터 아님).
// rel allowlist에서 IN_PROJECT는 제외한다 — K4 측정 결정(2026-06-12):
// :Project 허브(4노드, 수백 page 연결)를 지나는 depth=2 탐색은 LIMIT 50
// 결과가 100%(50/50 row) 같은 프로젝트 형제 page로 도배돼 지식 관계
// (SUPPORTS/DERIVED_FROM/...)가 전부 밀려난다. project 귀속은 노드
// props(project)로 이미 노출되므로 정보 손실이 없다. 재포함은 별도 결정.
MATCH path = (p:WikiPage {slug:$slug})
  -[:SUPPORTS|CONFLICTS_WITH|BACKLINK|DERIVED_FROM|EXTRACTED_FROM*1..2]-(n)
RETURN path LIMIT $limit;
// RETURN이 (p, r, n)이 아니라 path인 이유(구현 교정, 계약 의미 불변):
// 가변 길이 패턴의 중간 hop 노드는 변수에 바인딩되지 않아 driver가 속성
// 없는 stub으로 hydrate한다 — Bolt Path 구조만 경로 위 전체 노드를 속성
// 포함으로 전달한다(구현 명세 §6.1 결과 매핑).

// path_between(slug_a, slug_b, max_hops ≤ 4)
// neighbors와 같은 지식 rel allowlist를 쓴다 — IN_PROJECT 제외(K6 PR3, 2026-06-12):
// 허브를 타면 같은 프로젝트의 임의 두 page가 항상 a→:Project→b 2-hop으로 "연결"돼
// 진짜 지식 연결 부재(missing_link 신호)를 가린다. project 귀속은 노드 props로 노출됨.
MATCH path = shortestPath(
  (a:WikiPage {slug:$slug_a})
    -[:SUPPORTS|CONFLICTS_WITH|BACKLINK|DERIVED_FROM|EXTRACTED_FROM*..4]-(b:WikiPage {slug:$slug_b}))
RETURN path LIMIT $limit;

// conflicts_of(slug)
MATCH (c:WikiClaim {slug:$slug})-[k:CONFLICTS_WITH]-(o:WikiClaim)
RETURN c, k, o LIMIT $limit;

// provenance_chain(slug) — 관계 변수(sr/er)는 응답 edges 구성용(의미 불변).
MATCH (c:WikiClaim {slug:$slug})-[sr:SUPPORTS]->(s:WikiSource)
        -[er:EXTRACTED_FROM]->(d:Document)
RETURN c, sr, s, er, d LIMIT $limit;
```

### K4 API 응답 계약

```text
GET /wiki/pages/{slug:path}/graph?depth=1|2      # 기본 1, 최대 2
```

- 200 (KG on, Neo4j up):

  ```json
  {
    "slug": "...", "supported": true, "truncated": false,
    "nodes": [{"id": "<page_id|doc_id|row_id|slug>", "label": "WikiPage",
               "slug": "...", "title": "...", "materialized": true}],
    "edges": [{"src": "<id>", "dst": "<id>", "rel": "BACKLINK",
               "properties": {}}]
  }
  ```

- 200 (KG off 또는 Neo4j down — fail-open, P4 unsupported-state 패턴):

  ```json
  {"slug": "...", "supported": false,
   "reason": "kg_disabled" | "kg_unavailable", "nodes": [], "edges": []}
  ```

- 404: 현재 node-local company wiki page로 resolve되지 않는 slug.
- P8.8 cutover 전의 personal node runtime에서는 이 endpoint가 항상
  `supported:false`/`kg_disabled`다 — personal node에는 Neo4j가 없다(§1).
  404가 아니라 unsupported로 응답하는 것이 P4 federated-page 패턴과 동형이다.
- `$limit`은 Cypher row(경로) 기준이다 — row 수가 limit(50)에 도달하면
  잘라내고 `truncated: true`로 표시한다. 응답 노드 수는 dedupe 후라 row
  수 이하일 수 있다(K4 구현 명확화 — 수치 계약 불변).
- raw 본문은 응답에 없다 — `slug`/`title`/메타만. 본문은 기존
  `GET /wiki/pages/{slug:path}`로 되돌아간다(불변식 5).

### Grounding 합성 (불변식 5 보존 장치)

```text
graph 분기 응답 = 경로/관계 요약 + 경로 위 각 노드를 wiki page로 resolve
                → 답변 텍스트는 resolve된 compiled wiki page 본문에서 grounding
                → RoutedAnswer.sources = WikiSourceRef[] (기존 P4.2 wiki_links 계약 재사용)
```

KG는 근거 후보를 찾는 탐색만 담당하고, 답변 본문은 항상 compiled wiki
page에서 나온다. 이 분리로 불변식 5가 보존된다.

### Personal 데이터와 graph — K7 owner-scope graph (federation 항목 폐기)

K0 초안의 `GET /federation/kg/query`는 채택하지 않는다. P8이 federation
plane 자체를 central 단일 `/ask`/wiki merge로 대체하며(P8.4, P8.8에서 plane
제거), KG는 central runtime 안에서만 조회된다. v1(K2–K6) KG는 company
scope만 투영하므로 owner의 merged view에서도 graph 분기/패널은 company
관계만 보여준다 — 이는 K7 이전의 의도된 fail-closed 경계다.

personal owner-scope row의 투영은 **K7**에서 연다(2026-06-10 사용자 결정 —
owner별 인스턴스 분리/PG CTE 대안 대신 단일 central 그래프 + owner filter
강제 채택). K7 계약:

- **속성**: 모든 노드/엣지에 `scope`, `owner_id`를 projection한다. company
  row는 `owner_id` null, personal row는 owner UUID — 단 `documents`는
  `owner_id` 컬럼이 없어 personal row의 `user_id`를 owner로 매핑한다(§3
  owner 식별 매핑 표). P8.1 owner predicate와 동일 경계 철학이다.
- **slug resolve 네임스페이스**: K7부터 같은 slug의 company/personal page가
  공존할 수 있다(PG 유니크가 `(slug, scope, owner_id)` 복합 — §2). 템플릿의
  slug 파라미터는 caller 기준 (company) ∪ (caller 본인 personal)
  네임스페이스에서 resolve하고, 충돌 시 caller 본인 personal page를
  우선한다.
- **전 템플릿 owner 술어 강제**: 모든 Cypher 템플릿에
  `(n.scope='company' OR n.owner_id=$caller)` 술어를 포함한다. 술어는 시작
  노드만이 아니라 **패턴에 바인딩되는 모든 노드와 관계에**(가변 길이 경로의
  중간 hop 포함) 적용한다 — 아래 경로 가시성 규칙의 쿼리 수준 구현이다.
  `$caller`는 세션에서만 유도하고 클라이언트 입력으로 받지 않는다.
- **경로 가시성 규칙**: 결과에 포함되는 경로/이웃/체인의 **모든** 노드·엣지가
  caller-visible일 때만 반환한다. 중간 노드가 비가시면 부분 마스킹이 아니라
  경로 전체를 drop한다 — 경로 요약만으로 personal 노드의 존재가 새는 것을
  막는다.
- **cross-scope 엣지**: personal claim → company page backlink처럼 양쪽
  scope가 다른 엣지는 personal 쪽 owner 전용으로 취급한다(더 제한적인 쪽
  우선).
- **flag**: `ORTHUS_KG_OWNER_SCOPE_ENABLED=false` fail-closed로 개방한다
  (P6/P8 feature-flag 패턴).
- Neo4j Community에는 row-level security가 없으므로 이 경계는 전부 앱
  레이어 증명이다 — K7 Verify의 템플릿×역할 boundary 회귀와 path-leak
  테스트가 필수다(§5 K7 행).

#### B1–B6 — owner 경계 invariant (정규 계약, K7.1 도입)

RLS가 없으므로 **테스트 매트릭스가 곧 경계다.** 아래 invariant는 owner-variant
템플릿/projection이 반드시 만족해야 하는 정규 계약이며, 단일 출처는
`orthus/kg/visibility.py` 모듈 docstring이다(미래 P7.3 mail-linking / sharing 확장은
이 표를 가법적으로만 넓힌다). 각 invariant는 "막는 누출"과 "증명 테스트"를 동반한다.

| ID | Invariant | 막는 누출 | 증명 |
|----|-----------|-----------|------|
| **B1** | owner read 술어가 owner-variant 템플릿의 **모든 bound 노드 AND 모든 bound 관계**에 존재(엣지가 바인드 가능한 건 projection이 엣지에도 `owner_id`를 싣기 때문 — §4 속성). | 타 user에게 도달하는 외부 personal 노드/엣지. | `test_k7_path_leak_dropped_not_masked`(owner는 자기 personal 노드+엣지 순회, 비-owner/admin/null은 0), `test_k7_non_owner_no_foreign_personal_neighbor`, `test_predicate_fragment_byte_identical_across_owner_templates`, `test_owner_templates_carry_predicate_on_every_bound_var` |
| **B2** | `truncated`/count는 **Cypher 필터 후 행에서만** 계산 — Python post-filter 금지. | count/절단이 숨은 행을 드러냄. | gate가 driver 실행(=Cypher 술어 적용) 후 `truncated = len(records)==limit` 계산(구조적); `test_k7_path_leak_dropped_not_masked` 등 boundary 회귀가 행 수를 검증 |
| **B3** | 엣지 scope = **더 제한적인 끝점**(personal 우선). 엣지는 `scope`(min-of-endpoints) AND `owner_id`(personal 끝점 owner)를 둘 다 싣는다. | company처럼 보이는 엣지가 personal 끝점 존재를 노출; owner 자신의 personal 엣지가 조용히 탈락. | projection: `test_edge_scope_owner_personal_wins`, `test_personal_link_resolves_to_company_not_other_owner`; read/wire: `test_map_records_edge_scope_set_and_owner_id_stripped`, `test_map_records_drops_foreign_personal_edge` |
| **B4** | placeholder 정체성은 **ns-keyed**(`ns_key`); cross-owner 동일 slug placeholder는 merge 불가; v2 후 `page_id IS NULL AND ns_slug IS NULL` 잔존 0. placeholder DELETE도 ns_key로 키잉. | 공유 placeholder slug를 통한 cross-owner 존재 오라클; stale v1 placeholder가 `scope='company'`로 위장. | `test_placeholder_ns_separation_cross_owner`, store.prune v1 purge + monitor `stale_v1_placeholders` tripwire; read dedup: `test_map_records_scope_aware_dedup_same_slug_diff_scope` |
| **B5** | 외부 slug resolve는 truly-absent와 **byte-identical·timing·audit 동일** 404(단일 코드 경로). | 응답/타이밍/audit diff를 통한 존재 오라클. | `test_k7_foreign_start_node_404_equals_absent`, `test_k7_foreign_probe_query_run_row_shape_equivalent`(kg_query_runs 행 동형) |
| **B6** | start-node 가드: 추측한 외부 `page_id`는 에러가 아니라 **빈 결과**(resolve None → `slug_not_found` reject, 동일 코드 경로). | 시작 노드 에러-shape diff를 통한 존재 오라클. | `test_k7_foreign_start_node_404_equals_absent` |

추가(별도 B-id 없이 매트릭스에 접힘): **B-admin** — `admin` 역할은 자기 owner-scope
외 KG 가시성을 **추가로 얻지 않는다**(`visibility_predicate`는 역할을 보지 않고
`(var, caller)`만 받음; 미래 sharing의 `$visible_ids`도 admin이 auto-populate 금지).
증명 `test_k7_admin_no_role_bypass`. **B-null-caller** — `$caller`가 null/부재
(미인증·demo·collector token)면 owner 술어는 **company-only**로 수렴한다(Cypher null
비교 의미에 의존하며 테스트로 고정). 증명 `test_k7_null_caller_sees_company_only`.

**K7.2 read-path 구현 노트(2026-06-16 확정):**
- **resolve_slug는 PG다(L2, 코드리뷰 결정).** start-node slug→(page_id, scope) resolve는
  `orthus/kg/visibility.py::resolve_slug`가 **PG `wiki_pages`** owner-inclusive 쿼리로 한다
  (Cypher 아님). 근거: fail-open(Neo4j 다운에도 동작) + "Neo4j는 SoR 아님" 불변식.
  resolve된 page_id(문자열)를 owner-variant 템플릿 시작노드(`{page_id:$page_id}`)에
  바인딩한다(slug 바인딩의 cross-owner 동명 fanout 차단, B4). B5 등가는 단일 PG 쿼리로
  보장된다(foreign-only와 absent가 같은 쿼리로 None → 동일 404).
- **$caller는 세션 전용·문자열 바인딩.** gate는 `AuthenticatedUser.user_id`만 `$caller`로
  쓰고(client 입력 금지), Neo4j가 owner_id/page_id를 문자열로 저장하므로 `str()`로 바인딩한다.
- **L3 inter-PR fail-closed.** owner-scope flag-on인데 게이트 템플릿이 owner-variant가
  아니면(빌드/배포 불일치) lifespan `verify_owner_scope_gate_consistency()`가 KG를 process
  수준에서 강제 off(`kg.owner_scope_mismatch` audit)한다 — refuse-boot 아님, 모든 KG 경로가
  `supported:false`로 수렴해 개인 row 비노출. 판정 단일 출처는 `templates.owner_variants_present()`.
- **L4 개인 slug 로깅 = hash/생략.** 개인-scope로 resolve된 slug 값은 `kg_query_runs`에
  raw 제목이 아니라 `personal:<hash>`로 기록한다(company는 raw, 404는 caller 입력 그대로 →
  B5 등가). 개인 메모 제목이 admin-readable 로그에 raw로 남는 창을 차단한다(K6 carve-out
  raw 허용에서 강화).
- **deferred-template code-guard.** `entity_mentions`는 D2(entity_key에 owner 성분 없음)로
  owner-variant가 없다. K7.2 시점에는 owner-scope 활성 시 gate가 `deferred_template`로
  fail-closed reject해 소비자 경로를 차단했다(false-green/미래 누수 방지; flag-off K6
  substrate 동작은 불변). **K9 개정(PR #496):** `entity_mentions`는 deferred→`company_always`로
  재분류돼 `/ask` entity intent wiring이 허용됐다 — owner-scope ON에서도 gate stage-4가
  company-fork를 강제해($caller 미바인딩) 안전하게 서빙된다. 가드 자체는 유지되며 현재
  `deferred` 템플릿은 없다(`orthus/kg/gate.py` 2b 가드 주석).
- **wire 노드/엣지는 `scope`/`is_own_personal`(서버 파생)만 노출하고 `owner_id`는 절대 싣지
  않는다.** `_map_records`가 독립 tripwire로 `scope=='personal' AND owner_id!=caller` 노드/
  엣지를 drop + `kg.boundary_violation` audit한다(predicate가 옳으면 0인 must-be-zero 방어선).

**exact-scope write(B1의 write-twin):** outbox worker DELETE는 읽기 술어(`company OR
owner=$caller`)가 아니라 **enqueued 이벤트의 scope/owner_id**로 키잉한다. **personal
delete만** owner WHERE(`scope='personal' AND owner_id=$owner`)를 박아 그 owner 노드만
detach한다(읽기의 `company OR this-owner`가 아니다) — owner 빈 personal 이벤트는 매칭 0.
**company delete는 유니크 키만으로 무가드 DETACH**다(코드리뷰 #1): page_id/doc_id/row_id가
전역 유니크라 cross-scope 오삭제가 구조적으로 불가능하고, `WHERE n.scope='company'`를 박으면
v1·v2 혼합 그래프의 scope-없는 v1 노드(:Document/:StructuredFact/:Project/placeholder)에서
`null='company'`→0 매칭으로 삭제가 조용히 실패해 노드가 부활한다(version-NULL 활성화 구간엔
version-hold가 못 막음). company scope WHERE는 순이익이 없어 제거했다
(`test_delete_company_event_is_unconditional`, `test_delete_personal_event_exact_owner_scope`).

**prod 방어(RLS 없음):** `kg_monitor_summary`가 4개 must-be-zero tripwire(B-shape
node/edge, B4 stale-v1 placeholder, B3 over-permissive 엣지)를 capped LIMIT-1 probe로
측정하고, `python -m orthus.kg.monitor`는 위반 시 exit 3, 검증 불가(쿼리 에러/Neo4j
미가용 while owner-scope on) 시 exit 4로 fail-closed 종료한다(could-not-verify ≠
violation-found). **boundary는 flag 상태와 무관하게 측정한다(코드리뷰 #3)** — owner-scope
**off**면 네 tripwire에 더해 personal 노드/엣지 잔존(`residual_personal`, presence probe)을
위반으로 본다(company-only 모드엔 personal 데이터가 0이어야 하므로 rollback 잔여를 잡는다).
flag-off의 추가 비용은 LIMIT-1 probe 둘뿐이고(split full-count는 flag-on에서만), KG 자체가
off(`ORTHUS_KG_ENABLED=false`)거나 Neo4j 미가용이면 PG-only fail-open으로 boundary 측정을
건너뛴다. **could-not-verify(exit 4)는 owner-scope ON 전용이다(코드리뷰 R2):** flag-off의
boundary는 best-effort 잔여 검사라, 확인된 잔여(probe 성공 + `residual_personal`>0)는 exit
3로 올리되 검증 실패(probe 에러)는 advisory로 두고 exit를 안 올린다 — 안 그러면 flag-off에서
일시 probe 에러(exit 4)가 Neo4j 완전 다운(exit 0)보다 심하게 처리되는 비대칭이 생긴다.

#### ADR — entity-key 접두 슬롯(K7.1, projection-only)

K7은 owner-접두 entity-key **shape**(`<scope-prefix>:kind:name_norm`)을 예약하되,
cross-owner entity merge/dedup 규칙은 **정의하지 않는다**(personal-entity 슬라이스로
미룸 — 그 슬라이스가 per-source/shared-merge 키잉을 개정할 수 있다). D2 아래 entity는
company-only라 모든 K7 row 값이 literally `company:`이고 merge/정체성 의미는 0이다.
접두는 **projection-only**다: Neo4j 노드 키/엣지 끝점/`store.delete_entity_nodes` MATCH는
접두 키를 쓰고(단일 변환 `visibility.entity_node_key_for` 경유 — erasure가 접두 노드를
놓치면 PII 잔존), PG `kg_entities.entity_key`는 `kind:name_norm`로 유지한다(데이터
마이그레이션 없음). 미룬 이유: 같은 실세계 entity가 owner마다 등장하는 것(예: 두
사람의 노트 속 '김대표')은 가치 있는 다리이자 누출 위험이라, merge 규칙은 의도된 미래
설계지 동결된 기본값이 아니다.

#### P7.3 inherits-pointer (mail 지식연결 합류 — 경량 포인터, 정규 계약 아님)

P7 mail 지식연결(P7.3)은 K7 owner-scope graph 위에 합류한다. **상속(K7.5에서 잠금):** P7.3은
K7의 **B1–B6 owner 경계**(§4 매트릭스, 비-owner/admin 비노출 + path-leak drop)와 **EXTRACTED_FROM
provenance shape**(`provenance_chain` 템플릿, claim→source→document 근거 체인)를 그대로 상속한다 —
owner 경계 모델 밖에서 과제약하지 않는다(P7.3 미구현이라 node-type/write-path 신설은 **P7.3 PR이
결정**한다). entity-key merge 규칙은 위 "ADR — entity-key 접두 슬롯"이 SoR다(재서술하지 않는다 —
personal-entity 슬라이스가 개정). `is_own_personal`의 2-state→3-state widening(공유 가시성)은 §8
sharing 슬라이스 breadcrumb다(`$visible_ids` proof harness와 함께). 이 포인터는 dangling "합류
지점" 참조(구 §5 K6 행)를 대체하는 **경량 방향 표시**이지 정규 P7.3 계약이 아니다.

#### KG_SCHEMA_VERSION v2 활성화 절차(breaking — placeholder 재키잉)

v2는 placeholder MERGE 키를 slug → `ns_key`로 바꾸므로 rebuild 강제 대상이다(scope/
owner_id 속성 추가 자체는 additive지만 같은 bump에 탑승). 절차: 코드 머지(flag off, v2
포함) → `kg-sync`는 version 불일치로 자동 거부 + outbox worker는 version-hold(둘 다
fail-closed) → `make node-kg-rebuild NODE=company`(placeholder ns 재키잉 + scope props
소급 + entity 접두) → `ORTHUS_KG_OWNER_SCOPE_ENABLED=true` → 재기동. **rebuild가 flag보다
먼저**다(`docs/operations.md` §2.1).

**비활성화(rollback) 절차:** flag를 끄면 SELECT/enqueue/worker는 company-only로 즉시
돌아가지만(동작 불변), flag-on 기간에 이미 투영된 personal 노드/엣지는 자동으로 사라지지
않는다 — sync(증분 upsert)는 prune을 안 한다. **단, monitor는 이제 flag-off에서도
`residual_personal`로 잔여를 감시한다(코드리뷰 #3)** — rollback 직후 personal 노드가 남아
있으면 boundary violation(exit 3)으로 가시화된다(이전엔 boundary=None/exit 0으로 무감시).
잔여를 실제로 제거하려면 **flag off 직후 `make node-kg-rebuild`를 1회 돌려** company-only
ExpectedKeys로 personal 노드를 prune한다. 그때까지 monitor가 violation을 계속 보고하므로
운영자가 rebuild 누락을 놓치지 않는다.

**erase(PII 삭제)와 v2 transient:** 동기 erasure 경로(`erase.py` → `store.delete_entity_nodes`)는
v2부터 `company:` 접두 키로 :Entity를 MATCH한다. v2 코드 머지 후 **rebuild 전**(그래프
:Entity가 아직 unprefixed)에 erasure가 돌면 접두 MATCH가 0건이라 그 시점엔 노드를 못 지운다 —
다만 PG row가 삭제됐으므로 **다음 full rebuild의 prune이 orphan :Entity를 회수**한다(기존
안전망, `test_erasure_orphan_entity_recovered_by_rebuild_not_rerun`). rebuild-before-flag
절차를 지키면 이 창은 생기지 않는다. 동기 erase는 outbox worker의 version-hold로 보호되지
않으므로 이 의존성을 명시한다.

---

## 5. 마일스톤 (K0–K7 + K8/K9 후속) — 레포 슬라이스 컨벤션 준수

> 상태 열(2026-07-05 확인): ✅ = main 머지 완료, ⏳ = 미머지 브랜치 작업 있음, ❌ = 미착수.

| ID | 내용 | Verify | 비고 | 상태 |
|---|---|---|---|---|
| **K0** spec lock | 본 문서 canonical 확정 + system-spec Non-goals/절대금지·roadmap hold·AGENTS hard constraint 개정("KG는 K-spec 범위 내 허용") + data-model.md 개정(§5.1) + acceptance criteria | `make docs-check`, `orthus-operator-reviewer` | **코드 0줄.** P6.0과 동일 패턴 | ✅ |
| **K1** infra + 스키마 | central runtime에 neo4j 컨테이너 **1개 신규 추가**(현행 compose는 postgres 단독; bolt `7687`/HTTP `7474` loopback only, volume 분리) + test 전용 컨테이너(별도 포트 `7688`), `ORTHUS_KG_ENABLED=false` fail-closed + `ORTHUS_KG_URI`/`ORTHUS_KG_USER` env(password는 keychain `secret_ref`, `.env`는 bootstrap/dev fallback — connector secret 패턴 동일), constraints/index 부트스트랩(`orthus/kg/bootstrap.py` + `make kg-bootstrap` — §1 K1 상세 계약) | `make node-smoke` 확장 (KG off일 때도 전부 green, personal node는 KG 부재 확인) | flag off가 default — P6 feature-flag 패턴. **personal node에는 Neo4j를 추가하지 않는다** | ✅ |
| **K2** 결정론 projection | `orthus/kg/{schema,project,store}.py`, **company scope** wiki/structured/document → MERGE, `make kg-rebuild`/`kg-sync`(`:KgMeta` watermark), LLM 0회 | projection 멱등 테스트, PG↔Neo4j count parity 테스트(company scope 기준), 2회 실행 무변경, **owner-scope row 미투영 boundary 회귀** | **여기까지가 v1 코어** | ✅ |
| **K3** outbox 준실시간 | `kg_outbox` migration, consolidate/publish/promote-approve enqueue, worker + lease + dead-letter, `audit("kg.apply")` | outbox 멱등/replay/dead-letter 테스트, node-smoke | legacy §4.3 설계 재사용 | ✅ |
| **K4** 읽기 경로 | 템플릿 registry + read-only 게이트 + **`GET /wiki/pages/{slug:path}/graph` read-only API**(P4.3/P4.5 page-단위 GET 패턴 동형) + provenance resolve + `kg.retrieve` audit. **라우터는 건드리지 않는다** | **게이트 reject 회귀 세트**(미등록 템플릿/파라미터·깊이 초과/timeout 런타임 reject + 등록 Cypher write-키워드 정적 검사 — structured 5-reject와 동형, §8 수용 기준 4) | grounding은 wiki page 본문 | ✅ |
| **K4b** `/ask` graph 분기 | K4 게이트/템플릿/audit 기반 위에 `/ask` 라우터 `graph` 분기 추가(관계형 질문 확신 시에만, 아니면 wiki fallback) + grounding 합성. 분기 판단 방식(rule + LLM 분류 결합)은 K4b PR에서 명문화하되, **LLM confidence-only routing 금지**(AGENTS 절대 규칙)와 충돌하지 않아야 한다 | 라우팅 통합 테스트(기존 wiki/structured 분기 무회귀), fail-open 테스트(Neo4j down → 기존 분기 정상) | K4와 분리된 후속 PR — 라우터 리스크 격리 | ✅ main 머지(2026-06-15, PR #333) — `Route`에 `graph` 추가(`_GRAPH_TERMS` rule-우선 + LLM enum), `orthus/router/graph.py`(bind/resolve/`try_graph_answer` blanket fail-open), company node + `scope∈{company,all}` + `kg_available` 3중 가드, demote 시 단일 기존 wiki dispatch fall-through(+reject_reason warning), grounding은 `retrieve(page_slugs=...)` 제한(불변식 5), `RoutedAnswer.graph`는 typed `KgGraphNode/Edge` 재사용(§7.3 개정). 기존 5 router 무회귀 + 8 신규 회귀 |
| **K5** FE 가시화 | `/wiki/{slug}` related-graph panel (K4 API 소비, P4.3 cross-link panel 패턴, read-only, capped 20), `/ask` graph 답변 `wiki_links` 동반(K4b 이후). 1차 시각화는 rel별 그룹 칩 리스트(신규 FE 의존성 0, 인접 1–2 hop) — 그래프 캔버스/미니맵 라이브러리 도입은 별도 결정(구현 명세 §8.1) | browser QA 390×844 (P5 mobile 계약 준수), FE lint/build | route 신설 없음, P4 additive 원칙. panel은 K4만으로 착수 가능 | ✅ main 머지(2026-06-15, PR #335) — `web/src/components/wiki/graph-ring-panel.tsx` 결정론 radial hop-ring + `web/src/app/wiki/[...slug]/page.tsx::RelatedGraphPanel`(graph/list 토글, capped 20, P5 모바일 390×844). 인터랙티브 노드 확장 explorer는 그래프 탐색기 E-series로 main 머지 완료(2026-07-05 확인 — E1 backend expand PR #569, E2 FE d3-force 탐색기 PR #584, E3 모바일/a11y QA PR #586, 내부 문서(비공개)) |
| **K6** entity layer + hardening | distill 확장으로 entity 추출(LLM은 추출만, 코드가 dedupe, 충돌→`WikiTask(kind="entity_conflict")`), entity 그래프 substrate(`entity_mentions` 템플릿 등록 + projection; **사용자 노출 endpoint/`/ask` intent는 K4b 후속**, §9.4 신규 route 금지), erasure 전파(operations §8.4에 Neo4j detach-delete + `kg_entities`/`kg_entity_mentions` + person 절차), KG 기반 `data_gaps.reason="missing_link"` 감지(결정론, LLM 0회, 동기-가드 §5.2), rebuild drill, 모니터링 | entity 멱등/`entity_conflict` 태스크 테스트(+conflict-task 인덱스 비오염), owner-scope 미투영 boundary 회귀 유지(entity 포함), missing_link 감지 결정론 테스트, person-entity redaction 테스트 | **배송: 4-way 순차 PR**(substrate → distill → missing_link → erasure/ops, 독립 rollback). P7 mail 지식연결(P7.3)은 §4 "P7.3 inherits-pointer"로 합류(B1–B6 경계 + EXTRACTED_FROM provenance 상속). ~~`/federation/kg/query`~~는 P8 정합으로 폐기 | ✅ main(4-way: substrate PR #285/distill PR #286/missing_link PR #296/erasure-ops PR #297) — 완료. entity 사용자 표면(`entity_neighbors`/`entity_mentions` 노출 + `/ask` entity intent)은 K9에서 노출 완료(2026-06-24, PR #496) |
| **K7** owner-scope graph | projection/outbox를 personal owner-scope row까지 확장, 모든 노드/엣지에 `scope`/`owner_id` 속성, **전 템플릿 owner 술어 강제** + **경로 가시성 규칙**(중간 노드 누출 금지, §4 계약), cross-scope 엣지는 personal 쪽 owner 전용, `ORTHUS_KG_OWNER_SCOPE_ENABLED=false` fail-closed flag | **템플릿×역할 boundary 회귀**(타 user/admin 세션에서 personal 노드/엣지/경로 비노출), **path-leak 테스트**(부분 마스킹 없이 경로 drop), flag on 상태에서도 비-owner 세션의 결과는 K7 이전과 동일(기존 회귀 무변화), owner erasure 시 해당 owner의 personal 그래프 노드/엣지 detach-delete | 2026-06-10 사용자 결정. **K4 게이트/회귀 인프라 검증 후 착수.** P8 owner-only row-level 경계와 동일 철학 | ✅ K7.1–K7.5 main 머지 완료 — K7.1(projection/outbox owner-scope, migration 0057, schema v2), K7.2(gate owner-inclusive resolve + owner-variant 템플릿 + B1–B6 boundary matrix + tripwire, PR #340), K7.3(endpoint + K5 패널 owner-scope, PR #345, owner_footprint + '내 개인 메모' 패널), K7.4(/ask graph owner two-framing + missing_link owner 확장, PR #360), K7.5(owner erasure graph-view + monitor rebuild-HOLD + ops runbook, PR #362) |
| **K8** 모순 노출 (후속) | `conflicts_of` 반대편 라벨 완화(placeholder도 반환), 페이지 그래프 패널에 그 페이지 claim의 미해소 모순 union(`page_conflicts` 템플릿), `/ask` conflict intent의 page-resolve 허용, resolve→status 신선도. **표면: `/ask` + 페이지 그래프 패널만**, read-only, SoR/FE(`해소/미해소` 칩)·resolve 엔드포인트 재사용 | conflicts_of placeholder 반환 회귀, 페이지 패널 모순 union, `/ask` 모순 page-resolve, resolve/reopen status 회귀 | 계획 내부 문서(비공개). K8.1–K8.6(owner-scope conflict status 포함) 구현 완료. 데이터의 conflict 6건으로 통합 검증 완료(`page_conflicts` 6 surface + 패널 union + conflicts_of placeholder 매칭) | ✅ K8.1–K8.6 main 머지 완료(PR #373). task-event 엣지 재투영 회귀 수정 + `/ask` claim-rooted anchor 누출 수정 + 충돌 그룹 쌍 인터리브(§10) 포함. |
| **K9** 엔티티 연결 발견 (후속) | K6 substrate(`entity_mentions`/`:Entity`/`MENTIONED_IN`/`RELATES_TO`) 노출: 신규 `entity_neighbors` 템플릿(페이지→엔티티→공유 페이지), 페이지 그래프 패널 엔티티 합류 + FE `Entity` 라벨, `/ask` `entity` intent(명사구→`name_norm` 결정론 resolve). company-scope, full-rebuild-only, read-only | entity_neighbors 게이트/회귀, 패널 엔티티 렌더, `/ask` entity 라우팅 무회귀 | 설계 내부 문서(비공개) §5. 엔티티 적재는 distill 실행에 종속(K6 substrate) | ✅ main 머지 (2026-06-24, PR #496) |
| **K10.1** 엔티티 정규화 강화 (후속) | K6 entity 품질 개선: E-N1a `_clean_name` Slack 표기 정준화(`<@U…\|실명>`→실명, bare `<@…>`/불투명 ID 정리) + 불투명 person Slack-ID 드롭, E-N1b 결정론 backfill 명령(`orthus.kg.entity_normalize`, dry-run/apply, 재정규화·병합·드롭·last_seen union·two-phase key·stale entity_conflict 탐지, LLM 0회·멱등), E-N1c company 데이터 실행+parity 검증. company-scope, full-rebuild-only(backfill 후 `node-kg-rebuild`) | 정규화/병합/드롭 회귀 18종, backfill 멱등·PG↔Neo4j parity·불투명 person 0 수렴 | 설계 내부 문서(비공개). 실명화(E-N2)·수동 병합(E-N3)은 후속 별도 스펙 | ✅ E-N1a(PR #618)/E-N1b(코드 완료)/E-N1c(데이터 실행) 완료 (2026-07-07) |

각 슬라이스는 독립 merge 가능하고, K2에서 멈춰도 "재구축 가능한 관계 인덱스"라는
완결 가치가 있다. K7은 K4의 게이트 reject 회귀 세트가 green인 상태를 전제한다.

**현재(2026-07-05):** K1–K9 전부 main에 머지돼 동작한다 — KG 백엔드(K1–K4)·
entity layer/hardening(K6)·`/ask` graph 분기(K4b, PR #333)·wiki 페이지 그래프
패널(K5, PR #335)·owner-scope graph K7.1–K7.5(K7.2 PR #340, K7.3 PR #345,
K7.4 PR #360, K7.5 PR #362)·모순 노출(K8, PR #373)·엔티티 연결 발견(K9,
PR #496). K6 entity substrate의 사용자 표면(entity 템플릿 노출 + `/ask` entity
intent)은 K9에서 노출됐다. 인터랙티브 그래프 탐색기(E1 PR #569 / E2 PR #584 /
E3 PR #586)도 main 머지 완료다(내부 문서(비공개)). **K10.1 엔티티
정규화 강화(E-N1a/b/c, 2026-07-07)**: Slack 표기 정준화 + 불투명 person ID 드롭 +
결정론 backfill로 기존 적재 엔티티를 수렴시킨다(내부 문서(비공개)).

### 테스트 전략

- **unit/integration**: `orthus_test` DB + test 전용 neo4j(`:7688`)에서
  slice별 Verify 열의 회귀를 pytest로 고정한다 — projection 멱등(2회 실행
  무변경), PG↔Neo4j parity, 게이트 reject 세트(structured 5-reject 동형),
  scope boundary(K7 이전 owner-scope 미투영 → K7 이후 템플릿×역할 +
  path-leak).
- **parity의 측정 정의**: 라벨별 Neo4j 노드 수가 대응 SQL
  count(`scope='company'`, placeholder 제외)와 일치하고, rel별 엣지 수가
  `wiki_links`(rel별)·provenance join·`IN_PROJECT` 귀속 count와 일치하면
  parity 100%다.
- **CI**: 현행 PR CI는 backend lint/test + frontend lint/build만 필수다.
  KG-off(fail-closed) 경로 테스트를 먼저 CI에 올리고, neo4j service
  container 채택 여부는 K2 PR에서 결정한다.
- **browser QA**: K5는 AGENTS 공통 QA 체크리스트(phone viewport 390×844
  포함)를 로컬 브라우저로 따른다. CI browser E2E는 추가하지 않는다(레포
  정책 — 사용자가 요청하는 PR에서만).

### 5.1 K0에서 반영할 data-model.md 개정 항목

아래는 K0 spec-lock에서 `docs/data-model.md`에 함께 개정한 항목의 **이행
완료 기록**이다(문서 단독 선행 수정은 spec drift이므로 K0 PR 안에서 같이
갔다). 새 작업 항목이 아니다.

1. **§1 저장소 경계 표:** `Neo4j | KG backend | not active, 후속 phase` 행을
   "K-series에서 read-only 파생 인덱스로 활성화 예정, `docs/kg-model.md` 참조"로
   갱신한다.
2. **§12 Removed/Non-Current:** legacy `kg_outbox`/`kg_change_log` 행에
   "P0/P1 persona-era 스키마는 폐기 유지. K3에서 **동명의 신규 스키마**를
   wiki consolidate/document publish/promote approve 이벤트 기준으로
   재정의한다"를 병기한다. legacy 부활이 아니라 신규 정의임을 명시한다.
3. **KG projection 원칙 신설:** "Postgres + node-local wiki-store가 유일
   SoR이다. Neo4j는 `wiki_pages`/`wiki_links`/`structured_rows`/`documents`를
   읽기 전용으로 투영하는 파생 인덱스이며, 불일치 시 `kg-rebuild`가
   언제나 SoR 기준으로 복원한다"를 명문화한다. *(P8 정합 개정: "노드별" →
   central 단일 인스턴스, v1 projection은 company scope만.)*

### 5.2 data-gap 연계 후보 (K6)

K6 이전 gap reason(`no_data`/`weak_retrieval`/`insufficient_grounding`)은 retrieval
점수 기반이라 "정보는 존재하지만 정보 간 연결 고리가 끊겨 답하지 못하는" 경우를
구분하지 못한다. K4 이후 KG가 있으면 질문이 닿은 wiki page들이 그래프상
비연결(disconnected component)임을 결정론적으로(LLM 0회) 감지해
`data_gaps.reason="missing_link"`로 적재할 수 있다. 기존 gap detection의
LLM-0회 원칙과 dedup upsert 계약을 그대로 따르며, K6에서 별도 acceptance
criteria로 확정했다(구현 완료 — PR #296). DB는 `data_gaps.reason`이 CHECK 없는
TEXT라 migration이 불필요했고, canonical `GapReason` Literal
(`orthus/schemas/canonical.py`)은 K6에서 `missing_link` 포함 4종으로 확장됐다.

**감지 위치(K6 확정 — 동기-가드):** insufficient_grounding 답변 직후
gap 기록 지점에서, retrieval hit가 ≥2개의 materialized company wiki page로
resolve되고 `kg_available()`일 때만 상위 2개에 `path_between(max_hops=4)`를
1회 실행한다(경로 없음 → `missing_link` dedup upsert). `path_between`은 지식 rel만
타고 `IN_PROJECT` 허브를 제외하므로(§4), 같은 프로젝트라는 이유만으로는 "연결"로
보지 않는다 — 같은 프로젝트의 비연결 page 쌍도 정상 감지된다. 실패/KG 미가용은 skip
(fail-open, 답변 차단 없음). hit가 이미 손에 있어 추가 retrieval/스키마 변경이
없는 것이 채택 근거다. KG-down 시 매 약답이 driver connect timeout을 물지
않도록 `kg_available()` 음성 결과를 짧은 TTL로 캐시한다. /ask 볼륨이 커져
이 동기 KG read가 hot-path에 부담이 되면(전환 trigger: p95 latency 기여분
또는 qualifying-call rate 임계 — 구현 명세 §9.3에 수치 기록) data_gaps에 hit
slug 컬럼을 더해 주기 배치 재평가로 전환한다. 구현 상세(트리거 앵커
`qa.py`, 매퍼)는 구현 명세 §9.3.

---

## 6. 명시적 비-목표 (spec 충돌 방지)

- LangGraph/persona/drift/confidence routing 부활 아님 — KG는 policy gate
  outcome 계산에 관여하지 않는다(P3 gate는 결정론 유지).
- **K7 이전의 owner-scope row graph 투영 아님.** v1(K2–K6) projection은
  company scope만 읽는다. owner-scope graph는 K7에서만, §4 계약(전 템플릿
  owner 술어 + 경로 가시성 규칙 + boundary/path-leak 회귀) 전체와 함께 연다.
  K7을 앞당기거나 계약 일부만 떼어 구현하지 않는다.
- Neo4j를 SoR로 쓰지 않는다. 사용자 직접 Cypher 콘솔/Browser 노출 없음.
- raw 본문/chunk를 그래프에 저장하지 않는다.
- 새 central write path 아님 — KG 쓰기는 전부 기존 wiki/promote 경로의 파생.
- 내부 문서(비공개)의 Fact/Tendency/Value persona 3계층은 채택하지 않는다
  (legacy). 채택하는 건 wiki claim/page/source 실체 기반 그래프다.

---

## 7. 제약 조건과 리스크

### 제약 조건

- **팀/일정**: 운영자 1인(설계·구현·운영 동일인). 고정 마감 없음 — slice
  단위 독립 merge로 진행하되, P8이 레포 최우선이므로 우선순위 경합 시
  K-series가 양보한다.
- **하드웨어**: central Mac mini 1대에 postgres/FastAPI/Next.js와 동거한다.
  Neo4j heap은 보수 상한(`server.memory.heap.max_size`)으로 시작하고 측정
  후 조정한다.
- **에디션/라이선스**: Neo4j **Community Edition만** 사용한다. Enterprise
  전용 기능(role/grant RBAC, multi-database, row-level security)에 의존하는
  설계 금지 — 경계 증명은 전부 앱 레이어다(§4).
- **금지 사항**: LangGraph/persona/drift/confidence routing, raw-chunk RAG,
  LLM 자유 Cypher 생성, 새 central write path, Neo4j SoR화(§6, AGENTS 절대
  규칙). 신규 Python 의존성은 `neo4j` driver 1개만 — 추가 그래프
  라이브러리/플러그인은 별도 결정.
- **성능**: 초기 full projection(약 9천 wiki_pages + links)은 off-peak
  batch로 수용한다. K4 쿼리 게이트 기본값은 transaction timeout 2초 +
  LIMIT 50 + depth≤2/hop≤4(§4)이며, 측정 후 본 문서 갱신과 함께 조정한다 —
  상한 완화는 별도 결정.
- **보안/규정**: PII redaction 정책 준수(§1 — KG에 본문 미저장), Neo4j
  ports loopback 전용(`7687`/`7474`/`7688`), password는 keychain
  `secret_ref`, 경계 위반은 전부 fail-closed 기본값.

### 리스크와 완화

| 리스크 | 완화 |
|---|---|
| central Mac mini에 컨테이너 1개 추가(메모리) | community + `server.memory.heap.max_size` 보수 설정. P8 정합으로 central 단일 인스턴스만 필요(K0 초안의 노드별 2-인스턴스 불필요) |
| graph 분기 답변 품질이 wiki 분기보다 낮을 위험 | K4에서 graph는 보조 분기 — 라우터가 관계형 질문으로 확신할 때만, 아니면 wiki fallback. gap detection처럼 결정론 감지 우선 |
| 8929 wiki_pages 초기 projection 시간 | UNWIND batch + 인덱스 선생성, 측정 후 `kg-rebuild`를 launchd off-peak로 |
| 테스트 인프라 | `orthus_test` DB 분리 패턴 그대로 — test 전용 neo4j 컨테이너(별도 포트). CI에서는 service container 또는 KG-off 경로 테스트 우선 |

---

## 8. 수용 기준 (K-series 전체 완료 선언)

1. central 노드에서 `kg-rebuild` 후 company scope row 기준 PG↔Neo4j parity
   100%, 2회 연속 실행 무변경(멱등).
2. wiki consolidate → 60초 내 KG 반영(K3 outbox merge 이후에 적용되는 기준),
   Neo4j 정지 상태에서 `/ask` 기존 분기 전부 정상.
3. "A와 B는 무슨 관계?" 류 질문이 graph 분기로 라우팅되고, 답변 sources가 전부
   compiled wiki page provenance. (K4b에서 충족 — K4 자체는
   `GET /wiki/pages/{slug:path}/graph`까지)
4. 게이트 공개 표면에 raw Cypher 입력 경로가 존재하지 않고(typed
   template과 파라미터 바인딩만), 런타임에서 거부 가능한 시도(미등록
   템플릿/파라미터·깊이 초과/timeout)는 전부 reject + `kg_query_runs` 기록,
   등록 템플릿 전수의 write 키워드 부재는 정적 검사 회귀로 고정 (reject
   회귀 세트 green — 구현 명세 §6.2/§6.5).
5. **scope 경계 (P8 정합, 2단계):** K7 이전 — personal owner-scope
   row(`scope='personal'`, `owner_id` 보유)가 Neo4j에 노드/엣지로 존재하지
   않음을 boundary 회귀로 증명(projection SELECT 필터와 rebuild 결과 양쪽).
   K7 이후 — personal row는 존재하되, 타 user/admin 세션의 어떤 템플릿
   결과(노드/엣지/경로)에도 비노출임을 템플릿×역할 boundary 회귀 +
   path-leak 테스트로 증명(fail-closed).
6. erasure 절차(`docs/operations.md`) 실행 시, 삭제된 SoR row에 대응하는
   **그래프 노드/엣지**가 KG에서도 detach-delete됨을 확인("노드"는 orthus
   시스템 노드가 아니라 그래프 노드를 뜻한다). K7 이후에는 owner erasure 시
   해당 owner의 personal 그래프 노드/엣지 전체 detach-delete 포함.

---

## 9. 문서 관계

| 문서 | 관계 |
|---|---|
| `docs/kg-implementation-spec.md` | K1–K7 **구현 명세**(모듈 배치/시그니처/마이그레이션 DDL/테스트 케이스/slice 게이트). 본 문서가 canonical 설계 계약이며 충돌 시 본 문서 우선 |
| 내부 문서(비공개) | 최상위 계약. K0에서 Non-goals/절대금지 개정 대상 |
| 내부 문서(비공개) | **K-series 아키텍처 전제.** §8이 KG projection 대상을 central 단일 DB로 재배치 — 본 문서 P8 정합 개정의 근거 |
| `docs/architecture-v2.md` | §1 KG 슬롯, §7 후속 Phase, §8 hard-constraint 개정 예고의 실행 |
| `docs/llm-wiki.md` | KG의 원천 데이터 모델(claim/page/source/task, wiki_links) |
| `docs/data-model.md` | `wiki_pages`/`wiki_links`/`structured_rows`/`documents` 스키마 + `kg_outbox`(K3)·`kg_query_runs`(K4)·`kg_entities`/`kg_entity_mentions`(K6) 신규 migration 추가 대상. K0 개정 항목은 §5.1 |
| 내부 문서(비공개) (legacy) | §4.3 KG outbox 패턴 재사용 출처 |
| `docs/operations.md` | secret/port/erasure/audit 정책 확장 대상 |
| 내부 문서(비공개) | KG hold 해제 + K0–K7 milestone 등록 대상 |
