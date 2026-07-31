"""골든셋 QA 평가 하네스 — /ask wiki 답변 경로의 end-to-end 채점.

`eval_retrieval`(retrieve-only hit rate)과 달리 실제 답변 경로를 태운다:
  - 단일턴: `orthus.wiki.qa.ask(..., learn=False, record_gaps=False)`
    (learn/record_gaps=False 필수 — 평가 실행이 wiki/personal 레이어를 오염시키지 않게)
  - followup(history 보유): `orthus.router.answer(..., history=..., allow_cache=False,
    learn=False, record_gaps=False)`

채점 2축:
  - 결정론(항상): expected_slugs any-hit + recall — `WikiAnswer.sources[].page_slug`
    substring 매칭(eval_retrieval과 동일 관례). negative(`expect_gap=True`)는
    `WikiAnswer.gap`이 참이거나 답변에 부재 표지가 있으면 정답.
  - LLM-judge(옵트인 `--judge`): 정확성/근거일치/무환각/무카탈로그 4항목 1-5 루브릭.

케이스 파일: JSONL(줄당 1 object) 또는 JSON list. `evals/golden/company_chat_v1.jsonl` 참조.

실행:
  make node-golden-eval NODE=company ARGS="--cases evals/golden/company_chat_v1.jsonl --out out.json"
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

# 로컬 company 스냅샷의 실사용자 (개인 스코프 grounding 포함 'all' 평가용).
DEFAULT_USER_ID = "a656035c-c5a8-4874-9f90-80cdb00783d3"

# negative 케이스 부재 표지 — gap row가 안 잡혀도 모델이 "모른다"고 답하면 정답.
# 좁게 유지한다(일상 답변에 흔한 "없습니다" 단독은 오탐이라 제외).
ABSENCE_MARKERS: tuple[str, ...] = (
    "찾지 못했",
    "근거 없음",
    "근거가 없",
    "정보가 없",
    "정보는 없",
    "정보를 찾을 수 없",
    "내용이 없",
    "확인할 수 없",
    "확인되지 않",
    "포함되어 있지 않",
    "존재하지 않",
    "다루지 않",
    "언급되지 않",
    "나와 있지 않",
)

# --- coverage 기반 pass 기준 (넓은 질문 전용) -------------------------------------
#
# 넓은 질문의 핵심 품질은 "여러 페이지를 얼마나 잘 묶는가"인데 any-hit(하나라도 맞으면
# 통과)로는 이게 안 잡혀 포화된다. broad 태그 케이스는 covered/total(=recall)이 임계를
# 넘어야 통과하도록 pass 기준을 바꾼다. specific/negative/followup은 기존 any-hit(negative는
# gap)을 그대로 유지한다. 전부 결정론 — DB/LLM 불필요.
DEFAULT_COVERAGE_THRESHOLD = 0.5  # covered_expected / total_expected >= 이 값이면 broad 통과
COVERAGE_PASS_TAGS: tuple[str, ...] = ("broad",)  # coverage 기준을 적용할 태그


@dataclass(frozen=True)
class QAEvalCase:
    """골든셋 1문항. `tags`는 specific/broad/followup/negative 서브셋 집계 키."""

    case_id: str
    question: str
    expected_slugs: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    # followup 전용: [{"role": "user"|"assistant", "text": ...}, ...] 2턴 히스토리.
    history: tuple[dict, ...] = ()
    expect_gap: bool = False

    def __post_init__(self) -> None:  # JSON list → tuple 정규화
        object.__setattr__(self, "expected_slugs", tuple(self.expected_slugs))
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "history", tuple(self.history or ()))


# --- 결정론 채점 (순수 함수 — DB/LLM 불필요, 단위 테스트 대상) ---------------------


def match_slug(expected: str, got_slugs: list[str]) -> int | None:
    """expected slug이 결과 slug 목록에 substring으로 존재하면 1-based rank."""
    for i, slug in enumerate(got_slugs, start=1):
        if expected in slug:
            return i
    return None


def score_slugs(expected_slugs: tuple[str, ...], got_slugs: list[str]) -> dict:
    """any-hit + recall. recall = expected 중 결과에 잡힌 비율."""
    matched: dict[str, int] = {}
    for expected in expected_slugs:
        rank = match_slug(expected, got_slugs)
        if rank is not None:
            matched[expected] = rank
    n = len(expected_slugs)
    return {
        "any_hit": bool(matched),
        "recall": (len(matched) / n) if n else 0.0,
        "matched": matched,
        "first_rank": min(matched.values()) if matched else None,
    }


def is_absence_answer(answer_text: str) -> bool:
    return any(marker in answer_text for marker in ABSENCE_MARKERS)


def _pass_mode(case: QAEvalCase, coverage_pass_tags: tuple[str, ...]) -> str:
    """케이스가 어떤 pass 기준을 쓰는지: gap(부재) / coverage(넓은 질문) / any_hit."""
    if case.expect_gap:
        return "gap"
    if any(t in coverage_pass_tags for t in case.tags):
        return "coverage"
    return "any_hit"


def score_case(
    case: QAEvalCase,
    *,
    answer_text: str,
    got_slugs: list[str],
    gap_detected: bool,
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
    coverage_pass_tags: tuple[str, ...] = COVERAGE_PASS_TAGS,
) -> dict:
    """케이스 1건 결정론 채점 row (실행 메타 제외).

    pass 기준은 태그에 따라 갈린다:
      - gap: `expect_gap` 케이스 — gap 감지 또는 부재 표지면 통과 (기존 유지)
      - coverage: broad 등 넓은 질문 — recall(covered/total) >= `coverage_threshold`면 통과
      - any_hit: 그 외 — 하나라도 맞으면 통과 (기존 유지)
    any_hit/recall 지표는 항상 함께 출력해 하위호환을 유지한다."""
    slug_score = score_slugs(case.expected_slugs, got_slugs)
    coverage = slug_score["recall"]  # covered_expected / total_expected
    mode = _pass_mode(case, coverage_pass_tags)
    coverage_pass = coverage >= coverage_threshold
    if mode == "gap":
        gap_ok = gap_detected or is_absence_answer(answer_text)
        passed = gap_ok
    elif mode == "coverage":
        gap_ok = None
        passed = coverage_pass
    else:
        gap_ok = None
        passed = slug_score["any_hit"]
    return {
        "case_id": case.case_id,
        "tags": list(case.tags),
        "expect_gap": case.expect_gap,
        "expected_slugs": list(case.expected_slugs),
        "got_slugs": got_slugs,
        "gap_detected": gap_detected,
        "gap_ok": gap_ok,
        "passed": passed,
        "pass_mode": mode,
        "coverage": coverage,
        "coverage_threshold": coverage_threshold,
        "coverage_pass": coverage_pass,
        **{k: slug_score[k] for k in ("any_hit", "recall", "matched", "first_rank")},
    }


def summarize_judge(rows: list[dict]) -> dict:
    """judge 채점 시도/성공/폐기 집계 (순수 함수 — DB/LLM 미경유, `row["judge"]` dict만 사용).

    `row["judge"]`가 없는 row(judge 미실행)는 attempted에서 제외한다. 있으면 `error`
    필드 유무로 성공/폐기를 가르고, 폐기 사유(`parse`/`call:<ExcType>`)별 개수를 남겨
    dropped 케이스가 summary에서 조용히 사라지지 않게 한다."""
    attempted = [r for r in rows if r.get("judge") is not None]
    dropped = [r for r in attempted if r["judge"].get("error")]
    reasons: dict[str, int] = {}
    for r in dropped:
        reason = r["judge"]["error"]
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "judge_attempted": len(attempted),
        "judge_scored": len(attempted) - len(dropped),
        "judge_dropped": len(dropped),
        "judge_dropped_reasons": reasons,
    }


def summarize(rows: list[dict]) -> dict:
    """전체 + tag별 서브셋 집계."""

    def _agg(subset: list[dict]) -> dict:
        n = len(subset) or 1
        scored = [r for r in subset if not r["expect_gap"]]
        sn = len(scored) or 1
        # coverage-mode 케이스만 따로 집계 — broad "여러 페이지 묶기" 품질을 포화 없이 본다.
        cov = [r for r in subset if r.get("pass_mode") == "coverage"]
        cn = len(cov) or 1
        return {
            "cases": len(subset),
            "passed": sum(1 for r in subset if r["passed"]),
            "pass_rate": sum(1 for r in subset if r["passed"]) / n,
            "any_hit_rate": sum(1 for r in scored if r["any_hit"]) / sn,
            "mean_recall": sum(r["recall"] for r in scored) / sn,
            # 신규 coverage 지표 (하위호환 — 필드 추가만). coverage-mode 케이스가 없으면 0.
            "coverage_cases": len(cov),
            "coverage_pass": sum(1 for r in cov if r["passed"]),
            "coverage_pass_rate": (sum(1 for r in cov if r["passed"]) / cn) if cov else 0.0,
            "mean_coverage": (sum(r["coverage"] for r in cov) / cn) if cov else 0.0,
            # judge 폐기 투명성 — judge 미실행 실행에서는 전부 0.
            **summarize_judge(subset),
        }

    tags = sorted({t for r in rows for t in r["tags"]})
    return {
        **_agg(rows),
        "by_tag": {tag: _agg([r for r in rows if tag in r["tags"]]) for tag in tags},
    }


def _load_cases(path: Path) -> list[QAEvalCase]:
    """JSONL(줄당 1 object) 또는 JSON list 로딩."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        items = json.loads(text)
        if not isinstance(items, list):
            raise ValueError("case file must contain a JSON list or JSONL lines")
    return [QAEvalCase(**item) for item in items]


