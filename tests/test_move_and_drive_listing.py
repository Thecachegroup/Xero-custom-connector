"""Moving items, and a drive listing that survives a missing permission.

Two failures on 5 September 2026 prompted both halves of this file.

onedrive_drives() returned nothing at all - not even the SharePoint site it
had already resolved - because enumerating other users is a DIRECTORY read
(User.Read.All) and only Files.ReadWrite.All was consented. One 403 threw
away the whole answer.

And nothing in the cloud path could move a file, so filing a deployed package
out of _PENDING UPLOAD meant a folder grant on the laptop and a second copy of
the truth. Move is a PATCH of parentReference; the item keeps its id.

Offline. Every Graph call is stubbed.
"""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.graph_client import GraphClient


class Fake(GraphClient):
    """A GraphClient with the network removed and every call recorded."""

    def __init__(self, items=None, users=None, users_raise=None,
                 site_raises=False):
        self.files_owner = "andrew.hurnard@thecachegroup.com.au"
        self.sp_host = "thecachegroup.sharepoint.com"
        self._drive_cache = {}
        self.gets = []
        self.requests = []
        self._items = items or {}
        self._users = users or []
        self._users_raise = users_raise
        self._site_raises = site_raises

    def get(self, url, params=None):
        self.gets.append(url)
        if "/sites/" in url and url.endswith("/drive"):
            if self._site_raises:
                raise RuntimeError("403 Authorization_RequestDenied")
            return {"id": "DRIVE-SITE", "name": "Documents",
                    "webUrl": "https://sp/site"}
        if url.endswith("/drive"):
            return {"id": "DRIVE-" + url.split("/")[-2],
                    "webUrl": "https://sp/" + url.split("/")[-2]}
        if url.endswith("/root"):
            return {"id": "ROOT-ID"}
        for path, item in self._items.items():
            if path and url.endswith(path.replace(" ", "%20")):
                return item
        raise RuntimeError(f"unexpected GET {url}")

    def get_all(self, url, params=None):
        self.gets.append(url)
        if self._users_raise:
            raise self._users_raise
        return self._users

    def _request(self, method, url, **kw):
        self.requests.append((method, url, kw.get("json")))
        return None


FILE = {"id": "ITEM-1", "name": "pkg.zip", "size": 54304}
FOLDER = {"id": "DIR-1", "name": "_deployed", "folder": {"childCount": 3}}


def _drive(**kw):
    return Fake(items={"AI%20Working%20Folder/_PENDING%20UPLOAD/pkg.zip": FILE,
                       "AI%20Working%20Folder/_deployed": FOLDER,
                       "AI%20Working%20Folder/notes.md": {"id": "F-2",
                                                          "name": "notes.md"},
                       "AI%20Working%20Folder/_PENDING%20UPLOAD": {
                           "id": "DIR-P", "name": "_PENDING UPLOAD",
                           "folder": {"childCount": 1}}},
                **kw)


SRC = "AI Working Folder/_PENDING UPLOAD/pkg.zip"


# ------------------------------------------------------------------- moving

def test_move_patches_the_parent_and_keeps_the_item_id():
    g = _drive()
    out = g.move_item(SRC, "AI Working Folder/_deployed")
    method, url, body = g.requests[0]
    assert method == "PATCH"
    assert url.endswith("/items/ITEM-1")          # id preserved, not recreated
    assert body == {"parentReference": {"id": "DIR-1"}}
    assert out["moved"] is True
    assert out["dest"] == "AI Working Folder/_deployed/pkg.zip"


def test_move_can_rename_in_the_same_call():
    g = _drive()
    out = g.move_item(SRC, "AI Working Folder/_deployed", new_name="old.zip")
    _, _, body = g.requests[0]
    assert body["name"] == "old.zip"
    assert out["renamed"] is True
    assert out["dest"].endswith("/old.zip")


def test_a_blank_destination_means_the_drive_root():
    g = _drive()
    g.move_item(SRC, "")
    _, _, body = g.requests[0]
    assert body == {"parentReference": {"id": "ROOT-ID"}}


@pytest.mark.parametrize("path", ["", "   ", "/", "  /  "])
def test_move_refuses_the_drive_root_as_the_source(path):
    g = _drive()
    with pytest.raises(RuntimeError, match="Nothing has been moved"):
        g.move_item(path, "AI Working Folder/_deployed")
    assert g.requests == []


