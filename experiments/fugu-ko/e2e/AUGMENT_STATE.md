# 골든 증강 오케스트레이션 상태 (AUGMENT_STATE.md)

> 이 파일은 HANDOFF_BEDROCK_AUGMENT.md 실행 세션의 진행 SoR이다.
> 컨텍스트 리셋/세션 교체 시 이 파일부터 읽고 이어받을 것.
> 시작: 2026-07-22. 오케스트레이터가 각 단계 완료 시 갱신한다.

## 방침 결정 (§2 분기점 — 오케스트레이터 판단, 사용자 위임)

- **§1.3 dirty 변경 처리 = §3 신규 증강과 통합해 한 번에 재계산** (옵션 C).
  근거: freeze.lock/tier_b/카나리아/11모델 전량 재실행이 어차피 §3 후에도
  다시 필요하므로, dirty를 별도 커밋으로 완성하면 전량 재실행 6시간짜리를
  두 번 낸다. dirty의 `_routing_extra_items()`는 유지·완성하고 §3 신규
  생성분과 함께 단일 rebuild→freeze→카나리아→전량 순서로 간다.
- `routing_holdout.json`의 control 40개(`rule_route` 필드): **버린다**.
  스키마 변환 비용 대비 40개 기여가 작고 필드 의미가 달라 채점 왜곡 위험.
- provenance: §4.2 규약 준수 — tags에 `gen_bedrock_nova_pro` /
  `gen_bedrock_llama3_3_70b` / `gen_db_deterministic` / `gen_real_log` +
  `augment_provenance.json` 매니페스트.

## 단계 체크리스트

- [x] Phase 0: 정찰 (worktree 상태 / 빌더 구조 / DB·환경) — 3 서브에이전트
- [ ] Phase 1: §1.3 dirty 완성 방침 반영 (t5 routing extra 유지, control 제외 확인)
- [x] Phase 2: 신규 생성 완료 (2026-07-22, 오케스트레이터 직접 재집계 검증: 총 **1,033문항**, 전 파일 질문 고유 + tags 존재)
  - [x] t3: **274** (결정론 163 + Nova 패러프레이즈 111; 인텐트 count-single 12/groupby-count 44/groupby-count-order 46/filter-count 172; expected.kind=exact `{"gate_pass":true,"result":<sorted int set>}` + 재현용 `spec`; 샘플 20 SQL 재검증 0 불일치)
  - [x] t9: **200** (결정론 170 + Nova 30; relation 53/entity 53/conflict 47/provenance 47; subjects 전수 kg_entities.display_name 일치)
  - [x] t7: **244** (Nova/Llama ≈70/30; conj 38/enum 38/compare 32/ellipsis 32/cmd_q 22/trap 82 — 네거티브 33.6%; 금지 태그 0; 기존 t7 코퍼스 122문항 대비 겹침 0)
  - [x] t5: **100** / t6: **92** (실로그 82 + Nova 합성 110; 라벨은 query_runs status='executed' 규칙 또는 템플릿 규칙만; 클래스 60% 상한 준수)
  - [x] t10: **123** (전량 합성 — DB에 agent_task 실로그 0행; 포지티브 80/함정 네거티브 43=35%; Nova 79/Llama 44; assignee는 기존 이름 풀만)
  - [x] dedup: 각 에이전트가 기존 tier_a/tier_b(+routing 3파일, t7 코퍼스, t3/t9/t10 골든) 대비 수행
  - [ ] augment_provenance.json 병합 (프래그먼트 5개는 완료: e2e/augment/provenance_{t3,t5t6,t7,t9,t10}.json — Phase 3에서 병합)
