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
# Split by strength. "hours" beats "invoice" because Peter Small's hours workbook
# is called "Invoice_0024-HOURS.xlsx" - it carries the invoice number it belongs
# to, but it is the timesheet. "ts" is a two-letter substring that also appears in
# "receipts" and "payments", so it is checked last, after everything specific.
_TIMESHEET_STRONG = ("timesheet", "time sheet", "hours")
_TIMESHEET_WEAK = ("ts",)
_TIMESHEET_HINTS = _TIMESHEET_STRONG + _TIMESHEET_WEAK
_EXPENSE_HINTS = ("expense", "receipt", "reimburse")
# Onboarding paperwork. Arrives in the same mailbox but is NOT period paperwork -
# a passport does not belong in a fortnight folder, and filing one there once
# means it is copied forward every fortnight afterwards.
# ONBOARDING PAPERWORK, not money. Matched as WHOLE WORDS - see _has_hint().
#
# SUBSTRING MATCHING SILENTLY ATE AN INVOICE. Prasanthi Dharanikota's August
# 2026 invoice is named "Indian_Contractor_... Invoice.pdf". "contract" sits
# inside "Contractor", so classify() returned "admin" and the sweep dropped it
# before the invoice test ever ran - and an admin file is not filed and not
# reported, so it appeared in no list at all. Found 5 September 2026 only
# because Andrew knew the invoice existed. "id " was as bad: it matches inside
# "Pravid Technologies". Anyone whose filename says Contractor, Contractors,
# subcontractor or contracting loses their invoice the same way.
_ADMIN_HINTS = ("passport", "banking", "bank detail", "super choice", "tfn",
                "declaration", "contract", "handbook", "licence", "license",
                "visa", "id", "identification")


def _has_hint(name: str, hints: tuple[str, ...]) -> bool:
    """Is any hint present as a WHOLE WORD (or whole phrase) in NAME.

    Word characters are letters and digits; every other character is a
    separator, so "indian_contractor" splits on the underscore and yields
    "contractor", which is not "contract". A multi-word hint like "bank detail"
    is matched against the same normalised text.
    """
    words = re.split(r"[^a-z0-9]+", str(name or "").lower())
    text = " ".join(w for w in words if w)
    have = set(words)
    for h in hints:
        h = h.strip().lower()
        if not h:
            continue
        if " " in h:
            if h in text:
                return True
        elif h in have:
            return True
    return False


def load_contractors(path: str | None = None) -> list[dict]:
    """The old hand-maintained roster, kept only as a fallback and for tests.

    The live roster is built from Xero by src/roster.py and passed in. This
    reads config/contractor_mail.json if it is still there and returns an empty
    list if it is not - a missing file is not an error any more.
    """
    try:
        with open(path or CONFIG_PATH) as fh:
            return (json.load(fh) or {}).get("contractors", [])
    except FileNotFoundError:
        return []


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


# --------------------------------------------------------------------------
# Matching a message to a person by NAME, when the address is not on file
# --------------------------------------------------------------------------
#
# The address list is gone; the roster now comes from Xero. That buys a roster
# that cannot drift, and costs the one thing the list was good at - knowing
# that dat_le@linfox.com is Dat Le. So the address has to be READ instead.
#
# Nine of every ten contractor addresses carry the person's own name:
# devinia_liddelow, jachakkshitija, karenmareecrabb, mudassirali27, donvuong,
# jhalajay, richaarora16, mdmazherali, jerry_gonsalves. The one that does not -
# techneitconsulting@gmail.com - carries their Xero CONTACT name instead, which
# is why contact names are matched too.
#
# Everything here returns None rather than a best guess. An unmatched message is
# reported to Andrew and recoverable in a minute. A wrongly matched one files a
# contractor's invoice against somebody else's bill, and nobody finds it.

_BILLING_SYSTEMS = ("reckon", "myob", "xero.com", "invoices@", "quickbooks",
                    "billing@", "noreply@", "no-reply@")


