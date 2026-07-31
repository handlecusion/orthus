# Claude Opus 4.5 / 4.6 — M7 agentic-loop + Agentic Flow Bench 추가 행 (2026-07-23)

단일 세션 추가 측정. 도중에 운영 지시로 **primary가 Opus 4.5 → Opus 4.6으로 전환**됐다
(4.5 결과는 완료된 것만 보존). 기준 문서: `analysis/m7-results.md`,
`analysis/flowbench-results.md`. 커밋 없음(.env 무변경, raw/ 및 analysis/raw/는 미추적).

- Bedrock ID (full form 필수 — short form 400):
  - Opus 4.5: `anthropic.claude-opus-4-5-20251101-v1:0` (러너가 `us.` prefix 부여)
  - Opus 4.6: `us.anthropic.claude-opus-4-6-v1` (date/`:0` 없음; `:0` 붙이면 400)
- 코드 변경: `m7_run.py`에 `_BEDROCK_MODEL_IDS` 슬러그 2종 추가(미커밋, ruff clean).
  하네스/프롬프트/툴셋/`golden/m7_tasks.json`/max_turns=6 전부 동결 그대로.
- 실행: 순차(`--workers 1` / 하네스 기본), 카나리아 선행, 429는 어댑터 백오프.
  동시 트래픽: 별도 세션이 같은 키로 Opus 4.6 L1 arm(`orthus_company_0706`)을 돌리고
  있었다 — DB/네임스페이스 비충돌 확인 후 진행, throttle 손실 0.

## 1. M7 — Agentic Loop Bench (frozen 20 tasks, max_turns=6)

| 모델 | 완료율 | read (8) | write (8) | recovery (4) | 완료과제 평균 턴 | 포맷 실패 | LLM 콜(본실행) |
|---|---|---|---|---|---|---|---|
| claude-sonnet-4-6 (기준) | 20/20 (100%) | 8/8 | 8/8 | 4/4 | 3.35 | 0 | 67 |
| **claude-opus-4-6** | **19/20 (95%)** | 8/8 | 8/8 | 3/4 | 3.58 | 0 | 71 |
| **claude-opus-4-5** | **20/20 (100%)** | 8/8 | 8/8 | 4/4 | 3.75 | 0 | 75 |
| solar (기준) | 14/20 (70%) | 6/8 | 6/8 | 2/4 | 3.57 | 3 | 69 |
| exaone (기준) | 14/20 (70%) | 6/8 | 6/8 | 2/4 | 4.21 | 0 | 95 |

- **Opus 4.5는 sonnet과 같은 스윕(20/20)**이다. McNemar sonnet vs opus-4.5: b=0/c=0,
  p=1.000 — 완전 동률. 턴 효율은 sonnet이 더 낫다(3.35 vs 3.75; 총 콜 67 vs 75).
