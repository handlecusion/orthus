"""E2 §13 재측정용 골든 v2 — 지식 앵커를 **페이지 그라운딩**으로 교체.

§13(자기수정)이 밝힌 결함: keyword 앵커(`격주`/`핫픽스`)는 **다중정답 질문**에서 *다른 참인 답*을
오답 처리한다. 실제로 "atlas이 뭐야?"에 리프가 *"출연자와 기획사를 연결하는 플랫폼"* 이라는
**참인 답**을 냈는데 앵커 불일치로 실패 집계됐다 → §7의 전체 성공률(38~42%)이 **하방 편향**.

v2의 처방:
- 지식 앵커 = `kind:"page"` — **"어느 근거에서 답했나"**(인용된 wiki page slug)로 판정.
  표현·관점이 달라도 옳은 페이지에 그라운딩됐으면 성공. 내용 키워드는 **OR 보조**로만 둔다.
- 질문은 `e2_kb_gen.py`가 **wiki 페이지에서 역유도**하고 2모델 교차 검증(둘 다 거절 아님 +
  앵커 적중 + 출처에 원 페이지 포함)한 것만 쓴다 → 근거 실재가 **사전 보장**된다.
- 집계 앵커(`kind:"number"`)는 실DB gold라 편향이 없으므로 **v1 그대로 재사용**.
- v1의 설계 축(프리필터 사각 4문항)은 유지해 **v1↔v2 비교 가능성**을 지킨다.

실행: PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/e2_golden_v2.py
출력: golden/e2_v2.json
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden"

# orthus `_CONNECTIVE_TOKENS`에 **있는** 접속어 — 프리필터를 통과해 LLM 게이트까지 도달한다
_REACH = ["그리고", "또한"]
# orthus 토큰셋에 **없는** 접속어 — D4 O4 / E2 §8이 정량화한 코드 갭(프리필터 사각)
_BLIND = ["이랑", "각각"]

_STOP = {
    "무엇", "어떤", "어떻게", "언제", "누구", "관련", "포함", "내용", "정보", "설명", "알려",
    "인가요", "입니까", "있나요", "인지", "뭐야", "뭐고", "이고", "대해", "대한", "하는",
    # 질문 어미/기능어 — 토픽으로 쓰면 split 추적이 무의미해진다
    "무엇인가", "무엇인가요", "속하나요", "있나요", "되나요", "하나요", "인가", "몇개", "총몇",
    "문서", "제목", "범주", "정의", "요소", "이유", "경우", "항목", "기능", "사용", "상태",
    "얼마", "어디", "왜냐", "그리고", "또한", "각각", "이랑",
}
_CONNECTIVES = ["그리고", "또한", "이랑", "각각"]
_TOK = re.compile(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}")


def topics_of(q: str, page_slug: str) -> list[str]:
    """split 추적용 토픽 — **질문에 실재하는** 고유 토큰(앵커가 아니라 질문에서 뽑는다).

    topic_in_split()은 "이 토픽이 split 하위질문에 살아남았는가"를 보므로, 답이 아니라
    **질문의 식별 토큰**이어야 한다.
    """
    slug_toks = {t.lower() for t in _TOK.findall(page_slug)}
    cands: list[str] = []
    for t in _TOK.findall(q):
        low = t.lower()
        if low in _STOP or len(t) < 3:
            continue
        # 페이지 slug와 겹치는 토큰을 최우선(그 질문을 유일하게 식별)
        cands.append((0 if low in slug_toks else 1, -len(t), t))
    cands.sort()
    out: list[str] = []
    for _, _, t in cands:
        if t not in out:
            out.append(t)
        if len(out) == 2:
            break
    return out or [q.split()[0]]


def load_numeric() -> list[dict]:
    """집계 하위질문 = **t3 골든 원문** + 실DB gold number-set.

    v1 질문 텍스트를 파싱해 집계 절을 복원하려 했으나 실패했다 — v1의 지식 절이 `설명해주고`처럼
    **그 자체가 orthus 접속어**로 끝나서 절 경계가 모호하다. t3 골든은 **순수 단일 집계 질문**이라
    그런 모호성이 없고, gold는 `t3_gold.SPECS`로 실DB에서 계산하므로 **모델 편향 0**이다.
    """
    from t3_gold import SPECS, gold_numbers  # 실험 격리 코드 재사용

    t3 = {it["id"]: it["q"] for it in json.load(open(GOLDEN / "t3.json", encoding="utf-8"))["items"]}
    out: list[dict] = []
    for tid, spec in SPECS.items():
        if tid not in t3:
            continue
        gold = sorted(gold_numbers(*spec))
        if len(gold) != 1:  # 단일 정답(count)만 — groupby는 앵커가 다중이라 E2E 채점에 부적합
            continue
        q = t3[tid]
        out.append({
            "q": q,
            "anchor": {"topic": topics_of(q, ""), "kind": "number", "values": gold},
        })
    return out


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    # E2 루프 재측정(2026-07-14): 원래 v2 보류 사유가 "표본이 작다"였으므로 확대한다.
    # 지식 풀을 113개로 키운 뒤(`e2_kb_gen.py --limit 170 --keep-existing`) 슬라이스를 늘린다.
    ap.add_argument("--out", default="e2_v2.json", help="golden/<파일>")
    ap.add_argument("--ks", type=int, default=6, help="지식+집계 문항 수")
    ap.add_argument("--kk", type=int, default=6, help="지식+지식 문항 수")
    ap.add_argument("--blind", type=int, default=4, help="프리필터 사각 문항 수")
    args = ap.parse_args()

    know = json.load(open(GOLDEN / "e2_knowledge.json", encoding="utf-8"))["items"]
    uniq_num = load_numeric()
    if not uniq_num:
        raise SystemExit("집계 앵커 0개 — t3 SPECS/골든 확인 필요")

    need = args.ks + args.kk * 2 + args.blind
    if need > len(know):
        raise SystemExit(
            f"지식 풀 부족: {need}개 필요한데 {len(know)}개뿐. "
            f"`e2_kb_gen.py --limit N --keep-existing`로 먼저 확장하라."
        )

    items: list[dict] = []
    kn = list(know)
    ni = 0

    def knowledge_anchor(k: dict) -> dict:
        return {
            "topic": topics_of(k["q"], k["page_slug"]),
            "kind": "page",
            "slugs": [k["page_slug"]],
            "any_of": k["anchors"],  # 내용 키워드는 OR 보조 (그라운딩 실패해도 내용 맞으면 인정)
        }

    def add(kind: str, q: str, anchors: list[dict], blind: bool) -> None:
        items.append({
            "id": f"V2-{len(items) + 1:02}",
            "type": kind,
            "expected_parts": 2,
            "prefilter_blind": blind,
            "q": q,
            "anchors": anchors,
        })

    # 슬라이스는 **서로 겹치지 않게** 자른다. (구 버전은 blind가 ks와 같은 지식 항목을 재사용해
    # 같은 지식 절이 두 문항에 나왔다 — 풀이 25개뿐이라 어쩔 수 없었다. 풀이 113개가 된 지금은
    # 겹칠 이유가 없고, 겹치면 한 지식 항목의 난이도가 두 문항의 성패를 동시에 끌고 간다.)
    ks_src = kn[: args.ks]
    kk_src = kn[args.ks : args.ks + args.kk * 2]
    blind_src = kn[args.ks + args.kk * 2 : args.ks + args.kk * 2 + args.blind]

    # ── ks (지식 + 집계): 편향 0인 수치 앵커가 절반을 차지하게 한다 ──
    # 수치 질문은 **순환 재사용**한다(v1도 771을 5문항에서 썼다). gold가 실DB라 편향이 없고,
    # 지식 절이 매번 달라 복합질문은 서로 다른 문항이다.
    for i, k in enumerate(ks_src):
        n = uniq_num[ni % len(uniq_num)]
        ni += 1
        conn = _REACH[i % 2]
        add("ks", f"{k['q'].rstrip('?')}, {conn} {n['q']}", [knowledge_anchor(k), n["anchor"]], False)

    # ── kk (지식 + 지식) — 페이지 앵커의 다중정답 강건성이 여기서 드러난다 ──
    for i in range(0, len(kk_src) - 1, 2):
        a, b = kk_src[i], kk_src[i + 1]
        conn = _REACH[(i // 2) % 2]
        add("kk", f"{a['q'].rstrip('?')}, {conn} {b['q']}", [knowledge_anchor(a), knowledge_anchor(b)], False)

    # ── 프리필터 사각 (v1 설계 축 유지) — orthus 토큰셋에 없는 접속어 ──
    for i, k in enumerate(blind_src):
        n = uniq_num[(ni + i) % len(uniq_num)]
        conn = _BLIND[i % 2]
        q = (
            f"{k['q'].rstrip('?')}{conn} {n['q']}"
            if conn == "이랑"
            else f"{k['q'].rstrip('?')}, {conn} {n['q']}"
        )
        add("ks", q, [knowledge_anchor(k), n["anchor"]], True)

    out = GOLDEN / args.out
    json.dump({"items": items}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    n_blind = sum(1 for it in items if it["prefilter_blind"])
    n_page = sum(1 for it in items for a in it["anchors"] if a["kind"] == "page")
    n_num = sum(1 for it in items for a in it["anchors"] if a["kind"] == "number")
    print(f"골든 v2 {len(items)}문항 → {out}")
    print(f"  타입: ks {sum(1 for i in items if i['type'] == 'ks')} / kk {sum(1 for i in items if i['type'] == 'kk')}")
    print(f"  앵커: page {n_page} (다중정답 허용) / number {n_num} (실DB gold)")
    print(f"  프리필터 사각: {n_blind} (v1 설계 축 유지)")
    print("\n  샘플 3:")
    for it in items[:3]:
        print(f"    [{it['id']}] {it['q'][:70]}")
        for a in it["anchors"]:
            tgt = a.get("slugs") or a.get("values")
            print(f"        {a['kind']:8} topic={a['topic']} → {tgt}")


if __name__ == "__main__":
    main()
