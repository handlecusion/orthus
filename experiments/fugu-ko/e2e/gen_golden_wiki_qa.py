#!/usr/bin/env python3
"""wiki_qa(T2) 골든셋 **질문 역생성** 파이프라인 — 라이브 company wiki에서 문항을 만든다.

왜 역생성인가
-------------
T2 골든 스키마(`experiments/fugu-ko/golden/t2.json`)는 `{task, desc, note, items[{id,q}]}`로
**질문만** 담는다. 정답도 grounding도 인라인하지 않는다 — 근거는 채점 시점에 실제
`orthus.wiki.retrieve.retrieve`가 라이브 wiki-store/pgvector에서 가져온다. 따라서 골든 문항의
유일한 요건은 **"위키에 실제로 근거가 있는 질문"**이다. 사람이 주제를 상상해 쓰면 그 요건이
보장되지 않으므로(기존 t2 30문항은 손으로 고른 확인된 주제였다), 여기서는 방향을 뒤집는다:

    실제 wiki 청크 → (gpt-4o) 그 청크로 답할 수 있는 자연스러운 질문 → **retrieve 재검증**

마지막 재검증이 핵심이다. 생성된 질문을 진짜 `retrieve()`에 태워 **원천 청크가 상위 k 안에
돌아오는지** 확인하고, 안 돌아오면 그 문항을 버린다. 검색이 못 찾는 질문은 어느 모델도
근거 있게 답할 수 없으므로 모델 비교에 쓸 수 없다(측정하려는 것이 검색이 아니라 답변이므로).

문항 두 유형 (owner 결정, 2026-07-29)
-------------------------------------
- ``factual``        : 청크 **1개**로 답하는 좁은 사실형. 환각 탐지에 예민하다.
                       ("Whisper STT 캡션 기능 검증이 완료된 날짜가 언제인가요?")
- ``synthetic_broad``: 같은 주제 영역의 청크 **2~4개**를 아울러야 답하는 넓은 종합형.
                       기존 t2 30문항의 성격이고 실사용 분포에 가깝다.
                       ("회사의 배포 프로세스가 어떻게 진행돼?")

종합형의 주제 묶음은 **wiki_links 관계**로 결정론 구성한다(슬러그 prefix보다 의미가 정확하다):
  (a) ``page`` 모드 — 같은 consolidated 페이지로 backlink되는 claim들(= 하나의 토픽)
  (b) ``src``  모드 — 같은 source 문서(`src-*`)를 supports하는 claim들(= 하나의 회의록/문서)
히트 검증도 유형별로 다르다: factual은 원천 slug가 top-k에 있어야 하고(strict),
종합형은 **원천 청크 중 하나 이상**이면 통과한다(넓은 질문은 특정 청크를 정조준하지 않는다).

모호 문항 2차 판정
------------------
파일럿에서 retrieve는 통과했지만 사람이 보면 대상이 특정되지 않는 문항이 ~10% 남았다
("버그 신고의 우선순위가 어떻게 지정되었나요?"). 결정론 규칙으로는 못 잡는다(고유명사
유무로 자르면 "형준하와 연락하려면 어떤 경로를 써야 하나요?" 같은 정상 문항이 같이 죽는다).
그래서 (1) 생성 프롬프트에 "유일 특정" 조항을 넣고, (2) 생성된 문항을 **원천 없이** gpt-4o에
한 번 더 보여 "이 질문 하나만 읽고 대상이 유일하게 특정되는가"를 판정시킨다. 원천을 같이
주면 판정자가 맥락에서 지시어를 해소해 버려 검사 자체가 무의미해진다.

생성기를 gpt-4o로 쓰는 이유
---------------------------
평가 대상(국내 3사: solar/exaone/ax)과도, judge(Claude Sonnet)와도 겹치지 않아야 문항 자체가
특정 피험자에게 유리해지지 않는다. gpt-4o는 프로덕션 모델로는 금지(벤더 금지, AGENTS.md
model-orchestration)지만 **측정 도구로는 허용**된다 — `t2_holdout_judge.py`의 gpt-4o judge와
같은 선례다.

결정론 / 재개
-------------
샘플링은 `md5(key || salt)` 정렬이라 같은 salt면 항상 같은 순서다. 모든 시도(채택·폐기
불문)를 jsonl에 append하므로, 중단 후 재실행하면 이미 시도한 후보를 건너뛰고 이어서 채운다.
배치 내 LLM 호출만 병렬이고 채택 판정(중복 제거 포함)은 **결정론 순서대로 직렬** 수행한다.

실행:
    python experiments/fugu-ko/e2e/gen_golden_wiki_qa.py --run pilot --target 50
    python experiments/fugu-ko/e2e/gen_golden_wiki_qa.py --run full \\
        --target-factual 600 --target-broad 400 --max-gpt-calls 4000 --workers 6
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
_REPO_ROOT = HERE.parent.parent.parent  # e2e -> fugu-ko -> experiments -> repo root

# --------------------------------------------------------------------------- #
# company node env. `orthus.settings`는 pydantic-settings라 **실제 env var가
# repo-root `.env`보다 우선**한다. repo `.env`의 DSN은 `orthus`(dev 픽스처 DB,
# wiki_chunks 3건)를 가리키므로 node.env를 먼저 환경에 올려야
# `orthus_company`(라이브 위키, wiki_chunks 28,186건)로 붙는다. 실제로 다른 하네스가
# `--env-file` 없이 돌다가 3청크 픽스처를 라이브로 착각한 사례가 있다.
# --------------------------------------------------------------------------- #
_NODE_ENV = Path(
    os.environ.get("ORTHUS_NODE_ENV", str(Path.home() / ".orthus/nodes/company/node.env"))
)
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*?)\s*$")


def _env_pairs(path: Path) -> list[tuple[str, str]]:
    """파일의 KEY=VALUE를 **파일 순서 그대로** 반환(shell 확장은 하지 않음)."""
    out: list[tuple[str, str]] = []
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if val[:1] == val[-1:] and val[:1] in {'"', "'"} and len(val) >= 2:
            val = val[1:-1]
        out.append((key, val))
    return out


def _file_env(path: Path) -> dict[str, str]:
    """파일의 KEY=VALUE 맵(환경 미반영). 값 확장은 현재 환경 기준."""
    return {k: os.path.expandvars(v) for k, v in _env_pairs(path)}


def _load_node_env(path: Path) -> int:
    """node.env를 환경에 반영. 이미 export된 값은 존중(override 안 함).

    확장은 **한 줄씩 set한 뒤** 수행해야 한다. node.env는 `ORTHUS_PG_DSN=...${ORTHUS_NODE_DB}`
    처럼 앞줄에서 정의한 값을 뒤에서 참조하는데, 전체를 먼저 파싱해 놓고 확장하면
    `${ORTHUS_NODE_DB}`가 빈 문자열로 남아 `database "${ORTHUS_NODE_DB}" does not exist`로
    죽는다(실측)."""
    n = 0
    for key, val in _env_pairs(path):
        if key in os.environ:
            continue
        os.environ[key] = os.path.expandvars(val)
        n += 1
    return n


_load_node_env(_NODE_ENV)
# 캐시가 골든 생성/검증에 끼면 재현이 깨진다(같은 질문이 옛 답을 되받음).
os.environ["ORTHUS_ASK_SEMANTIC_CACHE_ENABLED"] = "false"
os.environ["ORTHUS_ASK_CACHE_SEMANTIC_MATCH_ENABLED"] = "false"

for _p in (str(_REPO_ROOT), str(HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FACTUAL = "factual"
BROAD = "synthetic_broad"

# --------------------------------------------------------------------------- #
# 후보 프리필터 (결정론, LLM 0회)
# --------------------------------------------------------------------------- #

# "없다"만 말하는 claim은 질문을 만들어 봐야 답이 '정보 없음'이라 모델 변별력이 0이다.
_ABSENCE = (
    "정보가 없", "정보가 제공되지", "제공되지 않", "내용이 없", "(없음)", "없습니다.",
    "표기되어 있지 않", "명시되어 있지 않", "확인되지 않", "비어 있", "기재되어 있지 않",
    "포함되어 있지 않", "언급되지 않",
)
# 문서 자체를 말하는 메타 claim(등록일/수정일/문서 상태)은 지식 질문이 아니다.
_META = ("등록일", "최종 수정", "수정일", "문서 등록", "schema_version", "슬러그")
# 개인 연락처를 묻는 문항은 벤치마크로 부적절하다(PII). 원천 단계에서 잘라낸다 —
# 파일럿에서 "이개발의 연락처 번호가 어떻게 되나요?"가 채택되어 추가했다.
_PII = ("연락처", "전화번호", "휴대폰", "핸드폰", "이메일 주소", "계좌번호", "주민등록")
_LATIN = re.compile(r"[A-Za-z]{3,}")
_NUM = re.compile(r"\d{2,}")
_WORD = re.compile(r"[0-9A-Za-z가-힣]+")


def _sections(content: str) -> dict[str, str]:
    """`# Claim: x / ## Claim / ## Evidence / ## Notes` 마크다운을 섹션 맵으로."""
    out: dict[str, str] = {}
    cur = "_head"
    buf: list[str] = []
    for line in content.splitlines():
        if line.startswith("## "):
            out[cur] = "\n".join(buf).strip()
            cur = line[3:].strip().lower()
            buf = []
        else:
            buf.append(line)
    out[cur] = "\n".join(buf).strip()
    return out


def _claim_text(content: str) -> tuple[str, str]:
    """(claim 본문, evidence 본문). claim 구조가 아니면 ('', '')."""
    s = _sections(content)
    return s.get("claim", ""), s.get("evidence", "")


def prefilter_claim(claim: str, evidence: str) -> str | None:
    """통과면 None, 아니면 폐기 사유 문자열."""
    if len(claim) < 18:
        return "claim_too_short"
    body = f"{claim} {evidence}"
    if any(p in claim for p in _ABSENCE):
        return "absence_claim"
    if any(p in claim for p in _META):
        return "meta_claim"
    if any(p in body for p in _PII):
        return "contact_pii"
    # 구체성: 라틴 고유명사(제품/서비스/사람)나 숫자(날짜/버전/수치) 중 하나는 있어야
    # retrieve가 걸릴 앵커가 생긴다. 둘 다 없으면 "미팅 유형은 '외부미팅'입니다" 류가 된다.
    if not (_LATIN.search(body) or _NUM.search(body)):
        return "no_anchor"
    return None


def prefilter_page(body: str) -> str | None:
    if len(body) < 300:
        return "page_too_short"
    if any(p in body for p in _PII):
        return "contact_pii"
    if not (_LATIN.search(body) or _NUM.search(body)):
        return "no_anchor"
    return None


# --------------------------------------------------------------------------- #
# 후보 로딩 (결정론 샘플링)
# --------------------------------------------------------------------------- #


@dataclass
class Member:
    slug: str
    title: str
    claim: str
    evidence: str


@dataclass
class Candidate:
    key: str  # 후보 식별자 (factual=page_id, broad=cluster 키)
    qkind: str  # FACTUAL | BROAD
    source: str  # claim | page | cluster:page | cluster:src
    slugs: list[str]  # 히트 검증 대상 slug 집합
    grounding: str  # LLM에 넘길 근거 텍스트
    ref_text: str  # 내용 동치 판정용 원문


_CLAIM_SQL = """
with base as (
  select p.page_id::text as page_id, p.slug, p.title, c.content,
         regexp_replace(p.slug, '-[0-9a-f]{8}$', '') as stem
    from wiki_pages p
    join wiki_chunks c on c.page_id = p.page_id
   where p.scope = 'company' and p.kind = 'claim'
     and length(c.content) between :minlen and :maxlen
),
st as (select stem, count(*) as n from base group by 1)
select b.page_id, b.slug, b.title, b.content
  from base b join st on st.stem = b.stem
 where st.n <= :max_stem
 order by md5(b.page_id || :salt)
