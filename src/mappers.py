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

import os
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


# The tracking category carrying the payroll-tax exemption flag. Matched on
# NAME, case-insensitively, never on position. Override if it is renamed in Xero.
PAYROLL_TAX_CATEGORY = os.environ.get(
    "TCG_PAYROLL_TAX_CATEGORY", "Payroll Tax"
).strip().lower()

# Optional hard pin for TrackingName1 / TrackingName2 column order, as a
# comma-separated list of category names. Only needed if Xero's own category
# order stops matching the order the CSV export writes (e.g. after a category is
# archived). Leave unset to use whatever /TrackingCategories returns.
TRACKING_ORDER_OVERRIDE = [
    c.strip() for c in os.environ.get("TCG_TRACKING_ORDER", "").split(",") if c.strip()
]


def _norm_cat(v) -> str:
    return str(v or "").strip().lower()


def _tracking_slots(line: dict, categories: list[str] | None) -> list[tuple[str, str]]:
    """The (name, option) pair for each tracking slot on one line item.

    Filed by CATEGORY NAME, never by position.

    The bug this replaces: the old version indexed the line's own Tracking[]
    array, which only carries the categories actually set on THAT line. Where a
    line had only the second category populated it landed in the
    TrackingName1 / TrackingOption1 columns - and the payroll-tax lookup, which
    read TrackingOption1, returned blank for every row instead. Low volume, high
    consequence: exempt lines were silently treated as payable.

    The category name is written into its slot whenever the org defines that
    category (matching the Xero CSV export, which names the column's category on
    every row); the option is written only where the line actually carries one.
    """
    tracks = [t for t in (line.get("Tracking") or []) if t.get("Name")]
    by_name = {_norm_cat(t.get("Name")): t for t in tracks}

    order = list(TRACKING_ORDER_OVERRIDE or categories or [])
    if not order:
        # No category list available - fall back to the line's own order. Worse
        # than keying by name, but better than dropping the data entirely.
        order = [t.get("Name", "") for t in tracks]
    # A category present on the line but missing from the org list (archived, or
    # created since the list was fetched) goes on the end rather than vanishing.
    known = {_norm_cat(c) for c in order}
    for t in tracks:
        if _norm_cat(t.get("Name")) not in known:
            order.append(t.get("Name", ""))
            known.add(_norm_cat(t.get("Name")))

    slots = [(cat, (by_name.get(_norm_cat(cat)) or {}).get("Option", "")) for cat in order]
    while len(slots) < 2:
        slots.append(("", ""))
    return slots


def credit_note_to_invoice_shape(note: dict) -> dict:
    """Recast a Xero CreditNote as an Invoice-shaped dict.

    One mapper then handles both document kinds, so the drop tab cannot drift
    between them.

    SIGNS. A credit note reduces revenue, but Xero stores its amounts POSITIVE.
    The workbook sums LineAmount without knowing what produced the row, so the
    signs are flipped here: Quantity and every money field go negative while
    UnitAmount stays positive. That keeps units x rate = amount true, so the
    rate-card check still compares the right rate, and
    checks.negative_or_reversal correctly flags the credit for confirmation
    against its original.
    """
    def neg(v):
        try:
            return -float(v)
        except (TypeError, ValueError):
            return v

    lines = []
    for line in note.get("LineItems", []) or []:
        line = dict(line)
        line["Quantity"] = neg(line.get("Quantity", 0))
        line["LineAmount"] = neg(line.get("LineAmount", 0))
        line["TaxAmount"] = neg(line.get("TaxAmount", 0))
        lines.append(line)

    is_sales = note.get("Type") == "ACCRECCREDIT"
    return {
        "Contact": note.get("Contact", {}) or {},
        "InvoiceNumber": note.get("CreditNoteNumber", ""),
        "Reference": note.get("Reference", ""),
        "Date": note.get("Date") or note.get("DateString"),
        "DueDate": note.get("DueDate") or note.get("DueDateString"),
        "PlannedPaymentDate": None,
        "Total": neg(note.get("Total", 0)),
        "TotalTax": neg(note.get("TotalTax", 0)),
        # A credit note has no AmountPaid/AmountDue. AppliedAmount is the part
        # already offset against invoices; RemainingCredit is what is still
        # available to apply - the closest honest equivalents.
        "AmountPaid": neg(note.get("AppliedAmount", 0)),
        "AmountDue": neg(note.get("RemainingCredit", 0)),
        "CurrencyCode": note.get("CurrencyCode", "AUD"),
        "SentToContact": note.get("SentToContact"),
        "Status": note.get("Status", ""),
        "InvoiceID": note.get("CreditNoteID", ""),
        "LineItems": lines,
        "Type": note.get("Type", ""),
        "_TypeLabel": "Sales credit note" if is_sales else "Bill credit note",
    }


