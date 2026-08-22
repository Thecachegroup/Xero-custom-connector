"""
Write operations against Xero.

Everything here creates DRAFT records only. Nothing is approved, posted, or paid
by this code. Andrew reviews in Xero and clicks the button himself.

That is deliberate and it is not negotiable in the code: a timesheet where 7.5
was read as 75 is real money out the door, and the review step is the only thing
standing between a bad number and a bank transfer.

Writes are OFF unless TCG_WRITE_ENABLED=true. Without it every function here
refuses and explains why.

Scope requirements - these must be added to the Custom Connection in Xero, and
adding them deactivates the connection until it is re-authorised:
    payroll.timesheets      (was payroll.timesheets.read)
    accounting.settings     (was accounting.settings.read)  - item rate card
    accounting.transactions (was accounting.invoices.read)  - draft invoices
"""

from __future__ import annotations

import os
import logging

log = logging.getLogger(__name__)

API_BASE = "https://api.xero.com/api.xro/2.0"

# Payroll AU lives on 1.0 throughout. (2.0 is UK/NZ - pointing AU at it returns
# 404, which is exactly what happened on the first live run.)
PAYROLL_BASE = os.environ.get(
    "XERO_PAYROLL_BASE", "https://api.xero.com/payroll.xro/1.0"
)


class WritesDisabled(RuntimeError):
    pass


def _guard() -> None:
    if os.environ.get("TCG_WRITE_ENABLED", "").strip().lower() != "true":
        raise WritesDisabled(
            "Writes are disabled. Set TCG_WRITE_ENABLED=true in Render to turn them "
            "on, and make sure the Custom Connection has the write scopes "
            "(payroll.timesheets, accounting.settings, accounting.transactions). "
            "Nothing has been sent to Xero."
        )


