"""Who is on the payroll roster this fortnight - built from Xero, not a list.

WHY THIS EXISTS. The roster used to live in config/contractor_mail.json: a
hand-maintained table of addresses, one entry per person. Every joiner needed a
commit and a redeploy, and when that did not happen the sweep did not merely
miss their paperwork - it never looked for them at all. Jerry Gonsalves started
on 17 August 2026 and his timesheet landed in UNMATCHED SENDERS for the
fortnight ending 30 August; Mazher Ali started on the 24th and was not looked
for at all. Andrew, 21 Aug 2026: "I don't wanna be you changing the email every
time someone joins or leaves the business."

He is right, and a list is not needed. To pay or bill anybody at all, they must
already exist in Xero:

    PAYG people are payroll employees, and their name resolves to an inventory
    item code through the same map the invoice check has used for months.

    ABN people are the contact on a REPEATING BILL, and the bill's line carries
    the item code. That is the link - a contact on its own says nothing about
    which contractor it is.

So the roster is derived at run time and cannot drift. A leaver drops off when
their template stops. A joiner appears the moment they are set up to be paid,
which is the same moment they can send a timesheet.

WHAT XERO CANNOT KNOW is kept in config/roster_overrides.json, and it is small
by design: the folder name where it does not match the person's name (ten years
of folders carry typos - "Deepati Bansal", "Saied Almer" - and correcting them
here would break the match against every existing file), the monthly cadence of
the offshore team, and any extra personal address someone sends from. Nothing
in it is required for a person to appear.

A person who is set up in Xero but has no repeating template - Mazher Ali on
2 September 2026, five days worked and nothing raised - is reported by
`gaps()`. That is the coverage failure Andrew asked to be caught: "someone that
is in the spreadsheet that isn't being paid or vice versa."
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable

# Items whose code starts with this are archived. Long-standing TCG convention:
# a finished contractor's item is renamed with a leading z so it sorts to the
# bottom of the rate card rather than being deleted, which would break history.
ARCHIVE_PREFIX = "z"

OVERRIDES_PATH = "config/roster_overrides.json"


def _key(value: Any) -> str:
    """A name reduced to something comparable. Punctuation and case are noise."""
    return re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower()).strip()


def _squash(value: Any) -> str:
    return _key(value).replace(" ", "")


def load_overrides(path: str | None = None) -> dict:
    p = path or os.environ.get("TCG_ROSTER_OVERRIDES", OVERRIDES_PATH)
    try:
        with open(p) as fh:
            return (json.load(fh) or {}).get("by_item_code", {}) or {}
    except Exception:                                          # noqa: BLE001
        return {}


def client_of(item_code: str) -> str:
    """The client prefix on a TCG item code: 'Linfox - DL' -> 'Linfox'.

    It is the first half of the folder name, and it is why the codes are shaped
    this way in the first place.
    """
    code = str(item_code or "")
    return code.split(" - ", 1)[0].strip() if " - " in code else code.strip()


def is_archived(item_code: str) -> bool:
    return str(item_code or "").lower().startswith(ARCHIVE_PREFIX)


def _emails(*values: Any) -> list[str]:
    """Every distinct, plausible address out of whatever was handed in."""
    out: list[str] = []
    for v in values:
        for part in re.split(r"[;,\s]+", str(v or "")):
            a = part.strip().lower().strip("<>")
            if "@" in a and "." in a.rsplit("@", 1)[-1] and a not in out:
                out.append(a)
    return out


def name_aliases(path: str | None = None) -> dict[str, list[str]]:
    """Other spellings of a person's name, keyed by item code.

    config/employee_codes.json already carries these - it exists so the invoice
    check can match a payroll employee to an item whatever the spelling, and it
    is maintained whenever unmatched_employees reports somebody. It lists
    "Bhasker Veerla" AND "Bhasker Veela" against Linfox - BV, which is exactly
    the transposition that stopped his email being recognised. Reusing it here
    costs nothing and means one table is kept, not two.
    """
    p = path or os.environ.get("TCG_EMPLOYEE_CODES", "config/employee_codes.json")
    try:
        with open(p) as fh:
            raw = (json.load(fh) or {}).get("map", {}) or {}
    except Exception:                                          # noqa: BLE001
        return {}
    out: dict[str, list[str]] = {}
    for name, code in raw.items():
        if name and code:
            out.setdefault(str(code), []).append(str(name))
    return out


def build(items: Iterable[dict], employees: Iterable[dict],
          repeating: Iterable[dict], contacts: Iterable[dict] | None = None,
          resolve_employee=None, overrides: dict | None = None,
          aliases: dict | None = None) -> list[dict]:
    """The roster, in the same shape the old config file produced.

    Every entry: name, item_code, folder, kind, cadence, emails, contact_names.
    Downstream code does not know or care where it came from.

    ITEMS are the spine - a contractor without an inventory item cannot be
    billed, so there is nothing to file for them. Archived (z-prefixed) and
    inactive items are dropped. So is any item that resolves to neither a
    payroll employee nor a repeating bill: that is how 'Passthru - Expenses'
    stays out without being named here.
    """
    overrides = load_overrides() if overrides is None else overrides
    aliases = name_aliases() if aliases is None else aliases
    by_code: dict[str, dict] = {}

    live = {}
    for it in items or []:
        code = str(it.get("Code") or it.get("*ItemCode") or "").strip()
        if not code or is_archived(code):
            continue
        if str(it.get("Status", "ACTIVE")).upper() not in ("ACTIVE", ""):
            continue
        live[code] = str(it.get("Name") or "").strip()

    # ---- PAYG: payroll employees, name -> item code -------------------------
    for e in employees or []:
        full = " ".join(x for x in [str(e.get("FirstName") or ""),
                                    str(e.get("LastName") or "")] if x).strip()
        if not full:
            continue
        code = resolve_employee(full) if resolve_employee else None
        if not code or code not in live:
            continue
        by_code.setdefault(code, {
            "name": live[code] or full, "item_code": code, "kind": "PAYG",
            "emails": [], "contact_names": [], "source": "payroll employee",
        })
        by_code[code]["emails"] = _emails(*by_code[code]["emails"],
                                          e.get("Email"), full and "")
        by_code[code]["payroll_name"] = full

    # ---- ABN: the contact on a repeating BILL, via its line item ------------
    # A contact on its own proves nothing: "D & L Solutions Pty Ltd" does not
    # say Don Vuong anywhere. The repeating bill's line carries the item code,
    # and that is the only reliable link between a supplier and a contractor.
    for r in repeating or []:
        if str(r.get("Type") or "").upper() != "ACCPAY":
            continue
        if str(r.get("Status") or "AUTHORISED").upper() == "DELETED":
            continue
        contact = r.get("Contact") or {}
        cname = str(contact.get("Name") or "").strip()
        for line in r.get("LineItems") or []:
            code = str(line.get("ItemCode") or "").strip()
            if not code or code not in live or is_archived(code):
                continue
            entry = by_code.setdefault(code, {
                "name": live[code] or cname, "item_code": code, "kind": "ABN",
                "emails": [], "contact_names": [], "source": "repeating bill",
            })
            if cname and cname not in entry["contact_names"]:
                entry["contact_names"].append(cname)
            entry["emails"] = _emails(*entry["emails"],
                                      contact.get("EmailAddress"))

    # ---- contact detail, for addresses the repeating bill did not carry -----
    wanted = {_key(n): c for c in by_code.values() for n in c["contact_names"]}
    for c in contacts or []:
        entry = wanted.get(_key(c.get("Name")))
        if not entry:
            continue
        extra = [c.get("EmailAddress")] + [
            p.get("EmailAddress") for p in (c.get("ContactPersons") or [])]
        entry["emails"] = _emails(*entry["emails"], *extra)

    # ---- what Xero cannot know ---------------------------------------------
    roster = []
    for code, entry in by_code.items():
        ov = overrides.get(code, {}) or {}
        # A payroll employee who is not a swept contractor. Nuria Carricondo is
        # in the pay run for superannuation only and Samuel Ferrie and Shane
        # Bell are monthly salaried staff Andrew runs himself - all three are
        # real employees, so the roster finds them, and all three were being
        # chased every fortnight for a timesheet that does not exist.
        if ov.get("skip") or ov.get("ignore"):
            continue
        name = str(ov.get("name") or entry["name"] or "").strip()
        entry["name"] = name
        # CADENCE CAN BE SPLIT. Peter Small is billed fortnightly and invoiced
        # monthly, so his override carries a dict. str() on a dict produced
        # "{'sales': 'monthly', ...}", which matched neither cadence and made
        # in_scope() unable to place him at all. What HE sends is his own
        # invoice - the bill side - so that is the side the sweep reads.
        cad = ov.get("cadence") or "fortnightly"
        if isinstance(cad, dict):
            cad = cad.get("bills") or cad.get("sales") or "fortnightly"
        entry["cadence"] = str(cad).lower()
        # The day a MONTHLY cycle turns over, where it is offset from the
        # calendar month. Prasanthi runs the 12th to the 11th. Without this on
        # the roster entry the sweep cannot judge her documents at all.
        try:
            entry["period_day"] = int(ov["period_day"])
        except (KeyError, TypeError, ValueError):
            entry["period_day"] = None
        entry["kind"] = str(ov.get("kind") or entry["kind"]).upper()
        entry["folder"] = ov.get("folder") or f"{client_of(code)}_{name}"
        entry["emails"] = _emails(*entry["emails"], *(ov.get("emails") or []))
        for n in list(ov.get("contact_names") or []) + list(aliases.get(code) or []):
            if n and n not in entry["contact_names"]:
                entry["contact_names"].append(n)
        # THE ITEM NAME CARRIES A ROLE, THE EMAIL DOES NOT. Andrew added the
        # role to Xero on 5 Sep 2026 so Linfox AP can match a PO, and the item
        # `Linfox - MAZ` went from "Mazher Ali" to "Mazher Ali - Power
        # Platforms". The roster name follows the item, name matching looks for
        # that whole string, and Mazher's invoice stopped being attributed the
        # same day - it landed in UNMATCHED and he was reported as having sent
        # nothing. Every peer carries "Name - Role" in the same field, so this
        # is a trap set for all of them. Keep the bare name as an alias.
        bare = re.split(r"\s+[-\u2013]\s+", name)[0].strip()
        if bare and bare != name and bare not in entry["contact_names"]:
            entry["contact_names"].append(bare)
        # The payroll name too - "Dat Le" where the item says "Dat Tien Le".
        pn = entry.pop("payroll_name", "")
        if pn and pn != name and pn not in entry["contact_names"]:
            entry["contact_names"].append(pn)
        roster.append(entry)

    return sorted(roster, key=lambda c: c["name"].lower())


def looks_like_a_person(name: str) -> bool:
    """Two to four plain words. A last-resort filter, behind the overrides.

    The coverage report is only read if everything in it is a person. Left
    unfiltered it carries SEEK ads, LinkedIn training and pass-through expenses,
    and a report nobody reads catches nothing.
    """
    n = str(name or "").strip()
    if not n or any(ch.isdigit() for ch in n):
        return False
    words = n.split()
    if not 2 <= len(words) <= 4:
        return False
    if any(w in _key(n).split() for w in
           ("ad", "ads", "training", "expenses", "articles", "hourly", "leadership",
            "standout", "contractor", "seek", "fee", "fees", "licence", "software")):
        return False
    return all(w.replace("'", "").replace("-", "").replace(".", "").isalpha()
               for w in words)


def gaps(items: Iterable[dict], roster: Iterable[dict],
         overrides: dict | None = None) -> list[dict]:
    """Live items with nobody behind them - work that cannot be billed.

    An inventory item is NOT enough to raise a draft: the repeating templates
    do that, and a person can have a correct item, a signed brief and a
    spreadsheet row while being completely invisible to list_period_drafts.
    Mazher Ali worked five days that way. This is the check that finds it.
    """
    overrides = load_overrides() if overrides is None else overrides
    have = {c["item_code"] for c in roster}
    out = []
    for it in items or []:
        code = str(it.get("Code") or it.get("*ItemCode") or "").strip()
        if not code or is_archived(code) or code in have:
            continue
        if str(it.get("Status", "ACTIVE")).upper() not in ("ACTIVE", ""):
            continue
        ov = overrides.get(code, {}) or {}
        if ov.get("ignore") or ov.get("skip"):
            continue
        name = str(it.get("Name") or "").strip()
        if not looks_like_a_person(name):
            continue
        out.append({"item_code": code, "name": name,
                    "why": "active item with no payroll employee and no repeating bill"})
    return sorted(out, key=lambda g: g["item_code"])


def duplicates(items: Iterable[dict]) -> list[dict]:
    """The same person carrying more than one live item, usually at stale rates.

    'Mazher Ali' had three on 2 September 2026: Linfox - MAZ at the current
    1000/1225 and zLinfox - MA and zLinfox - MAZ still Active at an old
    900/1103. Archived ones are excluded from the roster but they are still
    sitting in the rate card where somebody can bill off them.
    """
    seen: dict[str, list[str]] = {}
    for it in items or []:
        code = str(it.get("Code") or it.get("*ItemCode") or "").strip()
        name = _squash(it.get("Name"))
        if not code or not name:
            continue
        if str(it.get("Status", "ACTIVE")).upper() not in ("ACTIVE", ""):
            continue
        seen.setdefault(name, []).append(code)
    return [{"name": n, "item_codes": sorted(codes)}
            for n, codes in sorted(seen.items()) if len(codes) > 1]
