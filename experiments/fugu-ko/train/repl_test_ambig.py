"""D6 ambig 재현 판정(B+C) — non-ambig로 학습한 같은 레시피 모델을:
  (C) FRESH ambig(data/fresh_ambig.jsonl)에 시험 → 사전선언 단일 재현 McNemar
  (B) 원 168 ambig(labeled2)에 시험 → 부트스트랩 2000회로 +3.6p가 한 번 운인지 CI 확인
둘 다 부트스트랩 95% CI(gain>0 비율)까지 낸다. 학습 레시피 = by_task=ambig와 동일(train=non-ambig).
실행(A100): CUDA_VISIBLE_DEVICES=<free> python train/repl_test_ambig.py --seeds 3
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from math import comb
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tier2v as T  # noqa: E402

DATA = T.DATA
WORKERS = T.WORKERS
STATIC = T.STATIC
SEED = T.SEED


def mcnemar(a, b):
    ids = set(a) & set(b)
    b10 = sum(1 for i in ids if a[i] and not b[i])
    b01 = sum(1 for i in ids if not a[i] and b[i])
    n = b10 + b01
    if n == 0:
        return b10, b01, 1.0
    k = min(b10, b01)
    return b10, b01, min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def boot_ci(ids, cc, bc, iters=2000):
    rng = random.Random(SEED)
    gains = []
    for _ in range(iters):
        samp = [ids[rng.randrange(len(ids))] for _ in ids]
        g = (sum(cc[i] for i in samp) - sum(bc[i] for i in samp)) / len(samp) * 100
        gains.append(g)
    gains.sort()
    lo, hi = gains[int(0.025 * iters)], gains[int(0.975 * iters)]
    frac_pos = sum(1 for g in gains if g > 0) / iters
    return lo, hi, frac_pos


def train_predict(tr, va, tests, uf, dep, seeds, epochs, bs, lr, backbone, device):
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(backbone)
    ev = []
    et = {name: [] for name in tests}
    for s in range(seeds):
        T.set_seed(SEED + s)
        model = T.Sel(backbone).to(device)
        dl = DataLoader(T.DS(tr, tok, uf), batch_size=bs, shuffle=True,
                        generator=torch.Generator().manual_seed(SEED + s))
        opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
        best_state, best_v = None, -1
        for _ in range(epochs):
            model.train()
            for iid, mask, y in dl:
                opt.zero_grad()
                lg = model(iid.to(device), mask.to(device))
                loss = T.dep_loss(lg, y.to(device)) if dep else \
                    nn.functional.binary_cross_entropy_with_logits(lg, y.to(device))
                loss.backward(); opt.step()
            vc = T.acc_labeled(va, T.probs(model, va, tok, uf, device), 0.0)
            if vc >= best_v:
                best_v, best_state = vc, {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        model.load_state_dict(best_state)
        temp = T.tune_temp(model, va, tok, uf, device)
        ev.append(T.probs(model, va, tok, uf, device, temp))
        for name, rows in tests.items():
            et[name].append(T.probs(model, rows, tok, uf, device, temp))
        del model; torch.cuda.empty_cache()

    def avg(maps, keys):
        return {k: {m: float(np.mean([mp[k][m] for mp in maps])) for m in WORKERS} for k in keys}
    vp = avg(ev, [r["id"] for r in va])
    tau = max([0.0, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3], key=lambda t: (T.acc_labeled(va, vp, t), t))
    out = {}
    for name, rows in tests.items():
        tp = avg(et[name], [r["id"] for r in rows])
        out[name] = (tp, tau, rows)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backbone", default="monologg/koelectra-base-v3-discriminator")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--bs", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-5)
    a = ap.parse_args()
    uf, dep = True, True
    device = "cuda" if torch.cuda.is_available() else "cpu"

    rows = [json.loads(l) for l in (DATA / "labeled2.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    non_ambig = [r for r in rows if r.get("tag") != "ambig"]
    orig_ambig = [r for r in rows if r.get("tag") == "ambig"]
    random.Random(SEED).shuffle(non_ambig)
    cut = int(len(non_ambig) * 0.85)
    tr, va = non_ambig[:cut], non_ambig[cut:]

    tests = {"orig_ambig(168)": orig_ambig}
    fp = DATA / "fresh_ambig.jsonl"
    if fp.exists():
        fresh = [json.loads(l) for l in fp.read_text(encoding="utf-8").splitlines() if l.strip()]
        fresh = [r for r in fresh if all(f"correct_{m}" in r for m in WORKERS)]
        tests["FRESH_ambig(%d)" % len(fresh)] = fresh
    else:
        print("[warn] fresh_ambig.jsonl 없음 — 원 168만 부트스트랩")

    print(f"[env] {device} backbone={a.backbone.split('/')[-1]} train(non-ambig) {len(tr)} val {len(va)} seeds={a.seeds}")
    res = train_predict(tr, va, tests, uf, dep, a.seeds, a.epochs, a.bs, a.lr, a.backbone, device)
    print("\n" + "=" * 74)
    print("사전선언 가설: 'ambig 질문에서 선택기가 static(solar)을 이긴다' (단일 방향 테스트)")
    print("=" * 74)
    for name, (tp, tau, trows) in res.items():
        ids = [r["id"] for r in trows]
        cc = {r["id"]: bool(r[f"correct_{T.pick_margin(tp[r['id']], tau)}"]) for r in trows}
        bc = {r["id"]: bool(r[f"correct_{STATIC}"]) for r in trows}
        b = sum(bc.values()) / len(ids) * 100
        o = sum(1 for r in trows if any(r[f"correct_{m}"] for m in WORKERS)) / len(ids) * 100
        cr = sum(cc.values()) / len(ids) * 100
        w, lo, pv = mcnemar(cc, bc)
        blo, bhi, fpos = boot_ci(ids, cc, bc)
        choice = {r["id"]: T.pick_margin(tp[r["id"]], tau) for r in trows}
        sig = "★유의승" if pv < 0.05 and cr > b else "무의"
        print(f"\n── {name} (tau={tau}) ──")
        print(f"   static {b:.1f} · oracle {o:.1f} · headroom {o-b:+.1f}p")
        print(f"   선택기 {cr:.1f}  (gain {cr-b:+.1f}p)  McNemar p={pv:.4f} [c만맞{w} b만맞{lo}]  {sig}")
        print(f"   부트스트랩 95%CI of gain: [{blo:+.1f}, {bhi:+.1f}]p · P(gain>0)={fpos:.1%}")
        print(f"   선택분포 {dict(Counter(choice.values()))}")
        dis = [r for r in trows if len({r[f'correct_{m}'] for m in WORKERS}) > 1]
        if dis:
            cd = sum(1 for r in dis if cc[r["id"]]) / len(dis) * 100
            bd = sum(1 for r in dis if bc[r["id"]]) / len(dis) * 100
            print(f"   [불일치 {len(dis)}] static {bd:.1f} · 선택기 {cd:.1f} ({cd-bd:+.1f}p)")
    print("\n판정: FRESH_ambig에서 p<0.05 & CI 하한>0 & P(gain>0)>0.95 이면 → 재현 성공(진짜 신호).")


if __name__ == "__main__":
    main()
