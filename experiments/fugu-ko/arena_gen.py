"""Metric 05 — Assistant Arena: 생성 러너 (채점 없음).

prereg: analysis/arena-prereg.md (규칙 고정 후 실행). 항목셋 2종을 4모델로 생성한다:

- t2 wiki QA 30 : e2e/tier_a.jsonl task=="t2" → 프로덕션 `orthus.wiki.qa.ask(...)`
                  (scope=company, learn=False, record_gaps=False, chat_model 주입,
                  grounding=orthus_r2 — b3_r2_run.py와 동일 프로세스-로컬 배선)
- email draft 30: t12_generation.py::EMAIL_ITEMS → 프로덕션
                  `agentwork.service._generate_command_email` (b2_run의
                  get_chat_model_for 캡처 패치로 모델 주입, 반환 tuple이 배틀 대상)

모델: exaone(조립 1차)·solar(보조)·claude-sonnet-4-6(bedrock, 전체 prefix)·
gpt-5.3(openai, temperature/max_tokens 미전송). ax 금지. `.env` 무수정.

    .venv/bin/python experiments/fugu-ko/arena_gen.py --models exaone,solar,gpt-5.3
    .venv/bin/python experiments/fugu-ko/arena_gen.py --models claude-sonnet-4-6 --workers 4
"""

from __future__ import annotations

import os

# --- 프로세스-로컬 배선: orthus import 전에 orthus_r2 + solar embedding (b3_r2_run과 동일) --- #
os.environ["ORTHUS_PG_DSN"] = "postgresql+psycopg://orthus:orthus@localhost:5433/orthus_r2"
os.environ["ORTHUS_PG_DSN_READONLY"] = (
    "postgresql+psycopg://orthus_ro:orthus_ro@localhost:5433/orthus_r2"
)
os.environ["ORTHUS_EMBEDDING"] = "solar"
os.environ["ORTHUS_NODE_KIND"] = "company"

import argparse  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402
from pathlib import Path  # noqa: E402
from uuid import UUID  # noqa: E402

import b2_run  # noqa: E402  — load_dotenv + 프로덕션 어댑터/캡처 패치(get_chat_model_for, audit)
from b3_r2_run import _null_audit  # noqa: E402  — 동일 no-op audit 재사용

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "raw"
USER_ID = UUID("11111111-1111-1111-1111-111111111111")  # L1_HARNESS_UID — orthus_r2에 실존

# t2를 순수 read로: qa/retrieve/gap의 audit를 no-op으로 (b3_r2_run과 동일 대상).
import orthus.wiki.gap as _gap_mod  # noqa: E402
import orthus.wiki.qa as _qa_mod  # noqa: E402
import orthus.wiki.retrieve as _retrieve_mod  # noqa: E402

for _m in (_qa_mod, _retrieve_mod, _gap_mod):
    _m.audit = _null_audit

from orthus.agentwork import service as agent_service  # noqa: E402
from orthus.models.adapters.openai_compat import OpenAIChat  # noqa: E402
from orthus.wiki.qa import ask  # noqa: E402
from t12_generation import EMAIL_ITEMS  # noqa: E402

PREFLIGHT_Q = "nova가 어떤 서비스인지 설명해줘"  # prereg §2 — 알려진 answerable