"""

_PAGE_SQL = """
with base as (
  select p.page_id::text as page_id, p.slug, p.title,
         string_agg(c.content, E'\n' order by c.ordinal) as content
    from wiki_pages p
    join wiki_chunks c on c.page_id = p.page_id
   where p.scope = 'company' and p.kind = 'page'
   group by 1, 2, 3
)
select page_id, slug, title, content
  from base
 where length(content) >= :minlen
 order by md5(page_id || :salt)
"""

# 종합형 클러스터: company claim이 어느 페이지/소스에 묶이는지. `backlink`는 claim→page,
# `supports`는 claim→src-문서. 둘 다 wiki 저작이 결정론으로 남긴 관계라 재현 가능하다.
_CLUSTER_SQL = """
select l.dst_slug as key, p.slug, p.title, c.content
  from wiki_links l
  join wiki_pages p on p.page_id = l.src_page_id
  join wiki_chunks c on c.page_id = p.page_id
 where p.scope = 'company' and p.kind = 'claim' and l.rel = :rel
   and length(c.content) between :minlen and :maxlen
"""

# 클러스터 키(=source/page slug)의 **사람이 읽는 제목**. src 소스는 실제 문서 제목
# ("[6/13] 스케줄 배정 정보 추가")이라 종합형 질문의 주제어로 쓸 만하다. 반대로 page
# 제목은 슬러그를 Title Case한 것("Estimated Work Duration")뿐이라 주제어로 쓰면
# 질문이 "estimated-work-duration 문서에서…" 같은 기계 말투가 된다(실측) — 그런 제목은
# 아래에서 걸러 아예 주제어 없이 근거만 준다.
_KEY_TITLE_SQL = """
select slug, title, kind from wiki_pages
 where scope = 'company' and kind in ('source', 'page')
