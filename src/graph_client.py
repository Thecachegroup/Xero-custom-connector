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
import hashlib
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
        # SharePoint hostname for the team site. Derived from the files owner's
        # domain so a tenant rename needs one env var, not a code change.
        self.sp_host = os.environ.get(
            "TCG_SHAREPOINT_HOST",
            self.files_owner.split("@")[-1].split(".")[0] + ".sharepoint.com")
        # Resolving a drive is a network round trip that never changes inside a
        # request, and _drive_id gets called several times in one tool call.
        self._drive_cache: dict[str, str] = {}
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

    def _drive_id(self, drive: str = "") -> str:
        """Resolve a drive TARGET to a Graph drive id. Cached for the session.

        Until 5 September 2026 this was hardcoded to the files owner's OneDrive,
        so the connector could not see the SharePoint team site or anybody
        else's drive - including Matt's, which holds every interview
        transcript. The app registration already carries Files.ReadWrite.All
        tenant-wide, so the permission was never the limit; this line was.

        TARGET accepts:
          ""  /  "me"        the files owner's OneDrive - the long-standing
                             default, so every existing caller is unchanged
          "someone@x.com"    that person's OneDrive
          "site" / "team"    the root SharePoint site's default library
          "site:Name"        a named SharePoint site's default library
          "b!..."            a literal drive id, used as given
        """
        key = (drive or "").strip()
        if key in self._drive_cache:
            return self._drive_cache[key]

        low = key.lower()
        if not key or low in ("me", "self", "owner", "default"):
            url = f"{GRAPH}/users/{quote(self.files_owner)}/drive"
        elif key.startswith("b!"):
            self._drive_cache[key] = key      # already an id; nothing to look up
            return key
        elif "@" in key:
            url = f"{GRAPH}/users/{quote(key)}/drive"
        elif low in ("site", "team", "shared", "sharepoint"):
            url = f"{GRAPH}/sites/{self.sp_host}/drive"
        elif low.startswith("site:"):
            name = key.split(":", 1)[1].strip()
            hits = self.get(f"{GRAPH}/sites", {"search": name}).get("value") or []
            if not hits:
                raise RuntimeError(
                    f"No SharePoint site matching {name!r}. Nothing has been read."
                )
            url = f"{GRAPH}/sites/{hits[0]['id']}/drive"
        else:
            raise RuntimeError(
                f"Unrecognised drive target {key!r}. Use '' for the default "
                "OneDrive, an email address, 'site', 'site:Name', or a b!... "
                "drive id. Nothing has been read."
            )

        did = self.get(url)["id"]
        self._drive_cache[key] = did
        return did

    def list_drives(self) -> list[dict]:
        """Every drive this connector can actually reach, for discovery.

        Exists because the answer used to be guesswork, and a file that could
        not be found was assumed missing when it was simply on a drive nothing
        was looking at. A user with no provisioned OneDrive is skipped rather
        than raised, so one unlicensed account cannot blind the whole listing.

        THE TWO DRIVES THAT MATTER ARE LISTED WITHOUT ENUMERATING THE TENANT.
        The team site and the files owner's own OneDrive each resolve from a
        single known URL. Only the "everybody else" part needs GET /users,
        which is a directory read and a separate consent - so on 5 September
        2026, with Files.ReadWrite.All granted but User.Read.All not, the whole
        listing died on a 403 and reported nothing, including the two drives it
        had already found. Each section now fails on its own.
        """
        out: list[dict] = []

        try:
            d = self.get(f"{GRAPH}/sites/{self.sp_host}/drive")
            out.append({"target": "site", "kind": "SharePoint site",
                        "name": d.get("name"), "id": d.get("id"),
                        "web_url": d.get("webUrl")})
        except Exception as e:  # noqa: BLE001
            out.append({"target": "site", "kind": "SharePoint site",
                        "name": f"UNREACHABLE: {e}", "id": "", "web_url": ""})

        owner = (self.files_owner or "").strip()
        if owner:
            try:
                d = self.get(f"{GRAPH}/users/{quote(owner)}/drive")
                out.append({"target": "", "kind": "OneDrive (default)",
                            "name": owner, "id": d.get("id"),
                            "web_url": d.get("webUrl")})
            except Exception as e:  # noqa: BLE001
                out.append({"target": "", "kind": "OneDrive (default)",
                            "name": f"UNREACHABLE: {e}", "id": "",
                            "web_url": ""})

        try:
            users = self.get_all(f"{GRAPH}/users",
                                 {"$select": "displayName,mail,userPrincipalName",
                                  "$top": "100"})
        except Exception as e:  # noqa: BLE001
            # A directory read, not a files read. Say which permission it wants
            # rather than leaving a bare 403 to be diagnosed twice.
            out.append({"target": "-", "kind": "other users",
                        "name": f"UNAVAILABLE: {e}", "id": "", "web_url": ""})
            return out

        for u in users:
            who = u.get("mail") or u.get("userPrincipalName")
            if not who or who.lower() == owner.lower():
                continue          # the owner is already listed, as the default
            try:
                d = self.get(f"{GRAPH}/users/{quote(who)}/drive")
            except Exception:  # noqa: BLE001
                continue          # no OneDrive provisioned; not an error
            out.append({"target": who, "kind": "OneDrive",
                        "name": u.get("displayName") or who,
                        "id": d.get("id"), "web_url": d.get("webUrl")})
        return out

    def move_item(self, relative_path: str, dest_folder: str, root: str = "",
                  drive: str = "", new_name: str = "") -> dict:
        """Move one file or folder to another folder on the SAME drive.

        Added 6 September 2026. Until then the cloud path could write and it
        could delete, but it could not move, so filing a finished package away
        from a working folder meant going through the laptop - a folder grant,
        a sync wait, and a second copy of the truth. Graph moves by PATCHing
        parentReference, which keeps the item id, so existing share links and
        resource URIs survive.

        DEST_FOLDER is a folder path relative to the drive root; blank means
        the drive root itself. NEW_NAME optionally renames in the same call.

        The drive root cannot be moved, the destination must be a folder, and a
        folder cannot be moved inside itself. Graph refuses a name that already
        exists at the destination rather than overwriting, and that refusal is
        passed straight through - nothing here silently replaces a file.

        Cross-drive is not supported by this operation and is not faked with a
        copy-then-delete: a half-finished copy that has already deleted the
        original is the one failure mode worth designing out.
        """
        raw = f"{root}/{relative_path}" if root else relative_path
        path = raw.strip().strip("/").strip()
        if not path:
            raise RuntimeError(
                "Refusing to move the drive root. Nothing has been moved."
            )
        dest = (dest_folder or "").strip().strip("/").strip()

        # A folder cannot become its own descendant. Graph reports this as a
        # generic 400, which reads like a bad path rather than a bad idea.
        if dest == path or dest.startswith(path + "/"):
            raise RuntimeError(
                f"{dest!r} is inside {path!r}. A folder cannot be moved into "
                "itself. Nothing has been moved."
            )

        did = self._drive_id(drive)
        item = self.get(f"{GRAPH}/drives/{did}/root:/{quote(path)}",
                        {"$select": "id,name,size,folder"})

        if dest:
            parent = self.get(f"{GRAPH}/drives/{did}/root:/{quote(dest)}",
                              {"$select": "id,name,folder"})
            # Presence, not truthiness - Graph sends {"childCount": 0} for an
            # empty folder, which is falsy.
            if "folder" not in parent:
                raise RuntimeError(
                    f"{dest!r} is a file, not a folder. Nothing has been moved."
                )
        else:
            parent = self.get(f"{GRAPH}/drives/{did}/root", {"$select": "id"})

        body: dict[str, Any] = {"parentReference": {"id": parent["id"]}}
        if new_name:
            body["name"] = new_name
        self._request("PATCH", f"{GRAPH}/drives/{did}/items/{item['id']}",
                      json=body)

        name = new_name or item.get("name")
        return {"path": path, "name": name, "size": item.get("size"),
                "was_folder": "folder" in item,
                "dest": f"{dest}/{name}" if dest else name,
                "renamed": bool(new_name), "moved": True}

    def delete_item(self, relative_path: str, root: str = "",
                    drive: str = "", allow_folder: bool = False) -> dict:
        """Delete one file. It goes to the recycle bin, recoverable ~93 days.

        Added 5 September 2026. Nothing in the cloud path could delete anything
        at all, so every working file a run wrote stayed for ever and had to be
        cleared by hand.

        A FOLDER IS REFUSED unless ALLOW_FOLDER. Deleting a folder takes its
        whole contents with it, and the distance between a stale working file
        and a contractor's signed agreements is one wrong path.
        """
        # Whitespace is stripped BEFORE the root check, not after. "   " and
        # "/" survive a bare strip("/") as truthy strings, and a path that is
        # really the drive root must never reach a DELETE.
        raw = f"{root}/{relative_path}" if root else relative_path
        path = raw.strip().strip("/").strip()
        if not path:
            raise RuntimeError(
                "Refusing to delete the drive root. Nothing has been deleted."
            )
        did = self._drive_id(drive)
        item = self.get(f"{GRAPH}/drives/{did}/root:/{quote(path)}",
                        {"$select": "id,name,size,folder"})
        # Presence, not truthiness - Graph sends {"childCount": 0} for an empty
        # folder, which is falsy.
        if "folder" in item and not allow_folder:
            raise RuntimeError(
                f"{path!r} is a folder. Deleting it would take everything "
                "inside it with it. Pass allow_folder=True if that is genuinely "
                "intended. Nothing has been deleted."
            )
        self._request("DELETE", f"{GRAPH}/drives/{did}/items/{item['id']}")
        return {"path": path, "name": item.get("name"),
                "size": item.get("size"), "was_folder": "folder" in item,
                "deleted": True}

    def delete_items(self, paths, root: str = "", drive: str = "",
                     allow_folder: bool = False) -> list[dict]:
        """Delete several files in ONE call. Never stops at the first problem.

        Added 5 September 2026. Deleting page by page meant one approval prompt
        per file, and a run clearing seven timesheet fragments sat waiting on
        seven separate clicks - one of which was for a file an earlier click had
        already removed. That stall is the reason this exists.

        A MISSING FILE IS NOT A FAILURE. Graph 404s on a path that is already
        gone, and "already gone" is the outcome the caller wanted. It is
        reported as `absent` and the batch carries on.

        Every path is judged on its own. A folder refusal or a bad path stops
        that one path and nothing else, so a typo in the fourth entry cannot
        cost the other six.
        """
        out: list[dict] = []
        for raw in paths:
            p = str(raw or "").strip()
            if not p:
                continue
            try:
                d = self.delete_item(p, root=root, drive=drive,
                                     allow_folder=allow_folder)
                out.append({"path": d["path"], "status": "deleted",
                            "size": d.get("size"), "detail": ""})
            except requests.HTTPError as e:
                code = getattr(getattr(e, "response", None), "status_code", 0)
                if code == 404:
                    out.append({"path": p, "status": "absent", "size": None,
                                "detail": "not there - nothing to do"})
                else:
                    out.append({"path": p, "status": "failed", "size": None,
                                "detail": str(e)[:200]})
            except Exception as e:  # noqa: BLE001
                out.append({"path": p, "status": "refused", "size": None,
                            "detail": str(e)[:200]})
        return out

    def folder_digests(self, folder: str, root: str = "",
                       drive: str = "") -> list[dict]:
        """Every file in one folder with a content digest, for finding repeats.

        Same size is not the same file, so size alone can never be the test -
        it is only the cheap filter that says which files are worth reading.
        Files whose size is unique in the folder cannot have a twin and are
        never downloaded.

        Graph carries `file.hashes.quickXorHash` on OneDrive for Business
        items, so most of the time nothing is downloaded at all. Where the
        annotation is absent the bytes are read and hashed here, and the two
        never get compared against each other - a digest is only ever matched
        against another digest of the same kind.
        """
        items = [it for it in self.list_children(folder, root=root, drive=drive)
                 if "folder" not in it]
        by_size: dict[int, int] = {}
        for it in items:
            by_size[int(it.get("size") or 0)] = by_size.get(int(it.get("size") or 0), 0) + 1

        base = f"{root}/{folder}".strip("/") if root else folder.strip("/")
        out: list[dict] = []
        for it in items:
            size = int(it.get("size") or 0)
            rel = it.get("path") or it.get("name")
            row = {"name": it.get("name"), "path": f"{base}/{rel}".strip("/"),
                   "size": size, "digest": "", "kind": ""}
            if by_size.get(size, 0) > 1:
                qx = (((it.get("file") or {}).get("hashes") or {})
                      .get("quickXorHash"))
                if qx:
                    row["digest"], row["kind"] = str(qx), "quickXor"
                else:
                    blob = self.download(rel, root=base, drive=drive)
                    row["digest"] = hashlib.sha256(blob).hexdigest()
                    row["kind"] = "sha256"
            out.append(row)
        return out

    def find_identical(self, folder: str, blob: bytes, root: str = "",
                       drive: str = "", listing: list[dict] | None = None) -> str:
        """The path of an existing file in FOLDER with exactly these bytes.

        Used before writing, so a repeat is never created in the first place.
        Contractors' inline timesheet images come back a second time inside a
        reply that quotes the original mail; both copies are real attachments on
        real messages, they get different part numbers, and nothing downstream
        can tell that two of the seven pages are the same page twice.

        Only files of exactly the same length are read, and the comparison is
        the bytes themselves rather than a digest - at one candidate per call
        there is nothing to be gained by hashing, and a byte comparison cannot
        be wrong.
        """
        try:
            items = listing if listing is not None else self.list_children(
                folder, root=root, drive=drive)
        except Exception:  # noqa: BLE001
            # A folder that does not exist yet holds nothing to collide with.
            return ""
        base = f"{root}/{folder}".strip("/") if root else folder.strip("/")
        n = len(blob)
        for it in items:
            if "folder" in it or int(it.get("size") or 0) != n:
                continue
            rel = it.get("path") or it.get("name")
            try:
                if self.download(rel, root=base, drive=drive) == blob:
                    return f"{base}/{rel}".strip("/")
            except Exception:  # noqa: BLE001
                continue
        return ""

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

    def list_children(self, path: str = "", root: str = "",
                      recursive: bool = False, drive: str = "") -> list[dict]:
        """One folder's immediate children - folders AND files.

        list_files() is timesheet-shaped: it drops folders entirely and defaults
        to the Contractors/Timesheets tree, so CONTRACTOR AGREEMENTS listed as
        empty when every contractor in it is a folder. This is the plain
        listing, rooted at the drive.

        "" is the drive root, which needs the /root/children form. Building it
        the other way gives "root:/:/children", which Graph answers with a 400.
        """
        full = f"{root}/{path}".strip("/") if root else path.strip("/")
        did = self._drive_id(drive)

        def children_url(rel: str) -> str:
            if not rel:
                return f"{GRAPH}/drives/{did}/root/children"
            return f"{GRAPH}/drives/{did}/root:/{quote(rel)}:/children"

        def walk(rel: str) -> list[dict]:
            out: list[dict] = []
            for it in self.get_all(children_url(rel), {"$top": "200"}):
                child = f"{rel}/{it['name']}".strip("/")
                # Paths relative to the folder that was asked for, matching
                # list_files - the caller asked about a folder, not the drive.
                it["path"] = child[len(full):].strip("/") if full else child
                out.append(it)
                # Presence, not truthiness. {"childCount": 0} is falsy and an
                # empty contractor folder is normal.
                if recursive and "folder" in it:
                    out += walk(child)
            return out

        return walk(full)

    def download(self, relative_path: str, root: str = "Contractors/Timesheets",
                 drive: str = "") -> bytes:
        """Read a filed document back out of OneDrive.

        DRIVE added 5 Sep 2026 alongside the dedupe tools - without it every
        read was pinned to Andrew's own OneDrive, so a repeat sitting in the
        team library could be listed but never opened.
        """
        path = f"{root}/{relative_path}".strip("/")
        url = f"{GRAPH}/drives/{self._drive_id(drive)}/root:/{quote(path)}:/content"
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

    def download_url(self, relative_path: str, root: str = "",
                     drive: str = "") -> dict:
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
        base = f"{GRAPH}/drives/{self._drive_id(drive)}/root:/{quote(path)}"
        item = self.get(base, {"$select": "id,name,size,lastModifiedDateTime,folder"})

        # Presence, not truthiness - Graph sends {"childCount": 0} for an empty
        # folder, which is falsy.
        if "folder" in item:
            raise RuntimeError(
                f"No download URL for {path!r} - that is a folder, not a file. "
                "Nothing has been read."
            )

        # Graph DROPS @microsoft.graph.downloadUrl from any response that carries
        # a $select - including a $select that names it. Asking for the annotation
        # is what removes it, so every file came back 200 with no link. Take the
        # URL off the /content redirect instead: it arrives as the Location header
        # on a 302, and no $select can strip a header.
        resp = self._request("GET", f"{base}:/content", allow_redirects=False)
        link = resp.headers.get("Location")
        if not link:
            raise RuntimeError(
                f"No download URL for {path!r} - Graph answered {resp.status_code} "
                "with no Location header. Nothing has been read."
            )
        return {
            "path": path,
            "name": item.get("name"),
            "size": item.get("size"),
            "last_modified": item.get("lastModifiedDateTime"),
            "download_url": link,
        }

    def upload_url(self, relative_path: str, root: str = "",
                   conflict: str = "replace", drive: str = "") -> dict:
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
        url = f"{GRAPH}/drives/{self._drive_id(drive)}/root:/{quote(path)}:/createUploadSession"
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
