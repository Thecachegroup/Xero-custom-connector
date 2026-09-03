"""Tracked-inventory coverage, the submit-time stock guard, the cadence and
exclusion filters, and payroll tax keyed on item code.

The acceptance case throughout is the fortnight ending 30 August 2026 AS IT
STOOD ON 2 SEPTEMBER, before the three missing adjustments were posted by hand:

    Linfox - DL   Devinia Liddelow   10 billed, 10 on hand   posted
    Linfox - DTL  Dat Tien Le        10 billed,  0 on hand   NOT posted
    Linfox - KBJ  Kshitija Jachak     8 billed,  0 on hand   NOT posted
    Linfox - EK   Emily Kimmins       9 billed,  0 on hand   NOT posted
    Linfox - JG   Jerry Gonsalves    untracked - never appears
    Linfox - MAZ  Mazher Ali         untracked - never appears
"""

import pytest

from src import coverage, mappers, writes


# --------------------------------------------------------------- the fixture

def _item(code, name, tracked, qoh):
    return {"Code": code, "Name": name,
            "IsTrackedAsInventory": tracked, "QuantityOnHand": qoh}


ITEMS = [
    _item("Linfox - DL", "Devinia Liddelow", True, 10),
    _item("Linfox - DTL", "Dat Tien Le", True, 0),
    _item("Linfox - KBJ", "Kshitija Jachak", True, 0),
    _item("Linfox - EK", "Emily Kimmins", True, 9),
    _item("Linfox - DV", "Don Vuong", True, 10),
    _item("Linfox - JG", "Jerry Gonsalves", False, 0),
    _item("Linfox - MAZ", "Mazher Ali", False, 0),
    _item("SEEK Ad", "SEEK advertisement", True, 0),
]

# EK is at 9 in ITEMS so the "posted since" state can be expressed too; the
# 2 September state overrides it below.
AS_AT_2_SEP = [
    _item("Linfox - DL", "Devinia Liddelow", True, 10),
    _item("Linfox - DTL", "Dat Tien Le", True, 0),
    _item("Linfox - KBJ", "Kshitija Jachak", True, 0),
    _item("Linfox - EK", "Emily Kimmins", True, 0),
    _item("Linfox - JG", "Jerry Gonsalves", False, 0),
    _item("Linfox - MAZ", "Mazher Ali", False, 0),
]


def _doc(num, code, qty, contact="Linfox Pty Ltd", status="SUBMITTED", **kw):
    d = {"InvoiceNumber": num, "InvoiceID": f"id-{num}", "Status": status,
         "Contact": {"Name": contact}, "Type": "ACCREC",
         "LineAmountTypes": "Exclusive",
         "LineItems": [{"ItemCode": code, "Quantity": qty,
                        "LineItemID": f"li-{num}"}]}
    d.update(kw)
    return d


AS_AT_2_SEP_DOCS = [
    _doc("TCG-21200", "Linfox - DL", 10),
    _doc("TCG-21201", "Linfox - DTL", 10),
    _doc("TCG-21202", "Linfox - KBJ", 8),
    _doc("TCG-21203", "Linfox - EK", 9),
    _doc("TCG-21204", "Linfox - JG", 10),
    _doc("TCG-21206", "Linfox - MAZ", 5),
]


def _verdicts(rows):
    return {r["Item code"]: r["Verdict"] for r in rows}


# ------------------------------------------------------------ the acceptance

def test_the_30_august_fortnight_as_it_stood_on_2_september():
    """The whole reason this module exists. Three adjustments were never posted
    and nothing noticed for a week."""
    rows = coverage.plan_coverage(AS_AT_2_SEP_DOCS, AS_AT_2_SEP)
    v = _verdicts(rows)
    assert v["Linfox - DL"] == "OK"
    assert v["Linfox - DTL"].startswith("SHORT 10")
    assert v["Linfox - KBJ"].startswith("SHORT 8")
    assert v["Linfox - EK"].startswith("SHORT 9")


