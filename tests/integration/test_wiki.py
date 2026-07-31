import json

from orthus.documents import save_editor_document
from orthus.models.adapters.mock import MockChat
from orthus.wiki import ask

# Scripted distill output so save_editor_document authors a real wiki page whose
# body carries "연차/15일", which retrieve then grounds the answer on.
_DISTILL_JSON = json.dumps(
    {
        "summary": "연차 휴가 정책 요약: 입사 1년차부터 15일 부여.",
        "key_concepts": ["연차 휴가"],
        "terminology": ["연차"],
        "open_questions": [],
        "claims": [
            {
                "claim": "연차 휴가는 입사 1년차부터 15일이 부여된다.",
                "evidence": "연차 휴가는 입사 1년차부터 15일이 부여됩니다.",
                "confidence": "high",
                "page": {"slug": "연차-휴가", "title": "연차 휴가"},
                "conflicting": [],
            }
        ],
    }
)


def test_ask_grounds_on_authored_wiki_page(user_id):
    # Distill chat returns valid JSON; the doc is authored into the wiki on save.
    save_editor_document(
        user_id,
        "휴가",
        [{}],
        "연차 휴가는 입사 1년차부터 15일이 부여됩니다.",
        chat_model=MockChat(default=_DISTILL_JSON),
    )

    out = ask(
        user_id,
        "연차 며칠 부여돼?",
        k=3,
        chat_model=MockChat(default="연차는 1년차부터 15일입니다."),
    )

    assert out.sources, "must return wiki grounding sources"
    src = out.sources[0]
    assert src.page_slug, "source carries its wiki page slug"
    assert "15일" in src.excerpt or "연차" in src.excerpt
    assert out.answer


def test_ask_with_empty_wiki_says_no_source(user_id):
    out = ask(user_id, "아무거나", chat_model=MockChat())
    assert out.sources == []
    assert "근거" in out.answer or "찾지" in out.answer
