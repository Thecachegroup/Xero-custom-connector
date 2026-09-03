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
