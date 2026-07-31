"""본문 원격 이미지 프록시 가드 회귀 (`orthus/mail/backends.py`).

- `validate_remote_image_url`: 사설/루프백/비http 스킴은 SSRF 가드로 reject.
- `nova_attachment_key_from_url`: S3 인라인 이미지 URL → provider-relative key.
- `sniff_image_type`: content-type 미지정 응답의 이미지 판별.
"""

from __future__ import annotations

import socket

import pytest

from orthus.mail.backends import (
    MailImageProxyError,
    nova_attachment_key_from_url,
    sniff_image_type,
    validate_remote_image_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/a.png",
        "file:///etc/passwd",
        "https:///no-host.png",
        "http://127.0.0.1/x.png",
        "http://localhost:8820/auth/me",
        "http://10.0.0.8/internal.png",
        "http://192.168.0.10/cam.jpg",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/x.png",
    ],
)
def test_validate_remote_image_url_rejects_unsafe(url: str) -> None:
    with pytest.raises(MailImageProxyError):
        validate_remote_image_url(url)


def test_validate_remote_image_url_accepts_public_host(monkeypatch) -> None:
    def fake_getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    parsed = validate_remote_image_url("https://example.com/logo.png?a=1&b=2")
    assert parsed.host == "example.com"
    assert parsed.scheme == "https"


def test_validate_remote_image_url_rejects_mixed_resolution(monkeypatch) -> None:
    """공인+사설이 섞여 resolve되면(rebinding 스타일) reject."""

    def fake_getaddrinfo(host, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(MailImageProxyError):
        validate_remote_image_url("https://evil.example/pixel.png")


def test_nova_attachment_key_path_style() -> None:
    url = (
        "https://s3.ap-northeast-2.amazonaws.com/nova-uploads/"
        "mail/attachments/65af5874-89ca-43d7-b480-11a0b5076a60/9b75c306.png"
    )
    assert nova_attachment_key_from_url(url) == (
        "mail/attachments/65af5874-89ca-43d7-b480-11a0b5076a60/9b75c306.png"
    )


def test_nova_attachment_key_virtual_host_style() -> None:
    url = "https://nova-uploads.s3.ap-northeast-2.amazonaws.com/mail/attachments/a/b.jpg"
    assert nova_attachment_key_from_url(url) == "mail/attachments/a/b.jpg"


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/mail/attachments/a/b.jpg",  # amazonaws 아님
        "https://s3.ap-northeast-2.amazonaws.com/nova-uploads/other/key.png",
        "https://s3.amazonaws.com/bucket/mail/attachments/../../etc/passwd",
        "not a url",
    ],
)
def test_nova_attachment_key_rejects(url: str) -> None:
    assert nova_attachment_key_from_url(url) is None


def test_sniff_image_type() -> None:
    assert sniff_image_type(b"\x89PNG\r\n\x1a\n" + b"0" * 8) == "image/png"
    assert sniff_image_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_image_type(b"GIF89a...") == "image/gif"
    assert sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_type(b"<html>nope</html>") is None
