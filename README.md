# Orthus — Solar로 자기저작(self-authoring)하는 팀 지식 아카식 + 지식그래프 에이전트

> **Upstage Solar 에이전트 아이디어 대회 제출작.** 이 레포는 Upstage **Solar를
> 유일한 LLM 백엔드**로 쓰는 에이전틱 지식 시스템이다 — 팀의 문서·대화가
> Solar로 **위키(메모리 레이어)** 와 **지식그래프**로 컴파일되고, 에이전트는
> 그 위에서 **검증 게이트를 통과한 도구만으로** 검색·질의·행동을
> 오케스트레이션한다. MCP/CLI로 감싸져 있어 어떤 외부 에이전트든 이 지식
> 베이스를 도구로 쓸 수 있다.

## 한 줄 요약

**"RAG 대신 위키를 쓴다"** — raw chunk를 그때그때 프롬프트에 붓는 대신, Solar가
문서를 클레임 단위로 증류(distill)하고 모순을 관리하며(consolidate) 위키
페이지로 굳힌다. 답변은 항상 이 **컴파일된 위키 페이지에서만** 그라운딩되고,
관계형 질문은 위키에서 결정론적으로 투영된 **지식그래프**가 받는다. LLM은
오케스트레이션과 컴파일만 하고, 실행은 전부 typed guard가 통제한다.

## 이런 게 됩니다 — 유즈케이스

> 아래는 시스템의 실제 동작 경로를 보여주는 예시 흐름이다(응답 문구는 예시).
> 공통점: **Solar는 판단하고, 실행은 전부 결정론 게이트가 한다.**

**U1. 팀 지식 즉답 — 출처까지**

```
Q: "신규 입사자 온보딩 절차 어떻게 되지?"
→ 라우터가 지식형으로 분류 → 컴파일된 위키 페이지에서 그라운딩
A: "온보딩은 3단계로 … [출처: wiki/온보딩-절차 §2, wiki/장비-신청]"
```
raw chunk가 아니라 사람이 읽고 고칠 수 있는 위키 페이지가 근거라서,
답이 틀리면 **페이지를 고치면 된다** — 프롬프트를 고치는 게 아니라.

**U2. 자연어 집계 — SQL은 게이트 뒤에서만**

```
Q: "업무 보드에서 상태별 티켓 개수랑 담당자별 개수 알려줘"
→ 복합질문 감지 → 2조각으로 decompose → 각각 NL→SQL 컴파일
→ sqlglot 게이트: SELECT-only ✓ 스키마 실재 ✓ LIMIT 주입 ✓ EXPLAIN ✓
→ DB read-only 롤로 실행. 결과가 0행이면? SVC가 solar-mini로 한 번 더
  검증해 가짜 0을 회수한다 (실측 77.8% → 100%)
```
"직원 다 지워줘"라고 하면? 컴파일은 되지만 게이트가 거부한다 —
거부 5종은 회귀 테스트로 고정돼 있다.

**U3. 관계 질문 — 지식그래프가 받는다**

```
Q: "A 프로젝트랑 B 정책이 무슨 관계야?"
→ graph 분기 → Solar가 의도+명사구만 추출 → typed 템플릿(path_between)으로 질의
A: "A 프로젝트 → (roadmap 문서) → B 정책 — 2-hop 경로, 근거 페이지 3건"
```
그래프는 위키에서 **LLM 0회** 결정론 투영으로 만들어지고(멱등), raw Cypher
입력 경로가 코드에 존재하지 않는다. 웹의 d3-force 탐색기로 더블클릭 탐색.

**U4. 에이전틱 검색 — Solar가 도구를 오케스트레이션**

