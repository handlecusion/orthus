"""국내 3종(Solar/A.X/EXAONE) 임베딩 엔드포인트 존재 여부 라이브 프로브.

키/응답 원문은 출력하지 않는다 — HTTP status + 벡터 length만. 회사 데이터도 쓰지 않는다
(합성 문장 1개). VARCO는 OpenAI-호환 엔드포인트 자체가 없어(D0에서 chat도 드롭) 제외.

실측 결과는 `README.md` §1 / `LAB-NOTES.md` S1.
"""
from __future__ import annotations
import json
import os

import httpx

KEYS_PATH = os.environ.get(
    "FUGU_KEYS", "<로컬 키 저장소>/keys.json"
)

TARGETS = {
    "solar": {
        "match": "upstage",
        "candidates": [
            ("https://api.upstage.ai/v1/embeddings", "embedding-query"),
            ("https://api.upstage.ai/v1/embeddings", "solar-embedding-1-large-query"),
            ("https://api.upstage.ai/v2/embeddings", "embedding-query"),
            ("https://api.upstage.ai/v2/embeddings", "solar-embedding-1-large-query"),
        ],
    },
    "ax": {
        "match": "a.x",
        "candidates": [
            ("https://awf-gw.adot.ai/v1/embeddings", "A.X-Embedding"),
            ("https://awf-gw.adot.ai/v1/embeddings", "A.X-K1"),
        ],
    },
    "exaone": {
        "match": "exaone",
        "candidates": [
            ("https://api.friendli.ai/dedicated/v1/embeddings", None),  # model = endpoint id
        ],
    },
}


def load_keys() -> dict[str, dict]:
    raw = json.load(open(KEYS_PATH, encoding="utf-8"))
    out = {}
    for entry in raw:
        prov = str(entry.get("provider", "")).lower()
        for slug, spec in TARGETS.items():
            if spec["match"] in prov:
                out[slug] = entry
    return out


def try_embed(url: str, key: str, model: str, text: str = "테스트 문장입니다.") -> tuple[int, str]:
    body = {"model": model, "input": text}
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(url, headers={"Authorization": f"Bearer {key}"}, json=body)
        note = ""
        if r.status_code == 200:
            try:
                data = r.json()
                vec = data["data"][0]["embedding"]
                note = f"OK dims={len(vec)}"
            except Exception as e:
                note = f"200 but unparsable: {type(e).__name__}"
        else:
            note = r.text[:150].replace("\n", " ")
        return r.status_code, note
    except Exception as e:
        return -1, f"{type(e).__name__}: {str(e)[:100]}"


def main():
    keys = load_keys()
    for slug, spec in TARGETS.items():
        if slug not in keys:
            print(f"{slug:8} SKIP (no key)")
            continue
        entry = keys[slug]
        key = entry["key"]
        base_model = str(entry.get("model", "")).split()[0]
        print(f"--- {slug} ---")
        for url, model in spec["candidates"]:
            m = model or base_model
            status, note = try_embed(url, key, m)
            print(f"  {url}  model={m!r:35}  status={status}  {note}")


if __name__ == "__main__":
    main()
