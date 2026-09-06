"""
test_geocoding_guard.py — Regression tests for _house_number_consistent().

Background
----------
PR #215 adds a house-number-change guard to the Google Maps geocoding fallback
in main.py.  When Nominatim fails on a misspelled street, _geocode_google_maps()
is called as a fallback.  In some cases Google Maps returns a *different* address
at the same approximate location (different house number) rather than correcting
the misspelling.

Bug (Form 9 PDF, 2026-03-08)
-----------------------------
LKP: "300 MORETTE LANE MILPITAS, CA"
Google Maps result: "447 Great Mall Dr APT 110, Milpitas, CA 95035"

House number changed (334 → 447) → Google geocoded a different address entirely.
Because geo was set unconditionally, staging and CalTopo anchored to the wrong
location.  No WARNING fired because geo was non-None.  The deferred
_street_corrections accumulator logged this as a "Residence address corrected"
entry with LKP text as the input — a misleading Event Log entry.

Fix
---
_house_number_consistent(query_addr, gm_formatted_addr) returns False when:
  - Both have a leading house number AND they differ

When False, geo stays None → the existing "LKP could not be geocoded" WARNING
fires → dispatcher is alerted.

Non-regressions confirmed:
  - Same-number spelling fix (TRADEN DR → Tradan Dr): house numbers match → True
  - Park / landmark (no leading number in query): guard skipped → True
  - Google Maps drops apartment suffix (300 MORETTE LN APT 2 → 300 Moretti Ln): True

Run with:
    pytest backend/test_geocoding_guard.py -v
"""

import re
import unicodedata
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Inline mirror of _house_number_consistent() from main.py.
# Update this whenever the production function changes.
# ---------------------------------------------------------------------------

def _house_number_consistent(query_addr: str, gm_formatted_addr: str) -> bool:
    """Mirror of main.py::_house_number_consistent()."""
    def _leading_num(addr: str):
        m = re.match(r"^(\d+)\b", addr.split(",")[0].strip())
        return m.group(1) if m else None

    q_num = _leading_num(query_addr)
    r_num = _leading_num(gm_formatted_addr)
    if q_num and r_num:
        return q_num == r_num
    return True  # Can't check — allow through


# ---------------------------------------------------------------------------
# Form 9 regression — the exact inputs that triggered the bug
# ---------------------------------------------------------------------------

class TestForm9Regression:
    """
    Exact Form 9 inputs that caused the 2026-03-08 live-test bug.

    Form data (confirmed from "SAR Dispatch Form v2 FILLABLE-9 corrected.pdf"):
    - LKP (last_seen_location field): "300 MORETTI LANE, MILPITAS"
    - Residence (mp_address field):   "300 MORETTE LANE MILPITAS, CA 95035"
    - Both have house number 334; real street is "Moretti Lane, Milpitas CA"
    - Google Maps returned "447 Great Mall Dr APT 110" (house 447 ≠ 334 → wrong address)
    """

    def test_lkp_moretti_to_great_mall_dr_returns_false(self):
        """
        Actual LKP text (MORETTI) → 447 Great Mall Dr: 334 ≠ 447 → False.
        """
        query  = "300 MORETTI LANE, MILPITAS, CA"
        result = "447 Great Mall Dr APT 110, Milpitas, CA 95035"
        assert _house_number_consistent(query, result) is False, (
            "Different house numbers must return False — geo must stay None"
        )

    def test_residence_morette_to_great_mall_dr_returns_false(self):
        """
        Actual Residence text (MORETTE) → 447 Great Mall Dr: 334 ≠ 447 → False.
        """
        query  = "300 MORETTE LANE MILPITAS, CA 95035"
        result = "447 Great Mall Dr APT 110, Milpitas, CA 95035"
        assert _house_number_consistent(query, result) is False

    def test_morette_lane_with_ca_only_still_false(self):
        """
        After city-abbrev stripping: "300 MORETTE LANE, CA" — still fails.
        """
        query  = "300 MORETTE LANE, CA"
        result = "447 Great Mall Dr, Milpitas, CA 95035"
        assert _house_number_consistent(query, result) is False


# ---------------------------------------------------------------------------
# Valid spelling correction — must NOT be blocked
# ---------------------------------------------------------------------------

class TestValidSpellingCorrections:
    """Same house number with corrected street spelling — guard must pass."""

    def test_traden_to_tradan_passes(self):
        """
        TRADEN DR → Tradan Dr (PR #178 canonical example).
        Same house number (1950) → True.
        """
        query  = "1100 TRADEN DR, San Jose, CA"
        result = "1100 Tradan Dr, San Jose, CA 95124"
        assert _house_number_consistent(query, result) is True

    def test_moretti_spelling_fix_same_number_passes(self):
        """
        If officer wrote 300 Moretti Lane correctly and Google Maps confirms it
        with the same number, the fix should pass through.
        """
        query  = "300 MORETTI LANE, Milpitas, CA"
        result = "300 Moretti Ln, Milpitas, CA 95035"
        assert _house_number_consistent(query, result) is True

    def test_same_number_different_format_passes(self):
        """
        Number match ignores case and abbreviation differences in street.
        """
        query  = "2000 SAMARITAN DR. SAN JOSE, CA"
        result = "2000 Samaritan Dr, San Jose, CA 95124"
        assert _house_number_consistent(query, result) is True


# ---------------------------------------------------------------------------
# No house number in query — landmark / park — must pass through
# ---------------------------------------------------------------------------

