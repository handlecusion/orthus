"""Productized ``orthus`` CLI core.

P9.1 keeps this as a thin router over the existing MCP central client and MCP
stdio server. Secrets stay in ``orthus.mcp.central`` env/Keychain resolution and
are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
import getpass
import http.server
from importlib import metadata
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from orthus.mcp.central import (
    CENTRAL_URL_ENV,
    KEYCHAIN_SERVICE,
    CentralClient,
    CentralConfig,
    CentralError,
    resolve_token,
)
from orthus.mcp.tickets import (
    TICKET_PRIORITIES,
    TICKET_STATUSES,
    TicketUXError,
    board_prop,
    normalize_priority,
    normalize_status,
    parse_date_word,
    resolve_any_ticket,
    resolve_bucket,
    resolve_kanban_board,
    resolve_project,
    short_id,
)

TOKEN_RE = re.compile(r"dct_[A-Za-z0-9._:-]+")
CLI_CONFIG_ENV = "ORTHUS_CLI_CONFIG"
CLI_CONFIG_PATH = Path(".orthus") / "config.json"

# Collector-scope token resolution (server-side collector API auth). The client
# collector daemon itself was removed; these stay because the owner-scope
# connector CLI (`orthus connector …`) authenticates with the same dct_ token
# stored under this Keychain service.
COLLECTOR_CENTRAL_URL_ENV = "ORTHUS_COLLECTOR_CENTRAL_URL"
COLLECTOR_TOKEN_ENV = "ORTHUS_COLLECTOR_TOKEN"
COLLECTOR_KEYCHAIN_SERVICE = "orthus-collector-token"


def resolve_collector_token() -> str | None:
    """Env wins; fall back to the macOS Keychain. Returns None if unavailable."""
    env_token = os.environ.get(COLLECTOR_TOKEN_ENV, "").strip()
    if env_token:
        return env_token
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", COLLECTOR_KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if completed.returncode != 0:
        return None
    token = completed.stdout.strip()
    return token or None
EXPECTED_MCP_TOOLS = {
    # central
    "wiki_search",
    "wiki_page",
    "wiki_ask",
    "structured",
    "team_schedule",
    "team_schedule_add",
    "team_schedule_update",
    "team_schedule_delete",
    "team_members",
    "team_members_add",
    "personal_schedule_list",
    "personal_schedule_add",
    "personal_schedule_update",
    "ticket_list",
    "ticket_get",
    "ticket_create",
    "ticket_update",
    "ticket_comment",
    "ticket_project_list",
    "ticket_board",
    "ticket_board_add",
    "ticket_board_move",
    "wiki_update_candidate",
    "whoami",
    "agent_work_list",
    "agent_work_get",
    # P10.4b personal agent gateway: agent-proposed gated commit actions
    "submit_email_draft",
    "delegate_task",
    # P10 mail read tools (owner-isolated dual-auth inbox)
    "mail_list",
    "mail_get",
    # pre-existing tools that had drifted out of this smoke set (fixed alongside tickets)
    "board",
    "projects",
    "data_gaps",
    "inbox_summary",
    "kg_relations",
    "entity_relations",
}
LOCAL_BIN_SNIPPET = 'export PATH="$HOME/.local/bin:$PATH"'


def _redact(value: str) -> str:
    return TOKEN_RE.sub("<redacted>", value)


def _sanitize(payload: Any) -> Any:
    if isinstance(payload, str):
        return _redact(payload)
    if isinstance(payload, list):
        return [_sanitize(item) for item in payload]
    if isinstance(payload, dict):
        return {key: _sanitize(value) for key, value in payload.items()}
    return payload


def _emit_json(payload: Any) -> None:
    json.dump(_sanitize(payload), sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _emit_human(payload: Any) -> None:
    json.dump(_sanitize(payload), sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _json_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False) or getattr(args, "global_json", False))


def _emit(args: argparse.Namespace, payload: Any) -> None:
    if _json_requested(args):
        _emit_json(payload)
    else:
        _emit_human(payload)


def _central_client(args: argparse.Namespace) -> CentralClient:
    central_url = _resolved_central_url(args)
    if central_url:
        token = resolve_token()
        if not token:
            raise CentralError(
                f"knowledge token not found (set ORTHUS_MCP_TOKEN or store it in Keychain "
                f"service '{KEYCHAIN_SERVICE}')"
            )
        return CentralClient(
            CentralConfig(base_url=central_url, token=token),
        )
    return CentralClient()


def _connector_client(args: argparse.Namespace) -> CentralClient:
    central_url = _resolved_central_url(args)
    if not central_url:
        raise CentralError(f"{CENTRAL_URL_ENV} is not set; point it at the central API base URL")
    token = resolve_collector_token()
    if not token:
        raise CentralError(
            f"collector token not found (set {COLLECTOR_TOKEN_ENV} or store it in Keychain "
            f"service '{COLLECTOR_KEYCHAIN_SERVICE}')"
        )
    return CentralClient(CentralConfig(base_url=central_url, token=token))


def _central_exit_code(exc: CentralError) -> int:
    message = str(exc)
    if "not set" in message or "not found" in message:
        return 2
    if "401" in message or "403" in message or "missing the required scope" in message:
        return 3
    status = getattr(exc, "status", None)
    if "unreachable" in message or (isinstance(status, int) and status >= 500):
        return 4
    return 1


def _emit_error(args: argparse.Namespace, exc: Exception, code: int) -> None:
    message = _redact(str(exc))
    if _json_requested(args):
        _emit_json({"ok": False, "error": {"code": code, "message": message}})
        return
    print(f"FAIL {message}", file=sys.stderr)
    if isinstance(exc, CentralError):
        if code == 2:
            if "collector token" in message:
                print(
                    f"FIX set {CENTRAL_URL_ENV} and store collector token in Keychain service "
                    f"'{COLLECTOR_KEYCHAIN_SERVICE}'",
                    file=sys.stderr,
                )
            else:
                print(
                    f"FIX set {CENTRAL_URL_ENV} and store token in Keychain service "
                    f"'{KEYCHAIN_SERVICE}'",
                    file=sys.stderr,
                )
        elif code == 3:
            scope_hint = (
                "collector/commands scopes" if "collector" in message else "knowledge scopes"
            )
            print(f"FIX issue token with required {scope_hint}", file=sys.stderr)
        elif code == 4:
            print("FIX check central URL and network reachability", file=sys.stderr)


def _emit_usage_error(args: argparse.Namespace, message: str) -> None:
    if _json_requested(args):
        _emit_json({"error": {"code": 2, "message": _redact(message)}, "ok": False})
        return
    print(f"FAIL {_redact(message)}", file=sys.stderr)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _cli_config_path(*, home: Path | None = None) -> Path:
    configured = os.environ.get(CLI_CONFIG_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return (home or Path.home()).expanduser() / CLI_CONFIG_PATH


def _read_cli_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or _cli_config_path()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _write_cli_config(payload: dict[str, Any], path: Path | None = None) -> None:
    config_path = path or _cli_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)


def _configured_central_url() -> str:
    value = _read_cli_config().get("central_url")
    return str(value).strip() if value else ""


def _resolved_central_url(args: argparse.Namespace) -> str:
    return (
        getattr(args, "central_url", None)
        or os.environ.get(CENTRAL_URL_ENV, "").strip()
        or _configured_central_url()
    ).rstrip("/")


@contextmanager
def _central_url_env_fallback(args: argparse.Namespace, env_key: str) -> Iterator[None]:
    if os.environ.get(env_key, "").strip():
        yield
        return
    central_url = _resolved_central_url(args)
    if not central_url:
        yield
        return

    previous = os.environ.get(env_key)
    os.environ[env_key] = central_url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = previous


def cmd_wiki_search(args: argparse.Namespace) -> int:
    payload = _central_client(args).wiki_search(args.query, scope=args.scope, limit=args.limit)
    _emit(args, payload)
    return 0


def cmd_wiki_page(args: argparse.Namespace) -> int:
    payload = _central_client(args).wiki_page(args.slug)
    _emit(args, payload)
    return 0


def cmd_wiki_ask(args: argparse.Namespace) -> int:
    payload = _central_client(args).wiki_ask(
        args.question,
        scope=args.scope,
        context_wiki_slug=args.context_wiki_slug,
    )
    _emit(args, payload)
    return 0


def cmd_wiki_suggest(args: argparse.Namespace) -> int:
    payload = _central_client(args).wiki_update_candidate(
        args.slug,
        args.note,
        args.evidence_url,
    )
    _emit(args, payload)
    return 0


def cmd_work_list(args: argparse.Namespace) -> int:
    payload = _central_client(args).agent_work_list(state=args.state, limit=args.limit)
    _emit(args, payload)
    return 0


def cmd_work_show(args: argparse.Namespace) -> int:
    payload = _central_client(args).agent_work_get(args.work_id)
    _emit(args, payload)
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    payload = _central_client(args).whoami()
    _emit(args, payload)
    return 0


def _skills_root() -> Path:
    # PyInstaller --onefile extracts bundled data under sys._MEIPASS; in source /
    # wheel installs the skills dir is co-located with this module.
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "orthus" / "skills"
    return Path(__file__).resolve().parent / "skills"


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].lstrip("\n")
    return text


def _skill_description(skill_md: Path) -> str:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def cmd_skills_list(args: argparse.Namespace) -> int:
    root = _skills_root()
    skills = (
        [
            (child.name, _skill_description(child / "SKILL.md"))
            for child in sorted(root.iterdir())
            if (child / "SKILL.md").is_file()
        ]
        if root.is_dir()
        else []
    )
    if _json_requested(args):
        _emit_json({"skills": [{"name": n, "description": d} for n, d in skills]})
    else:
        if not skills:
            print("(no skills bundled)")
        for name, desc in skills:
            print(f"{name}\t{desc}")
    return 0


def cmd_skills_get(args: argparse.Namespace) -> int:
    skill_md = _skills_root() / args.name / "SKILL.md"
    if not skill_md.is_file():
        raise RuntimeError(f"unknown skill: {args.name} (try `orthus skills list`)")
    text = skill_md.read_text(encoding="utf-8")
    if not args.full:
        text = _strip_frontmatter(text)
    if _json_requested(args):
        _emit_json({"name": args.name, "content": text})
    else:
        print(text)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """Self-update the CLI/skill, dispatching on install shape:

    - source checkout (``.git``): fast-forward + reinstall deps;
    - ``uv tool install``: ``uv tool upgrade orthus``.
    """
    before = _package_version()
    root = _repo_root()
    if (root / ".git").is_dir():
        pull = subprocess.run(  # noqa: S603 - argv list, never shell=True
            ["git", "-C", str(root), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            check=False,
        )
        if pull.returncode != 0:
            raise RuntimeError(f"git pull failed: {(pull.stderr or pull.stdout).strip()}")
        if shutil.which("uv"):
            # --reinstall-package: pyproject's version is dynamic (from VERSION),
            # but a plain `uv sync` reuses the cached editable build and does NOT
            # re-bake the version on a VERSION-only pull, so `orthus version` would
            # report the old release. Force a reinstall so the bump takes effect.
            sync_cmd = ["uv", "sync", "--reinstall-package", "orthus"]
        else:
            sync_cmd = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--force-reinstall",
                "-e",
                str(root),
            ]
        sync = subprocess.run(  # noqa: S603 - argv list, never shell=True
            sync_cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if sync.returncode != 0:
            raise RuntimeError(f"dependency sync failed: {(sync.stderr or sync.stdout).strip()}")
        _emit(
            args,
            {
                "mode": "source",
                "repo": str(root),
                "version_before": before,
                "version_after": _package_version(),
                "updated": True,
            },
        )
        return 0
    tool_root = _uv_tool_root()
    if tool_root is not None:
        if not shutil.which("uv"):
            raise RuntimeError(
                "uv-tool install detected but `uv` is not on PATH; "
                "reinstall uv or run `uv tool upgrade orthus` manually"
            )
        upgrade = subprocess.run(  # noqa: S603 - argv list, never shell=True
            ["uv", "tool", "upgrade", "orthus"],
            capture_output=True,
            text=True,
            check=False,
        )
        if upgrade.returncode != 0:
            raise RuntimeError(
                f"uv tool upgrade failed: {(upgrade.stderr or upgrade.stdout).strip()}"
            )
        _emit(
            args,
            {
                "mode": "uv-tool",
                "tool_root": str(tool_root),
                "version_before": before,
                # uv's own line ("Updated orthus vX -> vY" / "Nothing to upgrade")
                # is the honest signal; the running process still holds the old
                # version in memory, so it takes effect on the next invocation.
                "detail": (upgrade.stdout or upgrade.stderr).strip(),
                "updated": True,
                "note": "새 버전은 다음 `orthus` 실행부터 적용됩니다.",
            },
        )
        return 0
    raise RuntimeError("unknown install shape; reinstall the orthus CLI via uv or pip")


def _parse_set_pairs(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--set expects key=value, got: {pair}")
        key, _, value = pair.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"--set key is empty: {pair}")
        out[key] = value
    return out


def _manifest_for_slug(client: CentralClient, slug: str) -> dict | None:
    listing = client.connector_list()
    manifests = listing.get("manifests") if isinstance(listing, dict) else None
    if not isinstance(manifests, list):
        return None
    for manifest in manifests:
        if isinstance(manifest, dict) and manifest.get("slug") == slug:
            return manifest
    return None


def cmd_connector_list(args: argparse.Namespace) -> int:
    payload = _connector_client(args).connector_list()
    _emit(args, payload)
    return 0


def cmd_connector_show(args: argparse.Namespace) -> int:
    client = _connector_client(args)
    listing = client.connector_list()
    slug = args.slug
    manifest = _manifest_for_slug(client, slug)
    if manifest is None:
        _emit_usage_error(args, f"unknown personal connector: {slug}")
        return 2
    accounts = listing.get("accounts") if isinstance(listing, dict) else None
    account = None
    if isinstance(accounts, list):
        account = next(
            (a for a in accounts if isinstance(a, dict) and a.get("connector_slug") == slug),
            None,
        )
    _emit(args, {"manifest": manifest, "account": account})
    return 0


def cmd_connector_config(args: argparse.Namespace) -> int:
    try:
        settings = _parse_set_pairs(args.set or [])
    except ValueError as exc:
        _emit_usage_error(args, str(exc))
        return 2

    client = _connector_client(args)
    manifest = _manifest_for_slug(client, args.slug)
    if manifest is None:
        _emit_usage_error(args, f"unknown personal connector: {args.slug}")
        return 2

    fields = manifest.get("config_fields") if isinstance(manifest, dict) else None
    field_keys = {
        str(field.get("key"))
        for field in (fields or [])
        if isinstance(field, dict) and field.get("key")
    }
    secret_keys = {
        str(field.get("key"))
        for field in (fields or [])
        if isinstance(field, dict) and field.get("kind") == "secret" and field.get("key")
    }

    unknown_set = sorted(key for key in settings if key not in field_keys)
    if unknown_set:
        _emit_usage_error(
            args,
            f"unknown setting(s) for {args.slug}: {','.join(unknown_set)}; "
            f"allowed: {','.join(sorted(field_keys))}",
        )
        return 2
    overlap = sorted(key for key in settings if key in secret_keys)
    if overlap:
        _emit_usage_error(
            args,
            f"use --secret for secret field(s): {','.join(overlap)}",
        )
        return 2

    unknown_secret = sorted(key for key in (args.secret or []) if key not in secret_keys)
    if unknown_secret:
        _emit_usage_error(
            args,
            f"unknown secret field(s) for {args.slug}: {','.join(unknown_secret)}; "
            f"allowed: {','.join(sorted(secret_keys))}",
        )
        return 2

    secrets: dict[str, str] = {}
    for key in args.secret or []:
        # getpass keeps the value off argv and shell history.
        secrets[key] = getpass.getpass(f"{args.slug} {key}: ")

    payload = client.connector_config(
        args.slug,
        settings=settings,
        secrets=secrets,
        label=args.label,
    )
    _emit(args, payload)
    return 0


def cmd_connector_ensure(args: argparse.Namespace) -> int:
    payload = _connector_client(args).connector_ensure(args.slug)
    _emit(args, payload)
    return 0


def cmd_connector_delete(args: argparse.Namespace) -> int:
    client = _central_client(args)
    account_id = args.account_id
    if not account_id:
        listing = client.connector_list()
        accounts = listing.get("accounts") if isinstance(listing, dict) else None
        matches = [
            a
            for a in (accounts or [])
            if isinstance(a, dict) and a.get("connector_slug") == args.slug
        ]
        if not matches:
            _emit_usage_error(args, f"no {args.slug} account to delete")
            return 2
        if len(matches) > 1:
            _emit_usage_error(
                args,
                f"{args.slug} has multiple accounts; pass --account-id to choose one",
            )
            return 2
        account_id = str(matches[0].get("account_id"))
    payload = client.connector_delete(args.slug, account_id)
    _emit(args, payload)
    return 0


# ---- calendar (team schedule) + myschedule (personal schedule) ----------------
def _calendar_payload(args: argparse.Namespace) -> dict:
    fields = {
        "title": args.title,
        "event_date": args.date,
        "all_day": args.all_day,
        "end_date": args.end_date,
        "start_time": args.start_time,
        "end_time": args.end_time,
        "event_type": args.event_type,
        "location": args.location,
        "description": args.description,
        "color": args.color,
    }
    payload = {k: v for k, v in fields.items() if v is not None}
    if args.member:
        payload["member_ids"] = args.member
    return payload


def cmd_calendar_add(args: argparse.Namespace) -> int:
    _emit(args, _central_client(args).team_schedule_add(_calendar_payload(args)))
    return 0


def cmd_calendar_update(args: argparse.Namespace) -> int:
    _emit(args, _central_client(args).team_schedule_update(args.event_id, _calendar_payload(args)))
    return 0


def cmd_calendar_delete(args: argparse.Namespace) -> int:
    _emit(args, _central_client(args).team_schedule_delete(args.event_id))
    return 0


def cmd_calendar_list(args: argparse.Namespace) -> int:
    _emit(args, {"events": _central_client(args).team_schedule(args.since, args.until)})
    return 0


def cmd_calendar_members_list(args: argparse.Namespace) -> int:
    _emit(args, {"members": _central_client(args).team_members()})
    return 0


def cmd_calendar_members_add(args: argparse.Namespace) -> int:
    payload = {"name": args.name}
    for key in ("title", "department", "email", "phone"):
        value = getattr(args, key, None)
        if value:
            payload[key] = value
    _emit(args, _central_client(args).team_members_add(payload))
    return 0


def _myschedule_payload(args: argparse.Namespace) -> dict:
    fields = {
        "title": args.title,
        "starts_at": args.start,
        "ends_at": args.end,
        "project_id": args.project_id,
        "source_label": getattr(args, "source_label", None),
    }
    return {k: v for k, v in fields.items() if v is not None}


def cmd_myschedule_add(args: argparse.Namespace) -> int:
    _emit(args, _central_client(args).personal_schedule_add(_myschedule_payload(args)))
    return 0


def cmd_myschedule_update(args: argparse.Namespace) -> int:
    _emit(
        args,
        _central_client(args).personal_schedule_update(args.event_id, _myschedule_payload(args)),
    )
    return 0


def cmd_myschedule_list(args: argparse.Namespace) -> int:
    _emit(args, {"events": _central_client(args).personal_schedule_list(args.since, args.until)})
    return 0


TICKET_OVERVIEW = """\
orthus ticket — 티켓 (이슈 트래킹). 기본은 내 보드, --project를 주면 그 프로젝트 칸반.

  orthus ticket ls   [--project 프로젝트] [--status ...] [--date today] [--limit N]
  orthus ticket add  "제목" [--project 프로젝트] [--status 시작전] [--assignee 이름]
                    [--priority u|p|n|l] [--due +3d] [--note/--body ...] [--prop 이름=값]
  orthus ticket set  <id> [--status ...] [--title ...] [--assignee 이름] [--due ...]
                    [--priority ...] [--date ...|--bucket 키] [--channel 이름|none]
                    [--note ...] [--prop 이름=값]
  orthus ticket rm   <id>              # soft: 내 보드=archived(복구 가능), 칸반=거부+안내
  orthus ticket show <id>              # 상세 + 댓글. id는 앞 4자 이상이면 충분
  orthus ticket comment <id> "내용"    # 내 보드 티켓 전용
  orthus ticket projects               # 채널(프로젝트) + 백로그 버킷 키

