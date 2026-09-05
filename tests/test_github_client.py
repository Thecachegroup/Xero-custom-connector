"""
The branch rule is the whole safety model, so it is the thing most worth
testing. This token can write to every repo Andrew owns, and those repos deploy
to Vercel where the Xero and Microsoft 365 credentials live. If a write ever
reaches main, a bad commit is in production in ninety seconds with nobody in
the loop.

Everything here is stubbed. No network, no token, no repo touched.
"""

import base64
import json

import pytest

from src.github_client import BranchProtected, GitHubClient, PROTECTED_BRANCHES


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text or json.dumps(self._payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


class FakeSession:
    """Records every call so a test can assert what did - and did not - happen."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("json"), kw.get("params")))
        for pattern, resp in self.responses.items():
            if pattern in url:
                return resp() if callable(resp) else resp
        return FakeResponse({})

    @property
    def methods_used(self):
        return {c[0] for c in self.calls}


@pytest.fixture
def gh():
    client = GitHubClient(token="fake-token")
    client._session = FakeSession()
    return client


# ---- the branch rule -----------------------------------------------------


@pytest.mark.parametrize("branch", sorted(PROTECTED_BRANCHES))
def test_commit_refuses_protected_branches(gh, branch):
    with pytest.raises(BranchProtected):
        gh.commit_files("Xero-custom-connector", branch, {"a.py": "x"}, "msg")


@pytest.mark.parametrize("branch", ["MAIN", "Main", "  main  ", "MASTER"])
def test_protection_is_not_fooled_by_case_or_padding(gh, branch):
    with pytest.raises(BranchProtected):
        gh.commit_files("Xero-custom-connector", branch, {"a.py": "x"}, "msg")


@pytest.mark.parametrize("branch", ["", "   ", None])
def test_a_missing_branch_is_refused(gh, branch):
    with pytest.raises(BranchProtected):
        gh.commit_files("Xero-custom-connector", branch, {"a.py": "x"}, "msg")


def test_refusal_happens_before_any_request(gh):
    """Refused, not attempted-then-warned."""
    with pytest.raises(BranchProtected):
        gh.commit_files("Xero-custom-connector", "main", {"a.py": "x"}, "msg")
    assert gh._session.calls == [], "a request was made toward a protected branch"


def test_open_pr_also_refuses_a_protected_head(gh):
    with pytest.raises(BranchProtected):
        gh.open_pr("Xero-custom-connector", "main", "title")
    assert gh._session.calls == []


def test_a_normal_branch_is_allowed(gh):
    assert gh._guard_branch("fix/rate-card") == "fix/rate-card"


# ---- repo allow-list -----------------------------------------------------


def test_unknown_repo_is_refused_by_name(gh):
    with pytest.raises(RuntimeError, match="Unknown repo"):
        gh.read_file("some-other-repo", "README.md")
    assert gh._session.calls == []


def test_known_repos_resolve(gh):
    assert gh._repo("cats-mcp-server").endswith("/Thecachegroup/cats-mcp-server")


# ---- token ---------------------------------------------------------------


def test_missing_token_says_what_to_do(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError) as e:
        GitHubClient()
    assert "GITHUB_TOKEN" in str(e.value)
    assert "redeploy" in str(e.value), "the redeploy step is the one people miss"


def test_token_is_sent_as_a_bearer(gh):
    assert gh._headers()["Authorization"] == "Bearer fake-token"


# ---- reads ---------------------------------------------------------------


def test_read_file_decodes_content(gh):
    text = "print('hello')\n"
    gh._session.responses = {
        "/contents/": FakeResponse(
            {"sha": "abc123", "size": len(text),
             "content": base64.b64encode(text.encode()).decode()}
        )
    }
    got = gh.read_file("Xero-custom-connector", "src/x.py")
    assert got["text"] == text
    assert got["sha"] == "abc123"


def test_read_file_on_a_directory_says_so(gh):
    gh._session.responses = {"/contents/": FakeResponse([{"name": "x"}])}
    with pytest.raises(RuntimeError, match="directory"):
        gh.read_file("Xero-custom-connector", "src")


def test_list_dir_returns_entries(gh):
    gh._session.responses = {
        "/contents/": FakeResponse(
            [{"name": "x.py", "path": "src/x.py", "type": "file", "size": 10}]
        )
    }
    entries = gh.list_dir("Xero-custom-connector", "src")
    assert entries[0]["name"] == "x.py"


def test_head_sha(gh):
    gh._session.responses = {"/commits/": FakeResponse({"sha": "deadbeef" * 5})}
    assert gh.head_sha("Xero-custom-connector").startswith("deadbeef")


# ---- committing ----------------------------------------------------------


def _commit_stubs():
    return {
        "/commits/": FakeResponse({"sha": "base111", "tree": {"sha": "tree000"}}),
        "/git/commits/": FakeResponse({"sha": "base111", "tree": {"sha": "tree000"}}),
        "/git/blobs": FakeResponse({"sha": "blob222"}),
        "/git/trees": FakeResponse({"sha": "tree333"}),
        "/git/refs": FakeResponse({"ref": "refs/heads/x"}),
    }


def test_commit_is_a_single_commit(gh):
    """Three files, one commit - a per-file loop that dies halfway leaves a
    branch holding half a change, and these functions have ten seconds."""
    stubs = _commit_stubs()
    stubs["/git/commits"] = FakeResponse({"sha": "commit444", "tree": {"sha": "tree333"}})
    gh._session.responses = stubs

    result = gh.commit_files(
        "Xero-custom-connector",
        "fix/thing",
        {"a.py": "1", "b.py": "2", "c.py": "3"},
        "fix the thing",
    )

    created_commits = [c for c in gh._session.calls
                       if c[0] == "POST" and c[1].endswith("/git/commits")]
    assert len(created_commits) == 1
    assert result["branch"] == "fix/thing"
    assert result["files"] == ["a.py", "b.py", "c.py"]


def test_commit_blobs_every_file(gh):
    stubs = _commit_stubs()
    stubs["/git/commits"] = FakeResponse({"sha": "commit444", "tree": {"sha": "tree333"}})
    gh._session.responses = stubs
    gh.commit_files("Xero-custom-connector", "fix/thing", {"a.py": "1", "b.py": "2"}, "m")
    blobs = [c for c in gh._session.calls if c[1].endswith("/git/blobs")]
    assert len(blobs) == 2


def test_commit_needs_files(gh):
    with pytest.raises(RuntimeError, match="[Nn]othing to commit"):
        gh.commit_files("Xero-custom-connector", "fix/x", {}, "m")


@pytest.mark.parametrize("message", ["", "   "])
def test_commit_needs_a_message(gh, message):
    with pytest.raises(RuntimeError, match="commit message"):
        gh.commit_files("Xero-custom-connector", "fix/x", {"a.py": "1"}, message)


# ---- CI ------------------------------------------------------------------

SHA = "a" * 40


def _runs_stubs(runs):
    """A branch name has to be resolved to a sha before runs can be queried,
    so the commits endpoint is stubbed alongside the runs endpoint."""
    return {
        "/commits/": FakeResponse({"sha": SHA}),
        "/actions/runs": FakeResponse({"workflow_runs": runs}),
    }


@pytest.mark.parametrize("runs,expected", [
    ([{"status": "completed", "conclusion": "success"}], "passed"),
    ([{"status": "completed", "conclusion": "failure"}], "FAILED - do not merge"),
    ([{"status": "in_progress", "conclusion": None}], "still running"),
    ([{"status": "completed", "conclusion": "success"},
      {"status": "completed", "conclusion": "failure"}], "FAILED - do not merge"),
])
def test_ci_verdicts(gh, runs, expected):
    gh._session.responses = _runs_stubs(runs)
    assert gh.checks_for("Xero-custom-connector", "abc")["verdict"] == expected


def test_no_runs_yet_is_not_a_pass(gh):
    """The dangerous answer would be 'passed' when nothing has run."""
    gh._session.responses = _runs_stubs([])
    verdict = gh.checks_for("Xero-custom-connector", "abc")["verdict"]
    assert "no runs yet" in verdict
    assert "pass" not in verdict.lower()


def test_a_branch_name_is_resolved_before_runs_are_queried(gh):
    """THE BUG THIS GUARDS.

    head_sha is an exact match on a commit sha. Passing a branch name matched
    nothing, so a branch whose CI had failed came back "no runs yet" - read as
    "CI has not started" when the truth was "CI failed". Found on main on
    05/09/2026 while those failures were actively sending email.
    """
    gh._session.responses = _runs_stubs(
        [{"status": "completed", "conclusion": "failure"}]
    )
    result = gh.checks_for("Xero-custom-connector", "test/some-branch")

    assert result["verdict"] == "FAILED - do not merge"

    queried = [c for c in gh._session.calls if "/actions/runs" in c[1]]
    assert len(queried) == 1
    assert queried[0][3]["head_sha"] == SHA, (
        "a branch name was sent as head_sha - it matches nothing and the "
        "verdict silently becomes 'no runs yet'"
    )


def test_the_ref_is_reported_back_intact(gh):
    """ref[:7] rendered 'test/connector-check' as 'test/co'."""
    gh._session.responses = _runs_stubs([])
    result = gh.checks_for("Xero-custom-connector", "test/connector-check")
    assert result["ref"] == "test/connector-check"
    assert result["sha"] == SHA[:7]


def test_a_full_sha_is_not_resolved_again(gh):
    """The common case - checking the commit you just made - costs one call."""
    gh._session.responses = _runs_stubs(
        [{"status": "completed", "conclusion": "success"}]
    )
    gh.checks_for("Xero-custom-connector", SHA)
    assert not [c for c in gh._session.calls if "/commits/" in c[1]], (
        "a 40-character sha was sent back to GitHub to be resolved"
    )


# ---- errors --------------------------------------------------------------


def test_404_explains_the_likely_cause(gh):
    gh._session.responses = {"/contents/": FakeResponse({}, status_code=404)}
    with pytest.raises(RuntimeError, match="scoped to"):
        gh.read_file("Xero-custom-connector", "nope.py")


def test_error_body_is_surfaced(gh):
    gh._session.responses = {
        "/contents/": FakeResponse({}, status_code=422, text="Validation failed: bad ref")
    }
    with pytest.raises(RuntimeError, match="Validation failed"):
        gh.read_file("Xero-custom-connector", "x.py")


# ---- targeted replace ----------------------------------------------------
#
# commit_files replaces a path outright. For a 156KB module that means
# reproducing 156KB to change one line, which is how a one-line auth fix ended
# up being typed into the GitHub web editor by hand on 05/09/2026.


FILE_TEXT = (
    "line one\n"
    "    if KEY and key != KEY:\n"
    "        raise HTTPException(401)\n"
    "line four\n"
    "line one\n"
)


def _replace_stubs(text=FILE_TEXT, branch_exists=False):
    stubs = _commit_stubs()
    stubs["/git/commits"] = FakeResponse({"sha": "commit444", "tree": {"sha": "tree333"}})
    stubs["/contents/"] = FakeResponse(
        {"sha": "filesha", "size": len(text),
         "content": base64.b64encode(text.encode()).decode()}
    )
    if not branch_exists:
        # head_sha on a branch that does not exist yet -> 404 from GitHub
        def commits(url_seen=[]):
            url_seen.append(1)
            if len(url_seen) == 1:
                return FakeResponse({}, status_code=404)
            return FakeResponse({"sha": "base111", "tree": {"sha": "tree000"}})
        stubs["/commits/"] = commits
    return stubs


def test_replace_refuses_main(gh):
    with pytest.raises(BranchProtected):
        gh.replace_in_file("Xero-custom-connector", "main", "a.py", "x", "y", "m")
    assert gh._session.calls == []


def test_replace_changes_only_the_matched_passage(gh):
    gh._session.responses = _replace_stubs()
    gh.replace_in_file(
        "Xero-custom-connector", "fix/auth", "api/index.py",
        "    if KEY and key != KEY:", "    if not KEY or not compare_digest(key, KEY):",
        "fail closed",
    )
    blobs = [c for c in gh._session.calls if c[1].endswith("/git/blobs")]
    assert len(blobs) == 1
    sent = blobs[0][2]["content"]
    assert "if not KEY or not compare_digest(key, KEY):" in sent
    assert "if KEY and key != KEY:" not in sent
    assert sent.startswith("line one\n"), "the rest of the file must survive untouched"
    assert sent.endswith("line four\nline one\n")


def test_replace_refuses_when_not_found(gh):
    gh._session.responses = _replace_stubs()
    with pytest.raises(RuntimeError, match="not found"):
        gh.replace_in_file(
            "Xero-custom-connector", "fix/x", "api/index.py", "nowhere", "y", "m"
        )


def test_replace_refuses_an_ambiguous_match(gh):
    """Two matches is a refusal. Picking one silently is worse than no tool."""
    gh._session.responses = _replace_stubs()
    with pytest.raises(RuntimeError, match="appears 2 times"):
        gh.replace_in_file(
            "Xero-custom-connector", "fix/x", "api/index.py", "line one", "y", "m"
        )


def test_replace_writes_nothing_on_an_ambiguous_match(gh):
    gh._session.responses = _replace_stubs()
    with pytest.raises(RuntimeError):
        gh.replace_in_file(
            "Xero-custom-connector", "fix/x", "api/index.py", "line one", "y", "m"
        )
    assert not [c for c in gh._session.calls if c[0] in ("POST", "PATCH")], (
        "a refused replace still wrote to the repo"
    )


@pytest.mark.parametrize("old,new", [("", "y"), ("same", "same")])
def test_replace_rejects_pointless_arguments(gh, old, new):
    with pytest.raises(RuntimeError):
        gh.replace_in_file("Xero-custom-connector", "fix/x", "a.py", old, new, "m")


def test_replace_stacks_on_an_existing_branch(gh):
    """Second edit must build on the branch, not reset it back to base."""
    gh._session.responses = _replace_stubs(branch_exists=True)
    result = gh.replace_in_file(
        "Xero-custom-connector", "fix/auth", "api/index.py",
        "line four", "line 4", "second edit",
    )
    assert result["built_on"] == "fix/auth"
