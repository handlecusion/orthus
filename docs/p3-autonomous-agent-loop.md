# P3 Autonomous Agent Work Loop

> status: target spec for next phase
> updated: 2026-06-05
> authority: 내부 문서(비공개)의 P3 agent-work 계약을 상세화한다.
> 구현 세부는 `docs/architecture-v2.md`, 운영 규칙은 `AGENTS.md`를 따른다.

이 문서는 P2까지 완성된 central/personal 지식 substrate 위에 다음 phase에서 붙일
자율 작업 루프를 정의한다. 목표는 "LLM이 모든 것을 마음대로 실행하는 시스템"이
아니다. 목표는 새 정보와 자연어 지시를 agent가 해석하고, policy gate가
허용한 일은 자동 처리하며, 검토가 필요한 일은 `agent-work` review queue에 올리고,
데이터가 부족한 일은 사용자에게 필요한 추가 정보를 요청하는 구조다.

이 문서는 제품 내부의 agent-work 루프를 다룬다. Codex 작업을 검토하는 외부 PO role
agent는 서비스 기능이 아니며, 앱 내부 persona나 runtime으로 구현하지 않는다.

---

## 1. 제품 의도

P3는 Orthus를 "질문하면 답하는 지식 시스템"에서 "새 정보가 들어오면 처리할 일을
찾고, 가능한 일은 끝내고, 불확실한 일은 검토/질문으로 되돌리는 지식 운영 시스템"으로
확장한다.

사용자 관점의 완료 문장:

> "내 personal node가 새 메일, 파일, 세션, 보드 변경을 보고 필요한 sync와 정리는
> 알아서 한다. 문서와 메일은 초안으로 만들거나, 내가 허용한 반복 정책 안에서는
> 자동 실행한다. central wiki에 반영될 수 있는 회사 지식은 agent-work에서 검토한 뒤
> publish/promote 또는 wiki 반영으로 넘어간다. 부족한 정보는 agent가 무엇을 더 넣으면
> 되는지 묻는다."

P3가 추가하는 핵심 제품 표면:

| Surface | 역할 |
|---|---|
| Wiki | compiled wiki page와 wiki task를 보는 지식 표면이다. |
| Assistant (`/ask`) | 사용자가 자연어로 agent에게 일을 시킨다. 첫 구현은 route를 바꾸지 않고 UI label만 Assistant로 둔다. 5초 이내 + external write 없음 + review 불필요 작업은 즉시 결과를 보여주고, 나머지는 Agent Work queue로 보낸다. |
| Agent Work (`/agent-work`) | agent가 자동 처리한 일, 검토 대기 초안, 데이터 요청, 거부 사유를 보는 독립 review UI다. |
| Editor | document draft를 승인 전 미리 보고 수정한 뒤 approve하는 작업 표면이다. |
| Policy memory | node별 approve/reject/edit/reason을 기억해 다음 policy gate와 wiki 설명에 반영한다. |

### 확정된 Product Decisions

- Email send는 처음에는 `draft_for_review`로 시작한다. P6 통합 메일은 manual
  compose/send까지만 정착하고, inbound 답장 draft는 P7.1로 미룬다. P7.5 이후 사용자가
  수정 없이 승인/발송한 기록이 같은 policy bucket에서 최근 60일 20건 이상 쌓이고 no-edit
  approval rate가 95%를 넘으며 owner/admin이 해당 bucket을 켜면 auto-send 후보가 될 수
  있다. LLM action judgment는 후보 판단 입력으로 쓸 수 있지만 LLM-only 발송은 금지한다.
- Agent Work는 `/wiki` tab이 아니라 독립 `/agent-work` review UI다.
- Document draft는 승인 전에도 `/editor`에 draft 상태로 보인다. 사용자는 editor에서
  확인/수정 후 approve한다.
- Policy memory summary는 사용자가 직접 편집하지 않는다. Agent가 만든 summary를
  review/approve만 한다.
- Central wiki 반영 전 task triage는 자동 가능하지만, 실제 company knowledge 변경은
  review가 필요하다.

---

## 2. 불변식

P3는 아래 P2 불변식을 깨지 않는다.

1. central과 personal은 DB/corpus/vector/wiki-store/runtime/session을 공유하지 않는다.
2. personal raw/corpus/wiki가 central로 자동 저장되는 경로는 없다.
3. personal to central 이동은 publish/promote 또는 central reviewer가 승인한 company
   agent-work item으로만 가능하다.
4. 답변 grounding은 compiled wiki page 또는 검증된 read-only structured query다.
5. SQL write/DDL/DML은 assistant/agent가 실행하지 않는다.
6. LLM confidence 수치만으로 실행을 결정하지 않는다. P6 이후 LLM action judgment는
   bounded policy input으로 사용할 수 있다.
