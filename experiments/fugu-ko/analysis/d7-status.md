# D7 진행 상태 — 세션 재개용 (2026-07-17 갱신)

## -4. 최신: D9 완료 — 실험 전체 종결, 남은 건 보고서뿐 (2026-07-17)

**D9 = "현행 GPT → 국내 스택 교체" 측정** (`d9-prereg.md` LOCKED, `judge_d9.py` 1회,
상세 `d7-results.md` **E16**). 현행 프로덕션 슬롯 gpt-4o-mini를 D8 동일 판정셋에 첫 투입
(baseline k=5, 7,190콜, 194.7분).

| arm | D8셋 | **실제 DB분포 재가중** |
|---|---|---|
| 현행 = gpt-4o-mini 맨몸 | 52.2% | **75.0%** |
| C2b = SFT 1.2B 자체스택(외부 API 0) | 91.9% | 88.6% |
| **C2a = solar + R+** | **98.8%** | **98.6%** |

- **C2a > 현행: 전 가중에서 우월 유지(+2.9 ~ +46.5p) = 견고.** owner가 원한 "국내 스택을
  잘 조립하면 현행을 이긴다" **성립**.
- ⚠️ 자가 적발: D8셋은 함정 58.4%(실제 29.4%) → GPT에 불리. gpt는 **순수클린서 99.0%**.
  그래서 가중 민감도로 재검했고 방향은 안 뒤집혔다.
- ❌ 못 쓰는 주장 2개: **C2b는 견고하지 않음**(보수 가중 −10.0p 역전), **"국내가 더 낫다"**도
  과표집 산물(재가중 +0.3p ≈ 무승부) → 정직한 표현은 **"뒤지지 않는다"**.

**실험 전부 종결.** 남은 것 = `competition-report.md`에 D6~D9 이식(그 문서는 G3까지만 커버).
보고서 인용은 `d7-results.md` **§0 · §3′ · §7′ · E14~E16**만 — 중간 로그엔 뒤집힌 수치 다수
(§7′ "인용 금지 목록" 참조).

## -3. D8 본판정 — 학습기 패배 (2026-07-17)

**결과: R+ 98.8% vs c8 94.3% (McNemar 득14/실79, p=3.62e-12, 부트스트랩 P(c8>R+)=0.0%).**
§4 승리 기준 전부 미충족 → D7 §9 문안 발동. 상세·태그별·해석은 `d7-results.md` **E14**.
전 수치 `train/data/judge_d8_result.json`(per-item 포함).

- 백본 동결 = **sft12b_v4**(학습셋 val 타깃 정확일치 96.2% > 32B 94.9%; `val_select.py`,
  `train/data/backbone_choice.json`). 32B QLoRA는 207분 학습했으나 1.2B에 졌다.
- zero 슬라이스 비열등 충족(c8 100.0 vs R+ 99.1) — probe-0을 양쪽에 준 공정 설계로 D7에서
  c2의 유일한 구조적 우위였던 zero 구멍이 규칙으로 메워짐.
- **잔여 작업 없음.** 승리 시 의무였던 독립 재현셋은 패배로 불발동. 대회 보고서는
  `d7-results.md`(E1–E14 + I1–I7 + T1–T18)가 SoR.

## -2. D8 재도전 준비 (완료 — 판정으로 종결)

D7 본판정 무승부(§-1) 후 E12 부검에서 시험지 결함(조사 마커 26.9%) 발견 → **D8 1회
재판정 결정**(사유·클린 슬라이스 근거는 `d7-results.md` E12, 준비 상세는 E13).

- **`analysis/d8-prereg.md` LOCKED (2026-07-17):** c8 = SFT k=5 자기일관성 + snap +
  probe-0 + 폴백(solar t0→ax) / R+ = 기존 + probe-0. 주판정 백본 = sft32b_v4
  (학습셋 val에서 sft12b_v4가 더 좋으면 판정 전 교체·동결). 헤드룸 게이트 폐지.
- **판정셋:** `golden/t3_d8_holdout.json` 1,438문항(합성 12DB·신규 템플릿 17·zero 8%·
  결함 2종 수정). 생성기 `train/build_d8_holdout.py` (`--wipe` 지원).
- **워커 캡처 완주(2026-07-17):** `analysis/raw/d8_worker_sql.jsonl` 10,066줄.
  t0 단독: solar 73.5 · ax 58.3 · exaone 64.3.
