"""T13 — distill 커버리지는 **모델 특성인가, 프롬프트 상한인가**.

배경: t11에서 문서당 클레임이 baseline 7.1 / exaone 6.5 / solar 4.8 / ax 3.0으로 나왔고,
"Solar로 옮기면 위키가 32% 얇아진다"를 **모델 고유의 커버리지 열세**로 보고 owner 결정으로
올렸다. 그런데 프로덕션 distill에는 상한이 **두 겹**으로 박혀 있다:

    orthus/wiki/distill.py
      _SYSTEM      "... Return at most 8 claims. ..."   ← 프롬프트 상한
      _MAX_CLAIMS = 8 ;  raw_claims[:_MAX_CLAIMS]       ← 코드 절단

그리고 **하한 지시가 없다** — "빠짐없이 뽑아라"는 말이 프롬프트 어디에도 없다. baseline은
상한 8에 7.1로 거의 붙어 있고(89%), solar는 4.8이다. 그렇다면 4.8은 "덜 캐는 모델"이 아니라
**"지시받지 않은 것을 안 한 모델"**일 수 있다 — 특히 solar의 정밀도가 40/40(오염 0%)이었다는
사실과 함께 보면 그렇다. **틀리게 뽑는 게 아니라 적게 뽑을 뿐이다.**

가설:
  H-T13a  상한을 풀고 하한(빠짐없이)을 주면 **solar의 문서당 클레임이 오른다.**
  H-T13b  같은 처치에서 **baseline도 오르면** 격차는 유지된다(= 커버리지는 모델 특성).
  H-T13c  처치가 solar의 **정밀도를 무너뜨리지 않는다**(오염 0% 유지) — 이게 무너지면
          커버리지를 산 대가로 위키를 오염시키는 것이라 채택 불가.

판정선 (실행 전 고정 — 숫자를 보고 기준을 정하지 않는다):
  ① solar 문서당 클레임이 **baseline의 90% 이상**(P1 arm 내 비교)  AND
  ② solar 정밀도(supported 비율)가 **P0 대비 하락 5%p 이내**       AND
  ③ solar 문서당 지연이 **baseline의 2배 이내**(현재 7.3s vs 13.0s — 여유 큼)
  → 셋 다 통과: "커버리지 트레이드는 **프롬프트 산물**이었다" → owner 결정 D1/D2 소멸
  → ①만 실패: "커버리지는 **모델 특성**" → 기존 결론(트레이드) 강화, owner 결정 유지
  → ②가 실패: "커버리지는 살 수 있으나 **오염을 지불**해야 한다" → 채택 불가, 트레이드 유지

무효 조건: 어느 arm이든 파싱 실패가 5문서(20%)를 넘으면 그 arm은 무효.

변인 통제:
  - **문서 25건 동일**(t11과 같은 결정론 쿼리 — 본문 길이 내림차순 상위 25).
  - **프롬프트 외 전부 고정**: 같은 프로덕션 함수(`distill_document`), 같은 어댑터, 같은 온도.
  - P0(대조)는 **현행 프롬프트 그대로** — t11 수치를 재현해야 한다(재현 안 되면 환경 결함).
  - 절단 상한(`_MAX_CLAIMS`)도 P1에서 함께 푼다. **프롬프트만 풀고 코드가 자르면 처치가 무의미**.
  - 판정은 t11과 **같은 원문-읽는 판정자**(`t11_judge.py`)를 그대로 쓴다.

읽기 전용: `distill_document`는 문서 row를 SELECT할 뿐 아무것도 쓰지 않는다(쓰기는 별도 직렬
단계). 실행 전후로 `wiki_pages` count를 찍어 불변을 확인한다.

    python t13_distill_cap.py                          # P0,P1 × solar,baseline × 25문서
    python t13_distill_cap.py --models solar,baseline,exaone
    python t13_distill_cap.py --score-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "analysis" / "raw"
N_DOCS = 25
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")

# ── arm 정의 ────────────────────────────────────────────────────────────────
# P1 = 상한 해제 + 하한(빠짐없이) 지시. 그 외 문장은 현행과 **글자 단위로 동일**하게 둔다 —
# 커버리지 이외의 것(슬러그 규칙·엔티티·open_question)이 바뀌면 변인이 둘이 된다.
P1_CAP = 20  # 상한을 없애지 않고 넉넉히 올린다(무한 상한은 장문 폭주/토큰 낭비 위험).

_OLD_CAP_SENTENCE = "Return at most " "8 claims. "
_NEW_CAP_SENTENCE = (
    "Extract EVERY verifiable claim the document supports — do not stop early, "
    "and do not summarize several facts into one claim. Prefer more atomic claims "
    "over fewer compound ones. "
    f"Return at most {P1_CAP} claims. "
)

ARMS = ("P0", "P1")


def _system_for(arm: str, base: str) -> str:
    if arm == "P0":
        return base
    assert _OLD_CAP_SENTENCE in base, "현행 프롬프트에서 상한 문장을 못 찾았다 — 프롬프트가 바뀌었나?"
    return base.replace(_OLD_CAP_SENTENCE, _NEW_CAP_SENTENCE)


def _docs() -> list[tuple]:
    import psycopg

    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute(
            "SELECT doc_id, user_id, title, markdown FROM documents "
            "WHERE length(markdown) > 600 ORDER BY length(markdown) DESC LIMIT %s",
            (N_DOCS,),
        )
        return cur.fetchall()


def _wiki_page_count() -> int:
    import psycopg

    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM wiki_pages")
        return cur.fetchone()[0]


def run(models: list[str], arms: list[str]) -> None:
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parent.parent))
    from pool import build_pool

    from orthus.wiki import distill as D

    base_system = D._SYSTEM
    base_cap = D._MAX_CLAIMS
    pool = build_pool(models)
    docs = _docs()
    RAW.mkdir(parents=True, exist_ok=True)

    before = _wiki_page_count()
    print(f"[guard] wiki_pages before = {before}\n")

    for arm in arms:
        # 프롬프트 상한과 코드 절단을 **함께** 푼다. 하나만 풀면 처치가 무의미하다.
        D._SYSTEM = _system_for(arm, base_system)
        D._MAX_CLAIMS = base_cap if arm == "P0" else P1_CAP
        print(f"── arm {arm}: cap={D._MAX_CLAIMS} · floor={'있음' if arm == 'P1' else '없음'}")
        for slug in models:
            chat = pool[slug]
            out = RAW / f"t13_{arm}_{slug}.jsonl"
            with out.open("w", encoding="utf-8") as fh:
                for n, (doc_id, user_id, title, md) in enumerate(docs, 1):
                    t0 = time.monotonic()
                    rec: dict = {"arm": arm, "model": slug, "doc_id": str(doc_id), "title": title}
                    try:
                        r = D.distill_document(doc_id, user_id, chat_model=chat)
                        rec["claims"] = [
                            {"claim": c.claim, "evidence": c.evidence, "confidence": c.confidence}
                            for c in (r.claims or [])
                        ]
                        rec["ok"] = True
                    except Exception as e:  # noqa: BLE001
                        rec |= {"ok": False, "claims": [], "error": f"{type(e).__name__}: {str(e)[:120]}"}
                    rec["ms"] = int((time.monotonic() - t0) * 1000)
                    # 상한이 실제로 물렸는가 — P0에서 baseline이 8에 붙어 잘렸다면 격차는
                    # 우리가 잰 것보다 더 크다(절단된 만큼 안 보였다).
                    rec["at_cap"] = len(rec["claims"]) >= D._MAX_CLAIMS
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                    print(
                        f"  {arm} {slug:9} {n:2}/{len(docs)} claims={len(rec['claims']):2}"
                        f"{' [CAP]' if rec['at_cap'] else ''} {rec['ms']}ms",
                        flush=True,
                    )
    D._SYSTEM, D._MAX_CLAIMS = base_system, base_cap

    after = _wiki_page_count()
    print(f"\n[guard] wiki_pages after = {after} — {'불변 ✅' if after == before else '변함 ❌ 조사 필요'}")


def score(models: list[str], arms: list[str]) -> None:
    print("\n══ T13 — distill 커버리지: 모델 특성인가 프롬프트 상한인가 ══\n")
    print("  | arm | 모델      | 문서당 클레임 | 상한에 붙음 | 실패 | p50 지연 |")
    print("  |-----|-----------|--------------|------------|------|----------|")
    table: dict = {}
    for arm in arms:
        for m in models:
            p = RAW / f"t13_{arm}_{m}.jsonl"
            if not p.exists():
                continue
            rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
            ok = [r for r in rows if r.get("ok")]
            fail = len(rows) - len(ok)
            n = [len(r["claims"]) for r in ok]
            avg = sum(n) / len(n) if n else 0.0
            capped = sum(1 for r in ok if r.get("at_cap"))
            ms = sorted(r["ms"] for r in ok)
            p50 = ms[len(ms) // 2] if ms else 0
            table[(arm, m)] = avg
            flag = " ⚠무효" if fail > 5 else ""
            print(
                f"  | {arm}  | {m:9} | {avg:12.1f} | {capped:2}/{len(ok):2} 문서 |  {fail:2}  |"
                f" {p50 / 1000:6.1f}s |{flag}"
            )

    if ("P1", "solar") in table and ("P1", "baseline") in table:
        s1, b1 = table[("P1", "solar")], table[("P1", "baseline")]
        s0 = table.get(("P0", "solar"), 0.0)
        b0 = table.get(("P0", "baseline"), 0.0)
        print("\n  ── 판정 (사전 고정 기준) ──")
        print(f"  solar   {s0:.1f} → {s1:.1f}   ({(s1 / s0 - 1) * 100:+.0f}%)" if s0 else "")
        print(f"  baseline{b0:.1f} → {b1:.1f}   ({(b1 / b0 - 1) * 100:+.0f}%)" if b0 else "")
        ratio = s1 / b1 if b1 else 0
        print(f"\n  ① solar/baseline (P1 내) = {ratio * 100:.0f}%  →  {'PASS ≥90%' if ratio >= 0.90 else 'FAIL <90%'}")
        print("  ② 정밀도는 t11_judge.py 재실행으로 판정한다(원문을 읽는 판정자).")
        print("     python t11_judge.py --files t13_P1_solar,t13_P1_baseline")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="solar,baseline")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--score-only", action="store_true")
    a = ap.parse_args()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    arms = [x.strip() for x in a.arms.split(",") if x.strip()]
    if not a.score_only:
        run(models, arms)
    score(models, arms)


if __name__ == "__main__":
    main()
