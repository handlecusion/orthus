"""G3 최종 평가 — (a)단일 / (b)static / (c)학습 선택기 / oracle 4자 대조.

평가 무대(계획 §4):
  1) **골든 holdout(미학습)** — 선택기는 합성 730문항으로만 학습, 골든 79문항은 dedupe로 절대 미학습.
     DB질의 18(실DB 채점) + 라우팅 21(route 일치). → `train/data/golden_labeled.jsonl` (g3_gold.py).
  2) 합성 test 불일치 부분집합 — H3 주판정(이미 tier2/tier3에서 산출).
  지식응답(qa) 30은 선택기 학습 대상이 아니므로(계획 §1) static(=solar) 고정으로만 참고 보고.

선택기는 tier2(KLUE-RoBERTa) 또는 tier3(EXAONE LoRA)를 **학습 프로토콜 그대로 재학습**한 뒤
골든에 추론한다(체크포인트 미저장 설계라 재학습이 곧 재현). seed 42 고정.

실행:
  export HF_HOME=/data/tta/hf-cache
  CUDA_VISIBLE_DEVICES=0 python train/g3_eval.py --tier 2
  CUDA_VISIBLE_DEVICES=0 python train/g3_eval.py --tier 3 --model LGAI-EXAONE/EXAONE-3.5-2.4B-Instruct --revision e949c91dec92
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import torch
from torch import nn
from torch.utils.data import DataLoader

import tier2 as T2
import tier3 as T3

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
WORKERS = T2.WORKERS
STATIC = T2.STATIC
SEED = T2.SEED


def load_golden() -> list[dict]:
    return [json.loads(l) for l in open(DATA / "golden_labeled.jsonl", encoding="utf-8")]


def pick_margin(probs: dict[str, float], task: str, tau: float) -> str:
    """margin 게이트: static 배정을 기본값으로 두고, 다른 워커가 **tau 이상** 더
    확신될 때만 이탈. tau=0이면 기존 argmax(pick)과 동일. tau는 val로만 튜닝."""
    static = STATIC[task]
    best = max(WORKERS, key=lambda m: probs[m])
    if best == static:
        return static
    return best if probs[best] - probs[static] >= tau else static


def tune_tau(va: list[dict], vp: dict, grid=None) -> float:
    """val 정확도 최대화 tau(동률이면 더 보수적인=큰 tau). test/골든 미열람."""
    grid = grid or [0.0, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
    best_tau, best_acc = 0.0, -1.0
    for tau in grid:
        acc = T2.routed_acc(va, lambda r: pick_margin(vp[r["id"]], r["task"], tau))
        if acc > best_acc or (acc == best_acc and tau > best_tau):
            best_tau, best_acc = tau, acc
    return best_tau


def routed_score(rows: list[dict], choose) -> float:
    """score_* 기준(가중 점수 — qa는 승1/무0.5). 백분율."""
    tot = sum(r[f"score_{choose(r)}"] for r in rows)
    return tot / len(rows) * 100


def oracle_score(rows: list[dict]) -> float:
    tot = sum(max(r[f"score_{m}"] for m in WORKERS) for r in rows)
    return tot / len(rows) * 100


def four_way(name: str, rows: list[dict], c_choose) -> tuple[float, float]:
    a = routed_score(rows, lambda r: "solar")
    b = routed_score(rows, lambda r: STATIC.get(r["task"], "solar"))
    c = routed_score(rows, c_choose)
    o = oracle_score(rows)
    dis = [r for r in rows if len({r[f"score_{m}"] for m in WORKERS}) > 1]
    print(f"\n== {name} (n={len(rows)}) ==")
    print(f"  (a) 항상 solar   {a:5.1f}%")
    print(f"  (b) static 규칙  {b:5.1f}%")
    print(f"  (c) 학습 선택기  {c:5.1f}%   {'✅ ≥(b)' if c >= b else '❌ <(b) 손실'}")
    print(f"  oracle 상한      {o:5.1f}%")
    if dis:
        db = routed_score(dis, lambda r: STATIC.get(r["task"], "solar"))
        dc = routed_score(dis, c_choose)
        do = oracle_score(dis)
        print(f"  [불일치 {len(dis)}문항] (b) {db:.1f}%  (c) {dc:.1f}%  oracle {do:.1f}%")
    return b, c


def train_selector(args):
    """합성 train으로 선택기 학습(티어별 프로토콜 동일) → (model, tok, device, max_len)."""
    T2.set_seed(SEED)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    by_id, split = T2.load_split()
    tr = [by_id[i] for i in split["train"]]
    va = [by_id[i] for i in split["val"]]

    if args.tier == 2:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(args.model)
        model = T2.Selector(args.model).to(device)
        bs, lr = 16, 2e-5
    else:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True,
                                            revision=args.revision)
        if tok.pad_token is None:
            tok.pad_token = tok.eos_token
        model = T3.Selector(args.model, args.rank, args.alpha, torch.bfloat16,
                            revision=args.revision).to(device)
        bs, lr = 8, 1e-4

    train_dl = DataLoader(T2.SelDataset(tr, tok, args.max_len), batch_size=bs, shuffle=True,
                          generator=torch.Generator().manual_seed(SEED))
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=lr, weight_decay=0.01)
    lossf = nn.BCEWithLogitsLoss()
    pred = T2.predict_probs if args.tier == 2 else T3.predict_probs

    va_dis = [r for r in va if len({r[f"correct_{m}"] for m in WORKERS}) > 1]
    best_key, best_state, best_ep = (-1.0, -1.0, -1), None, -1
    for ep in range(1, args.epochs + 1):
        model.train()
        for ids, mask, y in train_dl:
            opt.zero_grad()
            loss = lossf(model(ids.to(device), mask.to(device)), y.to(device))
            loss.backward()
            opt.step()
        vp = pred(model, va, tok, device, args.max_len)
        v_c = T2.routed_acc(va, lambda r: T2.pick(vp[r["id"]], r["task"]))
        v_cd = T2.routed_acc(va_dis, lambda r: T2.pick(vp[r["id"]], r["task"])) if va_dis else 0.0
        key = (round(v_c, 6), round(v_cd, 6), ep)
        if key >= best_key:
            best_key, best_ep = key, ep
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
    model.load_state_dict(best_state, strict=False)
    print(f"[선택기 학습] tier{args.tier} {args.model} → best epoch {best_ep} "
          f"(val_c={best_key[0]:.1f} c_dis={best_key[1]:.1f})")
    # margin 게이트 tau를 **val로만** 튜닝 (test/골든 미열람)
    vp = pred(model, va, tok, device, args.max_len)
    tau = tune_tau(va, vp)
    v_plain = T2.routed_acc(va, lambda r: T2.pick(vp[r["id"]], r["task"]))
    v_marg = T2.routed_acc(va, lambda r: pick_margin(vp[r["id"]], r["task"], tau))
    print(f"[margin 게이트] val 튜닝 tau={tau:.2f} (val: argmax {v_plain:.1f}% → margin {v_marg:.1f}%)")
    return model, tok, device, pred, tau


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", type=int, choices=[2, 3], default=2)
    ap.add_argument("--model", default=None)
    ap.add_argument("--revision", default="e949c91dec92")
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--rank", type=int, default=8)
    ap.add_argument("--alpha", type=int, default=16)
    ap.add_argument("--max_len", type=int, default=96)
    args = ap.parse_args()
    if args.model is None:
        args.model = "klue/roberta-base" if args.tier == 2 else "LGAI-EXAONE/EXAONE-3.5-2.4B-Instruct"

    model, tok, device, pred, tau = train_selector(args)

    # --- 골든 holdout 추론 (학습 태스크: structured/routing만) ---
    gold = load_golden()
    trained = [r for r in gold if r["task"] in STATIC]          # structured + routing
    qa = [r for r in gold if r["task"] == "qa"]                 # 선택기 미학습
    gp = pred(model, trained, tok, device, args.max_len)

    def c_choose(r):                                            # (c) 기본: argmax
        if r["task"] not in STATIC:
            return "solar"
        return T2.pick(gp[r["id"]], r["task"])

    def c_margin(r):                                            # (c′) margin 게이트
        if r["task"] not in STATIC:
            return "solar"
        return pick_margin(gp[r["id"]], r["task"], tau)

    print("\n" + "=" * 62)
    print(f"G3 최종 — 골든 holdout (미학습 79문항 중 채점가능 {len(gold)})  tier{args.tier}")
    print("=" * 62)
    for t in ["structured", "routing"]:
        four_way(f"골든 {t}", [r for r in trained if r["task"] == t], c_choose)
    b_all, c_all = four_way("골든 학습태스크 합계 (DB질의+라우팅)", trained, c_choose)
    print(f"\n  골든 선택 분포 (c): {dict(Counter(c_choose(r) for r in trained))}")
    print(f"  → G3 수용선((c) ≥ (b) on 골든): {'PASS' if c_all >= b_all else 'FAIL'}")

    # --- (c′) margin 게이트 재시도 (계획 §4 '1회 재시도') ---
    cm_all = routed_score(trained, c_margin)
    dis = [r for r in trained if len({r[f"score_{m}"] for m in WORKERS}) > 1]
    print(f"\n== [재시도] margin 게이트 tau={tau:.2f} — 골든 학습태스크 합계 (n={len(trained)}) ==")
    print(f"  (b) static      {b_all:5.1f}%")
    print(f"  (c) argmax      {c_all:5.1f}%")
    print(f"  (c′) margin     {cm_all:5.1f}%   {'✅ ≥(b)' if cm_all >= b_all else '❌ <(b)'}")
    print(f"  불일치 {len(dis)}문항: (b) {routed_score(dis, lambda r: STATIC[r['task']]):.1f}%  "
          f"(c′) {routed_score(dis, c_margin):.1f}%")
    print(f"  골든 선택 분포 (c′): {dict(Counter(c_margin(r) for r in trained))}")
    print(f"  → **G3 수용선((c′) ≥ (b) on 골든 holdout): "
          f"{'PASS' if cm_all >= b_all else 'FAIL'}**")

    # --- 합성 test: margin 게이트가 기존 이득을 유지하는가 (트레이드오프) ---
    by_id, split = T2.load_split()
    te = [by_id[i] for i in split["test"]]
    tp = pred(model, te, tok, device, args.max_len)
    t_b = T2.routed_acc(te, lambda r: STATIC[r["task"]])
    t_c = T2.routed_acc(te, lambda r: T2.pick(tp[r["id"]], r["task"]))
    t_cm = T2.routed_acc(te, lambda r: pick_margin(tp[r["id"]], r["task"], tau))
    te_dis = [r for r in te if len({r[f"correct_{m}"] for m in WORKERS}) > 1]
    d_b = T2.routed_acc(te_dis, lambda r: STATIC[r["task"]])
    d_c = T2.routed_acc(te_dis, lambda r: T2.pick(tp[r["id"]], r["task"]))
    d_cm = T2.routed_acc(te_dis, lambda r: pick_margin(tp[r["id"]], r["task"], tau))
    print(f"\n== 합성 test (n={len(te)}) — 같은 선택기, margin 트레이드오프 ==")
    print(f"  (b) static {t_b:5.1f}%   (c) argmax {t_c:5.1f}%   (c′) margin {t_cm:5.1f}%")
    print(f"  불일치 {len(te_dis)}문항: (b) {d_b:.1f}%  (c) {d_c:.1f}%  (c′) {d_cm:.1f}%")

    # qa: 선택기 미학습 — static 고정 참고 보고
    a_qa = routed_score(qa, lambda r: "solar")
    o_qa = oracle_score(qa)
    print(f"\n== 골든 qa (n={len(qa)}, 선택기 미학습 — static=solar 고정) ==")
    print(f"  (a)=(b)=(c) solar {a_qa:5.1f}%   oracle {o_qa:5.1f}%  (문항단위 선택 여지 {o_qa-a_qa:.1f}%p)")


if __name__ == "__main__":
    main()
