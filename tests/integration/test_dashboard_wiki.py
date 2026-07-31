"""Company dashboard records (calendar / weekly retro / meeting note) reflect
into the LLM wiki via the same idempotent corpus + author path as Notion/editor.

Covers: documents+corpus idempotency on re-save, all three sources land, member
names render, and authoring produces a wiki source (mock chat). Wiki-store root is
the isolated tmp from conftest `_isolate_wiki_store`, so repo wiki-store/ is clean.
"""

from __future__ import annotations

import json
import uuid
from datetime import date

from sqlalchemy import func, insert, select

from orthus import dashboard as d
from orthus.dashboard_wiki import (
    backfill_node_wiki,
    reflect_calendar_event,
    reflect_meeting_note,
    reflect_partner_company,
    reflect_weekly,
    render_calendar_event,
)
from orthus.db import session
from orthus.models.adapters.mock import MockChat
from orthus.tables import corpus_chunks, dashboard_projects, documents, team_members
from orthus.wiki import store

_NODE = "company"


def _chat() -> MockChat:
    return MockChat(
        default=json.dumps(
            {
                "summary": "회사 운영 기록 요약.",
                "key_concepts": ["일정", "회의"],
                "terminology": [],
                "open_questions": [],
                "claims": [
                    {
                        "claim": "분기 계획 회의가 진행되었다.",
                        "evidence": "대시보드 기록 근거.",
                        "confidence": "high",
                        "page": {"slug": "company-operations", "title": "회사 운영"},
                        "conflicting": [],
                    }
                ],
            }
        )
    )


def _doc_count(source: str) -> int:
    with session() as s:
        return s.execute(
            select(func.count()).select_from(documents).where(documents.c.source == source)
        ).scalar_one()


def _chunk_count(source: str) -> int:
    with session() as s:
        return s.execute(
            select(func.count())
            .select_from(corpus_chunks)
            .join(documents, documents.c.doc_id == corpus_chunks.c.doc_id)
            .where(documents.c.source == source)
        ).scalar_one()


def test_calendar_event_reflected_and_idempotent(user_id):
    ev = d.create_event(
        _NODE,
        user_id,
        d.CalendarEventIn(
            title="분기 계획 회의",
            event_date=date(2026, 6, 8),
            description="2분기 채용/제작 일정 논의",
        ),
    )

    reflect_calendar_event(user_id, ev, chat_model=_chat())
    assert _doc_count("dashboard_calendar") == 1
    assert _chunk_count("dashboard_calendar") >= 1

    # re-save (edit) the same event -> still one document, no duplicate corpus doc
    ev2 = d.update_event(
        _NODE,
        ev.event_id,
        d.CalendarEventIn(
            title="분기 계획 회의 (수정)",
            event_date=date(2026, 6, 8),
            description="2분기 채용/제작 일정 + 예산 논의",
        ),
    )
    reflect_calendar_event(user_id, ev2, chat_model=_chat())
    assert _doc_count("dashboard_calendar") == 1

    with session() as s:
        row = s.execute(
            select(documents.c.markdown, documents.c.source_canonical_id).where(
                documents.c.source == "dashboard_calendar"
            )
        ).one()
    assert row.source_canonical_id == f"dashboard:calendar:{ev.event_id}"
    assert "예산" in row.markdown  # reflects the edited content

    # authoring produced a wiki source in the isolated store
    assert len(store.list_slugs("source")) >= 1


def test_meeting_and_weekly_reflected(user_id):
    note = d.create_meeting_note(
        _NODE,
        user_id,
        d.MeetingNoteIn(
            title="주간 운영 회의",
            meeting_date=date(2026, 6, 8),
            body="채용 진행 상황과 제작 일정 점검.",
        ),
    )
    reflect_meeting_note(user_id, note, chat_model=_chat())

    project_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(dashboard_projects).values(project_id=project_id, node_id=_NODE, name="회사")
        )
        s.commit()
    entry = d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=project_id,
            week_start=date(2026, 6, 8),
            plan_items=[d.PlanItem(id="1", text="채용 공고 마감")],
            retro_items=[d.PlanItem(id="2", text="배포 자동화 완료")],
        ),
    )
    reflect_weekly(user_id, entry, chat_model=_chat())

    assert _doc_count("dashboard_meeting") == 1
    assert _doc_count("dashboard_weekly") == 1

    with session() as s:
        weekly_md = s.execute(
            select(documents.c.markdown).where(documents.c.source == "dashboard_weekly")
        ).scalar_one()
    assert "## 계획" in weekly_md and "## 회고" in weekly_md
    assert "배포 자동화 완료" in weekly_md


