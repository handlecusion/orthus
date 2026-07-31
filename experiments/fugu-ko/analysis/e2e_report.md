# E2E 벤치마크 최종 리포트

> 대회 E2E 벤치마크의 최종 통합 리포트. 실측·측정 과정의 상세(D/E 시리즈)는
> `experiments/fugu-ko/analysis/`의 개별 문서(`experiment-log.md`, `d*-results.md`,
> `e*-results.md`)에 있고, 본 문서는 그 위에서 돌린 **라이브 E2E 하네스**
> (`experiments/fugu-ko/harness_e2e.py`, 동결 데이터셋 `e2e/freeze.lock`)의 결과를
> Phase 단위로 정리한다. Phase 5 = 국내 3모델(Solar / EXAONE / A.X) + baseline
> (gpt-4o-mini) 비교, Phase 6 = 대형 모델 비교(GPT-4o / GLM-5.2 / DeepSeek → 확장
> GPT-5.3 / DeepSeek V4 Pro → 2026-07-21 Bedrock bearer key 확보로 Claude Sonnet 4.6 /
> Claude Haiku 4.5 추가, **총 11모델**; Bedrock 잔여 3종(Llama 70B/8B, Nova Pro)은
> 미측정).

> **골든셋 확장 재측정 (n=145 → n=324, 측정일 2026-07-21):** 초기 전 리포트는
> 공통 채점 세트 **n=145**로 나왔다. 검정력을 높이기 위해 tier-A 골든을 작업별로
> 확장(t3 28→69, t5 21→76, t6 20→47, t7 22→46, t9 32 유지, t10 22→54)해 **공통
> 채점 n=324**로 **전 11모델을 동일 프로덕션 배선·exact/structural 채점으로 재실행**
> 했다(11모델 전부 error 0, invariant CLEAN). **같은 모델·같은 파이프라인에 표본만
> 늘린 순수 검정력 재시험**이며(신규 모델 추가가 아님), 병합 SoR은
> `analysis/raw/phase9_verified_stats_11model.json`(`n_common_scored=324`)이다.
> 아래 본문 수치는 **n=324 기준으로 갱신**했고 역사적 n=145 값은 `n=145 → n=324`
> 형태로 병기한다. 재측정의 핵심 결론(§7.5): **상위권 단일 클러스터가 중위권과
> 새로 분리됐고**, 국내 슬롯 조립 vs 최강 단일 대형 **동률**과 V1 vs V2 조립
> **동률**은 검정력을 높여도 그대로 유지됐으며, **V2 조립이 gpt-4o-mini baseline을
> 새로 유의하게 앞섰다**(n=145에선 비유의).

> **골든 증강 재측정 (n=324 → n=1,750, 측정일 2026-07-22 — §Phase 8):** tier-A 골든을
> **851→1,884문항**으로 증강(공통 채점 세트 **n=1,750**)해 전 11모델을 동일 배선으로
> 재실행했다. 증강분은 **"기존 자산 재분류분"(routing dedup-merge 393) + "순수 신규
> 생성분"(1,033)**으로 나뉘고, 신규 생성은 **결정론(DB) + Bedrock Nova Pro/Llama 3.3-70B**
> 제3벤더 합성 + 실로그로만 만들어 국내 3사·Bedrock Claude 생성기를 배제했다(§8.1).
> 동시에 t3 채점기의 `_flatten_rows` 라벨 오염 결함을 **counts-only 교정**해 억울 fail을
> 회수했다(§8.2, 전 모델 오프라인 재채점, 역행 0). 핵심 결론: **상위 동률 클러스터가 해소**
> (EXAONE이 프론티어4에 유의 열세로 갈림)됐고, 국내 슬롯 조립은 최강 단일 프론티어와 **동률**
> 이며 최강 국내 단일 Solar에 **유의 우세**다. **⚠️ 신규 발견 — diversification cost:** n=1,750
> 에서 best-per-slot(all-solar-except-t10)이 §15 다양화 조립보다 **유의 우세**(p=7.6e-5, ~1pp)
> — 단 ASSIGNMENTS 변경은 owner 게이트라 본 리포트는 **기록만** 한다(§8.5).

---

## Phase 5 — 국내 3모델 vs baseline 라이브 벤치마크

**권위 SoR:** `analysis/raw/phase5_final_stats.json`
(t3 `orthus_company` 재실행 + t10 존칭-strip 재채점 반영, 2026-07-21).
지연은 `phase5_digest.md`(Phase 5 full run), 원 상세는 `phase5_digest.md`,
보정 중간단계는 `phase5_corrected_stats.md`.

### 5.0 Executive summary — 결론 먼저

- **국내 3모델과 baseline 사이에 확정적으로 유의한 정확도 차이는 A.X 열세 하나뿐이다.**
  골든 확장 재측정 **scored n=145 → n=324**에서 정확도는 **EXAONE 80.86%(262/324) >
  baseline = Solar 79.01%(256/324) > A.X 76.85%(249/324)**이고(n=145: EXAONE 81.38 >
  baseline 79.31 > Solar 77.24 > A.X 75.17), 6개 쌍대 McNemar 중 **5개가 p>0.05로
  비유의**다. 유일하게 문턱을 넘은 쌍은 여전히 **EXAONE vs A.X**인데, 표본을 2배로
  늘리자 n=145의 경계값 p=0.049에서 **p=0.024(discordant 21/8)로 다소 강화**됐다 —
  단 이는 여전히 6쌍 중 하나이고 나머지 5쌍(Solar-EXAONE p=0.263, Solar-A.X 0.281,
  Solar-baseline 1.000, EXAONE-baseline 0.286, A.X-baseline 0.324)은 전부 비유의다.
  즉 **"국내 3모델 + baseline 간 확정 유의 정확도 차이는 A.X가 EXAONE에 지는 것
  하나뿐"**이라는 결론은 검정력을 높여도 유지된다(§5.2 참조).

- **지연(scored 공통 p50)은 EXAONE이 최속이다: EXAONE 295ms < A.X 663ms ≈ Solar
  681ms < baseline(gpt-4o-mini) 780ms.** p95는 EXAONE 1447 < baseline 1579 < A.X
  1998 < Solar 2803(Solar 꼬리가 노이즈). 정확도가 A.X 열세 외엔 동률인 반면,
  **국내 3모델 모두 p50이 baseline과 같거나 빠르고 EXAONE은 baseline보다 2.6배
  빠르다.**

- **기존 결론과 정합한다.** `docs/model-orchestration.md` §11.3b의 상시 결론
  ―「국내 3모델 간 어떤 쌍대 차이도 유의하지 않다 / 국내 모델을 쓸 근거는 성능이
  아니라 **벤더 금지·지연·안전** 셋뿐이고 결론은 "옮겨도 큰 손해가 없다"」― 를
  Phase 5는 **뒤집지 않고 재확인**한다. 정확도는 동률(문서와 일치), 지연은 국내
  모델 우세(문서의 "지연" 근거와 일치), 안전은 t10 위임 오탐 재현으로 관측(§5.1
  t10). n=324에서 유의로 강화된 EXAONE vs A.X(p=0.024) 1건은 A.X의 t3 group-by
  채점 긴장(§5.2c)과 EXAONE의 t10 존칭 보정(§5.2b)이라는 동일 측정 효과가 표본
  확대로 누적된 결과라, 이 상시 결론을 뒤집을 성능 근거로 쓰기엔 이르며 별도
  review 대상으로만 남긴다.

(지연은 **scored 공통 세트 기준** p50/p95다 — n=324 확장본 `phase9_verified_stats_11model.json::latency.scored_common_*`.)

| model | scored n | pass | accuracy | p50 ms | p95 ms |
|---|---|---|---|---|---|
| **EXAONE** | 324 | 262 | **0.8086** | **295** | **1447** |
| baseline (gpt-4o-mini) | 324 | 256 | 0.7901 | 780 | 1579 |
| **Solar** | 324 | 256 | 0.7901 | 681 | 2803 |
| **A.X** | 324 | 249 | 0.7685 | 663 | 1998 |

(n=145 → n=324: EXAONE 118→262, baseline 115→256, Solar 112→256, A.X 109→249.
deferred/skipped 잔여는 4모델 공통. 아래 §5.1·§5.3.)

**쌍대 유의성 (McNemar + bootstrap 95% CI, n_paired=324, 최종 corrected):**

| a | b | a만 정답 | b만 정답 | McNemar p | bootstrap Δ CI95 | 유의(p<0.05) |
|---|---|---|---|---|---|---|
| solar | exaone | 7 | 13 | 0.2632 | [−0.046, 0.009] | 아니오 |
| solar | ax | 19 | 12 | 0.2810 | [−0.012, 0.056] | 아니오 |
| solar | baseline | 5 | 5 | 1.0000 | [−0.019, 0.019] | 아니오 |
| **exaone** | **ax** | **21** | **8** | **0.0241** | **[0.009, 0.074]** | **예** |
| exaone | baseline | 14 | 8 | 0.2863 | [−0.009, 0.046] | 아니오 |
| ax | baseline | 15 | 22 | 0.3240 | [−0.059, 0.015] | 아니오 |

(n=145에서 유일 유의였던 EXAONE vs A.X는 경계 p=0.049 → n=324에서 p=0.024로
다소 강화됐고, 나머지 5쌍은 n=145·n=324 모두 비유의다.)

### 5.1 Task별 분해표

집계 정확도(0.75–0.81)는 여러 이질적 작업의 혼합이라 **작업별 분해가 판정의
기준**이다. Phase 5에서 실제 채점된 작업은 t3/t5/t6/t7/t9/t10 여섯 개이며(t2/t8은
judge-kind라 이 런에서 deferred, g1–g4는 L2로 skip — §5.3), 골든 확장 후 scored
**324건**의 구성은 t5(76) > t3(69) > t10(54) > t6(47) > t7(46) > t9(32)로 **한 작업이
지배하지 않는다**(n=145: t9 32 > t3 28 > t7=t10 22 > t5 21 > t6 20). (주의: 데이터셋
감사가 말하는 "t3 편중"은 **동결 매니페스트/디스패치 기준**이고 — t3 tier_b 홀드아웃이
전부 deferred이기 때문 — **채점된 324건 중 t3는 21%**(69/324)다.)

| task | 엔트리포인트 / 성격 | scored n | Solar | EXAONE | A.X | baseline | 통계 | 검정력 플래그 |
|---|---|---|---|---|---|---|---|---|
| **t3** structured NL→SQL | `structured/query.py::query_structured` · exact | 69 | 54/15 | 55/14 | 49/20 | 54/15 | 아래 pairwise에 포함 | **DB 재실행 보정분**(§5.2a) + 채점기 긴장(§5.2c) |
| **t5** router route | `router/route.py::classify` · exact | 76 | 62/14 | 62/14 | 64/12 | 63/13 | McNemar만 | 포화(near-saturation); known 12건 공통(§5.2d) |
| **t6** router intent | `router/route.py::classify_intent` · exact | 47 | 40/7 | 39/8 | 40/7 | 39/8 | McNemar만 | 포화; known 7건 공통(§5.2d), EXAONE·baseline만 1건 추가 |
| **t7** decompose | `router/decompose.py::should_decompose` · exact | 46 | 30/16 | 30/16 | 24/22 | 31/15 | McNemar만 | 14건 known ext_tier=0 prefilter 갭 공통(§5.2d) |
| **t9** graph bind | `router/graph.py::bind_graph_params` · exact | 32 | 32/0 | 30/2 | 32/0 | 32/0 | McNemar만 | 포화(홀드아웃 없음, n=32 유지); EXAONE만 2 fail(`A-t9-0012/0013`) |
| **t10** delegation extract | `agentwork/delegation.py::extract_delegation_intent` · exact | 54 | 38/16 | 46/8 | 40/14 | 37/17 | McNemar만 | 채점기 존칭 버그 보정 후(§5.2b). EXAONE 최고 |
| t2 wiki QA | `wiki/qa.py::ask` · **judge** | 0 (deferred) | — | — | — | — | 미채점 | judge-kind, 이 런 미채점 (n=30 판정자 필요) |
| t8 decompose synth | `router/decompose.py::synthesize` · **judge** | 0 (deferred) | — | — | — | — | 미채점 | 정성 전용, 미채점 |

읽는 법 (셀 = pass/fail):

