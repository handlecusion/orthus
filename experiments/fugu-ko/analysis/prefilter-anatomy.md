# decompose 프리필터(Stage 1) 해부 — 수정 전 구조 파악

> 목적: DF 시리즈에서 "진짜 복합 질문 23개가 tier 3에서도 LLM 게이트에 도달하지 못한다"는
> recall 갭을 고치기 전에, `should_decompose` Stage 1의 정확한 제어흐름과 확장 지점을 라인
> 단위로 고정한다. **read-only 분석 — 코드 미수정.**
>
> 대상 코드: `orthus/router/decompose.py`(Stage 1/2), `orthus/settings.py:564`(티어 설정),
> `docs/decompose-prefilter-ext.md`(O4/E3 설계+실측), `orthus/router/event_orchestration.py`
> (신호 공유 소비자), `orthus/router/tools.py`+`orthus/router/route.py`(classify_intent — 별개
> 메커니즘), `experiments/fugu-ko/e3_prefilter.py`(측정 하네스).

---

## (a) `should_decompose` Stage 1 제어흐름 (텍스트 다이어그램)

```
should_decompose(question, chat_model=None, ext_tier=None)          [decompose.py:319]
│
├─ compact = question.lower().replace(" ", "")
├─ if not compact: return False                                     [빈 문자열 즉시 종료]
│
├─ tier = ext_tier if given else prefilter_ext_tier()                [settings clamp [0,3]]
│
├─ has_verb = _has_command_verb(compact)                             [decompose.py:233]
│     └─ agentwork/service.py::_COMMAND_VERBS 토큰 중 하나라도 compact에 포함?
│
├─ has_conn = _has_connective_or_enum(question, ext_tier=tier)       [decompose.py:272]
│     ├─ compact 재계산 (question.lower().replace(" ",""))
│     ├─ has_numbered = "1." in question and "2." in question
│     ├─ 기존 신호(byte-identical @ tier=0):
│     │     any(_CONNECTIVE_TOKENS in compact) OR
│     │     any(_ENUM_TOKENS in question) OR
│     │     has_numbered
│     └─ ext_tier > 0 이면 OR _has_ext_signal(question, compact, ext_tier)  [decompose.py:246]
│           └─ tier 1..ext_tier 누적 순회, 각 티어의 (compact_tokens, raw_tokens) 매칭
│              (병렬 조사는 관계어 있으면 억제 — 아래 (b)/(d) 참조)
│
├─ ══════ 조기종료 게이트 ══════
│  if not has_verb and not has_conn:
│      return False        ← **LLM 미호출.** Stage 2(LLM enum)에 절대 도달하지 않는다.
│
└─ (여기까지 왔으면 = has_verb OR has_conn 중 하나라도 True)
   ── Stage 2: LLM enum ──
   chat = chat_model or get_chat_model_for(TASK_DECOMPOSE)
   raw = chat.complete(_DECOMPOSE_SYSTEM, f"Question: {question}", json_only=True)
   verdict = json.loads(raw).get("decompose", "uncertain")   [예외 시 "uncertain"]
   return verdict == "yes"        ← "uncertain"/"no"는 여기서 False (LLM은 불렀음)
```

**핵심**: Stage 1은 딱 하나의 OR 조건이다 — `has_verb OR has_conn`. 이 둘 다 False일
때만 LLM 도달이 막힌다. 그 외에는 (설령 실제로 단일 질문이라도) 무조건 LLM까지 가고,
품질 최종 판정은 Stage 2 LLM이 담당한다(§4 dial: 프리필터가 넓게 통과시켜도 비용은
지연뿐 — `docs/decompose-prefilter-ext.md` §7 "FP의 실비용은 지연뿐").

---

## (b) 티어별 활성 신호 매핑 표

`_PREFILTER_EXT_TIERS`(decompose.py:108-115), 티어는 **누적**(tier=N → 1..N 전부 활성):

