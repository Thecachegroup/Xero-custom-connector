"""
TCG Xero Invoice Checker - MCP server.

Transports:
  stdio           -> Claude Desktop / Cowork (local)
  streamable-http -> claude.ai + Android app (deployed on Render)

Run local:   python -m src.server
Run remote:  MCP_TRANSPORT=http python -m src.server

SECURITY. In http mode the endpoint is served at /mcp/<MCP_SHARED_SECRET>. That
secret IS the lock on the front door: anyone holding the full URL can read the
entire TCG ledger and payroll history. The server refuses to start in http mode
without one. Treat the URL like a password - never paste it into a document,
an email, a ticket, or a public repo.
"""

from __future__ import annotations

import os
import json
import time
from datetime import date, datetime
from functools import lru_cache

import pandas as pd
from mcp.server.fastmcp import FastMCP

from .xero_client import XeroClient
from . import mappers, checks, writes

TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
OUTPUT_DIR = os.environ.get("TCG_OUTPUT_DIR", "./output")

# Cache a full pull for this many seconds. A whole-FY pull is hundreds of Xero
# calls and several minutes; without this, every tool call repeats it and burns
# through the 5,000/day API limit.
CACHE_TTL = int(os.environ.get("TCG_CACHE_TTL", "900"))

if TRANSPORT == "http":
    _secret = os.environ.get("MCP_SHARED_SECRET", "").strip()
    if len(_secret) < 24:
        raise SystemExit(
            "MCP_SHARED_SECRET must be set to a random string of at least 24 "
            "characters before the server will accept HTTP traffic. Without it "
            "the endpoint would expose the TCG ledger to anyone who finds it."
        )
    mcp = FastMCP(
        "tcg-xero-invoice-checker",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),   # Render supplies PORT
        streamable_http_path=f"/mcp/{_secret}",
        stateless_http=True,
    )
else:
    mcp = FastMCP("tcg-xero-invoice-checker")


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    """Unauthenticated liveness probe for Render. Returns no business data."""
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("ok")


@lru_cache(maxsize=1)
def client() -> XeroClient:
    return XeroClient()


def _fy_bounds(fy: str | None) -> tuple[str, str, date]:
    """fy='FY26' -> 1 Jul 2025 to 30 Jun 2026. None -> current FY."""
    today = date.today()
    current_start = date(today.year if today.month >= 7 else today.year - 1, 7, 1)
    if not fy or fy.lower() in ("current", "current fy"):
        start = current_start
    else:
        yy = int(str(fy).upper().replace("FY", ""))
        start = date(2000 + yy - 1, 7, 1)
    end = date(start.year + 1, 6, 30)
    return start.isoformat(), end.isoformat(), current_start


_cache: dict[str, tuple[float, tuple]] = {}


def _load(fy: str | None):
    key = (fy or "current").lower()
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < CACHE_TTL:
        return hit[1]
    result = _pull(fy)
    _cache[key] = (time.time(), result)
    return result


def _pull(fy: str | None):
    c = client()
    start, end, current_start = _fy_bounds(fy)
    sales = mappers.invoices_to_rows(list(c.iter_invoices("ACCREC", start, end)))
    bills = mappers.invoices_to_rows(list(c.iter_invoices("ACCPAY", start, end)))
    items = mappers.items_to_rows(c.items())

    # Pay run summaries carry Wages/Super/Tax per employee - the same figures as
    # the Payroll Activity Details report, at ~36 calls a year instead of ~400.
    # Set TCG_PAYSLIP_DETAIL=true only if per-pay-item breakdown is needed.
    runs = []
    for run in c.pay_runs(start, end):
        runs.append(run if run.get("Payslips") else c.pay_run(run["PayRunID"]))

    if os.environ.get("TCG_PAYSLIP_DETAIL", "").strip().lower() == "true":
        payslips = [c.payslip(ps["PayslipID"])
                    for run in runs for ps in run.get("Payslips", []) or []]
        payroll = mappers.payslips_to_rows(payslips)
    else:
        payroll = mappers.payrun_summaries_to_rows(runs)

    data = mappers.build_data_frame(
        sales, bills, payroll, items,
        customer_lookup=_customer_lookup(),
        no_payroll_tax=_no_payroll_tax(),
        current_fy_start=current_start,
    )
    return data, items, sales, bills, payroll


