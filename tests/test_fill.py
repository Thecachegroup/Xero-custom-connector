"""The fill puts numbers on invoices that go to clients. It gets its own tests."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src import writes as w

STAMP = "Fortnight ending 16/08/2026"


def _doc(num, contact, code, unit, qty=0, desc="Devinia Liddelow - Change Manager"):
    return {"InvoiceID": f"id-{num}", "InvoiceNumber": num,
            "Contact": {"Name": contact},
            "LineItems": [{"LineItemID": f"li-{num}", "ItemCode": code,
                           "Description": desc, "Quantity": qty,
                           "UnitAmount": unit, "LineAmount": qty * unit,
                           "AccountCode": "200", "TaxType": "OUTPUT"}]}


def test_parse_quantities_handles_codes_containing_colons_and_spaces():
    got, bad = w.parse_quantities("Linfox - DL: 10\nLinfox - EK: 2.5\nTec - PS: 42.23")
    assert got == {"Linfox - DL": 10.0, "Linfox - EK": 2.5, "Tec - PS": 42.23}
    assert bad == []


def test_parse_quantities_reports_rubbish_rather_than_guessing():
    got, bad = w.parse_quantities("Linfox - DL 10\nLinfox - EK: ten\n# a comment\n")
    assert got == {}
    assert bad == ["Linfox - DL 10", "Linfox - EK: ten"]


def test_a_zero_line_is_filled_and_stamped_once():
    docs = [_doc("TCG-1", "Linfox", "Linfox - DL", 1406)]
    planned, skipped, to_write = w.plan_line_fill(docs, {"Linfox - DL": 10}, STAMP)
    assert planned[0]["Days"] == 10 and planned[0]["Amount"] == 14060.0
    assert skipped == []
    line = to_write[0]["LineItems"][0]
    assert line["Quantity"] == 10
    assert line["Description"].endswith(STAMP)
    # run the planner again over the now-filled line: nothing more happens
    planned2, skipped2, to_write2 = w.plan_line_fill(docs, {"Linfox - DL": 10}, STAMP)
    assert planned2 == [] and to_write2 == []
    assert skipped2[0]["Why"].startswith("already billed")
    assert line["Description"].count("Fortnight ending") == 1


def test_a_line_someone_already_billed_is_never_touched():
    """The single rule that makes four sweeps in one billing week safe."""
    docs = [_doc("TCG-2", "Linfox", "Linfox - JJ", 1270, qty=12)]
    planned, skipped, to_write = w.plan_line_fill(docs, {"Linfox - JJ": 99}, STAMP)
    assert planned == [] and to_write == []
    assert docs[0]["LineItems"][0]["Quantity"] == 12


def test_an_item_code_not_asked_for_is_left_alone():
    """Saeid Almaher's draft is still live. Not in the list means not touched."""
    docs = [_doc("TCG-3", "Linfox", "Linfox - SA", 1375)]
    planned, skipped, to_write = w.plan_line_fill(docs, {"Linfox - DL": 10}, STAMP)
    assert planned == [] and skipped == [] and to_write == []
    assert docs[0]["LineItems"][0]["Quantity"] == 0


def test_part_days_survive_as_fractions():
    docs = [_doc("TCG-4", "Linfox", "Linfox - EK", 1385)]
    planned, _, to_write = w.plan_line_fill(docs, {"Linfox - EK": 2.5}, STAMP)
    assert to_write[0]["LineItems"][0]["Quantity"] == 2.5
    assert planned[0]["Amount"] == 3462.5


def test_line_amount_is_not_echoed_back():
    """Sending a stale LineAmount with a new Quantity gives a total that does not
    match its own lines. Xero must recompute it."""
    assert "LineAmount" not in w._LINE_KEEP
    assert "LineItemID" in w._LINE_KEEP
