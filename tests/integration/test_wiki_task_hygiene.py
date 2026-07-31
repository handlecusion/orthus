from __future__ import annotations

from datetime import UTC, date, datetime

from orthus.schemas.canonical import WikiClaim, WikiSource
from orthus.wiki import consolidate, store
from orthus.wiki.task_hygiene import (
    claims_equivalent_for_conflict,
    filter_generated_open_questions,
    is_structured_row_tautology,
    open_question_hygiene_reason,
)


def _source(slug: str, questions: list[str]) -> WikiSource:
    return WikiSource(
        slug=slug,
        title=slug,
        source_type="corpus_doc",
        source_ref="doc",
        ingested_at=datetime(2026, 6, 17, tzinfo=UTC),
        summary="summary",
        key_concepts=[],
        key_claims=[],
        terminology=[],
        related_pages=[],
        open_questions=questions,
    )


def test_generated_open_questions_drop_noise_dedupe_and_cap():
    questions = [
        "What is the visual content of the image 001.jpg?",
        "담당자는 누구인가?",
        "What durable decision should be recorded?",
        "What durable decision should be recorded?",
        "Which rollout risk remains unresolved?",
        "What user-facing boundary changed?",
        "What fourth question should be capped?",
    ]

    out = filter_generated_open_questions("src-notion-orthus-roadmap-2026-06-15", questions)

    assert out == [
        "What durable decision should be recorded?",
        "Which rollout risk remains unresolved?",
        "What user-facing boundary changed?",
    ]


def test_ai_session_open_questions_are_transient():
    reason = open_question_hygiene_reason(
        "How will the introduction of new agents affect performance?",
        source_slug="src-claude-users-ys-code-orthus-ai-session-jsonl",
    )

    assert reason == "ai_session_transient"
    assert (
        filter_generated_open_questions(
            "src-claude-users-ys-code-orthus-ai-session-jsonl",
            ["What architecture decision remains?"],
        )
        == []
    )


def test_consolidate_filters_generated_open_question_tasks(user_id, tmp_path):
    source = _source(
        "src-notion-orthus-roadmap-2026-06-15",
        [
            "What is the visual content of the image 001.jpg?",
            "What durable decision should be recorded?",
            "Which rollout risk remains unresolved?",
            "What user-facing boundary changed?",
            "What fourth question should be capped?",
        ],
    )
    source = source.model_copy(
        update={
            "related_pages": ["orthus-roadmap", "agent-work"],
            "key_claims": ["orthus-roadmap-abcd1234"],
        }
    )

    _pages, tasks = consolidate(source, [], user_id=user_id, root=tmp_path, project="company")

    assert len(tasks) == 3
    assert [task.description for task in tasks] == [
        "What durable decision should be recorded?",
        "Which rollout risk remains unresolved?",
        "What user-facing boundary changed?",
    ]
    assert tasks[0].related == [
        "src-notion-orthus-roadmap-2026-06-15",
        "orthus-roadmap",
        "agent-work",
        "orthus-roadmap-abcd1234",
    ]
    assert len(store.list_slugs("task", root=tmp_path)) == 3


def test_equivalent_claim_text_does_not_create_conflict(user_id, tmp_path):
    claim_slug = "assistant-mode-abcd1234"
    store.write_claim(
        WikiClaim(
            slug=claim_slug,
            claim="The assistant indicated that 'Caveman mode' is active.",
            supporting=[],
            conflicting=[],
            confidence="medium",
            last_reviewed=date(2026, 6, 1),
            related_pages=["assistant-mode"],
            evidence="existing",
        ),
        user_id=user_id,
        root=tmp_path,
        scope="company",
        project="company",
    )
    incoming = WikiClaim(
        slug=claim_slug,
        claim="The assistant indicated that \\'caveman mode\\' is active.",
        supporting=["src-x"],
        conflicting=[],
        confidence="medium",
        last_reviewed=date(2026, 6, 17),
        related_pages=["assistant-mode"],
        evidence="incoming",
    )

    _pages, tasks = consolidate(
        _source("src-x", []),
        [incoming],
        user_id=user_id,
        root=tmp_path,
        project="company",
    )

    assert claims_equivalent_for_conflict(
        "The assistant indicated that 'Caveman mode' is active.",
        "The assistant indicated that \\'caveman mode\\' is active.",
    )
    assert tasks == []
    assert not store.list_slugs("task", root=tmp_path)