def invoices_to_rows(
    invoices: list[dict], tracking_categories: list[str] | None = None
) -> pd.DataFrame:
    """One row per line item, matching the Xero CSV export exactly.

    Accepts invoices, credit notes recast by credit_note_to_invoice_shape(), or
    a mix of both.

    tracking_categories: the org's category names in order, from
    XeroClient.tracking_categories(). Pass it wherever tracking matters -
    without it tracking falls back to per-line order, which is what put the
    payroll-tax flag in the wrong column.
    """
    rows = []
    for inv in invoices:
        contact = inv.get("Contact", {}) or {}
        po, sa = _addr(contact, "POBOX"), _addr(contact, "STREET")
        is_sales = inv.get("Type") in ("ACCREC", "ACCRECCREDIT")

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
            # Credit notes carry their own label so the drop distinguishes them
            # exactly as the Xero export does.
            "Type": inv.get("_TypeLabel") or ("Sales invoice" if is_sales else "Bill"),
            "Sent": "Sent" if inv.get("SentToContact") else ("Unsent" if is_sales else ""),
            "Status": (inv.get("Status", "") or "").title().replace("Awaitingpayment", "Awaiting Payment"),
            "InvoiceID": inv.get("InvoiceID", ""),  # kept for traceability, dropped on export
        }

        for line in inv.get("LineItems", []) or []:
            slots = _tracking_slots(line, tracking_categories)
            (t1n, t1o), (t2n, t2o) = slots[0], slots[1]
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
            # These were crossed. Xero puts InventoryAssetAccountCode at the top
            # level of the item and COGSAccountCode inside PurchaseDetails, so
            # each column was reporting the other one's account.
            "InventoryAssetAccount": it.get("InventoryAssetAccountCode", ""),
            "CostOfGoodsSoldAccount": pd_.get("COGSAccountCode", ""),
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


def payrun_summaries_to_rows(pay_runs: list[dict]) -> pd.DataFrame:
    """Payroll rows built from the pay run's own payslip summaries.

    GET /PayRuns already returns, per employee: Wages, Deductions, Tax, Super,
    Reimbursements and NetPay. That is the same information the Payroll Activity
    Details report shows in the Xero UI - and the report itself is not exposed
    through the API, so this is the closest equivalent.

    Why this is the default path: fetching each payslip individually costs one
    API call per person per pay run (roughly 400 for a financial year) against
    Xero's 60-per-minute limit. Reading the summaries costs one call per pay run
    (roughly 36). Same figures, an order of magnitude fewer calls, and no
    serverless timeout.

    Employer cost = Wages + Super. Tax and NetPay are carved OUT of wages, not
    added to them - summing them would roughly double the apparent cost. Here
    they are named fields rather than rows to be classified, so the trap the
    workbook handles with its "Ignore" flag cannot be sprung.

    Dates use the PAYMENT date (the transaction date), not the period end.
    """
    rows = []
    for run in pay_runs:
        pay_date = parse_xero_date(
            run.get("PaymentDate") or run.get("PayRunPeriodEndDate")
        )
        period_end = parse_xero_date(run.get("PayRunPeriodEndDate"))
        for ps in run.get("Payslips", []) or []:
            employee = f"{ps.get('FirstName','')} {ps.get('LastName','')}".strip()
            for label, amount, cost_class in (
                ("Wages", ps.get("Wages"), "wages"),
                ("Superannuation", ps.get("Super"), "super"),
            ):
                if not amount:
                    continue
                rows.append({
                    "Employee": employee,
                    "Pay Item": label,
                    "Date": pay_date,
                    "Period End": period_end,
                    "Rate Per Unit": 0,
                    "Units": 0,
                    "Amount": float(amount),
                    "Cost Class": cost_class,
                })
            # Withheld tax is NOT employer cost - carried separately so PAYG
            # withholding can be reported without polluting the cost side.
            if ps.get("Tax"):
                rows.append({
                    "Employee": employee, "Pay Item": "PAYG Withholding",
                    "Date": pay_date, "Period End": period_end,
                    "Rate Per Unit": 0, "Units": 0,
                    "Amount": float(ps["Tax"]), "Cost Class": "paygw",
                })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Unified Data frame - the thing the whole workbook actually runs on