- **t5(router route)가 가장 큰 채점 작업(76)이고 4모델이 62–64/76로 포화**다. **t3
  (structured)가 여전히 모델 간 가장 벌어지는 작업** — A.X 49/69로 뒤처지는데,
  이는 정확도 저하가 아니라 A.X가 거의 모든 t3 항목을 `SELECT label, COUNT(*)` group-by
  형태로 컴파일해 **라벨 컬럼까지 반환 → flatten-intset 채점기와 충돌**하기 때문이다
  (§5.2c). t3를 빼면 나머지 작업의 모델 간 격차는 t7의 A.X 열세를 빼면 대부분 1–2건이다.
- **t10(위임 추출)은 EXAONE가 46/54로 최고**인데, 이는 §5.2b 채점기 보정의 직접
  효과다(존칭 보정 없으면 하락). 보정 후에도 남는 EXAONE t10 실패에는 `A-t10-0016`이
  포함되고,
  이는 **회의록 전달발화("…맡기로 했다")를 현재형 위임으로 오탐하는 4모델 공통
  false-positive**로, `docs/model-orchestration.md`가 경고한 위임 오탐 트랩 범주가
  라이브에서 재현된 사례다(안전 근거의 실측).

### 5.2 측정 보정 이력 (투명성)

Phase 5 원 런은 두 개의 **측정 인프라 결함**을 안고 나왔고, 둘 다 모델 성능이
아니라 환경/채점기 문제였다. 원 런 산출물은
`analysis/raw/phase5_full_orthus_db/`(빈 DB 백업)에 보존돼 있고, 아래 보정을 거친
최종본이 `phase5_final_stats.json`이다.

**(a) t3 빈-DB → `orthus_company` 재실행.** 원 런은 루트 `.env`의 기본 개발 DB
`orthus`(notion_rows **0행**)를 물어, t3 exact-scored 18건이 **전 모델 동일하게 가짜
실패**했다(gate `True`, rows `[]`/`[[0]]`). t3는 `notion_rows` JSONB store 대상
read-only NL→SQL이라 DB가 비면 SQL은 정상 컴파일돼도 결과가 0이다 — LLM/코드 회귀가
아니다. 골든은 populated company corpus(1,365 notion_rows, `협업업무표` 794행 =
골든 `[7,16,771]` 합) 기준으로 계산됐으므로 **populated DB로 재실행**해 t3 tier_a
28건(exact 18 + gate_only 10)을 모델별로 교체했다.
  - **RO DSN 함정:** t3 SQL **실행**은 별도 필드 `pg_dsn_readonly`
    (`ORTHUS_PG_DSN_READONLY`)를 타므로, 메인 DSN만 `orthus_company`로 바꾼 1차
    재실행은 여전히 빈 `orthus`를 읽어 18/18 실패가 유지됐다. **두 DSN을 모두**
    `orthus_company`(+ `orthus_ro` 롤)로 오버라이드해야 복구됐다.
  - **staging TRUNCATE 함정:** 이름에 `staging`/`test`가 든 DB는 하네스가
    dispatch 전 `notion_rows` 포함 전 테이블을 TRUNCATE한다. 따라서 러너의
    "company-staging 사용" 권고는 t3에 대해 **틀렸고**, 이름에 `staging`/`test`가
    없어 truncate 가드가 건너뛰는 `orthus_company` 직결이 올바른 타깃이었다.

**(b) t10 채점기 존칭 버그 → 스코어러 수정 + 오프라인 재채점.** t10 exact 채점기가
`assignee` 문자열 완전일치를 요구해, 모델이 `"김철수"` 대신 `"김철수님"`(존칭)을
뽑으면 **`mode` 분류가 정확해도 항목 전체가 fail** 처리됐다. `_strip_honorific()`
(님/씨, 선행 공백 유무 4케이스)을 모델 출력·골든 양쪽에 대칭 적용하도록 최소
diff로 고치고, **기존 raw jsonl을 read-only로 오프라인 재채점**(LLM 재호출 없음)했다.
결과(n=145): **EXAONE만 6건 fail→pass**(`A-t10-0001/0002/0005/0006/0007/0009` — 전부
mode는 이미 정확, assignee 존칭 문자열차로만 실패했던 항목), solar/ax/baseline은 t10
존칭 출력이 없어 변화 0. 이 보정은 골든 확장(t10 22→54) 후에도 EXAONE t10을 46/54로
전 모델 최고로 유지시키는 직접 요인이고, §5.0의 EXAONE vs A.X 유의(n=145 p=0.049 →
n=324 p=0.024)를 만든 주된 요인이다(모델-비종속 수정이 우연히 한 모델에만 영향을 준
사례 → 이 쌍을 성능 결론으로 승격하지 않는 이유).

**(c) t3 잔존 실패 = 채점기 긴장(스코어러 한계로 명시).** DB 보정 후에도 남는 t3
실패는 **진짜 모델 컴파일-형태 차이**다. 채점기는 반환된 모든 컬럼을 하나의 set으로
flatten해 골든 intset(`{7,16,771}`)과 비교하는데, `SELECT 상태, COUNT(*) … GROUP BY 1`
형태로 컴파일한 모델은 **라벨 컬럼까지 반환**해 flatten set이
`{'시작 전',7,'완료',771,'보류',16}` ≠ `{7,16,771}`이 되어 **카운트가 정확해도
실패**한다(실측: `A-t3-0001` solar rows = `[["시작 전",7],["완료",771],["보류",16]]`).
A.X가 이 형태를 거의 전 항목에서 써 t3 49/69로 가장 낮다. 이는 flatten-intset 골든과
자연스러운 SQL 형태 사이의 **채점기/골든 긴장**이며, 순위 왜곡은 아니되(전 모델 동일
규칙) 절대 점수 해석 시 주의를 요한다.

**(d) known-fail 상수 오프셋 — 골든 확장으로 8건 → 45건.** 결정론 fast-path/stale-golden
known-fail은 n=145의 8건에서 골든 확장 후 **45건**(t3 12 + t5 12 + t6 7 + t7 14; `known_fail_8`
필드, `all_models_fail_all_8=true`)으로 늘었다. 전부 **LLM 호출 전 결정론 fast-path에서
결판**나 11모델 전부 동일하게 실패한다(`per_model_status` 전 모델 fail) → 모델당 정확히
같은 상수 오프셋이라 순위·쌍대 비교(McNemar)에 영향 0. 성격은 동일: 실제 프로드
버그(용어 리스트 우선순위 — route/wiki, board/wiki-task 선후) + `ORTHUS_DECOMPOSE_PREFILTER_EXT_TIER=0`
기본값이 만든 stale golden(T3 tier 채택 시 자동 해소, `docs/decompose-prefilter-ext.md`;
comma-adjacent 병렬조사 갭은 max tier에서도 남는 잔여). **채점 세트 이론상한 = 324 − 45 =
279/324**(= §6/§7의 oracle any-of-11 279와 일치 — 45건은 어느 모델도 못 푸는 천장).

### 5.3 데이터셋 감사 caveat + 미채점 범위

데이터셋 감사(`dataset_audit.md`) 판정 = **PROCEED-WITH-CAVEATS (blocker 없음).**
동결·해시·제외 메커니즘은 흠결 없이 재검증됐고(freeze.lock ↔ 재계산 완전 일치,
input_sha256 불일치 전 디스패치 전수 0, 중복 id 0), 표본 검사에서 틀린 골든을 못 찾았다.
남는 것은 "데이터가 틀렸다"가 아니라 "무엇을 재는가"의 한계 3종:

1. **t3 편중(디스패치 기준):** 동결 매니페스트의 t3는 tier_a 69(확장 후) + tier_b
   홀드아웃으로 디스패치의 다수지만 tier_b가 전부 deferred라, **채점된 324건 중 t3는
   21%**(69/324)다. 그래도 집계 점수의 절대값은 structured NL→SQL 성능에 민감하므로
   **per-task 분해(§5.1)가 필수**이고 blended 0.77–0.81을 단독 인용하지 않는다.
2. **L2 모델-변별 아이템 62 pending:** DESIGN 기준 L2의 **모델 변별 아이템 전부**
   (G1-M distill recall/오염 16, G1-J grounding judge 12 등)가 아직 pending(frozen
   input 없음)이라 제외됐다. 남은 built L2 75건(g1–g4)은 대부분 구조적/결정론 분기
   커버리지라 **L2는 사실상 파이프라인 정확성 검증이지 모델 비교 신호가 아니다.**
   이 런에서 g1–g4 75건은 live DB 감지로 fixture write가 막혀 전부 **skipped**.
3. **24 human-verified anchor 미검증:** 전부 built(포함)이나 사람의 코드-읽기 검증
   미수행. 앵커는 구조적/결정론 assert가 다수라 정답이 틀려도 전 모델 동일 실패
   (상수 오프셋)라 순위 비차별적 — 단 절대 점수 해석 시 주의.

**미채점 범위(deferred/judge-kind + L2):**
- **deferred/모델** = 대부분 tier_b 홀드아웃(`model_fallback_zero` invariant로
  후속 stats phase에 의도적 유보) + t2(judge) + t8(judge). 골든 확장으로 tier_a
  채점분이 145→324로 늘며 deferred는 그만큼 줄었다.
- **skipped/모델** = L2 g1–g4, live DB fixture-write 차단.
- 따라서 Phase 5는 실질적으로 **L1 read-only 결정론/exact 작업의 모델 비교**이고,
  생성형 품질(t2 wiki QA judge, t8 synth) 판정은 이 런의 범위 밖이다.

**L2 해석 한계:** 위 2번대로 built L2는 결정론 파이프라인 검증이므로 "모델이 L2를
통과했다"는 모델 우열이 아니라 **코드 경로 정합성**의 증거로만 읽어야 한다.

### 5.4 빈 응답 오염 검사 (GLM류 pollution 대칭성)

채점 세트 n=324 내 빈 출력 행을 전수 카운트(`empty_content_check.n_empty_output_in_common_scored`).
t10이 22→54로 늘며 **expected-null**(수신자 없음 = 담당자를 뽑지 않는 것이 정답) 아이템도
함께 늘었다:

| model | 빈 출력 수 | 전부 fail? | 성격 |
|---|---|---|---|
| solar | 29 | 아니오 | t10 expected-null(빈 출력=정답) |
| exaone | 32 | 아니오 | 위와 동일 |
| ax | 23 | 아니오 | 위와 동일 |
| baseline | 28 | 아니오 | 위와 동일 |

**전 4모델 `empty_are_all_fail=false`.** 빈 출력은 전부 t10 expected-null(정당한
`"assignee": ""`, 수신자 없음 케이스)이지 잘린/빈 모델 응답이 아니다. GLM류
empty-response 오염은 어느 모델에서도 발견되지 않았다(11모델 전체는 §6.3).

### 5.5 invariant 블록 (무결성)

- `model.fallback` spans (런 전후 delta): **0 (CLEAN)** — 배정 슬롯 fallback 미발생.
- confident-zero violations: **0** — 확신 있게 0을 답한 위반 없음.
- 즉, FAIL 판정 신호는 오직 `scored_fails` 비어있지 않음에서 왔고(원 런 기준),
  그 대부분(t3 18건)이 §5.2a DB 타깃 문제였음이 확인돼 보정으로 해소됐다.

### 5.6 Phase 5 종합 판정

- **모델 순위 근거:** 정확도는 n=324에서도 4모델이 노이즈 안에서 동률(5/6 쌍 p>0.05,
  EXAONE-A.X만 p=0.024로 유의·강화). **성능만으로는 A.X 열세 외엔 어느 국내 모델도
  baseline을 확정적으로 못 이기고 못 진다.** 국내 모델 채택 근거는 성능이 아니라
  **지연(scored p50 EXAONE 최속 295ms, 국내 3모델 전부 baseline 780ms 이하)**과
  **벤더 금지·안전(t10 위임 오탐 재현)**이라는 `docs/model-orchestration.md` §11.3b
  상시 결론에 **부합**한다.
- **측정 신뢰성:** 두 인프라 결함(빈-DB t3, t10 존칭 채점기)은 규명·보정됐고,
  잔존 t3 실패는 채점기/골든 긴장으로 명시(스코어러 한계). known-fail 45건은 상수
  오프셋으로 순위 무영향. 빈-응답 오염 0, fallback 0.
