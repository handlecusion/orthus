# LIVE_BENCH_RUNBOOK — Phase 5 국내 풀 라이브 벤치 + Phase 6 게이트

이 문서는 `harness_e2e.py`(argparse/CLI)와 `pool.py`(keys.json 로더)를 직접 읽고 검증한
**실제 CLI 그대로**의 절차다. harness/tier/golden/prod 코드는 이 작업에서 건드리지 않았다.

> ✅ **DB 안전 가드 fail-closed로 수정됨(코드 실측):** `e2e/runner_lib.py::truncate_guard_ok()`가
> 해석된 `ORTHUS_PG_DSN`의 데이터베이스 이름에 `test` 또는 `staging`이 포함될 때만 True를 반환하는
> 화이트리스트 가드다(`is_safe_truncate_dsn`, 순수 함수 + import-time self-test). `truncate_all_tables()`와
> `apply_fixture()`는 이 가드를 통과하지 못하면 TRUNCATE/seed를 전혀 실행하지 않고 경고
> 한 줄(`live DB detected — L2 fixture items skipped, L1 read-only items only`)만 남긴다.
> `harness_e2e.py::dispatch_l2()`도 같은 가드를 항목 진입 전에 먼저 확인해 L2 항목 전체를
> `skipped`(fail 아님)로 표시하고 DB를 건드리지 않는다 — L1 read-only 항목(예: `t3`의
> `orthus_company` 조회)은 영향 없이 그대로 dispatch된다. 즉 지금은 `~/.orthus/nodes/company/node.env`를
> 그대로 source해도 로컬 `orthus_company` DB가 비워지지 않는다(가드가 막는다). 다만 안전망일 뿐
> **staging 클론 사용은 여전히 권장**이다 — 가드 없이도 안전한 격리 환경에서 도는 편이 낫고,
> 아래 "사전 준비"의 staging 클론 절차를 1차 경로로 유지한다.

## 1. 사전 준비 (사용자)

### 1.1 keys.json (`pool.py::_load_keys`) — 기존 경로

`pool.build_pool()`은 `FUGU_KEYS` 환경변수가 가리키는 JSON 파일을 리스트로 읽어
`provider` 문자열 부분매칭(소문자, `upstage`/`a.x`/`exaone`)으로 항목을 찾는다. `model`은
공백으로 split한 첫 토큰을 쓴다(없으면 `WorkerSpec.model` 고정값). 형식:

```json
[
  { "provider": "Upstage", "key": "<UPSTAGE_KEY>", "model": "solar-pro" },
  { "provider": "A.X", "key": "<ADOT_KEY>", "model": "A.X-K1" },
  { "provider": "Exaone (FriendliAI)", "key": "<FRIENDLI_KEY>", "model": "depmkuykpfon9lg (Endpoint ID)" }
]
```

`model` 필드는 실제로 쓰이는 것은 solar/ax뿐이고(exaone은 `WorkerSpec.model=None`이라 항상
keys.json의 첫 토큰을 씀 — 엔드포인트ID 형식 유지 필요), key 값만 실키로 채운다.

```bash
export FUGU_KEYS=/path/to/keys.json   # default가 Windows 경로라 반드시 override
```

keys.json이 없거나(`_load_keys`가 이제 파일 부재를 빈 리스트로 처리) 해당 slug의 entry가
없거나 플레이스홀더 값(`YOUR_KEY_HERE` 류 — `_is_real_key`)이면 아래 1.1b `.env` 폴백으로
넘어간다. keys.json에 실키가 있으면 항상 그게 우선이다(기존 keys.json-only 사용자는 무변경).

### 1.1b `.env`로 직접 키 넣기 (신규, `pool.py::_vendor_env_fallback`)

keys.json 파일을 따로 관리하고 싶지 않으면, repo-root `.env`에 **production이 이미 쓰는 것과
동일한 env var**를 넣기만 해도 harness가 그대로 읽는다(`orthus.settings.get_settings()` →
`orthus.models.registry.vendor_specs()`, `orthus/settings.py`가 `env_file=".env"`로 자동
로드한다 — 별도 `python-dotenv`/`source` 불필요). 정확한 변수명:

