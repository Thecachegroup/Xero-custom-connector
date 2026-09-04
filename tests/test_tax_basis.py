"""Every TCG document is TAX EXCLUSIVE. Rates are quoted and contracted ex GST
and the tax goes on top.

Andrew, 3 September 2026: "they should be set to exclusive, but sometimes one
or two sneak through with inclusive, and that can cause all sorts of problems."

An INCLUSIVE $1,000/day line bills $909.09 + $90.91 rather than $1,000 + $100.
The client is short-invoiced, the contractor is paid in full, and once it is
approved and sent it takes a credit note to unwind."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src import writes as w


def _doc(num, basis, qty=10):
    d = {"InvoiceID": f"id-{num}", "InvoiceNumber": num,
         "Contact": {"Name": "Linfox ADIT - Project Management"},
         "LineItems": [{"LineItemID": "l1", "ItemCode": "Linfox - DL",
                        "Quantity": qty, "UnitAmount": 1000.0}]}
    if basis is not None:
        d["LineAmountTypes"] = basis
    return d


def test_the_arithmetic_this_exists_to_prevent():
    """Not a code path - the reason. $1,000 a day, ten days, GST 10%."""
    exclusive = 10 * 1000.0
    assert exclusive == 10000.0 and round(exclusive * 1.10, 2) == 11000.00
    # the same line billed Inclusive: the 10,000 already contains the GST
    assert round(10000.0 / 1.10, 2) == 9090.91          # what the client pays ex
    assert round(10000.0 - 9090.91, 2) == 909.09        # the GST inside it
    # so TCG invoices $909.09 less revenue than it contracted


def test_exclusive_is_silent():
    assert w.tax_basis_problems([_doc("TCG-1", "Exclusive")]) == []


def test_inclusive_is_reported():
    out = w.tax_basis_problems([_doc("TCG-2", "Inclusive")])
    assert len(out) == 1
    assert out[0]["Tax basis"] == "Inclusive"
    assert "short-invoiced" in out[0]["Why it matters"]


def test_notax_is_reported_but_not_called_wrong():
    """BASEXCLUDED is right on the offshore bills and wrong on anything
    carrying GST, so it is shown rather than treated as a fault."""
    out = w.tax_basis_problems([_doc("TCG-3", "NoTax")])
    assert len(out) == 1 and out[0]["Tax basis"] == "NoTax"
    assert "offshore" in out[0]["Why it matters"]


def test_a_missing_field_is_not_evidence_of_anything():
    """Xero not sending LineAmountTypes is not the same as it being wrong."""
    assert w.tax_basis_problems([_doc("TCG-4", None)]) == []


def test_an_inclusive_invoice_is_held_back_from_awaiting_approval():
    """The strongest guard. Once approved and sent it needs a credit note."""
    ready, held = w.plan_submission([_doc("TCG-5", "Inclusive")], {}, False)
    assert ready == []
    assert held[0]["Why"] == "TAX INCLUSIVE - must be Exclusive"


def test_an_exclusive_invoice_still_submits():
    ready, held = w.plan_submission([_doc("TCG-6", "Exclusive")], {}, False)
    assert held == [] and ready[0]["Doc"] == "TCG-6"


def test_a_zero_line_still_holds_it_first():
    """The existing rule is not displaced - an invoice billing nothing is not
    an invoice, whatever its tax basis."""
    ready, held = w.plan_submission([_doc("TCG-7", "Inclusive", qty=0)], {}, False)
    assert ready == [] and "still at zero" in held[0]["Why"]


# ---------------------------------------------------------------------------
# 4 September 2026. The guard above fired six times in one run and every one of
# the six was correct as it stood: That's Sparkling Clean, DITYA, Logic Lanes,
# PRAVID, VVR and Karen Crabb. Five carry no GST at all, so the basis cannot do
# anything; the sixth bills a GST-inclusive total on purpose.
#
# Left alone it does not merely nag - plan_submission HOLDS every one of those
# bills out of Awaiting Approval, for ever, and the only way through is to set
# them Exclusive, which is wrong for all six. A guard that blocks correct work
# is a bug.
# ---------------------------------------------------------------------------

def _offshore(num, basis="Inclusive", tax_type="BASEXCLUDED", tax_amount=0.0,
              contact="PRAVID TECHNOLOGIES PVT LTD"):
    """A bill with no GST on it - the offshore monthlies and Karen Crabb's NZ one."""
    return {"InvoiceID": f"id-{num}", "InvoiceNumber": num, "Type": "ACCPAY",
            "Contact": {"Name": contact},
            "LineAmountTypes": basis,
            "LineItems": [{"LineItemID": "l1", "ItemCode": "Linfox - PD",
                           "Quantity": 21, "UnitAmount": 295.0,
                           "TaxType": tax_type, "TaxAmount": tax_amount}]}