# --- LLM judge (옵트인) ----------------------------------------------------------

_JUDGE_SYSTEM = (
    "너는 회사 위키 QA 채점자다. 질문·근거 발췌·답변을 보고 아래 4항목을 0-5로 채점해 "
    "JSON만 출력한다. 다른 텍스트 금지. 0은 유효한 최저점이다(예: 완전히 실패했거나 "
    "환각이 확실하면 0을 쓴다).\n"
    '{"accuracy": n, "groundedness": n, "no_hallucination": n, "no_catalog": n}\n'
    "- accuracy: 답변이 질문에 실제로 답했는가\n"
    "- groundedness: 답변 내용이 제공된 근거 발췌와 일치하는가\n"
    "- no_hallucination: 근거에 없는 사실을 지어내지 않았는가 (지어냈으면 0)\n"
    '- no_catalog: "문서 [1] 참조" 식 출처 나열로 때우지 않고 내용으로 답했는가'
)


def _judge_prompt(question: str, answer: str, excerpts: list[str]) -> str:
    joined = "\n\n".join(f"[{i + 1}] {e}" for i, e in enumerate(excerpts))
    return f"질문: {question}\n\n근거 발췌:\n{joined}\n\n답변:\n{answer}\n\nJSON 채점:"


#: 루브릭 4항목의 유효 점수 범위. 0은 유효한 최저점이다 — 특히 no_hallucination=0은
#: "환각을 실제로 발견했다"는 신호이므로, 이 값을 범위 밖으로 취급해 케이스를 통째로
#: 폐기하면 negative 표본이 조용히 줄어들고 점수가 낙관적으로 왜곡된다(과거 버그: 1-5).
JUDGE_SCORE_MIN = 0
JUDGE_SCORE_MAX = 5
JUDGE_KEYS: tuple[str, ...] = ("accuracy", "groundedness", "no_hallucination", "no_catalog")


