"""D7 — SFT 워커 학습/생성 (A100). EXAONE-4.0을 (배포 프롬프트 → {"sql": ...})로 파인튜닝.

D6 선택기(tier2v/tier3v)와 다른 점: 3지선다 분류가 아니라 **SQL 생성**이다. 고르기의
oracle 천장(68%)에 묶이지 않는 유일한 학습 접근(prereg §1.2/§4-c2).

입력 데이터 = build_sft_data.py 산출(sft_train/val.jsonl, messages 포맷 — 배포 compile
프롬프트와 동일 + 스키마 증강). 라벨은 assistant 구간만(프롬프트 토큰 -100 마스킹).

실행(A100, /data/tta/fugu-ko):
  CUDA_VISIBLE_DEVICES=0 python train/sft_worker.py --train \
      --model LGAI-EXAONE/EXAONE-4.0-1.2B --out /data/tta/fugu-ko/train/ckpt/sft12b
  CUDA_VISIBLE_DEVICES=0 python train/sft_worker.py --gen data/sft_val.jsonl \
      --ckpt /data/tta/fugu-ko/train/ckpt/sft12b --out_file data/sft_val_gen.jsonl
32B는 --quant4 (QLoRA). 채점은 로컬(sft_score.py — DB 필요)."""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from pathlib import Path

import torch
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer

SEED = 42
MAX_LEN = 4096


def set_seed(s: int) -> None:
    random.seed(s)
    torch.manual_seed(s)
    torch.cuda.manual_seed_all(s)


def load_jsonl(p: str | Path) -> list[dict]:
    return [json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]


class SftData(Dataset):
    def __init__(self, rows: list[dict], tok):
        self.rows, self.tok = rows, tok

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        m = self.rows[i]["messages"]
        # transformers 5.x: tokenize=True 반환형이 버전에 따라 달라(BatchEncoding) 문자열 렌더
        # 후 직접 토크나이즈한다. 템플릿이 special token을 이미 포함하므로 add_special_tokens=False.
        prompt_txt = self.tok.apply_chat_template(m[:-1], add_generation_prompt=True, tokenize=False)
        full_txt = self.tok.apply_chat_template(m, tokenize=False)
        prompt_ids = self.tok(prompt_txt, add_special_tokens=False).input_ids
        full_ids = self.tok(full_txt, add_special_tokens=False).input_ids
        # 문자열로는 prefix지만 토큰 경계가 다를 수 있다("\n\n"+"A" 병합 토큰화, EXAONE-4.0 실측).
        # → 공통 토큰 prefix까지만 마스킹. 경계 1토큰이 라벨에 남는 것은 무해(타깃 첫 글자 포함).
        L = 0
        for a, b in zip(prompt_ids, full_ids):
            if a != b:
                break
            L += 1
        assert L >= len(prompt_ids) - 3, f"prefix diverges too early: {L}/{len(prompt_ids)}"
        ids = full_ids[:MAX_LEN]
        labels = [-100] * min(L, len(ids)) + ids[L:]
        labels = labels[: len(ids)]
        return {"ids": ids, "labels": labels}


def collate(batch, pad_id: int):
    n = max(len(b["ids"]) for b in batch)
    ids = torch.full((len(batch), n), pad_id, dtype=torch.long)
    lab = torch.full((len(batch), n), -100, dtype=torch.long)
    att = torch.zeros((len(batch), n), dtype=torch.long)
    for i, b in enumerate(batch):
        L = len(b["ids"])
        ids[i, :L] = torch.tensor(b["ids"])
        lab[i, :L] = torch.tensor(b["labels"])
        att[i, :L] = 1
    return ids, att, lab


