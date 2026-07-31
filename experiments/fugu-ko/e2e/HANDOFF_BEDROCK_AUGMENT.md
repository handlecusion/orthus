# 골든 증강 핸드오프 — Bedrock 비교군外 모델 기반 신규 문항 생성 (다른 세션용)

> 단일 진입 문서. 작성 시점 2026-07-22, worktree `.worktrees/golden-expand`
> (branch `feat/golden-expand-324`) 기준. **이 세션은 계획/조사만 하고 실행은
> 하지 않았다** — 사용자가 "아직 진행하지 마"라고 명시적으로 멈췄다. 다음
> 세션은 §2(미해결 분기점)부터 먼저 사람에게 확인하거나 판단한 뒤 진행할 것.

---

## 0. 왜 이 문서인가

`HANDOFF_GOLDEN_EXPANSION.md`(같은 디렉터리, 기존 문서)의 목표는 "n=145 →
더 큰 골든셋으로 11모델+조립 V1/V2 재검증"이었고, 그 문서가 시킨 절차대로
**기존 tier_b holdout 자산을 dedup-merge해서 tier_a로 편입**하는 방식으로
이미 n=145→324까지 진행됐다(커밋 `511c0de7`). 이 문서는 그 다음 단계 —
**기존 자산 재활용이 아니라 진짜 신규 문항을 모델로 생성**하는 작업의
설계와 현재 조사 결과를 남긴다. 두 작업은 같은 worktree/브랜치를 공유하므로
순서와 상태를 반드시 이 문서로 동기화할 것.

---

## 1. 현재 상태 (사실관계, 재도출 불필요)

### 1.1 worktree/브랜치
```
경로: <repo>/.worktrees/golden-expand
브랜치: feat/golden-expand-324 (main에서 분기, 아직 main에 미머지)
```

### 1.2 커밋된 것 (`511c0de7`, 2026-07-21)
- `build_manifest.py`가 tier_b holdout을 질문 텍스트 dedup(base 우선)으로
  scored tier_a에 편입 → **t3 28→69, t5 21→76, t6 20→47, t7 22→46(주의:
  scored 기준. tier_a 전체 t7은 142), t9 32(변화 없음), t10 22→54, 합계
  scored n=324**.
- `combine_stats.py`/`slot_swap_exp.py` 소스를 fresh `raw/e2e_<slug>.jsonl`로
  재지정, `freeze.lock`/`tier_b.jsonl` 재생성 완료, sanity 상수(264/p=0.1338)
  145→324로 갱신, 게이트 PASS.
- 결과: 조립 V1/V2 vs 최강 단일 여전히 동률, V1 vs V2 여전히 동률(p=0.375),
  단 V2가 baseline을 유의하게 앞섬(p=0.0192, n=145엔 없던 신규 유의),
  상위군/중위군 분리가 새로 드러남. 전문은 커밋 메시지 참조
  (`git log -1 --format=%B 511c0de7` 또는 `experiments/fugu-ko/analysis/e2e_report.md`).

### 1.3 커밋 안 된 dirty 변경 (다음 증분, **미완성·미검증**)
`build_manifest.py`에 `_routing_extra_items()`가 추가돼 t5 빌더가
`routing_holdout.json`(main 서브셋 290개, control 40개는 필드 다름
`rule_route`라 제외), `routing_graph_golden.json`(main 63개), `routing_holdout_tn.json`
(flat list 40개)까지 dedup-merge하도록 바뀌어 있다. 그 결과:

```
현재 working tree: t5 76 → 469 (다른 task는 커밋 상태와 동일)
```

**이 변경은 다음이 전부 안 됐다** — 다음 세션이 이어받으면 먼저 처리할 것:
- [ ] `freeze.lock` 미재생성 (drift-gate 걸릴 것)
- [ ] `tier_b.jsonl` 이 t5 관련 항목과 정합한지 미확인(위 3개 파일에서 tier_b로
  옮겨간 항목과 새로 tier_a에 들어간 항목 간 dedup 재검증 필요)
- [ ] 카나리아(`--limit 3`) 미실행 — 새 469개 항목이 실제로 deferred 안 되고
  pass/fail로 잡히는지 확인 안 됨
- [ ] 11모델 전량 재실행 안 됨, `combine_stats`/`slot_swap_exp` sanity 상수
  (지금 264/p=0.1338은 n=324 기준이라 t5만 늘어난 새 n에서는 다시 깨짐) 미갱신
