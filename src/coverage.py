"""Tracked-inventory coverage - did the Phase 5 adjustment actually get posted?

Phase 5 of the fortnightly run posts an inventory adjustment against each
TRACKED PAYG item, moving the wage cost out of 477 (PAYG Employees) into 630
(Inventory) so the sales invoice can consume it. Xero exposes no Accounting API
endpoint for it - QuantityOnHand is a derived, read-only field and adjustments
go through an internal route the web UI uses - so Phase 5 is a manual,
item-by-item loop through Products and services in a browser.

A MANUAL LOOP WITH NO COMPLETION CHECK STOPS WHEREVER IT STOPS AND LEAVES NO
TRACE. For the fortnight ending 30 August 2026 it ran once and stopped.
'Linfox - DL' was posted; 'Linfox - DTL', 'Linfox - KBJ' and 'Linfox - EK' were
not. $24,670 of wage cost sat in the wrong account for a week and three sales
invoices sat in Awaiting Approval against items with zero stock on hand -
approving any of them would have driven the item negative and thrown the cost of
sales onto the wrong side. Nothing in the connector, the skill or the review
report noticed. A human found it by seeing that one item had quantity and three
had zero.

The connector already read every number needed to catch it. QuantityOnHand comes
back on every item in get_rate_card today. Nothing compared it to anything. That
comparison is all this module is. Nothing here writes.

MATCH ON ITEM CODE, NEVER ON NAME. One human arrives as "Dat Le", "Dat Tien Le"
and "Le, Dat"; matching on a name once split one contractor into two and moved
$60,000 in get_contractor_ledger.

Codes are compared EXACTLY - case-folded and stripped - and the leading z of a
retired item is deliberately NOT stripped, unlike mappers.normalise_code.
'Linfox - MA' and 'zLinfox - MA' are two separate Xero items, each holding its
own stock; pooling them would report a balance that neither of them has. The
join key that is right for a ledger is wrong for a stock count.

AN APPROVED INVOICE HAS ALREADY SPENT ITS STOCK. Only DRAFT and SUBMITTED
quantities are demand - see PENDING_STATUSES below, which carries the live
evidence. The change request asked for AUTHORISED to count too; it must not, and
counting it would have reported Bhasker Veela SHORT 21 on an item that is
perfectly in order.

UNTRACKED ITEMS ARE EXCLUDED ENTIRELY, not reported as OK. They post the cost
straight to the expense account off the bill or the pay run and need no
adjustment, ever. Listing them buries the ones that do - the same failure the
coverage report had when it came back with twelve names, six of which were SEEK
ads, and the repeating-template audit had when it flagged eighty lines of which
one was wrong. A report nobody reads catches nothing.
"""

from __future__ import annotations

# Anything at or below this is float noise, not a real shortfall. Xero returns
# QuantityOnHand at four decimal places.
EPSILON = 1e-6


def code_key(code) -> str:
    """The comparison key for an item code: stripped and case-folded, nothing
    else. See the module docstring on why the leading z survives."""
    return str(code or "").strip().lower()


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def fmt_qty(n: float) -> str:
    """Trim a quantity for display: 10.0 -> '10', 0.9999 -> '0.9999'."""
    n = round(float(n), 4)
    return str(int(n)) if abs(n - int(n)) < EPSILON else f"{n:g}"


def stock_index(items: list[dict]) -> dict[str, dict]:
    """code_key -> {Code, Name, Tracked, OnHand}, straight off Xero /Items.

    Built from the raw Xero item objects rather than the rate-card DataFrame so
    the tracked flag and the quantity travel together and cannot be crossed the
    way InventoryAssetAccountCode and COGSAccountCode once were.
    """
    out: dict[str, dict] = {}
    for it in items or []:
        code = str(it.get("Code") or "").strip()
        if not code:
            continue
        out[code_key(code)] = {
            "Code": code,
            "Name": str(it.get("Name") or ""),
            "Tracked": bool(it.get("IsTrackedAsInventory")),
            "OnHand": _num(it.get("QuantityOnHand")),
        }
    return out


