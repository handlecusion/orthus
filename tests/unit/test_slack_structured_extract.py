from __future__ import annotations

from orthus.structured.slack_extract import extract_slack_rows_from_markdown


def test_extracts_contact_link_event_action_decision_from_slack_markdown() -> None:
    markdown = """# Slack C123 1770000000.000100

Source: slack
Channel: C123
Thread: 1770000000.000100
Messages: 2

## Transcript

### U1 (1770000000.000100)

김대표 / <mailto:owner@example.com|owner@example.com> / <tel:010-1234-5678|010-1234-5678>

### U2 (1770000001.000100)

내일 오후 2시 중앙센터 미팅 자료 확인 부탁. <https://example.com|자료> 이 방향으로 하기로 결정
"""
    rows = extract_slack_rows_from_markdown(markdown)
    types = {row["record_type"] for row in rows}

    assert {"contact", "link", "event", "action_item", "decision"} <= types
    contacts = [row for row in rows if row["record_type"] == "contact"]
    assert any(row["properties"]["email"] == "owner@example.com" for row in contacts)
    assert any(row["properties"].get("phone") == "010-1234-5678" for row in contacts)
    assert any(row["properties"]["name"] == "김대표" for row in contacts)
