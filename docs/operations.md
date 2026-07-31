# Orthus — 운영 정책

> status: current operations policy
> updated: 2026-06-01
> authority: 내부 문서(비공개)의 최종 방향성을 운영에서 지키기 위한 정책.
> 상세 구조는 `docs/architecture-v2.md`, local node 운영은 내부 문서(비공개),
> S1 공개 edge는 내부 문서(비공개)와 내부 문서(비공개)를 따른다.

이 문서는 현행 사내 SaaS 라인(central company 아카식 + personal local nodes)의
운영 규칙이다. 과거 LangGraph/persona/KG/Redis DLQ/confidence routing 운영서는
더 이상 현재 구현 기준이 아니다.

---

## 1. 운영 불변식

운영자는 아래 조건을 깨는 설정, PR, 수동 DB 작업, 배포를 승인하지 않는다.

- central과 personal은 DB/corpus/vector/wiki-store/agent/FE/runtime을 공유하지 않는다.
- 개인 raw/corpus/wiki는 central로 자동 저장하지 않는다. 단, 회사 도메인 메일(@nova.example/@acme.example)은 P6 통합 메일(내부 문서(비공개) §5-A)에 따라 personal node를 거치지 않고 company-scope source로 직접 흡수한다. *(P6.7 개정 예정: 회사 도메인 메일 ingest scope를 메일함별 owner 선택 — default owner-scope, 명시 opt-in 시 company-scope — 으로 확장한다. 내부 문서(비공개) §12. 구현 전까지 현행 company-scope 직접 흡수가 유효.)* 진짜 개인 소스의 personal→central 자동 저장 금지는 그대로 유지된다.
- personal→central 이동은 publish/promote 게이트만 사용한다.
- `/ask` 답변은 compiled wiki page 또는 검증된 read-only structured query에만
  grounded 된다.
- assistant, connector, router가 LLM-only 판단만으로 write/DDL/DML 또는 external write를
  실행하지 않는다. P6 이후 LLM action judgment는 bounded policy input으로 허용하지만
  typed action allowlist, role, secret state, rate limit, audit guard, kill switch를
  통과해야 한다.
- public browser access는 `session` mode + Google OAuth + invite allowlist를 쓴다.
- public host에서 `demo` auth, dev-login, insecure cookie가 열리면 안 된다.
- app-internal ports(`3820`, `8820`, `8880`, `3000`, `8000`)를 public internet에
  직접 노출하지 않는다. K1 이후 Neo4j ports(`7687` bolt, `7474` HTTP, `7688`
  test)도 동일하게 loopback 전용이며 public 노출 금지다(`docs/kg-model.md` §1).
- secret value를 git, PR body, screenshot, markdown, DB redacted settings에 남기지 않는다.
- Linear connector, LangGraph/persona/drift/confidence-routing 신규 코드는
  별도 결정 전까지 만들지 않는다. KG backend는 K0 spec-lock(2026-06-10) 이후
  K-series 범위(`docs/kg-model.md`)에서만 허용하며 `ORTHUS_KG_ENABLED=false`
  fail-closed를 유지한다.

---

## 2. Node 운영

기본 node:

| node | 역할 | public host | local API/Web |
|---|---|---|---|
| `company` | central 회사 아카식 + 회사 connector + central FE/API | `https://orthus-central.example.com` | `8820` / `3820` |
| `personal-a` | 개인 wiki + 개인 connector + 개인 비서/FE | owner가 별도 결정 | `8830` / `3830` |

운영 규칙:

- local runtime root는 `~/.orthus/nodes/<node>/`다.
- node별 `node.env`, `web.env`, `wiki-store`, imports/exports/logs 디렉터리를 분리한다.
- 같은 Mac mini에서 company와 personal을 side-by-side로 띄워도 storage는 분리한다.
- worktree 작업 중 `.env`, `web/.env.local`, node env 파일을 복사할 수 있지만 stage/commit하지 않는다.
- node smoke:

```bash
make node-smoke NODE=company
make node-smoke NODE=personal-a
```

### 2.1 KG (Neo4j) 운영 — K2/K3

> 통합 실행서: 내부 문서(비공개)

> **K7.2 owner-scope read path (구현됨, 비활성 default).** central 단일 Neo4j에
> personal owner-scope row를 여는 read 경로(owner-variant 템플릿 + 경계 매트릭스)는
> `ORTHUS_KG_OWNER_SCOPE_ENABLED`(D3 double-flag: P8 `ORTHUS_OWNER_SCOPE_ENABLED`도
> 켜져야 동작) 뒤에서 fail-closed default다. 운영 요점: ① start-node resolve는 **PG**
> `wiki_pages`(`resolve_slug`, fail-open — Neo4j 다운에도 동작), ② flag-on인데 게이트가
> owner-variant가 아니면 lifespan 가드가 **KG를 강제 off**(refuse-boot 아님,
> `kg.owner_scope_mismatch` audit), ③ 개인 slug는 `kg_query_runs`에 `personal:<hash>`로만
> 기록(개인 제목 비노출), ④ `entity_mentions`는 owner-scope에서 `deferred_template` reject.
> 경계 계약은 `docs/kg-model.md` §4 B1–B6 표가 SoR다. **활성화 절차/순서(인덱스→rebuild→
> flag)·latency 게이트·erasure는 K7.5 runbook에서 확정**하며, K7.2 단독으로 flag를 켜지
> 않는다(rebuild가 flag보다 먼저 + K7.5 owner-erasure 머지가 선결).

