"""메일 초안의 위키 그라운딩 (compiled wiki page 전용).

메일 초안은 지금까지 **받은 메일 본문만** 보고 쓰였다. 그래서 "Atlas 출시일이
언제냐"는 문의에 답장 초안을 만들면 날짜 자리를 `[확인 후 기입]`으로 비우거나,
더 나쁘게는 지어냈다. 회사가 이미 컴파일해 둔 위키에 답이 있는데도 초안이 그걸
못 본 것이다.

이 모듈은 `/ask`가 쓰는 **동일한 그라운딩 경로**(retrieve → answer_from_hits)를
재사용해 초안 프롬프트에 근거 블록을 얹는다. 같은 질문에 `/ask` 화면과 메일 초안이
같은 근거를 대는 것이 계약이다 — 별도 검색 경로를 만들지 않는다.

지켜야 하는 경계:
- **compiled wiki page 전용**(설계 원칙 7). raw corpus chunk를 직접 읽지 않는다.
- **학습/갭 기록 안 함**(`learn=False`, `record_gaps=False`). 메일 초안 생성은 읽기
  전용 부수효과 없는 경로다 — 초안을 만들었다고 위키가 바뀌면 안 된다.
- **fail-open**. 검색이 실패하거나 근거가 약하면 `None`을 돌려주고, 호출측은 기존
  ungrounded 초안 경로를 그대로 탄다. 그라운딩은 품질 향상이지 필수 의존이 아니다.
- **flag fail-closed**(`ORTHUS_MAIL_DRAFT_WIKI_GROUNDING_ENABLED`, default false).
"""

from __future__ import annotations

import re
from uuid import UUID

from orthus.audit import audit
from orthus.schemas.canonical import WikiGroundingRef, WikiMailGrounding
from orthus.settings import Settings, get_settings

# 근거로 붙일 위키 히트 수. /ask 기본값(5)보다 적게 잡는다 — 초안 프롬프트는 메일
# 본문·지시와 예산을 나눠 써야 하고, 상위 3건을 넘어가면 관련 없는 페이지가 섞인다.
_TOP_K = 3

# 이 점수 아래는 "관련 문서를 못 찾음"으로 보고 근거를 안 붙인다. wiki/gap.py의
# WEAK_SCORE_THRESHOLD와 같은 값을 의도적으로 공유한다 — 같은 검색 결과를 한쪽은
# "갭"이라 부르고 다른 쪽은 "근거"라 부르는 불일치를 막는다.
_WEAK_SCORE = 0.55

# 근거 블록 1건당 발췌 상한. 위키 페이지 전문을 넣으면 초안 프롬프트가 메일 맥락을
# 밀어낸다.
_EXCERPT_MAX = 700

# 그라운딩 부재 판정 마커. qa.py가 "문맥에 없다"를 평문으로 답하므로 그 답을 근거로
# 넘기면 초안이 "정보가 없습니다"를 그대로 베낀다.
_INSUFFICIENT = (
    "정보 없",
    "정보가 없",
    "포함되어 있지 않",
    "찾을 수 없",
    "확인할 수 없",
    "알 수 없",
    "답변할 수 없",
)

_WS = re.compile(r"\s+")


def _clean(text: str, limit: int) -> str:
    return _WS.sub(" ", (text or "").strip())[:limit]


def build_grounding_query(subject: str, body: str) -> str:
    """받은 메일에서 검색 질의를 만든다.

    제목이 대개 용건("Atlas 출시일 문의")을 담으므로 제목을 앞에 두고 본문 앞부분을
    덧붙인다. 서명/인용부까지 넣으면 질의가 흐려져 본문은 앞 400자만 쓴다.
    """
    subj = _clean(subject, 200)
    # 인용문(> ...)과 서명 구분선 이후는 원 발신자의 용건이 아니다.
    lines: list[str] = []
    for raw in (body or "").splitlines():
        line = raw.strip()
        if line.startswith(">") or line.startswith("--"):
            break
        if line:
            lines.append(line)
    head = _clean(" ".join(lines), 400)
    return f"{subj} {head}".strip()


def gather_wiki_grounding(
    user_id: UUID | None,
    *,
    subject: str,
    body: str,
    settings: Settings | None = None,
) -> WikiMailGrounding | None:
    """받은 메일 내용으로 회사 위키를 조회해 초안용 근거를 만든다.

    반환이 `None`이면 호출측은 그라운딩 없이 기존 경로대로 초안을 쓴다(fail-open).
    """
    settings = settings or get_settings()
    if not settings.mail_draft_wiki_grounding_enabled:
        return None
    if user_id is None:
        # 그라운딩 조회는 tenant 가시성(scope/owner)을 user_id로 판정한다. 없으면
        # 경계를 세울 수 없으므로 조회 자체를 하지 않는다(fail-closed).
        return None

    query = build_grounding_query(subject, body)
    if len(query) < 4:
        return None

    # import를 함수 안에 두는 이유: orthus.mail은 ingest 등 가벼운 경로에서도 import되는데
    # wiki.qa는 모델 어댑터를 끌고 온다. 그라운딩을 쓰는 호출에서만 비용을 낸다.
    from orthus.wiki.qa import answer_from_hits
    from orthus.wiki.retrieve import retrieve

    try:
        with audit("mail.draft.grounding") as span:
            span.add_meta(query_len=len(query))
            hits = retrieve(user_id, query, k=_TOP_K, scope="company")
            hits = [h for h in hits if (h.score or 0.0) >= _WEAK_SCORE]
            if not hits:
                span.set_output({"grounded": False, "reason": "no_strong_hit"})
                return None
            answer = answer_from_hits(
                user_id,
                query,
                hits,
                learn=False,  # 초안 생성이 위키를 오염시키지 않는다
                record_gaps=False,  # 사용자가 물은 질문이 아니므로 갭 장부에 넣지 않는다
            )
            text = (answer.answer or "").strip()
            if not text or any(m in text for m in _INSUFFICIENT):
                span.set_output({"grounded": False, "reason": "insufficient"})
                return None
            refs = [
                WikiGroundingRef(
                    page_slug=h.page_slug,
                    title=h.title or h.page_slug,
                    excerpt=_clean(h.excerpt or "", _EXCERPT_MAX),
                    score=round(float(h.score or 0.0), 3),
                )
                for h in hits
            ]
            warning = _conflict_warning(user_id, refs)
            span.set_output({"grounded": True, "hits": len(refs), "conflicted": bool(warning)})
            return WikiMailGrounding(
                query=query, answer=text, sources=refs, conflict_warning=warning
            )
    except Exception:  # noqa: BLE001 — 그라운딩 실패가 초안 생성을 막으면 안 된다
        return None