```bash
# .env (repo root)
ORTHUS_LLM_SOLAR_API_KEY=<실키>
ORTHUS_LLM_SOLAR_BASE_URL=https://api.upstage.ai/v1      # 생략 가능, settings 기본값과 동일
ORTHUS_LLM_SOLAR_MODEL=solar-pro                          # 생략 가능, settings 기본값과 동일

ORTHUS_LLM_AX_API_KEY=<실키>
ORTHUS_LLM_AX_BASE_URL=https://awf-gw.adot.ai/v1           # 생략 가능
ORTHUS_LLM_AX_MODEL=A.X-K1                                  # 생략 가능

ORTHUS_LLM_EXAONE_API_KEY=<실키>
ORTHUS_LLM_EXAONE_BASE_URL=https://api.friendli.ai/dedicated/v1  # 생략 가능
ORTHUS_LLM_EXAONE_MODEL=<Friendli dedicated endpoint id>          # 필수 — settings 기본값 빈 문자열
```

주의:

- ✅ **CWD-relative-loading 갭 수정됨(2026-07-20, `harness_e2e.py` 모듈 최상단):**
  `orthus.settings`의 `env_file=".env"`는 pydantic-settings 관례대로 "프로세스 CWD 기준
  상대경로"로 해석되므로(`orthus/settings.py` 파일 위치 기준이 아님) 원래는 harness를 repo
  root에서 실행할 때만 repo-root `.env`가 자동 로드됐다. 지금은 `harness_e2e.py`가 import
  시점에 `load_dotenv(_REPO_ROOT / ".env", override=False)`를 **파일 경로 기준**(`Path(__file__)`
  로 resolve, CWD 무관)으로 먼저 호출해 `.env`의 값을 실제 `os.environ`에 채워 넣는다. 그
  뒤에 `orthus.settings.get_settings()`가 자기 CWD-relative `env_file` lookup을 시도해도 이미
  진짜 env var가 있으므로 그 값을 그대로 쓴다 — 즉 harness를 **어느 cwd에서 실행해도** 도메스틱
  경로가 repo-root `.env`를 읽는다(실측: `/tmp` 등 임의 cwd에서 `PYTHONPATH`만 잡고 실행해도
  `get_settings().llm_api_key`가 `.env` 값을 그대로 반영, 2026-07-20 확인). `override=False`
  (python-dotenv 기본값)라 이미 shell에 export된 실제 값이 있으면 그게 항상 우선이다 — 기존
  export 기반 사용자는 무변경.
  `python-dotenv`는 신규 의존성이 아니다 — `pydantic-settings`(top-level orthus 의존성)의 hard
  transitive dependency라 이미 이 venv에 설치돼 있다(`pyproject.toml` 미변경).
- keys.json 폴백과 마찬가지로 이 경로도 네트워크 호출 없이 object만 구성한다 — 키가 없으면
  `pool build_pool`이 `KeyError`로 어느 slug의 어느 env var가 없는지 명시해 죽는다(값은 절대
  로깅하지 않는다).
- `baseline`은 이 폴백 대상이 아니다 — 아래 1.2가 그대로 적용된다.

### 1.2 baseline (`pool.py::_baseline_chat`)

`--models`에 `baseline`을 포함하면 `orthus.settings.get_settings()`를 읽어 **`s.llm != "openai"`
면 즉시 RuntimeError**로 죽는다(다른 모델을 조용히 기준선으로 재는 것 방지). 즉:

```bash
export ORTHUS_LLM=openai
export ORTHUS_LLM_API_KEY=<실키>
# ORTHUS_LLM_MODEL 미설정이면 settings 기본값 gpt-4o-mini 사용
```

`ORTHUS_LLM`/`ORTHUS_LLM_API_KEY`도 위 1.1b와 같은 이유로 shell export 대신 repo-root `.env`에
그냥 적어 둬도 harness가 CWD 무관하게 그대로 동작한다(1.1b와 동일하게 `harness_e2e.py` 모듈
최상단의 `load_dotenv(_REPO_ROOT / ".env", override=False)`가 `.env`의 `ORTHUS_LLM=openai`를
`os.environ`에 먼저 채워 넣으므로, 이후 `get_settings()`가 어느 cwd에서 호출되든 `s.llm`에
그대로 반영된다). 별도 런타임 export가 "항상 필요한 것"은 아니고, `.env`에 값이 있으면 harness를
어느 cwd에서 실행하든 그 값으로 충분하다.

### 1.3 DB — company-staging 클론 (company node.env 직접 source 금지)

로컬에는 `company`/`personal-a`만 부트스트랩돼 있고 `company-staging`은 없다(`~/.orthus/nodes/`
확인 완료). 1회 생성 후 재사용:

```bash
make staging-snapshot   # company -> company-staging 클론 (orthus/orthus_test 타깃은 script가 거부)
set -a; source ~/.orthus/nodes/company-staging/node.env; set +a
```

