"""Company dashboard: team, finance, weekly/monthly plans & retros, team
calendar, and culture info. Company-node only. Business logic lives here;
routes (orthus/api/routes/dashboard.py) stay thin."""

from __future__ import annotations

import json
import logging
import os as _os
import re
import subprocess as _subprocess
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus import media as media_store
from orthus import realtime
from orthus.connectors.nova import BUSY_UTIL_THRESHOLD, NovaMLClient
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import (
    auth_allowlist,
    auth_identities,
    company_culture,
    dashboard_entry_history,
    dashboard_kpi_checkins,
    dashboard_kpi_confidence,
    dashboard_kpis,
    dashboard_pages,
    dashboard_projects,
    finance_accounts,
    finance_api_keys,
    finance_ledger,
    finance_subscriptions,
    infra_providers,
    infra_resource_projects,
    infra_resources,
    meeting_attachments,
    meeting_notes,
    monthly_entries,
    notion_rows,
    partner_companies,
    partner_contacts,
    personal_board_tasks,
    personal_board_workspaces,
    project_activity,
    project_assignments,
    project_database_files,
    project_database_rows,
    project_databases,
    project_requirements,
    project_roles,
    recruiting_candidate_comments,
    recruiting_candidates,
    support_notes,
    support_programs,
    team_calendar_events,
    team_members,
    users,
    weekly_entries,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# date helpers (일요일 시작 주차, 월 1일)
# --------------------------------------------------------------------------
def week_start_sunday(d: date) -> date:
    """Return the Sunday on/before d (Sunday-start week).

    회사 계획·회고는 일요일 시작 주차다(FE 라벨 "일~토", 개인 보드 월요일주와
    구분). weekday(): Mon=0..Sun=6 → (weekday+1)%7 일만큼 빼면 그 주 일요일.
    """
    return d - timedelta(days=(d.weekday() + 1) % 7)


def month_first(d: date) -> date:
    """Return the first day of the month containing d."""
    return d.replace(day=1)


def _f(value: Decimal | float | int | None) -> float:
    return float(value) if value is not None else 0.0


# --------------------------------------------------------------------------
# Team members
# --------------------------------------------------------------------------
class TeamMemberIn(BaseModel):
    name: str
    title: str | None = None
    department: str | None = None
    email: str | None = None
    phone: str | None = None
    join_date: date | None = None
    birthday: date | None = None
    address: str | None = None
    emergency_contact: str | None = None
    bank_account: str | None = None
    color: str | None = None
    bio: str | None = None
    sort_order: int = 0
    active: bool = True


class TeamMember(TeamMemberIn):
    member_id: UUID
    # 출력 전용: 로그인 계정과 연결됐는지(linked) / 로그인 가능하게 초대(allowlist)됐는지(invited).
    user_id: UUID | None = None
    linked: bool = False
    invited: bool = False


def _member_out(row, *, invited: bool = False) -> TeamMember:
    user_id = getattr(row, "user_id", None)
    return TeamMember(
        member_id=row.member_id,
        name=row.name,
        title=row.title,
        department=row.department,
        email=row.email,
        phone=row.phone,
        join_date=row.join_date,
        birthday=row.birthday,
        address=row.address,
        emergency_contact=row.emergency_contact,
        bank_account=row.bank_account,
        color=row.color,
        bio=row.bio,
        sort_order=row.sort_order,
        active=row.active,
        user_id=user_id,
        linked=user_id is not None,
        invited=invited,
    )


_MEMBER_COLS = (
    team_members.c.member_id,
    team_members.c.name,
    team_members.c.title,
    team_members.c.department,
    team_members.c.email,
    team_members.c.phone,
    team_members.c.join_date,
    team_members.c.birthday,
    team_members.c.address,
    team_members.c.emergency_contact,
    team_members.c.bank_account,
    team_members.c.color,
    team_members.c.bio,
    team_members.c.sort_order,
    team_members.c.active,
    team_members.c.user_id,
)


# --------------------------------------------------------------------------
# 새 팀원 자동 연결 (member ↔ user ↔ allowlist)
# --------------------------------------------------------------------------
def _member_emails(email_str: str | None) -> set[str]:
    """팀원의 콤마 구분 email 필드를 정규화된 이메일 set으로."""
    from orthus.auth import normalize_email

    if not email_str:
        return set()
    # team_members.email은 콤마 또는 공백으로 구분될 수 있다(#524) — 둘 다 분리.
    return {normalize_email(e) for e in email_str.replace(",", " ").split()}


def _active_allowlist_emails(s, node_id: str) -> set[str]:
    from orthus.auth import normalize_email

    return {
        normalize_email(str(r.email))
        for r in s.execute(
            select(auth_allowlist.c.email).where(
                auth_allowlist.c.node_id == node_id,
                auth_allowlist.c.revoked_at.is_(None),
            )
        ).all()
        if r.email
    }


def _resolve_user_for_emails(s, emails: set[str]) -> UUID | None:
    """이메일 set에 매칭되는 user를 결정론적으로 찾는다. 정확히 하나면 그 user,
    없거나(0) 서로 다른 user가 같은 이메일을 공유(충돌)하면 None(안전하게 미링크).
    auth_identities.email에 UNIQUE 제약이 없으므로 first-match 비결정성을 피한다."""
    if not emails:
        return None
    from orthus.auth import normalize_email

    found: set[UUID] = set()
    for r in s.execute(select(auth_identities.c.user_id, auth_identities.c.email)).all():
        if r.email and normalize_email(str(r.email)) in emails:
            found.add(r.user_id)
            if len(found) > 1:
                return None
    return next(iter(found)) if len(found) == 1 else None


def _relink_member(s, member_id: UUID, emails: set[str]) -> None:
    """member.user_id를 현재 이메일 기준으로 재계산(clear-then-relink). 매칭 user가
    정확히 하나면 링크하고, 없거나 충돌이거나 이메일이 비면 NULL로 해제한다.
    이메일을 바꾸거나 지운 뒤에도 옛 계정 링크가 남지 않게 한다."""
    s.execute(
        team_members.update()
        .where(team_members.c.member_id == member_id)
        .values(user_id=_resolve_user_for_emails(s, emails))
    )


def _ensure_members_invited(s, node_id: str, emails: set[str], created_by: UUID | None) -> None:
    """신규 팀원이 로그인할 수 있게 allowlist에 'member' 역할로 초대(insert-only).
    기존 항목은 절대 건드리지 않는다(admin/owner 강등·revoke 해제 방지)."""
    if not emails:
        return
    from orthus.auth import utcnow

    now = utcnow()
    for email in emails:
        s.execute(
            pg_insert(auth_allowlist)
            .values(
                allowlist_id=uuid.uuid4(),
                node_id=node_id,
                email=email,
                role="member",
                created_by=created_by,
                created_at=now,
                revoked_at=None,
            )
            .on_conflict_do_nothing(
                index_elements=[auth_allowlist.c.node_id, auth_allowlist.c.email]
            )
        )


def _emails_owned_by_other_active_member(
    s, node_id: str, emails: set[str], exclude_member_id: UUID
) -> set[str]:
    """주어진 이메일 중, 이 멤버 말고 다른 *재직중* 팀원이 아직 보유한 이메일 set."""
    if not emails:
        return set()
    from orthus.auth import normalize_email

    owned: set[str] = set()
    for r in s.execute(
        select(team_members.c.email).where(
            team_members.c.node_id == node_id,
            team_members.c.active.is_(True),
            team_members.c.member_id != exclude_member_id,
        )
    ).all():
        if not r.email:
            continue
        for e in str(r.email).split(","):
            if e.strip():
                ne = normalize_email(e)
                if ne in emails:
                    owned.add(ne)
    return owned


def _revoke_invites(s, node_id: str, emails: set[str], exclude_member_id: UUID) -> None:
    """auto-grant했던 'member' allowlist 초대를 회수(revoked_at 설정). 오프보딩
    대칭성: 멤버 삭제/비활성/이메일 제거 시 로그인 접근이 남지 않게 한다. 단
    (1) 다른 재직 팀원이 같은 이메일을 보유하면 회수하지 않고, (2) owner/admin/
    viewer 역할은 절대 건드리지 않는다(수동 권한 부여 보존, 마지막 관리자 보호)."""
    if not emails:
        return
    from orthus.auth import utcnow

    to_revoke = emails - _emails_owned_by_other_active_member(s, node_id, emails, exclude_member_id)
    if not to_revoke:
        return
    s.execute(
        auth_allowlist.update()
        .where(
            auth_allowlist.c.node_id == node_id,
            auth_allowlist.c.email.in_(to_revoke),
            auth_allowlist.c.role == "member",
            auth_allowlist.c.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )


def _apply_member_links(
    s,
    node_id: str,
    member_id: UUID,
    *,
    new_emails: set[str],
    old_emails: set[str],
    active: bool,
    created_by: UUID | None,
) -> None:
    """멤버 생성/수정에 따른 user 링크·allowlist 초대/회수를 한 SAVEPOINT 안에서
    진짜 best-effort로 적용한다 — 무엇이 실패하든 멤버 행 write는 보존한다.
      - user_id를 현재 이메일 기준으로 재계산(clear-then-relink)
      - 재직중이면 현재 이메일을 'member'로 초대(insert-only) + 빠진 옛 이메일 회수
      - 비활성화면 이 멤버의 옛/새 이메일을 모두 회수(오프보딩 대칭)
    """
    try:
        with s.begin_nested():
            _relink_member(s, member_id, new_emails)
            if active:
                _ensure_members_invited(s, node_id, new_emails, created_by)
                dropped = old_emails - new_emails
                if dropped:
                    _revoke_invites(s, node_id, dropped, member_id)
            else:
                _revoke_invites(s, node_id, old_emails | new_emails, member_id)
    except Exception:
        pass


def link_member_for_login(node_id: str, user_id: UUID) -> None:
    """로그인 시 best-effort로 이 user를 자기 팀원 행에 연결(이메일 매칭).
    내 보드/배정이 바로 이어지게 한다. 매칭 없으면 조용한 no-op."""
    try:
        resolve_member_id(node_id, user_id, None)
    except Exception:
        pass


def _member_with_status(s, node_id: str, member_id: UUID) -> TeamMember:
    """현재 트랜잭션 기준으로 member 행 + linked/invited 상태를 만들어 반환."""
    row = s.execute(select(*_MEMBER_COLS).where(team_members.c.member_id == member_id)).one()
    emails = _member_emails(row.email)
    invited = bool(emails & _active_allowlist_emails(s, node_id)) if emails else False
    return _member_out(row, invited=invited)


def list_team_members(node_id: str) -> list[TeamMember]:
    with session() as s:
        rows = s.execute(
            select(*_MEMBER_COLS)
            .where(team_members.c.node_id == node_id)
            .order_by(team_members.c.sort_order, team_members.c.name)
        ).all()
        allow = _active_allowlist_emails(s, node_id)
    out: list[TeamMember] = []
    for r in rows:
        emails = _member_emails(r.email)
        out.append(_member_out(r, invited=bool(emails & allow)))
    return out


def resolve_member_id(node_id: str, user_id: UUID, email: str | None = None) -> UUID | None:
    """Map a logged-in account to the team member that *is* that person.

    Strategy (docs/abstract-puzzling-wand plan, Part A):
      1. If a `team_members` row is already linked via `user_id`, return it.
      2. Otherwise match the account's login email(s) — the session email plus
         every `auth_identities.email` for this user — against each member's
         comma-separated `email` field. On a match, backfill `team_members.user_id`
         (so the link persists) and return the member_id.
      3. No match → None (sync features become a quiet no-op, never an error).
    """
    from orthus.auth import normalize_email  # local import avoids import cycle

    with session() as s:
        linked = s.execute(
            select(team_members.c.member_id)
            .where(
                team_members.c.node_id == node_id,
                team_members.c.user_id == user_id,
            )
            .limit(1)
        ).first()
        if linked:
            return linked.member_id

        emails: set[str] = set()
        if email:
            emails.add(normalize_email(email))
        for r in s.execute(
            select(auth_identities.c.email).where(auth_identities.c.user_id == user_id)
        ).all():
            if r.email:
                emails.add(normalize_email(str(r.email)))
        if not emails:
            return None

        rows = s.execute(
            select(
                team_members.c.member_id,
                team_members.c.email,
                team_members.c.user_id,
            ).where(team_members.c.node_id == node_id)
        ).all()
        for row in rows:
            if not row.email:
                continue
            # team_members.email may be comma- or whitespace-separated; split on both.
            member_emails = {normalize_email(e) for e in str(row.email).replace(",", " ").split()}
            if emails & member_emails:
                if row.user_id is None:
                    s.execute(
                        team_members.update()
                        .where(team_members.c.member_id == row.member_id)
                        .values(user_id=user_id)
                    )
                    s.commit()
                return row.member_id
    return None


def member_color(node_id: str, member_id: UUID) -> str | None:
    with session() as s:
        row = s.execute(
            select(team_members.c.color).where(
                team_members.c.node_id == node_id,
                team_members.c.member_id == member_id,
            )
        ).first()
    return row.color if row else None


def create_team_member(
    node_id: str, body: TeamMemberIn, created_by: UUID | None = None
) -> TeamMember:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    member_id = uuid.uuid4()
    emails = _member_emails(body.email)
    with session() as s:
        s.execute(
            team_members.insert().values(
                member_id=member_id, node_id=node_id, **body.model_dump(exclude={"name"}), name=name
            )
        )
        # 자동 연결/초대는 진짜 best-effort: SAVEPOINT로 격리해 어떤 실패도(예: 비정상
        # actor의 created_by FK 위반) 멤버 생성 자체를 롤백하지 못하게 한다.
        _apply_member_links(
            s,
            node_id,
            member_id,
            new_emails=emails,
            old_emails=set(),
            active=body.active,
            created_by=created_by,
        )
        result = _member_with_status(s, node_id, member_id)
        s.commit()
    return result


def update_team_member(
    node_id: str, member_id: UUID, body: TeamMemberIn, created_by: UUID | None = None
) -> TeamMember:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    new_emails = _member_emails(body.email)
    with session() as s:
        prev = s.execute(
            select(team_members.c.email).where(
                team_members.c.node_id == node_id, team_members.c.member_id == member_id
            )
        ).first()
        if prev is None:
            raise LookupError("team member not found")
        s.execute(
            team_members.update()
            .where(team_members.c.node_id == node_id, team_members.c.member_id == member_id)
            .values(**body.model_dump(exclude={"name"}), name=name, updated_at=func.now())
        )
        _apply_member_links(
            s,
            node_id,
            member_id,
            new_emails=new_emails,
            old_emails=_member_emails(prev.email),
            active=body.active,
            created_by=created_by,
        )
        out = _member_with_status(s, node_id, member_id)
        s.commit()
    return out


def delete_team_member(node_id: str, member_id: UUID) -> None:
    with session() as s:
        prev = s.execute(
            select(team_members.c.email).where(
                team_members.c.node_id == node_id, team_members.c.member_id == member_id
            )
        ).first()
        result = s.execute(
            delete(team_members).where(
                team_members.c.node_id == node_id, team_members.c.member_id == member_id
            )
        )
        if result.rowcount == 0:
            raise LookupError("team member not found")
        # 삭제 = 오프보딩: auto-grant했던 초대를 회수해 로그인 접근이 남지 않게 한다.
        if prev is not None:
            try:
                with s.begin_nested():
                    _revoke_invites(s, node_id, _member_emails(prev.email), member_id)
            except Exception:
                pass
        s.commit()


# --------------------------------------------------------------------------
# 인재영입(recruiting) 후보 리스트 — 앞으로 영입하거나 킵인터치할 사람들의 CRM.
# Notion '영입 후보' DB를 본떴으되 검증 메모는 빼고 전화번호를 더했다.
# --------------------------------------------------------------------------
class RecruitingCandidateIn(BaseModel):
    name: str
    role: str | None = None
    education: str | None = None
    phone: str | None = None
    email: str | None = None
    linkedin: str | None = None
    send_status: str = "컨택전"
    note: str | None = None
    sort_order: int = 0


class RecruitingCandidate(RecruitingCandidateIn):
    candidate_id: UUID


_CANDIDATE_COLS = (
    recruiting_candidates.c.candidate_id,
    recruiting_candidates.c.name,
    recruiting_candidates.c.role,
    recruiting_candidates.c.education,
    recruiting_candidates.c.phone,
    recruiting_candidates.c.email,
    recruiting_candidates.c.linkedin,
    recruiting_candidates.c.send_status,
    recruiting_candidates.c.note,
    recruiting_candidates.c.sort_order,
)


def _candidate_out(row) -> RecruitingCandidate:
    return RecruitingCandidate(
        candidate_id=row.candidate_id,
        name=row.name,
        role=row.role,
        education=row.education,
        phone=row.phone,
        email=row.email,
        linkedin=row.linkedin,
        send_status=row.send_status,
        note=row.note,
        sort_order=row.sort_order,
    )


def list_recruiting_candidates(node_id: str) -> list[RecruitingCandidate]:
    with session() as s:
        rows = s.execute(
            select(*_CANDIDATE_COLS)
            .where(recruiting_candidates.c.node_id == node_id)
            .order_by(recruiting_candidates.c.sort_order, recruiting_candidates.c.created_at)
        ).all()
    return [_candidate_out(r) for r in rows]


def create_recruiting_candidate(node_id: str, body: RecruitingCandidateIn) -> RecruitingCandidate:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    candidate_id = uuid.uuid4()
    with session() as s:
        row = s.execute(
            recruiting_candidates.insert()
            .values(
                candidate_id=candidate_id,
                node_id=node_id,
                **body.model_dump(exclude={"name"}),
                name=name,
            )
            .returning(*_CANDIDATE_COLS)
        ).one()
        s.commit()
    return _candidate_out(row)


def update_recruiting_candidate(
    node_id: str, candidate_id: UUID, body: RecruitingCandidateIn
) -> RecruitingCandidate:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    with session() as s:
        result = s.execute(
            recruiting_candidates.update()
            .where(
                recruiting_candidates.c.node_id == node_id,
                recruiting_candidates.c.candidate_id == candidate_id,
            )
            .values(**body.model_dump(exclude={"name"}), name=name, updated_at=func.now())
            .returning(*_CANDIDATE_COLS)
        ).first()
        s.commit()
    if result is None:
        raise LookupError("recruiting candidate not found")
    return _candidate_out(result)


def delete_recruiting_candidate(node_id: str, candidate_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(recruiting_candidates).where(
                recruiting_candidates.c.node_id == node_id,
                recruiting_candidates.c.candidate_id == candidate_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("recruiting candidate not found")


# --- 후보별 팀원 코멘트 ---
class RecruitingCommentIn(BaseModel):
    author_member_id: UUID | None = None
    author_name: str
    body: str


class RecruitingComment(RecruitingCommentIn):
    comment_id: UUID
    candidate_id: UUID
    created_at: datetime


def _comment_out(row) -> RecruitingComment:
    return RecruitingComment(
        comment_id=row.comment_id,
        candidate_id=row.candidate_id,
        author_member_id=row.author_member_id,
        author_name=row.author_name,
        body=row.body,
        created_at=row.created_at,
    )


_COMMENT_COLS = (
    recruiting_candidate_comments.c.comment_id,
    recruiting_candidate_comments.c.candidate_id,
    recruiting_candidate_comments.c.author_member_id,
    recruiting_candidate_comments.c.author_name,
    recruiting_candidate_comments.c.body,
    recruiting_candidate_comments.c.created_at,
)


def list_recruiting_comments(node_id: str, candidate_id: UUID) -> list[RecruitingComment]:
    with session() as s:
        rows = s.execute(
            select(*_COMMENT_COLS)
            .where(
                recruiting_candidate_comments.c.node_id == node_id,
                recruiting_candidate_comments.c.candidate_id == candidate_id,
            )
            .order_by(recruiting_candidate_comments.c.created_at.asc())
        ).all()
    return [_comment_out(r) for r in rows]


def create_recruiting_comment(
    node_id: str, candidate_id: UUID, body: RecruitingCommentIn
) -> RecruitingComment:
    author = body.author_name.strip()
    text = body.body.strip()
    if not author:
        raise ValueError("author_name required")
    if not text:
        raise ValueError("body required")
    with session() as s:
        owner = s.execute(
            select(recruiting_candidates.c.candidate_id).where(
                recruiting_candidates.c.node_id == node_id,
                recruiting_candidates.c.candidate_id == candidate_id,
            )
        ).first()
        if owner is None:
            raise LookupError("recruiting candidate not found")
        row = s.execute(
            recruiting_candidate_comments.insert()
            .values(
                comment_id=uuid.uuid4(),
                node_id=node_id,
                candidate_id=candidate_id,
                author_member_id=body.author_member_id,
                author_name=author,
                body=text,
            )
            .returning(*_COMMENT_COLS)
        ).one()
        s.commit()
    return _comment_out(row)


def delete_recruiting_comment(node_id: str, comment_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(recruiting_candidate_comments).where(
                recruiting_candidate_comments.c.node_id == node_id,
                recruiting_candidate_comments.c.comment_id == comment_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("comment not found")


# --- Notion '팀원' DB → team_members sync (acme workspace) ---
# notion_rows.properties is a flat {column_name: readable_value} map produced by
# the Notion connector. Column names vary, so map by candidate keys.
class TeamSyncResult(BaseModel):
    total: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0


_TEAM_DB_NAMES = ("팀원",)
_NAME_KEYS = ("이름", "성명", "성함", "Name", "name")
_EMAIL_KEYS = ("이메일", "메일", "Email", "email", "E-mail", "e-mail")
_PHONE_KEYS = ("전화번호", "연락처", "휴대폰", "핸드폰", "전화", "Phone", "phone")
_BIRTHDAY_KEYS = ("생일", "생년월일", "Birthday", "birthday")
_JOIN_KEYS = ("입사일", "입사", "합류일", "입사 일자", "Join", "join")
_ADDRESS_KEYS = ("주소", "Address", "address")
_EMERGENCY_KEYS = ("비상연락처", "비상 연락처", "Emergency", "비상")
_BIO_KEYS = ("소개", "메모", "비고", "Bio", "bio", "Note", "note")


def _pick(props: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = props.get(k)
        if v not in (None, ""):
            text = str(v).strip()
            if text:
                return text
    return None


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def sync_team_members_from_notion(node_id: str) -> TeamSyncResult:
    """Upsert company-scope Notion '팀원' rows into team_members, deduped by the
    Notion row id (source_ref). Requires the Notion connector import to have run."""
    result = TeamSyncResult()
    with session() as s:
        rows = s.execute(
            select(notion_rows.c.row_id, notion_rows.c.properties).where(
                notion_rows.c.db_name.in_(_TEAM_DB_NAMES),
                notion_rows.c.scope == "company",
            )
        ).all()
        result.total = len(rows)
        for r in rows:
            props = r.properties or {}
            name = _pick(props, _NAME_KEYS) or next(
                (str(v).strip() for v in props.values() if str(v).strip()), None
            )
            if not name:
                result.skipped += 1
                continue
            ref = str(r.row_id)
            values = dict(
                name=name,
                email=_pick(props, _EMAIL_KEYS),
                phone=_pick(props, _PHONE_KEYS),
                birthday=_parse_iso_date(_pick(props, _BIRTHDAY_KEYS)),
                join_date=_parse_iso_date(_pick(props, _JOIN_KEYS)),
                address=_pick(props, _ADDRESS_KEYS),
                emergency_contact=_pick(props, _EMERGENCY_KEYS),
                bio=_pick(props, _BIO_KEYS),
                source="notion",
            )
            existing = s.execute(
                select(team_members.c.member_id).where(
                    team_members.c.node_id == node_id,
                    team_members.c.source_ref == ref,
                )
            ).first()
            if existing:
                s.execute(
                    team_members.update()
                    .where(team_members.c.member_id == existing.member_id)
                    .values(**values, updated_at=func.now())
                )
                result.updated += 1
            else:
                s.execute(
                    team_members.insert().values(
                        member_id=uuid.uuid4(), node_id=node_id, source_ref=ref, **values
                    )
                )
                result.created += 1
        s.commit()
    return result


# --------------------------------------------------------------------------
# Dashboard projects (계획·회고 단위)
# --------------------------------------------------------------------------
# SE 단계 (docs/project-se-management.md): 요구조건 검토 → 시스템 설계 검토 →
# 예비/상세 설계 검토 → 검증(V&V) → 운영. NULL이면 SE 관리 미적용 프로젝트.
SE_STAGES = ("srr", "sdr", "pdr", "cdr", "vnv", "ops")


PROJECT_HEALTH = ("on_track", "at_risk", "off_track")


class ProjectIn(BaseModel):
    name: str
    color: str | None = None
    description: str | None = None
    # 노션식 자유 본문(BlockNote 블록 JSON 문자열). description은 목록 요약용.
    body: str | None = None
    status: str | None = None
    sort_order: int = 0
    active: bool = True
    # 하위 프로젝트: 부모 project_id. 루트는 None. 깊이 2단계(루트→하위)만 허용.
    parent_project_id: UUID | None = None
    # SE 단계 (SE_STAGES 중 하나). None=미적용.
    se_stage: str | None = None
    # 트래킹 (마이그레이션 0094): 기간·오너(DRI)·건강 신호.
    start_date: date | None = None
    target_date: date | None = None
    owner_member_id: UUID | None = None
    # on_track|at_risk|off_track. None=미평가.
    health: str | None = None


class DashboardProject(ProjectIn):
    project_id: UUID
    updated_at: datetime | None = None


def _project_out(row) -> DashboardProject:
    return DashboardProject(
        project_id=row.project_id,
        name=row.name,
        color=row.color,
        description=row.description,
        body=row.body,
        status=row.status,
        sort_order=row.sort_order,
        active=row.active,
        parent_project_id=row.parent_project_id,
        se_stage=row.se_stage,
        start_date=row.start_date,
        target_date=row.target_date,
        owner_member_id=row.owner_member_id,
        health=row.health,
        updated_at=row.updated_at,
    )


_PROJECT_COLS = (
    dashboard_projects.c.project_id,
    dashboard_projects.c.name,
    dashboard_projects.c.color,
    dashboard_projects.c.description,
    dashboard_projects.c.body,
    dashboard_projects.c.status,
    dashboard_projects.c.sort_order,
    dashboard_projects.c.active,
    dashboard_projects.c.parent_project_id,
    dashboard_projects.c.se_stage,
    dashboard_projects.c.start_date,
    dashboard_projects.c.target_date,
    dashboard_projects.c.owner_member_id,
    dashboard_projects.c.health,
    dashboard_projects.c.updated_at,
)


def _validate_se_stage(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if value not in SE_STAGES:
        raise ValueError(f"se_stage는 {'/'.join(SE_STAGES)} 중 하나여야 합니다")
    return value


def _validate_health(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if value not in PROJECT_HEALTH:
        raise ValueError(f"health는 {'/'.join(PROJECT_HEALTH)} 중 하나여야 합니다")
    return value


def _validate_parent(s, node_id: str, project_id: UUID | None, parent_id: UUID | None) -> None:
    """하위 프로젝트 깊이 2단계 보장: 부모는 같은 노드의 루트 프로젝트여야 하고,
    이미 하위를 가진 프로젝트를 남의 하위로 옮길 수 없다."""
    if parent_id is None:
        return
    if project_id is not None and parent_id == project_id:
        raise ValueError("자기 자신을 상위 프로젝트로 지정할 수 없습니다")
    parent = s.execute(
        select(dashboard_projects.c.parent_project_id).where(
            dashboard_projects.c.node_id == node_id,
            dashboard_projects.c.project_id == parent_id,
        )
    ).first()
    if parent is None:
        raise ValueError("상위 프로젝트를 찾을 수 없습니다")
    if parent.parent_project_id is not None:
        raise ValueError("하위 프로젝트 아래에는 하위를 만들 수 없습니다 (2단계까지)")
    if project_id is not None:
        has_children = s.execute(
            select(dashboard_projects.c.project_id)
            .where(dashboard_projects.c.parent_project_id == project_id)
            .limit(1)
        ).first()
        if has_children is not None:
            raise ValueError(
                "하위 프로젝트를 가진 프로젝트는 다른 프로젝트 아래로 옮길 수 없습니다"
            )


def list_projects(node_id: str) -> list[DashboardProject]:
    with session() as s:
        rows = s.execute(
            select(*_PROJECT_COLS)
            .where(dashboard_projects.c.node_id == node_id)
            .order_by(dashboard_projects.c.sort_order, dashboard_projects.c.name)
        ).all()
    return [_project_out(r) for r in rows]


def get_project(node_id: str, project_id: UUID) -> DashboardProject:
    with session() as s:
        row = s.execute(
            select(*_PROJECT_COLS).where(
                dashboard_projects.c.node_id == node_id,
                dashboard_projects.c.project_id == project_id,
            )
        ).first()
    if row is None:
        raise LookupError("project not found")
    return _project_out(row)


def create_project(node_id: str, body: ProjectIn) -> DashboardProject:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    project_id = uuid.uuid4()
    se_stage = _validate_se_stage(body.se_stage)
    health = _validate_health(body.health)
    with session() as s:
        _validate_parent(s, node_id, None, body.parent_project_id)
        row = s.execute(
            pg_insert(dashboard_projects)
            .values(
                project_id=project_id,
                node_id=node_id,
                name=name,
                color=body.color,
                description=body.description,
                body=body.body,
                status=body.status,
                sort_order=body.sort_order,
                active=body.active,
                parent_project_id=body.parent_project_id,
                se_stage=se_stage,
                start_date=body.start_date,
                target_date=body.target_date,
                owner_member_id=body.owner_member_id,
                health=health,
            )
            .on_conflict_do_update(
                index_elements=["node_id", "name"],
                set_={
                    "color": body.color,
                    "description": body.description,
                    "body": body.body,
                    "status": body.status,
                    "sort_order": body.sort_order,
                    "active": body.active,
                    "parent_project_id": body.parent_project_id,
                    "se_stage": se_stage,
                    "start_date": body.start_date,
                    "target_date": body.target_date,
                    "owner_member_id": body.owner_member_id,
                    "health": health,
                },
            )
            .returning(*_PROJECT_COLS)
        ).one()
        s.commit()
    return _project_out(row)


# 활동 로그에 남길 프로젝트 필드 (body는 에디터 자동저장 노이즈라 제외,
# sort_order/color/active는 표시용이라 제외). description은 값 없이 "수정"만.
_ACTIVITY_FIELDS = (
    "name",
    "status",
    "se_stage",
    "health",
    "start_date",
    "target_date",
    "owner_member_id",
    "parent_project_id",
)


def _log_activity(
    s,
    node_id: str,
    project_id: UUID,
    actor_user_id: UUID | None,
    entity_type: str,
    action: str,
    *,
    entity_id: str | None = None,
    field: str | None = None,
    before: str | None = None,
    after: str | None = None,
) -> None:
    """활동 로그 append (호출자 세션/트랜잭션에 편승 — 본 작업과 원자적)."""
    s.execute(
        project_activity.insert().values(
            activity_id=uuid.uuid4(),
            node_id=node_id,
            project_id=project_id,
            actor_user_id=actor_user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            field=field,
            before=(before or None) and str(before)[:200],
            after=(after or None) and str(after)[:200],
        )
    )


def update_project(
    node_id: str,
    project_id: UUID,
    body: ProjectIn,
    actor_user_id: UUID | None = None,
) -> DashboardProject:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    se_stage = _validate_se_stage(body.se_stage)
    health = _validate_health(body.health)
    with session() as s:
        _validate_parent(s, node_id, project_id, body.parent_project_id)
        old = s.execute(
            select(*_PROJECT_COLS).where(
                dashboard_projects.c.node_id == node_id,
                dashboard_projects.c.project_id == project_id,
            )
        ).first()
        if old is None:
            raise LookupError("project not found")
        result = s.execute(
            dashboard_projects.update()
            .where(
                dashboard_projects.c.node_id == node_id,
                dashboard_projects.c.project_id == project_id,
            )
            .values(
                name=name,
                color=body.color,
                description=body.description,
                body=body.body,
                status=body.status,
                sort_order=body.sort_order,
                active=body.active,
                parent_project_id=body.parent_project_id,
                se_stage=se_stage,
                start_date=body.start_date,
                target_date=body.target_date,
                owner_member_id=body.owner_member_id,
                health=health,
                updated_at=func.now(),
            )
            .returning(*_PROJECT_COLS)
        ).first()
        if result is not None:
            new_vals = {
                "name": name,
                "status": body.status,
                "se_stage": se_stage,
                "health": health,
                "start_date": body.start_date,
                "target_date": body.target_date,
                "owner_member_id": body.owner_member_id,
                "parent_project_id": body.parent_project_id,
            }
            for field in _ACTIVITY_FIELDS:
                before, after = getattr(old, field), new_vals[field]
                if str(before or "") != str(after or ""):
                    _log_activity(
                        s,
                        node_id,
                        project_id,
                        actor_user_id,
                        "project",
                        "update",
                        field=field,
                        before=str(before) if before is not None else None,
                        after=str(after) if after is not None else None,
                    )
            if str(old.description or "") != str(body.description or ""):
                _log_activity(
                    s, node_id, project_id, actor_user_id, "project", "update", field="description"
                )
        s.commit()
    if result is None:
        raise LookupError("project not found")
    return _project_out(result)


def delete_project(node_id: str, project_id: UUID) -> None:
    with session() as s:
        exists = s.execute(
            select(dashboard_projects.c.project_id).where(
                dashboard_projects.c.node_id == node_id,
                dashboard_projects.c.project_id == project_id,
            )
        ).first()
        if exists is None:
            raise LookupError("project not found")
        # 소속 인라인 데이터베이스(칸반 보드 포함)도 함께 삭제 — project_id는
        # 느슨한 참조(FK 없음)라 명시적으로 지우지 않으면 고아 데이터베이스가 남는다.
        # 하위 프로젝트는 FK CASCADE로 지워지므로 그 소속 데이터베이스까지 포함한다.
        child_ids = [
            r.project_id
            for r in s.execute(
                select(dashboard_projects.c.project_id).where(
                    dashboard_projects.c.node_id == node_id,
                    dashboard_projects.c.parent_project_id == project_id,
                )
            )
        ]
        db_ids = [
            r.database_id
            for r in s.execute(
                select(project_databases.c.database_id).where(
                    project_databases.c.node_id == node_id,
                    project_databases.c.project_id.in_([project_id, *child_ids]),
                )
            )
        ]
        if db_ids:
            s.execute(
                delete(project_database_files).where(
                    project_database_files.c.database_id.in_(db_ids)
                )
            )
            s.execute(
                delete(project_database_rows).where(project_database_rows.c.database_id.in_(db_ids))
            )
            s.execute(delete(project_databases).where(project_databases.c.database_id.in_(db_ids)))
        result = s.execute(
            delete(dashboard_projects).where(
                dashboard_projects.c.node_id == node_id,
                dashboard_projects.c.project_id == project_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("project not found")


# --------------------------------------------------------------------------
# 프로젝트 SE 요구조건 대장 (docs/project-se-management.md)
#
# SE-Seminar의 SRR 모델을 따른다: 요구조건 = 외부 '제약조건(constraint)' +
# 집단이 세운 '프로젝트 목표(goal)'. 번호(kind별 순번)를 붙여 표로 관리하고,
# 하위 프로젝트는 상위 요구조건을 flow-down(복사+원본 링크)해 서브시스템
# 요구조건으로 구체화한다. 상태로 검증(V&V) 여부를 추적한다.
# --------------------------------------------------------------------------
REQUIREMENT_KINDS = ("constraint", "goal")
REQUIREMENT_STATUSES = ("open", "verifying", "satisfied", "failed")


class RequirementIn(BaseModel):
    kind: str = "goal"
    text: str
    verify_method: str | None = None
    notes: str | None = None
    status: str = "open"


class ProjectRequirement(RequirementIn):
    requirement_id: UUID
    project_id: UUID
    num: int
    parent_requirement_id: UUID | None = None


class RequirementSummary(BaseModel):
    """프로젝트별 요구조건 충족 현황 — 목록/하위 카드 배지용."""

    project_id: UUID
    total: int = 0
    satisfied: int = 0
    verifying: int = 0
    failed: int = 0


_REQ_COLS = (
    project_requirements.c.requirement_id,
    project_requirements.c.project_id,
    project_requirements.c.num,
    project_requirements.c.kind,
    project_requirements.c.text,
    project_requirements.c.verify_method,
    project_requirements.c.notes,
    project_requirements.c.status,
    project_requirements.c.parent_requirement_id,
)


def _req_out(row) -> ProjectRequirement:
    return ProjectRequirement(
        requirement_id=row.requirement_id,
        project_id=row.project_id,
        num=row.num,
        kind=row.kind,
        text=row.text,
        verify_method=row.verify_method,
        notes=row.notes,
        status=row.status,
        parent_requirement_id=row.parent_requirement_id,
    )


def _validate_requirement(body: RequirementIn) -> None:
    if body.kind not in REQUIREMENT_KINDS:
        raise ValueError("kind는 constraint 또는 goal이어야 합니다")
    if body.status not in REQUIREMENT_STATUSES:
        raise ValueError(f"status는 {'/'.join(REQUIREMENT_STATUSES)} 중 하나여야 합니다")
    if not body.text.strip():
        raise ValueError("요구조건 내용이 필요합니다")


def _require_project(s, node_id: str, project_id: UUID) -> None:
    row = s.execute(
        select(dashboard_projects.c.project_id).where(
            dashboard_projects.c.node_id == node_id,
            dashboard_projects.c.project_id == project_id,
        )
    ).first()
    if row is None:
        raise LookupError("project not found")


def _next_req_num(s, project_id: UUID, kind: str) -> int:
    current = s.execute(
        select(func.max(project_requirements.c.num)).where(
            project_requirements.c.project_id == project_id,
            project_requirements.c.kind == kind,
        )
    ).scalar()
    return int(current or 0) + 1


def list_requirements(node_id: str, project_id: UUID) -> list[ProjectRequirement]:
    with session() as s:
        _require_project(s, node_id, project_id)
        rows = s.execute(
            select(*_REQ_COLS)
            .where(
                project_requirements.c.node_id == node_id,
                project_requirements.c.project_id == project_id,
            )
            .order_by(project_requirements.c.kind, project_requirements.c.num)
        ).all()
    return [_req_out(r) for r in rows]


def create_requirement(
    node_id: str,
    project_id: UUID,
    body: RequirementIn,
    actor_user_id: UUID | None = None,
) -> ProjectRequirement:
    _validate_requirement(body)
    with session() as s:
        _require_project(s, node_id, project_id)
        row = s.execute(
            project_requirements.insert()
            .values(
                requirement_id=uuid.uuid4(),
                node_id=node_id,
                project_id=project_id,
                num=_next_req_num(s, project_id, body.kind),
                kind=body.kind,
                text=body.text.strip(),
                verify_method=body.verify_method,
                notes=body.notes,
                status=body.status,
            )
            .returning(*_REQ_COLS)
        ).one()
        _log_activity(
            s,
            node_id,
            project_id,
            actor_user_id,
            "requirement",
            "create",
            entity_id=str(row.requirement_id),
            after=body.text.strip(),
        )
        s.commit()
    return _req_out(row)


def update_requirement(
    node_id: str,
    project_id: UUID,
    requirement_id: UUID,
    body: RequirementIn,
    actor_user_id: UUID | None = None,
) -> ProjectRequirement:
    _validate_requirement(body)
    with session() as s:
        old = s.execute(
            select(project_requirements.c.status, project_requirements.c.text).where(
                project_requirements.c.node_id == node_id,
                project_requirements.c.project_id == project_id,
                project_requirements.c.requirement_id == requirement_id,
            )
        ).first()
        row = s.execute(
            project_requirements.update()
            .where(
                project_requirements.c.node_id == node_id,
                project_requirements.c.project_id == project_id,
                project_requirements.c.requirement_id == requirement_id,
            )
            .values(
                kind=body.kind,
                text=body.text.strip(),
                verify_method=body.verify_method,
                notes=body.notes,
                status=body.status,
                updated_at=func.now(),
            )
            .returning(*_REQ_COLS)
        ).first()
        if row is not None and old is not None and old.status != body.status:
            _log_activity(
                s,
                node_id,
                project_id,
                actor_user_id,
                "requirement",
                "status_change",
                entity_id=str(requirement_id),
                field="status",
                before=old.status,
                after=body.status,
            )
        s.commit()
    if row is None:
        raise LookupError("requirement not found")
    return _req_out(row)


def delete_requirement(
    node_id: str,
    project_id: UUID,
    requirement_id: UUID,
    actor_user_id: UUID | None = None,
) -> None:
    with session() as s:
        old = s.execute(
            select(project_requirements.c.text).where(
                project_requirements.c.node_id == node_id,
                project_requirements.c.project_id == project_id,
                project_requirements.c.requirement_id == requirement_id,
            )
        ).first()
        result = s.execute(
            delete(project_requirements).where(
                project_requirements.c.node_id == node_id,
                project_requirements.c.project_id == project_id,
                project_requirements.c.requirement_id == requirement_id,
            )
        )
        if result.rowcount:
            _log_activity(
                s,
                node_id,
                project_id,
                actor_user_id,
                "requirement",
                "delete",
                entity_id=str(requirement_id),
                before=old.text if old else None,
            )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("requirement not found")


def flow_down_requirements(
    node_id: str, project_id: UUID, requirement_ids: list[UUID]
) -> list[ProjectRequirement]:
    """상위 프로젝트 요구조건을 하위 프로젝트로 flow-down(복사 + 원본 링크).

    SE의 시스템 요구조건 → 서브시스템 요구조건 분해에 해당한다. 하위에서 내용을
    구체화해도 원본 링크(parent_requirement_id)로 추적성이 남는다. 이미 같은
    원본에서 가져온 요구조건이 있으면 건너뛴다(멱등).
    """
    with session() as s:
        proj = s.execute(
            select(dashboard_projects.c.parent_project_id).where(
                dashboard_projects.c.node_id == node_id,
                dashboard_projects.c.project_id == project_id,
            )
        ).first()
        if proj is None:
            raise LookupError("project not found")
        if proj.parent_project_id is None:
            raise ValueError("상위 프로젝트가 없는 프로젝트에는 flow-down할 수 없습니다")
        existing_parents = {
            r[0]
            for r in s.execute(
                select(project_requirements.c.parent_requirement_id).where(
                    project_requirements.c.project_id == project_id,
                    project_requirements.c.parent_requirement_id.is_not(None),
                )
            )
        }
        sources = s.execute(
            select(*_REQ_COLS).where(
                project_requirements.c.node_id == node_id,
                project_requirements.c.project_id == proj.parent_project_id,
                project_requirements.c.requirement_id.in_(requirement_ids),
            )
        ).all()
        created: list[ProjectRequirement] = []
        for src in sources:
            if src.requirement_id in existing_parents:
                continue
            row = s.execute(
                project_requirements.insert()
                .values(
                    requirement_id=uuid.uuid4(),
                    node_id=node_id,
                    project_id=project_id,
                    num=_next_req_num(s, project_id, src.kind),
                    kind=src.kind,
                    text=src.text,
                    verify_method=src.verify_method,
                    notes=src.notes,
                    status="open",
                    parent_requirement_id=src.requirement_id,
                )
                .returning(*_REQ_COLS)
            ).one()
            created.append(_req_out(row))
        s.commit()
    return created


def requirements_summary(node_id: str) -> list[RequirementSummary]:
    """노드 전체 프로젝트의 요구조건 충족 현황 집계 (결정론, LLM 0회)."""
    with session() as s:
        rows = s.execute(
            select(
                project_requirements.c.project_id,
                project_requirements.c.status,
                func.count(),
            )
            .where(project_requirements.c.node_id == node_id)
            .group_by(project_requirements.c.project_id, project_requirements.c.status)
        ).all()
    agg: dict[UUID, RequirementSummary] = {}
    for pid, status, count in rows:
        summary = agg.setdefault(pid, RequirementSummary(project_id=pid))
        summary.total += count
        if status == "satisfied":
            summary.satisfied += count
        elif status == "verifying":
            summary.verifying += count
        elif status == "failed":
            summary.failed += count
    return list(agg.values())


# --------------------------------------------------------------------------
# Dashboard pages (노션식 중첩 하위 페이지 — subpage 블록이 참조)
# --------------------------------------------------------------------------
class PageIn(BaseModel):
    # Offline-first clients may allocate the stable UUID before the first
    # network round-trip.  Omitted keeps the legacy server-generated ID path.
    page_id: UUID | None = None
    title: str = "새 페이지"
    icon: str | None = None
    body: str | None = None
    # 'memo' = 메모 목록 최상위, 'page' = 본문 안 중첩 하위 페이지(기본)
    kind: str = "page"


class PagePatch(BaseModel):
    title: str | None = None
    icon: str | None = None
    body: str | None = None


class DashboardPage(BaseModel):
    page_id: UUID
    title: str
    icon: str | None = None
    body: str | None = None
    kind: str = "page"
    owner_id: UUID | None = None


class DashboardPageSummary(BaseModel):
    page_id: UUID
    title: str
    icon: str | None = None
    kind: str = "page"
    updated_at: datetime | None = None
    owner_id: UUID | None = None
    # Short plain-text snippet for Apple Notes-style lists. Never includes the
    # full body or inline media payload.
    preview: str | None = None


class PageIdCollision(Exception):
    """A client-generated page ID belongs to another owner or node.

    Keep the exception deliberately context-free: the API maps it to a generic
    409 and must not reveal which other owner/node already holds the UUID.
    """

    def __init__(self) -> None:
        super().__init__("page_id already exists")


def _page_out(row) -> DashboardPage:
    return DashboardPage(
        page_id=row.page_id,
        title=row.title,
        icon=row.icon,
        body=row.body,
        kind=row.kind,
        owner_id=row.owner_id,
    )


_PAGE_COLS = (
    dashboard_pages.c.page_id,
    dashboard_pages.c.title,
    dashboard_pages.c.icon,
    dashboard_pages.c.body,
    dashboard_pages.c.kind,
    dashboard_pages.c.owner_id,
)


def _page_preview(body_prefix: str | None) -> str | None:
    """Extract at most 90 visible characters from a capped body prefix.

    `list_pages` asks Postgres for only the first 4KB, so a large inline image can
    never turn the summary endpoint into a full-body transfer. Complete BlockNote
    JSON is walked normally; a truncated prefix falls back to its early `text`
    fields. Legacy plain text uses the first non-empty line.
    """
    if not body_prefix or not body_prefix.strip():
        return None

    def clean(value: str) -> str | None:
        normalized = " ".join(value.split()).strip()
        return normalized[:90] if normalized else None

    stripped = body_prefix.strip()
    if not stripped.startswith("["):
        return clean(stripped)

    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError, RecursionError):
        parts: list[str] = []
        for raw in re.findall(r'"text"\s*:\s*"((?:\\.|[^"\\])*)"', stripped):
            try:
                parts.append(json.loads(f'"{raw}"'))
            except (TypeError, ValueError):
                continue
        return clean(" ".join(parts))

    parts: list[str] = []
    text_length = 0
    visited = 0
    stack = [parsed]
    # Iterative + capped: a stored hostile/deep document cannot wedge every list
    # request with recursion or unbounded traversal.
    while stack and visited < 512 and text_length < 120:
        value = stack.pop()
        visited += 1
        if isinstance(value, list):
            stack.extend(reversed(value[:512]))
        elif isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str):
                parts.append(text)
                text_length += len(text)
            children = value.get("children")
            content = value.get("content")
            if children is not None:
                stack.append(children)
            if content is not None:
                stack.append(content)
    return clean(" ".join(parts))


def list_pages(
    node_id: str,
    kind: str = "memo",
    *,
    owner_id: UUID | None = None,
    scope: str = "all",
    include_preview: bool = False,
) -> list[DashboardPageSummary]:
    """메모/페이지 목록.

    scope="all" (기본, 레거시 호환): node의 모든 memo. `/dashboard/memo` 유지.
    scope="mine": 내 소유(owner_id==me) + 레거시 공용(owner_id IS NULL). owner_id 필요.
    (scope="shared"는 note_shares 조인이 필요해 별도 함수 list_shared_pages를 쓴다.)
    """
    conds = [
        dashboard_pages.c.node_id == node_id,
        dashboard_pages.c.kind == kind,
    ]
    if scope == "mine" and owner_id is not None:
        conds.append(
            or_(
                dashboard_pages.c.owner_id == owner_id,
                dashboard_pages.c.owner_id.is_(None),
            )
        )
    # Preview is body-derived private data. The legacy scope=all endpoint keeps
    # its old title-only behavior; only the caller-filtered scope=mine path may
    # ask Postgres for a capped body prefix.
    preview_expr = (
        func.left(dashboard_pages.c.body, 4000)
        if include_preview and scope == "mine" and owner_id is not None
        else literal(None)
    ).label("body_prefix")
    with session() as s:
        rows = s.execute(
            select(
                dashboard_pages.c.page_id,
                dashboard_pages.c.title,
                dashboard_pages.c.icon,
                dashboard_pages.c.kind,
                dashboard_pages.c.updated_at,
                dashboard_pages.c.owner_id,
                preview_expr,
            )
            .where(*conds)
            .order_by(dashboard_pages.c.updated_at.desc())
        ).all()
    return [
        DashboardPageSummary(
            page_id=r.page_id,
            title=r.title,
            icon=r.icon,
            kind=r.kind,
            updated_at=r.updated_at,
            owner_id=r.owner_id,
            preview=(_page_preview(r.body_prefix) if include_preview and scope == "mine" else None),
        )
        for r in rows
    ]


def create_page(node_id: str, body: PageIn, *, owner_id: UUID | None = None) -> DashboardPage:
    page_id = body.page_id or uuid.uuid4()
    normalized_kind = body.kind or "page"
    normalized_title = (body.title or "새 페이지").strip() or "새 페이지"
    owner_match = (
        dashboard_pages.c.owner_id.is_(None)
        if owner_id is None
        else dashboard_pages.c.owner_id == owner_id
    )
    with session() as s:
        insert = pg_insert(dashboard_pages).values(
            page_id=page_id,
            node_id=node_id,
            kind=normalized_kind,
            title=normalized_title,
            icon=body.icon,
            body=body.body,
            owner_id=owner_id,
        )
        row = s.execute(
            insert.on_conflict_do_update(
                index_elements=[dashboard_pages.c.page_id],
                # Offline create payloads are final-value snapshots. If the
                # first response was lost and the user typed more before retry,
                # the exact same owner/node may advance title/icon/body. Kind is
                # immutable so an accidental memo/subpage UUID reuse is rejected.
                set_={
                    "title": insert.excluded.title,
                    "icon": insert.excluded.icon,
                    "body": insert.excluded.body,
                    "updated_at": func.now(),
                },
                where=and_(
                    dashboard_pages.c.node_id == node_id,
                    owner_match,
                    dashboard_pages.c.kind == normalized_kind,
                ),
            ).returning(*_PAGE_COLS)
        ).first()
        if row is None:
            # WHERE rejected a cross-owner, cross-node, or cross-kind collision.
            # No colliding row is loaded, so the caller learns no ownership data.
            s.rollback()
            raise PageIdCollision()
        s.commit()
    return _page_out(row)


def get_page(node_id: str, page_id: UUID) -> DashboardPage:
    with session() as s:
        row = s.execute(
            select(*_PAGE_COLS).where(
                dashboard_pages.c.node_id == node_id,
                dashboard_pages.c.page_id == page_id,
            )
        ).first()
    if row is None:
        raise LookupError("page not found")
    return _page_out(row)


def update_page(node_id: str, page_id: UUID, patch: PagePatch) -> DashboardPage:
    values: dict = {}
    if patch.title is not None:
        values["title"] = patch.title.strip() or "새 페이지"
    if patch.icon is not None:
        values["icon"] = patch.icon or None
    if patch.body is not None:
        values["body"] = patch.body or None
    if not values:
        return get_page(node_id, page_id)
    values["updated_at"] = func.now()
    with session() as s:
        row = s.execute(
            dashboard_pages.update()
            .where(
                dashboard_pages.c.node_id == node_id,
                dashboard_pages.c.page_id == page_id,
            )
            .values(**values)
            .returning(*_PAGE_COLS)
        ).first()
        s.commit()
    if row is None:
        raise LookupError("page not found")
    return _page_out(row)


def delete_page(node_id: str, page_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(dashboard_pages).where(
                dashboard_pages.c.node_id == node_id,
                dashboard_pages.c.page_id == page_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("page not found")


# --------------------------------------------------------------------------
# Project assignments (member ↔ project ↔ role)
# --------------------------------------------------------------------------
class AssignmentIn(BaseModel):
    project_id: UUID
    member_id: UUID
    role: str | None = None
    sort_order: int = 0


class Assignment(BaseModel):
    assignment_id: UUID
    project_id: UUID
    member_id: UUID
    project_name: str | None = None
    member_name: str | None = None
    role: str | None = None
    sort_order: int = 0


class RoleUpdate(BaseModel):
    role: str | None = None


def _assignment_out(row) -> Assignment:
    return Assignment(
        assignment_id=row.assignment_id,
        project_id=row.project_id,
        member_id=row.member_id,
        project_name=row.project_name,
        member_name=row.member_name,
        role=row.role,
        sort_order=row.sort_order,
    )


# 역할 옵션 기본값(FE 하드코딩 ROLE_COLOR와 동일). 노드에 옵션이 하나도 없을 때 시드한다.
DEFAULT_PROJECT_ROLES: list[tuple[str, str]] = [
    ("PM", "blue"),
    ("기획", "yellow"),
    ("개발", "green"),
    ("AX", "purple"),
    ("디자인", "pink"),
    ("마케팅", "orange"),
    ("운영", "teal"),
    ("대표", "red"),
    ("리드", "brown"),
    ("기타", "gray"),
]


class ProjectRole(BaseModel):
    role_id: UUID
    name: str
    color: str | None = None
    sort_order: int = 0


class ProjectRoleIn(BaseModel):
    name: str
    color: str | None = None


class ProjectRolePatch(BaseModel):
    name: str | None = None
    color: str | None = None
    sort_order: int | None = None


def _project_role_out(row) -> ProjectRole:
    return ProjectRole(
        role_id=row.role_id, name=row.name, color=row.color, sort_order=row.sort_order
    )


def ensure_default_project_roles(node_id: str) -> None:
    """노드에 역할 옵션이 하나도 없으면 기본 목록을 시드한다(첫 list 시 1회)."""
    with session() as s:
        has_any = s.execute(
            select(project_roles.c.role_id).where(project_roles.c.node_id == node_id).limit(1)
        ).first()
        if has_any is not None:
            return
        for index, (name, color) in enumerate(DEFAULT_PROJECT_ROLES):
            s.execute(
                pg_insert(project_roles)
                .values(
                    role_id=uuid.uuid4(),
                    node_id=node_id,
                    name=name,
                    color=color,
                    sort_order=index,
                )
                .on_conflict_do_nothing(index_elements=["node_id", "name"])
            )
        s.commit()


def list_project_roles(node_id: str) -> list[ProjectRole]:
    ensure_default_project_roles(node_id)
    with session() as s:
        rows = s.execute(
            select(
                project_roles.c.role_id,
                project_roles.c.name,
                project_roles.c.color,
                project_roles.c.sort_order,
            )
            .where(project_roles.c.node_id == node_id)
            .order_by(project_roles.c.sort_order, project_roles.c.name)
        ).all()
    return [_project_role_out(r) for r in rows]


def create_project_role(node_id: str, body: ProjectRoleIn) -> ProjectRole:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    role_id = uuid.uuid4()
    with session() as s:
        next_order = (
            s.execute(
                select(func.coalesce(func.max(project_roles.c.sort_order), -1)).where(
                    project_roles.c.node_id == node_id
                )
            ).scalar()
            or -1
        ) + 1
        row = s.execute(
            pg_insert(project_roles)
            .values(
                role_id=role_id,
                node_id=node_id,
                name=name,
                color=body.color,
                sort_order=next_order,
            )
            .on_conflict_do_update(
                index_elements=["node_id", "name"],
                set_={"color": body.color, "updated_at": func.now()},
            )
            .returning(
                project_roles.c.role_id,
                project_roles.c.name,
                project_roles.c.color,
                project_roles.c.sort_order,
            )
        ).one()
        s.commit()
    return _project_role_out(row)


def update_project_role(node_id: str, role_id: UUID, body: ProjectRolePatch) -> ProjectRole:
    """역할 옵션 이름/색/정렬 수정. 이름을 바꾸면 그 역할을 쓰던 배정에 cascade 반영한다."""
    with session() as s:
        current = s.execute(
            select(project_roles.c.name).where(
                project_roles.c.role_id == role_id,
                project_roles.c.node_id == node_id,
            )
        ).first()
        if current is None:
            raise LookupError("role not found")
        old_name = current.name
        values: dict = {}
        if body.name is not None:
            new_name = body.name.strip()
            if not new_name:
                raise ValueError("name required")
            if new_name != old_name:
                clash = s.execute(
                    select(project_roles.c.role_id).where(
                        project_roles.c.node_id == node_id,
                        project_roles.c.name == new_name,
                        project_roles.c.role_id != role_id,
                    )
                ).first()
                if clash is not None:
                    raise ValueError("이미 있는 역할 이름이에요")
            values["name"] = new_name
        if body.color is not None:
            values["color"] = body.color
        if body.sort_order is not None:
            values["sort_order"] = body.sort_order
        if values:
            values["updated_at"] = func.now()
            s.execute(
                project_roles.update()
                .where(
                    project_roles.c.role_id == role_id,
                    project_roles.c.node_id == node_id,
                )
                .values(**values)
            )
            # 이름 변경은 그 역할을 쓰던 배정(자유 텍스트)에 cascade 반영.
            if "name" in values and values["name"] != old_name:
                s.execute(
                    project_assignments.update()
                    .where(
                        project_assignments.c.node_id == node_id,
                        project_assignments.c.role == old_name,
                    )
                    .values(role=values["name"], updated_at=func.now())
                )
            s.commit()
        row = s.execute(
            select(
                project_roles.c.role_id,
                project_roles.c.name,
                project_roles.c.color,
                project_roles.c.sort_order,
            ).where(project_roles.c.role_id == role_id)
        ).one()
    return _project_role_out(row)


def delete_project_role(node_id: str, role_id: UUID) -> None:
    """역할 옵션 삭제. 그 역할을 쓰던 배정의 role은 비운다(NULL)."""
    with session() as s:
        current = s.execute(
            select(project_roles.c.name).where(
                project_roles.c.role_id == role_id,
                project_roles.c.node_id == node_id,
            )
        ).first()
        if current is None:
            raise LookupError("role not found")
        s.execute(
            project_assignments.update()
            .where(
                project_assignments.c.node_id == node_id,
                project_assignments.c.role == current.name,
            )
            .values(role=None, updated_at=func.now())
        )
        s.execute(
            project_roles.delete().where(
                project_roles.c.role_id == role_id,
                project_roles.c.node_id == node_id,
            )
        )
        s.commit()


def list_assignments(
    node_id: str, project_id: UUID | None = None, member_id: UUID | None = None
) -> list[Assignment]:
    cols = (
        project_assignments.c.assignment_id,
        project_assignments.c.project_id,
        project_assignments.c.member_id,
        dashboard_projects.c.name.label("project_name"),
        team_members.c.name.label("member_name"),
        project_assignments.c.role,
        project_assignments.c.sort_order,
    )
    stmt = (
        select(*cols)
        .select_from(
            project_assignments.outerjoin(
                dashboard_projects,
                dashboard_projects.c.project_id == project_assignments.c.project_id,
            ).outerjoin(
                team_members,
                team_members.c.member_id == project_assignments.c.member_id,
            )
        )
        .where(project_assignments.c.node_id == node_id)
        .order_by(project_assignments.c.sort_order, team_members.c.name)
    )
    if project_id is not None:
        stmt = stmt.where(project_assignments.c.project_id == project_id)
    if member_id is not None:
        stmt = stmt.where(project_assignments.c.member_id == member_id)
    with session() as s:
        rows = s.execute(stmt).all()
    return [_assignment_out(r) for r in rows]


class ProjectBoardTask(BaseModel):
    """A 회사 공개(scope='company') personal-board task surfaced to the company.

    These are tasks team members placed in a 회사 프로젝트 채널 on their 내 보드.
    Visible to all company members (사용자 결정: 공개 범위 = 회사 구성원 전체)."""

    task_id: UUID
    title: str
    status: str
    completed: bool
    priority: str
    scheduled_date: date | None = None
    due_date: date | None = None
    owner_user_id: UUID | None = None
    owner_name: str | None = None


def list_project_board_tasks(node_id: str, project_id: UUID) -> list[ProjectBoardTask]:
    """회사 프로젝트 채널에 등록된 회사 공개 업무를 프로젝트 단위로 모은다.

    경계: scope='company' 행만 읽는다. 팀원의 사적 채널 업무(scope='personal')는
    절대 포함하지 않는다(개인 경계 fail-closed). node 안의 모든 워크스페이스를 가로질러
    같은 회사 프로젝트(company_project_id)에 속한 업무를 합친다.

    추가로 읽기 시점에 '그 업무 소유자가 지금도 이 프로젝트 담당인지'를 재확인한다.
    담당이 빠진 뒤 소유자가 보드를 다시 안 열어 scope 되돌림이 늦어지더라도, 회사쪽
    노출은 즉시 끊긴다(되돌림에 의존하지 않는 권위적 경계)."""
    with session() as s:
        # 소유자가 현재도 이 프로젝트 담당인지 EXISTS로 재검증.
        still_assigned = (
            select(1)
            .select_from(
                team_members.join(
                    project_assignments,
                    (project_assignments.c.member_id == team_members.c.member_id)
                    & (project_assignments.c.node_id == team_members.c.node_id),
                )
            )
            .where(
                team_members.c.node_id == node_id,
                team_members.c.user_id == personal_board_tasks.c.user_id,
                project_assignments.c.project_id == project_id,
            )
            .exists()
        )
        rows = s.execute(
            select(
                personal_board_tasks.c.task_id,
                personal_board_tasks.c.title,
                personal_board_tasks.c.status,
                personal_board_tasks.c.priority,
                personal_board_tasks.c.scheduled_date,
                personal_board_tasks.c.due_date,
                personal_board_tasks.c.user_id,
            )
            .select_from(
                personal_board_tasks.join(
                    personal_board_workspaces,
                    personal_board_workspaces.c.workspace_id == personal_board_tasks.c.workspace_id,
                )
            )
            .where(
                personal_board_workspaces.c.node_id == node_id,
                personal_board_tasks.c.scope == "company",
                personal_board_tasks.c.company_project_id == project_id,
                personal_board_tasks.c.status != "archived",
                still_assigned,
            )
            .order_by(
                personal_board_tasks.c.scheduled_date.asc(),
                personal_board_tasks.c.created_at.asc(),
            )
        ).all()
        names = dict(
            s.execute(
                select(team_members.c.user_id, team_members.c.name).where(
                    team_members.c.node_id == node_id,
                    team_members.c.user_id.is_not(None),
                )
            ).all()
        )
    return [
        ProjectBoardTask(
            task_id=r.task_id,
            title=r.title,
            status=r.status,
            completed=r.status == "done",
            priority=r.priority,
            scheduled_date=r.scheduled_date,
            due_date=r.due_date,
            owner_user_id=r.user_id,
            owner_name=names.get(r.user_id),
        )
        for r in rows
    ]


def upsert_assignment(
    node_id: str, body: AssignmentIn, actor_user_id: UUID | None = None
) -> Assignment:
    """Assign a member to a project (one role per member/project). Idempotent on
    (project_id, member_id): re-assigning updates the role."""
    with session() as s:
        s.execute(
            pg_insert(project_assignments)
            .values(
                assignment_id=uuid.uuid4(),
                node_id=node_id,
                project_id=body.project_id,
                member_id=body.member_id,
                role=body.role,
                sort_order=body.sort_order,
            )
            .on_conflict_do_update(
                index_elements=["project_id", "member_id"],
                set_={"role": body.role, "updated_at": func.now()},
            )
        )
        member = s.execute(
            select(team_members.c.name).where(team_members.c.member_id == body.member_id)
        ).first()
        _log_activity(
            s,
            node_id,
            body.project_id,
            actor_user_id,
            "assignment",
            "assign",
            entity_id=str(body.member_id),
            after=f"{member.name if member else body.member_id} · {body.role or ''}".strip(" ·"),
        )
        s.commit()
    rows = list_assignments(node_id, project_id=body.project_id, member_id=body.member_id)
    if not rows:
        raise LookupError("assignment not found")
    return rows[0]


def update_assignment(node_id: str, assignment_id: UUID, body: RoleUpdate) -> Assignment:
    with session() as s:
        result = s.execute(
            project_assignments.update()
            .where(
                project_assignments.c.node_id == node_id,
                project_assignments.c.assignment_id == assignment_id,
            )
            .values(role=body.role, updated_at=func.now())
            .returning(project_assignments.c.assignment_id)
        ).first()
        s.commit()
    if result is None:
        raise LookupError("assignment not found")
    rows = [a for a in list_assignments(node_id) if a.assignment_id == assignment_id]
    return rows[0]


def delete_assignment(node_id: str, assignment_id: UUID, actor_user_id: UUID | None = None) -> None:
    with session() as s:
        old = s.execute(
            select(project_assignments.c.project_id, team_members.c.name)
            .select_from(
                project_assignments.outerjoin(
                    team_members, project_assignments.c.member_id == team_members.c.member_id
                )
            )
            .where(
                project_assignments.c.node_id == node_id,
                project_assignments.c.assignment_id == assignment_id,
            )
        ).first()
        result = s.execute(
            delete(project_assignments).where(
                project_assignments.c.node_id == node_id,
                project_assignments.c.assignment_id == assignment_id,
            )
        )
        if result.rowcount and old is not None:
            _log_activity(
                s,
                node_id,
                old.project_id,
                actor_user_id,
                "assignment",
                "unassign",
                before=old.name,
            )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("assignment not found")


# --------------------------------------------------------------------------
# 프로젝트 활동 피드 (마이그레이션 0094)
# --------------------------------------------------------------------------
class ProjectActivityItem(BaseModel):
    activity_id: UUID
    actor_user_id: UUID | None = None
    actor_name: str | None = None
    entity_type: str
    entity_id: str | None = None
    action: str
    field: str | None = None
    before: str | None = None
    after: str | None = None
    created_at: datetime


def list_project_activity(
    node_id: str, project_id: UUID, limit: int = 50
) -> list[ProjectActivityItem]:
    """프로젝트 최근 활동 (최신순). actor 이름은 users.display_name."""
    with session() as s:
        rows = s.execute(
            select(project_activity, users.c.display_name)
            .select_from(
                project_activity.outerjoin(
                    users, project_activity.c.actor_user_id == users.c.user_id
                )
            )
            .where(
                project_activity.c.node_id == node_id,
                project_activity.c.project_id == project_id,
            )
            .order_by(project_activity.c.created_at.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
    return [
        ProjectActivityItem(
            activity_id=r.activity_id,
            actor_user_id=r.actor_user_id,
            actor_name=r.display_name,
            entity_type=r.entity_type,
            entity_id=r.entity_id,
            action=r.action,
            field=r.field,
            before=r.before,
            after=r.after,
            created_at=r.created_at,
        )
        for r in rows
    ]


def row_props_for_log(node_id: str, database_id: UUID, row_id: UUID) -> dict | None:
    """삭제 직전 행 제목 로그용 props 스냅샷 (없으면 None)."""
    with session() as s:
        row = s.execute(
            select(project_database_rows.c.props).where(
                project_database_rows.c.node_id == node_id,
                project_database_rows.c.database_id == database_id,
                project_database_rows.c.row_id == row_id,
            )
        ).first()
    return dict(row.props) if row is not None and row.props else None


def log_row_activity(
    node_id: str,
    database_id: UUID,
    actor_user_id: UUID | None,
    action: str,
    row_id: UUID,
    props: dict | None = None,
) -> None:
    """보드/테이블 행 생성·삭제 활동 로그 (라우트 레이어에서 호출, 단독 커밋).

    project 소속이 아닌 데이터베이스(project_id NULL)는 조용히 스킵한다.
    제목은 title 속성 값에서 뽑는다(없으면 행 id만)."""
    with session() as s:
        db = s.execute(
            select(project_databases.c.project_id, project_databases.c.properties).where(
                project_databases.c.node_id == node_id,
                project_databases.c.database_id == database_id,
            )
        ).first()
        if db is None or db.project_id is None:
            return
        title = None
        if props:
            for prop in db.properties or []:
                if isinstance(prop, dict) and prop.get("type") == "title":
                    value = props.get(str(prop.get("id")))
                    if isinstance(value, str) and value.strip():
                        title = value.strip()[:120]
                    break
        _log_activity(
            s,
            node_id,
            db.project_id,
            actor_user_id,
            "row",
            action,
            entity_id=str(row_id),
            after=title,
        )
        s.commit()


# --------------------------------------------------------------------------
# Weekly / Monthly plans & retros
# --------------------------------------------------------------------------
class PlanItem(BaseModel):
    id: str
    text: str
    done: bool = False
    # 노션식 상세 본문(BlockNote 블록 JSON 문자열). 항목 클릭 시 편집.
    detail: str | None = None
    # 프로젝트별 하위 칸 태그(예: AX의 "대본"/"아트인"). 없으면 단일 칸.
    group: str | None = None
    # 담당자(팀원 id, JSONB 저장이라 문자열). 부여되면 그 사람 개인 보드 주간
    # 플랜에 반영된다.
    assignee_member_id: str | None = None
    # 연결된 KPI(dashboard_kpis.kpi_id). 주간 실행을 월간/연간 KPI에 엮는다.
    kpi_id: str | None = None
    # 계획 점검 달성 점수(0~10 정수). None=미채점 — 0은 유효한 점수라서
    # 읽는 쪽은 truthiness가 아니라 `is not None`으로 판별해야 한다.
    # 점수가 기록되면 저장 시 done이 score>=7로 파생된다(_derive_done_from_score
    # — 점검 UI의 체크박스 통합, 오너 결정 2026-07-12). 점수 없는 항목의 done은
    # 주중 체크박스 값 그대로다.
    score: int | None = Field(default=None, ge=0, le=10)


class ItemProgress(BaseModel):
    """담당자가 그 회사 할당 항목을 개인 주간 목표로 받아 진행한 정도(집계만).

    read-only 표시용 — 서브태스크 제목/메모 같은 사적 내용은 절대 담지 않는다
    (개인 보드 owner-only 경계의 진행률-only carve-out)."""

    has_objective: bool = False  # 담당자가 주간플랜을 열어 목표가 생겼는지
    subtasks_total: int = 0
    subtasks_done: int = 0
    completed: bool = False  # 담당자 목표 완료 여부(회사 plan_item.done과는 별개)


class WeeklyEntry(BaseModel):
    entry_id: UUID | None = None
    project_id: UUID
    week_start: date
    plan_items: list[PlanItem] = Field(default_factory=list)
    retro_items: list[PlanItem] = Field(default_factory=list)
    # Loaded row timestamp; None when no row exists. The client echoes it back as
    # WeeklyUpsert.base_updated_at so the destructive-empty guard can tell an
    # intentional clear (loaded row) from an accidental empty (never loaded).
    updated_at: datetime | None = None
    # plan_item.id -> 담당자 진행률(집계). 읽기 전용, 저장 안 됨(WeeklyUpsert에 없음).
    assignee_progress: dict[str, ItemProgress] = Field(default_factory=dict)


class WeeklyUpsert(BaseModel):
    project_id: UUID
    week_start: date
    plan_items: list[PlanItem] = Field(default_factory=list)
    retro_items: list[PlanItem] = Field(default_factory=list)
    # Optimistic-concurrency base for the destructive-empty guard only.
    base_updated_at: datetime | None = None


class MonthlyEntry(BaseModel):
    entry_id: UUID | None = None
    project_id: UUID
    month: date
    plan_items: list[PlanItem] = Field(default_factory=list)
    retro_items: list[PlanItem] = Field(default_factory=list)
    updated_at: datetime | None = None


class MonthlyUpsert(BaseModel):
    project_id: UUID
    month: date
    plan_items: list[PlanItem] = Field(default_factory=list)
    retro_items: list[PlanItem] = Field(default_factory=list)
    base_updated_at: datetime | None = None


class EntryHistory(BaseModel):
    """A pre-overwrite snapshot of a weekly/monthly entry (newest first)."""

    period: date
    plan_items: list[PlanItem] = Field(default_factory=list)
    retro_items: list[PlanItem] = Field(default_factory=list)
    prev_updated_at: datetime | None = None
    snapshot_at: datetime | None = None


class EntryConflict(Exception):
    """A destructive empty write was blocked because the client did not echo the
    current row timestamp (stale/missing base_updated_at). Carries the unchanged
    current entry so the route can return it with HTTP 409."""

    def __init__(self, current):
        self.current = current
        super().__init__("destructive empty write blocked: stale/missing base_updated_at")


def _items(raw) -> list[PlanItem]:
    return [PlanItem(**it) for it in (raw or [])]


def _is_empty(plan: list, retro: list) -> bool:
    return len(plan) == 0 and len(retro) == 0


def _content_changed(cur_plan, cur_retro, new_plan, new_retro) -> bool:
    return (cur_plan or []) != new_plan or (cur_retro or []) != new_retro


def _assignee_progress(
    node_id: str, plan_items: list[PlanItem], ws: date
) -> dict[str, ItemProgress]:
    """담당 지정된 plan 항목들의 담당자 진행률(집계)을 모은다. 개인 보드 진행률-only
    carve-out — 서브태스크 제목 등 사적 내용은 안 가져온다(personal_board 헬퍼가 보장)."""
    pairs = [(it.id, it.assignee_member_id) for it in plan_items if it.assignee_member_id and it.id]
    if not pairs:
        return {}
    from orthus.personal_board import assignee_progress_for_plan_items

    raw = assignee_progress_for_plan_items(node_id, pairs, ws)
    return {k: ItemProgress(**v) for k, v in raw.items()}


def get_weekly(node_id: str, project_id: UUID, ref: date) -> WeeklyEntry:
    """Return the (project, week) entry. The retro view reads plan_items from
    this same row, so plans written for a week auto-appear in that week's retro."""
    ws = week_start_sunday(ref)
    with session() as s:
        row = s.execute(
            select(
                weekly_entries.c.entry_id,
                weekly_entries.c.plan_items,
                weekly_entries.c.retro_items,
                weekly_entries.c.updated_at,
            ).where(
                weekly_entries.c.node_id == node_id,
                weekly_entries.c.project_id == project_id,
                weekly_entries.c.week_start == ws,
            )
        ).first()
    if row is None:
        return WeeklyEntry(project_id=project_id, week_start=ws)
    plan_items = _items(row.plan_items)
    return WeeklyEntry(
        entry_id=row.entry_id,
        project_id=project_id,
        week_start=ws,
        plan_items=plan_items,
        retro_items=_items(row.retro_items),
        updated_at=row.updated_at,
        assignee_progress=_assignee_progress(node_id, plan_items, ws),
    )


def _derive_done_from_score(items: list[PlanItem]) -> list[dict]:
    """계획 점검 통합 규칙: 달성 점수가 있으면 done은 score>=7로 파생된다.

    점검(회고) UI에서 체크박스를 없애고 점수 단일 입력으로 통합하면서(오너 결정
    2026-07-12) done의 SoR을 점수에 종속시킨다 — 7점 이상=완료(톤 밴드 7~10=달성
    과 동일 기준). 점수가 없는 항목(주중 체크박스 등)은 done을 그대로 둔다.
    score=0도 유효한 채점이므로 is not None으로만 분기한다."""
    out = []
    for it in items:
        d = it.model_dump()
        if d.get("score") is not None:
            d["done"] = d["score"] >= 7
        out.append(d)
    return out


def upsert_weekly(node_id: str, body: WeeklyUpsert) -> WeeklyEntry:
    ws = week_start_sunday(body.week_start)
    plan = _derive_done_from_score(body.plan_items)
    retro = [it.model_dump() for it in body.retro_items]
    # Select-current (FOR UPDATE), guard, snapshot, and write in one transaction
    # so the destructive-empty check cannot race a concurrent save.
    with session() as s:
        cur = s.execute(
            select(
                weekly_entries.c.entry_id,
                weekly_entries.c.plan_items,
                weekly_entries.c.retro_items,
                weekly_entries.c.updated_at,
            )
            .where(
                weekly_entries.c.node_id == node_id,
                weekly_entries.c.project_id == body.project_id,
                weekly_entries.c.week_start == ws,
            )
            .with_for_update()
        ).first()

        if cur is not None and _is_empty(plan, retro):
            current_nonempty = bool(cur.plan_items) or bool(cur.retro_items)
            stale = body.base_updated_at is None or body.base_updated_at != cur.updated_at
            if current_nonempty and stale:
                raise EntryConflict(
                    WeeklyEntry(
                        entry_id=cur.entry_id,
                        project_id=body.project_id,
                        week_start=ws,
                        plan_items=_items(cur.plan_items),
                        retro_items=_items(cur.retro_items),
                        updated_at=cur.updated_at,
                    )
                )

        if cur is not None and _content_changed(cur.plan_items, cur.retro_items, plan, retro):
            s.execute(
                dashboard_entry_history.insert().values(
                    history_id=uuid.uuid4(),
                    node_id=node_id,
                    project_id=body.project_id,
                    period_kind="weekly",
                    period=ws,
                    plan_items=cur.plan_items or [],
                    retro_items=cur.retro_items or [],
                    prev_updated_at=cur.updated_at,
                )
            )

        row = s.execute(
            pg_insert(weekly_entries)
            .values(
                entry_id=uuid.uuid4(),
                node_id=node_id,
                project_id=body.project_id,
                week_start=ws,
                plan_items=plan,
                retro_items=retro,
            )
            .on_conflict_do_update(
                index_elements=["node_id", "project_id", "week_start"],
                set_={"plan_items": plan, "retro_items": retro, "updated_at": func.now()},
            )
            .returning(weekly_entries.c.entry_id, weekly_entries.c.updated_at)
        ).one()
        # 담당 지정 즉시 반영: 이 저장으로 담당이 바뀔 수 있는 팀원 = 이전 항목 담당 ∪ 새
        # 항목 담당(제거된 담당의 stale 목표까지 그 팀원 보드에서 정리하려면 합집합 필요).
        # plan_items가 실제로 바뀐 저장만 push한다(회고-only/무변경 저장의 중복 sync를 막고,
        # 담당자 materialize 동시성 창도 줄인다). 미변경이면 담당자 보드는 이미 최신이다.
        plan_changed = cur is None or (cur.plan_items or []) != plan
        affected_members = (
            {
                str(it.get("assignee_member_id"))
                for it in (list(cur.plan_items or []) if cur is not None else []) + plan
                if it.get("assignee_member_id")
            }
            if plan_changed
            else set()
        )
        s.commit()
    sync_weekly_meeting(node_id, ws)
    # 담당자 보드로 push(pull을 안 기다리고 지정 즉시 그 사람 보드에 넣는다). best-effort —
    # 실패해도 저장은 성공 유지하고, 담당자가 보드를 열 때 기존 pull이 백필한다.
    if affected_members:
        from orthus.personal_board import sync_company_plan_for_members

        try:
            sync_company_plan_for_members(node_id, ws, affected_members)
        except Exception:
            logger.exception("company plan board push failed for week %s", ws)
    realtime.publish(
        {
            "kind": "weekly",
            "node_id": node_id,
            "project_id": str(body.project_id),
            "week_start": ws.isoformat(),
        }
    )
    return WeeklyEntry(
        entry_id=row.entry_id,
        project_id=body.project_id,
        week_start=ws,
        plan_items=body.plan_items,
        retro_items=body.retro_items,
        updated_at=row.updated_at,
    )


def list_weekly_history(node_id: str, project_id: UUID, ref: date) -> list[EntryHistory]:
    ws = week_start_sunday(ref)
    with session() as s:
        rows = s.execute(
            select(
                dashboard_entry_history.c.period,
                dashboard_entry_history.c.plan_items,
                dashboard_entry_history.c.retro_items,
                dashboard_entry_history.c.prev_updated_at,
                dashboard_entry_history.c.snapshot_at,
            )
            .where(
                dashboard_entry_history.c.node_id == node_id,
                dashboard_entry_history.c.period_kind == "weekly",
                dashboard_entry_history.c.project_id == project_id,
                dashboard_entry_history.c.period == ws,
            )
            .order_by(dashboard_entry_history.c.snapshot_at.desc())
            .limit(20)
        ).all()
    return [
        EntryHistory(
            period=r.period,
            plan_items=_items(r.plan_items),
            retro_items=_items(r.retro_items),
            prev_updated_at=r.prev_updated_at,
            snapshot_at=r.snapshot_at,
        )
        for r in rows
    ]


def get_monthly(node_id: str, project_id: UUID, ref: date) -> MonthlyEntry:
    mf = month_first(ref)
    with session() as s:
        row = s.execute(
            select(
                monthly_entries.c.entry_id,
                monthly_entries.c.plan_items,
                monthly_entries.c.retro_items,
                monthly_entries.c.updated_at,
            ).where(
                monthly_entries.c.node_id == node_id,
                monthly_entries.c.project_id == project_id,
                monthly_entries.c.month == mf,
            )
        ).first()
    if row is None:
        return MonthlyEntry(project_id=project_id, month=mf)
    return MonthlyEntry(
        entry_id=row.entry_id,
        project_id=project_id,
        month=mf,
        plan_items=_items(row.plan_items),
        retro_items=_items(row.retro_items),
        updated_at=row.updated_at,
    )


def upsert_monthly(node_id: str, body: MonthlyUpsert) -> MonthlyEntry:
    mf = month_first(body.month)
    plan = _derive_done_from_score(body.plan_items)
    retro = [it.model_dump() for it in body.retro_items]
    with session() as s:
        cur = s.execute(
            select(
                monthly_entries.c.entry_id,
                monthly_entries.c.plan_items,
                monthly_entries.c.retro_items,
                monthly_entries.c.updated_at,
            )
            .where(
                monthly_entries.c.node_id == node_id,
                monthly_entries.c.project_id == body.project_id,
                monthly_entries.c.month == mf,
            )
            .with_for_update()
        ).first()

        if cur is not None and _is_empty(plan, retro):
            current_nonempty = bool(cur.plan_items) or bool(cur.retro_items)
            stale = body.base_updated_at is None or body.base_updated_at != cur.updated_at
            if current_nonempty and stale:
                raise EntryConflict(
                    MonthlyEntry(
                        entry_id=cur.entry_id,
                        project_id=body.project_id,
                        month=mf,
                        plan_items=_items(cur.plan_items),
                        retro_items=_items(cur.retro_items),
                        updated_at=cur.updated_at,
                    )
                )

        if cur is not None and _content_changed(cur.plan_items, cur.retro_items, plan, retro):
            s.execute(
                dashboard_entry_history.insert().values(
                    history_id=uuid.uuid4(),
                    node_id=node_id,
                    project_id=body.project_id,
                    period_kind="monthly",
                    period=mf,
                    plan_items=cur.plan_items or [],
                    retro_items=cur.retro_items or [],
                    prev_updated_at=cur.updated_at,
                )
            )

        row = s.execute(
            pg_insert(monthly_entries)
            .values(
                entry_id=uuid.uuid4(),
                node_id=node_id,
                project_id=body.project_id,
                month=mf,
                plan_items=plan,
                retro_items=retro,
            )
            .on_conflict_do_update(
                index_elements=["node_id", "project_id", "month"],
                set_={"plan_items": plan, "retro_items": retro, "updated_at": func.now()},
            )
            .returning(monthly_entries.c.entry_id, monthly_entries.c.updated_at)
        ).one()
        s.commit()
    sync_monthly_meeting(node_id, mf)
    realtime.publish(
        {
            "kind": "monthly",
            "node_id": node_id,
            "project_id": str(body.project_id),
            "month": mf.isoformat(),
        }
    )
    return MonthlyEntry(
        entry_id=row.entry_id,
        project_id=body.project_id,
        month=mf,
        plan_items=body.plan_items,
        retro_items=body.retro_items,
        updated_at=row.updated_at,
    )


def list_monthly_history(node_id: str, project_id: UUID, ref: date) -> list[EntryHistory]:
    mf = month_first(ref)
    with session() as s:
        rows = s.execute(
            select(
                dashboard_entry_history.c.period,
                dashboard_entry_history.c.plan_items,
                dashboard_entry_history.c.retro_items,
                dashboard_entry_history.c.prev_updated_at,
                dashboard_entry_history.c.snapshot_at,
            )
            .where(
                dashboard_entry_history.c.node_id == node_id,
                dashboard_entry_history.c.period_kind == "monthly",
                dashboard_entry_history.c.project_id == project_id,
                dashboard_entry_history.c.period == mf,
            )
            .order_by(dashboard_entry_history.c.snapshot_at.desc())
            .limit(20)
        ).all()
    return [
        EntryHistory(
            period=r.period,
            plan_items=_items(r.plan_items),
            retro_items=_items(r.retro_items),
            prev_updated_at=r.prev_updated_at,
            snapshot_at=r.snapshot_at,
        )
        for r in rows
    ]


class PeriodSummary(BaseModel):
    """A row in the Notion-DB-like period list (한눈에 보는 주차/월 목록)."""

    period: date  # week_start (Monday) or month-first
    plan_count: int = 0
    plan_done: int = 0
    retro_count: int = 0
    # 계획 점검 달성 점수 집계(0~10 스케일). scored는 score가 기록된 항목 수.
    plan_scored: int = 0
    plan_score_avg: float | None = None


def _summary_counts(plan_items, retro_items) -> tuple[int, int, int, int, float | None]:
    plan = plan_items or []
    done = sum(1 for it in plan if it.get("done"))
    # score=0도 유효한 채점 — truthiness 금지, is not None으로만 센다.
    scores = [it["score"] for it in plan if it.get("score") is not None]
    avg = round(sum(scores) / len(scores), 2) if scores else None
    return len(plan), done, len(retro_items or []), len(scores), avg


def list_weekly(node_id: str, project_id: UUID) -> list[PeriodSummary]:
    with session() as s:
        rows = s.execute(
            select(
                weekly_entries.c.week_start,
                weekly_entries.c.plan_items,
                weekly_entries.c.retro_items,
            )
            .where(
                weekly_entries.c.node_id == node_id,
                weekly_entries.c.project_id == project_id,
            )
            .order_by(weekly_entries.c.week_start.desc())
        ).all()
    out = []
    for r in rows:
        pc, pd, rc, ps, pa = _summary_counts(r.plan_items, r.retro_items)
        out.append(
            PeriodSummary(
                period=r.week_start,
                plan_count=pc,
                plan_done=pd,
                retro_count=rc,
                plan_scored=ps,
                plan_score_avg=pa,
            )
        )
    return out


def list_monthly(node_id: str, project_id: UUID) -> list[PeriodSummary]:
    with session() as s:
        rows = s.execute(
            select(
                monthly_entries.c.month,
                monthly_entries.c.plan_items,
                monthly_entries.c.retro_items,
            )
            .where(
                monthly_entries.c.node_id == node_id,
                monthly_entries.c.project_id == project_id,
            )
            .order_by(monthly_entries.c.month.desc())
        ).all()
    out = []
    for r in rows:
        pc, pd, rc, ps, pa = _summary_counts(r.plan_items, r.retro_items)
        out.append(
            PeriodSummary(
                period=r.month,
                plan_count=pc,
                plan_done=pd,
                retro_count=rc,
                plan_scored=ps,
                plan_score_avg=pa,
            )
        )
    return out


# --------------------------------------------------------------------------
# Team calendar
# --------------------------------------------------------------------------
class CalendarEventIn(BaseModel):
    member_ids: list[UUID] = Field(default_factory=list)
    title: str
    description: str | None = None
    all_day: bool = True
    event_date: date
    end_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    # "복귀불가" — 이 일정 뒤 담당자가 복귀하지 않음을 팀에 표시한다(종료 시각 옆 부가 선택).
    no_return: bool = False
    # "복귀 시간" — 복귀하는 경우의 복귀 시각(None=미지정). no_return과 상호 배타적이며
    # no_return=true면 _event_values가 None으로 정리한다.
    return_time: time | None = None
    # 반복(루틴): None=반복 없음 | daily|weekly|biweekly|monthly. 마스터 행 하나만
    # 저장하고 list_calendar가 조회 윈도 안의 회차로 펼친다.
    repeat_freq: str | None = None
    # weekly/biweekly에서 반복할 요일 — Python date.weekday() 규약(0=월…6=일).
    repeat_weekdays: list[int] = Field(default_factory=list)
    # 반복 종료일(포함). None = 무기한.
    repeat_until: date | None = None
    # 추가 날짜: 반복 규칙과 별개로, 같은 일정을 그대로 붙일 임의 시작일 목록.
    # event_date와 같은 기간만큼의 회차를 각 날짜에 만든다. 반복 유무와 무관.
    extra_dates: list[date] = Field(default_factory=list)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    event_type: str = "event"
    color: str | None = None
    project_id: UUID | None = None
    location: str | None = None


class CalendarEvent(CalendarEventIn):
    event_id: UUID
    # 반복 일정에서 펼쳐진 회차면 마스터의 시작일(수정/삭제는 시리즈 전체 대상).
    series_start: date | None = None


_EVENT_COLS = (
    team_calendar_events.c.event_id,
    team_calendar_events.c.member_ids,
    team_calendar_events.c.title,
    team_calendar_events.c.description,
    team_calendar_events.c.all_day,
    team_calendar_events.c.event_date,
    team_calendar_events.c.end_date,
    team_calendar_events.c.start_time,
    team_calendar_events.c.end_time,
    team_calendar_events.c.no_return,
    team_calendar_events.c.return_time,
    team_calendar_events.c.repeat_freq,
    team_calendar_events.c.repeat_weekdays,
    team_calendar_events.c.repeat_until,
    team_calendar_events.c.extra_dates,
    team_calendar_events.c.starts_at,
    team_calendar_events.c.ends_at,
    team_calendar_events.c.event_type,
    team_calendar_events.c.color,
    team_calendar_events.c.project_id,
    team_calendar_events.c.location,
)


def _event_out(row) -> CalendarEvent:
    return CalendarEvent(
        event_id=row.event_id,
        member_ids=list(row.member_ids or []),
        title=row.title,
        description=row.description,
        all_day=row.all_day,
        event_date=row.event_date,
        end_date=row.end_date,
        start_time=row.start_time,
        end_time=row.end_time,
        no_return=row.no_return,
        return_time=row.return_time,
        repeat_freq=row.repeat_freq,
        repeat_weekdays=[int(x) for x in (row.repeat_weekdays or [])],
        repeat_until=row.repeat_until,
        extra_dates=[_as_date(x) for x in (row.extra_dates or [])],
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        event_type=row.event_type,
        color=row.color,
        project_id=row.project_id,
        location=row.location,
    )


_REPEAT_FREQS = ("daily", "weekly", "biweekly", "monthly")
# 반복 시리즈 하나가 한 조회 윈도에서 만들 수 있는 최대 회차 수. FE 월 그리드는
# 42일 윈도라 daily도 42회가 상한이지만, 임의 from/to 호출을 대비한 안전판이다.
_OCCURRENCE_CAP = 200


def _occurrence_dates(
    start: date, freq: str, weekdays: list[int], until: date | None, lo: date, hi: date
) -> list[date]:
    """반복 규칙이 [lo, hi] 윈도 안에서 만드는 회차 날짜 목록."""
    lo = max(lo, start)
    if until is not None:
        hi = min(hi, until)
    if hi < lo:
        return []
    out: list[date] = []
    if freq == "monthly":
        # 매월 시작일과 같은 일(day-of-month). 그 일이 없는 달(예: 31일)은 건너뛴다.
        y, m = lo.year, lo.month
        while (y, m) <= (hi.year, hi.month):
            try:
                d = date(y, m, start.day)
            except ValueError:
                d = None
            if d is not None and lo <= d <= hi:
                out.append(d)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out
    days = weekdays or [start.weekday()]
    # 격주 기준: 시작일이 속한 주(월요일 시작)를 0주차로 두고 짝수 주차만.
    anchor_week = start - timedelta(days=start.weekday())
    d = lo
    while d <= hi and len(out) < _OCCURRENCE_CAP:
        if freq == "daily":
            ok = True
        else:
            ok = d.weekday() in days
            if ok and freq == "biweekly":
                ok = ((d - anchor_week).days // 7) % 2 == 0
        if ok:
            out.append(d)
        d += timedelta(days=1)
    return out


def _as_date(value) -> date:
    """JSONB에서 온 ISO date 문자열(또는 이미 date)을 date로."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _is_expanded(row) -> bool:
    """반복 회차나 추가 날짜(extra_dates)로 여러 회차로 펼쳐지는 일정인지."""
    return bool(row.repeat_freq) or bool(row.extra_dates)


def _expand_event(row, frm: date | None, to: date | None) -> list[CalendarEvent]:
    """event_date + 반복 회차 + extra_dates를 합쳐 조회 윈도 회차로 펼친다.

    반복 규칙이 있으면 그 회차를, 없으면 event_date 하나를 기준으로 삼고, 거기에
    extra_dates(임의 추가 날짜)를 합친다. 모든 회차는 같은 마스터(event_id)를
    가리키고 series_start로 원래 시작일을 노출한다(수정·삭제는 시리즈 전체 대상).
    """
    # 반복 회차 계산용 윈도. 반복은 무한 시리즈라 윈도가 없으면 마스터 시작일부터
    # 6주(월 그리드 크기)로 제한한다.
    lo = frm if frm is not None else row.event_date
    hi = to if to is not None else lo + timedelta(days=41)
    span = (row.end_date - row.event_date) if row.end_date is not None else None

    def _in_caller_window(start: date) -> bool:
        # 단일 회차([start, start+span])가 호출자 윈도(frm/to, None=무제한)와 겹치는지.
        # 시작일이 윈도 밖이어도 기간이 걸치면 포함(단일 일정 plain 분기와 동일).
        # 추가 날짜는 유한한 명시 목록이라 반복의 +41일 기본 윈도로 자르지 않고
        # 호출자 윈도 기준으로만 거른다(None-윈도 호출자는 전부 포함).
        end = (start + span) if span is not None else start
        return (to is None or start <= to) and (frm is None or end >= frm)

    dates: set[date] = set()
    if row.repeat_freq:
        weekdays = [int(x) for x in (row.repeat_weekdays or [])]
        dates.update(
            _occurrence_dates(row.event_date, row.repeat_freq, weekdays, row.repeat_until, lo, hi)
        )
    elif _in_caller_window(row.event_date):
        dates.add(row.event_date)
    # 추가 날짜: 반복 유무와 무관하게 기간이 호출자 윈도에 걸치면 포함(중복은 set 흡수).
    for x in row.extra_dates or []:
        d = _as_date(x)
        if _in_caller_window(d):
            dates.add(d)
    base = _event_out(row)
    return [
        base.model_copy(
            update={
                "event_date": d,
                "end_date": (d + span) if span is not None else None,
                "series_start": row.event_date,
            }
        )
        for d in sorted(dates)
    ]


def list_calendar(node_id: str, frm: date | None, to: date | None) -> list[CalendarEvent]:
    c = team_calendar_events.c
    # 다중일 일정: 윈도 이전에 시작해 안으로 걸쳐 들어오는 일정도 포함하려면
    # event_date 단일 비교가 아니라 [event_date, end_date] 구간과의 겹침으로 본다.
    span_end = func.coalesce(c.end_date, c.event_date)
    has_extra = func.jsonb_array_length(c.extra_dates) > 0
    # 단일 일정(반복·추가날짜 없음): [event_date, end_date] 구간이 윈도와 겹치면.
    plain = [c.repeat_freq.is_(None), func.jsonb_array_length(c.extra_dates) == 0]
    if to is not None:
        plain.append(c.event_date <= to)
    if frm is not None:
        plain.append(span_end >= frm)
    # 반복 일정: 시작 전이거나(repeat_until로) 끝난 시리즈만 거르고, 회차 계산은
    # Python 확장(_expand_event)에서 한다.
    recur = [c.repeat_freq.isnot(None)]
    if to is not None:
        recur.append(c.event_date <= to)
    if frm is not None:
        recur.append(or_(c.repeat_until.is_(None), c.repeat_until >= frm))
    # 추가 날짜가 있는 일정은 event_date가 윈도 밖이어도 extra_date가 윈도 안일 수
    # 있으므로 항상 후보에 넣고, 정확한 윈도 필터는 Python 확장에서 한다(팀 캘린더
    # 규모라 후보 수가 작다).
    with session() as s:
        rows = s.execute(
            select(*_EVENT_COLS)
            .where(c.node_id == node_id, or_(and_(*plain), and_(*recur), has_extra))
            .order_by(c.event_date)
        ).all()
    out: list[CalendarEvent] = []
    for r in rows:
        if _is_expanded(r):
            out.extend(_expand_event(r, frm, to))
        else:
            out.append(_event_out(r))
    out.sort(key=lambda e: e.event_date)
    return out


def list_calendar_locations(node_id: str) -> list[str]:
    """장소 자동완성용 — 이 노드의 캘린더에 과거에 입력된 distinct 장소 목록."""
    loc = team_calendar_events.c.location
    with session() as s:
        rows = s.execute(
            select(loc)
            .where(
                team_calendar_events.c.node_id == node_id,
                loc.isnot(None),
                func.trim(loc) != "",
            )
            .distinct()
            .order_by(loc)
        ).all()
    seen: list[str] = []
    for r in rows:
        v = (r.location or "").strip()
        if v and v not in seen:
            seen.append(v)
    return seen


def _validated_recurrence(body: CalendarEventIn) -> tuple[str | None, list[int], date | None]:
    """반복 규칙 정규화 — freq 미설정이면 나머지 반복 필드도 비운다."""
    freq = (body.repeat_freq or "").strip() or None
    if freq is None:
        return None, [], None
    if freq not in _REPEAT_FREQS:
        raise ValueError(f"invalid repeat_freq: {freq}")
    weekdays: list[int] = []
    if freq in ("weekly", "biweekly"):
        for d in body.repeat_weekdays:
            if d < 0 or d > 6:
                raise ValueError("repeat_weekdays must be 0(월)–6(일)")
            if d not in weekdays:
                weekdays.append(d)
        weekdays.sort()
        if not weekdays:
            # 요일 미선택이면 시작일 요일로 반복한다.
            weekdays = [body.event_date.weekday()]
    if body.repeat_until is not None and body.repeat_until < body.event_date:
        raise ValueError("repeat_until must be on or after event_date")
    return freq, weekdays, body.repeat_until


# 한 일정에 붙일 수 있는 추가 날짜 상한(펼침 폭 안전판).
_MAX_EXTRA_DATES = 100


def _validated_extra_dates(body: CalendarEventIn) -> list[str]:
    """추가 날짜 정규화 — 시작일과 같은 날짜 제거, 중복 제거, 정렬, ISO 문자열로.

    상한(_MAX_EXTRA_DATES) 초과 시 reject. 반복 유무와 무관하게 저장한다."""
    out: list[date] = []
    for d in body.extra_dates:
        if d == body.event_date or d in out:
            continue
        out.append(d)
    if len(out) > _MAX_EXTRA_DATES:
        raise ValueError(f"too many extra_dates (max {_MAX_EXTRA_DATES})")
    out.sort()
    return [d.isoformat() for d in out]


def _event_values(body: CalendarEventIn) -> dict:
    # JSONB member_ids must be plain strings, not UUID objects.
    values = body.model_dump(exclude={"title"})
    values["member_ids"] = [str(m) for m in body.member_ids]
    freq, weekdays, until = _validated_recurrence(body)
    values["repeat_freq"] = freq
    values["repeat_weekdays"] = weekdays
    values["repeat_until"] = until
    # JSONB extra_dates must be plain ISO strings, not date objects.
    values["extra_dates"] = _validated_extra_dates(body)
    # 복귀 불가와 복귀 시간은 상호 배타적 — no_return이면 return_time을 비운다.
    if values.get("no_return"):
        values["return_time"] = None
    return values


def create_event(
    node_id: str,
    created_by: UUID,
    body: CalendarEventIn,
    client_event_id: UUID | None = None,
) -> CalendarEvent:
    title = body.title.strip()
    if not title:
        raise ValueError("title required")
    values = _event_values(body)
    with session() as s:
        if client_event_id is not None:
            # 클라이언트가 생성한 멱등 event_id. 자동 저장의 keepalive/재시도/bfcache
            # 재실행으로 같은 id가 여러 번 create돼도 중복 행이 아니라 upsert된다
            # (같은 노드일 때만 갱신 — event_id는 PK라 교차 노드 충돌은 사실상 0).
            stmt = (
                pg_insert(team_calendar_events)
                .values(
                    event_id=client_event_id,
                    node_id=node_id,
                    created_by=created_by,
                    **values,
                    title=title,
                )
                .on_conflict_do_update(
                    index_elements=[team_calendar_events.c.event_id],
                    set_={**values, "title": title, "updated_at": func.now()},
                    where=(team_calendar_events.c.node_id == node_id),
                )
                .returning(*_EVENT_COLS)
            )
            row = s.execute(stmt).first()
            if row is None:
                # 다른 노드의 event_id와 충돌(사실상 불가) → 안전하게 거부.
                raise ValueError("event_id conflict")
        else:
            row = s.execute(
                team_calendar_events.insert()
                .values(
                    event_id=uuid.uuid4(),
                    node_id=node_id,
                    created_by=created_by,
                    **values,
                    title=title,
                )
                .returning(*_EVENT_COLS)
            ).one()
        s.commit()
    return _event_out(row)


def update_event(node_id: str, event_id: UUID, body: CalendarEventIn) -> CalendarEvent:
    title = body.title.strip()
    if not title:
        raise ValueError("title required")
    with session() as s:
        result = s.execute(
            team_calendar_events.update()
            .where(
                team_calendar_events.c.node_id == node_id,
                team_calendar_events.c.event_id == event_id,
            )
            .values(**_event_values(body), title=title, updated_at=func.now())
            .returning(*_EVENT_COLS)
        ).first()
        s.commit()
    if result is None:
        raise LookupError("event not found")
    return _event_out(result)


def delete_event(node_id: str, event_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(team_calendar_events).where(
                team_calendar_events.c.node_id == node_id,
                team_calendar_events.c.event_id == event_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("event not found")


# --------------------------------------------------------------------------
# Finance: subscriptions
# --------------------------------------------------------------------------
class SubscriptionIn(BaseModel):
    name: str
    vendor: str | None = None
    plan: str | None = None
    billing_cycle: str = "monthly"
    amount: float = 0
    currency: str = "KRW"
    next_billing_date: date | None = None
    status: str = "active"
    category: str | None = None
    owner_member_id: UUID | None = None
    masked_account: str | None = None
    notes: str | None = None


class Subscription(SubscriptionIn):
    sub_id: UUID


_SUB_COLS = (
    finance_subscriptions.c.sub_id,
    finance_subscriptions.c.name,
    finance_subscriptions.c.vendor,
    finance_subscriptions.c.plan,
    finance_subscriptions.c.billing_cycle,
    finance_subscriptions.c.amount,
    finance_subscriptions.c.currency,
    finance_subscriptions.c.next_billing_date,
    finance_subscriptions.c.status,
    finance_subscriptions.c.category,
    finance_subscriptions.c.owner_member_id,
    finance_subscriptions.c.masked_account,
    finance_subscriptions.c.notes,
)


def _sub_out(row) -> Subscription:
    return Subscription(
        sub_id=row.sub_id,
        name=row.name,
        vendor=row.vendor,
        plan=row.plan,
        billing_cycle=row.billing_cycle,
        amount=_f(row.amount),
        currency=row.currency,
        next_billing_date=row.next_billing_date,
        status=row.status,
        category=row.category,
        owner_member_id=row.owner_member_id,
        masked_account=row.masked_account,
        notes=row.notes,
    )


def list_subscriptions(node_id: str) -> list[Subscription]:
    with session() as s:
        rows = s.execute(
            select(*_SUB_COLS)
            .where(finance_subscriptions.c.node_id == node_id)
            .order_by(finance_subscriptions.c.name)
        ).all()
    return [_sub_out(r) for r in rows]


def create_subscription(node_id: str, body: SubscriptionIn) -> Subscription:
    if not body.name.strip():
        raise ValueError("name required")
    with session() as s:
        row = s.execute(
            finance_subscriptions.insert()
            .values(sub_id=uuid.uuid4(), node_id=node_id, **body.model_dump())
            .returning(*_SUB_COLS)
        ).one()
        s.commit()
    return _sub_out(row)


def update_subscription(node_id: str, sub_id: UUID, body: SubscriptionIn) -> Subscription:
    if not body.name.strip():
        raise ValueError("name required")
    with session() as s:
        result = s.execute(
            finance_subscriptions.update()
            .where(
                finance_subscriptions.c.node_id == node_id,
                finance_subscriptions.c.sub_id == sub_id,
            )
            .values(**body.model_dump(), updated_at=func.now())
            .returning(*_SUB_COLS)
        ).first()
        s.commit()
    if result is None:
        raise LookupError("subscription not found")
    return _sub_out(result)


def delete_subscription(node_id: str, sub_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(finance_subscriptions).where(
                finance_subscriptions.c.node_id == node_id,
                finance_subscriptions.c.sub_id == sub_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("subscription not found")


# --------------------------------------------------------------------------
# Finance: API keys (metadata only — no plaintext secret stored)
# --------------------------------------------------------------------------
class ApiKeyIn(BaseModel):
    service_name: str
    label: str | None = None
    key_last4: str | None = None
    environment: str = "prod"
    monthly_cost: float = 0
    currency: str = "KRW"
    status: str = "active"
    rotated_at: date | None = None
    owner_member_id: UUID | None = None
    notes: str | None = None


class ApiKey(ApiKeyIn):
    key_id: UUID


_KEY_COLS = (
    finance_api_keys.c.key_id,
    finance_api_keys.c.service_name,
    finance_api_keys.c.label,
    finance_api_keys.c.key_last4,
    finance_api_keys.c.environment,
    finance_api_keys.c.monthly_cost,
    finance_api_keys.c.currency,
    finance_api_keys.c.status,
    finance_api_keys.c.rotated_at,
    finance_api_keys.c.owner_member_id,
    finance_api_keys.c.notes,
)


def _key_out(row) -> ApiKey:
    return ApiKey(
        key_id=row.key_id,
        service_name=row.service_name,
        label=row.label,
        key_last4=row.key_last4,
        environment=row.environment,
        monthly_cost=_f(row.monthly_cost),
        currency=row.currency,
        status=row.status,
        rotated_at=row.rotated_at,
        owner_member_id=row.owner_member_id,
        notes=row.notes,
    )


def _normalize_last4(raw: str | None) -> str | None:
    """Defense in depth: only ever keep the last 4 chars, never a full secret."""
    if not raw:
        return None
    cleaned = raw.strip()
    return cleaned[-4:] if cleaned else None


def list_api_keys(node_id: str) -> list[ApiKey]:
    with session() as s:
        rows = s.execute(
            select(*_KEY_COLS)
            .where(finance_api_keys.c.node_id == node_id)
            .order_by(finance_api_keys.c.service_name)
        ).all()
    return [_key_out(r) for r in rows]


def create_api_key(node_id: str, body: ApiKeyIn) -> ApiKey:
    if not body.service_name.strip():
        raise ValueError("service_name required")
    values = body.model_dump()
    values["key_last4"] = _normalize_last4(values.get("key_last4"))
    with session() as s:
        row = s.execute(
            finance_api_keys.insert()
            .values(key_id=uuid.uuid4(), node_id=node_id, **values)
            .returning(*_KEY_COLS)
        ).one()
        s.commit()
    return _key_out(row)


def update_api_key(node_id: str, key_id: UUID, body: ApiKeyIn) -> ApiKey:
    if not body.service_name.strip():
        raise ValueError("service_name required")
    values = body.model_dump()
    values["key_last4"] = _normalize_last4(values.get("key_last4"))
    with session() as s:
        result = s.execute(
            finance_api_keys.update()
            .where(finance_api_keys.c.node_id == node_id, finance_api_keys.c.key_id == key_id)
            .values(**values, updated_at=func.now())
            .returning(*_KEY_COLS)
        ).first()
        s.commit()
    if result is None:
        raise LookupError("api key not found")
    return _key_out(result)


def delete_api_key(node_id: str, key_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(finance_api_keys).where(
                finance_api_keys.c.node_id == node_id, finance_api_keys.c.key_id == key_id
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("api key not found")


# --------------------------------------------------------------------------
# Finance: accounts (metadata only)
# --------------------------------------------------------------------------
class AccountIn(BaseModel):
    account_name: str
    kind: str = "login"
    masked_identifier: str | None = None
    owner_member_id: UUID | None = None
    notes: str | None = None


class Account(AccountIn):
    account_id: UUID


_ACCT_COLS = (
    finance_accounts.c.account_id,
    finance_accounts.c.account_name,
    finance_accounts.c.kind,
    finance_accounts.c.masked_identifier,
    finance_accounts.c.owner_member_id,
    finance_accounts.c.notes,
)


def _acct_out(row) -> Account:
    return Account(
        account_id=row.account_id,
        account_name=row.account_name,
        kind=row.kind,
        masked_identifier=row.masked_identifier,
        owner_member_id=row.owner_member_id,
        notes=row.notes,
    )


def list_accounts(node_id: str) -> list[Account]:
    with session() as s:
        rows = s.execute(
            select(*_ACCT_COLS)
            .where(finance_accounts.c.node_id == node_id)
            .order_by(finance_accounts.c.account_name)
        ).all()
    return [_acct_out(r) for r in rows]


def create_account(node_id: str, body: AccountIn) -> Account:
    if not body.account_name.strip():
        raise ValueError("account_name required")
    with session() as s:
        row = s.execute(
            finance_accounts.insert()
            .values(account_id=uuid.uuid4(), node_id=node_id, **body.model_dump())
            .returning(*_ACCT_COLS)
        ).one()
        s.commit()
    return _acct_out(row)


def update_account(node_id: str, account_id: UUID, body: AccountIn) -> Account:
    if not body.account_name.strip():
        raise ValueError("account_name required")
    with session() as s:
        result = s.execute(
            finance_accounts.update()
            .where(
                finance_accounts.c.node_id == node_id,
                finance_accounts.c.account_id == account_id,
            )
            .values(**body.model_dump(), updated_at=func.now())
            .returning(*_ACCT_COLS)
        ).first()
        s.commit()
    if result is None:
        raise LookupError("account not found")
    return _acct_out(result)


def delete_account(node_id: str, account_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(finance_accounts).where(
                finance_accounts.c.node_id == node_id,
                finance_accounts.c.account_id == account_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("account not found")


# --------------------------------------------------------------------------
# Finance: ledger (매출/지출)
# --------------------------------------------------------------------------
class LedgerIn(BaseModel):
    entry_date: date
    entry_type: str  # revenue | expense
    amount: float = 0
    currency: str = "KRW"
    category: str | None = None
    description: str | None = None
    project_id: UUID | None = None


class LedgerEntry(LedgerIn):
    ledger_id: UUID


_LEDGER_COLS = (
    finance_ledger.c.ledger_id,
    finance_ledger.c.entry_date,
    finance_ledger.c.entry_type,
    finance_ledger.c.amount,
    finance_ledger.c.currency,
    finance_ledger.c.category,
    finance_ledger.c.description,
    finance_ledger.c.project_id,
)


def _ledger_out(row) -> LedgerEntry:
    return LedgerEntry(
        ledger_id=row.ledger_id,
        entry_date=row.entry_date,
        entry_type=row.entry_type,
        amount=_f(row.amount),
        currency=row.currency,
        category=row.category,
        description=row.description,
        project_id=row.project_id,
    )


def list_ledger(node_id: str) -> list[LedgerEntry]:
    with session() as s:
        rows = s.execute(
            select(*_LEDGER_COLS)
            .where(finance_ledger.c.node_id == node_id)
            .order_by(finance_ledger.c.entry_date.desc())
        ).all()
    return [_ledger_out(r) for r in rows]


def create_ledger(node_id: str, body: LedgerIn) -> LedgerEntry:
    if body.entry_type not in ("revenue", "expense"):
        raise ValueError("entry_type must be revenue or expense")
    with session() as s:
        row = s.execute(
            finance_ledger.insert()
            .values(ledger_id=uuid.uuid4(), node_id=node_id, **body.model_dump())
            .returning(*_LEDGER_COLS)
        ).one()
        s.commit()
    return _ledger_out(row)


def update_ledger(node_id: str, ledger_id: UUID, body: LedgerIn) -> LedgerEntry:
    if body.entry_type not in ("revenue", "expense"):
        raise ValueError("entry_type must be revenue or expense")
    with session() as s:
        result = s.execute(
            finance_ledger.update()
            .where(finance_ledger.c.node_id == node_id, finance_ledger.c.ledger_id == ledger_id)
            .values(**body.model_dump(), updated_at=func.now())
            .returning(*_LEDGER_COLS)
        ).first()
        s.commit()
    if result is None:
        raise LookupError("ledger entry not found")
    return _ledger_out(result)


def delete_ledger(node_id: str, ledger_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(finance_ledger).where(
                finance_ledger.c.node_id == node_id, finance_ledger.c.ledger_id == ledger_id
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("ledger entry not found")


# --------------------------------------------------------------------------
# Finance: summary (매출현황 / 남은금액)
# --------------------------------------------------------------------------
class FinanceSummary(BaseModel):
    total_revenue: float = 0
    total_expense: float = 0
    balance: float = 0
    monthly_subscription_cost: float = 0
    active_subscription_count: int = 0
    monthly_api_cost: float = 0
    # 번레이트 = 월 고정 지출(구독+API). 런웨이 = 잔액 ÷ 번레이트(개월).
    monthly_burn: float = 0
    runway_months: float | None = None
    currency: str = "KRW"


def finance_summary(node_id: str) -> FinanceSummary:
    with session() as s:
        revenue = s.execute(
            select(func.coalesce(func.sum(finance_ledger.c.amount), 0)).where(
                finance_ledger.c.node_id == node_id,
                finance_ledger.c.entry_type == "revenue",
            )
        ).scalar_one()
        expense = s.execute(
            select(func.coalesce(func.sum(finance_ledger.c.amount), 0)).where(
                finance_ledger.c.node_id == node_id,
                finance_ledger.c.entry_type == "expense",
            )
        ).scalar_one()
        # monthly recurring subscription cost (yearly normalized to /12)
        sub_rows = s.execute(
            select(finance_subscriptions.c.amount, finance_subscriptions.c.billing_cycle).where(
                finance_subscriptions.c.node_id == node_id,
                finance_subscriptions.c.status.in_(("active", "trial")),
            )
        ).all()
        api_cost = s.execute(
            select(func.coalesce(func.sum(finance_api_keys.c.monthly_cost), 0)).where(
                finance_api_keys.c.node_id == node_id,
                finance_api_keys.c.status == "active",
            )
        ).scalar_one()

    monthly_sub = 0.0
    for amount, cycle in sub_rows:
        amt = _f(amount)
        if cycle == "yearly":
            monthly_sub += amt / 12.0
        elif cycle == "monthly":
            monthly_sub += amt
    balance = _f(revenue) - _f(expense)
    monthly_burn = round(monthly_sub + _f(api_cost), 2)
    runway = round(balance / monthly_burn, 1) if monthly_burn > 0 and balance > 0 else None
    return FinanceSummary(
        total_revenue=_f(revenue),
        total_expense=_f(expense),
        balance=balance,
        monthly_subscription_cost=round(monthly_sub, 2),
        active_subscription_count=len(sub_rows),
        monthly_api_cost=_f(api_cost),
        monthly_burn=monthly_burn,
        runway_months=runway,
    )


# --------------------------------------------------------------------------
# Culture (사무실/WiFi/근무시간 등 display-only 설정)
# --------------------------------------------------------------------------
class CompanyCulture(BaseModel):
    content: dict = Field(default_factory=dict)


def get_culture(node_id: str) -> CompanyCulture:
    with session() as s:
        row = s.execute(
            select(company_culture.c.content).where(company_culture.c.node_id == node_id)
        ).first()
    if row is None:
        return CompanyCulture(content={})
    return CompanyCulture(content=row.content or {})


def upsert_culture(node_id: str, body: CompanyCulture) -> CompanyCulture:
    with session() as s:
        row = s.execute(
            pg_insert(company_culture)
            .values(node_id=node_id, content=body.content)
            .on_conflict_do_update(
                index_elements=["node_id"],
                set_={"content": body.content, "updated_at": func.now()},
            )
            .returning(company_culture.c.content)
        ).one()
        s.commit()
    return CompanyCulture(content=row.content or {})


# --------------------------------------------------------------------------
# Meeting notes (회의록) — Notion-DB-like records
# --------------------------------------------------------------------------
class MeetingNoteIn(BaseModel):
    title: str
    project_id: UUID | None = None
    partner_id: UUID | None = None
    meeting_kind: str = "internal"
    meeting_date: date
    attendee_ids: list[UUID] = Field(default_factory=list)
    body: str | None = None


class MeetingAttachment(BaseModel):
    attachment_id: UUID
    filename: str
    mime_type: str | None = None
    size_bytes: int = 0
    url: str
    created_at: datetime | None = None


class MeetingNote(MeetingNoteIn):
    note_id: UUID
    source: str = "manual"
    attachments: list[MeetingAttachment] = Field(default_factory=list)


_NOTE_COLS = (
    meeting_notes.c.note_id,
    meeting_notes.c.title,
    meeting_notes.c.project_id,
    meeting_notes.c.partner_id,
    meeting_notes.c.meeting_kind,
    meeting_notes.c.meeting_date,
    meeting_notes.c.attendee_ids,
    meeting_notes.c.body,
    meeting_notes.c.source,
)


def _note_out(row) -> MeetingNote:
    return MeetingNote(
        note_id=row.note_id,
        title=row.title,
        project_id=row.project_id,
        partner_id=row.partner_id,
        meeting_kind=row.meeting_kind,
        meeting_date=row.meeting_date,
        attendee_ids=list(row.attendee_ids or []),
        body=row.body,
        source=row.source,
    )


def _note_values(body: MeetingNoteIn) -> dict:
    values = body.model_dump(exclude={"title"})
    values["attendee_ids"] = [str(a) for a in body.attendee_ids]
    values["partner_id"] = str(body.partner_id) if body.partner_id else None
    kind = values.get("meeting_kind")
    if kind not in ("internal", "external"):
        values["meeting_kind"] = "internal"
    return values


# --- meeting attachments (PDF 등 미팅자료) ---------------------------------
_ATTACH_COLS = (
    meeting_attachments.c.attachment_id,
    meeting_attachments.c.note_id,
    meeting_attachments.c.filename,
    meeting_attachments.c.media_filename,
    meeting_attachments.c.mime_type,
    meeting_attachments.c.size_bytes,
    meeting_attachments.c.created_at,
)


def _attachment_out(row) -> MeetingAttachment:
    return MeetingAttachment(
        attachment_id=row.attachment_id,
        filename=row.filename,
        mime_type=row.mime_type,
        size_bytes=int(row.size_bytes or 0),
        url=f"/dashboard/media/{row.media_filename}",
        created_at=row.created_at,
    )


def _attachments_by_note(s, node_id: str, note_ids: list[UUID]) -> dict[UUID, list[MeetingAttachment]]:
    """회의록 여러 개의 첨부를 한 번에 로드(리스트 N+1 방지)."""
    if not note_ids:
        return {}
    rows = s.execute(
        select(*_ATTACH_COLS)
        .where(
            meeting_attachments.c.node_id == node_id,
            meeting_attachments.c.note_id.in_(note_ids),
        )
        .order_by(meeting_attachments.c.created_at)
    ).all()
    out: dict[UUID, list[MeetingAttachment]] = {}
    for r in rows:
        out.setdefault(r.note_id, []).append(_attachment_out(r))
    return out


def list_meeting_attachments(node_id: str, note_id: UUID) -> list[MeetingAttachment]:
    with session() as s:
        return _attachments_by_note(s, node_id, [note_id]).get(note_id, [])


def create_meeting_attachment(
    node_id: str,
    note_id: UUID,
    data: bytes,
    filename: str | None,
    content_type: str | None,
    created_by: UUID | None,
) -> MeetingAttachment:
    """파일 바이트를 media_store(실파일)에 저장하고 회의↔첨부 매핑을 남긴다.

    회의록이 없으면 파일 저장 전에 LookupError(→404)를 던져 orphan 파일을 막는다.
    빈/용량초과는 media_store가 ValueError(→413/422).
    """
    with session() as s:
        exists = s.execute(
            select(meeting_notes.c.note_id).where(
                meeting_notes.c.node_id == node_id,
                meeting_notes.c.note_id == note_id,
            )
        ).first()
        if exists is None:
            raise LookupError("meeting note not found")
    meta = media_store.store_upload(data, filename, content_type)
    with session() as s:
        row = s.execute(
            meeting_attachments.insert()
            .values(
                attachment_id=uuid.uuid4(),
                node_id=node_id,
                note_id=note_id,
                filename=meta["name"],
                media_filename=meta["filename"],
                mime_type=meta["content_type"],
                size_bytes=int(meta["size"]),
                created_by=created_by,
            )
            .returning(*_ATTACH_COLS)
        ).one()
        s.commit()
    return _attachment_out(row)


def delete_meeting_attachment(node_id: str, note_id: UUID, attachment_id: UUID) -> None:
    with session() as s:
        row = s.execute(
            select(meeting_attachments.c.media_filename).where(
                meeting_attachments.c.node_id == node_id,
                meeting_attachments.c.note_id == note_id,
                meeting_attachments.c.attachment_id == attachment_id,
            )
        ).first()
        if row is None:
            raise LookupError("attachment not found")
        s.execute(
            delete(meeting_attachments).where(
                meeting_attachments.c.node_id == node_id,
                meeting_attachments.c.note_id == note_id,
                meeting_attachments.c.attachment_id == attachment_id,
            )
        )
        s.commit()
    # 매핑 삭제가 커밋된 뒤에만 실파일 제거(살아 있는 row의 파일을 지우지 않도록).
    media_store.delete_media(row.media_filename)


def list_meeting_notes(
    node_id: str, meeting_kind: str | None = None, project_id: UUID | None = None
) -> list[MeetingNote]:
    stmt = (
        select(*_NOTE_COLS)
        .where(meeting_notes.c.node_id == node_id)
        .order_by(meeting_notes.c.meeting_date.desc())
    )
    if meeting_kind in ("internal", "external"):
        stmt = stmt.where(meeting_notes.c.meeting_kind == meeting_kind)
    if project_id is not None:
        stmt = stmt.where(meeting_notes.c.project_id == project_id)
    with session() as s:
        rows = s.execute(stmt).all()
        notes = [_note_out(r) for r in rows]
        attach = _attachments_by_note(s, node_id, [n.note_id for n in notes])
    for n in notes:
        n.attachments = attach.get(n.note_id, [])
    return notes


# --- auto-sync internal meeting from weekly/monthly plan·retro ---
def _plan_lines(items: list[PlanItem]) -> str:
    if not items:
        return "- (없음)"
    out = []
    for it in items:
        group = f"[{it.group}] " if it.group else ""
        done = "[x] " if it.done else "[ ] "
        out.append(f"- {done}{group}{it.text}")
    return "\n".join(out)


def _week_meeting_title(ws: date) -> str:
    we = ws + timedelta(days=6)
    if ws.month == we.month:
        return f"{ws.month}월 {ws.day}일~{we.day}일 주간회의"
    return f"{ws.month}월 {ws.day}일~{we.month}월 {we.day}일 주간회의"


def _render_aggregate_body(
    sections: list[tuple[str, list[PlanItem], list[PlanItem]]],
) -> str:
    out: list[str] = []
    for name, plan, retro in sections:
        out.append(f"## {name}")
        out.append("### 계획")
        out.append(_plan_lines(plan))
        out.append("### 회고")
        out.append(_plan_lines(retro))
        out.append("")
    return "\n".join(out).strip()


def _aggregate_period_meeting(
    node_id: str, table, date_col, period_date: date, source: str, title: str
) -> None:
    """Rebuild ONE internal meeting per period (week/month) that aggregates every
    project's plan·retro for that period. Idempotent on (node_id, source,
    source_ref=period_date)."""
    source_ref = period_date.isoformat()
    with session() as s:
        rows = s.execute(
            select(table.c.plan_items, table.c.retro_items, dashboard_projects.c.name)
            .select_from(
                table.outerjoin(
                    dashboard_projects,
                    dashboard_projects.c.project_id == table.c.project_id,
                )
            )
            .where(table.c.node_id == node_id, date_col == period_date)
            .order_by(dashboard_projects.c.sort_order, dashboard_projects.c.name)
        ).all()
        sections: list[tuple[str, list[PlanItem], list[PlanItem]]] = []
        for r in rows:
            plan = _items(r.plan_items)
            retro = _items(r.retro_items)
            if not plan and not retro:
                continue
            sections.append((r.name or "프로젝트", plan, retro))
        if not sections:
            s.execute(
                delete(meeting_notes).where(
                    meeting_notes.c.node_id == node_id,
                    meeting_notes.c.source == source,
                    meeting_notes.c.source_ref == source_ref,
                )
            )
            s.commit()
            return
        attendees = [
            str(r.member_id)
            for r in s.execute(
                select(team_members.c.member_id).where(
                    team_members.c.node_id == node_id,
                    team_members.c.active.is_(True),
                )
            ).all()
        ]
        body = _render_aggregate_body(sections)
        s.execute(
            pg_insert(meeting_notes)
            .values(
                note_id=uuid.uuid4(),
                node_id=node_id,
                title=title,
                project_id=None,
                meeting_kind="internal",
                meeting_date=period_date,
                attendee_ids=attendees,
                body=body,
                source=source,
                source_ref=source_ref,
            )
            .on_conflict_do_update(
                index_elements=["node_id", "source", "source_ref"],
                set_={
                    "title": title,
                    "project_id": None,
                    "meeting_date": period_date,
                    "attendee_ids": attendees,
                    "body": body,
                    "updated_at": func.now(),
                },
            )
        )
        s.commit()


def sync_weekly_meeting(node_id: str, ws: date) -> None:
    _aggregate_period_meeting(
        node_id,
        weekly_entries,
        weekly_entries.c.week_start,
        ws,
        "weekly_plan",
        _week_meeting_title(ws),
    )


def sync_monthly_meeting(node_id: str, mf: date) -> None:
    _aggregate_period_meeting(
        node_id,
        monthly_entries,
        monthly_entries.c.month,
        mf,
        "monthly_plan",
        f"{mf.month}월 월간회의",
    )


def resync_internal_meetings(node_id: str) -> int:
    """One-off: rebuild every weekly/monthly aggregate internal meeting from the
    current plan·retro entries (e.g. after the per-project → per-week change)."""
    with session() as s:
        weeks = [
            r.week_start
            for r in s.execute(
                select(weekly_entries.c.week_start)
                .where(weekly_entries.c.node_id == node_id)
                .distinct()
            ).all()
        ]
        months = [
            r.month
            for r in s.execute(
                select(monthly_entries.c.month)
                .where(monthly_entries.c.node_id == node_id)
                .distinct()
            ).all()
        ]
    for ws in weeks:
        sync_weekly_meeting(node_id, ws)
    for mf in months:
        sync_monthly_meeting(node_id, mf)
    return len(weeks) + len(months)


def create_meeting_note(node_id: str, created_by: UUID, body: MeetingNoteIn) -> MeetingNote:
    title = body.title.strip()
    if not title:
        raise ValueError("title required")
    with session() as s:
        row = s.execute(
            meeting_notes.insert()
            .values(
                note_id=uuid.uuid4(),
                node_id=node_id,
                created_by=created_by,
                **_note_values(body),
                title=title,
            )
            .returning(*_NOTE_COLS)
        ).one()
        s.commit()
    return _note_out(row)


def update_meeting_note(node_id: str, note_id: UUID, body: MeetingNoteIn) -> MeetingNote:
    title = body.title.strip()
    if not title:
        raise ValueError("title required")
    values = _note_values(body)
    # 유실 방지: body=None 은 "본문 변경 없음"으로 취급해 기존 본문을 지우지 않는다.
    # (에디터 로드 레이스·필드만 인라인 수정 등으로 빈 본문이 전송돼도 안전.)
    # 본문을 실제로 비우려면 명시적으로 빈 문자열 ""을 보낸다.
    if body.body is None:
        values.pop("body", None)
    with session() as s:
        result = s.execute(
            meeting_notes.update()
            .where(meeting_notes.c.node_id == node_id, meeting_notes.c.note_id == note_id)
            .values(**values, title=title, updated_at=func.now())
            .returning(*_NOTE_COLS)
        ).first()
        if result is None:
            s.rollback()
            raise LookupError("meeting note not found")
        note = _note_out(result)
        note.attachments = _attachments_by_note(s, node_id, [note_id]).get(note_id, [])
        s.commit()
    return note


def delete_meeting_note(node_id: str, note_id: UUID) -> None:
    with session() as s:
        media_files = [
            r.media_filename
            for r in s.execute(
                select(meeting_attachments.c.media_filename).where(
                    meeting_attachments.c.node_id == node_id,
                    meeting_attachments.c.note_id == note_id,
                )
            ).all()
        ]
        s.execute(
            delete(meeting_attachments).where(
                meeting_attachments.c.node_id == node_id,
                meeting_attachments.c.note_id == note_id,
            )
        )
        result = s.execute(
            delete(meeting_notes).where(
                meeting_notes.c.node_id == node_id, meeting_notes.c.note_id == note_id
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("meeting note not found")
    # 커밋 후 첨부 실파일도 best-effort 제거(orphan 파일 방지).
    for name in media_files:
        media_store.delete_media(name)


# --------------------------------------------------------------------------
# Infra resources (GPU / 스토리지(NAS) / 서버 / API 키)
# --------------------------------------------------------------------------
class InfraResourceIn(BaseModel):
    kind: str = "gpu"  # gpu | storage | server | api_key | dashboard | other
    name: str
    vendor: str | None = None
    model: str | None = None
    location: str | None = None
    status: str = "active"  # active | idle | reserved | down | expired
    capacity: float | None = None
    used: float | None = None
    unit: str | None = None
    usage_percent: int | None = None
    owner_member_id: UUID | None = None
    link: str | None = None
    notes: str | None = None
    period: str | None = None  # free-text usage/lease period
    parent_id: UUID | None = None  # legacy self-group (unused)
    unit_price: float | None = None  # 단가
    balance: float | None = None  # legacy per-service balance (unused)
    provider_id: UUID | None = None  # 제공처 group
    price_unit: str | None = None  # per_call | per_1m | monthly | hourly | other
    color: str | None = None  # graph line color (hex)
    currency: str | None = None  # KRW | USD (for unit_price)
    sort_order: int = 0


class InfraResource(InfraResourceIn):
    resource_id: UUID


_INFRA_COLS = (
    infra_resources.c.resource_id,
    infra_resources.c.kind,
    infra_resources.c.name,
    infra_resources.c.vendor,
    infra_resources.c.model,
    infra_resources.c.location,
    infra_resources.c.status,
    infra_resources.c.capacity,
    infra_resources.c.used,
    infra_resources.c.unit,
    infra_resources.c.usage_percent,
    infra_resources.c.owner_member_id,
    infra_resources.c.link,
    infra_resources.c.notes,
    infra_resources.c.period,
    infra_resources.c.parent_id,
    infra_resources.c.unit_price,
    infra_resources.c.balance,
    infra_resources.c.provider_id,
    infra_resources.c.price_unit,
    infra_resources.c.color,
    infra_resources.c.currency,
    infra_resources.c.sort_order,
)


def _infra_out(row) -> InfraResource:
    return InfraResource(
        resource_id=row.resource_id,
        kind=row.kind,
        name=row.name,
        vendor=row.vendor,
        model=row.model,
        location=row.location,
        status=row.status,
        capacity=_f(row.capacity) if row.capacity is not None else None,
        used=_f(row.used) if row.used is not None else None,
        unit=row.unit,
        usage_percent=row.usage_percent,
        owner_member_id=row.owner_member_id,
        link=row.link,
        notes=row.notes,
        period=row.period,
        parent_id=row.parent_id,
        unit_price=_f(row.unit_price) if row.unit_price is not None else None,
        balance=_f(row.balance) if row.balance is not None else None,
        provider_id=row.provider_id,
        price_unit=row.price_unit,
        color=row.color,
        currency=row.currency,
        sort_order=row.sort_order,
    )


def list_infra(node_id: str) -> list[InfraResource]:
    with session() as s:
        rows = s.execute(
            select(*_INFRA_COLS)
            .where(infra_resources.c.node_id == node_id)
            .order_by(infra_resources.c.kind, infra_resources.c.sort_order, infra_resources.c.name)
        ).all()
    return [_infra_out(r) for r in rows]


def create_infra(node_id: str, body: InfraResourceIn) -> InfraResource:
    if not body.name.strip():
        raise ValueError("name required")
    with session() as s:
        row = s.execute(
            infra_resources.insert()
            .values(resource_id=uuid.uuid4(), node_id=node_id, **body.model_dump())
            .returning(*_INFRA_COLS)
        ).one()
        s.commit()
    return _infra_out(row)


def update_infra(node_id: str, resource_id: UUID, body: InfraResourceIn) -> InfraResource:
    if not body.name.strip():
        raise ValueError("name required")
    with session() as s:
        result = s.execute(
            infra_resources.update()
            .where(
                infra_resources.c.node_id == node_id,
                infra_resources.c.resource_id == resource_id,
            )
            .values(**body.model_dump(), updated_at=func.now())
            .returning(*_INFRA_COLS)
        ).first()
        s.commit()
    if result is None:
        raise LookupError("infra resource not found")
    return _infra_out(result)


def delete_infra(node_id: str, resource_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(infra_resources).where(
                infra_resources.c.node_id == node_id,
                infra_resources.c.resource_id == resource_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("infra resource not found")


# ---- Service ↔ project links (N:M, simple connection) ----
class InfraResourceLinkIn(BaseModel):
    resource_id: UUID
    project_id: UUID


class InfraResourceLink(InfraResourceLinkIn):
    link_id: UUID


def list_infra_links(node_id: str) -> list[InfraResourceLink]:
    with session() as s:
        rows = s.execute(
            select(
                infra_resource_projects.c.link_id,
                infra_resource_projects.c.resource_id,
                infra_resource_projects.c.project_id,
            ).where(infra_resource_projects.c.node_id == node_id)
        ).all()
    return [
        InfraResourceLink(link_id=r.link_id, resource_id=r.resource_id, project_id=r.project_id)
        for r in rows
    ]


def add_infra_link(node_id: str, body: InfraResourceLinkIn) -> InfraResourceLink:
    """Idempotent: returns the existing link if the pair is already connected."""
    with session() as s:
        row = s.execute(
            pg_insert(infra_resource_projects)
            .values(
                link_id=uuid.uuid4(),
                node_id=node_id,
                resource_id=body.resource_id,
                project_id=body.project_id,
            )
            .on_conflict_do_nothing(index_elements=["node_id", "resource_id", "project_id"])
            .returning(
                infra_resource_projects.c.link_id,
                infra_resource_projects.c.resource_id,
                infra_resource_projects.c.project_id,
            )
        ).first()
        if row is None:
            row = s.execute(
                select(
                    infra_resource_projects.c.link_id,
                    infra_resource_projects.c.resource_id,
                    infra_resource_projects.c.project_id,
                ).where(
                    infra_resource_projects.c.node_id == node_id,
                    infra_resource_projects.c.resource_id == body.resource_id,
                    infra_resource_projects.c.project_id == body.project_id,
                )
            ).one()
        s.commit()
    return InfraResourceLink(
        link_id=row.link_id, resource_id=row.resource_id, project_id=row.project_id
    )


def remove_infra_link(node_id: str, link_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(infra_resource_projects).where(
                infra_resource_projects.c.node_id == node_id,
                infra_resource_projects.c.link_id == link_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("link not found")


# ---- 제공처(provider) = service group holding 잔여량 ----
class InfraProviderIn(BaseModel):
    name: str
    balance: float | None = None
    currency: str | None = None  # KRW | USD (for balance)
    link: str | None = None
    notes: str | None = None
    sort_order: int = 0


class InfraProvider(InfraProviderIn):
    provider_id: UUID


_PROVIDER_COLS = (
    infra_providers.c.provider_id,
    infra_providers.c.name,
    infra_providers.c.balance,
    infra_providers.c.currency,
    infra_providers.c.link,
    infra_providers.c.notes,
    infra_providers.c.sort_order,
)


def _provider_out(row) -> InfraProvider:
    return InfraProvider(
        provider_id=row.provider_id,
        name=row.name,
        balance=_f(row.balance) if row.balance is not None else None,
        currency=row.currency,
        link=row.link,
        notes=row.notes,
        sort_order=row.sort_order,
    )


def list_providers(node_id: str) -> list[InfraProvider]:
    with session() as s:
        rows = s.execute(
            select(*_PROVIDER_COLS)
            .where(infra_providers.c.node_id == node_id)
            .order_by(infra_providers.c.sort_order, infra_providers.c.name)
        ).all()
    return [_provider_out(r) for r in rows]


def create_provider(node_id: str, body: InfraProviderIn) -> InfraProvider:
    """Idempotent on name: returns the existing provider if the name is taken."""
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    with session() as s:
        row = s.execute(
            pg_insert(infra_providers)
            .values(
                provider_id=uuid.uuid4(),
                node_id=node_id,
                name=name,
                balance=body.balance,
                currency=body.currency,
                link=body.link,
                notes=body.notes,
                sort_order=body.sort_order,
            )
            .on_conflict_do_nothing(index_elements=["node_id", "name"])
            .returning(*_PROVIDER_COLS)
        ).first()
        if row is None:
            row = s.execute(
                select(*_PROVIDER_COLS).where(
                    infra_providers.c.node_id == node_id,
                    infra_providers.c.name == name,
                )
            ).one()
        s.commit()
    return _provider_out(row)


def update_provider(node_id: str, provider_id: UUID, body: InfraProviderIn) -> InfraProvider:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    with session() as s:
        result = s.execute(
            infra_providers.update()
            .where(
                infra_providers.c.node_id == node_id,
                infra_providers.c.provider_id == provider_id,
            )
            .values(
                name=name,
                balance=body.balance,
                currency=body.currency,
                link=body.link,
                notes=body.notes,
                sort_order=body.sort_order,
                updated_at=func.now(),
            )
            .returning(*_PROVIDER_COLS)
        ).first()
        s.commit()
    if result is None:
        raise LookupError("provider not found")
    return _provider_out(result)


def delete_provider(node_id: str, provider_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(infra_providers).where(
                infra_providers.c.node_id == node_id,
                infra_providers.c.provider_id == provider_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("provider not found")


class GpuSummary(BaseModel):
    total: float = 0
    used: float = 0
    available: float = 0
    down: float = 0


def gpu_summary(node_id: str) -> GpuSummary:
    with session() as s:
        rows = s.execute(
            select(
                infra_resources.c.capacity,
                infra_resources.c.used,
                infra_resources.c.status,
            ).where(infra_resources.c.node_id == node_id, infra_resources.c.kind == "gpu")
        ).all()
    total = used = down = 0.0
    for cap, u, status in rows:
        c = _f(cap)
        total += c
        if status == "down":
            down += c
        else:
            used += _f(u)
    available = max(total - used - down, 0.0)
    return GpuSummary(
        total=round(total, 2),
        used=round(used, 2),
        available=round(available, 2),
        down=round(down, 2),
    )


# --------------------------------------------------------------------------
# GPU live sync — Tailscale tailnet 의 GPU 호스트에 SSH 로 nvidia-smi 실행.
# `ssh <host> nvidia-smi` (host 기본 'a100', ORTHUS_GPU_SSH_HOST 로 override).
# --------------------------------------------------------------------------
class GpuSyncResult(BaseModel):
    host: str
    total: int = 0
    used: int = 0
    available: int = 0
    avg_util: int = 0
    detail: str = ""


def sync_gpu_from_ssh(node_id: str) -> GpuSyncResult:
    """SSH into the GPU host and upsert one aggregate infra row from nvidia-smi.
    Busy = utilization >= 10%. Raises ValueError on ssh/parse failure."""
    host = _os.environ.get("ORTHUS_GPU_SSH_HOST", "a100")
    query = "nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits"
    try:
        proc = _subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=12",
                "-o",
                "StrictHostKeyChecking=accept-new",
                host,
                query,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (_subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise ValueError(f"GPU 호스트 접속 실패({host}): {e}") from e
    if proc.returncode != 0:
        raise ValueError(f"GPU 호스트 nvidia-smi 실패({host}): {proc.stderr.strip()[:200]}")

    gpus: list[tuple[int, str, int, int, int]] = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            gpus.append((int(parts[0]), parts[1], int(parts[2]), int(parts[3]), int(parts[4])))
        except ValueError:
            continue
    if not gpus:
        raise ValueError(f"GPU 정보를 파싱하지 못했습니다({host}).")

    total = len(gpus)
    busy = sum(1 for g in gpus if g[4] >= 10)
    avg_util = round(sum(g[4] for g in gpus) / total)
    model = gpus[0][1]
    detail = " · ".join(f"GPU{g[0]} {g[4]}% {g[3]}/{g[2]}MiB" for g in gpus)

    values = dict(
        kind="gpu",
        name=host,
        model=model,
        location=f"Tailscale: {host}",
        status="active",
        capacity=total,
        used=busy,
        unit="장",
        usage_percent=avg_util,
        link=f"tailscale:{host}",
        notes=detail,
    )
    with session() as s:
        existing = s.execute(
            select(infra_resources.c.resource_id).where(
                infra_resources.c.node_id == node_id,
                infra_resources.c.kind == "gpu",
                infra_resources.c.name == host,
            )
        ).first()
        if existing:
            s.execute(
                infra_resources.update()
                .where(infra_resources.c.resource_id == existing.resource_id)
                .values(**values, updated_at=func.now())
            )
        else:
            s.execute(
                infra_resources.insert().values(
                    resource_id=uuid.uuid4(), node_id=node_id, sort_order=0, **values
                )
            )
        s.commit()

    return GpuSyncResult(
        host=host,
        total=total,
        used=busy,
        available=total - busy,
        avg_util=avg_util,
        detail=detail,
    )


# --------------------------------------------------------------------------
# GPU live sync — Nova ML platform VictoriaMetrics (PromQL over the tailnet).
# Default path when ORTHUS_NOVA_ML_VM_URL is set; falls back to SSH otherwise.
# Unlike SSH (one host), VM covers every GPU node and is upserted one row each.
# --------------------------------------------------------------------------
def _nova_client() -> NovaMLClient:
    """Build a NovaMLClient from settings. Module-level so tests can monkeypatch."""
    s = get_settings()
    return NovaMLClient(
        vm_url=s.nova_ml_vm_url,
        mlflow_url=s.nova_ml_mlflow_url,
        grafana_url=s.nova_ml_grafana_url,
        timeout=s.nova_ml_timeout_seconds,
    )


def sync_gpu(node_id: str) -> GpuSyncResult:
    """Dispatch GPU sync: VictoriaMetrics pull when configured, else legacy SSH."""
    if get_settings().nova_ml_vm_url:
        return sync_gpu_from_vm(node_id)
    return sync_gpu_from_ssh(node_id)


def sync_gpu_from_vm(node_id: str, client: NovaMLClient | None = None) -> GpuSyncResult:
    """Pull per-GPU DCGM metrics from VictoriaMetrics and upsert one infra row per
    GPU node. Busy = util >= BUSY_UTIL_THRESHOLD (parity with the SSH path).
    Raises ValueError (→ 422) when VM is unreachable or returns no GPU metrics."""
    client = client or _nova_client()
    devices = client.gpu_devices()
    if not devices:
        raise ValueError("VictoriaMetrics에서 GPU 메트릭을 찾지 못했습니다 (GPU 노드 다운?).")

    grafana_url = get_settings().nova_ml_grafana_url

    by_node: dict[str, list] = {}
    for dev in devices:
        by_node.setdefault(dev.node, []).append(dev)

    node_details: list[str] = []
    multi = len(by_node) > 1
    for node, devs in by_node.items():
        count = len(devs)
        busy = sum(1 for d in devs if d.util >= BUSY_UTIL_THRESHOLD)
        node_avg = round(sum(d.util for d in devs) / count)
        model = next((d.model for d in devs if d.model), "")
        detail = " · ".join(
            f"GPU{d.index} {d.util:.0f}% {d.mem_used_mib:.0f}/{d.mem_total_mib:.0f}MiB"
            + (f" {d.temp_c:.0f}°C" if d.temp_c is not None else "")
            for d in devs
        )
        node_details.append(f"[{node}] {detail}" if multi else detail)

        values = dict(
            kind="gpu",
            name=node,
            model=model,
            location=f"Nova k3s: {node}",
            status="active",
            capacity=count,
            used=busy,
            unit="장",
            usage_percent=node_avg,
            link=grafana_url or f"nova-vm:{node}",
            notes=detail,
        )
        with session() as s:
            existing = s.execute(
                select(infra_resources.c.resource_id).where(
                    infra_resources.c.node_id == node_id,
                    infra_resources.c.kind == "gpu",
                    infra_resources.c.name == node,
                )
            ).first()
            if existing:
                s.execute(
                    infra_resources.update()
                    .where(infra_resources.c.resource_id == existing.resource_id)
                    .values(**values, updated_at=func.now())
                )
            else:
                s.execute(
                    infra_resources.insert().values(
                        resource_id=uuid.uuid4(), node_id=node_id, sort_order=0, **values
                    )
                )
            s.commit()

    total = len(devices)
    busy_total = sum(1 for d in devices if d.util >= BUSY_UTIL_THRESHOLD)
    fleet_avg = round(sum(d.util for d in devices) / total)
    return GpuSyncResult(
        host=", ".join(by_node.keys()),
        total=total,
        used=busy_total,
        available=total - busy_total,
        avg_util=fleet_avg,
        detail=" · ".join(node_details),
    )


# --------------------------------------------------------------------------
# Storage live sync — node_exporter filesystem metrics from VictoriaMetrics.
# One storage row per node (its largest real filesystem = the data volume).
# Upsert key (node_id, 'storage', name=<node>) so the NAS row goes live in place.
# --------------------------------------------------------------------------
class StorageSyncResult(BaseModel):
    nodes: int = 0
    detail: str = ""


def sync_storage_from_vm(node_id: str, client: NovaMLClient | None = None) -> StorageSyncResult:
    """Pull node_exporter filesystem metrics and upsert one storage row per node
    (largest real filesystem). Raises ValueError (→ 422) when VM is unreachable
    or no node reports filesystem metrics."""
    client = client or _nova_client()
    fs_list = client.storage_filesystems()
    if not fs_list:
        raise ValueError(
            "VictoriaMetrics에서 파일시스템 메트릭을 찾지 못했습니다 (node_exporter 확인)."
        )

    grafana_url = get_settings().nova_ml_grafana_url
    details: list[str] = []
    for fs in fs_list:
        # Adaptive unit: TB for >= 1TB volumes, else GB (a 30GB root reads as
        # "0.03TB" otherwise).
        if fs.size_bytes >= 1e12:
            unit, div = "TB", 1e12
        else:
            unit, div = "GB", 1e9
        total = round(fs.size_bytes / div, 2)
        used = round(fs.used_bytes / div, 2)
        pct = round(fs.used_bytes / fs.size_bytes * 100) if fs.size_bytes > 0 else 0
        details.append(f"{fs.node} {used}/{total}{unit} ({pct}%)")
        values = dict(
            kind="storage",
            name=fs.node,
            model=(fs.fstype.upper() or None),
            location=f"Nova: {fs.node} ({fs.mountpoint})",
            status="active",
            capacity=total,
            used=used,
            unit=unit,
            usage_percent=pct,
            link=grafana_url or f"nova-vm:{fs.node}",
        )
        with session() as s:
            existing = s.execute(
                select(infra_resources.c.resource_id).where(
                    infra_resources.c.node_id == node_id,
                    infra_resources.c.kind == "storage",
                    infra_resources.c.name == fs.node,
                )
            ).first()
            if existing:
                s.execute(
                    infra_resources.update()
                    .where(infra_resources.c.resource_id == existing.resource_id)
                    .values(**values, updated_at=func.now())
                )
            else:
                s.execute(
                    infra_resources.insert().values(
                        resource_id=uuid.uuid4(), node_id=node_id, sort_order=0, **values
                    )
                )
            s.commit()

    return StorageSyncResult(nodes=len(fs_list), detail=" · ".join(details))


# --------------------------------------------------------------------------
# MLflow live panel — recent experiment runs from the Nova MLflow (read-only).
# Rendered alongside DB-backed sections, so it never raises on upstream failure:
# it degrades to configured/error flags and the page still loads.
# --------------------------------------------------------------------------
class MlRun(BaseModel):
    run_id: str
    run_name: str
    experiment_id: str
    experiment_name: str
    status: str = ""
    start_time: int | None = None
    end_time: int | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    url: str = ""


class MlPanel(BaseModel):
    # Grafana embed: present whenever Grafana is configured, independent of MLflow.
    grafana_url: str = ""  # base UI URL ("" if unset) — used for the fallback link
    grafana_ok: bool = False  # Grafana reachable AND a dashboard was found → embed
    grafana_embed_url: str = ""  # kiosk iframe src; "" → FE shows a link, not an iframe
    # MLflow runs: configured = MLflow URL set; error set on upstream failure.
    configured: bool = False
    mlflow_url: str = ""
    error: str | None = None
    runs: list[MlRun] = Field(default_factory=list)


def _grafana_embed_url(grafana_url: str, uid: str) -> str:
    """Kiosk-mode dashboard URL for iframe embedding (no Grafana chrome). Empty
    when grafana_url/uid missing. Requires anon Viewer + allow_embedding."""
    if not grafana_url or not uid:
        return ""
    return f"{grafana_url}/d/{uid}/?kiosk&theme=light&from=now-6h&to=now&refresh=30s"


def _grafana_panel(client: NovaMLClient, grafana_url: str) -> tuple[bool, str]:
    """Best-effort Grafana state for the infra page: (reachable, embed_url).
    Discovers a dashboard at runtime (no hardcoded uid) so it survives Grafana
    dashboard renames/changes. Never raises — any failure → (False, "")."""
    if not grafana_url:
        return False, ""
    try:
        if not client.grafana_health():
            return False, ""
        uid = client.resolve_dashboard_uid(get_settings().nova_ml_grafana_dashboard_uid)
    except Exception:  # noqa: BLE001 — page must render regardless of Grafana state
        return False, ""
    if not uid:
        # Grafana is up but has no dashboard to embed → still "ok" enough to link,
        # but no iframe. Report reachable so FE can offer the Grafana home link.
        return True, ""
    return True, _grafana_embed_url(grafana_url, uid)


def ml_panel(limit: int = 20, client: NovaMLClient | None = None) -> MlPanel:
    """Grafana embed + recent MLflow runs for the infra page. NEVER raises: the
    page must render whatever state Grafana/MLflow are in. Grafana unreachable →
    grafana_ok=False (FE shows a link/placeholder, no broken iframe). MLflow
    unset → configured=False; MLflow failure → error set, runs=[]."""
    s = get_settings()
    client = client or _nova_client()
    grafana_url = s.nova_ml_grafana_url
    grafana_ok, embed = _grafana_panel(client, grafana_url)

    base = dict(
        grafana_url=grafana_url,
        grafana_ok=grafana_ok,
        grafana_embed_url=embed,
    )
    mlflow_url = s.nova_ml_mlflow_url
    if not mlflow_url:
        return MlPanel(configured=False, **base)
    try:
        raw = client.recent_runs(limit=limit)
    except ValueError as e:
        return MlPanel(configured=True, mlflow_url=mlflow_url, error=str(e), **base)
    runs = [_ml_run_out(r, mlflow_url) for r in raw]
    return MlPanel(configured=True, mlflow_url=mlflow_url, runs=runs, **base)


def _ml_run_out(raw: dict, mlflow_url: str) -> MlRun:
    info = raw.get("info", {})
    data = raw.get("data", {})
    run_id = info.get("run_id", "")
    tags = {t["key"]: t["value"] for t in data.get("tags", [])}
    run_name = info.get("run_name") or tags.get("mlflow.runName") or run_id[:8]
    exp_id = info.get("experiment_id", "")
    metrics = {m["key"]: m["value"] for m in data.get("metrics", [])}
    url = f"{mlflow_url}/#/experiments/{exp_id}/runs/{run_id}" if mlflow_url and run_id else ""
    return MlRun(
        run_id=run_id,
        run_name=run_name,
        experiment_id=exp_id,
        experiment_name=raw.get("_experiment_name", ""),
        status=info.get("status", ""),
        start_time=info.get("start_time"),
        end_time=info.get("end_time"),
        metrics=metrics,
        url=url,
    )


# --------------------------------------------------------------------------
# Partner companies + contacts (파트너사 관리)
# --------------------------------------------------------------------------
class PartnerContactIn(BaseModel):
    name: str
    role: str | None = None
    phone: str | None = None
    email: str | None = None
    channels: list[str] = Field(default_factory=list)
    link: str | None = None
    memo: str | None = None
    is_primary: bool = False
    sort_order: int = 0


class PartnerContact(PartnerContactIn):
    contact_id: UUID
    partner_id: UUID


class PartnerCompanyIn(BaseModel):
    name: str
    org_type: str | None = None
    address: str | None = None
    representative: str | None = None
    project_ids: list[UUID] = Field(default_factory=list)
    field_tags: list[str] = Field(default_factory=list)
    status: str | None = None
    memo: str | None = None
    next_action: str | None = None
    last_contact: date | None = None
    link: str | None = None
    sort_order: int = 0
    active: bool = True


class PartnerCompany(PartnerCompanyIn):
    partner_id: UUID
    contact_count: int = 0


def _contact_out(row) -> PartnerContact:
    return PartnerContact(
        contact_id=row.contact_id,
        partner_id=row.partner_id,
        name=row.name,
        role=row.role,
        phone=row.phone,
        email=row.email,
        channels=list(row.channels or []),
        link=row.link,
        memo=row.memo,
        is_primary=row.is_primary,
        sort_order=row.sort_order,
    )


_CONTACT_COLS = (
    partner_contacts.c.contact_id,
    partner_contacts.c.partner_id,
    partner_contacts.c.name,
    partner_contacts.c.role,
    partner_contacts.c.phone,
    partner_contacts.c.email,
    partner_contacts.c.channels,
    partner_contacts.c.link,
    partner_contacts.c.memo,
    partner_contacts.c.is_primary,
    partner_contacts.c.sort_order,
)

_PARTNER_COLS = (
    partner_companies.c.partner_id,
    partner_companies.c.name,
    partner_companies.c.org_type,
    partner_companies.c.address,
    partner_companies.c.representative,
    partner_companies.c.project_ids,
    partner_companies.c.field_tags,
    partner_companies.c.status,
    partner_companies.c.memo,
    partner_companies.c.next_action,
    partner_companies.c.last_contact,
    partner_companies.c.link,
    partner_companies.c.sort_order,
    partner_companies.c.active,
)


def _partner_out(row, contact_count: int = 0) -> PartnerCompany:
    return PartnerCompany(
        partner_id=row.partner_id,
        name=row.name,
        org_type=row.org_type,
        address=row.address,
        representative=row.representative,
        project_ids=[UUID(str(p)) for p in (row.project_ids or [])],
        field_tags=list(row.field_tags or []),
        status=row.status,
        memo=row.memo,
        next_action=row.next_action,
        last_contact=row.last_contact,
        link=row.link,
        sort_order=row.sort_order,
        active=row.active,
        contact_count=contact_count,
    )


def _partner_values(body: PartnerCompanyIn) -> dict:
    values = body.model_dump(exclude={"name"})
    values["project_ids"] = [str(p) for p in body.project_ids]
    return values


def list_partners(node_id: str, project_id: UUID | None = None) -> list[PartnerCompany]:
    with session() as s:
        rows = s.execute(
            select(*_PARTNER_COLS)
            .where(partner_companies.c.node_id == node_id)
            .order_by(partner_companies.c.sort_order, partner_companies.c.name)
        ).all()
        counts = dict(
            s.execute(
                select(partner_contacts.c.partner_id, func.count().label("n"))
                .where(partner_contacts.c.node_id == node_id)
                .group_by(partner_contacts.c.partner_id)
            ).all()
        )
    out = [_partner_out(r, int(counts.get(r.partner_id, 0))) for r in rows]
    if project_id is not None:
        pid = str(project_id)
        out = [p for p in out if pid in {str(x) for x in p.project_ids}]
    return out


def create_partner(node_id: str, body: PartnerCompanyIn) -> PartnerCompany:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    with session() as s:
        row = s.execute(
            partner_companies.insert()
            .values(partner_id=uuid.uuid4(), node_id=node_id, name=name, **_partner_values(body))
            .returning(*_PARTNER_COLS)
        ).one()
        s.commit()
    return _partner_out(row)


def update_partner(node_id: str, partner_id: UUID, body: PartnerCompanyIn) -> PartnerCompany:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    with session() as s:
        result = s.execute(
            partner_companies.update()
            .where(
                partner_companies.c.node_id == node_id,
                partner_companies.c.partner_id == partner_id,
            )
            .values(name=name, **_partner_values(body), updated_at=func.now())
            .returning(*_PARTNER_COLS)
        ).first()
        if result is None:
            raise LookupError("partner not found")
        count = s.execute(
            select(func.count())
            .select_from(partner_contacts)
            .where(partner_contacts.c.partner_id == partner_id)
        ).scalar_one()
        s.commit()
    return _partner_out(result, int(count))


def delete_partner(node_id: str, partner_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(partner_companies).where(
                partner_companies.c.node_id == node_id,
                partner_companies.c.partner_id == partner_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("partner not found")


def _require_partner(s, node_id: str, partner_id: UUID) -> None:
    exists = s.execute(
        select(partner_companies.c.partner_id).where(
            partner_companies.c.node_id == node_id,
            partner_companies.c.partner_id == partner_id,
        )
    ).first()
    if exists is None:
        raise LookupError("partner not found")


def list_partner_contacts(node_id: str, partner_id: UUID) -> list[PartnerContact]:
    with session() as s:
        _require_partner(s, node_id, partner_id)
        rows = s.execute(
            select(*_CONTACT_COLS)
            .where(
                partner_contacts.c.node_id == node_id,
                partner_contacts.c.partner_id == partner_id,
            )
            .order_by(
                partner_contacts.c.is_primary.desc(),
                partner_contacts.c.sort_order,
                partner_contacts.c.name,
            )
        ).all()
    return [_contact_out(r) for r in rows]


def create_partner_contact(
    node_id: str, partner_id: UUID, body: PartnerContactIn
) -> PartnerContact:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    with session() as s:
        _require_partner(s, node_id, partner_id)
        row = s.execute(
            partner_contacts.insert()
            .values(
                contact_id=uuid.uuid4(),
                node_id=node_id,
                partner_id=partner_id,
                name=name,
                **body.model_dump(exclude={"name"}),
            )
            .returning(*_CONTACT_COLS)
        ).one()
        s.commit()
    return _contact_out(row)


def update_partner_contact(
    node_id: str, contact_id: UUID, body: PartnerContactIn
) -> PartnerContact:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    with session() as s:
        result = s.execute(
            partner_contacts.update()
            .where(
                partner_contacts.c.node_id == node_id,
                partner_contacts.c.contact_id == contact_id,
            )
            .values(name=name, **body.model_dump(exclude={"name"}), updated_at=func.now())
            .returning(*_CONTACT_COLS)
        ).first()
        s.commit()
    if result is None:
        raise LookupError("contact not found")
    return _contact_out(result)


def delete_partner_contact(node_id: str, contact_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(partner_contacts).where(
                partner_contacts.c.node_id == node_id,
                partner_contacts.c.contact_id == contact_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("contact not found")


# --------------------------------------------------------------------------
# Support programs (지원사업 보드)
# --------------------------------------------------------------------------
SUPPORT_PROGRAM_STATUSES = ("시작 전", "서류제출완", "발표준비", "합격", "유감", "협약취소")
SUPPORT_DEFAULT_OWNER_NAME = "박기획"


class SupportProgramIn(BaseModel):
    name: str
    status: str = "시작 전"
    project_id: UUID | None = None
    company: str | None = None
    deadline: date | None = None
    presentation_deadline: date | None = None
    presentation_date: date | None = None
    presentation_time: time | None = None
    owner_member_id: UUID | None = None
    task_number: str | None = None
    url: str | None = None
    follow_up: str | None = None
    body: str | None = None
    sort_order: int = 0


class SupportProgram(SupportProgramIn):
    program_id: UUID
    project_name: str | None = None
    owner_name: str | None = None
    calendar_event_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


_SUPPORT_COLS = (
    support_programs.c.program_id,
    support_programs.c.name,
    support_programs.c.status,
    support_programs.c.project_id,
    support_programs.c.company,
    support_programs.c.deadline,
    support_programs.c.presentation_deadline,
    support_programs.c.presentation_date,
    support_programs.c.presentation_time,
    support_programs.c.owner_member_id,
    support_programs.c.calendar_event_id,
    support_programs.c.task_number,
    support_programs.c.url,
    support_programs.c.follow_up,
    support_programs.c.body,
    support_programs.c.sort_order,
    support_programs.c.created_at,
    support_programs.c.updated_at,
)


def _support_out(
    row,
    project_name: str | None = None,
    owner_name: str | None = None,
    calendar_event_id: UUID | None = None,
    calendar_event_known: bool = False,
) -> SupportProgram:
    return SupportProgram(
        program_id=row.program_id,
        name=row.name,
        status=row.status,
        project_id=row.project_id,
        project_name=project_name,
        company=row.company,
        deadline=row.deadline,
        presentation_deadline=row.presentation_deadline,
        presentation_date=row.presentation_date,
        presentation_time=row.presentation_time,
        owner_member_id=row.owner_member_id,
        owner_name=owner_name,
        calendar_event_id=(calendar_event_id if calendar_event_known else row.calendar_event_id),
        task_number=row.task_number,
        url=row.url,
        follow_up=row.follow_up,
        body=row.body,
        sort_order=row.sort_order,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _support_validate(body: SupportProgramIn) -> str:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    if body.status not in SUPPORT_PROGRAM_STATUSES:
        raise ValueError(f"status must be one of {SUPPORT_PROGRAM_STATUSES}")
    return name


def _project_names(s, node_id: str) -> dict[UUID, str]:
    return dict(
        s.execute(
            select(dashboard_projects.c.project_id, dashboard_projects.c.name).where(
                dashboard_projects.c.node_id == node_id
            )
        ).all()
    )


def _member_names(s, node_id: str) -> dict[UUID, str]:
    return dict(
        s.execute(
            select(team_members.c.member_id, team_members.c.name).where(
                team_members.c.node_id == node_id
            )
        ).all()
    )


def member_identity(node_id: str, user_id: UUID) -> tuple[str, str | None]:
    """프레즌스 표시용: user_id에 연결된 팀원의 이름/색. 없으면 기본값."""
    with session() as s:
        row = s.execute(
            select(team_members.c.name, team_members.c.color).where(
                team_members.c.node_id == node_id,
                team_members.c.user_id == user_id,
            )
        ).first()
    if row is not None:
        return row.name, row.color
    return "사용자", None


def _default_owner_id(s, node_id: str) -> UUID | None:
    row = s.execute(
        select(team_members.c.member_id).where(
            team_members.c.node_id == node_id,
            team_members.c.name == SUPPORT_DEFAULT_OWNER_NAME,
        )
    ).first()
    return row.member_id if row else None


def _sync_presentation_event(s, node_id: str, row, created_by: UUID) -> UUID | None:
    """발표일이 설정되면 팀 일정에 자동 등록하고, 바뀌면 갱신, 비우면 제거한다.

    Returns the linked calendar event id (or None). Caller persists it on the
    support_programs row within the same transaction.
    """
    event_id: UUID | None = row.calendar_event_id
    if row.presentation_date is None:
        if event_id is not None:
            s.execute(
                delete(team_calendar_events).where(
                    team_calendar_events.c.node_id == node_id,
                    team_calendar_events.c.event_id == event_id,
                )
            )
        return None

    title = f"[발표] {row.name}"
    member_ids = [str(row.owner_member_id)] if row.owner_member_id else []
    # 발표 시간이 있으면 시간 일정으로, 없으면 종일 일정으로 등록한다.
    all_day = row.presentation_time is None
    start_time = row.presentation_time
    if event_id is not None:
        updated = s.execute(
            team_calendar_events.update()
            .where(
                team_calendar_events.c.node_id == node_id,
                team_calendar_events.c.event_id == event_id,
            )
            .values(
                title=title,
                event_date=row.presentation_date,
                all_day=all_day,
                start_time=start_time,
                member_ids=member_ids,
                project_id=row.project_id,
                updated_at=func.now(),
            )
        )
        if updated.rowcount > 0:
            return event_id
    event_id = uuid.uuid4()
    s.execute(
        team_calendar_events.insert().values(
            event_id=event_id,
            node_id=node_id,
            created_by=created_by,
            title=title,
            description="지원사업 보드에서 자동 등록된 발표 일정",
            all_day=all_day,
            event_date=row.presentation_date,
            start_time=start_time,
            event_type="event",
            member_ids=member_ids,
            project_id=row.project_id,
        )
    )
    return event_id


def list_support_programs(node_id: str, project_id: UUID | None = None) -> list[SupportProgram]:
    with session() as s:
        q = (
            select(*_SUPPORT_COLS)
            .where(support_programs.c.node_id == node_id)
            .order_by(support_programs.c.sort_order, support_programs.c.created_at)
        )
        if project_id is not None:
            q = q.where(support_programs.c.project_id == project_id)
        rows = s.execute(q).all()
        names = _project_names(s, node_id)
        members = _member_names(s, node_id)
    return [_support_out(r, names.get(r.project_id), members.get(r.owner_member_id)) for r in rows]


def create_support_program(
    node_id: str, body: SupportProgramIn, created_by: UUID
) -> SupportProgram:
    name = _support_validate(body)
    with session() as s:
        values = body.model_dump(exclude={"name"})
        if values.get("owner_member_id") is None:
            values["owner_member_id"] = _default_owner_id(s, node_id)
        row = s.execute(
            support_programs.insert()
            .values(program_id=uuid.uuid4(), node_id=node_id, name=name, **values)
            .returning(*_SUPPORT_COLS)
        ).one()
        event_id = _sync_presentation_event(s, node_id, row, created_by)
        if event_id != row.calendar_event_id:
            s.execute(
                support_programs.update()
                .where(support_programs.c.program_id == row.program_id)
                .values(calendar_event_id=event_id)
            )
        names = _project_names(s, node_id)
        members = _member_names(s, node_id)
        s.commit()
    return _support_out(
        row,
        names.get(row.project_id),
        members.get(row.owner_member_id),
        event_id,
        calendar_event_known=True,
    )


def update_support_program(
    node_id: str, program_id: UUID, body: SupportProgramIn, created_by: UUID
) -> SupportProgram:
    name = _support_validate(body)
    with session() as s:
        row = s.execute(
            support_programs.update()
            .where(
                support_programs.c.node_id == node_id,
                support_programs.c.program_id == program_id,
            )
            .values(
                **{**body.model_dump(exclude={"name"}), "name": name},
                updated_at=func.now(),
            )
            .returning(*_SUPPORT_COLS)
        ).first()
        if row is None:
            raise LookupError("support program not found")
        event_id = _sync_presentation_event(s, node_id, row, created_by)
        if event_id != row.calendar_event_id:
            s.execute(
                support_programs.update()
                .where(support_programs.c.program_id == program_id)
                .values(calendar_event_id=event_id)
            )
        names = _project_names(s, node_id)
        members = _member_names(s, node_id)
        s.commit()
    return _support_out(
        row,
        names.get(row.project_id),
        members.get(row.owner_member_id),
        event_id,
        calendar_event_known=True,
    )


def delete_support_program(node_id: str, program_id: UUID) -> None:
    with session() as s:
        row = s.execute(
            delete(support_programs)
            .where(
                support_programs.c.node_id == node_id,
                support_programs.c.program_id == program_id,
            )
            .returning(support_programs.c.calendar_event_id)
        ).first()
        if row is not None and row.calendar_event_id is not None:
            s.execute(
                delete(team_calendar_events).where(
                    team_calendar_events.c.node_id == node_id,
                    team_calendar_events.c.event_id == row.calendar_event_id,
                )
            )
        s.commit()
    if row is None:
        raise LookupError("support program not found")


class SupportReorderIn(BaseModel):
    status: str
    ordered_ids: list[UUID] = Field(default_factory=list)


def reorder_support_programs(node_id: str, body: SupportReorderIn) -> list[SupportProgram]:
    """드래그 정렬: 한 상태 컬럼의 카드 순서를 통째로 재배치한다.

    ``ordered_ids`` 순서대로 각 카드의 ``status``를 대상 컬럼으로 맞추고
    ``sort_order``를 0..n으로 다시 매긴다. 다른 컬럼에서 끌어온 카드의 컬럼 이동도
    같은 호출로 처리된다(컬럼 이동 + 위치 지정 한 번에).
    """
    if body.status not in SUPPORT_PROGRAM_STATUSES:
        raise ValueError(f"status must be one of {SUPPORT_PROGRAM_STATUSES}")
    with session() as s:
        for idx, pid in enumerate(body.ordered_ids):
            s.execute(
                support_programs.update()
                .where(
                    support_programs.c.node_id == node_id,
                    support_programs.c.program_id == pid,
                )
                .values(status=body.status, sort_order=idx, updated_at=func.now())
            )
        s.commit()
    return list_support_programs(node_id)


# --------------------------------------------------------------------------
# Support notes (지원사업 꿀팁 / 검색 사이트 — 보드 아래 인라인 DB)
# --------------------------------------------------------------------------
SUPPORT_NOTE_KINDS = ("tip", "site")


class SupportNoteIn(BaseModel):
    kind: str = "tip"
    title: str
    description: str | None = None
    url: str | None = None
    body: str | None = None
    sort_order: int = 0


class SupportNote(SupportNoteIn):
    note_id: UUID


_SUPPORT_NOTE_COLS = (
    support_notes.c.note_id,
    support_notes.c.kind,
    support_notes.c.title,
    support_notes.c.description,
    support_notes.c.url,
    support_notes.c.body,
    support_notes.c.sort_order,
)


def _support_note_out(row) -> SupportNote:
    return SupportNote(
        note_id=row.note_id,
        kind=row.kind,
        title=row.title,
        description=row.description,
        url=row.url,
        body=row.body,
        sort_order=row.sort_order,
    )


def _support_note_validate(body: SupportNoteIn) -> str:
    title = body.title.strip()
    if not title:
        raise ValueError("title required")
    if body.kind not in SUPPORT_NOTE_KINDS:
        raise ValueError(f"kind must be one of {SUPPORT_NOTE_KINDS}")
    return title


def list_support_notes(node_id: str, kind: str | None = None) -> list[SupportNote]:
    with session() as s:
        q = (
            select(*_SUPPORT_NOTE_COLS)
            .where(support_notes.c.node_id == node_id)
            .order_by(support_notes.c.sort_order, support_notes.c.created_at)
        )
        if kind is not None:
            q = q.where(support_notes.c.kind == kind)
        rows = s.execute(q).all()
    return [_support_note_out(r) for r in rows]


def create_support_note(node_id: str, body: SupportNoteIn) -> SupportNote:
    title = _support_note_validate(body)
    with session() as s:
        row = s.execute(
            support_notes.insert()
            .values(
                note_id=uuid.uuid4(),
                node_id=node_id,
                **{**body.model_dump(exclude={"title"}), "title": title},
            )
            .returning(*_SUPPORT_NOTE_COLS)
        ).one()
        s.commit()
    return _support_note_out(row)


def update_support_note(node_id: str, note_id: UUID, body: SupportNoteIn) -> SupportNote:
    title = _support_note_validate(body)
    with session() as s:
        row = s.execute(
            support_notes.update()
            .where(support_notes.c.node_id == node_id, support_notes.c.note_id == note_id)
            .values(
                **{**body.model_dump(exclude={"title"}), "title": title},
                updated_at=func.now(),
            )
            .returning(*_SUPPORT_NOTE_COLS)
        ).first()
        if row is None:
            raise LookupError("support note not found")
        s.commit()
    return _support_note_out(row)


def delete_support_note(node_id: str, note_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(support_notes).where(
                support_notes.c.node_id == node_id, support_notes.c.note_id == note_id
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("support note not found")


# ==========================================================================
# 회사 KPI (OKR + North Star Metric)
# 계층: north_star → objective(연간) → key_result(분기) → target(월간).
# 단일 테이블 + parent_id. cadence/period로 월간·분기·년간 관리. company node only.
# ==========================================================================
KPI_LEVELS = ("north_star", "objective", "key_result", "target")
KPI_CADENCES = ("annual", "quarterly", "monthly")
KPI_METRIC_TYPES = ("number", "percent", "currency", "count", "boolean")
KPI_DIRECTIONS = ("up", "down")
KPI_STATUSES = ("on_track", "at_risk", "off_track", "done", "archived")


class KpiIn(BaseModel):
    parent_id: UUID | None = None
    level: str
    cadence: str
    fiscal_year: int
    quarter: int | None = None
    month: int | None = None
    title: str
    description: str | None = None
    metric_type: str = "number"
    unit: str | None = None
    baseline: float | None = None
    target: float | None = None
    current_value: float | None = None
    direction: str = "up"
    project_id: UUID | None = None
    owner_member_id: UUID | None = None
    status: str = "on_track"
    sort_order: int = 0


class Kpi(KpiIn):
    kpi_id: UUID
    period_start: date | None = None
    progress: float | None = None
    # 주기말 공식 채점(0~10 정수, None=미채점 — 0 유효). KpiIn에는 없어서
    # PATCH /kpis(body=KpiIn)로는 구조적으로 쓸 수 없다(grade 엔드포인트 전용).
    grade: int | None = None
    grade_note: str | None = None
    graded_at: datetime | None = None
    graded_by_member_id: UUID | None = None
    # 마감 진실: 채점이 있으면 grade/10, 없으면 측정 progress. 트리/요약 롤업 기준.
    # (신뢰도는 여기 없다 — 표시 전용 신호라 confidence API로만 노출.)
    final_progress: float | None = None


class KpiTreeNode(Kpi):
    children: list["KpiTreeNode"] = Field(default_factory=list)


class KpiCheckinIn(BaseModel):
    value: float | None = None
    status: str | None = None
    note: str | None = None
    author_member_id: UUID | None = None


class KpiCheckin(KpiCheckinIn):
    checkin_id: UUID
    kpi_id: UUID
    checkin_date: date
    created_at: datetime


class KpiSummary(BaseModel):
    fiscal_year: int
    north_star: Kpi | None = None
    objectives: int = 0
    key_results: int = 0
    on_track: int = 0
    at_risk: int = 0
    off_track: int = 0
    # final_progress(채점 우선) 기준 평균 — 채점 도입 후 수치가 움직이는 건 의도.
    avg_progress: float | None = None
    # 채점 현황(target/key_result 레벨만 집계).
    targets_graded: int = 0
    targets_total: int = 0
    kr_graded: int = 0


class KpiLinked(BaseModel):
    project_id: UUID | None = None
    weekly_plan_count: int = 0
    weekly_plan_done: int = 0
    monthly_plan_count: int = 0
    monthly_plan_done: int = 0


def _kpi_period_start(cadence: str, year: int, quarter: int | None, month: int | None) -> date:
    if cadence == "quarterly":
        q = quarter or 1
        return date(year, 3 * q - 2, 1)
    if cadence == "monthly":
        return date(year, month or 1, 1)
    return date(year, 1, 1)


def _quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    start = date(year, 3 * quarter - 2, 1)
    end_month = 3 * quarter
    if end_month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, end_month + 1, 1) - timedelta(days=1)
    return start, end


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def _f(v) -> float | None:
    if v is None:
        return None
    return float(v)


def _own_progress(
    metric_type: str,
    baseline: float | None,
    target: float | None,
    current: float | None,
    direction: str,
) -> float | None:
    if metric_type == "boolean":
        return None if current is None else (1.0 if current >= 1 else 0.0)
    if target is None or current is None:
        return None
    b = baseline or 0.0
    if direction == "down":
        denom = b - target
        if denom == 0:
            return None
        raw = (b - current) / denom
    else:
        denom = target - b
        if denom == 0:
            return None
        raw = (current - b) / denom
    return max(0.0, min(1.0, raw))


_KPI_COLS = (
    dashboard_kpis.c.kpi_id,
    dashboard_kpis.c.parent_id,
    dashboard_kpis.c.level,
    dashboard_kpis.c.cadence,
    dashboard_kpis.c.period_start,
    dashboard_kpis.c.fiscal_year,
    dashboard_kpis.c.quarter,
    dashboard_kpis.c.month,
    dashboard_kpis.c.title,
    dashboard_kpis.c.description,
    dashboard_kpis.c.metric_type,
    dashboard_kpis.c.unit,
    dashboard_kpis.c.baseline,
    dashboard_kpis.c.target,
    dashboard_kpis.c.current_value,
    dashboard_kpis.c.direction,
    dashboard_kpis.c.project_id,
    dashboard_kpis.c.owner_member_id,
    dashboard_kpis.c.status,
    dashboard_kpis.c.sort_order,
    dashboard_kpis.c.grade,
    dashboard_kpis.c.grade_note,
    dashboard_kpis.c.graded_at,
    dashboard_kpis.c.graded_by,
)


def _kpi_out(row) -> Kpi:
    baseline = _f(row.baseline)
    target = _f(row.target)
    current = _f(row.current_value)
    progress = _own_progress(row.metric_type, baseline, target, current, row.direction)
    # grade=0은 유효한 채점(final 0.0) — is not None으로만 분기한다.
    final = (row.grade / 10.0) if row.grade is not None else progress
    return Kpi(
        kpi_id=row.kpi_id,
        parent_id=row.parent_id,
        level=row.level,
        cadence=row.cadence,
        period_start=row.period_start,
        fiscal_year=row.fiscal_year,
        quarter=row.quarter,
        month=row.month,
        title=row.title,
        description=row.description,
        metric_type=row.metric_type,
        unit=row.unit,
        baseline=baseline,
        target=target,
        current_value=current,
        direction=row.direction,
        project_id=row.project_id,
        owner_member_id=row.owner_member_id,
        status=row.status,
        sort_order=row.sort_order,
        progress=progress,
        grade=row.grade,
        grade_note=row.grade_note,
        graded_at=row.graded_at,
        graded_by_member_id=row.graded_by,
        final_progress=final,
    )


def _kpi_validate(node_id: str, body: KpiIn) -> None:
    if body.level not in KPI_LEVELS:
        raise ValueError("invalid level")
    if body.cadence not in KPI_CADENCES:
        raise ValueError("invalid cadence")
    if body.metric_type not in KPI_METRIC_TYPES:
        raise ValueError("invalid metric_type")
    if body.direction not in KPI_DIRECTIONS:
        raise ValueError("invalid direction")
    if body.status not in KPI_STATUSES:
        raise ValueError("invalid status")
    if not body.title.strip():
        raise ValueError("title required")
    if body.cadence == "quarterly" and not body.quarter:
        raise ValueError("quarter required for quarterly")
    if body.cadence == "monthly" and not body.month:
        raise ValueError("month required for monthly")
    with session() as s:
        if body.parent_id is not None:
            parent = s.execute(
                select(dashboard_kpis.c.kpi_id).where(
                    dashboard_kpis.c.node_id == node_id,
                    dashboard_kpis.c.kpi_id == body.parent_id,
                )
            ).first()
            if parent is None:
                raise LookupError("parent kpi not found")
        if body.project_id is not None:
            proj = s.execute(
                select(dashboard_projects.c.project_id).where(
                    dashboard_projects.c.node_id == node_id,
                    dashboard_projects.c.project_id == body.project_id,
                )
            ).first()
            if proj is None:
                raise LookupError("project not found")


def _kpi_values(body: KpiIn) -> dict:
    return {
        "parent_id": body.parent_id,
        "level": body.level,
        "cadence": body.cadence,
        "period_start": _kpi_period_start(body.cadence, body.fiscal_year, body.quarter, body.month),
        "fiscal_year": body.fiscal_year,
        "quarter": body.quarter if body.cadence == "quarterly" else None,
        "month": body.month if body.cadence == "monthly" else None,
        "title": body.title.strip(),
        "description": body.description,
        "metric_type": body.metric_type,
        "unit": body.unit,
        "baseline": body.baseline,
        "target": body.target,
        "current_value": body.current_value,
        "direction": body.direction,
        "project_id": body.project_id,
        "owner_member_id": body.owner_member_id,
        "status": body.status,
        "sort_order": body.sort_order,
    }


def list_kpis(
    node_id: str,
    *,
    fiscal_year: int | None = None,
    cadence: str | None = None,
    quarter: int | None = None,
    month: int | None = None,
    project_id: UUID | None = None,
    level: str | None = None,
) -> list[Kpi]:
    stmt = select(*_KPI_COLS).where(dashboard_kpis.c.node_id == node_id)
    if fiscal_year is not None:
        stmt = stmt.where(dashboard_kpis.c.fiscal_year == fiscal_year)
    if cadence is not None:
        stmt = stmt.where(dashboard_kpis.c.cadence == cadence)
    if quarter is not None:
        stmt = stmt.where(dashboard_kpis.c.quarter == quarter)
    if month is not None:
        stmt = stmt.where(dashboard_kpis.c.month == month)
    if project_id is not None:
        stmt = stmt.where(dashboard_kpis.c.project_id == project_id)
    if level is not None:
        stmt = stmt.where(dashboard_kpis.c.level == level)
    stmt = stmt.order_by(dashboard_kpis.c.sort_order, dashboard_kpis.c.created_at)
    with session() as s:
        rows = s.execute(stmt).all()
    return [_kpi_out(r) for r in rows]


_LEVEL_RANK = {"north_star": 0, "objective": 1, "key_result": 2, "target": 3}


def kpi_tree(
    node_id: str, *, fiscal_year: int, project_id: UUID | None = None
) -> list[KpiTreeNode]:
    items = list_kpis(node_id, fiscal_year=fiscal_year, project_id=project_id)
    nodes: dict[UUID, KpiTreeNode] = {k.kpi_id: KpiTreeNode(**k.model_dump()) for k in items}
    roots: list[KpiTreeNode] = []
    for node in nodes.values():
        parent = nodes.get(node.parent_id) if node.parent_id else None
        if parent is not None:
            parent.children.append(node)
        else:
            roots.append(node)

    def _rollup(node: KpiTreeNode) -> None:
        for child in node.children:
            _rollup(child)
        node.children.sort(key=lambda c: (c.sort_order, c.title))
        if node.progress is None and node.children:
            vals = [c.progress for c in node.children if c.progress is not None]
            if vals:
                node.progress = sum(vals) / len(vals)
        # 마감 진실 병행 롤업: 자기 값(채점/측정)이 없으면 자식 final 평균.
        # 채점된 자식은 grade가, 미채점 자식은 측정이 섞여 올라간다.
        if node.final_progress is None and node.children:
            fvals = [c.final_progress for c in node.children if c.final_progress is not None]
            if fvals:
                node.final_progress = sum(fvals) / len(fvals)

    for r in roots:
        _rollup(r)
    roots.sort(key=lambda n: (_LEVEL_RANK.get(n.level, 9), n.sort_order, n.title))
    return roots


def kpi_summary(node_id: str, fiscal_year: int, project_id: UUID | None = None) -> KpiSummary:
    items = list_kpis(node_id, fiscal_year=fiscal_year, project_id=project_id)
    north = next((k for k in items if k.level == "north_star"), None)
    objectives = [k for k in items if k.level == "objective"]
    krs = [k for k in items if k.level == "key_result"]
    targets = [k for k in items if k.level == "target"]
    # 채점 우선 마감 진실(final_progress) 기준 — 채점이 쌓이면 평균이 움직인다(의도).
    measured = [
        k.final_progress for k in items if k.final_progress is not None and k.level != "objective"
    ]
    return KpiSummary(
        fiscal_year=fiscal_year,
        north_star=north,
        objectives=len(objectives),
        key_results=len(krs),
        on_track=sum(1 for k in krs if k.status == "on_track"),
        at_risk=sum(1 for k in krs if k.status == "at_risk"),
        off_track=sum(1 for k in krs if k.status == "off_track"),
        avg_progress=(sum(measured) / len(measured)) if measured else None,
        targets_graded=sum(1 for k in targets if k.grade is not None),
        targets_total=len(targets),
        kr_graded=sum(1 for k in krs if k.grade is not None),
    )


def create_kpi(node_id: str, body: KpiIn) -> Kpi:
    _kpi_validate(node_id, body)
    with session() as s:
        row = s.execute(
            dashboard_kpis.insert()
            .values(kpi_id=uuid.uuid4(), node_id=node_id, **_kpi_values(body))
            .returning(*_KPI_COLS)
        ).one()
        s.commit()
    return _kpi_out(row)


def update_kpi(node_id: str, kpi_id: UUID, body: KpiIn) -> Kpi:
    _kpi_validate(node_id, body)
    with session() as s:
        row = s.execute(
            dashboard_kpis.update()
            .where(
                dashboard_kpis.c.node_id == node_id,
                dashboard_kpis.c.kpi_id == kpi_id,
            )
            .values(**_kpi_values(body), updated_at=func.now())
            .returning(*_KPI_COLS)
        ).first()
        s.commit()
    if row is None:
        raise LookupError("kpi not found")
    return _kpi_out(row)


def delete_kpi(node_id: str, kpi_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(dashboard_kpis).where(
                dashboard_kpis.c.node_id == node_id,
                dashboard_kpis.c.kpi_id == kpi_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("kpi not found")


_CHECKIN_COLS = (
    dashboard_kpi_checkins.c.checkin_id,
    dashboard_kpi_checkins.c.kpi_id,
    dashboard_kpi_checkins.c.value,
    dashboard_kpi_checkins.c.status,
    dashboard_kpi_checkins.c.note,
    dashboard_kpi_checkins.c.author_member_id,
    dashboard_kpi_checkins.c.checkin_date,
    dashboard_kpi_checkins.c.created_at,
)


def _checkin_out(row) -> KpiCheckin:
    return KpiCheckin(
        checkin_id=row.checkin_id,
        kpi_id=row.kpi_id,
        value=_f(row.value),
        status=row.status,
        note=row.note,
        author_member_id=row.author_member_id,
        checkin_date=row.checkin_date,
        created_at=row.created_at,
    )


def list_kpi_checkins(node_id: str, kpi_id: UUID) -> list[KpiCheckin]:
    with session() as s:
        rows = s.execute(
            select(*_CHECKIN_COLS)
            .where(
                dashboard_kpi_checkins.c.node_id == node_id,
                dashboard_kpi_checkins.c.kpi_id == kpi_id,
            )
            .order_by(
                dashboard_kpi_checkins.c.checkin_date.desc(),
                dashboard_kpi_checkins.c.created_at.desc(),
            )
        ).all()
    return [_checkin_out(r) for r in rows]


def create_kpi_checkin(node_id: str, kpi_id: UUID, body: KpiCheckinIn) -> KpiCheckin:
    if body.status is not None and body.status not in KPI_STATUSES:
        raise ValueError("invalid status")
    with session() as s:
        owner = s.execute(
            select(dashboard_kpis.c.kpi_id).where(
                dashboard_kpis.c.node_id == node_id, dashboard_kpis.c.kpi_id == kpi_id
            )
        ).first()
        if owner is None:
            raise LookupError("kpi not found")
        row = s.execute(
            dashboard_kpi_checkins.insert()
            .values(
                checkin_id=uuid.uuid4(),
                node_id=node_id,
                kpi_id=kpi_id,
                value=body.value,
                status=body.status,
                note=body.note,
                author_member_id=body.author_member_id,
                checkin_date=date.today(),
            )
            .returning(*_CHECKIN_COLS)
        ).one()
        # 체크인이 KPI의 현재값/상태를 동기화한다(같은 트랜잭션).
        sync: dict = {"updated_at": func.now()}
        if body.value is not None:
            sync["current_value"] = body.value
        if body.status is not None:
            sync["status"] = body.status
        s.execute(
            dashboard_kpis.update()
            .where(dashboard_kpis.c.node_id == node_id, dashboard_kpis.c.kpi_id == kpi_id)
            .values(**sync)
        )
        s.commit()
    return _checkin_out(row)


def kpi_linked(node_id: str, kpi_id: UUID) -> KpiLinked:
    """KPI와 같은 프로젝트·기간의 계획/보드 데이터를 읽기전용 집계로 보여준다.
    개인 보드 task는 카운트만 노출(본문/title 비노출 — privacy 준수)."""
    with session() as s:
        row = s.execute(
            select(
                dashboard_kpis.c.project_id,
                dashboard_kpis.c.cadence,
                dashboard_kpis.c.fiscal_year,
                dashboard_kpis.c.quarter,
                dashboard_kpis.c.month,
            ).where(dashboard_kpis.c.node_id == node_id, dashboard_kpis.c.kpi_id == kpi_id)
        ).first()
        if row is None:
            raise LookupError("kpi not found")
        out = KpiLinked(project_id=row.project_id)
        if row.project_id is None:
            return out
        year = row.fiscal_year
        if row.cadence == "monthly" and row.month:
            start, end = _month_bounds(year, row.month)
        elif row.cadence == "quarterly" and row.quarter:
            start, end = _quarter_bounds(year, row.quarter)
        else:
            start, end = date(year, 1, 1), date(year, 12, 31)

        weekly = s.execute(
            select(weekly_entries.c.plan_items).where(
                weekly_entries.c.node_id == node_id,
                weekly_entries.c.project_id == row.project_id,
                weekly_entries.c.week_start >= start,
                weekly_entries.c.week_start <= end,
            )
        ).all()
        for w in weekly:
            items = w.plan_items or []
            out.weekly_plan_count += len(items)
            out.weekly_plan_done += sum(
                1 for it in items if isinstance(it, dict) and it.get("done")
            )

        monthly = s.execute(
            select(monthly_entries.c.plan_items).where(
                monthly_entries.c.node_id == node_id,
                monthly_entries.c.project_id == row.project_id,
                monthly_entries.c.month >= start,
                monthly_entries.c.month <= end,
            )
        ).all()
        for m in monthly:
            items = m.plan_items or []
            out.monthly_plan_count += len(items)
            out.monthly_plan_done += sum(
                1 for it in items if isinstance(it, dict) and it.get("done")
            )
    return out


class KpiWeeklyItem(BaseModel):
    """KPI에 태그된 주간 계획 실행 항목(계획·회고 weekly plan_item)."""

    entry_id: UUID
    project_id: UUID
    week_start: date
    item_id: str
    text: str
    done: bool


def kpi_weekly_items(node_id: str, kpi_id: UUID) -> list[KpiWeeklyItem]:
    """해당 KPI(kpi_id)에 연결된 주간 계획 항목을 모은다. KPI 연도로 범위 한정."""
    kid = str(kpi_id)
    with session() as s:
        krow = s.execute(
            select(dashboard_kpis.c.fiscal_year).where(
                dashboard_kpis.c.node_id == node_id, dashboard_kpis.c.kpi_id == kpi_id
            )
        ).first()
        if krow is None:
            raise LookupError("kpi not found")
        y = krow.fiscal_year
        rows = s.execute(
            select(
                weekly_entries.c.entry_id,
                weekly_entries.c.project_id,
                weekly_entries.c.week_start,
                weekly_entries.c.plan_items,
            )
            .where(
                weekly_entries.c.node_id == node_id,
                weekly_entries.c.week_start >= date(y, 1, 1),
                weekly_entries.c.week_start <= date(y, 12, 31),
            )
            .order_by(weekly_entries.c.week_start.asc())
        ).all()
    out: list[KpiWeeklyItem] = []
    for r in rows:
        for it in r.plan_items or []:
            if isinstance(it, dict) and it.get("kpi_id") == kid:
                out.append(
                    KpiWeeklyItem(
                        entry_id=r.entry_id,
                        project_id=r.project_id,
                        week_start=r.week_start,
                        item_id=str(it.get("id", "")),
                        text=str(it.get("text", "")),
                        done=bool(it.get("done")),
                    )
                )
    return out


# --------------------------------------------------------------------------
# KPI OKR 운영 루프: 주간 신뢰도 체크인 + 주기말 공식 채점 + 회고 카드 (0099)
# --------------------------------------------------------------------------
KPI_GRADABLE_LEVELS = ("target", "key_result")


class KpiConfidenceIn(BaseModel):
    # 1~10 (0 없음 — "0 신뢰"는 존재하지 않고 falsiness 함정 클래스도 제거).
    confidence: int = Field(ge=1, le=10)
    # 미지정이면 오늘이 속한 주. 서버가 항상 일요일로 정규화(weekly_entries 규약).
    week_start: date | None = None
    note: str | None = None


class KpiConfidence(BaseModel):
    confidence_id: UUID
    kpi_id: UUID
    week_start: date
    confidence: int
    note: str | None = None
    author_member_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class KpiGradeIn(BaseModel):
    # 0~10 정수 — 0은 유효한 채점(완전 미달성)이다.
    grade: int = Field(ge=0, le=10)
    note: str | None = None


class KpiConfidencePoint(BaseModel):
    week_start: date
    confidence: int


class KpiRetroItem(BaseModel):
    kpi: Kpi
    section: str  # kr_confidence | month_target | quarter_kr | annual_kr
    this_week_recorded: bool = False
    confidence_series: list[KpiConfidencePoint] = Field(default_factory=list)
    # 채점 근거: 기간 창 안에서 이 KPI에 태그된 계획 항목 집계(0~10 스케일).
    linked_total: int = 0
    linked_done: int = 0
    linked_scored: int = 0
    linked_score_avg: float | None = None
    # KR 근거: 하위 타깃 채점 평균(0~10).
    child_grade_avg: float | None = None
    # 제안 점수(표시 전용, 절대 자동 저장 안 함): 측정→하위채점→항목점수 순.
    suggested_grade: int | None = None


class KpiRetroCard(BaseModel):
    mode: str  # weekly | monthly
    ref: date  # 정규화된 조회 기간 키(일요일/1일)
    # monthly: 채점 대상(조회 월의 지난달) 창 — FE 라벨용.
    review_start: date | None = None
    review_end: date | None = None
    confidence_krs: list[KpiRetroItem] = Field(default_factory=list)
    grade_targets: list[KpiRetroItem] = Field(default_factory=list)
    grade_krs: list[KpiRetroItem] = Field(default_factory=list)
    unrecorded_kr_count: int = 0
    ungraded_past_count: int = 0


_KPI_CONFIDENCE_COLS = (
    dashboard_kpi_confidence.c.confidence_id,
    dashboard_kpi_confidence.c.kpi_id,
    dashboard_kpi_confidence.c.week_start,
    dashboard_kpi_confidence.c.confidence,
    dashboard_kpi_confidence.c.note,
    dashboard_kpi_confidence.c.author_member_id,
    dashboard_kpi_confidence.c.created_at,
    dashboard_kpi_confidence.c.updated_at,
)


def _confidence_out(row) -> KpiConfidence:
    return KpiConfidence(
        confidence_id=row.confidence_id,
        kpi_id=row.kpi_id,
        week_start=row.week_start,
        confidence=row.confidence,
        note=row.note,
        author_member_id=row.author_member_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _kpi_project_or_404(s, node_id: str, kpi_id: UUID):
    row = s.execute(
        select(dashboard_kpis.c.level, dashboard_kpis.c.project_id).where(
            dashboard_kpis.c.node_id == node_id, dashboard_kpis.c.kpi_id == kpi_id
        )
    ).first()
    if row is None:
        raise LookupError("kpi not found")
    return row


def _publish_kpi_ping(node_id: str, kpi_id: UUID, project_id) -> None:
    # 주의: `type` 키 금지 — FE SSE 핸들러의 live_edit 분기가 type을 먼저 본다.
    # node_id는 SSE 제너레이터의 노드 필터에 필수.
    realtime.publish(
        {
            "kind": "kpi",
            "node_id": node_id,
            "kpi_id": str(kpi_id),
            "project_id": str(project_id) if project_id else None,
        }
    )


def upsert_kpi_confidence(
    node_id: str, kpi_id: UUID, body: KpiConfidenceIn, author_member_id: UUID | None
) -> KpiConfidence:
    """(node, kpi, 일요일 주) 당 1값 upsert — 재기록은 마지막 값이 이긴다."""
    ws = week_start_sunday(body.week_start or date.today())
    with session() as s:
        krow = _kpi_project_or_404(s, node_id, kpi_id)
        row = s.execute(
            pg_insert(dashboard_kpi_confidence)
            .values(
                confidence_id=uuid.uuid4(),
                node_id=node_id,
                kpi_id=kpi_id,
                week_start=ws,
                confidence=body.confidence,
                note=body.note,
                author_member_id=author_member_id,
            )
            .on_conflict_do_update(
                index_elements=["node_id", "kpi_id", "week_start"],
                set_={
                    "confidence": body.confidence,
                    "note": body.note,
                    "author_member_id": author_member_id,
                    "updated_at": func.now(),
                },
            )
            .returning(*_KPI_CONFIDENCE_COLS)
        ).one()
        s.commit()
    _publish_kpi_ping(node_id, kpi_id, krow.project_id)
    return _confidence_out(row)


def list_kpi_confidence(node_id: str, fiscal_year: int) -> list[KpiConfidence]:
    """연도 배치 조회(스파크라인용) — KPI별 N+1 회피."""
    with session() as s:
        rows = s.execute(
            select(*_KPI_CONFIDENCE_COLS)
            .select_from(
                dashboard_kpi_confidence.join(
                    dashboard_kpis,
                    dashboard_kpi_confidence.c.kpi_id == dashboard_kpis.c.kpi_id,
                )
            )
            .where(
                dashboard_kpi_confidence.c.node_id == node_id,
                dashboard_kpis.c.fiscal_year == fiscal_year,
            )
            .order_by(
                dashboard_kpi_confidence.c.kpi_id.asc(),
                dashboard_kpi_confidence.c.week_start.asc(),
            )
        ).all()
    return [_confidence_out(r) for r in rows]


def set_kpi_grade(node_id: str, kpi_id: UUID, body: KpiGradeIn, graded_by: UUID | None) -> Kpi:
    """주기말 공식 채점. target/key_result만 허용, 재채점=덮어쓰기."""
    with session() as s:
        krow = _kpi_project_or_404(s, node_id, kpi_id)
        if krow.level not in KPI_GRADABLE_LEVELS:
            raise ValueError("only target/key_result can be graded")
        row = s.execute(
            dashboard_kpis.update()
            .where(
                dashboard_kpis.c.node_id == node_id,
                dashboard_kpis.c.kpi_id == kpi_id,
            )
            .values(
                grade=body.grade,
                grade_note=body.note,
                graded_at=func.now(),
                graded_by=graded_by,
                updated_at=func.now(),
            )
            .returning(*_KPI_COLS)
        ).one()
        s.commit()
    _publish_kpi_ping(node_id, kpi_id, krow.project_id)
    return _kpi_out(row)


def clear_kpi_grade(node_id: str, kpi_id: UUID) -> Kpi:
    """채점 말소(미채점 복귀) — 파괴적이라 라우트에서 operator 게이트."""
    with session() as s:
        krow = _kpi_project_or_404(s, node_id, kpi_id)
        row = s.execute(
            dashboard_kpis.update()
            .where(
                dashboard_kpis.c.node_id == node_id,
                dashboard_kpis.c.kpi_id == kpi_id,
            )
            .values(
                grade=None,
                grade_note=None,
                graded_at=None,
                graded_by=None,
                updated_at=func.now(),
            )
            .returning(*_KPI_COLS)
        ).one()
        s.commit()
    _publish_kpi_ping(node_id, kpi_id, krow.project_id)
    return _kpi_out(row)


def _linked_item_evidence(
    s, node_id: str, kpi_id: UUID, start: date, end: date
) -> tuple[int, int, int, float | None]:
    """기간 창 안 weekly+monthly 계획 항목 중 이 KPI에 태그된 것들의 집계.

    반환: (linked_total, linked_done, linked_scored, linked_score_avg 0~10).
    score=0 유효 — is not None으로만 센다."""
    kid = str(kpi_id)
    total = done = 0
    scores: list[float] = []

    def _scan(items) -> None:
        nonlocal total, done
        for it in items or []:
            if not isinstance(it, dict) or it.get("kpi_id") != kid:
                continue
            total += 1
            if it.get("done"):
                done += 1
            if it.get("score") is not None:
                scores.append(float(it["score"]))

    wrows = s.execute(
        select(weekly_entries.c.plan_items).where(
            weekly_entries.c.node_id == node_id,
            weekly_entries.c.week_start >= start,
            weekly_entries.c.week_start <= end,
        )
    ).all()
    for r in wrows:
        _scan(r.plan_items)
    mrows = s.execute(
        select(monthly_entries.c.plan_items).where(
            monthly_entries.c.node_id == node_id,
            monthly_entries.c.month >= start,
            monthly_entries.c.month <= end,
        )
    ).all()
    for r in mrows:
        _scan(r.plan_items)
    avg = round(sum(scores) / len(scores), 2) if scores else None
    return total, done, len(scores), avg


def _suggested_grade(
    progress: float | None, child_grade_avg: float | None, linked_score_avg: float | None
) -> int | None:
    # progress=0.0도 유효한 근거 — truthiness 금지.
    if progress is not None:
        return round(progress * 10)
    if child_grade_avg is not None:
        return round(child_grade_avg)
    if linked_score_avg is not None:
        return round(linked_score_avg)
    return None


def _prev_month(ref: date) -> tuple[int, int]:
    """조회 월의 지난달 (연, 월) — 1월 조회는 작년 12월(연도 경계)."""
    if ref.month == 1:
        return ref.year - 1, 12
    return ref.year, ref.month - 1


def _project_scope_clause(project_id: UUID | None):
    # 단일 프로젝트 조회는 그 프로젝트 + 회사 공통(NULL) KPI를 함께 본다.
    if project_id is None:
        return None
    return or_(
        dashboard_kpis.c.project_id == project_id,
        dashboard_kpis.c.project_id.is_(None),
    )


def kpi_retro_card(
    node_id: str, mode: str, ref: date, project_id: UUID | None = None
) -> KpiRetroCard:
    """계획·회고 화면의 KPI 회고 카드 데이터.

    - weekly: 조회 주가 속한 분기의 진행 중 KR 신뢰도 섹션.
    - monthly: 조회 월의 지난달(=기존 '계획 점검(지난 달)' 의미론과 동일) 타깃
      채점 섹션 + 지난달이 3/6/9/12월이면 그 분기 KR 채점 섹션(12월이면 연간
      KR도 포함).
    """
    if mode not in ("weekly", "monthly"):
        raise ValueError("invalid mode")

    with session() as s:
        if mode == "weekly":
            ws = week_start_sunday(ref)
            y = ws.year
            q = (ws.month - 1) // 3 + 1
            conds = [
                dashboard_kpis.c.node_id == node_id,
                dashboard_kpis.c.level == "key_result",
                dashboard_kpis.c.fiscal_year == y,
                dashboard_kpis.c.status.not_in(("done", "archived")),
                or_(
                    and_(
                        dashboard_kpis.c.cadence == "quarterly",
                        dashboard_kpis.c.quarter == q,
                    ),
                    dashboard_kpis.c.cadence == "annual",
                ),
            ]
            scope = _project_scope_clause(project_id)
            if scope is not None:
                conds.append(scope)
            rows = s.execute(
                select(*_KPI_COLS)
                .where(*conds)
                .order_by(dashboard_kpis.c.sort_order, dashboard_kpis.c.title)
            ).all()
            krs = [_kpi_out(r) for r in rows]
            series: dict[UUID, list[KpiConfidencePoint]] = {}
            if krs:
                crow = s.execute(
                    select(
                        dashboard_kpi_confidence.c.kpi_id,
                        dashboard_kpi_confidence.c.week_start,
                        dashboard_kpi_confidence.c.confidence,
                    )
                    .where(
                        dashboard_kpi_confidence.c.node_id == node_id,
                        dashboard_kpi_confidence.c.kpi_id.in_([k.kpi_id for k in krs]),
                    )
                    .order_by(dashboard_kpi_confidence.c.week_start.asc())
                ).all()
                for r in crow:
                    series.setdefault(r.kpi_id, []).append(
                        KpiConfidencePoint(week_start=r.week_start, confidence=r.confidence)
                    )
            items = []
            unrecorded = 0
            for k in krs:
                pts = series.get(k.kpi_id, [])
                recorded = any(p.week_start == ws for p in pts)
                if not recorded:
                    unrecorded += 1
                items.append(
                    KpiRetroItem(
                        kpi=k,
                        section="kr_confidence",
                        this_week_recorded=recorded,
                        confidence_series=pts,
                    )
                )
            return KpiRetroCard(
                mode=mode,
                ref=ws,
                confidence_krs=items,
                unrecorded_kr_count=unrecorded,
            )

        # monthly — 채점 대상은 조회 월의 지난달.
        mf = month_first(ref)
        ry, rm = _prev_month(mf)
        rstart, rend = _month_bounds(ry, rm)

        def _grade_items(
            level_conds, section: str, window: tuple[date, date]
        ) -> list[KpiRetroItem]:
            conds = [dashboard_kpis.c.node_id == node_id, *level_conds]
            scope = _project_scope_clause(project_id)
            if scope is not None:
                conds.append(scope)
            rows = s.execute(
                select(*_KPI_COLS)
                .where(*conds)
                .order_by(dashboard_kpis.c.sort_order, dashboard_kpis.c.title)
            ).all()
            out: list[KpiRetroItem] = []
            for r in rows:
                k = _kpi_out(r)
                total, done, scored, savg = _linked_item_evidence(
                    s, node_id, k.kpi_id, window[0], window[1]
                )
                child_avg = None
                if k.level == "key_result":
                    grades = s.execute(
                        select(dashboard_kpis.c.grade).where(
                            dashboard_kpis.c.node_id == node_id,
                            dashboard_kpis.c.parent_id == k.kpi_id,
                            dashboard_kpis.c.grade.is_not(None),
                        )
                    ).all()
                    if grades:
                        child_avg = round(sum(g.grade for g in grades) / len(grades), 2)
                out.append(
                    KpiRetroItem(
                        kpi=k,
                        section=section,
                        linked_total=total,
                        linked_done=done,
                        linked_scored=scored,
                        linked_score_avg=savg,
                        child_grade_avg=child_avg,
                        suggested_grade=_suggested_grade(k.progress, child_avg, savg),
                    )
                )
            return out

        targets = _grade_items(
            [
                dashboard_kpis.c.level == "target",
                dashboard_kpis.c.cadence == "monthly",
                dashboard_kpis.c.fiscal_year == ry,
                dashboard_kpis.c.month == rm,
            ],
            "month_target",
            (rstart, rend),
        )
        krs_out: list[KpiRetroItem] = []
        if rm in (3, 6, 9, 12):
            rq = rm // 3
            qstart, qend = _quarter_bounds(ry, rq)
            krs_out = _grade_items(
                [
                    dashboard_kpis.c.level == "key_result",
                    dashboard_kpis.c.cadence == "quarterly",
                    dashboard_kpis.c.fiscal_year == ry,
                    dashboard_kpis.c.quarter == rq,
                ],
                "quarter_kr",
                (qstart, qend),
            )
            if rm == 12:
                krs_out += _grade_items(
                    [
                        dashboard_kpis.c.level == "key_result",
                        dashboard_kpis.c.cadence == "annual",
                        dashboard_kpis.c.fiscal_year == ry,
                    ],
                    "annual_kr",
                    (date(ry, 1, 1), date(ry, 12, 31)),
                )

        # 미채점 과거 넛지: 채점 대상 연도(ry)에서 지난달 이전의 미채점 타깃/KR 수.
        ungraded_conds = [
            dashboard_kpis.c.node_id == node_id,
            dashboard_kpis.c.fiscal_year == ry,
            dashboard_kpis.c.grade.is_(None),
            or_(
                and_(
                    dashboard_kpis.c.level == "target",
                    dashboard_kpis.c.cadence == "monthly",
                    dashboard_kpis.c.month < rm,
                ),
                and_(
                    dashboard_kpis.c.level == "key_result",
                    dashboard_kpis.c.cadence == "quarterly",
                    dashboard_kpis.c.quarter < (rm // 3 + (1 if rm % 3 else 0)),
                ),
            ),
        ]
        scope = _project_scope_clause(project_id)
        if scope is not None:
            ungraded_conds.append(scope)
        ungraded = s.execute(
            select(func.count()).select_from(dashboard_kpis).where(*ungraded_conds)
        ).scalar_one()

        return KpiRetroCard(
            mode=mode,
            ref=mf,
            review_start=rstart,
            review_end=rend,
            grade_targets=targets,
            grade_krs=krs_out,
            ungraded_past_count=int(ungraded),
        )
