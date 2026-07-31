"""B4 / X1 — 판정자-사람 일치 측정 (`analysis/b4-prereg.md` §3 X1).

재는 것:
  1. **천장** — `HAERAE-HUB/KUDGE`. 포인트와이즈 기술통계(순서형 1-5)와, 우리 판정자와 같은
     명목 A/B/tie 공간의 **유도쌍 천장**(같은 instruction 안의 응답쌍) 둘 다 낸다.
  2. **judge-human** 일치율 + Cohen's κ — `HAERAE-HUB/Korean-Human-Judgements` 694 쌍대에
     **우리 기존 pairwise 판정자 프롬프트를 위치 스왑 포함해 그대로** 실행
  3. **위치 스왑 flip rate** — 한국어에서의 위치 편향
  4. **영어 앵커** — `lmsys/mt_bench_human_judgments`에 동일 프롬프트. 한국어 κ가 낮을 때
     "한국어 페널티"인지 "우리 프롬프트 자체의 노이즈"인지 분리한다.
  5. **factuality 축** — `mteb/summeval` consistency(1-5) 격차 쌍에 동일 프롬프트

  **주판정 (사전선언 §3 개정 "결함 B 해소", 2026-07-22).** 분자·분모를 **같은 명목 A/B/tie
      라벨 공간** + **같은 표본 S**에서 잰다. S = KUDGE Human Annotations의 유도쌍을
      instruction당 8개 균등 표본(card 필터, tie band 0):
        분모 = S 전체 유도쌍의 human-human κ (annotator1 유도선호 vs annotator2)
        분자 = S의 합의 부분집합에서 우리 판정자(위치 스왑, 양방향 접기) vs 사람 gold κ
        비율 = 분자 / 분모, CI = **instruction 군집 부트스트랩**(쌍 아님) 하한 ≥ 0.80 이면 PASS
      [2]/[3]/[4](KHJ 694 · 영어 앵커 · summeval)는 비율에 안 들어가는 **보조 코로보레이션**이다.

판정자는 **새로 쓰지 않는다.** `judge/pairwise.py`의 `_SYS` / `_prompt` / `judge_once`와
양방향 일치 접기 규칙을 그대로 import한다. 새 프롬프트를 쓰면 그건 다른 것을 재는 것이고,
t2·t8·email_draft 승률을 낳은 그 판정자의 신뢰도가 아니게 된다. 프롬프트가 나중에 바뀌었는지
알 수 있도록 `judge/pairwise.py`의 sha256을 결과에 함께 기록한다.

실행:
    # 오프라인 검증 (API 키 불필요 — 합성 fixture + 결정론 스텁 판정자)
    python experiments/fugu-ko/external/b4_judge_kappa.py --dry

    # 실측 (ORTHUS_LLM_BASE_URL / ORTHUS_LLM_API_KEY 필요, judge=gpt-4o)
    python experiments/fugu-ko/external/download.py
    python experiments/fugu-ko/external/b4_judge_kappa.py

비용: 쌍당 2회 호출. 기본값 = 주판정 유도쌍 합의분(card·band0·8/instr, 약 420쌍) + KHJ 694(전량)
      + 앵커 300 + summeval 약 100 → 약 3,000 gpt-4o 호출.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import itertools
import statistics
import sys
import types
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FUGU = HERE.parent
CACHE = HERE / ".cache"
NORM = CACHE / "normalized"
FIXTURES = HERE / "fixtures"
JUDGE_SRC = FUGU / "judge" / "pairwise.py"

SEED = 1234
BOOTSTRAP_N = 10_000
PREREG_RATIO = 0.80
SUMMEVAL_MIN_GAP = 2.0  # consistency 격차가 이보다 작은 쌍은 "정답"이 모호해서 쓰지 않는다


# ------------------------------------------------------- 기존 판정자 그대로 가져오기


def _import_pairwise(dry: bool) -> types.ModuleType:
    """`judge/pairwise.py`를 verbatim import한다.

    dry 모드에서는 실 API 어댑터(`orthus...openai_compat`)가 설치 안 된 환경에서도 돌아야 하므로
    그 심볼만 스텁으로 채운 뒤 import한다. 프롬프트(`_SYS`/`_prompt`)와 접기 규칙은
    스텁의 영향을 받지 않는 **진짜 원본 객체**다.
    """
    sys.path.insert(0, str(FUGU / "judge"))
    try:
        import pairwise  # type: ignore[import-not-found]  # noqa: PLC0415

        return pairwise
    except ModuleNotFoundError as exc:
        if not dry:
            raise RuntimeError(
                f"judge/pairwise.py import 실패({exc.name}). 실측 모드는 orthus 런타임 의존성"
                "(httpx 등)이 필요하다. 오프라인 검증은 --dry로 하라."
            ) from exc
        stub_pkg = types.ModuleType("orthus.models.adapters.openai_compat")
        stub_pkg.OpenAIChat = object  # type: ignore[attr-defined]
        for name in ("orthus", "orthus.models", "orthus.models.adapters"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["orthus.models.adapters.openai_compat"] = stub_pkg
        import pairwise  # type: ignore[import-not-found]  # noqa: PLC0415

        return pairwise


def _judge_src_sha() -> str:
    return hashlib.sha256(JUDGE_SRC.read_bytes()).hexdigest()


# ------------------------------------------------------------------- 통계 (stdlib)


def cohen_kappa(pairs: list[tuple[str, str]]) -> float:
    """비가중 Cohen's κ. pairs = [(rater1, rater2), ...]."""
    n = len(pairs)
    if n == 0:
        return float("nan")
    labels = sorted({x for p in pairs for x in p})
    po = sum(1 for a, b in pairs if a == b) / n
    c1, c2 = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum((c1[label] / n) * (c2[label] / n) for label in labels)
    return float("nan") if pe == 1.0 else (po - pe) / (1 - pe)


