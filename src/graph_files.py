"""
graph_files.py — file plumbing for the TCG Xero/payroll connector.

WHY THIS EXISTS
---------------
An unattended Claude session (scheduled task, no laptop, no Chrome) can read
Andrew's email as text but cannot get FILE BYTES in or out:

  * the Claude M365 connector returns attachments as extracted text, never bytes
  * sharepoint_upload_file 403s (Files.ReadWrite.All not consented on THAT app)
  * outlook_forward_mail refuses any message carrying an attachment

This connector's app registration already holds Files.ReadWrite.All and already
resolves Andrew's OneDrive (proved by graph_diagnostics). So the plumbing goes
here, not on the mailer.

DESIGN — NO BYTES THROUGH THE CONVERSATION
------------------------------------------
These tools never return file content. They return short-lived, PRE-AUTHENTICATED
URLs that Claude's sandbox fetches or PUTs directly. A 5 MB PDF costs four lines
of tool output instead of seven million characters of base64.

  download: Graph "@microsoft.graph.downloadUrl"  — ~1 hour, no auth header
  upload:   Graph createUploadSession "uploadUrl" — ~15 min, no auth header

All document work (signing, .docx editing) stays in the sandbox where the
toolchain is tested. This module moves files. It does not know what a contract is.

DEPENDS ON: requests  (already in the connector; msal optional, see _token)
"""

from __future__ import annotations

import os
import time
import base64
import threading
from typing import Optional

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
TIMEOUT = 60

# Andrew's OneDrive is the file store. Same value graph_diagnostics reports as
# "Files owner". Override with GRAPH_FILES_OWNER if that ever changes.
FILES_OWNER = os.environ.get(
    "GRAPH_FILES_OWNER", "andrew.hurnard@thecachegroup.com.au"
)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
# Self-contained on purpose. This module does not import from the rest of the
# connector, so it cannot be broken by a refactor elsewhere, and it can be
# dropped in and tested on its own. It reads the SAME app credentials the
# connector already uses; the name variants below cover the usual spellings so
# it works without anyone having to hunt through Vercel env settings.

_TENANT_KEYS = ("GRAPH_TENANT_ID", "AZURE_TENANT_ID", "MS_TENANT_ID", "TENANT_ID")
_CLIENT_KEYS = ("GRAPH_CLIENT_ID", "AZURE_CLIENT_ID", "MS_CLIENT_ID", "CLIENT_ID")
_SECRET_KEYS = (
    "GRAPH_CLIENT_SECRET",
    "AZURE_CLIENT_SECRET",
    "MS_CLIENT_SECRET",
    "CLIENT_SECRET",
)

_token_cache = {"value": None, "expires": 0.0}
_token_lock = threading.Lock()


def _first_env(keys) -> Optional[str]:
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return None


def _token() -> str:
    """App-only Graph token, cached until 60s before expiry."""
    with _token_lock:
        if _token_cache["value"] and time.time() < _token_cache["expires"]:
            return _token_cache["value"]

        tenant = _first_env(_TENANT_KEYS)
        client = _first_env(_CLIENT_KEYS)
        secret = _first_env(_SECRET_KEYS)

        missing = []
        if not tenant:
            missing.append("tenant id (tried: %s)" % ", ".join(_TENANT_KEYS))
        if not client:
            missing.append("client id (tried: %s)" % ", ".join(_CLIENT_KEYS))
        if not secret:
            missing.append("client secret (tried: %s)" % ", ".join(_SECRET_KEYS))
        if missing:
            raise RuntimeError(
                "Graph credentials not found in the environment.\n  "
                + "\n  ".join(missing)
                + "\nSet the names above in Vercel, or add the names this "
                "connector actually uses to _TENANT_KEYS/_CLIENT_KEYS/"
                "_SECRET_KEYS at the top of graph_files.py."
            )

        r = requests.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "client_id": client,
                "client_secret": secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Token request failed {r.status_code}: {r.text[:400]}")
        payload = r.json()
        _token_cache["value"] = payload["access_token"]
        _token_cache["expires"] = time.time() + int(payload.get("expires_in", 3600)) - 60
        return _token_cache["value"]


