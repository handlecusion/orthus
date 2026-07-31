# 복붙 런북 — Solar 임베딩 품질 실측

설계/근거는 `README.md`. 이 파일은 **그대로 복사해 붙이는 실행 절차**만 담는다.

> ⚠️ **이 런북의 STEP 2~4는 회사 위키 원문을 Upstage(외부)로 전송한다.**
> 에이전트가 못 돌리는 이유가 그것이다 — 실행 = 그 결정을 내리는 것.
> STEP 1이 정확한 전송 분량을 먼저 보여주니 그걸 보고 결정하라.

---

## STEP 0 — 준비 상태 확인

```bash
cd <repo> && \
set -a && source ~/.orthus/nodes/company/node.env && set +a && \
echo "load  : $(cut -d' ' -f1-3 /proc/loadavg)  (코어 $(nproc) — 이보다 높으면 LLM 호출이 타임아웃난다)" && \
echo "DB    : $(.venv/bin/python -c 'import os,sqlalchemy as sa; sa.create_engine(os.environ["ORTHUS_PG_DSN_READONLY"]).connect(); print("OK")' 2>&1 | tail -1)" && \
echo "keys  : $([ -f "<로컬 키 저장소>/keys.json" ] && echo OK || echo MISSING)" && \
echo "질문셋:" && .venv/bin/python -c "
import json,glob,os
b=len(json.load(open('experiments/fugu-ko/embedding/sample_pages.json',encoding='utf-8')))
print(f'  표본 기준: {b}페이지')
for p in sorted(glob.glob('experiments/fugu-ko/embedding/cases_*.json')):
    n=len(json.load(open(p,encoding='utf-8')))
    print(f'  {os.path.basename(p)[6:-5]:8} {n:3}/{b}' + ('' if n==b else '  ← 부족: --resume 필요(부록 A)'))"
```

기대: `DB OK`, `keys OK`, `gpt`/`claude`가 `83/83`.
개수가 부족하면 부록 A로 채우거나 **그대로 진행해도 된다**(슬라이스 안에서는 동일 케이스로
비교하므로 편향 검사는 유효).

---

## STEP 1 — dry-run (전송 없음, 분량 확인)

```bash
cd <repo> && \
set -a && source ~/.orthus/nodes/company/node.env && set +a && \
.venv/bin/python experiments/fugu-ko/embedding/mirror_retrieve.py --dry-run
```

출력의 **"회사 위키 청크 N건 (약 M자)"** 가 STEP 3에서 실제로 외부로 나갈 전부다.
이 숫자가 납득되면 다음으로.

---

## STEP 2 — Solar 질문 생성 (선택)

3번째 생성기를 추가한다. **안 해도 gpt·claude 2종으로 편향 교차검증은 성립**하고,
"Solar 자기편향" 칸만 비게 된다.

```bash
cd <repo> && \
set -a && source ~/.orthus/nodes/company/node.env && set +a && \
.venv/bin/python experiments/fugu-ko/embedding/gen_questions.py --generator solar
```

- 전송: 페이지 83건의 제목+본문 일부 → `api.upstage.ai/v1/chat/completions` (solar-pro)
- 소요: 1~2분
- 출력 끝의 **`제목 누출: N/83`** 을 반드시 확인하라. 질문에 제목 단어가 남으면 lexical arm이
  문자열 매칭으로 공짜로 맞혀서 **그 슬라이스만 부풀려진다.**

  **실측 기준선(2026-07-16):** `gpt 6/83 (7%)`, `claude 2/83 (2.4%)`.
  Solar가 이 수준이면 정상. **20/83(24%) 이상이면** 지시를 못 따른 것이니 그 슬라이스는
  신뢰도를 낮게 보거나 재생성하라(부록 B).

---

## STEP 3 — 본 실행

```bash
cd <repo> && \
set -a && source ~/.orthus/nodes/company/node.env && set +a && \
.venv/bin/python experiments/fugu-ko/embedding/mirror_retrieve.py
```

- 전송: 청크 ~1,583건 + 질문 (83 × 생성기 수) → `api.upstage.ai/v1/embeddings`
- 소요: 수 분
- 벡터는 `*.npy`로 캐시 → 재실행 시 API 재호출 없음. 다시 임베딩하려면 `.npy` 삭제.
- 결과: 콘솔 교차표 + `mirror_results.json`

---

## STEP 4 — (선택) 노출/충실도 조절

| 목적 | 명령 꼬리에 추가 | 노출 |
|---|---|---|
| 노출 줄이기 | `--distractors 300` | ~380청크 (신뢰도↓) |
| 프로덕션 충실 | `--full-corpus` | **전 company 청크 ~10k** |
| 표집 바꿔 재현성 확인 | `--seed 999` | 동일 |

예:

```bash
cd <repo> && \
set -a && source ~/.orthus/nodes/company/node.env && set +a && \
.venv/bin/python experiments/fugu-ko/embedding/mirror_retrieve.py --full-corpus
```

