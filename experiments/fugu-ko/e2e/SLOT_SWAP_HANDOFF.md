# SLOT_SWAP_HANDOFF — 슬롯별 모델 재배정 실험 핸드오프

**한 줄 요약:** 오늘 세션(2026-07-21)에서 9모델(국내 4 + 대형 5) × 6개 프로덕션 슬롯(t3/t5/t6/t7/t9/t10)을
145문항 공통 채점셋으로 완주했다. 이 문서는 **다음 세션에서 "슬롯별로 어느 모델을 배정할지 바꿔보는
실험"**을 처음부터 다시 조사하지 않고 이어갈 수 있도록, 지금 갖고 있는 데이터·도구·프로덕션 배정 현황·
3가지 가능한 실험 트랙을 정리한다. 이 문서 하나만 읽고 새 세션에서 바로 시작할 수 있도록
self-contained로 작성했다. `NEW_MODEL_EVAL_HANDOFF.md`(신규 모델 1개를 비교표에 추가하는 절차)와는
목적이 다르다 — 이번엔 **슬롯 배정 자체를 바꾸는 실험**이다.

---

## 1. 지금 갖고 있는 것 (선행 세션 산출물 인벤토리)

### 1.1 9모델 × 6슬롯 raw 결과 (완주됨, 재실행 불필요)

각 모델의 145문항 채점 결과가 이미 있다. 슬롯(task)별 pass/n은 아래 §2 표에 전부 옮겨뒀으니
JSON을 다시 파싱할 필요 없이 이 문서만 봐도 된다. 원본이 필요하면:

- `experiments/fugu-ko/analysis/raw/e2e_{slug}.jsonl` — 모델별 raw 결과. 9개 슬러그:
  `solar`, `exaone`, `ax`, `baseline`, `openai:gpt-4o`, `glm:glm-5.2`, `deepseek`,
  `openai:gpt-5.3-chat-latest`, `deepseek:deepseek-v4-pro`. (gitignored — 로컬에만 존재, 세션 간
  유실 가능. 유실 시 §6 "재실행" 참고.)
- `experiments/fugu-ko/analysis/raw/phase6_verified_stats_9model.json` — 9모델 종합 통계
  (accuracy, per-task, 전 쌍 McNemar, latency, empty-check). **9모델 확장본**이며 기존 7모델본
  `phase6_verified_stats.json`은 그대로 보존돼 있다.
- `experiments/fugu-ko/analysis/raw/orchestration_composite_9model.json` — 현재 프로덕션 배정표
  그대로 조립한 합성 점수(81.38%, 118/145) + 슬롯별 상세. **이 파일이 "슬롯 바꾸기 실험"의 baseline
  이다.**
- `experiments/fugu-ko/analysis/e2e_report.md` §6–§7 — 9모델 순위표, 페어와이즈, 슬롯 최적성 분석
  서술본.
- `experiments/fugu-ko/e2e/combine_stats.py` — **재사용 필수 도구**. id 교집합 계산, 7개 역사
  슬러그의 authoritative 소스 핀(`KNOWN_SOURCES`), t10 존칭 재채점 로직, McNemar/부트스트랩 호출을
  전부 캡슐화한 CLI. 신규 슬롯 조합을 검증할 때 이 모듈의 로딩/재채점 함수를 **import해서 재사용**하고
  재구현하지 않는다.
- `experiments/fugu-ko/e2e/runner_lib.py` — 통계 원함수: `mcnemar_from_correct(a_correct, b_correct)`,
  `bootstrap_paired_diff_ci(a_correct, b_correct, n_resamples=10000, seed=1234)`, DB 안전가드
  (`is_safe_truncate_dsn`, `truncate_guard_ok`).
- `experiments/fugu-ko/harness_e2e.py` — 하네스 본체. 신규 모델 슬러그 추가는 여기(§4 참고, 절차는
  `NEW_MODEL_EVAL_HANDOFF.md` §3와 동일).

### 1.2 이번 세션에서 신규로 확인된 것 (재확인 불필요)

