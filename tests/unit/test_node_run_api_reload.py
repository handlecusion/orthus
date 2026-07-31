from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]


def _write_node_env(
    tmp_path: Path,
    *,
    public: bool,
    reload_value: str | None = None,
) -> Path:
    nodes_root = tmp_path / "nodes"
    node_root = nodes_root / "company"
    wiki_store = node_root / "wiki-store"
    wiki_store.mkdir(parents=True)
    auth_public_base = "https://orthus-central.example.com/api" if public else "http://localhost:8820"
    auth_web_base = "https://orthus-central.example.com" if public else "http://localhost:3820"
    reload_line = f"ORTHUS_API_RELOAD={reload_value}\n" if reload_value is not None else ""
    (node_root / "node.env").write_text(
        "\n".join(
            [
                "ORTHUS_NODE_KIND=company",
                "ORTHUS_NODE_ID=company",
                f"ORTHUS_NODE_ROOT={node_root}",
                "ORTHUS_NODE_DB=orthus_company",
                f"ORTHUS_WIKI_STORE={wiki_store}",
                "ORTHUS_API_PORT=19999",
                "ORTHUS_AUTH_MODE=session",
                f"ORTHUS_AUTH_PUBLIC_BASE_URL={auth_public_base}",
                f"ORTHUS_AUTH_WEB_BASE_URL={auth_web_base}",
                reload_line,
            ]
        ),
        encoding="utf-8",
    )
    return nodes_root


def _run_node_api(tmp_path: Path, *, public: bool, reload_value: str | None = None):
    nodes_root = _write_node_env(tmp_path, public=public, reload_value=reload_value)
    capture = tmp_path / "uv-args.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "${UV_CAPTURE}"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    env = {
        **os.environ,
        "ORTHUS_NODES_ROOT": str(nodes_root),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "UV_CAPTURE": str(capture),
    }
    result = subprocess.run(
        ["bash", "scripts/node/run_api.sh", "company"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    captured_args = capture.read_text(encoding="utf-8") if capture.exists() else ""
    return result, captured_args


def test_node_api_auto_reload_disabled_for_public_auth_origin(tmp_path):
    result, captured_args = _run_node_api(tmp_path, public=True)

    assert result.returncode == 0
    assert "reload: false (auto)" in result.stdout
    assert "--reload" not in captured_args


def test_node_api_auto_reload_enabled_for_local_auth_origin(tmp_path):
    result, captured_args = _run_node_api(tmp_path, public=False)

    assert result.returncode == 0
    assert "reload: true (auto)" in result.stdout
    assert "--reload" in captured_args


def test_node_api_rejects_forced_reload_for_public_auth_origin(tmp_path):
    result, captured_args = _run_node_api(tmp_path, public=True, reload_value="true")

    assert result.returncode != 0
    assert "ORTHUS_API_RELOAD=true is not allowed for public auth origins" in result.stderr
    assert captured_args == ""
