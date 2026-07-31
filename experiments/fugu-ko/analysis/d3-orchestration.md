# D3 + 2단(b) 결과 — T2 judge & 규칙 오케스트레이션 (H2)

## 1. T2 쌍대비교 judge (D3)
judge = **gpt-4o(풀버전)**, 익명 A/B, **양방향 일치만 승패**(위치편향 방어), 30문항.
기준선 = gpt-4o-mini(동일 OpenAI 계열 → 국내에 보수적 방향).

| 워커 | 승 | 패 | 무 | 승률(승/(승+패)) | 수용(≥40%) |
|---|---|---|---|---|---|
| **Solar** | 19 | 3 | 8 | **86%** | PASS |
| **EXAONE** | 14 | 4 | 12 | **78%** | PASS |
| A.X | 5 | 8 | 17 | 38% | FAIL |

- **높은 무(tie)** = 위치 스왑 시 판정이 뒤집힌 신뢰불가 케이스를 정직히 tie 처리(ax 17, exaone 12). 편향 방어 작동 증거.
- **A.X는 QA에서 baseline에 짐(38%)** — 답변이 지나치게 간결(평균 59자 vs solar 230자). 라우팅 최강과 대비.
- judge 신뢰도 스팟체크(`t2-15`, 전 워커 baseline 패): baseline 완결·정확, ax 과소답변, **EXAONE 출력 끝 깨짐("에이전 '__'")**, solar 장황/잘림 → judge 판정 타당(아티팩트 아님). **기록: EXAONE 간헐적 출력 깨짐.**

## 2. 1단 종합 (4축) — 단일 모델 선정
| 태스크 | solar | ax | exaone | baseline(현행) |
|---|---|---|---|---|
| T3 정답률 | **94%** | 78% | **94%** | 78% |
| T5 라우팅 | 18/21(86%) | **20/21(95%)** | 19/21(90%) | **20/21(95%)** |
| T2 승률(vs base) | **86%** | 38% | 78% | — |
| T3 지연 p50 | 828ms | 1759ms | **715ms** | 1462ms |

**단일 최적 = Solar Pro**(QA·SQL 최강, 라우팅만 약함). 단 **전 태스크 1등 모델은 없음**:
Solar/EXAONE=QA·SQL 강 / A.X=라우팅 강. → 상보성(H1)이 뚜렷.

## 3. 2단(b) 규칙 오케스트레이션 — H2 판정
`selectors/static_map.py`: structured→solar, routing→ax, wiki_qa→solar (D2/D3 프로파일 근거).
orthus는 태스크를 이미 안다(엔드포인트/`classify`) → 태스크-라벨 라우팅은 **실현 가능**.

수용 기준(§4.4, base=gpt-4o-mini): T3 ≥73%(base−5), T5 ≥95%(무손실), T2 승률 ≥40%.

| 구성 | T3 | T5 | T2 | 수용 전체 |
|---|---|---|---|---|
| solar 단일 | 94% | 86% | 86% | ✗ (T5) |
| ax 단일 | 78% | 95% | 38% | ✗ (T2) |
| exaone 단일 | 94% | 90% | 78% | ✗ (T5) |
| baseline(현행) | 78% | 95% | — | — |
| **오케스트레이션** | **94%** | **95%** | **86%** | **✓ 전부** |

**H2 지지: 어떤 단일 국내 모델도 전 수용기준 미달, 오케스트레이션만 전부 통과.**
- 이득 위치: 라우팅을 A.X(95%)로, QA·SQL을 Solar(94%/86%)로 배정 → 각 축의 최강을 취합.
- 현행 baseline 대비: T3 +16%p(94 vs 78), T5 동률(95), T2 우세(86% 승률). **오케스트레이션은 현행 운영을 전 축에서 ≥ 능가.**

## 4. 한계·정직성 (보고서 명기)
- 본 오케스트레이션은 **태스크-라벨 정적 라우팅(b)**. "태스크별 최강 취합"이라 상한(upper-bound) 성격 — 단, orthus가 태스크를 실제로 판정하므로 realizable.
- **T2 judge self-preference**: 기준선이 gpt-4o-mini(judge와 동일 OpenAI 계열)라 국내에 보수적. 그럼에도 Solar/EXAONE 승률 78~86% → 결론 방향 견고.
- **within-task 동적 선택(질문 내용 기반)과 학습 선택기(c)** 는 2단 후속(A100 SFT) — H3 대상. static(b)는 상한을, learned(c)는 realizable 하한을 잰다.
- ground-truth 채점 18문항(count/groupby). 필터·정렬·메타형은 게이트통과만.

## 5. 재현
```
python experiments/fugu-ko/judge/pairwise.py   # T2 쌍대(gpt-4o, 180콜)
python experiments/fugu-ko/orchestrate.py      # H2 오케 vs 단일 (원자료 재사용, 무호출)
```