7. LangGraph/persona/drift/confidence routing/KG/Neo4j 신규 code 또는 stub을 만들지
   않는다.

P3에서 새로 허용되는 것은 **typed action handler**다. connector sync, document draft,
email send, personal board 정리, central wiki 반영 전 task 정리 같은 action family는
각자 typed policy gate와 audit trail을 가진다. LLM은 후보 action, 초안, 필요 데이터,
근거 요약, action judgment를 만들 수 있지만, 실행은 typed action allowlist, role,
secret state, rate limit, audit guard, kill switch를 통과해야 한다.

---

## 3. Work Item Lifecycle

모든 agent 작업은 `AgentWorkItem`으로 표현한다. 구현 schema 이름은 달라져도 아래
상태와 의미는 보존해야 한다.

```text
signal
  -> candidate
  -> policy gate
  -> auto_execute | draft_for_review | request_more_data | reject
  -> reviewer decision when review/data is required
  -> audit + policy memory update
  -> optional wiki update
```

### Signal sources

| Source | 예시 |
|---|---|
| Assistant command | 사용자가 `/ask`에서 "Gmail sync하고 답장 초안 만들어줘"라고 지시 |
| Connector event | Gmail/Drive/GitHub/local file sync 후 새 정보 발견 |
| WikiTask | open_question/conflict/stale/dedup/provenance_fix |
| Data gap | `/ask` 답변 부족으로 생긴 `data_gaps` backlog |
| Board change | personal board task/event/note 변경 |
| Schedule tick | due connector sync, stale task sweep |

### Outcome classes

| Outcome | 의미 | 사용자 표면 |
|---|---|---|
| `auto_execute` | policy gate가 허용한 action을 즉시 실행 | Agent Work에 실행 결과와 근거 표시 |
| `draft_for_review` | 초안/계획/변경 diff를 만들고 승인 대기 | Agent Work review card |
| `request_more_data` | 처리에 필요한 입력이 부족해 질문으로 멈춤 | Assistant surface와 Agent Work에 필요 데이터 표시 |
| `reject` | 정책상 실행 불가 또는 금지 | Agent Work에 reject 사유와 안전 경계 표시 |

`auto_execute`는 "LLM이 자신감 있어서 단독 실행"이 아니다. action type, node boundary,
role, secret state, evidence, reversibility, destination, policy memory, LLM action
judgment를 bounded policy matrix가 통과한 결과다.

Reviewer decision은 `approve`, `dismiss`, `request_more_data`만 허용한다. 전이는 기존
state vocabulary만 쓴다: `approve -> resolved`, `dismiss -> dismissed`,
`request_more_data -> request_more_data`. decision endpoint는 `draft_for_review` 또는
`request_more_data` work item에만 적용되고, `auto_execute`, `rejected`, `resolved`,
`dismissed` 같은 실행/종결 상태에는 fail-closed한다. P3.1b의 approve는 state와 decision
log만 남기며 외부 action runner를 호출하지 않는다.

---

## 4. Action Matrix

P3 첫 구현은 아래 action family로 제한한다.

| Action family | 기본 node | 기본 outcome | auto 조건 | review/data request 조건 |
|---|---|---|---|---|
| Connector sync | company 또는 personal | `auto_execute` | account configured, node policy allows, owner/admin or scheduler, no new secret needed | missing config/secret, stale auth, account_kind mismatch |
| Document draft | company 또는 personal | `draft_for_review` | editor draft 생성 + Agent Work review link, external write 없음 | publish/save/import 전 review 필요 |
| Email send | personal | `draft_for_review` | 같은 policy bucket에서 최근 60일 20건 이상 + no-edit approval rate 95% 초과 + owner/admin opt-in + LLM action judgment 통과 + recipient/domain/template/rate limit이 허용된 반복 발송만 `auto_execute` | new recipient, sensitive content, attachment, unclear intent, missing recipient, 표본/기간/95% 미달, opt-in 없음 |
| Personal board 정리 | personal | `auto_execute` | reversible reorder/triage/status suggestion, owner policy 허용 | delete, external calendar/email write, ambiguous date/time |
| Central wiki 반영 전 task 정리 | company | `draft_for_review` | WikiTask dedup/labeling/summary처럼 reversible queue cleanup은 auto 가능 | company wiki page write, promote approve, conflict resolution |
| Data request | company 또는 personal | `request_more_data` | 필요한 source/field/question이 특정됨 | user answer, connector config, file upload, reviewer decision 필요 |

금지:

- LLM이 SQL write/DDL/DML을 직접 실행.
- personal raw/corpus/wiki를 central로 자동 import.
- central reviewer 승인 없이 promoted company source 생성.
- email을 arbitrary shell 또는 untyped SMTP command로 발송. (P6 통합 메일
  내부 문서(비공개) §5-C에 따라 owner approve를 통과한 manual send만 승인된 외부
  메일서버 API 경유로 허용한다. bounded auto-send는 P7.5에서만 다룬다.)
