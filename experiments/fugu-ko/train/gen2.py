"""D6 Phase C — 스키마 자동추출 기반 structured 문항 생성기 (gen.py 계승·확장).

D5 gen.py의 하드코딩 SCHEMA를 제거하고 **복원 DB에서 스키마를 실측 추출**한다. 학습 DB와
holdout DB를 include/exclude로 분리해 gen2(학습)와 build_unseen_v2(holdout)가 같은 기계를 쓴다.

승부처(owner 2026-07-15) = structured 단일 타깃, **hard-case(이모지/후행공백 db_name) + ambig
(count vs groupby 모호)** 집중. 라우팅은 headroom 0(G0′)이라 제외.

라벨은 스펙 기반 결정론(t3_gold.gold_numbers) — 패러프레이즈가 라벨을 훼손하지 않는다.
실행: PYTHONPATH=... FUGU_DSN=...orthus_company_0706 python train/gen2.py --validate
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
DATA = HERE / "data"
GOLDEN = ROOT / "golden"
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company_0706")

# holdout 전용 DB(학습에서 절대 제외) — 행 충분 + 후행공백 hard-case 1개(직원 ) 포함.
HELDOUT_DBS = ["참고 문서", "파트너사", "아틀라스 링크", "프로젝트 주간 노트", "직원 ", "파트너"]

_JOSA = {"[를]": ("을", "를"), "[는]": ("은", "는"), "[와]": ("과", "와"),
         "[가]": ("이", "가"), "[로]": ("으로", "로")}


def josa(text: str) -> str:
    def pick(prev: str, marker: str) -> str:
        jong, plain = _JOSA[marker]
        if not prev or not ("가" <= prev <= "힣"):
            return plain
        code = (ord(prev) - 0xAC00) % 28
        if marker == "[로]" and code == 8:
            return plain
        return jong if code else plain

    for m in _JOSA:
        while m in text:
            i = text.index(m)
            j = i - 1
            while j >= 0 and text[j] == " ":
                j -= 1
            prev = text[j] if j >= 0 else ""
            text = text[:i] + pick(prev, m) + text[i + len(m):]
    return text


def norm(q: str) -> str:
    return re.sub(r"\s+", "", q.lower())


def is_hard(db: str) -> bool:
    return bool(re.search(r"[^가-힣a-zA-Z0-9 ]", db) or db != db.strip())


def extract_schema(min_rows: int = 10, include: list[str] | None = None,
                   exclude: list[str] | None = None) -> dict[str, dict]:
    """DB에서 db_name별 groupby 가능 필드(값 2~12종, 값 커버리지 충분)와 필터 값을 실측 추출.
    SQL-안전상 작은따옴표 든 값은 제외."""
    schema: dict[str, dict] = {}
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT db_name, count(*) FROM notion_rows WHERE scope='company' "
            "GROUP BY db_name HAVING count(*)>=%s ORDER BY 2 DESC", (min_rows,))
        dbs = [r[0] for r in cur.fetchall()]
        if include:
            dbs = [d for d in dbs if d in include]
        if exclude:
            dbs = [d for d in dbs if d not in exclude]
        for db in dbs:
            cur.execute(
                "SELECT key, val, count(*) FROM notion_rows, jsonb_each_text(properties) e(key,val) "
                "WHERE scope='company' AND db_name=%s AND val<>'' GROUP BY key,val", (db,))
            byfield: dict[str, dict[str, int]] = {}
            for k, v, c in cur.fetchall():
                if "'" in v or len(v) > 40:
                    continue
                byfield.setdefault(k, {})[v] = c
            gb: dict[str, list[str]] = {}
            for k, vals in byfield.items():
                ndist = len(vals)
                covered = sum(vals.values())
                if 2 <= ndist <= 12 and covered >= 6:
                    gb[k] = sorted(vals, key=lambda x: -vals[x])
            if gb:
                schema[db] = {"gb": gb, "hard": is_hard(db)}
    return schema


# ── 표현 변형 템플릿 (gen.py 계승 + 대폭 확장) ─────────────────────────────────
T_COUNT = ["{db}에 항목이 총 몇 개야?", "{db} 행 개수 알려줘", "{db}에 등록된 건 모두 몇 건이지?",
           "{db} 전체 개수가 궁금해", "{db}에는 몇 개나 들어 있어?", "{db} 총 몇 개인지 세어줘",
           "{db} 레코드 수가 얼마나 되지?", "지금 {db}에 전부 몇 건 있어?", "{db} 규모가 몇 개 정도야?",
           "{db}에 쌓인 항목 수 좀 확인해줘", "{db} 몇 건 등록돼 있어?", "{db} 항목이 몇 개인지 알려줘",
           "{db} 데이터 몇 개야?", "{db} 전부 세면 몇 개지?"]
T_COUNT_FLT = ["{db}에서 {f}[가] {v}인 항목은 몇 개야?", "{db} 중 {f} {v}인 건 몇 건이지?",
               "{f}[가] {v}인 {db} 항목 개수 알려줘", "{db}에서 {f}[가] {v}[로] 표시된 건 총 몇 개야?",
               "{db}에 {f} {v}인 게 몇 개 있어?", "{db}에서 {f} {v}짜리 몇 건이야?",
               "{v}인 {db} 몇 개인지 세어줘", "{db} 중 {f}={v} 조건 개수 알려줘",
               "{db}에서 {f}[가] {v}인 것만 몇 건인지 확인해줘"]
T_GROUPBY = ["{db}[를] {f}별로 개수 알려줘", "{db} {f}별 분포가 어떻게 돼?",
             "{f} 기준으로 {db} 건수를 나눠서 보여줘", "{db}[는] {f}별로 몇 개씩 있어?",
             "{f}마다 {db}[가] 몇 개인지 집계해줘", "{db}의 {f}별 건수 통계 좀 보여줘",
             "{db} 항목을 {f}[로] 묶으면 각각 몇 건이야?", "{f} 값별로 {db} 개수를 정리해줘",
             "{db} {f}별로 몇 건씩인지 표로 보여줘", "{f}별 {db} 분포 집계"]
# ambig: "총 몇 개"인데 필드를 언급 → count 정답, GROUP BY 과분해 유도(A.X 약점)
T_AMBIG = ["{db}에서 {f} 구분 있잖아, 그거 다 합쳐서 총 몇 개야?",
           "{f}[가] 뭐든 상관없이 {db} 전체는 몇 건이야?",
           "{f} 나누지 말고 {db} 통째로 몇 개인지만 말해줘",
           "{db}[는] {f} 종류 상관없이 다 더하면 몇 건이지?",
           "{f}별로 말고 {db} 총합 개수만 알려줘",
           "{db}에 {f} 값이 있는 것까지 포함해서 모두 몇 개인지 알려줘",
           "{f} 상관없이 {db} 전체 건수만 딱 말해줘"]
T_COUNT_FLT2 = ["{db}에서 {f1}[가] {v1}이고 {f2}[가] {v2}인 항목 몇 개야?",
                "{db} 중 {f1} {v1}이면서 {f2} {v2}인 건 몇 건이지?",
                "{f1}[가] {v1}이고 {f2}[가] {v2}인 {db} 개수 알려줘",
                "{db}에서 {f1}={v1} 그리고 {f2}={v2} 조건 몇 개인지 세어줘"]


def gen_items(schema: dict[str, dict], *, hard_boost: int = 2, ambig_boost: int = 2,
              seed: int = 42, max_vals: int = 5) -> list[dict]:
    rng = random.Random(seed)
    rows: list[dict] = []

    def add(q, db, kind, gkey=None, where=None, tag=""):
        rows.append({"task": "structured", "q": josa(q), "spec": [db, kind, gkey, where],
                     "tag": tag, "hard": is_hard(db)})

    for db, sch in schema.items():
        boost = hard_boost if sch["hard"] else 1
        for t in rng.sample(T_COUNT, k=min(len(T_COUNT), 5 * boost)):
            add(t.format(db=db), db, "count", tag="count")
        fields = list(sch["gb"].items())
        for f, vals in fields:
            for v in vals[:max_vals]:
                for t in rng.sample(T_COUNT_FLT, k=min(3 * boost, len(T_COUNT_FLT))):
                    add(t.format(db=db, f=f, v=v), db, "count", None,
                        f"properties->>'{f}'='{v}'", tag="filter")
            for t in rng.sample(T_GROUPBY, k=min(len(T_GROUPBY), 3 * boost)):
                add(t.format(db=db, f=f), db, "groupby", f, None, tag="groupby")
            for t in rng.sample(T_AMBIG, k=min(ambig_boost * boost, len(T_AMBIG))):
                add(t.format(db=db, f=f), db, "count", None, None, tag="ambig")
        # 복합 필터(2필드 AND) — distinct spec 확대 + 모델 필터처리 변별(gold는 validate에서 비-empty만 유지)
        for (f1, v1s), (f2, v2s) in [(fields[i], fields[j])
                                     for i in range(len(fields)) for j in range(i + 1, len(fields))]:
            for v1 in v1s[:3]:
                for v2 in v2s[:3]:
                    where = f"properties->>'{f1}'='{v1}' AND properties->>'{f2}'='{v2}'"
                    for t in rng.sample(T_COUNT_FLT2, k=min(2 * boost, len(T_COUNT_FLT2))):
                        add(t.format(db=db, f1=f1, v1=v1, f2=f2, v2=v2), db, "count", None,
                            where, tag="filter2")
    return rows


def golden_norms() -> set[str]:
    out: set[str] = set()
    for name in ["t3", "t5", "t3_v2_holdout", "t5_v2_holdout"]:
        p = GOLDEN / f"{name}.json"
        if p.exists():
            out |= {norm(it["q"]) for it in json.load(open(p, encoding="utf-8"))["items"]}
    return out


def dedupe(rows: list[dict], extra_norms: set[str]) -> list[dict]:
    seen = set(extra_norms)
    out = []
    for r in rows:
        n = norm(r["q"])
        if n in seen:
            continue
        seen.add(n)
        out.append(r)
    return out


def validate_specs(rows: list[dict]) -> list[dict]:
    from t3_gold import gold_numbers
    ok, dropped, cache = [], 0, {}
    for r in rows:
        key = tuple(r["spec"])
        if key not in cache:
            try:
                g = gold_numbers(*r["spec"])
                cache[key] = bool(g) and g != {0}
            except Exception:  # noqa: BLE001
                cache[key] = False
        if cache[key]:
            ok.append(r)
        else:
            dropped += 1
    print(f"  스펙 검증: 유지 {len(ok)} / 제외 {dropped}")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--min-rows", type=int, default=10)
    args = ap.parse_args()

    schema = extract_schema(min_rows=args.min_rows, exclude=HELDOUT_DBS)
    print(f"[schema] 학습 DB {len(schema)}개 (held-out {len(HELDOUT_DBS)}개 제외): "
          + ", ".join(f"{d}{'*' if s['hard'] else ''}" for d, s in schema.items()))
    rows = gen_items(schema)
    rows = dedupe(rows, golden_norms())
    if args.validate:
        rows = validate_specs(rows)
    rng = random.Random(42)
    rng.shuffle(rows)
    for i, r in enumerate(rows):
        r["id"] = f"g2-{i:04d}"
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "questions2.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    tags: dict[str, int] = {}
    for r in rows:
        tags[r["tag"]] = tags.get(r["tag"], 0) + 1
    hard = sum(1 for r in rows if r["hard"])
    print(f"[gen2] {len(rows)}문항 → data/questions2.jsonl · 유형 {tags} · hard-case {hard}")


if __name__ == "__main__":
    main()