def _squash(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _local_part(address: str) -> str:
    return _addr(address).split("@", 1)[0]


def _name_in_string(name: str, hay: str) -> bool:
    """Is this person's name inside this string, allowing either word order.

    Three ways, tightest first. The length floors matter: "Dat Le" squashes to
    five characters and would otherwise match inside words that have nothing to
    do with him.
    """
    parts = [p for p in re.split(r"[^a-z0-9]+", str(name or "").lower()) if p]
    if not parts or not hay:
        return False
    flat = "".join(parts)
    rev = "".join(reversed(parts))
    if hay == flat or hay == rev:                     # dat_le -> "datle"
        return True
    if len(flat) >= 8 and (flat in hay or rev in hay):   # richaarora16
        return True
    # Every part, each long enough to mean something, in order: karenmareecrabb
    if all(len(p) >= 3 for p in parts):
        pos = 0
        for p in parts:
            i = hay.find(p, pos)
            if i < 0:
                return False
            pos = i + len(p)
        return True
    return False


def name_evidence(msg: dict, person: dict) -> set[str]:
    """Which kinds of evidence say this message is from this person.

    Tiers, because they are not worth the same. An address is chosen once and
    then used for years; a display name is whatever the sender typed into
    Outlook this morning.
    """
    found: set[str] = set()
    names = [person.get("name", "")] + list(person.get("contact_names") or [])
    names = [n for n in names if n]

    sender_addrs = [msg.get("sender", "")] + list(msg.get("cc", []) or []) \
        + list(msg.get("reply_to", []) or [])
    for a in sender_addrs:
        local = _squash(_local_part(a))
        if not local:
            continue
        for n in names:
            if _name_in_string(n, local):
                found.add("address")
            # techneitconsulting@gmail.com against "Techne IT Consulting Pty Ltd"
            sq = _squash(n)
            if len(local) >= 8 and len(sq) >= 8 and (local in sq or sq in local):
                found.add("address")

    display = _squash(re.sub(r"<[^>]*>", "", str(msg.get("sender_name") or "")))
    for n in names:
        if display and _name_in_string(n, display):
            found.add("display")

    subject = _squash(msg.get("subject", ""))
    for n in names:
        if subject and _name_in_string(n, subject):
            found.add("subject")

    # "ARORA, RICHA" - surname first, upper case, inside the invoice body. The
    # order-insensitive test above already handles it; this just looks in the
    # body and the attachment names as well as the subject.
    body = _squash(str(msg.get("body", "") or "")[:4000])
    names_blob = _squash(" ".join(str(a.get("name", ""))
                                 for a in msg.get("attachments", []) or []))
    for n in names:
        if body and _name_in_string(n, body):
            found.add("body")
        if names_blob and _name_in_string(n, names_blob):
            found.add("filename")
    return found


def match_by_name(msg: dict, roster: list[dict]) -> tuple[dict | None, str]:
    """(person, why) - or (None, why not).

    An address, or a contact name in the address, is enough on its own. Nothing
    else is: a display name, a subject line or a name in the body needs a second
    piece of evidence agreeing with it, because all three can carry somebody
    else's name. Andrew's own forward of "RE: louis_soto@linfox.com" would
    otherwise read as Louis.

    Two people matching equally well is reported, never resolved. Two
    contractors sharing a surname is a real thing and guessing between them puts
    one person's money on another person's record.
    """
    scored: list[tuple[int, dict, set[str]]] = []
    for person in roster:
        ev = name_evidence(msg, person)
        if not ev:
            continue
        if "address" in ev:
            rank = 3
        elif len(ev - {"display"}) >= 2:
            rank = 2
        elif len(ev) >= 2:                    # display plus one other
            rank = 1
        else:
            continue                          # a single soft signal is not enough
        scored.append((rank, person, ev))

    if not scored:
        return None, "no name on this message matches anyone on the roster"

    top = max(r for r, _p, _e in scored)
    best = [(p, e) for r, p, e in scored if r == top]
    if len(best) > 1:
        who = ", ".join(p["name"] for p, _e in best)
        return None, f"matches more than one person equally well ({who}) - not guessed"

    person, ev = best[0]
    return person, "name found in " + ", ".join(sorted(ev))


def looks_like_a_billing_system(address: str) -> bool:
    """Reckon, MYOB and the like send on a contractor's behalf.

    Their address carries no person, so a name match will usually fail - and
    the message must still not be dismissed as junk, because there is an
    invoice inside it.
    """
    a = _addr(address)
    return any(h in a for h in _BILLING_SYSTEMS)


def _generic_name(name: str) -> bool:
    """A filename that tells you nothing - image.png, image001.png, tmp_<guid>.png."""
    n = str(name or "").lower()
    return bool(re.fullmatch(r"(image|img|pasted[-_ ]?image)\d*\.\w+", n)) or n.startswith("tmp_")


_FWD_FROM = re.compile(
    r"^\s*(?:From|Sent by)\s*:\s*(?:[^<\n]*<\s*)?([\w.+-]+@[\w.-]+\.\w+)",
    re.I | re.M)


def forwarded_senders(text: str) -> list[str]:
    """Addresses appearing on a 'From:' line inside a forwarded message.

    Andrew forwards contractor mail into the payroll mailbox, which makes HIM
    the sender and hides the contractor completely. The original sender is still
    there, in the quoted header block Outlook writes into the body.

    Order is preserved: the outermost forward comes first, so the first match is
    the person who actually sent the thing being forwarded.
    """
    out: list[str] = []
    for a in _FWD_FROM.findall(str(text or "")):
        low = a.strip().lower()
        if low not in out:
            out.append(low)
    return out


def match_sender_or_forward(msg: dict, contractors: list[dict] | None = None,
                            own_domains: tuple[str, ...] = ()) -> dict | None:
    """The contractor this message is about, following one forward if needed.

    The sender is tried first and always wins. Only when the sender is one of
    OUR OWN addresses - a forward from Andrew or the payroll mailbox - is the
    body consulted, because a contractor's own message must never be attributed
    to somebody named inside it.
    """
    who, _why = match_message(msg, contractors, own_domains)
    return who


def match_message(msg: dict, contractors: list[dict] | None = None,
                  own_domains: tuple[str, ...] = ()) -> tuple[dict | None, str]:
    """As above, but says WHY - which is what the unmatched report needs.

    Order is deliberate and does not change: a known address beats everything,
    a forward is only followed for our own domains, and reading names out of
    the message is the last resort rather than the first.
    """
    roster = contractors if contractors is not None else load_contractors()

    who = match_sender(msg.get("sender", ""), roster)
    if who:
        return who, "sender address is on file"

    # Reckon and MYOB send on someone's behalf and put them in the CC.
    for a in list(msg.get("cc", []) or []) + list(msg.get("reply_to", []) or []):
        who = match_sender(a, roster)
        if who:
            return who, "address is on file, in the cc or reply-to"

    addr = _addr(msg.get("sender", ""))
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    is_ours = bool(own_domains) and domain in {d.strip().lower() for d in own_domains}
    if is_ours:
        for candidate in forwarded_senders(msg.get("body", "")):
            who = match_sender(candidate, roster)
            if who:
                return who, "forwarded from an address on file"

    # Nothing on file. Read the name instead - but never out of a message one of
    # OUR OWN people sent, unless it is a forward we have already failed to
    # resolve above. Andrew forwarding "RE: louis_soto@linfox.com" carries
    # Louis's name and is not from Louis.
    who, why = match_by_name(msg, roster)
    if who and not is_ours:
        return who, why
    if who and is_ours:
        return None, ("a forward from us naming " + who["name"]
                      + " - sender identity leads, so this is not attributed")

    if looks_like_a_billing_system(msg.get("sender", "")):
        return None, ("sent by a billing system on someone's behalf - "
                      "OPEN IT, there is a document inside")
    return None, why


# An inline image smaller than this is a signature logo, a divider or a tracking
# pixel, not a timesheet. Andrew's signature graphic is 5,496 bytes; the smallest
# real PPM screenshot filed so far is 28,023.
SIGNATURE_IMAGE_MAX_BYTES = 15_000


def classify(filename: str, content_type: str = "", is_inline: bool = False,
             subject: str = "", has_document_invoice: bool = False,
             size: int | None = None) -> str:
    """'invoice' | 'timesheet' | 'expense' | 'unknown'.

    Order matters. The filename is checked first because a named file is the
    sender's own statement of what it is. Only when the name says nothing -
    which is every inline screenshot, all called image.png - do we fall back to
    the subject line and the inline flag.

    An inline image with an uninformative name is a timesheet: that is what the
    Linfox PPM screenshot is, every fortnight, from everyone who sends one.

    HAS_DOCUMENT_INVOICE settles the case the subject line used to get wrong.
    "Bilal Virk - Invoice - Linfox" carries an invoice hint in the subject, so
    every inline screenshot in that email was filed as a second invoice - when
    in fact Don Vuong, Mudassir Ali, Jay Jhala and Bilal Virk all send one
    invoice as a proper document and their timesheets as images beside it. If
    the message already contains a named invoice file, the images are the
    evidence behind it, not another copy of it.
    """
    name = str(filename or "").lower()
    subj = str(subject or "").lower()

    # Forwarding a message drags the forwarder's signature graphic along as an
    # inline attachment. Filing it as a timesheet puts a company logo on an
    # invoice as evidence of days worked.
    # Word writes ~WRD0000.jpg into a forwarded message. It is layout debris and
    # it is 14,559 bytes - close enough to the size cutoff that name-matching it
    # is the safer test.
    if name.startswith("~wrd") or name.startswith("~$"):
        return "signature"
    if (is_inline and size is not None and size <= SIGNATURE_IMAGE_MAX_BYTES
            and str(content_type or "").lower().startswith("image/")
            and not any(h in name for h in _TIMESHEET_STRONG)):
        return "signature"

    if _has_hint(name, _ADMIN_HINTS):
        return "admin"
    if any(h in name for h in _TIMESHEET_STRONG):
        return "timesheet"
    if any(h in name for h in _INVOICE_HINTS):
        return "invoice"
    if any(h in name for h in _EXPENSE_HINTS):
        return "expense"
    if any(h in name for h in _TIMESHEET_WEAK):
        return "timesheet"

    # The name says nothing. A message that already carries a named invoice
    # document has its supporting evidence in whatever else is attached.
    if has_document_invoice:
        return "timesheet"

    if is_inline or _generic_name(name):
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


def in_scope(contractor: dict, cadence: str = "all") -> bool:
    """Is this person swept on this run.

    'all' is the default and the normal case. Andrew's decision, 2 Sep 2026:
    MONTHLY CONTRACTORS FILE INTO THE CURRENT FORTNIGHT FOLDER, the same as
    everyone else. One folder to look in, not two, and no second folder
    template for attach_period_files to know about.

    Before this, the fortnightly sweep filtered them out entirely: Bhasker
    Veela's August invoices sat unmatched and unfiled, and his bill had to be
    given its evidence by hand.
    """
    c = str(cadence or "all").lower()
    if c in ("all", "", "both"):
        return True
    return str(contractor.get("cadence", "fortnightly")).lower() == c



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

    # Only spans that actually yield a plausible date are consumed. A span that
    # is rejected must stay readable, because "3/8-14/8" is a RANGE - two dates
    # in day/month form - and this pattern grabs "3/8-14" out of the middle of
    # it. Blanking that span destroyed both real dates and left the whole thing
    # unparseable; keeping it lets the day/month pass below read 3/8 and 14/8.
    def _dmy(match: "re.Match") -> str:
        a, m, b = match.group(1), match.group(2), match.group(3)
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
            return " "
        # A year a decade away from the one being worked on is not a year. It is
        # the middle of something else that happens to look like a date.
        if abs(tail_year - year_hint) > 1:
            return match.group(0)
        add(tail_year, m, a)
        return " "

    consumed = _DMY.sub(_dmy, consumed)

    for d, mon in _D_MONTH.findall(consumed):
        add(year_hint, _MONTHS[mon.lower()[:3]], d)
    for mon, d in _MONTH_D.findall(consumed):
        add(year_hint, _MONTHS[mon.lower()[:3]], d)
    consumed = _MONTH_D.sub(" ", _D_MONTH.sub(" ", consumed))

    for d, m in _DM.findall(consumed):              # "3/8-14/8"
        if 1 <= int(m) <= 12:
            add(year_hint, m, d)

    return out


def _last_of_month(year: int, month: int) -> date:
    return date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)