"""

# factual claim → 그 claim이 나온 **원본 문서 제목**. claim 본문만 주면 "오류 수정 작업이
# 완료된 날짜는?" 처럼 대상이 없는 질문이 나오고(실측 2차 판정 폐기율 45%), 같은 위키에
# 후보가 수백 개라 채점이 무의미해진다. 출처 문서 제목("[6/28] 정산하기 외주인원 모바일대응")을
# 함께 주면 생성기가 한정어를 넣을 수 있다 — 종합형 스트림이 제목을 받아 폐기율 10.7%인 것과
# 같은 처방이다.
_CLAIM_SOURCE_SQL = """
select p.page_id::text as page_id, src.title as src_title
  from wiki_pages p
  join wiki_links l on l.src_page_id = p.page_id and l.rel = 'supports'
  join wiki_pages src on src.slug = l.dst_slug and src.scope = 'company' and src.kind = 'source'
 where p.scope = 'company' and p.kind = 'claim'
"""


def load_factual_candidates(
    salt: str,
    *,
    max_stem: int,
    claim_minlen: int,
    claim_maxlen: int,
    page_ratio: float,
    with_source_title: bool = True,
) -> list[Candidate]:
    from sqlalchemy import text

    from orthus.db import session

    claims: list[Candidate] = []
    pages: list[Candidate] = []
    with session() as s:
        src_title: dict[str, str] = {}
        if with_source_title:
            for r in s.execute(text(_CLAIM_SOURCE_SQL)).all():
                t = (r.src_title or "").strip()
                # Slack 원문 제목 등 불투명 ID가 박힌 제목은 한정어로 못 쓴다.
                if t and not _OPAQUE_ID.search(t) and r.page_id not in src_title:
                    src_title[r.page_id] = t[:120]
        for r in s.execute(
            text(_CLAIM_SQL),
            {"salt": salt, "max_stem": max_stem, "minlen": claim_minlen, "maxlen": claim_maxlen},
        ).all():
            c, e = _claim_text(r.content)
            if prefilter_claim(c, e):
                continue
            ev = f"\n근거 원문: {e}" if e else ""
            doc = src_title.get(r.page_id)
            head = f"출처 문서: {doc}\n" if doc else ""
            claims.append(Candidate(r.page_id, FACTUAL, "claim", [r.slug], f"{head}{c}{ev}", c))
    if with_source_title:
        # 출처 제목이 있는 claim을 앞세운다 — 한정어를 넣을 재료가 있어야 2차 판정을
        # 통과한다(실측 채택률 titled 33% vs untitled 28%). 각 그룹 안의 순서는 위의
        # md5(page_id||salt) 정렬 그대로라 결정론은 유지된다.
        claims.sort(key=lambda c: 0 if c.grounding.startswith("출처 문서:") else 1)
        for r in s.execute(text(_PAGE_SQL), {"salt": salt, "minlen": 300}).all():
            if prefilter_page(r.content):
                continue
            pages.append(Candidate(r.page_id, FACTUAL, "page", [r.slug], r.content, r.content))

    # 결정론 인터리브: page-ratio 만큼 page를 섞는다.
    order: list[Candidate] = []
    ci = pi = idx = 0
    step = int(round(1 / page_ratio)) if page_ratio > 0 else 0
    while ci < len(claims) or pi < len(pages):
        idx += 1
        if step and idx % step == 0 and pi < len(pages):
            order.append(pages[pi])
            pi += 1
        elif ci < len(claims):
            order.append(claims[ci])
            ci += 1
        elif pi < len(pages):
            order.append(pages[pi])
            pi += 1
    return order


def _usable_topic(entry: tuple[str, str] | None) -> str | None:
    """클러스터 제목을 주제어로 쓸 수 있으면 반환, 아니면 None.

    거르는 것: (a) page 제목처럼 슬러그를 Title Case한 것뿐인 경우 (b) Slack 원문 제목처럼
    불투명 ID가 박힌 경우 — 둘 다 질문에 그대로 옮겨지면 사람 말이 아니게 된다."""
    if not entry:
        return None
    title, kind = entry
    title = (title or "").strip()
    if not title or kind != "source":
        return None
    if _OPAQUE_ID.search(title):
        return None
    return title[:120]


def load_broad_candidates(
    salt: str, *, min_members: int, max_members: int, claim_minlen: int, claim_maxlen: int
) -> list[Candidate]:
    """주제 클러스터. **src 모드(하나의 문서)를 먼저**, page 모드를 뒤에 붙인다.

    처음에는 page 모드(같은 consolidated 페이지로 backlink되는 claim들)를 "토픽"으로 보고
    우선했는데, 실측해 보니 그 페이지들은 서로 다른 문서에서 온 **같은 종류의 메타데이터**를
    모아 둔 자루였다(`bug-fix-completion` = 여러 문서의 완료일들). 거기서 나온 질문은
    "…의 등록일, 완료일, 다음 배포일은 각각 언제인가요?" 같은 날짜 나열이 됐다. 반면 src
    모드는 하나의 회의록/공지/Notion row에서 나온 claim 묶음이라 실제 주제가 하나다."""
    from sqlalchemy import text

    from orthus.db import session

    out: list[Candidate] = []
    with session() as s:
        titles = {
            r.slug: (r.title or "", r.kind) for r in s.execute(text(_KEY_TITLE_SQL)).all()
        }
        for rel, mode in (("supports", "src"), ("backlink", "page")):
            groups: dict[str, list[Member]] = defaultdict(list)
            for r in s.execute(
                text(_CLUSTER_SQL),
                {"rel": rel, "minlen": claim_minlen, "maxlen": claim_maxlen},
            ).all():
                c, e = _claim_text(r.content)
                if prefilter_claim(c, e):
                    continue
                groups[r.key].append(Member(r.slug, r.title, c, e))
            cands: list[Candidate] = []
            for key, members in groups.items():
                if len(members) < min_members:
                    continue
                # 결정론 절단: slug 정렬 후 앞에서 max_members개.
                ms = sorted(members, key=lambda m: m.slug)[:max_members]
                # 근거 줄에 slug를 붙이지 않는다 — 붙였더니 생성 모델이 그 슬러그를 질문에
                # 그대로 옮겨 적어 "estimated-work-duration 문서에서…"가 됐다(실측).
                body = "\n".join(
                    f"- {m.claim}" + (f" [근거: {m.evidence}]" if m.evidence else "") for m in ms
                )
                topic = _usable_topic(titles.get(key))
                grounding = (f"주제: {topic}\n" if topic else "") + body
                cands.append(
                    Candidate(
                        f"{mode}:{key}",
                        BROAD,
                        f"cluster:{mode}",
                        [m.slug for m in ms],
                        grounding,
                        " ".join(m.claim for m in ms),
                    )
                )
            cands.sort(key=lambda c: hashlib.md5((c.key + salt).encode()).hexdigest())
            out.extend(cands)
    return out


# --------------------------------------------------------------------------- #
# 질문 생성 (gpt-4o)
# --------------------------------------------------------------------------- #

_UNIQUE_RULE = (
    "질문만 따로 떼어 읽어도 **어떤 대상·사건을 묻는지 유일하게 특정**되어야 한다. "
    "지시어('이 문서', '해당 일정')나 일반명사만으로 지칭하지 마라 — 회사 위키에 후보가 "
    "여럿 있을 법한 질문('버그 수정이 언제 완료됐나요?')은 실패다.\n"
)

_GEN_SYS_FACTUAL = (
    "너는 사내 위키 기반 QA 벤치마크의 **출제자**다. 주어진 위키 근거 한 조각을 읽고, "
    "그 근거만으로 답할 수 있는 **자연스러운 한국어 질문 1개**를 만든다.\n"
    "규칙:\n"
    "1. 근거 문장을 그대로 베끼거나 어순만 바꾸지 마라. 실제 회사 직원이 사내 비서에게 "
    "물을 법한 말투로 새로 써라.\n"
    "2. 답이 반드시 그 근거 안에 있어야 한다. 근거에 없는 사실을 전제하는 질문은 금지.\n"
    "3. 검색이 걸릴 수 있게 근거에 등장하는 **고유명사(제품·프로젝트·기능·사람·날짜·수치)를 "
    "최소 1개** 질문에 넣어라. 단 Slack ID(U0ATK...)나 해시·UUID 같은 불투명 식별자는 "
    "사람이 물을 말이 아니니 쓰지 마라.\n"
    "4. 예/아니오 또는 한 단어로 끝나는 질문은 금지. '어떻게/무엇이/왜/어떤 내용' 식의 "
    "서술형 답을 요구하는 질문으로 써라.\n"
    "5. " + _UNIQUE_RULE +
    "5-1. 근거에 `출처 문서:` 줄이 있으면 그 문서·기능·프로젝트 이름을 **한정어로 질문에 "
    "넣어라**. 사내 위키에는 비슷한 사실이 수백 건 있어서 한정어가 없으면 "
    "'오류 수정 작업이 완료된 날짜는?' 같은 특정 불가 질문이 된다. 단 문서 제목을 통째로 "
    "베끼지 말고 사람이 부르는 이름으로 자연스럽게 녹여라.\n"
    "6. 질문이 답을 포함하면 안 된다(날짜를 말해 놓고 그 날짜를 되묻지 마라).\n"
    "7. 한 문장, 80자 이내, 물음표로 끝낸다.\n"
    '출력은 JSON 하나: {"q": "<질문>"}'
)

_GEN_SYS_BROAD = (
    "너는 사내 위키 기반 QA 벤치마크의 **출제자**다. 같은 주제로 묶인 위키 근거 "
    "**여러 조각**을 읽고, 그 조각들을 **아울러야만** 제대로 답할 수 있는 "
    "**넓은 한국어 질문 1개**를 만든다.\n"
    "규칙:\n"
    "1. 근거 한 줄만 보면 답할 수 있는 좁은 사실 질문은 실패다. 여러 조각을 종합해야 "
    "하는 질문이어야 한다 — 진행 상황 정리, 절차 설명, 주요 항목 나열, 원인·영향 요약 같은 "
    "형태가 좋다.\n"
    "2. 그렇다고 위키 밖 지식을 요구하면 안 된다. 답은 주어진 조각들 안에서 구성 가능해야 "
    "한다.\n"
    "3. 주제를 이름으로 못 박아라 — 근거에 등장하는 **프로젝트·기능·제품·문서 이름**을 "
    "질문에 넣어라. 불투명 식별자(Slack ID·해시·UUID)나 위키 슬러그 같은 "
    "영문-하이픈 식별자(`estimated-work-duration`)는 쓰지 마라 — 사람이 부르는 이름으로 "
    "바꿔 써라.\n"
    "4. " + _UNIQUE_RULE +
    "5. 예/아니오 질문 금지. 사내 비서에게 묻듯 자연스럽게, 한 문장 80자 이내, "
    "물음표로 끝낸다.\n"
    '출력은 JSON 하나: {"q": "<질문>"}'
)

# 문항 단독으로 무엇을 묻는지 알 수 없게 만드는 지시어. 파일럿 실측에서 "해당 일정의
# 유형은 무엇인가요?" / "이 버그는 어떤 마일스톤에 할당되었나요?"가 통과해 버려서 확장했다 —
# 원천 청크를 옆에 두고 읽으면 말이 되지만, 골든 문항은 청크 없이 던져진다.
_DEICTIC = (
    "이 문서", "위 내용", "해당 클레임", "이 클레임", "위 근거", "이 근거", "아래 내용",
    "본 문서", "해당 ", "이 버그", "이 일정", "이 업데이트", "이 항목", "이 작업",
    "이 이슈", "이 기능은", "그 문서",
)
# 의문사가 하나도 없으면 사실상 예/아니오 질문이다("GCP는 클라우드 분야에 속하나요?").
# 반대로 "무엇인가요?/누구인가요?"는 어미만 보면 polar처럼 보이지만 wh 질문이다 —
# 어미 패턴으로 거르면(초기 구현) 정상 문항의 80%가 잘못 폐기됐다(smoke 4/5).
_WH = (
    "무엇", "뭐", "뭔", "무슨", "어떤", "어떻게", "어째", "왜", "언제", "누가", "누구",
    "어디", "얼마", "몇", "어느", "어떠", "정리해", "설명해", "알려",
)
# Slack user/channel id(U0ATKMHKZBL), UUID 조각 같은 불투명 식별자는 사람이 물을 말이
# 아니다. 고유명사 앵커로는 쓸모없고 문항 가독성만 깎는다.
# `\b`를 쓰면 안 된다 — 파이썬 `\w`는 한글을 포함하므로 "U0ATKMHKZBL이"에서 L과 이 사이에
# 경계가 생기지 않아 실측에서 2건이 그대로 통과했다. 명시적 문자군 lookaround를 쓴다.
_OPAQUE_ID = re.compile(r"(?<![0-9A-Za-z])(?:[UCD][0-9A-Z]{7,}|[0-9a-f]{8,})(?![0-9A-Za-z])")
_DATE_TOK = re.compile(r"(20\d{2}년|\d{1,2}월\s?\d{1,2}일|20\d{2}-\d{2}-\d{2})")
_ASKS_DATE = ("언제", "날짜", "일자", "며칠")


def validate_question(q: str, *, qkind: str = FACTUAL) -> str | None:
    q = q.strip()
    if not q:
        return "empty"
    if not q.endswith("?"):
        return "no_question_mark"
    if len(q) < 10:
        return "too_short"
    if len(q) > 95:
        return "too_long"
    if "\n" in q:
        return "multiline"
    if any(d in q for d in _DEICTIC):
        return "deictic"
    if not any(w in q for w in _WH):
        return "yes_no"
    if _OPAQUE_ID.search(q):
        return "opaque_id"
    if any(p in q for p in _PII):
        return "contact_pii"
    # 정답 유출: 질문이 날짜를 이미 말해 놓고 그 날짜를 되묻는 형태
    # ("2025년 6월 28일에 완료된 X 업데이트는 어떤 날짜에 완료되었나요?" — 파일럿 실측).
    # 채점이 무의미해지므로 폐기한다. 날짜를 **맥락으로** 쓰고 다른 걸 묻는 질문
    # ("4월 20일에 어떤 일이 예정돼 있나요?")은 그대로 통과한다.
    if _DATE_TOK.search(q) and any(a in q for a in _ASKS_DATE):
        return "answer_leak"
    return None


def leaks_slug(q: str, cand: Candidate) -> bool:
    """질문이 원천 슬러그(또는 클러스터 키)를 그대로 옮겨 적었는가.

    결정론이고 정확하다 — 우리가 무슨 슬러그를 줬는지 알기 때문이다. 일반적인
    kebab-case 금지 규칙과 달리 `atlas-public-web` 같은 **실제 저장소 이름**은
    살려 둔다(그건 슬러그가 아니라 사람이 쓰는 이름이다)."""
    low = q.lower()
    keys = [cand.key.split(":", 1)[-1]] + list(cand.slugs)
    for k in keys:
        stem = re.sub(r"^src-", "", k)
        stem = re.sub(r"-[0-9a-f]{8}$", "", stem)
        if len(stem) >= 12 and stem in low:
            return True
    return False


def _norm(q: str) -> str:
    return "".join(_WORD.findall(q.lower()))


def is_duplicate(q: str, accepted: list[str], *, ratio: float) -> bool:
    n = _norm(q)
    for a in accepted:
        an = _norm(a)
        if n == an:
            return True
        if SequenceMatcher(None, n, an).ratio() >= ratio:
            return True
    return False


# --------------------------------------------------------------------------- #
# 2차 판정: "질문 하나만 읽고 대상이 유일하게 특정되는가" (gpt-4o, 원천 미제공)
# --------------------------------------------------------------------------- #

_JUDGE_SYS = (
    "너는 사내 지식 QA 벤치마크의 **문항 심사관**이다. 회사 사내 위키(프로젝트 atlas/"
    "nova/orbit, 회의록·배포공지·버그리포트·주간회고·AI 툴 조사 등 수천 건)에 던질 질문 "
    "하나를 받는다. **근거 문서는 주지 않는다** — 질문 텍스트만 보고 판단해야 한다.\n"
    "판정 기준: 이 질문을 받은 사내 비서가 **무엇을 묻는지 유일하게 특정**할 수 있는가?\n"
    "- 위키에 후보가 여럿 있을 법한 질문은 ambiguous다. 예: '버그 수정이 완료된 날짜가 "
    "언제인가요?'(버그가 수백 건), '업데이트의 유형은 무엇인가요?'(어느 업데이트?), "
    "'현재 어떤 패턴이 유지되고 있나요?'(무엇의 패턴?).\n"
    "- 프로젝트·기능·제품·사람·문서 이름이나 날짜 같은 **한정어**가 있어 대상이 좁혀지면 "
    "specific이다. 예: 'Whisper STT 캡션 기능 검증이 완료된 날짜가 언제인가요?', "
    "'nova 배포 프로세스는 어떻게 진행되나요?'.\n"
    "- **넓은 주제 질문 자체는 문제가 아니다.** 주제가 이름으로 못 박혀 있으면 "
    "('nova 개발 로드맵의 주요 마일스톤은?') specific으로 본다. 주제조차 불명확한 것만 "
    "ambiguous다.\n"
    "- 질문이 답을 이미 포함하거나(자문자답) 사내 위키와 무관한 일반상식이면 reject한다.\n"
    '출력은 JSON 하나: {"verdict": "specific"|"ambiguous"|"reject", "reason": "<한 문장>"}'
)


def judge_question(chat, q: str, qkind: str) -> tuple[str, str]:
    hint = "이 문항은 여러 근거를 종합하는 넓은 질문으로 출제됐다." if qkind == BROAD else ""
    prompt = f"[질문]\n{q}\n\n{hint}\n판정하라."
    try:
        raw = chat.complete(_JUDGE_SYS, prompt, json_only=True)
        data = json.loads(raw)
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict not in {"specific", "ambiguous", "reject"}:
            return "error", f"bad verdict: {verdict[:40]}"
        return verdict, str(data.get("reason", ""))[:200]
    except Exception as exc:  # noqa: BLE001
        return "error", f"{type(exc).__name__}: {exc}"[:200]


# --------------------------------------------------------------------------- #
# retrieve 히트 검증
# --------------------------------------------------------------------------- #

_CONTENT_STOP = {"claim", "evidence", "notes", "none", "문서", "내용"}


def _tokens(text_: str) -> set[str]:
    return {
        t.lower() for t in _WORD.findall(text_) if len(t) >= 2 and t.lower() not in _CONTENT_STOP
    }


def verify_hit(cand: Candidate, question: str, *, user_id: UUID, k: int, equiv: float) -> dict:
    """실제 `retrieve()`로 검증.

    - factual: 원천 slug가 top-k에 있어야 통과(strict).
    - broad  : 원천 slug **중 하나 이상**이 top-k에 있으면 통과 — 넓은 질문은 특정 청크를
      정조준하지 않으므로 strict 기준을 쓰면 유형 자체가 전멸한다(owner 결정).

    equiv(내용 동치)는 **중복 claim** 때문에 둔다: 같은 사실을 담은 claim이 여러 개면
    원천이 아닌 형제 claim이 상위에 올 수 있는데, 그건 "위키에 근거가 있다"는 요건을
    똑같이 만족한다. 두 지표를 따로 기록해 폐기율의 성격을 구분한다."""
    from orthus.wiki.retrieve import retrieve

    hits = retrieve(user_id, question, k=k, scope="company")
    slugs = [h.page_slug for h in hits]
    matched = [s for s in cand.slugs if s in slugs]
    src_tok = _tokens(cand.ref_text)
    best = 0.0
    for h in hits:
        ht = _tokens(h.excerpt)
        if not src_tok or not ht:
            continue
        best = max(best, len(src_tok & ht) / len(src_tok | ht))
    return {
        "strict": bool(matched),
        "n_matched": len(matched),
        "n_sources": len(cand.slugs),
        "rank": min(slugs.index(s) for s in matched) + 1 if matched else None,
        "equiv": best >= equiv,
        "best_jaccard": round(best, 3),
        "retrieved": slugs,
    }


# --------------------------------------------------------------------------- #
# 실행
# --------------------------------------------------------------------------- #


@dataclass
class Stats:
    gen_calls: int = 0
    judge_calls: int = 0
    gen_error: int = 0
    invalid: dict[str, int] = field(default_factory=dict)
    duplicate: int = 0
    embed_calls: int = 0
    retrieve_verified: int = 0
    retrieve_miss: int = 0
    judged: int = 0
    judge_drop: dict[str, int] = field(default_factory=dict)
    accepted: int = 0

    def bump(self, d: dict[str, int], key: str) -> None:
        d[key] = d.get(key, 0) + 1

    def as_dict(self) -> dict:
        rate = (
            round(1 - self.retrieve_miss / self.retrieve_verified, 3)
            if self.retrieve_verified
            else None
        )
        # `error_passed`는 판정 실패라 폐기가 아니다 — 폐기율 분모/분자에서 뺀다.
        dropped = sum(v for kk, v in self.judge_drop.items() if kk != "error_passed")
        return {
            "gen_calls": self.gen_calls,
            "judge_calls": self.judge_calls,
            "gen_error": self.gen_error,
            "invalid": self.invalid,
            "duplicate": self.duplicate,
            "embed_calls": self.embed_calls,
            "retrieve_verified": self.retrieve_verified,
            "retrieve_miss": self.retrieve_miss,
            "retrieve_pass_rate": rate,
            "judged": self.judged,
            "judge_drop": self.judge_drop,
            "judge_drop_rate": round(dropped / self.judged, 3) if self.judged else None,
            "accepted": self.accepted,
        }


class Budget:
    """gpt-4o 콜 총량 가드 (생성 + 2차 판정 합산)."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.used = 0
        self._lock = threading.Lock()

    def take(self, n: int = 1) -> bool:
        with self._lock:
            if self.used + n > self.cap:
                return False
            self.used += n
            return True


