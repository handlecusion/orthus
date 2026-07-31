"""T3 홀드아웃 42문항 — 실행 + 실DB 채점 + 오류 비상관 행렬.

왜 만드나 (두 가지를 한 번에 답한다):

1. **SVC 2차 모델 선정** (`docs/.../structured-verification-cascade.md` §3.3). 그 문서는
   2차 모델을 "정확도"가 아니라 **"1차와 오류가 비상관인가"** 로 고르라고 했고, 그 값은
   **미측정**이라고 적어 뒀다. 기존 18문항으로 계산하면 solar의 오답이 **단 1건**이라
   후보 셋이 전부 자카드 0.00으로 동률 — 표본 n=1로는 아무것도 못 고른다. 42문항을 더해
   60문항으로 키우면 그 비교가 비로소 의미를 갖는다.

2. **(b) 배정 규칙의 낙관 편향 검증** (competition-report §8.1). 기존 18문항은 배정 규칙을
   *뽑아낸* 문항이라 (b)에게 홈그라운드다. 이 42문항은 규칙 확정 뒤에 만들었고 규칙 도출에
   일절 쓰이지 않았으므로 **진짜 out-of-sample 홀드아웃**이다.

정답(gold)은 LLM을 거치지 않는다 — `HOLDOUT_SPECS`가 실DB에서 canonical 집계로 정수 집합을
만들고, 모델 결과 rows에서 뽑은 정수 집합과 number-set 비교한다(t3_gold.py와 동일한 채점법).

    FUGU_DSN=postgresql://orthus:orthus@localhost:5433/orthus_company \
      python t3_holdout.py                 # 4모델 × 42문항 실행 + 채점 + 비상관 행렬
      python t3_holdout.py --score-only    # 이미 저장된 raw만 재채점
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from itertools import combinations
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
RAW = HERE / "analysis" / "raw"
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")
MODELS = ["baseline", "solar", "ax", "exaone"]

# id -> (db_name, kind, group_key|None, extra_where|None).  kind: count | groupby
# 기존 t3 18문항과 (db, kind, key, where) 조합이 하나도 겹치지 않는다 — holdout 이려면 필수.
# 날짜 속성은 NULL 그룹/포맷 모호성 때문에 넣지 않았다.
HOLDOUT_SPECS: dict[str, tuple] = {
    # 협업업무표 (794) — 기존: groupby 상태/담당자/우선순위, count 상태=완료/보류
    "t3-h01": ("협업업무표", "count", None, None),
    # (t3-h02 '예상 소요 시간' 삭제) 실제 키가 끝에 공백이 붙은 `'예상 소요 시간 '` 이다(노션 데이터
    # 오염). 공백 없이 채점하면 전 모델이 NULL 그룹 794를 반환해 전원 정답 → 변별력 0이고, 골드에
    # 공백을 넣으면 전 모델이 전원 오답 → 모델 품질이 아니라 "끝 공백을 알아맞히나"를 재게 된다.
    # 어느 쪽도 모델을 비교하지 못하므로 문항에서 뺀다.
    "t3-h03": ("협업업무표", "groupby", "관리자/출연자", None),
    "t3-h04": ("협업업무표", "count", None, "properties->>'우선순위'='비상'"),
    "t3-h05": ("협업업무표", "count", None, "properties->>'우선순위'='핫픽스'"),
    "t3-h06": ("협업업무표", "count", None, "properties->>'상태'='시작 전'"),
    "t3-h07": ("협업업무표", "count", None, "properties->>'우선순위'='높음'"),
    # Nova 개발 로드맵 (279) — 기존: groupby 상태/우선순위/오너
    "t3-h08": ("Nova 개발 로드맵", "count", None, None),
    "t3-h09": ("Nova 개발 로드맵", "groupby", "마일스톤", None),
    "t3-h10": ("Nova 개발 로드맵", "groupby", "Blocked", None),
    "t3-h11": ("Nova 개발 로드맵", "groupby", "스프린트/주차", None),
    "t3-h12": ("Nova 개발 로드맵", "count", None, "properties->>'상태'='Done'"),
    "t3-h13": ("Nova 개발 로드맵", "count", None, "properties->>'상태'='In Progress'"),
    "t3-h14": ("Nova 개발 로드맵", "count", None, "properties->>'마일스톤'='MVP'"),
    "t3-h15": ("Nova 개발 로드맵", "count", None, "properties->>'우선순위'='P0'"),
    # AI관련툴 (49) — 기존: count 전체, groupby 카테고리
    "t3-h16": ("AI관련툴", "groupby", "모델", None),
    "t3-h17": ("AI관련툴", "count", None, "properties->>'모델'='오픈소스'"),
    "t3-h18": ("AI관련툴", "count", None, "properties->>'카테고리'='영상'"),
    # 배포 공지 (48) — 기존: groupby 공지 상태, count 전체
    "t3-h19": ("배포 공지", "groupby", "관리자/출연자", None),
    "t3-h20": ("배포 공지", "count", None, "properties->>'공지 상태'='완료'"),
    # 회사 미팅 기록 (28) — 기존: count 전체, groupby 파트너사
    "t3-h21": ("회사 미팅 기록", "groupby", "미팅 상태", None),
    "t3-h22": ("회사 미팅 기록", "count", None, "properties->>'미팅 상태'='완료'"),
    # 참고 문서 (26) — 신규 db
    "t3-h23": ("참고 문서", "count", None, None),
    "t3-h24": ("참고 문서", "groupby", "카테고리", None),
    "t3-h25": ("참고 문서", "count", None, "properties->>'카테고리'='전략'"),
    # 영입 후보 (19) — 기존: count 전체, groupby Tier/발송 상태
    "t3-h26": ("🎯 NOVA 영입 후보 리스트", "groupby", "컨택 경로", None),
    "t3-h27": ("🎯 NOVA 영입 후보 리스트", "count", None, "properties->>'컨택 경로'='LinkedIn DM'"),
    # 파트너 (11) — 기존: groupby 분야
    "t3-h28": ("파트너", "count", None, None),
    "t3-h29": ("파트너", "groupby", "구분", None),
    "t3-h30": ("파트너", "count", None, "properties->>'구분'='업계 파트너'"),
    # 파트너사 (11) — 신규 db (파트너와 다른 db_name이다)
    "t3-h31": ("파트너사", "count", None, None),
    "t3-h32": ("파트너사", "groupby", "상태", None),
    "t3-h33": ("파트너사", "groupby", "선택", None),
    "t3-h34": ("파트너사", "count", None, "properties->>'상태'='협업중'"),
    # 아틀라스 링크 (10) — 신규 db
    "t3-h35": ("아틀라스 링크", "count", None, None),
    "t3-h36": ("아틀라스 링크", "groupby", "구분", None),
    # 프로젝트 주간 노트 (10) — 신규 db
    "t3-h37": ("프로젝트 주간 노트", "count", None, None),
    "t3-h38": ("프로젝트 주간 노트", "groupby", "프로젝트", None),
    # 배포 일정 (7) — 신규 db
    "t3-h39": ("배포 일정", "count", None, None),
    "t3-h40": ("배포 일정", "groupby", "배포 상태", None),
    "t3-h41": ("배포 일정", "groupby", "배포 유형", None),
    # 미팅정리 (6) — 신규 db
    "t3-h42": ("미팅정리", "count", None, None),
}


def gold_numbers(db: str, kind: str, gkey, where) -> set[int]:
    """정답 정수 집합 — 실DB canonical 집계. LLM 미개입."""
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        w = "scope='company' AND db_name=%s" + (f" AND {where}" if where else "")
        if kind == "count":
            cur.execute(f"SELECT count(*) FROM notion_rows WHERE {w}", (db,))
            return {int(cur.fetchone()[0])}
        cur.execute(
            f"SELECT count(*) FROM notion_rows WHERE {w} GROUP BY properties->>%s", (db, gkey)
        )
        return {int(r[0]) for r in cur.fetchall()}


def result_numbers(rows) -> set[int]:
    nums: set[int] = set()
    for row in rows or []:
        for c in row:
            if isinstance(c, bool):
                continue
            if isinstance(c, int):
                nums.add(c)
            elif isinstance(c, str) and re.fullmatch(r"-?\d+", c.strip()):
                nums.add(int(c))
    return nums


def raw_path(model: str) -> Path:
    return RAW / f"t3h_{model}.jsonl"


def run(models: list[str]) -> None:
    """각 모델을 프로덕션 structured 경로에 태워 raw jsonl로 저장."""
    sys.path.insert(0, str(HERE))
    from harness import run_t3  # noqa: E402  (orthus import 부작용 때문에 지연)
    from pool import build_pool  # noqa: E402

    items = json.loads((HERE / "golden" / "t3_holdout.json").read_text(encoding="utf-8"))["items"]
    pool = build_pool(models)
    RAW.mkdir(parents=True, exist_ok=True)
    for slug in models:
        chat = pool[slug]
        out = raw_path(slug)
        with out.open("w", encoding="utf-8") as fh:
            for i, item in enumerate(items, 1):
                rec = run_t3(chat, item)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                print(f"  {slug:9} {i:2}/{len(items)} {item['id']}", flush=True)
        print(f"  → {out}")


def score(models: list[str]) -> dict[str, set[str]]:
    """모델별 오답 문항 집합. 파일이 없으면 그 모델은 건너뛴다."""
    gold = {i: gold_numbers(*s) for i, s in HOLDOUT_SPECS.items()}
    wrong: dict[str, set[str]] = {}
    for m in models:
        p = raw_path(m)
        if not p.exists():
            print(f"  ! {m}: raw 없음 ({p.name}) — 건너뜀")
            continue
        bad: set[str] = set()
        n = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            i = r["id"]
            if i not in gold:
                continue
            n += 1
            want, got = gold[i], result_numbers(r.get("rows"))
            if not (want and want <= got):
                bad.add(i)
        wrong[m] = bad
        print(f"  {m:9} 정답 {n - len(bad):2}/{n}  ({(n - len(bad)) / n * 100:5.1f}%)")
    return wrong


def decorrelation(wrong: dict[str, set[str]]) -> None:
    """SVC 2차 모델 선정 지표 — 정확도가 아니라 '오류가 겹치지 않는가'."""
    print("\n  ── 오류 비상관 (SVC 2차 모델 선정) ──")
    print("  회수가능 = 1차가 틀린 걸 2차가 맞힘 | 동시오답 = 둘 다 틀림(캐스케이드 무력)")
    for a, b in combinations([m for m in MODELS if m in wrong], 2):
        pa, pb = wrong[a], wrong[b]
        union = pa | pb
        j = len(pa & pb) / len(union) if union else 0.0
        print(
            f"  {a:9}↔{b:9} 동시오답 {len(pa & pb):2} | "
            f"{a}→{b} 회수 {len(pa - pb):2} | {b}→{a} 회수 {len(pb - pa):2} | 자카드 {j:.2f}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--score-only", action="store_true")
    a = ap.parse_args()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    if not a.score_only:
        run(models)
    print()
    decorrelation(score(models))


if __name__ == "__main__":
    main()
