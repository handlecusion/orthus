# 전용 지식그래프 화면 — `/dashboard/graph` + `GET /kg/overview`

> 상태: 구현 완료. 회사쪽 대시보드 채널 그룹 아래 `#지식그래프`로 진입하는 독립 화면.

## 동기

KG 시각화는 지금까지 `/wiki/{slug}` 페이지의 "관련 그래프" 패널에만 묻혀 있어, 특정 위키
페이지를 열어야만 그래프를 볼 수 있었다. `/wiki`·`/agent-work`처럼 **전용 독립 화면**을 만들어
아무 페이지에서나 검색으로 시작 노드를 골라 회사 지식 그래프를 탐색할 수 있게 한다.

내부 문서(비공개) §2/§9는 당시 "새 전용 화면(entity hub / conflict dashboard)"을
**비목표**로 두고 §5.3에서 전용 entity 엔드포인트를 연기했다. 이 작업은 그 연기 항목을
**의도적으로 여는 것**이다 — 단, **read-only · K-series 정합** 범위를 지킨다: 새 write 경로
없음, raw Cypher 입력 없음, 모든 그래프 읽기는 기존 게이트(`run_kg_template`)를 그대로 통과.
신규 Cypher는 gate 레벨 집계(company count) 하나뿐이고 `owner_footprint`를 그대로 본떴다.

## 구성

`/dashboard/graph`는 `dashboard/layout.tsx`를 상속해 **회사 노드 전용 게이트**(personal 노드는
"회사 노드에서만" 안내)와 모바일 탭 스트립을 무료로 받는다. 위→아래:

1. **집계 헤더** (`GET /kg/overview`): 노드/연결/개체/미해결 모순 통계 타일 + by_label
   브레이크다운. owner-scope가 켜져 있으면 본인 personal footprint(`GET /wiki/kg/footprint`)를
   "내 개인 메모" 줄로 함께 표시.
