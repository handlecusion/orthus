"""G2 티어② — KLUE-RoBERTa 미세조정 선택기 (A100).

설계(d5-learned-selector-plan.md §3):
- 타깃 = 문항별 워커 정답 벡터(multi-label) → 단일 인코더 + 3-way sigmoid head
  P(correct | question, task). 추론 = argmax P, 동률은 static(b) 배정 우선.
  (티어①과 동일한 pick/평가 로직 — 티어 간 비교 공정성)
- 입력 = "[task] question" (orthus는 태스크를 이미 앎 — realizable).
- split = train/data/split.json 재사용(seed 42 고정) — 티어①③과 동일 시험지.

평가(합성 val/test): (a) 항상 solar / (b) static / (c) 티어② / oracle 4자 대조.
불일치 부분집합 별도 보고(H3 예비). G2 게이트: (c) ≥ (b) on 합성 test.

실행:
  export HF_HOME=/data/tta/hf-cache
  CUDA_VISIBLE_DEVICES=2 python train/tier2.py [--model klue/roberta-base]
                                                [--epochs 10] [--bs 16] [--lr 2e-5]
                                                [--smoke]  # 1 epoch 소배치 스모크
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModel, AutoTokenizer

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
WORKERS = ["solar", "ax", "exaone"]
STATIC = {"structured": "solar", "routing": "ax"}  # 2단(b) static_map과 동일
SEED = 42


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def load_split() -> tuple[dict[str, dict], dict[str, list[str]]]:
    rows = [json.loads(l) for l in open(DATA / "labeled.jsonl", encoding="utf-8")]
    by_id = {r["id"]: r for r in rows}
    split = json.load(open(DATA / "split.json", encoding="utf-8"))
    print(f"[split] train {len(split['train'])} / val {len(split['val'])} / test {len(split['test'])}")
    return by_id, split


def featurize_text(r: dict) -> str:
    return f"[{r['task']}] {r['q']}"


def pick(probs: dict[str, float], task: str) -> str:
    """argmax P(correct); 동률(±1e-9)은 static 배정 우선. (티어①과 동일)"""
    static = STATIC[task]
    best = max(probs.values())
    cands = [m for m in WORKERS if probs[m] >= best - 1e-9]
    return static if static in cands else max(cands, key=lambda m: probs[m])


def routed_acc(rows: list[dict], choose) -> float:
    ok = sum(1 for r in rows if r[f"correct_{choose(r)}"])
    return ok / len(rows) * 100


class SelDataset(Dataset):
    def __init__(self, rows: list[dict], tok, max_len: int = 96):
        self.rows = rows
        self.enc = tok([featurize_text(r) for r in rows],
                       truncation=True, max_length=max_len, padding="max_length",
                       return_tensors="pt")
        self.y = torch.tensor(
            [[float(r[f"correct_{m}"]) for m in WORKERS] for r in rows])

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        return (self.enc["input_ids"][i], self.enc["attention_mask"][i], self.y[i])


class Selector(nn.Module):
    def __init__(self, model_name: str):
        super().__init__()
        self.enc = AutoModel.from_pretrained(model_name)
        h = self.enc.config.hidden_size
        self.drop = nn.Dropout(0.1)
        self.head = nn.Linear(h, len(WORKERS))

    def forward(self, ids, mask):
        out = self.enc(input_ids=ids, attention_mask=mask)
        cls = out.last_hidden_state[:, 0]  # [CLS]
        return self.head(self.drop(cls))  # logits (B, 3)


@torch.no_grad()
def predict_probs(model, rows, tok, device, max_len=96) -> dict[str, dict[str, float]]:
    model.eval()
    ds = SelDataset(rows, tok, max_len)
    dl = DataLoader(ds, batch_size=64)
    out = {}
    idx = 0
    for ids, mask, _ in dl:
        logits = model(ids.to(device), mask.to(device))
        probs = torch.sigmoid(logits).cpu().numpy()
        for row_probs in probs:
            r = rows[idx]
            out[r["id"]] = {m: float(row_probs[j]) for j, m in enumerate(WORKERS)}
            idx += 1
    return out


def report(name, rows_, prob_map):
    def c_choose(r):
        return pick(prob_map[r["id"]], r["task"])
    acc_a = routed_acc(rows_, lambda r: "solar")
    acc_b = routed_acc(rows_, lambda r: STATIC[r["task"]])
    acc_c = routed_acc(rows_, c_choose)
    oracle = sum(1 for r in rows_ if any(r[f"correct_{m}"] for m in WORKERS)) / len(rows_) * 100
    print(f"\n== 합성 {name} ({len(rows_)}문항) ==")
    print(f"  (a) 항상 solar      {acc_a:5.1f}%")
    print(f"  (b) static 규칙     {acc_b:5.1f}%")
    print(f"  (c) 티어② 학습     {acc_c:5.1f}%")
    print(f"  oracle 상한         {oracle:5.1f}%")
    dis = [r for r in rows_ if len({r[f"correct_{m}"] for m in WORKERS}) > 1]
    if dis:
        d_b = routed_acc(dis, lambda r: STATIC[r["task"]])
        d_c = routed_acc(dis, c_choose)
        d_o = sum(1 for r in dis if any(r[f"correct_{m}"] for m in WORKERS)) / len(dis) * 100
        print(f"  [불일치 {len(dis)}문항] (b) {d_b:.1f}%  (c) {d_c:.1f}%  oracle {d_o:.1f}%")
    return acc_b, acc_c, c_choose


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="klue/roberta-base")
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--bs", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--max_len", type=int, default=96)
    ap.add_argument("--smoke", action="store_true", help="1 epoch 소배치 스모크")
    args = ap.parse_args()
    if args.smoke:
        args.epochs = 1

    set_seed(SEED)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[env] device={device} model={args.model} epochs={args.epochs} bs={args.bs} lr={args.lr}")

    by_id, split = load_split()
    tr = [by_id[i] for i in split["train"]]
    va = [by_id[i] for i in split["val"]]
    te = [by_id[i] for i in split["test"]]
    if args.smoke:
        tr = tr[:64]

    tok = AutoTokenizer.from_pretrained(args.model)
    model = Selector(args.model).to(device)

    train_ds = SelDataset(tr, tok, args.max_len)
    train_dl = DataLoader(train_ds, batch_size=args.bs, shuffle=True,
                          generator=torch.Generator().manual_seed(SEED))
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    lossf = nn.BCEWithLogitsLoss()

    # val-only best-epoch 선택 (test 미열람). 동점 tiebreak: val (c) → val 불일치 (c) → 최신 epoch.
    va_dis = [r for r in va if len({r[f"correct_{m}"] for m in WORKERS}) > 1]
    best_key, best_state, best_epoch = (-1.0, -1.0, -1), None, -1
    for ep in range(1, args.epochs + 1):
        model.train()
        tot = 0.0
        for ids, mask, y in train_dl:
            opt.zero_grad()
            logits = model(ids.to(device), mask.to(device))
            loss = lossf(logits, y.to(device))
            loss.backward()
            opt.step()
            tot += loss.item()
        vp = predict_probs(model, va, tok, device, args.max_len)
        v_b = routed_acc(va, lambda r: STATIC[r["task"]])
        v_c = routed_acc(va, lambda r: pick(vp[r["id"]], r["task"]))
        v_cd = routed_acc(va_dis, lambda r: pick(vp[r["id"]], r["task"])) if va_dis else 0.0
        print(f"[epoch {ep}] train_loss={tot/len(train_dl):.4f}  val (b){v_b:.1f} (c){v_c:.1f} (c_dis){v_cd:.1f}")
        key = (round(v_c, 6), round(v_cd, 6), ep)  # val-only, 최신 epoch 우선
        if key >= best_key:
            best_key, best_epoch = key, ep
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    if best_state is not None:
        model.load_state_dict(best_state)
        print(f"\n[best] val 선택 = epoch {best_epoch} (val_c={best_key[0]:.1f} c_dis={best_key[1]:.1f})")

    vp = predict_probs(model, va, tok, device, args.max_len)
    tp = predict_probs(model, te, tok, device, args.max_len)
    report("val", va, vp)
    b_test, c_test, c_choose = report("test", te, tp)
    gate = "PASS" if c_test >= b_test else "FAIL"
    print(f"  → G2 게이트((c)≥(b) on test): {gate}")
    dist = Counter(c_choose(r) for r in te)
    print(f"\n  티어② test 선택 분포: {dict(dist)}")


if __name__ == "__main__":
    main()
