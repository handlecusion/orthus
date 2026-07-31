"""D7 — fresh 판정셋 생성 (prereg §10.3 명세 구현).

동결 명세: 학습 6종·구홀드아웃 6종과 무겹침 DB(미사용 실DB ≥3행 + 합성 2종), 신규 질문
템플릿만(기존 gen2.T_*와 템플릿 문자열 중복 0), n≥900, 정답 0 문항 ~10%, gold는 결정론
실행 산출·동결.

합성 2종(합성임을 명시 — 워크스페이스 버릇 반영: 끝공백·이모지·특수문자 값):
  '채용 파이프라인 '(끝공백, 18행) · '🎬 촬영 장비 대여'(이모지, 15행)
격리 DB(orthus_company_0706)에 db_id='synthetic-fresh-*'로 멱등 삽입. 운영 무접촉.

실행: python train/build_fresh_holdout.py            # → golden/t3_fresh_holdout.json
      python train/build_fresh_holdout.py --wipe     # 합성 DB 제거
"""

from __future__ import annotations

import argparse
import json
import random
import hashlib
import sys
import uuid
from collections import Counter
from pathlib import Path

import sqlalchemy as sa

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from gen2 import josa  # noqa: E402 — 조사 처리만 재사용(템플릿은 전부 신규)

GOLDEN = ROOT / "golden"
DSN = "postgresql://orthus:orthus@localhost:5433/orthus_company_0706"
SEED = 20260716
_engine = sa.create_engine(DSN)

TRAIN_DBS = {"🎯 NOVA 영입 후보 리스트", "협업업무표", "Nova 개발 로드맵", "배포 공지", "AI관련툴", "회사 미팅 기록"}
OLD_HELD_DBS = {"참고 문서", "파트너사", "아틀라스 링크", "프로젝트 주간 노트", "직원 ", "파트너"}
MIN_ROWS = 3
MAX_REAL_DBS = 8
TARGET_ZERO_FRAC = 0.10

# ── 신규 템플릿 (기존 gen2.T_*와 문자열 중복 0 — 생성 시 assert로 검증) ──────────
T_COUNT_F = [
    "{db} 목록에 줄이 전부 몇 개나 있는지 궁금해",
    "{db} 안에 든 항목 숫자만 딱 알려줘",
    "{db} 테이블 크기가 몇 건이나 되는지 확인 부탁해",
    "{db}[에] 기록된 게 통틀어 몇 개인지 볼 수 있을까?",
    "우리 {db} 지금까지 몇 건 쌓였어?",
    "{db} 항목 총원이 몇이야?",
    "{db}[를] 전부 합치면 몇 건 나와?",
    "{db} 건수 현황 하나만 알려줄래?",
]
T_FLT_F = [
    "{db} 중에서 {f} 값이 {v}[로] 잡힌 건 몇 건이나 돼?",
    "{f}[가] {v}[로] 들어간 {db}[가] 몇 개인지 궁금해",
    "{db}에서 {f}[이] {v}인 케이스만 골라서 세어봐 줘",
    "{v} 상태의 {f}[를] 가진 {db} 건수 알려줄래?",
    "{db} 가운데 {f}: {v} 항목이 몇 개인지 확인해 줘",
    "{f}[가] {v}[에] 해당하는 {db}[는] 전부 몇 건이야?",
    "{db} 리스트에서 {f}[이] {v}인 줄 수를 세어줘",
    "{f} 필드가 {v}인 {db} 레코드가 몇 개나 있는지 알려줘",
]
T_GB_F = [
    "{db}[를] {f} 값 단위로 쪼개서 각 몇 건인지 보여줄래?",
    "{f} 종류마다 {db}[가] 얼마나 되는지 세어봐 줘",
    "{db} 현황을 {f} 기준 그룹으로 나눠 개수 좀 뽑아줘",
    "{f}에 따라 {db} 건수가 어떻게 갈리는지 알려줘",
    "{db}에서 {f}별 개수 내역서 좀 보여줘",
]
T_AMBIG_F = [
    "{f} 같은 건 신경 쓰지 말고 {db}[가] 통틀어 몇 건인지만 답해줘",
    "{db}[에] {f} 값이 뭐가 됐든 전체 합계 건수 하나만 줘",
    "{f}[로] 쪼개지 말고 {db} 총 건수를 숫자 하나로 알려줘",
    "{db} 말이야, {f} 구분은 됐고 전부 몇 개인지만 궁금해",
]
T_FLT2_F = [
    "{db} 중에서 {f1}[가] {v1}이고 동시에 {f2}[가] {v2}인 건 몇 건이나 돼?",
    "{f1} {v1}, {f2} {v2} 둘 다 만족하는 {db} 개수 좀 세어줘",
    "{db}에서 {f1}[이] {v1}이면서 {f2}[도] {v2}인 항목 수 알려줄래?",
    "{f1}={v1} 그리고 {f2}={v2}[를] 동시에 충족하는 {db}[는] 몇 개야?",
]
# 정답 0 문항용 부재 값 (실DB 값과 충돌하지 않게 생성 시 실재 여부 검사)
ABSENT_VALUES = ["보류(외부 검토)", "폐기 예정", "임시저장", "미배정", "검수 반려", "이관 완료"]

