"""E2 지식 질문 풀 확장 — wiki 페이지에서 **역방향**으로 답변 가능한 질문을 유도한다.

배경(F3, `analysis/e2-results.md`): t2 골든 30문항 중 실제 답변 가능한 것은 7개뿐이었다.
근본 원인은 코퍼스 구조다 — company wiki는 **2,618개의 아주 짧은 페이지**(본문 최대 382자,
대부분 100~200자)로 컴파일돼 있어 **서술·종합형 질문("주간 회고에서 반복된 개선 안건은?")은
구조적으로 답할 근거가 없다.** 답할 수 있는 것은 **정의·사실형**뿐이다.

그래서 질문을 상상해 적지 않고(TS9), **실제 페이지 본문에서 역유도**한다:
  page body → (LLM) 그 본문만으로 답할 수 있는 사실형 질문 + 정답 앵커
            → (검증) ask()를 2모델로 돌려 **둘 다 거절 아님** + 출처에 그 페이지 포함
            → 통과분만 골든에 남긴다

앵커는 **본문에서 그대로 뽑은 고유항**이라 모델 말버릇에 편향되지 않는다(E2 계획 §4.2).
거절 판정은 프로덕션 술어가 아니라 확장 술어를 쓴다(F1 — 프로덕션 술어는 거절을 100% 놓친다).

출력: golden/e2_knowledge.json  {items: [{id, q, page_slug, anchors[], verified_by[]}]}

실행:
  set -a; source ~/.orthus/nodes/company/node.env; set +a
  export ORTHUS_NODE=company ORTHUS_NODE_DB=orthus_company
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/e2_kb_gen.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

from e2_grounding import refusal_extended  # noqa: E402
from pool import build_pool  # noqa: E402

from orthus.wiki.qa import ask  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
GOLDEN = HERE / "golden"
RAW = HERE / "analysis" / "raw"
WIKI = Path(
    os.environ.get(
        "FUGU_WIKI_DIR", str(Path.home() / ".orthus/nodes/company/wiki-store/company/wiki")
    )
)

_GEN_SYSTEM = (
    "너는 사내 지식 평가셋 저자다. 주어진 위키 페이지 본문 **하나만 보고** 답할 수 있는 "
    "구체적인 사실형 질문 1개를 만든다.\n"
    "규칙:\n"
    "1. 본문에 실제로 적힌 **내용**만 묻는다. 본문에 없는 것(추세·이유·비교·종합)은 묻지 않는다.\n"
    "2. **문서 자체의 메타데이터(문서의 날짜/제목/slug)를 묻지 않는다.** 문서가 서술하는 "
    "**사실의 내용**을 물어야 한다.\n"
    "3. 질문만 읽어도 **어느 페이지 얘기인지 특정**되도록 고유명사를 포함한다(다른 페이지와 "
    "헷갈릴 수 있는 일반적 질문 금지).\n"
    "4. 질문에 정답을 그대로 노출하지 않는다.\n"
    "5. anchors = 정답에 **반드시** 등장해야 하는 핵심 항목 1~3개. **본문에 있는 표기 그대로** "
    "적는다. **고유명사·기술용어·구체 수치를 우선**하고, 연도만(2026)·월일만(06)·한두 글자 "
    "같은 **약한 토큰은 금지**한다.\n"
    '출력은 JSON 하나: {"q": "<질문>", "anchors": ["<항목>", ...]}'
)

# 템플릿 클론 페이지 — 서로 거의 같은 본문이라 질문이 중복되고 retrieve가 어느 것인지 못 가린다
_TEMPLATE_SLUG = re.compile(r"^(주간-회고-|\d{4}-w\d+|untitled$)")

# 약한 앵커: 순수 숫자(4자리 이하), 3자 미만, 흔한 연도/월
_WEAK = {"2026", "2025", "월", "일", "년"}


def _strong_anchor(a: str) -> bool:
    if len(a) < 3 or a in _WEAK:
        return False
    if re.fullmatch(r"\d{1,4}", a):  # "06", "22", "2026"
        return False
    return True


def load_pages(limit: int) -> list[tuple[str, str]]:
    """본문이 실한 페이지 상위 N (템플릿 클론 제외). (slug, body)"""
    rows: list[tuple[int, str, str]] = []
    for f in WIKI.rglob("*.md"):
        if _TEMPLATE_SLUG.match(f.stem):
            continue
        text = f.read_text("utf-8", errors="ignore")
        body = text.split("---", 2)[-1] if text.startswith("---") else text
        body = body.strip()
        if len(body) < 140:  # 너무 짧으면 사실형 질문도 못 만든다
            continue
        rows.append((len(body), f.stem, body))
    rows.sort(reverse=True)
    return [(slug, body) for _, slug, body in rows[:limit]]


def gen_question(chat, slug: str, body: str) -> dict | None:
    try:
        raw = chat.complete(_GEN_SYSTEM, f"[페이지 slug] {slug}\n\n[본문]\n{body}", json_only=True)
        d = json.loads(raw)
        q = str(d.get("q", "")).strip()
        anchors = [str(a).strip() for a in d.get("anchors", []) if str(a).strip()]
        if not q or not anchors:
            return None
        # 앵커는 본문에 실재해야 하고(LLM 지어냄 차단), 약한 토큰은 버린다
        anchors = [a for a in anchors if a in body and _strong_anchor(a)]
        if not anchors:
            return None
        return {"q": q, "anchors": anchors[:3]}
    except Exception:  # noqa: BLE001
        return None


def anchors_hit(answer: str, anchors: list[str]) -> int:
    return sum(1 for a in anchors if a.lower() in (answer or "").lower())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60, help="후보 페이지 수")
    ap.add_argument(
        "--keep-existing",
        action="store_true",
        help="기존 e2_knowledge.json의 검증 완료 항목을 보존하고, 그 페이지는 재생성하지 않는다 "
        "(E2 재측정용 풀 확장 — 이미 2모델 교차검증을 통과한 항목을 LLM 저작 비결정성으로 잃지 않는다)",
    )
    args = ap.parse_args()

    pages = load_pages(args.limit)
    pool = build_pool(["baseline", "solar"])
    gen = pool["baseline"]  # 질문 저작 = 기준선(중립). 검증은 2모델 교차.

    kept, rejected = [], []
    seen_q: set[str] = set()
    if args.keep_existing and (GOLDEN / "e2_knowledge.json").exists():
        prior = json.load(open(GOLDEN / "e2_knowledge.json", encoding="utf-8"))["items"]
        kept.extend(prior)
        seen_q.update(re.sub(r"\s+", "", it["q"]) for it in prior)
        done = {it["page_slug"] for it in prior}
        pages = [(s, b) for s, b in pages if s not in done]
        print(f"[E2 지식 풀] 기존 검증 완료 {len(prior)}개 보존 — 신규 후보 {len(pages)}개만 생성\n")

    print(f"[E2 지식 풀] 후보 페이지 {len(pages)} (본문 ≥140자, 템플릿 제외) → 생성 + 2모델 교차 검증\n")
    for i, (slug, body) in enumerate(pages, 1):
        item = gen_question(gen, slug, body)
        if item is None:
            rejected.append((slug, "gen_fail"))
            continue
        # 질문 중복 차단 — 같은 질문이 여러 페이지에서 나오면 retrieve가 어느 것인지 못 가린다
        key = re.sub(r"\s+", "", item["q"])
        if key in seen_q:
            print(f"  ⏭  [{i:2}/{len(pages)}] {slug[:34]:36} 중복 질문 — 스킵")
            rejected.append((slug, "dup_q"))
            continue
        seen_q.add(key)

        verdicts: dict[str, dict] = {}
        for m, chat in pool.items():
            try:
                a = ask(UID, item["q"], k=5, scope="company", chat_model=chat, learn=False, record_gaps=False)
                ans = a.answer or ""
                verdicts[m] = {
                    "refused": refusal_extended(ans),
                    "hits": anchors_hit(ans, item["anchors"]),
                    "src_ok": slug in [s.page_slug for s in a.sources],
                }
            except Exception:  # noqa: BLE001
                verdicts[m] = {"refused": True, "hits": 0, "src_ok": False}

        # 채택 조건: 두 모델 모두 거절 아님 + 앵커 1개 이상 적중 + 출처에 원 페이지 포함
        ok = all(
            (not v["refused"]) and v["hits"] >= 1 and v["src_ok"] for v in verdicts.values()
        )
        tag = "✅" if ok else "❌"
        detail = " ".join(
            f"{m}[{'REF' if v['refused'] else 'ans'} a{v['hits']}/{len(item['anchors'])}"
            f" {'src' if v['src_ok'] else 'nosrc'}]"
            for m, v in verdicts.items()
        )
        print(f"  {tag} [{i:2}/{len(pages)}] {slug[:34]:36} {detail}")
        (kept if ok else rejected).append(
            {"id": f"k-{len(kept) + 1:02}", "q": item["q"], "page_slug": slug,
             "anchors": item["anchors"], "verdicts": verdicts}
            if ok else (slug, "unanswerable")
        )

    GOLDEN.mkdir(exist_ok=True)
    out = GOLDEN / "e2_knowledge.json"
    json.dump(
        {"items": [{k: v for k, v in it.items() if k != "verdicts"} for it in kept]},
        open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2,
    )
    RAW.mkdir(parents=True, exist_ok=True)
    with open(RAW / "e2_kb_gen.jsonl", "w", encoding="utf-8") as f:
        for it in kept:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    print(f"\n== 채택 {len(kept)}/{len(pages)} → {out}")
    print(f"   (기존 t2 답변가능 7개 대비 {len(kept)}개 확보 — F3 병목 해소 여부 판정)")


if __name__ == "__main__":
    main()