- [ ] `routing_holdout.json`의 `control`(40개, `rule_route` 필드) 을 어떻게
  다룰지 결정 안 됨 — 그대로 버릴지, 스키마 변환해서 살릴지
- [ ] 커밋 메시지/`e2e_report.md` 갱신 안 됨

**판단 필요**: 이 dirty 변경을 완성해서 커밋할지, 아니면 §3의 새 Bedrock
증강과 통합해서 한 번에 재계산할지 사람에게 확인하거나 다음 세션이 판단할 것.
이 문서 작성 세션은 이 부분을 건드리지 않았다(git stash/reset 등 아무 조치도
취하지 않음 — working tree 그대로 둠).

---

## 2. 미해결 분기점 (다음 세션이 먼저 볼 것)

1. **§1.3 dirty 변경 처리** — 완성 후 별도 커밋 vs 폐기 vs §3과 통합.
2. **정말 "증강"이 필요한 이유**: §1.2/§1.3 방식(기존 tier_b/routing_holdout류
   자산을 dedup-merge)은 **이미 존재하는 자산을 재분류하는 것**이라 유한하다
   (t5는 이제 사실상 쓸 수 있는 기존 자산을 거의 다 끌어왔다). 사용자가
   원래 요청한 "검증용 데이터셋 증강"(스크럼 회의록 기준 — 5~6개 벤치마크로
   확대, 국내 3사 동률 무리를 가르려면 수천 문항 단위 필요)은 **진짜 신규
   문항을 만드는 것**이라 §3의 생성 파이프라인이 필요하다. 즉 §1.2/§1.3은
   "이미 있던 걸 셈에 넣은 것"이고, §3이 실제 "새로 만드는 것"이다 — 둘을
   합산해 최종 n을 보고할 때 이 구분을 문서에 명시할 것.

---

## 3. 신규 설계: Bedrock 비교군外 모델 기반 문항 생성

### 3.1 왜 필요한가 (배경)

- 11개 비교 대상(`docs/model-orchestration.md` §15, `HANDOFF_GOLDEN_EXPANSION.md`)의
  상위 6개가 2~3%p 안에 몰려 McNemar 동률이다. 이 격차를 통계적으로 가르려면
  (2-proportion 근사) **n이 수천 단위**로 필요하다:
  ```
  n ≈ (z_α/2+z_β)² × [p1(1-p1)+p2(1-p2)] / (p1-p2)²
    ≈ 7.84 × 0.29 / 0.0207² ≈ 5,300 (per-arm 근사, paired McNemar면 다소 적게 필요)
  ```
- §1.2/§1.3 방식(기존 tier_b/holdout 자산 dedup-merge)은 회사 골든 원본 자산이
  유한해서 이 규모까지 못 간다. **진짜 신규 문항 생성이 필요**하다는 게 이
  세션의 결론.

### 3.2 생성 모델 편향 분석 (사용자와 합의된 결론)

- **국내 3사(Solar/EXAONE/A.X)로 생성 금지**: 이 셋이 직접 채점 대상이라
  자기 문체에 맞는 문항을 스스로 만들면 자기 문항에서 유리해진다(기존
  "한 사람이 골든을 뽑아 EXAONE에 유리했을 수 있다"는 지적과 동일 구조의
  문제가 모델 단위로 재발).
- **"국내 3개를 똑같이 1/3씩 쓰면 공정하지 않나?"는 기각** — 국내 3사 상호
  비교에서는 어느 정도 상쇄되지만, **이 벤치마크의 핵심 질문인 "국내 조립
  vs 해외 모델" 축에서는 국내 진영 전체가 유리해지는 구조적 편향이 그대로
  남는다.** 해외 8개(GPT/DeepSeek/GLM/Claude 등)는 어떤 문항에서도 자기
  홈그라운드가 없기 때문.
- **Bedrock Claude(`anthropic.claude-sonnet-4-6`, `claude-haiku-4-5`)도 생성기로
  쓰면 안 됨** — 이 둘이 11개 비교 대상에 이미 포함돼 있어 같은 문제가
  해외 쪽에서 반복된다.
- **결론**: 11개 비교군에 전혀 없고, "국내 vs 해외" 서사에도 이해관계가
  없는 제3벤더 모델(Amazon/Meta)을 Bedrock 인프라로 호출.
