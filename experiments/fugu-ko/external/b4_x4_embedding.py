"""B4 / X4 — 임베딩 외부 코로보레이션 (`analysis/b4-prereg.md` §3 X4).

**재는 것 하나:** 내부 실험(자체 wiki 코퍼스)이 낸 "Solar `embedding-passage` > 현행
`text-embedding-3-small`"라는 **방향**이, 저장소 밖 공개 한국어 검색 벤치마크에서도
같은 부호로 성립하는가. 주판정은 **부호 일치(sign agreement)**뿐이다 — 도메인이 달라
절대 수치는 이전되지 않는다(prereg §2·§5).

데이터셋 (prereg §3 X4가 잠근 것 — 대체 금지):
  autorag   mteb/AutoRAGRetrieval (MIT)            self-contained BeIR: 114q / 720doc / 114qrel
  miracl    miracl/miracl ko (Apache-2.0)          provided-candidate pool (pos + BM25 neg 인라인)
  mrtydi    castorini/mr-tydi korean (Apache-2.0)  ⚠ 본문이 별도 1.5M 코퍼스에만 있음 → 차단 보고

**대체 금지 (prereg §5·hard rule).** 게이팅·부재·스키마 불일치는 조용히 다른 셋으로 갈지
않고 보고한다. autorag는 datasets-server rows API(stdlib)로 self-contained하게 받는다.
miracl/mrtydi는 script-based(datasets v5가 로딩 스크립트를 거부)라 격리 환경에서 인라인
후보 본문만 별도 덤프한다(`.cache/raw/*_dev.jsonl`) — **full-corpus MIRACL/Mr.TyDi가 아니라
제공된 후보(pos + neg) 재랭킹**임을 표에 명시한다.

모델:
  현행   text-embedding-3-small  (OpenAI, base=ORTHUS_EMBEDDING_BASE_URL / OPENAI_API_KEY)
  후보   embedding-passage       (Upstage Solar, base=https://api.upstage.ai/v1)
차원은 둘 다 1024(prod ORTHUS_EMBEDDING_DIMENSIONS=1024, 내부 실험과 동일). Solar는 내부
결론대로 **대칭**(query에도 embedding-passage) 배선을 1차로 쓰고, 벤더 문서의 비대칭
(embedding-query) 배선을 보조로 같이 측정해 "비대칭이 더 나쁘다"는 내부 관측을 외부에서 확인한다.

지표: MRR@10 · nDCG@10 (직접 계산 — mteb 라이브러리 불필요). 질의 지연 p50/p95도 잰다.

의존성: numpy + httpx (stdlib로 데이터 fetch). 회사 데이터를 전혀 쓰지 않는다.

실행:
    # 오프라인 검증 (API 키·네트워크 불필요)
    python experiments/fugu-ko/external/b4_x4_embedding.py --dry-run
    # 실측 (repo .env 자동 로드: OPENAI_API_KEY + ORTHUS_LLM_SOLAR_API_KEY)
    python experiments/fugu-ko/external/b4_x4_embedding.py
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import statistics as st
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np


def force_ipv4() -> None:
    """이 박스에서 api.openai.com의 IPv6 경로가 간헐적으로 무한 대기한다(curl은
    happy-eyeballs로 IPv4 폴백해 정상, httpx는 폴백이 없어 read timeout×재시도로 기어간다).
    프로세스 전역에서 DNS를 IPv4로만 해소해 그 함정을 원천 차단한다. Solar에도 무해하다."""
    _orig = socket.getaddrinfo

    def _ipv4_only(host, port, family=0, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return _orig(host, port, socket.AF_INET, *args, **kwargs)

    socket.getaddrinfo = _ipv4_only

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
RAW = CACHE / "raw"
OUT = CACHE / "b4_x4_results.json"
ENV_FILE = HERE.parent.parent.parent / ".env"  # repo root .env

DIMS = 1024
K = 10  # MRR@10 / nDCG@10
HF_ROWS = "https://datasets-server.huggingface.co/rows"
OPENAI_MODEL = "text-embedding-3-small"
SOLAR_URL = "https://api.upstage.ai/v1/embeddings"
SOLAR_PASSAGE = "embedding-passage"
SOLAR_QUERY = "embedding-query"

# prereg가 잠근 앵커. rows API가 refs/convert/parquet에서 서빙하므로 main sha를 기록만 한다.
AUTORAG_SHA = "43b817937708cb10ba519f86edb4f6885a1631a4"


# --------------------------------------------------------------------------- env


def load_env() -> None:
    """repo .env를 읽어 os.environ에 주입한다. 키 값은 절대 출력하지 않는다.

    **비어 있지 않은 .env 값은 부모 셸 값을 덮어쓴다.** repo .env가 이 실험의 canonical
    설정이기 때문이다 — 부모 셸에 남은 stale `OPENAI_API_KEY`(예: 소진된 개인 키)가
    .env의 유효 키를 가리는 함정을 막는다(실측으로 겪음)."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val:  # 비어 있지 않은 값만 override(빈 .env 값이 실 값을 지우지 않게)
            os.environ[key] = val


