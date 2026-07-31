# prompt-lab — 에이전트 프롬프트 A/B 실험 설계 (2026-07-19)

컨텍스트 전달 경로(C1–C6, P10 A-3/C-6 등)는 많이 고쳤지만 **프롬프트 문구 자체를
측정 기반으로 다듬은 적이 없다**는 문제의식에서 출발한다. 대상 표면은 owner 결정으로
둘 다: ① central agent-work 채팅 오케스트레이터, ② P10 텔레그램 게이트웨이(상주 codex).
1차 지표(owner 선택): 산출물 품질/포맷 준수 · 잡 보고 품질(B3/B4) · 지시 준수/안내
정확성 · 답변 자체 품질. 채점은 결정론 체크 + LLM 판정자 혼합.

fugu-ko 랩 관례를 그대로 상속한다: 프로덕션 코드 import(재구현 금지), 골든셋 역방향
생성, paired 측정 + exact McNemar, 판정자 쌍대비교(양방향 스왑·불일치=tie·판정자∉쌍),
회사 데이터 산출물 gitignore. 재사용 유틸: `experiments/fugu-ko/pool.py`(WorkerChat),
`judge/pairwise.py`·`judge/panel.py`, `embedding/significance.py`(exact_mcnemar).

---

## 0. Solar 적합성 검토 — "한국어 wiki면 solar로 실험해도 되나"

근거 문서: `~/Documents/대리ai/orthus-임베딩-실험-보고서.html` (2026-07-16~17,
본편 249문항 + 속편 A.X 파인튜닝).

**결론: 프롬프트 실험을 Solar로 하는 것은 "괜찮다"를 넘어 "해야 한다"가 맞다.
단, 그 근거는 이 보고서가 아니다.**

1. 보고서의 결론은 **검색 임베딩 슬롯 한정**이다 (Solar embedding-passage 대칭 ≻
   OpenAI text-embedding-3-small: MRR +0.080/+0.112, p<0.001, p50 241→69ms).
   보고서 스스로 "Solar가 GPT보다 낫다"를 chat으로 일반화하지 말라고 경계를
   그었다 — chat 슬롯은 별도 실험에서 "국내 모델이 유의하게 이긴 작업 없음,
   옮겨도 큰 손해 없음"이었다 (`docs/model-orchestration.md` §11.3b).
2. 프롬프트 실험을 Solar로 해야 하는 진짜 근거는 **prod 모델 배정**이다: wiki_qa·
   synthesize·decompose·rewrite 등 10개 chat 작업의 1차 모델이 이미 Solar이고
   (`orthus/models/orchestration.py`), 프롬프트 튜닝은 모델-특이적이다. gpt-4o-mini
   에서 이기는 문구가 Solar에서 이긴다는 보장이 없으므로, 배정 모델로 측정하지
   않은 프롬프트 개선은 무효다. (`delegation_extract`만 EXAONE — 그 표면을 만지면
   EXAONE으로 측정.)
3. 보고서가 프롬프트 실험에 실제로 기여하는 것 3가지:
   - **컨텍스트 대표성**: 검색 스택이 Solar-passage 대칭으로 확정됐으므로, 프롬프트에
     주입될 retrieval hits도 그 스택에서 뽑아야 prod 대표성이 있다.
   - **혼재 경고**: `retrieve()`/`ask_cache`는 `model_version`을 필터하지 않는다 —
     임베딩 혼재 DB에서 hits를 뽑으면 "조용한 오답" 컨텍스트 위에서 프롬프트를
     채점하게 된다. hits 캡처 전 대상 DB의 임베딩 단일성 확인 필수.
   - **방법론**: 지표가 표본보다 먼저(정보 버리는 채점으로 "차이 없음" 내지 말 것),
     문서→질문 역방향 골든셋 + 제목 누출 자동 검사, 판정자에게 근거(hits)를 반드시
     제시(§12.1 — 안 주면 자신감 있는 환각에 가점).
4. "한국어로 구축했을 때"라는 전제는 부정확하다: 실측 위키 글자 구성은 한글 15.9% /
   영어 48.2%다. Solar를 쓰는 이유는 "한국어라서"가 아니라 방침+측정이다. 골든셋도
   한국어 질문 + 영어 혼재 본문이라는 실제 분포를 따라야 한다.

