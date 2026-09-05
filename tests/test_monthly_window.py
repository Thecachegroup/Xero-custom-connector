"""The monthly period window - defect found 5 September 2026.

Every document was judged against the FORTNIGHT window. A monthly contractor's
invoice states a calendar month, which cannot fit inside a fortnight, so it was
refused on every run while the same person's WEEKLY timesheets filed normally.
Prasanthi Dharanikota's four August timesheets filed and her August invoice did
not; TCG-21207 and her PRAVID bill both sat at zero and August went unbilled.
"""
from datetime import date

from src import mail_mappers as mm

END = date(2026, 8, 30)          # the fortnight ending Sunday 30 August 2026
CAL = {"name": "Bhasker Veela", "item_code": "Linfox - BV", "cadence": "monthly",
       "folder": "f", "period_day": None}
OFF = {"name": "Prasanthi Dharanikota", "item_code": "Linfox - PD",
       "cadence": "monthly", "folder": "f", "period_day": 12}
FORT = {"name": "Don Vuong", "item_code": "Linfox - DV", "cadence": "fortnightly",
        "folder": "f", "period_day": None}


# ------------------------------------------------------------------ spans
def test_calendar_month_span_is_august():
    """The cycle ENDING on or before the end of the run window - August, not
    September, and bounded at 1 August so July cannot follow it in."""
    assert mm.monthly_span(END, 10) == (date(2026, 8, 1), date(2026, 9, 9))


def test_offset_span_follows_period_day():
    """Prasanthi runs the 12th to the 11th, so her cycle starts 12 July."""
    assert mm.monthly_span(END, 10, 12) == (date(2026, 7, 12), date(2026, 9, 9))


def test_fortnightly_span_is_unchanged():
    """Nothing about the fortnightly path moves. This is the regression guard."""
    assert mm.contractor_span(FORT, END, 10) == mm.period_window(END, 10)


def test_cadence_is_read_from_the_person_not_the_run():
    assert mm.contractor_span(CAL, END, 10) != mm.contractor_span(FORT, END, 10)


def test_month_end_run_does_not_jump_forward():
    """A window that reaches into the next month must not claim that month -
    its cycle has not finished, so nobody has invoiced for it."""
    assert mm.monthly_span(date(2026, 9, 13), 10)[0] == date(2026, 8, 1)


# ------------------------------------------------------------- verdicts
def _msg(subject, names, received, sender="x@y.com"):
    return {"id": "m1", "sender": sender, "subject": subject, "received": received,
            "attachments": [{"id": f"a{i}", "name": n, "contentType": "application/pdf",
                             "size": 90000} for i, n in enumerate(names)]}


def test_month_invoice_was_refused_before_and_passes_now():
    """The exact failure. Prasanthi's cycle runs 12 July to 11 August and her
    invoice names it; every date in it falls outside the 17-30 August fortnight
    and its grace week, so the old code refused the whole document."""
    text = "Invoice 12-07-2026 to 11-08-2026"
    assert mm.period_verdict(text, END, 10, not_after=date(2026, 8, 31)) == "out"
    assert mm.period_verdict(text, END, 10, not_after=date(2026, 8, 31),
                             span=mm.monthly_span(END, 10, 12)) == "in"


def test_early_august_still_belongs_to_the_month():
    """Bhasker's split invoice: 1-9 August is inside his month and would fail
    any fortnight test, which is what refused all three of his messages."""
    span = mm.monthly_span(END, 10)
    assert mm.period_verdict("1/8 to 9/8", END, 10, not_after=date(2026, 8, 31),
                             span=span) == "in"


def test_july_does_not_follow_august_in():
    span = mm.monthly_span(END, 10)
    assert mm.period_verdict("Invoice 2026-07-15", END, 10,
                             not_after=date(2026, 8, 31), span=span) == "out"


def test_offset_person_accepts_their_own_cycle_and_not_the_one_before():
    span = mm.monthly_span(END, 10, 12)
    assert mm.period_verdict("2026-07-20", END, 10,
                             not_after=date(2026, 8, 31), span=span) == "in"
    assert mm.period_verdict("2026-07-05", END, 10,
                             not_after=date(2026, 8, 31), span=span) == "out"


# -------------------------------------------------------------- end to end
def test_monthly_invoice_is_filed_and_not_reported_out_of_period():
    plan = mm.plan_filing(
        [_msg("Invoice - Aug 2026", ["Invoice 01-08-2026 to 31-08-2026.pdf"],
              "2026-08-31T09:00:00Z", "prasanthidharanikota362@gmail.com")],
        END, contractors=[dict(OFF, emails=["prasanthidharanikota362@gmail.com"],
                               contact_names=["Prasanthi Dharanikota"])])
    assert plan["out_of_period"] == []
    assert [f["contractor"] for f in plan["files"]] == ["Prasanthi Dharanikota"]
    assert plan["missing"] == []


def test_fortnightly_person_still_rejects_the_wrong_fortnight():
    """The guard that must not be lost: widening the monthly window must not
    widen anyone else's."""
    plan = mm.plan_filing(
        [_msg("Timesheet", ["Timesheet 2026-07-15.pdf"], "2026-08-20T09:00:00Z",
              "dv@x.com")],
        END, contractors=[dict(FORT, emails=["dv@x.com"], contact_names=["Don Vuong"])])
    assert plan["files"] == []
    assert len(plan["out_of_period"]) == 1
    assert "2026-08-17 to 2026-09-09" in plan["out_of_period"][0]["reason"]


def test_out_of_period_reason_names_the_window_not_the_received_date():
    """Defect 8. The old text said 'received outside <window>' for documents
    that had arrived well inside it."""
    plan = mm.plan_filing(
        [_msg("Invoice", ["Invoice 2026-06-30.pdf"], "2026-08-31T09:00:00Z",
              "bv@x.com")],
        END, contractors=[dict(CAL, emails=["bv@x.com"], contact_names=["Bhasker Veela"])])
    row = plan["out_of_period"][0]
    assert "monthly cycle" in row["reason"]
    assert "2026-08-01" in row["reason"]
