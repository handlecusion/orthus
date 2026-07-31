# NEW_MODEL_EVAL_HANDOFF — 신규 대형모델 1개를 7모델 비교표에 추가하기

**한 줄 요약:** 이 문서는 오늘 세션에서 완주한 국내 4모델(Solar/EXAONE/A.X/baseline
gpt-4o-mini) + 대형 3모델(GPT-4o/GLM-5.2/DeepSeek V3.2) E2E 벤치마크에 **신규 대형모델
1개(또는 여러 개)를 기존 145문항 공통 채점셋에 추가**해 7모델 → N모델 비교표로 편입시키는
절차를 담는다. 이 문서 하나만 읽고 다른 세션에서 바로 실행할 수 있도록 self-contained로
작성했다. `experiments/fugu-ko/e2e/SESSION_HANDOFF.md`(오늘 세션 재개용, 다른 목적)와는
별개 문서다.

---

## 1. ⚠️ 반드시 지켜야 할 DB 함정 3가지

오늘 세션에서 실제로 겪은 실패이고, 원인 분석은 `analysis/e2e_report.md` §5.2(a)에
"측정 보정 이력"으로 기록돼 있다.

### 1.1 DB 타깃은 반드시 `orthus_company` — 빈 `orthus`로 돌리면 t3가 전모델 가짜 실패한다

repo-root `.env`의 기본 개발 DB는 `orthus`이고 여기의 `notion_rows`는 **0행**이다. t3는
`notion_rows` JSONB store 대상 read-only NL→SQL이므로, DB가 비어 있으면 SQL은 정상
컴파일되지만 결과 row가 항상 0/빈 배열이다 — LLM 회귀가 아니라 환경 문제인데도 t3
exact-scored 18건이 **전 모델 동일하게** 가짜 실패한다(gate=True, rows=`[]`/`[[0]]`).
골든은 populated company corpus(1,365 notion_rows, `협업업무표` 794행 = 골든
`[7,16,771]` 합) 기준으로 계산돼 있으므로, **populated `orthus_company` DB로 돌려야 한다.**

### 1.2 `ORTHUS_PG_DSN`뿐 아니라 `ORTHUS_PG_DSN_READONLY`도 같이 override해야 한다

t3의 SQL **실행**은 별도 필드 `pg_dsn_readonly`(env `ORTHUS_PG_DSN_READONLY`)를 탄다.
메인 DSN(`ORTHUS_PG_DSN`)만 `orthus_company`로 바꾼 1차 재실행은 t3 SQL 실행이 여전히
빈 `orthus`를 읽어 18/18 실패가 그대로 유지됐다(오늘 세션의 실제 1차 재실행 실패
사례). **두 env var를 모두** `orthus_company`(readonly 쪽은 `orthus_ro` 롤 사용)로
override해야 비로소 복구된다.

### 1.3 이름에 `staging`/`test`가 들어간 DB는 하네스가 자동 TRUNCATE한다 — 절대 쓰지 말 것

`e2e/runner_lib.py::is_safe_truncate_dsn()`는 DSN의 database 이름에 `"test"` 또는
`"staging"`이 포함될 때만 `True`를 반환하는 **화이트리스트** 가드다. 이 가드가
`True`인 DB는 `truncate_all_tables()`/`apply_fixture()`/`dispatch_l2()`가 L2 fixture 항목
진입 시 `notion_rows` 포함 전 테이블을 TRUNCATE한다. `LIVE_BENCH_RUNBOOK.md` §1.3이
안내하는 "company-staging 클론 사용" 권고는 **t3에 대해서는 틀렸다** — staging 클론으로
t3를 돌리면 실행 중 DB가 비워진다. 이름에 `staging`/`test`가 **없어서** 이 가드가 개입하지
않는 `orthus_company` 직결이 t3의 올바른 타깃이다(반대로 L2 fixture 항목은 staging/test
DB가 필요하지만, 이 문서가 다루는 채점 슬라이스 t3/t7/t9/t10은 전부 L1이라 해당 없다).

### DSN 구성 셸 스니펫