def test_different_claim_text_still_creates_conflict(user_id, tmp_path):
    claim_slug = "vacation-abcd1234"
    store.write_claim(
        WikiClaim(
            slug=claim_slug,
            claim="연차는 15일이다.",
            supporting=[],
            conflicting=[],
            confidence="medium",
            last_reviewed=date(2026, 6, 1),
            related_pages=["vacation"],
            evidence="existing",
        ),
        user_id=user_id,
        root=tmp_path,
        scope="company",
        project="company",
    )
    incoming = WikiClaim(
        slug=claim_slug,
        claim="연차는 20일이다.",
        supporting=["src-x"],
        conflicting=[],
        confidence="medium",
        last_reviewed=date(2026, 6, 17),
        related_pages=["vacation"],
        evidence="incoming",
    )

    _pages, tasks = consolidate(
        _source("src-x", []),
        [incoming],
        user_id=user_id,
        root=tmp_path,
        project="company",
    )

    assert len(tasks) == 1
    assert tasks[0].kind == "conflict"


def test_transactional_source_buckets_drop_open_questions():
    from orthus.wiki.task_hygiene import source_bucket

    cases = {
        "src-주간-회고-아틀라스-2026-06-15": "weekly_retro_transient",
        "src-slack-c123-1770000000-000000": "slack_fragment_transient",
        "src-일정-홈즈데모-본사-미팅": "schedule_metadata",
        "src-mail-orthus-login-link-46d95b61": "mail_metadata",
    }
    for slug, reason in cases.items():
        assert source_bucket(slug) in {
            "weekly_retro",
            "slack",
            "schedule",
            "mail",
        }, slug
        assert (
            open_question_hygiene_reason("해당 주에 어떤 계획이 있었는가?", source_slug=slug)
            == reason
        )
        # whole-bucket suppression regardless of question text
        assert filter_generated_open_questions(slug, ["임의의 질문은?", "another question?"]) == []


def test_substantive_source_open_questions_survive():
    # A non-transactional source (e.g. notion/corpus doc) still produces tasks.
    out = filter_generated_open_questions(
        "src-atlas-정산-정책-문서",
        ["정산 마감일은 매월 며칠인가?"],
    )
    assert out == ["정산 마감일은 매월 며칠인가?"]


# ── structured-row source suppression (A) ─────────────────────────────────


def test_is_structured_row_source_db_row():
    from orthus.wiki.task_hygiene import is_structured_row_source

    # Notion DB row: source_db_name set + real source value
    assert is_structured_row_source("notion", "미팅 DB") is True
    assert is_structured_row_source("notion", "아틀라스 현황") is True


def test_is_structured_row_source_narrative_page():
    from orthus.wiki.task_hygiene import is_structured_row_source

    # Narrative Notion page: source_db_name is None
    assert is_structured_row_source("notion", None) is False
    # Editor doc
    assert is_structured_row_source("editor", None) is False
    # Slack fragment
    assert is_structured_row_source("slack", None) is False


def test_is_structured_row_source_dashboard_logs():
    from orthus.wiki.task_hygiene import is_structured_row_source

    assert is_structured_row_source("dashboard_meeting", None) is True
    assert is_structured_row_source("dashboard_weekly", None) is True
    assert is_structured_row_source("dashboard_calendar", None) is True


def test_is_structured_row_source_empty_db_name_not_structured():
    from orthus.wiki.task_hygiene import is_structured_row_source

    # Empty source_db_name means narrative page even if source is set
    assert is_structured_row_source("notion", "") is False
    assert is_structured_row_source("notion", "   ") is False


# ── source_excerpt stamped on open_question tasks (B-data) ────────────────


def test_open_question_task_carries_source_excerpt(user_id, tmp_path):
    """consolidate sets source_excerpt from source.summary on open_question tasks."""
    source = _source("src-atlas-policy-doc", ["정산 마감일은 며칠인가?"])
    source = source.model_copy(
        update={"summary": "이 문서는 정산 정책을 설명합니다. 마감일은 매월 말일이다."}
    )

    _pages, tasks = consolidate(source, [], user_id=user_id, root=tmp_path, project="company")

    assert len(tasks) == 1
    assert tasks[0].kind == "open_question"
    assert tasks[0].source_excerpt == "이 문서는 정산 정책을 설명합니다. 마감일은 매월 말일이다."


def test_open_question_task_source_excerpt_truncated_at_500(user_id, tmp_path):
    """source_excerpt is capped at 500 chars."""
    long_summary = "x" * 600
    source = _source("src-atlas-long-doc", ["질문은?"])
    source = source.model_copy(update={"summary": long_summary})

    _pages, tasks = consolidate(source, [], user_id=user_id, root=tmp_path, project="company")

    assert len(tasks) == 1
    assert tasks[0].source_excerpt == "x" * 500


def test_open_question_task_source_excerpt_none_when_no_summary(user_id, tmp_path):
    """source_excerpt is None when source.summary is empty."""
    source = _source("src-atlas-empty", ["질문은?"])
    source = source.model_copy(update={"summary": ""})

    _pages, tasks = consolidate(source, [], user_id=user_id, root=tmp_path, project="company")

    assert len(tasks) == 1
    assert tasks[0].source_excerpt is None


