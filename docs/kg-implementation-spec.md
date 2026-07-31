# K1–K7 구현 명세 — Neo4j KG rebuildable index

> status: 구현 명세 v1 (2026-06-11). canonical 설계 계약은 `docs/kg-model.md`다 —
> 두 문서가 충돌하면 `kg-model.md`가 우선하고, 계약 변경은 해당 K-series slice
> PR에서 두 문서를 함께 갱신한다.
> 깊이: **설계 전용** — 모듈 배치, 함수·클래스 시그니처, Pydantic 모델 필드,
> 의사코드, 테스트 케이스 목록까지 담는다. 실제 Python/TS 구현 코드는 각 slice
> PR에서 작성한다(레포 slice 컨벤션).
> 본 문서의 file:line 참조는 2026-06-11 main 기준 실측값이다. 구현 시점에
> 라인이 밀렸으면 심볼 이름으로 찾는다 — 심볼 이름이 계약이고 라인은 길잡이다.
>
> **상태 갱신(2026-07-06):** K1–K7 전 slice가 main에 머지 완료됐다 — K1/K2/K3/K4
> (2026-06-12), K6(PR #297), K4b(PR #333)·K5(PR #335)·K7.1(PR #309)·K7.2(PR #340)
> (2026-06-15~16), K7.3(PR #345)·K7.4(PR #360)·K7.5(PR #362)(2026-06-17). 미결로
> 남아 있던 게이트 체크박스는 머지 증거와 함께 [x]로 갱신했다. 후속 시리즈(K8 모순
> 노출 PR #373, K9 엔티티 연결 PR #496, 그래프 탐색기 E1–E3 PR #569/#584/#586)는 본
> 문서 범위 밖이며, 본문 계약을 바꾼 지점에만 날짜 있는 개정 주석으로 표시한다.

---

## 0. 목표와 문제 정의 (구현 관점)

문제 정의·포지셔닝·완성 시 사용자 가치는 `docs/kg-model.md` §0이 canonical이다.
본 문서는 그 설계를 **PR 단위로 옮길 수 있는 구현 계약**으로 내린다. slice별
"done"의 사용자 관점 정의:

| Slice | 구현 PR이 끝났을 때 참인 문장 |
|---|---|
| K1 | `make up` 후 neo4j 컨테이너가 loopback에서 기동하고, `make kg-bootstrap`이 멱등으로 constraints/index를 적용하며, KG off 상태에서 company/personal `node-smoke`가 전부 green |
| K2 | `make kg-rebuild` 2회 연속 실행이 무변경이고, company scope 기준 PG↔Neo4j parity 100%이며, owner-scope row가 그래프에 존재하지 않음이 회귀로 고정 |
| K3 | wiki consolidate가 60초 내 그래프에 반영되고(60초 SLA 측정 기준 — kg-model §8 수용 기준 2; document publish/promote approve도 같은 outbox 경로로 반영), Neo4j 정지 시 outbox가 적체 후 재기동 drain |
| K4 | `GET /wiki/pages/{slug:path}/graph`가 동작하고, 게이트 reject 회귀 세트(임의 쿼리 불가·depth 초과·timeout)가 green이며 모든 실행이 `kg_query_runs`에 남음 |
| K4b | "A와 B는 무슨 관계?" 질문이 graph 분기로 라우팅되고 답변 sources가 전부 compiled wiki page provenance |
| K5 | `/wiki/{slug}`에서 1–2 hop related-graph 패널이 보이고 phone viewport QA 통과 |
| K6 | entity 그래프 substrate(추출→SoR→projection→`entity_mentions` 템플릿 등록; 사용자 탐색 표면은 K4b 후속 — K9에서 노출 완료, PR #496)·`missing_link` data gap·erasure 전파·rebuild drill까지 hardening 완료. 배송은 4-way 순차 PR(substrate → distill → missing_link → erasure/ops) |
| K7 | owner 본인 세션만 personal 그래프를 보고, 타 user/admin 세션은 K7 이전과 결과가 동일함이 boundary/path-leak 회귀로 고정 |

### 결과물 형식 — 이 문서가 담는 것 / 담지 않는 것

- 담는 것: 모듈/파일 배치, 시그니처(타입 포함), DDL·Cypher·compose 계약
  스니펫, 처리 순서 의사코드, slice별 테스트 케이스 목록, 진입/완료 게이트,
  대안 검토, 리스크/엣지케이스.
- 담지 않는 것: 동작하는 애플리케이션 코드, FE 컴포넌트 마크업, LLM 프롬프트
  전문(계약 필드만 정의). 이들은 각 slice PR의 산출물이다.

---

## 1. 기존 코드베이스 접점 지도 (2026-06-11 실측)

K-series가 읽거나 호출하거나 확장하는 실제 심볼. 구현 전 이 표의 심볼이
여전히 존재하는지 확인하고, 바뀌었으면 본 문서를 같은 PR에서 갱신한다.

### 1.1 SoR 읽기 접점 (K2 projection 입력)

| 접점 | 심볼 | 위치 | 사용 slice |
|---|---|---|---|
| wiki_pages 테이블 | `wiki_pages` — `page_id`(PK)·`slug`·`kind`·`title`·`confidence`·`content_hash`·`scope`·`project`·`owner_id`·`updated_at`, UNIQUE `(slug, scope, owner_id)` NULLS NOT DISTINCT | `orthus/tables.py:284` | K2/K7 |
| wiki_links 테이블 | `wiki_links` — `src_page_id`·`dst_slug`·`rel`(supports/conflicts/backlink/derived_from), PK 없음 | `orthus/tables.py:321` | K2 |
| structured_rows 테이블 | `structured_rows` — `row_id`(PK)·`record_type`·`source_doc_id`·`properties`(JSONB)·`confidence`·`scope`·`owner_id`·`user_id`·`updated_at` | `orthus/tables.py:248` | K2/K7 |
| documents 테이블 | `documents` — `doc_id`(PK)·`source`·`source_db_name`·`scope`·`project`·`user_id`·`updated_at`. **owner_id 컬럼 없음** — personal row 소유자는 `user_id`(kg-model §3 owner 매핑) | `orthus/tables.py:138` | K2/K7 |
| task frontmatter 로더 | `load_task(slug, *, root=None, scope="company", owner_id=None) -> WikiTask \| None` / `list_slugs(kind, ...) -> list[str]` | `orthus/wiki/store.py:905` / `:853` | K2 (§4.3) |
| canonical WikiTask | `WikiTask(slug, kind, description, related, created_at, resolved)` | `orthus/schemas/canonical.py:729` | K2 |
| canonical WikiSource | `WikiSource.source_type`(`corpus_doc`/`qa_session`/`conversation`)·`source_ref` | `orthus/schemas/canonical.py:689` | K2 provenance |

### 1.2 쓰기 이벤트 접점 (K3 outbox enqueue)

| 접점 | 심볼 | 위치 |
|---|---|---|
| wiki write 공통 커밋 지점 | `_persist(...)` — 내부 `with session() as s:`에서 `_upsert_page_row`/`_reindex_chunks`/`_replace_links` 후 `s.commit()` | `orthus/wiki/store.py:779` (commit `:835`) |
| document publish | `publish_agent_draft_document(...)` — `audit("document.publish")` 안에서 source 전환 commit 후 corpus/wiki authoring | `orthus/documents.py:152` |
| promote approve | `approve_promotion_stage(stage_id, user_id, settings)` — `audit("promote.approve")` 안에서 `upsert_source_document(..., scope="company")` | `orthus/promote.py:186` |
| consolidate 진입점 | `consolidate(source, claims, *, user_id, root, scope, owner_id, project) -> tuple[list[WikiPage], list[WikiTask]]` — 내부에서 write_source/claim/page/task 순 호출(각각 `_persist` 트랜잭션) | `orthus/wiki/consolidate.py:51` |

### 1.3 읽기 경로 패턴 접점 (K4/K4b가 동형 복제할 원본)

| 접점 | 심볼 | 위치 |
|---|---|---|
| 라우터 분기 | `Route = Literal["structured", "wiki"]` / `classify(question, *, chat_model=None) -> Route`(rule 우선 + LLM fallback, fail-safe `"wiki"`) | `orthus/router/route.py:19` / `:62` |
| 라우터 진입 | `answer(user_id, question, *, scope="all", project=None, chat_model=None, context_wiki_slug=None) -> RoutedAnswer` | `orthus/router/__init__.py:26` |
| 검증 게이트 기록 | `insert_run(query_id, user_id, source_id, question)` / `update_run(query_id, *, compiled_sql, validation, status, result_meta)` | `orthus/assistant/pipeline.py:23` / `:41` |
| reject 사유 문자열 | `parse_failed`·`multiple_statements`·`non_select_statement`·`denied_function:{name}`·`unknown_table:{name}`·`explain_failed:{e}`·`scope_rewrite_failed:{ExcType}` | `orthus/assistant/validate.py:144`–, `orthus/structured/query.py:313`·`:369` |
| audit span | `audit(node: str, *, correlation_id: UUID \| None = None)` → `AuditSpan.set_output(...)`/`.add_meta(**kw)` | `orthus/audit/logger.py:78` |
| PII redaction | `redact_pii_text(s: str) -> str` / `redact_pii(obj) -> Any` | `orthus/audit/redact.py:42` / `:47` |
| 인증 dependency | `get_session_user_or_knowledge_token` (`orthus/api/knowledge_token.py:79`) / `get_current_user`·`get_user_id` (`orthus/api/deps.py:13`/`:22`) / `AuthenticatedUser(user_id, auth_mode, display_name, email, role, node_id)` (`orthus/auth.py:49`) | — |
| 운영자 가드 | `require_node_operator(current)` — `MANAGER_ROLES = {"owner","admin"}` | `orthus/auth.py:749` / `:40` |
| owner fail-closed 술어 원본 | `_scope_filter(user_id, scope)` (`orthus/wiki/retrieve.py:90`) / `_scope_clause` · `_scope_predicate_sql` · `_inject_scope_filter` (`orthus/structured/query.py:73`/`:102`/`:121`) | K7 동형 패턴 |
| wiki page API 패턴 | `GET /wiki/pages/{slug:path}` (`orthus/api/routes/wiki.py:227`) — `_wiki_scope` resolve → `store.load_page` → 404 | K4 endpoint 추가 지점 |

### 1.4 인프라/테스트 접점

| 접점 | 현황 | 위치 |
|---|---|---|
| docker-compose | postgres 단독(`orthus_pg`, :5433, healthcheck, `orthus_pg_data` volume). profiles 미사용 | `docker-compose.yml` |
| Settings | `class Settings(BaseSettings)`, `env_prefix="ORTHUS_"`, `owner_scope_enabled: bool = False`(:32) 패턴 | `orthus/settings.py:16` |
| secret backend | `get_secret(ref, *, backend=None) -> str \| None` / ref 컨벤션 `orthus/connectors/{account_id}/{key}` | `orthus/secrets.py:46` / `:24` |
| migration head | `0046_email_send_manual_origin` — 파일명 `NNNN_slug.py`, `op.execute()` raw DDL, `TIMESTAMPTZ DEFAULT now()`, index `idx_*`/`uq_*` | `migrations/postgres/versions/` |
| 테스트 fixture | `pg()`(세션, :5433 가용성 skip)·`clean(pg)`(테이블 전체 wipe + 설정 고정)·`user_id(clean)` | `tests/conftest.py:155`/`:162`/`:220` |
| 테스트 DB | `make test` → `scripts/setup_test_db.sh`(orthus_test + `orthus_ro` grant) + `ORTHUS_EMBEDDING=mock ORTHUS_LLM=mock` | `Makefile:27` |
| CI | backend(ruff + docs-check + `make test`, compose postgres service) + frontend(lint/build) | `.github/workflows/ci.yml` |
| docs-check | `scripts/check_docs_spec.py` — `REQUIRED_FILES`·spec token·link drift 검사 | `Makefile:67` |
| 스케줄러 | `install_launchd_scheduler.sh` → `sync_cycle.sh`(`REBUILD_WIKI=1` 조건부 rebuild 패턴) | `scripts/node/` |
| KG 잔존물 | **없음** — `orthus/kg/`·neo4j 참조 0건 확인(2026-06-11). 클린 슬레이트 | — |

---

## 2. 공통 기반 (전 슬라이스 공유)

### 2.1 모듈 배치

```text
orthus/kg/
├─ __init__.py      # 공개 표면: kg_enabled()/kg_available() re-export만. 무거운 import 금지
├─ client.py        # K1 — driver lifecycle, 가용성 ping, 예외 타입
├─ bootstrap.py     # K1 — constraints/index 멱등 적용 + KgMeta 초기화 (__main__)
├─ schema.py        # K2 — 라벨/관계/속성 상수, KG_SCHEMA_VERSION, row dict 계약
├─ project.py       # K2 — SoR SELECT → row dict 변환 (순수 결정론, Neo4j 비의존)
├─ store.py         # K2 — Neo4j MERGE batch writer, KgMeta read/write, prune
├─ rebuild.py       # K2 — full rebuild 오케스트레이션 (__main__)
├─ sync.py          # K2 — incremental watermark sync (__main__)
├─ outbox.py        # K3 — enqueue helper + KGOutboxWorker (+ __main__ 수동 drain)
├─ templates.py     # K4 — typed template registry + 파라미터 Pydantic 모델
├─ gate.py          # K4 — run_kg_template() 게이트 + kg_query_runs 기록
├─ entities.py      # K6 — distill entity persist (kg_entities/kg_entity_mentions SoR)
├─ erase.py         # K6 — erasure 전파 (§9.4)
├─ monitor.py       # K6 — read-only 모니터 CLI (§9.4)
├─ visibility.py    # K7 — owner-scope 경계 단일 출처(ns 키·write/read 술어·resolve_slug)
├─ inventory.py     # K7.3 — cross-scope edge inventory 러너
└─ relations.py     # 후속(PR #550) — relation enum → 기존 템플릿 매핑 read 헬퍼(mcp/agentic 툴 공유)
```

규칙:

- **lazy import**: `neo4j` 패키지 import는 `client.py` 함수 내부에서만 한다.
  `orthus/kg` 하위 모듈의 module-level에서 driver를 만들지 않는다 —
  `make node-smoke`의 "KG off 시 driver 미설치/미기동 무영향" 계약(kg-model
  §1)의 구현 수단이다.
- `api/routes/wiki.py`(K4 endpoint), `router/`(K4b), `wiki/store.py`·
  `documents.py`·`promote.py`(K3 enqueue 1줄 hook) 외에는 기존 모듈을
  수정하지 않는다.
- 신규 모듈 파일은 해당 slice PR에서 생성한다(레포 규칙 — 빈 파일 선점 금지).
  위 트리는 배치 계약이지 일괄 생성 지시가 아니다.

### 2.2 Settings 필드 (K1에서 일괄 추가)

`orthus/settings.py` `Settings`에 추가 — kg-model §1과 동일, 선언 위치만 구체화:

```python
# --- KG (K-series, docs/kg-model.md) ---
kg_enabled: bool = False                  # ORTHUS_KG_ENABLED — fail-closed
kg_uri: str = "bolt://127.0.0.1:7687"     # ORTHUS_KG_URI
kg_user: str = "neo4j"                    # ORTHUS_KG_USER
kg_password: str = ""                     # ORTHUS_KG_PASSWORD — §2.4 시크릿 해석 순서
kg_owner_scope_enabled: bool = False      # ORTHUS_KG_OWNER_SCOPE_ENABLED — K1에서 선언만 하고
                                          # 코드 참조는 K7에서 시작(그 전까지 어떤 동작도 바꾸지 않음)
kg_query_timeout_ms: int = 2000           # K4 게이트 transaction timeout (kg-model §4 기본값)
kg_query_limit: int = 50                  # K4 결과 LIMIT 주입 기본·상한 (완화는 별도 결정)
kg_outbox_poll_seconds: int = 5           # K3 worker poll 주기
```

`tests/conftest.py::clean`의 설정 고정 블록에 `kg_enabled=False`를 추가해
테스트 기본 상태를 KG-off로 못박는다(KG 테스트는 fixture로 명시 opt-in, §3.5).

### 2.3 driver lifecycle — `orthus/kg/client.py` 계약

```python
class KgDisabled(RuntimeError): ...      # flag off — 호출측 분기용
class KgUnavailable(RuntimeError): ...   # driver 미설치 / 연결 실패 / 인증 실패

def kg_enabled() -> bool
    # settings.kg_enabled 단순 위임. import 부작용 없음.

def get_kg_driver() -> "neo4j.Driver"
    # 프로세스 싱글턴 (lru_cache 또는 모듈 lock + 캐시 — get_settings() 패턴 동형).
    # 내부에서 import neo4j (lazy). flag off면 KgDisabled.
    # 생성 실패(ImportError/ServiceUnavailable/AuthError)는 KgUnavailable로 정규화.

def kg_available() -> bool
    # get_kg_driver().verify_connectivity() best-effort. False여도 예외 전파 금지.
    # K4 unsupported 응답(reason="kg_unavailable")의 판정 함수.

def kg_read_session() -> ContextManager["neo4j.Session"]
    # session(default_access_mode=READ, database="neo4j") 래퍼 — K4 게이트 전용 진입로.
    # settings.kg_query_timeout_ms를 transaction timeout으로 적용한다(§6.2 6단계의
    # timeout 단일 출처 — 호출자가 별도 timeout을 만들지 않는다).

def kg_write_session() -> ContextManager["neo4j.Session"]
    # projection/outbox/bootstrap 및 last_accessed_at best-effort 기록 전용.

def close_kg_driver() -> None
    # 테스트 teardown / 프로세스 종료용. 캐시 무효화 포함.
```

호출 규약: **읽기 경로는 `kg_read_session`만, 쓰기 경로는 `kg_write_session`만**
쓴다. K4 게이트 코드에 write session import가 등장하면 리뷰에서 거부한다
(Community Edition에 DB 레벨 read-only 계정이 없는 만큼 — kg-model §4 — 코드
경계가 그 역할을 대신한다).

### 2.4 의존성·시크릿

- `pyproject.toml` `dependencies`에 `"neo4j>=5.28"` 1개 추가(메인 의존성 —
  대안 검토는 §11-1). 다른 그래프 라이브러리/GDS 플러그인 금지(kg-model §7).
- `ORTHUS_KG_PASSWORD` 해석 순서(connector secret 패턴 동형):
  1. `get_secret("orthus/kg/password")` (`orthus/secrets.py:46`, keychain 우선)
  2. miss면 env `ORTHUS_KG_PASSWORD` (bootstrap/dev fallback)
  3. 둘 다 없고 `kg_enabled=true`면 driver 생성 시 `KgUnavailable` —
     평문 기본 password를 코드에 두지 않는다.
- compose의 `NEO4J_AUTH: neo4j/${ORTHUS_KG_PASSWORD:?}`는 `:?`로 미설정 기동을
  거부한다(kg-model §1 계약 그대로). test 컨테이너만 고정값
  `orthus-kg-test`(secret 아님).

### 2.5 공통 타입 계약 — `orthus/kg/schema.py`

```python
KG_SCHEMA_VERSION: int = 2   # 그래프 스키마 계약 버전. §2 스키마 변경 시 증가
                             # → sync가 rebuild 요구 (§4.7). 작성 시점 v1;
                             # K7.1 owner-scope 키/속성 전환이 2로 bump(§10.1)

# 라벨/관계 상수 — 문자열 산재 금지, projection·템플릿·테스트가 공유
class Label(StrEnum):
    WIKI_PAGE = "WikiPage"; WIKI_CLAIM = "WikiClaim"; WIKI_SOURCE = "WikiSource"
    DOCUMENT = "Document"; STRUCTURED_FACT = "StructuredFact"
    PROJECT = "Project"; ENTITY = "Entity"          # ENTITY는 K6
    KG_META = "KgMeta"; OUTBOX_APPLIED = "OutboxApplied"

class Rel(StrEnum):
    SUPPORTS = "SUPPORTS"; CONFLICTS_WITH = "CONFLICTS_WITH"
    BACKLINK = "BACKLINK"; DERIVED_FROM = "DERIVED_FROM"
    EXTRACTED_FROM = "EXTRACTED_FROM"; IN_PROJECT = "IN_PROJECT"
    MENTIONED_IN = "MENTIONED_IN"; RELATES_TO = "RELATES_TO"   # K6

WIKI_LINK_REL_MAP: dict[str, Rel] = {
    "supports": Rel.SUPPORTS, "conflicts": Rel.CONFLICTS_WITH,
    "backlink": Rel.BACKLINK, "derived_from": Rel.DERIVED_FROM,
}
```

projection 단계 간 통신은 **plain dict가 아니라 TypedDict/Pydantic row 모델**
로 한다(원칙 2 — canonical 슬롯 스키마):

```python
class KgNodeRow(BaseModel):       # project.py 출력 단위
    label: Label
    merge_key: str                # "page_id" | "doc_id" | "row_id" | "name" | "slug"(placeholder)
    merge_value: str
    props: dict[str, Any]         # 본문 금지 — slug/title/메타/hash만 (kg-model §1 PII 행)

class KgEdgeRow(BaseModel):
    rel: Rel
    src_label: Label              # K2 구현 교정(2026-06-12): Neo4j 속성 인덱스가
    src: tuple[str, str]          #   라벨 단위라, MATCH에 라벨이 없으면 엣지 batch가
    dst_label: Label              #   전수 스캔이 된다 — 계약 의미 불변, 필드만 추가
    dst: tuple[str, str]          # (merge_key, merge_value)
    props: dict[str, Any] = {}
```

### 2.6 fail-open 시맨틱 규약

| 호출자 | KG off / Neo4j down 시 동작 |
|---|---|
| K4 page graph API | 200 + `supported:false` + `reason="kg_disabled"|"kg_unavailable"` (kg-model §4 응답 계약) — 예외 전파 금지 |
| K4b 라우터 graph 분기 | `classify`가 graph를 골랐어도 `kg_available()` false면 wiki 분기로 fallback + `RoutedAnswer.warnings`에 `"kg_unavailable"` 추가 |
| K3 enqueue | **무조건 enqueue**(PG 트랜잭션이므로 Neo4j 상태 무관). worker가 적체 처리 |
| K3 worker | `KgUnavailable`이면 claim 해제 후 다음 poll 대기 — attempts 증가 없음(인프라 다운은 이벤트 실패가 아니다) |
| kg-rebuild / kg-sync CLI | 비정상 종료(exit≠0) + stderr 사유 — 스케줄러/운영자가 인지해야 하므로 CLI만 fail-loud |

---

## 3. K1 — infra + 스키마 부트스트랩

### 3.1 docker-compose / Makefile / env

compose 서비스 정의·메모리 상한·loopback 바인딩·test profile은 kg-model §1
"K1 인프라 상세 계약"의 YAML이 그대로 계약이다. 구현 메모만 추가:

- `neo4j-test`는 `profiles: [test]`라 `make up`에 미포함 — KG 통합 테스트
  실행 전 `docker compose --profile test up -d neo4j-test`가 필요하다.
  Makefile에 `kg-test-up` 편의 target을 추가한다(아래).
- healthcheck `wget`은 neo4j 공식 이미지에 포함돼 있다. `interval`/`retries`는
  postgres 서비스와 같은 수치로 맞춘다(파일 내 일관성).
- kg-model §1 스니펫은 서비스 정의만 담고 있다 — compose top-level
  `volumes:`에 `orthus_neo4j_data` 선언을 함께 추가해야 한다(기존
  `orthus_pg_data`와 동렬).

Makefile 추가 target — K1은 `kg-bootstrap` + `kg-test-up` 2종만 추가한다
(시점 조정, 2026-06-11 K1 구현: `kg-rebuild`/`kg-sync`는 해당 모듈
`rebuild.py`/`sync.py`가 생기는 K2 PR에서 함께 추가 — 존재하지 않는 모듈을
가리키는 target을 선점하지 않는 레포 규칙과 합치. 2026-06-12 K2 구현에서
4종 전부 존재):

```text
kg-bootstrap:  uv run python -m orthus.kg.bootstrap
kg-test-up:    docker compose --profile test up -d neo4j-test   # 테스트 인프라 편의
kg-rebuild:    uv run python -m orthus.kg.rebuild                # K2에서 추가
kg-sync:       uv run python -m orthus.kg.sync                   # K2에서 추가
node-kg-bootstrap/-rebuild/-sync NODE=company                   # K2 리뷰 교정 — prod 경로
```

**K2 리뷰 교정(2026-06-12, SoR 단일성):** root `.env` 기반 `kg-*` target은
local dev DB(`orthus`) 전용이다. prod central은 company 데이터가 node
DB(`orthus_company`)에 있으므로 `node-kg-*` 변형(node env 로드)을 쓴다 — 두
env를 섞으면 rebuild prune(삭제 수렴 권위)이 상대 SoR의 투영을 전부 삭제해
그래프가 두 DB 사이를 진동한다(`docs/operations.md` §2.1). 같은 계열의 코드
레벨 가드로 `client.require_company_node()`가 rebuild/sync(향후 K3 worker
포함) 진입 시 `node_kind != company`를 fail-closed로 거부한다.

`.env.example`에 `ORTHUS_KG_ENABLED=false` + `ORTHUS_KG_URI`/`ORTHUS_KG_USER`
주석 라인 추가(secret 키 이름만 — 절대 규칙 "시크릿 평문 금지").

**`make up` 전제 변경**: compose의 `NEO4J_AUTH: neo4j/${ORTHUS_KG_PASSWORD:?}`는
env 미설정 시 기동을 거부하므로, K1 머지 후 `make up`은 `.env`에
`ORTHUS_KG_PASSWORD`(dev는 임의값)가 있어야 동작한다 — `.env.example` 주석과
K1 PR 본문에 명시한다. **CI 교정(2026-06-11 K1 구현 실측)**: compose는
`docker compose up -d postgres`처럼 일부 서비스만 기동해도 파일 전체를
interpolate하므로 `:?`가 CI에서도 값을 요구한다("postgres만 기동하므로
무영향"이라던 본 절 초안 가정은 오류). CI backend job은 interpolation 전용
더미 `ORTHUS_KG_PASSWORD`를 job env로 주입하며(neo4j 서비스는 기동하지 않음),
K2의 neo4j-test service container 채택 시 이 값 정리를 함께 결정한다.

### 3.2 `orthus/kg/bootstrap.py` 설계

```python
def apply_schema(driver: "neo4j.Driver") -> BootstrapReport
    # kg-model §2의 constraints/index DDL 목록을 순서대로 실행.
    # 전부 IF NOT EXISTS — 2회 실행 무변경(멱등)이 K1 Verify.
    # 실행 후 SHOW CONSTRAINTS 결과와 기대 목록 diff → report에 기록.

def ensure_kg_meta(driver) -> None
    # MERGE (m:KgMeta {id:'kg_meta'})
    #   ON CREATE SET m.kg_schema_version=$v, m.last_sync_at=null, m.last_rebuild_at=null
    # 기존 노드의 version은 건드리지 않는다 — version 승급은 rebuild만 한다(§4.7).

def main() -> int        # python -m orthus.kg.bootstrap
    # kg_enabled 검사 → driver → audit("kg.bootstrap") span 안에서
    # apply_schema + ensure_kg_meta → report stdout → exit 0/1.

class BootstrapReport(BaseModel):
    constraints_applied: list[str]; indexes_applied: list[str]
    already_present: list[str]; kg_schema_version: int
```

DDL 원본은 kg-model §2 코드블록이 canonical이다. bootstrap.py는 그 목록을
상수 리스트로 보유하고, K2 이후 스키마가 늘면 **kg-model §2와 같은 PR에서**
리스트를 갱신한다.

### 3.3 node-smoke 확장

`scripts/node/smoke.sh`에 추가하는 검사(전부 KG-off 전제):

1. `ORTHUS_KG_ENABLED` 미설정/false에서 API 기동·기존 smoke 전부 green.
2. lazy import 검증: `orthus.kg` import와 API 기동이 Neo4j 연결을 시도하지
   않음을 확인한다. neo4j 패키지는 uv 환경에 항상 설치돼 있어 "패키지 부재"
   자체는 검증할 수 없으므로, 대신 `ORTHUS_KG_URI=bolt://127.0.0.1:1` 같은
   죽은 URI를 주입한 상태에서도 KG-off 경로 전부가 green임을 검증한다.
3. personal node(`node_kind=personal`): `kg_enabled`가 false인지 assert —
   personal node에는 Neo4j가 없다(kg-model §1).

### 3.4 K1 테스트 케이스

| 테스트 | 종류 | 검증 |
|---|---|---|
| `test_kg_disabled_default` | unit | `Settings().kg_enabled is False`, `kg_owner_scope_enabled is False` |
| `test_get_kg_driver_disabled_raises` | unit | flag off → `KgDisabled`, driver 생성 시도 없음 |
| `test_kg_available_false_on_dead_uri` | unit | 죽은 URI + flag on → `kg_available() is False`, 예외 미전파 |
| `test_bootstrap_idempotent` | integration(`kg` fixture) | `apply_schema` 2회 → 2회째 `already_present`가 전체 |
| `test_bootstrap_creates_kg_meta_once` | integration | `ensure_kg_meta` 2회 → KgMeta 노드 1개, version 보존 |

`tests/conftest.py`에 세션 fixture 추가(기존 `pg()` 패턴 동형):

```python
@pytest.fixture(scope="session")
def kg():
    # bolt://127.0.0.1:7688 (neo4j-test) 연결 시도, 실패 시
    # pytest.skip("neo4j-test not running — make kg-test-up")
    # yield driver; teardown에서 MATCH (n) DETACH DELETE n + close
```

함수 단위 격리는 `kg_clean(kg)` fixture가 테스트 전 그래프 전체 wipe로 보장
한다(`clean`의 그래프판 — tmpfs 컨테이너라 비용 무시 가능).

설정 주입 계약: `clean`(§1.4)은 `kg_enabled=False`를 기본 고정하고, KG 통합
테스트는 `kg_clean`이 그 위에 `kg_enabled=True`,
`kg_uri="bolt://127.0.0.1:7688"`, `kg_password="orthus-kg-test"`를 override
한다 — 테스트가 실수로 운영 포트(`7687`)를 보는 일이 구조적으로 불가능하다.

### 3.5 K1 완료 게이트 (K2 진입 조건)

전부 충족 — K1 구현 완료(2026-06-11), K2가 그 위에 머지됨(2026-06-12):

- [x] `make up` 후 `orthus_neo4j` healthy, ports가 `127.0.0.1`에만 바인딩
      (`docker port orthus_neo4j` 증거)
- [x] `make kg-bootstrap` 2회 멱등
- [x] `make node-smoke NODE=company` / `NODE=personal-a` green (KG off)
- [x] §3.4 테스트 전부 green, CI 기존 job 무회귀
- [x] `docs/operations.md` 포트 표·secret 표에 KG 행 반영 확인(이미 K0에서
      반영 — drift만 체크)

---

## 4. K2 — 결정론 projection

### 4.1 처리 흐름 의사코드

```text
rebuild():                                   # python -m orthus.kg.rebuild
  with audit("kg.rebuild") as span:
    driver 확보; apply_schema(멱등 — kg-bootstrap 선행 실행 없이도 동작하도록 내장 재실행)
    snapshot_started_at = PG now()           # watermark 후보 — §4.7
    rows  = project.load_all(scope_filter=COMPANY)     # PG + frontmatter join
    plan  = project.build(rows)              # → KgNodeRow[]/KgEdgeRow[] + 기대 키 집합
    store.merge_all(driver, plan)            # §4.5 UNWIND batch
    store.prune(driver, plan.expected_keys)  # §4.6 SoR에 없는 잔존물 제거
    store.set_meta(last_rebuild_at=now, last_sync_at=snapshot_started_at,
                   kg_schema_version=KG_SCHEMA_VERSION)
    span.add_meta(**plan.counts)

sync():                                      # python -m orthus.kg.sync
  with audit("kg.sync") as span:
    meta = store.get_meta()
    if meta.kg_schema_version != KG_SCHEMA_VERSION: exit("rebuild required")
    if meta.last_sync_at is None:            exit("rebuild required")
    since = meta.last_sync_at - OVERLAP_60S  # §4.7 — 멱등 MERGE라 중복 무해
    snapshot_started_at = PG now()
    rows = project.load_changed(since, scope_filter=COMPANY)
    plan = project.build(rows)               # 변경분만 — prune 없음 (§3 삭제 의미론)
    store.merge_all(driver, plan)
    store.set_meta(last_sync_at=snapshot_started_at)
```

`project.py`는 **Neo4j를 import하지 않는다** — 입력 PG/frontmatter, 출력
`KgNodeRow`/`KgEdgeRow`. 결정론 검증(같은 SoR → 같은 plan)이 unit 테스트로
가능해진다. 공개 시그니처(§4.5 store와 §5.4 outbox가 공유):

```python
class KgProjectionPlan(BaseModel):
    nodes: list[KgNodeRow]
    edges: list[KgEdgeRow]
    expected_keys: ExpectedKeys | None = None  # full load일 때만 — §4.6 prune 입력
    counts: dict[str, int] = {}                # 라벨/rel별 집계 — audit meta용

def load_all(*, scope_filter: ScopeFilter) -> SourceRows       # full SELECT (§4.2)
def load_changed(since: datetime, *, scope_filter: ScopeFilter) -> SourceRows
def load_one(entity_kind: str, entity_id: UUID) -> SourceRows  # K3 단일 row (§5.4)
def build(rows: SourceRows) -> KgProjectionPlan                # 순수 결정론 변환
```

`SourceRows`는 테이블별 row 목록 + conflict task index(§4.3)를 묶은 컨테이너
모델이고, `ScopeFilter`는 §4.2 WHERE절의 선택 enum이다 — v1은 `COMPANY`
하나뿐이며 K7이 `COMPANY_PLUS_OWNER` 변형을 추가한다(§10.1). 세 load 함수가
전부 같은 `build()`를 지나므로 batch/sync/outbox 세 경로의 변환 결과가
구조적으로 동일하다. `ExpectedKeys`의 필드 구성은 §4.6에 정의돼 있다.

### 4.2 SoR SELECT 계약 (라벨별)

전부 `scope='company'` 필터가 SELECT에 박힌다 — 이 WHERE절 자체가 K2 boundary
회귀의 검증 대상이다(kg-model §3).

| 라벨 | SELECT (개념 SQL) | 속성 매핑 |
|---|---|---|
| `:WikiPage`/`:WikiClaim`/`:WikiSource` | `SELECT page_id, slug, kind, title, confidence, content_hash, project, updated_at FROM wiki_pages WHERE scope='company' AND kind IN ('page','claim','source') [AND updated_at > :since]` | kind→라벨 분배. `:WikiSource`의 `source_type`/`source_ref`는 PG에 없음 → frontmatter join(§4.3) |
| (task) | 같은 SELECT 골격에 `kind='task'` 필터로 별도 조회 — **노드로 투영하지 않음.** CONFLICTS_WITH 엣지 속성 재료로만 사용. sync 변경분에 task row가 잡히면 그 task의 `related` claim 쌍에 해당하는 CONFLICTS_WITH 엣지 **속성**을 재투영 대상에 포함한다(엣지 존재 여부는 여전히 `wiki_links`가 결정) | §4.3 |
| `:Document` | `SELECT doc_id, source, project, source_db_name, updated_at FROM documents WHERE scope='company' [...]` | 그대로 |
| `:StructuredFact` | `SELECT row_id, record_type, confidence, source_doc_id, properties->>'valid_until' AS valid_until, updated_at FROM structured_rows WHERE scope='company' [...]` | `valid_until`은 ISO 문자열→datetime 파싱 실패 시 null(발명 금지) |
| `:Project` | enum 고정 4행(`atlas/nova/orbit/company`) — SELECT 없음 | — |
| 엣지(wiki_links) | `SELECT l.src_page_id, l.dst_slug, l.rel, p.kind, p.slug FROM wiki_links l JOIN wiki_pages p ON p.page_id=l.src_page_id WHERE p.scope='company'` | rel→`WIKI_LINK_REL_MAP`. dst_slug resolve는 §4.4 |
| 엣지(provenance) | `:WikiSource`의 frontmatter `source_type=='corpus_doc'`이고 `source_ref`가 UUID 파싱 가능 + 해당 `documents.doc_id`(company)가 존재할 때만 `EXTRACTED_FROM` 생성. 그 외(`qa_session`/`conversation`/미존재)는 **엣지 생략** — Document placeholder를 만들지 않는다 | kg-model 엣지 저작 원칙 |
| 엣지(fact provenance) | `structured_rows.source_doc_id IS NOT NULL` + 대상 doc이 company → `EXTRACTED_FROM` | — |
| 엣지(IN_PROJECT) | wiki_pages(kind='page')·documents의 `project` 컬럼 → `:Project` 허브로 | — |

`last_accessed_at`은 projection이 **건드리지 않는다** — 읽기 경로(K4)가
SET하는 관측 속성이므로, MERGE의 SET 절에서 명시적으로 제외한다(rebuild가
관측값을 지우지 않도록 `ON CREATE`에서만 null 초기화).

### 4.3 CONFLICTS_WITH 속성 join 알고리즘

kg-model §2 매핑 규칙의 구현 계약:

```text
build_conflict_index() -> dict[frozenset[slug,slug], TaskMeta]:
  for task_slug in store.list_slugs("task", scope="company"):       # store.py:853
      t = store.load_task(task_slug, scope="company")                # store.py:905
      if t is None or t.kind != "conflict": continue
      meta = TaskMeta(detected_at=t.created_at, reason=t.description,
                      status="RESOLVED" if t.resolved else "UNRESOLVED")
      for (a, b) in itertools.combinations(sorted(set(t.related)), 2):
          key = frozenset({a, b})
          if key not in index or meta.detected_at > index[key].detected_at:
              index[key] = meta                      # 같은 쌍에 task 복수 → 최신 우선

엣지 생성: wiki_links rel='conflicts' 행마다
  props = index.get(frozenset({src_slug, dst_slug}))
  매칭 없음 → status='UNRESOLVED', detected_at/conflict_reason=null
  resolved_favoring_id는 항상 null (v1 — kg-model §2)
```

`related`에 claim이 아닌 page slug가 섞여 있어도 무해하다 — 엣지 존재는
`wiki_links`가 결정하고 index는 속성 lookup일 뿐이므로, 매칭 실패는 null
속성으로 수렴한다(발명 금지 원칙과 합치).

### 4.4 dst_slug resolve + placeholder 승격

```text
resolve_dst(dst_slug, company_pages: dict[slug, page_id]):
  if dst_slug in company_pages: → 실체 노드 (merge by page_id)
  else: → placeholder KgNodeRow(label=WIKI_PAGE, merge_key="slug",
                                merge_value=dst_slug,
                                props={slug, materialized: false})

실체 승격 (kg-model §2 규칙의 Cypher 계약):
  // 실체 page upsert 시 — page_id MERGE보다 먼저 실행
  MATCH (ph:WikiPage {slug:$slug}) WHERE ph.page_id IS NULL
  SET ph.page_id=$page_id, ph += $props, ph.materialized=true
  // 위 MATCH가 0행이면 일반 MERGE (p:WikiPage {page_id:$page_id})
```

승격은 단일 트랜잭션 안에서 "placeholder 우선 조회 → 없으면 page_id MERGE"
순서를 지킨다 — 같은 slug의 placeholder/실체 중복 공존 금지 계약.
batch UNWIND와의 결합: placeholder 존재 가능성이 있는 `:WikiPage` upsert만
이 2단계 변형을 쓰고, `:WikiClaim`/`:WikiSource` 등 dangling이 불가능한
라벨은 단순 MERGE를 쓴다.

### 4.5 MERGE batch 계약 — `orthus/kg/store.py`

```python
def merge_nodes(session, label: Label, key: str, rows: list[dict],
                batch_size: int = 1000) -> int
    # UNWIND $rows AS row MERGE (n:<label> {<key>: row.<key>})
    #   ON CREATE SET n += row.props, n.last_accessed_at = null
    #   ON MATCH  SET n += row.props        # 멱등 SET — last_accessed_at은 props에 없어 보존
    # 1k rows/tx (kg-model §3). 라벨/키는 상수 enum에서만 — 문자열 보간 금지.

def replace_page_link_edges(session, src_page_id: str, edges: list[KgEdgeRow]) -> None
    # wiki_links 유래 rel(SUPPORTS/CONFLICTS_WITH/BACKLINK/DERIVED_FROM)만:
    #   MATCH (s {page_id:$src})-[r]->() WHERE type(r) IN $link_rels DELETE r
    #   이후 edges MERGE — store._replace_links(:645) 의미론의 그래프판.
    # EXTRACTED_FROM/IN_PROJECT는 이 삭제 대상에서 제외(컬럼 유래 — 별도 MERGE).

def merge_edges(session, rel: Rel, rows: list[dict], batch_size: int = 1000) -> int
    # UNWIND + MATCH src/dst + MERGE (src)-[r:<rel>]->(dst) SET r += row.props

def prune(session, expected: ExpectedKeys) -> PruneReport          # §4.6
def get_meta(session) -> KgMeta;  def set_meta(session, **fields) -> None
```

diff 최적화(선택): sync 경로는 어차피 `updated_at > since` 변경분만 다루므로
v1 구현에서 content_hash 비교로 MERGE를 생략하는 최적화는 **하지 않아도
계약 위반이 아니다**(MERGE+SET는 멱등). kg-model §3의 diff 키 표는 "변경
감지의 권위가 무엇인가"의 계약이며, 성능 최적화 도입 시점은 측정 후 결정.

### 4.6 prune(삭제 수렴) 알고리즘 — rebuild 전용

```text
ExpectedKeys = {
  page_ids_by_label: dict[Label.value, set[UUID]]  # 라벨별 — K2 리뷰 교정(2026-06-12):
                                 # _upsert_page_row가 기존 row의 kind를 UPDATE할 수
                                 # 있어(정체성은 (slug,scope,owner)), 합집합 set이면
                                 # kind가 바뀐 page_id의 구 라벨 노드가 prune을
                                 # 영원히 통과한다 — 라벨별 분리가 계약이다
  placeholder_slugs: set[str]    # 이번 plan에서 미해결로 남은 dst_slug
  doc_ids / row_ids: set[UUID]
  edge_triples: set[(src_key, rel, dst_key)]
}

prune 순서 (각각 1k 단위 배치):
 1. 엣지: 그래프의 (src,rel,dst) 전수 조회 → expected에 없는 r DELETE
 2. placeholder: MATCH (p:WikiPage) WHERE p.page_id IS NULL
                 AND NOT p.slug IN $placeholder_slugs → DETACH DELETE
 3. 실체 노드: 라벨별 MATCH n WHERE n.<key> IS NOT NULL
               AND NOT n.<key> IN $expected → DETACH DELETE
    # <key> IS NOT NULL 조건이 placeholder(2에서 이미 처리)를 명시적으로 제외
 4. :KgMeta / :OutboxApplied는 prune 대상에서 제외 (동기화 메타)
    단, :OutboxApplied는 K3에서 보존 기간(예: 30일) 경과분만 별도 trim
```

9천 page 규모에서 전수 키 비교는 메모리에 충분히 올라간다(UUID 36B × 수만).
규모가 커지면 라벨별 스트리밍 비교로 바꾼다 — v1에서는 단순함 우선.

### 4.7 watermark / 스키마 버전 시맨틱

- `:KgMeta {id:'kg_meta', last_sync_at, last_rebuild_at, kg_schema_version}`
  (kg-model §3 — PG에 KG 상태 저장 금지).
- watermark는 **PG `now()` 스냅샷 시작 시각**으로 갱신한다(처리 완료 시각이
  아님). projection 도중 커밋된 row를 다음 sync가 놓치지 않게 하는 장치이고,
  추가로 `OVERLAP_60S`를 빼서 commit-시각/`updated_at` 시계 차를 흡수한다.
  중복 재처리는 MERGE 멱등으로 무해.
- `kg_schema_version` 불일치 시 sync는 거부하고 exit 메시지로 rebuild를
  요구한다. rebuild만 version을 현재 값으로 SET한다. **bump 정책**:
  additive 변경(새 라벨/관계/속성 추가 — K6 entity가 해당)은 bump하지
  않는다(기존 그래프와 공존 가능, sync가 자연 반영). 기존 키/속성의 의미
  변경(K7의 placeholder 키 전환·scope 속성 소급)만 bump + rebuild 강제
  대상이다.
- rebuild와 sync/worker 동시 실행 방지: rebuild 시작 시
  `:KgMeta.rebuild_lock_until = now()+30min`을 SET하고, sync/worker는 lock
  활성 시 no-op 대기한다. rebuild가 정상 완료하면 lock을 즉시 해제(null
  SET)하고, 비정상 종료 시에는 시간 만료로 자연 해제된다 — 단일 운영자/
  단일 호스트 전제의 보수적 락이다.

### 4.8 스케줄러 합류

- `scripts/node/sync_cycle.sh`에 `REBUILD_WIKI=1` 패턴 동형으로
  `KG_SYNC=1`(기본 0) 분기 추가: company node env에서만
  `uv run python -m orthus.kg.sync` 실행.
- 주기 full rebuild(삭제 수렴 권위)는 launchd off-peak 별도 plist
  (`ai.orthus.company.kg-rebuild`, 1일 1회)로 — K2 PR에서는 Makefile target
  까지만 제공하고 plist 설치는 운영 단계 문서(`docs/operations.md`)에 기록.
- 최초 프로덕션 활성화(off→on, K2 머지 후) 절차 — `docs/operations.md`에
  기록: ① keychain `orthus/kg/password` 등록(+`.env` fallback 확인) →
  ② `make up`(neo4j 기동) → ③ `make node-kg-bootstrap NODE=company` →
  ④ `make node-kg-rebuild NODE=company` → ⑤ company `node.env`에
  `ORTHUS_KG_ENABLED=true` → ⑥ API 재기동. **rebuild가 flag보다 먼저다** —
  켜진 상태에서 빈 그래프를 노출하지 않는다. *(K2 리뷰 교정: prod는 root
  `.env`가 아니라 node env — §3.1 SoR 단일성.)*

### 4.9 K2 엣지케이스

| 케이스 | 처리 |
|---|---|
| slug rename(page_id 동일, slug 변경) | MERGE 키가 page_id라 노드는 자동 갱신. **구 slug placeholder가 남을 수 있음** → rebuild prune이 수렴. sync-only 기간의 잔존은 허용(다음 rebuild까지) — 문서화된 eventual consistency |
| 같은 claim 쌍에 conflict task 다수 | `created_at` 최신 우선(§4.3) — kg-model §2 규칙 |
| `valid_until` 파싱 불가 문자열 | null 처리 + audit meta에 카운트 — 발명 금지 |
| wiki-store frontmatter와 PG row 불일치(고아 PG row) | frontmatter 로드 실패 시 보강 속성을 **null로 포함**해 투영하고(키 생략이 아님 — Neo4j `SET +=`는 생략된 키를 못 지워 stale 값이 rebuild로도 잔존한다, K2 리뷰 교정 2026-06-12) `EXTRACTED_FROM` 엣지는 생략 + audit warning 카운트. projection이 SoR 불일치를 "수리"하지 않는다 |
| 빈 그래프에서 sync 호출 | `last_sync_at` null → "rebuild required" exit (§4.1) |
| dst_slug가 자기 자신(self-link) | SoR에 있으면 그대로 투영 — projection은 검열하지 않는다 |

### 4.10 K2 테스트 케이스

| 테스트 | 검증 |
|---|---|
| `test_rebuild_idempotent_two_runs` | rebuild 2회 → 노드/엣지/속성 snapshot 동일 (수용 기준 1) |
| `test_parity_counts_company_scope` | 라벨별 노드 수 == 대응 SQL count(placeholder 제외), rel별 엣지 수 == wiki_links/provenance/IN_PROJECT count (kg-model §5 parity 정의) |
| `test_owner_scope_rows_not_projected` | personal row(`scope='personal'`) seed 후 rebuild → 그래프에 해당 page_id/row_id/doc_id 부재 (**boundary 회귀** — SELECT 필터·rebuild 결과 양쪽) |
| `test_conflict_edge_props_from_task` | conflict task 유/무/복수/resolved 4조합 → 속성 매핑 정확 |
| `test_placeholder_promotion_no_duplicate` | dangling 링크 → placeholder → 실체 page 추가 후 sync → 같은 slug 노드 1개, materialized=true |
| `test_prune_removes_deleted_rows` | row 삭제 후 rebuild → 노드/엣지 제거, KgMeta/OutboxApplied 보존 |
| `test_sync_watermark_overlap` | watermark 직전 commit row가 다음 sync에 포함 |
| `test_sync_refuses_on_schema_version_mismatch` | version 강제 불일치 → exit, 그래프 무변경 |
| `test_project_build_deterministic` (unit) | 같은 입력 fixture → 같은 plan (Neo4j 불필요) |
| `test_no_body_text_in_graph` | 어떤 노드 속성에도 markdown 본문/chunk 부재 — PII 계약 회귀 |

### 4.11 K2 완료 게이트 (K3/K4 진입 조건)

- [x] §4.10 전부 green (특히 boundary 회귀·멱등·parity — 수용 기준 1·5 전반부)
- [x] 실데이터 rebuild 측정치(소요 시간/노드·엣지 수) PR evidence에 기록 →
      kg-model §7 성능 가정 검증
- [x] CI에서 neo4j service container 채택 여부 결정·기록(§11-7)

**K2 실측 기록 (2026-06-12, dev 머신 실데이터 orthus DB):** company scope
wiki_pages 5,215행(claim 1,824/page 1,617/source 1,774) + documents 2,439 +
structured_rows 432 + wiki_links 12,620 입력에서 full rebuild가 노드 8,090
(placeholder 5 포함) + 엣지 18,625를 투영, 소요 약 2m20s(wall; user ~26s —
대부분 frontmatter 디스크 로드와 Neo4j I/O 대기). 2회 연속 실행 카운트 동일 +
prune 0건(멱등), 직후 `kg-sync` no-change 정상. off-peak 일일 batch 가정
(kg-model §7)을 실측으로 확인 — UNWIND batch 튜닝은 필요 시 별도.

**K2 구현 교정 2건(계약 의미 불변):** ① `KgEdgeRow`에 `src_label`/`dst_label`
필드 추가(§2.5 주석 — 라벨 없는 엣지 MATCH는 전수 스캔). ② diff 키 계약
(kg-model §3 표)에 맞춰 `content_hash`를 `:WikiClaim`/`:WikiSource` props에도
저장한다(kg-model §2 주요 속성 표는 WikiPage에만 명시했으나 diff 키는 세 라벨
공통 — kg-model §2도 같은 PR에서 병기).

**K2 리뷰 교정 6건(2026-06-12, 정밀 리뷰 후속 — 회귀 테스트 동반):**
① prod 운영 경로를 `node-kg-*` target으로 분리(§3.1 SoR 단일성 — root/node
env 혼용 시 prune이 상대 SoR 투영을 삭제). ② `client.require_company_node()`
코드 레벨 가드 — projection 쓰기 경로는 company node 전용(셸 가드와 별개,
`test_kg_refuses_on_personal_node`). ③ `ExpectedKeys.page_ids_by_label` 라벨별
분리(§4.6 — kind-flip 잔존, `test_prune_removes_stale_node_after_kind_change`).
④ `/projects` 재태깅·slack retag의 `documents`/`wiki_pages`/`structured_rows`
UPDATE에 `updated_at` bump 추가 — sync watermark 계약의 전제
(`test_sync_picks_up_project_retag`; 신규 SoR 쓰기 경로도 같은 의무).
⑤ `:WikiSource` frontmatter 보강 속성을 null 포함으로 투영(§4.9 —
`SET +=`는 생략 키를 못 지움, `test_rebuild_clears_stale_source_front_props`).
⑥ 테스트 `clean`이 `secret_backend="memory"` 고정 — keychain이 settings
override보다 우선해 운영자 Mac에서 KG 테스트가 prod credential로 붙는 문제 차단.
CI cleanup도 `docker compose --profile test down -v`로 교정(neo4j-test orphan).

---

## 5. K3 — transactional outbox

### 5.1 migration DDL

merge 시점의 다음 빈 번호를 쓴다(본 문서 작성 시점 head는 `0046` — 가칭
`0047_kg_outbox`). 스키마는 kg-model §3 표가 canonical이며 DDL로 옮기면:

```sql
CREATE TABLE kg_outbox (
  outbox_id      UUID PRIMARY KEY,
  entity_kind    TEXT NOT NULL CHECK (entity_kind IN ('wiki_page','document','structured_row')),
  entity_id      UUID NOT NULL,
  op             TEXT NOT NULL CHECK (op IN ('upsert','delete')),
  status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','applied','dead')),
  attempts       INT  NOT NULL DEFAULT 0,
  lease_until    TIMESTAMPTZ NULL,
  last_error     TEXT NULL,
  correlation_id UUID NULL,
  enqueued_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_kg_outbox_status_enqueued ON kg_outbox (status, enqueued_at);
```

`tests/conftest.py::clean`의 wipe 목록에 `kg_outbox` 추가(필수 — 누락 시
테스트 간 이벤트 누수).

### 5.2 enqueue 계약

```python
# orthus/kg/outbox.py
KgOutboxEntityKind = Literal["wiki_page", "document", "structured_row"]  # §5.1 CHECK와 1:1

def enqueue_kg_event(s: Session, *, entity_kind: KgOutboxEntityKind, entity_id: UUID,
                     op: str = "upsert", scope: str,
                     correlation_id: UUID | None = None) -> None
    # scope != 'company' → no-op (K7에서 owner-scope로 확장 — §10.1)
    # settings.kg_enabled=false → no-op. flag off 기간의 변경은 outbox에 쌓지
    #   않고, flag를 켤 때 rebuild 1회를 운영 절차로 요구한다(docs/operations.md에 기록) —
    #   off 기간 적체가 on 직후 폭주하는 것보다 단순한 계약이다.
    # 같은 PG 세션 s에 INSERT — 호출자 트랜잭션과 원자적으로 commit/rollback.
```

호출 지점 3곳(각 1줄 hook — 해당 세션의 `commit()` 이전):

| 지점 | entity_kind | 비고 |
|---|---|---|
| `orthus/wiki/store.py::_persist` (`:779`, commit `:835` 직전) | `wiki_page` | consolidate의 source/claim/page/task 쓰기 전부가 이 단일 지점을 지난다 — kg-model §3 1항이 명시한 실제 commit 트랜잭션 지점. task row 이벤트도 enqueue한다(CONFLICTS_WITH 속성 재투영 트리거). WikiTask resolve(`PATCH /wiki/tasks` — `wiki.py` `patch_wiki_task`)도 `store.write_task`→`_persist`를 지남을 실측 확인(2026-06-11) — 충돌 해소 상태 변경이 자동으로 잡힌다 |
| `orthus/documents.py::publish_agent_draft_document` (`:152`, commit `:206` 직전) | `document` | publish만 — draft save는 제외(P3.4b 경계 그대로) |
| `orthus/documents.py::upsert_source_document` — documents row를 commit하는 **자체 세션 내부**(`:258`, commit `:289`/`:321`) | `document` | **실측 교정(2026-06-11)**: promote approve(`orthus/promote.py:186`)는 doc row를 직접 commit하지 않는다 — `upsert_source_document`가 자기 세션에서 commit하므로, "데이터 변경과 같은 트랜잭션에서 enqueue"라는 outbox 원칙을 지키려면 hook은 이 함수 안이다(company 조건은 enqueue helper의 scope 분기가 담당). promote 외에 이 함수를 지나는 company 문서 쓰기 경로가 있어도 이벤트는 멱등 upsert라 무해 — K3 PR에서 호출자 전수를 확인해 기록한다(kg-model §3도 같은 실측 hook 지점으로 개정됨, 2026-06-11). 후속 corpus/wiki authoring이 일으키는 wiki 쓰기는 `_persist` hook(표 첫 행)이 다시 잡는다 |

`structured_rows` 변경(slack backfill/refresh)은 K3에서 **enqueue하지
않는다** — kg-model §3이 명시한 세 원천이 전부이고, structured fact의 그래프
반영은 주기 `kg-sync`가 담당한다(수용 기준 2의 60초 SLA는 wiki consolidate
기준). 필요해지면 `orthus.structured.slack_*`에 같은 helper를 다는 것이 확장
경로다.

delete 이벤트: 현행 코드에 wiki page/document hard-delete 경로가 없으므로
K3 시점에 `op='delete'` enqueue 호출처는 없고, 첫 호출처는 K6 erasure 절차(§9.4)다.
worker의 delete 처리(§5.4)는 K3에서 먼저 구현해 둔다.

### 5.3 worker 설계

```python
class KGOutboxWorker:
    def __init__(self, *, poll_seconds: int, batch_size: int = 20,
                 lease_seconds: int = 60, max_attempts: int = 5): ...

    def run_forever(self, stop: threading.Event) -> None
        # poll loop: claim → apply → 다음 poll. KgUnavailable이면 claim 해제(§2.6)

    def claim(self, s) -> list[OutboxRow]
        # SELECT ... FROM kg_outbox
        #  WHERE status='pending' AND (lease_until IS NULL OR lease_until < now())
        #  ORDER BY enqueued_at LIMIT :batch FOR UPDATE SKIP LOCKED;
        # UPDATE lease_until = now() + lease_seconds; commit

    def apply_one(self, driver, row: OutboxRow) -> None
        # 단일 Cypher 트랜잭션 (kg-model §3 3항):
        #   MERGE 마커 검사: MATCH (:OutboxApplied {outbox_id:$id}) → 있으면 no-op
        #   없으면: §5.4 entity 재투영 + CREATE (:OutboxApplied {outbox_id, applied_at})
        # 성공 → PG status='applied'; 실패 → attempts+=1, last_error 기록,
        #   attempts>=5 → status='dead'  (audit("kg.apply") span, correlation_id 전파)
```

`OutboxRow`는 `kg_outbox` 컬럼의 1:1 Pydantic 사상이다(§5.1 DDL).

런타임: **FastAPI lifespan background thread**(중앙 API 프로세스, daemon
thread + stop event). 통합 지점은 기존 `orthus/api/main.py:34`
`async def lifespan(_app)` — startup에서 기동 조건
`kg_enabled and node_kind == "company"` 검사 후 thread start, shutdown에서
stop event set + `join(timeout)`. 선택 근거와 대안은 §11-3. 수동 drain CLI
`python -m orthus.kg.outbox drain`을 함께 제공한다(테스트·장애 복구·launchd
fallback 공용).

### 5.4 단일 이벤트 apply 매핑

| entity_kind / op | 동작 |
|---|---|
| `wiki_page` / upsert | PG에서 해당 page_id row + frontmatter 재로드(company 필터 통과 못 하면 no-op) → 노드 MERGE → `replace_page_link_edges`로 그 page의 링크 엣지 통째 재투영(kg-model §3 — wiki_links는 별도 entity_kind가 아님). kind='task'면 노드 없이 conflict index 갱신 → 관련 CONFLICTS_WITH 엣지 속성 재SET |
| `document` / upsert | doc row 재로드 → `:Document` MERGE + IN_PROJECT/EXTRACTED_FROM 재투영 |
| `structured_row` / upsert | (K3에 enqueue 원천 없음 — worker 지원만) row 재로드 → `:StructuredFact` MERGE + EXTRACTED_FROM |
| any / delete | 해당 키 노드 DETACH DELETE (없으면 no-op) |

재투영은 K2 `project.py`의 단일-row 변형(`load_one(entity_kind, entity_id)`)
을 재사용한다 — batch와 outbox가 같은 변환 코드를 지나므로 두 경로의 결과
드리프트가 구조적으로 불가능하다.

### 5.5 K3 엣지케이스

| 케이스 | 처리 |
|---|---|
| upsert 이벤트 처리 시점에 row가 이미 삭제됨 | 재로드 miss → no-op + 마커 생성(이벤트 소비). 최종 상태는 rebuild가 권위 |
| 이벤트 순서 역전(같은 row upsert 2건) | apply가 항상 "현재 PG 상태" 재로드라 마지막 적용이 곧 최신 — 순서 무관 수렴 |
| worker 죽음(마커 생성 후 PG update 전) | 재claim 시 마커 존재 → no-op → status='applied' 갱신. 멱등 보장 지점 |
| rebuild 동시 실행 | `:KgMeta.rebuild_lock_until` 검사 후 대기(§4.7) |
| dead-letter 가시화 | `kg_outbox WHERE status='dead'` — K6 모니터링에서 노출(§9.4). K3에서는 audit error span + CLI 조회로 충분 |
| 적용 완료 row 적체(PG 용량) | `status='applied'` row는 30일 경과 후 주기 정리(단순 DELETE batch). 멱등 마커의 권위는 Neo4j `:OutboxApplied`라 PG row 삭제가 재적용을 유발하지 않는다. `dead`는 운영자 처리 전까지 보존 |

### 5.6 K3 테스트 케이스

`test_outbox_enqueue_in_same_tx`(롤백 시 이벤트도 롤백) ·
`test_outbox_enqueue_company_only`(personal 쓰기 → 이벤트 0건 — K7 전 경계) ·
`test_worker_apply_idempotent_replay`(같은 이벤트 2회 claim → 그래프 무변경) ·
`test_worker_dead_letter_after_5`(강제 실패 5회 → dead + last_error) ·
`test_worker_lease_reclaim`(lease 만료 후 재claim) ·
`test_consolidate_to_graph_e2e`(consolidate → drain → 그래프 반영; 60초 SLA의
기계 검증판) · `test_neo4j_down_accumulates_then_drains`(컨테이너 정지 →
적체 → 재기동 → drain) · node-smoke 재실행.

### 5.7 K3 완료 게이트

- [x] §5.6 green + 기존 wiki/documents/promote 통합 테스트 무회귀
- [x] enqueue hook 3곳이 각 1줄 수준(호출자 로직 비침투) — diff 리뷰 기준
- [x] 수용 기준 2 전반부(60초 내 반영) 로컬 evidence — 측정 구간은
      consolidate의 PG commit 시각 → 해당 이벤트의 `:OutboxApplied` 생성
      시각으로 정의한다

**K3 실측 기록 (2026-06-12, dev 머신):** 60초 SLA 측정치 — consolidate PG
commit → `:OutboxApplied` 생성 **1.65s**(`test_consolidate_to_graph_e2e`,
drain 직접 호출 기준; lifespan worker 경로는 poll 주기 ≤5s가 더해진다).
§5.6 전수 + 추가 회귀(rebuild lock 대기/`op='delete'`/trim/문서 hook) 13개
green, 전체 suite는 main baseline과 동일 결과(K3 회귀 0건).

**K3 구현 기록 5건(계약 의미 불변):**

① **trim 위치(2026-06-12 사용자 결정):** `applied` row(PG)와 `:OutboxApplied`
마커(Neo4j)의 30일 정리는 worker poll loop 내 저빈도(1시간) best-effort +
`python -m orthus.kg.outbox trim` CLI 옵션으로 구현했다(launchd plist 추가
없음 — §4.8 K2 선례와 일관). 마커는 자체 `applied_at` 기준이라 PG row가 먼저
사라진 마커도 수렴하고, 극단 케이스(30일 넘게 pending인 이벤트의 마커
선삭제)도 apply가 '현재 PG 상태' 재로드 멱등이라 무해하다.
② **task 이벤트 구현:** `project.load_one`이 `kind='task'` row(노드 미투영
대상)를 인식해 conflict task의 related 쌍에 해당하는 conflicts 링크 +
conflict index를 싣는다 — task 이벤트도 batch와 같은 `build()`를 지난다(§5.4
"같은 변환 코드" 계약 유지).
③ **KgUnavailable 정규화:** driver 생성이 lazy 연결이라 미가용이 첫
쿼리에서 raw `ServiceUnavailable`/`SessionExpired`/`AuthError`로 드러난다 —
worker가 이를 `KgUnavailable`로 정규화해 "인프라 다운은 attempts 미증가"
계약(§2.6)을 예외 타입 수준에서 지킨다.
④ **다운타임 회귀 방식(2026-06-12 사용자 결정):**
`test_neo4j_down_accumulates_then_drains`는 CI 공유 neo4j-test 컨테이너를
멈추지 않고 죽은 URI 주입으로 미가용을 시뮬레이션한다(K1 node-smoke 기법
동형). 실제 컨테이너 정지→재기동 사이클은 로컬 evidence로 별도 기록한다.
⑤ **`upsert_source_document` 호출자 전수(§5.2 의무):** `dashboard_wiki`
(company) · `personal_board`(personal — enqueue no-op) · `promote.py`
approve(company) · `mail/ingest.py`(company) · `connectors/base.py`(account
정책 scope). promote 외 company 경로의 이벤트도 전부 멱등 upsert라 무해 —
계약 예상대로다. insert/update 두 commit 분기에 각 1줄 hook.

**K3 리뷰 교정 7건(2026-06-12, 머지 전 정밀 리뷰 — 회귀 테스트 동반):**
① `load_one` task 분기에 비-빈 `related` 가드 — 빈 related conflict task
이벤트가 `_load_links` 무필터 전사 링크 로드(~12.6k)로 퇴화하는 것을 차단
(batch `_load`의 기존 가드와 동형, `test_task_event_empty_related_noop`).
② 이벤트 실패 시 lease를 backoff로 유지 — 즉시 재claim으로 attempts 5가
1초 안에 소진돼 일시 오류가 dead가 되는 것을 방지(`dead`만 lease 해제;
dead-letter 회귀가 attempt 간 lease 만료를 명시 시뮬레이션).
③ `run_once`가 **성공 적용 수만** 반환 — drain/CLI `applied=` 보고가 실패를
집계하지 않고, 전부-실패 drain은 pending 잔존 → exit 1(fail-loud)로 수렴.
④ `trim_applied`의 Neo4j 경로 예외를 `KgUnavailable`로 정규화 — lazy 연결로
미가용이 raw driver traceback으로 CLI를 죽이던 것 교정
(`test_trim_unavailable_normalized`).
⑤ `_normalize_unavailable`에 `TransientError` 추가 — worker·CLI drain이
lease 만료 직후 겹칠 때의 락 경합/constraint 패자 오류가 attempts를 소진하지
않게(중복 마커 자체는 K1 `outbox_marker` 유니크 제약이 차단함을 리뷰에서
확인, `test_normalize_unavailable_taxonomy`).
⑥ `run_once`에 `require_company_node()` — K2 교정 ②(rebuild/sync 핵심 경로
가드)와 동형으로 worker 핵심 경로를 fail-closed
(`test_run_once_refuses_on_personal_node`).
⑦ claim/release 등 PG-경로 실패와 미가용 idle 상태에 stderr 관측 추가
(연속 동일 메시지 억제) — `kg.apply` audit span이 커버하지 못하는 적체
원인(비밀번호 미설정, PG 오류)의 무증거 상태 제거. 리뷰에서 기각된 후보:
중복 `:OutboxApplied` 축적(유니크 제약 존재), structured_rows의 document
이벤트 미반영(§5.2 명시 — kg-sync가 커버, 전 쓰기 경로 `updated_at` bump
확인), rebuild 중간-batch 경합(다음 poll/sync에서 자가 치유 — 허용).

---

## 6. K4 — 읽기 경로 (template gate + page graph API)

### 6.1 템플릿 registry — `orthus/kg/templates.py`

```python
class NeighborsParams(BaseModel):
    slug: str = Field(min_length=1)
    depth: Literal[1, 2] = 1

class PathBetweenParams(BaseModel):
    slug_a: str; slug_b: str
    max_hops: Literal[2, 3, 4] = 4
    # model_validator: slug_a == slug_b → ValidationError. Neo4j shortestPath는
    # 동일 시작/끝 노드를 기본 설정에서 에러로 거부하므로 게이트가 선차단한다
    # (reject_reason "invalid_params:identical_slugs" — §6.5 회귀 #6)

class ConflictsOfParams(BaseModel):
    slug: str

class ProvenanceChainParams(BaseModel):
    slug: str

@dataclass(frozen=True)
class KgTemplate:
    name: str
    params_model: type[BaseModel]
    cypher_for: Callable[[BaseModel], str]   # 사전 컴파일 변형 중 선택만 —
                                             # 파라미터 문자열 보간 절대 금지
    description: str

TEMPLATES: dict[str, KgTemplate] = {
    "neighbors": ..., "path_between": ..., "conflicts_of": ..., "provenance_chain": ...,
    # 후속 등록(현행 registry — orthus/kg/templates.py):
    #   "entity_mentions"(K6) · "path_between_company"(K7.2 owner two-framing 재료)
    #   "page_conflicts"(K8, PR #373) · "entity_neighbors"(K9.1, PR #496)
    #   "expand_node"/"expand_entity"(그래프 탐색기 E1, PR #569)
}
```

- Cypher 문자열은 kg-model §4 reference가 canonical. `depth`/`max_hops`는
  **사전 컴파일된 쿼리 문자열 선택**(`_NEIGHBORS_D1`/`_NEIGHBORS_D2`,
  `_PATH_H2/H3/H4`)으로 구현 — Cypher 가변 길이 상한은 파라미터 불가
  (kg-model §4).
- 값 파라미터(slug 등)는 전부 driver 파라미터 바인딩으로만 전달한다.
  f-string/`%`/`.format`으로 Cypher를 만드는 코드는 게이트 위반이다.
- Literal 타입이 depth/hop 상한 하드코딩의 구현이다 — Pydantic validation
  실패가 곧 reject(`invalid_params`).
- v1 템플릿의 시작 노드 MATCH는 **slug 기반**이다(kg-model §4 reference의
  `{slug:$slug}` 그대로). K7에서 owner 네임스페이스 도입과 함께 page_id 기반
  변형으로 교체한다(§10.2) — 사전 컴파일 문자열 교체이므로 게이트 공개
  표면은 불변이다. *(K7.2에서 구현 완료, PR #340 —
  `orthus/kg/visibility.py::resolve_slug` + owner-variant 사전 컴파일 문자열.)*

**결과 매핑 규약**: 템플릿별 custom 매퍼를 두지 않는다. 공통 매퍼 하나가
driver record의 `Node`/`Relationship`/`Path` 타입 값을 재귀 평탄화해
`KgGraphNode`/`KgGraphEdge`로 정규화한다(중복 노드는 id로 dedupe). 노드
`id` 추출 우선순위는 라벨 무관하게
`page_id → doc_id → row_id → entity_key → name → slug` 고정이다(§6.4 응답
모델의 id 정의와 동일) — 이 고정 덕에 K6 라벨 추가 시에도 매퍼 수정이
필요 없다. RETURN이 이 타입 외의 값(스칼라 집계 등)을 반환하는 템플릿은
v1에 없다 — 추가하려면 본 규약을 함께 개정한다.

매퍼는 `gate.py` 소속이며(§6.2 6단계), 다음 정규화를 함께 수행한다:

1. **temporal 변환** — driver가 반환하는 `neo4j.time.DateTime` 등 temporal
   값은 `to_native()`로 Python datetime으로 바꾼 뒤 ISO 8601 문자열로
   직렬화한다. Pydantic v2 기본 직렬화에 맡기지 않는다 — driver 고유 타입은
   Pydantic이 모르는 타입이라 매퍼의 명시 변환이 단일 책임 지점이다.
2. **TTL 필터** — `:StructuredFact` 노드 중 `valid_until`이 현재보다 과거인
   것은 결과에서 제외하고 incident 엣지도 함께 drop한다. kg-model §2 TTL
   계약("만료 후 query filter에서 제외")의 구현 지점이다 — 템플릿 Cypher가
   아니라 매퍼 단일 지점에서 처리해 전 템플릿·향후 템플릿에 일괄 적용된다.
3. **placeholder 포함** — placeholder 노드는 `materialized=false` +
   `id=slug`로 응답에 포함한다(숨기지 않는다 — dangling 링크 가시화는 wiki
   품질 신호다).
4. **방어적 skip** — id 후보 속성이 전무한 노드는 구조상 생기지 않지만,
   발견 시 해당 노드만 skip하고 audit meta에 카운트한다.

### 6.2 게이트 — `orthus/kg/gate.py`

```python
class KgQueryStatus(StrEnum):
    OK = "ok"; REJECTED = "rejected"; TIMEOUT = "timeout"; ERROR = "error"

# reject_reason 값 (kg_query_runs.reject_reason — 회귀 세트가 고정):
#   "kg_disabled" | "kg_unavailable" | "unknown_template"
#   | "invalid_params:<field>" | "slug_not_found:<slug>"
#   | "timeout" | "driver_error:<ExcType>"
#   후속 추가(현행 gate.py:70): "mapping_error:<ExcType>"/"internal_error"(K4 교정 ⑥)
#   | "deferred_template" | "owner_scope_required"(K7/K9 게이트 확장)

class KgTemplateResult(BaseModel):
    status: KgQueryStatus
    reject_reason: str | None = None
    nodes: list[KgGraphNode] = []
    edges: list[KgGraphEdge] = []
    truncated: bool = False
    duration_ms: int | None = None

def run_kg_template(*, user_id: UUID, template_name: str, params: dict,
                    correlation_id: UUID | None = None) -> KgTemplateResult
```

실행 순서 의사코드:

```text
with audit("kg.retrieve", correlation_id=...) as span:
  1. kg_enabled?            아니오 → REJECTED("kg_disabled")
  2. TEMPLATES lookup       miss   → REJECTED("unknown_template")
  3. params_model validate  실패   → REJECTED("invalid_params:<첫 필드>")
  4. slug 선검증: 템플릿의 slug 파라미터 **각각**을 PG
     wiki_pages(slug, scope='company')에서 lookup
     miss → REJECTED("slug_not_found:<slug>")   # 존재하지 않는 시작점의
     그래프 스캔 방지 + 404 판정 재료 (K7에서 네임스페이스 resolve로 확장 — §10.2)
     # lookup은 kind 불문 — 시작 노드 라벨(WikiPage/WikiClaim)은 Cypher MATCH가
     # 강제하므로 kind 불일치는 reject가 아니라 빈 결과로 수렴한다
  5. kg_available()?        아니오 → REJECTED("kg_unavailable")
  6. kg_read_session() + tx timeout = settings.kg_query_timeout_ms
     limit = min(내부 호출자 요청 limit 또는 기본, settings.kg_query_limit)
     # K4 API는 limit 파라미터를 노출하지 않는다(서버 고정) — limit 인자는
     # K4b/K5 등 내부 호출자용이며 상한은 항상 settings가 cap한다
     실행 → 결과 매핑(KgGraphNode/Edge), len==limit → truncated=true
     neo4j ClientError.TransactionTimedOut → TIMEOUT
     기타 driver 예외 → ERROR("driver_error:<cls>")
  7. kg_query_runs INSERT — 1~6 중 어느 분기로 끝났든 모든 결과가 이 기록을
     거친다(reject 포함). params는 redact_pii_text() 통과(JSONB),
     status/reject_reason/duration_ms/result_count/user_id/correlation_id
  8. status==OK면 별도 kg_write_session으로 best-effort:
     UNWIND $ids MATCH (n {…}) SET n.last_accessed_at = datetime()
     — 실패는 swallow + audit meta 카운트 (kg-model §2 각주 계약)
```

"write Cypher reject"의 구현적 의미: 이 게이트에는 **raw Cypher 입력 경로
자체가 없다.** 회귀 세트는 (a) 게이트 공개 표면에 Cypher 문자열 파라미터가
없음을 타입 수준에서 고정하고, (b) `templates.py`의 등록 Cypher 전수가
read-only 절(MATCH/RETURN/WHERE/UNWIND)만 포함함을 정적 검사하는 unit
테스트로 구성한다(금지 키워드 CREATE/MERGE/SET/DELETE/REMOVE/DROP/CALL을
**단어 경계로 매칭** — `shortestPath` 같은 함수명이 오탐되지 않는 이유).
last_accessed_at SET은 이 정적 검사 대상인 템플릿 registry가 아니라
`gate.py`의 분리된 write 경로(8단계)에서 수행되므로 검사 대상이 아니며,
별도 테스트로 best-effort 시맨틱을 고정한다.

### 6.3 `kg_query_runs` migration (가칭 `0048_kg_query_runs`)

kg-model §4 표가 canonical. DDL 골격:

```sql
CREATE TABLE kg_query_runs (
  run_id          UUID PRIMARY KEY,
  template_name   TEXT NOT NULL,
  params_redacted JSONB NOT NULL DEFAULT '{}'::jsonb,
  status          TEXT NOT NULL CHECK (status IN ('ok','rejected','timeout','error')),
  reject_reason   TEXT NULL,
  duration_ms     INT NULL,
  result_count    INT NULL,
  user_id         UUID NULL,
  correlation_id  UUID NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_kg_query_runs_created  ON kg_query_runs (created_at);
CREATE INDEX idx_kg_query_runs_template ON kg_query_runs (template_name);
```

`clean` wipe 목록에 `kg_query_runs` 추가. 기존 structured `query_runs`는
건드리지 않는다(kg-model §4 결정 그대로).

### 6.4 API — `GET /wiki/pages/{slug:path}/graph`

`orthus/api/routes/wiki.py`에 추가. P4.3/P4.5 page-단위 GET 패턴 동형:

```python
class KgGraphNode(BaseModel):
    id: str                      # page_id|doc_id|row_id|entity_key|name 우선순위,
                                 # placeholder는 slug — §6.1 결과 매핑 규약
    label: str                   # "WikiPage" | "WikiClaim" | ...
    slug: str | None = None
    title: str | None = None
    materialized: bool = True

class KgGraphEdge(BaseModel):
    src: str; dst: str; rel: str
    properties: dict[str, Any] = {}

class WikiPageGraphResponse(BaseModel):
    slug: str
    supported: bool
    reason: str | None = None    # "kg_disabled" | "kg_unavailable"
    truncated: bool = False
    nodes: list[KgGraphNode] = []
    edges: list[KgGraphEdge] = []

@router.get("/pages/{slug:path}/graph", response_model=WikiPageGraphResponse)
def get_wiki_page_graph(
    slug: str,
    depth: int = Query(1, ge=1, le=2),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> WikiPageGraphResponse
```

> **후속 확장(additive — 현행 정의는 `orthus/schemas/canonical.py`가 SoR):**
> K7.2(PR #340)가 `KgGraphNode.scope`/`is_own_personal`과 `KgGraphEdge.scope`를
> 추가했고(`owner_id`는 wire 미노출), K9.2(PR #496)가 `:Entity` 노드용
> `mention_count`/`entity_kind`를 추가했다. 위 스케치는 K4 시점 필드다.

핸들러 분기(kg-model §4 응답 계약의 구현 순서):

1. `kg_enabled` false(personal node 포함) → 200 `supported:false,
   reason="kg_disabled"` — 404보다 먼저 평가한다(P4 federated unsupported
   패턴 동형, kg-model §4).
2. company wiki page resolve 실패(`wiki_pages(slug, scope='company')` miss)
   → 404 (`GET /wiki/pages/{slug:path}` `:227`의 404 메시지 포맷 재사용).
3. `run_kg_template(template_name="neighbors", params={slug, depth})` →
   OK → nodes/edges/truncated 매핑. `kg_unavailable`/`timeout`/`error` →
   200 `supported:false, reason="kg_unavailable"` (fail-open). 응답 `reason`은
   kg-model §4 계약상 2종(`kg_disabled`/`kg_unavailable`)뿐이므로
   timeout/error도 `kg_unavailable`로 수렴시키며, 세부 사유는
   `kg_query_runs.status`에 남는다.
4. 응답에 본문 없음 — slug/title/메타만(불변식 5 — 본문은 기존 page GET).

인증 dependency 선택 근거: 그래프 메타는 wiki page 본문과 같은 민감도
(slug/title)이므로 page GET과 동일한 `get_session_user_or_knowledge_token`을
쓴다. operator 가드(`require_node_operator`)는 걸지 않는다 — agent-work
패널류(operator 콘솔)와 달리 지식 표면이다. 대안 검토 §11-5.

### 6.5 K4 reject 회귀 세트 (structured 5-reject 동형)

| # | 시나리오 | 기대 |
|---|---|---|
| 1 | 미등록 템플릿 이름 실행 | `rejected` + `unknown_template` + kg_query_runs 기록 |
| 2 | depth=3 / max_hops=5 / 음수 limit | `rejected` + `invalid_params:*` (Pydantic Literal/ge·le) |
| 3 | 존재하지 않는 slug | `rejected` + `slug_not_found:*` |
| 4 | timeout (test 그래프에 깊은 체인 seed + timeout 1ms 설정) | `timeout` 기록, API는 unsupported fail-open |
| 5 | 등록 Cypher 정적 검사 — write 키워드 0건 + 게이트 공개 표면에 raw Cypher 입력 부재 | unit 테스트로 고정 |
| 6 | `path_between`에 slug_a == slug_b | `rejected` + `invalid_params:identical_slugs` — Neo4j shortestPath 동일 끝점 에러 선차단 |
| 7 | datetime 속성 포함 응답 직렬화 (`test_temporal_props_serialized`) | temporal 값이 ISO 8601 문자열로 반환, driver 타입 누출 없음 |
| 8 | 만료 `valid_until` StructuredFact (`test_expired_fact_excluded`) | 노드·incident 엣지가 응답에서 제외 |
| 9 | placeholder 경유 결과 (`test_placeholder_in_response`) | `materialized=false` + `id=slug`로 포함 |

추가 회귀: `test_graph_endpoint_unsupported_when_disabled`(flag off → 200
unsupported) · `test_graph_endpoint_404_unknown_slug` ·
`test_graph_endpoint_no_body_in_response` ·
`test_last_accessed_at_best_effort`(write 실패 강제 → 질의 결과 정상) ·
`test_kg_query_runs_params_redacted`(PII 포함 slug 파라미터 → redaction 통과
저장).

### 6.6 K4 완료 게이트 (K4b·K5·**K7** 진입 조건)

- [x] §6.5 reject 회귀 세트 green (수용 기준 4) — **K7 착수의 명시 전제**
      (kg-model §5 "K4 게이트/회귀 인프라 검증 후")
- [x] 정상 인프라 경로의 모든 실행이 `kg_query_runs` + `audit("kg.retrieve")`에
      남음 (붙은 correlation_id로 audit↔run 조인 가능 —
      `test_kg_query_runs_params_redacted_and_correlated`). **단 기록은
      best-effort다**(fail-open 우선, 아래 ⑥) — PG 장애로 `_record_run`/audit
      write가 실패하면 그 실행은 row가 누락될 수 있고, 결과 반환은 막지 않는다.
- [x] 라우터(`orthus/router/`) diff 0줄 — K4는 라우터를 건드리지 않는다

**K4 측정 기록 (2026-06-12, K2 실측 비율 합성 그래프 — page 1,600/4 :Project
허브/노드 7,504/엣지 13,898):** depth=2 neighbors에서 IN_PROJECT 포함 시
LIMIT 50 row가 **100%(50/50) :Project 허브 경유 형제 page**로 채워져 지식
관계가 전부 밀려났고(고유 노드 구성: WikiPage 50 + Project 1), 제외 시
WikiPage/WikiSource/WikiClaim/Document 혼합 구성으로 정상화됐다. latency는
양쪽 동등(중앙값 41–50ms; 게이트 timeout 2s 대비 충분한 여유) → **neighbors
기본 탐색에서 IN_PROJECT 제외 확정**(kg-model §4 reference 동시 개정.
project 귀속은 노드 props로 노출 유지, 재포함은 별도 결정).

**K4 구현 교정/기록 5건 (2026-06-12, 계약 의미 불변):**
① migration 번호는 `0048_kg_query_runs` 확정 — `0047`은 merge된
`collector_liveness`(#270)가 선점했고, K3 outbox는 `0047_kg_outbox`로 0046에서
별도 분기, `fadb806_merge_kg_branches`가 두 갈래(0047_kg_outbox·0048)를 합치는
alembic merge migration이다(K3·K4 통합 브랜치).
② neighbors도 `RETURN path`로 반환(§6.1 매핑 전제) — 가변 길이 패턴의 중간
hop 노드는 변수 미바인딩이라 driver가 속성 없는 stub으로 hydrate하며, Bolt
Path 구조만 전체 노드를 속성 포함 전달한다. provenance_chain은 관계
변수(sr/er)를 RETURN에 추가했다(edges 구성용).
③ `truncated`는 Cypher row(경로) 기준이다 — 응답 노드 수는 dedupe 후라 row
수 이하일 수 있다(kg-model §4 동시 명확화).
④ timeout 회귀의 결정성: Neo4j transaction timeout은 reaper 주기(기본
`db.transaction.monitor.check.interval=2s`)로만 강제돼 2초 미만 쿼리는 1ms
timeout이 안 잡힌다 — **neo4j-test 컨테이너만** interval을 `10ms`로 설정
(compose 변경, 운영 컨테이너 불변). driver 예외 정규화는
`client.is_timeout_error` 단일 지점이고 fake-예외 백스톱 회귀
(`test_gate_timeout_mapping_deterministic`)를 함께 둔다.
⑤ v1 템플릿 토폴로지에서 `:StructuredFact`는 doc 1-hop leaf라 WikiPage 시작
depth≤2 탐색에 닿지 않는다 — TTL 필터(§6.1 규약 2)는 게이트 매퍼 단일
지점을 driver 실데이터 record로 직접 고정한다(`test_expired_fact_excluded`).
⑥ **fail-open 견고화(코드 리뷰 후속):** `run_kg_template`은 **예외를 던지지
않는다**. driver 예외(`driver_error:<ExcType>`/`timeout`)와 결과 매핑 예외
(`mapping_error:<ExcType>` — driver와 별도 reason으로 진단성 보존)를 각각 정규화
하고, run 기록(`_record_run`)과 last_accessed는 best-effort이며, audit 인프라(PG
enter/exit write) 예외까지 최상위 try가 잡아 `ERROR`(reason `internal_error`)로
떨어뜨린다 — endpoint가 raw 호출만 해도 fail-open 200 `supported:false`가
구조적으로 보장된다. **fail-open 범위는 KG/Neo4j 가용성 + 그래프 처리이고, PG(SoR)
장애는 대상이 아니다** — endpoint의 404 존재 확인(`_company_wiki_page_exists`)과
게이트 slug 선검증은 PG에 의존하므로 PG 다운은 다른 route처럼 5xx로 드러난다(의도).
회귀: `test_mapping_error_normalized_to_error_status`(mapping_error) /
`test_audit_infra_failure_fails_open`(실제 `_write` mock으로 enter/exit 실패 경로) /
`test_record_run_failure_preserves_result`(부분 장애 시 결과 보존) /
`test_endpoint_fail_open_on_mapping_error`. limit 인자(내부 호출자용)는 양수일
때만 요청값으로 쓰고 0/음수/None은 서버 기본으로 수렴시켜 음수 `LIMIT` 누출을
막는다(`test_limit_guard_zero_and_negative`가 bind된 effective limit을 직접 검증).
**계약 변화:** run 기록이 best-effort가 되어 "모든 실행 기록"은 정상 인프라
경로에서만 무조건이다(§6.6 체크 보강).
⑦ §6.2 7단계의 "params는 `redact_pii_text()` 통과" 표현은 dict 입력에 부정확
하다 — 코드는 같은 rule set의 dict 재귀판 `redact_pii(params)`를 쓰고(JSONB
컬럼용), `reject_reason`(문자열)만 `redact_pii_text`를 쓴다. endpoint는 입력
위생을 위해 `_require_wiki_slug`(clean) 단계를 거치며 malformed slug는 422다
(§6.4 sketch 미기재 — 파일 내 다른 wiki endpoint와 동형의 방어). **알려진
trade-off:** `redact_pii`의 RRN/카드/전화 정규식이 digit-heavy slug(예
`invoice-1234567890123`)를 `kg_query_runs.params_redacted`에서 마스킹할 수 있어
감사 row가 실행 slug와 정확히 일치하지 않을 수 있다. 그래도 **redaction을
유지한다** — 절대 규칙 "PII redaction 우회 금지" + structured `query_runs`와 동일
패턴이 보안 우선이고, 실행은 redacted 값이 아니라 검증된 raw slug로 바인딩되므로
조회 정확성에는 영향이 없다(감사 forensic lookup만 일부 손실).
부수: 병렬 worktree 로컬 개발용으로 conftest가 `ORTHUS_KG_TEST_URI` env
override를 허용한다(PG `TEST_DB` 분리와 동일 이유 — 두 스위트가 같은 :7688
그래프를 동시에 wipe하면 서로를 깨뜨린다. CI/단일 세션은 기본값 불변).
`KgGraphNode`/`KgGraphEdge`/`WikiPageGraphResponse`는 §6.4 스케치의 route
파일이 아니라 `orthus/schemas/canonical.py`에 둔다(원칙 2 — §6.2
`KgTemplateResult`가 같은 모델을 참조하므로 단일 정의 지점).

---

## 7. K4b — `/ask` graph 분기

### 7.1 라우팅 확장

```python
# orthus/router/route.py
Route = Literal["structured", "wiki", "graph"]
```

`classify()` 확장 계약 — **rule 우선, LLM 보조, 기본 wiki**(현행과 동일한
보수성. LLM confidence-only routing 금지 — AGENTS 절대 규칙):

1. rule 단계: 관계형 패턴(예: "무슨 관계", "어떻게 연결", "충돌", "근거가
   어디", "어디서 나온") 매칭 시 graph **후보**로 표시. 구체 키워드 목록은
   K4b PR에서 `_GRAPH_TERMS` 상수로 명문화한다(기존 `_STRUCTURED_TERMS`
   `route.py:32` 패턴).
2. LLM 단계: 기존 json_only 분류 프롬프트의 route enum에 `graph` 추가.
3. **graph 확정 조건(전부 충족해야)**: rule 또는 LLM이 graph + 파라미터
   바인딩 성공(§7.2) + `kg_available()` + 요청 `scope`가 `company`/`all`
   (v1 그래프는 company-only이므로 `personal` 단독 scope는 graph 후보에서
   제외 — K7 owner-scope 개방과 함께 재검토). 하나라도 실패 → `"wiki"`
   fallback(현행 fail-safe `route.py:74`와 동형).

### 7.2 파라미터 바인딩 (결정론)

```text
# 모듈 홈: orthus/router/graph.py (K4b 신규 — route.py 비대화 방지)
bind_graph_params(question, context_wiki_slug=None) -> (template_name, params) | None:
  1. LLM 추출(압축/추출만 — 원칙 1): {"intent": "relation|conflict|provenance",
     "subjects": ["<제목/명사구>", ...]} json_only
     # 후속: K9.3(PR #496)이 intent enum에 "entity"(단일 개체 중심 질문 →
     # entity_mentions star)를 추가 — 현행 orthus/router/graph.py:66
     # 출력 파싱은 allowlist — 위 2필드 외 키 무시, intent enum 외 값이면
     # None, subjects는 최대 3개로 절단
  2. 코드 resolve: 각 subject를 wiki_pages(scope='company')에서
     slug 정확일치 → title 정확일치 → title ILIKE 전방일치 순으로 1건 resolve.
     후보가 0건이거나 복수 후보가 1건으로 가려지지 않으면 → None (graph 포기)
  3. intent→템플릿: relation+2subjects → path_between
                   conflict+1 → conflicts_of / provenance+1 → provenance_chain
                   relation+1 → neighbors(depth=2)
```

`context_wiki_slug`가 함께 온 요청(위키 페이지에서 시작한 질문 — P4.2 prefill)
은 그 slug를 subject 후보 목록의 선두에 추가한다 — "이 페이지와 B는 무슨
관계?"처럼 주어가 생략된 질문의 resolve 성공률을 올리는 결정론 보강이다.

LLM은 템플릿 이름도 Cypher도 만들지 않는다 — intent enum과 명사구 추출만.
템플릿 선택·resolve는 전부 코드다(kg-model §4 2항의 역할 분담을 한 단계 더
보수적으로).

게이트 단계 fallback: bind 성공 후에도 `run_kg_template`이 reject할 수 있다
(예: resolve와 실행 사이의 row 삭제 race). 이 경우도 §7.1과 동일하게 wiki
분기로 fallback하고 `RoutedAnswer.warnings`에 reject_reason을 남긴다 —
graph 분기에서 에러가 사용자에게 그대로 노출되는 경로는 없다.

> **v1 한계 (conflict/provenance intent — 2026-06-15):** conflict/provenance
> 템플릿은 `:WikiClaim`에서 출발하므로 그 subject는 claim 슬러그로만 resolve된다.
> claim 슬러그는 `write_claim`이 만드는 머신 슬러그 `{page_slug}-{8hexhash}`이고
> (`WikiClaim.title == slug`), 자연어 토픽/주장 표현은 이 머신 슬러그와 정확
> 일치하지도 title 일치하지도 않는다. 따라서 v1에서는 자연어 표현의
> conflict/provenance 질문이 claim에 바인딩되지 못하고 결정론적으로 wiki로 demote
> 한다(fail-open — 불변식 5 보존). **자연어 표현으로 실제 도달 가능한 intent는
> relation뿐이다.** e2e 테스트 `test_conflict_intent_resolves_claim` /
> `test_provenance_intent_resolves_claim`는 바인딩 경로 자체를 태우려고 의도적으로
> 슬러그 모양의 subject를 쓴다 — 자연어 표현이 동작한다는 보장이 아니다. 자연어
> claim resolution(page→claim 또는 K6 entity/claim-text 레이어 경유)은 K4b 범위
> 밖의 후속 작업이며 미구현이다.
>
> **후속 개정(2026-06-18/24):** "도달 가능 intent는 relation뿐"은 K4b v1 시점
> 서술이다. K8(PR #373)이 conflict intent에 **page-resolve fallback**을 추가해
> 자연어 conflict 질문이 `page_conflicts` 템플릿으로 도달하고, K9.3(PR #496)이
> `intent=entity`(단일 개체 중심 질문 → `entity_mentions` star)를 추가했다.
> 자연어 **claim** 단위 resolution은 여전히 미구현이다.

### 7.3 grounding 합성 + canonical 모델 추가

```python
# orthus/schemas/canonical.py 추가 (additive)
class KgGraphAnswer(BaseModel):
    template: str                    # neighbors | path_between | conflicts_of | provenance_chain
    intent: str                      # relation | conflict | provenance
    nodes: list[KgGraphNode] = []    # K4 GET .../graph와 동일 typed 모델 재사용
    edges: list[KgGraphEdge] = []    # 동일 — sparse properties 계약 보존
    path_slugs: list[str] = []       # 경로/이웃의 WikiPage·Claim slug 순서
    params_redacted: dict[str, Any] = {}
    truncated: bool = False

class RoutedAnswer(BaseModel):       # 기존 — additive 확장
    mode: str                        # 'structured' | 'wiki' | 'agent_work' | 'graph'
    graph: KgGraphAnswer | None = None    # 신규 필드 (mode='graph'일 때 wiki와 함께 채움)
    # wiki 필드가 graph 모드의 본문 답변을 그대로 담는다 (아래)
```

> **구현 결정 (2026-06-15, 본 PR — 초안 개정):** 초안의
> `edges: list[dict[str,str]]` 대신 K4가 이미 반환하고 FE(`web/src/lib/api.ts`)가
> 이미 import하는 **typed `KgGraphNode`/`KgGraphEdge`를 재사용**한다(canonical.py
> §6.4 정의 그대로). 이유: ① K5의 `/ask` 경로 렌더가 backend 추가 0으로
> `/wiki/{slug}/graph` 패널과 같은 컴포넌트를 쓴다 ② loose dict는 `KgGraphEdge`의
> sparse-property 계약을 잃는다. `intent`는 초안에 없었으나 FE가 답변 종류
> (관계/충돌/근거)를 라벨링하도록 추가했다. `mode='graph'`는 `wiki`(본문)와
> `graph`(경로 메타)를 **함께** 채우므로 `RoutedAnswer` docstring의
> "exactly one populated"를 graph 예외로 개정했다.

합성 의사코드(kg-model §4 "Grounding 합성" 구현 — `orthus/router/graph.py::try_graph_answer`):

```text
try_graph_answer(user_id, question, ...) -> GraphOutcome:
  # 구조 가드(LLM/Neo4j 전): node_kind==company + scope∈{company,all}
  #   kg_enabled False → demote, no warning / kg_available False → demote + "kg_unavailable"
  binding = bind_graph_params(...)                  # LLM 의도+명사구 추출만, 코드가 template/slug 결정
  binding None → demote to wiki (no warning)
  result = run_kg_template(...)                     # K4 게이트 그대로 — 우회 금지
  result not OK → demote to wiki + warnings(reject_reason)   # §2.6
  page_slugs = 경로 위 materialized :WikiPage/:WikiClaim slug
               (Claim는 wiki_pages row라 자기 chunk로 직접 grounding — "owning page"
                간접화 안 함. Document/StructuredFact/placeholder → grounding 대상 아님)
  page_slugs 비면 → demote to wiki (mode="graph"+빈 sources 금지)
  hits = retrieve(user_id, question, scope="company", page_slugs=set(page_slugs))  # 기존 가드 유지
  hits 비면 → demote to wiki
  wiki_answer = answer_from_hits(..., learn=False, record_gaps=False)   # 본문은 compiled page에서만
  return GraphOutcome(RoutedAnswer(mode="graph", wiki=wiki_answer, graph=KgGraphAnswer(...)))
  # 어떤 예외도 blanket except → GraphOutcome(answer=None)으로 fail-open

# router.answer: mode=="graph"이고 company node일 때만 호출. answer None → mode="wiki"
#   demote 후 기존 단일 wiki dispatch로 fall-through(federation 포함), fallback_warnings 합류.
```

답변 본문이 wiki 분기와 같은 코드(`answer_from_hits`)로 grounding되므로 불변식 5
(수용 기준 3 "sources가 전부 compiled wiki page provenance")가 구조적으로 보장된다.
`record_gaps=False`는 graph **성공** 경로에만 적용되고, 경로를 못 찾은 demote는 일반
`ask()`(record_gaps=True)로 떨어져 K6 `missing_link` 갭이 정상 동작한다(§9.3과 정합).

### 7.4 K4b 테스트 케이스

`test_route_graph_relation_question`(rule+LLM mock → mode=graph) ·
`test_route_fallback_when_bind_fails`(subject resolve 실패 → wiki) ·
`test_route_fallback_when_kg_down`(컨테이너 정지 → wiki + warning — 수용
기준 2 후반부) · `test_existing_routes_no_regression`(기존 wiki/structured
질문 세트가 K4b 전후 동일 분기 — 라우팅 통합 회귀) ·
`test_graph_answer_sources_are_wiki_pages`(sources 전부 WikiSourceRef,
mode=graph — 수용 기준 3) · FE 계약 영향 없음 확인(`/ask` 응답 additive).

### 7.5 K4b 완료 게이트

전부 충족 — K4b 구현 완료(PR #333, main 머지 2026-06-16):

- [x] §7.4 green — 특히 기존 분기 무회귀와 fail-open(기존 5 router 무회귀 +
      8 신규 회귀)
- [x] K4b PR 본문에 분기 판단 방식(rule+LLM 결합) 명문화(kg-model §5 K4b 행)

---

## 8. K5 — FE 가시화

### 8.1 구현 형태 (route 신설 없음)

- `/wiki/{slug}` 상세 페이지(기존 P4.3/P4.5 related 패널 스택)에
  `RelatedGraphPanel` 추가: `GET /api/wiki/pages/{slug}/graph?depth=1` 소비.
- **1차 구현은 그래프 캔버스가 아니라 관계 그룹 칩 리스트다**: rel별
  (BACKLINK/SUPPORTS/CONFLICTS_WITH/DERIVED_FROM) 그룹 헤더 + 대상
  slug/title 칩(클릭 → 해당 `/wiki/{slug}` 이동), cap 20 + "더 보기" 없음
  (truncated 표시만). 시각화 라이브러리(force-graph 등) 도입은 **별도
  결정** — 신규 FE 의존성 없이 착수한다(kg-model §5 "미니맵부터"의 보수적
  해석; canvas 미니맵은 후속 polish).
- depth 토글(1↔2)은 44px 버튼 1개. `supported:false`면 패널 자체를 P4
  unsupported-state 문구로 렌더(federated page 패턴 동형 — personal node와
  KG off에서 동일하게 보임).
- 패널은 자체 가시성 필터를 갖지 않는다 — 무엇이 보이는지는 전적으로 서버
  게이트가 결정한다. K7 owner-scope 개방 후에도 같은 API가 owner 술어를
  적용하므로 FE 변경이 없다(경계는 항상 서버 한 곳).
- K4b 머지 이후 `/ask` 답변 화면의 graph 모드는 **별도 FE 작업이 없다** —
  답변 본문과 `wiki_links` 칩이 기존 wiki 모드 렌더를 그대로 쓴다(§11-6의
  `wiki` 필드 재사용이 의도한 효과, kg-model §5 K5 행의 "`/ask` graph 답변
  `wiki_links` 동반" 충족). `graph` 필드 기반 경로 시각화는 K5 범위 밖의
  후속 polish다.

> **구현 개정(PR #335, main 머지 2026-06-16 / E-series 2026-07-03~05):** 실제
> K5는 칩 리스트 단독이 아니라 **결정론 radial hop-ring 캔버스
> (`web/src/components/wiki/graph-ring-panel.tsx`, 신규 FE 의존성 0) + rel 그룹
> 칩 리스트(`graph-chip-list.tsx`)의 graph/list 토글**로 착수했다(cap 20 ·
> read-only · phone `390x844` QA 계약은 본문 그대로). "그래프 캔버스 라이브러리
> 도입은 별도 결정"은 이후 그래프 탐색기 E-series로 확정됐다 — E1
> `GET /wiki/graph/expand` + (label,id) 선검증 게이트(PR #569), E2
> `graph-explorer-panel.tsx` d3-force 결정론 레이아웃 누적 탐색기(PR #584), E3
> 모바일/a11y QA(PR #586); 설계는 내부 문서(비공개). `/ask` graph
> 모드의 경로/star 시각화도 후속으로 붙었다(K7.4 path framings PR #360, K9.3b
> entity star PR #496 — ring 컴포넌트 재사용).

### 8.2 K5 QA/게이트 (AGENTS 공통 QA 체크리스트 준수)

전부 충족 — K5 구현 완료(PR #335, main 머지 2026-06-16):

- [x] company node 데스크톱 + phone `390x844`(stretch `360x780`) browser QA:
      칩 wrap, 가로 스크롤 없음, 44px 탭 타겟, long slug overflow
- [x] personal node에서 같은 page → unsupported 문구(콘솔 에러 0)
- [x] `pnpm lint` / `pnpm build` green, 신규 route 0개, compact-shell
      threshold `<760px` 무변경
- [x] 스크린샷 evidence PR 첨부

---

## 9. K6 — entity layer + hardening

### 9.1 entity의 SoR — 신규 PG 테이블 (저작 원칙 준수 장치)

`:Entity`를 "LLM 추출 결과를 그래프에 직접 쓰기"로 구현하면 KG가 제2 저작
경로가 된다(kg-model §2 엣지 저작 원칙 위반) + rebuild 시 LLM 재호출이
필요해 rebuildable 계약이 깨진다. 따라서 **추출 결과를 먼저 PG SoR로
적재**하고 KG는 그것을 결정론 투영한다:

```sql
-- 0049_kg_entities (K6 PR1 migration; data-model.md §6 버전 규약 준수)
-- down_revision = 'fadb806ba302'  ← 현행 단일 alembic head(0047_kg_outbox+0048_kg_query_runs 머지).
--   0048이 아니다. 작성 전 `uv run alembic heads`로 단일 head 재확인.
CREATE TABLE kg_entities (
  entity_id    UUID PRIMARY KEY,
  entity_key   TEXT NOT NULL UNIQUE,        -- "{entity_kind}:{name_norm}"
  entity_kind  TEXT NOT NULL CHECK (entity_kind IN ('person','org','project','system')),
  name_norm    TEXT NOT NULL,               -- 정규화 규칙 §9.2
  display_name TEXT NOT NULL,               -- persist 전 redact_pii 통과(person carve-out, kg-model §1/operations §8)
  scope        TEXT NOT NULL DEFAULT 'company',
  owner_id     UUID NULL,                   -- K7 대비 — K6에서는 항상 NULL
  first_seen   TIMESTAMPTZ NOT NULL,
  last_seen    TIMESTAMPTZ NOT NULL,
  schema_version INT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_kg_entities_name_norm ON kg_entities (name_norm);  -- entity_conflict 교차-kind 검사(§9.2)
CREATE TABLE kg_entity_mentions (
  mention_id   UUID PRIMARY KEY,
  entity_id    UUID NOT NULL REFERENCES kg_entities(entity_id),
  page_id      UUID NOT NULL,               -- wiki_pages(kind in page|claim), company scope page만
  evidence_slug TEXT NOT NULL,              -- RELATES_TO evidence 재료 — company page slug만
  schema_version INT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (entity_id, page_id)
);
```

`tests/conftest.py`의 `_DATA_TABLES` wipe 목록에 **FK 순서로** 추가한다
(`kg_entity_mentions` → `kg_entities`; 자식 먼저). entity 테스트는 `clean`(PG)
+ `kg_clean`(graph) 두 fixture를 함께 요청한다.

이 테이블 신설은 data-model.md 갱신을 동반한다(K6 PR — kg-model §5.1 패턴).
KG 쪽은 `:Entity` MERGE 키 `entity_key`(kg-model §2 합성 키 예고 그대로),
`MENTIONED_IN`/`RELATES_TO`를 mentions에서 투영 — K2 파이프라인에 라벨
2종/관계 2종을 추가하는 일이고 새 메커니즘이 아니다.

그래프 반영 경로: §4.2 SELECT 계약에 `kg_entities`/`kg_entity_mentions`를
추가하되 **full rebuild 경로(`load_all`)에서만** 로드한다 — `_load`의 `since`
가드로 `kg-sync`(증분)는 entity를 건드리지 않는다(co-mention RELATES_TO가 page
전체 mention을 봐야 결정론적이라 부분 투영을 피한다, owner 확정 2026-06-13).
**outbox `entity_kind`도 확장하지 않는다** — entity는 distill 주기에만 변하므로
주기 `kg-rebuild`가 권위로 수렴시키고, 60초 SLA(수용 기준 2)는 wiki consolidate
이벤트에만 적용되는 기준이다.
`entity_mentions` 템플릿(§6.1 registry의 K6 예약분)은 이 slice에서
`EntityMentionsParams(name_norm: str)` param 모델과 함께 등록한다.
`tests/conftest.py::clean` wipe 목록에 `kg_entities`/`kg_entity_mentions`를
추가한다(§5.1/§6.3과 동일 의무). 라벨/관계 추가는 additive라
KG_SCHEMA_VERSION bump 없이 들어간다(§4.7 bump 정책).

### 9.2 distill 확장 계약

**LLM 계약(추출만 — 원칙 1):** 기존 distill JSON을 확장한다(별도 호출 아님 —
owner 결정). `_JSON_SHAPE`/`_SYSTEM`에 `"entities": [{"kind":
"person|org|project|system", "name": "<원문 표기>"}]`를 추가하되 **`_SYSTEM`의
"distillation" 부분문자열을 보존**한다(MockChat matcher 폴백 방지). entity
배열은 `_MAX_ENTITIES` cap을 두고 maxTokens headroom을 확인한다(8 claim 예산을
굶기지 않도록 — 같은 JSON 객체·같은 토큰 천장). `entities` 키 결측/비-list/
per-item 비-dict·kind allowlist 밖은 방어 파싱으로 drop(claims 방어 파싱 동형).

**반환·persist 분리(serial):** `distill_document`는 entity를 **추출만 반환**한다
(`DistillResult(source, claims, entities)` dataclass — 2-tuple→3-tuple positional
사고 방지). persist는 **serial write 단계**(`connectors/base.py`·
`collector/compile.py`의 ThreadPoolExecutor distill은 병렬이고 writes는 serial)
에서 `orthus/kg/entities.py::persist_distilled_entities`(`persist_entities` 래퍼)가
수행한다 — 병렬 distill 안에서 PG를 쓰면 교차-row 충돌검사 TOCTOU race. **unpack
지점(전수)**: `author.py`(단일·rebuild 경로), `connectors/base.py`,
`collector/compile.py`(personal scope → no-op), MockChat `_default_distill_json`
+ `_distill` 테스트 헬퍼.

**페이지 바인딩(owner 결정 2026-06-13 — 모든 관련 페이지):** distill은 페이지
연결 없는 문서 수준 entity 목록만 주므로, `persist_distilled_entities`는 문서가
기여한 **모든** company concept page(`source.related_pages` 중 `scope='company'`
+ `kind in (page|claim)`로 resolve된 것 전부)에 같은 entity 집합을 mention으로
붙인다. 같은 page에 함께 묶인 entity들에서 projection이 `RELATES_TO{co_mention}`을
유도하고, 여러 page에 걸친 같은 쌍은 `(src,rel,dst)` dedup으로 1 엣지에 수렴한다
(evidence_slug는 정렬 결정론 1개). "대표 1페이지" 대안은 entity→page 신호 손실로
미채택. `scope != 'company'`(personal compile)이면 page 조회 없이 no-op(owner-scope
entity는 K7).

**fail-closed(`ORTHUS_KG_ENABLED`):** `persist_distilled_entities`는 live
distill→consolidate 경로의 wiring 지점이라 K3 outbox enqueue와 **같은 기준**으로
게이트한다 — KG off면 PG `kg_entities`/`kg_entity_mentions`(person 이름 PII 포함)
적재 자체를 skip한다(off 기간 변경 미적재; flag on 시 full `kg-rebuild` 1회가
distill 재실행으로 entity를 채운다 — full-rebuild-only 계약과 정합). 저수준
`persist_entities`는 flag-무관(SoR 원시 연산 — PR1 단위 테스트가 KG-off로 직접
호출)이며, 게이트는 wiring 래퍼에만 둔다.

**코드 정규화·dedup:** `name_norm = NFKC → casefold → 공백 collapse → 직함/조사
suffix strip`(규칙 상수 + unit 테스트). `entity_key` 충돌(같은 key, 다른
display_name)은 silent merge + `display_name` 최신 갱신. **upsert는 내용
불변 시 no-op**(display_name/kind 미변경이면 `updated_at` 미갱신 — 그렇지 않으면
watermark sync가 영원히 quiesce 안 함; `_upsert_page_row` content-hash 게이트
동형).

**entity 충돌 → `WikiTask(kind="entity_conflict")` (결정①):** **같은 `name_norm`,
다른 `entity_kind`**(person:김대표 vs org:김대표)는 key가 달라 두 노드로 갈리는
정체성 분류 불일치다. silent split 금지 → persist 시 `name_norm` 교차-kind 검사
(idx_kg_entities_name_norm)로 감지해 `WikiTask(kind="entity_conflict",
related=[evidence page slugs])`를 emit한다(두 entity row는 적재, LLM 0회). **반드시
`canonical.py`의 `WikiTask.kind` Literal에 `"entity_conflict"`를 추가**한다(현재
닫힌 Literal `open_question|conflict|stale_audit|dedup|provenance_fix` — 추가
전엔 인스턴스화 ValidationError + `load_task` 루프 전부 오염). `entity_conflict`는
`kind="conflict"`와 **반드시 달라야** 한다(같으면 §4.3 `build_conflict_index`가
entity task의 `related` 쌍을 CONFLICTS_WITH 엣지 속성으로 흡수 — 검토 BLOCKER).
`entity_conflict`는 cleanup-kind가 아니므로(WikiTask source family
`central_wiki_task_cleanup` + `cleanup_only=False`) policy gate에서 **`draft_for_review`
(owner review, no-auto-execute)** 로 수렴한다 — 구현 정합(2026-06-14, K6 리뷰): 초안 spec은
`request_more_data`를 적었으나 실제 gate(`agentwork/state.py` central_wiki_task_cleanup 기본
분기)는 company wiki knowledge change로 보아 owner 검토 draft를 요구한다. 둘 다 핵심 의도
(자동 실행 금지 + 검토 필수)를 충족하며, conflict는 "데이터 부족"이 아니라 "동일 실체 여부
판단"이라 owner review가 더 적합하다. 의도 outcome(`draft_for_review`, not auto_execute)을
회귀로 고정한다(`apply_policy(entity_conflict)` 단언).

**redaction(결정④):** persist 전 entity `name`/`display_name`에 `redact_pii`를
적용한다(이름에 섞인 이메일/전화만 차단; 사람 이름 자체는 company knowledge로
유지). carve-out은 `entity_kind=person`에만 — kg-model §1 / operations §8.

**저신뢰 추출 skip(B4 — 계산 가능하게 재정의):** §9.2 초안의 "본문 내 2회 미만
언급 skip"은 `{kind,name}` 계약만으론 도출 불가(LLM은 카운트를 안 줌,
`persist_entities`엔 본문이 없음). → **필터를 distill 안에서** 수행한다(거기엔
markdown이 있음): kind allowlist + name-length floor + normalized 본문에서 entity
name 등장 횟수(정규화 substring) < 2면 drop. 규칙 상수는 PR2에서 고정 + unit
테스트. persist는 이미-필터된 entities를 받는다.

**RELATES_TO:** 같은 **company** page에 co-mention된 entity 쌍에서만 결정론 유도
(`kind="co_mention"`, evidence_slug = 그 company page slug). 무방향 중복은
entity_key 정렬 후 1방향만 emit, page당 폭증은 정렬 prefix cap. mention/co-mention/
evidence_slug 모두 **company scope page로 join 필터**(mixed-scope 누출 차단).

**claim-regression evidence(PR2 게이트):** 프롬프트 변경은 claim 추출에 영향을
줄 수 있다. "1회 비교"가 아니라 **N≥30 held-out docs(한/영·단/장문·
claude_sessions/gws 장문 포함) before/after claim-set diff + hard threshold**로
회귀를 고정하고, LLM `name` 출력에 이메일/전화 혼입 여부도 샘플 검사한다.

### 9.3 missing_link gap 감지 (결정론, LLM 0회)

**감지 위치(K6 확정 — 동기-가드, 초안 §9.3에서 개정).** 초안은 트리거 앵커를
`gap.py:131`로 적었으나, source_hits가 손에 있는 곳은 `qa.py`의 `record_gap`
호출 지점(`source_hits=hits` 전달)이다 → **트리거를 거기로 relocate**한다.
배치(주기 재평가) 대안은 top-2 hit slug를 재구성하려면 data_gaps 컬럼 추가
(migration) 또는 재-retrieval이 필요해 채택하지 않았다.

```text
트리거: insufficient_grounding 답변 직후, record_gap 호출 지점(orthus/wiki/qa.py:105)
선조건(가드): ⓐ reason == insufficient_grounding
            ⓑ retrieval hit가 ≥2개의 materialized company wiki page로 resolve
            ⓒ kg_available()  ← 음성 결과는 짧은 TTL 캐시(KG-down 시 매 약답이
              driver connect timeout을 물지 않도록)
판정:  상위 2개 page에 run_kg_template("path_between", max_hops=4)
       → 경로 없음 → record_gap(reason="missing_link", ...)  # dedup upsert(새 row 아님)
       # "상위 2개·hop 4"는 v1 — 확대는 측정 후 별도 결정. company scope 한정.
       # path_between은 지식 rel allowlist만 타고 IN_PROJECT 허브를 제외한다(§6.1
       # neighbors와 공유). 안 그러면 같은 프로젝트의 임의 두 page가 a→:Project→b
       # 2-hop으로 항상 "연결"돼 missing_link가 같은-프로젝트 쌍에서 발화하지 못한다.
스키마: GapReason Literal에 "missing_link" 추가 (canonical.py:122) +
       gap.py `_build_message` dict에 "missing_link" 4번째 메시지 추가
       (현재 3키뿐 — 누락 시 KeyError로 답변 경로가 깨진다). data_gaps.reason은
       CHECK 없는 TEXT라 migration 불필요(kg-model §5.2).
fail-open: KG 불가/실패 시 skip — 기존 reason으로만 적재, 답변 차단 없음(swallow).
배치 전환 trigger(미래): /ask p95 latency 기여분 또는 qualifying-call rate가
       임계를 넘으면 data_gaps에 hit slug 컬럼 추가 + 주기 배치로 전환.
```

> **후속 개정(K7.4, PR #360, 2026-06-17):** "company scope 한정"은 K6 v1 시점
> 계약이다. K7.4가 owner-inclusive로 확장했다 — `kg_owner_scope_enabled` ON이면
> `maybe_missing_link`(`orthus/wiki/gap.py`)가 caller 본인 personal hit도 anchor
> 후보로 받고(`_top_company_page_slugs(owner_scope=True)`) owner-variant
> `path_between`을 탄다. `user_id`는 세션에서만 유도(hit에서 재유도 금지 —
> confused-deputy 차단). flag OFF면 종전 company-only 동작 byte-identity.

### 9.4 erasure 전파 + 모니터링 + rebuild drill

구현(2026-06-14): erasure는 `orthus/kg/erase.py`, 모니터링은 `orthus/kg/monitor.py`,
둘 다 라이브러리 함수 + `python -m` CLI + `make node-kg-{erase,monitor}` target이다
(self-service UI 없음 — operator 수동 절차, operations.md §8.4).

- **erasure(`erase_kg_for_pages(page_ids)` + `make node-kg-erase`)** — 실행 모델은
  **하이브리드**(owner 결정 2026-06-14): ① 지워진 page의 `kg_entity_mentions`
  삭제 → ② 이번 erasure로 surviving company mention이 0이 된 `kg_entities` row만
  선별 삭제(무관 row 비건드림) → ③ **wiki page SoR 삭제**(`wiki_store.delete_item` —
  `wiki_pages`/`wiki_links`/`wiki_chunks`/`embeddings` + markdown). 잔존 row가 다음
  rebuild에 WikiPage 노드를 재생성(resurrection)하는 것을 차단한다(owner 결정 A,
  2026-06-14 — erase가 KG-only면 SoR row가 남아 rebuild가 노드를 되살린다) →
  ④ 지워진 page는 `op='delete'` outbox 이벤트로 worker가 WikiPage 노드 drop
  (**K6이 `op='delete'` enqueue 최초 호출처** — K3엔 호출처 없음; rebuild도 SoR 부재로
  prune) → ⑤ orphan `:Entity` 노드는 **즉시** Neo4j detach-delete
  (`store.delete_entity_nodes` — outbox `entity_kind`가 entity 미지원이라 우회;
  가장 민감한 PII인 사람 이름은 지연 없이 제거). PG SoR 삭제(①②③)는 KG 가용 여부와
  무관하게 수행된다(privacy 보증의 권위; wiki는 KG 독립). company-node 전용
  (`require_company_node`). 남은 source-layer row(`documents`/`corpus_chunks`/
  `notion_rows`/`connector_*`)는 operator가 지우고 `make node-kg-rebuild`로 최종 수렴
  (수용 기준 6).
- **§8.4 PG 테이블 목록에 `kg_entities`/`kg_entity_mentions` 포함**(identity-bearing).
- **person-entity orphan(검토 BLOCKER):** `:Entity`는 이름(`entity_key`) 키라 다른
  page가 같은 이름을 mention하면 노드가 잔존한다 — 위 ②④가 처리하고,
  `test_erasure_detaches_graph_nodes`가 모든 mention page erase 후 **person `:Entity`
  노드 소멸**을 assert한다(page/doc detach만으론 불충분).
- **모니터링(`kg_monitor_summary()` + `make node-kg-monitor`)** — read-only CLI 요약
  (신규 public route 금지 결정, owner 2026-06-14): `kg_outbox` status별 건수 + 최고
  pending 적체 나이, 최근 7일(기본) `kg_query_runs` status 분포, `:KgMeta`
  last_sync/last_rebuild 나이, `:Entity`/placeholder 노드 수. PG 부분 항상, Neo4j
  부분은 가용 시(fail-open). `dead`>0이면 exit≠0.
- **rebuild drill**: "volume loss → `make node-kg-bootstrap` → `make node-kg-rebuild`
  → parity green → 재-rebuild prune 0" 절차를 `docs/operations.md` §2에 기록 +
  kg-test 1회 실측 evidence(2026-06-14: nodes=7/edges=5/entities=1 parity OK).

### 9.5 K6 테스트 케이스

`test_entity_norm_rules`(unit — 정규화 표) · `test_entity_dedupe_idempotent`
(같은 distill 2회 → mentions 무증가; no-op 재-distill 후 kg-sync가 0 changed
entity 선택) · `test_entity_conflict_creates_wiki_task`(같은 `name_norm` 다른
`entity_kind` → `WikiTask(kind="entity_conflict")`, 두 entity row 보존) ·
`test_entity_conflict_task_excluded_from_conflict_index`(§4.3 오염 방지) ·
`test_entity_conflict_task_does_not_poison_agentwork_enum`(Literal 추가 +
load_task 루프 무오염) · `test_prune_removes_stale_entity`(stale 엣지 prune +
live 엣지 2회 rebuild 생존 동시) · `test_person_entity_redaction`(name/display_name
redact_pii 통과, 섞인 이메일/전화 마스킹) · `test_missing_link_detected_on_disconnected_pages` ·
`test_missing_link_skipped_when_kg_down` ·
`test_erasure_detaches_graph_nodes`(person `:Entity` 노드 소멸, 수용 기준 6) ·
owner-scope 미투영 boundary 회귀(entity 2분리 — `persist_entities(scope="personal")`
PG no-op + personal compile의 Neo4j projection 부재; K6 kg_entities row는 항상
`owner_id IS NULL` assert).

---

## 10. K7 — owner-scope graph

전제: K4 게이트 reject 회귀 green(§6.6). 계약 전체는 kg-model §4 K7 절이
canonical — 여기는 구현 순서와 테스트 매트릭스만 보탠다.

### 10.1 projection/outbox 확장

- §4.2의 모든 SELECT에서 `scope='company'` 필터를
  `scope='company' OR (scope='personal' AND <owner col> IS NOT NULL)`로
  확장하되 **`kg_owner_scope_enabled` flag로 분기** — off면 v1 쿼리 그대로.
- owner 컬럼 매핑(kg-model §3 실측 표): `wiki_pages`/`structured_rows` →
  `owner_id`, `documents` → `user_id`(personal row). projection 출력 props에
  전 노드 `scope`·`owner_id`(company는 null) 추가.
- 엣지 `scope`/`owner_id`: 양끝 중 더 제한적인 쪽(personal 우선 — cross-scope
  엣지 규칙)으로 SET. 서로 다른 owner의 personal-personal 엣지는 구조상
  생기지 않는다 — dst slug resolve가 (company ∪ src page owner 본인 personal)
  네임스페이스 안에서만 일어나기 때문이다(§10.2와 동일 규칙의 projection판).
- enqueue(§5.2)의 company no-op 분기를 flag 조건으로 확장.
- flag on 활성화 절차(순서 고정, runbook은 §10.5): ① K7 코드 머지(flag는
  여전히 off, `KG_SCHEMA_VERSION=2` 포함 — 이 시점부터 sync는 version
  불일치로 자동 거부) → ② `make kg-bootstrap`(신규 인덱스 멱등) →
  ③ `make kg-rebuild`(scope props 소급 + placeholder 키 전환) →
  ④ `ORTHUS_KG_OWNER_SCOPE_ENABLED=true` → ⑤ API 재기동. **rebuild 전에
  flag를 켜지 않는다**(§4.7 메커니즘 재사용).

### 10.2 slug → page_id 서버측 resolve (네임스페이스)

K7부터 같은 slug의 company/personal page 공존 가능(PG 유니크
`(slug, scope, owner_id)`). 게이트 4단계(§6.2)의 slug 선검증을 **resolve**로
승격한다:

```text
resolve_slug(slug, caller_id) -> page_id | None:
  personal 우선: wiki_pages(slug, scope='personal', owner_id=caller) →
  company:      wiki_pages(slug, scope='company')
  → 템플릿 시작 노드 MATCH를 slug가 아니라 resolve된 page_id로 바꾼다
```

시작점을 page_id로 박으면 slug 모호성이 Cypher까지 내려가지 않는다.
`$caller`는 세션 `AuthenticatedUser.user_id`에서만 유도(클라이언트 입력
금지 — kg-model §4).

### 10.3 전 템플릿 owner 술어 (경로 가시성)

flag on일 때 모든 템플릿을 owner-variant 사전 컴파일 문자열로 교체:

```cypher
// neighbors owner-variant (depth=2 예시) — 패턴의 모든 노드·관계에 술어
MATCH path = (p:WikiPage {page_id:$page_id})-[r*1..2]-(n)
WHERE all(x IN nodes(path)
          WHERE x.scope = 'company' OR x.owner_id = $caller)
  AND all(e IN relationships(path)
          WHERE e.scope = 'company' OR e.owner_id = $caller)
RETURN p, r, n LIMIT $limit
```

- placeholder 노드(`scope` 부재)는 `x.scope IS NULL` 케이스가 생기지 않게
  projection이 placeholder에도 `scope='company'`를 SET한다(v1 placeholder는
  company 네임스페이스 전용 — kg-model §2). K7에서 personal dangling slug의
  placeholder는 slug 단독 MERGE가 company placeholder와 충돌하므로, MERGE
  키를 합성 네임스페이스 키(예: `ns_slug = "<owner_id|company>:" + slug`)로
  전환한다 — §10.1의 KG_SCHEMA_VERSION 2 bump + 필수 rebuild에 포함되는
  변경이다.
- 경로 가시성: 술어가 `all(...)`이라 중간 hop 비가시 시 **경로 전체가 결과
  에서 빠진다** — 부분 마스킹 없음(kg-model §4 규칙의 쿼리 수준 구현).
  `shortestPath`는 술어로 경로가 기각되면 차선 경로를 재탐색하지 않을 수
  있다 — "보이는 것 중 최단"이 아니라 "최단이 보이면 반환"이 K7 v1 계약
  이며, 이는 누출이 아니라 보수적 누락이므로 허용한다(문서화).
- flag off면 v1 쿼리 문자열 그대로 — **비-owner 세션 결과가 K7 전후 동일**
  회귀(kg-model §5 K7 Verify)의 구현 전제.

### 10.4 K7 테스트 매트릭스

| 축 | 값 |
|---|---|
| 템플릿 | neighbors / path_between / conflicts_of / provenance_chain (×entity_mentions) |
| 세션 | owner 본인 / 타 user / admin(타인) |
| flag | off / on |

고정할 성질:

1. **boundary**: flag on + 타 user/admin → personal 노드·엣지·경로 0건
   (전 템플릿 × 전 응답 필드 — 수용 기준 5 후반부).
2. **path-leak**: company A — personal X — company B 체인 seed →
   비-owner의 `path_between(A,B)`에 X 경유 경로 부재(경로 요약·count에도
   흔적 없음); owner에게는 노출.
3. **무변화 회귀**: flag on 상태 비-owner 세션의 전 템플릿 결과 ==
   flag off 결과 (snapshot 비교).
4. **erasure**: owner erasure → 해당 owner personal 노드/엣지 전체
   detach-delete (수용 기준 6 후반부).
5. **slug 충돌 resolve**: 동일 slug company/personal 공존 → owner 호출은
   personal 우선, 비-owner는 company.

구현 테스트 심볼(고정 — K2~K6의 명명 테스트 케이스 관례 동형):
`test_k7_boundary_matrix_all_templates_roles`(성질 1) ·
`test_k7_path_leak_dropped_not_masked`(성질 2) ·
`test_k7_flag_off_results_unchanged`(성질 3) ·
`test_k7_owner_erasure_detach_delete`(성질 4) ·
`test_k7_slug_namespace_personal_first`(성질 5).

### 10.5 K7 완료 게이트

전부 충족 — K7 구현 완료(K7.1–K7.5 main 머지, 2026-06-15~17):

- [x] §10.4 매트릭스 green — Neo4j Community에 RLS가 없으므로 이 테스트
      세트가 곧 경계 증명이다(kg-model §4). §10.4의 테스트 심볼 5종은 이름
      그대로 존재(`tests/integration/test_kg_boundary_matrix.py` ·
      `test_kg_owner_erasure.py` · `tests/unit/test_kg_flag_off_cypher_golden.py`)
- [x] flag 활성화 runbook(rebuild 필수 + version bump) `docs/operations.md`
      기록
- [x] `orthus-operator-reviewer` 검토(개인정보 경계 변경 — operator review
      reminder 대상)

**K7 구현 기록(2026-06-15~17, main 머지):** K7.1 projection/outbox owner-scope
확장(PR #309) — 경계 계약 단일 출처 `orthus/kg/visibility.py` 신설(ns 키
`ns_slug`/write·read 술어), migration `0057_kg_outbox_owner_scope`(kg_outbox에
`scope`/`owner_id` 컬럼 추가 — §5.1 DDL엔 없던 additive 컬럼; exact-scope worker
DELETE 재료), `KG_SCHEMA_VERSION=2` bump. K7.2 게이트 owner-inclusive
resolve(`visibility.py::resolve_slug`) + 전 템플릿 owner-variant(+
`path_between_company`) + B1–B6 boundary/path-leak matrix + 와이어
`scope`/`is_own_personal`(PR #340). K7.3 K5 패널 owner-scope +
`GET /wiki/kg/footprint`(PR #345). K7.4 /ask graph owner
two-framing(`run_path_framings` + `RoutedAnswer.path_framings`) + owner-inclusive
missing_link(PR #360). K7.5 owner erasure + ops runbook(PR #362).

---

## 11. 주요 설계 결정 — 대안 검토

| # | 결정 | 채택 | 대안과 기각 사유 |
|---|---|---|---|
| 1 | `neo4j` 의존성 위치 | 메인 `dependencies` | (a) `kg` extra: flag off 배포에서 가볍지만, central 단일 런타임(P8)에선 어차피 설치 대상이고 extra 미설치+flag on 조합의 운영 사고면이 생김. lazy import가 비용을 이미 제거 — extra의 이득이 작다 |
| 2 | outbox enqueue 지점 | `store.py::_persist` 단일 hook(+`publish_agent_draft_document`·`upsert_source_document` 2곳 — §5.2 실측 교정 반영) | (a) consolidate/저작 호출자마다 enqueue: 호출자 누락 위험(특히 task 쓰기), 같은 이벤트 중복. _persist는 모든 wiki 쓰기의 유일한 commit 지점이라 구조적으로 누락 불가 (b) PG 트리거: 앱 밖 마법 — 레포에 전례 없음, 마이그레이션·테스트 비용 |
| 3 | K3 worker 런타임 | FastAPI lifespan background thread + 수동 drain CLI | (a) launchd 별도 plist: 운영 표면 +1, 60초 SLA에 poll 간격이 묶임. fallback으로는 유효(CLI 재사용) (b) 요청 경로 동기 적용: Neo4j 장애가 쓰기 경로 지연으로 전파 — fail-open 위반 |
| 4 | watermark 저장 위치 | Neo4j `:KgMeta` (kg-model 기결정) | PG 테이블: volume 유실 시 watermark가 살아남아 "비어 있는데 sync가 변경 없음으로 종료"하는 모순 상태 가능. KgMeta는 그래프와 운명을 같이해 자연히 rebuild로 수렴 — 재확인만 |
| 5 | K4 endpoint 인증 | `get_session_user_or_knowledge_token` (page GET 동형) | `get_current_user`+`require_node_operator`: 그래프 메타를 operator 전용으로 좁히면 K5 일반 사용자 패널이 못 씀. 민감도가 page 본문 이하라 과보호 |
| 6 | K4b 응답 envelope | `RoutedAnswer.graph` 필드 신설 + 본문은 `wiki` 필드 재사용 | (a) structured envelope 재사용: 표 형태 결과가 아니라 부적합 (b) graph 전용 본문 합성: wiki qa와 별도 grounding 경로가 생겨 불변식 5 검증 표면이 늘어남 — wiki 재사용이 구조적 보장 |
| 7 | CI에서 Neo4j | **K2에서 채택 확정(2026-06-12 사용자 결정):** backend job이 `docker compose --profile test up -d neo4j-test`를 기동하고 `ORTHUS_KG_TEST_REQUIRED=1`로 kg fixture의 silent skip을 fail로 강제한다 — K2의 핵심 회귀(멱등/parity/boundary)가 CI 상시 실행 | 즉시 service container: CI 시간 증가와 flaky 면이 늘어남 → tmpfs + 고정 auth로 상태가 없고, K2 회귀의 검증력이 그 비용을 정당화한다고 판단. K4 reject 회귀도 같은 컨테이너를 쓴다 |
| 8 | K6 entity SoR | 신규 PG 테이블 `kg_entities`/`kg_entity_mentions` | (a) 그래프 직저장: rebuild 시 LLM 재호출 필요 — rebuildable 계약 파괴 (b) wiki_links 재사용: entity는 page가 아니라 rel 모델이 안 맞음 |
| 9 | depth/hop 가변성 | Literal 파라미터 + 사전 컴파일 쿼리 문자열 선택 | (a) APOC 동적 깊이: 플러그인 의존 금지(kg-model §7) (b) 문자열 보간: 게이트 원칙 위반 — 논외 |
| 10 | K5 1차 시각화 | rel 그룹 칩 리스트(신규 FE 의존성 0) — *구현 개정(§8.1): 결정론 ring 캔버스+칩 토글로 착수(PR #335), "별도 결정"은 이후 E-series d3-force 탐색기로 확정(PR #584, 내부 문서(비공개))* | 그래프 캔버스 라이브러리: 의존성·모바일 인터랙션·접근성 비용이 큼. 칩 리스트로 가치 검증 후 별도 결정 |

---

## 12. 리스크 / 엣지케이스 / 기술적 불확실성

kg-model §7의 리스크 표에 더해, 구현 수준에서 새로 식별된 항목:

| 항목 | 내용 | 완화 |
|---|---|---|
| neo4j Python driver와 FastAPI 스레딩 | driver는 thread-safe지만 session은 아니다 | session을 함수 스코프 밖으로 내보내지 않는 §2.3 래퍼 계약으로 봉인 |
| lifespan worker와 `ORTHUS_API_RELOAD=auto` | dev reload 시 worker thread 중복 기동 가능 | worker를 실제 serving 프로세스에서만 기동하도록 가드(uvicorn reload 모드의 감시용 부모 프로세스에서는 미기동) — K3 PR에서 검증. 최악의 중복 기동도 멱등 마커가 흡수 |
| `updated_at`와 commit 시각 차 | `DEFAULT now()`는 트랜잭션 시작 시각 — 긴 트랜잭션이면 watermark가 건너뛸 수 있음 | OVERLAP_60S(§4.7). 60초 초과 트랜잭션은 현 코드베이스에 없음 — 가정으로 명시 |
| Cypher 파라미터로 들어가는 slug에 PII | slug는 wiki 저장 시 redaction 통과한 값(kg-model §1)이지만 질의 파라미터는 사용자 입력일 수 있음 | `kg_query_runs.params_redacted`에 `redact_pii_text()` 통과 후 저장(§6.2 7단계) — 회귀 테스트 고정 |
| timeout 식별의 driver 버전 의존 | `TransactionTimedOut` 예외 클래스/코드가 driver 버전에 따라 다를 수 있음 | client.py에서 예외 정규화 단일 지점 + K4 timeout 회귀가 버전 업그레이드 시 깨지면 그 지점만 수정 |
| 그래프 fan-out 폭주(허브 노드) | `:Project` 허브나 인기 page의 depth=2 이웃이 수천 개 | LIMIT 50 주입 + truncated 표시(이미 계약). `IN_PROJECT`는 K4 측정(§6.6 기록 — 허브 경유 row 50/50 도배) 후 neighbors 기본 탐색에서 **제외 확정**, kg-model §4 동시 개정 완료 |
| placeholder 폭증 | 오타 dangling slug가 노드로 쌓임 | SoR(wiki_links)이 권위 — placeholder 수 자체가 wiki 품질 신호. rebuild prune이 SoR 정리를 따라 수렴. K6 모니터링에 placeholder count 노출 |
| K4b 분류 품질 | 관계형 질문 오탐 → 불필요한 graph 시도 | 확정 조건 AND 결합(§7.1) + 실패 시 wiki fallback이라 사용자 피해는 지연뿐. `kg_query_runs`로 오탐률 관측 가능 |
| 운영자 1인 부재 시 적체 | outbox dead/적체를 알아챌 사람이 1명 | K6 모니터링 최소선(§9.4) + 주기 rebuild가 최종 수렴 보장 — "방치해도 틀리지 않고 늦을 뿐" 상태를 설계 불변으로 유지 |
| 불확실성: 9천 page 초기 projection 시간 | **dev 측정(2026-06-17, WSL + neo4j-test :7688, mock embed)**: company-only 300-page cold full rebuild `last_rebuild_seconds≈5.56s`, company+synthetic-owner(300+300) 증분 MERGE `≈0.97s`. 선형 외삽 시 9천 page ≈ 167s(2.8분)로 default lock 30분의 0.5×(900s) headroom 안. **단 WSL/소형 synthetic seed라 prod Mac mini/실 corpus 비대표** — 활성화 시점 Mac mini 실측이 baseline(operations.md §2.1). owner-inclusive는 company-real+personal-synthetic. | K7.5: rebuild가 `:KgMeta.last_rebuild_seconds` 기록 + monitor 노출 + `>0.5×lock` headroom WARN(§6.3 chunked rebuild 신호). 활성화 baseline 실측 후 chunked rebuild 발동 여부 확정 |
| 불확실성: Community Edition 메모리 상한에서의 depth=2 latency | 미측정 | K4 게이트 기본값(2s/50)이 안전판 — 측정 후 kg-model §7과 함께 조정 |

---

## 13. 마일스톤별 검증 포인트 (요약)

각 slice의 "다음으로 넘어가는 조건". 상세는 각 절의 게이트 체크리스트.

| Slice | 진입 조건 | 완료 판정(요약) |
|---|---|---|
| K1 | K0 spec-lock(완료) | compose/bootstrap 멱등 + node-smoke green(KG off) — §3.5 |
| K2 | K1 게이트 | rebuild 멱등·parity 100%·owner-scope 미투영 회귀 — §4.11 |
| K3 | K2 게이트 | enqueue 원자성·replay 멱등·dead-letter·60초 e2e — §5.7 |
| K4 | K2 게이트 (K3과 병행 가능 — 읽기는 batch 그래프로 충분) | reject 회귀 세트·kg_query_runs·라우터 무변경 — §6.6 |
| K4b | K4 게이트 | 기존 분기 무회귀·fail-open·sources=wiki provenance — §7.5 |
| K5 | K4 게이트 (K4b 불요 — 패널은 page graph API만 소비) | browser QA + lint/build — §8.2 |
| K6 | K4 게이트 + K3(erasure 전파가 outbox 사용) | entity 멱등·missing_link·erasure·drill — §9.5 |
| K7 | **K4 reject 회귀 green**(명시 전제) + K2 boundary 회귀 유지 | 템플릿×역할 매트릭스·path-leak·flag-off 무변화 — §10.5 |

전 slice 공통 완료 조건: `make test`·`make docs-check`·CI green, 본 문서와
`docs/kg-model.md`의 해당 절 갱신(계약 변경 시), PR 제목 `[K<N>]` 마일스톤
ID, evidence(실행 명령·측정치·스크린샷) 기록.

---

## 14. 우선순위와 트레이드오프 기준

구현 중 선택지가 갈릴 때 일관 적용하는 순서:

1. **경계 > 기능**: fail-closed(scope/owner/flag) 위반 가능성이 있는 지름길은
   기능 지연보다 항상 나쁘다. 의심되면 reject/미투영이 기본값.
2. **SoR 권위 > 그래프 정합**: 그래프가 틀리면 rebuild로 고친다. SoR을
   그래프에 맞추는 코드(역방향 쓰기)는 어떤 이유로도 금지.
3. **단순 수렴 > 똑똑한 증분**: 증분 최적화(diff 스킵, 부분 prune)는 측정이
   필요를 증명한 뒤에만. v1은 멱등 MERGE + 주기 rebuild의 단순함을 지킨다.
4. **기존 패턴 동형 > 신규 발명**: 게이트·audit·flag·테스트 fixture 전부
   기존 심볼(§1)의 동형 복제가 기본. 새 패턴은 본 문서 개정과 함께만.
5. **P8 우선**: 우선순위 경합 시 K-series가 양보(kg-model §7). slice는 전부
   독립 merge 가능하므로 중단점이 곧 안정점이다.

---

## 15. 문서 관계 / 갱신 규약

| 문서 | 관계 |
|---|---|
| `docs/kg-model.md` | **canonical 설계 계약** — 충돌 시 우선. 스키마/계약 변경은 두 문서 동시 갱신 |
| 내부 문서(비공개) §11 | K-series 수용 기준 — 본 문서 §13이 그 기준의 slice 분해 |
| `docs/data-model.md` | K3 `kg_outbox`·K4 `kg_query_runs`·K6 `kg_entities`/`kg_entity_mentions` migration 반영 대상 |
| `docs/operations.md` | KG secret/port(반영 완료)·K6 erasure §8.4·rebuild drill·K7 flag runbook 추가 대상 |
| `scripts/check_docs_spec.py` | K1 PR에서 `REQUIRED_FILES`에 `docs/kg-model.md`·`docs/kg-implementation-spec.md` 추가 — 반영 완료(`scripts/check_docs_spec.py:21-22`; K7.2가 kg-model §4 B-table 토큰 검사도 추가) |

갱신 규약: 각 K-slice PR는 (구현 코드) + (Verify 테스트) + (본 문서 해당 절
실측치/결정 반영) + (kg-model.md — 계약이 바뀐 경우만)을 한 PR에 담는다.
측정 후 조정 가능으로 표시된 수치(timeout/limit/batch/poll)는 본 문서 갱신만
으로 조정할 수 있고, **상한 완화·경계 변경은 kg-model.md 개정이 필요한 별도
결정**이다.
