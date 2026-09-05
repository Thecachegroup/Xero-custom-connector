"""
The two things standing between the open internet and the whole ATS:
the connector key on the MCP endpoint, and the HMAC on file-relay links.

Both are one-line guards. One-line guards are exactly what a careless upload
removes without anyone noticing, because nothing about the app looks wrong
afterwards.
"""

import time

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------
# Connector key
# --------------------------------------------------------------------------


@pytest.fixture
def client(api):
    return TestClient(api.app, raise_server_exceptions=False)


def _rpc(method="initialize", **params):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def test_wrong_key_is_rejected(api, client, monkeypatch):
    monkeypatch.setattr(api, "CONNECTOR_SHARED_KEY", "the-real-key")
    resp = client.post("/api/mcp/not-the-real-key", json=_rpc())
    assert resp.status_code == 401


def test_right_key_is_accepted(api, client, monkeypatch):
    monkeypatch.setattr(api, "CONNECTOR_SHARED_KEY", "the-real-key")
    resp = client.post("/api/mcp/the-real-key", json=_rpc())
    assert resp.status_code == 200
    assert resp.json()["result"]["serverInfo"]["name"] == "cats-connector"


@pytest.mark.xfail(
    strict=True,
    reason="BUG (found 05/09/2026, fix ready, not yet applied): mcp_endpoint "
           "reads `if CONNECTOR_SHARED_KEY and key != ...`, which skips the "
           "check entirely when the variable is empty. Fix: "
           "`if not CONNECTOR_SHARED_KEY or not hmac.compare_digest(key, "
           "CONNECTOR_SHARED_KEY):`. Not currently exploitable - the key IS "
           "set in Vercel and a wrong key returns 401 - but a renamed or "
           "dropped env var would open the whole ATS silently.",
)
def test_unset_key_fails_closed(api, client, monkeypatch):
    """The important one.

    If CONNECTOR_SHARED_KEY is missing from the environment - a fat-fingered
    Vercel setting, a new deployment, a renamed variable - the endpoint must
    refuse every request. It must NOT quietly serve the entire ATS to anyone
    who guesses the domain.

    The original code read `if CONNECTOR_SHARED_KEY and key != ...`, which
    skips the check completely when the variable is empty. That is a fail-OPEN
    default on a connector holding candidate data and outbound email.
    """
    monkeypatch.setattr(api, "CONNECTOR_SHARED_KEY", "")
    resp = client.post("/api/mcp/anything-at-all", json=_rpc())
    assert resp.status_code == 401, (
        "Connector served a request with no shared key configured - "
        "the endpoint is unauthenticated."
    )


@pytest.mark.xfail(
    strict=True,
    reason="BUG (found 05/09/2026, fix ready, not yet applied): the connector "
           "key is compared with a plain !=, which leaks it a character at a "
           "time to a patient attacker. Same one-line fix as above.",
)
def test_key_comparison_is_constant_time(api):
    """A plain != leaks the key one character at a time to a patient attacker."""
    import inspect

    source = inspect.getsource(api.mcp_endpoint)
    assert "compare_digest" in source, (
        "Connector key is compared with a plain !=. Use hmac.compare_digest."
    )


# --------------------------------------------------------------------------
# Signed file-relay links
# --------------------------------------------------------------------------


@pytest.fixture
def signing_key(api, monkeypatch):
    monkeypatch.setattr(api, "CONNECTOR_SHARED_KEY", "signing-key-for-tests")
    return "signing-key-for-tests"


def test_valid_token_round_trips(api, signing_key):
    expires = int(time.time()) + 600
    token = api._file_token(4242, expires)
    assert api._file_token_valid(4242, expires, token) is True


def test_token_is_bound_to_one_attachment(api, signing_key):
    """A link for one CV must not open a different one."""
    expires = int(time.time()) + 600
    token = api._file_token(4242, expires)
    assert api._file_token_valid(9999, expires, token) is False


def test_token_is_bound_to_its_expiry(api, signing_key):
    """Extending the deadline in the URL must invalidate the signature."""
    expires = int(time.time()) + 600
    token = api._file_token(4242, expires)
    assert api._file_token_valid(4242, expires + 3600, token) is False


def test_expired_token_is_rejected(api, signing_key):
    past = int(time.time()) - 1
    token = api._file_token(4242, past)
    assert api._file_token_valid(4242, past, token) is False


@pytest.mark.parametrize("forged", ["", None, "deadbeef", "0" * 64])
def test_forged_tokens_are_rejected(api, signing_key, forged):
    expires = int(time.time()) + 600
    assert api._file_token_valid(4242, expires, forged) is False


def test_signing_refuses_without_a_key(api, monkeypatch):
    """No key means no signature - never an unsigned link."""
    from fastapi import HTTPException

    monkeypatch.setattr(api, "CONNECTOR_SHARED_KEY", "")
    with pytest.raises(HTTPException):
        api._file_token(4242, int(time.time()) + 600)


def test_validation_fails_shut_without_a_key(api, monkeypatch):
    monkeypatch.setattr(api, "CONNECTOR_SHARED_KEY", "")
    assert api._file_token_valid(4242, int(time.time()) + 600, "anything") is False


def test_a_different_key_cannot_sign(api, monkeypatch):
    """Rotating CONNECTOR_SHARED_KEY must invalidate every link already issued."""
    expires = int(time.time()) + 600
    monkeypatch.setattr(api, "CONNECTOR_SHARED_KEY", "old-key")
    old_token = api._file_token(4242, expires)
    monkeypatch.setattr(api, "CONNECTOR_SHARED_KEY", "new-key")
    assert api._file_token_valid(4242, expires, old_token) is False