def quadratic_weighted_kappa(pairs: list[tuple[int, int]]) -> float:
    """순서형(1-5)용 2차 가중 κ — KUDGE 천장의 보조 지표."""
    n = len(pairs)
    if n == 0:
        return float("nan")
    labels = sorted({x for p in pairs for x in p})
    idx = {label: i for i, label in enumerate(labels)}
    k = len(labels)
    if k < 2:
        return float("nan")

    def w(i: int, j: int) -> float:
        return ((i - j) ** 2) / ((k - 1) ** 2)

    obs = [[0.0] * k for _ in range(k)]
    for a, b in pairs:
        obs[idx[a]][idx[b]] += 1 / n
    c1, c2 = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    num = sum(w(i, j) * obs[i][j] for i in range(k) for j in range(k))
    den = sum(
        w(i, j) * (c1[labels[i]] / n) * (c2[labels[j]] / n) for i in range(k) for j in range(k)
    )
    return float("nan") if den == 0 else 1 - num / den


# (구 `bootstrap_ratio_lower` 쌍 단위 부트스트랩은 제거됨 — 유도쌍은 비독립이라 쌍 단위
#  재표집이 CI를 과소추정한다. 주판정 CI는 `cluster_bootstrap_ratio`(instruction 군집)만 쓴다.)


# ------------------------------------------------------------------------ 데이터


def _load(name: str, dry: bool) -> list[dict[str, Any]]:
    path = (FIXTURES if dry else NORM) / f"{name}.jsonl"
    if not path.exists():
        raise SystemExit(
            f"{path} 없음. {'fixtures 손상' if dry else 'download.py를 먼저 실행하라'}."
        )
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


@dataclass
class Pair:
    """한 쌍대 비교 문항. gold는 사람 라벨을 'A'|'B'|'tie'로 정규화한 것."""

    pid: str
    question: str
    ans_a: str
    ans_b: str
    gold: str


def khj_pairs(dry: bool, limit: int | None) -> list[Pair]:
    rows = _load("khj", dry)
    out = [
        Pair(
            pid=f"khj-{i}",
            question=r["instruction"],
            ans_a=r["response_a"],
            ans_b=r["response_b"],
            gold={"A": "A", "B": "B", "Tie": "tie", "tie": "tie"}[r["decision"]],
        )
        for i, r in enumerate(rows)
    ]
    return out[:limit] if limit else out


def mtbench_pairs(dry: bool, limit: int | None) -> list[Pair]:
    """영어 앵커. 프로토콜을 한국어 쪽과 동일하게 유지하려고 **turn 1 단일 Q/A만** 쓰고,
    한 쌍에 붙은 여러 주석자 투표는 다수결로 접는다(동수는 tie)."""
    rows = [r for r in _load("mtbench_human", dry) if int(r["turn"]) == 1]
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[(r["question_id"], r["model_a"], r["model_b"])].append(r)

    pairs: list[Pair] = []
    for (qid, ma, mb), votes in sorted(grouped.items(), key=lambda kv: str(kv[0])):
        tally = Counter(v["winner"] for v in votes)
        top = tally.most_common()
        gold = "tie" if len(top) > 1 and top[0][1] == top[1][1] else top[0][0]
        gold = {"model_a": "A", "model_b": "B", "tie": "tie"}[gold]
        conv_a, conv_b = votes[0]["conversation_a"], votes[0]["conversation_b"]
        pairs.append(
            Pair(
                pid=f"mtb-{qid}-{ma}-{mb}",
                question=conv_a[0]["content"],
                ans_a=conv_a[1]["content"],
                ans_b=conv_b[1]["content"],
                gold=gold,
            )
        )
    rng = random.Random(SEED)
    rng.shuffle(pairs)
    return pairs[:limit] if limit else pairs


