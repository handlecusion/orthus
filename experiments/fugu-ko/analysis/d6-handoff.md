# D6 핸드오프 — 다른 세션에서 이어서 실험하기

> **이 문서 하나만 읽으면** D6(학습 선택기 재도전) 실험의 결론·자산 위치·실행법·함정·다음
> 아이디어를 전부 알 수 있다. 작성 2026-07-16. 상세 결과는 같은 폴더 `d6-results.md`.

---

## 0. 30초 요약

- **목표:** 질문마다 최적 워커를 고르는 **학습 선택기**가 "무조건 Solar" **규칙**을 이기게.
- **결론:** **배포 가능한 어떤 방법도 규칙을 못 이긴다.** 학습 선택기 13종·앙상블 전부 실패.
  이길 여지(+7.3%p)는 실재했지만 회수 불가 — 병목은 "추론 시점에 어느 워커가 틀릴지 알 수
  없음"(신호가 DB-국소적·비전이성).
- **부산물:** 판을 짜면 "이겼다"를 얼마든지 만들 수 있음을 실증(컨닝 채점 +9.2%p, p≈1e-83).
  → 발표용 "승리 vs 정직" 2부 아티팩트 제작.
- **산출물:** PR #744(검증 하네스+문서), 아티팩트 2개, `d6-results.md`.

---

## 1. 지금까지 한 것 (완료 상태)

| 단계 | 상태 | 핵심 수치 |
|---|---|---|
| G0′ headroom 게이트 | ✅ | 새 DB holdout 463: 규칙 60.5 · 오라클 67.8 · **여지 +7.3%p** |
| 학습 선택기 13종 스윕(A100) | ✅ | **13/13 McNemar p>0.05** (전부 못 이김). 피처 −1.5, 32B −0.9 |
| 민감도 분석(구성별) | ✅ | random +0.4·groupby +0.4·**ambig +3.6(p=0.031)**·by_db +0.2 |
| ambig 승리 재현 검증 | ✅ | **기각** — 원168 재실행 +0.6(부트 P=58.7%), fresh120 +0.0 |
| 워커 투표 앙상블 | ✅ | 배포가능 vote2of3 **−2.6**; union +6.9는 recall artifact(=oracle) |
| "조작된 승리" 실증(rigged) | ✅ | 컨닝 채점 +9.2%p(76.4 vs 67.2), 오라클 76.5 코앞, p≈1.6e-83 |
| 문서/PR/아티팩트 | ✅ | `d6-results.md`, PR #744, 아티팩트 2개 |

**PR:** `gh pr view 744` 로 상태 확인(open이면 self-merge 게이트 없음 — 격리 실험).
**아티팩트:** (내부 아티팩트 링크 제거)

---

## 2. 자산 위치 (제일 중요 — 데이터는 gitignore라 유실 주의)

### 작업 worktree (로컬 WSL)
- **`.worktrees/fugu-ko-selector-v2`** (브랜치 `restore/fugu-ko-selector-v2`) — **주 작업본.**
  스크립트 + 복구된 데이터 전부. 여기서 로컬 실행(DB 붙는 라벨/투표).
- `.worktrees/fugu-ko-d6` (브랜치 `exp/fugu-ko-d6-validation`) — PR #744용(4파일만). 실험엔 불필요.
- venv 공유: `<repo>/.venv/bin/python` (psycopg2-binary 설치됨).

### ⚠️ 데이터 SoR = A100 (gitignore라 git에 없음, worktree 삭제되면 로컬 유실)
`train/data/`·`analysis/raw/`는 gitignore. **유일 백업은 A100 `/data/tta/fugu-ko`.**
(2026-07-16 worktree가 예기치 않게 삭제돼 A100에서 전량 복구한 전례 있음 — 새 세션도 이 사실 유지.)

