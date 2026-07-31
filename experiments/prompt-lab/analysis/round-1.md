# Track C round 1 — H-C1 지시문 한국어화 (2026-07-19)

## 설정

- 표본: prod company-scope 층화 80 docs (tiny 20 / short 40 / medium 15 / long 5)
- 모델: Solar(solar-pro), temperature 0 · 판정자: gpt-4o (원문 동봉, 핵심사실 문서당 1회 캐시)
- A = `baseline` (프로덕션 현행, 영어 지시문) · B = `kr-v1` (내용 1:1 동일, 지시문만 한국어)

## 결과 (paired 80 docs)

| 지표 | baseline | kr-v1 | paired 검정 |
|---|---|---|---|
| claims/doc | 5.36 | 5.25 | — |
| 오염률 (claim) | 5.13% | 5.48% | 문서단위 McNemar A-only 9 / B-only 8, **p=1.00** |
| 메타-claim률 (claim) | 4.43% | 0.95% | 문서단위 McNemar A-only 6 / B-only 2, **p=0.29** |
| 커버리지 | 69.4% | 67.1% | per-doc Δ mean −2.5pp, sign test **p=0.77** |

## 판정

**채택 없음 — 현행(영어 지시문) 유지.** 어떤 축도 유의하지 않다. 메타-claim은
방향상 kr-v1이 낫지만(claim률 4.4%→0.95%) 소수 문서에 집중된 차이라 p=0.29.
"Solar니까 한국어 지시가 낫다"는 가설은 이 표본에서 기각도 입증도 안 됨 —
효과가 있어도 작다.

## round 1의 진짜 발견 — 오염의 패턴

baseline 오염 claim을 열어보니 패턴이 일관된다: **문서에 없는 외부 지식/추정
보완**이다.

- "Grok-imagine은 Grok 플랫폼의 이미지 생성 기능을 확장한 모델입니다" ← 문서에 없는 배경지식
- "'ㅇㅋ 굿'은 한국어 인터넷 슬랭으로 …" ← 슬랭 해설(일반상식) 주입
- "v1.9.0 배포 공지는 관리자 및 출연자 대상으로 작성되었습니다" ← 대상 추정
- "배역 추가 오류 수정 사항은 2025년 4월 24일에 완료되었습니다" ← 날짜 단정

현행 프롬프트는 "with concrete evidence drawn from the document"라고만 하고
**외부 지식 금지를 명시하지 않는다**. → round 2 가설 H-C4: baseline에
외부지식/추정 금지 규칙 한 문장 추가(`ext-v1`), 오염률·오염 문서 수를 1차
지표로 paired 재측정.

## 원자료

- `raw/distill_{baseline,kr-v1}_solar.jsonl`, `raw/score_*_gpt-4o.jsonl`,
  `raw/keyfacts_gpt-4o.jsonl` (전부 gitignore)
