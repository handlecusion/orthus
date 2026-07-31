from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from orthus import cli


class FakeCentralClient:
    calls: list[tuple] = []

    def __init__(self, config=None) -> None:
        self.config = config
        FakeCentralClient.calls.append(("init", config))

    def wiki_search(self, query: str, *, scope: str, limit: int) -> dict:
        FakeCentralClient.calls.append(("wiki_search", query, scope, limit))
        return {"items": [{"slug": "company/mail/p6", "title": "P6"}]}

    def wiki_page(self, slug: str) -> dict:
        FakeCentralClient.calls.append(("wiki_page", slug))
        return {"slug": slug, "title": "Page"}

    def wiki_ask(self, question: str, *, scope: str, context_wiki_slug: str | None) -> dict:
        FakeCentralClient.calls.append(("wiki_ask", question, scope, context_wiki_slug))
        return {"answer": "grounded"}

    def wiki_update_candidate(self, slug: str, note: str, evidence_urls: list[str]) -> dict:
        FakeCentralClient.calls.append(("wiki_update_candidate", slug, note, evidence_urls))
        return {"created": True, "slug": slug}

    def agent_work_list(self, *, state: str | None, limit: int) -> list:
        FakeCentralClient.calls.append(("agent_work_list", state, limit))
        return [{"id": "aw_1", "state": state}]

    def agent_work_get(self, work_id: str) -> dict:
        FakeCentralClient.calls.append(("agent_work_get", work_id))
        return {"id": work_id}

    def whoami(self) -> dict:
        FakeCentralClient.calls.append(("whoami",))
        return {"user_id": "u1", "email": "a@b.c", "role": "admin", "node_id": "company"}

    def connector_list(self) -> dict:
        FakeCentralClient.calls.append(("connector_list",))
        return {
            "node_kind": "company",
            "node_id": "company",
            "manifests": [
                {
                    "slug": "github",
                    "config_fields": [
                        {"key": "repos", "kind": "text"},
                        {"key": "max_items", "kind": "number"},
                        {"key": "token", "kind": "secret"},
                    ],
                }
            ],
            "accounts": [
                {"connector_slug": "github", "account_id": "acct-github", "status": "active"}
            ],
            "runs": [],
        }

    def connector_config(self, slug, *, settings, secrets, label):
        FakeCentralClient.calls.append(("connector_config", slug, settings, secrets, label))
        return {"connector_slug": slug, "settings_redacted": settings}

    def connector_ensure(self, slug):
        FakeCentralClient.calls.append(("connector_ensure", slug))
        return {"connector_slug": slug}

    def connector_delete(self, slug, account_id):
        FakeCentralClient.calls.append(("connector_delete", slug, account_id))
        return {"account_id": account_id, "deleted": True}

    def team_schedule(self, since, until):
        FakeCentralClient.calls.append(("team_schedule", since, until))
        return [{"event_id": "ev_1"}]

    def team_schedule_add(self, payload):
        FakeCentralClient.calls.append(("team_schedule_add", payload))
        return {"event_id": "ev_new", **payload}

    def team_schedule_update(self, event_id, payload):
        FakeCentralClient.calls.append(("team_schedule_update", event_id, payload))
        return {"event_id": event_id, **payload}

    def team_schedule_delete(self, event_id):
        FakeCentralClient.calls.append(("team_schedule_delete", event_id))
        return {"deleted": event_id}

    def team_members(self):
        FakeCentralClient.calls.append(("team_members",))
        return [{"member_id": "m_1", "name": "민수"}]

    def team_members_add(self, payload):
        FakeCentralClient.calls.append(("team_members_add", payload))
        return {"member_id": "m_new", **payload}

    def personal_schedule_list(self, since, until):
        FakeCentralClient.calls.append(("personal_schedule_list", since, until))
        return [{"event_id": "pe_1"}]

    def personal_schedule_add(self, payload):
        FakeCentralClient.calls.append(("personal_schedule_add", payload))
        return {"event_id": "pe_new", **payload}

    def personal_schedule_update(self, event_id, payload):
        FakeCentralClient.calls.append(("personal_schedule_update", event_id, payload))
        return {"event_id": event_id, **payload}


