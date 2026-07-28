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
    from .mappers import normalise_code
    # Index the rate card on the NORMALISED code so a bill under 'Linfox - SJ'
    # still finds a card entry filed as 'zLinfox - SJ' (the retired-code rename).
    card = items.copy()
    card["_key"] = card["*ItemCode"].map(normalise_code)
    card = (card[card["_key"] != ""]
            .drop_duplicates("_key", keep="last")
            .set_index("_key")[["SalesUnitPrice", "PurchasesUnitPrice"]])
    out = []
    mismatches: list[dict] = []
    seen_unknown: set[str] = set()
    for _, r in data[data["Inventory code"].notna()].iterrows():
        key = normalise_code(r["Inventory code"])
        # Blank codes are reported once per customer by uncoded_sales - don't
        # also emit one row per line here, or the real exceptions drown.
        if key == "":
            continue
        ref = card.loc[key] if key in card.index else None
        if ref is None:
            # One row per unknown code, not one per line.
            if key in seen_unknown:
                continue
            seen_unknown.add(key)
            out.append(_ex(contractor=r["Description"], period=r["Month"], severity=MEDIUM,
                           rule="unknown_item_code",
                           detail=f"Item code {r['Inventory code']!r} not in Xero rate card",
                           amount=r["Amount"]))
            continue
        expected = ref["SalesUnitPrice"] if r["Source"] == "Sales" else ref["PurchasesUnitPrice"]
        if pd.notna(expected) and pd.notna(r["Rate"]) and r["Units"] and abs(float(r["Rate"]) - float(expected)) > 0.005:
            mismatches.append({"key": key, "contractor": r["Description"],
                               "source": r["Source"], "rate": float(r["Rate"]),
                               "expected": float(expected), "month": r["Month"],
                               "amount": float(r["Amount"] or 0)})

    # One row per distinct (code, source, rate, card rate). The rate card is
    # point-in-time: a rate that rose in March flags every month it ever applied,
    # which previously produced ~218 rows saying about a dozen things. Collapsing
    # them keeps the signal and shows how long the gap has been running.
    if mismatches:
        m = pd.DataFrame(mismatches)
        for (key, src, rate, exp), g in m.groupby(["key", "source", "rate", "expected"]):
            months = sorted(set(g["month"].dropna()))
            direction = "under" if rate < exp else "over"
            out.append(_ex(
                contractor=str(g["contractor"].iloc[0]), period=f"{len(months)} months",
                severity=HIGH, rule="rate_mismatch",
                detail=(f"{src} rate {rate:,.2f} vs rate card {exp:,.2f} "
                        f"({direction} by {abs(rate-exp):,.2f}) across {len(g)} lines"),
                amount=round(float(g["amount"].sum()), 2)))
    return out


def _load_cfg(path: str, key: str, default):
    import json, os
    try:
        with open(os.environ.get(f"TCG_{key.upper()}", path)) as fh:
            return json.load(fh)
    except Exception:
        return default


def no_sales_expected() -> set:
    cfg = _load_cfg("config/internal_codes.json", "internal_codes", {})
    return {str(c).strip().lower() for c in cfg.get("no_sales_expected", [])}


def payrolling_cost_codes() -> set:
    """Cost-side item codes belonging to payrolling customers. Their revenue sits
    on uncoded invoice lines, so a code-level match will always show cost with no
    sales. Netting happens at customer level via uncoded_sales instead."""
    cfg = _load_cfg("config/payrolling_customers.json", "payrolling_customers", {})
    out = set()
    for codes in (cfg.get("customers") or {}).values():
        out |= {str(c).strip().lower() for c in codes}
    return out


