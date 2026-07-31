# 실험 로그 — 그래프 사각지대 entity recall 규칙 앵커

> ⚠️ 공개 빌드 주의: 본문이 인용하는 experiments/fugu-ko 하위 golden/원자료 일부는 사내 데이터라 공개 레포에서 제거됐다(experiments/README.md 참조). 수치와 결론은 experiments/fugu-ko/RESULTS.md에 살균본으로 보존돼 있다.

> 작성 2026-07-15. 브랜치 `feat/graph-entity-anchor`.
> **결과 SoR:** `docs/routing-graph-follow-up.md` §8.7(적용 결과)·§8.8(shadow audit).
> **설계 배경:** 내부 문서(비공개)(핸드오프, 완료).
> 이 문서는 그 위 두 문서를 **재현 가능하게** 뒷받침하는 측정·로그·인사이트·트러블슈팅 노트다.

---

## 0. 한 줄 요약

`orthus/router/route.py::_GRAPH_TERMS`에 entity-mention 어미 앵커 **6개**를 추가해, "X가 어디에
나오나/다뤄지나" 류 그래프 질문이 LLM 판정에 의존하지 않고 **규칙만으로** graph 경로에
도달하게 했다. 모델 교체 아님 — 결정론 규칙(LLM 0회)으로 recall을 회복. solar classify
recall **4.8% → 100%**, 과포획(leak)은 **1.0% 그대로**, 실트래픽 shadow 과포획 **0/1,198**.

---

## 1. 측정 결과

### 1.1 기준선 (수정 전, `docs/routing-graph-follow-up.md` §8.1)

| 모델 | recall(63) | recall(essential 25) | leak/FP(290) | precision |
|---|---|---|---|---|
| solar (프로덕션 배정) | **4.8%** | **4.0%** | 1.0% | 50.0% |
| ax | 14.3% | 4.0% | 2.8% | 52.9% |
| exaone | 3.2% | 0.0% | 2.1% | 25.0% |
| gpt-4o-mini (현 prod, 벤더 금지) | 68.3% | 72.0% | 7.6% | 66.2% |

핵심: entity 사각지대는 **라우팅 모델이 병목**이다. GPT는 벤더 금지이고, 배정을 켜면
solar(5%)로 조용히 잘린다. → 내구적 경로는 LLM enum이 아니라 결정론 규칙 앵커.

### 1.2 규칙-only 측정 (수정된 `route.py` import, DB/LLM 불필요)

수정된 `_rule_based_route`를 직접 import해 골든 63 + holdout 330에 태움. 모델 무관.

| 지표 | 값 |
|---|---|
| entity 골든 rule→graph | **61/61** |
| essential 골든 rule→graph | **25/25** |
| holdout rule-leak (290 main + 40 control) | **0/330** |
| entity misses | 없음 |

### 1.3 엔드투엔드 measure (solar classify, 실 KG)

`experiments/fugu-ko/r1_graph.py --stage measure`. 프로덕션 solar 배정 + 실 Neo4j.

| 지표 | 수정 전 | **수정 후** |
|---|---|---|
| recall (전체 63) | 4.8% | **100.0% (63/63)** |
| recall (essential 25) | 4.0% | **100.0% (25/25)** |
| leak/FP (290) | 1.0% | **1.0% (3/290)** |
| precision @골든prev | 50.0% | **95.5%** |
| demote 안전(K4b) | — | **3/3 bind_miss, graph 오답 0** |
| probe 지연 | — | p50 746–825ms · p95 876–995ms* |

\* probe 지연은 run 간 자연 변동(같은 Neo4j probe의 표본 노이즈). recall/leak 헤드라인은 불변.

### 1.4 코드리뷰 반영 (앵커 8→6 축소 후 재측정)

리뷰에서 편익 0·검증 0인 `나온데가어디`/`나온데들이어디`(골든·holdout hit 0)를 제거.
축소 후 재측정 시 **모든 헤드라인 수치 불변**(1.2·1.3과 동일). 축소가 recall을 안 깎음을
확인(제거 앵커가 애초에 hit 0이므로 수학적으로 자명 + 실측 재확인).