def test_untracked_items_do_not_appear_at_all():
    """Not reported as OK - not reported. They need no adjustment ever, and
    listing them buries the ones that do. The six-SEEK-ads failure."""
    rows = coverage.plan_coverage(AS_AT_2_SEP_DOCS, AS_AT_2_SEP)
    codes = {r["Item code"] for r in rows}
    assert "Linfox - JG" not in codes
    assert "Linfox - MAZ" not in codes
    assert len(rows) == 4


def test_faults_sort_to_the_top():
    rows = coverage.plan_coverage(AS_AT_2_SEP_DOCS, AS_AT_2_SEP)
    assert rows[0]["_fault"] is True
    assert rows[-1]["Item code"] == "Linfox - DL"          # the only OK


def test_everything_reads_ok_once_the_adjustments_are_posted():
    posted = [_item("Linfox - DL", "Devinia Liddelow", True, 10),
              _item("Linfox - DTL", "Dat Tien Le", True, 10),
              _item("Linfox - KBJ", "Kshitija Jachak", True, 8),
              _item("Linfox - EK", "Emily Kimmins", True, 9)]
    rows = coverage.plan_coverage(AS_AT_2_SEP_DOCS[:4], posted)
    assert all(r["Verdict"] == "OK" for r in rows)
    assert not any(r["_fault"] for r in rows)


# --------------------------------- an approved invoice has already spent it

def test_an_approved_invoice_at_zero_on_hand_is_not_short():
    """THE ONE THING THE REPORT TURNS ON. Live, 3 September 2026: Bhasker Veela,
    Linfox - BV, 21 days across two AUTHORISED invoices, 0 on hand. His cost
    comes through a BILL, which puts the quantity in automatically, and the
    approved invoice takes it straight back out. Zero is the correct end state.
    Counting his approved 21 as demand reports SHORT 21 on an item in perfect
    order - the eighty-lines-one-real failure, again."""
    items = [_item("Linfox - BV", "Bhasker Veela", True, 0)]
    docs = [_doc("TCG-21205", "Linfox - BV", 5, status="AUTHORISED"),
            _doc("TCG-21208", "Linfox - BV", 16, status="AUTHORISED")]
    rows = coverage.plan_coverage(docs, items)
    assert rows[0]["Verdict"].startswith("APPROVED")
    assert rows[0]["_fault"] is False
    assert rows[0]["Already approved"] == "21"
    assert rows[0]["Days billed"] == "0"


def test_an_approved_invoice_that_went_negative_is_still_the_loudest_case():
    items = [_item("Linfox - BV", "Bhasker Veela", True, -21)]
    docs = [_doc("TCG-21205", "Linfox - BV", 21, status="AUTHORISED")]
    rows = coverage.plan_coverage(docs, items)
    assert rows[0]["Verdict"].startswith("NEGATIVE")
    assert rows[0]["_fault"] is True


def test_a_pending_invoice_beside_an_approved_one_still_needs_its_own_stock():
    items = [_item("Linfox - DL", "Devinia Liddelow", True, 0)]
    docs = [_doc("TCG-1", "Linfox - DL", 10, status="AUTHORISED"),
            _doc("TCG-2", "Linfox - DL", 10, status="DRAFT")]
    rows = coverage.plan_coverage(docs, items)
    assert rows[0]["Verdict"].startswith("SHORT 10")


def test_voided_and_deleted_documents_are_ignored():
    items = [_item("Linfox - DL", "Devinia Liddelow", True, 10)]
    docs = [_doc("TCG-1", "Linfox - DL", 10, status="DRAFT"),
            _doc("TCG-2", "Linfox - DL", 10, status="VOIDED"),
            _doc("TCG-3", "Linfox - DL", 10, status="DELETED")]
    rows = coverage.plan_coverage(docs, items)
    assert rows[0]["Verdict"] == "OK"


def test_a_document_with_no_status_is_treated_as_pending():
    """The safe reading of an unknown status is the one that still checks."""
    items = [_item("Linfox - DL", "Devinia Liddelow", True, 0)]
    d = _doc("TCG-1", "Linfox - DL", 10)
    d.pop("Status")
    assert coverage.plan_coverage([d], items)[0]["Verdict"].startswith("SHORT 10")