- node boundary 밖 secret/token 읽기 또는 평문 저장.

---

## 5. Policy Gate

Policy gate는 결정론적 함수여야 한다. 입력은 최소 아래 슬롯을 가진다.

| Slot | 예시 |
|---|---|
| `action_family` | `connector_sync`, `email_send`, `document_draft` |
| `node_kind` | `company`, `personal` |
| `actor_role` | `owner`, `admin`, `member`, `viewer`, `scheduler` |
| `destination` | local draft, personal board, external email, central staging, central wiki |
| `evidence_refs` | wiki pages, source docs, connector run ids, prior policy ids |
| `reversibility` | reversible, append-only, external side effect |
| `secret_state` | configured, missing, expired, unauthorized |
| `policy_memory_refs` | prior approve/reject/edit decisions |
| `llm_action_judgment` | intent/risk/relevance/reply-readiness score and evidence summary |

출력은 `outcome`, `reason_codes`, `required_review_role`, `required_data`, `audit_meta`다.
LLM raw confidence만으로 outcome을 정하지 않는다. LLM action judgment는 입력으로 쓸 수
있지만, typed safety guard와 hard boundary를 override할 수 없다.

---

## 6. UI Contract

P3의 사용자 모델은 독립된 4개 표면이다. `/wiki`는 지식 표면, `/ask`는 Assistant
명령 표면, `/agent-work`는 review queue, `/editor`는 document draft 수정/승인 표면이다.

| Surface | 역할 |
|---|---|
| `/wiki` | compiled wiki page browse/search/ask, wiki task link |
| `/ask` | 자연어로 connector sync, draft, board 정리, data request 처리를 지시. UI label은 Assistant로 표시한다. 5초 이내 + external write 없음 + review 불필요 작업은 즉시 결과 표시 |
| `/agent-work` | 오래 걸리는 작업 queue, auto 실행 결과, draft review, data request, reject 사유, audit/run history |
| `/editor` | document draft 확인/수정/approve |

구현은 기존 `/ask`, `/wiki`, `/wiki/tasks`, `/editor`를 유지하면서 `/agent-work`를
추가한다. `/wiki/tasks`는 독립 task route로 남을 수 있지만 Agent Work source queue로도
연결되어야 한다.

Agent Work card는 최소 아래 정보를 보여야 한다.

- action family, node, actor, source signal
- proposed change 또는 실행 결과
- evidence/source refs
- policy gate outcome과 reason codes
- sensitive/external side-effect warning
- approve/reject/edit/request-data controls
- audit/run ids
- policy memory update 여부

Assistant 응답 규칙:

- 5초 이내 + external write 없음 + review 불필요 작업은 Assistant 화면에 바로 결과를 보여준다.
- 장시간 sync, draft 생성, 외부 write 후보, review 필요 action은 Agent Work queue에
  item을 만들고 Assistant에는 queue 상태와 link를 보여준다.
- `request_more_data`는 Assistant와 Agent Work 양쪽에서 필요한 입력을 명확히 묻는다.

---

## 7. Policy Memory + Wiki Update

Reviewer feedback은 node-local policy memory에 쌓인다.

| Scope | 저장 원칙 |
|---|---|
| company node | company agent-work decisions, central reviewer policy, company wiki에 반영 가능한 운영 규칙 |
| personal node | owner 개인 policy, email/board/source preferences, personal wiki에만 반영 |

Policy memory는 append-only decision log가 primary다. 주기적 summarize job은 승인/거부
패턴을 compiled wiki policy page update 후보로 만들 수 있다. 사용자는 agent가 만든
summary를 review/approve만 하고 직접 편집하지 않는다. 이 wiki update도 node-local
경계를 따른다. personal policy memory를 central로 옮기려면 publish/promote 또는 explicit
company staging이 필요하다.

Policy memory에 저장하는 것:

- action family
- outcome
- reviewer action: approve, reject, edit, request_more_data
- reason codes
- evidence refs
- normalized preference/policy summary
- created_at, actor, node_id

PII와 secret은 저장 전 redaction을 통과해야 한다. email body 원문, OAuth token, secret
value는 policy memory에 저장하지 않는다.

---

## 8. Implementation Milestones

### P3.0 Spec + PO Review Contract

- 본 문서, `system-spec.md`, `architecture-v2.md`, `roadmap.md`, `AGENTS.md` 갱신.
- 외부 Codex PO role agent는 서비스 기능이 아니라 작업 검토 도구로 정의.
- 검증: `make docs-check`.

### P3.1 Agent Work Substrate

- `AgentWorkItem` state machine.
- policy gate function.
- audit/run history.
- Agent Work page/list/detail.
- existing `data_gaps`, `WikiTask`, `promote_staging`, `connector_runs`를 Agent Work source로 연결.