`scripts/node/snapshot_to_staging.sh`는 타깃 노드명에 `staging`이 없으면 거부하고
`orthus`/`orthus_test` DB 이름도 보호 대상으로 거부한다 — 이 스냅샷은 truncate돼도
`make staging-snapshot`으로 재생성 가능한 **disposable clone**이다. `company-staging`
node.env는 자체 `ORTHUS_LLM_API_KEY`가 비어 있을 수 있으니 1.2의 `ORTHUS_LLM_API_KEY`로
덮어써야 한다(source 순서: node.env 먼저, `export ORTHUS_LLM_API_KEY=...` 나중).

### 1.4 시맨틱 캐시

`harness_e2e.py` 모듈 최상단(20-41행)이 `orthus.settings` 최초 로드 전에
`os.environ["ORTHUS_ASK_SEMANTIC_CACHE_ENABLED"] = "false"`를 **강제**한다 — 사용자가 별도로
설정할 필요 없음(이미 false가 default이므로 이중 방어).

## 2. 1단계 — 라이브 카나리아

```bash
cd <repo>
PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" \
uv run python experiments/fugu-ko/harness_e2e.py \
  --models solar,exaone,ax,baseline \
  --tier all --layer all --limit 3
```

확인 항목:

- 키가 유효함: `pool build failed` 없이 4개 모델 모두 row 생성(`[slug] {...} scored ... p50 ... p95 ...`
  줄이 4개).
- `== invariants ==` 블록의 `model.fallback spans (delta over run): 0 (CLEAN)`.
- 캐시 우회: `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED=false`가 1.4에서 이미 강제되므로 별도 확인 불필요
  (라인 수 재출력 정도로 스팟체크 가능).
- `analysis/raw/e2e_{slug}.jsonl`에 `latency_ms` 필드가 채워짐.
- 대형모델 미승인 거부(negative test) — `--final-verify` 없이 거부되는지 확인:

```bash
PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" \
uv run python experiments/fugu-ko/harness_e2e.py \
  --models bedrock:anthropic.claude-sonnet-4-6,openai:gpt-4o \
  --limit 1
```

기대 출력: `REFUSED: large/frontier models require --final-verify ...` + `SystemExit(2)`
(`enforce_final_verify_gate`, harness_e2e.py 67-77행).

## 3. 2단계 — 국내 풀 벤치 (Phase 5)

```bash
cd <repo>
PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" \
uv run python experiments/fugu-ko/harness_e2e.py \
  --models solar,exaone,ax,baseline \
  --tier all --layer all
```

`--tier all`이 필수다 — default는 `tier=A`뿐이라 tier_b.jsonl(t3 tier_b d8, 이미 n=1438)이
빠진다. `--layer`도 default가 이미 `all`이라 L1(tier_a/tier_b) + L2(`l2/g1..g4`)가 함께 실행된다.

리마인더:

- **distill(t11) 제외는 자동이다** — `t11`은 tier_a.jsonl/tier_b.jsonl/l2/g*.jsonl 어디에도
  `"task":"t11"` 항목이 없다(grep 확인). `docs/model-orchestration.md` §14/T14의 기존 distill
  측정(Solar 배정 완료)을 인용하고 재측정하지 않는다 — `--tasks`로 별도 제외할 필요 없음.
- t3는 tier_b d8이 이미 n=1438로 POWERED(STATE.md §1b D10 LOCK, p=5.6e-187) — `--tier all`로
  자동 포함.
- pending stub 62건은 `l2/g1.jsonl`(28) + `l2/g2.jsonl`(20) + `l2/g3.jsonl`(14) = 62이며
  `load_manifest_files`가 `pending_user_fill` 등 태그로 자동 skip+count한다(출력의
  `N pending (no frozen input) skipped`).
- t8(synthesize)은 `_l1_reached_llm`/dispatch에서 `qualitative_only=True`로 항상 표시되고
  수치 채점 대상이 아니다(정성 리뷰 전용, harness_e2e.py 197-200행).
- e3 태그 aggregate 게이트: `missed_recall >= 0.80`, `mis_split_rate <= 0.05`
  (`aggregate_e3`, harness_e2e.py 641행 이하) — 콘솔에 pass/fail로 바로 출력됨.
- t10 golden-override는 **런타임 플래그가 아니라 freeze 시점에 이미 반영**돼 있다: tier_b의
  t10 항목은 `tags`에 `scorer_golden_override_required`/`golden_file_t10_holdout2`가 붙어 있고
  `expected.value`에 t10_holdout2 golden 값이 이미 박혀 있다(freeze `build:3dbfcbf`) — 하네스는
  그냥 `item.expected`로 채점하므로 별도 `--golden` 옵션은 없고 필요도 없다.
