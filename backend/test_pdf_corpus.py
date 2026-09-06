"""
test_pdf_corpus.py — Corpus-level regression tests using the real test-form PDFs.

Purpose
-------
Verifies that pdf_extract.py correctly parses the full corpus of officer-filled
v2 PDF forms from `experiments/test_forms/`.  The corpus contains typed versions
of realistic officer inputs: correct spellings, misspellings, vague/abbreviated
addresses, combined date+time fields, etc.

These tests catch regressions in:
  - AcroForm field extraction (field mapping, line-join, zip truncation)
  - Address normalisation (apt stripping, time normalisation)
  - Key spot-checks for known regression forms (Form 9, Form 14)

The corpus lives in the gitignored `experiments/test_forms/` directory.
Tests skip automatically when that directory is absent (i.e. in worktrees or CI
environments without the full corpus).

Run with:
    pytest backend/test_pdf_corpus.py -v

Or from within backend/:
    python -m pytest test_pdf_corpus.py -v
"""

import pathlib
import re
import sys
import pytest

# ---------------------------------------------------------------------------
# Corpus directory — adjacent to the repo root experiments/ folder.
# In a git worktree the path won't exist (experiments/ is gitignored); tests
# skip automatically via the skip_no_corpus fixture.
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).parent.parent
FORMS_DIR = REPO_ROOT / "experiments" / "test_forms"

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from pdf_extract import extract_acroform_fields, build_synthetic_summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract(pdf_path: pathlib.Path) -> dict:
    """Return a dict of key fields from a PDF, raises on parse error."""
    fields = extract_acroform_fields(pdf_path.read_bytes())
    summary = build_synthetic_summary(
        fields, dispatcher_last_name="Test", intake_timestamp="2026-01-01 00:00"
    )
    result = {}
    for line in summary.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


def _forms(*stems: str) -> list[pathlib.Path]:
    """Return paths for the given form stems (e.g. '9 corrected', '14')."""
    return [FORMS_DIR / f"SAR Dispatch Form v2 FILLABLE-{s}.pdf" for s in stems]


# ---------------------------------------------------------------------------
# Skip fixture — all tests in this module require the corpus
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not FORMS_DIR.exists(),
    reason=f"Test corpus not found: {FORMS_DIR} (gitignored; run from main repo)"
)


# ---------------------------------------------------------------------------
# Smoke test — all forms must parse without crashing
# ---------------------------------------------------------------------------

class TestCorpusSmoke:
    """Every PDF in the corpus must parse without raising an exception."""

    def test_all_forms_parse_without_error(self):
        pdfs = list(FORMS_DIR.glob("*.pdf"))
        assert pdfs, f"No PDFs found in {FORMS_DIR}"
        errors = []
        for pdf in sorted(pdfs):
            try:
                _extract(pdf)
            except Exception as e:
                errors.append(f"{pdf.name}: {e}")
        assert not errors, "Extraction failures:\n" + "\n".join(errors)

    def test_all_forms_have_lkp_field(self):
        """Every form must produce a Last Known Position line."""
        pdfs = list(FORMS_DIR.glob("*.pdf"))
        missing = []
        for pdf in sorted(pdfs):
            try:
                fields = _extract(pdf)
                if "Last Known Position" not in fields:
                    missing.append(pdf.name)
            except Exception:
                pass  # Already caught in smoke test above
        assert not missing, "Missing LKP field: " + ", ".join(missing)

    def test_all_forms_have_residence_field(self):
        """Every form must produce a Residence Address line."""
        pdfs = list(FORMS_DIR.glob("*.pdf"))
        missing = []
        for pdf in sorted(pdfs):
            try:
                fields = _extract(pdf)
                if "Residence Address" not in fields:
                    missing.append(pdf.name)
            except Exception:
                pass
        assert not missing, "Missing Residence field: " + ", ".join(missing)


# ---------------------------------------------------------------------------
# Form 9 — the Moretti Lane / house-number-change regression
# ---------------------------------------------------------------------------

