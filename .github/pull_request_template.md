# PR Checklist

## Risk

- [ ] Fast: docs, tests, copy, or small UI polish only. Post-merge audit ok.
- [ ] Normal: feature or bugfix, no protected area touched.
- [ ] Protected: pre-merge owner review required.

## Protected Area

Check every area this PR touches.

- [ ] Auth, session, SSO, or invite allowlist
- [ ] Owner-scope, privacy, permission, or cross-node boundary
- [ ] Mail send, ingest, external write, or rate limit
- [ ] Connector secrets, tokens, or account ownership
- [ ] DB migration, destructive cleanup, deletion, or backfill
- [ ] KG projection, outbox, entity layer, or graph retrieval boundary
- [ ] Infra, deploy, or release workflow
- [ ] Audit log, security guard, or policy gate

If any protected area is checked, do not self-merge before owner review.

## QA Evidence

What changed:


How tested:


Commands run:


Screenshots or browser QA, if UI:


Known risk / rollback:


## Post-Merge Audit

Reviewer checks:

- [ ] Risk classification matches diff
- [ ] QA evidence is enough for change size
- [ ] Protected area was not missed
- [ ] Regression risk is acceptable
- [ ] Follow-up issue or revert is needed
