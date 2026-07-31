"""E2 §4.2/§4.3 — 오케스트레이터 루프 E2E + 단계별 실패 귀속.

D2~D4는 전부 **단일 결정 단위**를 쟀다(intent/decompose/synthesize 따로). 여기서는
`answer_or_decompose` **루프 전체**를 arm별로 돌려 태스크 성공률을 재고, 실패를 단계로 귀속한다.

arm 3종 (paired — 같은 시나리오를 전 arm이 푼다):
  baseline  : 현행 프로덕션 단일슬롯(gpt-4o-mini)이 전 단계
  solar     : 최강 단일 국내 모델이 전 단계
  assign(b) : PromptKeyedSelectorChat — 스테이지별 static_map 배정 (프로덕션 diff 0줄)

채점(§4.2): 시나리오마다 하위앵커를 사전 고정. E2E 성공 = **모든 하위앵커가 최종 답에 존재**.
  - 집계 앵커: 실DB gold number-set (모델 편향 0)
  - 지식 앵커: e2_anchor_probe에서 baseline·solar가 **공통 산출**한 항 (모델 편향 0)

귀속(§4.3): 실패 문항마다 결정론으로 단계 지목
  gate_miss      — should_decompose가 복합질문을 단일로 판정(분해 자체를 안 함)
  split_bad      — 분해했으나 split 결과에 그 앵커의 토픽이 아예 없음
  leaf_wrong     — split엔 있는데 어느 리프 본문에도 앵커가 없음(리프가 못 답함)
  synthesis_drop — 리프 본문엔 앵커가 있는데 최종 합성 답에서 사라짐

grounding 술어는 3종(prod/gap_aware/extended)을 **같은 실행 기록에 사후 적용**해 비교한다
(e2_grounding.py — 프로덕션 무수정, 수정안의 효과를 오프라인으로 검증).

실행:
  set -a; source ~/.orthus/nodes/company/node.env; set +a
  export ORTHUS_NODE=company ORTHUS_NODE_DB=orthus_company
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/e2_e2e.py --arms baseline,solar,assign
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "selectors"))
sys.path.insert(0, str(HERE.parent.parent))

from e2_grounding import PREDICATES  # noqa: E402
from pool import build_pool  # noqa: E402
from prod_spy import ProdSpy, preflight  # noqa: E402
from prompt_keyed import PromptKeyedSelectorChat, SingleChat, verify_registry  # noqa: E402
from orthus.models import orchestration as orch  # noqa: E402
from orthus.router.decompose import _extract_body, answer_or_decompose  # noqa: E402
from orthus.router.tools import knowledge_only_leaves  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
GOLDEN = HERE / "golden"
RAW = HERE / "analysis" / "raw"

_NUM = re.compile(r"-?\d[\d,]*")


def numbers_in(text: str) -> set[int]:
    """텍스트의 정수 집합. '1,234'/'771건' 모두 잡는다."""
    out: set[int] = set()
    for m in _NUM.findall(text or ""):
        try:
            out.add(int(m.replace(",", "")))
        except ValueError:
            continue
    return out


def anchor_hit(anchor: dict, text: str, sources: list[str] | None = None) -> bool:
    """앵커가 존재하는가 (결정론).

    kind 3종:
      number  — 정수집합 포함 (실DB gold, 정답 유일)
      keyword — any_of 중 하나가 본문에 등장
      page    — **다중정답 허용 앵커(§13 자기수정)**. 답이 *기대 wiki 페이지에 그라운딩*됐는가로
                판정한다. "atlas이 뭐야?"처럼 참인 답이 여럿인 질문에서 keyword 앵커는
                *다른 참인 답*을 오답 처리해 §7 수치를 하방 편향시켰다. 페이지 앵커는
                "어느 근거에서 답했나"를 보므로 표현·관점 차이에 강건하다.
                any_of 키워드가 함께 있으면 **OR**로 인정한다(둘 중 하나만 맞아도 성공).
    """
    if anchor["kind"] == "number":
        nums = numbers_in(text)
        return all(int(v) in nums for v in anchor["values"])
    if anchor["kind"] == "page":
        want = {s.lower() for s in anchor.get("slugs", [])}
        got = {s.lower() for s in (sources or [])}
        if want & got:
            return True
        low = (text or "").lower()  # 그라운딩 실패해도 내용이 맞으면 인정(OR)
        return any(kw.lower() in low for kw in anchor.get("any_of", []))
    if not text:
        return False
    low = text.lower()
    return any(kw.lower() in low for kw in anchor["any_of"])


def _sources_of(routed) -> list[str]:
    """RoutedAnswer에서 인용된 wiki page slug (합성/단일 양쪽)."""
    if routed is None:
        return []
    if getattr(routed, "synthesized_body", None) is not None:
        return [s.page_slug for s in routed.synthesized_body.sources]
    if getattr(routed, "wiki", None) is not None:
        return [s.page_slug for s in routed.wiki.sources]
    return []


_ZERO_COUNT = re.compile(r"\[count\]\s*0\b")


def structured_leaves(results: list[dict]) -> tuple[int, int]:
    """(structured 리프 총수, 그중 zero-answer 수).

    zero-answer = 게이트를 통과하고 `status=executed`로 실행됐는데 결과가 0인 리프 —
    **"확신에 찬 0건"**이다. `grounded=True`로 합성에 들어가므로 사용자 답과 후속 행동으로
    그대로 샌다. E2가 배정을 못 켜게 막은 지표가 정확히 이것이다(배정 18% vs 현행 11%).
    판정식은 e2_abl_eval.zeros()와 동일하게 유지한다(같은 자를 쓴다).
    """
    total = zero = 0
    for r in results:
        for lf in r.get("leaves", []):
            if lf.get("mode") != "structured":
                continue
            total += 1
            if _ZERO_COUNT.search(lf.get("body") or ""):
                zero += 1
    return total, zero


def topic_in_split(anchor: dict, split_texts: list[str]) -> bool:
    """split 결과 하위질문 중에 이 앵커의 토픽이 살아 있는가."""
    joined = " ".join(split_texts).lower()
    return any(t.lower() in joined for t in anchor["topic"])


def run_one(chat, item: dict) -> dict:
    """시나리오 1건을 루프 전체로 실행하고 중간 산출물을 전부 남긴다.

    `chat`이 `ProdSpy`(arm assign_final)면 **chat_model=None**으로 부른다 — 호출부의
    `chat_model or get_chat_model_for(task)` 패턴 때문에, 무엇이든 주입하면 프로덕션 배정
    계층을 건너뛰어 버린다. 그 arm에서 재는 대상은 바로 그 계층이다.
    """
    t0 = time.monotonic()
    chat.reset()
    is_prod = getattr(chat, "is_production", False)
    injected = None if is_prod else chat
    try:
        # knowledge_only_leaves(): action-intake 리프를 억제한다. 이유 2가지 —
        # (1) 계획 §3 Out-of-scope: E2E는 지식/집계 복합 질문 한정(policy gate 실측은 P3 회귀 영역).
        # (2) 부작용 차단: 억제 안 하면 명령형으로 오분류된 하위질문이 `queue_agent_work`로
        #     실DB에 agent_work_items 행을 남긴다(실험이 프로덕션 데이터를 오염).
        with knowledge_only_leaves():
            routed = answer_or_decompose(
                UID,
                item["q"],
                scope="company",
                project=None,
                chat_model=injected,
                context_wiki_slug=None,
                history=None,
                stream_id=None,
                actor_role="owner",
                allow_agentic_in_leaf=False,  # cli 엔진은 chat_model을 무시 → 실험 격리 위해 off
                learn=False,
                record_gaps=False,
            )
    except Exception as e:  # noqa: BLE001
        return {
            "id": item["id"], "error": f"{type(e).__name__}: {str(e)[:160]}",
            "ms": int((time.monotonic() - t0) * 1000),
        }

    ms = int((time.monotonic() - t0) * 1000)
    decomposed = routed.mode == "decomposed"

    # ── 중간 산출물 ──────────────────────────────────────────────────────────
    parts = routed.parts or []
    split_texts = [p.text or "" for p in parts]
    leaf_bodies: list[str] = []
    leaf_meta: list[dict] = []
    for p in parts:
        body = _extract_body(p.routed) if p.routed is not None else ""
        leaf_bodies.append(body)
        leaf_meta.append({
            "text": p.text,
            "mode": (p.routed.mode if p.routed else None),
            "error": p.error,
            "grounded_wire": p.grounded,          # 프로덕션이 실제로 매긴 값
            "grounded_pred": {
                name: bool(fn(p.routed)) for name, fn in PREDICATES.items()
            },
            "body": body[:400],
        })

    if decomposed and routed.synthesized_body is not None:
        final = routed.synthesized_body.answer or ""
    elif routed.wiki is not None:
        final = routed.wiki.answer or ""
    elif routed.structured is not None:
        final = _extract_body(routed)
    else:
        final = ""

    # ── 앵커 채점 + 귀속 ────────────────────────────────────────────────────
    final_sources = _sources_of(routed)
    leaf_sources = [_sources_of(p.routed) for p in parts]
    anchors = item["anchors"]
    per_anchor = []
    for a in anchors:
        in_final = anchor_hit(a, final, final_sources)
        in_leaf = any(
            anchor_hit(a, b, leaf_sources[i] if i < len(leaf_sources) else [])
            for i, b in enumerate(leaf_bodies)
        )
        in_split = topic_in_split(a, split_texts) if decomposed else False
        if in_final:
            attrib = None
        elif not decomposed:
            attrib = "gate_miss"
        elif not in_split:
            attrib = "split_bad"
        elif not in_leaf:
            attrib = "leaf_wrong"
        else:
            attrib = "synthesis_drop"
        per_anchor.append({
            "topic": a["topic"][0], "kind": a["kind"],
            "in_final": in_final, "in_leaf": in_leaf, "in_split": in_split,
            "attrib": attrib,
        })
    # 페이지 앵커 진단용: 최종 답이 실제로 인용한 근거
    final_src = final_sources

    success = all(p["in_final"] for p in per_anchor)
    return {
        "id": item["id"], "type": item["type"], "q": item["q"],
        "success": success,
        "decomposed": decomposed, "n_parts": len(parts),
        "expected_parts": item.get("expected_parts"),
        "prefilter_blind": bool(item.get("prefilter_blind")),
        "anchors": per_anchor,
        "split": split_texts,
        "leaves": leaf_meta,
        "final_sources": final_src,
        "final": final[:800],
        "calls": [c.__dict__ for c in chat.calls],
        "n_calls": len(chat.calls),
        "unmatched": list(chat.unmatched),
        "ms": ms,
    }


ARM_LABEL = {
    "baseline": "baseline(gpt-4o-mini)",
    "solar": "solar 단일",
    "assign": "(b) 배정 — 1차안(기각됨)",
    "assign_final": "★ 최종배정(프로덕션 계층)",
}


def build_arm(arm: str, pool: dict, trace: Path):
    if arm == "assign_final":
        # 프로덕션 배정 계층을 그대로 태운다. pool을 안 쓴다 — 모델은 orthus settings가 만든다.
        spy = ProdSpy(trace_path=trace)
        spy.install()
        return spy
    if arm == "assign":
        return PromptKeyedSelectorChat(pool, default="solar", trace_path=trace)
    return SingleChat(pool[arm], arm, trace_path=trace)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="baseline,solar,assign")
    ap.add_argument("--limit", type=int, default=0)
    # §13 재측정용. 기본값은 기존 골든/네임스페이스라 이전 실행과 완전 호환.
    # TS21(공용 워크트리 병렬 세션) 교훈: 새 실행은 반드시 새 tag로 → 기존 원자료 미덮어씀.
    ap.add_argument("--golden", default="e2.json", help="golden/<파일>")
    ap.add_argument("--tag", default="", help="원자료 접미사 (예: v2 → e2_e2e_v2_<arm>.jsonl)")
    args = ap.parse_args()

    arms = args.arms.split(",")

    # ── 무효 조건 사전 게이트 ────────────────────────────────────────────────
    # 실험 셀렉터 arm: 프롬프트 상수 drift면 여기서 raise.
    if any(a in {"assign", "baseline", "solar", "ax", "exaone"} for a in arms):
        verify_registry()
        print("registry OK — 프롬프트-키 셀렉터가 orthus 상수 11종과 정합")
    # 프로덕션 arm: 배정이 실제로 물리는 상태인지 **측정 시작 전에** 확인한다. 여기서 안 막으면
    # "배정을 켰다고 믿었는데 전부 기본 슬롯이 답한" 측정이 그럴듯한 숫자를 내고 끝난다.
    if "assign_final" in arms:
        problems = preflight()
        if problems:
            raise SystemExit(
                "❌ assign_final 사전 게이트 실패 — 측정을 시작하지 않는다:\n  - "
                + "\n  - ".join(problems)
            )
        assigned = {t: orch.ASSIGNMENTS[t] for t in sorted(orch.ASSIGNMENTS)}
        print(f"preflight OK — 배정 물림 확인. ASSIGNMENTS={assigned}")
    print()

    suffix = f"_{args.tag}" if args.tag else ""
    items = json.load(open(GOLDEN / args.golden, encoding="utf-8"))["items"]
    if args.limit:
        items = items[: args.limit]
    # assign arm(1차안)은 국내 워커 3종이 전부 필요(그 배정표가 ax도 부른다).
    # assign_final은 pool을 안 쓴다 — 프로덕션이 settings로 자기 어댑터를 만든다.
    need = {"solar", "ax", "exaone"} if "assign" in arms else set()
    need |= {a for a in arms if a in {"baseline", "solar", "ax", "exaone"}}
    pool = build_pool(sorted(need)) if need else {}

    RAW.mkdir(parents=True, exist_ok=True)
    print(f"[E2 E2E] golden={args.golden} tag={args.tag or '(기본)'} — {len(items)}문항 × {arms} (paired)\n")

    for arm in arms:
        trace = RAW / f"e2_calls{suffix}_{arm}.jsonl"
        trace.unlink(missing_ok=True)
        chat = build_arm(arm, pool, trace)
        results = []
        for i, item in enumerate(items, 1):
            r = run_one(chat, item)
            results.append(r)
            mark = "O" if r.get("success") else "X"
            print(f"  {mark} {item['id']:7} {arm:9} {r.get('n_calls', 0):2}콜 {r['ms']:6}ms"
                  f"  {'분해' if r.get('decomposed') else '단일'}"
                  f"  ({i}/{len(items)})")

        out = RAW / f"e2_e2e{suffix}_{arm}.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        ok = sum(1 for r in results if r.get("success"))
        unmatched = sum(len(r.get("unmatched", [])) for r in results)
        # 이번 재측정의 1차 판정 지표 — E2가 배정을 막은 근거가 zero-answer였다(18% vs 11%).
        sl, zl = structured_leaves(results)
        z_pct = f"{zl / sl * 100:.0f}%" if sl else "—"
        print(f"\n  {ARM_LABEL[arm]:26} 성공 {ok}/{len(results)} ({ok / len(results) * 100:.0f}%)"
              f"  zero-answer {zl}/{sl} ({z_pct})"
              f"  미매칭콜 {unmatched}{'  ← 무효!' if unmatched else ''}\n")

    print(f"원자료: {RAW}/e2_e2e_*.jsonl  → 판정은 e2_eval.py")


if __name__ == "__main__":
    main()
