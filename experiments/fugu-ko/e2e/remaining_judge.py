"""잔여 5종 중 **judge 채점 2종**(wiki_qa · synthesize)의 쌍대 판정 러너.

`remaining_run.py`가 만든 워커 산출물(`analysis/raw/remaining/{task}__{system}.jsonl`)을
읽어 쌍대 판정을 돌리고 `analysis/raw/remaining_judge/{task}__{judge}.jsonl`에 append한다.
집계는 `remaining_analyze.py`가 한다(2단 분리 — RUNNER_DESIGN §3과 동일 규약).

## ⚠️ 전수 pairwise를 하지 않는다 — 비교 설계가 이 파일의 1차 결정이다

워커가 7종이라 전수 쌍은 21개다. n=1,000 · 양방향 2회 · 판정자 2인이면
21 × 1000 × 2 × 2 = **84,000콜/태스크**로, 태스크 2종이면 15만 콜이다. 불가능하다.

채택 설계 = **국내 전수 + 앵커 스포크(hub-and-spoke)**, 7쌍.

| 티어 | 쌍 | 왜 |
|---|---|---|
| **A(국내 전수)** | solar×exaone · solar×ax · exaone×ax | **이 실험의 결정 대상**이다. 프로덕션 슬롯을 국내 3사 중 누가 갖느냐는 이 삼각형 안에서 정해진다. 삼각형을 닫아야 순위의 이행성(intransitivity)까지 검증된다. |
| **B(프론티어 격차)** | solar×{opus-4.8, gpt-5.6-sol, deepseek-v4-pro, glm-5-bedrock} | 프론티어는 **채택 후보가 아니다**(벤더 금지·비용). 알아야 할 것은 "국내가 프론티어에 얼마나 뒤지는가" 한 가지뿐이라, **공통 앵커 1개와의 쌍**이면 그 격차가 같은 척도로 잡힌다. |

앵커를 `solar`로 고정한 근거(사후 선택 아님):
- `ASSIGNMENTS` 13슬롯 중 10개가 Solar 1차이고, 분석 결론(`docs/model-orchestration.md`
  §11.4/§12.4)의 기준 모델이다. §15 diversified 오버라이드는 "동점 구간 안의 다양화"라
  기준 모델을 바꾸지 않는다.
- 앵커가 하나여야 프론티어 4종의 격차가 **서로 비교 가능**하다(앵커가 태스크마다 다르면
  격차 수치를 가로로 못 읽는다).
- exaone/ax 대 프론티어는 티어 A의 solar×exaone / solar×ax를 통해 **이행적으로** 읽는다.
  이행성이 깨지면 분석기가 그것을 경고로 띄운다(순위 신뢰도 진단).

**티어 B는 서브샘플링한다**(`--tier-b-n`, 기본 250). 프론티어 격차는 효과크기가 크다고
예상되는 구간이라(승률 0.65+) 결정 200건이면 검출력 0.8을 넘는다. 반대로 티어 A는
"유의차 없음"을 주장해야 하는 구간이라 전수로 간다(기본 `--tier-a-n` 무제한).
샘플은 `sha1(task|id)` 결정론 정렬 + wiki_qa는 `kind` 층화라 **판정자·쌍에 무관하게 동일**하고,
티어 B 표본은 티어 A 집합의 부분집합이다(중첩 설계).

## 판정자 2인 — judge ∉ 판정쌍

- **Claude Sonnet 4.6**(Bedrock, `us.anthropic.claude-sonnet-4-6`) — 워커 로스터에서
  제외됐으므로 7쌍 전부를 판정해도 judge∉쌍이 자동으로 성립한다.
- **국내 판정자 1인 = 그 쌍에 없는 국내 모델**. `t2_holdout_judge.PAIRS/JUDGES`의 선례를
  그대로 따른다(solar×exaone→ax, solar×ax→exaone). 후보가 둘인 경우(=티어 B, 쌍에 solar만
  있음)는 `ax`를 먼저 쓴다 — E4에서 EXAONE은 판정자로 부적합(변별력 없이 천장, F-E4d)이고
  A.X는 kappa 0.54로 변별했다. A.X의 장문 JSON 계약 위반(F-E4e)은 tie로 삼키지 않고
  `bad_json` 플래그로 **따로 센다**.

## 규약 (원본 문자열 그대로 재사용 — 재작성 금지)

프롬프트 `_SYS`/`_prompt`와 근거 로더 `_page_body`는 `t2_holdout_judge.py`에서 **import**한다.
한 글자라도 다르면 측정 대상이 "판정자 차이"가 아니라 "프롬프트 차이"가 되고, judge 파일럿
(`JUDGE_PILOT_RESULT.md`, 일치율 70.0% / kappa 0.535)의 신뢰도 근거가 이월되지 않는다.

- **양방향 스왑 2회**(A↔B). 두 방향이 일치할 때만 승패, 불일치 = tie. 단 이 러너는
  **방향별로 1행씩** 남긴다(재개 키 = `(task, id, pair, direction, judge)`) — 파일럿처럼
  쌍당 1행이면 fwd 성공 직후 죽었을 때 그 콜을 버린다. verdict 합성은 분석기가 한다.
- 근거(`srcs`)는 태스크마다 원천이 다르다. **문항이 본 것과 같은 것만** 준다:
  - `wiki_qa` — 입력 freeze된 히트의 상위 3개 page slug → `_page_body`(파일럿과 동일).
  - `synthesize` — freeze된 sub-answer 조각(`synthesize_subs.jsonl`). 위키 페이지가
    아니라 조각 답이 곧 근거이기 때문이다. 없으면 골든의 `subs`로 폴백한다.

## 실패 정책

파일럿과 같은 이유로 **API 실패를 tie로 삼키지 않는다**(조용한 실패가 tie로 둔갑하면 무승부가
부풀려진다). 단 2만 콜 규모라 1건 실패로 전체를 죽이지는 않는다:
재시도 3회 → 그래도 실패면 `error` 행(재개 대상으로 남는다) → 누적 오류율이
`--max-error-rate`(기본 5%)를 넘고 시도가 50건 이상이면 **전체 중단**. JSON 파싱 실패만
tie로 흡수하되 `bad_json=true`로 따로 센다.

## 사용

    # 콜 수·비용만 계산(네트워크 0, 워커 산출물 없어도 골든만으로 추정)
    python experiments/fugu-ko/e2e/remaining_judge.py --dry-run

    # 배선 스모크(실콜 소수)
    python experiments/fugu-ko/e2e/remaining_judge.py --tasks wiki_qa --limit 4

    # 본실행
    python experiments/fugu-ko/e2e/remaining_judge.py --tasks wiki_qa,synthesize
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import zip_longest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent          # experiments/fugu-ko/e2e
FUGU = HERE.parent                              # experiments/fugu-ko
_WORKTREE_ROOT = FUGU.parent.parent             # 이 체크아웃의 루트(orthus 패키지 위치)

for _p in (str(HERE), str(FUGU), str(_WORKTREE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# --------------------------------------------------------------------------- #
# 로스터 / 비교 설계
# --------------------------------------------------------------------------- #
DOMESTIC: tuple[str, ...] = ("solar", "exaone", "ax")
FRONTIER: tuple[str, ...] = (
    "claude-opus-4.8",
    "gpt-5.6-sol",
    "deepseek-v4-pro",
    "glm-5-bedrock",
)
ROSTER: tuple[str, ...] = DOMESTIC + FRONTIER
ANCHOR = "solar"

JUDGE_TASKS: tuple[str, ...] = ("wiki_qa", "synthesize")

# 쌍의 (left, right) 순서는 ROSTER 순서로 정규화한다 — 같은 쌍이 두 이름을 갖지 않게.
def _norm_pair(a: str, b: str) -> tuple[str, str]:
    ia, ib = ROSTER.index(a), ROSTER.index(b)
    return (a, b) if ia < ib else (b, a)


TIER_A: list[tuple[str, str]] = [_norm_pair(a, b) for a, b in itertools.combinations(DOMESTIC, 2)]
TIER_B: list[tuple[str, str]] = [_norm_pair(ANCHOR, f) for f in FRONTIER]
PAIRS: list[tuple[str, str]] = TIER_A + TIER_B
TIER: dict[tuple[str, str], str] = {p: "A" for p in TIER_A} | {p: "B" for p in TIER_B}

# 전수 설계(비교용 — 실제로는 쓰지 않는다). dry-run이 절감량을 보여주는 데 쓴다.
ALL_PAIRS = [_norm_pair(a, b) for a, b in itertools.combinations(ROSTER, 2)]

SONNET_SLUG = "claude-sonnet-4.6"          # arena_run.PRICING 키
SONNET_MODEL_ID = "anthropic.claude-sonnet-4-6"  # judge_pilot._SonnetJudge와 동일
SONNET_JUDGE = "sonnet"                    # 결과 파일/행에 쓰는 판정자 이름

# 국내 판정자 우선순위 — 쌍에 없는 첫 모델. E4: EXAONE은 판정자로 부적합(천장),
# A.X는 변별하되 장문 JSON 계약을 7.5% 위반(따로 센다).
_DOMESTIC_JUDGE_ORDER: tuple[str, ...] = ("ax", "exaone", "solar")


def domestic_judge(pair: tuple[str, str], order: tuple[str, ...] | None = None) -> str:
    for m in (order or (_ORDER_OVERRIDE[0] if _ORDER_OVERRIDE else _DOMESTIC_JUDGE_ORDER)):
        if m not in pair:
            return m
    raise AssertionError(f"국내 판정자 없음: {pair}")  # pragma: no cover


# 런타임 오버라이드. 사전등록 순서는 `_DOMESTIC_JUDGE_ORDER`지만, 판정자가 그 태스크에서
# **측정으로 실격**되면(JSON 계약 위반율) 바꿀 수 있어야 한다 — 고장난 판정자의 tie는
# 무승부가 아니라 결측이다. 바꾼 사실은 결과 행의 `judge` 필드에 그대로 남는다.
_ORDER_OVERRIDE: list[tuple[str, ...]] = []

DEFAULT_RAW_DIR = FUGU / "analysis" / "raw" / "remaining"
DEFAULT_OUT_DIR = FUGU / "analysis" / "raw" / "remaining_judge"
DEFAULT_GOLDEN_DIR = FUGU / "golden"
DEFAULT_NODE_ENV = Path.home() / ".orthus" / "nodes" / "company" / "node.env"

# remaining_run.py와 동일한 골든 후보 목록(골든 파일명을 못 박지 않는다).
GOLDEN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "wiki_qa": (
        "t2_wiki_qa_1k.json", "t12_wiki_qa.json", "wiki_qa.json", "t2_wiki_qa.json",
        "t2_wiki_qa_1k.jsonl", "wiki_qa.jsonl", "t2_holdout.json",
    ),
    "synthesize": (
        "t8_synthesize_1k.json", "t12_synthesize.json", "synthesize.json",
        "t8_frozen.json", "t8.json",
    ),
}

# 실측 judge 프롬프트 길이(REMAINING_SLOTS_SCALEUP_PLAN §4.1): 평균 1,346자.
# 한국어 chars/token은 벤더마다 3.7배까지 벌어진다 — 같은 프롬프트가 sonnet에서 2.5배 토큰이 된다.
JUDGE_PROMPT_CHARS = 1346
# 출력은 `{"winner":..., "reason":"<한 문장>"}`. 2026-07-29 실콜 4건에서 Sonnet completion
# 95~125토큰(chars/token 1.0)으로 실측돼 110으로 잡는다. 입력도 같은 실콜에서 1,059~1,292토큰이라
# 위 1,346자 가정과 맞는다.
JUDGE_OUTPUT_CHARS = 110
CHARS_PER_TOKEN: dict[str, float] = {   # 실측(§4.1)
    "sonnet": 1.00, "claude-opus-4.8": 1.01, "gpt-5.6-sol": 1.91,
    "deepseek-v4-pro": 1.76, "glm-5-bedrock": 1.37,
    "solar": 2.94, "ax": 3.30, "exaone": 3.70,
}


# --------------------------------------------------------------------------- #
# 원본 규약 import — 프롬프트/근거 로더는 **재작성 금지**
# env 로드 뒤에 import해야 `t2_holdout_judge.WIKI`(FUGU_WIKI_STORE)가 옳게 잡힌다.
# --------------------------------------------------------------------------- #
_ORIG: dict[str, Any] = {}


def _orig():
    """`t2_holdout_judge`의 `_SYS`/`_prompt`/`_page_body`를 지연 import(문자열 그대로)."""
    if not _ORIG:
        from t2_holdout_judge import _SYS, _page_body, _prompt

        _ORIG.update({"SYS": _SYS, "prompt": _prompt, "page_body": _page_body})
    return _ORIG


# --------------------------------------------------------------------------- #
# 위키 본문 인덱스 — `_page_body`의 rglob은 31,617파일을 슬러그마다 훑는다.
# 결과가 같은지 앞 20건에서 실검증한 뒤 인덱스로 갈아탄다(다르면 즉시 중단).
# --------------------------------------------------------------------------- #
class PageBodies:
    def __init__(self, *, verify: int = 20) -> None:
        self._cache: dict[str, str] = {}
        self._index: dict[str, Path] | None = None
        self._verified = 0
        self._verify_n = verify
        self._lock = threading.Lock()
        self.mismatches: list[str] = []

    def _build_index(self) -> dict[str, Path]:
        from t2_holdout_judge import WIKI

        idx: dict[str, Path] = {}
        if not WIKI.exists():
            return idx
        for f in WIKI.rglob("*.md"):
            rel = f.relative_to(WIKI).with_suffix("")
            parts = rel.parts
            # 슬러그는 "a/b" 형태도 있고 "b" 하나만 쓰이기도 한다 — 접미 경로 전부를 키로 건다.
            for i in range(len(parts)):
                idx.setdefault("/".join(parts[i:]), f)
        return idx

    def _from_index(self, slug: str) -> str:
        assert self._index is not None
        f = self._index.get(slug)
        if f is None:
            return ""
        t = f.read_text(encoding="utf-8", errors="ignore")
        parts = t.split("\n---\n", 1)
        body = parts[1] if len(parts) > 1 else t
        return " ".join(body.split())[:400]

    def get(self, slug: str) -> str:
        with self._lock:
            hit = self._cache.get(slug)
        if hit is not None:
            return hit
        if self._index is None:
            with self._lock:
                if self._index is None:
                    self._index = self._build_index()
        idx_val = self._from_index(slug)
        if self._verified < self._verify_n:
            ref = _orig()["page_body"](slug)
            with self._lock:
                self._verified += 1
                if ref != idx_val:
                    self.mismatches.append(slug)
            idx_val = ref  # 검증 구간에서는 원본 결과를 쓴다
        with self._lock:
            self._cache[slug] = idx_val
        return idx_val


# --------------------------------------------------------------------------- #
# 입출력
# --------------------------------------------------------------------------- #
def _read_items(path: Path) -> list[dict]:
    if path.suffix == ".jsonl":
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    else:
        blob = json.loads(path.read_text(encoding="utf-8"))
        rows = blob if isinstance(blob, list) else (blob.get("items") or blob.get("rows") or [])
    if not isinstance(rows, list):
        raise SystemExit(f"{path}: items가 리스트가 아니다")
    return rows


def resolve_golden(task: str, golden_dir: Path, override: str | None) -> Path | None:
    if override:
        p = Path(override)
        if not p.exists():
            raise SystemExit(f"--golden {task}={override}: 파일 없음")
        return p
    for d in (golden_dir, HERE, HERE / "golden_wiki_qa"):
        for name in GOLDEN_CANDIDATES[task]:
            if (d / name).exists():
                return d / name
    return None


def load_worker_rows(raw_dir: Path, task: str, system: str) -> dict[str, dict]:
    """`{task}__{system}.jsonl` → id별 **마지막 정상 행**. error 행은 버린다."""
    p = raw_dir / f"{task}__{system}.jsonl"
    if not p.exists():
        return {}
    out: dict[str, dict] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("error"):
            out.pop(str(r.get("id")), None)
            continue
        out[str(r.get("id"))] = r
    return out


def answer_of(row: dict) -> str:
    parsed = row.get("parsed") or {}
    txt = parsed.get("answer") if isinstance(parsed, dict) else None
    if not (txt or "").strip():
        txt = row.get("raw_output") or ""
    return (txt or "").strip()


def load_done_keys(out_path: Path) -> set[tuple[str, str, str]]:
    """재개 대상 = **에러 없이 완료된** (id, pair, direction). 같은 키는 마지막 행이 이긴다."""
    if not out_path.exists():
        return set()
    ok: dict[tuple[str, str, str], bool] = {}
    for line in out_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ok[(str(r.get("id")), str(r.get("pair")), str(r.get("direction")))] = not r.get("error")
    return {k for k, clean in ok.items() if clean}


# --------------------------------------------------------------------------- #
# 결정론 샘플링 — 판정자·쌍에 무관, wiki_qa는 kind 층화(비례 배분)
# --------------------------------------------------------------------------- #
def _hkey(task: str, item_id: str) -> str:
    return hashlib.sha1(f"{task}|{item_id}".encode()).hexdigest()


def select_ids(task: str, ids: list[str], kinds: dict[str, str], n: int | None) -> list[str]:
    ordered = sorted(ids, key=lambda i: _hkey(task, i))
    if not n or n >= len(ordered):
        return ordered
    strata: dict[str, list[str]] = defaultdict(list)
    for i in ordered:
        strata[kinds.get(i, "_")].append(i)
    if len(strata) <= 1:
        return ordered[:n]
    total = len(ordered)
    picked: list[str] = []
    for k in sorted(strata):
        want = max(1, round(n * len(strata[k]) / total))
        picked.extend(strata[k][:want])
    picked = sorted(set(picked), key=lambda i: _hkey(task, i))
    return picked[:n]


# --------------------------------------------------------------------------- #
# 근거(srcs) 조립 — 태스크마다 원천이 다르다
# --------------------------------------------------------------------------- #
class Grounding:
    def __init__(self, task: str, raw_dir: Path, golden: dict[str, dict], pages: PageBodies):
        self.task = task
        self.golden = golden
        self.pages = pages
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()
        self.hit_cache: dict[str, list[dict]] = {}
        self.subs_cache: dict[str, list[dict]] = {}
        if task == "wiki_qa":
            p = raw_dir / "wiki_qa_hits.jsonl"
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        d = json.loads(line)
                        self.hit_cache[str(d["id"])] = d.get("hits") or []
        elif task == "synthesize":
            p = raw_dir / "synthesize_subs.jsonl"
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        d = json.loads(line)
                        self.subs_cache[str(d["id"])] = d.get("subs") or []

    def _build(self, qid: str, left_row: dict) -> str:
        if self.task == "wiki_qa":
            slugs = list((left_row.get("metrics") or {}).get("hit_slugs") or [])
            if not slugs:
                slugs = [h.get("page_slug", "") for h in self.hit_cache.get(qid, [])]
            parts: list[str] = []
            for n, slug in enumerate(slugs[:3], 1):
                body = self.pages.get(slug) if slug else ""
                if not body:  # 위키 원문을 못 찾으면 freeze된 발췌로 폴백한다(빈 근거 금지)
                    body = next(
                        (h.get("excerpt", "") for h in self.hit_cache.get(qid, [])
                         if h.get("page_slug") == slug),
                        "",
                    )
                parts.append(f"[{n}] {body}")
            return "\n".join(parts)
        if self.task == "synthesize":
            subs = self.subs_cache.get(qid) or (self.golden.get(qid) or {}).get("subs") or []
            return "\n\n".join(
                f"[{i + 1}] Sub-question: {s.get('q', '')}\nAnswer: {s.get('body', '')}"
                for i, s in enumerate(subs)
            )
        raise AssertionError(self.task)  # pragma: no cover

    def get(self, qid: str, left_row: dict) -> str:
        with self._lock:
            hit = self._cache.get(qid)
        if hit is not None:
            return hit
        s = self._build(qid, left_row)
        with self._lock:
            self._cache[qid] = s
        return s


# --------------------------------------------------------------------------- #
# 판정자 클라이언트
# --------------------------------------------------------------------------- #
def build_judge_client(judge: str):
    """sonnet은 파일럿(`judge_pilot._SonnetJudge`)과 동일 배선 + usage.

    파일럿은 `maxTokens=512 / temperature=0.0`이었다. arena의 `BedrockVendorClient`는
    같은 프로덕션 wire 함수를 쓰되 default가 4096이므로 **명시적으로 512로 맞춘다**
    (usage를 노출하므로 cost-cap이 가능해진다는 것만 다르다).
    """
    if judge == SONNET_JUDGE:
        from arena_run import BedrockVendorClient

        # ⚠️ `ORTHUS_LLM_API_KEY`(OpenAI) 폴백을 두지 않는다. arena_run.build_client에는 있지만,
        # 여기서 그 폴백이 붙으면 Bedrock에 OpenAI 키가 실려 403
        # "Invalid API Key format: Must start with pre-defined prefix"로 죽는다(2026-07-29 실측).
        # 조용한 오설정보다 즉시 중단이 낫다.
        key = os.environ.get("ORTHUS_LLM_BEDROCK_API_KEY", "")
        if not key.strip():
            raise SystemExit(
                "ORTHUS_LLM_BEDROCK_API_KEY 미설정 — 중단. "
                "(node.env에 **빈 값**으로 선언돼 있으면 저장소 .env의 실제 키를 가린다 — "
                "이 러너는 빈 값을 미설정으로 보고 보충하지만, 두 파일 다 비어 있으면 여기서 멈춘다.)"
            )
        return BedrockVendorClient(
            SONNET_SLUG,
            api_key=key,
            region=os.environ.get("ORTHUS_LLM_BEDROCK_REGION", "us-east-1"),
            model_id=SONNET_MODEL_ID,
            max_tokens=512,
            temperature=0.0,
        )
    from arena_run import build_client

    return build_client(judge)


def pricing_slug(judge: str) -> str:
    return SONNET_SLUG if judge == SONNET_JUDGE else judge


# 판정의 **결정 변수는 `winner` 하나**다. `reason`은 진단용이고 집계에 쓰이지 않는다.
# A.X는 `{"winner": "A", "reason": 따옴표 없는 한국어}` 형태를 자주 뱉는데(실측: bad_json의
# 100%), 객체 전체 파싱을 요구하면 **쓰지도 않는 필드 때문에 멀쩡한 판정을 버린다.**
# 그래서 객체 파싱이 실패하면 `winner` 키만 정규식으로 회수한다. 값 범위를 A|B|tie로 못박아
# 모호성이 없고, **전 판정자에 동일하게 적용**되므로 특정 벤더를 봐주는 보정이 아니다.
_WINNER_RE = re.compile(r'"winner"\s*:\s*"?(A|B|tie)"?', re.IGNORECASE)


def _parse_winner(raw: str) -> str | None:
    txt = (raw or "").strip()
    if txt.startswith("```"):  # 방어: 펜스가 오면 벗긴다(파일럿과 동일)
        txt = txt.strip("`")
        txt = txt.split("\n", 1)[1] if "\n" in txt else txt
    try:
        w = json.loads(txt[txt.find("{") : txt.rfind("}") + 1]).get("winner")
    except Exception:  # noqa: BLE001
        m = _WINNER_RE.search(txt)          # 객체가 깨져도 결정 변수는 살릴 수 있다
        if not m:
            return None
        w = m.group(1)
    w = str(w).strip()
    if w in ("A", "B", "tie"):
        return w
    low = w.lower()
    return {"a": "A", "b": "B", "tie": "tie"}.get(low)


# --------------------------------------------------------------------------- #
# 계획 수립 (dry-run과 본실행이 같은 함수를 쓴다)
# --------------------------------------------------------------------------- #
def build_plan(args, tasks: list[str], pairs: list[tuple[str, str]]) -> dict:
    raw_dir = Path(args.raw_dir)
    golden_dir = Path(args.golden_dir)
    overrides = dict(
        kv.split("=", 1) for kv in (args.golden or []) if "=" in kv
    )

    plan: dict[str, Any] = {"tasks": {}, "warnings": []}
    for task in tasks:
        gpath = resolve_golden(task, golden_dir, overrides.get(task))
        gitems = _read_items(gpath) if gpath else []
        golden = {str(i["id"]): i for i in gitems if i.get("id")}
        kinds = {k: str(v.get("kind") or "_") for k, v in golden.items()}

        rows = {s: load_worker_rows(raw_dir, task, s) for s in ROSTER}
        have = {s: set(k for k, r in rows[s].items() if answer_of(r)) for s in ROSTER}
        if not golden:
            # 질문 없이 판정하면 판정자가 근거·답변만 보고 "그럴듯함"을 고른다 — 측정이 아니다.
            plan["warnings"].append(f"{task}: 골든(질문 원천)이 없다 — 건너뜀")
            continue
        base_ids = sorted(i for i in golden if str(golden[i].get("q") or "").strip())
        if len(base_ids) < len(golden):
            plan["warnings"].append(
                f"{task}: 질문 필드가 빈 골든 {len(golden) - len(base_ids)}건 제외"
            )
        if not base_ids:
            plan["warnings"].append(f"{task}: 판정 가능한 문항 0건 — 건너뜀")
            continue

        ids_a = select_ids(task, base_ids, kinds, args.tier_a_n)
        ids_b = select_ids(task, base_ids, kinds, args.tier_b_n)
        if set(ids_b) - set(ids_a):
            plan["warnings"].append(f"{task}: 티어B 표본이 티어A의 부분집합이 아니다(중첩 설계 위반)")

        per_pair: dict[str, dict] = {}
        for pair in pairs:
            left, right = pair
            want = ids_a if TIER[pair] == "A" else ids_b
            if any(have.values()):
                usable = [i for i in want if i in have[left] and i in have[right]]
                missing = len(want) - len(usable)
            else:  # dry-run 계획: 워커 산출물이 아직 없다 → 골든 전량 가정
                usable, missing = list(want), 0
            per_pair[f"{left}v{right}"] = {
                "pair": [left, right],
                "tier": TIER[pair],
                "n_planned": len(want),
                "n_usable": len(usable),
                "n_missing_answers": missing,
                "ids": usable,
                "judges": [SONNET_JUDGE, domestic_judge(pair)],
            }
        plan["tasks"][task] = {
            "golden": gpath.name if gpath else None,
            "n_golden": len(golden),
            "n_base": len(base_ids),
            "kinds": dict(Counter(kinds.values())) if golden else {},
            "worker_rows": {s: len(have[s]) for s in ROSTER},
            "pairs": per_pair,
        }
    return plan


def measure_prompt_chars(plan: dict, args, pairs: list[tuple[str, str]], n: int) -> dict:
    """실제 산출물로 judge 프롬프트를 **조립해** 크기를 잰다(네트워크 0).

    상수 1,346자는 파일럿 30문항에서 나온 값이다. 워커 실행에서 카나리 외삽 견적이 실제보다
    크게 빗나간 원인(wiki_qa 근거 5문단 평균 929자 / synthesize sub 본문 평균 870자가 카나리
    프롬프트보다 컸다)이 judge 프롬프트에는 **더 크게** 적용된다 — judge는 근거 + 답변 2개를
    한꺼번에 담기 때문이다. 그래서 견적 입력을 가정이 아니라 실측으로 바꾼다.
    """
    raw_dir = Path(args.raw_dir)
    pages = PageBodies(verify=0)
    prompt_fn = _orig()["prompt"]
    out: dict[str, Any] = {}
    for task, td in plan["tasks"].items():
        gpath = resolve_golden(task, Path(args.golden_dir),
                              dict(kv.split("=", 1) for kv in (args.golden or []) if "=" in kv).get(task))
        golden = {str(i["id"]): i for i in (_read_items(gpath) if gpath else []) if i.get("id")}
        ground = Grounding(task, raw_dir, golden, pages)
        rows = {s: load_worker_rows(raw_dir, task, s) for s in ROSTER}
        sizes: list[int] = []
        for pk, pd in td["pairs"].items():
            left, right = pd["pair"]
            per_pair = max(1, n // max(1, len(td["pairs"])))
            for qid in pd["ids"][:per_pair]:
                lrow, rrow = rows[left].get(qid), rows[right].get(qid)
                if not (lrow and rrow):
                    continue
                q = (golden.get(qid) or {}).get("q") or ""
                sizes.append(len(prompt_fn(q, ground.get(qid, lrow), answer_of(lrow), answer_of(rrow))))
        if sizes:
            sizes.sort()
            out[task] = {
                "n_sampled": len(sizes),
                "mean": sum(sizes) / len(sizes),
                "p50": sizes[len(sizes) // 2],
                "p90": sizes[int(len(sizes) * 0.9)],
                "max": sizes[-1],
            }
    return out


def _est_cost(judge: str, calls: int, prompt_chars: float = JUDGE_PROMPT_CHARS) -> float:
    from arena_run import PRICING, estimate_cost_usd

    cpt = CHARS_PER_TOKEN.get(judge, 2.0)
    usage = {
        "prompt_tokens": int(prompt_chars / cpt) * calls,
        "completion_tokens": int(JUDGE_OUTPUT_CHARS / cpt) * calls,
    }
    pin, pout = PRICING[pricing_slug(judge)]
    return estimate_cost_usd(usage, pin, pout)


def print_plan(plan: dict, *, args, sizes: dict | None = None) -> tuple[int, float]:
    print("\n══ 비교 설계 ══")
    print(f"  워커 {len(ROSTER)}종 → 전수 쌍 {len(ALL_PAIRS)}개 ·"
          f" 채택 쌍 {len(PAIRS)}개 (티어A 국내전수 {len(TIER_A)} + 티어B 앵커스포크 {len(TIER_B)}, 앵커={ANCHOR})")
    for p in PAIRS:
        print(f"    {TIER[p]}  {p[0]:16} × {p[1]:16}  판정자: {SONNET_JUDGE}, {domestic_judge(p)}")

    total_calls = 0
    total_cost = 0.0
    by_judge: Counter = Counter()
    by_judge_chars: dict[str, float] = {}
    sizes = sizes or {}
    if sizes:
        print("\n  judge 프롬프트 실측 크기(자) — 상수 가정이 아니라 실제 산출물로 조립해 잰 값")
        for t, d in sizes.items():
            print(f"    {t:12} n={d['n_sampled']:4}  평균 {d['mean']:6.0f}  p50 {d['p50']:5}"
                  f"  p90 {d['p90']:5}  max {d['max']:6}   (상수 가정 {JUDGE_PROMPT_CHARS})")
    for task, td in plan["tasks"].items():
        print(f"\n  ── {task} (golden={td['golden']} n={td['n_base']}"
              f"{', kind=' + str(td['kinds']) if td['kinds'] else ''}) ──")
        for pk, pd in td["pairs"].items():
            n = pd["n_usable"]
            calls = n * 2 * len(pd["judges"])
            total_calls += calls
            for j in pd["judges"]:
                by_judge[j] += n * 2
                # 판정자별 가중평균 프롬프트 크기(태스크 믹스를 반영)
                pc = (sizes.get(task) or {}).get("mean", JUDGE_PROMPT_CHARS)
                prev = by_judge_chars.get(j, 0.0)
                by_judge_chars[j] = prev + pc * n * 2
            miss = f"  (답변없음 {pd['n_missing_answers']}건 제외)" if pd["n_missing_answers"] else ""
            print(f"    [{pd['tier']}] {pk:34} n={n:5}  ×2방향 ×{len(pd['judges'])}판정자 = {calls:6}콜{miss}")
    print("\n  판정자별 콜 / 추정 비용")
    for j, c in sorted(by_judge.items(), key=lambda kv: -kv[1]):
        pc = by_judge_chars.get(j, JUDGE_PROMPT_CHARS * c) / max(1, c)
        cost = _est_cost(j, c, pc)
        total_cost += cost
        print(f"    {j:10} {c:7}콜   ~${cost:8.2f}   "
              f"(프롬프트 {pc:.0f}자, chars/token {CHARS_PER_TOKEN.get(j, 2.0)})")
    print(f"\n  합계 {total_calls}콜 · 추정 ${total_cost:.2f}  "
          f"(⚠️ 프론티어/Bedrock 단가는 가정치 — cost-cap 산정용)")

    # 전수 설계였다면 얼마였는지 — 설계 축소의 근거를 숫자로 남긴다.
    naive = sum(td["n_base"] * 2 * 2 * len(ALL_PAIRS) for td in plan["tasks"].values())
    if naive:
        print(f"  참고: 21쌍 전수·전량이면 {naive}콜 (현 설계는 {100 * total_calls / naive:.1f}%)")
    for w in plan["warnings"]:
        print(f"  [warn] {w}")
    return total_calls, total_cost


# --------------------------------------------------------------------------- #
# 실행
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(args) -> int:
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    unknown = set(tasks) - set(JUDGE_TASKS)
    if unknown:
        raise SystemExit(f"알 수 없는 태스크 {sorted(unknown)} — judge 채점은 {JUDGE_TASKS}뿐이다")

    if args.pairs:
        want = {p.strip() for p in args.pairs.split(",") if p.strip()}
        pairs = [p for p in PAIRS if f"{p[0]}v{p[1]}" in want]
        if not pairs:
            raise SystemExit(f"--pairs 매칭 없음. 가능: {[f'{a}v{b}' for a, b in PAIRS]}")
    else:
        pairs = list(PAIRS)

    judges_filter = {j.strip() for j in args.judges.split(",")} if args.judges else None

    plan = build_plan(args, tasks, pairs)
    sizes = measure_prompt_chars(plan, args, pairs, args.prompt_sample) if args.prompt_sample else {}
    calls, cost = print_plan(plan, args=args, sizes=sizes)
    if args.dry_run:
        print("\n  --dry-run: 네트워크 호출 없음.")
        return 0
    if not calls:
        print("\n  할 일 없음.")
        return 0

    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = PageBodies()
    orig = _orig()
    SYS, prompt_fn = orig["SYS"], orig["prompt"]

    # ── work items + 재개 ──────────────────────────────────────────────────
    work: list[dict] = []
    ground: dict[str, Grounding] = {}
    worker_rows: dict[tuple[str, str], dict[str, dict]] = {}
    golden_by_task: dict[str, dict[str, dict]] = {}

    overrides = dict(kv.split("=", 1) for kv in (args.golden or []) if "=" in kv)
    for task, td in plan["tasks"].items():
        gpath = resolve_golden(task, Path(args.golden_dir), overrides.get(task))
        golden_by_task[task] = {str(i["id"]): i for i in (_read_items(gpath) if gpath else []) if i.get("id")}
        for s in ROSTER:
            worker_rows[(task, s)] = load_worker_rows(raw_dir, task, s)
        ground[task] = Grounding(task, raw_dir, golden_by_task[task], pages)

        done_by_judge = {
            j: load_done_keys(out_dir / f"{task}__{j}.jsonl")
            for j in ({SONNET_JUDGE} | {domestic_judge(p) for p in pairs})
        }
        for pk, pd in td["pairs"].items():
            left, right = pd["pair"]
            for j in pd["judges"]:
                if judges_filter and j not in judges_filter:
                    continue
                done = done_by_judge[j]
                for qid in pd["ids"]:
                    for direction in ("fwd", "rev"):
                        if (qid, pk, direction) in done:
                            continue
                        work.append({
                            "task": task, "id": qid, "pair": pk, "left": left, "right": right,
                            "tier": pd["tier"], "direction": direction, "judge": j,
                            "kind": str((golden_by_task[task].get(qid) or {}).get("kind") or ""),
                            "out": out_dir / f"{task}__{j}.jsonl",
                        })

    # 결정론 정렬 후 **판정자 레인을 라운드로빈으로 섞는다.** 정렬만 하면 한 판정자의 일이
    # 통째로 앞에 몰려 벤더가 직렬로 돌고(전체 시간 = 벤더 시간의 합), A.X처럼 RPS 3으로
    # 스로틀된 레인이 도는 동안 Bedrock 레인이 논다. 섞으면 전체 = 가장 느린 레인 하나다.
    work.sort(key=lambda w: (w["task"], w["judge"], w["pair"], w["id"], w["direction"]))
    lanes: dict[str, list[dict]] = defaultdict(list)
    for w in work:
        lanes[w["judge"]].append(w)
    work = [x for tup in zip_longest(*lanes.values()) for x in tup if x is not None]
    if args.limit:
        work = work[: args.limit]
    print(f"\n  실행 대상 {len(work)}콜 (재개분 제외)")
    if not work:
        print("  전부 체크포인트됨.")
        return 0

    clients = {}
    for j in sorted({w["judge"] for w in work}):
        clients[j] = build_judge_client(j)
        print(f"  judge {j:10} model_id={getattr(clients[j], 'model_id', '?')}")

    from arena_run import PRICING, estimate_cost_usd

    lock = threading.Lock()
    write_locks: dict[Path, threading.Lock] = {}
    stop = threading.Event()
    stats = {"done": 0, "err": 0, "bad_json": 0, "cost": 0.0, "attempts": 0}
    errors: list[str] = []

    def _wl(p: Path) -> threading.Lock:
        with lock:
            return write_locks.setdefault(p, threading.Lock())

    def process(w: dict) -> None:
        if stop.is_set():
            return
        task, qid, j = w["task"], w["id"], w["judge"]
        lrow = worker_rows[(task, w["left"])].get(qid) or {}
        rrow = worker_rows[(task, w["right"])].get(qid) or {}
        a_txt, b_txt = answer_of(lrow), answer_of(rrow)
        if w["direction"] == "rev":
            a_txt, b_txt = b_txt, a_txt
        q = (golden_by_task[task].get(qid) or {}).get("q") or (lrow.get("parsed") or {}).get("q") or ""
        srcs = ground[task].get(qid, lrow)

        client = clients[j]
        raw, usage, err, vote, bad = None, {}, None, None, False
        t0 = time.monotonic()
        for attempt in range(3):
            try:
                raw, usage = client.complete(SYS, prompt_fn(q, srcs, a_txt, b_txt), json_only=True)
                err = None
                break
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {str(e)[:200]}"
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        latency = int((time.monotonic() - t0) * 1000)
        if err is None:
            vote = _parse_winner(raw or "")
            if vote is None:
                bad, vote = True, "tie"   # JSON 파싱 실패만 tie로 흡수하되 따로 센다

        row = {
            "task": task, "id": qid, "kind": w["kind"], "pair": w["pair"],
            "left": w["left"], "right": w["right"], "tier": w["tier"],
            "direction": w["direction"], "judge": j,
            "model_id": getattr(client, "model_id", "?"),
            "vote": vote, "bad_json": bad, "raw": (raw or "")[:400] if bad or err else None,
            "usage": usage, "latency_ms": latency, "timestamp": _now_iso(), "error": err,
        }
        with _wl(w["out"]):
            with open(w["out"], "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        with lock:
            stats["attempts"] += 1
            stats["done"] += 1
            if bad:
                stats["bad_json"] += 1
            if err:
                stats["err"] += 1
                errors.append(f"{task}/{j}/{w['pair']}/{qid}/{w['direction']}: {err}")
                rate = stats["err"] / max(1, stats["attempts"])
                if stats["attempts"] >= 50 and rate > args.max_error_rate and not stop.is_set():
                    stop.set()
                    print(f"\n[abort] 오류율 {rate:.1%} > {args.max_error_rate:.1%} — 중단(재개 가능)")
                return
            pin, pout = PRICING[pricing_slug(j)]
            stats["cost"] += estimate_cost_usd(usage, pin, pout)
            if stats["cost"] >= args.cost_cap_usd and not stop.is_set():
                stop.set()
                print(f"\n[cost-cap] 누적 ${stats['cost']:.4f} >= ${args.cost_cap_usd:.2f} — 중단(재개 가능)")
            if stats["done"] % 200 == 0:
                print(f"    ... {stats['done']}/{len(work)}  ${stats['cost']:.3f}"
                      f"  bad_json={stats['bad_json']} err={stats['err']}", flush=True)

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = []
        for w in work:
            if stop.is_set():
                break
            futs.append(pool.submit(process, w))
        for f in as_completed(futs):
            f.result()

    print(f"\n  완료 {stats['done']}콜 · 추정 ${stats['cost']:.4f}"
          f" · JSON 파싱실패 {stats['bad_json']} · 오류 {stats['err']}")
    if pages.mismatches:
        print(f"  [warn] page_body 인덱스 불일치 {len(pages.mismatches)}건: {pages.mismatches[:5]}")
    for e in errors[:10]:
        print(f"    {e}")
    print(f"  → {out_dir}")
    return 1 if stats["err"] and stats["done"] == stats["err"] else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="잔여 judge 2종 쌍대 판정 러너")
    ap.add_argument("--tasks", default=",".join(JUDGE_TASKS))
    ap.add_argument("--pairs", default=None, help="쉼표구분 'solarvexaone' 형식(기본 7쌍 전부)")
    ap.add_argument("--judges", default=None, help="쉼표구분 sonnet,ax,exaone,solar (기본 설계대로)")
    ap.add_argument("--tier-a-n", type=int, default=0, help="국내 3쌍 표본(0=전수)")
    ap.add_argument("--tier-b-n", type=int, default=250, help="프론티어 4쌍 표본")
    ap.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--golden-dir", default=str(DEFAULT_GOLDEN_DIR))
    ap.add_argument("--golden", action="append", default=[], help="task=path 강제 지정")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--cost-cap-usd", type=float, default=100.0)
    ap.add_argument("--max-error-rate", type=float, default=0.05)
    ap.add_argument("--limit", type=int, default=0, help="work item 상한(스모크용)")
    ap.add_argument("--domestic-judge-order", default=None,
                    help="국내 판정자 우선순위(쉼표). 기본 ax,exaone,solar. "
                         "해당 태스크에서 측정상 실격된 판정자를 피할 때만 쓴다.")
    ap.add_argument("--prompt-sample", type=int, default=200,
                    help="견적용 실측 프롬프트 표본 수(0=상수 가정 사용)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env-file", default=None)
    args = ap.parse_args()
    if args.domestic_judge_order:
        _ORDER_OVERRIDE.append(tuple(x.strip() for x in args.domestic_judge_order.split(",") if x.strip()))
        print(f"  [override] 국내 판정자 우선순위 = {_ORDER_OVERRIDE[0]}")

    try:
        from dotenv import dotenv_values, load_dotenv

        def _fill_blank(path: Path) -> None:
            """현재 값이 **비어 있는** 키만 보충한다.

            `load_dotenv(override=False)`는 "이미 os.environ에 있으면 건너뛴다"라서, 앞서 로드한
            node.env가 `ORTHUS_LLM_BEDROCK_API_KEY=`(빈 값)를 선언해 두면 뒤에 오는 저장소 .env의
            **진짜 키가 영원히 가려진다** — 그러면 코드가 다른 키로 폴백해 403을 맞거나(실측)
            조용히 엉뚱한 벤더로 붙는다. 빈 값은 '설정됨'이 아니라 '미설정'으로 본다.
            """
            for k, v in (dotenv_values(path) or {}).items():
                if v and not (os.environ.get(k) or "").strip():
                    os.environ[k] = v

        primary = Path(args.env_file) if args.env_file else DEFAULT_NODE_ENV
        if primary.exists():
            load_dotenv(primary, override=False)
            print(f"  env: {primary}")
        elif args.env_file:
            raise SystemExit(f"--env-file 없음: {primary}")
        for cand in (_WORKTREE_ROOT / ".env", FUGU.parent.parent / ".env"):
            if cand.exists():
                _fill_blank(cand)  # 빈 벤더 키만 보충(빈 문자열도 미설정으로 취급)
    except ImportError:
        pass

    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
