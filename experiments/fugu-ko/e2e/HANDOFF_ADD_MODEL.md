# 새 모델 추가 핸드오프 (다른 세션용)
> 단일 진입 문서. `main` 브랜치, HEAD `02924618` 기준(2026-07-21). 이 파일부터 읽을 것.

## 지금 상태 (사실관계, 재도출 불필요)
- fugu-ko E2E 벤치마크 관련 PR **4개 전부 main에 머지 완료**: #2(하네스+데이터셋+7모델), #3(model-orchestration §15 ASSIGNMENTS diversify), #4(GPT-5.3 + DeepSeek V4 Pro 추가 → 9모델), #5(slot-swap 실험, negative result).
- **현재 비교 모델 11종**: 국내 Solar/EXAONE/A.X, baseline(gpt-4o-mini), 프론티어 GPT-4o/GLM-5.2/DeepSeek V3.2/**GPT-5.3**(`openai:gpt-5.3-chat-latest`)/**DeepSeek V4 Pro**(`deepseek:deepseek-v4-pro`)/**Claude Sonnet 4.6**(`bedrock:anthropic.claude-sonnet-4-6`)/**Claude Haiku 4.5**(`bedrock:anthropic.claude-haiku-4-5-20251001-v1:0`). Bedrock 키 인증 막힘은 2026-07-21 새 bearer key로 해소돼 Claude 2종을 측정했다(기존 `_build_bedrock_chat` 재사용, 코드 수정 0줄): Sonnet 4.6은 118/145=81.38%로 EXAONE과 정확히 동수 공동 3위(상위권 전부와 McNemar 동률). Haiku 4.5는 62/145=42.76% 최하위(전 모델 대비 유의 열세) — JSON 응답을 코드펜스로 감싸는 지시-이행 실패로 프로덕션 파서가 t3/t9 전건 파싱 실패한 것이며 어댑터 결함이 아니다(`e2e_report.md` §6.0 각주 ²). 나머지 Bedrock 3종(Llama 70B/8B, Nova Pro)은 미측정.
- **현재 순위(공통 n=145)**: DeepSeek V3.2 83.45% > GPT-5.3 82.07% > EXAONE 81.38% > baseline=GLM-5.2 79.31% > GPT-4o=DeepSeek V4 Pro 78.62% > Solar 77.24% > A.X 75.17%. **상위 3개는 통계적으로 동률**(McNemar p>0.4 전 쌍). Phase 7이 "production-slot orchestration composite"(118/145=81.38%)를 3위 동급 시스템 행으로 추가(자기 구성요소 Solar/A.X만 유의하게 이김).
- **최신 결과는 `experiments/fugu-ko/analysis/e2e_report.md`가 SoR** — 635줄. §6.0(233-276줄)이 9모델 평문 요약, **§7.5(619-635줄)가 가장 최신/최종 종합 판정**. 새 모델 추가 후 여기에 행을 추가하면 됨.

## 새 모델 추가 레시피 (이미 검증된 방식, 그대로 따를 것)
**상세 메커닉스는 `experiments/fugu-ko/e2e/NEW_MODEL_EVAL_HANDOFF.md`(396줄)를 봐라 — 여전히 유효하고 최신이다.** 아래는 그 문서 대비 최근 2개 모델(GPT-5.3, DeepSeek V4 Pro) 추가 때 실제로 쓰인 **더 간단한 지름길**이니 먼저 확인할 것:

1. **새 벤더가 기존 빌더(`_build_openai_chat`/`_build_bedrock_chat`/`_build_glm_chat`/`_build_deepseek_chat`, `harness_e2e.py`)로 OpenAI-호환/같은 프로바이더면 새 `_build_<x>_chat()` 함수를 안 만들어도 된다.** 실제 사례:
   - GPT-5.3: 기존 `_build_openai_chat()`을 `openai:<model>` prefix로 그대로 재사용, `harness_e2e.py:189-190`에 `if model.startswith("gpt-5.3"): kwargs["temperature"] = None` 한 줄만 추가(그 벤더가 `temperature=0`을 거부해서).
   - DeepSeek V4 Pro: 기존 `_build_deepseek_chat()`을 `deepseek:deepseek-v4-pro` slug-suffix override로 재사용(`harness_e2e.py:225`, `override = slug.split(":",1)[1]`) — 기존 `ORTHUS_LLM_DEEPSEEK_API_KEY`/`ORTHUS_LLM_DEEPSEEK_BASE_URL` 그대로 씀, 새 env var 0개.
2. 완전히 새 벤더(위 4개 빌더 패턴에 안 맞음)면 `NEW_MODEL_EVAL_HANDOFF.md` §3.1(OpenAI-호환)/§3.2(Bedrock) 절차대로 새 `_build_<name>_chat(slug)` 추가 — 하네스-전용 env var 3개(`ORTHUS_LLM_<NAME>_API_KEY`/`_MODEL`/`_BASE_URL`) + `build_e2e_pool()`에 dispatch 한 줄.
3. **`--final-verify` 게이트는 자동 적용된다** — `harness_e2e.py`의 `_is_large_slug`가 `{solar, ax, exaone, baseline, mock}`에 없는 슬러그는 전부 자동으로 게이트에 걸리게 돼 있어서(`_LARGE_PREFIXES`는 이제 사실상 장식적), 새 슬러그 추가해도 별도 게이트 코드 수정 불필요.
4. **DB 함정(반드시 확인)**: `ORTHUS_PG_DSN`과 `ORTHUS_PG_DSN_READONLY` **둘 다** `orthus_company`를 가리켜야 함(빈 `orthus`나 staging/test 아님) — 안 그러면 t3가 전 모델에서 실패한다.
5. 실행: 3-item `--final-verify` 카나리아 → (reasoning 모델이면 토큰 스모크) → 전량 `--tier all --layer all --final-verify`.
6. 결과 병합: **`experiments/fugu-ko/e2e/combine_stats.py`(407줄)가 현재 유효한 병합 스크립트.** `runner_lib.py`의 `mcnemar_from_correct`/`bootstrap_paired_diff_ci` 재사용. `KNOWN_SOURCES`에 없는 새 슬러그는 `analysis/raw/e2e_{slug}.jsonl`을 자동으로 잡아서 **코드 수정 0줄로 병합됨**.
7. `analysis/e2e_report.md`에 새 행 추가(§7.5 최종 판정 갱신), 비용 추정 기록.

## ⚠️ 주의 — main 워크트리가 지금 clean하지 않음
`main`이 `origin/main`과는 동기화돼 있지만(`02924618`), **로컬에 이 벤치마크와 무관한 변경이 섞여 있다**: `pyproject.toml`/`uv.lock` 수정 + untracked `api_calling_test.py`, `experiments/fugu-ko/prompt-tuning/`, `t12_items_ext.py`, `t2h_judge_focused.py`, `t2h_metrics.py`, `t2h_run.py`. **이것들은 건드리지 말 것** — 다른 작업(별도 세션/워크스트림)의 것으로 추정되며, 새 모델 추가 작업 범위에 포함시키면 안 된다. 커밋 시 스테이징은 항상 파일을 명시해서(`git add -A` 금지) 벤치마크 관련 파일만 골라 담을 것.

## 게이트 관련 참고
원래 "8개 대형모델, 데이터셋 확정 전 절대 호출 금지" 하드게이트는 **이미 사용자 승인 하에 통과됐고, 그 이후로 이미 2개 모델(GPT-5.3, DeepSeek V4 Pro)이 추가로 반복 승인받아 추가됨** — 매번 처음부터 재승인받는 절차가 아니라 "모델 하나 추가"가 이미 반복 가능한 정상 워크플로가 됐다. 그래도 실제 API 비용이 드는 라이브 호출이니 실행 전 사용자에게 어떤 모델/예상 비용인지 확인하는 게 안전하다.

## 참고 문서 (중복 안 하고 포인터만)
- `experiments/fugu-ko/analysis/e2e_report.md` — **결과 SoR**, §6.0/§7.5.
- `experiments/fugu-ko/e2e/NEW_MODEL_EVAL_HANDOFF.md` — 새 모델 추가 상세 메커닉스(DB 함정, 어댑터 패턴, 비용 추정, 결과 병합) — 위 "지름길" 섹션과 함께 볼 것.
- `experiments/fugu-ko/e2e/combine_stats.py` — 현재 유효한 결과 병합 스크립트.
- `experiments/fugu-ko/e2e/SESSION_HANDOFF.md` / `STATE.md` — 데이터셋(Tier A/B/L2, 2,069아이템)·하네스 빌드 이력 전반(새 모델 추가 자체엔 안 봐도 됨, 배경 참고용).
- `experiments/fugu-ko/e2e/SLOT_SWAP_HANDOFF.md` + `SLOT_SWAP_EXPERIMENT_RESULT.md` — PR #5의 별개 실험(슬롯 재배정, negative result) — 새 모델 추가와 무관, 참고만.

## 오케스트레이션 방침 (유지)
모든 read/write는 서브에이전트에 위임, ≤10문장 요약만 받는다. sonnet 기본, 복잡한 것만 opus, haiku 미사용. 커밋은 파일 명시(`git add -A` 금지), main이 아니라 `.worktrees/<topic>` 브랜치에서(단, 이번 벤치마크는 이미 여러 PR이 그렇게 머지된 선례가 있으니 같은 패턴 유지).