def parse_judge_output(raw: str) -> dict | None:
    """judge 응답에서 4항목 int 파싱.

    유효 범위는 `JUDGE_SCORE_MIN`-`JUDGE_SCORE_MAX`(0-5, 0 포함)다. JSON을 아예 못 찾거나
    파싱할 수 없는 경우, 또는 키 누락/비정수/범위 밖(6 이상·음수) 값이 하나라도 있으면
    전체를 폐기하고 None을 반환한다 — 호출부(`run_judge`)가 그 사유를 `error` 필드에
    남겨 dropped count로 집계할 수 있게 한다(침묵 폐기 방지)."""
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        data = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return None
    out = {}
    for key in JUDGE_KEYS:
        try:
            # bool은 int의 subclass라 isinstance(True, int)가 True다 — 루브릭 점수로
            # False/True가 섞여 들어오는 걸 막는다.
            if isinstance(data[key], bool):
                raise TypeError("bool is not a valid judge score")
            value = int(data[key])
        except (KeyError, TypeError, ValueError):
            return None
        if not JUDGE_SCORE_MIN <= value <= JUDGE_SCORE_MAX:
            return None
        out[key] = value
    return out


def run_judge(chat, question: str, answer: str, excerpts: list[str]) -> dict | None:
    try:
        raw = chat.complete(_JUDGE_SYSTEM, _judge_prompt(question, answer, excerpts))
    except Exception as exc:  # noqa: BLE001 — judge 실패는 케이스 skip으로만 기록
        return {"error": f"call:{type(exc).__name__}"}
    parsed = parse_judge_output(raw)
    if parsed is None:
        return {"error": "parse", "raw": raw[:500]}
    return parsed


# --- 실행 경로 --------------------------------------------------------------------


