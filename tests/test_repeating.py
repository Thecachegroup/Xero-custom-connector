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
