"""A.X-Encoder-base 대조학습 파인튜닝 (MultipleNegativesRankingLoss).

MNRL = in-batch softmax cross-entropy. 배치 안의 다른 정답들이 자동으로 네거티브가 되고,
거기에 gen_train_data.py가 뽑은 하드 네거티브를 얹는다.

    sims   = scale · cos(q_i, [pos_0..pos_B-1, neg_0..neg_{B·n-1}])
    labels = i                      (i번 질문의 정답은 pos_i)
    loss   = CE(sims, labels)

인코딩은 ax_encoder.encode/mean_pool을 쓴다 — **채점 때 zero-shot arm과 같은 경로**여야
두 arm의 차이가 가중치뿐이 된다(공정성).

dev MRR은 학습에서 뺀 300쌍으로 잰다. 채점(Step 4)은 별도 평가셋으로 하고, 여기 dev는
"학습이 조용히 망가지지 않았나"를 보는 계기판일 뿐이다.

사용 (A100):
    export HF_HOME=/data/tta/hf-cache TMPDIR=/data/tta/tmp HF_HUB_OFFLINE=1
    CUDA_VISIBLE_DEVICES=1 python3 train_ax.py --pairs train_pairs.jsonl --out ckpt/ax-ft
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

import torch
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ax_encoder import MAX_LEN, MODEL_ID, encode, mean_pool  # noqa: E402

SEED = 20260717


def set_seed(s: int) -> None:
    random.seed(s)
    torch.manual_seed(s)
    torch.cuda.manual_seed_all(s)


def embed_grad(model, tok, texts, device, max_len):
    """학습용 인코딩(그래디언트 유지). ax_encoder.encode는 no_grad라 여기선 못 쓴다 —
    풀링/정규화 수식은 mean_pool을 공유해 채점 경로와 동일하게 맞춘다."""
    b = tok(texts, padding=True, truncation=True, max_length=max_len, return_tensors="pt").to(device)
    h = model(**b).last_hidden_state
    return F.normalize(mean_pool(h, b["attention_mask"]), dim=-1)


@torch.no_grad()
def dev_mrr(model, tok, dev, device) -> float:
    """dev 질문 → dev 정답 전체(=풀) 안에서의 MRR. 학습 진행의 계기판."""
    q = encode(model, tok, [d["q"] for d in dev], device=device)
    p = encode(model, tok, [d["pos"] for d in dev], device=device)
    sims = torch.from_numpy(q) @ torch.from_numpy(p).T
    ranks = (sims > sims.diag().unsqueeze(1)).sum(1) + 1
    return float((1.0 / ranks.float()).mean())


def main() -> None:
    ap = argparse.ArgumentParser(description="A.X-Encoder 대조학습 파인튜닝")
    ap.add_argument("--pairs", default="train_pairs.jsonl")
    ap.add_argument("--out", default="ckpt/ax-ft")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--negs", type=int, default=4, help="쌍당 하드 네거티브 수(0=in-batch만)")
    ap.add_argument("--scale", type=float, default=20.0, help="코사인 온도(ST 기본값)")
    ap.add_argument("--dev", type=int, default=300)
    ap.add_argument("--max-len", type=int, default=MAX_LEN)
    args = ap.parse_args()

    set_seed(SEED)
    device = "cuda"
    pairs = [json.loads(l) for l in open(args.pairs, encoding="utf-8")]
    random.Random(SEED).shuffle(pairs)
    dev, train = pairs[: args.dev], pairs[args.dev :]
    print(f"학습 {len(train)} / dev {len(dev)} | batch {args.batch} · {args.epochs}ep · "
          f"lr {args.lr} · negs {args.negs} · scale {args.scale}")

    from transformers import AutoModel, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(MODEL_ID)
    model = AutoModel.from_pretrained(MODEL_ID).to(device)
    print(f"모델: {sum(p.numel() for p in model.parameters()) / 1e6:.1f}M params")

    base = dev_mrr(model, tok, dev, device)
    print(f"[dev MRR] 파인튜닝 전(zero-shot): {base:.4f}")

    steps = (len(train) // args.batch) * args.epochs
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=args.lr, total_steps=steps, pct_start=0.1, anneal_strategy="linear"
    )

    step = 0
    for ep in range(args.epochs):
        model.train()
        random.Random(SEED + ep).shuffle(train)
        run, nb = 0.0, 0
        for i in range(0, len(train) - args.batch + 1, args.batch):
            b = train[i : i + args.batch]
            docs = [d["pos"] for d in b]
            for d in b:
                docs.extend(d["negs"][: args.negs])

            with torch.autocast("cuda", dtype=torch.bfloat16):
                qv = embed_grad(model, tok, [d["q"] for d in b], device, args.max_len)
                dv = embed_grad(model, tok, docs, device, args.max_len)
                # 정답 i는 docs[i] — labels가 대각이다. 나머지 전부(다른 정답 + 하드 네거)가 네거티브.
                loss = F.cross_entropy(
                    args.scale * (qv.float() @ dv.float().T),
                    torch.arange(len(b), device=device),
                )
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            run += float(loss)
            nb += 1
            step += 1
            if nb % 20 == 0:
                print(f"  ep{ep + 1} {nb}/{len(train) // args.batch} loss {run / nb:.4f}", flush=True)
        d = dev_mrr(model, tok, dev, device)
        print(f"[ep{ep + 1}] train loss {run / max(nb, 1):.4f} | dev MRR {d:.4f} (zero-shot {base:.4f})")

    os.makedirs(args.out, exist_ok=True)
    model.save_pretrained(args.out)
    tok.save_pretrained(args.out)
    print(f"\n저장: {args.out}")
    print(f"dev MRR: {base:.4f}(전) → {dev_mrr(model, tok, dev, device):.4f}(후)")


if __name__ == "__main__":
    main()
