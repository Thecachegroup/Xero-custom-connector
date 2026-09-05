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
from . import mappers, checks, writes, roster, coverage

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

    exempt_codes, _unresolved = _payroll_tax_exempt_codes(items)
    data = mappers.build_data_frame(
        sales, bills, payroll, items,
        customer_lookup=_customer_lookup(),
        no_payroll_tax=_no_payroll_tax(),
        no_payroll_tax_codes=exempt_codes,
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


def _tax_inclusive_ok() -> set[str]:
    """Suppliers whose Inclusive tax basis is deliberate and correct.

    The house rule is TAX EXCLUSIVE, so Inclusive is normally reported and held
    back from Awaiting Approval. A handful of suppliers genuinely bill a
    GST-inclusive total - That's Sparkling Clean is one - and a rule cannot tell
    those from a mistake, so they are named.

    A missing or unreadable file returns nothing, which restores the stricter
    behaviour rather than silently waiving the check.
    """
    path = os.environ.get("TCG_TAX_INCLUSIVE_OK", "config/tax_inclusive_ok.json")
    if not os.path.exists(path):
        return set()
    try:
        raw = json.load(open(path))
    except Exception as e:                                        # noqa: BLE001
        log.warning("Could not read %s (%s). Every Inclusive document will be "
                    "reported and held, including the ones that are correct.",
                    path, e)
        return set()
    if isinstance(raw, list):
        return set(raw)
    if isinstance(raw, dict):
        return set(raw.get("names") or [])
    return set()


def _payroll_tax_config() -> tuple[set[str], set[str]]:
    """(exempt names, explicitly exempt item codes) from no_payroll_tax.json.

    Both shapes are accepted. The file was a bare list of names; it is now
    {"names": [...], "item_codes": [...]} so a person with no inventory item, or
    an item whose Xero Name does not read like the person's name, can be named
    directly. A bare list still loads, so an old copy of the config cannot
    silently empty the exemption list.
    """
    path = os.environ.get("TCG_NO_PAYROLL_TAX", "config/no_payroll_tax.json")
    if not os.path.exists(path):
        return set(), set()
    try:
        raw = json.load(open(path))
    except Exception as e:                                        # noqa: BLE001
        log.warning("Could not read %s (%s). No payroll-tax exemptions applied "
                    "- the base will be OVERSTATED until this is fixed.", path, e)
        return set(), set()
    if isinstance(raw, list):
        return set(raw), set()
    if isinstance(raw, dict):
        return set(raw.get("names") or []), set(raw.get("item_codes") or [])
    return set(), set()


def _no_payroll_tax() -> set[str]:
    return _payroll_tax_config()[0]


def _payroll_tax_exempt_codes(items) -> tuple[set[str], list[str]]:
    """Every normalised item code that is exempt from payroll tax, and the
    exempt names no item could be found for.

    The names are resolved against the Xero item list once, here, so the config
    stays a list of people and the matching still happens on the item code. See
    mappers.exempt_item_codes for why the name match is strict, and
    build_data_frame for what the old description-only match cost.
    """
    names, explicit = _payroll_tax_config()
    resolved, unresolved = mappers.exempt_item_codes(items, names)
    return resolved | {mappers.normalise_code(c) for c in explicit}, unresolved


def _ignored_item_codes() -> set[str]:
    """Item codes flagged `ignore` in roster_overrides.json - SEEK ads, training
    products, pass-through expenses. Things, not people, so never adjustments."""
    return {c for c, ov in (roster.load_overrides() or {}).items()
            if (ov or {}).get("ignore")}


def _cadence_value(entry: dict, side: str = "") -> str:
    """The cadence for one side of one person: 'monthly' or 'fortnightly'.

    `cadence` in roster_overrides.json is normally a plain string applying to
    both sides. It may instead be a mapping {"sales": ..., "bills": ...} for
    somebody whose two cycles differ - Peter Small is billed fortnightly by
    TecAlliance and invoiced monthly to Xenon Media. SIDE is 'sales', 'bills',
    or '' meaning "monthly on either side".

    Anything unreadable reads as fortnightly, which is what everybody not
    listed is, so a malformed entry cannot silently take somebody out of the
    fortnightly run.
    """
    def _clean(v) -> str:
        # Only 'monthly' is acted on. Anything else - a typo, a null, a value
        # nobody has taught this about - reads as fortnightly, so a malformed
        # entry cannot quietly take somebody out of the fortnightly run.
        return "monthly" if str(v or "").strip().lower() == "monthly" else "fortnightly"

    raw = (entry or {}).get("cadence")
    if isinstance(raw, dict):
        if side in ("sales", "bills"):
            return _clean(raw.get(side))
        return ("monthly" if any(_clean(v) == "monthly" for v in raw.values())
                else "fortnightly")
    return _clean(raw)


def _monthly_item_codes(side: str = "") -> set[str]:
    """Item codes whose person bills monthly on SIDE. Cadence already lives in
    roster_overrides.json keyed on item code; nothing else needs to know."""
    return {c for c, ov in (roster.load_overrides() or {}).items()
            if _cadence_value(ov, side) == "monthly"}


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
def get_rate_card(show_accounts: bool = False, contains: str = "") -> str:
    """The Xero item rate card: what each contractor should cost and sell for.

    SHOW_ACCOUNTS adds the general ledger codes each item posts to - the cost
    account and the revenue account. Andrew codes contractor cost to 477 (PAYG
    Contractors), and an item quietly pointing somewhere else puts the cost in
    the wrong place on every invoice it touches.

    CONTAINS filters to item codes or names containing that text, because the
    full card runs to 200 lines.
    """
    items = mappers.items_to_rows(client().items())
    if contains:
        needle = contains.strip().lower()
        items = items[items["*ItemCode"].str.lower().str.contains(needle, na=False)
                      | items["ItemName"].str.lower().str.contains(needle, na=False)]
        if items.empty:
            return f"No item matches {contains!r}."
    items["Margin"] = items["SalesUnitPrice"] - items["PurchasesUnitPrice"]
    cols = ["*ItemCode", "ItemName", "PurchasesUnitPrice", "SalesUnitPrice",
            "Margin", "Status"]
    if show_accounts:
        cols = ["*ItemCode", "ItemName", "PurchasesUnitPrice", "PurchasesAccount",
                "SalesUnitPrice", "SalesAccount", "InventoryType", "Quantity",
                "InventoryAssetAccount", "CostOfGoodsSoldAccount", "Status"]
    return items[cols].to_markdown(index=False)


# ------------------------------------------------------------ payroll mailbox


def _graph():
    from .graph_client import GraphClient
    return GraphClient()


def _live_roster() -> tuple[list[dict], list[dict], list[dict]]:
    """(roster, gaps, duplicate items) straight from Xero.

    The roster is derived, never listed. Anyone set up to be paid is on it the
    moment they are set up, and a leaver drops off when their template stops -
    which is the whole point, because the old config file needed a commit and a
    redeploy every time somebody joined and did not get one.
    """
    import pandas as pd
    from . import roster as rst

    c = client()
    items = c.items()
    employees = c.employees()
    repeating = c.repeating_invoices()

    idf = pd.DataFrame([{"*ItemCode": i.get("Code"), "Name": i.get("Name")}
                        for i in items])
    resolve = mappers.build_employee_code_map(idf)["_resolve"] if not idf.empty \
        else (lambda _n: None)

    people = rst.build(items, employees, repeating,
                       contacts=None, resolve_employee=resolve)
    return people, rst.gaps(items, people), rst.duplicates(items)


def _prior_invoice_numbers(period_end: date, months: int = 12,
                           window_days: int = 10) -> dict:
    """{item code: {invoice number: the period it was billed for}} for every
    contractor bill belonging to an EARLIER fortnight than this one.

    This is what makes a duplicate detectable. The bill carries the
    contractor's OWN invoice number - fill_period_drafts puts it there - so
    Xero already holds the answer to "have I billed this number before". Keyed
    on ITEM CODE, never a contact name: Mudassir Ali's bills sit under
    Datacraft Consulting Services Pty Ltd, and a name match would find nothing.

    VOIDED bills are excluded deliberately. A voided document's number is free
    to be used again, and refusing it would be wrong.

    Both the raw code and the normalised one are keyed, so a person whose
    retired item was renamed with a leading z still resolves.

    THE CUTOFF IS THE BILLING DATE, NOT THE WORK PERIOD. This was wrong when
    the guard shipped and the guard could therefore never fire once, for
    anybody. It cut at period_end - 13 days, i.e. the Monday this fortnight's
    WORK started - 17 August for the fortnight ending 30 August - and required
    a bill to be dated strictly before that. But TCG dates a bill the Monday
    AFTER the fortnight it pays for, so the previous fortnight's bills are dated
    17 August too, and every one of them was excluded by a single day. Proved
    live on 3 September: Bilal Virk's INV-0016 and Jay Jhala's 20260802 both sit
    on bills dated 2026-08-17, both PAID, both re-sent and both filed without a
    murmur by the very sweep built to refuse them.

    Cutting at period_end - window_days instead separates the two fortnights on
    the axis that actually distinguishes them. It works because fortnights are
    exactly 14 days apart: the previous fortnight's billing Monday is 13 days
    before this period_end, and this fortnight's own bills land within a few
    days either side of it. WINDOW_DAYS MUST STAY BELOW 14 or the previous
    fortnight starts being read as this one.
    """
    from datetime import timedelta

    c = client()
    cutoff = period_end - timedelta(days=window_days)
    start = cutoff - timedelta(days=31 * months)
    out: dict = {}
    for d in c.iter_invoices("ACCPAY", start.isoformat(), cutoff.isoformat(),
                             statuses=["DRAFT", "SUBMITTED", "AUTHORISED", "PAID"]):
        num = str(d.get("InvoiceNumber") or "").strip()
        if not num:
            continue
        when = mappers.parse_xero_date(d.get("Date"))
        if not when or when >= cutoff:
            continue
        for li in d.get("LineItems") or []:
            raw = str(li.get("ItemCode") or "").strip()
            if not raw:
                continue
            for key in {raw, mappers.normalise_code(raw)}:
                out.setdefault(key, {}).setdefault(num, when.isoformat())
    return out


@mcp.tool()
def roster_diagnostics() -> str:
    """Who the sweep thinks is on the roster, and why - before it matters.

    READ ONLY. Run this after deploying, and any time somebody joins or leaves,
    to see the roster Xero produces without waiting for a sweep to act on it.

    Three things it answers. WHO is on it, with the addresses that will be
    matched. WHO IS SET UP BUT CANNOT BE BILLED - an active inventory item with
    no payroll record and no repeating bill, which is exactly how Mazher Ali
    worked five days in the fortnight ending 30 August 2026 with no invoice
    behind him. And WHICH PEOPLE CARRY MORE THAN ONE LIVE ITEM, usually an old
    one at a stale rate that somebody can still bill off.
    """
    people, gaps_, dupes = _live_roster()

    lines = [f"Roster built from Xero: {len(people)} people", ""]
    rows = [{
        "Name": p["name"], "Item": p["item_code"], "Kind": p["kind"],
        "Cadence": p["cadence"], "Folder": p["folder"],
        "Addresses on file": ", ".join(p["emails"]) or "(none - matched by name)",
    } for p in people]
    lines.append(pd.DataFrame(rows).to_markdown(index=False))

    lines += ["", "An address is not required. Nine of ten contractor addresses",
              "carry the person's own name and are read directly; the tenth",
              "carries their Xero contact name.", ""]

    if gaps_:
        lines += [f"SET UP BUT CANNOT BE BILLED ({len(gaps_)}) - active item, no "
                  "payroll record and no repeating bill. Their work raises no "
                  "draft and nobody is chased for a timesheet:"]
        for g_ in gaps_:
            lines.append(f"  {g_['item_code']:<24} {g_['name']}")
        lines.append("")

    if dupes:
        lines += [f"MORE THAN ONE LIVE ITEM ({len(dupes)}) - check the rate on "
                  "each, and archive or delete the ones nobody should bill:"]
        for d in dupes:
            lines.append(f"  {d['name']:<24} {', '.join(d['item_codes'])}")
        lines.append("")

    lines.append("config/roster_overrides.json holds only what Xero cannot know "
                 "- folder names that do not match the person, the monthly "
                 "cadence, and extra personal addresses. Nobody needs an entry "
                 "there to be found.")
    return "\n".join(lines)


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
def list_repeating_templates(kinds: str = "both", contains: str = "",
                             include_deleted: bool = False,
                             include_fixed_fee: bool = False) -> str:
    """Repeating templates that generate a NON-ZERO quantity. READ ONLY.

    THE POINT OF THIS. The repeating templates raise the fortnightly and
    monthly drafts, and `fill_period_drafts` only fills a line whose quantity is
    currently ZERO - that guard is what makes four sweeps across a billing week
    safe. A day-rate template that generates a non-zero quantity is therefore
    invisible to the fill: nothing corrects it, and it invoices the client that
    quantity every period until somebody reads the number. Bhasker Veela's
    sales template generated at 1: his August invoice reached Awaiting Approval
    at ONE day, $320, against 21 days worked, ~$6,573 under-billed.

    WHAT IS NOT A FAULT, and why this does not just list every non-zero line.
    A first pass over the live org flagged 80 lines of which one was wrong.

      DELETED templates generate nothing at all. 62 of those 80. Hidden unless
      include_deleted=True.

      FIXED-FEE lines are billed as one unit of a monthly amount, not days x a
      rate - the Xenon Media monthly billing, the office cleaning, the offshore
      people on an annual fee split over twelve. Quantity 1 is CORRECT on
      those and setting it to zero would stop them billing. They carry no item
      code, or an item code whose only live template is this one. Listed
      separately, not flagged. include_fixed_fee=True to see them.

      A DAY-RATE line is the one that matters, and the giveaway is asymmetry:
      the same item code has another live template - the matching bill, or the
      matching sale - sitting at ZERO. One of the pair was reset and the other
      was missed. That is exactly Bhasker: his VVR bill at 0, his Linfox sale
      at 1.

    A report where nine flags in ten are noise catches nothing. This one is
    meant to be short enough to read.

    KINDS: 'both' | 'sales' | 'bills'.
    CONTAINS filters on the contact name or the item code.
    """
    c = client()
    want = str(kinds or "both").lower()
    needle = str(contains or "").strip().lower()

    templates = c.repeating_invoices()
    inclusive_ok = _tax_inclusive_ok()

    # An item code is "day rate" when a LIVE template somewhere carries it at
    # zero - something is filling that line from a timesheet. Built across
    # every template first, because the evidence for a sales line is usually on
    # the bill and the other way round.
    zero_somewhere: set[str] = set()
    for r in templates:
        if str(r.get("Status") or "").upper() == "DELETED":
            continue
        for line in r.get("LineItems") or []:
            code = str(line.get("ItemCode") or "").strip()
            qty = line.get("Quantity")
            if code and float(qty or 0) == 0.0:
                zero_somewhere.add(code.lower())

    flagged, fixed, deleted_hits, tax_bad = [], [], 0, []
    for r in templates:
        typ = str(r.get("Type") or "").upper()
        if want == "sales" and typ != "ACCREC":
            continue
        if want == "bills" and typ != "ACCPAY":
            continue
        status = str(r.get("Status") or "").upper()
        contact = str((r.get("Contact") or {}).get("Name") or "")
        # The tax basis is a fault whatever the quantity is, and the template
        # is where it starts - fix the invoice only and next period repeats it.
        if status != "DELETED":
            for row_ in writes.tax_basis_problems(
                    [r], "sales" if typ == "ACCREC" else "bill",
                    inclusive_ok=inclusive_ok):
                row_["Repeats"] = "template"
                tax_bad.append({k: v for k, v in row_.items()
                                if k not in ("InvoiceID", "Repeats")})
        sched = r.get("Schedule") or {}
        unit = str(sched.get("Unit") or "").title()
        period = sched.get("Period")
        every = f"every {period} {unit}".strip() if period else unit or "?"

        for line in r.get("LineItems") or []:
            code = str(line.get("ItemCode") or "").strip()
            if needle and needle not in contact.lower() and needle not in code.lower():
                continue
            qty = line.get("Quantity")
            qty = 0.0 if qty is None else float(qty)
            if qty == 0.0:
                continue
            if status == "DELETED":
                deleted_hits += 1
                if not include_deleted:
                    continue
            row = {
                "Kind": "sales" if typ == "ACCREC" else "bill",
                "Contact": contact[:34],
                "Item": code or "(no item code)",
                "Qty": qty,
                "Unit": line.get("UnitAmount"),
                "Repeats": every,
                "Status": status,
            }
            if code and code.lower() in zero_somewhere:
                # the matching template for this person sits at zero
                row[""] = "  <<<"
                flagged.append(row)
            elif status == "DELETED":
                flagged.append({**row, "": ""})
            else:
                fixed.append(row)

    out: list[str] = []
    if flagged:
        cols = ["", "Kind", "Contact", "Item", "Qty", "Unit", "Repeats", "Status"]
        df = pd.DataFrame(flagged)
        out += [f"{len([f for f in flagged if f.get('')])} DAY-RATE TEMPLATE(S) "
                "GENERATING A NON-ZERO QUANTITY", "",
                df[[c_ for c_ in cols if c_ in df.columns]].to_markdown(index=False), "",
                "Marked <<<: the SAME item code has another live template sitting",
                "at zero - the matching bill or the matching sale. One of the pair",
                "was reset and the other was missed, so this line invoices at the",
                "quantity shown every period and fill_period_drafts will never",
                "touch it.", "",
                "Fix each with: set_repeating_quantity('<item code>', 0)"]
    else:
        out.append("No day-rate template is generating a non-zero quantity. "
                   "Nothing to fix.")

    if fixed:
        out += ["", f"{len(fixed)} FIXED-FEE line(s) - NOT a fault, do not zero these."]
        if include_fixed_fee:
            out += ["", pd.DataFrame(fixed).to_markdown(index=False)]
        else:
            by = {}
            for f in fixed:
                by[f["Contact"]] = by.get(f["Contact"], 0) + 1
            out.append("  " + ", ".join(f"{k} ({v})" for k, v in sorted(by.items())))
        out += ["", "One unit of a monthly amount rather than days x a rate - the",
                "monthly billing, the cleaning, an annual fee split over twelve.",
                "Quantity 1 is correct and zeroing one stops it billing. Nothing",
                "here carries an item code that any live template bills by the",
                "day. include_fixed_fee=True for the full list."]

    if tax_bad:
        out += ["", "*** TEMPLATE TAX BASIS - READ THIS ***",
                pd.DataFrame(tax_bad).to_markdown(index=False),
                "",
                "Every TCG document is TAX EXCLUSIVE. A template set otherwise",
                "generates every future invoice that way - an INCLUSIVE one",
                "takes the GST out of the rate rather than adding it on, so a",
                "$1,000/day line bills $909.09 + $90.91 instead of $1,000 +",
                "$100. Fix the template in Xero: Business > Invoices >",
                "Repeating (or Bills to pay > Repeating), open it, set the",
                "Amounts are dropdown to Tax Exclusive, Save. Fix any draft it",
                "has already generated as well."]

    if deleted_hits and not include_deleted:
        out += ["", f"{deleted_hits} non-zero line(s) on DELETED templates hidden - "
                    "they generate nothing. include_deleted=True to see them."]
    return "\n".join(out)


@mcp.tool()
def set_repeating_quantity(item_code: str, quantity: float = 0,
                           kinds: str = "both", dry_run: bool = True) -> str:
    """Set the quantity a repeating template generates. DRY BY DEFAULT.

    Almost always used to put a template back to ZERO so that
    `fill_period_drafts` can do its job. A template is read back from Xero
    first and re-posted with its own values, so nothing but the quantity moves.

    Matched on ITEM CODE, never a name.
    """
    c = client()
    want = str(kinds or "both").lower()
    code_l = str(item_code or "").strip().lower()
    if not code_l:
        return "Give an item code. Nothing has been changed."

    hits = []
    for r in c.repeating_invoices():
        typ = str(r.get("Type") or "").upper()
        if want == "sales" and typ != "ACCREC":
            continue
        if want == "bills" and typ != "ACCPAY":
            continue
        if str(r.get("Status") or "").upper() == "DELETED":
            continue        # deleted in Xero; it generates nothing and is not ours to touch
        if any(str(l.get("ItemCode") or "").lower() == code_l
               for l in r.get("LineItems") or []):
            hits.append(r)

    if not hits:
        return (f"No repeating template carries item code {item_code!r}. "
                "Nothing has been changed.")

    lines = [f"{len(hits)} live template(s) carry {item_code}", ""]
    written: list[tuple[str, str, str]] = []
    failed: list[str] = []
    api_refused = False
    for r in hits:
        typ = "sales" if str(r.get("Type")).upper() == "ACCREC" else "bill"
        contact = str((r.get("Contact") or {}).get("Name") or "")
        changed = []
        for l in r.get("LineItems") or []:
            if str(l.get("ItemCode") or "").lower() != code_l:
                continue
            was = l.get("Quantity")
            was = 0.0 if was is None else float(was)
            if was == float(quantity):
                lines.append(f"  {typ:<5} {contact[:32]:<34} already {was:g}")
                continue
            l["Quantity"] = float(quantity)
            changed.append(f"  {typ:<5} {contact[:32]:<34} {was:g} -> {float(quantity):g}")
        if not changed:
            continue
        lines += changed
        if not dry_run:
            try:
                writes.update_repeating_template(c, r)
                written.append((str(r.get("RepeatingInvoiceID") or ""), typ, contact))
            except Exception as e:                                 # noqa: BLE001
                failed.append(f"  {typ:<5} {contact[:32]:<34} NOT CHANGED - {e}")
                if "must be set to DELETED" in str(e):
                    api_refused = True

    if failed:
        lines += ["", "NOT CHANGED:"] + failed

    if api_refused:
        # Xero's RepeatingInvoices endpoint accepts a POST against an existing
        # template only to DELETE it. The quantity cannot be changed through
        # the API at all - proven against the live org, 3 Sep 2026, on
        # Bhasker's template. Deleting and recreating is NOT the workaround:
        # it loses the template's history and a recreate that goes wrong
        # invoices a client every period. So this becomes instructions.
        lines += ["",
                  "XERO WILL NOT LET THE API CHANGE A REPEATING TEMPLATE.",
                  "",
                  "A POST against an existing template is only accepted to",
                  "DELETE it. Nothing has been changed and nothing will be -",
                  "deleting and recreating would lose the template's history",
                  "and a recreate that goes wrong bills a client every period.",
                  "",
                  "DO IT IN XERO, it is four clicks:",
                  "",
                  "  1. Business > Invoices, then the Repeating tab.",
                  "     For a bill: Business > Bills to pay > Repeating.",
                  f"  2. Open the template above carrying {item_code}.",
                  f"  3. Change Qty on that line to {float(quantity):g}.",
                  "  4. Save.",
                  "",
                  "Then run list_repeating_templates() to confirm it took."]

    if dry_run:
        lines += ["", "DRY RUN - nothing written. Re-run with dry_run=False to apply.",
                  "Note: Xero may refuse the write - see below if it does."]
    elif written:
        # Read the templates back and prove the number actually moved. This is
        # the one tool whose whole reason for existing is that nobody reads the
        # quantity, so it is not going to ask somebody to go and read it.
        after = {str(t.get("RepeatingInvoiceID") or ""): t
                 for t in c.repeating_invoices()}
        verified, wrong = [], []
        for tid, typ, contact in written:
            t = after.get(tid)
            if not t:
                wrong.append(f"  {typ:<5} {contact[:32]:<34} could not be read back")
                continue
            now = [float(l.get("Quantity") or 0) for l in (t.get("LineItems") or [])
                   if str(l.get("ItemCode") or "").lower() == code_l]
            if now and all(q == float(quantity) for q in now):
                verified.append(f"  {typ:<5} {contact[:32]:<34} confirmed at {float(quantity):g}")
            else:
                wrong.append(f"  {typ:<5} {contact[:32]:<34} reads back as "
                             f"{', '.join(f'{q:g}' for q in now) or '(no line)'}")
        lines += ["", "READ BACK FROM XERO:"] + verified + wrong
        if wrong:
            lines += ["", "One or more templates do NOT read back at the value "
                          "asked for. Do not assume the change took - open the "
                          "template in Xero before the next period generates."]
    return "\n".join(lines)


@mcp.tool()
def sweep_timesheets(period_end: str, dry_run: bool = True, lookback_days: int = 45,
                     cadence: str = "all") -> str:
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
        cadence: 'all' (default), 'fortnightly' or 'monthly'. Monthly
            contractors file into the CURRENT FORTNIGHT folder alongside
            everyone else - Andrew's decision, 2 Sep 2026 - so 'all' is the
            normal way to run this and the other two are for narrowing down.

    REPEATS ARE NOT FILED (5 Sep 2026). A page is compared on its bytes against
    what is already in the destination folder before it is written. When
    somebody replies to a timesheet mail the client quotes the original, and
    every inline image arrives a second time as a real attachment on a real
    message - filed under a new part number, and identical to a page already
    there. Prasanthi Dharanikota's fortnight ending 30 August 2026 held seven
    pages of which two were a page twice. They are reported, not written.
    Folders that already hold repeats are cleaned with onedrive_dedupe.
    """
    from datetime import timedelta
    from . import mail_mappers as mmap

    end = date.fromisoformat(period_end)
    since = end - timedelta(days=lookback_days)
    g = _graph()

    _fid, folder_used = g.resolve_folder(g.folder)
    msgs = g.messages(g.folder, since, end + timedelta(days=10))

    # Andrew forwards contractor mail into the payroll mailbox, which makes him
    # the sender. bodyPreview usually carries the quoted "From:" line, but his
    # signature can push it past 255 characters - so anything still unmatched
    # after the preview gets its full body fetched, one message at a time.
    own = tuple(d.strip().lower() for d in
                os.environ.get("TCG_OWN_DOMAINS", "thecachegroup.com.au").split(",")
                if d.strip())
    for m in msgs:
        if mmap.match_sender_or_forward(m, own_domains=own):
            continue
        addr = str(m.get("sender", "")).lower()
        if any(addr.endswith("@" + d) for d in own):
            try:
                m["body"] = g.message_body(m["id"])
            except Exception:                                      # noqa: BLE001
                pass

    people, gaps_, _dupes = _live_roster()

    # Xero already knows which invoice numbers have been billed before. If this
    # lookup fails the sweep still runs - it just loses the duplicate guard,
    # which is how it behaved before the guard existed.
    try:
        prior_numbers = _prior_invoice_numbers(end)
    except Exception as e:                                     # noqa: BLE001
        prior_numbers, prior_err = {}, str(e)
    else:
        prior_err = ""

    plan = mmap.plan_filing(msgs, end, cadence=cadence, own_domains=own,
                            contractors=people,
                            prior_invoice_numbers=prior_numbers)
    lo, hi = mmap.period_window(end)

    in_play = [p for p in people if mmap.in_scope(p, cadence)]
    monthly = [p["name"] for p in in_play if str(p.get("cadence")) == "monthly"]
    head = [
        f"Fortnight ending {end.isoformat()}  (period {mmap.period_start(end)} to {end})",
        f"Searched {folder_used} from {since}: {len(msgs)} messages",
        f"Roster from Xero: {len(in_play)} of {len(people)} people swept"
        + (f", {len(monthly)} of them monthly ({', '.join(sorted(monthly))})"
           if monthly else ""),
        f"Folder: Contractors/Timesheets/{mmap.fortnight_folder(end)}/",
        "",
    ]

    if not plan["files"] and not plan.get("body_only"):
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

    if plan.get("duplicates"):
        head += ["", f"DUPLICATE - NOT FILED ({len(plan['duplicates'])}). This "
                     "invoice number was already billed in an earlier period, so "
                     "it is not evidence for this one.",
                 "The WHOLE message is refused, timesheets included - they cover "
                 "the period that invoice covers. Chase a fresh invoice; leave "
                 "the sales invoice and the bill at zero:"]
        for d_ in plan["duplicates"]:
            head.append(f"  {d_['contractor']:<22} {str(d_['number']):<16} "
                        f"already billed for {d_['used_for']}"
                        f"   ({d_['attachments']} attachment(s) held)")
            head.append(f"  {'':<22} sent as {d_['file']}")

    if prior_err:
        head += ["", "DUPLICATE CHECK DID NOT RUN - could not read prior bills "
                     f"from Xero: {prior_err}",
                 "Check invoice numbers against last fortnight by hand."]

    if plan.get("body_only"):
        head += ["", f"TIMESHEET IS IN THE EMAIL BODY ({len(plan['body_only'])}) - no "
                     "attachment to file, so the message itself is saved as .eml.",
                 "READ IT AND KEY THE DAYS BY HAND - nothing here counts the days:"]
        for b in plan["body_only"]:
            head.append(f"  {b['received'][:10]}  {b['contractor']:<22} "
                        f"{str(b['subject'])[:40]:<42} -> {b['path'].rsplit('/', 1)[-1]}")

    if plan.get("out_of_period"):
        # The heading used to read "received outside <window>", which was wrong
        # far more often than it was right: the reason is almost always the
        # period the document STATES, and the message itself arrived inside the
        # window. Six were reported that way on 5 Sep 2026, all received on the
        # 31st, well inside it - which is exactly what hid the monthly defect.
        head += ["", f"OUTSIDE THEIR PERIOD ({len(plan['out_of_period'])}) - NOT filed. "
                     "The window shown is the one that person is judged against;",
                 "a monthly contractor's is their cycle, not the fortnight:"]
        for o in plan["out_of_period"][:15]:
            head.append(f"  {o['received']}  {o['contractor']:<22} {o['subject'][:45]}")
            head.append(f"  {'':<12}{'':<22} {o.get('reason', '')}"
                        + (f"; states {o['stated']}" if o.get("stated") else ""))

    if plan["missing"]:
        head += ["", f"NOT SENT ANYTHING ({len(plan['missing'])}): "
                     + ", ".join(plan["missing"])]
    if plan["unmatched"]:
        head += ["", f"UNMATCHED ({len(plan['unmatched'])}) - no address on file and "
                     "no name on the message matches anyone on the roster:"]
        for u in plan["unmatched"][:15]:
            head.append(f"  {u['sender']:<40} {str(u['subject'])[:44]}")
            head.append(f"  {'':<40} {u.get('reason', '')}")

    if gaps_:
        head += ["", f"SET UP BUT CANNOT BE BILLED ({len(gaps_)}) - active inventory "
                     "item, no payroll record and no repeating bill. No draft is "
                     "raised for these people and nobody is chased for a timesheet:"]
        for g_ in gaps_:
            head.append(f"  {g_['item_code']:<24} {g_['name']}")

    if dry_run:
        head += ["", "Nothing was written. Re-run with dry_run=False to file these."]
        if plan["missing"]:
            head.append("Before trusting NOT SENT ANYTHING: search the mailbox by "
                        "surname. A matched sender with no attachment now shows "
                        "above, but an UNMATCHED address never will.")
        return "\n".join(head)

    written, skipped, failed = [], [], []
    # A page that arrives twice is filed twice. The second copy comes back
    # inside a reply that quotes the original mail - a real attachment on a real
    # message, given its own part number, indistinguishable by name from a page
    # nobody has seen. Compared on bytes before writing, so the repeat is never
    # created rather than cleaned up afterwards. Same-length files only; at most
    # one folder listing per contractor.
    repeats: list[tuple[str, str]] = []
    seen_bytes: dict[str, list[tuple[int, bytes, str]]] = {}
    listings: dict[str, list[dict]] = {}
    for f in plan["files"]:
        try:
            if g.exists(f["path"]):
                skipped.append(f["path"])
                continue
            blob = g.attachment_bytes(f["message_id"], f["attachment_id"])

            folder = f["path"].rsplit("/", 1)[0]
            twin = ""
            for size, other, where in seen_bytes.get(folder, []):
                if size == len(blob) and other == blob:
                    twin = where
                    break
            if not twin:
                if folder not in listings:
                    try:
                        listings[folder] = g.list_children(
                            folder, root="Contractors/Timesheets")
                    except Exception:                         # noqa: BLE001
                        listings[folder] = []
                twin = g.find_identical(folder, blob,
                                        root="Contractors/Timesheets",
                                        listing=listings[folder])
            if twin:
                repeats.append((f["path"], twin.rsplit("/", 1)[-1]))
                continue

            g.upload(f["path"], blob)
            seen_bytes.setdefault(folder, []).append(
                (len(blob), blob, f["path"]))
            written.append(f["path"])
        except Exception as e:                                # noqa: BLE001
            failed.append(f"{f['path']}: {e}")

    for b in plan.get("body_only", []):
        try:
            if g.exists(b["path"]):
                skipped.append(b["path"])
                continue
            g.upload(b["path"], g.message_mime(b["message_id"]))
            written.append(b["path"])
        except Exception as e:                                # noqa: BLE001
            failed.append(f"{b['path']}: {e}")

    head += ["", f"Written: {len(written)}   Already there: {len(skipped)}   "
                 f"Repeats: {len(repeats)}   Failed: {len(failed)}"]
    if repeats:
        head.append(f"NOT FILED ({len(repeats)}) - byte-for-byte the same as a "
                    "page already in the folder, almost always the original "
                    "image quoted back inside a reply:")
        for path, twin in repeats:
            head.append(f"  {path.rsplit('/', 1)[-1]:<34} same as {twin}")
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

    _, items, sales, bills, _ = _load(fy)
    out = [f"Xero tracking categories, in order: {cats or '(none returned)'}"]
    if note:
        out.append(note)
    if mappers.TRACKING_ORDER_OVERRIDE:
        out.append(f"TCG_TRACKING_ORDER override in force: "
                   f"{mappers.TRACKING_ORDER_OVERRIDE}")
    out.append(f"Payroll-tax category matched on name: "
               f"{mappers.PAYROLL_TAX_CATEGORY!r}")

    # The exemption list is a list of PEOPLE and the matching happens on ITEM
    # CODE. A name that resolves to no item falls back to matching the line
    # description exactly, which is what missed $95,876.29 in July FY27 - so
    # every unresolved name is printed, not counted.
    names, explicit = _payroll_tax_config()
    exempt_codes, unresolved = _payroll_tax_exempt_codes(items)
    out.append(f"Payroll-tax exemptions: {len(names)} name(s) + "
               f"{len(explicit)} explicit code(s) -> {len(exempt_codes)} item "
               "code(s) exempt")
    if unresolved:
        out.append(f"  NO ITEM CODE FOUND for {len(unresolved)} exempt name(s) - "
                   "these still match on description only, which misses any "
                   "line carrying a role title or a PO number. Add them to "
                   '"item_codes" in config/no_payroll_tax.json:')
        for n in sorted(unresolved):
            out.append(f"    - {n}")
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
def find_documents(contact: str, kind: str = "bills", months: int = 12,
                   statuses: str = "", lines: bool = True, limit: int = 40) -> str:
    """Every invoice or bill for one contact, whatever its status. Read-only.

    This is the plain "what have we done with this supplier before" lookup, and
    it exists because nothing else could answer it. `get_contractor_ledger` is
    keyed on item code and line description, so anybody billed without an
    inventory item - Matt O'Meara, the cleaner, anything coded straight to an
    expense account - was invisible to it. That is not a small gap: the way you
    handle someone this fortnight should follow what you did last fortnight, and
    you cannot copy what you cannot see.

    CONTACT is matched as a case-insensitive substring of the contact name.
    KIND: 'bills' (ACCPAY) | 'sales' (ACCREC) | 'both'.
    STATUSES: comma separated, e.g. "AUTHORISED,PAID". Blank means every status.
    """
    c = client()
    today = date.today()
    start = date(today.year - (months // 12 + 1), today.month, 1).isoformat()
    want = [s.strip().upper() for s in statuses.split(",") if s.strip()] or \
        ["DRAFT", "SUBMITTED", "AUTHORISED", "PAID", "VOIDED"]
    kinds = {"bills": ["ACCPAY"], "sales": ["ACCREC"], "both": ["ACCPAY", "ACCREC"]}
    if kind not in kinds:
        return "kind must be 'bills', 'sales' or 'both'."

    needle = contact.strip().lower()
    found = []
    for k in kinds[kind]:
        for d in c.iter_invoices(k, start, today.isoformat(), statuses=want):
            name = str((d.get("Contact") or {}).get("Name", ""))
            if needle in name.lower():
                found.append((k, d))

    if not found:
        return (f"Nothing for a contact matching {contact!r} in the last "
                f"{months} months. Try fewer words, or months=36.")

    found.sort(key=lambda x: mappers.parse_xero_date(x[1].get("Date")) or date.min)
    found = found[-limit:]

    out = [f"{len(found)} document(s) for a contact matching {contact!r}", ""]
    rows = []
    for k, d in found:
        rows.append({
            "Date": mappers.parse_xero_date(d.get("Date")),
            "Type": "bill" if k == "ACCPAY" else "invoice",
            "Number": d.get("InvoiceNumber") or "",
            "Reference": d.get("Reference") or "",
            "Status": d.get("Status", ""),
            "Total": d.get("Total"),
            "Due": d.get("AmountDue"),
        })
    out.append(pd.DataFrame(rows).to_markdown(index=False))

    if lines:
        out += ["", "LINES:"]
        lrows = []
        for k, d in found:
            for li in d.get("LineItems") or []:
                lrows.append({
                    "Date": mappers.parse_xero_date(d.get("Date")),
                    "Number": d.get("InvoiceNumber") or "",
                    "Description": str(li.get("Description") or "")[:60],
                    "Qty": li.get("Quantity"),
                    "Unit": li.get("UnitAmount"),
                    "Amount": li.get("LineAmount"),
                    "Account": li.get("AccountCode") or "",
                    "Tax": li.get("TaxType") or "",
                })
        if lrows:
            out.append(pd.DataFrame(lrows).to_markdown(index=False))
    return "\n".join(out)


def _last_bill_for(c, needle: str, months: int = 24) -> dict | None:
    """The most recent BILL for a contact matching NEEDLE, whatever its status.

    This is the whole safety model for create_supplier_bill(). A supplier we
    have paid before gives us three things we would otherwise be guessing:
    the ContactID, the account code and the tax type. Andrew's standing rule -
    base a repair on what the LAST ISSUED document actually used, never a
    guess - applied to a document we are creating rather than fixing.
    """
    today = date.today()
    start = date(today.year - (months // 12 + 1), today.month, 1).isoformat()
    n = str(needle or "").strip().lower()
    if not n:
        return None
    hits = [d for d in c.iter_invoices(
        "ACCPAY", start, today.isoformat(),
        statuses=["DRAFT", "SUBMITTED", "AUTHORISED", "PAID", "VOIDED"])
        if n in str((d.get("Contact") or {}).get("Name", "")).lower()]
    if not hits:
        return None
    hits.sort(key=lambda d: mappers.parse_xero_date(d.get("Date")) or date.min)
    return hits[-1]


@mcp.tool()
def create_supplier_bill(contact: str, number: str, total: float,
                         date_issued: str = "", due_date: str = "",
                         description: str = "", dry_run: bool = True) -> str:
    """Raise a bill for a NON-CONTRACTOR supplier straight into Awaiting Approval.

    WHY THIS EXISTS. The sweep only ever knew contractors, so SEEK, Equifax and
    the MYOB bills landed in UNMATCHED every fortnight and no bill was ever
    raised for them. SEEK invoice 702078071 - $1,173.15, due 14 September 2026 -
    sat in the payroll mailbox from 31 August and was in Xero nowhere at all.
    Nobody was late; it simply was not there.

    THE SAFETY MODEL IS THE SUPPLIER'S OWN HISTORY, NOT A PATTERN. Andrew,
    5 September 2026: SEEK "isn't regular, it's just been regular recently" -
    its bills run from $56 to $3,795 - so nothing here checks the amount against
    what came before, and there is no sanity band to lean on. What IS stable is
    the CODING: every SEEK bill is account 400 / INPUT, every Equifax bill is
    470 / INPUT. So the account code and tax type are COPIED FROM THE MOST
    RECENT BILL for that contact, and the amount is read from the email. A
    supplier with no prior bill is refused outright - an unknown payee is not
    something to code by guesswork.

    STATUS IS SUBMITTED - AWAITING APPROVAL, NOT DRAFT. Andrew's call: "that's
    where I check it in Xero." Drafts is the to-do list of things that did not
    come through; Awaiting Approval is the queue he actually reads. Landing
    there means the bill is one click from being paid, which is exactly why the
    duplicate check below is not tidiness.

    A DUPLICATE NUMBER IS REFUSED. The same invoice number already on that
    contact, in any status, stops the write. Paying a supplier twice is the
    failure this prevents, and it is one approval away.

    A STATEMENT IS NOT AN INVOICE. SEEK sends both. The statement summarises
    invoices already raised, so a bill made from one double-counts every line
    on it. Only ever pass an invoice number and an invoice total.

    DATE_ISSUED and DUE_DATE are YYYY-MM-DD; both default sensibly if blank.
    TOTAL is the amount as it appears on the invoice, GST inclusive or not
    exactly as the prior bills were keyed - the tax type carried over decides
    how Xero reads it.
    """
    c = client()
    num = str(number or "").strip()
    if not num:
        return ("A bill needs the supplier's own invoice number. It is the only "
                "thing that makes a duplicate visible later.")
    try:
        amount = round(float(total), 2)
    except (TypeError, ValueError):
        return f"total must be a number, not {total!r}."
    if amount <= 0:
        return "total must be greater than zero."

    prior = _last_bill_for(c, contact)
    if not prior:
        return (f"No bill has ever been raised for a contact matching "
                f"{contact!r}. This tool only creates for a supplier already on "
                f"file, because the account code and tax type are copied from "
                f"their last bill rather than guessed. Set the first one up by "
                f"hand in Xero, and this will handle every one after it.")

    pc = prior.get("Contact") or {}
    contact_id, contact_name = pc.get("ContactID"), pc.get("Name", "")
    lines = prior.get("LineItems") or []
    account = str((lines[0] if lines else {}).get("AccountCode") or "").strip()
    tax = str((lines[0] if lines else {}).get("TaxType") or "").strip()
    prior_desc = str((lines[0] if lines else {}).get("Description") or "").strip()
    if not account:
        return (f"The last bill for {contact_name} carries no account code, so "
                f"there is nothing to copy. Code this one by hand and the next "
                f"will follow it.")

    seen = {str(d.get("InvoiceNumber") or "").strip().lower()
            for d in c.iter_invoices(
                "ACCPAY", (date.today() - timedelta(days=900)).isoformat(),
                date.today().isoformat(),
                statuses=["DRAFT", "SUBMITTED", "AUTHORISED", "PAID", "VOIDED"])
            if str((d.get("Contact") or {}).get("Name", "")).lower()
            == str(contact_name).lower()}
    if num.lower() in seen:
        return (f"REFUSED - {contact_name} already has a bill numbered {num}. "
                f"Nothing was written. If this really is a second invoice that "
                f"reuses a number, raise it by hand so the decision is a "
                f"person's.")

    issued = str(date_issued or "").strip() or date.today().isoformat()
    due = str(due_date or "").strip() or (
        date.fromisoformat(issued) + timedelta(days=14)).isoformat()
    desc = str(description or "").strip() or prior_desc or contact_name

    plan = [
        f"Supplier bill for {contact_name}",
        "",
        pd.DataFrame([{
            "Number": num, "Date": issued, "Due": due, "Total": amount,
            "Account": account, "Tax": tax, "Description": desc[:50],
            "Status": "SUBMITTED (Awaiting Approval)",
        }]).to_markdown(index=False),
        "",
        f"Coding copied from their last bill, {prior.get('InvoiceNumber') or '(no number)'} "
        f"dated {mappers.parse_xero_date(prior.get('Date'))}.",
    ]
    if dry_run:
        plan.append("")
        plan.append("DRY RUN - nothing written. Re-run with dry_run=False.")
        return "\n".join(plan)

    res = writes.create_draft_invoice(
        c, contact_id,
        [{"Description": desc, "Quantity": 1, "UnitAmount": amount,
          "AccountCode": account, "TaxType": tax}],
        issued, due, reference="", invoice_type="ACCPAY",
        status="SUBMITTED", number=num,
    )
    made = (res.get("Invoices") or [{}])[0]
    plan.append("")
    plan.append(f"WRITTEN. {contact_name} {num}, ${amount:,.2f}, Awaiting "
                f"Approval. InvoiceID {made.get('InvoiceID', '')}")
    plan.append("Approving and paying it is yours - nothing here does that.")
    return "\n".join(plan)


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


@mcp.tool()
def inventory_coverage(period_end: str, window_days: int = 10) -> str:
    """Did the Phase 5 inventory adjustment actually get posted for everybody?

    READ-ONLY. Nothing is written and there is nothing to dry-run.

    Phase 5 of the fortnightly run moves each TRACKED contractor's wage cost out
    of 477 into 630 so the sales invoice can consume it. Xero has no API for it,
    so it is a manual item-by-item loop in Products and services - and a manual
    loop with no completion check stops wherever it stops and leaves no trace.
    For the fortnight ending 30 August 2026 it ran once and stopped: DL posted,
    DTL, KBJ and EK not. $24,670 of wage cost sat in the wrong account for a
    week and three invoices sat in Awaiting Approval against zero stock. A human
    found it, a week late, by noticing that one item had quantity and three had
    zero. This is that comparison, done every time instead of once by luck.

    It reads every sales line in the window, keeps the ones whose item is
    tracked, and compares each item's QuantityOnHand to the days billed on the
    invoices that have NOT yet been approved.

      OK        quantity on hand covers what is pending
      SHORT n   n less than billed. THE ADJUSTMENT WAS NOT POSTED.
      OVER n    more stock than billed. Usually a duplicate adjustment.
      NEGATIVE  below zero. An invoice was already approved against stock that
                was never there. Loudest case.
      APPROVED  the only invoice for it is approved, so the stock it billed has
                already been consumed and a zero balance is correct.

    ONLY DRAFT AND SUBMITTED QUANTITIES ARE DEMAND, and that is the whole trick.
    Approving a sales invoice makes Xero take the stock straight back out, so a
    correctly adjusted item reads zero afterwards and is indistinguishable from
    one that was never adjusted. Counting approved quantities as demand reported
    Bhasker Veela SHORT 21 against an item in perfect order on 3 September 2026.
    Approved documents are still read - a NEGATIVE balance means an approval
    already went through against nothing - but their quantity is shown in its
    own column instead of being weighed against what is left.

    UNTRACKED ITEMS DO NOT APPEAR AT ALL. They need no adjustment, ever, and
    listing them as OK buries the ones that matter. Jerry Gonsalves and Mazher
    Ali were created untracked in August and are the model: from 25 August 2026
    TCG creates no new tracked contractor items, so this report shrinks every
    time somebody finishes.

    Matching is on ITEM CODE only. One human arrives as "Dat Le", "Dat Tien Le"
    and "Le, Dat"; matching on a name once split one contractor into two and
    moved $60,000.
    """
    c = client()
    end = date.fromisoformat(period_end)
    lo = (end - timedelta(days=window_days)).isoformat()
    hi = (end + timedelta(days=window_days)).isoformat()

    docs = list(c.iter_invoices("ACCREC", lo, hi,
                                statuses=["DRAFT", "SUBMITTED", "AUTHORISED"]))
    # NOT _stage("items", ...). QuantityOnHand is the one number this tool
    # exists to read, the cache holds it for 15 minutes, and the whole workflow
    # is "post the adjustment by hand in Xero, then run this to confirm". Off
    # the cache that confirmation returns the PRE-adjustment snapshot, tells the
    # operator the adjustment is still missing, and the next line of advice is
    # require_stock=False - which switches the guard off over a stale read. One
    # extra Xero call is the right price.
    items = c.items()
    rows = coverage.plan_coverage(docs, items, _ignored_item_codes())

    out = [f"Tracked-inventory coverage, sales invoices dated {lo} to {hi}",
           f"{len(docs)} sales document(s) read.", ""]
    if not rows:
        out.append("No tracked item was billed in this window. Nothing to "
                   "adjust - and if that is a surprise, the invoices are not "
                   "filled yet: run list_period_drafts.")
        return "\n".join(out)

    faults = [r for r in rows if r["_fault"]]
    if faults:
        out += ["*** ADJUSTMENT MISSING - READ THIS ***",
                "Post these by hand in Xero: Business > Products and services >"
                " click the item > New inventory adjustment > Increase, dated "
                "the period end. There is no API for it.",
                "",
                pd.DataFrame([{k: v for k, v in r.items()
                               if not k.startswith("_")} for r in faults]
                             ).to_markdown(index=False),
                ""]

    out += [f"ALL TRACKED ITEMS BILLED ({len(rows)}):",
            pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")}
                          for r in rows]).to_markdown(index=False),
            ""]
    out.append(f"{len(faults)} needing an adjustment, "
               f"{len(rows) - len(faults)} clear.")
    if not faults:
        out.append("Every tracked item billed in this window has the stock "
                   "behind it. Phase 5 is complete.")
    return "\n".join(out)


def _cadence_of(item_code: str, side: str = "") -> tuple[bool, int | None]:
    """(is_monthly, period_day) for an item code, from roster_overrides.json -
    no network, and cadence lives nowhere else.

    SIDE is 'sales', 'bills', or '' for "monthly on either side". It matters
    only for somebody whose two cycles differ; see _cadence_value.

    Default is fortnightly, which is what everybody not listed is.

    period_day is set only where the monthly cycle is OFFSET from the calendar
    month. Prasanthi's is 12 - the 12th of the preceding month to the 11th of
    the current one. None means calendar month, and the connector does not
    write a reference for those: Deepti's house format is a month label,
    "Deepti Bansal May 2026", not a range.
    """
    try:
        # load_overrides() already returns the by_item_code map, not the file.
        by_code = roster.load_overrides() or {}
    except Exception:                                              # noqa: BLE001
        return False, None
    entry = by_code.get(str(item_code or "").strip()) or {}
    monthly = _cadence_value(entry, side) == "monthly"
    day = entry.get("period_day")
    try:
        day = int(day) if day is not None else None
    except (TypeError, ValueError):
        day = None
    return monthly, (day if monthly else None)


def _is_monthly(item_code: str) -> bool:
    """Back-compatible shorthand."""
    return _cadence_of(item_code)[0]


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
    if not wanted and not str(bill_numbers or "").strip():
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
    monthly_left: list[dict] = []
    tax_rows: list[dict] = []

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

        tax_rows += writes.tax_basis_problems(
            [d for d in docs
             if any((li.get("ItemCode") or "").strip() in wanted
                    for li in (d.get("LineItems") or []))], label,
            inclusive_ok=_tax_inclusive_ok())
        p_, s_, to_write = writes.plan_line_fill(docs, wanted, stamp, label)
        planned += p_
        skipped += s_

        # The contractor's own invoice number, on their bill only. A sales
        # invoice number belongs to TCG and is never rewritten.
        num_changes = writes.plan_number_change(docs, numbers) if (
            kind == "ACCPAY" and numbers) else []
        renumbered += num_changes
        new_num = {n["InvoiceID"]: n["Now"] for n in num_changes}

        # A bill matched by SUPPLIER NAME rather than item code is a cleaning or
        # subscription bill - no inventory item, no timesheet. Those suppliers
        # reconcile on their own reference, not on TCG's, so their invoice
        # number goes in the Reference field as well as the number field.
        # Andrew, 4 September 2026, on That's Sparkling Clean: "that's how that
        # company works out who's paid and who hasn't."
        bill_refs = [
            {"InvoiceID": n["InvoiceID"], "Contact": n["Contact"],
             "Was": "(none)", "Now": n["Now"]}
            for n in num_changes if n.get("ByContact")
        ] if kind == "ACCPAY" else []

        # The period reference is TCG's, so it goes on sales invoices only, and
        # on every draft for somebody in THIS period's list - not only the ones
        # this pass happens to be filling. Filling and referencing are separate
        # runs across a billing week: an invoice filled on Wednesday must still
        # get its reference on Friday. Scoped by item code, so an untouched
        # draft - a leaver's, the empty expenses one - keeps whatever it had.
        # The auto reference is a FORTNIGHT range - "17 August to 30 August
        # 2026". It must never land on a monthly contractor. Prasanthi's cycle
        # runs the 12th to the 11th, so a fortnight range on her invoice is
        # wrong twice over: wrong length, and wrong dates. Deepti's house
        # reference is "Deepti Bansal May 2026", a month label, not a range.
        # Monthly people are left with whatever their template generated and
        # the reference is REPORTED instead, so a broken one is visible.
        in_scope, monthly_refs, monthly_ref = set(), [], {}
        for d in docs:
            codes = [(li.get("ItemCode") or "").strip()
                     for li in (d.get("LineItems") or [])]
            hit = [c for c in codes if c in wanted]
            if not hit:
                continue
            # The reference being decided here is a SALES reference, so a
            # split-cadence person is judged on their sales cycle. Peter Small
            # is fortnightly on the bill and monthly to the client; stamping a
            # fortnight range on his monthly invoice is wrong twice over -
            # wrong length and wrong dates.
            cad = [_cadence_of(c, "sales") for c in hit]
            if any(m for m, _ in cad):
                # An OFFSET monthly cycle can be written exactly - Prasanthi's
                # 12th-to-11th window is a date range in the same house format
                # as the fortnightly one. A calendar-month person is left
                # alone: their house reference is a month label, not a range.
                days = {dd for m, dd in cad if m and dd}
                if len(days) == 1 and all(m for m, _ in cad):
                    monthly_ref[d["InvoiceID"]] = writes.monthly_reference(
                        end, days.pop())
                else:
                    monthly_refs.append({
                        "Doc": d.get("InvoiceNumber") or str(d.get("InvoiceID", ""))[:8],
                        "Contact": (d.get("Contact") or {}).get("Name", "?"),
                        "Item": ", ".join(hit),
                        "Reference now": str(d.get("Reference") or "(none)"),
                    })
                continue
            in_scope.add(d["InvoiceID"])
        ref_changes = writes.plan_reference_change(
            [d for d in docs if d.get("InvoiceID") in in_scope], ref
        ) if (kind == "ACCREC" and ref) else []
        if kind == "ACCREC" and ref:
            for d in docs:
                own = monthly_ref.get(d.get("InvoiceID"))
                if own:
                    ref_changes += writes.plan_reference_change([d], own)
        if kind == "ACCREC":
            monthly_left += monthly_refs
        rereferenced += ref_changes + bill_refs
        new_ref = {r["InvoiceID"]: r["Now"] for r in ref_changes + bill_refs}

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
    tax_bad = [{k: v for k, v in r.items() if k != "InvoiceID"} for r in tax_rows]

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
        out += ["", "REFERENCE set on:",
                pd.DataFrame([{k: v for k, v in r.items() if k != "InvoiceID"}
                              for r in rereferenced]).to_markdown(index=False)]
    if monthly_left:
        out += ["", "MONTHLY - REFERENCE NOT TOUCHED:",
                pd.DataFrame(monthly_left).to_markdown(index=False),
                "",
                "The auto reference is a fortnight range and these people are",
                "billed monthly, so it has been left alone. Check the reference",
                "above reads the way you send it - the house format for a",
                "monthly person is a month label, e.g. \"Deepti Bansal May 2026\"."]
    if renumbered:
        out += ["", "BILL NUMBERS set to the contractor's own:",
                pd.DataFrame([{k: v for k, v in n.items() if k != "InvoiceID"}
                              for n in renumbered]).to_markdown(index=False)]
    if skipped:
        # A line already carrying days is never overwritten. But a line carrying
        # a DIFFERENT number to the days you gave is not "already billed" - it
        # is a number nobody put there on purpose, and it goes out at that
        # number. Bhasker Veela's August sales invoice sat at 1 day against 21
        # worked because the repeating template generated at 1; the fill left it
        # alone, said "already billed", and ~$6,573 got one approval from going
        # out the door. Mismatches lead, on their own, above the benign ones.
        mism = [r for r in skipped if r.get("Mismatch")]
        benign = [r for r in skipped if not r.get("Mismatch")]
        if mism:
            out += ["", "*** QUANTITY MISMATCH - READ THIS ***",
                    pd.DataFrame([{k: v for k, v in r.items() if k != "Mismatch"}
                                  for r in mism]).to_markdown(index=False),
                    "",
                    "These lines already carry a quantity that is NOT the days",
                    "you gave, so the fill has left them alone and they will",
                    "invoice at the number shown. Usually a repeating template",
                    "generating at something other than zero - check it with",
                    "list_repeating_templates() and fix it with",
                    "set_repeating_quantity('<item code>', 0). Correct the",
                    "current draft in Xero by hand; nothing here overwrites a",
                    "line somebody may have set deliberately."]
        if benign:
            out += ["", "LEFT ALONE (already at the days you gave):",
                    pd.DataFrame([{k: v for k, v in r.items() if k != "Mismatch"}
                                  for r in benign]).to_markdown(index=False)]
    if tax_bad:
        out += ["", "*** TAX BASIS - READ THIS ***",
                pd.DataFrame(tax_bad).to_markdown(index=False),
                "",
                "Every TCG document is TAX EXCLUSIVE. Rates are quoted and",
                "contracted ex GST and the tax goes on top. An INCLUSIVE",
                "document computes the GST out of the rate instead: $1,000/day",
                "bills $909.09 + $90.91 rather than $1,000 + $100, so the client",
                "is short-invoiced, the contractor is not, and it takes a credit",
                "note to unwind once it has been approved.",
                "",
                "Fix it in Xero on the invoice - the Amounts are dropdown at the",
                "top right of the line table - and on the REPEATING TEMPLATE",
                "behind it, or the next period generates the same fault.",
                "submit_period_invoices will not move an Inclusive document."]
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
      - DRAFT, SUBMITTED and AUTHORISED - a late timesheet must still reach an
        invoice that has already been sent. PAID and VOIDED are left alone.
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

    # From the LIVE roster, not config/contractor_mail.json. sweep_timesheets
    # files into folders derived from Xero; this step mapped those folders back
    # through the retired hand-maintained JSON, whose own header says nothing
    # reads it and it is safe to delete. Delete it and every attachment silently
    # reports "folder does not map to a contractor" and nothing is attached;
    # keep it and anyone set up in Xero but never added to it has their
    # timesheet swept, filed, and then never attached - the Jerry Gonsalves and
    # Mazher Ali drift that roster.py exists to end, reintroduced one step later.
    # The JSON is still merged underneath so an entry only it knows about is not
    # lost, but Xero wins on any folder both describe.
    folder_to_code = {ct["folder"]: ct["item_code"] for ct in mmap.load_contractors()}
    try:
        folder_to_code.update({r["folder"]: r["item_code"]
                               for r in _live_roster()[0]
                               if r.get("folder") and r.get("item_code")})
    except Exception as e:                                         # noqa: BLE001
        log.warning("Could not build the folder map from the live roster (%s). "
                    "Falling back to config/contractor_mail.json, which is "
                    "retired and may be missing recent starters.", e)

    # Not only drafts. A timesheet that turns up after the invoice has gone out
    # still belongs on it - Andrew's rule is that the invoice does not wait for
    # the evidence, so the evidence has to be able to catch up. Attaching to an
    # authorised invoice changes no figure and does not resend it.
    live = ["DRAFT", "SUBMITTED", "AUTHORISED"]
    sales, bills = {}, {}
    for kind, bucket in (("ACCREC", sales), ("ACCPAY", bills)):
        for d in c.iter_invoices(kind, lo, hi, statuses=live):
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
                           window_days: int = 10, kinds: str = "both",
                           require_attachment: bool = False,
                           require_stock: bool = True,
                           cadence: str = "fortnightly",
                           exclude: str = "") -> str:
    """Move the finished sales invoices from Draft to Awaiting Approval.

    The point is that Drafts becomes a to-do list. An invoice that is filled and
    has its timesheet attached moves out by itself; anything still sitting in
    Drafts on Saturday is something that did not come through - a contractor who
    never sent a timesheet, an expenses invoice with nothing to pass through, a
    leaver whose template is still firing.

    An invoice is submitted when EVERY LINE HAS A QUANTITY. An invoice billing
    nothing is not an invoice, and that test never bends.

    A MISSING DOCUMENT NO LONGER HOLDS IT BACK. A contractor being late with a
    timesheet should not delay the client's invoice - the client can see the
    hours in their own system and rarely asks. The timesheet is attached when it
    arrives, to whatever the invoice has become by then. Anything going out
    without evidence is named in the report so it stays visible.

    REQUIRE_ATTACHMENT=True restores the stricter rule.

    NO STOCK HOLDS IT BACK - new 3 September 2026. A sales line carrying a
    TRACKED item whose QuantityOnHand is below the line quantity means the
    manual Phase 5 inventory adjustment was never posted. Approving it drives
    the item negative and throws the cost of sales onto the wrong side. Three
    invoices reached Awaiting Approval that way for the fortnight ending
    30 August and nothing noticed for a week. Submitting is where an invoice
    leaves the to-do list, so this is the last automated point the fault can be
    caught. REQUIRE_STOCK=False overrides it for one run.

    CADENCE defaults to 'fortnightly' - also new, and it is a behaviour change.
    A monthly document dated inside the fortnight's window used to be swept up
    with the fortnightly ones; TCG-21205 (Bhasker Veela) went out that way on
    2 September 2026 and had to be pulled back to Draft by hand. Monthly
    documents are now held, named, and told how to include them. The MONTHLY run
    passes cadence='monthly'; cadence='all' restores the old behaviour. Nothing
    is lost either way - a held document stays a Draft.

    EXCLUDE is the manual escape hatch: comma-separated text matched against the
    invoice number, contact name, reference or item code.

    KINDS: 'both' | 'sales' | 'bills'. Bills move too, for the same reason - a
    bill with no supplier invoice behind it is one that has not been checked.
    The stock guard applies to SALES ONLY: a bill is what creates the cost, and
    it is the sales invoice that consumes the stock.

    Approving, paying and sending all remain Andrew's; this only moves
    DRAFT -> SUBMITTED, and he can move it back.
    """
    c = client()
    end = date.fromisoformat(period_end)
    lo = (end - timedelta(days=window_days)).isoformat()
    hi = (end + timedelta(days=window_days)).isoformat()

    types = {"both": [("ACCREC", "invoice"), ("ACCPAY", "bill")],
             "sales": [("ACCREC", "invoice")], "bills": [("ACCPAY", "bill")]}
    if kinds not in types:
        return "kinds must be 'both', 'sales' or 'bills'."
    if (cadence or "").strip().lower() not in ("all", "fortnightly", "monthly"):
        return "cadence must be 'fortnightly', 'monthly' or 'all'."

    inclusive_ok = _tax_inclusive_ok()
    # Live items, for the same reason inventory_coverage does not use the cache.
    stock = coverage.stock_index(c.items()) if require_stock else {}
    # Everything already in Awaiting Approval is going to eat this stock the
    # moment it is approved - Xero only decrements on approval - so its demand
    # is subtracted before any new draft is measured against what is left.
    reserved = (list(c.iter_invoices("ACCREC", lo, hi, statuses=["SUBMITTED"]))
                if require_stock else [])
    monthly_codes = {"sales": _monthly_item_codes("sales"),
                     "bills": _monthly_item_codes("bills")}
    patterns = [p for p in (exclude or "").split(",") if p.strip()]

    ready, held, skipped = [], [], []
    for kind, label in types[kinds]:
        docs = list(c.iter_invoices(kind, lo, hi, statuses=["DRAFT"]))
        if not docs:
            continue
        docs, out_of_run = writes.split_excluded(docs, patterns, monthly_codes,
                                                 cadence)
        for row in out_of_run:
            row["Kind"] = label
        skipped += out_of_run
        if not docs:
            continue
        att = {d["InvoiceID"]: writes.existing_attachments(c, d["InvoiceID"])
               for d in docs}
        r, h = writes.plan_submission(
            docs, att, require_attachment,
            stock=stock if kind == "ACCREC" else None,
            require_stock=require_stock,
            reserved=reserved if kind == "ACCREC" else None,
            inclusive_ok=inclusive_ok, kind=kind)
        for row in r:
            row["Kind"] = label
        for row in h:
            row["Kind"] = label
        ready += r
        held += h

    if not ready and not held and not skipped:
        return f"No draft documents dated {lo} to {hi}."

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

    tax_held = [h for h in held if "TAX INCLUSIVE" in str(h.get("Why"))]
    stock_held = [h for h in held if "INSUFFICIENT STOCK" in str(h.get("Why"))]
    out = [f"Fortnight ending {end.isoformat()}",
           "DRY RUN - nothing moved." if dry_run else
           "MOVED to Awaiting Approval. Approving and sending is still yours.",
           ""]
    bare = [r for r in ready if r.get("Evidence") == "NONE YET"]
    if ready:
        out += [f"READY ({len(ready)}):",
                pd.DataFrame([{k: v for k, v in r.items() if k != "InvoiceID"}
                              for r in ready]).to_markdown(index=False)]
    else:
        out.append("Nothing is ready to submit.")
    if bare:
        out += ["", f"GOING OUT WITH NO DOCUMENT ATTACHED ({len(bare)}) - "
                "attach it when it arrives:",
                pd.DataFrame([{k: v for k, v in b.items()
                               if k in ("Kind", "Doc", "Contact")} for b in bare]
                             ).to_markdown(index=False)]
    if held:
        out += ["", f"LEFT IN DRAFTS ({len(held)}) - these are the ones to look at:",
                pd.DataFrame([{k: v for k, v in h.items() if k != "InvoiceID"}
                              for h in held]).to_markdown(index=False)]
    if skipped:
        out += ["", f"NOT IN THIS RUN ({len(skipped)}) - still Drafts, nothing "
                "touched:",
                pd.DataFrame([{k: v for k, v in s.items() if k != "InvoiceID"}
                              for s in skipped]).to_markdown(index=False)]
    if errors:
        out += ["", "ERRORS:"] + [f"  {e}" for e in errors]
    if stock_held:
        out += ["", "*** HELD BECAUSE THE INVENTORY ADJUSTMENT IS MISSING ***",
                "",
                "These bill a TRACKED item that has less stock on hand than the",
                "invoice bills. That means Phase 5 never ran for them. Approved,",
                "the item goes negative and the cost of sales lands on the wrong",
                "side - $24,670 sat in the wrong account for a week after the",
                "fortnight ending 30 August 2026 for exactly this reason.",
                "",
                "Post the adjustment by hand: Xero > Business > Products and",
                "services > click the item > New inventory adjustment >",
                "Increase, dated the period end. There is no API for it.",
                "Then run inventory_coverage(period_end) to confirm, and this",
                "again to submit. require_stock=False overrides if you are sure."]
    if tax_held:
        out += ["", "*** HELD BECAUSE THE TAX BASIS IS WRONG ***",
                "",
                "Every TCG document is TAX EXCLUSIVE - the rate is ex GST and",
                "the tax goes on top. An INCLUSIVE document takes the GST OUT",
                "of the rate: a $1,000/day line bills $909.09 + $90.91 instead",
                "of $1,000 + $100. Approved and sent, that needs a credit note",
                "to unwind, so it does not move from here.",
                "",
                "In Xero, open the invoice, set the Amounts are dropdown above",
                "the line table to Tax Exclusive, save - then fix the REPEATING",
                "TEMPLATE behind it or next period generates the same fault.",
                "Re-run this once both are right."]
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
                         payroll_calendar_id: str = "",
                         dry_run: bool = True) -> str:
    """Create a DRAFT timesheet in Xero. It is not approved and does not pay
    anyone until you approve it in Xero yourself.

    DRY BY DEFAULT. A draft is still a thing that appears in Xero and has to be
    found and deleted when the name matched the wrong employee, so the match and
    the units are shown first. Pass dry_run=False to create it.

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

    if dry_run:
        return ("DRY RUN - nothing created. Re-run with dry_run=False.\n"
                f"Employee matched: {emp.get('FirstName')} {emp.get('LastName')} "
                f"({emp.get('EmployeeID')})\n"
                f"Period: {period_start} to {period_end} ({len(span)} days)\n"
                f"Units: {sum(units):g} across "
                f"{len([u for u in units if u])} worked day(s)\n"
                + "\n".join(f"  {d}: {u:g}" for d, u in zip(span, units) if u))

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
    out += ["", "Earnings rates  (GL account each one posts wages to):"]
    for x in rates:
        acct = (x.get("AccountCode") or x.get("accountCode")
                or x.get("ExpenseAccountCode") or "")
        out.append(
            f"  {x.get('Name') or x.get('name')} "
            f"[{x.get('RateType') or x.get('EarningsType') or ''}] "
            f"account={acct or 'NOT SET'} -> "
            f"{x.get('EarningsRateID') or x.get('earningsRateID')}")

    # Everything the rate carries, once, so a field named differently than
    # expected is visible rather than silently reported as NOT SET.
    if rates:
        out += ["", f"Fields on an earnings rate: {sorted(rates[0].keys())}"]
    return "\n".join(out)


@mcp.tool()
def post_pay_period(days_worked: str, period_start: str, period_end: str,
                    payroll_calendar_id: str, earnings_rate_id: str,
                    invoice_date: str = "", invoice_due_date: str = "",
                    dry_run: bool = True) -> str:
    """Create BOTH sides of a pay period in Xero as drafts, from one set of days:
    the payroll timesheets AND the matching sales invoice lines. Because both
    come from the same days figure, payroll and the invoice cannot diverge.

    DRY BY DEFAULT. This is the largest write in the connector - a timesheet per
    person, in one pass, with no undo but deleting each one by hand. Pass
    dry_run=False once the figures below read right.

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
        if not dry_run:
            writes.create_draft_timesheet(
                c, emp["EmployeeID"], period_start, period_end,
                earnings_rate_id, units)
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

    out = [("DRY RUN - nothing created. Re-run with dry_run=False.\n"
            f"WOULD create {len(ts_done)} draft timesheet(s):") if dry_run
           else f"DRAFT timesheets created: {len(ts_done)}", *log_lines, ""]
    out.append("Draft invoice lines prepared (one invoice per customer still needs "
               "a contact ID - run create_draft_invoice per customer, or tell me the "
               "customer and I will do it):")
    for l in inv_lines:
        out.append(f"  {l['ItemCode']}: {l['Quantity']:g} x ${l['UnitAmount']:,.2f} "
                   f"= ${l['Quantity']*l['UnitAmount']:,.2f}")
    if dry_run:
        out += ["", "DRY RUN - nothing was written to Xero."]
    else:
        out += ["", "ALL DRAFTS. Nothing approved, sent or paid.",
                "Review: Xero > Payroll > Timesheets, and Business > Invoices > Draft."]
        _cache.clear()
    return "\n".join(out)


@mcp.tool()
def set_rate_card(item_code: str, cost_rate: float = None,
                  sell_rate: float = None, dry_run: bool = True) -> str:
    """Update a contractor's cost and/or sell rate on the Xero item rate card.

    DRY BY DEFAULT, like every other write here. It was the odd one out: a
    mistyped rate went straight into Xero and then onto every invoice and bill
    the item touches from that moment. Pass dry_run=False to write.

    An item that is TRACKED and has ever been invoiced cannot be changed through
    the API at all - Xero locks those to the web UI. The dry run says so before
    you find out the hard way.
    """
    c = client()
    wanted = (item_code or "").strip().lower()
    existing = next((i for i in c.items()
                     if (i.get("Code") or "").strip().lower() == wanted), None)
    if existing is None:
        return (f"No Xero item with code {item_code!r}. Check get_rate_card for "
                "the exact code - they are case and space sensitive.")
    if cost_rate is None and sell_rate is None:
        return "Give at least one of cost_rate or sell_rate."

    now_cost = (existing.get("PurchaseDetails") or {}).get("UnitPrice")
    now_sell = (existing.get("SalesDetails") or {}).get("UnitPrice")
    plan = [f"{existing['Code']} - {existing.get('Name') or ''}",
            f"  cost  {now_cost} -> "
            f"{cost_rate if cost_rate is not None else 'unchanged'}",
            f"  sell  {now_sell} -> "
            f"{sell_rate if sell_rate is not None else 'unchanged'}"]

    if dry_run:
        plan.insert(0, "DRY RUN - nothing written. Re-run with dry_run=False.")
        if existing.get("IsTrackedAsInventory"):
            plan += ["", "This item is TRACKED. If it has ever been invoiced, "
                     "Xero will refuse the write and it has to be changed by "
                     "hand: Business > Products and services > click the item > "
                     "edit the price > Save."]
        return "\n".join(plan)

    res = writes.update_item_rates(c, item_code, cost_rate, sell_rate)
    _cache.clear()
    return "\n".join(["WRITTEN to Xero.", *plan[0:],
                      f"Xero returned: {str(res)[:200]}"])


if __name__ == "__main__":
    mcp.run(transport="streamable-http" if TRANSPORT == "http" else "stdio")


@mcp.tool()
def onedrive_selftest() -> str:
    """Prove the file plumbing works before trusting anything built on it.

    Run this FIRST after deploying. graph_diagnostics proves the connection can
    READ; this proves it can WRITE and read back, which is the half that matters
    for signing a document. Writes one probe file to AI Working Folder and
    deletes it again.
    """
    lines = []
    try:
        g = _graph()
    except RuntimeError as e:
        return f"FAILED before connecting.\n{e}"

    probe = "AI Working Folder/_onedrive_selftest.txt"
    payload = b"onedrive selftest - safe to delete"

    try:
        drive = g._drive_id()
        lines.append(f"drive           OK ({drive[:24]}...)")
    except Exception as e:  # noqa: BLE001
        return "\n".join(lines + [f"drive           FAILED: {e}"])

    try:
        g.upload(probe, payload, root="")
        lines.append("write           OK")
    except Exception as e:  # noqa: BLE001
        return "\n".join(lines + [f"write           FAILED: {e}"])

    try:
        got = g.download(probe, root="")
        lines.append("read back       OK" if got == payload
                     else f"read back       FAILED: got {got[:40]!r}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"read back       FAILED: {e}")

    try:
        d = g.download_url(probe)
        lines.append(f"download url    OK ({d['size']} bytes)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"download url    FAILED: {e}")

    try:
        g.upload_url("AI Working Folder/_onedrive_selftest_session.bin")
        lines.append("upload session  OK")
    except Exception as e:  # noqa: BLE001
        lines.append(f"upload session  FAILED: {e}")

    try:
        from urllib.parse import quote as _q
        g._request("DELETE", f"https://graph.microsoft.com/v1.0/drives/"
                             f"{g._drive_id()}/root:/{_q(probe)}")
        lines.append("cleanup         OK")
    except Exception:  # noqa: BLE001
        lines.append(f"cleanup         left {probe} behind - delete it by hand")

    return "\n".join(lines)


@mcp.tool()
def onedrive_list(path: str = "", root: str = "", recursive: bool = False,
                  drive: str = "") -> str:
    """List a OneDrive or SharePoint folder. Read-only.

    PATH is relative to ROOT; ROOT defaults to the drive root, so
    "CONTRACTOR AGREEMENTS/Devinia Liddelow" works as written - the same form
    attach_from_onedrive takes. Blank lists the top level.

    DRIVE picks whose drive. Blank is Andrew's OneDrive, as it always was.
    "site" is the SharePoint team library; an email address is that person's
    OneDrive - matt@thecachegroup.com.au is where the interview transcripts
    live. Run onedrive_drives() to see what is reachable.

    Use this to find a contractor's own last brief before editing it, and to
    confirm a signed document actually landed where it was meant to.
    """
    g = _graph()
    try:
        items = g.list_children(path, root=root, recursive=recursive, drive=drive)
    except Exception as e:  # noqa: BLE001
        return f"Could not list {path or '(root)'}: {e}"

    where = f"{path or '(root)'}" + (f"  [{drive}]" if drive else "")
    if not items:
        return f"{where} is empty."

    rows = []
    for it in sorted(items, key=lambda i: ("folder" not in i, i.get("name", "").lower())):
        kind = "dir " if "folder" in it else "file"
        size = it.get("size") or 0
        rows.append(f"  {kind}  {size:>10}  {it.get('path') or it.get('name')}")
    return f"{where} - {len(items)} items\n" + "\n".join(rows)


@mcp.tool()
def onedrive_save_mail_attachment(message_id: str, dest_path: str,
                                  attachment_id: str = "",
                                  mailbox: str = "") -> str:
    """Copy an Outlook attachment straight into OneDrive. Bytes stay server-side.

    THIS IS THE ONE THAT UNBLOCKS UNATTENDED SIGNING. With no laptop awake there
    is no other route: the Claude M365 connector returns attachments as extracted
    text, and Outlook refuses to forward a message carrying one.

    DEST_PATH is relative to the drive root, e.g.
    "CONTRACTOR AGREEMENTS/Bhasker Veela/Consultancy Brief signed.pdf".

    ATTACHMENT_ID may be left blank when the message has exactly one file
    attachment; with more than one it refuses and lists them rather than
    guessing which document to countersign.

    MAILBOX defaults to the payroll mailbox. Reading another one needs that
    address inside the Application Access Policy's scope group - a 403 here
    means the policy, not the code.
    """
    g = _graph()
    try:
        r = g.save_mail_attachment(
            message_id=message_id,
            dest_path=dest_path,
            attachment_id=attachment_id or None,
            mailbox=mailbox or None,
        )
    except Exception as e:  # noqa: BLE001
        return f"FAILED: {e}"

    return (f"Saved {r['source_name']!r} ({r['bytes']} bytes) from "
            f"{r['source_mailbox']}\n  to: {r['saved_to']}\n"
            f"  {r['web_url']}\n\n"
            f"Attach it with send_email(attach_from_onedrive=['{r['saved_to']}']).")


# Code packages have ONE home, and it is not wherever the session that cut them
# happened to be working. Andrew works on this connector from several projects
# at once; before this, each one wrote its zip somewhere else and the answer to
# "what do I still have to upload?" was a hunt through five near-identically
# named files. The rule lived in his preferences and was still being missed,
# because a preference is advice and this needs to be a refusal.
PENDING_UPLOAD = "AI Working Folder/_PENDING UPLOAD"
DEPLOYED = "AI Working Folder/_deployed"


def _package_path_problem(path: str) -> str:
    """Why this zip must not be written here. Empty string means it is fine."""
    p = str(path or "").strip().strip("/")
    if not p.lower().endswith(".zip"):
        return ""
    folder, _, name = p.rpartition("/")
    if folder not in (PENDING_UPLOAD, DEPLOYED):
        return (f"A zip belongs in {PENDING_UPLOAD!r} while it is waiting to be "
                f"uploaded, and {DEPLOYED!r} once it is deployed - not "
                f"{folder or 'the drive root'!r}.\n"
                "That folder is the answer to 'what do I still have to "
                "upload?', and it only works while it is the ONLY place they "
                "go.\n"
                "For a zip that is not a code package - a bundle of documents - "
                "pass force=True.")
    if folder == PENDING_UPLOAD and "cut-against-" not in name:
        return (f"{name!r} does not say which commit it was cut against.\n"
                "An upload REPLACES whole files, so a package is only safe on "
                "the commit it was cut from, and the filename is the only thing "
                "that carries it:\n"
                "  <what-it-does>_cut-against-<sha>_YYYY-MM-DD_HHMM.zip\n"
                "Pass force=True if this genuinely is not a code package.")
    return ""


@mcp.tool()
def onedrive_transfer_url(path: str, direction: str = "download",
                          conflict: str = "replace", drive: str = "",
                          force: bool = False) -> str:
    """A pre-authenticated URL for reading or writing one OneDrive file.

    Returns a URL, never file content. The caller fetches or PUTs it directly,
    so a 5MB contract costs four lines here instead of seven million characters
    of base64 - and the document work (signing a PDF, editing the real .docx
    rather than rebuilding it) happens where the tooling for it is tested.

    DIRECTION: 'download' for an existing file, 'upload' for a new or
    replacement one. CONFLICT applies to uploads only: replace | rename | fail.
    Use 'fail' for anything already signed.

    Download URLs last about an hour, upload sessions about fifteen minutes.
    Neither needs an Authorization header - treat both as short-lived
    credentials and do not write them anywhere they persist.

    A ZIP HAS ONE HOME. Uploading one anywhere but
    `AI Working Folder/_PENDING UPLOAD` (waiting) or `AI Working Folder/_deployed`
    (done) is refused, and so is a name in _PENDING UPLOAD that does not say
    which commit it was cut against. Andrew works on this connector from several
    projects at once and each one used to write its package somewhere else, so
    nothing could answer "what do I still have to upload?". FORCE=True is the
    way past both, for a zip that is not a code package.
    """
    g = _graph()
    if direction not in ("download", "upload"):
        return f"direction must be 'download' or 'upload', not {direction!r}."

    if direction == "upload" and not force:
        problem = _package_path_problem(path)
        if problem:
            return f"REFUSED. Nothing has been written.\n{problem}"

    try:
        if direction == "download":
            d = g.download_url(path, drive=drive)
            return (f"{d['name']}  {d['size']} bytes  modified {d['last_modified']}\n"
                    f"{d['download_url']}\n\n"
                    "Fetch with: curl -sL '<url>' -o <file>   (no auth header)")

        u = g.upload_url(path, conflict=conflict, drive=drive)
        return (f"Upload session for {u['path']}, expires {u['expires']}\n"
                f"{u['upload_url']}\n\n"
                "PUT with, for a file of N bytes:\n"
                "  curl -s -X PUT '<url>' \\\n"
                "    -H 'Content-Length: N' \\\n"
                "    -H 'Content-Range: bytes 0-<N-1>/N' \\\n"
                "    --data-binary @file")
    except Exception as e:  # noqa: BLE001
        return f"FAILED: {e}"


@mcp.tool()
def pending_uploads() -> str:
    """What is still waiting for Andrew to upload to GitHub. Read only.

    Nobody but Andrew can push to the repo - three automated routes were tried
    on 5 September 2026 and all three are blocked - so every change ends as a
    zip he drags into GitHub by hand. The gap that creates is knowing which
    zips are still outstanding, and it was being answered by looking at a folder
    full of near-identically named files from four different projects.

    `AI Working Folder/_PENDING UPLOAD` is the single answer. It should normally
    be EMPTY. Anything in it is outstanding.

    A package is only safe to upload onto the commit it was cut from, because an
    upload replaces whole files - so the base commit is read back out of each
    name here. CHECK IT AGAINST THE REPO'S CURRENT HEAD BEFORE UPLOADING. If it
    does not match, stop: the package was cut against something older and will
    take other people's changes back out with it.

    A zip whose contents are already identical to HEAD has been deployed and
    never filed - move it to `_deployed` with onedrive_move rather than
    uploading it again.
    """
    import re as _re
    try:
        items = _graph().list_children(PENDING_UPLOAD)
    except Exception as e:  # noqa: BLE001
        return f"FAILED: {e}"

    files = [it for it in items if "folder" not in it]
    if not files:
        return (f"{PENDING_UPLOAD} is empty.\n"
                "Nothing is waiting to be uploaded.")

    out = [f"{len(files)} package(s) WAITING TO BE UPLOADED in {PENDING_UPLOAD}:",
           ""]
    for it in sorted(files, key=lambda x: str(x.get("name"))):
        name = str(it.get("name"))
        m = _re.search(r"cut-against-([0-9a-f]{7,40})", name)
        out.append(f"  {name}   ({it.get('size')} bytes)")
        if m:
            out.append(f"      cut against {m.group(1)} - confirm this is the "
                       "repo's HEAD before uploading")
        else:
            out.append("      NO BASE COMMIT IN THE NAME. Do not upload it "
                       "until you know which commit it was cut from.")
    out += ["",
            "Upload: GitHub - Add file - Upload files at the REPO ROOT, "
            "dragging the whole src and tests folders together.",
            "Then move the zip to _deployed with onedrive_move, and start a "
            "fresh chat if any tool was added or removed."]
    return "\n".join(out)


@mcp.tool()
def onedrive_drives() -> str:
    """Every drive this connector can reach. Read-only. Run it when a file
    cannot be found before concluding it is not there.

    The connector was hardcoded to one OneDrive until 5 September 2026, so
    files on the SharePoint team site and in other people's drives read as
    missing when they were simply somewhere nothing was looking. The TARGET
    column is what to pass as `drive` to onedrive_list, onedrive_transfer_url
    and onedrive_delete.
    """
    try:
        drives = _graph().list_drives()
    except Exception as e:  # noqa: BLE001
        return f"Could not list drives: {e}"
    if not drives:
        return "No drives reachable. That is itself a fault - check Graph credentials."

    rows = ["| Target | Kind | Name | Web URL |", "|:--|:--|:--|:--|"]
    for d in drives:
        rows.append(f"| `{d['target']}` | {d['kind']} | {d['name']} | {d['web_url']} |")

    # A partial answer is still an answer. Say plainly which part is missing and
    # what grants it, rather than leaving a 403 to be diagnosed a second time.
    bad = [d for d in drives if str(d.get("name", "")).startswith(
        ("UNREACHABLE:", "UNAVAILABLE:"))]
    good = len(drives) - len(bad)
    note = ""
    if any(d.get("target") == "-" for d in bad):
        note = ("\n\nOther people's drives could not be listed. Enumerating "
                "them reads the DIRECTORY, not files, so Files.ReadWrite.All "
                "does not cover it - the app registration also needs "
                "`User.Read.All` (application) with admin consent. Naming a "
                "drive directly still works without it, e.g. "
                "onedrive_list(path='', drive='matt@thecachegroup.com.au').")

    return (f"{good} drives reachable"
            + (f", {len(bad)} listing(s) failed" if bad else "")
            + "\n\n" + "\n".join(rows) + note +
            "\n\nPass the target as `drive`, e.g. "
            "onedrive_list(path='', drive='site').")


def _split_paths(raw) -> list[str]:
    """One path, several paths one per line, or a JSON array. All the same thing.

    Split on NEWLINES ONLY. A comma is a legal character in a OneDrive filename
    and splitting on it would quietly cut a real path in half; a newline is not,
    so it is the one separator that cannot be part of the data.
    """
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        text = str(raw or "").strip()
        if text.startswith("["):
            try:
                items = json.loads(text)
            except Exception:  # noqa: BLE001
                items = text.splitlines()
        else:
            items = text.splitlines()
    return [str(x).strip().strip('"').strip("'") for x in items if str(x).strip()]


@mcp.tool()
def onedrive_delete(path: str, root: str = "", drive: str = "",
                    allow_folder: bool = False) -> str:
    """Delete one file - or a list of them - from OneDrive or SharePoint.

    SEVERAL PATHS GO IN ONE CALL, one per line. That is the whole point of the
    5 September 2026 change: deleting seven timesheet fragments used to mean
    seven approval prompts, and a run stalled for 46 minutes on a prompt for a
    file an earlier prompt had already removed. One call, one approval, one
    report.

        onedrive_delete("Contractors/.../PD_x_part2.png\nContractors/.../PD_x_part5.png")

    A PATH THAT IS ALREADY GONE IS NOT AN ERROR. It comes back as `absent` and
    the rest still run. Nothing aborts the batch - a typo in the fourth path
    costs that path and no other.

    Everything deleted goes to the recycle bin, recoverable for about 93 days.

    A FOLDER IS REFUSED unless ALLOW_FOLDER is set, because deleting one takes
    its whole contents. Do not use this on anything signed or executed - a
    contractor agreement is not a working file, and the recycle bin is not a
    filing system.

    DRIVE works as it does on onedrive_list. Blank is Andrew's OneDrive.
    """
    paths = _split_paths(path)
    if not paths:
        return "FAILED: no path given. Nothing has been deleted."
    rows = _graph().delete_items(paths, root=root, drive=drive,
                                 allow_folder=allow_folder)
    done = [r for r in rows if r["status"] == "deleted"]
    gone = [r for r in rows if r["status"] == "absent"]
    bad = [r for r in rows if r["status"] in ("failed", "refused")]

    if len(rows) == 1 and done:
        d = done[0]
        return (f"Deleted {d['path']}  ({d['size']} bytes)"
                + (f"  [{drive}]" if drive else "")
                + "\nIn the recycle bin, recoverable for about 93 days.")

    out = [f"Deleted: {len(done)}   Already gone: {len(gone)}   "
           f"Not deleted: {len(bad)}" + (f"   [{drive}]" if drive else "")]
    for r in done:
        out.append(f"  deleted   {r['path']}  ({r['size']} bytes)")
    for r in gone:
        out.append(f"  absent    {r['path']}  - nothing to do")
    for r in bad:
        out.append(f"  NOT DONE  {r['path']}")
        out.append(f"            {r['detail']}")
    if done:
        out.append("In the recycle bin, recoverable for about 93 days.")
    return "\n".join(out)


@mcp.tool()
def onedrive_dedupe(folder: str, root: str = "", drive: str = "",
                    apply: bool = False) -> str:
    """Find files in one folder that are byte-for-byte the same, and clear them.

    DRY BY DEFAULT. It reports what it would keep and what it would drop, and
    writes nothing until apply=True.

    WHY. Contractors paste their timesheet pages into the body of an email. When
    somebody replies and the mail client quotes the original, every one of those
    images arrives a second time as a real attachment on a real message. The
    sweep files both, under different part numbers, and the folder ends up
    holding seven pages of which two are a page twice. Nothing downstream can
    see it: the names differ, the sizes are buried, and the pages are only
    identical if you open them.

    Prasanthi Dharanikota's fortnight ending 30 August 2026 is the case this was
    written for - part4 and part6 were the same page, and the clean-up was being
    done a file at a time through an approval prompt.

    WHAT IT KEEPS. The first copy by name, which for `_partN` files is the
    lowest part number - the one that arrived on the original mail rather than
    in the quoted reply. Only exact byte matches are ever touched; a page that
    merely looks similar is left alone.

    This is the tidy-up for folders that already hold repeats. The sweep no
    longer creates them - see sweep_timesheets.
    """
    try:
        rows = _graph().folder_digests(folder, root=root, drive=drive)
    except Exception as e:  # noqa: BLE001
        return f"FAILED: {e}"
    if not rows:
        return f"{folder} - no files. Nothing to do."

    groups: dict[tuple, list[dict]] = {}
    for r in sorted(rows, key=lambda x: str(x["name"])):
        if not r["digest"]:
            continue
        groups.setdefault((r["kind"], r["digest"], r["size"]), []).append(r)

    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    head = [f"{folder}" + (f"   [{drive}]" if drive else ""),
            f"{len(rows)} files, {len(dupes)} set(s) of identical copies."]
    if not dupes:
        head.append("Nothing is duplicated. Nothing to do.")
        return "\n".join(head)

    drop: list[str] = []
    for _k, v in sorted(dupes.items(), key=lambda kv: str(kv[1][0]["name"])):
        head.append("")
        head.append(f"  KEEP  {v[0]['name']}  ({v[0]['size']} bytes)")
        for r in v[1:]:
            head.append(f"  DROP  {r['name']}  - identical to {v[0]['name']}")
            drop.append(r["path"])

    if not apply:
        head += ["", f"Nothing was deleted. Re-run with apply=True to remove "
                     f"the {len(drop)} copy(ies) above."]
        return "\n".join(head)

    res = _graph().delete_items(drop, drive=drive)
    done = [r for r in res if r["status"] == "deleted"]
    gone = [r for r in res if r["status"] == "absent"]
    bad = [r for r in res if r["status"] not in ("deleted", "absent")]
    head += ["", f"Deleted: {len(done)}   Already gone: {len(gone)}   "
                 f"Not deleted: {len(bad)}"]
    for r in bad:
        head.append(f"  NOT DONE  {r['path']}: {r['detail']}")
    head.append("In the recycle bin, recoverable for about 93 days.")
    return "\n".join(head)



@mcp.tool()
def onedrive_move(path: str, dest_folder: str = "", root: str = "",
                  drive: str = "", new_name: str = "") -> str:
    """Move ONE file or folder to another folder on the same drive.

    Added because the cloud path could write and delete but not move, so
    filing a finished package away - a deployed connector zip out of
    `_PENDING UPLOAD` and into `_deployed` - had to go through the laptop.
    The item keeps its id, so share links and resource URIs survive the move.

    DEST_FOLDER is a folder path from the drive root; blank moves it to the
    root. NEW_NAME renames it in the same call.

    Graph refuses rather than overwrites when the destination already holds
    that name, and the refusal is passed straight through. Moving between
    drives is not supported - move within a drive, or copy and check before
    deleting anything.

    DRIVE works as it does on onedrive_list. Blank is Andrew's OneDrive.
    """
    try:
        d = _graph().move_item(path, dest_folder, root=root, drive=drive,
                               new_name=new_name)
    except Exception as e:  # noqa: BLE001
        return f"FAILED: {e}"
    kind = "folder" if d["was_folder"] else "file"
    where = d["dest"] if dest_folder else f"the drive root as {d['name']}"
    return (f"Moved {kind} {d['path']} -> {where}"
            + ("  (renamed)" if d["renamed"] else "")
            + (f"  [{drive}]" if drive else ""))


# ---------------------------------------------------------------------------
# GitHub - edit the connector repos directly instead of building zips.
#
# Every write here lands on a BRANCH and opens a pull request. Nothing in this
# section can push to main; github_client refuses before it makes a request.
# See the module docstring in src/github_client.py for why that rule exists.
# ---------------------------------------------------------------------------


def _gh():
    from .github_client import GitHubClient

    return GitHubClient()


@mcp.tool()
def github_read(repo: str, path: str = "", ref: str = "main") -> str:
    """Read a file, or list a directory, from one of Andrew's GitHub repos.

    Read the LIVE repo before changing anything. Notes, state docs and zips in
    a conversation all go stale; main does not. Leave `path` blank to list the
    repo root.

    repo: Xero-custom-connector, cats-mcp-server, ms365-mailer, cv-suite,
          cv-suite-full, CV-Suite-Free
    ref:  a branch name or a commit sha. Defaults to main.
    """
    gh = _gh()
    try:
        entry = gh.read_file(repo, path, ref) if path else None
    except RuntimeError as e:
        if "is a directory" not in str(e):
            entry = None
            if path:
                raise
        else:
            entry = None
    if path and entry:
        return json.dumps(entry, indent=2)
    listing = gh.list_dir(repo, path, ref)
    return json.dumps({"repo": repo, "ref": ref, "path": path or "/", "entries": listing}, indent=2)


@mcp.tool()
def github_head(repo: str, ref: str = "main") -> str:
    """The current commit sha of a repo. Check this before and after a change -
    if it moved when you did not expect it to, someone uploaded something."""
    return json.dumps({"repo": repo, "ref": ref, "sha": _gh().head_sha(repo, ref)})


@mcp.tool()
def github_commit(
    repo: str,
    branch: str,
    message: str,
    files: dict,
    base: str = "main",
) -> str:
    """Commit files to a BRANCH in one commit. Never writes to main.

    `files` maps repo-relative path to the file's complete new text, e.g.
    {"src/writes.py": "<the whole file>"}. Send whole files, not fragments -
    this replaces each path outright. Set a path to null to delete it.

    Creates the branch from `base` if it does not exist. Follow with
    github_open_pr, then github_checks once CI has run.

    A branch name of main, master, trunk, release or production is refused.
    """
    return json.dumps(_gh().commit_files(repo, branch, files, message, base), indent=2)


@mcp.tool()
def github_open_pr(
    repo: str, branch: str, title: str, body: str = "", base: str = "main"
) -> str:
    """Open a pull request from a branch. Andrew merges it; nothing here does.

    Say in the body what changed and why, and what would break if it is wrong -
    that text is what he reads before clicking merge.
    """
    return json.dumps(_gh().open_pr(repo, branch, title, body, base), indent=2)


@mcp.tool()
def github_checks(repo: str, ref: str) -> str:
    """Did the test suite pass on this commit or branch?

    'FAILED - do not merge' means exactly that. 'no runs yet' usually means CI
    has not started - wait ten seconds and ask again.
    """
    return json.dumps(_gh().checks_for(repo, ref), indent=2)


@mcp.tool()
def github_replace(
    repo: str,
    branch: str,
    path: str,
    old_str: str,
    new_str: str,
    message: str,
    base: str = "main",
) -> str:
    """Change ONE exact passage in a file, without resending the whole file.

    Use this instead of github_commit for a small edit to a large file.
    github_commit replaces a path outright, so a one-line change to a 156KB
    module means reproducing all 156KB - slow, and every character is a chance
    to corrupt a file that deploys straight to production.

    old_str must match EXACTLY ONCE, whitespace and line breaks included.
    Zero matches or two matches is refused rather than guessed: widen old_str
    with surrounding lines until it is unique. Read the file with github_read
    first and copy the passage out of it rather than retyping it.

    Lands on a BRANCH like everything else here - main is refused. Repeated
    edits to the same branch stack instead of resetting it back to base.
    """
    return json.dumps(
        _gh().replace_in_file(repo, branch, path, old_str, new_str, message, base),
        indent=2,
    )