| 티어 | 신호(원문 표기) | 매칭 방식 | 비고 |
|---|---|---|---|
| (base, tier 무관) | `_COMMAND_VERBS`(agentwork/service.py:163) | compact 부분일치 | `has_verb` — 티어와 독립적으로 항상 체크 |
| (base, tier 무관) | `_CONNECTIVE_TOKENS` 7개(그리고/그다음/또한/동시에/그후/알려주고/해주고) | compact 부분일치 | 기존 신호, tier=0에도 항상 활성 |
| (base, tier 무관) | `_ENUM_TOKENS` 5개(①②③/첫째/둘째) + `"1."&"2."` 동시 존재 | 원문(question) 부분일치 | 기존 신호, tier=0에도 항상 활성 |
| **T1** (tier≥1) | compact: 각각·비교·차이·vs·둘다·모두알려 / raw: 셋째·④ | compact 또는 원문 | 저위험 — 단일 질문에 드묾 |
| **T2** (tier≥2) | compact: 이랑·뭐고·이고 / raw: `"랑 "`(공백 포함) | compact 또는 원문 | 중위험 — 조사·연결어미라 단일에도 출현 가능 |
| **T3** (tier≥3) | raw만: `"와 "`, `"과 "`(공백 포함) | 원문만 | 고위험 — 병렬 조사, 관계형 단일질문과 어휘로 구분 불가 → 관계어 억제 동반 |

`tier=0`(default)이면 `_PREFILTER_EXT_TIERS` 테이블은 아예 조회되지 않는다 — base 신호만.
`tier=3`이 현재 유일하게 "채택"된 값(§5 판정, `docs/decompose-prefilter-ext.md` §7)이지만
**프로덕션 default는 여전히 0**이고 `ORTHUS_DECOMPOSE_PREFILTER_EXT_TIER` 환경변수로만 올라간다
(operator-gated, off까지 fail-closed).

**T3 전용 억제 레이어** (decompose.py:118-135, `_has_ext_signal` 내부 `fires()` 클로저):
- `_PARALLEL_PARTICLES = {"이랑", "랑 ", "와 ", "과 "}` — **T1/T2/T3 모든 조사류 토큰**이 억제
  대상 집합에 들어 있다(T1의 어휘 신호인 각각/비교/차이/vs/둘다/모두알려/셋째/④는 이 집합에
  없어 억제되지 않음).
- `_RELATIONAL_SUPPRESS = ("관계", "연결", "연관", "사이")` — compact에 이 중 하나라도 있으면,
  위 조사 토큰의 발화를 막는다("문어" 하드코딩 예외는 없음; "관련"은 의도적으로 제외 — 회사
  고유명사 "AI관련툴"과 충돌해 누락형 1건을 잃었던 실측 때문).
- 즉 판정식은 티어별로 **토큰별** 적용: `fires(token) = token in haystack AND NOT (relational AND token in _PARALLEL_PARTICLES)`.

---

## (c) 조기종료(LLM 미도달) 조건 목록

Stage 1에서 `return False`가 일어나 **Stage 2 LLM 호출 자체가 발생하지 않는** 경로는 딱 2곳:

1. **빈 입력** (decompose.py:336-337): `compact`가 빈 문자열이면 즉시 False.
2. **신호 완전 부재** (decompose.py:344-346): `has_verb`(명령 verb 없음) **AND**
   `has_conn`(접속/열거/확장신호 없음, 현재 활성 티어 기준)가 **동시에** False일 때.
   - 이것이 O4가 지목한 실제 원인이다: tier 0에서는 "랑/이랑/각각/비교/와·과" 류 어휘가
     `_CONNECTIVE_TOKENS`/`_ENUM_TOKENS`/`_COMMAND_VERBS` 어디에도 없어 이 조건에 걸린다.
   - T3까지 켜도 여전히 여기 걸리는 잔여 사례들(F1/F2/F4/F5족, `docs/decompose-finetune-plan.md`
     §7) — 병렬 조사도 명령 verb도 없는 표현(예: "설계와 구현 원칙을 담은 문서를 찾아줘" —
     "과/와"가 있어도 관계어 억제나 다른 어휘 함정에 걸리거나, 애초에 조사 자체가 다른 형태소로
     흡수된 경우) — DF 시리즈가 지목한 "23개 미도달" 갭은 이 경로의 잔여분이다.
   - **관계어 억제도 이 조건에 기여할 수 있다** — T3 병렬 조사가 있어도 `_RELATIONAL_SUPPRESS`에
     걸리면 `fires()`가 False를 반환하므로, 결과적으로 `has_conn`이 False가 되어 조기종료될 수
     있다(단 이건 의도된 동작 — 관계형 단일 질문을 KG graph route로 보내기 위함).

