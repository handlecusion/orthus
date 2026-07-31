"""P6 unified mail client substrate."""

from orthus.mail.backends import (
    fetch_backend_conversation,
    fetch_backend_email_detail,
    list_unified_inbox,
    mutate_backend_email,
    normalize_backend_email,
    resolve_backend_config,
    trash_backend_email,
)
from orthus.mail.ingest import (
    MailIngestConfigError,
    ingest_scope_and_owner,
    ingest_scope_for_backend,
)
from orthus.mail.send import (
    MailSendRoutingError,
    MailSendUnconfiguredError,
    backend_for_from_addr,
    backend_send_configured,
    send_mail,
)

__all__ = [
    "MailIngestConfigError",
    "ingest_scope_and_owner",
    "ingest_scope_for_backend",
    "fetch_backend_conversation",
    "fetch_backend_email_detail",
    "list_unified_inbox",
    "mutate_backend_email",
    "normalize_backend_email",
    "resolve_backend_config",
    "trash_backend_email",
    "MailSendRoutingError",
    "MailSendUnconfiguredError",
    "backend_for_from_addr",
    "backend_send_configured",
    "send_mail",
]