class TestNoHouseNumber:
    """Addresses without a leading number cannot be checked — must pass."""

    def test_park_name_no_number_passes(self):
        query  = "CARDOZA PARK MILPITAS, CA"
        result = "Cardoza Park, Milpitas, CA 95035"
        assert _house_number_consistent(query, result) is True

    def test_city_only_passes(self):
        query  = "MILPITAS, CA"
        result = "Milpitas, CA"
        assert _house_number_consistent(query, result) is True

    def test_no_number_in_result_passes(self):
        """Result without a number — can't check — allow through."""
        query  = "300 MORETTE LANE MILPITAS, CA"
        result = "Great Mall Dr, Milpitas, CA"   # hypothetical; no house number
        assert _house_number_consistent(query, result) is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Boundary and edge-case inputs."""

    def test_empty_query_passes(self):
        assert _house_number_consistent("", "447 Great Mall Dr") is True

    def test_empty_result_passes(self):
        assert _house_number_consistent("300 MORETTE LANE", "") is True

    def test_both_empty_passes(self):
        assert _house_number_consistent("", "") is True

    def test_large_number_mismatch_returns_false(self):
        assert _house_number_consistent("10000 CALVERT DR", "99999 Other St") is False

    def test_same_large_number_passes(self):
        assert _house_number_consistent("10000 CALVERT DR, Cupertino, CA", "10000 Calvert Dr, Cupertino, CA 95014") is True

    def test_apt_in_query_number_still_extracted(self):
        """
        Apt qualifier in query doesn't confuse house-number extraction.
        Leading digits before first non-digit boundary are used.
        """
        # 300 MORETTE LANE APT 2 — leading number is still 334
        query  = "300 MORETTE LANE APT 2, Milpitas"
        result = "300 Moretti Ln, Milpitas, CA 95035"
        assert _house_number_consistent(query, result) is True

    def test_apt_in_query_mismatch_still_detected(self):
        query  = "300 MORETTE LANE APT 2, Milpitas"
        result = "447 Great Mall Dr, Milpitas, CA 95035"
        assert _house_number_consistent(query, result) is False


# ===========================================================================
# City-consistency guard (issue #604)
# ===========================================================================
#
# Bug (2026-07-24 SCPD Moreland, REAL CALLOUT, sccssar-dev v1.11.7)
# ------------------------------------------------------------------
# LKP/Residence: "600 Parkview Dr, Santa Clara, CA 95051"
#   — the real street is "Park View Drive", two words.
# Nominatim result: 37.4412241, -121.887235 — Milpitas, ~13 km / 8 mi away.
#
# "Parkview Dr" (one word) does not exist in Santa Clara, so Nominatim matched
# the same-named street in a different city and ignored BOTH the city and the
# ZIP present in the query. _house_number_consistent() passed, because Milpitas
# also has a 600 Parkview Dr. The Google Maps fallback never ran, because the
# cascade only fires on geocoder failure — never on a confident wrong answer.
#
# Consequence: all 12 staging candidates were in the wrong city, the CalTopo
# LKP/Residence markers were plotted there, and a stale city string survived the
# dispatcher's manual edits into the Everbridge body. Responders were paged to
# drive to the wrong city. Highest-severity finding recorded on this project.
#
# Fix
# ---
# _city_consistent() is the sibling of _house_number_consistent() on the city
# axis. On mismatch, _reconcile_geocode_city() retries via Google Maps (whose
# spelling tolerance resolves the one-word/two-word variant) and prefers a
# city-consistent answer.
#
# DESIGN DECISION (Bill, 2026-07-24): an unresolvable mismatch KEEPS the
# coordinate and raises a dispatcher-facing WARNING — it does not reject.
# Rejecting would cost the whole staging list and leave CalTopo with no seed on
# every false positive. Pinned by TestReconcileSourceParity below.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Inline mirrors from main.py. Update these whenever the production functions
# change — TestReconcileSourceParity pins the pieces most likely to drift.
# ---------------------------------------------------------------------------

_CITY_ALIASES = {
    "alviso": "san jose",
    "willow glen": "san jose",
    "cambrian park": "san jose",
    "almaden valley": "san jose",
    "new almaden": "san jose",
    "berryessa": "san jose",
    "evergreen": "san jose",
    "blossom valley": "san jose",
    "moffett field": "mountain view",
}

_STATE_COMPONENT_RE = re.compile(r"^(?:CA|California)(?:\s+\d{5}(?:-\d{4})?)?$", re.IGNORECASE)


def _normalize_city_name(name):
    """Mirror of main.py::_normalize_city_name()."""
    if not name:
        return None
    decomposed = unicodedata.normalize("NFKD", str(name))
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9 ]", " ", ascii_only.lower())
    cleaned = re.sub(r"^(?:city|town)\s+of\s+", "", cleaned.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    return _CITY_ALIASES.get(cleaned, cleaned)


def _extract_query_city(address):
    """Mirror of main.py::_extract_query_city()."""
    if not address:
        return None
    parts = [p.strip() for p in address.split(",")]
    state_idx = next(
        (i for i in range(len(parts) - 1, -1, -1) if _STATE_COMPONENT_RE.match(parts[i])),
        None,
    )
    if state_idx is None or state_idx < 2:
        return None
    candidate = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", parts[state_idx - 1]).strip()
    if not candidate or re.match(r"^\d", candidate):
        return None
    if re.search(r"\bcounty\b", candidate, re.IGNORECASE):
        return None
    return candidate


def _city_consistent(query_addr, resolved_city):
    """Mirror of main.py::_city_consistent()."""
    requested = _normalize_city_name(_extract_query_city(query_addr))
    resolved = _normalize_city_name(resolved_city)
    if not requested or not resolved:
        return True
    return requested == resolved


class TestMorelandRegression:
    """The 2026-07-24 wrong-city dispatch. These must never both pass silently."""

    LKP_QUERY = "600 Parkview Dr, Santa Clara, CA 95051"

    def test_milpitas_result_for_santa_clara_query_is_inconsistent(self):
        assert _city_consistent(self.LKP_QUERY, "Milpitas") is False

    def test_correct_city_result_is_consistent(self):
        assert _city_consistent(self.LKP_QUERY, "Santa Clara") is True

    def test_house_number_guard_alone_does_not_catch_it(self):
        """The exact reason a second guard was needed.

        Milpitas also has a 600 Parkview Dr, so the PR #215 house-number guard
        sees nothing wrong. If this ever starts returning False, the city guard
        is no longer the only thing standing between OCR and a wrong-city page.
        """
        assert _house_number_consistent(self.LKP_QUERY, "600 Parkview Dr, Milpitas, CA 95035") is True
        assert _city_consistent(self.LKP_QUERY, "Milpitas") is False

    def test_google_maps_spacing_variant_is_accepted(self):
        """Google Maps resolves "Parkview" -> "Park View" and lands in the right city.

        This is what makes the retry worth making: the correct answer was one
        API call away the whole time.
        """
        gm_formatted = "600 Park View Dr, Santa Clara, CA 95051"
        assert _house_number_consistent(self.LKP_QUERY, gm_formatted) is True
        assert _city_consistent(self.LKP_QUERY, _extract_query_city(gm_formatted)) is True


class TestNormalizeCityName:
    """Folding rules. The accent case is the one that decides whether the guard survives."""

    def test_osm_accented_san_jose_folds_to_plain(self):
        """OSM tags San Jose as "San Jose" with an accent.

        Without folding, the guard would fire a false mismatch on the most
        common dispatch city this team has, and would be switched off in a week.
        """
        assert _normalize_city_name("San José") == "san jose"
        assert _normalize_city_name("San Jose") == "san jose"

    def test_case_and_punctuation_folded(self):
        assert _normalize_city_name("SANTA CLARA") == "santa clara"
        assert _normalize_city_name("Santa  Clara.") == "santa clara"

    def test_city_of_prefix_stripped(self):
        assert _normalize_city_name("City of Santa Clara") == "santa clara"

    def test_neighborhood_alias_resolves_to_parent_city(self):
        assert _normalize_city_name("Alviso") == "san jose"
        assert _normalize_city_name("Willow Glen") == "san jose"
        assert _normalize_city_name("Moffett Field") == "mountain view"

    def test_empty_and_none_return_none(self):
        assert _normalize_city_name(None) is None
        assert _normalize_city_name("") is None
        assert _normalize_city_name("   ") is None


class TestExtractQueryCity:
    """One extractor serves both the outbound query and the Google Maps formatted_address."""

    def test_city_state_zip(self):
        assert _extract_query_city("600 Parkview Dr, Santa Clara, CA 95051") == "Santa Clara"

    def test_city_state_no_zip(self):
        assert _extract_query_city("123 Main St, Milpitas, CA") == "Milpitas"

    def test_state_only_query_returns_none(self):
        """"2000 GAZELLE DR, CA" — the CA-append rule with no city context."""
        assert _extract_query_city("2000 GAZELLE DR, CA") is None

    def test_park_name_with_city(self):
        assert _extract_query_city("Cardoza Park, Milpitas, CA") == "Milpitas"

    def test_county_context_returns_none(self):
        """_AGENCY_CITY maps the sheriff's office to "Santa Clara County, CA".

        A county is not a city; comparing it against a resolved locality would
        flag every county-agency callout as a mismatch.
        """
        assert _extract_query_city("616 Foo Rd, Santa Clara County, CA") is None

    def test_city_without_comma_returns_none(self):
        """Documented limitation — skip rather than guess.

        "300 MORETTE LANE MILPITAS, CA" has the city welded to the street
        component. Returning None means the guard sits this one out.
        """
        assert _extract_query_city("300 MORETTE LANE MILPITAS, CA") is None

    def test_landmark_prefix_does_not_shift_the_city(self):
        """Officers prefix a landmark; main.py's own comments cite this shape.

        Reading component #1 here would call "2000 SAMARITAN DR" the city and
        warn against a perfectly correct geocode.
        """
        q = "GOOD SAM HOSPITAL, 2000 SAMARITAN DR, SAN JOSE, CA 95124"
        assert _extract_query_city(q) == "SAN JOSE"
        assert _city_consistent(q, "San José") is True

    def test_unit_qualifier_does_not_shift_the_city(self):
        """"Space"/"Lot" are outside _APT_STRIP_RE's vocabulary — routine at
        mobile-home parks, which are a recurring SAR address shape."""
        q = "123 Main St, Space 45, San Jose, CA"
        assert _extract_query_city(q) == "San Jose"
        assert _city_consistent(q, "San José") is True

    def test_trailing_country_component_tolerated(self):
        """Google Maps sometimes appends ", USA" to formatted_address."""
        assert _extract_query_city("600 Park View Dr, Santa Clara, CA 95051, USA") == "Santa Clara"

    def test_street_line_candidate_rejected(self):
        """A leading house number in the city slot means the parse is wrong."""
        assert _extract_query_city("Foo Landmark, 123 Main St, CA") is None

    def test_no_comma_at_all_returns_none(self):
        assert _extract_query_city("600 Parkview Dr") is None

    def test_none_and_empty(self):
        assert _extract_query_city(None) is None
        assert _extract_query_city("") is None


class TestCityConsistentAllowsThrough:
    """Mirrors _house_number_consistent()'s can't-check-means-allow contract.

    Every case here is a potential false positive. Under the accept-and-warn
    design a false positive costs one noisy Event Log line rather than the whole
    staging list -- but the Event log policy says noise makes real warnings
    invisible, so they still matter.
    """

    def test_resolved_city_missing_allows_through(self):
        assert _city_consistent("600 Parkview Dr, Santa Clara, CA", None) is True

    def test_query_city_missing_allows_through(self):
        assert _city_consistent("2000 GAZELLE DR, CA", "Milpitas") is True

    def test_county_agency_context_allows_through(self):
        assert _city_consistent("616 Foo Rd, Santa Clara County, CA", "San Jose") is True

    def test_neighborhood_as_city_allows_through(self):
        """Officer writes a neighborhood; the geocoder answers with the parent city."""
        assert _city_consistent("1 Foo St, Alviso, CA", "San José") is True

    def test_accented_resolution_allows_through(self):
        assert _city_consistent("123 Main St, San Jose, CA 95124", "San José") is True

    def test_both_missing_allows_through(self):
        assert _city_consistent("", None) is True

    def test_genuine_mutual_aid_mismatch_is_flagged(self):
        """Not a false positive — a real out-of-county resolution should warn."""
        assert _city_consistent("123 Main St, Gilroy, CA", "Fresno") is False


def _find_stale_locality(superseded_locality, current_locality, surfaces):
    """Mirror of main.py::_find_stale_locality()."""
    superseded_key = _normalize_city_name(superseded_locality)
    current_key = _normalize_city_name(current_locality)
    if not superseded_key or not current_key or superseded_key == current_key:
        return None, None

    def _fold(s):
        decomposed = unicodedata.normalize("NFKD", s)
        return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()

    pattern = re.compile(rf"\b{re.escape(_fold(str(superseded_locality)))}\b")
    for label, text in surfaces:
        if text and pattern.search(_fold(text)):
            return str(superseded_locality), label
    return None, None


class TestStaleLocalityGuard:
    """Issue #605 — the half of the 2026-07-24 dispatch that reached responders.

    The LKP geocoded to Milpitas. The dispatcher overrode staging to Santa Clara,
    hand-edited the Everbridge text, and missed one remaining "Milpitas". The
    notification went out and responders began driving to the wrong town.
    """

    TITLE = ("notification title", "SOSAR - Callout")

    def _surfaces(self, body="", staging=""):
        return (self.TITLE, ("notification text", body), ("staging address", staging))

    def test_the_2026_07_24_body_is_flagged(self):
        """resolved=Milpitas (wrong), requested=Santa Clara, body still says Milpitas."""
        body = ("SOSAR - Missing adult. Stage at the library.\n"
                "Subject last seen near Milpitas. Respond code 2.")
        assert _find_stale_locality("Milpitas", "Santa Clara",
                                    self._surfaces(body=body)) == ("Milpitas", "notification text")

    def test_clean_surfaces_are_not_flagged(self):
        body = "SOSAR - Missing adult. Stage at 600 Moreland Way, Santa Clara."
        assert _find_stale_locality("Milpitas", "Santa Clara",
                                    self._surfaces(body=body)) == (None, None)

    def test_staging_address_alone_is_flagged(self):
        """The surface that reaches responders even with a clean body.

        The auto-composed EB body only ever names the city written on the FORM
        (_ebParseCityFromLkp reads the LKP text), never the city the geocoder
        resolved. So on 2026-07-24, dispatching without a staging override would
        have sent a wrong-city staging address into the Slack welcome and both
        map links while the body looked perfectly clean.
        """
        body = "SOSAR - Missing 35 year old in Santa Clara. Please respond."
        staging = "1000 E Calaveras Blvd, Milpitas"
        assert _find_stale_locality("Milpitas", "Santa Clara",
                                    self._surfaces(body=body, staging=staging)) == (
            "Milpitas", "staging address")

    def test_surface_precedence_reports_the_first_match(self):
        """Title before text before staging — deterministic remedy wording."""
        assert _find_stale_locality(
            "Milpitas", "Santa Clara",
            (("notification title", "SOSAR - Milpitas callout"),
             ("notification text", "also Milpitas"),
             ("staging address", "somewhere in Milpitas")),
        ) == ("Milpitas", "notification title")

    def test_no_divergence_never_flags(self):
        """Geocode agrees with the address — the common case, must be silent."""
        body = "SOSAR - Missing adult in Santa Clara. Stage at the library."
        assert _find_stale_locality("Santa Clara", "Santa Clara",
                                    self._surfaces(body=body)) == (None, None)

    def test_benign_cross_city_staging_is_not_the_trigger(self):
        """Staging across a city line is routine and must NOT arm this gate.

        _ebComposeBody() writes the LKP city into every body by construction
        ("missing 45 year old in Los Gatos"), so a guard keyed on
        LKP-vs-STAGING locality would fire on ordinary dispatches and train the
        dispatcher to click through the one warning that matters. The gate is
        keyed on the #604 suspect flag instead; here the geocode was correct,
        so resolved == requested and nothing fires even though staging really
        is in another city.
        """
        body = "SOSAR - Missing 45 year old in Los Gatos. Please respond."
        staging = "123 Foo Ave, San Jose"
        assert _find_stale_locality("Los Gatos", "Los Gatos",
                                    self._surfaces(body=body, staging=staging)) == (None, None)

    def test_accent_mismatch_still_matches(self):
        """OSM says "San Jos\u00e9"; the dispatcher types "San Jose"."""
        assert _find_stale_locality(
            "San Jos\u00e9", "Milpitas",
            (("notification text", "Stage in San Jose"),))[0] == "San Jos\u00e9"

    def test_word_boundary_prevents_substring_false_positive(self):
        """"Santa Clara" must not match inside "Santa Clarita"."""
        assert _find_stale_locality(
            "Santa Clara", "Gilroy",
            (("notification text", "Mutual aid to Santa Clarita"),)) == (None, None)

    def test_unknown_locality_allows_through(self):
        """Same can't-check-means-allow contract as the other guards."""
        assert _find_stale_locality(None, "Santa Clara",
                                    (("notification text", "Milpitas"),)) == (None, None)
        assert _find_stale_locality("Milpitas", None,
                                    (("notification text", "Milpitas"),)) == (None, None)
        assert _find_stale_locality("", "", (("notification text", ""),)) == (None, None)

    def test_empty_surfaces_do_not_crash(self):
        assert _find_stale_locality(
            "Milpitas", "Santa Clara",
            (("notification title", None), ("notification text", ""))) == (None, None)


class TestStaleLocalityGateOrdering:
    """The gate MUST precede the Step 1.5 skeleton .create().

    If it ran after, a blocked dispatch would leave a tombstone and the
    dispatcher's CORRECTED retry would be rejected as a duplicate ("dispatch
    already in progress") — converting a safety guard into a lockout during an
    active callout. This ordering is the whole reason the gate is at Step 1.4.
    """

    @staticmethod
    def _main_source():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    def test_gate_runs_before_the_skeleton_create(self):
        src = self._main_source()
        # Anchor on the CALL SITES. Searching for "_find_stale_locality(" alone
        # matches its own `def` near the top of the file, which sits before the
        # skeleton unconditionally — the assertion would pass no matter where
        # the gate actually ran.
        gate = src.find("_stale_locality, _stale_where = _find_stale_locality(")
        skeleton = src.find(".document(event_id).create(")
        assert gate != -1, "stale-locality gate CALL not found in main.py"
        assert skeleton != -1, "skeleton .create() CALL not found in main.py"
        assert gate < skeleton, (
            "the stale-locality gate moved AFTER the Step 1.5 skeleton .create() — "
            "a blocked dispatch now leaves a tombstone, so the corrected retry will "
            "be rejected as a duplicate dispatch"
        )

    def test_gate_is_ackable(self):
        """Hard-confirm, not hard-block — a false-positive block on a real
        callout would be worse than the bug it prevents."""
        src = self._main_source()
        assert "stale_locality_ack" in src, (
            "the acknowledge path is gone — the guard became an unconditional block"
        )

    def test_gate_returns_422_not_409(self):
        """409 is spoken for by the double-dispatch tombstone; the frontend
        branches on it. Colliding would make a stale-city warning read as
        'dispatch already in progress'."""
        src = self._main_source()
        # Window = the gate block only, bounded by the NEXT "# ---- Step"
        # header. Anchoring the end on a specific later step is fragile: the
        # gate's own prose mentions other step numbers, and the block has
        # already been renumbered once (1.4 -> 0.5) when it moved above the
        # rate limiter.
        start = src.find("# ---- Step 0.5: Stale-locality hard-confirm")
        assert start != -1, "stale-locality gate header not found in main.py"
        end = src.find("\n    # ---- Step ", start + 1)
        assert end != -1 and end > start, "no following step header — window unbounded"
        window = src[start:end]
        assert "status_code=422" in window, "stale-locality gate no longer returns 422"
        assert "status_code=409" not in window, "stale-locality gate must not use 409"

    def test_gate_is_armed_only_by_the_604_suspect_flag(self):
        """The arming condition is the whole false-positive story.

        Keying on lkp_locality_suspect restricts the gate to incidents where
        #604 already found the geocode untrustworthy. Widening it to a plain
        LKP-vs-staging city comparison would fire on every legitimate
        cross-city staging override.
        """
        src = self._main_source()
        start = src.find("# ---- Step 0.5: Stale-locality hard-confirm")
        end = src.find("\n    # ---- Step ", start + 1)
        window = src[start:end]
        assert 'get("lkp_locality_suspect")' in window, (
            "the stale-locality gate is no longer armed by the #604 suspect flag — "
            "it will fire on ordinary cross-city staging"
        )
        # staging_address is a SEARCH SURFACE, never the arming condition and
        # never the `current_locality` comparison. Those are different roles and
        # the difference is the whole false-positive story:
        #   as a surface  -> "the staging address names the known-bad city"  (right)
        #   as a compare  -> "staging is in a different city than the LKP"   (fires
        #                     on every legitimate cross-city override)
        # It was ALSO a silent no-op as a comparison: production staging strings
        # ("55 North 7th Street, San Jose") carry no state component, so
        # _extract_query_city returns None and the gate never armed at all.
        assert '("staging address", body.get("staging_address")' in window, (
            "staging_address is no longer scanned — a wrong-city staging address "
            "reaches responders via the Slack welcome and both map links even when "
            "the auto-composed body is clean"
        )
        assert '_extract_query_city(body.get("staging_address")' not in window, (
            "staging_address is being used as a locality COMPARISON again — that "
            "is the silent no-op the reviewer caught, and widening it to a real "
            "comparison would fire on every legitimate cross-city staging override"
        )

    def test_gate_runs_before_the_rate_limiter(self):
        """A confirm cycle must not burn the shared 5/min budget.

        The check is pure — no external calls, no writes — so charging a token
        per confirm would let a correction cycle 429 the dispatcher at exactly
        the wrong moment.
        """
        src = self._main_source()
        gate = src.find("_stale_locality, _stale_where = _find_stale_locality(")
        # the send_notification rate-limit call, not the ones in other handlers
        limiter = src.find("# Rate limit (separate, tighter cap than /ocr")
        assert gate != -1 and limiter != -1
        assert gate < limiter, (
            "the stale-locality gate moved BELOW check_rate_limits() — each "
            "hard-confirm now costs a rate-limit token"
        )


