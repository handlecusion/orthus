# 핸드오프 — distill을 Solar로 이전하고 GPT 단일 슬롯 걷어내기

> 상태: **A/B 코드 완료 + C 로컬 실측 검증 완료 (2026-07-15). prod C(운영 `ORTHUS_LLM`
> 전환)만 owner 잔여.** distill이 프로덕션에서 유일하게 GPT 기본 슬롯에 남아 있던 작업이라,
> 이걸 옮기면 **시스템에서 GPT를 완전히 뺄 수 있다.**
>
> **완료 요약:**
> - **A(상한 해제):** `orthus/wiki/distill.py` `_SYSTEM`에 전수 추출 지시 + `Return at most
>   20 claims`, `_MAX_CLAIMS=20`(두 상한 함께). `tests/unit/test_wiki_distill.py`가 프롬프트
>   상한과 코드 절단이 다시 어긋나지 않게 고정.
> - **B(배정):** `orchestration.py`에 `TASK_DISTILL="distill"` + `ASSIGNMENTS[...]="solar"`,
>   distill 호출부 2곳(`wiki/distill.py`·`connectors/base.py`)이
>   `get_chat_model_for(TASK_DISTILL)`로 전환. 프로덕션 `get_chat_model()` 직접 호출부 0곳.
> - **C 로컬 실측(orthus_company 노드, ORTHUS_LLM=solar + orchestration on + Solar 키):**
>   `get_chat_model_for(distill)` → `FallbackChat(model_id=solar-pro)` — **GPT 미호출 증명**.
>   실 company 문서 5건 distill 평균 **7.4 클레임/문서**(기준선 7.2 초과, 상한 20 미도달),
>   첫 클레임 근거가 원문과 정확히 일치(지어냄 없음). 5건 표본이라 T14의 25건 8.4에는 못
>   미치지만 기준선을 넘고 배선을 실증한다.
> - **prod C 잔여:** 아래 §4-C 순서를 owner가 prod `node.env`에서 실행(코드 변경 0).

---

## 0. 한 줄 요약

`distill`(위키 저작)을 Solar로 배정하고, 지금 GPT를 가리키는 **기본 슬롯(`ORTHUS_LLM`)을
국내 모델로 바꿔** 프로덕션에서 GPT를 제거한다. **선결 조건은 distill 프롬프트의 클레임
상한 해제**(안 하면 커버리지가 4.8로 떨어짐 — T14).

---

## 1. 배경 (왜 지금)

- 작업별 모델 배정에서 **`distill`만 미배정**으로 남아 GPT 기본 슬롯을 쓰고 있었다. 이유는
  "성능"이 아니라 **커버리지 트레이드**였다: Solar distill은 오염(없는 사실 지어냄) **0%**(40/40
  supported)로 GPT와 동등하고 1.8배 빠르지만, 문서당 클레임을 4.8개만 뽑아(GPT 7.1) 위키가
  얇아진다고 봤다(`docs/model-orchestration.md` §12.3).
- **T14(§13)에서 그 전제가 깨졌다.** 커버리지 열세는 모델 특성이 아니라 **프롬프트 상한**
  때문이었다 — 프롬프트에 `Return at most 8 claims`만 있고 **하한 지시가 없어** GPT조차 25문서
  중 14건(56%)이 상한에 잘리고 있었다. 상한을 풀면 **Solar 8.4 > 현행 프로덕션 7.2**로 역전
  (정밀도 100%, 지어냄·모순 0). 즉 **트레이드는 소멸했고, distill은 Solar로 옮길 수 있다.**
- distill은 **토큰 지출을 지배**하고(문서마다 1회 × 전 임포트) 다른 모든 작업이 읽는 위키를
  만들므로, 여기를 국산으로 옮기는 게 "GPT 벤더 제거"의 마지막 조각이다.

---

## 2. "단일 슬롯"이 지금 무엇인가 (그냥 못 지우는 이유)

기본 슬롯 = `get_chat_model()`(= `ORTHUS_LLM`, 현재 `openai`/`gpt-4o-mini`). 이건 **4가지 역할**을
겸한다. 그래서 "코드에서 삭제"가 아니라 **가리키는 모델을 바꾸고 distill 의존을 끊는 것**이
정답이다:

