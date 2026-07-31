# D4 — 오케스트레이터 계층 측정 (T6 intent / T7 decompose)

## 0. 왜 이 측정을 추가했나
D2/D3(T2/T3/T5)는 `/ask` 라우터가 분기하는 **잎(leaf) 작업**을 쟀다. 그런데
실제 오케스트레이션은 `POST /agent-work/chats/{id}/orchestrate`에서 일어난다. 이
오케스트레이터는 자기만의 모델을 갖지 않고 다음 흐름으로 **잎 작업을 조합**한다:

```
orchestrate
  → classify_intent()   # ① 명령이냐 질문이냐 (7-way)         ← T6
  → should_decompose()  # ② 복합질문을 쪼갤까 (게이트)        ← T7
  → split_question()    #    복합질문 분해                     ← T7
      → answer()        #    조각별 → structured/wiki/graph    = T3/T2/T5(잎)
  → synthesize()        # ④ 조각 답 합치기                     ← T8(예정)
```

D2/D3는 잎(③)만 쟀으므로, 오케스트레이터 **고유 결정**(①②④)의 모델 민감도는
미측정이었다. D4는 ①intent·②decompose·④synthesize **세 결정을 모두** 실제 함수에
국내 모델을 주입해 측정한다. 모든 함수가 `chat_model=` 인자를 노출해 잎과 동일한
격리 주입이 가능하다(프로덕션 무수정).

측정: `harness.py {t6,t7}` (결정론 채점) + `t8_synth.py` (judge). 골든 `golden/{t6,t7,t8}.json`.

---

## 1. 발견 O1 — 오케스트레이터 결정은 결정론 게이트가 지배적
두 결정 모두 **LLM 앞단에 결정론 fast-path**가 있다. 명확한 입력은 LLM 0회로 끝난다.

| 결정 | fast-path | LLM 도달 |
|---|---|---|
| T6 intent (20문항) | 14 (70%) — 키워드 detector + 룰 route | 6 (30%) |
| T7 decompose 게이트 (22문항) | 11 (50%) — 명령verb/접속·열거 신호 부재 시 즉시 False | 11 (50%) |

→ **함의:** 오케스트레이터 계층은 잎 계층(T2/T3/T5, 매 호출이 LLM)보다 **모델 교체
민감도가 낮다.** 결정의 상당 부분을 결정론 코드가 처리하므로, 국내 모델 도입
리스크가 오케스트레이션에서 더 작다. (채택 근거)

---

## 2. 발견 O2 — intent 판정은 모델 무관 (T6)
LLM 도달 6문항에서 **4모델 전부 6/6 만점**, 전체도 19/20 동률.

| 모델 | intent 전체 | LLM 도달분 | p95 |
|---|---|---|---|
| solar | 19/20 | 6/6 | 885ms |
| ax | 19/20 | 6/6 | 945ms |
| exaone | 19/20 | 6/6 | 364ms |
| baseline | 19/20 | 6/6 | 919ms |

- 유일한 오답 `t6-08`("이미 처리된 위키 정리 작업들 마무리해줘" → 기대 `central_wiki_task_cleanup`,
  실제 `personal_board_cleanup`)은 **결정론 키워드 detector의 오분류**로 4모델 동일 —
  모델 문제가 아니라 **fast-path 규칙의 한계**다(O4).
- → **intent는 국내 모델도 baseline과 동일하게 안전.** 오케스트레이터 첫 관문에서
  모델 리스크 없음.

---

## 3. 발견 O3 ★ — decompose 게이트에서 A.X 붕괴 (T7)
복합질문을 "쪼개야 한다"고 인식하는 능력. LLM 도달 복합 10문항에서:

| 모델 | decompose recall (복합 10문항) | 게이트 종합(LLM 11) | split 유효 | p50 |
|---|---|---|---|---|
| **solar** | **9/10 (90%)** | 10/11 | 16/16 | 748ms |
| baseline | 9/10 (90%) | 10/11 | 16/16 | 1337ms |
| exaone | 8/10 (80%) | 9/11 | 16/16 | **371ms** |
| **A.X** | **2/10 (20%) ← 붕괴** | 3/11 | 14/16 | 1362ms |

