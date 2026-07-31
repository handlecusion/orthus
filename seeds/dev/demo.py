"""End-to-end console demo (`make demo`). No external API calls (mock adapters).

Demonstrates the post-P2 router pipeline:
  1. wiki path  — natural-language Q&A grounded on compiled wiki pages
  2. structured path — NL → SQL through the validation gate (정상 SELECT)
  3. structured path — write attempt rejected by the gate (DELETE → reject)

Run after `make seed` + `make migrate`. Uses MockChat so no LLM/embedding keys
required.
"""

from __future__ import annotations

from orthus.models.adapters.mock import MockChat
from orthus.router import answer
from orthus.structured.query import query_structured
from orthus.wiki import ask
from seeds.dev.seed import DEMO_USER_ID, seed


def _rule(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * 8}")


def main() -> None:
    seed()

    _rule("wiki path — direct ask()")
    out = ask(DEMO_USER_ID, "연차 휴가는 며칠 부여돼?", k=3)
    print("Q:", out.question)
    print("A:", out.answer)
    print("sources:", [f"{h.score:.3f} {h.content[:30]}…" for h in out.sources])

    _rule("router — wiki branch")
    chat_route_wiki = MockChat(rules=[("", '{"route": "wiki"}')])
    r = answer(
        DEMO_USER_ID, "휴가 며칠?", scope="company", project="company", chat_model=chat_route_wiki
    )
    print("mode:", r.mode, " answer:", (r.wiki.answer if r.wiki else "")[:80])

    _rule("structured — 정상 SELECT (게이트 통과)")
    chat_ok = MockChat(rules=[("행", '{"sql": "SELECT count(*) AS n FROM notion_rows"}')])
    sr = query_structured(
        DEMO_USER_ID, "notion 행 몇 개야?", scope="company", project="company", chat_model=chat_ok
    )
    print("status:", sr.status, " sql:", sr.compiled.sql if sr.compiled else None)
    print(
        "validation.passed:", sr.validation.passed, " injected_limit:", sr.validation.injected_limit
    )
    print("rows:", sr.rows[:3] if sr.rows else None)

    _rule("structured — write 시도 (게이트 거부)")
    chat_bad = MockChat(rules=[("삭제", '{"sql": "DELETE FROM notion_rows"}')])
    br = query_structured(
        DEMO_USER_ID, "다 삭제해", scope="company", project="company", chat_model=chat_bad
    )
    print("status:", br.status, "(NOT executed)")
    print("sql:", br.compiled.sql if br.compiled else None)
    print("rejected_reason:", br.validation.rejected_reason)


if __name__ == "__main__":
    main()