# An APPROVED sales invoice has ALREADY CONSUMED the stock it billed, so its
# quantity must not be counted as demand against what is left. This is the one
# thing the whole report turns on and it is not obvious.
#
# Verified against the live org on 3 September 2026, fortnight ending 30 August:
#
#   Emily Kimmins  Linfox - EK   9 billed, SUBMITTED   9 on hand
#   Dat Le         Linfox - DTL 10 billed, SUBMITTED  10 on hand
#   Bhasker Veela  Linfox - BV  21 billed, AUTHORISED  0 on hand
#
# EK and DTL are adjusted and not yet approved, so the stock is still sitting
# there and the comparison works. Bhasker's two invoices are approved, Xero has
# taken the 21 back out, and 0 on hand is the CORRECT end state - his cost comes
# through a BILL, which puts the quantity in automatically, and the approved
# invoice takes it out again. Counting his approved 21 as demand reports
# "SHORT 21" against an item that is perfectly in order.
#
# So demand is DRAFT + SUBMITTED only. That is the window where the adjustment
# should already be posted and the invoice has not yet spent it - which is
# exactly the window the report needs to catch, and exactly the state the three
# missed invoices were in on 2 September.
#
# Approved documents are still read, for two reasons: a NEGATIVE balance means
# an approval already went through against stock that was never there, and the
# approved quantity is worth showing beside the pending one so a zero balance
# reads as "consumed", not as "missing".
PENDING_STATUSES = ("DRAFT", "SUBMITTED")
SPENT_STATUSES = ("AUTHORISED", "PAID")


def billed_by_item(docs: list[dict]) -> dict[str, dict]:
    """Quantity billed per item code, split into pending and already approved.

    Aggregated across documents, not per line and not per document, because the
    stock is one pool. Two invoices each billing ten days of one item against
    ten on hand both look fine on their own and take the item to minus ten
    together.

    A document with no Status is treated as PENDING: a fixture or a hand-built
    document is being asked about before it goes anywhere, and the safe reading
    of an unknown status is the one that still checks the stock.
    """
    out: dict[str, dict] = {}
    for d in docs or []:
        status = str(d.get("Status") or "").strip().upper()
        if status in ("VOIDED", "DELETED"):
            continue
        spent = status in SPENT_STATUSES
        num = d.get("InvoiceNumber") or str(d.get("InvoiceID") or "")[:8]
        for li in d.get("LineItems") or []:
            code = str(li.get("ItemCode") or "").strip()
            if not code:
                continue
            k = code_key(code)
            row = out.setdefault(k, {"Code": code, "Qty": 0.0, "Spent": 0.0,
                                     "Docs": [], "SpentDocs": []})
            row["Spent" if spent else "Qty"] += _num(li.get("Quantity"))
            bucket = row["SpentDocs"] if spent else row["Docs"]
            if num and num not in bucket:
                bucket.append(num)
    return out


def verdict_for(billed: float, on_hand: float, spent: float = 0.0) -> str:
    """OK / SHORT n / OVER n / NEGATIVE / APPROVED, from the numbers.

    `billed` is PENDING quantity only - see PENDING_STATUSES above for why an
    approved invoice's quantity is not demand.

    NEGATIVE outranks everything: an item below zero means an invoice was
    already approved against stock that was never there, which is past the point
    the other verdicts are warning about.
    """
    if on_hand < -EPSILON:
        return "NEGATIVE - approved against stock that was never there"
    if billed <= EPSILON:
        # Nothing pending. The item is only here because an approved invoice
        # billed it, and that invoice has already taken its stock back out.
        if spent > EPSILON:
            return "APPROVED - stock already consumed"
        return "OK"
    gap = round(on_hand - billed, 4)
    if gap < -EPSILON:
        return f"SHORT {fmt_qty(-gap)} - adjustment not posted"
    if gap > EPSILON:
        return f"OVER {fmt_qty(gap)} - check for a duplicate adjustment"
    return "OK"


# Verdicts that mean somebody has to do something. Used for the loud block and
# for the exit summary, so the two can never disagree.
def is_fault(verdict: str) -> bool:
    return verdict.startswith(("SHORT", "NEGATIVE", "UNKNOWN"))


