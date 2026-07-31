# T4 프리필터 신호 — 설계·실측·판정

> decompose 프리필터(Stage 1) recall 갭 중 **"-고" 접속어미로만 이어붙인 순수 지식
> 병렬질문**(진단 §3 모드 A/C)을 닫는 T4 신호를 프로토타입·실측한다. **프로덕션 미머지 —
> `feat/prefilter-t4-recall` worktree에만 있다.** LLM 0회(Stage 1 결정론)·DB 0회.
>
> 코드: `orthus/router/decompose.py`(T4 predicate + 억제 버그픽스), `orthus/settings.py`(티어
> 주석), 테스트 `tests/unit/test_decompose_prefilter_ext.py`, 하네스
> `experiments/fugu-ko/t4_prefilter_measure.py`. 선행 진단은
> `analysis/prefilter-anatomy.md` / `analysis/prefilter-gap-diagnosis.md`.

---

## 1. 버그 수정 — 억제어 부분일치 (correctness)

`_has_ext_signal`의 관계어 억제는 `any(x in compact for x in _RELATIONAL_SUPPRESS)`로
**공백 제거 문자열 부분일치**였다. 그래서 회사 고유명사 "디아더사이드"의 "사이"가 관계어로
잡혀 병렬 조사("복구**랑** …") 신호를 통째로 무효화했다(진단 모드 E, c34 — 실제 복합질문이
tier3에서도 미도달).

**수정**: `_has_relational_term(lowered)` 신설 — 억제어가 **양쪽 모두 한글 음절**로 둘러싸인
순수 단어-내부 매칭이면 관계 표지로 보지 않는다(최소 한쪽 경계 = 공백/조사/문장부호/문자열
끝). "무슨 관계야"(공백 경계)·"문서들 사이의"(공백 경계)·"연결된"(공백 경계)은 그대로
관계어로 잡히고, "디아더사이드"의 "사이"(양쪽 한글)만 걸러진다.

효과(같은 코드, bug fix on/off 대조):

| | 26-probe q_original tier3 recall | e3_control tier3 오발화 |
|---|---|---|
| main (버그) | 8/26 (30.8%) | 25/40 |
| bug fix | **9/26 (34.6%)** | **25/40 (불변)** |

즉 순수 correctness 이득이다 — c34(진짜 복합질문) 1건을 recall로 되찾으면서 대조군 precision
비용은 0. tier 0 경로는 `_has_ext_signal`을 아예 타지 않으므로 **default 동작 byte-identical**.

---

## 2. T4 설계 규칙 — "-고" co-occurrence

"-고"는 한국어 연결어미로 극히 흔해("이거 처리하고 확인해줘"), 순진한 토큰 매칭은 precision을
붕괴시킨다. 그래서 T4는 토큰이 아니라 **결정론 predicate**(`_has_t4_go_signal`)다:

**절경계 "-고"**(바로 뒤 공백/쉼표/마침표) 중 하나라도 다음을 **모두** 만족하면 발화:

1. **보조용언 활용 제외** — "-고" 다음 어절이 `싶/있/계시/나서/나면/보니/보면/자/서/말/드리/봐/볼/나가`로
   시작하면 단일 절이다("높이**고 싶**다", "진행하**고 있**다", "회의하**고 나서**"). 스킵.
2. **고-종결 명사 제외** — 그 "고"가 단독 명사("사고/보고/광고/신고/참고/…")의 끝이면
   접속어미가 아니다("**사고** 원인이 뭐야"). 명사가 앞 경계(비한글)로 단독일 때만 차단하므로
   "알아**보고**,"(=알아보다+고, 동사) 같은 활용은 오차단되지 않는다.
3. **후행 절이 독립 요청 구조** — "-고" 뒤 꼬리절에
   - 의문사/의문 어미(`뭐/무엇/무슨/어때/어떻/어디/얼마/누구/몇/왜/언제/…지`),
   - 또는 종결 의문(`?`, `…야/어/까/나요/…`),
   - 또는 **순차 지시어**(`그/거기/다음/이후/해당` — 모드 C "…찾**고, 그** 사람이 …")
   가 있어야 한다. 요청/명령형 종결(`줘/세요`)은 **의도적으로 뺐다** — 그건 command-split
   소관이고, 여기는 순수 질문 병렬만 본다.

