"""
Map Xero API objects onto the exact column schemas used in Invoice_Checker.xlsx.

Three targets, all reproduced 1:1 so the workbook keeps working unchanged:
  - "Sales Invoices drop"  (ACCREC, one row per line item)
  - "Bills Drop"           (ACCPAY, one row per line item)
  - "Inventory Drop"       (Items)
  - "Pay Details drop"     (Payroll AU payslip earnings lines)

Plus `build_data_frame()`, which produces the normalised `Data` sheet directly -
the union of all three with lookups applied. That is the sheet everything
downstream actually depends on.
"""

from __future__ import annotations

import re
from datetime import datetime, date

import pandas as pd

INVOICE_COLUMNS = [
    "ContactName", "EmailAddress",
    "POAddressLine1", "POAddressLine2", "POAddressLine3", "POAddressLine4",
    "POCity", "PORegion", "POPostalCode", "POCountry",
    "SAAddressLine1", "SAAddressLine2", "SAAddressLine3", "SAAddressLine4",
    "SACity", "SARegion", "SAPostalCode", "SACountry",
    "InvoiceNumber", "Reference", "InvoiceDate", "DueDate", "PlannedDate",
    "Total", "TaxTotal", "InvoiceAmountPaid", "InvoiceAmountDue",
    "InventoryItemCode", "Description", "Quantity", "UnitAmount", "Discount",
    "LineAmount", "AccountCode", "TaxType", "TaxAmount",
    "TrackingName1", "TrackingOption1", "TrackingName2", "TrackingOption2",
    "Currency", "Type", "Sent", "Status",
]

DATA_COLUMNS = [
    "Place", "Primary Source", "FY", "Source", "Invoiced/Billed", "Description",
    "Inventory code", "Description (Sales only)", "Vendor", "Customer", "Type",
    "Contractor type", "Date", "Month", "Units", "Sold Rate", "Rate", "Amount",
    "Contractor Active in current financial year", "Payroll Tax Payable",
    "Contractor Active", "Wages type with Super", "Wage Type",
]

_XERO_DATE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def parse_xero_date(value) -> date | None:
    """Xero returns either '/Date(1488338552390+0000)/' or ISO 'YYYY-MM-DDT00:00:00'."""
    if value in (None, ""):
        return None
    if isinstance(value, (datetime, date)):
        return value.date() if isinstance(value, datetime) else value
    m = _XERO_DATE.match(str(value))
    if m:
        return datetime.utcfromtimestamp(int(m.group(1)) / 1000).date()
    return datetime.fromisoformat(str(value).replace("Z", "")).date()


def _addr(contact: dict, addr_type: str) -> dict:
    for a in contact.get("Addresses", []) or []:
        if a.get("AddressType") == addr_type:
            return a
    return {}


def _tracking(line: dict, idx: int) -> tuple[str, str]:
    tracks = line.get("Tracking", []) or []
    if idx < len(tracks):
        return tracks[idx].get("Name", ""), tracks[idx].get("Option", "")
    return "", ""


