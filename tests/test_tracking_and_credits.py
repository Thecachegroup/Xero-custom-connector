"""Regression tests for the three defects fixed on 4 Aug 2026.

1. Tracking read by POSITION put the payroll-tax category in the wrong column,
   so every exempt line silently read as "Payable".
2. Credit notes were never fetched, so revenue was overstated.
3. export_workbook wrote to a relative './output' and died on Vercel.

Run:  python -m pytest tests/test_tracking_and_credits.py -q
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from src import mappers


CATEGORIES = ["Region", "Payroll Tax"]


def _invoice(tracking, *, inv_type="ACCREC", number="INV-001", total=1100.0):
    return {
        "Type": inv_type,
        "InvoiceID": f"id-{number}",
        "InvoiceNumber": number,
        "Reference": "",
        "Date": "2025-09-15T00:00:00",
        "DueDate": "2025-09-29T00:00:00",
        "Total": total,
        "TotalTax": total / 11,
        "AmountPaid": total,
        "AmountDue": 0.0,
        "CurrencyCode": "AUD",
        "Status": "PAID",
        "SentToContact": True,
        "Contact": {"Name": "Linfox", "EmailAddress": "ap@linfox.com"},
        "LineItems": [{
            "ItemCode": "Linfox - SJ",
            "Description": "Consulting",
            "Quantity": 10,
            "UnitAmount": 100.0,
            "LineAmount": 1000.0,
            "AccountCode": "200",
            "TaxType": "OUTPUT",
            "TaxAmount": 100.0,
            "Tracking": tracking,
        }],
    }


# --------------------------------------------------------------- tracking

def test_payroll_tax_lands_in_slot_two_even_when_slot_one_is_absent():
    """THE BUG. The line carries ONLY the payroll-tax category. Positional
    indexing put it at index 0 -> TrackingOption1, where nothing looked for it."""
    inv = _invoice([{"Name": "Payroll Tax", "Option": "Payroll Tax NOT Payable"}])
    df = mappers.invoices_to_rows([inv], CATEGORIES)

    assert df.loc[0, "TrackingName1"] == "Region"
    assert df.loc[0, "TrackingOption1"] == ""
    assert df.loc[0, "TrackingName2"] == "Payroll Tax"
    assert df.loc[0, "TrackingOption2"] == "Payroll Tax NOT Payable"


def test_exempt_line_resolves_to_not_payable():
    inv = _invoice([{"Name": "Payroll Tax", "Option": "Payroll Tax NOT Payable"}])
    df = mappers.invoices_to_rows([inv], CATEGORIES)
    assert mappers.payroll_tax_option(df).iloc[0] == "Not Payable"


def test_unflagged_line_is_blank_and_defaults_to_payable_downstream():
    df = mappers.invoices_to_rows([_invoice([])], CATEGORIES)
    assert mappers.payroll_tax_option(df).iloc[0] == ""


def test_category_order_survives_reordering_in_xero():
    """Same data, categories declared the other way round. The flag must follow
    the NAME, not the slot - that is the whole point of the fix."""
    inv = _invoice([{"Name": "Payroll Tax", "Option": "Payroll Tax NOT Payable"}])
    df = mappers.invoices_to_rows([inv], ["Payroll Tax", "Region"])
    assert df.loc[0, "TrackingName1"] == "Payroll Tax"
    assert df.loc[0, "TrackingOption1"] == "Payroll Tax NOT Payable"
    assert mappers.payroll_tax_option(df).iloc[0] == "Not Payable"


def test_archived_category_is_appended_not_dropped():
    inv = _invoice([{"Name": "Old Project", "Option": "Retired"}])
    df = mappers.invoices_to_rows([inv], CATEGORIES)
    # Region and Payroll Tax hold slots 1 and 2; the unknown one must still be
    # visible somewhere rather than silently vanishing.
    assert "Old Project" not in {df.loc[0, "TrackingName1"], df.loc[0, "TrackingName2"]}
    slots = mappers._tracking_slots(inv["LineItems"][0], CATEGORIES)
    assert ("Old Project", "Retired") in slots


def test_no_category_list_falls_back_to_line_order():
    inv = _invoice([{"Name": "Payroll Tax", "Option": "Payroll Tax NOT Payable"}])
    df = mappers.invoices_to_rows([inv], None)
    assert df.loc[0, "TrackingName1"] == "Payroll Tax"


# ----------------------------------------------------------- credit notes

def _credit_note():
    return {
        "Type": "ACCRECCREDIT",
        "CreditNoteID": "cn-1",
        "CreditNoteNumber": "CN-0001",
        "Date": "2025-11-02T00:00:00",
        "Total": 550.0,
        "TotalTax": 50.0,
        "AppliedAmount": 550.0,
        "RemainingCredit": 0.0,
        "CurrencyCode": "AUD",
        "Status": "PAID",
        "Contact": {"Name": "Linfox"},
        "LineItems": [{
            "ItemCode": "Linfox - SJ",
            "Description": "Credit for overbilled day",
            "Quantity": 5,
            "UnitAmount": 100.0,
            "LineAmount": 500.0,
            "AccountCode": "200",
            "TaxType": "OUTPUT",
            "TaxAmount": 50.0,
            "Tracking": [{"Name": "Payroll Tax", "Option": "Payroll Tax NOT Payable"}],
        }],
    }


def test_credit_note_carries_through_as_negative():
    shaped = mappers.credit_note_to_invoice_shape(_credit_note())
    df = mappers.invoices_to_rows([shaped], CATEGORIES)
    r = df.iloc[0]
    assert r["Type"] == "Sales credit note"
    assert r["InvoiceNumber"] == "CN-0001"
    assert r["LineAmount"] == -500.0
    assert r["Total"] == -550.0
    assert r["TaxAmount"] == -50.0


def test_credit_note_keeps_units_times_rate_equal_to_amount():
    """Quantity is negated and UnitAmount is not, so the rate-card check still
    compares the real rate instead of a negative one."""
    shaped = mappers.credit_note_to_invoice_shape(_credit_note())
    r = mappers.invoices_to_rows([shaped], CATEGORIES).iloc[0]
    assert r["UnitAmount"] == 100.0
    assert r["Quantity"] == -5.0
    assert r["Quantity"] * r["UnitAmount"] == r["LineAmount"]


def test_credit_note_nets_against_its_invoice():
    docs = [_invoice([]), mappers.credit_note_to_invoice_shape(_credit_note())]
    df = mappers.invoices_to_rows(docs, CATEGORIES)
    assert df["LineAmount"].sum() == 500.0          # 1000 invoiced less 500 credited
    assert set(df["Type"]) == {"Sales invoice", "Sales credit note"}


# ------------------------------------------------------------ drop layout

DOC_COLUMNS = [
    "ContactName", "EmailAddress", "POAddressLine1", "POAddressLine2",
    "POAddressLine3", "POAddressLine4", "POCity", "PORegion", "POPostalCode",
    "POCountry", "SAAddressLine1", "SAAddressLine2", "SAAddressLine3",
    "SAAddressLine4", "SACity", "SARegion", "SAPostalCode", "SACountry",
    "InvoiceNumber", "Reference", "InvoiceDate", "DueDate", "PlannedDate",
    "Total", "TaxTotal", "InvoiceAmountPaid", "InvoiceAmountDue",
    "InventoryItemCode", "Description", "Quantity", "UnitAmount", "Discount",
    "LineAmount", "AccountCode", "TaxType", "TaxAmount", "TrackingName1",
    "TrackingOption1", "TrackingName2", "TrackingOption2", "Currency", "Type",
    "Sent", "Status",
]


def test_drop_layout_is_the_44_columns_in_order():
    assert mappers.INVOICE_COLUMNS == DOC_COLUMNS
    assert len(DOC_COLUMNS) == 44
    df = mappers.invoices_to_rows([_invoice([])], CATEGORIES)
    assert list(df.columns)[:44] == DOC_COLUMNS


def test_empty_pull_still_has_the_full_layout():
    df = mappers.invoices_to_rows([], CATEGORIES)
    assert list(df.columns)[:44] == DOC_COLUMNS


# ------------------------------------------------- build_data_frame wiring

def test_build_data_frame_reads_the_exemption_from_the_right_column():
    inv = _invoice([{"Name": "Payroll Tax", "Option": "Payroll Tax NOT Payable"}])
    sales = mappers.invoices_to_rows([inv], CATEGORIES)
    items = pd.DataFrame([{
        "Name": "Sam Jones", "*ItemCode": "Linfox - SJ", "SalesUnitPrice": 100.0,
        "PurchasesUnitPrice": 80.0, "Status": "Active",
    }])
    data = mappers.build_data_frame(
        sales, pd.DataFrame(), pd.DataFrame(), items,
        customer_lookup={}, no_payroll_tax=set(),
        current_fy_start=dt.date(2025, 7, 1),
    )
    assert data.loc[0, "Payroll Tax Payable"] == "Not Payable"


def test_unflagged_line_still_defaults_to_payable():
    sales = mappers.invoices_to_rows([_invoice([])], CATEGORIES)
    items = pd.DataFrame([{
        "Name": "Sam Jones", "*ItemCode": "Linfox - SJ", "SalesUnitPrice": 100.0,
        "PurchasesUnitPrice": 80.0, "Status": "Active",
    }])
    data = mappers.build_data_frame(
        sales, pd.DataFrame(), pd.DataFrame(), items,
        customer_lookup={}, no_payroll_tax=set(),
        current_fy_start=dt.date(2025, 7, 1),
    )
    assert data.loc[0, "Payroll Tax Payable"] == "Payable"
