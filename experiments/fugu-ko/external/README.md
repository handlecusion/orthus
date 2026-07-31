# X-tier 외부 데이터셋 (B4 external validity)

> 사전선언 SoR = `analysis/b4-prereg.md` · 선정 근거/라이선스/함정 SoR = `analysis/x0-external-dataset-plan.md`
> 이 디렉터리는 **X1(판정자-사람 일치)** 을 지금 돌릴 수 있게 하고, 나머지 X2–X4를 스테이징한다.

## 왜 있는가

골든 자산 34개가 전부 자체 제작이고 저장소에 외부 공개 데이터셋 사용 이력이 0이다.
`e2e/STATE.md` GATE caveat (3)이 **"24 anchors are unverified by a human"** 을 자인하는데,
t2·t8·email_draft 승률이 전부 그 판정자에 의존한다. X1은 그 판정자가 사람과 얼마나 맞는지를
**신규 라벨링 0건**으로 잰다.

## 파일

| 파일 | 역할 |
|---|---|
| `download.py` | X-tier 4종을 gitignored 캐시로 받고 `MANIFEST.sha256` 작성. **스키마가 사전선언 가정과 다르면 크게 실패**한다 |
| `b4_judge_kappa.py` | X1 측정. 유도쌍 주판정([5], 군집 부트스트랩) · 포인트/쌍대 천장 · judge-human κ([2] 보조) · flip rate · 영어 앵커 · factuality 축 |
| `fixtures/` | 오프라인 dry 모드용 **합성** 데이터 + 심어둔 결정론 판정자 응답. 실제 데이터셋 행이 **아니다** |
| `MANIFEST.sha256` | 고정 revision의 raw 파일 + 정규화 JSONL의 sha256. **데이터 대신 이것만 커밋한다** |
| `.cache/` | 데이터 캐시. gitignored — 절대 커밋 금지 |

## 데이터셋

| key | HF id | revision | license | 재배포 | 오염 위험 | prereg subset |
|---|---|---|---|---|---|---|
| `kudge_human` | `HAERAE-HUB/KUDGE` (`Human Annotations`) | `7168dc84…` | **미표기(unstated)** | **금지** | 낮음 — 사람 주석 자체가 라벨이라 모델 사전학습이 천장을 올리지 않는다 | X1 (human-human κ **천장**) |
| `khj` | `HAERAE-HUB/Korean-Human-Judgements` | `24dae25b…` | **미표기(unstated)**, 카드에 "평가 전용" 명시 | **금지** | 중간 — 응답이 공개 아레나 출력이라 판정자가 본 적 있을 수 있으나, **사람 선호 라벨**은 재현 대상이 아니다 | X1 (judge-human κ + flip rate) |
| `mtbench_human` | `lmsys/mt_bench_human_judgments` (split `human`) | `f7d2896d…` | **CC-BY-4.0** | 허용(출처표기) | 높음 — MT-Bench는 널리 학습·인용됨. **그래서 앵커로만 쓴다**(절대 수치 주장 없음) | X1 (영어 앵커) |
| `summeval` | `mteb/summeval` | `bfc12115…` | **MIT** | 허용 | 높음 — MTEB 수록셋. factuality 축 **대리지표**로만 | X1 (factuality 축) |

전부 2026-07-21 기준 **public·non-gated**로 실제 확인했다(`gated: False`).

### 아직 스테이징 안 된 것 (X2–X4)

`b4-prereg.md` §3이 잠근 나머지다. 라이선스가 깨끗해 재배포 제약은 덜하지만, 이번 슬라이스에는
다운로더를 넣지 않았다 — X1이 최우선이고(§3 "최우선"), 나머지는 파이프라인 통합 방식이 다르다.

| subset | 자산 | license |
|---|---|---|
| X2 | `allganize/RAG-Evaluation-Dataset-KO` (300) | MIT |
| X3 | KorWikiTQ (LG-NLP) → `notion_rows` 적재 | CC-BY-SA-4.0 |
| X4 | `mteb/AutoRAGRetrieval` · `miracl/miracl` ko · `castorini/mr-tydi` korean | MIT / Apache-2.0 |

## 라이선스·재배포 (하드 제약 — `b4-prereg.md` §6)

- KUDGE · Korean-Human-Judgements는 **라이선스 미표기** → **평가·인용만, 재배포 금지**.
- 저장소에는 **다운로드 스크립트 + sha256만** 커밋한다. `.cache/`는 gitignored다.
- CC-BY-SA 파생물을 **공개 배포**하면 동일조건 전파가 붙는다 → owner 결정 전까지 사내 평가 한정.
- AI-Hub 자산은 승인 전 사용 금지(내국인 전용·해외반출 불가). 여기에는 하나도 없다.

## 쓰는 법