def openai_key() -> str:
    k = os.environ.get("ORTHUS_EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not k:
        raise SystemExit("OpenAI 키 없음: ORTHUS_EMBEDDING_API_KEY 또는 OPENAI_API_KEY 필요")
    return k


def solar_key() -> str:
    k = os.environ.get("ORTHUS_EMBEDDING_SOLAR_API_KEY") or os.environ.get(
        "ORTHUS_LLM_SOLAR_API_KEY", ""
    )
    if not k:
        raise SystemExit("Solar 키 없음: ORTHUS_EMBEDDING_SOLAR_API_KEY 또는 ORTHUS_LLM_SOLAR_API_KEY 필요")
    return k


def openai_base() -> str:
    return os.environ.get("ORTHUS_EMBEDDING_BASE_URL", "https://api.openai.com/v1").rstrip("/")


# ------------------------------------------------------------------------ fetch


def _http_get_json(url: str, params: dict[str, object]) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(6):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "orthus-b4-x4/1.0"})
            with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (401, 403):
                raise SystemExit(
                    f"{url} → HTTP {exc.code}. 게이트/권한 문제. prereg §5대로 대체하지 말고 보고하라."
                ) from exc
            wait = (6.0 if exc.code == 429 else 1.5) * (2**attempt)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            time.sleep(1.5 * (2**attempt))
    raise RuntimeError(f"{url} fetch 실패: {last!r}")


def fetch_rows(dataset: str, config: str, split: str) -> list[dict]:
    """HF datasets-server rows API 전량 페이지네이션(stdlib). 429는 백오프. 디스크 캐시."""
    slug = dataset.replace("/", "__")
    cache = RAW / f"{slug}__{config}__{split}.jsonl"
    if cache.exists():
        return [json.loads(line) for line in cache.read_text(encoding="utf-8").splitlines() if line]
    out: list[dict] = []
    offset = 0
    while True:
        payload = _http_get_json(
            HF_ROWS,
            {"dataset": dataset, "config": config, "split": split, "offset": offset, "length": 100},
        )
        if "error" in payload:
            raise SystemExit(
                f"{dataset} rows API 오류: {payload['error']!r} (config={config} split={split}). "
                "prereg가 잠근 config/split이다 — 보고하라."
            )
        batch = payload.get("rows", [])
        if not batch:
            break
        out.extend(r["row"] for r in batch)
        offset += len(batch)
        if len(batch) < 100 or offset >= int(payload.get("num_rows_total", offset)):
            break
    RAW.mkdir(parents=True, exist_ok=True)
    cache.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n", encoding="utf-8")
    return out


# ------------------------------------------------------------------- embedding


@dataclass
class EmbedStat:
    calls: int = 0
    texts: int = 0
    chars: int = 0
    tokens: int = 0
    retries_429: int = 0
    query_latencies_ms: list[float] = field(default_factory=list)