def plan_coverage(docs: list[dict], items: list[dict],
                  ignore_codes: set[str] | frozenset[str] = frozenset()
                  ) -> list[dict]:
    """One row per TRACKED item billed in the window, with its verdict.

    docs        - sales documents (ACCREC) in the period window, any status.
                  DRAFT, SUBMITTED and AUTHORISED all count: a missing
                  adjustment is a fault whether or not the invoice has moved on.
    items       - raw Xero /Items objects.
    ignore_codes- item codes flagged `ignore` in roster_overrides.json: SEEK
                  ads, training products, pass-through expenses. Not people, so
                  not adjustments.

    Rows are ordered faults first, then OVER, then OK, and alphabetically inside
    each - the thing to act on is at the top and stays there.
    """
    index = stock_index(items)
    billed = billed_by_item(docs)
    ignore = {code_key(c) for c in (ignore_codes or ())}

    rows: list[dict] = []
    for k, b in billed.items():
        if k in ignore:
            continue
        item = index.get(k)
        if item is None:
            # Billed against a code Xero does not have an item for. Not a stock
            # question, and silently dropping it would hide a typo, so say so.
            rows.append({"Item code": b["Code"], "Name": "(no such item in Xero)",
                         "Days billed": fmt_qty(b["Qty"]),
                         "Already approved": fmt_qty(b["Spent"]),
                         "Quantity on hand": "-",
                         "Verdict": "UNKNOWN ITEM - check the code",
                         "Docs": ", ".join(b["Docs"] + b["SpentDocs"]),
                         "_fault": True, "_sort": 0})
            continue
        if not item["Tracked"]:
            continue                       # untracked needs no adjustment, ever
        v = verdict_for(b["Qty"], item["OnHand"], b["Spent"])
        rows.append({
            "Item code": item["Code"],
            "Name": item["Name"],
            "Days billed": fmt_qty(b["Qty"]),
            "Already approved": fmt_qty(b["Spent"]),
            "Quantity on hand": fmt_qty(item["OnHand"]),
            "Verdict": v,
            "Docs": ", ".join(b["Docs"] + b["SpentDocs"]),
            "_fault": is_fault(v),
            "_sort": 0 if is_fault(v) else (1 if v.startswith("OVER") else 2),
        })

    rows.sort(key=lambda r: (r["_sort"], r["Item code"].lower()))
    return rows


def shortfalls(index: dict[str, dict], lines: list[dict],
               pool: dict[str, float]) -> list[str]:
    """Which tracked items on these lines cannot be covered by what is left.

    `pool` is the remaining stock by code_key and is NOT modified - the caller
    decides whether this document is actually going out before spending any of
    it. Quantities are summed per item first, so a document carrying one item on
    two lines is measured once against the balance.
    """
    want: dict[str, float] = {}
    for li in lines or []:
        code = str(li.get("ItemCode") or "").strip()
        if not code:
            continue
        k = code_key(code)
        item = index.get(k)
        if item is None or not item["Tracked"]:
            continue
        want[k] = want.get(k, 0.0) + _num(li.get("Quantity"))

    out = []
    for k, qty in sorted(want.items()):
        have = pool.get(k, 0.0)
        if qty - have > EPSILON:
            out.append(f"{index[k]['Code']} needs {fmt_qty(qty)}, "
                       f"{fmt_qty(have)} on hand")
    return out


def spend(lines: list[dict], index: dict[str, dict],
          pool: dict[str, float]) -> None:
    """Draw the tracked quantities on these lines down from the pool, in place.

    Called only once a document is actually going out, so a document held back
    for any reason never eats the stock that the next one needs.
    """
    for li in lines or []:
        code = str(li.get("ItemCode") or "").strip()
        if not code:
            continue
        k = code_key(code)
        item = index.get(k)
        if item is None or not item["Tracked"]:
            continue
        pool[k] = pool.get(k, 0.0) - _num(li.get("Quantity"))


def opening_pool(index: dict[str, dict],
                 reserved: list[dict] | None = None) -> dict[str, float]:
    """A fresh stock pool: every tracked item at its current QuantityOnHand,
    less anything already spoken for.

    RESERVED is the documents that are going to consume this stock but have not
    consumed it yet - in practice everything already sitting in Awaiting
    Approval. Xero decrements tracked stock on APPROVAL, not on submit, so an
    invoice submitted on Wednesday still shows its quantity as available on
    Friday. Without this the guard measured Friday's drafts against stock that
    Wednesday had already claimed, cleared them, and both went negative on
    approval - the very outcome it exists to prevent. inventory_coverage got
    this right from the start and the two tools disagreed on identical facts.
    """
    pool = {k: v["OnHand"] for k, v in index.items() if v["Tracked"]}
    for d in reserved or []:
        spend(d.get("LineItems") or [], index, pool)
    return pool
