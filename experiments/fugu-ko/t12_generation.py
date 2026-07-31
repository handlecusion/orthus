"""생성 3종 측정 — email_draft · gap_suggest · claim_headline.

이 셋은 남은 `get_chat_model()` 호출부 중 distill을 뺀 전부다. 셋 다 **형식이 깨지면
결정론 템플릿으로 조용히 떨어지는** 경로다. 그래서 "누가 글을 더 잘 쓰나"(주관)보다 아래가
실질 지표다:

- **형식 실패율** — email_draft/gap_suggest는 `json_only=True`다. JSON이 깨지면 그 요청은
  LLM을 부르고도 결정론 폴백으로 떨어진다. 모델을 바꾼 의미가 0이 되는 실패다.
- **환각** — 지시/참고자료에 없는 고유명사(사람·회사·날짜·금액)를 본문에 심는가. 메일 초안의
  환각은 **사람이 그대로 보내면 외부로 나간다.**
- **지시 준수** — email 제목의 `Re:` 금지, headline 길이 상한 같은 명시 제약.
- 지연.

프로덕션 프롬프트/파서를 그대로 임포트한다(t9/t10과 같은 방식). 코드는 건드리지 않는다.

    python t12_generation.py                    # 3작업 × 4모델
    python t12_generation.py --score-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

RAW = HERE / "analysis" / "raw"
GOLDEN = HERE / "golden"
MODELS = ["solar", "ax", "exaone", "baseline"]


def _golden(name: str, fallback: list[dict]) -> list[dict]:
    """`golden/t12_<name>.json` 이 있으면 그것이 골든이다(n=1,000 확장분).

    없으면 아래 인라인 리터럴로 떨어진다 — 확장 전 실행분(raw jsonl)을 `--score-only`로
    다시 채점할 때 골든 파일이 없어도 동작해야 하기 때문이다. 골든은 **입력 문항만** 담는다
    (이 3종은 judge가 없고 채점이 결정론이라 정답 라벨이 필요 없다).
    생성기는 `e2e/gen_golden_t12.py`.
    """
    p = GOLDEN / f"t12_{name}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))["items"]
    return fallback


# ── 골든: 실제 회사 맥락에서 나올 법한 요청들. 지시에 없는 사실을 채워 넣으면 환각이다. ──
_EMAIL_FALLBACK = [
    {"id": "e01", "to": "파트너사 담당자", "inst": "다음 주 미팅 일정을 제안하는 메일", "ctx": ""},
    {"id": "e02", "to": "김민수 팀장", "inst": "제안 주신 견적은 예산을 초과해 이번엔 어렵다고 정중히 거절", "ctx": ""},
    {"id": "e03", "to": "지원자", "inst": "서류 전형 합격 안내와 면접 일정 조율 요청", "ctx": ""},
    {"id": "e04", "to": "고객사", "inst": "이 내용을 기반으로 진행 상황 공유 메일 작성", "ctx": "Sprint 6에서 자막 패널 렌더링 버그를 수정했고, 다크 테마는 설정 화면까지 적용됐다. 영상 생성 파이프라인 개선은 다음 스프린트로 이월됐다."},
    {"id": "e05", "to": "협력사", "inst": "청구서 발행 지연에 대해 사과하고 예상 처리 일정을 알림", "ctx": ""},
    {"id": "e06", "to": "owner01@nova.example", "inst": "이 결과를 정리해서 공유해줘", "ctx": "라이브아바타 일반 카테고리 리소스 최소화 작업으로 초기 로딩이 40% 줄었다."},
    {"id": "e07", "to": "외부 검토자", "inst": "보안 점검 결과 회신 요청", "ctx": ""},
    {"id": "e08", "to": "채용 담당", "inst": "채용 공고 정렬 순서 변경 배경을 설명하는 메일", "ctx": ""},
    {"id": "e09", "to": "팀 전체", "inst": "서버 인증서 갱신 작업 공지", "ctx": ""},
    {"id": "e10", "to": "거래처", "inst": "비용 영수증 재발행을 요청", "ctx": ""},
    {"id": "e11", "to": "PM", "inst": "이 내용대로 일정 재조정을 요청하는 메일", "ctx": "출국 QR 오류가 재현돼 릴리스를 3일 미뤄야 한다."},
    {"id": "e12", "to": "디자인 파트너", "inst": "레퍼런스 추출 절차를 안내", "ctx": ""},
    # ── 확장분(e13~e30). 12문항으로는 solar 3 vs exaone 1 이 p=0.63 이라 판단이 안 섰다.
    # 절반 이상을 **날짜/금액/수량이 지시에 없는** 요청으로 채운다 — 지어낼 기회를 주고 그때
    # 대괄호로 비우는지 보는 게 이 측정의 목적이기 때문이다.
    {"id": "e13", "to": "전 직원", "inst": "정기 보안 교육 일정 공지", "ctx": ""},
    {"id": "e14", "to": "협력사", "inst": "계약 갱신 의사를 확인하는 메일", "ctx": ""},
    {"id": "e15", "to": "이수진 매니저", "inst": "지난번 요청하신 자료를 첨부해 보낸다고 안내", "ctx": ""},
    {"id": "e16", "to": "팀 전체", "inst": "이번 배포 롤백 사실과 후속 조치 공지", "ctx": "출국 QR 오류로 릴리스를 롤백했다. 원인 분석 중이다."},
    {"id": "e17", "to": "고객사 담당자", "inst": "서비스 점검으로 인한 일시 중단 안내", "ctx": ""},
    {"id": "e18", "to": "지원자", "inst": "최종 불합격 통보를 정중하게", "ctx": ""},
    {"id": "e19", "to": "외주 개발사", "inst": "산출물 검수 결과와 수정 요청 전달", "ctx": "자막 패널 렌더링에서 이미지 중복 오류가 남아 있다."},
    {"id": "e20", "to": "회계 담당", "inst": "출장비 정산 서류를 제출한다고 알림", "ctx": ""},
    {"id": "e21", "to": "파트너사", "inst": "공동 마케팅 제안에 대한 내부 검토 결과 회신", "ctx": ""},
    {"id": "e22", "to": "박지훈 대리", "inst": "이 내용을 정리해서 회의 전에 공유해달라고 요청", "ctx": "Sprint 6 완료율은 82%이고, 오디오 P0 항목 2건이 미완이다."},
    {"id": "e23", "to": "신규 입사자", "inst": "온보딩 첫 주 일정 안내", "ctx": ""},
    {"id": "e24", "to": "고객", "inst": "환불 요청을 접수했다고 확인 메일", "ctx": ""},
    {"id": "e25", "to": "법무 검토자", "inst": "계약서 조항 해석에 대한 자문 요청", "ctx": ""},
    {"id": "e26", "to": "인프라 담당", "inst": "서버 증설이 필요하다고 요청", "ctx": "라이브아바타 트래픽이 늘어 초기 로딩이 느려졌다."},
    {"id": "e27", "to": "전 직원", "inst": "사무실 이전 일정과 준비 사항 공지", "ctx": ""},
    {"id": "e28", "to": "투자사", "inst": "분기 진행 상황 업데이트", "ctx": ""},
    {"id": "e29", "to": "번역 파트너", "inst": "자연스러운 톤 표현 가이드를 전달하며 재작업 요청", "ctx": ""},
    {"id": "e30", "to": "김하늘 팀장", "inst": "이 결과를 근거로 예산 증액을 건의", "ctx": "리소스 최소화 작업으로 초기 로딩이 40% 줄었으나, 추가 최적화에는 GPU가 더 필요하다."},
]

EMAIL_ITEMS = _golden("email_draft", _EMAIL_FALLBACK)

# ⚠️ 아래 인라인 reason 값(`no_hits`/`low_confidence`)은 **프로덕션 enum에 없다**
# (orthus.schemas.canonical.GapReason = no_data | weak_retrieval | insufficient_grounding |
# missing_link). 골든 파일 `golden/t12_gap_suggest.json` 에서 실제 enum으로 교정했고,
# 여기 리터럴은 옛 raw 재채점 호환용으로만 남는다.
_GAP_FALLBACK = [
    {"id": "g01", "q": "우리 회사 휴가 규정이 어떻게 돼?", "reason": "no_hits"},
    {"id": "g02", "q": "2분기 매출이 얼마야?", "reason": "no_hits"},
    {"id": "g03", "q": "장비 구매 승인은 누가 해?", "reason": "no_hits"},
    {"id": "g04", "q": "온보딩 첫 주에 뭘 해야 해?", "reason": "low_confidence"},
    {"id": "g05", "q": "출장비 정산 기한이 언제야?", "reason": "no_hits"},
    {"id": "g06", "q": "배포 롤백 절차가 문서로 있어?", "reason": "low_confidence"},
    {"id": "g07", "q": "고객 데이터 보관 기간은?", "reason": "no_hits"},
    {"id": "g08", "q": "인턴 평가 기준이 뭐야?", "reason": "no_hits"},
    {"id": "g09", "q": "사내 보안 교육은 얼마나 자주 받아?", "reason": "no_hits"},
    {"id": "g10", "q": "프로젝트 예산 초과 시 보고 라인은?", "reason": "low_confidence"},
    {"id": "g11", "q": "협력사 계약 갱신 주기가 어떻게 돼?", "reason": "no_hits"},
    {"id": "g12", "q": "재택근무 신청은 어디에 해?", "reason": "no_hits"},
]
GAP_ITEMS = _golden("gap_suggest", _GAP_FALLBACK)

# claim → headline. 골든 파일이 없을 때만 wiki-store를 직접 스캔한다(아래 _claims()).
N_CLAIMS = 20


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "").casefold())


# 지어내면 실제로 위험한 것들만 잡는다. 메일 초안의 환각은 사람이 그대로 보내면 **외부로 나간다** —
# 없는 사람 이름, 없는 날짜/금액을 담은 채로.
#   (1) 한글 인명+직함 — "김철수 대표", "박영희 팀장"  ← 가장 위험. 순수 정규식으론 이 형태만 잡힌다.
#   (2) 영문 대문자 시작 고유명사 — 회사/제품/시스템명
#   (3) 숫자+단위 — 날짜·금액·기간·비율
_TITLES = "대표|팀장|부장|과장|차장|이사|대리|매니저|담당자|님|씨"
_UNITS = "만원|천원|원|억|%|명|일|시|분|초|개월|주|월|년|건|차|회"
# 이름은 별도 그룹으로 잡는다 — "담당자님"처럼 지시에 이미 있는 말에 존칭만 붙은 건 환각이 아니므로
# **직함을 뗀 이름만** 근거와 대조해야 한다. (`\b`는 한글 뒤에서 경계로 동작하지 않아 쓰지 않는다.)
_PROPER = re.compile(
    rf"(?P<name>[가-힣]{{2,4}})\s*(?:{_TITLES})"
    rf"|(?P<en>[A-Z][A-Za-z0-9]{{2,}})"
    rf"|(?P<num>\d+(?:[,.]\d+)*\s*(?:{_UNITS}))"
)


# "담당자님", "관계자분" 같은 **일반 호칭**은 인명이 아니다. 이름 자리에 이런 낱말이 오면 무죄다 —
# 안 걸러 내면 정상적인 존칭 사용이 전부 환각으로 집계된다(초기 실행에서 7건 중 3건이 이 오탐이었다).
_GENERIC_ROLE = {
    "담당자", "관계자", "책임자", "실무자", "고객", "귀하", "여러분", "팀원", "직원",
    "협력사", "거래처", "파트너", "지원자", "발신자", "수신자", "고객사", "대표자",
}


def _invented(text: str, grounding: str, *, latin: bool = True) -> list[str]:
    """본문에 있으나 지시/참고자료에는 없는 인명·고유명사·수치. 대괄호 빈칸은 제외(프롬프트가 허용).

    `latin=False` 는 **영문 Title Case 출력**(claim_headline)에 쓴다. 거기서는 "Strict",
    "Policy", "Overview" 같은 **평범한 단어가 전부 대문자로 시작**해서 "대문자=고유명사" 가정이
    통째로 깨진다(초기 실행에서 헤드라인 오탐 8/8이 전부 이것이었다). 그때는 숫자·날짜·금액만 본다 —
    거기서 지어내는 건 Title Case와 무관하게 진짜 환각이다.
    """
    body = re.sub(r"\[[^\]]*\]", " ", text or "")  # [빈칸] 은 지어내지 않고 정직하게 비운 표시다
    g = _norm(grounding)
    out = []
    for m in _PROPER.finditer(body):
        if not latin and m.group("en"):
            continue
        probe = m.group("name") or m.group("en") or m.group("num")
        if not probe or probe in _GENERIC_ROLE:
            continue
        if _norm(probe) not in g:
            out.append(probe.strip())
    return sorted(set(out))


CLAIMS_DIR = Path(
    os.environ.get("FUGU_WIKI_STORE", str(Path.home() / ".orthus/nodes/company/wiki-store/company"))
) / "claims"


def _claims() -> list[dict]:
    """클레임의 SoR은 markdown이다(DB에 wiki_claims 테이블은 없다). `## Claim` 섹션 본문을 읽는다.

    골든 파일이 있으면 그것을 쓴다 — 이 함수의 `sorted(glob)[:20]` 은 슬러그 알파벳 앞쪽에
    쏠려 있어서(숫자로 시작하는 진척도/날짜 클레임) 코퍼스 대표성이 없다.
    """
    p = GOLDEN / "t12_claim_headline.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))["items"]
    out: list[dict] = []
    for f in sorted(CLAIMS_DIR.glob("*.md")):
        m = re.search(r"^## Claim\s*\n+(.+?)(?=\n##|\Z)", f.read_text(encoding="utf-8"), re.S | re.M)
        if not m:
            continue
        text = " ".join(m.group(1).split())
        if 40 <= len(text) <= 300:  # 너무 짧은 건 헤드라인이 곧 클레임이라 변별력이 없다
            out.append({"id": f"h{len(out) + 1:02}", "claim": text})
        if len(out) >= N_CLAIMS:
            break
    return out


def run(models: list[str], tasks: set[str], limit: int | None = None) -> None:
    from pool import build_pool

    from orthus.agentwork.service import _generate_command_email  # noqa: F401 — 프롬프트 확인용
    from orthus.wiki.distill import HEADLINE_SYSTEM

    # 프로덕션 프롬프트를 그대로 재현(함수가 chat 주입을 안 받는 호출부라 프롬프트를 임포트해 쓴다).
    from orthus.wiki.gap import _SUGGEST_SYSTEM, _parse_suggestion

    EMAIL_SYSTEM = (
        "당신은 회사 직원을 대신해 새 이메일 초안을 작성하는 비서다. "
        "사용자의 요청에 근거해 정중하고 전문적인 한국어 이메일을 작성한다. "
        "참고 자료가 주어지면 그 내용을 근거로 본문을 채운다. "
        "확실하지 않은 사실은 지어내지 않고, 필요한 빈칸은 대괄호 표시로 남긴다. "
        '오직 JSON만 출력한다: {"subject": "<제목>", "body": "<본문>"}'
    )

    pool = build_pool(models)
    # `--limit` 은 골든 **앞에서부터** 자른다. 세 골든 모두 해시 정렬돼 있어 앞 N건도
    # 전체와 같은 분포를 갖는다(부분 실행이 특정 reason/수신자에 쏠리지 않는다).
    n = limit or len(EMAIL_ITEMS)
    emails, gaps, claims = EMAIL_ITEMS[:n], GAP_ITEMS[:n], _claims()[:n]
    RAW.mkdir(parents=True, exist_ok=True)

    for slug in models:
        chat = pool[slug]
        # 태스크별 파일 — 이메일만 재실행해도 gap/headline 결과가 날아가지 않는다.
        fhs = {k: (RAW / f"t12_{slug}_{k}.jsonl").open("w", encoding="utf-8") for k in tasks}
        if True:

            def emit(rec: dict) -> None:
                fh = fhs[rec["task"]]
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()

            # ── email_draft ──
            for it in emails if "email" in tasks else []:
                ctx = f"\n[참고 자료 — 요청의 '이 내용/이걸' 등이 가리키는 배경]\n{it['ctx']}\n" if it["ctx"] else ""
                user = (
                    f"받는 사람: {it['to']}\n요청: {it['inst']}\n{ctx}\n"
                    "위 요청에 맞는 이메일 제목과 본문을 작성하라. 참고 자료가 있으면 그 내용을 근거로 "
                    "본문을 구체적으로 채운다. 제목에 'Re:'를 붙이지 마라."
                )
                t0 = time.monotonic()
                rec: dict = {"task": "email", "id": it["id"]}
                try:
                    raw = (chat.complete(EMAIL_SYSTEM, user, json_only=True) or "").strip()
                    p = json.loads(raw)
                    subj = " ".join(str(p.get("subject") or "").split())
                    body = str(p.get("body") or "").strip()
                    grounding = f"{it['to']} {it['inst']} {it['ctx']}"
                    rec |= {
                        "ok": bool(subj and body),
                        "subject": subj,
                        "body": body,
                        "re_prefix": subj.lower().startswith("re:"),
                        "invented": _invented(f"{subj}\n{body}", grounding),
                    }
                except Exception as e:  # noqa: BLE001 — 프로덕션은 여기서 결정론 폴백으로 떨어진다
                    rec |= {"ok": False, "error": f"{type(e).__name__}"}
                rec["ms"] = int((time.monotonic() - t0) * 1000)
                emit(rec)

            # ── gap_suggest ──
            for it in gaps if "gap" in tasks else []:
                t0 = time.monotonic()
                rec = {"task": "gap", "id": it["id"]}
                try:
                    raw = chat.complete(
                        _SUGGEST_SYSTEM,
                        f"Question: {it['q']}\nReason it failed: {it['reason']}",
                        json_only=True,
                    )
                    target, connector, sections = _parse_suggestion(raw)
                    rec |= {
                        "ok": bool(target and sections),
                        "target": target,
                        "connector": connector,
                        "n_sections": len(sections or []),
                        "n_items": sum(len(s.items) for s in (sections or [])),
                    }
                except Exception as e:  # noqa: BLE001
                    rec |= {"ok": False, "error": f"{type(e).__name__}"}
                rec["ms"] = int((time.monotonic() - t0) * 1000)
                emit(rec)

            # ── claim_headline ──
            for it in claims if "headline" in tasks else []:
                claim = it["claim"]
                t0 = time.monotonic()
                rec = {"task": "headline", "id": it["id"], "claim": claim}
                try:
                    raw = (chat.complete(HEADLINE_SYSTEM, claim) or "").strip()
                    head = " ".join(raw.split())
                    rec |= {
                        "ok": bool(head),
                        "headline": head,
                        "chars": len(head),
                        "invented": _invented(head, claim, latin=False),
                    }
                except Exception as e:  # noqa: BLE001
                    rec |= {"ok": False, "error": f"{type(e).__name__}"}
                rec["ms"] = int((time.monotonic() - t0) * 1000)
                emit(rec)

            for fh in fhs.values():
                fh.close()
            print(f"  {slug:9} done", flush=True)


def _p50(xs: list[int]) -> int:
    return sorted(xs)[len(xs) // 2] if xs else 0


def score(models: list[str]) -> None:
    print("\n  ══ 생성 3종 (형식이 깨지면 프로덕션은 결정론 폴백 — LLM을 부른 의미가 0이 된다) ══")
    for task, label in (("email", "email_draft"), ("gap", "gap_suggest"), ("headline", "claim_headline")):
        print(f"\n  ── {label} ──")
        for m in models:
            p = RAW / f"t12_{m}_{task}.jsonl"
            if not p.exists():
                p = RAW / f"t12_{m}.jsonl"  # 태스크 분리 이전 실행분 호환
            if not p.exists():
                continue
            rs = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
            rs = [r for r in rs if r["task"] == task]
            if not rs:
                continue
            n = len(rs)
            bad = sum(1 for r in rs if not r.get("ok"))
            ms = _p50([r["ms"] for r in rs])
            if task == "email":
                # 저장된 `invented` 를 읽지 않고 **채점 시점에 다시 계산한다.** 탐지기는 이 실험에서
                # 두 번 틀렸고(일반 호칭 오탐, Title Case 오탐), 그때마다 재실행하면 API 비용이
                # 든다. 원본 출력만 있으면 탐지기 수정은 공짜여야 한다.
                by_id = {i["id"]: i for i in EMAIL_ITEMS}
                inv = 0
                for r in rs:
                    if not r.get("ok"):
                        continue
                    it = by_id[r["id"]]
                    g = f"{it['to']} {it['inst']} {it['ctx']}"
                    if _invented(f"{r['subject']}\n{r['body']}", g):
                        inv += 1
                re_ = sum(1 for r in rs if r.get("re_prefix"))
                blen = [len(r.get("body") or "") for r in rs if r.get("ok")]
                avg = sum(blen) / len(blen) if blen else 0
                print(
                    f"    {m:9} 형식실패 {bad}/{n} | 없는 고유명사·수치 심음 {inv}/{n} "
                    f"| 'Re:' 위반 {re_} | 본문 {avg:4.0f}자 | p50 {ms}ms"
                )
            elif task == "gap":
                sec = [r.get("n_sections", 0) for r in rs if r.get("ok")]
                itm = [r.get("n_items", 0) for r in rs if r.get("ok")]
                over = sum(1 for s in sec if s < 2 or s > 4)  # 프롬프트: 2-4 섹션
                print(
                    f"    {m:9} 형식실패 {bad}/{n} | 섹션수 규격(2-4) 위반 {over} "
                    f"| 평균 섹션 {sum(sec) / len(sec) if sec else 0:.1f} · 항목 {sum(itm) / len(itm) if itm else 0:.1f} | p50 {ms}ms"
                )
            else:
                ch = [r.get("chars", 0) for r in rs if r.get("ok")]
                # 프롬프트가 명시한 상한(_HEADLINE_MAX=120). 넘으면 프로덕션 `_one_line_cap`이
                # 말줄임표로 자른다 — 잘린 헤드라인이 그대로 그래프 노드 라벨이 된다.
                long_ = sum(1 for c in ch if c > 120)
                inv = sum(
                    1
                    for r in rs
                    if r.get("ok") and _invented(r["headline"], r["claim"], latin=False)
                )
                print(
                    f"    {m:9} 실패 {bad}/{n} | 평균 {sum(ch) / len(ch) if ch else 0:5.1f}자 "
                    f"| 120자 초과(잘림) {long_} | 없는 표현 {inv} | p50 {ms}ms"
                )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--score-only", action="store_true")
    ap.add_argument("--tasks", default="email,gap,headline")
    ap.add_argument("--limit", type=int, default=None, help="골든 앞에서부터 N건만 실행")
    a = ap.parse_args()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    tasks = {x.strip() for x in a.tasks.split(",") if x.strip()}
    if not a.score_only:
        run(models, tasks, a.limit)
    score(models)


if __name__ == "__main__":
    main()
