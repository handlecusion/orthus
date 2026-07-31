# B4 / X4 결과 — 임베딩 외부 코로보레이션

> 실행 2026-07-22 · 하네스 `experiments/fugu-ko/external/b4_x4_embedding.py` (standalone, ruff clean)
> 사전선언 SoR = `analysis/b4-prereg.md` §3 X4 · 데이터셋 선정 = `analysis/x0-external-dataset-plan.md` §3.1
> 원시 결과 JSON = `external/.cache/b4_x4_results.json` (gitignored) · 실행 로그 = `external/.cache/b4_x4_run.log`

## 0. 한 줄

내부 실험(자체 wiki 코퍼스)의 **"Solar `embedding-passage` > 현행 `text-embedding-3-small`"**
방향이, 저장소 밖 공개 한국어 검색 벤치마크 **2셋에서 2/2 같은 부호로 성립**한다. 주판정은
부호 일치이며 **PASS**. 절대 수치는 이전하지 않는다(도메인 상이, prereg §2·§5).

## 1. 설정

- **모델.** 현행 = `text-embedding-3-small` (OpenAI, `.env` `OPENAI_API_KEY`). 후보 = Solar
  `embedding-passage` (Upstage, `https://api.upstage.ai/v1`, `.env` `ORTHUS_LLM_SOLAR_API_KEY`).
  **차원 둘 다 1024** (prod `ORTHUS_EMBEDDING_DIMENSIONS=1024`, 내부 실험과 동일).
- **배선.** Solar는 내부 결론대로 **대칭**(query에도 `embedding-passage`)이 1차. 벤더 문서의
  **비대칭**(`embedding-query`)도 보조로 같이 측정했다(§4 — 외부에서 결과가 뒤집혔다).
- **지표.** MRR@10 · nDCG@10 (직접 계산, `mteb` 라이브러리 없이). 코사인 순위. 질의 지연 p50/p95.
- **`mteb` 라이브러리 부재.** 이 환경에 `mteb`/`datasets`/`pyarrow`가 없어, AutoRAGRetrieval은
  HF datasets-server rows API(stdlib)로 self-contained하게 받고 MRR/nDCG를 직접 계산했다. 공개
  리더보드 제출 규약을 따른 것이 아니다(prereg §5.4).

## 2. 데이터셋 — 잠근 3종 중 2종 실행 · 1종 차단

| key | HF id | license | 규모 | 방식 | 상태 |
|---|---|---|---|---|---|
| autorag | `mteb/AutoRAGRetrieval` @`43b8179…` | MIT | 114q / 720doc / 114qrel | **self-contained 표준 full-corpus 검색** | ✅ 실행 |
| miracl_ko | `miracl/miracl` ko dev | Apache-2.0 | 213q / pool 2,835doc | provided-candidate 재랭킹 (pos 547 + BM25 neg 2,510) | ✅ 실행 (⚠ full-corpus 아님) |
| mrtydi_ko | `castorini/mr-tydi` korean dev | Apache-2.0 | 303q | — | ⛔ **차단** |

**mrtydi 차단 사유(대체하지 않고 보고 — prereg §5·hard rule).** `castorini/mr-tydi`는 쿼리
행에 `positive_passages` **docid만** 담고 **본문은 별도 1.5M-passage 코퍼스**(`mr-tydi-corpus`)에만
있다. datasets-server rows API도, 로딩 스크립트(격리 환경 `datasets==3.6.0`)도 본문을 채우지
못했다(307 positive 전부 `text=""`, negative 0건). 전량 재랭킹하려면 1.5M passage 임베딩이
필요해 부호 확인에 비현실적이다. **다른 셋으로 갈아끼우지 않고 차단으로 보고한다.**