- [x] Phase 3 완료 (2026-07-22, 오케스트레이터 직접 재집계 검증 일치): tier_a **851→1,884**(+1,033, 드랍 0) — t3 343/t5 569/t6 139/t7 386/t9 232/t10 177/t2 30/t8 8. 기존 851개 byte-identical(t7 id 시프트 함정은 aug_t7을 마지막 블록으로 빼서 해결). gen 태그 전파: db_det 474/nova 500/real_log 82/llama 118. 신규 전량 expected.kind=exact. freeze.lock/tier_b(1653, 불변) 정합. inventory에 aug 6 asset 추가. augment_provenance.json 병합 완료(신규 1,033 vs §1.3 재분류 393 구분). 알려진 기존 이슈(비신규): COUNT MISMATCH soft print, tier_a↔tier_b 질문 겹침 181건(전부 기존 holdout 재사용, aug 무관).
- [x] Phase 4 카나리아 통과 (2026-07-22): aug 24문항 전 provenance 계열 pass/fail 정상 채점, deferred/skip/error 0, 하네스·스키마 블로커 0 (실패는 전부 정상 모델 오답). 주의: `--limit N`은 task별이 아니라 **전역 슬라이스**(harness_e2e.py:1063). solar 문항당 평균 574ms.
- **Phase 5 확정 사실 (raw 파일 실측)**: 과거 전량 = **tier A / L1만, 모델당 813행**(judge형 t2/t8 38개 제외) → 이번 예상 모델당 **1,846행**. 실제 11모델 슬러그(raw 파일명 SoR): solar, ax, exaone, baseline, deepseek, deepseek:deepseek-v4-pro, glm:glm-5.2, openai:gpt-4o, openai:gpt-5.3-chat-latest, bedrock:anthropic.claude-sonnet-4-6, bedrock:anthropic.claude-haiku-4-5-20251001-v1:0. 커맨드: `--tier A --layer all --final-verify` + 양 DSN orthus_company export. 레인: ①solar ②ax ③exaone 3-way 샤드(RAW_DIR 리다이렉트 후 병합; RAW_DIR은 repo 안에 둬야 relative_to 크래시 회피) ④openai 3종(baseline,gpt-4o,gpt-5.3) 순차 ⑤bedrock 2종 순차 ⑥deepseek 2종 순차 ⑦glm. tmux 세션명 `e2e-<lane>`.
- [~] Phase 5: 진행 중 (2026-07-22 02:45~ 7레인/9 tmux 발사). **solar 완료(14분, 1,955행, error 0)** — score.status 기준 정확 집계: t9/aug 200/200 pass, t5/aug 71%, t6/aug 67%, t7/aug 51%, t10/aug 57% (경계·함정 설계 의도 부합), t3/old 78%. **⚠️ t3/aug만 pass 23.7%(65/274)** — 실패 샘플: 모델이 (그룹라벨,카운트) 쌍 rows 반환했는데 aug 골드는 평탄 정수 집합만 기대 → groupby 계열 골드 형식 설계 결함 의심. **raw에 model rows가 저장되므로 API 재실행 없이 오프라인 재채점으로 교정 가능** — 레인 중단 불필요, 진단 에이전트 가동(채점기 로직/old 골드 형식/억울 fail 수량/교정·재채점·freeze 순서 설계). t7 deferred 96 = 기존 aggregate-only(e3) 아이템, 정상. L2 137은 skip(예상대로). raw는 하네스 종료 시점 일괄 기록(mtime으로 mid-run ETA 측정 불가).
- [x] Phase 6 완료 (2026-07-22, solar/exaone t3 재채점 오케스트레이터 독립 재계산 일치 330·284/343): **scored n=1,750**, 11모델 순위 Sonnet .835 > GPT-5.3 .829 > DeepSeek .829 > GLM .827 > **Solar .811** > V4Pro .807 > **EXAONE .803** > GPT-4o .798 > baseline .770 > **A.X .762** > Haiku .448(fence). 유의쌍 43/55 — **상위 동률 클러스터 해소**(EXAONE이 프론티어4에 유의 열세로 탈락, 프론티어4는 상호 무승부). 국내 축: solar가 프론티어4엔 유의하게 지지만 gpt-4o·baseline은 유의하게 이김. **조립 diversified(1449)는 최강 단일 프론티어와 통계 동률 유지** + 최강 국내 단일(solar)을 유의하게 이김. ⚠️ 부수 발견: n=1750에선 best-per-slot(all-solar-except-t10)=1466이 diversified보다 유의 우세(p=7.6e-5, ~1pp diversification cost) — §15 의도적 다양화의 비용이 이제 유의함, **ASSIGNMENTS 변경은 owner 게이트라 미조치**(Phase 7 리포트에 기록만). sanity 상수 갱신: slot_swap 1449/2.98e-16/(0.044,0.072) GATE PASS, combine 키 `p6_large_models_identical_scored_set`(=True, 1750). 산출물: analysis/raw/phase6_expanded_summary.json 외 3종.
  - **[선행 확정] t3 채점기 교정 (진단 완료 2026-07-22):** 원인 = `harness_e2e.py::score_l1_exact` t3 분기 L414-426의 `_flatten_rows`(L430-439)가 라벨 셀 포함 집합 비교 → 라벨 동반 정답이 억울 fail. 골드(int-set)는 정상, old t3에도 잠재(solar old 11/27). solar aug 209 fail = 억울 197 + 진짜 12. **교정 = 채점기 counts-only 추출(int/float만, bool 제외) + combine_stats에 t3 재채점 분기 추가(`rescored_status` t10 선례 미러, load_t3_golden)** — raw의 rows로 전 모델 오프라인 재채점, API 재호출·재빌드·freeze 재생성 불필요(input_sha256는 입력만 해시). 전 모델 pass→fail 역행 0 검증됨. 골드에 라벨 넣는 방식은 금지(모델별 라벨 표기 변동으로 취약). 드랍 필요 0 (선택: 구분자 포함 필터 6건). 진단 스크립트는 scratchpad(analyze_t3.py/rescore_all.py/final.py).
  - 순서: 전 레인 종료 → harness 채점기 패치 + combine_stats t3 분기 → combine 재실행(그 시점 raw가 aug 포함 1,955행인지 모델별 확인 — 구 813행이면 해당 레인 미완).
  - **[구현 완료 2026-07-22]** `harness_e2e.py`에 `_t3_counts_only` 신설 + t3 분기 교체(타 task 무영향), `combine_stats.py`에 `load_t3_golden()` + `rescored_status` t3 분기(t10 미러). 10개 완료 모델 재채점 검증: 역행 0, solar aug 65→262 정확 일치, 전 모델 aug +71~+197/old +9~+14 회복. combine 실행 시 t3 자동 재채점됨.
  - **[haiku t3/t9 전멸 = 기존 문서화된 현상, 조치 불필요]**: 커밋 리포트(511c0de7 e2e_report.md 각주 ²) — haiku가 JSON-only 지시에도 마크다운 fence로 감싸 파싱 실패(t3 0/69, t9 1/32). 이번도 동일 패턴(t3 0/343 gate rejected, t9 1/232). 신규 회귀 아님, 리포트 각주 유지.
  - **레인 완료 현황 (04:5x 시점)**: 10/11 완료(solar/exaone/ax/baseline/gpt-4o/gpt-5.3/sonnet/haiku/deepseek/v4-pro 각 1,955행 검증). **glm만 실행 중** — glm 레인은 `--tasks t3,t5,t6,t7,t9,t10` 필터 사용이라 t2/t8 defer 38행이 빠진 1,917행 예상(채점 무영향, combine 시 참고).
