"""E2 앵커 유도 프로브 — 지식 앵커를 '추측'하지 않고 코퍼스 실재 근거에서 뽑는다.

TS9 교훈(합성 문항은 코퍼스에 실존하는 토픽에 앵커해야 한다)의 연장. E2E 성공 판정에 쓸
지식 앵커(필수 키워드)를 저자가 상상해 적으면 두 가지가 깨진다:
- 코퍼스에 없는 키워드 → 전 arm 동시 실패(변별력 0, TS9 재현)
- 특정 모델의 말버릇 키워드 → 그 모델에 유리한 채점(편향)

그래서 후보 지식 하위질문(t2 검증분, grounding 30/30)을 **baseline(gpt-4o-mini) + solar**로
각 1회 단답시키고, 두 답에 **공통 등장**하는 고유항만 앵커 후보로 남긴다(모델 무관 + 코퍼스 실재).

실행:
  set -a; source ~/.orthus/nodes/company/node.env; set +a
  export ORTHUS_NODE=company ORTHUS_NODE_DB=orthus_company
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/e2_anchor_probe.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

from pool import build_pool  # noqa: E402
from e2_grounding import refusal_extended, refusal_gap, refusal_prod  # noqa: E402
from orthus.wiki.qa import ask  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
RAW = HERE / "analysis" / "raw"

# 후보 지식 하위질문 = t2 골든 전량(30). t2는 D2/D3에서 "n_sources>0"으로 30/30 grounded로
# 집계됐지만, 그건 **retrieve가 5건을 반환했다**는 뜻이지 **답이 그 안에 있다**는 뜻이 아니었다.
# 여기서 실제 답변 가능 여부(거절문 여부)를 처음으로 분리 측정한다.
KNOWLEDGE_SUBQ: dict[str, str] = {
    it["id"]: it["q"]
    for it in json.load(open(HERE / "golden" / "t2.json", encoding="utf-8"))["items"]
}

# 후보항 추출: 한글 3자+ / 영문 3자+ / 숫자. 불용어(일반 서술어)는 제외.
_STOP = {
    "있습니다", "합니다", "됩니다", "입니다", "관련", "내용", "정보", "다음", "이러한", "통해",
    "위해", "대한", "그리고", "또한", "있으며", "하는", "하고", "이는", "등이", "등의", "근거",
    "제공", "설명", "진행", "포함", "확인", "가능", "사용", "필요", "주요", "핵심", "때문",
    "출처", "문서", "페이지", "회사", "프로젝트", "서비스", "시스템", "기능", "과정", "방식",
    "없음", "답변", "질문", "기준", "경우", "이후", "현재", "구성", "지원", "작업", "관리",
}
_TOKEN = re.compile(r"[가-힣]{3,}|[A-Za-z][A-Za-z0-9_.-]{2,}|\d+")


def terms(text: str) -> set[str]:
    out = set()
    for t in _TOKEN.findall(text or ""):
        low = t.lower()
        if low in _STOP or t in _STOP:
            continue
        out.add(low)
    return out


def main() -> None:
    pool = build_pool(["baseline", "solar"])
    RAW.mkdir(parents=True, exist_ok=True)
    rows = []
    print(f"지식 하위질문 {len(KNOWLEDGE_SUBQ)}개 × {list(pool)} — 앵커 후보 유도\n")

    for kid, q in KNOWLEDGE_SUBQ.items():
        answers: dict[str, str] = {}
        srcs: dict[str, list] = {}
        for slug, chat in pool.items():
            try:
                a = ask(UID, q, k=5, scope="company", chat_model=chat, learn=False, record_gaps=False)
                answers[slug] = a.answer or ""
                srcs[slug] = [s.page_slug for s in a.sources]
            except Exception as e:  # noqa: BLE001
                answers[slug] = ""
                srcs[slug] = []
                print(f"  {kid} {slug}: ERR {type(e).__name__}")

        # 3술어 대조: prod(센티널) / gap(orthus 결정론 감지기) / extended(말투 보강)
        ref = {
            m: {
                "prod": refusal_prod(answers[m]),
                "gap": refusal_gap(answers[m]),
                "extended": refusal_extended(answers[m]),
            }
            for m in pool
        }
        # 실질 답변 = 두 모델 모두 확장 술어로 거절이 아님
        answerable = all(not ref[m]["extended"] and answers[m] for m in pool)
        common = terms(answers.get("baseline", "")) & terms(answers.get("solar", ""))
        rows.append({
            "kid": kid, "q": q, "answerable_both": answerable, "refusal": ref,
            "common_terms": sorted(common),
            "answers": answers, "sources": srcs,
        })
        mark = "ANS" if answerable else "REF"
        # prod 술어가 거절을 grounded로 통과시키는지 표시(F1 규모 측정)
        leak = [m for m in pool if ref[m]["extended"] and not ref[m]["prod"]]
        flag = f"  ← prod누출:{','.join(leak)}" if leak else ""
        print(f"  {mark} {kid:5} 공통항 {len(common):3} | {', '.join(sorted(common)[:8])}{flag}")

    out = RAW / "e2_anchor_probe.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ans = [r for r in rows if r["answerable_both"]]
    # F1 규모: 확장 술어로 거절인데 프로덕션 술어(센티널)는 통과시키는 콜 수
    leaks = sum(
        1 for r in rows for m in pool
        if r["refusal"][m]["extended"] and not r["refusal"][m]["prod"]
    )
    gap_miss = sum(
        1 for r in rows for m in pool
        if r["refusal"][m]["extended"] and not r["refusal"][m]["gap"]
    )
    print(f"\n== 요약 ==")
    print(f"  실질 답변 가능(두 모델 모두): {len(ans)}/{len(rows)}")
    print(f"  F1 prod 술어 누출(거절→grounded 통과): {leaks}/{len(rows) * len(pool)} 콜")
    print(f"  F2 gap 마커 누출(거절인데 gap 미감지): {gap_miss}/{len(rows) * len(pool)} 콜")
    print(f"\n  [앵커 후보 = 답변 가능 문항]")
    for r in ans:
        print(f"    {r['kid']:6} {r['q'][:38]:40} | {', '.join(r['common_terms'][:8])}")
    print(f"\n원자료: {out}")


if __name__ == "__main__":
    main()
