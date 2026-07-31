# 세션 인수인계 — E2E 벤치마크 (다른 세션에서 이어감)
> 단일 진입 문서. 이 파일부터 읽을 것. 상세는 아래 "참고 문서" 목록으로 분기.
> 작성 시점: 2026-07-21 오전 (Phase 5+6 실행 완료, 리포트 작성 진행 중 스냅샷) · 미커밋(untracked) on `main`.

## 지금 상태 한 줄 요약
**Phase 0~6 실행 전부 완료**(Bedrock 5종만 키 문제로 제외). 7모델(Solar/A.X/EXAONE/baseline/GPT-4o/DeepSeek/GLM-5.2) 비교 통계 산출 완료. `analysis/e2e_report.md`는 Phase 5 파트 + Phase 6 파트(6.0~6.4, 7모델 통합 결과·측정 이력·한계 포함) **전부 작성 완료**. 다음 세션 할 일은 **t2/t8 judge 정성 평가 → Bedrock 키 해결되면 5종 실행 → 커밋** 세 가지.

## ⚠️ 꼭 알아야 할 것 (보안, 계속 유지)
- API 키 **값**은 어떤 파일/서브에이전트 프롬프트/채팅 응답에도 쓰지 않는다. 이름/상태만 기록한다. `.env`는 gitignore됨(확인됨), `keys.json`도 `experiments/fugu-ko/.gitignore`에 방어 패턴 추가됨(`keys.json`, `**/keys.json`).
- NC VARCO는 벤치 스코프에서 제외(2026-07-21 사용자 결정) — 하네스에 슬롯 없음.
- `.env` cat 금지. 새 세션도 이 원칙을 그대로 따를 것.

## ⚠️ Phase 5 실행 중 발견된 2가지 결함 — 이미 수정/재실행 완료, 재발 방지용으로 기록
**(a) DB 타깃 오설정 — t3 exact 18건이 전 모델에서 가짜로 실패했던 원인**
- 최초 Phase 5 전량 런이 **빈 `orthus` DB**를 보고 돌았다. `ORTHUS_PG_DSN`과 `ORTHUS_PG_DSN_READONLY` **둘 다** override해야 하는데, `.env`의 RO DSN이 `orthus`로 하드코딩돼 있어 하나만 바꾸면 안 됐다.
- 게다가 staging 이름이 붙은 DB는 하네스가 **자동 TRUNCATE**하는 함정이 있음 — 아무 DB나 override 대상으로 넣으면 안 됨.
- **올바른 타깃은 `orthus_company`(live read-only)**. 다음 세션이 라이브 런을 다시 돌릴 일이 있으면 이 DSN 페어를 반드시 확인할 것.
- t3 tier A만 재실행한 결과: `analysis/raw/t3_rerun_orthus_company/`
- 빈 DB로 돈 최초 전량 런 백업(참고용, 통계에 쓰지 않음): `analysis/raw/phase5_full_orthus_db/`

**(b) t10 채점기 존칭 버그**
- assignee 이름에 "님" 존칭이 붙으면 완전일치 채점이 실패하던 버그. `harness_e2e.py`에 `_strip_honorific` 수정 적용.
- 오프라인 재채점 결과: EXAONE만 영향받음, +6건 회복(15/22 → 21/22). 다른 모델은 이 버그의 영향 없음(또는 이미 존칭 없이 답함).

## Phase 5 최종 결과 (corrected, DB 재실행 + 채점기 수정 반영, scored n=145)
```
EXAONE   81.38%  >  baseline  79.31%  >  Solar  77.24%  >  A.X  75.17%
```
유의한 쌍은 **EXAONE > A.X, p=0.049(경계)** 하나뿐. 나머지 쌍은 유의차 없음(기존 국내 3모델 무유의차 결론과 일관).
데이터: `analysis/raw/phase5_final_stats.json`