- `openai:gpt-5.3-chat-latest`와 `deepseek:deepseek-v4-pro` 모두 정상 동작 확인됨(에러 0, invariants
  CLEAN). GPT-5.3은 API가 `temperature=0`을 거부해 `orthus/models/adapters/openai_compat.py::OpenAIChat`에
  옵셔널 `temperature: float | None = 0.0` 파라미터를 추가했고(하위호환, 기존 호출자 영향 없음, 67개
  단위테스트 통과), `harness_e2e.py::_build_openai_chat`에서 `gpt-5.3` 접두 슬러그만 `temperature=None`으로
  생성한다. 이 수정은 이미 코드에 반영돼 있다 — **다시 할 필요 없음.**
- `RESULT: FAIL (invariant or model-independent regression)`은 전 모델 공통의 기지 실패 26건(t3
  group-by 채점 긴장 등)이 건드리는 무해한 트립와이어로 판정됐다(교차 모델 대조로 확인). 신규 슬롯
  조합을 돌려도 이 FAIL이 뜰 수 있는데, 그 자체는 신경 쓰지 않아도 된다 — `error` 필드 유무와
  `model.fallback spans`만 진짜 신호다.

---

## 2. 현재 프로덕션 배정 (baseline) — 슬롯 × 9모델 성적 매트릭스

프로덕션 SoR은 `docs/model-orchestration.md` §15, 코드는 `orthus/models/orchestration.py:106`
`ASSIGNMENTS` 딕셔너리다. 이번 벤치마크가 다루는 6개 슬롯(task)의 현재 배정:

| task 코드 | ASSIGNMENTS 키 | 문항수 | **현재 배정 모델** |
|---|---|---|---|
| t3 | `TASK_STRUCTURED` | 28 | **solar** |
| t5 | `TASK_ROUTING` | 21 | **exaone** |
| t6 | `TASK_INTENT` | 20 | **solar** |
| t7 | `TASK_DECOMPOSE` | 22 | **exaone** |
| t9 | `TASK_GRAPH_BIND` | 32 | **ax** |
| t10 | `TASK_DELEGATION_EXTRACT` | 22 | **exaone** |

배정 이유는 `orthus/models/orchestration.py:106-131`의 인라인 주석에 있다 — 핵심은 국내 3모델이
대부분 태스크에서 McNemar 유의차가 없어(p>0.05) **의도적으로 다양화**했다는 것(owner-picked,
2026-07-20). 즉 지금 배정이 "그 태스크 최고 성능 모델"이 아닐 수 있다는 게 출발점이다.

### 9모델 × 6슬롯 pass/n 전체 매트릭스 (145문항 공통셋, 2026-07-21 측정)

```
모델                 t3/28  t5/21  t6/20  t7/22  t9/32  t10/22   합계/145
solar (국내)            13     18     19     14     32     16      112
exaone (국내)            15     18     19     15     30     21      118
ax (국내)                10     19     19     11     32     18      109
baseline gpt-4o-mini     15     19     19     14     32     16      115
gpt-4o                   16     19     19     11     32     17      114
glm-5.2                  15     19     19     11     32     19      115
deepseek V3.2            16     19     19     16     32     19      121
gpt-5.3-chat-latest      16     19     19     14     32     19      119
deepseek-v4-pro          16     19     19     11     32     17      114
─────────────────────────────────────────────────────────────────────
현재 배정(solar/exaone/  13     18     19     15     32     21      118 ← baseline
solar/exaone/ax/exaone)                                              (81.38%)
```

### 슬롯별 국내-최강 / 전체-최강 (이미 계산됨)

| task | 현재 배정·점수 | 국내 최강·점수 | 전체(9모델) 최강·점수 |
|---|---|---|---|
| t3 | solar 13 | **exaone 15** | gpt-4o/deepseek/gpt-5.3/v4pro **16** (4-way tie) |
| t5 | exaone 18 | **ax 19** | ax/baseline/gpt-4o/glm/deepseek/gpt-5.3/v4pro **19** (7-way tie) |
| t6 | solar 19 | solar/exaone/ax **19** (동점, 배정 이미 최강) | 전 9모델 **19** (전원 동점) |
| t7 | exaone 15 | exaone **15** (배정 이미 국내 최강) | deepseek V3.2 **16** |
| t9 | ax 32 | ax/solar **32** (배정 이미 국내 최강) | ax/solar/baseline/gpt-4o/glm/deepseek/gpt-5.3/v4pro **32** (전원 동점, exaone만 30) |
| t10 | exaone 21 | exaone **21** (배정 이미 국내 최강이자 전체 단독 최강) | exaone **21** (단독) |

