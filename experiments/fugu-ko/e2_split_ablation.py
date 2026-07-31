"""E2 절제실험 — "잎 우위는 왜 루프에서 증발하는가"의 범인을 split으로 특정한다.

배경(e2-results.md §7): 잎에서 structured +16%p 앞서던 solar가 루프 E2E에서는 baseline에
−5%p로 뒤집힌다. 가설: **루프가 사용자 질문과 리프 사이에 손실 있는 변환(split)을 끼워 넣는데,
잎 벤치마크는 그 단계를 측정한 적이 없다.**

관측된 정황:
- E2-11: 원문 첫 절의 한정어 "최근"이 solar의 split에서 **둘째 하위질문으로 누출** →
  NL→SQL이 충실히 날짜 필터로 컴파일 → 0 (정답 48). 리프 모델과 무관한 **split 결함**.
- E2-19: baseline split "…몇 개인지 확인해줘"→794 ✅ vs solar split "…몇 개인가요?"→0 ❌.
  단 이건 **교란**(split 문구도 다르고 리프 모델도 다름) → 이 실험이 가른다.
- solar arm과 (b) 배정 arm의 structured 실패가 **완전 동일**(E2-10/11/19) — 배정표가
  `decompose→solar` + `structured→solar`라 **split을 만드는 모델과 먹는 모델이 같기** 때문.

## arm 설계

  fixed_*  : LLM split을 **우회**하고 골든 `subq`(= D2가 잎을 측정할 때 쓴 t2/t3 원문)를
             SubQuestion으로 직접 주입. fan_out/synthesize는 프로덕션 함수 그대로.
             → 리프에 **깨끗한 입력**을 줬을 때 잎 우위가 되살아나는가?
  cross    : LLM split은 그대로 두되 **decompose(gate+split)→baseline, structured→solar**.
             → 스테이지별 최강이 아니라 **파이프라인으로서의 최강**이 따로 있는가?

판정축은 **수치 앵커(실DB gold, 정답 유일)** — keyword 앵커는 다중정답 취약(§13).

실행:
  set -a; source ~/.orthus/nodes/company/node.env; set +a
  export ORTHUS_NODE=company ORTHUS_NODE_DB=orthus_company
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/e2_split_ablation.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "selectors"))
sys.path.insert(0, str(HERE.parent.parent))

from e2_e2e import UID, anchor_hit, _sources_of  # 동일 채점기 재사용  # noqa: E402
from e2_grounding import PREDICATES  # noqa: E402
from pool import build_pool  # noqa: E402
from prompt_keyed import PromptKeyedSelectorChat, SingleChat, match_stage, verify_registry  # noqa: E402
from static_map import TASK_MODEL  # noqa: E402
from orthus.router.decompose import _enrich_parts, _extract_body, fan_out, synthesize  # noqa: E402
from orthus.router.tools import knowledge_only_leaves  # noqa: E402
from orthus.schemas.canonical import RoutedAnswer, SubQuestion  # noqa: E402
from orthus.settings import get_settings  # noqa: E402

GOLDEN = HERE / "golden"
RAW = HERE / "analysis" / "raw"


class OverrideSelectorChat(PromptKeyedSelectorChat):
    """(b) 배정에 스테이지별 override를 얹은 셀렉터 (cross arm용)."""

    def __init__(self, pool, overrides: dict[str, str], **kw):
        super().__init__(pool, **kw)
        self._ov = overrides                     # task label → worker slug
        self.model_id = f"selector(cross):{overrides}"

    def _dispatch(self, system: str):
        m = match_stage(system)
        if m is None:
            self.unmatched.append(system[:80])
            return "UNMATCHED", self._default
        stage, task = m
        return stage, self._ov.get(task, TASK_MODEL.get(task, self._default))


def run_fixed_split(chat, item: dict) -> RoutedAnswer:
    """LLM split을 우회하고 골든 subq를 그대로 SubQuestion으로 넣는다.

    should_decompose(게이트)와 split_question(재작성)만 건너뛴다. fan_out(route→leaf)과
    synthesize는 **프로덕션 함수 그대로** — 즉 이 arm에서 달라지는 변수는 split 하나뿐이다.
    (settings의 depends_enabled=True여도 subq는 depends_on=[]라 wave loop == flat fan_out.)
    """
    s = get_settings()
    sub_questions = [
        SubQuestion(id=uuid4(), text=t, scope="company") for t in item["subq"]
    ]
    sub_answers = fan_out(
        sub_questions,
        user_id=UID,
        scope="company",
        project=None,
        chat_model=chat,
        context_wiki_slug=None,
        actor_role="owner",
        allow_agentic_in_leaf=False,
        stream_key=None,
        max_concurrency=s.ask_decompose_max_concurrency,
        leaf_timeout_sec=s.ask_decompose_leaf_timeout_sec,
    )
    sub_answers = _enrich_parts(sub_answers, sub_questions)
    body = synthesize(item["q"], sub_answers, sub_questions, chat_model=chat)
    return RoutedAnswer(
        question=item["q"], mode="decomposed", synthesized_body=body, parts=sub_answers
    )


def run_llm_split(chat, item: dict) -> RoutedAnswer:
    """기존 경로 그대로 (cross arm용) — answer_or_decompose 전체."""
    from orthus.router.decompose import answer_or_decompose

    return answer_or_decompose(
        UID, item["q"], scope="company", project=None, chat_model=chat,
        context_wiki_slug=None, history=None, stream_id=None, actor_role="owner",
        allow_agentic_in_leaf=False, learn=False, record_gaps=False,
    )


def score(item: dict, routed: RoutedAnswer, chat, ms: int) -> dict:
    parts = routed.parts or []
    leaf_bodies = [_extract_body(p.routed) if p.routed else "" for p in parts]
    leaf_srcs = [_sources_of(p.routed) for p in parts]
    final = (routed.synthesized_body.answer if routed.synthesized_body else "") or ""
    final_srcs = _sources_of(routed)

    per = []
    for a in item["anchors"]:
        per.append({
            "topic": a["topic"][0], "kind": a["kind"],
            "in_final": anchor_hit(a, final, final_srcs),
            "in_leaf": any(
                anchor_hit(a, b, leaf_srcs[i] if i < len(leaf_srcs) else [])
                for i, b in enumerate(leaf_bodies)
            ),
        })
    return {
        "id": item["id"], "type": item["type"], "q": item["q"],
        "prefilter_blind": bool(item.get("prefilter_blind")),
        "success": all(p["in_final"] for p in per),
        "anchors": per,
        "split": [p.text or "" for p in parts],
        "leaves": [
            {"text": p.text, "mode": (p.routed.mode if p.routed else None),
             "grounded_wire": p.grounded,
             "grounded_pred": {k: bool(f(p.routed)) for k, f in PREDICATES.items()},
             "body": (_extract_body(p.routed) if p.routed else "")[:300]}
            for p in parts
        ],
        "final": final[:600],
        "n_calls": len(chat.calls),
        "calls": [c.__dict__ for c in chat.calls],
        "unmatched": list(chat.unmatched),
        "ms": ms,
    }


# arm → (러너, chat 빌더 키)
ARMS = {
    # 고정 split — 리프에 깨끗한 입력. split 변수만 제거.
    "fixed_baseline": ("fixed", "baseline"),
    "fixed_solar": ("fixed", "solar"),
    "fixed_assign": ("fixed", "assign"),
    # 교차 배정 — LLM split이되 split은 baseline, structured는 solar
    "cross": ("llm", "cross"),
}


def build_chat(kind: str, pool: dict, trace: Path):
    if kind == "assign":
        return PromptKeyedSelectorChat(pool, default="solar", trace_path=trace)
    if kind == "cross":
        # decompose(gate+split)만 baseline으로 뺀다. 나머지는 static_map 그대로.
        return OverrideSelectorChat(
            pool, {"decompose": "baseline"}, default="solar", trace_path=trace
        )
    return SingleChat(pool[kind], kind, trace_path=trace)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--limit", type=int, default=0)
    # v2 골든(page 앵커) 지원 — v1 keyword 앵커는 고정-split에서 **순환**한다:
    # 앵커를 원문 하위질문의 단독 답변에서 유도했는데 고정-split이 바로 그 원문을 리프에 넣으므로
    # 앵커가 구조적으로 맞는다. page 앵커("어느 wiki 페이지를 인용했나")는 그 순환이 없다.
    ap.add_argument("--golden", default="e2_v2.json")
    ap.add_argument("--tag", default="v2")
    args = ap.parse_args()

    verify_registry()
    items = json.load(open(GOLDEN / args.golden, encoding="utf-8"))["items"]
    if args.limit:
        items = items[: args.limit]
    arms = args.arms.split(",")
    pool = build_pool(["baseline", "solar", "ax", "exaone"])
    RAW.mkdir(parents=True, exist_ok=True)
    tag = f"{args.tag}_" if args.tag else ""

    print(f"[E2 절제 {args.golden}] {len(items)}문항 × {arms}\n")
    for arm in arms:
        mode, kind = ARMS[arm]
        trace = RAW / f"e2_calls_abl_{tag}{arm}.jsonl"
        trace.unlink(missing_ok=True)
        chat = build_chat(kind, pool, trace)
        runner = run_fixed_split if mode == "fixed" else run_llm_split
        results = []
        for i, item in enumerate(items, 1):
            chat.reset()
            t0 = time.monotonic()
            try:
                with knowledge_only_leaves():
                    routed = runner(chat, item)
                r = score(item, routed, chat, int((time.monotonic() - t0) * 1000))
            except Exception as e:  # noqa: BLE001
                r = {"id": item["id"], "error": f"{type(e).__name__}: {str(e)[:140]}",
                     "success": False, "anchors": [], "leaves": [], "unmatched": [],
                     "prefilter_blind": bool(item.get("prefilter_blind")),
                     "type": item["type"], "ms": int((time.monotonic() - t0) * 1000)}
            results.append(r)
            print(f"  {'O' if r['success'] else 'X'} {item['id']:7} {arm:15} "
                  f"{r.get('n_calls', 0):2}콜 {r['ms']:6}ms ({i}/{len(items)})")

        with open(RAW / f"e2_abl_{tag}{arm}.jsonl", "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        ok = sum(1 for r in results if r["success"])
        um = sum(len(r.get("unmatched", [])) for r in results)
        print(f"\n  {arm:15} 성공 {ok}/{len(results)}  미매칭 {um}"
              f"{'  ← 무효!' if um else ''}\n")

    print(f"원자료: {RAW}/e2_abl_{tag}*.jsonl → 판정은 e2_abl_eval.py --tag {args.tag}")


if __name__ == "__main__":
    main()
