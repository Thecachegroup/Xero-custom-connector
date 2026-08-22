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
import io
import json
import time
import base64
import hmac
import logging
import tempfile
from datetime import date, datetime, timedelta
from functools import lru_cache

import pandas as pd
from mcp.server.fastmcp import FastMCP

from .xero_client import XeroClient
from . import mappers, checks, writes

log = logging.getLogger(__name__)

TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")

# Serverless filesystems are read-only except the system temp directory, so the
# old relative './output' failed with EROFS the moment it was deployed. Nothing
# in this server writes to a repo-relative path any more. Even /tmp is
# per-instance and vanishes, so the workbook is built in MEMORY and handed back
# in the response or over the download route; a temp copy is written only when
# TCG_OUTPUT_DIR is set explicitly, for local runs.
OUTPUT_DIR = os.environ.get("TCG_OUTPUT_DIR") or tempfile.gettempdir()

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


_cache: dict[str, tuple[float, object]] = {}


def _stage(key: str, build):
    """Memoise one stage of a pull.

    A whole-FY pull can exceed the 60-second MCP timeout from a cold start. When
    it does, every stage that HAD completed used to be thrown away, so the retry
    started from zero and timed out in exactly the same place. Caching per stage
    means a timed-out first call still leaves its finished work behind and the
    retry picks up where it stopped.

    This only helps when the retry lands on the same warm instance, which on
    serverless is likely but not guaranteed. The durable fix for a genuinely
    cold call is a longer function timeout (Vercel Settings > Functions > Max
    Duration; >60s needs Pro).
    """
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < CACHE_TTL:
        return hit[1]
    val = build()
    _cache[key] = (time.time(), val)
    return val


def _load(fy: str | None):
    key = (fy or "current").lower()
    return _stage(f"pull:{key}", lambda: _pull(fy))


def _tracking_categories():
    """The org's tracking category order, cached.

    Threaded through both mappers so tracking columns are filed by CATEGORY
    NAME rather than by position. One extra Xero call. If it fails the mapper
    falls back to per-line order rather than losing the whole pull - but the
    payroll-tax flag is only trustworthy with it, so the failure is logged loudly.
    """
    try:
        return client().tracking_categories()
    except Exception as e:                     # noqa: BLE001 - never fatal
        log.warning("Could not read Xero tracking categories (%s). Tracking "
                    "columns fall back to per-line order and the payroll-tax "
                    "flag may be unreliable.", e)
        return None


def _pull(fy: str | None):
    c = client()
    start, end, current_start = _fy_bounds(fy)
    k = (fy or "current").lower()

    categories = _stage("tracking-categories", _tracking_categories)

    # Sales = ACCREC invoices + ACCRECCREDIT credit notes; bills likewise.
    # Credit notes are a SEPARATE Xero endpoint, so pulling only /Invoices
    # silently dropped every credit ever raised and overstated revenue.
    def _docs(inv_type: str, credit_type: str):
        return list(c.iter_invoices(inv_type, start, end)) + [
            mappers.credit_note_to_invoice_shape(n)
            for n in c.iter_credit_notes(credit_type, start, end)
        ]

    sales_docs = _stage(f"docs:ACCREC:{k}", lambda: _docs("ACCREC", "ACCRECCREDIT"))
    bills_docs = _stage(f"docs:ACCPAY:{k}", lambda: _docs("ACCPAY", "ACCPAYCREDIT"))
    sales = mappers.invoices_to_rows(sales_docs, categories)
    bills = mappers.invoices_to_rows(bills_docs, categories)
    items = mappers.items_to_rows(_stage("items", c.items))

    # Pay run summaries carry Wages/Super/Tax per employee - the same figures as
    # the Payroll Activity Details report, at ~36 calls a year instead of ~400.
    # Set TCG_PAYSLIP_DETAIL=true only if per-pay-item breakdown is needed.
    def _runs():
        out = []
        for run in c.pay_runs(start, end):
            out.append(run if run.get("Payslips") else c.pay_run(run["PayRunID"]))
        return out

    runs = _stage(f"payruns:{k}", _runs)

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
def unmatched_employees(fy: str = "current") -> str:
    """List every payroll employee whose name does NOT resolve to an inventory
    item code, with suggested codes for each. These are the people whose cost
    is invisible to the invoice check - they show as 'nan' cost and make their
    contractor look invoiced-but-never-paid.

    Fix by adding the correct pairs to config/employee_codes.json, then
    redeploy. Suggestions are ranked guesses based on the code's initials -
    check each one before adding it.
    """
    data, items, *_ = _load(fy)
    pay = data[(data["Source"] == "Payroll") & (data["Inventory code"].isna())]
    if pay.empty:
        return f"All payroll employees resolve to an item code for {fy}. Nothing to fix."

    lines = [f"Unmatched payroll employees for {fy}:", ""]
    for name, g in pay.groupby(pay["Description"].fillna("(no name)")):
        total = float(g["Amount"].sum())
        lines.append(f"{name}  -  ${total:,.2f} across {len(g)} rows")
        for s_ in mappers.suggest_codes_for(str(name), items):
            lines.append(f"     suggest: {s_['code']!r}"
                         + (f"  (item name: {s_['name']})" if s_["name"] else ""))
        if not mappers.suggest_codes_for(str(name), items):
            lines.append("     no candidate found - check the item exists in Xero")
        lines.append("")
    lines += ["Add confirmed pairs to config/employee_codes.json under \"map\",",
              'e.g.  "Louis Soto": "Linfox - LSOTO"', "then commit and redeploy."]
    return "\n".join(lines)