class TestAgencyCityValuesDoNotFalsePositive:
    """Every _AGENCY_CITY value is a city string main.py appends to a geocode query.

    If one of them extracts to something the guard then flags against a correct
    geocode, every callout from that agency gets a spurious WARNING. Swept
    exhaustively rather than sampled: the dict grows whenever a new agency shows
    up on a form, and the next person to add one should find out here.

    Source of truth: backend/main.py::_AGENCY_CITY.
    """

    @staticmethod
    def _agency_city_values():
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        block = re.search(r"_AGENCY_CITY: dict\[str, str\] = \{(.*?)\n\}", src, re.DOTALL)
        assert block, "_AGENCY_CITY not found in main.py"
        return dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', block.group(1)))

    def test_every_agency_city_extracts_the_expected_city(self):
        """Compare against an INDEPENDENTLY derived expectation.

        Asserting `_city_consistent(query, _extract_query_city(query))` would be
        a tautology — both sides reduce to the same expression, so it holds for
        any input including garbage. The expected value here is derived by
        string-splitting the _AGENCY_CITY value instead.
        """
        values = self._agency_city_values()
        assert len(values) >= 25, "sanity: _AGENCY_CITY unexpectedly small — regex drift?"
        for agency, city_context in sorted(values.items()):
            query = f"123 Main St, {city_context}"  # how main.py assembles it
            extracted = _extract_query_city(query)
            expected = city_context.rsplit(",", 1)[0].strip()  # "Milpitas, CA" -> "Milpitas"
            if "county" in expected.lower() or expected.lower() in ("california", "ca"):
                assert extracted is None, (
                    f"{agency} -> {city_context!r} is county/state-level context; the guard "
                    f"must sit out, but it extracted {extracted!r} and would compare against it"
                )
                continue
            assert extracted == expected, (
                f"{agency} -> {city_context!r} extracted {extracted!r}, expected {expected!r} "
                f"— every callout from this agency would compare against the wrong string"
            )

    def test_agency_city_survives_a_correct_geocode(self):
        """A correct geocode of an agency-anchored query must never warn.

        The resolved side is supplied independently (accented OSM spelling where
        that is what Nominatim actually returns), so this is not self-comparison.
        """
        osm_spelling = {"San Jose": "San José"}
        for agency, city_context in sorted(self._agency_city_values().items()):
            query = f"123 Main St, {city_context}"
            plain = city_context.rsplit(",", 1)[0].strip()
            resolved = osm_spelling.get(plain, plain)
            assert _city_consistent(query, resolved) is True, (
                f"{agency}: correct geocode to {resolved!r} flagged as a mismatch"
            )

    def test_county_and_state_agencies_skip_the_check(self):
        """The sheriff's office and CHP have no city to check against."""
        values = self._agency_city_values()
        for agency in ("SCSO", "SCCSO", "CHP"):
            if agency not in values:
                continue
            assert _extract_query_city(f"123 Main St, {values[agency]}") is None


class TestReconcileSourceParity:
    """Pin the pieces of main.py most likely to drift away from these mirrors.

    Cross-file literal pin policy (CLAUDE.md): any literal living in 2+ files
    gets pinned with a source-of-truth comment. Source of truth is backend/main.py.
    """

    @staticmethod
    def _main_source():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    def test_alias_keys_match_production(self):
        src = self._main_source()
        block = re.search(r"_CITY_ALIASES: dict\[str, str\] = \{(.*?)\n\}", src, re.DOTALL)
        assert block, "_CITY_ALIASES not found in main.py"
        prod_keys = set(re.findall(r'"([^"]+)":\s*"[^"]+"', block.group(1)))
        assert prod_keys == set(_CITY_ALIASES), (
            "backend/main.py::_CITY_ALIASES drifted from the mirror in this file.\n"
            f"  only in main.py: {sorted(prod_keys - set(_CITY_ALIASES))}\n"
            f"  only in mirror:  {sorted(set(_CITY_ALIASES) - prod_keys)}"
        )

    def test_mirrored_helper_bodies_match_production(self):
        """Every helper mirrored in this file must still match main.py.

        The mirrors exist because the suite deliberately does not import main
        (heavyweight GCP deps). Without this pin, a change to the production
        helper cannot fail these tests — they would keep exercising a stale
        copy and reporting green.
        """
        src = self._main_source()
        for name in ("_normalize_city_name", "_extract_query_city",
                     "_city_consistent", "_find_stale_locality"):
            prod = re.search(rf"^def {name}\(.*?(?=\n\n(?:def |async def |# -{{10,}}))",
                             src, re.DOTALL | re.MULTILINE)
            assert prod, f"{name} not found in main.py — mirror pin cannot verify it"
            import ast
            import inspect
            import textwrap

            def _shape(fn_src):
                """AST of the function with docstrings and type annotations removed.

                Compared structurally rather than as text: the mirrors omit
                annotations and carry their own docstrings/comments, so a
                line-by-line diff reports noise. Formatting, comments, and
                line breaks are all invisible here; a real logic change is not.
                """
                tree = ast.parse(textwrap.dedent(fn_src))
                for node in ast.walk(tree):
                    if isinstance(node, ast.arg):
                        node.annotation = None
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        node.returns = None
                        if (node.body and isinstance(node.body[0], ast.Expr)
                                and isinstance(node.body[0].value, ast.Constant)
                                and isinstance(node.body[0].value.value, str)):
                            node.body = node.body[1:]
                return ast.dump(tree)

            assert _shape(prod.group(0)) == _shape(inspect.getsource(globals()[name])), (
                f"{name}: the mirror in test_geocoding_guard.py has drifted from "
                f"backend/main.py (source of truth). Update the mirror — the tests "
                f"above are exercising the stale copy."
            )

    def test_every_geo_unpack_expects_four_elements(self):
        """Guard the widened-tuple arity across all consumers.

        `_geocode_nominatim` / `_geocode_lkp_smart` return a 4-tuple since #604.
        A leftover 3-element unpack raises ValueError — and the two in the
        map_data builder sit inside a `try:` whose handler logs
        "map_data build failed (non-fatal)" and sets `map_data = {}`, so the
        crash is SILENT: /ocr returns an empty map_data, /create-map then 400s
        with "map_data is required", and no CalTopo map is produced at all.

        No test exercises the /ocr handler, so pytest stays green while every
        real dispatch breaks. Two such sites were missed on the first pass of
        this very PR — hence a mechanical sentinel rather than a careful grep.
        """
        src = self._main_source()
        offenders = []
        for m in re.finditer(r"^\s*([\w, ]+?)\s*=\s*(geo|geo_res|nom_result|result)\s*$",
                             src, re.MULTILINE):
            targets = [t.strip() for t in m.group(1).split(",")]
            if len(targets) > 1 and len(targets) != 4:
                line_no = src[: m.start()].count("\n") + 1
                offenders.append(f"main.py:{line_no}: {m.group(0).strip()}")
        assert not offenders, (
            "geocoder tuple unpack with the wrong arity (expected 4 elements):\n  "
            + "\n  ".join(offenders)
        )

    def test_unresolvable_mismatch_keeps_the_coordinate(self):
        """Locked behaviour: warn, do not reject.

        Rejecting would kill the staging list and leave _build_seed_feature()
        with no LKP and no Residence seed on every false positive. If a future
        PR changes this to `return None`, it needs Bill's sign-off first.
        """
        src = self._main_source()
        body = re.search(
            r"async def _reconcile_geocode_city\(.*?\n(?=\n\n# -{10,})", src, re.DOTALL
        )
        assert body, "_reconcile_geocode_city not found in main.py"
        assert "return geo, (" in body.group(0), (
            "_reconcile_geocode_city no longer returns the original geo on an "
            "unresolvable city mismatch — the accept-and-warn design decision "
            "(Bill, 2026-07-24) was reverted."
        )

    def test_city_names_never_reach_the_logger(self):
        """"No PII in logs" — a locality is address data.

        City names belong in the Event Log entry (dispatcher-facing, sits next
        to the address in the summary), never in a logger.* call.
        """
        src = self._main_source()
        body = re.search(
            r"async def _reconcile_geocode_city\(.*?\n(?=\n\n# -{10,})", src, re.DOTALL
        )
        assert body
        for call in re.findall(r"logger\.(?:info|warning|error)\((.*?)\)\n", body.group(0), re.DOTALL):
            # Inspect only the interpolated ARGUMENTS — the format string itself is
            # allowed to contain the words "requested"/"resolved" as English prose.
            args = re.sub(r'^\s*(?:f?"[^"]*"|f?\'[^\']*\')\s*,?', "", call, count=1).strip()
            for banned in ("requested", "resolved", "geo[3]", "gm_city", "query"):
                assert banned not in args, (
                    f"city/address value interpolated into a logger call: {call.strip()}"
                )


# ===========================================================================
# Staging distance sanity guard
# ===========================================================================
#
# Bug (2026-07-24 SCPD Moreland, REAL CALLOUT — seen first-hand on re-run)
# -----------------------------------------------------------------------
# The officer staging field OCR'd as the NEXT field's LABEL, "CalTopo Map ID:".
# Google Maps "corrected" that to "California", which geocoded to the state
# centroid ~250 km away and was plotted as staging marker #8. Another dispatcher
# deleted it mid-callout as a distraction. The same bogus correction also
# rewrote the summary text, leaving a stray "California" line under
# "Staging Area for Resources:" in BOTH dispatch summaries.
#
# Why no existing guard caught it:
#   _house_number_consistent()          -> neither string has a house number,
#                                          so "can't check -> allow through"
#   _is_substantive_street_correction() -> only asks whether the WORDS changed
#   _city_consistent()  (#604)          -> _extract_query_city("CalTopo Map ID:, CA")
#                                          returns None (state at index 1) -> can't check
#
# Distance is the axis that works: it tests the RESULT, not the input, so it
# covers every future variant instead of the one string that happened to appear.
# ---------------------------------------------------------------------------

import math as _math


def _haversine_m(lat1, lon1, lat2, lon2):
    """Mirror of main.py::_haversine_m()."""
    R = 6_371_000.0
    phi1, phi2 = _math.radians(lat1), _math.radians(lat2)
    dphi = _math.radians(lat2 - lat1)
    dlambda = _math.radians(lon2 - lon1)
    a = _math.sin(dphi / 2) ** 2 + _math.cos(phi1) * _math.cos(phi2) * _math.sin(dlambda / 2) ** 2
    return R * 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1 - a))


_MAX_STAGING_DIST_M = 50_000


def _staging_geocode_implausible(lkp_geo, lat, lng):
    """Mirror of main.py::_staging_geocode_implausible()."""
    if not lkp_geo or lat is None or lng is None:
        return False
    return _haversine_m(lkp_geo[0], lkp_geo[1], lat, lng) > _MAX_STAGING_DIST_M


# Real coordinates from the 2026-07-24 incident.
_LKP_SANTA_CLARA = (37.39700, -121.94500, "Santa Clara", "Santa Clara")
_CALIFORNIA_CENTROID = (36.77830, -119.41790)   # what "California" geocodes to
_THAMIEN_PARK = (37.39810, -121.94660)          # the #1 recommendation, 0.08 mi away


class TestStagingDistanceGuard:

    def test_california_centroid_is_rejected(self):
        assert _staging_geocode_implausible(_LKP_SANTA_CLARA, *_CALIFORNIA_CENTROID) is True

    def test_the_rejected_distance_is_genuinely_absurd(self):
        """Sanity-check the fixture itself, so the test above can't pass for
        the wrong reason if someone edits the coordinates."""
        d = _haversine_m(_LKP_SANTA_CLARA[0], _LKP_SANTA_CLARA[1], *_CALIFORNIA_CENTROID)
        assert d > 200_000, f"fixture drift: expected >200 km, got {d/1000:.0f} km"

    def test_real_staging_recommendation_is_accepted(self):
        """Thamien Park — the actual #1 recommendation, 0.08 mi from the LKP."""
        assert _staging_geocode_implausible(_LKP_SANTA_CLARA, *_THAMIEN_PARK) is False

    def test_mutual_aid_is_not_a_false_positive(self):
        """THE scenario Bill raised before approving this guard.

        On mutual aid the team stages far from home but close to THAT
        incident's LKP. The check is LKP-relative, not home-county-relative,
        so a callout 200 km away with staging 3 km from its own LKP passes.
        """
        fresno_lkp = (36.75000, -119.77000, "Fresno", "Fresno")
        staging_3km_away = (36.77700, -119.77000)
        assert _haversine_m(_LKP_SANTA_CLARA[0], _LKP_SANTA_CLARA[1],
                            fresno_lkp[0], fresno_lkp[1]) > 200_000  # genuinely far from home
        assert _staging_geocode_implausible(fresno_lkp, *staging_3km_away) is False

    def test_no_lkp_anchor_allows_through(self):
        """Same can't-check-means-allow contract as the sibling guards."""
        assert _staging_geocode_implausible(None, *_CALIFORNIA_CENTROID) is False

    def test_missing_coords_allow_through(self):
        assert _staging_geocode_implausible(_LKP_SANTA_CLARA, None, None) is False

    def test_boundary_just_inside_is_accepted(self):
        """~45 km north — implausible in practice but inside the threshold.
        Pinned so a future tightening is a deliberate, visible change."""
        lat, lng = _LKP_SANTA_CLARA[0] + 0.40, _LKP_SANTA_CLARA[1]
        d = _haversine_m(_LKP_SANTA_CLARA[0], _LKP_SANTA_CLARA[1], lat, lng)
        assert d < _MAX_STAGING_DIST_M
        assert _staging_geocode_implausible(_LKP_SANTA_CLARA, lat, lng) is False

    def test_boundary_just_outside_is_rejected(self):
        lat, lng = _LKP_SANTA_CLARA[0] + 0.50, _LKP_SANTA_CLARA[1]
        d = _haversine_m(_LKP_SANTA_CLARA[0], _LKP_SANTA_CLARA[1], lat, lng)
        assert d > _MAX_STAGING_DIST_M
        assert _staging_geocode_implausible(_LKP_SANTA_CLARA, lat, lng) is True


