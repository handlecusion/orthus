# BFCL v3 multi-turn 결과 (외부 corroboration — B2-C6 / B3)

> 실행: 2026-07-22~23. 사전선언 = `analysis/bfcl-prereg.md` (모델 호출 전 등록).
> 하네스: gorilla BFCL, 커밋 `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8`
> (gitignored `external/.cache/bfcl/`). 채점 전부 프로그램(state + call-subset), 판정자 없음.
> 러너: `bfcl_run.py` (키 in-process 주입, 디스크에 키 파일 없음) ·
> split: `bfcl_split.py`. temperature 0.001, 단일 런.

## 주 결과 (카테고리당 200문항, A.X는 20문항 probe)

| 모델 | multi_turn_base | multi_turn_miss_param | Δ(base−mp) | hold-턴 asked-rate* |
|---|---|---|---|---|
| Claude Opus 4.6 (Bedrock) | **75.5%** | **60.0%** | 15.5 | 0.590 |
| Claude Sonnet 4.6 (Bedrock) | 73.0% | 56.0% | 17.0 | **0.625** |
| EXAONE (Friendli dedicated) | 49.5% | 34.5% | 15.0 | 0.520 |
| Solar Pro (Upstage) | 39.0% | 19.5% | 19.5 | 0.260 |
| A.X-K1 (probe 20문항) | 0% | 0% | — | (1.0 — artefact, 아래) |

\* asked-rate = miss_param에서 ground truth가 빈 턴(정보 부족 → 호출하면 안 되는
턴)에 structured call을 내지 않은 아이템 비율 (`bfcl_split.py`, BFCL 채점과 독립
축). 반대편 수치가 환각-인자 호출: Solar 148/200, EXAONE 96/200, Opus 82/200,
Sonnet 75/200.

**A.X asked-rate 1.0은 무의미하다** — 40/40 아이템 전부에서 structured tool call
0건, 텍스트(bash 코드블록/산문)로만 "시도". 호출 자체를 못 하니 hold-턴에서도
자동으로 '자제'로 집계될 뿐이다.

## 등록 예측 판정

| 예측 | 결과 |
|---|---|
| **P1** base 순위 Sonnet ≥ solar > exaone >> ax | **부분 일치.** frontier >> 국내 >> ax(0)는 재현. 그러나 **solar/exaone 순서가 뒤집혔다**: BFCL은 EXAONE(49.5) > Solar(39.0), 내부 B2-C6는 solar 90 > exaone 82.5. 아래 해석. |
| **P2** miss_param에서 frontier-국내 격차 확대 | **혼재.** Δ(Sonnet−Solar): base 34.0 → mp 36.5 (확대, 예측 방향). Δ(Sonnet−EXAONE): 23.5 → 21.5 (소폭 축소). 상대비(frontier 대비 비율)로는 국내 모델이 mp에서 더 크게 무너진다(Solar 39→19.5는 절반, Sonnet 73→56은 23% 감소). |
| **P3** ax는 strict 0, 시도는 텍스트에 존재 | **적중.** strict 0/40, 텍스트 시도 40/40. 내부 B2 strict-vs-lenient 소견과 동형. |
| **B3 corroboration** (asking 행동 frontier 우위) | **재현.** frontier 0.59–0.63 vs Solar 0.26. 단 EXAONE(0.52)은 내부 예상보다 frontier에 가깝다. |

### 부호 일치(sign agreement) 요약

- **일치**: frontier(Claude) > 국내 모델, ax ≈ 0 (strict), asking 행동 frontier 우위,
  Solar의 "묻는 대신 환각 인자로 호출" 경향 (B3-R2와 동방향).
- **불일치**: 국내 2강의 순서. 내부 B2-C6(단일턴, 우리 도메인 tool, 포맷 준수율)는
  solar > exaone였지만 BFCL multi-turn(장기 실행 + 상태 추적)은 exaone > solar.
  측정 construct가 다르다: B2는 "호출 포맷을 지키는가", BFCL은 "여러 턴에 걸쳐
  올바른 호출 시퀀스를 완주하는가". Solar의 65k context 상한이 multi-turn
  long-horizon에서 실제로 두 번 터졌고(400 context-length, 재생성으로 회수),
  낮은 asked-rate(0.26)가 miss_param 점수를 직접 깎는다. 내부 순위의 외부 이식은
  **작업 형태(단일턴 포맷 vs 장기 orchestration)에 따라 뒤집힐 수 있다**가 정직한
  결론이다.