def summeval_pairs(dry: bool, limit: int | None) -> list[Pair]:
    """factuality 축. 문서마다 consistency 격차가 큰 요약 쌍을 만들고, 고-consistency 쪽을
    gold로 둔다. ⚠️ 우리 프롬프트는 종합 선호를 묻지 factuality만 묻지 않으므로 **대리지표**다."""
    pairs: list[Pair] = []
    for r in _load("summeval", dry):
        scored = sorted(
            zip(r["consistency"], r["machine_summaries"], strict=True), key=lambda t: t[0]
        )
        lo_s, lo_t = scored[0]
        hi_s, hi_t = scored[-1]
        if hi_s - lo_s < SUMMEVAL_MIN_GAP:
            continue
        pairs.append(
            Pair(
                pid=f"summ-{r['id']}",
                question=(
                    "다음 원문에 대한 두 요약 중 어느 쪽이 원문 사실과 더 일치하는가?\n\n"
                    f"[원문]\n{r['text']}"
                ),
                ans_a=hi_t,
                ans_b=lo_t,
                gold="A",
            )
        )
    return pairs[:limit] if limit else pairs


# ------------------------------------------------------------------------ 판정 실행


class FixtureJudge:
    """dry 모드 전용 결정론 스텁. 네트워크·키 없이 파이프라인 전체를 태운다.

    `fixtures/dry_verdicts.json`에 문항 id별 **내용 선호**와 **위치 편향 여부**를 심어 두었다.
    스텁은 프롬프트에 실제로 실린 A/B 텍스트를 보고 답하므로, 스왑 접기와 flip rate 코드
    경로가 실측과 똑같이 동작한다. 숫자는 합성이며 실측 판정자 성능이 아니다.
    """

    def __init__(self) -> None:
        self._planted = json.loads((FIXTURES / "dry_verdicts.json").read_text("utf-8"))
        self._current: dict[str, Any] | None = None
        self._a_text = ""

    def arm(self, pid: str, ans_a: str) -> None:
        self._current = self._planted.get(pid)
        self._a_text = ans_a

    def complete(self, _sys: str, prompt: str, json_only: bool = False) -> str:  # noqa: ARG002
        plan = self._current or {"prefers": "none", "position_biased": False}
        if plan["position_biased"]:
            winner = "A"  # 내용과 무관하게 앞자리를 고른다 = 순수 위치 편향
        elif plan["prefers"] == "none":
            winner = "tie"
        else:
            slot_a_is_orig_a = f"[답변 A]\n{self._a_text}" in prompt
            want_a = plan["prefers"] == "a"
            winner = "A" if want_a == slot_a_is_orig_a else "B"
        return json.dumps({"winner": winner, "reason": "fixture"})


def run_pairwise(
    pw: types.ModuleType, judge: Any, pairs: list[Pair], group: str, verbose: bool
) -> list[dict[str, Any]]:
    """`judge/pairwise.py`의 양방향 접기 규칙을 그대로 적용한다.

    v_fwd = judge(q, A=a, B=b), v_rev = judge(q, A=b, B=a).
    두 방향이 일치할 때만 승패를 인정하고, 흔들리면 tie로 접는다 (pairwise.py L74-79 동일).
    """
    records: list[dict[str, Any]] = []
    for i, p in enumerate(pairs):
        if isinstance(judge, FixtureJudge):
            judge.arm(p.pid, p.ans_a)
        v_fwd = pw.judge_once(judge, p.question, p.ans_a, p.ans_b)  # A=a, B=b
        if isinstance(judge, FixtureJudge):
            judge.arm(p.pid, p.ans_a)
        v_rev = pw.judge_once(judge, p.question, p.ans_b, p.ans_a)  # A=b, B=a (스왑)

        if v_fwd == "A" and v_rev == "B":
            collapsed = "A"
        elif v_fwd == "B" and v_rev == "A":
            collapsed = "B"
        else:
            collapsed = "tie"  # 위치에 흔들림 → 신뢰불가

        unswapped_rev = {"A": "B", "B": "A", "tie": "tie"}[v_rev]
        records.append(
            {
                "id": p.pid,
                "gold": p.gold,
                "v_fwd": v_fwd,
                "v_rev": v_rev,
                "pred": collapsed,
                "flip": unswapped_rev != v_fwd,
                "front_bias": v_fwd == "A" and v_rev == "A",
            }
        )
        if verbose and (i + 1) % 50 == 0:
            print(f"    {group}: {i + 1}/{len(pairs)}", flush=True)
    return records


