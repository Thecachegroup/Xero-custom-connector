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
# Onboarding paperwork. Arrives in the same mailbox but is NOT period paperwork -
# a passport does not belong in a fortnight folder, and filing one there once
# means it is copied forward every fortnight afterwards.
_ADMIN_HINTS = ("passport", "banking", "bank detail", "super choice", "tfn",
                "declaration", "contract", "handbook", "licence", "license",
                "visa", "id ", "identification")


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

    if any(h in name for h in _ADMIN_HINTS):
        return "admin"
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



# --------------------------------------------------------------------------
# Which period does this document SAY it is for
# --------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# Deliberately conservative. A bare 8-digit run like "DevIT Invoice 20260802"
# is an invoice NUMBER as often as a date, and guessing wrong moves a document
# into the wrong fortnight silently. Anything not confidently a date is left
# alone and the received date decides instead.
# Year-first, any of - . / as separator. Karen Crabb writes "2026.8.16";
# a four-digit leading year makes the order unambiguous, so this is safe
# to widen where the day-first form below is not.
_ISO = re.compile(r"\b(20\d{2})[-./](\d{1,2})[-./](\d{1,2})\b")
_DMY = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](20\d{2}|\d{2})\b")
_DM = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})\b")
_D_MONTH = re.compile(
    r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
    re.I)
_MONTH_D = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})\s*(?:st|nd|rd|th)?\b",
    re.I)


def stated_dates(text: str, year_hint: int) -> list[date]:
    """Every date the text explicitly states.

    Contractors title their emails with the period the work covers - "Timesheets
    3rd - 14th August", "(02-08-2026 to 15-08-2026)", "Weeks Ending 2026-08-08".
    That is a direct statement of which fortnight the document belongs to, and
    it beats the received date, which only says when they got round to sending it.
    """
    out: list[date] = []
    t = str(text or "")

    def add(y, m, d):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass

    for y, m, d in _ISO.findall(t):
        add(y, m, d)
    consumed = _ISO.sub(" ", t)

    for a, m, b in _DMY.findall(consumed):
        lead = int(a)
        lead_year = 2000 + lead if lead < 100 else lead
        tail_year = 2000 + int(b) if int(b) < 100 else int(b)
        # Australian day-first is the default. The one exception: a two-digit
        # LEADING value that is the year we are working in, against a trailing
        # value that is not, is a year-first date. Karen Crabb writes
        # "26.8.16" for 16 August 2026; read day-first that is August 2016,
        # which is not a wrong guess so much as a document lost.
        if tail_year != year_hint and lead_year == year_hint and int(b) <= 31:
            add(lead_year, m, b)
        else:
            add(tail_year, m, a)
    consumed = _DMY.sub(" ", consumed)

    for d, mon in _D_MONTH.findall(consumed):
        add(year_hint, _MONTHS[mon.lower()[:3]], d)
    for mon, d in _MONTH_D.findall(consumed):
        add(year_hint, _MONTHS[mon.lower()[:3]], d)
    consumed = _MONTH_D.sub(" ", _D_MONTH.sub(" ", consumed))

    for d, m in _DM.findall(consumed):              # "3/8-14/8"
        if 1 <= int(m) <= 12:
            add(year_hint, m, d)

    return out


def period_verdict(text: str, period_end: date, grace_days: int = 10) -> str:
    """'in' | 'out' | 'unknown' - what the document itself says about its period.

    'unknown' means no date was stated and the caller should fall back to the
    received date. Returning 'unknown' rather than a guess is the point: a
    document with no stated period is genuinely undecidable from its title.
    """
    start = period_end - timedelta(days=13)
    found = stated_dates(text, period_end.year)
    if not found:
        return "unknown"
    if any(start <= d <= period_end for d in found):
        return "in"
    # Dates stated, none in this fortnight. Allow the pay week itself - an
    # invoice dated the Monday after still belongs to the period it covers.
    if any(period_end < d <= period_end + timedelta(days=grace_days) for d in found):
        return "in"
    return "out"


def period_window(period_end: date | str, grace_days: int = 10) -> tuple[date, date]:
    """Which messages belong to this fortnight.

    THIS REPLACES A LIVE DEFECT. The first version searched a flat 45 days back,
    which was meant to catch late senders but had no way to tell "sent late for
    this fortnight" from "sent on time for the last one". A dry run for the
    fortnight ending 16 Aug proposed filing four fortnights of Peter Small's
    invoices - 21/06, 05/07, 19/07 and 02/08 - into the same folder.

    The honest boundary is the PREVIOUS period end: anything received after the
    last fortnight closed, up to a grace period past this one, belongs to this
    fortnight. Someone sending three weeks late is now reported as missing
    rather than silently filed against the wrong period, which is the right
    failure - it is visible.
    """
    end = period_end if isinstance(period_end, date) else date.fromisoformat(str(period_end))
    return end - timedelta(days=13), end + timedelta(days=grace_days)