def _post_with_backoff(
    client: httpx.Client, url: str, headers: dict, body: dict, stat: EmbedStat
) -> tuple[dict, float]:
    last: str = "no-attempt"
    for attempt in range(6):
        try:
            t0 = time.perf_counter()
            r = client.post(url, headers=headers, json=body, timeout=httpx.Timeout(30.0, connect=10.0))
            dt = (time.perf_counter() - t0) * 1000
            if r.status_code == 429:
                stat.retries_429 += 1
                last = f"HTTP 429 (attempt {attempt})"
                # Retry-After가 있으면 그대로, 없으면 완만한 백오프(상한 20s — 예전 192s는 hang처럼 보였다).
                ra = r.headers.get("retry-after")
                time.sleep(min(float(ra), 20.0) if ra and ra.isdigit() else min(2.0 * (2**attempt), 20.0))
                continue
            r.raise_for_status()
            return r.json(), dt
        except httpx.HTTPStatusError as exc:
            last = f"HTTP {exc.response.status_code if exc.response is not None else '?'}: {exc.response.text[:120] if exc.response is not None else ''}"
            if exc.response is not None and exc.response.status_code == 429:
                stat.retries_429 += 1
                time.sleep(min(2.0 * (2**attempt), 20.0))
                continue
            time.sleep(1.5 * (2**attempt))
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
            time.sleep(1.5 * (2**attempt))
    raise RuntimeError(f"embed POST 실패 (마지막: {last}, batch={len(body.get('input', []))})")


def embed_openai(
    client: httpx.Client, texts: list[str], key: str, stat: EmbedStat, *, batch: int = 128
) -> np.ndarray:
    url = f"{openai_base()}/embeddings"
    headers = {"Authorization": f"Bearer {key}"}
    vecs: list = [None] * len(texts)
    for i in range(0, len(texts), batch):
        chunk = texts[i : i + batch]
        body = {"model": OPENAI_MODEL, "input": chunk, "dimensions": DIMS}
        data, dt = _post_with_backoff(client, url, headers, body, stat)
        for d in data["data"]:
            vecs[i + d["index"]] = d["embedding"]
        stat.calls += 1
        stat.texts += len(chunk)
        stat.chars += sum(len(t) for t in chunk)
        stat.tokens += int(data.get("usage", {}).get("total_tokens", 0))
        if len(chunk) == 1:
            stat.query_latencies_ms.append(dt)
    return np.asarray(vecs, dtype=np.float32)


def embed_solar(
    client: httpx.Client, texts: list[str], key: str, model: str, stat: EmbedStat, *, batch: int = 90
) -> np.ndarray:
    headers = {"Authorization": f"Bearer {key}"}
    vecs: list = [None] * len(texts)
    for i in range(0, len(texts), batch):
        chunk = texts[i : i + batch]
        body = {"model": model, "input": chunk, "dimensions": DIMS}
        data, dt = _post_with_backoff(client, SOLAR_URL, headers, body, stat)
        for d in data["data"]:
            vecs[i + d["index"]] = d["embedding"]
        stat.calls += 1
        stat.texts += len(chunk)
        stat.chars += sum(len(t) for t in chunk)
        stat.tokens += int(data.get("usage", {}).get("total_tokens", 0))
        if len(chunk) == 1:
            stat.query_latencies_ms.append(dt)
        time.sleep(0.1)
    return np.asarray(vecs, dtype=np.float32)


# --------------------------------------------------------------------- scoring


def cosine_rank(qvecs: np.ndarray, dvecs: np.ndarray) -> np.ndarray:
    """각 질의에 대해 문서 인덱스를 코사인 내림차순 정렬한 배열(n_q, n_d)."""
    qn = qvecs / (np.linalg.norm(qvecs, axis=1, keepdims=True) + 1e-9)
    dn = dvecs / (np.linalg.norm(dvecs, axis=1, keepdims=True) + 1e-9)
    sims = qn @ dn.T
    return np.argsort(-sims, axis=1)


def mrr_ndcg_at_k(
    order: np.ndarray, rel_by_q: list[set[int]], k: int = K
) -> tuple[float, float, list[int | None]]:
    """단일-관련 또는 다중-관련 qrel 모두 지원. 문서 인덱스 집합으로 관련 표기."""
    mrrs: list[float] = []
    ndcgs: list[float] = []
    ranks: list[int | None] = []
    for qi, rel in enumerate(rel_by_q):
        if not rel:
            ranks.append(None)
            continue
        topk = order[qi, :k]
        # DCG: 관련 문서마다 1/log2(rank+1)
        dcg = 0.0
        first: int | None = None
        for pos, di in enumerate(topk, start=1):
            if di in rel:
                dcg += 1.0 / math.log2(pos + 1)
                if first is None:
                    first = pos
        ideal = sum(1.0 / math.log2(p + 1) for p in range(1, min(len(rel), k) + 1))
        ndcgs.append(dcg / ideal if ideal else 0.0)
        mrrs.append(1.0 / first if first else 0.0)
        ranks.append(first)
    return (
        round(sum(mrrs) / len(mrrs), 4) if mrrs else float("nan"),
        round(sum(ndcgs) / len(ndcgs), 4) if ndcgs else float("nan"),
        ranks,
    )


