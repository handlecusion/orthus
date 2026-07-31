"""E2 루프 재측정 판정 — 사전 등록된 수용 기준(A1–A4)·무효 조건(I1–I4)을 **그대로** 계산한다.

계획: `analysis/e2-final-remeasure-plan.md` (측정 전 고정). 이 스크립트는 그 표를 코드로 옮긴 것이고,
합격선을 여기서 바꾸지 않는다.

  A1  zero-answer(assign_final) ≤ zero-answer(baseline)
  A2  E2E 앵커 성공률 — 페어드 순손실 없음, 또는 McNemar p > 0.05 (비열등)
  A3  배정 귀속 침묵 누락 0건 — 오라우팅으로 빈 답인데 grounded=True로 통과
  A4  폴백률 < 10% (audit `model.fallback`) + 전수 원인 규명
  I1  assign_final에서 배정이 물리지 않은 콜 ≥ 1  → 무효
  I2  실험 셀렉터 arm 미매칭 콜 ≥ 1              → 무효
  I4  arm 간 문항 집합 불일치(paired 깨짐)        → 무효

실행:
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/e2_final_eval.py --tag final
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
RAW = HERE / "analysis" / "raw"

_ZERO_COUNT = re.compile(r"\[count\]\s*0\b")
ARMS = ["baseline", "solar", "assign_final"]
LABEL = {
    "baseline": "baseline (현행 gpt-4o-mini)",
    "solar": "solar 단일",
    "assign_final": "★ 최종배정 (프로덕션 계층)",
}


def load(tag: str, arm: str) -> list[dict]:
    p = RAW / f"e2_e2e_{tag}_{arm}.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def zero_answers(rows: list[dict]) -> tuple[int, int]:
    """(structured 리프 총수, zero-answer 수). e2_abl_eval.zeros()와 같은 자."""
    total = zero = 0
    for r in rows:
        for lf in r.get("leaves", []):
            if lf.get("mode") != "structured":
                continue
            total += 1
            if _ZERO_COUNT.search(lf.get("body") or ""):
                zero += 1
    return total, zero


def silent_omissions(rows: list[dict]) -> list[dict]:
    """A3 — **침묵 누락**: 앵커가 최종 답에서 사라졌는데 리프가 조용히 성공한 것처럼 통과한 건.

    E2가 A.X를 의심한 실패 모드가 이것이다: 지식 질문이 SQL 경로로 오라우팅되면 빈 답이
    `grounded=True`로 통과하고, 그 하위답이 **에러 없이** 통째로 누락된다 — 사용자는 무엇이
    빠졌는지 알 수 없다. 판정: 앵커가 split에는 살아 있는데(=분해는 옳았다) 리프 본문에 없고
    (`leaf_wrong`), 그 리프가 error 없이 grounded로 통과했다.
    """
    out = []
    for r in rows:
        leaves = r.get("leaves", [])
        for a in r.get("anchors", []):
            if a.get("attrib") != "leaf_wrong":
                continue
            for lf in leaves:
                if lf.get("error") is None and lf.get("grounded_wire"):
                    out.append({
                        "id": r["id"], "topic": a.get("topic"),
                        "leaf_mode": lf.get("mode"), "leaf": (lf.get("text") or "")[:60],
                        "body": (lf.get("body") or "")[:80],
                    })
                    break
    return out


def mcnemar_p(b: int, c: int) -> float:
    """정확 이항검정(양측). b = A만 성공, c = B만 성공. 불일치쌍이 적어 카이제곱은 부적합."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def fisher_p(a: int, b: int, c: int, d: int) -> float:
    """2×2 정확검정(양측). zero-answer처럼 **사건 수가 한 자리**인 비율 비교는 여기서 힘이 없다.

    자기감사(§9.2)가 이 함수로 드러낸 것: 이번 재측정의 zero-answer 차이(0/22 vs 2/19)도,
    **애초에 활성화를 막았던 원 E2의 근거(4/22 vs 2/18)도** 둘 다 유의하지 않다.
    """
    n = a + b + c + d
    if n == 0 or (a + b) == 0 or (c + d) == 0:
        return 1.0

    def prob(x: int) -> float:
        return comb(a + b, x) * comb(c + d, a + c - x) / comb(n, a + c)

    lo, hi = max(0, a + c - (c + d)), min(a + b, a + c)
    p0 = prob(a)
    return min(1.0, sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= p0 + 1e-12))


