# 핸드오프 — 검증 캐스케이드(SVC)가 실제로 쓸모 있는지 실측

> 상태: **실측 완료 (2026-07-15)**. 결과:
> `experiments/fugu-ko/analysis/svc-cascade-verify-results.md`
> (브랜치 `exp/cascade-verify`, 하네스 `experiments/fugu-ko/e6_{replay,cascade,eval}.py`).
> 작성 2026-07-15. 이 문서 하나로 콜드스타트 가능하게 작성했다.
>
> **결론 요약:** 2차=A.X로 실측하니 캐스케이드는 **작동한다** — 회수는 오류-비상관 2차가
> 원인임이 통계적으로 확정됐다(공동 발동 트랩 arm A[gpt→A.X] **11/11 회수** vs arm B[gpt→gpt]
> **0/11**, McNemar **p=0.00098**; gpt 이모지 DB 1/14→8/14). **그러나 설계가 가정한 "오발동 0"은
> 반증됐다** — 진짜-0(TN) 질문에서 2차가 다른 해석의 확신 비-0을 내면 채택돼 오답이 된다
> (arm A 오발동 2/36 ≈ 5.6%). 순효과는 정확도 +이나 무료 안전은 아니다.
>
> **실측 중 발견·수정한 본 문서 오류 3건:** ① 모드 env는 `ORTHUS_STRUCTURED_FALLBACK_MODE`
> (아래 §5의 `ORTHUS_LLM_FALLBACK_MODE`는 미존재). ② provider=`ax` 2차 키는
> `ORTHUS_LLM_AX_API_KEY`에서 오고, 아래 §5가 지시한 `ORTHUS_LLM_FALLBACK_API_KEY`/`_BASE_URL`은
> 벤더 경로에서 무시된다(registry.py:266-275). ③ 하네스가 `chat_model=`을 주입하면 캐스케이드가
> 스킵되므로 env로만 지정하고 preflight로 span 발생을 확인해야 한다. **아래 원안은 인수인계
> 기록으로 보존하되, 정확한 절차/수치는 결과 문서를 따를 것.**

---

## 0. 한 줄 요약

검증 캐스케이드(SVC)는 1차 모델의 "조용히 틀린 0행"을 2차 모델로 잡는 장치인데, **설계대로
2차를 오류 비상관 모델(A.X)로 두고 실제 루프에서 효용을 잰 적이 한 번도 없다.** 유일한 E2E
측정이 2차를 1차와 **같은 모델(gpt-4o-mini)**로 둬서 구조적으로 아무것도 못 잡았다. 이 작업은
**2차 = A.X**로 두고 캐스케이드가 실제로 오답을 회수하는지 실측한다.

---

## 1. 캐스케이드(SVC)가 무엇인가

`orthus/structured/query.py`. NL→SQL structured 답을 1차 모델이 낸 뒤, **결정론 신호**로 "이 답
망가졌다"가 감지되면 2차 모델로 한 번 더 묻는다. LLM confidence/logprob는 **일절 안 본다**(절대룰:
confidence routing 금지 — 채택은 결정론 코드가 정함).

- **트리거**(`_retry_signal`, query.py:313): `gate_fail`(sqlglot 게이트 실패) / `not_executed` /
  `empty_rows`(0행) / `zero_answer`(결과 정수집합이 {0}). None이면 1차 그대로.