**miracl 방식 주의.** `miracl/miracl`은 script-based라 현대 `datasets`(v5)가 로딩을 거부한다.
격리 환경(`datasets==3.6.0`, trust_remote_code)에서 dev 쿼리의 **인라인 pos+neg 본문만** 덤프해
(`.cache/raw/miracl_ko_dev.jsonl`), 전 쿼리의 pos+neg 합집합(2,835 passage)을 pool로 재랭킹했다.
**표준 MIRACL full-corpus(~1.5M) 검색이 아니라 제공된 BM25 하드네거티브 재랭킹**이다 — 더 어려운
distractor라 볼 수도 있으나, autorag의 self-contained full-corpus 결과가 1차 근거이고 miracl은 보조다.

## 3. 결과 — MRR@10 / nDCG@10

| dataset | model | MRR@10 | nDCG@10 | vs 현행 (MRR) |
|---|---|---|---|---|
| **autorag** (full-corpus) | 현행 `text-embedding-3-small` | 0.6954 | 0.7539 | — |
| | **Solar `embedding-passage` (대칭)** | **0.7703** | **0.8182** | **+0.0749** |
| | Solar `embedding-query` (비대칭, 보조) | 0.8494 | 0.8841 | +0.1540 |
| **miracl_ko** (재랭킹 pool) | 현행 `text-embedding-3-small` | 0.7102 | 0.6880 | — |
| | **Solar `embedding-passage` (대칭)** | **0.7350** | **0.7290** | **+0.0248** |
| | Solar `embedding-query` (비대칭, 보조) | 0.8148 | 0.8052 | +0.1046 |

- **부호(대칭 Solar vs 현행): 2/2 모두 Solar 우세.** autorag +0.075 MRR / +0.064 nDCG,
  miracl +0.025 MRR / +0.041 nDCG.
- 내부 관측치(자체 wiki veconly **MRR +0.080**)와 autorag 델타(+0.075)가 **우연히도 근접**하나,
  **크기는 주장하지 않는다**(도메인 상이). 부호만 본다.

### 질의 지연 (표본 30, 교대 1건씩, 워밍업 제외)

| | p50 | p95 |
|---|---|---|
| 현행 (OpenAI, US 리전) | 291.5 ms | 1,007.6 ms |
| Solar (Upstage, KR 리전) | **125.2 ms** | **579.2 ms** |

Solar가 p50 기준 **약 2.3배 빠르다** — 내부의 "Solar가 훨씬 빠르다" 주장과 **같은 방향**.
⚠ 단 이 박스가 한국 리전에 가까워 US OpenAI에 지리적으로 불리하다(내부 `embedding/latency.py`도
동일 caveat). **배포 위치가 바뀌면 재측정 필요.** 부호(Solar가 빠름)는 리전 이점의 실제 운영 반영이다.

### 비용 (임베딩은 싸다)

429 재시도 0건(클린 런). 토큰/호출: autorag OpenAI 7콜/576k tok · Solar 10콜/325k tok;
miracl OpenAI 25콜/798k tok · Solar 35콜/436k tok (+ 비대칭 질의 + 지연 60콜). 총 OpenAI ~1.37M tok
+ Solar ~0.76M tok → **합계 $0.20 미만**. 회사 데이터는 전혀 쓰지 않았다(외부 공개셋만).

## 4. 주판정 — 부호 일치 (prereg §3 X4)

```
내부 방향: Solar embedding-passage > text-embedding-3-small  (자체 wiki, MRR +0.080 veconly)
외부 X4:   autorag   Solar>현행   (MRR 0.7703 vs 0.6954)
           miracl_ko Solar>현행   (MRR 0.7350 vs 0.7102)
           mrtydi_ko 차단(대체 안 함)
→ 외부 2셋 중 Solar 우세 2/2. 부호 일치: 예.
```

**판정: SIGN AGREEMENT (PASS).** X4 임베딩 교체 결론은 orthus 스냅샷에 국한되지 않는다 —
공개 한국어 검색셋에서도 같은 부호로 재현됐다. 이는 `docs/model-orchestration.md` §14 임베딩
슬롯 교체 결정에 **외적 타당성**을 붙인다.