## Phase 6 실행 결과 (Bedrock 제외, GPT-4o + DeepSeek + GLM-5.2, `orthus_company` 양 DSN + 수정된 채점기로 실행)
- **GPT-4o**: 전량 완료(26분). 로그 `analysis/raw/e2e_phase6_live.log`.
- **DeepSeek(공식 API)**: 전량 완료. 로그 `analysis/raw/e2e_phase6_deepseek.log`. (deprecate 예정이던 `deepseek-chat` 이슈는 이 실행으로 이미 해소됨 — 더 이상 임박 이슈 아님.)
- **GLM-5.2**: 잔액 사건 발생 — reasoning 출력 과금이 예상보다 커서 전량 실행 시 예상 비용이 **$30-40**로 치솟았고, 이미 $10이 순차 실행 중 소진됐다. **사용자 승인 하에 tier A로 축소 실행**(~35분). 순차 실행이 t3 477개 처리 중 시간당 40개로 급감하는 병목이 발견돼 중단 후 **병렬 분리 재실행**으로 완료. 로그 `analysis/raw/e2e_phase6_glm.log`.
- ⚠️ **GLM은 tier A로 축소됐으므로 GLM이 포함된 비교(7모델 통합)는 GLM에 한해 tier A 대상 결과라는 점을 리포트에 명시할 것** — 다른 6모델은 전량(tier A+B) 기준.

## 7모델 통합 비교 (공통 scored n=145 — 전 모델이 답변한 교집합 기준)
```
DeepSeek  83.45%  >  EXAONE  81.38%  >  baseline=GLM  79.31%(동률)  >  GPT-4o  78.62%  >  Solar  77.24%  >  A.X  75.17%
```
- **DeepSeek이 EXAONE을 제외한 전 모델에 유의 우위.**
- DeepSeek vs EXAONE: 동률(p=0.45, 유의차 없음).
- GPT-4o vs baseline(gpt-4o-mini): 동률(p=1.000, 유의차 없음).
- error 30~60건은 전부 429 rate-limit로 인한 것이고, scored 셋과 교집합 0(즉 rate-limit 에러가 채점 결과를 오염시키지 않음 — 별도 카운트).
데이터: `analysis/raw/phase6_verified_stats.json`

## `analysis/e2e_report.md` 작성 상태
- Phase 5 파트(국내 3모델 + baseline, corrected 결과, caveat 3개 반영)는 **작성 완료**.
- Phase 6 파트(6.0~6.4 섹션, 7모델 통합 결과·측정 이력·한계 포함) **작성 완료**.

## 데이터셋 승인 게이트 — 사용자 직접 승인 → 오케스트레이터 감사 판정으로 대체됨 (이전 세션에서 확정, 유효)
"사용자가 Tier A/B 데이터셋을 직접 확인 후 승인"하던 기존 게이트를 **오케스트레이터의 감사 판정**으로 대체했다. 감사 결과:

**판정: PROCEED-WITH-CAVEATS** (무결성 흠결 없음)
- caveat ①: 실행분의 75%가 t3 structured 태스크라 최종 리포트는 반드시 **task별 분해 보고**를 해야 한다.
- caveat ②: 제외된 pending 62건은 L2 모델-변별 아이템이라, 지금 L2는 사실상 **파이프라인 검증 성격**이고 완전한 모델 변별력은 아직 없다.
- caveat ③: anchor 24개는 **사람 검증 미수행**.
- (신규, 이번 세션) caveat ④: **GLM-5.2는 예산 사유로 tier A만 실행** — 7모델 통합 비교에서 GLM 항목은 다른 모델과 tier 커버리지가 다르다는 점을 리포트에 명시해야 함.

## API 키/모델 상태 (최신)
- **Solar / A.X / EXAONE / GPT-4o-mini(baseline) / GPT-4o / DeepSeek(공식 API) / GLM-5.2**: 전부 동작 확인, Phase 5/6 실행 완료.
- **Bedrock**: 여전히 403 인증 실패 — **아직 미해결**. 사용자가 발급처에 문의 중. 해결되면 사용자가 알려줄 것 — 그 전까지 다음 세션이 먼저 재시도할 필요 없음.

