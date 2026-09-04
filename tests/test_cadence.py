"""Cadence can differ by side.

Peter Small is billed FORTNIGHTLY by TecAlliance and invoiced MONTHLY to Xenon
Media. One person, two cycles, and until 4 September 2026 the register held one
cadence per person - so he was in it as nothing at all, defaulted to
fortnightly, and the fortnightly run was one write away from stamping
"17 August to 30 August 2026" over his "Peter Little August" reference.

Marking him plainly monthly is not the fix either: split_excluded judges a
document monthly when any line carries a monthly item code, so his fortnightly
BILL would have been pulled out of the fortnightly submit and he would have
stopped being paid on time.

Andrew, 4 September 2026: "there will always be the exception."
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src import writes as w
from src import server as srv


def _doc(num, typ, code):
    return {"InvoiceID": f"id-{num}", "InvoiceNumber": num, "Type": typ,
            "Contact": {"Name": "Xenon Media" if typ == "ACCREC" else "Peter Small"},
            "LineItems": [{"LineItemID": "l1", "ItemCode": code, "Quantity": 1}]}


SPLIT = {"sales": {"Tec - PS"}, "bills": set()}


# --------------------------------------------------------------- the register

def test_a_plain_string_still_means_both_sides():
    """The offshore four must not change. They are monthly either way round."""
    e = {"cadence": "monthly"}
    assert srv._cadence_value(e) == "monthly"
    assert srv._cadence_value(e, "sales") == "monthly"
    assert srv._cadence_value(e, "bills") == "monthly"


def test_absent_means_fortnightly():
    assert srv._cadence_value({}) == "fortnightly"
    assert srv._cadence_value({}, "sales") == "fortnightly"


def test_a_split_entry_answers_per_side():
    e = {"cadence": {"sales": "monthly", "bills": "fortnightly"}}
    assert srv._cadence_value(e, "sales") == "monthly"
    assert srv._cadence_value(e, "bills") == "fortnightly"


def test_no_side_asked_means_monthly_on_either():
    """Anything that is monthly somewhere is monthly to a caller that did not
    say which side it meant - the safer reading for a report."""
    e = {"cadence": {"sales": "monthly", "bills": "fortnightly"}}
    assert srv._cadence_value(e) == "monthly"


def test_a_malformed_entry_reads_as_fortnightly():
    """A typo must not quietly take somebody out of the fortnightly run."""
    assert srv._cadence_value({"cadence": {"sales": "montly"}}, "sales") == "fortnightly"
    assert srv._cadence_value({"cadence": None}, "sales") == "fortnightly"


# ------------------------------------------------------------ what it prevents

def test_his_monthly_invoice_is_held_out_of_the_fortnightly_run():
    kept, excluded = w.split_excluded([_doc("TCG-21192", "ACCREC", "Tec - PS")],
                                      None, SPLIT, "fortnightly")
    assert kept == []
    assert "monthly cadence" in excluded[0]["Why"]


def test_his_fortnightly_bill_is_NOT_held():
    """The whole reason a plain 'monthly' will not do. He still gets paid."""
    kept, excluded = w.split_excluded([_doc("0025", "ACCPAY", "Tec - PS")],
                                      None, SPLIT, "fortnightly")
    assert excluded == [] and kept[0]["InvoiceNumber"] == "0025"


def test_the_monthly_run_takes_the_invoice_and_leaves_the_bill():
    kept, _ = w.split_excluded([_doc("TCG-21192", "ACCREC", "Tec - PS")],
                               None, SPLIT, "monthly")
    assert kept and kept[0]["InvoiceNumber"] == "TCG-21192"
    kept2, excluded2 = w.split_excluded([_doc("0025", "ACCPAY", "Tec - PS")],
                                        None, SPLIT, "monthly")
    assert kept2 == [] and excluded2


def test_a_plain_set_still_works_on_both_sides():
    """Back compatible: the offshore people are passed as one set and both
    their documents are monthly."""
    both = {"linfox - pd"}
    for typ in ("ACCREC", "ACCPAY"):
        kept, excluded = w.split_excluded([_doc("X", typ, "Linfox - PD")],
                                          None, both, "fortnightly")
        assert kept == [] and excluded


def test_a_document_with_no_type_is_judged_on_the_sales_side():
    """Xero not sending Type must not let a monthly invoice through: a sales
    invoice going out a cycle early is the costlier mistake."""
    d = _doc("TCG-21192", "ACCREC", "Tec - PS")
    d.pop("Type")
    kept, excluded = w.split_excluded([d], None, SPLIT, "fortnightly")
    assert kept == [] and excluded


def test_peter_is_actually_in_the_shipped_config():
    """The fix is the entry, not just the code that reads it."""
    import json, pathlib
    cfg = json.load(open(pathlib.Path(__file__).resolve().parents[1]
                         / "config" / "roster_overrides.json", encoding="utf-8"))
    entry = cfg["by_item_code"]["Tec - PS"]
    assert entry["cadence"] == {"sales": "monthly", "bills": "fortnightly"}
