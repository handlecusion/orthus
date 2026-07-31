# B4-X2 / B4-X3 결과 — 외부 타당성 (grounded QA + 한국어 스키마 게이트)

> 실행 2026-07-22 · 사전선언 `b4-prereg.md` §3(X2·X3)·§4 + `x0-external-dataset-plan.md` §3.2·§3.4·§5.
> 러너(standalone, `harness_e2e.py` 미사용): `x3_korwikitq.py`, `x2_allganize.py`.
> 판정 모델 = **solar 단일**. gpt-4o-mini는 이 창에서 OpenAI 429로 실행 불가(§가용성).
> **ax(A.X)·Bedrock 미사용**(B1 리소스 경합 회피). raw는 `analysis/raw/`(gitignored).

## 0. 한 줄

- **X3(한국어 스키마 게이트 통과율):** 외부 한국어 위키 표 200개에서 **게이트 통과 99.5% ·
  한국어 컬럼 링킹 96.5% · 실행 96.0%** — 내부 t3 게이트 통과 **100%**와 **같은 방향(부호 일치)**.
  → 내부 결론 "게이트 통과율 우수"가 **우리 스냅샷 밖 한국어 스키마에서도 유지**된다.
- **X2(외부 grounded QA):** option (a) 직접 실행은 이 창에서 **비현실적**(원문 문서 미동봉·
  OpenAI 임베딩 429·B1 경합). 프리레그가 명시한 **더 작고 깨끗한 option (b)** 실행 —
  판정자-사람 코로보레이션 **일치율 85.8% · Cohen's κ = 0.717**(substantial), 판정자는
  보수적(사람-오답을 정답으로 통과시킨 오탐 5/120).

---

## X3 — KorWikiTQ 한국어 스키마 게이트 통과율 (headline)

### 데이터셋 · 라이선스

- **KorWikiTQ** (LG-NLP, `github.com/LG-NLP/KorWikiTableQuestions`, **CC-BY-SA-4.0**).
  dev split(Git LFS, `KorWikiTQ_ko_dev.json`, 49MB, sha256 `b44b1e60…`)를 받아 슬라이스.
  11,771 dev 엔트리 / 3,012 유니크 표. 각 엔트리 = 한국어 위키 표 1개(`TBL`: 헤더행+데이터행) + QA 1쌍.
- **재배포 안 함** — `.cache/`(gitignored)에만 두고 해시만 남긴다(`external/X2X3.MANIFEST.sha256`).
  CC-BY-SA는 파생 공개 시 동일조건 전파 → 사내 평가 한정(§6).

### orthus_x3 셋업 (격리 확인)

- **자체 스크래치 DB `orthus_x3`** 를 `orthus_pg` 컨테이너에 생성(`createdb`). B1이 읽는
  `orthus_company_0706`을 **건드리지 않는다.**
- `orthus_company_0706`에서 `notion_rows`/`structured_rows`/`query_runs`/`audit_log` **4개 테이블만
  schema-only dump** → 외부 FK(`users`/`documents`) 제거 후 orthus_x3에 적재(스크래치라 FK 불필요).
- **`.env` 미수정.** 러너가 이 프로세스의 `os.environ["ORTHUS_PG_DSN"]` /
  `ORTHUS_PG_DSN_READONLY`만 orthus_x3로 돌린다(orthus 임포트 전 설정 → `get_settings()` lru_cache가
  orthus_x3로 해석). production 코드(`query_structured`) 무수정.
- 실행 role은 `orthus`(스크래치라 별도 `orthus_ro` grant 없음) — read-only는 executor의
  `postgresql_readonly=True` 커넥션 옵션이 강제(이중방어 앱-게이트 층은 그대로). 게이트 통과율
  측정에는 role 무관(honest caveat).

### 슬라이스 · 적재

- 결정론 선택(seed 1234): >=3열·>=4행·유니크 헤더 필터 후 **표당 최대 2문항**으로 **200문항 / 190 유니크 표**.
  헤더 mean 5.9열(5–10), 데이터 mean 13.3행(5–20, median 12).
- **표 1개 = notion_rows JSONB DB 1개.** 한국어 헤더 → `properties` 키(= 스키마 링킹 대상).
  **단일-표 스코프**로 적재(질문마다 그 표만 로드) → 순수 **한국어 컬럼 링킹**을 잰다.
  (여러 표 중 db_name 선택은 별개 retrieval 관심사라 이 슬라이스 범위 밖.)

### 결과 (solar, n=200)

