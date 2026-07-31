"""structured(PG) backend: NL → SQL over our own JSONB row store `notion_rows`
(P2.2a, docs/architecture-v2.md §1/§5).

The 비서 validation gate is reused AS-IS. Only the target changes: instead of an
external DSN, queries run against OUR Postgres read-only role (`pg_dsn_readonly`)
over the `notion_rows` table. The logical catalog has fixed columns — including
`properties JSONB` — so the gate's `schema_ok` accepts `properties->>'<key>'`
(the key is data, not a column) while still rejecting unknown tables/columns and
all 5 reject classes. Double defense (app gate + read-only role) is preserved.

Scope-isolation hardening (P2.2b): after the gate passes, the validated SQL is
deterministically rewritten so every `notion_rows` reference becomes a scope-
filtered subquery (company OR own-personal). A crafted
`WHERE db_name='<another user's personal db>'` therefore cannot read another
user's rows even at the executor level — the predicate is injected by server-
controlled code, not the LLM.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from uuid import UUID

import sqlglot
from sqlglot import exp
from sqlalchemy import select

from orthus.assistant.compile import compile_query
from orthus.assistant.execute import execute_readonly
from orthus.assistant.pipeline import grounding_for, insert_run, update_run
from orthus.assistant.validate import validate
from orthus.audit import audit
from orthus.db import session

# 두 층이 겹친다(의도된 구조 — 실험 리포트 "(b) 작업별 배정 위에 (d) 검증 캐스케이드"):
#   1차 시도 = `get_chat_model_for(TASK_STRUCTURED)`  — 작업별 모델 배정(#669).
#              오케스트레이션 flag off면 기존 `get_chat_model()`과 동일하다.
#   재질의   = `get_fallback_chat_model()`             — 결정론 검증 캐스케이드(SVC).
# 배정이 켜지면 zero-answer가 오히려 늘 수 있으므로(E2 실측: solar 18% vs baseline 11%)
# 캐스케이드는 배정과 함께 살아 있어야 한다. 한쪽을 지우지 말 것.
from orthus.models.orchestration import TASK_STRUCTURED, get_chat_model_for
from orthus.models.registry import get_fallback_chat_model
from orthus.schemas.canonical import AssistantResult, CompiledQuery, ValidationResult
from orthus.settings import get_settings
from orthus.tables import notion_rows, structured_rows

_DIALECT = "postgres"

# Fixed logical schema of the JSONB row store. `properties` is JSONB; the LLM
# reads keys via `properties->>'<key>'`, which the gate accepts (the key is data).
_COLUMNS: dict[str, str] = {
    "db_id": "text",
    "db_name": "text",
    "row_id": "text",
    "properties": "jsonb",
    "scope": "text",
    "owner_id": "text",
    "user_id": "text",
    "updated_at": "timestamptz",
}
_STRUCTURED_ROW_COLUMNS: dict[str, str] = {
    "source": "text",
    "record_type": "text",
    "source_doc_id": "text",
    "source_external_id": "text",
    "source_account_id": "text",
    "record_key": "text",
    "properties": "jsonb",
    "evidence": "text",
    "confidence": "text",
    "scope": "text",
    "project": "text",
    "owner_id": "text",
    "user_id": "text",
    "updated_at": "timestamptz",
}
_SCOPED_STRUCTURED_TABLES = {"notion_rows", "structured_rows"}


def _scope_clause(user_id: UUID, scope: str, project: str | None = None):
    """Rows visible to `user_id`: company (shared) OR own-personal — never another
    user's personal rows (docs/architecture-v2.md §2). When `project` is set, narrow
    to that company→project bucket (P2)."""
    own_personal = (notion_rows.c.scope == "personal") & (notion_rows.c.user_id == user_id)
    if scope == "company":
        clause = notion_rows.c.scope == "company"
    elif scope == "personal":
        clause = own_personal
    else:
        clause = (notion_rows.c.scope == "company") | own_personal
    if project is not None:
        clause = clause & (notion_rows.c.project == project)
    return clause


def _structured_rows_scope_clause(user_id: UUID, scope: str, project: str | None = None):
    own_personal = (structured_rows.c.scope == "personal") & (structured_rows.c.user_id == user_id)
    if scope == "company":
        clause = structured_rows.c.scope == "company"
    elif scope == "personal":
        clause = own_personal
    else:
        clause = (structured_rows.c.scope == "company") | own_personal
    if project is not None:
        clause = clause & (structured_rows.c.project == project)
    return clause


def _scope_predicate_sql(user_id: UUID, scope: str, project: str | None = None) -> str:
    """The scope (+ optional project) filter as raw SQL over base `notion_rows`
    columns. `user_id` is server-controlled (never LLM/user input) and is a UUID;
    `project` is a server-controlled enum value — both are safe to inline as quoted
    literals."""
    uid = str(user_id)
    company = "scope = 'company'"
    own_personal = f"(scope = 'personal' AND user_id = '{uid}')"
    if scope == "company":
        clause = company
    elif scope == "personal":
        clause = own_personal
    else:
        clause = f"{company} OR {own_personal}"
    if project is not None:
        clause = f"({clause}) AND project = '{project}'"
    return clause


def _inject_scope_filter(
    final_sql: str, user_id: UUID, scope: str, project: str | None = None
) -> str:
    """Rewrite every scoped structured table reference into a scope-filtered
    derived table, regardless of the SQL the LLM produced. Returns rewritten SQL.

        notion_rows -> (SELECT * FROM notion_rows WHERE <scope>) AS notion_rows
        structured_rows -> (SELECT * FROM structured_rows WHERE <scope>) AS structured_rows

    The rewritten statement is still a single read-only SELECT (asserted); the
    gate already validated kind/schema/explain on the pre-rewrite SQL, and the
    transform only narrows the rows the executor can see."""
    root = sqlglot.parse_one(final_sql, read=_DIALECT)
    predicate = _scope_predicate_sql(user_id, scope, project)

    def _transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table) and node.name in _SCOPED_STRUCTURED_TABLES:
            table_name = node.name
            filtered = sqlglot.parse_one(
                f"SELECT * FROM {table_name} WHERE {predicate}", read=_DIALECT
            )
            alias = node.alias_or_name or table_name
            return exp.Subquery(
                this=filtered.copy(),
                alias=exp.TableAlias(this=exp.to_identifier(alias)),
            )
        return node

    rewritten = root.transform(_transform)
    if not isinstance(rewritten, (exp.Select, exp.SetOperation)):
        raise ValueError("scope rewrite produced non-SELECT")
    return rewritten.sql(dialect=_DIALECT)


def build_notion_catalog(user_id: UUID, *, scope: str = "all", project: str | None = None) -> dict:
    """Catalog for the gate: the fixed `notion_rows` columns plus a description
    listing each visible `db_name` and the union of its `properties` keys, so the
    model knows what to filter and aggregate on. When `project` is set, the catalog
    is scoped to that company→project bucket (P2)."""
    clause = _scope_clause(user_id, scope, project)
    db_keys: dict[str, set[str]] = {}
    internal_team_member_names: set[str] = set()
    row_type_keys: dict[str, set[str]] = {}
    with session() as s:
        rows = s.execute(
            select(notion_rows.c.db_name, notion_rows.c.properties).where(clause)
        ).all()
        generic_rows = s.execute(
            select(structured_rows.c.record_type, structured_rows.c.properties).where(
                _structured_rows_scope_clause(user_id, scope, project)
            )
        ).all()
    for db_name, properties in rows:
        keys = db_keys.setdefault(db_name, set())
        if isinstance(properties, dict):
            keys.update(properties.keys())
            if db_name == "팀원":
                name = str(properties.get("이름") or "").strip()
                if name:
                    internal_team_member_names.add(name)
    for record_type, properties in generic_rows:
        keys = row_type_keys.setdefault(record_type, set())
        if isinstance(properties, dict):
            keys.update(properties.keys())

    if db_keys:
        parts = [f"{name} [{', '.join(sorted(keys))}]" for name, keys in sorted(db_keys.items())]
        description = (
            "Generic JSONB row store of Notion DB rows. Filter rows by `db_name`; "
            "read row fields from the JSONB `properties` column with "
            "`properties->>'<key>'`. Databases and their property keys: "
            + "; ".join(parts)
            + _semantic_hints_for_dbs(db_keys, internal_team_member_names)
        )
    else:
        description = (
            "Generic JSONB row store of Notion DB rows (currently empty for this "
            "scope). Filter by `db_name`; read fields via `properties->>'<key>'`."
        )

    return {
        "tables": {
            "notion_rows": {"columns": dict(_COLUMNS), "description": description},
            "structured_rows": {
                "columns": dict(_STRUCTURED_ROW_COLUMNS),
                "description": _structured_rows_description(row_type_keys),
            },
        }
    }


def _structured_rows_description(row_type_keys: dict[str, set[str]]) -> str:
    contact_policy = (
        " For internal Acme/company team member directory questions, use "
        "`notion_rows` with `db_name = '팀원'` first. Use `structured_rows` contact "
        "records for questions that explicitly mention Slack/source-derived contacts "
        "or when a requested contact field is absent from `팀원`. When reading "
        "Slack contact rows, prefer non-redacted records by filtering "
        "`COALESCE(properties->>'is_redacted', 'false') <> 'true'` when possible."
    )
    if not row_type_keys:
        return (
            "Generic structured facts extracted from non-Notion sources such as Slack. "
            "Filter by source and record_type; read fields from JSONB properties with "
            "`properties->>'<key>'`." + contact_policy
        )
    parts = [
        f"{record_type} [{', '.join(sorted(keys))}]"
        for record_type, keys in sorted(row_type_keys.items())
    ]
    return (
        "Generic structured facts extracted from non-Notion sources such as Slack. "
        "Filter by source and record_type; read fields from JSONB properties with "
        "`properties->>'<key>'`. Available record types and property keys: "
        + "; ".join(parts)
        + ". Prefer this table when a question asks for Slack-derived links, "
        "events, action items, decisions, or project updates." + contact_policy
    )


def _semantic_hints_for_dbs(
    db_keys: dict[str, set[str]], internal_team_member_names: set[str]
) -> str:
    """Domain disambiguation for DB names whose labels are easy to confuse."""
    has_team_members = "팀원" in db_keys
    has_partner_staff = any(name.strip() == "직원" for name in db_keys)
    if not (has_team_members and has_partner_staff):
        return ""
    names = ", ".join(sorted(internal_team_member_names))
    name_hint = f" Current internal team member names: {names}." if names else ""
    return (
        " Important database semantics: `db_name = '팀원'` means internal "
        "Acme/company team members. For questions about 아크메 직원, "
        "회사 직원, 내부 직원, team members, or members, use `db_name = '팀원'` "
        f"and for questions naming one of the current internal team members use "
        f"`db_name = '팀원'`.{name_hint} "
        "and prefer fields 이름, 역할, 이메일, 활성. If a requested internal "
        "team member contact field such as 전화번호 is not present on `팀원`, "
        "use `structured_rows` contact records as a secondary source, but do not "
        "switch to partner/vendor staff databases. For email, prefer `팀원.이메일` "
        "over Slack contact rows because Slack may contain redacted copies. "
        "`db_name = '직원'` and "
        "`db_name = '직원 '` mean partner/vendor staff contacts, not internal "
        "Acme employees."
    )


# ── SVC: 결정론 검증 캐스케이드 ────────────────────────────────────────────────
#
# 검증 게이트는 "유효하지 않은 SQL"은 막지만 "유효한데 틀린 SQL"은 통과시킨다. 그런데
# 실행 결과 자체가 "이 답은 망가졌다"고 결정론적으로 말해 주는 신호가 이미 있다 — 게이트
# 실패 / 미실행 / 0행 / 결과 정수집합이 {0}. 실측(E1): 현행 모델이 틀린 골든 DB질의
# 4문항이 전부 이 신호에 걸린다. 신호가 뜰 때만 2차 모델로 한 번 더 묻는다.
#
# 계약(설계 문서 §3.1):
#   1차 → _retry_signal(result)
#         └ None → 1차 그대로 반환 (기존 동작)
#         └ 발동 → 2차 모델로 재실행 (같은 게이트를 그대로 통과해야 실행된다)
#                  └ 2차도 신호 발동 → 1차 유지 ("진짜 0"인 답 보존)
#                  └ 2차 정상        → 2차 채택
#
# 모델 confidence/logprob/self-report는 일절 보지 않는다(절대룰: confidence routing
# 금지). 채택 여부는 아래 결정론 코드가 정한다(LLM 판단 0회).

_FALLBACK_MODES = {"off", "shadow", "on"}

# 2차 채택 대상 트리거. `zero_answer`(1차가 실행에 성공했는데 결과가 확신에 찬 0)는 **제외**한다.
# 실측(experiments/fugu-ko/analysis/svc-cascade-verify-results.md §3.6-3.7): 확신한 0은 대개
# 진짜 0이라, 2차가 다른 해석으로 비-0을 내면 그걸 채택해 진짜-0 질문을 confidently-wrong으로
# 만든다(적대적 TN 오발동 6/6이 전부 zero_answer 1차였고 R3가 전부 방지). zero_answer에서
# 2차를 아예 부르지 않고 1차의 0을 유지한다(회수 1건은 포기하나 gpt 고유 이모지 버그라
# prod solar 1차엔 거의 안 나타나고, 오발동은 어떤 1차에서도 발생 → 8:1 유리). 나머지 세 트리거
# (gate_fail/not_executed/empty_rows = 1차가 명백히 깨졌거나 아무것도 못 냄)는 2차 채택 유지.
_ADOPT_ELIGIBLE_TRIGGERS = frozenset({"gate_fail", "not_executed", "empty_rows"})


def _numeric_cells(rows: list[list]) -> set[float]:
    """Numeric values present in the result rows (bool은 숫자가 아니다)."""
    numbers: set[float] = set()
    for row in rows:
        for cell in row:
            if isinstance(cell, bool):
                continue
            if isinstance(cell, (int, float, Decimal)):
                numbers.add(float(cell))
    return numbers


def _retry_signal(result: AssistantResult) -> str | None:
    """ "이 결과는 망가져 보인다"는 결정론 신호. 순수 함수 · LLM 0회.

    `AssistantResult`의 기존 필드(validation/status/row_count/rows)만 읽는다. 반환값은
    `gate_fail` | `not_executed` | `empty_rows` | `zero_answer` | None(정상)."""
    if not result.validation.passed:
        return "gate_fail"
    if result.status != "executed":
        return "not_executed"
    if result.row_count == 0 or not result.rows:
        return "empty_rows"
    numbers = _numeric_cells(result.rows)
    if numbers and numbers == {0.0}:
        return "zero_answer"
    return None


def retry_signal(result: AssistantResult) -> str | None:
    """Public alias of `_retry_signal` — 다른 모듈(예: decompose의 read→act 게이트)이
    같은 결정론 신호를 재사용할 수 있게 한다."""
    return _retry_signal(result)


def _fallback_mode(settings) -> str:
    mode = (settings.structured_fallback_mode or "").strip().lower()
    return mode if mode in _FALLBACK_MODES else "off"  # 오타는 fail-closed


def _primary_model_id() -> str:
    """audit에 남길 **실제** 1차 모델 id.

    `settings.llm_model`을 쓰면 안 된다 — 작업별 배정(#669)이 켜지면 1차는 배정된 워커
    (예: solar)이지 기본 모델이 아니다. 잘못 기록하면 "1차 gpt-4o-mini가 zero를 냈다"는
    거짓 증거가 쌓인다. `get_chat_model_for`는 결정론 dict lookup + 어댑터 생성뿐이라
    네트워크를 타지 않는다(트리거가 뜬 경우에만 호출된다).
    """
    try:
        model = get_chat_model_for(TASK_STRUCTURED)
        return str(getattr(model, "model_id", "") or "") or get_settings().llm_solar_model
    except Exception:  # noqa: BLE001 — 감사 메타 때문에 답을 잃지 않는다
        return get_settings().llm_solar_model


def _run_once(
    user_id: UUID,
    question: str,
    scope: str,
    project: str | None,
    chat_model,
) -> AssistantResult:
    """한 번의 정규 run: 자체 query_id + 자체 `assistant.run` correlation + 자체
    `query_runs` row(PII redaction 경로 불변) + 동일한 검증 게이트."""
    query_id = uuid.uuid4()
    with audit("assistant.run", correlation_id=query_id):
        return _query_structured(query_id, user_id, question, scope, project, chat_model)


def _verify_and_maybe_retry(
    primary: AssistantResult,
    user_id: UUID,
    question: str,
    scope: str,
    project: str | None,
) -> AssistantResult:
    settings = get_settings()
    mode = _fallback_mode(settings)
    if mode == "off":
        return primary
    if mode == "on" and not settings.llm_fallback_model.strip():
        return primary  # 미설정 = 기능 off. 신호 계산조차 하지 않는다(동작 불변).

    trigger = _retry_signal(primary)
    if trigger is None:
        return primary

    if mode == "shadow":
        # SVC.0 관측 모드: 발동률만 기록하고 재질의는 하지 않는다(추가 LLM 호출 0).
        with audit("structured.fallback_shadow", correlation_id=primary.query_id) as span:
            span.add_meta(
                trigger=trigger,
                adopted=False,
                primary_model=_primary_model_id(),
                primary_query_id=str(primary.query_id),
            )
        return primary

    if trigger not in _ADOPT_ELIGIBLE_TRIGGERS:
        # `zero_answer`(확신한 0)는 채택 비적격 → 2차를 부르지 않고 1차의 0을 유지한다
        # (오발동 방어, §_ADOPT_ELIGIBLE_TRIGGERS 주석). shadow 스팬으로 관측만 남긴다.
        with audit("structured.fallback_shadow", correlation_id=primary.query_id) as span:
            span.add_meta(
                trigger=trigger,
                adopted=False,
                retry_skipped="non_adopt_trigger",
                primary_model=_primary_model_id(),
                primary_query_id=str(primary.query_id),
            )
        return primary

    fallback_model = get_fallback_chat_model()
    if fallback_model is None:
        return primary  # provider 슬롯이 2차를 못 만든다 → fail-closed, 1차 유지.

    with audit("structured.fallback", correlation_id=primary.query_id) as span:
        span.add_meta(
            trigger=trigger,
            primary_model=_primary_model_id(),
            fallback_model=settings.llm_fallback_model,
            primary_query_id=str(primary.query_id),
        )
        try:
            second = _run_once(user_id, question, scope, project, fallback_model)
        except Exception as e:  # noqa: BLE001 — 2차 실패는 1차를 무너뜨리지 않는다
            span.add_meta(adopted=False, fallback_error=type(e).__name__)
            return primary
        second_trigger = _retry_signal(second)
        adopted = second_trigger is None
        span.add_meta(
            adopted=adopted,
            fallback_trigger=second_trigger,
            fallback_query_id=str(second.query_id),
        )
    # 2차도 신호가 뜨면 1차를 유지한다 — 두 모델이 독립적으로 "없다"고 하면 진짜 없는
    # 것으로 본다(오발동 방어, 설계 문서 §6.1).
    return second if adopted else primary


def query_structured(
    user_id: UUID,
    question: str,
    *,
    scope: str = "all",
    project: str | None = None,
    chat_model=None,
) -> AssistantResult:
    """NL → SQL over `notion_rows`, gated and executed read-only. One correlation_id
    (= query_id) ties every step's audit span together (§4.3). When `project` is set,
    both the grounding catalog and the executor-level scope rewrite are narrowed to
    that company→project bucket so cross-project rows cannot leak (P2).

    SVC: 1차 실행 뒤 결정론 신호가 뜨면 2차 모델로 한 번 더 묻는다(위 §SVC). 호출자가
    `chat_model=`을 명시 주입한 경우(실험/테스트 하네스)에는 개입하지 않는다 — 단일
    모델 측정이 오염되지 않아야 한다. `_query_structured` 본문은 불변이다."""
    primary = _run_once(user_id, question, scope, project, chat_model)
    if chat_model is not None:
        return primary
    return _verify_and_maybe_retry(primary, user_id, question, scope, project)


def _query_structured(
    query_id: UUID,
    user_id: UUID,
    question: str,
    scope: str,
    project: str | None,
    chat_model,
) -> AssistantResult:
    chat_model = chat_model or get_chat_model_for(TASK_STRUCTURED)
    settings = get_settings()
    started = time.monotonic()

    catalog = build_notion_catalog(user_id, scope=scope, project=project)
    insert_run(query_id, user_id, None, question)

    # (a) compile.
    compiled: CompiledQuery | None = None
    try:
        compiled = compile_query(question, None, _DIALECT, catalog, [], chat_model)
    except Exception as e:  # noqa: BLE001 — record compile failure, never execute
        validation = ValidationResult(rejected_reason=f"compile_failed:{e}")
        update_run(
            query_id,
            compiled_sql=None,
            validation=validation.model_dump(),
            status="rejected",
            result_meta=None,
        )
        return AssistantResult(
            query_id=query_id,
            question=question,
            source_id=None,
            compiled=None,
            validation=validation,
            status="rejected",
            message="Could not compile the question into a query. Try rephrasing.",
        )

    # (b) validate (the gate) — EXPLAIN runs against our read-only PG role.
    validation, final_sql = validate(
        compiled.sql,
        _DIALECT,
        catalog,
        default_limit=settings.assistant_default_limit,
        dsn_secret_key=None,
    )
    grounding = grounding_for([], final_sql if validation.passed else None, _DIALECT)

    # (c) rejected → record, do NOT execute.
    if not validation.passed:
        update_run(
            query_id,
            compiled_sql=compiled.sql,
            validation=validation.model_dump(),
            status="rejected",
            result_meta=None,
        )
        return AssistantResult(
            query_id=query_id,
            question=question,
            source_id=None,
            compiled=compiled,
            validation=validation,
            status="rejected",
            grounding=grounding,
            message=f"Query rejected by the validation gate: {validation.rejected_reason}.",
        )

    # (c.5) scope-isolation hardening: rewrite every notion_rows reference into a
    # scope-filtered subquery before execution. Deterministic, server-controlled.
    # Fail-closed: any rewrite failure rejects the query rather than executing
    # the unrewritten (unscoped) SQL.
    try:
        final_sql = _inject_scope_filter(final_sql, user_id, scope, project)
    except Exception as e:  # noqa: BLE001
        rewrite_validation = ValidationResult(
            rejected_reason=f"scope_rewrite_failed:{type(e).__name__}"
        )
        update_run(
            query_id,
            compiled_sql=compiled.sql,
            validation=rewrite_validation.model_dump(),
            status="rejected",
            result_meta=None,
        )
        return AssistantResult(
            query_id=query_id,
            question=question,
            source_id=None,
            compiled=compiled,
            validation=rewrite_validation,
            status="rejected",
            grounding=grounding,
            message=f"Query rejected: scope rewrite failed ({type(e).__name__}).",
        )

    # (d) passed → execute read-only against our PG read-only role.
    dsn = settings.pg_dsn_readonly
    try:
        columns, rows, row_count = execute_readonly(
            final_sql, dsn, settings.assistant_query_timeout_ms
        )
    except Exception as e:  # noqa: BLE001 — execution failure
        latency_ms = int((time.monotonic() - started) * 1000)
        update_run(
            query_id,
            compiled_sql=final_sql,
            validation=validation.model_dump(),
            status="failed",
            result_meta={"row_count": None, "latency_ms": latency_ms},
        )
        return AssistantResult(
            query_id=query_id,
            question=question,
            source_id=None,
            compiled=compiled,
            validation=validation,
            status="failed",
            grounding=grounding,
            latency_ms=latency_ms,
            message=f"Query passed validation but failed to execute: {e}.",
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    update_run(
        query_id,
        compiled_sql=final_sql,
        validation=validation.model_dump(),
        status="executed",
        result_meta={"row_count": row_count, "latency_ms": latency_ms},
    )
    return AssistantResult(
        query_id=query_id,
        question=question,
        source_id=None,
        compiled=CompiledQuery(question=question, source_id=None, dialect=_DIALECT, sql=final_sql),
        validation=validation,
        status="executed",
        columns=columns,
        rows=rows,
        row_count=row_count,
        latency_ms=latency_ms,
        grounding=grounding,
    )
