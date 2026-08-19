"""
Turn payroll-mailbox messages into filed contractor documents.

Pure logic only - no network. Everything here is unit-testable, which is
deliberate: the Graph calls in graph_client.py cannot be tested without live
credentials, so all the decisions that can go quietly wrong live here instead.

The two decisions that matter:
  1. WHO sent it   -> match_sender()
  2. WHAT is it    -> classify()

Both fail loudly rather than guessing. Filing Jay Jhala's invoice into Karen
Crabb's folder, or attaching it to the wrong Xero bill, is worse than not
filing it at all.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta

# Two spaces after "Fortnight". This is not a typo - it is how every folder from
# 2015 onward is named, and the match is exact.
FOLDER_TEMPLATE = "Fortnight  Ending {ddmmyyyy}"

CONFIG_PATH = os.environ.get("TCG_CONTRACTOR_MAIL", "config/contractor_mail.json")

# Inline images from Linfox PPM arrive as image.png / image001.png with no
# meaningful name. Real documents carry an extension that says what they are.
_INVOICE_HINTS = ("invoice", "inv", "tax invoice", "bill")
_TIMESHEET_HINTS = ("timesheet", "time sheet", "hours", "ts")
_EXPENSE_HINTS = ("expense", "receipt", "reimburse")


def load_contractors(path: str | None = None) -> list[dict]:
    with open(path or CONFIG_PATH) as fh:
        return (json.load(fh) or {}).get("contractors", [])


def _addr(value: str) -> str:
    """Normalised email address. Case and surrounding display name stripped."""
    v = str(value or "").strip().lower()
    m = re.search(r"<([^>]+)>", v)          # "Dat Le <dat_le@linfox.com>"
    return (m.group(1) if m else v).strip()


def match_sender(sender: str, contractors: list[dict] | None = None) -> dict | None:
    """The contractor who sent this, or None.

    Matched on the ADDRESS, never the display name. Display names arrive as
    "Dat Le", "DatLe", "Le, Dat" and "Dat Tien Le" for the same person; the
    address is the only stable key.

    Returns None rather than a best guess. An unmatched message is reported to
    Andrew, which is recoverable. A wrongly matched one files a contractor's
    invoice against someone else's bill, which is not.
    """
    a = _addr(sender)
    if not a:
        return None
    for c in contractors if contractors is not None else load_contractors():
        if a in {_addr(e) for e in c.get("emails", [])}:
            return c
    return None


def classify(filename: str, content_type: str = "", is_inline: bool = False,
             subject: str = "") -> str:
    """'invoice' | 'timesheet' | 'expense' | 'unknown'.

    Order matters. The filename is checked first because a named file is the
    sender's own statement of what it is. Only when the name says nothing -
    which is every inline screenshot, all called image.png - do we fall back to
    the subject line and the inline flag.

    An inline image with an uninformative name is a timesheet: that is what the
    Linfox PPM screenshot is, every fortnight, from everyone who sends one.
    """
    name = str(filename or "").lower()
    subj = str(subject or "").lower()

    if any(h in name for h in _INVOICE_HINTS):
        return "invoice"
    if any(h in name for h in _TIMESHEET_HINTS):
        return "timesheet"
    if any(h in name for h in _EXPENSE_HINTS):
        return "expense"

    generic = bool(re.fullmatch(r"(image|img|pasted[-_ ]?image)\d*\.\w+", name)) or \
              name.startswith("tmp_")
    if is_inline or generic:
        if any(h in subj for h in _INVOICE_HINTS):
            return "invoice"
        return "timesheet"

    if any(h in subj for h in _INVOICE_HINTS):
        return "invoice"
    if any(h in subj for h in _TIMESHEET_HINTS):
        return "timesheet"
    return "unknown"


def fortnight_folder(period_end: date | str) -> str:
    d = period_end if isinstance(period_end, date) else date.fromisoformat(str(period_end))
    return FOLDER_TEMPLATE.format(ddmmyyyy=d.strftime("%d%m%Y"))


def period_start(period_end: date | str) -> date:
    """The Monday 13 days before the Sunday the fortnight ends on."""
    d = period_end if isinstance(period_end, date) else date.fromisoformat(str(period_end))
    return d - timedelta(days=13)


def _initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", str(name).strip()) if p]
    if not parts:
        return "X"
    return (parts[0][0] + "".join(p[0] for p in parts[1:])).upper()


def build_filename(contractor: dict, kind: str, period_end: date | str,
                   original: str, index: int = 1, total: int = 1) -> str:
    """A predictable name, because step 2 has to find this file without a human.

    <Initials>_<kind>_<YYYY-MM-DD>[_partN].<ext>

    The extension is carried across from the original - the bytes are unchanged,
    so claiming a different type would be a lie. Sorting by name sorts by
    contractor then kind then period, which is the order you read them in.
    """
    d = period_end if isinstance(period_end, date) else date.fromisoformat(str(period_end))
    ext = os.path.splitext(str(original))[1].lower() or ".bin"
    stem = f"{_initials(contractor['name'])}_{kind}_{d.isoformat()}"
    if total > 1:
        stem += f"_part{index}"
    return stem + ext


def target_path(contractor: dict, kind: str, period_end: date | str,
                original: str, index: int = 1, total: int = 1) -> str:
    """Folder + filename, relative to Contractors/Timesheets/."""
    return "/".join([
        fortnight_folder(period_end),
        contractor["folder"],
        build_filename(contractor, kind, period_end, original, index, total),
    ])


def in_scope(contractor: dict, cadence: str = "fortnightly") -> bool:
    return str(contractor.get("cadence", "fortnightly")).lower() == cadence.lower()


def plan_filing(messages: list[dict], period_end: date | str,
                contractors: list[dict] | None = None,
                cadence: str = "fortnightly") -> dict:
    """Decide where every attachment goes, before anything is written.

    Returns {"files": [...], "unmatched": [...], "missing": [...]}.

    Nothing is uploaded here. A dry plan can be shown to Andrew and checked
    against what he expects, which is the difference between a filing step he
    trusts and one he has to audit afterwards.
    """
    roster = contractors if contractors is not None else load_contractors()
    in_play = [c for c in roster if in_scope(c, cadence)]
    files, unmatched, seen = [], [], set()

    for msg in messages:
        who = match_sender(msg.get("sender", ""), roster)
        if not who or not in_scope(who, cadence):
            if not who:
                unmatched.append({
                    "sender": _addr(msg.get("sender", "")),
                    "subject": msg.get("subject", ""),
                    "received": msg.get("received", ""),
                    "reason": "no contractor matches this address",
                })
            continue

        seen.add(who["name"])
        atts = msg.get("attachments", []) or []
        by_kind: dict[str, list[dict]] = {}
        for a in atts:
            k = classify(a.get("name", ""), a.get("contentType", ""),
                         bool(a.get("isInline")), msg.get("subject", ""))
            by_kind.setdefault(k, []).append(a)

        for kind, group in by_kind.items():
            for i, a in enumerate(group, start=1):
                files.append({
                    "contractor": who["name"],
                    "item_code": who["item_code"],
                    "kind": kind,
                    "attachment_id": a.get("id"),
                    "message_id": msg.get("id"),
                    "source_name": a.get("name", ""),
                    "path": target_path(who, kind, period_end, a.get("name", ""),
                                        i, len(group)),
                })

    missing = [c["name"] for c in in_play if c["name"] not in seen]
    return {"files": files, "unmatched": unmatched, "missing": sorted(missing)}