| 역할 | 설명 | 이번 작업에서 |
|---|---|---|
| ① distill 백엔드 | 유일하게 배정 안 된 프로덕션 작업이 여기 탐 | **← 끊는다 (Solar로 배정)** |
| ② 최후 폴백 | 배정 워커·국내 백업이 다 실패하면 마지막 rung (`FallbackChat`) | 유지(모델만 국내로) — §5 결정 |
| ③ 플래그 off 동작 | `ORTHUS_MODEL_ORCHESTRATION_ENABLED` off면 모든 작업이 기본 슬롯으로 | 유지 (기계 보존) |
| ④ mock 테스트 핀 | `ORTHUS_LLM=mock`이면 결정론·오프라인 (CI 고정) | **반드시 유지** |

→ `get_chat_model()` 자체는 지우지 않는다. **`ORTHUS_LLM`을 국내 모델로 바꾸면** ①(distill 배정
후 flag-off 경로 포함)·②·③에서 GPT가 사라지고, ④(mock)는 그대로 산다.

---

## 3. 정확한 현재 코드

### distill 호출부 (2곳 — 둘 다 `get_chat_model()` = 맨 기본 슬롯)
- `orthus/wiki/distill.py:286` → `chat = chat_model or get_chat_model()`
- `orthus/connectors/base.py:169` → `chat = chat_model or get_chat_model()`  (임포트 병렬 경로의 distill 진입점, 같은 작업)

### 프롬프트 상한 (선결 해제 대상)
- `orthus/wiki/distill.py:49-50` → 시스템 프롬프트 `"Return at most 8 claims."` (하한 지시 없음)
- `orthus/wiki/distill.py:315` → `for c in raw_claims[:_MAX_CLAIMS]` (코드 절단, `_MAX_CLAIMS`=8)
- **둘 다 함께 풀어야 한다** — 하나만 풀면 다른 하나가 여전히 자른다(T14 arm 설계).

### 배정 테이블 (`orthus/models/orchestration.py`)
- `TASK_*` 상수: line 30–40. **`TASK_DISTILL`은 존재하지 않는다** — 추가 필요.
- `ASSIGNMENTS` dict: line ~103–113 (11작업 배정, 전부 solar 외 delegation_extract=exaone).
- line 116 주석: "`distill` is deliberately NOT here … the ONLY production task left on the bare default slot".
- `get_chat_model_for(task)`: 배정 있으면 `FallbackChat`(배정워커 → 국내백업 → 기본슬롯), 없으면
  기본 슬롯. **flag off / `ORTHUS_LLM=mock` / 미배정이면 기본 슬롯**을 그대로 반환.
- `_BACKUP = {"solar": "exaone", "exaone": "solar"}` — 국내 백업 사다리.

---

## 4. 할 일 (순서대로)

### (선결) A. 프롬프트 상한 해제 — 이거 없이 옮기면 커버리지 −40%
`orthus/wiki/distill.py`:
- 프롬프트 `Return at most 8 claims` → 상한을 넉넉히(예: 20) + **하한/전수 지시 추가**
  ("extract **all** verified atomic claims", 빠뜨리지 말 것). T14 arm P1 문구 참고.
- `_MAX_CLAIMS` 절단 상향(또는 제거) — 프롬프트와 **함께** 풀어야 유효.
- 이건 모델 무관 개선이라 **먼저 단독 PR**로 내도 좋다(현행 GPT도 7.2→더 뽑음).

### B. distill을 Solar로 배정
`orthus/models/orchestration.py`:
- `TASK_DISTILL = "distill"` 상수 추가.
- `ASSIGNMENTS[TASK_DISTILL] = "solar"` 추가 (주석의 "distill은 미배정" 문단 갱신).

`orthus/wiki/distill.py:286` + `orthus/connectors/base.py:169`:
- `get_chat_model()` → `get_chat_model_for(TASK_DISTILL)` 로 전환.
- ⚠️ distill은 대량 저작이라 폴백 사다리(exaone 백업 + 기본슬롯)를 태울지, 아니면 실패 시
  그냥 에러낼지 결정할 것. 현행 다른 작업은 `FallbackChat`을 타므로 일관성상 그대로 두는 게
  기본. (bulk authoring "no fallback" 언급이 orchestration.py 주석에 있으니 확인.)

### C. GPT 제거 — 기본 슬롯을 국내 모델로 (운영 설정, 코드 0) — prod runbook

로컬 company 노드에서 아래 순서를 실측 검증했다(위 상태 헤더). prod는 같은 순서를 owner가
prod `node.env`에서 실행한다:

