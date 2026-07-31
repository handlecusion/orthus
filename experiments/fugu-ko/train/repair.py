"""D7 — 결정론 SQL 수리 (LLM 0회, sqlglot AST 조작).

D6는 "어느 워커를 고를까"만 봤고 그래서 **oracle(68.0%)이 천장**이었다. 수리는 선택이
아니라 **후보 SQL을 고쳐서 다시 실행**하므로 그 천장을 넘을 수 있다(전 워커가 틀린 문항도
고칠 수 있다).

관측된 solar 주 실패 모드(2026-07-16 실측):

  Q "파트너사에는 몇 개나 들어 있어?"  (총계 질문 — 숫자 하나 기대)
  SQL SELECT properties->>'이름', COUNT(*) ... GROUP BY 1     → [1,1,1,...] (오답)
  수리 SELECT COUNT(*) ...                                     → [11]      (정답, 실증)

안전: sqlglot AST를 조작하고 결과를 **기존 검증 게이트에 다시 태운다**(SELECT-only /
read_only / schema_ok / EXPLAIN 불변). 새 SQL 텍스트를 LLM이 쓰는 게 아니라 기존 AST에서
노드를 제거/치환할 뿐이라 write/DDL이 생길 수 없다.

수리는 **규칙**이다 — D7의 "학습기가 이긴다" 주장에서 이건 *강화된 규칙 베이스라인(R+)*
쪽에 속한다. 공정한 싸움을 위해 학습 검증기는 R+를 이겨야 한다(§d7-execution-guide §4.6).
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

# 총계 의도 신호 — '숫자 하나'를 기대하는 질문
COUNT_TERMS = (
    "몇 개", "몇 건", "몇 명", "개수", "총 몇", "몇 개나", "얼마나",
    "규모가", "세어", "총계", "총 개수", "몇이야", "몇개",
)
# 분해 의도 신호 — 있으면 GROUP BY가 정당하다
BREAKDOWN_TERMS = ("별로", "별 ", "별 분포", "각각", "나눠", "분포", "그룹", "종류별", "구분별", "따로", "집계")


def is_total_count_question(q: str) -> bool:
    """'총 몇 개' 류 = 단일 숫자 기대 (분해 요구 없음)."""
    return any(t in q for t in COUNT_TERMS) and not any(t in q for t in BREAKDOWN_TERMS)


def has_groupby(sql: str | None) -> bool:
    return bool(sql) and "GROUP BY" in sql.upper()


def repair_spurious_groupby(sql: str) -> str | None:
    """총계 질문에 붙은 군더더기 GROUP BY 제거 → `SELECT COUNT(*)` 단일 집계로 치환.

    반환 None = 수리 대상 아님(GROUP BY 없음 / SELECT 아님 / 파싱 실패).
    """
    try:
        t = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:  # noqa: BLE001 — 파싱 실패는 수리 포기(원본 유지)
        return None
    if not isinstance(t, exp.Select) or not t.args.get("group"):
        return None
    t.set("group", None)
    t.set("order", None)
    t.set("having", None)
    t.set("expressions", [exp.alias_(exp.Count(this=exp.Star()), "cnt")])
    return t.sql(dialect="postgres")


def repair_spurious_notnull(sql: str) -> str | None:
    """군더더기 `NOT properties->>'X' IS NULL` 필터 제거.

    관측(g2-0365): "회사 미팅 기록 데이터 몇 개야?"(gold 28) → solar가
    `WHERE db_name='회사 미팅 기록' AND NOT properties->>'미팅 기록' IS NULL` → 0.
    총계 질문에 NULL-존재 조건은 질문에 없는 제약이다.
    """
    try:
        t = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(t, exp.Select):
        return None
    where = t.args.get("where")
    if where is None:
        return None

    changed = False

    def strip(node):
        nonlocal changed
        # NOT (x IS NULL)  /  x IS NOT NULL  → 항상 참으로 치환
        if isinstance(node, exp.Not) and isinstance(node.this, exp.Is):
            if isinstance(node.this.expression, exp.Null):
                changed = True
                return exp.true()
        return node

    where.transform(strip, copy=False)
    if not changed:
        return None
    out = t.sql(dialect="postgres")
    try:  # 치환 후 재파싱 가능해야 게이트를 통과한다
        sqlglot.parse_one(out, dialect="postgres")
    except Exception:  # noqa: BLE001
        return None
    return out


def propose_repairs(q: str, sql: str | None) -> list[tuple[str, str]]:
    """(수리이름, 수리SQL) 후보 목록. 질문 의도에 맞는 수리만 제안한다."""
    if not sql:
        return []
    out: list[tuple[str, str]] = []
    if is_total_count_question(q):
        if (r := repair_spurious_groupby(sql)) and r != sql:
            out.append(("drop_groupby", r))
        base = out[-1][1] if out else sql
        if (r := repair_spurious_notnull(base)) and r != base:
            out.append(("drop_notnull", r))
    return out


if __name__ == "__main__":  # 스모크 — 실측 실패 SQL로 수리 동작 확인
    bad = (
        "SELECT properties ->> '이름' AS partner_name, COUNT(*) AS partner_count "
        "FROM (SELECT * FROM notion_rows WHERE scope = 'company') AS notion_rows "
        "WHERE db_name = '파트너사' GROUP BY 1 LIMIT 1000"
    )
    for name, s in propose_repairs("파트너사에는 몇 개나 들어 있어?", bad):
        print(f"[{name}] {s}")
    bad2 = (
        "SELECT COUNT(*) FROM (SELECT * FROM notion_rows WHERE scope = 'company') AS notion_rows "
        "WHERE db_name = '회사 미팅 기록' AND NOT properties ->> '미팅 기록' IS NULL LIMIT 1000"
    )
    for name, s in propose_repairs("회사 미팅 기록 데이터 몇 개야?", bad2):
        print(f"[{name}] {s}")