@mcp.tool()
def refresh_cache() -> str:
    """Discard cached Xero data so the next check re-pulls live figures.
    Use after raising or paying invoices, or after a pay run."""
    n = len(_cache)
    _cache.clear()
    return f"Cache cleared ({n} cached pull(s) discarded). Next check will re-pull from Xero."


def _customer_lookup() -> dict[str, str]:
    path = os.environ.get("TCG_CUSTOMER_LOOKUP", "config/customer_lookup.json")
    return json.load(open(path)) if os.path.exists(path) else {}


def _no_payroll_tax() -> set[str]:
    path = os.environ.get("TCG_NO_PAYROLL_TAX", "config/no_payroll_tax.json")
    return set(json.load(open(path))) if os.path.exists(path) else set()


# ---------------------------------------------------------------- tools

@mcp.tool()
def run_invoice_check(fy: str = "current") -> str:
    """Run the full invoice check for a financial year and return the exception
    report. This is the primary tool - it replaces the manual monthly scan.

    Args:
        fy: 'current', or 'FY26', 'FY25' etc.
    """
    data, items, *_ = _load(fy)
    ex = checks.run_all(data, items)

    # Totals every run, so reconciling against the workbook is automatic rather
    # than a separate manual step. PAYG withheld is shown but excluded from cost:
    # it is carved out of gross wages and remitted to the ATO, not paid on top.
    is_ignore = data["Wages type with Super"].astype(str).str.lower() == "ignore"
    rev = float(data.loc[data["Source"] == "Sales", "Amount"].sum())
    cost = float(data.loc[data["Source"].isin(["Bills", "Payroll"]) & ~is_ignore,
                          "Amount"].sum())
    paygw = float(data.loc[is_ignore, "Amount"].sum())
    margin = rev - cost
    totals = [
        "",
        f"TOTALS {fy}",
        f"  Invoiced          ${rev:>14,.2f}",
        f"  Contractor cost   ${cost:>14,.2f}",
        f"  Gross margin      ${margin:>14,.2f}"
        + (f"   ({margin / rev * 100:.1f}%)" if rev else ""),
        f"  PAYG withheld     ${paygw:>14,.2f}   (remit to ATO - not a cost)",
        "",
    ]

    if ex.empty:
        return "\n".join([f"Invoice check for {fy}: {len(data)} lines, no exceptions."] + totals)

    summary = ex["severity"].value_counts().to_dict()
    lines = [
        f"Invoice check for {fy}: {len(data)} lines, {len(ex)} exceptions "
        f"({summary.get('HIGH',0)} HIGH / {summary.get('MEDIUM',0)} MEDIUM / {summary.get('LOW',0)} LOW)",
        *totals,
        ex.to_markdown(index=False),
    ]
    return "\n".join(lines)


@mcp.tool()
def get_contractor_ledger(contractor: str, fy: str = "current") -> str:
    """Every sales line, bill line and pay line for one contractor in a FY,
    side by side with the margin. Use when a specific contractor looks wrong."""
    data, *_ = _load(fy)
    mask = data["Description"].str.contains(contractor, case=False, na=False) | \
           data["Inventory code"].fillna("").str.contains(contractor, case=False, na=False)
    sub = data[mask].sort_values("Date")
    if sub.empty:
        return f"No lines found for {contractor!r} in {fy}."

    cols = ["Date", "Source", "Inventory code", "Description", "Units", "Rate", "Amount", "Status"]
    margin = (sub[sub["Source"] == "Sales"]["Amount"].sum()
              - sub[sub["Source"].isin(["Bills", "Payroll"])]["Amount"].sum())
    return (f"{contractor} - {fy}\nGross margin: ${margin:,.2f}\n\n"
            + sub[[c for c in cols if c in sub.columns]].to_markdown(index=False))