## 남은 대형모델 라운드: Bedrock 5종 (키 해결 후에만)
사용자가 키 문제 해결을 확인해주면, `--final-verify --models bedrock:...` 형태로 5종(Claude Sonnet 4.6, Claude Haiku 4.5, Llama 3.3 70B, Llama 3.1 8B, Nova Pro — DeepSeek는 이미 Bedrock 아닌 공식 API로 완료됐으므로 Bedrock 목록에서 제외) 실행. `us.` 접두 필수, `us-east-1` 리전. 확정 modelId는 `PHASE6_MODEL_IDS.md` 참조.

## 잔여 작업 (다음 세션 우선순위, 3가지만 남음)
1. t2(wiki_qa)/t8(정성) judge 평가 — 아직 미실행.
2. Bedrock 5종 — 사용자의 키 해결 확인 대기, 확인되면 즉시 실행 가능(커맨드는 위 참조).
3. 커밋 — 여전히 미실행. 방침대로 `.worktrees/` 브랜치로 옮겨서 진행할 것.

## 미해결/사소한 것
- (해소됨) `LIVE_BENCH_RUNBOOK.md` §1.2 stale CWD 문구, §5 deepseek 슬러그 반영 — 이전 세션에서 수정 완료.
- DeepSeek deprecation(07-24) 이슈는 **이미 실행 완료로 해소됨** — 더 이상 신경 쓸 필요 없음.
- `LIVE_BENCH_RUNBOOK.md`에 위 "DB 타깃 오설정" 함정(orthus vs orthus_company, staging 자동 TRUNCATE)이 아직 명시적으로 반영 안 됐을 수 있음 — 다음 세션이 §1/§3에 요약 추가해두면 재발 방지에 도움됨.

## 병렬로 할 수 있는 것 (하네스 실행과 무관)
- `experiments/fugu-ko/e2e/l2/USER_FILL_CHECKLIST.md`: 62개 pending stub 채우기 + 24개 human-verified anchor 손검증(caveat ②③과 직결). g3-X는 기존 골든(`golden/t10_holdout2.json` h-01/03/07/15/27/30) 재사용 가능.

## 참고 문서 (상세는 여기서)
- `STATE.md` — Phase별 체크포인트, **가장 최신/권위 있는 진행상태**.
- `RESUME_RUNBOOK.md` — 더 이른 시점(Phase 2 진행 중)에 쓰인 재개 문서. Phase 상태 표는 낡음(STATE.md를 신뢰할 것).
- `HARNESS_RECON.md` — harness.py/모델 배선 인수인계(Phase 4 구현 시 사용, 이미 반영 완료).
- `SMOKE_FAIL_TRIAGE.md` — 8건 스모크 실패 triage(2 real bug + 6 stale golden).
- `LIVE_BENCH_RUNBOOK.md` — 로컬 라이브 실행 커맨드 전체(§1 키 설정/§2 카나리아/§3 Phase5/§4 통계/§5 Phase6 게이트, deepseek 슬러그 반영됨).
- `PHASE6_MODEL_IDS.md` — Bedrock 확정 modelId 5종 + DeepSeek 공식 API 전환 반영.
- `REMOTE_GPU_RUNBOOK.md` — A100(tailscale) 원격 실행 시 참고(이번 세션은 로컬에서 실행).
- `l2/DESIGN.md` + `l2/USER_FILL_CHECKLIST.md` — L2(g1-g4) 137아이템 설계 + 62 stub/24 anchor 목록.
- `inventory.json` / `manifest_schema.md` / `build_manifest.py` / `build_tier_b.py` / `freeze.lock` — 데이터셋 빌드 산출물.
- `analysis/raw/phase5_final_stats.json` / `analysis/raw/phase6_verified_stats.json` — 최종 통계 산출물(gitignored, 로컬에만 존재).
- `analysis/e2e_report.md` — 최종 리포트(작성 중).

## 오케스트레이션 방침 (사용자 지시, 계속 유지)
모든 read/write는 서브에이전트에 위임, 각 ≤10문장 요약만 받는다. sonnet 기본, 복잡한 것만 opus, haiku 미사용. 중간 보고 불필요(사용자 지시). 커밋은 아직 안 함 — 커밋 시점엔 `.worktrees/` 브랜치로 옮겨서 진행(방침 확정, 아직 미실행).