class TestStagingDistanceWiring:
    """Pin the two things that make the guard actually work in the handler."""

    @staticmethod
    def _main_source():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    def test_threshold_matches_production(self):
        src = self._main_source()
        m = re.search(r"^_MAX_STAGING_DIST_M = ([\d_]+)", src, re.MULTILINE)
        assert m, "_MAX_STAGING_DIST_M not found in main.py"
        assert int(m.group(1).replace("_", "")) == _MAX_STAGING_DIST_M, (
            "backend/main.py::_MAX_STAGING_DIST_M drifted from the mirror in this file"
        )

    def test_rejected_google_maps_geocode_skips_the_street_correction(self):
        """The bogus 'CalTopo Map ID:' -> 'California' correction is what put a
        stray 'California' line into BOTH dispatch summaries. A geocode we do
        not trust must not be trusted to respell the summary text either.

        Verified structurally: the rejection branch must come BEFORE the branch
        that calls _apply_street_correction_to_summary, and must not call it.
        """
        src = self._main_source()
        reject = src.find("Staging Pass B Google Maps geocode rejected")
        accept = src.find("Staging Pass B Google Maps geocoded")
        assert reject != -1, "Google Maps distance-rejection branch not found"
        assert accept != -1, "Google Maps accept branch not found"
        assert reject < accept, (
            "the distance rejection no longer precedes the accept branch — a "
            "too-far geocode will be accepted and its 'correction' applied"
        )
        # Anchor on the branch's CONDITION, not its trailing logger call. The
        # logger.warning is the LAST statement of the rejection branch, so a
        # window starting there covers none of the branch body — the assertion
        # would hold no matter what the branch did.
        branch_start = src.rfind(
            "elif gm_tuple and not nom_result and _staging_geocode_implausible", 0, reject
        )
        assert branch_start != -1, "GM rejection branch condition not found in main.py"
        window = src[branch_start:reject]
        assert window.strip(), "empty window — this pin is not checking anything"
        assert "_apply_street_correction_to_summary" not in window, (
            "the rejection branch applies a street correction from a geocode it "
            "just rejected — this is what leaked 'California' into both summaries"
        )

    @classmethod
    def _nominatim_reject_sub_branches(cls):
        """(officer window, non-officer window) of the Nominatim distance rejection.

        #773 split this branch in two, and the two halves must now be pinned
        SEPARATELY: an assertion over the whole branch cannot tell "the officer
        address was kept" from "the guard was deleted". Both windows are bound
        on real code markers at both ends, and comments are stripped before any
        negative assertion — a rationale comment naming a literal is otherwise
        indistinguishable from the code that uses it.
        """
        src = cls._main_source()
        start = src.find("if nom_result and _staging_geocode_implausible")
        assert start != -1, "Nominatim rejection branch condition not found in main.py"
        officer = src.index("if pg_is_officer:", start)
        else_at = src.index("\n                    else:", officer)
        reject = src.index("Staging Pass B Nominatim geocode rejected", else_at)
        strip = lambda t: "\n".join(l.split("#")[0] for l in t.splitlines())
        return strip(src[officer:else_at]), strip(src[else_at:reject])

    def test_officer_address_is_kept_not_rejected(self):
        """#773 — an officer-written ADDRESS beats the distance guard.

        2026-08-23 Placer/Auburn: a one-character city misspelling anchored the
        LKP ~370 mi away, and this branch measured the officer's CORRECT
        staging address against that anchor and discarded it. Second occurrence
        of the class #668 fixed for written-out coordinates.
        """
        officer_window, _ = self._nominatim_reject_sub_branches()
        assert officer_window.strip(), "empty window — this pin is not checking anything"
        assert 'staging_entries[entry_idx]["lat"]' in officer_window, (
            "the officer sub-branch no longer stores the geocoded coordinate — "
            "the officer's address is being discarded on distance again (#773)"
        )
        assert "_staging_address_distance_note(" in officer_window, (
            "the officer address is kept but the dispatcher is not told it "
            "resolved far from the LKP"
        )

    def test_rejected_entry_leaves_coords_unset_for_the_officer_fallback(self):
        """Rejection must behave like a geocode FAILURE, not delete the entry.

        Scoped to the NON-officer sub-branch since #773. Alternates are Gemini's
        recommendations, and on a bad anchor every one of them is far by
        construction — on 2026-08-23 the list was seven Las Vegas POIs — so
        keeping them would plot the whole wrong-region list.
        """
        _, alt_window = self._nominatim_reject_sub_branches()
        assert alt_window.strip(), "empty window — this pin is not checking anything"
        assert 'staging_entries[entry_idx]["lat"]' not in alt_window, (
            "the non-officer rejection branch assigns coordinates — it must "
            "leave them unset so the entry is treated as a geocode failure"
        )
        assert "_staging_distance_note(" in alt_window, (
            "the non-officer rejection no longer tells the dispatcher why the "
            "entry vanished"
        )

    def test_nominatim_rejection_precedes_the_accept_branch(self):
        """Reordering the Nominatim legs would make the rejection unreachable.

        The Google Maps leg has an equivalent ordering pin; without this one,
        the Nominatim leg could be reordered with no test failure.
        """
        src = self._main_source()
        reject = src.find("Staging Pass B Nominatim geocode rejected")
        accept = src.find("Staging Pass B Nominatim geocoded")
        assert reject != -1 and accept != -1
        assert reject < accept, (
            "the Nominatim distance rejection no longer precedes its accept "
            "branch — too-far Nominatim results will be accepted"
        )

    def test_officer_note_does_not_contradict_the_lkp_fallback(self):
        """An officer entry with no coords DOES get a marker, at the LKP.

        Claiming "no map marker was plotted" on that path contradicts the very
        next Event Log line, which the fallback emits saying the Command Post
        marker was placed at the LKP.
        """
        src = self._main_source()
        start = src.find("def _staging_distance_note(")
        assert start != -1
        body = src[start:src.find("\ndef ", start + 1)]
        assert "is_officer" in body and "Command Post marker was placed at the LKP" in body, (
            "_staging_distance_note no longer distinguishes the officer path — it "
            "will claim no marker was plotted while the fallback plots one"
        )


class TestCreateMapStaleLocalityGate:
    """Issue #622 — the stale-locality gate that runs BEFORE map creation.

    Map creation is a point of no return the app cannot undo: it deliberately
    holds no CalTopo DELETE privileges (per Bill 2026-05-30), so a wrong-city
    map is permanent until a human removes it in the CalTopo UI. Gating only at
    dispatch (#605) let a fully-populated wrong-city map exist before the
    dispatcher was ever asked to confirm — 9 markers in Milpitas for a Santa
    Clara address, observed live 2026-07-25.
    """

    @staticmethod
    def _main_source():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @staticmethod
    def _create_map_body(src):
        """The create_map handler body only — bounded by the NEXT top-level def.

        Bounding matters: several other handlers raise 422s and call
        check_rate_limits, so an unbounded search would happily pass on another
        handler's code.
        """
        start = src.find("async def create_map(")
        assert start != -1, "create_map handler not found in main.py"
        end = src.find("\n@app.", start + 1)
        if end == -1:
            end = src.find("\nasync def ", start + 1)
        assert end != -1 and end > start, "could not bound the create_map handler"
        return src[start:end]

    def test_gate_precedes_the_caltopo_post(self):
        """The whole point: no wrong-city map may exist before the confirm."""
        body = self._create_map_body(self._main_source())
        gate = body.find('"code": "stale_locality_map"')
        # Anchor on the CALL SITE, not the bare name: the gate's own comment
        # mentions build_incident_map, and a bare-name search matched that
        # comment first — which sits BEFORE the gate and made this pin fail for
        # the wrong reason. Had the comment sat after the gate instead, the pin
        # would have PASSED vacuously while comparing against prose.
        post = body.find("functools.partial(build_incident_map")
        assert gate != -1, "create-map stale-locality gate not found"
        assert post != -1, "build_incident_map CALL SITE not found in create_map"
        assert gate < post, (
            "the create-map stale-locality gate moved AFTER build_incident_map — "
            "a wrong-city CalTopo map is now created BEFORE the dispatcher is asked "
            "to confirm, and the app cannot delete it"
        )

    def test_gate_precedes_the_rate_limiter(self):
        """A correction cycle must not burn the shared 5/min budget."""
        body = self._create_map_body(self._main_source())
        gate = body.find('"code": "stale_locality_map"')
        limiter = body.find("await check_rate_limits(")
        assert gate != -1 and limiter != -1
        assert gate < limiter, (
            "the create-map gate moved BELOW check_rate_limits() — each "
            "hard-confirm now costs a rate-limit token"
        )

    def test_gate_is_armed_only_by_the_604_suspect_flag(self):
        """Arming on anything broader fires on ordinary incidents."""
        body = self._create_map_body(self._main_source())
        assert 'map_data.get("lkp_locality_suspect")' in body, (
            "the create-map gate is no longer armed by the #604 suspect flag"
        )

    def test_gate_is_ackable(self):
        """Hard-confirm, not hard-block — a false positive must not brick the map."""
        body = self._create_map_body(self._main_source())
        assert 'body.get("stale_locality_ack")' in body, (
            "the create-map acknowledge path is gone — the guard became an "
            "unconditional block on map creation"
        )

    def test_gate_returns_422_and_a_distinct_code(self):
        """409 is the double-dispatch code; the two gates must not collide.

        The code must also differ from the dispatch gate's plain
        "stale_locality" so the frontend can branch unambiguously.
        """
        body = self._create_map_body(self._main_source())
        gate = body.find('"code": "stale_locality_map"')
        window = body[max(0, gate - 400):gate + 400]
        assert "status_code=422" in window, "create-map gate no longer returns 422"
        assert "status_code=409" not in window, "create-map gate must not use 409"

    def test_the_two_gates_use_different_codes(self):
        """A shared code would let one message silently retarget the other."""
        src = self._main_source()
        assert '"code": "stale_locality_map"' in src, "create-map gate code missing"
        assert '"code": "stale_locality"' in src, "dispatch gate code missing"

    def test_create_map_gate_does_not_scan_surfaces(self):
        """At map time there is no composed title/body to scan.

        _find_stale_locality is the DISPATCH gate's tool. Using it here would
        require surfaces that do not exist yet and would silently never arm.
        """
        body = self._create_map_body(self._main_source())
        assert "_find_stale_locality(" not in body, (
            "create_map is calling _find_stale_locality — at map time no "
            "notification title or body exists, so the surface scan cannot arm"
        )


