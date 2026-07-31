"""T14 — distill 커버리지는 **모델 특성인가, 프롬프트 상한인가**. (T13 재실행)

> **역사 기록 (2026-07-15):** 이 측정의 P1(상한 해제) arm이 채택되어 prod distill
> 프롬프트가 됐다(`orthus/wiki/distill.py` cap 20 + 전수 추출 지시, distill→Solar 배정).
> 그 결과 아래 `_OLD_CAP_SENTENCE`("Return at most 8 claims. ")가 더는 현행 프롬프트에
> 없어 `_system_for('P1', ...)`의 assert가 깨진다 — 재실행하려면 P0 프롬프트를 literal로
> 복원해야 한다. 측정 로직 자체는 불변이며 결과는 docs/model-orchestration.md §13이 SoR.


## T13을 왜 버렸나 (재사용 금지)

T13은 결함 두 개로 무효다. 둘 다 **모델이 아니라 측정 도구**의 결함이다:

1. **회사 문서가 아니라 에이전트 대화록을 쟀다.** 문서 선택이 `ORDER BY length DESC LIMIT 25`
   였는데, 이 노드의 `documents`에는 `claude_sessions` 커넥터가 넣은 **personal scope 세션
   로그 1,228건**이 있고 그게 압도적으로 길다(최대 108,579자). 실제 company 문서는 최대
   7,473자다. → **상위 25건이 전부 내 대화 로그였다.**
2. **판정자가 원문의 5%만 봤다.** `body[:6000]` — 108k자 문서에서 앞 6천 자만 주고 "이 클레임이
   원문에 있나"를 물었다. 뒤에서 뽑힌 클레임은 **구조적으로 "지어냄"이 된다.** T13의 "오염률
   60~65%"는 모델이 아니라 이 truncation이 만든 숫자다.

T13의 P0 대조군이 t11 수치를 재현하지 못한 것(뒷받침 40% vs 100%)이 그 신호였다. **결과를
읽기 전에 자[尺]를 의심하라** — 그 규칙이 여기서 실제로 작동했다.

## T14가 그 결함을 구조적으로 막는 법

- **문서 집합을 scope='company'로 제한**하고, 선택된 doc_id를 **파일로 얼린다**
  (`t14_docset.json`). 이후 모든 단계(판정 포함)는 **그 파일만** 읽는다 → 코퍼스가 실행 중
  바뀌어도 측정이 흔들리지 않는다(T13은 실행과 판정이 각각 DB를 다시 쿼리했다).
- **판정자는 원문 전체를 본다.** company 문서 최대가 7,473자이므로 창을 16,000자로 두면
  truncation이 **구조적으로 발생할 수 없다.** 그리고 본문이 비면 **조용히 넘어가지 않고 죽는다**
  (T13은 `docs.get(id, "")`로 빈 원문을 판정에 넘겼다).

## 답하려는 질문

프로덕션 distill에는 상한이 **두 겹**으로 박혀 있고, **하한 지시는 없다**:

    _SYSTEM      "... Return at most 8 claims. ..."   ← 프롬프트 상한
    _MAX_CLAIMS = 8 ;  raw_claims[:_MAX_CLAIMS]       ← 코드 절단

"Solar로 옮기면 위키가 얇아진다"(문서당 클레임 4.8 vs 7.1)를 **모델 고유의 커버리지 열세**로
보고 owner 결정으로 올렸다. 그런데 그 4.8/7.1이 **인위적 천장에 눌린 값**이라면, 트레이드
자체가 프롬프트 산물일 수 있다. Solar의 정밀도가 40/40(오염 0%)이었다는 사실과 함께 보면
**"틀리게 뽑는 게 아니라 적게 뽑을 뿐"** 일 가능성이 있다.

## 가설과 판정선 (실행 전 고정 — 숫자를 보고 기준을 정하지 않는다)

  H-a  상한을 풀고 하한을 주면 **solar의 문서당 클레임이 오른다.**
  H-b  같은 처치에서 **baseline도 같은 비율로 오르면** 격차는 유지된다 = 커버리지는 모델 특성.
  H-c  처치가 solar의 **정밀도를 무너뜨리지 않는다.** 무너지면 커버리지를 오염으로 산 것이라
       채택 불가.

  ① 커버리지: P1에서 **solar/baseline ≥ 90%**
  ② 정밀도:   P1 solar의 supported 비율이 **P0 solar 대비 5%p 초과 하락 없음**
  ③ 지연:     P1 solar가 **baseline(P0, 현행)의 2배 이내**

  ①②③ 모두 통과 → "커버리지 트레이드는 **프롬프트 산물**" → owner 결정 D1/D2 소멸
  ①만 실패     → "커버리지는 **모델 특성**"            → 기존 결론(트레이드) 강화
  ②가 실패     → "커버리지를 **오염으로 지불**"        → 채택 불가

**무효 조건:** 어느 arm이든 파싱 실패 > 5문서(20%). 또는 P1에서 상한(20)에 붙는 문서가
발생하면 처치가 다시 천장에 눌린 것이므로 상한을 올려 재실행한다.

## 변인 통제

- **문서 25건 동일**(얼린 docset) · 프롬프트 외 전부 고정 · 같은 프로덕션 함수
  (`distill_document`) · 같은 어댑터.
- P0는 **현행 프롬프트와 바이트 동일**. P1은 상한 문장만 교체하고 나머지는 글자 단위로 같다 —
  슬러그 규칙·엔티티·open_question이 같이 바뀌면 변인이 둘이 된다.
- 프롬프트 상한과 **코드 절단을 함께** 푼다. 하나만 풀면 처치가 무의미하다.

읽기 전용: `distill_document`는 문서 row를 SELECT할 뿐 쓰지 않는다(쓰기는 별도 직렬 단계).
실행 전후로 **company scope** wiki_pages count 불변을 확인한다(personal scope는 이 노드의
다른 프로세스가 쓰고 있어 총계 가드는 무용하다 — T13에서 확인).

    python t14_distill_cap.py --preflight      # 무엇을 잴지 눈으로 확인만
    python t14_distill_cap.py                  # P0,P1 × solar,baseline × 25문서
    python t14_distill_cap.py --score-only
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
DOCSET = RAW / "t14_docset.json"
N_DOCS = 25
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")

P1_CAP = 20

_OLD_CAP_SENTENCE = "Return at most " "8 claims. "
_NEW_CAP_SENTENCE = (
    "Extract EVERY verifiable claim the document supports — do not stop early, "
    "and do not summarize several facts into one claim. Prefer more atomic claims "
    "over fewer compound ones. "
    f"Return at most {P1_CAP} claims. "
)

ARMS = ("P0", "P1")
MODELS = ("solar", "baseline")


def _system_for(arm: str, base: str) -> str:
    if arm == "P0":
        return base
    assert _OLD_CAP_SENTENCE in base, "현행 프롬프트에서 상한 문장을 못 찾았다 — 프롬프트가 바뀌었나?"
    return base.replace(_OLD_CAP_SENTENCE, _NEW_CAP_SENTENCE)


def select_docs() -> list[dict]:
    """company scope 실문서만. 선택 결과를 **얼려서** 이후 단계가 DB를 다시 안 보게 한다."""
    import psycopg

    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute(
            "SELECT doc_id, user_id, title, source, markdown FROM documents "
            "WHERE scope = 'company' AND length(markdown) > 600 "
            "ORDER BY length(markdown) DESC LIMIT %s",
            (N_DOCS,),
        )
        rows = cur.fetchall()
    docs = [
        {
            "doc_id": str(d), "user_id": str(u), "title": t, "source": s, "markdown": m,
        }
        for d, u, t, s, m in rows
    ]
    assert docs, "company scope 문서를 하나도 못 찾았다"
    bad = [d for d in docs if d["source"] not in ("notion", "slack")]
    assert not bad, f"회사 코퍼스가 아닌 소스가 섞였다: {[d['source'] for d in bad]}"
    return docs


def load_docset() -> list[dict]:
    assert DOCSET.exists(), f"{DOCSET} 없음 — 먼저 --preflight 또는 본실행으로 docset을 얼려라"
    return json.loads(DOCSET.read_text(encoding="utf-8"))


def _company_wiki_pages() -> int:
    import psycopg

    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM wiki_pages WHERE scope = 'company'")
        return cur.fetchone()[0]


def preflight() -> list[dict]:
    docs = select_docs()
    RAW.mkdir(parents=True, exist_ok=True)
    DOCSET.write_text(json.dumps(docs, ensure_ascii=False), encoding="utf-8")
    longest = max(len(d["markdown"]) for d in docs)
    print(f"\n[docset] company 문서 {len(docs)}건을 얼렸다 → {DOCSET.name}")
    print(f"[docset] 최장 본문 {longest:,}자 — 판정자 창(16,000자) 안이라 truncation 불가능 ✅\n")
    print("  | # | source | 길이 | 제목 |")
    print("  |---|--------|------|------|")
    for i, d in enumerate(docs, 1):
        print(f"  | {i:2} | {d['source']:6} | {len(d['markdown']):5,} | {d['title'][:52]} |")
    assert longest < 16000, "판정자 창을 넘는 문서가 있다 — 창을 올려라"
    return docs


def run() -> None:
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parent.parent))
    from pool import build_pool

    from orthus.wiki import distill as D

    docs = preflight()
    base_system, base_cap = D._SYSTEM, D._MAX_CLAIMS
    pool = build_pool(list(MODELS))

    before = _company_wiki_pages()
    print(f"\n[guard] company wiki_pages before = {before}\n")

    for arm in ARMS:
        D._SYSTEM = _system_for(arm, base_system)
        D._MAX_CLAIMS = base_cap if arm == "P0" else P1_CAP
        print(f"── arm {arm}: cap={D._MAX_CLAIMS} · floor={'있음' if arm == 'P1' else '없음'}")
        for slug in MODELS:
            chat = pool[slug]
            with (RAW / f"t14_{arm}_{slug}.jsonl").open("w", encoding="utf-8") as fh:
                for n, d in enumerate(docs, 1):
                    t0 = time.monotonic()
                    rec: dict = {"arm": arm, "model": slug, "doc_id": d["doc_id"], "title": d["title"]}
                    try:
                        from uuid import UUID

                        r = D.distill_document(UUID(d["doc_id"]), UUID(d["user_id"]), chat_model=chat)
                        rec["claims"] = [
                            {"claim": c.claim, "evidence": c.evidence, "confidence": c.confidence}
                            for c in (r.claims or [])
                        ]
                        rec["ok"] = True
                    except Exception as e:  # noqa: BLE001
                        rec |= {"ok": False, "claims": [], "error": f"{type(e).__name__}: {str(e)[:120]}"}
                    rec["ms"] = int((time.monotonic() - t0) * 1000)
                    rec["at_cap"] = len(rec["claims"]) >= D._MAX_CLAIMS
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                    print(
                        f"  {arm} {slug:9} {n:2}/{len(docs)} claims={len(rec['claims']):2}"
                        f"{' [CAP]' if rec['at_cap'] else ''} {rec['ms']}ms",
                        flush=True,
                    )
    D._SYSTEM, D._MAX_CLAIMS = base_system, base_cap

    after = _company_wiki_pages()
    print(f"\n[guard] company wiki_pages after = {after} — {'불변 ✅' if after == before else '변함 ❌'}")


def score() -> None:
    print("\n══ T14 — distill 커버리지: 모델 특성인가 프롬프트 상한인가 ══")
    print("   (company scope 실문서 25건 · 얼린 docset · 판정자는 원문 전체를 본다)\n")
    print("  | arm | 모델      | 문서당 클레임 | 상한에 붙음 | 실패 | p50 지연 |")
    print("  |-----|-----------|--------------|------------|------|----------|")
    avg: dict = {}
    for arm in ARMS:
        for m in MODELS:
            p = RAW / f"t14_{arm}_{m}.jsonl"
            if not p.exists():
                continue
            rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
            ok = [r for r in rows if r.get("ok")]
            fail = len(rows) - len(ok)
            n = [len(r["claims"]) for r in ok]
            a = sum(n) / len(n) if n else 0.0
            avg[(arm, m)] = a
            capped = sum(1 for r in ok if r.get("at_cap"))
            ms = sorted(r["ms"] for r in ok)
            p50 = ms[len(ms) // 2] if ms else 0
            warn = " ⚠무효(실패>5)" if fail > 5 else ""
            if arm == "P1" and capped:
                warn += " ⚠P1이 상한에 붙었다 — 상한을 올려 재실행"
            print(
                f"  | {arm}  | {m:9} | {a:12.1f} | {capped:2}/{len(ok):2} 문서 |  {fail:2}  |"
                f" {p50 / 1000:6.1f}s |{warn}"
            )

    if ("P1", "solar") in avg and ("P1", "baseline") in avg:
        s0, s1 = avg.get(("P0", "solar"), 0), avg[("P1", "solar")]
        b0, b1 = avg.get(("P0", "baseline"), 0), avg[("P1", "baseline")]
        print("\n  ── 판정 (사전 고정 기준) ──")
        if s0:
            print(f"  solar     {s0:.1f} → {s1:.1f}  ({(s1 / s0 - 1) * 100:+.0f}%)")
        if b0:
            print(f"  baseline  {b0:.1f} → {b1:.1f}  ({(b1 / b0 - 1) * 100:+.0f}%)")
        r0 = s0 / b0 * 100 if b0 else 0
        r1 = s1 / b1 * 100 if b1 else 0
        print(f"\n  solar/baseline:  P0 {r0:.0f}%  →  P1 {r1:.0f}%")
        print(f"  ① 커버리지 게이트 (P1 ≥ 90%): {'PASS' if r1 >= 90 else 'FAIL'}")
        print("  ②③ 정밀도·지연은 t14_judge.py로 판정한다.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preflight", action="store_true", help="무엇을 잴지 확인만 하고 종료")
    ap.add_argument("--score-only", action="store_true")
    a = ap.parse_args()
    if a.preflight:
        preflight()
        return
    if not a.score_only:
        run()
    score()


if __name__ == "__main__":
    main()