> `--distractors 0`은 쓰지 마라. 정답만 pool에 남아 랭킹 과제가 성립하지 않는다.

---

## STEP 5 — 지연/처리량 측정 (회사 데이터 미사용)

품질이 무승부로 나왔으므로 **지연이 실제 결정 변수**다. 이건 합성 한국어로 재므로
**회사 지식이 외부로 나가지 않는다**(길이만 실제 워크로드에 맞춤).

```bash
cd <repo> && \
set -a && source ~/.orthus/nodes/company/node.env && set +a && \
echo "load 확인: $(cut -d' ' -f1-3 /proc/loadavg) / $(nproc)코어 — 높으면 측정값 오염!" && \
.venv/bin/python experiments/fugu-ko/embedding/latency.py
```

- **반드시 load가 낮을 때 재라**(8코어 기준 <2 권장). 실측은 load 0.87에서 했다.
- 소요: 2~3분. 표본 늘리려면 `--n-query 100`.
- 실측 결과(2026-07-16): 질의 p50 **OpenAI 241ms vs Solar 69ms**, 분포 완전 분리(p<0.0001).
  배치 재임베딩은 33,570청크 기준 5.5분 vs 6.2분으로 사실상 동등.

---

## STEP 5b — 원가/토크나이저 (회사 데이터 미사용, 실행 완료)

```bash
cd <repo>/.worktrees/embedding-swap && \
set -a && source ~/.orthus/nodes/company/node.env && set +a && \
<repo>/.venv/bin/python experiments/fugu-ko/embedding/cost.py
```

이미 돌렸다(README §7). 재현/단가 갱신 시에만 다시 돌리면 된다. 결론: **원가는 결정 변수가
아니다**(전량 재임베딩 $0.29, /ask 100만회 $4.14 — 배수는 5.5~18배지만 절대액이 무의미).

---

## STEP 6 — 후속: 비대칭 절제 + 차원 실험 (owner 실행, **재측정 필요**)

**두 질문에 답한다:** ①비대칭 배선을 할 값어치가 있나 ②4096 차원이 더 나은가.

> ### ⚠️ 1차 실행은 **설계 오류로 무력** → 하네스를 고쳤다. **재측정 필요.**
> 1차는 순위를 top-5까지만 보고 이진 hit@5로만 쟀다 — 6위든 40위든 "실패"로 뭉개서 불일치
> 쌍이 0~4로 붕괴했다(README §8.5). **수정: 관측 깊이 40위 + RR/Wilcoxon + 전 슬라이스.**
> 실질 표본이 **4 → 23(≈6배)**로 늘었다(스텁 실증).
> **아래를 돌리면 제대로 된 답이 나온다.**

```bash
cd <repo>/.worktrees/embedding-swap && \
set -a && source ~/.orthus/nodes/company/node.env && set +a && \
PYTHONPATH=experiments/fugu-ko/embedding \
  <repo>/.venv/bin/python experiments/fugu-ko/embedding/ablation.py
```

- **기본값이 전 슬라이스(249문항)로 바뀌었다.** gpt만 쓰면 포화로 변별력이 없다 —
  claude(0.867)/solar(0.855)가 헤드룸을 준다.
- ⚠️ **회사 데이터 전송**: 청크는 **캐시 재사용(전송 0)**, 질문만 249건 × 2arm × 2차원 신규.
  (`abl_pool_{1024,4096}.npy`가 이미 있다.)
- gpt 슬라이스만 재현하려면 `--generators gpt` → **캐시 100% 히트, Upstage 전송 0**
  (OpenAI 기준선 호출만 발생).
- 전송을 줄이려면 `--dims 1024`(절반).

**출력 읽는 법 — 순위 기반을 먼저 봐라:**

```text
  [순위 기반 — 민감]  RR=1/rank, 40위까지 관측, Wilcoxon
                       MRR비대  MRR대칭     차이   순위변화        p
  1024-d veconly        0.xxx   0.xxx   +0.xxx     23/249    0.xxx  검출못함

  [이진 hit@5 — 참고용, 정보 손실 큼]
                     비대칭만/대칭만  불일치쌍        p
  1024-d veconly         3 / 1          4    0.625  ← 불일치 부족, 판정 불가
```

| 판정 | 의미 |
|---|---|
| `유의` | p<0.05 — 진짜 차이 |
| `검출못함` | 순위는 바뀌었는데 방향이 일관되지 않음 = 차이 없다고 볼 근거 |
| `완전 동일(순위까지)` | 두 arm이 **같은 순위**를 낸다 → 배선/차원이 **무의미**함이 확정 |

- **`순위변화` 칸이 실질 표본이다.** 이게 20 이상이면 판정을 신뢰할 만하다.
- **이진 hit@5 줄은 참고용**이다 — `불일치 부족` 표시가 뜨면 그 줄로는 아무것도 판단할 수 없다.
  1차 실행이 이 줄만 보고 "차이 미미"라고 **과잉 판정**했다(README §8.5).

