"""메일 명함(서명) owner-scope 저장/조회.

작성한 메일 끝에 넣는 본인 비즈니스 카드를 구조화 필드로 보관한다. (node_id,
owner_id, from_addr)당 1행 upsert. 본문 HTML 렌더는 FE가 필드에서 만들고 서버는 임의 HTML을
저장하지 않는다(저장 XSS 표면 차단). Pydantic 검증(`MailSignatureInput`)이
링크 개수/스킴/길이를 제한한다.
"""

from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import func

from orthus.db import session
from orthus.schemas.canonical import MailSignature, MailSignatureInput, MailSignatureLink
from orthus.tables import mail_signatures


def _normalize_from_addr(value: str | None) -> str:
    return (value or "").strip().lower()[:200]


def _row_to_signature(row) -> MailSignature:
    links = [
        MailSignatureLink(label=str(item.get("label", "")), url=str(item.get("url", "")))
        for item in (row.links or [])
        if isinstance(item, dict)
    ]
    return MailSignature(
        from_addr=row.from_addr or "",
        display_name=row.display_name or "",
        title=row.title or "",
        company=row.company or "",
        email=row.email or "",
        phone=row.phone or "",
        website=row.website or "",
        links=links,
        enabled=bool(row.enabled),
    )


def get_signature(*, node_id: str, owner_id: UUID, from_addr: str | None = None) -> MailSignature:
    """저장된 명함을 돌려준다.

    `from_addr`가 있으면 해당 발신 주소 전용 명함을 먼저 찾는다. 없으면 legacy/default
    명함(from_addr='')을 템플릿처럼 돌려주되 응답 from_addr/email은 요청 주소로 보정한다.
    """

    normalized = _normalize_from_addr(from_addr)
    with session() as s:
        row = s.execute(
            select(mail_signatures).where(
                mail_signatures.c.node_id == node_id,
                mail_signatures.c.owner_id == owner_id,
                mail_signatures.c.from_addr == normalized,
            )
        ).first()
        if row is None and normalized:
            row = s.execute(
                select(mail_signatures).where(
                    mail_signatures.c.node_id == node_id,
                    mail_signatures.c.owner_id == owner_id,
                    mail_signatures.c.from_addr == "",
                )
            ).first()
    if row is None:
        return MailSignature(from_addr=normalized, email=normalized)
    sig = _row_to_signature(row)
    if normalized and sig.from_addr != normalized:
        sig.from_addr = normalized
        if not sig.email:
            sig.email = normalized
    return sig


def upsert_signature(
    *, node_id: str, owner_id: UUID, payload: MailSignatureInput, from_addr: str | None = None
) -> MailSignature:
    """owner 명함을 저장/갱신하고 저장된 값을 돌려준다."""

    normalized = _normalize_from_addr(from_addr or payload.from_addr)
    links_json = [link.model_dump() for link in payload.links]
    with session() as s:
        stmt = (
            pg_insert(mail_signatures)
            .values(
                signature_id=uuid.uuid4(),
                node_id=node_id,
                owner_id=owner_id,
                from_addr=normalized,
                display_name=payload.display_name,
                title=payload.title,
                company=payload.company,
                email=payload.email,
                phone=payload.phone,
                website=payload.website,
                links=links_json,
                enabled=payload.enabled,
            )
            .on_conflict_do_update(
                index_elements=["node_id", "owner_id", "from_addr"],
                set_={
                    "from_addr": normalized,
                    "display_name": payload.display_name,
                    "title": payload.title,
                    "company": payload.company,
                    "email": payload.email,
                    "phone": payload.phone,
                    "website": payload.website,
                    "links": links_json,
                    "enabled": payload.enabled,
                    "updated_at": func.now(),
                },
            )
            .returning(mail_signatures)
        )
        row = s.execute(stmt).one()
        s.commit()
    return _row_to_signature(row)
