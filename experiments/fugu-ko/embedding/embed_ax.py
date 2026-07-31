"""A100에서 평가 pool·질문을 A.X로 임베딩해 .npy로 떨군다 (ft / zero-shot 각각).

ax_encoder.encode 하나만 쓴다 — ft arm과 zs arm의 유일한 차이가 **가중치**여야 채점이
'파인튜닝 효과'를 재는 게 된다(공정성).

입력(로컬에서 scp): pool_texts.json · q_texts_{gen}.json
출력(로컬로 회수):  ax_pool_{ft,zs}.npy · ax_q_{ft,zs}_{gen}.npy

사용 (A100):
    export HF_HOME=/data/tta/hf-cache TMPDIR=/data/tta/tmp HF_HUB_OFFLINE=1
    CUDA_VISIBLE_DEVICES=1 python3 embed_ax.py --variant ft --model ckpt/ax-ft
    CUDA_VISIBLE_DEVICES=1 python3 embed_ax.py --variant zs
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ax_encoder import MODEL_ID, encode, load  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    ap = argparse.ArgumentParser(description="A.X로 평가 pool/질문 임베딩")
    # ft = 전체 pool로 학습(positive 74% claim) · ftp = page-only positive로 학습 · zs = 원본 백본
    ap.add_argument("--variant", required=True, choices=["ft", "ftp", "zs"])
    ap.add_argument("--model", default=None, help="ft/ftp면 체크포인트 경로. zs면 생략(원본 백본)")
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--datadir", default=HERE)
    args = ap.parse_args()

    path = args.model or MODEL_ID
    if args.variant in ("ft", "ftp") and not args.model:
        raise SystemExit(f"--variant {args.variant} 는 --model <ckpt> 가 필요하다")
    print(f"[{args.variant}] 모델: {path}")
    model, tok = load(path)

    pool = json.load(open(f"{args.datadir}/pool_texts.json", encoding="utf-8"))
    print(f"pool {len(pool)}건 임베딩...")
    pv = encode(model, tok, pool, batch=args.batch, progress=True)
    np.save(f"{args.datadir}/ax_pool_{args.variant}.npy", pv)
    print(f"  → ax_pool_{args.variant}.npy {pv.shape}")

    for p in sorted(glob.glob(f"{args.datadir}/q_texts_*.json")):
        gen = os.path.basename(p)[len("q_texts_") : -len(".json")]
        qs = json.load(open(p, encoding="utf-8"))
        qv = encode(model, tok, qs, batch=args.batch)
        np.save(f"{args.datadir}/ax_q_{args.variant}_{gen}.npy", qv)
        print(f"  → ax_q_{args.variant}_{gen}.npy {qv.shape}")


if __name__ == "__main__":
    main()