- **보조 원칙**: 코드/DB로 결정론 생성 가능한 task(t3/t9)는 LLM을 아예 안
  쓰는 게 최우선(편향 리스크 0). 실사용 로그(`query_runs`, 실제 위임
  메시지/회의록)가 있으면 그것도 모델보다 우선(모델 개입 자체가 없어 편향
  원천 자체가 없음). LLM 생성은 이 두 소스로 못 채우는 나머지에만 사용.
- **단일 생성기 획일화 방지**: 패러프레이즈/합성이 필요한 문항은 한 모델에
  전량을 맡기지 않고 최소 2개 계열을 교차 사용(문체 아티팩트 방지 —
  `docs/model-orchestration.md`에 decompose n=16→n=160 재측정에서 유의성이
  사라진 선례가 있어, 양보다 생성 다양성이 우선).

### 3.3 Bedrock 모델 접근성 프로브 결과 (이 세션에서 실측, 재확인 불필요)

리전 `us-east-1`, 기존 `ORTHUS_LLM_BEDROCK_API_KEY`(Bedrock API key 방식,
`orthus/models/adapters/bedrock.py::BedrockConverseChat`, boto3/AWS 자격증명
아님) 그대로 사용. `model_id`는 어댑터의 `_normalize_model_id`가 자동으로
`us.` 접두어를 붙이므로(`us./eu./apac.` 이미 있으면 그대로 통과) 슬러그에
접두어 없이 넣으면 됨.

| model_id | 상태 | 비고 |
|---|---|---|
| `amazon.nova-pro-v1:0` | ✅ 접근 가능, 한국어 자연스러움 확인 | **주력 생성기 추천** |
| `amazon.nova-lite-v1:0` | ✅ 접근 가능 | 경량/저비용 대안 |
| `meta.llama3-3-70b-instruct-v1:0` | ✅ 접근 가능 | 교차검증/문체 다양화용 |
| `meta.llama3-1-70b-instruct-v1:0` | ✅ 접근 가능 | 교차검증 대안 |
| `amazon.nova-premier-v1:0` | ❌ 404 access denied (legacy 취급) | 계정에서 비활성 |
| `meta.llama3-1-405b-instruct-v1:0` | ❌ 400 invalid identifier | 이 계정/리전에 없음 |
| `mistral.mistral-large-2402-v1:0` / `2407-v1:0` | ❌ 400 invalid identifier | |
| `mistral.mixtral-8x7b-instruct-v0:1` | ❌ 400 invalid identifier | |
| `cohere.command-r-plus-v1:0` | ❌ 400 invalid identifier | |

**결정**: **`amazon.nova-pro-v1:0` 주력(전체의 ~70~80%) + `meta.llama3-3-70b-instruct-v1:0`
교차용(~20~30%)**. 둘 다 11개 비교군 밖이고 "국내 vs 해외" 서사에 무관한
제3벤더. 재현용 프로브 스니펫(그대로 실행하면 재확인 가능):
```python
from orthus.models.adapters.bedrock import BedrockConverseChat
chat = BedrockConverseChat(
    api_key=os.environ["ORTHUS_LLM_BEDROCK_API_KEY"],
    region=os.environ.get("ORTHUS_LLM_BEDROCK_REGION") or "us-east-1",
    model_id="amazon.nova-pro-v1:0", inference_prefix="us",
    max_tokens=50, temperature=0.0,
)
chat.complete("You are terse.", "한국어로 딱 한 문장으로 자기소개해줘.")
```
(`PYTHONPATH="$PWD" uv run python3 ...`로 실행 — repo 의존성 필요, 순수
`python3`로는 `httpx` 등이 없어 실패함.)

### 3.4 task별 생성 전략 + 목표 물량 (1차 합의안, §1.2/§1.3의 dedup-merge n과 별도 가산)