- 예상 비용 $10-20(도메스틱 3사 + baseline, 전량 실행 기준 추정 — 실측치 아님, 카나리아
  latency/토큰으로 1단계 후 재추정 권장).
- 출력 위치: 모델별 `experiments/fugu-ko/analysis/raw/e2e_{slug}.jsonl`
  (`solar`/`exaone`/`ax`/`baseline`), 통합 요약 `experiments/fugu-ko/analysis/raw/e2e_summary.json`
  — 둘 다 `experiments/fugu-ko/.gitignore`(`analysis/raw/`)로 gitignore됨.

## 4. 3단계 — 통계/증거

- `pairwise_model_stats`(McNemar + bootstrap-CI 10k)는 `analysis/raw/e2e_summary.json`의
  최상위 키 `"pairwise_model_stats"`에 그대로 실리고, 실행 중 콘솔에도
  `== pairwise model comparison ==` 블록으로 `n_paired`/`a_only`/`b_only`/`p`/`sig`/
  `bootstrap_diff_ci95`가 모델쌍마다 출력된다(`>=2 models`일 때만).
- task별 McNemar/bootstrap-CI + power 플래그를 **`analysis/e2e_report.md`에 직접 옮겨 적어야
  한다** — 이 파일은 하네스가 자동 생성하지 않으므로 사용자/에이전트가 `e2e_summary.json` +
  콘솔 로그를 근거로 작성한다.
- underpowered로 명시 표기해야 할 task(STATE.md §1c): t5 routing(n=48, MDE~14pp), t6 intent(n=20),
  t2 wiki_qa(n=30, judge), t8(n=8, 정성 전용 — 애초에 McNemar 대상 아님).

## 5. 🚦 게이트 후 — Phase 6

```bash
PYTHONPATH="$PWD:$PWD/experiments/fugu-ko" \
uv run python experiments/fugu-ko/harness_e2e.py \
  --models bedrock:<BEDROCK_MODEL_ID>,openai:gpt-4o,glm:<GLM_MODEL_ID>,deepseek \
  --tier all --layer all --final-verify
```

`<BEDROCK_MODEL_ID>`/`<GLM_MODEL_ID>`는 자리표시자다 — 정확한 modelId 문자열은
`PHASE6_MODEL_IDS.md` §1/§3에서 확정됐다(Bedrock 5종 + `glm:glm-5.2`). **DeepSeek는
팀 Bedrock 계정에 없어 Bedrock 경로를 폐기하고 `deepseek` 슬러그(공식 API 직접
호출)로 전환했다**(PHASE6_MODEL_IDS.md §2). Phase 6 예상 비용은 PHASE6_MODEL_IDS.md
§5.1(DeepSeek ≈ $0.5~0.6 / GPT-4o ≈ $6 / GLM-5.2 ≈ $3, 1회 전량 실행 grounding-realistic 기준).

✅ **코드 갭 수정됨(배선만, 실제 벤더 호출 없이 검증):** `build_e2e_pool`(harness_e2e.py)이 이제
`bedrock:<modelId>` / `openai:<model>` / `glm:<model>` / `deepseek` slug를 처리한다 — 각각 실제
프로덕션 어댑터(`orthus.models.adapters.bedrock.BedrockConverseChat` / `...openai_compat.OpenAIChat`,
재구현 아님)를 생성하고 `_ProdAdapterUsageWrapper`로 감싸 국내 풀(`pool.WorkerChat`)과 동일한
`model_id`/`reset_usage()`/`usage_totals()` 형태를 유지한다. `enforce_final_verify_gate`는
여전히 `build_e2e_pool` **이전에** 실행되므로 `--final-verify` 없이는 이 네 slug 모두 어댑터
생성 자체가 일어나지 않는다(REFUSED, `SystemExit(2)`, 실측 확인 완료 — `deepseek` 포함). 필요 env:

- `bedrock:<modelId>` → `ORTHUS_LLM_BEDROCK_API_KEY`(없으면 `ORTHUS_LLM_API_KEY` fallback),
  `ORTHUS_LLM_BEDROCK_REGION`(default `us-east-1`), `ORTHUS_LLM_BEDROCK_INFERENCE_PREFIX`(default `us`)
  — `orthus/models/registry.py`의 프로덕션 bedrock 배선과 동일한 env 이름.
