# D9 저작 검증 노트 — +55 blind authoring (AUTHORING+FREEZE 단계)

> 작성 2026-07-23 · `analysis/d9-extension-prereg.md`의 AUTHORING+FREEZE 단계 산출물.
> **모델 호출 0회** — 본 단계에서 어떤 chat-LLM API도 호출하지 않았고, DB 접근은
> `orthus_company_0706` read-only(t3 gold 재계산)뿐이다. 벤치마크 arm 실행은 후속 단계.

## 1. 산출물

| 파일 | 내용 |
|---|---|
| `golden/t3_d9ext.json` … `golden/t10_d9ext.json` (6개) | 신규 확장 골든 — 기존 골든 파일은 **무변경** |
| `e2e/build_manifest.py` | ext 로더 + d9 gold(0706 DSN) + 1001+ id band + **frozen-line 바이트 보존 replay** |
| `e2e/inventory.json` | Tier-A asset 6종 추가 (11→17), item_count 합 334 |
| `e2e/tier_a.jsonl` | 279 → 334 (git diff: **55 insertions, 0 deletions, 0 modifications**) |
| `e2e/freeze.lock` | tier_a 섹션만 재동결 (아래 §6) |

## 2. 문항 수 — 사전선언 §2 표와 정확히 일치

| 작업 | 기존 | 신규 | 합계 | 신규 id band |
|---|---|---|---|---|
| t3 structured | 28 | +11 | 39 | `A-t3-1001`–`1011` |
| t5 routing | 21 | +8 | 29 | `A-t5-1001`–`1008` |
| t6 intent | 20 | +8 | 28 | `A-t6-1001`–`1008` |
| t7 decompose | 22 | +8 | 30 | `A-t7-1001`–`1008` |
| t9 graph_bind | 32 | +12 | 44 | `A-t9-1001`–`1012` |
| t10 delegation_extract | 22 | +8 | 30 | `A-t10-1001`–`1008` |
| **계** | **145** | **+55** | **200** | 전 문항 tag `d9_ext` |

- 층2 채점셋 정의 확인: 145 = t3 28 + t5 21 + t6 20 + **t7 base_golden 22**(t7_holdout/e3는
  `aggregate_scored_set_wide` 태그로 per-item 채점 제외, `harness_e2e.is_model_independent`)
  + t9 32 + t10 22. 신규 t7 8문항은 base_golden과 동일하게 probe/aggregate 태그 없이
  (ext_tier 미지정) 발행돼 **per-item 채점셋에 들어간다**.
- **사전선언 §2 표기 정정**: 표의 "t9 email_draft"는 오기다. 층2 채점셋의 t9(기존 32문항)는
  `golden/t9_graph_bind.json`(graph_bind, `bind_graph_params`)이며 email_draft 골든이 아니다.
  문항 수 열(32→44)이 기준이므로 신규 12문항은 graph_bind로 저작했다. "t6 wiki_qa 게이트"도
  실제로는 intent 7-way(`classify_intent`)다.

## 3. 라벨 균형 (기존 분포 비례)

| 작업 | 기존 분포 | 신규 분포 |
|---|---|---|
| t3 intent | groupby 다수 + count/filter 소수 | groupby-count 7 / count-single 2 / filter-count 2 (emoji-db 3, 끝공백 db 3) |
| t5 route | structured 8 / wiki 8 / graph 5 | structured 3 / wiki 3 / graph 2 |
| t6 label | read 10 / command 10 (8라벨) | 8라벨 각 1 (read 3 + command family 5) |
| t7 compound | true 16 / false 6 (73%) | true 6 / false 2 (75%) — parts 2×5, 3×1 |
| t9 intent | relation/conflict/provenance/entity 각 8 | 각 3 |
| t10 | true 10 (code 5/knowledge 5) / false 12 | true 4 (code 2/knowledge 2) / false 4 |

## 4. t3 gold 검증 — 11/11 gold_verified (gate_only 0)