| Task | 신규 생성 목표 | 방식 | LLM 필요 여부 |
|---|---|---|---|
| **t3** 구조화질의 | +250~350 | notion_rows 스키마 × 인텐트(groupby-count/filter/topN/sort/join) 조합을 코드로 열거 → `orthus_company` DB에서 SQL 실행해 정답 산출 → 질문 표면만 다양화 | 질문 패러프레이즈만 (Nova Pro) |
| **t9** 그래프바인딩 | +150~200 | `kg_entities`/`kg_entity_mentions`에서 실존 인물·프로젝트 쌍 코드로 추출 → 인텐트별(relation/neighbors/path_between/conflicts) 템플릿 | 템플릿 슬롯 채우기만, 필요 시 Nova Pro |
| **t7** 분해판정 | +200~250 | 템플릿 계열 최소 5~6종(접속사/열거/비교/생략복합/함정단일문) 분산 생성 — 계열 다양성이 양보다 우선 | Nova Pro + Llama 3.3 교차 |
| **t5/t6** 라우팅/인텐트 | +150~200 | 우선 `query_runs` 실사용 로그 마이닝, 부족분만 클래스 균형·경계케이스(집계처럼 보이는 지식질문 등) 합성 | 실로그 우선, 부족분만 Nova Pro |
| **t10** 위임추출 | +100~150 | 실제 회의록/위임 메시지 우선, 부족분은 함정 네거티브(회의록 액션아이템/자기배정 등 실제 오탐 유형) 30% 포함해 합성 | 실로그 우선, 부족분 Nova Pro/Llama 교차 |

**주의**: 위 표의 신규 생성분은 §1.2/§1.3의 dedup-merge로 이미 늘어난
n(t5=76 또는 469)에 **더해지는** 것 — 최종 보고 시 "기존 자산 재분류분"과
"모델/실로그로 만든 순수 신규분"을 provenance로 구분해서 합산 근거를 밝힐 것
(§4.2 참조).

---

## 4. 실행 시 지켜야 할 규약 (기존 문서와 동일 패턴 재확인)

### 4.1 스키마/파이프라인 (`HANDOFF_GOLDEN_EXPANSION.md` §3 그대로 적용)
- `expected.kind`는 반드시 `exact` 또는 `structural`(judge/metric이면
  채점에서 deferred로 빠짐).
- t7 신규 문항에 `missed_probe`/`control_probe`/`aggregate_scored_set_wide`
  태그를 넣지 말 것(넣으면 집계 전용으로 빠짐).
- `frozen.input_sha256` 세팅 필수, `build_manifest.py`/`build_tier_b.py`
  결정론 빌더 경유 권장.
- 문항 추가 후 `freeze.lock` 재생성 필수(CI drift-gate).
- 신규 task 코드 추가는 금지(하네스 dispatch/채점 로직까지 손대야 해서 범위
  커짐) — 기존 t3/t5/t6/t7/t9/t10 안에서만 늘릴 것.

### 4.2 provenance/생성기 메타데이터 (이번 작업에서 새로 지켜야 할 것)
- 기존 골든은 전부 `provenance:"golden"`으로만 태깅돼 구분이 안 된다(이
  세션에서 확인 — `tier_a.jsonl` 전체가 `golden` 851개, 세부 출처 구분 없음).
  **이번 신규 생성분부터는 반드시 구분 가능하게 남길 것**:
  - `tags`에 생성기 식별자 포함 (예: `"gen_bedrock_nova_pro"`,
    `"gen_bedrock_llama3_3_70b"`, `"gen_db_deterministic"`, `"gen_real_log"`)
  - 어느 모델이 몇 문항을 생성했는지 별도 매니페스트(예:
    `experiments/fugu-ko/e2e/augment_provenance.json`)에 기록 — 나중에
    "이 생성기가 특정 계열에 유리하게 작용했는지" 사후 감사 가능해야 함.
- 신규 생성 문항과 기존 tier_a/tier_b 전체 간 **중복 제거**(질문 텍스트
  정규화 dedup 최소, 여유 있으면 임베딩 유사도 dedup) 필수.

