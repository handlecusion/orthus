# 프론티어 동급성 검증 실험계획서 (Frontier Parity Plan)

작성: 2026-07-27 · 개정: 2026-07-27 (2차 — arena-benchmark 워크트리 실제 코드/raw 반영, §0 정정 포함)
상태: **Phase 0 완료 (Track E2E + Track Arena 모두 오프라인 재확인 완료)**

## 0. 1차 초안 정정 (중요)

1차 초안은 `docs/model-orchestration.md`·`AGENTS.md` 서술과 main 브랜치 `e2e/` 자산만 근거로 삼았다. 사용자 지적대로 **워크트리 코드를 직접 읽어 다음을 정정한다.**

- **"조립 V1/V2/V3"는 `orthus/models/orchestration.py::ASSIGNMENTS`에 없다.** 그 파일은 6개 워크트리 전부에서 md5까지 완전 동일한 단일 배정표(§15)이며 버전 개념이 없다.
- **V1/V2/V3는 `.worktrees/arena-benchmark`(브랜치 `feat/arena-benchmark-dataset`)에만 존재하는 사후 스티칭(post-hoc stitching) 실험**이다 — `experiments/fugu-ko/arena_v3.py`, `arena_assemble.py`, `golden/arena_gold/assignment_maps.json`. 국내 3모델을 각자 전체 문항에 돌린 뒤, 배정표대로 정답을 사후에 짜깁기(stitch)해 "만약 이 배정이었다면"을 계산하는 방식이다(실제 라우팅 스택을 태우는 것이 아님 — Phase 2에서 이 괴리를 확인한다).
- **V1 = 현행 프로덕션 배정, V2 = 슬롯별 국내 최강 3곳 교체(structured/routing/graph_bind), V3 = 통계적으로 유의한 3곳만 교체(decompose/contract/toolcall)** — V1→V2→V3가 하나의 선형 개정 사슬이 아니라 **서로 다른 3슬롯을 건드리는 두 개의 독립 실험**이다.
- **V3는 코드 docstring이 스스로 "홀드아웃 성적을 보고 골랐다"고 명시**하며, `experiments/fugu-ko/analysis/arena-prereg.md` §9 "홀드아웃 튜닝 금지" 규정 위반임을 인정한다. 그대로 프로덕션 채택 근거가 아니다 — 새 홀드아웃 재확인이 필요하다.
- **arena 벤치마크(13태스크, n=1,589, 4난이도 슬라이스)에서는 H1(동급성)이 성립하지 않는다.** contract·decompose 태스크에서 V1(현행 조립)이 6개 프론티어 전원에게 압도적으로 패배한다(McNemar p<1e-13대, 아래 §4 Track Arena 참조). 이는 1차 초안의 e2e tier_a(n=1,750, 8태스크) 기준 "전 프론티어 유의차 없음" 결론과 **정면으로 배치**된다 — 두 벤치마크가 서로 다른 태스크 taxonomy(특히 contract/toolcall/delegation/email은 arena에만 있음)를 쓰기 때문이며, 어느 한쪽만 보고 결론 내리면 안 된다.

이하 계획은 **두 개의 독립 트랙**으로 재구성한다: Track E2E(t2–t10, 8태스크)와 Track Arena(13태스크, contract/toolcall/delegation/email 포함, 난이도 슬라이스 보유). 최종 H1/H2 판정은 두 트랙을 함께 봐야 한다.

## 1. 가설

- **H1 (주가설):** 국내 모델(Solar / EXAONE / A.X)로 조립한 orthus 오케스트레이터는 한국어 업무 태스크에서 프론티어 단일 모델과 **동급(비열등)** 성능을 낸다.
- **H2 (부가설):** 현행 배정보다 나은 슬롯 조합이 존재한다. Track E2E에서는 best-per-slot(전 슬롯 solar+t10 exaone) 우위가, Track Arena에서는 V3(decompose/contract/toolcall 교체) 우위가 이미 발견됐으나 **둘 다 홀드아웃 자체에서 발견된 것이라 확정이 아니다** — 새 홀드아웃 확증이 이 계획의 핵심 목적이다.

**동급(비열등) 판정 기준:** 공통 채점셋에서 `조립 pass율 − 프론티어 최고 pass율`의 95% 부트스트랩 CI 하한이 **−2.0%p 이상**이면 동급으로 판정한다. Track별로 별도 판정하고, 최종 결론은 두 트랙 중 **더 엄격한(불리한) 쪽**을 따른다(체리피킹 방지).

