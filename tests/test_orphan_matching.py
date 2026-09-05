"""
Orphan shell matching.

SEEK cover letters arrive as separate CATS records with no email address.
merge_orphan_shells copies the letter onto the real candidate - and copies it
unconditionally. Get the matching wrong and a cover letter lands on the wrong
person's record, which is a privacy problem, not a tidiness one.

On 1 September a scan that failed to skip already-parked shells copied the
same letters onto the same candidates twice. It was caught before it ran a
third time.
"""

import pytest


# ---- is this a cover letter? ---------------------------------------------


@pytest.mark.parametrize("filename", [
    "Nidhi Chowdary Gadde CATS 16838338 CoverLetter.txt",
    "Jane Smith CATS 123 coverletter.txt",
    "notes.txt",
])
def test_recognises_a_cover_letter(api, filename):
    assert api._looks_like_cover_letter(filename) is True


@pytest.mark.parametrize("filename", ["", None, "Nicole Barrett CV.pdf", "resume.docx"])
def test_ignores_everything_else(api, filename):
    assert api._looks_like_cover_letter(filename) is False


# ---- pulling a name off a filename ---------------------------------------


def test_name_is_taken_from_before_the_cats_marker(api):
    assert api._filename_stem_name(
        "Nidhi Chowdary  Gadde CATS 16838338 CoverLetter.txt"
    ) == "nidhi chowdary gadde"


def test_underscores_and_dots_are_separators(api):
    assert api._filename_stem_name("Jane_Smith.CATS 99 CoverLetter.txt") == "jane smith"


@pytest.mark.parametrize("filename", ["", None])
def test_no_filename_gives_no_name(api, filename):
    assert api._filename_stem_name(filename) == ""


def test_filename_without_the_convention_yields_the_bare_stem(api):
    """No CATS marker means the whole filename is treated as the name.

    Pinned deliberately: it is the case most likely to produce a wrong match,
    so a change in behaviour here should be a decision, not a surprise.
    """
    assert api._filename_stem_name("randomfile.txt") == "randomfile txt"


# ---- name normalisation --------------------------------------------------


def test_case_and_spacing_are_normalised(api):
    assert api._norm_name("  Nicole   BARRETT ") == "nicole barrett"


def test_punctuation_and_digits_are_dropped(api):
    assert api._norm_name("O'Brien-Smith 123") == "obriensmith"


@pytest.mark.parametrize("value", ["", None])
def test_empty_name_normalises_to_empty(api, value):
    assert api._norm_name(value) == ""


def test_hyphenated_name_normalises_predictably(api):
    """A hyphen closes up; a space does not. Both spellings must agree."""
    assert api._norm_name("Jean-Luc Picard") == "jeanluc picard"
    assert api._norm_name("JEAN-LUC  picard") == api._norm_name("Jean-Luc Picard")


# ---- does this candidate have an email? ----------------------------------


def test_primary_email_counts(api):
    assert api._has_email({"emails": {"primary": "a@b.com"}}) is True


def test_secondary_email_counts(api):
    assert api._has_email({"emails": {"secondary": "a@b.com"}}) is True


@pytest.mark.parametrize("candidate", [
    {"emails": {}},
    {"emails": {"primary": "", "secondary": ""}},
    {"emails": []},
    {"emails": None},
    {},
])
def test_no_email_is_detected(api, candidate):
    """The absence of an email is what marks a record as a shell."""
    assert api._has_email(candidate) is False


def test_list_shaped_emails_are_handled(api):
    assert api._has_email({"emails": ["a@b.com"]}) is True


# ---- display name --------------------------------------------------------


def test_full_name_is_joined(api):
    assert api._name_of({"first_name": "Nicole", "last_name": "Barrett"}) == "Nicole Barrett"


@pytest.mark.parametrize("candidate,expected", [
    ({"first_name": "Nicole"}, "Nicole"),
    ({"last_name": "Barrett"}, "Barrett"),
    ({}, ""),
    ({"first_name": None, "last_name": None}, ""),
])
def test_partial_names_do_not_leave_stray_spaces(api, candidate, expected):
    assert api._name_of(candidate) == expected
