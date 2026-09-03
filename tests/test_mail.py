"""Filing decisions: who sent it, what it is, where it goes."""
from datetime import date

from src import mail_mappers as mm

ROSTER = mm.load_contractors("config/contractor_mail.json")


# ---------------------------------------------------------------- sender

def test_matches_on_address_not_display_name():
    assert mm.match_sender("Dat Le <Dat_Le@linfox.com>")["item_code"] == "Linfox - DTL"
    assert mm.match_sender("DAT_LE@LINFOX.COM")["item_code"] == "Linfox - DTL"


def test_second_address_for_the_same_person_matches():
    """Emily sends from Linfox with her Hotmail CC'd. Both are her."""
    a = mm.match_sender("Emily_Kimmins@linfox.com")
    b = mm.match_sender("emilykimmins@hotmail.com")
    assert a["name"] == b["name"] == "Emily Kimmins"


def test_unknown_sender_returns_none_rather_than_guessing():
    assert mm.match_sender("someone.else@example.com") is None
    assert mm.match_sender("") is None


# ---------------------------------------------------------------- classify

def test_named_invoice_is_an_invoice():
    assert mm.classify("Invoice-0011.pdf") == "invoice"
    assert mm.classify("100003 KC invoice to 2026.8.2.pdf") == "invoice"


def test_named_timesheet_is_a_timesheet():
    assert mm.classify("TCG Weekly Timesheet - Karen Crabb 26.8.2.pdf") == "timesheet"


def test_inline_screenshot_with_no_useful_name_is_a_timesheet():
    """Every PPM screenshot arrives as image.png. That is the common case."""
    assert mm.classify("image.png", "image/png", is_inline=True,
                       subject="Timesheets 3rd - 14th August") == "timesheet"
    assert mm.classify("image001.png", "image/png", is_inline=True) == "timesheet"
    assert mm.classify("tmp_4e0bcaab-d008.png", "image/png", is_inline=True) == "timesheet"


def test_inline_image_on_an_invoice_email_follows_the_subject():
    assert mm.classify("image.png", "image/png", is_inline=True,
                       subject="Invoice Submission for Work Completed") == "invoice"


def test_filename_beats_subject():
    """A named file is the sender's own statement of what it is."""
    assert mm.classify("Invoice-0011.pdf", subject="timesheets for the fortnight") == "invoice"


# ---------------------------------------------------------------- paths

def test_fortnight_folder_keeps_the_double_space():
    assert mm.fortnight_folder(date(2026, 8, 16)) == "Fortnight  Ending 16082026"
    assert "  " in mm.fortnight_folder("2026-08-16")


def test_period_start_is_the_monday_thirteen_days_back():
    assert mm.period_start(date(2026, 8, 16)) == date(2026, 8, 3)


def test_target_path_uses_the_folder_exactly_as_it_exists_on_disk():
    kj = mm.match_sender("jachakkshitija@gmail.com")
    p = mm.target_path(kj, "timesheet", date(2026, 8, 16), "image.png", 1, 2)
    assert p == ("Fortnight  Ending 16082026/Linfox_Kshitija Jachak/"
                 "KJ_timesheet_2026-08-16_part1.png")


def test_single_file_gets_no_part_suffix():
    ma = mm.match_sender("mudassirali27@outlook.com")
    p = mm.target_path(ma, "invoice", date(2026, 8, 16), "Invoice-0011.pdf")
    assert p.endswith("MA_invoice_2026-08-16.pdf")


def test_folder_typos_are_preserved_because_the_folders_exist():
    """'Deepati' and 'Saied' are wrong but they are what is on disk."""
    folders = {c["folder"] for c in ROSTER}
    assert "Linfox_Deepati Bansal Monthly" in folders


# ---------------------------------------------------------------- planning

def _msg(sender, subject, atts, mid="m1"):
    return {"id": mid, "sender": sender, "subject": subject,
            "received": "2026-08-17", "attachments": atts}


def test_plan_files_two_inline_screenshots_as_parts_one_and_two():
    plan = mm.plan_filing([_msg(
        "jachakkshitija@gmail.com", "Timesheets 3rd - 14th August",
        [{"id": "a1", "name": "image.png", "isInline": True},
         {"id": "a2", "name": "image.png", "isInline": True}],
    )], date(2026, 8, 16), ROSTER)
    paths = sorted(f["path"] for f in plan["files"])
    assert paths[0].endswith("KJ_timesheet_2026-08-16_part1.png")
    assert paths[1].endswith("KJ_timesheet_2026-08-16_part2.png")


