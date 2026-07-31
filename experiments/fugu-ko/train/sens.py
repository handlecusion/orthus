"""D6 민감도 분석 — "결과가 데이터셋 구성(분할)에 얼마나 좌우되나"를 실측.

같은 학습 데이터·같은 백본(koelectra)으로 **분할 방식만 바꿔가며** 선택기 vs static을 잰다.
분포이동 두 축을 분리:
  random   — 이동 없음(같은 DB·같은 task, 새 질문). 방법이 성립하는지 상한 확인.
  by_task  — task 이동만(같은 DB, 홀드아웃 task-type). task-typed 신호가 전이되나?
  by_db    — DB 이동만(홀드아웃 DB). 현재 주판정 재현.

각 regime: n · static% · oracle% · headroom · 선택기% · McNemar p · 불일치 부분집합.
실행(A100): CUDA_VISIBLE_DEVICES=<free> python train/sens.py --backbone monologg/koelectra-base-v3-discriminator --features 1 --dep 1 --seeds 3
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
import tier2v as T  # noqa: E402  (Sel/DS/dep_loss/probs/pick_margin/acc_labeled/tune_temp/set_seed 재사용)

DATA = T.DATA
GOLDEN = T.GOLDEN
RAW = T.RAW
WORKERS = T.WORKERS
STATIC = T.STATIC
SEED = T.SEED


def mcnemar(a: dict, b: dict):
    ids = set(a) & set(b)
    b10 = sum(1 for i in ids if a[i] and not b[i])
    b01 = sum(1 for i in ids if not a[i] and b[i])
    n = b10 + b01
    if n == 0:
        return b10, b01, 1.0
    k = min(b10, b01)
    return b10, b01, min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def load_pool():
    """labeled2(학습 DB) + holdout(홀드아웃 DB, 워커정오 fold-in). 각 row: id/q/db/tag/correct_*."""
    lab = [json.loads(l) for l in (DATA / "labeled2.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    for r in lab:
        r["db"] = r["spec"][0]
    train_rows = lab
    hold = json.loads((GOLDEN / "t3_unseen_holdout.json").read_text(encoding="utf-8"))["items"]
    wc = json.loads((RAW / "unseen_worker_correct.json").read_text(encoding="utf-8"))
    hold_rows = []
    for it in hold:
        r = {"id": it["id"], "q": it["q"], "db": it["spec"][0], "tag": it.get("tag")}
        for m in WORKERS:
            r[f"correct_{m}"] = bool(wc[m].get(it["id"], False))
        hold_rows.append(r)
    train_dbs = {r["db"] for r in train_rows}
    hold_dbs = {r["db"] for r in hold_rows}
    return train_rows, hold_rows, train_dbs, hold_dbs


def stat(rows):
    b = sum(r[f"correct_{STATIC}"] for r in rows) / len(rows) * 100
    o = sum(1 for r in rows if any(r[f"correct_{m}"] for m in WORKERS)) / len(rows) * 100
    return b, o


def train_eval(tr, va, te, backbone, uf, dep, seeds, epochs, bs, lr, device):
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(backbone)
    ev, et = [], []
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
        et.append(T.probs(model, te, tok, uf, device, temp))
        del model
        torch.cuda.empty_cache()

    def avg(maps, keys):
        return {k: {m: float(np.mean([mp[k][m] for mp in maps])) for m in WORKERS} for k in keys}
    vp = avg(ev, [r["id"] for r in va]); tp = avg(et, [r["id"] for r in te])
    tau = max([0.0, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3], key=lambda t: (T.acc_labeled(va, vp, t), t))
    c_correct = {r["id"]: bool(r[f"correct_{T.pick_margin(tp[r['id']], tau)}"]) for r in te}
    b_correct = {r["id"]: bool(r[f"correct_{STATIC}"]) for r in te}
    choice = {r["id"]: T.pick_margin(tp[r["id"]], tau) for r in te}
    return tau, c_correct, b_correct, choice


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backbone", default="monologg/koelectra-base-v3-discriminator")
    ap.add_argument("--features", type=int, default=1)
    ap.add_argument("--dep", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--bs", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--held_tasks", default="groupby,ambig")
    a = ap.parse_args()
    uf, dep = bool(a.features), bool(a.dep)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    train_rows, hold_rows, train_dbs, hold_dbs = load_pool()
    print(f"[env] {device} backbone={a.backbone} features={uf} dep={dep} seeds={a.seeds}")
    print(f"[pool] 학습DB rows {len(train_rows)} ({len(train_dbs)} DB) · 홀드아웃DB rows {len(hold_rows)} ({len(hold_dbs)} DB)")
    print(f"[pool] 두 DB집합 교집합: {sorted(train_dbs & hold_dbs) or '없음(완전 분리 ✓)'}")
    print(f"[pool] 학습풀 tag별 headroom(static→oracle):")
    for tg in ["filter2", "filter", "groupby", "ambig", "count"]:
        sub = [r for r in train_rows if r.get("tag") == tg]
        if sub:
            b, o = stat(sub)
            print(f"        {tg:8s} n={len(sub):4d}  static {b:.1f}  oracle {o:.1f}  headroom {o-b:+.1f}p")

    rng = random.Random(SEED)

    def split_random(pool, tr_frac=0.70, va_frac=0.15):
        ids = [r["id"] for r in pool]; rng.shuffle(ids)
        n = len(ids); trS = set(ids[:int(n*tr_frac)]); vaS = set(ids[int(n*tr_frac):int(n*(tr_frac+va_frac))])
        tr = [r for r in pool if r["id"] in trS]; va = [r for r in pool if r["id"] in vaS]
        te = [r for r in pool if r["id"] not in trS and r["id"] not in vaS]
        return tr, va, te

    regimes = []
    # 1) random(matched): 학습 DB 풀 내 랜덤 70/15/15
    tr, va, te = split_random(train_rows)
    regimes.append(("random(matched)", tr, va, te))
    # 2) by_task: 홀드아웃 task-type만 test(같은 DB), 나머지 task로 학습
    for tg in [t.strip() for t in a.held_tasks.split(",") if t.strip()]:
        te = [r for r in train_rows if r.get("tag") == tg]
        rest = [r for r in train_rows if r.get("tag") != tg]
        rng.shuffle(rest)
        cut = int(len(rest) * 0.85)
        tr, va = rest[:cut], rest[cut:]
        if len(te) >= 20:
            regimes.append((f"by_task={tg}", tr, va, te))
    # 3) by_db(현재 주판정): 학습 DB 전체로 학습, 홀드아웃 DB로 test
    rng.shuffle(train_rows)
    cut = int(len(train_rows) * 0.85)
    regimes.append(("by_db(현재)", train_rows[:cut], train_rows[cut:], hold_rows))

    print("\n" + "=" * 78)
    print(f"{'regime':20s} {'n':>4s} {'static':>7s} {'oracle':>7s} {'head':>6s} {'선택기':>7s} {'gain':>6s} {'McNemar':>9s}  판정")
    print("-" * 78)
    results = []
    for name, tr, va, te in regimes:
        b, o = stat(te)
        tau, cc, bc, choice = train_eval(tr, va, te, a.backbone, uf, dep, a.seeds, a.epochs, a.bs, a.lr, device)
        c_rate = sum(cc.values()) / len(cc) * 100
        w, lo, pv = mcnemar(cc, bc)
        gain = c_rate - b
        verdict = "★유의승" if pv < 0.05 and gain > 0 else ("유의패" if pv < 0.05 and gain < 0 else "무의(동률권)")
        print(f"{name:20s} {len(te):4d} {b:7.1f} {o:7.1f} {o-b:+6.1f} {c_rate:7.1f} {gain:+6.1f} {'p='+format(pv,'.3f'):>9s}  {verdict}")
        dis = [r for r in te if len({r[f'correct_{m}'] for m in WORKERS}) > 1]
        if dis:
            cd = sum(1 for r in dis if cc[r["id"]]) / len(dis) * 100
            bd = sum(1 for r in dis if bc[r["id"]]) / len(dis) * 100
            print(f"{'  └ 불일치'+str(len(dis)):20s} {'':4s} {bd:7.1f} {'':7s} {'':6s} {cd:7.1f} {cd-bd:+6.1f}   (c만맞 {w}, b만맞 {lo}) 선택분포 {dict(Counter(choice.values()))}")
        results.append({"regime": name, "n": len(te), "static": b, "oracle": o, "headroom": o-b,
                        "selector": c_rate, "gain": gain, "p": pv, "tau": tau, "w": w, "lo": lo})
    print("=" * 78)
    out = DATA / "sens_results.json"
    out.write_text(json.dumps({"backbone": a.backbone, "features": uf, "dep": dep, "seeds": a.seeds,
                               "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out.name}")
    print("해석: random 유의승 & by_db 무의 → 방법은 성립하나 DB이동에 전이실패(F9). "
          "by_task도 무의면 신호가 task-typed가 아님(=DB-국소적) 확정.")


if __name__ == "__main__":
    main()
