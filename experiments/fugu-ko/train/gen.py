"""G1a — 학습 데이터 생성기 (D5 §2). 질문 + 정답 스펙을 실DB 위에서 동시 생성.

설계(d5-learned-selector-plan.md):
- DB질의(structured) ~600: 실DB(notion_rows) 스키마·실값 기반 템플릿 —
  {count-single, count-filter, groupby, 모호형(count vs groupby)} × 표현 변형.
  정답 스펙(t3_gold 방식 (db, kind, group_key, extra_where))을 질문과 함께 생성
  → 라벨링은 실DB 대조만으로 결정론(LLM 판단 0회).
- 라우팅(routing) ~400: route별(구조화/지식/그래프) 템플릿 × 실토픽 — expected는 구성상.
- hard-type 과표집: 이모지·공백 포함 DB명(baseline 약점 S8), count-단일을 GROUP BY로
  과분해하는 모호 표현(A.X 약점 D2) — 불일치(모델 간 정오 갈림)가 학습 신호의 전부.
- 골든(t3/t5) 정규화 dedupe — 골든은 절대 미학습 holdout.
- seed 고정(42) → 재현 가능.

출력: train/data/questions.jsonl
  {"id","task":"structured"|"routing","q", "spec":[db,kind,gkey,where] | "expected_route"}
실행: PYTHONPATH=... python experiments/fugu-ko/train/gen.py [--validate]
  --validate: 각 structured 스펙의 gold number-set이 실DB에서 계산 가능한지 검증(권장).
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

DATA = HERE / "data"
GOLDEN = ROOT / "golden"

random.seed(42)

# ── 실DB 스키마 재료 (2026-07-08 orthus_company 실측 — 행 10+ DB, select형 필드만) ──
# db_name -> {"gb": groupby 가능 필드, "flt": {필드: [값...]}}  ('가 든 값은 SQL where 안전상 제외)
SCHEMA: dict[str, dict] = {
    "협업업무표": {
        "gb": ["상태", "우선순위", "담당자"],
        "flt": {"상태": ["시작 전", "완료"], "우선순위": ["높음", "보통", "비상", "핫픽스"]},
    },
    "Nova 개발 로드맵": {
        "gb": ["상태", "우선순위", "유형", "오너"],
        "flt": {"상태": ["Done", "In Progress", "Not Started"],
                "우선순위": ["P0", "P1", "P2", "P3"],
                "유형": ["Bug", "Epic", "Perf", "Research", "Tech Debt", "UX"]},
    },
    "AI관련툴": {
        "gb": ["카테고리", "모델"],
        "flt": {"카테고리": ["범용", "영상", "오디오", "이미지", "자막"], "모델": ["오픈소스", "제품"]},
    },
    "배포 공지": {
        "gb": ["공지 상태", "관리자/출연자"],
        "flt": {"공지 상태": ["시작 전", "완료"]},
    },
    "회사 미팅 기록": {"gb": ["미팅 상태", "파트너사"], "flt": {"미팅 상태": ["시작 전", "완료"]}},
    "참고 문서": {"gb": ["카테고리"], "flt": {"카테고리": ["과거자료", "양식", "전략"]}},
    "🎯 NOVA 영입 후보 리스트": {  # hard: 이모지 DB명 (S8 baseline 약점)
        "gb": ["Tier", "발송 상태", "컨택 경로"],
        "flt": {"Tier": ["A", "B", "S", "산업계"],
                "발송 상태": ["발송 완료", "콜드메일 전"]},
    },
    "직원 ": {"gb": ["직책"], "flt": {"직책": ["대표", "반장", "지부장", "행정"]}},  # hard: 후행 공백
    "파트너사": {"gb": ["상태"], "flt": {"상태": ["시작전", "컨택중", "협업중"]}},
    "파트너": {"gb": ["구분", "분야"], "flt": {"구분": ["AI 파운데이션 모델 기관", "기타", "업계 파트너"]}},
    "프로젝트 주간 노트": {"gb": ["프로젝트"],
                    "flt": {"프로젝트": ["Nova", "orbit", "아틀라스", "Script AI"]}},
    "아틀라스 링크": {"gb": ["구분"], "flt": {"구분": ["관리자용", "사용자용", "웹"]}},
}
HARD_DBS = ["🎯 NOVA 영입 후보 리스트", "직원 "]  # 과표집 대상

# ── 표현 변형 템플릿 ──────────────────────────────────────────────────────────
T_COUNT = [
    "{db}에 항목이 총 몇 개야?",
    "{db} 행 개수 알려줘",
    "{db}에 등록된 건 모두 몇 건이지?",
    "{db} 전체 개수가 궁금해",
    "{db}에는 몇 개나 들어 있어?",
    "{db} 총 몇 개인지 세어줘",
    "{db} 레코드 수가 얼마나 되지?",
    "{db}에 쌓인 항목 수 좀 확인해줘",
    "지금 {db}에 전부 몇 건 있어?",
    "{db} 규모가 몇 개 정도야?",
]
T_COUNT_FLT = [
    "{db}에서 {f}[가] {v}인 항목은 몇 개야?",
    "{db} 중 {f} {v}인 건 몇 건이지?",
    "{f}[가] {v}인 {db} 항목 개수 알려줘",
    "{db}에서 {f}=={v} 조건이면 몇 개나 돼?",
    "{v} 상태의 {f} 기준으로 {db} 몇 건인지 세어줘",
    "{db} 안에 {f}[가] {v}[로] 표시된 건 총 몇 개야?",
]
T_GROUPBY = [
    "{db}[를] {f}별로 개수 알려줘",
    "{db} {f}별 분포가 어떻게 돼?",
    "{f} 기준으로 {db} 건수를 나눠서 보여줘",
    "{db}[는] {f}별로 몇 개씩 있어?",
    "{db} 항목을 {f}[로] 묶으면 각각 몇 건이야?",
    "{f}마다 {db}[가] 몇 개인지 집계해줘",
    "{db}의 {f}별 건수 통계 좀 보여줘",
    "{f} 값별로 {db} 개수를 정리해줘",
]
# 모호형(A.X 약점 D2): "총 몇 개"인데 대상 필드를 언급 → count-단일이 정답, GROUP BY 과분해 유도
T_AMBIG_COUNT = [
    "{db}에서 {f} 구분 있잖아, 그거 다 합쳐서 총 몇 개야?",
    "{f}[가] 뭐든 상관없이 {db} 전체는 몇 건이야?",
    "{db}에 {f} 값이 있는 것까지 포함해서 모두 몇 개인지 알려줘",
    "{f} 나누지 말고 {db} 통째로 몇 개인지만 말해줘",
    "{db}[는] {f} 종류 상관없이 다 더하면 몇 건이지?",
    "{f}별로 말고 {db} 총합 개수만 알려줘",
]

# 라우팅 재료 (route는 구성상 라벨)
WIKI_TOPICS = ["nova 서비스", "atlas 프로젝트", "회사 배포 프로세스", "영입 후보 선정 기준",
               "협업 방식", "온보딩 절차", "주간 회고 운영", "파트너 협업 정책",
               "영상 제작 파이프라인", "AI 툴 선정 기준", "orbit 프로젝트", "대리 AI 비서",
               "배포 공지 작성 규칙", "콜드메일 발송 프로세스", "미팅 기록 관리 방식",
               "씬 작업 트래커 사용법", "월간 목표 수립 절차", "팀 커뮤니케이션 규칙",
               "아틀라스 링크 운영", "참고 문서 분류 체계"]
T_WIKI = ["{t}에 대해 설명해줘", "{t}[가] 뭔지 알려줘", "{t}[는] 어떻게 진행돼?",
          "{t}의 핵심이 뭐야?", "{t} 관련해서 정리된 내용 있어?",
          "{t}[는] 왜 그렇게 하는 거야?", "{t}[를] 처음 보는 사람한테 소개해줘",
          "{t}에서 주의할 점이 뭐야?"]
GRAPH_PAIRS = [("Nova", "아틀라스"), ("아크메", "Nova"), ("영입 후보", "파트너사"),
               ("배포 공지", "협업업무표"), ("orbit", "대리 AI"), ("Nova", "영상 제작"),
               ("아틀라스", "파트너"), ("미팅 기록", "파트너사"), ("AI관련툴", "영상 파이프라인"),
               ("주간 노트", "월간 목표")]
T_GRAPH = ["{a}[와] {b}[는] 무슨 관계야?", "{a}랑 {b}[가] 어떻게 연결돼 있어?",
           "{a} 관련 주장이 {b}[와] 충돌하는 게 있어?", "{a}에 대한 근거가 어디서 나왔는지 추적해줘",
           "{a}에서 {b}까지 이어지는 경로를 보여줘", "{a}[와] {b} 사이에 공통으로 엮인 게 뭐야?"]
T_STRUCT_ROUTE = ["{db} 상태별 개수 보여줘", "{db}에 몇 건 있는지 세어줘",
                  "{db} 목록 뽑아줘", "{db}에서 담당자별로 몇 개씩인지 알려줘",
                  "{db} 최근 항목 10개만 보여줘", "{db}에서 완료된 것만 몇 개인지 알려줘",
                  "{db} 우선순위 높은 순으로 정렬해줘", "{db} 건수 집계해줘"]


_JOSA = {"[를]": ("을", "를"), "[는]": ("은", "는"), "[와]": ("과", "와"),
         "[가]": ("이", "가"), "[로]": ("으로", "로")}


def josa(text: str) -> str:
    """받침 규칙 조사 치환: 변수 뒤 '[를]'-마커를 앞 글자 종성으로 결정.
    한글이 아니면(영문/이모지/숫자) 무종성 취급. '[로]'는 종성 ㄹ이면 '로'."""
    def pick(prev: str, marker: str) -> str:
        jong, plain = _JOSA[marker]
        if not prev or not ("가" <= prev <= "힣"):
            return plain
        code = (ord(prev) - 0xAC00) % 28
        if marker == "[로]" and code == 8:  # 종성 ㄹ
            return plain
        return jong if code else plain

    for m in _JOSA:
        while m in text:
            i = text.index(m)
            prev = text[i - 1].strip() if i else ""
            # 변수 끝이 공백이면(후행공백 DB명) 그 앞 실문자 기준
            j = i - 1
            while j >= 0 and text[j] == " ":
                j -= 1
            prev = text[j] if j >= 0 else ""
            text = text[:i] + pick(prev, m) + text[i + len(m):]
    return text


def norm(q: str) -> str:
    return re.sub(r"\s+", "", q.lower())


def golden_norms() -> set[str]:
    out = set()
    for name in ["t3", "t5"]:
        items = json.load(open(GOLDEN / f"{name}.json", encoding="utf-8"))["items"]
        out |= {norm(it["q"]) for it in items}
    return out


def gen_structured() -> list[dict]:
    rows: list[dict] = []

    def add(q, db, kind, gkey=None, where=None, tag=""):
        rows.append({"task": "structured", "q": josa(q), "spec": [db, kind, gkey, where], "tag": tag})

    for db, sch in SCHEMA.items():
        boost = 2 if db in HARD_DBS else 1  # hard DB 과표집
        # count-single
        for t in random.sample(T_COUNT, k=min(len(T_COUNT), 5 * boost)):
            add(t.format(db=db), db, "count", tag="count")
        # count-filter
        for f, vals in sch["flt"].items():
            for v in vals:
                if "'" in v:
                    continue
                for t in random.sample(T_COUNT_FLT, k=min(3 * boost, len(T_COUNT_FLT))):
                    add(t.format(db=db, f=f, v=v), db, "count", None,
                        f"properties->>'{f}'='{v}'", tag="filter")
        # groupby
        for f in sch["gb"]:
            for t in random.sample(T_GROUPBY, k=min(len(T_GROUPBY), 3 * boost)):
                add(t.format(db=db, f=f), db, "groupby", f, None, tag="groupby")
        # 모호형(count가 정답)
        for f in sch["gb"][:2]:
            for t in random.sample(T_AMBIG_COUNT, k=min(3 * boost, len(T_AMBIG_COUNT))):
                add(t.format(db=db, f=f), db, "count", None, None, tag="ambig")
    return rows


def gen_routing() -> list[dict]:
    rows: list[dict] = []

    def add(q, route, tag):
        rows.append({"task": "routing", "q": josa(q), "expected_route": route, "tag": tag})

    for db in SCHEMA:
        for t in T_STRUCT_ROUTE:
            add(t.format(db=db), "structured", "r-struct")
    for topic in WIKI_TOPICS:
        for t in T_WIKI:
            add(t.format(t=topic), "wiki", "r-wiki")
    for a, b in GRAPH_PAIRS:
        for t in T_GRAPH:
            add(t.format(a=a, b=b), "graph", "r-graph")
    return rows


def validate_specs(rows: list[dict]) -> list[dict]:
    """각 structured 스펙의 gold가 실DB에서 계산 가능하고 비어있지 않은지 검증."""
    from t3_gold import gold_numbers

    ok, dropped = [], 0
    cache: dict[tuple, bool] = {}
    for r in rows:
        if r["task"] != "structured":
            ok.append(r)
            continue
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
    print(f"  스펙 검증: 유지 {len(ok)} / 제외 {dropped} (gold 비었거나 계산 불가)")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true", help="실DB로 스펙 검증(권장)")
    args = ap.parse_args()

    structured = gen_structured()
    routing = gen_routing()
    rows = structured + routing

    # 골든 dedupe (holdout 누출 차단)
    gn = golden_norms()
    before = len(rows)
    rows = [r for r in rows if norm(r["q"]) not in gn]
    # 자기 자신 중복 제거
    seen: set[str] = set()
    uniq = []
    for r in rows:
        n = norm(r["q"])
        if n in seen:
            continue
        seen.add(n)
        uniq.append(r)
    rows = uniq
    print(f"[gen] structured {len(structured)} + routing {len(routing)} = {before}"
          f" → dedupe 후 {len(rows)}")

    if args.validate:
        rows = validate_specs(rows)

    random.shuffle(rows)
    for i, r in enumerate(rows):
        r["id"] = f"g1-{i:04d}"

    DATA.mkdir(parents=True, exist_ok=True)
    out = DATA / "questions.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n_s = sum(1 for r in rows if r["task"] == "structured")
    n_r = len(rows) - n_s
    tags: dict[str, int] = {}
    for r in rows:
        tags[r["tag"]] = tags.get(r["tag"], 0) + 1
    print(f"[gen] 최종 {len(rows)}문항 → {out}")
    print(f"  structured {n_s} / routing {n_r}")
    print("  유형별:", json.dumps(tags, ensure_ascii=False))


if __name__ == "__main__":
    main()
