# X1 — Fugu 원문 평가 대조 + 도메인 정확도 벤치마크 확장 기획

> 작성 2026-07-23 · 상태 **제안(미승인)** · 리서치 2갈래(Fugu 원문 fetch · 엔터프라이즈 에이전트 벤치 서베이) 실측 조사.
> `x0-external-dataset-plan.md`의 후속 — X0는 문항 데이터셋(RAG/SQL/라우팅/판정자), 본 문서는
> **업무-태스크형 벤치마크**(상태-기반 채점)와 **Fugu 원문과의 방법론 대조**를 다룬다.

## 1. Fugu 원문(arXiv:2606.21228) 실제 평가 설정 — 검증 완료

repo 인용과 원문 일치 확인(Sakana AI, 2026-06-19 제출). 핵심:

- **평가 스위트(전부 범용 held-out)**: SWE Bench Pro · Terminal Bench 2.1 · LiveCodeBench v6 ·
  GPQA-Diamond · HLE(2,500) · SciCode · CharXiv · τ³ Banking · MRCRv2(128k). 선택기 학습 데이터
  (SFT 단일스텝 컬렉션 + Claude Code/Codex 실궤적 + RL 환경)와 분리해 "미학습 벤치"로 일반성 주장.
- **워커 풀**: Claude-Opus-4.8 / GPT-5.5 / Gemini-3.1-Pro. **선택기**: Fugu(SFT soft분포 KL +
  sep-CMA-ES, 쿼리당 단일 워커) / Fugu-Ultra(GRPO RL, ≤5스텝 동적 워크플로).
- **채점**: pass-rate/accuracy 위주, judge는 CharXiv 한 곳(gpt-4o, 위치스왑 등 안전장치 언급 없음).
  **유의성 검정 전무**, 대부분 단일 런.

### 우리 스위트 대비 — 대회 보고서에 쓸 3가지 사실

1. **"single best를 이긴다"에 예외 존재**: MRCRv2는 GPT-5.5 단독 > Fugu-Ultra, 지연최적 Fugu는
   SWE에서 Opus 단독에 큼(59.0 vs 69.2). → 우리 "조립 ≈ 최선 단일" 발견과 결이 맞다.
2. **rule vs learned 어블레이션이 논문에 없다** → 우리 "(c) 학습선택 기각, (b) 규칙 승" 발견을
   원 논문이 반박하지 못한다(비교 부재).
3. **통계 엄밀성은 우리가 강하다**: 우리 = McNemar 쌍대 + 위치스왑 판정 + 사전선언 vs 그쪽 =
   검정 없음·단일 런·judge 무장치. 보고서 문구: *"방법론은 원 논문보다 엄격하게 적용했다."*

| Fugu 스위트 | 우리 대응 메트릭 |
|---|---|
| GPQA/HLE/CharXiv | wiki_qa(근거 그라운딩 판정) |
| τ³ Banking(tool+user-sim) | routing + agent-work typed action(결정론 gate) |
| SWE/Terminal/LiveCodeBench | (미측정 — 위임 루프 runner는 있으나 벤치 아님) |
| MRCRv2 | (부분) 임베딩 retrieval MRR |
| 학습 셀렉터 자체 | ASSIGNMENTS 결정론 상수 테이블 |

## 2. 도메인 정확도 확장 — 채택 후보 (전부 페이지 직접 검증)

**채택 기준(횡단 관찰에서 도출)**: ① 결정론(상태-기반) 채점 — judge 최소화 ② 태스크 모양이 우리
업무(메일·문서·위임·다단계 플로우)와 일치 ③ **지시문만 번역하면 채점기가 언어 무관** → 한국어화 비용 낮음.