패스워드는 `.env`에서 직접 grep으로 추출하고, echo로 화면에 찍지 않는다(값 노출 금지).
아래는 예시 형태 — 실제 값은 로컬 `~/.orthus/nodes/company/node.env` 또는 repo-root
`.env`의 `ORTHUS_PG_DSN`/`ORTHUS_PG_DSN_READONLY`(또는 그 구성요소인 postgres 계정/포트)를
확인해 채운다.

```bash
cd <repo>

# company DB 접속정보 확인 (값은 절대 echo/print하지 말고, 필요한 필드만 셸 변수로만 사용)
grep -o '^ORTHUS_PG_DSN=.*' ~/.orthus/nodes/company/node.env   # 존재 여부/형태 확인용, 필요시에만

# 두 DSN 모두 orthus_company로 override (host/port/계정은 로컬 환경에 맞게 대체)
export ORTHUS_PG_DSN="postgresql+psycopg://orthus:<PW>@localhost:5433/orthus_company"
export ORTHUS_PG_DSN_READONLY="postgresql+psycopg://orthus_ro:<RO_PW>@localhost:5433/orthus_company"
```

실행 직전에 반드시 두 DSN의 database 이름이 `orthus_company`인지, `staging`/`test`가
섞여 있지 않은지 확인한다.

---

## 2. 게이트 메커니즘 (`--final-verify`)

`harness_e2e.py`는 대형/프론티어 모델 슬러그를 하드코딩된 prefix 목록
(`_LARGE_PREFIXES = ("bedrock:", "openai:gpt-4o", "glm:", "deepseek")`, harness_e2e.py:72)
으로 판별한다. `_is_large_slug()`는 `_DOMESTIC | {"baseline", "mock"}`
(`solar`/`exaone`/`ax`/`baseline`/`mock`) **이외의 모든 슬러그**를 크다고 간주한다
(harness_e2e.py:77-81) — 즉 **아직 목록에 없는 신규 슬러그도 기본적으로 "large"로
취급돼 게이트에 걸린다.**

`enforce_final_verify_gate(slugs, final_verify)`(harness_e2e.py:84)는 `--final-verify`
플래그 없이 large 슬러그가 하나라도 섞여 있으면 즉시:

```text
REFUSED: large/frontier models require --final-verify AND user dataset confirmation. ...
```

를 출력하고 `SystemExit(2)`로 죽는다. 인터랙티브 확인이나 별도 env var는 없다 — 순수
CLI 플래그 하나뿐이다. 이 체크는 `build_e2e_pool()`(어댑터 생성, 네트워크 호출 없음)보다
**먼저** 실행되므로, 게이트 없이 호출하면 어댑터 생성 자체가 일어나지 않는다(벤더 호출 0회).

---

## 3. 신규 모델 추가 방법

### 3.1 신규 모델이 OpenAI 호환 API인 경우 (권장 경로)

기존 3개 어댑터(`_build_openai_chat`/`_build_glm_chat`/`_build_deepseek_chat`,
harness_e2e.py:173-231)는 전부 같은 패턴이다 — 프로덕션 어댑터
`orthus.models.adapters.openai_compat.OpenAIChat`(재구현 아님, 그대로 재사용)을
`base_url`/`api_key`/`model`로 생성하고 `_ProdAdapterUsageWrapper`로 감싼다. 예시
(`_build_deepseek_chat`, harness_e2e.py:209-231):

```python
def _build_deepseek_chat(slug: str) -> Any:
    override = slug.split(":", 1)[1] if ":" in slug else ""
    model = override or os.environ.get("ORTHUS_LLM_DEEPSEEK_MODEL", "deepseek-chat")
    api_key = os.environ.get("ORTHUS_LLM_DEEPSEEK_API_KEY", "")
    if not api_key.strip():
        raise ValueError(
            f"ORTHUS_LLM_DEEPSEEK_API_KEY required for deepseek slugs (slug={slug!r})"
        )
    base_url = os.environ.get("ORTHUS_LLM_DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    from orthus.models.adapters.openai_compat import OpenAIChat

    inner = OpenAIChat(base_url, api_key, model)
    return _ProdAdapterUsageWrapper(inner, model_id=slug)
```

