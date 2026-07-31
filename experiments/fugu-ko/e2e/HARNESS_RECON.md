# harness.py 정찰 — Phase 4(harness_e2e.py) 구현 인수인계
> 미커밋(untracked). RESUME_RUNBOOK.md §6.4/§7 보완. Phase 4 재정찰 불필요 — 이 파일 사용.

## 기존 harness.py 동작
- golden 로드: `json.load(GOLDEN/f"{task}.json")["items"]`. task 문자열로 `RUNNERS={t3,t5,t2,t6,t7}` 디스패치.
- 각 runner가 **라이브 프로덕션 함수를 in-process 직접 호출(HTTP 아님)**, 항상 `chat_model=chat` 주입(모든 orthus L1 함수가 받는 seam): `orthus.structured.query.query_structured`, `orthus.router.route.classify`/`classify_intent`, `orthus.wiki.qa.ask`, `orthus.router.decompose.should_decompose`/`split_question`.
- 채점: runner별 inline 결정론(t3 gate_passed/executed, t5/t6 exact, t7 gate+split). t2(wiki_qa)는 실행+기록만, 판정은 별도: `judge/pairwise.py`(단일 judge=gpt-4o, position-swap 2회, tie-on-disagreement, win-rate≥40% 수용), `judge/panel.py`(이종 3-judge PoLL, 다른 국내모델을 judge로, `_done_keys` 재개가능).

## 모델 풀 / CLI
- `--models`(default "solar,ax,exaone") → `experiments/fugu-ko/pool.py::build_pool(slugs)`. **harness-local 풀(orthus.models.registry 아님)**, 키는 `keys.json`(`FUGU_KEYS` env, default OneDrive 경로)에서 읽음. 각 벤더를 `WorkerChat`으로 래핑(ChatModel Protocol: `.model_id` + `.complete(system,user,json_only=)`), `openai_compat._post_json` 재사용.
- 특수 슬러그 `"baseline"` = 현행 프로덕션 모델(`get_settings()`, `ORTHUS_LLM=openai` 필요=gpt-4o-mini) 래핑 → 비용/usage 통일.
- `run_with_usage()`가 매 runner 호출 감싸 `n_llm_calls`, `in_tok`/`out_tok`, `n_usage_missing`(WorkerChat.usage_totals()) 수집.