def install_fake_central(monkeypatch) -> None:
    FakeCentralClient.calls = []
    monkeypatch.setattr(cli, "CentralClient", FakeCentralClient)
    monkeypatch.setattr(cli, "resolve_token", lambda: "dct_secret_token")
    monkeypatch.setattr(cli, "resolve_collector_token", lambda: "dct_collector_token")
    # Hermetic central-url resolution: without this, `--central-url` 미지정 커맨드가
    # env/`~/.orthus/config.json` 순서로 폴백해 개발 머신 상태에 따라 결과가 갈린다.
    monkeypatch.setenv(cli.CENTRAL_URL_ENV, "https://central.test/api")


def test_parser_registers_p9_1_commands():
    parser = cli.build_parser()
    args = parser.parse_args(["version"])
    assert args.func is cli.cmd_version
    args = parser.parse_args(["init", "--central-url", "https://central.test/api"])
    assert args.func is cli.cmd_init
    args = parser.parse_args(["wiki", "ask", "hello"])
    assert args.func is cli.cmd_wiki_ask
    args = parser.parse_args(["work", "show", "aw_1"])
    assert args.func is cli.cmd_work_show
    args = parser.parse_args(["mcp", "config", "codex"])
    assert args.func is cli.cmd_mcp_config


def test_parser_registers_calendar_and_myschedule_commands():
    parser = cli.build_parser()
    args = parser.parse_args(["calendar", "add", "--title", "회의", "--date", "2026-07-01"])
    assert args.func is cli.cmd_calendar_add
    args = parser.parse_args(["calendar", "update", "ev_1", "--location", "A동"])
    assert args.func is cli.cmd_calendar_update
    args = parser.parse_args(["calendar", "delete", "ev_1"])
    assert args.func is cli.cmd_calendar_delete
    args = parser.parse_args(["calendar", "list"])
    assert args.func is cli.cmd_calendar_list
    args = parser.parse_args(["calendar", "members", "add", "--name", "민수"])
    assert args.func is cli.cmd_calendar_members_add
    args = parser.parse_args(
        ["myschedule", "add", "--title", "치과", "--start", "2026-07-01T14:00:00+09:00",
         "--end", "2026-07-01T15:00:00+09:00"]
    )
    assert args.func is cli.cmd_myschedule_add
    args = parser.parse_args(["myschedule", "update", "pe_1", "--title", "치과 예약"])
    assert args.func is cli.cmd_myschedule_update


