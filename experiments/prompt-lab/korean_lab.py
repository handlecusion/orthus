"""미측정 영역 실험 — 순한글(한글비율 0.9+) 코퍼스에서 "한국어모델–한국어wiki–
한국어지시" 3박자 조건 검증.

실제 회사 코퍼스는 한글 15.9%/영어 48.2% 혼합이라 이 조건이 존재하지 않는다.
합성 순한글 코퍼스로 kr-v1(한국어 지시문)이 distill/wiki_qa에서 이기는지 잰다.
합성이므로 prod 대표성은 없고, 오직 언어-정합 가설의 조건부 성립 여부만 답한다.

서브커맨드:
  gen-corpus   gpt-4o로 순한글 회사 문서 생성 → data/korean_sample.jsonl
  gen-q        distill 결과 claim에서 한국어 질문 역생성 → data/ko_golden.jsonl
  build-hits   claim으로 합성 frozen hits(정답+distractor) → data/ko_hits.jsonl
  answer       ko_hits 위에서 wiki_qa 변형 실행 → analysis/raw/ko_qa_{variant}.jsonl
  judge        두 변형 쌍대 판정(익명 A/B 스왑, 근거 동봉)

distill A/B는 기존 distill_harness --sample data/korean_sample.jsonl --tag ko 재사용.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent
RAW_DIR = LAB_DIR / "analysis" / "raw"
DATA_DIR = LAB_DIR / "data"
SEED_USER = uuid.UUID("00000000-0000-4000-8000-000000000001")
_NS = uuid.UUID("11111111-2222-3333-4444-555555555555")

# 순한글 회사 문서 주제 시드 (영상/아틀라스 제작사 맥락, 전부 한국어로 작성 지시).
_TOPICS = [
    ("회의록", "주간 제작 회의에서 나온 결정사항과 액션아이템"),
    ("정책", "출연자 정산 및 지급 정책"),
    ("정책", "촬영 현장 안전 수칙과 사고 대응 절차"),
    ("프로젝트현황", "신규 드라마 아틀라스 진행 상황과 남은 배역"),
    ("프로젝트현황", "예능 파일럿 촬영 일정과 장소 섭외 현황"),
    ("인사", "신입 조연출 온보딩 절차와 담당자"),
    ("인사", "프리랜서 스태프 계약 갱신 기준"),
    ("제품", "영상 편집 협업 도구의 새 기능과 사용법"),
    ("제품", "출연자 관리 시스템의 배포 일정과 개선사항"),
    ("파트너", "제작 협력사와의 공동 제작 조건 합의 내용"),
    ("일정", "이번 분기 주요 촬영 및 납품 일정표"),
    ("회의록", "월간 예산 점검 회의 요약"),
    ("정책", "저작권 및 초상권 관리 지침"),
    ("프로젝트현황", "다큐멘터리 후반 작업 진행률과 병목"),
    ("인사", "팀별 인원 구성과 역할 분담 현황"),
]

_CORPUS_SYSTEM = (
    "너는 영상·아틀라스 제작사의 내부 문서를 쓰는 직원이다. 주어진 주제로 실제 사내 "
    "문서 하나를 자연스러운 한국어로 작성하라. 규칙: (1) 영어 단어·약어를 쓰지 마라 "
    "— 고유명사나 도구 이름도 한글로 표기하라 (2) 구체적인 사실을 담아라: 날짜, "
    "인원수, 금액, 담당자 이름(가상), 상태 값 (3) {length}. 제목 한 줄 + 본문. "
    "마크다운 목록을 써도 된다."
)

_LEN_HINT = {"short": "6~10문장 분량의 짧은 문서", "medium": "3~4개 소제목이 있는 중간 길이 문서"}


def _pool_chat(slug: str):
    import pool

    if slug.startswith("gpt"):
        key = os.environ.get("JUDGE_OPENAI_KEY", "").strip()
        spec = pool.WorkerSpec("gptx", provider_match="", base_url="https://api.openai.com/v1", model=slug, timeout=90)
        return pool.WorkerChat(spec, key, slug)
    return pool.build_pool([slug])[slug]


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _append(path: Path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _hangul_ratio(s: str) -> float:
    if not s:
        return 0.0
    h = sum(1 for c in s if "가" <= c <= "힣")
    alnum = sum(1 for c in s if c.isalnum() or "가" <= c <= "힣")
    return h / max(alnum, 1)


def cmd_gen_corpus(args: argparse.Namespace) -> None:
    chat = _pool_chat("gpt-4o")
    out = DATA_DIR / "korean_sample.jsonl"
    done = len(_load(out))
    n = 0
    target = args.n
    idx = done
    while n < target - done and idx < target * 2:
        topic, seed = _TOPICS[idx % len(_TOPICS)]
        length = "medium" if idx % 3 == 0 else "short"
        sys_p = _CORPUS_SYSTEM.replace("{length}", _LEN_HINT[length])
        try:
            body = chat.complete(sys_p, f"주제 분류: {topic}\n구체 주제: {seed} (변형 {idx})")
        except Exception as exc:
            print("  gen fail:", exc)
            idx += 1
            continue
        ratio = _hangul_ratio(body)
        if ratio < 0.80:  # 순한글 조건 미달 문서는 버린다
            idx += 1
            continue
        lines = body.strip().split("\n", 1)
        title = lines[0].lstrip("# ").strip()[:120]
        doc_id = str(uuid.uuid5(_NS, f"ko-{idx}"))
        rec = {
            "doc_id": doc_id,
            "title": title,
            "markdown": body.strip(),
            "source": "synthetic_ko",
            "project": "company",
            "len": len(body),
            "bucket": 2 if length == "short" else 3,
            "hangul": round(ratio, 3),
        }
        _append(out, rec)
        n += 1
        idx += 1
        print(f"[{done + n}] {title[:30]} 한글={ratio:.2f} {len(body)}자")
    rows = _load(out)
    import statistics

    print(f"corpus total {len(rows)} docs, 한글비율 median={statistics.median(r['hangul'] for r in rows):.2f} "
          f"min={min(r['hangul'] for r in rows):.2f}")


_GENQ = (
    "너는 사내 지식베이스 평가 문항 작성자다. 아래 claim 목록으로 답할 수 있는 실제 "
    "직원이 물을 법한 한국어 질문 1개를 작성하라. 규칙: 제목/claim의 단어를 그대로 "
    "베끼지 말고 자연스럽게, 15~30자. claim에 실제 답이 있는 질문만. "
    'JSON만 출력: {"q": "..."}'
)


def cmd_gen_q(args: argparse.Namespace) -> None:
    runs = {r["doc_id"]: r for r in _load(RAW_DIR / "distill_baseline_solar_ko.jsonl") if "parsed" in r}
    corpus = {r["doc_id"]: r for r in _load(DATA_DIR / "korean_sample.jsonl")}
    chat = _pool_chat("gpt-4o")
    out = DATA_DIR / "ko_golden.jsonl"
    done = {r["doc_id"] for r in _load(out)}
    n = 0
    for did, run in runs.items():
        if did in done:
            continue
        claims = run["parsed"]["claims"]
        if len(claims) < 3:
            continue
        claim_txt = "\n".join(f"- {c['claim']}" for c in claims[:8])
        try:
            q = json.loads(chat.complete(_GENQ, claim_txt, json_only=True)).get("q", "").strip()
        except Exception:
            continue
        if not q:
            continue
        _append(out, {"doc_id": did, "q": q, "title": corpus[did]["title"], "page_slug": claims[0]["page_slug"]})
        n += 1
    print(f"ko golden: +{n} (total {len(_load(out))})")


def cmd_build_hits(args: argparse.Namespace) -> None:
    runs = {r["doc_id"]: r for r in _load(RAW_DIR / "distill_baseline_solar_ko.jsonl") if "parsed" in r}
    golden = _load(DATA_DIR / "ko_golden.jsonl")
    all_ids = list(runs)
    out = DATA_DIR / "ko_hits.jsonl"
    done = {r["id"] for r in _load(out)}
    n = 0
    for gi, g in enumerate(golden):
        rid = g["doc_id"]
        if rid in done:
            continue
        own = runs[rid]["parsed"]["claims"][:3]
        # distractor: 다른 문서 claim 2개 (결정론 회전)
        distract = []
        for off in (1, 2):
            odid = all_ids[(all_ids.index(rid) + off) % len(all_ids)]
            oc = runs[odid]["parsed"]["claims"]
            if oc:
                distract.append((odid, oc[0]))
        hits = []
        for c in own:
            hits.append({
                "page_slug": c["page_slug"], "title": g["title"], "kind": "claim",
                "excerpt": c["claim"] + (f" ({c['evidence']})" if c.get("evidence") else ""),
                "score": 0.8, "provenance": [], "source_scope": "company", "source_node_id": None,
            })
        for odid, c in distract:
            hits.append({
                "page_slug": c["page_slug"], "title": "", "kind": "claim",
                "excerpt": c["claim"], "score": 0.5, "provenance": [],
                "source_scope": "company", "source_node_id": None,
            })
        avg_ko = sum(_hangul_ratio(h["excerpt"]) for h in hits) / len(hits)
        _append(out, {"id": rid, "q": g["q"], "hits": hits, "hits_hangul": round(avg_ko, 3)})
        n += 1
    rows = _load(out)
    import statistics
    print(f"ko hits: +{n} (total {len(rows)}) 근거 한글비율 median={statistics.median(r['hits_hangul'] for r in rows):.2f}")


def cmd_answer(args: argparse.Namespace) -> None:
    from orthus.schemas.canonical import WikiSourceRef
    from orthus.wiki import qa as qa_mod

    from variants_qa import VARIANTS

    v = VARIANTS[args.variant]
    if v.get("system"):
        qa_mod._SYSTEM = v["system"]
    if v.get("system_append"):
        qa_mod._SYSTEM = qa_mod._SYSTEM + v["system_append"]

    chat = _pool_chat(args.model)
    out = RAW_DIR / f"ko_qa_{args.variant}_{args.model}.jsonl"
    done = {r["id"] for r in _load(out)}
    n = 0
    for row in _load(DATA_DIR / "ko_hits.jsonl"):
        if row["id"] in done:
            continue
        refs = [WikiSourceRef.model_validate(h) for h in row["hits"]]
        try:
            ans = qa_mod.answer_from_hits(SEED_USER, row["q"], refs, chat_model=chat, learn=False, record_gaps=False)
            _append(out, {"id": row["id"], "q": row["q"], "answer": ans.answer})
        except Exception as exc:
            _append(out, {"id": row["id"], "q": row["q"], "error": str(exc)})
        n += 1
    print(f"ko answer {args.variant}: +{n}")


_JUDGE = (
    "너는 사내 위키 어시스턴트 답변 품질 심사자다. 질문, 위키 근거, 익명의 두 답변 "
    "A/B를 받는다. 근거에 실제로 있는 내용만 좋은 답으로 인정하라 — 근거에 없는 단정은 "
    "감점, 근거에 있는데 회피해도 감점. 유용성(구체성·정확성·간결성)을 비교하라. "
    'JSON만 출력: {"winner": "A"|"B"|"tie", "why": "한 줄"}'
)


def cmd_judge(args: argparse.Namespace) -> None:
    import significance

    hits = {r["id"]: r for r in _load(DATA_DIR / "ko_hits.jsonl")}
    A = {r["id"]: r for r in _load(RAW_DIR / f"ko_qa_{args.a}_{args.model}.jsonl")}
    B = {r["id"]: r for r in _load(RAW_DIR / f"ko_qa_{args.b}_{args.model}.jsonl")}
    common = sorted(set(A) & set(B) & set(hits))
    chat = _pool_chat(args.judge)
    out = RAW_DIR / f"ko_qajudge_{args.a}_vs_{args.b}_{args.judge}.jsonl"
    done = {r["id"] for r in _load(out)}
    for qid in common:
        if qid in done or "error" in A[qid] or "error" in B[qid]:
            continue
        ctx = "\n".join(f"[{i+1}] {h['excerpt'][:400]}" for i, h in enumerate(hits[qid]["hits"]))
        votes = []
        for first, second, la, lb in ((A[qid], B[qid], "A", "B"), (B[qid], A[qid], "B", "A")):
            user = f"질문: {A[qid]['q']}\n\n위키 근거:\n{ctx}\n\n답변 A:\n{first['answer']}\n\n답변 B:\n{second['answer']}"
            try:
                w = json.loads(chat.complete(_JUDGE, user, json_only=True)).get("winner", "tie")
            except Exception:
                w = "tie"
            votes.append({"A": la, "B": lb, "tie": "tie"}.get(w, "tie"))
        final = votes[0] if votes[0] == votes[1] else "tie"
        _append(out, {"id": qid, "votes": votes, "winner": final})
    rows = _load(out)
    from collections import Counter
    c = Counter(r["winner"] for r in rows)
    # 거부형 계측
    refuse_a = sum(1 for i in common if "찾지 못" in A[i].get("answer", "") or A[i].get("answer", "")[:40].endswith("없습니다"))
    refuse_b = sum(1 for i in common if "찾지 못" in B[i].get("answer", "") or B[i].get("answer", "")[:40].endswith("없습니다"))
    print(f"판정 (n={len(rows)}): {args.a}={c['A']} {args.b}={c['B']} tie={c['tie']} "
          f"sign p={significance.exact_mcnemar(c['A'], c['B']):.4f}")
    print(f"거부형 답: {args.a}={refuse_a} {args.b}={refuse_b}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen-corpus"); g.add_argument("--n", type=int, default=60); g.set_defaults(fn=cmd_gen_corpus)
    sub.add_parser("gen-q").set_defaults(fn=cmd_gen_q)
    sub.add_parser("build-hits").set_defaults(fn=cmd_build_hits)
    a = sub.add_parser("answer"); a.add_argument("--variant", required=True); a.add_argument("--model", default="solar"); a.set_defaults(fn=cmd_answer)
    j = sub.add_parser("judge"); j.add_argument("--a", required=True); j.add_argument("--b", required=True)
    j.add_argument("--model", default="solar"); j.add_argument("--judge", default="gpt-4o"); j.set_defaults(fn=cmd_judge)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