**즉시 계산 가능한 상한 두 가지 (API 호출 0, 지금 바로 알 수 있음):**

- **국내 3모델 한정, 슬롯별 최강으로만 재배정**: t3=exaone(15) + t5=ax(19) + t6=19 + t7=15 + t9=32 +
  t10=21 = **121/145 = 83.45%** — 공교롭게도 DeepSeek V3.2(1위)와 정확히 동점. (`e2e_report.md`
  §7.3b에 이미 서술돼 있음.)
- **9모델 전체 개방, 슬롯별 최강으로만 재배정** (이 문서에서 처음 집계): t3=16 + t5=19 + t6=19 +
  t7=16(deepseek V3.2) + t9=32 + t10=21(exaone) = **123/145 = 84.83%**. 즉 해외 대형모델까지 슬롯
  후보로 열어도 국내-한정 최적(121) 대비 딱 2문항(t3, t7 각 1개)만 더 얻는다 — **열어봐야 이득이
  작다는 것 자체가 하나의 결론감**이다.

⚠️ **주의**: 위 두 상한은 "그 슬롯에서 가장 pass 수가 많은 모델을 그대로 고른다"는 **aggregate 집계
산술**이다. 실제로 이 조합이 통계적으로 baseline(118/145)보다 유의하게 나은지 검증하려면
**문항 단위(id-level) 정오답 벡터**가 필요하다 — `combine_stats.py`가 그 인프라(공통 id 계산,
`KNOWN_SOURCES` 소스 핀, McNemar)를 이미 갖고 있으니 §3.A에서 그대로 재사용한다.

---

## 3. 실험 트랙 3가지 — 목적에 따라 고를 것

### 트랙 A — 계산만으로 슬롯 조합 탐색 (API 비용 0, 가장 먼저 할 것)

목적: 이미 측정된 9모델 데이터 안에서 슬롯 배정을 바꿔보고, 후보 조합들을 baseline(118/145)과
McNemar로 통계 검증한다. 신규 벤치마크 실행이 전혀 필요 없다.

절차:
1. `combine_stats.py`를 열어 id-level correctness 벡터를 만드는 함수(`KNOWN_SOURCES` 기반 로딩 +
   t10 재채점)를 확인한다. 이 함수는 모델 슬러그를 받아 `{id: bool}` 딕셔너리를 반환하는 형태일
   것이다 — 그대로 import해서 쓴다.
2. 각 슬롯(t3/t5/t6/t7/t9/t10)마다 배정하고 싶은 모델 슬러그를 골라(예: §2 표의 "전체 최강" 열)
   해당 task의 id만 그 모델의 correctness 벡터에서 골라내 합성 벡터를 만든다. (task별 id 집합은
   `combine_stats.py`가 이미 계산하는 `per_task_on_common`의 부산물이므로 재활용.)
3. 합성 벡터 vs baseline(현재 §15 배정 그대로 조립한 벡터, 이미 `orchestration_composite_9model.json`에
   있음) vs 9모델 각각을 `mcnemar_from_correct` + `bootstrap_paired_diff_ci`로 비교.
4. 결과를 `experiments/fugu-ko/analysis/raw/orchestration_composite_<실험명>.json`으로 저장
   (기존 파일 명명 규칙을 따를 것, 기존 파일들은 덮어쓰지 않는다).