def _cleaner(num, basis="Inclusive"):
    """That's Sparkling Clean: $132 IS the GST-inclusive total, no item code."""
    return {"InvoiceID": f"id-{num}", "InvoiceNumber": num, "Type": "ACCPAY",
            "Contact": {"Name": "That's Sparkling Clean"},
            "LineAmountTypes": basis,
            "LineItems": [{"LineItemID": "l1", "Description": "Office cleaning",
                           "Quantity": 1, "UnitAmount": 132.0,
                           "TaxType": "INPUT", "TaxAmount": 12.0}]}


OK = frozenset({"That's Sparkling Clean"})


def test_carries_gst_reads_the_tax_amount_first():
    assert w.carries_gst(_offshore("B1")) is False
    assert w.carries_gst(_cleaner("B2")) is True


def test_carries_gst_falls_back_to_the_tax_type():
    d = _offshore("B3")
    for li in d["LineItems"]:
        li.pop("TaxAmount")
    assert w.carries_gst(d) is False
    d["LineItems"][0]["TaxType"] = "INPUT"
    assert w.carries_gst(d) is True


def test_carries_gst_says_it_cannot_tell_rather_than_guessing():
    """None is not False. A template read back with no tax fields must keep the
    old, stricter behaviour rather than being waved through as GST-free."""
    d = _offshore("B4")
    for li in d["LineItems"]:
        li.pop("TaxAmount"); li.pop("TaxType")
    assert w.carries_gst(d) is None
    assert w.carries_gst({"LineItems": []}) is None


def test_a_gst_free_bill_is_not_reported():
    """Inclusive and Exclusive are the same number on a zero-rated line."""
    assert w.tax_basis_problems([_offshore("B5")]) == []


def test_a_gst_free_bill_is_not_held_back():
    """The one that was actually costing work."""
    ready, held = w.plan_submission([_offshore("B6")], {}, False)
    assert held == [] and ready[0]["Doc"] == "B6"


def test_the_cleaner_is_still_flagged_when_not_named():
    """It really does carry 10% GST, so nothing about it is automatic - it is
    exempt only because Andrew says so, in config."""
    out = w.tax_basis_problems([_cleaner("B7")])
    assert len(out) == 1 and out[0]["Tax basis"] == "Inclusive"
    ready, held = w.plan_submission([_cleaner("B8")], {}, False)
    assert ready == [] and held[0]["Why"] == "TAX INCLUSIVE - must be Exclusive"


def test_the_cleaner_is_exempt_once_named_in_config():
    assert w.tax_basis_problems([_cleaner("B9")], inclusive_ok=OK) == []
    ready, held = w.plan_submission([_cleaner("B10")], {}, False,
                                    inclusive_ok=OK)
    assert held == [] and ready[0]["Doc"] == "B10"


def test_the_name_match_ignores_case_and_padding():
    ok = frozenset({"  thats sparkling clean  ".replace("thats", "that's")})
    assert w.tax_basis_problems([_cleaner("B11")], inclusive_ok=ok) == []


