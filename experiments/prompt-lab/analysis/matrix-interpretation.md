# 모델×태스크 매트릭스 — 해석 (2026-07-20)

## 설정

- 국내 3모델 **Solar·A.X·EXAONE** × 7표면. 프롬프트 고정(현행 prod), 모델만 변형.
- 신규 홀드아웃: distill 48문서(기존 201 제외)·wiki_qa 60문항(미사용 페이지)·
  rewrite 58·synthesize 29·decompose 38(t7+t7_holdout 라벨)·delegation 54
  (t10+t10_holdout2 라벨).
- 채점: 하이브리드 — rewrite/synthesize/decompose/delegation/신뢰성은 결정론,
  distill 오염·wiki_qa 품질은 **codex(ChatGPT OAuth) 중립 판정자**(API 빌링 소진
  대체 + 국내 self-preference 제거). 질문 생성은 3모델 로테이션(생성기 편향 제거).
- 유의성: paired McNemar + **Holm 다중비교 보정**(19개 비교, α=0.05).

## 핵심 결론 — 기존 "유의차 없음"과 다르다

`model-orchestration.md`는 "국내 3모델 간 어떤 태스크에서도 쌍대 차이 비유의"였다.
그건 **판정자 기반 holistic 품질** 측정이었다. 이번에 **객관 결함율 지표**로 특정
실패모드를 직접 재니 **Holm 보정 후에도 살아남는 태스크별 차이 4건**이 나왔다:

| 살아남은 유의차 (Holm) | 승자 | p |
|---|---|---|
| rewrite 지시어잔존 — Solar vs EXAONE | **Solar** | <0.0001 |
| decompose 게이트오답 — Solar vs A.X | **Solar** | 0.0005 |
| decompose 게이트오답 — EXAONE vs A.X | **EXAONE** | 0.0020 |
| rewrite 지시어잔존 — A.X vs EXAONE | **A.X** | 0.0026 |

즉 **holistic 품질(wiki_qa 판정)에선 여전히 강건한 승자가 없지만(기존 결론 재확인),
객관 결함율에선 모델별 뚜렷한 약점 프로파일이 드러난다.**

## 모델별 태스크 프로파일

| 태스크 | 최고 | 최악 | 지표 (최고→최악) |
|---|---|---|---|
| **rewrite** (지시어 해소) | Solar | EXAONE | 잔존 1.7% → A.X 10.3% → EXAONE 34.5% ★ |
| **decompose** (분해 게이트) | Solar | A.X | 오답 15.8% → EXAONE 26.3% → A.X 52.6% ★ |
| **delegation** (함정 오탐) | EXAONE≈Solar | A.X | 오탐 11.1%/13.0% → A.X 25.9% (raw p=0.008, Holm 미생존) |
| **wiki_qa** (답변 품질) | EXAONE≈A.X | Solar | Copeland EXAONE +21 / A.X +11 / Solar −32 (raw p=0.008, Holm 미생존) |
| **synthesize** | 무차이 | — | n=29 소표본, 마커·action 모두 비유의 |
| **distill** (오염) | 무차이 | — | codex 판정 3모델 전부 0% (아래 caveat) |
| **지연(p50)** | EXAONE 945ms | A.X 1553ms | Solar 1140ms · A.X 꼬리 p95 4777ms(RPS 상한) |

**한 줄 요약**:
- **Solar** = 구조적 지시 따르기 최강(rewrite·decompose), 답변 품질 최약(간결 편향).
- **EXAONE** = 답변 품질·위임 안전·속도 우위, 지시어 해소 최약.
- **A.X** = 분해 게이트·위임 오탐·지연 꼬리 최약, 나머지 중위.

delegation에서 A.X가 함정에 가장 많이 발화(25.9%)한 건 `model-orchestration.md`의
기존 관측("A.X 오탐 4건, 발신자 자기 계획을 위임으로 오독")과 방향 일치 — Holm은
못 넘었지만 재현됐다.

## Caveat (정직하게)

1. **현행 prod 프롬프트로 측정** — 프롬프트는 prod 배정 모델에 맞춰졌을 수 있어,
   "그 프롬프트에서 어느 모델이 나은가"이지 "모델 절대 능력"이 아니다.
2. **distill 0% 오염**은 codex 판정자가 gpt-4o보다 fabrication 플래그에 관대하거나
   신규 48문서가 유독 깨끗해서일 수 있다(앞 gpt-4o 실측은 5~7%였다). 모델 차이는
   못 봤다는 결론만 유효.
3. **wiki_qa는 codex 판정** — codex가 충실·상세 답을 선호하면 간결한 Solar가 불리.
   prod가 Solar를 쓰는 건 지연·비용·벤더 때문이지 품질 최고라서가 아니다(이 결과와 정합).
4. synthesize n=29 소표본. distill·wiki_qa 판정은 codex 1종 — 국내 heterogeneous나
   gpt-4o(빌링 충전 시) 교차검증 여지.

## 실무 함의

"태스크별 분할 배정"의 근거가 **약점 회피 관점에선** 부분적으로 선다:
- rewrite는 EXAONE 배정 회피(지시어 잔존 34.5%).
- decompose는 A.X 배정 회피(오답 52.6%).
- delegation은 A.X 회피(오탐), EXAONE 선호 — 기존 `delegation_extract→EXAONE`
  배정과 정합.

단 holistic 품질 차이는 여전히 강건하지 않으므로, 이건 "특정 모델이 특정 태스크를
잘한다"보다 **"특정 모델은 특정 태스크에서 눈에 띄게 약하니 그 배정만 피하라"**가
더 정확한 결론이다.
