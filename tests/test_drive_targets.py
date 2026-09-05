"""Drive targeting and delete. Offline; every Graph call is stubbed.

Until 5 September 2026 GraphClient._drive_id was hardcoded to the files
owner's OneDrive. The SharePoint team site and everybody else's drive were
unreachable - including Matt's, which holds every interview transcript - and a
file sitting on one of them read as missing. The permission was never the
limit: the app registration already carries Files.ReadWrite.All tenant-wide.

The single most important test in this file is the last one. Every existing
caller passes no drive at all, and if the default ever stops meaning "the
files owner's OneDrive" the payroll sweep silently starts filing timesheets
somewhere else.
"""
import sys, pathlib
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.graph_client import GraphClient


class Fake(GraphClient):
    """A GraphClient with the network removed and every call recorded."""

    def __init__(self, items=None, sites=None):
        self.files_owner = "andrew.hurnard@thecachegroup.com.au"
        self.sp_host = "thecachegroup.sharepoint.com"
        self._drive_cache = {}
        self.gets = []
        self.requests = []
        self._items = items or {}
        self._sites = sites if sites is not None else {"value": [{"id": "SITE-9"}]}

    def get(self, url, params=None):
        self.gets.append(url)
        if url.endswith("/drive"):
            return {"id": "DRIVE-" + url.split("/")[-2]}
        if "/sites" in url and not url.endswith("/drive"):
            return self._sites
        for path, item in self._items.items():
            if path and url.endswith(path.replace(" ", "%20")):
                return item
        raise RuntimeError(f"unexpected GET {url}")

    def get_all(self, url, params=None):
        self.gets.append(url)
        return []

    def _request(self, method, url, **kw):
        self.requests.append((method, url))
        return None


# --------------------------------------------------------------- resolution

def test_blank_target_is_the_files_owner():
    g = Fake()
    assert g._drive_id("") == "DRIVE-andrew.hurnard%40thecachegroup.com.au"
    assert "users/andrew.hurnard%40thecachegroup.com.au/drive" in g.gets[0]


@pytest.mark.parametrize("alias", ["me", "self", "owner", "default", "  ME  "])
def test_owner_aliases_all_resolve_to_the_owner(alias):
    g = Fake()
    assert g._drive_id(alias) == "DRIVE-andrew.hurnard%40thecachegroup.com.au"


def test_an_email_resolves_to_that_persons_onedrive():
    g = Fake()
    g._drive_id("matt@thecachegroup.com.au")
    assert "users/matt%40thecachegroup.com.au/drive" in g.gets[0]


def test_site_resolves_to_the_team_library():
    g = Fake()
    g._drive_id("site")
    assert g.gets[0].endswith("/sites/thecachegroup.sharepoint.com/drive")


@pytest.mark.parametrize("alias", ["site", "team", "shared", "SharePoint"])
def test_site_aliases(alias):
    g = Fake()
    g._drive_id(alias)
    assert "/sites/thecachegroup.sharepoint.com/drive" in g.gets[0]


def test_named_site_searches_then_takes_its_default_library():
    g = Fake()
    g._drive_id("site:Payroll")
    assert any(u.endswith("/sites") for u in g.gets)
    assert any(u.endswith("/sites/SITE-9/drive") for u in g.gets)


def test_named_site_with_no_match_raises_and_says_nothing_was_read():
    g = Fake(sites={"value": []})
    with pytest.raises(RuntimeError) as e:
        g._drive_id("site:Nope")
    assert "Nothing has been read" in str(e.value)


def test_a_literal_drive_id_is_used_as_given_with_no_lookup():
    g = Fake()
    assert g._drive_id("b!abc123") == "b!abc123"
    assert g.gets == []          # a drive id needs no round trip


def test_an_unrecognised_target_raises_rather_than_guessing():
    g = Fake()
    with pytest.raises(RuntimeError) as e:
        g._drive_id("Andrews Laptop")
    assert "Unrecognised drive target" in str(e.value)
    assert "Nothing has been read" in str(e.value)