- **프롬프트:** `train/data/sft_d8_prompts.jsonl` 1,438건 생성 완료.
- **판정기:** `train/judge_d8.py` 작성 완료 + probe-0 스모크 6/6 PASS. **아직 미실행**
  (prereg §5: 학습→백본 동결→c8 생성 후 1회). 물리 컬럼명은 `properties`(T13).
- **블로커 = A100 미접속.** 감시 루프 2종 detached 가동: `a100_retry_launch.sh`(재접속 시
  v4 데이터·백업 업로드 + 32B/1.2B v4 학습 자동 발사, 로그 `train/logs/a100_retry.log`) +
  `a100_d8_upload.sh`(D8 산출물 업로드, 로그 `train/logs/a100_d8_upload.log`).

**재개 절차(A100 복구 후):**

```bash
# 1) 학습 완료 확인 (32B QLoRA ~1h 예상)
ssh a100 'tail -3 /data/tta/fugu-ko/train/logs/sft32b_v4.log /data/tta/fugu-ko/train/logs/sft12b_v4.log'
# 2) 백본 선택 — 학습셋 val 생성+채점 양쪽, 더 좋은 쪽 동결 (판정셋 무접촉!)
ssh a100 'cd /data/tta/fugu-ko && CUDA_VISIBLE_DEVICES=0 python3 train/sft_worker.py --gen train/data/sft_val.jsonl --ckpt train/ckpt/sft32b_v4 --quant4 --out_file train/data/sft_val_gen_32b_v4.jsonl'  # 1.2B도 동일
# 로컬 채점: train/sft_score.py --gen ... --ref train/data/sft_val.jsonl
# 3) c8 생성 k=5 (프롬프트 data/sft_d8_prompts.jsonl, GPU 분산 — id 해시로 샤딩하거나 파일 split)
#    python3 train/sft_worker.py --gen train/data/sft_d8_prompts.jsonl --ckpt <동결백본> [--quant4] --k 5 --out_file train/data/sft_d8_gen.jsonl
# 4) 로컬 본판정 1회: $PY train/judge_d8.py --gen_file data/sft_d8_gen.jsonl
# 5) 승리 시에만: 독립 재현셋(다른 seed·다른 합성 DB) 생성→캡처→생성→재판정
```

## -1. prereg LOCKED + fresh 판정셋 생성 + 캡처 진행 중 (D7 — 완결)

- **prereg §10 동결 명세 추가 후 LOCKED** (c2 = sft12b_v2+스냅+null폴백 / R+ = solar k5
  자기일관성+스냅+폴백+수리 / fresh 셋 명세 / 본판정 절차).
- **fresh 판정셋 생성 완료:** `golden/t3_fresh_holdout.json` — **1,063문항**, 신규 DB 10종
  (미사용 실DB 8 + 합성 2: `'데모 DB A '`(끝공백) 18행 · `'데모 DB B'` 15행,
  `db_id='synthetic-fresh-<sha1>'`로 격리 DB 삽입), 신규 템플릿 29종(기존과 중복 0 assert),
  zero 문항 87(8%). 생성기 `train/build_fresh_holdout.py` (`--wipe`로 합성 제거 가능).
  ⚠️ 버그 이력: 합성 db_id를 `hash()`로 만들면 프로세스 salt로 중복 삽입됨(gold 오염) →
  sha1로 수정, wipe 후 재생성 완료(gold 정상 확인).
- **진행 중(전부 detached — 세션 무관):** ① 로컬 `capture_fresh.py` solar k=5(5,315콜) ②
  로컬 ax→exaone(2,126콜; exaone이 롱폴) → `analysis/raw/fresh_worker_sql.jsonl`(재개 지원)
  ③ A100 c2 생성(`sft_fresh_gen_v2.jsonl`). 프롬프트 `train/data/sft_fresh_prompts.jsonl`.
- ~~다음: 헤드룸 게이트 → R+ 풀 계산 → c2 채점 → §6 판정~~ → **본판정 완료(2026-07-17 새벽,
  `train/judge_fresh.py` 1회 실행, 상세 `d7-results.md` E11):**

| 시스템 | fresh 1,063 |
|---|---|
| solar(D6 챔피언) | 63.0% |
| oracle(고르기 천장) | 74.5% |
| **R+ (규칙기)** | **83.5%** |
| **c2 (학습기)** | **81.8%** |

  **판정 = 학습기 승리 주장 불가(§9 발동).** McNemar 득100/실118 p=0.25(무승부), 부트스트랩
  P(c2>R+) 10.4%, 게이트도 FAIL(단 R+>oracle이라는 게이트 설계 결함 병기). 정답0 슬라이스는
  c2 유의 우위(86.2 vs 71.3, p=0.0002), groupby도 c2 우위(90.0 vs 60.0) — 상보성 큼(단순
  태그 스위칭 상한 ~88%+는 **미청구 후속 가설**, 주장하려면 D8 prereg부터).
  진짜 성과 = R+ 자체(63.0→83.5, +20.5p — D7이 만든 자기일관성+수리+스냅).
  ⚠️ A100 백업 일부 보류(판정 직후 tailscale DNS 불안정) — `judge_fresh_result.json`·
  `fresh_worker_sql.jsonl`·`judge_fresh.py` scp 재시도 필요.