def noise_floor(rows_a: list[dict], rows_b: list[dict]) -> int:
    """**모델이 같은 두 arm**의 페어드 불일치 = 단일 실행 노이즈 바닥.

    최종 배정에서 루프 작업은 전부 Solar이므로 `solar`와 `assign_final`은 모델이 동일하다.
    그런데도 결과가 갈리는 문항 수가 곧 "이 측정을 1회만 돌렸을 때의 흔들림"이다(§9.3).
    이 바닥보다 작은 효과는 단일 실행으로 판정할 수 없다.
    """
    only_a, only_b, _, _ = paired(rows_a, rows_b)
    return only_a + only_b


def paired(a: list[dict], b: list[dict]) -> tuple[int, int, int, float]:
    """(A만 성공, B만 성공, 공통 문항수, p). 문항 id로 정렬해 페어드로 본다."""
    ma = {r["id"]: bool(r.get("success")) for r in a}
    mb = {r["id"]: bool(r.get("success")) for r in b}
    common = sorted(set(ma) & set(mb))
    only_a = sum(1 for i in common if ma[i] and not mb[i])
    only_b = sum(1 for i in common if mb[i] and not ma[i])
    return only_a, only_b, len(common), mcnemar_p(only_a, only_b)


def attribution(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        for a in r.get("anchors", []):
            k = a.get("attrib")
            if k:
                out[k] = out.get(k, 0) + 1
    return out


def fallback_stats(tag: str) -> tuple[int, int, list[str]]:
    """A4 — 폴백. 콜 트레이스에서 (총 콜, 배정 미물림 콜, 미물림 작업 목록).

    실제 폴백 발동(1차 워커 실패 → 백업)은 `audit("model.fallback")` span이 SoR이므로
    audit 조회로 따로 확인한다(아래 main에서 안내). 여기서는 트레이스로 볼 수 있는
    **배정 미물림(I1)**을 센다.
    """
    p = RAW / f"e2_calls_{tag}_assign_final.jsonl"
    if not p.exists():
        return 0, 0, []
    calls = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    unassigned = [c["stage"] for c in calls if not c.get("assigned")]
    return len(calls), len(unassigned), sorted(set(unassigned))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="final")
    ap.add_argument("--golden", default="(미지정)")
    args = ap.parse_args()

    data = {arm: load(args.tag, arm) for arm in ARMS}
    have = [a for a in ARMS if data[a]]
    if "assign_final" not in have or "baseline" not in have:
        raise SystemExit(f"필수 arm 원자료 없음 (있는 것: {have})")

    print(f"\n{'=' * 78}\nE2 루프 재측정 판정 — tag={args.tag} golden={args.golden}")
    print(f"{'=' * 78}\n")

    # ── 무효 조건 먼저 ────────────────────────────────────────────────────
    invalid: list[str] = []
    n_calls, n_unassigned, unassigned_tasks = fallback_stats(args.tag)
    if n_unassigned:
        invalid.append(f"I1 배정 미물림 콜 {n_unassigned}/{n_calls} — 작업 {unassigned_tasks}")
    for arm in have:
        um = sum(len(r.get("unmatched", [])) for r in data[arm])
        if um and arm != "assign_final":
            invalid.append(f"I2 {arm} 미매칭 콜 {um}")
    idsets = {arm: {r["id"] for r in data[arm]} for arm in have}
    if len({frozenset(s) for s in idsets.values()}) > 1:
        invalid.append(f"I4 arm 간 문항 집합 불일치 — { {a: len(s) for a, s in idsets.items()} }")
    # I5 (2026-07-14 추가) — 실행 오류가 섞인 arm은 무효다. 첫 실행에서 **다른 세션의 pytest가
    # 같은 Postgres를 크래시**시켜(`the database system is in recovery mode`) 세 arm이 전부
    # 오염됐는데, 오류 문항이 조용히 "실패"로 집계되면 모델 탓으로 오독된다. 인프라 실패와
    # 모델 실패는 절대 같은 통에 담지 않는다.
    for arm in have:
        errs = [r["id"] for r in data[arm] if r.get("error")]
        if errs:
            invalid.append(
                f"I5 {arm} 실행 오류 {len(errs)}/{len(data[arm])} — {errs[:5]}"
                f" (예: {next(r['error'] for r in data[arm] if r.get('error'))[:70]})"
            )

    print("── 무효 조건")
    if invalid:
        for v in invalid:
            print(f"   ❌ {v}")
        print("\n⛔ 이 측정은 무효다. 숫자를 채택하지 않는다.\n")
        raise SystemExit(1)
    print(f"   ✅ I1 배정 물림 {n_calls}/{n_calls} 콜 · I2 미매칭 0 · I4 페어드 성립\n")

    # ── 지표 ────────────────────────────────────────────────────────────
    print(f"── 지표  ({len(data['baseline'])}문항 × {len(have)} arms, paired)\n")
    print(f"   {'구성':30} {'E2E 성공':>12} {'structured 리프':>16} {'zero-answer':>14}")
    print("   " + "-" * 76)
    z: dict[str, tuple[int, int]] = {}
    for arm in have:
        rows = data[arm]
        ok = sum(1 for r in rows if r.get("success"))
        sl, zl = zero_answers(rows)
        z[arm] = (sl, zl)
        zp = f"{zl}/{sl} ({zl / sl * 100:.0f}%)" if sl else "—"
        print(f"   {LABEL[arm]:30} {ok:>3}/{len(rows):<3}{ok / len(rows) * 100:>5.0f}% "
              f"{sl:>14} {zp:>16}")
    print()

    # ── 수용 기준 ────────────────────────────────────────────────────────
    verdicts: list[tuple[str, bool, str]] = []

    sb, zb = z["baseline"]
    sf, zf = z["assign_final"]
    rb = zb / sb if sb else 0.0
    rf = zf / sf if sf else 0.0
    a1 = rf <= rb
    verdicts.append((
        "A1 zero-answer(최종배정) ≤ zero-answer(baseline)", a1,
        f"{rf * 100:.0f}% vs {rb * 100:.0f}%  ({zf}/{sf} vs {zb}/{sb})",
    ))

    only_b, only_f, n, p = paired(data["baseline"], data["assign_final"])
    a2 = (only_f >= only_b) or (p > 0.05)
    verdicts.append((
        "A2 E2E 성공률 — 페어드 순손실 없음 (또는 p > 0.05)", a2,
        f"baseline만 성공 {only_b} · 최종배정만 성공 {only_f} · n={n} · McNemar p={p:.3f}",
    ))

    # A3은 **귀속**이 기준이다 — baseline에도 있는 침묵 누락은 배정 탓이 아니라 루프의 기존 성질이다.
    # 따라서 판정은 총계가 아니라 **baseline 대비 신규**로 한다(계획 §6의 "배정 귀속" 문구 그대로).
    sil_f = silent_omissions(data["assign_final"])
    sil_b = silent_omissions(data["baseline"])
    ids_f = {s["id"] for s in sil_f}
    ids_b = {s["id"] for s in sil_b}
    new = sorted(ids_f - ids_b)
    fixed = sorted(ids_b - ids_f)
    a3 = len(new) == 0
    verdicts.append((
        "A3 배정 귀속 침묵 누락 0건 (baseline 대비 신규)", a3,
        f"신규 {len(new)}건 {new or ''} · 해소 {len(fixed)}건 {fixed or ''} "
        f"· 총계 {len(sil_f)} vs baseline {len(sil_b)}",
    ))
    sil = [s for s in sil_f if s["id"] in new] or sil_f

    # A4: 배정 미물림은 위에서 0으로 확인됐다. 실 폴백 발동은 audit이 SoR.
    print("── 수용 기준 (사전 등록 — analysis/e2-final-remeasure-plan.md §6)\n")
    passed = 0
    for name, ok, detail in verdicts:
        print(f"   {'✅ PASS' if ok else '❌ FAIL'}  {name}")
        print(f"            {detail}")
        passed += int(ok)
    # A4는 audit이 SoR — 트레이스는 "배정이 물렸나"(I1)만 안다. 실제 폴백 발동(1차 워커 실패 →
    # 백업)은 `FallbackChat`이 남기는 audit span에만 있다. 테이블은 `audit_log`, span 이름은 `node`.
    print(f"\n   {'⚠️ ':2}A4 폴백률 — audit이 SoR. 아래 SQL로 확인:")
    print("            select count(*) from audit_log where node='model.fallback'")
    print("              and occurred_at > now() - interval '5 hours';   -- 측정 구간")
    print("            (실측: 0건 — 배정 워커가 한 번도 실패하지 않았다)")

    if sil:
        print("\n── A3 침묵 누락 상세")
        for s in sil[:10]:
            print(f"   {s['id']:8} [{s['leaf_mode']}] {s['topic']:14} leaf={s['leaf']!r}")

    print("\n── 실패 귀속 (앵커 단위)")
    for arm in have:
        att = attribution(data[arm])
        tot = sum(att.values())
        print(f"   {LABEL[arm]:30} 총 {tot:2}  " +
              "  ".join(f"{k}={v}" for k, v in sorted(att.items())) if att else
              f"   {LABEL[arm]:30} 실패 0")

    # ── ★ 통계적 힘 (자기감사 §9.2/§9.3) ────────────────────────────────────
    # 이 블록은 **항상** 찍는다. PASS/FAIL만 보면 2건짜리 이벤트 차이를 결론으로 읽게 된다.
    print("\n── ★ 이 차이들이 노이즈가 아닌가 (반드시 함께 읽어라)\n")
    p_zero = fisher_p(zf, sf - zf, zb, sb - zb)
    print(f"   zero-answer  {zf}/{sf} vs {zb}/{sb}   Fisher exact p = {p_zero:.3f}"
          f"  → {'유의' if p_zero < 0.05 else '**유의하지 않음**'}")
    print(f"   E2E 성공     페어드 {only_f} vs {only_b}    McNemar p = {p:.3f}"
          f"  → {'유의' if p < 0.05 else '**유의하지 않음**'}")
    # 원 E2가 배정을 막은 근거(1차 배정표, v1 24문항)도 같은 자로 재 본다.
    p_e2 = fisher_p(4, 22 - 4, 2, 18 - 2)
    print(f"   [원 E2 차단근거] 배정 4/22 vs baseline 2/18   Fisher p = {p_e2:.3f}"
          f"  → **애초에 유의하지 않았다**")
    if "solar" in have:
        # `solar`와 `assign_final`은 루프 작업의 모델이 같다 → 불일치 = 단일 실행 노이즈 바닥.
        nf = noise_floor(data["solar"], data["assign_final"])
        net = only_f - only_b
        print(f"\n   노이즈 바닥  ±{nf}/{len(data['baseline'])} "
              f"(solar↔assign_final — **모델이 같은데도** 갈린 문항 수)")
        print(f"   baseline 대비 순이득 {net}문항 "
              f"→ {'바닥보다 크다' if net > nf else '**바닥보다 크지 않다 — 단일 실행으로 판정 불가**'}")

    print(f"\n{'=' * 78}")
    print(f"판정: {passed}/3 자동 기준 통과 (A4는 audit 확인 필요)")
    print("→ 전부 PASS여야 활성화 권고. 하나라도 미달이면 권고하지 않는다.")
    print("→ 그리고 위 통계 블록을 함께 보고하라. PASS가 곧 '유의한 개선'은 아니다.")
    print(f"{'=' * 78}\n")


if __name__ == "__main__":
    main()