# ------------------------------------------------------------------- datasets


@dataclass
class RetrievalSet:
    key: str
    queries: list[str]
    docs: list[str]
    rel_by_q: list[set[int]]  # query index -> set of relevant doc indices
    note: str
    source: str


def load_autorag() -> RetrievalSet:
    q = fetch_rows("mteb/AutoRAGRetrieval", "queries", "test")
    c = fetch_rows("mteb/AutoRAGRetrieval", "corpus", "test")
    qr = fetch_rows("mteb/AutoRAGRetrieval", "qrels", "test")
    qid_order = [r["_id"] for r in q]
    qid_idx = {qid: i for i, qid in enumerate(qid_order)}
    cid_idx = {r["_id"]: i for i, r in enumerate(c)}
    rel: list[set[int]] = [set() for _ in q]
    dropped = 0
    for r in qr:
        if int(r.get("score", 0)) <= 0:
            continue
        qi = qid_idx.get(r["query-id"])
        di = cid_idx.get(r["corpus-id"])
        if qi is None or di is None:
            dropped += 1
            continue
        rel[qi].add(di)
    docs = [(r.get("title", "") + "\n" + r["text"]).strip() for r in c]
    if dropped:
        print(f"  [autorag] qrel {dropped}건이 id 해소 실패로 탈락")
    return RetrievalSet(
        key="autorag",
        queries=[r["text"] for r in q],
        docs=docs,
        rel_by_q=rel,
        note="self-contained BeIR (queries+corpus+qrels 동봉). 표준 full-corpus 검색.",
        source=f"mteb/AutoRAGRetrieval@{AUTORAG_SHA[:12]} (MIT)",
    )


def load_pool_dump(key: str, hf: str, lic: str) -> RetrievalSet | None:
    """script-based 셋의 인라인 후보 덤프(.cache/raw/{key}_dev.jsonl)로 pool 재랭킹 셋 구성.

    ⚠ full-corpus 아님. pool = 전 질의의 pos+neg 본문 합집합(docid dedup). 본문이 비면 None.
    """
    path = RAW / f"{key}_dev.jsonl"
    if not path.exists():
        return None
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    doc_by_id: dict[str, str] = {}
    empty = 0
    for r in rows:
        for p in r["positive_passages"] + r["negative_passages"]:
            txt = (p.get("text") or "").strip()
            if not txt:
                empty += 1
                continue
            doc_by_id.setdefault(p["docid"], txt)
    if not doc_by_id:
        print(f"  [{key}] 인라인 본문이 전부 비었다(별도 코퍼스 필요) → 차단 보고. (빈 후보 {empty})")
        return None
    did_idx = {did: i for i, did in enumerate(doc_by_id)}
    docs = list(doc_by_id.values())
    queries: list[str] = []
    rel: list[set[int]] = []
    for r in rows:
        pos = {did_idx[p["docid"]] for p in r["positive_passages"] if p["docid"] in did_idx}
        if not pos:
            continue
        queries.append(r["query"])
        rel.append(pos)
    neg_total = sum(len(r["negative_passages"]) for r in rows)
    return RetrievalSet(
        key=key,
        queries=queries,
        docs=docs,
        rel_by_q=rel,
        note=(
            f"provided-candidate 재랭킹 (pool={len(docs)}doc = pos+neg 합집합, "
            f"neg={neg_total}). ⚠ full-corpus 아님."
        ),
        source=f"{hf} dev ({lic})",
    )


# ---------------------------------------------------------------------- report


def pct(a: list[float], p: float) -> float:
    if not a:
        return float("nan")
    s = sorted(a)
    return s[min(int(p / 100 * len(s)), len(s) - 1)]