def _run_case(case: QAEvalCase, *, user_id: UUID, scope: str, k: int) -> tuple[str, list, object]:
    """(answer_text, sources, gap) — followup은 router.answer, 그 외 qa.ask."""
    if case.history:
        from orthus.router import answer as route_answer
        from orthus.schemas.canonical import ChatTurn

        turns = [ChatTurn(role=t["role"], text=t["text"]) for t in case.history]
        routed = route_answer(
            user_id,
            case.question,
            scope=scope,
            history=turns,
            allow_cache=False,
            learn=False,
            record_gaps=False,
        )
        wiki = routed.wiki
        if wiki is None:
            return "", [], None
        return wiki.answer, wiki.sources, wiki.gap

    from orthus.wiki.qa import ask

    result = ask(
        user_id,
        case.question,
        k=k,
        scope=scope,
        learn=False,
        record_gaps=False,
    )
    return result.answer, result.sources, result.gap


def evaluate_qa(
    cases: list[QAEvalCase],
    *,
    user_id: UUID,
    scope: str = "all",
    k: int = 5,
    judge: bool = False,
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
    coverage_pass_tags: tuple[str, ...] = COVERAGE_PASS_TAGS,
) -> dict:
    judge_chat = None
    if judge:
        from orthus.models.registry import get_chat_model

        judge_chat = get_chat_model()

    rows = []
    for case in cases:
        try:
            answer_text, sources, gap = _run_case(case, user_id=user_id, scope=scope, k=k)
            error = None
        except Exception as exc:  # noqa: BLE001 — 케이스 실패는 row로 기록하고 계속
            answer_text, sources, gap = "", [], None
            error = f"{type(exc).__name__}: {exc}"
        got_slugs = [s.page_slug for s in sources]
        row = score_case(
            case,
            answer_text=answer_text,
            got_slugs=got_slugs,
            gap_detected=gap is not None,
            coverage_threshold=coverage_threshold,
            coverage_pass_tags=coverage_pass_tags,
        )
        row["question"] = case.question
        row["answer"] = answer_text
        row["error"] = error
        if error is not None:
            row["passed"] = False
        if judge_chat is not None and answer_text:
            row["judge"] = run_judge(
                judge_chat,
                case.question,
                answer_text,
                [s.excerpt for s in sources],
            )
        rows.append(row)

    summary = summarize(rows)
    summary.update(
        {
            "scope": scope,
            "k": k,
            "judge": judge,
            "coverage_threshold": coverage_threshold,
            "coverage_pass_tags": list(coverage_pass_tags),
        }
    )
    return {"summary": summary, "cases": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description="골든셋 QA 평가 (wiki 답변 경로 end-to-end).")
    parser.add_argument("--cases", type=Path, required=True, help="JSONL/JSON case file")
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--scope", default="all")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--judge", action="store_true", help="LLM judge 4항목 루브릭 추가")
    parser.add_argument(
        "--coverage-threshold",
        type=float,
        default=DEFAULT_COVERAGE_THRESHOLD,
        help=f"broad coverage pass 임계 covered/total (default {DEFAULT_COVERAGE_THRESHOLD})",
    )
    parser.add_argument("--out", type=Path, default=None, help="write detailed JSON result")
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args()

    cases = _load_cases(args.cases)
    result = evaluate_qa(
        cases,
        user_id=UUID(args.user_id),
        scope=args.scope,
        k=args.k,
        judge=args.judge,
        coverage_threshold=args.coverage_threshold,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        summary = result["summary"]
        print(
            "qa eval complete: "
            f"cases={summary['cases']} pass_rate={summary['pass_rate']:.3f} "
            f"any_hit={summary['any_hit_rate']:.3f} mean_recall={summary['mean_recall']:.3f}"
        )
        if summary["judge_attempted"]:
            print(
                "  judge: "
                f"attempted={summary['judge_attempted']} scored={summary['judge_scored']} "
                f"dropped={summary['judge_dropped']} reasons={summary['judge_dropped_reasons']}"
            )
        for tag, agg in summary["by_tag"].items():
            extra = ""
            if agg["coverage_cases"]:
                extra = (
                    f" coverage_pass={agg['coverage_pass_rate']:.3f} "
                    f"mean_coverage={agg['mean_coverage']:.3f}"
                )
            print(f"  [{tag}] cases={agg['cases']} pass_rate={agg['pass_rate']:.3f}{extra}")
        for row in result["cases"]:
            status = "PASS" if row["passed"] else "MISS"
            detail = row["error"] or f"expected={row['expected_slugs']} got={row['got_slugs'][:3]}"
            print(f"{status} {row['case_id']}: {detail}")


if __name__ == "__main__":
    main()