1. **Solar 키 provisioning** — `ORTHUS_LLM_SOLAR_API_KEY=<upstage key>`. (키가 없는 상태로
   3을 하면 fail-closed로 distill이 죽으므로 **반드시 선행**.)
2. **`ORTHUS_MODEL_ORCHESTRATION_ENABLED=true`** — distill이 `FallbackChat(solar → exaone →
   기본슬롯)`을 탄다. GPT는 아직 최후 rung으로만 남음.
3. **`ORTHUS_LLM=solar`** — 최후 폴백(②)·flag-off(③)·기본 슬롯 전반에서 GPT 소멸.
   `ORTHUS_LLM=mock`(④)은 그대로 mock — 테스트 불변.
4. API 재기동(`make redeploy` 또는 launchctl kickstart). 출력 토큰 3.6배(비용) 인지.

- 이건 **운영 게이트**(owner)다. 코드 변경 0. `docs/model-orchestration.md` §12.4 노트 참고.
- **로컬 실측 명령(참고):**
  ```bash
  ORTHUS_PG_DSN=postgresql+psycopg://orthus:orthus@localhost:5433/orthus_company \
  ORTHUS_LLM=solar ORTHUS_LLM_SOLAR_API_KEY=<key> \
  ORTHUS_MODEL_ORCHESTRATION_ENABLED=true ORTHUS_EMBEDDING=mock \
  python -c "from orthus.models.orchestration import *; m=get_chat_model_for(TASK_DISTILL); print(type(m).__name__, m.model_id)"
  # → FallbackChat solar-pro  (GPT 아님을 즉시 확인)
  ```

---

## 5. Owner / 설계 결정 지점 (2026-07-15 결정 완료)

> 아래 3건은 사용자 확정: (1) 최후 폴백 **유지**(FallbackChat), (2) distill 기본 모델
> **Solar**, (3) `get_chat_model()` 코드 **유지**. 원안 논거는 기록용으로 남긴다.


1. **최후 폴백(②)을 남길 것인가.** 지금은 "국내 벤더 전면 장애 시 마지막 rung = 기본 슬롯"
   이다(`FallbackChat`). `ORTHUS_LLM=solar`로 두면 그 rung도 Solar라 **벤더 금지와 안 부딪힌다**
   → **남기는 것을 권장**(가용성 backstop 유지, GPT는 이미 사라짐). 폴백 자체를 없애면 국내
   전면 장애 시 distill이 fail-closed로 죽는다 — 가용성 트레이드, owner 판단.
2. **distill 기본 모델을 무엇으로.** Solar 권장(측정된 유일 후보 — A.X는 3/5 실패·165초 실격,
   EXAONE 부적합). `ORTHUS_LLM`도 solar로 맞추면 배정 on/off 무관하게 일관.
3. **`get_chat_model()` 코드 자체는 유지.** ③④ 기계가 여기 의존하므로 삭제 금지. "단일 슬롯
   제거"는 **삭제가 아니라 GPT 제거 + distill 의존 끊기**로 해석한다.

---

## 6. 측정 (재현 + 검증)

**하네스** (`experiments/fugu-ko/`, `docs/model-orchestration.md` §12.3·§13):
```bash
cd experiments/fugu-ko
python t14_distill_cap.py   # P0(현행 프롬프트) vs P1(상한 해제) — 문서당 클레임/상한붙음/정밀도
python t11_distill.py       # distill 25문서 × 4모델 (읽기전용, 실 company DB)
python t11_judge.py         # 환각 판정 — 원문을 읽은 판정자
```

**기준선 (T14, §13.1):**

| arm | 모델 | 문서당 클레임 | 상한에 붙음 | 정밀도(supported) | p50 |
|---|---|---|---|---|---|
| P0 (현행) | gpt-4o-mini | 7.2 | 14/25 (56%) | 97.5% | 13.4s |
| **P1 (상한 해제)** | **solar** | **8.4** (+96%) | 0/25 | **100%** | 9.3s |

**성공 기준:**
- 상한 해제 후 Solar distill: **오염 0% 유지** + 문서당 클레임 **≥ 현행 7.2** + 정밀도 ~100%.
- distill 결과가 배정 전환 후에도 동일 품질(회귀 없음).
- `ORTHUS_LLM=mock`으로 전체 테스트 무회귀(테스트 핀 보존 확인).

---

## 7. 가드레일 / 주의

- **redaction·audit 불변:** distill은 wiki 저장 전 redaction 경로를 그대로 타야 한다(P6/P8
  ingest carve-out 외에는 유지). `audit("...")` span도 유지.