class TestOverrideLkpDivergence:
    """Issue #606 — warn when a staging override lands far from the LKP.

    On 2026-07-24 the LKP geocoded to the wrong city. The dispatcher corrected
    staging, but the LKP and residence markers kept the bad coordinates, leaving
    markers ~13 km outside the search area. Another dispatcher texted to say he
    was deleting one himself because it was "a big distractor."

    WARN ONLY. The LKP is a fact from the intake form — where the subject was
    last seen — and re-anchoring it to match a staging pick would corrupt the
    one coordinate the whole search is built around.
    """

    THRESHOLD_M = 3_000

    @staticmethod
    def _divergence(lkp, anchor, threshold_m=3_000):
        """Mirror of the #606 block in main.py::apply_staging_override."""
        if not lkp or anchor[0] is None or anchor[1] is None:
            return None
        try:
            d = _haversine_m(float(lkp["lat"]), float(lkp["lng"]), anchor[0], anchor[1])
        except (TypeError, ValueError):
            return None
        if d <= threshold_m:
            return None
        return {"distance_mi": round(d / 1609.344, 1), "distance_km": round(d / 1000.0, 1)}

    def test_the_2026_07_24_case_is_flagged(self):
        """MEASURED coordinates, not estimates. This fixture is the whole point.

        Both earlier thresholds (10 km, then 6 km) were calibrated from GUESSED
        coordinates and left the guard silent on this exact scenario — proved
        by live testing 2026-07-25. These anchors are the ones the service
        actually used, recovered from the staging-POI query URLs in Cloud
        Logging. Do not replace them with approximations.
        """
        lkp = {"lat": 37.4159771, "lng": -121.896571}     # 447 Great Mall Dr -> Milpitas
        anchor = (37.3957815, -121.9469793)                # 600 Moreland Way, Santa Clara
        d = _haversine_m(lkp["lat"], lkp["lng"], anchor[0], anchor[1])
        assert 4_900 < d < 5_100, (
            f"fixture drifted off the measured 2026-07-24 divergence: {d:.0f} m "
            f"(expected ~4990 m)"
        )
        out = self._divergence(lkp, anchor)
        assert out is not None, (
            "the real 2026-07-24 divergence is NOT flagged — this is the third "
            "time the threshold has been set above the case it exists for"
        )
        assert out["distance_mi"] == 3.1

    def test_normal_staging_is_silent(self):
        """Real staging on this team runs 0.05-0.4 mi from the LKP."""
        lkp = {"lat": 37.4168, "lng": -121.8973}
        anchor = (37.4172, -121.8981)                    # ~0.05 mi away
        assert self._divergence(lkp, anchor) is None

    def test_cross_city_staging_is_silent(self):
        """Staging across a city line is routine and must NOT warn.

        The nearest large lot is often in the next town. A guard that fires on
        that trains the dispatcher to ignore the one warning that matters —
        the same reasoning that set the arming condition in #605.
        """
        lkp = {"lat": 37.3541, "lng": -121.9552}         # Santa Clara
        anchor = (37.3660, -121.9680)                     # next town over, ~1.7 km
        assert self._divergence(lkp, anchor) is None

    def test_mutual_aid_is_not_a_false_positive(self):
        """LKP-relative, not home-county-relative.

        On mutual aid the team stages far from home but close to THAT
        incident's LKP, so the distance stays small. Assert the anchor really
        is far from home first, so this cannot pass for the wrong reason.
        """
        home = (37.3541, -121.9552)                       # Santa Clara
        lkp = {"lat": 34.1008, "lng": -117.8265}          # La Verne — mutual aid
        anchor = (34.1015, -117.8290)                     # staging near that LKP
        assert _haversine_m(home[0], home[1], anchor[0], anchor[1]) > 400_000, (
            "fixture is not actually a mutual-aid distance from home"
        )
        assert self._divergence(lkp, anchor) is None

    def test_wilderness_staging_may_warn_and_that_is_accepted(self):
        """Documents a KNOWN, DELIBERATE false positive — do not "fix" it.

        Joseph D. Grant County Park is ~15 km across; an LKP deep inside with
        staging at the gate measures ~4.4 km, which exceeds the 3 km threshold
        and WILL warn. That overlap was accepted with Bill on 2026-07-25: the
        real 2026-07-24 divergence is 4.99 km, so any threshold that catches it
        also catches a big wilderness callout. There is no number that
        separates them.

        It is affordable only because this WARNS and never blocks — a false
        positive costs a glance at a status line. This test exists so the next
        person to see a wilderness warning recognises it as a decision rather
        than a bug, and does not raise the threshold back above the case the
        guard exists for.
        """
        lkp = {"lat": 37.3350, "lng": -121.7050}
        anchor = (37.3670, -121.7350)
        d = _haversine_m(lkp["lat"], lkp["lng"], anchor[0], anchor[1])
        assert 4_000 < d < 5_000, f"fixture drifted off the wilderness case: {d:.0f} m"
        assert self._divergence(lkp, anchor) is not None, (
            "wilderness no longer warns — if the threshold was raised to silence "
            "it, the 4.99 km real case is silent too; see the constant's comment"
        )

    def test_threshold_boundaries(self):
        lkp = {"lat": 37.0, "lng": -122.0}
        # ~2.7 km north — under
        assert self._divergence(lkp, (37.0245, -122.0)) is None
        # ~3.4 km north — over
        assert self._divergence(lkp, (37.0305, -122.0)) is not None

    def test_cant_check_means_allow(self):
        """Same contract as every sibling guard."""
        anchor = (37.0, -122.0)
        assert self._divergence(None, anchor) is None
        assert self._divergence({}, anchor) is None
        assert self._divergence({"lat": None, "lng": None}, anchor) is None
        assert self._divergence({"lat": "x", "lng": "y"}, anchor) is None
        assert self._divergence({"lat": 37.0, "lng": -122.0}, (None, None)) is None

    def test_PRODUCTION_threshold_catches_the_real_case(self):
        """Reads the PRODUCTION constant — the pin every other test here lacks.

        Every other test in this class calls _divergence() with the MIRROR's
        own threshold_m default, so they pass no matter what main.py is set to.
        That is exactly how the threshold shipped wrong twice (10 km, then
        6 km) without a single test failing: the suite was green while the
        guard was inert on the only case it exists for.

        This asserts the real, MEASURED 2026-07-24 divergence exceeds whatever
        production currently uses. If someone raises the constant above ~4.99 km
        again, this goes red.
        """
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        m = re.search(r"_OVERRIDE_LKP_DIVERGENCE_M = ([\d_]+)", src)
        assert m, "_OVERRIDE_LKP_DIVERGENCE_M not found in main.py"
        prod_threshold = int(m.group(1).replace("_", ""))
        measured = _haversine_m(37.4159771, -121.896571, 37.3957815, -121.9469793)
        assert prod_threshold < measured, (
            f"production threshold is {prod_threshold} m but the real "
            f"2026-07-24 divergence is only {measured:.0f} m — the guard is "
            f"INERT on the incident it was built for. This has happened twice; "
            f"read the comment on the constant before changing it."
        )

    def test_stays_below_the_618_reject_threshold(self):
        """The two distance guards must never disagree.

        _staging_geocode_implausible REJECTS a staging geocode beyond 50 km.
        This one only WARNS, so its threshold has to sit below that — otherwise
        there would be distances this guard warns about that the other has
        already thrown away, and the warning would be unreachable.
        """
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        warn = int(re.search(r"_OVERRIDE_LKP_DIVERGENCE_M = ([\d_]+)", src)
                   .group(1).replace("_", ""))
        reject = int(re.search(r"_MAX_STAGING_DIST_M = ([\d_]+)", src)
                     .group(1).replace("_", ""))
        assert warn < reject, (
            f"the #606 warn threshold ({warn} m) is not below the #618 reject "
            f"threshold ({reject} m) — the warning is unreachable"
        )


class TestOverrideDivergenceWiring:
    """Pin the wiring. A correct helper that nothing calls is no fix at all."""

    @staticmethod
    def _handler_body():
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        start = src.find("async def apply_staging_override(")
        end = src.find("\n@app.", start + 1)
        assert start != -1 and end > start
        return src[start:end]

    def test_lkp_is_read_from_the_body(self):
        assert 'lkp_in   = body.get("lkp")' in self._handler_body(), (
            "the optional lkp context is no longer read — the divergence check "
            "can never arm"
        )

    def test_divergence_is_computed_and_returned(self):
        body = self._handler_body()
        assert "_OVERRIDE_LKP_DIVERGENCE_M" in body, "threshold no longer referenced"
        assert '"lkp_divergence": lkp_divergence' in body, (
            "the divergence is no longer returned — the dispatcher never sees it"
        )

    def test_it_warns_and_never_blocks(self):
        """WARN ONLY, per Bill 2026-07-25.

        A 4xx here would strand the dispatcher: the override is exactly the
        tool the warning tells them to use again, and the results list they
        need to pick from arrives on this same response.
        """
        body = self._handler_body()
        start = body.find("lkp_divergence: dict | None = None")
        end = body.find("_outcome = ", start)
        assert start != -1 and end > start
        window = body[start:end]
        assert "HTTPException" not in window, (
            "the divergence check now raises — it must warn on a normal 200 so "
            "the dispatcher can immediately pick another location"
        )

    def test_lkp_is_not_a_fourth_input_mode(self):
        """It is context, not a coordinate input.

        If it were folded into the 'exactly one of' rule, every override that
        sent the LKP would 400.
        """
        body = self._handler_body()
        provided = body.find("provided = sum(1 for x in (address, lat_lng, utm_in)")
        assert provided != -1, (
            "the exactly-one-of check changed shape — confirm lkp was not added "
            "to it; doing so 400s every override that supplies LKP context"
        )

    def test_lkp_coords_are_never_logged(self):
        """Locked Decision: this endpoint logs no PII. LKP coords are PII."""
        body = self._handler_body()
        calls = []
        for kw in ("logger.info(", "logger.warning(", "logger.error("):
            i = body.find(kw)
            while i != -1:
                j = i + len(kw) - 1
                depth, k = 0, j
                while k < len(body):
                    if body[k] == "(":
                        depth += 1
                    elif body[k] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                calls.append(body[j:k + 1])
                i = body.find(kw, k)
        for call in calls:
            for forbidden in ("lkp_in", "_lkp_lat", "_lkp_lng", "lkp_divergence", "_dist_m"):
                assert forbidden not in call, (
                    f"apply-staging-override logs {forbidden!r} — LKP coordinates are "
                    f"PII and this endpoint's log is contractually PII-free"
                )


class TestMapGateRemedyIsAchievable:
    """Live-test correction 2026-07-25 — the map gate must not offer a remedy
    that cannot clear it.

    The gate is armed by `lkp_locality_suspect`, set once by /ocr. A staging
    override mutates only `_rawMapData.staging` and can NEVER change that flag,
    so no override in any city dismisses this modal. The earlier copy listed
    "Apply Override" under "Fix it:" — Bill applied overrides in two different
    cities, got the same modal both times, and asked which city he was supposed
    to pick. The answer was: none of them.

    A remedy that cannot resolve the condition it is attached to is worse than
    no remedy — it spends the scarcest thing a dispatcher has.
    """

    @staticmethod
    def _map_gate_message():
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        start = src.find('"code": "stale_locality_map"')
        assert start != -1, "create-map gate not found"
        end = src.find("},\n            )", start)
        assert end != -1 and end > start, "could not bound the gate detail dict"
        return src[start:end]

    def test_override_is_not_offered_as_a_fix(self):
        msg = self._map_gate_message()
        assert "will NOT clear this warning" in msg, (
            "the map gate no longer states that Apply Override cannot clear it — "
            "a dispatcher will override repeatedly and keep getting the same modal"
        )

    def test_the_only_real_remedy_is_named(self):
        msg = self._map_gate_message()
        assert "Only a corrected form clears this" in msg, (
            "the map gate no longer names re-uploading a corrected form as the "
            "only thing that clears it"
        )
        assert "resubmit it" in msg and "Dispatch Turbo" in msg, (
            "the remedy must say resubmit to Dispatch Turbo — 'correct the form' "
            "alone reads as though the form can be edited in-app"
        )

    def test_dispatch_gate_still_offers_override_because_there_it_works(self):
        """The asymmetry is real, not an oversight.

        The DISPATCH gate scans surfaces, so changing staging removes the stale
        city from the staging_address surface and genuinely clears it. The MAP
        gate keys on the suspect flag, which no override touches. Both messages
        must stay correct for their own gate.
        """
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        start = src.find('"code": "stale_locality"')
        end = src.find("},\n                )", start)
        dispatch_msg = src[start:end]
        assert "Apply Override" in dispatch_msg, (
            "the dispatch gate dropped the override remedy — there it DOES clear "
            "the condition, and removing it would send the dispatcher to the "
            "slower fix for no reason"
        )


# ===========================================================================
# #667 — the California anchor is a request parameter, not only a suffix
# ===========================================================================
# `_geocode_nominatim` sent `, CA` as a string hint and nothing else. `CA` is
# both the USPS code for California and the ISO 3166-1 alpha-2 code for CANADA,
# so a query with no other signal resolves the country. Empirically, against
# live Nominatim on 2026-07-31:
#
#   q="Treatment Facility, CA"                  -> 44.716, -63.673  Nova Scotia, CANADA
#   q="Treatment Facility, CA" countrycodes=us  -> 37.404, -120.732 California, US
#   q="10410, CA"                               ->  -6.178, 106.838 Jakarta, INDONESIA
#   q="10410, CA"              countrycodes=us  -> 32.750, -117.209 California, US
#
# The first pair is the 2026-07-31 Humboldt mutual-aid callout: a landmark-only
# LKP put the incident ~4,900 km outside the search area, and the staging POI
# circle, the CalTopo seed and all 9 markers, the D4H location, and the
# responder-facing Slack staging link all inherited it. `_house_number_consistent`
# cannot fire on a landmark LKP, so no guard caught it — a human did.
#
# The second pair is the SAME bug, already known: the bare-house-number guard in
# main.py carries a comment about "10410, CA" resolving to Jakarta, and works
# around it by substituting the Residence address. That is one input shape
# patched; the parameter fixes the class.
#
# `us`, NOT California, per Bill 2026-07-31 — out-of-state mutual aid is rare
# but real, and a state assertion would reject exactly the callout class that
# surfaced this. Verified in the same spike: "1 Fremont St, Las Vegas, NV"
# still resolves with countrycodes=us.

class TestNominatimCountryBinding:
    """Source-scan pins. `_geocode_nominatim` is async and does live HTTP, so
    it is not mirrored in this file; production text is the contract."""

    @staticmethod
    def _geocode_fn_source() -> str:
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        start = src.find("async def _geocode_nominatim(")
        assert start != -1, "_geocode_nominatim not found in main.py"
        # Bound on the next top-level def, not a char count — a window that
        # ends early stops describing the thing it names.
        end = src.find("\n# `@` added 2026-05-08", start)
        assert end != -1 and end > start, "could not bound _geocode_nominatim"
        return src[start:end]

    def test_request_is_country_bound(self):
        body = self._geocode_fn_source()
        assert '"countrycodes": "us"' in body, (
            "_geocode_nominatim no longer binds the country — the ', CA' "
            "suffix alone is advisory, and 'CA' is the ISO code for Canada. "
            "A landmark-only LKP resolves out of the country again (#667)"
        )

    def test_binding_is_country_not_state(self):
        """A state-level constraint would reject out-of-state mutual aid —
        rare but real, and it is the callout class that surfaced this bug."""
        body = self._geocode_fn_source()
        assert '"countrycodes": "ca"' not in body.lower().replace('"us"', ""), \
            "countrycodes must be 'us'"
        for state_filter in ("state=California", '"state": "California"',
                             "administrative_area"):
            assert state_filter not in body, (
                f"{state_filter!r} added to _geocode_nominatim — the binding is "
                "deliberately US-wide, NOT California. Out-of-state mutual aid "
                "is rare but real (Bill, 2026-07-31)"
            )

    def test_ca_suffix_is_retained_and_documented_as_advisory(self):
        """Both halves are needed: the parameter fixes the country, the suffix
        still disambiguates within the US (Spanish street names)."""
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        # Scope to the LKP-path append, not the file. main.py has a SECOND
        # CA-append in _normalize_staging_geocode_query, and an unscoped grep
        # matched that one instead — this pin passed with the LKP append
        # deleted until mutation testing caught it.
        start = src.find("# DESIGN DECISION: SCCSSAR operates entirely within")
        assert start != -1, "the CA-append design-decision comment is gone"
        end = src.find('logger.info("State absent', start)
        assert end != -1 and end > start, "could not bound the CA-append block"
        comment = src[start:end]
        assert 'geocode_query = f"{geocode_query}, CA"' in comment, (
            "the ', CA' append was removed from the LKP geocode path — "
            "countrycodes=us does not disambiguate San Felipe / Santa Teresa "
            "WITHIN the US"
        )
        assert "countrycodes=us" in comment, (
            "the CA-append comment no longer names the binding half — a future "
            "reader sees a mechanism documented as sufficient when it is not"
        )
        assert "Canada" in comment, (
            "the comment no longer records WHY the suffix is insufficient; "
            "without the Canada ambiguity it reads as belt-and-braces"
        )