## 5. 짚어둘 것 (정직하게)

1. **⚠ 비대칭 배선 부호가 외부에서 뒤집혔다.** 내부 LAB-NOTES/`embedding/README.md`는 Solar
   `embedding-query`/`passage` 비대칭이 대칭보다 **나쁘다**고 관측했으나, 외부 2셋 모두에서
   비대칭이 대칭을 **크게 이겼다**(autorag 0.849 vs 0.770, miracl 0.815 vs 0.735).
   - 이것은 **1차 판정을 바꾸지 않는다** — 1차는 내부가 채택한 대칭 배선끼리의 비교이고 그건
     PASS다. 하지만 "대칭이 항상 낫다"는 **내부 결론은 도메인 한정**일 수 있음을 시사한다.
     회사 wiki(짧은 질의 ↔ 긴 청크, register 동질)와 외부 QA(자연어 질문 ↔ 위키 문단)에서
     query/passage 프롬프트 분리의 효과가 갈렸을 가능성. 프로덕션 전환 시 회사 코퍼스에서
     비대칭을 **재확인**할 가치가 있다(내부 실험 재현 대상). 부호가 도메인 간 갈린 사실을 그대로 보고한다.
2. **오염(contamination).** autorag/miracl 모두 공개 MTEB/BeIR 계열이라 두 벤더 모두 학습에서
   봤을 수 있다. **단 bi-encoder 검색의 오염은 grounded QA의 closed-book 통제와 성격이 다르다** —
   정답 문단을 본 적 있어도 그 이점은 두 모델에 **대칭적으로** 작용하므로 상대 부호를 크게
   왜곡하지 않는다. prereg §4의 closed-book 제외는 생성형 QA용 통제라 임베딩 검색엔 직접
   적용되지 않는다. 그래서 **절대 수치를 이전하지 않고 부호만** 본다.
3. **miracl은 full-corpus가 아니다**(§2). provided-candidate 재랭킹이라 절대 MRR을 공개
   MIRACL-ko 리더보드와 비교하면 안 된다. autorag만이 표준 full-corpus 결과다.
4. **1셋 차단(mrtydi).** 잠근 3셋 중 2셋만 실행됐다. 부호는 2/2로 일관되나 표본 데이터셋이
   1개 줄었다는 사실을 병기한다.
5. **표본 크기.** autorag 114q, miracl 213q. 부호는 안정적이나 통계적 유의성 검정은 하지 않았다
   (prereg X4는 부호 일치만 요구). 절대 델타의 신뢰구간은 주장하지 않는다.

## 6. 재현

```bash
# 데이터: autorag는 하네스가 datasets-server rows API로 자동 캐시(.cache/raw/, gitignored).
#         miracl/mrtydi는 script-based라 격리 환경에서 1회 덤프(재배포 금지 아님, Apache-2.0):
uv run --no-project --with 'datasets==3.6.0' --with pyarrow \
  python <scratch>/dump_beir.py   # -> .cache/raw/{miracl_ko,mrtydi_ko}_dev.jsonl

# 벤치마크 (repo .env의 OPENAI_API_KEY + ORTHUS_LLM_SOLAR_API_KEY 사용):
python experiments/fugu-ko/external/b4_x4_embedding.py --dry-run   # 오프라인 검증
python experiments/fugu-ko/external/b4_x4_embedding.py             # 실측
```

**운영 함정 2개(하네스에 반영됨).** (i) 이 박스에서 httpx가 간헐적으로 IPv6 경로로 붙어
`api.openai.com` read가 무한 대기했다(curl은 happy-eyeballs로 IPv4 폴백) → `force_ipv4()`로
DNS를 IPv4로 강제. (ii) 부모 셸에 소진된 `OPENAI_API_KEY`가 남아 .env의 유효 키를 가렸다 →
`load_env()`가 비어 있지 않은 .env 값으로 override하게 수정. 데이터/결과는 `.cache/`(gitignored)에만
남고 커밋하지 않는다.
