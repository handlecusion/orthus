# Phase 6 대형모델 modelId/슬러그 확정 (사전 준비 — 호출 아님)

> 이 문서는 `LIVE_BENCH_RUNBOOK.md` §5의 `<BEDROCK_MODEL_ID>`/`<GLM_MODEL_ID>`
> 자리표시자를 채우기 위한 조사 결과다. **API 호출 0회** — 웹 문서 조사만 했다.
> API 키 값은 어디에도 적지 않는다(env 변수 **이름**만).

> **2026-07-21 공식 공지 갱신:** 팀 계정에서 확정 사용 가능한 Bedrock 모델은
> 아래 §1의 **5종**이며, 리전은 **us-east-1**, Bearer Token(`ABSK`로 시작)
> 만료는 **2026-07-31**이다. 팀 계정 정책상 5종 전부 호출 시 **`us.` 접두가
> 필수**이며, 접두 없이 bare modelId로 호출하면 **HTTP 400**이 난다(AWS
> 문서상의 in-region 지원 여부와 무관하게 팀 계정 정책이 우선). DeepSeek는 이
> 공지의 확정 목록에 **없다** — §2 참고.

## 0. 하네스 배선 요약 (harness_e2e.py 재확인)

- `bedrock:<modelId>` 슬러그 → `_build_bedrock_chat`(harness_e2e.py:139) →
  `orthus.models.adapters.bedrock.BedrockConverseChat`. env:
  `ORTHUS_LLM_BEDROCK_API_KEY`(fallback `ORTHUS_LLM_API_KEY`),
  `ORTHUS_LLM_BEDROCK_REGION`(default `us-east-1`),
  `ORTHUS_LLM_BEDROCK_INFERENCE_PREFIX`(default `us`).
- `glm:<model>` 슬러그 → `_build_glm_chat`(harness_e2e.py:192) → `OpenAIChat`
  (OpenAI-호환) pointed at `_GLM_BASE_URL = "https://api.z.ai/api/paas/v4/"`
  (harness에 고정). env: `ORTHUS_GLM_API_KEY`(하네스 전용, `orthus/settings.py`
  필드 아님).
- `openai:<model>` 슬러그 → `_build_openai_chat` → `OpenAIChat`. env:
  `ORTHUS_LLM_API_KEY`, `ORTHUS_LLM_BASE_URL`(default `https://api.openai.com/v1`).
- `deepseek`(또는 `deepseek:<model>`) 슬러그 → `_build_deepseek_chat`(harness_e2e.py)
  → `OpenAIChat`(DeepSeek 공식 API는 OpenAI-호환), `_build_glm_chat`과 대칭. env:
  `ORTHUS_LLM_DEEPSEEK_API_KEY`(필수), `ORTHUS_LLM_DEEPSEEK_MODEL`(default `deepseek-chat`),
  `ORTHUS_LLM_DEEPSEEK_BASE_URL`(default `https://api.deepseek.com`). 셋 다 하네스
  전용 env(`orthus/settings.py` 필드 아님). `deepseek:<model>` suffix는
  `ORTHUS_LLM_DEEPSEEK_MODEL`을 override한다. **대형모델이므로 `--final-verify`
  게이트 뒤에 있다**(`_LARGE_PREFIXES`에 `"deepseek"` prefix 추가 — 게이트 없이
  호출 시 다른 대형 slug과 동일하게 REFUSED/`SystemExit(2)`).
- **중요 배선 디테일**: `bedrock.py::_normalize_model_id`(orthus/models/adapters/bedrock.py:300)
  가 `model_id`에 `us.`/`eu.`/`apac.`/`arn:` 접두가 이미 없으면 **무조건**
  `ORTHUS_LLM_BEDROCK_INFERENCE_PREFIX`(default `us`)를 자동으로 붙인다.
  즉 슬러그에 `bedrock:anthropic.claude-sonnet-4-6`처럼 접두 없는 순수
  modelId를 넣으면 실제 호출 시 `us.anthropic.claude-sonnet-4-6`으로 나간다.
  **이 자동 접두가 DeepSeek V3.2에는 문제가 된다 — §2 참고.**