## 2. 비교 대상

### 2.1 조립(우리 측) — 트랙 공통 정의

| 구성 | 정의 | Track E2E 점수 | Track Arena 점수 |
|---|---|---|---|
| **V1 (=현행 프로덕션, C1)** | §15 배정 그대로. contract/toolcall처럼 orchestration.py에 슬롯이 없는 태스크는 기본값 solar. | 1,449/1,750 (82.8%) | 1,032/1,589 (64.95%) |
| **V2 (슬롯별 국내 최강, 3곳)** | structured→exaone, routing→ax, graph_bind→solar (나머지 V1과 동일) | 미측정 | 1,040/1,589 (65.45%) |
| **C2 (E2E 전용 best-per-slot)** | 전 슬롯 solar + t10(위임)만 exaone | 1,466/1,750 (83.8%) | 미측정(taxonomy 다름) |
| **V3 (Arena 전용, 근거 기반)** | decompose→solar, contract→ax, toolcall→ax (나머지 V1과 동일) — ⚠️홀드아웃 튜닝 산물 | 미측정(taxonomy 다름) | 1,211/1,589 (76.21%) |
| 참고: 단일모델 | solar/exaone/ax 단독 | 기존 raw | 기존 raw |

두 트랙의 조합 실험(C2 vs V3)은 **서로 다른 태스크 집합**을 최적화한 것이라 이름은 비슷해도 같은 실험이 아니다 — Phase 5에서 두 아이디어를 하나의 통합 배정안으로 합칠지 별도 검토한다(예: 통합안 = t9/graph_bind는 solar, decompose/contract/toolcall은 V3, 나머지는 V1).

### 2.2 프론티어 베이스라인 (owner 지정 6종) — 워크트리별 raw 현황 실측

| 모델 | Track E2E (`golden-expand` 워크트리, n=1,750) | Track Arena (`arena-benchmark` 워크트리, n=1,589) |
|---|---|---|
| Claude Sonnet 4.6 | ✅ 1,461/1,750 (83.5%) | ✅ raw 완주(2,899/2,899) — 단 **REFERENCE 취급**(주판정 로스터 제외), 74.45%. Phase 1에서 주판정으로 승격 필요 |
| Claude Opus 4.8 | ❌ 없음 — 신규 레인 필요 | ✅ 주판정, 1,183/1,589 (74.45%) |
| GPT-5.3 | ✅ 1,451/1,750 (82.9%) | ✅ raw 완주, REFERENCE 취급, 1,227/1,589 (77.22%) — Phase 1에서 승격 필요 |
| GPT-5.6-sol | ❌ 없음 — 신규 레인 필요 | ✅ 주판정, 1,235/1,589 (77.72%) |
| DeepSeek V3.2 | ✅ 1,450/1,750 (82.9%, 슬러그 `deepseek`) | ❌ **없음** — arena raw는 `deepseek-v4-pro`만 존재(74.39%), 순수 V3.2 아님. 신규 레인 필요 |
| GLM-5.2 | ✅ 1,448/1,750 (82.7%) | ⚠️ raw는 있으나 **scored 993/2,899행으로 미완주** — 재실행 필요(`glm-5-bedrock`는 별도로 완주 1,168/1,589, 73.51%, 하지만 사용자가 지정한 건 5.2) |

**Phase 1 신규/보완 작업 정리:**
1. Track E2E: Opus 4.8, GPT-5.6-sol → tier_a 신규 레인 (2건)
2. Track Arena: DeepSeek V3.2(plain), GLM-5.2 완주 → arena 13태스크 신규/재실행 (2건)
3. Track Arena: Sonnet 4.6·GPT-5.3는 raw 있으니 재실행 없이 **주판정 로스터에 재편입**(집계 스크립트만 재실행, API 호출 0)

## 3. 데이터셋 (워크트리 자산 실측)