def test_plan_separates_an_invoice_from_its_timesheet():
    plan = mm.plan_filing([_msg(
        "karenmareecrabb@gmail.com", "Karen Crabb - invoice",
        [{"id": "a1", "name": "100003 KC invoice to 2026.8.16.pdf"},
         {"id": "a2", "name": "TCG Weekly Timesheet - Karen Crabb 26.8.16.pdf"}],
    )], date(2026, 8, 16), ROSTER)
    kinds = {f["kind"] for f in plan["files"]}
    assert kinds == {"invoice", "timesheet"}


def test_plan_reports_unmatched_senders_loudly():
    plan = mm.plan_filing([_msg("nobody@nowhere.com", "Timesheet", [{"id": "a", "name": "x.pdf"}])],
                          date(2026, 8, 16), ROSTER)
    assert plan["files"] == []
    assert plan["unmatched"][0]["sender"] == "nobody@nowhere.com"


def test_plan_reports_who_has_not_sent_anything():
    plan = mm.plan_filing([_msg("dat_le@linfox.com", "Timesheets",
                                [{"id": "a", "name": "image.png", "isInline": True}])],
                          date(2026, 8, 16), ROSTER)
    assert "Kshitija Jachak" in plan["missing"]
    assert "Dat Le" not in plan["missing"]


def test_monthly_contractors_file_into_the_current_fortnight_folder():
    """Andrew's decision, 2 Sep 2026: one folder to look in, not two.

    Before this the fortnightly sweep filtered them out entirely, so Bhasker
    Veela's August invoices were never filed and his bill had to be given its
    evidence by hand.
    """
    plan = mm.plan_filing([_msg("bansal.deepti90@gmail.com", "August Invoice",
                                [{"id": "a", "name": "invoice.pdf"}])],
                          date(2026, 8, 16), ROSTER)
    assert len(plan["files"]) == 1
    assert plan["files"][0]["path"].startswith("Fortnight  Ending 16082026/")
    assert plan["unmatched"] == []


def test_a_cadence_can_still_be_named_to_narrow_the_sweep():
    plan = mm.plan_filing([_msg("bansal.deepti90@gmail.com", "August Invoice",
                                [{"id": "a", "name": "invoice.pdf"}])],
                          date(2026, 8, 16), ROSTER, cadence="fortnightly")
    assert plan["files"] == []
    assert plan["unmatched"] == []          # recognised, just not this run
    assert "Deepti Bansal" not in plan["missing"]


# --- period window and part numbering (defects found by the first dry run) ----

def _m(sender, subject, atts, received, mid):
    return {"id": mid, "sender": sender, "subject": subject,
            "received": received, "attachments": atts}


def test_previous_fortnights_are_excluded_not_filed():
    """The first dry run proposed filing four fortnights of Peter Small's
    invoices into the 16 Aug folder. Only the current one belongs."""
    msgs = [
        _m("pjs.ucanemailme@gmail.com", "Invoice 0021", [{"id": "a", "name": "Invoice_0021.pdf"}], "2026-07-05", "m1"),
        _m("pjs.ucanemailme@gmail.com", "Invoice 0022", [{"id": "b", "name": "Invoice_0022.pdf"}], "2026-07-19", "m2"),
        _m("pjs.ucanemailme@gmail.com", "Invoice 0023", [{"id": "c", "name": "Invoice_0023.pdf"}], "2026-08-02", "m3"),
        _m("pjs.ucanemailme@gmail.com", "Invoice 0024", [{"id": "d", "name": "Invoice_0024.pdf"}], "2026-08-17", "m4"),
    ]
    plan = mm.plan_filing(msgs, date(2026, 8, 16), ROSTER)
    assert len(plan["files"]) == 1
    assert plan["files"][0]["source_name"] == "Invoice_0024.pdf"
    assert len(plan["out_of_period"]) == 3


def test_two_messages_never_collide_on_a_filename():
    """Part numbers must run across the whole plan, not restart per message -
    otherwise both resolve to _part1 and skip-if-exists drops one silently."""
    msgs = [
        _m("jachakkshitija@gmail.com", "Timesheets wk1",
           [{"id": "a1", "name": "image.png", "isInline": True}], "2026-08-10", "m1"),
        _m("jachakkshitija@gmail.com", "Timesheets wk2",
           [{"id": "a2", "name": "image.png", "isInline": True}], "2026-08-17", "m2"),
    ]
    plan = mm.plan_filing(msgs, date(2026, 8, 16), ROSTER)
    paths = [f["path"] for f in plan["files"]]
    assert len(paths) == 2
    assert len(set(paths)) == 2, f"filenames collided: {paths}"