| 파일 | 로컬 경로 | A100 경로 | 내용 |
|---|---|---|---|
| labeled2.jsonl | train/data/ | /data/tta/fugu-ko/train/data/ | 3000 라벨(correct_solar/ax/exaone/baseline) |
| fresh_ambig.jsonl | train/data/ | 〃 | 재현용 신규 모호질문 120 |
| t3_unseen_holdout.json | golden/ | /data/tta/fugu-ko/golden/ | **동결** 주 채점 463문항 |
| unseen_worker_correct.json | analysis/raw/ | /data/tta/fugu-ko/analysis/raw/ | **동결** 워커 정오 GT(주판정) |
| sel_choice_holdout_*.json | train/data/ | 〃 | 13 config 선택 결과 |
| sens_results.json / rigged_results.json | train/data/ | 〃 | 민감도 / 조작 결과 |

**로컬↔A100 복원:** `scp -q a100:'/data/tta/fugu-ko/train/data/*' train/data/` (analysis/raw도 동일).

### 격리 DB (라벨링/채점용, prod 무접촉)
- `orthus_company_0706` — docker `orthus_pg` :5433, user orthus/orthus. 2026-07-06 스냅샷 복원본.
  현행 `orthus_company`(운영)와 별개. `docker ps --filter name=orthus_pg` 로 살아있는지 확인.

### 키
- 국내 3워커 API 키: `<로컬 키 저장소>/keys.json` (`FUGU_KEYS`).
  **커밋/외부공유 금지.**

### A100 (Tailscale)
- `ssh a100` → gpu-a100-jul, user tta, 4×A100-80GB 공유. 크레딧 ~7/31.
- ⚠️ 루트fs 100% 참 → 작업/캐시/tmp/pylib 전부 `/data/tta/*`.

---

## 3. 실행법 (복붙용)

### 로컬 실험 env (라벨/투표 = DB+키 필요)
```bash
cd <repo>/.worktrees/fugu-ko-selector-v2
set -a; source ~/.orthus/nodes/company/node.env 2>/dev/null; set +a
export ORTHUS_PG_DSN="postgresql://orthus:orthus@localhost:5433/orthus_company_0706"
export ORTHUS_PG_DSN_READONLY="$ORTHUS_PG_DSN" FUGU_DSN="$ORTHUS_PG_DSN"
export FUGU_KEYS="<로컬 키 저장소>/keys.json"
export PYTHONPATH="$PWD:$PWD/experiments/fugu-ko"
PY=<repo>/.venv/bin/python
```
- 장시간 작업은 `setsid bash -c '... > train/logs/xxx.log 2>&1' < /dev/null &` (세션 끊겨도 유지).

### A100 학습 env
```bash
ssh a100 'cd /data/tta/fugu-ko && export HF_HOME=/data/tta/hf-cache TMPDIR=/data/tta/tmp \
  PYTHONPATH=/data/tta/fugu-ko:/data/tta/fugu-ko/train:/data/tta/pylibs && \
  setsid bash -c "CUDA_VISIBLE_DEVICES=0 python -u train/<script>.py > train/logs/<x>.log 2>&1" < /dev/null &'
```

### 스크립트 지도 (`experiments/fugu-ko/train/`)
| 스크립트 | 어디서 | 하는 일 |
|---|---|---|
| `gen2.py` | 로컬(DB) | 스키마 자동추출 → 질문 생성(`--validate`) |
| `augment2.py` | 로컬 | 패러프레이즈 증강(중립 gpt) |
| `build_unseen_v2.py` | 로컬(DB) | held-out DB로 주 채점셋 생성 + headroom |
| `label2.py` | 로컬(DB+키) | 워커별 실행→gold 채점 라벨(`--models`, `--merge`) |
| `features.py` | 공용 | 결정론 피처 추출 |
| `tier2v.py` | A100 | 인코더 선택기(features/dep/seeds/backbone) |
| `tier3v.py` | A100 | EXAONE 선택기(full/LoRA/quant4) |
| `sens.py` | A100 | 민감도(random/by_task/by_db) `--held_tasks` |
| `repl_gen_ambig.py` | 로컬(DB+키) | fresh 모호질문 생성+라벨 |
| `repl_test_ambig.py` | A100 | 재현 판정 + 부트스트랩 |
| `vote_holdout.py` | 로컬(DB+키) | 워커 투표 앙상블(OperationalError 재시도 내장) |
| `rigged.py` | A100 | "조작된 승리" 실증(컨닝 채점 vs 정직) |
| `eval_v2.py` | 공용 | 동결 GT로 McNemar 주판정 `--tags` |
| `a100_train.sh` | A100 | 3-GPU 종합 스윕 원커맨드 |

