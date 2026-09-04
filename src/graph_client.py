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
    TCG_PAYROLL_MAILBOX   the SHARED MAILBOX payrollmb@thecachegroup.com.au.
                          payroll@ is a distribution group - it delivers INTO
                          payrollmb@ but cannot itself be read or sent from.
    TCG_PAYROLL_FOLDER    default 'Payroll - TCG'; falls back to Inbox if absent
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


def _addresses(recipients) -> list[str]:
    """Plain address strings out of Graph's recipient objects."""
    out = []
    for r in recipients or []:
        a = ((r or {}).get("emailAddress") or {}).get("address")
        if a:
            out.append(str(a).strip().lower())
    return out


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
        self.folder = os.environ.get("TCG_PAYROLL_FOLDER", "Payroll - TCG")
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

    # Graph accepts these as folder ids directly, no lookup needed.
    WELL_KNOWN = {"inbox", "archive", "sentitems", "drafts", "deleteditems"}

    def find_folder_id(self, name: str) -> str:
        """Resolve a mail folder by display name, searching child folders too.

        'Payroll - TCG' is a top-level folder today, but resolving by name rather
        than a hardcoded id means it survives being moved or recreated.
        """
        target = name.strip().lower()
        if target.replace(" ", "") in self.WELL_KNOWN:
            return target.replace(" ", "")
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

    def resolve_folder(self, name: str) -> tuple[str, str]:
        """(folder id, what to call it). Falls back to the Inbox, and says so.

        A dedicated payroll mailbox has no 'Payroll - TCG' folder - everything
        arrives in its Inbox, because the folder only ever existed as a rule in
        Andrew's own mailbox. Falling back is correct for a mailbox that exists
        solely for payroll, but it is never silent: the caller prints which
        folder was actually read, so nobody mistakes "read the wrong place" for
        "nobody sent anything".
        """
        try:
            return self.find_folder_id(name), name
        except RuntimeError:
            return "inbox", f"Inbox (no folder called {name!r} in this mailbox)"

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

        fid, _ = self.resolve_folder(folder)
        raw = self.get_all(
            f"{GRAPH}/users/{quote(self.mailbox)}/mailFolders/{fid}/messages",
            {"$filter": flt, "$top": "50", "$orderby": "receivedDateTime desc",
             "$select": "id,subject,from,toRecipients,ccRecipients,replyTo,"
                        "receivedDateTime,hasAttachments,bodyPreview"},
        )

        out = []
        for m in raw:
            frm = (m.get("from", {}) or {}).get("emailAddress", {}) or {}
            out.append({
                "id": m["id"],
                "subject": m.get("subject", ""),
                "sender": frm.get("address", ""),
                # The display name is weak evidence on its own - the sender sets
                # it - but it is the only thing carrying a name on an address
                # like pjs.ucanemailme@gmail.com.
                "sender_name": frm.get("name", ""),
                # Reckon and MYOB send on a contractor's behalf and put the
                # contractor in the CC. Without these the sweep sees only the
                # billing system.
                "cc": _addresses(m.get("ccRecipients")),
                "reply_to": _addresses(m.get("replyTo")),
                "received": m.get("receivedDateTime", ""),
                "body": m.get("bodyPreview", ""),
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

    def list_files(self, relative_path: str, root: str = "Contractors/Timesheets",
                   recursive: bool = False) -> list[dict]:
        """Everything filed under a folder. Returns Graph driveItems.

        Each item gains a 'path' key relative to RELATIVE_PATH - the folder that
        was asked for - not to `root`. The caller needs to know which contractor
        folder a file came out of, and the folder is what identifies the person;
        the filename prefix is initials and two people can share those.
        """
        drive = self._drive_id()

        def walk(rel: str, sub: str) -> list[dict]:
            path = f"{root}/{rel}".strip("/")
            url = f"{GRAPH}/drives/{drive}/root:/{quote(path)}:/children"
            out: list[dict] = []
            for it in self.get_all(url, {"$top": "200"}):
                child_sub = f"{sub}/{it['name']}".strip("/")
                # Presence, not truthiness. Graph sends {"childCount": 0} for an
                # empty folder, which is falsy - and an empty contractor folder is
                # normal, so testing the value silently turns folders into files.
                if "folder" in it:
                    if recursive:
                        out += walk(f"{rel}/{it['name']}".strip("/"), child_sub)
                else:
                    it["path"] = child_sub
                    out.append(it)
            return out

        return walk(relative_path, "")

    def download(self, relative_path: str, root: str = "Contractors/Timesheets") -> bytes:
        """Read a filed document back out of OneDrive."""
        path = f"{root}/{relative_path}".strip("/")
        url = f"{GRAPH}/drives/{self._drive_id()}/root:/{quote(path)}:/content"
        return self._request("GET", url).content

    def message_body(self, message_id: str) -> str:
        """The full body of one message, for when bodyPreview is not enough.

        Fetched per message and only when needed - a forward whose quoted header
        block sits below a long signature. Selecting bodies for every message in
        a 45-day sweep would multiply the payload for no benefit.
        """
        data = self.get(f"{GRAPH}/users/{quote(self.mailbox)}/messages/{message_id}",
                        {"$select": "body"})
        return str((data.get("body") or {}).get("content") or "")

    def message_mime(self, message_id: str) -> bytes:
        """The whole message as RFC-822 bytes, for a timesheet that IS the email.

        Devinia Liddelow types her hours into the body as a table and attaches
        nothing at all. There is no file to download, so the message itself is
        the document - saved verbatim, exactly as she sent it, rather than
        retyped into a PDF by us. Andrew: "if I just save the file, it is
        exactly what the person has sent me."
        """
        resp = self._request(
            "GET",
            f"{GRAPH}/users/{quote(self.mailbox)}/messages/{message_id}/$value",
            headers={"Accept": "*/*"},
        )
        if not resp.content:
            raise RuntimeError(
                f"Message {message_id} returned no MIME content. "
                "Nothing has been written."
            )
        return resp.content

    # ---------- files: pre-authenticated URLs ----------

    def download_url(self, relative_path: str, root: str = "") -> dict:
        """A pre-authenticated URL for one OneDrive file. Valid about an hour.

        Returns the URL, never the bytes. Graph's @microsoft.graph.downloadUrl
        needs no Authorization header, so whoever holds it can fetch the file
        directly - which is the point: bytes that never enter a conversation
        cost nothing to move and cannot be truncated on the way.

        ROOT defaults to "" - the drive root - not to Contractors/Timesheets.
        A contract does not live in the timesheet tree, and defaulting to it
        here would file signed agreements in the wrong place forever.
        """
        path = f"{root}/{relative_path}".strip("/") if root else relative_path.strip("/")
        url = f"{GRAPH}/drives/{self._drive_id()}/root:/{quote(path)}"
        item = self.get(url, {"$select": "id,name,size,lastModifiedDateTime,"
                                         "@microsoft.graph.downloadUrl"})
        link = item.get("@microsoft.graph.downloadUrl")
        if not link:
            raise RuntimeError(
                f"No download URL for {path!r} - a folder rather than a file? "
                "Nothing has been read."
            )
        return {
            "path": path,
            "name": item.get("name"),
            "size": item.get("size"),
            "last_modified": item.get("lastModifiedDateTime"),
            "download_url": link,
        }

    def upload_url(self, relative_path: str, root: str = "",
                   conflict: str = "replace") -> dict:
        """A pre-authenticated upload URL. Valid about fifteen minutes.

        upload() is a simple PUT and refuses anything over 4MB. This opens a
        resumable session instead, so a scanned contract that runs to 12MB goes
        through the same way a 200KB timesheet does.

        CONFLICT: replace | rename | fail. Use 'fail' for anything signed -
        quietly overwriting an executed document is not recoverable.
        """
        if conflict not in ("replace", "rename", "fail"):
            raise ValueError(
                f"conflict must be replace, rename or fail, not {conflict!r}. "
                "Nothing has been written."
            )
        path = f"{root}/{relative_path}".strip("/") if root else relative_path.strip("/")
        url = f"{GRAPH}/drives/{self._drive_id()}/root:/{quote(path)}:/createUploadSession"
        resp = self._request(
            "POST", url,
            json={"item": {"@microsoft.graph.conflictBehavior": conflict}},
        )
        session = resp.json()
        return {
            "path": path,
            "upload_url": session["uploadUrl"],
            "expires": session.get("expirationDateTime"),
        }

    # ---------- mail -> files ----------

    def save_mail_attachment(self, message_id: str, dest_path: str,
                             attachment_id: str | None = None,
                             mailbox: str | None = None,
                             root: str = "") -> dict:
        """Copy an Outlook attachment straight into OneDrive, server-side.

        MAILBOX defaults to self.mailbox - the payroll mailbox - so every
        existing caller behaves as it always has. Pass a different address to
        read someone else's, which the Application Access Policy still has to
        allow: a 403 here means that address is not in the policy's scope
        group, not that the code is wrong.

        ATTACHMENT_ID may be omitted when the message carries exactly one file
        attachment. With more than one it raises and lists them rather than
        picking, because guessing which document to countersign is not a
        recoverable mistake.

        Inline attachments are skipped. A pasted signature image in a reply is
        not the contract.
        """
        box = mailbox or self.mailbox
        base = f"{GRAPH}/users/{quote(box)}/messages/{message_id}/attachments"

        if attachment_id is None:
            items = self.get_all(base)
            files = [a for a in items
                     if a.get("@odata.type", "").endswith("fileAttachment")
                     and not a.get("isInline")]
            if not files:
                raise RuntimeError(
                    f"No file attachments on message {message_id} in {box}. "
                    "Nothing has been written."
                )
            if len(files) > 1:
                listing = "; ".join(
                    f"{a.get('name')} ({a.get('size')}B) id={a.get('id')}"
                    for a in files
                )
                raise RuntimeError(
                    f"{len(files)} attachments - say which one. {listing}. "
                    "Nothing has been written."
                )
            attachment_id = files[0]["id"]

        att = self.get(f"{base}/{attachment_id}")
        content = att.get("contentBytes")
        if not content:
            raise RuntimeError(
                f"Attachment {att.get('name')!r} returned no contentBytes - an "
                "item attachment (a forwarded email) rather than a file? "
                "Nothing has been written."
            )
        blob = base64.b64decode(content)

        session = self.upload_url(dest_path, root=root, conflict="replace")
        put = self._session.put(
            session["upload_url"], data=blob,
            headers={
                "Content-Length": str(len(blob)),
                "Content-Range": f"bytes 0-{len(blob) - 1}/{len(blob)}",
            },
            timeout=300,
        )
        if put.status_code not in (200, 201):
            raise RuntimeError(
                f"Upload of {att.get('name')!r} failed: {put.status_code} "
                f"{put.text[:300]}. Nothing has been written."
            )
        written = put.json()
        return {
            "saved_to": session["path"],
            "source_name": att.get("name"),
            "source_mailbox": box,
            "bytes": len(blob),
            "web_url": written.get("webUrl"),
        }