# ----------------------------------------------------------------- verdicts

def test_over_names_the_duplicate():
    assert coverage.verdict_for(8, 10).startswith("OVER 2")


def test_negative_outranks_short():
    """Below zero means an invoice was already approved against stock that was
    never there. That is past the point SHORT is warning about."""
    assert coverage.verdict_for(10, -3).startswith("NEGATIVE")


def test_over_is_not_a_fault_but_short_and_negative_are():
    assert coverage.is_fault("SHORT 10 - adjustment not posted")
    assert coverage.is_fault("NEGATIVE - approved against stock that was never there")
    assert not coverage.is_fault("OVER 2 - check for a duplicate adjustment")
    assert not coverage.is_fault("OK")


def test_float_noise_is_not_a_shortfall():
    assert coverage.verdict_for(10, 10.00000001) == "OK"


def test_a_fractional_shortfall_is_reported_honestly():
    """Deepti Bansal's item sits at 0.0001 on hand against a monthly quantity of
    1. That is a real gap, not noise, and it is not rounded away."""
    assert coverage.verdict_for(1, 0.0001).startswith("SHORT 0.9999")


# ------------------------------------------------------------ the match key

def test_the_leading_z_is_not_stripped_here():
    """mappers.normalise_code folds 'zLinfox - MA' onto 'Linfox - MA' because
    for a LEDGER they are one person. For a STOCK COUNT they are two separate
    Xero items each holding their own balance, and pooling them would report a
    number neither of them has."""
    assert mappers.normalise_code("zLinfox - MA") == "linfox - ma"
    assert coverage.code_key("zLinfox - MA") == "zlinfox - ma"
    assert coverage.code_key("Linfox - MA") != coverage.code_key("zLinfox - MA")


def test_code_matching_is_case_and_space_tolerant():
    idx = coverage.stock_index(ITEMS)
    assert idx[coverage.code_key("  linfox - dl  ")]["Code"] == "Linfox - DL"


def test_ignored_codes_are_dropped():
    docs = AS_AT_2_SEP_DOCS + [_doc("TCG-9", "SEEK Ad", 1)]
    rows = coverage.plan_coverage(docs, ITEMS, ignore_codes={"SEEK Ad"})
    assert "SEEK Ad" not in {r["Item code"] for r in rows}


def test_a_code_with_no_xero_item_is_surfaced_not_swallowed():
    rows = coverage.plan_coverage([_doc("TCG-9", "Linfox - TYPO", 10)], ITEMS)
    assert rows[0]["Verdict"].startswith("UNKNOWN ITEM")
    assert rows[0]["_fault"] is True


# ------------------------------------------- quantities are one pool per item

def test_two_invoices_for_one_item_are_measured_together():
    """Ten days on each of two invoices against ten on hand looks fine on either
    one alone and takes the item to minus ten together."""
    docs = [_doc("TCG-1", "Linfox - DL", 10), _doc("TCG-2", "Linfox - DL", 10)]
    rows = coverage.plan_coverage(docs, ITEMS)
    assert rows[0]["Verdict"].startswith("SHORT 10")
    assert rows[0]["Docs"] == "TCG-1, TCG-2"


def test_one_item_on_two_lines_of_one_invoice_is_summed():
    d = _doc("TCG-1", "Linfox - DL", 6)
    d["LineItems"].append({"ItemCode": "Linfox - DL", "Quantity": 6})
    rows = coverage.plan_coverage([d], ITEMS)
    assert rows[0]["Verdict"].startswith("SHORT 2")


# ------------------------------------------------------- the submit-time guard

def _att(docs):
    return {d["InvoiceID"]: {"timesheet.pdf"} for d in docs}