DSN `postgresql://orthus:orthus@localhost:5433/orthus_company_0706` read-only,
`t3_gold.gold_numbers`와 동일 로직(`build_manifest.py::_load_t3_gold(T3_D9_SPECS, T3_D9_DSN)`).
(db,kind,group_key,where) 조합은 기존 `t3.json` SPECS와 전부 무겹침.

| id | db | spec | gold set |
|---|---|---|---|
| t3-x01 | 배우 오디션 기록 | groupby 결과 | {7,8,9} |
| t3-x02 | 시사회 초청 명단 | groupby 참석 여부 | {7,12} |
| t3-x03 | 📅 촬영 일정표 | count | {23} |
| t3-x04 | 외주 계약␣ | groupby 진행 상태 | {4,5,9} |
| t3-x05 | 정산 내역␣ | count, 지급 상태='지급 완료' | {3} |
| t3-x06 | 보도자료 배포처 | groupby 매체 유형 | {2,6,10} |
| t3-x07 | 편집본 버전 관리 | count | {20} |
| t3-x08 | 🎧 사운드 소스 관리 | groupby 정리 상태 | {3,4,10} |
| t3-x09 | 장면 콘티 검수 | groupby 우선도 | {5,7} |
| t3-x10 | 의상 소품 재고␣ | count, 대여 여부='대여 중' | {10} |
| t3-x11 | 🎼 배경음악 라이선스 | groupby 라이선스 종류 | {4,6} |

