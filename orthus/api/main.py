"""FastAPI app for the 아카식(archive) + 비서(secretary)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from orthus.api.routes import (
    agent_gateway,
    agent_work,
    ask,
    auth,
    board,
    collector,
    connectors,
    corpus,
    dashboard,
    documents,
    federation,
    gaps,
    kg,
    mail,
    personal_board,
    projects,
    promote,
    wiki,
)
from orthus.agentwork import stream as agent_stream
from orthus.collector.ws_registry import WS_REGISTRY
from orthus.kg.client import verify_owner_scope_gate_consistency
from orthus.kg.outbox import start_outbox_worker_thread
from orthus.router.event_orchestration import start_event_orchestration_worker_thread
from orthus.settings import get_settings, validate_runtime_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_runtime_settings(settings)
    # K7.2 L3 — owner-scope flag가 켜졌는데 게이트가 owner-variant가 아니면 KG를 강제
    # off(보팅은 정상, 모든 KG 경로 supported:false). 반드시 worker 기동 전에 호출 —
    # 강제-off면 kg_enabled()=False라 outbox worker도 함께 뜨지 않는다(read+write 동시 차단).
    verify_owner_scope_gate_consistency()
    # Slice 8: capture the running loop so create_command (which runs in a
    # threadpool) can hand a "command available" notify to a connected collector
    # daemon's WebSocket via loop.call_soon_threadsafe.
    WS_REGISTRY.set_loop(asyncio.get_running_loop())
    # Inline agentic /ask runs in the sync-endpoint threadpool; capture the loop so
    # its live frames can be marshalled to the per-request SSE via publish_threadsafe.
    agent_stream.set_loop(asyncio.get_running_loop())
    # K3 KG outbox worker — kg_enabled + company node일 때만 기동(None 반환이
    # 기동 조건 가드, kg-implementation-spec §5.3). uvicorn reload의 감시용
    # 부모 프로세스는 lifespan을 실행하지 않아 worker가 뜨지 않는다(§12).
    worker_handle = start_outbox_worker_thread()
    # Phase 3-B MA.8a — event-triggered decompose orchestration worker. Starts only
    # when ORTHUS_ASK_EVENT_ORCH_ENABLED + company node (None otherwise → no thread,
    # prod 동작 불변, 불변식 32).
    event_orch_handle = start_event_orchestration_worker_thread()
    yield
    WS_REGISTRY.clear_loop()
    agent_stream.clear_loop()
    for handle in (worker_handle, event_orch_handle):
        if handle is not None:
            thread, stop = handle
            stop.set()
            thread.join(timeout=10)


app = FastAPI(title="Orthus — 아카식 + 비서", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(corpus.router)
app.include_router(connectors.router)
app.include_router(agent_work.router)
app.include_router(agent_gateway.router)
app.include_router(wiki.router)
app.include_router(ask.router)
app.include_router(gaps.router)
app.include_router(mail.router)
app.include_router(federation.router)
app.include_router(projects.router)
app.include_router(board.router)
app.include_router(collector.router)
app.include_router(personal_board.router)
app.include_router(promote.router)
app.include_router(dashboard.router)
app.include_router(kg.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