#### P3.1a First Vertical Spine

첫 구현 slice는 P3.1 전체가 아니라 read-only source 하나를 붙인 vertical spine이다.

- node-local `agent_work_items` table.
- canonical `AgentWorkItem`, `AgentWorkCandidate`, `AgentWorkDecision`, `PolicyOutcome`.
- deterministic policy gate와 state mapping. LLM confidence나 LLM 호출은 입력/실행 경로에 없다.
- `data_gaps` adapter: open gap을 `data_request` work item으로 idempotent backfill.
- API: `GET /agent-work`, `GET /agent-work/{id}`, `POST /agent-work/sync/data-gaps`.
- FE: 독립 `/agent-work` list/detail. `/ask`와 분리한다.
- audit: classify/persist span, `correlation_id`, `last_run_id` 노출.

P3.1a는 connector sync, email send, document publish, central wiki write 같은 action runner를
연결하지 않는다. `auto_execute`는 gate outcome으로는 존재하지만 slice-1에서 외부 action을
실행하지 않는다. 특히 email auto-send는 candidate payload의 boolean으로 허용하지 않고,
후속 policy-memory observation store가 최근 60일/20건/95% bucket을 결정론적으로 계산한
뒤에만 gate 조건으로 사용할 수 있다.

#### P3.1b Reviewer Decision Endpoint

- append-only `agent_work_decisions` table.
- API: `POST /agent-work/{id}/decision` with
  `{ decision: "approve" | "dismiss" | "request_more_data", note?: string }`.
- allowed transitions:
  - `draft_for_review + approve -> resolved`
  - `draft_for_review + dismiss -> dismissed`
  - `draft_for_review + request_more_data -> request_more_data`
  - `request_more_data + approve -> resolved`
  - `request_more_data + dismiss -> dismissed`
- reject double decision, already terminal items, `auto_execute`, `rejected`, and
  unsupported states.
- owner/admin-only on session-auth nodes via node operator gate.
- audit span: `agent_work.decision`, existing `correlation_id` propagated,
  reviewer note redacted before persist.
- no external action runner, connector sync, email send, wiki write, or
  policy-memory observation update in this slice.

#### P3.1c WikiTask Source Adapter

- unresolved node-local `WikiTask` rows become idempotent Agent Work items.
- API: `POST /agent-work/sync/wiki-tasks`.
- source mapping:
  - `source_kind="wiki_task"`
  - `source_ref_id=WikiTask.slug`
  - `action_family="central_wiki_task_cleanup"`
  - payload includes `kind`, `description`, `related`, `resolved`, `scope`,
    `node_id`, `cleanup_only`, `company_wiki_write`, `created_at`.
- `open_question` and `conflict` stay `draft_for_review` because they imply
  reviewer judgment before company wiki knowledge changes.
- `stale_audit`, `dedup`, and `provenance_fix` can classify as `auto_execute`
  only as reversible queue-cleanup candidates. P3.1c still does not run any
  cleanup action.
- resolved `WikiTask` rows are skipped by sync and rejected by the policy gate
  if passed directly as candidates.
- personal nodes read only the caller owner's personal wiki task partition.
- no external action runner, connector sync, email send, central wiki write,
  or policy-memory observation update in this slice.

#### P3.1d Promote/Connector Source Expansion

- pending central `promote_staging` rows become idempotent Agent Work items.
- failed node-local `connector_runs` rows become idempotent Agent Work items.
- API:
  - `POST /agent-work/sync/promote-staging`
  - `POST /agent-work/sync/connector-runs`
- source mapping:
  - `source_kind="promote_staging"`, `action_family="promote_review"`,
    `source_ref_id=stage_id`.
  - `source_kind="connector_run"`, `action_family="connector_sync"`,
    `source_ref_id=run_id`.
- promote payload includes sanitized title, source node/doc ids, status,
  target project, redacted source metadata, and sanitized markdown length only;
  it does not duplicate full `sanitized_markdown` into Agent Work payload.
- pending promote stages classify as `draft_for_review` with `no_auto_import`.
  decided/non-pending stages are skipped by sync and rejected by policy if
  passed directly.
- failed connector runs classify as `request_more_data`; unsupported connector
  slugs/account kinds, non-failed runs, accountless runs, and out-of-node/other
  personal-owner runs are skipped.
- no promote approve/reject/import, connector retry/sync execution, email send,
  wiki write, or policy-memory observation update in this slice.

#### P3.2a FE Review Controls

- `/agent-work` detail consumes existing decision endpoint:
  `POST /agent-work/{id}/decision`.
- Review controls are visible only when `state` is `draft_for_review` or
  `request_more_data`.
- `request_more_data` button is hidden when item is already in
  `request_more_data`; backend remains authoritative and still rejects illegal
  transitions or double decisions.