def _conflict_warning(user_id: UUID, refs: list[WikiGroundingRef]) -> str | None:
    """근거로 쓴 페이지에 미해결 모순이 걸려 있으면 경고 문구를 만든다.

    위키 검색은 top-k 유사도라 **경합하는 주장 중 하나만** 데려온다(실측: Atlas 출시일
    질의가 '3월 31일 확정' 클레임을 1위로 올렸는데, 같은 코퍼스에 '6월 30일로 3개월 연기'가
    함께 있다). 그 상태로 초안을 쓰면 사외 메일에 철회된 날짜가 확정처럼 나간다.
    지식그래프의 `CONFLICTS_WITH`가 이 위험을 유일하게 잡아낸다 — 모순은 두 주장 **사이의
    관계**라 top-k 리스트에는 담길 자리가 없기 때문이다.

    KG 미가용/비활성은 조용히 `None`(fail-open) — 경고가 없다고 초안이 막히면 안 된다.
    """
    from orthus.kg.relations import run_kg_relation, supported_from

    for ref in refs:
        try:
            result = run_kg_relation(user_id=user_id, relation="conflicts", slug=ref.page_slug)
        except Exception:  # noqa: BLE001 — 게이트는 raise하지 않지만 방어적으로
            continue
        if not supported_from(result):
            return None  # KG 자체가 안 떠 있으면 나머지도 볼 필요 없다
        rival = [
            n.title
            for n in (result.nodes or [])
            if getattr(n, "label", None) == "WikiClaim" and n.title
        ]
        if rival:
            names = ", ".join(f"'{t}'" for t in rival[:3])
            return f"이 사실은 사내에서 아직 확정되지 않았다. 경합하는 주장이 있다: {names}."
    return None


def grounding_prompt_block(grounding: WikiMailGrounding | None) -> str:
    """초안 프롬프트에 끼워 넣을 근거 블록. 그라운딩이 없으면 빈 문자열.

    페이지 slug/제목은 **일부러 넣지 않는다** — 실측에서 모델이 `atlas-ga-target-date`
    같은 내부 slug를 답장 본문에 그대로 나열했다. 근거 출처는 리뷰어 화면이
    `payload.wiki_grounding`으로 따로 보여주는 정보이지, 사외로 나가는 본문에 들어갈
    내용이 아니다.
    """
    if grounding is None:
        return ""
    lines = [
        "",
        "회사 위키에서 확인된 사실 (사내에 이미 컴파일된 지식):",
        grounding.answer,
    ]
    if grounding.conflict_warning:
        # KG가 이 근거에 미해결 모순이 있다고 알려준 경우. 단정 대신 확인 중으로 쓰게
        # 한다 — 사외로 나가는 메일에 사내에서 아직 안 정해진 값을 확정처럼 적는 것이
        # 이 기능 최대의 위험이다.
        lines += [
            "",
            f"주의: {grounding.conflict_warning}",
            "이 항목은 확정 사실처럼 단정하지 말고, 확인 중이며 확정되는 대로 회신하겠다는 "
            "취지로 정중히 작성하라. 특정 날짜/수치를 확정 표현으로 쓰지 마라.",
            # 실측: 이 지시가 없으면 모델이 "3월 31일, 7월 15일, 6월 30일로 각각 기재되어
            # 있습니다"처럼 경합값을 전부 나열했다. 사내 미확정 상태를 사외에 그대로
            # 중계하는 것이라 단정 못지않게 나쁘다 — 값 자체를 꺼내지 않게 한다.
            "경합하는 날짜·수치를 본문에 나열하거나 사내 문서가 서로 다르다는 사실을 "
            "설명하지 마라. 회사 내부 사정은 드러내지 말고 확인 중이라는 사실만 전하라.",
        ]
    else:
        lines += [
            "",
            "위 사실에 해당하는 내용은 자리표시자로 비우지 말고 그대로 반영해 답하라.",
        ]
    lines += [
        "위 사실에 없는 것은 지어내지 말고 자리표시자로 두라.",
        "위키 페이지 이름이나 slug 같은 내부 식별자는 본문에 절대 쓰지 마라.",
    ]
    return "\n".join(lines)