def test_onboarding_paperwork_is_not_filed_as_period_paperwork():
    """A passport is not a timesheet and does not belong in a fortnight folder."""
    assert mm.classify("KC passport.jpg") == "admin"
    assert mm.classify("TCG Contractor Details and Banking Form - Karen Crabb.docx") == "admin"
    plan = mm.plan_filing([_m("karenmareecrabb@gmail.com", "Docs",
                              [{"id": "a", "name": "KC passport.jpg"}],
                              "2026-08-17", "m1")], date(2026, 8, 16), ROSTER)
    assert plan["files"] == []


def test_don_vuong_now_resolves():
    """He was reported as having sent nothing; his config entry had no address."""
    assert mm.match_sender("donvuong@mail.com")["item_code"] == "Linfox - DV"


def test_period_window_runs_from_the_previous_period_end():
    lo, hi = mm.period_window(date(2026, 8, 16))
    assert lo == date(2026, 8, 3)
    assert hi == date(2026, 8, 26)


# --------------------------------------------------------------------------
# The period the document STATES beats the date it arrived
# --------------------------------------------------------------------------

def test_stated_dates_reads_dot_separated_year_first():
    """Karen Crabb's real filename. Year-first with dots parsed nothing before."""
    assert mm.stated_dates("100003 KC invoice to 2026.8.16.pdf", 2026) == [date(2026, 8, 16)]
    assert mm.stated_dates("Timesheet 2026.08.27.pdf", 2026) == [date(2026, 8, 27)]
    assert mm.stated_dates("Weeks Ending 2026-08-08", 2026) == [date(2026, 8, 8)]
    assert mm.stated_dates("period 2026/08/16", 2026) == [date(2026, 8, 16)]


def test_day_first_dates_still_read_day_first():
    """16.8.2026 is the sixteenth of August, not the eighth of ... whatever."""
    assert mm.stated_dates("Invoice 16.8.2026", 2026) == [date(2026, 8, 16)]
    assert mm.stated_dates("(02-08-2026 to 15-08-2026)", 2026) == [
        date(2026, 8, 2), date(2026, 8, 15)]


def test_stated_period_beats_received_date():
    assert mm.period_verdict("100003 KC invoice to 2026.8.16.pdf", date(2026, 8, 16)) == "in"
    assert mm.period_verdict("Timesheet 2026.08.30.pdf", date(2026, 8, 16)) == "out"
    assert mm.period_verdict("Timesheet.pdf", date(2026, 8, 16)) == "unknown"


def test_a_later_fortnight_is_not_filed_into_this_one():
    """States 30 Aug and was sent after it. Must not land in the 16 Aug folder."""
    msgs = [_m("karenmareecrabb@gmail.com", "Timesheet",
               [{"id": "a", "name": "Timesheet 2026.08.30.pdf"}], "2026-08-31", "m1")]
    assert mm.plan_filing(msgs, date(2026, 8, 16), ROSTER)["files"] == []


def test_a_date_after_the_send_date_is_not_a_period():
    """Karen Crabb, real email, 17 Aug 2026.

    Two attachments, covering note says both are for the same billing period.
    "2026.08.27" cannot be the period - nobody documents work they have not
    done. Read as one, it threw her timesheet out of its own fortnight.
    """
    assert mm.period_verdict("Timesheet 2026.08.27.pdf", date(2026, 8, 16),
                             not_after=date(2026, 8, 17)) == "unknown"
    msgs = [_m("karenmareecrabb@gmail.com", "Karen Crabb - invoice",
               [{"id": "a1", "name": "100003 KC invoice to 2026.8.16.pdf"},
                {"id": "a2", "name": "Timesheet 2026.08.27.pdf"}],
               "2026-08-17", "m1")]
    plan = mm.plan_filing(msgs, date(2026, 8, 16), ROSTER)
    assert len(plan["files"]) == 2
    assert {f["kind"] for f in plan["files"]} == {"invoice", "timesheet"}


def test_previous_fortnights_paperwork_is_excluded_by_its_stated_date():
    """2026.8.2 is the fortnight before. It arrived in the window; it is not ours."""
    plan = mm.plan_filing([_msg(
        "karenmareecrabb@gmail.com", "Karen Crabb - invoice",
        [{"id": "a1", "name": "100003 KC invoice to 2026.8.2.pdf"},
         {"id": "a2", "name": "TCG Weekly Timesheet - Karen Crabb 26.8.2.pdf"}],
    )], date(2026, 8, 16), ROSTER)
    assert plan["files"] == []
    assert len(plan["out_of_period"]) == 2
    assert "Karen Crabb" in plan["missing"]