# --------------------------------------------------------------------------

_Z_PREFIX = re.compile(r"^z+", re.IGNORECASE)


def normalise_code(code) -> str:
    """Matching key for an item code.

    TCG historically renamed retired contractors with a leading z (or zz) to
    push them to the bottom of Xero's alphabetical item list. The consequence is
    that one person's sales sit under 'zLinfox - SJ' while their payroll sits
    under 'Linfox - SJ', so any match on the raw code splits them into two
    people - one apparently never paid, the other apparently never invoiced.

    Strip any leading z's and casefold. Note the z may be followed by a digit
    ('z4PL - JD'), so the pattern must not require a letter after it.

    The displayed code is left untouched; this is only the join key. The durable
    fix is Xero's archive function, which retires an item without renaming it.
    """
    c = str(code or "").strip()
    return _Z_PREFIX.sub("", c).strip().lower()


def _name_key(v) -> str:
    """Normalised key for matching a person's name.

    Collapses internal whitespace as well as stripping punctuation - dictated
    and copy-pasted names arrive with double spaces, and 'louis  soto' must
    match 'Louis Soto' or the lookup silently misses.
    """
    # Hyphens and punctuation become spaces (not nothing), so 'Louis-Soto' and
    # "O'Brien" normalise the same way a human would read them.
    return re.sub(r"\s+", " ", re.sub(r"[^a-z]+", " ", str(v or "").lower())).strip()


def load_employee_overrides(path: str | None = None) -> dict:
    """Explicit employee-name -> item-code mappings from config.

    An automatic matcher can only work when the Xero item Name resembles the
    payroll employee name. Where it doesn't - 'Louis Soto' against an item coded
    'Linfox - LSOTO' whose Name is blank or abbreviated - no amount of cleverness
    will find it, and guessing would put one contractor's cost against another's
    revenue. An explicit table is the honest answer, and it stays correct.
    """
    import json, os
    path = path or os.environ.get("TCG_EMPLOYEE_CODES", "config/employee_codes.json")
    try:
        with open(path) as fh:
            raw = (json.load(fh) or {}).get("map", {})
    except Exception:
        return {}
    return {_name_key(k): v for k, v in raw.items() if k and v}


def build_employee_code_map(items: pd.DataFrame) -> dict:
    """Map a payroll employee name to their inventory item code.

    PAYG contractors carry their cost in payroll, not in a bill, so without this
    every PAYG person reads as invoiced-but-never-costed AND as cost with no
    contractor attached. Exact matching on the item Name is too brittle: names
    are entered as "Dat Le", "Le, Dat", "Dat Le - Test Analyst" and so on.

    Resolution order, stopping at the first hit:
      1. exact normalised name
      2. reversed "Surname, First" form
      3. the item name starts with the employee name (trailing role titles)
      4. every word of the employee name appears in the item name
    Ambiguous matches are dropped rather than guessed - a wrong mapping puts one
    contractor's cost against another's revenue, which is worse than no match.
    """
    overrides = load_employee_overrides()
    named = items.dropna(subset=["Name"]).copy()
    named["_k"] = named["Name"].map(_name_key)
    named = named[named["_k"] != ""]
    exact = dict(zip(named["_k"], named["*ItemCode"]))

    def resolve(employee: str):
        k = _name_key(employee)
        if not k:
            return None
        if k in overrides:          # explicit table always wins
            return overrides[k]
        if k in exact:
            return exact[k]
        parts = k.split()
        if len(parts) >= 2:
            rev = " ".join(reversed(parts))
            if rev in exact:
                return exact[rev]
        starts = [c for kk, c in exact.items() if kk.startswith(k + " ")]
        if len(set(starts)) == 1:
            return starts[0]
        words = set(parts)
        contains = [c for kk, c in exact.items() if words and words <= set(kk.split())]
        if len(set(contains)) == 1:
            return contains[0]
        # Last resort: ignore spacing entirely, so "Patrick OBrien" still finds
        # "Patrick O'Brien". Only accepted when exactly one item matches.
        flat = k.replace(" ", "")
        squashed = [c for kk, c in exact.items() if kk.replace(" ", "") == flat]
        if len(set(squashed)) == 1:
            return squashed[0]
        return None

    return {"_resolve": resolve}