- FE sends optional reviewer note as `note`, shows returned
  `from_state -> to_state`, refreshes local item state, and hides controls once
  item becomes terminal/read-only.
- `/agent-work` top toolbar exposes source syncs for data gaps, wiki tasks,
  promote staging, and connector runs.
- FE does not execute actions, implement policy, write wiki pages, trigger
  connector sync/retry, approve promote import, send email, create drafts, or
  write policy memory.

#### P3.2b Policy Memory Observation Store

- `agent_work.decision` writes an append-only node-local
  `agent_policy_observations` row in the same transaction as
  `agent_work_decisions`.
- Observation rows copy only policy/source/reviewer-decision slots needed for
  learning:
  - node id/kind and personal `owner_id`.
  - work/decision ids.
  - `source_kind`, `source_ref_id`, `action_family`, `policy_outcome`,
    `reason_codes`.
  - reviewer action, state transition, `note_present`, observed time.
  - deterministic `bucket_key`.
- Reviewer note body is not duplicated into policy memory; notes stay only in
  the redacted decision log.
- API: `GET /agent-work/policy-memory` returns bucket summaries for the current
  node, owner-scoped on personal nodes: total, approval, dismiss,
  request-more-data, `note_present_count`, explicit no-edit approval metrics,
  recent 60-day sample counts/rate, and last observed time.
- This slice does not change the policy gate, write policy summaries to wiki,
  auto-send email, retry connectors, approve promote import, or execute any
  action runner.

#### P3.2c Policy Memory Read-Only Context

- Source adapters classify each `AgentWorkCandidate` with the deterministic
  policy gate first, then compute the policy bucket from
  `action_family + source_kind + policy_outcome + reason_codes`.
- The service reads the current node's bucket observations and attaches a
  `policy_memory` object to the persisted Agent Work payload:
  - `bucket_key`, `total`, `approvals`, `dismissals`, `request_more_data`,
    `note_present_count`, explicit no-edit approval metrics, recent 60-day
    sample counts/rate, `email_auto_send_observation_threshold_met`,
    `last_observed_at`.
  - `used_for_outcome=false`.
- On personal nodes, the lookup is owner-scoped just like the summary API.
- P3.2c does not re-run the policy gate after the lookup and does not change
  `AgentWorkDecision.outcome`, state, reason, or required review/data fields.
- Regression coverage must keep email send as `draft_for_review` even when
  policy-memory counts look like they satisfy the future 60-day/20-observation
  and 95% no-edit approval threshold.
- This slice does not write wiki summaries, auto-send email, retry connectors,
  approve promote import, or execute any action runner.

### P3.2 Assistant Command Intake

- `/ask`에서 natural language command를 받는다. UI label은 Assistant로 표시하고,
  `/assistant` route alias는 후순위로 둔다.
- deterministic command detector가 connector sync, document draft, email send,
  personal board cleanup, central wiki task cleanup 계열 명령만 감지한다.
- command는 direct action 실행이 아니라 `source_kind="assistant_command"`
  `AgentWorkItem` candidate를 만든다.
- command queue 생성은 node operator gate를 통과해야 한다. session member나
  정보성 command-shaped 질문은 queue 생성 없이 기존 wiki/structured route로
  fall through 한다.
- `/ask` response는 `mode="agent_work"`와 queued work summary를 반환한다.
- `request_more_data`/draft/reject state는 Agent Work queue에 남고, Assistant 화면은
  queue 결과를 보여준다. Later action handlers may also return already resolved
  auto-executed results.
- P3.2는 5초 이내 simple direct result, connector sync 실행, document draft 생성,
  email send, board cleanup 실행, wiki write를 아직 연결하지 않는다.

### P3.3 First Auto Actions

- connector sync.
- personal board reversible 정리.
- WikiTask/data gap triage.

#### P3.3a Cleanup-Only WikiTask Auto Execute

- `WikiTask` kinds `stale_audit`, `dedup`, and `provenance_fix` are
  cleanup-only queue tasks. They do not change company wiki knowledge pages.
- `/agent-work/sync/wiki-tasks` persists the work item, then immediately executes
  cleanup-only `auto_execute` items by marking the source WikiTask `resolved=true`
  and the Agent Work item `state="resolved"`.
- The auto execution writes an `agent_work.auto_execute` audit span and records
  `payload.auto_execution.kind="wiki_task_cleanup"`.
- `open_question` and `conflict` remain `draft_for_review` because they imply
  knowledge changes or reviewer judgment.
- P3.3a does not run connectors, send email, approve promote imports, or write
  company wiki content pages.

#### P3.3b Configured Assistant Connector Sync Auto Execute

- `/ask` connector sync commands still materialize as
  `source_kind="assistant_command"` Agent Work items first. The policy gate can
  return `auto_execute` only when the candidate payload has a configured active
  connector account, `node_policy_allows=true`, `secret_state="configured"`, and
  actor role `owner`, `admin`, or normalized local/demo operator.
