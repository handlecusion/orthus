# 라우팅 후속 — graph 사각지대 측정 (다음 세션용)

> ⚠️ 공개 빌드 주의: 본문이 인용하는 experiments/fugu-ko 하위 golden/원자료 일부는 사내 데이터라 공개 레포에서 제거됐다(experiments/README.md 참조). 수치와 결론은 experiments/fugu-ko/RESULTS.md에 살균본으로 보존돼 있다.

> 상태: **측정 완료 (2026-07-15)**. 결과·판정은 아래 **§8**. §1~§4는 원래 인수인계(설계)이고 §8이 실측 결론이다.
> 선행: `docs/routing-holdout-plan.md`(§13 홀드아웃 결과 + §13.5b E2 재측정), PR #718/#719(머지됨).

---

## 0. 지금까지 (배포 완료)

C2(사각지대 wiki 기본값 + 수량 규칙)와 C3-a(structured 게이트거부 → wiki 회수)가
`orthus/router/route.py`·`orthus/router/__init__.py`에 들어가 **main에 머지됐다**(PR #719).

- `classify()`/`classify_intent()`는 공유 헬퍼 `_quantity_route`/`_apply_wiki_default`로
  단일화됨. 사각지대에서 LLM의 structured/wiki 판정을 wiki로 강제하되 **`graph`는 살린다**.
- fail-closed revert switch: `ORTHUS_ROUTING_WIKI_DEFAULT`,
  `ORTHUS_ROUTING_GATE_REJECT_WIKI_RECOVER`(둘 다 default True).
- 검증: 프로덕션 `classify()`가 홀드아웃 골든셋 290문항에서 **90.7%** 재현.
  E2 루프 재측정에서 A3(신규 침묵 누락) 게이트 3/3 통과.

---

## 1. ★ 남은 최우선 빈틈 — **graph 라우팅이 shipped인데 측정된 적이 없다**

홀드아웃 290문항은 **structured vs wiki만** 쟀다(라벨 분포 222:68, graph 0건).
그런데 `_apply_wiki_default`는 "graph는 살린다"로 **LLM의 graph 판정을 그대로 신뢰**한다:

```python
def _apply_wiki_default(route: Route) -> Route:
    if get_settings().routing_wiki_default and route != "graph":
        return "wiki"
    return route
```

즉 **사각지대에서 LLM이 `graph`라고 하면 그대로 graph로 간다 — 그 판정의 정밀도를 잰 적이 없다.**
structured/wiki는 "LLM 못 믿음 → wiki 강제"로 처리했는데 graph만 예외로 통과시켰고, 그
예외의 근거는 "홀드아웃이 graph를 안 쟀으니 죽이지 말자"라는 **소극적 판단**이었다.

**측정해야 할 질문:**
- 사각지대에서 LLM `graph` 판정의 정밀도(precision)는? = graph로 보낸 것 중 진짜 관계/모순/
  provenance 질문의 비율. 낮으면 wiki/structured 질문이 graph로 새서 bind+Neo4j probe 비용만
  치르고 demote된다(K4b fail-open이라 답은 안 틀리지만 지연·비용).
- 재현율(recall)은? = 진짜 graph 질문 중 사각지대에서 graph에 도달하는 비율. `_GRAPH_TERMS`
  규칙 앵커가 없는 NL conflict/관계 질문(K8/K9)이 여기 의존한다.

---

## 2. 부차 빈틈 — 290 라벨 사람 표본 감사

주 셋 290 라벨은 **자동 생성 + 자동 양방향 검증**만 거쳤다. TN 40건은 손으로 전수 확인해서
탐지기 오판 1건을 잡았지만(§13.2), **주 셋 라벨은 눈으로 안 봤다.** 이 캠페인의 반복 교훈이
"검증기가 틀린다"이므로, 결론(90.7%)을 떠받치는 라벨 30~50건 표본 감사는 값싼 보강이다.
graph 골든을 만드는 김에 같이 하면 좋다.

---

## 3. 하네스 위치 + 실행법

측정 코드는 **작업 체크아웃의 solar-adapter 워크트리**에 있다(main에 미포함 — 실험 하네스):

```
.worktrees/solar-adapter/experiments/fugu-ko/
  r1_gen.py       # 골든셋 생성기 (복합질문 → 프로덕션 decompose → 리프 → 양방향 검증)
  r1_routing.py   # 측정 하네스 (C1/C2/C3 + McNemar + 게이트 판정)
  r1_tn.py        # TN 버킷 (정답='없음', SQL로 0행 확인)
  golden/routing_holdout.json      # 확정 290 + 대조군 40 (structured/wiki만)
  golden/routing_holdout_tn.json   # TN 40
  pool.py         # 국내 3모델 + baseline(gpt) WorkerChat 풀
  e2_e2e.py       # anchor_hit (앵커 판정 SoR) — r1이 import
```

> ⚠️ 이 파일들 중 일부는 공개 빌드에 포함되지 않았다(내부 실험 브랜치 산출물).

**env 준비**(company node + 국내 3사 키; 키는 Windows 파일에서 로드):

```bash
# /tmp/e2ax_env.sh 형태 — solar/ax/exaone 키를 keys.json에서 export,
# ORTHUS_MODEL_ORCHESTRATION_ENABLED=true, FUGU_DSN=orthus_company, scope=company(I6).
# 상세는 solar-adapter 세션 로그 참조. keys.json:
#   <로컬 키 저장소>/keys.json
```

**기존 홀드아웃 재현**(참고):

```bash
python r1_routing.py --stage c1c2   # 4모델 × 규칙2종
python r1_routing.py --stage c3     # C3 회수 + TN 환각
python r1_routing.py --stage report # A1~A4 판정
```

---

## 4. graph 측정을 어떻게 지을까 (설계 스케치)

기존 홀드아웃 방법론(구성으로 라벨 + 양방향 검증)을 graph 평면으로 확장한다:

1. **graph-answerable 골든 생성** — company `kg_entities`/관계에서 실재하는 관계/모순 쌍을
   뽑아, 그 관계가 답이 되는 NL 질문을 만든다(`_GRAPH_TERMS` 앵커 **없이** — 규칙이 잡으면
   사각지대가 아니다). 라벨 = graph.
   - 재료: `orthus/kg/templates.py`의 `entity_neighbors`/`page_conflicts`/`entity_mentions`가
     답하는 것들. K8(conflict) 실 6건, K9(entity) 소스가 이미 있다(`docs/kg-model.md`,
     KG 이력은 `AGENTS.md` K4b–K9 참조).
2. **양방향 검증** — 각 질문을 graph 평면(`try_graph_answer`)과 wiki 평면 양쪽에 태워:
   - graph만 답한다 → 라벨 graph 확정
   - wiki도 답한다(같은 관계를 산문으로) → "graph 필수 아님" → 제외 or 별도 버킷
   - I6: 전 경로 `scope="company"`(personal 합성 페이지 24,673개 오염 방지).
3. **부정 대조** — structured/wiki-answerable 사각지대 질문에서 LLM이 graph로 **오판**하는
   비율(precision의 분모). 기존 routing_holdout.json 290문항을 `try_graph_answer`에 태워
   몇 개가 graph 후보로 새는지 세면 된다.
4. **지표** — graph precision(=graph 보낸 것 중 진짜 graph), graph recall(=진짜 graph 중
   도달), demote 비용(bind+probe 후 wiki로 떨어진 횟수/지연).
5. **판정** — `_apply_wiki_default`의 `route != "graph"` 예외가 정당한가:
   - precision 높음 → 유지(현행). 근거 생김.
   - precision 낮음(대량 오판) → graph도 규칙 앵커(`_GRAPH_TERMS`) 있을 때만 살리고 LLM
     graph는 죽이는 쪽으로 좁힌다.

---

## 5. 코드리뷰에서 조치 안 한 findings (graph 측정과 무관, 참고)

`/code-review` high에서 나왔으나 저위험/설계 감수로 남긴 것:

- **#5 (correctness, 낮음-중간)** — `_QUANTITY_TERMS` 오탐이 structured로 갔다가 **0행**이면
  C3-a가 회수 안 한다(C3-a는 게이트거부만 회수, 0행 제외). 위키 질문이 조용한 빈 답으로
  사라질 수 있다. 실트래픽에서 얼마나 잦은지 미측정.
- **#7 (correctness, 낮음)** — C3-a가 `span.add_meta(mode="wiki")`로 재기록해 같은
  router.answer span에 mode가 두 번 찍힌다(structured→wiki). served-mode 대시보드가 왜곡될
  수 있다. audit add_meta 병합 의미 확인 필요.

이 둘은 graph 측정과 독립이며, 실트래픽 shadow(§6)에서 자연스럽게 드러난다.

---

## 6. 그 다음 (지금은 안 함 — 데이터 필요)

- **C2 실트래픽 shadow** — "구조화 질문 16%를 wiki로 잃는다"(M2)가 실제 `/ask` 분포에서
  얼마나 아픈지는 구성 골든이 아니라 실트래픽 replay로만 안다. prod audit `router.answer`
  span의 mode 분포 + 사용자 재질의율로 근사 가능.

## 7. 안 하는 것 (이미 결정 / 수익 체감)

- 배정 활성화 = 운영 결정(node.env), 근거 측정 완료.
- 모델 스왑(C1) = 기각(쌍대 무차이, `docs/model-orchestration.md` §11). **단 이 "쌍대 무차이"는 structured/wiki 라우팅 한정이다 — graph 라우팅에는 성립하지 않는다(§8.2).**
- E2 N 확대로 p=0.508 추격 = 십중팔구 "차이 없음" 재확인.

---

## 8. ★ 실측 결과 (2026-07-15)

하네스 `experiments/fugu-ko/r1_graph.py`(solar-adapter 워크트리, 미머지 실험 코드).
company node + KG on(neo4j: Entity 139 · RELATES_TO 337 · CONFLICTS_WITH 20 ·
MENTIONED_IN 789) + 국내 3사 키 + baseline(gpt-4o-mini). I6 전 경로 `scope="company"`.

### 8.0 방법 (구성 + 검증, r1_gen과 동형)

1. **graph 골든** — 실재 KG 엣지에서 뽑은 named subject(엔티티/공동언급쌍/모순토픽)로 NL
   질문 생성, **`_GRAPH_TERMS` 앵커 금지 → 사각지대 강제**(`_rule_based_route==None`).
   graph-answerable 판정 오라클 = `try_graph_answer`가 실제 graph 답 + 기대 연결(상대 노드/
   페이지)을 노출하는가. 후보 99 → 검증 후 **골든 63**(essential 25 = graph만 노출·wiki 실패,
   optional 38 = wiki도 노출/약graph, unanswerable 36 제외).
   - ⚠️ **범위 한계:** 골든이 **entity intent 편중**(entity 61 / relation 2 / conflict 0).
     relation(path_between)·conflict(page_conflicts) 재료는 대부분 bind/grounding 실패로
     탈락했다. 즉 이 측정은 실질적으로 **K9 entity-mention 분기**를 잰다. entity 분기가 규칙
     앵커가 가장 specific해 LLM enum 의존이 가장 큰 곳이라 타깃은 맞지만, LLM 경유 conflict/
     relation graph recall은 **미측정**으로 남는다.
2. **부정 대조** — 기존 290 non-graph 홀드아웃(structured/wiki)을 `classify()`에 태워 graph로
   새는 비율(leak=FP) + 새면 `try_graph_answer`로 demote 지연·오답위험.

### 8.1 지표 — LLM `graph` 판정의 재현율/정밀도 (전 문항 사각지대)

| 모델 | recall(전체 63) | recall(essential 25) | leak/FP(290) | precision | F1 |
|---|---|---|---|---|---|
| **solar (프로덕션 배정)** | **4.8%** | **4.0%** | 1.0% | 50.0% | 8.7% |
| ax | 14.3% | 4.0% | 2.8% | 52.9% | 22.5% |
| exaone | 3.2% | 0.0% | 2.1% | 25.0% | 5.6% |
| baseline (gpt-4o-mini) | **68.3%** | **72.0%** | 7.6% | 66.2% | 67.2% |

precision은 골든:non-graph = 63:290 혼합 기준(합성 prevalence — 실트래픽 아님). 1차 지표는
**recall + FP-rate**이고 precision은 참고다.

> **⚠️ §8.6 필독:** 위 표는 **entity intent** 측정이다. relation/conflict를 따로 재면 그림이
> **정반대**다(classify recall ~100%, 병목은 KG 데이터). §8.2 발견 1은 entity 한정이다.

### 8.2 발견 (entity intent)

1. **graph precision은 문제가 아니다 — (entity에선) recall이 문제다.** 프로덕션 배정 solar는
   진짜 entity graph 질문(사각지대)의 **4.8%**만 graph로 보낸다. 63문항 중 48을 wiki, 12를
   structured로 보낸다. entity-mention 질문("X는 어디에 나오나")은 검색과 언어적으로 구별
   불가라 LLM이 wiki로 본다. **단 이건 entity 특유다** — relation/conflict는 §8.6처럼 다르다.
2. **모델 의존이 지배적이다.** baseline(gpt-4o-mini) recall 68% vs solar 5% — **14배**. 라우팅
   배정을 solar로 옮기며 사각지대 graph recall이 68%→5%로 조용히 잘렸다. `docs/model-orchestration.md`
   §11의 "국내 3모델 쌍대 무차이"는 **structured/wiki 한정**이고 **graph에는 성립하지 않는다**
   (McNemar 불요 — recall 4.8% vs 68.3%는 압도적). 국내 모델 중 최선도 ax 14%로 회복 불가.
3. **`route != "graph"` 예외는 solar에서 거의 무동작(inert)이다.** 353문항 중 graph로 간 건
   6건뿐(golden 3 + leak 3). 예외가 살리는 recall이 5%라 **의미 있게 돕지 않고**, leak 1%가 전부
   안전 demote라 **의미 있게 해치지도 않는다.**
4. **demote는 안전하고 값이 크지 않다(K4b fail-open 실증).** leak 3/3 전부 안전 demote
   (`bind_miss` 2·`gate_reject:timeout` 1) → **오답 0건**. probe 지연 p50 2.6s·p95 5.2s. non-graph가
   graph 답을 받는 품질사고는 0건.

### 8.3 판정 — §4.5 질문에 대한 답

**예외는 유지(keep)하되, 그것은 레버가 아니다.**

- §4.5의 "precision 낮으면 규칙 앵커만 살리고 LLM graph 죽인다" 분기는 **발동 안 한다** —
  leak이 1%로 낮아 precision 손실이 없다. 예외를 좁힐 이유가 없다(비용 ~0).
- 그러나 예외가 지키려던 recall이 solar에서 5%다. **진짜 병목은 예외가 아니라 분류기 모델**이고,
  국내 모델로는 회복이 안 된다(GPT 벤더 금지 → gpt-4o-mini 68% 불가). 따라서 사각지대 graph
  recall의 **내구적 경로는 LLM enum이 아니라 결정론 규칙 앵커**(`_GRAPH_TERMS` 확장)다 — 이는
  graph를 규칙-우선으로 두고 LLM enum을 약한 보조로 둔 현행 설계와 정합이다.
- **손실은 부분적으로 bounded다.** 골든 63 중 38(60%)이 graph-optional(wiki도 답함) — entity
  질문("X는 어디에 나오나")은 검색과 언어적으로 구별 불가라 wiki 라우팅이 흔히 "틀린" 게 아니다.
  진짜 손실은 essential 25건뿐이고, 사각지대 리프 전체(≈353)의 ~7%다.

**후속 후보(이번엔 안 함):** entity-mention 패턴 규칙 앵커("어디어디에 나오"/"다뤄지는 데"/
"나오는 곳") 추가 → 모델 무관 결정론 recall. 리스크는 검색형 wiki 질문 과포획이나 demote가
fail-open(오답 0)이고 probe ~2.6s라 downside가 bounded다. 채택 전 자연 트래픽 shadow(§6)로
과포획 빈도 확인 권장.

### 8.4 §2 라벨 표본 감사 (290 중 40건)

대체로 타당하나 잡음 바닥 ~5%:
- **"지부장 직책을 가진 직원 항목은 어느 정도인가요?"** — number 앵커 [7]인데 라벨 **wiki**.
  "어느 정도" 집계 질문이라 **structured가 맞아 보인다**(코드리뷰 #5 수량어 오탐과 정합).
- yes/no 질문에 number 앵커가 붙은 케이스 1건(앵커-질문 불일치).
- '관리자용'/'출연자용' 같은 **흔한 역할값 앵커**가 서로 다른 6문항에 재등장 → 무관한 wiki 답도
  우연히 앵커 hit할 위험(false-positive 앵커).

90.7% 헤드라인을 뒤집진 않지만 "검증기가 틀린다"는 캠페인 교훈을 재확인한다. 산출물:
`experiments/fugu-ko/analysis/r1/label_audit_sample.json`.

### 8.5 산출물

```
experiments/fugu-ko/r1_graph.py                       # 하네스 (material/gen/verify/measure/audit)
experiments/fugu-ko/golden/routing_graph_golden.json  # 골든 63 (essential flag 포함)
experiments/fugu-ko/analysis/r1/graph_verified.jsonl  # 양방향 검증 원본
experiments/fugu-ko/analysis/r1/graph_measure.json    # recall/leak/demote 원본
experiments/fugu-ko/analysis/r1/graph_leak_allmodels.json  # 4모델 leak
experiments/fugu-ko/analysis/r1/label_audit_sample.json    # §2 표본 40
experiments/fugu-ko/analysis/r1/relconf_measure.json       # §8.6 relation/conflict 보강
```

### 8.6 relation/conflict 보강 측정 — 범위 한계 해소 (entity ≠ 나머지)

§8.1 골든이 entity 편중이라 relation(path_between)·conflict(page_conflicts)를 따로 쟀다.
먼저 **왜 그 둘이 자동 골든에서 탈락했는지** 진단했다:

- **재료 자체가 코퍼스에 희소하다.** path_between이 요구하는 **의미 있는 page↔page 지식
  엣지가 거의 없다**(대부분 "Color" 같은 BACKLINK 허브 경유 — 지식 연결 아님). conflict는
  CONFLICTS_WITH 20엣지 중 resolvable 실제 페이지가 **`new-hire` 하나뿐**이고 나머지는
  projection 아티팩트(클레임 텍스트가 slug가 된 노드)다.
- **resolver가 엄격하다.** `_resolve_subject`는 exact-slug / 유니크 exact-title /
  유니크 title-prefix만 resolve한다. 페이지 제목("New Hire")은 중복이라 ambiguous→None.
  실제 resolve되는 relation 주어는 **name==slug 페이지 5개**뿐(nova-server/nova-app/
  novalang/chatterbox-turbo/clawra).

즉 **entity 편중은 샘플링 편향이 아니라 코퍼스의 성질이다** — entity_mentions만이 풍부·
bindable한 NL graph 재료를 갖는다.

그 5개 페이지쌍(relation 10) + `new-hire`(conflict 3)로 재보니 **entity와 정반대**다:

| intent | classify recall (LLM→graph) | graph grounded (KG가 답) | 병목 |
|---|---|---|---|
| entity (K9) | **~5%** (solar) | 높음(entity_mentions 풍부) | **classify** (검색과 구별불가) |
| relation (path_between) | **100%** (10/10) | 30% (3/10) | **KG 데이터+resolver** (page↔page 경로 희소) |
| conflict (page_conflicts) | **100%** (3/3) | 0% (0/3) | **KG 데이터+resolver** (resolvable 페이지 1개) |

**발견:** solar classify는 relation/conflict **어법**("A랑 B는 어디서 만나나", "X에 대해 안 맞는
얘기 있나")은 관계 신호가 있어 **정확히 graph로 보낸다**(recall 100%). entity 저조(5%)는
어법 특유(검색처럼 보임)일 뿐 graph 전반의 결함이 아니다. 그러나 relation/conflict는 **KG가
못 받친다** — path가 희소해 framings_demote(7/10), conflict 클레임이 page_conflicts로 안
붙어 bind_miss(3/3). 전부 wiki로 **안전 demote**(K4b, 오답 0).

**§8.3 판정에 대한 함의(강화):** `route != "graph"` 예외는 relation/conflict에서 **제 일을
한다** — classify가 graph라 하면 예외가 살리고, 그다음 graph 평면이 데이터 부족으로 안전
demote한다. 예외를 죽이면 이 candidate들을 잃지만, 지금은 어차피 grounding이 안 돼 실익이
작다. **어느 intent에서도 예외가 병목이 아니다** — 병목은 (entity) classify 어법 모호성 +
모델, (relation/conflict) KG page↔page/conflict 밀도 + resolver 엄격도다. 예외는 유지.

### 8.7 entity 규칙 앵커 적용 결과 (2026-07-15, 구현 완료)

§8.2 발견 1·2 + §8.3의 "후속 후보"(entity-mention 규칙 앵커)를 구현했다. §7 핸드오프
(내부 문서(비공개)) 기준 **entity 앵커만** 손댔다(relation/conflict·KG
무변경). `orthus/router/route.py::_GRAPH_TERMS`에 골든 3개 어미 군집을 결정론 앵커 6개로
추가했다:

| 앵커 (compact) | 골든 hit | 군집 |
|---|---|---|
| `어디어디에나오` | 24 | A ("어디어디에 나오나요") |
| `다뤄지는데가어디` / `다뤄지는데들이어디` | 16 / 3 | B ("다뤄지는 데(들)가 어디") |
| `페이지들에서다뤄` | 14 | C ("어떤 페이지들에서 다뤄지나요") |
| `페이지나맥락에서` / `페이지또는맥락에서` | 3 / 1 | C ("어떤/어느 페이지·맥락에서 다뤄지") |

SPECIFIC 다단어 원칙 유지: 단독 `나오`/`다뤄`/`어디`와 연결어미 `-는데`는 금지. **채택
기준은 "골든 실제 hit ≥ 1"** — 편익 없이 과포획 표면만 넓히는 앵커는 배제한다. 기각 후보:
`나오는페이지`/`등장하는페이지`/`페이지에서다뤄`/`맥락에서다뤄`는 골든 기여 0 + 일반 wiki
질문("결과가 잘 나오는 페이지 설정", "온보딩 페이지에서 다뤄지는 내용 요약") 과포획으로,
초안에 있던 `나온데가어디`/`나온데들이어디`(전방 대비형)는 골든·holdout 양쪽 hit 0(측정
근거 없음) + provenance는 이미 `어디서나온` 앵커가 커버하므로 코드리뷰에서 제외했다.
군집 C(`페이지들에서다뤄` 계열)는 `+어디` 가드가 없어 요약형 wiki 질문을 graph로 끄는
**수용된 과포획**이 있고(bind 미스 → wiki 안전 demote), 이 동작은
`test_group_c_page_anchor_accepted_overcapture_is_pinned`로 고정했다.

**측정 (수정 전/후, 전 문항 사각지대 모집단 불변):**

| 지표 | solar 수정 전 (§8.1) | **solar 수정 후** | gpt-4o-mini (참고) |
|---|---|---|---|
| recall (전체 63) | 4.8% | **100.0% (63/63)** | 68.3% |
| recall (essential 25) | 4.0% | **100.0% (25/25)** | 72.0% |
| leak/FP (290) | 1.0% | **1.0% (3/290)** | 7.6% |
| precision @골든prev | 50.0% | **95.5%** | 66.2% |
| demote 안전(K4b) | — | **3/3 bind_miss, 오답 0** | — |
| probe 지연 | — | p50 746ms · p95 876ms | — |

- **핵심: recall이 모델 무관이 됐다.** 앵커가 `_rule_based_route` 단계에서 graph를 확정해
  LLM classify 이전에 short-circuit하므로, solar(4.8%→100%)든 gpt(68%)든 동일하게 잡는다.
  이로써 §8.2 발견 2("모델 의존 지배적, 배정 켜면 5%로 조용히 잘림")가 해소된다 —
  model-orchestration 배정 활성화의 선결 완화책.
- **leak 증가 0.** 규칙-only 측정(수정된 `route.py` import, 골든 63 + holdout 330)에서
  새 앵커의 rule-leak은 **0/330**이다. classify() 엔드투엔드 leak 3/290(=1.0%)은 전부 앵커
  미매칭 홀드아웃에 대한 기존 solar LLM enum 판정으로, 이번 변경과 무관하게 §8.1과 동일하다.
- **재현:** 규칙-only는 `route.py`의 `_rule_based_route`를 직접 import; 엔드투엔드는
  `experiments/fugu-ko` cwd에서 `ORTHUS_KG_ENABLED=true` + KG env export + company DB로
  `python r1_graph.py --stage measure`(solar). 골든/holdout 라벨셋은 frozen(재생성 없음).
- **회귀:** `tests/integration/test_router.py::test_entity_mention_ending_anchors_route_without_overcapture`
  신설(positive 7 + negative 5) + 기존 `test_existing_routes_no_regression` fixture-disjoint 통과.

**후속(이번 범위 밖, 활성화 전 권장):** 자연 트래픽 shadow audit — 아래 §8.8에서 수행.

### 8.8 자연 트래픽 shadow audit (2026-07-15)

골든/holdout은 frozen 합성셋이라 실트래픽 과포획을 직접 재지 못한다. company node의 실제
로깅 질문을 오프라인으로 뽑아 **OLD(신규 6앵커 제외) vs NEW `_rule_based_route`**를 비교했다.

**질문 풀 (distinct 1,198):** `query_runs.nl_question`(1,155, PII-redacted), `ask_cache.question_redacted`(19),
`agent_chat_messages`(role=user, 26). NL 질문 원문이 persist되는 곳은 이 셋뿐이다(audit_log
`router.classify` span은 route 라벨만, 질문 텍스트 없음).

**결과:**

| 지표 | 값 |
|---|---|
| 새 앵커가 유발한 graph flip (과포획) | **0 / 1,198 (0.00%)** |
| 앵커 어간(`다뤄지는`/`다뤄`/`어디어디`/`페이지들에서`/`맥락에서`/`나온데`) 실트래픽 등장 | **전부 0건** |
| entity-mention 유사 실질문(`언급` 포함, 10건)의 라우팅 | 전부 wiki 유지(정상) |

- **과포획 0.** 6개 앵커의 어간이 실트래픽 1,198건에 **한 번도 등장하지 않는다** — 다단어
  결합형이라 실사용 structured/wiki 어법과 충돌하지 않음을 실증한다. golden/holdout leak 0이
  실트래픽에도 성립한다.
- **`언급된 X는` 류는 안전하다.** 실트래픽의 entity-유사 질문("문서에서 언급된 다음 배포일은?")은
  전부 특정 문서 내 조회형 wiki이고, cross-page 발견 앵커("언급한페이지" 등 K9.3a)와도 신규
  앵커와도 안 겹쳐 wiki로 정상 라우팅된다.

**⚠️ 한계(정직 고지):** NL 질문 원문은 **structured로 라우팅된 것만** `query_runs`에 남는다
(wiki/graph 질문 텍스트는 어디에도 persist 안 됨). 따라서 풀 1,198 중 1,155가 structured 편향이고,
all-route 표본은 45건(ask_cache 19 + chat 26)뿐이다. 즉 이 audit는 **structured→graph 과포획을
강하게 반증**하지만(1,155건 0 flip) **wiki→graph 과포획은 저표본**이다. 완전한 wiki-population
검증이 필요하면 `router.classify` span에 redacted question을 add_meta하도록 계측한 뒤(코드 변경)
실트래픽을 수집하는 진짜 프로덕션 shadow가 선행돼야 한다 — 본 오프라인 audit의 범위 밖이다.

**판정:** 사용 가능한 실트래픽에서 과포획 0. structured 대량 표본이 clean하고, 남은 리스크
(wiki 저표본)는 어차피 K4b fail-open으로 오답이 아닌 지연뿐이라, **배정 활성화 진행을 막을
근거는 없다.** 활성화 후 `router.graph.bind`/`kg.retrieve` span의 demote율을 모니터링하면 충분.
