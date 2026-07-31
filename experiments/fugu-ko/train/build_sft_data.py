"""D7 — SFT 워커 학습 데이터 생성 (API 0콜, 결정론).

labeled2.jsonl 3,000문항의 spec에서 **정답 SQL을 결정론 생성**하고, 입력은 배포 경로
(`orthus.assistant.compile.compile_query`)가 워커에게 주는 (system, user) 프롬프트와
**동일 포맷**으로 만든다. 타깃은 compile이 파싱하는 것과 동일한 `{"sql": "..."}` JSON.

핵심 설계 — **스키마 증강(schema augmentation):**
전 예시가 똑같은 스키마 문자열이면 모델이 스키마를 무시하고 질문만 외운다. 예시마다
카탈로그의 DB 목록을 (타깃 DB 포함) 무작위 부분집합·순서로 바꿔 넣어 "스키마에서 db_name을
찾아 **글자 그대로**(공백까지) 복사"하는 행동을 강제한다 — 이것이 새 DB 전이의 메커니즘이다.

**v2 함정 클래스 증강 (2026-07-16, 홀드아웃 프리뷰 실패 분석 반영):**
v1 홀드아웃 프리뷰에서 SFT 오답 232건 중 100건이 `'직원 '`(끝공백 DB) 단일 클래스,
다수가 필터값 변형 환각('과거자료'→'과거의 자료')이었다. 원인 = 학습 분포에 그 클래스가
0건. 워크스페이스 실태(미사용 DB명 `'월간 목표 '`, `'벤치마킹 회사 '`, nbsp, 이모지)를
반영해 **결정론 변이 증강**을 추가한다 — 홀드아웃 DB/문항은 여전히 무접촉:
  (a) db_name 변이: 타깃 DB명에 끝공백/nbsp/이모지 접두를 붙인 가상 변형을 스키마+질문+
      타깃 SQL에 일관 적용. 절반은 clean 이름도 스키마에 함께 넣어(짝 함정) "질문 문자열과
      가장 길게 정확히 일치하는 스키마 항목을 복사"를 가르친다.
  (b) 필터값 변이: 질문·타깃의 필터값을 특수문자/무공백 합성어로 치환("질문의 값을 글자
      그대로 복사" 강화). DB에 없는 값이므로 정답이 0이어도 SQL 자체는 옳다 — gold 실행
      검증은 이 부류에 한해 건너뛴다(copy-discipline 교육이 목적).

정답 SQL 규칙(spec = [db, kind, gkey, where], t3_gold.gold_numbers와 동일 의미):
  kind='count' → SELECT COUNT(*) FROM notion_rows WHERE db_name = '<db>' [AND <where>]
  kind=groupby → SELECT properties->>'<gkey>' AS k, COUNT(*) FROM notion_rows
                 WHERE db_name = '<db>' GROUP BY 1
(scope 필터는 서버가 실행 직전 결정론 재작성으로 주입하므로 워커 SQL엔 넣지 않는다 —
 실워커들의 원 출력과 동일 계약.)

무결성 게이트: 생성된 정답 SQL을 격리 DB에서 실행해 number-set이 gold와 일치하는지
전수 검증한다. 불일치 spec은 제외하고 개수를 보고한다.

실행: python train/build_sft_data.py            # → data/sft_train.jsonl / sft_val.jsonl
"""

from __future__ import annotations

import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

import sqlalchemy as sa

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from t3_gold import model_numbers  # noqa: E402
from orthus.assistant.compile import _SYSTEM, _render_catalog  # noqa: E402
from orthus.structured.query import _COLUMNS  # noqa: E402

DATA = HERE / "data"
def _dsn() -> str:
    """드라이버 비종속 DSN — venv에 psycopg2/psycopg 중 뭐가 있든 붙는다(T15)."""
    import importlib.util as _u

    driver = "psycopg2" if _u.find_spec("psycopg2") else "psycopg"
    return f"postgresql+{driver}://orthus:orthus@localhost:5433/orthus_company_0706"


