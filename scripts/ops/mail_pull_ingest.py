#!/usr/bin/env python
"""P6.2-pull operator trigger: orthus-side mail pull-ingest.

Runs `pull_ingest_all` for the configured company-mail backends (nova +
acme). This is an operator/launchd job, not an HTTP route. It is a no-op
unless `ORTHUS_MAIL_PULL_INGEST_ENABLED=true` on a company-kind node; in that
case each per-backend result reports enabled=False with zero counts. Prints a
per-backend {listed, ingested, skipped, errors} summary.
"""

from __future__ import annotations

import sys

from orthus.mail.pull import pull_ingest_all
from orthus.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    if not settings.mail_pull_ingest_enabled:
        print("mail pull-ingest disabled (set ORTHUS_MAIL_PULL_INGEST_ENABLED=true)")
        return 0
    if settings.node_kind != "company":
        print(f"mail pull-ingest is company-node only (node_kind={settings.node_kind})")
        return 0

    results = pull_ingest_all(settings)
    if not results:
        print("no company mail backends configured")
        return 0
    for result in results:
        print(
            f"backend={result.backend} enabled={result.enabled} "
            f"listed={result.listed} ingested={result.ingested} "
            f"skipped={result.skipped} errors={result.errors}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