### 1.5 실트래픽 shadow audit (§8.8)

company node 실 로깅 질문 distinct 1,198건에 OLD(6앵커 제외) vs NEW 규칙 비교.

| 지표 | 값 |
|---|---|
| 새 앵커 유발 graph flip(과포획) | **0 / 1,198 (0.00%)** |
| 앵커 어간 실트래픽 등장 | 전부 0건 |
| 질문 풀 구성 | query_runs 1,155 · ask_cache 19 · chat_user 26 |

한계: NL 원문은 structured 분기(`query_runs`)만 persist → structured→graph 과포획은 강하게
반증(1,155건 0 flip), wiki→graph는 저표본(all-route 45건). 상세 §8.8.

---

## 2. 채택한 앵커 6개 + 기각 근거

| 앵커 (compact) | 골든 hit | 군집 | 판정 |
|---|---|---|---|
| `어디어디에나오` | 24 | A | 채택 |
| `다뤄지는데가어디` / `다뤄지는데들이어디` | 16 / 3 | B (`+어디` 결합) | 채택 |
| `페이지들에서다뤄` | 14 | C | 채택 |
| `페이지나맥락에서` / `페이지또는맥락에서` | 3 / 1 | C | 채택 |
| `나오는페이지`/`등장하는페이지`/`페이지에서다뤄`/`맥락에서다뤄` | 0 | — | **기각**(hit 0 + wiki 과포획) |
| `나온데가어디`/`나온데들이어디` | 0/0 | — | **기각**(hit 0 + provenance는 `어디서나온`이 커버, 코드리뷰) |

**채택 기준 = 골든 실제 hit ≥ 1.** 편익 없이 과포획 표면만 넓히는 앵커는 배제.

---

## 3. 인사이트

1. **사각지대는 하나가 아니라 둘이다(entity ≠ relation/conflict).** entity는 **라우팅 모델**
   병목(어법이 검색과 구별 불가), relation/conflict는 **KG 데이터/resolver** 병목(page↔page
   지식 엣지 희소, resolvable 페이지 5개뿐). 같은 "graph 사각지대"지만 레버가 정반대다
   (§8.6). 이 작업은 entity만 손댔다.

2. **모델이 병목이면 규칙이 답이다.** 앵커가 `_rule_based_route` 단계에서 graph를 확정해 LLM
   classify **이전에 short-circuit**하므로 recall이 모델 무관이 된다(solar 4.8%→100%, gpt와
   동일). model-orchestration "국내 3모델 쌍대 무차이"는 structured/wiki 한정이고 graph엔
   성립하지 않았는데, 규칙 앵커가 그 모델 의존성 자체를 제거한다.

3. **"골든 hit ≥ 1"이 과포획 방어의 1차 원칙.** hit 0 앵커는 편익 0인데 과포획 표면만 넓힌다.
   초안의 `나온데가어디`류가 이 기준으로 코드리뷰에서 잘렸다. golden이 뽑아낸 어법만 앵커화.

4. **군집 C는 `+어디` 가드가 없는 "수용된 과포획"이다.** `페이지들에서다뤄`는 요약형 wiki
   질문("이 페이지들에서 다뤄진 내용 정리")을 graph로 끌 수 있다. 페이지 열거 의미라 준-정답 +
   bind 미스 시 wiki 안전 demote라 수용하되, 회귀 테스트로 동작을 못박았다
   (`test_group_c_page_anchor_accepted_overcapture_is_pinned`).

5. **K4b fail-open이 이 접근의 안전 근거다.** 과포획이 발생해도 bind/gate 미스 시 wiki로
   demote → **오답이 아니라 지연(probe ~750ms)만** 치른다. 그래서 "recall은 규칙으로 공격적
   회복, leak 리스크는 fail-open이 흡수"라는 비대칭 전략이 성립한다.