def _month_back(year: int, month: int, n: int = 1) -> tuple[int, int]:
    i = (year * 12 + month - 1) - n
    return i // 12, i % 12 + 1


def monthly_span(period_end: date | str, grace_days: int = 10,
                 period_day: int | None = None) -> tuple[date, date]:
    """The cycle a MONTHLY contractor's paperwork belongs to.

    THIS FIXES A LIVE DEFECT, found 5 September 2026. Every document was judged
    against the FORTNIGHT window. A monthly invoice states a calendar month,
    which cannot fit inside a fortnight, so it failed the test on every single
    run and was never filed - while the same person's WEEKLY timesheets passed
    and filed normally. Prasanthi Dharanikota's four August timesheets were
    filed and her August invoice was refused; her sales draft TCG-21207 and her
    PRAVID bill both sat at zero and August went unbilled on both sides.

    The cycle is the one ENDING on or before the end of the run window - so the
    30 August fortnight, whose window runs to 9 September, picks up AUGUST.
    Anything stated from the start of that cycle to the end of the window is in.
    Bounding it at the cycle START is what stops the previous month's paperwork
    being swept in behind it, which is the failure period_window() was written
    to prevent for fortnightly people.

    PERIOD_DAY handles an OFFSET cycle. Prasanthi runs the 12th of one month to
    the 11th of the next - Andrew, 3 Sep 2026 - so her cycle ends the day BEFORE
    period_day. She is the only offset person; Bhasker, Deepti and Vivek are all
    calendar month, which is period_day unset.
    """
    end = period_end if isinstance(period_end, date) else date.fromisoformat(str(period_end))
    anchor = end + timedelta(days=grace_days)
    day = int(period_day or 0)
    if day > 1:
        day = min(day, 28)          # a cycle day past the 28th has no February
        y, m = anchor.year, anchor.month
        if date(y, m, day) - timedelta(days=1) > anchor:
            y, m = _month_back(y, m)
        sy, sm = _month_back(y, m)
        return date(sy, sm, day), anchor
    y, m = anchor.year, anchor.month
    if _last_of_month(y, m) > anchor:
        y, m = _month_back(y, m)
    return date(y, m, 1), anchor