def sales_without_bill(data: pd.DataFrame) -> list[dict]:
    """Contractor invoiced to the client but never billed to us (or vice versa)
    in the same month. Margin leakage in both directions."""
    d = data[data["Source"].isin(["Sales", "Bills", "Payroll"])].copy()
    key = "Match key" if "Match key" in d.columns else "Inventory code"
    # Lines with no item code cannot be matched to a contractor at all. Xenon-style
    # payrolling invoices are billed as cost components (base salary, super,
    # payroll tax, workcover) and legitimately carry no code. Reporting each one
    # individually buried the real exceptions under hundreds of rows, so they are
    # summarised per customer instead.
    uncoded = d[(d["Source"] == "Sales") & (d[key].astype(str).str.strip() == "")]
    d = d[d[key].astype(str).str.strip() != ""]
    # Cost side = Bills (ABN) OR Payroll (PAYG). Both are ways of paying a contractor.
    d["Side"] = d["Source"].map({"Sales": "Sales"}).fillna("Cost")
    piv = (d.pivot_table(index=[key, "Month"], columns="Side",
                         values="Amount", aggfunc="sum")
            .fillna(0))
    exempt = no_sales_expected() | payrolling_cost_codes()
    out = []
    for (code, month), row in piv.iterrows():
        if str(code).strip().lower() in exempt:
            # Directors' drawings, internal staff and payrolling cost codes are
            # cost-only by design. Flagging them every month buried the real
            # one-sided contractors under ~50 rows of noise.
            continue
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


def uncoded_sales_summary(data: pd.DataFrame) -> list[dict]:
    """One row per customer for sales lines carrying no item code, rather than
    one row per line. These are usually payrolling arrangements billed as cost
    components; they need matching at customer level, not code level."""
    key = "Match key" if "Match key" in data.columns else "Inventory code"
    s = data[(data["Source"] == "Sales") &
             (data[key].astype(str).str.strip() == "")]
    if s.empty:
        return []
    out = []
    for cust, g in s.groupby(s["Customer"].fillna("(no customer)")):
        out.append(_ex(contractor=str(cust), period="FY", severity=MEDIUM,
                       rule="uncoded_sales",
                       detail=(f"{len(g)} sales lines with no item code - cannot be "
                               f"matched to a contractor. Check against this "
                               f"customer's cost separately."),
                       amount=round(float(g["Amount"].sum()), 2)))
    return out


def zero_unit_rows(data: pd.DataFrame) -> list[dict]:
    """Zero-unit placeholder lines. Harmless individually, poisonous to any
    count-based formula."""
    z = data[(data["Units"].fillna(0) == 0) & (data["Source"] != "Payroll")]
    return [_ex(contractor=r["Description"], period=r["Month"], severity=LOW,
                rule="zero_unit_line",
                detail=f"{r['Source']} line with 0 units at rate {r['Rate']}",
                amount=r["Amount"]) for _, r in z.iterrows()]


def _coded_only(d: "pd.DataFrame") -> "pd.DataFrame":
    """Rows that carry an item code. Payrolling invoices (Xenon) bill each cost
    component as its own line on one date and carry no code; rules about
    duplicate or same-date lines would fire on every one of them."""
    key = "Match key" if "Match key" in d.columns else "Inventory code"
    return d[d[key].astype(str).str.strip() != ""]


def duplicate_same_date_lines(data: pd.DataFrame) -> list[dict]:
    """Same contractor, same date, multiple lines at different rates - either a
    legitimate mid-period rate change or a double-bill. Always needs eyes."""
    grp = (_coded_only(data)[_coded_only(data)["Source"] != "Payroll"]
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
    uncoded_sales_summary,
]


def drop_non_cost_rows(data: pd.DataFrame) -> pd.DataFrame:
    """Remove rows that are NOT employer cost before any rule sums money.

    PAYG withholding and net pay are carved OUT of gross wages - they are not
    additional cost. The mapper marks them "Ignore" (the workbook's own
    convention). If a rule sums them anyway, every PAYG contractor reads as
    ~25% more expensive than they are and throws a false negative-margin
    exception. Stripping them once here means no individual rule can get it
    wrong, and no future rule can reintroduce the bug.
    """
    if "Wages type with Super" not in data.columns:
        return data
    keep = data["Wages type with Super"].astype(str).str.strip().str.lower() != "ignore"
    return data[keep].copy()


def run_all(data: pd.DataFrame, items: pd.DataFrame) -> pd.DataFrame:
    data = drop_non_cost_rows(data)
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