SYNTHETIC = {
    "채용 파이프라인 ": {  # 끝공백 — 워크스페이스 버릇
        "rows": 18,
        "fields": {
            "이름": None,  # unique
            "포지션": ["그래픽스 엔지니어", "AI 리서처", "프로덕트 디자이너"],
            "상태": ["서류 검토", "1차 면접", "최종 합격(처우 협의)", "보류"],
            "소스": ["리크루터", "사내 추천", "채용 공고"],
        },
    },
    "🎬 촬영 장비 대여": {  # 이모지
        "rows": 15,
        "fields": {
            "장비명": None,
            "구분": ["카메라", "조명", "오디오·모니터"],
            "상태": ["대여 중", "반납 완료", "수리(점검)"],
            "담당 팀": ["영상팀", "제작팀"],
        },
    },
}


def upsert_synthetic(rng: random.Random) -> None:
    """합성 DB 멱등 삽입 (db_id prefix 'synthetic-fresh-')."""
    with _engine.begin() as c:
        uid = c.execute(sa.text("SELECT user_id FROM notion_rows LIMIT 1")).scalar()
        for db, spec in SYNTHETIC.items():
            db_id = "synthetic-fresh-" + hashlib.sha1(db.encode()).hexdigest()[:12]
            n = c.execute(sa.text("SELECT count(*) FROM notion_rows WHERE db_id=:d"), {"d": db_id}).scalar()
            if n == spec["rows"]:
                continue
            c.execute(sa.text("DELETE FROM notion_rows WHERE db_id=:d"), {"d": db_id})
            for i in range(spec["rows"]):
                props = {}
                for f, vals in spec["fields"].items():
                    props[f] = f"{f} 샘플-{i + 1:02d}" if vals is None else rng.choice(vals)
                c.execute(
                    sa.text(
                        "INSERT INTO notion_rows (row_id, db_id, db_name, properties, scope, user_id, updated_at, created_at, project) "
                        "VALUES (:rid, :did, :dn, CAST(:props AS jsonb), 'company', :uid, now(), now(), 'company')"
                    ),
                    {"rid": str(uuid.uuid4()), "did": db_id, "dn": db, "props": json.dumps(props, ensure_ascii=False), "uid": str(uid)},
                )
            print(f"[synthetic] {db!r} {spec['rows']}행 삽입")


def wipe_synthetic() -> None:
    with _engine.begin() as c:
        n = c.execute(sa.text("DELETE FROM notion_rows WHERE db_id LIKE 'synthetic-fresh-%'")).rowcount
    print(f"[wipe] 합성 행 {n}건 제거")


def fresh_schema() -> dict[str, dict]:
    """fresh DB → {field: {value: count}} (필터 가능 필드만)."""
    with _engine.connect() as c:
        rows = c.execute(sa.text("SELECT db_name, properties FROM notion_rows WHERE scope='company'")).fetchall()
    by_db: dict[str, list[dict]] = {}
    for n, p in rows:
        if n in TRAIN_DBS or n in OLD_HELD_DBS or not isinstance(p, dict):
            continue
        by_db.setdefault(n, []).append(p)
    out: dict[str, dict] = {}
    real = 0
    for n, ps in sorted(by_db.items(), key=lambda x: -len(x[1])):
        if len(ps) < MIN_ROWS:
            continue
        if n not in SYNTHETIC:
            if real >= MAX_REAL_DBS:
                continue
            real += 1
        fields: dict[str, Counter] = {}
        for p in ps:
            for k, v in p.items():
                if isinstance(v, str) and v.strip() and len(v) <= 45:
                    fields.setdefault(k, Counter())[v] += 1
        out[n] = {"total": len(ps), "fields": {k: dict(c) for k, c in fields.items()}}
    return out


def gold_exec(db: str, kind: str, gkey, where) -> list[int]:
    esc = db.replace("'", "''")
    w = f"scope='company' AND db_name = '{esc}'" + (f" AND {where}" if where else "")
    with _engine.connect() as c:
        if kind == "count":
            return [int(c.execute(sa.text(f"SELECT count(*) FROM notion_rows WHERE {w}")).scalar())]
        g = str(gkey).replace("'", "''")
        rs = c.execute(sa.text(f"SELECT count(*) FROM notion_rows WHERE {w} GROUP BY properties->>'{g}'")).fetchall()
        return sorted({int(r[0]) for r in rs})