| 축 | 자산 | 규모 | 용도 |
|---|---|---|---|
| **Track E2E 주 벤치** | `.worktrees/golden-expand` `experiments/fugu-ko/e2e/tier_a.jsonl`(freeze.lock 동결) | 1,884행 / 공통 채점셋 n=1,750 | 8태스크(t2/t3/t5/t6/t7/t8/t9/t10) |
| **Track E2E 확증 홀드아웃** | 동 워크트리 `e2e/tier_b.jsonl` | 1,653행 | 1회성 확증 전용 |
| **Track Arena 주 벤치** | `.worktrees/arena-benchmark` `golden/arena_gold/gold_labels.jsonl` + `analysis/raw/arena/*_scored.jsonl` | 2,340문항 라벨 / 실측 완주는 시스템별 n=1,589(strict) | 13태스크(structured/routing/intent/graph_bind/wiki_qa/gap_suggest/claim_headline/**contract/toolcall/delegation/distill/decompose/email**) × happy/edge/adversarial/production 4슬라이스 |
| **Track Arena 신규 홀드아웃 (V3 재확인용)** | 동 워크트리 `golden/arena_parts/`에서 미사용 슬라이스 또는 신규 생성분 | 확정 필요(Phase 3) | V3의 홀드아웃 튜닝 편향 제거 전용 — **반드시 V3 배정을 고른 그 문항 밖에서** 측정 |
| **불변성** | `.worktrees/prism-benchmark` `golden/prism/epoch1/` + `prism/score/` 체커 21종 | 1,216 메타모픽 쌍 (12모델 결과 1,933파일 기보유) | Phase 6 — 일관성 축 비교 |
| 참고 | `.worktrees/prompt-tuning` 골든 80문항 | 80 | 조합 변경 시 회귀 스팟체크 |

주의: `golden/` 4,811문항은 전 워크트리 공통 중복이라 합산 금지. `analysis/raw/`는 gitignore — 로컬 디스크가 유일본(백업 필수, §7).

## 4. 실험 설계 (Phase)

### Phase 0 — 오프라인 재분석 (API 0회) — ✅ 완료 (2026-07-27, 2차 갱신)

#### Track E2E (n=1,750, 8태스크)

⚠️ **main 워크트리 `analysis/raw/` 8개 파일은 truncate 오염** — 완전한 raw는 `golden-expand` 워크트리가 유일본.

| 순위 | 모델 | pass/n | 정확도 |
|---|---|---|---|
| 1 | claude-sonnet-4-6 | 1461/1750 | 83.5% |
| 2 | gpt-5.3-chat-latest | 1451/1750 | 82.9% |
| 3 | deepseek(V3.2) | 1450/1750 | 82.9% |
| — | **V1 현행 조립** | **1449/1750** | **82.8%** |
| 4 | glm-5.2 | 1448/1750 | 82.7% |
| 5 | solar 단독 | 1420/1750 | 81.1% |
| — | exaone 단독 | 1406/1750 | 80.3% |
| — | baseline(gpt-4o-mini) | 1348/1750 | 77.0% |
| — | ax 단독 | 1333/1750 | 76.2% |
| — | claude-haiku-4.5 | 784/1750 | 44.8%(포맷 붕괴, 참고치) |

McNemar(n=1,750): V1 vs sonnet-4.6 p=0.266 · vs gpt-5.3 p=0.911 · vs deepseek p=1.0 · vs glm p=1.0 — **프론티어 4종 전부 유의차 없음**. V1 vs baseline p=2.98e-16 압도.

C2(best-per-slot, 전슬롯 solar+t10 exaone) = 1,466/1,750 — sonnet(1,461)도 수치상 상회. 손실 원인: t9=ax(−9, solar 만점), t5=exaone(−8).

#### Track Arena (n=1,589, 13태스크, 실측 재확인 완료)

**태스크별 정확도(V1/V2/V3):**

| task | V1 | V2 | V3 |
|---|---|---|---|
| contract | 80/220 (36.4%) | 80/220 (36.4%) | 167/220 (**75.9%**) |
| toolcall | 131/214 (61.2%) | 131/214 (61.2%) | 146/214 (68.2%) |
| decompose | 96/219 (43.8%) | 96/219 (43.8%) | 173/219 (**79.0%**) |
| delegation | 191/218 (87.6%) | 동일 | 동일 |
| distill | 179/220 (81.4%) | 동일 | 동일 |
| email | 131/219 (59.8%) | 동일 | 동일 |
| routing | 33/40 (82.5%) | 37/40 (92.5%) | V1과 동일 |
| structured | 37/40 (92.5%) | 36/40 (90.0%) | V1과 동일 |
| graph_bind | 28/40 (70.0%) | 33/40 (82.5%) | V1과 동일 |
| intent/wiki_qa/gap_suggest/claim_headline | 전 버전 동일 (87.2%/42.5%/97.5%/90.0%) | | |

**전체 정확도 순위(16개 시스템, strict n=1,589):**

| 순위 | 시스템 | pass/n | % | 로스터 구분 |
|---|---|---|---|---|
| 1 | gpt-5.5 | 1289/1588 | 81.17% | 주판정 |
| 2 | gpt-5.6-sol | 1235/1589 | 77.72% | 주판정 |
| 3 | gpt-5.3 | 1227/1589 | 77.22% | 참고→**Phase1 승격 대상** |
| — | **V3(조립 신규)** | 1211/1589 | 76.21% | |
| 4 | gpt-5.6(luna) | 1193/1589 | 75.08% | 참고 |
| 5 | claude-sonnet-4.6 | 1183/1589 | 74.45% | 참고→**Phase1 승격 대상** |
| 5 | claude-opus-4.8 | 1183/1589 | 74.45% | 주판정 |
| 6 | deepseek-v4-pro | 1182/1589 | 74.39% | 주판정(⚠️V3.2 아님) |
| 7 | glm-5-bedrock | 1168/1589 | 73.51% | 주판정(⚠️GLM-5.2 아님) |
| 8 | claude-opus-4.6 | 1127/1589 | 70.93% | 주판정 |
| — | gpt-4o | 1119/1589 | 70.42% | 참고 |
| — | ax 단독 | 1110/1589 | 69.86% | 진단 |
| — | solar 단독 | 1096/1589 | 68.97% | 진단 |
| — | **V2(조립)** | 1040/1589 | 65.45% | |
| — | **V1(조립=현행)** | 1032/1589 | 64.95% | |
| — | exaone 단독 | 1020/1589 | 64.19% | 진단 |
| — | claude-haiku-4.5 | 951/1588 | 59.89% | 참고 |

**McNemar 핵심 (Holm 보정, family=12, 집중 6태스크):**
- **contract**: V1은 6개 프론티어 전원에게 압도 패배(p=1.5e-31 ~ 4.5e-8). V3는 gpt-5.5에만 패(p=4.1e-3), opus-4.6엔 역전승(p=1.2e-5), 나머지 4종엔 동률(p≥0.70).
- **decompose**: V1 전패(p=5.0e-22 ~ 3.0e-14). V3는 **6쌍 전부 동률**(최저 p=0.175)로 회복.
- **toolcall**: V1/V3 대부분 동률, V3가 deepseek 상대만 순수 승(p=0.016).
- **delegation/email**: V1=V3(재배정 안 함), 조립이 opus-4.8·gpt-5.6-sol 상대 일부 순수승 — 단 이는 "상대(신세대 프론티어)가 delegation 판단에서 False Negative 급증"한 결과로 arena 문서가 명시. **조립 실력으로 오독 금지.**

### Phase 1 — 신규/보완 레인 (Track별)

**Track E2E**: Opus 4.8, GPT-5.6-sol tier_a(1,884문항) 신규 실행.
**Track Arena**: DeepSeek V3.2(plain), GLM-5.2 완주(현재 993/2,899 → 나머지 실행) — 13태스크 전량, 4난이도 슬라이스 포함.
**Track Arena 로스터 정리**: Sonnet 4.6·GPT-5.3는 raw 완주 상태이므로 API 호출 없이 `arena_judge.py`/`arena_v3.py`의 `EXTERNAL_SYSTEMS`에 추가해 주판정 재집계만 수행(Holm family 12→더 커짐, 재계산 필요).

대형 모델은 `_LARGE_PREFIXES` 게이트상 `--final-verify` 필요. 실행·요약은 서브에이전트 위임(출력 대량).

### Phase 2 — 조립 실측 레인 (Track별 1회)

Phase 0/1의 조립 점수는 **사후 스티칭(합성)**이지 실제 라우팅 스택(프리필터→routing→각 워커→SVC 2차 검증) 실행 결과가 아니다. 각 트랙에서 실제 스택을 1회 태워 합성치와의 괴리(fallback 발화율, 지연)를 확인한다. 괴리 ±0.5%p 이내면 이후 합성치 사용을 허용.

### Phase 3 — V3 홀드아웃 재확인 (H2, Track Arena, 최우선)

V3(decompose/contract/toolcall 교체)는 그 배정을 고른 **동일 홀드아웃**에서 나온 수치라 §0의 prereg §9 위반이 확정적이다. 이 계획의 핵심 신규 작업은:
1. arena 골든에서 V3 배정을 고르는 데 전혀 쓰이지 않은 신규 문항 집합을 만들거나(§3 표 "Track Arena 신규 홀드아웃"), tier_b 성격의 미사용 슬라이스를 지정한다.
2. 그 신규셋에서 V1 vs V3 vs (Phase 1 승격된 프론티어 포함) 재측정.
3. **V3가 신규셋에서도 decompose/contract 우위를 유지해야만 "H2 확정"으로 인정** — 유지 못 하면 V3는 홀드아웃 과적합으로 기각.

### Phase 4 — 통계 판정 (H1, 두 Track 종합)

- 각 Track에서 조립(V1, 그리고 통과 시 V3/C2) vs 프론티어 6종 쌍대 McNemar + pass율 차 95% 부트스트랩 CI. 다중비교 Holm 보정.
- **최종 H1 판정은 두 트랙 중 더 불리한 쪽을 따른다.** Track E2E만 보면 동급, Track Arena(contract/decompose 포함)까지 보면 V1은 동급이 아니다 — 이 차이 자체를 보고서의 핵심 발견으로 명시한다.
- 부가 지표: 지연 p50/p95(국내 조립의 기존 강점, 698ms 기록 참고).

### Phase 5 — Tier B / 확증 홀드아웃

- Track E2E: H1/H2 통과 구성만 tier_b(1,653) 1회 측정(반복 금지).
- Track Arena: Phase 3에서 확정된 V3(또는 기각 시 V1)만 별도 신규 슬라이스로 1회 확증.

### Phase 6 — 배정 통합 및 채택 판정 (H2 최종)

1. Track E2E의 C2 아이디어(전슬롯 solar+t10 exaone)와 Track Arena의 V3 아이디어(decompose/contract/toolcall→solar/ax/ax)를 **하나의 통합 배정안**으로 합칠 수 있는지 검토(겹치지 않는 슬롯이므로 원칙적으로 합성 가능 — graph_bind/t9=solar, decompose=solar, contract/toolcall=ax, 나머지 V1 유지).
2. 채택 기준: 두 Track 신규 홀드아웃에서 통합안 ≥ V1 (유의) **그리고** 안전 표면 무회귀(delegation_extract 오탐 0 — EXAONE 유지는 성능 아닌 안전 근거이므로 손대지 않음) **그리고** A.X 실격 이력 표면(decompose/distill/email JSON 실패, delegation 오탐) 재진입 금지 — A.X를 새로 추가하는 슬롯이 있다면(contract/toolcall) 그 두 태스크에서 A.X의 과거 실격 사유(JSON 미준수 등)가 재현되는지 별도 확인.
3. 채택 시 `docs/model-orchestration.md` §15 개정 + `orthus/models/orchestration.py::ASSIGNMENTS` + `test_model_orchestration.py` 고정 테스트 갱신 PR(owner 게이트).

### Phase 7 — 확장 검증 (선택)

arena adversarial 슬라이스(4슬라이스 중 adversarial)와 PRISM 메타모픽 위반율(1,216쌍)로 "정확도 동급이어도 강건성/일관성은 다른가"를 비교 — 발표 자료의 차별화 축.

## 5. 채점·통계 방법 요약

- **Track E2E**: exact match 결정론 채점이 주(~98%): `score_l1_exact`, t3 counts-only, t10 존칭 정규화. LLM judge는 t2/t8뿐(양방향 스왑).
- **Track Arena**: exact match + Holm 보정 McNemar(family=12), discordant<25 자동 "증거부족" 처리(약한 검정력 방지), n_common<task_full_n 자동 증거부족.
- 신규 증강 생성은 하지 않는다(기존 동결 세트 사용) — 생성기-피평가 모델 분리 원칙 유지. arena 신규 홀드아웃(Phase 3)이 필요하면 **V3를 고르는 데 관여하지 않은 생성기/방식**으로 만들어야 한다(prereg §9 "문항 생성기 금지 목록" 준수 — 로스터 내 모델은 생성기로 쓰지 않음).

## 6. 실행·비용 계획

| 항목 | 규모 | 비용 성격 |
|---|---|---|
| Phase 0 | 0 API 호출 | 로컬 CPU만 |
| Phase 1 (E2E) | 1,884문항 × 2모델(Opus 4.8, GPT-5.6-sol) | 신규 지출 |
| Phase 1 (Arena) | 1,589문항 × 2모델(DeepSeek V3.2, GLM-5.2 완주분) | 신규 지출 |
| Phase 1 (Arena 로스터 편입) | 0 API 호출 | 재집계 스크립트만 |
| Phase 2 | Track별 조립 1레인 | 국내 3사 API(저가) |
| Phase 3 | 신규 arena 홀드아웃 × (V1/V3 + 프론티어 일부) | 핵심 신규 지출 — 규모는 홀드아웃 크기 확정 후 산정 |
| Phase 5 | tier_b 1레인 + arena 신규 슬라이스 확증 | 혼합 |

- 실행 규칙: 레인 실행은 nohup/tmux + 파일 리다이렉트, 진행·요약은 서브에이전트가 담당(대량 출력 컨텍스트 격리).
- Track E2E raw는 `golden-expand` 워크트리 `analysis/raw/`에 슬러그 규칙(`e2e_<slug>.jsonl`)대로 저장. Track Arena raw는 `arena-benchmark` 워크트리 `analysis/raw/arena/`에 저장.
- A.X 레인은 3 req/s 캡 준수.

## 7. 리스크·함정 체크리스트

- [ ] **1차 초안의 "조립 = orchestration.py 단일 개념" 전제가 틀렸다** — 실제로는 워크트리별 사후 실험(V1/V2/V3, C2)이 병존. 결과 보고 시 어느 워크트리·어느 스크립트에서 나온 숫자인지 반드시 명시.
- [ ] Track E2E와 Track Arena의 결론이 다르다(동급 vs 비동급) — **더 유리한 쪽만 인용하지 않는다.**
- [ ] V3는 홀드아웃에서 발견돼 그 자체가 과적합 위험(prereg §9 위반 자인) — Phase 3 재확인 전에는 "개선"으로 발표하지 않는다.
- [ ] arena `glm-5.2_scored.jsonl`이 993/2,899로 미완주 — 그대로 집계에 쓰면 안 됨.
- [ ] arena에는 순수 DeepSeek V3.2 raw가 없다(`deepseek-v4-pro`만 존재) — 사용자가 지정한 모델과 다른 버전을 잘못 인용하지 않도록 주의.
- [ ] main 워크트리 `analysis/raw/` truncate 오염 — Track E2E는 `golden-expand` 워크트리 raw만 사용.
- [ ] Bedrock는 `us.` inference prefix 필수, Sonnet 4.6만 bare ID(버전 suffix 금지).
- [ ] `analysis/raw/`는 gitignore — 실험 전 로컬 백업, 산출 통계(md/json 요약)만 커밋.
- [ ] 루트 `api_calling_test.py`에 Bedrock bearer 키 평문 하드코딩 — **즉시 삭제 + 키 회전**.
- [ ] Tier B 및 arena 신규 홀드아웃은 1회성 확증 전용 — 튜닝 루프에 재사용 금지.
- [ ] GPT-5.6-sol/GPT-5.5/GPT-5.6(luna) 대표 모델 명명이 arena 문서 내에서도 사후 교체된 이력(luna→sol) — 실행 전 정확한 model id를 스모크 1문항으로 검증.
- [ ] EXAONE(Friendli dedicated) timeout 120s / thinking off 유지, A.X 3 req/s 캡.

## 8. 산출물

1. `analysis/e2e_report.md`(Track E2E) + arena 쪽 리포트(Track Arena, 신규 또는 `arena-p6a-verdict.md` 후속) 갱신: V1/V2/V3/C2 vs 프론티어 6종 순위표 + task별 분해 + McNemar/CI 표, 두 트랙 병기.
2. H1 판정문(Track별 + 종합 판정, 동급 여부·마진), H2 판정문(Phase 3 신규 홀드아웃 기준 채택 여부 + 근거).
3. 채택 시 `docs/model-orchestration.md` §15 개정 PR(코드 `ASSIGNMENTS` + 고정 테스트 + 문서), 미채택 시 기각 근거(과적합 재현 실패 등) 기록.