def test_one_message_can_straddle_two_fortnights():
    """Judged per attachment: this fortnight's invoice files, the prior one does not."""
    plan = mm.plan_filing([_m(
        "karenmareecrabb@gmail.com", "Karen Crabb",
        [{"id": "a1", "name": "100003 KC invoice to 2026.8.16.pdf"},
         {"id": "a2", "name": "100003 KC invoice to 2026.8.2.pdf"}],
        "2026-08-17", "m1",
    )], date(2026, 8, 16), ROSTER)
    assert [f["source_name"] for f in plan["files"]] == ["100003 KC invoice to 2026.8.16.pdf"]
    assert [o["file"] for o in plan["out_of_period"]] == ["100003 KC invoice to 2026.8.2.pdf"]


def test_two_digit_year_first_is_read_as_this_year_not_2016():
    """26.8.16 is 16 Aug 2026. Day-first would make it Aug 2016 and lose the file."""
    assert mm.stated_dates("TCG Weekly Timesheet - Karen Crabb 26.8.16.pdf", 2026) == [
        date(2026, 8, 16)]
    assert mm.stated_dates("Invoice 15/08/26", 2026) == [date(2026, 8, 15)]
    assert mm.stated_dates("Invoice 02-08-2026", 2026) == [date(2026, 8, 2)]


def test_images_beside_a_real_invoice_are_timesheets():
    """Don, Mudassir, Jay and Bilal all send one invoice document plus timesheet
    images. The subject says "Invoice", which used to file the images as invoices."""
    msgs = [_m("donvuong@mail.com", "Invoice - D&L Solutions P/L",
               [{"id": "a1", "name": "Invoice#20260819.pdf"},
                {"id": "a2", "name": "2026-08-08_00-24-42.png"},
                {"id": "a3", "name": "2026-08-14_15-55-36.png", "isInline": True}],
               "2026-08-19", "m1")]
    plan = mm.plan_filing(msgs, date(2026, 8, 16), ROSTER)
    kinds = sorted(f["kind"] for f in plan["files"])
    assert kinds == ["invoice", "timesheet", "timesheet"], kinds


def test_an_invoice_only_email_still_files_its_image_as_an_invoice():
    """No invoice document present, subject says invoice - the image is the invoice."""
    assert mm.classify("image.png", is_inline=True, subject="Bilal Virk - Invoice") == "invoice"
    assert mm.classify("image.png", is_inline=True, subject="Bilal Virk - Invoice",
                       has_document_invoice=True) == "timesheet"


def test_an_hours_workbook_is_a_timesheet_even_when_named_invoice():
    """Peter Small's backing sheet is 'Invoice_0024-HOURS.xlsx'."""
    assert mm.classify("Peter Small_TecAlliance_(02-08-2026 to 15-08-2026) "
                       "Invoice_0024-HOURS.xlsx") == "timesheet"
    assert mm.classify("Peter Small_TecAlliance_(02-08-2026 to 15-08-2026) "
                       "Invoice_0024.pdf") == "invoice"


# ------------------------------------------------------- forwards from our side

FWD_BODY = """Regards

Andrew Hurnard
Director
The Cache Group
0417 037 451


From: Karen Crabb <karenmareecrabb@gmail.com>
Sent: Tuesday, 18 August 2026 4:43 AM
To: Payroll - TCG <Payroll@thecachegroup.com.au>
Cc: Andrew Hurnard <andrew.hurnard@thecachegroup.com.au>
Subject: Karen Crabb - invoice
"""
OWN = ("thecachegroup.com.au",)


def test_a_forward_from_us_resolves_to_the_original_sender():
    msg = {"sender": "andrew.hurnard@thecachegroup.com.au", "body": FWD_BODY}
    assert mm.match_sender_or_forward(msg, ROSTER, OWN)["name"] == "Karen Crabb"


def test_the_body_is_only_consulted_for_our_own_addresses():
    """A contractor's own message must never be attributed to someone named in it."""
    msg = {"sender": "jhalajay@gmail.com", "body": FWD_BODY}
    assert mm.match_sender_or_forward(msg, ROSTER, OWN)["name"] == "Jay Jhala"
    stranger = {"sender": "someone@elsewhere.com", "body": FWD_BODY}
    assert mm.match_sender_or_forward(stranger, ROSTER, OWN) is None


def test_without_own_domains_nothing_is_read_out_of_a_body():
    msg = {"sender": "andrew.hurnard@thecachegroup.com.au", "body": FWD_BODY}
    assert mm.match_sender_or_forward(msg, ROSTER, ()) is None


