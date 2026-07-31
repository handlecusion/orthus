from __future__ import annotations

from datetime import datetime
from urllib.parse import quote
from uuid import UUID

import httpx

from orthus.schemas.canonical import AssistantResult, WikiPage, WikiSourceRef
from orthus.settings import Settings, get_settings


class CentralReadUnavailable(RuntimeError):
    """Central read endpoint is missing, disabled, or unreachable."""


def central_read_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.central_read_base_url.strip() and settings.central_read_token.strip())


def retrieve_company_wiki(
    user_id: UUID,
    question: str,
    *,
    k: int = 5,
    project: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    source: str | None = None,
    settings: Settings | None = None,
) -> list[WikiSourceRef]:
    _ = user_id  # central company reads are service-authenticated, not per-user.
    settings = settings or get_settings()
    data = _post(
        settings,
        "/federation/wiki/retrieve",
        {
            "question": question,
            "k": k,
            "project": project,
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "source": source,
        },
    )
    return [WikiSourceRef.model_validate(item) for item in data]


def query_company_structured(
    user_id: UUID,
    question: str,
    *,
    project: str | None = None,
    settings: Settings | None = None,
) -> AssistantResult:
    _ = user_id
    settings = settings or get_settings()
    data = _post(
        settings,
        "/federation/structured/query",
        {"question": question, "project": project},
    )
    return AssistantResult.model_validate(data)


def get_company_wiki_page(
    slug: str,
    *,
    settings: Settings | None = None,
) -> WikiPage:
    settings = settings or get_settings()
    data = _get(settings, f"/federation/wiki/pages/{_encode_path_slug(slug)}")
    return WikiPage.model_validate(data)


def _post(settings: Settings, path: str, payload: dict) -> object:
    if not central_read_configured(settings):
        raise CentralReadUnavailable("central read not configured")

    base_url = settings.central_read_base_url.rstrip("/")
    timeout = max(settings.central_read_timeout_ms / 1000, 0.1)
    headers = {"Authorization": f"Bearer {settings.central_read_token}"}
    try:
        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            response = client.post(path, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise CentralReadUnavailable(f"central read failed: {type(exc).__name__}") from exc


def _get(settings: Settings, path: str) -> object:
    if not central_read_configured(settings):
        raise CentralReadUnavailable("central read not configured")

    base_url = settings.central_read_base_url.rstrip("/")
    timeout = max(settings.central_read_timeout_ms / 1000, 0.1)
    headers = {"Authorization": f"Bearer {settings.central_read_token}"}
    try:
        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            response = client.get(path, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise CentralReadUnavailable(f"central read failed: {type(exc).__name__}") from exc


def _encode_path_slug(slug: str) -> str:
    return "/".join(quote(part, safe="") for part in slug.split("/"))
