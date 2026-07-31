# X0 — 외부 공개 데이터셋 도입 기획 (external validity)

> 작성 2026-07-21 · 상태 **제안(미승인)** · 리서치 4갈래(RAG/QA · Text-to-SQL · 라우팅/툴콜/분해 · 생성+판정자) 실측 조사 결과
> 전제: 아래 데이터셋 사실(행 수·라이선스·필드)은 이번 조사에서 **실제 페이지를 열어 확인**한 것이다.
> "미확인"으로 표시된 항목은 인용 전 재확인이 필요하다.

## 0. 왜 필요한가 — 지금 벤치마크의 구조적 구멍

`e2e/inventory.json` 기준 **골든 자산 34개가 전부 자체 제작**이고, 저장소 전체에서 외부 공개
데이터셋을 쓴 흔적이 없다(`grep -ri huggingface|korquad|kmmlu` → 코드 유틸 1건뿐). 이건 강점이자
약점이다. 강점은 "남의 벤치마크가 아니라 실회사 데이터"라는 보고서의 핵심 서사고, 약점은 아래 넷이다.

| ID | 구멍 | 근거 |
|---|---|---|
| **G-JUDGE** | 판정자(gpt-4o pairwise)가 **사람과 일치하는지 한 번도 검증 안 됨** | `e2e/STATE.md` GATE caveat (3): "24 anchors are unverified by a human" |
| **G-EXT** | 모든 결론이 orthus 스냅샷 1개 위에서만 성립 — 일반화 주장 불가 | d9-prereg §4.1 "태스크 1종뿐" 자인 |
| **G-REJECT** | 게이트가 **거부해야 할 입력**을 외부 기준으로 검증한 적 없음 | t3는 게이트 통과율 중심, infeasible 셋 없음 |
| **G-EMBED** | 임베딩 슬롯 교체 근거(MRR +0.080)가 **자체 wiki 코퍼스 1종** | `experiments/fugu-ko/embedding/README.md` |

이 기획의 목표는 **자체 셋을 대체하는 게 아니라**, 위 4개 구멍만 외부 자산으로 메우는 것이다.
자체 셋은 도메인 정합성에서 여전히 우월하므로 SoR로 유지한다.

## 1. 결론 먼저 — 채택 후보 8종

| 우선 | 데이터셋 | 메우는 구멍 | 라이선스 | 규모 | 비용 |
|---|---|---|---|---|---|
| **P0** | `HAERAE-HUB/KUDGE` | G-JUDGE | 미표기(평가·인용만) | 인간 라벨 2.88k, 주석자 2인 | 낮음 |
| **P0** | `HAERAE-HUB/Korean-Human-Judgements` | G-JUDGE | 미표기, 카드에 "평가 전용" 명시 | 694 쌍대(A/B/tie) | 낮음 |
| **P0** | `mteb/AutoRAGRetrieval` + `miracl/miracl(ko)` + `castorini/mr-tydi(korean)` | G-EMBED | MIT / Apache-2.0 | 114q / 213q / 2,020 | 낮음 |
| **P1** | `wicho/kor_3i4k` | G-EXT(t6), G-REJECT | CC-BY-SA-4.0 | 61,255 (7-way) | 낮음 |
| **P1** | `allganize/RAG-Evaluation-Dataset-KO` | G-EXT(t2) | **MIT** | 300 + 18개 시스템 출력 | 낮음 |
| **P1** | KorWikiTQ (LG-NLP) | G-EXT(t3) | CC-BY-SA-4.0 | 69,992 QA / 한국어 표 헤더 | 중간 |
| **P2** | `lmsys/mt_bench_human_judgments` + `mteb/summeval` | G-JUDGE(영어 앵커) | CC-BY-4.0 / MIT | 3.3k 쌍대 / 100 factuality | 낮음 |
| **P2** | `AmazonScience/massive(ko-KR)` · `warnikchow/paraKQC` | G-EXT(t5/t6) | CC-BY-4.0 / CC-BY-SA-4.0 | 16.5k / 10k | 중간 |

