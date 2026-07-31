# E4 실험 계획서 — 이종 judge 패널(PoLL) 교차검증

> E-시리즈 우선순위 2/5 · 작성 2026-07-13 · 상태: 계획 (미실행)
> 실행 위치: `.worktrees/fugu-ko-experiment` (워커 재실행 0회 — judge 콜만 추가)

## 1. 목표 / 가설

보고서 8.1이 스스로 인정한 최대 방법론 약점: **judge self-preference** — 지식응답 judge가
gpt-4o인데 기준선이 gpt-4o-mini(동일 계열)라, 판정이 한 회사 모델의 취향에 묶여 있다.
현재 방어는 "보수적 방향이니 결론 견고"라는 논증뿐, 실측 교차검증이 없다.

Panel-of-LLM-judges(PoLL) 연구 흐름의 처방 — 단일 대형 judge보다 **이종(heterogeneous) 패널
다수결**이 개별 judge 편향에 강건 — 을 기존 응답 원자료에 그대로 적용한다.

- **H-E4a (견고성):** judge를 3-패널 다수결로 바꿔도 지식응답 승률 순위(Solar > EXAONE > A.X)와
  수용선 통과(승률 ≥ 40%)가 유지된다.
- **H-E4b (국내 judge 가능성):** 국내 모델(판정=분류형 작업)이 gpt-4o와 실질 일치하는 judge로
  기능한다 — A.X가 "단호하지만 좁은" 프로파일(분류 최강)대로 judge에서도 강한지가 관전 포인트.

부수 산출물: "국내 모델을 평가자로 쓸 수 있는가"는 그 자체로 대회 보고서의 독립 발견이 된다.

## 2. 기존 코드/데이터 접점 (실측 심볼)

| 접점 | 위치 | 용도 |
|---|---|---|
| 기존 judge | `judge/pairwise.py` — `_SYS`(익명화 한국어 심사 프롬프트), `judge_once()`, 양방향 판정 + 불일치→tie 규칙, `OpenAIChat(..., "gpt-4o")` | 패널화 확장 대상 (판정 프로토콜 재사용) |
| 워커 응답 원자료 | `analysis/raw/t2_{solar,ax,exaone,baseline}.jsonl` (`answer` 필드) | 재판정 입력 — **워커 재실행 불필요** |
| 기존 판정 | `analysis/raw/t2_judge.jsonl` (`v_fwd`/`v_rev`/`result`) | gpt-4o 단독 기준선 (재사용, 재판정 불필요) |
| 합성 확장분 | `analysis/raw/t8_{...}.jsonl` + `t8_judge.jsonl` | 옵션 arm (규모 확장 시) |
| 국내 judge 풀 | `pool.py::build_pool()` — 3종 모두 json_only 5/5 PASS(D0 S2) | 신규 judge 어댑터 불필요 |

## 3. 스코프

**In:** T2 지식응답 30문항(3워커쌍)의 패널 재판정, judge 간 일치도/편향 진단, 승률 재계산,
순위 안정성 판정. 옵션: t8 확장.

**Out:** 워커 응답 재생성(원자료 고정 — 순수 판정층 실험), 결정론 채점 태스크(T3/T5/T6/T7 —
judge 무관), judge 프롬프트 개선 실험(프로토콜은 기존 `_SYS` byte-identical 유지 — 변인 통제),
사람 라벨 대규모 수집(스팟체크만).

## 4. 방법

### 4.1 패널 구성 — "judge ∉ 판정 쌍" 규칙 (핵심 설계 결정)

자기 출력 판정(self-judging)은 self-preference를 완화가 아니라 **역방향으로 재도입**한다.
따라서 쌍별로 판정 참여 모델을 제외한 이종 패널을 구성:

| 판정 쌍 | 패널 (3 judge) |
|---|---|
| solar vs baseline | gpt-4o · **exaone** · **ax** |
| exaone vs baseline | gpt-4o · **solar** · **ax** |
| ax vs baseline | gpt-4o · **solar** · **exaone** |

- gpt-4o는 전 쌍 공통(기존 판정 재사용 — 신규 콜 0), 국내 judge 2종만 신규 판정.
- 판정 프로토콜은 기존과 동일: 익명화 + 양방향 2회 + 방향 불일치→tie (`judge_once` 재사용,
  judge 모델만 `build_pool()` 워커로 교체).