def _post(client, url: str, payload: dict) -> dict:
    """POST through the same rate limiter the read path uses."""
    client._limiter.acquire()
    resp = client._session.post(
        url,
        headers={
            "Authorization": f"Bearer {client._access_token()}",
            "Xero-tenant-id": client.tenant_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Xero rejected the write ({resp.status_code}). Nothing was changed. "
            f"Xero said: {resp.text[:500]}"
        )
    return resp.json()


# ------------------------------------------------------------------ payroll


def find_employee(client, name: str) -> dict:
    """Resolve a name fragment to exactly one employee. Refuses on ambiguity."""
    people = client.employees()
    hits = [
        e for e in people
        if name.strip().lower() in f"{e.get('FirstName','')} {e.get('LastName','')}".lower()
    ]
    if not hits:
        raise RuntimeError(f"No Xero employee matches {name!r}.")
    if len(hits) > 1:
        listed = ", ".join(f"{h.get('FirstName')} {h.get('LastName')}" for h in hits)
        raise RuntimeError(
            f"{name!r} matches more than one employee ({listed}). "
            "Use a fuller name - I will not guess which person to pay."
        )
    return hits[0]


def earnings_rates(client) -> list[dict]:
    """Earnings rate IDs, needed to build timesheet lines. Read-only."""
    return client.pay_items().get("EarningsRates", [])


def payroll_calendars(client) -> list[dict]:
    """Payroll calendar IDs, needed to create a timesheet. Read-only."""
    return client.payroll_calendars()


def create_draft_timesheet(
    client,
    employee_id: str,
    start_date: str,
    end_date: str,
    earnings_rate_id: str,
    units_by_day: list[float],
) -> dict:
    """
    Create a DRAFT timesheet on the Payroll AU API.

    AU expects NumberOfUnits as an ARRAY - one entry per day from StartDate to
    EndDate inclusive, in order. A 14-day fortnight needs exactly 14 numbers,
    zeros included. Getting the length wrong silently shifts everyone's days
    onto the wrong dates, so it is checked here rather than trusted.

    Status stays DRAFT. It must be approved in Xero before it feeds a pay run.
    """
    _guard()
    from datetime import date as _date, timedelta
    d0, d1 = _date.fromisoformat(start_date), _date.fromisoformat(end_date)
    span = (d1 - d0).days + 1
    if span < 1:
        raise ValueError("end_date is before start_date.")
    if len(units_by_day) != span:
        raise ValueError(
            f"The period {start_date} to {end_date} is {span} days but "
            f"{len(units_by_day)} unit values were given. They must match "
            "exactly, including zeros for days not worked - otherwise Xero "
            "records the hours against the wrong dates."
        )
    payload = {
        "Timesheets": [{
            "EmployeeID": employee_id,
            "StartDate": start_date,
            "EndDate": end_date,
            "Status": "DRAFT",
            "TimesheetLines": [{
                "EarningsRateID": earnings_rate_id,
                "NumberOfUnits": [float(u) for u in units_by_day],
            }],
        }]
    }
    log.info("Draft timesheet %s %s..%s (%d days)", employee_id, start_date, end_date, span)
    return _post(client, f"{PAYROLL_BASE}/Timesheets", payload)


# ------------------------------------------------------------------ accounting


def update_item_rates(
    client,
    item_code: str,
    purchase_rate: float | None = None,
    sell_rate: float | None = None,
) -> dict:
    """Update the cost and/or sell rate on one inventory item (the rate card)."""
    _guard()
    if purchase_rate is None and sell_rate is None:
        raise ValueError("Give at least one of purchase_rate or sell_rate.")
    item: dict = {"Code": item_code}
    if purchase_rate is not None:
        item["PurchaseDetails"] = {"UnitPrice": round(float(purchase_rate), 4)}
    if sell_rate is not None:
        item["SalesDetails"] = {"UnitPrice": round(float(sell_rate), 4)}
    return _post(client, f"{API_BASE}/Items", {"Items": [item]})


def create_draft_invoice(
    client,
    contact_id: str,
    line_items: list[dict],
    date: str,
    due_date: str,
    reference: str = "",
) -> dict:
    """
    Create a DRAFT sales invoice. Never AUTHORISED - it does not go to the
    customer until Andrew approves it in Xero.

    line_items: [{"ItemCode": "Linfox - JJ", "Quantity": 10, "UnitAmount": 1100,
                  "Description": "Jay Jhala, 6-19 Jul 2026"}, ...]
    """
    _guard()
    payload = {
        "Invoices": [{
            "Type": "ACCREC",
            "Contact": {"ContactID": contact_id},
            "Date": date,
            "DueDate": due_date,
            "Reference": reference,
            "LineItems": line_items,
            "Status": "DRAFT",
        }]
    }
    return _post(client, f"{API_BASE}/Invoices", payload)

# ------------------------------------------------------------------ attachments


def _attach(client, kind: str, doc_id: str, filename: str, content: bytes,
            content_type: str = "application/octet-stream",
            include_online: bool = True) -> dict:
    """Attach a file to a Xero invoice or bill.

    kind is the Xero collection: "Invoices" for both sales invoices (ACCREC) and
    bills (ACCPAY) - Xero keeps them in one collection and tells them apart by
    Type, so the same call serves the contractor's timesheet going onto the
    Linfox sales invoice AND their own invoice going onto the bill.

    include_online=True marks the file to travel with the invoice when it is
    emailed, which is the point for a timesheet: the client sees the evidence
    for the days they are being billed without anyone attaching it by hand. It
    is set to False for bills - the supplier's invoice is a record for TCG, not
    something to send back out.

    Requires the accounting.attachments scope. The read-only variant is not
    enough and fails with 401 rather than anything descriptive.
    """
    _guard()
    if not content:
        raise ValueError(f"{filename}: no content. Nothing was sent to Xero.")
    safe = filename.replace("/", "-").replace("\\", "-")
    url = f"{API_BASE}/{kind}/{doc_id}/Attachments/{safe}"
    if include_online:
        url += "?IncludeOnline=true"
    client._limiter.acquire()
    resp = client._session.post(
        url,
        headers={
            "Authorization": f"Bearer {client._access_token()}",
            "Xero-tenant-id": client.tenant_id,
            "Accept": "application/json",
            "Content-Type": content_type,
        },
        data=content,
        timeout=90,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Xero rejected the attachment ({resp.status_code}) for {safe}. Nothing "
            f"was changed. If this is a 401, the Custom Connection is missing the "
            f"accounting.attachments scope. Xero said: {resp.text[:400]}"
        )
    return resp.json()


def attach_to_invoice(client, invoice_id: str, filename: str, content: bytes,
                      content_type: str = "application/octet-stream") -> dict:
    """Attach a timesheet to a sales invoice, and send it with the invoice."""
    return _attach(client, "Invoices", invoice_id, filename, content,
                   content_type, include_online=True)


def attach_to_bill(client, invoice_id: str, filename: str, content: bytes,
                   content_type: str = "application/octet-stream") -> dict:
    """Attach a contractor's own invoice to their bill. Kept internal."""
    return _attach(client, "Invoices", invoice_id, filename, content,
                   content_type, include_online=False)

# Fields Xero accepts back on a line. LineAmount is deliberately NOT sent - it is
# computed from Quantity x UnitAmount, and echoing a stale value back while
# changing the quantity is how you end up with an invoice whose total does not
# match its own lines.
_LINE_KEEP = ("LineItemID", "Description", "Quantity", "UnitAmount", "ItemCode",
              "AccountCode", "TaxType", "Tracking", "DiscountRate")


def update_invoice(client, invoice_id: str, lines: list[dict] | None = None,
                   invoice_number: str | None = None,
                   reference: str | None = None) -> dict:
    """Change the lines and/or the number on one existing invoice or bill, in place.

    The document keeps its InvoiceID, contact, dates and status. Each line keeps
    its own LineItemID so Xero updates it rather than adding a second one beside
    it. Only the fields actually passed are sent - a payload that names a field
    is a payload that changes it, so nothing is included by accident.
    """
    if lines is None and invoice_number is None and reference is None:
        raise ValueError("Nothing to change - pass lines, invoice_number or reference.")
    _guard()
    doc: dict = {"InvoiceID": invoice_id}
    if lines is not None:
        doc["LineItems"] = [{k: v for k, v in li.items()
                             if k in _LINE_KEEP and v is not None} for li in lines]
    if invoice_number is not None:
        doc["InvoiceNumber"] = invoice_number
    if reference is not None:
        doc["Reference"] = reference
    return _post(client, f"{API_BASE}/Invoices", {"Invoices": [doc]})


def update_invoice_lines(client, invoice_id: str, lines: list[dict]) -> dict:
    """Back-compatible alias."""
    return update_invoice(client, invoice_id, lines=lines)


# ---------------------------------------------------------------- fill planning
# Pure logic, no network. Separated so it can be tested, because this decides
# what number goes on an invoice that goes to a client.


def parse_quantities(text: str) -> tuple[dict[str, float], list[str]]:
    """'item code: days' per line -> ({code: days}, [lines that made no sense]).

    The code may contain spaces and hyphens ("Linfox - DL"), so the split is on
    the LAST colon, not the first.
    """
    wanted: dict[str, float] = {}
    bad: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            bad.append(line)
            continue
        code, _, qty = line.rpartition(":")
        try:
            wanted[code.strip()] = float(qty.strip())
        except ValueError:
            bad.append(line)
    return wanted, bad


def plan_line_fill(docs: list[dict], wanted: dict[str, float], stamp: str,
                   label: str = "invoice") -> tuple[list[dict], list[dict], list[dict]]:
    """Decide what to change before anything is sent.

    Returns (planned, skipped, docs_to_write). Mutates the line dicts inside
    `docs` so the caller can post them straight back.

    A line whose quantity is already non-zero is NEVER touched. That single rule
    is what makes this safe to run four times across a billing week - Wednesday's
    fill cannot be applied again on Friday and double the days.
    """
    planned: list[dict] = []
    skipped: list[dict] = []
    to_write: list[dict] = []

    for d in docs:
        lines = list(d.get("LineItems", []) or [])
        changed = False
        for li in lines:
            code = (li.get("ItemCode") or "").strip()
            if code not in wanted:
                continue
            qty = li.get("Quantity") or 0
            if qty:
                skipped.append({
                    "Doc": d.get("InvoiceNumber") or str(d.get("InvoiceID", ""))[:8],
                    "Contact": (d.get("Contact") or {}).get("Name", "?"),
                    "Item": code, "Qty already": qty,
                    "Why": "already billed - left alone",
                })
                continue

            desc = str(li.get("Description") or "")
            if stamp and stamp.lower() not in desc.lower():
                li["Description"] = f"{desc} - {stamp}".strip(" -")
            li["Quantity"] = wanted[code]
            changed = True
            unit = float(li.get("UnitAmount") or 0)
            planned.append({
                "Kind": label,
                "Doc": d.get("InvoiceNumber") or str(d.get("InvoiceID", ""))[:8],
                "Contact": (d.get("Contact") or {}).get("Name", "?"),
                "Item": code,
                "Days": wanted[code],
                "Unit": unit,
                "Amount": round(wanted[code] * unit, 2),
            })
        if changed:
            to_write.append({"InvoiceID": d.get("InvoiceID"), "LineItems": lines,
                             "InvoiceNumber": d.get("InvoiceNumber")})
    return planned, skipped, to_write


def plan_number_change(docs: list[dict], numbers: dict[str, str]) -> list[dict]:
    """Which bills should carry the contractor's own invoice number.

    Keyed on ITEM CODE, like everything else - the bill's existing number is a
    placeholder from the repeating template ("Inv", "JJ", "KCrabb") and is no use
    as a key.

    A bill that already carries the right number is left alone, so this is safe
    to run repeatedly across a billing week.
    """
    lower = {k.strip().lower(): v for k, v in numbers.items()}
    out: list[dict] = []
    for d in docs:
        codes = {(li.get("ItemCode") or "").strip()
                 for li in (d.get("LineItems") or [])}
        hit = sorted(c for c in codes if c and c in numbers)
        if len(hit) > 1:
            continue                       # ambiguous - do not guess
        if not hit:
            # No inventory item on the line. Office cleaning, subscriptions and
            # the like are repeating bills with a plain description, so fall back
            # to the supplier's name.
            contact = str((d.get("Contact") or {}).get("Name", "")).strip().lower()
            if contact not in lower:
                continue
            hit = [(d.get("Contact") or {}).get("Name", "").strip()]
            want = str(lower[contact]).strip()
        else:
            want = str(numbers[hit[0]]).strip()
        have = str(d.get("InvoiceNumber") or "").strip()
        if have == want:
            continue
        out.append({"InvoiceID": d.get("InvoiceID"), "Item": hit[0],
                    "Contact": (d.get("Contact") or {}).get("Name", "?"),
                    "Was": have or "(none)", "Now": want})
    return out


def period_reference(period_start, period_end) -> str:
    """TCG's house reference on a sales invoice: "3 August to 16 August 2026".

    Taken from the invoices actually sent, not invented - "6 July to 19 July 2026"
    and "20 July to 2 August 2026" are both real. So the month is written on both
    sides even when it is the same month, days carry no leading zero, and the year
    appears once at the end.

    A period spanning a year end has never occurred in the data. Both years are
    written in that case, which is the only unambiguous reading.
    """
    a, b = period_start, period_end
    if a.year != b.year:
        return f"{a.day} {a:%B} {a.year} to {b.day} {b:%B} {b.year}"
    return f"{a.day} {a:%B} to {b.day} {b:%B} {b.year}"


def plan_reference_change(docs: list[dict], reference: str) -> list[dict]:
    """Which sales invoices need the period reference. Already-correct ones skipped."""
    out: list[dict] = []
    for d in docs:
        have = str(d.get("Reference") or "").strip()
        if have == reference:
            continue
        out.append({"InvoiceID": d.get("InvoiceID"),
                    "Doc": d.get("InvoiceNumber") or str(d.get("InvoiceID", ""))[:8],
                    "Contact": (d.get("Contact") or {}).get("Name", "?"),
                    "Was": have or "(none)", "Now": reference})
    return out


def existing_attachments(client, doc_id: str) -> set[str]:
    """Filenames already attached to an invoice or bill, lowercased.

    Xero does not reject a duplicate filename - it stores a second copy. So the
    caller has to check, or every run of the billing week adds another copy of
    the same timesheet.
    """
    try:
        data = client.get(f"{API_BASE}/Invoices/{doc_id}/Attachments")
    except Exception:                                              # noqa: BLE001
        return set()
    return {str(a.get("FileName", "")).strip().lower()
            for a in (data.get("Attachments") or [])}


_CONTENT_TYPES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".docx":
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv", ".txt": "text/plain",
}