**"사서 쓸 수 없는 것"도 결론이다:** t9(graph_bind)와 t10(delegation_extract)에 대응하는 공개
데이터셋은 **존재하지 않는다**(한국어 KGQA 없음, 위임지시 라벨셋 없음). 이 둘은 외부 *프로토콜*만
빌려 자체 제작하는 게 유일한 길이다(§4).

## 2. P0 — 판정자 검증 (G-JUDGE). 가장 싸고 가장 아프다

지금 t2/t8/email_draft 승률은 전부 gpt-4o 판정자에 의존한다. 판정자가 한국어에서 사람과 얼마나
일치하는지 모르면 그 승률의 의미도 모른다. E2E STATE의 caveat (3)이 이걸 이미 자인한다.

**측정 설계 (신규 라벨링 0건):**

1. `HAERAE-HUB/Korean-Human-Judgements` 694건 → 우리 pairwise 프롬프트(위치 스왑 포함) 그대로 실행
   → **judge vs human 정확도 + Cohen's κ**, 그리고 **위치 스왑 flip rate**(한국어에서의 위치편향).
2. `HAERAE-HUB/KUDGE` `Human Annotations` 2.88k → 주석자 2인(`score1`/`score2`)이 있으므로
   **human-human κ = 천장**을 먼저 구하고, judge-human κ를 그 천장 대비로 보고한다.
   (카드: 두 주석자 83.85%가 동일 또는 1점 이내)
3. `KUDGE`의 `Pointwise-False`/`Pairwise-False`(각 54행, 허위정보 주입) → 우리 판정자가
   **사실오류를 감지하는지** 별도 축으로 측정. distill 오염률 지표와 같은 방향의 신호다.
4. 영어 앵커 `lmsys/mt_bench_human_judgments`(3.3k, CC-BY-4.0)에 **동일 프롬프트**를 돌려,
   한국어 κ가 낮다면 그게 "한국어 페널티"인지 "우리 프롬프트 자체의 노이즈"인지 분리한다.
5. `mteb/summeval`(100건, `consistency` 1–5 전문가 라벨, MIT) → factuality 축 판정자 검증.

**보고 문안(사전 고정):** 판정자 κ가 human-human 천장의 X% 이상이면 "판정 승률은 사람 판단의
대리지표로 유효"라고 쓰고, 미달이면 **모든 judge-scored 결과에 그 κ를 병기**한다. 결과를 보고
문안을 바꾸지 않는다(D7 §7 상속).

⚠️ KUDGE·Korean-Human-Judgements는 **라이선스 미표기**다. 평가·인용은 하되 **재배포 금지**로
취급하고, 저장소에는 다운로드 스크립트 + 해시만 커밋한다.

## 3. P0/P1 — 외부 타당성 (G-EXT). 태스크별 매핑

### 3.1 임베딩 슬롯 (G-EMBED) — 가장 근거가 얇은 주장부터

현재 Solar 임베딩 교체 근거는 자체 wiki 코퍼스 1종의 MRR/지연이다. 그런데 한국어 검색 벤치마크는
**이미 표준 하네스가 있다**: KURE(고려대) MTEB-ko-retrieval이 `AutoRAGRetrieval`, `Ko-StrategyQA`,
`MIRACL-ko`, `Mr.TyDi-ko`, `PublicHealthQA`를 묶어 쓴다. 그중 라이선스가 깨끗한 셋만 고른다:

- `mteb/AutoRAGRetrieval` — MIT, BeIR 포맷(queries/corpus/qrels), 114q/720doc
- `miracl/miracl` ko — Apache-2.0, 868 train / 213 dev
- `castorini/mr-tydi` korean — Apache-2.0, 2,020

`mteb` 라이브러리로 돌리면 **공개 리더보드와 비교 가능한 숫자**가 나온다. 자체 코퍼스 결론과 부호가
같으면 §14 교체 결정의 외적 타당성이 붙고, 다르면 그게 더 중요한 발견이다.
⚠️ Ko-StrategyQA/PublicHealthQA는 라이선스 미표기 → 보조 지표로만.

