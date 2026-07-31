# E2 실험 계획서 — 오케스트레이터 read→act 루프 E2E + 국내 모델 툴콜 실측

> E-시리즈 우선순위 5/5 (규모 최대 — 제출 후 2차 트랙 권장) · 작성 2026-07-13 · 상태: 계획 (미실행)
> 실행 위치: `.worktrees/fugu-ko-experiment`. 프로덕션 코드 수정이 필요해지면 그 부분만
> main 기반 별도 워크트리로 분리(현재 설계상 불필요 — §4.1 참조).

## 1. 목표 / 가설

지금까지의 채점(D2~D4)은 전부 **단일 결정 단위**였다 — intent/decompose/synthesize를 따로따로.
`/agent-work`의 실제 가치는 `orchestrate → decompose → fan_out(leaf) → synthesize`
**루프 전체의 태스크 성공률**인데, 잎 정확도가 높아도 루프에서 오류가 곱해지는지(복리) 상쇄되는지는
E2E만이 답한다. 에이전트 평가의 표준도 per-decision accuracy → task-level success로 이동했다
(τ-bench 계열). 추가로, 국내 모델이 inline agentic 경로(bedrock 엔진식 tool-use 루프)의 두뇌가
될 수 있는지 — **툴콜 능력** — 는 아직 아무도 안 잰 축이다.

- **H-E2a (E2E):** (b) 배정(규칙표)을 루프 전체에 물린 E2E 태스크 성공률이 baseline(gpt-4o-mini)
  단일 E2E 이상이다. (잎 우위 +7.7%p가 루프에서 보존되는가?)
- **H-E2b (오류 전파):** E2E 실패의 주 원인은 잎 오답이 아니라 오케 결정(decompose miss /
  synthesize drop)이다 — 단계별 실패 귀속(attribution)으로 검증.
- **H-E2c (툴콜):** 국내 3종 중 최소 1종이 3-스텝 tool-use 시나리오에서 스텝 정확도 ≥ 80%를 낸다.

## 2. 기존 코드/데이터 접점 (실측 심볼)

| 접점 | 위치 | 용도 |
|---|---|---|
| 루프 진입점 | `orthus/router/decompose.py::answer_or_decompose(..., chat_model=)` — should_decompose→split→fan_out→synthesize 전체가 단일 `chat_model` 인자를 스레딩 | E2E 러너가 직접 호출 (HTTP 불필요) |
| 스테이지 프롬프트 상수 | 같은 파일 `_DECOMPOSE_SYSTEM`, `_SPLIT_SYSTEM_TMPL`, `_SYNTHESIZE_SYSTEM` | §4.1 프롬프트-키 셀렉터의 매칭 키 |
| 잎 실행 | `_run_leaf` → `orthus.router.answer()` (wiki/structured/graph 분기) | 잎 단계 모델 주입 경로 확인 |
| 배정표 | `selectors/static_map.py::TASK_MODEL` | (b) arm의 배정 소스 |
| 워커 풀 | `pool.py::WorkerChat` (`extra_body` 지원 — `tools` 파라미터 주입 가능) | 툴콜 프로브 |
| 합성 질문 생성기 | `t8_synth.py` (유니코드 종성 조사 처리기 내장, TS10) | 복합 시나리오 골든 생성 |
| 기존 오케 골든 | `golden/t6.json`, `golden/t7.json` (intent/decompose 문항) | 시나리오 재료 + 라벨 재사용 |

## 3. 스코프

**In:** 데모 노드(`orthus_company`) 위 E2E 복합 시나리오 20~30건 × {baseline, solar 단일,
(b) 배정} 3 arm. 단계별 실패 귀속. 툴콜 프로브(네이티브 `tools` 지원 여부 + JSON-ReAct 폴백,
3-스텝 시나리오 10건 × 3모델).

**Out:** 프로덕션 orchestrate 엔드포인트/FE 변경, action-intake 실행 경로(policy gate 실측은
P3 회귀 테스트 영역 — E2E는 지식/집계 복합 질문 한정), agentic cli 엔진(claude 전용 경로),
personal 노드, VARCO(D0 드롭 유지).

## 4. 방법

### 4.1 (b) 배정을 루프에 물리는 방법 — 프롬프트-키 셀렉터 (프로덕션 무수정)

`answer_or_decompose`는 chat_model **1개**를 전 단계에 스레딩하므로 단계별 배정이 구조적으로
불가하다. 실험 격리를 유지하는 해법: **`ChatModel` Protocol을 구현한 `PromptKeyedSelectorChat`**
— `.complete(system, user, ...)`에서 `system` 프리픽스를 스테이지 프롬프트 상수와 대조해
`static_map.TASK_MODEL`의 워커로 위임한다 (decompose/split/synthesize 상수 + wiki qa/structured
compile/route classify 프롬프트 프리픽스; 미매칭은 default solar). 프로덕션 diff 0줄.

- 대안 A(단계별 몽키패치): 스테이지 함수를 partial로 감쌈 — 깨지기 쉬움(내부 호출 경로 다수), 기각.
- 대안 B(HTTP + env 스왑): 단일 모델 arm만 가능, (b) arm 불가 — baseline arm 검증용으로만 참고.
- 리스크: 프롬프트 상수가 바뀌면 매칭이 조용히 default로 새어나감 → 러너가 매 콜의
  (매칭 스테이지, 위임 워커)를 jsonl로 기록하고, 미매칭 콜 > 0이면 결과 무효 처리(사전 고정).

