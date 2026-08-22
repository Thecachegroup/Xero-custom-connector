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


# ------------------------------------------------- the contractor's own number

def _bill(num, contact, code):
    return {"InvoiceID": f"bid-{code}", "InvoiceNumber": num,
            "Contact": {"Name": contact},
            "LineItems": [{"LineItemID": "l1", "ItemCode": code,
                           "Description": contact, "Quantity": 0,
                           "UnitAmount": 950}]}


def test_placeholder_bill_numbers_are_replaced_with_the_real_one():
    docs = [_bill("Inv", "Techne IT Consulting Pty Ltd", "Linfox - BVIRK"),
            _bill("JJ", "Dev InfoTech Pty Ltd", "Linfox - JJ"),
            _bill(None, "Peter Small", "Tec - PS")]
    got = w.plan_number_change(docs, {"Linfox - BVIRK": "INV-0016",
                                      "Linfox - JJ": "20260802",
                                      "Tec - PS": "0024"})
    assert [(g["Was"], g["Now"]) for g in got] == [
        ("Inv", "INV-0016"), ("JJ", "20260802"), ("(none)", "0024")]


def test_a_bill_already_carrying_the_right_number_is_left_alone():
    docs = [_bill("INV-0016", "Techne IT Consulting Pty Ltd", "Linfox - BVIRK")]
    assert w.plan_number_change(docs, {"Linfox - BVIRK": "INV-0016"}) == []


def test_a_bill_with_two_known_item_codes_is_not_guessed_at():
    """Two contractors on one bill - there is no single right number."""
    d = _bill("Inv", "Someone", "Linfox - JJ")
    d["LineItems"].append({"LineItemID": "l2", "ItemCode": "Linfox - DV",
                           "Quantity": 0, "UnitAmount": 900})
    assert w.plan_number_change([d], {"Linfox - JJ": "1", "Linfox - DV": "2"}) == []


def test_update_invoice_refuses_to_send_an_empty_change():
    import pytest
    with pytest.raises(ValueError):
        w.update_invoice(None, "some-id")


# ----------------------------------------------------------- the house reference

def test_the_reference_matches_the_invoices_actually_sent():
    from datetime import date as D
    assert w.period_reference(D(2026, 7, 6), D(2026, 7, 19)) == "6 July to 19 July 2026"
    assert w.period_reference(D(2026, 7, 20), D(2026, 8, 2)) == "20 July to 2 August 2026"
    assert w.period_reference(D(2026, 8, 3), D(2026, 8, 16)) == "3 August to 16 August 2026"


def test_a_period_across_a_year_end_names_both_years():
    from datetime import date as D
    assert w.period_reference(D(2026, 12, 21), D(2027, 1, 3)) == \
        "21 December 2026 to 3 January 2027"


def test_an_invoice_already_carrying_the_reference_is_left_alone():
    ref = "3 August to 16 August 2026"
    docs = [{"InvoiceID": "a", "InvoiceNumber": "TCG-1", "Reference": ref,
             "Contact": {"Name": "Linfox"}},
            {"InvoiceID": "b", "InvoiceNumber": "TCG-2", "Reference": "DL",
             "Contact": {"Name": "Linfox"}}]
    got = w.plan_reference_change(docs, ref)
    assert [(g["Doc"], g["Was"]) for g in got] == [("TCG-2", "DL")]


# ------------------------------------------------------------------ attachments

FOLDERS = {"Linfox_Devinia Liddelow": "Linfox - DL",
           "Linfox_Bilal Virk": "Linfox - BVIRK"}
SALES = {"Linfox - DL": {"InvoiceID": "s1", "InvoiceNumber": "TCG-21185",
                         "Contact": {"Name": "Linfox ADIT"}},
         "Linfox - BVIRK": {"InvoiceID": "s2", "InvoiceNumber": "TCG-21180",
                            "Contact": {"Name": "Linfox ADIT"}}}
BILLS = {"Linfox - BVIRK": {"InvoiceID": "b2", "InvoiceNumber": "INV-0016",
                            "Contact": {"Name": "Techne IT Consulting Pty Ltd"}}}


def _f(folder, name):
    return {"name": name, "path": f"{folder}/{name}"}


def test_a_timesheet_goes_on_the_invoice_and_travels_with_it():
    got, bad = w.plan_attachments(
        [_f("Linfox_Bilal Virk", "BV_timesheet_2026-08-16_part1.png")],
        FOLDERS, SALES, BILLS)
    assert bad == []
    assert got[0]["Onto"] == "sales invoice"
    assert got[0]["Doc"] == "TCG-21180"
    assert got[0]["IncludeOnline"] is True