```
Q: "다음 주 회의 일정 확인해서 안건 초안 잡고 담당자한테 메일 초안까지"
→ Solar function calling 루프:
   team_schedule(다음주) → wiki_ask(지난 회의 결정사항) → mail_compose(초안)
A: 일정 요약 + 안건 초안 + 메일 초안 카드(발송 아님 — 사람이 '보내기'를 눌러야 발송)
```
루프는 fail-open(예외가 나도 요청이 죽지 않음), 발송·쓰기 같은 커밋 액션은
결정론 policy gate(`auto_execute | draft_for_review | request_more_data | reject`)를
통과해야만 실행된다.

**U5. 내 에이전트에 꽂기 — MCP 한 줄**

```json
{ "mcpServers": { "orthus": { "command": "orthus", "args": ["mcp", "serve"] } } }
```
Claude Code·Codex 같은 외부 에이전트가 `wiki_search`/`wiki_ask`/`kg_relations`/
`structured` 도구로 팀 지식 베이스를 그대로 쓴다. 같은 게이트, 같은 감사 로그.

**U6. 지식이 스스로 자란다 — 모순은 숨지 않는다**

```
새 문서 저장 → Solar distill이 클레임 추출(실측 8.4 claims/doc, 정밀도 100%)
→ consolidate가 위키 페이지에 병합
→ 기존 클레임과 모순? silent overwrite 금지 — WikiTask로 표면화 → 사람이 결정
```

## 벤치마크 — 숫자로 보기

전국민 AI 경진대회(AI Champion) 중간결과보고서(2026-07)에 제출한 실측이다.
합성 벤치 전용 데이터가 아니라 **실운영 아카이브**(위키 claim 12,123건 · 문서
2,480건) 위에서, 골든셋 1,884문항 + 사전선언·블라인드 채점·McNemar 쌍대 검정
절차로 측정했다. 재현 하네스는 [`experiments/`](experiments/)에 그대로 있다.

### 헤드라인: 성능을 지배하는 것은 모델이 아니라 하네스

같은 판정셋(자연어→SQL, n=1,438~1,483)에서 모델은 고정한 채 결정론 검증
규칙(하네스)만 쌓았을 때:

```
Solar 단독(bare)      72.8%  ─┐
Solar + 하네스 5단     98.6%  ─┴─ +25.8%p   ← 모델 교체 효과는 ±2%p에 그친다
```

| 측정 | 결과 |
|---|---|
| 자연어→SQL 정확도 (n=1,438) | Solar 단독 72.8% → **하네스 적용 98.6%** (+25.8%p) |
| vs 파인튜닝 | A100 SFT 학습기 94.3% vs **규칙 스택 98.8%** (McNemar p=3.6e-12) — GPU 학습 없이 우위 |
| **이메일 생성 아레나** (9모델 라운드로빈 2,160표) | **Solar+하네스 78.3점 — 1위.** Claude Opus 4.8(70.4)·GPT-5.3(60.8) 전부 제침. 같은 Solar가 하네스 없이는 41.0점 — 순위를 바꾼 건 벤더가 아니라 구조다 |
| E2E 다단계 업무 완수율 (168문항) | 하네스 조립 **98.2%** — 2위 해외 프론티어 91.1% (+12문항) |
| 실사내 6작업 정확도 (n=200) | 프론티어 9종(Opus·GPT-5.3·Sonnet·DeepSeek…) 중 유의하게 앞선 비교군 **0종** |
| 응답 지연 p50 | **417~746ms** vs 프론티어 1,612~2,061ms — **3.2~4.6배** 빠름 |
| 질문당 원가 | Solar **0.33원** vs GPT-4o 약 5.6원 · Sonnet 약 6.6원 |
| embedding (Solar `embedding-passage`) | 검색 MRR **+0.080** (p=0.0001) · 질의 p50 **241ms → 69ms** (−71%) — 코드 변경 **0줄** |
| 위키 저작 (distill) | 근거 이탈률 9.4% → **0.6%** · 8.4 claims/doc @ 정밀도 100% |
| 안전 | 쓰기·DDL 오실행 **0건** · 적대 함정 24건에서 정상 위임 오차단 **0건** · 거부 5종 회귀 고정 |
| 검증 캐스케이드 (SVC) | 결정론 트리거 + 2차 재질의로 structured 오답 회수, 적대 표본 오발동 13.6% → **0%** |
| 테스트 스위트 | **2,500+ passed** — mock 슬롯 고정, 네트워크 0회 재현 |