| 지표 | 값 | 의미 |
|---|---|---|
| **게이트 통과** | **199/200 (99.5%)** | parse·SELECT-only·schema_ok·read_only·LIMIT·**EXPLAIN** 전부 통과 |
| **한국어 컬럼 링킹** | **193/200 (96.5%)** | SQL이 참조한 `properties->>'키'`가 전부 실제 표 헤더 |
| 실행 성공(status=executed) | 192/200 (96.0%) | 게이트 통과 SQL이 PG에서 무오류 실행 |
| 근사 답 일치(answer_hit) | 59/200 (~30%) | gold 답 문자열이 결과 셀에 등장(**근사**, 손저술 gold 아님) |
| 게이트 거부 | 1 | `explain_failed`: 없는 컬럼 `generation` → **EXPLAIN이 정확히 거부**(게이트 작동) |
| 실행 실패(게이트는 통과) | 7 | 유효 SELECT였으나 PG 런타임 오류(타입 아틀라스 등) — 게이트 실패 아님 |

### 해석 (중요 — 게이트가 무엇을 검증하는가)

- `schema_ok`는 **테이블/기본컬럼** 존재만 검증하고 **`properties->>'<키>'`의 키는 데이터로 취급**한다
  (설계상: 키는 노션 속성명이라 컬럼이 아님). 따라서 **한국어 헤더 환각은 게이트를 통과하고
  실행 시 0행으로 드러난다** — 게이트 통과율이 구조적으로 높게 나오는 이유.
- 그래서 "한국어 스키마 링킹이 실제로 됐나"의 진짜 지표는 별도 결정론 신호 **schema_link
  (참조 키 ⊆ 실제 헤더) = 96.5%**다. 7건만 없는 한국어 키를 지어냈다.
- `answer_hit ~30%`가 낮은 것은 **게이트/스키마 문제가 아니라** 값 포맷 불일치(질문 "2014년 11월 20일"
  ↔ 셀 값 포맷) + 셀-룩업형 질문이 우리의 집계-지향 파이프라인과 형태가 다르기 때문. 프리레그가
  "gold SQL 불필요, 게이트 통과율이 headline"이라 못박은 근거가 이것이다. answer_hit은 **참고용 근사**.

### 부호 대조 (sign check)

```
내부:  게이트 통과율 우수                    →  외부 X3(한국어 스키마)에서도 유지?
내부 t3 게이트 통과(orthus_company, n=69):  100.0%
외부 KorWikiTQ 게이트 통과(orthus_x3, n=200): 99.5%      →  ✅ 부호 일치
외부 한국어 컬럼 링킹:                        96.5%      →  높음 유지
```

- 내부 기준선은 **`orthus_company`**(B1의 `orthus_company_0706` 아님)에 t3 골든(28)+홀드아웃(41)=69문항을
  같은 production 경로로 태워 **게이트 통과 100%**. 내부/외부 둘 다 한국어 스키마인데(회사 노션 vs 위키 표),
  게이트 통과율이 사실상 동일 → **결론이 스냅샷에 국한되지 않음**을 지지.
- **크기는 주장하지 않는다**(프리레그 §5). 도메인이 달라 부호만 본다.

---

## X2 — allganize 외부 grounded QA

### feasibility 조사 (실행 전 보고 — 프리레그가 요구한 판단)

allganize/RAG-Evaluation-Dataset-KO(**MIT**, 300문항, 금융/공공/의료/법률/커머스 각 60)는
`question` · `target_answer` · `target_file_name` · `target_page_no` · `context_type` + **23개 시스템
× (answer, O/X 사람채점)**을 동봉한다(HF `rag_evaluation_result.csv`, sha256 `b78fd2bc…`).

- **option (a) 외부 도메인 grounded QA 직접 실행 — 이 창에서 비현실적.**
  1. 데이터셋은 **원문 문서를 포함하지 않는다.** `documents.csv`는 파일명 + **외부 gov/finance URL**
     (bok.or.kr·fsc.go.kr·kofia.or.kr의 `view.do` 포털 링크, 직접 PDF 아님)만 준다. 원문은 별도 수집 필요.
  2. 우리 `ask()`는 **compiled wiki page에만 그라운딩**한다(raw-chunk RAG 금지 — 절대룰).
     따라서 raw 컨텍스트 주입 shortcut이 불가하고, PDF ~60건 수집→파싱(pypdf)→corpus 인덱싱→
     **embedding**→distill/consolidate **LLM wiki 저작**을 전부 거쳐야 `ask()`가 답할 수 있다.
  3. 지금 **OpenAI 임베딩/chat이 429**(text-embedding-3-small·gpt-4o-mini 동일 키)로 막혀 있고,
     동시 실행 중인 B1 리소스 규칙과 충돌한다. → 수 시간짜리 LLM+임베딩 파이프라인은 **강행하지 않는다**
     (프리레그: "measure what X2 intends하지 않는 실행을 강행하지 마라").
- **option (b) 판정자-사람 코로보레이션 — 실행함(더 작고 깨끗).**
  동봉된 사람 O/X를 재활용해 **판정자가 한국어 외부 도메인에서 사람과 얼마나 일치하나**를 잰다
  (프리레그 §3 X2가 명시한 X1 보조검증 경로, G-JUDGE 외적 타당성).

### option (b) 설계 · 결과 (solar, n=240)