@mcp.tool()
def get_rate_card() -> str:
    """The Xero item rate card: what each contractor should cost and sell for."""
    items = mappers.items_to_rows(client().items())
    cols = ["*ItemCode", "ItemName", "PurchasesUnitPrice", "SalesUnitPrice", "Status"]
    items["Margin"] = items["SalesUnitPrice"] - items["PurchasesUnitPrice"]
    return items[cols + ["Margin"]].to_markdown(index=False)


@mcp.tool()
def export_workbook(fy: str = "current") -> str:
    """Write the drop sheets + Data + Exceptions to an xlsx, ready to drop into
    the existing Invoice Checker workbook."""
    data, items, sales, bills, payroll = _load(fy)
    ex = checks.run_all(data, items)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"Invoice_Checker_{fy}_{datetime.now():%Y%m%d}.xlsx")
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        ex.to_excel(xw, sheet_name="Exceptions", index=False)
        data.to_excel(xw, sheet_name="Data", index=False)
        sales.drop(columns=["InvoiceID"], errors="ignore").to_excel(xw, sheet_name="Sales Invoices drop", index=False)
        bills.drop(columns=["InvoiceID"], errors="ignore").to_excel(xw, sheet_name="Bills Drop", index=False)
        payroll.to_excel(xw, sheet_name="Pay Details drop (formatted)", index=False)
        items.to_excel(xw, sheet_name="Inventory Drop", index=False)
    return f"Written: {path} ({len(data)} data rows, {len(ex)} exceptions)"


@mcp.tool()
def cash_and_receivables() -> str:
    """Outstanding and overdue totals - who owes TCG and how much."""
    c = client()
    today = date.today().isoformat()
    inv = list(c.iter_invoices("ACCREC", "2000-01-01", today, statuses=["AUTHORISED"]))
    df = mappers.invoices_to_rows(inv)
    if df.empty:
        return "Nothing outstanding."
    head = df.drop_duplicates("InvoiceID")
    out = head[head["InvoiceAmountDue"] > 0]
    overdue = out[pd.to_datetime(out["DueDate"]) < pd.Timestamp(today)]
    return (f"Outstanding: ${out['InvoiceAmountDue'].sum():,.2f} across {len(out)} invoices\n"
            f"Overdue:     ${overdue['InvoiceAmountDue'].sum():,.2f} across {len(overdue)} invoices\n\n"
            + overdue.nlargest(10, "InvoiceAmountDue")[
                ["ContactName", "InvoiceNumber", "DueDate", "InvoiceAmountDue"]
            ].to_markdown(index=False))


# ---------------------------------------------------------------- write tools
# Every one of these creates a DRAFT. Andrew approves in Xero.


@mcp.tool()
def payroll_entry_plan(days_worked: str, period_start: str, period_end: str) -> str:
    """Turn 'name: days' lines into the exact payroll and sales figures for a
    fortnight, using each person's current Xero rate card. Read-only - it
    calculates and shows, it does not send anything.

    Args:
        days_worked: one per line, e.g. 'Jay Jhala: 10' / 'Bhasker Veela: 9.5'
        period_start: YYYY-MM-DD
        period_end: YYYY-MM-DD
    """
    items = mappers.items_to_rows(client().items())
    rows, problems = [], []
    for raw in days_worked.strip().splitlines():
        if not raw.strip():
            continue
        if ":" not in raw:
            problems.append(f"Could not read {raw!r} - expected 'Name: days'.")
            continue
        name, dstr = raw.rsplit(":", 1)
        try:
            days = float(dstr.strip())
        except ValueError:
            problems.append(f"Could not read the days in {raw!r}.")
            continue
        name = name.strip()
        hit = items[items["ItemName"].str.contains(name, case=False, na=False) |
                    items["*ItemCode"].str.contains(name, case=False, na=False)]
        if len(hit) != 1:
            problems.append(
                f"{name}: {'no' if hit.empty else len(hit)} rate card matches - "
                "resolve in Xero before entering this person."
            )
            continue
        r = hit.iloc[0]
        cost, sell = float(r["PurchasesUnitPrice"]), float(r["SalesUnitPrice"])
        rows.append({
            "Person": name, "Code": r["*ItemCode"], "Days": days,
            "Pay (cost)": round(days * cost, 2),
            "Invoice (sell)": round(days * sell, 2),
            "Margin": round(days * (sell - cost), 2),
        })

    if not rows and problems:
        return "Nothing calculated.\n" + "\n".join(problems)
    df = pd.DataFrame(rows)
    out = [f"Pay period {period_start} to {period_end}", "", df.to_markdown(index=False), "",
           f"Total to pay:    ${df['Pay (cost)'].sum():,.2f}",
           f"Total to invoice: ${df['Invoice (sell)'].sum():,.2f}",
           f"Margin:           ${df['Margin'].sum():,.2f}",
           f"Days:             {df['Days'].sum():g}"]
    if problems:
        out += ["", "NOT INCLUDED:"] + [f"  - {p}" for p in problems]
    out += ["", "Nothing has been sent to Xero. Say 'post these as drafts' to create "
            "draft timesheets and a draft invoice for you to approve."]
    return "\n".join(out)