def test_source_excerpt_round_trips_through_store(user_id, tmp_path):
    """source_excerpt survives write_task → load_task round-trip."""
    source = _source("src-roundtrip", ["질문?"])
    source = source.model_copy(update={"summary": "요약 텍스트입니다."})

    _pages, tasks = consolidate(source, [], user_id=user_id, root=tmp_path, project="company")
    assert tasks[0].source_excerpt == "요약 텍스트입니다."

    # Reload from disk
    loaded = store.load_task(tasks[0].slug, root=tmp_path)
    assert loaded is not None
    assert loaded.source_excerpt == "요약 텍스트입니다."


def test_legacy_task_without_source_excerpt_loads_as_none(tmp_path):
    """A task file without source_excerpt frontmatter loads with source_excerpt=None."""

    # Write a legacy task file with no source_excerpt line
    legacy_md = (
        "---\n"
        'scope: "company"\n'
        'project: "atlas"\n'
        'slug: "legacy-task-abc12345"\n'
        'kind: "open_question"\n'
        'related: ["src-old-doc"]\n'
        'created_at: "2026-01-01T00:00:00+00:00"\n'
        "resolved: false\n"
        "incoming_claim: null\n"
        "resolution_decision: null\n"
        "resolution_note: null\n"
        "resolution_resolved_by: null\n"
        "resolution_resolved_at: null\n"
        "resolution_produced_claim_slugs: []\n"
        "---\n\n"
        "# Task: legacy-task-abc12345\n\n"
        "## Description\n\n"
        "옛날 질문은?\n"
    )
    task_dir = tmp_path / "company" / "tasks"
    task_dir.mkdir(parents=True)
    (task_dir / "legacy-task-abc12345.md").write_text(legacy_md)

    loaded = store.load_task("legacy-task-abc12345", root=tmp_path, scope="company")
    assert loaded is not None
    assert loaded.source_excerpt is None


# ── structured-row claim tautology suppression ─────────────────────────────
# docs/agent-chat-answer-quality.md "Notion 수집 손실 4종" 잔여 항목: Notion DB row
# 속성 dump("**속성**: 값")를 distill하면 자기참조/무정보 단일-형용사 claim이 생긴다.
# Caller (distill.py) gates `is_structured_row_tautology` on
# `is_structured_row_source` — these unit tests exercise the classifier itself;
# the distill.py wiring boundary is covered by
# test_distill_document_suppresses_structured_row_tautology below.


def test_self_name_tautology_korean_is_suppressed():
    assert is_structured_row_tautology("Wan2.2의 이름은 Wan2.2이다.") is True
    assert is_structured_row_tautology("Wan2.2의 제목은 Wan2.2다") is True
    assert is_structured_row_tautology("서비스 기획서의 명칭은 서비스 기획서입니다.") is True


def test_self_name_tautology_english_is_suppressed():
    assert is_structured_row_tautology("The name of Wan2.2 is Wan2.2.") is True
    assert is_structured_row_tautology("Wan2.2's name is Wan2.2.") is True
    assert is_structured_row_tautology("The title of Wan2.2 is Wan2.2") is True


def test_bare_filler_adjective_is_suppressed():
    assert is_structured_row_tautology("우선순위는 높음") is True
    assert is_structured_row_tautology("우선순위는 높음.") is True
    assert is_structured_row_tautology("중요도가 보통이다.") is True
    assert is_structured_row_tautology("Priority is high.") is True


def test_different_subject_and_value_name_claim_is_kept():
    # Not self-reference — a real named relationship, must survive.
    assert is_structured_row_tautology("Wan2.2의 이름은 완달용 프로젝트이다.") is False
    assert is_structured_row_tautology("The name of the project is Wan2.2.") is False


def test_informative_single_value_claims_are_kept():
    # Named values, statuses outside the closed filler set, numbers, and dates
    # are real information and must never be suppressed.
    assert is_structured_row_tautology("담당자는 김철수이다.") is False
    assert is_structured_row_tautology("상태는 완료다.") is False
    assert is_structured_row_tautology("가격은 100000이다.") is False
    assert is_structured_row_tautology("마감일은 2026-07-01이다.") is False


def test_elaborated_claim_with_filler_word_is_kept():
    # A filler adjective plus a real additional clause is no longer "bare" —
    # over-suppression guard.
    assert is_structured_row_tautology("우선순위는 높음, 마감이 임박했기 때문이다.") is False
    assert is_structured_row_tautology("Wan2.2는 텍스트-투-비디오 생성 모델이다.") is False


def test_is_structured_row_tautology_blank_is_false():
    assert is_structured_row_tautology("") is False
    assert is_structured_row_tautology("   ") is False