def run_set(
    rs: RetrievalSet, client: httpx.Client, okey: str, skey: str
) -> dict:
    """검색 품질만. 지연은 별도 소량 표본(measure_latency)에서 잰다 — 질의를 1건씩
    임베딩하면 호출 수가 수백~천 건이 되어 OpenAI RPM 한도에 걸린다(429). 여기선 전부 배치."""
    print(f"\n=== [{rs.key}] {rs.source} ===")
    print(f"    {rs.note}")
    print(f"    queries={len(rs.queries)} docs={len(rs.docs)}", flush=True)
    n_rel = sum(1 for r in rs.rel_by_q if r)
    if n_rel != len(rs.queries):
        print(f"    관련문서 있는 질의 {n_rel}/{len(rs.queries)}")

    variants: dict[str, dict] = {}

    # OpenAI (현행) — 문서+질의 모두 배치
    o_stat = EmbedStat()
    print("    OpenAI 임베딩(문서+질의 배치)...", flush=True)
    o_docs = embed_openai(client, rs.docs, okey, o_stat)
    o_q = embed_openai(client, rs.queries, okey, o_stat)
    mrr, ndcg, _ = mrr_ndcg_at_k(cosine_rank(o_q, o_docs), rs.rel_by_q)
    variants["openai_3small"] = {
        "model": OPENAI_MODEL,
        "mrr@10": mrr,
        "ndcg@10": ndcg,
        "embed": vars(o_stat) | {"query_latencies_ms": None},
    }
    print(f"      MRR@10={mrr}  nDCG@10={ndcg}", flush=True)

    # Solar 대칭(embedding-passage 양쪽) — 내부 결론 배선
    s_stat = EmbedStat()
    print("    Solar 임베딩(embedding-passage 대칭, 문서+질의 배치)...", flush=True)
    s_docs = embed_solar(client, rs.docs, skey, SOLAR_PASSAGE, s_stat)
    s_q = embed_solar(client, rs.queries, skey, SOLAR_PASSAGE, s_stat)
    mrr, ndcg, _ = mrr_ndcg_at_k(cosine_rank(s_q, s_docs), rs.rel_by_q)
    variants["solar_passage_symmetric"] = {
        "model": f"{SOLAR_PASSAGE} (query+doc 대칭)",
        "mrr@10": mrr,
        "ndcg@10": ndcg,
        "embed": vars(s_stat) | {"query_latencies_ms": None},
    }
    print(f"      MRR@10={mrr}  nDCG@10={ndcg}", flush=True)

    # Solar 비대칭(embedding-query for queries) — 벤더 문서 배선, 보조 확인
    sa_stat = EmbedStat()
    print("    Solar 질의 임베딩(embedding-query=비대칭 보조, 배치)...", flush=True)
    sa_q = embed_solar(client, rs.queries, skey, SOLAR_QUERY, sa_stat)
    mrr_a, ndcg_a, _ = mrr_ndcg_at_k(cosine_rank(sa_q, s_docs), rs.rel_by_q)
    variants["solar_asymmetric_query"] = {
        "model": f"{SOLAR_QUERY} query / {SOLAR_PASSAGE} doc",
        "mrr@10": mrr_a,
        "ndcg@10": ndcg_a,
        "note": "벤더 문서 배선. 내부 LAB-NOTES는 비대칭이 더 나쁘다고 관측 — 외부 확인용.",
    }
    print(f"      (비대칭) MRR@10={mrr_a}  nDCG@10={ndcg_a}", flush=True)

    # 부호
    o = variants["openai_3small"]
    s = variants["solar_passage_symmetric"]
    sign = "Solar>현행" if s["mrr@10"] > o["mrr@10"] else ("현행>Solar" if s["mrr@10"] < o["mrr@10"] else "동률")
    print(f"    부호(MRR@10): {sign}  (Solar {s['mrr@10']} vs 현행 {o['mrr@10']})", flush=True)
    return {
        "source": rs.source,
        "note": rs.note,
        "n_queries": len(rs.queries),
        "n_docs": len(rs.docs),
        "variants": variants,
        "sign_mrr": sign,
    }