```bash
# 오프라인 검증 (API 키·네트워크 불필요). 합성 fixture + 결정론 스텁 판정자.
python experiments/fugu-ko/external/b4_judge_kappa.py --dry

# 실측
python experiments/fugu-ko/external/download.py          # 받기 + 스키마 검증 + 매니페스트
python experiments/fugu-ko/external/download.py --verify  # 해시 대조만
ORTHUS_LLM_BASE_URL=... ORTHUS_LLM_API_KEY=... \
  python experiments/fugu-ko/external/b4_judge_kappa.py   # judge=gpt-4o, 약 2,190콜
```

의존성은 **stdlib 0개 추가**다. parquet은 `pyarrow`가 있으면 직접 읽고, 없으면 HF
datasets-server rows API로 같은 revision 내용을 받는다(매니페스트에 어느 경로였는지 기록).
rows API는 페이지네이션이 길면 429가 나므로, 반복 실행할 거면 `pyarrow`를 넣는 편이 낫다.

## 설계상 지킨 것

- **판정자를 새로 쓰지 않았다.** `judge/pairwise.py`의 `_SYS` / `_prompt` / `judge_once`와
  양방향 접기 규칙(두 방향 일치할 때만 승패, 흔들리면 tie)을 그대로 import한다. 새 프롬프트를
  쓰면 t2·t8 승률을 낳은 **그 판정자**의 신뢰도가 아니게 된다. 프롬프트가 나중에 바뀌면 알 수
  있도록 `judge/pairwise.py`의 sha256을 결과 JSON에 함께 기록한다.
- **대체 금지.** 게이팅·부재·스키마 불일치는 `SchemaMismatch`로 크게 실패하고 보고한다.
  다른 데이터셋으로 갈아끼우면 사전선언이 잠근 것이 바뀐다(§5).
- **주판정은 결과 보기 전에 고정.** 유도쌍 same-label-space 비율,
  `judge_induced_κ / human_human_induced_κ ≥ 0.80`(95% CI 하한, instruction 군집 부트스트랩,
  10,000 resamples, seed 1234) — 아래 "천장" 절 참조.

## 천장 — 사전선언 3차 개정 (2026-07-21 ~ 07-22)

### (1) `healthy?` 자유도 — **해소, `card` 기본**

KUDGE CSV에는 사전선언이 언급하지 않은 주석 오류 플래그 `healthy?`(e0/e1/e2)가 251건 있다.
coordinator 결정으로 **제외(`card`)가 기본**이다. 사후 확인으로 **HAERAE 자신도 같은 필터를
적용했음이 실측 확인**됐다 — `KUDGE` `Pointwise` config(2,506행)가 card 필터 결과와 행수
정확히 일치하고, 겹치는 행의 `score1`/`score2` 불일치가 **0건**이다(8행은 텍스트 정규화 차이).

| 변형 | n | κ(비가중) | κ(2차 가중) | 완전일치 | ±1 이내 |
|---|---|---|---|---|---|
| `prereg` — 센티널 `score2=-1`(123건)만 제외 | 2,757 | 0.2529 | 0.5078 | 44.5% | 78.7% |
| **`card`** — 위 + `healthy?` 251건 추가 제외 **(기본)** | 2,506 | 0.2834 | 0.6400 | 47.9% | **83.84%** |

카드가 광고하는 "83.85% 동일 또는 ±1"은 `card`에서만 정확히 재현된다.

### (2) 비율의 분모 — **결함 B 해소, 유도쌍으로 같은 라벨 공간에서 잰다**

원래 사전선언 §3의 비율은 분자(KHJ 명목 A/B/tie 쌍대)와 분모(KUDGE 순서형 1-5 포인트와이즈)가
**서로 다른 측정**이라 비율이 의미 없었다(coordinator가 잡은 결함 B). 위 [1]의 포인트와이즈 κ는
그래서 **비율의 분모로 쓰지 않는다** — 기술통계·카드 대조용으로만 남긴다.

개정판(2026-07-22)은 분자·분모를 **같은 명목 라벨 공간 + 같은 표본**에서 잰다. 아래 유도쌍이
그 장치다.

### 주판정 장치 — 유도쌍 (구성 가능, 풍부함)

KUDGE Human Annotations는 **instruction 90개 × 모델 32개 완전 격자**(2,880행, 결측 0)다.
같은 instruction 안의 응답 두 개를 집으면 각 주석자의 점수차가 곧 선호가 되므로, **판정자와
같은 명목 라벨 공간의 천장**을 만들 수 있다. `card` 필터 기준 instruction 87개 · 응답 2,505개
→ **유도쌍 35,162개**([1b] 전수 천장).

**주판정([5])의 구성** — `card` · tie band 0 · instruction당 8쌍 균등 표본(seed 1234):
- **분모(천장)** = 표본 유도쌍 **전체**의 human-human κ (annotator1 유도선호 vs annotator2).
  κ가 성립하려면 불일치쌍이 있어야 하므로 전체를 쓴다.
