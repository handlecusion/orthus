"""청크 업로드(orthus/media.py) 단위 테스트 — 대용량 동영상 경로."""

from __future__ import annotations

import pytest

from orthus import media
from orthus.settings import get_settings


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "media_store_path", tmp_path)
    monkeypatch.setattr(settings, "media_max_bytes", 1024)
    return tmp_path


def test_chunk_roundtrip_assembles_and_serves(media_root):
    upload_id = media.chunk_init("clip.mp4", "video/mp4")
    assert media.chunk_append(upload_id, 0, b"a" * 400) == 400
    assert media.chunk_append(upload_id, 1, b"b" * 400) == 800
    asset = media.chunk_complete(upload_id)

    assert asset["size"] == 800
    assert asset["name"] == "clip.mp4"
    assert asset["filename"].endswith(".mp4")
    assert asset["content_type"] == "video/mp4"
    path, ctype = media.resolve_media(asset["filename"])
    assert path.read_bytes() == b"a" * 400 + b"b" * 400
    assert ctype == "video/mp4"
    # 임시 파일은 남지 않는다.
    assert not list((media_root / "incomplete").glob("*"))


def test_chunk_append_rejects_out_of_order_part(media_root):
    upload_id = media.chunk_init("clip.mp4", "video/mp4")
    media.chunk_append(upload_id, 0, b"x")
    with pytest.raises(ValueError, match="index"):
        media.chunk_append(upload_id, 2, b"y")


def test_chunk_append_over_cap_cleans_session(media_root):
    upload_id = media.chunk_init("big.mp4", "video/mp4")
    with pytest.raises(ValueError, match="large"):
        media.chunk_append(upload_id, 0, b"z" * 2048)
    with pytest.raises(LookupError):
        media.chunk_complete(upload_id)


def test_chunk_unknown_or_invalid_id_raises(media_root):
    with pytest.raises(LookupError):
        media.chunk_append("0" * 32, 0, b"x")
    with pytest.raises(LookupError):
        media.chunk_append("../evil", 0, b"x")
    with pytest.raises(LookupError):
        media.chunk_complete("f" * 32)


def test_chunk_abort_is_idempotent(media_root):
    upload_id = media.chunk_init("clip.mov", "video/quicktime")
    media.chunk_append(upload_id, 0, b"x")
    media.chunk_abort(upload_id)
    media.chunk_abort(upload_id)  # 멱등
    with pytest.raises(LookupError):
        media.chunk_complete(upload_id)


def test_chunk_init_sweeps_stale_leftovers(media_root, monkeypatch):
    stale = media_root / "incomplete"
    stale.mkdir(parents=True, exist_ok=True)
    old = stale / ("a" * 32 + ".part")
    old.write_bytes(b"stale")
    import os
    import time

    past = time.time() - media._STALE_UPLOAD_SECONDS - 60
    os.utime(old, (past, past))

    media.chunk_init("new.mp4", "video/mp4")

    assert not old.exists()