- **채택 규칙**(query.py:407-420): 신호가 뜨면 2차 재실행 → **2차도 신호가 뜨면 1차 유지**("두
  모델이 독립적으로 '없다' → 진짜 0"), **2차가 정상이면 2차 채택.**
- **모드**(`_FALLBACK_MODES = {"off","shadow","on"}`): `off`=신호 계산도 안 함, `shadow`=발동률만
  audit 기록·재질의 없음(추가 LLM 0), `on`=재질의. `structured.fallback`/`structured.fallback_shadow`
  audit span.
- **2차 모델 슬롯**: `ORTHUS_LLM_FALLBACK_MODEL`(+`ORTHUS_LLM_FALLBACK_PROVIDER`). 미설정이면 캐스케이드
  off. 이건 배정 폴백 사다리(`FallbackChat`)와는 **다른 별도 슬롯**이다.

### 설계가 요구하는 2차 모델 = A.X (정확도 아님, 오류 비상관)
- E1 실측: 모델 간 오답 상관 **Jaccard 0.00**(structured 오답이 서로 겹치지 않음). SVC가 2차에서
  요구하는 건 **정확도가 아니라 "1차와 다른 문제에서 틀리는" 비상관 오류**다.
- A.X는 Solar 오답 8건 중 **4건 회수, Jaccard 0.24(최저 상관)** — 그래서 SVC 2차로 배정됐다
  (`docs/model-orchestration.md` §12.4 "SVC 2차 검증 → A.X").
- `registry.py:258`은 `ORTHUS_LLM_FALLBACK_MODEL == ORTHUS_LLM_MODEL`(같은 모델)이면 **경고**한다
  (거부는 아님): "같은 모델은 검증이 아니라 1차를 두 번 부르는 것 — 체계적 버그는 안 고쳐지고
  1.22× 비용만 낸다."

---

## 2. 왜 "미검증"인가 (핵심)

두 측정이 있었지만 **둘 다 "실제 루프에서 A.X 2차의 효용"을 재지 않았다:**

1. **E1(잎/오라클, PR #684):** 캐스케이드 solar→exaone이 오라클 골든에서 18/18, 1.05× 호출, 손실
   0. **그러나 이건 잎 단위 합성 측정**이지 전체 파이프라인(라우팅→structured→합성) E2E가 아니다.
2. **E2(E2E 루프, `docs/model-orchestration.md` §13.5):** 캐스케이드 ON/OFF를 3회씩 돌렸는데
   (ON 1/65 zero vs OFF 2/66), **`ORTHUS_LLM_FALLBACK_MODEL`을 `gpt-4o-mini`로 뒀고 baseline arm의
   1차도 `gpt-4o-mini`** — **같은 모델**이다. 이모지 DB명 버그(`🎯 NOVA 영입 후보 리스트`를
   놓쳐 0행)를 **양쪽이 공유**하므로 캐스케이드가 **구조적으로 구제 불가능**했다(zero 2건이 3회
   모두 안 고쳐짐).

**→ 그래서 "캐스케이드는 쓸모없다"는 결론은 성립하지 않는다. 그 측정은 캐스케이드를 시험한 게
아니라 잘못 배선된 캐스케이드를 시험했다.** 설계대로 **2차 = A.X(오류 비상관)**로 두고 실제
실패 케이스(이모지 zero-answer 등)에서 회수하는지가 **아직 실측되지 않았다.**

---

## 3. 검증할 질문

1. **회수율:** 1차가 조용히 틀린 zero-answer(예: 이모지 DB명) 중, 2차(A.X)가 **정답으로 고치는**
   비율은? (E1 오라클 18/18이 A.X·실루프에서도 유지되나?)
2. **오발동 방어(false adoption):** 정답이 **진짜 0/없음**인 질문(TN)에서, 2차가 잘못 override해
   틀린 비-0을 내는 비율은? (설계상 2차도 신호 뜨면 1차 유지 → 이게 실제로 지켜지나)
3. **비상관 재현:** A.X 오답이 1차와 실제로 비상관인가(Jaccard 낮게 유지)? 루프에서도 성립하나?
4. **비용:** 추가 호출 배율(트리거율 × 1). E1 1.05× / 같은-모델 1.22× 대비 실측은?
5. **대조:** 2차=같은 모델(gpt→gpt)이면 회수 ~0임을 재현해 **"비상관 2차가 원인"**임을 보이기.

---

## 4. 실험 설계

### arm (2차 모델을 바꿔 가며)
| arm | 1차 | 2차 | 기대 |
|---|---|---|---|
| **A (핵심)** | gpt-4o-mini(이모지 버그 보유) | **A.X** | 이모지 zero-answer 회수 — SVC 설계 검증 |
| B (대조) | gpt-4o-mini | gpt-4o-mini(같은 모델) | 회수 ~0 재현(E2 결과) → 비상관이 원인임 증명 |
| C | Solar | A.X | Solar가 놓치는 케이스 회수(프로덕션 배정 조합) |
| D (참고) | Solar | EXAONE | E1이 쓴 조합 |

### 골든셋 (두 종류 필요 — 회수 + 오발동 둘 다 봐야 함)
- **회수용:** structured zero-answer 케이스(1차가 유효 SQL·게이트 통과인데 0행 오답). E2 골든
  `experiments/fugu-ko/golden/e2_v2x*.json`의 이모지 DB명 문항(`🎯 NOVA 영입 후보 리스트` 등,
  gold 19행인데 이모지 누락 시 0행)이 대표. `docs/model-orchestration.md` §13 참조.
- **오발동용(중요):** 정답이 **진짜 0/없음**인 질문(TN). `experiments/fugu-ko/golden/`의
  routing TN 버킷(`r1_tn.py`가 생성, SQL로 0행 확인된 "정답=없음")을 재사용. 2차가 이걸 잘못
  override하지 않는지 = 캐스케이드의 안전성.

### 지표
- **회수율** = (2차가 고친 오답) / (1차 zero-answer 오답).
- **오발동율** = (2차가 진짜-0을 override해 틀림) / (TN 케이스). **0이어야 안전.**
- **비상관** = 1차 오답셋 vs 2차 오답셋 Jaccard(낮을수록 좋음, A.X 목표 ≤0.24).
- **비용 배율** = 총 호출 / 1차만일 때 호출(트리거율에 비례).
- arm A vs B 대비로 "비상관 2차"의 순효과를 분리.

---

## 5. 정확한 코드 / 설정 위치

| 위치 | 내용 |
|---|---|
| `orthus/structured/query.py:313` `_retry_signal` | 트리거 4종(gate_fail/not_executed/empty_rows/zero_answer) |
| `orthus/structured/query.py:370-420` | 캐스케이드 본체(mode·shadow·2차 재실행·채택 규칙) |
| `orthus/models/registry.py:217-295` `get_fallback_chat_model` | 2차 모델 슬롯 빌드 + **같은-모델 경고**(258) |
| `orthus/settings.py` | `llm_fallback_model`·`llm_fallback_provider`·`llm_fallback_api_key`·`llm_fallback_base_url` + 모드 설정 |
| `orthus/models/orchestration.py:88-91` | SVC 2차=A.X 근거 주석("decorrelated error, not accuracy") |
| `docs/model-orchestration.md` §13.5 + ⚠️⚠️ 블록(145-160줄) | E2 캐스케이드 ON/OFF 측정 + "2차를 잘못 뒀다" 자기감사 |

### 캐스케이드를 A.X 2차로 켜는 설정 (arm A/C)
```bash
ORTHUS_LLM_FALLBACK_MODE=on              # (env 이름은 settings.py에서 확인 — _fallback_mode)
ORTHUS_LLM_FALLBACK_PROVIDER=ax
ORTHUS_LLM_FALLBACK_MODEL=A.X-K1
ORTHUS_LLM_FALLBACK_API_KEY=<SKT A.X 키>
ORTHUS_LLM_FALLBACK_BASE_URL=https://awf-gw.adot.ai/v1   # ax 슬롯 기본값
# 1차는 ORTHUS_LLM / ORTHUS_LLM_MODEL 로 지정 (arm A=gpt-4o-mini, arm C=solar)
```
> ⚠️ **provider를 반드시 `ax`로** 지정하라. `openai` + 손수 base_url로 같은 엔드포인트를 부르면
> A.X의 RPS 3 레이트리밋이 안 걸려 429가 caller에서 삼켜지고(`except→1차 유지`) 캐스케이드가
> **조용히 아무것도 안 고치면서 설정된 것처럼 보인다**(registry.py 주석 경고).

### 하네스
- `experiments/fugu-ko/e1_vote.py` — E1 캐스케이드 vs 다수결 투표(오라클/합성).
- `experiments/fugu-ko/e1_synth_eval.py` · `e1_synth_rerun.py` — E1 합성 재현.
- `experiments/fugu-ko/e2_final_eval.py` · `e2_e2e.py` — E2E 루프(§13.5 캐스케이드 ON/OFF는
  여기서 돌렸다). **이 경로를 2차=A.X로 재실행**하는 게 핵심. `e2_e2e.py::anchor_hit`가 정오답
  판정 SoR.

---

## 6. 성공 / 실패 기준

- **캐스케이드 유용(검증 성공):** arm A(2차=A.X)가 1차 zero-answer를 **의미 있게 회수**
  (예: 이모지 문항 회수율 높음) **하면서 오발동율 0**(TN 진짜-0을 안 뒤집음), 비용 ~1.05–1.22×.
  arm B(같은 모델)는 회수 ~0 → **"비상관 2차가 원인"**이 대조로 증명됨.
- **캐스케이드 무용(설계 반증):** arm A에서도 A.X가 거의 회수 못 하거나(비상관이 루프에서
  깨짐), 오발동율이 높아 진짜-0을 자주 뒤집으면 → 캐스케이드 재설계/철회 근거. (지금까지의
  E1·비상관 근거상 가능성 낮지만, **재는 게 이 작업의 목적**이다.)
- 통계적 힘: E2 표본이 작아(38문항) 노이즈 바닥 ±4/38. **회수 케이스 수를 충분히 확보**하도록
  이모지/zero-answer 문항을 골든에서 넉넉히 모을 것(합성 증강 허용, 단 실 DB 실행 기반).

---

## 7. 가드레일 / 주의

- **A.X RPS 3:** 2차가 A.X면 초당 3건 제한. 측정 루프에 rate-limit 백오프를 넣어라. 429를
  그냥 삼키면 회수가 0으로 나와 **거짓 음성**이 된다(위 provider 경고와 같은 함정).
- **결정론 유지:** 채택은 `_retry_signal`(결정론)만으로. confidence/logprob 도입 금지(절대룰).
- **오발동 반드시 측정:** 회수만 보고 "유용"이라 하면 안 된다. TN(진짜-0) override가 0인지 같이
  봐야 안전성이 증명된다. 설계상 "2차도 신호 뜨면 1차 유지"가 이걸 막게 돼 있는데, **실제로
  지켜지는지**가 검증 대상이다.
- **1차 트리거율 ≠ 회수율 혼동 금지:** E2 자기감사(§13.5)가 "1차 실행 창만 보고 트리거 0건"이라
  잘못 썼던 실수. 전 구간(2차 재질의 포함)을 봐야 한다.
- **prod 무영향:** 이 작업은 측정 전용. 캐스케이드 활성화는 이미 `ORTHUS_LLM_FALLBACK_MODEL`
  미설정이면 off라 prod 동작 불변. 측정은 로컬 company 노드에서.

---

## 8. 절차 (AGENTS.md 준수)

```bash
git worktree add -b exp/cascade-verify .worktrees/cascade-verify main
cp .env .worktrees/cascade-verify/.env    # 실행용, stage 금지
cd .worktrees/cascade-verify
uv sync --extra dev
# arm A/B/C 설정으로 e2_final_eval.py(또는 e1_vote.py) 재실행, 회수·오발동·비상관·비용 측정
```

- 결과는 `experiments/fugu-ko/analysis/`에 결과 문서로(예: `svc-cascade-verify-results.md`) 남기고,
  `docs/model-orchestration.md` §13.5의 "미검증" 경고를 실측으로 갱신.
- 코드 변경이 없다면(측정만) PR은 결과 문서만. 캐스케이드 로직을 손대면 회귀 테스트 필수.
- 커밋/푸시는 owner 요청 전까지 금지.

---

## 9. 참고 파일

| 파일 | 내용 |
|---|---|
| `orthus/structured/query.py` | SVC 캐스케이드 본체(트리거·모드·2차·채택) |
| `orthus/models/registry.py` `get_fallback_chat_model` | 2차 모델 슬롯 + 같은-모델 경고 |
| `orthus/models/orchestration.py` §주석 88-91 | SVC 2차=A.X 근거(오류 비상관) |
| `docs/model-orchestration.md` §13.5 + ⚠️⚠️ 블록 | E2 캐스케이드 측정 + "2차 잘못 뒀다" 자기감사(이 작업의 출발점) |
| `experiments/fugu-ko/e1_vote.py` · `e1_synth_eval.py` | E1 캐스케이드 하네스 |
| `experiments/fugu-ko/e2_final_eval.py` · `e2_e2e.py` | E2E 루프 하네스(2차=A.X로 재실행 대상) |
| `experiments/fugu-ko/golden/e2_v2x*.json` | zero-answer 문항(이모지 DB명 포함) |
| `experiments/fugu-ko/r1_tn.py` + TN 골든 | 진짜-0(TN) 케이스 — 오발동 측정용 |
| `docs/model-orchestration.md` §12.4 | SVC 2차=A.X 배정(Jaccard 0.24) |

> 실험 산출물(E1 결과·SVC 설계 문서 `structured-verification-cascade.md`)은 PR #733로 아카이브 중 —
> 머지되면 `experiments/fugu-ko/analysis/`에서 E1 원자료를 참고할 수 있다.