def load_t2_items() -> list[dict]:
    items = []
    for line in (HERE / "e2e" / "tier_a.jsonl").read_text("utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("task") == "t2":
            items.append({"id": r["id"], **r["input"]["request"]})
    return items


class _Gpt53Endpoint:
    """openai:gpt-5.3-chat-latest — b2_run.Endpoint 최소 호환(slug/adapter/complete).

    harness_e2e._build_openai_chat와 동일 규칙: temperature 미전송(벤더가 default 강제),
    max_tokens 미전송(미지원). base_url은 api.openai.com 고정, 키는 ORTHUS_LLM_API_KEY
    (없으면 OPENAI_API_KEY 폴백 — b2_run gpt-4o-mini와 같은 폴백 규칙).
    """

    slug = "gpt-5.3"
    family = "openai"

    def __init__(self) -> None:
        key = (
            os.environ.get("ORTHUS_LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        ).strip()
        if not key:
            raise ValueError("ORTHUS_LLM_API_KEY/OPENAI_API_KEY required for gpt-5.3")
        self.adapter = OpenAIChat(
            "https://api.openai.com/v1", key, "gpt-5.3-chat-latest", temperature=None
        )

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        return self.adapter.complete(system, user, json_only=json_only)


class _CountingChat:
    """어댑터 프록시 — LLM 호출 수를 정확히 센다(보고서 §호출 수, Bedrock 특히)."""

    def __init__(self, inner) -> None:  # noqa: ANN001
        self._inner = inner
        self.model_id = getattr(inner, "model_id", "?")
        self.n_calls = 0
        self._lock = threading.Lock()

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        with self._lock:
            self.n_calls += 1
        return self._inner.complete(system, user, json_only=json_only)


def build_endpoint(slug: str):  # noqa: ANN201
    if slug == "gpt-5.3":
        ep = _Gpt53Endpoint()
    elif slug == "ax":
        raise ValueError("ax is banned in this layer (prereg §3)")
    else:
        ep = b2_run.build_endpoint(slug)
    ep.adapter = _CountingChat(ep.adapter)
    return ep


# --------------------------------------------------------------------------- #
def run_t2(ep, item: dict) -> dict:  # noqa: ANN001
    t0 = time.monotonic()
    res = ask(
        USER_ID,
        item["question"],
        k=item.get("k", 5),
        scope=item.get("scope", "company"),
        learn=False,
        record_gaps=False,
        chat_model=ep.adapter,
    )
    return {
        "id": item["id"],
        "question": item["question"],
        "answer": res.answer or "",
        "gap": (res.gap.reason if res.gap is not None else None),
        "sources": [
            {"slug": s.page_slug, "excerpt": (s.excerpt or "")[:400]} for s in (res.sources or [])[:5]
        ],
        "ms": int((time.monotonic() - t0) * 1000),
    }


def run_email(ep, item: dict) -> dict:  # noqa: ANN001
    t0 = time.monotonic()
    b2_run._email_tls.capture = ep.adapter  # thread-local — 패치된 get_chat_model_for가 반환
    try:
        subject, body, llm_drafted = agent_service._generate_command_email(
            item["to"], item["inst"], item["ctx"] or None
        )
    finally:
        b2_run._email_tls.capture = None
    return {
        "id": item["id"],
        "to": item["to"],
        "inst": item["inst"],
        "ctx": item["ctx"],
        "subject": subject,
        "body": body,
        "llm_drafted": bool(llm_drafted),
        "ms": int((time.monotonic() - t0) * 1000),
    }


def preflight_t2(ep) -> str | None:  # noqa: ANN001
    """prereg §2(개정 2026-07-22) — 배선 증명: sources ≥1 + 실질 답변. gap은 telemetry.

    원 기준(gap=None)은 결정론 `_looks_insufficient`가 정직한 유보 표현에 반응해
    실질·근거기반 답변에도 gap을 세우는 것이 확인돼 본판정 전에 개정됐다."""
    try:
        rec = run_t2(ep, {"id": "preflight", "question": PREFLIGHT_Q, "k": 5, "scope": "company"})
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:180]}"
    if len(rec["answer"]) < 40:
        return f"answer too short ({len(rec['answer'])} chars)"
    if not rec["sources"]:
        return "no sources"
    print(f"  [{ep.slug}] t2 preflight OK — gap={rec['gap']} (telemetry), "
          f"{len(rec['sources'])} sources", flush=True)
    return None


def run_set(ep, items: list[dict], fn, workers: int) -> tuple[list[dict], int]:  # noqa: ANN001
    results: dict[str, dict] = {}
    errors = 0
    lock = threading.Lock()

    def work(item: dict) -> None:
        nonlocal errors
        try:
            rec = fn(ep, item)
        except Exception as exc:  # noqa: BLE001 — 절대 조용히 드롭하지 않음
            with lock:
                errors += 1
            rec = {"id": item["id"], "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        results[item["id"]] = rec

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in as_completed([ex.submit(work, it) for it in items]):
            pass
    rows = [results[it["id"]] for it in items]
    print(
        f"  [{ep.slug}/{fn.__name__}] {len(rows)} items errors={errors} "
        f"{time.monotonic() - t0:.1f}s ({workers} workers)",
        flush=True,
    )
    return rows, errors


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default="exaone,solar,claude-sonnet-4-6,gpt-5.3")
    ap.add_argument("--tasks", default="t2,email")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    # retrieve.py의 lazy connector-registry는 스레드풀에서 check-then-act 레이스 (b3_r2_run 참고).
    from orthus.connectors.registry import register_default_connector_providers

    register_default_connector_providers(replace=True)

    t2_items = load_t2_items()
    email_items = [dict(it) for it in EMAIL_ITEMS]
    if args.limit:
        t2_items, email_items = t2_items[: args.limit], email_items[: args.limit]
    tasks = {t.strip() for t in args.tasks.split(",") if t.strip()}

    call_counts: dict[str, int] = {}
    for slug in [s.strip() for s in args.models.split(",") if s.strip()]:
        try:
            ep = build_endpoint(slug)
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] {slug}: build failed {type(exc).__name__}: {str(exc)[:150]}", flush=True)
            continue
        pf = b2_run.preflight(ep)
        if pf is not None:
            print(f"[skip] {slug}: preflight {pf}", flush=True)
            continue
        is_bedrock = getattr(ep, "family", "") == "bedrock"
        workers = min(args.workers, 4) if is_bedrock else args.workers

        if "t2" in tasks:
            pf2 = preflight_t2(ep)
            if pf2 is not None:
                print(f"[skip-t2] {slug}: grounding preflight failed — {pf2}", flush=True)
            else:
                rows, _ = run_set(ep, t2_items, run_t2, workers)
                write_jsonl(RAW_DIR / f"arena_t2_{slug}.jsonl", rows)
        if "email" in tasks:
            rows, _ = run_set(ep, email_items, run_email, workers)
            write_jsonl(RAW_DIR / f"arena_email_{slug}.jsonl", rows)
        call_counts[slug] = ep.adapter.n_calls
        print(f"  [{slug}] exact adapter .complete() calls: {ep.adapter.n_calls} "
              f"(incl. connectivity + t2 grounding preflight)", flush=True)

    print("\n== arena gen done. exact LLM calls by model ==", flush=True)
    for slug, n in call_counts.items():
        print(f"  {slug:<18} {n}", flush=True)


if __name__ == "__main__":
    main()