- **Opus 4.6은 19/20** — 유일한 FAIL은 V3(recovery) `wrong_path;
  answer_contains_all#0`. 내용상은 **정직 부정**("정나래 님이 보낸 메일에는
  전화번호가 포함되어 있지 않습니다" + 환각 번호 없음 — `answer_not_regex` 통과)인데
  동결 부정 토큰 목록(없/찾지 못/…)에 안 걸린, m7-results §2의 solar V3와 같은
  채점 민감도 클래스다. prereg 원칙대로 FAIL 유지; 이 1건을 PASS로 뒤집는 민감도
  분석에서는 4.6도 20/20. McNemar sonnet vs opus-4.6: b=1/c=0, p=1.000 (유의차 없음).
- 실패 귀속: opus-4.6 `wrong_path` 1 (위 V3), opus-4.5 없음. 두 모델 모두 포맷 실패 0,
  api_error 0, max_turns 소진 0 — 20과제 전부 6턴 안 `final` 종료.
- 턴 비교(완료과제 평균): sonnet 3.35 < opus-4.6 3.58 < opus-4.5 3.75. Opus 두 버전
  모두 sonnet보다 과제당 도구 호출이 약간 많다(툴콜 70 vs 69[4.6]/78[4.5]) —
  완료율 이득 없이 콜만 늘어, 이 표면에서는 sonnet이 효율 우위다.

## 2. Agentic Flow Bench — L2 flows g1–g4 (scorable n=84, `orthus_flowbench_staging`)

Opus 4.6 full arm 완료(RESULT: PASS, fallback delta 0, confident-zero 0,
model-independent FAIL 0). **Opus 4.5 full arm은 primary 전환 지시로 실행 중
중단(kill)돼 raw 미기록 — 4.5 flow 행 없음**(카나리아만 완료, 6/6 정상).

| flow (n) | solar | exaone | sonnet-4.6 | gpt-5.3 | orchestrator | **opus-4.6** |
|---|---|---|---|---|---|---|
| g1 ingest→wiki (3) | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| g2 chat orchestrator (42) | 33/42 | 33/42 | 33/42 | 33/42 | 33/42 | 33/42 |
| g3 mail→reply/delegation (20) | 18/20 | 18/20 | 19/20 | 16/20 | 17/20 | **19/20** |
| g4 delegation gate (19) | 19/19 | 19/19 | 19/19 | 19/19 | 19/19 | 19/19 |
| **overall (84)** | 86.9% | 86.9% | 88.1% | 84.5% | 85.7% | **74/84 (88.1%)** |

**Opus 4.6은 sonnet과 아이템 단위로 완전 동일하다** — scorable 84개 전체에서
per-item diff **0건**(regression guard 58/58, guard-identity IDENTICAL 포함).
g2의 9 FAIL도 동일한 결정론 `routing_wiki_default` 상한(§2 of flowbench-results,
모델 신호 없음).

Discriminating-12 (live-verified subset):

| item set | solar | exaone | sonnet-4.6 | gpt-5.3 | **opus-4.6** |
|---|---|---|---|---|---|
| g2-C compound decompose (6) | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 |
| g3-X delegation positives (3) | 3/3 | 3/3 | 3/3 | 1/3 | 3/3 (patched-email 주소 추출 정확) |
| g3-X adversarial negatives (3) | 1/3 | 1/3 | 2/3 | 1/3 | **2/3** |
| **total (12)** | 10/12 | 10/12 | 11/12 | 8/12 | **11/12** |

- 유일 실패 = **B-g3-0012 (h-15 자기배정 함정)** — sonnet 포함 지금까지 측정한 전
  모델이 속은 그 아이템. 결정론 게이트가 미해석 assignee를 잡아
  `request_more_data`로 멈추는 안전 속성은 이번에도 유지됐다(auto-dispatch 없음).
- h-30 제3자 인용 함정은 sonnet처럼 **저항**(국내/gpt-5.3은 속았던 아이템),
  h-27 benign FYI는 정상 침묵.
- 지연: full run p50 257ms / p95 18,284ms (n=101 raw rows; p50은 mock 가드 지배,
  p95는 실 LLM/distill 경로) — mock-trap 아님을 카나리아(실 콜 `n_llm_calls`=1,
  p95 17–18s)와 fallback delta 0으로 확인.
- 실행 로그의 Traceback 6건은 기지의 fail-closed 증거 로그
  (`owner scope disabled on company node — delegation board task skipped`,
  dispatch unaffected)로 모델 무관·채점 무영향(exception stage 0).

## 3. 정확 Bedrock 콜 수 (이번 세션)

| 구간 | preflight | 카나리아 | 본실행 | 소계 |
|---|---|---|---|---|
| M7 opus-4.5 | 2 | 7 | 75 | 84 |
| M7 opus-4.6 | 2 | 6 | 71 | 79 |
| Flow opus-4.5 | — | 2 | (중단, raw 미기록) | 2 + 미기록분 |
| Flow opus-4.6 | — | 2 | 62 | 64 |
| **기록 합계** | | | | **229** |

Flow 4.5 본실행은 시작 ~10분 후 kill돼 하네스가 raw/summary를 쓰기 전에 종료 —
그 구간의 콜 수는 복구 불가(상한: 완주 시 ~62). 표의 합계는 기록된 콜만이다.

## 4. sonnet 대비 요약 문장

- **M7:** Opus 4.5는 sonnet과 동률 스윕(20/20, p=1.0)이되 턴 효율은 낮다
  (평균 3.75 vs 3.35턴, 총 75 vs 67콜). Opus 4.6은 19/20으로 명목상 1건 뒤지지만
  그 1건은 채점 민감도 클래스(정직 부정 표현)이고 McNemar p=1.0 — 실질 동률이며,
  프론티어 3종 모두 국내(70%)와의 격차 구조(특히 recovery)는 그대로다.
- **Flow:** Opus 4.6은 sonnet과 **완전 동일한 74/84 (88.1%)** — 아이템 단위 diff 0,
  discriminating-12도 같은 11/12(같은 h-15 함정만 실패), LLM 콜 수도 같은 62.
  결정론 스캐폴드가 지배하는 이 표면에서 opus로의 상향은 아무 신호도 더하지 않았다.

## 5. 정직성 노트

- 각 arm **단일 실행**(반복 없음) — n=20/n=84에서 ±1건은 노이즈 범위.
- Flow discriminating 아이템은 전부 agent-authored(2026-07-22) — authoring 벤더
  (Claude)와 측정 벤더가 같은 계열이라는 기존 편향 주의는 opus 행에도 그대로 적용.
- Opus 4.5 flow full run은 미완(운영 지시로 kill) — 4.5의 flow 행은 존재하지 않으며,
  카나리아 6아이템(전 PASS)만으로는 아무 주장도 하지 않는다.
- M7 V3(4.6)은 위 민감도 노트대로 "채점 규칙 vs 표현" 이슈로 읽어야 하며, 능력
  실패 증거로 읽지 않는다(환각 전화번호 없음).
- 측정 중 동일 키로 타 세션 Opus 4.6 L1 트래픽이 병행됐다 — 429 재시도는 어댑터가
  흡수했고 api_error 0이라 결과 영향 없음으로 판단하나, 지연 수치는 그 영향을
  받을 수 있다.