def test_a_real_inclusive_fault_still_bites():
    """The whole point of the guard. A Linfox day-rate line at 10% GST set
    Inclusive short-invoices the client and must still be held."""
    d = _doc("TCG-99", "Inclusive")
    d["LineItems"][0].update(TaxType="OUTPUT", TaxAmount=909.09)
    assert len(w.tax_basis_problems([d], inclusive_ok=OK)) == 1
    ready, held = w.plan_submission([d], {}, False, inclusive_ok=OK)
    assert ready == [] and held[0]["Why"] == "TAX INCLUSIVE - must be Exclusive"


def test_an_undeterminable_document_keeps_the_strict_behaviour():
    """carries_gst returning None must not open the gate."""
    d = _doc("TCG-98", "Inclusive")          # no TaxType, no TaxAmount
    assert w.carries_gst(d) is None
    assert len(w.tax_basis_problems([d])) == 1
    ready, held = w.plan_submission([d], {}, False)
    assert ready == [] and held[0]["Why"] == "TAX INCLUSIVE - must be Exclusive"


def test_a_zero_line_still_holds_a_gst_free_bill():
    """The quantity rule is not displaced by any of this."""
    d = _offshore("B12")
    d["LineItems"][0]["Quantity"] = 0
    ready, held = w.plan_submission([d], {}, False)
    assert ready == [] and "still at zero" in held[0]["Why"]


# ---------------------------------------------------------------------------
# BILLS ONLY. Andrew, 4 September 2026. A bill is the supplier's document and
# they choose the basis; a TCG sales invoice is ours and is contracted ex GST,
# so Inclusive on one of those is always a fault and always held.
# ---------------------------------------------------------------------------

def _gst_free_sale(num):
    """A TCG SALES invoice, Inclusive, with no GST on the line. Still wrong."""
    d = _offshore(num, contact="Linfox ADIT - Project Management")
    d["Type"] = "ACCREC"
    return d


def test_no_carve_out_on_a_sales_invoice_even_with_no_gst():
    out = w.tax_basis_problems([_gst_free_sale("S1")], "sales")
    assert len(out) == 1 and out[0]["Tax basis"] == "Inclusive"
    ready, held = w.plan_submission([_gst_free_sale("S2")], {}, False,
                                    kind="ACCREC")
    assert ready == [] and held[0]["Why"] == "TAX INCLUSIVE - must be Exclusive"


def test_no_carve_out_on_a_sales_invoice_even_for_a_named_supplier():
    """The allow-list is a supplier list. It cannot excuse a sales invoice."""
    d = _cleaner("S3")
    d["Type"] = "ACCREC"
    assert len(w.tax_basis_problems([d], "sales", inclusive_ok=OK)) == 1


def test_the_label_decides_when_xero_did_not_send_a_type():
    """A trimmed payload must not silently become a bill."""
    d = _offshore("S4")
    d.pop("Type")
    assert w.tax_basis_problems([d], "bill") == []
    assert len(w.tax_basis_problems([d], "sales")) == 1
    assert len(w.tax_basis_problems([d], "invoice")) == 1


def test_a_contact_matched_bill_number_is_marked_for_the_reference():
    """That's Sparkling Clean reconciles on their own reference, so the number
    has to reach the Reference field too - not just the number field."""
    docs = [{"InvoiceID": "sc1", "InvoiceNumber": "SC", "Type": "ACCPAY",
             "Contact": {"Name": "That's Sparkling Clean"},
             "LineItems": [{"Description": "Office cleaning", "Quantity": 1}]}]
    got = w.plan_number_change(docs, {"That's Sparkling Clean": "SC-4471"})
    assert len(got) == 1
    assert got[0]["Now"] == "SC-4471" and got[0]["ByContact"] is True


def test_an_item_matched_bill_number_is_not():
    """A contractor bill carries TCG's own period reference logic; their
    invoice number must not overwrite it."""
    docs = [{"InvoiceID": "b1", "InvoiceNumber": "Inv", "Type": "ACCPAY",
             "Contact": {"Name": "Techne IT Consulting Pty Ltd"},
             "LineItems": [{"ItemCode": "Linfox - BVIRK", "Quantity": 10}]}]
    got = w.plan_number_change(docs, {"Linfox - BVIRK": "INV-0017"})
    assert len(got) == 1 and got[0]["ByContact"] is False
