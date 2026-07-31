"""distill 측정 — 위키를 저술하는 모델이 **원문에 없는 것을 지어내는가**.

distill은 토큰 지출을 지배하고(문서마다 1회 × 전 임포트) **다른 모든 작업이 읽는 위키를
저술**한다. 그래서 여기서 중요한 건 "잘 요약하나"가 아니라 **"오염시키나"** 다 — 환각한 클레임
하나가 위키에 박히면 그 뒤의 모든 답변이 그걸 근거로 인용한다.

**코드는 건드리지 않는다.** `distill_document(doc_id, user_id, chat_model=...)` 은 이미 주입을
받고, 내부는 문서 row를 SELECT할 뿐 **아무것도 쓰지 않는다**(쓰기는 별도 직렬 단계). 따라서 실
company DB에 대고 읽기 전용으로 안전하게 돌 수 있다.

채점 (골드 라벨 없이 객관):
- **엔티티 환각률** — 추출한 엔티티 이름이 원문에 실제로 등장하는가(정규화 후 부분일치).
  등장하지 않으면 지어낸 것이다.
- **클레임 근거 이탈률** — 클레임의 evidence 텍스트가 원문에서 뒷받침되는가(토큰 중복률).
- **파싱 실패** — 프로덕션은 `_JSON_RETRIES` 후 빈 결과로 떨어진다(= 그 문서는 위키에 안 실림).
- 출력량/지연 — 원가 대리 지표.

    python t11_distill.py            # 25문서 × 4모델
    python t11_distill.py --score-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "analysis" / "raw"
MODELS = ["solar", "ax", "exaone", "baseline"]
N_DOCS = 25


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"\s+", "", s)


def _toks(s: str) -> set[str]:
    return {t for t in re.split(r"[^0-9A-Za-z가-힣]+", (s or "").casefold()) if len(t) > 1}


def _docs() -> list[tuple]:
    import psycopg

    dsn = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute(
            "SELECT doc_id, user_id, title, markdown FROM documents "
            "WHERE length(markdown) > 600 ORDER BY length(markdown) DESC LIMIT %s",
            (N_DOCS,),
        )
        return cur.fetchall()


def run(models: list[str]) -> None:
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parent.parent))
    from pool import build_pool

    from orthus.wiki.distill import distill_document

    pool = build_pool(models)
    docs = _docs()
    RAW.mkdir(parents=True, exist_ok=True)
    for slug in models:
        chat = pool[slug]
        with (RAW / f"t11_{slug}.jsonl").open("w", encoding="utf-8") as fh:
            for n, (doc_id, user_id, title, md) in enumerate(docs, 1):
                t0 = time.monotonic()
                rec: dict = {"doc_id": str(doc_id), "title": title}
                try:
                    r = distill_document(doc_id, user_id, chat_model=chat)
                    rec["claims"] = [
                        {
                            "claim": c.claim,
                            "evidence": c.evidence,
                            "confidence": c.confidence,
                            "pages": list(c.related_pages or []),
                        }
                        for c in (r.claims or [])
                    ]
                    rec["entities"] = [{"kind": e.kind, "name": e.name} for e in (r.entities or [])]
                    rec["ok"] = True
                except Exception as e:  # noqa: BLE001
                    rec |= {"ok": False, "claims": [], "entities": [], "error": f"{type(e).__name__}: {str(e)[:120]}"}
                rec["ms"] = int((time.monotonic() - t0) * 1000)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                print(f"  {slug:9} {n:2}/{len(docs)} claims={len(rec['claims'])} ent={len(rec['entities'])}", flush=True)


def score(models: list[str]) -> None:
    docs = {str(d[0]): d[3] for d in _docs()}
    print(f"\n  ══ distill ({len(docs)}문서 — 회사 코퍼스에서 본문이 가장 두꺼운 것들) ══")
    print("  ⚠ 핵심 지표는 '환각' — 원문에 없는 걸 지어내면 위키가 오염되고, 그 뒤 모든 답변이 그걸 인용한다.\n")
    for m in models:
        p = RAW / f"t11_{m}.jsonl"
        if not p.exists():
            continue
        fail = 0
        e_tot = e_bad = 0
        c_tot = c_bad = 0
        claims_n = []
        ms = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            body = docs.get(r["doc_id"], "")
            nb, tb = _norm(body), _toks(body)
            if not r.get("ok"):
                fail += 1
                continue
            claims_n.append(len(r["claims"]))
            ms.append(r["ms"])
            for e in r["entities"]:
                e_tot += 1
                if _norm(e["name"]) not in nb:  # 원문에 이름 자체가 없다 → 지어낸 엔티티
                    e_bad += 1
            for c in r["claims"]:
                c_tot += 1
                ct = _toks(c.get("evidence") or c.get("claim") or "")
                # evidence 토큰의 절반도 원문에 없으면 근거 이탈로 센다
                if ct and len(ct & tb) / len(ct) < 0.5:
                    c_bad += 1
        ep = e_bad / e_tot * 100 if e_tot else 0
        cp = c_bad / c_tot * 100 if c_tot else 0
        avg = sum(claims_n) / len(claims_n) if claims_n else 0
        med = sorted(ms)[len(ms) // 2] if ms else 0
        print(
            f"  {m:9} 실패 {fail:2} | 엔티티 {e_tot:3}개 중 원문에 없음 {e_bad:3} (**{ep:4.1f}%**) "
            f"| 클레임 {c_tot:3}개 중 근거이탈 {c_bad:3} (**{cp:4.1f}%**) "
            f"| 문서당 클레임 {avg:.1f} | p50 {med}ms"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--score-only", action="store_true")
    a = ap.parse_args()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    if not a.score_only:
        run(models)
    score(models)


if __name__ == "__main__":
    main()
