# prompt-lab — 프롬프트·모델 측정 실험 (2026-07-19~21)

orthus의 **컨텍스트 전달 경로**는 계속 고쳐왔지만 **프롬프트 문구 자체를 측정으로
다듬은 적은 없었다**. 이 랩은 그 공백을 메운다 — 7개 프롬프트 표면에 A/B 하네스를
세우고, 축을 뒤집어 국내 3모델을 같은 표면에 돌리고, 위키 저작 모델까지 비교했다.

> 이 레포(orthus-ai-competition)는 **실험 분기**다. 운영 레포(orthus-ai)는 프로덕션
> 시스템이라 실험적 변경을 넣기 어려워 여기에 기록·반영한다.

## 보고서 (읽기용)

| 보고서 | 파일 |
|---|---|
| 종합 — 프롬프트 실험 전체 | [`html-reports/prompt-experiments-report.html`](html-reports/prompt-experiments-report.html) |
| 모델×태스크 매트릭스 + 미니위키 | [`html-reports/model-matrix-report.html`](html-reports/model-matrix-report.html) |

## 결과 요약

### 1. 프롬프트 A/B — 채택 3 · 기각 5

모델 고정(Solar, prod 배정) + 프롬프트 변형. 채택 판정은 전부 **결정론 지표**.

| 표면 | 변형 | 결과 |
|---|---|---|
| wiki_qa | **cite-v2** (마커 금지 재강조 + 실체 보존) | ✅ 채택 — 인용마커 위반 57%→30% (p=3.2e-8) |
| rewrite | **strict-v1** (지시어 잔존 금지 명시) | ✅ 채택 — 잔존 17.5%→3.3% (p=1e-4) |
| synthesize | **syn-v2** ("마커 보존"→"마커 drop" 교정) | ✅ 채택 — 마커 83%→17% (p<1e-5) |
| distill | kr-v1 한국어화 / ext-v1 외부지식 금지 / quote-v1 verbatim | ❌ 3건 모두 기각 |
| wiki_qa | kr-v1 한국어화 | ❌ 기각 — baseline이 **유의하게 우세**(p=0.0013) |
| decompose | — | 설정 권고: `ORTHUS_DECOMPOSE_PREFILTER_EXT_TIER=3` |
| 게이트웨이 시드 | — | 현행 유지 — 9시나리오 계약 위반 0 (38/38) |

**횡단 발견**
- "한국어 모델 = 한국어 지시"는 **두 표면 모두에서 기각**. 순한글 코퍼스(한글 0.91)로
  3박자 조건을 따로 검증해도 동일 — 패인은 언어 정합이 아니라 **행동 프라이어 변화**
  (한국어 지시문이 답을 짧고 소극적으로 만듦).
- distill 오염(5~7%)·synthesize action 누설(70%)은 문구·구조 개입 어디에도 안 움직인다
  → **프롬프트 저항성** 계열. 파이프라인(검증 패스/결정론 검출)의 일.

### 2. 모델×태스크 매트릭스 — 국내 3모델

프롬프트 고정 + 모델 변형. 신규 홀드아웃 + McNemar + **Holm 보정**.

| 태스크 | Solar | A.X | EXAONE |
|---|---|---|---|
| rewrite 지시어 잔존 | **1.7%** | 10.3% | 34.5% ★ |
| decompose 게이트 오답 | **15.8%** | 52.6% ★ | 26.3% |
| delegation 함정 오탐 | 13.0% | 25.9% | **11.1%** |
| wiki_qa 답변 Copeland | −32 | +11 | **+21** |
| 지연 p50 / p95 | 1140/1823 | 1553/**4777** | **945**/3363 |

★ = Holm 생존. **결론**: holistic 품질엔 강건한 승자가 없다(기존 `model-orchestration.md`
결론 재확인). 새 신호는 **객관 결함율**에 있고, 방향은 "누가 잘한다"보다
**"누가 이 태스크에서 약하니 그 배정만 피하라"** — rewrite에 EXAONE 회피,
decompose·delegation에 A.X 회피.

### 3. 미니위키 — 저작 모델이 downstream을 바꾸나

공통 70문서를 3모델 distill로 각각 저작(임베딩·답변 모델은 Solar 고정).

- **밀도는 갈린다**: EXAONE 6.7 claims/doc > Solar 5.7 > A.X 4.2. A.X는 **12문서 저작
  실패** + Solar의 10배 느림(35분).
- **downstream은 안 갈린다**: 어떤 쌍도 유의하지 않다(최소 p=0.066).
- **★측정 결함을 잡은 과정**: 1차 판정에선 A.X-저작 위키가 1위(+24)였으나, 판정자가
  원본을 못 본 데다 A.X가 저작 실패한 문서에도 거부 없이 답을 지어냈다. **원본을 정답
  근거로 동봉해 재판정하자 A.X +24→+8, EXAONE +4→+19로 역전** — 1위는 아티팩트였다.

## 방법론 — 이 결론들을 믿어도 되는 이유

- **prod 배정 모델로 측정** — 프롬프트 튜닝은 모델-특이적이라 배정 모델(Solar) 아닌
  측정은 무효.
