"""
The preview gate.

Every tool that changes something in CATS is preview-by-default: called
without confirm: true it must describe what it WOULD do and touch nothing.
That gate is the only thing standing between a misread instruction and a
bulk pipeline update across a live job.

These tests call each write tool with the network wired to explode. If a tool
reaches the ATS without confirmation, the test fails rather than the candidate
record changing.
"""

import asyncio

import pytest

from tests.test_manifest import WRITE_TOOLS


# Some tools accept either of two arguments (candidate_id OR candidate_ids)
# and so declare neither as required. The schema cannot express that, so the
# working set is named here.
ARG_OVERRIDES = {
    "add_candidate_tag": {"candidate_id": 1, "tag": "Test Tag"},
    "remove_candidate_tag": {"candidate_id": 1, "tag": "Test Tag"},
    # update_job correctly refuses a no-op, so the preview needs a real change.
    "update_job": {"job_id": 1, "title": "Revised Title"},
}


def _minimal_args(schema):
    """Build the smallest argument set a tool's schema will accept.

    Values are shaped by declared type only - none of them are ever sent
    anywhere, because a tool that gets this far without confirm must not make
    a request at all.
    """
    args = {}
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        spec = properties.get(name, {})
        kind = spec.get("type")
        if kind == "integer":
            args[name] = 1
        elif kind == "number":
            args[name] = 1.0
        elif kind == "boolean":
            args[name] = False
        elif kind == "array":
            item_type = spec.get("items", {}).get("type")
            args[name] = [1] if item_type == "integer" else ["x"]
        elif kind == "object":
            args[name] = {}
        else:
            enum = spec.get("enum")
            args[name] = enum[0] if enum else "x"
    return args


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_write_tool_does_nothing_without_confirm(api, no_network, name):
    tool = api.TOOLS[name]
    args = _minimal_args(tool["inputSchema"])
    args.update(ARG_OVERRIDES.get(name, {}))
    args.pop("confirm", None)

    result = asyncio.run(tool["handler"](args))

    assert isinstance(result, dict), f"{name} returned {type(result).__name__}, not a dict"


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_write_tool_says_nothing_has_changed(api, no_network, name):
    """The preview has to be legible as a preview, not just inert.

    Claude decides whether to ask the user before confirming based on what
    comes back. A preview that does not announce itself gets confirmed
    automatically, and the gate stops meaning anything.
    """
    tool = api.TOOLS[name]
    args = _minimal_args(tool["inputSchema"])
    args.update(ARG_OVERRIDES.get(name, {}))
    args.pop("confirm", None)

    result = asyncio.run(tool["handler"](args))
    blob = " ".join(str(v).lower() for v in result.values())

    assert "confirm: true" in blob, (
        f"{name}'s preview does not tell the caller how to proceed - "
        f"got: {result}"
    )
    assert ("nothing has changed" in blob
            or "nothing has been created" in blob), (
        f"{name}'s preview does not state that nothing happened - got: {result}"
    )


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_confirm_is_in_the_schema(api, name):
    """A write tool whose schema omits confirm can never be confirmed."""
    properties = api.TOOLS[name]["inputSchema"].get("properties", {})
    assert "confirm" in properties, (
        f"{name} is a write tool but does not advertise a confirm argument"
    )


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_confirm_is_never_required(api, name):
    """If confirm were required, the preview call itself would be rejected."""
    required = api.TOOLS[name]["inputSchema"].get("required", [])
    assert "confirm" not in required, (
        f"{name} lists confirm as required - the preview path is unreachable"
    )


def test_the_tripwire_actually_trips(api, no_network):
    """A test suite that cannot detect a write call proves nothing."""
    import httpx

    async def _try():
        async with httpx.AsyncClient() as client:
            await client.post("https://example.invalid")

    with pytest.raises(AssertionError):
        asyncio.run(_try())
