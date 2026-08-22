"""list_files must report paths relative to the folder asked for.

Reporting them relative to the drive root made every file look like it lived in
the fortnight folder itself, so no file mapped to a contractor and nothing was
attached.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.graph_client import GraphClient


class Fake(GraphClient):
    def __init__(self, tree):
        self.tree = tree                       # path under root -> children

    def _drive_id(self):
        return "drive1"

    def get_all(self, url, params=None):
        path = url.split("root:/")[1].split(":/children")[0]
        from urllib.parse import unquote
        return self.tree.get(unquote(path), [])


def test_paths_are_relative_to_the_folder_asked_for():
    g = Fake({
        "Contractors/Timesheets/Fortnight  Ending 16082026": [
            {"name": "Linfox_Bilal Virk", "folder": {}},
            {"name": "Linfox_Dat Le", "folder": {}},
        ],
        "Contractors/Timesheets/Fortnight  Ending 16082026/Linfox_Bilal Virk": [
            {"name": "BV_invoice_2026-08-16.pdf", "file": {}},
        ],
        "Contractors/Timesheets/Fortnight  Ending 16082026/Linfox_Dat Le": [
            {"name": "DL_timesheet_2026-08-16_part1.png", "file": {}},
        ],
    })
    got = sorted(f["path"] for f in g.list_files("Fortnight  Ending 16082026",
                                                 recursive=True))
    assert got == ["Linfox_Bilal Virk/BV_invoice_2026-08-16.pdf",
                   "Linfox_Dat Le/DL_timesheet_2026-08-16_part1.png"]
    assert all(not f["path"].startswith("Fortnight") for f in
               g.list_files("Fortnight  Ending 16082026", recursive=True))