DSN = _dsn()
SEED = 42
VAL_FRAC = 0.05
MIN_DBS_IN_SCHEMA = 4  # 증강 시 카탈로그에 남길 최소 DB 수(타깃 포함)

_engine = sa.create_engine(DSN)


def load_db_keys() -> dict[str, list[str]]:
    """db_name → 정렬된 properties 키 목록 (build_notion_catalog와 동일 소스)."""
    out: dict[str, set[str]] = {}
    with _engine.connect() as c:
        for db, props in c.execute(
            sa.text("SELECT db_name, properties FROM notion_rows WHERE scope='company'")
        ):
            keys = out.setdefault(db, set())
            if isinstance(props, dict):
                keys.update(props.keys())
    return {k: sorted(v) for k, v in out.items()}


def render_user(question: str, db_subset: dict[str, list[str]]) -> str:
    """compile_query의 user 프롬프트와 동일 포맷 — 카탈로그 DB 목록만 부분집합."""
    parts = [f"{name} [{', '.join(keys)}]" for name, keys in db_subset.items()]
    description = (
        "Generic JSONB row store of Notion DB rows. Filter rows by `db_name`; "
        "read row fields from the JSONB `properties` column with "
        "`properties->>'<key>'`. Databases and their property keys: " + "; ".join(parts)
    )
    catalog = {"tables": {"notion_rows": {"columns": dict(_COLUMNS), "description": description}}}
    return (
        f"Question: {question}\n\n"
        f"Schema:\n{_render_catalog(catalog)}\n\n"
        f"Business/wiki grounding:\n(no wiki grounding)"
    )


def canonical_sql(spec: list) -> str:
    db, kind, gkey, where = spec
    esc = db.replace("'", "''")
    w = f"db_name = '{esc}'" + (f" AND {where}" if where else "")
    if kind == "count":
        return f"SELECT COUNT(*) AS cnt FROM notion_rows WHERE {w}"
    gesc = str(gkey).replace("'", "''")
    return (
        f"SELECT properties->>'{gesc}' AS k, COUNT(*) AS cnt "
        f"FROM notion_rows WHERE {w} GROUP BY 1"
    )


def verify_sql(sql: str, gold: list[int]) -> bool:
    """정답 SQL이 실제로 gold number-set을 내는지 격리 DB에서 검증 (scope 필터 수동 부여)."""
    scoped = sql.replace(
        "FROM notion_rows", "FROM (SELECT * FROM notion_rows WHERE scope='company') AS notion_rows", 1
    )
    try:
        with _engine.connect() as c:
            rows = [list(r) for r in c.execute(sa.text(scoped)).fetchall()]
    except Exception:  # noqa: BLE001
        return False
    return bool(gold) and set(gold) <= model_numbers(rows)


NAME_MUT_P = 0.35   # (a) db_name 변이 비율
VAL_MUT_P = 0.25    # (b) 필터값 변이 비율
SUFFIXES = [" ", " ", "  "]
EMOJIS = ["🎯 ", "📌 ", "🗂️ ", "🤝 "]
VAL_MUTS = [  # 워크스페이스풍 특수문자 값 (질문·SQL에 그대로 복사돼야 함)
    "정책&기획", "과거자료", "AI·에이전트", "보조출연(영화)", "스토어/콘솔",
    "R&D 계획", "1차·2차 검수", "미팅(외부)", "계약 완료(선금)", "온보딩·교육",
]


SIB_SUFFIXES = ["사", " 기록", " 리스트", "스", " 관리"]  # v3: 파트너↔파트너사류 혼동쌍
PAIR_SIB_P = 0.30   # (c) 접두 혼동쌍(sibling) 트랩 비율 — v3
RECIPE = "v2"       # v2(실측 최선) | v3(대칭변이+혼동쌍) | v4(v2+count/ambig 표현 다양화)