def test_forwarded_senders_reads_plain_and_angle_bracket_forms():
    assert mm.forwarded_senders("From: Matt <Matt@thecachegroup.com.au>") == \
        ["matt@thecachegroup.com.au"]
    assert mm.forwarded_senders("From: donvuong@mail.com\nSent: today") == \
        ["donvuong@mail.com"]
    assert mm.forwarded_senders("no headers here") == []


def test_a_forwarded_message_files_against_the_right_contractor():
    msgs = [_m("andrew.hurnard@thecachegroup.com.au", "FW: Karen Crabb - invoice",
               [{"id": "a", "name": "100003 KC invoice to 2026.8.16.pdf"}],
               "2026-08-22", "m1")]
    msgs[0]["body"] = FWD_BODY
    plan = mm.plan_filing(msgs, date(2026, 8, 16), ROSTER, own_domains=OWN)
    assert [f["contractor"] for f in plan["files"]] == ["Karen Crabb"]


def test_a_date_range_is_two_dates_not_one_wrong_one():
    """Devinia titles hers "Payroll - 3/8-14/8". The day-first pattern grabbed
    "3/8-14" out of the middle and read it as August 2014, which threw her
    timesheet out of its own fortnight."""
    assert mm.stated_dates("FW: Payroll - 3/8-14/8", 2026) == [
        date(2026, 8, 3), date(2026, 8, 14)]
    assert mm.stated_dates("Payroll - 20/7-31/7", 2026) == [
        date(2026, 7, 20), date(2026, 7, 31)]
    assert mm.period_verdict("FW: Payroll - 3/8-14/8", date(2026, 8, 16)) == "in"


def test_real_two_digit_years_still_read():
    assert mm.stated_dates("Invoice 15/08/26", 2026) == [date(2026, 8, 15)]
    assert mm.stated_dates("Invoice 16.8.2026", 2026) == [date(2026, 8, 16)]
    assert mm.stated_dates("(02-08-2026 to 15-08-2026)", 2026) == [
        date(2026, 8, 2), date(2026, 8, 15)]


def test_a_forwarders_signature_graphic_is_not_a_timesheet():
    """Andrew's signature logo is 5,496 bytes and rides along on every forward.
    The smallest real PPM screenshot filed is 28,023."""
    assert mm.classify("image001.png", "image/png", True,
                       "FW: Karen Crabb - invoice", size=5496) == "signature"
    assert mm.classify("image.png", "image/png", True,
                       "Timesheets", size=95301) == "timesheet"
    # no size known - behave as before rather than guess
    assert mm.classify("image001.png", "image/png", True, "Timesheets") == "timesheet"


def test_the_signature_is_dropped_from_the_plan():
    msgs = [_m("andrew.hurnard@thecachegroup.com.au", "FW: Karen Crabb - invoice",
               [{"id": "a1", "name": "100003 KC invoice to 2026.8.16.pdf",
                 "contentType": "application/pdf", "size": 65343},
                {"id": "a2", "name": "Timesheet 2026.08.27.pdf",
                 "contentType": "application/pdf", "size": 422065},
                {"id": "a3", "name": "image001.png", "contentType": "image/png",
                 "isInline": True, "size": 5496}],
               "2026-08-22", "m1")]
    msgs[0]["body"] = FWD_BODY
    plan = mm.plan_filing(msgs, date(2026, 8, 16), ROSTER, own_domains=OWN)
    assert sorted(f["source_name"] for f in plan["files"]) == [
        "100003 KC invoice to 2026.8.16.pdf", "Timesheet 2026.08.27.pdf"]


# ------------------------------------------------- body-only timesheets
# Devinia Liddelow types her hours into the email as a table and attaches
# nothing. Before this, plan_filing had no entry for her at all and she came
# back under "missing" - identical to someone who never wrote. The fortnight
# ending 30 August 2026 nearly went out without her.

DEVINIA_BODY_ONLY = {
    "id": "m-devinia-1",
    "subject": "Re: Payroll - 17/8-28/8",
    "sender": "Devinia_Liddelow@linfox.com",
    "received": "2026-09-01T09:49:00Z",
    "attachments": [],
}


def test_a_body_only_message_is_not_reported_as_missing():
    plan = mm.plan_filing([DEVINIA_BODY_ONLY], date(2026, 8, 30))
    assert "Devinia Liddelow" not in plan["missing"]


def test_a_body_only_message_is_saved_as_eml_in_her_own_folder():
    plan = mm.plan_filing([DEVINIA_BODY_ONLY], date(2026, 8, 30))
    assert len(plan["body_only"]) == 1
    entry = plan["body_only"][0]
    assert entry["contractor"] == "Devinia Liddelow"
    assert entry["message_id"] == "m-devinia-1"
    assert entry["path"] == (
        "Fortnight  Ending 30082026/Linfox_Devinia Liddelow/"
        "DL_timesheet_2026-08-30.eml"
    )


