# judge 파일럿 — gpt-4o judge vs Claude Sonnet 4.6 judge

wiki_qa/synthesize의 판정자를 gpt-4o에서 **Claude Sonnet 4.6(Bedrock)**으로 바꾸기 전,
두 판정자가 같은 것을 보고 같은 판정을 내리는지 재는 소규모 파일럿.

- 스크립트: `experiments/fugu-ko/e2e/judge_pilot.py` (재개 가능, jsonl append)
- 원시 판정: `experiments/fugu-ko/e2e/judge_pilot.jsonl` (180행 = 90단위 × 2judge)
- 실행일: 2026-07-29

## 방법

E4 PoLL과 동일하게 **워커 출력을 고정하고 판정층만 교체**했다. 신규 추론 0회 —
`analysis/raw/t2h_{solar,ax,exaone}.jsonl`에 이미 저장된 t2 홀드아웃 30문항 답변을
그대로 재사용했다. 프롬프트(`_SYS`/`_prompt`)와 근거 로더(`_page_body`)는
`t2_holdout_judge.py`에서 **문자열 그대로 import**했다 — 재작성하면 측정 대상이
"judge 차이"가 아니라 "프롬프트 차이"가 된다.

- 쌍 3종: solar×exaone, solar×ax, exaone×ax (원본 PAIRS 2종 + exaone×ax)
- **양방향 스왑 2회**(A↔B), 두 방향이 일치할 때만 승패 인정, 불일치 = tie (원본 규약)
- judge 2인: gpt-4o(`ORTHUS_LLM_API_KEY`) / `us.anthropic.claude-sonnet-4-6`
  (Bedrock Converse, `ORTHUS_LLM_BEDROCK_API_KEY` + `us-east-1`, temperature 0)
- **실제 콜 360회** (gpt4o 180 + sonnet 180) = 예산 상한과 정확히 일치. JSON 파싱 실패 0건,
  API 실패 0건. 판정단위 n = 3쌍 × 30문항 = 90.

> 실패 정책은 원본과 다르게 잡았다. 원본 `_vote`는 모든 예외를 tie로 삼키는데, 판정자
> **비교**에서는 그게 치명적이다 — 조용한 API 실패가 tie로 둔갑해 일치율을 부풀린다.
> 여기서는 API 실패는 예외로 올려 즉시 중단하고 JSON 파싱 실패만 tie로 흡수하며 별도
> 카운트한다. 이번 실행에서는 둘 다 0건이라 모든 tie가 진짜 판정이다.

## 결과

| 지표 | 값 | 합격선 | 판정 |
|---|---|---|---|
| 판정 일치율 (승/패/tie 3값) | **70.0%** (63/90) | ≥70% | ✅ (경계값) |
| Cohen's kappa | **0.535** | ≥0.4 | ✅ |

### 불일치 27건의 성격

| 유형 | 건수 | 뜻 |
|---|---|---|
| 한쪽만 tie | 18 | 승패 방향은 안 싸움 (gpt4o가 tie→sonnet이 승자 지목 13, 역 5) |
| 승자 반대 | **9** | 진짜 모순 |

tie를 뺀 승패 방향만 보면 34/43 = **79.1%** 일치.

### judge 자기일관성 (양방향 불일치 = tie 비율)

| judge | tie 비율 | position_bias | both_tie | partial |
|---|---|---|---|---|
| gpt-4o | 46.7% (42/90) | 16 | 16 | 10 |
| **Sonnet 4.6** | **37.8% (34/90)** | **5** | 23 | 6 |

`position_bias` = 스왑해도 같은 **자리**를 골랐다(= 답변이 아니라 위치를 봤다).
Sonnet이 16→5로 3배 낮다. Sonnet의 tie는 대부분 양방향 모두 "tie"라고 명시한
`both_tie`(23)이고, 위치에 흔들린 tie는 거의 없다.

### 쌍별 — ⚠️ 판정자 교체가 결론을 바꾸는 지점

| 쌍 | 일치 | kappa | gpt-4o 판정 | Sonnet 판정 |
|---|---|---|---|---|
| solar×exaone | 17/30 (57%) | 0.337 | solar 8 / exaone 5 / tie 17 | solar 10 / exaone 9 / tie 11 |
| solar×ax | 18/30 (60%) | 0.393 | solar **16** / ax 5 / tie 9 | solar 11 / ax **10** / tie 9 |
| exaone×ax | 28/30 (93%) | 0.889 | exaone 10 / ax 4 / tie 16 | exaone 12 / ax 4 / tie 14 |

승자 반대 9건이 **예외 없이 전부 gpt-4o=solar / Sonnet=상대편** 방향이다
(solar×ax 6건, solar×exaone 3건). 즉 두 판정자의 차이는 잡음이 아니라 **체계적 편향차**다 —
gpt-4o가 solar를 더 후하게 본다.

## 판정

전체 합격선은 통과한다(일치율 70.0%, kappa 0.535). 자기일관성만 놓고 보면 Sonnet이 오히려
**더 나은 판정자**다 — 위치에 흔들린 tie가 gpt-4o의 1/3이고(16→5), tie를 남발하는 대신
근거를 보고 승자를 지목하는 비율이 높다. 다만 일치율이 70.0%로 합격선에 정확히 걸쳐 있어
여유가 없고, 불일치가 무작위가 아니라 **solar에 대한 판정자별 호오차**라는 점이 중요하다.
gpt-4o judge로 얻은 "solar가 ax를 16-5로 이긴다"는 solar×ax 결론은 Sonnet judge에서
11-10 무승부로 무너지므로, **판정자를 바꾸면 그 쌍의 기존 결론은 이월되지 않는다** —
Sonnet judge로 본 실행을 진행하되, 과거 gpt-4o judge 수치와 직접 비교하거나 섞어 쓰면 안 된다.
"국내 3모델 간 유의차 없음"이라는 큰 결론 자체는 Sonnet judge에서 오히려 더 강해진다
(solar의 우위가 줄어드는 방향).