## 0. 최신: v2 결과 + v3 진행 중

**v2 (함정클래스 증강) 홀드아웃 프리뷰 — 돌파:**

| 지표 | v1 | **v2** |
|---|---|---|
| 단독 | 49.9% | **60.7%** (solar 60.5 동률) |
| 세 워커 전멸 148 회수 | 24 (16%) | **80 (54%)** |
| 4후보 oracle | 73.2% | **85.3%** (3워커 68.0) |
| **학습기 시스템** (SFT 1차→null이면 solar→ax) | — | **82.9%** |

- **학습기 시스템 82.9% vs 규칙기 R+근사 65.0% — 득91/실8, McNemar p=5.9e-19 (프리뷰).**
- **3워커 oracle(68.0%)을 넘었다** = 어떤 고르기/투표 규칙도 도달 불가능한 값. 구조적 승리 신호.
- v2 남은 오답: `' 아틀라스 링크'` 앞공백 창작 48건(증강 과잉일반화 부작용), `파트너`↔`파트너사` 18건,
  값 복사손상('지부장'→'지점장') 84건.
- **v3 결과(완료): 기각.** 대칭 변이+혼동쌍 906건을 더 넣었지만 v2보다 약간 나쁨
  (단독 58.5/시스템 81.9) — 증강은 v2에서 수확 체감. 앞공백 창작 48건은 증강으로 안 잡힘.
- **최종 후보 = v2 + db_name 스냅 글루(2026-07-16 밤):** 생성 SQL의 db_name이 카탈로그에
  없으면 공백무시 '유일'일치 항목으로 교체(결정론, 카탈로그는 배포 시 항상 가용).

| 구성 | 단독 | 전멸148 회수 | 시스템 |
|---|---|---|---|
| v2 | 60.7% | 80 | 82.9% |
| **v2+스냅 (동결 후보)** | **70.2%** | 80 | **83.2%** (vs 규칙기 65.0, 득92/실8, p=3.2e-19) |
| v3 / v3+스냅 | 58.5 / 68.0 | 74 | 81.9 / 82.1 |

  단독 70.2%는 **단일 워커로 3워커 oracle(68.0%)을 초과** — 고르기 규칙이 도달 불가능한 값.
- **동결할 학습기 시스템(c2) 정의:** `ckpt/sft12b_v2` + db_name 스냅 + null 폴백(→solar→ax).
- ⚠️ 스냅 글루는 홀드아웃 프리뷰 오답을 보고 고안 — 동결 463은 이제 완전한 **개발셋**이다.
  정식 판정 fresh 셋은 무접촉 유지, 거기서 한 번만 잰다(prereg §5와 정합).
- 남은 정식 절차: ① prereg LOCK(c2 정의 포함) ② fresh 판정셋 생성(신규 DB·신규 표현·n≥900·
  정답0 ~10%) ③ **풀 R+**(자기일관성 k=5 + null폴백 + SQL수리) fresh 실측 ④ 본판정 ⑤ 독립 재현.
  (32B QLoRA는 선택 — v2+스냅 시스템 83.2%가 4후보 천장 85.3%에 근접해 이득 제한적.)

> 세션이 끊겨도 이 문서 + `d7-prereg.md` + `d7-execution-guide.md`로 이어받는다.
> **A100에서 v2 학습+홀드아웃 생성 파이프가 setsid로 돌고 있다** — 세션과 무관하게 완주함.

## 1. 지금까지의 결과 (전부 실측)

### R+ 사다리 (학습셋 파일럿 150, `rplus_ladder.py`)
| 정책 | 정답률 |
|---|---|
| R0 무조건 Solar | 69.3% |
| R1 +자기일관성(k=5 다수결) | 78.0% |
| R2 +null 폴백 | 82.0% |
| R+ (+SQL수리) | **82.7% = oracle** |

**함의: "고르기" 학습기는 자리가 0 (규칙이 고르기 천장 도달). D6는 고르기에 대해 옳았다.**
남은 땅 = 세 워커 전멸 구간(학습셋 17%, 홀드아웃 32%) — SFT 워커만 도달 가능.