## 내부 벤치와의 대응표

| 축 | 내부 (B2-C6 strict) | BFCL base | 대응 |
|---|---|---|---|
| Sonnet | 95 | 73.0 | 1위 유지 ✓ |
| Solar | 90 | 39.0 | 2위 → 3위 ✗ |
| EXAONE | 82.5 | 49.5 | 3위 → 2위 ✗ |
| A.X | 0 | 0 | 최하위/포맷 불이행 ✓ |

## 하네스 비호환 vs 모델 실패 (silent-zero 조사 기록)

- **EXAONE 최초 캐너리 0%는 하네스 비호환이었다**: Friendli가 BFCL tool 스키마의
  비표준 `response` 필드를 strict 파싱으로 422 거부. 수정: 커스텀 핸들러에서 tool
  function을 `{name, description, parameters}`로 정규화(전 OpenAI-compat 모델 공통
  적용). 수정 후 EXAONE은 정상 점수 — 이 값이 본 표다. strict한 쪽이 오히려
  하네스(우리)였던 케이스로, B2 strict-vs-lenient 교훈의 역방향 사례.
- **A.X 0%는 모델 실패다**: 요청은 정상 수리되고 응답도 오지만 structured tool
  call을 전혀 내지 않는다(내부 B2와 동일). format-tolerant rescore는 하지 않았다
  — 텍스트 bash를 실행 가능한 호출로 재해석하는 것은 별도 실행기 구현이라
  "저렴한" 재채점 범위를 벗어난다. 시도 존재 여부(40/40)만 기록.
- Solar 400(context 65k 초과) 2건, EXAONE 오류 2건(행 재생성으로 전부 회수) —
  최종 결과 파일에 inference error 0.

## 운영 기록

- 정확 호출 수(결과 파일 latency 항목 합): Solar 4,500 · EXAONE 5,767 ·
  Sonnet 4,211 · Opus 4,215 · A.X 160. Bedrock 총 ~8.4k 호출, threads ≤4
  (Sonnet 4, Opus 2 — 코디네이터 지시).
- EXAONE(Friendli dedicated)은 산발적 요청 hang으로 3회 재시작:
  기본 SDK timeout 600s가 워커를 장시간 점유 → 핸들러에 timeout 150s +
  max_retries 3을 넣자 처리량 정상화. resume은 id 단위 skip이라 중복/유실 없음
  (유니크 id 검증함).
- Opus 4.5가 먼저 큐에 있었으나 실행 전 4.6 지시로 대체됨 — 4.5는 캐너리 포함
  일절 실행하지 않았다. Opus 4.6 Bedrock ID는 `us.anthropic.claude-opus-4-6-v1`
  (`-v1` 접미, 날짜/`:0` 없음).
- Bedrock 인증: `AWS_BEARER_TOKEN_BEDROCK` bearer 키 + anthropic SDK
  `AnthropicBedrock` — orthus converse 어댑터와 같은 키로 동작 확인.
- 클론 로컬 패치(커밋 안 함, gitignored): `fugu_ko.py` 핸들러 4종+Bedrock,
  `model_config.py`/`supported_models.py` 등록 5건.

## Caveats (사전선언 non-claims 재확인 + 추가)

1. 단일 런, temperature 0.001 — 분산 추정 없음. 순위 간 근접 구간(예: Opus 75.5
   vs Sonnet 73.0)은 동률로 취급해야 안전하다.
2. 공개 벤치마크 — 오염 가능성. trajectory 채점이 희석하지만 제거하지 못함.
3. 시뮬레이터 8종 API는 우리 도메인이 아니다. miss_param→`request_more_data`
   구조 동형성은 유효하나 도메인 전이 주장은 하지 않는다.
4. EXAONE은 Friendli dedicated 엔드포인트(내부 운영 구성) — 공식 리더보드 제출
   구성과 다를 수 있어 절대 수치 비교 금지.
5. asked-rate는 "그 턴에 호출을 안 했다"의 근사다. 실제로 되물었는지 vs 그냥
   무응답인지의 세부는 텍스트 검사가 필요하며 여기선 구분하지 않았다.
6. A.X는 20문항 probe — 순위 비교 대상이 아니다(사전선언대로 기록만).
