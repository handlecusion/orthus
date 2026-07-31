"""E2 grounding 술어 3종 — 프로덕션 현행 / gap-aware 수정안 / 확장 마커 수정안.

E2 앵커 프로브에서 드러난 구조 결함(상세 `analysis/e2-results.md` F1/F2):

- `decompose.py::_run_leaf`의 grounded 판정은 `"근거 없음" not in answer`뿐이다. 그런데
  `"근거 없음"`은 **retrieve 0건**일 때만 나오는 센티널(`wiki/qa.py::no_hits_answer`)이고,
  실제로 훨씬 흔한 실패는 **무관한 5건이 검색되고 LLM이 자연어로 거절**하는 경우다
  ("…정보가 포함되어 있지 않습니다"). 이 거절이 grounded=True로 통과한다.
- 같은 함수가 바로 옆 줄에서 `gap = routed.wiki.gap`을 받아둔다. orthus에는 이미 결정론
  거절 감지기(`wiki/gap.py::detect_gap` → `insufficient_grounding`)가 있고 **캐시 경로는
  그걸 쓴다**(`router/cache.py::_cacheable` — `gap is None` 요구). decompose 경로만 안 쓴다.
  → 같은 불변식의 두 구현이 갈라진 drift.
- 그 gap 감지기 자체도 **부분문자열 마커 테이블**이라 말투에 샌다. 실측: `"포함되어 있지 않"`은
  잡지만 `"제공되지 않"`/`"명시되어 있지 않"`은 놓친다(영어 `not provided in`은 있는데 한국어
  대응이 없다). 말투는 **모델마다 다르므로**, 이 술어는 멀티모델 오케스트레이션에서
  모델 편향 장치가 된다.

여기서는 세 술어를 나란히 두고 같은 실행 기록에 적용해 **차이를 수치화**한다(프로덕션 무수정).
"""
from __future__ import annotations

from orthus.wiki.gap import _INSUFFICIENT_MARKERS, _looks_insufficient

# 현행 마커가 놓치는 자연스러운 한국어 거절 말투 (실측 관측분 + 대칭 보강)
MISSING_MARKERS: tuple[str, ...] = (
    "제공되지 않",       # "정보는 제공되지 않았습니다" — 영어 not provided in 의 한국어 대응 누락
    "제공하지 않",
    "명시되어 있지 않",   # "핵심 내용이 명시되어 있지 않습니다"
    "명시되지 않",
    "기재되어 있지 않",
    "드러나지 않",
    "답변을 제공할 수 없",
    "답변할 수 없",
    "설명되어 있지 않",
    "구체적인 정보가 부족",
)

EXTENDED_MARKERS: tuple[str, ...] = tuple(_INSUFFICIENT_MARKERS) + MISSING_MARKERS


def refusal_prod(text: str) -> bool:
    """프로덕션 `_run_leaf` 현행 술어 — 센티널 문자열 하나뿐."""
    return "근거 없음" in (text or "")


def refusal_gap(text: str) -> bool:
    """orthus가 이미 갖고 있는 결정론 감지기(gap.py). 캐시 경로가 쓰는 것."""
    return _looks_insufficient(text)


def refusal_extended(text: str) -> bool:
    """gap 마커 + 실측에서 새던 말투 보강."""
    low = (text or "").lower()
    return any(m in low for m in EXTENDED_MARKERS)


# ── 리프 grounded 술어 3종 (RoutedAnswer 기준) ──────────────────────────────


def grounded_prod(routed) -> bool:
    """현행 프로덕션(decompose.py:717-719) 그대로."""
    if routed is None:
        return False
    if routed.mode in {"wiki", "graph"} and routed.wiki is not None:
        body = routed.wiki.answer or ""
        return bool(body) and "근거 없음" not in body
    if routed.mode == "structured" and routed.structured is not None:
        return routed.structured.status != "rejected"
    return False


def grounded_gap_aware(routed) -> bool:
    """수정안 A (1줄): 이미 같은 스코프에 있는 `routed.wiki.gap`을 판정에 쓴다."""
    if routed is None:
        return False
    if routed.mode in {"wiki", "graph"} and routed.wiki is not None:
        body = routed.wiki.answer or ""
        return bool(body) and "근거 없음" not in body and routed.wiki.gap is None
    if routed.mode == "structured" and routed.structured is not None:
        return routed.structured.status != "rejected"
    return False


def grounded_extended(routed) -> bool:
    """수정안 B: gap-aware + 확장 마커(모델 말투 편향 축소)."""
    if routed is None:
        return False
    if routed.mode in {"wiki", "graph"} and routed.wiki is not None:
        body = routed.wiki.answer or ""
        if not body or routed.wiki.gap is not None:
            return False
        return not refusal_extended(body)
    if routed.mode == "structured" and routed.structured is not None:
        return routed.structured.status != "rejected"
    return False


PREDICATES = {
    "prod": grounded_prod,
    "gap_aware": grounded_gap_aware,
    "extended": grounded_extended,
}