두 저장소, 한 인터페이스:
  · 내 보드(기본)       — 내 실행 항목. --channel <회사채널>이면 팀 공개 + 프로젝트에 집계
  · 프로젝트 칸반(--project) — 팀 공유 작업 카드. 담당자 지정 가능, 컬럼(상태)로 관리
id는 저장소를 몰라도 된다 — 내 보드와 담당 프로젝트 칸반을 함께 검색한다.
--status/--priority는 어느 쪽이든 통한다: 칸반에서 open/done은 컬럼 그룹으로,
u/urgent 등은 그 보드의 우선순위 옵션으로 자동 매핑된다.
옵션 상세 + 예시: orthus ticket <명령> --help · 워크플로: orthus skills get ticket
삭제 명령은 되돌릴 수 있는 것만 있다(rm=archive). 실삭제는 웹에서만."""


def _emit_ticket_ux_error(args: argparse.Namespace, exc: TicketUXError) -> int:
    """FAIL/VALID/TRY/NOTE self-healing error (JSON mirror for --json).

    NOTE는 규칙의 배경/반대편 저장소의 대응 수단을 한 줄로 가르친다 — 에이전트가
    같은 실수를 반복하지 않게."""
    if _json_requested(args):
        error: dict[str, Any] = {"code": 2, "message": _redact(str(exc))}
        if exc.valid:
            error["valid"] = exc.valid
        if exc.example:
            error["example"] = exc.example
        if exc.note:
            error["note"] = exc.note
        _emit_json({"ok": False, "error": error})
        return 2
    print(f"FAIL {_redact(str(exc))}", file=sys.stderr)
    if exc.valid:
        print(f"VALID {' | '.join(exc.valid)}", file=sys.stderr)
    if exc.example:
        print(f"TRY  {exc.example}", file=sys.stderr)
    if exc.note:
        print(f"NOTE {exc.note}", file=sys.stderr)
    return 2


def _emit_ticket_central_error(args: argparse.Namespace, exc: CentralError) -> int:
    """Map central HTTP errors to actionable ticket guidance; None → re-raise path."""
    status = getattr(exc, "status", None)
    message = str(exc)
    if status == 404:
        return _emit_ticket_ux_error(
            args,
            TicketUXError("ticket not found on your board", example="orthus ticket ls"),
        )
    if status == 422:
        detail = message.split(":", 1)[-1].strip() if ":" in message else message
        if "placement" in message:
            return _emit_ticket_ux_error(
                args,
                TicketUXError(
                    f"{detail} — 내 보드 티켓 배치는 날짜(--date)와 백로그(--bucket) 중 정확히 하나",
                    example='orthus ticket add "제목" --date today  (또는 --bucket next_week)',
                ),
            )
        return _emit_ticket_ux_error(args, TicketUXError(detail))
    if status == 429:
        return _emit_ticket_ux_error(
            args,
            TicketUXError(
                "write rate limit exceeded (30 writes / 5 min per token) — wait and retry",
            ),
        )
    raise exc


def _ticket_line(task: dict) -> str:
    placement = str(task.get("scheduled_date") or "backlog")
    project = task.get("project") or {}
    suffix = f"  [{project['name']}]" if project.get("name") else ""
    due = f"  due:{task['due_date']}" if task.get("due_date") else ""
    return (
        f"{short_id(task['task_id'])}  {task.get('status', ''):<8}"
        f"{task.get('priority', ''):<9}{placement:<11}{task.get('title', '')}{suffix}{due}"
    )


def _print_ticket_detail(task: dict, comments: list[dict]) -> None:
    project = task.get("project") or {}
    lines = [
        f"id:        {task['task_id']}  (short: {short_id(task['task_id'])})",
        f"title:     {task.get('title', '')}",
        f"status:    {task.get('status', '')}",
        f"priority:  {task.get('priority', '')}",
        f"placement: {task.get('scheduled_date') or 'backlog'}",
    ]
    if task.get("due_date"):
        due_time = f" {task['due_time']}" if task.get("due_time") else ""
        lines.append(f"due:       {task['due_date']}{due_time}")
    if project.get("name"):
        lines.append(f"project:   {project['name']} ({project.get('kind', 'personal')})")
    if task.get("scope") == "company":
        lines.append("scope:     company (팀 전체에게 보임)")
    if task.get("note"):
        lines.append(f"note:      {task['note']}")
    for subtask in task.get("subtasks") or []:
        mark = "x" if subtask.get("completed") else " "
        lines.append(f"subtask:   [{mark}] {subtask.get('title', '')}")
    for comment in comments:
        author = comment.get("author_name") or "?"
        lines.append(
            f"comment:   {comment.get('created_at', '')} {author}: {comment.get('body', '')}"
        )
    print("\n".join(lines))


def _print_kanban_card_detail(schema: dict, row: dict, members: dict[str, str]) -> None:
    """칸반 카드 상세 — 스키마 속성 이름으로 값을 풀어 보여준다."""
    props = row.get("props") or {}
    project_name = (schema.get("_project") or {}).get("name")
    lines = [
        f"id:        {row['row_id']}  (short: {short_id(row['row_id'])})",
        f"project:   {project_name} / {schema.get('title')}  (칸반 카드)",
    ]
    for prop in schema.get("properties") or []:
        value = props.get(str(prop["id"]))
        if value in (None, "", []):
            continue
        prop_type = prop.get("type")
        if prop_type in ("status", "select"):
            value = next(
                (o["name"] for o in prop.get("options", []) if str(o["id"]) == str(value)),
                value,
            )
        elif prop_type == "multi_select":
            names = {str(o["id"]): o["name"] for o in prop.get("options", [])}
            value = ", ".join(names.get(str(v), str(v)) for v in value)
        elif prop_type == "person":
            value = ", ".join(members.get(str(v), "?") for v in value)
        lines.append(f"{prop.get('name', '')}: {value}")
    if row.get("body"):
        lines.append(f"body:\n{_kanban_body_text(row['body'])}")
    print("\n".join(lines))


def _kanban_body_text(body: str) -> str:
    """카드 본문은 BlockNote 블록 JSON으로 저장된다 — 사람이 읽을 텍스트로 푼다."""
    try:
        blocks = json.loads(body)
    except (ValueError, TypeError):
        return body
    if not isinstance(blocks, list):
        return body
    lines = []
    for block in blocks:
        content = block.get("content") if isinstance(block, dict) else None
        if isinstance(content, list):
            lines.append("".join(str(c.get("text", "")) for c in content if isinstance(c, dict)))
    return "\n".join(lines)


def cmd_ticket_overview(args: argparse.Namespace) -> int:
    if _json_requested(args):
        _emit_json({"help": TICKET_OVERVIEW})
    else:
        print(TICKET_OVERVIEW)
    return 0


def cmd_ticket_ls(args: argparse.Namespace) -> int:
    from orthus.mcp.tickets import kanban_snapshot

    client = _central_client(args)
    try:
        if args.project:
            from orthus.mcp.tickets import resolve_kanban_boards

            # 프로젝트에 DB(보드)가 여러 개일 수 있다 — --board 없이는 전부 보여준다.
            boards = resolve_kanban_boards(client, args.project, board=args.board)
            snapshots = [kanban_snapshot(client, b) for b in boards]
            if _json_requested(args):
                _emit_json({"project": snapshots[0]["project"], "boards": snapshots})
                return 0
            for index, snapshot in enumerate(snapshots):
                if index:
                    print()
                print(f"[{snapshot['project']} / {snapshot['board']}] 카드 {snapshot['total']}건")
                for column in snapshot["columns"]:
                    print(f"{column['status']} ({column['count']})")
                    for card in column["cards"]:
                        who = ",".join(card["assignees"]) or "—"
                        due = f"  마감:{card['due']}" if card.get("due") else ""
                        print(
                            f"  {card['short_id']}  {card['title'] or '(제목 없음)'}  담당:{who}{due}"
                        )
            return 0
        status = None if args.status == "all" else normalize_status(args.status)
        day = parse_date_word(args.date) if args.date else None
        channel_id = None
        if args.channel:
            channel_id = str(resolve_project(client, args.channel)["project_id"])
        tasks = client.board_tasks_list(
            status=status,
            project_id=channel_id,
            date_from=day,
            date_to=day,
            limit=args.limit,
        )
    except TicketUXError as exc:
        return _emit_ticket_ux_error(args, exc)
    except CentralError as exc:
        return _emit_ticket_central_error(args, exc)
    if _json_requested(args):
        _emit_json({"count": len(tasks), "tickets": tasks})
        return 0
    if not tasks:
        print("(no tickets)  TRY orthus ticket ls --status all")
        return 0
    for task in tasks:
        print(_ticket_line(task))
    return 0


def cmd_ticket_show(args: argparse.Namespace) -> int:
    client = _central_client(args)
    try:
        kind, schema, obj = resolve_any_ticket(
            client, args.ticket_id, project=args.project, board=getattr(args, "board", None)
        )
        if kind == "board":
            comments = client.board_task_comments(str(obj["task_id"])) or []
            if _json_requested(args):
                _emit_json({"kind": "board", "ticket": obj, "comments": comments})
            else:
                _print_ticket_detail(obj, comments)
            return 0
        full = client.database_row_get(str(schema["database_id"]), str(obj["row_id"]))
        members = {
            str(m.get("member_id")): str(m.get("name")) for m in (client.team_members() or [])
        }
        if _json_requested(args):
            _emit_json({"kind": "kanban", "card": full, "board": schema.get("title")})
        else:
            _print_kanban_card_detail(schema, full, members)
    except TicketUXError as exc:
        return _emit_ticket_ux_error(args, exc)
    except CentralError as exc:
        return _emit_ticket_central_error(args, exc)
    return 0


def cmd_ticket_add(args: argparse.Namespace) -> int:
    from orthus.mcp.tickets import build_board_row_props

    client = _central_client(args)
    try:
        if args.project:
            # 프로젝트 칸반 카드. 내 보드 전용 플래그는 친절히 거부 + 대응 수단 안내.
            kanban_equivalent = {
                "--date": "칸반 카드의 날짜는 --due(마감/종료일)뿐",
                "--bucket": "칸반은 백로그 버킷 대신 상태 컬럼 — --status 시작전",
                "--note": "칸반 카드의 본문은 --body(markdown)",
                "--channel": "--project가 이미 프로젝트를 지정한다",
                "--id": "칸반 row 생성엔 멱등 키가 없다 — 재시도 전 ls --project로 중복 확인",
            }
            for flag, name in (
                (args.date, "--date"),
                (args.bucket, "--bucket"),
                (args.note, "--note"),
                (args.channel, "--channel"),
                (args.task_id, "--id"),
            ):
                if flag:
                    raise TicketUXError(
                        f"{name}은(는) 내 보드 티켓 전용 — --project(칸반 카드)와 함께 못 씀",
                        example='orthus ticket add "제목" --project <프로젝트> --status 시작전 --due +3d',
                        note=kanban_equivalent[name],
                    )
            board = resolve_kanban_board(client, args.project, board=args.board)
            props = build_board_row_props(
                client,
                board,
                title=args.title,
                status=args.status,
                assignee=args.assignee,
                priority=args.priority,
                due=args.due,
                sets=list(args.prop),
            )
            payload: dict[str, Any] = {"props": props}
            if args.body:
                payload["body"] = args.body
            row = client.database_row_create(str(board["database_id"]), payload)
            if _json_requested(args):
                _emit_json({"ok": True, "kind": "kanban", "card": row})
            else:
                status_prop = board_prop(board, prop_type="status")
                status_name = "—"
                if status_prop:
                    oid = row.get("props", {}).get(str(status_prop["id"]))
                    status_name = next(
                        (
                            o["name"]
                            for o in status_prop.get("options", [])
                            if str(o["id"]) == str(oid)
                        ),
                        "—",
                    )
                print(
                    f"OK card {short_id(row['row_id'])}  "
                    f"[{board.get('_project', {}).get('name')} / {board.get('title')}]  "
                    f"{status_name}  {args.title}"
                )
            return 0
        # 내 보드 티켓.
        board_equivalent = {
            "--assignee": "내 보드 티켓은 항상 나(토큰 소유자) 담당 — 남에게 주려면 칸반 카드로",
            "--prop": "내 보드 필드는 고정 플래그(--priority/--date/--due/--note)로 지정",
            "--body": "내 보드 티켓의 자유 기록은 --note (생성 후 comment도 가능)",
        }
        for flag, name in (
            (args.assignee, "--assignee"),
            (args.prop, "--prop"),
            (args.body, "--body"),
        ):
            if flag:
                raise TicketUXError(
                    f"{name}은(는) 칸반 카드 전용 — --project와 함께 사용",
                    example='orthus ticket add "제목" --project <프로젝트> --assignee 이름',
                    note=board_equivalent[name],
                )
        if args.status:
            raise TicketUXError(
                "--status는 칸반 컬럼 지정용(--project 필요). 내 보드 티켓은 open으로 생성",
                example='orthus ticket add "제목" --project <프로젝트> --status 시작전',
                note="내 보드 상태는 생성 후 set으로: orthus ticket set <id> --status done",
            )
        if args.date and args.bucket:
            raise TicketUXError(
                "--date and --bucket are mutually exclusive (배치는 정확히 하나)",
                example='orthus ticket add "제목" --date today',
            )
        payload = {
            "title": args.title,
            "priority": normalize_priority(args.priority or "normal"),
        }
        if args.bucket:
            payload["backlog_bucket_id"] = str(resolve_bucket(client, args.bucket)["bucket_id"])
        else:
            payload["scheduled_date"] = parse_date_word(args.date or "today")
        if args.channel:
            payload["project_id"] = str(resolve_project(client, args.channel)["project_id"])
        if args.due:
            payload["due_date"] = parse_date_word(args.due)
        if args.task_id:
            # Idempotency key: reusing the same --id across retries never duplicates.
            payload["task_id"] = args.task_id
        task = client.board_task_create(payload)
        if args.note:
            task = client.board_task_update(str(task["task_id"]), {"note": args.note})
    except TicketUXError as exc:
        return _emit_ticket_ux_error(args, exc)
    except CentralError as exc:
        return _emit_ticket_central_error(args, exc)
    if _json_requested(args):
        _emit_json({"ok": True, "kind": "board", "ticket": task})
    else:
        print(f"OK created {_ticket_line(task)}")
    return 0


def _board_ticket_patch(args: argparse.Namespace, client: CentralClient) -> dict[str, Any]:
    """내 보드 티켓용 set 페이로드 — 준 플래그만 반영."""
    payload: dict[str, Any] = {}
    if args.title is not None:
        payload["title"] = args.title
    if args.status is not None:
        payload["status"] = normalize_status(args.status)
    if args.priority is not None:
        payload["priority"] = normalize_priority(args.priority)
    if args.date is not None and args.bucket is not None:
        raise TicketUXError(
            "--date and --bucket are mutually exclusive (배치는 정확히 하나)",
            example="orthus ticket set <id> --date tomorrow",
        )
    if args.date is not None:
        payload["scheduled_date"] = parse_date_word(args.date)
        payload["backlog_bucket_id"] = None
    if args.bucket is not None:
        payload["backlog_bucket_id"] = str(resolve_bucket(client, args.bucket)["bucket_id"])
        payload["scheduled_date"] = None
    if args.channel is not None:
        if args.channel.strip().lower() == "none":
            payload["project_id"] = None
        else:
            payload["project_id"] = str(resolve_project(client, args.channel)["project_id"])
    if args.due is not None:
        payload["due_date"] = (
            None if args.due.strip().lower() == "none" else parse_date_word(args.due)
        )
    if args.note is not None:
        payload["note"] = args.note
    return payload


def cmd_ticket_set(args: argparse.Namespace) -> int:
    from orthus.mcp.tickets import build_board_row_props

    client = _central_client(args)
    try:
        kind, schema, obj = resolve_any_ticket(
            client, args.ticket_id, project=args.project, board=getattr(args, "board", None)
        )
        if kind == "board":
            for flag, name in ((args.assignee, "--assignee"), (args.prop, "--prop")):
                if flag:
                    raise TicketUXError(
                        f"{name}은(는) 칸반 카드 전용 필드 — 이 id는 내 보드 티켓",
                        example="orthus ticket set <id> --status done",
                    )
            payload = _board_ticket_patch(args, client)
            if not payload:
                raise TicketUXError(
                    "nothing to set (pass at least one field flag)",
                    example="orthus ticket set <id> --status done",
                )
            updated = client.board_task_update(str(obj["task_id"]), payload)
            if _json_requested(args):
                _emit_json({"ok": True, "kind": "board", "ticket": updated})
            else:
                print(f"OK set {_ticket_line(updated)}")
            return 0
        # 칸반 카드.
        kanban_note = {
            "--date": "칸반 카드의 날짜는 --due(마감/종료일)뿐 — 컬럼 배치는 --status",
            "--bucket": "칸반은 백로그 버킷 대신 상태 컬럼 — --status 시작전",
            "--channel": "칸반 카드는 이미 프로젝트 소속 — 옮기기는 웹에서만",
            "--note": "칸반 카드의 자유 기록은 본문(body) — 웹에서 편집",
        }
        for flag, name in (
            (args.date, "--date"),
            (args.bucket, "--bucket"),
            (args.channel, "--channel"),
            (args.note, "--note"),
        ):
            if flag is not None:
                raise TicketUXError(
                    f"{name}은(는) 내 보드 티켓 전용 필드 — 이 id는 칸반 카드",
                    example="orthus ticket set <id> --status 진행중",
                    note=kanban_note[name],
                )
        if not any((args.title, args.status, args.assignee, args.priority, args.due, args.prop)):
            raise TicketUXError(
                "nothing to set (pass at least one field flag)",
                example="orthus ticket set <id> --status 진행중",
            )
        # 서버 row PATCH는 props 전체 교체 — 기존 props를 base로 병합해 보낸다.
        props = build_board_row_props(
            client,
            schema,
            title=args.title,
            status=args.status,
            assignee=args.assignee,
            priority=args.priority,
            due=args.due,
            sets=list(args.prop),
            base=obj.get("props") or {},
        )
        updated = client.database_row_update(
            str(schema["database_id"]), str(obj["row_id"]), {"props": props}
        )
        if _json_requested(args):
            _emit_json({"ok": True, "kind": "kanban", "card": updated})
        else:
            title_prop = board_prop(schema, prop_type="title")
            title = (updated.get("props") or {}).get(str(title_prop["id"])) if title_prop else ""
            changed = ", ".join(
                part
                for part, flag in (
                    (f"status→{args.status}", args.status),
                    (f"title→{args.title}", args.title),
                    (f"assignee→{args.assignee}", args.assignee),
                    (f"priority→{args.priority}", args.priority),
                    (f"due→{args.due}", args.due),
                    (", ".join(args.prop), args.prop),
                )
                if flag
            )
            print(f"OK set {short_id(updated['row_id'])}  {title}  ({changed})")
    except TicketUXError as exc:
        return _emit_ticket_ux_error(args, exc)
    except CentralError as exc:
        return _emit_ticket_central_error(args, exc)
    return 0


def cmd_ticket_rm(args: argparse.Namespace) -> int:
    """soft rm — 내 보드 티켓은 archived로(복구 가능). 실삭제 경로는 만들지 않는다."""
    client = _central_client(args)
    try:
        kind, schema, obj = resolve_any_ticket(
            client, args.ticket_id, project=args.project, board=getattr(args, "board", None)
        )
        if kind == "kanban":
            raise TicketUXError(
                "칸반 카드 삭제는 웹(세션) 전용 — 치우려면 보류/완료 컬럼으로 옮겨라",
                example=f"orthus ticket set {short_id(obj['row_id'])} --status 보류",
            )
        updated = client.board_task_update(str(obj["task_id"]), {"status": "archived"})
    except TicketUXError as exc:
        return _emit_ticket_ux_error(args, exc)
    except CentralError as exc:
        return _emit_ticket_central_error(args, exc)
    if _json_requested(args):
        _emit_json({"ok": True, "kind": "board", "ticket": updated})
    else:
        print(
            f"OK archived {short_id(updated['task_id'])}  {updated.get('title', '')}"
            f"  (복구: orthus ticket set {short_id(updated['task_id'])} --status open)"
        )
    return 0


def cmd_ticket_comment(args: argparse.Namespace) -> int:
    client = _central_client(args)
    try:
        kind, _schema, obj = resolve_any_ticket(
            client, args.ticket_id, project=args.project, board=getattr(args, "board", None)
        )
        if kind == "kanban":
            raise TicketUXError(
                "칸반 카드에는 댓글이 없다 — 본문에 기록하려면 웹에서, 진행 기록은 내 보드 티켓으로",
                example='orthus ticket add "제목" --note "진행 기록"',
            )
        comment = client.board_task_comment_add(str(obj["task_id"]), args.body)
    except TicketUXError as exc:
        return _emit_ticket_ux_error(args, exc)
    except CentralError as exc:
        return _emit_ticket_central_error(args, exc)
    if _json_requested(args):
        _emit_json({"ok": True, "comment": comment})
    else:
        print(f"OK comment on {short_id(obj['task_id'])}  {obj.get('title', '')}")
    return 0


def cmd_ticket_projects(args: argparse.Namespace) -> int:
    client = _central_client(args)
    try:
        projects = client.board_projects() or []
        buckets = client.board_backlog_buckets() or []
    except CentralError as exc:
        return _emit_ticket_central_error(args, exc)
    if _json_requested(args):
        _emit_json({"projects": projects, "buckets": buckets})
        return 0
    for project in projects:
        kind = project.get("kind", "personal")
        label = " (company — 팀 전체에게 보임, --project 사용 가능)" if kind == "company" else ""
        print(f"project  {project.get('name', '')}{label}")
    for bucket in buckets:
        print(f"bucket   {bucket.get('key', '')}  {bucket.get('label', '')}")
    if not projects and not buckets:
        print("(no channels)")
    return 0


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    from orthus.mcp.server import main as mcp_main

    with _central_url_env_fallback(args, CENTRAL_URL_ENV):
        mcp_main()
    return 0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _package_version() -> str:
    try:
        return metadata.version("orthus")
    except metadata.PackageNotFoundError:
        # Uninstalled source checkout (e.g. `python -m orthus.cli` without a build):
        # VERSION is the release SoR that pyproject's version is derived from, so
        # read it directly rather than the now-dynamic pyproject.
        try:
            version = (_repo_root() / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown"
        return version or "unknown"


def _version_payload() -> dict[str, Any]:
    package_version = _package_version()
    return {
        "cli": {"package": "orthus", "version": package_version},
        "mcp": {"command": "orthus-mcp", "version": package_version},
    }


def cmd_version(args: argparse.Namespace) -> int:
    payload = _version_payload()
    if _json_requested(args):
        _emit_json(payload)
    else:
        print(f"cli: {payload['cli']['version']} ({payload['cli']['package']})")
        print(f"mcp: {payload['mcp']['version']} ({payload['mcp']['command']})")
    return 0


def _mcp_server_command() -> tuple[str, list[str]]:
    # Prefer the `orthus-mcp` entrypoint installed next to the current Python
    # (venv/uv-tool bin); otherwise fall back to running the module directly.
    candidate = Path(sys.executable).with_name("orthus-mcp")
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate), []

    return sys.executable, ["-m", "orthus.mcp"]


def _current_cli_executable() -> Path:
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        argv0_path = Path(argv0)
        if argv0_path.is_absolute() or len(argv0_path.parts) > 1:
            return argv0_path.expanduser().resolve(strict=False)
        resolved = shutil.which(argv0)
        if resolved:
            return Path(resolved).resolve(strict=False)
    return Path(sys.executable).resolve(strict=False)


def _uv_tool_root() -> Path | None:
    """Return the uv-tool venv root iff this CLI runs from a ``uv tool install``.

    uv installs each tool into its own venv under ``uv tool dir`` and drops a
    ``uv-receipt.toml`` at the venv root (== ``sys.prefix``). A plain ``uv sync``
    or pip venv never has that receipt, so its presence is a robust signal — no
    path-shape guessing against XDG/platform variance.
    """
    prefix = Path(sys.prefix).resolve(strict=False)
    return prefix if (prefix / "uv-receipt.toml").is_file() else None


def _path_has_local_bin(home: Path, path_env: str | None = None) -> bool:
    local_bin = home.joinpath(".local", "bin").resolve(strict=False)
    raw_path = os.environ.get("PATH", "") if path_env is None else path_env
    for entry in raw_path.split(os.pathsep):
        if not entry:
            continue
        entry_path = Path(entry.replace("$HOME", str(home))).expanduser()
        if not entry_path.is_absolute():
            entry_path = home / entry_path
        if entry_path.resolve(strict=False) == local_bin:
            return True
    return False


def _cli_doctor_status(
    *,
    home: Path | None = None,
    path_env: str | None = None,
    current_executable: Path | None = None,
) -> dict[str, Any]:
    resolved_home = (home or Path.home()).expanduser().resolve(strict=False)
    symlink_path = resolved_home.joinpath(".local", "bin", "orthus")
    current = (current_executable or _current_cli_executable()).resolve(strict=False)

    symlink_target: str | None = None
    symlink_target_resolved: Path | None = None
    non_symlink_path = False
    status = "unavailable"

    if symlink_path.is_symlink():
        raw_target = os.readlink(symlink_path)
        symlink_target = raw_target
        target_path = Path(raw_target)
        if not target_path.is_absolute():
            target_path = symlink_path.parent / target_path
        symlink_target_resolved = target_path.resolve(strict=False)
        if not target_path.exists():
            status = "broken"
        else:
            status = "installed" if target_path.name == "orthus" else "mismatch"
    elif symlink_path.exists():
        non_symlink_path = True
        status = "mismatch"

    return {
        "current_executable": str(current),
        "expected_symlink_path": str(symlink_path),
        "symlink_target": symlink_target,
        "symlink_target_resolved": (
            str(symlink_target_resolved) if symlink_target_resolved is not None else None
        ),
        "status": status,
        "broken": status == "broken",
        "non_symlink_path": non_symlink_path,
        "path_has_local_bin": _path_has_local_bin(resolved_home, path_env),
        "path_snippet": LOCAL_BIN_SNIPPET,
    }


async def _run_mcp_smoke_async() -> tuple[int, str, str]:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except Exception as exc:  # noqa: BLE001
        return 1, "", f"failed to import MCP client: {type(exc).__name__}: {exc}"

    command, server_args = _mcp_server_command()
    try:
        params = StdioServerParameters(command=command, args=server_args)
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
    except Exception as exc:  # noqa: BLE001
        return 1, "", f"mcp-smoke failed: {type(exc).__name__}: {exc}"

    names = {tool.name for tool in result.tools}
    missing = EXPECTED_MCP_TOOLS - names
    extra = names - EXPECTED_MCP_TOOLS
    if missing or extra:
        return 1, "", f"mcp-smoke FAIL: missing={sorted(missing)} unexpected={sorted(extra)}"
    return 0, f"mcp-smoke OK: orthus-mcp exposed {len(names)} tools over stdio", ""


def _run_mcp_smoke() -> subprocess.CompletedProcess[str]:
    returncode, stdout, stderr = asyncio.run(_run_mcp_smoke_async())
    command, server_args = _mcp_server_command()
    return subprocess.CompletedProcess(
        [command, *server_args],
        returncode,
        stdout=f"{stdout}\n" if stdout else "",
        stderr=f"{stderr}\n" if stderr else "",
    )


def cmd_mcp_smoke(args: argparse.Namespace) -> int:
    completed = _run_mcp_smoke()
    payload = {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }
    if _json_requested(args):
        _emit_json(payload)
    else:
        if completed.stdout:
            sys.stdout.write(_redact(completed.stdout))
        if completed.stderr:
            sys.stderr.write(_redact(completed.stderr))
    return completed.returncode


def _central_url_for_snippet(args: argparse.Namespace) -> str:
    return _resolved_central_url(args) or "<central-api-url>"


def _claude_config(args: argparse.Namespace) -> dict:
    return {
        "mcpServers": {
            "orthus": {
                "command": "orthus",
                "args": ["mcp", "serve"],
                "env": {CENTRAL_URL_ENV: _central_url_for_snippet(args)},
            }
        }
    }


def _codex_config_toml(args: argparse.Namespace) -> str:
    central_url = _central_url_for_snippet(args)
    return "\n".join(
        [
            "[mcp_servers.orthus]",
            'command = "orthus"',
            'args = ["mcp", "serve"]',
            f'env = {{ {CENTRAL_URL_ENV} = "{central_url}" }}',
            f"# token: macOS Keychain service '{KEYCHAIN_SERVICE}'",
        ]
    )


def cmd_mcp_config(args: argparse.Namespace) -> int:
    if args.target == "claude":
        payload: Any = {
            "provider": "claude",
            "config": _claude_config(args),
            "token_source": f"macOS Keychain service '{KEYCHAIN_SERVICE}'",
        }
        if _json_requested(args):
            _emit_json(payload)
        else:
            _emit_human(payload["config"])
            print(f"Token source: {payload['token_source']}")
    else:
        payload = {
            "provider": "codex",
            "config_toml": _codex_config_toml(args),
            "token_source": f"macOS Keychain service '{KEYCHAIN_SERVICE}'",
        }
        if _json_requested(args):
            _emit_json(payload)
        else:
            print(_redact(payload["config_toml"]))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    central_url = _resolved_central_url(args)
    token_present = bool(resolve_token())
    payload: dict[str, Any] = {
        "ok": True,
        "central": {
            "url_configured": bool(central_url),
            "url": central_url or None,
            "token_present": token_present,
            "reachable": False,
            "error": None,
        },
        "mcp": {
            "smoke_ok": False,
            "smoke_returncode": None,
            "smoke_stdout": None,
            "smoke_stderr": None,
        },
        "cli": _cli_doctor_status(),
    }
    exit_code = 0

    if not central_url or not token_present:
        exit_code = 2
        payload["central"]["error"] = (
            f"{CENTRAL_URL_ENV} is not set"
            if not central_url
            else f"knowledge token not found in env or Keychain service '{KEYCHAIN_SERVICE}'"
        )
    else:
        try:
            _central_client(args).wiki_search("doctor", scope="all", limit=1)
            payload["central"]["reachable"] = True
        except CentralError as exc:
            payload["central"]["error"] = str(exc)
            exit_code = _central_exit_code(exc)

    completed = _run_mcp_smoke()
    payload["mcp"].update(
        {
            "smoke_ok": completed.returncode == 0,
            "smoke_returncode": completed.returncode,
            "smoke_stdout": completed.stdout.strip(),
            "smoke_stderr": completed.stderr.strip(),
        }
    )
    if completed.returncode != 0 and exit_code == 0:
        exit_code = 1

    payload["ok"] = exit_code == 0
    _emit(args, payload)
    return exit_code


def _store_keychain_secret(service: str, token: str) -> None:
    account = getpass.getuser()
    try:
        completed = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                service,
                "-a",
                account,
                "-w",
                token,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"macOS security command not found; cannot store Keychain service '{service}'"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"macOS security command unavailable; cannot store Keychain service '{service}'"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"security failed storing Keychain service '{service}' (exit {completed.returncode})"
        )


def cmd_init(args: argparse.Namespace) -> int:
    central_url = (
        getattr(args, "init_central_url", None)
        or getattr(args, "central_url", None)
        or os.environ.get(CENTRAL_URL_ENV, "").strip()
        or _configured_central_url()
    ).rstrip("/")
    if not central_url:
        _emit_usage_error(args, f"--central-url or {CENTRAL_URL_ENV} is required")
        return 2

    config_path = _cli_config_path()
    payload: dict[str, Any] = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "config": {
            "path": str(config_path),
            "central_url": central_url,
            "would_write": bool(args.dry_run),
            "written": False,
        },
        "keychain": {
            "mcp_token": "not_provided",
            "collector_token": "not_provided",
        },
    }

    if args.mcp_token and args.mcp_token_stdin:
        _emit_usage_error(args, "--mcp-token and --mcp-token-stdin are mutually exclusive")
        return 2
    if args.collector_token and args.collector_token_stdin:
        _emit_usage_error(
            args, "--collector-token and --collector-token-stdin are mutually exclusive"
        )
        return 2

    mcp_requested = bool(args.mcp_token) or args.mcp_token_stdin
    collector_requested = bool(args.collector_token) or args.collector_token_stdin
    if mcp_requested:
        payload["keychain"]["mcp_token"] = "would_store" if args.dry_run else "stored"
    if collector_requested:
        payload["keychain"]["collector_token"] = "would_store" if args.dry_run else "stored"

    if args.dry_run:
        _emit(args, payload)
        return 0

    # Read stdin-flagged tokens via getpass so the secret never lands in argv
    # (shell history / `ps`); the explicit `--*-token` args stay for scripts.
    if args.mcp_token_stdin:
        mcp_token = getpass.getpass("MCP token: ").strip()
        if not mcp_token:
            _emit_usage_error(args, "--mcp-token-stdin received an empty token")
            return 2
    else:
        mcp_token = args.mcp_token
    if mcp_token:
        _store_keychain_secret(KEYCHAIN_SERVICE, mcp_token)

    if args.collector_token_stdin:
        collector_token = getpass.getpass("Collector token (dct_…): ").strip()
        if not collector_token:
            _emit_usage_error(args, "--collector-token-stdin received an empty token")
            return 2
    else:
        collector_token = args.collector_token
    if collector_token:
        _store_keychain_secret(COLLECTOR_KEYCHAIN_SERVICE, collector_token)

    config = _read_cli_config(config_path)
    config["central_url"] = central_url
    _write_cli_config(config, config_path)
    payload["config"]["written"] = True
    payload["config"]["would_write"] = False

    _emit(args, payload)
    return 0


# --- `orthus connect` — browser-login one-shot token handoff (PR-O2) -----------
#
# Security model: the CLI binds an ephemeral loopback-ONLY HTTP server
# (127.0.0.1, port 0), opens the logged-in central page
# `{central}/connect/cli?state=...&port=...&name=...`, and that page — on the
# user's click — redirects the browser to
# `http://127.0.0.1:{port}/callback?state=...&token=dct_...`. The random
# `state` (secrets.token_urlsafe(24)) binds the callback to THIS CLI run, so a
# local process that guesses the port cannot inject a token without the state.
# The token travels browser→loopback once, is stored straight into the Keychain
# and is never printed, logged, or placed in argv.

_CONNECT_TOKEN_PREFIX = "dct_"
_CONNECT_DEFAULT_TIMEOUT_S = 300.0


def _connect_html(title: str, detail: str) -> str:
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        f"<title>orthus connect</title></head>"
        '<body style="font-family:-apple-system,sans-serif;text-align:center;'
        'padding-top:4rem">'
        f"<h2>{title}</h2><p>{detail}</p></body></html>"
    )


class _ConnectCallbackHandler(http.server.BaseHTTPRequestHandler):
    server_version = "orthus-connect"
    sys_version = ""

    def _respond(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        parsed = urlsplit(self.path)
        if parsed.path != "/callback":
            self._respond(404, _connect_html("잘못된 경로", "이 창을 닫아도 됩니다."))
            return
        params = parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        token = (params.get("token") or [""])[0]
        if not state or state != self.server.expected_state:
            # Wrong/forged state: reject but KEEP WAITING for the real callback.
            self._respond(
                400,
                _connect_html(
                    "state 불일치",
                    "이 창을 닫고 터미널에서 `orthus connect` 를 다시 실행하세요.",
                ),
            )
            return
        if not token.startswith(_CONNECT_TOKEN_PREFIX):
            self._respond(
                400,
                _connect_html(
                    "토큰 형식 오류",
                    "dct_… 형식의 collector 토큰이 필요합니다. 다시 시도하세요.",
                ),
            )
            return
        self.server.received_token = token
        self._respond(
            200,
            _connect_html("연결 완료 — 이 창을 닫아도 됩니다", "터미널로 돌아가 계속 진행하세요."),
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # NEVER log the request line — the callback query string carries the token.
        return


class _ConnectCallbackServer(http.server.HTTPServer):
    """Loopback-only one-shot callback receiver. Binds 127.0.0.1 port 0."""

    def __init__(self, expected_state: str):
        super().__init__(("127.0.0.1", 0), _ConnectCallbackHandler)
        self.expected_state = expected_state
        self.received_token: str | None = None


def _open_browser(url: str) -> bool:
    try:
        completed = subprocess.run(["open", url], capture_output=True, check=False)
    except (FileNotFoundError, OSError):
        return False
    return completed.returncode == 0


def _connect_manual_hint(central_url: str) -> str:
    return (
        "수동 경로 (구버전 central / 페이지 404): central 웹 /connectors 의 '연결 준비'에서 "
        "토큰을 발급한 뒤\n"
        f"  orthus init --central-url {central_url or '<central-url>'} "
        "--mcp-token-stdin --collector-token-stdin"
    )


def run_connect_flow(
    *,
    central_url: str,
    timeout: float = _CONNECT_DEFAULT_TIMEOUT_S,
    no_browser: bool = False,
) -> str | None:
    """Open the central connect page and wait for the loopback callback.

    Returns the received ``dct_…`` token, or None on timeout. Does NOT store or
    print the token — callers persist it via ``_store_connect_token``.
    """
    state = secrets.token_urlsafe(24)
    # config의 central_url은 API 베이스(`…/api`)일 수 있지만 /connect/cli 는
    # 웹 페이지라 프리픽스 밖에 있다 — 페이지 URL 조립 시에만 strip.
    page_base = central_url.rstrip("/").removesuffix("/api")
    with _ConnectCallbackServer(state) as server:
        port = server.server_address[1]
        url = (
            f"{page_base}/connect/cli?state={quote(state)}&port={port}"
            f"&name={quote(socket.gethostname())}"
        )
        if no_browser:
            print("브라우저에서 이 URL 을 여세요 (central 에 로그인된 상태로):")
            print(f"  {url}")
        elif _open_browser(url):
            print("브라우저를 열었습니다 — central 로그인 후 페이지의 연결 버튼을 누르세요.")
            print(f"  {url}")
        else:
            print("브라우저 자동 열기 실패 — 직접 여세요:")
            print(f"  {url}")
        print(
            f"콜백 대기 중… (127.0.0.1:{port}, 최대 {int(timeout)}초 — "
            "페이지가 404 라면 구버전 central 입니다; 타임아웃 후 수동 경로를 안내합니다)"
        )
        server.timeout = 1.0
        deadline = time.monotonic() + timeout
        while server.received_token is None and time.monotonic() < deadline:
            server.handle_request()
        return server.received_token


def _store_connect_token(token: str, central_url: str) -> None:
    """Persist a connect token: SAME token into both Keychain services (the
    central-issued dct_ token carries both knowledge+collector scopes) + the
    central_url into the CLI config — exactly what ``orthus init`` does."""
    _store_keychain_secret(KEYCHAIN_SERVICE, token)
    _store_keychain_secret(COLLECTOR_KEYCHAIN_SERVICE, token)
    config = _read_cli_config()
    config["central_url"] = central_url
    _write_cli_config(config)


def cmd_connect(args: argparse.Namespace) -> int:
    central_url = (
        getattr(args, "connect_central_url", None)
        or os.environ.get(CENTRAL_URL_ENV, "").strip()
        or _configured_central_url()
    ).rstrip("/")
    if not central_url:
        _emit_usage_error(
            args,
            "central URL 이 필요합니다: orthus connect --central-url https://…",
        )
        return 2

    token = run_connect_flow(
        central_url=central_url,
        timeout=args.timeout,
        no_browser=args.no_browser,
    )
    if token is None:
        print("⏱️  시간 초과 — 토큰을 받지 못했습니다.")
        print(_connect_manual_hint(central_url))
        return 1

    _store_connect_token(token, central_url)
    _emit(
        args,
        {
            "ok": True,
            "central_url": central_url,
            # masked on purpose — the real token lives only in the Keychain
            "token": "dct_****",
            "scopes": "central 연결 페이지가 부여한 knowledge+collector scope",
            "keychain": {
                "mcp_token": f"stored ({KEYCHAIN_SERVICE})",
                "collector_token": f"stored ({COLLECTOR_KEYCHAIN_SERVICE})",
            },
        },
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orthus")
    parser.add_argument("--json", action="store_true", dest="global_json")
    parser.add_argument("--central-url", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    version = sub.add_parser("version", help="print orthus CLI version")
    version.add_argument("--json", action="store_true")
    version.set_defaults(func=cmd_version)

    init = sub.add_parser("init", help="configure local Orthus CLI")
    init.add_argument("--central-url", dest="init_central_url", default=None)
    init.add_argument("--mcp-token", default=None)
    init.add_argument("--mcp-token-stdin", action="store_true")
    init.add_argument("--collector-token", default=None)
    init.add_argument("--collector-token-stdin", action="store_true")
    init.add_argument("--dry-run", action="store_true")
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=cmd_init)

    connect = sub.add_parser(
        "connect", help="브라우저 로그인으로 central 연결 — 토큰 자동 발급 → Keychain 저장"
    )
    connect.add_argument("--central-url", dest="connect_central_url", default=None)
    connect.add_argument(
        "--timeout", type=float, default=_CONNECT_DEFAULT_TIMEOUT_S, help="콜백 대기 초 (기본 300)"
    )
    connect.add_argument(
        "--no-browser", action="store_true", help="브라우저를 열지 않고 URL 만 출력"
    )
    connect.add_argument("--json", action="store_true")
    connect.set_defaults(func=cmd_connect)

    wiki = sub.add_parser("wiki", help="central wiki commands")
    wiki_sub = wiki.add_subparsers(dest="wiki_command", required=True)

    search = wiki_sub.add_parser("search", help="search compiled wiki pages")
    search.add_argument("query")
    search.add_argument("--scope", default="all")
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=cmd_wiki_search)

    page = wiki_sub.add_parser("page", help="read compiled wiki page")
    page.add_argument("slug")
    page.set_defaults(func=cmd_wiki_page)

    ask = wiki_sub.add_parser("ask", help="ask grounded wiki question")
    ask.add_argument("question")
    ask.add_argument("--scope", default="all")
    ask.add_argument("--context-wiki-slug", default=None)
    ask.set_defaults(func=cmd_wiki_ask)

    suggest = wiki_sub.add_parser("suggest", help="submit wiki update candidate")
    suggest.add_argument("slug")
    suggest.add_argument("--note", required=True)
    suggest.add_argument("--evidence-url", action="append", default=[])
    suggest.set_defaults(func=cmd_wiki_suggest)

    work = sub.add_parser("work", help="read Agent Work")
    work_sub = work.add_subparsers(dest="work_command", required=True)

    list_cmd = work_sub.add_parser("list", help="list Agent Work")
    list_cmd.add_argument("--state", default=None)
    list_cmd.add_argument("--limit", type=int, default=50)
    list_cmd.set_defaults(func=cmd_work_list)

    show = work_sub.add_parser("show", help="show Agent Work item")
    show.add_argument("work_id")
    show.set_defaults(func=cmd_work_show)

    mcp = sub.add_parser("mcp", help="MCP server helpers")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)

    serve = mcp_sub.add_parser("serve", help="start stdio MCP server")
    serve.set_defaults(func=cmd_mcp_serve)

    smoke = mcp_sub.add_parser("smoke", help="run stdio MCP smoke check")
    smoke.set_defaults(func=cmd_mcp_smoke)

    config = mcp_sub.add_parser("config", help="print MCP client config snippet")
    config.add_argument("target", choices=["claude", "codex"])
    config.set_defaults(func=cmd_mcp_config)

    doctor = sub.add_parser("doctor", help="check local Orthus CLI/MCP setup")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    connector = sub.add_parser("connector", help="manage owner personal connectors on central")
    connector_sub = connector.add_subparsers(dest="connector_command", required=True)

    connector_list = connector_sub.add_parser("list", help="list personal connectors + accounts")
    connector_list.set_defaults(func=cmd_connector_list)

    connector_show = connector_sub.add_parser("show", help="show one connector config + status")
    connector_show.add_argument("slug")
    connector_show.set_defaults(func=cmd_connector_show)

    connector_config = connector_sub.add_parser("config", help="configure a personal connector")
    connector_config.add_argument("slug")
    connector_config.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="non-secret setting (repeatable)",
    )
    connector_config.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="KEY",
        help="secret field; value is prompted hidden (repeatable)",
    )
    connector_config.add_argument("--label", default=None)
    connector_config.set_defaults(func=cmd_connector_config)

    connector_ensure = connector_sub.add_parser("ensure", help="ensure default connector account")
    connector_ensure.add_argument("slug")
    connector_ensure.set_defaults(func=cmd_connector_ensure)

    connector_delete = connector_sub.add_parser("delete", help="delete a connector account")
    connector_delete.add_argument("slug")
    connector_delete.add_argument("--account-id", default=None)
    connector_delete.set_defaults(func=cmd_connector_delete)

    calendar = sub.add_parser("calendar", help="company team calendar (team schedule)")
    calendar_sub = calendar.add_subparsers(dest="calendar_command", required=True)

    def _add_calendar_event_flags(p: argparse.ArgumentParser, *, require: bool) -> None:
        p.add_argument("--title", required=require)
        p.add_argument("--date", required=require, help="event_date YYYY-MM-DD")
        p.add_argument(
            "--all-day", dest="all_day", action=argparse.BooleanOptionalAction, default=None
        )
        p.add_argument("--end-date", dest="end_date", default=None, help="YYYY-MM-DD")
        p.add_argument("--start-time", dest="start_time", default=None, help="HH:MM")
        p.add_argument("--end-time", dest="end_time", default=None, help="HH:MM")
        p.add_argument("--event-type", dest="event_type", default=None)
        p.add_argument("--location", default=None)
        p.add_argument("--description", default=None)
        p.add_argument(
            "--member",
            action="append",
            default=[],
            metavar="MEMBER_ID",
            help="team member UUID (repeatable)",
        )
        p.add_argument("--color", default=None)

    cal_add = calendar_sub.add_parser("add", help="add a team calendar event")
    _add_calendar_event_flags(cal_add, require=True)
    cal_add.set_defaults(func=cmd_calendar_add)

    cal_update = calendar_sub.add_parser("update", help="update a team calendar event")
    cal_update.add_argument("event_id")
    _add_calendar_event_flags(cal_update, require=False)
    cal_update.set_defaults(func=cmd_calendar_update)

    cal_delete = calendar_sub.add_parser("delete", help="delete a team calendar event")
    cal_delete.add_argument("event_id")
    cal_delete.set_defaults(func=cmd_calendar_delete)

    cal_list = calendar_sub.add_parser("list", help="list team calendar events")
    cal_list.add_argument("--since", default=None, help="from YYYY-MM-DD")
    cal_list.add_argument("--until", default=None, help="to YYYY-MM-DD")
    cal_list.set_defaults(func=cmd_calendar_list)

    cal_members = calendar_sub.add_parser("members", help="team members")
    cal_members_sub = cal_members.add_subparsers(dest="calendar_members_command", required=True)
    cal_members_list = cal_members_sub.add_parser("list", help="list team members")
    cal_members_list.set_defaults(func=cmd_calendar_members_list)
    cal_members_add = cal_members_sub.add_parser("add", help="add a team member")
    cal_members_add.add_argument("--name", required=True)
    cal_members_add.add_argument("--title", default=None)
    cal_members_add.add_argument("--department", default=None)
    cal_members_add.add_argument("--email", default=None)
    cal_members_add.add_argument("--phone", default=None)
    cal_members_add.set_defaults(func=cmd_calendar_members_add)

    myschedule = sub.add_parser("myschedule", help="my personal schedule (owner-private)")
    myschedule_sub = myschedule.add_subparsers(dest="myschedule_command", required=True)

    my_add = myschedule_sub.add_parser("add", help="add a personal schedule event")
    my_add.add_argument("--title", required=True)
    my_add.add_argument("--start", required=True, help="starts_at ISO-8601")
    my_add.add_argument("--end", required=True, help="ends_at ISO-8601")
    my_add.add_argument("--project-id", dest="project_id", default=None)
    my_add.add_argument("--source-label", dest="source_label", default=None)
    my_add.set_defaults(func=cmd_myschedule_add)

    my_update = myschedule_sub.add_parser("update", help="update a personal schedule event")
    my_update.add_argument("event_id")
    my_update.add_argument("--title", default=None)
    my_update.add_argument("--start", default=None, help="starts_at ISO-8601")
    my_update.add_argument("--end", default=None, help="ends_at ISO-8601")
    my_update.add_argument("--project-id", dest="project_id", default=None)
    my_update.set_defaults(func=cmd_myschedule_update)

    my_list = myschedule_sub.add_parser("list", help="list my personal schedule")
    my_list.add_argument("--since", default=None, help="from YYYY-MM-DD")
    my_list.add_argument("--until", default=None, help="to YYYY-MM-DD")
    my_list.set_defaults(func=cmd_myschedule_list)

    ticket = sub.add_parser(
        "ticket",
        help="티켓 (이슈 트래킹) — 인자 없이 실행하면 요약 도움말",
        description="티켓 CRUD. 기본은 내 보드, --project를 주면 그 프로젝트 칸반."
        " id는 앞 4자 이상 prefix면 충분하고 저장소(내 보드/칸반)를 몰라도 된다."
        " rm은 archive(복구 가능)다 — 실삭제 경로는 없다.",
    )
    ticket.set_defaults(func=cmd_ticket_overview)
    ticket_sub = ticket.add_subparsers(dest="ticket_command")

    date_help = "today | tomorrow | +3d | YYYY-MM-DD"
    priority_help = f"{' | '.join(TICKET_PRIORITIES)} (첫 글자 축약 가능: u/p/n/l)"
    id_help = "티켓/카드 id — 앞 4자 이상 prefix (orthus ticket ls로 확인)"
    project_help = "회사 프로젝트 이름 → 그 프로젝트 칸반(업무 보드) 대상"

    t_ls = ticket_sub.add_parser(
        "ls",
        help="티켓 목록 — 기본 내 보드, --project는 그 칸반",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  orthus ticket ls                            # 내 보드 열린 티켓\n"
            '  orthus ticket ls --project "탕수육 레시피"    # 프로젝트 칸반 컬럼별\n'
            "  orthus ticket ls --status all --json"
        ),
    )
    t_ls.add_argument("--project", default=None, help=project_help)
    t_ls.add_argument(
        "--status",
        default="open",
        help=f"{' | '.join(TICKET_STATUSES)} | all (내 보드 전용, default: open)",
    )
    t_ls.add_argument("--channel", default=None, help="내 보드 채널 이름으로 필터")
    t_ls.add_argument("--date", default=None, help=f"해당 날짜만 (내 보드): {date_help}")
    t_ls.add_argument("--limit", type=_positive_int, default=50)
    t_ls.add_argument("--board", default=None, help="칸반 보드가 여럿일 때 이름으로 지정")
    t_ls.add_argument("--json", action="store_true")
    t_ls.set_defaults(func=cmd_ticket_ls)

    t_add = ticket_sub.add_parser(
        "add",
        help="티켓 생성 — 기본 내 보드(오늘), --project는 칸반 카드(첫 컬럼)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "내 보드: --date|--bucket 중 하나(기본 today), --priority, --channel(회사 채널 지정"
            " 시 팀 공개), --due, --note, --id(멱등 재시도 키).\n"
            "칸반(--project): --status(옵션 이름, 기본 첫 컬럼), --assignee(팀 멤버 이름),"
            " --due, --prop 이름=값, --body(markdown).\n"
            "examples:\n"
            '  orthus ticket add "로그인 500 조사" --priority u --channel nova\n'
            '  orthus ticket add "경쟁사 분석" --project "탕수육 레시피" --assignee Jaden'
            " --prop 우선순위=높음 --due +6d"
        ),
    )
    t_add.add_argument("title")
    t_add.add_argument("--project", default=None, help=project_help)
    t_add.add_argument(
        "--status",
        default=None,
        help="칸반 컬럼 (--project 필요): 옵션 이름(공백 무시) 또는 open/done 등 영어 자동 매핑",
    )
    t_add.add_argument("--assignee", default=None, help="칸반 담당자 — 팀 멤버 이름")
    t_add.add_argument(
        "--priority",
        default=None,
        help=f"{priority_help} — 칸반이면 그 보드의 우선순위 옵션으로 자동 매핑",
    )
    t_add.add_argument("--date", default=None, help=f"내 보드 배치: {date_help} (기본 today)")
    t_add.add_argument(
        "--bucket",
        default=None,
        help="내 보드 백로그 키: next_week | next_month | next_quarter | next_year | someday | never",
    )
    t_add.add_argument("--channel", default=None, help="내 보드 채널(회사 채널이면 팀 공개)")
    t_add.add_argument("--due", default=None, help=f"마감일: {date_help}")
    t_add.add_argument("--note", default=None, help="내 보드 티켓 노트")
    t_add.add_argument("--body", default=None, help="칸반 카드 본문 (markdown)")
    t_add.add_argument(
        "--prop", action="append", default=[], metavar="이름=값", help="칸반 임의 속성 (repeatable)"
    )
    t_add.add_argument("--id", dest="task_id", default=None, help="멱등 재시도용 클라이언트 UUID")
    t_add.add_argument("--board", default=None, help="칸반 보드가 여럿일 때 이름으로 지정")
    t_add.add_argument("--json", action="store_true")
    t_add.set_defaults(func=cmd_ticket_add)

    t_set = ticket_sub.add_parser(
        "set",
        help="티켓 필드 변경 — 넘긴 플래그만 바뀐다 (상태 이동 포함)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "id가 내 보드 티켓이면: --status open|done|archived, --title, --priority,"
            " --date|--bucket, --channel(이름|none), --due(값|none), --note(''=삭제).\n"
            "id가 칸반 카드면: --status(컬럼 옵션 이름), --title, --assignee, --due,"
            " --prop 이름=값.\n"
            "examples:\n"
            "  orthus ticket set 3fa8c2e1 --status done\n"
            "  orthus ticket set 85c41169 --status 진행중 --assignee Jaden\n"
            '  orthus ticket set 85c4 --prop 우선순위=핫픽스 --project "탕수육 레시피"'
        ),
    )
    t_set.add_argument("ticket_id", help=id_help)
    t_set.add_argument("--project", default=None, help="칸반으로 한정 + 제목으로도 카드 지정 가능")
    t_set.add_argument(
        "--status",
        default=None,
        help="내 보드: open|done|archived · 칸반: 컬럼 옵션 이름 또는 open/done 영어 자동 매핑",
    )
    t_set.add_argument("--title", default=None)
    t_set.add_argument(
        "--priority",
        default=None,
        help=f"{priority_help} — 칸반이면 그 보드의 우선순위 옵션으로 자동 매핑",
    )
    t_set.add_argument("--assignee", default=None, help="칸반 담당자 — 팀 멤버 이름")
    t_set.add_argument("--date", default=None, help=f"내 보드 배치: {date_help}")
    t_set.add_argument("--bucket", default=None, help="내 보드 백로그 키")
    t_set.add_argument("--channel", default=None, help="내 보드 채널 이름, 'none'=해제")
    t_set.add_argument("--due", default=None, help=f"{date_help}, 'none'=해제(내 보드)")
    t_set.add_argument("--note", default=None, help="내 보드 노트, ''=삭제")
    t_set.add_argument(
        "--prop", action="append", default=[], metavar="이름=값", help="칸반 임의 속성 (repeatable)"
    )
    t_set.add_argument("--board", default=None, help="프로젝트에 보드가 여럿일 때 이름으로 지정")
    t_set.add_argument("--json", action="store_true")
    t_set.set_defaults(func=cmd_ticket_set)

    t_rm = ticket_sub.add_parser(
        "rm",
        help="티켓 치우기 (soft) — 내 보드=archived(복구 가능), 칸반=거부+안내",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "실삭제 경로는 없다. 복구: orthus ticket set <id> --status open\n"
            "examples:\n  orthus ticket rm 3fa8c2e1"
        ),
    )
    t_rm.add_argument("ticket_id", help=id_help)
    t_rm.add_argument("--project", default=None, help=project_help)
    t_rm.add_argument("--board", default=None, help="프로젝트에 보드가 여럿일 때 이름으로 지정")
    t_rm.add_argument("--json", action="store_true")
    t_rm.set_defaults(func=cmd_ticket_rm)

    t_show = ticket_sub.add_parser(
        "show",
        help="티켓/카드 상세 (+댓글)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n  orthus ticket show 3fa8c2e1\n  orthus ticket show 85c4 --json",
    )
    t_show.add_argument("ticket_id", help=id_help)
    t_show.add_argument("--project", default=None, help="칸반으로 한정 + 제목으로도 지정 가능")
    t_show.add_argument("--board", default=None, help="프로젝트에 보드가 여럿일 때 이름으로 지정")
    t_show.add_argument("--json", action="store_true")
    t_show.set_defaults(func=cmd_ticket_show)

    t_comment = ticket_sub.add_parser(
        "comment",
        help="내 보드 티켓에 댓글 추가",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='examples:\n  orthus ticket comment 3fa8c2e1 "원인: 세션 쿠키 만료 처리 누락"',
    )
    t_comment.add_argument("ticket_id", help=id_help)
    t_comment.add_argument("body", help="댓글 내용 (4000자 이내)")
    t_comment.add_argument("--project", default=None, help=argparse.SUPPRESS)
    t_comment.add_argument("--board", default=None, help=argparse.SUPPRESS)
    t_comment.add_argument("--json", action="store_true")
    t_comment.set_defaults(func=cmd_ticket_comment)

    t_projects = ticket_sub.add_parser(
        "projects",
        help="채널(프로젝트) + 백로그 버킷 키 목록",
    )
    t_projects.add_argument("--json", action="store_true")
    t_projects.set_defaults(func=cmd_ticket_projects)

    whoami = sub.add_parser("whoami", help="show my identity + node role")
    whoami.add_argument("--json", action="store_true")
    whoami.set_defaults(func=cmd_whoami)

    skills = sub.add_parser("skills", help="bundled agent skills (MCP + CLI usage)")
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    skills_list = skills_sub.add_parser("list", help="list bundled skills")
    skills_list.add_argument("--json", action="store_true")
    skills_list.set_defaults(func=cmd_skills_list)
    skills_get = skills_sub.add_parser("get", help="print a skill's usage doc")
    skills_get.add_argument("name")
    skills_get.add_argument("--full", action="store_true", help="include YAML frontmatter")
    skills_get.add_argument("--json", action="store_true")
    skills_get.set_defaults(func=cmd_skills_get)

    update = sub.add_parser("update", help="self-update the CLI/skills (source checkout)")
    update.add_argument("--json", action="store_true")
    update.set_defaults(func=cmd_update)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CentralError as exc:
        code = _central_exit_code(exc)
        _emit_error(args, exc, code)
        return code
    except subprocess.TimeoutExpired as exc:
        _emit_error(args, RuntimeError(f"process timed out: {exc.cmd}"), 1)
        return 1
    except RuntimeError as exc:
        _emit_error(args, exc, 1)
        return 1
    except OSError as exc:
        _emit_error(args, exc, 1)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