def test_backfill_reflects_existing_rows(user_id):
    # rows created directly (bypassing routes) — like a seed/migration insert
    d.create_event(_NODE, user_id, d.CalendarEventIn(title="촬영 D-1", event_date=date(2026, 6, 9)))
    d.create_meeting_note(
        _NODE, user_id, d.MeetingNoteIn(title="제작 점검", meeting_date=date(2026, 6, 9))
    )
    project_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(dashboard_projects).values(project_id=project_id, node_id=_NODE, name="제작")
        )
        s.commit()
    d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=project_id,
            week_start=date(2026, 6, 8),
            retro_items=[d.PlanItem(id="1", text="촬영 완료")],
        ),
    )

    counts = backfill_node_wiki(_NODE, user_id, chat_model=_chat())
    assert counts == {"calendar": 1, "meeting": 1, "weekly": 1}
    assert _doc_count("dashboard_calendar") == 1
    assert _doc_count("dashboard_meeting") == 1
    assert _doc_count("dashboard_weekly") == 1

    # idempotent re-run: no duplicate documents
    backfill_node_wiki(_NODE, user_id, chat_model=_chat())
    assert _doc_count("dashboard_calendar") == 1
    assert _doc_count("dashboard_weekly") == 1


def test_partner_company_reflected_with_contacts(user_id):
    with session() as s:
        s.execute(
            insert(dashboard_projects).values(
                project_id=(pid := uuid.uuid4()), node_id=_NODE, name="아틀라스"
            )
        )
        s.commit()

    partner = d.create_partner(
        _NODE,
        d.PartnerCompanyIn(
            name="해오름기획",
            org_type="보조출연사",
            address="서울 어딘가구",
            representative="박도현",
            project_ids=[pid],
            field_tags=["보조출연(드라마)"],
            status="협업중",
        ),
    )
    d.create_partner_contact(
        _NODE,
        partner.partner_id,
        d.PartnerContactIn(name="박도현", role="대표", phone="010-0000-2222", is_primary=True),
    )
    contacts = d.list_partner_contacts(_NODE, partner.partner_id)

    reflect_partner_company(user_id, partner, contacts, chat_model=_chat())
    assert _doc_count("dashboard_partner") == 1
    assert _chunk_count("dashboard_partner") >= 1

    with session() as s:
        row = s.execute(
            select(documents.c.markdown, documents.c.source_canonical_id).where(
                documents.c.source == "dashboard_partner"
            )
        ).one()
    assert row.source_canonical_id == f"dashboard:partner:{partner.partner_id}"
    assert "보조출연사" in row.markdown
    assert "아틀라스" in row.markdown  # project name resolved
    assert "박도현" in row.markdown  # contact rendered

    # re-reflect (edit) stays idempotent on documents
    reflect_partner_company(user_id, partner, contacts, chat_model=_chat())
    assert _doc_count("dashboard_partner") == 1


def test_calendar_markdown_resolves_member_names(user_id):
    member_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(team_members).values(
                member_id=member_id, node_id=_NODE, name="이수민", active=True
            )
        )
        s.execute(
            insert(dashboard_projects).values(
                project_id=(pid := uuid.uuid4()), node_id=_NODE, name="아틀라스"
            )
        )
        s.commit()

    ev = d.create_event(
        _NODE,
        user_id,
        d.CalendarEventIn(
            title="촬영", event_date=date(2026, 6, 10), member_ids=[member_id], project_id=pid
        ),
    )
    _, md = render_calendar_event(ev)
    assert "이수민" in md
    assert "아틀라스" in md
