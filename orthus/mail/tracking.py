"""메일 열람(읽음) 추적 — orthus 자체 픽셀 주입 + 공개 기록.

orthus는 보낸 메일의 HTML 본문을 직접 조립하므로, provider 협조 없이도 본문 끝에
자기 호스트의 1x1 투명 픽셀(`<img>`)을 심을 수 있다. 수신자가 메일을 열어
이미지를 로드하면 orthus 공개 엔드포인트가 호출돼 open을 1건 기록한다.

저장은 email_send_log와 동일하게 hash-only다 — recipient/subject는 sha256만
보관하고, token은 추측 불가 랜덤이다. 공개 픽셀 엔드포인트는 token만으로 open을
올릴 뿐 어떤 정보도 반환하지 않는다(1x1 GIF만). 읽음 현황 조회는 owner 세션에서
자기 token에 한해서만 가능하다.

fail-closed: 픽셀 주입/행 생성은 `mail_open_tracking_enabled` 플래그와 공개
base URL이 모두 설정된 경우에만 일어난다(기본 off).
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update

from orthus.db import session
from orthus.tables import mail_tracking

# token_urlsafe(24)는 [A-Za-z0-9_-] 32자. 공개 픽셀이 DB write를 할지 정하는 값싼
# 형태 가드 — 명백히 토큰이 아닌(스캐너/봇) 요청은 DB를 건드리지 않는다.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def is_plausible_token(token: str) -> bool:
    """공개 픽셀이 DB를 건드릴지 결정하는 값싼 형태 검사(token_urlsafe 모양만 통과)."""
    return bool(_TOKEN_RE.match(token))


# 1x1 투명 GIF (43 bytes). 공개 픽셀 엔드포인트가 이 바이트를 그대로 반환한다.
TRANSPARENT_GIF: bytes = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
    b"\x00\x00\x02\x02D\x01\x00;"
)


def make_tracking_token() -> str:
    """추측 불가 URL-safe 토큰. 공개 엔드포인트의 유일 키."""
    return secrets.token_urlsafe(24)


def pixel_url(base_url: str, token: str) -> str:
    """공개 픽셀 URL. base_url은 Caddy가 /api로 프록시하는 공개 호스트(예:
    https://orthus-central.example.com/api)."""
    return f"{base_url.rstrip('/')}/mail/track/open/{token}.gif"


def tracking_pixel_html(base_url: str, token: str) -> str:
    """본문에 심을 추적 픽셀 마크업. 보이지 않게 1x1로 둔다."""
    src = pixel_url(base_url, token)
    return (
        f'<img src="{src}" width="1" height="1" alt="" '
        'style="display:none;width:1px;height:1px" />'
    )


def inject_tracking_pixel(html: str, base_url: str, token: str) -> str:
    """HTML 본문 끝(닫는 </body> 직전, 없으면 맨 끝)에 픽셀을 추가한다."""
    pixel = tracking_pixel_html(base_url, token)
    if not html:
        return pixel
    lowered = html.lower()
    idx = lowered.rfind("</body>")
    if idx != -1:
        return html[:idx] + pixel + html[idx:]
    return html + pixel


def create_tracking(
    *,
    node_id: str,
    owner_id: UUID,
    token: str,
    recipient_hash: str,
    subject_hash: str,
    sender_kind: str,
) -> None:
    """발송 직전 추적 행을 만든다(open_count=0)."""
    with session() as s:
        s.execute(
            insert(mail_tracking).values(
                tracking_id=uuid4(),
                node_id=node_id,
                owner_id=owner_id,
                token=token,
                recipient_hash=recipient_hash,
                subject_hash=subject_hash,
                sender_kind=sender_kind,
                open_count=0,
            )
        )
        s.commit()


def record_open(token: str, now: datetime) -> bool:
    """공개 픽셀 히트. token 행이 있으면 open_count를 올리고 시각을 갱신한다.

    반환값은 token이 실재했는지 여부(엔드포인트는 어느 쪽이든 GIF를 반환한다 —
    토큰 존재 여부를 외부에 노출하지 않기 위해 응답은 동일하다)."""
    with session() as s:
        result = s.execute(
            update(mail_tracking)
            .where(mail_tracking.c.token == token)
            .values(
                open_count=mail_tracking.c.open_count + 1,
                last_opened_at=now,
                # first_opened_at은 최초 1회만 설정.
                first_opened_at=func.coalesce(mail_tracking.c.first_opened_at, now),
            )
        )
        s.commit()
        return result.rowcount > 0


@dataclass(frozen=True)
class TrackingStatus:
    token: str
    open_count: int
    first_opened_at: datetime | None
    last_opened_at: datetime | None

    @property
    def opened(self) -> bool:
        return self.open_count > 0


def get_status(token: str, *, node_id: str, owner_id: UUID) -> TrackingStatus | None:
    """owner 세션이 자기 token의 읽음 현황을 조회한다. 타 owner row는 None."""
    with session() as s:
        row = s.execute(
            select(
                mail_tracking.c.token,
                mail_tracking.c.open_count,
                mail_tracking.c.first_opened_at,
                mail_tracking.c.last_opened_at,
            ).where(
                mail_tracking.c.token == token,
                mail_tracking.c.node_id == node_id,
                mail_tracking.c.owner_id == owner_id,
            )
        ).first()
    if row is None:
        return None
    return TrackingStatus(
        token=row.token,
        open_count=row.open_count,
        first_opened_at=row.first_opened_at,
        last_opened_at=row.last_opened_at,
    )