def build_model(name: str, *, quant4: bool):
    kw: dict = dict(trust_remote_code=True)
    if quant4:
        from transformers import BitsAndBytesConfig

        kw["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
        kw["device_map"] = {"": 0}
    else:
        kw["torch_dtype"] = torch.bfloat16
    model = AutoModelForCausalLM.from_pretrained(name, **kw)
    if quant4:
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

        model = prepare_model_for_kbit_training(model)
        model = get_peft_model(
            model,
            LoraConfig(
                r=16, lora_alpha=32, lora_dropout=0.05, task_type="CAUSAL_LM",
                target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
            ),
        )
        model.print_trainable_parameters()
    return model


def train(a) -> None:
    set_seed(SEED)
    tok = AutoTokenizer.from_pretrained(a.model, trust_remote_code=True)
    pad_id = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id
    tr = SftData(load_jsonl(a.data), tok)
    va = SftData(load_jsonl(a.val_data), tok)
    model = build_model(a.model, quant4=a.quant4)
    if not a.quant4:
        model = model.cuda()
    model.gradient_checkpointing_enable()
    model.train()

    dl = DataLoader(tr, batch_size=a.bs, shuffle=True, collate_fn=lambda b: collate(b, pad_id))
    steps = math.ceil(len(dl) / a.accum) * a.epochs
    opt = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=a.lr, weight_decay=0.01)
    sch = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, s / max(1, int(steps * 0.03))) * 0.5 * (1 + math.cos(math.pi * min(1.0, s / steps)))
    )

    def val_loss() -> float:
        model.eval()
        tot, cnt = 0.0, 0
        with torch.no_grad():
            for ids, att, lab in DataLoader(va, batch_size=a.bs, collate_fn=lambda b: collate(b, pad_id)):
                out = model(input_ids=ids.cuda(), attention_mask=att.cuda(), labels=lab.cuda())
                tot += float(out.loss) * ids.size(0)
                cnt += ids.size(0)
        model.train()
        return tot / max(cnt, 1)

    t0 = time.monotonic()
    gstep = 0
    best = float("inf")
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    for ep in range(a.epochs):
        for i, (ids, att, lab) in enumerate(dl):
            out = model(input_ids=ids.cuda(), attention_mask=att.cuda(), labels=lab.cuda())
            (out.loss / a.accum).backward()
            if (i + 1) % a.accum == 0:
                torch.nn.utils.clip_grad_norm_((p for p in model.parameters() if p.requires_grad), 1.0)
                opt.step()
                sch.step()
                opt.zero_grad(set_to_none=True)
                gstep += 1
                if gstep % 20 == 0:
                    print(f"ep{ep} step{gstep}/{steps} loss={float(out.loss):.4f} ({(time.monotonic() - t0) / 60:.1f}m)", flush=True)
        vl = val_loss()
        print(f"[ep{ep}] val_loss={vl:.4f}", flush=True)
        if vl < best:
            best = vl
            model.save_pretrained(outdir)
            tok.save_pretrained(outdir)
            print(f"[ep{ep}] saved → {outdir}", flush=True)
    print(f"[train] done best_val={best:.4f} ({(time.monotonic() - t0) / 60:.1f}m)", flush=True)


