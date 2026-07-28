"""
Invoice check rules.

Each rule returns a DataFrame of exceptions with a consistent shape:
    contractor | period | severity | rule | detail | amount

Severity: HIGH   = money is wrong right now, fix before the run
          MEDIUM = probably wrong, needs a human to confirm
          LOW    = hygiene / reporting risk

These encode the failure modes already found by hand in the FY26 workbook, so
they get caught automatically from here on instead of on a monthly manual scan.
"""

from __future__ import annotations

import pandas as pd

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"

_COLS = ["contractor", "period", "severity", "rule", "detail", "amount"]


def _ex(**kw) -> dict:
    return {c: kw.get(c) for c in _COLS}


def duplicate_item_codes(items: pd.DataFrame) -> list[dict]:
    """Same item code defined twice in the Xero rate card - the rate card has two
    answers for what one contractor costs. Fix in Xero, not here."""
    dup = items[items["*ItemCode"].duplicated(keep=False)].sort_values("*ItemCode")
    out = []
    for code, g in dup.groupby("*ItemCode"):
        out.append(_ex(contractor=code, period="-", severity=HIGH,
                       rule="duplicate_item_code",
                       detail=f"{len(g)} item records share code {code!r}; "
                              f"sell rates {sorted(set(g['SalesUnitPrice'].dropna()))}, "
                              f"cost rates {sorted(set(g['PurchasesUnitPrice'].dropna()))}",
                       amount=None))
    return out


def rate_vs_rate_card(data: pd.DataFrame, items: pd.DataFrame) -> list[dict]:
    """Invoiced/billed rate does not match the Xero item rate card.
    This is the rule that would have caught the unexplained rate spike."""
    card = (items.drop_duplicates("*ItemCode", keep="last")
                 .set_index("*ItemCode")[["SalesUnitPrice", "PurchasesUnitPrice"]])
    out = []
    for _, r in data[data["Inventory code"].notna()].iterrows():
        ref = card.loc[r["Inventory code"]] if r["Inventory code"] in card.index else None
        if ref is None:
            out.append(_ex(contractor=r["Description"], period=r["Month"], severity=MEDIUM,
                           rule="unknown_item_code",
                           detail=f"Item code {r['Inventory code']!r} not in Xero rate card",
                           amount=r["Amount"]))
            continue
        expected = ref["SalesUnitPrice"] if r["Source"] == "Sales" else ref["PurchasesUnitPrice"]
        if pd.notna(expected) and pd.notna(r["Rate"]) and r["Units"] and abs(float(r["Rate"]) - float(expected)) > 0.005:
            out.append(_ex(contractor=r["Description"], period=r["Month"], severity=HIGH,
                           rule="rate_mismatch",
                           detail=f"{r['Source']} rate {r['Rate']} vs rate card {expected}",
                           amount=r["Amount"]))
    return out


def sales_without_bill(data: pd.DataFrame) -> list[dict]:
    """Contractor invoiced to the client but never billed to us (or vice versa)
    in the same month. Margin leakage in both directions."""
    d = data[data["Source"].isin(["Sales", "Bills", "Payroll"])].copy()
    # Cost side = Bills (ABN) OR Payroll (PAYG). Both are ways of paying a contractor.
    d["Side"] = d["Source"].map({"Sales": "Sales"}).fillna("Cost")
    piv = (d.pivot_table(index=["Inventory code", "Month"], columns="Side",
                         values="Amount", aggfunc="sum")
            .fillna(0))
    out = []
    for (code, month), row in piv.iterrows():
        sales, bills = row.get("Sales", 0), row.get("Cost", 0)
        if sales > 0 and bills == 0:
            out.append(_ex(contractor=code, period=month, severity=HIGH,
                           rule="sales_without_cost",
                           detail="Invoiced to client, no bill and no payroll cost recorded",
                           amount=sales))
        elif bills > 0 and sales == 0:
            out.append(_ex(contractor=code, period=month, severity=HIGH,
                           rule="cost_without_sales",
                           detail="Contractor cost incurred, client never invoiced",
                           amount=bills))
        elif sales > 0 and bills > 0 and (sales - bills) <= 0:
            out.append(_ex(contractor=code, period=month, severity=HIGH,
                           rule="negative_margin",
                           detail=f"Cost {bills:,.2f} >= revenue {sales:,.2f}",
                           amount=sales - bills))
    return out