구현은 `_PREFILTER_EXT_TIERS[4] = ((), ())` 빈 엔트리 + `_has_ext_signal`의 `t == _T4_TIER`
특수 분기로, MAX_TIER 자동 갱신·clamp·누적 로직을 그대로 재사용한다. **공유 base 상수
(`_CONNECTIVE_TOKENS`/`_ENUM_TOKENS`) 무수정** → `command_split_signal`/
`mail_has_orchestration_signal` 모집단 불변(`TestPopulationIsolation` 통과).

---

## 3. Recall — tier3 → tier4 (LLM 0회)

**26-probe q_original**(진단이 복원한 "각각/그다음 삽입 전" 원표현, 모드별):

| mode | 성격 | n | tier3 | tier4 |
|---|---|---|---|---|
| A | "-고" 병렬 + 후행 의문 | 9 | 0/9 (0.0%) | **9/9 (100%)** |
| C | "-고," + 순차 지시어 "그" | 5 | 0/5 (0.0%) | **5/5 (100%)** |
| B | 쉼표 열거 + 공유 서술어 | 2 | 0/2 | 0/2 |
| D | 비유창 이중문장("아울러") | 1 | 0/1 | 0/1 |
| E | 고유명사 부분일치(버그) | 1 | 1/1 (버그픽스) | 1/1 |
| **ALL** | | 26 | **9/26 (34.6%)** | **23/26 (88.5%)** |

- T4 타깃인 **모드 A+C 14문항이 0% → 100%**. tier3 전체 34.6% → tier4 88.5% (+53.9%p).
- tier4 잔여 미도달 3건 = c05/c08(모드 B 쉼표 열거), c33(모드 D "아울러" 비유창). 둘 다 "-고"
  절경계가 없어 T4 범위 밖이다(§5 참조).

**e3_missed.json**(n=40, 기존 확장 티어 recall 셋): tier3 40/40 → **tier4 40/40** — T4는
누적이라 하위 티어 신호를 그대로 통과시켜 **회귀 0**.

---

## 4. Precision — 오발화(프리필터 오통과) tier3 → tier4

Stage 1 통과 = LLM 게이트에 위임이므로, 오발화의 실비용은 **최종 오답이 아니라 LLM 콜 1회
지연**이다(진짜 오분해는 뒤의 LLM 게이트가 거른다). 낮을수록 좋다.

| 대조군 | n | tier3 오발화 | tier4 오발화 | **T4 증분** |
|---|---|---|---|---|
| e3_control (adversarial 단일질문) | 40 | 25/40 (62.5%) | 25/40 (62.5%) | **+0** |
| t7 compound=false | 6 | 1/6 | 1/6 | **+0** |
| 자연 분포 (t5 + t7 단일, e3 채택 지표) | 27 | 2/27 (7.4%) | 2/27 (7.4%) | **+0.0%p** |

- **T4가 새로 흘리는 오발화는 어느 대조군에서도 0건**이다. e3_control의 62.5%는 기존 T1–T3
  adversarial 기저(차이/비교/이랑/…)이지 T4가 아니다.
- e3 문서가 채택 판정에 쓰는 **자연 분포 오통과 증분은 +0.0%p** — T3와 동일하게 유지된다.

---

## 5. 홀드아웃 확인 (1회, 튜닝 미사용)

`t7_holdout2` **trap arm 60**(H2, 전부 단일질문 함정 — 모델 실험용 동결셋, 신호 튜닝에
미사용):

| | tier3 오발화 | tier4 오발화 | T4 증분 |
|---|---|---|---|
| H2 trap (n=60) | 48/60 (80.0%) | 48/60 (80.0%) | **+0** |

미접촉 홀드아웃 함정 60개에서도 T4 신규 오발화 0. (80% 기저는 이 arm이 확장 티어 토큰을
일부러 품은 adversarial 함정이라 그런 것이고, T4와 무관하며 T4는 여기에 아무 것도 더하지
않는다.)

---

## 6. 테스트

- 기존 `tests/unit/test_decompose_prefilter_ext.py` **전 42종 통과 유지**(flag-off byte-identical,
  티어 누적·clamp, `TestPopulationIsolation`, `TestRelationalSuppression` 포함).
- 신규 T4 유닛(같은 파일): `TestSuppressionBoundaryBugfix`(고유명사 부분일치·진짜 관계어·c34),
  `TestT4GoConnective`(모드 A/C 양성 6종, 단일 "-고"·고-종결 명사 음성 10종, 누적성, MAX=4).
  → 합계 **74종 전부 통과**. (DB 미사용 순수 유닛; 실행은 `ORTHUS_PG_DSN=…orthus_test`로
  임포트 가드 회피.)