| 우선 | 벤치마크 | 라이선스 | 채점 | 우리 매핑 | 비용 |
|---|---|---|---|---|---|
| **P0** | **WorkBench** (COLM'24, 690 tasks, 5 샌드박스 DB: email/calendar/analytics/CRM/project) | MIT | **결정론** — 최종상태 vs GT, 유해부작용 3분류 | mail→reply·플로우·tool-use — **모양 최일치, 하네스 최경량** | 낮음 (지시문 번역만) |
| **P0** | **BFCL v3 multi-turn** (1,000건: base 200 + missing-param/func 등 800) | 공개 | **프로그램적** — 턴별 상태+호출 subset 매칭 | tool-call 정확도 + **missing-param=request_more_data 행동** (B3와 직결) | 낮음 |
| **P1** | **AppWorld** (ACL'24 BRA, 9 apps/457 APIs/~750 tasks) | Apache-2.0 | **결정론** — DB unit test + 부수피해 감점 | 다단계 플로우·위임완수 검증의 최엄밀 채점기 | 중간 (소비자앱 도메인 갭) |
| **P1** | **Spider 2.0 Lite-SQLite** (135건 무료 슬라이스) | MIT | 실행 기반 | NL→SQL **상한선** 측정 (실기업 DW 난이도) | 중간 |
| **P2** | **EnterpriseRAG-Bench** (Onyx '26, 합성 사내문서 50만 + 500Q, Info-Not-Found 20·Conflicting 20 포함) | MIT | **README 미명시(judge 추정) — 채택 전 확인 필수** | wiki-grounded QA + 우리 R2 추상화·K8 conflict와 카테고리 정합. 2026 신작 = **오염 최소** | 중간 |
| **P2** | **τ²-bench** (airline/retail/telecom/banking) | MIT | DB 상태 결정론 + LLM user-sim(분산·비용) | 정책 준수형 multi-turn = policy gate 흐름 동형 | 중간 |

### 프로토콜만 빌릴 것

- **EnterpriseRAG-Bench의 문항 카테고리 설계**(Info-Not-Found / Conflicting / metadata 의존) —
  코퍼스 재생성 없이 **우리 wiki 위에 카테고리 구성비만 이식**하면 B3-R2를 확장할 수 있다.
- **AppWorld의 부수피해(collateral damage) 감점** — Flow Bench(⑥) 채점에 "완수했지만 다른 상태를
  망가뜨렸는가" 축 추가 근거.
- **WorkBench의 3분류**(성공/무해실패/유해부작용) — 우리 실패 귀속에 "유해성" 차원 추가.

### 제외 (사유 명시)

| 대상 | 사유 |
|---|---|
| CRMArena/-Pro | **CC BY-NC 4.0** — 대회/상용 맥락 법무 확인 전 배제 (fit은 좋음) |
| WorkArena | ServiceNow 인스턴스 + gated + 웹 UI 조작 — 표면 불일치 |
| GAIA | 웹검색/파일 일반 비서 — 사내지식 아님, gated |
| Spider2-V | VMware VM + GUI 멀티모달 — 불일치 |
| emailbench(Proofpoint) | MCQA 이메일 **지식** 퀴즈 — 드래프팅 평가 아님 |
| ProcessBench(Qwen) | 이름과 달리 **수학 추론 오류 탐지** — 무관 |
| TechQA | 2020년작 오염 높음 + IBM 제품 도메인 |
| BEAVER | 라이선스 미확인(채택 전 확인) — 보류 |

### 확정 결론 2개 (조사가 닫은 질문)

1. **이메일 초안/답장 "생성"을 결정론 채점하는 공개 벤치는 없다** — 상태 검증(발송됐나)은 WorkBench가
   하지만 내용 품질은 아니다. 자체 골든셋 + Arena 판정(우리가 이미 하는 방식)이 정답.
2. **한국어 기업 에이전트 벤치는 공백 지대** — 상태-기반 벤치를 한국어 지시문으로 이식하는 것 자체가
   기여가 된다(채점기 언어 무관).

## 3. 실행 제안 (owner 결정)

- **1차**: WorkBench 한국어 지시문 이식(P0) — 690 tasks 중 이메일·캘린더 슬라이스 ~200부터.
  조립(배정표) vs 프론티어를 그 위에서 재측정 → ⑥의 외부판. 사전선언 필수.
- **병행**: BFCL v3 multi-turn을 국내 3사 + Sonnet에 — B2 C6(tool-call)·B3(request_more_data)의
  외부 코로보레이션.
- **보류**: EnterpriseRAG-Bench는 채점 방식 확인 후, τ²는 user-sim 비용 검토 후.
- 오염 참고: 상태-기반 벤치는 정답 암기보다 trajectory 능력이라 **오염에 상대적으로 둔감** — 그래도
  BFCL(공개 문항)은 closed-book류 통제 불가라 부호 확인용으로만.
