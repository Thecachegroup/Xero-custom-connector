"""Deleting several files in one call, and never filing the same page twice.

Written 5 September 2026, from a run that sat for 46 minutes.

Prasanthi Dharanikota's timesheet folder for the fortnight ending 30 August
2026 held seven page images, two of which were another page over again. The
clean-up was going through the approval prompt one file at a time, and the
prompt that stalled was asking to delete a file an earlier prompt had already
removed - so the answer to it was neither yes nor no.

Three things come out of that, and this file covers all three:

  * a delete that takes a LIST, so one clean-up is one approval;
  * a path that is already gone reported as `absent`, not as a failure - it is
    the outcome the caller asked for;
  * the repeat never created in the first place. A quoted reply carries the
    original inline images back as fresh attachments; they are filed under new
    part numbers and nothing downstream can tell.

Offline. Every Graph call is stubbed.
"""
import hashlib
import sys
import pathlib

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.graph_client import GraphClient
from src.server import _split_paths


def _http_error(code: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = code
    return requests.HTTPError(f"{code} for GET ...", response=resp)


class Fake(GraphClient):
    """A GraphClient with the network removed and every call recorded."""

    def __init__(self, items=None, children=None, blobs=None):
        self.files_owner = "andrew.hurnard@thecachegroup.com.au"
        self.sp_host = "thecachegroup.sharepoint.com"
        self._drive_cache = {}
        self._items = items or {}
        self._children = children or []
        self._blobs = blobs or {}
        self.deleted = []
        self.downloaded = []

    def _drive_id(self, drive=""):
        return "DRIVE-1"

    def get(self, url, params=None):
        for path, item in self._items.items():
            if url.endswith(path.replace(" ", "%20")):
                return item
        raise _http_error(404)

    def _request(self, method, url, **kw):
        if method == "DELETE":
            self.deleted.append(url.rsplit("/", 1)[-1])
        return None

    def list_children(self, path="", root="", recursive=False, drive=""):
        return list(self._children)

    def download(self, relative_path, root="Contractors/Timesheets", drive=""):
        self.downloaded.append(relative_path)
        return self._blobs[relative_path]


# ---------------------------------------------------------------- _split_paths

def test_one_path_is_still_one_path():
    assert _split_paths("A/b.png") == ["A/b.png"]


def test_several_paths_one_per_line():
    assert _split_paths("A/b.png\nA/c.png\n\n A/d.png ") == [
        "A/b.png", "A/c.png", "A/d.png"]


def test_a_json_array_is_accepted():
    assert _split_paths('["A/b.png", "A/c.png"]') == ["A/b.png", "A/c.png"]


def test_a_real_list_is_accepted():
    assert _split_paths(["A/b.png", "A/c.png"]) == ["A/b.png", "A/c.png"]


def test_a_comma_in_a_filename_survives():
    """Splitting on commas would cut this in half. Newlines only."""
    assert _split_paths("Agreements/Smith, John - signed.pdf") == [
        "Agreements/Smith, John - signed.pdf"]


def test_nothing_at_all_is_empty_not_a_blank_path():
    assert _split_paths("") == []
    assert _split_paths("   \n  \n") == []


# ---------------------------------------------------------------- delete_items

def test_a_batch_deletes_every_file_in_one_call():
    g = Fake(items={"F/a.png": {"id": "A", "name": "a.png", "size": 10},
                    "F/b.png": {"id": "B", "name": "b.png", "size": 20}})
    rows = g.delete_items(["F/a.png", "F/b.png"])
    assert [r["status"] for r in rows] == ["deleted", "deleted"]
    assert g.deleted == ["A", "B"]


def test_a_file_that_is_already_gone_is_absent_not_failed():
    """The 46-minute prompt. Deleting a file that is not there is a no-op,
    and a run must not stop to ask about it."""
    g = Fake(items={"F/a.png": {"id": "A", "name": "a.png", "size": 10}})
    rows = g.delete_items(["F/gone.png", "F/a.png"])
    assert rows[0]["status"] == "absent"
    assert rows[1]["status"] == "deleted"
    assert g.deleted == ["A"]


def test_one_bad_path_does_not_cost_the_others():
    g = Fake(items={"F/a.png": {"id": "A", "name": "a.png", "size": 10},
                    "F/dir": {"id": "D", "name": "dir", "folder": {"childCount": 0}},
                    "F/c.png": {"id": "C", "name": "c.png", "size": 30}})
    rows = g.delete_items(["F/a.png", "F/dir", "F/c.png"])
    assert [r["status"] for r in rows] == ["deleted", "refused", "deleted"]
    assert g.deleted == ["A", "C"]


def test_a_folder_in_a_batch_is_still_refused():
    g = Fake(items={"F/dir": {"id": "D", "name": "dir", "folder": {"childCount": 9}}})
    rows = g.delete_items(["F/dir"])
    assert rows[0]["status"] == "refused"
    assert "folder" in rows[0]["detail"]
    assert g.deleted == []


def test_the_drive_root_is_refused_inside_a_batch_too():
    g = Fake()
    rows = g.delete_items(["  /  ", "   "])
    assert [r["status"] for r in rows] == ["refused"]
    assert g.deleted == []


# --------------------------------------------------------------- folder_digests

def _child(name, size, qx=None):
    it = {"name": name, "path": name, "size": size, "file": {}}
    if qx:
        it["file"] = {"hashes": {"quickXorHash": qx}}
    return it


def test_a_file_of_unique_length_is_never_downloaded():
    """Size is the cheap filter, not the test. A file no other file matches
    in length cannot have a twin, so reading it would be wasted."""
    g = Fake(children=[_child("part1.png", 107472), _child("part3.png", 103232)])
    rows = g.folder_digests("F")
    assert g.downloaded == []
    assert all(r["digest"] == "" for r in rows)


def test_same_length_files_are_hashed_from_graph_when_it_offers_a_hash():
    g = Fake(children=[_child("part4.png", 90139, qx="XOR-1"),
                       _child("part6.png", 90139, qx="XOR-1")])
    rows = g.folder_digests("F")
    assert g.downloaded == []
    assert [r["kind"] for r in rows] == ["quickXor", "quickXor"]
    assert rows[0]["digest"] == rows[1]["digest"]


def test_without_a_graph_hash_the_bytes_are_read_and_hashed_here():
    g = Fake(children=[_child("part4.png", 5), _child("part6.png", 5)],
             blobs={"part4.png": b"AAAAA", "part6.png": b"AAAAA"})
    rows = g.folder_digests("F")
    assert sorted(g.downloaded) == ["part4.png", "part6.png"]
    assert [r["kind"] for r in rows] == ["sha256", "sha256"]
    assert rows[0]["digest"] == hashlib.sha256(b"AAAAA").hexdigest()


def test_same_length_different_bytes_are_not_the_same_file():
    g = Fake(children=[_child("a.png", 5), _child("b.png", 5)],
             blobs={"a.png": b"AAAAA", "b.png": b"BBBBB"})
    rows = g.folder_digests("F")
    assert rows[0]["digest"] != rows[1]["digest"]


def test_the_path_reported_is_the_full_path_delete_will_take():
    g = Fake(children=[_child("part4.png", 5), _child("part6.png", 5)],
             blobs={"part4.png": b"AAAAA", "part6.png": b"AAAAA"})
    rows = g.folder_digests("Contractors/Timesheets/Fx/PD")
    assert rows[0]["path"] == "Contractors/Timesheets/Fx/PD/part4.png"


# --------------------------------------------------------------- find_identical

def test_a_page_already_in_the_folder_is_found_by_its_bytes():
    g = Fake(children=[_child("part1.png", 5)], blobs={"part1.png": b"AAAAA"})
    assert g.find_identical("F", b"AAAAA") == "F/part1.png"


def test_a_page_of_the_same_length_but_different_content_is_not_a_match():
    g = Fake(children=[_child("part1.png", 5)], blobs={"part1.png": b"AAAAA"})
    assert g.find_identical("F", b"BBBBB") == ""


def test_files_of_another_length_are_never_opened():
    g = Fake(children=[_child("part1.png", 9999)], blobs={})
    assert g.find_identical("F", b"AAAAA") == ""
    assert g.downloaded == []


def test_a_folder_that_does_not_exist_yet_holds_nothing_to_collide_with():
    class Missing(Fake):
        def list_children(self, *a, **kw):
            raise _http_error(404)

    assert Missing().find_identical("F", b"AAAAA") == ""