def test_calendar_add_builds_payload_and_calls_central(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    code = cli.main(
        [
            "--json", "--central-url", "https://central.test/api",
            "calendar", "add", "--title", "주간회의", "--date", "2026-07-01",
            "--no-all-day", "--start-time", "10:00", "--end-time", "11:00",
            "--location", "회의실A", "--member", "m_1", "--member", "m_2",
        ]
    )
    assert code == 0
    name, payload = FakeCentralClient.calls[-1][0], FakeCentralClient.calls[-1][1]
    assert name == "team_schedule_add"
    assert payload["title"] == "주간회의"
    assert payload["event_date"] == "2026-07-01"
    assert payload["all_day"] is False
    assert payload["start_time"] == "10:00"
    assert payload["member_ids"] == ["m_1", "m_2"]
    out = json.loads(capsys.readouterr().out)
    assert out["event_id"] == "ev_new"


def test_calendar_update_only_sends_provided_fields(monkeypatch):
    install_fake_central(monkeypatch)
    code = cli.main(["calendar", "update", "ev_9", "--location", "B동"])
    assert code == 0
    assert FakeCentralClient.calls[-1] == ("team_schedule_update", "ev_9", {"location": "B동"})


def test_myschedule_add_builds_iso_payload(monkeypatch):
    install_fake_central(monkeypatch)
    code = cli.main(
        [
            "myschedule", "add", "--title", "치과",
            "--start", "2026-07-01T14:00:00+09:00", "--end", "2026-07-01T15:00:00+09:00",
        ]
    )
    assert code == 0
    assert FakeCentralClient.calls[-1] == (
        "personal_schedule_add",
        {
            "title": "치과",
            "starts_at": "2026-07-01T14:00:00+09:00",
            "ends_at": "2026-07-01T15:00:00+09:00",
        },
    )


def test_wiki_commands_use_central_client(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    code = cli.main(
        [
            "--json",
            "--central-url",
            "https://central.test/api",
            "wiki",
            "search",
            "메일",
            "--scope",
            "company",
            "--limit",
            "3",
        ]
    )
    assert code == 0
    assert FakeCentralClient.calls[1] == ("wiki_search", "메일", "company", 3)
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"][0]["slug"] == "company/mail/p6"

    code = cli.main(
        [
            "--json",
            "wiki",
            "suggest",
            "company/mail/p6",
            "--note",
            "QA done",
            "--evidence-url",
            "https://example.test/evidence",
        ]
    )
    assert code == 0
    assert FakeCentralClient.calls[-1] == (
        "wiki_update_candidate",
        "company/mail/p6",
        "QA done",
        ["https://example.test/evidence"],
    )


def test_wiki_page_and_ask_use_central_client(monkeypatch, capsys):
    install_fake_central(monkeypatch)

    assert cli.main(["--json", "wiki", "page", "company/mail/p6"]) == 0
    page_payload = json.loads(capsys.readouterr().out)
    assert page_payload["slug"] == "company/mail/p6"
    assert FakeCentralClient.calls[-1] == ("wiki_page", "company/mail/p6")

    assert (
        cli.main(
            [
                "--json",
                "wiki",
                "ask",
                "메일 상태?",
                "--scope",
                "company",
                "--context-wiki-slug",
                "company/mail/p6",
            ]
        )
        == 0
    )
    ask_payload = json.loads(capsys.readouterr().out)
    assert ask_payload["answer"] == "grounded"
    assert FakeCentralClient.calls[-1] == (
        "wiki_ask",
        "메일 상태?",
        "company",
        "company/mail/p6",
    )


def test_work_commands_use_central_client(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    assert cli.main(["--json", "work", "list", "--state", "draft_for_review"]) == 0
    assert FakeCentralClient.calls[-1] == ("agent_work_list", "draft_for_review", 50)
    payload = json.loads(capsys.readouterr().out)
    assert payload == [{"id": "aw_1", "state": "draft_for_review"}]

    assert cli.main(["--json", "work", "show", "aw_1"]) == 0
    assert FakeCentralClient.calls[-1] == ("agent_work_get", "aw_1")


def test_mcp_config_omits_token(monkeypatch, capsys):
    monkeypatch.setenv(cli.CENTRAL_URL_ENV, "https://central.test/api")
    monkeypatch.setenv("ORTHUS_MCP_TOKEN", "dct_secret_token")

    assert cli.main(["mcp", "config", "claude"]) == 0
    assert cli.main(["mcp", "config", "codex"]) == 0

    output = capsys.readouterr().out
    assert "dct_" not in output
    assert "orthus" in output
    assert "mcp" in output
    assert "Keychain" in output


def test_mcp_config_codex_human_redacts_token_shaped_values(monkeypatch, capsys):
    monkeypatch.setenv(cli.CENTRAL_URL_ENV, "https://central.test/api/dct_secret_token")

    assert cli.main(["mcp", "config", "codex"]) == 0

    output = capsys.readouterr().out
    assert "dct_" not in output
    assert "<redacted>" in output


def test_version_reports_cli_and_mcp(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_package_version", lambda: "1.2.3")

    assert cli.main(["version", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert "app" not in payload
    assert payload["cli"] == {"package": "orthus", "version": "1.2.3"}
    assert payload["mcp"] == {"command": "orthus-mcp", "version": "1.2.3"}


def test_init_dry_run_does_not_write_or_echo_tokens(monkeypatch, capsys, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv(cli.CLI_CONFIG_ENV, str(config_path))
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kwargs: calls.append(argv) or None)

    assert (
        cli.main(
            [
                "init",
                "--central-url",
                "https://central.test/api",
                "--mcp-token",
                "plain_mcp_secret",
                "--collector-token",
                "plain_collector_secret",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "plain_mcp_secret" not in output
    assert "plain_collector_secret" not in output
    assert calls == []
    assert not config_path.exists()
    payload = json.loads(output)
    assert payload["dry_run"] is True
    assert payload["config"]["would_write"] is True
    assert payload["keychain"] == {
        "mcp_token": "would_store",
        "collector_token": "would_store",
    }


def test_init_stores_config_and_central_client_uses_config_fallback(
    monkeypatch,
    capsys,
    tmp_path,
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv(cli.CLI_CONFIG_ENV, str(config_path))
    monkeypatch.delenv(cli.CENTRAL_URL_ENV, raising=False)

    assert (
        cli.main(
            [
                "init",
                "--central-url",
                "https://cfg.test/api/",
                "--json",
            ]
        )
        == 0
    )
    init_payload = json.loads(capsys.readouterr().out)
    assert init_payload["config"]["written"] is True
    assert json.loads(config_path.read_text())["central_url"] == "https://cfg.test/api"

    install_fake_central(monkeypatch)
    # 이 테스트의 대상은 config-file 폴백이다 — 헬퍼가 주입한 env를 걷어낸다.
    monkeypatch.delenv(cli.CENTRAL_URL_ENV, raising=False)
    assert cli.main(["--json", "wiki", "search", "hello"]) == 0
    json.loads(capsys.readouterr().out)
    config = FakeCentralClient.calls[0][1]
    assert config.base_url == "https://cfg.test/api"


def test_mcp_config_uses_cli_config_when_env_and_arg_absent(
    monkeypatch,
    capsys,
    tmp_path,
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv(cli.CLI_CONFIG_ENV, str(config_path))
    monkeypatch.delenv(cli.CENTRAL_URL_ENV, raising=False)
    cli._write_cli_config({"central_url": "https://cfg.test/api"}, config_path)

    assert cli.main(["mcp", "config", "codex"]) == 0

    output = capsys.readouterr().out
    assert 'ORTHUS_MCP_CENTRAL_URL = "https://cfg.test/api"' in output


def test_init_stores_tokens_with_security_argv(monkeypatch, capsys, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv(cli.CLI_CONFIG_ENV, str(config_path))
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert (
        cli.main(
            [
                "init",
                "--central-url",
                "https://central.test/api",
                "--mcp-token",
                "dct_mcp_secret",
                "--collector-token",
                "dct_collector_secret",
                "--json",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "dct_" not in output
    assert len(calls) == 2
    assert all(isinstance(argv, list) for argv in calls)
    assert calls[0][:5] == ["security", "add-generic-password", "-U", "-s", cli.KEYCHAIN_SERVICE]
    assert calls[0][-2:] == ["-w", "dct_mcp_secret"]
    assert calls[1][:5] == [
        "security",
        "add-generic-password",
        "-U",
        "-s",
        cli.COLLECTOR_KEYCHAIN_SERVICE,
    ]
    assert calls[1][-2:] == ["-w", "dct_collector_secret"]
    payload = json.loads(output)
    assert payload["keychain"] == {"mcp_token": "stored", "collector_token": "stored"}


def test_init_keychain_unavailable_fails_without_echoing_token(
    monkeypatch,
    capsys,
    tmp_path,
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv(cli.CLI_CONFIG_ENV, str(config_path))

    def fake_run(argv, **kwargs):
        raise FileNotFoundError("security")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert (
        cli.main(
            [
                "init",
                "--central-url",
                "https://central.test/api",
                "--mcp-token",
                "plain_secret_token",
                "--json",
            ]
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "plain_secret_token" not in output
    payload = json.loads(output)
    assert payload["ok"] is False
    assert "security command not found" in payload["error"]["message"]
    assert not config_path.exists()


def test_init_collector_token_stdin_prompts_and_stores_without_argv(
    monkeypatch,
    capsys,
    tmp_path,
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv(cli.CLI_CONFIG_ENV, str(config_path))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "dct_prompted_collector")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert (
        cli.main(
            [
                "init",
                "--central-url",
                "https://central.test/api",
                "--collector-token-stdin",
                "--json",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    # Token reaches the keychain but never the orthus argv or stdout.
    assert "dct_prompted_collector" not in output
    assert len(calls) == 1
    assert calls[0][:5] == [
        "security",
        "add-generic-password",
        "-U",
        "-s",
        cli.COLLECTOR_KEYCHAIN_SERVICE,
    ]
    assert calls[0][-2:] == ["-w", "dct_prompted_collector"]
    payload = json.loads(output)
    assert payload["keychain"]["collector_token"] == "stored"


def test_init_collector_token_and_stdin_mutually_exclusive(monkeypatch, capsys, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv(cli.CLI_CONFIG_ENV, str(config_path))

    def fail_prompt(prompt=""):
        raise AssertionError("getpass must not run when args conflict")

    monkeypatch.setattr(cli.getpass, "getpass", fail_prompt)

    assert (
        cli.main(
            [
                "init",
                "--central-url",
                "https://central.test/api",
                "--collector-token",
                "dct_explicit",
                "--collector-token-stdin",
                "--json",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "mutually exclusive" in payload["error"]["message"]
    assert not config_path.exists()


def test_init_token_stdin_dry_run_reports_without_prompting(monkeypatch, capsys, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv(cli.CLI_CONFIG_ENV, str(config_path))

    def fail_prompt(prompt=""):
        raise AssertionError("dry-run must not prompt for a secret")

    monkeypatch.setattr(cli.getpass, "getpass", fail_prompt)
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kwargs: calls.append(argv) or None)
    calls: list[list[str]] = []

    assert (
        cli.main(
            [
                "init",
                "--central-url",
                "https://central.test/api",
                "--collector-token-stdin",
                "--mcp-token-stdin",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )

    assert calls == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["keychain"] == {
        "mcp_token": "would_store",
        "collector_token": "would_store",
    }
    assert not config_path.exists()


def test_central_exit_code_treats_http_530_as_unreachable():
    exc = cli.CentralError(
        "central temporarily unavailable (HTTP 530; retry shortly)",
        status=530,
    )

    assert cli._central_exit_code(exc) == 4


def test_doctor_json_no_token(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    monkeypatch.setattr(
        cli,
        "_run_mcp_smoke",
        lambda: subprocess.CompletedProcess(
            [sys.executable, "scripts/mcp/stdio_smoke.py"],
            0,
            stdout="mcp-smoke OK\n",
            stderr="",
        ),
    )
    monkeypatch.setenv(cli.CENTRAL_URL_ENV, "https://central.test/api")

    assert cli.main(["doctor", "--json"]) == 0
    output = capsys.readouterr().out
    assert "dct_" not in output
    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["central"]["token_present"] is True
    assert payload["central"]["reachable"] is True
    assert payload["mcp"]["smoke_ok"] is True
    assert payload["cli"]["expected_symlink_path"].endswith("/.local/bin/orthus")
    assert payload["cli"]["status"] in {
        "installed",
        "missing",
        "broken",
        "mismatch",
        "unavailable",
    }


def _write_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)


def test_cli_doctor_status_reports_installed_symlink(tmp_path):
    current = tmp_path / "tools" / "bin" / "orthus"
    _write_executable(current)
    link = tmp_path / ".local" / "bin" / "orthus"
    link.parent.mkdir(parents=True)
    os.symlink(current, link)

    status = cli._cli_doctor_status(
        home=tmp_path,
        path_env=str(link.parent),
        current_executable=current,
    )

    assert status["status"] == "installed"
    assert status["symlink_target_resolved"] == str(current)
    assert status["path_has_local_bin"] is True
    assert status["path_snippet"] == cli.LOCAL_BIN_SNIPPET


def test_cli_doctor_status_reports_broken_symlink(tmp_path):
    current = tmp_path / "tools" / "bin" / "orthus"
    old = tmp_path / "old" / "bin" / "orthus"
    _write_executable(current)
    link = tmp_path / ".local" / "bin" / "orthus"
    link.parent.mkdir(parents=True)
    os.symlink(old, link)

    status = cli._cli_doctor_status(
        home=tmp_path,
        path_env="/usr/bin:/bin",
        current_executable=current,
    )

    assert status["status"] == "broken"
    assert status["broken"] is True
    assert status["path_has_local_bin"] is False


def test_cli_doctor_status_reports_mismatch_symlink(tmp_path):
    current = tmp_path / "tools" / "bin" / "orthus"
    other = tmp_path / "other" / "bin" / "not-orthus"
    _write_executable(current)
    _write_executable(other)
    link = tmp_path / ".local" / "bin" / "orthus"
    link.parent.mkdir(parents=True)
    os.symlink(other, link)

    status = cli._cli_doctor_status(home=tmp_path, current_executable=current)

    assert status["status"] == "mismatch"
    assert status["broken"] is False


def test_cli_doctor_status_source_run_is_structured(tmp_path):
    current = tmp_path / "repo" / ".venv" / "bin" / "orthus"
    _write_executable(current)

    status = cli._cli_doctor_status(home=tmp_path, current_executable=current)

    assert status["current_executable"] == str(current)
    assert status["status"] == "unavailable"


def test_mcp_smoke_subprocess_can_be_monkeypatched(monkeypatch, capsys):
    calls: list[str] = []

    async def fake_smoke():
        calls.append("smoke")
        return 0, "smoke ok", ""

    monkeypatch.setattr(cli, "_run_mcp_smoke_async", fake_smoke)

    assert cli.main(["--json", "mcp", "smoke"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["stdout"] == "smoke ok"
    assert calls == ["smoke"]


def test_connector_list_uses_central_client(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    assert (
        cli.main(["--json", "--central-url", "https://central.test/api", "connector", "list"]) == 0
    )
    assert FakeCentralClient.calls[0][1].token == "dct_collector_token"
    assert FakeCentralClient.calls[-1] == ("connector_list",)
    payload = json.loads(capsys.readouterr().out)
    assert payload["manifests"][0]["slug"] == "github"


def test_wiki_commands_still_use_mcp_token(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    assert cli.main(["--json", "--central-url", "https://central.test/api", "wiki", "search", "p6"]) == 0
    assert FakeCentralClient.calls[0][1].token == "dct_secret_token"
    capsys.readouterr()


def test_connector_show_projects_manifest_and_account(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    assert cli.main(["--json", "connector", "show", "github"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["manifest"]["slug"] == "github"
    assert payload["account"]["account_id"] == "acct-github"


def test_connector_show_unknown_slug_exits_2(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    assert cli.main(["--json", "connector", "show", "nope"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_connector_config_prompts_secret_and_passes_settings(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "ghp_prompted_secret")
    assert (
        cli.main(
            [
                "--json",
                "connector",
                "config",
                "github",
                "--set",
                "repos=acme/app",
                "--secret",
                "token",
            ]
        )
        == 0
    )
    config_call = FakeCentralClient.calls[-1]
    assert config_call[0] == "connector_config"
    assert config_call[1] == "github"
    assert config_call[2] == {"repos": "acme/app"}
    assert config_call[3] == {"token": "ghp_prompted_secret"}
    output = capsys.readouterr().out
    # The prompted secret must not be echoed to stdout.
    assert "ghp_prompted_secret" not in output


def test_connector_config_rejects_unknown_setting_key(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    assert cli.main(["--json", "connector", "config", "github", "--set", "bogus=1"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "unknown setting" in payload["error"]["message"]
    # No config write was attempted.
    assert not any(call[0] == "connector_config" for call in FakeCentralClient.calls)


def test_connector_config_rejects_secret_in_set(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    assert cli.main(["--json", "connector", "config", "github", "--set", "token=plain"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert "use --secret" in payload["error"]["message"]
    assert not any(call[0] == "connector_config" for call in FakeCentralClient.calls)


def test_connector_ensure_and_delete_use_central_client(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    assert cli.main(["--json", "connector", "ensure", "local_files"]) == 0
    assert FakeCentralClient.calls[-1] == ("connector_ensure", "local_files")
    capsys.readouterr()

    # No --account-id: the single github account is resolved from connector_list.
    assert cli.main(["--json", "connector", "delete", "github"]) == 0
    assert FakeCentralClient.calls[-1] == ("connector_delete", "github", "acct-github")
    capsys.readouterr()

    # Explicit --account-id is passed through verbatim.
    assert cli.main(["--json", "connector", "delete", "github", "--account-id", "acct-x"]) == 0
    assert FakeCentralClient.calls[-1] == ("connector_delete", "github", "acct-x")


def test_connector_parser_registers_commands():
    parser = cli.build_parser()
    assert parser.parse_args(["connector", "list"]).func is cli.cmd_connector_list
    assert parser.parse_args(["connector", "show", "github"]).func is cli.cmd_connector_show
    assert parser.parse_args(["connector", "config", "github"]).func is cli.cmd_connector_config
    assert parser.parse_args(["connector", "ensure", "github"]).func is cli.cmd_connector_ensure
    assert parser.parse_args(["connector", "delete", "github"]).func is cli.cmd_connector_delete


def test_pyproject_scripts_present():
    repo_root = Path(__file__).resolve().parents[2]
    payload = tomllib.loads((repo_root / "pyproject.toml").read_text())
    assert payload["project"]["scripts"] == {
        "orthus": "orthus.cli:main",
        "orthus-mcp": "orthus.mcp.server:main",
    }


def test_parser_registers_whoami_skills_update():
    parser = cli.build_parser()
    assert parser.parse_args(["whoami"]).func is cli.cmd_whoami
    assert parser.parse_args(["skills", "list"]).func is cli.cmd_skills_list
    get_args = parser.parse_args(["skills", "get", "orthus-usage", "--full"])
    assert get_args.func is cli.cmd_skills_get
    assert get_args.full is True
    assert parser.parse_args(["update"]).func is cli.cmd_update


def test_package_version_falls_back_to_version_file(monkeypatch, tmp_path):
    # pyproject version is dynamic (from VERSION); when the dist metadata is
    # absent (uninstalled source run) `_package_version` reads VERSION directly.
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)

    def _raise(name):
        raise cli.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(cli.metadata, "version", _raise)
    assert cli._package_version() == "9.9.9"


def test_uv_tool_root_detects_receipt(monkeypatch, tmp_path):
    # A uv-tool venv carries uv-receipt.toml at its root; a plain venv does not.
    monkeypatch.setattr(cli.sys, "prefix", str(tmp_path))
    assert cli._uv_tool_root() is None
    (tmp_path / "uv-receipt.toml").write_text("", encoding="utf-8")
    assert cli._uv_tool_root() == tmp_path.resolve()


def test_update_uv_tool_install_runs_uv_tool_upgrade(monkeypatch, capsys, tmp_path):
    # Non-git root so the source branch is skipped; running from a uv-tool venv.
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    tool_root = tmp_path / "uvtool"
    tool_root.mkdir()
    monkeypatch.setattr(cli, "_uv_tool_root", lambda: tool_root)
    monkeypatch.setattr(cli, "_package_version", lambda: "0.1.68")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout="Updated orthus v0.1.68 -> v0.1.69\n", stderr=""
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.main(["--json", "update"]) == 0
    assert calls == [["uv", "tool", "upgrade", "orthus"]]
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "uv-tool"
    assert payload["updated"] is True
    assert payload["tool_root"] == str(tool_root)
    assert "Updated orthus" in payload["detail"]


def test_update_source_checkout_force_reinstalls_orthus(monkeypatch, capsys, tmp_path):
    # Source (.git) checkout: git pull, then a uv sync that FORCE-reinstalls orthus so
    # the dynamic version (from VERSION) is re-baked — a plain `uv sync` would leave
    # `orthus version` reporting the pre-pull release.
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_package_version", lambda: "0.1.73")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["--json", "update"]) == 0
    assert ["uv", "sync", "--reinstall-package", "orthus"] in calls
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "source"


def test_update_uv_tool_install_without_uv_errors(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_uv_tool_root", lambda: tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    # Never shell out when uv is unavailable.
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: pytest.fail("must not run subprocess")
    )
    assert cli.main(["--json", "update"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "uv" in payload["error"]["message"]


def test_whoami_uses_central_client(monkeypatch, capsys):
    install_fake_central(monkeypatch)
    assert cli.main(["--json", "--central-url", "https://central.test/api", "whoami"]) == 0
    assert FakeCentralClient.calls[-1] == ("whoami",)
    payload = json.loads(capsys.readouterr().out)
    assert payload["role"] == "admin"
    assert payload["node_id"] == "company"


def test_skills_list_and_get_read_bundled_skill(capsys):
    assert cli.main(["--json", "skills", "list"]) == 0
    listing = json.loads(capsys.readouterr().out)
    names = [s["name"] for s in listing["skills"]]
    assert "orthus-usage" in names

    assert cli.main(["skills", "get", "orthus-usage", "--full"]) == 0
    full = capsys.readouterr().out
    assert "orthus-mcp" in full
    assert "wiki_update_candidate" in full
    assert full.startswith("---")  # YAML frontmatter present with --full

    assert cli.main(["skills", "get", "orthus-usage"]) == 0
    body = capsys.readouterr().out
    assert not body.startswith("---")  # frontmatter stripped by default
    assert "orthus whoami" in body


def test_skills_get_unknown_skill_fails():
    # Unknown skill raises RuntimeError -> exit code 1 (handled in main()).
    assert cli.main(["skills", "get", "does-not-exist"]) == 1


def test_skills_root_uses_meipass_when_frozen(monkeypatch, tmp_path):
    # PyInstaller --onefile sets sys._MEIPASS; the bundled skills live under it.
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert cli._skills_root() == Path(tmp_path) / "orthus" / "skills"
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    # Source/wheel install: co-located with the module.
    assert cli._skills_root() == Path(cli.__file__).resolve().parent / "skills"