### 4.2 E2E 시나리오 골든

- 복합 질문 20~30건: 지식+지식 / 지식+집계 / 집계+집계 조합. **TS9 교훈 준수** — 토픽은
  wiki에 근거 실재가 확인된 것만(기존 t2/t3 골든에서 근거 확인된 토픽 페어링 + `t8_synth` 변형).
- 문항별 기대 앵커 사전 고정: 하위 질문마다 결정론 앵커(집계=정답 number-set, 지식=필수
  키워드/슬러그) — E2E 성공 = **모든 하위 앵커가 최종 답에 존재**.
- 채점 2층: ① 결정론 커버리지(앵커 매칭) ② 커버리지 동률 구간만 쌍대 judge(E4 패널 규약 재사용).

### 4.3 단계별 실패 귀속

E2E 실패 문항마다 중간 산출물(should_decompose 판정, split 결과, 잎별 grounded/정오,
synthesize 입출력)을 jsonl로 남겨 실패 단계를 결정론으로 귀속: `gate_miss`(분해 안 함) /
`split_bad` / `leaf_wrong` / `synthesis_drop`(잎은 맞았는데 최종 답에서 누락). H-E2b 판정 근거.

### 4.4 툴콜 프로브 (별도 arm)

1. **네이티브 지원 프로브(모델당 30분 타임박스, D0 방식):** `tools`/`tool_choice` 파라미터를
   `WorkerChat.extra_body`로 주입해 OpenAI-호환 tool_calls 응답이 오는지 확인
   (Solar=지원 가능성 높음, A.X/EXAONE-Friendli=미확인).
2. **폴백:** 미지원 모델은 JSON-ReAct 프롬프트 루프(3종 모두 json_only 5/5 PASS라 실현 가능).
3. **시나리오:** orthus 함수 3종(wiki 검색→페이지 선택→집계)을 tool로 노출한 3-스텝 시나리오 10건.
   지표: 스텝별 (올바른 tool 선택 / 인자 유효성 / 환각 tool 이름 비율 / 루프 완주율).

## 5. 판정 기준 (사전 고정)

- **H-E2a 지지:** (b) 배정 E2E 성공률 ≥ baseline E2E 성공률 (동일 시나리오, paired).
  미달 시 실패 귀속 분포와 함께 정직 기록 — "잎 우위가 루프에서 증발"은 그 자체가 헤드라인.
- **H-E2c 지지:** 최소 1모델 스텝 정확도 ≥ 80% + 환각 tool 이름 ≤ 5%.
- 툴콜 프로브는 수용선 없는 **프로파일링** — 결과는 배정표에 "agentic loop" 행 추가로 반영.
- 무효 조건: 셀렉터 미매칭 콜 > 0 (§4.1), 앵커 없는 시나리오 혼입.

## 6. 대안 비교 (측정 설계)

| 옵션 | 장점 | 단점 | 판단 |
|---|---|---|---|
| A. `answer_or_decompose` 직접 호출 | 중간 산출물 접근(귀속 가능), HTTP/auth 불요 | orchestrate 엔드포인트 고유 로직(intake 등) 미커버 | 채택 — E2E 정의를 "지식 복합 질문 루프"로 한정했으므로 충분 |
| B. `POST /agent-work/chats/{id}/orchestrate` HTTP | 실배포 경로 그대로 | 모델 주입 불가(env 단일), 중간 산출물 불가시 | 최종 1회 스모크로만 (배정 채택 시) |

## 7. 리스크 / 불확실성

- **표본 20~30으로 arm 간 차이 검별력 낮음** — paired 설계 + 문항별 귀속으로 "어디서 갈렸나"를
  정성 보강. 대규모 확장은 결과가 유망할 때만.
- **judge 의존 구간:** 커버리지 동률 구간 판정이 E4 결과(패널 신뢰도)에 의존 → E4 선행 권장.
- **EXAONE 지연(~콜드 36s, TS3):** E2E는 직렬 다단이라 콜드 1회가 p95를 지배 — 워밍업 콜 후 측정.
- **A.X RPS 3:** fan_out 병렬 잎이 스로틀에 걸림 — `min_interval` 직렬화로 지연만 증가, 정합성 무영향.
- 툴콜 네이티브 프로브가 3종 모두 실패해도 JSON-ReAct 폴백 측정은 성립(프로브 자체가 산출물).

## 8. 게이트

1. **G-준비:** E4 패널 판정 규약 확정(권장) + 시나리오 골든 30건 앵커 검수 완료 → 실행 진입.
2. **G-툴콜:** 네이티브 프로브(모델당 30분) 종료 후에만 루프 하네스 구현 착수(폴백 범위 확정).
3. **G-보고서:** §5 판정 후 `competition-report.md` 4.1/6.4 확장 + 배정표(6.6) agentic 행 추가.

## 9. 비용 추정

E2E: 30문항 × 3 arm × (게이트1 + split1 + 잎~2.5 + synth1 ≈ 5.5콜) ≈ **~500콜**.
툴콜: 10시나리오 × 3모델 × 3스텝 × 폴백 오버헤드 ≈ ~150콜. 합계 크레딧 내. 구현+실행 2~3일.
