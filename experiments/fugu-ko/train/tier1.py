"""G2 티어① — TF-IDF + 로지스틱 회귀 선택기 (하한 베이스라인, GPU 불필요).

설계(d5-learned-selector-plan.md §3):
- 타깃 = 문항별 모델 정답 벡터(multi-label) → 워커별 독립 이진 분류기 3개
  P(correct | question, task). 추론 = argmax P, 동률은 static(b) 배정 우선.
- 입력 = "[task] question" (orthus는 태스크를 이미 앎 — realizable).
- 피처 = TF-IDF char 2-4gram (한국어 조사/받침 변형에 강함).
- split 70/15/15 (seed 42) → train/data/split.json에 **고정 저장** — 티어②③(A100)이
  동일 split을 재사용해야 티어 간 비교가 공정하다.

평가(합성 test):
- (a) 항상 solar / (b) static(구조화→solar, 라우팅→ax) / (c) 티어① / oracle 4자 대조.
- 주 지표 = 라우팅된 모델의 정답률(end-to-end). 불일치 부분집합 별도 보고(H3 예비).
- G2 게이트: (c) ≥ (b) on 합성 test.

실행: PYTHONPATH=... python experiments/fugu-ko/train/tier1.py
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
WORKERS = ["solar", "ax", "exaone"]
STATIC = {"structured": "solar", "routing": "ax"}  # 2단(b) static_map과 동일
SEED = 42


def load_split() -> tuple[list[dict], dict[str, list[str]]]:
    rows = [json.loads(l) for l in open(DATA / "labeled.jsonl", encoding="utf-8")]
    split_path = DATA / "split.json"
    if split_path.exists():
        split = json.load(open(split_path, encoding="utf-8"))
        print(f"[split] 기존 split.json 재사용 (train {len(split['train'])} / val {len(split['val'])} / test {len(split['test'])})")
    else:
        ids = [r["id"] for r in rows]
        random.Random(SEED).shuffle(ids)
        n = len(ids)
        split = {"train": ids[: int(n * 0.7)],
                 "val": ids[int(n * 0.7): int(n * 0.85)],
                 "test": ids[int(n * 0.85):]}
        json.dump(split, open(split_path, "w", encoding="utf-8"))
        print(f"[split] 신규 생성 seed={SEED} (train {len(split['train'])} / val {len(split['val'])} / test {len(split['test'])}) → split.json 고정")
    return rows, split


def featurize_text(r: dict) -> str:
    return f"[{r['task']}] {r['q']}"


def pick(probs: dict[str, float], task: str) -> str:
    """argmax P(correct); 동률(±1e-9)은 static 배정 우선."""
    static = STATIC[task]
    best = max(probs.values())
    cands = [m for m in WORKERS if probs[m] >= best - 1e-9]
    return static if static in cands else max(cands, key=lambda m: probs[m])


def routed_acc(rows: list[dict], choose) -> float:
    ok = sum(1 for r in rows if r[f"correct_{choose(r)}"])
    return ok / len(rows) * 100


def main() -> None:
    rows, split = load_split()
    by_id = {r["id"]: r for r in rows}
    tr = [by_id[i] for i in split["train"]]
    va = [by_id[i] for i in split["val"]]
    te = [by_id[i] for i in split["test"]]

    # TF-IDF + 워커별 이진 분류기
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=2)
    Xtr = vec.fit_transform([featurize_text(r) for r in tr])
    Xva = vec.transform([featurize_text(r) for r in va])
    Xte = vec.transform([featurize_text(r) for r in te])

    clfs: dict[str, LogisticRegression] = {}
    for m in WORKERS:
        y = [int(r[f"correct_{m}"]) for r in tr]
        clf = LogisticRegression(max_iter=2000, C=1.0, random_state=SEED)
        clf.fit(Xtr, y)
        clfs[m] = clf

    def probs_for(X, i):
        return {m: float(clfs[m].predict_proba(X[i])[0][1]) for m in WORKERS}

    def tier1_choose_factory(X, rows_):
        idx = {r["id"]: k for k, r in enumerate(rows_)}
        return lambda r: pick(probs_for(X, idx[r["id"]]), r["task"])

    # 4자 대조
    for name, rows_, X in [("val", va, Xva), ("test", te, Xte)]:
        c_t1 = tier1_choose_factory(X, rows_)
        acc_a = routed_acc(rows_, lambda r: "solar")
        acc_b = routed_acc(rows_, lambda r: STATIC[r["task"]])
        acc_c = routed_acc(rows_, c_t1)
        oracle = sum(1 for r in rows_ if any(r[f"correct_{m}"] for m in WORKERS)) / len(rows_) * 100
        print(f"\n== 합성 {name} ({len(rows_)}문항) ==")
        print(f"  (a) 항상 solar     {acc_a:5.1f}%")
        print(f"  (b) static 규칙    {acc_b:5.1f}%")
        print(f"  (c) 티어① 학습    {acc_c:5.1f}%")
        print(f"  oracle 상한        {oracle:5.1f}%")
        # 불일치 부분집합 (H3 주판정 무대)
        dis = [r for r in rows_ if len({r[f"correct_{m}"] for m in WORKERS}) > 1]
        if dis:
            d_b = routed_acc(dis, lambda r: STATIC[r["task"]])
            d_c = routed_acc(dis, c_t1)
            d_o = sum(1 for r in dis if any(r[f"correct_{m}"] for m in WORKERS)) / len(dis) * 100
            print(f"  [불일치 {len(dis)}문항] (b) {d_b:.1f}%  (c) {d_c:.1f}%  oracle {d_o:.1f}%")
        if name == "test":
            gate = "PASS" if acc_c >= acc_b else "FAIL"
            print(f"  → G2 게이트((c)≥(b) on test): {gate}")

    # 선택 분포 (test)
    c_t1 = tier1_choose_factory(Xte, te)
    from collections import Counter
    dist = Counter(c_t1(r) for r in te)
    print(f"\n  티어① test 선택 분포: {dict(dist)}")


if __name__ == "__main__":
    main()