class TestGoogleDeliberatelyNotCountryBound:
    """The asymmetry between the two geocoders is a decision, and an
    undefended decision is one a future "consistency" PR reverts.

    Measured against live Google 2026-07-31: `components=country:US` fixes
    nothing (Google never left the US on any probe — `, CA` is sufficient
    there) and costs signal. On `"Treatment Facility, CA"` it turns an
    APPROXIMATE state centroid into a ROOFTOP address 300 km away; on
    `"Community Center, CA"` it flips `partial_match` from True to False.
    """

    @staticmethod
    def _google_fn_source() -> str:
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        start = src.find("async def _geocode_google_maps(")
        assert start != -1, "_geocode_google_maps not found in main.py"
        end = src.find("\ndef _street_correction_note(", start)
        assert end != -1 and end > start, "could not bound _geocode_google_maps"
        return src[start:end]

    def test_no_components_country_filter(self):
        body = self._google_fn_source()
        # Match the params-dict KEY, not the bare word — the docstring names
        # `components=country:US` when explaining why it is absent.
        assert '"components"' not in body, (
            "a country/components filter was added to _geocode_google_maps. "
            "Re-run the spike before doing this: it fixes nothing Google was "
            "doing wrong and it flips partial_match True->False on landmark "
            "queries, hiding the one confidence signal Google gives us (#667)"
        )

    def test_asymmetry_is_documented(self):
        """Without the rationale in the docstring, the next reader sees one
        geocoder bound and one not, and closes the gap."""
        body = self._google_fn_source()
        assert "DELIBERATELY NOT country-bound" in body, (
            "the docstring no longer records that the missing country filter "
            "is a decision — a future PR adds it for symmetry with "
            "_geocode_nominatim and silently degrades geocode diagnostics"
        )
        assert "partial_match" in body, (
            "the docstring no longer names partial_match as the signal the "
            "filter would destroy — that is the whole reason for the asymmetry"
        )


# ---------------------------------------------------------------------------
# _apply_street_correction_to_summary — idempotency (live regression 2026-08-01)
# ---------------------------------------------------------------------------

def _apply_street_correction_to_summary(
    summary: str, input_first_component: str, corrected_first_component: str
) -> str:
    """Mirror of main.py::_apply_street_correction_to_summary().

    Parity with production is pinned by
    TestStreetCorrectionIdempotency.test_production_anchors_the_pattern.
    """
    misspelled_street = re.sub(r"^\d+\s+", "", input_first_component).strip()
    # Strip the corrected side's house number only when the input had one —
    # otherwise a park-to-address correction loses the number it just gained.
    if re.match(r"^\d+\b", input_first_component.strip()):
        corrected_street = re.sub(r"^\d+\s+", "", corrected_first_component).strip()
    else:
        corrected_street = corrected_first_component.strip()
    if not misspelled_street or not corrected_street:
        return summary

    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    if _norm(misspelled_street) == _norm(corrected_street):
        return summary
    misspelled_base = re.sub(r"[.\s]+$", "", misspelled_street)
    lower_base, lower_corrected = misspelled_base.lower(), corrected_street.lower()
    guard = ""
    trailer = ""
    if lower_corrected.endswith(lower_base) and lower_corrected != lower_base:
        guard = f"(?<!{re.escape(corrected_street[:-len(misspelled_base)])})"
    elif lower_corrected.startswith(lower_base):
        trailer = f"(?!{re.escape(corrected_street[len(misspelled_base):])})"
    pattern = re.compile(guard + re.escape(misspelled_base) + r"\.?" + trailer, re.IGNORECASE)
    return pattern.sub(corrected_street, summary)


class TestStreetCorrectionIdempotency:
    """LIVE REGRESSION, personal-dev 2026-08-01 (Humboldt form).

    The dispatcher's staging list showed "1100 N N Winton Way" and
    "1000 N N Winton Way". Two corrections were queued during geocoding —
    ("1100 Winton Way" -> "1100 N Winton Way") and the 1300 equivalent — but
    both reduce to the SAME street-level replacement, "Winton Way" ->
    "N Winton Way". The first pass fixes both entries globally; the second pass
    then re-matches INSIDE its own output and double-prefixes.

    The function's docstring claimed the replacement was idempotent. That only
    holds when the corrected form does not CONTAIN the misspelled form — and
    the most common correction Google makes is adding a cardinal or a street
    type, which always contains it.

    Not cosmetic: by the Locked Decision "Staging line text IS the responders'
    maps-link query", that string is what a responder's phone searches for.
    """

    REAL = ("5. 1100 Winton Way, Livingston, CA — Best Western.\n"
            "6. 1000 Winton Way, Livingston, CA — Shell.")

    def test_the_2026_08_01_double_prefix_does_not_recur(self):
        out = self.REAL
        for a, b in (("1100 Winton Way", "1100 N Winton Way"),
                     ("1000 Winton Way", "1000 N Winton Way")):
            out = _apply_street_correction_to_summary(out, a, b)
        assert "N N" not in out, f"double-prefixed again: {out!r}"
        assert "1100 N Winton Way" in out
        assert "1000 N Winton Way" in out

    def test_one_correction_fixes_every_occurrence(self):
        """Why deduping alone would be enough for THIS case but not in general:
        the replacement is global, so one pass already fixes both entries."""
        out = _apply_street_correction_to_summary(
            self.REAL, "1100 Winton Way", "1100 N Winton Way")
        assert out.count("N Winton Way") == 2

    def test_prefix_addition_is_idempotent(self):
        once = _apply_street_correction_to_summary(
            "1100 Winton Way", "1100 Winton Way", "1100 N Winton Way")
        twice = _apply_street_correction_to_summary(
            once, "1100 Winton Way", "1100 N Winton Way")
        assert once == twice == "1100 N Winton Way"

    def test_suffix_addition_is_idempotent(self):
        """The other affix shape — Google expanding an abbreviated type."""
        once = _apply_street_correction_to_summary(
            "1100 TRADEN DR.", "1100 TRADEN DR.", "1100 TRADEN DRIVE")
        twice = _apply_street_correction_to_summary(
            once, "1100 TRADEN DR.", "1100 TRADEN DRIVE")
        assert once == twice == "1100 TRADEN DRIVE"

    def test_a_genuine_misspelling_is_still_corrected(self):
        """The guard must not disable the feature it protects."""
        out = _apply_street_correction_to_summary(
            "1100 TRADEN DR", "1100 TRADEN DR", "1100 Tradan Dr")
        assert out == "1100 Tradan Dr"

    def test_unrelated_street_untouched(self):
        out = _apply_street_correction_to_summary(
            "1100 Winton Way and 500 Main St", "1100 Winton Way", "1100 N Winton Way")
        assert "500 Main St" in out

    # --- production parity ------------------------------------------------

    def test_production_anchors_the_pattern(self):
        """Assert the anchors exist INSIDE the function, comments stripped.

        The rationale above the code names lookbehind/lookahead and the exact
        strings, so an unscoped search finds the explanation, not the code.
        """
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        fn = re.search(
            r"^def _apply_street_correction_to_summary\(.*?"
            r"\n    return pattern\.sub\(corrected_street, summary\)",
            src, re.DOTALL | re.MULTILINE,
        )
        assert fn, "_apply_street_correction_to_summary not found in main.py"
        code = "\n".join(l.split("#")[0] for l in re.sub(
            r'""".*?"""', "", fn.group(0), flags=re.DOTALL).splitlines())
        assert "(?<!" in code, (
            "The negative lookbehind is gone — a prefix correction "
            "(\"Winton Way\" -> \"N Winton Way\") re-matches its own output "
            "and produces \"N N Winton Way\" again."
        )
        assert "(?!" in code, (
            "The negative lookahead is gone — a suffix correction "
            "(\"DR.\" -> \"DRIVE\") re-matches its own output."
        )
        assert "guard + re.escape(misspelled_base)" in code, (
            "The anchors are computed but not applied to the pattern."
        )


# ---------------------------------------------------------------------------
# _correction_is_plausible_street — reject a "correction" that is not a street
# Live regression 2026-08-01 (Humboldt run 2)
# ---------------------------------------------------------------------------

_HOUSE_NUMBER_PREFIX_RE = re.compile(r"^\d+\b")

# Mirror of main.py::_STREET_TYPES, trimmed to what this guard needs. Full-table
# parity is not asserted here; presence-detection behaviour is.
_MIRROR_STREET_TYPES = re.compile(
    r"\s+(?:Blvd|Blvd\.|Boulevard|Dr|Dr\.|Drive|Ave|Ave\.|Avenue|St|St\.|Street|"
    r"Rd|Rd\.|Road|Ln|Ln\.|Lane|Way|Ct|Ct\.|Court|Pl|Pl\.|Place|"
    r"Pkwy|Pkwy\.|Parkway|Hwy|Hwy\.|Highway|Expy|Expressway)(?:\s.*)?$",
    re.IGNORECASE,
)


def _correction_is_plausible_street(input_first: str, corrected_first: str) -> bool:
    """Mirror of main.py::_correction_is_plausible_street()."""
    in_house = bool(_HOUSE_NUMBER_PREFIX_RE.match(input_first))
    out_house = bool(_HOUSE_NUMBER_PREFIX_RE.match(corrected_first))
    if in_house and not out_house:
        return False
    in_type = bool(_MIRROR_STREET_TYPES.search(input_first))
    out_type = bool(_MIRROR_STREET_TYPES.search(corrected_first))
    if in_type and not out_type and not out_house:
        return False
    return True


class TestCorrectionPlausibility:
    """LIVE REGRESSION, personal-dev 2026-08-01 (Humboldt run 2).

        Staging address corrected: "1000 E Childs Ave" -> "Livingston"

    Google returned a formatted_address whose first component was the bare
    CITY. `_street_correction_note` had no validity check at all, so the street
    was replaced by the city name throughout the summary and the dispatcher's
    staging list read "1440 Livingston, Livingston, CA".

    Worse than a wrong spelling on two counts: a house number followed by a
    city is not navigable, and it CORRUPTED AN ADDRESS THAT WAS ALREADY
    CORRECT. The split was visible on the map — the CalTopo marker, built
    before the correction runs, still read "1100 E Childs Ave" while the
    textarea read "1440 Livingston".

    By the Locked Decision "Staging line text IS the responders' maps-link
    query", the corrupted string is exactly what a responder's phone searches.
    """

    def test_the_2026_08_01_case_is_rejected(self):
        assert not _correction_is_plausible_street("1000 E Childs Ave", "Livingston")

    def test_street_only_to_bare_city_is_rejected(self):
        """Isolates the STREET-TYPE rule: no house number on either side, so
        the house-number rule cannot fire."""
        assert not _correction_is_plausible_street("Winton Way", "Livingston")

    def test_house_number_loss_alone_is_rejected(self):
        """Isolates the HOUSE-NUMBER rule.

        "Broadway" carries no street-type token, so the street-type rule cannot
        fire and only the house-number rule can catch this. The 2026-08-01 case
        is caught by BOTH rules, so it cannot pin either one on its own —
        mutation testing found exactly that gap.
        """
        assert not _correction_is_plausible_street("1400 Broadway", "Livingston")

    def test_genuine_misspelling_still_accepted(self):
        """The guard must not disable the feature it protects (Form-9 case)."""
        assert _correction_is_plausible_street("1100 TRADEN DR", "1100 Tradan Dr")

    def test_cardinal_addition_still_accepted(self):
        assert _correction_is_plausible_street("1100 Winton Way", "1100 N Winton Way")

    def test_type_expansion_still_accepted(self):
        assert _correction_is_plausible_street("1100 TRADEN DR.", "1100 TRADEN DRIVE")

    def test_park_to_real_address_still_accepted(self):
        """Parks legitimately have no house number on the way in — the observed
        "Livingston Community Park" -> "600 B St" correction must survive."""
        assert _correction_is_plausible_street("Livingston Community Park", "600 B St")

    def test_house_number_alone_justifies_a_missing_street_type(self):
        """The `not out_house` escape in the street-type rule, isolated.

        "600 Broadway" is a real address with NO street-type token, so a guard
        that demanded one would reject it. "600 B St" cannot pin this — its
        trailing " St" satisfies the type check either way.
        """
        assert _correction_is_plausible_street("Cardoza Park", "600 Broadway")

    def test_a_street_named_after_a_city_is_not_rejected(self):
        """The guard keys on STREET-NESS, not on the word looking like a city."""
        assert _correction_is_plausible_street("1000 E Childs Ave", "1000 Livingston Ave")

    # --- production parity ------------------------------------------------

    @staticmethod
    def _main_src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    def test_production_note_builder_consults_the_guard(self):
        """Assert the CALL inside _street_correction_note, comments stripped.

        The guard existing but never being consulted is the shape that would
        re-ship the bug while every unit test above still passes.
        """
        src = self._main_src()
        fn = re.search(
            r"^def _street_correction_note\(.*?(?=\n\n(?:def |# -{10,}))",
            src, re.DOTALL | re.MULTILINE,
        )
        assert fn, "_street_correction_note not found in main.py"
        code = "\n".join(l.split("#")[0] for l in re.sub(
            r'""".*?"""', "", fn.group(0), flags=re.DOTALL).splitlines())
        assert "_correction_is_plausible_street(input_first, corrected_first)" in code, (
            "_street_correction_note no longer validates the correction — "
            "Google returning a bare city replaces the street throughout the "
            "summary again."
        )
        assert code.index("_correction_is_plausible_street") < code.index("return corrected_first"), (
            "The guard runs after the correction is returned."
        )

    def test_production_rejection_log_carries_no_address_text(self):
        """Core privacy guarantee #3 — location is PII.

        The rejection is worth logging, but only its SHAPE.
        """
        src = self._main_src()
        fn = re.search(
            r"^def _street_correction_note\(.*?(?=\n\n(?:def |# -{10,}))",
            src, re.DOTALL | re.MULTILINE,
        )
        code = "\n".join(l.split("#")[0] for l in re.sub(
            r'""".*?"""', "", fn.group(0), flags=re.DOTALL).splitlines())
        warn = code[code.index("logger.warning"):code.index("return None", code.index("logger.warning"))]
        for leak in ("input_first,", "corrected_first,", "input_addr", "gm_formatted"):
            assert leak not in warn, (
                f"the rejection log interpolates {leak!r} — that is address "
                f"text reaching Cloud Run logs"
            )