def measure_latency(
    client: httpx.Client, queries: list[str], okey: str, skey: str, n: int = 30
) -> dict:
    """질의 지연 p50/p95 — 실제 한국어 질의 표본을 1건씩, 교대 실행(네트워크 변동 균등화).
    워밍업 제외. `/ask` hot path = 질의 1건 임베딩이므로 이게 사용자 체감이다."""
    sample = queries[:n]
    print(f"\n=== 질의 지연 (표본 {len(sample)}, 교대 1건씩) ===", flush=True)
    o_ms: list[float] = []
    s_ms: list[float] = []
    for q in sample:
        try:
            ost = EmbedStat()
            embed_openai(client, [q], okey, ost)
            t0 = time.perf_counter()
            r = client.post(
                f"{openai_base()}/embeddings",
                headers={"Authorization": f"Bearer {okey}"},
                json={"model": OPENAI_MODEL, "input": [q], "dimensions": DIMS},
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
            r.raise_for_status()
            o_ms.append((time.perf_counter() - t0) * 1000)
        except Exception:  # noqa: BLE001
            pass
        try:
            t0 = time.perf_counter()
            r = client.post(
                SOLAR_URL,
                headers={"Authorization": f"Bearer {skey}"},
                json={"model": SOLAR_PASSAGE, "input": [q], "dimensions": DIMS},
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
            r.raise_for_status()
            s_ms.append((time.perf_counter() - t0) * 1000)
        except Exception:  # noqa: BLE001
            pass
    out = {
        "n_openai": len(o_ms),
        "n_solar": len(s_ms),
        "openai_p50_ms": round(st.median(o_ms), 1) if o_ms else None,
        "openai_p95_ms": round(pct(o_ms, 95), 1) if o_ms else None,
        "solar_p50_ms": round(st.median(s_ms), 1) if s_ms else None,
        "solar_p95_ms": round(pct(s_ms, 95), 1) if s_ms else None,
    }
    print(
        f"    OpenAI p50={out['openai_p50_ms']}ms p95={out['openai_p95_ms']}ms  |  "
        f"Solar p50={out['solar_p50_ms']}ms p95={out['solar_p95_ms']}ms",
        flush=True,
    )
    return out


def dry_run() -> None:
    print("[DRY RUN] API 전송 없음. env·데이터 로딩 경로만 검증.\n")
    load_env()
    print(f"  OpenAI base : {openai_base()}  model={OPENAI_MODEL} dims={DIMS}")
    print(f"  OpenAI key  : {'SET' if (os.environ.get('ORTHUS_EMBEDDING_API_KEY') or os.environ.get('OPENAI_API_KEY')) else 'MISSING'}")
    print(f"  Solar url   : {SOLAR_URL}  model={SOLAR_PASSAGE}/{SOLAR_QUERY} dims={DIMS}")
    print(f"  Solar key   : {'SET' if (os.environ.get('ORTHUS_EMBEDDING_SOLAR_API_KEY') or os.environ.get('ORTHUS_LLM_SOLAR_API_KEY')) else 'MISSING'}")
    print("\n  데이터셋:")
    try:
        rs = load_autorag()
        est_docs = sum(len(d) for d in rs.docs)
        print(f"    autorag  q={len(rs.queries)} docs={len(rs.docs)} (~{est_docs:,}자) — self-contained ✓")
    except Exception as e:  # noqa: BLE001
        print(f"    autorag  로딩 실패: {type(e).__name__}: {str(e)[:120]}")
    for key, hf, lic in [
        ("miracl_ko", "miracl/miracl ko", "Apache-2.0"),
        ("mrtydi_ko", "castorini/mr-tydi korean", "Apache-2.0"),
    ]:
        rs2 = load_pool_dump(key, hf, lic)
        if rs2:
            print(f"    {key}  q={len(rs2.queries)} pool={len(rs2.docs)} — provided-candidate ✓")
        else:
            print(f"    {key}  덤프 없음/본문 빈 값 → 차단 보고 (prereg §5)")
    print("\n파이프라인 정상. 실제 실행은 --dry-run 제거.")


def main() -> None:
    ap = argparse.ArgumentParser(description="B4/X4 임베딩 외부 코로보레이션")
    ap.add_argument("--dry-run", action="store_true", help="API 전송 없이 env/데이터 로딩만 검증")
    ap.add_argument(
        "--only",
        choices=["autorag", "miracl_ko", "mrtydi_ko"],
        help="한 셋만 실행",
    )
    args = ap.parse_args()

    if args.dry_run:
        dry_run()
        return

    force_ipv4()
    load_env()
    okey, skey = openai_key(), solar_key()

    sets: list[RetrievalSet] = []
    blocked: list[dict] = []
    want = [args.only] if args.only else ["autorag", "miracl_ko", "mrtydi_ko"]

    if "autorag" in want:
        sets.append(load_autorag())
    for key, hf, lic in [
        ("miracl_ko", "miracl/miracl ko", "Apache-2.0"),
        ("mrtydi_ko", "castorini/mr-tydi korean", "Apache-2.0"),
    ]:
        if key not in want:
            continue
        rs = load_pool_dump(key, hf, lic)
        if rs is None:
            reason = (
                "덤프 부재 (격리 환경 로딩 필요)"
                if not (RAW / f"{key}_dev.jsonl").exists()
                else "인라인 후보 본문이 비어 별도 full-corpus 필요"
            )
            blocked.append({"key": key, "source": f"{hf} ({lic})", "reason": reason})
            print(f"  [{key}] 차단: {reason}")
        else:
            sets.append(rs)

    results: dict[str, dict] = {}
    # 이 박스에서 httpx 기본 경로가 간헐적으로 IPv6로 붙어 read가 무한 대기한다(curl은
    # happy-eyeballs로 IPv4 폴백해 정상). local_address로 IPv4 바인딩을 강제해 그 함정을 막는다.
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=1)
    with httpx.Client(transport=transport) as client:
        # 워밍업(콜드스타트 제외) — 지연 측정 공정성
        try:
            embed_openai(client, ["워밍업"], okey, EmbedStat())
            embed_solar(client, ["워밍업"], skey, SOLAR_PASSAGE, EmbedStat())
        except Exception as e:  # noqa: BLE001
            print(f"워밍업 경고: {type(e).__name__}: {str(e)[:80]}")
        for rs in sets:
            results[rs.key] = run_set(rs, client, okey, skey)
        latency = (
            measure_latency(client, sets[0].queries, okey, skey) if sets else {}
        )

    # 부호 일치 종합
    signs = {k: v["sign_mrr"] for k, v in results.items()}
    solar_wins = sum(1 for s in signs.values() if s == "Solar>현행")
    verdict = {
        "internal_direction": "Solar embedding-passage > text-embedding-3-small (MRR +0.080, veconly)",
        "external_signs": signs,
        "solar_wins": solar_wins,
        "n_sets": len(signs),
        "sign_agreement": solar_wins == len(signs) and len(signs) > 0,
        "blocked": blocked,
    }

    payload = {
        "dims": DIMS,
        "k": K,
        "results": results,
        "query_latency": latency,
        "verdict": verdict,
        "contamination_note": (
            "전 셋이 공개(MTEB/BeIR 계열)라 두 벤더 모두 학습에서 봤을 수 있다. bi-encoder "
            "검색에서 오염은 grounded QA의 closed-book 통제와 성격이 다르다(정답 텍스트를 "
            "본 적 있어도 두 모델에 대칭적으로 유리하다). 절대 수치는 이전 안 하고 부호만 본다."
        ),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 74)
    print("주판정 — 부호 일치 (prereg §3 X4)")
    print(f"  내부 방향: {verdict['internal_direction']}")
    for k, s in signs.items():
        v = results[k]["variants"]
        print(
            f"  [{k:9}] {s:10}  Solar {v['solar_passage_symmetric']['mrr@10']} "
            f"vs 현행 {v['openai_3small']['mrr@10']}  (nDCG "
            f"{v['solar_passage_symmetric']['ndcg@10']} vs {v['openai_3small']['ndcg@10']})"
        )
    for b in blocked:
        print(f"  [{b['key']:9}] 차단 — {b['reason']}")
    if latency:
        print(
            f"  질의 지연: OpenAI p50={latency.get('openai_p50_ms')}ms / Solar "
            f"p50={latency.get('solar_p50_ms')}ms"
        )
    print(
        f"\n  → 외부 {len(signs)}셋 중 Solar 우세 {solar_wins}. "
        f"부호 일치: {'예 (내부 결론 외적 타당)' if verdict['sign_agreement'] else '부분/불일치 → 해당 항목 스냅샷 한정 강등'}"
    )
    print(f"\n상세: {OUT}")


if __name__ == "__main__":
    main()
