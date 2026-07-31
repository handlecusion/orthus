"""Small local secret store facade.

Connector config entered through the web UI must not be persisted in Postgres
plaintext. DB rows keep only secret refs; values live in the configured backend.
"""

from __future__ import annotations

import platform
import subprocess
from threading import Lock

from orthus.settings import get_settings

_SERVICE = "orthus-ai"
_MEMORY: dict[str, str] = {}
_MEMORY_LOCK = Lock()


class SecretStoreError(RuntimeError):
    """Raised when the configured secret backend cannot store/read a value."""


def connector_secret_ref(account_id: object, key: str) -> str:
    clean_key = key.strip().lower()
    if not clean_key:
        raise ValueError("secret key required")
    return f"orthus/connectors/{account_id}/{clean_key}"


def put_secret(ref: str, value: str, *, backend: str | None = None) -> None:
    _validate_ref(ref)
    if not value:
        raise ValueError("secret value required")
    resolved = _resolve_backend(backend)
    if resolved == "memory":
        with _MEMORY_LOCK:
            _MEMORY[ref] = value
        return
    if resolved == "keychain":
        _keychain_put(ref, value)
        return
    raise SecretStoreError(f"unsupported secret backend: {resolved}")


def get_secret(ref: str, *, backend: str | None = None) -> str | None:
    _validate_ref(ref)
    resolved = _resolve_backend(backend)
    if resolved == "memory":
        with _MEMORY_LOCK:
            return _MEMORY.get(ref)
    if resolved == "keychain":
        return _keychain_get(ref)
    raise SecretStoreError(f"unsupported secret backend: {resolved}")


def clear_memory_secrets() -> None:
    """Test helper."""
    with _MEMORY_LOCK:
        _MEMORY.clear()


def _validate_ref(ref: str) -> None:
    if not ref.strip():
        raise ValueError("secret ref required")
    if "\x00" in ref:
        raise ValueError("secret ref contains null byte")


def _resolve_backend(backend: str | None) -> str:
    configured = (backend or get_settings().secret_backend or "auto").strip().lower()
    if configured == "auto":
        if platform.system() == "Darwin":
            return "keychain"
        raise SecretStoreError(
            "ORTHUS_SECRET_BACKEND=auto requires macOS Keychain; set an explicit backend"
        )
    return configured


def _keychain_put(ref: str, value: str) -> None:
    _run_security(
        [
            "add-generic-password",
            "-U",
            "-s",
            _SERVICE,
            "-a",
            ref,
            "-w",
            value,
        ]
    )


def _keychain_get(ref: str) -> str | None:
    result = _run_security(
        ["find-generic-password", "-s", _SERVICE, "-a", ref, "-w"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


def _run_security(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["security", *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise SecretStoreError("macOS security command not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise SecretStoreError("macOS security command timed out") from exc

    if check and result.returncode != 0:
        detail = result.stderr.strip() or "security command failed"
        raise SecretStoreError(detail)
    return result
