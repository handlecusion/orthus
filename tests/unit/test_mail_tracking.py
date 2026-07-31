"""메일 열람 추적 순수 헬퍼 단위 테스트 — token/픽셀/주입 (DB 불필요)."""

from __future__ import annotations

from orthus.mail.tracking import (
    inject_tracking_pixel,
    is_plausible_token,
    make_tracking_token,
    pixel_url,
    tracking_pixel_html,
)

BASE = "https://orthus-central.example.com/api"


def test_is_plausible_token_accepts_real_tokens_rejects_garbage():
    for _ in range(20):
        assert is_plausible_token(make_tracking_token())  # 실제 토큰 통과
    assert not is_plausible_token("")  # 빈 값
    assert not is_plausible_token("short")  # 너무 짧음
    assert not is_plausible_token("x" * 400)  # 너무 김
    assert not is_plausible_token("has space 0000000")  # 허용 문자 외
    assert not is_plausible_token("../../etc/passwd00")  # 경로/특수문자


def test_token_is_unique_and_urlsafe():
    tokens = {make_tracking_token() for _ in range(50)}
    assert len(tokens) == 50  # 충돌 없음
    for t in tokens:
        assert t  # 비어있지 않음
        assert all(c.isalnum() or c in "-_" for c in t)  # URL-safe


def test_pixel_url_strips_trailing_slash():
    assert pixel_url(BASE + "/", "abc") == f"{BASE}/mail/track/open/abc.gif"
    assert pixel_url(BASE, "abc") == f"{BASE}/mail/track/open/abc.gif"


def test_tracking_pixel_html_is_hidden_img():
    html = tracking_pixel_html(BASE, "tok123")
    assert "<img" in html
    assert f"{BASE}/mail/track/open/tok123.gif" in html
    assert "display:none" in html
    assert 'width="1"' in html and 'height="1"' in html


def test_inject_pixel_before_body_close():
    html = "<html><body><p>hi</p></body></html>"
    out = inject_tracking_pixel(html, BASE, "tok")
    assert out.count("<img") == 1
    # 픽셀이 </body> 바로 앞에 들어간다.
    assert out.index("<img") < out.lower().index("</body>")
    assert out.endswith("</body></html>")


def test_inject_pixel_appends_when_no_body():
    html = "<p>hi</p>"
    out = inject_tracking_pixel(html, BASE, "tok")
    assert out.startswith("<p>hi</p>")
    assert out.rstrip().endswith("/>")
    assert "<img" in out


def test_inject_pixel_empty_html_returns_pixel_only():
    out = inject_tracking_pixel("", BASE, "tok")
    assert out == tracking_pixel_html(BASE, "tok")


def test_inject_pixel_case_insensitive_body_close():
    html = "<HTML><BODY>x</BODY></HTML>"
    out = inject_tracking_pixel(html, BASE, "tok")
    assert out.count("<img") == 1
    assert out.lower().index("<img") < out.lower().index("</body>")