- **A.X는 거의 모든 복합질문을 "쪼갤 필요 없음"으로 오판한다.** 명시적 열거
  (`t7-04` "1. 영입 후보 몇 명 2. 그중 개발자 몇 명")조차 단일로 판정.
- **오케스트레이션 파괴적 함의:** A.X를 오케스트레이터에 쓰면 복합질문의 절반이
  조용히 누락된다("환불 정책과 배송 정책?" → 환불만 답). 잎 라우팅(T5)에선 A.X가
  최강이었지만, 오케스트레이터 결정에는 **절대 배정 불가**.
- split_question(분해 실행) 자체는 A.X도 14/16으로 준수 — **문제는 "쪼갤지 판단"
  (recall)이지 "쪼개는 실행"이 아니다.**

### O3은 S9(라우팅 특화 역설)의 연장
A.X의 실패 패턴이 실험 전체에서 일관된다:

| 태스크 | 성격 | A.X |
|---|---|---|
| T5 라우팅 | 명확한 1개 라벨 선택(decisive) | **최강 95%** |
| T2 지식응답 | 충분히 풀어 설명(elaborate) | 최약 38% |
| T7 decompose | "더 해야 함"을 인식(recognize-more) | **붕괴 20%** |

→ **A.X = 단호하지만 좁은(decisive-but-narrow) 모델.** 크리스프한 분류엔 최고,
"더 필요하다"를 알아채는 판단엔 최악. 이 상보성이 오케스트레이션 논거의 핵심.

---

## 3-b. 발견 O5 ★ — synthesize(조각 답 통합)에서도 A.X만 패 (T8)
오케스트레이터의 마지막 결정 ④: 복합질문의 grounded 조각 답들을 하나로 통합
(`synthesize`). 입력(sub_answers)을 **고정 solar로 1회 생성해 freeze**하고 synthesize
모델만 교체(입력 통제) → 워커 통합 vs baseline 통합을 양방향 쌍대 judge. grounded 조각
≥2인 5문항 대상(8문항 중 3개는 grounded<2로 제외 — 1개 wiki 본문은 결정론 passthrough).

| 모델 | 승 | 패 | 무 | 승률(승/(승+패)) | 수용(≥40%) |
|---|---|---|---|---|---|
| solar | 1 | 0 | 4 | **100%** | PASS |
| exaone | 1 | 0 | 4 | **100%** | PASS |
| **A.X** | 0 | **2** | 3 | **0%** | **FAIL** |

- 입력이 고정이라 무(tie)가 많다(출력 수렴) — 표본이 작고 결정 케이스가 얇으니 승률
  100%는 방향성 신호. **강한 신호는 A.X가 유일하게 패(0승 2패)** 한다는 것.
- A.X 패는 실품질 갭(빈 출력 아님): `t8-05`는 **`[partner_name]` 미치환 템플릿 토큰
  누출** + 인용 노이즈(`[3] 및 [4] 참`), `t8-02`는 어색한 헤더 반복 + 문장 중간 잘림.
  baseline/Solar/EXAONE는 산문으로 정리 + 두 조각을 매끄럽게 통합.
- → **A.X는 통합에서도 약하다.** 오케스트레이터 3개 결정 중 판단·생성이 필요한
  둘(decompose·synthesize)에서 모두 최약, intent(단순 분류)에서만 대등.

### O3+O5 = A.X 패턴의 완성 (S9 확정)
| 계층·태스크 | 성격 | A.X |
|---|---|---|
| T5 라우팅 | 크리스프 분류(decisive) | **최강 95%** |
| T2 지식응답 | 풀어 설명(elaborate) | 최약 38% |
| T7 decompose | "더 해야 함" 인식(recognize-more) | **붕괴 20%** |
| T8 synthesize | 조각을 매끄럽게 통합(integrate) | **유일 패 0%** |

