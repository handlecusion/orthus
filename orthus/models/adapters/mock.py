"""Deterministic offline adapters (P1 default). No network. Used by tests/CI and
`make demo`. Same Protocols as real adapters so swapping is a registry change."""

from __future__ import annotations

import hashlib
import json
import math
import re

DIM = 1024
_WORD = re.compile(r"\w+", re.UNICODE)


def _features(text: str) -> list[str]:
    """Words + char trigrams. Trigrams give overlap on Korean morphology
    (e.g. '휴가' inside '휴가는') so lexically related texts get higher cosine."""
    t = text.lower()
    feats = _WORD.findall(t)
    compact = re.sub(r"\s+", "", t)
    feats += [compact[i : i + 3] for i in range(len(compact) - 2)]
    return feats


def _hash_dim(token: str) -> int:
    return int.from_bytes(hashlib.sha1(token.encode("utf-8")).digest()[:4], "big") % DIM


class MockEmbedding:
    """Deterministic 1024-d unit vectors via feature hashing of words + char
    trigrams. Same text -> same vector, and lexically overlapping texts have
    higher cosine similarity, so offline RAG/grounding actually retrieves."""

    model_version = "mock:1024"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            v = [0.0] * DIM
            feats = _features(t)
            for tok in feats:
                # signed hashing reduces collisions cancelling vs reinforcing.
                d = _hash_dim(tok)
                sign = 1.0 if (_hash_dim(tok + "#") & 1) else -1.0
                v[d] += sign
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


class MockChat:
    """Scripted chat. `rules` is an ordered list of (needle, response); the first
    needle found in the user (or system) text wins. Lets tests pin exact SQL.

    Defaults:
    - json_only distill: a minimal valid WikiSource+claim JSON echoing the doc,
      so the T1 author-on-save path produces a grounded wiki page.
    - json_only compile: {"sql": "SELECT 1"} — a trivially valid SELECT.
    - otherwise (wiki answer): a grounded-sounding stub citing the context head.

    Router classify is NOT defaulted — tests pin the route by scripting
    {"route": ...} via `rules`. An unscripted classify call falls through to the
    compile default (no "route" key), so `classify()` fail-safes to "wiki".
    """

    model_id = "mock:chat"

    def __init__(self, rules: list[tuple[str, str]] | None = None, default: str | None = None):
        self.rules = rules or []
        self.default = default

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        haystack = f"{system}\n{user}"
        for needle, response in self.rules:
            if needle in haystack:
                return response
        if self.default is not None:
            return self.default
        if json_only:
            if "distillation" in system:
                return _default_distill_json(user)
            return json.dumps({"sql": "SELECT 1"})
        head = user.strip().replace("\n", " ")[:200]
        return f"근거에 기반한 답변입니다. (context head: {head})"


def _default_distill_json(user: str) -> str:
    """Minimal valid distill output: one claim whose evidence echoes the document
    body, so the authored wiki page is retrievable on the same terms."""
    body = user.replace("\n", " ").strip()[:400]
    return json.dumps(
        {
            "summary": body,
            "key_concepts": [],
            "terminology": [],
            "open_questions": [],
            "claims": [
                {
                    "claim": body or "문서 요약",
                    "evidence": body,
                    "confidence": "medium",
                    "page": {"slug": "doc", "title": "문서"},
                    "conflicting": [],
                }
            ],
            "entities": [],
        }
    )