### 3.2 t2 wiki_qa — `allganize/RAG-Evaluation-Dataset-KO` (MIT)

300문항(금융/공공/의료/법률/커머스), `question / target_answer / target_file_name / target_page_no`
+ **18개 상용 RAG 시스템의 답변과 O/X 채점**이 같이 들어있다. 두 가지로 쓴다:

- (a) 우리 파이프라인을 이 300문항에 태워 **외부 도메인 grounded QA 승률**을 보고 → "회사 wiki에
  과적합된 게 아니다"의 유일한 직접 증거.
- (b) 동봉된 O/X 사람 채점을 **판정자 2차 검증**으로 재활용(§2의 보강).

한계: 도메인이 회사 wiki와 다르고 **"모른다"(unanswerable) 케이스가 0건**이다. 그래서 abstention은
아래 KLUE로 따로 본다.

### 3.3 abstention / "모른다" — `klue/klue` config `mrc` (CC-BY-SA-4.0)

23.4k 행에 **`is_impossible` 불리언**이 있다. 조사한 한국어 자산 중 답할 수 없음을 명시 라벨로 가진
건 이것뿐이다. 우리 `expect_gap` 케이스(`evals/golden/company_chat_v1.jsonl`의 `annual_leave_policy`
류)는 현재 자체 제작 소수인데, KLUE-MRC는 **형식 기증자(format donor)**로 쓴다 — 즉 KLUE 문항을
직접 채점에 넣기보다, 그 answerable/unanswerable 구성비와 문항 설계를 우리 wiki 위에 이식한다.
⚠️ KLUE는 Solar/EXAONE/A.X 사전학습에 거의 확실히 포함 → **헤드라인 수치로 쓰지 않는다**.

### 3.4 t3 NL→SQL — KorWikiTQ가 유일하게 "한국어 스키마"를 준다

E2E Phase 5에서 **실행 문항의 75%가 t3**인데, 그 t3는 우리 12개 DB에만 걸려 있다(STATE caveat 1).
t3의 진짜 난이도는 **한국어 컬럼/테이블명 스키마 링킹**(협업업무표·담당자·우선순위)인데,
`huggingface-KREW/spider-ko`는 질문만 한국어이고 **스키마는 영어**라 그 난이도를 안 건드린다.

- **KorWikiTQ**(LG-NLP, CC-BY-SA-4.0, 69,992 QA / 한국어 위키 표 69k) → 표 1개 = Notion DB 1개로
  `notion_rows` JSONB에 적재하면 **한국어 헤더가 공짜로 따라온다**.
- gold SQL이 없지만 **문제가 안 된다**: 우리 1차 지표는 게이트 통과율(parse / SELECT-only /
  schema_ok / read_only / LIMIT / EXPLAIN)이고 이건 gold 없이 채점된다. 실행 일치가 필요한
  ~200문항 슬라이스만 gold SQL을 저자한다(groupby-count / filter-count / order-by 인텐트 커버).
- **AI-Hub 71351 자연어 기반 질의(NL2SQL)** — 111,152쌍 / 6,401 DB / **한국어 컬럼명 + SQLite 동봉**.
  t3에 가장 가까운 공개 자산이지만 **내국인만 신청 가능 + 해외반출 불가 + 재배포 불가**.
  **승인 리드타임이 긴 길목**이라 쓸 거면 지금 신청하고, 아니면 명시적으로 버린다(§6 결정 필요).

### 3.5 t5/t6 라우팅·의도 — 한국어 자산이 의외로 좋다

- `wicho/kor_3i4k`(61,255, CC-BY-SA-4.0, **번역 아닌 원어**): 7-way = fragment / statement /
  **question** / **command** / **rhetorical question** / **rhetorical command** / intonation-dependent.
  t6의 질문-vs-명령 축 그 자체다. 그리고 클래스 4·5(수사적 질문/명령)는 **공짜 함정 세트**다 —
  "그걸 누가 하겠냐"가 `agent_task`로 바뀌면 안 된다. 클래스 6(운율 의존 모호)은 원리상
  `request_more_data`로 가야 맞다.