def test_submit_holds_the_three_with_no_stock():
    stock = coverage.stock_index(AS_AT_2_SEP)
    docs = AS_AT_2_SEP_DOCS
    ready, held = writes.plan_submission(docs, _att(docs), stock=stock)
    assert {h["Doc"] for h in held} == {"TCG-21201", "TCG-21202", "TCG-21203"}
    assert all("INSUFFICIENT STOCK" in h["Why"] for h in held)
    # DL has its adjustment; the two untracked people are never held.
    assert {r["Doc"] for r in ready} == {"TCG-21200", "TCG-21204", "TCG-21206"}


def test_the_hold_names_the_item_and_both_numbers():
    stock = coverage.stock_index(AS_AT_2_SEP)
    docs = [_doc("TCG-21201", "Linfox - DTL", 10)]
    _, held = writes.plan_submission(docs, _att(docs), stock=stock)
    assert "Linfox - DTL needs 10, 0 on hand" in held[0]["Why"]


def test_require_stock_false_lets_them_through():
    stock = coverage.stock_index(AS_AT_2_SEP)
    docs = AS_AT_2_SEP_DOCS
    ready, held = writes.plan_submission(docs, _att(docs), stock=stock,
                                         require_stock=False)
    assert len(ready) == len(docs)
    assert held == []


def test_no_stock_index_means_the_guard_is_simply_off():
    """Bills are submitted with stock=None: a bill creates the cost, the sales
    invoice consumes the stock."""
    docs = AS_AT_2_SEP_DOCS
    ready, held = writes.plan_submission(docs, _att(docs), stock=None)
    assert len(ready) == len(docs)
    assert held == []


def test_the_pool_is_spent_in_document_order():
    """Ten on hand, two invoices of ten. The first goes, the second is held -
    not both, and not neither."""
    stock = coverage.stock_index(ITEMS)
    docs = [_doc("TCG-1", "Linfox - DL", 10), _doc("TCG-2", "Linfox - DL", 10)]
    ready, held = writes.plan_submission(docs, _att(docs), stock=stock)
    assert [r["Doc"] for r in ready] == ["TCG-1"]
    assert [h["Doc"] for h in held] == ["TCG-2"]


def test_a_document_held_for_another_reason_does_not_eat_the_stock():
    """Being held for a missing quantity must not also starve the invoice
    behind it."""
    stock = coverage.stock_index(ITEMS)
    docs = [_doc("TCG-1", "Linfox - DL", 0), _doc("TCG-2", "Linfox - DL", 10)]
    ready, held = writes.plan_submission(docs, _att(docs), stock=stock)
    assert [r["Doc"] for r in ready] == ["TCG-2"]
    assert held[0]["Why"] == "1 line(s) still at zero"


def test_the_zero_quantity_rule_still_outranks_everything():
    stock = coverage.stock_index(AS_AT_2_SEP)
    docs = [_doc("TCG-1", "Linfox - DTL", 0)]
    _, held = writes.plan_submission(docs, _att(docs), stock=stock)
    assert "still at zero" in held[0]["Why"]


def test_the_tax_basis_refusal_still_outranks_the_stock_guard():
    stock = coverage.stock_index(AS_AT_2_SEP)
    docs = [_doc("TCG-1", "Linfox - DTL", 10, LineAmountTypes="Inclusive")]
    _, held = writes.plan_submission(docs, _att(docs), stock=stock)
    assert "TAX INCLUSIVE" in held[0]["Why"]


# ------------------------------------------------- cadence and exclusion

MONTHLY = {"Linfox - BV", "Linfox - PD", "Linfox - DBL", "Linfox - VKC"}


def test_a_monthly_document_is_kept_out_of_the_fortnightly_run():
    """TCG-21205 (Bhasker Veela) went to Awaiting Approval with the fortnightly
    batch on 2 September 2026 and had to be pulled back to Draft by hand."""
    docs = [_doc("TCG-21200", "Linfox - DL", 10),
            _doc("TCG-21205", "Linfox - BV", 21)]
    kept, out = writes.split_excluded(docs, monthly_codes=MONTHLY,
                                      cadence="fortnightly")
    assert [d["InvoiceNumber"] for d in kept] == ["TCG-21200"]
    assert out[0]["Doc"] == "TCG-21205"
    assert "monthly cadence" in out[0]["Why"]