def score(records: list[dict[str, Any]]) -> dict[str, Any]:
    pairs = [(r["gold"], r["pred"]) for r in records]
    n = len(records)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "accuracy": sum(1 for g, p in pairs if g == p) / n,
        "kappa": cohen_kappa(pairs),
        "flip_rate": sum(1 for r in records if r["flip"]) / n,
        "front_bias_rate": sum(1 for r in records if r["front_bias"]) / n,
        "pred_dist": dict(Counter(r["pred"] for r in records)),
        "gold_dist": dict(Counter(r["gold"] for r in records)),
    }


# --------------------------------------------------------------- KUDGE 천장 (human-human)


def _ceiling_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    str_pairs = [(str(r["score1"]), str(r["score2"])) for r in rows]
    int_pairs = [(int(r["score1"]), int(r["score2"])) for r in rows]
    return {
        "n": len(rows),
        "kappa": cohen_kappa(str_pairs),
        "quadratic_weighted_kappa": quadratic_weighted_kappa(int_pairs),
        "exact_agreement": sum(1 for a, b in int_pairs if a == b) / len(int_pairs),
        "within_1_agreement": sum(1 for a, b in int_pairs if abs(a - b) <= 1) / len(int_pairs),
        "_pairs": str_pairs,
    }


def human_human_ceiling(dry: bool, variant: str = "card") -> dict[str, Any]:
    """KUDGE 주석자 2인 **포인트와이즈** 천장 (5점 척도).

    ⚠️ 이 함수의 κ는 **주판정 분모로 쓰면 안 된다.** 우리 판정자는 명목 A/B/tie 쌍대인데
    이건 순서형 1-5 포인트와이즈다. 라벨 공간·과제·데이터셋이 전부 다르므로 두 κ의 비율은
    의미 있는 양이 아니다(coordinator 확인, 2026-07-21). 쌍대 공간의 천장은
    `induced_pair_ceiling()`을 쓴다. 이 함수는 이제 **기술통계·데이터 카드 대조용**이다.

    `healthy?` 필터: 사전선언 개정으로 **card(오류플래그 행 제외)가 기본**이다. HAERAE 자신도
    `Pointwise` config(2,506행)에서 정확히 같은 필터를 적용했음을 실측 확인했다
    (card 필터 결과 2,506행과 행수 일치, score1/score2 불일치 0건).
    `prereg`(문언 그대로 남김)는 대조용으로 남긴다.
    """
    rows = _load("kudge_human", dry)
    scored = [r for r in rows if str(r["score2"]) != "-1"]  # -1 = 미주석 센티널
    healthy = [r for r in scored if not str(r.get("healthy?", "")).strip()]

    prereg, card = _ceiling_stats(scored), _ceiling_stats(healthy)
    chosen = prereg if variant == "prereg" else card
    return {
        "variant_used": variant,
        "n_total": len(rows),
        "n_excluded_sentinel": len(rows) - len(scored),
        "n_excluded_unhealthy": len(scored) - len(healthy),
        "exclusion_rate": (len(rows) - len(chosen["_pairs"])) / len(rows),
        "variants": {
            "prereg": {k: v for k, v in prereg.items() if not k.startswith("_")},
            "card": {k: v for k, v in card.items() if not k.startswith("_")},
        },
        **{k: v for k, v in chosen.items() if k != "n"},
        "n_used": chosen["n"],
    }


# ------------------------------------------------- KUDGE 쌍대 천장 (induced pairs)


def _kudge_usable(dry: bool, variant: str) -> dict[str, list[dict[str, Any]]]:
    """instruction → 사용 가능한 응답 행. 동일 응답 텍스트는 중복 제거한다."""
    by: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in _load("kudge_human", dry):
        if str(r["score2"]) == "-1":
            continue
        if variant == "card" and str(r.get("healthy?", "")).strip():
            continue
        by[r["instruction"]].setdefault(r["response"], r)
    return {k: list(v.values()) for k, v in by.items() if len(v) >= 2}


def _induced(delta: int, tie_band: int) -> str:
    return "tie" if abs(delta) <= tie_band else ("A" if delta > 0 else "B")