def test_a_contractor_invoice_goes_on_the_bill_and_does_not_travel():
    got, _ = w.plan_attachments(
        [_f("Linfox_Bilal Virk", "BV_invoice_2026-08-16.pdf")], FOLDERS, SALES, BILLS)
    assert got[0]["Onto"] == "bill"
    assert got[0]["Doc"] == "INV-0016"
    assert got[0]["IncludeOnline"] is False, "a supplier invoice must not be emailed out"


def test_a_paygs_own_invoice_has_no_bill_and_is_reported_not_guessed():
    """PAYG people are paid through payroll. There is no bill to attach to."""
    got, bad = w.plan_attachments(
        [_f("Linfox_Devinia Liddelow", "DL_invoice_2026-08-16.pdf")],
        FOLDERS, SALES, BILLS)
    assert got == []
    assert "no draft bill" in bad[0]["Why"]


def test_an_unknown_folder_is_reported_rather_than_attached_somewhere():
    got, bad = w.plan_attachments(
        [_f("Linfox_Someone New", "XX_timesheet_2026-08-16.png")], FOLDERS, SALES, BILLS)
    assert got == []
    assert "does not map to a contractor" in bad[0]["Why"]


def test_content_types_so_xero_renders_them():
    assert w.content_type_for("a.pdf") == "application/pdf"
    assert w.content_type_for("a.PNG") == "image/png"
    assert w.content_type_for("a.xlsx").endswith("spreadsheetml.sheet")
    assert w.content_type_for("noextension") == "application/octet-stream"


# ------------------------------------------------- submit only what is finished

def _inv(num, lines, total=1000):
    return {"InvoiceID": f"i-{num}", "InvoiceNumber": num,
            "Contact": {"Name": "Linfox"}, "LineItems": lines, "Total": total}


def test_only_a_filled_and_attached_invoice_is_submitted():
    filled = [{"Quantity": 10, "UnitAmount": 1406}]
    docs = [_inv("TCG-1", filled), _inv("TCG-2", filled),
            _inv("TCG-3", [{"Quantity": 0, "UnitAmount": 0}]), _inv("TCG-4", [])]
    ready, held = w.plan_submission(docs, {"i-TCG-1": {"ts.png"}, "i-TCG-2": set(),
                                           "i-TCG-3": {"x.pdf"}, "i-TCG-4": set()})
    assert [r["Doc"] for r in ready] == ["TCG-1"]
    why = {h["Doc"]: h["Why"] for h in held}
    assert why["TCG-2"] == "nothing attached"
    assert "still at zero" in why["TCG-3"]
    assert why["TCG-4"] == "no lines"


def test_status_changes_are_limited_to_draft_and_submitted():
    import pytest
    for bad in ("AUTHORISED", "PAID", "VOIDED", "DELETED"):
        with pytest.raises(ValueError):
            w.set_invoice_status(None, "id", bad)


def test_a_bill_with_no_item_code_is_matched_on_the_supplier_name():
    """Office cleaning is a repeating bill with a plain description, no item."""
    docs = [{"InvoiceID": "x", "InvoiceNumber": "53 North Rd",
             "Contact": {"Name": "That's Sparkling Clean"},
             "LineItems": [{"Description": "Office Cleaning", "Quantity": 1,
                            "UnitAmount": 132}]}]
    got = w.plan_number_change(docs, {"That's Sparkling Clean": "SC-4471"})
    assert [(g["Was"], g["Now"]) for g in got] == [("53 North Rd", "SC-4471")]


def test_the_reference_is_not_lost_when_the_days_were_filled_on_an_earlier_run():
    """Filling and referencing happen on different days of the billing week.
    Scoping the reference to 'what this pass filled' skipped every invoice."""
    ref = "3 August to 16 August 2026"
    already_filled = {"InvoiceID": "a", "InvoiceNumber": "TCG-21185", "Reference": "DL",
                      "Contact": {"Name": "Linfox"},
                      "LineItems": [{"ItemCode": "Linfox - DL", "Quantity": 10,
                                     "UnitAmount": 1406}]}
    planned, skipped, to_write = w.plan_line_fill([already_filled], {"Linfox - DL": 10},
                                                  "stamp")
    assert to_write == [], "nothing to fill - that is the situation under test"
    assert w.plan_reference_change([already_filled], ref)[0]["Now"] == ref