@mcp.tool()
def post_draft_timesheet(employee_name: str, period_start: str, period_end: str,
                         earnings_rate_id: str, units_by_date: str,
                         payroll_calendar_id: str = "") -> str:
    """Create a DRAFT timesheet in Xero. It is not approved and does not pay
    anyone until you approve it in Xero yourself.

    Args:
        employee_name: enough of the name to match exactly one employee
        payroll_calendar_id: from list_payroll_setup
        period_start / period_end: YYYY-MM-DD
        earnings_rate_id: from list_payroll_setup
        units_by_date: one per line, 'YYYY-MM-DD: hours'
    """
    from datetime import timedelta
    c = client()
    emp = writes.find_employee(c, employee_name)
    by_date = {}
    for raw in units_by_date.strip().splitlines():
        if not raw.strip():
            continue
        d, u = raw.rsplit(":", 1)
        by_date[d.strip()] = float(u.strip())

    d0, d1 = date.fromisoformat(period_start), date.fromisoformat(period_end)
    span = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    stray = sorted(set(by_date) - set(span))
    if stray:
        return (f"NOTHING POSTED. These dates fall outside {period_start} to "
                f"{period_end}: {', '.join(stray)}. Fix the dates or the period.")
    units = [by_date.get(day, 0.0) for day in span]

    res = writes.create_draft_timesheet(
        c, emp["EmployeeID"], period_start, period_end, earnings_rate_id, units)
    return (f"DRAFT timesheet created for {emp.get('FirstName')} {emp.get('LastName')}: "
            f"{sum(units):g} units across {len([u for u in units if u])} worked days, "
            f"{period_start} to {period_end} ({len(span)} day period).\n"
            f"It is a DRAFT. Approve it in Xero (Payroll > Timesheets) before the pay run.\n"
            f"Xero returned: {str(res)[:200]}")


@mcp.tool()
def list_payroll_setup() -> str:
    """The payroll calendar IDs and earnings rate IDs needed to build a
    timesheet. Read-only."""
    c = client()
    cals = writes.payroll_calendars(c)
    rates = writes.earnings_rates(c)
    out = ["Payroll calendars:"]
    out += [f"  {x.get('Name')} ({x.get('CalendarType')}) -> {x.get('PayrollCalendarID')}"
            for x in cals]
    out += ["", "Earnings rates:"]
    out += [f"  {x.get('Name') or x.get('name')} "
            f"[{x.get('RateType') or x.get('EarningsType') or ''}] -> "
            f"{x.get('EarningsRateID') or x.get('earningsRateID')}" for x in rates]
    return "\n".join(out)