## 불변식
- latency p50/p95: `summarize()` inline(정렬 후 n//2, int(n*0.95)). 별도 모듈 없음.
- **`model.fallback==0`: harness.py는 추적 안 함.** 실제 기전: `orthus/models/orchestration.py::FallbackChat.complete()`가 폴백마다 `with audit("model.fallback") as span: span.add_meta(task=,assigned=,fallback=,reason=)` 발화. **신규 하네스는 `orthus.audit`의 "model.fallback" span count==0을 assert.**
- confident-zero: 라이브러리 함수 없음. golden `golden/e6_zprobe.json`(`e6_cascade.py`) + manifest 개념으로 존재. `manifest_schema.md §14`가 item-level `invariants:["model_fallback_zero","no_confident_zero"]` 정의 — **신규 하네스가 강제(현재 코드 부재)**.

## 통계
- `experiments/fugu-ko/` 루트엔 significance.py/power.py 없음. **`experiments/fugu-ko/embedding/`에만 존재**(retrieval 특화): `significance.py::exact_mcnemar(b,c)->float`; `power.py`: `exact_mcnemar`, `min_split_for_sig(n)`, `n_discordant_for_power(pi)`, `mde_pi(n)`, `diff_ci(b,c,n_total)`(95% CI, **정규근사, 부트스트랩 아님**). **부트스트랩 CI 함수 없음.** harness_e2e.py는 이 McNemar/power를 **일반화 포팅**(현재 retrieval hits()/rank에 묶임). §1b가 부트스트랩 10k를 언급하므로 부트스트랩 CI는 신규 구현 필요.

## 어댑터 배선(orthus/models/)
- `registry.py`(프로덕션 seam): `VendorSpec`(slug/base_url/model/api_key/timeout/extra_body/min_interval), `vendor_specs()`가 Solar/A.X/EXAONE spec 생성, `build_vendor_chat(spec,*,retries,model=None)->ChatModel|None`가 `OpenAIChat`(adapters/openai_compat.py) 구성(미설정 시 None). `get_chat_model()`=`ORTHUS_LLM` 스위치(openai|solar|bedrock|cli|codex_pool|mock), `OpenAIChat`/`BedrockConverseChat`(adapters/bedrock.py)/`CLIChat`/`CodexPoolChat`/`MockChat`.
- env 이름(값 금지): ORTHUS_LLM, ORTHUS_LLM_API_KEY, ORTHUS_LLM_BASE_URL, ORTHUS_LLM_MODEL, ORTHUS_LLM_SOLAR_API_KEY, ORTHUS_LLM_AX_API_KEY, ORTHUS_LLM_EXAONE_API_KEY(+base_url/model 쌍), ORTHUS_LLM_BEDROCK_API_KEY, ORTHUS_LLM_BEDROCK_REGION, ORTHUS_LLM_BEDROCK_MODEL_ID, ORTHUS_LLM_FALLBACK_MODEL/_PROVIDER/_BASE_URL/_API_KEY, ORTHUS_MODEL_ORCHESTRATION_ENABLED.

## 핵심 스왑 seam(4b)
- `orthus/models/orchestration.py::get_chat_model_for(task)->ChatModel`가 THE 스왑점: per-task `ASSIGNMENTS`(task const→벤더 슬러그, 예 TASK_STRUCTURED→"solar"), `FallbackChat(task,primary,backups)` 래핑(backup 순서=`_BACKUP`, default 슬롯 마지막). fail-closed 게이트: `s.llm=="mock"` 또는 `s.model_orchestration_enabled`(ORTHUS_MODEL_ORCHESTRATION_ENABLED).
- **L1 아이템**: harness.py처럼 프로덕션 진입점을 `chat_model=`(harness-built WorkerChat/OpenAIChat, 풀 슬러그별)로 직접 호출 → `get_chat_model_for` 우회. 이게 모델별 비교의 정공법.
- **L2 아이템**: HTTP route(orchestrate_chat_route 등)라 `chat_model=` 미수용 → 모델 스왑은 (a) 라우트 호출 전 `ASSIGNMENTS`/`ORTHUS_MODEL_ORCHESTRATION_ENABLED` env 토글, 또는 (b) L2 디스패치 동안 `orchestration.get_chat_model_for`/`registry.get_chat_model` monkeypatch. regression-guard L2는 MockChat 게이팅으로 결정론.

## 캐시
- 단일 마스터 스위치 `get_settings().ask_semantic_cache_enabled`(env ORTHUS_ASK_SEMANTIC_CACHE_ENABLED, default False). `orthus/router/cache.py` L27: off면 router.answer가 양 훅 전부 스킵. 하네스는 이 env 하나로 깔끔히 off.

## harness_e2e.py 확장 요약
- **재사용**: `pool.build_pool()`/`WorkerChat`(+usage), `judge/pairwise.py`·`panel.py`(expected.kind=="judge"), `embedding/{significance,power}.py`의 McNemar/power(일반화).
- **신규**: (1) 스키마 구동 manifest 로더(tier_a.jsonl + l2/g*.jsonl, task/entry_point/expected.kind 키, RUNNERS 유사 디스패치 테이블, **e3/t7 태그 기반 e3_prefilter 라우팅 플래그 준수**). (2) invariants 체커(`orthus.audit` "model.fallback" span count + confident-zero 검출). (3) L2 HTTP-route 디스패치(scratch DB fixture 로드, regression-guard는 MockChat 게이팅). (4) 부트스트랩 CI. (5) 대형모델 `--final-verify` 게이트.
- 사용자 액션: `keys.json`(FUGU_KEYS) 국내 벤더 키 구성, baseline용 `ORTHUS_LLM=openai`, 라이브 시 company node.env.