- 하나의 harness 프로세스 실행 안에서 `ORTHUS_LLM_BEDROCK_INFERENCE_PREFIX`는
  전역 env 하나뿐이라, `--models bedrock:A,bedrock:B,...`처럼 여러 bedrock
  슬러그를 한 커맨드에 섞으면 전부 같은 prefix 규칙을 공유한다.

## 1. Bedrock 5종 — 공지 확정 modelId (2026-07-21 공식 공지 + AWS 문서 기준)

> 팀 계정: 리전 **us-east-1**, Bearer Token(`ABSK`로 시작) 만료
> **2026-07-31**. 아래 5종은 전부 호출 시 **`us.` 접두 필수**(팀 계정
> 정책 — 미사용 시 HTTP 400). AWS 문서상 in-region 자체 지원 여부(마지막
> 열)와 무관하게 팀 계정 정책이 우선한다.

| 모델 | bedrock-runtime Model ID (`bedrock:<이 값>`) | 실제 호출 ID(`us.` 접두 필수) | 비고 |
| --- | --- | --- | --- |
| Claude Sonnet 4.6 | `anthropic.claude-sonnet-4-6`(**bare, 버전 suffix 없음 — `-v1:0` 등을 붙이지 말 것**) | `us.anthropic.claude-sonnet-4-6` | 러너 §1: us-east-1 In-Region 미지원, Geo/Global만 지원 → `us.` 접두로 정상 동작. **런북에 이미 확정돼 있던 값과 일치(대조 완료)** |
| Claude Haiku 4.5 | `anthropic.claude-haiku-4-5-20251001-v1:0` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | us-east-1 In-Region/Geo/Global 전부 지원. 팀 계정 정책상 `us.` 접두는 여전히 필수 |
| Llama 3.3 70B Instruct | `meta.llama3-3-70b-instruct-v1:0` | `us.meta.llama3-3-70b-instruct-v1:0` | us-east-1 In-Region 미지원(Geo만) → `us.` 접두 필수 |
| Llama 3.1 8B Instruct | `meta.llama3-1-8b-instruct-v1:0` | `us.meta.llama3-1-8b-instruct-v1:0` | us-east-1 In-Region 미지원(Geo만) → `us.` 접두 필수 |
| Amazon Nova Pro | `amazon.nova-pro-v1:0` | `us.amazon.nova-pro-v1:0` | us-east-1 In-Region + Geo 모두 지원. 팀 계정 정책상 `us.` 접두는 여전히 필수 |

harness 커맨드 형태(슬러그, 하네스가 자동으로 `us.` 접두를 붙임):

```
bedrock:anthropic.claude-sonnet-4-6
bedrock:anthropic.claude-haiku-4-5-20251001-v1:0
bedrock:meta.llama3-3-70b-instruct-v1:0
bedrock:meta.llama3-1-8b-instruct-v1:0
bedrock:amazon.nova-pro-v1:0
```

출처:
- [Claude Sonnet 4.6 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html)
- [Claude Haiku 4.5 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html)
- [Llama 3.3 70B Instruct model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-meta-llama-3-3-70b-instruct.html)
- [Llama 3.1 8B Instruct model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-meta-llama-3-1-8b-instruct.html)
- [Amazon Nova Pro model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-pro.html)
- [DeepSeek V3.2 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-deepseek-deepseek-v3-2.html)

## 2. DeepSeek V3.2 — ✅ 공식 API 직접 호출로 전환 (Bedrock 경로 폐기)

