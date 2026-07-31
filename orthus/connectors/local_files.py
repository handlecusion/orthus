"""Personal local-files connector.

Reads a configured allowlist of local directories and normalizes text-like files
into InternalDocument rows. Absolute paths stay out of document content and
external ids are path hashes so central exports do not leak local filesystem
layout by default.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from orthus.connectors.base import Connector
from orthus.schemas.canonical import InternalDocument

DEFAULT_LOCAL_FILE_EXTENSIONS = ".md,.mdx,.txt,.json,.jsonl,.csv,.tsv"
DEFAULT_LOCAL_FILE_MAX_BYTES = 1024 * 1024

_SKIP_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "dist",
    "node_modules",
}


def parse_local_file_roots(value: str) -> tuple[Path, ...]:
    """Parse comma-separated root paths with shell-style env/user expansion."""
    roots: list[Path] = []
    seen: set[Path] = set()
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        path = Path(os.path.expandvars(item)).expanduser().resolve(strict=False)
        if path in seen:
            continue
        seen.add(path)
        roots.append(path)
    return tuple(roots)


def parse_local_file_extensions(value: str) -> frozenset[str]:
    """Parse comma-separated extensions. '*' means all non-hidden files."""
    out: set[str] = set()
    for raw in value.split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if item == "*":
            return frozenset({"*"})
        if not item.startswith("."):
            item = f".{item}"
        out.add(item)
    return frozenset(out)


class LocalFilesConnector(Connector):
    """Connector over node-local text files under configured roots."""

    def __init__(
        self,
        roots: Iterable[Path | str],
        *,
        extensions: Iterable[str] | None = None,
        max_bytes: int = DEFAULT_LOCAL_FILE_MAX_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.roots = tuple(Path(root).expanduser().resolve(strict=False) for root in roots)
        self.extensions = frozenset(
            extensions or parse_local_file_extensions(DEFAULT_LOCAL_FILE_EXTENSIONS)
        )
        self.max_bytes = max_bytes

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        for root in self.roots:
            yield from self._iter_root(root, since)

    def _iter_root(self, root: Path, since: datetime | None) -> Iterator[InternalDocument]:
        if not root.exists():
            raise FileNotFoundError(f"local_files root does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"local_files root is not a directory: {root}")

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            dirnames[:] = sorted(name for name in dirnames if not _skip_dir(current / name))
            for name in sorted(filenames):
                path = current / name
                doc = self._read_document(root, path, since)
                if doc is not None:
                    yield doc

    def _read_document(
        self,
        root: Path,
        path: Path,
        since: datetime | None,
    ) -> InternalDocument | None:
        if self._skip_file(path):
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_size > self.max_bytes:
            return None

        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        if since is not None and mtime <= since:
            return None

        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            return None

        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in raw:
            return None
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

        rel = path.relative_to(root).as_posix()
        external_id = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
        markdown = f"# {path.name}\n\nSource: local_files\nPath: {rel}\n\n---\n\n{body.strip()}\n"
        return InternalDocument(
            title=rel,
            markdown=markdown,
            source="local_files",
            source_external_id=f"path-sha256:{external_id}",
            source_last_edited_at=mtime,
            project="company",
        )

    def _skip_file(self, path: Path) -> bool:
        if path.name.startswith(".") or path.is_symlink():
            return True
        if "*" in self.extensions:
            return False
        return path.suffix.lower() not in self.extensions


def _skip_dir(path: Path) -> bool:
    return path.name.startswith(".") or path.name in _SKIP_DIR_NAMES or path.is_symlink()