- exaone 레인 완료(3샤드 병렬 ~6분, 1,955행, dup 0, fallback 0). **실측 지연 0.5~2s/문항 — 기존 '36s/콜'은 낡은 수치.** exaone t3 58.9% vs solar 34.7% = 모델별 SQL 스타일에 따라 채점기 결함 타격이 달라지는 증거.
- [ ] Phase 7: e2e_report.md 갱신 (재분류 n vs 신규 생성 n 구분) + 커밋 (git add -A 금지)

## 진행 로그

- 2026-07-22: 세션 시작. Phase 0 정찰 3에이전트 발사.
- 2026-07-22: Phase 0 완료. 핵심 사실:
  - dirty 상태는 §1.3 문서보다 진행돼 있음 — tier_a.jsonl(851줄, t5=469)/tier_b.jsonl(1653줄)/freeze.lock까지 working tree에서 이미 재생성됨. 미커밋. control 40개는 이미 코드에서 제외됨(방침 일치).
  - freeze.lock 재생성 = `build_tier_b.py` 실행 (전용 스크립트 없음). tier_a는 `build_manifest.py` 실행.
  - 하네스 = `experiments/fugu-ko/harness_e2e.py` (--models/--tier/--layer/--tasks/--limit/--final-verify).
  - sanity 상수 3개 = `analysis/slot_swap_exp.py` L75-77 (KNOWN_DIVERSIFIED_COMPOSITE_PASS=264 등), combine_stats.py L338 `"p6_three_identical_145"` 키.
  - 신규 골든 파일 추가 지점 = `build_tN()`의 `_items_multi` 파일 리스트 + `inventory.json` item_count 갱신 필요.
  - .env DSN의 DB명은 `orthus`(5433) — 실행 시 ORTHUS_PG_DSN/READONLY 둘 다 orthus_company로 override 필수.
  - DB 가용: notion_rows=1365, kg_entities=302, kg_entity_mentions=4321, query_runs=11423(nl_question 마이닝 가능). Bedrock 키/어댑터/venv OK.
- 2026-07-22: 공통 헬퍼 `e2e/augment/gen_common.py` 작성. Phase 2 생성 에이전트 5개 병렬 발사 (t3·t5/t6=opus, t7·t9·t10=sonnet). 산출 규약: golden/aug_tN.json + e2e/augment/gen_tN.py + provenance_tN.json 프래그먼트, build_manifest.py는 Phase 3에서만 수정.

## Phase 3 이어받기 노트 (WSL shutdown 체크포인트, 2026-07-22 01:5x)

