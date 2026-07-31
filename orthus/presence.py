"""실시간 협업 프레즌스(누가 어디를 작성/보고 있는지).

단일 프로세스 dev API용 in-memory 저장소. 클라이언트가 주기적으로 heartbeat를
보내면 본인 프레즌스를 갱신하고 만료(>TTL)된 것을 정리한 뒤 현재 활성 목록을
돌려준다. 노션처럼 "○○님 작성 중"을 보여주는 데 쓴다. 멀티워커/멀티노드로
확장 시 Redis 등 공유 저장소로 교체한다.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel

_TTL = timedelta(seconds=12)
_lock = threading.Lock()
# keyed by str(user_id)
_store: dict[str, dict] = {}


class PresenceHeartbeat(BaseModel):
    # 작업 영역(예: "plans") + 세부 위치(project_id/section)
    area: str
    project_id: str | None = None
    section: str | None = None


class Presence(BaseModel):
    user_id: str
    name: str
    color: str | None = None
    area: str
    project_id: str | None = None
    section: str | None = None


def heartbeat(user_id: UUID, name: str, color: str | None, hb: PresenceHeartbeat) -> list[Presence]:
    now = datetime.now(UTC)
    key = str(user_id)
    with _lock:
        _store[key] = {
            "name": name,
            "color": color,
            "area": hb.area,
            "project_id": hb.project_id,
            "section": hb.section,
            "ts": now,
        }
        out: list[Presence] = []
        for uid, p in list(_store.items()):
            if now - p["ts"] > _TTL:
                del _store[uid]
                continue
            out.append(
                Presence(
                    user_id=uid,
                    name=p["name"],
                    color=p["color"],
                    area=p["area"],
                    project_id=p["project_id"],
                    section=p["section"],
                )
            )
    return out
