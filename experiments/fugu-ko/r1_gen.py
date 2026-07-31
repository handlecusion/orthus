"""R1 라우팅 홀드아웃 — 골든셋 생성 (`docs/routing-holdout-plan.md`).

기존 T5 홀드아웃은 **사람이 쓴 완결된 사용자 질문**으로 라우팅을 쟀다(p=1.00). 그러나
프로덕션이 라우팅하는 것은 **decompose가 쪼갠 리프**다(E2 v2x: 38문항 중 30문항 분해).
Solar 라우팅 오류가 T5에서 7.1%인데 실제 리프에서 ≥12%로 갈린다 — 모집단이 틀렸다(§1.2).

그래서 이 생성기는 **프로덕션 경로를 그대로 태운다**:

    회사 콘텐츠 → 복합 질문 생성(3모델 균등) → 프로덕션 decompose(Solar) → 리프
                                                                            ↓
                                              규칙 사각지대만 남김(_rule_based_route == None)
                                                                            ↓
                                                     양방향 검증 → 라벨 확정

라벨은 **의견이 아니라 구성 + 검증**이다(§3). 각 리프를 두 평면 모두에 태우고:
  · 정확히 한 평면만 앵커를 낸다  → 그 평면이 라벨
  · **두 평면 다 낸다**            → 라우팅이 틀릴 수 없다 → **제외**
  · 둘 다 못 낸다                  → 답이 없다 → **제외**

"둘 다 답한다"의 제외가 결정적이다. 회사 위키는 **같은 노션 행에서 컴파일**됐으므로 DB 행
기반 질문의 상당수를 위키도 답한다. 남기면 어느 쪽을 골라도 맞아 정확도가 부풀려진다.

평면 능력 판정은 **모델 하나에 맡기지 않는다** — solar가 SQL을 못 써서 실패한 것과 "데이터가
거기 없다"는 것은 다르다. 두 모델(solar·baseline)의 **합집합**으로 "그 평면이 답할 수 있는가"를
정의한다(= 데이터의 성질이지 모델의 성질이 아니다).

I6: 전 경로 scope="company". company DB에 e2_kb_gen이 만든 personal-scope 위키 24,673개가
있어 scope="all"이면 합성 페이지가 위키 평면에 섞여 라벨링이 통째로 무효가 된다.

실행 (company node env 선행):
    python r1_gen.py --stage compound --n 800
    python r1_gen.py --stage leaves
    python r1_gen.py --stage verify
    python r1_gen.py --stage build
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
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
from e2_e2e import anchor_hit  # noqa: E402  — 앵커 판정 단일 SoR
from orthus.router.route import _rule_based_route  # noqa: E402
from orthus.router.decompose import should_decompose, split_question  # noqa: E402
from orthus.settings import get_settings  # noqa: E402
from orthus.structured.query import query_structured  # noqa: E402
from orthus.wiki.retrieve import retrieve  # noqa: E402
from orthus.wiki.qa import answer_from_hits  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
SCOPE = "company"  # I6 — 절대 바꾸지 말 것
OUT = HERE / "analysis" / "r1"
GOLDEN = HERE / "golden"
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")

_print_lock = threading.RLock()  # 재진입 필수 — log()가 락 안에서 불린다


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ─────────────────────────────────────────────────────────────── 재료 샘플링


def sample_aggregates(n: int) -> list[dict]:
    """**집계형 S 재료** — 정답 개수를 내가 SQL로 직접 센다(환각 불가능한 앵커).

    행-사실형 S 질문은 사각지대에서 structured 라벨을 거의 못 만든다(실측 1/11). 이유가 있다:
    **진짜 구조화 질문(집계·필터·목록)은 결정론 규칙이 먼저 다 잡아채서** LLM에 도달조차 하지
    않는다. 그래서 사각지대에 남는 건 대부분 위키가 답할 것들이다.

    측정하려면 사각지대에도 structured가 정답인 문항이 있어야 M2(structured→wiki 오류)를 잴 수
    있다. 해법: 집계 질문을 **규칙 키워드 없이** 묻게 한다('몇 개' 대신 '얼마나 되나').
    개수는 내가 세므로 앵커가 결정론적이다.
    """
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """select db_name, key, val, cnt from (
                 select db_name, key, value as val, count(*) as cnt
                 from notion_rows, jsonb_each_text(properties)
                 where scope='company' and db_name is not null
                   and value <> '' and length(value) between 2 and 20
                   and value !~ '\\d{4}-\\d{2}-\\d{2}'
                 group by 1,2,3
               ) t where cnt between 2 and 100
               order by random() limit %s""",
            (n,),
        )
        return [
            {"db_name": d, "field": k, "value": v, "count": c} for d, k, v, c in cur.fetchall()
        ]


def sample_material(n: int) -> list[dict]:
    """복합 질문 1개당 재료 = notion_row 1개(S) + wiki_page 1개(W)."""
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """select db_name, properties::text from notion_rows
               where scope='company' and db_name is not null
                 and jsonb_typeof(properties)='object'
               order by random() limit %s""",
            (n,),
        )
        rows = [{"db_name": d, "props": json.loads(p)} for d, p in cur.fetchall()]
        # 위키 본문은 wiki_pages에 없다 — markdown이 SoR이고 PG는 인덱스다(설계원칙 7).
        # 본문은 wiki_chunks.content에 쪼개져 있고 **청크가 짧다**(중앙값 137자). 청크 1개를
        # 재료로 쓰면 length>300을 넘는 게 회사 전체에 39개뿐이라 재료가 고갈된다(실측).
        # 페이지 단위로 청크를 이어붙여 온전한 본문을 만든다.
        cur.execute(
            """select slug, title, left(body, 1200) from (
                 select p.slug, p.title,
                        string_agg(c.content, E'\\n' order by c.ordinal) as body
                 from wiki_pages p join wiki_chunks c on c.page_id = p.page_id
                 where p.scope='company'
                 group by p.page_id, p.slug, p.title
               ) t where length(body) > 120
               order by random() limit %s""",
            (n,),
        )
        pages = [{"slug": s, "title": t, "body": b} for s, t, b in cur.fetchall()]
    k = min(len(rows), len(pages))
    return [{"row": rows[i], "page": pages[i]} for i in range(k)]


# ─────────────────────────────────────────────────────────────── 1. 복합 질문

_GEN_SYS = (
    "너는 사내 지식 시스템의 평가 문항을 만든다. 주어진 두 재료를 각각 묻는 **한국어 복합 질문 "
    "1개**를 만들어라. 재료 A는 사내 DB의 한 행(표 데이터), 재료 B는 사내 위키 문서다.\n\n"
    "형식:\n"
    "- 두 물음을 '그리고' 또는 '또한'으로 이어 **한 문장**으로 만든다.\n"
    "  예) '<A를 묻는 물음>, 그리고 <B를 묻는 물음>?'\n\n"
    "내용 규칙:\n"
    "- ★ **재료를 언급하지 말고, 그 안의 '사실'을 직접 물어라.** '문서'·'표'·'항목'·'DB' 같은\n"
    "  말을 질문에 쓰지 마라. 답이 어디 있는지 노출하면 안 된다.\n"
    "  (나쁨: '주간회고 문서에 계획이 기록돼 있나요?')\n"
    "  (좋음: 'OTIO import/export 기능은 어떤 프로젝트 파일을 가져오고 내보낼 수 있는가?')\n"
    "- ★ **각 물음은 그 자체로 답할 수 있어야 한다.** '이 업무'·'해당 문서' 같은 지시어 금지.\n"
    "  대상을 **고유한 이름·주제어**로 지칭하라. 슬러그·해시·id 같은 기계 식별자는 쓰지 마라.\n"
    "- ★ 예/아니오로 답하는 물음 금지. 구체적 사실을 묻는 물음이어야 한다.\n"
    "- A 쪽 물음의 정답은 **주어진 필드값 그대로**여야 한다. 값을 바꾸거나 다른 필드와 합치지 마라.\n"
    "- B 쪽 물음의 정답은 재료 B의 **구체적 사실**이어야 한다.\n"
    "- **어투를 매번 다르게** 하라. '무엇/어떻게/설명'만 반복하지 말고 실제 동료가 묻듯 다양하게\n"
    "  물어라(누가·언제·어디·얼마나·어느·했는지·됐는지 …).\n"
    "- 재료 문장을 그대로 베끼지 말 것.\n\n"
    'JSON만 출력: {"q": "복합 질문", "w_keywords": ["B 정답에 반드시 나올 핵심어", "2~4개"]}'
)

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}")
_DEICTIC_RE = re.compile(r"(이|해당|위|그)\s*(업무|문서|버그|항목|작업|건|행|표|이슈|프로젝트)")


def _pick_fields(props: dict, salt: int) -> tuple[str, str, str] | None:
    """행에서 **식별 필드**와 **타깃 필드**를 결정론적으로 고른다.

    정답을 모델에게 물으면 안 된다 — 실측상 생성기가 두 필드를 이어붙인 합성 정답
    ('완료, 2026-04-20T…', 'Bug, 2026-04-04')을 지어내고, 그건 어디에도 그대로 나오지 않아
    앵커가 통째로 죽는다(무응답 제외 10/29). 값은 **행에서 직접** 뽑고 모델에겐 질문만 쓰게 한다.

    날짜/타임스탬프는 타깃에서 뺀다 — 답변 렌더링 형식이 달라 부분문자열 매칭이 실패한다.
    """
    items = [(k, v.strip()) for k, v in props.items() if isinstance(v, str) and v.strip()]
    ident = [(k, v) for k, v in items if len(v) >= 8 and not _DATE_RE.search(v)]
    target = [(k, v) for k, v in items if 2 <= len(v) <= 30 and not _DATE_RE.search(v)]
    if not ident or not target:
        return None
    id_k, id_v = max(ident, key=lambda x: len(x[1]))
    cands = [(k, v) for k, v in target if k != id_k]
    if not cands:
        return None
    t_k, t_v = cands[salt % len(cands)]
    return id_v, t_k, t_v


def _gen_one(chat, mat: dict, idx: int) -> dict | None:
    picked = _pick_fields(mat["row"]["props"], idx)
    if picked is None:
        return None
    id_v, t_k, t_v = picked
    # 슬러그는 프롬프트에 넣지 않는다 — 생성기가 그대로 질문에 박아 넣으면 "이건 위키다"를
    # 라우터에 누출한다(실측 확인). 제목 + 본문만 준다.
    user = (
        f"[재료 A — 사내 표 '{mat['row']['db_name']}'의 한 항목]\n"
        f"  대상: {id_v[:120]}\n  물어볼 필드: '{t_k}'\n  그 필드의 값(= A쪽 정답): {t_v}\n\n"
        f"[재료 B — 사내 위키 문서]\n제목: {mat['page']['title']}\n본문: {mat['page']['body'][:900]}"
    )
    try:
        raw = chat.complete(_GEN_SYS, user, json_only=True)
        d = json.loads(raw)
        q, kws = d.get("q"), d.get("w_keywords") or []
        if not q or not kws:
            return None
        q = q.strip()
        if _DEICTIC_RE.search(q):  # '이 업무는…' — 재료를 못 본 사람은 답할 수 없다
            return None
        return {
            "id": f"r1-{idx:04d}",
            "q": q,
            "generator": chat.model_id,
            # 앵커는 **행에서 직접 뽑은 실제 필드값**이다 — 모델이 지어낸 값이 아니다.
            "s_anchor": {"kind": "keyword", "any_of": [t_v]},
            "w_anchor": {"kind": "page", "slugs": [mat["page"]["slug"]], "any_of": [str(k) for k in kws][:4]},
            "src_db": mat["row"]["db_name"],
            "src_slug": mat["page"]["slug"],
            "s_field": t_k,
        }
    except Exception as e:  # noqa: BLE001
        log(f"    gen 실패 {idx}: {type(e).__name__}")
        return None


_GEN_AGG_SYS = (
    "너는 사내 지식 시스템의 평가 문항을 만든다. 두 물음을 '그리고'/'또한'으로 이어 **한 문장**의\n"
    "한국어 복합 질문 1개를 만들어라.\n\n"
    "A쪽 = 사내 표에서 **조건에 맞는 항목이 얼마나 되는지** 묻는 집계 물음.\n"
    "B쪽 = 사내 위키 문서의 **구체적 사실**을 묻는 물음.\n\n"
    "★ 절대 금지어 (A쪽에 쓰지 마라): '몇 개', '개수', '건수', '목록', '리스트', '상태별', '담당자별'.\n"
    "  대신 자연스럽게 물어라 — '얼마나 되나요', '얼마나 있나요', '어느 정도인가요' 등.\n"
    "★ '표'·'DB'·'문서' 같은 말을 질문에 쓰지 마라. 답이 어디 있는지 노출 금지.\n"
    "★ 지시어('이 항목','해당 문서') 금지 — 그 자체로 답할 수 있어야 한다.\n\n"
    'JSON만 출력: {"q": "복합 질문", "w_keywords": ["B 정답에 나올 핵심어", "2~4개"]}'
)


def _gen_agg(chat, agg: dict, page: dict, idx: int) -> dict | None:
    user = (
        f"[재료 A — 집계]\n  대상: 사내 '{agg['db_name']}'에서 '{agg['field']}'가 '{agg['value']}'인 항목\n"
        f"  (정답 개수는 시스템이 이미 알고 있다. 개수를 질문에 쓰지 마라.)\n\n"
        f"[재료 B — 사내 위키 문서]\n제목: {page['title']}\n본문: {page['body'][:900]}"
    )
    try:
        d = json.loads(chat.complete(_GEN_AGG_SYS, user, json_only=True))
        q, kws = (d.get("q") or "").strip(), d.get("w_keywords") or []
        if not q or not kws or _DEICTIC_RE.search(q):
            return None
        return {
            "id": f"r1a-{idx:04d}",
            "q": q,
            "generator": chat.model_id,
            # 개수 앵커 — 내가 SQL로 센 값. 모델이 지어낼 수 없다.
            "s_anchor": {"kind": "number", "values": [agg["count"]]},
            "w_anchor": {"kind": "page", "slugs": [page["slug"]], "any_of": [str(k) for k in kws][:4]},
            "src_db": agg["db_name"],
            "src_slug": page["slug"],
            "s_field": f"count({agg['field']}={agg['value']})",
        }
    except Exception as e:  # noqa: BLE001
        log(f"    agg 실패 {idx}: {type(e).__name__}")
        return None


def stage_compound(n: int) -> None:
    # 절반은 행-사실형(S), 절반은 집계형(S) — 집계형이 사각지대의 structured 라벨을 만든다.
    n_agg = n // 2
    mats = sample_material(n - n_agg)
    aggs = sample_aggregates(n_agg)
    agg_pages = [m["page"] for m in sample_material(len(aggs))]
    pool = build_pool(["solar", "ax", "exaone"])
    slugs = list(pool)
    log(f"[compound] 행-사실형 {len(mats)} + 집계형 {len(aggs)} · 생성기 {slugs} 균등")

    # (kind, idx, payload) — 두 유형을 생성기에 균등 분배
    jobs: list[tuple[str, int, tuple]] = [("row", i, (m,)) for i, m in enumerate(mats)]
    jobs += [("agg", i, (a, agg_pages[i])) for i, a in enumerate(aggs) if i < len(agg_pages)]

    buckets: dict[str, list] = {s: [] for s in slugs}
    for i, j in enumerate(jobs):
        buckets[slugs[i % len(slugs)]].append(j)

    out: list[dict] = []

    def worker(slug: str) -> None:
        chat = pool[slug]
        for kind, i, payload in buckets[slug]:
            r = _gen_one(chat, payload[0], i) if kind == "row" else _gen_agg(chat, payload[0], payload[1], i)
            if r:
                with _print_lock:
                    out.append(r)
                    if len(out) % 100 == 0:
                        log(f"    ... {len(out)}개")

    with ThreadPoolExecutor(max_workers=len(slugs)) as ex:
        list(ex.map(worker, slugs))

    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "compound.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in sorted(out, key=lambda x: x["id"]):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log(f"[compound] {len(out)}개 → {p}")


# ─────────────────────────────────────────────────────────────── 2. 리프 (프로덕션 decompose)


def stage_leaves() -> None:
    """프로덕션 decompose를 그대로 태워 리프를 뽑는다. 리프는 여기서 **고정**된다 —
    라우팅 비교 시 전 모델이 같은 리프를 본다(분해기 차이가 라우팅 측정에 새지 않게)."""
    items = [json.loads(l) for l in open(OUT / "compound.jsonl", encoding="utf-8")]
    # 프로덕션과 **같은 k**를 쓴다 — 리프가 프로덕션 산출과 달라지면 모집단이 또 틀어진다.
    k = get_settings().ask_decompose_max_parts
    log(f"[leaves] 복합질문 {len(items)}개 → 프로덕션 decompose(Solar, k={k})")

    leaves: list[dict] = []
    done = [0]

    def work(it: dict) -> list[dict]:
        try:
            if not should_decompose(it["q"]):
                return [{**it, "leaf": it["q"], "leaf_idx": 0, "n_leaves": 1, "single": True}]
            parts = split_question(it["q"], k=k)
            texts = [p for p in (parts or []) if p and p.strip()]
            if len(texts) < 2:
                return [{**it, "leaf": it["q"], "leaf_idx": 0, "n_leaves": 1, "single": True}]
            return [
                {**it, "leaf": t.strip(), "leaf_idx": j, "n_leaves": len(texts), "single": False}
                for j, t in enumerate(texts)
            ]
        except Exception as e:  # noqa: BLE001
            log(f"    decompose 실패 {it['id']}: {type(e).__name__}")
            return []
        finally:
            done[0] += 1
            if done[0] % 100 == 0:
                log(f"    ... {done[0]}/{len(items)}")

    with ThreadPoolExecutor(max_workers=4) as ex:
        for res in ex.map(work, items):
            leaves.extend(res)

    blind = [x for x in leaves if _rule_based_route(x["leaf"]) is None]
    ruled = [x for x in leaves if _rule_based_route(x["leaf"]) is not None]
    for x in ruled:
        x["rule_route"] = _rule_based_route(x["leaf"])

    for name, data in (("leaves_blind.jsonl", blind), ("leaves_ruled.jsonl", ruled)):
        with open(OUT / name, "w", encoding="utf-8") as f:
            for r in data:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n_single = sum(1 for x in leaves if x["single"])
    log(
        f"[leaves] 리프 {len(leaves)} (단일 {n_single} · 분해 {len(leaves)-n_single})\n"
        f"         규칙 사각지대 {len(blind)} ({len(blind)/max(1,len(leaves))*100:.0f}%) ← 측정 모집단\n"
        f"         규칙이 결정   {len(ruled)}  ← 대조군 재료"
    )


# ─────────────────────────────────────────────────────────────── 3. 양방향 검증

_VERIFY_MODELS: dict = {}


def _plane_structured(leaf: str, chat) -> tuple[str, int]:
    """(본문, row_count). 예외는 빈 결과로."""
    try:
        r = query_structured(UID, leaf, scope=SCOPE, chat_model=chat)
        if r.status != "executed" or not r.rows:
            return "", 0
        return json.dumps(r.rows, ensure_ascii=False, default=str), int(r.row_count or 0)
    except Exception:  # noqa: BLE001
        return "", 0


def _plane_wiki(leaf: str, chat) -> tuple[str, list[str], float]:
    """(답변, 근거 slug들, 최고 유사도). 유사도는 C3-b 임계 탐색용."""
    try:
        hits = retrieve(UID, leaf, k=5, scope=SCOPE)
        if not hits:
            return "", [], 0.0
        top = max((getattr(h, "score", 0.0) or 0.0) for h in hits)
        a = answer_from_hits(UID, leaf, hits, chat_model=chat, learn=False, record_gaps=False)
        return (a.answer or ""), [s.page_slug for s in a.sources], float(top)
    except Exception:  # noqa: BLE001
        return "", [], 0.0


def _verify_one(x: dict) -> dict:
    """리프 하나를 두 평면 × 두 모델에 태우고 라벨을 확정한다.

    평면 능력 = **두 모델의 합집합** — solar가 SQL을 못 써서 실패한 것과 "데이터가 거기 없다"는
    것은 다르다. 라벨은 데이터의 성질이어야 한다.
    """
    leaf = x["leaf"]
    s_anchor, w_anchor = x["s_anchor"], x["w_anchor"]

    s_texts, s_rows = [], 0
    w_texts, w_slugs, w_top = [], [], 0.0
    for slug in ("solar", "baseline"):
        chat = _VERIFY_MODELS[slug]
        t, rc = _plane_structured(leaf, chat)
        s_texts.append(t)
        s_rows = max(s_rows, rc)
        a, srcs, top = _plane_wiki(leaf, chat)
        w_texts.append(a)
        w_slugs += srcs
        w_top = max(w_top, top)

    def hits(anchor: dict, texts: list[str], sources: list[str]) -> bool:
        return any(anchor_hit(anchor, t, sources) for t in texts)

    grid = {
        ("structured", "s"): hits(s_anchor, s_texts, []),
        ("structured", "w"): hits(w_anchor, s_texts, []),
        ("wiki", "s"): hits(s_anchor, w_texts, w_slugs),
        ("wiki", "w"): hits(w_anchor, w_texts, w_slugs),
    }
    # 이 리프가 겨냥한 앵커 = 어느 평면이든 맞힌 앵커
    target = None
    for a in ("s", "w"):
        if grid[("structured", a)] or grid[("wiki", a)]:
            target = a
            break
    if target is None:
        verdict, label = "drop_unanswerable", None
    else:
        planes = [p for p in ("structured", "wiki") if grid[(p, target)]]
        if len(planes) == 2:
            verdict, label = "drop_both_planes", None  # ← 라우팅이 틀릴 수 없다
        else:
            verdict, label = "keep", planes[0]

    return {
        **x,
        "verdict": verdict,
        "label": label,
        "target_anchor": target,
        "structured_rows": s_rows,
        "wiki_top_score": round(w_top, 4),
        "grid": {f"{p}:{a}": v for (p, a), v in grid.items()},
    }


def stage_verify() -> None:
    global _VERIFY_MODELS
    _VERIFY_MODELS = build_pool(["solar", "baseline"])
    items = [json.loads(l) for l in open(OUT / "leaves_blind.jsonl", encoding="utf-8")]
    log(f"[verify] 사각지대 리프 {len(items)}개 × 2평면 × 2모델 = {len(items)*4} plane-run")

    done, out = [0], []
    t0 = time.monotonic()

    def work(x: dict) -> dict:
        r = _verify_one(x)
        done[0] += 1
        if done[0] % 25 == 0:
            el = time.monotonic() - t0
            eta = el / done[0] * (len(items) - done[0])
            log(f"    ... {done[0]}/{len(items)}  경과 {el/60:.0f}분  ETA {eta/60:.0f}분")
        return r

    with ThreadPoolExecutor(max_workers=4) as ex:
        for r in ex.map(work, items):
            out.append(r)

    with open(OUT / "verified.jsonl", "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter

    c = Counter(r["verdict"] for r in out)
    lab = Counter(r["label"] for r in out if r["label"])
    log(
        f"[verify] 완료 → {OUT/'verified.jsonl'}\n"
        f"    채택          {c['keep']}\n"
        f"    제외(양평면)   {c['drop_both_planes']}   ← 라우팅이 틀릴 수 없는 문항\n"
        f"    제외(무응답)   {c['drop_unanswerable']}\n"
        f"    라벨분포      {dict(lab)}"
    )


# ─────────────────────────────────────────────────────────────── 4. 골든 조립


def stage_build() -> None:
    from collections import Counter

    ver = [json.loads(l) for l in open(OUT / "verified.jsonl", encoding="utf-8")]
    keep = [r for r in ver if r["verdict"] == "keep"]
    ruled = [json.loads(l) for l in open(OUT / "leaves_ruled.jsonl", encoding="utf-8")]

    # 오염 방지(I5): 기존 골든셋과 문자열 중복 금지
    burned: set[str] = set()
    for f in GOLDEN.glob("*.json"):
        try:
            d = json.load(open(f, encoding="utf-8"))
            items = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
            for it in items:
                if isinstance(it, dict) and it.get("q"):
                    burned.add(it["q"].strip())
        except Exception:  # noqa: BLE001
            pass
    keep = [r for r in keep if r["leaf"].strip() not in burned]

    # 중복 리프 제거
    seen: set[str] = set()
    uniq = []
    for r in keep:
        if r["leaf"] not in seen:
            seen.add(r["leaf"])
            uniq.append(r)

    main = [
        {
            "id": f"r1m-{i:03d}",
            "q": r["leaf"],
            "expected": r["label"],
            "anchor": r["s_anchor"] if r["target_anchor"] == "s" else r["w_anchor"],
            "generator": r["generator"],
            "from_single": r["single"],
            "wiki_top_score": r["wiki_top_score"],
            "structured_rows": r["structured_rows"],
        }
        for i, r in enumerate(uniq)
    ]

    random.seed(20260715)
    ctl_pool = [r for r in ruled if r["leaf"].strip() not in burned]
    random.shuffle(ctl_pool)
    control = [
        {"id": f"r1c-{i:03d}", "q": r["leaf"], "rule_route": r["rule_route"]}
        for i, r in enumerate(ctl_pool[:40])
    ]

    golden = {
        "note": "R1 routing holdout — docs/routing-holdout-plan.md. 모집단=규칙 사각지대 × 리프 분포.",
        "main": main,
        "control": control,
    }
    p = GOLDEN / "routing_holdout.json"
    json.dump(golden, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(
        f"[build] → {p}\n"
        f"    주 셋   {len(main)}   라벨 {dict(Counter(m['expected'] for m in main))}\n"
        f"    대조군  {len(control)} (규칙이 결정 — 모델 무관이어야 함)\n"
        f"    생성기  {dict(Counter(m['generator'] for m in main))}"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["compound", "leaves", "verify", "build"])
    ap.add_argument("--n", type=int, default=800)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    {"compound": lambda: stage_compound(a.n), "leaves": stage_leaves,
     "verify": stage_verify, "build": stage_build}[a.stage]()