KG는 central(company) 전용 rebuildable 파생 인덱스다(`docs/kg-model.md`).
personal node는 Neo4j를 갖지 않는다. Postgres + wiki-store가 SoR이며, 그래프가
의심스러우면 언제든 full rebuild가 ground truth를 복원한다.

**SoR 단일성 — 가장 중요한 규칙:** 그래프 하나에는 SoR DB 하나만 쓴다. prod
central의 company 데이터는 node DB(`~/.orthus/nodes/company/node.env`,
`orthus_company`)에 있으므로 **prod에서는 반드시 `node-kg-*` target을 쓴다.**
root `.env` 기반 `make kg-bootstrap`/`kg-rebuild`/`kg-sync`는 repo dev DB
(`orthus`) 전용이다. 두 env를 섞으면 rebuild prune(삭제 수렴 권위)이 상대 DB가
투영한 노드를 전부 "기대 외"로 삭제해 그래프가 두 SoR 사이를 진동한다.
projection 쓰기 경로는 코드 레벨에서도 company node 전용이다 —
`require_company_node()`가 `node_kind != company`면 fail-closed로 거부한다.

**최초 프로덕션 활성화(off→on)** — 순서 고정(구현 명세 §4.8). **rebuild가
flag보다 먼저다**: 켜진 상태에서 빈 그래프를 노출하지 않는다.

1. keychain에 `orthus/kg/password` 등록(`.env` `ORTHUS_KG_PASSWORD`는 dev fallback).
2. `make up` — neo4j 컨테이너 기동(loopback 7474/7687).
3. `make node-kg-bootstrap NODE=company` — constraints/index 멱등 적용.
4. company node env(`ORTHUS_KG_ENABLED=true` 일시 주입)로
   `make node-kg-rebuild NODE=company` — full projection.
5. company `node.env`에 `ORTHUS_KG_ENABLED=true` 영구 설정.
6. API 재기동.

**Steady-state 배포(flag 이미 ON 상태 — 신규 라벨/테이블 추가 slice, 예: K6).**
위 off→on 절차는 cold-start용이다. flag가 이미 켜져 있고 `KGOutboxWorker`가
도는 상태에서 신규 projection 코드 + 신규 테이블을 배포할 때는 순서가 다르다 —
**migration이 코드 재기동보다 먼저**다(반대로 하면 실행 중 worker가
`load_one`→missing table로 실제 wiki 이벤트를 dead-letter한다):

1. `alembic upgrade`(예: `0049_kg_entities`) — additive 테이블 먼저.
2. `make node-kg-bootstrap NODE=company` — 신규 constraint(`:Entity` 등) 멱등 적용.
3. 코드 배포 + API 재기동.
4. **즉시** `make node-kg-rebuild NODE=company` — 신규 라벨 백필(반-투영 그래프
   노출 방지). `kg-rebuild`는 PG→Neo4j projection이며 LLM을 호출하지 않는다 —
   신규 라벨이 PG SoR에 이미 있어야 채워진다(예: entity는 distill이 채운 뒤
   누적되므로 초기엔 sparse, going-forward 누적; 전 corpus 백필은 별도
   re-distill 결정).
5. `SELECT * FROM kg_outbox WHERE status='dead'`가 빈 것을 확인.

**주기 동기화** — 두 경로가 함께 돌며, 둘 다 company node env를 쓴다(kg-model
§3 삭제 의미론):

- incremental: company node sync cycle에 `KG_SYNC=1`을 설정하면
  `scripts/node/sync_cycle.sh`가 node env로 `orthus.kg.sync`(watermark 증분
  upsert)를 함께 실행한다. personal node에서는 `KG_SYNC=1`이어도 skip한다.
  sync 실패는 cycle을 죽이지 않는다(fail-open — 다음 rebuild가 수렴).
- full: 삭제 수렴의 권위는 주기 `make node-kg-rebuild NODE=company`다. launchd
  off-peak plist(`ai.orthus.company.kg-rebuild`, 1일 1회) 설치는 운영자 작업이며,
  K2는 Makefile target까지만 제공한다.

**sync가 보는 것의 한계:** 증분 sync는 `updated_at` watermark 기반이다. 행을
바꾸면서 `updated_at`을 갱신하지 않는 쓰기는 다음 full rebuild까지 그래프에
반영되지 않는다 — `/projects` 재태깅과 slack retag 경로는 이 이유로
`updated_at`을 함께 갱신한다(신규 SoR 쓰기 경로를 추가할 때 같은 의무가 있다).

`kg-sync`는 `:KgMeta.kg_schema_version` 불일치나 watermark 부재 시 rebuild를
요구하며 그래프를 건드리지 않는다(exit≠0). Neo4j volume을 삭제했다면
bootstrap+rebuild로 재구축한다 — watermark도 volume과 함께 사라지므로 별도
상태 정리가 필요 없다.

**conflict status freshness (K8).** 모순(CONFLICTS_WITH) 엣지의 `status`
(`UNRESOLVED`/`RESOLVED`)는 conflict WikiTask의 `resolved`에서 유도한다. 운영자가
`POST /wiki/tasks/{slug}/resolve`(또는 reopen)로 conflict task를 바꾸면
`write_task → _persist`가 K3 outbox에 task 이벤트를 enqueue하고, worker drain이
`_build_conflict_index`로 그 쌍의 CONFLICTS_WITH `status`를 재투영한다(노드 추가 없이
엣지 속성만 재SET). 양방향이다: resolve→`RESOLVED`, reopen→`UNRESOLVED`. 즉시성은
비목표다 — outbox SLA 실측 ~1.65초이나 운영 표기는 "수 분 내"로 보수적으로 잡는다.
주기 full rebuild가 같은 `_build_conflict_index` 경로로 수렴 권위를 갖는다. owner-scope
ON이면 personal conflict task의 status도 같은 outbox/rebuild 경로로 각 owner
네임스페이스에 재투영된다(아래 K8.6 백필 참조). owner-scope OFF면 company conflict
status만 권위적이다.