신규 모델을 붙이려면:

1. 같은 패턴으로 `_build_<name>_chat(slug)` 함수를 추가한다. env 3종:
   - `ORTHUS_LLM_<NAME>_API_KEY` — 필수(없으면 `ValueError`로 env 이름 명시, 네트워크
     호출 0회로 죽는다).
   - `ORTHUS_LLM_<NAME>_MODEL` — default 값 하드코딩(모델 카드 확인한 정확한 문자열).
   - `ORTHUS_LLM_<NAME>_BASE_URL` — default 값 하드코딩(공식 OpenAI-호환 엔드포인트).
   - 전부 `orthus/settings.py`의 Pydantic 필드가 **아닌** 하네스 전용 env다
     (`ORTHUS_GLM_API_KEY`/`ORTHUS_LLM_DEEPSEEK_API_KEY`와 동일한 취급).
2. `build_e2e_pool()`(harness_e2e.py:234-265)의 슬러그 분기에 한 줄 추가:

   ```python
   elif slug == "<name>" or slug.startswith("<name>:"):
       pool[slug] = _build_<name>_chat(slug)
   ```

3. `_LARGE_PREFIXES`(harness_e2e.py:72)에 `"<name>"` 추가 — 이걸 빠뜨려도 `_is_large_slug()`의
   기본값이 "목록에 없으면 large"이므로 **게이트 자체는 여전히 걸리지만**, 명시적으로
   추가해 의도를 코드에 남기는 편이 안전하다.

### 3.2 신규 모델이 Bedrock 계열인 경우

기존 `bedrock:<modelId>` 슬러그(`_build_bedrock_chat`, harness_e2e.py:139-170)를
그대로 재사용할 수 있다 — 새 코드 불필요. 단:

- **팀 Bedrock 계정에서 실제로 사용 가능한 모델인지 먼저 확인해야 한다.** 오늘 세션의
  전례: DeepSeek V3.2는 AWS 문서상 Bedrock model card가 존재했지만 **팀 계정 확정
  목록(공식 공지 기준 5종: Claude Sonnet 4.6/Haiku 4.5, Llama 3.3 70B/3.1 8B, Nova
  Pro)에 없었다.** 이 사실은 API 호출 없이는 확인이 안 됐고, 결국 Bedrock 경로를
  폐기하고 `deepseek` 공식 API 직접 호출 슬러그로 전환했다(`PHASE6_MODEL_IDS.md` §2).
  신규 모델도 팀 Bedrock 계정의 확정 공지/사용 가능 목록을 먼저 확인하고, 없으면
  §3.1 경로(공식 API 직접 호출)로 간다.
- `bedrock.py::_normalize_model_id`가 `model_id`에 `us.`/`eu.`/`apac.`/`arn:` 접두가
  없으면 `ORTHUS_LLM_BEDROCK_INFERENCE_PREFIX`(default `us`)를 **무조건** 자동으로
  붙인다. 팀 계정 정책상 `us.` 접두가 필수인 모델도 있고, 반대로 접두를 붙이면 안
  되는(bare modelId로만 존재하는) 모델도 있을 수 있으니 실제 modelId 형식을 모델
  카드에서 확인한다.
- env: `ORTHUS_LLM_BEDROCK_API_KEY`(fallback `ORTHUS_LLM_API_KEY`),
  `ORTHUS_LLM_BEDROCK_REGION`(default `us-east-1`),
  `ORTHUS_LLM_BEDROCK_INFERENCE_PREFIX`(default `us`) — `orthus/models/registry.py`의
  프로덕션 배선과 동일한 이름.
- 오늘 세션 기준 Bedrock API 키는 형식은 정상(`ABSK`로 시작, 132자)이지만 실호출이
  `http_403`으로 실패해 미검증 상태로 남아 있다(`PHASE6_MODEL_IDS.md` §6) — 신규
  세션에서 Bedrock 경로를 쓰려면 키가 여전히 유효한지(토큰 만료 2026-07-31) 먼저
  1콜 카나리아로 확인한다.

---

## 4. t10 채점기 존칭(님/씨) strip — 이미 반영돼 있음, 별도 조치 불필요