6. **다단어 앵커는 실트래픽에서 충돌이 거의 없다.** shadow에서 6앵커 어간이 1,198건에 0회
   등장 — SPECIFIC 다단어 원칙이 과포획을 구조적으로 억제함을 실증. 단, 이는 안전(no harm)
   증거이지 benefit 증거가 아니다(실트래픽에 target 어법 자체가 희소).

---

## 4. 실험 로그 (시간순, 재현용)

```bash
# 0) worktree
git worktree add -b feat/graph-entity-anchor .worktrees/graph-entity-anchor main
cp .env .worktrees/graph-entity-anchor/.env      # 실행용, stage 금지
cd .worktrees/graph-entity-anchor && uv sync --extra dev

# 1) 규칙-only 측정 (수정된 route.py import — DB/LLM 불필요)
python3 -c "
import sys, json; sys.path.insert(0,'.')
from orthus.router.route import _rule_based_route
g=json.load(open('experiments/fugu-ko/golden/routing_graph_golden.json'))
h=json.load(open('experiments/fugu-ko/golden/routing_holdout.json'))
ent=[x for x in g['main'] if x.get('material')=='entity']
ess=[x for x in g['main'] if x.get('essential')]
rg=lambda q:_rule_based_route(q)=='graph'
print('entity', sum(rg(x['q']) for x in ent), '/', len(ent))
print('essential', sum(rg(x['q']) for x in ess), '/', len(ess))
print('leak', sum(rg(x['q']) for x in h['main']+h['control']))
"

# 2) 회귀 테스트 (테스트 DB 격리 필수 — 트러블슈팅 §5.1)
bash scripts/setup_test_db.sh
ORTHUS_PG_DSN=postgresql+psycopg://orthus:orthus@localhost:5433/orthus_test \
ORTHUS_PG_DSN_READONLY=postgresql+psycopg://orthus_ro:orthus_ro@localhost:5433/orthus_test \
ORTHUS_EMBEDDING=mock ORTHUS_LLM=mock ORTHUS_MODEL_ORCHESTRATION_ENABLED=false \
uv run --active pytest tests/integration/test_router.py -q

# 3) 엔드투엔드 measure (실 Neo4j + solar — 트러블슈팅 §5.2/§5.3)
docker start orthus_neo4j            # 이미 존재하면 up -d 대신 start
export $(grep -E "^ORTHUS_KG_(ENABLED|URI|USER|PASSWORD)=" .env | xargs -d '\n')
cd experiments/fugu-ko
FUGU_DSN=postgresql://orthus:orthus@localhost:5433/orthus_company \
uv run --active python r1_graph.py --stage measure

# 4) shadow audit (실트래픽 오프라인 — 트러블슈팅 §5.4)
docker exec orthus_pg psql -U orthus -d orthus_company -tAc "
SELECT json_agg(q) FROM (
  SELECT DISTINCT nl_question q FROM query_runs WHERE nl_question IS NOT NULL
  UNION SELECT DISTINCT question_redacted FROM ask_cache WHERE question_redacted IS NOT NULL
  UNION SELECT DISTINCT text FROM agent_chat_messages WHERE role='user'
) s;" > /tmp/traffic.json
# → OLD(6앵커 제외) vs NEW _rule_based_route 비교, flip 카운트 (본문 §1.5)
```

---

## 5. 트러블슈팅 (겪은 문제 → 원인 → 해결)

### 5.1 pytest가 작업 DB를 가리켜 수집 단계 중단
- **증상:** `pytest.UsageError: pytest가 테스트 DB가 아닌 'orthus'를 가리키고 있습니다.`
- **원인:** `tests/conftest.py`의 fail-closed 가드 — fixture가 데이터 테이블을 DELETE하므로
  DB 이름에 `test`가 없으면 수집 단계에서 거부(2026-07 두 차례 실사고 방지).