- **분자** = 같은 표본의 **합의 부분집합**(두 주석자가 같은 방향 → gold 정의됨)에서
  우리 판정자(위치 스왑, 양방향 접기) vs 사람 gold κ.
- **비율** = 분자 / 분모, **95% CI 하한 ≥ 0.80**이면 PASS. CI는 아래 군집 부트스트랩.

분모는 표본 전체, 분자는 그 합의 부분집합이라 항목 수가 다르다(분자가 비합의쌍만큼 적다).
[5] 출력은 표본 쌍 수·군집 instruction 수·합의 탈락 수를 먼저 찍어 검정력을 노출한다.
실데이터 검정력(판정자 호출 없이 사전 확인): instruction 87 · 표본 696쌍 · 합의 420(분자) ·
비합의 276 탈락 · 표본 천장 κ ≈ 0.40 · 판정자 호출 840회.

| tie 규칙 | κ | raw agree | 합의쌍(gold 가용) | 주석자1 라벨분포 |
|---|---|---|---|---|
| `|d| == 0` → tie **(기본)** | **0.4440** | 62.9% | 22,131 | A 11,879 / B 11,818 / tie 11,465 |
| `|d| <= 1` → tie | 0.3692 | 69.4% | 24,410 | A 5,383 / B 5,492 / tie 24,287 |
| `|d| <= 2` → tie | 0.3223 | 85.4% | 30,041 | A 2,017 / B 2,007 / tie 31,138 |

tie 폭을 넓히면 raw agreement는 오르지만 κ는 **내려간다** — tie가 다수 클래스가 되면서 우연
일치 기대값이 커지기 때문이다. 밴드 0이 라벨 분포가 가장 균형 잡혀 있고 κ도 가장 높다.

`--tie-band {0,1,2}`로 바꿀 수 있고 세 값이 항상 함께 출력된다.

### ⚠️ 함정 — KUDGE `Pairwise` config(818행)를 천장으로 쓰면 안 된다

`Pairwise`는 `instruction`을 Human Annotations와 87개 전부 공유하고 join도 된다(818행 중
779행이 양쪽 주석자까지 복원됨). **하지만 평균점수 격차 ≥1.0으로 선별된 집합**이다
(격차 분포 1.0~4.0, 최빈 2.5). 그래서 그 위에서는 두 주석자가 거의 항상 같은 방향을 가리키고
(주석자1 라벨이 779건 중 A 738 / B 1 / tie 40), κ가 **-0.06**으로 무너진다 — raw agreement는
87.4%인데 라벨 분포가 degenerate라 우연 보정이 붕괴한다.

즉 `Pairwise`는 **판정자 정확도(raw accuracy)** 용으로는 쓸 수 있어도 **κ의 분자로도 분모로도
쓸 수 없고**, 난이도가 인위적으로 쉬워 판정자를 좋아 보이게 만든다. `sample_induced_pairs()`는
그래서 **점수 격차로 선별하지 않고** instruction당 균등 무작위 표본만 한다(합의 요건은 분자
단계에서만 적용).

### ⚠️ 유도쌍은 독립이 아니다 → 군집 부트스트랩

응답 하나가 ~30개 쌍에 재등장하고 쌍은 instruction으로 군집화된다. 쌍을 독립 표본으로 보고
부트스트랩하면 CI가 실제보다 훨씬 좁게 나온다. 그래서 [5]의 CI는 **instruction을 복원추출**하는
`cluster_bootstrap_ratio()`로 잡는다(쌍 재표집 아님) — 유효 표본은 쌍 수가 아니라 **instruction
수**(실데이터 87개)에 가깝다. **구 쌍 단위 `bootstrap_ratio_lower`는 이 footgun을 막으려고
아예 제거**했다.

### 판정자 실행 규모 (참고, `--per-instruction`)

전체 합의쌍(실데이터 22,131개)을 다 돌리면 비현실적이라 instruction당 균등 표본한다(seed 1234).
`--per-instruction` 기본 8. 합의 부분집합만 판정자에 물리므로 호출 수는 아래 합의쌍의 2배:

| per_instruction | 표본 쌍 | 합의(분자 대상) | 판정자 호출 |
|---|---|---|---|
| 4 | 348 | ~210 | ~420 |
| **8 (기본)** | 696 | 420 | **840** |
| 20 | 1,740 | ~1,050 | ~2,100 |

## 이 실험이 주장할 수 없는 것

1. X1은 **판정자를 검증하지 우리 답변을 검증하지 않는다**.
2. 외부 절대 수치를 우리 제품 성능으로 이전하지 않는다. Tier A/B와 **합산 금지**(`tier: X` 분리).
3. summeval 축은 **대리지표**다 — 우리 프롬프트는 종합 선호를 묻지 factuality만 묻지 않는다.
4. 영어 앵커는 한국어 κ의 해석을 돕는 용도지, 앵커 수치 자체가 결론이 아니다.
5. 리더보드 등재 주장 금지 — 공개 제출 규약을 따르지 않았다.
