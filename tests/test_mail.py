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


def test_monthly_contractors_are_out_of_scope_for_a_fortnightly_sweep():
    plan = mm.plan_filing([_msg("bansal.deepti90@gmail.com", "August Invoice",
                                [{"id": "a", "name": "invoice.pdf"}])],
                          date(2026, 8, 16), ROSTER)
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