def flt(f: str, v: str) -> str:
    return f"properties->>'{f.replace(chr(39), chr(39) * 2)}'='{v.replace(chr(39), chr(39) * 2)}'"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wipe", action="store_true")
    a = ap.parse_args()
    if a.wipe:
        wipe_synthetic()
        return

    # 템플릿 신규성 검증 (prereg §10.3 — 기존 템플릿과 문자열 중복 0)
    import gen2

    old = set(gen2.T_COUNT + gen2.T_COUNT_FLT + gen2.T_GROUPBY + gen2.T_AMBIG + gen2.T_COUNT_FLT2)
    new = set(T_COUNT_F + T_FLT_F + T_GB_F + T_AMBIG_F + T_FLT2_F)
    assert not (old & new), f"기존 템플릿과 중복: {old & new}"

    rng = random.Random(SEED)
    upsert_synthetic(rng)
    schema = fresh_schema()
    print(f"[fresh] DB {len(schema)}종: {list(schema)}")

    items: list[dict] = []

    def add(q: str, db: str, kind: str, gkey, where, tag: str, zero: bool = False) -> None:
        gold = [0] if zero else gold_exec(db, kind, gkey, where)
        if not zero and (not gold or gold == [0]):
            return  # 비-zero 문항의 gold가 비거나 0이면 스펙 오류 — 제외
        items.append({"task": "structured", "q": josa(q), "spec": [db, kind, gkey, where],
                      "tag": ("zero" if zero else tag), "gold": gold})

    for db, sch in schema.items():
        fields = sch["fields"]
        # count — 전 템플릿
        for t in T_COUNT_F:
            add(t.format(db=db), db, "count", None, None, "count")
        # ambig / groupby — 값이 2종 이상인 필드
        gb_fields = [f for f, vs in fields.items() if len(vs) >= 2][:3]
        for f in gb_fields:
            for t in T_AMBIG_F:
                add(t.format(db=db, f=f), db, "count", None, None, "ambig")
            for t in T_GB_F:
                add(t.format(db=db, f=f), db, "groupby", f, None, "groupby")
        # filter — (필드,값) 조합 최대 6개 × 템플릿 4개
        pairs = [(f, v) for f, vs in fields.items() for v in list(vs)[:3]]
        rng.shuffle(pairs)
        for f, v in pairs[:10]:
            for t in rng.sample(T_FLT_F, k=6):
                add(t.format(db=db, f=f, v=v), db, "count", None, flt(f, v), "filter")
        # filter2 — 서로 다른 필드 2쌍 × 템플릿 3개
        f2 = [(f1, v1, f2_, v2) for i, (f1, v1) in enumerate(pairs) for f2_, v2 in pairs[i + 1:] if f1 != f2_]
        rng.shuffle(f2)
        for f1, v1, f2_, v2 in f2[:6]:
            for t in T_FLT2_F:
                add(t.format(db=db, f1=f1, v1=v1, f2=f2_, v2=v2), db, "count", None,
                    flt(f1, v1) + " AND " + flt(f2_, v2), "filter2")
        # zero — DB에 없는 값 (실재 검사 후) × 필드 2개 × 템플릿 3개
        zf = [f for f in fields][:3]
        for f in zf:
            v = next((x for x in ABSENT_VALUES if x not in fields[f]), None)
            if v is None:
                continue
            for t in rng.sample(T_FLT_F, k=3):
                add(t.format(db=db, f=f, v=v), db, "count", None, flt(f, v), "filter", zero=True)

    # dedupe(질문 정규화) + id 부여
    seen: set[str] = set()
    final: list[dict] = []
    for it in items:
        k = it["q"].replace(" ", "")
        if k in seen:
            continue
        seen.add(k)
        it["id"] = f"f3-{len(final):04d}"
        final.append(it)

    zc = sum(1 for it in final if it["tag"] == "zero")
    out = {"task": "T3_fresh", "desc": "D7 fresh 판정셋 — prereg §10.3 (신규 DB+신규 템플릿+정답0 포함)",
           "fresh_dbs": list(schema), "synthetic_dbs": list(SYNTHETIC), "seed": SEED, "items": final}
    GOLDEN.mkdir(exist_ok=True)
    (GOLDEN / "t3_fresh_holdout.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[fresh] {len(final)}문항 (zero {zc} = {zc / len(final) * 100:.0f}%) → golden/t3_fresh_holdout.json")
    print(f"[fresh] tag: {Counter(it['tag'] for it in final)}")
    print(f"[fresh] DB별: {Counter(it['spec'][0] for it in final)}")


if __name__ == "__main__":
    main()
