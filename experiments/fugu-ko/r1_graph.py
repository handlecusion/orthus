"""R1 라우팅 후속 — graph 사각지대 측정 (`docs/routing-graph-follow-up.md`).

홀드아웃 290문항은 structured vs wiki만 쟀다(graph 0건). 그런데 프로덕션
`_apply_wiki_default`는 사각지대에서 LLM의 `graph` 판정을 **그대로 신뢰**한다
(`route != "graph"` 예외). 그 예외의 정밀도/재현율을 이 하네스가 잰다.

핵심 불변식은 r1_gen과 동일하다 — **라벨은 의견이 아니라 구성 + 검증**이다:
  · graph 골든 = 실재 KG 엣지(entity mention / path / conflict)에서 뽑은 named subject로
    NL 질문을 만들고, `try_graph_answer`가 **실제 graph 답을 내는지**로 확정한다
    (graph-answerable = 오라클). 앵커 규칙 없이(_rule_based_route==None) 사각지대여야 한다.
  · 양방향 검증 — 각 질문을 graph 평면과 plain-wiki 평면 양쪽에 태워:
      graph만 연결을 노출  → graph-essential (라우팅이 틀리면 손실)
      wiki도 같은 연결 노출 → graph-optional  (wiki로 가도 안 틀림 → 별도 버킷)
      둘 다 못 냄          → unanswerable (제외)

측정(§4):
  Recall     = classify()가 graph 골든을 graph로 보내는 비율
  Leak(FP)   = classify()가 290 non-graph를 graph로 오라우팅하는 비율(사각지대 precision 분모)
  Demote비용 = leak/mis-bind가 bind+probe 후 demote하는 지연 (K4b fail-open이라 답은 안 틀림)
  판정       = `route != "graph"` 예외가 정당한가

I6: 전 경로 scope="company"(personal 합성 페이지 오염 방지). company node + KG on 선행.

실행 (company node env + ORTHUS_KG_ENABLED=true 선행):
    python r1_graph.py --stage material --n 120
    python r1_graph.py --stage gen
    python r1_graph.py --stage verify
    python r1_graph.py --stage measure
    python r1_graph.py --stage audit    # §2 — 290 라벨 표본 감사
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

import psycopg  # noqa: E402

from pool import build_pool  # noqa: E402
from e2_e2e import anchor_hit  # noqa: E402
from orthus.kg.client import kg_available, kg_enabled  # noqa: E402
from orthus.router.route import _rule_based_route, classify  # noqa: E402
from orthus.router.graph import try_graph_answer  # noqa: E402
from orthus.wiki.retrieve import retrieve  # noqa: E402
from orthus.wiki.qa import answer_from_hits  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
SCOPE = "company"  # I6 — 절대 바꾸지 말 것
OUT = HERE / "analysis" / "r1"
GOLDEN = HERE / "golden"
HOLDOUT = GOLDEN / "routing_holdout.json"  # 기존 290 non-graph 셋
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")

_lock = threading.RLock()


def log(m: str) -> None:
    with _lock:
        print(m, flush=True)


# ────────────────────────────────────────────────────── 1. 재료 (실재 KG 엣지)

_BAD_NAME = ("***", "@", "http", "None")


def _clean_name(nm: str | None) -> bool:
    if not nm or len(nm.strip()) < 2 or len(nm) > 40:
        return False
    return not any(b in nm for b in _BAD_NAME)


def sample_entity(n: int) -> list[dict]:
    """entity_mentions 재료 — 2~10개 페이지에 언급된 실재 named 개체 + 언급 페이지 제목들.

    앵커 = 그 개체를 언급한 페이지들의 제목 집합(graph가 노출해야 하는 cross-page 연결).
    """
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """select e.display_name, e.entity_kind,
                      array_agg(distinct p.title) as titles,
                      array_agg(distinct p.slug) as slugs
               from kg_entities e
               join kg_entity_mentions m on m.entity_id=e.entity_id
               join wiki_pages p on p.page_id=m.page_id
               where e.scope='company' and p.scope='company'
               group by e.entity_id, e.display_name, e.entity_kind
               having count(distinct m.page_id) between 2 and 10
               order by random() limit %s""",
            (n * 2,),
        )
        out = []
        for name, kind, titles, slugs in cur.fetchall():
            if not _clean_name(name):
                continue
            titles = [t for t in (titles or []) if t]
            if len(titles) < 2:
                continue
            out.append(
                {
                    "material": "entity",
                    "subject": name,
                    "kind": kind,
                    "anchor_titles": titles[:6],
                    "anchor_slugs": [s for s in (slugs or []) if s][:6],
                }
            )
        return out[:n]


def sample_relation(n: int) -> list[dict]:
    """path_between 재료 — 같은 페이지에 함께 언급된 서로 다른 두 개체(연결 가능성 높음).

    두 개체를 named로 주고 "어떻게 이어지나"를 앵커 없이 묻는다. bind가 각 개체를 page slug로
    resolve해 path_between이 붙으면 graph-answerable. 앵커 = 상대 개체 이름(연결의 발견).
    """
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """select e1.display_name a, e2.display_name b, count(distinct m1.page_id) shared
               from kg_entity_mentions m1
               join kg_entity_mentions m2 on m1.page_id=m2.page_id and m1.entity_id < m2.entity_id
               join kg_entities e1 on e1.entity_id=m1.entity_id and e1.scope='company'
               join kg_entities e2 on e2.entity_id=m2.entity_id and e2.scope='company'
               group by e1.display_name, e2.display_name
               having count(distinct m1.page_id) between 1 and 6
               order by random() limit %s""",
            (n * 3,),
        )
        out = []
        for a, b, shared in cur.fetchall():
            if not (_clean_name(a) and _clean_name(b)) or a == b:
                continue
            out.append(
                {"material": "relation", "subject": a, "subject_b": b,
                 "anchor_titles": [b, a], "shared": int(shared)}
            )
        return out[:n]


def sample_conflict(n: int) -> list[dict]:
    """page_conflicts 재료 — CONFLICTS_WITH 엣지가 있는 페이지 제목(모순 토픽).

    conflict claim 제목이 모순 내용을 서술한다. 그 토픽을 앵커 없이("서로 안 맞는 얘기 있나")
    물어 page_conflicts가 붙는지 본다. 앵커 = conflict claim 제목의 핵심어(그 페이지가 노출).
    재료가 얇아(20 엣지) best-effort로만 채운다.
    """
    import subprocess

    try:
        r = subprocess.run(
            ["docker", "exec", "orthus_neo4j", "cypher-shell", "-u", "neo4j",
             "-p", "orthus-kg-localdev", "--format", "plain",
             "MATCH (c:WikiClaim)-[:CONFLICTS_WITH]->(pg:WikiPage) "
             "WHERE pg.title IS NOT NULL "
             "RETURN DISTINCT pg.title AS topic, c.title AS conflict LIMIT 40"],
            capture_output=True, text=True, timeout=30,
        )
        rows = []
        for line in r.stdout.splitlines()[1:]:  # skip header
            parts = [p.strip().strip('"') for p in line.split(", ", 1)]
            if len(parts) == 2 and _clean_name(parts[0]):
                rows.append({"material": "conflict", "subject": parts[0],
                             "anchor_titles": [parts[1][:40]], "conflict": parts[1]})
        random.shuffle(rows)
        return rows[:n]
    except Exception as e:  # noqa: BLE001
        log(f"    conflict 재료 실패: {type(e).__name__}")
        return []


def stage_material(n: int) -> None:
    ent = sample_entity(int(n * 0.55))
    rel = sample_relation(int(n * 0.30))
    con = sample_conflict(int(n * 0.15))
    mats = ent + rel + con
    random.shuffle(mats)
    for i, m in enumerate(mats):
        m["id"] = f"g-{i:04d}"
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "graph_material.jsonl", "w", encoding="utf-8") as f:
        for m in mats:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    log(f"[material] entity {len(ent)} · relation {len(rel)} · conflict {len(con)} = {len(mats)} → graph_material.jsonl")


# ────────────────────────────────────────────────────── 2. NL 질문 생성 (앵커 금지)

_GEN_ENTITY = (
    "너는 사내 지식 시스템의 평가 문항을 만든다. 한 named 대상이 **어느 페이지·맥락들에 걸쳐 "
    "나오는지**(cross-page 발견)를 묻는 자연스러운 한국어 질문 1개를 만들어라.\n\n"
    "★ 금지어(질문에 쓰지 마라): '무슨관계', '어떤관계', '어떻게연결', '언급한페이지', "
    "'언급된페이지', '언급한곳', '언급된곳', '어디서언급', '누가언급', '엮인페이지', "
    "'목록', '리스트', '개수', '몇개', '뭐야', '무엇', '무슨내용', '왜', '어떻게', '설명', '요약', '정리'.\n"
    "  이 말들은 결정론 규칙이 먼저 잡아 사각지대가 아니게 된다. 반드시 피하라.\n"
    "★ 대상을 주어진 이름 그대로 지칭하라. '이 개체' 같은 지시어 금지.\n"
    "★ 대신 이렇게 물어라 — 'X는 어디어디에 나오나요', 'X가 다뤄지는 데가 어디인가요', "
    "'X 관련해서 나온 데들이 어디인가요', 'X 얘기가 나오는 곳들 좀 알려줘'.\n"
    'JSON만: {"q": "질문"}'
)

_GEN_RELATION = (
    "너는 사내 지식 시스템의 평가 문항을 만든다. 서로 다른 두 named 대상이 **어떻게 이어지는지**를 "
    "묻는 자연스러운 한국어 질문 1개를 만들어라.\n\n"
    "★ 금지어(질문에 쓰지 마라): '무슨관계', '어떤관계', '관계야', '어떻게연결', '뭐야', '무엇', "
    "'무슨내용', '왜', '어떻게', '설명', '요약', '정리', '목록', '개수', '몇개'.\n"
    "  이 말들은 결정론 규칙이 먼저 잡아 사각지대가 아니게 된다. 반드시 피하라.\n"
    "★ 두 대상을 주어진 이름 그대로 지칭하라. 지시어 금지.\n"
    "★ 대신 이렇게 물어라 — 'A랑 B 사이에 무슨 접점이 있나요', 'A하고 B가 어디서 만나나요', "
    "'A와 B를 잇는 게 뭔가요', 'A가 B랑 엮이는 지점이 있나요'.\n"
    'JSON만: {"q": "질문"}'
)

_GEN_CONFLICT = (
    "너는 사내 지식 시스템의 평가 문항을 만든다. 한 주제에 대해 **서로 안 맞거나 모순되는 서술이 "
    "있는지**를 묻는 자연스러운 한국어 질문 1개를 만들어라.\n\n"
    "★ 금지어(질문에 쓰지 마라): '충돌하는주장', '상충하는주장', '모순되는주장', '뭐야', '무엇', "
    "'왜', '어떻게', '설명', '요약', '정리'.\n"
    "  이 말들은 결정론 규칙이 먼저 잡는다. 피하라.\n"
    "★ 주제를 주어진 이름 그대로 지칭하라.\n"
    "★ 대신 이렇게 물어라 — 'X에 대해 서로 안 맞는 얘기가 있나요', 'X 관련해서 앞뒤가 안 맞는 "
    "부분이 있나요', 'X에서 엇갈리는 서술이 있나요'.\n"
    'JSON만: {"q": "질문"}'
)


def _gen_one(chat, mat: dict) -> dict | None:
    kind = mat["material"]
    if kind == "entity":
        sysp, user = _GEN_ENTITY, f"대상: {mat['subject']}"
    elif kind == "relation":
        sysp, user = _GEN_RELATION, f"대상 A: {mat['subject']}\n대상 B: {mat['subject_b']}"
    else:
        sysp, user = _GEN_CONFLICT, f"주제: {mat['subject']}"
    try:
        d = json.loads(chat.complete(sysp, user, json_only=True))
        q = (d.get("q") or "").strip()
        if not q or len(q) < 6:
            return None
        # 사각지대 강제: 규칙이 결정하면 버린다(모델과 무관해진다).
        if _rule_based_route(q) is not None:
            return {**mat, "q": q, "gen_verdict": "ruled_out", "rule_route": _rule_based_route(q)}
        return {**mat, "q": q, "gen_verdict": "blind"}
    except Exception as e:  # noqa: BLE001
        log(f"    gen 실패 {mat['id']}: {type(e).__name__}")
        return None


def stage_gen() -> None:
    mats = [json.loads(l) for l in open(OUT / "graph_material.jsonl", encoding="utf-8")]
    pool = build_pool(["solar", "ax", "exaone"])  # 생성기 다양화(라우터 solar와 분리)
    slugs = list(pool)
    log(f"[gen] 재료 {len(mats)}개 × 생성기 {slugs} 균등 (앵커 금지 → 사각지대 강제)")

    out, done = [], [0]

    def work(item):
        i, mat = item
        r = _gen_one(pool[slugs[i % len(slugs)]], mat)
        done[0] += 1
        if done[0] % 25 == 0:
            log(f"    ... {done[0]}/{len(mats)}")
        return r

    with ThreadPoolExecutor(max_workers=3) as ex:
        for r in ex.map(work, enumerate(mats)):
            if r:
                out.append(r)

    blind = [r for r in out if r["gen_verdict"] == "blind"]
    ruled = [r for r in out if r["gen_verdict"] == "ruled_out"]
    with open(OUT / "graph_candidates.jsonl", "w", encoding="utf-8") as f:
        for r in blind:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter

    log(
        f"[gen] 사각지대 후보 {len(blind)} · 규칙탈락 {len(ruled)} "
        f"(규칙탈락 route: {dict(Counter(r.get('rule_route') for r in ruled))})\n"
        f"      후보 재료분포: {dict(Counter(r['material'] for r in blind))}"
    )


# ────────────────────────────────────────────────────── 3. 양방향 검증 → 골든

_VMODELS: dict = {}


def _plane_wiki(q: str) -> tuple[str, list[str]]:
    try:
        hits = retrieve(UID, q, k=5, scope=SCOPE)
        if not hits:
            return "", []
        a = answer_from_hits(UID, q, hits, chat_model=_VMODELS["solar"], learn=False, record_gaps=False)
        return (a.answer or ""), [s.page_slug for s in a.sources]
    except Exception:  # noqa: BLE001
        return "", []


def _graph_anchor(mat: dict) -> dict:
    """graph가 노출해야 하는 연결 앵커 = 상대 노드/페이지 제목 any_of + 근거 slug."""
    return {
        "kind": "page",
        "slugs": [s.lower() for s in mat.get("anchor_slugs", [])],
        "any_of": mat.get("anchor_titles", []),
    }


def _verify_one(x: dict) -> dict:
    q = x["q"]
    anchor = _graph_anchor(x)

    # graph 평면 — try_graph_answer가 실제 graph 답을 내는가 (graph-answerable 오라클)
    t0 = time.monotonic()
    g = try_graph_answer(UID, q, scope=SCOPE, chat_model=_VMODELS["solar"])
    g_ms = int((time.monotonic() - t0) * 1000)
    g_ans = g.answer
    graph_ok = False
    g_body, g_slugs, g_template = "", [], None
    if g_ans is not None:
        g_body = g_ans.wiki.answer or ""
        g_slugs = list(g_ans.graph.path_slugs or [])
        g_template = g_ans.graph.intent
        # graph-answerable = graph 답이 실제 연결(상대 노드/페이지)을 노출했는가
        graph_ok = anchor_hit(anchor, g_body, g_slugs)

    # wiki 평면 — plain-wiki가 같은 연결을 산문으로 노출하는가
    w_body, w_slugs = _plane_wiki(q)
    wiki_ok = anchor_hit(anchor, w_body, w_slugs)

    if graph_ok and not wiki_ok:
        verdict, label = "keep_essential", "graph"
    elif graph_ok and wiki_ok:
        verdict, label = "keep_optional", "graph"
    elif (not graph_ok) and g_ans is not None:
        # graph가 답은 냈으나 기대 연결을 못 노출 — 약한 graph. essential 아님.
        verdict, label = "keep_optional", "graph"
    else:
        verdict, label = "drop_unanswerable", None

    return {
        **x,
        "verdict": verdict,
        "label": label,
        "graph_ok": graph_ok,
        "wiki_ok": wiki_ok,
        "graph_bound": g_ans is not None,
        "demote_reason": None if g_ans is not None else g.demote_reason,
        "graph_ms": g_ms,
        "g_template": g_template,
        "g_slugs": g_slugs[:6],
    }


def stage_verify() -> None:
    global _VMODELS
    if not (kg_enabled() and kg_available()):
        raise SystemExit("KG off/미가용 — graph 검증 불가. ORTHUS_KG_ENABLED=true + neo4j 확인.")
    _VMODELS = build_pool(["solar"])
    items = [json.loads(l) for l in open(OUT / "graph_candidates.jsonl", encoding="utf-8")]
    log(f"[verify] 후보 {len(items)}개 × (graph 평면 + wiki 평면)")

    out, done, t0 = [], [0], time.monotonic()

    def work(x):
        r = _verify_one(x)
        done[0] += 1
        if done[0] % 20 == 0:
            el = time.monotonic() - t0
            log(f"    ... {done[0]}/{len(items)}  경과 {el/60:.0f}분  ETA {el/done[0]*(len(items)-done[0])/60:.0f}분")
        return r

    with ThreadPoolExecutor(max_workers=4) as ex:
        for r in ex.map(work, items):
            out.append(r)

    with open(OUT / "graph_verified.jsonl", "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ess = [r for r in out if r["verdict"] == "keep_essential"]
    opt = [r for r in out if r["verdict"] == "keep_optional"]
    drop = [r for r in out if r["verdict"] == "drop_unanswerable"]
    # 골든 = essential + optional (둘 다 진짜 graph-answerable). essential은 별도 표시.
    golden = [
        {"id": f"g1-{i:03d}", "q": r["q"], "expected": "graph", "material": r["material"],
         "essential": r["verdict"] == "keep_essential", "anchor": _graph_anchor(r),
         "g_template": r["g_template"]}
        for i, r in enumerate(ess + opt)
    ]
    json.dump({"note": "R1 graph golden — docs/routing-graph-follow-up.md", "main": golden},
              open(GOLDEN / "routing_graph_golden.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    from collections import Counter

    log(
        f"[verify] → graph_verified.jsonl\n"
        f"    graph-essential {len(ess)}  (graph만 연결 노출 — 라우팅 손실 위험)\n"
        f"    graph-optional  {len(opt)}  (wiki도 노출 or 약한 graph)\n"
        f"    unanswerable    {len(drop)}\n"
        f"    골든 {len(golden)}개 → routing_graph_golden.json  재료분포 {dict(Counter(g['material'] for g in golden))}"
    )


# ────────────────────────────────────────────────────── 4. 측정 (recall / leak / demote)


def _load_holdout_nongraph() -> list[dict]:
    g = json.load(open(HOLDOUT, encoding="utf-8"))
    return g["main"]  # 라벨 structured/wiki — graph 아님


def stage_measure() -> None:
    if not (kg_enabled() and kg_available()):
        raise SystemExit("KG off/미가용.")
    golden = json.load(open(GOLDEN / "routing_graph_golden.json", encoding="utf-8"))["main"]
    nongraph = _load_holdout_nongraph()
    pool = build_pool(["solar"])
    chat = pool["solar"]

    # ── A. Recall — classify()가 graph 골든을 graph로 보내는가
    log(f"[measure-A] recall — graph 골든 {len(golden)}문항 classify()")
    rec, done = [], [0]

    def cls_work(g):
        r = classify(g["q"], chat_model=chat)
        done[0] += 1
        return {**g, "classify": r}

    with ThreadPoolExecutor(max_workers=4) as ex:
        rec = list(ex.map(cls_work, golden))

    # ── B. Leak(FP) — classify()가 290 non-graph를 graph로 오라우팅하는가
    log(f"[measure-B] leak — non-graph {len(nongraph)}문항 classify()")

    def cls_ng(it):
        return {**it, "classify": classify(it["q"], chat_model=chat)}

    with ThreadPoolExecutor(max_workers=4) as ex:
        ng = list(ex.map(cls_ng, nongraph))
    leaks = [r for r in ng if r["classify"] == "graph"]

    # ── C. Demote 비용 — leak된 non-graph를 try_graph_answer에 태워 지연 + 오답위험
    log(f"[measure-C] demote 비용 — leak {len(leaks)}건 try_graph_answer")

    def probe(it):
        t0 = time.monotonic()
        g = try_graph_answer(UID, it["q"], scope=SCOPE, chat_model=chat)
        ms = int((time.monotonic() - t0) * 1000)
        return {**it, "graph_bound": g.answer is not None, "demote_reason": g.demote_reason,
                "probe_ms": ms}

    with ThreadPoolExecutor(max_workers=4) as ex:
        leak_probed = list(ex.map(probe, leaks)) if leaks else []

    json.dump({"recall": rec, "nongraph": ng, "leak_probed": leak_probed},
              open(OUT / "graph_measure.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    _report(rec, ng, leaks, leak_probed)


def _report(rec, ng, leaks, leak_probed) -> None:
    from collections import Counter

    n_g = len(rec)
    ess = [r for r in rec if r.get("essential")]
    to_graph = [r for r in rec if r["classify"] == "graph"]
    ess_to_graph = [r for r in ess if r["classify"] == "graph"]
    n_ng = len(ng)
    n_leak = len(leaks)

    # precision @ 골든 prevalence: 두 모집단을 합쳤을 때 graph로 간 것 중 진짜 graph
    tp, fp = len(to_graph), n_leak
    prec = tp / max(1, tp + fp)

    print("\n" + "=" * 78)
    print("R1 graph 사각지대 측정 — LLM `graph` 판정의 정밀도/재현율")
    print("=" * 78)
    print(f"\n모집단: graph 골든 {n_g}(essential {len(ess)}) · non-graph 홀드아웃 {n_ng}")
    print("전 문항 사각지대(_rule_based_route==None) · classify()=프로덕션 solar 배정\n")

    print("── A. Recall (진짜 graph 중 graph 도달)\n")
    print(f"   전체 골든     {len(to_graph):3d}/{n_g:<3d} = {len(to_graph)/max(1,n_g)*100:5.1f}%")
    print(f"   essential만   {len(ess_to_graph):3d}/{len(ess):<3d} = {len(ess_to_graph)/max(1,len(ess))*100:5.1f}%  ← 손실 위험 실제 대상")
    print(f"   골든 classify 분포: {dict(Counter(r['classify'] for r in rec))}")

    print("\n── B. Leak / False-positive (non-graph → graph 오라우팅)\n")
    print(f"   leak          {n_leak:3d}/{n_ng:<3d} = {n_leak/max(1,n_ng)*100:5.1f}%")
    print(f"   leak 라벨분포: {dict(Counter(r['expected'] for r in leaks))}")

    print("\n── C. Demote 비용 (leak이 bind+probe 후 어떻게 되나 — K4b fail-open)\n")
    if leak_probed:
        bound = [r for r in leak_probed if r["graph_bound"]]
        demoted = [r for r in leak_probed if not r["graph_bound"]]
        lat = sorted(r["probe_ms"] for r in leak_probed)
        print(f"   안전 demote   {len(demoted):3d}/{len(leak_probed):<3d}  (bind_miss/gate_reject → wiki 재질의, 답 안 틀림)")
        print(f"   graph 답 생성 {len(bound):3d}/{len(leak_probed):<3d}  ← non-graph인데 graph 답 (실제 품질 위험)")
        print(f"   demote_reason: {dict(Counter(r['demote_reason'] for r in demoted))}")
        print(f"   probe 지연 p50 {lat[len(lat)//2]}ms · p95 {lat[min(len(lat)-1,int(len(lat)*0.95))]}ms · max {lat[-1]}ms")
    else:
        print("   leak 0건 — precision 손실 없음")

    print("\n── 종합\n")
    print(f"   precision @골든prevalence  {prec*100:5.1f}%  (graph로 간 {tp+fp}건 중 진짜 graph {tp})")
    print(f"   recall(essential)          {len(ess_to_graph)/max(1,len(ess))*100:5.1f}%")
    print("\n" + "=" * 78)


# ────────────────────────────────────────────────────── 5. §2 — 290 라벨 표본 감사


def stage_audit() -> None:
    g = json.load(open(HOLDOUT, encoding="utf-8"))["main"]
    random.seed(20260715)
    sample = random.sample(g, min(40, len(g)))
    p = OUT / "label_audit_sample.json"
    json.dump(sample, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n[audit] 290 중 {len(sample)}건 표본 → {p}")
    print("각 문항: q / expected / anchor. 사람이 눈으로 라벨 타당성 확인.\n")
    for s in sample:
        a = s["anchor"]
        ak = a.get("any_of") or a.get("values") or a.get("slugs")
        print(f"  [{s['expected']:10s}] {s['q'][:66]}")
        print(f"               anchor({a['kind']}): {str(ak)[:60]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True,
                    choices=["material", "gen", "verify", "measure", "audit"])
    ap.add_argument("--n", type=int, default=120)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    {"material": lambda: stage_material(a.n), "gen": stage_gen, "verify": stage_verify,
     "measure": stage_measure, "audit": stage_audit}[a.stage]()
