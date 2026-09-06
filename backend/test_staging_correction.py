"""
test_staging_correction.py — Regression tests for the staging-area mismatch warning.

Background
----------
The dispatcher-visible Event Log emits a "verify with officer" note when the text in
`Staging Area for Resources` (written by the officer) doesn't match the officer entry
in the final staging list.  The comparison extracts the location name from the staging
label (text before " — "), then checks whether the first word of that name matches the
first word of the officer's raw staging text.

Bug (PR #193)
-------------
The extraction used `lstrip("0123456789. ")` to strip the "N. " sequential prefix that
`main.py` prepends to every staging label (e.g. "1. 10000 Calvert Dr. — Officer...").
`lstrip` strips each character in the given SET from the left, so it also strips the
house number ("10410" → all digits are in the set) and the trailing space.  The first
word of the extracted name therefore becomes the street name ("CALVERT"), while the
first word of `_sa_raw` is the house number ("10410") → mismatch → false positive.

Fix
---
Replace `lstrip("0123456789. ")` with `re.sub(r"^\\d+\\.\\s*", "", ...)` which strips
ONLY the leading "N. " sequential prefix (digits + dot + optional whitespace), leaving
the house number intact.

These tests confirm:
  1. The bug is real for the two known failure forms (Calvert, Camargo).
  2. The fix eliminates both false positives.
  3. A genuine mismatch (officer staging ≠ matched location) still triggers the warning.
  4. A named-location match (no house number involved) still works correctly.

Run with:
    pytest backend/test_staging_correction.py -v
"""

import re
import pytest


# ---------------------------------------------------------------------------
# Helpers — mirror the exact logic in main.py so changes stay in sync.
# ---------------------------------------------------------------------------

def _extract_matched_BUGGY(label: str) -> str:
    """Old (buggy) extraction — strips house number along with sequential prefix."""
    return label.split(" — ")[0].lstrip("0123456789. ").strip()


def _extract_matched_FIXED(label: str) -> str:
    """Fixed extraction — strips only the 'N. ' sequential prefix."""
    return re.sub(r"^\d+\.\s*", "", label.split(" — ")[0]).strip()


def _first_word_key(s: str) -> str:
    """Normalise to lowercase alphanumeric first token (mirrors main.py)."""
    toks = s.split()
    return re.sub(r"[^a-z0-9]", "", toks[0].lower()) if toks else ""


def _would_warn(sa_raw: str, label: str, *, use_fix: bool) -> bool:
    """Return True if the mismatch warning would fire for the given inputs."""
    extract = _extract_matched_FIXED if use_fix else _extract_matched_BUGGY
    matched = extract(label)
    return _first_word_key(sa_raw) != _first_word_key(matched)


# ---------------------------------------------------------------------------
# Label values derived from real corpus output (exact body[:60] truncation
# of the Gemini staging line, prefixed with "N. " by main.py).
# ---------------------------------------------------------------------------

# Form: 10000 Calvert Dr., Cupertino  (v2 PDF + v1 JPEG — both paths confirmed)
CALVERT_SA_RAW_PDF  = "10000 CALVERT DR."
CALVERT_SA_RAW_JPEG = "10000 Calvert Dr., Cupertino, CA"
CALVERT_LABEL = (
    "1. 10000 CALVERT DR. CUPERTINO 95014 — Officer-designated stagi"
)

# Form: 2000 Camargo Ct., San Jose  (v2 PDF)
CAMARGO_SA_RAW = "2000 CAMARGO CT. SAN JOSE, CA 95/32"
CAMARGO_LABEL = (
    "1. 2000 CAMARGO CT. SAN JOSE, CA 95132 — Officer-designated stag"
)

# Synthetic: named staging location — officer wrote park name, matched correctly.
NAMED_SA_RAW   = "CARDOZA PARK MILPITAS"
NAMED_LABEL    = "1. Cardoza Park, Milpitas — Officer-designated staging location"

# Synthetic: real mismatch — officer wrote one place, matched to a different one.
MISMATCH_SA_RAW = "DUNKIN DONUTS"
MISMATCH_LABEL  = "1. 300 Darryl Drive, Campbell — Officer-designated staging loc"


# ---------------------------------------------------------------------------
# Tests: demonstrate the bug
# ---------------------------------------------------------------------------