5. 원하는 만큼 다른 조합을 반복 — 이 트랙은 **완전히 결정론적이고 무료**이므로 여러 가설(예: "지연
   낮은 국내모델만", "정확도만 최적화", "국내모델 우선+동점일 때만 대형모델")을 비교표로 만들 수 있다.

이 트랙만으로도 "슬롯을 자유롭게 바꾸면 최대 얼마나 오르는가"에 대한 답은 이미 §2에 있다(121~123/145,
83.45~84.83%) — 트랙 A는 그 상한을 **통계적으로 유의한지** 검증하는 것이 핵심 가치다.

### 트랙 B — 새 모델을 슬롯 후보로 추가 벤치마킹 (API 호출 필요)

목적: 지금 9모델 풀에 없는 모델(예: 특정 슬롯에 특화된 신규 국내/해외 모델)을 추가로 벤치마킹해 슬롯
후보를 늘린다.

절차는 `NEW_MODEL_EVAL_HANDOFF.md`를 그대로 따른다(§3 어댑터 추가, §6 카나리아→본실행, §1 DB 함정,
§2 `--final-verify` 게이트 전부 동일). 특히 이번 실험이 "슬롯 하나만" 관심 대상이라면 `--tasks`로
좁혀서 비용을 아낄 수 있다:

```bash
# 예: t9(graph_bind) 슬롯 후보 모델 하나만 스모크
PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" \
uv run python experiments/fugu-ko/harness_e2e.py \
  --models "<new_slug>" \
  --tier A --layer all --tasks t9 --limit 10 --final-verify
```

단, task별 문항 수가 적으면(t6=20, t5=21이 가장 적음) 통계 검정력이 약하다는 점을 감안 — 새 모델
후보가 근소하게 이겨도 유의차가 안 날 수 있다(McNemar는 discordant pair가 적으면 p값이 잘 안 내려간다).

### 트랙 C — 검증된 조합을 실제 프로덕션 `ASSIGNMENTS`에 반영

목적: 트랙 A/B로 통계적으로 유의하거나 owner가 의도를 갖고 선택한 조합을 실제 코드에 반영한다.

**이것은 owner 게이트가 필요한 변경이다** — `docs/model-orchestration.md`가 이미 명시하듯 "prod
전환은 owner 게이트"(§12.4와 동일 원칙이 §15에도 적용됨). 절차:

1. `orthus/models/orchestration.py:106` `ASSIGNMENTS` 딕셔너리의 해당 태스크 키(`TASK_STRUCTURED` 등)
   값을 변경. **인라인 주석에 근거(정확도/지연/p값)를 남긴다** — 기존 코드 스타일이 그렇다(예:
   `# 86.4% (n=59) — best domestic`).
2. `docs/model-orchestration.md` §15(SoR)를 같은 근거로 갱신 — 이 문서가 실제 SoR이므로 코드만
   바꾸고 문서를 안 바꾸면 AGENTS.md 관례 위반이다.
3. `tests/unit/test_model_orchestration.py`에 배정 회귀 테스트가 있는지 확인(`grep -n ASSIGNMENTS
   tests/`), 있으면 기대값을 갱신하고 없으면 새 배정이 결정론적으로 로드되는지 확인하는 테스트를
   추가한다.
4. **LLM-only 실행 금지, 결정론 상수 테이블 원칙 준수** — `ASSIGNMENTS`는 여전히 고정 딕셔너리여야
   하며, 확신도/드리프트 기반 동적 라우팅으로 바꾸지 않는다(AGENTS.md 절대 규칙, `docs/model-orchestration.md`
   "확신도 routing 아님 — 결정론 상수 테이블" 명시).
5. PR 규칙(AGENTS.md "PR / 커밋") 따라 `.github/pull_request_template.md` 체크리스트 채워 PR — 이
   변경은 프로덕션 모델 배정이라 Protected Area 해당 가능성이 높으니 self-merge 전 owner review.

---

## 4. DB/게이트 함정 요약 (자세한 내용은 `NEW_MODEL_EVAL_HANDOFF.md` §1–§2)

트랙 A는 이미 만들어진 raw jsonl만 읽으므로 이 함정과 무관하다. **트랙 B로 새 벤치마크를 돌릴 때만**
해당:

1. DB는 반드시 populated `orthus_company` — 빈 `orthus`로 돌리면 t3가 전 모델 가짜 실패.
2. `ORTHUS_PG_DSN`뿐 아니라 `ORTHUS_PG_DSN_READONLY`도 같이 `orthus_company`로 override해야 한다(t3
   SQL 실행이 readonly DSN을 탄다).
3. 이름에 `staging`/`test`가 들어간 DB는 하네스가 자동 TRUNCATE한다 — 절대 쓰지 말 것.
4. 대형/프론티어 모델은 `--final-verify` 플래그 없이 거부된다(`_LARGE_PREFIXES` + `_is_large_slug`,
   `harness_e2e.py:72-94`).
5. reasoning/thinking 모델(GLM-5.2, DeepSeek V4 Pro 등)은 사전 스모크로 비용을 가늠할 것 — latency
   p95가 9~12초까지 튄 전례가 있다.

---

## 5. 통계 판정 원칙 (그대로 따를 것)

- McNemar exact test, p<0.05만 유의로 표기.
- **p가 0.05 경계에 근접한 값(예: 0.04~0.06)은 확정 유의차로 승격하지 않는다** — 이번 세션에서
  DeepSeek V4 Pro vs V3.2가 p=0.0391로 나왔을 때도 이 원칙에 따라 서술 수위를 낮췄다
  (`e2e_report.md` 참고).
- 부트스트랩 CI는 `n_resamples=10000, seed=1234` 고정 — 다른 seed로 돌리면 기존 결과와 재현
  비교가 안 되니 반드시 이 값을 유지한다.
- 합성/조립 점수는 항상 **"실제 통합 실행이 아니라 사후 스티칭"**이라는 정직성 고지를 붙인다
  (`e2e_report.md` §7.0 문구 재사용).

---

## 6. 원본 raw jsonl이 유실됐을 경우 (재실행 필요 시)

`analysis/raw/e2e_*.jsonl`은 `.gitignore` 대상이라 세션 간 파일시스템이 바뀌면 사라질 수 있다.
그 경우 `NEW_MODEL_EVAL_HANDOFF.md` §6의 커맨드로 모델별 재실행:

```bash
cd <repo>
export ORTHUS_PG_DSN="postgresql+psycopg://orthus:<PW>@localhost:5433/orthus_company"
export ORTHUS_PG_DSN_READONLY="postgresql+psycopg://orthus_ro:<RO_PW>@localhost:5433/orthus_company"
PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" \
uv run python experiments/fugu-ko/harness_e2e.py \
  --models solar,exaone,ax,baseline,openai:gpt-4o,glm:glm-5.2,deepseek,openai:gpt-5.3-chat-latest,deepseek:deepseek-v4-pro \
  --tier A --layer all --tasks t3,t5,t6,t7,t9,t10 --final-verify
```

(9모델 동시 실행 가능 여부는 `--models` comma-list 지원 여부에 달렸다 — `build_e2e_pool()`이 슬러그별로
독립 빌드하므로 가능할 것으로 보이나, 이번 세션은 모델별로 분리 실행했다. 동시 실행 시 대형모델 게이트가
전체를 막지 않는지 먼저 소수 모델로 확인할 것.)

phase6/composite JSON도 유실됐다면 `combine_stats.py --models <9개 슬러그 콤마> --out
<경로>`로 재생성.

---

## 7. 참고 문서 링크 목록

- `experiments/fugu-ko/e2e/NEW_MODEL_EVAL_HANDOFF.md` — 신규 모델 1개 추가 절차(트랙 B가 이걸 그대로 씀).
- `experiments/fugu-ko/analysis/e2e_report.md` §6(9모델 순위) §7(오케스트레이션 합성, §7.3a/§7.3b
  슬롯 최적성).
- `experiments/fugu-ko/analysis/raw/orchestration_composite_9model.json` — baseline 합성 상세.
- `experiments/fugu-ko/analysis/raw/phase6_verified_stats_9model.json` — 9모델 원 통계.
- `experiments/fugu-ko/e2e/combine_stats.py` — 병합/통계 도구(재사용 필수).
- `experiments/fugu-ko/e2e/runner_lib.py` — McNemar/부트스트랩/DB가드 원함수.
- `orthus/models/orchestration.py:106` — 프로덕션 `ASSIGNMENTS` 코드.
- `docs/model-orchestration.md` §15 — 배정 SoR(트랙 C에서 갱신 대상).
- `docs/model-orchestration.md` §11.3b, §11.4, §12.4 — "동점 구간 안에서의 선택"이라는 기존
  방법론 원칙(신규 배정도 이 프레임을 벗어나지 않아야 함 — 유의차 없는 동점 구간에서의 다양화는
  정당하지만, 유의하게 나쁜 모델을 배정하지 않도록).
