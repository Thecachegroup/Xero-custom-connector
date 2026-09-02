"""The roster is derived from Xero, not listed in a file.

Fixtures are the real item codes, item names and addresses as they stood on
2 September 2026 - including the ones that caused trouble: an item name that is
a fuller legal form than anyone uses ("Dat Tien Le"), a supplier whose trading
name says nothing about the contractor ("D & L Solutions Pty Ltd"), two people
with the same surname, and a contractor set up with an item but no template.
"""
from src import mail_mappers as mm
from src import roster as rst


def item(code, name, status="ACTIVE"):
    return {"Code": code, "Name": name, "Status": status}


ITEMS = [
    item("Linfox - DL", "Devinia Liddelow"),
    item("Linfox - DTL", "Dat Tien Le"),
    item("Linfox - KBJ", "Kshitija Jachak"),
    item("Linfox - EK", "Emily Kimmins"),
    item("Linfox - JG", "Jerry Gonsalves"),
    item("Linfox - DV", "Don Vuong"),
    item("Linfox - BVIRK", "Bilal Virk"),
    item("Linfox - Mali", "Mudassir Ali"),
    item("Linfox - MAZ", "Mazher Ali"),
    item("Linfox - RAA", "Richa Arora"),
    item("Linfox - KCrabb", "Karen Crabb"),
    item("Linfox - DBL", "Deepti Bansal"),
    item("Tec - PS", "Peter Small"),
    item("Passthru - Expenses", "Passthru Contractor Expenses"),
    item("zLinfox - MA", "Mazher Ali"),          # archived, old rate
    item("zLinfox - MAZ", "Mazher Ali"),         # archived, old rate
    item("zLinfox - CS", "Carla Saunders"),      # long gone
]

EMPLOYEES = [
    {"FirstName": "Devinia", "LastName": "Liddelow", "Email": "devinia_liddelow@linfox.com"},
    {"FirstName": "Dat", "LastName": "Le", "Email": "dat_le@linfox.com"},
    {"FirstName": "Kshitija", "LastName": "Jachak", "Email": "jachakkshitija@gmail.com"},
    {"FirstName": "Emily", "LastName": "Kimmins", "Email": "emily_kimmins@linfox.com"},
    {"FirstName": "Jerry", "LastName": "Gonsalves", "Email": "jerry_gonsalves@linfox.com"},
]


def bill(contact, email, *codes):
    return {"Type": "ACCPAY", "Status": "AUTHORISED",
            "Contact": {"Name": contact, "EmailAddress": email},
            "LineItems": [{"ItemCode": c} for c in codes]}


REPEATING = [
    bill("D & L Solutions Pty Ltd", "donvuong@mail.com", "Linfox - DV"),
    bill("Techne IT Consulting Pty Ltd", "techneitconsulting@gmail.com", "Linfox - BVIRK"),
    bill("Datacraft Consulting Services Pty Ltd", "mudassirali27@outlook.com", "Linfox - Mali"),
    bill("RICHA ARORA", "richaarora16@gmail.com", "Linfox - RAA"),
    bill("Karen Crabb Consulting Ltd", "karenmareecrabb@gmail.com", "Linfox - KCrabb"),
    bill("Peter Small", "pjs.ucanemailme@gmail.com", "Tec - PS"),
    bill("Deepti Bansal", "bansal.deepti90@gmail.com", "Linfox - DBL"),
    # A sales template, not a bill. It must not create a roster entry on its own.
    {"Type": "ACCREC", "Status": "AUTHORISED",
     "Contact": {"Name": "Linfox ADIT - DFN"},
     "LineItems": [{"ItemCode": "Linfox - EK"}]},
]

OVERRIDES = {
    "Linfox - DTL": {"name": "Dat Le", "folder": "Linfox_Dat Le"},
    "Linfox - EK": {"emails": ["emilykimmins@hotmail.com"]},
    "Linfox - DBL": {"folder": "Linfox_Deepati Bansal Monthly", "cadence": "monthly"},
}


def resolve(name):
    """Stand-in for mappers.build_employee_code_map, which is tested elsewhere."""
    return {"devinia liddelow": "Linfox - DL", "dat le": "Linfox - DTL",
            "kshitija jachak": "Linfox - KBJ", "emily kimmins": "Linfox - EK",
            "jerry gonsalves": "Linfox - JG"}.get(name.lower())


def build():
    return rst.build(ITEMS, EMPLOYEES, REPEATING,
                     resolve_employee=resolve, overrides=OVERRIDES)


def by_code(people):
    return {p["item_code"]: p for p in people}


# ------------------------------------------------------------------ the roster

def test_everyone_set_up_to_be_paid_is_on_the_roster():
    codes = set(by_code(build()))
    assert codes == {
        "Linfox - DL", "Linfox - DTL", "Linfox - KBJ", "Linfox - EK",
        "Linfox - JG", "Linfox - DV", "Linfox - BVIRK", "Linfox - Mali",
        "Linfox - RAA", "Linfox - KCrabb", "Linfox - DBL", "Tec - PS",
    }


