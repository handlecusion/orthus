"""평가용 자연어 질문을 생성기별로 만든다 (생성기 편향 탐지용).

왜 여러 생성기인가: 질문을 gpt-4o-mini로만 만들면 GPT가 쓴 표현이 GPT 임베딩에
유리할 수 있다(생성기 편향). 같은 83개 페이지에 대해 gpt/claude/solar가 각각 질문을
쓰면, 각 임베딩 모델이 "자기 벤더가 쓴 질문"에서만 유독 잘하는지 교차 확인할 수 있다.

**표본 페이지 83개는 3종이 공유한다** — 같은 문제를 다르게 물어본 것이어야 비교가 성립한다.

출력: cases_{generator}.json (회사 콘텐츠 포함 → gitignore)

사용:
    python gen_questions.py --generator gpt      # OpenAI (prod 슬롯)
    python gen_questions.py --generator claude   # 로컬 claude CLI
    python gen_questions.py --generator solar    # Upstage — 회사 데이터 외부 전송!
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from orthus.tables import wiki_pages, wiki_chunks

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_PATH = f"{HERE}/sample_pages.json"
# 표본 페이지 표집 시드. 초기 구현은 `ORDER BY random()`이라 시드가 없었고, 결과 파일이
# 지워지자 **평가셋을 재현할 수 없게 됐다**(선행 실험 수치와 직접 비교 불가). 표집을
# Python 쪽으로 옮기고 시드를 박아 같은 사고를 반복하지 않는다.
SAMPLE_SEED = int(os.environ.get("FUGU_SAMPLE_SEED", "20260717"))
KEYS_PATH = os.environ.get(
    "FUGU_KEYS", "<로컬 키 저장소>/keys.json"
)

# --- 질문 스타일 ---
# `verbose`(초기 기본): 길고 격식 있는 완전한 의문문. 실측 결과 실제 wiki 질의와 어긋난다
#   (평가셋 34자·의문형 94% vs 실제 19~26자·62~68%).
# `real`: **실제 wiki 분기 질의 분포에 맞춘다.** 근거는 두 테이블의 실측(2026-07-16, n=72):
#   - `ask_cache.question_redacted` 19건(답을 찾은 질의): 길이 p50 **19자**, 의문형 **47%**
#     예) "배포 프로세스가 어떻게 돼" / "nova가 어떤 서비스인지 설명해줘" / "지금 어떤 ai 툴 쓰고 있더라"
#   - `data_gaps.question` 53건(답을 못 찾은 질의): 길이 p50 **26자**, 의문형 **49%**
#   - **합계: 길이 p50 21자 / 의문형 49%** ← real 스타일의 목표값
#   (초기엔 62~68%로 쟀는데 판정기가 "찾아줘"의 `줘`를 의문형으로 셌다 — 지시형은 제외해야 한다.)
#   ⚠️ `query_runs`(5,635건)는 **structured(NL→SQL) 분기 로그라 임베딩을 타지 않는다** —
#      모집단이 다르므로 이 비교에 쓰면 안 된다(초기 문서의 "실제 48%"는 그 오류였다).
_SHARED_RULES = (
    "제약: (1) 문서 제목의 단어를 그대로(부분 문자열로도) 포함하지 마라 — 같은 뜻을 다른 표현으로 바꿔써라. "
    "(2) 문서 내용에 있는 구체적인 사실/키워드를 넣어 어느 문서를 찾는지 알 수 있게 하라. "
    '(3) 반드시 JSON 객체 {"question": "..."} 형식으로만 답하라.'
)

STYLES = {
    "verbose": (
        "너는 사내 위키 검색 품질 평가용 자연어 질문을 만든다. "
        "주어진 문서 제목과 내용을 보고, 그 문서를 찾기 위해 실제 사용자가 물어볼 법한 "
        "한국어 질문을 정확히 1개만 만들어라. " + _SHARED_RULES
    ),
    # 형태(의문형/키워드형)는 **호출마다 지정**한다 — 각 호출은 페이지 1개만 보므로
    # "3개 중 2개꼴" 같은 비율 지시를 스스로 지킬 수 없다(실측: 100% 의문형이 나왔다).
    "real": (
        "너는 사내 위키 검색창에 **실제 직원이 치는 짧은 질의**를 만든다. "
        "주어진 문서 제목과 내용을 보고, 그 문서를 찾으려고 칠 법한 한국어 질의를 1개만 만들어라. "
        "스타일(반드시 지켜라): "
        "(a) **18~25자로 짧게.** 격식 없는 구어체. 존댓말·완전한 문장 금지. "
        "(b) 아래 [형태] 지시를 정확히 따라라. "
        "(c) 실제 예시 톤: ‘배포 프로세스가 어떻게 돼’ / ‘nova가 어떤 서비스야’ / "
        "‘지금 어떤 ai 툴 쓰고 있더라’ / ‘출연자 프로필 관리는 어떻게 해’. " + _SHARED_RULES
    ),
}

# real 스타일의 형태 배분. 실측 의문형 **49%**(n=72)에 맞춰 절반만 의문형으로 둔다.
_REAL_FORMS = {
    "q": "[형태] **의문형**으로 써라 — ‘~가 어떻게 돼’, ‘~ 뭐야’, ‘~ 어떻게 해’ 같은 반말 물음.",
    "kw": "[형태] **의문형 금지.** 명사구나 지시형으로 써라 — "
    "‘~ 문서 찾아줘’, ‘~ 정리해줘’, 또는 그냥 키워드 나열. 물음표 쓰지 마라.",
}


_QWORD = re.compile(r"(뭐|무엇|무슨|어떻게|어떤|어디|언제|누가|누구|왜|몇|얼마|알려|설명|있나|있어|되나|돼|하나)")
_IMPER = re.compile(r"(찾아줘|정리해줘|보여줘|알려줘|만들어줘|세줘|집계해줘|줘$)")


def is_interrogative(t: str) -> bool:
    """의문형 판정. **지시형('찾아줘')을 의문형으로 세면 안 된다** — 초기 판정기가 `줘`로
    끝나면 전부 의문형으로 세서 실제 분포를 49%가 아니라 68%로 과대 측정했다."""
    t = t.strip()
    if t.endswith("?"):
        return True
    if _IMPER.search(t):
        return False
    if re.search(r"(까|나요|는지|니)$", t.rstrip(". ")):
        return True
    return bool(_QWORD.search(t)) and bool(re.search(r"(야|어|해|돼|되|나|지)$", t.rstrip(". ")))


def form_for(style: str, index: int) -> str:
    """항목 index의 형태 지시(결정론). real이 아니면 빈 문자열."""
    if style != "real":
        return ""
    return _REAL_FORMS["kw" if index % 2 == 1 else "q"]  # 1/2 의문형 (실측 49%)


def build_chat(generator: str):
    """생성기별 ChatModel(Protocol 동일)을 만든다."""
    if generator == "gpt":
        from orthus.models.adapters.openai_compat import OpenAIChat

        return OpenAIChat(
            os.environ["ORTHUS_LLM_BASE_URL"],
            os.environ["ORTHUS_LLM_API_KEY"],
            os.environ["ORTHUS_LLM_MODEL"],
        ), os.environ["ORTHUS_LLM_MODEL"]

    if generator == "claude":
        from orthus.models.adapters.cli import CLIChat

        cmd = os.environ.get("ORTHUS_QGEN_CLAUDE_CMD", "claude -p")
        # 로컬 claude CLI. OAuth/keychain 로그인을 쓰므로 --bare 금지(AGENTS.md).
        return CLIChat(cmd, timeout=120.0), cmd

    if generator == "solar":
        from orthus.models.adapters.openai_compat import OpenAIChat

        raw = json.load(open(KEYS_PATH, encoding="utf-8"))
        entry = next(e for e in raw if "upstage" in str(e.get("provider", "")).lower())
        return OpenAIChat(
            "https://api.upstage.ai/v1", entry["key"], "solar-pro", timeout=40.0
        ), "solar-pro"

    raise ValueError(f"unknown generator: {generator}")


def load_base(engine) -> list[dict]:
    """공유 표본 83페이지. 없으면 현재 DB에서 층화 샘플링해 새로 만든다.

    표집은 **DB가 아니라 Python에서 시드를 걸어** 한다. `ORDER BY random()`은 (1) 시드가
    없어 재현이 안 되고 (2) 전체 조인 위 무작위 정렬이라 orthus_ro statement_timeout을
    넘길 수 있다(mirror_retrieve.build_pool이 같은 이유로 이미 Python 표집을 쓴다).
    후보를 slug로 정렬해 넘기므로 DB 스캔 순서가 바뀌어도 같은 표본이 나온다.
    """
    if os.path.exists(BASE_PATH):
        return json.load(open(BASE_PATH, encoding="utf-8"))

    from sqlalchemy import func
    from orthus.tables import embeddings

    QUOTA = {"atlas": 35, "nova": 25, "company": 15, "orbit": 8}
    rng = random.Random(SAMPLE_SEED)
    pages: list[dict] = []
    with Session(engine) as s:
        for project, n in QUOTA.items():
            ids = (
                select(wiki_pages.c.page_id)
                .select_from(
                    wiki_pages.join(wiki_chunks, wiki_chunks.c.page_id == wiki_pages.c.page_id).join(
                        embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id
                    )
                )
                .where(
                    wiki_pages.c.scope == "company",
                    wiki_pages.c.kind == "page",
                    embeddings.c.project == project,
                    func.length(wiki_pages.c.title) >= 4,
                )
                .distinct()
                .subquery()
            )
            rows = s.execute(
                select(wiki_pages.c.slug, wiki_pages.c.title)
                .where(wiki_pages.c.page_id.in_(select(ids)))
                .order_by(wiki_pages.c.slug)  # 표집 입력을 결정론 순서로 고정
            ).all()
            picked = rng.sample(rows, min(n, len(rows)))
            if len(rows) < n:
                print(f"[base] ⚠️ {project}: 후보 {len(rows)}개 < 할당 {n}개 — 전부 사용")
            for slug, title in picked:
                pages.append(
                    {
                        "case_id": f"{project}-{slug[:40]}",
                        "expected_slug": slug,
                        "title": title,
                        "project": project,
                    }
                )
    json.dump(pages, open(BASE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[base] 새 표본 {len(pages)}페이지 → {BASE_PATH} (seed={SAMPLE_SEED})")
    return pages


def read_engine(timeout_ms: int = 60_000):
    """orthus_ro read-only 엔진. statement_timeout을 이 연결에 한해 늘린다.

    역할 기본값은 10s인데, 이 박스는 dev server/agent가 붙으면 load가 8코어를 넘겨
    4ms짜리 인덱스 쿼리도 CPU 굶주림으로 10s를 넘길 수 있다(실측: load 13). 역할 설정을
    바꾸지 않고 이 세션에만 여유를 준다."""
    return create_engine(
        os.environ["ORTHUS_PG_DSN_READONLY"],
        connect_args={"options": f"-c statement_timeout={timeout_ms}"},
    )


def fetch_contents(engine, pages: list[dict], limit: int = 3) -> dict[str, str]:
    """모든 표본 페이지 본문을 **한 번에** 읽고 세션을 닫는다.

    페이지마다 LLM 호출 사이에 DB를 다시 만지면 세션이 `idle in transaction`으로 83분간
    열려 있게 된다(실측). 읽기를 앞에 몰아 트랜잭션을 짧게 끝낸다."""
    slugs = [p["expected_slug"] for p in pages]
    with Session(engine) as s:
        rows = s.execute(
            select(wiki_pages.c.slug, wiki_chunks.c.content, wiki_chunks.c.ordinal)
            .select_from(
                wiki_chunks.join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
            )
            .where(wiki_pages.c.slug.in_(slugs))
            .order_by(wiki_pages.c.slug, wiki_chunks.c.ordinal)
        ).all()
    buckets: dict[str, list[str]] = {}
    for slug, content, _ in rows:
        b = buckets.setdefault(slug, [])
        if len(b) < limit:
            b.append(content)
    return {slug: "\n".join(v)[:1500] for slug, v in buckets.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description="생성기별 평가 질문 생성")
    ap.add_argument("--generator", required=True, choices=["gpt", "claude", "solar"])
    ap.add_argument(
        "--style", default="verbose", choices=sorted(STYLES),
        help="real = 실제 wiki 질의 분포(19~26자, 의문형 ~65%%, 구어체)에 맞춘다. "
        "verbose = 초기 스타일(34자, 의문형 94%%) — 실제와 어긋난다.",
    )
    ap.add_argument("--limit", type=int, default=0, help="앞 N개만(스모크용). 0=전체")
    ap.add_argument(
        "--resume",
        action="store_true",
        help="이미 만든 질문은 건너뛰고 빠진 페이지만 채운다(과부하로 일부 실패했을 때).",
    )
    args = ap.parse_args()

    engine = read_engine()
    pages = load_base(engine)
    # 형태 배분을 표본 원래 순서에 고정한다 — resume/limit으로 목록이 줄어도 같은 페이지는
    # 항상 같은 형태를 받아 재현 가능하고 비율이 유지된다.
    base_index = {p["expected_slug"]: i for i, p in enumerate(pages)}
    if args.limit:
        pages = pages[: args.limit]

    if args.generator == "solar":
        print("⚠️  solar 생성기는 회사 위키 원문을 Upstage 외부 API로 전송한다.")

    suffix = "" if args.style == "verbose" else f"-{args.style}"
    out_path = f"{HERE}/cases_{args.generator}{suffix}.json"
    cases: list[dict] = []
    if args.resume and os.path.exists(out_path):
        cases = json.load(open(out_path, encoding="utf-8"))
        have = {c["expected_slug"] for c in cases}
        todo = [p for p in pages if p["expected_slug"] not in have]
        print(f"[resume] 기존 {len(cases)}개 유지, 빠진 {len(todo)}개만 생성")
        pages = todo
        if not pages:
            print("빠진 항목 없음 — 할 일 없다.")
            return

    # DB 읽기를 앞에 몰아 끝낸다 — LLM 루프 도중엔 DB를 만지지 않는다.
    contents = fetch_contents(engine, pages)
    print(f"본문 로드 완료: {len(contents)}/{len(pages)} 페이지 (DB 세션 종료)")

    chat, model_label = build_chat(args.generator)

    failed = 0
    for i, p in enumerate(pages):
        # 형태 지시는 항목 index로 결정한다(2/3 의문형). --resume으로 이어 할 때 index가
        # 밀리면 비율이 어긋나므로 표본 내 원래 위치를 쓴다.
        form = form_for(args.style, base_index.get(p["expected_slug"], i))
        user = f"제목: {p['title']}\n\n내용:\n{contents.get(p['expected_slug'], '')}"
        if form:
            user = f"{form}\n\n{user}"
        try:
            raw = chat.complete(STYLES[args.style], user, json_only=True)
            q = json.loads(raw)["question"].strip()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [{i}] FAIL {p['case_id']}: {type(e).__name__}: {str(e)[:90]}", flush=True)
            continue
        cases.append({**p, "question": q, "generator": args.generator, "model": model_label})
        # 매 항목 저장: 83건 × 수십 초짜리 루프가 중간에 죽어도 진행분을 잃지 않는다.
        json.dump(cases, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        if (i + 1) % 5 == 0:
            print(f"  {i + 1}/{len(pages)}", flush=True)

    print(f"\n{args.generator} (style={args.style}): 총 {len(cases)}개 "
          f"(이번 실행 실패 {failed}) → {out_path}")
    if cases:
        import statistics as _st
        L = sorted(len(c["question"]) for c in cases)
        nq = sum(1 for c in cases if is_interrogative(c["question"]))
        print(f"  길이 p50={_st.median(L):.0f}자  의문형 {nq}/{len(cases)} = {nq/len(cases):.0%}")
        print("  (실제 wiki 질의 실측 n=72: 길이 p50 **21자** / 의문형 **49%** — README §8.6e)")
    if failed:
        print(f"  → 실패분은 `--resume`으로 다시 채워라(부하가 낮을 때).")

    leaked = sum(1 for c in cases if _title_leak(c["title"], c["question"]))
    print(f"제목 누출(질문에 제목 단어 포함): {leaked}/{len(cases)}")
    for c in cases[:3]:
        print(f"  {c['title']!r}\n    → {c['question']!r}")


def _title_leak(title: str, question: str) -> bool:
    """제목의 의미 단어가 질문에 그대로 남아 있으면 lexical arm이 공짜로 맞힌다."""
    q = question.lower()
    words = [w for w in title.lower().split() if len(w) >= 3]
    return any(w in q for w in words)


if __name__ == "__main__":
    main()
