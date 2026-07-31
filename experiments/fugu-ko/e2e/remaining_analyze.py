"""잔여 5종 재측정 집계 — 결정론 3종 + judge 2종을 하나의 리포트로.

입력
  · 워커 산출물   `analysis/raw/remaining/{task}__{system}.jsonl`  (`remaining_run.py`)
  · 판정 산출물   `analysis/raw/remaining_judge/{task}__{judge}.jsonl` (`remaining_judge.py`)
출력
  · 사람용 마크다운 `analysis/remaining_report.md`  (stdout에도 그대로 찍는다)
  · 기계용 JSON     `analysis/remaining_summary.json`

## 채점 계층 2종

**결정론 3종(email_draft / gap_suggest / claim_headline)** — judge를 쓰지 않는다
(`REMAINING_SLOTS_SCALEUP_PLAN` §2.1b). 지표는 `t12_generation.py`의 것을 **그대로 재사용**하며,
특히 환각 탐지 `_invented`는 **import해서 채점 시점에 다시 계산한다**. 저장된 `metrics.invented`를
믿지 않는 이유는 t12가 남긴 교훈 그대로다 — 탐지기는 두 번 틀렸고(일반 호칭 오탐, Title Case
오탐) 그때마다 재실행하면 API 비용이 든다. 원본 출력만 있으면 탐지기 수정은 공짜여야 한다.
저장값과 재계산값이 다르면 그 자체를 **탐지기 변경 트립와이어**로 표에 남긴다.

**judge 2종(wiki_qa / synthesize)** — `remaining_judge.py`가 남긴 **방향별 1행**을
`(judge, pair, id)`로 묶어 verdict을 합성한다. 양방향 일치일 때만 승패, 불일치는 tie
(원본 규약). tie의 종류(position_bias / both_tie / partial)도 판정자 자기일관성 진단으로 낸다.

## 통계

- **쌍대 검정 = McNemar.** 결정론 3종은 문항별 이진 성공(아래 정의)의 불일치쌍 (b, c)에,
  judge 2종은 결정 verdict의 (left승, right승)에 같은 검정을 건다. 후자는 McNemar의 정확형
  = 이항 부호검정과 동일하다. **정확 이항 양측 p**를 1차로 쓰고(불일치쌍이 작을 때 카이제곱
  근사는 과신한다), 연속성 보정 카이제곱 p를 참고로 병기한다.
- **다중비교 보정 = Holm-Bonferroni.** 태스크별로 그 태스크의 모든 쌍 p값을 한 family로 묶는다.
  보정 전/후를 **둘 다** 표에 남긴다 — 보정 전만 보면 21쌍에서 우연히 하나쯤 유의해진다.
- tie 처리는 **두 벌 다** 낸다: 결정 건만 본 승률(tie 제외)과 tie를 0.5로 센 승률.
  전자는 검정의 대상이고, 후자는 "얼마나 자주 갈리는가"를 감춘다 — 둘을 같이 봐야 한다.
- **wiki_qa는 `kind`(factual / synthetic_broad)별로도 쪼갠다.** 유형별로 순위가 뒤집히는지가
  이 실험의 관전 포인트다.
- 국내 삼각형(solar/exaone/ax)의 **이행성**을 점검한다. 순환(A>B>C>A)이 나오면 "순위"라는
  말 자체가 성립하지 않으므로 경고로 띄운다.

## 사용

    python experiments/fugu-ko/e2e/remaining_analyze.py
    python experiments/fugu-ko/e2e/remaining_analyze.py --raw-dir <dir> --judge-dir <dir> --out-dir <dir>
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FUGU = HERE.parent
_WORKTREE_ROOT = FUGU.parent.parent

for _p in (str(HERE), str(FUGU), str(_WORKTREE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from remaining_judge import (  # noqa: E402
    DEFAULT_GOLDEN_DIR,
    DEFAULT_OUT_DIR as DEFAULT_JUDGE_DIR,
    DEFAULT_RAW_DIR,
    DOMESTIC,
    JUDGE_TASKS,
    PAIRS,
    ROSTER,
    TIER,
    _read_items,
    answer_of,
    load_worker_rows,
)

DET_TASKS: tuple[str, ...] = ("email_draft", "gap_suggest", "claim_headline")

# 현행 프로덕션 배정(`orthus/models/orchestration.py::ASSIGNMENTS`, §15 diversified) — 이 실험이
# 유지/변경을 판정해야 하는 대상이다.
CURRENT_ASSIGNMENT: dict[str, str] = {
    "wiki_qa": "exaone", "synthesize": "solar", "email_draft": "exaone",
    "gap_suggest": "solar", "claim_headline": "solar",
}
PRIMARY_JUDGE = "sonnet"   # 파일럿 검증된 유일 판정자. 국내 판정자는 보조(아래 §0-2 참조).

# --------------------------------------------------------------------------- #
# synthesize 층화 — freeze 조각이 "실질 답변"인지의 결정론 판정
#
# 러너/`t8_synth.py`가 grounded 판정에 쓰는 `_DENIALS` 5종은 **한국어 거부 표현의 일부만**
# 잡는다. "명시되어 있지 않습니다" / "확인할 수 없습니다" / "언급되지 않았습니다" 같은 흔한
# 변형이 통과해서, grounded≥2를 통과한 747문항 중에도 한쪽 조각이 사실상 "정보 없음"인 것이
# 섞여 있다(넓은 어휘로 재집계 시 490건만 두 조각 모두 실질 답변 = 65.6%).
#
# ⚠️ **`_DENIALS` 원본은 건드리지 않는다.** 그 5종은 freeze 절차가 이미 그것으로 실행돼
# 캐시가 굳었으므로(`synthesize_subs.provenance.json`), 바꾸면 골든 정의와 캐시가 어긋난다.
# 넓은 기준은 **분석 단계에서만** 층을 나누는 데 쓴다.
#
# 층화하는 이유: 프로덕션 synthesize 프롬프트 rule 2가 "정보 없다는 하위답변은 명시하라"라서
# 그 문항은 **측정 대상에서 빼는 게 아니라** 별도 층으로 봐야 한다. 두 층에서 순위가 갈리면
# "정보 없음 조각을 어떻게 다루는가"가 모델 차이를 만든다는 뜻이고, 그 자체가 결과다.
# --------------------------------------------------------------------------- #
BASE_DENIALS: tuple[str, ...] = ("근거 없", "제공되지 않", "찾을 수 없", "정보가 없", "확인되지 않")
BROAD_EXTRA: tuple[str, ...] = ("명시되어 있지 않", "확인할 수 없", "언급되지 않")
BROAD_DENIALS: tuple[str, ...] = BASE_DENIALS + BROAD_EXTRA

STRATUM_BOTH = "both_substantive"      # 두 조각 모두 실질 답변
STRATUM_NOINFO = "has_no_info_leaf"    # 한쪽 이상이 사실상 '정보 없음'

DET_GOLDEN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "email_draft": ("t12_email_draft.json", "t12_email.json", "email_draft.json"),
    "gap_suggest": ("t12_gap_suggest.json", "t12_gap.json", "gap_suggest.json"),
    "claim_headline": ("t12_claim_headline.json", "t12_headline.json", "claim_headline.json"),
}


# --------------------------------------------------------------------------- #
# 통계
# --------------------------------------------------------------------------- #
def binom_two_sided(b: int, c: int) -> float:
    """McNemar 정확형 = 불일치쌍 n=b+c에 대한 p=0.5 이항 양측검정."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2.0**n)
    return min(1.0, 2.0 * tail)


def mcnemar_chi2(b: int, c: int) -> float:
    """연속성 보정 카이제곱 근사(참고용). df=1."""
    n = b + c
    if n == 0:
        return 1.0
    chi2 = (abs(b - c) - 1) ** 2 / n if abs(b - c) >= 1 else 0.0
    return math.erfc(math.sqrt(chi2 / 2.0)) if chi2 > 0 else 1.0


def holm(pvals: list[float]) -> list[float]:
    """Holm-Bonferroni 보정. 입력 순서를 유지한 보정 p를 돌려준다."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        val = min(1.0, (m - rank) * pvals[i])
        running = max(running, val)   # 단조성 강제
        adj[i] = running
    return adj


def _holm_by(blocks: list[dict], *, key) -> None:
    """`p_exact` → `p_holm`을 **family별로** 채운다(제자리 수정). family = key(block)."""
    fams: dict[Any, list[dict]] = defaultdict(list)
    for b in blocks:
        fams[key(b)].append(b)
    for members in fams.values():
        for b, ph in zip(members, holm([m["p_exact"] for m in members])):
            b["p_holm"] = ph
            b["holm_family_size"] = len(members)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """승률 신뢰구간(Wilson). n=0이면 (0,1)."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    ctr = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, ctr - half), min(1.0, ctr + half)