- 사용자 요청으로 Phase 2 완료 시점에서 일시 정지 (WSL shutdown). 재개 시 여기부터.
- **Phase 3 할 일 (통합 배선 — build_manifest.py 수정은 여기서만)**:
  1. `build_t3/t5/t6/t7/t9/t10`의 소스 파일 리스트에 `aug_tN.json` 추가 (t3는 `t3.json`+`t3_holdout.json`만 읽는 구조 + aug 아이템은 `spec`/`expected` 직접 보유 — `_load_t3_gold`/`T3_SPECS` 확장 방식은 gen_t3.py와 provenance_t3.json 참조; t7은 `build_t7_family`/`_t7_gate_record` 경유).
  2. **전 빌더가 item-level `tags`를 매니페스트로 전파하지 않음** — 아이템 `tags` 필드를 빌더 자체 태그에 merge하는 배선 추가 (gen_* provenance 태그가 tier_a.jsonl까지 흘러야 §4.2 사후감사 가능).
  3. `inventory.json`의 해당 asset item_count 갱신.
  4. e2e/augment/provenance_{t3,t5t6,t7,t9,t10}.json 5개 프래그먼트를 `e2e/augment_provenance.json`으로 병합.
  5. `build_manifest.py` 실행 → tier_a.jsonl 재생성, `build_tier_b.py` 실행 → tier_b.jsonl + freeze.lock 재생성.
  6. 재생성 후 task별 카운트 재집계로 기대치 대조 (기존 851 + 신규 ~1,033 - 빌더 dedup 드랍).
- 주의: gen_common.py의 `load_env`는 t7 에이전트가 중복 키 버그(빈 placeholder가 이김)를 수정해 둠 — 되돌리지 말 것.
- Bedrock Llama 3.3은 외국 문자/깨진 토큰을 ~9% 섞는 경향 — t7이 corruption 필터로 정리했음. 향후 Llama 사용 시 참고.

## Phase 5 병렬 레인 설계 (하네스 분석 결과, 2026-07-22)

- 하네스는 동시성/resume 없음. 모델별 출력은 `analysis/raw/e2e_<slug>.jsonl` "w" 모드 분리 — 모델 간 충돌 없음. `e2e_summary.json`은 last-writer-wins(정보용, combine_stats는 raw에서 병합하므로 무해).
- **L1: 벤더 레인 병렬 안전.** 레인: ①solar ②ax ③exaone(~36s/콜, wall-clock 지배) ④baseline(OpenAI 키 공유) ⑤bedrock 5종 한 프로세스 순차(`--models bedrock:A,bedrock:B,...`, 같은 토큰+INFERENCE_PREFIX 공유) ⑥glm:glm-5.2 ⑦deepseek.
- **L2: 문항마다 `truncate_all_tables()`+reseed** (runner_lib truncate_guard_ok — DB명에 test/staging 포함 시만 허용). 병렬 시 레인별 고유 스크래치 DB 필수 (예: orthus_test_solar …). 공유 DSN이면 조용한 오염.
- env는 프로세스별 export가 .env보다 우선(load_dotenv override=False) — 레인별 export 누락 시 조용히 같은 DSN 공유하는 함정.
- **[해결 2026-07-22] env 레시피 확정** (NEW_MODEL_EVAL_HANDOFF.md §1.3/§6 + 코드 확인): tier_a/tier_b는 100% L1 (t3 포함), L2 137문항은 별도 `e2e/l2/g1..g4.jsonl`. `dispatch_l2`는 DSN DB명이 test/staging이 아니면 **DB를 건드리기 전에 `skipped` 처리** — orthus_company에서 가드 위반 없음. 과거 커맨드: `PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" uv run python experiments/fugu-ko/harness_e2e.py --models <slug> --tier all --layer all --final-verify`, 실행 전 `ORTHUS_PG_DSN`/`ORTHUS_PG_DSN_READONLY` 둘 다 orthus_company로 export (RO 누락 시 t3 가짜 실패 — 과거 실측 18건). **이번 재실행도 동일 레시피 채택**: L2는 과거처럼 skip 유지(비교 가능성), 레인별 스크래치 DB 불필요, 병렬 레인은 orthus_company 읽기 전용 공유로 안전.
- 11모델 슬러그(pool.py+PHASE6_MODEL_IDS.md 확인): solar, ax, exaone, baseline, deepseek, glm:glm-5.2, bedrock:anthropic.claude-sonnet-4-6, bedrock:anthropic.claude-haiku-4-5-20251001-v1:0, bedrock:meta.llama3-3-70b-instruct-v1:0, bedrock:meta.llama3-1-8b-instruct-v1:0, bedrock:amazon.nova-pro-v1:0 (+mock). 대형/프론티어는 --final-verify 필수.

## 함정 리마인더 (핸드오프 §4.4/§5 요약)

- DSN 둘 다 orthus_company로 override (RO DSN 누락 시 t3 전 모델 가짜 실패)
- test/staging DB 금지 (TRUNCATE 가드)
- 생성 스크립트는 harness 밖이면 .env 수동 로드 필요
- Bedrock Claude/국내 3사 생성기 사용 금지
- WSL2 background 폴링 오탐 — 장시간 실행은 foreground until-loop
- 서브에이전트 수치는 raw jsonl 직접 재집계로 검증
- expected.kind는 exact|structural만, t7에 missed_probe/control_probe 태그 금지
