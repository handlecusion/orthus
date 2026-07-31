# Track C round 3 — H-C5 evidence verbatim 강제 (quote-v1) (2026-07-19)

## 설정

- A = `baseline` · B = `quote-v1`(baseline + "evidence는 원문에서 글자 그대로 복사,
  인용 못 찾으면 그 claim은 내지 마라") — 표본: round-1 80 docs
- 기대: 오염의 구조적 차단 + "비-verbatim evidence claim drop" 결정론 게이트 후보

## 결과

**구조 지시는 먹혔다**: verbatim evidence 20.7% → **82.3%**, claim 수 무손실
(5.36→5.65/doc, zero-claim 문서 0).

**그러나 오염은 안 줄었다**:

| 지표 | baseline | quote-v1 | 검정 |
|---|---|---|---|
| 오염률 | 5.13% | 4.87% | per-doc sign p=0.84, McNemar p=1.00 |
| 메타-claim률 | 4.43% | 3.10% | — |
| 커버리지 | 69.4% | 69.9% | — |

**게이트 교차표가 사인을 냈다** (quote-v1에서 gate=비verbatim claim drop):

- recall 23% — 오염 claim 22개 중 17개는 **verbatim evidence를 갖고 있다**
- precision 6% — 게이트에 걸린 80개 중 75개는 정상 claim
- 정상 claim 손실 17.4%, 잔존 오염률 4.87%→4.57% (사실상 무개선)

## 판정

**기각 — 프롬프트·게이트 둘 다.** 핵심 발견: **오염은 evidence 출처가 아니라
claim–evidence 관계에 산다.** Solar는 진짜 원문을 evidence로 인용하면서 claim
문장에는 외부지식/추정을 섞는다. evidence 충실도를 아무리 올려도 claim 충실도는
안 따라온다 — 결정론 verbatim 게이트가 원리적으로 오염을 못 잡는 이유.

## Track C 종합 결론 (3라운드)

| 라운드 | 개입 | 결과 |
|---|---|---|
| 1 | kr-v1 지시문 한국어화 | 기각 (전 지표 비유의) |
| 2 | ext-v1 외부지식 금지 문구 | 기각 (홀드아웃 사전등록 p=0.36) |
| 3 | quote-v1 verbatim 구조 강제 | 기각 (오염 불변, 게이트 recall 23%) |

**현행 distill 프롬프트 유지가 측정 기반 결론이다.** 문구·구조 개입 3종이 모두
유의차를 못 냈다 — 현행 프롬프트는 강건하고, 오염 5~7%(이 판정자 기준)는
프롬프트 수준에서 제거되지 않는 지속 성질이다. 오염을 실제로 줄이려면 distill
후 **검증 패스**(claim⊆문서를 2차 LLM/판정자가 확인해 drop — 파이프라인 변경,
프롬프트 범위 밖)가 필요하다는 것이 3라운드의 구조적 시사점. 후속 과제로 기록만
하고 Track C를 닫는다.

Phase 2(로컬 Solar 재구축)는 **현행 프로덕션 프롬프트로** 진행한다.