def test_a_body_only_message_produces_no_attachment_files():
    """It is a message, not a file. Nothing goes through attachment_bytes."""
    plan = mm.plan_filing([DEVINIA_BODY_ONLY], date(2026, 8, 30))
    assert plan["files"] == []


def test_a_message_that_carried_a_document_is_never_body_only():
    msg = dict(DEVINIA_BODY_ONLY, attachments=[
        {"id": "a1", "name": "image001.png", "contentType": "image/png",
         "isInline": True, "size": 95040},
    ])
    plan = mm.plan_filing([msg], date(2026, 8, 30))
    assert plan["body_only"] == []
    assert len(plan["files"]) == 1


def test_a_signature_graphic_alone_still_counts_as_body_only():
    """A logo dragged along by a forward is not a document."""
    msg = dict(DEVINIA_BODY_ONLY, attachments=[
        {"id": "a1", "name": "image9.png", "contentType": "image/png",
         "isInline": True, "size": 1200},
    ])
    plan = mm.plan_filing([msg], date(2026, 8, 30))
    assert len(plan["body_only"]) == 1
    assert plan["files"] == []


def test_a_body_only_message_from_another_fortnight_is_left_alone():
    msg = dict(DEVINIA_BODY_ONLY, subject="Payroll - 3/8-14/8",
               received="2026-08-17T09:00:00Z")
    plan = mm.plan_filing([msg], date(2026, 8, 30))
    assert plan["body_only"] == []
    assert "Devinia Liddelow" in plan["missing"]


def test_two_body_only_messages_from_one_person_do_not_collide():
    a = dict(DEVINIA_BODY_ONLY, id="m1", received="2026-08-28T09:00:00Z")
    b = dict(DEVINIA_BODY_ONLY, id="m2", received="2026-09-01T09:49:00Z")
    plan = mm.plan_filing([a, b], date(2026, 8, 30))
    paths = [e["path"] for e in plan["body_only"]]
    assert len(paths) == len(set(paths)) == 2
    assert all("_part" in p for p in paths)


# ------------------------------------------------- the two new starters

def test_jerry_gonsalves_is_on_the_roster():
    """Started 17/08/2026. Sends an inline image from his Linfox address."""
    who = mm.match_sender("Jerry_Gonsalves@linfox.com")
    assert who is not None and who["item_code"] == "Linfox - JG"
    assert who["folder"] == "Linfox_Jerry Gonsalves"


def test_mazher_ali_is_on_the_roster_and_is_not_mudassir():
    maz = mm.match_sender("mdmazherali@gmail.com")
    mud = mm.match_sender("mudassirali27@outlook.com")
    assert maz["item_code"] == "Linfox - MAZ"
    assert mud["item_code"] == "Linfox - Mali"
    assert maz["folder"] != mud["folder"]


# ------------------------------------------------- duplicate invoice numbers
#
# Both cases below are real. Fortnight ending 30 August 2026: Bilal Virk
# re-sent INV-0016 and Jay Jhala re-sent 20260802, each already billed on the
# 16 August run. Both were filed without complaint, and the wrong fortnight's
# timesheets came within one dry run of being attached to a client invoice.

PRIOR = {
    "Linfox - BVIRK": {"INV-0016": "2026-08-16"},
    "Linfox - JJ": {"20260802": "2026-08-16"},
    "Tec - PS": {"0024": "2026-08-16"},
    "Linfox - DV": {"20260817": "2026-08-16"},
}


def test_resent_invoice_number_is_refused():
    plan = mm.plan_filing([_msg(
        "techneitconsulting@gmail.com", "Invoice and timesheets",
        [{"id": "a1", "name": "Invoice INV-0016.pdf"},
         {"id": "a2", "name": "image001.png", "isInline": True}],
    )], date(2026, 8, 30), ROSTER, prior_invoice_numbers=PRIOR)
    assert plan["files"] == []
    assert len(plan["duplicates"]) == 1
    d = plan["duplicates"][0]
    assert d["contractor"] == "Bilal Virk"
    assert d["number"] == "INV-0016"
    assert d["used_for"] == "2026-08-16"


def test_the_timesheets_go_with_the_refused_invoice():
    """They cover the period that invoice covers, not this one. This is the
    whole reason the _QUERY folder existed."""
    plan = mm.plan_filing([_msg(
        "jhalajay@gmail.com", "August invoice",
        [{"id": "a1", "name": "89_Dev_IT_Tax_Invoice_20260802.pdf"},
         {"id": "a2", "name": "image.png", "isInline": True}],
    )], date(2026, 8, 30), ROSTER, prior_invoice_numbers=PRIOR)
    assert plan["files"] == []
    assert plan["duplicates"][0]["attachments"] == 2