**owner 확정 (2026-07-19)**: 현재 위키와 기존 골든셋은 고정 자산이 아니다 —
**Solar로 LLM wiki를 다시 구축(재수집·재distill·재임베딩)해도 된다.** 따라서
실험은 위키를 "주어진 것"으로 두지 않고, 위키를 짓는 프롬프트(distill)까지 실험
대상에 넣은 뒤 승자로 재구축한 위키를 기준으로 답변측을 측정한다 (§3 순서 참조).
이 순서를 지켜야 골든셋/frozen hits 무효화를 한 번으로 끝낸다.

**절연 설계**: 재구축이 끝난 위키에서 질문당 hits를 1회 캡처해 jsonl로 **freeze**하고
모든 답변측 프롬프트 변형이 같은 hits 위에서 경쟁하게 한다. 검색 변동·임베딩 상태와
프롬프트 효과를 분리하고, 재현 가능·저비용이 된다.

---

## 1. Track A — central 오케스트레이터 프롬프트 (Solar)

### 대상 표면 (우선순위순)

| # | 프롬프트 | 위치 | 1차 지표 |
|---|---|---|---|
| A1 | wiki_qa 답변 grounding `_SYSTEM` + `_build_user_prompt` | `orthus/wiki/qa.py:27,100` | 답변 품질 |
| A2 | 복합질문 합성 `_SYNTHESIZE_SYSTEM` | `orthus/router/decompose.py:211` | 답변 품질 |
| A3 | 후속질문 재작성 `_SYSTEM` | `orthus/router/rewrite.py:56` | 지시 준수/맥락 |
| A4 | 질문 분해 4종 템플릿 | `orthus/router/decompose.py:151-206` | 오분해율 |

라우팅/intent(`route.py`)는 fugu-ko 골든셋(t5/t6/routing_holdout)이 이미 있으므로
**회귀 가드로만** 쓴다(프롬프트 변형이 라우팅 정확도를 깎지 않는지).

### 방법

1. **골든셋**
   - `e2_knowledge.json`(113문항, 기존) — A1 baseline.
   - 신규 `chat_scenarios.json`(30–50문항): 히스토리 있는 후속질문(C2/C3 회귀 포함),
     "그거 내용 알려줘"류 지시어, 명령 혼재 질문(C4). 역방향 생성 + 제목 누출 검사.
   - `t7_*` decompose 골든(기존) — A4.
2. **frozen hits**: 문항당 `retrieve()` 1회 실행 결과를
   `analysis/raw/hits_<set>.jsonl`로 고정 (gitignore). 캡처 환경은 §3 참조.
3. **A/B 실행**: 프로덕션 함수 import + 프롬프트 상수 monkeypatch 주입
   (`orthus.wiki.qa._SYSTEM = variant` 식 — 프로덕션 코드 무변경 측정, 채택 시에만
   코드 PR). 모델은 pool.py Solar 워커. 변형당 전 문항 paired.
4. **채점**
   - 결정론: 인용 마커 유출(`[1]`류) 금지, 빈 답/전체 카탈로그 나열, 명령절 무시
     여부(A2 규칙), 재작성 self-containedness(지시어 잔존 정규식), 분해 개수/중복.
   - LLM 판정자: `judge/pairwise.py` 프로토콜 (익명 A/B + 양방향 스왑 + 불일치=tie),
     **판정자 프롬프트에 frozen hits 동봉**, 판정자는 쌍 밖 모델(panel.py 3-judge
     교차 검증은 최종 후보에만).
   - 통계: 쌍대 win-rate + exact McNemar, p<0.05만 채택. 동률이면 현행 유지.

### 변형 가설 (1차 라운드)