def exempt_item_codes(items: pd.DataFrame,
                      names) -> tuple[set[str], list[str]]:
    """Turn a list of payroll-tax-exempt NAMES into the set of item codes they
    own, and the names no item could be found for.

    Why this exists: config/no_payroll_tax.json is a list of people, which is
    how Andrew thinks about the exemption and how he will keep maintaining it.
    The comparison, though, has to happen on the item code - a line description
    carrying a role title or a PO number is not a name and never matched.
    Resolving names to codes ONCE, here, against the Xero item list keeps the
    config human and the matching stable.

    MATCHING IS STRICT EQUALITY on the normalised name, plus the reversed
    "Surname First" form, and nothing else. No prefix match, no
    every-word-appears match - build_employee_code_map uses those to find a
    contractor's cost, where a near miss costs a join. Here a near miss grants
    somebody else's exemption and takes money out of the payroll tax base, so
    the bar is higher and a name that does not match exactly is reported rather
    than guessed at.

    A name matching SEVERAL items returns all of them. That is not ambiguity to
    be dropped: 'Mazher Ali' owns 'Linfox - MAZ', 'zLinfox - MA' and
    'zLinfox - MAZ', and every one of them is his. Codes come back normalised
    (leading z stripped, case-folded), which is the join key the data frame uses.

    Returns (codes, unresolved_names).
    """
    named = items.dropna(subset=["Name"]).copy() if not items.empty else items
    lookup: dict[str, set[str]] = {}
    if not items.empty:
        for key, code in zip(named["Name"].map(_name_key), named["*ItemCode"]):
            if key:
                lookup.setdefault(key, set()).add(normalise_code(code))

    codes: set[str] = set()
    unresolved: list[str] = []
    for raw in names or []:
        k = _name_key(raw)
        if not k:
            continue
        hit = lookup.get(k)
        if not hit:
            parts = k.split()
            if len(parts) >= 2:
                hit = lookup.get(" ".join(reversed(parts)))
        if hit:
            codes |= hit
        else:
            unresolved.append(str(raw))
    codes.discard("")
    return codes, unresolved


def suggest_codes_for(employee: str, items: pd.DataFrame, limit: int = 3) -> list:
    """Candidate item codes for an unmatched employee, scored on initials and
    surname appearing in the code (TCG codes them 'Linfox - LSOTO' for Louis
    Soto). These are SUGGESTIONS for the lookup table only - never applied
    automatically, because a plausible-looking wrong match is worse than none.
    """
    parts = [p for p in _name_key(employee).split() if p]
    if not parts:
        return []
    first, last = parts[0], parts[-1]
    initials = "".join(p[0] for p in parts)
    scored = []
    for _, it in items.iterrows():
        code = str(it.get("*ItemCode") or "")
        tail = normalise_code(code).split("-")[-1].strip().replace(" ", "")
        if not tail:
            continue
        score = 0
        if tail == initials:                      score += 5
        if tail == (first[0] + last):             score += 6
        if last and last in tail:                 score += 4
        if tail and tail in _name_key(it.get("Name") or ""): score += 1
        if score:
            scored.append((score, code, str(it.get("Name") or "")))
    scored.sort(reverse=True)
    return [{"code": c, "name": n, "score": s} for s, c, n in scored[:limit]]


