# 골든 확장 재검증 핸드오프 (다른 세션용)

> 단일 진입 문서. `main` 브랜치, HEAD `fd882e41` 기준(2026-07-21). 이 파일부터 읽을 것.
> 목표: **골든 채점 문항수를 늘려서 11개 단일 모델 + 국내 조립 V1/V2를 전부 다시 E2E 검증**한다.
> 새 모델 추가가 아니라 **같은 모델들을 더 큰 골든셋에서 재측정**하는 실험이다.

---

## 0. 왜 이 실험을 하나 (배경)
현재 공통 채점셋은 **n=145**로, 상위 6개 대상(DeepSeek V3.2 / 조립 V2 / GPT-5.3 / 조립 V1 / EXAONE / Claude Sonnet 4.6)이
2.1%p 안에 몰려 **전부 쌍대 McNemar 동률**이다. 이 "동률 무리"가 진짜 동률인지, 아니면 **145문항이라는 작은
표본 때문에 검정력(power)이 부족해서 차이를 못 잡는 것인지**를 가르려면 골든 문항을 늘려 다시 재야 한다.
그래서 이 실험의 1차 산출물은 "순위가 바뀌었나"가 아니라 **"표본을 키우면 상위 동률 구간에서 유의차가 새로
드러나는가"**다. 특히 두 가지를 본다:
- **조립 vs 최강 단일**(조립 V1/V2 vs DeepSeek V3.2 / GPT-5.3 / Claude Sonnet 4.6)이 커진 표본에서도 동률로 남는가.
- **V1 vs V2**(t3·t5·t9 배정 차이)가 커진 표본에서 유의해지는가. 지금은 p=0.375로 동률.

---

## 1. 지금 상태 (사실관계, 재도출 불필요)
- **비교 대상 11개 단일 모델 + 조립 2종(V1/V2)** 전부 측정 완료(2026-07-21). 현재 순위(공통 n=145, exact 채점):

  | 대상 | 정확도 | pass/145 | 계열 |
  |---|---|---|---|
  | DeepSeek V3.2 | 83.45% | 121 | 해외 |
  | 국내 조립 V2 | 83.45% | 121 | 조립 |
  | GPT-5.3 (`gpt-5.3-chat-latest`) | 82.07% | 119 | 해외 |
  | 국내 조립 V1 (현행 프로덕션) | 81.38% | 118 | 조립 |
  | LG EXAONE | 81.38% | 118 | 국내 |
  | **Claude Sonnet 4.6** (`us.anthropic.claude-sonnet-4-6`) | 81.38% | 118 | 해외(Bedrock) |
  | gpt-4o-mini (baseline) | 79.31% | 115 | 해외 |
  | GLM-5.2 | 79.31% | 115 | 해외 |
  | GPT-4o | 78.62% | 114 | 해외 |
  | DeepSeek V4 Pro | 78.62% | 114 | 해외 |
  | Upstage Solar | 77.24% | 112 | 국내 |
  | SKT A.X | 75.17% | 109 | 국내 |
  | **Claude Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) | 42.76% | 62 | 해외(Bedrock) |