def test_a_refused_person_is_not_also_reported_as_silent():
    """Two chase lines for one problem, and the quieter one would be wrong."""
    plan = mm.plan_filing([_msg(
        "techneitconsulting@gmail.com", "Invoice",
        [{"id": "a1", "name": "Invoice INV-0016.pdf"}],
    )], date(2026, 8, 30), ROSTER, prior_invoice_numbers=PRIOR)
    assert "Bilal Virk" not in plan["missing"]


def test_a_fresh_number_from_the_same_person_is_filed():
    plan = mm.plan_filing([_msg(
        "techneitconsulting@gmail.com", "Invoice",
        [{"id": "a1", "name": "Invoice INV-0017.pdf"}],
    )], date(2026, 8, 30), ROSTER, prior_invoice_numbers=PRIOR)
    assert plan["duplicates"] == []
    assert len(plan["files"]) == 1


def test_a_short_number_matches_only_as_a_whole_token():
    """Peter Small's 0024 must not be found inside 0025, or inside a date."""
    assert mm.matches_known_number("Invoice_0024-HOURS.xlsx", "0024")
    assert not mm.matches_known_number("Invoice_0025.pdf", "0024")
    assert not mm.matches_known_number("Invoice#20260024.pdf", "0024")


def test_a_long_number_matches_inside_a_longer_filename():
    assert mm.matches_known_number("Invoice INV-0016.pdf", "INV-0016")
    assert mm.matches_known_number("89_Dev_IT_Tax_Invoice_20260802.pdf", "20260802")
    assert not mm.matches_known_number("Invoice#20260831.pdf", "20260817")


def test_an_old_thread_subject_does_not_condemn_a_new_invoice():
    """A reply on the INV-0016 thread carrying INV-0017. The attachment has a
    number of its own, so the subject is never consulted."""
    plan = mm.plan_filing([_msg(
        "techneitconsulting@gmail.com", "Re: Invoice INV-0016",
        [{"id": "a1", "name": "INV-0017.pdf"}],
    )], date(2026, 8, 30), ROSTER, prior_invoice_numbers=PRIOR)
    assert plan["duplicates"] == []


def test_a_generic_filename_falls_back_to_the_subject():
    plan = mm.plan_filing([_msg(
        "techneitconsulting@gmail.com", "Invoice INV-0016 for August",
        [{"id": "a1", "name": "invoice.pdf"}],
    )], date(2026, 8, 30), ROSTER, prior_invoice_numbers=PRIOR)
    assert len(plan["duplicates"]) == 1


def test_a_timesheet_carrying_an_old_invoice_number_is_not_refused():
    """Peter Small's hours workbook is named after the invoice it belongs to.
    Refusing that would refuse his hours."""
    plan = mm.plan_filing([_msg(
        "pjs.ucanemailme@gmail.com", "Hours",
        [{"id": "a1", "name": "Invoice_0024-HOURS.xlsx"}],
    )], date(2026, 8, 30), ROSTER, prior_invoice_numbers=PRIOR)
    assert plan["duplicates"] == []
    assert len(plan["files"]) == 1


def test_without_prior_numbers_nothing_changes():
    """The guard is additive. No lookup, old behaviour."""
    plan = mm.plan_filing([_msg(
        "techneitconsulting@gmail.com", "Invoice",
        [{"id": "a1", "name": "Invoice INV-0016.pdf"}],
    )], date(2026, 8, 30), ROSTER)
    assert plan["duplicates"] == []
    assert len(plan["files"]) == 1


# ---------------------------------------------------------------------------
# The duplicate guard's cutoff — proved wrong live on 3 September 2026
# ---------------------------------------------------------------------------

