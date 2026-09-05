"""
The JSON-RPC envelope.

Claude talks to this connector through one endpoint. If the envelope
regresses, every tool fails at once and the error Claude shows the user is
whatever leaked out of the handler.
"""

import pytest
from fastapi.testclient import TestClient

KEY = "test-connector-key"


@pytest.fixture
def client(api, monkeypatch):
    monkeypatch.setattr(api, "CONNECTOR_SHARED_KEY", KEY)
    return TestClient(api.app, raise_server_exceptions=False)


def _post(client, method, **params):
    return client.post(
        f"/api/mcp/{KEY}",
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
    )


def test_initialize_declares_the_protocol(client):
    body = _post(client, "initialize").json()
    assert body["id"] == 7
    assert body["result"]["protocolVersion"]
    assert body["result"]["capabilities"]["tools"] == {}


def test_tools_list_returns_every_tool(api, client):
    tools = _post(client, "tools/list").json()["result"]["tools"]
    assert len(tools) == len(api.TOOLS)
    assert {t["name"] for t in tools} == set(api.TOOLS)


def test_advertised_tools_carry_name_description_and_schema(client):
    for tool in _post(client, "tools/list").json()["result"]["tools"]:
        assert set(tool) == {"name", "description", "inputSchema"}
        assert tool["description"].strip()


def test_unknown_tool_is_a_clean_rpc_error(client):
    body = _post(client, "tools/call", name="no_such_tool", arguments={}).json()
    assert body["error"]["code"] == -32601
    assert "no_such_tool" in body["error"]["message"]


def test_unknown_method_is_a_clean_rpc_error(client):
    body = _post(client, "nonsense/method").json()
    assert body["error"]["code"] == -32601


def test_handler_failure_names_the_tool(api, client, monkeypatch):
    """A bare traceback tells the user nothing about which tool broke."""
    async def _explode(args):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(api.TOOLS["list_jobs"], "handler", _explode)
    body = _post(client, "tools/call", name="list_jobs", arguments={}).json()
    assert body["error"]["code"] == -32001
    assert "list_jobs" in body["error"]["message"]
    assert "RuntimeError" in body["error"]["message"]


def test_missing_argument_is_reported_as_a_bad_request(api, client, monkeypatch):
    async def _needs_arg(args):
        return {"job_id": args["job_id"]}

    monkeypatch.setitem(api.TOOLS["list_jobs"], "handler", _needs_arg)
    body = _post(client, "tools/call", name="list_jobs", arguments={}).json()
    assert body["error"]["code"] == -32602


def test_results_are_shaped_before_they_are_returned(api, client, monkeypatch):
    """auto_shape must run on the way out, not be forgotten by a refactor."""
    import json as _json

    async def _hal(args):
        return {"_embedded": {"jobs": [{"id": 1}]}, "_links": {"self": {"href": "/x"}}}

    monkeypatch.setitem(api.TOOLS["list_jobs"], "handler", _hal)
    body = _post(client, "tools/call", name="list_jobs", arguments={}).json()
    payload = _json.loads(body["result"]["content"][0]["text"])
    assert payload["jobs"] == [{"id": 1}]
    assert "_embedded" not in payload and "_links" not in payload


def test_health_answers_without_a_key(client):
    """Health is deliberately open - but it only proves the app booted."""
    resp = client.get("/api/mcp/anything")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_rpc_envelope_helpers(api):
    assert api.rpc_result(1, {"x": 1}) == {"jsonrpc": "2.0", "id": 1, "result": {"x": 1}}
    err = api.rpc_error(1, -32000, "bad")
    assert err["error"]["code"] == -32000 and err["error"]["message"] == "bad"