### 4.2 집계 (사전 고정)

- **judge-level verdict:** 각 judge의 양방향-일치 결과 (win/loss/tie).
- **panel verdict:** 3 judge 다수결. 3자 3색(win/loss/tie 각 1) → tie.
- **승률:** 기존 정의와 동일(tie 제외 승/[승+패]) — 패널 verdict 기준으로 재계산.
- **진단 지표:** ① judge 쌍별 일치율 + Cohen's kappa(3-way, tie 포함) ② judge별
  decisiveness(비-tie 비율 — A.X 짧은 출력 성향이 tie 남발로 나타나는지) ③ gpt-4o 단독 대비
  패널 verdict 플립 문항 목록(전수 스팟체크 대상).

## 5. 판정 기준 (사전 고정)

- **H-E4a 지지 = 결론 견고 판정:** 패널 승률에서도 (i) Solar·EXAONE ≥ 40%(수용선) 유지,
  (ii) 순위 Solar ≥ EXAONE > A.X 유지. → 보고서 8.1에 "패널 교차검증 통과" 1줄 추가.
- **부분 뒤집힘:** 순위는 유지되나 승률 절대치가 ±15%p 이상 이동 → 6.1/6.6 수치에 패널 값 병기.
- **뒤집힘:** 수용선/순위 붕괴 → 플립 문항 전수 스팟체크(사람) 후 8.1 전면 개정 —
  이 경우 기존 배정표(wiki_qa→solar)의 근거 재검토까지 명시.
- **H-E4b:** 국내 judge와 gpt-4o의 verdict 일치율 ≥ 70% & kappa ≥ 0.4(중등 일치)면
  "국내 judge 실용 가능" 판정.
- 무효 조건: 국내 judge json 파싱 실패율 > 10%(그 judge 제외하고 2-패널로 강등, 기록).

## 6. 대안 비교

| 옵션 | 장점 | 단점 | 판단 |
|---|---|---|---|
| A. 이종 3-패널, judge∉쌍 (본안) | self-preference 구조적 차단, 콜 최소 | 국내 judge 품질 미검증(그게 측정 대상) | 채택 |
| B. 강한 제3사 judge 1종 교체(Claude/Gemini) | 단순 | 신규 벤더 키/비용, "단일 judge 취향" 문제 재생산 | 기각 |
| C. 사람 전수 라벨 | 골드 스탠다드 | 30문항×3쌍 전수는 마감 내 비현실, 스팟체크로 충분 | 플립 문항 한정 채택 |
| D. G-Eval식 루브릭 점수화 | 세밀 | 쌍대→절대점수 전환은 기존 수치와 비교 불가(변인 파괴) | 기각 |

## 7. 리스크 / 불확실성

- **국내 judge의 위치 편향이 gpt-4o보다 클 수 있음** → 양방향 프로토콜이 이미 방어(불일치→tie).
  방향 불일치율을 judge별로 기록해 "judge 신뢰도" 자체를 산출물화.
- **A.X judge의 tie 남발 가능성**(짧은 출력 성향의 판정판) → decisiveness 지표로 노출;
  극단(비-tie < 20%)이면 해당 쌍은 실질 2-패널이 됨을 명기.
- **EXAONE 지연(~36s 콜드)** — 판정 30×2방향×2쌍 ≈ 120콜 직렬 시 오래 걸림 → 워밍업 후
  실행, RPS 제약 없는 병렬화.
- 패널이 기존 결론을 그대로 재확인하면 novelty 없음 — 그 경우에도 "교차검증 통과" 증거 가치는
  남음(부정적 리스크 아님).

## 8. 게이트

1. **G-스모크:** 국내 judge 2종 × 5문항 파일럿 — json 준수·tie율 정상 범위 확인 → 본판정 진입.
2. **G-보고서:** §5 판정 → `competition-report.md` 8.1 갱신(+ 플립 시 6.1/6.6). H-E4b 결과는
   E2 §4.2(judge 재사용)의 패널 규약으로 승계.

## 9. 비용 추정

신규 judge 콜: 30문항 × 3쌍 × 2방향 × 2 국내 judge = **360콜** (t8 확장 시 +α).
gpt-4o 신규 콜 0(기존 `t2_judge.jsonl` 재사용). 구현(pairwise.py 패널화) 2~3시간, 총 반나절.