> 보고서 원측정은 국내 3사 모델 조립이었다. 그런데 같은 측정이 **슬롯별 모델
> 선택의 효과는 1,750문항 중 17건(약 1%p), 모델 교체 효과는 ±2%p**임을 함께
> 보여줬다 — 이득의 출처는 하네스다. 그래서 이 공개 빌드는 하네스를 그대로 둔
> 채 전 슬롯을 **Solar 단일**로 수렴시켰다(가장 빠르고, tool-calling이
> 네이티브고, JSON 실패 0건인 후보).

### 측정이 기각한 것들 — 이게 이 프로젝트의 진짜 차별점이다

- 작업별 **멀티모델 분할 배정**(+7.7%p로 보였던 초기 결론) → 홀드아웃에서
  유의차 소멸 → Solar 단일화
- 질문 단위 **학습 셀렉터** 3티어(TF-IDF·RoBERTa·LoRA까지 훈련) → 홀드아웃
  463문항에서 규칙표를 유의하게 앞선 구성 **13개 중 0개** → 폐기
- **파인튜닝**(A100 4장, SFT 1.2B·32B QLoRA) → 규칙 스택에 4.5%p 미달 → 폐기
- 벤더 문서가 권하는 embedding **query/passage 비대칭 배선** → 4/4 조건 유의하게
  나쁨 → 대칭 배선 채택
- distill "커버리지 열세" → 모델 한계가 아니라 **프롬프트 상한 한 줄**이 원인
- 그리고 정직하게: **하네스 없는 자유 에이전트 루프에서는 프론티어가 2~3배
  앞선다**(WildBench-KO·BFCL 교차검증, 자율 루프 70% vs 95~100%). 그래서 이
  시스템은 자유 루프 대신 결정론 스캐폴드의 슬롯에 Solar를 배치한다 — 열세를
  숨기지 않고 설계 결정의 근거로 회수했다.

수치 전체·방법론·재현 절차: [`experiments/fugu-ko/RESULTS.md`](experiments/fugu-ko/RESULTS.md)

## CLI 한 눈에 보기

```
$ orthus --help
usage: orthus [-h] [--json] [--central-url CENTRAL_URL]
              {version,init,connect,wiki,work,mcp,doctor,connector,calendar,myschedule,ticket,whoami,skills,update} ...

    version             print orthus CLI version
    init                configure local Orthus CLI
    connect             브라우저 로그인으로 central 연결 — 토큰 자동 발급 → Keychain 저장
    wiki                central wiki commands        (search · page · ask · suggest)
    work                read Agent Work
    mcp                 MCP server helpers
    doctor              check local Orthus CLI/MCP setup
    connector           manage owner personal connectors on central
    calendar            company team calendar (team schedule)
    myschedule          my personal schedule (owner-private)
    ticket              티켓 (이슈 트래킹) — 인자 없이 실행하면 요약 도움말
    whoami              show my identity + node role
    skills              bundled agent skills (MCP + CLI usage)
    update              self-update the CLI/skills
```

에이전트 친화 표면의 예 — `orthus ticket`은 인자 없이 치면 사람도 에이전트도
그대로 따라할 수 있는 요약 도움말을 낸다:

```
$ orthus ticket
orthus ticket — 티켓 (이슈 트래킹). 기본은 내 보드, --project를 주면 그 프로젝트 칸반.

  orthus ticket ls   [--project 프로젝트] [--status ...] [--date today] [--limit N]
  orthus ticket add  "제목" [--project 프로젝트] [--status 시작전] [--assignee 이름]
  orthus ticket set  <id> [--status ...] [--title ...] [--due ...]
  orthus ticket rm   <id>              # soft: 내 보드=archived(복구 가능), 칸반=거부+안내
  orthus ticket show <id>              # 상세 + 댓글. id는 앞 4자 이상이면 충분
  orthus ticket comment <id> "내용"
  orthus ticket projects               # 채널(프로젝트) + 백로그 버킷 키

id는 저장소를 몰라도 된다 — 내 보드와 담당 프로젝트 칸반을 함께 검색한다.
삭제 명령은 되돌릴 수 있는 것만 있다(rm=archive). 실삭제는 웹에서만.
```

