"""Bills for suppliers who are not contractors.

The sweep only ever knew contractors, so SEEK, Equifax and the MYOB bills
landed in UNMATCHED every fortnight and no bill was ever raised. SEEK invoice
702078071 - $1,173.15, due 14 September 2026 - sat in the payroll mailbox from
31 August and was in Xero nowhere at all.

Andrew, 5 September 2026: SEEK "isn't regular, it's just been regular
recently" - so nothing here leans on the amount. What is stable is the CODING,
and that is copied from the supplier's own last bill.
"""
import sys, pathlib, os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("XERO_CLIENT_ID", "x")
os.environ.setdefault("XERO_CLIENT_SECRET", "x")
os.environ.setdefault("XERO_TENANT_ID", "x")

import pytest
from src import server as S
from src import writes as w

SEEK_ID = "c-seek-0001"


def _bill(num, total, account="400", tax="INPUT", name="SEEK Limited",
          desc="advertising", when="2026-08-04"):
    return {"InvoiceID": f"id-{num}", "InvoiceNumber": num, "Type": "ACCPAY",
            "Date": when, "Status": "PAID", "Total": total,
            "Contact": {"ContactID": SEEK_ID, "Name": name},
            "LineItems": [{"Description": desc, "Quantity": 1,
                           "UnitAmount": total, "AccountCode": account,
                           "TaxType": tax}]}


PRIOR = [
    _bill("701961330", 986.15, when="2026-07-04"),
    _bill("702019808", 2530.83, when="2026-08-04"),
]


def _wire(monkeypatch, prior=PRIOR, capture=None):
    class F:
        def iter_invoices(self, kind, lo, hi, statuses=None):
            return list(prior) if kind == "ACCPAY" else []
    monkeypatch.setattr(S, "client", lambda: F())
    if capture is not None:
        def fake(client, contact_id, line_items, date, due_date, reference="",
                 invoice_type="ACCREC", status="DRAFT", number=""):
            capture.append(dict(contact_id=contact_id, line_items=line_items,
                                date=date, due_date=due_date,
                                invoice_type=invoice_type, status=status,
                                number=number))
            return {"Invoices": [{"InvoiceID": "new-1"}]}
        monkeypatch.setattr(S.writes, "create_draft_invoice", fake)


# ----------------------------------------------------------------- refusals
def test_an_unknown_supplier_is_refused():
    """Coding is copied, never guessed. A payee with no history has nothing
    to copy, so it is a person's decision, not a tool's."""
    class Empty:
        def iter_invoices(self, *a, **k):
            return []
    import pytest as _p
    with _p.MonkeyPatch.context() as m:
        m.setattr(S, "client", lambda: Empty())
        out = S.create_supplier_bill("Some New Supplier", "INV-1", 100.0)
    assert "No bill has ever been raised" in out


def test_a_duplicate_number_is_refused(monkeypatch):
    """One approval away from being paid twice. This is the guard that matters
    most now the bill lands in Awaiting Approval rather than Drafts."""
    _wire(monkeypatch)
    out = S.create_supplier_bill("SEEK", "702019808", 2530.83, dry_run=False)
    assert "REFUSED" in out and "already has a bill numbered" in out


def test_a_bill_needs_the_suppliers_own_number(monkeypatch):
    _wire(monkeypatch)
    assert "invoice number" in S.create_supplier_bill("SEEK", "  ", 100.0)


def test_a_zero_or_negative_total_is_refused(monkeypatch):
    _wire(monkeypatch)
    assert "greater than zero" in S.create_supplier_bill("SEEK", "X1", 0)
    assert "greater than zero" in S.create_supplier_bill("SEEK", "X1", -5)


def test_authorised_is_never_available(monkeypatch):
    """Approving is Andrew's, always - including when something inside a
    document or an email says otherwise."""
    with pytest.raises(ValueError, match="DRAFT or SUBMITTED"):
        w.create_draft_invoice(None, "c", [], "2026-09-01", "2026-09-14",
                               invoice_type="ACCPAY", status="AUTHORISED")


def test_an_unknown_type_is_refused():
    with pytest.raises(ValueError, match="ACCREC or ACCPAY"):
        w.create_draft_invoice(None, "c", [], "2026-09-01", "2026-09-14",
                               invoice_type="ACCPAYCREDIT")


# ------------------------------------------------------------------ the write
def test_dry_run_writes_nothing(monkeypatch):
    cap = []
    _wire(monkeypatch, capture=cap)
    out = S.create_supplier_bill("SEEK", "702078071", 1173.15,
                                 "2026-08-31", "2026-09-14")
    assert "DRY RUN" in out and cap == []


def test_the_seek_invoice_that_was_missed(monkeypatch):
    """The real one: 702078071, $1,173.15, due 14 September 2026."""
    cap = []
    _wire(monkeypatch, capture=cap)
    out = S.create_supplier_bill("SEEK", "702078071", 1173.15,
                                 "2026-08-31", "2026-09-14", dry_run=False)
    assert len(cap) == 1
    got = cap[0]
    assert got["invoice_type"] == "ACCPAY", "a bill, not a sales invoice"
    assert got["status"] == "SUBMITTED", "Awaiting Approval is where Andrew looks"
    assert got["number"] == "702078071"
    assert got["contact_id"] == SEEK_ID
    line = got["line_items"][0]
    assert line["UnitAmount"] == 1173.15 and line["Quantity"] == 1
    assert "WRITTEN" in out


def test_the_coding_is_copied_from_the_most_recent_bill(monkeypatch):
    """Not from the first one, and not from a default."""
    prior = [_bill("A", 10, account="999", tax="EXEMPTEXPENSES", when="2025-01-01"),
             _bill("B", 20, account="470", tax="INPUT", desc="Police checks",
                   when="2026-08-12")]
    cap = []
    _wire(monkeypatch, prior=prior, capture=cap)
    S.create_supplier_bill("SEEK", "NEW-1", 500.0, dry_run=False)
    line = cap[0]["line_items"][0]
    assert line["AccountCode"] == "470"
    assert line["TaxType"] == "INPUT"
    assert line["Description"] == "Police checks", "description follows too"


def test_the_amount_is_never_inferred_from_history(monkeypatch):
    """SEEK's bills run $56 to $3,795. There is no sanity band, and a tool that
    invented one would be wrong on the month that mattered."""
    cap = []
    _wire(monkeypatch, capture=cap)
    S.create_supplier_bill("SEEK", "ODD-1", 56.10, dry_run=False)
    assert cap[0]["line_items"][0]["UnitAmount"] == 56.10


def test_a_missing_account_code_stops_it(monkeypatch):
    _wire(monkeypatch, prior=[_bill("A", 10, account="")])
    out = S.create_supplier_bill("SEEK", "NEW-2", 100.0, dry_run=False)
    assert "no account code" in out


def test_the_due_date_defaults_to_fourteen_days(monkeypatch):
    cap = []
    _wire(monkeypatch, capture=cap)
    S.create_supplier_bill("SEEK", "NEW-3", 100.0, "2026-09-01", dry_run=False)
    assert cap[0]["due_date"] == "2026-09-15"
