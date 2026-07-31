from orthus.audit.redact import redact_pii


def test_email_local_part_masked():
    out = redact_pii("contact alice.kim@example.com please")
    assert "alice.kim" not in out
    assert "@example.com" in out


def test_rrn_masked():
    out = redact_pii("주민번호 900101-1234567 입니다")
    assert "1234567" not in out
    assert "900101" in out  # birthdate prefix kept, secret tail masked


def test_card_masked():
    out = redact_pii("card 4111 1111 1111 1111 end")
    assert "4111 1111 1111 1111" not in out
    assert "****-****-****-****" in out


def test_phone_masked():
    out = redact_pii("call 010-1234-5678 now")
    assert "1234" not in out


def test_uuid_not_masked_by_phone_redaction():
    uuid = "f2fde785-9074-4615-9394-25540f729dc3"
    out = redact_pii(f"promoted_doc_id={uuid}")
    assert uuid in out


def test_nested_structures():
    out = redact_pii({"a": ["x@y.com", {"b": "010-1234-5678"}], "n": 5})
    assert out["a"][0].endswith("@y.com")
    assert out["n"] == 5  # non-strings untouched


def test_no_false_mask_on_plain_text():
    out = redact_pii("the quick brown fox")
    assert out == "the quick brown fox"


def test_redact_pii_text_returns_str():
    from orthus.audit.redact import redact_pii_text

    out = redact_pii_text("contact bob@corp.com for details")
    assert isinstance(out, str)
    assert "bob" not in out
    assert "@corp.com" in out


def test_redact_pii_text_plain_text_unchanged():
    from orthus.audit.redact import redact_pii_text

    assert redact_pii_text("hello world") == "hello world"
