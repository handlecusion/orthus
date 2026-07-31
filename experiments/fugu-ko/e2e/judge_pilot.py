"""judge_pilot — wiki_qa/synthesize 판정층 교체 전 신뢰도 파일럿.

**왜 필요한가.** 기존 신뢰도 근거(E4 PoLL, `t2_holdout_judge.py`)는 **gpt-4o를 기준
판정자**로 삼아 얻은 것이다. 판정자를 Claude Sonnet 4.6(Bedrock)으로 바꾸면 그 근거는
자동으로 이월되지 않는다 — 판정자가 다르면 판정 분포도 다를 수 있고, 그러면 "국내 모델이
동점"이라는 결론 자체가 판정층에 의존한 인공물일 수 있다.

**방법.** E4 PoLL과 동일하게 **워커 출력을 고정하고 판정층만 교체**한다. 새 추론은 0회다 —
`analysis/raw/t2h_{solar,ax,exaone}.jsonl`에 이미 저장된 30문항 답변을 그대로 재사용하고,
같은 질문·같은 근거·같은 답변 쌍에 대해 gpt-4o judge와 Sonnet judge를 각각 돌린다.

**규약(원본 그대로 승계).**
- 프롬프트/시스템 지시는 `t2_holdout_judge.py`에서 **문자열 그대로** import한다(재작성 금지 —
  프롬프트가 조금이라도 다르면 측정 대상이 "judge 차이"가 아니라 "프롬프트 차이"가 된다).
- **양방향 스왑 2회**(A↔B). 두 방향이 일치할 때만 승패 인정, 불일치 = 위치에 흔들림 = tie.
- 근거는 left 모델의 상위 3개 wiki 페이지 원문(원본과 동일).

**쌍은 3종**(solar×exaone, solar×ax, exaone×ax) — 원본 PAIRS 2종에 exaone×ax를 더해
30문항 × 3쌍 × 2방향 × 2judge = **360콜 상한**을 채운다. judge∉쌍 규칙은 여기서는 적용하지
않는다(두 judge 모두 쌍 밖의 외부 모델이다).

**재개 가능.** 결과는 (pair, judge, id) 키로 jsonl append이며, 재실행 시 이미 있는 키는
건너뛴다. 중간에 죽어도 이어서 돈다.

**실패 정책.** 원본 `_vote`는 모든 예외를 tie로 삼켰다. 파일럿에서는 그게 치명적이다 —
조용한 API 실패가 tie로 둔갑해 일치율을 부풀린다. 여기서는 API 실패(HTTP/타임아웃)는
재시도 후에도 실패하면 **즉시 중단**하고, JSON 파싱 실패만 tie로 처리하되 별도 카운트한다.

    python experiments/fugu-ko/e2e/judge_pilot.py            # 실행/재개
    python experiments/fugu-ko/e2e/judge_pilot.py --analyze  # 집계만
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent          # experiments/fugu-ko/e2e
FUGU = HERE.parent                              # experiments/fugu-ko
ROOT = FUGU.parent.parent                       # repo root (worktree)

sys.path.insert(0, str(FUGU))
sys.path.insert(0, str(ROOT))

try:  # 워크트리 .env (벤더 키). 없으면 프로세스 env를 그대로 쓴다.
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=True)
except Exception:  # noqa: BLE001
    pass

# 프롬프트/근거 로더를 원본에서 그대로 가져온다 — 재작성하면 측정 대상이 오염된다.
from t2_holdout_judge import _page_body, _prompt, _SYS  # noqa: E402

# 워커 출력(t2h_*.jsonl)은 gitignore된 실행 산출물이라 워크트리에 없을 수 있다.
# 이 파일럿의 **입력**이므로 메인 체크아웃 경로를 폴백으로 허용한다(읽기 전용).
_RAW_CANDIDATES = [
    Path(os.environ.get("FUGU_RAW", "")) if os.environ.get("FUGU_RAW") else None,
    FUGU / "analysis" / "raw",
    Path.home() / "src/orthus-ai-competition/experiments/fugu-ko/analysis/raw",
]

MODELS = ("solar", "exaone", "ax")
PAIRS = list(itertools.combinations(MODELS, 2))  # 3쌍
JUDGES = ("gpt4o", "sonnet")

OUT = HERE / "judge_pilot.jsonl"
CALL_BUDGET = 360


def _raw_dir() -> Path:
    for cand in _RAW_CANDIDATES:
        if cand and (cand / "t2h_solar.jsonl").exists():
            return cand
    raise SystemExit("t2h_*.jsonl 워커 출력을 찾지 못했다 — FUGU_RAW 로 경로를 지정해라.")


def _load(raw: Path, m: str) -> dict[str, dict]:
    p = raw / f"t2h_{m}.jsonl"
    return {
        json.loads(x)["id"]: json.loads(x)
        for x in p.read_text(encoding="utf-8").splitlines()
        if x.strip()
    }


# ---------------------------------------------------------------- judges


class _SonnetJudge:
    """Bedrock Converse. `arena_run.BedrockVendorClient`와 동일 배선이되 usage는 안 쓴다."""

    def __init__(self, model_id: str = "anthropic.claude-sonnet-4-6") -> None:
        from orthus.models.adapters.bedrock import _normalize_model_id

        key = os.environ.get("ORTHUS_LLM_BEDROCK_API_KEY", "").strip()
        region = os.environ.get("ORTHUS_LLM_BEDROCK_REGION", "").strip()
        if not key:
            raise SystemExit("ORTHUS_LLM_BEDROCK_API_KEY 미설정 — 중단")
        if not region:
            raise SystemExit("ORTHUS_LLM_BEDROCK_REGION 미설정 — 중단")
        self._key = key
        self._base = f"https://bedrock-runtime.{region}.amazonaws.com"
        self._model_id = _normalize_model_id(model_id, "us")  # → us.anthropic.claude-sonnet-4-6
        self.model_id = f"bedrock:{self._model_id}"

    def complete(self, system: str, user: str, *, json_only: bool) -> str:
        from orthus.models.adapters.bedrock import _extract_response_text, _post_converse

        sys_text = (system or "").strip()
        if json_only:
            sys_text = (
                f"{sys_text}\n\nReturn only a valid JSON object. Do not use Markdown fences."
            ).strip()
        body = {
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": {"maxTokens": 512, "temperature": 0.0},
        }
        if sys_text:
            body["system"] = [{"text": sys_text}]
        data = _post_converse(self._base, self._model_id, self._key, body, timeout=90)
        return _extract_response_text(data)


def _build_judge(name: str):
    if name == "gpt4o":
        from orthus.models.adapters.openai_compat import OpenAIChat
        from orthus.settings import get_settings

        s = get_settings()
        if not (s.llm_api_key or "").strip():
            raise SystemExit("ORTHUS_LLM_API_KEY 미설정 — 중단")
        return OpenAIChat(s.llm_base_url, s.llm_api_key, "gpt-4o")
    if name == "sonnet":
        return _SonnetJudge()
    raise ValueError(name)


_BAD_JSON = Counter()
_CALLS = Counter()
_LOCK = threading.Lock()


def _vote(judge, jname: str, q: str, srcs: str, a: str, b: str) -> str:
    """1콜. API 실패는 예외로 올려 즉시 중단시키고, JSON 파싱 실패만 tie로 흡수한다."""
    raw = judge.complete(_SYS, _prompt(q, srcs, a, b), json_only=True)
    with _LOCK:
        _CALLS[jname] += 1
    try:
        txt = raw.strip()
        if txt.startswith("```"):  # 방어: 펜스가 오면 벗긴다
            txt = txt.strip("`")
            txt = txt.split("\n", 1)[1] if "\n" in txt else txt
        w = json.loads(txt[txt.find("{") : txt.rfind("}") + 1]).get("winner")
    except Exception:  # noqa: BLE001
        w = None
    if w not in ("A", "B", "tie"):
        with _LOCK:
            _BAD_JSON[jname] += 1
        return "tie"
    return w


def _verdict(fwd: str, rev: str, left: str, right: str) -> str:
    if fwd == "A" and rev == "B":
        return left
    if fwd == "B" and rev == "A":
        return right
    return "tie"


# ---------------------------------------------------------------- run


def _done_keys() -> set[tuple[str, str, str]]:
    if not OUT.exists():
        return set()
    keys = set()
    for line in OUT.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        keys.add((r["pair"], r["judge"], r["id"]))
    return keys


def run() -> None:
    raw = _raw_dir()
    items = {
        i["id"]: i["q"]
        for i in json.loads((FUGU / "golden" / "t2_holdout.json").read_text(encoding="utf-8"))["items"]
    }
    runs = {m: _load(raw, m) for m in MODELS}
    done = _done_keys()

    tasks = []
    for left, right in PAIRS:
        for qid in items:
            for jn in JUDGES:
                if (f"{left}v{right}", jn, qid) not in done:
                    tasks.append((left, right, qid, jn))

    print(f"raw={raw}")
    print(f"할 일 {len(tasks)}건 (이미 완료 {len(done)}건) — 예상 콜 {len(tasks) * 2}")
    if len(tasks) * 2 > CALL_BUDGET:
        raise SystemExit(f"콜 예산 {CALL_BUDGET} 초과 예상 — 중단")
    if not tasks:
        print("모두 완료됨.")
        return

    judges = {n: _build_judge(n) for n in JUDGES}
    fh = OUT.open("a", encoding="utf-8")

    # 근거 문자열은 (pair, qid)당 1회만 만든다(디스크 rglob이 비싸다).
    src_cache: dict[tuple[str, str], str] = {}

    def _srcs(left: str, qid: str) -> str:
        k = (left, qid)
        with _LOCK:
            hit = src_cache.get(k)
        if hit is not None:
            return hit
        s = "\n".join(
            f"[{n}] {_page_body(slug)}"
            for n, slug in enumerate(runs[left][qid].get("sources", [])[:3], 1)
        )
        with _LOCK:
            src_cache[k] = s
        return s

    def _one(t):
        left, right, qid, jn = t
        q = items[qid]
        srcs = _srcs(left, qid)
        a = runs[left][qid]["answer"] or ""
        b = runs[right][qid]["answer"] or ""
        j = judges[jn]
        fwd = _vote(j, jn, q, srcs, a, b)   # A=left
        rev = _vote(j, jn, q, srcs, b, a)   # A=right → left 승 = "B"
        rec = {
            "pair": f"{left}v{right}",
            "left": left,
            "right": right,
            "judge": jn,
            "id": qid,
            "fwd": fwd,
            "rev": rev,
            "verdict": _verdict(fwd, rev, left, right),
        }
        with _LOCK:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
        return rec

    n_done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for rec in ex.map(_one, tasks):
            n_done += 1
            print(
                f"  [{n_done}/{len(tasks)}] {rec['pair']:14} {rec['judge']:7} {rec['id']}"
                f"  {rec['fwd']}/{rec['rev']} → {rec['verdict']}",
                flush=True,
            )
    fh.close()
    print(f"\n실제 콜: {dict(_CALLS)} (합 {sum(_CALLS.values())}), JSON 파싱실패 {dict(_BAD_JSON)}")


# ---------------------------------------------------------------- analyze


def _kappa(a: list[str], b: list[str], labels: list[str]) -> float:
    n = len(a)
    if n == 0:
        return float("nan")
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[l] / n) * (cb[l] / n) for l in labels)
    return float("nan") if pe == 1.0 else (po - pe) / (1 - pe)


def _tie_kinds(fwd: str, rev: str) -> str:
    """tie의 종류. 자기일관성 진단용."""
    if (fwd, rev) in (("A", "B"), ("B", "A")):
        return "clean"
    if fwd == "tie" and rev == "tie":
        return "both_tie"
    if fwd == rev in ("A", "B"):
        return "position_bias"  # 스왑해도 같은 자리를 골랐다
    return "partial_tie"


def analyze() -> None:
    rows = [json.loads(x) for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()]
    by: dict[tuple[str, str], dict] = {}
    for r in rows:
        by[(r["pair"], r["id"], r["judge"])] = r

    units = sorted({(p, i) for (p, i, _) in by})
    paired = [(by[(p, i, "gpt4o")], by[(p, i, "sonnet")]) for p, i in units
              if (p, i, "gpt4o") in by and (p, i, "sonnet") in by]

    # 3값 정규화: left승 → "A_win", right승 → "B_win", tie → "tie"
    def norm(r):
        return "tie" if r["verdict"] == "tie" else ("A_win" if r["verdict"] == r["left"] else "B_win")

    g = [norm(x) for x, _ in paired]
    s = [norm(y) for _, y in paired]
    labels = ["A_win", "B_win", "tie"]
    agree = sum(1 for x, y in zip(g, s) if x == y)
    n = len(paired)

    print(f"\n=== judge 파일럿 집계 (n={n} 판정단위 = 쌍 x 문항) ===")
    print(f"일치율   {agree}/{n} = {agree / n * 100:.1f}%")
    print(f"kappa    {_kappa(g, s, labels):.3f}   (합격선 일치율>=70% & kappa>=0.4)")

    print("\n판정 분포")
    for name, v in (("gpt4o", g), ("sonnet", s)):
        c = Counter(v)
        print(f"  {name:7} " + "  ".join(f"{l}={c[l]:2}({c[l]/n*100:4.1f}%)" for l in labels))

    print("\ntie(양방향 불일치) 내역 — judge 자기일관성")
    for jn in JUDGES:
        kinds = Counter(_tie_kinds(by[(p, i, jn)]["fwd"], by[(p, i, jn)]["rev"]) for p, i in units
                        if (p, i, jn) in by)
        tot = sum(kinds.values())
        tie_n = tot - kinds["clean"]
        print(f"  {jn:7} tie {tie_n}/{tot} ({tie_n/tot*100:.1f}%)"
              f"  [position_bias={kinds['position_bias']} both_tie={kinds['both_tie']}"
              f" partial={kinds['partial_tie']}]")

    print("\n쌍별")
    for left, right in PAIRS:
        pk = f"{left}v{right}"
        idx = [k for k, (p, _) in enumerate(units) if p == pk]
        sub_g = [g[k] for k in idx]
        sub_s = [s[k] for k in idx]
        a = sum(1 for x, y in zip(sub_g, sub_s) if x == y)
        cg, cs = Counter(sub_g), Counter(sub_s)
        print(f"  {pk:14} 일치 {a}/{len(idx)} ({a/len(idx)*100:.0f}%)  kappa={_kappa(sub_g, sub_s, labels):.3f}"
              f"  | gpt4o {left}={cg['A_win']} {right}={cg['B_win']} tie={cg['tie']}"
              f"  | sonnet {left}={cs['A_win']} {right}={cs['B_win']} tie={cs['tie']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyze", action="store_true", help="집계만 수행")
    args = ap.parse_args()
    if not args.analyze:
        run()
    analyze()