### 4.3 재실행 순서 (`HANDOFF_GOLDEN_EXPANSION.md` §4와 동일 골격)
1. §1.3 dirty 변경 처리(§2-1 판단) 먼저.
2. 신규 생성 스크립트(§3.4) 작성 → golden/*.json 추가 또는 신규 파일 →
   `build_manifest.py`/`build_tier_b.py` 확장 → `tier_a.jsonl`/`tier_b.jsonl`
   재빌드.
3. `freeze.lock` 재생성.
4. 카나리아 (`--models solar --tier A --layer all --tasks t3,t5,t6,t7,t9,t10 --limit 3 --final-verify`) — 신규 문항이 deferred 안 되고 pass/fail로 잡히는지 확인.
5. 11모델 순차 전량 재실행(`HANDOFF_GOLDEN_EXPANSION.md` §4.2 슬러그 목록 그대로,
   콜론 슬러그는 작은따옴표 필수, `--final-verify` 전 슬러그).
6. `combine_stats.py` 재병합(n 자동 계산) → `slot_swap_exp.py`의 145/324-핀
   sanity 상수 3개(`KNOWN_DIVERSIFIED_COMPOSITE_PASS`,
   `KNOWN_DIVERSIFIED_VS_BASELINE_MCNEMAR_P`, CI) 새 n 기준으로 갱신 후 재계산.
7. `e2e_report.md` 갱신 — "기존 자산 재분류 n" vs "신규 생성 n"을 구분해서
   최종 n과 판정(동률 무리 유지/해소 여부) 명시.

### 4.4 DB/환경 (기존 함정, 반드시 재확인)
- `ORTHUS_PG_DSN`과 `ORTHUS_PG_DSN_READONLY` **둘 다** `orthus_company`로 override
  (하나만 바꾸면 RO DSN이 빈 `orthus`를 읽어 t3 전 모델 가짜 실패 — 지난 세션에서
  실제로 발생한 사고).
- `test`/`staging` 이름 든 DB는 L2 진입 시 TRUNCATE 가드 걸리니 절대 금지.
- `ORTHUS_LLM_BEDROCK_API_KEY`/`ORTHUS_LLM_BEDROCK_REGION`은 이미 `.env`에
  있고(§3.3에서 실사용 확인), harness가 `.env`를 module top에서 로드하므로
  수동 export 안 해도 되지만(§Phase4 STATE.md 기록), 생성 스크립트를 별도로
  짤 경우 harness 바깥이라 직접 export 필요.

---

## 5. 함정 체크리스트 (누적)

- **§1.3 dirty 변경 미완성** — 다음 세션이 그대로 전량 실행하면 freeze.lock
  drift로 CI가 막히거나, sanity 상수 불일치로 slot_swap 게이트가 (정당하게)
  실패한다. 반드시 §4.3 순서대로.
- **Bedrock Claude(`bedrock:anthropic.claude-*`)를 생성기로 쓰지 말 것** —
  평가 대상과 겹침(§3.2).
- **국내 3사(Solar/EXAONE/A.X)를 생성기로 쓰지 말 것** — 직접 채점 대상(§3.2).
- **provenance 미표기 위험** — 기존 골든처럼 전부 `"golden"`으로만 태깅하면
  나중에 생성기 편향 감사가 불가능해진다(§4.2 신설 규칙 준수).
- **WSL2 background 폴링 오탐** — 장시간 실행은 foreground until-loop +
  timeout으로 이중 확인 ([[wsl2-background-monitor-unreliable]] 메모리 참조).
- **서브에이전트 보고 수치 불신** — raw jsonl 직접 재집계 필수
  ([[verify-subagent-metrics-directly]] 메모리 참조).
- **커밋 스테이징 파일 명시** — `git add -A` 금지, main worktree의 무관
  dirty 파일(`pyproject.toml`/`uv.lock`/`api_calling_test.py`/
  `experiments/fugu-ko/prompt-tuning/` 등)과 섞이지 않게 이 worktree 안에서만
  작업.

---

## 6. 참고 문서 포인터

- `experiments/fugu-ko/e2e/HANDOFF_GOLDEN_EXPANSION.md` — n=145→324 확장 절차/함정
  (§1.2 커밋의 원 출처, 스키마/freeze/재실행 규약 상세).
- `experiments/fugu-ko/e2e/STATE.md` — 전체 벤치마크 프로젝트 phase 이력(Phase 0-6),
  게이트 판정, 위임/오케스트레이션 방침.
- `experiments/fugu-ko/e2e/manifest_schema.md` — 골든 문항 스키마 정식 명세.
- `orthus/models/adapters/bedrock.py` — Bedrock 어댑터(model_id 정규화, Converse API).
- `docs/model-orchestration.md` §15 — 조립 배정표 SoR, decompose n=16→160 재측정
  유의성 소실 선례(§3.4 "양보다 다양성" 근거).
- `experiments/fugu-ko/analysis/slot_swap_exp.py` — 조립 V1/V2 재계산, 145/324-핀
  sanity 상수 위치.
- 이 대화의 스크럼 회의록 원문(문서화 안 됨, 대화 로그 참조) — "5~6개
  벤치마크로 확대", "국내 3사 동률이 표본 부족 때문인지" 문제의식의 원출처.