def payroll_tax_option(df: pd.DataFrame) -> pd.Series:
    """The payroll-tax tracking option for each row, from whichever slot it is in.

    THIS REPLACES A LIVE DEFECT. build_data_frame used to read TrackingOption1
    unconditionally. In this org the payroll-tax category sits in slot 2, so the
    lookup returned blank on every row and every line fell through to "Payable"
    - including the ones explicitly marked "Payroll Tax NOT Payable" in Xero.
    Low volume, high consequence: payroll tax liability overstated, silently.

    Matching on the category NAME means it keeps working if Xero reorders the
    categories, if one is archived, or if a third is added.
    """
    out = pd.Series([""] * len(df), index=df.index, dtype=object)
    for name_col, opt_col in (("TrackingName1", "TrackingOption1"),
                              ("TrackingName2", "TrackingOption2")):
        if name_col not in df.columns or opt_col not in df.columns:
            continue
        is_cat = df[name_col].astype(str).str.strip().str.lower() == PAYROLL_TAX_CATEGORY
        has_opt = df[opt_col].astype(str).str.strip() != ""
        out = out.mask(is_cat & has_opt, df[opt_col])
    # Xero's option reads "Payroll Tax NOT Payable"; the workbook's own
    # convention - and the config-driven branch in build_data_frame - is
    # "Not Payable" / "Payable". Normalise here so the two paths cannot produce
    # two different spellings of the same fact. The raw option is untouched in
    # the drop tab's TrackingOption columns.
    return out.map(
        lambda v: "Not Payable" if "not payable" in str(v).strip().lower()
        else ("Payable" if str(v).strip() else "")
    )


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
    no_payroll_tax_codes: set[str] | frozenset[str] = frozenset(),
) -> pd.DataFrame:
    """Union Sales + Bills + Payroll into the normalised `Data` schema.

    no_payroll_tax       - exempt people by NAME, matched against Description.
                           Kept for the names no item code can be found for.
    no_payroll_tax_codes - exempt people by ITEM CODE. See the payroll-tax
                           branch below for why this had to be added.
    """
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
            # Name-keyed, not positional. See payroll_tax_option().
            "Payroll Tax Payable": payroll_tax_option(df),
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
        resolver = build_employee_code_map(items)["_resolve"]
        # PAYG withholding is money carved OUT of gross wages and remitted to
        # the ATO - it is not additional employer cost. It rides along in the
        # frame so the withholding total can be reported, but it is marked
        # "Ignore" (the workbook's own convention) so nothing sums it as cost.
        payroll = payroll.copy()
        payroll["Cost Class"] = payroll.get("Cost Class", "wages")
        payroll.loc[payroll["Cost Class"] == "paygw", "Cost Class"] = "Ignore"

        pay = pd.DataFrame({
            "Primary Source": "Payroll",
            "Source": "Payroll",
            "Invoiced/Billed": "Paid",
            "Description": payroll["Employee"],
            "Inventory code": payroll["Employee"].map(resolver),
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
    data["Match key"] = data["Inventory code"].map(normalise_code)

    # Payroll tax exemptions are keyed on ITEM CODE first, name second.
    #
    # The list used to be compared to Description and nothing else. On a payroll
    # row Description IS the employee's name, so it worked there and looked
    # fine. On a sales or bill line Description is whatever the invoice line
    # says - "Rajeev Jindal - Solution Architect", "P/O 4500123456", a date
    # range - and an exact string comparison misses every one of them. Those
    # lines fell through to "Payable": $95,876.29 wrongly in the Victorian base
    # in July FY27 alone.
    #
    # The item code is the one stable key, and normalise_code folds the retired
    # 'zLinfox - X' back onto 'Linfox - X' so an exemption follows a person into
    # the archive instead of lapsing the day they are renamed.
    #
    # The name path is kept, not replaced: it is what still covers anyone with
    # no inventory item, and removing it would trade one silent miss for
    # another. server._payroll_tax_exemptions() reports every name it could not
    # resolve to a code, so the gap is visible rather than assumed empty.
    exempt_codes = {normalise_code(c) for c in (no_payroll_tax_codes or ())}
    exempt_codes.discard("")

    def _payroll_tax(r):
        if r["Match key"] in exempt_codes:
            return "Not Payable"
        if str(r["Description"]).strip() in no_payroll_tax:
            return "Not Payable"
        return r.get("Payroll Tax Payable") or "Payable"

    data["Payroll Tax Payable"] = data.apply(_payroll_tax, axis=1)
    data["Place"] = range(1, len(data) + 1)
    return data.reset_index(drop=True)