class TestForm9MorettiLane:
    """
    Form 9 (corrected) — Moretti Lane, Milpitas.
    Confirmed root cause of PR #215 (Google Maps returned 447 Great Mall Dr
    for the LKP — house number changed → wrong address).
    """

    @pytest.fixture
    def fields(self):
        return _extract(_forms("9 corrected")[0])

    def test_lkp_is_moretti_lane(self, fields):
        """LKP must be Moretti Lane, not Morette (officer spelled correctly in LKP field)."""
        assert "MORETTI LANE" in fields["Last Known Position"].upper()
        assert "MILPITAS" in fields["Last Known Position"].upper()

    def test_residence_is_morette_lane(self, fields):
        """Residence has the slightly different spelling MORETTE (officer typo in address field)."""
        assert "MORETTE LANE" in fields["Residence Address"].upper()
        assert "MILPITAS" in fields["Residence Address"].upper()

    def test_both_have_the_same_house_number(self, fields):
        """LKP and Residence must share a leading house number.

        That equality IS the invariant PR #215 turned on — a house number that
        changes between the two fields is the signal of a wrong geocode. The
        number itself is subject PII and is deliberately not asserted here or
        echoed into a failure message.
        """
        lkp_num = re.match(r"(\d+)", fields["Last Known Position"])
        res_num = re.match(r"(\d+)", fields["Residence Address"])
        assert lkp_num, "LKP has no leading house number"
        assert res_num, "Residence has no leading house number"
        assert lkp_num.group(1) == res_num.group(1), \
            "LKP and Residence leading house numbers differ"

# ---------------------------------------------------------------------------
# Form 14 — TRADEN DR misspelling (PR #178 regression guard)
# ---------------------------------------------------------------------------

class TestForm14TradenDr:
    """
    Form 14 — TRADEN DR (misspelling of TRADAN DR, San Jose).
    This form triggers the Google Maps spelling-correction path.
    After the house-number guard (PR #215), Google Maps result with
    same house number (1950) must still pass through and correction logged.
    """

    @pytest.fixture
    def fields(self):
        return _extract(_forms("14")[0])

    def test_lkp_has_traden_misspelling(self, fields):
        """Officer wrote TRADEN (not TRADAN) — must be preserved verbatim."""
        assert "TRADEN" in fields["Last Known Position"].upper()

    def test_lkp_house_number_is_1950(self, fields):
        """House number 1950 must be present — guard must pass for same-number correction."""
        lkp_num = re.match(r"(\d+)", fields["Last Known Position"])
        assert lkp_num and lkp_num.group(1) == "1950", \
            f"Expected leading 1950, got: {fields['Last Known Position']}"

    def test_residence_also_has_1950(self, fields):
        """Residence also has 1950 — Nominatim may fail, Google Maps correction must pass guard."""
        res_num = re.match(r"(\d+)", fields["Residence Address"])
        assert res_num and res_num.group(1) == "1950"


# ---------------------------------------------------------------------------
# Forms with no house number — guard must be skipped (return True)
# ---------------------------------------------------------------------------

class TestNoHouseNumberForms:
    """
    Forms where LKP is a landmark, intersection, or city — no leading house number.
    House-number guard must return True (can't check → allow through).
    """

    def test_form1_city_only_lkp(self):
        """Form 1: LKP is city-level ('SAN JOSE,CA') — no house number."""
        fields = _extract(_forms("1")[0])
        lkp = fields["Last Known Position"]
        assert not re.match(r"^\d+\b", lkp), \
            f"Form 1 LKP should have no leading house number: {lkp!r}"

    def test_form6_school_lkp(self):
        """Form 6: LKP is 'FLETCHER MIDDLE SCHOOL' — landmark, no house number."""
        fields = _extract(_forms("6")[0])
        lkp = fields["Last Known Position"]
        assert not re.match(r"^\d+\b", lkp), \
            f"Form 6 LKP should have no leading house number: {lkp!r}"

# ---------------------------------------------------------------------------
# Forms 11 and 12 — the Calvert / Camargo staging-mismatch regression (PR #194)
# ---------------------------------------------------------------------------

class TestStagingMismatchForms:
    """
    Forms 11 (Calvert Dr) and 12 (Camargo Ct) — the forms that triggered the
    lstrip() → re.sub() fix for staging-area mismatch false positives (PR #194).
    Verify address extraction produces the correct house numbers.
    """

    def test_form11_calvert_lkp_has_10410(self):
        fields = _extract(_forms("11")[0])
        lkp = fields["Last Known Position"]
        assert lkp.startswith("10410"), f"Form 11 LKP should start with 10410: {lkp!r}"

    def test_form11_calvert_residence_has_10410(self):
        fields = _extract(_forms("11")[0])
        res = fields["Residence Address"]
        assert res.startswith("10410"), f"Form 11 Residence should start with 10410: {res!r}"

    def test_form12_camargo_lkp_has_2991(self):
        fields = _extract(_forms("12")[0])
        lkp = fields["Last Known Position"]
        assert lkp.startswith("2991"), f"Form 12 LKP should start with 2991: {lkp!r}"

    def test_form12_camargo_residence_has_2991(self):
        fields = _extract(_forms("12")[0])
        res = fields["Residence Address"]
        assert res.startswith("2991"), f"Form 12 Residence should start with 2991: {res!r}"
