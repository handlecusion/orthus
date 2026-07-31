"""A.X-Encoder 인코딩 경로 — **학습과 채점이 공유한다**.

공정성 때문에 파일을 나눴다. ft arm과 zero-shot arm이 서로 다른 인코더 코드를 쓰면
두 arm의 차이가 '파인튜닝 효과'가 아니라 '풀링/정규화 구현 차이'까지 섞인다. 여기 있는
`encode()` 하나만 두 arm이 함께 쓰므로, 두 arm의 유일한 차이는 **가중치**다.

sentence-transformers를 쓰지 않는 이유:
  (1) A100은 공유 박스다 — `--target` 설치가 시스템 torch/transformers를 섀도잉할 수 있고
      `datasets`까지 딸려 온다(GPU 2·3에 타 사용자 작업 중).
  (2) MNRL은 in-batch softmax CE라 실제로 20줄이다. 절약분보다 위험이 크다.
  (3) 위 공정성 — ST를 쓰면 zero-shot arm만 손수 짜게 되어 경로가 갈린다.
"""
from __future__ import annotations

import torch
import torch.nn.functional as F

MODEL_ID = "skt/A.X-Encoder-base"
MAX_LEN = 256  # wiki 청크 실측 최대 420자 → 256 토큰이면 전량 수용(잘림 없음)


def mean_pool(last_hidden, attention_mask):
    """마스크 평균 풀링. MLM 백본은 [CLS]가 문장표현으로 학습된 적이 없어 평균이 표준이다."""
    m = attention_mask.unsqueeze(-1).to(last_hidden.dtype)
    return (last_hidden * m).sum(1) / m.sum(1).clamp(min=1e-9)


@torch.no_grad()
def encode(model, tok, texts: list[str], *, batch: int = 256, device: str = "cuda",
           max_len: int = MAX_LEN, progress: bool = False):
    """텍스트 → L2 정규화된 mean-pool 임베딩 (numpy float32).

    ft/zero-shot 두 arm이 이 함수를 그대로 공유한다.
    """
    model.eval()
    out = []
    for i in range(0, len(texts), batch):
        b = tok(texts[i : i + batch], padding=True, truncation=True,
                max_length=max_len, return_tensors="pt").to(device)
        h = model(**b).last_hidden_state
        e = F.normalize(mean_pool(h, b["attention_mask"]), dim=-1)
        out.append(e.float().cpu())
        if progress and i and (i // batch) % 20 == 0:
            print(f"    encode {i}/{len(texts)}", flush=True)
    return torch.cat(out).numpy()


def load(model_path: str = MODEL_ID, device: str = "cuda"):
    from transformers import AutoModel, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(MODEL_ID)
    model = AutoModel.from_pretrained(model_path).to(device)
    return model, tok