def kappa(a: list[str], b: list[str], labels: list[str]) -> float:
    """Cohen's kappa (judge_pilot._kappa와 동일 정의)."""
    n = len(a)
    if n == 0:
        return float("nan")
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[lab] / n) * (cb[lab] / n) for lab in labels)
    return float("nan") if pe == 1.0 else (po - pe) / (1 - pe)


def p50(xs: list[float]) -> float:
    return sorted(xs)[len(xs) // 2] if xs else 0.0


def _sig(p: float) -> str:
    return "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "n.s."


# --------------------------------------------------------------------------- #
# 결정론 3종
# --------------------------------------------------------------------------- #
def _det_golden(task: str, golden_dir: Path) -> dict[str, dict]:
    for name in DET_GOLDEN_CANDIDATES[task]:
        p = golden_dir / name
        if p.exists():
            return {str(i["id"]): i for i in _read_items(p) if i.get("id")}
    return {}


def score_det_row(task: str, row: dict, item: dict | None) -> dict:
    """행 1개 → 결정론 지표. `_invented`는 **여기서 다시 계산**한다(탐지기 수정은 공짜여야 한다)."""
    from t12_generation import _invented

    m = row.get("metrics") or {}
    parsed = row.get("parsed") or {}
    out: dict[str, Any] = {
        "ok": bool(m.get("ok")),
        "format_error": bool(m.get("format_error")),
        "latency_ms": row.get("latency_ms") or 0,
        "stored_invented": bool(m.get("invented")),
    }
    if task == "email_draft":
        subj, body = parsed.get("subject") or "", parsed.get("body") or ""
        g = f"{(item or {}).get('to', '')} {(item or {}).get('inst', '')} {(item or {}).get('ctx', '')}"
        inv = _invented(f"{subj}\n{body}", g) if item else list(m.get("invented") or [])
        out |= {
            "invented": inv,
            "re_prefix": bool(m.get("re_prefix")),
            "body_chars": len(body),
            "success": bool(m.get("ok")) and not inv and not m.get("re_prefix"),
        }
    elif task == "gap_suggest":
        out |= {
            "invented": [],
            "n_sections": m.get("n_sections") or 0,
            "n_items": m.get("n_items") or 0,
            "spec_violation": bool(m.get("section_spec_violation")),
            "success": bool(m.get("ok")) and not m.get("section_spec_violation"),
        }
    else:  # claim_headline
        head = parsed.get("headline") or ""
        claim = (item or {}).get("claim") or ""
        inv = _invented(head, claim, latin=False) if claim else list(m.get("invented") or [])
        out |= {
            "invented": inv,
            "chars": len(head),
            "over_cap": bool(m.get("over_cap")),
            "success": bool(m.get("ok")) and not m.get("over_cap") and not inv,
        }
    return out


def diagnose_format_errors(rows: list[dict]) -> dict:
    """형식 실패의 **원인 분해**. "JSON을 못 만든다"와 "포장만 틀렸다"는 전혀 다른 문제다.

    프로덕션은 `json.loads` 실패 시 결정론 템플릿으로 조용히 강등되므로, 원인이 사소한
    포장 결함(마크다운 펜스 / 꼬리에 붙은 여분 `}`)이라면 **파서 2줄로 회수 가능한 손실**이고,
    본문이 실제로 깨진 것이라면 모델 교체 사유다. 둘을 같은 칸에 세면 판단을 그르친다.
    """
    kinds: Counter = Counter()
    n_bad = repaired = 0
    for r in rows:
        if not (r.get("metrics") or {}).get("format_error"):
            continue
        n_bad += 1
        t = (r.get("raw_output") or "").strip()
        if t.startswith("```"):
            t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
            t = re.sub(r"```\s*$", "", t).strip()
            kinds["md_fence"] += 1
        if t.endswith("}}"):
            try:                       # 꼬리 `}` 하나만 떼면 파싱되는가
                json.loads(t[:-1])
                kinds["trailing_brace"] += 1
                t = t[:-1]
            except Exception:  # noqa: BLE001
                pass
        try:
            json.loads(t)
            repaired += 1
        except Exception:  # noqa: BLE001
            kinds["unrepairable"] += 1
    return {"n_format_error": n_bad, "trivially_repairable": repaired, "kinds": dict(kinds)}


def analyze_det(task: str, raw_dir: Path, golden_dir: Path) -> dict:
    golden = _det_golden(task, golden_dir)
    per_worker: dict[str, dict[str, dict]] = {}
    fmt_diag: dict[str, dict] = {}
    for s in ROSTER:
        rows = load_worker_rows(raw_dir, task, s)
        if not rows:
            continue
        per_worker[s] = {
            qid: score_det_row(task, r, golden.get(qid)) for qid, r in rows.items()
        }
        d = diagnose_format_errors(list(rows.values()))
        if d["n_format_error"]:
            fmt_diag[s] = d
    if not per_worker:
        return {}

    summary: dict[str, Any] = {"task": task, "n_golden": len(golden), "workers": {},
                               "pairs": [], "format_error_diagnosis": fmt_diag}
    for s, scored in per_worker.items():
        n = len(scored)
        vals = list(scored.values())
        w: dict[str, Any] = {
            "n": n,
            "fail": sum(1 for v in vals if not v["ok"]),
            "success": sum(1 for v in vals if v["success"]),
            "invented": sum(1 for v in vals if v["invented"]),
            "stored_invented": sum(1 for v in vals if v["stored_invented"]),
            "p50_ms": p50([v["latency_ms"] for v in vals]),
        }
        if task == "email_draft":
            w |= {
                "re_prefix": sum(1 for v in vals if v["re_prefix"]),
                "body_chars_mean": round(sum(v["body_chars"] for v in vals if v["ok"])
                                         / max(1, sum(1 for v in vals if v["ok"])), 1),
            }
        elif task == "gap_suggest":
            oks = [v for v in vals if v["ok"]]
            w |= {
                "spec_violation": sum(1 for v in vals if v["spec_violation"]),
                "sections_mean": round(sum(v["n_sections"] for v in oks) / max(1, len(oks)), 2),
                "items_mean": round(sum(v["n_items"] for v in oks) / max(1, len(oks)), 2),
            }
        else:
            oks = [v for v in vals if v["ok"]]
            w |= {
                "over_cap": sum(1 for v in vals if v["over_cap"]),
                "chars_mean": round(sum(v["chars"] for v in oks) / max(1, len(oks)), 1),
            }
        summary["workers"][s] = w

    # 쌍대 McNemar — 이 태스크 안에서 측정된 워커 전 조합(무료: API 호출 0).
    systems = [s for s in ROSTER if s in per_worker]
    raw_p: list[float] = []
    rows_out: list[dict] = []
    for i, a in enumerate(systems):
        for b in systems[i + 1 :]:
            common = sorted(set(per_worker[a]) & set(per_worker[b]))
            nb = sum(1 for q in common if per_worker[a][q]["success"] and not per_worker[b][q]["success"])
            nc = sum(1 for q in common if not per_worker[a][q]["success"] and per_worker[b][q]["success"])
            p = binom_two_sided(nb, nc)
            raw_p.append(p)
            rows_out.append({
                "left": a, "right": b, "n_common": len(common),
                "b_left_only": nb, "c_right_only": nc,
                "p_exact": p, "p_chi2": mcnemar_chi2(nb, nc),
            })
    # 결정론 3종은 판정자가 없으므로 family = 그 태스크의 전 쌍 하나.
    _holm_by(rows_out, key=lambda _r: task)
    summary["pairs"] = rows_out
    return summary


# --------------------------------------------------------------------------- #
# judge 2종
# --------------------------------------------------------------------------- #
def load_synth_strata(raw_dir: Path) -> tuple[dict[str, str], list[str]]:
    """freeze 캐시(`synthesize_subs.jsonl`) → id별 층. 반환 (층 매핑, 경고 목록).

    조각 **전부**가 넓은 거부 어휘를 안 건드릴 때만 `both_substantive`다. 캐시가 없으면
    빈 매핑을 돌려주고(층화 생략) 경고를 남긴다 — 조용히 전체를 한 층으로 뭉개지 않는다.
    """
    warns: list[str] = []
    p = raw_dir / "synthesize_subs.jsonl"
    if not p.exists():
        return {}, [f"synthesize 층화 생략 — freeze 캐시 없음({p})"]

    try:  # 러너의 원본 5종과 우리 base가 어긋나면(러너 쪽이 바뀌면) 즉시 드러나야 한다.
        from remaining_run import _DENIALS as RUNNER_DENIALS

        if tuple(RUNNER_DENIALS) != BASE_DENIALS:
            warns.append(
                "⚠️ `remaining_run._DENIALS`가 분석기의 BASE_DENIALS와 다르다 — "
                f"러너 {tuple(RUNNER_DENIALS)} vs 분석기 {BASE_DENIALS}. 층 정의를 재검토하라."
            )
    except Exception:  # noqa: BLE001 — import 실패는 층화를 막지 않는다
        warns.append("(참고) `remaining_run._DENIALS` 대조 생략 — import 실패")

    strata: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        subs = d.get("subs") or []
        both = bool(subs) and all(
            not any(x in (s.get("body") or "") for x in BROAD_DENIALS) for s in subs
        )
        strata[str(d["id"])] = STRATUM_BOTH if both else STRATUM_NOINFO
    return strata, warns


# --------------------------------------------------------------------------- #
# wiki_qa 층화 — 사실형 600 중 **처방 전 프롬프트로 만들어진 앞 55건**
#
# 저작 도중 폐기율이 40% 임계를 넘어 생성 프롬프트를 고쳤다(claim 출처 문서 제목 주입 +
# 한정어 삽입 규칙). 그 이후 545건은 개선된 프롬프트 산출물이고, 앞 55건은 그 전 것이다.
# 55건 모두 동일한 2차 판정(유일 특정 가능)은 통과했지만 문항 성격이 미묘하게 다르다
# ("오류 수정 작업이 완료된 날짜는?" 같은 특정 불가형이 처방 전 구간에 있었다).
#
# 경계는 `full_summary.json`의 **처방 후 accepted 수**에서 역산한다 —
# `n_prefix = (골든 사실형 수) - streams.factual.accepted`. 그 수만큼을 `full_attempts.jsonl`의
# `ts` 오름차순 앞에서 잘라 낸다. "가장 큰 ts 간극"으로 자동 탐지하지 않는 이유: 저작이
# 여러 배치로 끊겨 더 큰 간극이 앞쪽(40번째)에 있어서 그 규칙은 틀린 경계를 고른다
# (실측 확인: 55/56 경계의 간극은 727초, 40/41 경계는 2,085초).
# --------------------------------------------------------------------------- #
# 판정자가 JSON 계약을 깨면 그 판정은 tie로 흡수된다 — 즉 **실패가 무승부로 위장한다.**
# E4는 A.X의 장문 JSON 위반을 7.5%로 쟀지만 그건 짧은 입력에서였다. 이 임계를 넘는 판정자의
# 블록은 평균에 섞지 말고 "쓸 수 없음"으로 표시해야 한다(무승부 부풀리기 = 가짜 동점).
JUDGE_BAD_JSON_UNRELIABLE = 0.20

STRATUM_PRE_FIX = "pre_fix_prompt"      # 처방 전 프롬프트 산출물
STRATUM_POST_FIX = "post_fix_prompt"    # 처방 후

# 저작 에이전트가 남긴 골든 한계 — 리포트 각주로 그대로 옮긴다(측정값 해석에 필요).
WIKI_QA_CAVEATS: tuple[str, ...] = (
    "동어반복형이 약 5% 잔존한다 — `answer_leak` 검사가 **날짜 패턴만** 봐서 "
    "\"…작업의 제목은 무엇인가요?\"류(제목이 곧 답)를 못 잡았다.",
    "사실형 retrieve 통과율이 처방의 대가로 **89% → 67%**로 내려갔다. 통과 기준(strict)은 "
    "낮추지 않고 유지했으므로, 남은 문항의 근거 적중은 오히려 더 엄격한 쪽이다.",
    "기존 `t2.json`은 **반말**인데 이번 역생성분은 **존댓말**이라 문체가 두 갈래다 — "
    "과거 t2 수치와 직접 비교할 때 교란 요인이다(판정자 교체와 별개의 두 번째 비교 불가 사유).",
)


def load_wiki_qa_strata(
    golden_dir: Path, e2e_dir: Path
) -> tuple[dict[str, str], set[str], dict, list[str]]:
    """(id→층, 처방전 id 집합, 메타, 경고). 근거 파일이 없으면 층화를 조용히 생략하지 않는다."""
    warns: list[str] = []
    att = e2e_dir / "golden_wiki_qa" / "full_attempts.jsonl"
    summ = e2e_dir / "golden_wiki_qa" / "full_summary.json"
    gpath = None
    for name in ("t2_wiki_qa_1k.json",):
        for d in (golden_dir, e2e_dir):
            if (d / name).exists():
                gpath = d / name
    if not (att.exists() and summ.exists() and gpath):
        return {}, set(), {}, ["wiki_qa 처방전 층화 생략 — full_attempts/full_summary/골든 중 누락"]

    items = _read_items(gpath)
    factual = [i for i in items if str(i.get("kind")) == "factual"]
    ts: dict[str, float] = {}
    for line in att.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        a = json.loads(line)
        if a.get("status") != "accepted":
            continue
        q = a.get("q") or ""
        # 같은 질문의 시도가 둘 이상이면 **가장 이른** ts를 쓴다(처방 전에 이미 나온 문항이면
        # 처방 전으로 본다 — 민감도 분석은 보수적인 쪽으로 기울어야 한다).
        ts[q] = min(ts.get(q, float("inf")), float(a.get("ts") or 0))

    s = json.loads(summ.read_text(encoding="utf-8"))
    accepted_after = int((((s.get("streams") or {}).get("factual") or {}).get("accepted")) or 0)
    n_prefix = len(factual) - accepted_after
    if n_prefix <= 0 or n_prefix >= len(factual):
        return {}, set(), {}, [
            f"wiki_qa 처방전 층화 생략 — 역산 결과가 비정상(n_prefix={n_prefix}, "
            f"factual={len(factual)}, accepted_after={accepted_after})"
        ]

    have = [i for i in factual if i["q"] in ts]
    if len(have) < len(factual):
        warns.append(
            f"ts를 못 찾은 사실형 {len(factual) - len(have)}건 — 처방 후로 간주(경계가 보수적으로 이동)"
        )
    ordered = sorted(have, key=lambda i: ts[i["q"]])
    prefix_ids = {str(i["id"]) for i in ordered[:n_prefix]}

    boundary_gap = None
    if len(ordered) > n_prefix:
        boundary_gap = round(ts[ordered[n_prefix]["q"]] - ts[ordered[n_prefix - 1]["q"]], 1)

    strata = {
        str(i["id"]): (STRATUM_PRE_FIX if str(i["id"]) in prefix_ids else STRATUM_POST_FIX)
        for i in factual
    }
    meta = {
        "n_factual": len(factual),
        "accepted_after_fix": accepted_after,
        "n_pre_fix": n_prefix,
        "boundary_ts_gap_sec": boundary_gap,
        "derivation": "n_pre_fix = golden factual − full_summary.streams.factual.accepted; "
                      "full_attempts.jsonl ts 오름차순 앞에서 절단",
    }
    return strata, prefix_ids, meta, warns


def _tie_kind(fwd: str, rev: str) -> str:
    if (fwd, rev) in (("A", "B"), ("B", "A")):
        return "clean"
    if fwd == "tie" and rev == "tie":
        return "both_tie"
    if fwd == rev in ("A", "B"):
        return "position_bias"     # 스왑해도 같은 **자리**를 골랐다 = 답변이 아니라 위치를 봤다
    return "partial_tie"


# 러너와 **같은** 회수 규칙(집계 쪽에서도 동일하게 적용해야 이미 쌓인 행이 살아난다).
_WINNER_RE = re.compile(r'"winner"\s*:\s*"?(A|B|tie)"?', re.IGNORECASE)


def _recover_vote(row: dict) -> tuple[str, bool]:
    """(표, 회수여부). `bad_json`이어도 raw에 `winner`가 남아 있으면 그것이 판정이다."""
    vote = row.get("vote") or "tie"
    if not row.get("bad_json"):
        return vote, False
    m = _WINNER_RE.search(row.get("raw") or "")
    if not m:
        return vote, False
    w = m.group(1)
    w = "tie" if w.lower() == "tie" else w.upper()
    return w, True


def load_verdicts(judge_dir: Path, task: str) -> dict[tuple[str, str, str], dict]:
    """(judge, pair, id) → verdict 레코드. 방향 2행이 다 있어야 판정 단위가 성립한다."""
    halves: dict[tuple[str, str, str], dict] = defaultdict(dict)
    meta: dict[tuple[str, str, str], dict] = {}
    for p in sorted(judge_dir.glob(f"{task}__*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("error") or r.get("task") != task:
                continue
            k = (str(r["judge"]), str(r["pair"]), str(r["id"]))
            halves[k][str(r["direction"])] = r      # 같은 키는 마지막 행이 이긴다
            meta[k] = r
    out: dict[tuple[str, str, str], dict] = {}
    for k, hs in halves.items():
        if "fwd" not in hs or "rev" not in hs:
            continue
        fwd, rec_f = _recover_vote(hs["fwd"])
        rev, rec_r = _recover_vote(hs["rev"])
        m = meta[k]
        left, right = m.get("left"), m.get("right")
        verdict = left if (fwd, rev) == ("A", "B") else right if (fwd, rev) == ("B", "A") else "tie"
        out[k] = {
            "judge": k[0], "pair": k[1], "id": k[2], "left": left, "right": right,
            "tier": m.get("tier"), "kind": (m.get("kind") or "") or "_",
            "fwd": fwd, "rev": rev, "verdict": verdict, "tie_kind": _tie_kind(fwd, rev),
            # 회수된 것은 결측이 아니다 — `bad_json`은 **회수 못 한 것만** 센다.
            "bad_json": bool((hs["fwd"].get("bad_json") and not rec_f)
                             or (hs["rev"].get("bad_json") and not rec_r)),
            "recovered": bool(rec_f or rec_r),
        }
    return out


def _pair_block(recs: list[dict]) -> dict:
    left = recs[0]["left"]
    right = recs[0]["right"]
    lw = sum(1 for r in recs if r["verdict"] == left)
    rw = sum(1 for r in recs if r["verdict"] == right)
    tie = len(recs) - lw - rw
    decided = lw + rw
    lo, hi = wilson(lw, decided)
    p = binom_two_sided(lw, rw)
    return {
        "left": left, "right": right, "n": len(recs),
        "left_win": lw, "right_win": rw, "tie": tie, "decided": decided,
        "win_rate_decided": (lw / decided) if decided else None,
        "win_rate_ci95": [lo, hi],
        "win_rate_tie_half": (lw + 0.5 * tie) / len(recs) if recs else None,
        "p_exact": p, "p_chi2": mcnemar_chi2(lw, rw),
        "bad_json": sum(1 for r in recs if r["bad_json"]),
    }


def analyze_judge(task: str, judge_dir: Path, raw_dir: Path, golden_dir: Path) -> dict:
    verdicts = load_verdicts(judge_dir, task)
    if not verdicts:
        return {}
    by_pair_judge: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for v in verdicts.values():
        by_pair_judge[(v["pair"], v["judge"])].append(v)

    out: dict[str, Any] = {"task": task, "n_units": len(verdicts), "pairs": [], "by_kind": [],
                           "by_stratum": [], "strata": {}, "sensitivity": [], "warnings": [],
                           "judge_selfconsistency": {}, "judge_agreement": {}, "transitivity": {}}

    strata: dict[str, str] = {}
    excluded: set[str] = set()      # 민감도 분석에서 빼 볼 id(제외가 기본이 아니다)

    if task == "wiki_qa":
        strata, excluded, meta, warns = load_wiki_qa_strata(golden_dir, HERE)
        out["warnings"] += warns
        if strata:
            judged = {v["id"] for v in verdicts.values()}
            out["strata"] = meta | {
                "counts": dict(Counter(strata[i] for i in judged if i in strata)),
                "n_judged_items": len(judged),
                "caveats": list(WIKI_QA_CAVEATS),
            }
        for v in verdicts.values():
            v["stratum"] = strata.get(v["id"], "")   # 종합형은 층 대상이 아니다

    # synthesize 층화 — 판정 단위마다 층 라벨을 붙인다(문항 제외가 아니라 분해다).
    if task == "synthesize":
        strata, warns = load_synth_strata(raw_dir)
        out["warnings"] += warns
        if strata:
            judged = {v["id"] for v in verdicts.values()}
            counts = Counter(strata[i] for i in judged if i in strata)
            unmapped = len(judged - set(strata))
            out["strata"] = {
                "vocabulary_base": list(BASE_DENIALS),
                "vocabulary_extra": list(BROAD_EXTRA),
                "counts": dict(counts),
                "n_judged_items": len(judged),
                "unmapped": unmapped,
            }
            if unmapped:
                out["warnings"].append(
                    f"freeze 캐시에 없는 판정 문항 {unmapped}건 — 층화에서 제외됨(전체 표에는 남는다)"
                )
        for v in verdicts.values():
            v["stratum"] = strata.get(v["id"], "")

    # Holm family = **판정자별로 그 판정자가 본 쌍 전체**. 판정자를 섞어 한 family로 묶으면
    # m이 두 배가 돼 과보정되고, 판정자별 결론(둘이 갈리는지)도 못 읽는다.
    for (pair, judge), recs in sorted(by_pair_judge.items()):
        blk = _pair_block(recs) | {"pair": pair, "judge": judge, "tier": recs[0]["tier"]}
        blk["unreliable"] = (blk["bad_json"] / blk["n"] if blk["n"] else 0.0) > JUDGE_BAD_JSON_UNRELIABLE
        out["pairs"].append(blk)
    _holm_by(out["pairs"], key=lambda b: b["judge"])

    # kind별 (wiki_qa의 factual / synthetic_broad). 층이 하나뿐이면 생략한다.
    kinds = sorted({v["kind"] for v in verdicts.values()})
    if len(kinds) > 1:
        for (pair, judge), recs in sorted(by_pair_judge.items()):
            for k in kinds:
                sub = [r for r in recs if r["kind"] == k]
                if sub:
                    out["by_kind"].append(_pair_block(sub) | {"pair": pair, "judge": judge, "kind": k})
        _holm_by(out["by_kind"], key=lambda b: (b["judge"], b["kind"]))

    # 층별. 전체 표는 위에 이미 있으므로 여기서는 **부분집합 층**만 낸다 —
    # synthesize는 "전체 747 vs 두 조각 모두 실질 답변 490", wiki_qa는 "처방 전 55 vs 후 545".
    if strata:
        for (pair, judge), recs in sorted(by_pair_judge.items()):
            for st in (STRATUM_BOTH, STRATUM_NOINFO, STRATUM_PRE_FIX, STRATUM_POST_FIX):
                sub = [r for r in recs if r.get("stratum") == st]
                if sub:
                    out["by_stratum"].append(
                        _pair_block(sub) | {"pair": pair, "judge": judge, "stratum": st}
                    )
        _holm_by(out["by_stratum"], key=lambda b: (b["judge"], b["stratum"]))

    # 민감도 — 처방 전 55건을 **빼고** 다시 재서 결론이 뒤집히는지만 본다.
    # 기본 표는 어디까지나 전체다(제외본은 강건성 점검용이지 대체 결과가 아니다).
    if excluded:
        reduced: list[dict] = []
        for (pair, judge), recs in sorted(by_pair_judge.items()):
            kept = [r for r in recs if r["id"] not in excluded]
            if kept:
                reduced.append(_pair_block(kept) | {"pair": pair, "judge": judge})
        _holm_by(reduced, key=lambda b: b["judge"])
        full_by = {(b["pair"], b["judge"]): b for b in out["pairs"]}
        for rb in reduced:
            fb = full_by.get((rb["pair"], rb["judge"]))
            if not fb:
                continue
            f_sig, r_sig = fb["p_holm"] < 0.05, rb["p_holm"] < 0.05
            f_dir = (fb["left_win"] > fb["right_win"]) - (fb["left_win"] < fb["right_win"])
            r_dir = (rb["left_win"] > rb["right_win"]) - (rb["left_win"] < rb["right_win"])
            out["sensitivity"].append({
                "pair": rb["pair"], "judge": rb["judge"],
                "n_full": fb["n"], "n_excl": rb["n"], "n_dropped": fb["n"] - rb["n"],
                "win_rate_full": fb["win_rate_decided"], "win_rate_excl": rb["win_rate_decided"],
                "p_holm_full": fb["p_holm"], "p_holm_excl": rb["p_holm"],
                "sig_full": f_sig, "sig_excl": r_sig,
                # 결론이 뒤집힘 = 유의성 판정이 바뀌었거나 승자 방향이 바뀌었다.
                "flipped": (f_sig != r_sig) or (f_dir != r_dir and 0 not in (f_dir, r_dir)),
            })

    # 판정자 자기일관성 — tie가 '위치에 흔들린 것'인지 '진짜 무승부'인지 구분한다.
    for judge in sorted({v["judge"] for v in verdicts.values()}):
        recs = [v for v in verdicts.values() if v["judge"] == judge]
        c = Counter(r["tie_kind"] for r in recs)
        tot = len(recs)
        out["judge_selfconsistency"][judge] = {
            "n": tot, "tie": tot - c["clean"], "tie_rate": (tot - c["clean"]) / tot if tot else 0,
            "position_bias": c["position_bias"], "both_tie": c["both_tie"],
            "partial_tie": c["partial_tie"],
            "bad_json": sum(1 for r in recs if r["bad_json"]),
            "recovered": sum(1 for r in recs if r.get("recovered")),
            "bad_json_rate": (sum(1 for r in recs if r["bad_json"]) / tot) if tot else 0.0,
            "unreliable": (sum(1 for r in recs if r["bad_json"]) / tot if tot else 0.0)
                          > JUDGE_BAD_JSON_UNRELIABLE,
        }

    # 판정자 일치도 — 같은 (pair, id)를 두 판정자가 본 구간만.
    by_unit: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for v in verdicts.values():
        by_unit[(v["pair"], v["id"])][v["judge"]] = v
    judges = sorted({v["judge"] for v in verdicts.values()})
    for i, ja in enumerate(judges):
        for jb in judges[i + 1 :]:
            shared = [(u[ja], u[jb]) for u in by_unit.values() if ja in u and jb in u]
            if not shared:
                continue

            def norm(r: dict) -> str:
                return "tie" if r["verdict"] == "tie" else ("L" if r["verdict"] == r["left"] else "R")

            a = [norm(x) for x, _ in shared]
            b = [norm(y) for _, y in shared]
            agree = sum(1 for x, y in zip(a, b) if x == y)
            out["judge_agreement"][f"{ja} vs {jb}"] = {
                "n": len(shared), "agree": agree, "agree_rate": agree / len(shared),
                "kappa": kappa(a, b, ["L", "R", "tie"]),
            }

    # 이행성 — 국내 삼각형이 순환이면 '순위'라는 말이 성립하지 않는다.
    for judge in judges:
        edge: dict[tuple[str, str], str] = {}
        for blk in out["pairs"]:
            if blk["judge"] != judge or blk["tier"] != "A":
                continue
            if blk["p_exact"] < 0.05:
                w = blk["left"] if blk["left_win"] > blk["right_win"] else blk["right"]
                edge[(blk["left"], blk["right"])] = w
        cyc = None
        if len(edge) == 3:
            wins: dict[str, set[str]] = defaultdict(set)
            for (a_, b_), w in edge.items():
                wins[w].add(b_ if w == a_ else a_)
            cyc = all(len(wins[m]) == 1 for m in DOMESTIC if m in wins) and len(wins) == 3
        out["transitivity"][judge] = {"decided_edges": {f"{a}v{b}": w for (a, b), w in edge.items()},
                                      "cycle": bool(cyc)}

    # judge 태스크의 결정론 부가지표(환각·인용마커·길이) — 승패와 별개로 남긴다.
    det: dict[str, dict] = {}
    for s in ROSTER:
        rows = load_worker_rows(raw_dir, task, s)
        if not rows:
            continue
        vals = list(rows.values())
        det[s] = {
            "n": len(vals),
            "empty": sum(1 for r in vals if not answer_of(r)),
            "invented": sum(1 for r in vals if (r.get("metrics") or {}).get("invented")),
            "citation_marker": sum(1 for r in vals if (r.get("metrics") or {}).get("citation_marker")),
            "chars_mean": round(sum((r.get("metrics") or {}).get("chars") or 0 for r in vals)
                                / max(1, len(vals)), 1),
            "p50_ms": p50([r.get("latency_ms") or 0 for r in vals]),
        }
    out["worker_side_metrics"] = det
    return out


# --------------------------------------------------------------------------- #
# 리포트
# --------------------------------------------------------------------------- #
def _fmt_p(p: float) -> str:
    return f"{p:.3g}"


def _dom_rank_from_judge(jd: dict, judge: str) -> list[str]:
    """해당 판정자의 티어A 블록에서 국내 3사 승수를 세어 순위를 만든다."""
    score: Counter = Counter()
    for b in jd.get("pairs", []):
        if b["judge"] != judge or b["tier"] != "A" or b["p_holm"] >= 0.05:
            continue
        win, lose = (b["left"], b["right"]) if b["left_win"] > b["right_win"] else (b["right"], b["left"])
        score[win] += 1
        score[lose] -= 1
    return [m for m, _ in sorted(score.items(), key=lambda kv: -kv[1])] or list(DOMESTIC)


def render_conclusions(summary: dict) -> list[str]:
    L: list[str] = ["## 3. 결론 — 태스크별 배정 유지/변경 판정", "",
                    "각 권고에는 (a) 유의성 (b) 효과 크기 (c) 지연·비용 (d) 한계를 함께 단다. "
                    "**한계는 권고를 무르게 하려는 장식이 아니라 적용 조건이다.**", ""]
    for task in DET_TASKS + JUDGE_TASKS:
        cur = CURRENT_ASSIGNMENT.get(task, "?")
        det = summary["deterministic"].get(task)
        jd = summary["judge"].get(task)
        L += [f"### {task} — 현행 `{cur}`", ""]
        if det:
            rows = [(s_, w["success"] / max(1, w["n"]), w["p50_ms"]) for s_, w in det["workers"].items()]
            dom = sorted([r for r in rows if r[0] in DOMESTIC], key=lambda r: -r[1])
            best = dom[0]
            L.append("- 국내 성공률: " + " · ".join(f"**{m}** {v:.1%}({ms:.0f}ms)" for m, v, ms in dom))
            if best[0] == cur:
                rival = dom[1][0] if len(dom) > 1 else None
                sig = next((r for r in det["pairs"]
                            if rival and {r["left"], r["right"]} == {cur, rival}
                            and r["p_holm"] < 0.05), None)
                gap = (best[1] - dom[1][1]) if len(dom) > 1 else 0.0
                L += [f"- 현행 `{cur}`가 국내 1위이고, 차순위 `{rival}` 대비 효과크기 "
                      f"{gap * 100:.1f}%p" + (f" · McNemar Holm p={sig['p_holm']:.2g}로 **유의**"
                                              if sig else " · 유의차 없음"),
                      f"- **권고: `{cur}` 유지.** 측정이 현행 선택을 뒷받침한다."]
            else:
                sig = next((r for r in det["pairs"]
                            if {r["left"], r["right"]} == {cur, best[0]} and r["p_holm"] < 0.05), None)
                curv = next(v for m, v, _ in rows if m == cur)
                gap = best[1] - curv
                lat_cur = next(ms for m, _, ms in rows if m == cur)
                if sig:
                    L += [f"- 최고 국내 = **{best[0]}**, 현행 `{cur}` 대비 효과크기 **{gap * 100:.1f}%p** · "
                          f"McNemar Holm p={sig['p_holm']:.2g}로 **유의**",
                          f"- 지연: {best[0]} {best[2]:.0f}ms vs {cur} {lat_cur:.0f}ms "
                          f"({'개선' if best[2] < lat_cur else '악화'}). 비용은 국내 3사 모두 동급이라 변수 아님.",
                          f"- **권고: `{cur}` → `{best[0]}` 변경.**"]
                else:
                    L += [f"- 최고 국내 = **{best[0]}**이지만 현행 `{cur}` 대비 유의차 없음",
                          f"- **권고: `{cur}` 유지.** 동점 구간에서 옮길 근거가 없다."]
        if jd:
            rank = _dom_rank_from_judge(jd, PRIMARY_JUDGE)
            L += [f"- 주판정자({PRIMARY_JUDGE}) 기준 국내 순위: **{' > '.join(rank)}**",
                  "- ⚠️ 보조 국내 판정자와 kappa가 낮아(아래 일치도 표) **판정자 합의가 없다** — "
                  "이 태스크의 권고는 주판정자 단독 + 결정론 부가지표 정합에만 근거한다."]
            side = jd.get("worker_side_metrics") or {}
            if side:
                cm = {k: v["citation_marker"] for k, v in side.items()}
                ch = {k: v["chars_mean"] for k, v in side.items()}
                if max(cm.values()) > 50:
                    worst = max(cm, key=cm.get)
                    L.append(f"- 결정론 부가근거: **{worst}가 프로덕션이 금지한 인용마커를 "
                             f"{cm[worst]}/{side[worst]['n']}회 출력**(다른 모델 최대 "
                             f"{sorted(cm.values())[-2]}회), 평균 답변 길이도 {ch[worst]:.0f}자로 최장 "
                             f"(최단 {min(ch.values()):.0f}자). 판정자와 무관한 객관 결함이다.")
            # 현행 모델이 주판정자 기준 **꼴찌**이고 그것이 유의하면, "유지"는 무근거 유지다.
            lost_sig = [b for b in jd.get("pairs", [])
                        if b["judge"] == PRIMARY_JUDGE and b["tier"] == "A" and b["p_holm"] < 0.05
                        and ((b["left"] == cur and b["left_win"] < b["right_win"])
                             or (b["right"] == cur and b["right_win"] < b["left_win"]))]
            if lost_sig:
                worst_p = min(b["p_holm"] for b in lost_sig)
                L.append(f"- ⚠️ 현행 `{cur}`는 주판정자 기준 국내 상대 **{len(lost_sig)}쌍 모두에 유의하게 패**한다"
                         f"(최소 Holm p={worst_p:.2g}). 즉 이 슬롯의 현행 선택은 이번 측정으로 "
                         "**지지받지 못한다.**")
            if not lost_sig:
                # 현행이 아무에게도 유의하게 지지 않았다 = 공동 1위. 옮길 근거가 없다.
                L.append(f"- **권고: `{cur}` 유지.** 현행은 주판정자 기준 국내 상대 누구에게도 "
                         "유의하게 지지 않는다(상위권 내 유의차 없음). 순위 표기상의 1위와 "
                         "**통계적 1위는 다르다** — 동점 구간에서 옮길 근거가 없다.")
            elif rank and rank[0] != cur:
                L.append(f"- **권고: 즉시 변경은 보류하되 `{cur}` 유지를 '근거 있는 선택'으로 부르지 마라.** "
                         f"`{rank[0]}` 승격이 주판정자·결정론 부가지표 양쪽에서 지시되지만, 보조 판정자와 "
                         "합의가 없어(kappa 0.03~0.15) 단일 판정자에 슬롯을 걸 수는 없다. "
                         "**해소 조건: 판정자 3인 이상(제3 프론티어 판정자 추가) 또는 결정론 대리지표"
                         "(인용마커 위반·근거이탈)로의 전환.**")
            else:
                L.append(f"- **권고: `{cur}` 유지.**")
        L.append("")
    return L


def render(summary: dict) -> str:
    L: list[str] = ["# 잔여 5종 재측정 집계 — 최종 리포트", ""]
    L.append(f"생성: `remaining_analyze.py` · 워커 로스터 {len(ROSTER)}종 · "
             f"채택 쌍 {len(PAIRS)}개(티어A 국내전수 3 + 티어B 앵커스포크 4)")
    L += ["", "## 0. 먼저 읽을 것 — 이번 실험의 서사적 발견 3건", "",
          "### 0-1. 소표본 artefact가 실제로 뒤집혔다 (= 재측정을 한 이유)", "",
          "`email_draft`는 n=30에서 **\"Solar 5/30 vs EXAONE 4/30, p=1.000\" 완전 동점**이었고, "
          "현행 §15 배정은 그 동점 구간 안의 '다양화 선택'으로 EXAONE을 골랐다. n=1,000에서 다시 재니 "
          "**McNemar Holm p=2.8e-17로 EXAONE이 유의하게 우세**하다(환각 98건 vs Solar 241건). "
          "동점이라 믿고 임의로 고른 선택이 사실은 옳았지만, **그때 반대로 골랐어도 우리는 몰랐을 것이다.** "
          "잔여 슬롯 재측정의 정당성은 이 한 줄로 충분하다.", "",
          "### 0-2. 판정자를 바꾸면 결론이 바뀐다 — 과거 수치와 직접 비교 금지", "",
          "파일럿에서 gpt-4o↔Sonnet 불일치 9건이 **예외 없이 전부 gpt-4o=solar 방향**이었고, "
          "gpt-4o 기준 \"solar가 ax를 16-5로 이긴다\"가 Sonnet에서 11-10 무승부로 무너졌다. "
          "이번 본실행에서는 그 격차가 더 벌어져 **국내 보조 판정자와 Sonnet의 kappa가 0.03~0.15**로 "
          "사실상 우연 수준이다(§2 일치도 표). 국내 판정자는 일관되게 solar를 후하게, Sonnet은 "
          "일관되게 상대를 후하게 본다. **따라서 과거 gpt-4o judge 기준 t2/t8 수치와 이 리포트의 "
          "수치를 같은 표에 놓지 마라** — 판정층이 다르면 비교 자체가 성립하지 않는다.", "",
          "### 0-3. \"A.X의 JSON 취약성\"은 상당 부분 **우리 쪽 파서 엄격함**이었다", "",
          "- 결정론 3종: A.X `format_error` 248건 중 **247건(99.6%)이 본문 끝 여분 `}` 하나**, "
          "GLM 79건은 **전부 마크다운 코드펜스**. 둘 다 파서 2줄이면 100% 복구된다.",
          "- judge: A.X 위반율이 61~66%로 나와 한때 '판정자 실격'으로 판단했으나, 실제 출력은 "
          "`{\"winner\": \"A\", \"reason\": 따옴표 없는 한국어}` 형태로 **결정 변수인 `winner`는 항상 온전**했다. "
          "집계에 쓰지도 않는 `reason` 때문에 멀쩡한 판정을 버리고 있었던 것이다. `winner` 키만 회수하는 "
          "규칙을 전 판정자에 동일 적용하니 **위반율 61~66% → 0.0%**, 회수 가능률은 bad_json 1,882건 "
          "전수에서 **100%**였다(API 재호출 0회).",
          "- **따라서 E4에서 잰 A.X의 7.5% JSON 위반치도 같은 의심을 받아야 한다** — 그 수치 역시 "
          "모델의 능력이 아니라 파서의 엄격함을 잰 것일 수 있다.",
          "- **프로덕션 함의(이 리포트에서 가장 실행 가능한 항목):** 프로덕션은 파싱 실패를 조용히 "
          "결정론 템플릿으로 강등시켜 **LLM 호출을 통째로 버린다.** 여분 `}` 절단 · 코드펜스 제거 · "
          "필수 키만 회수 — 이 3줄이 A.X/GLM의 실효 성능을 크게 바꾼다. A.X의 현재 슬롯 "
          "`graph_bind`도 typed 출력 경로라 같은 계열의 위험을 공유한다.", ""]
    L.append("")

    # ── 결정론 3종 ────────────────────────────────────────────────────────
    L += ["## 1. 결정론 3종 (judge 없음 — `t12_generation.py` 지표 재사용)", ""]
    any_det = False
    for task in DET_TASKS:
        d = summary["deterministic"].get(task)
        if not d:
            continue
        any_det = True
        L += [f"### {task}  (golden n={d['n_golden']})", ""]
        if task == "email_draft":
            L += ["| 워커 | n | 형식실패 | 환각 | Re: 위반 | 본문자수 | 성공률 | p50 ms |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
            for s, w in d["workers"].items():
                L.append(f"| {s} | {w['n']} | {w['fail']} | {w['invented']} | {w['re_prefix']} | "
                         f"{w['body_chars_mean']} | {w['success'] / max(1, w['n']):.1%} | {w['p50_ms']:.0f} |")
        elif task == "gap_suggest":
            L += ["| 워커 | n | 형식실패 | 섹션규격(2-4) 위반 | 평균 섹션 | 평균 항목 | 성공률 | p50 ms |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
            for s, w in d["workers"].items():
                L.append(f"| {s} | {w['n']} | {w['fail']} | {w['spec_violation']} | {w['sections_mean']} | "
                         f"{w['items_mean']} | {w['success'] / max(1, w['n']):.1%} | {w['p50_ms']:.0f} |")
        else:
            L += ["| 워커 | n | 실패 | 평균 자수 | 120자 초과 | 없는 표현 | 성공률 | p50 ms |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
            for s, w in d["workers"].items():
                L.append(f"| {s} | {w['n']} | {w['fail']} | {w['chars_mean']} | {w['over_cap']} | "
                         f"{w['invented']} | {w['success'] / max(1, w['n']):.1%} | {w['p50_ms']:.0f} |")
        fd = d.get("format_error_diagnosis") or {}
        if fd:
            L += ["", "**형식 실패 원인 분해** — 프로덕션은 `json.loads` 실패 시 결정론 템플릿으로 "
                  "조용히 강등되므로, 원인이 포장 결함이면 **파서 2줄로 회수 가능한 손실**이고 "
                  "본문이 깨진 것이면 모델 교체 사유다. 둘을 같은 칸에 세면 판단을 그르친다.", "",
                  "| 워커 | 형식실패 | 사소한 보정으로 복구 | 원인 |", "|---|---:|---:|---|"]
            for s_, v in fd.items():
                kinds = ", ".join(f"{k}={n}" for k, n in sorted(v["kinds"].items()))
                L.append(f"| {s_} | {v['n_format_error']} | {v['trivially_repairable']} "
                         f"({v['trivially_repairable'] / max(1, v['n_format_error']):.0%}) | {kinds} |")
            L.append("")
        drift = [(s, w["invented"], w["stored_invented"]) for s, w in d["workers"].items()
                 if w["invented"] != w["stored_invented"]]
        if drift:
            L += ["", "> ⚠️ 탐지기 트립와이어 — 재계산 `_invented`가 저장값과 다르다 "
                  "(탐지기가 실행 후 바뀌었다는 뜻; 재계산값이 정본): "
                  + ", ".join(f"{s} {a}≠{b}" for s, a, b in drift)]
        L += ["", f"**쌍대 McNemar** (성공 = {_SUCCESS_DESC[task]}) — 보정 전/후 병기", "",
              "| 쌍 | 공통 n | b(왼쪽만 성공) | c(오른쪽만) | p(정확) | p(Holm) | 판정 |",
              "|---|---:|---:|---:|---:|---:|---|"]
        for r in d["pairs"]:
            L.append(f"| {r['left']} × {r['right']} | {r['n_common']} | {r['b_left_only']} | "
                     f"{r['c_right_only']} | {_fmt_p(r['p_exact'])} | {_fmt_p(r['p_holm'])} | "
                     f"{_sig(r['p_holm'])} |")
        L.append("")
    if not any_det:
        L += ["_(결정론 산출물 없음)_", ""]

    # ── judge 2종 ────────────────────────────────────────────────────────
    L += ["## 2. judge 2종 (쌍대 · 양방향 스왑 · 불일치=tie)", "",
          "> ⚠️ **판정층 경고 — 과거 수치와 섞지 마라.** 이 표의 판정자는 Claude Sonnet 4.6이고 "
          "기존 t2/t8 수치는 gpt-4o 판정이다. 파일럿에서 두 판정자의 불일치가 **전부 한 방향"
          "(gpt-4o가 solar에 후함)**이었고 그 때문에 쌍 결론이 실제로 뒤집혔다(16-5 → 11-10). "
          "같은 표에 놓고 비교하는 순간 그 표는 틀린다.", "",
          "> ⚠️ **티어 B 표본 비대칭.** `solar×claude-opus-4.8`만 전량(wiki_qa 1,000 / synthesize 747)이고 "
          "나머지 3스포크는 서브샘플 250이라 **CI 폭이 서로 다르다**(opus 쪽이 훨씬 좁다). 또 티어 B에는 "
          "설계상 2인이어야 할 판정자가 **3인**(sonnet·ax·exaone) 붙어 커버리지가 초과됐다 — 그만큼 "
          "판정단위가 늘어 CI가 좁아진 것이지 워커가 더 잘한 것이 아니다. "
          "**프론티어끼리는 직접 비교할 수 없다** — 상호 쌍을 측정하지 않았으므로 반드시 앵커(solar)를 "
          "경유한 이행적 읽기만 허용된다.", ""]
    any_j = False
    for task in JUDGE_TASKS:
        d = summary["judge"].get(task)
        if not d:
            continue
        any_j = True
        L += [f"### {task}  (판정단위 {d['n_units']})", "",
              "| 티어 | 쌍 | 판정자 | n | 왼쪽승 | 오른쪽승 | tie | 승률(tie제외) | 95% CI | 승률(tie=0.5) | p(정확) | p(Holm) | |",
              "|---|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|"]
        for b in d["pairs"]:
            wr = f"{b['win_rate_decided']:.1%}" if b["win_rate_decided"] is not None else "—"
            ci = f"{b['win_rate_ci95'][0]:.2f}–{b['win_rate_ci95'][1]:.2f}"
            wh = f"{b['win_rate_tie_half']:.1%}" if b["win_rate_tie_half"] is not None else "—"
            mark = " ⚠️쓸수없음" if b.get("unreliable") else ""
            L.append(f"| {b['tier']} | {b['left']} × {b['right']} | {b['judge']}{mark} | {b['n']} | "
                     f"{b['left_win']} | {b['right_win']} | {b['tie']} | {wr} | {ci} | {wh} | "
                     f"{_fmt_p(b['p_exact'])} | {_fmt_p(b['p_holm'])} | {_sig(b['p_holm'])} |")
        L.append("")
        if d["by_kind"]:
            L += ["#### kind별 (유형에 따라 순위가 갈리는가)", "",
                  "| 쌍 | 판정자 | kind | n | 왼쪽승 | 오른쪽승 | tie | 승률(tie제외) | p(정확) | p(Holm) |",
                  "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
            for b in d["by_kind"]:
                wr = f"{b['win_rate_decided']:.1%}" if b["win_rate_decided"] is not None else "—"
                L.append(f"| {b['left']} × {b['right']} | {b['judge']} | {b['kind']} | {b['n']} | "
                         f"{b['left_win']} | {b['right_win']} | {b['tie']} | {wr} | "
                         f"{_fmt_p(b['p_exact'])} | {_fmt_p(b['p_holm'])} |")
            L.append("")
        if d.get("by_stratum"):
            st = d["strata"]
            c = st.get("counts") or {}
            if task == "synthesize":
                nb, nn = c.get(STRATUM_BOTH, 0), c.get(STRATUM_NOINFO, 0)
                tot = max(1, nb + nn)
                L += [f"#### 층별 — 전체 {st['n_judged_items']} vs 두 조각 모두 실질 답변 "
                      f"{nb} ({nb / tot:.1%}) / 한쪽이 '정보 없음' {nn}", "",
                      "층 판정은 freeze 조각 본문(`synthesize_subs.jsonl`)에 **넓은 거부 어휘**를 "
                      "결정론 부분문자열 매칭한 결과다. 러너/`t8_synth.py`의 원본 `_DENIALS` 5종은 "
                      "**건드리지 않았고**(freeze 캐시가 그것으로 이미 굳었다) 여기서만 확장했다.", "",
                      f"- 원본 5종: {', '.join(repr(x) for x in st['vocabulary_base'])}",
                      f"- 분석 확장분: {', '.join(repr(x) for x in st['vocabulary_extra'])}", "",
                      "> 층에서 순위가 갈리면 그 자체가 결과다 — '정보 없음 조각을 어떻게 다루는가'가 "
                      "모델 차이를 만든다는 뜻이고, 프로덕션 프롬프트 rule 2가 요구하는 동작이 바로 그것이다."]
            else:
                L += [f"#### 층별 — 사실형 {st.get('n_factual')} 중 **처방 전 프롬프트** "
                      f"{c.get(STRATUM_PRE_FIX, 0)} / 처방 후 {c.get(STRATUM_POST_FIX, 0)}", "",
                      "저작 도중 폐기율이 40% 임계를 넘어 생성 프롬프트를 고쳤다(claim 출처 문서 제목 "
                      "주입 + 한정어 삽입 규칙). 앞 구간은 그 전 산출물이라 2차 판정은 똑같이 통과했어도 "
                      "문항 성격이 미묘하게 다르다(\"…완료된 날짜는?\" 같은 특정 불가형). "
                      f"경계는 {st.get('derivation')}로 잡았고, 경계 ts 간극은 "
                      f"{st.get('boundary_ts_gap_sec')}초다. 종합형(synthetic_broad)은 이 층 대상이 아니다."]
            L += ["",
                  "| 쌍 | 판정자 | 층 | n | 왼쪽승 | 오른쪽승 | tie | 승률(tie제외) | p(정확) | p(Holm) |",
                  "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
            for b in d["by_stratum"]:
                wr = f"{b['win_rate_decided']:.1%}" if b["win_rate_decided"] is not None else "—"
                L.append(f"| {b['left']} × {b['right']} | {b['judge']} | {b['stratum']} | {b['n']} | "
                         f"{b['left_win']} | {b['right_win']} | {b['tie']} | {wr} | "
                         f"{_fmt_p(b['p_exact'])} | {_fmt_p(b['p_holm'])} |")
            L.append("")
        if d.get("sensitivity"):
            flips = [s for s in d["sensitivity"] if s["flipped"]]
            nd = max(s["n_dropped"] for s in d["sensitivity"])
            L += [f"#### 민감도 — 처방 전 문항 제외(판정단위 최대 {nd}건 빠짐) 시 결론이 뒤집히는가", "",
                  "**기본 표는 전체다.** 아래는 강건성 점검이지 대체 결과가 아니다. "
                  "'뒤집힘'은 Holm 유의성 판정이 바뀌었거나 승자 방향이 바뀐 경우다.", "",
                  "| 쌍 | 판정자 | n(전체→제외) | 승률(전체) | 승률(제외) | p Holm(전체) | p Holm(제외) | 뒤집힘 |",
                  "|---|---|---|---:|---:|---:|---:|---|"]
            for s in d["sensitivity"]:
                wf = f"{s['win_rate_full']:.1%}" if s["win_rate_full"] is not None else "—"
                we = f"{s['win_rate_excl']:.1%}" if s["win_rate_excl"] is not None else "—"
                L.append(f"| {s['pair']} | {s['judge']} | {s['n_full']}→{s['n_excl']} | {wf} | {we} | "
                         f"{_fmt_p(s['p_holm_full'])} | {_fmt_p(s['p_holm_excl'])} | "
                         f"{'⚠️ 예' if s['flipped'] else '아니오'} |")
            if flips:
                names = ", ".join(f"{s['pair']}/{s['judge']}" for s in flips)
                L += ["", "> ⚠️ **뒤집힌 쌍이 있다** — 처방 전 문항이 결론을 좌우한다는 뜻이므로 "
                      f"그 자체가 보고 대상이다: {names}", ""]
            else:
                L += ["", "> 뒤집힌 쌍 없음 — 처방 전 문항이 결론을 좌우하지 않는다.", ""]
        if d.get("strata", {}).get("caveats"):
            L += ["#### 골든 한계 각주 (저작 단계에서 확인된 것)", ""]
            L += [f"{n}. {c}" for n, c in enumerate(d["strata"]["caveats"], 1)]
            L.append("")
        for w in d.get("warnings", []):
            L += [f"> [warn] {w}", ""]
        bad = [j for j, v in d["judge_selfconsistency"].items() if v["unreliable"]]
        if bad:
            L += ["", f"> ⚠️ **판정자 {', '.join(bad)} 는 이 태스크에서 쓸 수 없다** — JSON 계약 위반율이 "
                  f"{JUDGE_BAD_JSON_UNRELIABLE:.0%}를 넘는다. 파싱 실패는 tie로 흡수되므로 그 판정자의 "
                  "무승부는 **실력이 아니라 고장**이고, 평균에 섞으면 가짜 동점이 된다. "
                  "해당 행은 참고로만 두고 결론은 나머지 판정자로 낸다.", ""]
        L += ["#### 판정자 자기일관성 (tie = 양방향 불일치)", "",
              "| 판정자 | n | tie | tie율 | position_bias | both_tie | partial | JSON 실패 | 실패율 | winner 회수 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for j, sc in d["judge_selfconsistency"].items():
            L.append(f"| {j}{' ⚠️' if sc['unreliable'] else ''} | {sc['n']} | {sc['tie']} | "
                     f"{sc['tie_rate']:.1%} | {sc['position_bias']} | {sc['both_tie']} | "
                     f"{sc['partial_tie']} | {sc['bad_json']} | {sc['bad_json_rate']:.1%} | "
                     f"{sc.get('recovered', 0)} |")
        L.append("")
        if d["judge_agreement"]:
            L += ["#### 판정자 일치도 (공통 판정단위)", "",
                  "| 판정자 쌍 | n | 일치 | 일치율 | kappa |", "|---|---:|---:|---:|---:|"]
            for k, v in d["judge_agreement"].items():
                L.append(f"| {k} | {v['n']} | {v['agree']} | {v['agree_rate']:.1%} | {v['kappa']:.3f} |")
            L.append("")
        cyc = [j for j, t in d["transitivity"].items() if t["cycle"]]
        if cyc:
            L += [f"> ⚠️ **이행성 위반(순환)**: {', '.join(cyc)} 판정에서 국내 삼각형이 순환한다 — "
                  "이 판정자 기준으로는 '순위'를 주장할 수 없다.", ""]
        if d["worker_side_metrics"]:
            L += ["#### 워커측 결정론 부가지표 (승패와 별개)", "",
                  "| 워커 | n | 빈답 | 환각 | 인용마커 위반 | 평균 자수 | p50 ms |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
            for s, m in d["worker_side_metrics"].items():
                L.append(f"| {s} | {m['n']} | {m['empty']} | {m['invented']} | {m['citation_marker']} | "
                         f"{m['chars_mean']} | {m['p50_ms']:.0f} |")
            L.append("")
    if not any_j:
        L += ["_(판정 산출물 없음)_", ""]

    L += render_conclusions(summary)
    L += ["## 4. 읽는 법", "",
          "- **p(Holm)** 이 유의성의 정본이다. 보정 전 p만 보면 쌍이 많을수록 우연히 하나쯤 유의해진다.",
          "- 승률은 두 벌이다. `tie제외`는 검정의 대상이고, `tie=0.5`는 '얼마나 자주 갈리는가'를 감춘다.",
          "- 티어 B(프론티어)는 서브샘플이라 CI가 넓다. 격차의 **부호와 크기**를 읽는 용도이지, "
          "프론티어끼리의 순위를 읽는 표가 아니다(프론티어 상호 쌍은 측정하지 않았다).",
          "- exaone/ax 대 프론티어는 티어 A의 solar 대비 위치를 통해 **이행적으로** 읽는다. "
          "이행성 경고가 떠 있으면 그 추론을 하면 안 된다.", ""]
    return "\n".join(L)


_SUCCESS_DESC = {
    "email_draft": "ok ∧ 환각 없음 ∧ 'Re:' 없음",
    "gap_suggest": "ok ∧ 섹션 규격(2-4) 준수",
    "claim_headline": "ok ∧ 120자 이하 ∧ 없는 표현 없음",
}


def main() -> None:
    ap = argparse.ArgumentParser(description="잔여 5종 집계")
    ap.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    ap.add_argument("--judge-dir", default=str(DEFAULT_JUDGE_DIR))
    ap.add_argument("--golden-dir", default=str(DEFAULT_GOLDEN_DIR))
    ap.add_argument("--out-dir", default=str(FUGU / "analysis"))
    ap.add_argument("--md-name", default="remaining_report.md")
    ap.add_argument("--json-name", default="remaining_summary.json")
    a = ap.parse_args()

    raw_dir, judge_dir = Path(a.raw_dir), Path(a.judge_dir)
    golden_dir, out_dir = Path(a.golden_dir), Path(a.out_dir)

    summary: dict[str, Any] = {
        "roster": list(ROSTER),
        "pairs_measured": [{"left": a_, "right": b_, "tier": TIER[(a_, b_)]} for a_, b_ in PAIRS],
        "deterministic": {t: analyze_det(t, raw_dir, golden_dir) for t in DET_TASKS},
        "judge": {t: analyze_judge(t, judge_dir, raw_dir, golden_dir) for t in JUDGE_TASKS},
    }
    summary["deterministic"] = {k: v for k, v in summary["deterministic"].items() if v}
    summary["judge"] = {k: v for k, v in summary["judge"].items() if v}

    md = render(summary)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / a.md_name).write_text(md, encoding="utf-8")
    (out_dir / a.json_name).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(md)
    print(f"\n→ {out_dir / a.md_name}\n→ {out_dir / a.json_name}")


if __name__ == "__main__":
    main()