Stage 1을 통과한 뒤(LLM은 호출됨) False가 되는 경로는 조기종료가 **아니다** — 별도로 기록:
- `verdict != "yes"`(no/uncertain) 또는 `chat.complete`/`json.loads` 예외 → `verdict="uncertain"`
  → 결국 `should_decompose` False. 이건 LLM에 **도달은 했지만** 부정 판정된 경우이므로 recall
  갭의 원인이 아니다(LLM이 실제로 봤다).

---

## (d) 공유 토큰셋 격리 제약 — 수정 시 절대 건드리면 안 되는 것

`_CONNECTIVE_TOKENS`/`_ENUM_TOKENS`(decompose.py:72-87)는 **세 개의 서로 다른 모집단**이 함께
읽는 단일 소스다:

| 소비자 | 파일:라인 | 호출 방식 |
|---|---|---|
| `should_decompose` (본 분석 대상) | decompose.py:343 | `_has_connective_or_enum(question, ext_tier=tier)` — tier 명시 전달 |
| `command_split_signal` | decompose.py:289-297 | `_has_connective_or_enum(question)` — **ext_tier 인자 없음 → default 0 고정** |
| `mail_has_orchestration_signal` | event_orchestration.py:63-75 | 내부에서 `_has_command_verb`/`_has_connective_or_enum(text)`를 **인자 없이** 호출 → default 0 고정 |

제약의 실체:
- **`_CONNECTIVE_TOKENS`/`_ENUM_TOKENS` 상수 자체를 직접 늘리면 안 된다.** 이 두 상수는 세 소비자
  모두가 무조건 보는 base 신호라서, 여기 토큰을 추가하면 `command_split_signal`과
  `mail_has_orchestration_signal`의 모집단도 즉시 바뀐다(= 두 기능의 flag-off byte-identical
  계약이 깨짐 — 각각 `ask_command_split_enabled`/`ask_event_orch_enabled` 기능의 회귀 보증).
- 확장은 반드시 **별도 티어 테이블**(`_PREFILTER_EXT_TIERS`)에 넣고, `_has_connective_or_enum`의
  `ext_tier` 키워드 인자로만 노출한다. **`should_decompose`만 이 인자에 실제 티어값을 넘긴다**
  (decompose.py:343, `ext_tier=tier`). 다른 두 소비자는 인자를 넘기지 않아 default 0 → 절대
  영향받지 않는다.
- 이 불변식은 `tests/unit/test_decompose_prefilter_ext.py::TestPopulationIsolation`(라인 101-)이
  고정한다 — `prefilter_ext_tier()`를 monkeypatch로 MAX까지 올려도
  `command_split_signal`/`mail_has_orchestration_signal`이 여전히 False임을 검증. **수정 작업이
  새 신호를 추가한다면 이 테스트가 여전히 통과해야 하고, 통과하려면 새 신호도 티어 테이블 안에만
  넣고 두 소비자의 호출부는 절대 건드리지 않아야 한다.**
- 부수 제약: `should_decompose`의 `_has_command_verb` 체크는 티어와 무관하게 항상 base다 —
  `_COMMAND_VERBS` 확장도 같은 공유 문제를 가진다(agentwork/service.py:2314에서
  `_detect_command_family`도 같은 상수를 쓴다). `docs/decompose-prefilter-ext.md` §6 "스코프 밖"이
  `_COMMAND_VERBS` 확장을 명시적으로 제외했다.

---

## (e) 수정 후보가 될 만한 확장 지점