> **2026-07-21 결정:** 팀 계정에서 공식으로 확정된 Bedrock 모델은 §1의 5종뿐이고
> DeepSeek는 그 목록에 **없음이 확인**됐다(공지 + 사용자 xlsx). 따라서 Bedrock
> 경로(`bedrock:deepseek.v3.2` + `us.` 접두 충돌 캐비엇 등 아래 옛 본문)는
> **폐기**하고, DeepSeek는 **공식 API 키를 사용자가 직접 발급해 OpenAI-호환
> 엔드포인트로 직접 호출**한다. 하네스에 `deepseek` 슬러그를 신설했다(§0 배선
> 참고, `_build_deepseek_chat` → `OpenAIChat`). 아래 옛 Bedrock 본문(`deepseek.v3.2`
> modelId + 자동 접두 충돌 해결책)은 **더 이상 적용하지 않는다**(역사 기록).

**호출 경로(현행):** DeepSeek 공식 API. OpenAI-호환.

- Base URL: `https://api.deepseek.com`(chat completions 전체 경로는
  `https://api.deepseek.com/chat/completions`). env `ORTHUS_LLM_DEEPSEEK_BASE_URL`로 override 가능.
- Model 문자열: `deepseek-chat`(= V3.2 계열, non-thinking). env `ORTHUS_LLM_DEEPSEEK_MODEL`
  default. `deepseek:<model>` 슬러그 suffix로도 override 가능.
- API 키: env `ORTHUS_LLM_DEEPSEEK_API_KEY`(필수, 사용자 발급). **키 값은 이 문서에도,
  다른 어떤 문서/커밋에도 남기지 않는다 — env 이름만.**

확정 슬러그(게이트 뒤):

```
deepseek
```

harness 커맨드 형태(반드시 `--final-verify` 게이트 뒤):

```
--models deepseek --tier all --layer all --final-verify
```

**⚠️ `deepseek-chat` 모델명 deprecation(2026-07-24):** DeepSeek 공식 문서상
`deepseek-chat`/`deepseek-reasoner` 레거시 이름은 **2026-07-24 15:59 UTC에
deprecate**되고, `deepseek-chat`은 `deepseek-v4-flash`(non-thinking)로 매핑된다.
그 이후 실행하면 과금·동작이 v4-flash 기준으로 바뀌므로, V3.2 계열로 재려면
그 전에 1회 실행하거나 `ORTHUS_LLM_DEEPSEEK_MODEL`을 명시 pin해야 한다(§5 비용 표 참고).

출처: [DeepSeek API Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)

<details><summary>옛 Bedrock 경로(폐기, 역사 기록) — `deepseek.v3.2` + `us.` 접두 충돌</summary>

AWS 공식 model card 기준 bedrock-runtime Model ID는 `deepseek.v3.2`(In-Region only,
Geo/Global "Not supported")였고, 하네스 `bedrock.py::_normalize_model_id`가 default
`ORTHUS_LLM_BEDROCK_INFERENCE_PREFIX=us`를 자동으로 붙여 존재하지 않는 `us.deepseek.v3.2`를
만드는 충돌이 있었다. 슬러그별 `ORTHUS_LLM_BEDROCK_INFERENCE_PREFIX=""` override로 우회하는
해결책을 검토했으나, 팀 계정에 DeepSeek 자체가 없어 이 경로는 폐기됐다.
출처: [DeepSeek V3.2 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-deepseek-deepseek-v3-2.html)

</details>

## 3. GLM-5.2 (Z.ai / Zhipu)

**API 제공 경로:** Z.ai 공식 플랫폼(`api.z.ai`), OpenAI 호환 chat completions
엔드포인트.

**정확한 model 문자열:** `glm-5.2`

**Base URL:** `https://api.z.ai/api/paas/v4/`
(chat completions 전체 경로는 `https://api.z.ai/api/paas/v4/chat/completions`)

harness의 `_GLM_BASE_URL`이 이미 정확히 이 값으로 하드코딩돼 있어 별도 설정
불필요. 확정 슬러그:

```
glm:glm-5.2
```

Z.ai 공식 문서의 cURL 예제(`model: "glm-5.2"`)와 OpenAI SDK 예제
(`base_url='https://api.z.ai/api/paas/v4/'`) 양쪽에서 동일 문자열 확인.