# v4 — count/ambig 표현 다양화(유령 필터 교정). 조사 마커를 변수 뒤에 두지 않는다(E12 교훈).
V4_COUNT = [
    "{db} 총 건수 좀 뽑아줄래?", "{db}에 지금 몇 건이나 존재하는지 알려줘",
    "{db} 행이 몇 줄인지만 말해줘", "{db} 전체 레코드 숫자 알려줘",
    "{db} 크기 확인해줘 — 몇 건이야?", "{db} 다 합쳐서 몇 개인지 계산해줘",
    "{db}의 총 개수는?", "{db}에 담긴 데이터 양이 몇 건이지?",
    "{db} 규모 파악하게 총 건수 알려줘", "조건 없이 {db} 전부 몇 건인지 세어줘",
]
V4_AMBIG = [
    "{f} 필드는 무시하고 {db} 전체 수만 알려줘", "{f} 값 구분 없이 {db} 총 몇 건인지 궁금해",
    "{db} 총계만 알려줘 — {f} 나누기 없이", "{f}별 집계 말고 {db} 전체 개수 하나만",
    "{f} 값이 어떤 것이어도 좋으니 {db} 합계 건수 줘", "{db} 전체 건수만 필요해, {f} 분류는 하지 마",
]


def mutate_name_v2(rng: random.Random, name: str) -> str:
    if rng.random() < 0.5:
        return name.rstrip() + rng.choice(SUFFIXES)
    return rng.choice(EMOJIS) + name.strip()