## Solar가 하는 일 (전 슬롯 단일 벤더)

| 슬롯 | 모델 | 역할 |
|---|---|---|
| 위키 저작 (distill/consolidate) | `solar-pro` | 문서 → 클레임 추출·페이지 컴파일 (실측: 8.4 claims/doc, 정밀도 100%) |
| 라우팅/의도/분해 | `solar-pro` | /ask 경로 분류, 명령·질문 판정, 복합질문 decompose |
| NL→SQL (structured) | `solar-pro` | 집계형 질문 컴파일 — sqlglot 검증 게이트 뒤에서만 실행 |
| 그라운딩 QA (wiki_qa) | `solar-pro` | 위키 페이지 근거 답변 + 출처 |
| KG 바인딩 (graph_bind) | `solar-pro` | 관계형 질문 의도+명사구 추출 → 템플릿 게이트 질의 |
| **에이전틱 검색 루프** | `solar-pro` | Solar 네이티브 **function calling**으로 위 도구들을 다턴 오케스트레이션 |
| SVC 2차 검증 | `solar-mini` | 결정론 신호(0행/게이트 실패)가 뜬 답만 재검증 — E1 실측 77.8%→100% |
| **retrieval embedding** | `embedding-passage` | corpus/위키 인덱스+질의 (실측: MRR +0.112, p<0.001, p50 241→69ms) |

작업별 배정은 **결정론 상수 테이블**이다(`orthus/models/orchestration.py`) —
확신도 라우팅도, 학습 셀렉터도 아니다. 학습 셀렉터를 실제로 만들어 붙여봤고,
상수 테이블에 졌다. 측정 전문은 [`experiments/fugu-ko/RESULTS.md`](experiments/fugu-ko/RESULTS.md).

## 아키텍처

```
소스(에디터/커넥터) ──ingest──▶ corpus (chunk→embed→pgvector)      … raw 레이어
                                   │
                        [Solar distill → consolidate]
                                   ▼
                        LLM wiki (claims + pages, markdown SoR)     … 메모리 레이어
                          │                    │
              결정론 투영(LLM 0회)        /ask 그라운딩 (컴파일된 페이지 전용)
                          ▼                    │
                Neo4j 지식그래프           자동 라우터 ──▶ wiki | structured(NL→SQL 게이트) | graph
             (:Page/:Claim/:Entity)            │
                          │            에이전틱 루프 (Solar function calling)
                템플릿 게이트 질의        도구: wiki_search · wiki_ask · structured · kg_relations · …
             (raw Cypher 입력 경로 없음)       │
                          └──────────┬─────────┘
                                     ▼
                    MCP 서버(orthus mcp serve) · CLI(orthus) · Web FE(그래프 탐색기)
```

핵심 설계 결정 세 가지:

1. **위키가 메모리, corpus는 raw.** 답변 그라운딩은 컴파일된 위키 페이지 전용 —
   raw-chunk RAG 경로가 코드에 존재하지 않는다. 모순되는 클레임은 조용히
   덮어쓰지 않고 WikiTask로 가시화된다.
2. **LLM은 컴파일/오케스트레이션, 실행은 결정론 게이트.** NL→SQL은
   sqlglot parse → SELECT-only → 스키마 검증 → LIMIT 주입 → EXPLAIN dry-run을
   전부 통과해야 실행되고, DB 레벨 read-only 롤이 이중 방어한다. KG 질의는
   typed 템플릿 레지스트리로만 나간다. 에이전트 행동(action)은 결정론 policy
   gate가 `auto_execute | draft_for_review | request_more_data | reject`를 최종
   결정한다 — LLM-only 실행은 금지다.
