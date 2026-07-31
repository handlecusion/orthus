"""E6 라이브 — SVC 캐스케이드 실측 (실제 query_structured 경로, chat_model 미주입).

핸드오프: docs/cascade-verification-handoff.md / 계획: ~/.claude/plans/squishy-rolling-wren.md

핵심: `query_structured(user_id, q)`를 chat_model 주입 없이 부른다 → prod 캐스케이드가
그대로 돈다(query.py:440-441 스킵 함정 회피). 1차/2차 모델·모드는 **env로만** 지정하고,
`structured.fallback` audit span(Postgres audit_log)에서 캐스케이드 증거를 회수한다.

측정:
  t3셋(gold>0): 회수율 = 트리거 발동 후 최종 정답 / 트리거 발동. 오발동은 정의상 없음.
  TN셋(gold=0): 오발동율 = 2차 채택(adopted)으로 최종이 진짜-0을 뒤집어 비-0 오답이 된 비율.

env(래퍼가 주입):
  ORTHUS_STRUCTURED_FALLBACK_MODE=on
  ORTHUS_LLM / ORTHUS_LLM_MODEL / ORTHUS_LLM_API_KEY   (1차)
  ORTHUS_LLM_FALLBACK_PROVIDER + ORTHUS_LLM_FALLBACK_MODEL   (2차 선택)
  ORTHUS_LLM_AX_API_KEY / ORTHUS_LLM_SOLAR_API_KEY / ORTHUS_LLM_EXAONE_API_KEY (벤더 키)
  ※ provider=ax면 2차 키는 ORTHUS_LLM_AX_* 에서 온다(FALLBACK_API_KEY 아님, registry.py:266-275).

실행: (래퍼 experiments/fugu-ko/run_cascade_arm.sh 사용)
  python experiments/fugu-ko/e6_cascade.py --arm "A" --set t3 --out analysis/raw/e6_cascade_A.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")
DEMO_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")

from e1_vote import SPECS as T3_SPECS  # noqa: E402
from t3_holdout import HOLDOUT_SPECS as T3H_SPECS  # noqa: E402


def gold_numbers(db: str, kind: str, gkey, where) -> set[int]:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        w = "scope='company' AND db_name=%s" + (f" AND {where}" if where else "")
        if kind == "count":
            cur.execute(f"SELECT count(*) FROM notion_rows WHERE {w}", (db,))
            return {int(cur.fetchone()[0])}
        cur.execute(
            f"SELECT count(*) FROM notion_rows WHERE {w} GROUP BY properties->>%s", (db, gkey)
        )
        return {int(r[0]) for r in cur.fetchall()}


def model_numbers(rows) -> set[int]:
    if not rows:
        return set()
    nums: set[int] = set()
    for row in rows:
        for c in row:
            if isinstance(c, bool):
                continue
            if isinstance(c, int):
                nums.add(c)
            elif isinstance(c, str) and re.fullmatch(r"-?\d+", c.strip()):
                nums.add(int(c))
    return nums


def load_t3_questions() -> list[dict]:
    """t3.json + t3_holdout.json 중 gold 스펙 있는 문항만 (q + gold)."""
    out: list[dict] = []
    for fname, specs in [("t3.json", T3_SPECS), ("t3_holdout.json", T3H_SPECS)]:
        d = json.loads((HERE / "golden" / fname).read_text(encoding="utf-8"))
        for it in d["items"]:
            if it["id"] in specs:
                out.append({"id": it["id"], "q": it["q"], "gold": sorted(gold_numbers(*specs[it["id"]]))})
    return out


def load_tn_questions() -> list[dict]:
    d = json.loads((HERE / "golden" / "routing_holdout_tn.json").read_text(encoding="utf-8"))
    return [{"id": it["id"], "q": it["q"], "gold": []} for it in d]  # gold=진짜 0


def load_trap_questions() -> list[dict]:
    """이모지 DB 트랩 증강셋 (gold>0, 인라인 스펙)."""
    d = json.loads((HERE / "golden" / "e6_trap.json").read_text(encoding="utf-8"))
    return [
        {"id": it["id"], "q": it["q"],
         "gold": sorted(gold_numbers(it["db"], it["kind"], it["gkey"], it["where"]))}
        for it in d
    ]


def load_tn2_questions() -> list[dict]:
    """적대적 TN 확장 (count=0, gold=진짜 0)."""
    d = json.loads((HERE / "golden" / "e6_tn2.json").read_text(encoding="utf-8"))
    return [{"id": it["id"], "q": it["q"], "gold": []} for it in d]


def load_zprobe_questions() -> list[dict]:
    """zero_answer 회수 프로브 (이모지 count, gold>0 인라인)."""
    d = json.loads((HERE / "golden" / "e6_zprobe.json").read_text(encoding="utf-8"))
    return [{"id": it["id"], "q": it["q"], "gold": it["gold"]} for it in d]


def fetch_fallback_span(max_id_before: int) -> dict | None:
    """이번 호출에서 생긴 structured.fallback exit span meta (순차 실행 전제)."""
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT meta FROM audit_log WHERE node='structured.fallback' AND phase='exit' "
            "AND audit_id > %s ORDER BY audit_id DESC LIMIT 1",
            (max_id_before,),
        )
        r = cur.fetchone()
        return r[0] if r else None


def max_audit_id() -> int:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT COALESCE(max(audit_id), 0) FROM audit_log")
        return int(cur.fetchone()[0])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True)
    ap.add_argument("--set", choices=["t3", "tn", "trap", "tn2", "zprobe"], required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    # 지연 import: env 고정 후 settings 캐시.
    from orthus.structured.query import query_structured
    from orthus.models.registry import get_fallback_chat_model
    from orthus.settings import get_settings

    s = get_settings()
    print(f"[env] arm={args.arm} set={args.set}")
    print(f"[env] 1차 provider={s.llm} model={s.llm_model}")
    print(f"[env] fallback mode={s.structured_fallback_mode} provider={s.llm_fallback_provider} model={s.llm_fallback_model}")
    fb = get_fallback_chat_model()
    print(f"[env] 2차 슬롯 빌드: {'OK ' + type(fb).__name__ if fb else 'None(캐스케이드 off!)'}  model_id={getattr(fb,'model',None)}")

    questions = {"t3": load_t3_questions, "tn": load_tn_questions, "trap": load_trap_questions, "tn2": load_tn2_questions, "zprobe": load_zprobe_questions}[args.set]()
    print(f"[data] {len(questions)}문항 로드")

    out_path = HERE / args.out if not os.path.isabs(args.out) else Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fh = out_path.open("w", encoding="utf-8")

    for qi in questions:
        gold = set(qi["gold"])
        mid = max_audit_id()
        try:
            res = query_structured(DEMO_USER, qi["q"], scope="company")
            err = None
        except Exception as e:  # noqa: BLE001
            res, err = None, f"{type(e).__name__}:{e}"
        span = fetch_fallback_span(mid)
        final_nums = sorted(model_numbers(res.rows)) if res else []
        rc = res.row_count if res else None
        if args.set in ("t3", "trap", "zprobe"):
            final_correct = bool(gold and gold <= set(final_nums))
            final_zero = None
        else:
            final_correct = None
            final_zero = bool(res and ((rc or 0) == 0 or set(final_nums) == {0} or not final_nums))
        rec = {
            "id": qi["id"], "q": qi["q"], "gold": qi["gold"],
            "status": res.status if res else None, "row_count": rc,
            "final_nums": final_nums, "final_correct": final_correct, "final_zero": final_zero,
            "cascade_fired": span is not None,
            "trigger": (span or {}).get("trigger"),
            "adopted": (span or {}).get("adopted"),
            "fallback_trigger": (span or {}).get("fallback_trigger"),
            "fallback_error": (span or {}).get("fallback_error"),
            "primary_model": (span or {}).get("primary_model"),
            "fallback_model": (span or {}).get("fallback_model"),
            "error": err,
        }
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        tag = "FIRED" if span else "-----"
        extra = f"adopt={rec['adopted']} fbtrig={rec['fallback_trigger']} fberr={rec['fallback_error']}" if span else ""
        verdict = f"correct={final_correct}" if args.set in ("t3","trap","zprobe") else f"zero={final_zero}"
        print(f"  {qi['id']:9} {tag} {verdict:16} rc={rc} nums={final_nums[:6]} {extra}")
    fh.close()
    print(f"[out] {out_path}")


if __name__ == "__main__":
    main()
