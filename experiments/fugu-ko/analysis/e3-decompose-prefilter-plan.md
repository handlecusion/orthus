# E3 실험 계획서 — decompose 결정론 prefilter 확장 (O4 후속)

> E-시리즈 우선순위 4/5 · 작성 2026-07-13 · 상태: 계획 (미실행)
> **실행 위치 주의:** 측정(골든/하네스)은 `.worktrees/fugu-ko-experiment`에서,
> **구현(프로덕션 코드)은 main 기반 별도 워크트리 + 일반 PR**로 진행한다 — 이 실험만
> orthus 제품 코드를 수정하며, fugu-ko 실험 격리 원칙의 대상이 아니다.

## 1. 목표 / 문제 정의

D4의 **O4 발견**: `should_decompose`의 False-프리필터가 아는 신호가 접속어 7개
(`_CONNECTIVE_TOKENS`) + 열거 5개(`_ENUM_TOKENS`) + `"1."&"2."` 뿐이라, "랑/이랑/각각/비교형"
복합질문은 **LLM 게이트에 도달조차 못 하고** 단일로 답한다 — 복합질문의 절반이 조용히 누락되는
사용자 체감 최대 실패 모드이고, **어느 모델을 쓰든 발생**한다(모델 교체로 해결 불가 영역).

- **H-E3:** 프리필터 신호를 티어별로 확장하면, 누락 유형 복합질문의 LLM-게이트 도달 recall을
  0% → ≥80%로 올리면서 단일 질문 오통과(FP)로 인한 지연·오분해 비용을 수용선 내로 유지할 수 있다.

이 실험은 "모델 국산화 이득"과 별개인 **시스템 개선 기여도**를 분리 측정한다 — compound AI
system 관점("모델이 아니라 시스템을 최적화")의 보고서 서사 축.

## 2. 기존 코드 접점 (실측 심볼) — 제약 포함

| 접점 | 위치 | 내용 |
|---|---|---|
| 신호 상수 | `orthus/router/decompose.py:52-67` `_CONNECTIVE_TOKENS`(7) / `_ENUM_TOKENS`(5) | 확장 대상 |
| 프리필터 | 같은 파일 `_has_connective_or_enum()` (:171) | 확장 지점 |
| 게이트 | `should_decompose()` (:210) — 프리필터 통과 시에만 LLM enum | FP 비용 = LLM 1콜 추가 |
| **공유 제약 ★** | `command_split_signal()` (:182) — 같은 `_has_connective_or_enum`을 재사용하며 docstring이 **"공유 토큰셋 수정 금지(수정 시 should_decompose 모집단 오염 + flag-off byte-identical 파괴)"** 를 명시 | 공유 상수 직접 확장 불가 → §4.1 설계로 우회 |
| 측정 하네스 | `experiments/fugu-ko/harness.py::run_t7` (`reached_llm` = 프리필터 통과 여부 기록) | 전/후 recall 측정 재사용 |
| 기존 골든 | `golden/t7.json` (22문항) | 회귀 세트(무변화 확인용) |
| 조사 처리기 | `t8_synth.py` (유니코드 종성, TS10) | 신규 골든 생성 |

## 3. 스코프

**In:** `_has_connective_or_enum`의 **should_decompose 경로 한정** 신호 확장, fail-closed flag,
전/후 recall·FP 측정, 단위/회귀 테스트, 일반 PR 1건.

**Out:** `command_split_signal` 의미 변경(공유 제약 유지 — command-split 모집단 불변),
LLM 게이트 프롬프트 수정, split/synthesize 로직, 모델 배정(E-시리즈 다른 실험 영역),
`_COMMAND_VERBS` 확장(agentwork 영역 — 별도 슬라이스).

## 4. 방법

### 4.1 설계 — 확장은 should_decompose 전용 + fail-closed flag

공유 제약(§2 ★) 때문에 상수 직접 확장은 금지. 대신:

```
_CONNECTIVE_TOKENS_EXT = (...)   # 신규 상수 (티어별)
_has_connective_or_enum(question, *, extended: bool = False)
```

- `should_decompose`만 `extended=settings.decompose_prefilter_ext`(신규 flag
  `ORTHUS_DECOMPOSE_PREFILTER_EXT`, **default false**)를 전달.
- `command_split_signal`은 `extended=False` 고정 → command-split 모집단 byte-identical 보존.
- flag off 시 전 경로 byte-identical (기존 t7 골든 + decompose 단위 테스트 무변화로 증명).

### 4.2 신호 후보 — FP 위험 티어 분리 (핵심 설계 결정)

| 티어 | 후보 | FP 위험 | 근거 |
|---|---|---|---|
| T1 (저위험) | `각각`, `비교`, `차이`, `vs`, `둘 다`, `모두 알려`, `celebrity 열거형(셋째)` | 낮음 — 단일 질문에 드묾 | O4 명시 누락 유형 |
| T2 (중위험) | `~는 뭐고`/`~은 뭐고`/`~이고` (연결어미 `-고` 패턴), `이랑`, `랑 ` | 중간 — 어미/조사라 단일 질문에도 출현 | "환불이랑 배송" 유형 |
| T3 (고위험) | `와 `, `과 ` (병렬 조사) | 높음 — "A와 B의 관계는?"(단일)과 구분 불가 | 측정만, 채택은 결과 조건부 |