- Auto execution revalidates the account before running:
  `connector_accounts.account_id`, `connector_slug`, `node_id`, `status=active`,
  `account_kind == settings.node_kind`, and personal `owner_id == user_id`.
- The handler calls `run_connector_account_sync(reason="manual")` and writes:
  `agent_work.auto_execute`, nested `connector.command`, one `connector_runs`
  row, and `payload.auto_execution.kind="connector_sync"` with run status/report.
- Succeeded syncs move the Agent Work item to `state="resolved"`. Failed syncs
  stay visible as `state="request_more_data"` with redacted error metadata and
  `auto_execute_failed`; they are not silently resolved.
- Failed `connector_runs` imported by `/agent-work/sync/connector-runs` remain
  triage items. P3.3b does not auto-retry historical failed runs, create new
  secrets/accounts, promote imports, send email, run board cleanup, or write
  central wiki content.
- P3.3e makes that boundary reviewer-visible: failed connector-run work items
  include `payload.retry_guard` and matching evidence with
  `auto_retry_allowed=false`, `requires_operator_review=true`, and
  `used_for_outcome=false`.
- Reviewer decisions on failed connector-run triage items only close or keep the
  Agent Work row. They do not call `run_connector_account_sync`, write
  `payload.auto_execution`, or create a new `connector_runs` row.

#### P3.3c Personal Board Reversible Cleanup Auto Execute

- `/ask` personal board cleanup commands still materialize as
  `source_kind="assistant_command"` Agent Work items first. The policy gate can
  return `auto_execute` only when `node_kind="personal"`, actor role is `owner`,
  `admin`, `scheduler`, or normalized local/demo operator, `reversibility="reversible"`,
  `external_write=false`, and `delete=false`.
- The current handler is intentionally narrow: `cleanup_kind="archive_done_tasks"`
  archives existing personal board tasks whose `status="done"`. It does not
  delete rows, create tasks/events/notes, write email/calendar/GitHub/Notion, or
  touch company board/wiki state.
- Auto execution writes `agent_work.auto_execute`, nested
  `personal_board.cleanup`, and `payload.auto_execution.kind="personal_board_cleanup"`
  with archived task ids/count. The Agent Work item moves to `state="resolved"`.
- Duplicate commands keep the existing terminal item and do not archive again.
- Company-node board cleanup, delete/destructive wording, and external-write
  wording stay `draft_for_review`.

#### P3.3d Data Gap Decision Source Write-Back

- `source_kind="data_gap"` Agent Work review decisions close the source backlog
  row in the same reviewer transaction.
- `approve` writes `data_gaps.status="resolved"` and `dismiss` writes
  `data_gaps.status="dismissed"` for the caller's node scope/owner boundary.
- `request_more_data` deliberately leaves the source row `open` so the missing
  data remains visible until the owner supplies it or dismisses it.
- The work item stores `payload.source_writeback` with the target status,
  whether the source row was updated, and the decision timestamp. Missing or
  malformed source refs do not trigger external side effects; they are recorded
  as non-updated write-back evidence.
- P3.3d does not run corpus/wiki authoring, connector sync, email send,
  promote import, or policy-memory gate escalation.

### P3.4 Draft Actions

- document draft: editor draft 생성 + Agent Work review link.
- email draft/send review: 처음에는 draft only, 같은 policy bucket에서 최근 60일 20건 이상 +
  관측된 no-edit approval rate 95% 초과 시 auto-send 후보.
- central wiki 반영 전 task cleanup.

#### P3.4a Document Draft Handler

- `document_draft` Agent Work items create an editor-visible document row with
  `source="agent_draft"`.
- Draft metadata is written back to the work item payload as
  `draft_document.doc_id`, title, source, status, scope, and project.
- `agent_draft` creation does not call `save_editor_document`, corpus indexing,
  or LLM wiki authoring.
- Existing `draft_document.doc_id` is reused on idempotent command intake; duplicate
  drafts are not created for the same queued command.
- This slice does not create email drafts, execute connector sync, run board
  cleanup, write wiki pages, or approve/promote imports.

#### P3.4b Agent Draft Save/Publish Boundary

- `PUT /documents/{doc_id}` on `source="agent_draft"` saves reviewer edits to the
  draft row only. It must not call corpus indexing or LLM wiki authoring.
- `POST /documents/{doc_id}/publish` is the explicit reviewer boundary. It accepts
  the final editor body, flips `documents.source` from `agent_draft` to `editor`,
  then runs the normal corpus indexing + LLM wiki authoring path with the row's
  existing scope/project.
- Successful publish writes a `document.publish` audit span and resolves the
  matching `document_draft` Agent Work item when the item payload references the
  published `draft_document.doc_id`.