def contractor_span(contractor: dict, period_end: date | str,
                    grace_days: int = 10) -> tuple[date, date]:
    """The period window to judge ONE person's documents against.

    The cadence belongs to the person, not to the run. Deriving the window from
    the run alone is what refused every monthly invoice ever sent - see
    monthly_span(). Any future cadence breaks the same way until this is asked
    per person.
    """
    end = period_end if isinstance(period_end, date) else date.fromisoformat(str(period_end))
    if str((contractor or {}).get("cadence", "")).strip().lower() == "monthly":
        return monthly_span(end, grace_days, (contractor or {}).get("period_day"))
    return end - timedelta(days=13), end + timedelta(days=grace_days)


def period_verdict(text: str, period_end: date, grace_days: int = 10,
                   not_after: date | None = None,
                   span: tuple[date, date] | None = None) -> str:
    """'in' | 'out' | 'unknown' - what the document itself says about its period.

    'unknown' means no date was stated and the caller should fall back to the
    received date. Returning 'unknown' rather than a guess is the point: a
    document with no stated period is genuinely undecidable from its title.

    NOT_AFTER is the date the message arrived, and it discards impossible
    readings. Nobody invoices for work they have not done yet, so a date later
    than the day the document was sent is not the period it covers - it is a
    reference number, a due date, a scan counter, something else. Karen Crabb's
    "Timesheet 2026.08.27.pdf" arrived on 17 August with a covering note saying
    it was for the same period as the invoice beside it; read as a period date
    it was thrown out of its own fortnight.
    """
    start, stop = span if span else (period_end - timedelta(days=13),
                                     period_end + timedelta(days=grace_days))
    found = stated_dates(text, period_end.year)
    if not_after is not None:
        found = [d for d in found if d <= not_after]
    if not found:
        return "unknown"
    # STOP runs past the period end on purpose: an invoice dated the Monday
    # after still belongs to the period it covers.
    if any(start <= d <= stop for d in found):
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