def _headers(json_body: bool = False) -> dict:
    h = {"Authorization": f"Bearer {_token()}"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _clean(path: str) -> str:
    """Normalise a OneDrive path to the form Graph wants.

    Accepts back or forward slashes and a leading slash, so the paths Andrew
    already uses with attach_from_onedrive work unchanged, e.g.
        "CONTRACTOR AGREEMENTS/Devinia Liddelow/Consultancy Brief.docx"
    """
    p = (path or "").replace("\\", "/").strip().strip("/")
    if not p:
        raise ValueError("Empty OneDrive path.")
    if ".." in p.split("/"):
        raise ValueError(f"Path may not contain '..': {path}")
    return p


def _item_url(path: str) -> str:
    from urllib.parse import quote

    return f"{GRAPH}/users/{FILES_OWNER}/drive/root:/{quote(_clean(path))}"


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def download_url(path: str) -> dict:
    """Pre-authenticated download URL for a OneDrive file. Valid about an hour."""
    r = requests.get(
        _item_url(path)
        + "?select=id,name,size,lastModifiedDateTime,@microsoft.graph.downloadUrl",
        headers=_headers(),
        timeout=TIMEOUT,
    )
    if r.status_code == 404:
        raise FileNotFoundError(f"Not in {FILES_OWNER}'s OneDrive: {path}")
    if r.status_code != 200:
        raise RuntimeError(f"Graph {r.status_code} reading '{path}': {r.text[:400]}")

    item = r.json()
    url = item.get("@microsoft.graph.downloadUrl")
    if not url:
        raise RuntimeError(
            f"No download URL returned for '{path}' — is it a folder rather than a file?"
        )
    return {
        "path": _clean(path),
        "name": item.get("name"),
        "size": item.get("size"),
        "last_modified": item.get("lastModifiedDateTime"),
        "download_url": url,
        "expires_note": "Pre-authenticated, roughly one hour. Fetch it now, no auth header.",
    }


def upload_url(dest_path: str, conflict: str = "replace") -> dict:
    """Pre-authenticated upload URL for a new or replacement OneDrive file.

    conflict: replace | rename | fail
    """
    if conflict not in ("replace", "rename", "fail"):
        raise ValueError("conflict must be replace, rename or fail")

    r = requests.post(
        _item_url(dest_path) + ":/createUploadSession",
        headers=_headers(json_body=True),
        json={"item": {"@microsoft.graph.conflictBehavior": conflict}},
        timeout=TIMEOUT,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"Graph {r.status_code} opening upload session for '{dest_path}': {r.text[:400]}"
        )

    session = r.json()
    return {
        "path": _clean(dest_path),
        "upload_url": session["uploadUrl"],
        "expires": session.get("expirationDateTime"),
        "how": (
            "Single PUT with Content-Range. For a file of N bytes:\n"
            "  curl -s -X PUT '<upload_url>' \\\n"
            "    -H 'Content-Length: N' \\\n"
            "    -H 'Content-Range: bytes 0-<N-1>/N' \\\n"
            "    --data-binary @file\n"
            "No auth header. 201/200 means written. Files over ~60 MB need chunking."
        ),
    }


def save_mail_attachment(
    message_id: str,
    dest_path: str,
    attachment_id: Optional[str] = None,
    mailbox: Optional[str] = None,
) -> dict:
    """Copy an Outlook attachment straight into OneDrive. Bytes stay server-side.

    This is the step nothing else can do: it is the only way a returned signed
    brief gets out of the mailbox when no one is at the laptop.

    attachment_id may be omitted when the message carries exactly one file
    attachment. mailbox defaults to FILES_OWNER.

    NOTE: the app's mail permission is scoped by the Exchange application access
    policy. If that policy names only payrollmb@, this raises 403 for any other
    mailbox until the policy is widened — see README, step 3.
    """
    box = mailbox or FILES_OWNER
    base = f"{GRAPH}/users/{box}/messages/{message_id}/attachments"

    if attachment_id is None:
        r = requests.get(
            base + "?$select=id,name,contentType,size,isInline",
            headers=_headers(),
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"Graph {r.status_code} listing attachments on {box}: {r.text[:400]}"
            )
        files = [
            a
            for a in r.json().get("value", [])
            if not a.get("isInline")
            and a.get("@odata.type", "").endswith("fileAttachment")
        ]
        if not files:
            raise RuntimeError("No file attachments on that message.")
        if len(files) > 1:
            listing = "\n  ".join(f"{a['id']}  {a['name']}  {a.get('size')}B" for a in files)
            raise RuntimeError(
                f"{len(files)} attachments — pass attachment_id:\n  {listing}"
            )
        attachment_id = files[0]["id"]

    r = requests.get(f"{base}/{attachment_id}", headers=_headers(), timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(
            f"Graph {r.status_code} reading attachment from {box}: {r.text[:400]}"
        )
    att = r.json()
    raw = att.get("contentBytes")
    if not raw:
        raise RuntimeError(
            f"'{att.get('name')}' returned no contentBytes — item attachments "
            "(a forwarded email rather than a file) are not supported."
        )
    blob = base64.b64decode(raw)

    session = upload_url(dest_path, conflict="replace")
    put = requests.put(
        session["upload_url"],
        data=blob,
        headers={
            "Content-Length": str(len(blob)),
            "Content-Range": f"bytes 0-{len(blob) - 1}/{len(blob)}",
        },
        timeout=300,
    )
    if put.status_code not in (200, 201):
        raise RuntimeError(f"Upload failed {put.status_code}: {put.text[:400]}")

    written = put.json()
    return {
        "saved_to": session["path"],
        "source_name": att.get("name"),
        "bytes": len(blob),
        "web_url": written.get("webUrl"),
        "next": "Attach it with send_email(attach_from_onedrive=['%s'])." % session["path"],
    }


def list_folder(path: str = "") -> dict:
    """List a OneDrive folder — names, sizes, modified dates. Read-only."""
    if _safe_empty(path):
        url = f"{GRAPH}/users/{FILES_OWNER}/drive/root/children"
    else:
        url = _item_url(path) + ":/children"
    r = requests.get(
        url + "?$select=name,size,lastModifiedDateTime,folder,file&$top=200",
        headers=_headers(),
        timeout=TIMEOUT,
    )
    if r.status_code == 404:
        raise FileNotFoundError(f"No such folder: {path or '(root)'}")
    if r.status_code != 200:
        raise RuntimeError(f"Graph {r.status_code} listing '{path}': {r.text[:400]}")

    entries = []
    for c in r.json().get("value", []):
        entries.append(
            {
                "name": c["name"],
                "kind": "folder" if "folder" in c else "file",
                "size": c.get("size"),
                "modified": c.get("lastModifiedDateTime"),
            }
        )
    entries.sort(key=lambda e: (e["kind"] != "folder", e["name"].lower()))
    return {"path": _clean(path) if not _safe_empty(path) else "", "entries": entries}


def _safe_empty(p: Optional[str]) -> bool:
    return not (p or "").replace("\\", "/").strip().strip("/")


def selftest() -> str:
    """Prove auth, drive access, write and read-back. Leaves nothing behind."""
    lines = []
    try:
        _token()
        lines.append("token           OK")
    except Exception as e:
        return f"token           FAIL — {e}"

    try:
        root = list_folder("")
        lines.append(f"drive access    OK ({len(root['entries'])} items at root)")
    except Exception as e:
        lines.append(f"drive access    FAIL — {e}")
        return "\n".join(lines)

    probe = "AI Working Folder/_graph_files_selftest.txt"
    try:
        s = upload_url(probe, conflict="replace")
        payload = b"graph_files selftest - safe to delete"
        put = requests.put(
            s["upload_url"],
            data=payload,
            headers={
                "Content-Length": str(len(payload)),
                "Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}",
            },
            timeout=TIMEOUT,
        )
        lines.append(
            "write           OK" if put.status_code in (200, 201)
            else f"write           FAIL — {put.status_code} {put.text[:200]}"
        )
    except Exception as e:
        lines.append(f"write           FAIL — {e}")

    try:
        d = download_url(probe)
        got = requests.get(d["download_url"], timeout=TIMEOUT).content
        lines.append(
            "read back       OK" if got == payload
            else f"read back       FAIL — got {got[:60]!r}"
        )
    except Exception as e:
        lines.append(f"read back       FAIL — {e}")

    try:
        requests.delete(_item_url(probe), headers=_headers(), timeout=TIMEOUT)
        lines.append("cleanup         OK")
    except Exception:
        lines.append(f"cleanup         left {probe} behind — delete it by hand")

    return "\n".join(lines)
