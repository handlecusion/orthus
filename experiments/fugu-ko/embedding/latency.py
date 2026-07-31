"""임베딩 지연/처리량 측정: OpenAI(text-embedding-3-small) vs Solar.

**회사 데이터를 쓰지 않는다.** 지연은 내용 의미가 아니라 토큰 수와 왕복 시간에 달렸으므로,
실제 워크로드의 **길이 분포만 맞춘 합성 한국어**로 잰다(실측: 청크 p50=210자, 질문 p50=34자).
따라서 이 스크립트는 외부로 회사 지식을 내보내지 않는다.

측정 두 가지 — 성격이 완전히 다르다:
  1. **질의 지연**(hot path): `/ask`마다 1건씩 임베딩한다. 사용자 체감에 직결.
  2. **배치 처리량**(1회성): 교체 시 33,570 청크 재임베딩에 걸리는 시간.

공정성:
  - **교대 실행**(interleave) — 몰아서 재면 네트워크 변동이 한쪽에 몰린다.
  - 워밍업 후 측정(콜드스타트 제외).
  - 동일 텍스트·동일 차원(1024)·동일 클라이언트(httpx).

주의: OpenAI는 미국, Upstage는 한국 리전이다. 이 박스 위치에 따라 지리적 이점이 결과를
지배할 수 있다 — 그건 실제 운영 이점이지만, 다른 위치에 배포하면 재측정해야 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS_PATH = os.environ.get(
    "FUGU_KEYS", "<로컬 키 저장소>/keys.json"
)
SOLAR_URL = "https://api.upstage.ai/v1/embeddings"
DIMS = 1024

# 실측 워크로드 길이 (orthus_company wiki_chunks / 평가셋 질문)
QUERY_CHARS = 34
CHUNK_CHARS = 210
TOTAL_CHUNKS = 33_570  # company 10,153 + personal 23,417

# 합성 한국어 — 일반 업무 문장(회사 지식 아님). 길이를 맞추려고 반복/절단한다.
_FILLER = (
    "이번 분기 목표는 사용자 유입을 늘리고 응답 속도를 개선하는 것이다. "
    "담당자는 배포 일정을 조율하고 검토 의견을 정리해 공유하기로 했다. "
    "지표는 주간 단위로 집계하며 이상치가 보이면 원인을 분석한다. "
    "회의에서는 우선순위를 다시 확인하고 남은 작업을 나누기로 합의했다. "
)


def synth(n_chars: int, seed: int) -> str:
    """길이 n_chars인 합성 한국어. seed로 시작 위치를 바꿔 캐시 히트를 피한다."""
    off = (seed * 37) % len(_FILLER)
    s = (_FILLER[off:] + _FILLER * 10)[:n_chars]
    return f"{seed} {s}"[:n_chars]


def load_solar_key() -> str:
    raw = json.load(open(KEYS_PATH, encoding="utf-8"))
    return next(e for e in raw if "upstage" in str(e.get("provider", "")).lower())["key"]


def call_openai(client: httpx.Client, texts: list[str]) -> float:
    body = {"model": os.environ["ORTHUS_EMBEDDING_MODEL"], "input": texts, "dimensions": DIMS}
    t0 = time.perf_counter()
    r = client.post(
        f"{os.environ['ORTHUS_EMBEDDING_BASE_URL'].rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {os.environ['ORTHUS_EMBEDDING_API_KEY']}"},
        json=body,
        timeout=60,
    )
    r.raise_for_status()
    dt = time.perf_counter() - t0
    assert len(r.json()["data"]) == len(texts)
    return dt


def call_solar(client: httpx.Client, texts: list[str], model: str, key: str) -> float:
    body = {"model": model, "input": texts, "dimensions": DIMS}
    t0 = time.perf_counter()
    r = client.post(SOLAR_URL, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=60)
    r.raise_for_status()
    dt = time.perf_counter() - t0
    assert len(r.json()["data"]) == len(texts)
    return dt


def pct(a: list[float], p: float) -> float:
    if not a:
        return float("nan")
    s = sorted(a)
    i = min(int(p / 100 * len(s)), len(s) - 1)
    return s[i]


def report(name: str, ms: list[float], fails: int) -> None:
    if not ms:
        print(f"  {name:26} 전부 실패({fails})")
        return
    print(
        f"  {name:26} p50 {st.median(ms):6.0f}ms   p90 {pct(ms,90):6.0f}ms   "
        f"p99 {pct(ms,99):6.0f}ms   평균 {st.mean(ms):6.0f}ms" + (f"   실패 {fails}" if fails else "")
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="임베딩 지연 측정 (합성 텍스트, 회사 데이터 미사용)")
    ap.add_argument("--n-query", type=int, default=40, help="질의 지연 표본 수 (모델당)")
    ap.add_argument("--n-batch", type=int, default=5, help="배치 표본 수 (모델당)")
    ap.add_argument("--batch-size", type=int, default=90)
    args = ap.parse_args()

    key = load_solar_key()
    results: dict = {}

    with httpx.Client() as client:
        print("워밍업...", flush=True)
        for _ in range(2):
            try:
                call_openai(client, [synth(QUERY_CHARS, 0)])
                call_solar(client, [synth(QUERY_CHARS, 0)], "embedding-query", key)
            except Exception as e:  # noqa: BLE001
                print(f"  워밍업 실패: {type(e).__name__}: {str(e)[:80]}")

        # ---- 1. 질의 지연 (1건씩, hot path) — 교대 실행 ----
        print(f"\n질의 지연 측정: {args.n_query}회 × 2모델 (교대, {QUERY_CHARS}자)...", flush=True)
        o_ms: list[float] = []
        s_ms: list[float] = []
        o_f = s_f = 0
        for i in range(args.n_query):
            t = synth(QUERY_CHARS, i + 1)
            try:
                o_ms.append(call_openai(client, [t]) * 1000)
            except Exception:  # noqa: BLE001
                o_f += 1
            try:
                s_ms.append(call_solar(client, [t], "embedding-query", key) * 1000)
            except Exception:  # noqa: BLE001
                s_f += 1
            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{args.n_query}", flush=True)
        results["query"] = {"openai": o_ms, "solar": s_ms}

        # ---- 2. 배치 처리량 (재임베딩 마이그레이션 원가) ----
        print(f"\n배치 처리량 측정: {args.n_batch}회 × 2모델 "
              f"({args.batch_size}건/배치, {CHUNK_CHARS}자)...", flush=True)
        ob_ms: list[float] = []
        sb_ms: list[float] = []
        ob_f = sb_f = 0
        for i in range(args.n_batch):
            batch = [synth(CHUNK_CHARS, i * 1000 + j) for j in range(args.batch_size)]
            try:
                ob_ms.append(call_openai(client, batch) * 1000)
            except Exception as e:  # noqa: BLE001
                ob_f += 1
                print(f"  openai batch fail: {type(e).__name__}: {str(e)[:70]}")
            try:
                sb_ms.append(call_solar(client, batch, "embedding-passage", key) * 1000)
            except Exception as e:  # noqa: BLE001
                sb_f += 1
                print(f"  solar batch fail: {type(e).__name__}: {str(e)[:70]}")
        results["batch"] = {"openai": ob_ms, "solar": sb_ms}

    # ---- 리포트 ----
    print("\n" + "=" * 78)
    print(f"1. 질의 지연 — `/ask` 요청마다 1회 (사용자 체감 경로), {QUERY_CHARS}자 1건")
    report("OpenAI (3-small)", o_ms, o_f)
    report("Solar (embedding-query)", s_ms, s_f)
    if o_ms and s_ms:
        d = st.median(s_ms) - st.median(o_ms)
        faster = "Solar가 빠름" if d < 0 else "OpenAI가 빠름"
        print(f"\n  → p50 차이 {d:+.0f}ms ({faster}, {abs(d)/st.median(o_ms)*100:.0f}%)")
        print(f"  → 이 값은 매 /ask에 그대로 더해진다(그라운딩 1회 = 임베딩 1회).")

    print("\n" + "=" * 78)
    print(f"2. 배치 처리량 — 교체 시 1회성 재임베딩 원가 ({args.batch_size}건/배치, {CHUNK_CHARS}자)")
    report("OpenAI (3-small)", ob_ms, ob_f)
    report("Solar (embedding-passage)", sb_ms, sb_f)
    for label, arr in (("OpenAI", ob_ms), ("Solar", sb_ms)):
        if arr:
            per = st.median(arr) / args.batch_size
            total_min = TOTAL_CHUNKS * per / 1000 / 60
            print(f"  {label:8} 건당 {per:5.1f}ms → {TOTAL_CHUNKS:,}청크 전량 재임베딩 "
                  f"약 {total_min:5.1f}분 (단일 스레드)")

    print("\n" + "=" * 78)
    print("""주의:
  - OpenAI=미국 / Upstage=한국 리전. 이 박스 위치의 지리적 이점이 결과를 지배할 수 있다.
    실제 운영 이점이지만 **배포 위치가 바뀌면 재측정**해야 한다.
  - 이 박스는 부하 변동이 크다(실측 load 3~27). load가 높을 때 잰 값은 신뢰하지 마라.
  - 재임베딩 시간은 단일 스레드 기준이다. 실제로는 병렬화하므로 하한으로 봐라.""")

    out = f"{HERE}/latency_results.json"
    json.dump(
        {"query_ms": results["query"], "batch_ms": results["batch"],
         "batch_size": args.batch_size, "total_chunks": TOTAL_CHUNKS},
        open(out, "w"), indent=2,
    )
    print(f"\n상세: {out}")


if __name__ == "__main__":
    main()