@mcp.tool()
def post_pay_period(days_worked: str, period_start: str, period_end: str,
                    payroll_calendar_id: str, earnings_rate_id: str,
                    invoice_date: str = "", invoice_due_date: str = "") -> str:
    """Create BOTH sides of a pay period in Xero as drafts, from one set of days:
    the payroll timesheets AND the matching sales invoice lines. Because both
    come from the same days figure, payroll and the invoice cannot diverge.

    Everything created is a DRAFT. Nothing is approved, sent or paid.

    Args:
        days_worked: one per line, 'Name: days'
        period_start / period_end: YYYY-MM-DD
        payroll_calendar_id / earnings_rate_id: from list_payroll_setup
        invoice_date / invoice_due_date: default to period_end and +14 days
    """
    from datetime import timedelta
    c = client()
    items = mappers.items_to_rows(c.items())
    lookup = _customer_lookup()
    inv_date = invoice_date or period_end
    due = invoice_due_date or (date.fromisoformat(inv_date) + timedelta(days=14)).isoformat()

    parsed, problems = [], []
    for raw in days_worked.strip().splitlines():
        if not raw.strip():
            continue
        if ":" not in raw:
            problems.append(f"Could not read {raw!r} - expected 'Name: days'.")
            continue
        name, dstr = raw.rsplit(":", 1)
        try:
            days = float(dstr.strip())
        except ValueError:
            problems.append(f"Could not read the days in {raw!r}.")
            continue
        name = name.strip()
        hit = items[items["ItemName"].str.contains(name, case=False, na=False) |
                    items["*ItemCode"].str.contains(name, case=False, na=False)]
        if len(hit) != 1:
            problems.append(f"{name}: {'no' if hit.empty else len(hit)} rate card "
                            "matches - fix in Xero before posting this person.")
            continue
        parsed.append((name, days, hit.iloc[0]))

    if problems:
        return ("NOTHING POSTED. Resolve these first - I will not post a partial "
                "pay period, because a half-entered run is worse than none:\n"
                + "\n".join(f"  - {p}" for p in problems))

    ts_done, inv_lines, log_lines = [], [], []
    for name, days, r in parsed:
        emp = writes.find_employee(c, name)
        d0, d1 = date.fromisoformat(period_start), date.fromisoformat(period_end)
        span = (d1 - d0).days + 1
        units = [0.0] * span
        units[-1] = days          # booked to the period end date
        res = writes.create_draft_timesheet(
            c, emp["EmployeeID"], period_start, period_end, earnings_rate_id, units)
        ts_done.append(name)
        inv_lines.append({
            "ItemCode": r["*ItemCode"],
            "Description": f"{r['ItemName']}, {period_start} to {period_end}",
            "Quantity": days,
            "UnitAmount": float(r["SalesUnitPrice"]),
        })
        log_lines.append(
            f"  {name}: {days:g} days | pay ${days*float(r['PurchasesUnitPrice']):,.2f} "
            f"| invoice ${days*float(r['SalesUnitPrice']):,.2f}")

    out = [f"DRAFT timesheets created: {len(ts_done)}", *log_lines, ""]
    out.append("Draft invoice lines prepared (one invoice per customer still needs "
               "a contact ID - run create_draft_invoice per customer, or tell me the "
               "customer and I will do it):")
    for l in inv_lines:
        out.append(f"  {l['ItemCode']}: {l['Quantity']:g} x ${l['UnitAmount']:,.2f} "
                   f"= ${l['Quantity']*l['UnitAmount']:,.2f}")
    out += ["", "ALL DRAFTS. Nothing approved, sent or paid.",
            "Review: Xero > Payroll > Timesheets, and Business > Invoices > Draft."]
    _cache.clear()
    return "\n".join(out)


@mcp.tool()
def set_rate_card(item_code: str, cost_rate: float = None,
                  sell_rate: float = None) -> str:
    """Update a contractor's cost and/or sell rate on the Xero item rate card."""
    res = writes.update_item_rates(client(), item_code, cost_rate, sell_rate)
    _cache.clear()
    return (f"Rate card updated for {item_code}: "
            f"cost={cost_rate if cost_rate is not None else 'unchanged'}, "
            f"sell={sell_rate if sell_rate is not None else 'unchanged'}. "
            f"Xero returned: {str(res)[:200]}")


if __name__ == "__main__":
    mcp.run(transport="streamable-http" if TRANSPORT == "http" else "stdio")