# ------------------------------------------------------- duplicate paperwork

# A number shorter than this cannot be trusted as a substring. "0013" sitting
# inside "20260013" is a coincidence, not last fortnight's invoice. Short
# numbers still match - but only as a whole token.
_NUMBER_SUBSTRING_FLOOR = 5


def _number_tokens(text: str) -> set[str]:
    """Every alphanumeric run in a string, squashed."""
    return {_squash(t) for t in re.split(r"[^A-Za-z0-9]+", str(text or "")) if t}


def carries_a_number(text: str) -> bool:
    """Does this string contain a token with a digit in it.

    Decides whether a filename can be judged on its own. "Invoice INV-0016.pdf"
    can; "invoice.pdf" and "image.png" cannot, and only those fall back to the
    subject line - which matters, because a reply on an old thread carries the
    OLD invoice number in its subject and a NEW invoice as its attachment.
    Judging that message on its subject would refuse a perfectly good invoice.
    """
    return any(any(ch.isdigit() for ch in t) for t in _number_tokens(text))


def matches_known_number(text: str, known: str) -> bool:
    """Does `text` carry the invoice number `known`.

    Two rules, and the second is why this does not fire on coincidences.

    A number long enough to be distinctive (5+ characters once squashed)
    matches anywhere inside the text: "Invoice INV-0016.pdf" squashes to
    "invoiceinv0016", which contains "inv0016". Splitting on punctuation alone
    would have produced INVOICE, INV and 0016 and missed it.

    A short number - "0013", "0025" - matches only as a WHOLE TOKEN. Letting
    "0013" match anywhere would find it inside a date, an ABN or a longer
    number belonging to somebody else's period.
    """
    k = _squash(known)
    if len(k) < 3:
        return False
    if k in _number_tokens(text):
        return True
    return len(k) >= _NUMBER_SUBSTRING_FLOOR and k in _squash(text)


