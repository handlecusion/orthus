"""E6 채택규칙 비교 — 이미 모은 원자료로 후보 규칙 오프라인 재생 (신규 LLM 콜 0).

현재 규칙(query.py:411): 트리거 4종(gate_fail/not_executed/empty_rows/zero_answer) 중
하나라도 뜨면 2차 호출 → **2차가 트리거 안 뜨면 채택**.

문제(실측 §3.3): 오발동 2건 모두 `zero_answer` 트리거(1차가 확신에 찬 0 = TN에선 정답)에서
2차가 다른 해석의 비-0을 내 채택됨. 회수는 대부분 gate_fail/empty_rows.

후보 규칙 = "어떤 트리거에서 2차 채택을 허용할지" 부분집합 S:
  R1(현행): S={gate_fail,not_executed,empty_rows,zero_answer}
  R2:        S={gate_fail,not_executed}                     (empty/zero 채택 안 함)
  R3:        S={gate_fail,not_executed,empty_rows}          (zero_answer만 채택 안 함) ★유력
  R4:        S={gate_fail,not_executed,empty_rows,zero_answer} 단 zero_answer는
             1차가 count계열(단일 0셀)일 때만 제외 — 여기선 R3와 동일 취급(단순화)

재생 논리(부분집합 S ⊆ 현행 4종이므로 새 채택은 안 생기고, 기존 채택만 철회될 수 있음):
  현행 채택(adopted=True, t1∈4, t2=None)이던 문항에서 t1∉S면 → 2차 미채택, 1차 유지.
    - gold>0: 1차는 트리거났으므로 오답 → 유지 시 오답. (그 문항이 회수였으면 회수 상실.)
    - TN(gold=0): 1차 트리거(zero/empty)=진짜-0 정답 → 유지 시 안전(오발동 방지).
  t1∈S면 현행과 동일.

실행: PYTHONPATH=$PWD python experiments/fugu-ko/e6_rules.py
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "analysis" / "raw"

RULES = {
    "R1 현행(4종 전부)": {"gate_fail", "not_executed", "empty_rows", "zero_answer"},
    "R2 hard만(gate/not_exec)": {"gate_fail", "not_executed"},
    "R3 zero_answer 제외": {"gate_fail", "not_executed", "empty_rows"},
}


def load(tag: str) -> list[dict]:
    f = RAW / f"e6_cascade_{tag}.jsonl"
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()] if f.exists() else []


def eval_rule_t3(rows: list[dict], S: set[str]) -> tuple[int, int, list[str]]:
    """반환 (최종정답수, 회수수, 회수 잃은 문항). gold>0."""
    n = len(rows)
    final_ok = 0
    recovered = []
    for r in rows:
        if not r["cascade_fired"]:
            final_ok += bool(r["final_correct"])  # 트리거 안 뜸 = 1차 그대로
            continue
        t1, t2 = r["trigger"], r["fallback_trigger"]
        cur_adopt = r["adopted"]
        new_adopt = (t1 in S) and (t2 is None)
        if new_adopt == cur_adopt:
            ok = bool(r["final_correct"])  # 현행과 동일
        elif cur_adopt and not new_adopt:
            ok = False  # 2차 미채택 → 1차 유지, gold>0에서 트리거났으니 오답
        else:
            ok = bool(r["final_correct"])
        final_ok += ok
        # 회수 = 1차 트리거 오답인데 최종 정답
        if ok and r["trigger"] is not None:
            recovered.append(r["id"])
    return final_ok, len(recovered), recovered


def eval_rule_tn(rows: list[dict], S: set[str]) -> tuple[int, list[str]]:
    """반환 (오발동수, 오발동 문항). gold=0(진짜-0)."""
    false_adopt = []
    for r in rows:
        if not r["cascade_fired"]:
            continue
        t1, t2 = r["trigger"], r["fallback_trigger"]
        cur_adopt = r["adopted"]
        new_adopt = (t1 in S) and (t2 is None)
        # 현행에서 오발동(채택했고 최종 비-0)이던 문항이 새 규칙에서도 채택되면 여전히 오발동
        if r["final_zero"] is False and cur_adopt:  # 현행 오발동
            if new_adopt:
                false_adopt.append(r["id"])  # 여전히 채택 → 오발동 유지
            # else: 미채택 → 1차(진짜-0) 유지 → 방지됨
    return len(false_adopt), false_adopt


def main() -> None:
    # t3+trap 합산(회수), TN 합산(오발동). arm A(gpt) 기준 — 캐스케이드 효과 가장 큼.
    t3 = load("A") + load("A_trap")
    tn = load("A_tn")
    n_t3 = len(t3)
    print(f"arm A 기준  t3+trap {n_t3}문항, TN {len(tn)}문항\n")
    print(f"  {'규칙':28} {'최종정답(t3+trap)':>18} {'회수':>6} {'TN오발동':>9}")
    for name, S in RULES.items():
        ok, rec, recl = eval_rule_t3(t3, S)
        fa, fal = eval_rule_tn(tn, S)
        print(f"  {name:28} {ok:>4}/{n_t3} = {ok/n_t3*100:5.1f}%   {rec:>4}   {fa:>6}   {fal if fal else ''}")
    print("\n  (부분집합 규칙이라 새 채택은 안 생김 — 회수는 유지/감소, 오발동은 유지/감소만 가능)")
    # 회수 손실 상세
    print("\n  [규칙별 회수 문항]")
    for name, S in RULES.items():
        _, _, recl = eval_rule_t3(t3, S)
        print(f"    {name:28} {sorted(recl)}")


if __name__ == "__main__":
    main()
