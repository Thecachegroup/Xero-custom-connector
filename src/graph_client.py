"""
Microsoft Graph client - read the payroll mailbox, write to OneDrive.

Deliberately mirrors xero_client.py: same client-credentials shape, same rate
limiter idea, same "attach the URL to the error" habit. If you can read one you
can read the other.

WHY THIS EXISTS. Contractors send their Linfox PPM timesheets as images pasted
into the email body, not as attachments. Those are `isInline` attachments, and
the standard Outlook/Power Automate connector routinely omits them - which is
precisely the file we need. Graph returns them from /attachments like any other,
with contentBytes, so this reads them directly.

AUTH. Client credentials (app-only). No user, no refresh token, nothing to go
stale. Set in Vercel:
    GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET
    TCG_PAYROLL_MAILBOX   default payroll@thecachegroup.com.au
    TCG_FILES_OWNER       the mailbox whose OneDrive holds Contractors/

SECURITY - READ THIS BEFORE GRANTING THE PERMISSION. Mail.Read as an APPLICATION
permission reads every mailbox in the tenant, not just the payroll one. Scope it
down with an Application Access Policy so this app can only open the payroll
mailbox:

    New-ApplicationAccessPolicy -AppId <GRAPH_CLIENT_ID> `
        -PolicyScopeGroupId payroll@thecachegroup.com.au `
        -AccessRight RestrictAccess `
        -Description "TCG payroll sweep - payroll mailbox only"

Without that policy this app can read Andrew's mail, and everyone else's.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphClient:
    def __init__(self, tenant_id: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None):
        try:
            self.tenant_id = tenant_id or os.environ["GRAPH_TENANT_ID"]
            self.client_id = client_id or os.environ["GRAPH_CLIENT_ID"]
            self.client_secret = client_secret or os.environ["GRAPH_CLIENT_SECRET"]
        except KeyError as e:
            raise RuntimeError(
                f"Missing environment variable {e.args[0]}. Set GRAPH_TENANT_ID, "
                "GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET from the Entra app "
                "registration. Nothing has been read."
            ) from None
        self.mailbox = os.environ.get("TCG_PAYROLL_MAILBOX", "payroll@thecachegroup.com.au")
        self.files_owner = os.environ.get("TCG_FILES_OWNER", "andrew.hurnard@thecachegroup.com.au")
        self._token: str | None = None
        self._expiry = 0.0
        self._lock = threading.Lock()
        self._session = requests.Session()

    # ---------- auth ----------

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expiry - 60:
                return self._token
            resp = requests.post(
                f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
                timeout=30,
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    "Microsoft rejected the client credentials. Check GRAPH_TENANT_ID, "
                    "GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET, and that admin consent "
                    f"has been granted for Mail.Read and Files.ReadWrite.All. "
                    f"Microsoft said: {resp.status_code} {resp.text[:300]}"
                )
            payload = resp.json()
            self._token = payload["access_token"]
            self._expiry = time.time() + int(payload.get("expires_in", 3600))
            return self._token

    # ---------- transport ----------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        for attempt in range(6):
            resp = self._session.request(method, url, headers={**self._headers(), **kw.pop("headers", {})},
                                         timeout=90, **kw)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "20")) + 1
                log.warning("429 from Graph; backing off %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                # The URL matters. A bare 404 from Graph says nothing about which
                # of mailbox / folder / message / drive was actually wrong.
                raise requests.HTTPError(
                    f"{resp.status_code} for {method} {url}: {resp.text[:300]}", response=resp
                )
            return resp
        raise RuntimeError(f"Graph request failed after retries: {url}")

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict:
        return self._request("GET", url, params=params or {}).json()

    def get_all(self, url: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Follow @odata.nextLink. Graph pages at 10 by default, silently."""
        out: list[dict] = []
        page = self.get(url, params)
        while True:
            out.extend(page.get("value", []))
            nxt = page.get("@odata.nextLink")
            if not nxt:
                return out
            page = self.get(nxt)

    # ---------- mail ----------

    def find_folder_id(self, name: str) -> str:
        """Resolve a mail folder by display name, searching child folders too.

        'Payroll - TCG' is a top-level folder today, but resolving by name rather
        than a hardcoded id means it survives being moved or recreated.
        """
        target = name.strip().lower()
        roots = self.get_all(f"{GRAPH}/users/{quote(self.mailbox)}/mailFolders",
                             {"$top": "100"})
        for f in roots:
            if str(f.get("displayName", "")).strip().lower() == target:
                return f["id"]
        for f in roots:
            if f.get("childFolderCount"):
                kids = self.get_all(
                    f"{GRAPH}/users/{quote(self.mailbox)}/mailFolders/{f['id']}/childFolders",
                    {"$top": "100"})
                for k in kids:
                    if str(k.get("displayName", "")).strip().lower() == target:
                        return k["id"]
        raise RuntimeError(
            f"No mail folder called {name!r} in {self.mailbox}. Nothing has been read."
        )

    def messages(self, folder: str, since: date | str, until: date | str | None = None) -> list[dict]:
        """Messages in a folder, newest first, with their attachments resolved.

        `since` should be generous - 30 to 45 days rather than the fortnight.
        People send early, late and out of order, and a message that falls
        outside the window is silently not filed.
        """
        s = since if isinstance(since, str) else since.isoformat()
        flt = f"receivedDateTime ge {s}T00:00:00Z"
        if until:
            u = until if isinstance(until, str) else until.isoformat()
            flt += f" and receivedDateTime le {u}T23:59:59Z"

        fid = self.find_folder_id(folder)
        raw = self.get_all(
            f"{GRAPH}/users/{quote(self.mailbox)}/mailFolders/{fid}/messages",
            {"$filter": flt, "$top": "50", "$orderby": "receivedDateTime desc",
             "$select": "id,subject,from,receivedDateTime,hasAttachments"},
        )

        out = []
        for m in raw:
            out.append({
                "id": m["id"],
                "subject": m.get("subject", ""),
                "sender": (m.get("from", {}).get("emailAddress", {}) or {}).get("address", ""),
                "received": m.get("receivedDateTime", ""),
                "attachments": self.attachments(m["id"]),
            })
        return out

    def attachments(self, message_id: str) -> list[dict]:
        """Every attachment INCLUDING inline images.

        hasAttachments is false on a message whose only attachments are inline,
        so it must not be used to decide whether to look. Always look.
        """
        items = self.get_all(
            f"{GRAPH}/users/{quote(self.mailbox)}/messages/{message_id}/attachments"
        )
        return [{
            "id": a.get("id"),
            "name": a.get("name", ""),
            "contentType": a.get("contentType", ""),
            "isInline": bool(a.get("isInline")),
            "size": a.get("size", 0),
        } for a in items if a.get("@odata.type", "").endswith("fileAttachment")]

    def attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        a = self.get(
            f"{GRAPH}/users/{quote(self.mailbox)}/messages/{message_id}"
            f"/attachments/{attachment_id}"
        )
        content = a.get("contentBytes")
        if not content:
            raise RuntimeError(
                f"Attachment {a.get('name')!r} returned no contentBytes. "
                "Nothing has been written."
            )
        return base64.b64decode(content)

    # ---------- files ----------

    def _drive_id(self) -> str:
        return self.get(f"{GRAPH}/users/{quote(self.files_owner)}/drive")["id"]

    def upload(self, relative_path: str, content: bytes, root: str = "Contractors/Timesheets") -> dict:
        """Write bytes to OneDrive under `root`, creating folders as needed.

        Uses the simple PUT upload, which Graph caps at 4MB. Timesheet
        screenshots run 60-200KB and contractor invoices under 1MB, so the cap
        is not in play - but it fails loudly rather than truncating if it ever is.
        """
        if len(content) > 4 * 1024 * 1024:
            raise RuntimeError(
                f"{relative_path} is {len(content)/1e6:.1f}MB, over the 4MB simple-upload "
                "limit. Needs a resumable upload session - not implemented, because "
                "nothing filed here has ever been that big. Nothing was written."
            )
        path = f"{root}/{relative_path}".strip("/")
        drive = self._drive_id()
        url = f"{GRAPH}/drives/{drive}/root:/{quote(path)}:/content"
        resp = self._request(
            "PUT", url, data=content,
            headers={"Content-Type": "application/octet-stream"},
        )
        return resp.json()

    def exists(self, relative_path: str, root: str = "Contractors/Timesheets") -> bool:
        """True if the file is already filed. Re-running a sweep should be safe."""
        path = f"{root}/{relative_path}".strip("/")
        try:
            self.get(f"{GRAPH}/drives/{self._drive_id()}/root:/{quote(path)}")
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return False
            raise
