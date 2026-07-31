"""배정표 11작업 중 **루프가 안 태우는 5작업**을 프로덕션 경로로 검증한다 (2026-07-14).

배경: E2 루프 재측정(#698/#705)은 `assign_final` arm에서 LLM 콜 205건을 냈는데 **전부 solar-pro**
였다. 루프가 부르는 작업이 6개(routing·intent·decompose·synthesize·structured·wiki_qa)뿐이고
그게 전부 Solar이기 때문이다. 즉 배정표 11작업 중 **5개는 프로덕션 계층 경유로 검증된 적이 없다**:

    delegation_extract → exaone   ← ★ 유일한 비-Solar 슬롯. 안전상 가장 중요
    graph_bind         → solar
    email_draft        → solar
    gap_suggest        → solar
    claim_headline     → solar

특히 `delegation_extract`는 **EXAONE이 배정된 유일한 슬롯**인데 루프에서 한 번도 안 불렸다.
그리고 이 슬롯은 오탐 1건 = **팀원 PC에서 샌드박스 없는 full-access 에이전트 실행**이다
(AGENTS.md carve-out). 잎(leaf) 홀드아웃(#696)에서 EXAONE FP=0 · Solar 1 · A.X 4로 측정돼
그 슬롯에 들어갔지만, **그때는 워커를 직접 호출**했다(`t10_delegation.py`가 프롬프트만 빌려
`chat.complete()`를 부른다). **프로덕션 함수가 실제로 EXAONE을 골라 같은 결과를 내는지는
미검증**이다 — 이 스크립트가 그 구멍을 닫는다.

기존 하네스와의 결정적 차이: **`chat_model=None`으로 프로덕션 함수를 부른다.**
그래야 `get_chat_model_for(task)`가 자기 배정표로 워커를 고른다. `prod_spy.ProdSpy`가 그 선택을
기록하고, 배정이 안 물린 콜이 1건이라도 있으면 그 측정은 **무효**다(E2의 I1과 같은 규칙).

실행:
  set -a; source ~/.orthus/nodes/company/node.env; set +a
  export ORTHUS_NODE=company ORTHUS_NODE_DB=orthus_company
  export ORTHUS_MODEL_ORCHESTRATION_ENABLED=true   # + 워커 키
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko:$PWD/experiments/fugu-ko/selectors \
    python experiments/fugu-ko/prod_slots_verify.py --slots delegation
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "selectors"))
sys.path.insert(0, str(HERE.parent.parent))

from prod_spy import ProdSpy  # noqa: E402

from orthus.models import orchestration as orch  # noqa: E402

GOLDEN = HERE / "golden"
RAW = HERE / "analysis" / "raw"


# ── delegation_extract ────────────────────────────────────────────────────────
def run_delegation(spy: ProdSpy, arm: str, golden: str) -> dict:
    """프로덕션 `extract_delegation_intent`를 `chat_model=None`으로 태운다.

    채점: **오탐(FP)이 1차 지표.** 위임이 아닌 텍스트를 위임으로 읽으면 팀원 PC에서 에이전트가
    돈다. FN(놓친 위임)은 사람이 다시 부탁하면 되지만 FP는 되돌릴 수 없다 — 비대칭 손실이라
    정확도 평균이 아니라 **최악값**으로 본다.

    arm:
      assign_final — `chat_model=None` → 프로덕션 배정 계층이 EXAONE을 고른다 (검증 대상)
      baseline     — 기본 슬롯(gpt-4o-mini) 명시 주입 (대조군)
      solar / ax   — 워커 풀에서 명시 주입 (배정 결정의 근거를 신규 골든에서 재현하는지 확인)
    """
    from orthus.agentwork.delegation import extract_delegation_intent
    from orthus.settings import get_settings

    settings = get_settings()
    data = json.loads((GOLDEN / golden).read_text(encoding="utf-8"))["items"]

    # assign_final만 배정 계층을 태운다. 나머지 arm은 모델을 명시 주입해 대조군이 된다.
    injected = None
    if arm == "baseline":
        from orthus.models.registry import get_chat_model

        injected = get_chat_model()
    elif arm in {"solar", "ax", "exaone"}:
        from pool import build_pool

        injected = build_pool([arm])[arm]

    rows = []
    for it in data:
        t0 = time.monotonic()
        try:
            got = extract_delegation_intent(it["text"], settings, chat_model=injected)
            err = None
        except Exception as e:  # noqa: BLE001
            got, err = None, f"{type(e).__name__}: {str(e)[:80]}"
        pred = got is not None  # 프로덕션 계약: dict가 나오면 위임으로 실행된다
        want = bool(it["is_delegation"])
        rows.append({
            "id": it["id"], "want": want, "pred": pred,
            "fp": (pred and not want), "fn": (want and not pred),
            "assignee_want": it.get("assignee"), "assignee_got": (got or {}).get("assignee"),
            "mode_want": it.get("mode"), "mode_got": (got or {}).get("mode"),
            "ms": int((time.monotonic() - t0) * 1000), "error": err,
            "text": it["text"][:70], "trap": it.get("trap"),
        })
    return {"task": "delegation_extract", "rows": rows}


SLOTS = {"delegation": run_delegation}

# ── 나머지 4슬롯: 배선 스모크 ─────────────────────────────────────────────────
# 품질은 이미 잎 홀드아웃(#696 §12)에서 측정됐고 배정이 전부 Solar다 — 루프에서 검증된 그 Solar와
# **같은 모델**이므로 품질 재측정은 중복이다. 여기서 닫는 구멍은 다른 것이다:
# **프로덕션 함수가 `chat_model=None`일 때 실제로 배정 워커를 물고 정상 동작하는가.**
# 배선이 끊겨 있으면(플래그/키/미배정) 조용히 기본 슬롯이 답하고, 아무도 모른다.
def run_wiring(spy: ProdSpy, arm: str, golden: str) -> dict:
    """graph_bind · gap_suggest · claim_headline을 프로덕션 경로로 1콜씩 태운다."""
    from uuid import UUID

    from orthus.settings import get_settings

    get_settings()
    uid = UUID("11111111-1111-1111-1111-111111111111")
    rows = []

    def probe(task: str, fn) -> None:
        t0 = time.monotonic()
        before = len(spy.calls)
        try:
            out = fn()
            err = None
        except Exception as e:  # noqa: BLE001
            out, err = None, f"{type(e).__name__}: {str(e)[:90]}"
        used = [c for c in spy.calls[before:] if c.stage == task]
        rows.append({
            "id": task, "want": True,
            "pred": bool(used),                       # 배정 워커가 실제로 불렸는가
            "fp": False, "fn": not used,
            "worker": (used[0].worker if used else None),
            "assigned": (used[0].assigned if used else None),
            "out": str(out)[:90],
            "ms": int((time.monotonic() - t0) * 1000), "error": err, "trap": None,
        })

    # graph_bind — KG 의도/명사구 추출 (LLM 1콜)
    from orthus.router.graph import bind_graph_params

    probe("graph_bind", lambda: bind_graph_params("nova와 atlas은 어떤 관계야?"))

    # gap_suggest — 백로그 행의 LLM 제안 채우기.
    # ⚠️ 첫 시도에 `detect_gap`을 불렀는데 LLM이 안 불렸다(0ms) — 그건 결정론 감지기다.
    # 실제 배정 슬롯을 쓰는 건 `generate_suggestion`이다. 진입점을 잘못 고르면 "배선이 끊겼다"는
    # 거짓 경보가 난다.
    from orthus.wiki.gap import generate_suggestion, record_gap

    def _gap_probe():
        from orthus.schemas.canonical import WikiGap

        gap_id = record_gap(
            uid,
            WikiGap(
                detected=True,
                reason="insufficient_grounding",
                missing_topic="nova 2027 매출 목표",
                top_score=0.1,
                message="근거 부족",
            ),
        )
        return generate_suggestion(uid, gap_id)

    probe("gap_suggest", _gap_probe)

    # claim_headline — 클레임 한 줄 요약.
    # ⚠️ 첫 시도에 `_llm_headline(orch.get_chat_model_for(...))`을 직접 불렀다 — 그러면 스파이가
    # 패치한 **호출부 모듈의 심볼**을 안 거치므로 콜이 관측되지 않는다(482ms 걸렸는데 미기록).
    # 프로덕션이 실제로 배정을 조회하는 지점은 `_generate_headlines`다.
    from orthus.wiki.backfill_claim_headline import _ClaimCandidate, _generate_headlines

    def _headline_probe():
        cand = _ClaimCandidate(
            slug="nova",
            claim=None,
            claim_text=(
                "nova는 영상 자막을 자동 생성하는 서비스이며 "
                "2026년 6월 기준 렌더링 파이프라인을 개편했다."
            ),
        )
        return _generate_headlines([cand], use_llm=True, dry_run=False, concurrency=1)

    probe("claim_headline", _headline_probe)

    # email_draft — 초안 생성. `draft_email` 독스트링: **"Never sends."** 순수 생성이라 안전하다.
    # (실발송은 `POST /mail/send` / approve 경로에서만 일어나고 이 함수를 안 거친다.)
    from orthus.mail.compose import draft_email
    from orthus.schemas.canonical import MailDraftRequest

    probe("email_draft", lambda: draft_email(MailDraftRequest(
        mode="compose",
        instruction="다음 주 배포 일정 공유 메일 초안 작성",
        to="team@example.com",
        subject="배포 일정 공유",
        context="7/17 금요일 15:00 배포 예정. 다운타임 없음.",
    )))

    return {"task": "wiring", "rows": rows}


SLOTS["wiring"] = run_wiring



def report(arm: str, res: dict, spy: ProdSpy) -> bool:
    rows = res["rows"]
    if res["task"] == "wiring":
        ok = True
        print(f"\n  ── {arm}")
        for r in rows:
            mark = "✅" if (r["pred"] and r["assigned"] and not r["error"]) else "❌"
            ok &= bool(r["pred"] and r["assigned"] and not r["error"])
            print(f"     {mark} {r['id']:16} 워커={r['worker'] or '(안 불림)':16} "
                  f"배정물림={r['assigned']}  {r['ms']:5}ms")
            if r["error"]:
                print(f"          오류: {r['error']}")
        return ok
    n = len(rows)
    errs = [r for r in rows if r["error"]]
    fp = [r for r in rows if r["fp"]]
    fn = [r for r in rows if r["fn"]]
    correct = sum(1 for r in rows if r["pred"] == r["want"])
    workers = sorted({c.worker for c in spy.calls}) or ["(콜 없음)"]
    unassigned = [c.stage for c in spy.calls if not c.assigned]

    print(f"\n  ── {arm}")
    print(f"     워커      : {', '.join(workers)}")
    print(f"     정확도    : {correct}/{n} ({correct / n * 100:.0f}%)")
    print(f"     ★ 오탐(FP): {len(fp)}  {[r['id'] for r in fp] or ''}")
    print(f"     미탐(FN)  : {len(fn)}  {[r['id'] for r in fn] or ''}")
    if errs:
        print(f"     실행오류  : {len(errs)} — **무효(I5)**")
    if arm != "baseline" and unassigned:
        print(f"     배정 미물림: {len(unassigned)} — **무효(I1)**")
    for r in fp:
        trap = f"  [{r['trap']}]" if r.get("trap") else ""
        print(f"       FP! {r['id']}: {r['text']!r}{trap}")
    return not errs and not (arm != "baseline" and unassigned)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slots", default="delegation")
    ap.add_argument("--arms", default="assign_final,baseline")
    ap.add_argument("--golden", default="t10_delegation.json")
    ap.add_argument("--tag", default="prodslots")
    args = ap.parse_args()

    print("=" * 78)
    print("프로덕션 배정 슬롯 검증 — 루프가 안 태우는 작업을 프로덕션 경로로 직접 태운다")
    print("=" * 78)

    # 사전 게이트: 배정이 실제로 물리는가 (E2의 preflight와 같은 규칙)
    for task in [orch.TASK_DELEGATION_EXTRACT]:
        m = orch.get_chat_model_for(task)
        ok = isinstance(m, orch.FallbackChat)
        print(f"\npreflight  {task} → {orch.ASSIGNMENTS[task]} : "
              f"{'✅ ' + getattr(m, 'model_id', '?') if ok else '❌ 배정 미물림 — 측정 시작 불가'}")
        if not ok:
            raise SystemExit("배정이 물리지 않는다. 플래그/키를 확인하라.")

    RAW.mkdir(parents=True, exist_ok=True)
    valid = True
    for slot in args.slots.split(","):
        fn = SLOTS[slot.strip()]
        print(f"\n\n■ {slot}  (골든 {args.golden})")
        for arm in args.arms.split(","):
            spy = ProdSpy()
            spy.install()
            try:
                res = fn(spy, arm.strip(), args.golden)
            finally:
                spy.uninstall()
            valid &= report(arm.strip(), res, spy)
            out = RAW / f"{args.tag}_{slot}_{arm.strip()}.jsonl"
            with out.open("w", encoding="utf-8") as f:
                for r in res["rows"]:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("\n" + "=" * 78)
    print("✅ 유효한 측정" if valid else "⛔ 무효 — 수치를 채택하지 않는다")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    main()