def content_type_for(filename: str) -> str:
    """Xero shows a PDF inline and an image as a thumbnail only if told what it is."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def plan_attachments(files: list[dict], folder_to_code: dict[str, str],
                     sales_by_code: dict[str, dict],
                     bills_by_code: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Decide which filed document goes onto which Xero record.

    TIMESHEET -> the SALES INVOICE, and it travels with the invoice when it is
    emailed, so the client sees the evidence for the days being billed.

    THE CONTRACTOR'S INVOICE -> their BILL, and it does not travel - it is TCG's
    record of what they charged, not something to send back out.

    Matched on the CONTAINING FOLDER, not the filename. The folder is
    "<Client>_<Name>" and maps to one item code; the filename prefix is initials
    and two contractors can share initials.

    Returns (planned, unplaceable).
    """
    planned: list[dict] = []
    unplaceable: list[dict] = []

    for f in files:
        rel = str(f.get("path") or f.get("name") or "")
        name = str(f.get("name") or "")
        folder = rel.split("/")[0] if "/" in rel else ""
        code = folder_to_code.get(folder)
        low = name.lower()

        if "timesheet" in low:
            kind, target, online = "timesheet", sales_by_code.get(code), True
            onto = "sales invoice"
        elif "invoice" in low:
            kind, target, online = "invoice", bills_by_code.get(code), False
            onto = "bill"
        else:
            unplaceable.append({"File": name, "Folder": folder,
                                "Why": "not a timesheet or an invoice by name"})
            continue

        if not code:
            unplaceable.append({"File": name, "Folder": folder,
                                "Why": "folder does not map to a contractor"})
            continue
        if not target:
            unplaceable.append({"File": name, "Folder": folder,
                                "Why": f"no draft {onto} for {code}"})
            continue

        planned.append({
            "File": name, "Path": rel, "Kind": kind, "Onto": onto,
            "Item": code,
            "Contact": (target.get("Contact") or {}).get("Name", "?"),
            "Doc": target.get("InvoiceNumber") or str(target.get("InvoiceID", ""))[:8],
            "InvoiceID": target.get("InvoiceID"),
            "IncludeOnline": online,
        })
    return planned, unplaceable