2. **검색바** (`GET /wiki/search`, 300ms 디바운스): 아이콘 + 서술형 placeholder("인물·프로젝트·
   페이지를 검색해 그래프 탐색")를 가진 52px 바. 설명 없이도 "여기서 검색해 탐색한다"가 자명하게
   보이는 게 목적이라 그래프 바로 위에 눈에 띄게 둔다. 결과 클릭 → 그 페이지 그래프로 점프.
3. **그래프**: **진입 즉시 랜딩 개체 지도**(`GET /kg/graph`)를 렌더한다 — 빈 프롬프트 없음.
   검색으로 특정 페이지를 고르면 그 페이지 그래프(`GET /wiki/pages/{slug}/graph?include=entity`)로
   전환하고 "← 개체 지도로"로 돌아온다. 둘 다 기존 `GraphExplorerPanel`(더블클릭 확장
   `GET /wiki/graph/expand`, 노드 인스펙터, 누적 200노드 캡, d3-force)로 렌더하고, 렌더 실패는
   `ErrorBoundary`로 칩 리스트 강등.

### 왜 "개체 지도"가 랜딩인가 (그리고 왜 전체가 아닌가)

company 그래프는 실측 14,627노드/59,350엣지라 **인터랙티브 캔버스에 통째로 그릴 수 없다**
(탐색기 캡 200). 그래서 랜딩은 "대표 그래프를 바로 띄우고 더블클릭으로 나머지를 펼친다"로
설계했고, 대표 그래프는 **개체(Entity) co-mention 지도**다(owner 결정) — 인물·프로젝트·도구가
서로 어떻게 엮이는지가 가장 읽히는 의미 지도이고 노드 수도 렌더 가능 범위다. 개체를 더블클릭하면
`expand_entity`(MENTIONED_IN)로 그 개체를 언급한 페이지로 자연스럽게 드릴다운된다.

**시드 캡 규약**: 페이지 그래프는 wiki 페이지와 같은 관례로 초기 시드를 `GRAPH_TOTAL_CAP`(20)까지
접지만, **랜딩 개체 지도는 접지 않는다**(`buildFromGraph(..., {cap:false})`) — 서버 상한
(`kg_overview_limit`)이 이미 렌더 가능 크기이고 탐색기 누적 캡 200이 안전장치다. 20으로 접으면
지도가 텅 비어 보여 "전경을 보여준다"는 목적이 깨진다(실측: 캡 시 14노드 → 미적용 54노드).

FE는 검색/확장/페이지그래프를 전부 기존 표면으로 조합하므로 **신규 백엔드 표면은 집계 +
랜딩 개체 지도 둘뿐**이다.

## 엔드포인트 계약 — `GET /kg/overview`

- 라우터: `orthus/api/routes/kg.py`(prefix `/kg`), `orthus/api/main.py`에 등록.
- 인증: `get_session_user_or_knowledge_token`(기존 KG 읽기 4종과 동일 dual-auth). 응답은 count류
  뿐이라 page 본문 이하 민감도 — operator 가드 없음.
- 응답: `KgOverview`(`orthus/schemas/canonical.py`) — `node_count`, `edge_count`, `by_label`,
  `entity_count`(=`by_label["Entity"]` 파생), `unresolved_conflicts`, `resolved_conflicts`,
  `status`. 식별자(slug/이름/owner_id)는 담지 않는다.
- `Cache-Control: private, no-store`(기존 KG 엔드포인트와 통일, rebuild/ready 상태 stale 방지).
- 집계는 `company_kg_overview()`(`orthus/kg/gate.py`)가 수행 — `owner_footprint`와 동형으로
  `run_read`(READ 세션+timeout) + `audit("kg.overview")` + best-effort `kg_query_runs`
  (`template_name="kg_overview"`, `params_redacted={}`, `user_id=NULL`) 규율을 공유한다. scalar
  count라 `_map_records` tripwire를 타지 않아 술어가 by-construction 옳아야 하고, 단위/통합
  테스트가 이를 고정한다.
- **60초 TTL 캐시**(process-global, `entities_present` 동형): 전체 스캔 비용 때문에 필요하다
  (아래 "알려진 제약" 참조). **성공(ready)만 캐시** — `unavailable`을 캐시하면 Neo4j가 복구돼도
  장애를 TTL만큼 연장하므로 실패는 다음 요청이 즉시 재시도한다. flag-off(`disabled`)도 미캐시.
  stale 상한은 60초이고 카운트는 rebuild/outbox로만 변해 무해하다. HTTP `Cache-Control`
  (아래)과는 별개 레이어다 — 서버 내부 캐시이지 브라우저/CDN 캐시가 아니다.
  테스트 격리는 `invalidate_overview_cache()` + autouse fixture.

### fail-open 매트릭스

| 조건 | 응답(항상 200) | TTL 캐시 |
|---|---|---|
| `ORTHUS_KG_ENABLED=false` | `status="disabled"` (Neo4j 미왕복) | 미캐시 |
| `kg_rebuild_in_progress()` | `status="rebuilding"` (half-projected 오노출 보류) | 미캐시(endpoint 선판정) |
| Neo4j down / driver error / timeout | `status="unavailable"` (예외 미전파) | **미캐시**(복구 즉시 반영) |
| 정상 | `status="ready"` (count==0이어도 정직 — genuine 측정) | 60초 캐시 |

## 엔드포인트 계약 — `GET /kg/graph` (랜딩 개체 지도)

- 응답: `KgRelationResponse`(`supported`/`relation="entity_overview"`/`reason`/`truncated`/
  `nodes`/`edges`) — 기존 `/wiki/kg/query`와 같은 일반 그래프 응답형(페이지 앵커 없음).
- 인증/캐시: `/kg/overview`와 동일(dual-auth, `private, no-store`).
- 게이트 경유: `run_kg_template("entity_overview")` — 다른 그래프 읽기와 똑같이
  `kg_query_runs` + `audit("kg.retrieve")` + tripwire를 진다. raw Cypher 입력 없음.
- 템플릿 `entity_overview`(`orthus/kg/templates.py`): `company_always`(`:Entity`는 항상 company
  scope), 입력 파라미터 없음(`bind_params=()`, `slug_resolution=()`), 상한만
  `setting_params=(("overview_limit","kg_overview_limit"))`로 주입.
  Cypher는 `(:Entity)-[:RELATES_TO]->(:Entity)`를 `mention_count` 합 내림차순으로
  `$overview_limit`까지 — 가장 중심적인 허브가 먼저 보인다.
- **`$limit` 대신 `$overview_limit`인 이유**: 게이트가 `bind["limit"]`을
  `min(requested, kg_query_limit=50)`으로 캡하므로 `$limit`을 재바인딩하면 충돌한다
  (`settings.py` `kg_expand_limit` 주석의 선례). 별도 이름을 써 캡을 우회하지 않고 비껴간다.
- fail-open: flag-off → `supported:false reason="kg_disabled"`, rebuild/Neo4j 미가용 →
  `supported:false reason="kg_unavailable"` (모두 200).

### 드리프트 가드 (신규 템플릿 추가 시 필수)

`entity_overview`는 registry-completeness 가드가 요구하는 3곳에 함께 등록해야 CI가 green이다
(의도된 self-defending 경계):
`templates.py::_OWNER_SAMPLE_PARAMS`, `tests/unit/test_kg_registry_completeness.py::_COMPANY_ALWAYS`,
같은 파일 `_sample()`.

## owner-scope 경계

집계 Cypher는 **company-only 술어(`scope='company'`)만** 쓰고 `$caller`를 바인딩하지 않는다 —
타 owner의 personal 노드/엣지는 술어상 애초에 매칭되지 않는다. 본인 personal 카운트는 별도
`/wiki/kg/footprint`가 준다. 통합 테스트가 **분할 증명**(overview + Σowner_footprint == scope
있는 전역 노드 수)으로 누출 0을 고정한다. `:KgMeta`/`:OutboxApplied` 동기화 메타는 scope 속성이
없어 양쪽 카운트에서 자연 제외된다.

## 알려진 제약 / 후속

- **strict `scope='company'` 술어**: scope 프로퍼티 없는 pre-v2 잔재 노드는 미집계된다. 운영
  규칙(operations §2.1)이 flag-on 전 rebuild(schema v2 stamp)를 선행하므로 수용 가능. 미집계가
  문제되면 `_scope_of` 의미와 정합하게 `OR (scope IS NULL AND owner_id IS NULL)`을 더한다.
- **전그래프 스캔 카운트 — 타임아웃 여유가 적다(실측)**: `MATCH (n)` / `()-[r]->()`는 라벨 무관
  full scan이라 인덱스를 타지 않는다(WikiPage/Claim/Source에 `scope` 인덱스가 있어도 무의미 —
  라벨 없는 all-node scan이라서). **Neo4j page cache가 식으면 실측 노드 1.2s + 엣지 1.3s**
  (워밍 시 각각 ~0.1s, 14.6k노드/59k엣지 기준, 앱 드라이버 측정). `run_read`의 transaction
  timeout은 쿼리당 `kg_query_timeout_ms`(기본 **2s**)라 콜드에서 여유가 40%뿐이고, 부하가
  겹치면 초과 → blanket except → `unavailable`로 수렴한다.
  → **60초 TTL 캐시로 완화**(`gate.py` `_OVERVIEW_TTL_S`): 스캔 빈도를 분당 1회로 낮춰 그
  창을 좁힌다. 실측 콜드 7.1s → 캐시 히트 0.03s. **단 캐시는 빈도만 줄이지 비용을 없애지
  않는다** — TTL 만료 후 첫 요청이 콜드와 겹치면 여전히 한 번 `unavailable`이 뜰 수 있고
  다음 요청에 복구된다. 근본 해소(라벨별 인덱스 카운트/rel scope 인덱스/집계 사전계산)는 후속.
  참고: 기존 `owner_footprint`도 동일한 all-node scan 패턴이라 같은 노출을 공유한다.
  ⚠️ 성능 측정은 반드시 앱 드라이버(`orthus.kg.client.run_read`)로 할 것 — `cypher-shell`은
  JVM 기동만 ~5s라 측정을 지배해 오판을 부른다(초기 진단에서 실제로 15s로 잘못 읽었다).
- `include=entity` 기본 시드 fetch는 wiki 페이지의 teaser-first UX와 다르다 — 탐색 화면 의도적
  선택. QA에서 엔티티 노이즈가 크면 teaser 방식으로 회귀.
- `/wiki/search`는 임베딩 retrieve 기반이라 짧은 질의가 fuzzy할 수 있음 — v1 수용, title-prefix
  시드 엔드포인트는 후속.

## 테스트

- `tests/unit/test_kg_overview.py`: company-only 술어 + `$caller`/personal 미포함, flag-off
  disabled(Neo4j 미왕복), unavailable fail-open, ready 집계(라벨/엣지/모순 분리), 4-state 스키마,
  엔드포인트 rebuilding + Cache-Control. 개체 지도: Cypher company-only + `$overview_limit`,
  `company_always` 등록/무입력, 정적 write-scan 포함, `/kg/graph` disabled/rebuilding.
- `tests/integration/test_kg_overview.py`(neo4j-test): company-only 분할 증명, entity_count 일치,
  엔드포인트 + `kg_query_runs` row, flag-off disabled. 개체 지도: co-mention 시드 →
  `:Entity` 노드 + `RELATES_TO` 엣지 반환 + 전 노드 company scope.

## 운영 주의 — `kg_available()` 30초 음성캐시

`kg_available()`은 양성 5초/**음성 30초** 캐시다(`client.py`, K4 의도된 트레이드오프). API 재기동
직후나 Neo4j 드라이버가 cold일 때 첫 `verify_connectivity()`가 한 번 실패하면 **30초간 모든 KG
읽기가 `unavailable`**로 수렴한다 — 이 화면에선 통계 배너 + 그래프 미표시로 보인다. 코드 결함이
아니라 fail-open 설계이며 수 초~30초 뒤 자동 정상화된다(로컬 QA에서 반복 재기동 시 특히 자주
관측). 안정 상태 실측은 신규 로드 3회 연속 배너 0 · 개체 54노드 렌더.