`score_l1_exact()`의 t10 분기(harness_e2e.py:387-433)는 비교 전에
`_strip_honorific()`(harness_e2e.py:374-384)을 모델 출력·골든 양쪽 문자열에
대칭 적용한다 — 끝이 `님` 또는 `씨`(선행 공백 유무 무관)로 끝나면 그 접미사를 잘라낸
뒤 비교한다. 신규 모델을 평가해도 이 로직이 자동으로 적용되므로 별도 조치가 필요
없다.

**왜 필요했는지 한 줄:** 원래 채점기는 `assignee` 완전일치를 요구해, 모델이 정답
`"김철수"` 대신 존칭을 붙인 `"김철수님"`을 뽑으면 `mode` 분류가 정확해도 항목 전체가
fail 처리됐다 — EXAONE이 이 패턴으로 6건이 억울하게 fail 처리됐던 것을 발견해
스코어러를 고쳤다(`analysis/e2e_report.md` §5.2(b)).

---

## 5. 방법론 — 반드시 지킬 것 (공정 비교 조건)

t7/t9/t10은 하네스가 **프로덕션 함수를 직접 호출**한다 — 재구현이 아니다:

- t7(decompose) → `should_decompose`, 프로덕션 규칙기 프리필터(ext_tier=0)가 그대로
  적용된 뒤에만 LLM에 도달한다.
- t9(graph_bind) → `bind_graph_params`.
- t10(delegation_extract) → `extract_delegation_intent`.

이 세 태스크는 프로덕션 few-shot 프롬프트/전처리가 그대로 걸린 채로 모델을 부른다 —
즉 이 벤치마크가 재는 것은 **"모델 zero-shot 단독 능력"이 아니라 "이 파이프라인에
장착됐을 때의 성능"**이다. 신규 모델도 **반드시 동일한 배선(프로덕션 함수 직접
호출, 프리필터/프롬프트 변경 없음)으로 평가해야** 기존 7모델과 공정 비교가 성립한다.
다르게(예: 신규 모델 전용 프롬프트 튜닝) 평가하려면 별도의 새 배선이 필요하고, 오늘
세션에는 그런 배선이 없다 — 만들지 말고 기존 경로 그대로 쓴다.

---

## 6. 최소 실행 커맨드

### 6.1 카나리아 (먼저 3~5문항으로 배선/키 확인)

```bash
cd <repo>
export ORTHUS_PG_DSN="postgresql+psycopg://orthus:<PW>@localhost:5433/orthus_company"
export ORTHUS_PG_DSN_READONLY="postgresql+psycopg://orthus_ro:<RO_PW>@localhost:5433/orthus_company"
PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" \
uv run python experiments/fugu-ko/harness_e2e.py \
  --models <new_slug> \
  --tier all --layer all --limit 3 --final-verify
```

확인: `pool build failed` 없이 row 생성, `== invariants ==`의
`model.fallback spans (delta over run): 0 (CLEAN)`, `analysis/raw/e2e_{slug}.jsonl`에
`latency_ms` 채워짐.

### 6.2 tier A 전체(350) 실행 — 신규 모델 단독

```bash
PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" \
uv run python experiments/fugu-ko/harness_e2e.py \
  --models <new_slug> \
  --tier all --layer all --final-verify
```

`--tier all`을 명시해야 tier_b(t3 tier_b d8, n=1438)까지 포함된다(default는 tier=A뿐).
`--layer`는 default가 이미 `all`이라 L1+L2가 함께 실행되지만, L2 fixture 항목은
`orthus_company`(staging/test 아님)에서는 `truncate_guard_ok()`가 `False`이므로 자동
skip된다(§1.3) — L1 read-only 항목(t3 포함)만 실제로 dispatch된다.

### 6.3 채점 슬라이스만 (공통 145 세트에 필요한 태스크만 — 비용/시간 절약)

공통 145 채점셋은 t3/t5/t6/t7/t9/t10 여섯 태스크로 구성된다(t2/t8/t11은 이 비교표
대상이 아님 — t2는 judge 전용 별도 채점, t8은 정성 전용, t11은 manifest에 아예 없음).
`harness_e2e.py`는 `--tasks`(comma filter, 예: `t3,g4` — argparse 정의
`harness_e2e.py:1010`)를 지원하므로 그걸로 좁힐 수 있다:

```bash
PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" \
uv run python experiments/fugu-ko/harness_e2e.py \
  --models <new_slug> \
  --tier all --layer all --tasks t3,t5,t6,t7,t9,t10 --final-verify
```

단 t3는 t3 tier_a(28건) + tier_b d8(n=1438)이 전부 `t3` task 태그이므로 `--tier A`로
tier_a만 좁히지 않으면 tier_b 물량까지 함께 돌아 비용이 커진다. 공통 145 세트에
필요한 것은 tier_a의 t3 28건뿐이므로, 순수 채점 목적이면 `--tier A`로 좁히는 편이
낫다(`--tier all`은 tier_b/L2까지 포함해 전량 벤치용).

출력 위치: `experiments/fugu-ko/analysis/raw/e2e_{slug}.jsonl`(gitignored,
`experiments/fugu-ko/.gitignore`의 `analysis/raw/`).

---

## 7. 비용 가늠 팁 — reasoning/thinking 모델이면 사전 스모크 필수

**사고 사례(오늘 세션):** GLM-5.2는 reasoning 모델이라 출력 토큰이 예상보다 훨씬 많이
나왔다. 사전 추정은 `PHASE6_MODEL_IDS.md` §5.1 기준 grounding-realistic 1회 전량
실행(2,007 아이템) ≈ **$3**였는데, 실제로는 tier A(350문항)만 실행하는 도중
**reasoning-token 초과로 ~$10이 소진**됐고, 그 시점에 "전량 실행하면 $30~40에
이를 것"으로 재추정돼 사용자 승인 하에 **tier A로 scope-down**했다(`STATE.md` 25행).
같은 실행 중 t3의 477개 아이템에서 순차실행 병목(처리량이 시간당 ~40건까지 저하)이
발견돼 병렬 실행으로 분리 수정하기도 했다.

**권고:** 신규 모델이 reasoning/thinking 계열(모델 카드에 "reasoning"/"thinking"
언급, 또는 응답에 별도 reasoning 필드가 있는 API)이면 tier A 전체를 돌리기 전에
**1콜 스모크로 토큰 소비량을 가늠**한다. 스모크 스크립트 패턴 예시:

```python
# 1콜 스모크 — 실제 harness 어댑터로 짧은 프롬프트 1건만 호출해 usage 확인
import os
from orthus.models.adapters.openai_compat import OpenAIChat

chat = OpenAIChat(
    base_url=os.environ["<BASE_URL_ENV>"],
    api_key=os.environ["<API_KEY_ENV>"],
    model=os.environ.get("<MODEL_ENV>", "<default-model>"),
)
out = chat.complete(system="너는 도우미다.", user="1+1은?")
print(out)
# 어댑터가 raw usage를 반환하지 않으면(OpenAIChat/BedrockConverseChat 공통 한계,
# harness_e2e.py의 _ProdAdapterUsageWrapper 주석 참고) 벤더 대시보드/청구 콘솔에서
# 이 1콜의 실제 토큰/비용을 직접 확인한다.
```

`--limit 3~5`로 하네스 카나리아를 먼저 돌려보는 것(§6.1)도 같은 목적이지만, reasoning
모델은 문항 난이도에 따라 토큰 소비가 크게 튈 수 있으므로 하네스 카나리아 뒤에도
`--limit 20` 정도의 중간 규모 확인을 한 번 더 끼워 넣는 것을 권장한다.

---

## 8. 결과 병합 방법

### 8.1 병합 로직 요약 (`phase6_combine.py`)

Phase 6 병합에 실제로 쓰인 스크립트는 세션 스크래치패드
(`/tmp/<session>`)에 있었고 **세션 종료 시 사라질 수
있으므로**, 신규 세션은 아래 로직 요약을 보고 동등한 스크립트를 새로 작성한다:

1. **id 교집합(common set) 계산.** 이미 채점된 국내 4모델(P5: solar/exaone/ax/baseline)과
   기존 대형 3모델(P6: gpt-4o/glm-5.2/deepseek)의 per-id correctness(`score.status
   in ("pass","fail")`인 항목만)를 모델별 dict로 만들고, **전 모델(P5+P6+신규모델)의
   id 집합 교집합**을 `common`으로 잡는다. GLM처럼 tier A만 돈 모델이 있으면 그 모델의
   id-set이 교집합을 좁힌다 — 신규 모델이 tier A만 돌았다면 마찬가지로 그 범위로
   좁혀진다.
2. **정확도**: `common` 위에서만 모델별 `passed/n`을 계산한다(전량 실행분 중 공통
   범위 밖은 "context용 전체 채점 정확도"로 별도 표기만 하고 순위표에는 안 씀).
3. **task별 breakdown**: `common` 내에서 task별로 pass/n을 다시 집계(`per_task_on_common`).
4. **paired McNemar + bootstrap CI**: 모델 쌍마다 `common` 위에서
   `e2e/runner_lib.py::mcnemar_from_correct(a_correct, b_correct)`(discordant pair
   `b`=A만 정답, `c`=B만 정답, `exact_mcnemar(b,c)`로 p-value)와
   `bootstrap_paired_diff_ci(a_correct, b_correct, n_resamples=10000, seed=1234)`를
   그대로 재사용한다 — 재구현하지 않는다.
5. **empty-output contamination 체크**: `common` 내 빈 출력(None/""/[]/{})이 전부
   t10 expected-null(정답=담당자 없음) 케이스인지 확인 — 아니라면 진짜 모델 결함
   가능성으로 표기.
6. **latency**: `common` 위 `latency_ms`의 p50/p95(scored-common)와, `reached_llm=True`
   행만의 p50/p95(reached-llm) 둘 다 낸다.
7. 결과를 `analysis/raw/phase6_verified_stats.json` 형태(JSON)로 저장 — 신규 모델을
   추가하면 이 파일을 재생성(덮어쓰기 또는 새 파일)하고, `models` 리스트에 신규
   슬러그를 추가한다.

t10 재채점(§4)이 필요한 것은 **P5 국내 4모델의 원본 raw jsonl에만** 해당한다 — P6/신규
대형모델은 이미 존칭-보정 채점기가 적용된 상태로 실행되므로(오늘 세션 이후
`harness_e2e.py`에 고정 반영됨) 재채점 불필요.

### 8.2 `analysis/e2e_report.md`에 행 추가

1. §6.0(7모델 executive summary), §6.x 순위표(§10의 스냅샷 표와 같은 형식), 페어와이즈
   McNemar 표에 신규 모델 행/열을 추가한다.
2. `n_common_scored`가 145에서 바뀌면(신규 모델의 id-set이 기존 145보다 좁으면) 그
   사실과 새 n을 명시한다 — GLM-5.2가 tier A만 돌아 공통 세트를 145로 고정시킨 전례가
   있다(§7).
3. 새로 계산된 McNemar p-value/부트스트랩 CI를 표에 채우고, 유의(p<0.05)/비유의를
   명시한다. borderline(p가 0.05에 근접)은 §5.2(b)의 EXAONE vs A.X p=0.049 사례처럼
   "확정 유의차로 승격하지 않는다"는 원칙을 따른다.
4. 비용/지연 실측치가 있으면 §5.1 스타일 표(추정 vs 실측)에 추가한다.

---

## 9. 참고 문서 링크 목록

- `experiments/fugu-ko/e2e/LIVE_BENCH_RUNBOOK.md` — Phase 5/6 원 런북(키 설정, DB 준비,
  게이트 negative test, 국내 풀 실행 절차). **주의:** §1.3의 "company-staging 클론
  권장"은 t3에는 적용되지 않는다(§1.3 참고, 본 문서가 우선).
- `experiments/fugu-ko/e2e/PHASE6_MODEL_IDS.md` — Bedrock/GLM/DeepSeek/GPT-4o 정확한
  modelId·base_url·비용 추정치 조사 기록.