def zero_unit_rows(data: pd.DataFrame) -> list[dict]:
    """Zero-unit placeholder lines. Harmless individually, poisonous to any
    count-based formula."""
    z = data[(data["Units"].fillna(0) == 0) & (data["Source"] != "Payroll")]
    return [_ex(contractor=r["Description"], period=r["Month"], severity=LOW,
                rule="zero_unit_line",
                detail=f"{r['Source']} line with 0 units at rate {r['Rate']}",
                amount=r["Amount"]) for _, r in z.iterrows()]


def duplicate_same_date_lines(data: pd.DataFrame) -> list[dict]:
    """Same contractor, same date, multiple lines at different rates - either a
    legitimate mid-period rate change or a double-bill. Always needs eyes."""
    grp = (data[data["Source"] != "Payroll"]
           .groupby(["Inventory code", "Date", "Source"])
           .agg(n=("Rate", "size"), rates=("Rate", lambda s: sorted(set(s.dropna()))),
                amount=("Amount", "sum"), month=("Month", "first")))
    out = []
    for (code, dt, src), r in grp[grp["n"] > 1].iterrows():
        sev = HIGH if len(r["rates"]) > 1 else MEDIUM
        out.append(_ex(contractor=code, period=r["month"], severity=sev,
                       rule="same_date_multiple_lines",
                       detail=f"{int(r['n'])} {src} lines on {dt:%d %b %Y} at rates {r['rates']}",
                       amount=r["amount"]))
    return out


def late_billed(data: pd.DataFrame, tolerance_days: int = 45) -> list[dict]:
    """Line dated in one FY but entered well after period end - FY reporting risk."""
    out = []
    d = data[data["Source"] != "Payroll"].copy()
    if "EnteredDate" not in d.columns:
        return out
    lag = (pd.to_datetime(d["EnteredDate"]) - pd.to_datetime(d["Date"])).dt.days
    for _, r in d[lag > tolerance_days].iterrows():
        out.append(_ex(contractor=r["Description"], period=r["Month"], severity=MEDIUM,
                       rule="late_billed", detail="Entered well after invoice date; check FY allocation",
                       amount=r["Amount"]))
    return out


def negative_or_reversal(data: pd.DataFrame) -> list[dict]:
    """Credits / reversals. Confirm they net to zero against their original."""
    n = data[data["Amount"] < 0]
    return [_ex(contractor=r["Description"], period=r["Month"], severity=MEDIUM,
                rule="credit_or_reversal",
                detail=f"Negative {r['Source']} line - confirm it offsets an original",
                amount=r["Amount"]) for _, r in n.iterrows()]


def draft_invoices(data: pd.DataFrame) -> list[dict]:
    """Still in Draft at month end = unbilled revenue."""
    if "Status" not in data.columns:
        return []
    d = data[(data["Source"] == "Sales") & (data["Status"].str.lower() == "draft")]
    return [_ex(contractor=r["Description"], period=r["Month"], severity=HIGH,
                rule="draft_sales_invoice",
                detail="Sales invoice still in Draft - not sent, not revenue",
                amount=r["Amount"]) for _, r in d.iterrows()]


ALL_RULES = [
    draft_invoices,
    sales_without_bill,
    duplicate_same_date_lines,
    zero_unit_rows,
    negative_or_reversal,
]


def run_all(data: pd.DataFrame, items: pd.DataFrame) -> pd.DataFrame:
    exceptions: list[dict] = []
    exceptions += duplicate_item_codes(items)
    exceptions += rate_vs_rate_card(data, items)
    for rule in ALL_RULES:
        exceptions += rule(data)
    if not exceptions:
        return pd.DataFrame(columns=_COLS)
    df = pd.DataFrame(exceptions)
    order = {HIGH: 0, MEDIUM: 1, LOW: 2}
    return (df.assign(_s=df["severity"].map(order))
              .sort_values(["_s", "contractor", "period"])
              .drop(columns="_s")
              .reset_index(drop=True))
