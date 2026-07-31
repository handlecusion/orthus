"""Agent-chat conversation session storage (Slice A, storage only).

DB-backed, restart-safe, strictly owner-scoped Agent-chat threads. Every read
and write filters on (owner_id, node_id) so one user can never read or append to
another user's session. Pure storage: no policy gate, no LLM calls, no central
write paths. A caller turns a `None` return into a 404.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select

from orthus.db import session
from orthus.schemas.canonical import (
    AgentChatMessage,
    AgentChatSession,
    AgentChatSessionDetail,
    ChatTurn,
)
from orthus.tables import agent_chat_messages, agent_chat_sessions

_PREVIEW_MAX = 120
_TITLE_MAX = 60

_CHAT_HISTORY_TURN_LIMIT = 6
_CHAT_HISTORY_TEXT_MAX = 500


def create_session(owner_id: UUID, node_id: str, title: str = "") -> AgentChatSession:
    """Create an empty owner-scoped chat session."""
    session_id = uuid4()
    now = datetime.now(UTC)
    with session() as s:
        s.execute(
            agent_chat_sessions.insert().values(
                session_id=session_id,
                node_id=node_id,
                owner_id=owner_id,
                title=title or "",
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()
    return AgentChatSession(
        session_id=str(session_id),
        title=title or "",
        message_count=0,
        last_message_preview=None,
        last_message_at=None,
        created_at=now,
        updated_at=now,
    )


def list_sessions(owner_id: UUID, node_id: str) -> list[AgentChatSession]:
    """List the owner's sessions on this node, newest `updated_at` first.

    Each row carries an aggregated message_count, last_message_preview (first
    120 chars of the newest message), and last_message_at via correlated
    subqueries over the owner's own messages.
    """
    msgs = agent_chat_messages
    msg_count = (
        select(func.count())
        .where(msgs.c.session_id == agent_chat_sessions.c.session_id)
        .scalar_subquery()
    )
    last_at = (
        select(func.max(msgs.c.created_at))
        .where(msgs.c.session_id == agent_chat_sessions.c.session_id)
        .scalar_subquery()
    )
    with session() as s:
        rows = s.execute(
            select(
                agent_chat_sessions.c.session_id,
                agent_chat_sessions.c.title,
                agent_chat_sessions.c.created_at,
                agent_chat_sessions.c.updated_at,
                msg_count.label("message_count"),
                last_at.label("last_message_at"),
            )
            .where(
                agent_chat_sessions.c.owner_id == owner_id,
                agent_chat_sessions.c.node_id == node_id,
            )
            .order_by(agent_chat_sessions.c.updated_at.desc())
        ).all()
        previews = _previews_for(s, [row.session_id for row in rows])
    return [
        AgentChatSession(
            session_id=str(row.session_id),
            title=row.title,
            message_count=int(row.message_count or 0),
            last_message_preview=previews.get(row.session_id),
            last_message_at=row.last_message_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


def get_session(owner_id: UUID, node_id: str, session_id: UUID) -> AgentChatSessionDetail | None:
    """Return one owner-scoped session with its messages in created order.

    None when the session does not exist or is not the owner's — the caller
    turns that into a 404 (fail-closed, indistinguishable from not-found).
    """
    with session() as s:
        row = s.execute(
            select(agent_chat_sessions).where(
                agent_chat_sessions.c.session_id == session_id,
                agent_chat_sessions.c.owner_id == owner_id,
                agent_chat_sessions.c.node_id == node_id,
            )
        ).first()
        if row is None:
            return None
        message_rows = s.execute(
            select(agent_chat_messages)
            .where(agent_chat_messages.c.session_id == session_id)
            .order_by(agent_chat_messages.c.created_at, agent_chat_messages.c.message_id)
        ).all()
    messages = [_row_to_message(m) for m in message_rows]
    summary = AgentChatSession(
        session_id=str(row.session_id),
        title=row.title,
        message_count=len(messages),
        last_message_preview=(messages[-1].text[:_PREVIEW_MAX] if messages else None),
        last_message_at=(messages[-1].created_at if messages else None),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
    return AgentChatSessionDetail(session=summary, messages=messages)


def rename_session(
    owner_id: UUID, node_id: str, session_id: UUID, title: str
) -> AgentChatSession | None:
    """Rename an owner-scoped chat session, bumping updated_at.

    None when the session is missing or not the owner's (caller -> 404). The
    title is stripped and capped to 60 chars; an empty title after stripping is
    stored as "" (the FE falls back to the message preview for the label).
    """
    clean = (title or "").strip()[:_TITLE_MAX]
    now = datetime.now(UTC)
    with session() as s:
        row = s.execute(
            select(agent_chat_sessions.c.session_id).where(
                agent_chat_sessions.c.session_id == session_id,
                agent_chat_sessions.c.owner_id == owner_id,
                agent_chat_sessions.c.node_id == node_id,
            )
        ).first()
        if row is None:
            return None
        s.execute(
            agent_chat_sessions.update()
            .where(agent_chat_sessions.c.session_id == session_id)
            .values(title=clean, updated_at=now)
        )
        s.commit()
        return _summary_for_session(s, owner_id, node_id, session_id)


def delete_session(owner_id: UUID, node_id: str, session_id: UUID) -> bool:
    """Delete an owner-scoped chat session and all its messages.

    Returns False when the session is missing or not the owner's (caller -> 404);
    True on delete. agent_chat_messages has no FK cascade on session_id, so the
    messages are removed explicitly first.
    """
    with session() as s:
        row = s.execute(
            select(agent_chat_sessions.c.session_id).where(
                agent_chat_sessions.c.session_id == session_id,
                agent_chat_sessions.c.owner_id == owner_id,
                agent_chat_sessions.c.node_id == node_id,
            )
        ).first()
        if row is None:
            return False
        s.execute(
            agent_chat_messages.delete().where(
                agent_chat_messages.c.session_id == session_id
            )
        )
        s.execute(
            agent_chat_sessions.delete().where(
                agent_chat_sessions.c.session_id == session_id,
                agent_chat_sessions.c.owner_id == owner_id,
                agent_chat_sessions.c.node_id == node_id,
            )
        )
        s.commit()
    return True


def append_message(
    owner_id: UUID,
    node_id: str,
    session_id: UUID,
    role: str,
    text: str,
    work_id: UUID | None = None,
    kind: str = "text",
    payload: dict | None = None,
) -> AgentChatMessage | None:
    """Append a message to an owner-scoped session.

    None when the session is missing or not the owner's (caller -> 404). Bumps
    session.updated_at; if the session title is still empty and this is the
    first user message, sets the title from the message text (first 60 chars).

    `kind`/`payload` (migration 0101) carry HITL structured turns
    ("user_input_request"/"user_input_response"). Plain messages keep the
    "text"/None default so existing callers are unchanged.
    """
    message_id = uuid4()
    now = datetime.now(UTC)
    with session() as s:
        sess = s.execute(
            select(
                agent_chat_sessions.c.title,
            ).where(
                agent_chat_sessions.c.session_id == session_id,
                agent_chat_sessions.c.owner_id == owner_id,
                agent_chat_sessions.c.node_id == node_id,
            )
        ).first()
        if sess is None:
            return None
        had_user_message = (
            s.execute(
                select(func.count())
                .select_from(agent_chat_messages)
                .where(
                    agent_chat_messages.c.session_id == session_id,
                    agent_chat_messages.c.role == "user",
                )
            ).scalar_one()
            > 0
        )
        s.execute(
            agent_chat_messages.insert().values(
                message_id=message_id,
                session_id=session_id,
                role=role,
                text=text,
                work_id=work_id,
                kind=kind,
                payload=payload,
                created_at=now,
            )
        )
        values: dict = {"updated_at": now}
        if not sess.title and role == "user" and not had_user_message:
            values["title"] = text[:_TITLE_MAX]
        s.execute(
            agent_chat_sessions.update()
            .where(agent_chat_sessions.c.session_id == session_id)
            .values(**values)
        )
        s.commit()
    return AgentChatMessage(
        message_id=str(message_id),
        role=role,
        text=text,
        work_id=(str(work_id) if work_id is not None else None),
        kind=kind,
        payload=payload,
        created_at=now,
    )


def update_message_payload(
    owner_id: UUID,
    node_id: str,
    session_id: UUID,
    message_id: UUID,
    payload: dict,
) -> AgentChatMessage | None:
    """Overwrite the payload JSONB of one owner-scoped message.

    Used to flip a user_input_request from status "waiting" to "answered". Returns
    None when the session is missing/non-owner or the message is not in it (caller
    -> 404/409). Owner-scoping is enforced by first verifying the session, then
    updating only within that session_id.
    """
    with session() as s:
        sess = s.execute(
            select(agent_chat_sessions.c.session_id).where(
                agent_chat_sessions.c.session_id == session_id,
                agent_chat_sessions.c.owner_id == owner_id,
                agent_chat_sessions.c.node_id == node_id,
            )
        ).first()
        if sess is None:
            return None
        row = s.execute(
            select(agent_chat_messages).where(
                agent_chat_messages.c.message_id == message_id,
                agent_chat_messages.c.session_id == session_id,
            )
        ).first()
        if row is None:
            return None
        s.execute(
            agent_chat_messages.update()
            .where(
                agent_chat_messages.c.message_id == message_id,
                agent_chat_messages.c.session_id == session_id,
            )
            .values(payload=payload)
        )
        s.commit()
    return AgentChatMessage(
        message_id=str(row.message_id),
        role=row.role,
        text=row.text,
        work_id=(str(row.work_id) if row.work_id is not None else None),
        kind=row.kind,
        payload=payload,
        created_at=row.created_at,
    )


def latest_waiting_input_request(
    messages: list[AgentChatMessage],
) -> AgentChatMessage | None:
    """Return the most recent still-waiting user_input_request message, or None.

    Walks from the end and returns the first message with
    kind=="user_input_request" and payload.status=="waiting". A newer plain agent
    message does not block the search (the question may have been asked, then a
    status update appended); the endpoint additionally requires the answered
    message_id to equal this one, so this is the single source of "what question
    is currently open".
    """
    for m in reversed(messages):
        if m.kind == "user_input_request" and (m.payload or {}).get("status") == "waiting":
            return m
    return None


def load_chat_history(
    user_id: UUID,
    node_id: str,
    session_id: str,
    current_question: str,
) -> list[ChatTurn]:
    """Reconstruct prior turns from the owner-scoped chat session for non-authoritative
    reference context (the wiki branch resolves references against these turns; history
    never reaches retrieval or grounding).

    Strictly owner-scoped (get_session is fail-closed: a non-owner/missing session
    returns None -> empty history). The FE appends the current user message BEFORE
    calling the orchestrator, so the trailing user turn equals the current question —
    drop it so the model does not see the question twice. Cap to the last 6 turns and
    truncate each text to 500 chars.
    """
    try:
        sid = UUID(session_id)
    except (ValueError, TypeError):
        return []
    detail = get_session(user_id, node_id, sid)
    if detail is None:
        return []
    messages = list(detail.messages)
    # Drop the trailing user message equal to the current question (the FE-appended
    # user turn). Prefer an exact text match on the last user-role message; if none
    # matches, drop the last user-role message regardless.
    current = (current_question or "").strip()
    drop_index: int | None = None
    fallback_index: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role != "user":
            continue
        if fallback_index is None:
            fallback_index = i
        if messages[i].text.strip() == current:
            drop_index = i
            break
    target = drop_index if drop_index is not None else fallback_index
    if target is not None:
        messages = messages[:target] + messages[target + 1 :]
    turns = [
        ChatTurn(role=m.role, text=m.text[:_CHAT_HISTORY_TEXT_MAX])
        for m in messages[-_CHAT_HISTORY_TURN_LIMIT:]
    ]
    return turns


def _summary_for_session(
    s, owner_id: UUID, node_id: str, session_id: UUID
) -> AgentChatSession | None:
    """Owner-scoped session summary (title + aggregates) without loading every
    message. None when missing/non-owner. Used by rename_session to return the
    fresh row shape the FE list expects."""
    row = s.execute(
        select(agent_chat_sessions).where(
            agent_chat_sessions.c.session_id == session_id,
            agent_chat_sessions.c.owner_id == owner_id,
            agent_chat_sessions.c.node_id == node_id,
        )
    ).first()
    if row is None:
        return None
    msgs = agent_chat_messages
    count = s.execute(
        select(func.count())
        .select_from(msgs)
        .where(msgs.c.session_id == session_id)
    ).scalar_one()
    last = s.execute(
        select(msgs.c.text, msgs.c.created_at)
        .where(msgs.c.session_id == session_id)
        .order_by(msgs.c.created_at.desc(), msgs.c.message_id.desc())
        .limit(1)
    ).first()
    return AgentChatSession(
        session_id=str(row.session_id),
        title=row.title,
        message_count=int(count or 0),
        last_message_preview=(last.text[:_PREVIEW_MAX] if last else None),
        last_message_at=(last.created_at if last else None),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _previews_for(s, session_ids: list[UUID]) -> dict[UUID, str]:
    """Newest-message preview (first 120 chars) per session, owner already scoped
    by the session list query that produced these ids."""
    if not session_ids:
        return {}
    msgs = agent_chat_messages
    rows = s.execute(
        select(msgs.c.session_id, msgs.c.text, msgs.c.created_at, msgs.c.message_id)
        .where(msgs.c.session_id.in_(session_ids))
        .order_by(msgs.c.session_id, msgs.c.created_at.desc(), msgs.c.message_id.desc())
    ).all()
    previews: dict[UUID, str] = {}
    for row in rows:
        if row.session_id not in previews:
            previews[row.session_id] = row.text[:_PREVIEW_MAX]
    return previews


def _row_to_message(row) -> AgentChatMessage:
    return AgentChatMessage(
        message_id=str(row.message_id),
        role=row.role,
        text=row.text,
        work_id=(str(row.work_id) if row.work_id is not None else None),
        kind=getattr(row, "kind", "text") or "text",
        payload=getattr(row, "payload", None),
        created_at=row.created_at,
    )
