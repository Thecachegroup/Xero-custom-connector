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
        [{"id": "a1", "name": "100003 KC invoice to 2026.8.2.pdf"},
         {"id": "a2", "name": "TCG Weekly Timesheet - Karen Crabb 26.8.2.pdf"}],
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
