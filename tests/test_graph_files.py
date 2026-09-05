"""Offline tests for graph_files path handling and guards. No network."""
# The module lives at src/graph_files.py. A bare "import graph_files" raised
# ModuleNotFoundError at COLLECTION time, which aborts the whole pytest run -
# so from 3 September 2026 the CI ran no tests at all while showing a red cross
# that read as "tests failed" rather than "tests never started".
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src import graph_files as g

fails = []


def check(label, got, want):
    ok = got == want
    print(("PASS  " if ok else "FAIL  ") + label + "  ->  " + repr(got))
    if not ok:
        fails.append((label, got, want))


check(
    "backslash path normalised",
    g._clean("CONTRACTOR AGREEMENTS\\Devinia Liddelow\\brief.docx"),
    "CONTRACTOR AGREEMENTS/Devinia Liddelow/brief.docx",
)
check("leading slash stripped", g._clean("/AI Working Folder/x.pdf"), "AI Working Folder/x.pdf")
check("trailing slash stripped", g._clean("Employsure documents/"), "Employsure documents")

for bad in ["", "   ", "a/../b"]:
    try:
        g._clean(bad)
        print("FAIL  accepted bad path " + repr(bad))
        fails.append(("bad path accepted", bad, "ValueError"))
    except ValueError as e:
        print("PASS  rejected " + repr(bad) + "  ->  " + str(e))

check("empty root ''", g._safe_empty(""), True)
check("empty root '/'", g._safe_empty("/"), True)
check("not empty 'x'", g._safe_empty("x"), False)

try:
    g.upload_url("x", "clobber")
    print("FAIL  bad conflict value accepted")
    fails.append(("conflict guard", "accepted", "ValueError"))
except ValueError as e:
    print("PASS  conflict guard  ->  " + str(e))

tail = g._item_url("AI Working Folder/a b.pdf").split("/root:")[1]
check("url-encodes spaces", tail, "/AI%20Working%20Folder/a%20b.pdf")

import os
saved = {k: os.environ.pop(k, None) for k in g._TENANT_KEYS + g._CLIENT_KEYS + g._SECRET_KEYS}
g._token_cache["value"] = None
g._token_cache["expires"] = 0
try:
    g._token()
    print("FAIL  missing credentials did not raise")
    fails.append(("credential error", "silent", "RuntimeError"))
except RuntimeError as e:
    msg = str(e)
    ok = "GRAPH_TENANT_ID" in msg and "GRAPH_CLIENT_SECRET" in msg
    print(("PASS  " if ok else "FAIL  ") + "missing credentials names what it looked for")
    if not ok:
        fails.append(("credential error text", msg, "names env vars"))
finally:
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v

print()
print("FAILURES: " + str(len(fails)))


def test_no_failures():
    """This file reports by printing, and pytest never reads stdout.

    Without this, every check above could fail and the run would still be green.
    """
    assert not fails, fails
