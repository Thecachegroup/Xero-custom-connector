"""
The regression guard.

This is the test that catches a truncated, stale or half-uploaded index.py.
It does not check that any tool WORKS - it checks that every tool is still
THERE, still wired to a real handler, and still advertising a schema Claude
can call. That is precisely what went missing, silently, in the 3 September
incident on the sister connector.

If you add a tool, add its name to EXPECTED_TOOLS in the same commit. The
failure message will tell you exactly what to do.
"""

import asyncio
import inspect

import pytest

EXPECTED_TOOLS = {
    "add_candidate_tag",
    "add_candidate_to_pipeline",
    "add_candidates_to_list",
    "bulk_update_pipelines",
    "change_job_status",
    "change_pipeline_status",
    "convert_attachment",
    "create_candidate",
    "create_candidate_activity",
    "create_candidate_list",
    "create_contact_activity",
    "create_job",
    "download_attachment",
    "filter_candidates",
    "filter_jobs",
    "find_orphan_shells",
    "get_candidate",
    "get_candidate_custom_fields",
    "get_candidate_pipeline_history",
    "get_candidate_resume",
    "get_candidate_tags",
    "get_company",
    "get_contact",
    "get_conversion_result",
    "get_job",
    "get_job_applications",
    "get_list_items",
    "get_workflow_statuses",
    "list_candidate_activities",
    "list_candidate_lists",
    "list_contact_activities",
    "list_job_statuses",
    "list_jobs",
    "list_pipeline_candidates",
    "list_portals",
    "list_recent_candidates",
    "merge_orphan_shells",
    "publish_job_to_portal",
    "read_resume",
    "remove_candidate_tag",
    "remove_list_item",
    "search_candidates",
    "search_candidates_deep",
    "search_companies",
    "search_contacts",
    "search_pipelines_by_status",
    "send_candidate_email",
    "unpublish_job_from_portal",
    "update_candidate",
    "update_candidate_notes",
    "update_job",
    "update_job_notes",
    "update_pipeline_rating_status",
    "upload_candidate_attachment",
}

# Tools that change something in CATS. Every one of these must refuse to act
# without confirm: true. Kept here rather than in the preview-gate test so
# there is one list to maintain, not two.
WRITE_TOOLS = {
    "add_candidate_tag",
    "add_candidate_to_pipeline",
    "add_candidates_to_list",
    "bulk_update_pipelines",
    "change_job_status",
    "change_pipeline_status",
    "create_candidate_list",
    "create_job",
    "publish_job_to_portal",
    "remove_candidate_tag",
    "unpublish_job_from_portal",
    "update_candidate_notes",
    "update_job",
    "update_job_notes",
    "update_pipeline_rating_status",
}


def test_no_tool_has_gone_missing(api):
    """A stale upload removes tools. This is the four-second check that says so."""
    missing = EXPECTED_TOOLS - set(api.TOOLS)
    assert not missing, (
        f"{len(missing)} tool(s) vanished from TOOLS: {sorted(missing)}. "
        "If this was deliberate, remove them from EXPECTED_TOOLS. If it was "
        "not, you have uploaded an older or truncated index.py over the live one."
    )


def test_no_tool_appeared_unannounced(api):
    """New tools are fine - but they get recorded here, so the count is meaningful."""
    extra = set(api.TOOLS) - EXPECTED_TOOLS
    assert not extra, (
        f"New tool(s) not yet recorded: {sorted(extra)}. Add them to "
        "EXPECTED_TOOLS (and to WRITE_TOOLS if they change anything in CATS)."
    )


def test_every_write_tool_is_a_real_tool(api):
    """Guards the WRITE_TOOLS list itself against drift."""
    unknown = WRITE_TOOLS - set(api.TOOLS)
    assert not unknown, f"WRITE_TOOLS names tools that do not exist: {sorted(unknown)}"


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_tool_is_completely_wired(api, name):
    """Each tool needs a description, a schema and a callable async handler.

    A tool present in the dict but pointing at a handler that no longer exists
    fails at call time, in front of a user. This fails at push time instead.
    """
    tool = api.TOOLS.get(name)
    assert tool is not None, f"{name} is not registered"

    description = tool.get("description")
    assert isinstance(description, str) and description.strip(), (
        f"{name} has no description - Claude cannot tell when to use it"
    )

    handler = tool.get("handler")
    assert callable(handler), f"{name} has no callable handler"
    assert inspect.iscoroutinefunction(handler), (
        f"{name}'s handler is not async - the dispatcher awaits it and will crash"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_tool_schema_is_valid(api, name):
    """A malformed inputSchema makes a tool uncallable even though it is listed."""
    schema = api.TOOLS[name].get("inputSchema")
    assert isinstance(schema, dict), f"{name} has no inputSchema"
    assert schema.get("type") == "object", f"{name} inputSchema type must be 'object'"

    properties = schema.get("properties")
    assert isinstance(properties, dict), f"{name} inputSchema has no properties dict"

    required = schema.get("required", [])
    assert isinstance(required, list), f"{name} 'required' must be a list"
    unknown_required = [r for r in required if r not in properties]
    assert not unknown_required, (
        f"{name} requires argument(s) it does not define: {unknown_required}"
    )


def test_tools_list_matches_the_registry(api):
    """tools/list is what Claude actually sees. It must reflect TOOLS exactly."""
    advertised = {
        name: {"description": t["description"], "inputSchema": t["inputSchema"]}
        for name, t in api.TOOLS.items()
    }
    assert set(advertised) == set(api.TOOLS)
    for name, entry in advertised.items():
        assert entry["description"], f"{name} would be advertised with no description"
