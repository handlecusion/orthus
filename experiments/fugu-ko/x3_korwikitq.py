"""B4-X3 — KorWikiTQ 한국어 스키마 게이트 통과율 (standalone runner).

`analysis/b4-prereg.md` §3(X3) + `analysis/x0-external-dataset-plan.md` §3.4.

무엇을 재나:
  KorWikiTQ(LG-NLP, CC-BY-SA-4.0)의 한국어 위키 표를 `notion_rows` JSONB 로우스토어에
  적재하고(한국어 헤더 = properties 키 = 스키마 링킹 대상), 프로덕션 `query_structured`
  compile+게이트를 그대로 태워 **게이트 통과율**(parse · SELECT-only · schema_ok ·
  read_only · LIMIT · EXPLAIN)을 잰다. gold SQL 불필요(프리레그 X3). 부수적으로 답 문자열이
  결과 셀에 나타나는지(**근사** 실행일치, 손저술 gold 아님)를 함께 본다.

격리 (하드 제약):
  - 자체 스크래치 DB `orthus_x3`에만 적재한다. B1이 읽는 `orthus_company_0706`을 건드리지 않는다.
  - `.env`를 수정하지 않는다 — 이 프로세스의 os.environ만 orthus_x3로 돌린다(DSN 오버라이드).
    orthus 임포트 전에 세팅해야 `get_settings()`(lru_cached)가 orthus_x3로 해석된다.
  - `ax`(A.X) 미사용, Bedrock 미사용. solar / gpt-4o-mini 만.
  - 외부 데이터 행은 커밋하지 않는다(gitignored .cache). 라이선스: CC-BY-SA-4.0(§6 사내 평가 한정).

실행:
  .venv/bin/python experiments/fugu-ko/x3_korwikitq.py --n 200 --models solar,gpt
  .venv/bin/python experiments/fugu-ko/x3_korwikitq.py --internal --models solar  # 내부 부호대조
  .venv/bin/python experiments/fugu-ko/x3_korwikitq.py --score-only --models solar,gpt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CACHE = HERE / "external" / ".cache" / "raw" / "korwikitq"
DEV_JSON = CACHE / "KorWikiTQ_ko_dev.json"
SEL_JSON = HERE / "external" / ".cache" / "korwikitq_selection.jsonl"  # gitignored
RAW = HERE / "analysis" / "raw"

X3_DSN = "postgresql+psycopg://orthus:orthus@localhost:5433/orthus_x3"
X3_DSN_RAW = "postgresql://orthus:orthus@localhost:5433/orthus_x3"
INTERNAL_DSN = "postgresql+psycopg://orthus:orthus@localhost:5433/orthus_company"
INTERNAL_DSN_RAW = "postgresql://orthus:orthus@localhost:5433/orthus_company"

UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SEED = 1234


# ─────────────────────────────────────────────────────────────── selection

def _headers_ok(headers: list) -> bool:
    hs = [str(h).strip() for h in headers]
    return (
        len(hs) >= 3
        and all(hs)  # 빈 헤더 없음
        and len(set(hs)) == len(hs)  # 유니크(JSONB dict 붕괴 방지)
        and all(len(h) <= 40 for h in hs)
    )


def select_items(n: int) -> list[dict]:
    """dev에서 결정론(seed) 슬라이스. 표당 최대 2문항, >=3열 >=4행 유니크헤더."""
    import random

    data = json.loads(DEV_JSON.read_text(encoding="utf-8"))["data"]
    pool = []
    for e in data:
        tbl = e.get("TBL") or []
        if len(tbl) < 5:  # 헤더 + >=4 데이터행
            continue
        headers, body = tbl[0], tbl[1:]
        if not _headers_ok(headers):
            continue
        qas = e.get("QAS") or {}
        q, a = str(qas.get("question", "")).strip(), str(qas.get("answer", "")).strip()
        if not q or not a:
            continue
        pool.append(
            {
                "qid": qas.get("qid"),
                "title": str(e.get("T", "")).strip(),
                "question": q,
                "answer": a,
                "headers": [str(h).strip() for h in headers],
                "body": [[str(c) for c in row] for row in body],
            }
        )
    rng = random.Random(SEED)
    rng.shuffle(pool)
    per_table: dict[str, int] = {}
    chosen: list[dict] = []
    for item in pool:
        t = item["title"]
        if per_table.get(t, 0) >= 2:
            continue
        per_table[t] = per_table.get(t, 0) + 1
        item["id"] = f"x3-{len(chosen) + 1:03d}"
        chosen.append(item)
        if len(chosen) >= n:
            break
    return chosen


def write_selection(items: list[dict]) -> None:
    SEL_JSON.parent.mkdir(parents=True, exist_ok=True)
    with SEL_JSON.open("w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")


def load_selection() -> list[dict]:
    return [json.loads(x) for x in SEL_JSON.read_text(encoding="utf-8").splitlines() if x.strip()]


# ─────────────────────────────────────────────────────────────── db load

def _db_slug(title: str, idx: int) -> str:
    # db_name은 그대로 한국어 표 제목을 쓴다(스키마 링킹 대상). 너무 길면 자른다.
    name = re.sub(r"\s+", " ", title).strip()
    return (name[:60] or f"표{idx}")


def load_table(item: dict) -> None:
    """이 표 하나만 orthus_x3.notion_rows에 적재(단일-표 스코프 = 순수 컬럼 스키마 링킹)."""
    import psycopg

    db_name = item["db_name"]
    with psycopg.connect(X3_DSN_RAW) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE notion_rows")
        for row in item["body"]:
            props = {h: (row[i] if i < len(row) else None) for i, h in enumerate(item["headers"])}
            cur.execute(
                "INSERT INTO notion_rows(row_id, db_id, db_name, properties, scope, user_id, project)"
                " VALUES (%s, %s, %s, %s::jsonb, 'company', %s, 'company')",
                (
                    str(uuid.uuid4()),
                    "korwikitq",
                    db_name,
                    json.dumps(props, ensure_ascii=False),
                    str(UID),
                ),
            )
        conn.commit()


# ─────────────────────────────────────────────────────────────── run

_KEY_RE = re.compile(r"properties\s*->>?\s*'([^']+)'")


def _referenced_keys(sql: str | None) -> list[str]:
    """SQL이 읽은 JSONB 키들(`properties->>'X'`). schema_ok는 이 키를 데이터로 취급해
    존재 검증을 안 하므로, 한국어 컬럼 링킹의 실제 정확도는 여기서 별도로 잰다."""
    if not sql:
        return []
    return sorted(set(_KEY_RE.findall(sql)))


def _schema_link_ok(sql: str | None, headers: list[str]) -> bool | None:
    """SQL이 참조한 모든 한국어 키가 실제 표 헤더인가(존재하면). 참조 키 0개면 None."""
    keys = _referenced_keys(sql)
    if not keys:
        return None
    hset = set(headers)
    return all(k in hset for k in keys)


def _answer_hit(answer: str, rows) -> bool:
    """근사 실행일치: 정규화한 gold 답이 결과 셀 중 하나에 (부분)일치하나. 손저술 gold 아님."""
    def norm(s: str) -> str:
        return re.sub(r"\s+", "", str(s)).lower()

    a = norm(answer)
    if not a:
        return False
    for row in rows or []:
        for cell in row:
            c = norm(cell)
            if not c:
                continue
            if a == c or a in c or c in a:
                return True
    return False


def run_model(slug: str, chat, items: list[dict], out_path: Path) -> None:
    from orthus.structured.query import query_structured

    with out_path.open("w", encoding="utf-8") as fh:
        for i, item in enumerate(items, 1):
            load_table(item)
            t0 = time.monotonic()
            try:
                r = query_structured(UID, item["question"], scope="company", chat_model=chat)
                rec = {
                    "id": item["id"],
                    "qid": item.get("qid"),
                    "db_name": item["db_name"],
                    "n_cols": len(item["headers"]),
                    "n_rows": len(item["body"]),
                    "status": r.status,
                    "gate_passed": bool(r.validation.passed),
                    "reject": r.validation.rejected_reason,
                    "sql": (r.compiled.sql if r.compiled else None),
                    "row_count": r.row_count,
                    "ref_keys": _referenced_keys(r.compiled.sql if r.compiled else None),
                    "schema_link_ok": _schema_link_ok(
                        r.compiled.sql if r.compiled else None, item["headers"]
                    ),
                    "answer_hit": _answer_hit(item["answer"], r.rows),
                    "ms": int((time.monotonic() - t0) * 1000),
                }
            except Exception as e:  # noqa: BLE001
                rec = {
                    "id": item["id"],
                    "db_name": item["db_name"],
                    "status": "error",
                    "gate_passed": False,
                    "error": f"{type(e).__name__}: {str(e)[:140]}",
                    "ms": int((time.monotonic() - t0) * 1000),
                }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            if i % 10 == 0 or i == len(items):
                print(f"  {slug:11} {i:3}/{len(items)}", flush=True)


def raw_path(slug: str, internal: bool) -> Path:
    return RAW / (f"x3_internal_{slug}.jsonl" if internal else f"x3_{slug}.jsonl")


# ─────────────────────────────────────────────────────────────── internal

def run_internal(slug: str, chat, out_path: Path) -> None:
    """내부 t3 골든을 orthus_company에 태워 게이트 통과율(부호대조 기준선)."""
    from orthus.structured.query import query_structured

    items = json.loads((HERE / "golden" / "t3.json").read_text(encoding="utf-8"))["items"]
    holdout = json.loads((HERE / "golden" / "t3_holdout.json").read_text(encoding="utf-8"))["items"]
    items = items + holdout
    with out_path.open("w", encoding="utf-8") as fh:
        for i, item in enumerate(items, 1):
            t0 = time.monotonic()
            try:
                r = query_structured(UID, item["q"], scope="company", chat_model=chat)
                rec = {
                    "id": item["id"],
                    "status": r.status,
                    "gate_passed": bool(r.validation.passed),
                    "reject": r.validation.rejected_reason,
                    "row_count": r.row_count,
                    "ms": int((time.monotonic() - t0) * 1000),
                }
            except Exception as e:  # noqa: BLE001
                rec = {"id": item["id"], "status": "error", "gate_passed": False,
                       "error": f"{type(e).__name__}: {str(e)[:140]}"}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            if i % 10 == 0 or i == len(items):
                print(f"  internal {slug:8} {i:3}/{len(items)}", flush=True)


# ─────────────────────────────────────────────────────────────── score

def score(models: list[str], internal: bool) -> None:
    print("\n  ── 게이트 통과율 ──")
    for m in models:
        p = raw_path(m, internal)
        if not p.exists():
            print(f"  ! {m}: raw 없음 ({p.name})")
            continue
        recs = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        n = len(recs)
        gate = sum(1 for r in recs if r.get("gate_passed"))
        execed = sum(1 for r in recs if r.get("status") == "executed")
        errs = sum(1 for r in recs if r.get("status") == "error")
        hit = sum(1 for r in recs if r.get("answer_hit"))
        sl = [r.get("schema_link_ok") for r in recs if r.get("schema_link_ok") is not None]
        sl_ok = sum(1 for v in sl if v)
        rejects: dict[str, int] = {}
        for r in recs:
            if not r.get("gate_passed"):
                key = str(r.get("reject") or r.get("error") or "unknown").split(":")[0]
                rejects[key] = rejects.get(key, 0) + 1
        line = (
            f"  {m:11} n={n:3}  gate_pass {gate}/{n} ({gate / n * 100:5.1f}%)  "
            f"executed {execed}/{n} ({execed / n * 100:5.1f}%)  err {errs}"
        )
        if not internal:
            sl_den = len(sl) or 1
            line += (
                f"  schema_link {sl_ok}/{len(sl)} ({sl_ok / sl_den * 100:5.1f}%)"
                f"  answer_hit {hit}/{n} (~{hit / n * 100:.0f}%)"
            )
        print(line)
        if rejects:
            print(f"              게이트 실패 사유: {dict(sorted(rejects.items(), key=lambda x: -x[1]))}")


# ─────────────────────────────────────────────────────────────── main

def build_chat(slug: str):
    from pool import WorkerChat, WorkerSpec, build_pool

    if slug == "solar":
        return build_pool(["solar"])["solar"]
    if slug in ("gpt", "gpt-4o-mini", "baseline"):
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("OPENAI_API_KEY 없음 — gpt-4o-mini 실행 불가")
        return WorkerChat(
            WorkerSpec("gpt4omini", "", "https://api.openai.com/v1", "gpt-4o-mini", timeout=60),
            key,
            "gpt-4o-mini",
        )
    raise SystemExit(f"허용되지 않은 모델: {slug} (solar/gpt만; ax·bedrock 금지)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--models", default="solar")
    ap.add_argument("--internal", action="store_true", help="내부 t3 골든 게이트통과(orthus_company)")
    ap.add_argument("--score-only", action="store_true")
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    # DSN 오버라이드: .env는 건드리지 않고 이 프로세스만 스크래치 DB로. orthus 임포트 전에!
    dsn = INTERNAL_DSN if args.internal else X3_DSN
    os.environ["ORTHUS_PG_DSN"] = dsn
    os.environ["ORTHUS_PG_DSN_READONLY"] = dsn
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(REPO))
    RAW.mkdir(parents=True, exist_ok=True)

    if args.score_only:
        score(models, args.internal)
        return

    if args.internal:
        for slug in models:
            chat = build_chat(slug)
            print(f"\n== internal t3 게이트통과 · {slug} · {dsn.rsplit('/', 1)[-1]} ==")
            run_internal(slug, chat, raw_path(slug, True))
        score(models, True)
        return

    # 외부 X3
    if SEL_JSON.exists():
        items = load_selection()
        print(f"기존 선택 재사용: {len(items)} items")
    else:
        items = select_items(args.n)
        write_selection(items)
        print(f"신규 선택: {len(items)} items (seed={SEED}, 표당<=2, >=3열>=4행)")
    # db_name 부여
    for idx, it in enumerate(items, 1):
        it["db_name"] = _db_slug(it["title"], idx)

    sha = hashlib.sha256(DEV_JSON.read_bytes()).hexdigest()
    print(f"KorWikiTQ dev sha256={sha[:16]}…  n_selected={len(items)}")

    for slug in models:
        try:
            chat = build_chat(slug)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {slug} 스킵: {e}")
            continue
        print(f"\n== KorWikiTQ X3 · {slug} · orthus_x3 ==")
        run_model(slug, chat, items, raw_path(slug, False))
    score(models, False)


if __name__ == "__main__":
    main()
