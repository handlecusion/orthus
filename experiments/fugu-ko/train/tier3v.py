"""D6 Phase E — 선택기 v2 (EXAONE-3.5 LoRA). tier2v와 동일 로직, 인코더만 causal LM+LoRA.

D5 tier3 계승(2.4B sweet spot, 7.8B 과적합) + tier2v의 ①피처 ②이탈-margin ③캘리브레이션/앙상블.
tier2v에서 공유 로직(feat_text/dep_loss/pick_margin/acc_labeled/tune_temp/DS)을 import.
차이: 백본 = AutoModelForCausalLM + LoRA(attention), pooling = 마지막 비-pad 토큰.

실행(A100): CUDA_VISIBLE_DEVICES=<free> python train/tier3v.py \
  --model LGAI-EXAONE/EXAONE-3.5-2.4B-Instruct --revision e949c91dec92 --features 1 --dep 1 --seeds 2
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader
from transformers import AutoModelForCausalLM, AutoTokenizer

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tier2v as T  # noqa: E402  (공유 로직)

DATA = T.DATA
GOLDEN = T.GOLDEN
WORKERS = T.WORKERS
SEED = T.SEED
ATTN = ("q_proj", "k_proj", "v_proj", "o_proj", "out_proj")


class SelCausal(nn.Module):
    """EXAONE causal LM 선택기. 스케일 스윕용:
      full=True         전체 파인튜닝(2.4B ~40GB, 7.8B는 --opt8bit 필요)
      full=False        LoRA(peft) — 7.8B ~20GB
      quant4=True       QLoRA 4-bit(bitsandbytes) — 32B ~25GB, LoRA 강제
    pooling = 마지막 비-pad 토큰."""
    def __init__(self, name, revision, *, full=True, quant4=False, rank=8, alpha=16):
        super().__init__()
        kw = dict(trust_remote_code=True, revision=revision)
        if quant4:
            from transformers import BitsAndBytesConfig
            kw["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
            kw["device_map"] = {"": 0}
        else:
            kw["torch_dtype"] = torch.bfloat16
        base = AutoModelForCausalLM.from_pretrained(name, **kw)
        if full and not quant4:
            self.enc = base                     # 전체 파인튜닝
        else:
            from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
            if quant4:
                base = prepare_model_for_kbit_training(base)
            targets = sorted({n.split(".")[-1] for n, m in base.named_modules()
                              if isinstance(m, nn.Linear) and n.endswith(ATTN)})
            self.enc = get_peft_model(base, LoraConfig(
                r=rank, lora_alpha=alpha, lora_dropout=0.05, target_modules=targets,
                bias="none", task_type="CAUSAL_LM"))
        self.head = nn.Sequential(nn.Dropout(0.1),
                                  nn.Linear(base.config.hidden_size, len(WORKERS))).to(torch.float32)

    def forward(self, ids, mask):
        hs = self.enc(input_ids=ids, attention_mask=mask, output_hidden_states=True).hidden_states[-1]
        last = mask.sum(1) - 1
        pooled = hs[torch.arange(hs.size(0), device=hs.device), last]
        return self.head(pooled.float())


@torch.no_grad()
def probs(model, rows, tok, uf, device, temp=None):
    model.eval()
    dl = DataLoader(T.DS(rows, tok, uf), batch_size=16)
    out, idx = {}, 0
    for ids, mask, _ in dl:
        lg = model(ids.to(device), mask.to(device))
        if temp is not None:
            lg = lg / torch.tensor(temp, device=device)
        for row in torch.sigmoid(lg).float().cpu().numpy():
            out[rows[idx]["id"]] = {m: float(row[j]) for j, m in enumerate(WORKERS)}
            idx += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="LGAI-EXAONE/EXAONE-4.0-1.2B")
    ap.add_argument("--revision", default=None)  # EXAONE-4.0=native(main). 3.5 pin은 transformers 5.13 비호환
    ap.add_argument("--features", type=int, default=1)
    ap.add_argument("--dep", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--bs", type=int, default=8)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--rank", type=int, default=8)
    ap.add_argument("--alpha", type=int, default=16)
    ap.add_argument("--full", type=int, default=1, help="1=full 파인튜닝, 0=LoRA")
    ap.add_argument("--quant4", type=int, default=0, help="1=QLoRA 4-bit(32B용, bitsandbytes)")
    ap.add_argument("--opt8bit", type=int, default=0, help="1=8-bit AdamW(7.8B full FT 메모리)")
    ap.add_argument("--tag", default="exaone2.4b")
    a = ap.parse_args()
    uf = bool(a.features)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    mode = "QLoRA-4bit" if a.quant4 else ("full-FT" if a.full else "LoRA")
    print(f"[env] {device} model={a.model} {mode} opt8bit={a.opt8bit} features={uf} dep={a.dep} seeds={a.seeds} tag={a.tag}")

    rows = [json.loads(l) for l in (DATA/"labeled2.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    import random
    ids = [r["id"] for r in rows]; random.Random(SEED).shuffle(ids)
    n = len(ids); trS, vaS = set(ids[:int(n*.7)]), set(ids[int(n*.7):int(n*.85)])
    tr = [r for r in rows if r["id"] in trS]; va = [r for r in rows if r["id"] in vaS]
    te = [r for r in rows if r["id"] not in trS and r["id"] not in vaS]
    hold = json.loads((GOLDEN/"t3_unseen_holdout.json").read_text(encoding="utf-8"))["items"]
    print(f"[data] train {len(tr)} val {len(va)} test {len(te)} holdout {len(hold)}")

    tok = AutoTokenizer.from_pretrained(a.model, trust_remote_code=True, revision=a.revision)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    ev, et, eh = [], [], []
    for s in range(a.seeds):
        T.set_seed(SEED+s)
        model = SelCausal(a.model, a.revision, full=bool(a.full), quant4=bool(a.quant4),
                          rank=a.rank, alpha=a.alpha)
        if a.quant4:
            model.head = model.head.to(device)   # 4-bit base는 device_map로 이미 GPU 배치
        else:
            model = model.to(device)
        dl = DataLoader(T.DS(tr, tok, uf), batch_size=a.bs, shuffle=True,
                        generator=torch.Generator().manual_seed(SEED+s))
        params = [p for p in model.parameters() if p.requires_grad]
        if a.opt8bit:
            import bitsandbytes as bnb
            opt = bnb.optim.AdamW8bit(params, lr=a.lr, weight_decay=0.01)
        else:
            opt = torch.optim.AdamW(params, lr=a.lr, weight_decay=0.01)
        best_state, best_v = None, -1
        for ep in range(1, a.epochs+1):
            model.train()
            for iid, mask, y in dl:
                opt.zero_grad()
                lg = model(iid.to(device), mask.to(device))
                loss = T.dep_loss(lg, y.to(device)) if a.dep else \
                    nn.functional.binary_cross_entropy_with_logits(lg, y.to(device))
                loss.backward(); opt.step()
            vc = T.acc_labeled(va, probs(model, va, tok, uf, device), 0.0)
            if vc >= best_v:
                best_v, best_state = vc, {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        model.load_state_dict(best_state, strict=False)
        temp = _temp(model, va, tok, uf, device)  # tier3 전용(LoRA probs) — T.tune_temp는 tier2 probs라 재사용 불가
        ev.append(probs(model, va, tok, uf, device, temp))
        et.append(probs(model, te, tok, uf, device, temp))
        eh.append(probs(model, hold, tok, uf, device, temp))
        print(f"  seed {s}: val(argmax) {best_v:.1f} temp={temp[0]}")

    def avg(maps, keys):
        return {k: {m: float(np.mean([mp[k][m] for mp in maps])) for m in WORKERS} for k in keys}
    vp = avg(ev, [r["id"] for r in va]); tp = avg(et, [r["id"] for r in te]); hp = avg(eh, [it["id"] for it in hold])
    tau = max([0.0, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3], key=lambda t: (T.acc_labeled(va, vp, t), t))
    b = sum(r[f"correct_{T.STATIC}"] for r in te)/len(te)*100
    o = sum(1 for r in te if any(r[f"correct_{m}"] for m in WORKERS))/len(te)*100
    print(f"\n== 합성 test (n={len(te)}) ==")
    print(f"  static {b:.1f} · argmax {T.acc_labeled(te,tp,0.0):.1f} · margin(tau={tau}) {T.acc_labeled(te,tp,tau):.1f} · oracle {o:.1f}")
    choice = {it["id"]: T.pick_margin(hp[it["id"]], tau) for it in hold}
    (DATA/f"sel_choice_holdout_{a.tag}.json").write_text(json.dumps(
        {"tau": tau, "features": uf, "dep": bool(a.dep), "seeds": a.seeds, "backbone": a.model,
         "choice": choice}, ensure_ascii=False), encoding="utf-8")
    print(f"  holdout 선택 분포 {dict(Counter(choice.values()))} → sel_choice_holdout_{a.tag}.json")


def _temp(model, va, tok, uf, device):
    raw = probs(model, va, tok, uf, device)
    y = {r["id"]: [r[f"correct_{m}"] for m in WORKERS] for r in va}
    best_t, best_nll = 1.0, 1e18
    for t in [0.5, 0.75, 1.0, 1.5, 2.0, 3.0]:
        nll = 0.0
        for i, yy in y.items():
            for j, m in enumerate(WORKERS):
                p = min(max(raw[i][m]**(1.0/t), 1e-6), 1-1e-6)
                nll -= yy[j]*np.log(p) + (1-yy[j])*np.log(1-p)
        if nll < best_nll:
            best_nll, best_t = nll, t
    return [best_t]*len(WORKERS)


if __name__ == "__main__":
    main()