**rebuild drill (K6 — rebuildable 복구 검증).** 그래프는 PG SoR의 결정론
투영이므로 Neo4j volume이 사라져도 재구축된다. 절차(**prod 그래프 volume 삭제
금지** — dev/kg-test 대상):

1. (volume loss) Neo4j 데이터 소실 또는 `MATCH (n) DETACH DELETE n`.
2. `make node-kg-bootstrap NODE=company` — constraints/index 재적용.
3. `make node-kg-rebuild NODE=company` — full projection 재구축.
4. parity: 재구축 그래프 노드/엣지/entity 수가 소실 전과 동일.
5. idempotency: 한 번 더 rebuild → prune 0(무변경).

> parity 판정은 **소실 전 == 재구축 후**(노드/엣지/entity 수)이지 고정 상수가
> 아니다 — seed에 따라 절대값은 달라진다. kg-test 1회 실측(2026-06-14)에서는
> 소형 seed가 wipe 전후 동일 카운트로 재구축됐고(parity OK) 재-rebuild prune이
> 0이었다. 회귀는 `tests/integration/test_kg_*`의 rebuild 멱등/erasure 테스트가
> 상시 고정한다.

**K7 owner-scope activation (operator-gated, off→on).** personal owner-scope row를 단일
central 그래프에 여는 절차다. **rebuild가 flag보다 먼저**(반-투영 노출 금지) + owner-erasure
머지가 선결이다. B-legend: B1=wire tripwire, B3=overpermissive edge, B4=cross-owner placeholder,
B5=foreign≡absent 404(`docs/kg-model.md` §4 B1–B6 매트릭스가 SoR).

1. K7.1–K7.5 코드가 main에 머지됐는지 확인(projection/gate/erasure/monitor).
2. `make node-kg-bootstrap NODE=company` — constraint/index 멱등.
3. P8 `ORTHUS_OWNER_SCOPE_ENABLED=true`가 켜져 있는지 확인(D3 double-flag — 안 켜져 있으면
   owner-scope는 no-op).
4. `ORTHUS_KG_OWNER_SCOPE_ENABLED=true` 일시 주입으로 **`make node-kg-rebuild NODE=company`**
   — owner-scope row까지 포함한 full projection(schema v2). rebuild가 `kg_schema_version=2`를
   세팅해 worker schema hold를 푼다.
5. `make node-kg-monitor NODE=company`로 boundary 매트릭스 green(B1–B6 must-be-zero=0) 확인.
6. company `node.env`에 `ORTHUS_KG_OWNER_SCOPE_ENABLED=true` 영구 설정.
7. API 재기동 — lifespan 가드(`verify_owner_scope_gate_consistency`)가 owner-variant 템플릿
   부재면 KG를 강제 off(`kg.owner_scope_mismatch` audit, refuse-boot 아님).
8. `make node-kg-monitor-scheduler-install NODE=company` — boundary/health monitor 주기 스케줄.
9. owner 시나리오 browser-QA(footprint status-gating, '내 개인 메모' 패널) + admin
   boundary-health readout(`python -m orthus.kg.monitor --boundary-health`) green.

**K8.6 personal conflict status 백필.** K8은 owner-scope conflict status 코어(K8.6)를
함께 싣는다. owner-scope를 처음 켜는 경우엔 위 step 4 rebuild가 personal conflict
status까지 한 번에 백필하므로 추가 작업이 없다. **이미 owner-scope ON인 노드에 K8을
배포할 때만** 기존 personal CONFLICTS_WITH 엣지의 status를 새 owner-ns 인덱스로 채우기
위해 **`make node-kg-rebuild NODE=company` 1회**가 필요하다(백필 전에는 이미 적재된
personal conflict 엣지가 default `UNRESOLVED`로 보이고, 새 task 변경은 outbox 증분으로
정상 반영된다). company-only conflict는 rebuild를 건너뛰어도 불변이다. K8은 마이그레이션·
`kg_schema_version` bump가 없다(속성 신규 아님) — 배포는 코드 + API 재기동(+위 백필 1회).

**Rollback tiers (per-sub-PR; migration 0057은 additive·down-migration 불요):**

- **code-revert:** K7.x PR을 revert — flag-off 동작은 v1 company-only로 불변(코드 경로가
  `kg_owner_scope_enabled()`를 AND).
- **fast rollback(flag off, rebuild 없음):** `ORTHUS_KG_OWNER_SCOPE_ENABLED=false` + 재기동.
  **단, 그래프엔 personal 노드가 잔존**해 monitor `residual_personal` 위반을 낸다 → 다음
  flag-on 전 **하드 삭제 데드라인**(아래 clean rollback)을 둔다. fast는 임시 차단일 뿐 완료가
  아니다.
- **clean rollback(PROVEN — 미래 schema bump에도 durable):** wipe-graph(`MATCH (n) DETACH
  DELETE n`) → flag off → `make node-kg-rebuild NODE=company`(v1 company-only 투영). owner_id≠NULL
  노드/엣지·scope='personal'·v2 placeholder가 0으로 수렴한다(`tests/integration/test_kg_clean_rollback.py`가
  고정). 멱등 + bare sync 무-resurrection.