- **해결:** `bash scripts/setup_test_db.sh`로 `orthus_test` 준비 후, `ORTHUS_PG_DSN=...orthus_test`
  명시 env로 실행(또는 `make test`). 순수 함수 테스트라도 conftest가 먼저 걸리므로 우회 불가.

### 5.2 neo4j 컨테이너 이름 충돌
- **증상:** `docker compose up -d neo4j` → `Conflict. The container name "/orthus_neo4j" is
  already in use`.
- **원인:** 중지된 `orthus_neo4j` 컨테이너가 이미 존재(메인 worktree compose가 만든 것).
  worktree 디렉터리명이 compose project prefix에 들어가 새 볼륨/컨테이너를 만들려다 이름 충돌.
- **해결:** 새로 만들지 말고 기존 것을 `docker start orthus_neo4j`. KG 데이터(139 Entity,
  10,803 WikiPage)가 persist 볼륨에 이미 있어 rebuild 불필요했다.

### 5.3 measure가 "KG off/미가용"으로 즉시 종료
- **증상:** `r1_graph.py --stage measure` 첫 줄에 `KG off/미가용.` 후 SystemExit. 하지만
  repo 루트에서 `kg_available()`를 직접 호출하면 `True`.
- **원인:** measure는 `experiments/fugu-ko` **cwd**에서 도는데, 그 위치에서 settings 로더가
  루트 `.env`(특히 `ORTHUS_KG_PASSWORD`)를 못 읽어 Neo4j 인증 실패 → `kg_available()` False.
  `ORTHUS_KG_ENABLED=true`만 넘기고 password를 안 넘긴 게 함정.
- **해결:** 실행 전 KG env를 명시 export —
  `export $(grep -E "^ORTHUS_KG_(ENABLED|URI|USER|PASSWORD)=" .env | xargs -d '\n')`.

### 5.4 실트래픽 질문 원문 소스 부재
- **증상:** shadow audit용 "전 경로 실질문"을 구하려는데 audit_log(449,926행)의
  `router.classify` span meta에 `{"route": "wiki"}`만 있고 **질문 텍스트가 없음**.
- **원인:** PII 회피 설계 — classify span은 route 라벨만 기록. NL 원문이 persist되는 정규
  컬럼은 `query_runs.nl_question`(redacted) **하나뿐**이고, 그것도 **structured 분기만**.
  wiki/graph로 간 질문 텍스트는 어디에도 안 남는다.
- **해결/한계:** `query_runs` + `ask_cache.question_redacted` + `agent_chat_messages`(user)를
  합쳐 distinct 1,198건 확보. structured 편향은 §8.8에 한계로 명시. 완전한 wiki-population
  검증은 `router.classify`에 redacted question을 add_meta하는 **계측(코드 변경)** 후 실트래픽
  수집이 선행돼야 한다(이번 범위 밖).

### 5.5 uv VIRTUAL_ENV 경고
- **증상:** `warning: VIRTUAL_ENV=... does not match the project environment path`.
- **원인:** worktree의 `.venv`와 활성 VIRTUAL_ENV 불일치.
- **해결:** `uv run --active`로 활성 env 타깃(무해한 경고지만 명령에 `--active` 부착).

### 5.6 measure 산출물이 커밋에 섞일 위험
- **증상:** measure 실행 후 `experiments/fugu-ko/analysis/r1/graph_measure.json`(160KB) untracked
  생성. `.gitignore` 미적용.
- **해결:** `git add`에 **경로 명시**(`git add -A` 금지). 산출물은 커밋 제외.

---

## 6. 후속 (활성화 후 모니터링)

배정(`ORTHUS_MODEL_ORCHESTRATION_ENABLED`) 활성화 자체는 이 작업의 선결 완화가 끝나 진행 가능.
활성화 후 `router.graph.bind` / `kg.retrieve` audit span의 **demote율**을 모니터링하면 wiki
저표본 리스크를 실운영에서 커버한다(과포획은 fail-open이라 오답이 아닌 지연으로만 노출).
완전한 wiki-population shadow가 필요하면 §5.4의 계측 PR을 별도로 낸다.
