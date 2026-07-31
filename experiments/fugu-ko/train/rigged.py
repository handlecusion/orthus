"""D6 부록 — "조작된 승리" 실증. 판을 짜면 학습 선택기가 규칙을 이기는 걸 실제 수치로 보인다.
정직성 대조용: 같은 모델을 (1) 조작된 판, (2) 정직한 판에서 각각 채점해 나란히 낸다.

조작 3종을 계단식으로:
  RIG-C  학습 데이터로 채점(컨닝)        — 가장 노골적. 모델이 외운 걸 다시 물어봄.
  RIG-B  같은 DB·새 질문 + argmax        — 분포 일치 + 게이트 없음(sweep 재사용치 반영)
  HONEST 새 DB holdout + 배포 게이트      — 실제 판정(동률)
McNemar까지. 전부 koelectra 동일 백본.
실행(A100): CUDA_VISIBLE_DEVICES=<free> python train/rigged.py
"""
from __future__ import annotations

import json
import random
import sys
from math import comb
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tier2v as T  # noqa: E402

DATA, GOLDEN, RAW = T.DATA, T.GOLDEN, T.RAW
WORKERS, STATIC, SEED = T.WORKERS, T.STATIC, T.SEED
BB = "monologg/koelectra-base-v3-discriminator"


def mcnemar(a, b):
    ids = set(a) & set(b)
    b10 = sum(1 for i in ids if a[i] and not b[i])
    b01 = sum(1 for i in ids if not a[i] and b[i])
    n = b10 + b01
    if n == 0:
        return b10, b01, 1.0
    k = min(b10, b01)
    return b10, b01, min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def score(rows, pmap, tau):
    cc = {r["id"]: bool(r[f"correct_{T.pick_margin(pmap[r['id']], tau)}"]) for r in rows}
    bc = {r["id"]: bool(r[f"correct_{STATIC}"]) for r in rows}
    b = sum(bc.values()) / len(rows) * 100
    o = sum(1 for r in rows if any(r[f"correct_{m}"] for m in WORKERS)) / len(rows) * 100
    c = sum(cc.values()) / len(rows) * 100
    w, lo, pv = mcnemar(cc, bc)
    return b, o, c, w, lo, pv


def main():
    from transformers import AutoTokenizer
    device = "cuda" if torch.cuda.is_available() else "cpu"
    rows = [json.loads(l) for l in (DATA / "labeled2.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    hold = json.loads((GOLDEN / "t3_unseen_holdout.json").read_text(encoding="utf-8"))["items"]
    wc = json.loads((RAW / "unseen_worker_correct.json").read_text(encoding="utf-8"))
    hold_rows = [{**it, **{f"correct_{m}": bool(wc[m].get(it["id"], False)) for m in WORKERS}} for it in hold]

    # 조작 극대화: 피처 off·dep off(=최고 in-sample 구성) + argmax + 전 데이터 학습 + 다중 seed 앙상블
    tok = AutoTokenizer.from_pretrained(BB)
    uf = False
    print(f"[env] {device} backbone=koelectra features={uf} — 전 3000 학습, seed 3 앙상블")
    et_train, et_hold = [], []
    for s in range(3):
        T.set_seed(SEED + s)
        model = T.Sel(BB).to(device)
        dl = DataLoader(T.DS(rows, tok, uf), batch_size=16, shuffle=True,
                        generator=torch.Generator().manual_seed(SEED + s))
        opt = torch.optim.AdamW(model.parameters(), lr=2e-5, weight_decay=0.01)
        for _ in range(12):  # 넉넉히 — 외우게
            model.train()
            for iid, mask, y in dl:
                opt.zero_grad()
                lg = model(iid.to(device), mask.to(device))
                nn.functional.binary_cross_entropy_with_logits(lg, y.to(device)).backward()
                opt.step()
        et_train.append(T.probs(model, rows, tok, uf, device))
        et_hold.append(T.probs(model, hold_rows, tok, uf, device))
        print(f"  seed {s} done")
        del model; torch.cuda.empty_cache()

    def avg(maps, keys):
        return {k: {m: float(np.mean([mp[k][m] for mp in maps])) for m in WORKERS} for k in keys}
    tp = avg(et_train, [r["id"] for r in rows])
    hp = avg(et_hold, [r["id"] for r in hold_rows])

    print("\n" + "=" * 70)
    # RIG-C: 학습 데이터로 채점(컨닝), argmax
    b, o, c, w, lo, pv = score(rows, tp, 0.0)
    print(f"RIG-C  학습데이터로 채점(컨닝) n={len(rows)}")
    print(f"       규칙 {b:.1f} · 선택기 {c:.1f} (+{c-b:.1f}p) · 오라클 {o:.1f} · McNemar p={pv:.2e} [c만맞{w} b만맞{lo}]")
    # HONEST: 새 DB holdout, argmax(관대) 와 배포 게이트 둘 다
    bh, oh, ch, wh, loh, pvh = score(hold_rows, hp, 0.0)
    print(f"\nHONEST 새 DB holdout n={len(hold_rows)} (argmax, 관대하게)")
    print(f"       규칙 {bh:.1f} · 선택기 {ch:.1f} ({ch-bh:+.1f}p) · 오라클 {oh:.1f} · McNemar p={pvh:.3f} [c만맞{wh} b만맞{loh}]")
    print("=" * 70)
    out = {"rig_c": {"n": len(rows), "static": b, "selector": c, "oracle": o, "gain": c - b, "p": pv, "w": w, "lo": lo},
           "honest": {"n": len(hold_rows), "static": bh, "selector": ch, "oracle": oh, "gain": ch - bh, "p": pvh, "w": wh, "lo": loh}}
    (DATA / "rigged_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("저장: rigged_results.json")


if __name__ == "__main__":
    main()