- `warnikchow/paraKQC`(10,000 = 1,000세트 × 10 패러프레이즈, CC-BY-SA-4.0): 주제가 **메일 / 일정 /
  스마트에이전트 / 날씨**, 화행에 *요구(requirement)* / *금지(prohibition)* 포함. orthus가 실제로
  틀리는 "메일 명령 vs 메일 질문"에 정확히 대응하고, 10개 패러프레이즈 세트라 **표면 변형 강건성**을
  덤으로 잰다. ⚠️ 라벨 컬럼 의미가 README에 없어 논문(LREC 2020) 기준으로 경험적 매핑 필요.
- `AmazonScience/massive` ko-KR(16.5k, CC-BY-4.0): `scenario`(18) → t5식 coarse 라우팅,
  `intent`(60) + `annot_utt` 슬롯(55종) → t6 typed action 추출. ⚠️ MTEB 태스크라 **오염도 높음**.

### 3.6 t7 decompose — 살 수 있는 게 거의 없다

`NomaDamas/Ko-StrategyQA`(Apache-2.0)가 **유일한 한국어 gold `decomposition`**이지만, 그건
*암묵적 전략* 분해지 우리가 잡는 **표면 접속 분해**(랑/이랑/각각/비교/와·과)가 아니다.
→ `e3_prefilter.py` 자체 골든을 SoR로 유지하고, **라벨 택소노미만** BREAK의 `operators`
(select/filter/project/comparison/union/intersection)와 2WikiMultihopQA의 `type` 4분류에서 빌린다.
`KETI-AIR/kor_wiki_hop`(43.7k, CC-BY-SA-3.0)은 **"멀티홉이지만 쪼개면 안 되는" 음성 대조군**으로 쓸모.
음성 대조군은 `yixuantt/MultiHopRAG`(ODC-BY)의 `null_query` 타입도 같은 역할을 한다.

⚠️ **Ko-StrategyQA 미러 함정:** `taeminlee/`·`mteb/`·`kozistr/`·`tony9402/` 사본은 **`decomposition`
필드를 떨어뜨린다**. 반드시 `NomaDamas/Ko-StrategyQA` 원본을 쓴다(뷰어는 hex-key dict라 깨져 보이나
raw JSON은 정상). 영어 `ChilleD/StrategyQA`(MIT)도 decomposition이 없어 **한국어 사본이 오히려 풍부**하다.
확인된 부재: 한국어 MuSiQue·2Wiki·BREAK/QDMR 모두 없고, **원어로 작성된 한국어 복합질문 코퍼스는
존재하지 않는다**(HF 전수 검색). t7 표면 접속 분해는 자체 제작 외 대안이 없다.

🔻 **미해결 충돌 — Ko-StrategyQA (사용 전 직접 확인 필요).** 조사자 두 명의 보고가 갈렸다.
raw JSON(`ko-strategy-qa_dev.json`)을 직접 받은 쪽은 "한국어 `decomposition` 존재 + Apache-2.0"이라
보고했고, 카드만 본 쪽은 "뷰어 깨짐 + 라이선스 미표기 + decomposition 검증 불가"라고 보고했다.
뷰어가 hex-key dict라 깨져 보이는 것이 후자의 원인일 가능성이 크지만, **직접 raw 파일을 받아
필드와 라이선스를 확인하기 전에는 인용하지 않는다.** 어차피 t7 SoR은 자체 셋이라 리스크는 낮다.

**보너스(라이선스가 더 깨끗한 t9 기증자):** `framolfese/2WikiMultihopQA`(**Apache-2.0**)는 `evidences`가
**(subject, relation, object) Wikidata 트리플**이라 graph_bind 출력 형태 기증자로 KLUE-wos보다 낫다
(KLUE는 CC-BY-SA 전파 + 오염 최대). `dgslibisey/MuSiQue`의 `answerable` 불리언도 "답할 수 없음"
음성으로 쓸 수 있으나 **라이선스 미표기**다.