@torch.no_grad()
def gen(a) -> None:
    tok = AutoTokenizer.from_pretrained(a.ckpt, trust_remote_code=True)
    if a.quant4:  # LoRA 어댑터 로드
        from peft import AutoPeftModelForCausalLM

        model = AutoPeftModelForCausalLM.from_pretrained(a.ckpt, torch_dtype=torch.bfloat16, trust_remote_code=True).cuda()
    else:
        model = AutoModelForCausalLM.from_pretrained(a.ckpt, torch_dtype=torch.bfloat16, trust_remote_code=True).cuda()
    model.eval()
    rows = load_jsonl(a.gen)
    out_path = Path(a.out_file)
    done_k: dict[str, set[int]] = {}
    if out_path.exists():
        for x in out_path.read_text(encoding="utf-8").splitlines():
            if x.strip():
                r = json.loads(x)
                done_k.setdefault(r["id"], set()).add(r.get("sample_i", 0))
    # 배치 생성(GPU 1장 제약 — T16). decoder-only는 **left padding**이라야 배치가 정답과
    # 동일하다(right padding이면 pad가 컨텍스트에 섞여 출력이 깨짐). greedy/온도 의미는
    # 1건씩 생성과 동일 — 샘플 0은 여전히 greedy, 1..k-1은 t0.7.
    tok.padding_side = "left"
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    pad_id = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id

    def prompt_txt(r: dict) -> str:
        m = r["messages"][:-1] if r["messages"][-1]["role"] == "assistant" else r["messages"]
        return tok.apply_chat_template(m, add_generation_prompt=True, tokenize=False)

    todo = [(r, si) for r in rows for si in range(a.k) if si not in done_k.get(r["id"], set())]
    # greedy(si==0)와 sample(si>0)은 **분리해서** 배치를 만든다 — 한 배치에 섞으면 do_sample을
    # 하나만 고를 수밖에 없다. (T18: 예전엔 섞인 배치에서 한쪽을 걸러내 그 항목이 통째로
    # 유실됐다 — 경계 배치에서 greedy 8건 누락. 이제 유실 0.)
    groups = [[x for x in todo if x[1] == 0], [x for x in todo if x[1] != 0]]
    for g in groups:  # 길이순 정렬 → padding 낭비 최소화(결과 무관, 속도만)
        g.sort(key=lambda x: len(prompt_txt(x[0])))
    batches = [(g[s : s + a.bs_gen], gi == 0) for gi, g in enumerate(groups) for s in range(0, len(g), a.bs_gen)]
    assert sum(len(b) for b, _ in batches) == len(todo), "배치 분할에서 항목 유실"
    t0 = time.monotonic()
    done_n = 0
    with out_path.open("a", encoding="utf-8") as f:
        for chunk, greedy in batches:
            enc = tok([prompt_txt(r) for r, _ in chunk], add_special_tokens=False,
                      return_tensors="pt", padding=True).to("cuda")
            kw = dict(max_new_tokens=220, pad_token_id=pad_id)
            kw.update(dict(do_sample=False) if greedy
                      else dict(do_sample=True, temperature=0.7, top_p=0.95))
            out = model.generate(**enc, **kw)
            new = out[:, enc["input_ids"].shape[1] :]
            for (r, si), o in zip(chunk, new):
                text = tok.decode(o, skip_special_tokens=True)
                f.write(json.dumps({"id": r["id"], "sample_i": si, "raw": text}, ensure_ascii=False) + "\n")
            f.flush()
            done_n += len(chunk)
            el = time.monotonic() - t0
            print(f"gen {done_n}/{len(todo)} ({el / done_n:.2f}s/콜, 남은 ~{el / done_n * (len(todo) - done_n) / 60:.0f}분)", flush=True)
    print(f"[gen] done → {out_path}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--gen", default=None, help="생성할 messages jsonl")
    ap.add_argument("--model", default="LGAI-EXAONE/EXAONE-4.0-1.2B")
    ap.add_argument("--ckpt", default=None)
    ap.add_argument("--data", default="train/data/sft_train.jsonl")
    ap.add_argument("--val_data", default="train/data/sft_val.jsonl")
    ap.add_argument("--out", default="/data/tta/fugu-ko/train/ckpt/sft12b")
    ap.add_argument("--out_file", default="train/data/sft_val_gen.jsonl")
    ap.add_argument("--quant4", action="store_true")
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--bs_gen", type=int, default=16, help="생성 배치 크기 (GPU 1장 처리량)")
    ap.add_argument("--bs", type=int, default=2)
    ap.add_argument("--accum", type=int, default=8)
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--lr", type=float, default=1e-5)
    a = ap.parse_args()
    if a.train:
        train(a)
    elif a.gen:
        gen(a)
    else:
        raise SystemExit("--train 또는 --gen 필요")


if __name__ == "__main__":
    main()
