# REMOTE_GPU_RUNBOOK — Phase 5 라이브 벤치를 A100 80G 박스에서 실행

이 문서는 사용자의 tailscale-reachable A100 80G 리눅스 박스에서 Phase 5(국내 풀 라이브
벤치)를 돌리기 위한 셋업 전용 런북이다. **모델 호출/채점/게이트 명령 자체는 여기서
재작성하지 않는다** — 전부 원격 API 호출이라 GPU는 관련이 없고, `LIVE_BENCH_RUNBOOK.md`가
이미 실제 CLI를 검증해 둔 SoR이다. 이 문서는 그 커맨드를 "어디서" 돌리기 위한 박스 셋업
(clone/의존성/DB/터널/키)만 다룬다. 사용자 결정(2026-07-20, `STATE.md` "Remote execution
decision"): GPU 미사용(전 모델 원격 API), 회사 DB는 로컬 유지(박스로 복사 금지), 스크래치
`orthus_test`/`staging` Postgres만 박스에 둔다.

## 0. 언제 이 문서를 쓰나

로컬 머신 디스크 여유가 부족해 Phase 5 전량(2,069 items, 8 known fail 제외 — 아래 6번)을
돌릴 공간이 없을 때만 이 경로를 쓴다. 로컬에서 그대로 돌릴 수 있으면
`LIVE_BENCH_RUNBOOK.md`만으로 충분하고 이 문서는 필요 없다.

## 1. 박스에 코드 옮기기

`experiments/fugu-ko/harness_e2e.py`와 `experiments/fugu-ko/e2e/`는 지금 main에
**untracked**다(`LIVE_BENCH_RUNBOOK.md` §6). 두 경로 중 하나를 쓴다.

### 1a. rsync (커밋 전, 지금 바로 돌리고 싶을 때 — 권장)

untracked 파일은 `git clone`으로 안 옮겨지므로, repo 전체를 먼저 clone한 뒤 e2e 자산만
rsync로 덮어쓴다.

```bash
# 박스에서: repo를 main 기준으로 clone
ssh <user>@<tailnet-host>
git clone <remote-url> orthus-ai-competition
cd orthus-ai-competition
git checkout main   # 또는 현재 작업 중인 브랜치/커밋 SHA

# 로컬에서: untracked e2e 자산 + harness를 박스로 rsync
rsync -avz --exclude 'analysis/raw/' --exclude 'keys.json' --exclude '.venv' \
  <repo>/experiments/fugu-ko/ \
  <user>@<tailnet-host>:~/orthus-ai-competition/experiments/fugu-ko/
```

`--exclude`는 `experiments/fugu-ko/.gitignore`가 이미 막는 대상(`analysis/raw/`,
`keys.json`)과 로컬 가상환경을 박스로 새지 않게 하는 이중 방어다.

### 1b. commit 후 clone (harness가 최종화돼 커밋된 뒤)

`LIVE_BENCH_RUNBOOK.md` §6 계획대로 `.worktrees/<topic>` feature 브랜치에 harness/e2e
자산을 커밋했다면, 박스에서는 그 브랜치를 그대로 clone/checkout하면 된다 — rsync 불필요.

```bash
ssh <user>@<tailnet-host>
git clone <remote-url> orthus-ai-competition
cd orthus-ai-competition
git checkout feat/<topic>   # harness_e2e.py + e2e/*가 커밋된 브랜치
```

어느 경로든, 실행 전 박스에서 `git log -1 --oneline`(또는 rsync 출처 커밋)을 기록해 두면
결과 산출물의 코드 버전 추적이 쉽다.

## 2. 박스 의존성 설치

```bash
cd ~/orthus-ai-competition
uv sync --extra dev
```

## 3. 박스 로컬 Postgres (스크래치 orthus_test / staging 전용)

박스에서 Docker Postgres를 띄우고, L2 fixture용 스크래치 DB만 여기에 둔다 — **회사
`orthus_company` 데이터는 이 컨테이너에 절대 복사하지 않는다**(§4 참조, 회사 DB는 로컬
전용 유지가 사용자 결정).

```bash
# 박스에서
make up        # docker: postgres(5433) + neo4j — root .env 기준, 로컬 dev 컨테이너
make migrate   # orthus_test에 alembic upgrade head
```

`make up`/`make migrate`는 root `.env`(K1 KG 컨테이너 포함, `ORTHUS_KG_PASSWORD` 필요 —
KG는 이 벤치와 무관하면 값만 채워 두면 됨)를 그대로 쓴다. 이 컨테이너의 `orthus_test`가
`e2e/runner_lib.py::is_safe_truncate_dsn()`이 허용하는 이름("test"/"staging" 포함)이므로
L2 TRUNCATE/fixture-seed가 여기서 정상 동작한다.

## 4. 회사 DB 터널 (read-only, 회사 데이터는 박스에 복사하지 않음)

t3 등 일부 L1 항목은 `orthus_company`를 실제로 조회한다(read-only). 회사 Postgres는 회사
머신(로컬)의 5433 loopback에만 떠 있으므로, tailscale SSH 포트포워드로 박스에서
`localhost:15433`으로 접근한다.

```bash
# 박스에서 (회사 머신 방향으로 역방향이 아니라, 박스 -> 회사 머신 순방향 SSH 터널)
ssh -N -L 15433:localhost:5433 <company-user>@<company-tailnet-host> &
```

이제 박스의 `orthus_company` DSN은:

```bash
export ORTHUS_PG_DSN_COMPANY_RO="postgresql+psycopg://orthus_ro:orthus_ro@localhost:15433/orthus_company"
```

회사 node.env(`~/.orthus/nodes/company/node.env`)의 실제 값은 `ORTHUS_PG_PORT=5433`,
`ORTHUS_NODE_DB=orthus_company`, 그리고 read-only 롤 DSN이 이미
`ORTHUS_PG_DSN_READONLY="postgresql+psycopg://orthus_ro:orthus_ro@localhost:5433/orthus_company"`로
정의돼 있다 — 터널 포트만 `15433`으로 바꿔 위와 같이 구성하면 된다. **가능하면 admin
`orthus`/`orthus` 계정이 아니라 이 `orthus_ro`/`orthus_ro` 롤을 쓴다**(추가 방어; 아래 안전
근거는 롤과 무관하게도 성립한다).

### 안전 근거 — 왜 회사 DB가 안전한가

`e2e/runner_lib.py::is_safe_truncate_dsn()`은 DSN의 데이터베이스 이름에 `test` 또는
`staging`이 포함될 때만 True를 반환하는 화이트리스트다. `orthus_company`는 이 조건에
해당하지 않으므로 `truncate_guard_ok()`가 False가 되고, `harness_e2e.py::main()`은
`truncate_all_tables()`를 건너뛰며 L2 fixture 항목 전체를 `dispatch_l2()` 진입 전에
`skipped`(fail 아님) 처리한다(`LIVE_BENCH_RUNBOOK.md` 상단 경고 박스 참조). 즉 회사 DB를
가리키는 DSN을 실행 중 원격 실행 스텝(§7)에서 잠깐 섞어 써도 **write/truncate가 코드
레벨에서 자동 차단**되고, t3 같은 L1 read-only 조회만 그대로 통과한다. 그럼에도 이 문서는
스크래치 DSN(§3)과 회사 read-only DSN(본 절)을 **환경변수로 분리**해 실행 명령마다 올바른
`ORTHUS_PG_DSN`을 명시적으로 source하는 것을 권장한다(가드는 안전망이지 1차 방어가 아님).

## 5. 키 주입 (셸 env 전용, 파일/커밋 금지)

`LIVE_BENCH_RUNBOOK.md` §1이 요구하는 env는 그대로 박스 셸에서 export한다 — 값은 이
문서에도, 어떤 커밋/PR/wiki에도 남기지 않는다.

- `FUGU_KEYS=/path/to/keys.json` — keys.json 자체를 박스에 둘 경우 **`chmod 600`
  필수**, `.gitignore`/`--exclude` 대상 유지, 절대 rsync source(로컬)에서 커밋하지 않는다.
- `ORTHUS_LLM=openai` / `ORTHUS_LLM_API_KEY` — baseline(gpt-4o-mini) 비교용.
- (Phase 6 게이트 전용, 아직 호출 아님) `ORTHUS_LLM_BEDROCK_API_KEY`,
  `ORTHUS_LLM_BEDROCK_REGION`, `ORTHUS_GLM_API_KEY` — `LIVE_BENCH_RUNBOOK.md` §5 참조.

이름 전체 목록과 각 값의 의미는 `LIVE_BENCH_RUNBOOK.md` §1.1/§1.2/§5를 그대로 참조한다(중복
기술 금지).

## 6. 실행 순서

박스에서 각 단계마다 `ORTHUS_PG_DSN`을 그 단계에 맞는 DSN으로 명시적으로 export한 뒤 돈다.

1. **카나리아 (`--limit 3`)** — `LIVE_BENCH_RUNBOOK.md` §2 명령 그대로. 이때
   `ORTHUS_PG_DSN`은 §3의 박스 로컬 `orthus_test`(L2 항목이 존재하면 seed 가능해야
   하므로)로 둔다. t3 같은 회사 DB 조회 항목이 카나리아 3건 안에 걸리면 §4의 read-only
   터널 DSN으로 바꿔 재확인해도 된다(가드가 write를 차단하므로 섞여도 안전 — §4 참조).
2. **Phase 5 전량** — `LIVE_BENCH_RUNBOOK.md` §3 명령 그대로(`--tier all --layer all`).
   L2 fixture 항목은 §3의 스크래치 `orthus_test`를 쓰고, t3 등 L1 read-only 항목은 실행
   중 `orthus.settings`가 resolve하는 `ORTHUS_PG_DSN` 하나로 양쪽을 다 커버해야 하므로,
   **`ORTHUS_PG_DSN`을 §4의 회사 read-only 터널로 맞춰 두고 돌린다** — L2는 write가
   가드로 스킵되니 안전하고(§4), L1 read-only는 정상 조회된다. orthus_test로 실제
   TRUNCATE+seed가 필요한 L2 fixture 실행이 목적이면 그 구간만 `ORTHUS_PG_DSN`을 §3
   스크래치로 바꿔 별도 실행한다(두 DSN을 한 프로세스가 동시에 쓸 수는 없다 — 어떤
   조합을 택하든 가드가 잘못된 대상에 write되는 것은 막는다).
3. **통계/증거** — `LIVE_BENCH_RUNBOOK.md` §4.
4. **산출물 회수** — `analysis/raw/`(모델별 `e2e_{slug}.jsonl` + `e2e_summary.json`)를
   박스에서 로컬로 rsync 회수한다:

   ```bash
   rsync -avz <user>@<tailnet-host>:~/orthus-ai-competition/experiments/fugu-ko/analysis/raw/ \
     <repo>/experiments/fugu-ko/analysis/raw/
   ```

   회수 후 `analysis/e2e_report.md` 작성(§4 지시대로 사용자/에이전트가 직접 옮겨 적음)은
   로컬에서 진행해도 된다 — 통계 산출물만 있으면 되고 박스 세션을 유지할 필요는 없다.

5. **Phase 6 게이트** — `LIVE_BENCH_RUNBOOK.md` §5. `--final-verify` 없이는 대형모델
   어댑터 생성 자체가 일어나지 않는다(REFUSED, `SystemExit(2)`) — 이 게이트는 Tier A/B
   확정 전까지 박스에서도 호출 금지.

## 7. 주의사항

- **8건의 known smoke fail**(`e2e/SMOKE_FAIL_TRIAGE.md`에 트리아지 완료 — 2건은 실제 prod
  회귀, 6건은 stale golden)로 harness가 **exit 1을 반환하는 것이 정상**이다. 박스 실행
  스크립트/CI가 이 exit code로 자동 재시도·알람을 걸지 않도록 한다.
- **62건의 pending stub**(`l2/g1..g3.jsonl`, `pending_user_fill` 태그)은
  `load_manifest_files`가 자동 skip+count한다 — 별도 조치 불필요.
- **대형모델은 `--final-verify` 게이트 통과(사용자의 Tier A/B 확정 승인) 전까지 호출
  금지** — 박스에서도 예외 없음(`STATE.md` "Hard gates").
- 회사 DB 터널(§4)은 세션이 끊기면 죽는다 — 장시간 Phase 5 실행 중 SSH 세션이 끊기지
  않도록 `tmux`/`screen` 안에서 터널과 harness 프로세스를 함께 관리한다.
- keys.json/`.venv`/`analysis/raw/`는 박스에도 로컬에도 커밋 대상이 아니다(§1a exclude,
  `experiments/fugu-ko/.gitignore`).
