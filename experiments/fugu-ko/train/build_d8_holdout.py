"""D8 — 판정셋 생성 (d8-prereg 명세). D7 fresh 생성기의 결함 2종 수정판.

D7 E12 교훈 반영:
  ① 조사 마커를 변수({db}/{v}) 직후에 두지 않는다 — 생성 후 `[가]…` 잔존 0 assert.
  ② 합성값에 필드명 접두 금지("이름 샘플-03" 류) — 실제형 한국어 값 사용.
설계:
  - 신규 합성 DB 8종(명시) — 학습 6·구홀드아웃 6·D7 fresh 10과 전부 무겹침. 워크스페이스
    버릇(끝공백·이모지·특수문자 값) 반영. distinct-spec 위주(검정력), 패러프레이즈 ≤4.
  - n 목표 ~2,000 · zero ~10% · gold 결정론 실행 산출.
실행: python train/build_d8_holdout.py [--wipe]   # → golden/t3_d8_holdout.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import uuid
from collections import Counter
from pathlib import Path

import sqlalchemy as sa

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

GOLDEN = ROOT / "golden"
DSN = "postgresql://orthus:orthus@localhost:5433/orthus_company_0706"
SEED = 20260717
_engine = sa.create_engine(DSN)

# ── 신규 템플릿 (조사 마커 없음 — 변수 뒤는 항상 조사-중립 구문) ────────────────
T_COUNT_8 = [
    "{db} 테이블에 데이터가 총 몇 건 있는지 세어 줘",
    "{db} 항목 전체 개수 하나만 콕 집어 알려줘",
    "{db} 안에 지금 몇 건이나 쌓여 있지?",
    "{db} 전체 행 수 알려줘",
]
T_FLT_8 = [
    '{db} 중에 {f} 값이 "{v}" 인 항목은 몇 건이지?',
    '{f} 항목이 "{v}" 인 {db} 데이터 개수 알려줘',
    '{db} 리스트에서 {f} 필드가 "{v}" 인 것만 세어 줘',
    '"{v}" 라는 {f} 값인 {db} 건수 확인해 줘',
]
T_GB_8 = [
    "{db} 데이터, {f} 값 기준으로 각각 몇 건씩인지 나눠서 알려줘",
    "{f} 항목별 {db} 건수 분포 좀 보여줘",
    "{db} 개수 집계해 줘 — 기준은 {f} 항목",
]
T_AMBIG_8 = [
    "{f} 항목 구분 없이 {db} 전체 개수만 알려줘",
    "{db} 총 건수 궁금해, {f} 값 나누기는 필요 없어",
    "{f} 값 상관없이 {db} 데이터 전체 합계만 세어 줘",
]
T_FLT2_8 = [
    '{db} 중 {f1} 값이 "{v1}" 이고 {f2} 값이 "{v2}" 인 항목 몇 건이야?',
    '{f1} "{v1}" 그리고 {f2} "{v2}" 조건 둘 다 맞는 {db} 개수 알려줘',
    '{db} 리스트에서 {f1} 필드 "{v1}", {f2} 필드 "{v2}" 동시 조건 건수 세어 줘',
]
ABSENT_VALUES = ["보류(외부 검토)", "폐기 예정", "임시저장", "미배정", "검수 반려", "이관 완료"]

# ── 신규 합성 DB 8종 (필드명 접두 없는 실제형 값) ──────────────────────────────
D8_SYNTHETIC: dict[str, dict] = {
    "외주 계약 ": {"rows": 22, "fields": {  # 끝공백
        "업체": None, "계약 형태": ["건별 정산", "월 고정", "성과 연동"],
        "진행 상태": ["계약 완료(선금)", "검토 중", "협상 중", "종료"],
        "분야": ["VFX", "음향", "번역·자막"]}},
    "🎥 로케이션 헌팅": {"rows": 20, "fields": {
        "장소": None, "유형": ["실내 스튜디오", "야외", "관공서 협조"],
        "섭외 상태": ["확정", "답사 예정", "불가 통보"], "담당": ["김민서", "박도윤", "이서연"]}},
    "배우 오디션 기록": {"rows": 24, "fields": {
        "지원자": None, "배역군": ["주연 후보", "조연", "단역·보조"],
        "결과": ["합격", "대기(2차)", "불합격"], "추천 경로": ["에이전시", "공개 오디션", "내부 추천"]}},
    "정산 내역 ": {"rows": 18, "fields": {  # 끝공백
        "건명": None, "지급 상태": ["지급 완료", "지급 대기", "보류(증빙 미비)"],
        "비용 구분": ["인건비", "장비·렌탈", "기타 경비"]}},
    "🎼 배경음악 라이선스": {"rows": 16, "fields": {
        "곡명": None, "라이선스 종류": ["독점", "비독점", "기간 한정"],
        "협의 상태": ["체결", "견적 요청", "재협상"]}},
    "시사회 초청 명단": {"rows": 26, "fields": {
        "성함": None, "구분": ["언론·기자", "투자사", "제작진 지인"],
        "참석 여부": ["참석 확정", "미정", "불참"], "좌석 등급": ["VIP", "일반"]}},
    "장면 콘티 검수": {"rows": 19, "fields": {
        "장면 번호": None, "검수 단계": ["1차 통과", "수정 요청", "최종 승인"],
        "우선도": ["급함", "보통", "낮음"]}},
    "🎧 사운드 소스 관리": {"rows": 17, "fields": {
        "소스명": None, "카테고리": ["효과음", "앰비언스", "폴리"],
        "정리 상태": ["태깅 완료", "미분류", "검수 필요"]}},
    "의상 소품 재고 ": {"rows": 21, "fields": {  # 끝공백
        "품목": None, "보관 위치": ["1창고", "2창고", "세트 현장"],
        "상태 점검": ["양호", "수선 필요", "폐기 대상"], "대여 여부": ["대여 중", "보관 중"]}},
    "📅 촬영 일정표": {"rows": 23, "fields": {
        "회차": None, "진행 구분": ["예정", "완료", "우천 연기"],
        "촬영 조": ["A조", "B조"], "야간 여부": ["주간", "야간"]}},
    "보도자료 배포처": {"rows": 18, "fields": {
        "매체명": None, "매체 유형": ["일간지", "온라인", "방송"],
        "배포 상태": ["발송 완료", "발송 예정", "회신 대기"]}},
    "편집본 버전 관리": {"rows": 20, "fields": {
        "버전명": None, "검토 결과": ["승인", "재편집", "감독 확인 대기"],
        "포맷": ["4K 마스터", "프록시", "시사용"]}},
}
NAME_POOLS = {  # unique 필드용 실제형 값 (필드명 미포함)
    "업체": ["미래컴퍼니", "블루윙 스튜디오", "한강미디어", "선샤인웍스", "그린룸", "누리픽처스"],
    "장소": ["파주 세트장", "을지로 옥상", "부산 영도 창고", "양수리 종합촬영소", "홍대 카페거리", "인천 폐공장"],
    "지원자": ["강하늘봄", "서지우", "문태양", "임소율", "채민준", "황보라", "신예찬", "오다인"],
    "건명": ["4월 촬영분 정산", "장비 렌탈 2차", "로케이션 답사비", "후반 색보정", "의상 제작비"],
    "곡명": ["새벽의 강", "달리는 도시", "먼지의 왈츠", "회색 파도", "여름 끝자락"],
    "성함": ["정한결", "유가온", "백서준", "남도희", "구본하", "송이레", "탁윤슬", "명주안"],
    "장면 번호": ["S01-오프닝", "S12-추격전", "S27-회상", "S33-대치", "S41-엔딩"],
    "소스명": ["빗소리 루프 A", "지하철 진입음", "군중 웅성거림", "유리 파열음", "밤 풀벌레"],
    "품목": ["트렌치코트(남)", "경찰 제복", "구형 라디오", "가죽 서류가방", "웨딩드레스", "빈티지 전화기"],
    "회차": ["3회차-파주", "7회차-을지로", "12회차-야간 추격", "15회차-실내", "18회차-엔딩"],
    "매체명": ["데일리시네", "무비인사이드", "컬처타임즈", "스크린노트", "필름저널"],
    "버전명": ["v0.9 가편", "v1.2 감독판", "v1.5 시사용", "v2.0 최종 후보", "v2.1 색보정 반영"],
}


def upsert_synthetic(rng: random.Random) -> None:
    with _engine.begin() as c:
        uid = c.execute(sa.text("SELECT user_id FROM notion_rows LIMIT 1")).scalar()
        for db, spec in D8_SYNTHETIC.items():
            db_id = "synthetic-d8-" + hashlib.sha1(db.encode()).hexdigest()[:12]
            n = c.execute(sa.text("SELECT count(*) FROM notion_rows WHERE db_id=:d"), {"d": db_id}).scalar()
            if n == spec["rows"]:
                continue
            c.execute(sa.text("DELETE FROM notion_rows WHERE db_id=:d"), {"d": db_id})
            for i in range(spec["rows"]):
                props = {}
                for f, vals in spec["fields"].items():
                    if vals is None:
                        pool = NAME_POOLS[f]
                        props[f] = pool[i % len(pool)] + ("" if i < len(pool) else f" {i // len(pool) + 1}")
                    else:
                        props[f] = rng.choice(vals)
                c.execute(sa.text(
                    "INSERT INTO notion_rows (row_id, db_id, db_name, properties, scope, user_id, updated_at, created_at, project) "
                    "VALUES (:rid, :did, :dn, CAST(:props AS jsonb), 'company', :uid, now(), now(), 'company')"),
                    {"rid": str(uuid.uuid4()), "did": db_id, "dn": db, "props": json.dumps(props, ensure_ascii=False), "uid": str(uid)})
            print(f"[d8-synthetic] {db!r} {spec['rows']}행 삽입")


def wipe() -> None:
    with _engine.begin() as c:
        n = c.execute(sa.text("DELETE FROM notion_rows WHERE db_id LIKE 'synthetic-d8-%'")).rowcount
    print(f"[wipe] d8 합성 행 {n}건 제거")


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
    if ap.parse_args().wipe:
        wipe()
        return

    # 템플릿 신규성 assert (gen2 · D7 fresh · v4 학습 증강과 전부 무중복)
    import re

    import gen2
    from build_fresh_holdout import T_AMBIG_F, T_COUNT_F, T_FLT2_F, T_FLT_F, T_GB_F
    from build_sft_data import V4_AMBIG, V4_COUNT

    prev = set(gen2.T_COUNT + gen2.T_COUNT_FLT + gen2.T_GROUPBY + gen2.T_AMBIG + gen2.T_COUNT_FLT2
               + T_COUNT_F + T_FLT_F + T_GB_F + T_AMBIG_F + T_FLT2_F + V4_COUNT + V4_AMBIG)
    mine = set(T_COUNT_8 + T_FLT_8 + T_GB_8 + T_AMBIG_8 + T_FLT2_8)
    assert not (prev & mine), f"기존 템플릿 중복: {prev & mine}"

    rng = random.Random(SEED)
    upsert_synthetic(rng)

    # 스키마 로드 (D8 합성 8종만 대상)
    schema: dict[str, dict] = {}
    with _engine.connect() as c:
        for dbn, props in c.execute(sa.text(
                "SELECT db_name, properties FROM notion_rows WHERE db_id LIKE 'synthetic-d8-%'")):
            fields = schema.setdefault(dbn, {})
            for k, v in props.items():
                if isinstance(v, str) and v.strip():
                    fields.setdefault(k, Counter())[v] += 1

    items: list[dict] = []

    def add(q: str, db: str, kind: str, gkey, where, tag: str, zero: bool = False) -> None:
        gold = [0] if zero else gold_exec(db, kind, gkey, where)
        if not zero and (not gold or gold == [0]):
            return
        items.append({"task": "structured", "q": q, "spec": [db, kind, gkey, where],
                      "tag": ("zero" if zero else tag), "gold": gold})

    for db, fields in schema.items():
        for t in T_COUNT_8:
            add(t.format(db=db), db, "count", None, None, "count")
        gb_fields = [f for f, vs in fields.items() if len(vs) >= 2][:4]
        for f in gb_fields:
            for t in T_AMBIG_8:
                add(t.format(db=db, f=f), db, "count", None, None, "ambig")
            for t in T_GB_8:
                add(t.format(db=db, f=f), db, "groupby", f, None, "groupby")
        pairs = [(f, v) for f, vs in fields.items() for v in list(vs)]
        rng.shuffle(pairs)
        for f, v in pairs[:26]:
            for t in rng.sample(T_FLT_8, k=3):
                add(t.format(db=db, f=f, v=v), db, "count", None, flt(f, v), "filter")
        f2 = [(a, va, b, vb) for i, (a, va) in enumerate(pairs) for b, vb in pairs[i + 1:] if a != b]
        rng.shuffle(f2)
        for a, va, b, vb in f2[:14]:
            for t in rng.sample(T_FLT2_8, k=2):
                add(t.format(db=db, f1=a, v1=va, f2=b, v2=vb), db, "count", None,
                    flt(a, va) + " AND " + flt(b, vb), "filter2")
        for f in list(fields)[:3]:
            v = next((x for x in ABSENT_VALUES if x not in fields[f]), None)
            if v:
                for t in rng.sample(T_FLT_8, k=3):
                    add(t.format(db=db, f=f, v=v), db, "count", None, flt(f, v), "filter", zero=True)

    # 조사 마커 잔존 0 assert (E12 재발 방지)
    bad = [it for it in items if re.search(r"\[(가|이|를|로|는|에|도|와)\]", it["q"])]
    assert not bad, f"조사 마커 잔존 {len(bad)}건"

    seen: set[str] = set()
    final: list[dict] = []
    for it in items:
        k = it["q"].replace(" ", "")
        if k in seen:
            continue
        seen.add(k)
        it["id"] = f"d8-{len(final):04d}"
        final.append(it)

    zc = sum(1 for it in final if it["tag"] == "zero")
    out = {"task": "T3_d8", "desc": "D8 판정셋 — 조사버그 수정 생성기, 신규 합성 DB 8종(명시), distinct-spec 위주",
           "synthetic_dbs": list(D8_SYNTHETIC), "seed": SEED, "items": final}
    (GOLDEN / "t3_d8_holdout.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[d8] {len(final)}문항 (zero {zc} = {zc / len(final) * 100:.0f}%) → golden/t3_d8_holdout.json")
    print(f"[d8] tag: {Counter(it['tag'] for it in final)}")
    print(f"[d8] DB별: {Counter(it['spec'][0] for it in final)}")


if __name__ == "__main__":
    main()