---

## 4. 함정 (재발 방지 — 새 세션이 꼭 알아야)

1. **gitignore 데이터 유실:** worktree 삭제 시 `train/data`·`analysis/raw` 날아감. A100이 SoR.
   중요 산출물은 A100에 scp 백업.
2. **DB OperationalError(연결 풀 고갈):** 1000+ 쿼리 연타하면 워커 응답이 대량 실패(status err).
   → `vote_holdout.py`의 `_query_retry` 패턴(재시도+`get_ro_engine().dispose()`+sleep) 재사용.
   실행 후 반드시 **재실행 solar 정오 ≈ 동결 GT(60.5%)** 로 캡처 무결성 검증할 것.
3. **A100 pip:** `--no-deps --target=/data/tta/pylibs` 필수(torch 딸려오면 PYTHONPATH shadowing으로 CUDA 깨짐).
4. **transformers 5.13:** EXAONE-3.5 비호환 → **EXAONE-4.0**(native) 사용.
5. **구형 국내 인코더 .bin CVE 가드:** `tier2v.py`가 `check_torch_load_is_safe` 무효화(신뢰 repo 한정).
6. **중복 스윕:** 재실행 전 `ssh a100 'pgrep -f "tier[23]v.py"|wc -l'`=0 확인. `a100_train.sh`부터 kill.
7. **커밋 금지 기본:** owner 승인 전 커밋 안 함(메모리 규칙). 데이터/키 커밋 절대 금지.
8. **방법론 3원칙(제일 중요):** ① 학습 전 headroom 먼저 ② 주판정은 새 DB(held-out) ③ "이겼다"는
   반드시 fresh 데이터 재현+부트스트랩. 단일 분할/다중비교 위양성 조심.

---

## 5. 다음 실험 아이디어 (열린 스레드)

D6 본편은 종결. 더 파고들 여지:

- **A. "배포 현실 판"에서 재측정** — 회사 DB가 고정이라 가정하면 같은-DB 채점이 현실적. 거기서
  배포 게이트(margin) 선택기가 **유의하게** 이기는지 사전선언+재현으로 확정(지금은 +0.4 무의).
- **B. 캐스케이드(2차 검증)** — 선택 대신 "Solar 1차 → A.X가 검산"(model-orchestration `docs` SVC).
  예측이 아니라 사후 검증이라 전이 문제를 우회할 수 있음. E6에서 부분 실측됨.
- **C. 신호 전이 실패 자체를 분석** — "Solar가 틀리는 패턴"이 정말 DB-국소적인지, 어떤 특징이
  전이되는지 by-task-type 분할 확장(sens.py 재사용). 전이되는 신호를 찾으면 선택기 부활 가능.
- **D. 채점 메트릭 개선** — 현재 `gold⊆반환셋`(recall형)이 union을 oracle로 만듦. exact-match나
  precision 페널티 메트릭이면 앙상블 결론이 바뀌는지.
- **E. 다른 워커 조합** — 현재 solar/ax/exaone. 워커 풀을 바꾸면 headroom·불일치 구조가 달라짐.

> **주의:** 위 어떤 것도 "규칙이면 충분"이라는 D6 결론을 뒤집으려면 **§4-8 방법론**을 통과해야 함.
> 유리한 판에서 이긴 건 결론이 아니다(rigged.py가 그 증거).

---

## 6. 관련 문서
- `d6-results.md` — D6 전체 결과 + 트러블슈팅(이 폴더)
- `docs/model-orchestration.md`(repo root) — Fugu-KO 전체 모델 배정 결론
- 메모리 `fugu-ko-experiment.md` — D0~D6 통사(다른 세션에도 로드됨)