## 4. 살 수 없는 것 — 외부 *프로토콜*로 자체 제작 (가장 대회 가치가 높은 부분)

조사 결론: **t10(위임 추출)·t9(graph_bind)·게이트 거부셋에 대응하는 공개 데이터는 없다.**
그런데 "만드는 방법"은 검증된 외부 설계에서 빌릴 수 있다. 이게 §1 표의 P1/P2보다 우선순위가 높을 수 있다.

| 만들 것 | 빌릴 프로토콜 | 왜 |
|---|---|---|
| **t3 거부셋** (G-REJECT) | **TrustSQL**의 feasible/infeasible 쌍 구성(존재하지 않는 컬럼 / SQL 범위 밖 요청) + **AmbiQT**의 컬럼-동의어 모호성(담당자↔책임자) | 우리 게이트의 자랑은 "거부"인데 거부 성능을 잰 외부 기준이 없다. 데이터를 받지 말고 **레시피만** 우리 한국어 스키마에 이식 |
| **t6 4-way 결정셋** | **`nvidia/When2Call`**(CC-BY-4.0)의 4지선다: 도구호출 / 되묻기 / 답변불가 / 불가능 — 우리 `auto_execute / request_more_data / reject`와 동형. **`MadeAgents/xlam-irrelevance-7.5k`**(CC-BY-4.0)의 "관련 도구 없음→빈 답" 음성 | 한국어 등가물이 없다. 스키마를 번역 이식하는 게 정공법 |
| **t10 적대 홀드아웃** | **`naver-ai/kobbq`**(81k, **MIT** — 이번 조사에서 가장 깨끗한 라이선스)의 *모호 맥락 vs 명시 맥락* 설계 + **KoBEST**의 최소쌍/부정 섭동 | R1에서 최선 모델도 함정 24개 중 2개를 오탐했고(회의록 액션아이템·자기배정), 그 오탐은 팀원 PC에서 full-access 에이전트를 띄운다. **모호하면 지어내지 말고 `request_more_data`** — KoBBQ의 "ambiguous context → unknown"이 정확히 그 설계 |
| **t10 슬롯 택소노미** | AMI 회의록의 owner/task/timeframe 3슬롯 | AMI 자체는 액션아이템 라벨이 공개 배포되지 않음(확인) — 택소노미만 차용 |
| **t9 graph_bind 셋** | `klue/klue` config `wos`의 상태 표기 `도메인-슬롯-값`(예: `관광-종류-박물관`) + `ner`의 명사구 스팬 | 한국어 KGQA 공개셋 없음(확인). wos 트리플이 "enum intent + 명사구 subject"와 형태가 같아 **출력 스키마 기증자**로 쓴다 |

t10 음성 생성 축(제안): 시제·상 뒤집기(했어요/할게요/해주세요), 질문-vs-명령, **자기배정**,
제3자 보고(전언), 담당자 미지정. 앞의 셋이 R1 실측 오탐과 같은 계열이다.

**시드 텍스트 후보(승인 시):** AI-Hub `272`(한국어 대화 — 업무 도메인 10,202세트 / 46,414 턴쌍,
유일하게 확인된 한국어 직장 대화 코퍼스)와 `71795`(국회 회의록 QA 44,033 — "~하시기 바랍니다"류
공손 명령형이 **오탐 함정 원천**으로 좋다). ⚠️ 단 **AI-Hub 4종 어디에도 액션아이템/담당자/기한
라벨이 없다** — 전부 재주석 필요. 즉 AI-Hub를 받아도 "라벨을 산" 게 아니라 "원문을 산" 것이다.
영어 쪽도 마찬가지다: AMI의 ACTIONS는 요약문 안 제목일 뿐 독립 레이어가 아니고(확인),
Purver류 액션아이템 주석(AMI 101회의/381건)은 **공개 다운로드가 없다**. Avocado(실기업 메일함 279개)는
도메인은 이상적이나 **LDC 유료 + 무라벨**이다.