def test_a_new_starter_needs_no_config_entry():
    """Jerry started 17/08/2026. Nobody added him anywhere, and that was the bug."""
    jerry = by_code(build())["Linfox - JG"]
    assert jerry["name"] == "Jerry Gonsalves"
    assert jerry["kind"] == "PAYG"
    assert jerry["folder"] == "Linfox_Jerry Gonsalves"


def test_archived_items_are_not_on_the_roster():
    codes = set(by_code(build()))
    assert not any(c.lower().startswith("z") for c in codes)


def test_a_non_person_item_stays_out_without_being_named():
    """Passthru - Expenses has no employee and no repeating bill behind it."""
    assert "Passthru - Expenses" not in by_code(build())


def test_a_sales_template_alone_does_not_make_someone_abn():
    assert by_code(build())["Linfox - EK"]["kind"] == "PAYG"


def test_the_supplier_is_linked_through_the_bills_line_not_its_name():
    """'D & L Solutions Pty Ltd' says Don Vuong nowhere. The item code does."""
    don = by_code(build())["Linfox - DV"]
    assert don["kind"] == "ABN"
    assert "D & L Solutions Pty Ltd" in don["contact_names"]
    assert "donvuong@mail.com" in don["emails"]


def test_overrides_supply_only_what_xero_cannot_know():
    people = by_code(build())
    assert people["Linfox - DTL"]["name"] == "Dat Le"          # item says "Dat Tien Le"
    assert people["Linfox - DTL"]["folder"] == "Linfox_Dat Le"
    assert people["Linfox - DBL"]["cadence"] == "monthly"
    assert people["Linfox - DBL"]["folder"] == "Linfox_Deepati Bansal Monthly"
    assert "emilykimmins@hotmail.com" in people["Linfox - EK"]["emails"]


def test_the_folder_is_derived_from_the_item_code_and_name():
    people = by_code(build())
    assert people["Tec - PS"]["folder"] == "Tec_Peter Small"
    assert people["Linfox - KCrabb"]["folder"] == "Linfox_Karen Crabb"


def test_everyone_defaults_to_fortnightly():
    assert by_code(build())["Linfox - DL"]["cadence"] == "fortnightly"


# ------------------------------------------------------------------ the gaps

def test_an_item_with_no_way_to_be_paid_is_reported():
    """Mazher Ali, 2 September 2026: five days worked, no template, no invoice."""
    items = ITEMS + [item("Linfox - MAZX", "Someone New")]
    people = rst.build(items, EMPLOYEES, REPEATING,
                       resolve_employee=resolve, overrides=OVERRIDES)
    codes = [g["item_code"] for g in rst.gaps(items, people)]
    assert "Linfox - MAZX" in codes
    assert "Linfox - MAZ" in codes           # item exists, no template either
    assert "Linfox - DL" not in codes


def test_gaps_ignores_archived_items():
    people = build()
    assert not any(g["item_code"].lower().startswith("z")
                   for g in rst.gaps(ITEMS, people))


def test_one_person_carrying_several_live_items_is_reported():
    dupes = {d["name"]: d["item_codes"] for d in rst.duplicates(ITEMS)}
    assert dupes["mazherali"] == ["Linfox - MAZ", "zLinfox - MA", "zLinfox - MAZ"]
    assert "devinialiddelow" not in dupes


# ------------------------------------------------- matching a message by name

ROSTER = build()


def msg(sender, subject="Timesheet", sender_name="", body="", cc=None, atts=None):
    return {"id": "m1", "sender": sender, "sender_name": sender_name,
            "subject": subject, "body": body, "cc": cc or [], "reply_to": [],
            "received": "2026-09-01T09:00:00Z",
            "attachments": atts if atts is not None else
            [{"id": "a", "name": "image.png", "contentType": "image/png",
              "isInline": True, "size": 90000}]}


def test_every_real_contractor_address_resolves_by_name_alone():
    """No address is on file for anyone. The address itself carries the name."""
    cases = {
        "Devinia_Liddelow@linfox.com": "Devinia Liddelow",
        "dat_le@linfox.com": "Dat Le",
        "jachakkshitija@gmail.com": "Kshitija Jachak",      # surname first
        "Jerry_Gonsalves@linfox.com": "Jerry Gonsalves",
        "donvuong@mail.com": "Don Vuong",
        "mudassirali27@outlook.com": "Mudassir Ali",        # trailing digits
        "richaarora16@gmail.com": "Richa Arora",
        "karenmareecrabb@gmail.com": "Karen Crabb",         # middle name in the way
        "techneitconsulting@gmail.com": "Bilal Virk",       # trading name, not his
    }
    bare = [{**p, "emails": []} for p in ROSTER]
    for address, expected in cases.items():
        who, why = mm.match_by_name(msg(address), bare)
        assert who is not None, f"{address} matched nobody ({why})"
        assert who["name"] == expected, f"{address} -> {who['name']}, not {expected}"


