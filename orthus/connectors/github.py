"""GitHub issue/PR connector.

Fetches issues and pull requests from configured repositories via the official
GitHub REST API and normalizes each item into an InternalDocument.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

import httpx

from orthus.audit.redact import redact_pii_text
from orthus.connectors.base import Connector
from orthus.schemas.canonical import InternalDocument

DEFAULT_GITHUB_MAX_ITEMS = 200

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|authorization|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
]


def parse_github_repos(value: str) -> tuple[str, ...]:
    """Parse comma-separated GitHub repo slugs as owner/name."""
    repos: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        item = raw.strip().strip("/")
        if not item:
            continue
        if item.startswith("https://github.com/"):
            item = item.removeprefix("https://github.com/").strip("/")
        parts = item.split("/")
        if len(parts) < 2:
            continue
        slug = f"{parts[0]}/{parts[1]}".lower()
        if slug in seen:
            continue
        seen.add(slug)
        repos.append(slug)
    return tuple(repos)


class GitHubConnector(Connector):
    """Connector over GitHub repository issues and pull requests."""

    def __init__(
        self,
        token: str,
        repos: Iterable[str],
        *,
        client: httpx.Client | None = None,
        max_items: int = DEFAULT_GITHUB_MAX_ITEMS,
    ) -> None:
        if not token.strip():
            raise ValueError("GitHub token required")
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        self.repos = tuple(parse_github_repos(",".join(repos)))
        if not self.repos:
            raise ValueError("at least one GitHub repo required")
        self.max_items = max_items
        self._client = client or httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        for repo in self.repos:
            yielded = 0
            for item in self._iter_repo_items(repo):
                updated_at = _parse_time(item.get("updated_at"))
                if since is not None and updated_at is not None and updated_at <= since:
                    continue
                yield _document_from_item(repo, item, updated_at)
                yielded += 1
                if yielded >= self.max_items:
                    break

    def _iter_repo_items(self, repo: str) -> Iterator[dict[str, Any]]:
        page = 1
        yielded = 0
        while yielded < self.max_items:
            resp = self._client.get(
                f"/repos/{repo}/issues",
                params={
                    "state": "all",
                    "per_page": min(100, self.max_items - yielded),
                    "sort": "updated",
                    "direction": "desc",
                    "page": page,
                },
            )
            resp.raise_for_status()
            items = resp.json()
            if not isinstance(items, list) or not items:
                return
            for item in items:
                if isinstance(item, dict):
                    yielded += 1
                    yield item
            if len(items) < 100:
                return
            page += 1


def _document_from_item(
    repo: str,
    item: dict[str, Any],
    updated_at: datetime | None,
) -> InternalDocument:
    number = item.get("number")
    kind = "pull_request" if isinstance(item.get("pull_request"), dict) else "issue"
    title = _redact_text(str(item.get("title") or f"{repo} #{number}"))[:180]
    body = _redact_text(str(item.get("body") or "")).strip()
    labels = [
        str(label.get("name"))
        for label in item.get("labels") or []
        if isinstance(label, dict) and label.get("name")
    ]
    author = item.get("user") or {}
    author_login = str(author.get("login") or "") if isinstance(author, dict) else ""
    url = str(item.get("html_url") or "")
    state = str(item.get("state") or "")

    markdown = "\n".join(
        [
            f"# {title}",
            "",
            "Source: github",
            f"Repository: {repo}",
            f"Kind: {kind}",
            f"Number: {number}",
            f"State: {state}",
            f"URL: {url}",
            f"Author: {author_login}",
            f"Labels: {', '.join(labels)}",
            "",
            "## Body",
            "",
            body or "(empty)",
            "",
        ]
    )
    return InternalDocument(
        title=f"GitHub {repo} #{number}: {title}",
        markdown=markdown,
        source="github",
        source_external_id=f"github:{repo}:{kind}:{number}",
        source_last_edited_at=updated_at,
        project="company",
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _redact_text(text: str) -> str:
    out = text.replace("\x00", " ")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED_SECRET]", out)
    return redact_pii_text(re.sub(r"\s+", " ", out).strip())