def test_move_refuses_a_file_as_the_destination():
    g = _drive()
    with pytest.raises(RuntimeError, match="not a folder"):
        g.move_item(SRC, "AI Working Folder/notes.md")
    assert g.requests == []


def test_a_folder_cannot_be_moved_inside_itself():
    g = _drive()
    with pytest.raises(RuntimeError, match="cannot be moved into itself"):
        g.move_item("AI Working Folder", "AI Working Folder/_deployed")
    assert g.requests == []


def test_a_folder_cannot_be_moved_onto_itself():
    g = _drive()
    with pytest.raises(RuntimeError, match="cannot be moved into itself"):
        g.move_item("AI Working Folder", "AI Working Folder")
    assert g.requests == []


def test_root_prefix_is_applied_to_the_source_path():
    g = _drive()
    g.move_item("_PENDING UPLOAD/pkg.zip", "AI Working Folder/_deployed",
                root="AI Working Folder")
    assert g.requests[0][1].endswith("/items/ITEM-1")


def test_move_uses_the_drive_it_was_given():
    g = _drive()
    g.move_item(SRC, "AI Working Folder/_deployed", drive="site")
    assert "DRIVE-SITE" in g.requests[0][1]


def test_a_folder_move_is_reported_as_a_folder():
    g = Fake(items={"a/thing": FOLDER, "a/dest": {"id": "D-9", "name": "dest",
                                                  "folder": {}}})
    out = g.move_item("a/thing", "a/dest")
    assert out["was_folder"] is True


# ---------------------------------------------------------- drive discovery

DENIED = RuntimeError("403 for GET /users: Authorization_RequestDenied")


def test_a_denied_user_listing_still_returns_the_site_and_the_owner():
    g = Fake(users_raise=DENIED)
    drives = g.list_drives()
    targets = [d["target"] for d in drives]
    assert "site" in targets                  # resolved before the failure
    assert "" in targets                      # the owner's own OneDrive
    assert "-" in targets                     # the part that failed, named


def test_the_denied_row_carries_the_reason_not_a_silent_gap():
    g = Fake(users_raise=DENIED)
    row = [d for d in g.list_drives() if d["target"] == "-"][0]
    assert row["name"].startswith("UNAVAILABLE:")
    assert "Authorization_RequestDenied" in row["name"]


def test_the_owner_drive_is_resolved_without_enumerating_users():
    g = Fake(users_raise=DENIED)
    g.list_drives()
    assert not any(u.endswith("/users") for u in g.gets[:2])


def test_an_unreachable_site_does_not_stop_the_owner_being_listed():
    g = Fake(users_raise=DENIED, site_raises=True)
    drives = g.list_drives()
    site = [d for d in drives if d["target"] == "site"][0]
    owner = [d for d in drives if d["target"] == ""][0]
    assert site["name"].startswith("UNREACHABLE:")
    assert owner["name"] == "andrew.hurnard@thecachegroup.com.au"


def test_the_owner_is_not_listed_twice_when_users_can_be_read():
    g = Fake(users=[{"mail": "andrew.hurnard@thecachegroup.com.au",
                     "displayName": "Andrew"},
                    {"mail": "matt@thecachegroup.com.au",
                     "displayName": "Matt"}])
    drives = g.list_drives()
    targets = [d["target"] for d in drives]
    assert targets.count("andrew.hurnard@thecachegroup.com.au") == 0
    assert "" in targets
    assert "matt@thecachegroup.com.au" in targets


def test_a_user_with_no_onedrive_is_skipped_not_raised():
    class NoDrive(Fake):
        def get(self, url, params=None):
            if "nobody%40x.com" in url:
                raise RuntimeError("404 no drive")
            return super().get(url, params)

    g = NoDrive(users=[{"mail": "nobody@x.com"},
                       {"mail": "matt@thecachegroup.com.au"}])
    targets = [d["target"] for d in g.list_drives()]
    assert "nobody@x.com" not in targets
    assert "matt@thecachegroup.com.au" in targets


def test_a_user_row_with_no_address_at_all_is_skipped():
    g = Fake(users=[{"displayName": "Ghost"}])
    assert [d["target"] for d in g.list_drives()] == ["site", ""]
