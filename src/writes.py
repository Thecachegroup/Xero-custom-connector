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
