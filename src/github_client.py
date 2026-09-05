"""
GitHub client - read the connector repos, commit to a branch, open a PR.

WHY THIS EXISTS. Every change to these connectors used to travel the same way:
Claude builds a zip, Andrew downloads it, unpacks it, drags the folders into
GitHub's web uploader, and says "go and test it". That loop is slow, it leaves
near-identical zips lying around, and an upload replaces whole files - so a
package cut against one commit dropped onto a newer one silently reverts
whatever landed in between. That is exactly what happened on 3 September 2026.

With this, the connector edits its own repos directly. No zip, no upload, and
git tracks what changed instead of a filename convention trying to.

AUTH. One fine-grained personal access token, set in Vercel:
    GITHUB_TOKEN    Contents, Pull requests, Workflows, Issues: read/write
                    Actions: read. Metadata: read (automatic).

SECURITY - WHY NOTHING HERE CAN PUSH TO MAIN. This token can write to every
repo Andrew owns, and those repos deploy straight to Vercel, where the Xero and
Microsoft 365 credentials live. A bad commit on main is live in ninety seconds
with nobody in the loop.

So every write in this module goes to a branch and stops. PROTECTED_BRANCHES is
checked on the way in and the call is refused before a request is made - not
warned about, refused. Merging is a human clicking a button on a pull request
whose tests have already run. That is the whole safety model: the token is
broad, the blast radius is one branch.

A connector that can rewrite its own source can also break itself badly enough
that it can no longer be used to fix itself. The branch rule is what stops that
being a one-way door.
"""

from __future__ import annotations

import base64
import logging
import os

import requests

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_OWNER = "Thecachegroup"

# Never written to directly, on any repo, by any tool in this module.
PROTECTED_BRANCHES = {"main", "master", "trunk", "release", "production"}

# Repos this connector is allowed to touch. An allow-list rather than "whatever
# the token can reach", so a typo'd repo name fails loudly instead of creating
# a branch somewhere unexpected.
KNOWN_REPOS = {
    "Xero-custom-connector",
    "cats-mcp-server",
    "ms365-mailer",
    "cv-suite",
    "cv-suite-full",
    "CV-Suite-Free",
}

TIMEOUT = 20


class BranchProtected(RuntimeError):
    """Raised when a write targets a branch that must only change via a PR."""