## 5. 제외 목록 (쓰지 않는다 — 이유 명시)

| 대상 | 제외 사유 |
|---|---|
| `heegyu/namuwiki*` | **CC BY-NC-SA 2.0 = 비상업** + 원저작권 불명 |
| `KorQuAD` v1 / v2.1 | **CC BY-ND 2.0 KR = 파생 금지** → 하네스로 재가공·공개 시 위험 |
| `HAERAE-HUB/KMMLU` | **CC BY-ND** + 공개 train 208k로 **오염 최악**. 인용은 가능하나 헤드라인 금지 |
| `csebuetnlp/xlsum`, `MeetingBank`, `DialogSum`, SmileStyle | **비상업(NC)** |
| AI-Hub 전반(97/272/544/582/71351/71795) | **내국인만 신청 + 해외반출 불가 + 재배포 불가**. 팀 구성·호스팅 위치에 따라 정책 위반 가능 → §6 결정 사항 |
| `elenigkove/*`, `aadilsayad/*` 이메일 의도셋 | **ChatGPT 생성 라벨 25~1,000행** — LLM을 LLM이 만든 라벨로 채점하는 자기오염 |
| `irene93/function-calling-v3-datasets-korean` | 라이선스 미표기 |
| BFCL | **한국어 스플릿 없음**(확인). 평가 *방법론*(AST match)만 참고 |
| MultiSpider / CSpider | **한국어 미포함**(확인) |
| K-HALU 전체셋 | AI-Hub 게이팅 + 2025-12-30 접근 제한 공지. 공개분 21행은 벤치마크 아님 |

## 6. 결정이 필요한 것 (owner)

1. **AI-Hub 신청 여부.** 71351(NL2SQL, 한국어 스키마)과 544(지시하기 화행)는 t3·t10에 가장 잘 맞지만
   *내국인 전용·해외반출 불가·재배포 불가*다. 승인 리드타임이 길어 **지금 신청하거나 지금 버려야** 한다.
2. **공유 라이선스(CC-BY-SA) 전파 수용 여부.** kor_3i4k·paraKQC·KorWikiTQ·KLUE가 전부 SA다.
   파생 벤치마크를 공개 배포하면 동일조건 전파 의무가 붙는다. (사내 평가만이면 무관)
3. **오염 통제 강도.** KLUE/MASSIVE/Spider 계열은 국내 모델이 학습했을 가능성이 높다.
   최소 방어는 **closed-book 대조**(컨텍스트 없이 맞히는 문항은 grounded 점수에서 제외)인데,
   이걸 전 외부셋에 강제할지 결정 필요.

## 7. 권장 실행 순서

- **1주차 (P0, 신규 라벨 0):** 판정자 검증(§2) + 임베딩 외부 코로보레이션(§3.1).
  → E2E STATE caveat (3)을 직접 닫고, §14 임베딩 교체 결정에 외부 근거를 붙인다.
- **2주차 (P1):** allganize 300 외부 grounded QA 실행 + kor_3i4k/paraKQC로 t6 외부 검증.
  → "회사 스냅샷 과적합 아님" 증거 확보.
- **3주차 (P1/빌드):** KorWikiTQ → `notion_rows` 적재 + 게이트 통과율 측정(gold 없이 가능),
  이어서 TrustSQL 레시피로 **한국어 infeasible 거부셋** 제작(§4).
- **상시:** t10 적대 홀드아웃 자체 제작(KoBBQ 프로토콜). 외부에서 살 수 없으므로 리드타임이 가장 길다.

### 하네스 통합 방식(제안)

`e2e/manifest_schema.md`의 `tier` 필드에 **`X`(external)** 를 추가하고, 기존 Tier A/B 결론과
**절대 합산하지 않는다**. X-tier는 별도 표로 보고하며, 각 item에 `external_source`(HF id + revision)와
`license`, `contamination_risk`를 필수 필드로 둔다. 데이터 자체는 저장소에 커밋하지 않고
다운로드 스크립트 + `sha256` 고정만 커밋한다(재배포 금지 자산 다수).