def mutate_name(rng: random.Random, name: str) -> str:
    """v3: 끝공백뿐 아니라 앞공백/이중공백/이모지 — 어느 방향이든 "스키마 문자열을 글자
    그대로 복사"만이 정답이 되게 변이를 대칭으로 만든다. (v2는 끝공백·이모지만 가르쳐
    모델이 앞공백을 창작하는 과잉일반화 발생 — 홀드아웃 오답 48건 ' 아틀라스 링크'.)"""
    r = rng.random()
    if r < 0.35:
        return name.rstrip() + rng.choice(SUFFIXES)
    if r < 0.55:
        return " " + name.strip()
    if r < 0.65:
        s2 = name.strip()
        return s2.replace(" ", "  ", 1) if " " in s2 else s2 + " "
    return rng.choice(EMOJIS) + name.strip()


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--recipe", default="v4", choices=["v2", "v3", "v4"])
    global RECIPE
    RECIPE = ap.parse_args().recipe
    rng = random.Random(SEED)
    db_keys = load_db_keys()
    all_dbs = list(db_keys)
    rows = [json.loads(x) for x in (DATA / "labeled2.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]

    if RECIPE == "v4":  # count/ambig 표현 다양화 — 학습 6DB 한정, 정답은 실행 산출
        train_dbs = sorted({r["spec"][0] for r in rows if r.get("spec")})
        extra = []
        for db in train_dbs:
            if db not in db_keys:
                continue
            for t in V4_COUNT:
                extra.append({"id": f"v4c-{len(extra):04d}", "q": t.format(db=db), "tag": "count",
                              "spec": [db, "count", None, None], "gold": None})
            for f in db_keys[db][:3]:
                for t in V4_AMBIG:
                    extra.append({"id": f"v4a-{len(extra):04d}", "q": t.format(db=db, f=f), "tag": "ambig",
                                  "spec": [db, "count", None, None], "gold": None})
        import sqlalchemy as _sa
        with _engine.connect() as c:
            for e in extra:
                esc = e["spec"][0].replace("'", "''")
                e["gold"] = [int(c.execute(_sa.text(
                    f"SELECT count(*) FROM notion_rows WHERE scope='company' AND db_name = '{esc}'")).scalar())]
        rows = rows + extra
        print(f"[v4] count/ambig 추가 예시 {len(extra)}건")

    system = _SYSTEM.format(dialect="postgres")
    kept, dropped = [], Counter()
    stats = Counter()
    for r in rows:
        spec = list(r["spec"])
        if not spec or spec[0] not in db_keys:
            dropped["no_db"] += 1
            continue
        q = r["q"]
        db = spec[0]
        verify_gold = True

        # (a) db_name 변이 — 스키마+질문+SQL에 일관 적용, 절반은 clean 이름과 짝 함정
        pair_trap = None
        if rng.random() < NAME_MUT_P and db in q:
            mut = mutate_name(rng, db) if RECIPE == "v3" else mutate_name_v2(rng, db)
            q = q.replace(db, mut)
            if rng.random() < 0.5:
                pair_trap = db  # clean 이름도 스키마에 남겨 최장 정확일치 요구
            spec[0] = mut
            verify_gold = False  # 가상 이름이라 DB 실행 불가 — SQL 문자열 정합만 교육
            stats["name_mut"] += 1
        # (b) 필터값 변이 — "질문의 값을 글자 그대로" 강화 (정답 0이어도 SQL은 옳다)
        elif spec[1] == "count" and spec[3] and rng.random() < VAL_MUT_P:
            m = re.search(r"properties->>'([^']+)'='([^']+)'\s*$", spec[3])
            if m and m.group(2) in q:
                old = m.group(2)
                new = rng.choice(VAL_MUTS)
                q = q.replace(old, new)
                spec[3] = spec[3][: m.start(2)] + new + spec[3][m.end(2) :]
                verify_gold = False
                stats["val_mut"] += 1

        sql = canonical_sql(spec)
        if verify_gold and not verify_sql(sql, r["gold"]):
            dropped["gold_mismatch"] += 1
            continue

        # 스키마 증강: 타깃 DB(변이 포함) + 무작위 DB들, 순서 셔플
        others = [d for d in all_dbs if d != r["spec"][0]]
        n_extra = rng.randint(MIN_DBS_IN_SCHEMA - 1, len(others))
        subset_names = rng.sample(others, n_extra) + [spec[0]]
        if pair_trap and pair_trap not in subset_names:
            subset_names.append(pair_trap)
        # (c) v3 접두 혼동쌍: 타깃과 접두관계인 가상 sibling을 스키마에 넣어
        #     "파트너 질문에 파트너사를 쓰는" 오결합을 벌점 없이 교정 (질문·타깃 불변)
        sib = None
        if RECIPE == "v3" and rng.random() < PAIR_SIB_P:
            base = spec[0].strip() or spec[0]
            sib = base + rng.choice(SIB_SUFFIXES)
            if sib not in subset_names and sib != spec[0]:
                subset_names.append(sib)
                stats["sib_trap"] += 1
        rng.shuffle(subset_names)
        subset = {d: db_keys.get(d, db_keys[r["spec"][0]]) for d in subset_names}
        kept.append(
            {
                "id": r["id"],
                "tag": r["tag"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": render_user(q, subset)},
                    {"role": "assistant", "content": json.dumps({"sql": sql}, ensure_ascii=False)},
                ],
            }
        )

    rng.shuffle(kept)
    n_val = max(1, int(len(kept) * VAL_FRAC))
    val, train = kept[:n_val], kept[n_val:]
    (DATA / "sft_train.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in train) + "\n", encoding="utf-8")
    (DATA / "sft_val.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in val) + "\n", encoding="utf-8")

    print(f"[sft] 생성 {len(kept)} (train {len(train)} / val {len(val)}) · 제외 {dict(dropped)} · 변이 {dict(stats)}")
    print(f"[sft] tag 분포: {Counter(x['tag'] for x in kept)}")
    ex = train[0]
    print("\n── 샘플 1건 (user 앞 400자) ──")
    print(ex["messages"][1]["content"][:400])
    print("── target ──")
    print(ex["messages"][2]["content"][:200])


if __name__ == "__main__":
    main()
