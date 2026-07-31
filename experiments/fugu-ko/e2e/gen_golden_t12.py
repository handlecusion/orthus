"""t12 생성 3종(claim_headline / gap_suggest / email_draft) 골든셋 n=1,000 생성기.

이 3종은 LLM judge 없이 `t12_generation.py::score()`의 결정론 지표(형식 실패율 ·
`_invented` 환각 · 길이/섹션 규격 · 지연)로 채점된다. 따라서 골든은 **입력 문항만**
있으면 되고, 정답 라벨이 없다.

전부 **결정론**이다 — LLM을 쓰지 않고, `random`도 쓰지 않는다. 순서/샘플링은 전부
`sha256(salt|key)` 정렬이라 실행 시점·플랫폼과 무관하게 같은 결과가 나온다.
(claim_headline만 로컬 wiki-store를 읽으므로 그 스냅샷에 의존한다.)

    python experiments/fugu-ko/e2e/gen_golden_t12.py            # 3종 전부
    python experiments/fugu-ko/e2e/gen_golden_t12.py --task gap
    python experiments/fugu-ko/e2e/gen_golden_t12.py --check    # 쓰지 않고 검증만
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
FUGU = HERE.parent
GOLDEN = FUGU / "golden"
REPO = FUGU.parent.parent

N_TARGET = 1000

CLAIMS_DIR = Path(
    os.environ.get("FUGU_WIKI_STORE", str(Path.home() / ".orthus/nodes/company/wiki-store/company"))
) / "claims"


def _h(salt: str, key: str) -> str:
    return hashlib.sha256(f"{salt}|{key}".encode()).hexdigest()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip().casefold()


# ───────────────────────────── 1. claim_headline ─────────────────────────────
# 원천 = 회사 wiki-store의 claim markdown(SoR). 프로덕션 distill이 헤드라인을 붙이는
# 그 클레임 본문 그대로다. 필터(40~300자)는 t12_generation._claims()와 동일하다.
#
# 기존 코드는 `sorted(glob)` 앞 20개를 잘라 써서 슬러그 알파벳 앞쪽(숫자로 시작하는
# 진척도/날짜 클레임)에 심하게 편향돼 있었다. 여기서는 **슬러그 sha256 정렬**로 전 코퍼스에
# 균등하게 흩뿌린 뒤 앞에서 1,000개를 취한다 — 재현 가능하면서 편향이 없다.


def gen_claim_headline() -> dict:
    rows: list[tuple[str, str]] = []
    for f in sorted(CLAIMS_DIR.glob("*.md")):
        txt = f.read_text(encoding="utf-8")
        m = re.search(r"^## Claim\s*\n+(.+?)(?=\n##|\Z)", txt, re.S | re.M)
        if not m:
            continue
        claim = " ".join(m.group(1).split())
        if not (40 <= len(claim) <= 300):
            continue
        proj = re.search(r'^project: "(.*)"', txt, re.M)
        rows.append((f.stem, claim, proj.group(1) if proj else ""))

    rows.sort(key=lambda r: _h("t12-headline", r[0]))

    seen: set[str] = set()
    items = []
    for slug, claim, proj in rows:
        k = _norm(claim)
        if k in seen:  # 같은 배포일 클레임이 슬러그만 달리해 82건 중복돼 있다
            continue
        seen.add(k)
        items.append(
            {"id": f"h{len(items) + 1:04}", "slug": slug, "project": proj, "claim": claim}
        )
        if len(items) >= N_TARGET:
            break

    return {
        "task": "T12-claim_headline",
        "desc": (
            "wiki distill의 claim→headline 압축(orthus.wiki.distill.HEADLINE_SYSTEM). "
            "채점=결정론(빈 출력 실패율 · 120자 상한 초과로 프로덕션 `_one_line_cap` 말줄임 · "
            "`_invented(latin=False)` 로 클레임에 없는 수치/날짜 삽입 · p50 지연). LLM judge 없음."
        ),
        "note": (
            f"원천 = company wiki-store claims/*.md 의 `## Claim` 본문, 프로덕션과 동일한 40~300자 "
            f"필터 통과분 2,691건에서 정규화 텍스트 중복 제거(2,609건) 후 "
            f"sha256('t12-headline|<slug>') 정렬 상위 {N_TARGET}건. 슬러그 알파벳 순서를 그대로 "
            "자르던 기존 N_CLAIMS=20 방식의 편향(숫자 시작 슬러그 쏠림)을 제거했다. "
            "정답 라벨 없음 — 채점은 클레임 원문을 grounding으로 쓰는 결정론 지표."
        ),
        "items": items,
    }


# ───────────────────────────── 2. gap_suggest ─────────────────────────────
# 실데이터 = company PG `data_gaps` 57행(전부 status=open). 그 57건을 시드로 넣고,
# 나머지를 **실측 reason 분포 그대로** 합성 확장한다.
#
# reason 은 프로덕션 enum(orthus.schemas.canonical.GapReason) 4종만 쓴다. 기존 인라인
# 골든의 `no_hits` / `low_confidence` 는 프로덕션에 없는 값이었다(버그).
GAP_REASONS = ("insufficient_grounding", "missing_link", "no_data", "weak_retrieval")
GAP_REAL_DIST = {"insufficient_grounding": 40, "missing_link": 10, "no_data": 5, "weak_retrieval": 2}

# 회사 위키에 실재하는 주제군. atlas 4,638 / nova 2,072 / company 1,344 / orbit 176
# (claim project 분포)에 맞춰 atlas·nova를 두껍게 뒀다.
GAP_TOPICS: dict[str, list[str]] = {
    "atlas": [
        "촬영 현장 등록 절차", "출연자 프로필 등록 절차", "공고 지원 절차", "지원서 정렬 기준",
        "면접 일정 조율 방식", "아틀라스 매칭 기준", "촬영 자동 종료 정책", "출연료 정산 절차",
        "촬영 취소 및 환불 규정", "관리자 권한 체계", "2FA 미인증 사용자 처리 정책",
        "지원자 알림 발송 규칙", "촬영 현장 목록 필터 동작", "배역 공고 게시 기준",
        "촬영 일정 변경 절차", "타사 면접 결과 노출 정책", "지명 리스트 수정 권한",
        "촬영 현장 아이콘 버튼 동작", "출연자 등급 산정 기준", "공고 마감 처리 규칙",
        "촬영 현장 사진 업로드 제한", "지원자 프로필 비공개 설정", "아틀라스 계약서 발송 흐름",
        "촬영 당일 체크리스트", "출연자 노쇼 처리 정책", "공고 지원 취소 규정",
        "면접관 배정 규칙", "촬영 장소 예약 절차", "출연자 신원 확인 절차",
        "아틀라스 수수료 정책", "촬영 중 발생하는 오류 유형", "지원서 첨부 파일 용량 제한",
    ],
    "nova": [
        "자막 파서 지원 포맷", "자막 패널 렌더링 동작", "라이브아바타 리소스 최적화",
        "다크 테마 적용 범위", "영상 생성 파이프라인 구성", "알림 기능 알려진 이슈",
        "음성 합성 품질 기준", "프레임 수 조정 규칙", "MVP 마일스톤 범위",
        "타깃 고객 정의", "자막 스타일 편집 기능", "영상 렌더링 대기 시간",
        "자막 싱크 보정 방식", "영상 내보내기 해상도 옵션", "썸네일 자동 생성 규칙",
        "자막 번역 파이프라인", "아바타 카테고리 분류 기준", "영상 업로드 용량 제한",
        "구독 요금제 구성", "체험판 제공 기간", "자막 폰트 라이선스",
        "영상 저장 보관 기간", "오디오 트랙 분리 기능", "배경 음악 라이브러리 정책",
    ],
    "company": [
        "휴가 정책", "연차 이월 규정", "경비 처리 절차", "출장비 정산 기한",
        "보안 교육 주기", "재택근무 신청 방법", "신규 입사자 온보딩 일정",
        "인사 평가 기준", "채용 프로세스", "파트너사 계약 갱신 주기",
        "전자계약 절차", "사무실 출입 관리", "장비 구매 승인 라인",
        "예산 초과 시 보고 라인", "고객 데이터 보관 기간", "인턴 평가 기준",
        "복리후생 항목", "퇴사 인수인계 절차", "개인정보 처리 방침",
        "외부 공유 문서 승인 절차", "사내 보안 수칙", "회의록 작성 규칙",
        "부서별 업무 분장", "협력사 선정 기준", "지식재산권 귀속 규정",
    ],
    "platform": [
        "Slack 커넥터 연동 상태", "Gmail 커넥터 설정 방법", "Notion 임포트 범위",
        "위키 컴파일 주기", "에이전트 위임 절차", "메일 자동 발송 정책",
        "지식그래프 갱신 주기", "커넥터 동기화 실패 처리", "권한 승인 흐름",
        "데이터 갭 백로그 운영", "위키 태스크 해소 기준", "개인 노드와 회사 노드 경계",
        "프로모트 승인 절차", "감사 로그 보관 정책", "임베딩 모델 전환 절차",
    ],
    "orbit": [
        "orbit 서비스 범위", "orbit 파트너 연동 방식", "orbit 요금 정책",
        "orbit 운영 담당", "orbit 출시 일정",
    ],
}
GAP_TOPIC_ALL = [t for v in GAP_TOPICS.values() for t in v]

ALLOWED_ML_AREA_PAIRS = (
    ("atlas", "nova"), ("atlas", "orbit"), ("nova", "orbit"),
    ("atlas", "platform"), ("nova", "platform"), ("orbit", "platform"),
    ("company", "platform"),
)

# 템플릿에 주제를 그대로 꽂으면 조사가 깨진다("체크리스트은", "규칙와", "방식를").
# 앞 음절의 받침으로 결정론적으로 고른다. 프레임에서는 `{t}%은` 처럼 표기한다.
_JOSA = {"은": ("은", "는"), "이": ("이", "가"), "을": ("을", "를"), "와": ("과", "와")}
_JOSA_RE = re.compile(rf"(.)%({'|'.join(_JOSA)})")


def _josa(m: re.Match) -> str:
    ch, kind = m.group(1), m.group(2)
    code = ord(ch)
    if not (0xAC00 <= code <= 0xD7A3):  # 한글 음절이 아니면 판정 불가 → 생성기에서 막는다
        raise ValueError(f"josa target is not a Hangul syllable: {ch!r}")
    batchim = (code - 0xAC00) % 28 != 0
    return ch + _JOSA[kind][0 if batchim else 1]


def _fill(frame: str, **kw: str) -> str:
    return _JOSA_RE.sub(_josa, frame.format(**kw))

# reason 별 발화 형태. 실 데이터의 모양을 그대로 따라간다:
#   no_data            = 키워드/한 단어에 가까운 짧은 발화("보안 수칙 알려줘", "Itx-STUDIO")
#   insufficient_grounding = 정상적인 상세 지식 질문(전체의 70%)
#   missing_link       = 두 주제 사이 관계/연결 질문
#   weak_retrieval     = 지시어가 붕 뜬 모호한 요약 요청
GAP_FRAMES = {
    "insufficient_grounding": [
        "{t}%이 어떻게 되나요?",
        "{t}에 대해 자세히 알려줘",
        "{t} 관련 규정이 문서로 정리돼 있어?",
        "{t}%은 누가 담당해?",
        "{t}%을 단계별로 설명해줘",
        "{t}의 기준이 뭐야?",
        "{t} 현황을 정리해줘",
        "{t}에서 자주 발생하는 문제는 뭐야?",
        "{t} 변경 이력을 알려줘",
        "{t}%은 언제부터 적용됐어?",
        "{t} 내용을 요약해서 알려줘",
        "{t}에 예외가 있는 경우가 있어?",
        "{t}%을 처음 접하는 사람한테 설명한다면 어떻게 말해야 해?",
        "{t}%와 관련해서 우리가 정해 둔 원칙이 뭐야?",
    ],
    "missing_link": [
        "{a}%와 {b}%은 무슨 관계야?",
        "{a}%은 어떤 것들과 연결돼 있어?",
        "{a}%와 {b} 사이에 연관된 문서가 있어?",
        "{a} 관련 자료랑 {b} 관련 자료를 이어서 설명해줘",
        "{a}%이 {b}에 영향을 주는 부분이 있어?",
        "{a}%와 {b}%을 같이 보려면 어디를 봐야 해?",
    ],
    "no_data": [
        "{t}",
        "{t} 알려줘",
        "{t}%은?",
        "{t} 어디 있어",
    ],
    "weak_retrieval": [
        "{t} 쪽 내용 대충 요약해줘",
        "{t} 관련해서 아는 거 아무거나 알려줘",
        "{t} 그거 어떻게 되는지만 짧게",
        "{t} 정리된 거 있으면 그냥 보여줘",
    ],
}


def _largest_remainder(total: int, weights: dict[str, int]) -> dict[str, int]:
    """실측 분포 비율을 정수 total 에 배분(최대 잉여법). 합이 정확히 total 이 된다."""
    s = sum(weights.values())
    exact = {k: total * v / s for k, v in weights.items()}
    base = {k: int(v) for k, v in exact.items()}
    rest = total - sum(base.values())
    for k in sorted(exact, key=lambda k: (-(exact[k] - base[k]), k))[:rest]:
        base[k] += 1
    return base


def _read_real_gaps() -> list[dict]:
    """company PG `data_gaps` 57행. DSN 은 node.env 에서 읽는다(하드코딩 없음)."""
    import psycopg

    env = Path.home() / ".orthus/nodes/company/node.env"
    txt = env.read_text(encoding="utf-8")
    dsn = re.search(r'^ORTHUS_PG_DSN="?([^"\n]+)"?', txt, re.M).group(1)
    db = re.search(r"^ORTHUS_NODE_DB=(\S+)", txt, re.M).group(1)
    dsn = dsn.replace("${ORTHUS_NODE_DB}", db).replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as c:
        rows = c.execute(
            "select question, reason, scope, status from data_gaps "
            "where question is not null and btrim(question) <> '' "
            "order by created_at, gap_id"
        ).fetchall()
    return [{"q": q, "reason": r, "scope": s, "status": st} for q, r, s, st in rows]


def gen_gap_suggest(real: list[dict]) -> dict:
    items: list[dict] = []
    seen: set[str] = set()
    for i, r in enumerate(real, 1):
        assert r["reason"] in GAP_REASONS, f"unknown reason {r['reason']}"
        k = _norm(r["q"])
        if k in seen:
            continue
        seen.add(k)
        items.append(
            {"id": f"g-real-{i:03}", "q": r["q"], "reason": r["reason"], "src": "real"}
        )

    need = N_TARGET - len(items)
    quota = _largest_remainder(need, GAP_REAL_DIST)

    # reason 별 후보 풀을 만들고 sha256 정렬해 앞에서 quota 만큼 취한다.
    pools: dict[str, list[str]] = {}
    for reason in GAP_REASONS:
        cands: list[str] = []
        if reason == "missing_link":
            # 서로 다른 영역의 주제를 짝지어야 "연결이 없다"가 자연스럽다. 단 아무 두 영역이나
            # 붙이면 "인턴 평가 기준과 아바타 카테고리 분류 기준의 관계"처럼 사람이 실제로는
            # 묻지 않을 조합이 나온다 — 제품↔제품/제품↔플랫폼/사내운영↔플랫폼만 허용한다.
            for a_area, b_area in ALLOWED_ML_AREA_PAIRS:
                for a, b in itertools.product(GAP_TOPICS[a_area], GAP_TOPICS[b_area]):
                    for f in GAP_FRAMES[reason]:
                        cands.append(_fill(f, a=a, b=b))
        else:
            for t in GAP_TOPIC_ALL:
                for f in GAP_FRAMES[reason]:
                    cands.append(_fill(f, t=t))
        cands.sort(key=lambda q: _h(f"t12-gap-{reason}", q))
        pools[reason] = cands

    for reason in GAP_REASONS:
        taken = 0
        for q in pools[reason]:
            if taken >= quota[reason]:
                break
            k = _norm(q)
            if k in seen:
                continue
            seen.add(k)
            taken += 1
            items.append(
                {
                    "id": f"g-syn-{len(items) + 1:04}",
                    "q": q,
                    "reason": reason,
                    "src": "synth",
                }
            )
        assert taken == quota[reason], f"{reason}: pool exhausted ({taken}/{quota[reason]})"

    dist = {r: sum(1 for i in items if i["reason"] == r) for r in GAP_REASONS}
    return {
        "task": "T12-gap_suggest",
        "desc": (
            "데이터 갭 보완 제안(orthus.wiki.gap._SUGGEST_SYSTEM, json_only). 채점=결정론"
            "(JSON 파싱 실패율 · 프롬프트 규격 2~4 섹션 위반 수 · 평균 섹션/항목 수 · p50 지연). "
            "LLM judge 없음."
        ),
        "note": (
            f"실데이터 시드 {len(real)}건 = company PG `data_gaps` 전량(전부 status=open). "
            f"나머지 {need}건은 실측 reason 분포(insufficient_grounding 40 / missing_link 10 / "
            f"no_data 5 / weak_retrieval 2 = 57)를 최대잉여법으로 보존해 합성했다. "
            f"최종 분포 {dist}. reason 은 프로덕션 enum(GapReason 4종)만 쓴다 — 기존 인라인 골든의 "
            "`no_hits`/`low_confidence` 는 프로덕션에 없는 값이었다(스키마 위반 버그). "
            "합성 문항의 주제는 회사 wiki 실제 도메인(atlas/nova/company/platform/orbit)에서 "
            "뽑았고, 발화 형태는 reason 별 실데이터 모양을 따랐다(no_data=키워드성 짧은 발화, "
            "missing_link=서로 다른 영역 주제쌍의 관계 질문, weak_retrieval=지시어가 뜬 모호한 요약 요청). "
            "정답 라벨 없음."
        ),
        "items": items,
    }


# ───────────────────────────── 3. email_draft ─────────────────────────────
# 현행 DB에 실메일 본문이 0건이라 **템플릿 합성만** 한다(owner 결정).
# 축 = 수신자 유형 × 요청 의도 × ctx 유무.
#
# `_invented` 채점이 변별력을 가지려면 "지어낼 여지"가 필요하다:
#   - ctx 없는 문항(약 60%) — 날짜/금액/수량이 지시에 전혀 없다. 모델이 대괄호로 비우는지,
#     아니면 "3월 15일 오후 2시" 를 지어내는지가 그대로 갈린다.
#   - ctx 있는 문항(약 40%) — 구체적 고유명사·수치가 주어진다. 그 안에 없는 숫자를
#     추가로 심으면 환각이다(주어진 것을 그대로 쓰면 grounding 에 있으므로 무죄).
#
# 수신자 클래스. intent 의 `aud` 와 교집합으로 조합해 "지원자에게 서버 증설 요청" 같은
# 말이 안 되는 짝을 만들지 않는다.
EMAIL_RECIPIENTS: list[tuple[str, str]] = [
    # (이름, 클래스)
    ("김민수 팀장", "peer"), ("이수진 매니저", "peer"), ("박지훈 대리", "peer"),
    ("김하늘 팀장", "peer"), ("정우성 이사", "exec"), ("최은영 부장", "exec"),
    ("한지민 과장", "peer"), ("오세훈 차장", "peer"),
    ("팀 전체", "group"), ("전 직원", "group"), ("개발팀", "group"),
    ("디자인팀", "group"), ("운영팀", "group"), ("아틀라스 운영팀", "group"),
    ("PM", "pm"), ("프로덕트 오너", "pm"),
    ("회계 담당", "fin"), ("재무 담당", "fin"), ("정산 담당자", "fin"),
    ("법무 검토자", "legal"), ("계약 검토 담당", "legal"),
    ("인프라 담당", "infra"), ("플랫폼 엔지니어", "infra"),
    ("채용 담당", "hr"), ("인사 담당", "hr"),
    ("보안 감사 담당", "sec"), ("정보보호 담당", "sec"),
    ("데이터 담당자", "data"), ("고객지원 담당", "support"),
    ("파트너사 담당자", "partner"), ("협력사", "partner"), ("거래처", "partner"),
    ("디자인 파트너", "partner"), ("번역 파트너", "partner"), ("외주 개발사", "vendor"),
    ("외부 검토자", "vendor"), ("외부 감사인", "vendor"), ("클라우드 벤더 담당자", "vendor"),
    ("고객사", "customer"), ("고객", "customer"), ("고객사 담당자", "customer"),
    ("서비스 이용 고객", "customer"),
    ("지원자", "candidate"), ("신규 입사자", "candidate"), ("인턴", "candidate"),
    ("채용 후보자", "candidate"),
    ("투자사", "investor"), ("투자사 심사역", "investor"),
    ("owner01@nova.example", "peer"), ("contact@acme.example", "partner"),
]

# ctx 뱅크 — 전부 회사 실제 도메인의 사실 형태이며 고유명사·수치를 포함한다.
CTX = {
    "sprint": "Sprint 6에서 자막 패널 렌더링 버그를 수정했고, 다크 테마는 설정 화면까지 적용됐다. 영상 생성 파이프라인 개선은 다음 스프린트로 이월됐다.",
    "sprint_rate": "Sprint 6 완료율은 82%이고, 오디오 P0 항목 2건이 미완이다.",
    "avatar": "라이브아바타 일반 카테고리 리소스 최소화 작업으로 초기 로딩이 40% 줄었다.",
    "avatar_gpu": "리소스 최소화 작업으로 초기 로딩이 40% 줄었으나, 추가 최적화에는 GPU가 더 필요하다.",
    "qr": "출국 QR 오류가 재현돼 릴리스를 3일 미뤄야 한다.",
    "rollback": "출국 QR 오류로 릴리스를 롤백했다. 원인 분석 중이다.",
    "subtitle_bug": "자막 패널 렌더링에서 이미지 중복 오류가 남아 있다.",
    "atlas_apply": "아틀라스 공고 지원자 수가 지난달 대비 27% 늘어 1,240명을 기록했다.",
    "atlas_close": "촬영 자동 종료 정책이 24시간에서 12시간으로 조정됐다.",
    "slack_fail": "Slack 커넥터 동기화가 3회 연속 실패해 최근 이틀치 스레드가 누락됐다.",
    "cert": "서버 인증서 만료가 14일 남았고, 갱신 작업에는 약 30분의 다운타임이 필요하다.",
    "2fa": "2FA 미인증 사용자에게는 일부 공고가 숨김 처리되도록 정책이 바뀌었다.",
    "traffic": "라이브아바타 트래픽이 늘어 초기 로딩이 느려졌다.",
    "budget": "이번 분기 인프라 비용이 예산 대비 18% 초과했다.",
    "hire": "1차 서류 검토를 마쳤고 통과자는 6명이다.",
    "audit": "보안 점검에서 중간 등급 지적 2건이 나왔고, 둘 다 로그 보관 설정과 관련돼 있다.",
    "notion": "Notion 임포트 범위를 회사 개요와 프로젝트 DB 두 곳으로 좁혔다.",
    "sort": "지원서 정렬 순서를 최신순에서 매칭 점수순으로 변경했다.",
    "refund": "촬영 취소 환불 규정이 촬영 24시간 전 기준으로 정리됐다.",
    "mvp": "MVP 마일스톤에는 자막 편집과 영상 내보내기 두 기능만 포함된다.",
}

# 내부 기능 조직 전체(범용 사내 커뮤니케이션에만 쓴다).
_ROLES = ("pm", "fin", "legal", "infra", "hr", "sec", "data", "support")

# (키, 지시문, 대상 클래스, ctx 후보). ctx 후보가 비면 ctx 없는 문항만 만들어진다.
# 대상 클래스는 **그 지시가 실제로 갈 만한 상대**로 좁힌다 — "지원자에게 서버 증설 요청",
# "채용 담당에게 영수증 재발행 요청" 같은 조합이 나오면 문항이 현실성을 잃는다.
EMAIL_INTENTS: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = [
    ("meet_propose", "다음 주 미팅 일정을 제안하는 메일", ("peer", "exec", "partner", "customer", "vendor", "investor", *_ROLES), ()),
    ("meet_reschedule", "이 내용대로 일정 재조정을 요청하는 메일", ("peer", "pm", "partner", "vendor", "customer"), ("qr", "rollback", "sprint_rate")),
    ("meet_cancel", "예정된 미팅을 부득이하게 취소한다고 정중히 안내", ("peer", "partner", "customer", "vendor", "investor", "pm"), ()),
    ("meet_agenda", "다음 회의 안건을 사전에 공유해달라고 요청", ("peer", "pm", "group", "partner", "exec"), ()),
    ("decline_quote", "제안 주신 견적은 예산을 초과해 이번엔 어렵다고 정중히 거절", ("partner", "vendor"), ("budget",)),
    ("decline_offer", "공동 제안은 내부 검토 결과 이번에는 진행이 어렵다고 회신", ("partner", "vendor", "investor"), ()),
    ("hire_pass", "서류 전형 합격 안내와 면접 일정 조율 요청", ("candidate",), ("hire",)),
    ("hire_reject", "최종 불합격 통보를 정중하게", ("candidate",), ()),
    ("hire_offer", "최종 합격 안내와 입사 서류 준비 요청", ("candidate",), ()),
    ("hire_status", "채용 진행 상황을 중간 공유", ("hr", "peer", "exec"), ("hire",)),
    ("onboard", "온보딩 첫 주 일정 안내", ("candidate",), ()),
    ("onboard_doc", "온보딩에 필요한 제출 서류를 안내", ("candidate",), ()),
    ("progress", "이 내용을 기반으로 진행 상황 공유 메일 작성", ("customer", "partner", "exec", "investor", "peer", "pm"), ("sprint", "sprint_rate", "avatar", "atlas_apply")),
    ("progress_plain", "이번 주 진행 상황을 정리해 공유", ("customer", "partner", "exec", "peer", "group", "pm"), ()),
    ("share_result", "이 결과를 정리해서 공유해줘", ("peer", "group", "exec", "pm", "data"), ("avatar", "sprint_rate", "atlas_apply", "mvp")),
    ("share_before_meeting", "이 내용을 정리해서 회의 전에 공유해달라고 요청", ("peer", "pm", "group", "exec"), ("sprint_rate", "atlas_apply", "audit")),
    ("apology_invoice", "청구서 발행 지연에 대해 사과하고 예상 처리 일정을 알림", ("partner", "vendor", "customer", "fin"), ()),
    ("apology_outage", "서비스 중단에 대해 사과하고 재발 방지 대책을 안내", ("customer", "partner", "support"), ("rollback",)),
    ("review_request", "보안 점검 결과 회신 요청", ("vendor", "sec"), ("audit",)),
    ("review_result", "산출물 검수 결과와 수정 요청 전달", ("vendor", "partner"), ("subtitle_bug", "sprint")),
    ("explain_change", "정렬 순서 변경 배경을 설명하는 메일", ("hr", "pm", "peer", "customer", "group", "support"), ("sort",)),
    ("explain_policy", "정책 변경 배경과 적용 시점을 설명", ("group", "customer", "partner", "hr", "legal", "support"), ("2fa", "atlas_close", "refund")),
    ("notice_cert", "서버 인증서 갱신 작업 공지", ("group", "infra", "sec"), ("cert",)),
    ("notice_security_edu", "정기 보안 교육 일정 공지", ("group", "sec"), ()),
    ("notice_maintenance", "서비스 점검으로 인한 일시 중단 안내", ("customer", "group", "partner", "support"), ("cert",)),
    ("notice_office", "사무실 이전 일정과 준비 사항 공지", ("group",), ()),
    ("notice_rollback", "이번 배포 롤백 사실과 후속 조치 공지", ("group", "peer", "pm", "infra", "support"), ("rollback", "qr")),
    ("notice_holiday", "연휴 기간 근무 및 고객 응대 일정 공지", ("group", "customer", "partner", "support"), ()),
    ("request_receipt", "비용 영수증 재발행을 요청", ("partner", "vendor", "fin"), ()),
    ("request_contract", "계약 갱신 의사를 확인하는 메일", ("partner", "vendor"), ()),
    ("request_legal", "계약서 조항 해석에 대한 자문 요청", ("legal", "vendor"), ()),
    ("request_infra", "서버 증설이 필요하다고 요청", ("infra", "vendor", "exec"), ("traffic", "budget")),
    ("request_budget", "이 결과를 근거로 예산 증액을 건의", ("exec", "fin"), ("avatar_gpu", "budget", "traffic")),
    ("request_data", "요청한 데이터 추출 범위를 확정해달라고 요청", ("data", "peer", "partner", "pm"), ("notion",)),
    ("request_feedback", "초안에 대한 피드백을 이번 주 내로 달라고 요청", ("peer", "group", "pm", "partner", "exec"), ()),
    ("request_approval", "다음 단계 진행을 위한 승인을 요청", ("exec", "pm", "legal", "fin"), ("budget", "mvp")),
    ("send_attach", "지난번 요청하신 자료를 첨부해 보낸다고 안내", ("peer", "partner", "customer", "fin", "legal", "vendor", "pm"), ()),
    ("send_settlement", "출장비 정산 서류를 제출한다고 알림", ("fin",), ()),
    ("confirm_refund", "환불 요청을 접수했다고 확인 메일", ("customer", "support"), ("refund",)),
    ("confirm_receipt", "보내주신 자료를 잘 받았다고 확인하고 검토 일정을 안내", ("partner", "vendor", "customer", "candidate"), ()),
    ("guide_process", "레퍼런스 추출 절차를 안내", ("partner", "vendor"), ()),
    ("guide_tone", "자연스러운 톤 표현 가이드를 전달하며 재작업 요청", ("partner", "vendor"), ()),
    ("guide_access", "시스템 접근 권한 신청 방법을 안내", ("candidate", "vendor", "group", "partner", "infra"), ()),
    ("investor_update", "분기 진행 상황 업데이트", ("investor",), ("sprint_rate", "atlas_apply", "budget")),
    ("investor_meeting", "다음 분기 리뷰 미팅 일정을 조율", ("investor", "exec"), ()),
    ("escalate_issue", "이 문제를 상위 보고하고 대응 방향을 논의하자고 제안", ("exec", "pm", "peer", "infra"), ("slack_fail", "qr", "audit")),
    ("report_incident", "장애 발생 경위와 현재 상태를 보고", ("exec", "group", "customer", "support", "infra"), ("rollback", "slack_fail", "qr")),
    ("ask_status", "진행 상황을 확인하고 회신을 요청", ("vendor", "partner", "peer", "pm"), ()),
    ("ask_availability", "담당자 배정 가능 여부를 확인", ("vendor", "partner", "pm", "hr"), ()),
    ("thanks", "이번 협업에 대한 감사 인사와 후속 논의 제안", ("partner", "vendor", "customer", "peer", "investor"), ()),
    ("intro", "새로 담당하게 됐다는 인사와 연락 창구 안내", ("partner", "customer", "vendor", "peer", "pm"), ()),
    ("handover", "담당 업무 인수인계 내용을 정리해 전달", ("peer", "pm", "group", "exec"), ("notion", "sprint")),
    ("survey", "만족도 설문 참여를 요청", ("customer", "group", "partner"), ()),
    ("reminder", "회신 기한이 지났음을 정중히 상기", ("vendor", "partner", "pm", "peer", "fin"), ()),
    ("notify_delay", "일정이 지연될 것 같다고 미리 알리고 양해를 구함", ("customer", "partner", "exec", "peer", "pm"), ("qr", "subtitle_bug")),
    ("notify_fix", "보고된 문제를 수정 완료했다고 안내", ("customer", "partner", "peer", "group", "support"), ("subtitle_bug", "sprint")),
    ("notify_sync_fail", "커넥터 동기화 실패로 일부 데이터가 누락됐다고 안내", ("group", "data", "infra", "peer"), ("slack_fail",)),
    ("close_project", "프로젝트 종료를 알리고 산출물 인계 일정을 안내", ("partner", "vendor", "customer", "group", "pm"), ("mvp",)),
    ("ask_quote", "작업 범위에 대한 견적을 요청", ("vendor", "partner"), ()),
]

_ORIGINAL_EMAIL_ITEMS = [
    {"id": "e01", "to": "파트너사 담당자", "inst": "다음 주 미팅 일정을 제안하는 메일", "ctx": ""},
    {"id": "e02", "to": "김민수 팀장", "inst": "제안 주신 견적은 예산을 초과해 이번엔 어렵다고 정중히 거절", "ctx": ""},
    {"id": "e03", "to": "지원자", "inst": "서류 전형 합격 안내와 면접 일정 조율 요청", "ctx": ""},
    {"id": "e04", "to": "고객사", "inst": "이 내용을 기반으로 진행 상황 공유 메일 작성", "ctx": CTX["sprint"]},
    {"id": "e05", "to": "협력사", "inst": "청구서 발행 지연에 대해 사과하고 예상 처리 일정을 알림", "ctx": ""},
    {"id": "e06", "to": "owner01@nova.example", "inst": "이 결과를 정리해서 공유해줘", "ctx": CTX["avatar"]},
    {"id": "e07", "to": "외부 검토자", "inst": "보안 점검 결과 회신 요청", "ctx": ""},
    {"id": "e08", "to": "채용 담당", "inst": "채용 공고 정렬 순서 변경 배경을 설명하는 메일", "ctx": ""},
    {"id": "e09", "to": "팀 전체", "inst": "서버 인증서 갱신 작업 공지", "ctx": ""},
    {"id": "e10", "to": "거래처", "inst": "비용 영수증 재발행을 요청", "ctx": ""},
    {"id": "e11", "to": "PM", "inst": "이 내용대로 일정 재조정을 요청하는 메일", "ctx": CTX["qr"]},
    {"id": "e12", "to": "디자인 파트너", "inst": "레퍼런스 추출 절차를 안내", "ctx": ""},
    {"id": "e13", "to": "전 직원", "inst": "정기 보안 교육 일정 공지", "ctx": ""},
    {"id": "e14", "to": "협력사", "inst": "계약 갱신 의사를 확인하는 메일", "ctx": ""},
    {"id": "e15", "to": "이수진 매니저", "inst": "지난번 요청하신 자료를 첨부해 보낸다고 안내", "ctx": ""},
    {"id": "e16", "to": "팀 전체", "inst": "이번 배포 롤백 사실과 후속 조치 공지", "ctx": CTX["rollback"]},
    {"id": "e17", "to": "고객사 담당자", "inst": "서비스 점검으로 인한 일시 중단 안내", "ctx": ""},
    {"id": "e18", "to": "지원자", "inst": "최종 불합격 통보를 정중하게", "ctx": ""},
    {"id": "e19", "to": "외주 개발사", "inst": "산출물 검수 결과와 수정 요청 전달", "ctx": CTX["subtitle_bug"]},
    {"id": "e20", "to": "회계 담당", "inst": "출장비 정산 서류를 제출한다고 알림", "ctx": ""},
    {"id": "e21", "to": "파트너사", "inst": "공동 마케팅 제안에 대한 내부 검토 결과 회신", "ctx": ""},
    {"id": "e22", "to": "박지훈 대리", "inst": "이 내용을 정리해서 회의 전에 공유해달라고 요청", "ctx": CTX["sprint_rate"]},
    {"id": "e23", "to": "신규 입사자", "inst": "온보딩 첫 주 일정 안내", "ctx": ""},
    {"id": "e24", "to": "고객", "inst": "환불 요청을 접수했다고 확인 메일", "ctx": ""},
    {"id": "e25", "to": "법무 검토자", "inst": "계약서 조항 해석에 대한 자문 요청", "ctx": ""},
    {"id": "e26", "to": "인프라 담당", "inst": "서버 증설이 필요하다고 요청", "ctx": CTX["traffic"]},
    {"id": "e27", "to": "전 직원", "inst": "사무실 이전 일정과 준비 사항 공지", "ctx": ""},
    {"id": "e28", "to": "투자사", "inst": "분기 진행 상황 업데이트", "ctx": ""},
    {"id": "e29", "to": "번역 파트너", "inst": "자연스러운 톤 표현 가이드를 전달하며 재작업 요청", "ctx": ""},
    {"id": "e30", "to": "김하늘 팀장", "inst": "이 결과를 근거로 예산 증액을 건의", "ctx": CTX["avatar_gpu"]},
]

CTX_SHARE = 0.40  # 합성분 중 참고자료가 붙는 비율


def gen_email_draft() -> dict:
    by_class: dict[str, list[str]] = {}
    for name, cls in EMAIL_RECIPIENTS:
        by_class.setdefault(cls, []).append(name)

    free: list[tuple[str, str, str]] = []   # (to, inst, "")
    withctx: list[tuple[str, str, str]] = []
    for key, inst, auds, ctx_keys in EMAIL_INTENTS:
        recips = [r for a in auds for r in by_class.get(a, [])]
        for to in recips:
            free.append((to, inst, ""))
            for ck in ctx_keys:
                withctx.append((to, inst, CTX[ck]))

    def _key(t: tuple[str, str, str]) -> str:
        return f"{t[0]}|{t[1]}|{t[2][:24]}"

    free.sort(key=lambda t: _h("t12-email-free", _key(t)))
    withctx.sort(key=lambda t: _h("t12-email-ctx", _key(t)))

    items = [dict(it, src="original") for it in _ORIGINAL_EMAIL_ITEMS]
    seen = {(_norm(i["to"]), _norm(i["inst"]), _norm(i["ctx"])) for i in items}

    need = N_TARGET - len(items)
    n_ctx = round(need * CTX_SHARE)
    n_free = need - n_ctx

    def _take(pool: list[tuple[str, str, str]], n: int, tag: str) -> list[dict]:
        out = []
        for to, inst, ctx in pool:
            if len(out) >= n:
                break
            k = (_norm(to), _norm(inst), _norm(ctx))
            if k in seen:
                continue
            seen.add(k)
            out.append({"to": to, "inst": inst, "ctx": ctx})
        assert len(out) == n, f"{tag}: pool exhausted ({len(out)}/{n})"
        return out

    picked = _take(free, n_free, "free") + _take(withctx, n_ctx, "ctx")
    # 두 풀을 그대로 이어 붙이면 앞쪽 600문항이 전부 ctx 없음이 된다. 부분 실행(--limit)에서도
    # 두 종류가 섞이도록 한 번 더 해시 정렬한다.
    picked.sort(key=lambda d: _h("t12-email-mix", f"{d['to']}|{d['inst']}|{d['ctx'][:24]}"))
    for d in picked:
        items.append({"id": f"e-syn-{len(items) + 1:04}", "src": "synth", **d})

    n_with = sum(1 for i in items if i["ctx"])
    return {
        "task": "T12-email_draft",
        "desc": (
            "Assistant 이메일 초안(orthus.agentwork.service 계열 프롬프트, json_only). "
            "채점=결정론(JSON 파싱 실패율 · `_invented` 로 지시/참고자료에 없는 인명·고유명사·"
            "수치 삽입 · 제목 'Re:' 금지 위반 · 본문 길이 · p50 지연). LLM judge 없음."
        ),
        "note": (
            f"현행 DB에 실메일 본문이 0건이라 **템플릿 합성만** 했다(owner 결정). "
            f"축 = 수신자 유형({len(EMAIL_RECIPIENTS)}종, 9개 클래스) × 요청 의도"
            f"({len(EMAIL_INTENTS)}종) × 참고자료 유무. 의도마다 어울리는 수신자 클래스를 지정해 "
            "'지원자에게 서버 증설 요청' 같은 무의미한 짝을 배제했다. 기존 인라인 30문항(e01~e30)은 "
            f"내용/id 그대로 앞에 보존하고 나머지를 합성했다. 참고자료 보유 {n_with}/{N_TARGET}건 "
            f"(약 {n_with * 100 // N_TARGET}%) — `_invented` 가 변별력을 가지려면 지어낼 여지가 "
            "있어야 하므로, 날짜/금액/수량이 지시에 전혀 없는 문항(대괄호로 비우는지 관찰)과 구체적 "
            "고유명사·수치가 주어진 문항(주어진 것 밖을 지어내는지 관찰)을 섞었다. "
            "grounding = `to + inst + ctx` 라 수신자 이름은 환각으로 집계되지 않는다. 정답 라벨 없음."
        ),
        "items": items,
    }


# ───────────────────────────── 검수 ─────────────────────────────


def audit(name: str, doc: dict, text_key: str) -> list[str]:
    problems = []
    items = doc["items"]
    if len(items) != N_TARGET:
        problems.append(f"{name}: n={len(items)} (expected {N_TARGET})")
    ids = [i["id"] for i in items]
    if len(set(ids)) != len(ids):
        problems.append(f"{name}: duplicate ids")
    keys = []
    for i in items:
        keys.append(_norm(i[text_key]) if text_key != "*email" else
                    f"{_norm(i['to'])}|{_norm(i['inst'])}|{_norm(i['ctx'])}")
    dup = len(keys) - len(set(keys))
    if dup:
        problems.append(f"{name}: {dup} duplicate texts")
    for i in items:
        for k, v in i.items():
            if isinstance(v, str) and ("�" in v or "\x00" in v):
                problems.append(f"{name}: {i['id']} broken chars")
    if name == "gap_suggest":
        bad = {i["reason"] for i in items} - set(GAP_REASONS)
        if bad:
            problems.append(f"{name}: non-enum reasons {bad}")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="headline,gap,email")
    ap.add_argument("--check", action="store_true", help="파일을 쓰지 않고 생성+검수만")
    a = ap.parse_args()
    tasks = {t.strip() for t in a.task.split(",") if t.strip()}
    GOLDEN.mkdir(parents=True, exist_ok=True)

    plan = []
    if "headline" in tasks:
        plan.append(("claim_headline", gen_claim_headline(), "claim"))
    if "gap" in tasks:
        plan.append(("gap_suggest", gen_gap_suggest(_read_real_gaps()), "q"))
    if "email" in tasks:
        plan.append(("email_draft", gen_email_draft(), "*email"))

    problems: list[str] = []
    for name, doc, key in plan:
        problems += audit(name, doc, key)
        out = GOLDEN / f"t12_{name}.json"
        if not a.check:
            out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{name:16} n={len(doc['items']):5}  -> {out.relative_to(REPO)}")

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print("  " + p)
        sys.exit(1)
    print("\naudit OK")


if __name__ == "__main__":
    main()
