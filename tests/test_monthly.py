"""Monthly contractors are not fortnightly contractors with a longer gap.

Prasanthi Dharanikota's month runs the 12th to the 11th, not the calendar
month (Andrew, 3 Sep 2026). Deepti's house reference is "Deepti Bansal May
2026" - a month label, not a date range. The fill's auto reference is a
FORTNIGHT range and must never land on any of them."""
import sys, pathlib, os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("XERO_CLIENT_ID", "x")
os.environ.setdefault("XERO_CLIENT_SECRET", "x")
os.environ.setdefault("XERO_TENANT_ID", "x")

from src import server as S
from src import roster


def _inv(num, code, ref):
    return {"InvoiceID": f"id-{num}", "InvoiceNumber": num, "Reference": ref,
            "Contact": {"Name": "Linfox ADIT - Warehouse Solutions"},
            "LineItems": [{"LineItemID": f"li-{num}", "ItemCode": code,
                           "Quantity": 0, "UnitAmount": 365.0,
                           "AccountCode": "200", "TaxType": "OUTPUT"}]}


def _fill(monkeypatch, docs, quantities):
    class F:
        def iter_invoices(self, kind, lo, hi, statuses=None):
            return list(docs) if kind == "ACCREC" else []
    monkeypatch.setattr(S, "client", lambda: F())
    return S.fill_period_drafts(period_end="2026-08-30", quantities=quantities,
                                dry_run=True)


def test_cadence_is_read_from_the_real_overrides_file():
    """load_overrides() returns the by_item_code map, NOT the whole file.
    Getting that wrong made _is_monthly return False for everybody and the
    guard silently did nothing - which is how it first shipped."""
    assert S._is_monthly("Linfox - PD") is True, "Prasanthi is monthly"
    assert S._is_monthly("Linfox - DBL") is True, "Deepti is monthly"
    assert S._is_monthly("Linfox - JJ") is False, "Jay is fortnightly"
    assert S._is_monthly("nobody at all") is False, "default is fortnightly"


def test_an_offset_monthly_invoice_gets_its_own_cycle_reference():
    """The OFFSET MACHINERY, tested on its own rather than on a live person -
    which is what broke when Prasanthi turned out to be calendar month. A
    cycle that turns over on the 12th describes itself as the 12th to the 11th,
    never as the fortnight range."""
    from datetime import date
    from src import writes as w
    assert w.monthly_reference(date(2026, 8, 30), 12) == "12 August to 11 September 2026"
    assert "17 August to 30 August 2026" != w.monthly_reference(date(2026, 8, 30), 12)


def test_a_calendar_month_person_is_left_alone(monkeypatch):
    """Bhasker is monthly with no offset. His house reference is a month
    label, not a range, so the connector reports it rather than writing it."""
    out = _fill(monkeypatch, [_inv("TCG-21188", "Linfox - BV", "BV")],
                "Linfox - BV: 21")
    assert "MONTHLY - REFERENCE NOT TOUCHED" in out
    assert "17 August to 30 August 2026" not in out


def test_a_fortnightly_invoice_still_gets_it(monkeypatch):
    out = _fill(monkeypatch, [_inv("TCG-21195", "Linfox - JJ", "JJ")],
                "Linfox - JJ: 10")
    assert "17 August to 30 August 2026" in out
    assert "MONTHLY - REFERENCE NOT TOUCHED" not in out


def test_the_days_are_still_filled_for_a_monthly_person(monkeypatch):
    """Only the reference is withheld. The quantity is the whole point."""
    out = _fill(monkeypatch, [_inv("TCG-21207", "Linfox - PD", "x")],
                "Linfox - PD: 22")
    assert "8030" in out.replace(",", "")


def test_no_monthly_person_is_offset_unless_it_is_deliberate():
    """Prasanthi CARRIED period_day 12 until 5 September 2026, on a reading of
    "she bills on the 12th" as a 12th-to-11th CYCLE. It is a SEND day. Eight
    PRAVID bills from January to August are all referenced "<Month> <Year>" and
    dated the 1st of the following month, and her timesheets run 2-31 August
    with no part-weeks. Left set, her window started 12 August and an invoice
    stating 1 August was refused. An offset is a real thing and the machinery
    below still supports it - but it has to be evidenced, not inferred from
    when somebody presses send."""
    ov = roster.load_overrides()
    assert "period_day" not in ov.get("Linfox - PD", {})


def test_the_offset_cycle_is_the_one_containing_period_end():
    """The fortnightly reference describes the period being billed, not the one
    before it, and this matches. The 11th is that cycle's last day, so it
    belongs to the cycle that opened on the 12th of the month before."""
    from datetime import date
    from src import writes as w
    assert w.monthly_reference(date(2026, 8, 30), 12) == "12 August to 11 September 2026"
    assert w.monthly_reference(date(2026, 9, 11), 12) == "12 August to 11 September 2026"
    assert w.monthly_reference(date(2026, 9, 12), 12) == "12 September to 11 October 2026"


def test_the_offset_cycle_survives_a_year_end():
    """Never happened yet. Both years written, which is the only unambiguous
    reading, and it comes free from the fortnightly formatter."""
    from datetime import date
    from src import writes as w
    assert w.monthly_reference(date(2026, 12, 20), 12) == "12 December 2026 to 11 January 2027"
    assert w.monthly_reference(date(2027, 1, 5), 12) == "12 December 2026 to 11 January 2027"