**rebuild headroom baseline.** rebuild는 `:KgMeta.last_rebuild_seconds`를 기록하고 monitor가
이를 노출한다. measured rebuild가 `0.5×ORTHUS_KG_REBUILD_LOCK_MINUTES`(default 30분 → 15분)를
넘으면 rebuild CLI가 headroom WARN(soft)을 낸다 — staleness SLA headroom이 빠듯하다는
신호이며 `docs/kg-implementation-spec.md` §6.3 chunked rebuild 검토 대상이다. dev 실측
(2026-06-17, WSL + neo4j-test :7688, mock embed): company-only 300-page cold rebuild ≈5.56s,
company+synthetic-owner(300+300) 증분 ≈0.97s — WSL/소형 synthetic seed라 prod 비대표다.
**활성화 시점 Mac mini 실 corpus(company-only + company+personal-synthetic) 실측이 권위
baseline**이며, 그 값이 0.5×lock을 넘으면 chunked rebuild를 in-scope로 올린다.

**stuck `rebuild_in_progress` 복구.** rebuild HOLD는 `:KgMeta.rebuild_in_progress`(boolean)이며
완료/abort에 clear된다(시간 만료 release 없음). SIGKILL 등으로 clear가 누락되면 stuck-true가
되어 worker/sync가 영구 정지한다 — **자동치유 없음**. 증상: monitor가 `rebuild: STUCK`(exit 4,
could-not-verify) + outbox pending 증가 + dead 없음. 복구는 **`make node-kg-rebuild NODE=company`
재실행**(완료 시 HOLD를 정상 clear).

