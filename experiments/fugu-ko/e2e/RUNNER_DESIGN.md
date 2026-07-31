# 잔여 5종 러너 설계 노트 (wiki_qa / synthesize / email_draft / gap_suggest / claim_headline)

선례: `arena_run.py`(실행 계층 원본) · `koparity_run.py`(데이터 계층만 새로 쓰고 실행
계층은 import한 형제 러너) · `t12_generation.py`(생성 3종 결정론 지표) · `t8_synth.py`
(synthesize를 프로덕션 함수로 직접 구동) · `judge/panel.py`(이종 judge 패널 재판정).

## 1. arena_run.py에서 **그대로 import**할 것 (재정의 금지)

`koparity_run.py`가 세운 규약을 그대로 따른다 — `from arena_run import PRICING, SYSTEMS,
DryRunClient, build_client, estimate_cost_usd`.

- **벤더 클라이언트/키 해석**: `build_client(slug)` 하나로 OpenAI 호환(`VendorClient`)과
  Bedrock Converse(`BedrockVendorClient`)가 다 나온다. 국내 3종(solar/ax/exaone)은
  `orthus.models.registry.vendor_specs()`(프로덕션과 동일 env)에서 base_url/model/
  extra_body/min_interval을 가져오고, 해외는 하드코딩 env 이름을 쓴다.
- **스레드 안전 동시성**: `ThreadPoolExecutor(max_workers=--concurrency)` + per-call
  usage(공유 mutable state 없음) + A.X류 RPS 하드캡용 인스턴스 전역 `_throttle` lock.
- **비용 상한**: `estimate_cost_usd` + `PRICING` + `--cost-cap-usd` 도달 시 신규 제출 중단.
- **체크포인트 규약**: 문항 단위 append + `write_lock`, 출력은 `<out-dir>/{system}.jsonl`,
  재개는 **실패 행을 done으로 세지 않고**(`error` non-null 제외) 같은 키의 마지막 행이 이긴다.
- **부대 유틸**: `fugu_env.load_env(verbose=True)`(env 지문 로깅), `LaneHealth`/`LaneDeadError`
  (조용한 전량 실패를 에러 행으로 흡수하지 않고 소리 내어 죽임), `--dry-run`/`--canary`/
  `--limit`/`--env-file`/`--out-dir`.

## 2. 새로 짜야 할 것 — 데이터 계층

arena는 `golden/arena_gold/gold_labels.jsonl` + `golden/arena_parts/*.jsonl`을 id로 join하고
`split=="holdout" and retired!=true`만 남기며, 조건 확장(집중 6=bare+glue, 회귀 7=bare)이
`task` 축에 매여 있다. 잔여 5종은 그 축이 그대로 맞지 않는다:

- `wiki_qa`/`gap_suggest`/`claim_headline`은 `arena_prompts.REGRESSION_TASKS`에 이미 있지만
  **프롬프트가 arena용으로 재작성된 것**이다. 잔여 슬롯 측정의 목적은 프로덕션 슬롯 교체
  판단이므로 t9/t10/t12처럼 **프로덕션 프롬프트/파서를 import**하는 쪽이 맞다.
- `synthesize`/`email_draft`는 arena에 대응 태스크가 아예 없다(`arena_assemble.py` §21 주석).
  `synthesize`는 `orthus.router.decompose.synthesize(q, sub_answers, subqs, chat_model=…)`를
  직접 부르는 t8_synth 방식이어야 하고, 이때 입력(sub_answers)은 **freeze**해서 synthesize
  모델만 변수로 남겨야 한다.
- 따라서 골든 스키마는 arena join이 아니라 슬롯별 단일 파일(`{id, slot, input…, gold?}`)로
  두고, `load_dataset()`은 koparity처럼 `.json`(`{"items": […]}`) + `.jsonl` 양쪽을 읽고
  id 중복은 즉시 에러로 막는다.

**어댑터 갭**: 프로덕션 함수는 `ChatModel` 프로토콜(`.complete(system, user, json_only)`
호출부가 아니라 모델 객체)을 받는다. `build_client()` 산출물을 그 프로토콜로 감싸는 얇은
shim이 필요하다 — 프로덕션 프롬프트를 쓰되 벤더 계층은 arena 그대로 쓰기 위한 접착제.

## 3. 새로 짜야 할 것 — 채점 (arena와 가장 크게 갈리는 지점)

arena는 `arena_scorers_focused/_regression.py`의 **결정론 채점**(gold와 대조 → `score.strict`
boolean)이라 러너가 곧 채점 준비다. 잔여 5종은 전부 자유생성이라 gold 문자열 일치가 없다.

- **2단 분리 필수**: `생성 러너`(raw jsonl) → `judge 러너`(판정 jsonl). 한 프로세스에 넣지
  않는다 — judge는 콜 수·단가·재개 키가 다르고, 워커 raw를 고정한 채 judge만 바꿔 재판정하는
  것이 `judge/panel.py`가 세운 방법론이다(워커 재실행 0회).
- **judge 체크포인트 키**는 arena의 `(id, condition)`이 아니라 `(judge, worker, id, direction)`
  이다. 양방향 2회 + 방향 불일치 → tie가 프로토콜이고, **judge ∉ 판정 쌍**(자기 출력 판정
  금지)을 로스터 구성에서 강제해야 한다.
- **judge 전에 결정론 지표를 먼저 뽑는다**(t12 선례). 형식 실패율(`json_only`인
  email_draft/gap_suggest는 JSON이 깨지면 프로덕션이 결정론 폴백으로 조용히 떨어져 모델 교체
  의미가 0), 환각(지시/참고자료에 없는 고유명사 주입), 명시 제약 준수(제목 `Re:` 금지,
  headline 길이 상한), 지연. 주관 판정은 이걸로 안 걸러지는 잔여분에만 쓴다.
- 판정 무효 조건(judge JSON 실패율 >10%)과 tie율 정상범위 확인을 스모크 게이트로 둔다.

## 4. 열어둔 결정

(a) 5종을 한 러너의 `--slot` 분기로 둘지, 생성 3종(t12 계열)과 QA/synthesize(judge 무게가 다름)
를 나눌지. (b) 골든 규모와 출처(신규 홀드아웃 vs 기존 t8/t12 문항 재사용 — 재사용 시 낙관
편향 경고가 `docs/model-orchestration.md` §11에 이미 있다). (c) judge 로스터.