- 6,900 시스템-답(4293 O / 2607 X, 5도메인 균형)에서 **사람 O/X 균형**(120 O / 120 X) + 도메인 분산
  결정론 샘플. allganize 라벨이 **포인트와이즈 정오**라, production pairwise 판정자 대신 **포인트와이즈
  O/X 프롬프트**(질문+참조답+후보답 → O/X)를 solar로 실행(라벨 공간 일치).

| 지표 | 값 |
|---|---|
| 일치율(agreement) | **206/240 (85.8%)** |
| **Cohen's κ** | **0.717** (Landis-Koch "substantial") |
| 무효/오류 | 0 |

혼동행렬 (행=사람, 열=solar 판정):

|          | judge O | judge X |
|----------|--------:|--------:|
| human O  | 91 | 29 |
| human X  | **5** | 115 |

도메인별 일치율: finance 92.5% · commerce 90.6% · public 88.2% · law 82.5% · medical 74.4%.

- **판정자는 보수적**이다: 사람이 오답(X)이라 한 것을 정답(O)으로 통과시킨 **오탐이 5/120뿐**
  (품질 게이트로서 바람직한 방향). 대신 사람-정답 29건을 오답으로 깐다(엄격). medical이 가장 어렵다.
- κ=0.717은 **판정 승률이 사람 판단의 대리지표로 쓸 만함**을 외부 한국어 도메인에서 지지한다.
  단 이건 **판정자를 검증하지 우리 답변을 검증하지 않는다**(프리레그 §5.3). 프로덕션 판정자는
  gpt-4o 기반이고 여기 solar는 대체 판정자이므로, 이 κ는 "국내 모델을 판정자로 써도 사람과
  substantial 일치"라는 별개의 부수결론이다.

### 오염 / closed-book 노트

- **closed-book 오염통제는 option (a) grounded QA용이다**(컨텍스트 없이 맞히는 문항 제외, §4 임계 30%).
  option (b) 판정자는 **참조 정답을 보고** 판정하므로 closed-book 제외가 해당 없음 — 제외율/30% 임계 미적용.
  (option (a)를 실행했다면 finance/public 다수가 파라메트릭 지식으로 답 가능해 제외율 점검이 필수였을 것.)
- X3는 **게이트 통과율**(SQL 유효성/스키마 링킹)을 재지 사실 지식을 재지 않으므로 오염 민감도가 낮다.
  KorWikiTQ 위키 표가 국내 모델 사전학습에 포함됐더라도 "유효한 한국어-스키마 SQL을 쓰는가"는
  암기로 부풀지 않는다. (answer_hit은 참고용이라 오염 영향도 참고용에 그친다.)

---

## 스위트 부호 판정 기여

| 서브셋 | 내부 방향 | 외부 방향 | 부호 |
|---|---|---|---|
| **X3** | 게이트 통과율 우수(100%) | 한국어 스키마에서도 99.5% / 링킹 96.5% | **✅ 일치** |
| **X2** | (option a 미실행 — grounded QA 동률 판정 불가) | 판정자-사람 κ=0.717(보조검증) | — (판정자 타당성 데이터포인트) |

- X3는 프리레그 §3 스위트 부호판정의 3항목 중 "게이트 통과율" 항을 **부호 일치**로 채운다.
- X2는 grounded-QA 동률 부호(국내≈프론티어)를 재려면 option (a)가 필요하다. 이 창에서는 미실행이므로
  **그 부호는 미결(pending)**로 남기고, 대신 G-JUDGE 외적 타당성(κ=0.717)을 보고한다.

---

## 재현 · 격리 확인

```bash
# X3 (headline)
.venv/bin/python experiments/fugu-ko/x3_korwikitq.py --n 200 --models solar
.venv/bin/python experiments/fugu-ko/x3_korwikitq.py --internal --models solar   # 내부 부호대조
# X2 (option b)
.venv/bin/python experiments/fugu-ko/x2_allganize.py --n 240 --model solar
```

- **orthus_x3 는 별개 스크래치 DB** (`orthus_pg` 컨테이너, notion_rows/structured_rows/query_runs/audit_log
  4테이블). B1의 `orthus_company_0706` **미접근**(내 DSN은 orthus_x3 + 내부대조용 orthus_company뿐).
- **`.env` 미수정** — `git diff .env` 공란. DSN 오버라이드는 러너 프로세스의 os.environ 한정.
- **ax·Bedrock 미사용.** gpt-4o-mini는 OpenAI 429로 이 창에서 실행 불가(재시도·백오프에도 지속) →
  X3/X2 모두 solar 단일 보고. 429 해소 시 두 러너에 `--models solar,gpt` / `--model gpt`로 추가 가능.
- 외부 데이터 행 **미커밋**(gitignored `.cache/`). 해시: `external/X2X3.MANIFEST.sha256`.
  라이선스: KorWikiTQ = CC-BY-SA-4.0(사내 평가 한정), allganize = MIT.
