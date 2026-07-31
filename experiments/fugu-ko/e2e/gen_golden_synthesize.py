"""synthesize 슬롯 골든 생성기 — `golden/t8_synthesize_1k.json`.

## 무엇을 만드나

`orthus.router.decompose.synthesize`(오케스트레이터 ④)의 평가 골든이다. t8_synth.py 설계상
골든은 **부모 복합질문만** 담는다 — 정답도 sub_answers도 넣지 않는다. 러너
(`remaining_run.py::freeze_synthesize_inputs`)가 런타임에 고정 모델(solar)로
split → 리프 실행 → grounded 조각을 freeze한다.

## 왜 grounded≥2가 관문인가

`synthesize()`는 grounded 본문이 **정확히 1개인 wiki/graph** 조각이면 LLM을 아예 부르지
않고 결정론 passthrough한다(decompose.py §9). 즉 grounded 1개짜리 문항은 "합성 모델"을
측정하지 못한다. 그래서 저작한 n 중 grounded≥2를 통과한 문항만 실제 판정 대상이다.

## 페어링 전략 (결정론, LLM 0회)

원천 = wiki_qa 골든 `golden/t2_wiki_qa_1k.json`(읽기 전용). 각 문항의 근거 슬러그는
`e2e/golden_wiki_qa/full_attempts.jsonl`의 `status="accepted"` 행에서 질문 텍스트로 조인한다.

1. 슬러그에서 8자리 hex 접미사를 떼고 `-` 토큰으로 쪼갠다. 첫 토큰 = 주제 그룹.
2. 같은 그룹이거나 토큰이 하나라도 겹치는 문항은 **페어링하지 않는다** — 같은 주제면
   split이 하나로 합치거나 두 리프가 같은 근거를 물어 grounded가 1로 떨어진다.
3. 그룹으로 정렬한 뒤 half-shift(`i` ↔ `i + n//2 + r`)로 라운드 `r`을 돌며 짝을 만든다.
   정렬이 주제순이므로 half-shift는 구조적으로 먼 주제끼리 붙는다. 이미 쓴 (무순서) 짝은
   건너뛴다. 문항 재사용은 허용하되 **같은 짝은 1회**다.

## 왜 LLM 접합이 아닌가 (결정론 템플릿)

접합에 LLM(gpt-4o)을 쓰면 문장은 매끄러워지지만 **원 질문의 고유명사·날짜·수치가 바꿔
쓰이면서 retrieval이 빗나간다.** 이 골든의 유일한 관문이 "두 리프가 각각 grounded되는가"라
원문 토큰 보존이 자연스러움보다 상위 제약이다. 실제로 원 질문들은 어미가 `…나요?`/`…인가요?`
두 형태로 극히 규칙적이라(455건 전수 확인) `-는지/-ㄴ지` 연결형 변환만으로 t8.json 원본
8문항과 같은 결의 한국어가 나온다. 그래서 접합은 결정론이고, 생성 LLM 콜은 **0회**다
(재현성 + 비용 0). 자연스러움은 품질 게이트(20건 육안 검수)로 확인한다.

## 사용

    # 페어 후보만 뽑아 눈으로 본다 (LLM/DB 불필요)
    python experiments/fugu-ko/e2e/gen_golden_synthesize.py --preview 20

    # 파일럿: n건 저작 → 실제 freeze → grounded>=2 통과율 측정
    python experiments/fugu-ko/e2e/gen_golden_synthesize.py --pilot 50 --workers 8

    # 본생성: 골든 파일 + freeze 캐시
    python experiments/fugu-ko/e2e/gen_golden_synthesize.py --build 1400 --freeze --workers 8
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
FUGU = HERE.parent
REPO = FUGU.parent.parent
sys.path.insert(0, str(REPO))

GOLDEN_DIR = FUGU / "golden"
SRC_GOLDEN = GOLDEN_DIR / "t2_wiki_qa_1k.json"
SRC_ATTEMPTS = HERE / "golden_wiki_qa" / "full_attempts.jsonl"
OUT_GOLDEN = GOLDEN_DIR / "t8_synthesize_1k.json"
DEFAULT_OUT_DIR = FUGU / "analysis" / "raw" / "remaining"

UID = UUID("11111111-1111-1111-1111-111111111111")
SCOPE = "company"
SPLIT_K = 3

# t8_synth.py / remaining_run.py와 **동일한** denial 문자열 집합. 하나라도 본문에 있으면
# 그 리프는 미그라운드다. 이 튜플이 어긋나면 캐시가 러너 판정과 달라진다 — 복사 금지 대상.
_DENIALS = ("근거 없", "제공되지 않", "찾을 수 없", "정보가 없", "확인되지 않")

_HEX8 = re.compile(r"-[0-9a-f]{8}$")


# --------------------------------------------------------------------------- #
# env — remaining_run.py의 로더 규약을 그대로 따른다(빈 값 = 미설정).
# --------------------------------------------------------------------------- #
def load_env(env_file: str | None) -> None:
    from dotenv import dotenv_values, load_dotenv

    def _fill_blank(path: Path) -> None:
        for k, v in (dotenv_values(path) or {}).items():
            if v and not (os.environ.get(k) or "").strip():
                os.environ[k] = v

    primary = Path(env_file) if env_file else Path.home() / ".orthus" / "nodes" / "company" / "node.env"
    loaded: list[str] = []
    if primary.exists():
        load_dotenv(primary, override=False)
        loaded.append(str(primary))
    else:
        print(f"[warn] node env 없음: {primary}")
    for cand in (REPO / ".env",):
        if cand.exists() and str(cand) not in loaded:
            _fill_blank(cand)
            loaded.append(str(cand))
    print(f"  env: {' + '.join(loaded)}")


# --------------------------------------------------------------------------- #
# 원천 로드 + 슬러그 조인
# --------------------------------------------------------------------------- #
def load_source() -> list[dict]:
    """wiki_qa 골든 문항에 근거 슬러그를 붙여 반환. 조인 실패 문항은 버린다."""
    items = json.load(open(SRC_GOLDEN, encoding="utf-8"))["items"]
    by_q: dict[str, dict] = {}
    for line in open(SRC_ATTEMPTS, encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("status") == "accepted":
            by_q[r["q"]] = r

    out: list[dict] = []
    for it in items:
        att = by_q.get(it["q"])
        if att is None:
            continue
        slugs = att.get("slugs") or ([att["slug"]] if att.get("slug") else [])
        if not slugs:
            continue
        base = _HEX8.sub("", slugs[0])
        toks = [t for t in base.split("-") if t]
        if not toks:
            continue
        out.append(
            {
                "id": it["id"],
                "q": it["q"].strip(),
                "kind": it.get("kind", "unknown"),
                "slug": slugs[0],
                "group": toks[0],
                "toks": frozenset(toks),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 어미 변환 — "…나요?" / "…인가요?" → 연결형 "-는지" / "-ㄴ지"
# --------------------------------------------------------------------------- #
def to_connective(q: str) -> str | None:
    """의문문을 연결형 절로 바꾼다. 알려진 어미가 아니면 None(= 접합 후보 탈락)."""
    s = q.strip().rstrip("?？ ").strip()
    if s.endswith("인가요"):
        return s[:-3] + "인지"
    if s.endswith("나요"):
        return s[:-2] + "는지"
    if s.endswith("가요"):  # "다른가요" → "다른지"
        return s[:-2] + "지"
    return None


_TEMPLATES = (
    lambda a, b: f"{a}, 그리고 {b} 알려줘.",
    lambda a, b: f"{a}, 또 {b} 정리해줘.",
    lambda a, b: f"{a} 궁금하고, {b}도 함께 알려주세요.",
    lambda a, b: f"{a}, {b} 두 가지를 알려줘.",
)


def compose(q1: str, q2: str, idx: int) -> str | None:
    a, b = to_connective(q1), to_connective(q2)
    if a is None or b is None:
        return None
    return _TEMPLATES[idx % len(_TEMPLATES)](a, b)


# --------------------------------------------------------------------------- #
# 페어링 (결정론)
# --------------------------------------------------------------------------- #
def build_pairs(src: list[dict], want: int, *, kind_filter: str | None = None) -> list[dict]:
    """서로 다른 주제 그룹 + 토큰 무교집합 페어를 half-shift 라운드로 만든다."""
    pool = [s for s in src if kind_filter is None or s["kind"] == kind_filter]
    # 주제 그룹으로 정렬 → half-shift가 구조적으로 먼 주제를 고른다.
    pool = sorted(pool, key=lambda s: (s["group"], s["id"]))
    n = len(pool)
    if n < 4:
        return []

    used: set[tuple[str, str]] = set()
    out: list[dict] = []
    half = n // 2
    r = 0
    # 라운드마다 shift를 1씩 늘려 같은 짝이 반복되지 않게 한다.
    while len(out) < want and r < n:
        shift = half + r
        for i in range(n):
            if len(out) >= want:
                break
            j = (i + shift) % n
            if i == j:
                continue
            a, b = pool[i], pool[j]
            if a["group"] == b["group"] or (a["toks"] & b["toks"]):
                continue
            key = tuple(sorted((a["id"], b["id"])))
            if key in used:
                continue
            q = compose(a["q"], b["q"], len(out))
            if q is None:
                continue
            used.add(key)
            out.append(
                {
                    "id": f"t8s-{len(out) + 1:04d}",
                    "q": q,
                    "src": [a["id"], b["id"]],
                    "slugs": [a["slug"], b["slug"]],
                    "kinds": [a["kind"], b["kind"]],
                }
            )
        r += 1
    return out


# --------------------------------------------------------------------------- #
# freeze — t8_synth.py::build_inputs / remaining_run.py::freeze_synthesize_inputs 이식.
# 캐시 포맷은 러너와 **동일**해야 한다: {"id": ..., "subs": [{"q":..., "body":...}]}
# --------------------------------------------------------------------------- #
_print_lock = threading.Lock()


def _grounded_body(routed) -> str | None:
    from orthus.router.decompose import _extract_body

    if routed is None:
        return None
    body = (_extract_body(routed) or "").strip()
    if not body or any(d in body for d in _DENIALS):
        return None
    return body


class _Counting:
    """solar 콜 수를 전역 집계한다 — 비용 가드(6,000콜 상한) 확인용."""

    total = 0
    _lock = threading.Lock()

    def __init__(self, inner):
        self._inner = inner

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        with _Counting._lock:
            _Counting.total += 1
        return self._inner.complete(system, user, json_only=json_only)


def _make_solar():
    from orthus.models.registry import build_vendor_chat, vendor_specs

    chat = build_vendor_chat(vendor_specs()["solar"], retries=2)
    if chat is None:
        raise SystemExit("solar 미설정 — ORTHUS_LLM_SOLAR_API_KEY 확인")
    return _Counting(chat)


def freeze_one(item: dict, *, split_only: bool = False) -> dict:
    from orthus.router import answer as router_answer
    from orthus.router.decompose import split_question

    chat = _make_solar()
    subs: list[dict] = []
    n_parts = 0
    err = None
    t0 = time.monotonic()
    try:
        subtexts = split_question(item["q"], k=SPLIT_K, chat_model=chat)
        n_parts = len(subtexts)
        if split_only:
            return {"id": item["id"], "subs": [], "n_parts": n_parts, "parts": subtexts,
                    "error": None, "ms": int((time.monotonic() - t0) * 1000)}
        if n_parts >= 2:
            for t in subtexts:
                try:
                    routed = router_answer(
                        UID, t, scope=SCOPE, chat_model=chat, learn=False, record_gaps=False
                    )
                except Exception:  # noqa: BLE001 — 리프 실패는 미그라운드 처리
                    routed = None
                body = _grounded_body(routed)
                if body:
                    subs.append({"q": t, "body": body})
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {str(e)[:120]}"
    return {
        "id": item["id"],
        "subs": subs,
        "n_parts": n_parts,
        "error": err,
        "ms": int((time.monotonic() - t0) * 1000),
    }


def warmup() -> None:
    """병렬 진입 전 단일 워밍업 — 첫 `retrieve()`가 커넥터 레지스트리를 중복 등록하며
    죽는 사례(다른 세션 실측)를 피한다."""
    from orthus.wiki.retrieve import retrieve

    t0 = time.monotonic()
    refs = retrieve(UID, "회사 소개", k=3, scope=SCOPE)
    print(f"  warmup retrieve: {len(refs)}건 / {time.monotonic() - t0:.1f}s")


def run_freeze(items: list[dict], cache_path: Path, *, workers: int) -> tuple[int, list[dict]]:
    done: dict[str, dict] = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                done[str(d["id"])] = d
    todo = [it for it in items if it["id"] not in done]
    print(f"  freeze 대상 {len(todo)}건 (캐시 {len(done)}건 재사용), workers={workers}")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    n_done = 0
    with open(cache_path, "a", encoding="utf-8") as fh, ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(freeze_one, it): it for it in todo}
        for fut in as_completed(futs):
            res = fut.result()
            with lock:
                done[res["id"]] = res
                fh.write(
                    json.dumps(
                        {"id": res["id"], "subs": res["subs"], "n_parts": res["n_parts"]},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                fh.flush()
                n_done += 1
                if n_done % 10 == 0 or n_done == len(todo):
                    ok = sum(1 for r in done.values() if len(r.get("subs") or []) >= 2)
                    print(f"    {n_done}/{len(todo)}  누적 grounded>=2 {ok}/{len(done)}")

    rows = [done[it["id"]] for it in items if it["id"] in done]
    passed = sum(1 for r in rows if len(r.get("subs") or []) >= 2)
    return passed, rows


# --------------------------------------------------------------------------- #
def write_golden(items: list[dict], path: Path, note_extra: str = "") -> None:
    payload = {
        "task": "T8-synthesize",
        "desc": (
            "오케스트레이터 synthesize(router.decompose.synthesize). 복합질문의 grounded 조각 "
            "답들을 하나로 통합. 채점=통합 답 쌍대 승률(judge)."
        ),
        "note": (
            "부모 복합질문만 담는다(t8_synth.py 설계) — 정답/sub_answers 없음. 러너가 고정 "
            "모델(solar)로 split→리프 실행→grounded 조각을 freeze한다. grounded<2는 프로덕션이 "
            "결정론 passthrough라 LLM 미발화 → 제외된다. 문항은 wiki_qa 골든"
            "(t2_wiki_qa_1k.json)에서 근거 슬러그 그룹이 다른 두 문항을 결정론 어미변환"
            "(…나요?→…는지)으로 접합해 만들었다(생성 LLM 0회). " + note_extra
        ),
        "items": [
            {"id": it["id"], "q": it["q"], "src": it["src"], "slugs": it["slugs"],
             "kinds": it["kinds"]}
            for it in items
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {path} ({len(items)}문항)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", type=int, default=0, help="페어만 뽑아 출력(LLM/DB 불필요)")
    ap.add_argument("--probe", type=int, default=0, help="split만 n건 실행(리프 미실행, 1콜/건)")
    ap.add_argument("--pilot", type=int, default=0, help="파일럿 n건 freeze → 통과율")
    ap.add_argument("--build", type=int, default=0, help="본생성 n건 저작")
    ap.add_argument("--freeze", action="store_true", help="--build와 함께 freeze까지 수행")
    ap.add_argument("--kind", default=None, help="원천 kind 필터(factual|synthetic_broad)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--cache-name", default="synthesize_subs.jsonl")
    ap.add_argument("--skip", type=int, default=0, help="페어 앞에서 n건 건너뛴다(파일럿 회피)")
    ap.add_argument("--env-file", default=None)
    args = ap.parse_args()

    src = load_source()
    print(f"  원천 {len(src)}문항 (슬러그 조인 성공), 그룹 {len({s['group'] for s in src})}종")

    want = args.preview or args.probe or args.pilot or args.build
    if not want:
        ap.error("--preview / --pilot / --build 중 하나는 필요")

    pairs = build_pairs(src, want + args.skip, kind_filter=args.kind)
    pairs = pairs[args.skip :]
    print(f"  페어 {len(pairs)}건 생성 (요청 {want})")

    if args.preview:
        for it in pairs[: args.preview]:
            print(f"\n[{it['id']}] {'/'.join(it['kinds'])}  {it['slugs']}")
            print(f"  {it['q']}")
        return

    load_env(args.env_file)
    out_dir = Path(args.out_dir)
    cache = out_dir / args.cache_name

    if args.probe:
        from collections import Counter

        with ThreadPoolExecutor(args.workers) as ex:
            res = list(ex.map(lambda it: freeze_one(it, split_only=True), pairs))
        print(f"\n== split probe (n={len(res)}, k={SPLIT_K}) ==")
        print(f"  n_parts 분포: {dict(Counter(r['n_parts'] for r in res))}")
        for r in res[:6]:
            print(f"\n  [{r['id']}] parts={r['n_parts']}")
            for p in r.get("parts") or []:
                print(f"    - {p}")
        return

    if args.pilot:
        warmup()
        t0 = time.monotonic()
        passed, rows = run_freeze(pairs, cache, workers=args.workers)
        n = len(rows)
        parts = [r.get("n_parts", 0) for r in rows]
        print(f"\n== 파일럿 결과 (n={n}, {time.monotonic() - t0:.0f}s) ==")
        print(f"  split 성공(>=2조각) : {sum(1 for p in parts if p >= 2)}/{n}")
        print(f"  grounded>=2 통과     : {passed}/{n} = {passed / n * 100:.1f}%")
        from collections import Counter

        print(f"  grounded 분포        : {dict(Counter(len(r['subs']) for r in rows))}")
        print(f"  solar 콜(이번 실행)  : {_Counting.total} ({_Counting.total / max(n, 1):.1f}/문항)")
        return

    if args.build:
        if not args.freeze:
            write_golden(pairs, OUT_GOLDEN, "freeze 미수행 — grounded 필터 전 원안이다.")
            return
        warmup()
        t0 = time.monotonic()
        passed, rows = run_freeze(pairs, cache, workers=args.workers)
        elapsed = time.monotonic() - t0
        # 골든에는 **grounded>=2 통과분만** 남긴다. 통과 못 한 문항은 프로덕션 synthesize가
        # LLM을 아예 안 부르므로(결정론 passthrough) 측정 불가 문항이고, 골든에 남기면
        # 러너가 매 실행마다 같은 문항을 다시 drop하며 "n=..."만 흐린다.
        ok_ids = {r["id"] for r in rows if len(r.get("subs") or []) >= 2}
        kept = [it for it in pairs if it["id"] in ok_ids]
        write_golden(
            kept,
            OUT_GOLDEN,
            f"저작 {len(pairs)}건 중 실제 freeze(고정 solar, k={SPLIT_K})에서 grounded>=2를 "
            f"통과한 {len(kept)}건만 남겼다 — 통과율 {len(kept) / max(len(pairs), 1) * 100:.1f}%. "
            f"freeze 캐시는 {cache.name}(전 워커 공유 입력).",
        )
        # 캐시 provenance — 러너가 만든 것이 아니라 이 생성기가 선행 생성한 캐시임을 남긴다.
        (cache.parent / (cache.stem + ".provenance.json")).write_text(
            json.dumps(
                {
                    "produced_by": "experiments/fugu-ko/e2e/gen_golden_synthesize.py --build --freeze",
                    "golden": OUT_GOLDEN.name,
                    "freeze_model": "solar (고정 입력 생성기, 평가 대상 아님)",
                    "split_k": SPLIT_K,
                    "scope": SCOPE,
                    "authored": len(pairs),
                    "grounded_ge2": len(kept),
                    "solar_calls_this_run": _Counting.total,
                    "elapsed_sec": int(elapsed),
                    "note": (
                        "remaining_run.py::freeze_synthesize_inputs와 동일한 절차/denial 집합으로 "
                        "선행 생성했다. 러너는 --subs-cache로 이 파일을 그대로 재사용하면 된다."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            f"\n  freeze 완료 {len(rows)}건 / {elapsed:.0f}s — grounded>=2 {passed}건 "
            f"({passed / max(len(rows), 1) * 100:.1f}%), solar 콜 {_Counting.total}"
        )


if __name__ == "__main__":
    main()
