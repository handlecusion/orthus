from __future__ import annotations

from fastapi import HTTPException

from orthus.auth import oauth_login_error


def test_oauth_login_error_codes_are_stable_for_runbook():
    cases = {
        "account not invited": "not_invited",
        "email not verified": "email_not_verified",
        "google token exchange failed": "oauth_exchange_failed",
        "google id_token missing": "oauth_invalid_response",
        "google id_token invalid": "oauth_invalid_response",
        "google audience invalid": "oauth_invalid_response",
        "google issuer invalid": "oauth_invalid_response",
        "google exp invalid": "oauth_invalid_response",
        "google id_token expired": "oauth_expired",
        "google email missing": "oauth_invalid_response",
        "google nonce invalid": "oauth_invalid_state",
        "invalid oauth state": "oauth_invalid_state",
        "invalid oauth nonce": "oauth_invalid_state",
        "jwt expired": "oauth_invalid_state",
        "jwt signature invalid": "oauth_invalid_state",
        "jwt decode failed": "oauth_invalid_state",
    }

    for detail, expected in cases.items():
        assert oauth_login_error(HTTPException(status_code=401, detail=detail)) == expected


def test_oauth_login_error_falls_back_to_oauth_failed():
    assert (
        oauth_login_error(HTTPException(status_code=401, detail="unexpected oauth issue"))
        == "oauth_failed"
    )
