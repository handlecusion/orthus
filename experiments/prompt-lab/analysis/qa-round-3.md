# Track A round 4 — rewrite·synthesize·decompose baseline 측정 + 채택 2건 (2026-07-19)

방침: baseline을 먼저 재고 실약점이 보이는 곳만 개입 (앞 라운드 교훈).
골든셋: rewrite 120쌍(qa_golden 파생 히스토리+지시어 후속), synthesize 30쌍
(qa 질문 2개 결합, 1/3은 action 절 포함), decompose는 fugu-ko t7/t7_holdout 재사용.

## rewrite (후속 재작성) — ★채택 (strict-v1)

baseline 실측: judge-pass 80%, **지시어 잔존 17.5%** (21/120 — "그거"가 재작성에
그대로 남음). 변형 strict-v1 = 잔존 금지 명시 + 자기점검 한 문장.

| 지표 | baseline | strict-v1 | 검정 |
|---|---|---|---|
| 지시어 잔존 | 21/120 (17.5%) | **4/120 (3.3%)** | McNemar 18:1, **p=0.0001** |
| judge-pass | 75/120 | 77/120 | 7:9, p=0.80 (동등) |

n=40 1차에서 p=0.070으로 경계 → 신규 80쌍 확장 pooled로 확증. 채택 —
`orthus/router/rewrite.py::_SYSTEM` 말미 추가.

## synthesize (복합질문 합성) — ★채택 (syn-v2)

baseline 실측: 사실 보존 judge-pass 29/30은 건강. 그러나
**인용마커 25/30 (83%)** — 구 rule 3 "source markers must survive" 문구가
하위 답변의 죽은 마커를 보존하라고 명시 (cite-v2 채택 방향과 정면 충돌).
**action 무시 위반 7/10 (70%)** — "메일 보내드리겠습니다"류 누설.

syn-v2 = rule 3에 마커 drop 명시 + rule 5를 FINAL RULE로 승격+예시+자기점검.

| 지표 | baseline | syn-v2 | 검정 |
|---|---|---|---|
| 인용마커 | 25/30 | **5/30** | McNemar 20:0, **p<1e-5** |
| action 누설 | 7/10 | 6/10 | 무효 — 프롬프트 저항성 |
| judge-pass | 29/30 | 28/30 | 동등 |

마커 축 채택(`orthus/router/decompose.py::_SYNTHESIZE_SYSTEM` 교체). action 누설은
FINAL RULE 승격으로도 안 잡힘 — distill 오염과 같은 "프롬프트 저항성" 패턴.
구조적 후속 과제(합성 후 결정론 action-문구 검출→재생성, 또는 오케스트레이터
레벨 처리)로 기록.

## decompose (분해 게이트) — 코드/프롬프트 변경 없음, 설정 권고 재확인

t7 15/22 (68%) · t7_holdout 11/16 (69%) — 실패 전부 복합문 미탐(FN)이고 "랑/와/
각각" 연결형. `--ext-tier 3`으로 재실행 시 19/22 · 13/16 회복 —
`docs/decompose-prefilter-ext.md`의 기존 결론(T3 채택 시 recall 100%) 재확인.
**프롬프트 문제가 아니라 prod `ORTHUS_DECOMPOSE_PREFILTER_EXT_TIER=3` 활성화
결정 사안** (operator/owner 게이트).

## 판정자 하네스 버그 기록 (TS)

1차 채점에서 rw/syn judge-pass 0/40·0/30 전멸 — 원인은 모델이 아니라 판정
프롬프트에 "JSON" 단어가 없어 OpenAI json_object 모드가 요청 자체를 거부, 예외를
fail로 삼킨 하네스 버그. "0%든 100%든 극단값은 먼저 측정기를 의심하라"(임베딩
실험 '죽은 시험지' 교훈의 재현).