1. **`_PREFILTER_EXT_TIERS`에 새 티어(T4) 또는 기존 티어에 토큰 추가** (decompose.py:108-115) —
   가장 직접적인 지점. `_PREFILTER_EXT_MAX_TIER = max(_PREFILTER_EXT_TIERS)`가 자동으로 갱신되므로
   새 키만 추가하면 clamp 로직(`prefilter_ext_tier()`, `_has_ext_signal`)은 그대로 동작한다.
   F1(병렬 명사구 단일 의도)/F2(관계형)/F4(분배 수량사)/F5(주제 접속 수식) 같은 잔여 함정족은
   순수 토큰 매칭으로는 어휘와 의미가 겹쳐 위험도가 T3보다 높을 수 있음 — 새 티어 T4로 격리해
   측정하는 편이 안전(기존 T1-T3 실측 재현 없이 신호만 추가).
2. **`_has_ext_signal`의 억제 로직 확장** (decompose.py:246-269) — 현재는
   `_RELATIONAL_SUPPRESS` 단일 카테고리만 병렬 조사를 억제한다. F4(분배 수량사: "각각의 X"가
   실제로는 단일 집계)나 F5(주제 접속 수식: "A와 B를 담은 문서") 같은 새 억제 카테고리를 같은
   패턴(`fires()` 클로저에 추가 카테고리)으로 넣을 수 있다 — **단, T1 어휘 신호(각각/비교/…)
   자체가 F4의 트리거이므로, "각각"을 억제하려면 지금처럼 "특정 토큰군만" 억제하는 게 아니라
   해당 어휘 신호 자체에 새 조건을 걸어야 함**(현재 설계는 조사 토큰만 억제 대상 — 어휘 신호는
   무조건 발화).
3. **`_has_command_verb`/`_COMMAND_VERBS` 확장** (agentwork/service.py:163) — 스코프 밖으로
   명시돼 있었지만(§6), 만약 조사/접속 신호가 아니라 새로운 명령 동사가 원인이라면 이쪽도 후보.
   단 이건 `command_split_signal`/`mail_has_orchestration_signal`뿐 아니라
   `_detect_command_family`(agentwork/service.py:2314)까지 공유하는 **더 넓은** 공유 상수라
   리스크가 `_CONNECTIVE_TOKENS`보다 크다.
4. **형태소 분석기 도입** (`docs/decompose-prefilter-ext.md` §6에서 "옵션 C, T3 실패 시 재검토"로
   미룬 항목) — 토큰 매칭의 구조적 한계(조사가 다른 단어에 흡수되거나, "과"가 "결과"/"성과"에
   걸리는 식의 어휘적 오검출, §3)를 근본적으로 풀려면 필요하지만 지금은 스코프 밖으로 명시
   보류돼 있다. 새 잔여 갭(F1/F2/F4/F5)이 순수 토큰 확장으로 충분히 안 닫히면 이 옵션이
   재검토 대상이 된다.
5. **측정 하네스 확장** (`experiments/fugu-ko/e3_prefilter.py`) — 새 티어/억제를 추가하면
   `--max-tier`를 늘려 스윕 범위를 넓히고, 새 golden(F1/F2/F4/F5 함정 포함)을 `golden/` 아래
   추가해 실측해야 한다(하네스 자체는 `reached()`/`GateCache`로 프로덕션 코드를 직접 호출하므로
   로직 재구현 없이 새 티어를 바로 측정 가능 — 코드 수정 지점이 아니라 검증 지점).

---

## 참고 — `classify_intent`는 별개 메커니즘 (혼동 주의)

`should_decompose`의 Stage 1 프리필터는 **"이 질문 전체를 쪼갤지 말지"**를 판정하는 게이트다.
반면 `router.classify_intent`(route.py:281, `router/tools.py:60` `classify_subpart`를 통해서만
호출)는 **decompose가 이미 결정되어 split된 이후**, 개별 sub-question 하나하나를
`structured`/`wiki`/`graph`/`action-intake` 중 어디로 보낼지 정하는 완전히 다른 단계다
(`_run_leaf`, decompose.py:786에서 leaf마다 호출). 두 메커니즘은 신호 집합도 다르고
(`should_decompose`는 접속/열거/명령verb 토큰, `classify_intent`는 route enum LLM +
`_detect_command_family` 결정론 fast-path) 실행 시점도 다르다(전자가 whole-question 게이트,
후자가 per-leaf 분류기) — 프리필터 recall 갭 수정은 `classify_intent` 쪽을 건드릴 필요가 없다.