def test_two_people_sharing_a_surname_are_not_confused():
    """Mudassir Ali left on 21/08/2026 and his brother Mazher took the seat.

    Both are live, both are "... Ali", and their item codes are one character
    apart. Getting this wrong pays one brother against the other's contract.
    """
    both = REPEATING + [bill("Mazher Ali", "mdmazherali@gmail.com", "Linfox - MAZ")]
    people = rst.build(ITEMS, EMPLOYEES, both,
                       resolve_employee=resolve, overrides=OVERRIDES)
    bare = [{**p, "emails": []} for p in people]
    assert mm.match_by_name(msg("mudassirali27@outlook.com"), bare)[0]["item_code"] \
        == "Linfox - Mali"
    assert mm.match_by_name(msg("mdmazherali@gmail.com"), bare)[0]["item_code"] \
        == "Linfox - MAZ"


def test_a_display_name_on_its_own_is_never_enough():
    """Display names are set by the sender, and these documents move money."""
    m = msg("accounts@somewhere.com", subject="Invoice", sender_name="Don Vuong")
    who, why = mm.match_by_name(m, [{**p, "emails": []} for p in ROSTER])
    assert who is None
    assert "no name" in why or "more than one" in why


def test_a_display_name_plus_a_subject_line_is_enough():
    m = msg("pjs.ucanemailme@gmail.com", subject="Peter Small - TecAlliance hours",
            sender_name="Peter Small")
    who, _why = mm.match_by_name(m, [{**p, "emails": []} for p in ROSTER])
    assert who is not None and who["name"] == "Peter Small"


def test_an_ambiguous_message_is_reported_and_never_guessed():
    twins = [{"name": "Sam Taylor", "item_code": "X - 1", "contact_names": [], "emails": []},
             {"name": "Sam Taylor", "item_code": "X - 2", "contact_names": [], "emails": []}]
    who, why = mm.match_by_name(msg("sam_taylor@example.com"), twins)
    assert who is None
    assert "more than one" in why


def test_a_soft_signal_on_its_own_is_not_a_match():
    """A name in the subject and nothing else. Could be anybody talking ABOUT them."""
    m = msg("someone@example.com", subject="FW: Don Vuong timesheet", body="")
    who, why = mm.match_by_name(m, [{**p, "emails": []} for p in ROSTER])
    assert who is None
    assert "no name" in why


def test_a_forward_from_us_is_not_attributed_by_name():
    """Andrew forwarding 'RE: louis_soto@linfox.com' is not from Louis.

    Two pieces of evidence here - subject and body - which is enough for anyone
    else. From one of our own addresses it is refused anyway: the sender is the
    only thing that says who a message is FROM, and a forward names somebody
    else by definition.
    """
    m = msg("andrew.hurnard@thecachegroup.com.au",
            subject="FW: Don Vuong timesheet",
            body="Don Vuong sent this through, can you file it")
    bare = [{**p, "emails": []} for p in ROSTER]
    assert mm.match_by_name(m, bare)[0] is not None      # would match anyone else
    who, why = mm.match_message(m, bare, own_domains=("thecachegroup.com.au",))
    assert who is None
    assert "sender identity leads" in why


def test_a_forward_from_us_still_follows_an_address_on_file():
    m = msg("andrew.hurnard@thecachegroup.com.au", subject="FW: timesheet",
            body="From: donvuong@mail.com\nHere are my days")
    who, why = mm.match_message(m, ROSTER, own_domains=("thecachegroup.com.au",))
    assert who is not None and who["name"] == "Don Vuong"


def test_a_billing_system_is_flagged_rather_than_dismissed():
    m = msg("email-reckonone@reckon.com", subject="Document from Reckon", cc=[])
    who, why = mm.match_message(m, [{**p, "emails": []} for p in ROSTER])
    assert who is None
    assert "OPEN IT" in why


def test_a_billing_system_resolves_when_the_person_is_in_the_cc():
    m = msg("email-reckonone@reckon.com", subject="Document from Reckon",
            cc=["richaarora16@gmail.com"])
    who, _why = mm.match_message(m, [{**p, "emails": []} for p in ROSTER])
    assert who is not None and who["name"] == "Richa Arora"


def test_an_address_on_file_still_wins_over_everything():
    who, why = mm.match_message(msg("devinia_liddelow@linfox.com"), ROSTER)
    assert who["name"] == "Devinia Liddelow"
    assert why == "sender address is on file"


def test_a_stranger_is_still_a_stranger():
    who, why = mm.match_message(msg("mdeane@seek.com.au", subject="SEEK invoice"),
                                ROSTER)
    assert who is None