def test_resolution_is_cached():
    g = Fake()
    g._drive_id("site")
    g._drive_id("site")
    g._drive_id("site")
    assert len(g.gets) == 1


# ------------------------------------------------------------------ threading

def test_list_children_uses_the_drive_it_was_given():
    g = Fake()
    g.list_children("", drive="matt@thecachegroup.com.au")
    hit = [u for u in g.gets if "root/children" in u]
    assert hit and "DRIVE-matt%40thecachegroup.com.au" in hit[0]


def test_empty_path_uses_root_children_not_the_400_form():
    g = Fake()
    g.list_children("", drive="site")
    hit = [u for u in g.gets if "children" in u]
    assert hit and hit[0].endswith("/root/children")
    assert "root:/:/children" not in hit[0]


# --------------------------------------------------------------------- delete

FILE = {"id": "ITEM-1", "name": "note.txt", "size": 12}
FOLDER = {"id": "ITEM-2", "name": "Devinia Liddelow", "size": 999,
          "folder": {"childCount": 0}}


def test_delete_removes_a_file_and_reports_it():
    g = Fake(items={"note.txt": FILE})
    out = g.delete_item("note.txt")
    assert out["deleted"] is True
    assert out["was_folder"] is False
    assert g.requests == [("DELETE",
                           "https://graph.microsoft.com/v1.0/drives/"
                           "DRIVE-andrew.hurnard%40thecachegroup.com.au/items/ITEM-1")]


def test_delete_refuses_a_folder_by_default():
    g = Fake(items={"Devinia%20Liddelow": FOLDER})
    with pytest.raises(RuntimeError) as e:
        g.delete_item("Devinia Liddelow")
    assert "is a folder" in str(e.value)
    assert "Nothing has been deleted" in str(e.value)
    assert g.requests == []          # and nothing was sent


def test_a_folder_with_zero_children_is_still_a_folder():
    """{"childCount": 0} is falsy. Testing the value instead of its presence
    would let an empty contractor folder be deleted as though it were a file."""
    g = Fake(items={"Devinia%20Liddelow": FOLDER})
    with pytest.raises(RuntimeError):
        g.delete_item("Devinia Liddelow")


def test_delete_allows_a_folder_when_explicitly_asked():
    g = Fake(items={"Devinia%20Liddelow": FOLDER})
    out = g.delete_item("Devinia Liddelow", allow_folder=True)
    assert out["was_folder"] is True
    assert len(g.requests) == 1


@pytest.mark.parametrize("path", ["", "   ", "/"])
def test_delete_refuses_the_drive_root(path):
    g = Fake()
    with pytest.raises(RuntimeError) as e:
        g.delete_item(path)
    assert "drive root" in str(e.value)
    assert g.requests == []


def test_delete_uses_the_drive_it_was_given():
    g = Fake(items={"note.txt": FILE})
    g.delete_item("note.txt", drive="site")
    assert "DRIVE-thecachegroup.sharepoint.com" in g.requests[0][1]


# ------------------------------------------------------------ no regression

def test_the_default_is_still_the_owners_onedrive():
    """The one that matters. Every existing caller passes no drive at all."""
    g = Fake(items={"note.txt": FILE})
    owner = "DRIVE-andrew.hurnard%40thecachegroup.com.au"
    assert g._drive_id() == owner
    assert g._drive_id("") == owner
    g.list_children("")
    assert all(owner in u for u in g.gets if "children" in u)


def test_sharepoint_host_is_derived_from_the_owners_domain():
    """A tenant rename must not need a code change."""
    import os
    from unittest import mock
    env = {"GRAPH_TENANT_ID": "t", "GRAPH_CLIENT_ID": "c",
           "GRAPH_CLIENT_SECRET": "s",
           "TCG_FILES_OWNER": "someone@othercorp.com.au"}
    with mock.patch.dict(os.environ, env, clear=False):
        os.environ.pop("TCG_SHAREPOINT_HOST", None)
        assert GraphClient().sp_host == "othercorp.sharepoint.com"
