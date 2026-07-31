"""메일 명함(서명) owner-scope 저장/조회 + 엔드포인트 + 검증.

명함은 작성한 메일 끝에 넣는 본인 비즈니스 카드다. (node_id, owner_id, from_addr)당 1행 upsert,
구조화 필드만 저장(서버는 HTML 미보관). 본문 HTML 렌더는 FE 책임.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from orthus.api.main import app
from orthus.mail.signature import get_signature, upsert_signature
from orthus.schemas.canonical import MailSignatureInput, MailSignatureLink

client = TestClient(app)
DEMO_USER_HEADERS = {"X-User-Id": "00000000-0000-4000-8000-000000000001"}
NODE = "test-sig-node"


def test_get_signature_empty_default_when_none(clean):
    """저장된 명함이 없으면 빈 기본값(모든 필드 공백, links 없음, enabled=True)."""
    sig = get_signature(node_id=NODE, owner_id=uuid.uuid4())
    assert sig.from_addr == ""
    assert sig.display_name == ""
    assert sig.company == ""
    assert sig.links == []
    assert sig.enabled is True


def test_upsert_then_get_roundtrip(clean):
    owner = uuid.uuid4()
    saved = upsert_signature(
        node_id=NODE,
        owner_id=owner,
        payload=MailSignatureInput(
            display_name="박기획",
            title="대표",
            company="acme",
            email="2fe@acme.example",
            phone="010-0000-0000",
            website="https://acme.example",
            links=[MailSignatureLink(label="LinkedIn", url="https://linkedin.com/in/2fe")],
        ),
    )
    assert saved.display_name == "박기획"
    assert saved.links[0].label == "LinkedIn"

    got = get_signature(node_id=NODE, owner_id=owner)
    assert got.display_name == "박기획"
    assert got.title == "대표"
    assert got.website == "https://acme.example"
    assert [link.url for link in got.links] == ["https://linkedin.com/in/2fe"]


def test_upsert_updates_single_row(clean):
    owner = uuid.uuid4()
    upsert_signature(
        node_id=NODE, owner_id=owner, payload=MailSignatureInput(display_name="v1", title="t1")
    )
    upsert_signature(
        node_id=NODE, owner_id=owner, payload=MailSignatureInput(display_name="v2", title="t2")
    )
    got = get_signature(node_id=NODE, owner_id=owner)
    # 두 번째 값으로 갱신(덮어쓰기), owner+from_addr당 1행 유지.
    assert got.display_name == "v2"
    assert got.title == "t2"


def test_signatures_are_scoped_by_from_addr(clean):
    owner = uuid.uuid4()
    upsert_signature(
        node_id=NODE,
        owner_id=owner,
        from_addr="2fe@acme.example",
        payload=MailSignatureInput(display_name="Acme", email="2fe@acme.example"),
    )
    upsert_signature(
        node_id=NODE,
        owner_id=owner,
        from_addr="2fe@nova.example",
        payload=MailSignatureInput(display_name="Nova", email="2fe@nova.example"),
    )

    dz = get_signature(node_id=NODE, owner_id=owner, from_addr="2fe@acme.example")
    nova = get_signature(node_id=NODE, owner_id=owner, from_addr="2fe@nova.example")

    assert dz.display_name == "Acme"
    assert dz.from_addr == "2fe@acme.example"
    assert nova.display_name == "Nova"
    assert nova.from_addr == "2fe@nova.example"


def test_empty_links_filtered_out():
    sig = MailSignatureInput(
        links=[
            MailSignatureLink(label="", url=""),
            MailSignatureLink(label="LinkedIn", url="https://linkedin.com/in/x"),
        ]
    )
    assert len(sig.links) == 1
    assert sig.links[0].label == "LinkedIn"


def test_link_url_must_be_http():
    with pytest.raises(ValidationError):
        MailSignatureLink(label="bad", url="javascript:alert(1)")


def test_website_must_be_http():
    with pytest.raises(ValidationError):
        MailSignatureInput(website="ftp://nope")


def test_links_capped_at_8():
    with pytest.raises(ValidationError):
        MailSignatureInput(
            links=[MailSignatureLink(label=f"L{i}", url=f"https://x{i}.com") for i in range(9)]
        )


def test_put_and_get_signature_endpoint(clean):
    """엔드포인트 라운드트립 (owner = demo user)."""
    body = {
        "from_addr": "qa@nova.example",
        "display_name": "API 명함",
        "title": "PM",
        "company": "nova",
        "email": "qa@nova.example",
        "phone": "",
        "website": "",
        "links": [{"label": "GitHub", "url": "https://github.com/x"}],
        "enabled": True,
    }
    put = client.put("/mail/signature?from_addr=qa@nova.example", json=body, headers=DEMO_USER_HEADERS)
    assert put.status_code == 200, put.text
    assert put.json()["display_name"] == "API 명함"
    assert put.json()["from_addr"] == "qa@nova.example"

    got = client.get("/mail/signature?from_addr=qa@nova.example", headers=DEMO_USER_HEADERS)
    assert got.status_code == 200
    payload = got.json()
    assert payload["from_addr"] == "qa@nova.example"
    assert payload["display_name"] == "API 명함"
    assert payload["company"] == "nova"
    assert payload["links"] == [{"label": "GitHub", "url": "https://github.com/x"}]


def test_put_signature_rejects_bad_website(clean):
    resp = client.put(
        "/mail/signature",
        json={"website": "ftp://bad"},
        headers=DEMO_USER_HEADERS,
    )
    assert resp.status_code == 422


def test_signature_endpoint_rejects_anonymous_session(clean, monkeypatch):
    from orthus.settings import get_settings

    monkeypatch.setattr(get_settings(), "auth_mode", "session")
    resp = client.get("/mail/signature")
    assert resp.status_code == 401