→ A.X = **단호하지만 좁은(decisive-but-narrow)** 모델. 크리스프한 1개 라벨 선택엔 최강,
생성·통합·판단이 필요한 모든 곳에서 최약. **이 상보성이 오케스트레이션 논거의 핵심** —
잎에서 라우팅 최강이던 모델을 오케스트레이터 판단엔 절대 못 쓴다.

## 4. 발견 O4 — 결정론 fast-path의 한계도 노출 (모델 무관 코드 이슈)
오케스트레이터 품질은 모델뿐 아니라 결정론 로직에도 달렸다. 두 갭 관측:

- **intent 키워드 오분류(O2 t6-08):** "정리/마무리" 계열이 `central_wiki_task_cleanup`
  이어야 할 때 `personal_board_cleanup`으로 매핑. 키워드 detector 규칙 갭.
- **decompose prefilter recall 갭:** prefilter가 아는 접속어는 **7개뿐**
  (그리고/그다음/또한/동시에/그후/알려주고/해주고). 자연스러운 한국어 복합 어미
  (랑/이랑/고/같이/각각/함께/비교해서)는 **못 잡아** LLM 게이트에 도달조차 못 하고
  단일로 답한다(`t7-01,02,05,07,09,10`). 모델 무관 코드 recall 갭.

→ **보고 시사:** 오케스트레이터 개선은 "더 좋은 모델"과 "더 넓은 결정론 규칙" 둘 다
필요하다. 국내 모델 교체만으로 해결되지 않는 영역이 명확히 존재.

---

## 5. 선택기 반영 (2단b 확장)
`selectors/static_map.py`에 오케스트레이터 배정 추가:

| 오케스트레이터 결정 | 배정 | 근거 |
|---|---|---|
| intent | solar | 모델 무관(6/6 동률) → 종합 최강 |
| decompose | solar | Solar/baseline 90% ≫ A.X 20% |
| synthesize | solar | Solar/EXAONE 무패 vs A.X 0%(토큰 누출) |

→ **오케스트레이션은 잎+오케 양 계층에서 A.X를 라우팅 전용으로만 쓰고, 판단형은
Solar에 몰아준다.** 단일 A.X를 골랐다면(T5 최강이라는 이유로) 오케스트레이터가
복합질문(decompose)에서 붕괴하고 통합(synthesize)에서도 졌을 것 — 태스크별 배정의
가치를 오케스트레이터 3개 결정이 모두 재확인.

---

## 6. 한계
- **표본 크기:** 오케스트레이터 결정은 fast-path 지배라 LLM 도달분이 얇다(intent 6,
  decompose 복합 10, synthesize grounded≥2 5문항). A.X 붕괴(decompose 20% vs 90%,
  synthesize 유일 패)는 신호가 강하고 기전이 체계적이라 방향은 견고하나, 대규모
  재현은 2단에서 표본 확대.
- **synthesize 무(tie) 다수:** 입력을 고정(freeze)해 synthesize 모델만 변수로 두므로
  출력이 수렴 → tie가 많다(5문항 중 4). 이는 설계상 의도된 입력 통제의 결과이며,
  결정 케이스에서 A.X만 패한다는 신호가 요지.
- **decompose 게이트 라벨:** `t7-08`("리텐션 정책이 뭐고 왜 중요한지")은 단일 주제의
  what+why라 비분해가 정답일 수 있어 채점에서 보수적으로 처리(전 모델 False 일치).
- **오케스트레이터 3/3 커버:** ①intent·②decompose·④synthesize 모두 측정 완료.
  (③은 잎 T2/T3/T5 = D2/D3에서 이미 측정.)

## 7. 재현
```text
export ORTHUS_NODE=company ORTHUS_NODE_DB=orthus_company
set -a; source ~/.orthus/nodes/company/node.env; set +a
H="PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/harness.py"
$H t6 --models solar,ax,exaone,baseline   # intent 7-way
$H t7 --models solar,ax,exaone,baseline   # decompose 게이트+split
python experiments/fugu-ko/t8_synth.py    # synthesize(입력 freeze→쌍대 judge)
```
