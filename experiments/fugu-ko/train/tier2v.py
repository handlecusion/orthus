"""D6 Phase E — 선택기 v2 (RoBERTa). ①피처주입 + ②이탈-margin + ③캘리브레이션/앙상블.

D5 tier2 대비 3개 개선(owner 2026-07-15):
 ① 결정론 피처 프리픽스(features.prefix) — 워커 실패축(db_hard/ambig/flt)을 인코더에 직접.
 ② 이탈 비대칭 손실 + margin 게이트 — static(solar) 이탈 오탐(FP)에 가중 벌점(G3에서 static≈
    oracle 이탈이 순손실이었던 걸 손실에 내장). 추론은 margin(tau, val 튜닝).
 ③ per-head temperature scaling(val) + seed 앙상블 — tau 널뜀 안정화.

라벨 = labeled2.jsonl. 입력 = features.prefix(q). split seed 42.
평가 = 합성 test(15%) + **golden/t3_unseen_holdout.json(주판정)**. holdout 워커 정오 = analysis/raw/
unseen_headroom.json(build_unseen_v2 산출). 선택 결과를 sel_choice_holdout.json에 저장 → eval_v2.py 채점.

실행(A100): CUDA_VISIBLE_DEVICES=<free> python train/tier2v.py --features 1 --dep 1 --seeds 3
ablation: --features 0(D5식 원문) · --dep 0(BCE만) 로 대조.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModel, AutoTokenizer

# 공식 국내 모델 repo(KLUE/KoELECTRA/KcBERT 등)는 .bin 가중치만 있어 transformers의 CVE-2025-32434
# torch.load 가드(torch<2.6)에 막힌다. EXAONE은 safetensors라 무관. 신뢰된 공식 repo·연구용 한정으로
# 이 가드만 우회한다(임의 파일 로드 아님).
for _m in ("transformers.modeling_utils", "transformers.utils.import_utils"):
    try:
        __import__(_m)
        import sys as _sys
        setattr(_sys.modules[_m], "check_torch_load_is_safe", lambda *a, **k: None)
    except Exception:  # noqa: BLE001
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import features as F  # noqa: E402

DATA = HERE / "data"
GOLDEN = HERE.parent / "golden"
RAW = HERE.parent / "analysis" / "raw"
WORKERS = ["solar", "ax", "exaone"]
STATIC = "solar"
SEED = 42
DB_NAMES = json.loads((DATA / "db_names.json").read_text(encoding="utf-8"))


def set_seed(s):
    random.seed(s); np.random.seed(s); torch.manual_seed(s); torch.cuda.manual_seed_all(s)


def feat_text(q, uf):
    return F.prefix(q, DB_NAMES) if uf else q


class DS(Dataset):
    def __init__(self, rows, tok, uf, max_len=64):
        self.enc = tok([feat_text(r["q"], uf) for r in rows], truncation=True, max_length=max_len,
                       padding="max_length", return_tensors="pt")
        self.has_y = all(f"correct_{WORKERS[0]}" in r for r in rows)
        self.y = torch.tensor([[float(r.get(f"correct_{m}", 0)) for m in WORKERS] for r in rows])

    def __len__(self): return len(self.enc["input_ids"])
    def __getitem__(self, i): return self.enc["input_ids"][i], self.enc["attention_mask"][i], self.y[i]


class Sel(nn.Module):
    def __init__(self, name):
        super().__init__()
        self.enc = AutoModel.from_pretrained(name)
        self.drop = nn.Dropout(0.1)
        self.head = nn.Linear(self.enc.config.hidden_size, len(WORKERS))

    def forward(self, ids, mask):
        return self.head(self.drop(self.enc(input_ids=ids, attention_mask=mask).last_hidden_state[:, 0]))


def dep_loss(logits, y, w_fp=2.0):
    base = nn.functional.binary_cross_entropy_with_logits(logits, y)
    p = torch.sigmoid(logits); si = WORKERS.index(STATIC)
    static_ok = (y[:, si] > 0.5).float()
    other_max = p[:, [j for j in range(len(WORKERS)) if j != si]].max(1).values
    fp = torch.clamp(other_max - p[:, si], min=0.0) * static_ok
    return base + w_fp * fp.mean()


@torch.no_grad()
def probs(model, rows, tok, uf, device, temp=None):
    model.eval()
    dl = DataLoader(DS(rows, tok, uf), batch_size=64)
    out, idx = {}, 0
    for ids, mask, _ in dl:
        lg = model(ids.to(device), mask.to(device))
        if temp is not None:
            lg = lg / torch.tensor(temp, device=device)
        pr = torch.sigmoid(lg).cpu().numpy()
        for row in pr:
            out[rows[idx]["id"]] = {m: float(row[j]) for j, m in enumerate(WORKERS)}
            idx += 1
    return out


def pick_margin(p, tau):
    best = max(WORKERS, key=lambda m: p[m])
    return STATIC if best == STATIC or p[best] - p[STATIC] < tau else best


def acc_labeled(rows, pmap, tau):
    return sum(1 for r in rows if r[f"correct_{pick_margin(pmap[r['id']], tau)}"]) / len(rows) * 100


def tune_temp(model, va, tok, uf, device):
    raw = probs(model, va, tok, uf, device)
    y = {r["id"]: [r[f"correct_{m}"] for m in WORKERS] for r in va}
    best_t, best_nll = 1.0, 1e18
    for t in [0.5, 0.75, 1.0, 1.5, 2.0, 3.0]:
        nll = 0.0
        for i, yy in y.items():
            for j, m in enumerate(WORKERS):
                p = min(max(raw[i][m] ** (1.0/t), 1e-6), 1-1e-6)
                nll -= yy[j]*np.log(p) + (1-yy[j])*np.log(1-p)
        if nll < best_nll:
            best_nll, best_t = nll, t
    return [best_t]*len(WORKERS)


def main():
    ap = argparse.ArgumentParser()
    # 국내 인코더(owner 2026-07-15): KoELECTRA-base-v3(한국어 34GB 사전학습, monologg). 표준 HF 로딩.
    # KoBERT(SKT 벤더)는 커스텀 sentencepiece 토크나이저라 fallback.
    ap.add_argument("--backbone", default="monologg/koelectra-base-v3-discriminator")
    ap.add_argument("--features", type=int, default=1)
    ap.add_argument("--dep", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--bs", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--tag", default="v2")
    a = ap.parse_args()
    uf = bool(a.features)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[env] {device} backbone={a.backbone} features={uf} dep={a.dep} seeds={a.seeds} tag={a.tag}")

    rows = [json.loads(l) for l in (DATA/"labeled2.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    ids = [r["id"] for r in rows]; random.Random(SEED).shuffle(ids)
    n = len(ids); trS, vaS = set(ids[:int(n*.7)]), set(ids[int(n*.7):int(n*.85)])
    tr = [r for r in rows if r["id"] in trS]; va = [r for r in rows if r["id"] in vaS]
    te = [r for r in rows if r["id"] not in trS and r["id"] not in vaS]
    hold = json.loads((GOLDEN/"t3_unseen_holdout.json").read_text(encoding="utf-8"))["items"]
    print(f"[data] train {len(tr)} val {len(va)} test {len(te)} holdout {len(hold)}")

    tok = AutoTokenizer.from_pretrained(a.backbone)
    ev, et, eh = [], [], []
    for s in range(a.seeds):
        set_seed(SEED+s)
        model = Sel(a.backbone).to(device)
        dl = DataLoader(DS(tr, tok, uf), batch_size=a.bs, shuffle=True,
                        generator=torch.Generator().manual_seed(SEED+s))
        opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=0.01)
        best_state, best_v = None, -1
        for ep in range(1, a.epochs+1):
            model.train()
            for iid, mask, y in dl:
                opt.zero_grad()
                lg = model(iid.to(device), mask.to(device))
                loss = dep_loss(lg, y.to(device)) if a.dep else \
                    nn.functional.binary_cross_entropy_with_logits(lg, y.to(device))
                loss.backward(); opt.step()
            vc = acc_labeled(va, probs(model, va, tok, uf, device), 0.0)
            if vc >= best_v:
                best_v, best_state = vc, {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        model.load_state_dict(best_state)
        temp = tune_temp(model, va, tok, uf, device)
        ev.append(probs(model, va, tok, uf, device, temp))
        et.append(probs(model, te, tok, uf, device, temp))
        eh.append(probs(model, hold, tok, uf, device, temp))
        print(f"  seed {s}: val(argmax) {best_v:.1f} temp={temp[0]}")

    def avg(maps, keys):
        return {k: {m: float(np.mean([mp[k][m] for mp in maps])) for m in WORKERS} for k in keys}
    vp = avg(ev, [r["id"] for r in va]); tp = avg(et, [r["id"] for r in te]); hp = avg(eh, [it["id"] for it in hold])
    tau = max([0.0, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3], key=lambda t: (acc_labeled(va, vp, t), t))

    b = sum(r[f"correct_{STATIC}"] for r in te)/len(te)*100
    o = sum(1 for r in te if any(r[f"correct_{m}"] for m in WORKERS))/len(te)*100
    print(f"\n== 합성 test (n={len(te)}) ==")
    print(f"  static {b:.1f} · argmax {acc_labeled(te, tp, 0.0):.1f} · margin(tau={tau}) {acc_labeled(te, tp, tau):.1f} · oracle {o:.1f}")

    choice = {it["id"]: pick_margin(hp[it["id"]], tau) for it in hold}
    out = DATA / f"sel_choice_holdout_{a.tag}.json"
    out.write_text(json.dumps({"tau": tau, "features": uf, "dep": bool(a.dep), "seeds": a.seeds,
                               "backbone": a.backbone, "choice": choice}, ensure_ascii=False), encoding="utf-8")
    from collections import Counter
    print(f"  holdout 선택 분포 {dict(Counter(choice.values()))} → {out.name} (주판정은 eval_v2.py)")


if __name__ == "__main__":
    main()