def invoices_to_rows(invoices: list[dict]) -> pd.DataFrame:
    """One row per line item, matching the Xero CSV export exactly."""
    rows = []
    for inv in invoices:
        contact = inv.get("Contact", {}) or {}
        po, sa = _addr(contact, "POBOX"), _addr(contact, "STREET")
        is_sales = inv.get("Type") == "ACCREC"

        head = {
            "ContactName": contact.get("Name", ""),
            "EmailAddress": contact.get("EmailAddress", ""),
            "POAddressLine1": po.get("AddressLine1", ""),
            "POAddressLine2": po.get("AddressLine2", ""),
            "POAddressLine3": po.get("AddressLine3", ""),
            "POAddressLine4": po.get("AddressLine4", ""),
            "POCity": po.get("City", ""),
            "PORegion": po.get("Region", ""),
            "POPostalCode": po.get("PostalCode", ""),
            "POCountry": po.get("Country", ""),
            "SAAddressLine1": sa.get("AddressLine1", ""),
            "SAAddressLine2": sa.get("AddressLine2", ""),
            "SAAddressLine3": sa.get("AddressLine3", ""),
            "SAAddressLine4": sa.get("AddressLine4", ""),
            "SACity": sa.get("City", ""),
            "SARegion": sa.get("Region", ""),
            "SAPostalCode": sa.get("PostalCode", ""),
            "SACountry": sa.get("Country", ""),
            "InvoiceNumber": inv.get("InvoiceNumber", ""),
            "Reference": inv.get("Reference", ""),
            "InvoiceDate": parse_xero_date(inv.get("Date") or inv.get("DateString")),
            "DueDate": parse_xero_date(inv.get("DueDate") or inv.get("DueDateString")),
            "PlannedDate": parse_xero_date(inv.get("PlannedPaymentDate")),
            "Total": inv.get("Total", 0),
            "TaxTotal": inv.get("TotalTax", 0),
            "InvoiceAmountPaid": inv.get("AmountPaid", 0),
            "InvoiceAmountDue": inv.get("AmountDue", 0),
            "Currency": inv.get("CurrencyCode", "AUD"),
            "Type": "Sales invoice" if is_sales else "Bill",
            "Sent": "Sent" if inv.get("SentToContact") else ("Unsent" if is_sales else ""),
            "Status": (inv.get("Status", "") or "").title().replace("Awaitingpayment", "Awaiting Payment"),
            "InvoiceID": inv.get("InvoiceID", ""),  # kept for traceability, dropped on export
        }

        for line in inv.get("LineItems", []) or []:
            t1n, t1o = _tracking(line, 0)
            t2n, t2o = _tracking(line, 1)
            rows.append({
                **head,
                "InventoryItemCode": line.get("ItemCode", ""),
                "Description": line.get("Description", ""),
                "Quantity": line.get("Quantity", 0),
                "UnitAmount": line.get("UnitAmount", 0),
                "Discount": line.get("DiscountRate", ""),
                "LineAmount": line.get("LineAmount", 0),
                "AccountCode": line.get("AccountCode", ""),
                "TaxType": line.get("TaxType", ""),
                "TaxAmount": line.get("TaxAmount", 0),
                "TrackingName1": t1n, "TrackingOption1": t1o,
                "TrackingName2": t2n, "TrackingOption2": t2o,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=INVOICE_COLUMNS + ["InvoiceID"])
    return df[INVOICE_COLUMNS + ["InvoiceID"]]


def items_to_rows(items: list[dict]) -> pd.DataFrame:
    """Matches 'Inventory Drop'. This is the rate card - the source of truth for
    what each contractor should cost (PurchasesUnitPrice) and sell for
    (SalesUnitPrice)."""
    rows = []
    for it in items:
        pd_, sd = it.get("PurchaseDetails", {}) or {}, it.get("SalesDetails", {}) or {}
        rows.append({
            "Name": it.get("Name", ""),
            "*ItemCode": it.get("Code", ""),
            "ItemName": it.get("Name", ""),
            "Quantity": it.get("QuantityOnHand", 0),
            "PurchasesDescription": it.get("PurchaseDescription", ""),
            "PurchasesUnitPrice": pd_.get("UnitPrice"),
            "PurchasesAccount": pd_.get("AccountCode", ""),
            "PurchasesTaxRate": pd_.get("TaxType", ""),
            "SalesDescription": it.get("Description", ""),
            "SalesUnitPrice": sd.get("UnitPrice"),
            "SalesAccount": sd.get("AccountCode", ""),
            "SalesTaxRate": sd.get("TaxType", ""),
            "InventoryAssetAccount": pd_.get("COGSAccountCode", ""),
            "CostOfGoodsSoldAccount": it.get("InventoryAssetAccountCode", ""),
            "Status": "Active" if it.get("IsSold") or it.get("IsPurchased") else "Inactive",
            "InventoryType": "Tracked" if it.get("IsTrackedAsInventory") else "Untracked",
        })
    return pd.DataFrame(rows)


# Pay items that are components of gross pay, NOT additional employer cost.
# Summing these double-counts the contractor. The Xero payslip API does not
# return them as lines (they are payslip-level totals), but the CSV export does,
# so we guard both paths.
NON_COST_PAY_ITEMS = {"payg", "net pay", "payment", "paygw", "tax", "withholding"}


def is_cost_pay_item(name: str) -> bool:
    return str(name).strip().lower() not in NON_COST_PAY_ITEMS


def payslips_to_rows(payslips: list[dict]) -> pd.DataFrame:
    """Matches 'Pay Details drop (formatted)': Employee | Pay Item | Date |
    Rate Per Unit | Units | Amount."""
    rows = []
    for ps in payslips:
        employee = f"{ps.get('FirstName','')} {ps.get('LastName','')}".strip()
        pay_date = parse_xero_date(ps.get("PaymentDate") or ps.get("PayRunPeriodEndDate"))
        # Employer cost = gross earnings + super. Deductions and tax are carved
        # OUT of gross, not added to it, so they are not fetched here.
        for group, rate_key, unit_key, cost_class in (
            ("EarningsLines", "RatePerUnit", "NumberOfUnits", "wages"),
            ("LeaveEarningsLines", "RatePerUnit", "NumberOfUnits", "wages"),
            ("SuperannuationLines", None, None, "super"),
        ):
            for line in ps.get(group, []) or []:
                rows.append({
                    "Employee": employee,
                    "Pay Item": line.get("EarningsRateName")
                              or line.get("DeductionTypeName")
                              or line.get("SuperannuationTypeName", ""),
                    "Date": pay_date,
                    "Rate Per Unit": line.get(rate_key) if rate_key else 0,
                    "Units": line.get(unit_key) if unit_key else 0,
                    "Amount": line.get("Amount", 0),
                    "Cost Class": cost_class,
                })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df[df["Pay Item"].map(is_cost_pay_item)]


# --------------------------------------------------------------------------
# Unified Data frame - the thing the whole workbook actually runs on
# --------------------------------------------------------------------------

def fy_label(d: date, current_fy_start: date) -> str:
    """AU financial year: 1 Jul - 30 Jun."""
    if d is None or pd.isna(d):
        return ""
    fy_start = date(d.year if d.month >= 7 else d.year - 1, 7, 1)
    return "Current FY" if fy_start == current_fy_start else f"FY{str(fy_start.year + 1)[2:]}"


def build_data_frame(
    sales: pd.DataFrame,
    bills: pd.DataFrame,
    payroll: pd.DataFrame,
    items: pd.DataFrame,
    customer_lookup: dict[str, str],
    no_payroll_tax: set[str],
    current_fy_start: date,
) -> pd.DataFrame:
    """Union Sales + Bills + Payroll into the normalised `Data` schema."""
    frames = []

    # Prefer Active rows, then last-written, when an item code appears twice.
    # Duplicates are NOT swallowed - checks.duplicate_item_codes() reports them.
    rc = items.copy()
    rc["_active"] = (rc.get("Status", "") == "Active").astype(int)
    rc = (rc.sort_values("_active")
            .drop_duplicates("*ItemCode", keep="last")
            .set_index("*ItemCode"))
    rate_card = rc[["SalesUnitPrice", "PurchasesUnitPrice", "Name"]].to_dict("index")

    for df, source, invoiced in ((sales, "Sales", "Invoiced"), (bills, "Bills", "Billed")):
        if df.empty:
            continue
        out = pd.DataFrame({
            "Primary Source": source,
            "Source": source,
            "Invoiced/Billed": invoiced,
            "Description": df["Description"],
            "Inventory code": df["InventoryItemCode"],
            "Description (Sales only)": df["Description"] if source == "Sales" else None,
            "Vendor": df["ContactName"] if source == "Bills" else None,
            "Customer": df["ContactName"].map(customer_lookup).fillna(df["ContactName"]),
            "Date": pd.to_datetime(df["InvoiceDate"]),
            "Units": pd.to_numeric(df["Quantity"], errors="coerce"),
            "Rate": pd.to_numeric(df["UnitAmount"], errors="coerce"),
            "Amount": pd.to_numeric(df["LineAmount"], errors="coerce"),
            "Payroll Tax Payable": df["TrackingOption1"],
            "InvoiceNumber": df["InvoiceNumber"],
            "Status": df["Status"],
        })
        out["Sold Rate"] = out["Inventory code"].map(
            lambda c: (rate_card.get(c) or {}).get("SalesUnitPrice")
        )
        frames.append(out)

    if not payroll.empty:
        payroll = payroll[payroll["Pay Item"].map(is_cost_pay_item)].copy()

    if not payroll.empty:
        # PAYG contractors carry their cost in payroll, not in a bill. Map the
        # employee name back to their item code so payroll joins to sales the
        # same way bills do - otherwise every PAYG contractor reads as
        # "invoiced but never costed".
        name_to_code = (items.dropna(subset=["Name"])
                             .drop_duplicates("Name", keep="last")
                             .set_index(items.dropna(subset=["Name"])
                                        .drop_duplicates("Name", keep="last")["Name"]
                                        .str.strip().str.lower())["*ItemCode"]
                             .to_dict())
        pay = pd.DataFrame({
            "Primary Source": "Payroll",
            "Source": "Payroll",
            "Invoiced/Billed": "Paid",
            "Description": payroll["Employee"],
            "Inventory code": payroll["Employee"].str.strip().str.lower().map(name_to_code),
            "Vendor": payroll["Employee"],
            "Customer": None,
            "Date": pd.to_datetime(payroll["Date"]),
            "Units": pd.to_numeric(payroll["Units"], errors="coerce"),
            "Rate": pd.to_numeric(payroll["Rate Per Unit"], errors="coerce"),
            "Amount": pd.to_numeric(payroll["Amount"], errors="coerce"),
            "Wage Type": payroll["Pay Item"],
            "Wages type with Super": payroll.get("Cost Class", "wages"),
        })
        frames.append(pay)

    data = pd.concat(frames, ignore_index=True)
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    # Drop phantom rows FIRST - blank/dateless lines never enter the frame.
    data = data[data["Date"].notna() & data["Amount"].notna()].copy()
    data["FY"] = data["Date"].dt.date.map(lambda d: fy_label(d, current_fy_start))
    data["Month"] = data["Date"].dt.strftime("%b-%y")
    data["Contractor type"] = data["Source"].map({"Bills": "ABN", "Payroll": "PAYG"}).fillna("ABN")
    data["Type"] = "Contractor"
    data["Payroll Tax Payable"] = data.apply(
        lambda r: "Not Payable" if str(r["Description"]).strip() in no_payroll_tax
        else (r.get("Payroll Tax Payable") or "Payable"),
        axis=1,
    )
    data["Place"] = range(1, len(data) + 1)
    return data.reset_index(drop=True)
