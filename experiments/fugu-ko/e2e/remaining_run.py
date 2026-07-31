"""잔여 5종 슬롯 생성 러너 — wiki_qa · synthesize · email_draft · gap_suggest · claim_headline.

`RUNNER_DESIGN.md`의 결정을 그대로 구현한다.

- **실행 계층은 `arena_run.py`에서 import한다**(재정의 금지, `koparity_run.py`가 세운 규약):
  `PRICING` / `SYSTEMS` / `DryRunClient` / `build_client` / `estimate_cost_usd`.
  ⚠️ 이 워크트리에는 `arena_run.py`가 없어 **복사해 왔다**(아래 "arena_run 출처" 참조).
- **데이터 계층은 새로 쓴다** — arena의 gold/parts join·bare/glue 조건 확장 축이 잔여 5종에
  맞지 않는다. 슬롯별 단일 골든 파일(`{"items": [...]}` 또는 `.jsonl`)을 읽는다.
- **프롬프트는 프로덕션 경로를 그대로 부른다**(t9/t10/t12 방식). arena용으로 재작성된
  `arena_prompts.REGRESSION_TASKS` 프롬프트는 쓰지 않는다 — 이 측정의 목적이 "프로덕션 슬롯을
  이 모델로 바꿔도 되는가"이기 때문이다.
- **채점은 t12_generation.py의 지표를 그대로 재사용**한다(`_invented` 임포트). 주관 judge는
  이 러너 밖(2단 분리 — RUNNER_DESIGN §3).

## arena_run 출처 (provenance)

`experiments/fugu-ko/arena_run.py` · `arena_prompts.py`는 이 워크트리에 없었고,
`.worktrees/ko-parity/experiments/fugu-ko/`(브랜치 `feat/ko-parity-benchmark`,
HEAD `36e5573`)에서 **바이트 그대로 복사**했다. 복사 시점 md5:

    arena_run.py     64ffd9825be061fbd4ffa8fb57301c9a   (⚠️ ko-parity에서 uncommitted 상태 —
                                                         마지막 커밋 f13ad655(2026-07-24)보다
                                                         앞선 작업본. deepseek/glm 슬러그 확장 포함)
    arena_prompts.py 959ef64448557efc8e425d8f1bba8561   (커밋됨; arena_run이 import만 함)

`arena_run.py`는 `sys.path`에 `HERE.parent.parent`(= 저장소 루트)를 넣어 `orthus`를 임포트하므로
**반드시 `experiments/fugu-ko/` 바로 밑에 있어야 한다**(e2e/ 하위로 옮기면 경로 계산이 깨진다).

## 프로덕션 진입점

| task           | entry point                                                    |
|----------------|----------------------------------------------------------------|
| wiki_qa        | `orthus.wiki.qa.answer_from_hits(learn=False, record_gaps=False)` |
| synthesize     | `orthus.router.decompose.synthesize`                             |
| email_draft    | `orthus.agentwork.service._generate_command_email`의 프롬프트 재현 |
| gap_suggest    | `orthus.wiki.gap._SUGGEST_SYSTEM` + `_parse_suggestion`           |
| claim_headline | `orthus.wiki.distill.HEADLINE_SYSTEM`                            |

앞의 둘은 `chat_model=` 주입을 받으므로 **함수 자체를 부른다**. 대신 두 함수 모두 `audit()`
스팬을 쓰므로 **Postgres가 필요하다**(`ORTHUS_PG_DSN`). DB가 없으면 자동으로 프롬프트 레벨
경로(`_SYSTEM` + `_build_user_prompt` / `_SYNTHESIZE_SYSTEM` + 동일 user prompt)로 내려가고,
각 행의 `entry_point`에 어느 경로로 돌았는지 남는다(`--prompt-only`로 강제 가능).
뒤의 셋은 프로덕션 호출부가 chat 주입을 안 받아 t12_generation.py와 같이 프롬프트를
임포트/재현한다(email은 재현이라 import한 원본과 문자열 동일성을 런타임에 검증한다).

## ⚠️ 어느 DB에 붙는가 (2026-07-29 사고)

`--env-file` 기본값은 **회사 노드 env**(`~/.orthus/nodes/company/node.env` → `orthus_company`,
wiki_chunks 28,186)다. 저장소 `.env`의 `ORTHUS_PG_DSN`은 3-chunk dev 픽스처(`orthus`)를 가리켜서,
그걸 로드한 채 wiki_qa를 돌리면 **모든 문항이 같은 근거(`claim-a`/`claim-b`/`page-x`)를 받는다** —
조용히 무의미해지는 실패였다. 그래서 시작 시 `preflight_corpus`가 붙은 DB 이름·wiki 규모·
임베딩 `model_version` 단일성을 출력하고, `wiki_chunks < --min-wiki-chunks`(기본 1000)이면
wiki_qa/synthesize 실행을 **중단**한다(`--allow-small-corpus`로만 강행).
저장소 `.env`는 node.env에 없는 벤더 키(ax/exaone/deepseek/glm) 보충용으로만 뒤에 로드된다.

### 빈 값 함정 (2026-07-29)

node.env는 `ORTHUS_LLM_BEDROCK_API_KEY=`를 **빈 값**으로 선언한다. `load_dotenv(override=False)`는
"키가 이미 있으면 건너뛴다"라서 그 빈 값이 저장소 `.env`의 진짜 키를 영구히 가리고,
`arena_run.build_client`의 `... or ORTHUS_LLM_API_KEY` 폴백이 발동해 Bedrock에 OpenAI 키가
실린다 → **403 "Invalid API Key format: Must start with pre-defined prefix"**(claude-opus-4.8 /
glm-5-bedrock 둘 다 재현). 그래서 보충 로더 `_fill_blank`는 **빈 문자열을 미설정으로 보고**
덮어쓰고, `preflight_vendor_keys`가 Bedrock 2종에 대해 OpenAI 폴백을 허용하지 않고 즉시 중단한다.
`arena_run.py`는 출처 보존을 위해 수정하지 않았다.

또 하나: node.env와 저장소 `.env`의 `ORTHUS_LLM_API_KEY`가 **서로 다른 키**이고 node.env 쪽이
지속적으로 429다(실측). 셸 env가 두 파일보다 우선하므로
`export ORTHUS_LLM_API_KEY=<저장소 .env 값>`으로 gpt 계열만 덮어 회피한다.

## 입력 freeze (두 태스크 공통 원칙)

측정 변수는 **답변 모델 하나**여야 한다. 그래서 워커별로 입력을 다시 만들지 않는다.

- `wiki_qa`: 골든에 `hits`가 없으면 `retrieve()`로 **한 번만** 뽑아
  `<out-dir>/wiki_qa_hits.jsonl`(`--hits-cache`)에 남기고 전 워커가 공유한다.
- `synthesize`: 골든(`t8_synthesize_1k.json`)은 **부모 복합질문만** 담는 것이 정상이다
  (t8_synth.py 설계). 러너가 고정 모델(solar)로 split → 리프 실행 → grounded 조각을 freeze해
  `<out-dir>/synthesize_subs.jsonl`(`--subs-cache`)에 남긴다. grounded<2는 프로덕션이
  결정론 passthrough라 LLM을 안 부르므로 t8_synth와 동일하게 제외한다.

## 사용

    # 배선/비용/로스터만 검증 (네트워크·골든 불필요)
    python experiments/fugu-ko/e2e/remaining_run.py --dry-run

    # 벤더 1콜씩 카나리
    python experiments/fugu-ko/e2e/remaining_run.py --canary 1 --cost-cap-usd 0.50

    # 본실행 (태스크·시스템 지정)
    python experiments/fugu-ko/e2e/remaining_run.py --system solar,exaone --tasks wiki_qa

출력: `<out-dir>/{task}__{system}.jsonl` (기본 `experiments/fugu-ko/analysis/raw/remaining/`).
재개 키는 `(task, id, system)` — 파일이 이미 (task, system)로 갈려 있으므로 파일 안에서는
`id`가 키다. arena와 동일하게 **error 행은 done으로 세지 않고** 같은 키의 마지막 행이 이긴다.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

HERE = Path(__file__).resolve().parent          # experiments/fugu-ko/e2e
FUGU = HERE.parent                              # experiments/fugu-ko


def _find_repo_root(start: Path) -> Path:
    """arena_run._find_repo_root와 동일 규약(워크트리/메인 체크아웃 양쪽 동작)."""
    for p in start.parents:
        if p.name == ".worktrees":
            return p.parent
    return start.parent.parent


_REPO_ROOT = _find_repo_root(FUGU)
_WORKTREE_ROOT = FUGU.parent.parent             # 이 체크아웃(워크트리)의 루트 = orthus 패키지 위치

for _p in (str(HERE), str(FUGU), str(_WORKTREE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 실행 계층 — 재정의 금지 (koparity_run.py 규약)
from arena_run import (  # noqa: E402
    PRICING,
    SYSTEMS,
    DryRunClient,
    build_client,
    estimate_cost_usd,
)

# 채점 지표 — t12_generation.py 원본 재사용 (탐지기 수정은 한 곳에서)
from t12_generation import _invented  # noqa: E402

# --------------------------------------------------------------------------- #
# 로스터 / 태스크
# --------------------------------------------------------------------------- #
TASKS: tuple[str, ...] = (
    "wiki_qa",
    "synthesize",
    "email_draft",
    "gap_suggest",
    "claim_headline",
)

# 확정 7종(국내 3 + 프론티어 4). Claude Sonnet 4.6은 judge 전용이라 **워커가 아니다**.
ROSTER: tuple[str, ...] = (
    "solar",
    "exaone",
    "ax",
    "claude-opus-4.8",
    "gpt-5.6-sol",
    "deepseek-v4-pro",
    "glm-5-bedrock",
)

# 조용한 모델 스왑 방지용 하드 핀. build_client가 env override를 허용하므로(예:
# ORTHUS_LLM_DEEPSEEK_MODEL_OVERRIDE) 실제 해석된 model_id를 여기서 다시 확인한다.
#   · deepseek: 슬러그 default(`deepseek-chat`)는 2026-07-24 이후 v4-flash로 조용히 매핑된다.
#     → `deepseek-v4-pro`를 명시 pin. 직접 API(api.deepseek.com) 전용(Bedrock에 V4 없음).
#   · glm: z.ai 직접 API는 과금사고 이력으로 금지. Bedrock `zai.glm-5` (inference prefix 없음).
#   · opus-4.8: Bedrock `anthropic.claude-opus-4-8` (런타임 `us.` 접두, 끝에 `-v1` 없음).
EXPECTED_MODEL_ID: dict[str, str] = {
    "deepseek-v4-pro": "deepseek-v4-pro",
    "glm-5-bedrock": "bedrock:zai.glm-5",
    "claude-opus-4.8": "bedrock:us.anthropic.claude-opus-4-8",
}

DEFAULT_OUT_DIR = FUGU / "analysis" / "raw" / "remaining"
DEFAULT_GOLDEN_DIR = FUGU / "golden"
# 이 실험의 대상은 **회사 노드**다. 저장소 `.env`의 DSN은 dev 픽스처(`orthus`)라 기본값이 아니다.
DEFAULT_NODE_ENV = Path.home() / ".orthus" / "nodes" / "company" / "node.env"

# synthesize 입력 freeze에 쓰는 고정 모델(t8_synth.py 규약) — 평가 대상이 아니라 입력 생성기다.
FREEZE_MODEL = "solar"

# 골든 파일명 후보 — 골든은 다른 세션이 만드는 중이라 이름을 못 박지 않는다.
# 앞에서부터 먼저 존재하는 파일을 쓴다. `--golden task=path`로 강제 가능.
GOLDEN_CANDIDATES: dict[str, tuple[str, ...]] = {
    # 1순위는 신규 1k 골든(다른 세션이 생성 중). `t2_wiki_qa_1k.json`은 item에 `kind`
    # (사실형/종합형)가 붙지만 이 러너는 `q`만 읽으므로 하위호환 문제 없다.
    "wiki_qa": (
        "t2_wiki_qa_1k.json", "t12_wiki_qa.json", "wiki_qa.json", "t2_wiki_qa.json",
        "t2_wiki_qa_1k.jsonl", "wiki_qa.jsonl",
    ),
    # 골든은 **부모 복합질문만** 담는다(t8_synth.py 설계). frozen sub_answers는 러너가
    # `freeze_synthesize_inputs`로 만든다. `t8.json`(구 8문항)도 같은 스키마라 후보로 둔다.
    "synthesize": (
        "t8_synthesize_1k.json", "t12_synthesize.json", "synthesize.json", "t8_frozen.json",
        "t8.json",
    ),
    "email_draft": ("t12_email_draft.json", "t12_email.json", "email_draft.json"),
    "gap_suggest": ("t12_gap_suggest.json", "t12_gap.json", "gap_suggest.json"),
    "claim_headline": ("t12_claim_headline.json", "t12_headline.json", "claim_headline.json"),
}

UID = UUID("11111111-1111-1111-1111-111111111111")


# --------------------------------------------------------------------------- #
# 더미 문항 — 골든이 아직 없을 때 배선만 검증하기 위한 최소 세트.
# 결과 행에 golden="dummy"가 박히므로 분석에서 절대 섞이지 않는다.
# --------------------------------------------------------------------------- #
DUMMY_ITEMS: dict[str, list[dict]] = {
    "wiki_qa": [
        {
            "id": "dq01",
            "q": "Nova 자막 패널 렌더링 이슈는 어떻게 처리됐어?",
            "hits": [
                {
                    "page_slug": "nova/sprint-6",
                    "title": "Nova Sprint 6",
                    "excerpt": "Sprint 6에서 자막 패널 렌더링 버그를 수정했고 다크 테마는 설정 화면까지 적용됐다.",
                },
                {
                    "page_slug": "nova/roadmap",
                    "title": "Nova 개발 로드맵",
                    "excerpt": "영상 생성 파이프라인 개선은 다음 스프린트로 이월됐다.",
                },
            ],
        },
        {
            "id": "dq02",
            "q": "라이브아바타 로딩 개선 결과는?",
            "hits": [
                {
                    "page_slug": "nova/liveavatar",
                    "title": "라이브아바타",
                    "excerpt": "일반 카테고리 리소스 최소화 작업으로 초기 로딩이 40% 줄었다.",
                }
            ],
        },
    ],
    "synthesize": [
        {
            "id": "ds01",
            "q": "Sprint 6에서 뭐가 끝났고 라이브아바타 로딩은 얼마나 빨라졌어?",
            "subs": [
                {
                    "q": "Sprint 6에서 뭐가 끝났어?",
                    "body": "Sprint 6에서 자막 패널 렌더링 버그를 수정했고 다크 테마는 설정 화면까지 적용됐다.",
                },
                {
                    "q": "라이브아바타 로딩은 얼마나 빨라졌어?",
                    "body": "일반 카테고리 리소스 최소화 작업으로 초기 로딩이 40% 줄었다.",
                },
            ],
        },
        {
            "id": "ds02",
            "q": "출국 QR 오류 상태와 릴리스 일정은?",
            "subs": [
                {"q": "출국 QR 오류 상태는?", "body": "출국 QR 오류가 재현돼 원인 분석 중이다."},
                {"q": "릴리스 일정은?", "body": "릴리스를 3일 미루기로 했다."},
            ],
        },
    ],
    "email_draft": [
        {"id": "de01", "to": "파트너사 담당자", "inst": "다음 주 미팅 일정을 제안하는 메일", "ctx": ""},
        {
            "id": "de02",
            "to": "고객사",
            "inst": "이 내용을 기반으로 진행 상황 공유 메일 작성",
            "ctx": "Sprint 6에서 자막 패널 렌더링 버그를 수정했다.",
        },
    ],
    # reason은 프로덕션 enum(GapReason 4종)만 쓴다 — 옛 인라인 골든의 no_hits/low_confidence는
    # 프로덕션에 없는 값이었다(t12_gap_suggest.json note가 지적한 스키마 위반).
    "gap_suggest": [
        {"id": "dg01", "q": "우리 회사 휴가 규정이 어떻게 돼?", "reason": "no_data"},
        {"id": "dg02", "q": "온보딩 첫 주에 뭘 해야 해?", "reason": "insufficient_grounding"},
    ],
    "claim_headline": [
        {
            "id": "dh01",
            "claim": "라이브아바타 일반 카테고리 리소스 최소화 작업으로 초기 로딩 시간이 40% 감소했으며, 추가 최적화에는 GPU 증설이 필요하다.",
        },
        {
            "id": "dh02",
            "claim": "Sprint 6 완료율은 82%이고 오디오 P0 항목 2건이 미완 상태로 다음 스프린트에 이월됐다.",
        },
    ],
}


# --------------------------------------------------------------------------- #
# 골든 로드 (koparity 방식: .json {"items": [...]} + .jsonl 양쪽, id 중복은 즉시 에러)
# --------------------------------------------------------------------------- #
def _read_items(path: Path) -> list[dict]:
    if path.suffix == ".jsonl":
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        blob = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(blob, list):
            rows = blob
        elif isinstance(blob, dict):
            rows = blob.get("items") or blob.get("rows") or []
        else:
            raise SystemExit(f"{path}: unsupported golden shape {type(blob).__name__}")
    if not isinstance(rows, list):
        raise SystemExit(f"{path}: 'items' is not a list")
    seen: set[str] = set()
    for i, r in enumerate(rows):
        rid = str(r.get("id") or "")
        if not rid:
            raise SystemExit(f"{path}: item #{i} has no id")
        if rid in seen:
            raise SystemExit(f"{path}: duplicate id {rid!r} — 골든 id는 유일해야 한다")
        seen.add(rid)
    return rows


def resolve_golden(task: str, golden_dir: Path, override: str | None) -> Path | None:
    if override:
        p = Path(override)
        if not p.exists():
            raise SystemExit(f"--golden {task}={override}: 파일 없음")
        return p
    # golden/ 이 1순위, e2e/ 는 2순위(골든 생성 세션이 e2e 밑에 두는 경우가 있다).
    for d in (golden_dir, HERE):
        for name in GOLDEN_CANDIDATES[task]:
            p = d / name
            if p.exists():
                return p
    return None


def load_task_items(
    task: str, golden_dir: Path, override: str | None
) -> tuple[list[dict], str]:
    """(items, golden_label). 골든이 없으면 내장 더미로 떨어지고 label='dummy'."""
    path = resolve_golden(task, golden_dir, override)
    if path is None:
        return list(DUMMY_ITEMS[task]), "dummy"
    return _read_items(path), path.name


# --------------------------------------------------------------------------- #
# 항목 정규화 — 골든 스키마가 태스크마다 다르므로 필드 별칭을 여기서 흡수한다.
# 반환 dict가 executor의 유일한 입력이다(원본 item은 raw 저장에만 쓴다).
# --------------------------------------------------------------------------- #
def _first(item: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return default


def _prepare(task: str, item: dict) -> dict:
    """정규화. 못 쓰는 항목은 ValueError(사유)로 걸러 낸다."""
    if task == "wiki_qa":
        q = _first(item, "q", "question")
        if not q:
            raise ValueError("no question")
        raw_hits = item.get("hits") or item.get("sources") or item.get("passages") or []
        hits: list[dict] = []
        for i, h in enumerate(raw_hits):
            if isinstance(h, str):
                h = {"excerpt": h}
            if not isinstance(h, dict):
                continue
            excerpt = _first(h, "excerpt", "text", "body", "content")
            if not excerpt:
                continue
            hits.append(
                {
                    "page_slug": _first(h, "page_slug", "slug", default=f"golden/hit-{i + 1}"),
                    "title": _first(h, "title", default=""),
                    "kind": _first(h, "kind", default="claim"),
                    "excerpt": excerpt,
                    "score": float(h.get("score") or 1.0),
                }
            )
        # hits가 골든에 없으면 여기서 탈락시키지 않는다 — 로드 후 `freeze_wiki_qa_hits`가
        # 프로덕션 `retrieve()`로 **한 번만** 뽑아 전 워커에 같은 입력을 공유한다(입력 freeze).
        return {"q": q, "hits": hits or None}

    if task == "synthesize":
        q = _first(item, "q", "question")
        if not q:
            raise ValueError("no question")
        raw_subs = item.get("subs") or item.get("sub_answers") or item.get("parts") or []
        subs: list[dict] = []
        for s in raw_subs:
            if not isinstance(s, dict):
                continue
            body = _first(s, "body", "answer", "text_answer")
            sub_q = _first(s, "q", "question", "text", "sub_question")
            if body:
                subs.append({"q": sub_q or q, "body": body})
        # subs가 골든에 없는 것이 **정상**이다 — t8_synth.py 설계상 골든은 부모 복합질문만
        # 담고, 러너가 고정 모델로 split→leaf→grounded freeze를 수행한다.
        # grounded<2 판정(=합성 LLM 미발화 제외)은 freeze 이후에 한다.
        return {"q": q, "subs": subs}

    if task == "email_draft":
        to = _first(item, "to", "recipient", "recipient_hint")
        inst = _first(item, "inst", "instruction", "q", "question")
        if not to or not inst:
            raise ValueError("email item needs both recipient and instruction")
        return {"to": to, "inst": inst, "ctx": _first(item, "ctx", "context", "upstream")}

    if task == "gap_suggest":
        q = _first(item, "q", "question")
        if not q:
            raise ValueError("no question")
        reason = _first(item, "reason", default="insufficient_grounding")
        return {"q": q, "reason": reason}

    if task == "claim_headline":
        claim = _first(item, "claim", "text", "q")
        if not claim:
            raise ValueError("no claim text")
        return {"claim": claim}

    raise AssertionError(f"unknown task {task}")  # pragma: no cover


# --------------------------------------------------------------------------- #
# wiki_qa 입력 freeze — 골든이 질문만 담고 있으면 프로덕션 `retrieve()`로 hits를 한 번만
# 뽑아 **전 워커가 같은 근거를 본다**(측정 변수는 답변 모델 하나뿐이어야 한다, t8_synth 규약).
# 결과는 캐시 파일에 남겨 재개/재실행이 같은 입력을 쓰게 한다.
# --------------------------------------------------------------------------- #
def _ref_to_dict(ref: Any) -> dict:
    return {
        "page_slug": ref.page_slug,
        "title": ref.title,
        "kind": ref.kind,
        "excerpt": ref.excerpt,
        "score": float(ref.score),
    }


def freeze_wiki_qa_hits(
    rows: list[dict], *, k: int, scope: str, cache_path: Path, dry_run: bool
) -> tuple[list[dict], list[str]]:
    """hits가 없는 항목을 채운다. 반환 (사용 가능한 rows, 제외 사유 목록)."""
    cache: dict[str, list[dict]] = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                cache[str(d["id"])] = d["hits"]

    need = [r for r in rows if not r["prep"]["hits"] and r["id"] not in cache]
    if need and dry_run:
        # dry-run은 네트워크를 쓰지 않는다 — retrieve는 임베딩 API 호출이다.
        for r in need:
            cache[r["id"]] = [
                {
                    "page_slug": "dry-run/placeholder",
                    "title": "dry-run",
                    "kind": "claim",
                    "excerpt": "dry-run placeholder passage",
                    "score": 1.0,
                }
            ]
    elif need:
        from orthus.wiki.retrieve import retrieve

        # DB 규모/정합성 확인은 시작 시 `preflight_corpus`가 이미 크게 실패시킨다.
        print(f"      wiki_qa hits freeze: retrieve {len(need)}건 (k={k}, scope={scope})", flush=True)
        # **문항 단위 append + flush**. 1,000문항 freeze를 메모리에 모았다가 끝에 한 번 쓰면
        # 중간에 죽을 때 전량 유실되고 진행 상황도 안 보인다(2026-07-29 실측: 5분간 0바이트).
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        t_start = time.monotonic()
        with open(cache_path, "a", encoding="utf-8") as f:
            for i, r in enumerate(need, 1):
                try:
                    refs = retrieve(UID, r["prep"]["q"], k=k, scope=scope)
                except Exception as e:  # noqa: BLE001
                    print(f"      [warn] retrieve 실패 {r['id']}: {type(e).__name__}: {str(e)[:120]}", flush=True)
                    refs = []
                cache[r["id"]] = [_ref_to_dict(x) for x in refs]
                f.write(json.dumps({"id": r["id"], "hits": cache[r["id"]]}, ensure_ascii=False) + "\n")
                f.flush()
                if i % 50 == 0 or i == len(need):
                    el = time.monotonic() - t_start
                    print(
                        f"        freeze {i}/{len(need)} ({el:.0f}s, {el / i:.2f}s/건, "
                        f"ETA {(len(need) - i) * el / i / 60:.1f}분)",
                        flush=True,
                    )

    out: list[dict] = []
    dropped: list[str] = []
    for r in rows:
        if not r["prep"]["hits"]:
            r["prep"]["hits"] = cache.get(r["id"]) or []
        if not r["prep"]["hits"]:
            # 근거 0건이면 프로덕션도 LLM을 안 부른다(no_hits 상수 반환) — 측정 대상 아님.
            dropped.append(f"{r['id']}(no grounding hits)")
            continue
        out.append(r)
    return out, dropped


# --------------------------------------------------------------------------- #
# synthesize 입력 freeze — `t8_synth.py::build_inputs` 이식.
#
# 골든(`t8_synthesize_1k.json`)은 **부모 복합질문만** 담는 것이 정상이다(설계상 그렇다).
# 러너가 고정 모델(solar)로 split → 리프 실행 → grounded 조각을 freeze하고, 그 **하나의
# 입력**을 전 워커가 공유해야 비교가 성립한다(워커별 재freeze 금지 — 입력이 변수가 되면
# 측정 대상이 두 개가 된다). freeze 결과는 캐시 파일로 남아 재개 시 재사용된다.
# --------------------------------------------------------------------------- #
# t8_synth.py와 동일한 denial 문자열 집합(grounded 판정).
_DENIALS = ("근거 없", "제공되지 않", "찾을 수 없", "정보가 없", "확인되지 않")


def _grounded_body(routed: Any) -> str | None:
    from orthus.router.decompose import _extract_body

    if routed is None:
        return None
    body = (_extract_body(routed) or "").strip()
    if not body or any(d in body for d in _DENIALS):
        return None
    return body


def freeze_synthesize_inputs(
    rows: list[dict], *, cache_path: Path, dry_run: bool, scope: str, split_k: int = 3
) -> tuple[list[dict], list[str], float]:
    """subs가 없는 항목을 고정 모델(solar) split→leaf 실행으로 채운다.

    반환 (사용 가능한 rows, 제외 사유, freeze 추정 비용 USD).
    grounded 조각이 2개 미만이면 프로덕션 synthesize가 **결정론 passthrough**로 LLM을 안
    부르므로 t8_synth와 동일하게 제외한다.
    """
    cache: dict[str, list[dict]] = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                cache[str(d["id"])] = d["subs"]

    need = [r for r in rows if not r["prep"]["subs"] and r["id"] not in cache]
    freeze_cost = 0.0
    if need and dry_run:
        # 실측 기준(2026-07-29 canary): 문항당 LLM 4콜(split 1 + 리프 3), 고정 solar
        # 2문항 freeze에 $0.0010 / 벽시계 ~40초. 아래는 그 단가의 선형 외삽이다.
        per_item_usd, per_item_s, per_item_calls = 0.0005, 20.0, 4
        n = len(need)
        print(
            f"      [freeze 예상] {n}문항 × {per_item_calls}콜 = {n * per_item_calls:,}콜 "
            f"| 추정 ${n * per_item_usd:.2f} | 직렬 소요 ~{n * per_item_s / 3600:.1f}h "
            f"(고정 {FREEZE_MODEL}, split k={split_k}; 실측 2문항 단가의 선형 외삽)"
        )
        for r in need:
            cache[r["id"]] = [
                {"q": "dry-run sub 1", "body": "dry-run placeholder body 1"},
                {"q": "dry-run sub 2", "body": "dry-run placeholder body 2"},
            ]
    elif need:
        from orthus.router import answer as router_answer
        from orthus.router.decompose import split_question

        print(f"      synthesize 입력 freeze: {len(need)}건 (고정 모델 {FREEZE_MODEL}, split k={split_k})")
        fixed_client = build_client(FREEZE_MODEL)
        price_in, price_out = PRICING[FREEZE_MODEL]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "a", encoding="utf-8") as fh:
            for r in need:
                chat = ChatShim(fixed_client)
                subs: list[dict] = []
                try:
                    subtexts = split_question(r["prep"]["q"], k=split_k, chat_model=chat)
                    if len(subtexts) >= 2:
                        for t in subtexts:
                            try:
                                routed = router_answer(
                                    UID, t, scope=scope, chat_model=chat,
                                    learn=False, record_gaps=False,
                                )
                            except Exception:  # noqa: BLE001 — 리프 실패는 미그라운드 처리
                                routed = None
                            body = _grounded_body(routed)
                            if body:
                                subs.append({"q": t, "body": body})
                except Exception as e:  # noqa: BLE001
                    print(f"      [warn] freeze 실패 {r['id']}: {type(e).__name__}: {str(e)[:120]}")
                freeze_cost += estimate_cost_usd(chat.usage, price_in, price_out)
                cache[r["id"]] = subs
                fh.write(json.dumps({"id": r["id"], "subs": subs}, ensure_ascii=False) + "\n")
                fh.flush()

    out: list[dict] = []
    dropped: list[str] = []
    for r in rows:
        if not r["prep"]["subs"]:
            r["prep"]["subs"] = cache.get(r["id"]) or []
        if len(r["prep"]["subs"]) < 2:
            dropped.append(f"{r['id']}(grounded {len(r['prep']['subs'])} < 2 — 합성 LLM 미발화)")
            continue
        out.append(r)
    return out, dropped, freeze_cost


# --------------------------------------------------------------------------- #
# ChatModel shim — arena의 VendorClient(-> (text, usage))를 프로덕션 ChatModel
# 프로토콜(-> str)로 감싼다. **호출마다 새 인스턴스**를 만들어 usage가 스레드 간
# 섞이지 않게 한다(arena_run 모듈 docstring의 "공유 mutable state 없음" 규약).
# --------------------------------------------------------------------------- #
class ChatShim:
    def __init__(self, client: Any):
        self._client = client
        self.model_id = getattr(client, "model_id", "unknown")
        self.calls: list[dict] = []

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        text, usage = self._client.complete(system, user, json_only=json_only)
        self.calls.append(usage or {})
        return text

    @property
    def usage(self) -> dict:
        return {
            "prompt_tokens": sum((c.get("prompt_tokens") or 0) for c in self.calls) or None,
            "completion_tokens": sum((c.get("completion_tokens") or 0) for c in self.calls)
            or None,
            "n_calls": len(self.calls),
        }


# --------------------------------------------------------------------------- #
# 프로덕션 프롬프트 (chat 주입을 안 받는 호출부는 t12_generation.py처럼 재현/임포트)
# --------------------------------------------------------------------------- #
# `orthus/agentwork/service.py::_generate_command_email`의 system 문자열 그대로.
# 재현본이라 아래에서 원본 소스와 대조해 drift를 잡는다.
EMAIL_SYSTEM = (
    "당신은 회사 직원을 대신해 새 이메일 초안을 작성하는 비서다. "
    "사용자의 요청에 근거해 정중하고 전문적인 한국어 이메일을 작성한다. "
    "참고 자료가 주어지면 그 내용을 근거로 본문을 채운다. "
    "확실하지 않은 사실은 지어내지 않고, 필요한 빈칸은 대괄호 표시로 남긴다. "
    '오직 JSON만 출력한다: {"subject": "<제목>", "body": "<본문>"}'
)


def _email_user_prompt(to: str, inst: str, ctx: str) -> str:
    context_block = ""
    if ctx.strip():
        context_block = f"\n[참고 자료 — 요청의 '이 내용/이걸' 등이 가리키는 배경]\n{ctx.strip()}\n"
    return (
        f"받는 사람: {to}\n"
        f"요청: {inst}\n"
        f"{context_block}\n"
        "위 요청에 맞는 이메일 제목과 본문을 작성하라. 참고 자료가 있으면 그 내용을 근거로 "
        "본문을 구체적으로 채운다. 제목에 'Re:'를 붙이지 마라."
    )


def check_email_prompt_drift() -> str | None:
    """재현한 EMAIL_SYSTEM이 프로덕션 소스와 여전히 같은지 확인. 다르면 사유 문자열."""
    try:
        from orthus.agentwork.service import _generate_command_email

        src = inspect.getsource(_generate_command_email)
    except Exception as e:  # noqa: BLE001
        return f"프로덕션 소스 확인 불가: {type(e).__name__}"
    for line in EMAIL_SYSTEM.split(". "):
        probe = line.strip().strip('"')
        if probe and probe[:20] not in src:
            return "EMAIL_SYSTEM drift — service.py._generate_command_email과 불일치"
    if "제목에 'Re:'를 붙이지 마라" not in src:
        return "email user prompt drift — 'Re:' 제약 문구 불일치"
    return None


# --------------------------------------------------------------------------- #
# 태스크 실행기 — 각각 (raw_output, parsed, metrics, entry_point) 반환
# --------------------------------------------------------------------------- #
def _wiki_source_refs(hits: list[dict]):
    from orthus.schemas.canonical import WikiSourceRef

    return [WikiSourceRef(**h) for h in hits]


def _exec_wiki_qa(prep: dict, chat: ChatShim, *, prompt_only: bool) -> tuple:
    hits = _wiki_source_refs(prep["hits"])
    if prompt_only:
        from orthus.wiki.qa import _SYSTEM, _build_user_prompt

        answer = chat.complete(_SYSTEM, _build_user_prompt(prep["q"], hits, None))
        entry = "orthus.wiki.qa:_SYSTEM+_build_user_prompt"
    else:
        from orthus.wiki.qa import answer_from_hits

        result = answer_from_hits(
            UID, prep["q"], hits, chat_model=chat, learn=False, record_gaps=False
        )
        answer = result.answer
        entry = "orthus.wiki.qa.answer_from_hits"
    text = (answer or "").strip()
    # grounding = 모델에게 준 것 전부(질문 + 근거 문단). 질문에만 있는 고유명사를 답에 쓴 것은
    # 환각이 아니다 — t12의 email grounding(`to + inst + ctx`)과 같은 규약.
    grounding = prep["q"] + " " + " ".join(h["excerpt"] for h in prep["hits"])
    metrics = {
        "ok": bool(text),
        "chars": len(text),
        # 프로덕션 프롬프트가 명시 금지한 인용 마커. 위반은 프롬프트 미준수 지표다.
        "citation_marker": bool(_ANSWER_CITATION_RE().search(text)),
        "invented": _invented(text, grounding),
        # 입력 freeze 감사용 — 같은 id의 서로 다른 워커 행이 같은 근거를 봤는지 확인한다.
        "hit_slugs": [h["page_slug"] for h in prep["hits"]],
    }
    return text, {"answer": text}, metrics, entry


_CITATION_RE = None


def _ANSWER_CITATION_RE():
    """프로덕션의 인용마커 탐지 정규식을 그대로 재사용(자체 정의 금지)."""
    global _CITATION_RE
    if _CITATION_RE is None:
        from orthus.wiki.qa import _ANSWER_CITATION

        _CITATION_RE = _ANSWER_CITATION
    return _CITATION_RE


def _exec_synthesize(prep: dict, chat: ChatShim, *, prompt_only: bool) -> tuple:
    from orthus.schemas.canonical import (
        RoutedAnswer,
        SubAnswer,
        SubQuestion,
        WikiAnswer,
    )

    subqs: list[SubQuestion] = []
    subas: list[SubAnswer] = []
    for s in prep["subs"]:
        sid = uuid4()
        subqs.append(SubQuestion(id=sid, text=s["q"], scope="company"))
        routed = RoutedAnswer(
            question=s["q"],
            mode="wiki",
            wiki=WikiAnswer(question=s["q"], answer=s["body"], sources=[]),
        )
        subas.append(SubAnswer(sub_question_id=sid, routed=routed, grounded=True, text=s["q"]))

    if prompt_only:
        from orthus.router.decompose import _SYNTHESIZE_SYSTEM

        parts = [
            f"[{i + 1}] Sub-question: {s['q']}\nAnswer: {s['body']}"
            for i, s in enumerate(prep["subs"])
        ]
        user = (
            f"Original compound question: {prep['q']}\n\n"
            f"Sub-answers:\n" + "\n\n".join(parts) + "\n\n"
            "Synthesized answer:"
        )
        text = (chat.complete(_SYNTHESIZE_SYSTEM, user) or "").strip()
        entry = "orthus.router.decompose:_SYNTHESIZE_SYSTEM"
    else:
        from orthus.router.decompose import synthesize

        body = synthesize(prep["q"], subas, subqs, chat_model=chat)
        text = ((body.answer if body else "") or "").strip()
        entry = "orthus.router.decompose.synthesize"

    grounding = prep["q"] + " " + " ".join(
        f"{s['q']} {s['body']}" for s in prep["subs"]
    )
    metrics = {
        "ok": bool(text),
        "chars": len(text),
        "n_grounded": len(prep["subs"]),
        "invented": _invented(text, grounding),
    }
    return text, {"answer": text}, metrics, entry


def _exec_email_draft(prep: dict, chat: ChatShim, **_: Any) -> tuple:
    raw = (
        chat.complete(EMAIL_SYSTEM, _email_user_prompt(prep["to"], prep["inst"], prep["ctx"]), json_only=True)
        or ""
    ).strip()
    # 프로덕션은 여기서 json.loads 실패 시 **결정론 템플릿으로 조용히 떨어진다**.
    # 그 실패는 러너의 `error`(전송/API 실패)가 아니라 **측정 결과**다 — 이 태스크의 1차
    # 지표이기 때문이다(모델을 바꾼 의미가 0이 되는 실패). error로 올리면 재개 로직이
    # 같은 항목을 영원히 재시도하고, A.X처럼 구조적으로 JSON을 깨는 모델의 실패율이
    # 집계에서 사라진다.
    subject, body, fmt_err = "", "", None
    try:
        parsed_obj = json.loads(raw)
        subject = " ".join(str(parsed_obj.get("subject") or "").split())
        body = str(parsed_obj.get("body") or "").strip()
    except Exception as e:  # noqa: BLE001
        fmt_err = type(e).__name__
    grounding = f"{prep['to']} {prep['inst']} {prep['ctx']}"
    metrics = {
        "ok": bool(subject and body),
        "format_error": fmt_err,
        "re_prefix": subject.lower().startswith("re:"),
        "body_chars": len(body),
        "invented": _invented(f"{subject}\n{body}", grounding),
    }
    return raw, {"subject": subject, "body": body}, metrics, "agentwork.service._generate_command_email(prompt)"


def _exec_gap_suggest(prep: dict, chat: ChatShim, **_: Any) -> tuple:
    from orthus.wiki.gap import _SUGGEST_SYSTEM, _parse_suggestion

    raw = chat.complete(
        _SUGGEST_SYSTEM,
        f"Question: {prep['q']}\nReason it failed: {prep['reason']}",
        json_only=True,
    )
    # email_draft와 동일 규약: 파싱/형식 실패는 error가 아니라 측정 결과다(프로덕션은
    # 여기서 결정론 폴백으로 떨어진다).
    target, connector, sections, fmt_err = None, None, None, None
    try:
        target, connector, sections = _parse_suggestion(raw)
    except Exception as e:  # noqa: BLE001
        fmt_err = type(e).__name__
    n_sections = len(sections or [])
    metrics = {
        "ok": bool(target and sections),
        "format_error": fmt_err,
        "n_sections": n_sections,
        "n_items": sum(len(s.items) for s in (sections or [])),
        # 프롬프트가 명시한 2-4 섹션 규격
        "section_spec_violation": bool(n_sections < 2 or n_sections > 4),
    }
    parsed = {"target": target, "connector": connector, "n_sections": n_sections}
    return (raw or "").strip(), parsed, metrics, "orthus.wiki.gap:_SUGGEST_SYSTEM+_parse_suggestion"


def _exec_claim_headline(prep: dict, chat: ChatShim, **_: Any) -> tuple:
    from orthus.wiki.distill import HEADLINE_SYSTEM

    raw = (chat.complete(HEADLINE_SYSTEM, prep["claim"]) or "").strip()
    head = " ".join(raw.split())
    metrics = {
        "ok": bool(head),
        "chars": len(head),
        # _HEADLINE_MAX=120 초과분은 프로덕션 `_one_line_cap`이 말줄임표로 자른다.
        "over_cap": len(head) > 120,
        # latin=False — 영문 Title Case 출력에서 "대문자=고유명사" 가정이 깨진다(t12 주석).
        "invented": _invented(head, prep["claim"], latin=False),
    }
    return raw, {"headline": head}, metrics, "orthus.wiki.distill:HEADLINE_SYSTEM"


EXECUTORS = {
    "wiki_qa": _exec_wiki_qa,
    "synthesize": _exec_synthesize,
    "email_draft": _exec_email_draft,
    "gap_suggest": _exec_gap_suggest,
    "claim_headline": _exec_claim_headline,
}


# --------------------------------------------------------------------------- #
# 체크포인트 (arena_run.load_done_pairs와 동일 규약, 키만 다름)
# --------------------------------------------------------------------------- #
def load_done_ids(out_path: Path) -> set[str]:
    """재개 대상 = **에러 없이 완료된** id만. 같은 id는 마지막 행이 이긴다."""
    if not out_path.exists():
        return set()
    ok: dict[str, bool] = {}
    for line in out_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        ok[str(d.get("id"))] = not d.get("error")
    return {k for k, clean in ok.items() if clean}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# DB 가용성 (audit 스팬용 Postgres) — 없으면 프롬프트 레벨 경로로 자동 강등
# --------------------------------------------------------------------------- #
# 시스템 슬러그 -> 그 클라이언트가 **실제로 써야 하는** 키 env 이름.
# `arena_run.build_client`는 Bedrock 계열에서 `ORTHUS_LLM_BEDROCK_API_KEY or ORTHUS_LLM_API_KEY`
# 폴백을 쓰는데, 그 폴백이 발동하면 Bedrock에 OpenAI 키가 실려 403이 난다. arena_run.py는
# 출처 보존을 위해 **바이트 그대로** 두고, 대신 여기서 폴백이 발동할 상황 자체를 차단한다.
REQUIRED_KEY_ENV: dict[str, str] = {
    "solar": "ORTHUS_LLM_SOLAR_API_KEY",
    "exaone": "ORTHUS_LLM_EXAONE_API_KEY",
    "ax": "ORTHUS_LLM_AX_API_KEY",
    "gpt-5.6-sol": "ORTHUS_LLM_API_KEY",
    "deepseek-v4-pro": "ORTHUS_LLM_DEEPSEEK_API_KEY",
    "claude-opus-4.8": "ORTHUS_LLM_BEDROCK_API_KEY",
    "glm-5-bedrock": "ORTHUS_LLM_BEDROCK_API_KEY",
}


def preflight_vendor_keys(systems: tuple[str, ...]) -> None:
    """선택한 시스템의 키가 실제로 비어 있지 않은지 확인. 비면 즉시 중단.

    특히 Bedrock 2종은 `ORTHUS_LLM_API_KEY`(OpenAI) 폴백을 **허용하지 않는다** — 조용히
    엉뚱한 벤더 키로 붙어 403을 맞는 것보다 여기서 멈추는 게 낫다(2026-07-29 실측).
    """
    missing: list[str] = []
    for s in systems:
        env_name = REQUIRED_KEY_ENV.get(s)
        if env_name and not (os.environ.get(env_name) or "").strip():
            missing.append(f"{s} -> {env_name}")
    if missing:
        raise SystemExit(
            "  [FATAL] 벤더 키 미설정(빈 값 포함): " + ", ".join(missing) + "\n"
            "  node.env에 **빈 값**으로 선언돼 있으면 저장소 .env의 실제 키를 가린다. "
            "이 러너는 빈 값을 미설정으로 보고 보충하지만, 두 파일 다 비어 있으면 여기서 멈춘다."
        )


def preflight_corpus(*, min_chunks: int, allow_small: bool, hard: bool) -> None:
    """붙은 DB 이름 · wiki 규모 · 임베딩 model_version 정합성을 시작 시 1회 점검/출력.

    2026-07-29 사고 재발 방지: 저장소 `.env` DSN은 3-chunk dev 픽스처(`orthus`)를 가리키는데
    거기 붙은 채로 wiki_qa를 돌리면 모든 문항이 같은 근거(`claim-a/claim-b/page-x`)를 받아
    측정이 통째로 무의미해진다. 그때 실패는 조용했다 — 그래서 여기서 **크게 실패**시킨다.

    `hard=True`(wiki_qa/synthesize가 실행 대상)일 때만 임계 미달을 치명으로 본다.
    `model_version`이 2종 이상이면 검색 순위가 난수화된다(혼재 = 조용한 오답,
    `docs/model-orchestration.md` §14.6) — 치명은 아니지만 크게 경고한다.
    """
    from sqlalchemy import text as sql

    from orthus.db import session

    with session() as s:
        db = s.execute(sql("select current_database()")).scalar()
        n_chunks = s.execute(sql("select count(*) from wiki_chunks")).scalar() or 0
        n_pages = s.execute(sql("select count(*) from wiki_pages")).scalar() or 0
        versions = s.execute(
            sql(
                "select model_version, count(*) from embeddings "
                "where kind = 'wiki_chunk' group by 1 order by 2 desc"
            )
        ).all()
    vtxt = ", ".join(f"{v}×{c}" for v, c in versions) or "(없음)"
    print(f"  DB: {db} | wiki_chunks={n_chunks} wiki_pages={n_pages}")
    print(f"  embedding model_version(wiki_chunk): {vtxt}")
    if len(versions) > 1:
        print(
            "  [WARN] 임베딩 model_version이 2종 이상이다 — 옛 벡터가 난수 점수로 순위 경쟁에 "
            "끼어들어 검색이 '깨짐'이 아니라 '조용한 오답'이 된다. 전량 재임베딩 상태를 확인하라."
        )
    if n_chunks < min_chunks:
        msg = (
            f"  [FATAL] wiki_chunks={n_chunks} < {min_chunks} — 회사 위키가 아닌 DB({db})에 "
            "붙었다. 이대로 돌리면 모든 문항이 같은 근거를 받아 측정이 무의미해진다. "
            f"--env-file {DEFAULT_NODE_ENV} 를 확인하라(강행은 --allow-small-corpus)."
        )
        if allow_small or not hard:
            print(msg.replace("[FATAL]", "[WARN]"))
        else:
            raise SystemExit(msg)


def probe_db() -> str | None:
    try:
        from orthus.audit.logger import audit

        with audit("remaining_run.probe") as span:
            span.add_meta(probe=True)
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {str(e)[:160]}"


# --------------------------------------------------------------------------- #
# 실행
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    systems = ROSTER if args.system == "all" else tuple(
        s.strip() for s in args.system.split(",") if s.strip()
    )
    unknown = [s for s in systems if s not in SYSTEMS]
    if unknown:
        raise SystemExit(f"unknown --system {unknown}; arena_run.SYSTEMS={list(SYSTEMS)}")
    off_roster = [s for s in systems if s not in ROSTER]
    if off_roster:
        print(f"[warn] 확정 로스터 밖 시스템 포함: {off_roster}")

    tasks = TASKS if args.tasks == "all" else tuple(
        t.strip() for t in args.tasks.split(",") if t.strip()
    )
    bad = [t for t in tasks if t not in TASKS]
    if bad:
        raise SystemExit(f"unknown --tasks {bad}; valid={list(TASKS)}")

    overrides: dict[str, str] = {}
    for spec in args.golden or []:
        if "=" not in spec:
            raise SystemExit(f"--golden expects task=path, got {spec!r}")
        k, v = spec.split("=", 1)
        overrides[k.strip()] = v.strip()

    golden_dir = Path(args.golden_dir) if args.golden_dir else DEFAULT_GOLDEN_DIR
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 벤더 키 preflight (빈 값 마스킹 / 잘못된 폴백 차단) ─────────────────
    if not args.dry_run:
        preflight_vendor_keys(systems)

    # ── DB preflight (붙은 DB · 위키 규모 · 임베딩 정합성) ──────────────────
    needs_wiki_db = bool({"wiki_qa", "synthesize"} & set(tasks))
    try:
        preflight_corpus(
            min_chunks=args.min_wiki_chunks,
            allow_small=args.allow_small_corpus,
            hard=needs_wiki_db,
        )
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — DB 자체가 없으면 아래 probe_db가 강등 처리
        print(f"  [warn] DB preflight 실패: {type(e).__name__}: {str(e)[:160]}")

    # ── 데이터 로드 + 정규화 ────────────────────────────────────────────────
    prepared: dict[str, list[dict]] = {}
    labels: dict[str, str] = {}
    print(f"== remaining_run == systems={list(systems)} tasks={list(tasks)} "
          f"canary={args.canary} dry_run={args.dry_run}")
    for task in tasks:
        items, label = load_task_items(task, golden_dir, overrides.get(task))
        labels[task] = label
        rows: list[dict] = []
        skipped: list[str] = []
        for it in items:
            try:
                rows.append({"id": str(it["id"]), "prep": _prepare(task, it)})
            except ValueError as e:
                skipped.append(f"{it.get('id')}({e})")
        rows.sort(key=lambda r: r["id"])
        if args.canary:
            rows = rows[: args.canary]
        if task == "wiki_qa":
            cache = (
                Path(args.hits_cache) if args.hits_cache else out_dir / "wiki_qa_hits.jsonl"
            )
            rows, dropped = freeze_wiki_qa_hits(
                rows,
                k=args.wiki_qa_k,
                scope=args.wiki_qa_scope,
                cache_path=cache,
                dry_run=args.dry_run,
            )
            skipped.extend(dropped)
        elif task == "synthesize":
            cache = (
                Path(args.subs_cache) if args.subs_cache else out_dir / "synthesize_subs.jsonl"
            )
            rows, dropped, fcost = freeze_synthesize_inputs(
                rows,
                cache_path=cache,
                dry_run=args.dry_run,
                scope=args.wiki_qa_scope,
            )
            skipped.extend(dropped)
            if fcost:
                print(f"      freeze 추정 비용(고정 {FREEZE_MODEL}): ${fcost:.4f}")
        prepared[task] = rows
        note = "  ⚠️ 내장 더미(골든 없음)" if label == "dummy" else ""
        print(f"  {task:15} golden={label:24} items={len(rows):4}{note}")
        if skipped:
            print(f"      제외 {len(skipped)}: {', '.join(skipped[:5])}"
                  + (" …" if len(skipped) > 5 else ""))

    if args.freeze_only:
        # 입력 freeze(wiki_qa hits / synthesize sub-answers)만 수행하고 워커 호출은 하지 않는다.
        # 골든이 나온 직후 freeze를 선행 실행해 캐시를 확보하는 용도 — 이후 본실행은 캐시를
        # 재사용하므로 워커별 재freeze가 구조적으로 불가능해진다(입력 동일성 보장).
        print("\n  --freeze-only: 입력 freeze만 수행하고 종료(워커 호출 없음).")
        for task in tasks:
            if task in ("wiki_qa", "synthesize"):
                print(f"    {task:15} freeze 확보 문항 {len(prepared[task])}건")
            else:
                print(f"    {task:15} freeze 대상 아님(스킵)")
        return 0

    drift = check_email_prompt_drift()
    if drift and "email_draft" in tasks:
        print(f"  [warn] {drift}")

    # ── DB / 진입점 모드 ────────────────────────────────────────────────────
    prompt_only = bool(args.prompt_only)
    if not prompt_only and ({"wiki_qa", "synthesize"} & set(tasks)):
        err = probe_db()
        if err:
            prompt_only = True
            print(f"  [warn] audit DB 사용 불가 → 프롬프트 레벨 경로로 강등: {err}")
        else:
            print("  audit DB OK — 프로덕션 함수(answer_from_hits/synthesize) 직접 호출")

    # ── 클라이언트 ──────────────────────────────────────────────────────────
    clients: dict[str, Any] = {}
    for s in systems:
        if args.dry_run:
            clients[s] = DryRunClient()
            continue
        c = build_client(s)
        expected = EXPECTED_MODEL_ID.get(s)
        got = getattr(c, "model_id", "?")
        if expected and got != expected:
            raise SystemExit(
                f"{s}: model_id pin 불일치 — expected {expected!r}, got {got!r}. "
                "env override(ORTHUS_LLM_*_MODEL_OVERRIDE / *_INFERENCE_PREFIX)를 확인하라."
            )
        clients[s] = c
        print(f"  client {s:17} model_id={got}")

    # ── work items + 재개 ───────────────────────────────────────────────────
    work: list[dict] = []
    for s in systems:
        for task in tasks:
            out_path = out_dir / f"{task}__{s}.jsonl"
            done = load_done_ids(out_path)
            todo = [r for r in prepared[task] if r["id"] not in done]
            if done:
                print(f"  resume {task}/{s}: done={len(done)} remaining={len(todo)}")
            for r in todo:
                work.append({"task": task, "system": s, "id": r["id"], "prep": r["prep"],
                             "out": out_path, "golden": labels[task]})
    work.sort(key=lambda w: (w["task"], w["id"], w["system"]))
    if args.limit:
        work = work[: args.limit]
    print(f"\n  work items: {len(work)}")
    if not work:
        print("  nothing to do — 전부 체크포인트됨.")
        return 0

    stop_event = threading.Event()
    cost_lock = threading.Lock()
    write_locks: dict[Path, threading.Lock] = {}
    lock_guard = threading.Lock()
    total_cost = [0.0]
    completed = [0]
    errors: list[str] = []
    stopped_for_cost = [False]

    def _lock_for(p: Path) -> threading.Lock:
        with lock_guard:
            return write_locks.setdefault(p, threading.Lock())

    def process(item: dict) -> None:
        if stop_event.is_set():
            return
        chat = ChatShim(clients[item["system"]])
        t0 = time.monotonic()
        raw_output = None
        parsed: dict = {}
        metrics: dict = {}
        entry = ""
        error: str | None = None
        try:
            fn = EXECUTORS[item["task"]]
            raw_output, parsed, metrics, entry = fn(
                item["prep"], chat, prompt_only=prompt_only
            )
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {str(e)[:300]}"
        latency_ms = int((time.monotonic() - t0) * 1000)

        row = {
            "task": item["task"],
            "id": item["id"],
            "system": item["system"],
            "model_id": chat.model_id,
            "entry_point": entry,
            "golden": item["golden"],
            "raw_output": raw_output,
            "parsed": parsed,
            "metrics": metrics,
            "usage": chat.usage,
            "latency_ms": latency_ms,
            "timestamp": _now_iso(),
            "error": error,
        }
        with _lock_for(item["out"]):
            with open(item["out"], "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            completed[0] += 1

        if error:
            with cost_lock:
                errors.append(f"{item['task']}/{item['system']}/{item['id']}: {error}")
            return
        price_in, price_out = PRICING[item["system"]]
        cost = estimate_cost_usd(chat.usage, price_in, price_out)
        with cost_lock:
            total_cost[0] += cost
            if total_cost[0] >= args.cost_cap_usd and not stop_event.is_set():
                stop_event.set()
                stopped_for_cost[0] = True
                print(
                    f"\n[cost-cap] 누적 추정 ${total_cost[0]:.4f} >= cap "
                    f"${args.cost_cap_usd:.2f} — 신규 제출 중단(체크포인트 저장됨, 재개 가능)."
                )

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = []
        for item in work:
            if stop_event.is_set():
                break
            futures.append(pool.submit(process, item))
        for fut in as_completed(futures):
            fut.result()

    label = "mock cost (dry-run, 실제 $ 아님)" if args.dry_run else "이번 실행 누적 추정 비용"
    print(f"\n  wrote {completed[0]} rows -> {out_dir}")
    print(f"  {label}: ${total_cost[0]:.4f}")
    if errors:
        print(f"\n  실패 {len(errors)}건:")
        for e in errors[:20]:
            print(f"    {e}")
        # node.env와 저장소 .env가 **서로 다른** OpenAI 키를 갖고 있고(2026-07-29 실측,
        # sha8 672635fd vs dffbf1b7), node.env 쪽이 지속적으로 429다. env 우선순위상
        # node.env가 이기므로 gpt 계열만 골라 셸 env로 덮는 것이 정석 회피다.
        if any("429" in e and "gpt" in e for e in errors):
            print(
                "\n  [hint] OpenAI 429 — node.env의 ORTHUS_LLM_API_KEY가 저장소 .env와 다른 키이고 "
                "쿼터가 막혀 있다. 셸 env가 두 파일보다 우선하므로 "
                "`export ORTHUS_LLM_API_KEY=<저장소 .env 값>` 후 재실행하면 통과한다(실측)."
            )
    left = len(work) - completed[0]
    if left > 0:
        why = "cost cap" if stopped_for_cost[0] else "중단"
        print(f"  미실행 {left}건 ({why}) — 같은 명령을 다시 돌리면 재개된다.")
    return 1 if errors and completed[0] == len(errors) else 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--system", default="all", help=f"콤마 목록 또는 all. 로스터: {list(ROSTER)}")
    ap.add_argument("--tasks", default="all", help=f"콤마 목록 또는 all. {list(TASKS)}")
    ap.add_argument("--limit", type=int, default=0, help="총 work item 상한, 0=무제한")
    ap.add_argument("--canary", type=int, default=0, help="태스크·시스템당 N문항만")
    ap.add_argument("--cost-cap-usd", type=float, default=20.0, help="누적 추정 비용 상한(USD)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument(
        "--env-file",
        default=None,
        help=f"1순위 env (기본 {DEFAULT_NODE_ENV} — 회사 노드 DB). 저장소 .env는 빈 키 보충용으로 항상 뒤에 로드된다.",
    )
    ap.add_argument(
        "--min-wiki-chunks",
        type=int,
        default=1000,
        help="wiki_qa/synthesize 실행 전 요구하는 최소 wiki_chunks 행 수(기본 1000)",
    )
    ap.add_argument(
        "--allow-small-corpus",
        action="store_true",
        help="위 임계 미달이어도 강행(픽스처 DB에 붙은 걸 알고도 돌릴 때만)",
    )
    ap.add_argument("--out-dir", default=None, help=f"기본 {DEFAULT_OUT_DIR}")
    ap.add_argument("--golden-dir", default=None, help=f"기본 {DEFAULT_GOLDEN_DIR}")
    ap.add_argument("--golden", action="append", help="task=path 로 골든 파일 강제(반복 가능)")
    ap.add_argument("--wiki-qa-k", type=int, default=5, help="hits 없는 wiki_qa 골든의 retrieve k")
    ap.add_argument("--wiki-qa-scope", default="company", help="retrieve scope (company|all|personal)")
    ap.add_argument("--hits-cache", default=None, help="wiki_qa frozen hits 캐시(기본 <out-dir>/wiki_qa_hits.jsonl)")
    ap.add_argument("--subs-cache", default=None, help="synthesize frozen sub-answers 캐시(기본 <out-dir>/synthesize_subs.jsonl)")
    ap.add_argument(
        "--freeze-only",
        action="store_true",
        help="입력 freeze(wiki_qa hits / synthesize sub-answers)만 수행하고 종료. "
        "골든이 나온 직후 선행 실행해 캐시를 확보하는 용도. --dry-run과 함께 쓰면 콜 수/비용/시간 추정만 출력.",
    )
    ap.add_argument(
        "--prompt-only",
        action="store_true",
        help="wiki_qa/synthesize를 프로덕션 함수 대신 프롬프트 레벨로 실행(DB 불필요)",
    )
    ap.add_argument("--dry-run", action="store_true", help="네트워크 호출 없이 배선/비용/재개만 검증")
    args = ap.parse_args()

    # env는 dry-run에서도 로드한다(arena와 다른 점): DB DSN이 여기서 오고, 프로덕션 함수
    # 경로를 dry-run으로도 실제로 태워 봐야 배선 검증이 되기 때문.
    #
    # ⚠️ 기본값이 **회사 노드 env**다(`~/.orthus/nodes/company/node.env`). 이 실험의 대상은
    # 회사 위키이고, 저장소 `.env`의 `ORTHUS_PG_DSN`은 3-chunk dev 픽스처 DB(`orthus`)를
    # 가리킨다 — 2026-07-29에 실제로 그 픽스처에 붙어 wiki_qa 근거가 전부 같은
    # `claim-a/claim-b/page-x`로 나온 사고가 있었다. node.env는 `orthus_company`(28,186 chunk)다.
    # node.env에 없는 벤더 키(ax/exaone/deepseek/glm)는 저장소 `.env`에서 **보충만** 한다
    # (override=False라 node.env 값이 항상 이긴다).
    from dotenv import dotenv_values, load_dotenv

    def _fill_blank(path: Path) -> None:
        """현재 값이 **비어 있는** 키만 보충한다(빈 문자열 = 미설정으로 간주).

        `load_dotenv(override=False)`는 "이미 os.environ에 키가 있으면 건너뛴다"라서, 먼저 로드한
        node.env가 `ORTHUS_LLM_BEDROCK_API_KEY=`(빈 값)를 선언해 두면 뒤에 오는 저장소 .env의
        **진짜 키가 영원히 가려진다**. 그러면 `arena_run.build_client`의
        `... or os.environ.get("ORTHUS_LLM_API_KEY")` 폴백이 발동해 Bedrock에 OpenAI 키가 실리고
        403 "Invalid API Key format: Must start with pre-defined prefix"로 죽는다
        (2026-07-29 실측, claude-opus-4.8 / glm-5-bedrock 둘 다 재현).
        """
        for k, v in (dotenv_values(path) or {}).items():
            if v and not (os.environ.get(k) or "").strip():
                os.environ[k] = v

    loaded: list[str] = []
    primary = Path(args.env_file) if args.env_file else DEFAULT_NODE_ENV
    if primary.exists():
        load_dotenv(primary, override=False)
        loaded.append(str(primary))
    elif args.env_file:
        raise SystemExit(f"--env-file 없음: {primary}")
    else:
        print(f"[warn] 기본 node env 없음: {primary}")
    for cand in (_WORKTREE_ROOT / ".env", _REPO_ROOT / ".env"):
        if cand.exists() and str(cand) not in loaded:
            _fill_blank(cand)  # 빈 값도 미설정으로 보고 보충
            loaded.append(str(cand))
    if not loaded:
        print("[warn] env 파일 없음 — 키/DSN은 셸 환경에 있어야 한다")
    else:
        print(f"  env: {' + '.join(loaded)}")

    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
