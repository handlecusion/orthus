"""D7 파일럿 — solar k-샘플 캡처(SQL + 실행결과) on **학습셋**.

D6 병목("추론시점에 어느 워커가 틀릴지 모름")은 *질문만 보는* 사전 라우팅에만 성립한다.
실행 후에는 (질문, SQL, 결과)가 있고, 여기서 solar의 실패는 **SQL 모양**으로 드러난다.
관측된 주 실패 모드(2026-07-16, 홀드아웃 탐색):

    Q "파트너사에는 몇 개나 들어 있어?" (총계 질문)
    solar SQL: SELECT properties->>'이름', COUNT(*) ... GROUP BY 1   ← 군더더기 GROUP BY
    → 이름별 1씩 = [1]  (정답 11)
    ax    SQL: SELECT COUNT(*) ...                                   → [11] 정답

이 신호는 **DB 내용이 아니라 (질문 의도 ↔ SQL 모양) 정합성**이라 새 DB로 전이된다 —
D6가 못 배운 이유(질문 텍스트에 정보 없음)를 정면으로 우회한다.

또 하나: temperature=0인데도 solar 재실행 결과가 흔들린다(홀드아웃 t3u-188: 동결 [0] →
재실행 [9]). 흔들림이 크면 **k-샘플 자기일관성(실행결과 기준 다수결)**이 바로 이득이 된다.

이 스크립트는 그 둘을 학습셋에서 정량화한다(홀드아웃 무접촉 = 판정셋 오염 방지).
학습셋 DB(NOVA 영입 후보/협업업무표/로드맵/배포공지/AI관련툴/미팅기록)와 홀드아웃 DB
(참고문서/파트너사/아틀라스링크/주간노트/직원/파트너)는 서로 겹치지 않는다.

실행(로컬, node.env + FUGU_KEYS + 0706 DSN 3종):
  python train/sc_pilot.py --n 150 --k 5              # 캡처(재개 지원)
  python train/sc_pilot.py --analyze                  # 캡처본 분석만(API 0콜)
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from pool import build_pool  # noqa: E402
from t3_gold import model_numbers  # noqa: E402
from orthus.structured.query import query_structured  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
DATA = HERE / "data"
OUT = DATA / "sc_pilot_solar.jsonl"
SEED = 42


class TempChat:
    """WorkerChat 위임 래퍼 — temperature만 바꿔 k-샘플을 뽑는다.

    pool.WorkerChat.complete()는 temperature=0 하드코딩이라 k-샘플이 전부 같은 요청이 된다
    (그래도 벤더 비결정성으로 흔들리지만, 다양성을 의도적으로 만들려면 온도가 필요하다).
    프로덕션/기존 실험 코드는 건드리지 않고 여기서만 감싼다.
    """

    def __init__(self, inner, temperature: float):
        self._inner = inner
        self._t = temperature
        self.model_id = inner.model_id
        self.last_error: str | None = None

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        from orthus.models.adapters.openai_compat import _post_json

        w = self._inner
        body: dict = {
            "model": w._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._t,
        }
        if json_only:
            body["response_format"] = {"type": "json_object"}
        body.update(w._spec.extra_body)
        w._throttle()
        data = _post_json(w._spec.base_url, "/chat/completions", w._key, body, w._spec.timeout)
        content = data["choices"][0]["message"]["content"]
        if content is None:
            raise RuntimeError(f"{self.model_id}: null content")
        return content


def _query_retry(chat, q: str, tries: int = 4):
    """DB 연결 풀 고갈(OperationalError) 재시도 — vote_holdout.py `_query_retry` 패턴 재사용."""
    from orthus.db import get_engine, get_ro_engine

    last = None
    for a in range(tries):
        try:
            return query_structured(UID, q, scope="company", chat_model=chat), None
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
            if "Operational" not in last and "Operational" not in str(e):
                return None, f"{last}: {str(e)[:60]}"
        try:
            get_ro_engine().dispose()
            get_engine().dispose()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0 * (a + 1))
    return None, last


def sample_items(n: int) -> list[dict]:
    rows = [json.loads(x) for x in (DATA / "labeled2.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    random.Random(SEED).shuffle(rows)
    return rows[:n]


def capture(n: int, k: int, temperature: float) -> None:
    items = sample_items(n)
    done: set[str] = set()
    if OUT.exists():
        done = {json.loads(x)["id"] for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()}
    todo = [it for it in items if it["id"] not in done]
    print(f"[capture] 표본 {len(items)} / 완료 {len(done)} / 남음 {len(todo)} · k={k} temp={temperature}", flush=True)
    if not todo:
        return
    base = build_pool(["solar"])["solar"]
    # 샘플 0 = temperature 0 (현행 프로덕션과 동일 조건), 샘플 1..k-1 = temperature>0 다양화
    chats = [TempChat(base, 0.0)] + [TempChat(base, temperature) for _ in range(k - 1)]
    t0 = time.monotonic()
    with OUT.open("a", encoding="utf-8") as f:
        for n_i, it in enumerate(todo, 1):
            samples = []
            for chat in chats:
                r, err = _query_retry(chat, it["q"])
                if r is None:
                    samples.append({"status": f"err:{err}", "sql": None, "nums": []})
                    continue
                samples.append(
                    {
                        "status": r.status,
                        "sql": r.compiled.sql if r.compiled else None,
                        "nums": sorted(model_numbers(r.rows)) if r.status == "executed" else [],
                        "reject": r.validation.rejected_reason,
                    }
                )
            f.write(
                json.dumps(
                    {"id": it["id"], "q": it["q"], "tag": it["tag"], "spec": it["spec"], "gold": it["gold"], "samples": samples},
                    ensure_ascii=False,
                )
                + "\n"
            )
            f.flush()
            if n_i % 10 == 0:
                el = time.monotonic() - t0
                print(f"  {n_i}/{len(todo)} ({el / n_i:.1f}s/문항, 남은 ~{el / n_i * (len(todo) - n_i) / 60:.0f}분)", flush=True)
    print(f"[capture] 완료 → {OUT} ({(time.monotonic() - t0) / 60:.1f}분)", flush=True)


# ── 결정론 신호 (LLM 0회) ───────────────────────────────────────────────────
COUNT_TERMS = ("몇 개", "몇 건", "몇 명", "개수", "총 몇", "몇 개나", "얼마나", "규모가", "세어", "총계")
BREAKDOWN_TERMS = ("별로", "별 ", "각각", "나눠", "분포", "그룹", "종류별", "구분별", "따로")


def is_total_count_question(q: str) -> bool:
    """'총 몇 개' 류 = 단일 숫자 하나를 기대하는 질문 (분해 요구 없음)."""
    return any(t in q for t in COUNT_TERMS) and not any(t in q for t in BREAKDOWN_TERMS)


def has_groupby(sql: str | None) -> bool:
    return bool(sql) and "GROUP BY" in sql.upper()


def null_answer(s: dict) -> bool:
    """구조적 오답 — 에러/빈 결과/{0}. gold에 0·빈셋이 없으므로 위양성 0(§d7 §1.4)."""
    if s["status"] != "executed" or not s["nums"]:
        return True
    return set(s["nums"]) == {0}


def analyze() -> None:
    rows = [json.loads(x) for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()]
    n = len(rows)
    print(f"\n══ D7 파일럿 분석 (학습셋 표본 n={n}) ══")

    def ok(nums, gold) -> bool:
        return bool(gold) and set(gold) <= set(nums)

    # 1) 비결정성 — k 샘플이 서로 다른가
    flaky = sum(1 for r in rows if len({tuple(s["nums"]) for s in r["samples"]}) > 1)
    t0_flaky = sum(1 for r in rows if len({tuple(s["nums"]) for s in r["samples"][:1]}) > 1)
    print(f"\n[1] 비결정성: k샘플 결과가 갈리는 문항 {flaky}/{n} ({flaky / n * 100:.0f}%)  (t0 자체 흔들림 {t0_flaky})")

    # 2) 단일(t0, 현행) vs 자기일관성 다수결
    single = sum(1 for r in rows if ok(r["samples"][0]["nums"], r["gold"]))

    def sc_vote(r: dict) -> list:
        valid = [s for s in r["samples"] if not null_answer(s)]
        pool_ = valid or r["samples"]
        c = Counter(tuple(s["nums"]) for s in pool_)
        return list(c.most_common(1)[0][0])

    sc = sum(1 for r in rows if ok(sc_vote(r), r["gold"]))
    print(f"\n[2] 정답률: 단일(t0, 현행) {single / n * 100:.1f}%  →  자기일관성 다수결 {sc / n * 100:.1f}%  ({(sc - single) / n * 100:+.1f}p)")

    # 3) SQL 모양 신호 — 총계 질문인데 GROUP BY
    fire = [r for r in rows if is_total_count_question(r["q"]) and has_groupby(r["samples"][0]["sql"])]
    wrong = [r for r in fire if not ok(r["samples"][0]["nums"], r["gold"])]
    prec = len(wrong) / len(fire) * 100 if fire else 0.0
    allwrong = [r for r in rows if not ok(r["samples"][0]["nums"], r["gold"])]
    rec = len(wrong) / len(allwrong) * 100 if allwrong else 0.0
    print(f"\n[3] SQL모양 신호 '총계질문 + GROUP BY': 발동 {len(fire)}, 그중 오답 {len(wrong)} → 정밀도 {prec:.0f}% · 전체오답 중 재현율 {rec:.0f}%")

    # 4) null 신호(에러/빈/0)
    nfire = [r for r in rows if null_answer(r["samples"][0])]
    nwrong = [r for r in nfire if not ok(r["samples"][0]["nums"], r["gold"])]
    print(f"[4] null 신호(에러/빈/0): 발동 {len(nfire)}, 그중 오답 {len(nwrong)} → 정밀도 {len(nwrong) / len(nfire) * 100 if nfire else 0:.0f}%")

    # 5) 두 신호 합집합의 탐지력
    det = [r for r in rows if null_answer(r["samples"][0]) or (is_total_count_question(r["q"]) and has_groupby(r["samples"][0]["sql"]))]
    dwrong = [r for r in det if not ok(r["samples"][0]["nums"], r["gold"])]
    print(f"[5] 두 신호 합집합: 발동 {len(det)}, 오답 {len(dwrong)} → 정밀도 {len(dwrong) / len(det) * 100 if det else 0:.0f}% · 전체오답 {len(allwrong)}건 중 {len(dwrong)}건 탐지({len(dwrong) / len(allwrong) * 100 if allwrong else 0:.0f}%)")
    print(f"\n  (미탐 오답 {len(allwrong) - len(dwrong)}건 = 학습 검증기가 노릴 잔여분)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--analyze", action="store_true")
    a = ap.parse_args()
    if a.analyze:
        analyze()
    else:
        capture(a.n, a.k, a.temperature)
        analyze()


if __name__ == "__main__":
    main()
