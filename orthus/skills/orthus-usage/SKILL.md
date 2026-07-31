---
name: orthus-usage
description: How a delegated/inline agent uses Orthus company knowledge — orthus-mcp tools and the orthus CLI. Read this when you are dispatched a Orthus task or asked a company question.
---

# Orthus usage skill

You are a Orthus company agent. Two interfaces reach the company's central
knowledge ("아카식") and Agent Work. **Prefer the orthus-mcp tools** when they are
attached (they need no shell); fall back to the `orthus` CLI for the same data and
for local collector/archive operations.

Both are READ-mostly. The only write either exposes is a wiki **update
candidate** (a review task — never live page content). Approvals, mail send,
promote, and allowlist are NOT available to you; those stay in the human operator
browser flow.

## Who am I / what can I do

Always orient first when a task depends on authority:

- MCP: `whoami` → `{user_id, email, role, node}`.
- CLI: `orthus whoami` (add `--json` for automation).

`role` is `owner` / `admin` / `member` (or null). Owner/admin may submit wiki
update candidates that land in the shared review queue; members' candidates and
content changes still need operator review.

## Company knowledge (read)

| Need | MCP tool | CLI |
|---|---|---|
| Search compiled wiki pages | `wiki_search(query, scope, limit)` | `orthus wiki search "<q>"` |
| Read one page | `wiki_page(slug)` | `orthus wiki page <slug>` |
| Grounded Q&A | `wiki_ask(question, scope)` | `orthus wiki ask "<q>"` |
| Team calendar (date range) | `team_schedule(since, until)` | `orthus calendar list [--since --until]` |
| Team members | `team_members()` | `orthus calendar members list` |
| My personal schedule | `personal_schedule_list(since, until)` | `orthus myschedule list [--since --until]` |
| Aggregate/count/list (NL→SQL, gated) | `structured(question, scope)` | — |

Rules: never guess facts — confirm with a tool. Answer in the user's language,
summarize tool output (do not dump raw). Empty result → say it is empty.
`structured` is SELECT-only behind a server validation gate; pass natural
language, never SQL.

## Schedules (write — `knowledge:write`)

Add/update the company **team calendar** and your **personal schedule** directly.
Team calendar is company-shared; personal schedule is owner-private. To attach
teammates, resolve member_ids with `team_members` first. Writes are rate limited.

| Need | MCP tool | CLI |
|---|---|---|
| Add team event | `team_schedule_add(title, event_date, ...)` | `orthus calendar add --title <t> --date YYYY-MM-DD [...]` |
| Update team event | `team_schedule_update(event_id, ...)` | `orthus calendar update <event_id> [--location ...]` |
| Delete team event | `team_schedule_delete(event_id)` | `orthus calendar delete <event_id>` |
| Add team member | `team_members_add(name, ...)` | `orthus calendar members add --name <n>` |
| Add personal event | `personal_schedule_add(title, starts_at, ends_at, ...)` | `orthus myschedule add --title <t> --start <ISO> --end <ISO>` |
| Update personal event | `personal_schedule_update(event_id, ...)` | `orthus myschedule update <event_id> [--title ...]` |

Team event dates are `YYYY-MM-DD` (+ optional `start_time`/`end_time` `HH:MM`);
personal events use ISO-8601 datetimes (e.g. `2026-07-01T14:00:00+09:00`). To
update, find the `event_id` via the matching list tool first.

## Wiki update candidate (a review-only write)

When asked to add/fix/update company wiki content, submit a **review candidate**
— do not decline, do not over-confirm:

- MCP: `wiki_update_candidate(slug, note, evidence_urls)`.
- CLI: `orthus wiki suggest <slug> --note "<제안>" [--evidence-url <url>]`.

On a company node this creates a company-scope `open_question` review task that
**owner and admin** see in `/wiki/tasks`. It is NOT an immediate publish — the
actual content change is applied later by an owner/admin at resolve time. After
submitting, report: "검토 후보로 제출했다(즉시 반영 아님)". If the target slug is
unclear, `wiki_search` first.

## Agent Work (read)

- MCP: `agent_work_list(state, limit)`, `agent_work_get(work_id)`.
- CLI: `orthus work list [--state <s>]`, `orthus work show <work_id>`.

## Local collector / archive (CLI, owner machine only)

- `orthus collector status` — per-source cursors + reachability.
- `orthus collector sync --source <s>` — collect + archive + push one source.
- `orthus archive search "<needle>" [--source <s>]` — local raw archive metadata.

## Keeping this skill current

This skill ships inside the `orthus` CLI package, so it updates with the CLI.

- See available skills: `orthus skills list`.
- Re-read this in full: `orthus skills get orthus-usage --full`.
- If your CLI looks out of date: `orthus update` (source install self-updates;
  bundled Desktop app updates via the Desktop auto-updater).