class GitHubClient:
    def __init__(self, token: str | None = None, owner: str = DEFAULT_OWNER):
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        if not self.token:
            raise RuntimeError(
                "GITHUB_TOKEN is not set. Add it to the Vercel environment "
                "(Settings > Environment Variables) and redeploy - environment "
                "variables only reach the running code on a fresh deploy."
            )
        self.owner = owner
        self._session = requests.Session()

    # -- plumbing ---------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method: str, path: str, **kw) -> dict | list:
        url = f"{GITHUB_API}{path}"
        resp = self._session.request(
            method, url, headers=self._headers(), timeout=TIMEOUT, **kw
        )
        if resp.status_code == 404:
            raise RuntimeError(
                f"GitHub returned 404 for {method} {path}. Either the path is "
                f"wrong or the token cannot see it - a fine-grained token only "
                f"reaches the repositories it was scoped to."
            )
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            raise RuntimeError(f"GitHub rate limit hit on {method} {path}.")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"GitHub {resp.status_code} on {method} {path}: {resp.text[:400]}"
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def _repo(self, repo: str) -> str:
        if repo not in KNOWN_REPOS:
            raise RuntimeError(
                f"Unknown repo '{repo}'. Known: {', '.join(sorted(KNOWN_REPOS))}. "
                f"Add it to KNOWN_REPOS if this is deliberate."
            )
        return f"/repos/{self.owner}/{repo}"

    @staticmethod
    def _guard_branch(branch: str) -> str:
        """The one rule this module exists to enforce."""
        name = (branch or "").strip()
        if not name:
            raise BranchProtected("No branch given. Writes must name a branch.")
        if name.lower() in PROTECTED_BRANCHES:
            raise BranchProtected(
                f"Refusing to write directly to '{name}'. Commit to a new branch "
                f"and open a pull request - that is what keeps a bad change out "
                f"of production until someone has looked at it."
            )
        return name

    # -- reads ------------------------------------------------------------

    def head_sha(self, repo: str, ref: str = "main") -> str:
        data = self._request("GET", f"{self._repo(repo)}/commits/{ref}")
        return data["sha"]

    def read_file(self, repo: str, path: str, ref: str = "main") -> dict:
        data = self._request(
            "GET", f"{self._repo(repo)}/contents/{path}", params={"ref": ref}
        )
        if isinstance(data, list):
            raise RuntimeError(f"{path} is a directory - use list_dir.")
        content = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
        return {"path": path, "sha": data["sha"], "size": data.get("size"), "text": content}

    def list_dir(self, repo: str, path: str = "", ref: str = "main") -> list:
        data = self._request(
            "GET", f"{self._repo(repo)}/contents/{path}", params={"ref": ref}
        )
        if isinstance(data, dict):
            raise RuntimeError(f"{path} is a file - use read_file.")
        return [
            {"name": i["name"], "path": i["path"], "type": i["type"], "size": i.get("size")}
            for i in data
        ]

    # -- writes (branch only) ---------------------------------------------

    def commit_files(
        self,
        repo: str,
        branch: str,
        files: dict,
        message: str,
        base: str = "main",
    ) -> dict:
        """Commit every file in one commit, on a branch, creating it if needed.

        Uses the Git Data API - blobs, then a tree, then one commit - rather
        than a PUT per file. On a serverless function with a ten-second budget,
        a per-file loop that dies halfway leaves a branch holding half a change;
        this way the branch either moves or it does not.

        `files` maps repo path -> file text. A path set to None deletes it.
        """
        branch = self._guard_branch(branch)
        if not files:
            raise RuntimeError("No files given - nothing to commit.")
        if not message or not message.strip():
            raise RuntimeError("A commit message is required.")

        repo_path = self._repo(repo)
        base_sha = self.head_sha(repo, base)
        base_commit = self._request("GET", f"{repo_path}/git/commits/{base_sha}")
        base_tree = base_commit["tree"]["sha"]

        tree_entries = []
        for path, text in files.items():
            if text is None:
                tree_entries.append(
                    {"path": path, "mode": "100644", "type": "blob", "sha": None}
                )
                continue
            blob = self._request(
                "POST",
                f"{repo_path}/git/blobs",
                json={"content": text, "encoding": "utf-8"},
            )
            tree_entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            )

        tree = self._request(
            "POST",
            f"{repo_path}/git/trees",
            json={"base_tree": base_tree, "tree": tree_entries},
        )
        commit = self._request(
            "POST",
            f"{repo_path}/git/commits",
            json={"message": message, "tree": tree["sha"], "parents": [base_sha]},
        )

        ref = f"refs/heads/{branch}"
        try:
            self._request(
                "POST", f"{repo_path}/git/refs", json={"ref": ref, "sha": commit["sha"]}
            )
            created = True
        except RuntimeError:
            # Branch already exists - move it. force=True because the branch was
            # built from base in this same call, so there is nothing on it worth
            # keeping; a protected branch never reaches here.
            self._request(
                "PATCH",
                f"{repo_path}/git/{ref}",
                json={"sha": commit["sha"], "force": True},
            )
            created = False

        return {
            "repo": repo,
            "branch": branch,
            "created_branch": created,
            "commit": commit["sha"][:7],
            "base": base_sha[:7],
            "files": sorted(files),
        }

    def open_pr(
        self, repo: str, branch: str, title: str, body: str = "", base: str = "main"
    ) -> dict:
        branch = self._guard_branch(branch)
        pr = self._request(
            "POST",
            f"{self._repo(repo)}/pulls",
            json={"title": title, "body": body, "head": branch, "base": base},
        )
        return {
            "number": pr["number"],
            "url": pr["html_url"],
            "state": pr["state"],
            "branch": branch,
        }

    # -- CI ---------------------------------------------------------------

    def checks_for(self, repo: str, ref: str) -> dict:
        """Whether the test suite passed on a commit.

        The point of the whole exercise: a red cross here is the signal
        /healthz can never give.
        """
        runs = self._request(
            "GET",
            f"{self._repo(repo)}/actions/runs",
            params={"head_sha": ref, "per_page": 10},
        )
        out = []
        for run in runs.get("workflow_runs", []):
            out.append(
                {
                    "name": run.get("name"),
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "url": run.get("html_url"),
                }
            )
        if not out:
            return {"ref": ref[:7], "runs": [], "verdict": "no runs yet - check again shortly"}
        conclusions = {r["conclusion"] for r in out}
        if None in conclusions or any(r["status"] != "completed" for r in out):
            verdict = "still running"
        elif conclusions == {"success"}:
            verdict = "passed"
        else:
            verdict = "FAILED - do not merge"
        return {"ref": ref[:7], "runs": out, "verdict": verdict}