- 인접 스위트(`test_ask_decompose*`, `test_command_split*`, `test_model_orchestration`)의
  실패는 **로컬 env의 psycopg2 미설치**(main 체크아웃에서도 동일 재현)로 본 변경과 무관.

---

## 7. 판정 — owner 게이트 승격 권고: **O (권고함)**

> **⚠️ 정정(2026-07-19, `analysis/prefilter-t4-realtraffic.md`):** 아래 "precision 비용
> +0.0%p"는 **큐레이션 대조군(e3_control/t7/H2, 27~60건)만의 수치이며 착시였다.** company
> `query_runs` 실트래픽(distinct 1197, 3주치) 재측정 결과 자연 오통과 증분은 **+2.51%p**였다
> (misfire 27/30 = 90.0%가 `_T4_NOUN_GO` 명사-예외 공백 — 특히 "회고" 63%). 이후
> 회고/로고/공고/충고/재고 5개를 `_T4_NOUN_GO`에 추가하는 후속 패치로 **+0.67%p**까지
> 낮췄다(신규발화 30→8건, 73.3% 감소). **"최종 오분해율 0%"(Stage2 LLM 게이트가 구제하는
> 진짜 정확도)는 패치 전후 모두 100% 유지**되지만, 이는 "Stage1 오발화 +Xp"(LLM 콜 지연·비용
> 증분, 최종 정확도와 무관)와 별개 지표다 — 둘을 구분해서 읽을 것. 상세·트레이드오프
> 전문은 realtraffic 문서 §6 참조.

- **recall**: 실측상 가장 흔한 갭(모드 A "-고 …?" 병렬 + 모드 C "-고, 그…" 순차, 진단이 "캐주얼
  실사용 트래픽일수록 더 잘 걸린다"고 지목한 유형)을 **0% → 100%(14/14)**로 닫는다. 26-probe
  전체 34.6% → 88.5%.
- **precision 비용**: 큐레이션 대조군 e3_control **+0**, 자연 분포(큐레이션 27건) **+0.0%p**,
  미접촉 H2 홀드아웃 **+0** — 이 좁은 대조군들에서는 T4의 신규 오발화가 0이다.
  co-occurrence 규칙(절경계 + 보조용언/명사 제외 + 독립 의문/지시 후행절)이 "-고"의 FP 폭증
  위험을 결정론으로 억제했다. **단 실트래픽 전수 재측정(위 정정 박스, realtraffic.md §1-6)은
  이 대조군들이 "회고/로고/공고" 같은 흔한 회사 상용어를 담지 못해 생긴 샘플링 편향이었음을
  보였다 — 실제 정밀도 비용은 +0(패치 전 +2.51%p → 패치 후 +0.67%p)이 아니라 작지만 0이
  아닌 값이다.**
- **안전**: 확장은 `should_decompose` 경로에만 걸리고 공유 base 상수를 건드리지 않아
  command-split/event-orch 모집단 불변. flag default 0에서 byte-identical.
- **함께 딸려오는 버그픽스**: 억제어 경계 인식 수정은 recall +1(c34)·precision 0비용의 순
  correctness 이득으로, T4와 독립적으로도 반영 가치가 있다.

**따라서 `ORTHUS_DECOMPOSE_PREFILTER_EXT_TIER=4`를 owner 게이트 프로덕션 후보로 올릴 가치가
있다.** 기존 T3 채택 근거(누락형 recall↑ · 자연 오통과 +0.0%p)를 그대로 이으면서, T3가
구조적으로 못 잡던 "-고" 접속 갭을 무비용으로 추가로 닫는다.

**남은 갭(범위 밖)**: 모드 B(쉼표 열거 "A 종류, B 종류 뭐야?")·모드 D(비유창 "…뭔가요?
아울러 …")는 "-고" 절경계가 없어 T4로 안 닫힌다(잔여 3/26). 쉼표는 단일 질문에도 극히 흔해
FP 위험이 "-고"보다 높으므로 별도 신호(T5)로 격리 측정해야 하며, 본 실험 범위 밖이다.

---

## 부록 — 재현

```bash
# 측정(LLM 0회, DB 0회)
python experiments/fugu-ko/t4_prefilter_measure.py
# 유닛
ORTHUS_PG_DSN=postgresql://localhost/orthus_test \
  python -m pytest tests/unit/test_decompose_prefilter_ext.py -q
```