def test_cadence_monthly_runs_the_other_side():
    docs = [_doc("TCG-21200", "Linfox - DL", 10),
            _doc("TCG-21205", "Linfox - BV", 21)]
    kept, out = writes.split_excluded(docs, monthly_codes=MONTHLY,
                                      cadence="monthly")
    assert [d["InvoiceNumber"] for d in kept] == ["TCG-21205"]
    assert out[0]["Doc"] == "TCG-21200"


def test_cadence_all_restores_the_old_behaviour():
    docs = [_doc("TCG-21200", "Linfox - DL", 10),
            _doc("TCG-21205", "Linfox - BV", 21)]
    kept, out = writes.split_excluded(docs, monthly_codes=MONTHLY, cadence="all")
    assert len(kept) == 2 and out == []


def test_exclude_matches_number_contact_reference_and_item_code():
    docs = [_doc("TCG-21205", "Linfox - BV", 21),
            _doc("TCG-21200", "Linfox - DL", 10, contact="Linfox Armstrong"),
            _doc("TCG-21207", "Linfox - PD", 20, Reference="August 2026")]
    for pattern, gone in (("21205", "TCG-21205"),
                          ("armstrong", "TCG-21200"),
                          ("august", "TCG-21207"),
                          ("linfox - bv", "TCG-21205")):
        _, out = writes.split_excluded(docs, [pattern], cadence="all")
        assert [o["Doc"] for o in out] == [gone], pattern


def test_nothing_is_excluded_by_default():
    docs = [_doc("TCG-21200", "Linfox - DL", 10)]
    kept, out = writes.split_excluded(docs, [], cadence="all")
    assert len(kept) == 1 and out == []


# --------------------------------------------- payroll tax on the item code

import pandas as pd                                                   # noqa: E402
from datetime import date                                             # noqa: E402


RATE_CARD = pd.DataFrame([
    {"Name": "Rajeev Jindal", "*ItemCode": "zLinfox - RJ",
     "ItemName": "Rajeev Jindal", "SalesUnitPrice": 850.0,
     "PurchasesUnitPrice": 775.0, "Status": "Active"},
    {"Name": "Mazher Ali", "*ItemCode": "Linfox - MAZ", "ItemName": "Mazher Ali",
     "SalesUnitPrice": 1225.0, "PurchasesUnitPrice": 1000.0, "Status": "Active"},
    {"Name": "Mazher Ali", "*ItemCode": "zLinfox - MA", "ItemName": "Mazher Ali",
     "SalesUnitPrice": 1103.0, "PurchasesUnitPrice": 900.0, "Status": "Active"},
    {"Name": "Devinia Liddelow", "*ItemCode": "Linfox - DL",
     "ItemName": "Devinia Liddelow", "SalesUnitPrice": 1406.0,
     "PurchasesUnitPrice": 1250.0, "Status": "Active"},
])


def test_a_name_resolves_to_every_item_that_person_owns():
    """Not ambiguity to be dropped - all three items are Mazher's."""
    codes, unresolved = mappers.exempt_item_codes(RATE_CARD, ["Mazher Ali"])
    assert codes == {"linfox - maz", "linfox - ma"}
    assert unresolved == []


def test_the_leading_z_is_folded_for_the_exemption():
    """A retired contractor's exemption follows them into the archive instead
    of lapsing the day the item is renamed."""
    codes, _ = mappers.exempt_item_codes(RATE_CARD, ["Rajeev Jindal"])
    assert codes == {"linfox - rj"}


def test_a_name_with_no_item_is_reported_not_guessed():
    """A near miss here grants somebody else's exemption and takes money out of
    the payroll tax base, so the bar is exact equality."""
    codes, unresolved = mappers.exempt_item_codes(
        RATE_CARD, ["Rajeev Jindal - Solution Architect", "Jenny Drew"])
    assert codes == set()
    assert unresolved == ["Rajeev Jindal - Solution Architect", "Jenny Drew"]