티어를 **누적 적용**(T1 → T1+T2 → T1+T2+T3)해 recall/FP 곡선을 그리고, 채택 티어를 데이터로
결정한다. FP의 실비용은 2단계로 분리 측정: ① 프리필터 오통과율(= LLM 게이트 1콜 추가 지연),
② 최종 오분해율(LLM 게이트가 yes로 오판해 실제 fan-out — 진짜 품질 비용). LLM 게이트가 FP를
걸러주는 구조라 ①과 ②를 구분하는 것이 판정의 핵심.

### 4.3 골든 구축 + 측정

- **누락형 복합 40문항:** O4 유형(랑/같이/각각/비교형) — `t8_synth` 변형 + 수동 저작,
  근거 실재 토픽만(TS9). 현행 프리필터에서 `reached_llm=False`인 것만 채택(누락형 보장).
- **단일 질문 40문항(FP 대조군):** 기존 t5/t2 골든의 단일 질문 + T2/T3 신호를 포함하는
  단일 질문("A와 B의 관계는?")을 의도적으로 다수 포함(adversarial).
- `run_t7`로 전/후 paired 측정 — 프리필터 `reached_llm`, 게이트 판정, split 유효율.
  LLM 게이트 모델은 (b) 배정대로 solar 고정(변인 통제).

## 5. 판정 기준 (사전 고정)

- **채택(티어 단위):** 누락형 recall(프리필터 통과) ≥ 80% **AND** 단일 대조군 최종 오분해율
  ≤ 5% **AND** 단일 대조군 프리필터 오통과율 ≤ 15%(= 지연 비용 상한) **AND** 기존 t7 골든
  22문항 판정 무변화.
- **flag-off 불변:** flag off에서 decompose 관련 전 테스트 + t7 재실행 byte-identical.
- 티어별 독립 판정 — T1만 통과하면 T1만 채택(전부-아니면-전무 아님).

## 6. 대안 비교

| 옵션 | 장점 | 단점 | 판단 |
|---|---|---|---|
| A. 티어별 토큰/패턴 확장 (본안) | LLM 0콜, 결정론, 측정 용이 | 어휘 커버리지 한계(꼬리 유형 잔존) | 채택 |
| B. 프리필터 제거(전 질문 LLM 게이트) | recall 100% | 전 트래픽 +1 LLM 콜 — fast-path 설계 의도(O1) 파괴 | 기각 |
| C. 경량 형태소 분석(kiwipiepy 등) 병렬 조사 검출 | T3 유형 정밀 | 신규 의존성 + 결정론 코드에 모델성 컴포넌트 — 과설계 | 보류(A의 T3 실패 시 재검토) |
| D. LLM 게이트를 소형 국내 모델로 상시 호출 | — | B와 동일 비용 구조 + 모델 의존 | 기각 |

## 7. 리스크 / 불확실성

- **command-split 경로 오염(최대 리스크):** `command_split_signal` 공유 — §4.1 분리 설계 +
  command_split 단위 테스트 무변화 증명으로 방어. (C7/C8은 2026-07-13 owner 결정으로 미진행 —
  `decompose.py`를 두고 경쟁하는 다른 브랜치는 없다. main에서 단독 진행.)
- **FP 대조군의 대표성:** adversarial 단일 질문을 우리가 저작 → 실분포보다 가혹할 수 있음
  (보수 방향이므로 수용). G3 교훈(F9: 합성 분포 이동) 역방향 적용 — 대조군은 가혹할수록 안전.
- **LLM 게이트의 FP 필터 성능이 모델 의존:** solar 고정으로 측정하되, 채택 후 프로덕션 기본
  모델(gpt-4o-mini)로도 대조군 1회 재측정(운영 구성 검증).

## 8. 게이트

1. **G-골든:** 누락형 40 + 대조군 40 저작·검수(현행 프리필터 통과 여부 라벨 포함) → 구현 착수.
2. **G-구현:** flag-off byte-identical 테스트 통과 → 측정 진입.
3. **G-PR:** §5 채택 티어 확정 + `make test` + t7 회귀 → main 기반 워크트리에서 PR
   (제목 예: `[O4] decompose prefilter 신호 확장 (flag, fail-closed)`).
4. **G-보고서:** fugu-ko 보고서에는 "시스템 개선 기여도" 절로 recall/FP 곡선만 인용(코드는 제품 PR).

## 9. 비용 추정

골든 저작 반나절 + 구현/테스트 반나절 + 측정(80문항 × 전/후 × 게이트 도달분만 LLM ≈ 100여 콜) 반나절.
총 ~1.5일. GPU 불필요.