@mcp.tool()
def refresh_cache() -> str:
    """Discard cached Xero data so the next check re-pulls live figures.
    Use after raising or paying invoices, or after a pay run."""
    n = len(_cache)
    _cache.clear()
    return f"Cache cleared ({n} cached stage(s) discarded). Next check will re-pull from Xero."


@mcp.tool()
def warm_cache(fy: str = "current") -> str:
    """Pull a financial year into cache so the next real call returns instantly.

    A cold whole-FY pull can exceed the 60-second MCP timeout. Run this first,
    and re-run it if it times out - each attempt keeps whatever stages it
    finished, so a second or third call gets progressively further before
    completing.
    """
    t0 = time.time()
    data, items, sales, bills, payroll = _load(fy)
    return (f"Cache warm for {fy} in {time.time() - t0:.1f}s: "
            f"{len(sales)} sales lines, {len(bills)} bill lines, "
            f"{len(payroll)} pay lines, {len(items)} items, {len(data)} data rows. "
            f"run_invoice_check and export_workbook will now return immediately.")


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
    side by side with the margin. Use when a specific contractor looks wrong.

    Matched by ITEM CODE, not by name. The text passed in only has to find ONE
    row belonging to the person; every other row carrying the same item code is
    pulled in with it.

    The bug this replaces: matching was a substring test against Description.
    A contractor whose payroll name differs from their item name - "Dat Le" on
    payroll, "Dat Tien Le" on the invoice - came back as two half-people. One
    showed sales with no cost (margin +$31,200), the other cost with no sales
    (margin -$29,010.66). Neither figure was real; the true margin was +$7,199.34.

    Margin is revenue minus EMPLOYER COST. PAYG withholding is carved OUT of
    gross wages and remitted to the ATO, so it is not additional cost. mappers
    already marks those rows "Ignore"; this now honours it. Summing them
    understated every PAYG contractor and reported profitable people as
    loss-making - Devinia Liddelow as -$7,121.94 when she was +$4,680.06.
    """
    data, *_ = _load(fy)

    seed = (data["Description"].fillna("").str.contains(contractor, case=False, na=False)
            | data["Inventory code"].fillna("").str.contains(contractor, case=False, na=False))
    if not seed.any():
        return f"No lines found for {contractor!r} in {fy}."

    # Widen from the rows the text found to every row sharing their item code.
    # Match key is already z-prefix-normalised, so 'Linfox - SJ' and
    # 'zLinfox - SJ' resolve to the same person.
    keys = {k for k in data.loc[seed, "Match key"].dropna().unique() if str(k).strip()}
    mask = (seed | data["Match key"].isin(keys)) if keys else seed
    sub = data[mask].sort_values("Date")

    ignore = (sub.get("Wages type with Super", pd.Series("", index=sub.index))
                 .fillna("").astype(str).str.strip().str.lower() == "ignore")
    is_sale = sub["Source"] == "Sales"
    is_cost = sub["Source"].isin(["Bills", "Payroll"]) & ~ignore

    revenue = sub.loc[is_sale, "Amount"].sum()
    cost = sub.loc[is_cost, "Amount"].sum()
    withheld = sub.loc[ignore, "Amount"].sum()

    head = [f"{contractor} - {fy}"]
    if len(keys) > 1:
        # More than one item code means either a retired/renamed duplicate or a
        # search term loose enough to have swept in someone else. Say so rather
        # than quietly reporting two people as one.
        head.append(f"NOTE: {len(keys)} item codes matched - {', '.join(sorted(keys))}. "
                    "Confirm this is one person.")
    head.append(f"Revenue: ${revenue:,.2f}   Employer cost: ${cost:,.2f}   "
                f"Gross margin: ${revenue - cost:,.2f}")
    if withheld:
        head.append(f"PAYG withheld (excluded from cost): ${withheld:,.2f}")

    cols = ["Date", "Source", "Inventory code", "Description", "Wage Type",
            "Units", "Rate", "Amount", "Status"]
    return ("\n".join(head) + "\n\n"
            + sub[[c for c in cols if c in sub.columns]].to_markdown(index=False))


@mcp.tool()
def get_rate_card() -> str:
    """The Xero item rate card: what each contractor should cost and sell for."""
    items = mappers.items_to_rows(client().items())
    cols = ["*ItemCode", "ItemName", "PurchasesUnitPrice", "SalesUnitPrice", "Status"]
    items["Margin"] = items["SalesUnitPrice"] - items["PurchasesUnitPrice"]
    return items[cols + ["Margin"]].to_markdown(index=False)


# ------------------------------------------------------------ payroll mailbox


def _graph():
    from .graph_client import GraphClient
    return GraphClient()


@mcp.tool()
def graph_diagnostics() -> str:
    """Prove the Microsoft connection works before trusting anything built on it.

    Run this FIRST after deploying Graph credentials. It separates "the auth is
    wrong" from "the logic is wrong", which are otherwise indistinguishable when
    a sweep quietly returns nothing.

    Reads only. Touches no files.
    """
    from . import mail_mappers as mmap
    try:
        g = _graph()
    except RuntimeError as e:
        return f"FAILED before connecting.\n{e}"

    lines = [f"Mailbox:     {g.mailbox}", f"Files owner: {g.files_owner}", ""]
    try:
        fid, used = g.resolve_folder(g.folder)
        lines.append(f"Mail folder: {used} -> {fid[:24]}...")
    except Exception as e:                                    # noqa: BLE001
        return "\n".join(lines + [f"Mail folder lookup FAILED: {e}"])

    try:
        drive = g._drive_id()
        lines.append(f"OneDrive for {g.files_owner}: found ({drive[:24]}...)")
    except Exception as e:                                    # noqa: BLE001
        lines.append(f"OneDrive lookup FAILED: {e}")

    roster = mmap.load_contractors()
    fort = [c for c in roster if mmap.in_scope(c)]
    lines += [
        "",
        f"Contractor lookup: {len(roster)} people, {sum(len(c['emails']) for c in roster)} addresses",
        f"  fortnightly: {len(fort)}   monthly: {len(roster) - len(fort)}",
        "",
        "Connection is good. Run sweep_timesheets(period_end) for a dry plan.",
    ]
    return "\n".join(lines)


@mcp.tool()
def sweep_timesheets(period_end: str, dry_run: bool = True, lookback_days: int = 45,
                     cadence: str = "fortnightly") -> str:
    """File contractor timesheets and invoices from the payroll mailbox.

    DRY BY DEFAULT. dry_run=True reports what it WOULD file and writes nothing.
    Read the plan, check it against who you expect, then re-run with
    dry_run=False. The plan is the whole point - a filing step you check up
    front is one you can trust, rather than one you audit afterwards.

    Args:
        period_end: fortnight ending Sunday, YYYY-MM-DD
        dry_run: True = plan only. False = actually write the files.
        lookback_days: how far back to search. Default 45, deliberately much
            wider than the fortnight - people send early, late and out of order,
            and a message outside the window is silently never filed.
        cadence: 'fortnightly' or 'monthly'.
    """
    from datetime import timedelta
    from . import mail_mappers as mmap

    end = date.fromisoformat(period_end)
    since = end - timedelta(days=lookback_days)
    g = _graph()

    _fid, folder_used = g.resolve_folder(g.folder)
    msgs = g.messages(g.folder, since, end + timedelta(days=10))
    plan = mmap.plan_filing(msgs, end, cadence=cadence)
    lo, hi = mmap.period_window(end)

    head = [
        f"Fortnight ending {end.isoformat()}  (period {mmap.period_start(end)} to {end})",
        f"Searched {folder_used} from {since}: {len(msgs)} messages",
        f"Folder: Contractors/Timesheets/{mmap.fortnight_folder(end)}/",
        "",
    ]

    if not plan["files"]:
        head.append("Nothing to file.")
    else:
        head.append(f"{'DRY RUN - nothing written' if dry_run else 'FILING'}: "
                    f"{len(plan['files'])} file(s)")
        head.append("")
        rows = sorted(plan["files"], key=lambda f: (f["contractor"], f["kind"], f["path"]))
        head.append(pd.DataFrame([{
            "Contractor": f["contractor"], "Type": f["kind"],
            "Sent as": f["source_name"] or "(unnamed)",
            "Filed as": f["path"].rsplit("/", 1)[-1],
        } for f in rows]).to_markdown(index=False))

    if plan.get("out_of_period"):
        head += ["", f"OUTSIDE THIS FORTNIGHT ({len(plan['out_of_period'])}) - received "
                     f"outside {lo} to {hi}, NOT filed:"]
        for o in plan["out_of_period"][:15]:
            head.append(f"  {o['received']}  {o['contractor']:<22} {o['subject'][:45]}")

    if plan["missing"]:
        head += ["", f"NOT SENT ANYTHING ({len(plan['missing'])}): "
                     + ", ".join(plan["missing"])]
    if plan["unmatched"]:
        head += ["", f"UNMATCHED SENDERS ({len(plan['unmatched'])}) - nobody in "
                     "config/contractor_mail.json has these addresses:"]
        for u in plan["unmatched"][:15]:
            head.append(f"  {u['sender']:<40} {u['subject'][:50]}")

    if dry_run:
        head += ["", "Nothing was written. Re-run with dry_run=False to file these."]
        return "\n".join(head)

    written, skipped, failed = [], [], []
    for f in plan["files"]:
        try:
            if g.exists(f["path"]):
                skipped.append(f["path"])
                continue
            blob = g.attachment_bytes(f["message_id"], f["attachment_id"])
            g.upload(f["path"], blob)
            written.append(f["path"])
        except Exception as e:                                # noqa: BLE001
            failed.append(f"{f['path']}: {e}")

    head += ["", f"Written: {len(written)}   Already there: {len(skipped)}   "
                 f"Failed: {len(failed)}"]
    for x in failed:
        head.append(f"  FAILED {x}")
    return "\n".join(head)


@mcp.tool()
def list_period_documents(period_end: str, window_days: int = 7) -> str:
    """Every Xero sales invoice and bill for a fortnight, per contractor.

    READ ONLY. Writes nothing, changes nothing.

    This is the reconnaissance both write steps depend on: you cannot fill in a
    draft invoice or attach a file to a bill without first knowing WHICH document
    it is. Proving that lookup here - against real invoice IDs, in a tool that
    cannot damage anything - is what stops the write tools guessing.

    It also answers the question that matters most after a pay run: which
    invoices are still sitting at zero units, i.e. work that has been paid for
    but not yet billed to the client.

    Args:
        period_end: fortnight ending Sunday, YYYY-MM-DD
        window_days: how far either side of period_end to look for documents
            dated against this period. Invoices are dated the Monday after, so
            the default of 7 catches them.
    """
    from datetime import timedelta
    from . import mail_mappers as mmap

    end = date.fromisoformat(period_end)
    lo = (end - timedelta(days=window_days)).isoformat()
    hi = (end + timedelta(days=window_days)).isoformat()
    c = client()

    roster = {r["item_code"]: r for r in mmap.load_contractors()}
    rows, unmatched = [], []

    for kind, xtype in (("Sales", "ACCREC"), ("Bill", "ACCPAY")):
        for inv in c.iter_invoices(xtype, lo, hi):
            for li in inv.get("LineItems", []) or []:
                code = li.get("ItemCode") or ""
                who = roster.get(code)
                if not who and not code:
                    continue
                rows.append({
                    "Contractor": who["name"] if who else f"(code {code})",
                    "Kind": kind,
                    "Status": inv.get("Status", ""),
                    "Date": str(inv.get("DateString", inv.get("Date", "")))[:10],
                    "Number": inv.get("InvoiceNumber", ""),
                    "Units": li.get("Quantity", 0),
                    "Amount": li.get("LineAmount", 0),
                    "InvoiceID": inv.get("InvoiceID", ""),
                })
                if not who:
                    unmatched.append(code)

    if not rows:
        return f"No sales invoices or bills dated {lo} to {hi}."

    df = pd.DataFrame(rows).sort_values(["Contractor", "Kind", "Date"])
    out = [f"Documents dated {lo} to {hi}  (fortnight ending {end})", ""]
    out.append(df[["Contractor", "Kind", "Status", "Date", "Number",
                   "Units", "Amount"]].to_markdown(index=False))

    zero = df[(df["Units"].astype(float) == 0) & (df["Kind"] == "Sales")]
    if not zero.empty:
        out += ["", f"NOT YET BILLED - {len(zero)} sales line(s) still at zero units:"]
        for _, r in zero.iterrows():
            out.append(f"  {r['Contractor']:<24} {r['Number']:<12} {r['Status']}")
        out.append("  Work has been paid for but the client has not been invoiced.")

    if unmatched:
        out += ["", "Item codes not in config/contractor_mail.json: "
                    + ", ".join(sorted(set(unmatched)))]

    out += ["", "InvoiceIDs (needed to attach files or fill a draft):"]
    for _, r in df.iterrows():
        out.append(f"  {r['Contractor']:<24} {r['Kind']:<6} {r['InvoiceID']}")
    return "\n".join(out)


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

LOCAL_TZ = os.environ.get("TCG_TIMEZONE", "Australia/Melbourne")


def _now_local() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(LOCAL_TZ))
    except Exception:                           # noqa: BLE001 - naming only
        return datetime.now()


def _build_workbook(fy: str) -> tuple[bytes, str, dict]:
    """Build the workbook entirely in memory. Returns (bytes, filename, stats).

    Nothing touches the filesystem. That is deliberate: the previous version did
    os.makedirs('./output') and died with EROFS on Vercel, and even a successful
    write to /tmp is unreachable from the client and gone on the next instance.
    """
    data, items, sales, bills, payroll = _load(fy)
    ex = checks.run_all(data, items)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        ex.to_excel(xw, sheet_name="Exceptions", index=False)
        data.to_excel(xw, sheet_name="Data", index=False)
        sales.drop(columns=["InvoiceID"], errors="ignore").to_excel(
            xw, sheet_name="Sales Invoices drop", index=False)
        bills.drop(columns=["InvoiceID"], errors="ignore").to_excel(
            xw, sheet_name="Bills Drop", index=False)
        payroll.to_excel(xw, sheet_name="Pay Details drop (formatted)", index=False)
        items.to_excel(xw, sheet_name="Inventory Drop", index=False)

    blob = buf.getvalue()
    # Melbourne local time, not the server's. Vercel runs UTC, which in winter
    # stamps files 10 hours behind the day they were actually produced - and the
    # timestamp exists precisely so two pulls of the same FY can be told apart.
    name = f"Invoice_Checker_{fy}_{_now_local():%Y-%m-%d_%H%M}.xlsx"

    sales_docs = sales.drop_duplicates("InvoiceID") if "InvoiceID" in sales.columns else sales
    invoices = sales_docs[sales_docs["Type"] == "Sales invoice"]
    credits = sales_docs[sales_docs["Type"] == "Sales credit note"]
    stats = {
        "data_rows": len(data),
        "exceptions": len(ex),
        "sales_invoices": len(invoices),
        "sales_invoice_total": round(float(pd.to_numeric(
            invoices.get("Total"), errors="coerce").fillna(0).sum()), 2),
        "sales_credit_notes": len(credits),
        "sales_credit_total": round(float(pd.to_numeric(
            credits.get("Total"), errors="coerce").fillna(0).sum()), 2),
        "draft_sales_documents": int(
            (sales_docs["Status"].astype(str).str.lower() == "draft").sum()),
        "kb": round(len(blob) / 1024, 1),
    }
    return blob, name, stats


def _public_base_url() -> str:
    """Public origin of this deployment, for building the download link."""
    for var in ("TCG_PUBLIC_URL", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        val = (os.environ.get(var) or "").strip().rstrip("/")
        if val:
            return val if val.startswith("http") else f"https://{val}"
    return ""


@mcp.custom_route("/workbook/{secret}/{fy}", methods=["GET"])
async def workbook_download(request):
    """Authenticated xlsx download.

    Gated by the same MCP_SHARED_SECRET as the MCP endpoint, compared in
    constant time. Anyone holding this URL can read the whole ledger, so it is
    exactly as sensitive as the MCP URL - treat it the same way.
    """
    from starlette.responses import PlainTextResponse, Response
    want = os.environ.get("MCP_SHARED_SECRET", "")
    got = request.path_params.get("secret", "")
    if not want or not hmac.compare_digest(str(got), str(want)):
        return PlainTextResponse("not found", status_code=404)
    blob, name, _ = _build_workbook(request.path_params.get("fy", "current"))
    return Response(
        blob,
        media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@mcp.tool()
def export_workbook(fy: str = "current", delivery: str = "url"):
    """Build the drop sheets + Data + Exceptions as an xlsx, ready to paste into
    the existing Invoice Checker workbook.

    Args:
        fy: 'current', or 'FY26', 'FY25' etc.
        delivery: how to hand the file back.
            'url'  - a secret-gated download link (DEFAULT). Costs nothing in
                     context and the file lands straight in Downloads.
            'file' - the xlsx embedded in this response as base64. Use when a
                     browser download is not practical. A full financial year is
                     roughly 35-85k tokens of context every single call, so it
                     is not the default.
            'both' - link and embedded file.
    """
    delivery = (delivery or "url").strip().lower()
    if delivery not in ("url", "file", "both"):
        return (f"Unknown delivery {delivery!r}. Use 'url', 'file' or 'both'.")

    blob, name, st = _build_workbook(fy)

    summary = [
        f"Invoice Checker workbook built for {fy} - {name} ({st['kb']:,.1f} KB)",
        f"  Sales invoices        {st['sales_invoices']:>6}   "
        f"${st['sales_invoice_total']:>14,.2f}  (GST inclusive)",
        f"  Sales credit notes    {st['sales_credit_notes']:>6}   "
        f"${st['sales_credit_total']:>14,.2f}  (carried as negatives)",
        f"  Draft sales documents {st['draft_sales_documents']:>6}",
        f"  Data rows             {st['data_rows']:>6}",
        f"  Exceptions            {st['exceptions']:>6}",
    ]

    if delivery in ("url", "both"):
        base, secret = _public_base_url(), os.environ.get("MCP_SHARED_SECRET", "")
        if base and secret:
            summary += ["", f"Download: {base}/workbook/{secret}/{fy}",
                        "Treat that link like a password - it reads the whole ledger."]
        else:
            summary += ["", "No download link available: set TCG_PUBLIC_URL and "
                        "MCP_SHARED_SECRET, or call again with delivery='file'."]

    if delivery in ("file", "both"):
        from mcp.types import TextContent, EmbeddedResource, BlobResourceContents
        return [
            TextContent(type="text", text="\n".join(summary)),
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri=f"file:///{name}",
                    mimeType=XLSX_MIME,
                    blob=base64.b64encode(blob).decode(),
                ),
            ),
        ]

    return "\n".join(summary)


@mcp.tool()
def tracking_diagnostics(fy: str = "current") -> str:
    """Show which Xero tracking category lands in which drop column, and how
    many lines carry each option.

    Run this once after any change to tracking in Xero. It is the check that
    proves the payroll-tax flag is being read from the right column - the thing
    that was silently wrong when tracking was read by position.
    """
    try:
        cats = client().tracking_categories()
    except Exception as e:                      # noqa: BLE001
        cats = []
        note = f"Could not read /TrackingCategories: {e}"
    else:
        note = ""

    _, _, sales, bills, _ = _load(fy)
    out = [f"Xero tracking categories, in order: {cats or '(none returned)'}"]
    if note:
        out.append(note)
    if mappers.TRACKING_ORDER_OVERRIDE:
        out.append(f"TCG_TRACKING_ORDER override in force: "
                   f"{mappers.TRACKING_ORDER_OVERRIDE}")
    out.append(f"Payroll-tax category matched on name: "
               f"{mappers.PAYROLL_TAX_CATEGORY!r}")
    out.append("")

    for label, df in (("Sales", sales), ("Bills", bills)):
        if df.empty:
            continue
        out.append(f"{label} - {len(df)} lines")
        for n_col, o_col in (("TrackingName1", "TrackingOption1"),
                             ("TrackingName2", "TrackingOption2")):
            names = df[n_col].astype(str).str.strip()
            opts = df[o_col].astype(str).str.strip()
            out.append(f"  {n_col}: {(names != '').sum()} populated "
                       f"{sorted(set(names[names != '']))[:3]}")
            counts = opts[opts != ""].value_counts().to_dict()
            out.append(f"  {o_col}: {(opts != '').sum()} populated  {counts}")
        flag = mappers.payroll_tax_option(df).value_counts().to_dict()
        out.append(f"  -> resolved Payroll Tax Payable: {flag}")
        out.append("")
    return "\n".join(out)


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


@mcp.tool()
def list_period_drafts(period_end: str, window_days: int = 10) -> str:
    """Every DRAFT invoice and bill dated around a fortnight end, line by line.

    Read-only. This is what has to be filled in before anything is sent, and
    seeing it exactly as Xero holds it is the difference between filling a draft
    and accidentally raising a second one beside it.

    The repeating templates generate these drafts with Quantity 0.00. A line
    still showing 0 is a line nobody has billed.
    """
    c = client()
    end = date.fromisoformat(period_end)
    lo = (end - timedelta(days=window_days)).isoformat()
    hi = (end + timedelta(days=window_days)).isoformat()

    out = [f"DRAFT invoices and bills dated {lo} to {hi}", ""]
    grand_empty = 0

    for kind, label in (("ACCREC", "SALES INVOICES (to clients)"),
                        ("ACCPAY", "BILLS (from contractors)")):
        try:
            docs = list(c.iter_invoices(kind, lo, hi, statuses=["DRAFT"]))
        except Exception as e:                                    # noqa: BLE001
            out += [label, f"  lookup FAILED: {e}", ""]
            continue

        out.append(f"{label} - {len(docs)} draft(s)")
        if not docs:
            out += ["  none", ""]
            continue

        for d in docs:
            out.append("")
            out.append(f"  {d.get('Contact', {}).get('Name', '?')}"
                       f"   No: {d.get('InvoiceNumber') or '(none)'}"
                       f"   Date: {mappers.parse_xero_date(d.get('Date'))}"
                       f"   Ref: {d.get('Reference') or '(none)'}")
            out.append(f"    InvoiceID: {d.get('InvoiceID')}")
            rows = []
            for li in d.get("LineItems", []) or []:
                qty = li.get("Quantity")
                if not qty:
                    grand_empty += 1
                rows.append({
                    "Item": li.get("ItemCode") or "",
                    "Description": str(li.get("Description") or "")[:44],
                    "Qty": qty,
                    "Unit": li.get("UnitAmount"),
                    "Amount": li.get("LineAmount"),
                    "Account": li.get("AccountCode") or "",
                    "Tax": li.get("TaxType") or "",
                    "LineItemID": li.get("LineItemID"),
                })
            if rows:
                out.append(pd.DataFrame(rows).to_markdown(index=False))
        out.append("")

    out.append(f"Lines with no quantity: {grand_empty}")
    return "\n".join(out)


# ---------------------------------------------------------------- write tools
# Every one of these creates a DRAFT. Andrew approves in Xero.


@mcp.tool()
def fill_period_drafts(period_end: str, quantities: str, dry_run: bool = True,
                       window_days: int = 10, kinds: str = "both",
                       bill_numbers: str = "", reference: str = "auto") -> str:
    """Put the days worked onto the repeating drafts Xero has already generated.

    QUANTITIES is one 'item code: days' per line, e.g.

        Linfox - DL: 10
        Linfox - EK: 2.5
        Tec - PS: 42.23

    Matched on ITEM CODE, never on a name. Names arrive as "Dat Le", "Dat Tien
    Le" and "Le, Dat" for one person; the item code is the only stable key, and
    matching on a name once split one contractor into two and moved $60,000.

    THIS FILLS. IT NEVER CREATES. The repeating templates raise the drafts; this
    puts a quantity on the line that is already there. Creating a second invoice
    beside a draft Linfox has already been sent is the failure this exists to
    avoid.

    SAFETY, in order:
      - dry_run defaults True. Nothing is written until it is explicitly False.
      - Only DRAFT documents are touched. Approved, sent and paid are untouchable.
      - Only lines whose quantity is currently ZERO are filled. A line somebody
        has already billed is left exactly as it is, so running this four times
        across a billing week cannot add the same days four times.
      - The fortnight ending is appended to the description, once. A line that
        already carries it is not stamped again.

    BILL_NUMBERS, optional, one 'item code: their invoice number' per line:

        Linfox - BVIRK: INV-0016
        Linfox - JJ: 20260802

    Puts the contractor's OWN invoice number onto their bill, replacing the
    placeholder the repeating template generates ("Inv", "JJ", "KCrabb"). Bills
    only - a sales invoice number is TCG's and is never touched. A bill already
    carrying the right number is left alone.

    REFERENCE goes on the SALES invoices, in TCG's house format taken from the
    invoices actually sent: "3 August to 16 August 2026". 'auto' derives it from
    the period; a literal string overrides it; '' leaves references alone.

    KINDS: 'both' | 'sales' | 'bills'.
    """
    c = client()
    end = date.fromisoformat(period_end)
    lo = (end - timedelta(days=window_days)).isoformat()
    hi = (end + timedelta(days=window_days)).isoformat()
    stamp = f"Fortnight ending {end.strftime('%d/%m/%Y')}"

    wanted, bad = writes.parse_quantities(quantities)
    if bad:
        return "Could not read these lines - expected 'item code: days':\n  " + "\n  ".join(bad)
    if not wanted:
        return "No quantities given. Nothing to do."

    ref = (writes.period_reference(end - timedelta(days=13), end)
           if reference == "auto" else (reference or "").strip())

    numbers: dict[str, str] = {}
    for raw in str(bill_numbers or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        code, _, num = line.rpartition(":")
        if code.strip() and num.strip():
            numbers[code.strip()] = num.strip()

    types = {"both": ["ACCREC", "ACCPAY"], "sales": ["ACCREC"], "bills": ["ACCPAY"]}
    if kinds not in types:
        return "kinds must be 'both', 'sales' or 'bills'."

    planned, skipped, errors, renumbered, rereferenced = [], [], [], [], []
    seen_codes: set[str] = set()

    for kind in types[kinds]:
        label = "invoice" if kind == "ACCREC" else "bill"
        try:
            docs = list(c.iter_invoices(kind, lo, hi, statuses=["DRAFT"]))
        except Exception as e:                                     # noqa: BLE001
            errors.append(f"{label}s: could not be read - {e}")
            continue

        for d in docs:
            for li in d.get("LineItems", []) or []:
                code = (li.get("ItemCode") or "").strip()
                if code in wanted:
                    seen_codes.add(code)

        p_, s_, to_write = writes.plan_line_fill(docs, wanted, stamp, label)
        planned += p_
        skipped += s_

        # The contractor's own invoice number, on their bill only. A sales
        # invoice number belongs to TCG and is never rewritten.
        num_changes = writes.plan_number_change(docs, numbers) if (
            kind == "ACCPAY" and numbers) else []
        renumbered += num_changes
        new_num = {n["InvoiceID"]: n["Now"] for n in num_changes}

        # The period reference is TCG's, so it goes on sales invoices only, and
        # only on the ones this run is actually filling. An untouched draft -
        # Saeid Almaher's, the empty expenses one - keeps whatever it had.
        touched = {d["InvoiceID"] for d in to_write}
        ref_changes = writes.plan_reference_change(
            [d for d in docs if d.get("InvoiceID") in touched], ref
        ) if (kind == "ACCREC" and ref) else []
        rereferenced += ref_changes
        new_ref = {r["InvoiceID"]: r["Now"] for r in ref_changes}

        by_id = {d["InvoiceID"]: d for d in to_write}
        for inv_id in list(new_num) + list(new_ref):
            by_id.setdefault(inv_id, {"InvoiceID": inv_id, "LineItems": None,
                                      "InvoiceNumber": None})

        if not dry_run:
            for inv_id, doc in by_id.items():
                try:
                    writes.update_invoice(c, inv_id,
                                          lines=doc.get("LineItems"),
                                          invoice_number=new_num.get(inv_id),
                                          reference=new_ref.get(inv_id))
                except Exception as e:                             # noqa: BLE001
                    errors.append(f"{label} {doc.get('InvoiceNumber') or inv_id[:8]}: {e}")

    missing = sorted(set(wanted) - seen_codes)

    out = [f"Fortnight ending {end.isoformat()}   drafts dated {lo} to {hi}",
           "DRY RUN - nothing written." if dry_run else "WRITTEN to Xero. Still DRAFT - approve and send yourself.",
           ""]
    if planned or renumbered:
        pass
    if planned:
        df = pd.DataFrame(planned)
        out.append(df.to_markdown(index=False))
        out.append("")
        for k in df["Kind"].unique():
            out.append(f"{k} total: ${df[df['Kind'] == k]['Amount'].sum():,.2f}")
    elif not renumbered and not rereferenced:
        out.append("Nothing to fill - every matching line already has a quantity.")
    if rereferenced:
        out += ["", f'REFERENCE set to "{ref}" on:',
                pd.DataFrame([{k: v for k, v in r.items() if k != "InvoiceID"}
                              for r in rereferenced]).to_markdown(index=False)]
    if renumbered:
        out += ["", "BILL NUMBERS set to the contractor's own:",
                pd.DataFrame([{k: v for k, v in n.items() if k != "InvoiceID"}
                              for n in renumbered]).to_markdown(index=False)]
    if skipped:
        out += ["", "LEFT ALONE:", pd.DataFrame(skipped).to_markdown(index=False)]
    if missing:
        out += ["", "NO DRAFT FOUND for these item codes: " + ", ".join(missing)]
    if errors:
        out += ["", "ERRORS:"] + [f"  {e}" for e in errors]
    return "\n".join(out)


@mcp.tool()
def attach_period_files(period_end: str, dry_run: bool = True,
                        window_days: int = 10) -> str:
    """Put the evidence onto the Xero records before Andrew reviews them.

    TIMESHEET -> the SALES INVOICE, marked to travel with it when it is emailed,
    so Linfox sees what they are being billed for without anyone attaching it by
    hand.

    THE CONTRACTOR'S OWN INVOICE -> their BILL, and it does NOT travel - it is
    TCG's record of what they charged, not something to send back out.

    Files come from Contractors/Timesheets/Fortnight  Ending DDMMYYYY/ and are
    matched on the CONTAINING FOLDER, not the filename. The folder is
    "<Client>_<Name>" and maps to exactly one item code; a filename prefix is
    initials, and two people can share initials.

    Safety:
      - dry_run defaults True
      - DRAFT documents only
      - a file already attached under that name is SKIPPED. Xero does not reject
        a duplicate filename, it stores a second copy - so without this check
        every run of the billing week adds another copy of the same timesheet.

    Needs the accounting.attachments scope. Without it Xero returns 401 and the
    error will say so.
    """
    from . import mail_mappers as mmap
    c = client()
    g = _graph()
    end = date.fromisoformat(period_end)
    lo = (end - timedelta(days=window_days)).isoformat()
    hi = (end + timedelta(days=window_days)).isoformat()
    folder = mmap.fortnight_folder(end)

    try:
        files = g.list_files(folder, recursive=True)
    except Exception as e:                                         # noqa: BLE001
        return f"Could not read {folder}: {e}"
    if not files:
        return f"No files in {folder}. Run sweep_timesheets first."

    folder_to_code = {ct["folder"]: ct["item_code"] for ct in mmap.load_contractors()}

    sales, bills = {}, {}
    for kind, bucket in (("ACCREC", sales), ("ACCPAY", bills)):
        for d in c.iter_invoices(kind, lo, hi, statuses=["DRAFT"]):
            for li in d.get("LineItems") or []:
                code = (li.get("ItemCode") or "").strip()
                if code:
                    bucket.setdefault(code, d)

    planned, unplaceable = writes.plan_attachments(files, folder_to_code, sales, bills)

    already, todo, errors = [], [], []
    seen_on: dict[str, set] = {}
    for item in planned:
        doc_id = item["InvoiceID"]
        if doc_id not in seen_on:
            seen_on[doc_id] = writes.existing_attachments(c, doc_id)
        if item["File"].strip().lower() in seen_on[doc_id]:
            already.append(item)
        else:
            todo.append(item)

    if not dry_run:
        done = []
        for item in todo:
            try:
                content = g.download(f"{folder}/{item['Path']}")
                writes._attach(c, "Invoices", item["InvoiceID"], item["File"],
                               content, writes.content_type_for(item["File"]),
                               include_online=item["IncludeOnline"])
                done.append(item)
            except Exception as e:                                 # noqa: BLE001
                errors.append(f"{item['File']} -> {item['Doc']}: {e}")
        todo = done

    out = [f"Attachments for fortnight ending {end.isoformat()}",
           f"Folder: Contractors/Timesheets/{folder}/",
           "DRY RUN - nothing attached." if dry_run else "ATTACHED. Records still DRAFT.",
           ""]
    if todo:
        cols = ["File", "Kind", "Onto", "Contact", "Doc", "IncludeOnline"]
        out.append(pd.DataFrame(todo)[cols].to_markdown(index=False))
    else:
        out.append("Nothing to attach.")
    if already:
        out += ["", f"ALREADY ATTACHED, skipped ({len(already)}):",
                pd.DataFrame(already)[["File", "Doc"]].to_markdown(index=False)]
    if unplaceable:
        out += ["", "COULD NOT PLACE:",
                pd.DataFrame(unplaceable).to_markdown(index=False)]
    if errors:
        out += ["", "ERRORS:"] + [f"  {e}" for e in errors]
    return "\n".join(out)


@mcp.tool()
def submit_period_invoices(period_end: str, dry_run: bool = True,
                           window_days: int = 10) -> str:
    """Move the finished sales invoices from Draft to Awaiting Approval.

    The point is that Drafts becomes a to-do list. An invoice that is filled and
    has its timesheet attached moves out by itself; anything still sitting in
    Drafts on Saturday is something that did not come through - a contractor who
    never sent a timesheet, an expenses invoice with nothing to pass through, a
    leaver whose template is still firing.

    An invoice is only submitted when BOTH are true:
      - every line has a quantity
      - something is attached to it

    Submitting a half-finished invoice would defeat the whole point.

    SALES INVOICES ONLY. Bills stay in draft - they are paid, not sent.
    Approving and sending remains Andrew's; this only moves DRAFT -> SUBMITTED,
    and he can move it back.
    """
    c = client()
    end = date.fromisoformat(period_end)
    lo = (end - timedelta(days=window_days)).isoformat()
    hi = (end + timedelta(days=window_days)).isoformat()

    docs = list(c.iter_invoices("ACCREC", lo, hi, statuses=["DRAFT"]))
    if not docs:
        return f"No draft sales invoices dated {lo} to {hi}."

    att = {d["InvoiceID"]: writes.existing_attachments(c, d["InvoiceID"]) for d in docs}
    ready, held = writes.plan_submission(docs, att)

    errors = []
    if not dry_run and ready:
        done = []
        for r in ready:
            try:
                writes.set_invoice_status(c, r["InvoiceID"], "SUBMITTED")
                done.append(r)
            except Exception as e:                                 # noqa: BLE001
                errors.append(f"{r['Doc']}: {e}")
        ready = done

    out = [f"Sales invoices for fortnight ending {end.isoformat()}",
           "DRY RUN - nothing moved." if dry_run else
           "MOVED to Awaiting Approval. Approving and sending is still yours.",
           ""]
    if ready:
        out += [f"READY ({len(ready)}):",
                pd.DataFrame([{k: v for k, v in r.items() if k != "InvoiceID"}
                              for r in ready]).to_markdown(index=False)]
    else:
        out.append("Nothing is ready to submit.")
    if held:
        out += ["", f"LEFT IN DRAFTS ({len(held)}) - these are the ones to look at:",
                pd.DataFrame([{k: v for k, v in h.items() if k != "InvoiceID"}
                              for h in held]).to_markdown(index=False)]
    if errors:
        out += ["", "ERRORS:"] + [f"  {e}" for e in errors]
    return "\n".join(out)


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
