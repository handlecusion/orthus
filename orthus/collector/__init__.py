"""P8.2 central ingestion API + command queue for the thin personal collector.

The collector daemon authenticates with an owner-scoped `dct_` bearer token
(hash stored in `collector_tokens`) and pushes owner-scoped document batches plus
polls/reports a per-owner command queue. This surface is fail-closed: it only
exists on a company-kind node with `collector_api_enabled=True`. Collector tokens
authenticate the collector endpoints only; they never grant a browser session,
and browser sessions never authenticate the token-only endpoints.

P8.7a adds per-token scopes (`ALLOWED_SCOPES`). `require_scope` gates a route on
one scope; `collector_token_auth` is the underlying enable-gate + bearer-auth
dependency it wraps.
"""

from orthus.collector.auth import (
    ALLOWED_SCOPES,
    CollectorAuthError,
    CollectorDisabledError,
    CollectorToken,
    assert_collector_api_enabled,
    authenticate_collector_token,
    collector_token_auth,
    effective_scopes,
    require_scope,
)
from orthus.collector.commands import (
    claim_command,
    complete_command,
    create_command,
    list_commands,
    list_pending_commands,
)
from orthus.collector.compile import (
    CompileResult,
    compile_personal_documents,
    personal_compile_has_pending_work,
)
from orthus.collector.ingest import ingest_documents

__all__ = [
    "ALLOWED_SCOPES",
    "CollectorAuthError",
    "CollectorDisabledError",
    "CollectorToken",
    "CompileResult",
    "assert_collector_api_enabled",
    "authenticate_collector_token",
    "claim_command",
    "collector_token_auth",
    "compile_personal_documents",
    "complete_command",
    "create_command",
    "effective_scopes",
    "ingest_documents",
    "list_commands",
    "list_pending_commands",
    "personal_compile_has_pending_work",
    "require_scope",
]