def induced_pair_ceiling(dry: bool, variant: str, tie_band: int) -> dict[str, Any]:
    """**쌍대 공간의** human-human 천장.

    KUDGE Human Annotations는 instruction 90개 × 모델 32개 완전 격자라, 한 instruction 안의
    응답 두 개를 집으면 각 주석자의 점수차가 곧 선호(A/B/tie)가 된다. 이렇게 유도한 쌍은
    우리 판정자와 **같은 명목 라벨 공간**에 있으므로, 여기서 나온 κ가 비율의 정당한 분모다.

    ⚠️ 유도쌍은 **독립이 아니다** — 응답 하나가 ~30개 쌍에 재등장하고 쌍은 instruction으로
    군집화된다. 신뢰구간은 쌍이 아니라 **instruction 단위 군집 부트스트랩**으로 잡아야 한다.
    """
    by = _kudge_usable(dry, variant)
    per_instr: dict[str, list[tuple[str, str]]] = {}
    for instr, rows in by.items():
        prefs: list[tuple[str, str]] = []
        for a, b in itertools.combinations(rows, 2):
            d1 = int(a["score1"]) - int(b["score1"])
            d2 = int(a["score2"]) - int(b["score2"])
            prefs.append((_induced(d1, tie_band), _induced(d2, tie_band)))
        per_instr[instr] = prefs
    flat = [p for v in per_instr.values() for p in v]
    consensus = sum(1 for a, b in flat if a == b)
    return {
        "tie_band": tie_band,
        "variant": variant,
        "n_instructions": len(by),
        "n_responses": sum(len(v) for v in by.values()),
        "n_pairs": len(flat),
        "kappa": cohen_kappa(flat),
        "raw_agreement": consensus / len(flat) if flat else float("nan"),
        "consensus_retained": consensus,
        "annotator1_dist": dict(Counter(a for a, _ in flat)),
        "_per_instruction": per_instr,
    }


@dataclass
class InducedPair:
    """한 유도쌍. `pref1`/`pref2`는 두 주석자의 점수차가 유도한 선호(A/B/tie)."""

    pid: str
    instruction: str
    ans_a: str
    ans_b: str
    pref1: str
    pref2: str

    @property
    def consensus(self) -> bool:
        return self.pref1 == self.pref2

    @property
    def gold(self) -> str | None:
        return self.pref1 if self.consensus else None


def sample_induced_pairs(
    dry: bool, variant: str, tie_band: int, per_instruction: int, seed: int = SEED
) -> dict[str, list[InducedPair]]:
    """instruction마다 유도쌍을 **전수에서** 균등 표본한다(합의쌍만 고르지 않는다).

    분모(천장)는 이 표본 전체 위에서 human-human κ로 잰다 — κ가 성립하려면 불일치쌍이
    있어야 하므로 합의쌍만 남기면 안 된다(합의쌍만 두면 두 주석자가 항상 같아 κ가 자명히 1).
    분자(판정자)는 이 표본의 **합의 부분집합**(gold 정의됨) 위에서만 잰다. 둘 다 같은 표본 S에서
    파생되고, cluster bootstrap은 이 dict의 instruction을 재표집한다.

    ⚠️ 점수차로 쌍을 선별하지 않는다. KUDGE 공식 `Pairwise` config(818행)는 평균점수 격차
    ≥1.0으로 선별돼 정답이 쉬워지고 라벨 분포가 무너져 천장 κ가 -0.06으로 붕괴한다. 여기서는
    per_instruction개를 무작위 균등 표본만 한다(합의 요건은 분자 단계에서 적용).
    """
    by = _kudge_usable(dry, variant)
    rng = random.Random(seed)
    sample: dict[str, list[InducedPair]] = {}
    for instr in sorted(by):
        rows = by[instr]
        cands: list[InducedPair] = []
        for a, b in itertools.combinations(rows, 2):
            p1 = _induced(int(a["score1"]) - int(b["score1"]), tie_band)
            p2 = _induced(int(a["score2"]) - int(b["score2"]), tie_band)
            cands.append(
                InducedPair(
                    pid=f"kudge-{a['uuid']}-{b['uuid']}",
                    instruction=instr,
                    ans_a=a["response"],
                    ans_b=b["response"],
                    pref1=p1,
                    pref2=p2,
                )
            )
        rng.shuffle(cands)
        sample[instr] = cands[:per_instruction]
    return sample


def cluster_bootstrap_ratio(
    sample: dict[str, list[InducedPair]],
    pred_by_pid: dict[str, str],
    *,
    n_resamples: int = BOOTSTRAP_N,
    seed: int = SEED,
) -> dict[str, float]:
    """ratio = judge_κ / human_human_κ 의 95% CI, **instruction 단위 군집 부트스트랩**.

    유도쌍은 독립이 아니므로(응답 1개가 ~30쌍에 재등장 + instruction 군집) 쌍이 아니라
    **instruction을 복원추출**한다. 각 재표본에서 천장(전체 쌍)과 분자(합의쌍) κ를 다시 재고
    비율을 모은다. 천장이 붕괴한(κ≤0/NaN) replicate는 비율이 정의 안 되므로 제외한다.
    """
    instrs = list(sample)
    rng = random.Random(seed)
    ratios: list[float] = []
    for _ in range(n_resamples):
        chosen = [instrs[rng.randrange(len(instrs))] for _ in range(len(instrs))]
        ceil_pairs: list[tuple[str, str]] = []
        num_pairs: list[tuple[str, str]] = []
        for ins in chosen:
            for p in sample[ins]:
                ceil_pairs.append((p.pref1, p.pref2))
                if p.consensus and p.pid in pred_by_pid:
                    num_pairs.append((p.gold, pred_by_pid[p.pid]))  # type: ignore[arg-type]
        k_ceil, k_num = cohen_kappa(ceil_pairs), cohen_kappa(num_pairs)
        if k_ceil != k_ceil or k_num != k_num or k_ceil <= 0:
            continue
        ratios.append(k_num / k_ceil)
    ratios.sort()
    if not ratios:
        return {
            "lower": float("nan"),
            "upper": float("nan"),
            "median": float("nan"),
            "n_valid": 0.0,
        }
    return {
        "lower": ratios[int(0.025 * len(ratios))],
        "upper": ratios[min(len(ratios) - 1, int(0.975 * len(ratios)))],
        "median": statistics.median(ratios),
        "n_valid": float(len(ratios)),
    }


