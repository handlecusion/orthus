"""`orthus connect` loopback callback: state binding, token validation, storage.

Drives the real HTTP handler over a real loopback socket (no browser, no
network beyond 127.0.0.1). The security contract under test: the callback is
accepted ONLY with the exact one-shot `state`, the token must be `dct_…`, the
server binds loopback only, and the token is never printed.
"""

from __future__ import annotations

import http.client
import json
import threading
from urllib.parse import parse_qs, urlsplit

from orthus import cli


def _get(port: int, path: str) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    return resp.status, body


def _serve_one(server: cli._ConnectCallbackServer, path: str) -> tuple[int, str]:
    """Issue one GET from a side thread while the main thread handles it."""
    port = server.server_address[1]
    results: list[tuple[int, str]] = []
    thread = threading.Thread(target=lambda: results.append(_get(port, path)))
    thread.start()
    server.handle_request()
    thread.join(timeout=5)
    assert results, "request thread produced no response"
    return results[0]


def test_server_binds_loopback_only():
    with cli._ConnectCallbackServer("s3cret") as server:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_address[1] > 0  # ephemeral port actually bound


def test_state_mismatch_rejected_and_keeps_waiting():
    with cli._ConnectCallbackServer("expected-state") as server:
        status, _body = _serve_one(server, "/callback?state=WRONG&token=dct_evil")
        assert status == 400
        assert server.received_token is None  # forged callback did not land
        # the REAL callback still succeeds afterwards (server kept waiting)
        status, body = _serve_one(server, "/callback?state=expected-state&token=dct_ok")
        assert status == 200
        assert "연결 완료" in body
        assert server.received_token == "dct_ok"


def test_missing_state_rejected():
    with cli._ConnectCallbackServer("expected-state") as server:
        status, _ = _serve_one(server, "/callback?token=dct_ok")
        assert status == 400
        assert server.received_token is None


def test_token_prefix_validated():
    with cli._ConnectCallbackServer("st") as server:
        status, _ = _serve_one(server, "/callback?state=st&token=nope_123")
        assert status == 400
        assert server.received_token is None
        status, _ = _serve_one(server, "/callback?state=st")  # missing token
        assert status == 400
        assert server.received_token is None


def test_non_callback_path_is_404():
    with cli._ConnectCallbackServer("st") as server:
        status, _ = _serve_one(server, "/anything")
        assert status == 404
        assert server.received_token is None


def test_run_connect_flow_end_to_end(monkeypatch, capsys):
    """Full flow: URL contains state/port/name; hitting the callback returns
    the token; nothing sensitive printed."""
    seen: dict[str, str] = {}

    def fake_open(url: str) -> bool:
        seen["url"] = url
        query = parse_qs(urlsplit(url).query)
        state = query["state"][0]
        port = int(query["port"][0])

        threading.Thread(
            target=lambda: _get(port, f"/callback?state={state}&token=dct_e2e")
        ).start()
        return True

    monkeypatch.setattr(cli, "_open_browser", fake_open)
    token = cli.run_connect_flow(central_url="http://central", timeout=10)
    assert token == "dct_e2e"
    assert seen["url"].startswith("http://central/connect/cli?state=")
    assert "&port=" in seen["url"] and "&name=" in seen["url"]
    assert "dct_e2e" not in capsys.readouterr().out  # token never printed


def test_run_connect_flow_times_out(capsys):
    token = cli.run_connect_flow(central_url="http://central", timeout=0.05, no_browser=True)
    assert token is None
    out = capsys.readouterr().out
    assert "/connect/cli?state=" in out  # --no-browser prints the URL


def test_run_connect_flow_strips_api_suffix_from_page_url(capsys):
    """config의 central_url이 API 베이스(`…/api`)여도 /connect/cli 페이지 URL은
    웹 origin으로 조립된다 — `https://host/api/connect/cli` 404 회귀."""
    cli.run_connect_flow(central_url="http://central/api", timeout=0.05, no_browser=True)
    out = capsys.readouterr().out
    assert "http://central/connect/cli?state=" in out
    assert "/api/connect/cli" not in out


def test_cmd_connect_stores_both_services_and_config(monkeypatch, tmp_path, capsys):
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "run_connect_flow", lambda **_kw: "dct_secret123")
    monkeypatch.setattr(
        cli, "_store_keychain_secret", lambda service, token: stored.append((service, token))
    )
    config_path = tmp_path / "config.json"
    monkeypatch.setenv(cli.CLI_CONFIG_ENV, str(config_path))

    rc = cli.main(["connect", "--central-url", "http://central"])
    assert rc == 0
    # SAME token into BOTH Keychain services (reuses the orthus init helper)
    assert stored == [
        (cli.KEYCHAIN_SERVICE, "dct_secret123"),
        (cli.COLLECTOR_KEYCHAIN_SERVICE, "dct_secret123"),
    ]
    assert json.loads(config_path.read_text())["central_url"] == "http://central"
    out = capsys.readouterr().out
    assert "dct_secret123" not in out  # NEVER printed
    assert "dct_****" in out  # masked confirmation


def test_cmd_connect_timeout_prints_manual_fallback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_connect_flow", lambda **_kw: None)
    monkeypatch.setattr(
        cli,
        "_store_keychain_secret",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not store on timeout")),
    )
    rc = cli.main(["connect", "--central-url", "http://central"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "orthus init --central-url http://central" in out  # manual path
    assert "--mcp-token-stdin" in out


def test_cmd_connect_requires_central_url(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(cli.CENTRAL_URL_ENV, raising=False)
    monkeypatch.setenv(cli.CLI_CONFIG_ENV, str(tmp_path / "missing.json"))
    rc = cli.main(["connect"])
    assert rc == 2
    assert "central URL" in capsys.readouterr().err  # usage errors go to stderr