3. **그래프는 위키에서 결정론적으로 나온다.** KG 투영에 LLM 호출이 0회라
   멱등이고, 삭제 수렴은 full rebuild가 권위다. 준실시간 반영은 outbox 패턴
   (같은 트랜잭션 enqueue → worker 적용, 실측 SLA 1.65s).

## 표면 (에이전트가 이 지식을 쓰는 방법)

- **`POST /ask`** — 자동 라우터. 지식형→위키 그라운딩, 집계형→structured,
  관계형→graph. `ORTHUS_AGENTIC_ASK_ENABLED=true`면 Solar function-calling
  루프가 도구를 직접 오케스트레이션한다 (`orthus/router/agentic/`).
- **agent-work 오케스트레이터** — `POST /agent-work/chats/{id}/orchestrate` +
  라이브 SSE. 복합 질문 decompose·read→act·자연어 명령 intake. 명령은 typed
  action이 되어 policy gate를 통과해야 실행된다.
- **MCP** — `orthus mcp serve` (stdio). `wiki_search`/`wiki_ask`/`wiki_page`/
  `kg_relations`/`entity_relations`/`structured` 등 — Claude Code·Codex 같은
  외부 에이전트가 이 지식 베이스를 도구로 쓴다.
- **CLI** — `orthus` (wiki/ask/ticket/mcp …). 에이전트 친화적 오류 메시지와
  멱등 재시도 키를 갖춘 unix 동사 표면.
- **Web** — `/wiki`(위키 홈) · `/ask` · `/agent-work` · d3-force 기반
  **그래프 탐색기**(더블클릭 확장·pan/zoom·hop-ring).

## 빠른 시작