def induced_main_judgment(
    pw: types.ModuleType,
    judge: Any,
    *,
    dry: bool,
    variant: str,
    tie_band: int,
    per_instruction: int,
    verbose: bool,
) -> dict[str, Any]:
    """사전선언 개정판 주판정 (`b4-prereg.md` §3 "결함 B 해소").

    분자·분모를 **같은 라벨 공간(명목 A/B/tie)** + **같은 표본 S**에서 잰다:
      분모 = 표본 전체 유도쌍의 human-human κ (annotator1 유도선호 vs annotator2)
      분자 = 같은 표본의 합의 부분집합에서 우리 판정자(위치 스왑, 양방향 접기) vs 사람 gold κ
      비율 = 분자 / 분모, CI = instruction 군집 부트스트랩 하한 ≥ 0.80 이면 PASS
    """
    sample = sample_induced_pairs(dry, variant, tie_band, per_instruction)
    all_pairs = [p for v in sample.values() for p in v]
    consensus = [p for p in all_pairs if p.consensus]

    judge_input = [
        Pair(pid=p.pid, question=p.instruction, ans_a=p.ans_a, ans_b=p.ans_b, gold=p.gold)  # type: ignore[arg-type]
        for p in consensus
    ]
    records = run_pairwise(pw, judge, judge_input, "kudge_induced", verbose)
    pred_by_pid = {r["id"]: r["pred"] for r in records}

    ceil_k = cohen_kappa([(p.pref1, p.pref2) for p in all_pairs])
    num_k = cohen_kappa([(p.gold, pred_by_pid[p.pid]) for p in consensus])  # type: ignore[arg-type]
    ratio = num_k / ceil_k if ceil_k and ceil_k == ceil_k else float("nan")
    boot = cluster_bootstrap_ratio(sample, pred_by_pid)
    return {
        "variant": variant,
        "tie_band": tie_band,
        "per_instruction": per_instruction,
        "n_instructions": len(sample),
        "n_sampled_pairs": len(all_pairs),
        "n_consensus_pairs": len(consensus),
        "n_dropped_non_consensus": len(all_pairs) - len(consensus),
        "ceiling_kappa": ceil_k,
        "judge_kappa": num_k,
        "judge_accuracy": (
            sum(1 for p in consensus if p.gold == pred_by_pid[p.pid]) / len(consensus)
            if consensus
            else float("nan")
        ),
        "judge_flip_rate": (
            sum(1 for r in records if r["flip"]) / len(records) if records else float("nan")
        ),
        "ratio": ratio,
        "ratio_ci": boot,
        "gold_dist": dict(Counter(p.gold for p in consensus)),
    }


