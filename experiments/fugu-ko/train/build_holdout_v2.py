"""D6 Phase B — 실 anchor holdout 빌더 (0706 prod 스냅샷 실질문 기반).

D5 G3 실패(F8: 골든 static이 이미 오라클)를 피하려 **신규 실로그**로 holdout을 짓는다.
0706 prod 스냅샷의 채점가능 회사지식 실질문은 ~40건뿐(query_runs 32 / chat user 49 /
assistant_command 15, 다수 개인·PII·메타)이라, owner 결정으로 **하이브리드**:
- real anchor (이 파일): 실질문 큐레이션 → 라우팅(expected route) + 일부 structured gold.
- unseen 합성 (build_unseen_v2.py): 학습에 안 쓴 held-out DB(행 충분한 것) 통째.

PII: git 커밋되는 golden/이므로 인물명은 placeholder로 치환(라우팅 신호는 '인물 조회 →
structured'라 신원 무관). 그 뒤 redact_pii_text() 통과(이메일/전화 마스킹). orthus 표준과 정합.

산출: golden/t3_v2_holdout.json (structured, gold spec) · golden/t5_v2_holdout.json (routing).
실행: PYTHONPATH=$PWD:$PWD/experiments/fugu-ko FUGU_DSN=...orthus_company_0706 \
      python experiments/fugu-ko/train/build_holdout_v2.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))  # repo root for orthus.*

from orthus.audit.redact import redact_pii_text  # noqa: E402
from t3_gold import gold_numbers  # noqa: E402

GOLDEN = ROOT / "golden"

# ── real anchor: routing (T5형, expected route 채점) ──────────────────────────
# 0706 실질문에서 큐레이션. 인물명은 placeholder(김민지/이준호)로 치환 — 라우팅은 신원 무관.
ROUTING: list[dict] = [
    # structured — 집계/목록/조회 (compiled_sql이 실제로 notion_rows 질의로 감)
    {"q": "노션에 저장된 미팅 기록(미팅 노트)의 총 건수를 세어줘", "expected": "structured", "tag": "real-count"},
    {"q": "미팅 또는 회의 기록이 총 몇 건인지 건수를 세어줘", "expected": "structured", "tag": "real-count"},
    {"q": "회사 노션에 미팅 기록이 총 몇 건 있어? 집계해서 알려줘", "expected": "structured", "tag": "real-count"},
    {"q": "미팅 기록 총 몇 건", "expected": "structured", "tag": "real-count"},
    {"q": "아크메 직원 목록", "expected": "structured", "tag": "real-list"},
    {"q": "아크메 팀원 목록", "expected": "structured", "tag": "real-list"},
    {"q": "팀원 목록", "expected": "structured", "tag": "real-list"},
    {"q": "회사에 프로젝트가 몇 개야?", "expected": "structured", "tag": "real-count"},
    {"q": "source 값이 있는 행을 source별로 묶어서 각 source 이름과 행 개수를 개수 많은 순으로 나열해줘",
     "expected": "structured", "tag": "real-meta"},
    {"q": "김민지 이메일 알려줘", "expected": "structured", "tag": "real-lookup"},
    {"q": "김민지 전화번호", "expected": "structured", "tag": "real-lookup"},
    {"q": "이준호 전화번호", "expected": "structured", "tag": "real-lookup"},
    {"q": "김민지 생일", "expected": "structured", "tag": "real-lookup"},
    {"q": "김민지 이메일 전화번호", "expected": "structured", "tag": "real-lookup"},
    # wiki — 지식 요약/설명
    {"q": "아틀라스 제품 관련해서 회사 위키에 정리된 내용 요약해줘", "expected": "wiki", "tag": "real-wiki"},
    {"q": "회사 위키에 아틀라스 제품 관련 내용이 뭐가 정리돼 있어?", "expected": "wiki", "tag": "real-wiki"},
    {"q": "회사에서 orbit 프로젝트가 뭐야? 한 문단으로 설명해줘", "expected": "wiki", "tag": "real-wiki"},
    {"q": "회사 위키에서 orbit 프로젝트를 한 줄로 정리해줘", "expected": "wiki", "tag": "real-wiki"},
    {"q": "사무소 위치 알려줘", "expected": "wiki", "tag": "real-wiki"},
    {"q": "회사 배포 프로세스가 어떻게 돼?", "expected": "wiki", "tag": "real-wiki"},
]

# ── real anchor: structured gold (T3형, 실DB number-set 채점) ─────────────────
# 실로그에서 gold가 명확한 것만. 미팅 건수 4개 패러프레이즈가 유일하게 깨끗(gold=회사 미팅 기록 count).
STRUCTURED: list[dict] = [
    {"q": "노션에 저장된 미팅 기록(미팅 노트)의 총 건수를 세어줘",
     "spec": ["회사 미팅 기록", "count", None, None], "tag": "real-count"},
    {"q": "미팅 또는 회의 기록이 총 몇 건인지 건수를 세어줘",
     "spec": ["회사 미팅 기록", "count", None, None], "tag": "real-count"},
    {"q": "회사 노션에 미팅 기록이 총 몇 건 있어? 집계해서 알려줘",
     "spec": ["회사 미팅 기록", "count", None, None], "tag": "real-count"},
    {"q": "미팅 기록 총 몇 건",
     "spec": ["회사 미팅 기록", "count", None, None], "tag": "real-count"},
]


def build() -> None:
    # structured: gold 실DB 계산 검증(빈 gold는 제외)
    t3_items = []
    for i, it in enumerate(STRUCTURED, 1):
        g = gold_numbers(*it["spec"])
        if not g or g == {0}:
            print(f"  [skip] gold 비었음: {it['q'][:30]} → {g}")
            continue
        t3_items.append({
            "id": f"t3v-{i:02d}", "q": redact_pii_text(it["q"]),
            "spec": it["spec"], "gold": sorted(g), "tag": it["tag"], "src": "real-log",
        })
    t5_items = [
        {"id": f"t5v-{i:02d}", "q": redact_pii_text(it["q"]),
         "expected": it["expected"], "tag": it["tag"], "src": "real-log"}
        for i, it in enumerate(ROUTING, 1)
    ]

    t3 = {"task": "T3_v2", "desc": "0706 실로그 structured anchor. gold=실DB number-set. redaction 통과.",
          "src": "orthus_company_0706 query_runs/chat", "items": t3_items}
    t5 = {"task": "T5_v2", "desc": "0706 실로그 routing anchor. expected route. redaction 통과.",
          "src": "orthus_company_0706 query_runs/chat/assistant_command", "items": t5_items}

    (GOLDEN / "t3_v2_holdout.json").write_text(
        json.dumps(t3, ensure_ascii=False, indent=2), encoding="utf-8")
    (GOLDEN / "t5_v2_holdout.json").write_text(
        json.dumps(t5, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[build] t3_v2 structured {len(t3_items)}문항 (gold 확인) → golden/t3_v2_holdout.json")
    print(f"[build] t5_v2 routing {len(t5_items)}문항 → golden/t5_v2_holdout.json")
    print(f"  routing 분포: " + ", ".join(
        f"{r}={sum(1 for x in t5_items if x['expected']==r)}"
        for r in ("structured", "wiki", "graph")))


if __name__ == "__main__":
    print(f"DSN={os.environ.get('FUGU_DSN', '(default orthus_company)')}")
    build()