- **frozen hits** — 문항당 retrieval 1회 고정 후 모든 변형이 같은 근거 위에서 경쟁
  (검색 변동과 프롬프트 효과 분리).
- **골든셋 역방향 생성 + 제목 누출 검사**, 생성기 다중화(편향 제거).
- **판정자에 근거 동봉** — 근거 없는 판정자는 자신감 있는 환각에 가점한다(실제로
  미니위키에서 이 함정을 밟았다가 잡았다).
- **사전 등록 + 홀드아웃 + Holm 보정** — 경계 결과는 신규 표본으로 확증하고, 통과
  못 하면 채택하지 않는다("해로울 것 없으니 넣자" 금지).

### 반복된 교훈: "지표가 표본보다 먼저다"

임베딩 실험에서 나온 이 교훈이 이 세션에서 **세 번** 재현됐다.
① 판정 프롬프트에 "JSON"이 없어 판정이 전멸했는데 모델 탓으로 볼 뻔함
② 게이트웨이 "실패" 4건이 전부 루브릭 오탐(금지를 올바르게 재진술한 부정문)
③ 미니위키 A.X 1위가 근거 미동봉 판정의 아티팩트
**검정력 계산은 지표가 옳은지를 검증해주지 않는다.**

## 하네스

| 파일 | 역할 |
|---|---|
| `distill_harness.py` / `score_distill.py` | 위키 저작(distill) A/B + 판정자 채점(오염·커버리지·메타claim) |
| `qa_lab.py` | wiki_qa 골든 생성 · frozen hits · 답변 · 쌍대 판정 |
| `misc_lab.py` | rewrite · synthesize · decompose 표면 |
| `gateway_lab.py` | P10 게이트웨이 시드 리플레이(codex exec, 결정론 루브릭 9시나리오) |
| `korean_lab.py` | 순한글 3박자 조건 검증 |
| `matrix_lab.py` | 모델×태스크 매트릭스 + Holm 보정 리포트 |
| `mini_wiki.py` | 저작 모델별 미니위키 + 원본-정답 재판정 |
| `codex_judge.py` | codex(ChatGPT OAuth) 판정자 어댑터 — API 빌링과 별개, 중립 |
| `variants_*.py` | 프롬프트 변형 레지스트리(라운드별 가설) |

`run_matrix.sh` / `run_miniwiki.sh` / `resume_matrix.sh` — 전 파이프라인 드라이버(전부 resume 가능).

## 실행

레포 루트에서 환경을 로드한다 (PYTHONPATH·키·임베딩 슬롯·DB·판정자 한 번에):

```bash
source experiments/prompt-lab/env.sh     # zsh/bash 공용
uv run python experiments/prompt-lab/matrix_lab.py report --judge codex
uv run python experiments/prompt-lab/distill_harness.py --variant baseline --model solar
```

**로컬 준비물** (전부 레포 밖 — 커밋되지 않는다):

| 항목 | 위치 | 비고 |
|---|---|---|
| 국내 모델 키 3종 | `~/.orthus/fugu-keys.json` (0600) | `FUGU_KEYS`로 오버라이드 가능. 형식: `[{"provider":"upstage"\|"a.x"\|"exaone","key":"...","model":"..."}]` |
| 실험 원자료 | `experiments/prompt-lab/{data,analysis/raw}/` | gitignore. 없으면 하네스가 처음부터 재생성(수 시간+API 비용) |
| 로컬 DB | docker `orthus_pg` — `orthus`(회사 위키 재구축본) / `orthus_miniwiki`(저작 실험) | `ORTHUS_PG_DSN`로 전환 |
| 판정자 | `codex` CLI (ChatGPT OAuth) | `codex login` 필요. 국내 판정 대체는 `--judge solar\|ax\|exaone` |

**커밋 정책**: 스크립트·문서·보고서만 추적한다. 회사 문서 원문·생성 질문·답변·claim
원자료(`data/`, `analysis/raw/`)와 API 키는 `.gitignore`로 제외한다(fugu-ko 관례 동일).

## 프로덕션 반영

채택 3건은 이 레포의 코드에 반영돼 있다(운영 레포와 별개):

| 파일 | 변경 |
|---|---|
| `orthus/wiki/qa.py` | `_SYSTEM` 말미에 cite-v2 블록 |
| `orthus/router/rewrite.py` | `_SYSTEM`에 strict-v1 지시어 금지 문장 |
| `orthus/router/decompose.py` | `_SYNTHESIZE_SYSTEM` → syn-v2 |

각 변경에 근거 주석(측정치 + `analysis/*.md` 포인터)이 달려 있다.

## 상세 기록

`analysis/` — 라운드별 가설→측정→판정 전 과정:
`round-1..3.md`(distill) · `qa-round-1..3.md`(질의측) · `gw-round-1.md`(게이트웨이) ·
`korean-experiment.md`(순한글) · `matrix-{results,interpretation}.md`(모델 매트릭스) ·
`miniwiki-{results,interpretation}.md`(저작 모델).