### SFT 워커 v1 (EXAONE-4.0 1.2B full FT, 13.8분, A100)
| 판정 | 결과 |
|---|---|
| 인도메인 val 150 | **94.0%** (같은 문항 solar 64.0) — 파싱/게이트/실행 실패 0 |
| 홀드아웃 463 (새 DB, 프리뷰) | **49.9%** vs solar 60.5 — **단독 전이 실패** |
| 세 워커 전멸 148 중 구조 | **24건 회수(16%)** — 규칙 불가능 영역에서의 첫 회수 |
| 4번째 후보로 추가 시 oracle | 68.0% → **73.2% (+5.2p)** |

**실패 원인 분해 (전부 교정 가능 클래스):** 오답 232건 중 **100건 = `'직원 '` 끝공백 DB 단일
클래스**(학습 데이터에 0건이라 못 배움), 다수 = 필터값 변형 환각('과거자료'→'과거의 자료').

### v2 (지금 A100에서 진행 중)
`build_sft_data.py`에 함정 클래스 증강 추가: db_name 변이 1,045건(끝공백/nbsp/이모지,
절반은 clean 이름과 짝 함정 → "질문 문자열과 최장 정확일치 복사" 교육) + 필터값 변이
372건(특수문자 값 verbatim 복사). 파이프: 학습(sft12b_v2) → 홀드아웃 463 자동 생성.

## 2. 재개 절차 (다음 세션이 할 일)

```bash
# 0) env (d6-handoff.md §3 동일)
cd <repo>/.worktrees/fugu-ko-selector-v2/experiments/fugu-ko
PY=<repo>/.venv/bin/python

# 1) v2 파이프 완료 확인 (학습 ~14분 + 생성 ~10분; HF 504 재시도로 지연 가능)
ssh a100 'tail -3 /data/tta/fugu-ko/train/logs/sft12b_v2.log; tail -2 /data/tta/fugu-ko/train/logs/gen_holdout_v2.log'
# "[gen] done" 뜨면:

# 2) 다운로드 + 채점 (v1과 비교)
scp -q a100:/data/tta/fugu-ko/train/data/sft_holdout_gen_v2.jsonl train/data/
$PY train/sft_score.py --gen train/data/sft_holdout_gen_v2.jsonl --ref train/data/sft_holdout_prompts.jsonl

# 3) 판정 기준(프리뷰): v2가 '직원 ' 100건을 잡았는가? 단독 60.5%(solar)를 넘는가?
#    전멸 148 회수율이 16%에서 얼마나 오르는가?
```

## 3. v2 결과별 다음 수

- **v2 ≥ 65% & 전멸 회수 ↑** → 전이 가설 실증. 32B QLoRA 스케일업 + prereg LOCK →
  fresh 판정셋 생성(§2: 신규 DB·신규 표현·n≥900·정답0 ~10%) → 본판정.
- **v2 ~55-65%** → 오답 재분해, 남은 클래스 증강 v3 (반복).
- **v2 < solar** → 1.2B 한계 가능성. 32B QLoRA로 같은 데이터 재시도 후에도 안 되면
  prereg §9 문안대로 "D6가 옳았다" 보고 준비.

## 4. 자산 (전부 A100 `/data/tta/fugu-ko`에 백업 완료)

| 자산 | 위치 |
|---|---|
| SFT 데이터 v2 (증강) | `train/data/sft_train.jsonl` / `sft_val.jsonl` |
| v1 체크포인트 | A100 `/data/tta/fugu-ko/train/ckpt/sft12b` |
| v2 체크포인트(진행중) | A100 `.../ckpt/sft12b_v2` |
| v1 생성물 | `train/data/sft_val_gen.jsonl` / `sft_holdout_gen.jsonl` |
| 홀드아웃 프롬프트 | `train/data/sft_holdout_prompts.jsonl` |
| 스크립트 | `train/{build_sft_data,sft_worker,sft_score,build_eval_prompts,repair,rplus_ladder,sc_pilot,diag_exec_signal,capture_holdout_sql}.py` |
| 파일럿 캡처 | `train/data/sc_pilot_solar.jsonl` (150×k5, SQL 포함) |

⚠️ 함정 리마인드: 로컬 `train/data/`·`analysis/raw/`는 gitignore — worktree 삭제 시 유실,
A100이 SoR. A100 pip은 `--no-deps --target=/data/tta/pylibs`. 채점은 로컬만 가능(격리 DB).
