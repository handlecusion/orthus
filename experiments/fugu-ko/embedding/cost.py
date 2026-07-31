"""임베딩 원가 비교: OpenAI(text-embedding-3-small) vs Solar(Upstage Embed).

**회사 데이터를 쓰지 않는다.** 토큰 수는 내용 의미가 아니라 **문자 구성**에 달렸으므로,
실제 워크로드의 **길이 + 문자 구성비**만 맞춘 합성 텍스트로 잰다.

⚠️ 이 실험의 핵심 함정 — 순한글로 재면 틀린다:
  orthus company wiki_chunk의 실제 구성은 **한글 15.9% / 영문 48.2% / 숫자 7.1% /
  공백 18.1% / 기타 10.7%** (3,000청크 43만자 집계, 내용 미반출). 기술 문서라 영어가
  압도적이다. 순한글 문장으로 재면 Solar 토크나이저 이점이 **과대평가**된다 —
  영어는 두 토크나이저가 비슷하게 처리하기 때문이다. 그래서 두 조건을 모두 잰다:
    (A) 순한글        — 과대평가 조건(대조군)
    (B) 실제 구성비 매칭 — 진짜 워크로드

단가 (2026-07-16 확인, `README.md` §7 근거):
  OpenAI text-embedding-3-small : $0.02 / 1M tok  (Batch API $0.01)
  Upstage Embed                 : $0.10 / 1M tok  (VAT 10% 별도 — 미반영)
  → Upstage가 **토큰당 5배 비싸다.** 토크나이저 효율이 그걸 뒤집는지가 이 실험의 질문.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics as st

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS_PATH = os.environ.get(
    "FUGU_KEYS", "<로컬 키 저장소>/keys.json"
)
SOLAR_URL = "https://api.upstage.ai/v1/embeddings"

# --- 단가 (벤더 공시. 추정 대입 금지 — e5-price-sources.md 관례) ---
PRICE_OPENAI_PER_1M = 0.02   # text-embedding-3-small standard
PRICE_OPENAI_BATCH_PER_1M = 0.01
PRICE_SOLAR_PER_1M = 0.10    # Upstage "Embed", VAT 별도
VAT = 0.10

# --- 실제 워크로드 (DB 집계 실측) ---
CHUNK_CHARS = 210
QUERY_CHARS = 34
TOTAL_CHUNKS = 33_570        # company 10,153 + personal 23,417
REAL_COMPOSITION = {"한글": 0.159, "영문": 0.482, "숫자": 0.071, "공백": 0.181, "기타": 0.107}

_KO = "담당자는 배포 일정을 조율하고 검토 의견을 정리해 공유하기로 했다 지표는 주간 단위로 집계하며 이상치가 보이면 원인을 분석한다"
_EN = ("deploy pipeline review request latency threshold rollback config schema migration "
       "endpoint retry backoff cache index query embedding vector search ranking service "
       "worker queue batch token limit timeout session commit branch release build test")
_PUNCT = ".,()[]/:-—·"


def synth_korean(n: int, rng: random.Random) -> str:
    """(A) 순한글 — 과대평가 조건."""
    src = _KO
    off = rng.randrange(len(src))
    return ((src + " ") * 20)[off : off + n]


def synth_matched(n: int, rng: random.Random) -> str:
    """(B) 실제 문자 구성비를 맞춘 합성. 한글 토막 + 영문 토막 + 숫자 + 구두점을 섞는다."""
    ko_w = _KO.split()
    en_w = _EN.split()
    out: list[str] = []
    cur = 0
    while cur < n:
        r = rng.random()
        if r < 0.52:                       # 영문 우세 (실제 48%)
            t = rng.choice(en_w)
        elif r < 0.70:                     # 한글
            t = rng.choice(ko_w)
        elif r < 0.85:                     # 숫자/식별자
            t = rng.choice([str(rng.randrange(1, 9999)), f"{rng.randrange(1,300)}ms",
                            f"2026-07-{rng.randrange(10,29)}", f"v0.1.{rng.randrange(1,99)}",
                            f"{rng.randrange(1,99)}%"])
        else:                              # 구두점
            t = rng.choice(list(_PUNCT))
        out.append(t)
        cur += len(t) + 1
    return " ".join(out)[:n]


def composition(s: str) -> dict:
    tot = len(s) or 1
    c = {"한글": 0, "영문": 0, "숫자": 0, "공백": 0, "기타": 0}
    for ch in s:
        if "가" <= ch <= "힣" or "ㄱ" <= ch <= "ㅎ":
            c["한글"] += 1
        elif ch.isdigit():
            c["숫자"] += 1
        elif ch.isascii() and ch.isalpha():
            c["영문"] += 1
        elif ch.isspace():
            c["공백"] += 1
        else:
            c["기타"] += 1
    return {k: v / tot for k, v in c.items()}


def load_solar_key() -> str:
    raw = json.load(open(KEYS_PATH, encoding="utf-8"))
    return next(e for e in raw if "upstage" in str(e.get("provider", "")).lower())["key"]


def tokens_openai(client: httpx.Client, texts: list[str]) -> int:
    r = client.post(
        f"{os.environ['ORTHUS_EMBEDDING_BASE_URL'].rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {os.environ['ORTHUS_EMBEDDING_API_KEY']}"},
        json={"model": os.environ["ORTHUS_EMBEDDING_MODEL"], "input": texts, "dimensions": 1024},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["usage"]["prompt_tokens"]


def tokens_solar(client: httpx.Client, texts: list[str], key: str, model: str) -> int:
    r = client.post(
        SOLAR_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "input": texts, "dimensions": 1024},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["usage"]["prompt_tokens"]


def main() -> None:
    ap = argparse.ArgumentParser(description="임베딩 원가/토크나이저 효율 (합성 텍스트)")
    ap.add_argument("--n", type=int, default=30, help="조건별 표본 수")
    ap.add_argument("--seed", type=int, default=20260716)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    key = load_solar_key()
    results: dict = {}

    conditions = [
        ("A. 순한글(과대평가 조건)", synth_korean),
        ("B. 실제 구성비 매칭", synth_matched),
    ]

    with httpx.Client() as client:
        for label, gen in conditions:
            texts = [gen(CHUNK_CHARS, rng) for _ in range(args.n)]
            comp = composition("".join(texts))
            o = tokens_openai(client, texts)
            s = tokens_solar(client, texts, key, "embedding-passage")
            results[label] = {"openai": o, "solar": s, "ratio": s / o, "comp": comp}
            print(f"\n[{label}]  {args.n}건 × {CHUNK_CHARS}자")
            print("  구성: " + "  ".join(f"{k} {v:.1%}" for k, v in comp.items()))
            print(f"  토큰: OpenAI {o:,}  Solar {s:,}  → Solar/OpenAI = {s/o:.2f}배")

    print("\n" + "=" * 78)
    print("실제 워크로드 구성비 대조 (DB 3,000청크 집계):")
    print("  실측: " + "  ".join(f"{k} {v:.1%}" for k, v in REAL_COMPOSITION.items()))
    b = results["B. 실제 구성비 매칭"]
    print("  합성: " + "  ".join(f"{k} {v:.1%}" for k, v in b["comp"].items()))

    a_ratio = results["A. 순한글(과대평가 조건)"]["ratio"]
    b_ratio = b["ratio"]
    print(f"\n토크나이저 효율 — 조건에 따라 결론이 갈린다:")
    print(f"  순한글       Solar가 OpenAI의 {a_ratio:.2f}배 토큰  ({'절약' if a_ratio<1 else '더 씀'})")
    print(f"  실제 구성비  Solar가 OpenAI의 {b_ratio:.2f}배 토큰  ({'절약' if b_ratio<1 else '더 씀'})")
    print(f"  → 순한글로 재면 이점을 {(1/a_ratio)/(1/b_ratio):.2f}배 과대평가한다.")

    # --- 원가 ---
    print("\n" + "=" * 78)
    print("원가 (실제 구성비 기준)")
    tok_per_chunk_o = b["openai"] / args.n
    tok_per_chunk_s = b["solar"] / args.n
    print(f"  청크당 토큰: OpenAI {tok_per_chunk_o:.1f}  Solar {tok_per_chunk_s:.1f}")

    def usd(tok_total: float, per_1m: float, vat: float = 0.0) -> float:
        return tok_total / 1e6 * per_1m * (1 + vat)

    reembed_o = usd(TOTAL_CHUNKS * tok_per_chunk_o, PRICE_OPENAI_PER_1M)
    reembed_s = usd(TOTAL_CHUNKS * tok_per_chunk_s, PRICE_SOLAR_PER_1M, VAT)
    print(f"\n  1회성 전량 재임베딩 ({TOTAL_CHUNKS:,}청크):")
    print(f"    OpenAI ${reembed_o:.3f}   Solar ${reembed_s:.3f} (VAT 포함)")
    print(f"    → 교체 마이그레이션 원가는 **{'무시 가능' if reembed_s < 1 else f'${reembed_s:.2f}'}**")

    # --- 고정 오버헤드 측정 (비대칭 instruction prefix) ---
    # Solar의 query/passage 모델은 내부적으로 instruction을 앞에 붙인다. 1글자를 넣어도
    # 토큰이 남는 만큼이 그 오버헤드다. orthus 질의는 34자로 짧아서 이게 지배적이다.
    print("\n" + "=" * 78)
    print("고정 오버헤드 (비대칭 instruction prefix)")
    with httpx.Client() as client:
        ov_q = tokens_solar(client, ["a"], key, "embedding-query") - 1
        ov_p = tokens_solar(client, ["a"], key, "embedding-passage") - 1
        ov_o = tokens_openai(client, ["a"]) - 1
    print(f"  Solar embedding-query   : ~{ov_q} tok  ← 질의마다 무조건 붙는다")
    print(f"  Solar embedding-passage : ~{ov_p} tok")
    print(f"  OpenAI                  : ~{ov_o} tok")
    print(f"  → 34자 질의의 실제 내용은 ~4tok인데 오버헤드가 ~{ov_q}tok."
          f" **오버헤드가 내용의 {ov_q/4:.0f}배**다.")

    # 질의는 짧다 — 별도 측정
    with httpx.Client() as client:
        q = [synth_matched(QUERY_CHARS, rng) for _ in range(args.n)]
        qo = tokens_openai(client, q)
        qs = tokens_solar(client, q, key, "embedding-query")
        qs_sym = tokens_solar(client, q, key, "embedding-passage")  # 대칭 사용 시 비교
    qo_per, qs_per, qs_sym_per = qo / args.n, qs / args.n, qs_sym / args.n
    print(f"\n  질의 ({QUERY_CHARS}자): OpenAI {qo_per:.1f}tok  "
          f"Solar-query {qs_per:.1f}tok  (Solar-passage로 쓰면 {qs_sym_per:.1f}tok)")
    for n_q in (10_000, 100_000, 1_000_000):
        co = usd(n_q * qo_per, PRICE_OPENAI_PER_1M)
        cs = usd(n_q * qs_per, PRICE_SOLAR_PER_1M, VAT)
        print(f"    /ask {n_q:>9,}회:  OpenAI ${co:8.4f}   Solar ${cs:8.4f}   (Solar {cs/co:.1f}배)")

    print("\n" + "=" * 78)
    eff = (tok_per_chunk_s * PRICE_SOLAR_PER_1M * (1 + VAT)) / (tok_per_chunk_o * PRICE_OPENAI_PER_1M)
    print(f"""판정:

  1) 토크나이저 이점은 **우리 워크로드엔 없다.**
     순한글이면 Solar가 0.42배 토큰(2.4배 절약)이지만, 실제 company wiki는 영문 48%라
     실질 {b_ratio:.2f}배 — 이점이 소멸한다. 순한글로 재면 {(1/a_ratio)/(1/b_ratio):.1f}배 과대평가한다.

  2) 단가는 Upstage가 토큰당 **5배** 비싸다($0.10 vs $0.02).
     토크나이저가 그걸 {'뒤집는다' if eff < 1 else '뒤집지 못한다'} → 청크 실효 원가 **{eff:.1f}배**.

  3) 질의는 **instruction prefix가 지배한다.** embedding-query는 1글자에도 ~{ov_q}tok이
     붙어서 34자 질의가 Solar {qs_per:.0f}tok vs OpenAI {qo_per:.0f}tok = {qs_per/qo_per:.1f}배 →
     원가 {usd(1e6*qs_per, PRICE_SOLAR_PER_1M, VAT)/usd(1e6*qo_per, PRICE_OPENAI_PER_1M):.0f}배.

  **그러나 절대액이 무의미하다:** 전량 재임베딩 ${reembed_s:.2f}, /ask 100만회 ${usd(1e6*qs_per, PRICE_SOLAR_PER_1M, VAT):.2f}.
  → **원가는 이 결정의 변수가 아니다.** 배수는 크지만 금액이 커피 한 잔이다.
     (chat 슬롯과 결정적으로 다른 점: 임베딩은 출력 토큰이 없고 입력도 짧다.
      chat은 출력 토큰 3.6배가 실제 비용이었다 — docs/model-orchestration.md.)

주의:
  - Upstage "Embed" 단가는 pricing 페이지의 단일 행이다. embedding-query/passage 개별
    단가 표기는 없다 — 같은 단가로 가정했다.
  - VAT 10%는 Solar에만 반영(국내 사업자). OpenAI는 별도 처리.
  - OpenAI Batch API($0.01)면 재임베딩은 절반이나 이미 무시 가능하다.
  - 합성 구성비가 실측과 완전히 일치하진 않는다(한글 {b['comp']['한글']:.0%} vs 실측 15.9%).
    한글이 적을수록 Solar에 불리하므로 위 {b_ratio:.2f}배는 **Solar에 약간 비관적**이다.""")

    json.dump(
        {"conditions": {k: {kk: vv for kk, vv in v.items()} for k, v in results.items()},
         "prices": {"openai_per_1m": PRICE_OPENAI_PER_1M, "solar_per_1m": PRICE_SOLAR_PER_1M, "vat": VAT},
         "real_composition": REAL_COMPOSITION},
        open(f"{HERE}/cost_results.json", "w"), ensure_ascii=False, indent=2,
    )
    print(f"\n상세: {HERE}/cost_results.json")


if __name__ == "__main__":
    main()