def duplicate_number(text: str,
                     known_numbers: dict[str, str] | None) -> tuple[str, str] | None:
    """(number, the period it was already billed for), or None.

    `known_numbers` is {invoice number: period it was used in} for ONE
    contractor, built from their earlier bills. Longest number first, so
    "INV-0016" is reported rather than the "16" inside it.
    """
    if not known_numbers:
        return None
    for num in sorted(known_numbers, key=lambda n: -len(_squash(n))):
        if matches_known_number(text, num):
            return num, known_numbers[num]
    return None


def find_duplicate(msg: dict, prior: dict[str, str] | None,
                   has_doc_invoice: bool = False) -> tuple[str, str, str] | None:
    """(filename, number, period it was used in) if this message carries an
    invoice number already billed in an earlier period. None otherwise.

    Only attachments that classify as an invoice are checked. A timesheet has
    no number of its own, and Peter Small's hours workbook deliberately carries
    the invoice number it belongs to - refusing that would refuse his hours.
    """
    if not prior:
        return None
    subject = str(msg.get("subject", "") or "")
    for a in msg.get("attachments", []) or []:
        name = str(a.get("name", "") or "")
        if classify(name, a.get("contentType", ""), bool(a.get("isInline")),
                    subject, has_document_invoice=has_doc_invoice,
                    size=a.get("size")) != "invoice":
            continue
        hay = name if carries_a_number(name) else f"{subject} {name}"
        hit = duplicate_number(hay, prior)
        if hit:
            return name, hit[0], hit[1]
    return None