- `openai:<model>` → `ORTHUS_LLM_API_KEY`, `ORTHUS_LLM_BASE_URL`(default `https://api.openai.com/v1`).
- `glm:<model>` → `ORTHUS_GLM_API_KEY`(하네스 전용 env, `orthus/settings.py` 필드 아님).
  base_url은 `https://api.z.ai/api/paas/v4/`로 하네스에 고정.
- `deepseek`(또는 `deepseek:<model>`) → `ORTHUS_LLM_DEEPSEEK_API_KEY`(필수, 하네스 전용 env),
  `ORTHUS_LLM_DEEPSEEK_MODEL`(default `deepseek-chat`), `ORTHUS_LLM_DEEPSEEK_BASE_URL`(default
  `https://api.deepseek.com`). DeepSeek 공식 API는 OpenAI-호환이라 `_build_glm_chat`과 대칭.

키가 없으면 벤더 호출 없이(네트워크 0회) `ValueError`로 env 이름을 명시해 죽는다(실측 확인 완료).
이 노트는 env **이름**만 남긴다 — 실키 값은 여기에도, 다른 어떤 문서/커밋에도 남기지 않는다.

✅ **`.env` 파일 배치로 충분(2026-07-20) — 더 이상 `set -a && source .env` 류 shell export
불필요:** 위 세 slug는 원래 `os.environ.get(...)`만 직접 읽어서(§1.1b 도메스틱 경로가 쓰는
`orthus.settings`의 pydantic-settings `env_file` 로딩과 달리) `.env`에 값을 넣어도 shell에
export하지 않으면 조용히 무시됐다. 지금은 `harness_e2e.py` 모듈 최상단이 `load_dotenv()`로
repo-root `.env`를 **먼저** `os.environ`에 채우므로(§1.1b 주의 참고, `_build_bedrock_chat`/
`_build_openai_chat`/`_build_glm_chat`이 이 시점 이후에 호출됨) 위 6개 env var 전부 repo-root
`.env`에 적어 두기만 하면 harness가 어느 cwd에서 실행되든 그대로 읽는다 — 별도 export도,
`.env` 앞에서 실행 디렉터리를 repo root로 맞출 필요도 없다. 실키가 아닌 값(예:
`ORTHUS_GLM_API_KEY=dry-test-not-real`)으로 실측: `.env`에만 넣고 shell export 없이 임의
cwd에서 harness를 import해 세 `_build_*` 함수가 모두 네트워크 호출 없이 정상 구성됨을 확인
(2026-07-20). `override=False`(python-dotenv 기본값)라 이미 shell에 export된 실제 값이 있으면
`.env`보다 항상 우선이다.

- **Bedrock 토큰 만료 2026-07-31**(STATE.md/RESUME_RUNBOOK.md 고지) — 그 전에 1회만 실행.
- **1회성 원칙**: 사용자가 Tier A/B를 직접 확인·승인한 뒤에만, 그리고 딱 한 번만 돌린다
  (RESUME_RUNBOOK.md §7 "distill은 여기서만 대형모델 실행"과 동일한 원칙 — 재실행/재측정 금지).
- 결과는 `analysis/e2e_bedrock_final.md`에 남긴다(RESUME_RUNBOOK.md §7 지정 경로).

## 6. 미커밋 안내

`experiments/fugu-ko/harness_e2e.py`와 `experiments/fugu-ko/e2e/`(본 파일 포함) 전체가 현재
main에 **untracked** 상태다(`git status --porcelain` 확인 완료). 커밋 계획: 작업이 끝나면
`.worktrees/<topic>`에 feature 브랜치를 만들고 이 파일들을 그리로 옮긴 뒤 그 worktree 안에서
커밋한다(AGENTS.md "Worktree 작업 규칙" 그대로 — main worktree에는 작업 변경을 남기지 않는다).
`analysis/raw/`, `.venv` 등은 gitignore 대상이므로 worktree 이동 후에도 커밋 대상에서 자동
제외된다. `keys.json`은 **정정**: 지금까지는 gitignore 규칙이 아니라 `FUGU_KEYS` 기본값이
off-repo(OneDrive) 경로라는 "관례"로만 보호돼 있었다 — repo 트리 안에 실수로 두면 커밋될 수
있었다. `experiments/fugu-ko/.gitignore`에 `keys.json`/`**/keys.json` 패턴을 추가해(defense in
depth) 이제는 실제 gitignore 대상이다(`git check-ignore -v` 확인 완료). repo-root `.env`는
루트 `.gitignore`(`.env`/`.env.local`)로 이전부터 이미 보호돼 있었다.