- **한계:** 생성형 품질(t2/t8 judge)과 L2 모델-변별(62 pending)은 이 런 범위 밖이며,
  Phase 6에서도 동일 제약이 이어진다. n=324에서 유의로 강화된 EXAONE-A.X는 측정 효과
  누적분이라 review 대상으로만 남긴다.

---

## Phase 6 — 대형 모델 비교 + 11모델 통합

**권위 SoR:** 11모델 확장본 `analysis/raw/phase8_verified_stats_11model.json`(측정일
2026-07-21). 기존 7모델본 `analysis/raw/phase6_verified_stats.json`(+
`phase6_verified_stats.md`)과 9모델본 `analysis/raw/phase6_verified_stats_9model.json`
(2026-07-21)은 그대로 두고, GPT-5.3
(`openai:gpt-5.3-chat-latest`)과 DeepSeek V4 Pro(`deepseek:deepseek-v4-pro`)에 이어
Bedrock 경유 Anthropic **Claude Sonnet 4.6**(`bedrock:anthropic.claude-sonnet-4-6`)과
**Claude Haiku 4.5**(`bedrock:anthropic.claude-haiku-4-5-20251001-v1:0`) 2모델을
같은 조건으로 추가 측정해 11모델 확장본을 별도 파일로 남겼다. 전 11모델을
`orthus_company` 직결(Phase 5 §5.2a의 올바른 DB 타깃) + 존칭-보정 채점기 + 프로덕션
배선(§5 공정 비교 조건) 그대로 실행하고, Phase 5의 국내 3모델 + baseline
**corrected** 값과 **공통 tier-A 세트**로 paired 병합했다. **골든 확장 재측정으로
공통 채점 세트가 n=145 → n=324로 늘었고**, 병합 SoR도 `phase8_verified_stats_11model.json`
(n=145)에서 **`phase9_verified_stats_11model.json`(`n_common_scored=324`)**로 갱신됐다.
전 11모델의 채점 아이템 id-set이 완전 동일(`common_covers_all_p6=true`, `n_common=324`)해
공정 비교가 성립한다. 11모델 전부 tier A 채점 슬라이스(t3/t5/t6/t7/t9/t10) 실행,
error 행 0, `model.fallback` 0(CLEAN), empty-output은 전부 t10 expected-null(오염
없음)이다. Bedrock 5종은 당시 키 인증 실패로 보류였으나 **2026-07-21 bearer key
확보로 Claude Sonnet 4.6/Haiku 4.5 측정 완료**, 나머지 3종(Llama 70B/8B, Nova Pro)은
미측정(§6.2).

### 6.0 Executive summary — 11모델 결론

- **최상위권은 대형·국내가 통계적으로 뒤섞여 있다.** 공통 **324**(n=145 → n=324) 기준
  순위는 **DeepSeek 83.33%(270) > Claude Sonnet 4.6 82.72%(268) > GLM-5.2 82.10%(266) >
  GPT-5.3 81.79%(265) > EXAONE 80.86%(262) > DeepSeek V4 Pro 80.25%(260) > GPT-4o
  79.32%(257) > Solar = baseline 79.01%(256) > A.X 76.85%(249) > Claude Haiku 4.5
  50.62%(164, 최하위)**. 상위 5모델 **DeepSeek·Claude Sonnet 4.6·GLM-5.2·GPT-5.3·EXAONE
  (국내)**은 서로 **10쌍 McNemar 전부 비유의로 상호 통계 동률**(DeepSeek vs EXAONE p=0.115,
  vs Sonnet 0.774, vs GLM 0.454, vs GPT-5.3 0.267; EXAONE vs GPT-5.3 0.664, vs GLM 0.523,
  vs Sonnet 0.286; GLM vs GPT-5.3 1.000, vs Sonnet 0.688; Sonnet vs GPT-5.3 0.607)이다.
  대회 관점 핵심 메시지: **국내 EXAONE이 대형 프론티어 4종(DeepSeek·Sonnet·GLM·GPT-5.3)과
  통계 동률의 최상위 클러스터에 있다.**
- **🔑 NEW at n=324 — 최상위 클러스터가 중위권과 유의하게 분리됐다.** 표본을 2배로
  늘리자 n=145에서 동률이던 여러 쌍이 유의로 갈렸다: DeepSeek·Claude Sonnet 4.6은
  중위권 4모델(A.X/Solar/baseline/GPT-4o) **전부를 p<0.05로 이기고**, GLM은 A.X·GPT-4o,
  GPT-5.3은 A.X·Solar·baseline을 이긴다(EXAONE은 A.X만 유의). 11모델 55쌍 중 **유의쌍이
  n=145의 소수에서 25쌍(Haiku 10쌍 포함)으로 늘었다**(9모델 36쌍 기준 11쌍). 즉 검정력이
  오르자 "상위권 ↔ 중위권" 계층은 드러났지만, **상위 클러스터 내부(국내 EXAONE 포함)는
  여전히 상호 동률**로 남았다.
- **신규 GPT-5.3은 종합 4위지만 최상위권과 통계 동률이다.** 81.79%(265/324)로 상위
  클러스터 안이고, 유의하게 앞선 상대는 Solar(p=0.049)·baseline(0.049)·A.X(0.0037)
  셋이다. **⚠️ 공정성 캐비앗:** GPT-5.3은 OpenAI API가 `temperature=0`을 거부해(벤더
  기본값 `temperature=1` 고정만 허용) 이 슬러그만 temperature 파라미터를 생략(=기본값 1)해
  돌렸다 — 나머지 10모델은 전부 `temperature=0`이다(§6.2). 또한 OpenAI가 이미 deprecated
  처리한 모델로 2026-08경 shutdown 예정이라 비교 기준선으로서의 수명이 짧다.
- **신규 DeepSeek V4 Pro는 자사 전작 V3.2보다 뒤진다.** 80.25%(260/324)로 공동 6위이며,
  1위 DeepSeek V3.2(=`deepseek` 슬러그, deepseek-chat) 상대로 **p=0.031(V3.2-only 14,
  V4-only 4)로 유의 열세**다(n=145 p=0.039 경계 → n=324 p=0.031로 유지·소폭 강화). hybrid
  reasoning 모델(thinking 기본)이라 지연도 scored p50 2481ms / p95 7586ms로 GLM-5.2 다음으로
  느리다.
- **신규 Claude Sonnet 4.6은 268/324(82.72%)로 종합 2위이며 상위권 전부와 통계 동률이다**
  (vs DeepSeek p=0.774, vs GLM 0.688, vs GPT-5.3 0.607, vs EXAONE 0.286). 대신 중위권 4모델
  (A.X/Solar/baseline/GPT-4o)은 전부 p<0.05로 이긴다. scored 지연은 p50 1983ms / p95 3435ms로
  국내 최속권(EXAONE 295ms·Solar 681ms) 대비 3–7배 느리다.
- **신규 Claude Haiku 4.5는 50.62%(164/324)로 유일하게 전 모델 대비 유의 열세인 최하위
  (11위)다**(vs A.X p=2.9e-16, −26.2pp; vs Solar p=2.4e-21, −28.4pp). n=145의 42.76%보다
  절대값이 오른 것은 파싱이 걸리지 않는 t5/t6가 확장분에서 커졌기 때문이고, 근본 원인은
  능력이 아니라 **포맷 지시-이행 실패**다 — JSON 응답을 코드펜스로 감싸 프로덕션 파서가
  t3 전건(0/69)·t9 전건(1/32)에서 파싱 실패했다(각주 ², §6.2).
- **DeepSeek(V3.2)은 상위 클러스터 4모델(Sonnet·GLM·GPT-5.3·EXAONE)과 동률**이고,
  중위권 5모델(Solar/A.X/baseline/GPT-4o/V4 Pro)에 p<0.05 우위다. **단 n=145에서 유의였던
  DeepSeek > GLM은 n=324에서 GLM이 3위로 올라오며 p=0.454로 동률화됐다** — 표본 확대가
  순위를 흔든 대표 사례. 그 외 유의 하이라이트는 EXAONE > A.X(p=0.024), DeepSeek V3.2 >
  V4 Pro(p=0.031)다(Haiku의 전 모델 대비 유의 열세는 별도 — 위 bullet).
- **국내 3모델은 여전히 상호 동률**(Solar/EXAONE p=0.263, Solar/A.X 0.281),
  A.X만 EXAONE에 p=0.024 열세. Phase 5의 "국내 3모델 간 (A.X 열세 외) 유의차 없음" 결론은
  대형 모델 다수를 더 넣고 표본을 2배로 늘려도 유지된다.
- **특이점: 대형이 mini를 못 이긴다.** GPT-4o 79.32%(257) vs baseline(gpt-4o-mini)
  79.01%(256)은 **p=1.000 동률**(GPT-only 9 / base-only 8)로, 이 작업 분포에서 대형 GPT-4o가
  mini와 사실상 동수다. 신규 DeepSeek V4 Pro도 전작 V3.2에 열세인 것과 함께, 프론티어
  대형이라고 이 종류의 라우팅/구조화 판정에서 자동 우위는 아니라는 증거가 유지된다.
- **슬롯 배정 근거(벤더 금지·지연·안전)는 유지된다.** 국내 EXAONE이 대형 4종과 동률 최상위
  클러스터에 있고(나머지 국내도 mini와 동률), 국내 모델의 지연 우위(EXAONE scored p50 295ms,
  Solar 681ms — 전 11모델 중 최속권; GLM 3591ms·V4 Pro 2481ms가 최저속)와 안전(t10 위임
  오탐 트랩 재현)은 그대로다. `docs/model-orchestration.md` §11.3b의 "옮겨도 큰 손해가 없다 /
  근거는 성능이 아니라 벤더 금지·지연·안전"은 표본을 324로 늘려도 성립한다.

| # | model | Phase | scored n | pass | accuracy | scored p50 ms | scored p95 ms |
|---|---|---|---|---|---|---|---|
| 1 | **DeepSeek V3.2** (deepseek-chat) | P6 | 324 | 270 | **0.8333** | 815 | 1250 |
| 2 | **Claude Sonnet 4.6** (bedrock) | P8 | 324 | 268 | **0.8272** | 1983 | 3435 |
| 3 | **GLM-5.2** *(tier A)* | P6 | 324 | 266 | **0.8210** | **3591** | **18319** |
| 4 | **GPT-5.3** (gpt-5.3-chat-latest) ¹ | P6 | 324 | 265 | **0.8179** | 1855 | 5749 |
| 5 | **EXAONE** (국내) | P5 | 324 | 262 | **0.8086** | **295** | 1447 |
| 6 | **DeepSeek V4 Pro** (deepseek-v4-pro) | P6 | 324 | 260 | 0.8025 | 2481 | 7586 |
| 7 | GPT-4o | P6 | 324 | 257 | 0.7932 | 853 | 1982 |
| 8 | baseline (gpt-4o-mini) | P5 | 324 | 256 | 0.7901 | 780 | 1579 |
| 8 | **Solar** (국내) | P5 | 324 | 256 | 0.7901 | 681 | 2803 |
| 10 | **A.X** (국내) | P5 | 324 | 249 | 0.7685 | 663 | 1998 |
| 11 | **Claude Haiku 4.5** (bedrock) ² | P8 | 324 | 164 | 0.5062 | 1867 | 2957 |

(n=145 → n=324 pass: DeepSeek 121→270, Sonnet 118→268, GLM 115→266, GPT-5.3 119→265,
EXAONE 118→262, V4 Pro 114→260, GPT-4o 114→257, baseline 115→256, Solar 112→256,
A.X 109→249, Haiku 62→164. 순위 재편: Sonnet 공동3위→2위, GLM 공동5위→3위, EXAONE
공동3위→5위.)

¹ GPT-5.3은 `temperature=0`을 벤더가 거부해 temperature 파라미터 생략(기본값 1)으로
실행 — 나머지 10모델은 `temperature=0`(§6.2). OpenAI deprecated 모델(2026-08경 shutdown 예정).

