"""A repeating template writes an invoice every period with nobody looking at
it. It is the most consequential write in the connector and until now it was
the only one with no tests and no write guard."""
import sys, pathlib, os
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src import writes as w


class _Client:
    """Captures the payload instead of sending it."""
    def __init__(self):
        self.sent = None


def _template(qty=1.0, status="AUTHORISED", tid="rep-1"):
    return {
        "RepeatingInvoiceID": tid,
        "Type": "ACCREC",
        "Status": status,
        "Contact": {"ContactID": "c-1", "Name": "Linfox Australia Pty Ltd"},
        "Schedule": {"Period": 1, "Unit": "MONTHLY", "DueDate": 20},
        "SubTotal": 320.0, "TotalTax": 32.0, "Total": 352.0,
        "LineItems": [{
            "LineItemID": "li-1", "ItemCode": "Linfox - BV",
            "Description": "Bhasker Veela - SAP Consultant",
            "Quantity": qty, "UnitAmount": 320.0,
            "LineAmount": 320.0, "TaxAmount": 32.0,
            "AccountCode": "200", "TaxType": "OUTPUT",
        }],
    }


def _capture(monkeypatch):
    seen = {}
    monkeypatch.setattr(w, "_post",
                        lambda client, url, payload: seen.update(url=url, payload=payload) or {"ok": True})
    return seen


def test_a_template_write_is_refused_when_writes_are_off(monkeypatch):
    """Every other write in the connector goes through this gate. This one used
    to bypass writes.py entirely and post straight from xero_client."""
    monkeypatch.delenv("TCG_WRITE_ENABLED", raising=False)
    with pytest.raises(w.WritesDisabled):
        w.update_repeating_template(_Client(), _template())


def test_derived_money_fields_are_not_echoed_back(monkeypatch):
    """Posting Quantity=0 beside a stale LineAmount of 320 asks Xero to
    reconcile two answers. Which one wins is not something to discover on a
    live client template - so the derived fields do not go."""
    monkeypatch.setenv("TCG_WRITE_ENABLED", "true")
    seen = _capture(monkeypatch)
    t = _template()
    t["LineItems"][0]["Quantity"] = 0.0
    w.update_repeating_template(_Client(), t)

    body = seen["payload"]["RepeatingInvoices"][0]
    assert "RepeatingInvoices" in seen["url"]
    for gone in ("SubTotal", "TotalTax", "Total"):
        assert gone not in body
    line = body["LineItems"][0]
    for gone in ("LineAmount", "TaxAmount"):
        assert gone not in line
    # everything Xero needs to keep the template identical is still there
    assert body["RepeatingInvoiceID"] == "rep-1"
    assert body["Schedule"]["Unit"] == "MONTHLY"
    assert body["Contact"]["ContactID"] == "c-1"
    assert line["Quantity"] == 0.0 and line["UnitAmount"] == 320.0
    assert line["LineItemID"] == "li-1", "or Xero adds a second line beside it"
    assert line["AccountCode"] == "200" and line["TaxType"] == "OUTPUT"


def test_a_template_with_no_id_is_refused(monkeypatch):
    """Xero would create a SECOND template rather than update this one, and
    then two templates would invoice the same client every month."""
    monkeypatch.setenv("TCG_WRITE_ENABLED", "true")
    _capture(monkeypatch)
    t = _template()
    del t["RepeatingInvoiceID"]
    with pytest.raises(ValueError):
        w.update_repeating_template(_Client(), t)


def test_a_deleted_template_is_refused(monkeypatch):
    monkeypatch.setenv("TCG_WRITE_ENABLED", "true")
    _capture(monkeypatch)
    with pytest.raises(ValueError):
        w.update_repeating_template(_Client(), _template(status="DELETED"))


# --------------------------------------------------------------- the listing
# A first pass over the live org flagged 80 lines. One was wrong. A report where
# nine flags in ten are noise is a report nobody reads - the same failure the
# coverage report had when it came back with six SEEK ads in it.

from src import server as S


def _t(typ, contact, code, qty, unit, status="DRAFT"):
    return {"RepeatingInvoiceID": f"{contact}|{code}|{qty}", "Type": typ,
            "Status": status, "Contact": {"Name": contact},
            "Schedule": {"Period": 1, "Unit": "MONTHLY"},
            "LineItems": [{"ItemCode": code, "Quantity": qty, "UnitAmount": unit}]}


def _listing(monkeypatch, templates, **kw):
    monkeypatch.setattr(S, "client", lambda: type("C", (), {
        "repeating_invoices": lambda self: templates})())
    return S.list_repeating_templates(**kw)


BHASKER = [_t("ACCREC", "Linfox - Warehouse Solutions (Andrew)", "Linfox - BV", 1, 320),
           _t("ACCPAY", "VVR Consulting", "Linfox - BV", 0, 278)]

# Deepti and Vivek: an annual fee split over twelve, billed as one unit of a
# monthly amount. Both sides sit at 1 and that is CORRECT - zeroing either one
# stops it billing.
MONTHLY_FEE = [_t("ACCREC", "Linfox - Warehouse Solutions", "Linfox - DBL", 1, 9583.33),
               _t("ACCPAY", "Logic Lanes", "Linfox - DBL", 1, 8333.33)]


def test_the_asymmetric_day_rate_line_is_flagged(monkeypatch):
    """Bhasker's bill sits at 0 and his sale at 1. One of the pair was reset and
    the other was missed - that asymmetry is the signal."""
    out = _listing(monkeypatch, BHASKER + MONTHLY_FEE)
    assert "1 DAY-RATE TEMPLATE(S)" in out
    assert "Linfox - BV" in out.split("FIXED-FEE")[0]


def test_a_fixed_monthly_fee_is_not_a_fault(monkeypatch):
    """Both sides non-zero, no live template anywhere carrying that code at
    zero, so nothing is filling it from a timesheet."""
    out = _listing(monkeypatch, MONTHLY_FEE)
    assert "No day-rate template" in out
    assert "2 FIXED-FEE line(s)" in out and "Logic Lanes" in out
    assert "Linfox - DBL" in _listing(monkeypatch, MONTHLY_FEE, include_fixed_fee=True)


def test_deleted_templates_are_hidden_by_default(monkeypatch):
    """62 of the first 80 flags. A deleted template generates nothing."""
    dead = [_t("ACCREC", "Toll Group", f"zToll-{i}", 10, 1100, status="DELETED")
            for i in range(12)]
    out = _listing(monkeypatch, BHASKER + dead)
    assert "zToll-0" not in out
    assert "12 non-zero line(s) on DELETED templates hidden" in out
    assert "zToll-0" in _listing(monkeypatch, BHASKER + dead, include_deleted=True)


def test_a_line_with_no_item_code_is_never_flagged(monkeypatch):
    """The Xenon monthly billing and the office cleaning. One unit of an
    amount, and there is no item code for a timesheet to fill."""
    out = _listing(monkeypatch, [_t("ACCPAY", "That's Sparkling Clean", "", 1, 132)])
    assert "No day-rate template" in out
    assert "1 FIXED-FEE line(s)" in out
