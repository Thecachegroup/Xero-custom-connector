"""
Shared test fixtures for the CATS connector.

WHY THESE TESTS EXIST

On 3 September 2026 an old copy of the Xero connector's server.py was uploaded
over the live one. It silently removed a third of the functionality. /healthz
returned "ok" throughout, because the app started perfectly well - it was just
the wrong app. The regression stood in production for over an hour.

This connector is uploaded the same way, by dragging files into the GitHub web
UI, and until now it had no tests at all. It carries 54 tools against live
candidate data, bulk pipeline writes and outbound email. A truncated or stale
upload here costs candidate records, not an invoice line.

Every test in this suite runs with NO credentials and NO network. If a test
ever starts needing a real key, something under test has begun reaching the
live ATS, and that is the bug - not the missing secret.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_connector():
    """Load api/index.py as a module named `catsapi`.

    api/ is not a package (no __init__.py) because Vercel's Python runtime
    treats the directory as a function root, so a normal import will not find
    it. Loading it by path here keeps production layout untouched.
    """
    spec = importlib.util.spec_from_file_location(
        "catsapi", REPO_ROOT / "api" / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["catsapi"] = module
    spec.loader.exec_module(module)
    return module


catsapi = _load_connector()


@pytest.fixture
def api():
    """The loaded connector module."""
    return catsapi


@pytest.fixture
def no_network(monkeypatch):
    """Allow reads, make every WRITE fail loudly.

    A preview is allowed - and expected - to GET the current state so it can
    show what would change. What it must never do without confirm:true is
    POST, PUT or DELETE. Blocking reads too would only prove the preview is
    lazy; blocking writes proves the gate holds.
    """
    import httpx

    # cats_headers() refuses to build a request without a key. The stub
    # client below means nothing leaves the machine, so a placeholder is
    # enough to let read-modify-write previews run.
    monkeypatch.setattr("catsapi.CATS_API_KEY", "test-key-not-real")

    class ReadOnlyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return _StubResponse()

        async def post(self, *args, **kwargs):
            raise AssertionError(
                "POST attempted without confirm:true - the preview gate leaked."
            )

        async def put(self, *args, **kwargs):
            raise AssertionError(
                "PUT attempted without confirm:true - the preview gate leaked."
            )

        async def delete(self, *args, **kwargs):
            raise AssertionError(
                "DELETE attempted without confirm:true - the preview gate leaked."
            )

        async def request(self, method, *args, **kwargs):
            if str(method).upper() == "GET":
                return _StubResponse()
            raise AssertionError(
                f"{method} attempted without confirm:true - the preview gate leaked."
            )

    class _StubResponse:
        """A plausible CATS record.

        Read-modify-write previews GET the current object and read fields off
        it to show what would change. Returning a bare {} would fail them with
        a KeyError that says nothing about whether the preview gate holds.
        """

        status_code = 200
        text = "{}"
        content = b"{}"
        headers = {}

        def json(self):
            return {
                "id": 1,
                "title": "Existing Title",
                "notes": "Existing notes",
                "first_name": "Existing",
                "last_name": "Candidate",
                "status": {"id": 1, "title": "Existing Status"},
                "company_id": 1,
                "total": 0,
                "count": 0,
                "page": 1,
                "_embedded": {},
            }

    monkeypatch.setattr(httpx, "AsyncClient", ReadOnlyClient)
    return ReadOnlyClient