def test_a_reversed_surname_first_name_still_resolves():
    codes, _ = mappers.exempt_item_codes(RATE_CARD, ["Jindal Rajeev"])
    assert codes == {"linfox - rj"}


def _sales(description, code):
    return pd.DataFrame([{
        "Description": description, "InventoryItemCode": code,
        "ContactName": "Linfox Pty Ltd", "InvoiceDate": date(2026, 7, 15),
        "Quantity": 10, "UnitAmount": 850.0, "LineAmount": 8500.0,
        "InvoiceNumber": "TCG-1", "Status": "AUTHORISED",
    }])


def _build(sales, codes):
    return mappers.build_data_frame(
        sales, pd.DataFrame(), pd.DataFrame(), RATE_CARD,
        customer_lookup={}, no_payroll_tax={"Rajeev Jindal"},
        no_payroll_tax_codes=codes, current_fy_start=date(2026, 7, 1))


def test_the_description_match_missed_a_line_carrying_a_role_title():
    """$95,876.29 wrongly in the base in July FY27 alone."""
    df = _build(_sales("Rajeev Jindal - Solution Architect", "zLinfox - RJ"),
                codes=set())
    assert df.loc[0, "Payroll Tax Payable"] == "Payable"


def test_the_item_code_catches_it():
    df = _build(_sales("Rajeev Jindal - Solution Architect", "zLinfox - RJ"),
                codes={"linfox - rj"})
    assert df.loc[0, "Payroll Tax Payable"] == "Not Payable"


def test_a_po_number_in_the_description_is_no_longer_a_miss():
    df = _build(_sales("P/O 4500123456", "zLinfox - RJ"),
                codes={"linfox - rj"})
    assert df.loc[0, "Payroll Tax Payable"] == "Not Payable"


def test_the_name_path_still_works_for_anyone_with_no_item():
    """Kept, not replaced - removing it would trade one silent miss for
    another."""
    df = _build(_sales("Rajeev Jindal", "zLinfox - RJ"), codes=set())
    assert df.loc[0, "Payroll Tax Payable"] == "Not Payable"


def test_nobody_else_becomes_exempt():
    df = _build(_sales("Devinia Liddelow", "Linfox - DL"),
                codes={"linfox - rj"})
    assert df.loc[0, "Payroll Tax Payable"] == "Payable"


def test_the_default_is_no_code_exemptions_at_all():
    """build_data_frame's new argument defaults to empty, so an old caller
    behaves exactly as it did."""
    df = mappers.build_data_frame(
        _sales("Devinia Liddelow", "Linfox - DL"), pd.DataFrame(),
        pd.DataFrame(), RATE_CARD, customer_lookup={},
        no_payroll_tax=set(), current_fy_start=date(2026, 7, 1))
    assert df.loc[0, "Payroll Tax Payable"] == "Payable"


# ------------------------------------------------------------------ the config

def test_both_config_shapes_load():
    """A bare list is the old file. It must still load, or an old copy would
    silently empty the exemption list and overstate the base."""
    import json, tempfile, os
    from src import server

    for blob, names, codes in (
        (["Andrew Hurnard"], {"Andrew Hurnard"}, set()),
        ({"names": ["Andrew Hurnard"], "item_codes": ["Linfox - DL"]},
         {"Andrew Hurnard"}, {"Linfox - DL"}),
    ):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(blob, fh)
        fh.close()
        os.environ["TCG_NO_PAYROLL_TAX"] = fh.name
        try:
            assert server._payroll_tax_config() == (names, codes)
        finally:
            os.environ.pop("TCG_NO_PAYROLL_TAX", None)
            os.unlink(fh.name)


def test_the_shipped_config_parses_and_is_not_empty():
    import json
    with open("config/no_payroll_tax.json") as fh:
        blob = json.load(fh)
    assert isinstance(blob, dict)
    assert len(blob["names"]) > 40
    assert "item_codes" in blob
