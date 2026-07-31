"""G2 티어③ — EXAONE-3.5 LoRA 선택기 (A100 80G).

설계(d5-learned-selector-plan.md §3, 2026-07-12 개정):
- 백본 = EXAONE-3.5-Instruct(2.4B / 7.8B) causal LM, LoRA 어댑터만 학습(base frozen).
- 방식 = **티어②와 동일한 3-way sigmoid 분류 head**(생성형 SFT 아님) — 티어①→②→③이
  오직 "인코더 규모"(단어통계 → RoBERTa 110M → EXAONE 2.4B/7.8B)만 달라지게 하여
  보고서의 "선택기에 얼마짜리 모델이 필요한가"에 깨끗한 스케일 축을 만든다.
- pooling = 마지막 비-pad 토큰 hidden(causal LM). 추론 = argmax P, 동률 static 우선
  (티어①② 동일 pick/평가/`split.json` — 티어 간 비교 공정성).

데이터(730·train 510)는 그대로라 과적합 주의: LoRA rank 낮게 + val (c) best-epoch 선택.

실행:
  export HF_HOME=/data/tta/hf-cache
  CUDA_VISIBLE_DEVICES=2 python train/tier3.py --model LGAI-EXAONE/EXAONE-3.5-2.4B-Instruct
                                               [--epochs 12] [--bs 8] [--lr 1e-4] [--rank 8]
                                               [--smoke]
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
from peft import LoraConfig, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
WORKERS = ["solar", "ax", "exaone"]
STATIC = {"structured": "solar", "routing": "ax"}
SEED = 42
ATTN_SUFFIXES = ("q_proj", "k_proj", "v_proj", "o_proj", "out_proj")


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def load_split():
    rows = [json.loads(l) for l in open(DATA / "labeled.jsonl", encoding="utf-8")]
    by_id = {r["id"]: r for r in rows}
    split = json.load(open(DATA / "split.json", encoding="utf-8"))
    print(f"[split] train {len(split['train'])} / val {len(split['val'])} / test {len(split['test'])}")
    return by_id, split


def featurize_text(r: dict) -> str:
    return f"[{r['task']}] {r['q']}"


def pick(probs, task):
    static = STATIC[task]
    best = max(probs.values())
    cands = [m for m in WORKERS if probs[m] >= best - 1e-9]
    return static if static in cands else max(cands, key=lambda m: probs[m])


def routed_acc(rows, choose):
    ok = sum(1 for r in rows if r[f"correct_{choose(r)}"])
    return ok / len(rows) * 100


class SelDataset(Dataset):
    def __init__(self, rows, tok, max_len=96):
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
    def __init__(self, model_name, rank, alpha, dtype, revision=None):
        super().__init__()
        base = AutoModelForCausalLM.from_pretrained(model_name, trust_remote_code=True,
                                                    torch_dtype=dtype, revision=revision)
        targets = sorted({n.split(".")[-1] for n, m in base.named_modules()
                          if isinstance(m, nn.Linear) and n.endswith(ATTN_SUFFIXES)})
        if not targets:
            raise RuntimeError("no attention Linear targets found for LoRA")
        print(f"[lora] targets={targets} rank={rank} alpha={alpha}")
        lcfg = LoraConfig(r=rank, lora_alpha=alpha, lora_dropout=0.05,
                          target_modules=targets, bias="none", task_type="CAUSAL_LM")
        self.enc = get_peft_model(base, lcfg)
        h = base.config.hidden_size
        self.head = nn.Sequential(nn.Dropout(0.1), nn.Linear(h, len(WORKERS)))
        self.head = self.head.to(dtype=torch.float32)

    def forward(self, ids, mask):
        out = self.enc(input_ids=ids, attention_mask=mask, output_hidden_states=True)
        hs = out.hidden_states[-1]                       # 마지막 레이어 (B, T, H)
        last = mask.sum(1) - 1                            # 마지막 비-pad 위치
        pooled = hs[torch.arange(hs.size(0), device=hs.device), last]
        return self.head(pooled.float())                 # logits (B, 3)


@torch.no_grad()
def predict_probs(model, rows, tok, device, max_len=96):
    model.eval()
    dl = DataLoader(SelDataset(rows, tok, max_len), batch_size=32)
    out, idx = {}, 0
    for ids, mask, _ in dl:
        logits = model(ids.to(device), mask.to(device))
        probs = torch.sigmoid(logits).float().cpu().numpy()
        for rp in probs:
            r = rows[idx]
            out[r["id"]] = {m: float(rp[j]) for j, m in enumerate(WORKERS)}
            idx += 1
    return out


def report(name, rows_, prob_map, tier_label):
    def c_choose(r):
        return pick(prob_map[r["id"]], r["task"])
    acc_a = routed_acc(rows_, lambda r: "solar")
    acc_b = routed_acc(rows_, lambda r: STATIC[r["task"]])
    acc_c = routed_acc(rows_, c_choose)
    oracle = sum(1 for r in rows_ if any(r[f"correct_{m}"] for m in WORKERS)) / len(rows_) * 100
    print(f"\n== 합성 {name} ({len(rows_)}문항) ==")
    print(f"  (a) 항상 solar      {acc_a:5.1f}%")
    print(f"  (b) static 규칙     {acc_b:5.1f}%")
    print(f"  (c) {tier_label}   {acc_c:5.1f}%")
    print(f"  oracle 상한         {oracle:5.1f}%")
    dis = [r for r in rows_ if len({r[f"correct_{m}"] for m in WORKERS}) > 1]
    if dis:
        d_b = routed_acc(dis, lambda r: STATIC[r["task"]])
        d_c = routed_acc(dis, c_choose)
        d_o = sum(1 for r in dis if any(r[f"correct_{m}"] for m in WORKERS)) / len(dis) * 100
        print(f"  [불일치 {len(dis)}문항] (b) {d_b:.1f}%  (c) {d_c:.1f}%  oracle {d_o:.1f}%")
    return acc_b, acc_c, c_choose


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="LGAI-EXAONE/EXAONE-3.5-2.4B-Instruct")
    # 2026-02 리비전은 unreleased "Transformers v5"(RopeParameters) 가정 → v5-이전(2024-12) 리비전 고정
    ap.add_argument("--revision", default="e949c91dec92")
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--bs", type=int, default=8)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--rank", type=int, default=8)
    ap.add_argument("--alpha", type=int, default=16)
    ap.add_argument("--max_len", type=int, default=96)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    if args.smoke:
        args.epochs = 1
    tier_label = f"티어③({args.model.split('-')[-2]})"

    set_seed(SEED)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16
    print(f"[env] device={device} model={args.model} epochs={args.epochs} bs={args.bs} "
          f"lr={args.lr} rank={args.rank} dtype=bf16")

    by_id, split = load_split()
    tr = [by_id[i] for i in split["train"]]
    va = [by_id[i] for i in split["val"]]
    te = [by_id[i] for i in split["test"]]
    if args.smoke:
        tr = tr[:32]

    tok = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True, revision=args.revision)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = Selector(args.model, args.rank, args.alpha, dtype, revision=args.revision).to(device)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"[params] trainable={trainable/1e6:.2f}M")

    train_dl = DataLoader(SelDataset(tr, tok, args.max_len), batch_size=args.bs,
                          shuffle=True, generator=torch.Generator().manual_seed(SEED))
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=args.lr, weight_decay=0.01)
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
        key = (round(v_c, 6), round(v_cd, 6), ep)
        if key >= best_key:
            best_key, best_epoch = key, ep
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
    if best_state is not None:
        model.load_state_dict(best_state, strict=False)
        print(f"\n[best] val 선택 = epoch {best_epoch} (val_c={best_key[0]:.1f} c_dis={best_key[1]:.1f})")

    vp = predict_probs(model, va, tok, device, args.max_len)
    tp = predict_probs(model, te, tok, device, args.max_len)
    report("val", va, vp, tier_label)
    b_test, c_test, c_choose = report("test", te, tp, tier_label)
    print(f"  → G2 게이트((c)≥(b) on test): {'PASS' if c_test >= b_test else 'FAIL'}")
    print(f"\n  {tier_label} test 선택 분포: {dict(Counter(c_choose(r) for r in te))}")


if __name__ == "__main__":
    main()