class TestBuggyBehavior:
    """Confirm that the OLD lstrip logic produces false positives."""

    def test_calvert_pdf_false_positive(self):
        """PDF path: '10000 CALVERT DR.' vs label starting '1. 10410 ...' → false positive."""
        assert _would_warn(CALVERT_SA_RAW_PDF, CALVERT_LABEL, use_fix=False), (
            "Expected false positive with old lstrip logic — bug not demonstrated"
        )

    def test_calvert_jpeg_false_positive(self):
        """JPEG path: '10000 Calvert Dr., Cupertino, CA' → same false positive."""
        assert _would_warn(CALVERT_SA_RAW_JPEG, CALVERT_LABEL, use_fix=False), (
            "Expected false positive with old lstrip logic — bug not demonstrated"
        )

    def test_camargo_false_positive(self):
        """'2000 CAMARGO CT. ...' vs '1. 2000 CAMARGO CT. ...' → false positive."""
        assert _would_warn(CAMARGO_SA_RAW, CAMARGO_LABEL, use_fix=False), (
            "Expected false positive with old lstrip logic — bug not demonstrated"
        )


# ---------------------------------------------------------------------------
# Tests: confirm the fix
# ---------------------------------------------------------------------------

class TestFixedBehavior:
    """After the fix, house-number-same-as-label cases must NOT warn."""

    def test_calvert_pdf_no_false_positive(self):
        """PDF path: same address in both fields → no warning after fix."""
        assert not _would_warn(CALVERT_SA_RAW_PDF, CALVERT_LABEL, use_fix=True), (
            "False positive still fires after fix — fix is incomplete"
        )

    def test_calvert_jpeg_no_false_positive(self):
        """JPEG path: same address in both fields → no warning after fix."""
        assert not _would_warn(CALVERT_SA_RAW_JPEG, CALVERT_LABEL, use_fix=True), (
            "False positive still fires after fix — fix is incomplete"
        )

    def test_camargo_no_false_positive(self):
        """Camargo: same address in both fields → no warning after fix."""
        assert not _would_warn(CAMARGO_SA_RAW, CAMARGO_LABEL, use_fix=True), (
            "False positive still fires after fix — fix is incomplete"
        )

    def test_named_location_match_no_warning(self):
        """Named staging location matches correctly — no warning (fix or bug)."""
        assert not _would_warn(NAMED_SA_RAW, NAMED_LABEL, use_fix=True), (
            "Named-location match should never warn"
        )

    def test_real_mismatch_still_warns(self):
        """Genuine staging mismatch must still produce a warning after the fix."""
        assert _would_warn(MISMATCH_SA_RAW, MISMATCH_LABEL, use_fix=True), (
            "Real mismatch no longer warns after fix — fix broke the detection"
        )


# ---------------------------------------------------------------------------
# Tests: extraction correctness (unit)
# ---------------------------------------------------------------------------

class TestExtractionLogic:
    """Low-level tests for the extraction helper itself."""

    def test_fixed_preserves_house_number(self):
        assert _extract_matched_FIXED("1. 10000 CALVERT DR. CUPERTINO 95014 — foo") == \
            "10000 CALVERT DR. CUPERTINO 95014"

    def test_fixed_strips_two_digit_prefix(self):
        assert _extract_matched_FIXED("10. 2000 Camargo Ct — foo") == "2000 Camargo Ct"

    def test_fixed_strips_named_location_prefix(self):
        assert _extract_matched_FIXED("1. Cardoza Park, Milpitas — foo") == \
            "Cardoza Park, Milpitas"

    def test_buggy_strips_house_number(self):
        """Demonstrate exactly what lstrip does wrong."""
        assert _extract_matched_BUGGY("1. 10000 CALVERT DR. CUPERTINO 95014 — foo") == \
            "CALVERT DR. CUPERTINO 95014"

    def test_first_word_key_normalisation(self):
        assert _first_word_key("10000 CALVERT DR.") == "10000"
        assert _first_word_key("CALVERT DR. CUPERTINO 95014") == "calvert"
        assert _first_word_key("CARDOZA PARK MILPITAS") == "cardoza"
        assert _first_word_key("Cardoza Park, Milpitas") == "cardoza"