def plan_filing(messages: list[dict], period_end: date | str,
                contractors: list[dict] | None = None,
                cadence: str = "fortnightly",
                grace_days: int = 10,
                file_admin: bool = False) -> dict:
    """Decide where every attachment goes, before anything is written.

    Returns {"files": [...], "unmatched": [...], "missing": [...],
             "out_of_period": [...]}.

    Nothing is uploaded here. A dry plan can be shown to Andrew and checked
    against what he expects, which is the difference between a filing step he
    trusts and one he has to audit afterwards.

    Two things this gets right that the first version did not:

    PERIOD. Messages outside the fortnight's own window are excluded and
    reported, not filed. See period_window().

    PART NUMBERING. Parts run across the WHOLE plan per contractor and kind, not
    per message. Numbering per message meant two emails each produced a
    "_part1", both resolved to the same filename, and the skip-if-exists check
    silently dropped the second. Different files must never collide on a name.
    """
    roster = contractors if contractors is not None else load_contractors()
    in_play = [c for c in roster if in_scope(c, cadence)]
    lo, hi = period_window(period_end, grace_days)

    unmatched, out_of_period, seen = [], [], set()
    staged: list[dict] = []

    for msg in messages:
        who = match_sender(msg.get("sender", ""), roster)
        if not who:
            unmatched.append({
                "sender": _addr(msg.get("sender", "")),
                "subject": msg.get("subject", ""),
                "received": msg.get("received", ""),
                "reason": "no contractor matches this address",
            })
            continue
        if not in_scope(who, cadence):
            continue

        recv = _as_date(msg.get("received"))
        end_d = period_end if isinstance(period_end, date) else date.fromisoformat(str(period_end))

        # What the document SAYS beats when it arrived. Only fall back to the
        # received date when nothing states a period.
        #
        # Judged per attachment, not per message. Karen Crabb sends an invoice
        # and its timesheets together and names each one with the date it
        # covers, so a single message can straddle two fortnights. An
        # attachment whose own name states nothing inherits the message
        # verdict, which pools the subject and every attachment name - that is
        # what resolves her "26.8.2" timesheet, a format too ambiguous to parse
        # on its own but unmistakable next to "2026.8.2" on the invoice.
        subject = str(msg.get("subject", "") or "")
        names = " ".join(str(a.get("name", "")) for a in msg.get("attachments", []) or [])
        msg_verdict = period_verdict(f"{subject} {names}", end_d, grace_days)
        if msg_verdict == "unknown":
            msg_verdict = "in" if (recv and lo <= recv <= hi) else "out"

        filed_any = False
        for a in msg.get("attachments", []) or []:
            kind = classify(a.get("name", ""), a.get("contentType", ""),
                            bool(a.get("isInline")), subject)
            if kind == "admin" and not file_admin:
                continue

            name = str(a.get("name", "") or "")
            verdict = period_verdict(f"{subject} {name}", end_d, grace_days)
            if verdict == "unknown":
                verdict = msg_verdict
            if verdict == "out":
                out_of_period.append({
                    "contractor": who["name"],
                    "subject": subject,
                    "file": name,
                    "received": recv.isoformat() if recv else "",
                    "stated": ", ".join(d.isoformat() for d in
                                        stated_dates(f"{subject} {name}", end_d.year)[:4]),
                })
                continue

            filed_any = True
            staged.append({
                "contractor": who["name"], "item_code": who["item_code"],
                "folder": who["folder"], "kind": kind,
                "attachment_id": a.get("id"), "message_id": msg.get("id"),
                "source_name": a.get("name", ""), "received": recv,
                "_who": who,
            })

        if filed_any:
            seen.add(who["name"])

    # Part numbers assigned ACROSS the whole plan, ordered by when they arrived,
    # so two files can never resolve to the same name.
    files = []
    groups: dict[tuple, list[dict]] = {}
    for item in staged:
        groups.setdefault((item["contractor"], item["kind"]), []).append(item)

    for (_c, kind), group in groups.items():
        group.sort(key=lambda x: (x["received"] or date.min, x["source_name"]))
        for i, item in enumerate(group, start=1):
            files.append({
                "contractor": item["contractor"],
                "item_code": item["item_code"],
                "kind": kind,
                "attachment_id": item["attachment_id"],
                "message_id": item["message_id"],
                "source_name": item["source_name"],
                "path": target_path(item["_who"], kind, period_end,
                                    item["source_name"], i, len(group)),
            })

    missing = [c["name"] for c in in_play if c["name"] not in seen]
    return {"files": sorted(files, key=lambda f: f["path"]),
            "unmatched": unmatched,
            "missing": sorted(missing),
            "out_of_period": out_of_period}


def _as_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    txt = str(value)[:10]
    try:
        return date.fromisoformat(txt)
    except ValueError:
        return None