- Publishing a non-draft document returns `409`; missing/non-owned documents
  return `404`.
- The editor UI shows draft saves as draft-only and exposes a publish action only
  for `agent_draft` documents.

#### P3.4c Email Draft Payload + Recipient Request

- Assistant `email_send` commands are still draft-only and do not create an SMTP
  sender, provider call, or external write.
- If a redacted recipient hint is present, the persisted work item stays
  `draft_for_review` and includes `payload.email_draft`:
  - `recipient_hint`
  - `subject_hint`
  - `body_template`
  - `intent="draft_for_review"`
  - `source="assistant_command_template"`
  - `status="draft"`
  - `used_for_outcome=false`
- `payload.email_draft` must not include `smtp_*`, `send_*`, provider message id,
  token, or external delivery metadata.
- If recipient identity is missing, deterministic policy returns
  `request_more_data` with reason code `recipient_required` and
  `payload.required_data=["recipient name, address, or thread reference"]`.
- This closes the reviewability gap for email drafts without opening an email
  send path in this slice. P6 can later use this payload as part of bounded
  auto-send after extra guard/opt-in is added.

#### P3.4d Typed Email Draft Payload Allowlist

- `payload.email_draft` is produced from canonical `EmailDraftPayload`.
- The model allows only `recipient_hint`, `subject_hint`, `body_template`,
  `intent`, `source`, `status`, and `used_for_outcome`.
- Pydantic `extra="forbid"` rejects accidental `smtp_*`, `send_*`,
  provider message id, token, or delivery metadata before any sender work lands.
- This is schema hardening only. It does not add a sender, change email outcome,
  or use policy memory for outcome.

### P3.5 Policy Memory Learning

- node-local policy summary page generation.
- policy memory refs를 policy gate outcome 계산에 사용.
- email auto-send 후보는 같은 policy bucket에서 최근 60일 20건 이상 +
  no-edit approval rate 95% 초과를 결정론적으로 계산한 뒤에만 허용한다.

#### P3.5a Policy Memory Wiki Summary

- `POST /agent-work/policy-memory/wiki-summary` writes a deterministic
  `agent-policy-memory` wiki page from current `agent_policy_observations`
  bucket summaries.
- Company nodes write the page in company scope. Personal nodes write an
  owner-scoped personal page.
- The page records bucket counts, approve/dismiss/request-more-data rates,
  note-present rate, explicit no-edit approval rate, recent 60-day threshold
  evidence, node identity, and `Used for outcome: false`.
- The endpoint is operator-only and has a dedicated
  `agent_work.policy_memory_wiki` audit span.
- The `/agent-work` UI exposes a manual `policy wiki` toolbar action. This action
  does not run connectors, send email, approve promote imports, or execute queued
  work.
- P3.5a itself does not use policy memory for policy outcome escalation, auto-send,
  or connector retry. P7.5 may explicitly promote policy memory to bounded policy
  input for mail auto-send.

#### P3.5b Email No-Edit Threshold Evidence

- `POST /agent-work/{id}/decision` accepts optional `no_edit=true` telemetry.
  Server-side telemetry guard preserves `no_edit` only for
  `decision="approve"` on `draft_for_review` items in the exact email
  auto-send policy bucket. Non-matching direct API values are normalized to
  `null` before policy observation write.
- Only eligible `decision="approve"` with `no_edit=true` counts as a no-edit
  approval.
- `agent_policy_observations.meta.no_edit_approval` stores the explicit signal.
  Reviewer note bodies still stay out of policy memory.
- Policy memory summary/context now expose:
  - `no_edit_approvals`
  - `recent_window_days=60`
  - `recent_total`
  - `recent_no_edit_approvals`
  - `recent_no_edit_approval_rate`
  - `email_auto_send_observation_threshold_met`
- Threshold is deterministic: same bucket, recent 60-day observations,
  `recent_total >= 20`, and `recent_no_edit_approval_rate > 0.95`.
  The denominator is all observations in that bucket, so dismiss/request-more-data
  and edited approvals lower the rate.
- P3.5b is evidence only. In this slice `used_for_outcome=false` remains true,
  `email_send` still persists as `draft_for_review`, and no email send handler is
  executed. P7.5 may explicitly consume the same metrics for bounded auto-send.

#### P3.5c Email Auto-Send Gate Preflight Evidence

- Draft-review email command classification attaches `payload.email_auto_send_gate`
  after the base draft decision and policy-memory bucket re-query. Email
  commands stopped as `request_more_data` do not carry this gate payload.
- The preflight uses the exact historical bucket
  `email_send|assistant_command|draft_for_review|email_draft_first,policy_memory_observation_gate_required`.
- `eligible=true` requires:
  - source kind `assistant_command`
  - personal node
  - actor role `owner` or `admin`
  - recent 60-day no-edit threshold met
  - recipient identity present as non-PII `recipient_hash`/`recipient_id`
  - recipient/domain/template/rate-limit policy all true
  - no sensitive content
  - no attachment