² Claude Haiku 4.5는 어댑터의 명시적 지시("Return only a valid JSON object. Do not
use Markdown fences.")를 무시하고 모든 JSON 응답을 `` ```json `` 코드펜스로 감쌌다.
프로덕션 호출부(`orthus/assistant/compile.py`의 `json.loads`,
`orthus/router/graph.py`의 `bind_graph_params`)는 fence-stripping 없이 즉시 파싱
실패 → t3 전건 `compile_failed`/rejected(0/69), t9 전건 intent null(1/32). 샘플
검사에서 fence 내부의 SQL/intent 내용 자체는 정확했다. 같은 어댑터·같은 프롬프트의
Sonnet 4.6은 bare JSON을 반환해 정상 파싱 — 어댑터 결함이 아니라 모델의 지시-이행
실패다. 단 이는 "프로덕션 배선 그대로 평가"라는 벤치마크 원칙(다른 모델과 동일
기준)에 따른 정당한 채점이다(A.X의 이메일 JSON 실패 13/30과 같은 범주).

(지연은 n=324 확장본 `phase9_verified_stats_11model.json::latency.scored_common_*`의
**scored 공통 세트 기준**이다. GLM `all_rows_p50`의 zero-latency 행(all_rows_n=420 중
165행)은 deferred/skipped artefact이며 GLM은 실측 최저속이다.)

**주요 쌍대 McNemar (공통 324, exact 2-sided) — 하이라이트 + 순위변동:**

| 쌍 | a만 정답 | b만 정답 | McNemar p | bootstrap Δ CI95 | 유의 |
|---|---|---|---|---|---|
| **DeepSeek vs EXAONE** | EX 6 / DS 14 | — | **0.1153** | [−0.052, 0.003] | 아니오 (동률) |
| DeepSeek > A.X | DS 24 / AX 3 | | **4.9e-5** | [−0.096, −0.034] | 예 |
| DeepSeek > Solar | DS 15 / Solar 1 | | **0.0005** | [−0.068, −0.022] | 예 |
| DeepSeek > GPT-4o | DS 14 / GPT 1 | | **0.0010** | [−0.065, −0.019] | 예 |
| DeepSeek > baseline | DS 15 / base 1 | | **0.0005** | [−0.068, −0.022] | 예 |
| **DeepSeek vs GLM-5.2** | GLM 6 / DS 10 | | **0.4545** | [−0.037, 0.012] | 아니오 (동률·n=145엔 유의) |
| DeepSeek > **DeepSeek V4 Pro** | DS 14 / V4 4 | | **0.0309** | [0.006, 0.059] | 예 |
| **EXAONE > A.X** | EX 21 / AX 8 | | **0.0241** | [0.009, 0.074] | 예 |
| **GPT-4o vs baseline** | GPT 9 / base 8 | | **1.0000** | [−0.028, 0.022] | 아니오 (동률) |
| EXAONE vs baseline | EX 14 / base 8 | | 0.2863 | [−0.009, 0.046] | 아니오 |
| EXAONE vs GPT-4o | EX 14 / GPT 9 | | 0.4049 | [−0.012, 0.043] | 아니오 |
| EXAONE vs GLM-5.2 | EX 9 / GLM 13 | | 0.5235 | [−0.040, 0.015] | 아니오 |

**프론티어 추가 2모델(GPT-5.3·V4 Pro) 쌍대 (공통 324, 동일 검정) — 상위권 동률 + 신규 유의:**

| 쌍 | a만 정답 | b만 정답 | McNemar p | bootstrap Δ CI95 | 유의 |
|---|---|---|---|---|---|
| **DeepSeek vs GPT-5.3** | DS 9 / GPT-5.3 4 | — | **0.2668** | [−0.006, 0.037] | 아니오 (동률) |
| **EXAONE vs GPT-5.3** | EX 9 / GPT-5.3 12 | — | **0.6636** | [−0.037, 0.019] | 아니오 (동률) |
| GPT-5.3 > baseline | GPT-5.3 13 / base 4 | | **0.0490** | [0.003, 0.052] | 예 (신규) |
| GPT-5.3 vs GPT-4o | GPT-5.3 13 / GPT-4o 5 | | 0.0963 | [0.000, 0.052] | 아니오 |
| GPT-5.3 vs GLM-5.2 | GLM 9 / GPT-5.3 8 | | 1.0000 | [−0.022, 0.028] | 아니오 (동률) |
| GPT-5.3 > Solar | GPT-5.3 13 / Solar 4 | | **0.0490** | [0.003, 0.052] | 예 |
| GPT-5.3 > A.X | GPT-5.3 22 / AX 6 | | **0.0037** | [0.019, 0.083] | 예 |
| GPT-5.3 vs DeepSeek V4 Pro | GPT-5.3 11 / V4 6 | | 0.3323 | [−0.009, 0.040] | 아니오 |
| EXAONE vs DeepSeek V4 Pro | EX 11 / V4 9 | | 0.8238 | [−0.022, 0.034] | 아니오 |
| GPT-4o vs DeepSeek V4 Pro | GPT-4o 2 / V4 5 | | 0.4531 | [−0.025, 0.006] | 아니오 |
| baseline vs DeepSeek V4 Pro | base 6 / V4 10 | | 0.4545 | [−0.037, 0.012] | 아니오 |
| Solar vs DeepSeek V4 Pro | Solar 6 / V4 10 | | 0.4545 | [−0.037, 0.012] | 아니오 |
| A.X vs DeepSeek V4 Pro | AX 9 / V4 20 | | 0.0614 | [−0.068, −0.003] | 아니오 (경계) |

**9모델 전체 36쌍 중 유의쌍이 n=145의 8개 → n=324의 11개**로 늘었다: DeepSeek(V3.2)이
5모델(Solar/A.X/baseline/GPT-4o/V4 Pro)을 이기고(**n=145에서 이기던 GLM은 GLM이 3위로
오르며 이제 동률 p=0.454**), GPT-5.3이 Solar/A.X/baseline 셋, GLM·GPT-4o가 각각 A.X를
이기고 GLM>GPT-4o, EXAONE>A.X다. GPT-5.3은 최상위 DeepSeek(p=0.267)·EXAONE(p=0.664)과
동률, DeepSeek V4 Pro는 전작 V3.2에 유의 열세(p=0.031)이며 나머지와는 전부 동률이다.
(9모델 36쌍은 `phase6_verified_stats_9model.json::pairwise_mcnemar_on_common`; n=324
병합은 phase9.)

**Anthropic 2종 쌍대 (공통 324, 동일 검정):** Claude Sonnet 4.6은 상위 클러스터 전부와
통계 동률이다 — vs DeepSeek V3.2 p=0.774, vs GLM 0.688, vs GPT-5.3 0.607, vs EXAONE
0.286 — 대신 중위권 4모델(A.X/Solar/baseline/GPT-4o)은 전부 p<0.05로 이긴다. Claude
Haiku 4.5는 전 모델 대비 유의 열세다 — vs A.X p=2.9e-16(−26.2pp), vs Solar
p=2.4e-21(−28.4pp)로 최약체 국내 모델에게조차 압도적으로 진다(기전은 각주 ²의 fence
파싱 실패). **11모델 전체 55쌍 중 유의쌍은 25개(Haiku 10쌍 포함)**로, n=145의 소수에서
크게 늘었다 — `phase9_verified_stats_11model.json::pairwise_mcnemar_on_common`.

### 6.1 Task별 분해표 (pass/n, 공통 324)

(열 순서 = n=324 종합 순위. Sonnet 4.6 = 2위, GLM-5.2 = 3위, GPT-5.3 = 4위, EXAONE
= 5위, DeepSeek V4 Pro = 6위, Haiku 4.5 = 최하위 11위.)

| task | 성격 | n | DeepSeek | Sonnet 4.6 | GLM-5.2 | GPT-5.3 | EXAONE | V4 Pro | GPT-4o | baseline | Solar | A.X | Haiku 4.5 | 검정력 플래그 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **t3** structured NL→SQL | exact | 69 | 57 | 57 | 57 | 57 | 55 | 57 | 57 | 54 | 54 | 49 | **0** | 채점기 긴장(§5.2c) — Haiku 0/69 각주 ² fence 전멸 |
| **t5** router route | exact | 76 | 64 | 64 | 64 | 63 | 62 | 63 | 63 | 63 | 62 | 64 | 62 | 포화; known 12건 공통 |
| **t6** router intent | exact | 47 | 40 | 40 | 40 | 40 | 39 | 40 | 40 | 39 | 40 | 40 | 40 | 포화; known 7건 공통 |
| **t7** decompose | exact | 46 | **32** | 25 | 24 | 30 | 30 | 26 | 24 | 31 | 30 | 24 | 25 | known prefilter 14건 공통. DeepSeek 최고(32) |
| **t9** graph bind | exact | 32 | 32 | 32 | 32 | 32 | 30 | 32 | 32 | 32 | 32 | 32 | **1** | 포화(EXAONE 2 fail; Haiku 1/32는 각주 ² fence) |
| **t10** delegation | exact | 54 | 45 | **50** | 49 | 43 | 46 | 42 | 41 | 37 | 38 | 40 | 36 | 존칭 채점기 보정 반영. Sonnet 최고(50), GLM·EXAONE 상위 |
| **합** | | **324** | **270** | **268** | **266** | **265** | **262** | **260** | **257** | **256** | **256** | **249** | **164** | |
| t2 wiki QA | **judge** | 0 (deferred) | — | — | — | — | — | — | — | — | — | — | — | 미채점(§6.2 error 잠식) |
| t8 decompose synth | **judge** | 0 (deferred) | — | — | — | — | — | — | — | — | — | — | — | 정성 전용, 미채점 |

프론티어 추가 2모델 task별 fail(n=324): **GPT-5.3** t3=12·t5=13·t6=7·t7=16·t10=11(t9
만점) — t7 fail 16 중 14는 known prefilter, 2건은 borderline-decompose 모델 편차다.
**DeepSeek V4 Pro** t3=12·t5=13·t6=7·t7=20·t10=12 — reasoning 모델임에도 t7 decompose
26/46로 비-Haiku 최하위라 순위를 못 올린다.

Anthropic 2종 task별(n=324): **Claude Sonnet 4.6** fail은 t3=12·t5=12·t6=7·t7=21·t10=4로
상위권과 같은 분포이며 t9 만점·**t10 50/54(전 11모델 최고)**다. **Claude Haiku 4.5**는
JSON 파싱이 걸리는 t3(0/69)·t9(1/32)에서 각주 ²의 fence 기전으로 전멸한 반면, 파싱
실패가 개입하지 않는 t5/t6에서는 62/76·40/47로 정상권이다 — 점수 붕괴가 능력이 아니라
포맷 지시-이행에서 왔다는 분해 증거다.

읽는 법:

- **DeepSeek의 우위는 t7 decompose(32/46, 전 11모델 최고) + t3/t10 고른 상위**에서
  나온다. **t10 최고는 Sonnet 4.6(50/54)**, EXAONE(46/54)·GLM(49/54)도 상위다(§5.2b
  존칭 보정 효과 포함). t5/t6/t9는 전 모델 포화라 변별력이 낮다 — 순위는 사실상
  t3(A.X 열세)·t7·t10 세 작업이 가른다.
- **GPT-4o가 baseline을 못 이기는 것도 여기서 보인다:** t7에서 GPT-4o 24/46로
  DeepSeek 32·baseline 31보다 낮은데, t10(41 vs 37)에서 baseline을 되받아 총합은
  257 vs 256 dead-tie(§6.0)다.

### 6.2 측정 이력 — Phase 6 실행 조건 차이 + error triage

Phase 6 3모델(+ 9모델 확장분 2모델 + 11모델 확장분 Anthropic 2모델)은 실행 조건이
서로 달랐고, 비교 유효성에 영향이 없음을 확인해 기록한다.

- **(9모델 확장, 2026-07-21) GPT-5.3 temperature=0 거부 → 어댑터 옵셔널 temperature
  파라미터 추가.** `openai:gpt-5.3-chat-latest`는 OpenAI API가 `temperature=0`을
  HTTP 400으로 거부하고 벤더 기본값 `temperature=1` 고정만 허용한다. `OpenAIChat`
  어댑터가 기본으로 `temperature=0`을 보내 이 모델만 전량 실패했으므로, 어댑터
  (`orthus/models/adapters/openai_compat.py`)에 **옵셔널 `temperature: float | None`
  파라미터**를 추가해 `None`이면 요청 body에서 필드를 생략하도록 하위호환으로 고치고
  (기본값은 여전히 `0.0` — 기존 8모델 무영향), 하네스(`harness_e2e.py`)는
  **gpt-5.3 슬러그에 대해서만** `temperature=None`을 넘겨 파라미터를 생략했다. 결과적
  으로 GPT-5.3만 `temperature=1`(벤더 기본), 나머지 8모델은 `temperature=0`으로
  실행됐다 — §6.0/순위표의 공정성 캐비앗으로 명시한다.
- **(9모델 확장) 신규 2모델 실행 조건.** GPT-5.3·DeepSeek V4 Pro 모두 tier A 채점
  슬라이스(t3/t5/t6/t7/t9/t10)를 populated `orthus_company` DB + 프로덕션 배선(§5
  공정 비교 조건) 그대로 실행했다. error 행 0, empty-output은 전부 t10
  expected-null(오염 없음)이고, 골든 확장 재측정에서 `n_common=324`로 전 모델 동일
  id-set에서 paired 비교된다. DeepSeek V4 Pro는 hybrid reasoning(thinking 기본)
  모델이라 지연이 scored p50 2481ms / p95 7586ms로 GLM-5.2 다음으로 느렸다.
- **(11모델 확장, 2026-07-21) Anthropic Claude 2종 실행 조건.** 원래 Bedrock 5종은
  키 인증 실패로 보류였으나(아래 "모델 조달 경로") 새 bearer key로 해소돼,
  **Claude Sonnet 4.6**(`bedrock:anthropic.claude-sonnet-4-6` → 실제 전송 ID
  `us.anthropic.claude-sonnet-4-6`)과 **Claude Haiku 4.5**
  (`bedrock:anthropic.claude-haiku-4-5-20251001-v1:0` →
  `us.anthropic.claude-haiku-4-5-20251001-v1:0`)를 기존 하네스
  `_build_bedrock_chat` 재사용으로 측정했다 — **코드 수정 0줄**. 실행 조건은 기존
  신규 모델과 동일: `--tier A --layer all --tasks t3,t5,t6,t7,t9,t10
  --final-verify`, populated `orthus_company` DB, `temperature=0`, region
  `us-east-1`, bearer-key 인증. error 행 0, `model.fallback` spans 0(CLEAN). 골든
  확장 재측정에서 Anthropic 2종도 tier A 전량을 재실행해 `n_common=324` 병합에 합류했고,
  최종 병합 통계는 `phase9_verified_stats_11model.json`(phase8은 n=145 이전 버전).

- **실행 범위 차이:** GPT-4o / DeepSeek은 **전량(tier A+B)** 실행, **GLM-5.2는
  tier A 한정** 실행($10 비용 제약 소진 사건으로 tier B 미실행). 그러나 채점
  세트는 tier A 전용 324건(확장 후)이고 11모델의 채점 id-set이 완전 동일하므로 **GLM도
  공정 비교된다** — GLM의 all-rows 지표(zero-latency 행)만 tier A 한정의 artefact다.
- **모델 조달 경로:** DeepSeek은 팀 Bedrock 카탈로그에 없어 **공식 DeepSeek API**로
  전환해 측정했다(deepseek-chat). Bedrock 5종은 당시 키 인증 실패로 보류였으나,
  **이후 2026-07-21 bearer key 확보로 Claude Sonnet 4.6/Haiku 4.5 측정 완료**
  (§6.0, 위 11모델 확장 bullet). 나머지 Bedrock 3종(Llama 70B/8B, Nova Pro)은
  미측정.
- **error triage — n=324 재측정은 clean:** 골든 확장 재실행의 `phase9::error_triage`는
  **비어 있다(11모델 error 0)**. (Phase 6 원 런에서 관측됐던 3모델 429 rate-limit
  — GPT-4o tier-B `chat/completions`, GLM/DeepSeek의 t2 judge OpenAI 임베딩 429 —
  은 전부 deferred/judge 영역 국한이라 scored 세트 무손상이었고, 재측정에서는 error
  자체가 0으로 나왔다.) 다만 t2 judge·tier-B 생성형/judge 판정은 여전히 이 런 범위
  밖이다(Phase 5와 동일한 미채점 한계 연장).
- **known-fail 상수 오프셋 유지 (8건 → 45건):** `all_models_fail_all_8=true` — 11모델
  전부 45/45 실패(결정론 fast-path), 모델당 −45 상수 오프셋이라 순위·McNemar 비차별적.
  tier-A 채점 세트 이론상한 = **279/324**(§5.2d).
- **채점 조건 정합:** P5 4모델은 Phase 6/재측정과 동일 조건으로 맞췄다 — t3 tier-A는
  `t3_rerun_orthus_company/` 재실행분, t10은 존칭-정규화 재채점(원본 jsonl 무수정).
  n=324 재현 정확도(EXAONE .8086 / baseline .7901 / Solar .7901 / A.X .7685)가
  `phase9_verified_stats_11model.json`과 완전 일치 확인.

### 6.3 빈 응답 오염 검사 (11모델)

채점 세트 n=324 내 "빈 출력"(None/""/[]/{}) 행 전수 카운트
(`empty_content_check.n_empty_output_in_common_scored`):

| model | 빈 출력 수 | 전부 fail? | 성격 |
|---|---|---|---|
| ax 23 / gpt-5.3 26 / baseline 28 / solar·deepseek·gpt-4o 29–30 / exaone·sonnet 32 / glm·v4-pro 33 | 23–33 | 아니오 | t10 **expected-null**(위임 수신자 없음 = 빈 출력이 정답)이 t10 22→54 확장으로 늘어난 것 |
| **haiku 4.5** | 54 | 아니오 | t10 전건 empty(36 pass=expected-null / 18 fail) — 각주 ² fence 계열 지시-이행 붕괴의 t10 발현(§6.0) |

비-Haiku 10모델의 빈 출력은 전부 t10 expected-null(담당자를 뽑지 않는 것이 정답)이며
`empty_are_all_fail=false`다. **10모델 어디에도 GLM류 empty-response 오염은 없다** —
빈 출력은 정상 null 정답 처리다(GLM-5.2 특히 주의 확인, 오염 신호 없음). Haiku만 t10
전건 empty로 갈리는데, 이는 truncation 오염이 아니라 §6.0 각주 ²의 포맷 지시-이행
붕괴가 t10에서도 나타난 것이다.

### 6.4 Phase 6 종합 판정 (11모델, n=324)

- **모델 순위 근거:** DeepSeek이 최상위이나 상위 클러스터 4모델(Sonnet·GLM·GPT-5.3·
  국내 EXAONE)과 **전부 통계 동률**(DeepSeek vs EXAONE p=0.115, vs Sonnet 0.774, vs GLM
  0.454, vs GPT-5.3 0.267)이고 이 5모델이 한 클러스터다. **🔑 표본 확대(n=145→324)로 이
  최상위 클러스터가 중위권(A.X/Solar/baseline/GPT-4o)과 유의하게 분리**됐다 — n=145에서
  동률이던 여러 쌍이 갈렸다. 국내 3모델은 A.X 열세(EXAONE vs A.X p=0.024) 외 상호 동률.
  대형 GPT-4o는 mini와 동률(257 vs 256), DeepSeek V4 Pro는 전작 V3.2에 유의 열세
  (p=0.031)이며 6위다.
- **비교 유효성:** error 0(phase9 error_triage 비어 있음), 채점 324 id-set 11모델
  동일(`n_common=324`), known-fail 45 상수 오프셋 동형, 빈 응답 오염 0 → **유효**.
  실행 범위 차이(GLM tier A 한정, DeepSeek 공식 API 전환)와 골든 확장 재실행은 채점
  세트 정합에 영향 없음. **단 GPT-5.3만 `temperature=1`(벤더 강제)로 실행된 캐비앗**과
  GPT-5.3이 OpenAI deprecated 모델(2026-08경 shutdown 예정)이라는 점을 함께 명시한다.
- **한계:** t2 wiki QA judge와 tier-B t5/t6는 미측정(생성형/tier-B 판정은 Phase 5·6
  공통 범위 밖), L2 모델-변별 62 pending도 미해소. Bedrock 5종 중 Claude Sonnet 4.6/
  Haiku 4.5는 측정 완료(§6.0/§6.2), 나머지 3종(Llama 70B/8B, Nova Pro)은 미측정.
- **대회 메시지:** **국내 EXAONE이 대형 프론티어 4종(DeepSeek·Sonnet·GLM·GPT-5.3)과
  통계 동률의 최상위 클러스터**에 있고, 국내 3모델은 A.X 열세 외 상호 동률이며, 대형이
  항상 이기는 것도 아니다(GPT-4o가 mini에, DeepSeek V4 Pro가 전작 V3.2에 못 미침).
  검정력을 높이자 상위↔중위 계층은 드러났으나 상위 클러스터 내부(EXAONE 포함) 동률과
  슬롯 배정 근거(**벤더 금지·지연·안전**)는 유지된다.

---

## Phase 7 — 오케스트레이션 합성 vs 단일 대형모델 (11모델, 골든 확장 n=324, 측정일 2026-07-21)

**권위 SoR:** n=324 per-task는 `phase9_verified_stats_11model.json::per_task_on_common`,
조립 정의·V1/V2 대비는 `orchestration_composite_slot_swap_exp.json`(assignment 정의
SoR). 스크립트는 `combine_stats.py`의 소스 로딩(`KNOWN_SOURCES` + t10 존칭 재채점)과
`runner_lib.py`의 `mcnemar_from_correct`/`bootstrap_paired_diff_ci`(n_resamples=10000,
seed=1234)를 **재구현 없이 그대로 재사용**한다. LLM 호출 0·추가 비용 0. Phase 5/6의
**corrected·확장 채점 데이터를 그대로 사후 조립**해 계산했다. 두 조립 정의:

- **V1 = 현행 프로덕션 배정**(`docs/model-orchestration.md` §15 다양화): t3=solar,
  t5=exaone, t6=solar, t7=exaone, t9=ax, t10=exaone → **264/324 = 81.48%**(n=145: 118/145 = 81.38%).
- **V2 = 국내 슬롯별 최강 조립**: t3=exaone, t5=ax, t6=solar, t7=exaone, t9=solar,
  t10=exaone → **267/324 = 82.41%**(n=145: 121/145 = 83.45%).

이 벤치마크의 핵심 질문 — **"슬롯별로 잘하는 국내모델을 조립한 오케스트레이션 합성이
단일 대형모델 대비 어느 정도인가"** — 의 n=324 결론: **합성(V1·V2)은 어느 대형모델과도
통계적으로 동률**이며(V1 vs 최강 DeepSeek p=0.238, vs Sonnet 0.503, vs GLM 0.824, vs
GPT-5.3 1.000, vs EXAONE 0.688; V2 vs DeepSeek 0.607), **V1 vs V2도 여전히 비유의
(p=0.375)** — 즉 t3/t5/t9 다양화가 검정력을 높여도 유의차를 만들지 않았다. **🔑 유일한
새 유의 결과: V2 조립이 gpt-4o-mini baseline을 새로 유의하게 앞선다(p=0.0192)** — n=145에선
비유의였고, V1은 여전히 baseline과 동률(p=0.134)이다. 유일하게 동률권 밖의 최하위는
포맷 지시-이행 실패로 무너진 Claude Haiku 4.5(50.62%, §6.0 각주 ²)다. 정확도는 노이즈,
실이득은 지연(대형 대비 3~12배 빠름)과 안전·벤더 준수다. 즉 골든 확장의 핵심 결론:
**"조립 ≈ 최강 대형모델" 동률과 "V1 ≈ V2" 동률은 검정력을 높여도 유지됐고, "V2 조립 >
baseline"만 새로 유의해졌다.**

### 7.0 방법론 — 사후 조립 가상 점수임을 먼저 명시 (정직성)

- **무엇인가:** 프로덕션 슬롯 배정(`orthus/models/orchestration.py::ASSIGNMENTS`,
  `docs/model-orchestration.md` §15 다양화 배정 = V1) 또는 국내 슬롯별 최강 조립(V2)을
  적용해, **공통 채점 324건 각각에서 "그 task 담당 배정 모델"의 pass/fail만 뽑아** 하나의
  가상 시스템 correct 벡터를 만든 것이다.
- **무엇이 아닌가:** 슬롯별 배정 모델을 한 요청 안에서 실제로 라우팅해 돌린 **통합 실행이
  아니다.** 각 모델은 이미 독립적으로 전체 세트를 돌렸고(Phase 5/6), 여기서는 각
  item을 배정표에 맞는 모델의 이미-측정된 결과로 **사후 스티칭**했다. 슬롯 간
  fallback·retry·컨텍스트 상호작용은 반영되지 않는다.
- **왜 성립하나:** 각 task는 정확히 한 슬롯에 대응하고 배정은 결정론 상수 테이블
  (확신도 routing 아님)이라 "item → 담당 모델"이 유일 결정 → per-item 정답 여부가
  모호성 없이 정의되고 paired McNemar/부트스트랩 CI가 성립한다.

### 7.1 task → 슬롯 → 배정 모델 (하네스 `dispatch_l1` 호출 함수 기준, 유일 확정)

| task | 호출 함수 | 슬롯 | V1 배정 | V2 배정 | scored n |
|---|---|---|---|---|---|
| t3 | `query_structured` | structured | **solar** | exaone | 69 |
| t5 | `router/route.py::classify` | routing | **exaone** | ax | 76 |
| t6 | `router/route.py::classify_intent` | intent | **solar** | solar | 47 |
| t7 | `should_decompose` | decompose | **exaone** | exaone | 46 |
| t9 | `bind_graph_params` | graph_bind | **ax** | solar | 32 |
| t10 | `extract_delegation_intent` | delegation_extract | **exaone** | exaone | 54 |

t5/t6는 같은 `router/route.py`지만 함수가 달라(t5=`classify`, t6=`classify_intent`)
슬롯이 갈리므로 감도 분기 없이 단일 매핑으로 확정된다. V1은 현행 프로덕션 다양화 배정,
V2는 슬롯별 국내 최강 조립이다(t3/t5/t9만 다름).

### 7.2 합성 시스템 정확도 — V1 264/324 = 81.48%, V2 267/324 = 82.41%

| task | V1 배정 | V1 pass/n | V2 배정 | V2 pass/n | (참고) EXAONE | (참고) Solar |
|---|---|---|---|---|---|---|
| t3 | solar | 54/69 | exaone | 55/69 | 55/69 | 54/69 |
| t5 | exaone | 62/76 | ax | 64/76 | 62/76 | 62/76 |
| t6 | solar | 40/47 | solar | 40/47 | 39/47 | 40/47 |
| t7 | exaone | 30/46 | exaone | 30/46 | 30/46 | 30/46 |
| t9 | ax | 32/32 | solar | 32/32 | 30/32 | 32/32 |
| t10 | exaone | 46/54 | exaone | 46/54 | 46/54 | 38/54 |
| **합** | | **264/324** | | **267/324** | 262/324 | 256/324 |

**정직한 관찰:** V1 합성(264)은 단일 최고 국내 EXAONE(262)과 **통계 동률**(p=0.688, +2 items:
t9에서 A.X가 EXAONE 대비 +2, t6에서 solar +1, t3에서 solar −1로 대체로 상쇄). V2(267)는
슬롯별 국내 최강을 취해 EXAONE보다 +5(p=0.0625, 경계)이나 여전히 유의 문턱 아래다. 즉 슬롯
조립은 이 세트에서 **최고 단일 국내모델을 앙상블로 유의하게 앞지르지는 못하되, 동률로
재현하면서 각 슬롯의 최속·최안전 모델을 함께 취한다.**

### 7.3 대표 순위 — 오케스트레이션 합성을 대형/프론티어 모델과 한 순위표에서 (n=324)

합성을 "Δ 대비표"가 아니라 **하나의 시스템 행**으로 프론티어 모델들 사이에 편입시켰다.
국내 3모델 단독 전량 실행은 합성의 구성 재료이므로 여기서 빼고 아래 7.3a 참고표로 분리한다.

| 순위 | 시스템 | 정확도 | pass/n |
|---|---|---|---|
| 1 | DeepSeek V3.2 | **83.33%** | 270/324 |
| 2 | Claude Sonnet 4.6 (bedrock) | **82.72%** | 268/324 |
| 3 | **오케스트레이션 합성 V2 (국내 최강 조립)** | **82.41%** | 267/324 |
| 4 | GLM-5.2 | **82.10%** | 266/324 |
| 5 | gpt-5.3-chat-latest | **81.79%** | 265/324 |
| 6 | **오케스트레이션 합성 V1 (현행 프로덕션)** | **81.48%** | 264/324 |
| 7 | DeepSeek V4 Pro | 80.25% | 260/324 |
| 8 | GPT-4o | 79.32% | 257/324 |
| 9 | baseline (gpt-4o-mini) | 79.01% | 256/324 |
| 10 | Claude Haiku 4.5 (bedrock) | 50.62% | 164/324 |

- **두 합성(V2 3위·V1 6위)은 순위표의 모든 프론티어 대형과 통계 동률**이다 — V1 vs
  DeepSeek p=0.238, vs Sonnet 0.503, vs GLM 0.824, vs GPT-5.3 1.000; V2 vs DeepSeek 0.607,
  vs Sonnet 1.000, vs GLM 1.000, vs GPT-5.3 0.814. 두 합성의 −0.9~−1.9pp(V1) / +0.3pp
  이내(V2)는 노이즈 범위이며 어느 프론티어에도 확정 열세가 아니다.
- **🔑 골든 확장의 새 결과: V2 합성이 gpt-4o-mini baseline을 유의하게 앞선다(p=0.0192)** —
  n=145에선 비유의였다. V1은 여전히 baseline과 동률(p=0.134)이고, **V1 vs V2도 비유의
  (p=0.375)**라 다양화(V1)와 국내-최강(V2)의 선택은 통계적으로 무차별하다.
  → **국내 슬롯 조립 두 변형이 프론티어 대형 순위표 상위권에 편입, 최상위 그룹과 동률**이다.
- 최하위 Claude Haiku 4.5(50.62%)만 순위표에서 유일하게 크게 밑돈다 — §6.0 각주
  ²의 포맷 지시-이행 실패(코드펜스)로 t3/t9가 전멸한 결과다.

### 7.3a (참고) 조립 구성요소의 단독 성적 — 국내 3모델 전량 실행

합성이 조립해 쓴 국내 3모델을 **각각 전 세트 단독 실행**한 결과다(합성의 재료이지 경쟁
시스템이 아니라 대표 순위표에서 분리). 합성과의 paired McNemar 포함:

| 국내 단일모델 | 정확도 | pass/n | V1 대비 | V2 대비 | McNemar p (합성 기준) | 판정 |
|---|---|---|---|---|---|---|
| EXAONE | 80.86% | 262/324 | V1 +2(264) | V2 +5(267) | V1 0.688 / V2 0.0625 | 동률 (V2는 경계) |
| Solar | 79.01% | 256/324 | V1 +8 | V2 +11 | n=324 별도 재검정 미산출¹ | (n=145 유의) |
| A.X | 76.85% | 249/324 | V1 +15 | V2 +18 | n=324 별도 재검정 미산출¹ | (n=145 유의) |

¹ phase9 병합 pairwise는 단일-대-단일만 담고 합성-대-Solar/A.X paired McNemar는 별도
산출하지 않았다(n=145에선 각각 p=0.031·0.012로 합성 유의 우세였다). n=324 정확도 서열
(합성 > EXAONE ≥ Solar > A.X)은 그대로다.

- **최고 단일 국내 EXAONE과 합성은 통계 동률**(V1 264 vs 262 p=0.688, V2 267 vs 262
  p=0.0625 경계) — 슬롯 조립의 **앙상블 정확도 이득은 유의하지 않다**(정직 명시). 합성의
  값은 정확도가 아니라 지연·안전·벤더 준수다(7.4/7.5).
- **"슬롯별 배정 > 어느 단일 국내모델 통짜"의 방향(합성이 Solar·A.X를 상회)은 n=324에서도
  정확도 서열로 유지**되나, 그 쌍의 유의 재검정은 이 병합에서 별도 산출하지 않았다.

### 7.3b 합성 vs 단일 시스템 McNemar (n_paired=324)

합성(V1·V2)을 기준으로 한 paired McNemar p다. **phase9 병합은 단일-대-단일 discordant/CI만
담으므로 합성-대-단일 쌍은 findings 재집계 p만 노출한다**(discordant/CI 미산출).

| 상대 | 상대 정확도 | V1(264) vs 상대 p | V2(267) vs 상대 p | 유의 |
|---|---|---|---|---|
| **DeepSeek V3.2**(최강 대형) | 0.8333 | **0.238** | **0.607** | 아니오 (동률) |
| Claude Sonnet 4.6 | 0.8272 | 0.503 | 1.000 | 아니오 (동률) |
| GLM-5.2 | 0.8210 | 0.824 | 1.000 | 아니오 (동률) |
| gpt-5.3-chat-latest | 0.8179 | 1.000 | 0.814 | 아니오 (동률) |
| EXAONE(단일 최고 국내) | 0.8086 | 0.688 | 0.0625 | 아니오 (동률) |
| baseline(gpt-4o-mini) | 0.7901 | 0.134 | **0.0192** | V1 아니오 / **V2 예** |
| **합성 V1 vs V2** | — | (V1+1 / V2+4) | **0.375** | 아니오 (동률) |

합성이 유의하게 이긴 상대는 **V2 vs baseline(p=0.0192) 하나뿐**이고, 프론티어 5모델·EXAONE과는
V1·V2 모두 전부 통계 동률이며, V1 vs V2도 비유의(p=0.375)다.

### 7.3b 슬롯 최적성 — 배정 모델 vs 그 task 최강 모델

각 슬롯(V1)이 고른 모델이 그 task에서 (a) 국내 3모델 중 최강이었는지, (b) 11모델 전체
최강과 얼마나 벌어졌는지. §15 배정은 **의도적 다양화**라 항상 국내 최강을 고르지 않는다(§7.1).
(11모델 풀 기준. 골든 확장으로 task별 최강값이 커졌다: t3 최강 57, t10 최강 Sonnet 50.)

| task | 슬롯 | V1 배정 | 배정 pass/n | 국내 최강 | 국내최강 여부 | 11모델 최강 | 전체최강 대비 gap |
|---|---|---|---|---|---|---|---|
| t3 | structured | solar | 54/69 | exaone 55 | ✗ (−1) | 57 (deepseek 등 5종) | −3 |
| t5 | routing | exaone | 62/76 | ax 64 | ✗ (−2) | 64 (ax·glm·sonnet·deepseek) | −2 |
| t6 | intent | solar | 40/47 | 40 (solar·ax) | ✓ | 40 (다수) | 0 |
| t7 | decompose | exaone | 30/46 | 30 (exaone·solar) | ✓ | 32 (deepseek) | −2 |
| t9 | graph_bind | ax | 32/32 | 32 (ax·solar) | ✓ | 32 (다수) | 0 |
| t10 | delegation_extract | exaone | 46/54 | exaone 46 | ✓ | Sonnet 50 | −4 |

- **6 슬롯 중 4개(t6·t7·t9·t10)는 국내 최강을 골랐고**, t6·t9는 11모델 전체 최강과도
  동률이다. 단 **t10은 n=145에선 exaone이 전체 유일 최강(21/22)이었으나, n=324에서 Sonnet
  50/54가 앞서** 이제 전체최강이 아니다(−4).
- **t3·t5 두 슬롯은 §15 다양화로 국내 최강을 일부러 양보**했다(t3 solar 54 vs exaone 55,
  t5 exaone 62 vs ax 64).
- **참고(정직):** 다양화를 버리고 슬롯마다 국내 최강만 조립한 것이 V2 = 55+64+40+30+32+46 =
  **267/324 = 82.41%**로, 최강 대형 DeepSeek V3.2(270)와 **통계 동률**(p=0.607)이다. §15가
  V1(다양화)을 취한 이유는 벤치마크 스토리상 **task-aware 멀티모델 오케스트레이션 자체를
  보여주려 동점 구간 안에서 모델을 분산**시킨 것이며(코드 주석 명시), V1 vs V2가 비유의
  (p=0.375)이므로 이 양보는 성능 손해가 아니라 노이즈 구간 안의 선택이다. **오라클 상한**
  (11모델 per-item any-correct)은 **279/324 = 86.11%**(= §5.2d known-fail 45 제외분)로,
  정적 라우팅으로는 도달 불가한 천장 맥락일 뿐이다.

### 7.4 지연 관점 — 합성의 실이득 (scored 공통 324, item별 담당 모델 실측 지연)

n=324 scored 공통 p50/p95(`phase9::latency.scored_common_*`). 합성 종합 p50은 이 병합에서
별도 산출하지 않았으나, V1 슬롯 모델은 EXAONE·Solar·A.X뿐이라 합성 p50은 이 국내 최속권
밴드(295–681ms)에 놓이고 대형 대비 훨씬 빠르다.

| 시스템 | scored p50 (ms) | scored p95 (ms) |
|---|---|---|
| EXAONE(V1 슬롯) | **295** | 1447 |
| A.X(V1 슬롯) | 663 | 1998 |
| Solar(V1 슬롯) | 681 | 2803 |
| baseline | 780 | 1579 |
| DeepSeek | 815 | 1250 |
| GPT-4o | 853 | 1982 |
| gpt-5.3-chat-latest | 1855 | 5749 |
| Claude Sonnet 4.6 | 1983 | 3435 |
| DeepSeek V4 Pro | 2481 | 7586 |
| GLM-5.2 | **3591** | **18319** |

**합성은 DeepSeek(815ms)과 정확도 동률이면서 슬롯 모델 p50이 295–681ms로 대형 대비 3~12배
빠르다**(GLM 3591ms 대비 5~12배).

### 7.5 Phase 7 종합 판정 (11모델, 골든 확장 n=324)

**골든셋을 145→324로 2배 늘린 재측정의 중심 결론:**

- **✅ HELD — "조립 ≈ 최강 대형모델" 동률.** 합성 V1 81.48%(264)·V2 82.41%(267)는 **11모델
  어느 대형(DeepSeek·Sonnet·GLM·GPT-5.3·V4 Pro·GPT-4o)과도 통계 동률**이고 최고 단일 국내
  EXAONE과도 동률(V1 p=0.688)이다. 검정력을 높여도 조립이 최강 단일 대형에 유의하게 밀리지
  않는다는 핵심 결론은 그대로다.
- **✅ HELD — "V1 ≈ V2" 조립 동률.** t3/t5/t9 다양화(V1)와 국내-최강 조립(V2)의 차이는
  n=324에서도 **비유의(McNemar p=0.375, V1+1/V2+4)** — 표본을 2배로 늘려도 두 배정은 통계적으로
  무차별하다.
- **🔑 NEW — "V2 조립 > gpt-4o-mini baseline" 신규 유의(p=0.0192).** n=145에선 비유의였다.
  V1은 여전히 baseline과 동률(p=0.134)이므로, 국내-최강 조립(V2)만 검정력 확대로 baseline을
  유의하게 앞서게 됐다.
- **🔑 NEW — 상위 클러스터가 중위권과 유의 분리(§6.0/§6.4).** 최상위 5모델(DeepSeek·Sonnet·
  GLM·GPT-5.3·국내 EXAONE)이 중위권(A.X/Solar/baseline/GPT-4o)과 새로 갈렸지만, **상위 클러스터
  내부(EXAONE 포함)는 여전히 상호 동률**로 남았다 — 대회 메시지("국내 EXAONE이 프론티어와
  동률 최상위 클러스터")는 오히려 검정력으로 뒷받침된다.
- **교훈(Haiku):** Haiku 4.5는 프론티어 계열 중 **유일하게 유의 열세**(50.62%)인데 원인이
  능력이 아니라 "bare JSON만 반환" 포맷 지시-이행 붕괴다(§6.0 각주 ² — fence 내부 내용은
  정확했으나 프로덕션 파서가 t3/t9 전건 거부). n=145의 42.76%보다 오른 것은 파싱 무관 t5/t6
  확장분 덕이다.
- **슬롯 최적성:** 6 슬롯 중 4개가 국내 최강(t6·t9는 전체 최강권; t10은 n=324에서 Sonnet
  50에 −4로 밀림). 국내-최강 조립 V2 = 267/324 = 82.41%로 최강 대형 DeepSeek(270)와 동률.
  오라클 상한(11모델 any-correct)은 279/324 = 86.11%.
- **지연:** V1 슬롯 모델(EXAONE 295·A.X 663·Solar 681ms) p50로 대형 대비 3~12배 빠름
  (gpt-5.3 1855·sonnet 1983·v4-pro 2481·GLM 3591) → 정확도는 노이즈, 지연은 국내 슬롯 조립이
  명확한 승자. `docs/model-orchestration.md` §11.3b의 "근거는 성능이 아니라 벤더 금지·지연·안전"과 정합.
- **한계(정직, §7.0 유지):** 사후 조립 **가상 점수**이지 통합 실행이 아니다. 슬롯 간
  fallback·컨텍스트 상호작용 미반영, 채점 세트는 L1 read-only exact 6작업 324건(생성형 judge
  t2/t8·L2 62 pending은 범위 밖, Phase 5/6과 동일 한계). 합성-대-Solar/A.X paired McNemar는
  이 병합에서 별도 산출하지 않았다(n=145 유의 관측만 존재). 합성이 EXAONE 통짜와 동률인 점은
  "슬롯 조립 = 앙상블 이득"이 아니라 "동률 재현 + 슬롯별 속도/안전 취득"으로 읽는다.

---

## Phase 8 — 골든 증강 재측정 (n=1,750, 측정일 2026-07-22)

**권위 SoR:** 11모델 병합 통계 `analysis/raw/phase6_verified_stats_expanded.json`
(`n_common_scored=1750`), 조립 비교 `analysis/raw/composite_vs_single_expanded.json`,
슬롯 스왑 `analysis/orchestration_composite_slot_swap_exp.json`, 요약
`analysis/raw/phase6_expanded_summary.json`. 증강 매니페스트 SoR은
`e2e/augment_provenance.json`, 오케스트레이션 진행 로그는 `e2e/AUGMENT_STATE.md`.

Phase 5/6/7이 tier-A를 145→324로 늘린 순수 검정력 재시험이었다면, Phase 8은 골든을 다시
**851→1,884문항으로 증강**해(공통 채점 세트 **n=1,750**) 같은 11모델·같은 프로덕션 배선으로
재실행한 **대규모 재측정**이다. 신규 모델 추가가 아니며, 증강 데이터는 국내 3사·Bedrock
Claude 생성기를 배제하고 **제3벤더(Bedrock Nova Pro/Llama 3.3-70B) + 결정론 + 실로그**로만
만들어 자기채점 편향을 원천 차단했다.

### 8.0 Executive summary — 결론 먼저

- **상위 동률 클러스터가 해소됐다.** n=324에서 프론티어4 + EXAONE이 한 최상위 클러스터로
  묶였으나, n=1,750에서 **EXAONE이 프론티어4(Sonnet 4.6/GPT-5.3/DeepSeek/GLM-5.2)에
  전부 유의 열세**로 갈렸다(vs Sonnet/GPT-5.3/DeepSeek p<1e-4, vs GLM p=0.0002). **프론티어4
  상호는 여전히 무승부**(전 쌍 p≥0.10)다.
- **국내 축 결론(Solar 기준):** Solar(1420/.811)는 **프론티어4엔 유의 열세**(vs Sonnet
  p=0.0002 등)지만 **gpt-4o(p=0.0097)·baseline(p<1e-4)엔 유의 우세**이고 **DeepSeek V4 Pro와는
  동률**(p=0.48)이다. 즉 깨끗한 "국내<외산" 분할이 아니라, 국내 Solar/EXAONE이 **상위-중위
  밴드**에 앉아 프론티어4에만 밀리고 외산 3모델(V4 Pro/GPT-4o/baseline)은 이기거나 동률이다.
- **조립 판정 HELD:** §15 다양화 조립(diversified, 1449/.828)은 **최강 단일 프론티어
  (Sonnet/DeepSeek/GLM/GPT-5.3)와 전부 통계 동률**이고 **최강 단일 국내 Solar에 유의
  우세**(p=0.0019, +1.66pp)다 — 검정력을 5배로 키워도 "조립 ≈ 최강 대형"이 유지된다.
- **⚠️ 신규 발견 — diversification cost가 유의해졌다.** n=1,750에서 순수 best-per-slot
  (all-solar-except-t10, 1466/.838)이 다양화 조립(1449)보다 **유의 우세**(p=7.6e-5, ~1pp).
  n=324까지는 다양화 양보가 노이즈였으나, 이제 Solar가 t3/t5/t9를 명확히 이기면서 §15 의도적
  다양화의 정확도 비용이 통계적으로 드러난다. **`docs/model-orchestration.md` §15 ASSIGNMENTS
  변경은 owner 게이트이므로 본 리포트는 이 사실을 기록만 하고 미조치**한다(§8.5).
- **t3 채점기 교정:** `_flatten_rows` 라벨 오염 결함(기존 골든에도 잠재)을 counts-only로
  교정해 억울 fail을 회수했다 — 전 모델 오프라인 재채점, **pass→fail 역행 0**(§8.2).
- **Haiku .448**은 기존 각주 ² fence 현상(JSON을 마크다운 fence로 감싸 파서 거부: t3 0/343,
  t9 1/232)의 지속이며 **신규 회귀가 아니다**.

### 8.1 확장 구성 — 재분류분 vs 순수 신규 생성분

tier-A는 **851 → 1,884문항**(+1,033, 빌더 dedup 드랍 0)으로 늘었다. 증강분은 성격이 다른
두 범주로 나뉘며, 이를 섞으면 "얼마나 새로 만들었나"가 왜곡되므로 명시 분리한다.

| 범주 | 문항 | 성격 | 출처 |
|---|---|---|---|
| **순수 신규 생성분** | **1,033** | 이번에 새로 생성 | gen_t3/t5/t6/t7/t9/t10 (결정론 + Nova/Llama 합성 + 실로그) |
| **기존 자산 재분류분** | **393** | 기존 골든을 t5로 재편입 | §1.3 routing dedup-merge (`_routing_extra_items()`) |

- **순수 신규 생성분(1,033)** task별: t3 274 / t5 100 / t6 92 / t7 244 / t9 200 / t10 123.
- **재분류분(393)**은 `routing_holdout.json(main 290)` + `routing_graph_golden.json(63)` +
  `routing_holdout_tn.json(40)`을 t5로 dedup-merge한 것이다. `routing_holdout.json`의
  **control 40개(`rule_route` 필드)는 스키마 상이로 제외**했다.
- tier_a t5 최종 구성 = base_dedup 76 + 재분류 393 + 신규 aug 100 = **569**.
- 기존 851문항은 **byte-identical** 보존(t7 id 시프트 함정은 aug_t7을 마지막 블록으로 빼
  해결). tier_b(1,653)·freeze.lock 정합.

**생성기 provenance(신규 1,033의 태그 분포):** 결정론(DB) 474 / Bedrock Nova Pro 500 /
실로그(query_runs) 82 / Bedrock Llama 3.3-70B 118. item-level `tags`가 build_manifest를 통해
tier_a.jsonl까지 전파돼 사후감사가 가능하다. **국내 3사·Bedrock Claude 생성기는 배제**했다
— 채점 대상 모델(Solar/EXAONE/A.X, Claude Sonnet/Haiku)이 자기가 생성한 문항을 채점하는
자기편향을 막기 위함이다. 상세는 `e2e/augment_provenance.json`(per-task 생성기/모델/규칙/
dedup 근거 매니페스트).

- t3(274): 결정론 canonical 163 + Nova 패러프레이즈 111, expected.kind=exact(정수 집합).
- t7(244): Nova/Llama ≈70/30, 네거티브(함정) 33.6%, 금지 태그(missed_probe/control_probe) 0.
- t9(200): 결정론 170 + Nova 30, subjects 전수 `kg_entities.display_name` 일치.
- t5/t6(100/92): 실로그 82(query_runs status='executed', LLM 미개입) + Nova 합성 110.
- t10(123): DB에 agent_task 실로그 0행이라 전량 합성(Nova 79/Llama 44), 함정 네거티브 35%.

### 8.2 t3 채점기 counts-only 교정

**결함:** `harness_e2e.py::score_l1_exact`의 t3 분기가 쓰던 `_flatten_rows`가 group-by 결과의
**라벨 셀까지 집합에 넣어** 비교했다. 모델이 `(그룹라벨, 카운트)` 쌍 rows를 반환하면 골드
(정수 집합)와 라벨 표기가 어긋나 **정답이 억울하게 fail** 처리됐다. 이 결함은 **기존 골든에도
잠재**했고(old t3에서 solar 11/27이 억울 fail), 모델별 SQL 스타일에 따라 타격이 달라
편향을 만들었다(solar t3 34.7% vs exaone 58.9%).

**교정:** 채점기를 **counts-only 추출**(int/float만, bool 제외)로 바꾸고, `combine_stats.py`에
t3 재채점 분기(`load_t3_golden()` + `rescored_status` t3 분기, t10 rescored 선례 미러)를
추가했다. raw에 모델 rows가 저장돼 있어 **API 재호출·재빌드·freeze 재생성 없이 전 모델
오프라인 재채점**으로 교정했다(input_sha256는 입력만 해시하므로 골드 형식 변경과 무관).
골드에 라벨을 넣는 방식은 모델별 라벨 표기 변동에 취약해 금지했다.

| 모델 | baked raw t3 pass | 재채점 t3 pass | of | 역행 |
|---|---|---|---|---|
| solar | 119 | **330** | 343 | 0 |
| exaone | 202 | **284** | 343 | 0 |

전 모델 **pass→fail 역행 0**을 독립 재구현(하네스 미import, counts-only int-set 비교)으로
검증했다. combine 실행 시 t3가 자동 재채점되며, baked raw t3 score는 더 이상 SoR이 아니다.

### 8.3 11모델 순위표 + McNemar (공통 n=1,750)

| 순위 | 모델 | pass | acc | origin |
|---|---|---|---|---|
| 1 | Claude Sonnet 4.6 | 1461 | .8349 | 외산 |
| 2 | gpt-5.3-chat-latest | 1451 | .8291 | 외산 |
| 3 | DeepSeek (V3.2) | 1450 | .8286 | 외산 |
| 4 | GLM-5.2 | 1448 | .8274 | 외산 |
| 5 | **Solar** | 1420 | .8114 | **국내** |
| 6 | DeepSeek V4 Pro | 1413 | .8074 | 외산 |
| 7 | **EXAONE** | 1406 | .8034 | **국내** |
| 8 | GPT-4o | 1396 | .7977 | 외산 |
| 9 | baseline (gpt-4o-mini) | 1348 | .7703 | 외산 |
| 10 | **A.X** | 1333 | .7617 | **국내** |
| 11 | Claude Haiku 4.5 | 784 | .4480 | 외산 (fence 각주 ²) |

**McNemar: 55쌍 중 유의쌍 43개**(n=324의 25쌍 → n=1,750의 43쌍, 검정력 확대). 클러스터 구조:

- **프론티어4 상호 무승부:** Sonnet 4.6 / GPT-5.3 / DeepSeek / GLM-5.2는 전 쌍 p≥0.10.
- **🔑 상위 동률 클러스터 해소:** n=324에서 이 4모델과 한 클러스터였던 **EXAONE이 프론티어4에
  전부 유의 열세**로 갈렸다(vs Sonnet/GPT-5.3/DeepSeek p<1e-4, vs GLM p=0.0002).
- **국내 축(Solar):** 프론티어4엔 유의 열세(vs Sonnet p=0.0002, GPT-5.3 p=0.0010, DeepSeek
  p=0.0014, GLM p=0.0066), gpt-4o(+1.37pp, p=0.0097)·baseline(+4.11pp, p<1e-4)엔 유의 우세,
  DeepSeek V4 Pro와 동률(p=0.48). 깨끗한 국내<외산 분할이 아님을 명시한다.
- 하위 A.X / baseline은 상호 동률.

### 8.4 task별 성적표 (pass/n, 공통 1,750)

(열 = task, task별 n: t3 343 · t5 569 · t6 139 · t7 290 · t9 232 · t10 177. 합 1,750.)

| 모델 | t3 | t5 | t6 | t7 | t9 | t10 | 계 |
|---|---|---|---|---|---|---|---|
| Sonnet 4.6 | 329 | 477 | 102 | 153 | 232 | 168 | 1461 |
| GPT-5.3 | 330 | 484 | 102 | 152 | 232 | 151 | 1451 |
| DeepSeek | 330 | 479 | 102 | 157 | 232 | 150 | 1450 |
| GLM-5.2 | 336 | 480 | 102 | 145 | 232 | 153 | 1448 |
| **Solar** | 330 | 494 | 102 | 154 | 232 | 108 | 1420 |
| V4 Pro | 331 | 487 | 102 | 143 | 231 | 119 | 1413 |
| **EXAONE** | 284 | 486 | 102 | 154 | 226 | 154 | 1406 |
| GPT-4o | 327 | 472 | 102 | 142 | 228 | 125 | 1396 |
| baseline | 260 | 479 | 101 | 160 | 229 | 119 | 1348 |
| **A.X** | 235 | 491 | 102 | 144 | 223 | 138 | 1333 |
| Haiku 4.5 | 0 | 458 | 102 | 144 | 1 | 79 | 784 |

- **t6은 전 모델 포화(101–102/139)** — 변별력이 낮은 슬롯이다.
- **t3에서 A.X(235)·baseline(260)·EXAONE(284)이 유독 약함** — group-by SQL 스타일 차이가
  counts-only 교정 후에도 실력 차로 남는다(EXAONE은 교정으로 202→284 회복했으나 여전히 하위).
- **t10(위임 추출)은 EXAONE(154)·Sonnet(168)이 강하고 Solar(108)가 약함** — 이것이 §15가
  t10을 EXAONE에 배정한 근거다.
- Haiku는 t3 0/343·t9 1/232로 전멸(fence 파싱 거부), t5/t6/t7/t10만 정상 채점.

### 8.5 조립 — diversified vs best-per-slot + diversification cost

**diversified 조립(§15 프로덕션 배정)** = t3 solar / t5 exaone / t6 solar / t7 exaone /
t9 ax / t10 exaone = 330+486+102+154+223+154 = **1449/.828**.

**국내 best-per-slot(n=1,750)** = t3 solar / t5 solar / t6 solar / t7 exaone / t9 solar /
t10 exaone = 330+494+102+154+232+154 = **1466/.838** (= all-solar-except-t10; n=1,750에서
Solar가 t3/t5/t9를 명확히 이겨 더 이상 동률이 아님).

| 비교 | p | 판정 |
|---|---|---|
| diversified(1449) vs Sonnet 4.6 | 0.2664 | 동률 |
| diversified vs DeepSeek | 1.000 | 동률 |
| diversified vs GLM-5.2 | 1.000 | 동률 |
| diversified vs gpt-5.3 | 0.9111 | 동률 |
| diversified vs Solar 단일 | **0.0019** | **조립 유의 우세(+1.66pp)** |
| **best-per-slot(1466) vs diversified(1449)** | **7.6e-5** | **best-per-slot 유의 우세(~1pp)** |
| best-per-slot(1466) vs Sonnet 4.6 | 0.67 | 동률 |

- **✅ HELD — "조립 ≈ 최강 대형" 동률.** diversified(1449)는 최강 단일 프론티어 4모델과 전부
  통계 동률이고 최강 단일 국내 Solar에 유의 우세다 — n을 5배로 키워도 유지.
- **⚠️ NEW — diversification cost 유의.** best-per-slot(1466)이 diversified(1449)보다
  **유의 우세**(p=7.6e-5). n=1,750에서 Solar가 t3/t5/t9 슬롯을 명확히 이기면서, §15가 벤치마크
  스토리(task-aware 멀티모델 오케스트레이션 시연)를 위해 t5→exaone·t9→ax로 **의도 다양화한
  양보가 이제 통계적 정확도 비용**(~1pp)으로 드러난다. best-per-slot 자체는 최강 단일 Sonnet과
  동률(p=0.67)이다.
- **조치:** `docs/model-orchestration.md` §15의 `ASSIGNMENTS` 변경은 **owner 게이트**이므로
  본 리포트는 이 diversification cost를 **기록만** 하고 미조치한다. §15 결론("국내 3모델 간
  유의차 없음, 동점 구간 안 다양화")의 전제는 작은 n의 관측이었고, n=1,750에서 t3/t5/t9는 더
  이상 동점 구간이 아니라는 사실이 owner 판단 자료다.

> **각주 — slot_swap의 `DOMESTIC_BEST_ASSIGNMENT` 테이블은 스테일:** `slot_swap_exp.py`가
> 참조하는 국내-최강 슬롯 테이블은 과거 작은 n에서 t5·t9를 동률로 보고 굳힌 값이라, n=1,750
> 실측 best-per-slot(all-solar-except-t10)과 어긋난다. sanity 상수(아래)는 diversified 기준
> 이라 게이트는 PASS이나, 그 배정 테이블 자체는 실측과 불일치한 상태로 남아 있다.

### 8.6 sanity 상수 갱신 + 재현 조건

**slot_swap_exp.py 상수 3종(n=717 → n=1,750):**

| 상수 | old(n=717) | new(n=1,750) |
|---|---|---|
| `KNOWN_DIVERSIFIED_COMPOSITE_PASS` | 623 | **1449** |
| `KNOWN_DIVERSIFIED_VS_BASELINE_MCNEMAR_P` | 1.168e-4 | **2.981e-16** |
| `KNOWN_DIVERSIFIED_VS_BASELINE_CI` | (0.0167, 0.0488) | **(0.044, 0.072)** |

갱신 후 sanity gate PASS.

**combine_stats.py 키 개명:** `p6_three_identical_145` → **`p6_large_models_identical_scored_set`**
(값 True) + `large_identical_scored_set_size = 1750` 추가. 키 이름의 `145`는 스테일 n이었고,
불리언 의미("전 대형 모델이 동일 id-set을 채점")는 보존했다. 전 11모델이 동일 1,750-id 세트를
채점한다.

**재현 조건:**

- 커맨드: `--tier A --layer all --final-verify`, 양 DSN(`ORTHUS_PG_DSN`/`ORTHUS_PG_DSN_READONLY`)
  둘 다 **orthus_company**로 export(RO 누락 시 t3 가짜 실패). **L2 137문항은 skip 유지**
  (orthus_company는 test/staging DB가 아니라 `dispatch_l2`가 DB 접촉 전 skipped 처리 — Phase
  5/6과 비교 가능성 보존).
- 병렬: **7 벤더 레인**(①solar ②ax ③exaone ④openai 3종 순차 ⑤bedrock 2종 순차 ⑥deepseek
  2종 순차 ⑦glm). 모델별 출력은 `analysis/raw/e2e_<slug>.jsonl` "w" 모드 분리라 레인 간 충돌
  없음. **exaone은 3-way 샤드** 병렬(RAW_DIR 리다이렉트 후 병합; RAW_DIR은 repo 안에 둬야
  `relative_to` 크래시 회피).
- combine 순서: 전 레인 종료 → t3 채점기 counts-only 패치 + combine_stats t3 분기 → combine
  재실행(t3 자동 재채점). glm 레인만 `--tasks` 필터를 써 t2/t8 defer 38행이 빠진 1,917행이나
  채점(1,750 슬라이스)엔 무영향.

### 8.7 Phase 8 종합 판정

- **✅ 조립 스토리 견고:** diversified 조립은 최강 단일 프론티어와 동률, 최강 단일 국내 Solar에
  유의 우세 — n=1,750에서도 "국내 슬롯 조립 ≈ 최강 대형"이 유지된다.
- **🔑 대회 메시지 정련 필요:** n=324의 "EXAONE이 프론티어와 동률 최상위 클러스터"는 n=1,750
  에서 **더 이상 성립하지 않는다**(EXAONE 프론티어4에 유의 열세). 다만 국내 최강 Solar가
  프론티어4에만 밀리고 외산 중위권(V4 Pro/GPT-4o/baseline)은 이기거나 동률이라는 **상위-중위
  밴드 서사**는 유효하다.
- **⚠️ owner 결정 대기:** diversification cost(best-per-slot > diversified, p=7.6e-5, ~1pp)는
  §15 다양화 배정의 정확도 비용이 유의해졌음을 뜻한다. ASSIGNMENTS는 owner 게이트라 미조치 —
  본 리포트는 근거(t3/t5/t9가 n=1,750에서 동점 구간이 아님)를 owner 판단 자료로 남긴다.
- **한계(정직, Phase 7 유지):** 조립은 사후 가상 점수이지 통합 실행이 아니다. 채점 세트는 L1
  read-only exact 6작업 1,750건(생성형 judge t2/t8·L2 137 skip은 범위 밖). t3 counts-only 교정은
  기존 골든의 잠재 결함을 함께 회수한 것이며 신규 골든 형식은 정수 집합 exact를 유지한다.