def _run_stream(
    *,
    name: str,
    qkind: str,
    candidates: list[Candidate],
    target: int,
    accepted_q: list[str],
    seen: set[str],
    emit,
    gen_chat,
    judge_chat,
    budget: Budget,
    args,
    user_id: UUID,
) -> Stats:
    """한 유형(factual/broad)을 배치 파이프라인으로 채운다.

    배치 안에서 (1) 생성 (2) retrieve 검증 (3) 2차 판정만 병렬이고, 채택 판정은
    후보 순서대로 직렬 — 중복 제거 결과가 스레드 스케줄에 흔들리지 않게 한다.
    2차 판정을 retrieve **뒤에** 두는 것은 비용 설계다(임베딩은 싸고 gpt-4o는 비싸다)."""
    st = Stats()
    have = len(accepted_q)
    pool = ThreadPoolExecutor(max_workers=args.workers)
    queue = [c for c in candidates if c.key not in seen]
    print(f"[{name}] queue={len(queue)} (seen 제외)")
    pos = 0
    t0 = time.time()
    stop = False

    def gen_one(cand: Candidate) -> tuple[Candidate, str | None, str | None]:
        sys_prompt = _GEN_SYS_BROAD if qkind == BROAD else _GEN_SYS_FACTUAL
        prompt = (
            f"[위키 근거]\n{cand.grounding[:2400]}\n\n"
            "위 근거로 답할 수 있는 질문 하나를 JSON으로 내라."
        )
        try:
            raw = gen_chat.complete(sys_prompt, prompt, json_only=True)
            return cand, str(json.loads(raw).get("q", "")).strip(), None
        except Exception as exc:  # noqa: BLE001
            return cand, None, f"{type(exc).__name__}: {exc}"[:200]

    while have < target and pos < len(queue) and not stop:
        need = target - have
        batch = queue[pos : pos + min(args.batch, need * 2 + args.workers)]
        pos += len(batch)
        if not batch:
            break
        if not budget.take(len(batch)):
            print(f"[budget] {name}: gpt-4o 생성 콜 상한 도달 (used={budget.used}/{budget.cap})")
            break
        st.gen_calls += len(batch)

        gen_rows = list(pool.map(gen_one, batch))

        # 결정론 직렬 1차 필터 (형식/중복) — 살아남은 것만 retrieve로.
        survivors: list[tuple[Candidate, str]] = []
        for cand, q, err in gen_rows:
            if err:
                st.gen_error += 1
                emit(cand, qkind, "gen_error", error=err)
                continue
            bad = validate_question(q or "", qkind=qkind)
            if not bad and leaks_slug(q or "", cand):
                bad = "slug_leak"
            if bad:
                st.bump(st.invalid, bad)
                emit(cand, qkind, "invalid", q=q, reason=bad)
                continue
            if is_duplicate(q, accepted_q, ratio=args.dup_ratio) or is_duplicate(
                q, [s for _, s in survivors], ratio=args.dup_ratio
            ):
                st.duplicate += 1
                emit(cand, qkind, "duplicate", q=q)
                continue
            survivors.append((cand, q))
        if not survivors:
            continue

        verdicts = list(
            pool.map(
                lambda cq: verify_hit(cq[0], cq[1], user_id=user_id, k=args.k, equiv=args.equiv),
                survivors,
            )
        )
        st.embed_calls += len(survivors)
        st.retrieve_verified += len(survivors)

        passed: list[tuple[Candidate, str, dict]] = []
        for (cand, q), v in zip(survivors, verdicts, strict=True):
            ok = v["strict"] or (args.accept_equiv and v["equiv"])
            if not ok:
                st.retrieve_miss += 1
                emit(cand, qkind, "retrieve_miss", q=q, **v)
                continue
            passed.append((cand, q, v))

        if passed and args.judge:
            if not budget.take(len(passed)):
                print(f"[budget] {name}: 판정 콜 상한 도달 (used={budget.used}/{budget.cap})")
                break
            st.judge_calls += len(passed)
            jrows = list(pool.map(lambda t: judge_question(judge_chat, t[1], qkind), passed))
        else:
            jrows = [("specific", "judge disabled")] * len(passed)

        for (cand, q, v), (verdict, reason) in zip(passed, jrows, strict=True):
            st.judged += 1
            # 판정 자체가 실패(HTTP/JSON)한 건은 폐기하지 않는다 — 모델 장애로 골든이
            # 조용히 줄어드는 게 더 나쁘다. 통과시키되 사유를 남긴다.
            if verdict in {"ambiguous", "reject"}:
                st.bump(st.judge_drop, verdict)
                emit(cand, qkind, "judge_drop", q=q, verdict=verdict, judge_reason=reason, **v)
                continue
            if verdict == "error":
                st.bump(st.judge_drop, "error_passed")
            if have >= target:
                break
            st.accepted += 1
            accepted_q.append(q)
            have += 1
            emit(cand, qkind, "accepted", q=q, judge=verdict, judge_reason=reason, **v)
            d = st.as_dict()
            if have % 25 == 0 or have == target:
                print(
                    f"[{name}] {have}/{target} gpt={budget.used}/{budget.cap} "
                    f"retr_pass={d['retrieve_pass_rate']} judge_drop={d['judge_drop_rate']} "
                    f"{time.time() - t0:.0f}s"
                )

        # 중단 조건 (owner): 유형별 통과율/폐기율이 설계 재검토 신호에 닿으면 멈춘다.
        d = st.as_dict()
        if (
            st.retrieve_verified >= args.guard_min
            and (d["retrieve_pass_rate"] or 0) < args.min_retrieve_rate
        ):
            print(f"[STOP] {name}: retrieve 통과율 {d['retrieve_pass_rate']} < {args.min_retrieve_rate}")
            stop = True
        if st.judged >= args.guard_min and (d["judge_drop_rate"] or 0) > args.max_judge_drop:
            print(f"[STOP] {name}: 2차 판정 폐기율 {d['judge_drop_rate']} > {args.max_judge_drop}")
            stop = True
        # gen콜당 최종 채택률 — 예산 소진 속도를 직접 보는 지표(owner 가드).
        # 폐기율/통과율이 각각 임계 안이어도 곱이 나쁘면 예산이 먼저 마른다.
        #
        # 표본 하한이 `guard_min`(40)이 아니라 별도 `accept_guard_min`(150)인 이유:
        # 채택률은 세 확률(형식·retrieve·판정)의 곱이라 배치 하나(48콜)에서는 분산이 크다.
        # 실제로 첫 배치가 15/48=0.312를 내며 가드가 발동했는데, 그 표본의 95% 신뢰구간은
        # 대략 0.19~0.46이라 임계 0.35와 구분이 되지 않는다. **임계값은 owner가 정한
        # 0.35 그대로 두고**, 그 임계를 판정할 표본만 배치 3개 규모로 키운다 — 가드의
        # 목적은 "설정이 구조적으로 망가졌다"를 잡는 것이지 배치 노이즈로 승인된 작업을
        # 중단시키는 게 아니다.
        if st.gen_calls >= args.accept_guard_min:
            acc_rate = round(st.accepted / st.gen_calls, 3)
            if acc_rate < args.min_accept_rate:
                print(f"[STOP] {name}: gen콜당 채택률 {acc_rate} < {args.min_accept_rate}")
                stop = True

    pool.shutdown(wait=True)
    return st


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--target", type=int, default=0, help="(구) 단일 목표 — factual만 채운다")
    ap.add_argument("--target-factual", type=int, default=0)
    ap.add_argument("--target-broad", type=int, default=0)
    ap.add_argument("--k", type=int, default=5, help="retrieve 검증 상위 k")
    ap.add_argument("--salt", default="wiki-qa-v1", help="결정론 샘플링 salt")
    ap.add_argument("--page-ratio", type=float, default=0.2, help="본문 300자↑ page 원천 비율")
    ap.add_argument("--max-stem", type=int, default=2, help="같은 slug stem claim 최대 개수")
    ap.add_argument("--claim-minlen", type=int, default=60)
    ap.add_argument("--claim-maxlen", type=int, default=400)
    ap.add_argument("--cluster-min", type=int, default=3, help="종합형 클러스터 최소 claim 수")
    ap.add_argument("--cluster-max", type=int, default=4, help="종합형 클러스터 최대 claim 수")
    ap.add_argument("--dup-ratio", type=float, default=0.85)
    ap.add_argument("--equiv", type=float, default=0.5, help="내용 동치 판정 자카드 임계")
    ap.add_argument("--accept-equiv", action="store_true")
    ap.add_argument(
        "--source-title",
        action="store_true",
        default=True,
        help="factual 근거에 출처 문서 제목을 함께 준다(모호 문항 억제)",
    )
    ap.add_argument("--no-source-title", dest="source_title", action="store_false")
    ap.add_argument("--judge", action="store_true", default=True, help="2차 모호성 판정 사용")
    ap.add_argument("--no-judge", dest="judge", action="store_false")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--batch", type=int, default=48)
    ap.add_argument("--max-gpt-calls", type=int, default=4000, help="생성+판정 총 콜 상한")
    ap.add_argument("--guard-min", type=int, default=40, help="중단 조건 판정 최소 표본")
    ap.add_argument("--min-retrieve-rate", type=float, default=0.40)
    ap.add_argument("--max-judge-drop", type=float, default=0.40)
    ap.add_argument("--min-accept-rate", type=float, default=0.35, help="gen콜당 최종 채택률 하한")
    ap.add_argument(
        "--accept-guard-min", type=int, default=150, help="채택률 가드 판정 최소 gen콜 표본"
    )
    ap.add_argument("--out-dir", default=str(HERE / "golden_wiki_qa"))
    ap.add_argument("--run", default="full")
    ap.add_argument(
        "--golden-out",
        default=str(_REPO_ROOT / "experiments/fugu-ko/golden/t2_wiki_qa_1k.json"),
    )
    ap.add_argument("--model", default="gpt-4o")
    ap.add_argument("--dry-run", action="store_true", help="LLM 콜 없이 후보 통계만")
    args = ap.parse_args()

    if args.target and not (args.target_factual or args.target_broad):
        args.target_factual = args.target
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    attempts_path = out_dir / f"{args.run}_attempts.jsonl"

    # --- 재개 ---------------------------------------------------------------
    seen: set[str] = set()
    accepted_rows: list[dict] = []
    if attempts_path.exists():
        for line in attempts_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            seen.add(rec.get("key") or rec.get("page_id", ""))
            if rec["status"] == "accepted":
                accepted_rows.append(rec)
    acc_f = [r["q"] for r in accepted_rows if r.get("qkind", FACTUAL) == FACTUAL]
    acc_b = [r["q"] for r in accepted_rows if r.get("qkind") == BROAD]
    print(f"[resume] attempted={len(seen)} accepted factual={len(acc_f)} broad={len(acc_b)}")

    factual = load_factual_candidates(
        args.salt,
        max_stem=args.max_stem,
        claim_minlen=args.claim_minlen,
        claim_maxlen=args.claim_maxlen,
        page_ratio=args.page_ratio,
        with_source_title=args.source_title,
    )
    broad = load_broad_candidates(
        args.salt,
        min_members=args.cluster_min,
        max_members=args.cluster_max,
        claim_minlen=args.claim_minlen,
        claim_maxlen=args.claim_maxlen,
    )
    by_mode: dict[str, int] = {}
    for c in broad:
        by_mode[c.source] = by_mode.get(c.source, 0) + 1
    print(f"[pool] factual={len(factual)} broad={len(broad)} {by_mode}")
    if args.dry_run:
        return 0

    from orthus.models.adapters.openai_compat import OpenAIChat
    from orthus.settings import get_settings

    s = get_settings()
    # 생성기 키는 **DB/임베딩 슬롯과 분리해서** 고른다. company node.env의
    # `ORTHUS_LLM_API_KEY`는 실측상 quota 소진(429 insufficient_quota) 상태라, repo-root
    # `.env`의 같은 이름 키를 우선한다. node.env는 계속 DSN(orthus_company) + Solar 임베딩
    # 키를 담당한다 — 두 경로를 섞으면 "왜 붙는데 생성만 죽지"가 재현 불가능해진다.
    repo_env = _file_env(_REPO_ROOT / ".env")
    gen_key = (
        os.environ.get("FUGU_GEN_API_KEY", "")
        or repo_env.get("ORTHUS_LLM_API_KEY", "")
        or s.llm_api_key
    ).strip()
    gen_base = (
        os.environ.get("FUGU_GEN_BASE_URL", "")
        or repo_env.get("ORTHUS_LLM_BASE_URL", "")
        or s.llm_base_url
    ).strip()
    if not gen_key:
        print("생성기 API 키 없음 (FUGU_GEN_API_KEY / .env ORTHUS_LLM_API_KEY)", file=sys.stderr)
        return 2
    gen_chat = OpenAIChat(gen_base, gen_key, args.model, temperature=0.3, retries=4)
    judge_chat = OpenAIChat(gen_base, gen_key, args.model, temperature=0.0, retries=4)
    user_id = UUID(os.environ.get("FUGU_USER_ID", "11111111-1111-1111-1111-111111111111"))

    fh = attempts_path.open("a", encoding="utf-8")
    lock = threading.Lock()

    def emit(cand: Candidate, qkind: str, status: str, **extra) -> None:
        rec = {
            "key": cand.key,
            "slugs": cand.slugs,
            "source": cand.source,
            "qkind": qkind,
            "status": status,
            "ts": round(time.time(), 3),
            **extra,
        }
        with lock:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()

    # 워밍업 1회(단일 스레드). `retrieve`가 첫 호출에서 커넥터 프로바이더 레지스트리를
    # lazy 등록하는데, 워커 스레드 여럿이 동시에 처음 부르면
    # `ValueError: connector provider already registered: notion`으로 죽는다(실측).
    # 프로덕션 경로는 단일 프로세스 부팅에서 한 번 등록되므로 이건 하네스 쪽 책임이다.
    from orthus.wiki.retrieve import retrieve as _warm_retrieve

    _warm_retrieve(user_id, "워밍업", k=1, scope="company")

    budget = Budget(args.max_gpt_calls)
    t0 = time.time()
    streams: list[tuple[str, list[Candidate], int, list[str]]] = []
    if args.target_factual:
        streams.append((FACTUAL, factual, args.target_factual, acc_f))
    if args.target_broad:
        streams.append((BROAD, broad, args.target_broad, acc_b))

    stats: dict[str, dict] = {}
    for qkind, cands, target, acc in streams:
        print(f"=== stream {qkind}: target={target} have={len(acc)} ===")
        st = _run_stream(
            name=qkind,
            qkind=qkind,
            candidates=cands,
            target=target,
            accepted_q=acc,
            seen=seen,
            emit=emit,
            gen_chat=gen_chat,
            judge_chat=judge_chat,
            budget=budget,
            args=args,
            user_id=user_id,
        )
        stats[qkind] = st.as_dict()
    fh.close()

    # --- 골든 파일 export --------------------------------------------------
    rows = []
    for x in attempts_path.read_text(encoding="utf-8").splitlines():
        if not x.strip():
            continue
        r = json.loads(x)
        if r["status"] == "accepted":
            rows.append(r)
    keep: list[dict] = []
    n_f = n_b = 0
    for r in rows:
        qk = r.get("qkind", FACTUAL)
        if qk == FACTUAL and (not args.target_factual or n_f < args.target_factual):
            keep.append(r)
            n_f += 1
        elif qk == BROAD and (not args.target_broad or n_b < args.target_broad):
            keep.append(r)
            n_b += 1
    items = [
        {"id": f"t2w-{i + 1:04d}", "q": r["q"], "kind": r.get("qkind", FACTUAL)}
        for i, r in enumerate(keep)
    ]
    golden = {
        "task": "T2",
        "desc": (
            "/ask 지식응답(wiki grounding QA). ask(learn=False, record_gaps=False)로 순수 read. "
            "채점=근거를 본 judge의 익명 쌍대비교. scope=company."
        ),
        "note": (
            f"라이브 orthus_company wiki에서 역생성(gen={args.model}, salt={args.salt}). "
            "items[].kind = factual(단일 청크 사실형) | synthetic_broad(주제 클러스터 종합형). "
            f"각 문항은 (1) 원천 청크가 실제 retrieve(k={args.k}, scope=company) 상위에 "
            "돌아오는지(종합형은 원천 중 1개 이상) (2) 질문 단독으로 대상이 유일 특정되는지 "
            "gpt-4o 2차 판정 — 두 검증을 통과한 것만 채택. 정답 고정 없음 — 근거는 채점 시점 "
            "retrieve가 라이브로 가져온다."
        ),
        "items": items,
    }
    golden_path = Path(args.golden_out)
    golden_path.parent.mkdir(parents=True, exist_ok=True)
    golden_path.write_text(json.dumps(golden, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = {
        "elapsed_sec": round(time.time() - t0, 1),
        "gpt_calls_used": budget.used,
        "gpt_calls_cap": budget.cap,
        "streams": stats,
        "golden_items": len(items),
        "golden_factual": n_f,
        "golden_broad": n_b,
        "attempts_jsonl": str(attempts_path),
        "golden_json": str(golden_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    (out_dir / f"{args.run}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