class TestRejectedCorrectionReachesTheDispatcher:
    """A rejected correction must be VISIBLE, not just logged (Bill, 2026-08-01).

    Rejecting the correction means we keep the officer's original text. Without
    a dispatcher-facing note the only trace is a server log nobody reads — and
    a city-only answer from Google is itself a signal the address may not
    exist. The dispatcher is the one who can ring the officer and settle it.

    Event Log entries are dispatcher-facing SUMMARY content and already quote
    addresses (e.g. `Staging address corrected: "1100 Winton Way" → ...`).
    The "no PII" rule governs `logger.*` calls; it does not apply here.
    """

    @staticmethod
    def _src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @classmethod
    def _fn(cls, name):
        m = re.search(
            rf"^def {name}\(.*?(?=\n\n(?:def |# -{{10,}}|[A-Z_]+ = ))",
            cls._src(), re.DOTALL | re.MULTILINE,
        )
        assert m, f"{name} not found in main.py"
        return m.group(0)

    @staticmethod
    def _code_only(text):
        body = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    def test_helper_returns_a_rejection_note(self):
        """Bound to the RETURN, not merely present in the function.

        A mutation that moved the note text into a dead local variable and
        returned `None, None` passed a plain `in code` assertion — the string
        was still there, just never returned. Slice from the return statement.
        """
        code = self._code_only(self._fn("_street_correction_note"))
        assert "return None, (" in code, (
            "The rejection no longer RETURNS a dispatcher-facing note — the "
            "only trace of a kept-but-unverified address is a server log."
        )
        returned = code[code.index("return None, ("):]
        assert "could not be verified" in returned, (
            "The returned note no longer says the address could not be verified."
        )
        assert "confirm it with the officer" in returned, (
            "The returned note lost its actionable instruction."
        )

    def test_helper_returns_a_pair_on_every_path(self):
        """All three exits must return a 2-tuple or a caller unpacks a string."""
        code = self._code_only(self._fn("_street_correction_note"))
        assert "return None, None" in code, "the no-change path lost its pair"
        assert "return corrected_first, None" in code, "the accepted path lost its pair"
        assert "return None, (" in code, "the rejected path lost its pair"

    def test_every_call_site_surfaces_the_rejection(self):
        """Three call sites — LKP, Residence, staging Pass B.

        A site that unpacks the pair but drops the note is the shape that would
        pass every unit test above while the dispatcher still sees nothing.
        """
        src = self._src()
        code = "\n".join(l.split("#")[0] for l in src.splitlines())
        assert code.count("_street_correction_note(") >= 3, (
            "a call site disappeared; this pin no longer covers what it claims"
        )
        for var in ("_rejected", "_res_rejected", "_pb_rejected"):
            assert f"if {var}" in code and \
                   f"event_log_additions.append({var})" in code, (
                f"{var} is captured but never appended to the Event Log — the "
                f"rejection is invisible to the dispatcher at that call site."
            )

    def test_labels_are_distinct_per_call_site(self):
        """The dispatcher needs to know WHICH address could not be verified."""
        code = "\n".join(l.split("#")[0] for l in self._src().splitlines())
        for label in ('"LKP address"', '"Residence address"',
                      '"Officer staging address"', '"Staging address"'):
            assert label in code, f"the {label} label is gone from the call sites"


class TestCorrectionPreservesAnAddedHouseNumber:
    """LIVE REGRESSION, personal-dev 2026-08-01 (original Humboldt form).

        Staging address corrected: "Livingston Community Park" -> "600 B St"

    rendered as "B St, Livingston, CA" in the dispatcher's staging list. The
    house number was discarded.

    _apply_street_correction_to_summary strips the leading house number from
    BOTH sides. That is correct when the input has one — the replacement is
    street-name-only so the officer's number survives in the text. It is wrong
    when the input has NONE and Google's answer ADDS one: the number is thrown
    away and a street-only string remains.

    Park-to-address is the common shape: parks arrive without a house number by
    construction, and "B St, Livingston" is unnavigable — by the Locked Decision
    it is what a responder's phone searches for.
    """

    def test_added_house_number_survives(self):
        out = _apply_street_correction_to_summary(
            "4. Livingston Community Park, Livingston, CA — City park",
            "Livingston Community Park", "600 B St")
        assert "600 B St" in out, f"house number lost: {out!r}"

    def test_park_to_numbered_street_without_a_type_token(self):
        out = _apply_street_correction_to_summary(
            "Cardoza Park, Milpitas", "Cardoza Park", "600 Broadway")
        assert "600 Broadway" in out

    def test_existing_house_number_is_still_not_duplicated(self):
        """The reason the strip exists at all — must not regress.

        Both sides have a number, so the replacement stays street-only and the
        original number is not doubled.
        """
        out = _apply_street_correction_to_summary(
            "1100 TRADEN DR, San Jose", "1100 TRADEN DR", "1100 Tradan Dr")
        assert out == "1100 Tradan Dr, San Jose"
        assert "1950 1950" not in out

    def test_cardinal_addition_unaffected(self):
        out = _apply_street_correction_to_summary(
            "5. 1100 Winton Way, Livingston", "1100 Winton Way", "1100 N Winton Way")
        assert "1100 N Winton Way" in out
        assert "N N" not in out

    def test_production_strips_the_corrected_side_conditionally(self):
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        fn = re.search(
            r"^def _apply_street_correction_to_summary\(.*?"
            r"\n    return pattern\.sub\(corrected_street, summary\)",
            src, re.DOTALL | re.MULTILINE,
        )
        assert fn, "_apply_street_correction_to_summary not found in main.py"
        code = "\n".join(l.split("#")[0] for l in re.sub(
            r'""".*?"""', "", fn.group(0), flags=re.DOTALL).splitlines())
        assert "_HOUSE_NUMBER_PREFIX_RE.match(input_first_component" in code, (
            "The corrected side's house number is stripped unconditionally "
            "again — a park-to-address correction loses its number and the "
            "staging line becomes unnavigable."
        )


class TestPassBStagingCityGuard:
    """Pass B staging geocodes must run the SAME city guard as LKP/Residence (#736).

    `_reconcile_geocode_city` had exactly two callers — LKP and Residence — so
    the identical address string, geocoded twice by the same provider in one
    request, was city-checked on one path and accepted unchecked on the other.

    Live 2026-08-09 on personal-dev: an officer-designated staging address that
    was also the LKP address. Nominatim answered the wrong city on all three
    legs; LKP and Residence were corrected via Google, the staging leg was not,
    and the CP marker landed ~10 km away in a different city on a map whose LKP
    pin was correct.

    No existing guard could catch it — `_staging_geocode_implausible` runs with
    `_MAX_STAGING_DIST_M = 50_000`, so a 10 km error passes comfortably. Same
    shape as the #646 Richey Center case.
    """

    @staticmethod
    def _main_source():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @classmethod
    def _passb_block(cls):
        """The Pass B result loop, bounded at BOTH ends on real markers."""
        src = cls._main_source()
        m = re.search(
            r"for i, \(\(entry_idx, addr, pg_is_officer\), nom_result\) in enumerate\(.*?"
            r"(?=\n            # Officer entry last-resort|\n        # ---)",
            src, re.DOTALL,
        )
        assert m, "the Pass B result loop was not found in main.py"
        return m.group(0)

    @staticmethod
    def _code_only(text):
        return "\n".join(l.split("#")[0] for l in text.splitlines())

    def test_guard_runs_on_the_nominatim_success_path(self):
        code = self._code_only(self._passb_block())
        assert "_reconcile_geocode_city(" in code, (
            "Pass B staging no longer runs the city-consistency guard — the "
            "same address can again resolve to a different city than the LKP"
        )
        assert "addr, nom_result, _pb_city_label" in code, (
            "the guard must be called with the NORMALIZED query and the "
            "Nominatim result, or it compares the wrong things"
        )
        assert "nom_result, _pb_city_note, _ = await _reconcile_geocode_city(" in code, (
            "the guard's CORRECTED geo is not assigned back to nom_result — it "
            "runs, emits its note, and the wrong-city coordinate is stored "
            "anyway. Failure-mode Discipline Q3: a return value that is "
            "discarded is a guard that does nothing."
        )

    def test_guard_precedes_the_coordinate_assignment(self):
        """Structure, not keyword presence. A guard that runs after the entry's
        lat/lng are set is a no-op that still contains every pinned string.

        Scoped to the Nominatim ACCEPT branch. Since #773 the rejection branch
        above it also assigns coordinates (an officer-written address beats the
        distance guard), so an unscoped `index()` measures against THAT
        assignment and reports the guard as correctly-ordered no matter where
        the accept branch puts it.
        """
        code = self._code_only(self._passb_block())
        accept = code.index("elif nom_result:")
        branch = code[accept:]
        assert branch.index("_reconcile_geocode_city(") < branch.index(
            'staging_entries[entry_idx]["lat"]'
        ), "the city guard now runs after the coordinate is stored"

    def test_note_reaches_the_dispatcher(self):
        code = self._code_only(self._passb_block())
        assert "_pb_city_note" in code and "event_log_additions.append(_pb_city_note)" in code, (
            "the city-mismatch note is no longer surfaced — the dispatcher gets "
            "a silently relocated CP marker"
        )

    def test_label_distinguishes_officer_from_alternate(self):
        """Scoped to the _pb_city_label ASSIGNMENT.

        Both label strings and `pg_is_officer` also appear elsewhere in this
        block — the Google-fallback branch builds its own `_pb_label` from the
        same two strings — so an unscoped presence check passes even with the
        conditional collapsed to a constant.
        """
        code = self._code_only(self._passb_block())
        m = re.search(r"_pb_city_label\s*=\s*(.*?)\n\s*nom_result", code, re.DOTALL)
        assert m, "the _pb_city_label assignment is gone"
        assign = m.group(1)
        assert "pg_is_officer" in assign, (
            "the guard label no longer varies by officer/alternate, so the "
            "Event Log cannot say whose address was re-geocoded"
        )
        assert "Officer staging address" in assign and "Staging address" in assign

    def test_guard_is_not_applied_to_the_google_fallback(self):
        """The guard's move is 'Nominatim named the wrong city, ask Google'. An
        answer that already came from Google has nowhere to escalate, and its
        tuple carries a formatted address at index 3, not a city — passing it
        would compare an address against a city name."""
        code = self._code_only(self._passb_block())
        gm_branch = code[code.index("elif gm_tuple:"):]
        assert "_reconcile_geocode_city(" not in gm_branch, (
            "the city guard was extended to the Google fallback; it re-queries "
            "Google against itself and mis-reads gm_formatted as a city"
        )

    def test_every_geocode_caller_is_guarded(self):
        """Enumerate the CALLERS, do not assert a bare number (#740).

        The first version of this pin asserted "exactly 3 guarded legs" — a
        number taken from the three legs that PR had touched, without ever
        enumerating who calls the geocoder. `/apply-staging-override` was a
        fourth caller with the same exposure, so the pin did not catch the gap;
        it RATIFIED it, and stayed green while a dispatcher typing the correct
        city was sent to the wrong one.

        A count pin encodes a claim about completeness. This one derives that
        claim instead: every `_geocode_lkp_smart` call site must be paired with
        a reconciliation, so a new geocoding path fails here until it either
        runs the guard or is added to the documented exemptions below.
        """
        src = self._main_source()
        callers = [
            m.start() for m in re.finditer(r"_geocode_lkp_smart\(", src)
            if not src[:m.start()].rstrip().endswith("async def")
        ]
        guards = [m.start() for m in re.finditer(r"await _reconcile_geocode_city\(", src)]
        assert len(callers) == 3, (
            f"expected 3 _geocode_lkp_smart call sites (LKP, Pass B staging, "
            f"/apply-staging-override), found {len(callers)}. A new geocoding "
            f"path must run _reconcile_geocode_city or document why it cannot."
        )
        # Residence has its own guarded leg without going through
        # _geocode_lkp_smart, so guards legitimately outnumber these callers.
        assert len(guards) == 4, (
            f"expected 4 reconciliation call sites (LKP, Residence, Pass B "
            f"staging, /apply-staging-override), found {len(guards)}"
        )

    def test_the_override_endpoint_is_guarded(self):
        """The #740 leg specifically, asserted by its own call, not by a count."""
        src = self._main_source()
        assert (
            'result, _ov_city_note, _ = await _reconcile_geocode_city(' in src
        ), (
            "the guard's CORRECTED geo is not assigned back to `result` — it "
            "runs, returns a note, and the wrong-city anchor is used anyway. "
            "Same leak as #739's M2; Failure-mode Discipline Q3."
        )
        assert 'normalized, result, "Staging override address"' in src, (
            "/apply-staging-override no longer reconciles the city — a "
            "dispatcher who types the correct city is told the system "
            "disagrees, and is offered no remedy"
        )

    def test_override_guard_precedes_the_anchor_unpack(self):
        """Ordering. `resolved_locality` and the divergence distance are both
        derived from the unpacked anchor, so a guard that runs after it warns
        the dispatcher about a coordinate it already corrected — or fails to."""
        src = self._main_source()
        blk = src[src.index("result = await _geocode_lkp_smart(normalized)"):]
        blk = blk[: blk.index("primary_address = address.strip()")]
        assert blk.index("_reconcile_geocode_city(") < blk.index(
            "anchor_lat, anchor_lng, _display, resolved_locality = result"
        ), "the city guard now runs after the anchor is unpacked"

    def test_override_correction_is_disclosed(self):
        src = self._main_source()
        assert '"city_correction_note": _ov_city_note or ""' in src, (
            "the correction is no longer returned to the dispatcher — a silent "
            "rewrite is the failure mode this area keeps producing"
        )