표면 문구는 조사-중립 템플릿 풀에서 `SEED=20260723` 결정론 선택(사전선언 §3.1).
**저작 중 발견·수정한 결함 1건**: 초판 템플릿 풀이 D8 생성기(`train/build_d8_holdout.py`)의
풀을 재사용해 tier-B `t3_d8_holdout.json`과 표면 문구 4건이 **정확히 겹쳤다**(d8-0255/0487/
0850/0961). 동결 전에 템플릿 풀 전체를 신규 문구로 교체하고 같은 SEED로 재생성했으며,
재생성본은 **전 golden/*.json 대비 표면 중복 0**을 확인한 뒤에만 동결했다(모델 응답은 여전히
미참조 — 중복 검사는 골든 입력 텍스트끼리의 비교다).

## 5. Blind 규율 준수 (사전선언 §3)

- 어떤 모델의 응답도, `analysis/raw/*`의 어떤 파일도 참조하지 않았다.
- 기존 145에서 특정 모델이 틀린 문항의 변형·복제를 만들지 않았다 — 신규 표면은
  D8 합성 도메인(오디션/시사회/외주 계약/정산/라이선스 등) + 신규 인명·업체명으로 작성.
- 신규 55문항 전부 `frozen.input_sha256` 동결(`frozen_at: build:73b8b44`), id 중복 0,
  스키마 검증(필수 키/scoring-kind 정합/JSON round-trip) PASS — `build_manifest.py` 자체
  validate 출력 `334 records: valid JSON, required keys present, ids unique. OK`.

## 6. 기존 문항 바이트 동일성 증명

- `build_manifest.py`에 **frozen-line replay**를 추가했다: 기존 `tier_a.jsonl`에 이미 있는
  id는 저장된 라인을 그대로 재발행하고(입력 sha 드리프트 gate assert 후), 새 id만 신규
  발행한다. 재빌드 출력: `279 frozen lines replayed byte-identically, 55 newly minted`,
  재재빌드 시 `334 replayed, 0 newly minted`(멱등).
- `git diff --numstat e2e/tier_a.jsonl` = **55 added / 0 deleted** — 기존 279라인(채점 145
  포함) 1바이트도 불변. id별 파이썬 비교(구본 279 vs 신본)에서도 diff 0 / 누락 0.
- `freeze.lock`은 **tier_a 섹션만** 재동결했다(count 350→405 = tier_a.jsonl 334 + l2 tier-A
  71, pending 10 불변; manifest_sha256 갱신). tier_b 섹션은 건드리지 않았다 — 참고로 현재
  on-disk tier_b/l2는 lock의 tier_b 해시와 이미 불일치 상태였는데(1657/52 vs 1698/26, l2
  user-fill이 lock 생성 후 채워진 것), 이는 D9와 무관한 기존 상태라 그대로 두고 여기 기록만
  남긴다.

## 7. 다음 단계 (본 커밋 이후)

측정 단계는 사전선언 §4대로: 신규 55만 전 arm 실측(기존 145 결과 재사용), 합산 n=200
쌍대 McNemar. 본 커밋은 어떤 모델 호출보다 먼저 push된다.

## 8. 결함 수정 2 (build:1354c09 이후, 표면 재저작 4문항)

mock invariant 런(`--only-tag d9_ext`, invariant "model-independent scored FAILS: 0")이
**저작 결함 4건**을 노출했다: 결정론 프로덕션 레이어가 LLM에 도달하기 **전에** gold와
모순되는 답을 확정해, 모든 arm에서 모델 무관 FAIL이 나는 문항들이다. 라벨은 전부
유지하고 **표면 문구만** 재저작했다(사전선언 §3 blind 규율 유지 — 모델 응답/raw 미참조,
탐침 대상은 fixture의 일부인 결정론 룰 레이어뿐).

| id | 결함 | 구표면 → 신표면 |
|---|---|---|
| A-t7-1001 | decompose 프리필터(default tier 0, `docs/decompose-prefilter-ext.md`)가 "랑/각각" 누락형 신호를 잘라 `should_decompose`가 LLM 게이트 도달 전 False 확정 (gold True) | "외주 계약 진행 상태별 건수랑 정산 내역 지급 상태별 건수를 각각 알려줘" → **"외주 계약 진행 상태별 건수 알려주고 정산 내역 지급 상태별 건수도 알려줘"** (`알려주고` = tier-0 공유 접속 토큰, 기존 t7-20 형태) |
| A-t7-1002 | 동일 ("뭐고"는 tier-2 확장 신호라 tier 0에서 컷) | "시사회 초청 기준이 뭐고 좌석 배정은 어떻게 해?" → **"시사회 초청 기준이 뭔지 그리고 좌석 배정은 어떻게 하는지 알려줘"** (`그리고`, 기존 t7-16/19 형태) |
| A-t7-1003 | 동일 ("랑/같이"는 tier 0 신호 아님) | "촬영 일정표 회차 수랑 오디션 지원자 수를 같이 세어줘" → **"1. 촬영 일정표 회차 수 2. 오디션 지원자 수를 같이 세어줘"** (`1.`+`2.` 열거 신호, 기존 t7-04 형태) |
| A-t6-1008 | `detect_assistant_command_action`의 결정론 키워드 룰이 "태스크"(_BOARD_TERMS) + "정리"를 board 분기 선순위로 매칭해 `personal_board_cleanup`으로 선분류 — LLM 미도달 (gold central_wiki_task_cleanup) | "묵은 위키 정리 태스크들 한 번에 털어줘" → **"묵은 위키 태스크들 한 번에 싹 털어줘"** ("정리" 제거로 _COMMAND_VERBS 미발화 → keyword family None, `_rule_based_route`/`_quantity_route`도 None → 7-way LLM 분류기 도달) |

검증:

- 결정론 레이어 직접 탐침(직수입, `ORTHUS_LLM=mock`): 신표면 4건 전부
  t7 `_has_command_verb ∨ _has_connective_or_enum(ext_tier=0)` = True,
  t6 `detect_assistant_command_action=None ∧ _rule_based_route=None` → **reached_llm True**.
- 재동결: 구 4라인 제거 후 `build_manifest.py` 재실행 →
  `330 frozen lines replayed byte-identically, 4 newly minted` (`frozen_at: build:1354c09`).
  id별 파이썬 비교(구본 334 vs 신본 334): **변경 id 정확히 4개, 나머지 330라인 바이트 동일**.
  `freeze.lock`은 tier_a `manifest_sha256`만 재계산(4d1e5a3b… → bb839ce0…, count 405/pending 10 불변).
- mock invariant 재실행(지시 커맨드 그대로, `--only-tag d9_ext`):
  `[mock] {'deferred': 45, 'pass': 10} scored 10 (pass 10)` /
  **model-independent scored FAILS: 0** / confident-zero 0 / RESULT: PASS.
  raw에서 4문항 모두 `reached_llm: true` + status deferred(mock 출력은 채점 대상 아님) 확인.

## 9. main 머지 시 동결 상태 (2026-07-30)

D9 브랜치(`feat/fugu-ko-benchmark-suite`)를 main에 머지할 때, main은 그 사이 **다른
골든 확장**(holdout + `aug_*` 병합, `_items_multi`)을 독자적으로 진행해 tier_a가
334 → 1,884 라인으로 커져 있었다. 두 확장은 독립이라 **양쪽을 모두 보존**했다:

- `build_manifest.py`: `_items_multi`(main, holdout/aug 병합)와 `_ext_items`(D9)를
  함께 두고, 신설 `_with_ext(base, ext_fname, key)`가 D9 항목을 병합 리스트 뒤에
  덧붙인다. 정규화 `key` 충돌 시 D9 항목을 버려 기존 base/holdout id가 그대로 남는다
  (`_items_multi`와 같은 first-occurrence-wins).
- 실측 확인: t3 +11 · t5 +8 · t6 +8 · t7 +8 · t9 +12 · t10 +8 = **정확히 +55**,
  전부 `d9_ext` 태그, main 대비 신규 중복 golden id 0
  (t7의 dup 16은 ext_tier/e3 변형을 같은 골든 항목으로 내보내는 기존 설계이며
  main 기준선에서도 16이다).

⚠️ **동결 산출물은 재동결되지 않았다.** `e2e/tier_a.jsonl`(1,884라인)과
`e2e/freeze.lock`(`build:511c0de`, tier_a count 1,955)은 **main 버전을 그대로 유지**했다
— 두 파일은 서로의 `manifest_sha256`으로 묶여 있어 함께 가져와야 자기정합이다.
재동결에는 t3 gold 라이브 조회용 DB(`T3_DSN`, `T3_D9_DSN` = :5433)가 필요하고 머지
시점에 그 DB가 없어, 재동결을 시도하면 main의 1,884라인 전체가
`gold_verified` → `gold_unavailable_at_build`로 퇴화한다. 따라서:

- 현재 동결본에는 `d9_ext` 항목이 **0개**다(빌더는 포함하지만 동결본은 아직 아님).
- 드리프트 게이트는 빌더 재실행 시 **+55 델타**를 정상적으로 신고한다 — 이는 손상이
  아니라 "재동결 필요" 신호다.
- **후속(DB 접근 가능한 운영자):** :5433 기동 후 `build_manifest.py` 재실행 →
  기존 1,884라인 byte-identical 재생 + 55 신규 확인 → `freeze.lock` 재계산.
  그 전까지 D9 n=200 층2 수치는 **본 브랜치의 원본 동결본**(`build:caa791d`,
  tier_a 405/334라인)으로만 재현된다.

### 9.1 D10 l2 flow 확장도 같은 상태

D10(`build_l2_d10.py` + `fixtures/g{1,3,4}/*-d10.json`)의 l2 flow 137 → 168 확장도
동일하게 **소스만 머지되고 동결본은 main 유지**다. 머지 직후 `l2/g*.jsonl`이
D10 값(43/84/56/38)으로 자동 병합돼 main `freeze.lock`의 기대치(40/42/36/19)와
어긋났으므로, `l2/g*.jsonl` 4개를 main 버전으로 되돌려 **동결 산출물 전체를 한 세트로
자기정합**하게 맞췄다(tier_a 1,884 · tier_b 1,653 · l2 40/42/36/19 모두 lock과 일치).
`build_l2_d10.py`와 d10 fixture는 그대로 있으니 재동결 시 함께 반영된다.
`inventory.json`은 D9 골든 asset 항목만 추가됐고(l2/d10 참조 없음) 실제 파일이
존재하므로 정합하다.

재동결 1회로 D9(+55 tier_a)와 D10(+31 scored l2)이 동시에 들어온다.