- **mock 테스트 핀 보존(④):** `get_chat_model_for`가 `ORTHUS_LLM=mock`이면 기본 슬롯을 반환하는
  분기를 깨지 말 것 — 이게 깨지면 로컬 .env에 국내 키가 있는 개발자의 테스트가 실제 API를
  네트워크로 부른다(현행 주석 명시).
- **flag-off 동작(③):** 배정 플래그가 off여도 distill이 GPT를 안 보게 하려면 §4-C(운영설정)가
  필요하다 — 코드만 바꾸고 `ORTHUS_LLM`을 안 바꾸면 flag off 시 여전히 GPT.
- **선결 순서 엄수:** 상한 해제(A) → 배정(B) → 운영설정(C). A를 건너뛰고 B/C만 하면 위키
  커버리지가 4.8로 떨어진다.
- **대량 재저작 비용:** 이미 GPT로 저작된 위키를 Solar로 다시 컴파일할지(재구축)는 별건이다.
  `make node-kg-rebuild`/wiki rebuild는 `--clean --concurrency N` 규칙 준수
  (내부 문서(비공개) 취지 — orphan 누적·장시간 방지).
- **⚠️ 알려진 리스크(미조치) — distill JSON 출력 절단:** 상한을 8→20으로 올리고 "모든
  클레임 추출"을 지시하면서 distill 요청에 `max_tokens`가 설정돼 있지 않다. 긴 문서가 진짜
  ~20 클레임을 내면 응답이 provider 기본 출력 상한을 넘어 JSON이 중간에 잘리고,
  `_complete_distill_json`이 3회 재시도 내내 동일 실패 후 `wiki_distill_invalid_json`을
  raise할 수 있다(단일 publish 경로는 예외 전파). **실측상 리스크는 낮다** — solar-pro로
  가장 긴 회사 문서 6건 측정 시 completion_tokens **최대 2,107**(클레임 8–10, 상한 20 미도달),
  T14 상한 20 × 25문서에서도 절단 실패 0건. 20 클레임 최악 케이스 추정 ~4,000–4,500 토큰.
  조치한다면 **distill 호출에만** `max_tokens≈8192`를 배선할 것 —
  `OpenAIChat.complete()` `body`에 전역으로 넣으면 routing/structured 등 같은 어댑터를
  쓰는 모든 작업에 상한이 걸린다. `ChatModel.complete` 프로토콜에 `max_tokens` 선택 인자가
  없어 배선이 필요하므로 이번 변경 범위 밖으로 둔다.

---

## 8. 작업 절차 (AGENTS.md 준수)

```bash
git worktree add -b feat/distill-solar .worktrees/distill-solar main
cp .env .worktrees/distill-solar/.env    # 실행용, stage 금지
cd .worktrees/distill-solar
uv sync --extra dev
# A(상한) → B(배정+호출부) 코드 수정, 테스트, t14/t11 재측정
make test    # ORTHUS_LLM=mock 무회귀 확인
```

- 회귀: `tests/unit/test_model_orchestration.py`(배정/폴백/mock 핀) 무회귀 + distill 관련 테스트.
- PR: 제목 `[MO] distill Solar 배정 + 프롬프트 상한 해제` + `.github/pull_request_template.md`
  (Risk/Protected Area/QA). `make pr T="..."` 권장. §4-C(운영 `ORTHUS_LLM` 변경)는 **코드 PR과
  분리된 operator 단계**임을 PR 본문에 명시.
- 커밋/푸시는 owner 요청 전까지 금지.

---

## 9. 참고 파일

| 파일 | 내용 |
|---|---|
| `orthus/wiki/distill.py` (49-50, 315, 286) | 프롬프트 상한 + 절단 + distill 호출부 |
| `orthus/connectors/base.py` (169) | 병렬 임포트 경로 distill 호출부 |
| `orthus/models/orchestration.py` (30-40, 103-116, 201-) | TASK 상수·ASSIGNMENTS·`get_chat_model_for`·`FallbackChat` |
| `docs/model-orchestration.md` §12.3 · §12.4 · §13 | distill 측정·최종 배정·T14 상한 재측정 (SoR) |
| `experiments/fugu-ko/t14_distill_cap.py` | 상한 해제 P0/P1 하네스 |
| `experiments/fugu-ko/t11_distill.py` · `t11_judge.py` | distill 4모델 측정 + 환각 판정 |