def plan_filing(messages: list[dict], period_end: date | str,
                contractors: list[dict] | None = None,
                cadence: str = "all",
                grace_days: int = 10,
                file_admin: bool = False,
                own_domains: tuple[str, ...] = (),
                prior_invoice_numbers: dict[str, dict[str, str]] | None = None) -> dict:
    """Decide where every attachment goes, before anything is written.

    Returns {"files": [...], "unmatched": [...], "missing": [...],
             "out_of_period": [...], "body_only": [...], "duplicates": [...]}.

    PRIOR_INVOICE_NUMBERS is {item code: {invoice number: period it was billed
    for}}, built from Xero by the caller. Pass it and duplicate paperwork is
    refused at filing time; leave it out and behaviour is exactly as before.

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

    BODY-ONLY TIMESHEETS. The unit of work here is an attachment, so a person
    who types their hours into the email and attaches nothing produced no plan
    entry at all and fell through to "missing" - indistinguishable from someone
    who never wrote. That is how Devinia Liddelow was reported as having sent
    nothing for the fortnight ending 30 August 2026 when her timesheet had been
    sitting in the mailbox since the Tuesday. Those messages now come back under
    "body_only": the message itself is saved as .eml and the person counts as
    having sent. The days still have to be read by a human - which is the
    honest outcome, and a long way better than silence.

    DUPLICATE PAPERWORK. An invoice number that has already been billed in an
    earlier period is not evidence for this one. Bilal Virk re-sent INV-0016
    and Jay Jhala re-sent 20260802 for the fortnight ending 30 August 2026;
    both were filed without complaint, and the wrong fortnight's timesheets
    were then a single dry-run away from being attached to a client invoice.

    When a message carries a duplicate invoice number the WHOLE MESSAGE is
    refused - its timesheets too, because they belong to the period the invoice
    covers, not to this one. That is what the _QUERY folder was doing by hand,
    done at the point where the decision is actually made. The person still
    counts as having sent, so they are not also reported as silent; the
    "duplicates" list is the chase list.
    """
    roster = contractors if contractors is not None else load_contractors()
    in_play = [c for c in roster if in_scope(c, cadence)]
    lo, hi = period_window(period_end, grace_days)

    unmatched, out_of_period, seen = [], [], set()
    duplicates: list[dict] = []
    staged: list[dict] = []
    body_staged: list[dict] = []

    for msg in messages:
        who, why = match_message(msg, roster, own_domains)
        if not who:
            unmatched.append({
                "sender": _addr(msg.get("sender", "")),
                "subject": msg.get("subject", ""),
                "received": msg.get("received", ""),
                "reason": why,
            })
            continue
        if not in_scope(who, cadence):
            continue

        recv = _as_date(msg.get("received"))
        end_d = period_end if isinstance(period_end, date) else date.fromisoformat(str(period_end))

        # The window is the PERSON'S, not the run's. A monthly contractor's
        # invoice states a month and can never fit a fortnight; judging it
        # against one refused every monthly invoice ever sent.
        p_lo, p_hi = contractor_span(who, end_d, grace_days)
        monthly = (p_lo, p_hi) != (lo, hi)

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
        msg_verdict = period_verdict(f"{subject} {names}", end_d, grace_days,
                                     not_after=recv, span=(p_lo, p_hi))
        if msg_verdict == "unknown":
            msg_verdict = "in" if (recv and p_lo <= recv <= p_hi) else "out"

        # Does this message carry an invoice as a real, named document? If so its
        # unnamed images are the timesheets behind it. Judged on the filename
        # alone - the subject is what got this wrong in the first place.
        has_doc_invoice = any(
            not _generic_name(a.get("name", ""))
            and classify(a.get("name", ""), a.get("contentType", "")) == "invoice"
            for a in msg.get("attachments", []) or []
        )

        # Refuse the whole message if its invoice number has been billed before.
        # Its timesheets go with it - they cover the period that invoice covers.
        dup = find_duplicate(msg, (prior_invoice_numbers or {}).get(
            str(who.get("item_code") or "")), has_doc_invoice)
        if dup:
            duplicates.append({
                "contractor": who["name"], "item_code": who.get("item_code", ""),
                "file": dup[0], "number": dup[1], "used_for": dup[2],
                "subject": subject,
                "received": recv.isoformat() if recv else "",
                "attachments": len(msg.get("attachments", []) or []),
            })
            # They did send something. Saying "NOT SENT ANYTHING" as well would
            # be two chase lines for one problem, and the quieter one is wrong.
            seen.add(who["name"])
            continue

        filed_any = False
        saw_attachment = False
        for a in msg.get("attachments", []) or []:
            kind = classify(a.get("name", ""), a.get("contentType", ""),
                            bool(a.get("isInline")), subject,
                            has_document_invoice=has_doc_invoice,
                            size=a.get("size"))
            if kind == "signature":
                continue
            # Counted before the admin and period filters. A message that
            # carried a document is not a body-only timesheet, whatever we
            # decided to do with that document.
            saw_attachment = True
            if kind == "admin" and not file_admin:
                continue

            name = str(a.get("name", "") or "")
            verdict = period_verdict(f"{subject} {name}", end_d, grace_days,
                                     not_after=recv, span=(p_lo, p_hi))
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
                    "reason": ("stated period is outside "
                               f"{p_lo} to {p_hi}"
                               + (" (monthly cycle)" if monthly else " (fortnight)")),
                    "window": f"{p_lo} to {p_hi}",
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

        # Nothing attached but the signature block, and the message belongs to
        # this fortnight: the timesheet is the email. Save it whole.
        if not saw_attachment and msg_verdict == "in":
            body_staged.append({
                "contractor": who["name"], "item_code": who["item_code"],
                "folder": who["folder"], "message_id": msg.get("id"),
                "subject": subject, "received": recv, "_who": who,
            })
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

    # Same part-numbering rule as the attachments: across the whole plan, per
    # contractor, so two body-only emails from one person cannot collide.
    body_only: list[dict] = []
    bgroups: dict[str, list[dict]] = {}
    for item in body_staged:
        bgroups.setdefault(item["contractor"], []).append(item)
    for _c, group in bgroups.items():
        group.sort(key=lambda x: (x["received"] or date.min, str(x["message_id"] or "")))
        for i, item in enumerate(group, start=1):
            body_only.append({
                "contractor": item["contractor"],
                "item_code": item["item_code"],
                "kind": "timesheet",
                "message_id": item["message_id"],
                "subject": item["subject"],
                "received": item["received"].isoformat() if item["received"] else "",
                "path": target_path(item["_who"], "timesheet", period_end,
                                    "message.eml", i, len(group)),
            })

    missing = [c["name"] for c in in_play if c["name"] not in seen]
    return {"files": sorted(files, key=lambda f: f["path"]),
            "unmatched": unmatched,
            "missing": sorted(missing),
            "out_of_period": out_of_period,
            "body_only": sorted(body_only, key=lambda f: f["path"]),
            "duplicates": sorted(duplicates,
                                 key=lambda d: (d["contractor"], d["file"]))}


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