> **HOLD 종류별 stuck 임계 (review #1).** 같은 boolean HOLD를 owner erasure도 재사용한다
> (`:KgMeta.rebuild_hold_kind` = `"rebuild"` | `"erase"`). erase는 카운트 + DETACH 2회로
> sub-second에 끝나야 하므로 monitor는 erase HOLD의 stuck 임계를 `ORTHUS_KG_REBUILD_LOCK_MINUTES`
> (기본 30분 headroom)가 아니라 **고정 5분**으로 본다. 즉 erase 도중 Neo4j가 죽어 clear가
> 누락되면(worker/sync 정지 + 그래프 read 보류) 30분이 아니라 5분 만에 `rebuild: STUCK`(exit 4)로
> escalate한다. rebuild HOLD는 정당하게 길 수 있어 headroom lock을 유지한다. 메시지는 HOLD 종류를
> 표기한다(`erase HOLD held for …` / `rebuild HOLD held for …`). 복구는 동일하게 rebuild 재실행이며,
> erase 후 rebuild는 PG 사본이 남아 있으면 owner 노드를 재투영할 수 있다(P8 PG-delete 전까지 의도된
> 동작 — `pg_copies_pending=True`).

**모니터링 (K6 + K7.5 — read-only).** 신규 public route 없이 운영 요약을 CLI로 본다:
`make node-kg-monitor NODE=company`(= `python -m orthus.kg.monitor`)가 `kg_outbox`
status별 건수 + 최고 pending 적체 나이, 최근 7일 `kg_query_runs` status 분포,
`:KgMeta` last_sync/last_rebuild 나이, `:Entity`/placeholder 노드 수, owner-scope ON이면
B1–B6 경계 매트릭스 + rebuild HOLD 상태를 출력한다. PG 부분은 항상, Neo4j 부분은 가용할 때만
채운다(fail-open). exit code 사다리(먼저 매칭): **could-not-verify(4, stuck rebuild 포함) →
boundary-violation(3) → dead(1) → backlog-stale(2) → rebuild-in-progress(5, fresh) → ok(0)**.
`make node-kg-monitor-scheduler-install`로 주기 스케줄(`scripts/node/kg_monitor_cycle.sh`,
code별 distinct sentinel)을 설치하고, 책임 admin은 `python -m orthus.kg.monitor --boundary-health`로
**counts-only readout**(healthy + last_verified_at + must-be-zero 카운터)을 본다 — 스케줄이 기대
주기 내 미실행이면 `healthy=stale`(dead cadence가 healthy로 위장 불가). `make node-kg-monitor`를
root `.env`로 돌리는 것은 dev-only이며 prod 경계를 보지 못한다(거짓안심 — node 래퍼 필수).

**준실시간 outbox (K3)** — wiki consolidate/document publish/promote approve의
PG commit과 같은 트랜잭션이 `kg_outbox`에 이벤트를 적재하고, central API
프로세스의 lifespan background worker(`KGOutboxWorker`)가 Neo4j에 멱등
적용한다(`docs/kg-model.md` §3, 구현 명세 §5):

- worker 기동 조건은 `ORTHUS_KG_ENABLED=true` + company node다. personal
  node/flag off에서는 thread 자체가 뜨지 않는다.
- **flag off 기간의 변경은 outbox에 쌓이지 않는다** — 위 활성화 절차에서
  rebuild가 flag보다 먼저인 이유가 이것이다. flag를 껐다 켤 때도 rebuild
  1회를 먼저 돌린다.
- Neo4j 미가용 시 이벤트는 pending으로 적체되고(attempts 미증가) 재기동 후
  자동 drain된다. 수동 drain은 `python -m orthus.kg.outbox drain`(company node
  env, launchd fallback 겸용), 적체 정리는 같은 CLI의 `trim`이다.
- 이벤트 실패는 5회에서 `status='dead'`로 격리된다. 가시화는 K3에서는
  `audit_log`의 `kg.apply` error span과
  `SELECT * FROM kg_outbox WHERE status='dead'` 조회로 충분하다(대시보드
  노출은 K6 모니터링 — kg-model §5). dead/적체를 방치해도 주기 rebuild가
  최종 수렴을 보장한다 — "늦을 뿐 틀리지 않는" 상태가 설계 불변이다.
- `applied` row와 Neo4j `:OutboxApplied` 마커는 30일 보존 후 worker가
  저빈도 best-effort로 함께 정리한다.

---

## 3. Secrets

### 3.1 보관

- `.env`와 node env는 local runtime 전용이다. git에 올리지 않는다.
- `.env.example`과 docs에는 key 이름과 placeholder만 둔다.
- 운영 secret은 secret backend에 둔다. local Mac 기본은 macOS Keychain
  (`ORTHUS_SECRET_BACKEND=auto|keychain`)이다.
- non-mac runtime은 secret backend를 명시한다. 테스트는 memory backend만 사용한다.
- connector token은 web `/connectors`에서 입력할 수 있다. backend는 token value를
  local secret backend에 저장하고 DB에는 `settings_redacted.secret_refs`만 남긴다.
- `.env` connector token은 bootstrap/dev fallback이다. web config가 primary다.
- GWS Gmail/Drive는 orthus secret store를 쓰지 않는다. Google auth는 node-local `gws`
  CLI config/keyring이 소유한다.
- Google OAuth app login은 identity only다. orthus는 Google access/refresh token을
  app login용으로 저장하지 않는다.

### 3.2 주요 key

| Kind | Naming | 비고 |
|---|---|---|
| DB DSN | `ORTHUS_PG_DSN`, `ORTHUS_PG_DSN_READONLY` | node별 DB를 가리켜야 한다 |
| LLM/model | `ORTHUS_LLM_SOLAR_API_KEY`, `ORTHUS_EMBEDDING_SOLAR_API_KEY` | chat/embedding 슬롯의 Upstage key(같은 계정이면 하나로 공유 가능), secret backend 보관 |
| Connector creds | `ORTHUS_CONN_<source>_<field>` | fallback only, web config primary |
| JWT | `ORTHUS_AUTH_JWT_SECRET` | service-to-service/API client용 |
| Session | `ORTHUS_AUTH_SESSION_SECRET` | browser session signing/crypto |
| Google OAuth | `ORTHUS_GOOGLE_OAUTH_CLIENT_ID`, `ORTHUS_GOOGLE_OAUTH_CLIENT_SECRET` | public login용 |
| Public auth URL/cookie | `ORTHUS_AUTH_PUBLIC_BASE_URL`, `ORTHUS_AUTH_WEB_BASE_URL`, `ORTHUS_AUTH_COOKIE_NAME` | node별 public/local auth routing |
| KG (Neo4j) | keychain ref `orthus/kg/password`, env `ORTHUS_KG_PASSWORD` | K1, central 전용. keychain 우선, env는 bootstrap/dev fallback(`docs/kg-model.md` §1). compose `NEO4J_AUTH`가 미설정 기동을 거부 |

---

## 4. Auth / Access

지원 auth mode:

- `demo`: local/dev only. public exposure에서 금지.
- `jwt`: service-to-service/API client용. `X-User-Id`를 신뢰하지 않는다.
- `session`: browser user용. Google OAuth + invite allowlist + server session.

S1 public requirements:

- central public host는 `https://orthus-central.example.com`.
- session TTL은 sliding 90일, renewable absolute 3650일이다.
- DB에는 session token hash만 저장한다.
- cookie는 host-scoped, `Secure`, `HttpOnly`, `SameSite=Lax`여야 한다.
- `Domain=.orthus-central.example.com`는 cross-subdomain SSO 결정 전까지 쓰지 않는다.
- central allowlist는 central owner/admin이 관리한다.
- personal allowlist는 해당 personal owner/admin이 관리한다.
- 마지막 active owner/admin은 revoke/demote할 수 없다.
- 사용자는 자기 allowlist row를 직접 revoke할 수 없다.
- dev login은 local QA 전용이다. `ORTHUS_AUTH_DEV_LOGIN_ENABLED=true`는 public
  base URL이 localhost가 아니면 reject되어야 한다.

Public boot guard:

- public URL에서 `ORTHUS_AUTH_MODE=demo`면 app boot가 실패해야 한다.
- public session mode는 session secret, secure cookie, dev login off,
  Google OAuth client id/secret이 모두 있어야 한다.

---

## 6. Connector 운영

공통 정책:

- 모든 source는 공통 `Connector` substrate를 통해 붙인다.
- company/personal 분기는 `connector_accounts.account_kind`, `scope`, `owner_id`,
  `project`, `node_id` 정책으로 한다.
- document idempotency는 `(source, source_account_id, source_external_id)` 기준이다.
- cursor/seen/budget/run history는 account 단위로 관리한다.
- connector config UI는 필수값만 받는다. default path를 아는 local connector는
  사용자에게 path 입력을 요구하지 않는다.
- `ensure`, `config`, `sync`, legacy Notion import는 node-local command trigger다.
  `session` mode에서는 owner/admin만 실행할 수 있고 `connector.command` audit span을 남긴다.
- GWS connector는 allowlisted `gws` executable만 argv로 실행한다. `shell=True`와
  arbitrary command string은 금지다.

현재 source boundary:

| Connector | Node policy |
|---|---|
| Notion | company only in P8 thin collector; legacy personal-node connector remains pre-cutover only |
| Slack | company only |
| local_files | personal only |
| codex_sessions / claude_sessions | personal only |
| chat_exports / email_exports | legacy personal-node only; excluded from P8 thin collector |
| GitHub | personal only |
| `gws_gmail` / `gws_drive` | personal only, node-local `gws` CLI auth |

Personal source를 central로 공유하려면 connector sync가 아니라 `/promote`를 사용한다.

---

## 7. Publish / Promote

personal→central 이동 경로:

```text
personal export package
  -> redaction/sanitize
  -> central promote staging
  -> owner/admin review
  -> approve
  -> central corpus/wiki import + consolidate
```

운영 규칙:

- `pending` staging row는 central corpus/wiki에 들어간 것이 아니다.
- approve/reject는 central owner/admin만 수행한다.
- member/viewer가 approve/reject할 수 있으면 release blocker다.
- stage payload와 review UI는 raw personal secret/PII가 그대로 보이지 않게 해야 한다.
- promote 실패는 자동 import로 보정하지 않는다. 실패 stage를 조사하고 재시도한다.

---

## 8. PII / Redaction

### 8.1 Redaction 대상

- `audit_log.output`, error payload, connector command output.
- `query_runs.nl_question`, `query_runs.compiled_sql`.
- wiki page/task 저장 전 사용자 입력에서 감지된 PII.
- email/chat/session/local file connector output.
- promote export/stage payload.

### 8.2 Rules

- redaction helper는 `orthus/audit/redact.py`의 `redact_pii()`와
  `redact_pii_text()`를 사용한다.
- 기본 rule은 email, phone, 주민번호 형태, card number 형태를 mask한다.
- redaction은 저장 전 적용한다. UI에서 가리는 것은 at-rest 보호가 아니다. 단, P6 통합 메일 ingest 경로(내부 문서(비공개) §5-B)는 redaction을 생략한다(회사 내부 지식 한정, 외부 유출 아님). P8 personal ingest/compile 경로(내부 문서(비공개) §5-B)도 owner-only row-level 경계 안에서 redaction을 생략하되, company scope 전환(promote) 시점의 redaction/sanitize 의무는 유지한다. 그 외 경로의 redaction 의무는 유지된다.
- raw personal import가 central로 넘어가기 전에 redaction/sanitize를 다시 수행한다.
- redaction 우회가 발견되면 data bug가 아니라 security/privacy bug로 취급한다.

### 8.3 Sensitive data

| Class | 예 | 정책 |
|---|---|---|
| Direct PII | 이름, 이메일, 전화, 주소 | 저장 전 필요한 최소값만 유지, audit/output에는 redact |
| Work sensitive | 회사 계약, 고객, 일정, repo, issue | node boundary와 access role로 보호 |
| Personal sensitive | 개인 email, AI session, local file | personal node 안에 유지, central 이동은 promote only |
| Secret | token, API key, cookie, OAuth secret | secret backend only, docs/DB/log 금지 |

**K6 person-entity carve-out (문서화된 예외).** `redact_pii`는 이메일/RRN/카드/
전화만 거르고 **사람 이름은 거르지 못한다**. KG entity 레이어(`docs/kg-model.md`
§2, K6)는 `entity_kind=person`의 `display_name`/`name_norm`(=사람 이름)을 저장
하는데, 이는 Direct PII다. 회사 내부 지식 한정의 carve-out으로 저장을 허용하되
보상통제를 둔다: ① persist 전 entity 값에 `redact_pii` 적용(이름에 섞인 이메일/
전화만 차단, 이름 자체는 company knowledge로 유지) ② company-scope-only +
central-only(loopback 7474/7687) + 외부 노출 없음 ③ owner-erasure 경로 보장
(§8.4) ④ carve-out은 `entity_kind=person`에만(org/project/system 이름은 PII
아님). 본 carve-out은 P6 §5-B / P8 §5-B와 같은 "회사 내부 지식 redaction 완화"
계열이며, 적용 전 owner 검토 대상이다.

### 8.4 Erasure

현재 self-service erasure UI는 최종 산출물에 포함되어 있지 않다. 삭제 요청이나 사고 대응 시
운영자는 node별 저장소를 기준으로 삭제한다. **company wiki page는 먼저 아래 KG 항목의
`make node-kg-erase`를 실행한다** — KG/Neo4j까지 한 번에 처리하므로, 그 전에 wiki/KG row를
수동 선삭제하면 Neo4j의 WikiPage/`:Entity`(사람 이름) 노드가 고아로 남는다(수동 PG 삭제는
Neo4j를 건드리지 않는다).

- **`make node-kg-erase`가 처리(company wiki page)**: `wiki_pages`, `wiki_links`,
  `wiki_chunks`, `embeddings`(해당 page), `kg_entities`/`kg_entity_mentions`(K6 —
  identity-bearing) + node-local markdown + Neo4j 노드/엣지. 아래 KG 항목 참조.
- **운영자 수동(source-layer 등)**: `documents`, `corpus_chunks`, `notion_rows`,
  `query_runs`, `connector_items`, `connector_runs`, `promote_staging`.
- Auth: `auth_sessions`, `auth_identities`, `auth_allowlist` as appropriate.
- Logs/audit: legal/audit need가 있으면 row는 보존하되 secret/PII payload를 NULL/redact한다.
- **KG (Neo4j, K6):** 지워지는 company wiki page id 목록으로
  `make node-kg-erase NODE=company PAGES="<page_id> ..."`
  (= `python -m orthus.kg.erase`)를 실행한다. 이 명령이 page 단위 erasure를 결정론으로
  완결한다(구현 명세 §9.4): ① 지워진 page의 mention(`kg_entity_mentions`) 삭제 →
  ② surviving company mention이 0인 `kg_entities` row 삭제(person-entity
  orphan — `:Entity`는 이름 `entity_key` 키라 다른 page가 같은 이름을 mention하면
  노드가 잔존하므로 별도 처리) → ③ **wiki page SoR 삭제**
  (`wiki_pages`/`wiki_links`/`wiki_chunks`/`embeddings` row + markdown,
  `wiki_store.delete_item`) — 잔존 row가 다음 rebuild에 WikiPage 노드를 재생성하는
  것을 차단 → ④ 지워진 page는 `op='delete'` outbox 이벤트로 worker가 WikiPage 노드
  drop(준실시간, K6이 `op='delete'` 최초 호출처; rebuild도 SoR 부재로 prune) →
  ⑤ orphan `:Entity` 노드는 **즉시** Neo4j detach-delete(outbox `entity_kind`가
  entity 미지원, 가장 민감한 PII인 사람 이름은 지연 없이 제거). PG SoR 삭제(①②③)는
  KG 가용 여부와 무관하게 수행된다(privacy 보증의 권위).
  ⑥ **마지막 `make node-kg-rebuild NODE=company`는 필수다(PII 보증 단계).** ⑤의
  `:Entity` detach가 누락되는 창(erase 중 Neo4j 미가용, 또는 PG commit 직후 프로세스
  사망)에서는 orphan `:Entity`(사람 이름) 노드가 그래프에 잔존하고 **erase 재실행으로는
  복구되지 않는다**(mention이 이미 삭제돼 재감지 불가). 이 누락의 권위 복구는 rebuild
  뿐이다(prune이 `kg_entities` row 부재로 orphan `:Entity`를 detach). 같은 rebuild가 남은
  source-layer row(`documents`/`corpus_chunks`/`notion_rows`/`connector_*`/`query_runs` 등
  위 목록의 나머지, 운영자가 먼저 삭제) 정리도 최종 수렴시킨다. **rebuild 완료 + parity로
  orphan 부재를 확인하기 전에는 erasure를 완료로 보지 않는다.**

부분 삭제는 reconciliation note를 남긴다.

#### 8.4.1 Owner-scope KG erasure (K7.5)

owner-scope personal 데이터(개인 메모)의 KG 삭제는 page 단위가 아니라 **owner 단위**다.
`make node-kg-erase-owner NODE=company OWNER=<id>`(= `python -m orthus.kg.erase --owner`)가
`scope='personal' AND owner_id=<id>` 노드/엣지를 DETACH/DELETE한다. 배치(리오그/감원
오프보딩)는 `make node-kg-erase-owners NODE=company OWNERS_FILE=<f>`(단일 HOLD 윈도우 순차).

> **KG-only erase WITHOUT the PG step is FORBIDDEN as a forget action.** owner erasure는
> **그래프 표시(graph view)만** 제거한다 — 저장된 PG owner-row(`wiki_pages`/`kg_entities` 등
> owner-scope)는 **삭제하지 않으며**(그 삭제는 P8 owner-row delete 단계에 위임), 그래서 PG
> SoR이 남은 채 `make node-kg-rebuild`를 돌리면 owner 노드가 **다시 투영된다**(resurrection).

- **PG-first ONLY 완료 정의:** "완전 forget" = **owner-A PG row 선삭제 → KG rebuild/erase로
  `owner_footprint(A).node_count==0`(status=ready) 확인**. DETACH-먼저 stopgap 경로는 쓰지
  않는다. K7.5 단계에서 PG owner-row 삭제가 P8 위임이라 owner erasure는 구조적으로 항상
  **미완료**(`pg_copies_pending=true`)이며, `OwnerErasureReport`가 이를 정직하게 보고한다
  (kill-switch scaffold처럼 문서화된 의도적 미완료).
- **권한 = owner 본인 + content-blind admin.** admin은 `owner_id`만으로 실행하며 target에 대한
  `resolve_slug`/`neighbors`/`owner_footprint` **사전 content read를 하지 않는다**(제목/slug
  미열람). `audit("kg.erase.owner")`는 **counts-only**(actor + target_owner_id + 노드/엣지 수만,
  foreign slug/title/entity_key 금지).
- **owner-row resurrection gap:** DETACH는 P8 PG delete 전까지 **transient**다 — 그 사이 rebuild가
  owner 노드를 되살릴 수 있다. 그래서 완전 삭제는 **PG-delete-first 런북**(owner PG row 선삭제
  → rebuild/erase 수렴)을 따른다. CLI는 confirm 전 이 경고를 출력한다(`--confirm <owner-id>`
  정확 일치 필수, 비가역).
- **HR/직원 대상 정규 문구('확정 일자 없음'):**
  > 그래프 표시 제거 완료. 저장된 전체 기록의 완전 삭제는 이후 단계에서 진행되며 확정 일자는
  > 없습니다. / Graph-view suppression complete; full record deletion is pending in a later phase
  > with no committed date.
- **확인 hook:** 삭제 완료 시 별도 알림은 없다 — 이후 단계(PG delete) 적용 후 footprint 재확인
  시 0(status=ready)으로 표시되는 것이 검증 경로다(약속 날짜가 아니다).

> **node 래퍼 필수(SoR-mixing 방지):** `python -m orthus.kg.erase --owner`를 root `.env`로 직접
> 돌리면 dev DB를 가리켜 §2.1이 경고하는 SoR-mixing 위험이 생긴다. prod는 반드시
> `make node-kg-erase-owner NODE=company …`(node env 로드) 래퍼로 실행한다.

---

## 9. Audit / Observability

Primary audit API는 `orthus/audit/logger.py`의 `audit()` context manager다.

감사 대상:

- `/ask` route, router decision, structured compile/validate/execute.
- wiki distill/consolidate/retrieve/QA.
- connector ensure/config/sync/import command.
- promote export/stage/approve/reject.
- auth/allowlist mutation.
- external model/embedding/API calls.

운영 규칙:

- 각 span은 `correlation_id`를 전파한다.
- output/error payload는 저장 전 redaction한다.
- connector command는 누가 어떤 node/account/connector를 실행했는지 남긴다.
- public readiness claim에는 command output, URL, browser observation, CI/check 결과 중
  하나 이상의 직접 evidence가 있어야 한다.
- dashboard/alerting은 아직 canonical 산출물이 아니다. 없는 dashboard를 있다고 쓰지 않는다.

---

## 10. Error / Retry

일반 정책:

- network/5xx/timeout은 bounded retry 후 failed run으로 기록한다.
- validation, auth, policy, schema error는 retry하지 않고 reject/fail한다.
- connector sync 실패는 `connector_runs`에 error를 남기고 다음 due tick에서 재시도한다.
- structured query validation 실패는 실행하지 않고 `query_runs.rejected_reason`에 이유를 남긴다.
- scope rewrite 실패는 fail-closed reject(`scope_rewrite_failed:<ExceptionType>`)다.
- promote approve/import 실패는 staging 상태를 보존하고 운영자가 원인을 확인한다.
- 현재 운영 기준에 Redis DLQ, LangGraph checkpoint retry, persona drift rollback은 없다.

---

## 11. Time / Timezone

- DB 시간은 `TIMESTAMPTZ` UTC로 저장한다.
- source에서 timezone을 주면 source metadata에 보존한다.
- 사용자 표시 시간은 user/node preference timezone으로 변환한다.
- default human timezone은 `Asia/Seoul`이다.
- scheduler/due connector tick은 node-local clock 기준으로 실행하되, 저장 시간은 UTC다.
- relative date를 문서나 runbook에 남길 때는 가능하면 절대 날짜도 함께 적는다.

---

## 12. Verification

작업별 최소 검증:

| 변경 | 검증 |
|---|---|
| docs/spec | `make docs-check` |
| backend/auth/connector/promote | `make test` 또는 targeted pytest + ruff |
| frontend | `cd web && pnpm lint && pnpm build` |
| node boundary | `make node-smoke NODE=company`, `make node-smoke NODE=personal-a` |
| public edge/auth | GitHub Actions `S1 Public Smoke` manual/scheduled run, local `make s1-public-smoke BASE_URL=https://orthus-central.example.com`, browser QA |
| connector sync | `make node-sync NODE=<node> CONNECTOR=<slug>` 또는 due tick |

PR evidence:

- 실행한 command와 결과.
- 확인한 URL 또는 browser observation.
- 실패/스킵 사유.
- CI workflow maintenance는 GitHub runner deprecation annotation을 따라 action
  runtime을 갱신하고, PR CI와 main push CI 통과로 확인한다.
- production-facing/auth/boundary 변경은 owner 리뷰 결과.

### Node-Local Allowlist Break-Glass

정상 경로는 `/settings/access`에서 owner/admin이 allowlist를 관리하는 것이다.
단, public central에서 admin account가 bootstrap되지 않았거나 operator가
`not_invited` 복구를 해야 하면 Mac mini node-local env로 현재 node allowlist를
직접 upsert할 수 있다. 이 helper는 현재 node DB만 만지고 full email을 출력하지
않는다.

```bash
make node-auth-allowlist NODE=company ACTION=list
make node-auth-allowlist NODE=company ACTION=upsert EMAIL_FILE=/tmp/orthus-allowlist-email ROLE=admin
```

- `NODE=company`는 `~/.orthus/nodes/company/node.env`를 로드한다.
- `ACTION=upsert`는 기존 revoked row도 active로 되살린다. active owner/admin
  demote guard는 currently active row에만 적용된다.
- `ROLE` 기본값은 `member`다. 허용값은 `owner`, `admin`, `member`, `viewer`.
- shared logs에는 full email을 남기지 않는다. 가능하면 `EMAIL_FILE`을 쓰고, 파일은
  작업 후 삭제한다.
- revoke와 active owner/admin demote는 이 helper 범위 밖이다. lockout 위험이
  있으므로 web/API admin 경로를 쓴다.

---

## 13. Spec Drift 관리

- 최상위 계약은 내부 문서(비공개)다.
- `operations.md`는 운영 정책만 담고 제품 방향을 재정의하지 않는다.
- `architecture-v2.md`, `local-node.md`, `auth-and-caddy.md`, `p2-fe-and-sources.md`와
  충돌하면 PR에서 함께 수정한다.
- `architecture.md`와 `proposal.md`는 legacy reference다.
- 오래된 P1 용어(LangGraph, persona, KG, Redis DLQ, confidence routing)가 새 운영 기준처럼
  보이면 수정한다.
