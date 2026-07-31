"""Codec round-trip for the markdown SoR <-> canonical model conversion.
No DB needed — exercises only orthus.wiki.store's pure to/from-markdown helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime

from orthus.schemas.canonical import WikiClaim, WikiPage, WikiSource, WikiTask, WikiTaskResolution
from orthus.wiki import store


def test_source_round_trip():
    m = WikiSource(
        slug="src-vacation-policy",
        title="휴가 정책: 2026 개정판",  # title with colon
        source_type="corpus_doc",
        source_ref="11111111-1111-1111-1111-111111111111",
        ingested_at=datetime(2026, 5, 25, 9, 30, tzinfo=UTC),
        summary='첫 줄 요약.\n\n두 번째 문단: 콜론 포함, "따옴표" 포함.',
        key_concepts=["연차", "리프레시 휴가: 5년차"],
        key_claims=["vacation-policy-abcd1234"],
        terminology=["연차: annual leave"],
        related_pages=["vacation-policy"],
        open_questions=["반차 정책은?"],
        confidence_notes="원본 문서 v2 기준.\n불확실: 시행일.",
    )
    text = store.to_markdown["source"](m)
    back = store.from_markdown["source"](text)
    assert back == m


def test_claim_round_trip():
    m = WikiClaim(
        slug="vacation-policy-abcd1234",
        claim="연차는 입사 1년차부터 15일 부여된다: 근로기준법 기준.",
        supporting=["src-vacation-policy"],
        conflicting=["vacation-policy-deadbeef"],
        confidence="high",
        last_reviewed=date(2026, 5, 25),
        related_pages=["vacation-policy"],
        evidence='문서 3절: "입사 1년차 15일".\n둘째 줄 근거.',
        notes="재검토 필요: 2027 개정 예정.",
    )
    text = store.to_markdown["claim"](m)
    back = store.from_markdown["claim"](text)
    assert back == m


def test_claim_round_trip_none_notes():
    m = WikiClaim(
        slug="x-00000000",
        claim="단일 줄 주장.",
        supporting=["src-x"],
        conflicting=[],
        confidence="low",
        last_reviewed=date(2026, 1, 1),
        related_pages=["x"],
        evidence="근거.",
        notes=None,
    )
    back = store.from_markdown["claim"](store.to_markdown["claim"](m))
    assert back == m
    assert back.notes is None


def test_page_round_trip():
    m = WikiPage(
        slug="vacation-policy",
        title="휴가 정책",
        definition="연차/리프레시/반차를 포괄하는 사내 휴가 제도.",
        overview="개요 문단 1: 콜론 포함.\n\n개요 문단 2.",
        relations=["leave-of-absence", "remote-work: hybrid"],
        evidence=["vacation-policy-abcd1234"],
        competing=["해석 A: 입사일 기준", "해석 B: 회계연도 기준"],
        open_questions=["반차 누적?"],
        sources=["src-vacation-policy"],
        backlinks=["vacation-policy-abcd1234"],
    )
    text = store.to_markdown["page"](m)
    back = store.from_markdown["page"](text)
    assert back == m


def test_task_round_trip():
    m = WikiTask(
        slug="conflict-vacation-policy-abcd1234",
        kind="conflict",
        description="claim A와 claim B 모순: 입사일 vs 회계연도 기준.",
        related=["vacation-policy-abcd1234", "vacation-policy-deadbeef"],
        created_at=datetime(2026, 5, 25, 10, 0, tzinfo=UTC),
        resolved=False,
    )
    text = store.to_markdown["task"](m)
    back = store.from_markdown["task"](text)
    assert back == m


# --- regression tests for S2 and S3 fixes ------------------------------------


def test_competing_item_with_embedded_newline_round_trips():
    """S2: a competing item that contains a literal newline must survive codec."""
    m = WikiPage(
        slug="s2-test",
        title="S2 Test",
        definition="정의.",
        overview="개요.",
        relations=[],
        evidence=[],
        competing=["해석 A: 첫째 줄\n둘째 줄 계속", "해석 B: 단일 줄"],
        open_questions=[],
        sources=[],
        backlinks=[],
    )
    text = store.to_markdown["page"](m)
    back = store.from_markdown["page"](text)
    assert back == m
    # Confirm the newline is preserved in the item, not split into two items.
    assert back.competing[0] == "해석 A: 첫째 줄\n둘째 줄 계속"
    assert len(back.competing) == 2


def test_prose_with_embedded_h2_heading_round_trips():
    """S3: a ## heading in prose that is NOT a known section boundary must be
    preserved as prose content, not treated as a new section."""
    m = WikiClaim(
        slug="s3-test-abcd1234",
        claim="주장 본문.",
        supporting=["src-x"],
        conflicting=[],
        confidence="medium",
        last_reviewed=date(2026, 5, 25),
        related_pages=["x"],
        evidence="근거 단락 1.\n\n## Something Else\n\n이 줄은 prose 내용.",
        notes=None,
    )
    text = store.to_markdown["claim"](m)
    back = store.from_markdown["claim"](text)
    assert back == m
    assert "## Something Else" in back.evidence


def test_none_sentinel_literal_preserved():
    """S2/S3: a list item or prose value literally equal to '(none)' must be
    preserved, not collapsed to None/empty."""
    # List item literally "(none)" in competing.
    m = WikiPage(
        slug="none-test",
        title="None Test",
        definition="정의.",
        overview="개요.",
        relations=[],
        evidence=[],
        competing=["(none)"],
        open_questions=[],
        sources=[],
        backlinks=[],
    )
    back = store.from_markdown["page"](store.to_markdown["page"](m))
    assert back == m
    assert back.competing == ["(none)"]


# --- WTR.1 round-trip tests ---------------------------------------------------


def test_task_round_trip_with_resolution():
    """WTR.1: WikiTask with incoming_claim + full resolution round-trips losslessly."""
    from datetime import UTC, datetime
    from uuid import UUID

    resolved_by = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    resolved_at = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)
    m = WikiTask(
        slug="conflict-vacation-policy-abcd1234",
        kind="conflict",
        description="claim A와 claim B 모순: 입사일 vs 회계연도 기준.",
        related=["vacation-policy-abcd1234", "vacation-policy-deadbeef"],
        created_at=datetime(2026, 5, 25, 10, 0, tzinfo=UTC),
        resolved=True,
        incoming_claim="연차는 회계연도 기준으로 매년 1월 1일 부여된다: 내규 개정안.",
        resolution=WikiTaskResolution(
            decision="use_incoming",
            note="개정안 내규 적용 결정.",
            resolved_by=resolved_by,
            resolved_at=resolved_at,
            produced_claim_slugs=["vacation-policy-abcd1234"],
        ),
    )
    text = store.to_markdown["task"](m)
    back = store.from_markdown["task"](text)
    assert back == m
    assert back.incoming_claim == m.incoming_claim
    assert back.resolution is not None
    assert back.resolution.decision == "use_incoming"
    assert back.resolution.note == "개정안 내규 적용 결정."
    assert back.resolution.resolved_by == resolved_by
    assert back.resolution.resolved_at == resolved_at
    assert back.resolution.produced_claim_slugs == ["vacation-policy-abcd1234"]


def test_task_round_trip_old_style_none_defaults():
    """WTR.1: 기존 task 파일(신규 필드 없음)은 incoming_claim=None, resolution=None으로 로드."""
    from datetime import UTC, datetime

    m = WikiTask(
        slug="conflict-vacation-policy-abcd1234",
        kind="conflict",
        description="claim A와 claim B 모순: 입사일 vs 회계연도 기준.",
        related=["vacation-policy-abcd1234", "vacation-policy-deadbeef"],
        created_at=datetime(2026, 5, 25, 10, 0, tzinfo=UTC),
        resolved=False,
        # incoming_claim과 resolution은 기본값 None으로 두어 구 파일 형태 시뮬레이션.
    )
    text = store.to_markdown["task"](m)
    back = store.from_markdown["task"](text)
    assert back == m
    assert back.incoming_claim is None
    assert back.resolution is None