# ---------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="B4/X1 판정자-사람 일치 측정")
    ap.add_argument(
        "--dry",
        action="store_true",
        help="합성 fixture + 결정론 스텁 판정자로 오프라인 실행(API 키 불필요)",
    )
    ap.add_argument("--limit-khj", type=int, default=None, help="KHJ 문항 수 상한(기본 전량 694)")
    ap.add_argument("--limit-anchor", type=int, default=300, help="영어 앵커 쌍 상한(0=끄기)")
    ap.add_argument("--limit-summeval", type=int, default=100, help="summeval 쌍 상한(0=끄기)")
    ap.add_argument("--model", default="gpt-4o", help="실측 판정자 모델(기본 = 기존 t2 판정자)")
    ap.add_argument(
        "--ceiling-filter",
        choices=("prereg", "card"),
        default="card",
        help="KUDGE `healthy?` 오류플래그 행 처리. card=제외(기본, 사전선언 개정 2026-07-21 — "
        "HAERAE `Pointwise` config와 동일 필터임을 실측 확인) / prereg=문언대로 남김(대조용)",
    )
    ap.add_argument(
        "--tie-band",
        type=int,
        default=0,
        choices=(0, 1, 2),
        help="유도쌍 선호 판정의 tie 폭. |score1-score2|<=band 면 tie (기본 0, 사전선언 잠금)",
    )
    ap.add_argument(
        "--per-instruction",
        type=int,
        default=8,
        help="주판정 유도쌍 표본: instruction당 몇 쌍 뽑을지 (기본 8, 사전선언 잠금)",
    )
    ap.add_argument("--out", default=None, help="결과 JSON 경로(기본 .cache/b4_x1_results.json)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    pw = _import_pairwise(args.dry)
    if args.dry:
        judge: Any = FixtureJudge()
    else:
        import os  # noqa: PLC0415

        judge = pw.OpenAIChat(
            os.environ["ORTHUS_LLM_BASE_URL"], os.environ["ORTHUS_LLM_API_KEY"], args.model
        )

    mode = "DRY (합성 fixture · 결정론 스텁 판정자)" if args.dry else f"LIVE (judge={args.model})"
    print(f"== B4 / X1 판정자-사람 일치 ==  {mode}")
    print(f"   판정자 소스 judge/pairwise.py sha256={_judge_src_sha()[:16]}…")
    print(f"   프롬프트 재사용 확인: _SYS {len(pw._SYS)}자, _prompt() import 성공\n")

    print("[1] 포인트와이즈 기술통계 — KUDGE Human Annotations (score1 vs score2, 순서형 1-5)")
    hh = human_human_ceiling(args.dry, args.ceiling_filter)
    pv, cv = hh["variants"]["prereg"], hh["variants"]["card"]
    print(
        f"    센티널 -1 제외 {hh['n_excluded_sentinel']}건 / "
        f"주석오류(healthy?) 플래그 {hh['n_excluded_unhealthy']}건"
    )
    print(
        f"    {'변형':<8}{'n':>7}{'κ(비가중)':>12}{'κ(2차가중)':>12}{'완전일치':>10}{'±1이내':>10}"
    )
    for name, v in (("prereg", pv), ("card", cv)):
        mark = " ←기본" if name == hh["variant_used"] else ""
        print(
            f"    {name:<8}{v['n']:>7}{v['kappa']:>12.4f}"
            f"{v['quadratic_weighted_kappa']:>12.4f}{v['exact_agreement']:>10.1%}"
            f"{v['within_1_agreement']:>10.1%}{mark}"
        )
    print("    ⚠️ 이 κ는 순서형 1-5 포인트와이즈다. 우리 판정자(명목 A/B/tie 쌍대)와 라벨 공간이")
    print("       달라 **비율의 분모로 쓸 수 없다**. 쌍대 천장은 [1b]를 본다.")

    print(
        f"\n[1b] 쌍대 천장 — KUDGE 유도쌍 (같은 instruction 내 응답쌍, filter={args.ceiling_filter})"
    )
    ind = {b: induced_pair_ceiling(args.dry, args.ceiling_filter, b) for b in (0, 1, 2)}
    base = ind[args.tie_band]
    print(
        f"    instruction {base['n_instructions']}개 · 응답 {base['n_responses']}개 → "
        f"유도쌍 {base['n_pairs']:,}개"
    )
    print(f"    {'tie band':<10}{'κ':>10}{'raw agree':>12}{'합의쌍':>10}   주석자1 라벨분포")
    for b, v in ind.items():
        mark = " ←기본" if b == args.tie_band else ""
        print(
            f"    |d|<={b:<6}{v['kappa']:>10.4f}{v['raw_agreement']:>12.1%}"
            f"{v['consensus_retained']:>10,}   {v['annotator1_dist']}{mark}"
        )
    print("    ⚠️ 유도쌍은 독립이 아니다(응답 1개가 ~30쌍에 재등장, instruction으로 군집).")
    print("       CI는 쌍이 아니라 **instruction 단위 군집 부트스트랩**으로 잡아야 한다.")

    print(
        "\n[2] 보조 코로보레이션 — Korean-Human-Judgements 694 (우리 pairwise 프롬프트 + 위치 스왑)"
    )
    print("    (독립 코퍼스. 비율의 분자로 쓰지 않는다 — 주판정은 [5]의 유도쌍이다.)")
    kp = khj_pairs(args.dry, args.limit_khj)
    kr = run_pairwise(pw, judge, kp, "khj", args.verbose)
    ks = score(kr)
    print(f"    n={ks['n']}  일치율 {ks['accuracy']:.1%}  κ = {ks['kappa']:.4f}")
    print(
        f"    위치 스왑 flip rate = {ks['flip_rate']:.1%} "
        f"(앞자리 고정 선택 {ks['front_bias_rate']:.1%})"
    )
    print(f"    판정 분포 {ks['pred_dist']} / 사람 분포 {ks['gold_dist']}")

    anchor: dict[str, Any] = {"n": 0, "skipped": True}
    if args.limit_anchor:
        print("\n[3] 영어 앵커 — mt_bench_human_judgments (동일 프롬프트, turn 1, 다수결 gold)")
        ap_ = mtbench_pairs(args.dry, args.limit_anchor)
        ar = run_pairwise(pw, judge, ap_, "mtbench", args.verbose)
        anchor = score(ar)
        print(f"    n={anchor['n']}  일치율 {anchor['accuracy']:.1%}  κ = {anchor['kappa']:.4f}")
        print(f"    위치 스왑 flip rate = {anchor['flip_rate']:.1%}")

    summ: dict[str, Any] = {"n": 0, "skipped": True}
    if args.limit_summeval:
        print(f"\n[4] factuality 축 — summeval consistency 격차 ≥ {SUMMEVAL_MIN_GAP} 쌍 (대리지표)")
        sp = summeval_pairs(args.dry, args.limit_summeval)
        sr = run_pairwise(pw, judge, sp, "summeval", args.verbose)
        summ = score(sr)
        print(
            f"    n={summ['n']}  고-consistency 쪽 선택률 {summ['accuracy']:.1%}  "
            f"flip rate {summ['flip_rate']:.1%}"
        )

    print("\n[5] 주판정 — 유도쌍 same-label-space 비율 (사전선언 §3 개정 '결함 B 해소')")
    print(
        f"    셋업: KUDGE Human Annotations · filter={args.ceiling_filter} · tie band 0 · "
        f"instruction당 {args.per_instruction}쌍 표본"
    )
    mj = induced_main_judgment(
        pw,
        judge,
        dry=args.dry,
        variant=args.ceiling_filter,
        tie_band=0,
        per_instruction=args.per_instruction,
        verbose=args.verbose,
    )
    mb = mj["ratio_ci"]
    # 검정력 가시화: 표본 쌍 수 · 군집 부트스트랩 instruction 수 · 합의 탈락 수를 먼저 찍는다.
    print(
        f"    표본 유도쌍 {mj['n_sampled_pairs']:,}개 "
        f"(합의 {mj['n_consensus_pairs']:,} = 분자 대상 / 비합의 {mj['n_dropped_non_consensus']:,} 탈락)"
    )
    print(
        f"    군집 부트스트랩 instruction 수 = {mj['n_instructions']}  "
        f"(instruction 복원추출, 쌍 아님)"
    )
    print(
        f"    분모 천장 human-human κ = {mj['ceiling_kappa']:.4f}  "
        f"(표본 전체 쌍, 불일치 포함 — [1b] 전수 κ의 표본 추정)"
    )
    print(
        f"    분자 judge κ = {mj['judge_kappa']:.4f}  "
        f"(합의쌍, 판정자 일치율 {mj['judge_accuracy']:.1%} · flip {mj['judge_flip_rate']:.1%})"
    )
    print(f"    사람 gold 분포 {mj['gold_dist']}")
    verdict = "PASS" if mb["lower"] >= PREREG_RATIO else "FAIL"
    print(
        f"    비율 = {mj['ratio']:.4f}  ·  cluster bootstrap {BOOTSTRAP_N:,}회 seed={SEED} "
        f"· 유효 replicate {int(mb['n_valid']):,}"
    )
    print(
        f"    95% CI = [{mb['lower']:.4f}, {mb['upper']:.4f}]   하한 vs 기준 "
        f"{PREREG_RATIO} → **{verdict}**"
    )
    if verdict == "PASS":
        print("    문안: 판정 승률은 사람 판단의 대리지표로 유효.")
    else:
        print("    문안: 모든 judge-scored 결과에 이 비율을 병기하고 헤드라인에서 내린다.")
    if args.dry:
        print(
            f"    ⚠️ DRY — 합성 fixture(instruction {mj['n_instructions']}개)의 숫자다. "
            "군집 부트스트랩 경로만 실증할 뿐 인용 금지."
        )

    payload = {
        "mode": "dry" if args.dry else "live",
        "judge_model": None if args.dry else args.model,
        "judge_src_sha256": _judge_src_sha(),
        "seed": SEED,
        "bootstrap_resamples": BOOTSTRAP_N,
        "prereg_ratio_threshold": PREREG_RATIO,
        "ceiling_filter": args.ceiling_filter,
        "main_judgment_induced": {k: v for k, v in mj.items() if not k.startswith("_")},
        "human_human_pointwise": {k: v for k, v in hh.items() if not k.startswith("_")},
        "human_human_induced_pairwise": {
            str(b): {
                k: v
                for k, v in induced_pair_ceiling(args.dry, args.ceiling_filter, b).items()
                if not k.startswith("_")
            }
            for b in (0, 1, 2)
        },
        "judge_human_korean_secondary": ks,
        "english_anchor": anchor,
        "summeval_factuality": summ,
        "verdict": verdict,
    }
    out = Path(args.out) if args.out else CACHE / "b4_x1_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n    결과 → {out}  (gitignored — 판정 원문은 커밋하지 않는다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