- **Haiku 4.5 주의**: 42.76%는 실력이 아니라 **JSON 펜스 지시 불이행**(모든 응답을 ` ```json ` 펜스로 감쌈 →
  프로덕션 파서가 t3 0/28·t9 1/32 전건 거부)이다. 재실험에서도 같은 배선이면 같은 붕괴가 재현된다. 이건
  "프로덕션 배선 그대로 채점" 원칙상 정당한 결과이므로 별도 처리하지 말 것(포맷 보정 넣으면 다른 실험이 됨).
- **최신 SoR 문서**: `experiments/fugu-ko/analysis/e2e_report.md`(11모델 결과), 병합 통계
  `experiments/fugu-ko/analysis/raw/phase8_verified_stats_11model.json`(`n_common_scored=145`).
- **조립 배정표 SoR**: `docs/model-orchestration.md` §15. 조립 스크립트는 `experiments/fugu-ko/analysis/slot_swap_exp.py`.
- oracle(11모델 중 하나라도 맞힌 비율)은 현재 **124/145 = 85.5%** — 재실험에서 이 값이 어떻게 변하는지도 기록할 것.

---

## 2. 145 scored가 어떻게 구성되나 (골든 확장 전 반드시 이해)
채점셋은 `experiments/fugu-ko/e2e/tier_a.jsonl`의 문항 중 **`exact`/`structural` 채점 대상만** 잡는다.
```
145 scored = t3(28) + t5(21) + t6(20) + t7(22) + t9(32) + t10(22)
```
- **t7은 tier_a에 118문항이 있지만 22개만 scored.** 나머지 96개는 태그
  `missed_probe`/`control_probe`/`aggregate_scored_set_wide`(E3 프리필터 세트)라 **집계 전용 → per-item deferred**
  (`harness_e2e.py:787-790`).
- **t2(30)·t8(8)은 `expected.kind="judge"`라 항상 deferred** — LLM judge 채점이라 exact 셋에서 빠진다
  (`harness_e2e.py:773-774, 830-833`).
- 문항 스키마(필수 필드 `id/layer/task/entry_point/input/expected/scoring/tier/provenance/tags/frozen`)의
  정식 명세는 `experiments/fugu-ko/e2e/manifest_schema.md`. 실제 예시 1줄:
  ```json
  {"id":"A-t3-0001","layer":"L1","task":"t3","entry_point":"...","input":{...},
   "expected":{"kind":"exact",...},"scoring":"exact","tier":"A","provenance":"golden",
   "tags":["t3",...],"frozen":{"input_sha256":"...","frozen_at":"build:..."}}
  ```

---

## 3. 골든 문항을 늘리는 정확한 절차
새 문항이 **scored로 잡히려면** 다음을 전부 만족해야 한다(하나라도 어기면 조용히 deferred/skip됨):
1. `experiments/fugu-ko/e2e/tier_a.jsonl`(또는 tier_b holdout이면 `tier_b.jsonl`, L2면 `l2/g*.jsonl`)에
   스키마대로 문항 추가. 결정론 빌더를 쓰면 더 안전하다:
   - Tier A: `experiments/fugu-ko/e2e/build_manifest.py` (`golden/*.json` + `inventory.json` → tier_a.jsonl)
   - Tier B: `experiments/fugu-ko/e2e/build_tier_b.py`
2. `expected.kind`는 **`exact` 또는 `structural`** (절대 `judge`/`metric` 아님 — 그러면 deferred).
3. `task=="t7"`이면 태그에 `missed_probe`/`control_probe`/`aggregate_scored_set_wide`를 **넣지 말 것**(넣으면 집계 전용).
4. `frozen.input_sha256`가 세팅되고 pending 아님(`runner_lib.py:105-113`의 `_is_pending`이 skip함).
5. **`freeze.lock` 재생성**(`build_tier_b.py::build_freeze_lock`) — CI가 drift-gate한다(`manifest_schema.md` §13).
6. task가 t3/t5/t6/t7/t9/t10 중 하나여야 재실행 스코프(`--tasks`)에 잡힌다. 새 task를 추가하려면 하네스
   dispatch(`harness_e2e.py`의 L1 dispatch)와 채점 로직도 함께 손봐야 하므로 범위가 커진다 — 문항 확장이
   목적이면 **기존 6개 task 안에서 문항수만 늘리는 쪽**을 권장.

---

## 4. 재실행 순서 (그대로 따를 것)

### 4.0 공통 env (매 bash 호출마다 다시 export — shell state 유지 안 됨)
```bash
cd <repo>
# DB는 반드시 orthus_company (빈 orthus/staging/test 아님) — 둘 다 override 필수
export ORTHUS_PG_DSN="$(grep '^ORTHUS_PG_DSN=' .env | tail -1 | cut -d= -f2- | sed 's|/orthus$|/orthus_company|')"
export ORTHUS_PG_DSN_READONLY="$(grep '^ORTHUS_PG_DSN_READONLY=' .env | tail -1 | cut -d= -f2- | sed 's|/orthus$|/orthus_company|')"
# 벤더별 키 (.env 중복 정의는 tail -1이 유효본)
export ORTHUS_LLM_API_KEY="$(grep '^ORTHUS_LLM_API_KEY=' .env | tail -1 | cut -d= -f2-)"                 # openai:*
export ORTHUS_LLM_DEEPSEEK_API_KEY="$(grep '^ORTHUS_LLM_DEEPSEEK_API_KEY=' .env | tail -1 | cut -d= -f2-)" # deepseek*
export ORTHUS_GLM_API_KEY="$(grep '^ORTHUS_GLM_API_KEY=' .env | tail -1 | cut -d= -f2-)"                   # glm:*
export ORTHUS_LLM_BEDROCK_API_KEY="$(grep '^ORTHUS_LLM_BEDROCK_API_KEY=' .env | tail -1 | cut -d= -f2-)"   # bedrock:* (Claude), region us-east-1 기본
# 국내 3사(solar/exaone/ax)·baseline 키는 기존 pool.build_pool 경로가 .env에서 읽음
```

### 4.1 골든 확장 후 카나리아 (전량 전에 반드시)
```bash
PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" uv run python experiments/fugu-ko/harness_e2e.py \
  --models solar --tier A --layer all --tasks t3,t5,t6,t7,t9,t10 --limit 3 --final-verify 2>&1 | tail -30
```
- 확인: `pool build failed` 없음, invariants `model.fallback spans (delta over run): 0 (CLEAN)`,
  결과 jsonl `experiments/fugu-ko/analysis/raw/e2e_solar.jsonl`에 라인 생성 + `latency_ms` 채워짐.
- **새 문항의 채점이 실제로 도는지**(deferred로 안 빠지는지) 여기서 확인하라 — status 분포에 새 id가 pass/fail로 잡혀야 함.

### 4.2 11개 단일 모델 전량 (모델별 **순차** — `e2e_summary.json` 경합 방지)
슬러그 목록(그대로):
```
solar  exaone  ax  baseline
openai:gpt-4o  glm:glm-5.2  deepseek
openai:gpt-5.3-chat-latest  deepseek:deepseek-v4-pro
bedrock:anthropic.claude-sonnet-4-6  bedrock:anthropic.claude-haiku-4-5-20251001-v1:0
```
각 모델 1회씩(콜론 슬러그는 작은따옴표 필수):
```bash
PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" uv run python experiments/fugu-ko/harness_e2e.py \
  --models '<슬러그>' --tier A --layer all --tasks t3,t5,t6,t7,t9,t10 --final-verify
```
- **오래 걸린다**(모델당 확장 규모에 비례, 현재 145 기준 ~4.5분). nohup + 로그파일 + 폴링으로 돌리고,
  이 WSL2 환경은 **background bash/Monitor의 kill -0·sleep 폴링이 오탐**하니 종료 확인은 foreground
  `until ! kill -0 <pid>; do sleep 10; done`(timeout 570000)로 이중 확인할 것.
- `--final-verify`는 전 슬러그에 붙인다(`_is_large_slug`가 국내+baseline 외 전부 게이트). temperature는
  `gpt-5.3*`만 하네스가 자동으로 `None`(벤더가 0 거부), 나머지는 0. Bedrock Claude는 region us-east-1 기본.
- 각 모델 완료 후 **raw jsonl을 직접 파싱해 재검증**하라(서브에이전트 보고만 믿지 말 것 — 지난 세션에서
  동일 에이전트가 상충 수치를 보고한 적 있음). 확인: 라인수 = scored+deferred 총합, scored(pass+fail) 수,
  pass/정확도, task별 분포, error 0, invariants CLEAN.

### 4.3 11모델 병합 (n_common 자동)
```bash
uv run python experiments/fugu-ko/e2e/combine_stats.py \
  --models 'solar,exaone,ax,baseline,openai:gpt-4o,glm:glm-5.2,deepseek,openai:gpt-5.3-chat-latest,deepseek:deepseek-v4-pro,bedrock:anthropic.claude-sonnet-4-6,bedrock:anthropic.claude-haiku-4-5-20251001-v1:0' \
  --out experiments/fugu-ko/analysis/raw/phase9_verified_stats_11model.json
```
- `n_common_scored`는 `len(common)`으로 **동적 계산**(`combine_stats.py:196`) → 골든이 커지면 자동 반영, 코드 수정 0.
- 출력 JSON에 per-item correctness와 `pairwise_mcnemar_on_common`이 들어간다. 통계는
  `runner_lib.mcnemar_from_correct` / `bootstrap_paired_diff_ci`(10k resample, seed 1234) 재사용.

### 4.4 조립 V1/V2 재계산 ⚠ 하드코딩 sanity-gate 주의
```bash
.venv/bin/python3 experiments/fugu-ko/analysis/slot_swap_exp.py   # CLI 인자 없음
# → experiments/fugu-ko/analysis/orchestration_composite_slot_swap_exp.json
```
배정표(`slot_swap_exp.py`, 이번 확장에서 **바꾸지 말 것** — 같은 배정으로 재측정하는 게 실험 목적):
```python
KNOWN_DIVERSIFIED_ASSIGNMENT = {  # V1 = 현행 프로덕션(docs/model-orchestration.md §15)
    "t3":"solar", "t5":"exaone", "t6":"solar", "t7":"exaone", "t9":"ax", "t10":"exaone"}
DOMESTIC_BEST_ASSIGNMENT = {      # V2 = 슬롯별 국내 최강 후보
    "t3":"exaone", "t5":"ax", "t6":"solar", "t7":"exaone", "t9":"solar", "t10":"exaone"}
```
- `common`은 9개 국내/기존 모델 scored id 교집합으로 **동적**(`slot_swap_exp.py:221`) → n 자동 확장.
- **하지만** 3개 상수가 145셋에 핀돼 있다(`slot_swap_exp.py:79-81`):
  `KNOWN_DIVERSIFIED_COMPOSITE_PASS=118`, `KNOWN_DIVERSIFIED_VS_BASELINE_MCNEMAR_P=0.5488`, 그 CI.
  골든이 커지면 라인 231-237의 sanity gate가 **정당하게 실패**한다(n·pass가 바뀌므로). 새 값으로 재생성해
  이 3개 상수를 갱신한 뒤에야 출력을 신뢰할 것.
- 프린트 f-string의 `/145`(라인 240-242)는 리터럴 텍스트 — 확장 후 라벨이 틀리게 찍히니 `/{n}`으로 고칠 것(표시용).

---

## 5. 함정 체크리스트 (지난 세션에서 실제로 물린 것들)
- **DB DSN 둘 다 `orthus_company`** — `ORTHUS_PG_DSN`만 바꾸면 t3 SQL이 `ORTHUS_PG_DSN_READONLY`로 빈 `orthus`를 읽어
  전 모델 가짜 실패. 이름에 `test`/`staging` 들어간 DB는 L2 진입 시 TRUNCATE 가드에 걸리니 절대 쓰지 말 것.
- **raw jsonl은 `mode="w"`**(truncate) — 카나리아(--limit 3)가 전량에 덮어써진다. 카나리아 → 전량 순서면 문제없음.
- **background 감시 오탐(WSL2)** — foreground until-loop + timeout으로 종료 이중 확인(§4.2).
- **서브에이전트 수치 불신** — 문서/표에 넣기 전 raw 파일을 메인 컨텍스트에서 직접 재집계.
- **slot_swap sanity 상수·`/145` 리터럴**(§4.4) — 확장 후 반드시 갱신.
- **freeze.lock drift-gate**(§3.5) — 문항 추가 후 재생성 안 하면 CI 실패.
- 커밋 스테이징은 **파일 명시**(`git add -A` 금지). main worktree에 무관 dirty 파일(pyproject.toml/uv.lock/
  api_calling_test.py/experiments/fugu-ko/prompt-tuning/·t12_items_ext.py·t2h_*.py) 있으니 건드리지 말 것.
  작업은 `.worktrees/<topic>` 브랜치에서(기존 fugu-ko PR들과 동일 패턴).

---

## 6. 확장 후 문서 갱신 지점
- `experiments/fugu-ko/analysis/e2e_report.md`: 순위표(§6.0), 작업별 매트릭스, McNemar/§7 종합 판정을 새 n으로 갱신.
  기존 145 기준 문장은 "n=145 → n=<새값>"으로 대체하고, **동률 무리가 유지됐는지/깨졌는지**를 §7 판정에 명시.
- 새 병합 산출물명은 `phase9_verified_stats_11model.json` 권장(기존 phase8 보존).
- oracle 새 값, V1 vs V2 p 새 값 기록.
- 필요 시 이 프로젝트의 아티팩트(claude.ai artifact, "국내 LLM 슬롯 조립 vs 단일 대형모델")도 새 수치로 재배포.

## 7. 참고 문서 (포인터만)
- `experiments/fugu-ko/e2e/HANDOFF_ADD_MODEL.md` — 모델 **추가** 핸드오프(이번은 확장이지만 실행 메커닉스 공유).
- `experiments/fugu-ko/e2e/NEW_MODEL_EVAL_HANDOFF.md` — 하네스/어댑터/DB 함정 상세.
- `experiments/fugu-ko/e2e/manifest_schema.md` — 골든 문항 스키마·freeze·drift-gate 정식 명세.
- `experiments/fugu-ko/e2e/combine_stats.py` / `analysis/slot_swap_exp.py` / `e2e/runner_lib.py` — 병합·조립·통계 코어.
- `experiments/fugu-ko/analysis/e2e_report.md` — 결과 SoR. `docs/model-orchestration.md` §15 — 배정표 SoR.

## 8. 오케스트레이션 방침 (유지)
모든 read/write는 서브에이전트에 위임, ≤10문장 요약만 받는다. sonnet 기본, 정교한 판정만 opus/Fable, haiku 미사용.
아웃풋 큰 실행(전량 벤치·문서 편집)은 반드시 서브에이전트가 돌리고 요약 반환. 세션 토큰이 5시간마다 리셋되니
장시간 전량 실행은 그 점을 감안해 배분할 것.
