#!/usr/bin/env python3
"""D10 L2 flow-bench proportional extension builder (84 -> 168 scorable items).

Appends the +84 blind-authored D10 flow items (analysis/d10-flow-extension-prereg.md
§1: g1 +3, g2 +42, g3 +20, g4 +19; id band `B-g{n}-1001+`, tag `d10_ext`) to
`e2e/l2/g*.jsonl`, then re-freezes `e2e/freeze.lock`.

Stability contract (manifest_schema.md §2/§12, same discipline as
`build_manifest.py`'s frozen-line replay for D9):

- every EXISTING l2 line is replayed byte-identically (id-keyed; an id already in
  the file keeps its stored line verbatim — the builder asserts its own recomputed
  `input_sha256` matches before replaying, so a drifted input fails loudly);
- only genuinely new ids are minted, with `frozen.input_sha256` computed via the
  §12 canonicalization (`runner_lib.canonical_input_sha256` equivalent) and
  `frozen_at` = the current git build tag;
- `input.fixture.sha256` is the sha256 of the referenced fixture file's bytes at
  build time (matching how the existing l2 lines document their fixtures);
- `freeze.lock` is regenerated through the established `build_tier_b.build_freeze_lock`
  path, reading the on-disk `tier_a.jsonl`/`tier_b.jsonl` (both untouched) plus the
  extended l2 files. NOTE: the lock's tier_b/l2 sections were already stale before
  D10 (l2 user-fill landed after the last lock build — recorded in
  analysis/d9-authoring-note.md §6), so this refreeze also absorbs that
  pre-existing drift; the old and new lock stats are printed for the note.

Run (repo root, project venv):

    .venv/bin/python3 experiments/fugu-ko/e2e/build_l2_d10.py
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # experiments/fugu-ko/e2e
FUGU = HERE.parent
L2_DIR = HERE / "l2"
FIX_DIR = HERE / "fixtures"
LOCK = HERE / "freeze.lock"

for p in (str(FUGU), str(HERE), str(FUGU / "embedding")):
    if p not in sys.path:
        sys.path.insert(0, p)

D10_TAG = "d10_ext"
PROV_AGENT = "provenance:agent_authored_2026-07-24"
REQUIRED_KEYS = {
    "id", "layer", "task", "entry_point", "input", "expected", "scoring",
    "tier", "provenance", "tags", "frozen",
}


def _git_build_tag() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=FUGU, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        if out:
            return f"build:{out}"
    except Exception:  # noqa: BLE001
        pass
    return "build:0000000"


FROZEN_AT = _git_build_tag()


def input_sha256(input_obj: dict) -> str:
    blob = json.dumps(input_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fx(rel: str) -> dict:
    """Fixture ref with the live file-bytes sha256 (e.g. rel='g2/g2-session-empty.json')."""
    path = FIX_DIR / rel
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"id": path.stem, "path": f"e2e/fixtures/{rel}", "sha256": digest}


def rec(
    item_id: str,
    task: str,
    entry_point: str,
    request: dict,
    fixture_rel: str | None,
    assert_spec: dict,
    *,
    provenance: str = "new_blind",
    tags: list[str],
    invariants: list[str] | None = None,
) -> dict:
    input_obj: dict = {"request": request}
    if fixture_rel is not None:
        input_obj["fixture"] = fx(fixture_rel)
    out = {
        "id": item_id,
        "layer": "L2",
        "task": task,
        "entry_point": entry_point,
        "input": input_obj,
        "expected": {"kind": "structural", "assert": assert_spec},
        "scoring": "deterministic",
        "tier": "B",
        "provenance": provenance,
        "tags": [*tags, D10_TAG],
        "frozen": {"input_sha256": input_sha256(input_obj), "frozen_at": FROZEN_AT},
    }
    if invariants:
        out["invariants"] = invariants
    return out


# --------------------------------------------------------------------------- #
# g2 — session -> wiki/routing (POST /agent-work/chats/{session_id}/orchestrate)
# --------------------------------------------------------------------------- #
_G2_EP = "POST /agent-work/chats/{session_id}/orchestrate"
_G2_SESS_EMPTY = "c2a10001-0000-4000-8000-000000000001"       # g2-session-empty
_G2_SESS_DECOMP = "c2a10002-0000-4000-8000-000000000001"      # g2-session-empty-decompose-on
_G2_SESS_WIKI = "c2a10003-0000-4000-8000-000000000001"        # g2-session-with-wiki


def _g2_req(session_id: str, text: str) -> dict:
    return {
        "method": "POST",
        "path": f"/agent-work/chats/{session_id}/orchestrate",
        "body": {"text": text},
        "query": {},
    }


_EMAIL_STRONG_ASSERT = {
    "mode": "agent_work",
    "agent_work.action_family": "email_send",
    "agent_work.state": "draft_for_review",
    "agent_work.reason_codes_contains": [
        "email_draft_first",
        "policy_memory_observation_gate_required",
    ],
}
_EMAIL_WEAK_ASSERT = {
    "mode": "agent_work",
    "agent_work.action_family": "email_send",
    "agent_work.state": "request_more_data",
    "agent_work.reason_codes_contains": ["recipient_required"],
}


def _g2_cmd(idx: int, text: str, assert_spec: dict, tags: list[str]) -> dict:
    return rec(
        f"B-g2-{idx:04d}", "g2", _G2_EP, _g2_req(_G2_SESS_EMPTY, text),
        "g2/g2-session-empty.json", assert_spec, tags=["g2", "g2-d", *tags],
    )


def _g2_pf(idx: int, text: str, mode: str, tags: list[str]) -> dict:
    return rec(
        f"B-g2-{idx:04d}", "g2", _G2_EP, _g2_req(_G2_SESS_DECOMP, text),
        "g2/g2-session-empty-decompose-on.json", {"mode": mode},
        tags=["g2", "g2-c", "prefilter_false", "decompose_short_circuit",
              "no_llm_should_decompose", *tags],
    )


def _g2_amb(idx: int, text: str, mode: str, tags: list[str]) -> dict:
    return rec(
        f"B-g2-{idx:04d}", "g2", _G2_EP, _g2_req(_G2_SESS_WIKI, text),
        "g2/g2-session-with-wiki.json", {"mode": mode},
        provenance="independent_holdout",
        tags=["g2", "g2-r", "ambiguous_route", "human_labeled_route_required",
              "input-source:synthetic-blind-agent-authored", PROV_AGENT, *tags],
        invariants=["model_fallback_zero", "no_confident_zero"],
    )


def build_g2() -> list[dict]:
    items: list[dict] = []
    # -- prebuilt-style deterministic command intake (keyword detector path) --
    items.append(_g2_cmd(1001, "choi@orbit.kr에게 계약서 검토 요청 메일 보내줘",
                         _EMAIL_STRONG_ASSERT, ["email_send", "recipient_literal", "keyword_command"]))
    items.append(_g2_cmd(1002, "minsu.park@nova.example로 온보딩 안내 메일 전송해줘",
                         _EMAIL_STRONG_ASSERT, ["email_send", "recipient_literal", "keyword_command"]))
    items.append(_g2_cmd(1003, "신규 입사자 환영 메일 초안 작성해줘",
                         _EMAIL_WEAK_ASSERT, ["email_send", "no_recipient", "keyword_command"]))
    items.append(_g2_cmd(1004, "협력사에 보낼 답장 초안 써줘",
                         _EMAIL_WEAK_ASSERT, ["email_send", "no_recipient", "keyword_command"]))
    items.append(_g2_cmd(1005, "정리된 공지 내용 메일로 보내줘",
                         _EMAIL_WEAK_ASSERT, ["email_send", "no_recipient", "head_final_command", "keyword_command"]))
    for idx, text in ((1006, "노션 워크스페이스 동기화해줘"),
                      (1007, "슬랙 메시지 수집해줘"),
                      (1008, "깃허브 이슈 가져와줘")):
        items.append(_g2_cmd(idx, text, {
            "mode": "agent_work",
            "agent_work.action_family": "connector_sync",
            "agent_work.state": "request_more_data",
            "agent_work.reason_codes_contains": ["connector_missing_config_or_auth"],
        }, ["connector_sync", "unconfigured_account", "keyword_command"]))
    for idx, text in ((1009, "분기 결산 보고서 초안 작성해줘"),
                      (1010, "지난 회의록 문서로 정리해줘")):
        items.append(_g2_cmd(idx, text, {
            "mode": "agent_work",
            "agent_work.action_family": "document_draft",
            "agent_work.state": "draft_for_review",
            "agent_work.reason_codes_contains": ["editor_review_required"],
        }, ["document_draft", "keyword_command"]))
    for idx, text in ((1011, "완료된 할일 정리해줘"),
                      (1012, "칸반 카드 정돈해줘")):
        items.append(_g2_cmd(idx, text, {
            "mode": "agent_work",
            "agent_work.action_family": "personal_board_cleanup",
            "agent_work.state": "draft_for_review",
            "agent_work.reason_codes_contains": ["board_review_required"],
        }, ["personal_board_cleanup", "company_node", "keyword_command"]))
    for idx, text in ((1013, "묵은 위키 항목 반영해줘"),
                      (1014, "위키 반영 대기 건 처리해줘")):
        items.append(_g2_cmd(idx, text, {
            "mode": "agent_work",
            "agent_work.action_family": "central_wiki_task_cleanup",
            "agent_work.state": "draft_for_review",
            "agent_work.reason_codes_contains": ["company_wiki_review_required"],
        }, ["central_wiki_task_cleanup", "keyword_command"]))
    items.append(_g2_cmd(1015, "협업 문서 요약해서 jang@acme.example로 메일 초안 보내줘",
                         _EMAIL_STRONG_ASSERT,
                         ["email_send", "compound_summarize_email", "keyword_command"]))
    # -- prebuilt-style deterministic negatives (read-verb / word guards) --
    items.append(_g2_cmd(1016, "메일 답장 내용 설명해줘", {"mode": "wiki"},
                         ["email_send_negative", "read_verb_guard", "keyword_command_negative"]))
    items.append(_g2_cmd(1017, "보낸 메일함 요약해줘", {"mode": "wiki"},
                         ["read_only_summarize", "keyword_command_negative"]))
    items.append(_g2_cmd(1018, "할일 새로 만들어줘", {"mode": "wiki"},
                         ["board_create_word_guard", "keyword_command_negative"]))
    items.append(_g2_cmd(1019, "회사 위키에 쌓인 내용 요약해줘", {"mode": "wiki"},
                         ["read_only_summarize", "keyword_command_negative"]))
    # -- prebuilt-style deterministic rule routes --
    for idx, text in ((1020, "출연자 연락처 목록 보여줘"),
                      (1021, "팀원 이메일 주소 알려줘"),
                      (1022, "미해결 티켓 건수 알려줘")):
        items.append(_g2_cmd(idx, text, {"mode": "structured"}, ["structured_rule_route"]))
    for idx, text in ((1023, "출장비 규정이 뭐야"),
                      (1024, "장비 대여 절차 설명해줘"),
                      (1025, "지난 분기 회고 내용 요약해줘")):
        items.append(_g2_cmd(idx, text, {"mode": "wiki"}, ["wiki_rule_route"]))
    for idx, text in ((1026, "미팅 기록이 얼마나 쌓였어"),
                      (1027, "협력 업체가 총 몇 곳이야")):
        items.append(_g2_cmd(idx, text, {"mode": "structured"}, ["quantity_route"]))
    for idx, text in ((1028, "결제 모듈과 알림 기능은 무슨 관계야"),
                      (1029, "이 지침 근거가 어디서 나온 거야")):
        items.append(_g2_cmd(idx, text, {"mode": "wiki"}, ["graph_demote", "kg_off"]))
    # -- prebuilt-style prefilter-false decompose short-circuits --
    items.append(_g2_pf(1030, "법인카드 사용 한도가 뭐야", "wiki", []))
    items.append(_g2_pf(1031, "사내 장비 신청은 어떻게 접수해", "wiki", []))
    items.append(_g2_pf(1032, "올해 채용 예정 인원 몇 명이야", "structured", []))
    items.append(_g2_pf(1033, "사내 보안 교육은 왜 필수야", "wiki", []))
    # -- ambiguous aggregate-vs-knowledge routing (human-labeled, discriminating) --
    for idx, text in ((1034, "배우 오디션 기록 결과 상관없이 전부 세어 줘"),
                      (1035, "협업업무표 전체 행 수 좀 뽑아줘"),
                      (1036, "회사 미팅 기록을 파트너사 기준으로 집계해줘"),
                      (1037, "AI관련툴에 등록된 항목 수 궁금해"),
                      (1038, "보도자료 배포처에서 신문 매체가 몇 곳인지 봐줘")):
        items.append(_g2_amb(idx, text, "structured", ["aggregate_signal"]))
    for idx, text in ((1039, "노바 로드맵 3분기에 뭐가 나가는지 궁금해"),
                      (1040, "연차가 근속연수 따라 늘어나는 게 맞는지 궁금해"),
                      (1041, "배송이 보통 며칠 걸리는지 궁금해"),
                      (1042, "환불 신청하면 처리까지 얼마나 걸려")):
        items.append(_g2_amb(idx, text, "wiki", ["knowledge_signal"]))
    return items


# --------------------------------------------------------------------------- #
# g3 — mail -> reply/delegation (POST /mail/ingest, POST /mail/send)
# --------------------------------------------------------------------------- #
def _mail_body(
    *, backend: str = "nova", owner: str = "owner01@nova.example", ext: str,
    mid: str, direction: str = "inbound", from_addr: str = "client@partner.com",
    to_addr: str | None = None, subject: str, body_text: str,
    sent_at: str = "2026-07-10T09:00:00Z",
) -> dict:
    return {
        "backend": backend,
        "owner_addr": owner,
        "external_id": ext,
        "message_id": mid,
        "direction": direction,
        "from_addr": from_addr,
        "to_addr": [to_addr or owner],
        "cc_addr": [],
        "subject": subject,
        "body_text": body_text,
        "body_html": "",
        "sent_at": sent_at,
    }


def _g3_ingest(idx: int, body: dict, fixture_rel: str | None, assert_spec: dict,
               tags: list[str], *, provenance: str = "new_blind",
               invariants: list[str] | None = None) -> dict:
    return rec(
        f"B-g3-{idx:04d}", "g3", "POST /mail/ingest",
        {"method": "POST", "path": "/mail/ingest", "body": body, "query": {}},
        fixture_rel, assert_spec, provenance=provenance,
        tags=["g3", *tags], invariants=invariants,
    )


def _g3_send(idx: int, body: dict, fixture_rel: str, assert_spec: dict,
             tags: list[str]) -> dict:
    return rec(
        f"B-g3-{idx:04d}", "g3", "POST /mail/send",
        {"method": "POST", "path": "/mail/send", "body": body, "query": {}},
        fixture_rel, assert_spec, tags=["g3", "g3-s", "manual_send", *tags],
    )


_DELEGATION_ASSERT_POS = {
    "status": 200,
    "ingested": True,
    "delegation_work_id_present": True,
    "delegation_state": "draft_for_review",
    "delegation_reason_codes_contains": ["agent_task_llm_inferred", "human_review_required"],
}
_DELEGATION_ASSERT_NEG = {
    "status": 200,
    "ingested": True,
    "delegation_work_id_present": False,
}


def build_g3() -> list[dict]:
    items: list[dict] = []
    # -- deterministic ingest state machine --
    items.append(_g3_ingest(1001, _mail_body(
        ext="d10-g3-i1-ext", mid="<d10-g3-i1@nova.example>", from_addr="client2@partner.com",
        subject="납품 일정 조율 요청", body_text="다음 주 납품 일정 조율 가능하신지 회신 부탁드립니다."),
        "g3/g3-owner-nova.json",
        {"status": 200, "ingested": True, "scope": "personal", "work_id_present": True},
        ["g3-i", "mail_reply_candidate", "individual_mailbox"]))
    items.append(_g3_ingest(1002, _mail_body(
        ext="d10-g3-i2-ext", mid="<d10-g3-i2@nova.example>",
        subject="세금계산서 발행 문의", body_text="지난달 세금계산서 발행 부탁드립니다."),
        "g3/g3-owner-nova-noreply.json",
        {"status": 200, "ingested": True, "work_id_present": False},
        ["g3-i", "reply_draft_flag_off"]))
    items.append(_g3_ingest(1003, _mail_body(
        ext="d10-g3-i3-ext", mid="<d10-g3-i3@nova.example>", direction="outbound",
        from_addr="owner01@nova.example", to_addr="client@partner.com",
        subject="미팅 일정 안내", body_text="다음 미팅은 수요일 오전 10시입니다."),
        "g3/g3-owner-nova.json",
        {"status": 200, "ingested": True, "work_id_present": False},
        ["g3-i", "outbound_direction"]))
    items.append(_g3_ingest(1004, _mail_body(
        ext="d10-g3-i4-ext", mid="<d10-g3-i4@nova.example>",
        subject="중복 수신 테스트 D10", body_text="같은 메일이 두 번 들어온 상황."),
        "g3/g3-owner-nova-dup-d10.json",
        {"status": 200, "ingested": False, "scope": "personal",
         "doc_id": "7c8b7a6f-5e4d-4c3b-9a1f-0e1d2c3b4d10"},
        ["g3-i", "idempotent_ingest", "duplicate_message_id"]))
    items.append(_g3_ingest(1005, _mail_body(
        backend="gmail", owner="user2@gmail.com", ext="d10-g3-i5-ext",
        mid="<d10-g3-i5@gmail.com>", subject="Gmail D10",
        body_text="gmail backend push ingest is still unsupported."),
        "g3/g3-ingest-enabled.json", {"status": 422}, ["g3-i", "gmail_rejected"]))
    items.append(_g3_ingest(1006, _mail_body(
        owner="ghost@nova.example", ext="d10-g3-i6-ext", mid="<d10-g3-i6@nova.example>",
        subject="미등록 메일함", body_text="이 주소는 어떤 auth_identity에도 매핑되지 않는다."),
        "g3/g3-ingest-enabled.json", {"status": 422},
        ["g3-i", "unresolvable_owner", "individual_mailbox"]))
    items.append(_g3_ingest(1007, _mail_body(
        ext="d10-g3-i7-ext", mid="<d10-g3-i7@nova.example>",
        subject="인증 프로브 D10", body_text="bad bearer probe (D10)."),
        "g3/g3-ingest-enabled.json", {"status": 401}, ["g3-i", "auth_probe:bad_bearer"]))
    items.append(_g3_ingest(1008, _mail_body(
        ext="d10-g3-i8-ext", mid="<d10-g3-i8@nova.example>",
        subject="인증 프로브 D10-2", body_text="bad hmac probe (D10)."),
        "g3/g3-ingest-enabled.json", {"status": 401}, ["g3-i", "auth_probe:bad_hmac"]))
    items.append(_g3_ingest(1009, _mail_body(
        ext="d10-g3-i9-ext", mid="<d10-g3-i9@nova.example>",
        subject="기본 비활성 테스트 D10", body_text="mail_ingest_enabled=false (default, D10)."),
        None, {"status": 404}, ["g3-i", "ingest_disabled_default"]))
    # -- deterministic send gates --
    items.append(_g3_send(1010, {
        "from_addr": "lead@acme.example", "to": "dest+d10s1@example.com",
        "subject": "D10 manual", "text": "Hello from d10."},
        "g3/g3-send-kill-off.json", {"status": 404}, ["kill_switch_off"]))
    items.append(_g3_send(1011, {
        "from_addr": "lead@acme.example", "to": "dest+d10s2@example.com",
        "subject": "D10 manual", "text": "Hello from d10."},
        "g3/g3-send-personal-node.json", {"status": 404}, ["personal_node"]))
    items.append(_g3_send(1012, {
        "from_addr": "lead@acme.example", "to": "dest+d10s3@example.com",
        "subject": "D10 manual", "text": "Hello from d10."},
        "g3/g3-send-demo-role.json", {"status": 403, "log_count": 0}, ["demo_role_rejected"]))
    items.append(_g3_send(1013, {
        "from_addr": "lead@acme.example", "to": "dest+d10s4@example.com",
        "subject": "D10 manual", "text": "Hello from d10."},
        "g3/g3-send-unconfigured.json", {"status": 422, "log_count": 0}, ["unconfigured_backend"]))
    items.append(_g3_send(1014, {
        "from_addr": "lead@acme.example", "to": "same@example.com",
        "subject": "D10 rate limit", "text": "Second send to the same recipient within the hour (D10)."},
        "g3/g3-send-rate-limited.json", {"status": 429, "log_count": 1},
        ["rate_limited", "idempotent_state_preseed"]))
    # -- delegation extraction (model-discriminating: LLM classify/extract input only) --
    items.append(_g3_ingest(1015, _mail_body(
        ext="d10-g3-x1-ext", mid="<d10-g3-x1@nova.example>", from_addr="boss@partner.com",
        subject="배포 파이프라인 캐시 정합성", body_text=(
            "owner01@acme.example 님, 배포 파이프라인 캐시가 계속 어긋나는데 "
            "정합성 점검이랑 수정 부탁드립니다.")),
        "g3/g3-delegation-owner-nova.json", _DELEGATION_ASSERT_POS,
        ["g3-x", "delegation_positive", PROV_AGENT],
        invariants=["model_fallback_zero"]))
    items.append(_g3_ingest(1016, _mail_body(
        ext="d10-g3-x2-ext", mid="<d10-g3-x2@nova.example>", from_addr="boss@partner.com",
        subject="온보딩 문서 링크 정리 요청", body_text=(
            "김철수(cs.kim@acme.example)님, 온보딩 문서에 깨진 링크가 많다고 합니다. "
            "이번 주 안에 링크 정리 부탁드려요.")),
        "g3/g3-delegation-owner-nova.json", _DELEGATION_ASSERT_POS,
        ["g3-x", "delegation_positive", PROV_AGENT],
        invariants=["model_fallback_zero"]))
    items.append(_g3_ingest(1017, _mail_body(
        ext="d10-g3-x3-ext", mid="<d10-g3-x3@nova.example>", from_addr="boss@partner.com",
        subject="결제 웹훅 재시도 간격 수정", body_text=(
            "오세훈(sh.oh@acme.example)님, 결제 웹훅 재시도 간격 설정을 검토해서 "
            "수정해 주세요.")),
        "g3/g3-delegation-owner-nova.json", _DELEGATION_ASSERT_POS,
        ["g3-x", "delegation_positive", PROV_AGENT],
        invariants=["model_fallback_zero"]))
    items.append(_g3_ingest(1018, _mail_body(
        ext="d10-g3-x4-ext", mid="<d10-g3-x4@nova.example>", from_addr="boss@partner.com",
        subject="웹훅 재시도 간격 검토 진행", body_text=(
            "웹훅 재시도 간격은 제가 직접 검토하겠습니다. 다른 분들은 참고만 해주세요.")),
        "g3/g3-delegation-owner-nova.json", _DELEGATION_ASSERT_NEG,
        ["g3-x", "delegation_fp_probe", "negative_control", "adversarial",
         "self_assignment_trap", PROV_AGENT],
        invariants=["model_fallback_zero"]))
    items.append(_g3_ingest(1019, _mail_body(
        ext="d10-g3-x5-ext", mid="<d10-g3-x5@nova.example>", from_addr="boss@partner.com",
        subject="결제 웹훅 담당 관련 회의 내용 공유", body_text=(
            "어제 회의에서 대표님이 '결제 웹훅은 오세훈님 담당'이라고 정리하셨다는 "
            "내용 공유드립니다.")),
        "g3/g3-delegation-owner-nova.json", _DELEGATION_ASSERT_NEG,
        ["g3-x", "delegation_fp_probe", "negative_control", "adversarial",
         "third_party_quotation_trap", PROV_AGENT],
        invariants=["model_fallback_zero"]))
    items.append(_g3_ingest(1020, _mail_body(
        ext="d10-g3-x6-ext", mid="<d10-g3-x6@nova.example>", from_addr="boss@partner.com",
        subject="온보딩 문서 링크 점검 완료", body_text=(
            "온보딩 문서 링크 점검 끝났습니다. 깨진 링크 12개 모두 수정 완료했고 "
            "별도 조치는 필요 없습니다.")),
        "g3/g3-delegation-owner-nova.json", _DELEGATION_ASSERT_NEG,
        ["g3-x", "delegation_fp_probe", "negative_control", "benign_fyi", PROV_AGENT],
        invariants=["model_fallback_zero"]))
    return items


# --------------------------------------------------------------------------- #
# g4 — delegation gate (POST /agent-work/delegate)
# --------------------------------------------------------------------------- #
_G4_EP = "POST /agent-work/delegate"


def _g4(idx: int, body: dict, fixture_rel: str, assert_spec: dict, tags: list[str],
        *, entry_point: str = _G4_EP, method: str = "POST",
        path: str = "/agent-work/delegate") -> dict:
    return rec(
        f"B-g4-{idx:04d}", "g4", entry_point,
        {"method": method, "path": path, "body": body, "query": {}},
        fixture_rel, assert_spec, tags=["g4", *tags],
    )


def build_g4() -> list[dict]:
    items: list[dict] = []
    items.append(_g4(1001, {
        "instruction": "지난 스프린트 회고 문서 정리해서 위키 반영 후보로 올려줘",
        "assignee": "dev@example.com", "runner": "claude", "mode": "code", "cwd": "/repo"},
        "g4/g4-owner-enrolled-other.json", {
            "status": 200, "state": "auto_execute", "action_family": "agent_task",
            "policy_outcome": "auto_execute",
            "reason_codes_contains": ["agent_task_dispatch_ready"],
            "collector_commands_count": 1,
            "payload.auto_execution.status": "dispatched"},
        ["owner_actor", "other_assignee", "happy_path"]))
    items.append(_g4(1002, {
        "instruction": "온보딩 체크리스트 최신 상태인지 검토해줘",
        "assignee": "dev@example.com", "runner": "hermes", "mode": "knowledge"},
        "g4/g4-owner-enrolled-other.json", {
            "status": 200, "state": "auto_execute", "policy_outcome": "auto_execute",
            "collector_commands_count": 1,
            "payload.auto_execution.status": "dispatched",
            "payload.runner": "hermes"},
        ["owner_actor", "hermes_runner", "happy_path"]))
    items.append(_g4(1003, {
        "instruction": "내 다운로드 폴더 오래된 파일 정리해줘",
        "runner": "codex", "mode": "knowledge"},
        "g4/g4-owner-self.json", {
            "status": 200, "state": "auto_execute", "action_family": "agent_task",
            "policy_outcome": "auto_execute", "collector_commands_count": 1,
            "payload.auto_execution.status": "dispatched"},
        ["self_assignee", "blank_to_self"]))
    items.append(_g4(1004, {
        "instruction": "내 주간 리포트 초안 잡아줘",
        "runner": "codex", "mode": "knowledge"},
        "g4/g4-owner-self.json", {
            "status": 200, "state": "auto_execute", "collector_commands_count": 1,
            "payload.delegation_board_task": None},
        ["self_delegation_no_board_task", "owner_scope_enabled"]))
    items.append(_g4(1005, {
        "instruction": "레거시 스크립트 목록 정리해줘",
        "assignee": "nodaemon@example.com", "runner": "codex", "mode": "knowledge"},
        "g4/g4-no-daemon.json", {
            "status": 200, "state": "request_more_data", "action_family": "agent_task",
            "policy_outcome": "request_more_data",
            "reason_codes_contains": ["agent_task_dispatch_incomplete"],
            "collector_commands_count": 0},
        ["no_enrolled_daemon"]))
    items.append(_g4(1006, {
        "instruction": "배포 노트 정리해줘",
        "assignee": "ghost-d10@example.com", "runner": "codex", "mode": "knowledge"},
        "g4/g4-owner-only.json", {
            "status": 200, "state": "request_more_data",
            "policy_outcome": "request_more_data",
            "reason_codes_contains": ["agent_task_dispatch_incomplete"],
            "collector_commands_count": 0},
        ["unresolvable_assignee"]))
    items.append(_g4(1007, {
        "instruction": "다음 릴리스 체크리스트 정리해줘",
        "assignee": "flagoff-dev@example.com", "runner": "claude", "mode": "code"},
        "g4/g4-flag-off.json", {
            "status": 200, "state": "rejected", "policy_outcome": "reject",
            "reason_codes_contains": ["agent_task_disabled_or_wrong_node"],
            "collector_commands_count": 0},
        ["flag_off", "design_deviation_reject_is_200_not_422"]))
    items.append(_g4(1008, {
        "instruction": "개인 노드에서 위임 시도해줘",
        "assignee": "personal-dev@example.com", "runner": "claude", "mode": "code"},
        "g4/g4-personal-node.json", {
            "status": 200, "state": "rejected", "policy_outcome": "reject",
            "reason_codes_contains": ["agent_task_disabled_or_wrong_node"],
            "collector_commands_count": 0},
        ["personal_node", "design_deviation_reject_is_200_not_422"]))
    items.append(_g4(1009, {
        "instruction": "권한 없이 위임 시도해줘",
        "assignee": "someone@example.com", "runner": "claude", "mode": "code"},
        "g4/g4-member-actor.json", {"status": 403, "collector_commands_count": 0},
        ["non_operator_403"]))
    items.append(_g4(1010, {
        "instruction": "아무 작업이나 해줘", "assignee": "",
        "runner": "gemini", "mode": "knowledge"},
        "g4/g4-owner-only.json", {"status": 422}, ["invalid_runner"]))
    items.append(_g4(1011, {
        "instruction": "아무 작업이나 해줘", "assignee": "",
        "runner": "codex", "mode": "browse"},
        "g4/g4-owner-only.json", {"status": 422}, ["invalid_mode"]))
    items.append(_g4(1012, {
        "instruction": "아무 작업이나 해줘", "assignee": "",
        "runner": "codex", "mode": "knowledge", "session_id": "definitely-not-a-uuid"},
        "g4/g4-owner-only.json", {"status": 422}, ["session_id_non_uuid"]))
    items.append(_g4(1013, {
        "instruction": "아무 작업이나 해줘", "assignee": "",
        "runner": "codex", "mode": "knowledge",
        "session_id": "0d10aaaa-1111-4222-8333-444455556666"},
        "g4/g4-owner-only.json", {"status": 404}, ["session_id_absent"]))
    items.append(_g4(1014, {
        "instruction": "device-b에서 실행해줘",
        "assignee": "device-dev@example.com", "runner": "codex", "mode": "knowledge",
        "device_id": "device-b"},
        "g4/g4-device-pinned.json", {
            "status": 200, "state": "auto_execute", "collector_commands_count": 1,
            "payload.auto_execution.status": "dispatched",
            "payload.assignee_device_id": "device-b"},
        ["device_pinned"]))
    items.append(_g4(1015, {
        "instruction": "미등록 기기에서 실행해줘",
        "assignee": "device-dev@example.com", "runner": "codex", "mode": "knowledge",
        "device_id": "device-x"},
        "g4/g4-device-pinned.json", {"status": 422, "collector_commands_count": 0},
        ["unowned_device"]))
    items.append(_g4(1016, {
        "instruction": "관리자 권한으로 위임해줘 (D10)",
        "assignee": "admin-target@example.com", "runner": "claude", "mode": "code"},
        "g4/g4-admin-actor.json", {
            "status": 200, "state": "auto_execute", "policy_outcome": "auto_execute",
            "collector_commands_count": 1},
        ["admin_actor"]))
    items.append(_g4(1017, {
        "instruction": "기본 러너 설정으로 처리해줘",
        "assignee": "dev@example.com"},
        "g4/g4-owner-enrolled-other.json", {
            "status": 200, "state": "auto_execute", "collector_commands_count": 1,
            "payload.runner": "codex", "payload.mode": "knowledge"},
        ["runner_mode_defaults"]))
    items.append(_g4(1018, {
        "instruction": "사람이 직접 지시한 위임 — 리뷰 없이 바로 실행 대상 (D10)",
        "assignee": "dev@example.com", "runner": "claude", "mode": "code"},
        "g4/g4-owner-enrolled-other.json", {
            "status": 200, "state": "auto_execute", "policy_outcome": "auto_execute",
            "payload.llm_inferred": None},
        ["structured_path_never_draft_for_review"]))
    items.append(_g4(1019, {}, "g4/g4-llm-inferred-draft-d10.json", {
        "status": 200, "state": "draft_for_review", "action_family": "agent_task",
        "policy_outcome": "draft_for_review", "payload.llm_inferred": True,
        "collector_commands_count": 0},
        ["llm_inferred_draft_no_auto_execute", "mail_origin_preseed"],
        entry_point="GET /agent-work/{work_id}", method="GET",
        path="/agent-work/d10f00d1-2a3b-4c4d-8e5f-a6b7c8d9e0f1"))
    return items


# --------------------------------------------------------------------------- #
# g1 — document draft -> publish (POST /documents/{doc_id}/publish)
# --------------------------------------------------------------------------- #
_G1_EP = "POST /documents/{doc_id}/publish"


def build_g1() -> list[dict]:
    items: list[dict] = []
    items.append(rec("B-g1-1001", "g1", _G1_EP, {
        "method": "POST",
        "path": "/documents/1d10aaa1-2222-4111-8111-1111111111d1/publish",
        "body": {"title": "존재하지 않는 문서 D10", "block_json": [{}],
                 "markdown": "존재하지 않음 (D10)."},
        "query": {}},
        None, {"status": 404}, tags=["g1", "g1-structural", "g1-missing-doc-404"]))
    items.append(rec("B-g1-1002", "g1", _G1_EP, {
        "method": "POST",
        "path": "/documents/11111111-2222-4111-8111-111111111111/publish",
        "body": {"title": "이미 게시된 문서 (D10 재시도)", "block_json": [{}],
                 "markdown": "editor 소스 문서에 대한 D10 publish 재시도."},
        "query": {}},
        "g1/g1-draft-not-draft.json", {"status": 409},
        tags=["g1", "g1-structural", "g1-not-draft-409"]))
    items.append(rec("B-g1-1003", "g1", _G1_EP, {
        "method": "POST",
        "path": "/documents/aaaad100-1111-4111-8111-111111111201/publish",
        "body": {"title": "보안 점검 절차", "block_json": [{}],
                 "markdown": ("보안 점검은 분기마다 1회 실시하며, 점검 결과는 3영업일 안에 "
                              "보고하고, 미조치 항목은 재점검 대상에 자동 포함된다.")},
        "query": {}},
        "g1/g1-draft-triclaim-d10.json", {"status": 200, "resulting_claim_count": 3},
        tags=["g1", "g1-structural", "g1-scripted-triclaim"]))
    return items


# --------------------------------------------------------------------------- #
# assembly: frozen-line replay + append-only extend + freeze.lock refreeze
# --------------------------------------------------------------------------- #
def extend_file(task: str, new_items: list[dict]) -> tuple[int, int]:
    path = L2_DIR / f"{task}.jsonl"
    existing_lines = [ln for ln in path.read_text("utf-8").splitlines() if ln.strip()]
    existing: dict[str, str] = {json.loads(ln)["id"]: ln for ln in existing_lines}
    out_lines = list(existing_lines)  # replay every existing line byte-identically
    minted = replayed = 0
    for item in new_items:
        old = existing.get(item["id"])
        if old is not None:
            old_rec = json.loads(old)
            assert old_rec["frozen"]["input_sha256"] == input_sha256(item["input"]), (
                f"{item['id']}: input drifted vs frozen line — retire the id instead "
                "of mutating it (manifest_schema.md §2)"
            )
            replayed += 1
            continue
        out_lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        minted += 1
    path.write_text("\n".join(out_lines) + "\n", "utf-8")
    print(f"[build_l2_d10] {task}.jsonl: {len(existing_lines)} existing lines replayed "
          f"byte-identically, {minted} newly minted, {replayed} d10 ids already frozen")
    return len(existing_lines), minted


def validate(all_new: list[dict]) -> None:
    ids: set[str] = set()
    for r in all_new:
        assert set(r.keys()) - {"invariants"} == REQUIRED_KEYS, (
            f"{r['id']}: key set mismatch {set(r.keys())}"
        )
        assert r["id"] not in ids, f"duplicate id {r['id']}"
        ids.add(r["id"])
        assert r["expected"]["kind"] == "structural" and r["scoring"] == "deterministic"
        assert D10_TAG in r["tags"], f"{r['id']}: missing {D10_TAG}"
        assert r["frozen"]["input_sha256"] == input_sha256(r["input"])
        json.loads(json.dumps(r, ensure_ascii=False))
    # global id uniqueness across every l2 file
    seen: set[str] = set()
    for p in sorted(L2_DIR.glob("g*.jsonl")):
        for ln in p.read_text("utf-8").splitlines():
            if not ln.strip():
                continue
            rid = json.loads(ln)["id"]
            assert rid not in seen, f"duplicate id across l2 files: {rid}"
            seen.add(rid)
    print(f"[build_l2_d10] validation OK: {len(all_new)} new records, "
          f"{len(seen)} total l2 ids unique")


def refreeze_lock() -> None:
    import build_tier_b as btb

    old = json.loads(LOCK.read_text("utf-8"))
    lock = btb.build_freeze_lock(btb._read_jsonl(btb.OUT))
    lock["frozen_at_build"] = (
        f"{FROZEN_AT} (D10 l2 +84 flow ext; tier_a.jsonl/tier_b.jsonl untouched — "
        "tier hashes recomputed from disk, absorbing the pre-D10 l2 user-fill drift "
        "recorded in analysis/d9-authoring-note.md §6; prior: "
        f"{old['frozen_at_build']})"
    )
    LOCK.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", "utf-8")
    for tier in ("tier_a", "tier_b"):
        print(f"[freeze.lock] {tier}: count {old[tier]['count']} -> {lock[tier]['count']}, "
              f"pending {old[tier]['pending_excluded']} -> {lock[tier]['pending_excluded']}, "
              f"sha {old[tier]['manifest_sha256'][:12]} -> {lock[tier]['manifest_sha256'][:12]}")


def main() -> None:
    builders = {"g1": build_g1(), "g2": build_g2(), "g3": build_g3(), "g4": build_g4()}
    counts = {t: len(items) for t, items in builders.items()}
    assert counts == {"g1": 3, "g2": 42, "g3": 20, "g4": 19}, counts
    all_new = [i for items in builders.values() for i in items]
    validate(all_new)
    for task, items in builders.items():
        extend_file(task, items)
    refreeze_lock()
    print(f"[build_l2_d10] done — +{len(all_new)} items, frozen_at {FROZEN_AT}")


if __name__ == "__main__":
    main()
