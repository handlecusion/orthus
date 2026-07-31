"""E5 계측 배선 자체검증 — 실 API 0콜(`_post_json` 스텁). 본실행 전/후 아무 때나 돌린다.

계획서 §4.1이 요구하는 계측 계약을 고정한다:
1. usage **누적**(항목당 2콜이면 합산). 단일 last_usage 필드였다면 과소계상될 케이스.
2. 항목 경계 `reset_usage()`.
3. fast-path(LLM 미도달) → `n_llm_calls=0`, 토큰 0 = **원가 0 실측**(결측 아님).
4. usage 결측(콜은 했는데 벤더가 usage 미제공) → `n_usage_missing>0`으로 3과 구별.
5. `harness.run_with_usage`가 러너 결과 dict에 필드를 병기.

실행: PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/e5_selfcheck.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

import harness  # noqa: E402
import pool  # noqa: E402

_CALLS: list[str] = []


def _fake_post_json(base, path, key, body, timeout):  # noqa: ANN001, ARG001
    _CALLS.append(body["model"])
    n = len(_CALLS)
    if n == 3:  # 3번째 콜만 usage 미제공 벤더를 흉내
        return {"choices": [{"message": {"content": "ok"}}]}
    return {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 100 * n, "completion_tokens": 10 * n},
    }


def main() -> None:
    pool._post_json = _fake_post_json  # noqa: SLF001
    chat = pool.WorkerChat(pool.WorkerSpec("fake", "fake", "https://x/v1", "m"), "k", "m")

    chat.reset_usage()
    chat.complete("s", "u")
    chat.complete("s", "u")
    got = chat.usage_totals()
    assert got == {"n_llm_calls": 2, "in_tok": 300, "out_tok": 30, "n_usage_missing": 0}, got
    print(f"[1,2] 항목당 2콜 누적 합산  {got}")

    chat.reset_usage()
    chat.complete("s", "u")  # usage 미제공 콜
    got = chat.usage_totals()
    assert got == {"n_llm_calls": 1, "in_tok": 0, "out_tok": 0, "n_usage_missing": 1}, got
    print(f"[4]   usage 결측 콜 식별   {got}")

    chat.reset_usage()
    got = chat.usage_totals()
    assert got == {"n_llm_calls": 0, "in_tok": 0, "out_tok": 0, "n_usage_missing": 0}, got
    print(f"[3]   fast-path=원가 0 실측 {got}")

    def runner(ch, item):  # noqa: ANN001, ANN202
        ch.complete("s", "u")
        return {"id": item["id"], "ms": 5}

    r1 = harness.run_with_usage(runner, chat, {"id": "q1"})
    r2 = harness.run_with_usage(runner, chat, {"id": "q2"})
    assert r1["n_llm_calls"] == 1 and r2["n_llm_calls"] == 1, (r1, r2)
    assert r1["in_tok"] != r2["in_tok"], "항목 경계 reset 실패(직전 항목 토큰 누수)"
    print(f"[5]   harness 병기 + 항목 경계 reset  {r1} {r2}")

    print("\nE5 계측 자체검증 PASS (실 API 0콜)")


if __name__ == "__main__":
    main()