class _StubXero:
    """Just enough Xero to drive _prior_invoice_numbers.

    Returns the two bills that were actually re-sent and wrongly filed on
    3 September 2026, at their real dates and numbers, and ignores the date
    range it is handed - the point of the test is the cutoff the function
    applies AFTER the fetch, which is where the bug lived.
    """

    def iter_invoices(self, kind, start, end, statuses=None):
        assert kind == "ACCPAY"
        return iter([
            {"InvoiceNumber": "INV-0016", "Date": "2026-08-17T00:00:00",
             "Status": "PAID",
             "LineItems": [{"ItemCode": "Linfox - BVIRK", "Quantity": 10}]},
            {"InvoiceNumber": "20260802", "Date": "2026-08-17T00:00:00",
             "Status": "PAID",
             "LineItems": [{"ItemCode": "Linfox - JJ", "Quantity": 12}]},
            # This fortnight's own bills. Must NOT come back as prior, or a
            # contractor's current invoice reads as a duplicate of itself.
            {"InvoiceNumber": "August 2026", "Date": "2026-08-28T00:00:00",
             "Status": "AUTHORISED",
             "LineItems": [{"ItemCode": "Linfox - VKC", "Quantity": 1}]},
            {"InvoiceNumber": "20260831", "Date": "2026-08-31T00:00:00",
             "Status": "AUTHORISED",
             "LineItems": [{"ItemCode": "Linfox - DV", "Quantity": 10}]},
        ])


def test_prior_numbers_finds_the_previous_fortnights_billing_monday(monkeypatch):
    """THE PIN. Runs the real function, not date arithmetic beside it.

    An earlier version of this test asserted the arithmetic only, so it passed
    against the unfixed code and proved nothing - the deployed connector filed
    both duplicates while the suite reported 207 green. A test that cannot fail
    on the broken code is documentation, not a test.
    """
    from datetime import date
    from src import server

    monkeypatch.setattr(server, "client", lambda: _StubXero())
    prior = server._prior_invoice_numbers(date(2026, 8, 30))

    assert "INV-0016" in prior.get("Linfox - BVIRK", {})
    assert "20260802" in prior.get("Linfox - JJ", {})
    assert prior["Linfox - BVIRK"]["INV-0016"] == "2026-08-17"


def test_prior_numbers_excludes_this_fortnights_own_bills(monkeypatch):
    from datetime import date
    from src import server

    monkeypatch.setattr(server, "client", lambda: _StubXero())
    prior = server._prior_invoice_numbers(date(2026, 8, 30))

    assert "Linfox - VKC" not in prior          # bill dated 28 August
    assert "Linfox - DV" not in prior           # bill dated 31 August


def test_prior_numbers_is_silent_at_the_old_cutoff(monkeypatch):
    """window_days=13 reproduces the shipped bug exactly: the previous
    fortnight's billing Monday lands ON the cutoff and is excluded, and the
    guard has nothing to match against."""
    from datetime import date
    from src import server

    monkeypatch.setattr(server, "client", lambda: _StubXero())
    assert server._prior_invoice_numbers(date(2026, 8, 30), window_days=13) == {}


def test_the_prior_cutoff_clears_the_previous_fortnights_billing_monday():
    """THE BUG THAT MADE THE GUARD SILENT.

    TCG dates a bill the Monday AFTER the fortnight it pays for. For the
    fortnight ending 16 August that Monday is 17 August. The original cutoff was
    period_end - 13 days, which for the fortnight ending 30 August is ALSO
    17 August, and it required a bill dated strictly before it - so every one of
    the previous fortnight's bills missed by exactly one day and the guard could
    never fire for anybody.

    Live proof, 3 September 2026: Bilal Virk INV-0016 and Jay Jhala 20260802,
    both on bills dated 2026-08-17, both PAID, both re-sent, both filed.
    """
    from datetime import date, timedelta

    period_end = date(2026, 8, 30)
    prior_billing_monday = date(2026, 8, 17)

    old_cutoff = period_end - timedelta(days=13)
    assert old_cutoff == prior_billing_monday
    assert not (prior_billing_monday < old_cutoff)        # excluded - the bug

    new_cutoff = period_end - timedelta(days=10)          # window_days
    assert prior_billing_monday < new_cutoff              # included - the fix


def test_this_fortnights_own_bills_are_never_treated_as_prior():
    """A bill for THIS fortnight must not make this fortnight's own invoice look
    like a duplicate of itself. Vivek's August bill is dated the 28th and
    Bhasker's the 30th - both inside the window, both correctly excluded."""
    from datetime import date, timedelta

    cutoff = date(2026, 8, 30) - timedelta(days=10)
    for own in (date(2026, 8, 28), date(2026, 8, 30), date(2026, 8, 31)):
        assert not (own < cutoff)


def test_the_window_must_stay_under_a_fortnight():
    """Fortnights are 14 days apart. At window_days >= 14 the previous
    fortnight's billing Monday falls inside the window and stops counting as
    prior, which puts the guard straight back to silent."""
    from datetime import date, timedelta

    period_end = date(2026, 8, 30)
    prior_billing_monday = date(2026, 8, 17)
    assert prior_billing_monday < period_end - timedelta(days=10)
    assert not (prior_billing_monday < period_end - timedelta(days=14))