def set_invoice_status(client, invoice_id: str, status: str) -> dict:
    """Move one invoice between statuses. Only ever DRAFT -> SUBMITTED here.

    SUBMITTED puts it in Xero's Awaiting Approval list, which is the point: a
    finished invoice moves out of Drafts by itself, so anything left in Drafts on
    Saturday is something that did not come through. Reversible - Andrew can put
    it back to draft, and approving is still his.
    """
    if status not in {"SUBMITTED", "DRAFT"}:
        raise ValueError(f"Refusing to set status {status!r}. This only moves "
                         "between DRAFT and SUBMITTED - approving is Andrew's.")
    _guard()
    return _post(client, f"{API_BASE}/Invoices",
                 {"Invoices": [{"InvoiceID": invoice_id, "Status": status}]})


def plan_submission(docs: list[dict], attachments_by_id: dict[str, set]) -> tuple[list[dict], list[dict]]:
    """Which invoices are finished enough to submit, and why the rest are not.

    An invoice is finished when every line has a quantity AND something is
    attached to it. Submitting a half-finished invoice would defeat the whole
    point - Drafts has to mean "not done".

    Returns (ready, held).
    """
    ready: list[dict] = []
    held: list[dict] = []
    for d in docs:
        num = d.get("InvoiceNumber") or str(d.get("InvoiceID", ""))[:8]
        row = {"Doc": num, "Contact": (d.get("Contact") or {}).get("Name", "?"),
               "InvoiceID": d.get("InvoiceID")}
        lines = d.get("LineItems") or []
        if not lines:
            held.append({**row, "Why": "no lines"})
            continue
        empty = [li for li in lines if not (li.get("Quantity") or 0)]
        if empty:
            held.append({**row, "Why": f"{len(empty)} line(s) still at zero"})
            continue
        if not attachments_by_id.get(d.get("InvoiceID")):
            held.append({**row, "Why": "nothing attached"})
            continue
        ready.append({**row, "Total": d.get("Total") or d.get("SubTotal")})
    return ready, held