출처:
- [GLM-5.2 (Z.ai/Zhipu AI) official docs — model name + base URL](https://docs.z.ai/guides/llm/glm-5.2)
- 교차 확인: [Models.dev — GLM-5.2](https://models.dev/models/zhipuai/glm-5.2/), [AI/ML API GLM-5.2 reference](https://docs.aimlapi.com/api-references/text-models-llm/zhipu/glm-5.2)(모델명 변형 `zhipu/glm-5-2`는 서드파티 프록시 전용 별칭이며 Z.ai 직결 경로에는 적용되지 않음)

## 4. GPT-4o (OpenAI) — 참고용, 이미 확정

Phase 6의 8개 중 하나지만 이번 조사 대상(DeepSeek/GLM)은 아니다. harness
`openai:gpt-4o` 슬러그가 `ORTHUS_LLM_API_KEY` + `ORTHUS_LLM_BASE_URL`(default
`https://api.openai.com/v1`)로 이미 배선돼 있고, `gpt-4o`는 OpenAI 공식
model 문자열이라 별도 확정 불필요.

## 5. 최종 Phase 6 커맨드 초안 (게이트 후, 실행 전 재검토 필요)

런북 §5 자리표시자를 채운 형태. **DeepSeek는 Bedrock 경로를 폐기하고
`deepseek` 슬러그(공식 API 직접 호출)로 포함한다**(§2 참고):

```
# 공지 확정 Bedrock 5종(us. 접두 필수, 팀 계정 정책) + GPT-4o + GLM-5.2 + DeepSeek(공식 API)
PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" \
uv run python experiments/fugu-ko/harness_e2e.py \
  --models bedrock:anthropic.claude-sonnet-4-6,bedrock:anthropic.claude-haiku-4-5-20251001-v1:0,bedrock:meta.llama3-3-70b-instruct-v1:0,bedrock:meta.llama3-1-8b-instruct-v1:0,bedrock:amazon.nova-pro-v1:0,openai:gpt-4o,glm:glm-5.2,deepseek \
  --tier all --layer all --final-verify
```

DeepSeek만 단독 실행하려면 `--models deepseek --tier all --layer all --final-verify`.
(DeepSeek는 OpenAI-호환 공식 API라 Bedrock의 전역 `INFERENCE_PREFIX` 충돌과 무관하게
같은 커맨드에 섞어도 안전하다.)

### 5.1 Phase 6 예상 비용 (1회 전량 실행, 2,007 아이템)

**토큰 추정 방법.** 입력은 manifest(`tier_a`+`tier_b`+`l2/g*`, pending 제외 =
실행된 2,007 id)의 프롬프트 필드에서 집계했고, 한국어 **1토큰 ≈ 2.5자**(범위 2~3자)
가정을 적용했다. 출력은 Phase 5 raw(`analysis/raw/e2e_solar.jsonl`)의 응답 길이
분포에서 추정했다.

- **manifest-literal(질문/본문 텍스트만) = 하한**: 입력 ≈ 79.9K자 ≈ **0.032M tok**,
  출력 ≈ 95.4K자 ≈ **0.038M tok**(@2.5자/tok). ⚠️ 이 수치는 **바닥값**이다 —
  manifest에는 유저 질문/본문만 있고, 실제 매 호출 프롬프트에 붙는 **시스템
  프롬프트 + 런타임 그라운딩**(t3 NL→SQL의 JSONB 스키마·few-shot ~950+1,200자/호출,
  t2 wiki_qa의 k=5~8 청크 그라운딩)이 빠져 있다. t3가 1,511/2,007(75%)이라 이 고정
  오버헤드가 실제 입력을 지배한다.
- **grounding-realistic(실측 프롬프트 조립 반영) = 대표값**: 입력 ≈ **~2.0M tok**
  (t3 1,511호출 × ~1,200 tok + 나머지 ~0.2M), 출력 ≈ **~0.10M tok**(생성 SQL/라벨/답변
  포함).

**가격**(2026-07, $/M tokens, 캐시 미스 기준):

| 모델 | 입력 $/M | 출력 $/M | manifest-literal 비용 | grounding-realistic 비용 |
| --- | --- | --- | --- | --- |
| **DeepSeek V3.2**(`deepseek-chat`) | $0.28 | $0.42 | ~**$0.03** | ~**$0.60** |
| GPT-4o | $2.50 | $10.00 | ~$0.46 | ~$6.0 |
| GLM-5.2(z.ai 공식) | $1.40 | $4.40 | ~$0.21 | ~$3.2 |

- 대표값(grounding-realistic) 기준 **DeepSeek 1회 전량 ≈ $0.5~0.6**, GPT-4o ≈ $6,
  GLM-5.2 ≈ $3. 전부 런북 §3의 국내 4모델 풀 추정($10~20)보다 낮다(단일 모델 1회).
- ⚠️ `deepseek-chat`은 **2026-07-24 deprecate → `deepseek-v4-flash`(non-thinking,
  $0.14/$0.28)로 매핑**된다. 그 이후 실행하면 오히려 더 싸지지만 모델 실체가 V3.2가
  아니게 되므로, V3.2 계열을 재려면 07-24 전에 실행한다(§2).
- 출처: [DeepSeek 가격](https://api-docs.deepseek.com/quick_start/pricing/) ·
  [GPT-4o 가격](https://pricepertoken.com/pricing-page/model/openai-gpt-4o) ·
  [GLM-5.2 가격(z.ai)](https://docs.z.ai/guides/overview/pricing)

## 6. 잔여 미확정 항목

- **GLM API 키 미발급** — 사용자가 Z.ai 계정에서 직접 발급 필요
  (`ORTHUS_GLM_API_KEY`).
- **DeepSeek API 키 미발급** — 사용자가 DeepSeek 플랫폼에서 직접 발급 필요
  (`ORTHUS_LLM_DEEPSEEK_API_KEY`). Bedrock 경로 폐기 → 공식 API 직접 호출로 전환됨(§2).
  `ORTHUS_LLM_DEEPSEEK_MODEL`(default `deepseek-chat`)/`ORTHUS_LLM_DEEPSEEK_BASE_URL`
  (default `https://api.deepseek.com`)은 필요 시에만 override.
- **Bedrock API 키/토큰 — 형식 정상, 여전히 403** (2026-07-21 재점검):
  `.env`의 `ORTHUS_LLM_BEDROCK_API_KEY` 마지막 정의(dotenv는 마지막 값 사용)는
  `ABSK`로 시작하고, 길이 132자, 공백/따옴표/CR 없음 — 붙여넣기 손상 흔적은
  없다. 그럼에도 `bedrock:anthropic.claude-haiku-4-5-20251001-v1:0`(§1 확정
  5종 중 1개, `us.` 접두 자동 적용, max_tokens=10) 1회 실호출은 여전히
  `status=FAIL reason=http_403`로 실패했다. 즉 **키 값 자체(계정 발급/권한/
  활성화 여부)가 원인일 가능성이 붙여넣기 오류보다 높다** — 이 문서 조사
  범위 밖이며 발급 계정 쪽 재확인이 필요하다.
- **DeepSeek는 팀 Bedrock 계정에 없음이 확인됨** → 공식 API 직접 호출로 전환
  완료(§2/§5). `deepseek` 슬러그 배선 + `--final-verify` 게이트 뒤 배치 완료
  (오프라인 네거티브 게이트 REFUSED 확인). 남은 것은 사용자 키 발급뿐.
- **리전 선택** — 공지 확정: **us-east-1**. harness default
  `ORTHUS_LLM_BEDROCK_REGION=us-east-1`과 일치하므로 변경 불필요.
- **토큰 만료** — 공지 확정: **2026-07-31**. 그 이후 재발급 필요.