- The evidence records `checks`, `missing_checks`, recent counts/rate, and
  `threshold_met`.
- P3.5c still sets `used_for_outcome=false`. In this slice, even when preflight
  is eligible, `email_send` remains `draft_for_review` and no email sender/handler
  runs. P7.5 may replace this with explicit outcome use behind opt-in and kill switch.

#### P3.5d Email No-Edit Telemetry Server Guard

- The UI exposes `no-edit approval` only for draft email-gated work, but the API
  must not rely on UI gating.
- Before writing `agent_policy_observations`, the server keeps `no_edit` only
  when all are true:
  - reviewer decision is `approve`
  - current item state is `draft_for_review`
  - policy bucket equals the exact email auto-send bucket
- For `request_more_data`, non-email buckets, dismissals, and direct API misuse,
  `meta.no_edit=null` and `meta.no_edit_approval=false`.
- This is telemetry-only in P3. It does not change review transitions, email send
  outcome, or create a sender/handler until a later spec such as P7.5 explicitly
  promotes it.

#### P3.6a Email Fake Sender Boundary

- `ORTHUS_EMAIL_SENDER=none` is the default. In that mode, eligible email draft
  work stays `draft_for_review` and writes no send log.
- The only implemented sender is `fake`. It never opens SMTP, SES, SendGrid, or
  any external provider connection.
- Fake auto-send can resolve an item only when all deterministic checks pass:
  - `action_family="email_send"` and current state `draft_for_review`
  - personal node and `owner_id == user_id`
  - actor role `owner` or `admin`
  - `payload.email_auto_send_gate.eligible=true`
  - `payload.email_auto_send_gate.used_for_outcome=false`
  - typed `EmailDraftPayload`
  - no existing `email_send_log.status="sent"` for the same work id
  - no sent row for the same `(node_id, owner_id, recipient_hash)` in the last
    hour
- Success writes `agent_work.auto_execute`, nested `email.send`, one
  `email_send_log` row with `status="sent"`, and
  `payload.auto_execution.kind="email_send"` before moving the work item to
  `resolved`.
- Rate limit keeps the item `draft_for_review`, writes
  `email_send_log.status="rate_limited"`, and adds reason code
  `email_send_rate_limited`.
- `email_send_log` stores only recipient/subject/body hashes. It does not store
  raw addresses, subject text, body text, provider message ids, SMTP settings,
  tokens, or external delivery metadata.
- Real provider senders remain out of scope for P3.6a. P6 manual compose/send may
  send through approved external mail-server APIs after owner approval while orthus
  still does not own SMTP/SES/Resend provider credentials directly. Auto-send stays
  P7.5.

---

## 9. Acceptance Criteria

P3 완료는 아래 end-to-end로 증명한다.

1. Personal owner가 `/ask`에서 "Gmail sync하고 답장 초안 만들어줘"라고 지시한다.
2. agent가 connector sync를 policy gate로 `auto_execute`하고 run history를 남긴다.
3. 새 메일 근거로 email draft를 만들고 `draft_for_review`로 Agent Work에 올린다.
4. owner가 approve/edit/reject 중 하나를 선택하고 decision reason이 policy memory에 기록된다.
5. 같은 policy bucket에서 최근 60일 20건 이상 + no-edit approval rate 95%를 넘기 전까지 email send는 계속 draft다.
6. 95%를 넘긴 뒤에도 auto-send는 recipient/domain/template/rate limit policy를 통과해야 하며, P3.6a에서는 `fake` sender만 실행 가능하다.
7. personal board 정리 요청은 reversible change만 auto 실행하고, delete/external write는 review로 멈춘다.
8. central WikiTask cleanup은 triage만 auto 가능하고, central wiki write/import는 승인 전 실행되지 않는다.
9. document draft는 승인 전 editor에서 확인/수정 가능해야 한다.
10. 부족한 source/config/question은 `request_more_data`로 멈추고 필요한 입력을 구체적으로 묻는다.
11. personal raw/corpus/wiki가 central에 자동 저장되지 않는다.
12. 모든 action은 audit/run id와 policy reason code를 남긴다.

---

## 10. Fixed Parameters

구현 전 product parameter는 아래처럼 고정한다.

- Email no-edit approval rate 95%는 같은 policy bucket에서 최근 60일 20건 이상을 기준으로 계산한다. Denominator는 해당 bucket의 recent observations 전체이며, no-edit approval은 server-side guard를 통과한 explicit `no_edit=true` approval만 센다.
- "간단한 작업"은 5초 이내 + external write 없음 + review 불필요 작업이다.
- P3 첫 구현은 기존 `/ask` route를 유지하고 UI label만 Assistant로 둔다. `/assistant`
  alias는 후순위다.