- **H1 시스템 프롬프트 한국어화**: 현행 지시문은 영어("You are a company wiki
  assistant...")인데 응답·본문·질문은 한국어다. Solar는 한국어 특화 모델 —
  지시문 언어 정합이 이득인지 측정. (전 표면 공통 가설, 최우선.)
- **H2 실체 요구 강화**: C5(답변 프롬프트 실체 요구) 연장 — 근거 부족 시 "몇 개
  문서가 있다"식 메타 답변 금지 문구 강화.
- **H3 근거 포맷**: `[i] (slug) excerpt` 나열 vs 문서 경계/제목 구조화 vs 관련도
  주석. 컨텍스트 전달을 고친 만큼 프롬프트 쪽 표현 형식도 맞춰본다.
- **H4 히스토리 프레이밍**: `이전 대화 (참고용...)` 블록의 비권위 프레이밍 문구·위치.
- **H5 few-shot 1–2개**: 분해·재작성처럼 형식이 좁은 작업에 한정.

한 라운드에 1–2개 축만 바꾼다(동시 변경 금지 — 승인 요인 추적 불가).

## 1.5 Track C — 위키 구축(distill) 프롬프트 (Solar) — **Track A보다 먼저**

wiki는 재구축 가능 자산(owner 확정)이므로, 답변 품질의 상한을 정하는 **위키 저작
프롬프트**를 먼저 실험한다. 여기서 이긴 프롬프트로 위키를 재구축한 뒤에야 Track A의
골든셋·frozen hits가 안정된 기준을 갖는다.

### 대상 표면

| # | 프롬프트 | 위치 | 지표 |
|---|---|---|---|
| C1 | distill `_SYSTEM` + `_JSON_SHAPE` | `orthus/wiki/distill.py:34,64` | claim 정밀도·커버리지·오염 |
| C2 | claim headline `HEADLINE_SYSTEM` | `orthus/wiki/distill.py:156` | 헤드라인 충실성 (경량) |

### 방법

- **per-document 측정 — 변형마다 전량 rebuild 불필요.** seed corpus에서 문서
  50–80개 층화 표집(프로젝트별·길이별·한/영 비율별) → 변형별 distill 실행 →
  t11 방식 판정: 판정자가 원문을 보고 claim별 supported/fabricated/contradicts
  판정(오염률), 원문 핵심 사실 대비 커버리지, 메타-claim 비율(C6 회귀 —
  "문서에 X가 정리되어 있다"류), claim 수/길이 분포.
- baseline은 현행 main 프롬프트(cap 20 + 전수 추출, Solar 배정 2026-07-15,
  §12.3 실측: 오염 0%, 8.4 claim/문서).
- 변형 가설: **H-C1 지시문 한국어화**(현행 영어 — 한국어 위키 저작이 목표라면
  출력 언어·문체 규정도 명시), **H-C2 claim 원자성/granularity 규칙**(속편 실험의
  교훈 — 페이지 vs 클레임 알갱이 크기가 검색 품질을 갈랐다: 검색·답변에 유리한
  claim 형태를 명시), **H-C3 entity/summary 처리 문구**(C6 fallback 제거 이후
  summary 역할 재규정).
- 채택 판정: 오염률 우선(악화 즉시 기각), 다음 커버리지·메타-claim 비율.
  현행이 이미 오염 0%라 **동률이면 현행 유지**.

### 재구축 게이트

Track C 승자 확정 → 로컬 `--clean` rebuild (Solar distill + Solar embedding,
seed 1,372 docs 규모) → 이것이 Track A의 기준 위키가 된다. prod 재구축(약 6h,
`docs/agent-chat-answer-quality.md` — C6/C1 반영 rebuild가 어차피 pending)은
owner 게이트로 별도 실행하며, 이 실험의 채택 프롬프트가 머지된 main 코드로 돌린다.

## 2. Track B — P10 게이트웨이 프롬프트 (codex)

### 대상 표면

| # | 프롬프트 | 위치 | 1차 지표 |
|---|---|---|---|
| B1 | (아카이브) 게이트웨이 workspace 시드 — 대상 서브시스템은 공개 빌드에서 제거됨 | — | — |


| B4 | 첨부 untrusted 프레이밍 | `channel.py:102-107` | 인젝션 저항·첨부 활용 |

주의: `_SEED_BLOCKS` 주석 "문구를 임의로 다듬지 말 것"은 현행 카피가 e2e 검증본이라는
뜻이다 — 변경은 이 실험의 측정 근거를 달고 `-v2` 블록 버전으로만 올린다.

### 방법

1. **시나리오 리플레이 하네스**: 텔레그램 없이 engine runtime(`handle_turn`)에 직접
   턴을 주입하는 러너. 유즈케이스 카탈로그(`docs/p10-telegram-usecases.md`)에서
   20–30 시나리오 추출: 산출물 생성(U1/U8 축약판 — 브라우저 없이 로컬 자료 기반
   변형 포함), 잡 보고(B3/B4 — 가짜 seen-set 주입, 신규 0건/일부/전부 케이스),
   안내 프로토콜(브라우저 미설치·로그인 벽·제출 전 확인·메일 라우팅), 첨부 합류.
2. **결정론 루브릭** (스크립트 채점):
   - outbox 산출물: 존재 여부, 확장자 규약(.md 금지, 보고서=.pdf, 초안=.docx),
     채팅 텍스트 내 로컬 경로 정규식 위반.
   - 잡: 신규 0건에서 `[SILENT]` 준수, 신규 있는데 `[SILENT]` 오남용, seen-key
     형식/안정성, 기존 항목 재언급(본문 dedup 위반), `[JOB_DONE]` 발화 시점.
   - 안내: 규약 문구 포함 여부(browser setup/login 안내), 되묻기 금지 위반
     (mail-routing 블록).
3. **LLM 판정자 보조**: 보고서 유용성·요약 품질 등 결정론으로 못 잡는 차원만.
4. **비용 통제**: codex는 ChatGPT plan 토큰 — 시나리오 수를 작게 유지하고 변형당
   반복 3회로 비결정성 흡수. Track A보다 변형 수를 적게(시드 재구성 1–2안).

### 변형 가설 (1차 라운드)

- **H6 시드 재구성**: 5블록이 유기적으로 자랐다 — 우선순위/충돌 규칙 명시(예:
  "outbox 규약 > 사용자 즉흥 요청"인지), 중복 제거, 헤더에 요약 계약 1줄.
- **H7 잡 템플릿 보고 형식 명시**: 현행 템플릿은 키 보고 규약 위주 — 보고 본문
  구조(신규 항목만·항목당 1–2줄·링크)까지 명시했을 때 B3 산문 재언급이 줄어드는지.
- **H8 [SILENT] 기준 문구**: "신규 없음"의 정의를 좁혀 오남용(신규 있는데 침묵)과
  과보고(무의미 배달) 양쪽 오류율 측정.
- **H9 첨부 프레이밍 위치/강도**: untrusted 래핑이 첨부 "활용"까지 위축시키지
  않는지 (양식 채우기 시나리오와 인젝션 시나리오 쌍으로).

## 3. 실행 환경·순서

순서의 근거: 위키 구축 프롬프트(Track C)를 나중에 바꾸면 그 위에서 만든 골든셋과
frozen hits가 전부 무효가 된다. **구축 → 재구축 → 질의** 순서로 무효화를 한 번으로
끝낸다. Track B(게이트웨이)는 위키와 독립이라 병행 가능.

- **Phase 0 (환경)**: Solar API 키 수령 → `experiments/fugu-ko/keys.json` 형식으로
  저장(미커밋). 로컬 `make seed`로 seed corpus 준비(1,372 docs / 8,929 wiki_pages).
- **Phase 1 (Track C — 구축 프롬프트)**: per-doc distill 하네스 + 표집 50–80 docs
  → 현행 baseline(오염·커버리지·메타-claim) → H-C1(한국어화)부터 paired
  → 승자 확정.
- **Phase 2 (재구축)**: Track C 승자로 로컬 `--clean` rebuild (Solar distill +
  Solar embedding). rebuild 후 `wiki_chunks` 임베딩 단일성 확인(§0 혼재 경고).
  이 위키가 이후 모든 측정의 기준.
- **Phase 3 (Track A — 질의측)**: 재구축 위키에서 골든셋 역방향 재생성(+제목 누출
  검사) + frozen hits 캡처 → 현행 프롬프트 baseline → H1부터 paired A/B →
  유의 승자만 채택 목록에.
- **Phase 2·3과 병행 (Track B)**: 리플레이 하네스 + 결정론 루브릭 → 현행 시드
  baseline(이 자체가 첫 산출물 — 현행 위반율 실측) → H6–H8.
- **Phase 4 (반영)**: 채택분만 프로덕션 PR — 프롬프트 변경 + 해당 골든셋을 회귀
  가드로 고정. Track A는 라우팅 회귀(t5/t6) 무회귀 확인 동반. Track C 채택 시
  prod `--clean` rebuild(약 6h)는 owner 게이트 — C6/C1 반영 rebuild가 어차피
  pending이므로 같은 창에 태운다. Track B 시드는 `-v2` 블록 + 실봇 스모크 1회.

## 4. 산출물·기록 관례

- 결과·원자료: `experiments/prompt-lab/analysis/` (raw jsonl은 gitignore).
- 라운드마다 `analysis/round-N.md`에 가설→측정→판정 기록 (fugu-ko experiment-log
  S/TS 서사 관례).
- 회사 문서 원문·생성 질문·벡터 미커밋 — 스크립트와 문서만 추적.
