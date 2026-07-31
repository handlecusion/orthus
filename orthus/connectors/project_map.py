"""Deterministic project resolution for ingested content (P2,
docs/p2-fe-and-sources.md).

Hierarchy: company (acme) → project (atlas | nova | orbit | company) →
content. The broad Notion workspace still defaults to `atlas` for operational
DBs, but explicit DB-name hints split out Nova, Orbit, and company-wide
knowledge such as team directory/reference docs.

The mapping is intentionally an explicit, well-commented constant so reassigning a
source to a different project is a one-line edit (no DB migration, just reseed).
"""

from __future__ import annotations

# The Notion enum of valid projects (mirrors the CHECK in migration 0006).
PROJECTS = ("atlas", "nova", "orbit", "company")

# Workspace default: the token's workspace mostly contains atlas operational
# data. Generic company knowledge is split out below by explicit DB names.
_WORKSPACE_DEFAULT = "atlas"

# Top-level "Nova" Notion page id. A source whose parent chain reaches this page
# belongs to the `nova` project.
NOVA_PARENT_PAGE_ID = "00000000-0000-4000-8000-00000000a11a"

# db_name prefixes/substrings that mark a Notion DB as belonging to `nova`.
# Editable: add prefixes here to reassign databases without touching logic below.
_NOVA_DB_PREFIXES = ("Nova",)
_NOVA_DB_SUBSTRINGS = ("NOVA",)

# Orbit is not a separate Notion token yet, so resolve by explicit DB/name hints
# when those surfaces appear in the company workspace.
_ORBIT_DB_PREFIXES = ("Orbit", "ORBIT")
_ORBIT_DB_SUBSTRINGS = (
    "orbit",
    "ORBIT",
    "플레이스",
    "매물",
    "부동산",
    "원룸백과",
    "홈즈데모",
)

# Company-wide / non-project databases. These are neither atlas operations nor
# product-specific delivery work.
_COMPANY_DB_NAMES = {
    "팀원",
    "참고 문서",
    "AI관련툴",
}
_COMPANY_DB_SUBSTRINGS = (
    "회사 미팅",
    "벤치마킹 회사",
)


def resolve_project(*, db_name: str | None, parent_chain: list[str] | None = None) -> str:
    """Resolve the project for a piece of ingested content.

    Rule (deterministic):
      - `nova` if the Notion db_name starts with "Nova" or contains "NOVA", OR
        the parent chain reaches the top-level Nova page id.
      - `orbit` for explicit Orbit/real-estate DB name hints.
      - `company` for company-wide databases such as team directory/reference docs.
      - otherwise `atlas` (the workspace default).
    """
    if db_name:
        if any(db_name.startswith(p) for p in _NOVA_DB_PREFIXES):
            return "nova"
        if any(sub in db_name for sub in _NOVA_DB_SUBSTRINGS):
            return "nova"
        normalized = db_name.strip()
        if any(normalized.startswith(p) for p in _ORBIT_DB_PREFIXES):
            return "orbit"
        if any(sub in normalized for sub in _ORBIT_DB_SUBSTRINGS):
            return "orbit"
        if normalized in _COMPANY_DB_NAMES:
            return "company"
        if any(sub in normalized for sub in _COMPANY_DB_SUBSTRINGS):
            return "company"

    if parent_chain and NOVA_PARENT_PAGE_ID in parent_chain:
        return "nova"

    return _WORKSPACE_DEFAULT