- `experiments/fugu-ko/analysis/e2e_report.md` — 최종 통계 리포트. §5.2 "측정 보정
  이력"(DB/t10 버그 보정), §6 "7모델 결론", §7 "Phase 7 오케스트레이션 합성 — 사후
  조립 가상 점수임을 먼저 명시".
- `experiments/fugu-ko/analysis/raw/phase6_verified_stats.json` — 7모델 최종 통계
  (accuracy/per-task/pairwise McNemar/latency/error triage), 신규 모델 추가 시 이
  구조를 유지해 재생성.
- `experiments/fugu-ko/analysis/raw/orchestration_composite.json` — 프로덕션 슬롯
  배정(`orthus/models/orchestration.py::ASSIGNMENTS`)을 그대로 적용한 사후 조립 합성
  점수(81.38%, DeepSeek와 p=0.45로 통계 동률) — **실제 통합 실행이 아니라 이미 측정된
  결과의 사후 스티칭**이다(§7.0 정직성 고지 그대로 유지할 것).
- `experiments/fugu-ko/e2e/SMOKE_FAIL_TRIAGE.md` — 과거 스모크/에러 트리아지 기록.
- `experiments/fugu-ko/e2e/STATE.md` — 세션 진행 상태 로그(하드 게이트 정의, Phase 6
  실행 로그 요약, 남은 항목).
- `experiments/fugu-ko/e2e/SESSION_HANDOFF.md` — 오늘 세션 자체를 이어가기 위한
  핸드오프(본 문서와 목적이 다름 — 신규 모델 평가가 아니라 오늘 세션 상태 복원용).
- `experiments/fugu-ko/harness_e2e.py` — 하네스 본체. 어댑터 추가는 여기(§3).
- `experiments/fugu-ko/e2e/runner_lib.py` — DB 안전 가드(`is_safe_truncate_dsn`,
  `truncate_guard_ok`), 통계 함수(`mcnemar_from_correct`, `bootstrap_paired_diff_ci`).
- `docs/model-orchestration.md` §15 — 현재 프로덕션 코드 배정(ASSIGNMENTS) SoR.

---

## 10. 현재 7모델 순위 스냅샷 (참고용 — 신규 모델 비교 기준선)

출처: `analysis/raw/phase6_verified_stats.json`, `n_common_scored=145`.

| 순위 | 모델 | 정확도 | pass/n |
| --- | --- | --- | --- |
| 1 | DeepSeek V3.2 | 83.45% | 121/145 |
| 2 | EXAONE | 81.38% | 118/145 |
| 3 | baseline (gpt-4o-mini) | 79.31% | 115/145 |
| 3 | GLM-5.2 | 79.31% | 115/145 |
| 5 | GPT-4o | 78.62% | 114/145 |
| 6 | Solar | 77.24% | 112/145 |
| 7 | A.X | 75.17% | 109/145 |

- **1위 DeepSeek과 2위 EXAONE(국내)은 McNemar p=0.4531로 통계적 동률**이다
  (discordant: EXAONE-only 2, DeepSeek-only 5, n_paired=145). DeepSeek는 EXAONE을
  제외한 나머지 5모델 전부에 p<0.05로 유의하게 앞선다(예: DeepSeek vs baseline
  p=0.03125).
- GPT-4o vs baseline은 p=1.000으로 완전 비유의.
- **오케스트레이션 합성(프로덕션 슬롯별 국내모델 조립, 사후 스티칭)**은 81.38%로
  EXAONE과 정확히 같은 값이고(t3=solar, t5=exaone, t6=solar, t7=exaone, t9=ax,
  t10=exaone 슬롯 배정), DeepSeek와도 **p=0.45로 통계 동률**이다
  (`orchestration_composite.json`). 단 이는 **실제 통합 실행이 아니라 각 모델이
  독립적으로 이미 전체 세트를 돌린 결과를 배정표대로 사후에 이어붙인 것**이며
  슬롯 간 fallback/retry/컨텍스트 상호작용은 반영돼 있지 않다(`e2e_report.md` §7.0).

신규 모델을 추가하면 위 표에 행을 추가하고, 신규 모델 대 기존 7모델 각각의
McNemar/부트스트랩 CI를 §8의 절차로 계산해 표에 채운다.