class TestOverrideCityNoteReachesTheEventLog:
    """The #740 correction must be disclosed, and must not accumulate.

    _reconcile_geocode_city emits a dispatcher-facing note on BOTH paths — the
    corrected one and the unresolved one. The override commit already guarantees
    "ONE entry per active override, regardless of iteration count"; a second
    entry that the stripper does not know about breaks that, stacking one note
    per Apply attempt.
    """

    @staticmethod
    def _fe():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_note_is_carried_from_the_response_to_the_commit(self):
        src = self._fe()
        assert "primary.city_correction_note" in src, (
            "the response field is never read, so the correction is silent"
        )
        assert "source.cityNote" in src, (
            "the note never reaches the Event Log writer"
        )

    def test_stripper_removes_both_note_shapes(self):
        """Behaviour, not presence — run the production regexes.

        Two shapes: the corrected '... re-geocoded: ...' and the unresolved
        'WARNING: ... may be geocoded to the wrong city ...'. A stripper that
        handles one and not the other still accumulates.
        """
        src = self._fe()
        fn = re.search(
            r"function _removeOverrideEventLogEntries\(.*?\n  \}", src, re.DOTALL
        )
        assert fn, "_removeOverrideEventLogEntries not found"
        pats = re.findall(r"/(\^[^/]+)/gm", fn.group(0))
        assert len(pats) == 2, f"expected 2 strip patterns, found {len(pats)}: {pats}"
        text = (
            "2026-08-09 14:32 - Staging override applied via address: 616 X Dr\n"
            "2026-08-09 14:32 - Staging override address re-geocoded: resolved to A\n"
            "2026-08-09 14:33 - WARNING: Staging override address may be geocoded "
            "to the wrong city. B\n"
            "2026-08-09 14:31 - Agency normalized: SCPD\n"
        )
        out = text
        for pat in pats:
            out = re.sub(pat.replace("\\d", r"\d"), "", out, flags=re.M)
        assert out == "2026-08-09 14:31 - Agency normalized: SCPD\n", (
            f"iterating an override would accumulate notes; residue: {out!r}"
        )


class TestEventLogLabelSeparation:
    """The override stripper must not eat the OCR-time reconciliation note.

    Two different legs emit city-reconciliation notes into the same Event Log:

      * Pass B, at OCR time (#736)  -> "Officer staging address" / "Staging address"
      * /apply-staging-override (#740) -> "Staging override address"

    Only the SECOND is an override artifact, so only the second may be stripped
    when the dispatcher iterates. The first belongs to the base summary and must
    survive — it describes the anchor the whole dispatch is built on.

    That separation currently holds only because the two label strings differ.
    Nothing declared it, and renaming either one silently breaks it in one of
    two ways: the stripper stops matching its own note (accumulation across
    iterations) or starts matching the OCR note (a real correction disappears
    from the record). Live-observed 2026-08-09 on 1.11.60, where an OCR-time
    "Officer staging address re-geocoded" note correctly survived four override
    attempts.
    """

    @staticmethod
    def _main():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @staticmethod
    def _fe():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    @classmethod
    def _override_label(cls):
        m = re.search(
            r'_reconcile_geocode_city\(\s*normalized, result, "([^"]+)"', cls._main()
        )
        assert m, "the /apply-staging-override reconciliation label was not found"
        return m.group(1)

    @classmethod
    def _pass_b_labels(cls):
        m = re.search(
            r'_pb_city_label = \(\s*"([^"]+)" if pg_is_officer\s*else "([^"]+)"',
            cls._main(),
        )
        assert m, "the Pass B reconciliation labels were not found"
        return [m.group(1), m.group(2)]

    @classmethod
    def _strip_patterns(cls):
        fn = re.search(
            r"function _removeOverrideEventLogEntries\(.*?\n  \}", cls._fe(), re.DOTALL
        )
        assert fn, "_removeOverrideEventLogEntries not found"
        pats = re.findall(r"/(\^[^/]+)/gm", fn.group(0))
        assert len(pats) == 2, f"expected 2 strip patterns, found {len(pats)}"
        return pats

    def test_labels_are_distinct(self):
        ov = self._override_label()
        for pb in self._pass_b_labels():
            assert ov != pb, (
                f"the override label {ov!r} now equals a Pass B label — the "
                f"stripper cannot tell an override note from the OCR-time note"
            )

    def test_stripper_removes_the_override_note(self):
        """BOTH shapes _reconcile_geocode_city emits — the corrected
        "... re-geocoded: ..." and the unresolved "WARNING: ... may be
        geocoded to the wrong city ...".

        Two fixtures, not one: a stripper that drops the optional WARNING
        prefix still removes the first shape, so a single-shape test passes
        while the unresolved-mismatch note accumulates on every iteration.
        """
        ov = self._override_label()
        for line in (
            f"2026-08-09 14:55 - {ov} re-geocoded: resolved to A\n",
            f"2026-08-09 14:55 - WARNING: {ov} may be geocoded to the wrong city. B\n",
        ):
            out = line
            for pat in self._strip_patterns():
                out = re.sub(pat, "", out, flags=re.M)
            assert out == "", (
                f"the override note survives the stripper, so iterating an "
                f"override stacks one note per attempt. Residue: {out!r}"
            )

    def test_stripper_preserves_the_ocr_time_notes(self):
        """Both Pass B labels, officer and alternate — two legs, two fixtures."""
        for pb in self._pass_b_labels():
            for shape in (
                f"2026-08-09 14:50 - {pb} re-geocoded: resolved to A\n",
                f"2026-08-09 14:50 - WARNING: {pb} may be geocoded to the wrong city. B\n",
            ):
                out = shape
                for pat in self._strip_patterns():
                    out = re.sub(pat, "", out, flags=re.M)
                assert out == shape, (
                    f"the stripper ate an OCR-time note ({pb!r}) — a real "
                    f"geocode correction vanishes from the dispatch record "
                    f"when the dispatcher iterates an override"
                )


# ---------------------------------------------------------------------------
# #773 — an officer-written ADDRESS beats the distance guard
# ---------------------------------------------------------------------------

_GOOGLE_APPROXIMATE_LOCATION_TYPE = "APPROXIMATE"


def _staging_answer_is_region(location_type):
    """Mirror of main.py::_staging_answer_is_region()."""
    return (location_type or "").strip().upper() == _GOOGLE_APPROXIMATE_LOCATION_TYPE


def _staging_address_distance_note(addr, dist_m):
    """Mirror of main.py::_staging_address_distance_note()."""
    miles = dist_m / 1609.344
    return (
        f'WARNING: Officer staging address "{addr.split(",")[0].strip()}" resolved '
        f"{miles:.0f} mi from the Last Known Position. The marker was plotted at the "
        f"address as written — an officer-written address is trusted over a geocoded "
        f"anchor. Verify BOTH the staging address and the Last Known Position."
    )


class TestOfficerAddressBeatsDistanceGuard:
    """Issue #773 — 2026-08-23 Placer/Auburn mutual-aid callout.

    A one-character city misspelling in a city-only LKP geocoded ~370 mi away
    in a neighbouring state. `countrycodes=us` (#667) worked as designed and
    `_house_number_consistent()` structurally cannot fire on a city-only LKP,
    so the anchor was logged as a plain success. The officer's staging address
    was correct; Google resolved it; the distance guard measured the right
    answer against the wrong reference and threw it away, then fell the officer
    entry back to the bad anchor.

    Second occurrence of the class #668 fixed for written-out COORDINATES. The
    rationale there — "a relative guard is only as trustworthy as its reference
    point" — was always general; only the input shape was coordinate-specific.
    """

    @staticmethod
    def _main_source():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @classmethod
    def _google_reject_sub_branches(cls):
        """(officer-keep window, fallthrough window) of the Google distance rejection.

        Both bound on real code markers at both ends; comments stripped so a
        rationale line naming a literal cannot satisfy an assertion about code.
        """
        src = cls._main_source()
        start = src.find(
            "elif gm_tuple and not nom_result and _staging_geocode_implausible"
        )
        assert start != -1, "Google Maps rejection branch condition not found in main.py"
        officer = src.index("if pg_is_officer and not _staging_answer_is_region", start)
        else_at = src.index("\n                    else:", officer)
        reject = src.index("Staging Pass B Google Maps geocode rejected", else_at)
        strip = lambda t: "\n".join(l.split("#")[0] for l in t.splitlines())
        return strip(src[officer:else_at]), strip(src[else_at:reject])

    # -- the region predicate ------------------------------------------------

    def test_approximate_is_a_region(self):
        assert _staging_answer_is_region("APPROXIMATE") is True

    def test_specific_location_types_are_not_regions(self):
        """The three Google location_types that name a place, not an area.

        A park — the commonest SAR staging shape, and address-less by
        construction — comes back GEOMETRIC_CENTER. This is exactly why the
        predicate is not a street-type test: `_STREET_TYPES` has no `Park`, so
        asking "does the answer look like a street" would reject legitimate
        park staging while passing a state centroid on a named road.
        """
        for lt in ("ROOFTOP", "RANGE_INTERPOLATED", "GEOMETRIC_CENTER"):
            assert _staging_answer_is_region(lt) is False, lt

    def test_missing_signal_is_not_a_region(self):
        """No signal must mean TRUST, not reject.

        The Nominatim leg has no location_type at all, and a can't-check that
        rejected would re-create the very bug this issue is about.
        """
        assert _staging_answer_is_region(None) is False
        assert _staging_answer_is_region("") is False

    def test_predicate_tolerates_case_and_whitespace(self):
        assert _staging_answer_is_region(" approximate ") is True

    # -- the note ------------------------------------------------------------

    def test_note_names_the_address_and_the_distance(self):
        note = _staging_address_distance_note(
            "800 Pacific Ave, Auburn, CA", 595_000.0
        )
        assert "800 Pacific Ave" in note
        assert "370 mi" in note
        assert "Last Known Position" in note

    def test_note_does_not_claim_the_marker_was_dropped(self):
        """`_staging_distance_note`'s docstring requires the tail to match what
        happens to the marker. BOTH of its tails are false here — the marker IS
        plotted, at the address as resolved."""
        note = _staging_address_distance_note("800 Pacific Ave, Auburn, CA", 595_000.0)
        assert "No map marker was plotted" not in note
        assert "placed at the LKP" not in note
        assert "plotted at the" in note

    # -- production parity ---------------------------------------------------

    def test_predicate_mirror_matches_production(self):
        src = self._main_source()
        m = re.search(
            r'^_GOOGLE_APPROXIMATE_LOCATION_TYPE = "([A-Z_]+)"', src, re.MULTILINE
        )
        assert m, "_GOOGLE_APPROXIMATE_LOCATION_TYPE not found in main.py"
        assert m.group(1) == _GOOGLE_APPROXIMATE_LOCATION_TYPE, (
            "backend/main.py drifted from the mirror in this file"
        )
        start = src.find("def _staging_answer_is_region(")
        assert start != -1, "_staging_answer_is_region is gone from main.py"
        body = src[src.index('"""', src.index('"""', start) + 3) + 3:
                   src.find("\ndef ", start + 1)]
        assert ".upper()" in body and "_GOOGLE_APPROXIMATE_LOCATION_TYPE" in body, (
            "the region predicate no longer case-folds the comparison against "
            "the pinned literal"
        )

    def test_note_mirror_matches_production(self):
        """main.py is not importable under local pytest, so without this the
        behavioural tests above exercise a hand-written copy and stay green
        while production drifts."""
        src = self._main_source()
        start = src.find("def _staging_address_distance_note(")
        assert start != -1, "_staging_address_distance_note is gone from main.py"
        body = src[start:src.find("\ndef ", start + 1)]
        code = "\n".join(l.split("#")[0] for l in body.splitlines())
        assert "1609.344" in code, "the note no longer converts metres to miles"
        assert "plotted at the" in code
        assert "No map marker was plotted" not in code
        assert "placed at the LKP" not in code

    # -- wiring: the Google leg gates on specificity -------------------------

    def test_google_leg_gates_the_keep_on_the_location_type(self):
        """`gm_tuple[4]` is location_type; `gm_tuple[3]` is formatted_address.

        Reading the wrong element makes the gate a constant — a formatted
        address never equals "APPROXIMATE" — so every officer address would be
        kept, including the 2026-07-24 "CalTopo Map ID:" -> "California" state
        centroid that arrived through this same officer field.
        """
        officer_window, _ = self._google_reject_sub_branches()
        assert officer_window.strip(), "empty window — this pin is not checking anything"
        assert "_staging_answer_is_region(gm_tuple[4])" in officer_window, (
            "the Google leg no longer gates the keep on Google's own "
            "specificity signal, or is reading the wrong tuple element"
        )

    def test_google_leg_keeps_the_officer_address(self):
        officer_window, _ = self._google_reject_sub_branches()
        assert 'staging_entries[entry_idx]["lat"]' in officer_window, (
            "the Google officer sub-branch no longer stores the coordinate — "
            "this is the exact 2026-08-23 regression (#773)"
        )
        assert "_staging_address_distance_note(" in officer_window

    def test_google_region_answer_still_falls_through(self):
        """The 2026-07-24 case must still be rejected."""
        _, fallthrough = self._google_reject_sub_branches()
        assert fallthrough.strip(), "empty window — this pin is not checking anything"
        assert 'staging_entries[entry_idx]["lat"]' not in fallthrough, (
            "an area-level Google answer is now stored as a staging coordinate "
            "— the state-centroid case is back"
        )
        assert "_staging_distance_note(" in fallthrough

    def test_kept_geocode_still_does_not_respell_the_summary(self):
        """Trusting a coordinate enough to plot a marker is not the same as
        trusting it to rewrite the summary text, which propagates to the
        responders' maps-link query on every surface."""
        officer_window, _ = self._google_reject_sub_branches()
        assert "_apply_street_correction_to_summary" not in officer_window, (
            "the officer-keep branch now applies a street correction from a "
            "geocode the distance guard flagged"
        )