사전 준비: Docker, Python 3.12 + [uv](https://docs.astral.sh/uv/), Node 20+ + pnpm (web 실행 시).

```sh
cp .env.example .env  # ORTHUS_LLM_SOLAR_API_KEY 채우기 (Upstage 콘솔)
make install          # uv sync (Python 3.12)
make up               # docker: postgres+pgvector(:5433) + neo4j(loopback)
make migrate          # alembic upgrade head
make seed             # 데모 유저 + 데모 시드
make wiki-rebuild     # corpus → Solar distill/consolidate → LLM wiki
ORTHUS_KG_ENABLED=true make kg-bootstrap kg-rebuild   # 지식그래프 투영 (.env에서 켜도 됨)
make api              # FastAPI :8820
make web              # Next.js :3820
make test             # pytest — mock 슬롯 고정, 네트워크 0회
```

키 없이도 `ORTHUS_LLM=mock`/`ORTHUS_EMBEDDING=mock`으로 전체 파이프라인이
결정론적으로 돌아간다(테스트/CI가 이 슬롯을 쓴다).

## 안전 모델

1. **애플리케이션 게이트** (`orthus/assistant/validate.py`, fail-closed):
   parse → SELECT-only → 다중 statement 금지 → schema_ok → LIMIT 주입 → EXPLAIN.
   거부 5종(DELETE/UPDATE/다중 statement/환각 컬럼/EXPLAIN 실패)은 회귀 테스트로 고정.
2. **DB 롤**: `orthus_ro` read-only — write는 DB 레벨에서 실패.
3. **KG 게이트**: typed Cypher 템플릿 레지스트리만 — raw Cypher 입력 경로 없음.
4. **policy gate**: 에이전트 action은 결정론 게이트가 최종 결정. LLM 판단은
   입력일 뿐 실행 권한이 아니다.
5. **감사**: 모든 외부 호출(LLM/임베딩/DB)이 `audit()` span + correlation_id.
   PII는 저장 전 `redact_pii_text()` 통과.

## 레이아웃

```
orthus/
├─ api/routes/    ask · wiki · kg · documents · agent_work · …
├─ router/        자동 라우터 + agentic/ (Solar function-calling 루프)
├─ structured/    NL→SQL 게이트 (JSONB store 대상)
├─ assistant/     sqlglot compile · validate · execute (게이트 코어)
├─ wiki/          LLM wiki: distill · consolidate · retrieve · qa · gap
├─ kg/            결정론 투영 · outbox · 템플릿 게이트 · monitor
├─ corpus/        chunk → embed → pgvector
├─ agentwork/     오케스트레이터 + policy gate
├─ mcp/           orthus-mcp stdio 서버
├─ models/        Solar/Mock 어댑터 + 작업별 배정 테이블
└─ audit/         audit() span + redact_pii
experiments/fugu-ko/   측정 하네스 + RESULTS.md (살균판)
wiki-store/            LLM wiki markdown SoR
web/                   Next.js FE (그래프 탐색기 포함)
```

---

## 후기

이 시스템은 원래 "작업마다 제일 잘하는 모델을 골라 쓰는" 멀티모델
오케스트레이션으로 시작했다. 대회를 준비하며 Solar 단일로 좁히는 작업을 했는데,
그 과정에서 오히려 이 프로젝트에서 가장 중요한 교훈들이 선명해졌다.

**1. 측정하기 전의 확신은 대부분 틀렸다.** "작업별 분할 배정이 +7.7%p 이긴다"는
초기 결론은 홀드아웃 재측정에서 기각됐다 — 규칙을 뽑은 골든셋에서 채점한
낙관 편향이었다. n=16에서 유의해 보이던 차이가 n=160에서 사라졌고, 벤더 문서가
권하는 embedding 비대칭 배선은 실측에서 4/4 조건 유의하게 나빴고, 모델의
한계로 보였던 distill 커버리지 열세는 프롬프트의 상한 한 줄이 원인이었다.
"지표가 표본보다 먼저다"를 몇 번이고 다시 배웠다.

**2. 단일 벤더는 제약이 아니라 단순화였다.** 유의차가 없는 동점 구간에서
멀티모델은 관리 비용(키 3벌, 요청 속도 제한 3종, 폴백 사다리, 벤더별 JSON 모드
버그)만 남긴다. Solar 단일화로 폴백 사다리가 한 단으로 줄었고, tool-calling이
네이티브로 동작하는 덕에 에이전틱 루프를 별도 어댑터 없이 OpenAI 호환
프로토콜 하나로 밀 수 있었다. embedding까지 같은 계정 하나로 끝난다.

**3. LLM에게 오케스트레이션을 주고 실행을 주지 않는 구조가 끝까지 살아남았다.**
개발 내내 모델이 몇 번 바뀌었지만(그리고 이번에 전부 Solar가 됐지만) sqlglot
게이트·KG 템플릿 게이트·policy gate는 한 줄도 바뀌지 않았다. 모델 교체가
"어댑터 설정 변경"으로 끝나는 건 이 경계 덕분이다. 반대로 delegation처럼
오탐 1건의 비용이 큰 슬롯에서는 어떤 모델도(Solar 포함) 유일한 가드가 되면 안
된다는 것도 실측으로 확인했다 — 그래서 결정론 가드 두 겹이 모델 뒤에 서 있다.

**4. 위키-우선(RAG 아님)은 에이전트를 위한 선택이었다.** raw chunk는 사람이
검증할 수 없지만 위키 페이지는 팀이 읽고 고칠 수 있다. 에이전트의 기억이
사람이 감사할 수 있는 형태로 존재한다는 것 — 그게 이 프로젝트가 지식 시스템에
대해 하고 싶은 제안이고, Solar는 그 기억을 쓰는 필경사 역할을 훌륭하게 해냈다.

이 공개판을 만들며 사내 데이터(golden set 원문, 시드, 내부 리포트)는 전부
제거했다. 측정 하네스는 남겨뒀으니, 자기 팀의 위키로 golden을 다시 만들면 모든
수치를 재현할 수 있다.
