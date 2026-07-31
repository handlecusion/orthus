"""M4 — verify the production assignment layer reproduces the experiment.

The Fugu-KO experiment measured 94.9% on the golden set by *injecting* workers into
orthus's entry functions (`chat_model=`). This script proves the same numbers come out
of the **production path**: it calls `query_structured()` / `classify()` with no
`chat_model` at all, so `get_chat_model_for(task)` is what picks the model.

It also counts fallbacks. A fallback is a silent quality regression — the request
still succeeds, but on the default model — so "94.9% with 12 fallbacks" would mean
the assignment is not actually serving traffic. Fallbacks must be 0.

    export ORTHUS_NODE_DB=orthus_company            # the real company snapshot
    ORTHUS_MODEL_ORCHESTRATION_ENABLED=true python scripts/verify_model_orchestration.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from uuid import UUID

import psycopg

import orthus.models.orchestration as orch
from orthus.models.orchestration import ASSIGNMENTS
from orthus.router.route import classify
from orthus.settings import get_settings
from orthus.structured.query import query_structured

# The experiment's golden set + its number-set answer key. Same 18 + 21 questions
# the report scores (competition-report.md §6.5).
FUGU = Path(os.environ.get("FUGU_DIR", "../fugu-ko-experiment/experiments/fugu-ko"))
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")
# Same caller the experiment used, so the only thing that differs from its run is who
# picks the model: there it was injected, here production resolves it.
UID = UUID("11111111-1111-1111-1111-111111111111")

# id -> (db_name, kind, group_key|None, extra_where|None) — copied from t3_gold.py
SPECS: dict[str, tuple] = {
    "t3-01": ("협업업무표", "groupby", "상태", None),
    "t3-02": ("협업업무표", "count", None, "properties->>'상태'='완료'"),
    "t3-03": ("협업업무표", "groupby", "담당자", None),
    "t3-04": ("협업업무표", "groupby", "우선순위", None),
    "t3-05": ("협업업무표", "count", None, "properties->>'상태'='보류'"),
    "t3-06": ("Nova 개발 로드맵", "groupby", "상태", None),
    "t3-07": ("Nova 개발 로드맵", "groupby", "우선순위", None),
    "t3-09": ("Nova 개발 로드맵", "groupby", "오너", None),
    "t3-10": ("🎯 NOVA 영입 후보 리스트", "count", None, None),
    "t3-11": ("🎯 NOVA 영입 후보 리스트", "groupby", "Tier", None),
    "t3-12": ("🎯 NOVA 영입 후보 리스트", "groupby", "발송 상태", None),
    "t3-13": ("회사 미팅 기록", "count", None, None),
    "t3-14": ("회사 미팅 기록", "groupby", "파트너사", None),
    "t3-15": ("AI관련툴", "count", None, None),
    "t3-16": ("AI관련툴", "groupby", "카테고리", None),
    "t3-17": ("파트너", "groupby", "분야", None),
    "t3-18": ("배포 공지", "groupby", "공지 상태", None),
    "t3-r09": ("배포 공지", "count", None, None),
}


def gold_numbers(db: str, kind: str, gkey, where) -> set[int]:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        w = "scope='company' AND db_name=%s" + (f" AND {where}" if where else "")
        if kind == "count":
            cur.execute(f"SELECT count(*) FROM notion_rows WHERE {w}", (db,))
            return {int(cur.fetchone()[0])}
        cur.execute(
            f"SELECT count(*) FROM notion_rows WHERE {w} GROUP BY properties->>%s", (db, gkey)
        )
        return {int(r[0]) for r in cur.fetchall()}


def result_numbers(rows) -> set[int]:
    nums: set[int] = set()
    for row in rows or []:
        for c in row:
            if isinstance(c, bool):
                continue
            if isinstance(c, int):
                nums.add(c)
            elif isinstance(c, str) and re.fullmatch(r"-?\d+", c.strip()):
                nums.add(int(c))
    return nums


def load_golden(task: str) -> list[dict]:
    return json.loads((FUGU / "golden" / f"{task}.json").read_text(encoding="utf-8"))["items"]


class _FallbackCounter:
    """Wrap FallbackChat.complete so we see every degradation, not just the score."""

    def __init__(self):
        self.count = 0
        self._orig = orch.FallbackChat.complete
        counter = self

        def counted(fb_self, system, user, *, json_only=False):
            before = counter.count
            try:
                return fb_self._primary.complete(system, user, json_only=json_only)
            except Exception:
                counter.count = before + 1
                return fb_self._fallback.complete(system, user, json_only=json_only)

        orch.FallbackChat.complete = counted

    def restore(self):
        orch.FallbackChat.complete = self._orig


def main() -> int:
    s = get_settings()
    if not s.model_orchestration_enabled:
        print("ORTHUS_MODEL_ORCHESTRATION_ENABLED is false — nothing to verify.")
        return 2

    print("=== 배정 (production path) ===")
    for task, worker in ASSIGNMENTS.items():
        print(f"  {task:11} → {worker}")

    counter = _FallbackCounter()
    try:
        # --- DB질의: production picks the model, we only supply the question -----
        gold = {i: gold_numbers(*spec) for i, spec in SPECS.items()}
        questions = {it["id"]: it["q"] for it in load_golden("t3")}
        ok = 0
        for qid, want in gold.items():
            # No chat_model= — production's get_chat_model_for(task) picks the model.
            answer = query_structured(UID, questions[qid], scope="company")
            got = result_numbers(getattr(answer, "rows", None))
            hit = bool(want and want <= got)
            ok += hit
            if not hit:
                print(f"    ✗ {qid}: want⊆ {sorted(want)[:4]} got {sorted(got)[:4]}")
        sql_pct = ok / len(gold) * 100

        # --- 라우팅 ------------------------------------------------------------
        routes = load_golden("t5")
        r_ok = sum(1 for it in routes if classify(it["q"]) == it["expected"])  # Route = str
        route_pct = r_ok / len(routes) * 100
    finally:
        counter.restore()

    total = (ok + r_ok) / (len(gold) + len(routes)) * 100
    print("\n=== 결과 (실험 재현 대조) ===")
    print(f"  DB질의  {ok:2}/{len(gold)}  {sql_pct:5.1f}%   (실험 94.4%)")
    print(f"  라우팅  {r_ok:2}/{len(routes)}  {route_pct:5.1f}%   (실험 95.2%)")
    print(f"  합계    {ok + r_ok:2}/{len(gold) + len(routes)}  {total:5.1f}%   (실험 94.9%)")
    print(f"\n  폴백 발생: {counter.count}회  ← 0이어야 배정이 실제로 트래픽을 받은 것")

    if counter.count:
        print("\n  ❌ 폴백이 발생했다. 점수가 좋아도 국내 모델이 아니라 기본 모델이 답한 것.")
        return 1
    print("\n  ✅ 폴백 0 — 배정된 국내 모델이 프로덕션 경로로 전 요청을 처리했다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
