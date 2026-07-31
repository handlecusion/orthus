"""Unit tests for the deterministic chat (/ask) delegation parser.

`parse_chat_delegation` is a pure string parser — it never calls the LLM, the DB,
or any external service. These tests assert the explicit-prefix matching, the
optional `@email` teammate marker, code/knowledge mode inference, runner
selection, and the None fall-through for non-delegation text.
"""

from __future__ import annotations

from orthus.agentwork.delegation import parse_chat_delegation


# --- explicit lead matching ---------------------------------------------------


def test_colon_lead_extracts_instruction():
    parsed = parse_chat_delegation("위임: 리포 테스트 고쳐줘")
    assert parsed == {
        "assignee": None,
        "mode": "knowledge",
        "runner": "codex",
        "instruction": "리포 테스트 고쳐줘",
        "cwd": None,
    }


def test_space_lead_extracts_instruction():
    parsed = parse_chat_delegation("위임 휴가 정책 요약해줘")
    assert parsed is not None
    assert parsed["instruction"] == "휴가 정책 요약해줘"
    assert parsed["assignee"] is None


def test_slash_lead_extracts_instruction():
    parsed = parse_chat_delegation("/위임 회의록 정리해줘")
    assert parsed is not None
    assert parsed["instruction"] == "회의록 정리해줘"


def test_naega_agent_lead_extracts_instruction():
    parsed = parse_chat_delegation("내 에이전트한테 리포 분석시켜줘")
    assert parsed is not None
    assert parsed["instruction"] == "리포 분석시켜줘"


def test_agent_eke_lead_extracts_instruction():
    parsed = parse_chat_delegation("에이전트에게 문서 정리시켜줘")
    assert parsed is not None
    assert parsed["instruction"] == "문서 정리시켜줘"


def test_case_insensitive_slash_lead():
    # The "/위임 " lead is case-insensitive for any latin parts of the lead.
    parsed = parse_chat_delegation("/위임 update the docs")
    assert parsed is not None
    assert parsed["instruction"] == "update the docs"


# --- @email teammate marker ---------------------------------------------------


def test_email_marker_sets_assignee():
    parsed = parse_chat_delegation("@dev@example.com 위임: 코드 고쳐줘")
    assert parsed is not None
    assert parsed["assignee"] == "dev@example.com"
    assert parsed["instruction"] == "코드 고쳐줘"


def test_email_marker_with_agent_lead():
    parsed = parse_chat_delegation("@dev@example.com 에이전트한테 분석시켜줘")
    assert parsed is not None
    assert parsed["assignee"] == "dev@example.com"
    assert parsed["instruction"] == "분석시켜줘"


def test_email_marker_without_lead_is_none():
    # An @email marker but no delegation lead is not a delegation.
    assert parse_chat_delegation("@dev@example.com 휴가 정책 뭐야") is None


# --- cwd marker ---------------------------------------------------------------


def test_cwd_marker_extracted_and_stripped_from_instruction():
    parsed = parse_chat_delegation("위임: cwd=<repo> 이 버그 수정해줘")
    assert parsed is not None
    assert parsed["cwd"] == "<repo>"
    assert parsed["instruction"] == "이 버그 수정해줘"
    # the path token must not flip mode; the remaining instruction decides it
    assert parsed["mode"] == "code"


def test_cwd_default_none_when_absent():
    parsed = parse_chat_delegation("위임: 리포 분석해줘")
    assert parsed is not None
    assert parsed["cwd"] is None


def test_cwd_quoted_path_with_spaces():
    parsed = parse_chat_delegation('위임: cwd="<local> Repo" 코드 정리해줘')
    assert parsed is not None
    assert parsed["cwd"] == "<local> Repo"
    assert parsed["instruction"] == "코드 정리해줘"


def test_cwd_tilde_path_kept_verbatim_for_daemon_expansion():
    # "~" expansion happens daemon-side (_resolve_cwd), not in the parser.
    parsed = parse_chat_delegation("위임: cwd=~/proj 파일 정리해줘")
    assert parsed is not None
    assert parsed["cwd"] == "~/proj"
    assert parsed["instruction"] == "파일 정리해줘"


def test_cwd_with_email_marker_and_agent_lead():
    parsed = parse_chat_delegation("@dev@example.com 위임: cwd=/srv/app 코드 고쳐줘")
    assert parsed is not None
    assert parsed["assignee"] == "dev@example.com"
    assert parsed["cwd"] == "/srv/app"
    assert parsed["instruction"] == "코드 고쳐줘"


def test_cwd_path_word_does_not_flip_mode():
    # A path containing a code-mode word (파일) must not force code mode when the
    # actual instruction is knowledge-only.
    parsed = parse_chat_delegation("위임: cwd=<local> 시장 동향 조사해줘")
    assert parsed is not None
    assert parsed["cwd"] == "<local>"
    assert parsed["mode"] == "knowledge"


def test_cwd_only_without_instruction_is_none():
    assert parse_chat_delegation("위임: cwd=<repo>") is None


# --- mode inference -----------------------------------------------------------


def test_code_mode_when_instruction_has_code_word():
    for word in ("코드", "파일", "수정", "구현"):
        parsed = parse_chat_delegation(f"위임: {word} 작업 해줘")
        assert parsed is not None
        assert parsed["mode"] == "code", word


def test_knowledge_mode_default():
    parsed = parse_chat_delegation("위임: 시장 동향 조사해줘")
    assert parsed is not None
    assert parsed["mode"] == "knowledge"


# --- runner selection ---------------------------------------------------------


def test_runner_defaults_to_codex():
    parsed = parse_chat_delegation("위임: 리포 분석해줘")
    assert parsed is not None
    assert parsed["runner"] == "codex"


def test_explicit_runner_is_used():
    for runner in ("claude", "codex", "hermes"):
        parsed = parse_chat_delegation(f"위임: {runner}로 코드 고쳐줘")
        assert parsed is not None
        assert parsed["runner"] == runner, runner


# --- non-delegation fall-through ----------------------------------------------


def test_plain_question_is_none():
    assert parse_chat_delegation("휴가 정책이 뭐야?") is None
    assert parse_chat_delegation("오늘 일정 알려줘") is None


def test_command_without_lead_is_none():
    # A keyword command (handled by other families) is not a chat delegation.
    assert parse_chat_delegation("gmail 동기화 해줘") is None
    assert parse_chat_delegation("john에게 이메일 초안 만들어줘") is None


def test_bare_word_without_lead_is_none():
    # "위임" alone (no colon, no trailing space lead) is not a delegation.
    assert parse_chat_delegation("위임") is None


def test_empty_instruction_is_none():
    assert parse_chat_delegation("위임:") is None
    assert parse_chat_delegation("위임: ") is None
    assert parse_chat_delegation("에이전트한테 ") is None


def test_empty_and_blank_input_is_none():
    assert parse_chat_delegation("") is None
    assert parse_chat_delegation("   ") is None
    assert parse_chat_delegation(None) is None  # type: ignore[arg-type]


def test_leading_whitespace_is_stripped():
    parsed = parse_chat_delegation("   위임: 리포 고쳐줘  ")
    assert parsed is not None
    assert parsed["instruction"] == "리포 고쳐줘"