---

## 결과 읽는 법

```text
[veconly]  (veconly=임베딩 순수 영향, hybrid=사용자 체감)
  질문 작성     OpenAI hit@5   Solar hit@5     차이
  gpt                 0.xxx         0.xxx   +0.xxx
  claude              0.xxx         0.xxx   +0.xxx
  solar               0.xxx         0.xxx   +0.xxx
```

**판단 순서:**

1. **생성기마다 차이의 부호가 뒤집히면** → 생성기 편향. 결론 내지 마라.
2. **부호가 일정하면** → 진짜 신호. 그다음 크기를 본다.
3. **`solar_hybrid` ≥ `openai_hybrid`면 교체 가능.** veconly가 좀 져도 hybrid가 동등하면
   lexical arm이 받쳐준다는 뜻이라 실사용 리스크는 낮다.
4. **83문항 기준 ±몇 %p는 노이즈다.** 큰 차이만 신호로 읽어라.

**주의:** 이 평가셋은 "페이지 1개 = 정답 1개" 가정이라 **절대 수치는 비관적**이다
(관련된 다른 페이지가 상위에 와도 MISS). 모델 간 **상대 비교로만** 읽어라.

---

## 부록 A — Claude 질문 생성 / 실패분 채우기

`cases_claude.json`이 83개 미만이면(과부하 시 claude CLI 타임아웃으로 일부 실패한다 —
실측: load 27에서 18/83 실패) **빠진 것만** 채운다:

```bash
cd <repo> && \
set -a && source ~/.orthus/nodes/company/node.env && set +a && \
nohup .venv/bin/python experiments/fugu-ko/embedding/gen_questions.py --generator claude --resume \
  > /tmp/qgen_claude.log 2>&1 &
echo "PID $! — 진행: tail -f /tmp/qgen_claude.log"
```

개수 확인:

```bash
cd <repo> && .venv/bin/python -c "
import json,glob,os
for p in sorted(glob.glob('experiments/fugu-ko/embedding/cases_*.json')):
    print(f'  {os.path.basename(p):20} {len(json.load(open(p,encoding=\"utf-8\"))):3}개')"
```

- **호출당 ~30~60초**(로컬 claude CLI가 매번 프로세스를 띄움). 83건 전체면 1시간 가까이.
- **`--resume`은 다른 생성기에도 쓴다** — solar가 일부 실패해도 `--generator solar --resume`.
- 매 항목 저장되므로 중간에 죽어도 진행분은 남는다. 완료 확인: `pgrep -fc gen_questions.py` → `0`
- **load가 높으면 실패가 늘어난다.** `uptime`으로 확인하고 낮을 때 돌려라(8코어 기준 <8).
- 외부 전송은 Anthropic(이미 쓰는 경로)이고 Upstage로는 안 나간다.

> **생성기별 개수가 달라도 실험은 성립한다.** 하네스는 생성기 슬라이스 안에서 OpenAI vs Solar를
> **동일 케이스**로 비교하므로 편향 검사는 유효하다. 다만 83으로 맞추면 슬라이스 간 비교도 깔끔해진다.

## 부록 B — 초기화

```bash
cd <repo>/experiments/fugu-ko/embedding
rm -f *.npy solar_pool_keys.json mirror_results.json   # 벡터/결과만 (질문셋 유지)
# rm -f cases_solar.json                               # Solar 질문 다시 만들기
# rm -f sample_pages.json cases_*.json                 # 표본까지 전부 새로 (주의: gpt/claude 재생성 필요)
```

## 부록 C — 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| `cases_*.json 없음` | 질문 미생성 | 부록 A 또는 STEP 2 |
| `QueryCanceled: statement timeout` | **머신 과부하**(dev server/agent가 붙으면 load가 8코어 초과 → 4ms 인덱스 쿼리도 역할 기본 10s를 넘김). 실측: load 13~27 | **수정됨**(2026-07-16): 스크립트가 자체 연결에 `statement_timeout`을 60~120s로 올리고, 본문 읽기를 단일 벌크 쿼리로 앞에 몰았다. 재발 시 `uptime`으로 load 확인 후 무거운 프로세스(next dev server 등)를 끄고 재시도 |
| `KeyError: 'ORTHUS_PG_DSN_READONLY'` | env 미로드 | 각 블록의 `source ~/.orthus/nodes/company/node.env` 포함 확인 |
| Solar `404 page not found` | base_url 버전 | `/v1`이 맞다(`/v2`는 오기 — D0 기록) |
| Solar 질문 제목 누출 多 | 지시 미준수 | 그 슬라이스 신뢰도↓로 해석하거나 `cases_solar.json` 지우고 재생성 |
| 결과가 캐시처럼 안 바뀜 | `.npy` 재사용 | 부록 B로 삭제 후 재실행 |
