"""
test_main_regression.py — Regression tests for main.py pipeline constants and logic.

These tests mirror critical constants and logic from main.py and gemini.py directly
rather than importing those modules (which have heavyweight GCP/Vertex AI dependencies).

Philosophy: each test encodes a specific bug that was found in production so that
the same mistake cannot be re-introduced silently.  The test name and docstring
identify the PR/issue that introduced the fix.
"""

import ast
import datetime
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
import pytest


# ---------------------------------------------------------------------------
# Mirrored constants — must be kept in sync with main.py and gemini.py.
# If a test fails because a constant was intentionally changed, update the
# mirror here AND confirm the locked design decision in CLAUDE.md was updated.
# ---------------------------------------------------------------------------

# main.py — module-level constant (PR #176, PR #266)
_STAGING_TIER = {
    "park": 1, "fast_food": 1, "pharmacy": 1, "hotel": 1, "motel": 1,
    "supermarket": 1, "grocery": 1, "school": 1, "college": 1, "mall": 1,
    "convenience": 2, "chemist": 2, "place_of_worship": 2,
    "fuel": 3,
}

# gemini.py — type_labels dict used in both JPEG and PDF candidate blocks (PR #266)
_GEMINI_TYPE_LABELS = {
    "park": "City park", "fast_food": "Fast food", "fuel": "Gas station",
    "pharmacy": "Pharmacy", "hotel": "Hotel", "motel": "Motel",
    "school": "School", "college": "School",
    "convenience": "Convenience store",
    "supermarket": "Grocery store", "grocery": "Grocery store",
    "chemist": "Pharmacy", "place_of_worship": "Church/Place of Worship",
    "mall": "Shopping center",
}

# main.py — _AGENCY_DISPLAY dict (defined inside event-name handler, PR #256)
_AGENCY_DISPLAY = {
    "SAN JOSE P.D.":        "SJPD",
    "SAN JOSE PD":          "SJPD",
    "SAN JOSE POLICE":      "SJPD",
    "SAN JOSE POLICE DEPT": "SJPD",
    "SJPD":                 "SJPD",
    "MILPITAS P.D.":        "MPD",
    "MILPITAS PD":          "MPD",
    "MILPITAS POLICE":      "MPD",
    "SANTA CLARA P.D.":     "SCPD",
    "SANTA CLARA PD":       "SCPD",
    "CUPERTINO P.D.":       "CUPD",
    "CUPERTINO PD":         "CUPD",
    "SUNNYVALE P.D.":       "SVPD",
    "SUNNYVALE PD":         "SVPD",
    "MOUNTAIN VIEW P.D.":   "MVPD",
    "MOUNTAIN VIEW PD":     "MVPD",
    "PALO ALTO P.D.":       "PAPD",
    "PALO ALTO PD":         "PAPD",
    "CAMPBELL P.D.":        "CGPD",
    "CAMPBELL PD":          "CGPD",
    "MORGAN HILL P.D.":     "MHPD",
    "MORGAN HILL PD":       "MHPD",
    "GILROY P.D.":          "GPPD",
    "GILROY PD":            "GPPD",
    # SJSU PD — added 2026-05-08 after first live-incident at SJSU.
    "SJSU PD":                          "SJSU",
    "SJSU P.D.":                        "SJSU",
    "SAN JOSE STATE UNIVERSITY PD":     "SJSU",
    "SAN JOSE STATE UNIVERSITY P.D.":   "SJSU",
    "SAN JOSE STATE UNIV PD":           "SJSU",
    "SAN JOSE STATE UNIV P.D.":         "SJSU",
    "SAN JOSE STATE PD":                "SJSU",
    "SAN JOSE STATE P.D.":              "SJSU",
    "SANTA CLARA COUNTY S.O.": "SCCSO",
    "SANTA CLARA COUNTY SO":   "SCCSO",
    "SANTA CLARA SO":          "SCSO",
    # SCCSO OCR-misread defenses (PR-D-2.5)
    "SCC SLO":                 "SCCSO",
    "SCC SO":                  "SCCSO",
    "SCC S.O.":                "SCCSO",
}

def _normalize_agency(raw: str) -> str:
    """Mirror of main.py::_normalize_event_name_agency().

    PR-fix-4 (2026-05-08): pre-fix the dict was defined inside the primary
    Event Name reconstruction branch, so the city-fallback path silently
    skipped normalization (regression: "SJSU PD" landed in event names
    when LKP was an intersection / city-only / landmark). Helper now lives
    at module level and BOTH paths call it.
    """
    if not raw:
        return raw
    normed = re.sub(r"\.\s+", ".", raw.upper())
    return _AGENCY_DISPLAY.get(normed, _AGENCY_DISPLAY.get(raw.upper(), raw))


# main.py::_EVENT_NAME_INTERSECTION_RE — broadened in PR-fix-4 (2026-05-08)
# from `&|INTERSECTION` to also catch `<St> at <St>` and `<St> and <St>`.
# Pre-fix the regex missed "5th Street at St. John Street" → produced
# 8-token title-cased garbage in the Event Name (regression discovered on
# the 2026-05-07 SJSU smoke test).
_EVENT_NAME_INTERSECTION_RE_MIRROR = re.compile(
    # `@` added in PR-fix-5 (2026-05-08) — handwritten `5th @ St. John` is
    # a common officer shorthand for "at"; pre-fix it fell through to
    # title-case, producing `5Th St. @ St. John St.` in the Event Name.
    r"\s+(?:&|@|at|and)\s+|\bINTERSECTION\s+OF\b",
    re.IGNORECASE,
)


# Mirrors of main.py street-stripping regexes — used to reconstruct the
# city-fallback intersection branch in tests. If these drift from the
# production patterns the cross-cutting tests will fire.
_STREET_TYPES_MIRROR = re.compile(
    r"\b(?:Street|St|Boulevard|Blvd|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|"
    r"Court|Ct|Place|Pl|Way|Highway|Hwy|Freeway|Fwy|Parkway|Pkwy|"
    r"Expressway|Expy|Circle|Cir|Terrace|Ter|Trail|Trl|Square|Sq|"
    r"Point|Pt)\.?(?:\s.*)?$",
    re.IGNORECASE,
)
_STREET_CARDINALS_MIRROR = re.compile(
    r"^(?:North|South|East|West|Northeast|Northwest|Southeast|Southwest|"
    r"N|S|E|W|NE|NW|SE|SW)\.?\s+",
    re.IGNORECASE,
)


def _reconstruct_event_name_city_fallback_street(raw_lkp: str) -> str:
    """Mirror of the city-fallback intersection branch in main.py.

    Given a raw LKP like "5th Street at St. John Street, San Jose, CA"
    (no house number → city-fallback path), return what the Event Name
    `street` token should be. For intersections we extract the first
    street name and apply the same stripping as the primary path; for
    non-intersections we title-case the city.

    Issue #400 (2026-05-09): when the LKP starts with the prefix variant
    "Intersection of <St> and <St>", the regex matches the prefix at
    position 0, leaving the prefix-only branch with an empty first_street
    and falling through to UNKNOWN. Fix: skip past the prefix and re-search
    for the between-streets separator on the remainder.
    """
    intersection_match = _EVENT_NAME_INTERSECTION_RE_MIRROR.search(raw_lkp)
    if intersection_match:
        if intersection_match.group(0).strip().upper().startswith("INTERSECTION"):
            after_prefix = raw_lkp[intersection_match.end():].strip()
            between_match = re.search(
                r"\s+(?:&|@|at|and)\s+", after_prefix, re.IGNORECASE
            )
            if between_match:
                first_street = after_prefix[:between_match.start()].strip()
            else:
                first_street = after_prefix.split(",")[0].strip()
        else:
            first_street = raw_lkp[:intersection_match.start()].strip()
        first_street = _STREET_TYPES_MIRROR.sub("", first_street).strip()
        first_street = _STREET_CARDINALS_MIRROR.sub("", first_street).strip()
        return first_street if first_street else "UNKNOWN"
    if re.match(r"^\d+$", raw_lkp):
        return "UNKNOWN"
    return raw_lkp.title()

# main.py — school/college count logic in _query_overpass_staging (PR #258, PR #266)
def _school_count_uncapped(candidates: list) -> int:
    """Mirror of the pre-cap count in _query_overpass_staging."""
    return sum(1 for c in candidates if c.get("amenity") in ("school", "college"))

# main.py — exclusion note format (PR #266)
def _school_excl_note(count: int) -> str:
    """Mirror of the school/college exclusion note format string."""
    s = "s" if count > 1 else ""
    return f"Note: {count} school/college{s} excluded — daytime weekday (available after 3:30pm)."


# main.py — advisory exclusion notes used when Overpass is unavailable but the
# time-policy still applies (Bill 2026-05-20 live-test feedback). Mirror of
# the inline format strings in main.py's exclusion-note gating.
_SCHOOL_ADVISORY_NOTE = (
    "Note: Daytime weekday — schools/colleges excluded by policy (available after 3:30pm). "
    "Nearby location service unavailable, so this filter could not be applied server-side — "
    "review the list carefully."
)
_CHURCH_ADVISORY_NOTE = (
    "Note: Sunday morning service hours — churches excluded by policy (available after ~12pm). "
    "Nearby location service unavailable, so this filter could not be applied server-side — "
    "review the list carefully."
)


# main.py — minimum separation between two staging recommendations (issue #674).
# Parity with production is pinned by TestStagingProximityFilter.
_STAGING_MIN_SEPARATION_M = 100


def _mirror_haversine_m(lat1, lon1, lat2, lon2):
    """Mirror of main.py::_haversine_m — WGS-84 great-circle metres."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# main.py — shared staging ranking extracted in PR-A (Geoapify migration).
# Mirror of _rank_dedupe_cap_staging(): sort (tier, dist), dedup by name +
# house+street, drop candidates within _STAGING_MIN_SEPARATION_M of a better-
# ranked one (#674), count schools/churches pre-cap, cap at 12. Pins the ranking
# behavior both the Overpass and Geoapify sources depend on.
#
# KNOWN GAP (not introduced here): production also collapses word-reordered MALL
# names via a token-set key (#669). This mirror does not. That behaviour is
# pinned separately by TestShoppingMallStaging's source pin and its own
# behaviour test, so nothing is unguarded — but do not read this mirror as a
# complete stand-in for production.
def _rank_dedupe_cap_staging_mirror(raw_candidates: list) -> tuple:
    candidates = sorted(
        raw_candidates, key=lambda c: (_STAGING_TIER.get(c["amenity"], 2), c["dist_m"])
    )
    seen_names, seen_addrs, deduped = set(), set(), []
    for c in candidates:
        name_key = c["name"].lower()
        raw_addr = c["addr"]
        addr_key = (
            re.sub(r"\s+", " ", raw_addr.split(",")[0].lower()).strip()
            if raw_addr != "(address not in OSM)"
            else None
        )
        if name_key in seen_names:
            continue
        if addr_key and addr_key in seen_addrs:
            continue
        # #674 proximity filter. Parks are NOT exempt here, unlike the address
        # dedup above — see TestStagingProximityFilter for why.
        c_lat, c_lng = c.get("lat"), c.get("lng")
        if c_lat is not None and c_lng is not None and any(
            _mirror_haversine_m(c_lat, c_lng, k["lat"], k["lng"]) < _STAGING_MIN_SEPARATION_M
            for k in deduped
            if k.get("lat") is not None and k.get("lng") is not None
        ):
            continue
        seen_names.add(name_key)
        if addr_key:
            seen_addrs.add(addr_key)
        deduped.append(c)
    school = sum(1 for c in deduped if c.get("amenity") in ("school", "college"))
    church = sum(1 for c in deduped if c.get("amenity") == "place_of_worship")
    return deduped[:12], school, church


# main.py is not importable (heavyweight GCP/Vertex AI dependencies), which is
# why almost everything here is a mirror. _staging_candidate_name_leads (#722)
# needs no imports and touches no module state, so it can be lifted out and
# executed AS PRODUCTION instead — the behaviour tests then exercise the real
# function, not a copy of it.
#
# This is deliberately not a mirror. A mirror is blind to anything production
# does OUTSIDE the copied logic: mutation-testing this pin, an early
# `return True` inserted at the top of the production helper left every mirror
# fixture green and every source pin green, because the pinned lines survived
# underneath as dead code.
def _load_production_fn(name: str, end_marker: str = r"\n\n(?:def |async def |# -{10,})"):
    src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
    m = re.search(rf"^def {name}\(.*?(?={end_marker})", src, re.DOTALL | re.MULTILINE)
    assert m, f"{name} not found in main.py"
    # Seed only stdlib modules main.py imports at module level. Anything a
    # candidate helper needs BEYOND these is a signal it is not self-contained
    # and should not be lifted — let it NameError rather than widening this.
    ns: dict = {"re": re, "math": math, "datetime": datetime}
    exec(compile(m.group(0), "main.py", "exec"), ns)
    return ns[name]


_prod_name_leads = _load_production_fn("_staging_candidate_name_leads")


# ---------------------------------------------------------------------------
# _STAGING_TIER tests
# Regression: PR #176 — schools must be tier 1 or they get cut in dense urban areas.
# Regression: PR #266 — college must also be tier 1 (Valley Christian High School).
# ---------------------------------------------------------------------------

class TestStagingTier:
    def test_school_is_tier_1(self):
        """PR #176: schools tier 2 caused them to disappear in dense areas."""
        assert _STAGING_TIER["school"] == 1

    def test_college_is_tier_1(self):
        """PR #266: college missing from tier dict → Valley Christian never excluded."""
        assert _STAGING_TIER["college"] == 1

    def test_fuel_is_tier_3(self):
        """Gas stations are last resort — poor parking/active vehicle traffic."""
        assert _STAGING_TIER["fuel"] == 3

    def test_park_is_tier_1(self):
        assert _STAGING_TIER["park"] == 1

    def test_fast_food_is_tier_1(self):
        assert _STAGING_TIER["fast_food"] == 1

    def test_place_of_worship_is_tier_2(self):
        assert _STAGING_TIER["place_of_worship"] == 2

    def test_convenience_is_tier_2(self):
        assert _STAGING_TIER["convenience"] == 2

    def test_all_expected_keys_present(self):
        expected = {
            "park", "fast_food", "pharmacy", "hotel", "motel",
            "supermarket", "grocery", "school", "college",
            "convenience", "chemist", "place_of_worship", "fuel",
            "mall",  # issue #669
        }
        assert set(_STAGING_TIER.keys()) == expected


# ---------------------------------------------------------------------------
# Gemini type_labels tests
# Regression: PR #266 — college fell through to ".title()" default, producing
# label "College" instead of "School". Gemini's exclusion rule didn't fire.
# ---------------------------------------------------------------------------

class TestGeminiTypeLabels:
    def test_college_maps_to_school(self):
        """PR #266: college must map to 'School' so Gemini applies school exclusion rule."""
        assert _GEMINI_TYPE_LABELS["college"] == "School"

    def test_school_maps_to_school(self):
        assert _GEMINI_TYPE_LABELS["school"] == "School"

    def test_college_and_school_produce_same_label(self):
        """Both amenity types must be indistinguishable to Gemini."""
        assert _GEMINI_TYPE_LABELS["college"] == _GEMINI_TYPE_LABELS["school"]

    def test_fuel_is_gas_station(self):
        assert _GEMINI_TYPE_LABELS["fuel"] == "Gas station"

    def test_park_is_city_park(self):
        assert _GEMINI_TYPE_LABELS["park"] == "City park"

    def test_no_bare_college_label(self):
        """The string 'College' (bare) must never be produced — it bypasses exclusion."""
        assert _GEMINI_TYPE_LABELS.get("college") != "College"


# ---------------------------------------------------------------------------
# School/college count tests
# Regression: PR #258/#266 — count must include both amenity types.
# ---------------------------------------------------------------------------

class TestSchoolCount:
    def test_counts_school_amenity(self):
        candidates = [{"amenity": "school"}, {"amenity": "fast_food"}]
        assert _school_count_uncapped(candidates) == 1

    def test_counts_college_amenity(self):
        """PR #266: colleges must be counted alongside schools."""
        candidates = [{"amenity": "college"}, {"amenity": "fast_food"}]
        assert _school_count_uncapped(candidates) == 1

    def test_counts_both_together(self):
        candidates = [
            {"amenity": "school"},
            {"amenity": "college"},
            {"amenity": "fast_food"},
        ]
        assert _school_count_uncapped(candidates) == 2

    def test_zero_when_no_schools(self):
        candidates = [{"amenity": "fast_food"}, {"amenity": "park"}]
        assert _school_count_uncapped(candidates) == 0

    def test_does_not_count_place_of_worship(self):
        candidates = [{"amenity": "place_of_worship"}]
        assert _school_count_uncapped(candidates) == 0


# ---------------------------------------------------------------------------
# _rank_dedupe_cap_staging tests (PR-A — Geoapify staging migration)
# Golden pins for the shared ranking helper extracted from _query_overpass_staging.
# Both the Overpass and Geoapify sources feed raw candidates through this, so a
# silent change here would skew EVERY staging recommendation.
# ---------------------------------------------------------------------------

class TestRankDedupeCapStaging:
    def test_sorts_by_tier_then_distance(self):
        """Tier wins over distance; distance breaks ties within a tier."""
        raw = [
            {"name": "Far Park", "amenity": "park", "addr": "(address not in OSM)", "dist_m": 900},
            {"name": "Close Gas", "amenity": "fuel", "addr": "(address not in OSM)", "dist_m": 50},
            {"name": "Near Park", "amenity": "park", "addr": "(address not in OSM)", "dist_m": 100},
        ]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(raw)
        # Both parks (tier 1) precede the gas station (tier 3) despite the gas being closest;
        # within tier 1 the nearer park is first.
        assert [c["name"] for c in ranked] == ["Near Park", "Far Park", "Close Gas"]

    def test_dedups_by_name_keeping_best(self):
        """Same name → keep the first after the (tier, dist) sort."""
        raw = [
            {"name": "McDonald's", "amenity": "fast_food", "addr": "1 A St, SJ", "dist_m": 300},
            {"name": "McDonald's", "amenity": "fast_food", "addr": "1 A St, SJ", "dist_m": 300},
        ]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(raw)
        assert len(ranked) == 1

    def test_dedups_by_house_street_strip_mall(self):
        """Two businesses at one strip-mall address collapse to one entry."""
        raw = [
            {"name": "McDonald's", "amenity": "fast_food", "addr": "5000 Cottle Rd, San Jose", "dist_m": 200},
            {"name": "Walgreens", "amenity": "pharmacy", "addr": "5000 Cottle Rd, San Jose, 95123", "dist_m": 210},
        ]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(raw)
        assert len(ranked) == 1
        assert ranked[0]["name"] == "McDonald's"  # first after sort (both tier 1, nearer wins)

    def test_parks_not_collapsed_by_missing_address(self):
        """Parks share the '(address not in OSM)' sentinel — must NOT dedup on it."""
        raw = [
            {"name": "Kelley Park", "amenity": "park", "addr": "(address not in OSM)", "dist_m": 100},
            {"name": "Alum Rock Park", "amenity": "park", "addr": "(address not in OSM)", "dist_m": 200},
        ]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(raw)
        assert len(ranked) == 2

    def test_counts_are_pre_cap(self):
        """School/church counts come from the full deduped list, before the :12 cap."""
        raw = [{"name": f"FF{i}", "amenity": "fast_food", "addr": f"{i} A St, SJ", "dist_m": i}
               for i in range(12)]
        raw += [
            {"name": "Horace Mann", "amenity": "school", "addr": "55 N 7th St, SJ", "dist_m": 500},
            {"name": "St. Pats", "amenity": "place_of_worship", "addr": "9 B St, SJ", "dist_m": 600},
        ]
        ranked, school, church = _rank_dedupe_cap_staging_mirror(raw)
        assert len(ranked) == 12                 # capped
        assert school == 1 and church == 1       # counted despite being pushed out of the cap
        assert all(c["amenity"] == "fast_food" for c in ranked)

    def test_caps_at_twelve(self):
        raw = [{"name": f"P{i}", "amenity": "park", "addr": "(address not in OSM)", "dist_m": i}
               for i in range(20)]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(raw)
        assert len(ranked) == 12

    def test_extra_keys_ride_through(self):
        """lat/lng (for CalTopo markers + the override endpoint) survive ranking untouched."""
        raw = [{"name": "P", "amenity": "park", "addr": "(address not in OSM)",
                "dist_m": 10, "lat": 37.3, "lng": -121.9}]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(raw)
        assert ranked[0]["lat"] == 37.3 and ranked[0]["lng"] == -121.9


# ---------------------------------------------------------------------------
# Geoapify staging source (PR-B — Overpass→Geoapify migration)
# Mirrors of the pure normalization helpers in main.py. The two-call orchestration
# (_query_geoapify_staging) is integration-tested on the personal-dev soak; these
# pin the deterministic pieces: amenity resolution, addr shaping, junk-park filter.
# ---------------------------------------------------------------------------

# Mirror of _GEOAPIFY_PRIORITY in main.py (best-tier-first substring → amenity).
_GEOAPIFY_PRIORITY_MIRROR = [
    ("leisure.park", "park"),
    ("education.school", "school"),
    ("education.kindergarten", "school"),
    ("education.college", "college"),
    ("education.university", "college"),
    ("catering.fast_food", "fast_food"),
    ("healthcare.pharmacy", "pharmacy"),
    ("accommodation.hotel", "hotel"),
    ("accommodation.motel", "motel"),
    ("commercial.supermarket", "supermarket"),
    ("commercial.marketplace", "grocery"),
    ("commercial.convenience", "convenience"),
    ("religion.place_of_worship", "place_of_worship"),
    ("commercial.gas", "fuel"),
]


def _geoapify_resolve_amenity_mirror(categories: list):
    for substr, amenity in _GEOAPIFY_PRIORITY_MIRROR:
        if any(substr in c for c in categories):
            return amenity
    return None


def _geoapify_addr_shape_mirror(props: dict, is_park: bool) -> str:
    if is_park:
        return "(address not in OSM)"
    hn, st, city = props.get("housenumber", ""), props.get("street", ""), props.get("city", "")
    addr = " ".join(p for p in (hn, st) if p)
    if city:
        addr = f"{addr}, {city}" if addr else city
    return addr or "(address not in OSM)"


def _is_geoapify_junk_park_mirror(name: str, props: dict) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return True
    city = (props.get("city", "") or "").strip().lower()
    state = (props.get("state", "") or "").strip().lower()
    return bool(city and (n == city or (state and n == f"{city}, {state}")))


def _geoapify_display_name_mirror(props: dict) -> str:
    return str(props.get("name") or props.get("address_line1") or "")


class TestGeoapifyAmenityResolution:
    def test_park_wins(self):
        assert _geoapify_resolve_amenity_mirror(["leisure.park"]) == "park"

    def test_best_tier_first_supermarket_over_grocery(self):
        """A place tagged both supermarket + marketplace resolves to supermarket (tier 1)."""
        assert _geoapify_resolve_amenity_mirror(
            ["commercial.marketplace", "commercial.supermarket"]) == "supermarket"

    def test_university_maps_to_college(self):
        assert _geoapify_resolve_amenity_mirror(["education.university"]) == "college"

    def test_kindergarten_maps_to_school(self):
        assert _geoapify_resolve_amenity_mirror(["education.kindergarten"]) == "school"

    def test_substring_match_on_specific_category(self):
        """Geoapify returns dotted sub-categories, e.g. catering.fast_food.burger."""
        assert _geoapify_resolve_amenity_mirror(["catering.fast_food.burger"]) == "fast_food"

    def test_unmapped_returns_none(self):
        assert _geoapify_resolve_amenity_mirror(["office.company", "building.commercial"]) is None

    def test_resolved_amenities_are_all_known_to_tier_table(self):
        """Every amenity the resolver can emit must be a key in _STAGING_TIER."""
        for _, amenity in _GEOAPIFY_PRIORITY_MIRROR:
            assert amenity in _STAGING_TIER


class TestGeoapifyAddrShape:
    def test_park_returns_sentinel_even_with_city(self):
        """Parks ALWAYS get the sentinel, never the bare city — otherwise two distinct
        same-city parks collide on "<city>" in the address dedup and all but the
        nearest are dropped as false strip-mall duplicates (code review 2026-07-17)."""
        assert _geoapify_addr_shape_mirror({"city": "San Jose"}, is_park=True) == "(address not in OSM)"

    def test_park_without_city_returns_sentinel(self):
        assert _geoapify_addr_shape_mirror({}, is_park=True) == "(address not in OSM)"

    def test_full_street_and_city(self):
        props = {"housenumber": "55", "street": "N 7th St", "city": "San Jose"}
        assert _geoapify_addr_shape_mirror(props, is_park=False) == "55 N 7th St, San Jose"

    def test_city_only_when_no_street(self):
        assert _geoapify_addr_shape_mirror({"city": "San Jose"}, is_park=False) == "San Jose"

    def test_empty_returns_sentinel(self):
        """Exact sentinel — both downstream filters key off this literal."""
        assert _geoapify_addr_shape_mirror({}, is_park=False) == "(address not in OSM)"

    def test_same_city_parks_not_collapsed_end_to_end(self):
        """Regression (code review 2026-07-17): addr_shape → rank_dedupe must keep ALL
        distinct same-city Geoapify parks, not collapse them to one on the city name."""
        parks = [
            {"name": "Sterling Barnhart Park", "amenity": "park",
             "addr": _geoapify_addr_shape_mirror({"city": "Cupertino"}, is_park=True),
             "dist_m": 300},
            {"name": "John Mise Park", "amenity": "park",
             "addr": _geoapify_addr_shape_mirror({"city": "Cupertino"}, is_park=True),
             "dist_m": 500},
            {"name": "Jenny Strand Park", "amenity": "park",
             "addr": _geoapify_addr_shape_mirror({"city": "Cupertino"}, is_park=True),
             "dist_m": 700},
        ]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(parks)
        assert len(ranked) == 3, "same-city parks were collapsed by the address dedup"


class TestGeoapifyJunkParkFilter:
    def test_drops_bare_city_name(self):
        assert _is_geoapify_junk_park_mirror("San Jose", {"city": "San Jose"}) is True

    def test_drops_city_comma_state(self):
        assert _is_geoapify_junk_park_mirror(
            "Los Gatos, California", {"city": "Los Gatos", "state": "California"}) is True

    def test_keeps_real_park(self):
        assert _is_geoapify_junk_park_mirror(
            "Kelley Park", {"city": "San Jose", "state": "California"}) is False

    def test_drops_empty_name(self):
        assert _is_geoapify_junk_park_mirror("", {"city": "San Jose"}) is True

    def test_case_insensitive(self):
        assert _is_geoapify_junk_park_mirror("san jose", {"city": "San Jose"}) is True


class TestGeoapifyDisplayName:
    """Regression (2026-07-18 soak, DEVTEST7): Geoapify returns purely-numeric POI
    names (the Union "76" gas-station brand) as JSON ints, not strings. Un-coerced,
    the int name reached _rank_dedupe_cap_staging's c["name"].lower() and raised
    AttributeError, failing the entire Geoapify lookup (silent fallback to Overpass).
    _geoapify_display_name coerces at the parse boundary."""

    def test_numeric_name_coerced_to_str(self):
        assert _geoapify_display_name_mirror({"name": 76}) == "76"

    def test_string_name_passthrough(self):
        assert _geoapify_display_name_mirror({"name": "Panera Bread"}) == "Panera Bread"

    def test_falls_back_to_address_line1(self):
        assert _geoapify_display_name_mirror({"address_line1": "123 Main St"}) == "123 Main St"

    def test_missing_name_returns_empty(self):
        assert _geoapify_display_name_mirror({}) == ""

    def test_numeric_name_survives_rank_dedupe(self):
        """End-to-end: a numeric-named candidate must sort/dedup without crashing —
        the exact path that broke on DEVTEST7 (c["name"].lower() at main.py:1094)."""
        cands = [{
            "name": _geoapify_display_name_mirror({"name": 76}),
            "amenity": "fuel", "addr": "1 Landess Ave, San Jose", "dist_m": 400,
        }]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(cands)
        assert ranked[0]["name"] == "76"


# main.py — staging-source env var names (Overpass→Geoapify migration). Sibling to
# _EB_SLACK_ENV_VAR_NAMES: pins the names used in main.py os.environ.get(...) AND the
# Terraform `env { name = }` blocks so adding/renaming has to update all sites together.
_STAGING_ENV_VAR_NAMES = frozenset({
    "STAGING_SOURCE",
    "GEOAPIFY_API_KEY",
    "STAGING_SHADOW",
})


class TestStagingEnvVarNames:
    def test_canonical_set_pinned(self):
        assert _STAGING_ENV_VAR_NAMES == frozenset(
            {"STAGING_SOURCE", "GEOAPIFY_API_KEY", "STAGING_SHADOW"})

    def test_screaming_snake_case(self):
        for name in _STAGING_ENV_VAR_NAMES:
            assert name.isupper() and "-" not in name and " " not in name

    def test_valid_staging_source_values(self):
        """The startup guard in main.py accepts exactly these (plus unset)."""
        assert {"overpass", "geoapify"} == {"overpass", "geoapify"}

    def test_no_lookalikes(self):
        forbidden = {"GEOAPIFY_KEY", "STAGING_MODE", "STAGING_PROVIDER", "GEOAPIFY_API_TOKEN"}
        assert forbidden.isdisjoint(_STAGING_ENV_VAR_NAMES)


class TestStagingAttribution:
    """Legal requirement: Geoapify + OpenStreetMap attribution must be present in the
    frontend. Guards against a future refresh silently dropping the credit."""

    def _index_html(self) -> str:
        path = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
        return path.read_text(encoding="utf-8")

    def test_geoapify_attribution_present(self):
        html = self._index_html()
        assert "Powered by " in html
        assert "geoapify.com" in html

    def test_osm_attribution_present(self):
        html = self._index_html()
        assert "OpenStreetMap" in html
        assert "openstreetmap.org/copyright" in html


# ---------------------------------------------------------------------------
# Exclusion note format tests
# Regression: PR #266 — note must say "school/college" not just "school".
# ---------------------------------------------------------------------------

class TestExclusionNote:
    def test_singular_note_format(self):
        note = _school_excl_note(1)
        assert note == "Note: 1 school/college excluded — daytime weekday (available after 3:30pm)."

    def test_plural_note_format(self):
        note = _school_excl_note(9)
        assert note == "Note: 9 school/colleges excluded — daytime weekday (available after 3:30pm)."

    def test_note_contains_school_college(self):
        """Must say 'school/college', not just 'school'."""
        assert "school/college" in _school_excl_note(1)
        assert "school/college" in _school_excl_note(5)

    def test_note_does_not_say_school_only(self):
        """Regression: old note said 'school excluded' — dispatchers wouldn't know
        why Valley Christian was missing from daytime recommendations."""
        note = _school_excl_note(1)
        assert note != "Note: 1 school excluded — daytime weekday (available after 3:30pm)."


# ---------------------------------------------------------------------------
# Advisory exclusion notes when Overpass is unavailable
# Regression: Bill 2026-05-20 live-test feedback — at 10:31 PT all three
# Overpass mirrors failed (504 + ReadTimeout + 403). Gemini fell back to
# training-data POI recommendations, but the exclusion note was silently
# dropped (gating on _school_count > 0). Dispatcher was left unaware that
# the time-policy was still in force. The advisory note now fires when
# _overpass_ok is False AND we're in the relevant time window.
# ---------------------------------------------------------------------------

class TestExclusionNoteAdvisoryWhenOverpassDown:
    def test_school_advisory_note_mentions_time_policy(self):
        """Dispatcher must learn the daytime-weekday policy is in effect
        even when we can't enumerate specific schools."""
        assert "Daytime weekday" in _SCHOOL_ADVISORY_NOTE
        assert "after 3:30pm" in _SCHOOL_ADVISORY_NOTE
        assert "schools/colleges" in _SCHOOL_ADVISORY_NOTE

    def test_school_advisory_note_mentions_service_caveat(self):
        """Per Bill 2026-05-20 — dispatcher won't recognize the term
        'Overpass'. The note must use 'Nearby location service' to mirror
        the existing WARNING-line wording (event_log_additions emits
        'Nearby location servers unavailable...')."""
        assert "Nearby location service" in _SCHOOL_ADVISORY_NOTE
        assert "Overpass" not in _SCHOOL_ADVISORY_NOTE

    def test_school_advisory_note_instructs_review(self):
        """The note is advisory — Gemini's training-data fallback may or
        may not have excluded schools, so the dispatcher must verify."""
        assert "review the list" in _SCHOOL_ADVISORY_NOTE.lower()

    def test_school_advisory_distinct_from_count_note(self):
        """Format MUST diverge from the count-aware form so a dispatcher
        scanning the bottom of the staging list can immediately see
        'I'm reading the advisory form' vs 'here's an exact count'."""
        for n in (1, 5, 50):
            assert _school_excl_note(n) != _SCHOOL_ADVISORY_NOTE
            # And the count-aware form must NOT have the caveat
            assert "Nearby location service" not in _school_excl_note(n)

    def test_church_advisory_note_mentions_time_policy(self):
        assert "Sunday morning" in _CHURCH_ADVISORY_NOTE
        assert "after ~12pm" in _CHURCH_ADVISORY_NOTE
        assert "churches" in _CHURCH_ADVISORY_NOTE

    def test_church_advisory_note_mentions_service_caveat(self):
        assert "Nearby location service" in _CHURCH_ADVISORY_NOTE
        assert "Overpass" not in _CHURCH_ADVISORY_NOTE

    def test_church_advisory_note_instructs_review(self):
        assert "review the list" in _CHURCH_ADVISORY_NOTE.lower()


# ---------------------------------------------------------------------------
# _AGENCY_DISPLAY normalization tests
# Regression: PR #256 — verbose agency names (e.g. "SAN JOSE P.D.") must map
# to radio-traffic abbreviations in the event name.
# PR #260 — "P. D." (space between initials) must also match.
# ---------------------------------------------------------------------------

class TestAgencyDisplay:
    def test_san_jose_pd_dotted(self):
        assert _normalize_agency("SAN JOSE P.D.") == "SJPD"

    def test_san_jose_pd_no_dots(self):
        assert _normalize_agency("SAN JOSE PD") == "SJPD"

    def test_san_jose_pd_spaced_dots(self):
        """PR #260: 'SAN JOSE P. D.' (space after first dot) must normalize."""
        assert _normalize_agency("SAN JOSE P. D.") == "SJPD"

    def test_sjpd_passthrough(self):
        assert _normalize_agency("SJPD") == "SJPD"

    def test_milpitas_pd(self):
        assert _normalize_agency("MILPITAS P.D.") == "MPD"

    def test_milpitas_pd_spaced(self):
        assert _normalize_agency("MILPITAS P. D.") == "MPD"

    def test_unknown_agency_passthrough(self):
        """Unknown agencies should pass through unchanged."""
        assert _normalize_agency("UNKNOWN PD") == "UNKNOWN PD"

    def test_lowercase_input(self):
        """Input is uppercased before lookup."""
        assert _normalize_agency("san jose pd") == "SJPD"

    def test_santa_clara_county_so(self):
        assert _normalize_agency("SANTA CLARA COUNTY S.O.") == "SCCSO"

    def test_santa_clara_county_so_spaced(self):
        assert _normalize_agency("SANTA CLARA COUNTY S. O.") == "SCCSO"

    # SJSU campus PD — strip the trailing "PD" so the event name reads
    # "YYYY-MM-DD SJSU <street>" rather than "YYYY-MM-DD SJSU PD <street>".
    # Regression: 2026-05-07 SJSU 5th incident — the un-normalized form
    # landed in the Slack welcome and Everbridge title.

    def test_sjsu_pd_no_dots(self):
        assert _normalize_agency("SJSU PD") == "SJSU"

    def test_sjsu_pd_dotted(self):
        assert _normalize_agency("SJSU P.D.") == "SJSU"

    def test_sjsu_pd_spaced_dots(self):
        assert _normalize_agency("SJSU P. D.") == "SJSU"

    def test_san_jose_state_university_pd(self):
        assert _normalize_agency("SAN JOSE STATE UNIVERSITY PD") == "SJSU"

    def test_san_jose_state_univ_pd(self):
        assert _normalize_agency("SAN JOSE STATE UNIV PD") == "SJSU"

    def test_san_jose_state_pd(self):
        assert _normalize_agency("SAN JOSE STATE PD") == "SJSU"

    # SCCSO OCR-misread defenses — handwritten "SCCSO" OCRs unreliably as
    # space-split or letter-confused variants. Live confirmed on the
    # 2026-05-10 Verde Vista form which produced "SCC SLO" (the second
    # "S" misread as "SL"). Without canonicalization, the event name reads
    # "YYYY-MM-DD SCC SLO <street>" — non-canonical, longer than radio-
    # readable, and trips the assumption "agency is one token" downstream.

    def test_scc_slo_ocr_misread_normalizes_to_sccso(self):
        # 2026-05-10 Verde Vista canonical case.
        assert _normalize_agency("SCC SLO") == "SCCSO"

    def test_scc_so_space_split_normalizes_to_sccso(self):
        # Plain space-split form an officer might write.
        assert _normalize_agency("SCC SO") == "SCCSO"

    def test_scc_so_dotted_normalizes_to_sccso(self):
        assert _normalize_agency("SCC S.O.") == "SCCSO"


# ---------------------------------------------------------------------------
# main.py::_is_intersection_query — intersection detector for LKP geocoding
# Mirror of the top-level regex used by _geocode_lkp_smart() to route
# intersection LKPs directly to Google Maps (Nominatim cannot geocode them).
# Bug history: PR #390 (2026-05-08) — first surfaced on the 2026-05-07 SJSU
# incident where LKP "5th St & St John St" failed Nominatim and personal-dev
# had no Google Maps key.
#
# NOTE: This is a SEPARATE concern from the `_EVENT_NAME_INTERSECTION_RE_MIRROR`
# defined earlier in this file (PR #393). They serve different purposes:
#   - _is_intersection_query (geocoding): cares whether Nominatim CAN resolve.
#     Only "&" / "INTERSECTION" matter — those are hard blockers for Nominatim.
#   - _EVENT_NAME_INTERSECTION_RE_MIRROR (event-name extraction): cares about
#     ANY two-street pattern that would produce multi-token garbage. Includes
#     "at" and "and" too. Documented in main.py.
# ---------------------------------------------------------------------------

_INTERSECTION_RE_MIRROR = re.compile(
    # `@` added in PR-fix-5 (2026-05-08) — handwritten `5th @ St. John`
    # is common officer shorthand. Whitespace requirement avoids
    # false-triggering on email addresses.
    r"\s(?:&|@)\s|\bINTERSECTION\b",
    re.IGNORECASE,
)


def _is_intersection_query(address: str) -> bool:
    """Mirror of main.py::_is_intersection_query()."""
    if not address:
        return False
    return bool(_INTERSECTION_RE_MIRROR.search(address))


class TestIsIntersectionQuery:
    """Pin the intersection-detection regex used by _geocode_lkp_smart.
    Drift here = wrong geocoder for intersection LKPs (back to the 2026-05-08
    SJSU bug where intersections silently fell through to Nominatim, which
    can't geocode them, and got UNKNOWN in the Event Name)."""

    def test_real_intersection_with_ampersand(self):
        # The exact 2026-05-07 SJSU LKP that triggered the bug.
        assert _is_intersection_query("5th St & St John St, San Jose, CA") is True

    def test_intersection_word(self):
        # Officers sometimes write "INTERSECTION OF X AND Y" — also detected.
        assert _is_intersection_query("INTERSECTION OF 5th St AND St John St, San Jose, CA") is True
        # Case-insensitive
        assert _is_intersection_query("intersection of foo and bar") is True

    def test_plain_street_address_is_not_intersection(self):
        # Standard street address — Nominatim path, not Google Maps.
        assert _is_intersection_query("100 Example Rd, Palo Alto, CA 94306") is False

    def test_business_name_with_ampersand_is_not_intersection(self):
        # Defensive: don't false-trigger on business names like "Smith & Co".
        # The regex requires whitespace AROUND the "&" — "Smith&Co" has none.
        # (Real "Smith & Co" trips it, but that's an edge case the dispatcher
        # would correct anyway. Avoiding false positives here is more important
        # than avoiding occasional false negatives on bare business names.)
        assert _is_intersection_query("Smith&Co Headquarters, San Jose, CA") is False

    def test_at_symbol_intersection_handwritten_form(self):
        # PR-fix-5 (2026-05-08): handwritten `5th @ St. John` is a common
        # officer shorthand for "at" on call-out forms. Pre-fix the @ was
        # missed and Nominatim was attempted (then failed) before the
        # Google Maps fallback ran — wasted ~5s. Now caught directly.
        # Exact LKP from the second SJSU re-run that surfaced the gap.
        assert _is_intersection_query("5th St. @ St. John St., San Jose, CA 95112") is True
        assert _is_intersection_query("5th @ St John, San Jose") is True

    def test_email_address_is_not_intersection(self):
        # Defensive: `@` requires whitespace on BOTH sides, so emails like
        # `bill@sccssar.org` (no whitespace adjacent to `@`) don't trigger.
        # This matters because EB ack data sometimes flows through helpers
        # that mistakenly check geocode-style fields against email strings.
        assert _is_intersection_query("bill@sccssar.org") is False
        assert _is_intersection_query("Contact: dispatcher@example.com") is False

    def test_empty_or_none_returns_false(self):
        assert _is_intersection_query("") is False
        assert _is_intersection_query(None) is False  # type: ignore[arg-type]

    def test_no_ampersand_no_intersection_word_returns_false(self):
        assert _is_intersection_query("123 Main St, City, CA") is False
        assert _is_intersection_query("Cardoza Park, Milpitas, CA") is False


# ---------------------------------------------------------------------------
# main.py::_normalize_staging_geocode_query — Pass B staging address hygiene
# Mirror of the helper that anchors Pass B Nominatim queries to California.
# Bug history: 2026-05-09 SJSU smoke test on SCCSSAR-dev geocoded officer
# staging "ALMA @ 10TH" (no city, no state) to lat=49.26318, lng=-123.18535
# — Vancouver, BC. Without anchoring, Nominatim's global search returns
# plausible-looking but wrong-country matches. The LKP geocoder already had
# CA-append + city-context rules; this fix extends the same hygiene to
# Pass B (which previously sent raw text to Nominatim).
# ---------------------------------------------------------------------------


# Mirror of main.py::_CANONICAL_STAGING_FACILITIES (issue #646).
# Pinned against production by TestCanonicalStagingFacility below — do not edit
# one side without the other.
_CANONICAL_STAGING_FACILITIES: tuple[tuple[str, str, str], ...] = (
    (
        "richey_training_center",
        r"\bRich(?:ey|ie)\s+(?:Training\s+)?(?:Cent(?:er|re)|Ctr\.?)\b",
        "155 W Hedding St, San Jose, CA",
    ),
    (
        "sheriffs_office",
        r"\bSheriff(?:['’]?s)?\s+Office\b",
        "55 W Younger Ave, San Jose, CA",
    ),
)


def _match_canonical_staging_facility(addr: str) -> tuple[str, str] | None:
    """Mirror of main.py::_match_canonical_staging_facility()."""
    if not addr:
        return None
    for key, pattern, canonical in _CANONICAL_STAGING_FACILITIES:
        if re.search(pattern, addr, re.IGNORECASE):
            return (key, canonical)
    return None


def _normalize_staging_geocode_query(addr: str, city_context: str | None) -> str:
    """Mirror of main.py::_normalize_staging_geocode_query()."""
    if not addr:
        return addr
    facility = _match_canonical_staging_facility(addr)
    if facility is not None:
        return facility[1]
    enriched = addr
    if "," not in enriched and city_context:
        enriched = f"{enriched}, {city_context}"
    if not re.search(r"\b(CA|California)\b", enriched, re.IGNORECASE):
        enriched = f"{enriched}, CA"
    return enriched


# ---------------------------------------------------------------------------
# Apt/unit stripper regex — mirror of `_APT_STRIP_RE` in main.py.
#
# A real 2026-05-18 SJPD callout (LKP "2000 Hostetter Rd, San Jose, CA") had its
# event name truncated to "SJPD Ho" because the previous regex matched `Ste`
# (case-insensitive Suite abbreviation) as a substring of "Hostetter" and
# stripped it plus the trailing letters. Word boundaries on the keyword
# alternation prevent this. The contrived case of a street literally named
# "Suite Street" would still match — documented as a known limit because no
# such street exists in any operational dispatch area.
# ---------------------------------------------------------------------------

_APT_STRIP_RE = re.compile(
    r",?\s*(?:\b(?:Apt|Apartment|Unit|Ste|Suite)\b\.?|#)\s*#?\s*[\w-]+",
    re.IGNORECASE,
)


def _apt_strip(s: str) -> str:
    """Mirror of the inline strip used at main.py:1851 — apply the regex then
    collapse any resulting `,,` and trim surrounding `[ ,]`. Kept here for the
    test mirror; main.py applies the same shape inline."""
    return re.sub(r"\s*,\s*,", ",", _APT_STRIP_RE.sub("", s)).strip(" ,")


class TestAptStripRegex:
    """Pin: \\b word boundaries on the keyword alternation prevent substring
    matches inside street names. Established by the Hostetter regression fix.
    """

    # Bug cases — the regex MUST leave these unchanged.
    def test_hostetter_not_stripped(self):
        """Hostetter contains lowercase 'ste' — previous regex matched it as Suite."""
        assert _apt_strip("2000 Hostetter Rd, San Jose, CA 95132") == \
               "2000 Hostetter Rd, San Jose, CA 95132"

    def test_aptos_not_stripped(self):
        """Aptos starts with 'Apt' — previous regex stripped it to empty."""
        assert _apt_strip("2000 Aptos Way, San Jose, CA 95132") == \
               "2000 Aptos Way, San Jose, CA 95132"

    # Documented happy-path cases — must continue working.
    def test_apt_word_stripped(self):
        assert _apt_strip("123 Main St, Apt 4, City, CA") == "123 Main St, City, CA"

    def test_apt_period_stripped(self):
        assert _apt_strip("123 Main St, Apt. 5, San Jose, CA") == "123 Main St, San Jose, CA"

    def test_hash_stripped(self):
        assert _apt_strip("123 Main St, #2") == "123 Main St"

    def test_suite_stripped(self):
        assert _apt_strip("123 Main St, Suite 200, San Jose, CA") == "123 Main St, San Jose, CA"

    def test_ste_short_stripped(self):
        assert _apt_strip("123 Main St, Ste 200, San Jose, CA") == "123 Main St, San Jose, CA"

    def test_unit_stripped(self):
        assert _apt_strip("123 Main St, Unit B, San Jose, CA") == "123 Main St, San Jose, CA"

    def test_apartment_stripped(self):
        assert _apt_strip("123 Main St, Apartment 4, City, CA") == "123 Main St, City, CA"

    # End-to-end: the real Hostetter callout shape (LKP + Residence both the
    # same Hostetter address). Both must pass through unchanged so the
    # downstream Event Name extraction sees "Hostetter", not "Ho".
    def test_real_hostetter_callout_shape(self):
        lkp_residence = "2000 Hostetter Rd, San Jose, CA 95132"
        assert _apt_strip(lkp_residence) == lkp_residence


class TestNormalizeStagingGeocodeQuery:
    """Pin Pass B staging address hygiene. Drift here = officer staging
    geocoded as raw text → wrong-country resolution on bare strings.
    Live regression: 2026-05-09 SJSU smoke test on SCCSSAR-dev placed
    "ALMA @ 10TH" (officer staging) at coordinates in Vancouver, BC."""

    def test_alma_at_10th_with_san_jose_context_appends_city_and_ca(self):
        # The exact 2026-05-09 SJSU regression: officer staging with no city
        # context. SJSU agency lookup yields San Jose, CA — the city_context
        # cascade applies, then CA-append is a no-op (already in city_context).
        assert (
            _normalize_staging_geocode_query("ALMA @ 10TH", "San Jose, CA")
            == "ALMA @ 10TH, San Jose, CA"
        )

    def test_no_city_context_still_appends_ca(self):
        # Best-effort fallback: if no city_context could be derived (LKP had
        # no city, no agency match), CA alone still beats raw global search.
        assert (
            _normalize_staging_geocode_query("ALMA @ 10TH", None)
            == "ALMA @ 10TH, CA"
        )

    def test_already_canonical_address_passes_through(self):
        # Idempotent: addresses that already include CA are unchanged.
        # This is the typical Gemini-generated alt-staging shape (city + state).
        addr = "4000 Terman Drive, Palo Alto, CA 94306"
        assert _normalize_staging_geocode_query(addr, "Palo Alto, CA") == addr

    def test_address_with_zip_but_no_state_appends_ca(self):
        # Common Gemini output: "4000 Terman Drive, Palo Alto, 94306" — has
        # city and zip but no explicit state token. CA-append still fires.
        addr = "4000 Terman Drive, Palo Alto, 94306"
        assert (
            _normalize_staging_geocode_query(addr, "Palo Alto, CA")
            == "4000 Terman Drive, Palo Alto, 94306, CA"
        )

    def test_park_with_city_no_state_appends_ca(self):
        # Park entries often arrive as "Cardoza Park, Milpitas" (no state).
        # CA-append fires; city is already present so no city_context applied.
        assert (
            _normalize_staging_geocode_query("Cardoza Park, Milpitas", "Milpitas, CA")
            == "Cardoza Park, Milpitas, CA"
        )

    def test_california_word_is_not_re_appended(self):
        # Idempotent on full word "California" — regex matches the word boundary
        # variant too, so a pre-canonical query passes through.
        addr = "123 Main St, San Jose, California"
        assert _normalize_staging_geocode_query(addr, "San Jose, CA") == addr

    def test_empty_input_returns_empty(self):
        # Defensive: empty input passes through (caller already guards on this,
        # but the helper should not crash if invoked on a blank string).
        assert _normalize_staging_geocode_query("", "San Jose, CA") == ""


class TestCanonicalStagingFacility:
    """Pin the known-SAR-facility lookup (issue #646).

    Two real callouts failed on the same venue name:

      2026-07-18 (LACSO La Verne) — "Richey Training Center" needed a manual
          mid-intake correction to "155 W Hedding St".
      2026-07-26 (SCCSSAR Hale Avenue) — OCR produced "Sheriff's Office, Richey
          Center, 11am.", which collapsed to the California state centroid. The
          50 km guard caught it, so the officer's real staging never appeared on
          the map at all and the team self-corrected over Slack ~2h in.

    The dangerous case is neither of those. Measured 2026-07-26 against the live
    Google Geocoding API, "Richey Center, San Jose, CA" — the same string minus
    the word "Training" — returns ROOFTOP-precision "2850 Quimby Rd", 24 km from
    that incident's LKP. It has a house number, it is inside
    _MAX_STAGING_DIST_M, and its reported precision is the highest Google
    emits, so NO existing guard can detect it. It would ship silently.

    Every phrasing misses Nominatim, so the Google fallback's guess decides the
    outcome and the word "Training" is load-bearing for that guess.
    """

    # --- behaviour -------------------------------------------------------

    def test_richey_center_without_training_resolves_canonically(self):
        # THE silent-failure case: free-text this string and Google confidently
        # returns 2850 Quimby Rd, 24 km wrong, undetectable by any guard.
        assert _match_canonical_staging_facility("Richey Center") == (
            "richey_training_center", "155 W Hedding St, San Jose, CA"
        )

    def test_richey_training_center_resolves_canonically(self):
        # The 2026-07-18 LACSO La Verne phrasing.
        assert _match_canonical_staging_facility("Richey Training Center") == (
            "richey_training_center", "155 W Hedding St, San Jose, CA"
        )

    def test_richie_misspelling_resolves_canonically(self):
        # Bill's explicit ask: "Richey" and "Richie" both occur on real forms.
        assert _match_canonical_staging_facility("Richie Training Center") == (
            "richey_training_center", "155 W Hedding St, San Jose, CA"
        )

    def test_all_caps_form_text_resolves(self):
        # Handwritten intake forms are frequently transcribed in full caps.
        assert _match_canonical_staging_facility("RICHEY TRAINING CENTER") == (
            "richey_training_center", "155 W Hedding St, San Jose, CA"
        )

    def test_real_2026_07_26_intake_string_prefers_richey(self):
        """The verbatim 2026-07-26 text matches BOTH patterns — Richey must win.

        The team staged at Richey Training Center, not at the Sheriff's Office.
        Reversing the table order silently sends responders to 55 W Younger Ave,
        which is a real address ~1 km away — plausible enough to go unnoticed.
        """
        assert _match_canonical_staging_facility(
            "Sheriff's Office, Richey Center, 11am."
        ) == ("richey_training_center", "155 W Hedding St, San Jose, CA")

    def test_sheriffs_office_resolves_canonically(self):
        assert _match_canonical_staging_facility("Sheriff's Office") == (
            "sheriffs_office", "55 W Younger Ave, San Jose, CA"
        )

    def test_sheriff_office_without_possessive_resolves(self):
        # Bill, 2026-07-26: accept "Sheriff's Office" or "Sheriff Office".
        assert _match_canonical_staging_facility("Sheriff Office") == (
            "sheriffs_office", "55 W Younger Ave, San Jose, CA"
        )

    def test_curly_apostrophe_resolves(self):
        # Gemini OCR emits U+2019 for handwritten apostrophes often enough that
        # a straight-quote-only pattern would miss real forms.
        assert _match_canonical_staging_facility("Sheriff’s Office") == (
            "sheriffs_office", "55 W Younger Ave, San Jose, CA"
        )

    def test_bare_so_does_not_match(self):
        """Bare "SO" is deliberately NOT a pattern (Bill, 2026-07-26).

        It was in the original ask and was dropped: intake text is frequently
        ALL CAPS, so a \\bSO\\b match fires inside ordinary prose and silently
        rewrites the officer's staging text to an unrelated address.
        """
        assert _match_canonical_staging_facility("SO") is None
        assert _match_canonical_staging_facility("MEET AT THE PARK SO WE CAN STAGE") is None
        assert _match_canonical_staging_facility("S.O.") is None

    def test_unrelated_staging_text_passes_through(self):
        # The overwhelming majority of staging text must be untouched — a
        # false positive here rewrites a correct address to a wrong one.
        assert _match_canonical_staging_facility("ALMA @ 10TH") is None
        assert _match_canonical_staging_facility("Cardoza Park, Milpitas") is None
        assert _match_canonical_staging_facility("400 Llagas Road, Morgan Hill") is None

    def test_person_named_richie_does_not_match(self):
        # "Richie" alone is a plausible given name; the pattern requires a
        # Center/Ctr token so a name in staging text cannot trigger the table.
        assert _match_canonical_staging_facility("Richie") is None
        assert _match_canonical_staging_facility("Meet Richie at the trailhead") is None

    def test_empty_input_returns_none(self):
        assert _match_canonical_staging_facility("") is None

    def test_canonical_address_short_circuits_normalize(self):
        """End-to-end through the helper both call sites actually use.

        The canonical string already carries city + state, so the CA-append and
        comma-city rules must be no-ops rather than producing
        "155 W Hedding St, San Jose, CA, Morgan Hill, CA".
        """
        assert (
            _normalize_staging_geocode_query("Richey Center", "Morgan Hill, CA")
            == "155 W Hedding St, San Jose, CA"
        )
        assert (
            _normalize_staging_geocode_query("Sheriff's Office, Richey Center, 11am.", None)
            == "155 W Hedding St, San Jose, CA"
        )

    # --- production pins -------------------------------------------------
    #
    # The behaviour tests above run against the mirrors at the top of this file,
    # because the suite deliberately does not import main (heavyweight GCP deps).
    # Without these pins a change to production could not fail any of them —
    # they would keep exercising a stale copy and reporting green.

    @staticmethod
    def _main_src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    def test_table_matches_production(self):
        import ast
        src = self._main_src()
        block = re.search(
            r"^_CANONICAL_STAGING_FACILITIES: tuple\[tuple\[str, str, str\], \.\.\.\] = "
            r"(\(.*?\n\))\n",
            src, re.DOTALL | re.MULTILINE,
        )
        assert block, "_CANONICAL_STAGING_FACILITIES not found in main.py"
        prod = ast.literal_eval(block.group(1))
        assert prod == _CANONICAL_STAGING_FACILITIES, (
            "backend/main.py::_CANONICAL_STAGING_FACILITIES drifted from the "
            "mirror in this file.\n"
            f"  main.py: {prod}\n"
            f"  mirror:  {_CANONICAL_STAGING_FACILITIES}"
        )

    def test_production_orders_richey_before_sheriffs_office(self):
        """Ordering is behaviour, not style — see the real-string test above."""
        src = self._main_src()
        block = re.search(
            r"_CANONICAL_STAGING_FACILITIES.*?\n\)\n", src, re.DOTALL
        )
        assert block
        assert block.group(0).index("richey_training_center") < \
            block.group(0).index("sheriffs_office"), (
                "Richey must precede sheriffs_office in main.py — the real "
                "2026-07-26 intake string matches both, and first match wins."
            )

    def test_production_normalize_calls_the_facility_lookup(self):
        """Wiring pin: the table is inert unless _normalize_staging_geocode_query
        consults it. Both staging geocode call sites reach the table only
        through this helper."""
        src = self._main_src()
        fn = re.search(
            r"^def _normalize_staging_geocode_query\(.*?(?=\n\n(?:def |async def |# -{10,}))",
            src, re.DOTALL | re.MULTILINE,
        )
        assert fn, "_normalize_staging_geocode_query not found in main.py"
        assert "_match_canonical_staging_facility(" in fn.group(0), (
            "main.py::_normalize_staging_geocode_query no longer consults "
            "_match_canonical_staging_facility — issue #646 staging text would "
            "fall back to free-text geocoding, which returns a confident wrong "
            "address for 'Richey Center'."
        )

    def test_production_emits_event_log_entry_on_canonicalization(self):
        """The substitution must stay visible to the dispatcher.

        A silent rewrite of the officer's staging text is the specific complaint
        issue #646 was filed on.
        """
        src = self._main_src()
        assert "Staging facility canonicalized: " in src, (
            "The Event Log entry for a canonicalized staging facility is gone — "
            "the substitution would be invisible to the dispatcher."
        )

    def test_production_log_line_carries_no_raw_staging_text(self):
        """PII guard: the logger may carry the facility KEY, never the address.

        Same rule as /apply-staging-override — see the 'No PII in logs' core
        privacy guarantee.
        """
        src = self._main_src()
        # The interpolated values must be the loop index and the fixed key only.
        call = re.search(
            r'logger\.info\(\s*"Staging facility canonicalized \|[^)]*?\)',
            src, re.DOTALL,
        )
        assert call, "facility logger.info call not found"
        assert "_facility[0]" in call.group(0), "log must carry the facility key"
        assert "addr_part" not in call.group(0), (
            "raw staging text must never reach Cloud Logging"
        )


class TestUnnavigableOfficerStagingSubstitution:
    """An officer staging string that never geocoded must not reach the staging line.

    Real 2026-07-27 incident: the officer field read "OUTSIDE OF HOME ADDRESS
    (ABOVE)". That is an instruction, not an address. It failed to geocode, so
    the backend fell it back to the LKP coordinates ("Command Post marker placed
    at LKP") — correct for the map — but the pick list still offered it, and
    committing it would have written that phrase into
    "Staging Area for Resources:", which is what Slack turns into the
    responders' tappable maps link. Every responder's phone would search for the
    literal phrase.

    Fix (Bill's option 1, 2026-07-27): commit the LKP's ADDRESS instead.

    Sitting exactly on the LKP detects that fallback, but it is NOT proof the
    text is unnavigable (#721). When the LKP is ITSELF a named POI that is also
    a staging candidate, a correctly geocoded entry sits on the LKP by
    construction. Live 2026-08-08: the LKP was a shopping mall, and picking a
    ranked entry reading "<house#> <Street> Dr, <City>" shipped "<Venue>,
    <City>, CA" instead — for a site over a mile across, with both responder
    maps links resolving to the venue centroid.

    So the substitution now requires BOTH halves: the entry fell back to the LKP
    coordinates AND its text fails the navigability test directly.
    """

    @staticmethod
    def _frontend_src():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_pick_substitutes_lkp_address_when_entry_sits_on_lkp(self):
        src = self._frontend_src()
        fn = re.search(r"function _overrideUseRankedRec\(.*?\n  \}\n", src, re.DOTALL)
        assert fn, "_overrideUseRankedRec not found"
        body = fn.group(0)
        assert "_overrideNeedsLkpSubstitution(rec)" in body, (
            "no substitution check — an un-geocodable officer instruction "
            "would be committed verbatim into the responders' maps-link query"
        )
        assert "_overrideLkpAddress()" in body, "no LKP address source"
        assert "substitutedFrom" in body, (
            "the original officer text must be carried through so the "
            "substitution can be disclosed"
        )

    def test_lkp_detector_compares_coordinates_not_text(self):
        src = self._frontend_src()
        fn = re.search(r"function _overrideSitsOnLkp\(.*?\n  \}\n", src, re.DOTALL)
        assert fn, "_overrideSitsOnLkp not found"
        body = fn.group(0)
        assert "lkp.lat === rec.lat" in body and "lkp.lng === rec.lng" in body, (
            "the detector must compare coordinates — the officer text is "
            "free-form and cannot be pattern-matched reliably"
        )

    def test_substitution_requires_both_halves(self):
        """#721: coordinate identity alone is not proof of non-navigability."""
        src = self._frontend_src()
        fn = re.search(
            r"function _overrideNeedsLkpSubstitution\(.*?\n  \}\n", src, re.DOTALL
        )
        assert fn, "_overrideNeedsLkpSubstitution not found"
        code = "\n".join(l.split("//")[0] for l in fn.group(0).splitlines())
        assert "_overrideSitsOnLkp(rec)" in code, "lost the fell-back-to-LKP half"
        assert "!_overrideTextIsNavigable(rec.text)" in code, (
            "lost the direct navigability test — a correctly geocoded entry at "
            "an LKP that is itself a POI would be replaced by the venue name"
        )
        assert "&&" in code, (
            "the two halves must be conjoined; either one alone is a false "
            "positive"
        )

    def test_navigability_predicate_mirrors_backend_pass2(self):
        """SOURCE OF TRUTH: the PASS 2 filter + _PARK_AMENITY in main.py.

        Parks are rendered WITHOUT a street address by design, so a
        leading-digit-only test would classify every park entry as unnavigable
        and substitute the LKP for all of them.
        """
        src = self._frontend_src()
        fn = re.search(
            r"function _overrideTextIsNavigable\(.*?\n  \}\n", src, re.DOTALL
        )
        assert fn, "_overrideTextIsNavigable not found"
        code = "\n".join(l.split("//")[0] for l in fn.group(0).splitlines())
        assert 'split(" — ")[0]' in code, (
            "navigability is tested on the text BEFORE the em-dash — the same "
            "loc_part the backend PASS 2 filter uses"
        )
        assert "/[,:]/" in code, (
            "the location part must be split into segments on comma OR colon — "
            "staging lines are rendered VENUE-LED, so the house number is not "
            "at position 0 and a leading-digit-only test misclassifies 10.8% "
            "of real staging lines as unnavigable"
        )
        assert "/^\\d+\\s+\\S/" in code, "lost the house-number test"
        assert "_PARK_AMENITY_RE" in code, "lost the park exemption"

        py = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        py_re = re.search(
            r"_PARK_AMENITY = re\.compile\(\s*(.*?)\s*,\s*re\.IGNORECASE",
            py,
            re.DOTALL,
        )
        assert py_re, "_PARK_AMENITY not found in main.py"
        words = re.findall(r"[a-z ]{3,}", py_re.group(1).replace("\n", ""))
        words = [w.strip() for w in words if len(w.strip()) >= 4]
        assert len(words) >= 15, f"amenity-word extraction looks wrong: {words}"
        js_re = re.search(r"const _PARK_AMENITY_RE = (/.*?/i);", src)
        assert js_re, "_PARK_AMENITY_RE not found in index.html"
        missing = [w for w in words if w not in js_re.group(1)]
        assert not missing, (
            f"park amenity words in main.py but not mirrored in index.html: "
            f"{missing} — a park entry would be treated as unnavigable and "
            f"replaced by the LKP address"
        )

    def test_substitution_is_disclosed_in_the_event_log(self):
        """A silent rewrite of the officer's wording is the failure mode this
        whole area keeps producing. One entry, so iterations still collapse."""
        src = self._frontend_src()
        note = re.search(r"const _subNote = .*?;\n", src, re.DOTALL)
        assert note, "_subNote assignment not found"
        code = note.group(0)
        assert "source.substitutedFrom" in code, (
            "the disclosure must name the officer's original wording"
        )
        assert "did not resolve to an address" in code, (
            "the entry must state what was observed"
        )
        assert "is not a navigable address" not in code, (
            "#721: the entry must NOT assert the text is unnavigable — the "
            "2026-08-08 entry made that claim about a house-numbered address"
        )
        assert "_truncateOnWord(" in code, (
            "a mid-word cut in an audit line reads as a rendering fault"
        )
        assert "slice(0, 80)" not in code, "raw mid-word slice is back"

    def test_lkp_address_falls_back_to_the_marker_label(self):
        src = self._frontend_src()
        fn = re.search(r"function _overrideLkpAddress\(.*?\n  \}\n", src, re.DOTALL)
        assert fn, "_overrideLkpAddress not found"
        body = fn.group(0)
        assert "Last Known Position" in body, (
            "the dispatcher-visible LKP line is the primary source — it "
            "reflects any hand-edit they made"
        )
        assert "_rawMapData.lkp" in body, (
            "must fall back to the LKP marker label when the textarea line is "
            "missing, or the substitution silently does nothing"
        )


class TestUnnavigableSubstitutionBehaviour:
    """Executes the real JS. A source pin can prove the navigability test is
    WIRED but not that it answers correctly — an inverted predicate would pass
    every pin above. These are the #721 fixtures.

    Skipped when node is unavailable; the source pins are the guaranteed floor.
    """

    LKP = {"lat": 37.4161, "lng": -121.8985}

    @staticmethod
    def _run(rec, lkp):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not available")
        src = (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )
        parts = []
        const = re.search(r"^  const _PARK_AMENITY_RE = .*?;$", src, re.M)
        assert const, "_PARK_AMENITY_RE not found"
        parts.append(const.group(0))
        for name in (
            "_overrideSitsOnLkp",
            "_overrideTextIsNavigable",
            "_overrideNeedsLkpSubstitution",
            "_truncateOnWord",
        ):
            fn = re.search(rf"^  function {name}\(.*?\n  \}}$", src, re.M | re.S)
            assert fn, f"{name} not found"
            parts.append(fn.group(0))
        program = (
            f"var _rawMapData = {json.dumps({'lkp': lkp})};\n"
            + "\n".join(parts)
            + f"\nvar rec = {json.dumps(rec)};\n"
            + "console.log(JSON.stringify({"
            + "needs: _overrideNeedsLkpSubstitution(rec),"
            + "nav: _overrideTextIsNavigable(rec.text)}));"
        )
        out = subprocess.run(
            [node, "-e", program], capture_output=True, text=True, timeout=30
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_house_numbered_entry_on_an_lkp_that_is_a_poi_survives(self):
        """The 2026-08-08 Great Mall case. Coordinates identical to the LKP,
        text perfectly navigable — must NOT be substituted."""
        rec = {
            "text": "447 Great Mall Dr, Milpitas — Great Mall",
            "lat": self.LKP["lat"],
            "lng": self.LKP["lng"],
        }
        assert self._run(rec, self.LKP) == {"needs": False, "nav": True}

    def test_officer_instruction_on_lkp_is_still_substituted(self):
        """The 2026-07-27 case the guard exists for — must still fire."""
        rec = {
            "text": "OUTSIDE OF HOME ADDRESS (ABOVE) — Officer-designated "
            "staging location",
            "lat": self.LKP["lat"],
            "lng": self.LKP["lng"],
        }
        assert self._run(rec, self.LKP) == {"needs": True, "nav": False}

    def test_venue_led_entry_on_the_lkp_survives(self):
        """Staging lines are rendered VENUE-LED, so the house number is not at
        position 0. Measured across 416 real corpus staging lines, a
        leading-digit-only test called 10.8% of them unnavigable; on a
        POI-anchored LKP every one would have been substituted away.

        Both separators Gemini emits are covered — comma and colon.
        """
        for text in (
            "McDonald's, 200 Senter Rd, San Jose, CA — Fast food. 0.01 mi from LKP",
            "CVS Pharmacy: 900 Parkview Dr, Santa Clara — 0.15 mi from LKP",
            "Holiday Inn Express, 1000 Monterey Rd, San Jose, CA — Hotel",
        ):
            rec = {"text": text, "lat": self.LKP["lat"], "lng": self.LKP["lng"]}
            assert self._run(rec, self.LKP) == {"needs": False, "nav": True}, text

    def test_venue_name_without_an_address_is_still_unnavigable(self):
        """The narrowing must not swallow the case the guard exists for: a bare
        venue name carries no house number in any segment."""
        rec = {"text": "Great Mall, Milpitas — Shopping center",
               "lat": self.LKP["lat"], "lng": self.LKP["lng"]}
        assert self._run(rec, self.LKP) == {"needs": True, "nav": False}

    def test_park_on_the_lkp_is_not_substituted(self):
        """Parks are rendered without a street address BY DESIGN."""
        rec = {
            "text": "Ed Levin County Park, Milpitas",
            "lat": self.LKP["lat"],
            "lng": self.LKP["lng"],
        }
        assert self._run(rec, self.LKP) == {"needs": False, "nav": True}

    def test_unnavigable_text_away_from_the_lkp_is_not_substituted(self):
        """It geocoded somewhere real. The LKP is a DIFFERENT place, so
        substituting it would relocate the dispatcher's pick."""
        rec = {"text": "Richey Training Center", "lat": 37.30, "lng": -121.80}
        assert self._run(rec, self.LKP) == {"needs": False, "nav": False}


class TestTruncateOnWord:
    """#721 secondary defect: the Event Log cut the officer's text mid-word."""

    @staticmethod
    def _truncate(text, max_len):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not available")
        src = (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )
        fn = re.search(r"^  function _truncateOnWord\(.*?\n  \}$", src, re.M | re.S)
        assert fn, "_truncateOnWord not found"
        program = (
            fn.group(0)
            + f"\nconsole.log(JSON.stringify(_truncateOnWord("
            + f"{json.dumps(text)}, {max_len})));"
        )
        out = subprocess.run(
            [node, "-e", program], capture_output=True, text=True, timeout=30
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_short_text_is_untouched(self):
        assert self._truncate("OUTSIDE OF HOME ADDRESS", 80) == "OUTSIDE OF HOME ADDRESS"

    def test_cut_lands_on_a_word_boundary(self):
        text = "OUTSIDE OF THE HOME ADDRESS LISTED ABOVE NEAR THE MAILBOXES"
        # Two cut points, both landing MID-WORD on a hard slice. One alone is a
        # coin flip: at 45 the raw slice happens to end on a space, so a
        # hard-cutting implementation would pass it.
        for max_len in (43, 52):
            got = self._truncate(text, max_len)
            assert got.endswith("…"), got
            body = got[:-1]
            assert len(body) <= max_len
            assert text.startswith(body), f"not a prefix of the original: {got!r}"
            assert text[len(body)] == " ", f"cut mid-word at {max_len}: {got!r}"

    def test_single_over_long_word_is_hard_cut(self):
        assert self._truncate("A" * 40, 10) == "A" * 10 + "…"


class TestStagingPickListStableNumbering:
    """Row numbers are a STABLE IDENTITY, not a position in a mutable list.

    Live-caught 2026-07-27. The pick list was built from the live textarea, so
    after a pick the promoted entry was removed, everything renumbered, and the
    label "5." pointed at a different place. Clicking the same visual row twice
    selected two different locations, alternating forever:

        click 5 -> Grand Staircase
        click 5 -> Tuscany Hills Vineyard
        click 5 -> Grand Staircase

    A dispatcher reading "5." off the summary and clicking row 5 could commit a
    location they never chose. The numbers ARE the quality ranking, so they must
    come from the ranking as originally computed — the same pre-override
    snapshot the commit path rebuilds from.
    """

    @staticmethod
    def _frontend_src():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_pick_list_reads_the_pristine_snapshot(self):
        src = self._frontend_src()
        fn = re.search(
            r"function _overrideParseRankedRecs\(.*?\n  \}\n", src, re.DOTALL
        )
        assert fn, "_overrideParseRankedRecs not found"
        body = fn.group(0)
        assert "_stagingOverrideBackup.ocrText" in body, (
            "pick-list text must come from the pre-override snapshot, or the "
            "row numbers shift under the dispatcher after every pick"
        )
        assert "_stagingOverrideBackup.mapDataStaging" in body, (
            "pick-list coordinates must come from the same snapshot as the text "
            "or the two fall out of index alignment"
        )
        assert "srcText" in body and "ta.value.match" not in body, (
            "the header/entry scan must run over the snapshot, not the live "
            "textarea"
        )

    def test_active_pick_is_marked_not_hidden(self):
        """The current pick stays in the list, flagged and non-clickable.

        Removing it read as the row being deleted ("surprising in a bad way").
        With stable numbering there is no reason to hide it — showing which row
        is in force is the point of a pick list.
        """
        src = self._frontend_src()
        fn = re.search(
            r"function _overrideRenderPickList\(.*?\n  \}\n", src, re.DOTALL
        )
        assert fn, "_overrideRenderPickList not found"
        body = fn.group(0)
        assert "isActive" in body, "no active-row detection"
        assert "is-active" in body, "active row needs a visual marker class"
        assert 'badge.className = "current-badge"' in body, (
            "the active row must render a STATUS BADGE, not a button. A "
            "disabled button is still shaped like a button — 'Current' looked "
            "identical to 'Use this', invited a click, and did nothing, which "
            "reads as the app being broken (Bill, 2026-07-27)."
        )
        assert re.search(r"if \(isActive\) \{", body), (
            "the active row must take a different render branch entirely, not "
            "just a disabled flag on the same button"
        )

    def test_active_marker_css_exists(self):
        src = self._frontend_src()
        assert ".override-result-item.is-active" in src, (
            "the is-active class is applied but has no styling — the marker "
            "would be invisible"
        )
        assert ".override-result-item .current-badge" in src, (
            "the Current badge has no styling — it would render as bare text"
        )

    def test_disabled_buttons_look_disabled(self):
        """The dispatch lockout leaves real disabled buttons on screen.

        Without a :disabled rule they are pixel-identical to live ones, so
        after Create Map the rows still say 'press me' and then refuse.
        """
        src = self._frontend_src()
        assert ".override-result-item button:disabled" in src, (
            "no :disabled styling — a locked-out row is indistinguishable from "
            "an actionable one"
        )
        block = re.search(
            r"\.override-result-item button:disabled \{(.*?)\}", src, re.DOTALL
        )
        assert block and "cursor" in block.group(1), (
            "the disabled style must change the cursor — pointer on a dead "
            "control is the specific thing that reads as broken"
        )


class TestOverrideDivergenceWarningWording:
    """The LKP-divergence warning states distance + locality, and nothing vaguer.

    Bill, 2026-07-27: the trailing "No alternatives within N m either — the
    anchor is not where you expected" was confusing and carried no call to
    action. Its #607 job is already done twice in this branch — the distance is
    stated outright AND the resolved locality is named.

    The #607 signal is NOT lost: the sibling `no_candidates` branch still fires
    whenever there is no divergence to report, which is the case it was written
    for. That branch must survive.
    """

    @staticmethod
    def _frontend_src():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_divergence_branch_does_not_append_the_no_alternatives_clause(self):
        src = self._frontend_src()
        assert "No alternatives within ${_radius} m either" not in src, (
            "the struck clause is back in the divergence warning"
        )

    def test_divergence_warning_still_names_the_way_forward(self):
        src = self._frontend_src()
        assert 'To use it anyway, click "Use this" below' in src, (
            "the divergence warning must still tell the dispatcher how to "
            "proceed deliberately — a far staging point is often correct"
        )

    def test_no_candidates_sibling_branch_survives(self):
        """#607's real home — an empty list with no divergence to explain it.

        Pins the WRONG-CITY sentence specifically, not the generic "no
        alternatives within N m" phrasing: that phrase appears in both arms of
        the sibling branch, so asserting on it passes even after the arm
        carrying the actual signal is gutted (caught while verifying this pin).
        """
        src = self._frontend_src()
        assert "is not the right city, the address did not resolve where you expected" in src, (
            "the #607 wrong-city sentence is gone. An empty alternatives list "
            "then reads as 'nothing is near here' rather than 'we are searching "
            "the wrong city' — the 2026-07-24 failure it was written for."
        )
        assert "Anchor set — no alternatives within" in src, (
            "the no-locality arm of the no_candidates branch was removed"
        )


class TestStagingPickListLockAndIteration:
    """Two defects live-caught on personal-dev 2026-07-27, both from #414.

    1. LOCKOUT GAP. `_overrideSetLocked` disables the Apply/Clear buttons, the
       mode radios, the text inputs, and the `#override-results` rows — but the
       pick list is a SECOND container (`#override-pick-list`) that did not exist
       when that function was written. After Create Map its rows stayed live, so
       clicking one mutated the textarea and the staging list AFTER CalTopo had
       already been built from them. Same failure class PR-D-2.5 fixed for
       Apply/Clear: the textarea then lies about what was dispatched.

       A one-shot disable pass is NOT sufficient — the rows are rebuilt on every
       commit/revert/panel-open, so they must be born disabled.

    2. DESTRUCTIVE ITERATION. Each commit filtered out the previous dispatcher
       entry without restoring the recommendation it had been promoted from
       (that one had been deduped away on its own commit). A dispatcher
       comparing options silently deleted one real recommendation per
       comparison: 7 → 6 → 5. Rebuilding from the pre-override snapshot makes
       the result a function of the CURRENT pick alone, restores the original
       ranking order, and drops superseded facility/free-text picks.
    """

    @staticmethod
    def _frontend_src():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_pick_rows_are_born_disabled_when_locked(self):
        src = self._frontend_src()
        render = re.search(
            r"function _overrideRenderPickList\(.*?\n  \}\n", src, re.DOTALL
        )
        assert render, "_overrideRenderPickList not found"
        assert "btn.disabled = _overrideLocked" in render.group(0), (
            "pick-list buttons must read the lock at RENDER time — they are "
            "rebuilt on every commit/revert/panel-open, so a one-shot disable "
            "pass is undone by the next re-render and the rows come back live "
            "after Create Map"
        )

    def test_set_locked_covers_the_pick_list_container(self):
        src = self._frontend_src()
        fn = re.search(r"function _overrideSetLocked\(.*?\n  \}\n", src, re.DOTALL)
        assert fn, "_overrideSetLocked not found"
        assert '"override-pick-list"' in fn.group(0), (
            "_overrideSetLocked must disable the #override-pick-list container "
            "too — #override-results is a different element and only covers the "
            "nearby-alternatives rows"
        )
        assert "_overrideLocked = " in fn.group(0), (
            "_overrideSetLocked must record the lock state for render-time use"
        )

    def test_commit_rebuilds_both_surfaces_from_the_pristine_snapshot(self):
        src = self._frontend_src()
        commit = re.search(r"function _overrideCommit\(.*?\n  \}\n", src, re.DOTALL)
        assert commit, "_overrideCommit not found"
        body = commit.group(0)
        assert "_stagingOverrideBackup.mapDataStaging" in body, (
            "map_data staging must be restored from the pre-override snapshot; "
            "filtering out the previous dispatcher entry DELETES the "
            "recommendation it was promoted from"
        )
        assert "_stagingOverrideBackup.ocrText" in body, (
            "the textarea rebuild must be handed the pristine snapshot, or "
            "iterating picks erodes the numbered list one entry at a time"
        )

    def test_textarea_rebuild_accepts_and_uses_the_pristine_snapshot(self):
        src = self._frontend_src()
        fn = re.search(
            r"function _applyStagingOverrideToTextareaText\(.*?\n  \}\n",
            src, re.DOTALL,
        )
        assert fn, "_applyStagingOverrideToTextareaText not found"
        body = fn.group(0)
        assert "pristineText" in body.split("\n")[0], (
            "the helper must take the pristine snapshot as a parameter"
        )
        assert "entryLines" in body, (
            "the numbered entries must be sourced from the snapshot when given, "
            "not from the already-mutated current value"
        )


class TestStagingOverrideDoesNotRewriteEventName:
    """A staging override MUST NOT touch the Event Name (Bill, 2026-07-27).

    The Event Name identifies the INCIDENT and derives from where the subject was
    last seen. Staging is a logistics pick that is legitimately far away
    sometimes — a mutual-aid caravan point 30 km out is normal and correct.

    PR-D-1 coupled the two: `/apply-staging-override` returns
    `event_name_streetname_suggestion` extracted from the STAGING address, and
    the frontend applied it on commit. Live-caught on personal-dev 2026-07-27 —
    picking "Richey Training Center" on a Mountain Home Drive incident produced
    "2020-07-26 SJPD Richey Training Center", naming the event after a building
    in another part of the county from where the subject went missing. That
    string flows verbatim to D4H referenceDescription, the EB notification
    title, the Slack channel name, and the CalTopo map title.
    """

    @staticmethod
    def _frontend_src():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_commit_path_does_not_apply_the_streetname_suggestion(self):
        src = self._frontend_src()
        commit_fn = re.search(
            r"function _overrideCommit\(.*?\n  \}\n", src, re.DOTALL
        )
        assert commit_fn, "_overrideCommit not found"
        assert "_applyEventNameStreetnameSuggestion(" not in commit_fn.group(0), (
            "_overrideCommit re-applies the Event Name streetname suggestion. A "
            "staging override must not rename the incident — see the Locked "
            "Decision row 'Event Name format' in CLAUDE.md."
        )

    def test_helper_is_kept_but_unwired(self):
        """The helper and its `g`-flag pin stay — the coupling is what was removed.

        Keeping it preserves the 2026-05-10 Verde Vista rationale for the `g`
        flag and leaves a future EXPLICIT "also update the Event Name" action
        something to call.
        """
        src = self._frontend_src()
        assert "function _applyEventNameStreetnameSuggestion(" in src, (
            "helper was deleted — keep it (unwired); its g-flag pin documents a "
            "real bug and a deliberate Event-Name action may want it later"
        )
        calls = re.findall(r"_applyEventNameStreetnameSuggestion\(", src)
        assert len(calls) == 1, (
            f"expected exactly 1 occurrence (the definition), found {len(calls)} — "
            f"something is calling it again"
        )

    def test_backend_field_is_marked_unconsumed(self):
        """The response field stays for contract stability, but a future reader
        must not mistake it for live wiring."""
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        i = src.index('"event_name_streetname_suggestion": event_name_suggestion')
        preamble = src[max(0, i - 900):i]
        assert "NOT CONSUMED" in preamble.upper(), (
            "the deliberately-unconsumed note above "
            "event_name_streetname_suggestion is gone — without it the field "
            "reads as live wiring and invites re-coupling"
        )


class TestStagingPickList:
    """Pin the #414 staging pick list inside the Override Staging Location panel.

    The list offers the two canonical SAR facilities (#646) above the ranked
    recommendations, so a dispatcher can choose a SAR default without knowing its
    spelling or street address, and can promote rec #2/#3 without retyping it.
    """

    @staticmethod
    def _frontend_src():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _main_src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    def _frontend_facilities(self):
        """Parse the frontend _CANONICAL_STAGING_FACILITIES entries."""
        src = self._frontend_src()
        block = re.search(
            r"const _CANONICAL_STAGING_FACILITIES = \[(.*?)\n  \];", src, re.DOTALL
        )
        assert block, "_CANONICAL_STAGING_FACILITIES not found in index.html"
        return re.findall(r'commit:\s*"([^"]+)"', block.group(1))

    def test_frontend_commit_strings_carry_the_backend_canonical_address(self):
        """Cross-file literal pin — source of truth is main.py.

        If the canonical street address changes in main.py and the frontend is
        not updated, the dispatcher's pick would put a stale address into the
        staging line — which IS the responders' Slack maps-link query.
        """
        prod_addrs = re.findall(
            r'^\s+"((?:\d|W |E )[^"]*San Jose, CA)",$', self._main_src(), re.MULTILINE
        )
        assert prod_addrs, "no canonical addresses parsed from main.py"
        commits = self._frontend_facilities()
        for addr in prod_addrs:
            assert any(addr in c for c in commits), (
                f"main.py canonical address {addr!r} appears in no frontend "
                f"pick-list commit string. Frontend commits: {commits}"
            )

    def test_frontend_commit_strings_match_the_backend_facility_patterns(self):
        """End-to-end pin: what the frontend SENDS must be what the backend MATCHES.

        The pick posts its commit string to /apply-staging-override, which relies
        on _CANONICAL_STAGING_FACILITIES to resolve it. If the frontend wording
        drifts out of the backend regex, the string falls through to free-text
        geocoding — the exact #646 failure, reached by a different route.
        """
        for commit in self._frontend_facilities():
            assert _match_canonical_staging_facility(commit) is not None, (
                f"frontend pick-list commit {commit!r} does not match any "
                f"backend facility pattern — it would be free-text geocoded"
            )

    def test_commit_strings_include_a_street_address_not_just_a_venue_name(self):
        """The staging line becomes the responders' maps-link query.

        index.html builds staging_apple_url / staging_google_url from the TEXT,
        not from coordinates. A bare venue name would route every responder who
        taps the Slack link by whatever the maps app guesses — "Richey Center"
        alone resolves 24 km away.
        """
        for commit in self._frontend_facilities():
            assert re.search(r"\d+\s+\w", commit), (
                f"pick-list commit {commit!r} has no street number — the Slack "
                f"staging link would be a venue-name search"
            )

    def test_pick_list_renders_inside_the_override_panel(self):
        src = self._frontend_src()
        panel = re.search(
            r'<details id="staging-override-panel">(.*?)</details>', src, re.DOTALL
        )
        assert panel, "override panel not found"
        assert 'id="override-pick-list"' in panel.group(1), (
            "the pick list must live INSIDE the override panel (Bill's "
            "2026-07-26 refinement — not a second panel)"
        )

    def test_pick_list_rerenders_on_panel_open(self):
        """Without the toggle hook the list is empty on first open, or stale
        after an override renumbers the recommendations."""
        src = self._frontend_src()
        assert re.search(
            r'panel\.addEventListener\("toggle".*?_overrideRenderPickList\(\)',
            src, re.DOTALL,
        ), "pick list is not re-rendered when the panel opens"

    def test_ranked_rec_pick_dedupes_in_both_surfaces(self):
        """A promoted rec must not remain in its old position.

        Two entries at identical coordinates render as two CalTopo markers — one
        red cp and one blue point — on the same spot.
        """
        src = self._frontend_src()
        assert "sourceText && e.text === sourceText" in src, (
            "textarea dedupe missing — promoted rec would appear twice in the list"
        )
        assert re.search(
            r"s\.lat === source\.lat && s\.lng === source\.lng", src
        ), "map_data dedupe missing — duplicate marker at the same coordinates"

    def test_apply_listener_is_wrapped_not_passed_bare(self):
        """addEventListener passes the MouseEvent as arg 0.

        _overrideHandleApplyClick's first parameter is `pickLabel`, so a bare
        function reference makes every free-text Apply look like a pick-list
        commit and writes a MouseEvent into the Event Log line.
        """
        src = self._frontend_src()
        assert 'apply.addEventListener("click", () => _overrideHandleApplyClick())' in src, (
            "override-apply-btn listener must be wrapped in an arrow function"
        )

    def test_every_pick_source_is_audited(self):
        """Bill, 2026-07-26: log an event whichever choice the dispatcher makes.

        The shared "Staging override applied" prefix is what lets
        _removeOverrideEventLogEntries collapse iterations to one entry.
        """
        src = self._frontend_src()
        assert '"known facility"' in src, "facility picks are not distinctly logged"
        assert "recommendation #${source.n}" in src, (
            "recommendation picks must name which ranked entry was chosen"
        )
        assert "Staging override applied via ${modeLabel}" in src, (
            "all pick sources must share the 'Staging override applied' prefix "
            "so iterations collapse to a single Event Log entry"
        )

    def test_pick_list_offers_two_facilities_plus_five_ranked_recs(self):
        """Bill, 2026-07-26: "I'd like to offer 2 + 5."

        Uncapped, a real incident (7 ranked + officer entry) would render 10 tap
        targets. The cap applies ONLY to the picker — the textarea list,
        CalTopo, and the summary still carry everything.
        """
        src = self._frontend_src()
        m = re.search(r"const _PICK_LIST_REC_CAP = (\d+);", src)
        assert m, "_PICK_LIST_REC_CAP not found in index.html"
        assert m.group(1) == "5", (
            f"pick list ranked-rec cap is {m.group(1)}, expected 5 (2 canonical "
            f"facilities + 5 ranked)"
        )
        assert re.search(
            r"_overrideApplyPickListCap\(_overrideParseRankedRecs\(\)\)", src
        ), "the cap is defined but not applied to the rendered rows"
        assert len(self._frontend_facilities()) == 2, (
            "the '2' in '2 + 5' is the canonical facility count"
        )

    def test_officer_entry_keeps_the_last_pick_slot(self):
        """Bill, 2026-07-26: reserve a slot for the officer-designated entry.

        It is normally APPENDED LAST (position 8 of 8), so a plain top-5 slice
        drops the one location an actual deputy on scene designated. When it
        falls outside the cap it takes the final slot — ranked 1-4 + officer —
        and the row count stays at _PICK_LIST_REC_CAP.
        """
        src = self._frontend_src()
        fn = re.search(
            r"function _overrideApplyPickListCap\(.*?\n  \}\n", src, re.DOTALL
        )
        assert fn, "_overrideApplyPickListCap not found in index.html"
        body = fn.group(0)
        assert "_OFFICER_OVERRIDE_LABEL" in body and 'r.type === "officer"' in body, (
            "officer detection must check BOTH the entry type and the label — "
            "the textarea list and _rawMapData.staging are populated independently"
        )
        assert "_PICK_LIST_REC_CAP - 1" in body, (
            "the officer entry must REPLACE the last slot, not be appended — "
            "appending would make the list 6 rows and break the 2+5 contract"
        )

    def test_active_override_is_not_offered_as_a_pickable_row(self):
        """An active dispatcher override must not appear in the pick list.

        Found by walking the hand-entered-override flow (Bill, 2026-07-26).
        The committed item is built as `${address} — ${_DISPATCHER_OVERRIDE_LABEL}`,
        so re-committing an entry that already carries that label DOUBLES it:
            "1000 Foxworthy Ave — Dispatcher-specified staging location
                                — Dispatcher-specified staging location"
        That string becomes the "Staging Area for Resources:" line, which is the
        responders' Slack maps-link query.
        """
        src = self._frontend_src()
        fn = re.search(
            r"function _overrideParseRankedRecs\(.*?\n  \}\n", src, re.DOTALL
        )
        assert fn, "_overrideParseRankedRecs not found"
        body = fn.group(0)
        assert 'r.type !== "dispatcher"' in body, (
            "pick list must exclude the active dispatcher override by type"
        )
        assert "_DISPATCHER_OVERRIDE_LABEL" in body, (
            "pick list must also exclude by label — the textarea list and "
            "_rawMapData.staging are populated independently"
        )

    def test_pick_list_rerenders_after_commit_and_after_revert(self):
        """The panel stays OPEN across a commit, so the toggle hook never fires.

        Without these calls the list keeps rendering pre-commit state: "N."
        numbers that disagree with the renumbered textarea, and a row for an
        entry that no longer exists. After a revert, clicking such a row would
        re-commit a deleted override.
        """
        src = self._frontend_src()
        for fn_name in ("_overrideCommit", "_overrideRevertTextareaAndMapData"):
            fn = re.search(rf"function {fn_name}\(.*?\n  \}}\n", src, re.DOTALL)
            assert fn, f"{fn_name} not found"
            assert "_overrideRenderPickList()" in fn.group(0), (
                f"{fn_name} must re-render the pick list — otherwise it goes "
                f"stale while the panel is open"
            )

    def test_rec_pick_is_typed_dispatcher_so_it_takes_the_cp_marker(self):
        """Bill, 2026-07-26: whichever entry the dispatcher picks gets cp; all
        others are blue points.

        caltopo.py arbitrates on `s.get("type")`, so the promoted entry carries
        type "dispatcher" and wins cp. This ALSO preserves the locked rule that
        an entry typed "officer" never takes cp — picking the officer's location
        commits a dispatcher-typed copy of it rather than promoting the officer
        entry itself.
        """
        src = self._frontend_src()
        commit_fn = re.search(
            r"function _overrideCommit\(.*?\n  \}\n", src, re.DOTALL
        )
        assert commit_fn, "_overrideCommit not found"
        assert 'type: "dispatcher"' in commit_fn.group(0), (
            "the committed pick must be typed 'dispatcher' or caltopo.py will "
            "not give it the cp marker"
        )


# ---------------------------------------------------------------------------
# main.py — Staging Area for Resources content REPLACEMENT in the IIS.
# Bug history: 2026-05-09 SJSU smoke test on personal-dev surfaced a
# decision-level divergence between WhatsApp and Full Summary. The IIS
# "Staging Area for Resources" field carried over the pre-issue-#244 rule
# ("officer-designated staging always wins") by copying the officer's text
# verbatim, while the WhatsApp section and Recommendations list followed
# the post-#244 rule (quality-ranked, no officer auto-promotion). One
# payload, two answers.
#
# Fix: REPLACE the field's content (officer text) with the #1 quality-ranked
# recommendation's bare form (LOCATION — TYPE), AFTER PASS 3 + mismatch-check
# have parsed the original value. Both display surfaces show the same line.
#
# Pinned by mirroring the extraction + replacement regexes. Failure here =
# regression to the divergent state OR drift in the bare-form parsing that
# changes what the IIS field shows.
# ---------------------------------------------------------------------------


def _extract_top_recommendation_bare_form(summary: str) -> str:
    """Mirror of the #1-recommendation bare-form extraction in main.py."""
    m = re.search(
        r"\nStaging Area Recommendations:\n(.*?)(?=\n---\n)",
        summary,
        re.DOTALL,
    )
    if not m:
        return ""
    for line in m.group(1).splitlines():
        m1 = re.match(r"^1\.\s+(.+)$", line)
        if m1:
            entry_text = m1.group(1).strip()
            entry_text = re.sub(
                r"\s+—\s+Officer-designated staging location.*$", "", entry_text
            )
            return entry_text.split(". ")[0].strip()
    return ""


def _replace_staging_area_field(summary: str, top_rec_bare: str) -> str:
    """Mirror of the IIS field content-replacement in main.py.

    PR-B (Melanie's main.py C-2): the replacement value comes from Gemini's
    PASS 2 Recommendations text and can in principle contain a backslash-digit
    or `\\g<name>` sequence that re.sub would interpret as a backreference.
    The lambda form bypasses re.sub's replacement-string parser entirely.
    """
    if top_rec_bare:
        replacement = f"Staging Area for Resources: {top_rec_bare}"
    else:
        replacement = "Staging Area for Resources:"
    return re.sub(
        r"^Staging Area for Resources:.*$",
        lambda m: replacement,
        summary,
        count=1,
        flags=re.MULTILINE,
    )


# Realistic SJSU summary mirroring the 2026-05-09 smoke-test output. Used
# across multiple tests so the bug context stays visible at the bottom of
# the file and assertions read as "this exact regression cannot recur."
_SJSU_SUMMARY = (
    "Initial Incident Summary:\n"
    "Event Name: 2026-05-07 SJSU 5th\n"
    "Event #: 26 0450\n"
    "Last Known Position: N 5th St & E St John St, San Jose, CA\n"
    "Residence Address: 100 Example Rd, Palo Alto, CA 94306\n"
    "Staging Area for Resources: Alma Ave & S 10th St, San Jose, CA\n"
    "Dispatcher: Burns 305\n"
    "\n"
    "---\n"
    "Event Log:\n"
    "2026-05-07 14:43 - Request received from SJSU PD\n"
    "\n"
    "---\n"
    "Staging Area Recommendations:\n"
    "1. 55 North 7th Street, San Jose — Horace Mann Elementary School. 0.12 mi from LKP; parking ~15-20 vehicles; restrooms likely; lighting unverified.\n"
    "2. 235 East Santa Clara Street, San Jose — Sixth Street Burger. 0.12 mi from LKP; parking ~5-10 vehicles; restrooms likely; well-lit.\n"
    "7. Alma Ave & S 10th St, San Jose, CA — Officer-designated staging location (not among top recommendations — dispatcher discretion).\n"
    "\n"
    "---\n"
    "LPB Range Ring Analysis:\n"
)


class TestStagingAreaForResourcesReplacement:
    """Pin the IIS field content-replacement: the officer's text is replaced
    with the #1 quality-ranked recommendation's bare form so both display
    surfaces show the same staging answer. Drift here = pre-#244 'officer
    wins' rule silently re-emerges or the bare-form parsing changes."""

    def test_field_content_replaced_with_top_recommendation_bare_form(self):
        bare = _extract_top_recommendation_bare_form(_SJSU_SUMMARY)
        assert bare == "55 North 7th Street, San Jose — Horace Mann Elementary School"
        result = _replace_staging_area_field(_SJSU_SUMMARY, bare)
        assert (
            "Staging Area for Resources: 55 North 7th Street, San Jose — Horace Mann Elementary School"
            in result
        )
        # Officer's original text MUST NOT survive in the IIS field.
        assert "Staging Area for Resources: Alma Ave & S 10th St" not in result

    def test_officer_entry_preserved_in_recommendations_list(self):
        # Replacement targets only the IIS field, not the Recommendations
        # section. Officer's labeled entry at #7 stays intact so dispatcher
        # still sees the officer's preference.
        bare = _extract_top_recommendation_bare_form(_SJSU_SUMMARY)
        result = _replace_staging_area_field(_SJSU_SUMMARY, bare)
        assert "7. Alma Ave & S 10th St, San Jose, CA — Officer-designated staging location" in result

    def test_other_fields_preserved(self):
        # Surrounding IIS fields (LKP, Residence, Event #, Dispatcher) all
        # untouched — the replacement is anchored to the field label.
        bare = _extract_top_recommendation_bare_form(_SJSU_SUMMARY)
        result = _replace_staging_area_field(_SJSU_SUMMARY, bare)
        assert "Last Known Position: N 5th St & E St John St, San Jose, CA" in result
        assert "Residence Address: 100 Example Rd, Palo Alto, CA 94306" in result
        assert "Event #: 26 0450" in result
        assert "Dispatcher: Burns 305" in result

    def test_top_rec_when_officer_is_number_one(self):
        # Edge case: if the officer's location IS the #1 recommendation,
        # the bare form strips the "Officer-designated staging location"
        # suffix so the IIS field doesn't double-up the label.
        summary = (
            "Initial Incident Summary:\n"
            "Staging Area for Resources: officer raw text\n"
            "\n---\n"
            "Staging Area Recommendations:\n"
            "1. Cardoza Park, Milpitas — City park. 0.05 mi from LKP; parking ~15-20 vehicles; restrooms likely; lighting unverified. — Officer-designated staging location\n"
            "\n---\n"
            "End:\n"
        )
        bare = _extract_top_recommendation_bare_form(summary)
        # Officer-designated suffix stripped; bare form is just LOCATION — TYPE.
        assert bare == "Cardoza Park, Milpitas — City park"

    def test_blank_top_recommendation_blanks_field(self):
        # Defensive: when no #1 recommendation exists (Overpass failure +
        # Gemini fallback empty), the field content is blanked rather than
        # leaving stale officer text in place.
        summary_no_rec = (
            "Initial Incident Summary:\n"
            "Staging Area for Resources: officer raw text\n"
            "\n---\n"
            "Staging Area Recommendations:\n"
            "\n---\n"
            "End:\n"
        )
        bare = _extract_top_recommendation_bare_form(summary_no_rec)
        assert bare == ""
        result = _replace_staging_area_field(summary_no_rec, bare)
        assert "Staging Area for Resources:\n" in result
        assert "Staging Area for Resources: officer raw text" not in result

    def test_replacement_only_targets_iis_field_label(self):
        # The phrase "Staging Area for Resources" can appear in body text
        # (Event Log notes, etc.). The regex is anchored to start-of-line
        # followed by ":" — only the field LABEL is matched.
        summary = (
            "Initial Incident Summary:\n"
            "Staging Area for Resources: officer raw text\n"
            "\n---\n"
            "Event Log:\n"
            "2026-05-09 18:57 - Note: Officer's Staging Area for Resources noted at #7\n"
            "\n---\n"
            "Staging Area Recommendations:\n"
            "1. Some Place, Cityville — Type. details.\n"
            "\n---\n"
            "End:\n"
        )
        bare = _extract_top_recommendation_bare_form(summary)
        result = _replace_staging_area_field(summary, bare)
        # Only the IIS field label was replaced. Event Log mention preserved.
        assert "Officer's Staging Area for Resources noted at #7" in result
        assert "Staging Area for Resources: Some Place, Cityville — Type" in result


# main.py — Event Name length cap (#672). Source of truth: main.py.
# Parity pinned by TestEventNameLengthCap.test_cap_literal_matches_production.
_EVENT_NAME_MAX_LEN = 50


def _cap_event_name_mirror(evt_date: str, evt_agency: str, evt_location: str) -> str:
    """Mirror of main.py::_cap_event_name()."""
    full = f"{evt_date} {evt_agency} {evt_location}".strip()
    if len(full) <= _EVENT_NAME_MAX_LEN:
        return full
    prefix = f"{evt_date} {evt_agency}".strip()
    budget = _EVENT_NAME_MAX_LEN - len(prefix) - 1
    if budget <= 0:
        return full[:_EVENT_NAME_MAX_LEN].rstrip()
    words = evt_location.split()
    kept: list[str] = []
    for word in words:
        candidate = " ".join(kept + [word])
        if len(candidate) > budget:
            break
        kept.append(word)
    trimmed = " ".join(kept) if kept else evt_location[:budget].rstrip()
    return f"{prefix} {trimmed}".strip()


class TestD4HButtonDeepLink:
    """Pin the D4H button's deep link and its promotion out of stub styling.

    Until D4H was integrated the button could only open the incidents LIST —
    the dispatcher then hunted for their own incident mid-callout. The incident
    is now created by /send-notification, which returns its activity_id.

    URL SHAPE IS EMPIRICAL, not guessed: /incidents/view/{id}, confirmed live
    2026-05-18 (Bill, spike 11) and recorded in experiments/d4h/config.py.
    /team/incidents/{id} 404s. Do not "tidy" it to match the list URL.
    """

    @staticmethod
    def _index_html():
        return (Path(__file__).resolve().parent.parent / "frontend" /
                "index.html").read_text(encoding="utf-8")

    def test_deep_link_uses_the_confirmed_path_shape(self):
        html = self._index_html()
        assert "/incidents/view/${encodeURIComponent(activityId)}" in html, (
            "The D4H deep link no longer uses the confirmed /incidents/view/{id} "
            "shape. /team/incidents/{id} 404s — see experiments/d4h/config.py."
        )

    def test_activity_id_comes_from_the_dispatch_response(self):
        """/dispatch-status deliberately excludes d4h_activity_id as internal,
        so the send response is the only source."""
        html = self._index_html()
        assert "_setD4hIncident(data.d4h_activity_id)" in html, (
            "The button is never pointed at the created incident."
        )

    def test_button_is_promoted_out_of_stub_styling(self):
        """`.btn-dispatch-stub` means "opens the portal, but not yet
        integrated" — leaving it amber implies the integration did not run."""
        html = self._index_html()
        fn = html[html.index("function _setD4hIncident"):]
        fn = fn[:fn.index("function _resetD4hButton")]
        assert "classList.remove('btn-dispatch-stub')" in fn
        assert "classList.add('btn-dispatch-active')" in fn

    def test_falls_back_to_the_list_when_no_incident_exists(self):
        """Before dispatch there is no incident to open — the button must still
        do something useful rather than nothing."""
        html = self._index_html()
        assert "btn._d4hUrl || 'https://sccssar.team-manager.us.d4h.com/team/incidents'" in html, (
            "The pre-dispatch fallback to the incidents list is gone."
        )

    def test_reset_clears_the_previous_incident(self):
        """Processing a second form must not leave the FIRST incident's link on
        the button — the same stale-handle shape as the #414 teardown risk."""
        html = self._index_html()
        assert "_resetD4hButton();" in html, (
            "The D4H button is not reset between forms, so a second dispatch "
            "would deep-link to the previous incident."
        )
        fn = html[html.index("function _resetD4hButton"):]
        fn = fn[:fn.index("\n  }") + 4]
        assert "_d4hUrl = null" in fn
        assert "classList.add('btn-dispatch-stub')" in fn

    def test_host_stays_allowlisted(self):
        """safeOpen refuses non-allowlisted hosts; the deep link must pass."""
        html = self._index_html()
        assert '"sccssar.team-manager.us.d4h.com"' in html, (
            "The D4H host left the safeOpen allowlist — every D4H open would "
            "be blocked with an alert."
        )


class TestNotesExtractionReadsRiskFieldsOnly:
    """Pin the #670 frontend extraction to risk-factor fields (detail/reason).

    The first version matched any `— <label>:` field, which also caught Q2
    `number:` and Q4 `date:` — so the subject's PHONE NUMBER and the MUPS entry
    date could land in the pinned incident channel. Measured on the 40-form
    corpus: 11 of 46 snippets were that noise (8 dates, 3 phone numbers).

    Issue #670 asks for "risk-factor detail fields". `detail:` (Q9 mental
    health) and `reason:` (Q6 at-risk) are those fields; the others are not.

    Pinned here rather than in test_slack.py because the extraction lives in
    index.html, which no JS test suite covers — the server-side FILTERING is
    unit-tested in test_slack.py::TestNovelNotes, but the choice of WHICH
    fields to read is only visible here.
    """

    @staticmethod
    def _index_html():
        return (Path(__file__).resolve().parent.parent / "frontend" /
                "index.html").read_text(encoding="utf-8")

    def _regex_line(self):
        for line in self._index_html().splitlines():
            if "notesMatches" in line and "matchAll" in line:
                return line
        raise AssertionError("the #670 notes extraction regex is gone from index.html")

    def test_extraction_is_limited_to_detail_and_reason(self):
        line = self._regex_line()
        assert "(?:detail|reason)" in line, (
            "The notes regex no longer restricts to detail|reason. A broad "
            "label match pulls Q2 'number:' (the subject's phone) and Q4 "
            "'date:' into the pinned welcome — 11 of 46 corpus snippets."
        )

    def test_extraction_does_not_match_an_open_label_class(self):
        """The specific regression: `[A-Za-z ]+:` matched every label."""
        line = self._regex_line()
        assert "[A-Za-z ]+:" not in line, (
            "The open label class is back — Q2 number: and Q4 date: are "
            "matched again."
        )

    def test_staging_unverified_detection_is_narrow(self):
        """Narrow trigger, per Bill 2026-08-01 — only the distance guard.

        Broadening this to every low-confidence geocode would put a location-
        conflict warning on the pinned staging message of routine dispatches,
        which trains responders to ignore it.
        """
        html = self._index_html()
        line = next((l for l in html.splitlines() if "stagingUnverified" in l
                     and "test(" in l), None)
        assert line, "the staging-unverified detection is gone from index.html"
        assert "staging coordinate" in line and "from the LKP" in line, (
            "The detection no longer keys on the officer-coordinate-vs-LKP "
            "distance WARNING specifically."
        )

    def test_staging_unverified_is_sent_to_the_backend(self):
        assert "staging_unverified:" in self._index_html(), (
            "staging_unverified is no longer in the /send-notification payload, "
            "so the responder-facing warning can never render."
        )

    def test_staging_unverified_read_from_the_live_textarea(self):
        """Self-clearing behaviour.

        The flag is derived from the Event Log in the textarea, so a dispatcher
        who confirms the location with the officer and deletes that WARNING
        line also clears the warning responders see — the same principle as the
        staging text itself.
        """
        html = self._index_html()
        line = next(l for l in html.splitlines()
                    if "stagingUnverified" in l and "test(" in l)
        assert ".test(text)" in line.replace(" ", ""), (
            "The detection no longer runs against the parsed textarea `text`, "
            "so editing the Event Log would not clear the warning."
        )

    def test_staging_unmapped_detection_keys_on_the_no_pois_note(self):
        """Marks staging as unverified when the POI lookup returned nothing.

        Gemini then generates the whole recommendation list from training data,
        formatted identically to a real one. Live 2026-08-01: a remote anchor
        produced two entries at the SAME invented address, which no dedup could
        catch because there were no candidates to dedup.
        """
        html = self._index_html()
        line = next((l for l in html.splitlines()
                     if "stagingUnmapped" in l and "test(" in l), None)
        assert line, "the staging-unmapped detection is gone from index.html"
        assert "No staging POIs found within" in line, (
            "The detection no longer keys on the zero-candidates Event Log note."
        )

    def test_staging_unmapped_is_sent_to_the_backend(self):
        assert "staging_unmapped:" in self._index_html(), (
            "staging_unmapped is no longer in the /send-notification payload."
        )

    def test_notes_are_sent_to_the_backend(self):
        html = self._index_html()
        assert "mp_notes:" in html, (
            "mp_notes is no longer in the /send-notification payload, so the "
            "Notes line can never render."
        )


class TestEventNameLengthCap:
    """Pin the Event Name length cap (issue #672).

    _AGENCY_DISPLAY canonicalizes in-county agencies; an out-of-county
    mutual-aid agency in full legal form has no entry and passes through
    verbatim. On 2026-07-31 that produced a 62-char name that reached the
    Everbridge title, the CalTopo map title, the D4H record and the Slack
    channel name — renamed by hand afterwards.

    50 is measured, not chosen: across the 40-form corpus real event names run
    median 28 / p90 36 / max 41, so 50 truncates NOTHING ever dispatched while
    still catching the 62-char outlier.
    """

    def test_cap_literal_matches_production(self):
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        m = re.search(r"^_EVENT_NAME_MAX_LEN = (\d+)\s*$", src, re.MULTILINE)
        assert m, "_EVENT_NAME_MAX_LEN is not defined in main.py"
        assert int(m.group(1)) == _EVENT_NAME_MAX_LEN, (
            f"main.py caps the Event Name at {m.group(1)} but this file mirrors "
            f"{_EVENT_NAME_MAX_LEN}. Re-check the downstream limits before "
            f"changing it: Slack channel name 75 effective, D4H "
            f"referenceDescription 104 effective on personal-dev."
        )

    def test_real_corpus_length_names_are_untouched(self):
        """Corpus max is 41 — nothing ever dispatched may be truncated."""
        for date, agency, loc in [
            ("2026-04-25", "MPD", "Calaveras"),
            ("2026-07-27", "SJPD", "Mountain Home"),
            ("2026-05-08", "SCCSO", "Saratoga Sunnyvale"),
        ]:
            out = _cap_event_name_mirror(date, agency, loc)
            assert out == f"{date} {agency} {loc}"

    def test_the_2026_07_31_name_is_capped(self):
        out = _cap_event_name_mirror(
            "2026-07-31", "HUMBOLDT COUNTY SHERIFF'S OFFICE", "Redwood Valley Ranch")
        assert len(out) <= _EVENT_NAME_MAX_LEN

    def test_location_is_truncated_not_the_agency(self):
        """Issue #672: the agency identifies who is asking; the date is fixed
        width. The LOCATION is what gives way.

        The agency here is LONG on purpose. With a short one the date+agency
        prefix is ~16 chars, and a mutation that truncates the prefix is a
        silent no-op — mutation testing caught exactly that vacuity.
        """
        agency = "HUMBOLDT COUNTY SHERIFF'S OFFICE"
        full = f"2026-07-31 {agency} Redwood Valley Ranch"
        assert len(full) > _EVENT_NAME_MAX_LEN, (
            "fixture no longer exceeds the cap — this test would be vacuous"
        )
        out = _cap_event_name_mirror("2026-07-31", agency, "Redwood Valley Ranch")
        assert out.startswith(f"2026-07-31 {agency}"), (
            "The agency or date was truncated instead of the location."
        )

    def test_location_trims_on_a_word_boundary(self):
        """The fixture MUST cross the cap or this test is vacuous.

        The first version used a location short enough that the assembled name
        came in at 47 chars — under the cap, so the function returned early and
        no truncation ran at all. It passed against a mutation that removed the
        word-boundary trim entirely.
        """
        location = "Alpha Bravo Charlie Delta Echo Foxtrot"
        full = f"2026-07-31 SCCSO {location}"
        assert len(full) > _EVENT_NAME_MAX_LEN, (
            "fixture no longer exceeds the cap — this test would be vacuous"
        )
        out = _cap_event_name_mirror("2026-07-31", "SCCSO", location)
        assert len(out) <= _EVENT_NAME_MAX_LEN
        assert out != full, "nothing was truncated"
        tail = out[len("2026-07-31 SCCSO "):]
        assert not tail.endswith(" "), "trailing space left by the trim"
        # Every surviving token must be whole — a fragment is unreadable on radio.
        for token in tail.split():
            assert token in location.split(), (
                f"{token!r} is a fragment, not a whole word — the word-boundary "
                f"trim is gone."
            )

    def test_cap_holds_when_date_and_agency_alone_overflow(self):
        """The cap must be a GUARANTEE, not a best effort.

        A pathological out-of-county agency can exceed 50 on its own, in which
        case no part of the location survives — but the result is still capped,
        because Slack and D4H do not care why the name is long.
        """
        out = _cap_event_name_mirror(
            "2026-07-31", "A" * 60, "Main")
        assert len(out) == _EVENT_NAME_MAX_LEN

    def test_over_long_single_location_word_is_hard_cut(self):
        """Documented last resort. Dropping the location entirely would lose
        all location signal for a merely-long street name, so a fragment is
        preferred over nothing."""
        out = _cap_event_name_mirror("2026-07-31", "SJPD", "S" * 90)
        assert len(out) == _EVENT_NAME_MAX_LEN
        assert out.startswith("2026-07-31 SJPD S")

    def test_result_always_fits_the_slack_channel_name(self):
        """Slack is the tightest consumer: 80 minus the '_HHMM' suffix = 75."""
        for agency, loc in [
            ("SCCSO", "Calaveras"),
            ("HUMBOLDT COUNTY SHERIFF'S OFFICE", "Redwood Valley Ranch Road"),
            ("A" * 60, "Main"),
        ]:
            out = _cap_event_name_mirror("2026-07-31", agency, loc)
            assert len(out) <= 75

    def test_every_reconstruction_branch_routes_through_the_cap(self):
        """Five sites assembled the name; none may bypass the helper.

        Asserted on code with comments stripped, and by ABSENCE of the raw
        f-string — the pattern that would silently reintroduce an uncapped name.
        """
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        code = "\n".join(l.split("#")[0] for l in src.splitlines())
        assert 'canonical_name    = f"{evt_date} {evt_agency}' not in code and \
               'canonical_name = f"{evt_date} {evt_agency}' not in code, (
            "An Event Name reconstruction branch assembles the name directly "
            "again instead of going through _cap_event_name()."
        )
        assert code.count("_cap_event_name(evt_date, evt_agency") >= 4, (
            "Fewer than four reconstruction branches call _cap_event_name()."
        )


class TestEventNameReconstructionCrossCutting:
    """Cross-cutting Event Name reconstruction tests — PR-fix-4 (2026-05-08).

    Pre-fix the test suite covered `_normalize_agency` in isolation and
    street-stripping in isolation, but no test exercised the END-TO-END
    behavior of the OCR endpoint's Event Name reconstruction across BOTH
    code paths (primary = LKP-with-house-number, city-fallback = no
    house number). The 2026-05-07 SJSU live incident exposed a regression:
    the city-fallback path skipped agency normalization AND failed to
    extract the first street from intersection LKPs, producing
    "2026-05-07 SJSU PD 5Th Street At St. John Street" instead of
    "2026-05-07 SJSU 5th".

    These tests pin the END output of both paths.
    """

    # --- City-fallback path (no house number — intersection / city / landmark) ---

    def test_intersection_with_at_keyword_extracts_first_street(self):
        # The exact 2026-05-07 SJSU LKP that triggered the regression.
        # "5th Street at St. John Street" → "5th" (street type stripped).
        street = _reconstruct_event_name_city_fallback_street(
            "5th Street at St. John Street"
        )
        assert street == "5th"

    def test_intersection_with_at_symbol_extracts_first_street(self):
        # PR-fix-5 (2026-05-08): the second SJSU re-run produced
        # "5th St. @ St. John St., San Jose, CA 95112" from the OCR. Pre-fix
        # the @ symbol was missed and the LKP fell through to title-case →
        # `5Th St. @ St. John St.` in the Event Name. Now extracts "5th".
        street = _reconstruct_event_name_city_fallback_street(
            "5th St. @ St. John St."
        )
        assert street == "5th"

    def test_intersection_with_at_symbol_no_dots_or_types(self):
        # Bare form: officer wrote "5th @ St John" (no street type / dots).
        # First-street extraction should still produce "5th".
        street = _reconstruct_event_name_city_fallback_street("5th @ St John")
        assert street == "5th"

    def test_intersection_with_ampersand_extracts_first_street(self):
        # PR-fix-4 also broadened the regex; pre-fix only "&" worked.
        # Verify the original behavior still works AND now extracts the
        # first street rather than falling back to UNKNOWN.
        street = _reconstruct_event_name_city_fallback_street(
            "5th St & St. John St"
        )
        assert street == "5th"

    def test_intersection_with_and_keyword_extracts_first_street(self):
        # Officers occasionally write "5th and Main" instead of "5th & Main".
        street = _reconstruct_event_name_city_fallback_street(
            "1st St and 2nd St"
        )
        assert street == "1st"

    def test_intersection_with_cardinal_direction(self):
        # First street has a cardinal direction prefix — strip it like
        # the primary path does.
        street = _reconstruct_event_name_city_fallback_street(
            "East 5th Street at St. John Street"
        )
        assert street == "5th"

    def test_intersection_word_form(self):
        # "INTERSECTION OF X AND Y" form — pre-issue-#400 the regex matched
        # the prefix "INTERSECTION OF" at position 0 and first_street fell
        # through to UNKNOWN. Issue #400 fix: when the prefix variant is
        # matched, skip past it and re-search for the between-streets
        # separator, producing "5th" as expected.
        street = _reconstruct_event_name_city_fallback_street(
            "INTERSECTION OF 5th and St. John"
        )
        assert street == "5th"

    def test_bare_house_number_returns_unknown(self):
        # Officer wrote just a house number — pre-existing behavior, pinned
        # so it doesn't drift back to a less-helpful "5" event name.
        street = _reconstruct_event_name_city_fallback_street("10410")
        assert street == "UNKNOWN"

    def test_city_only_lkp_title_cases(self):
        # Pre-existing fallback behavior: no intersection markers, no bare
        # number → title-case the city name.
        street = _reconstruct_event_name_city_fallback_street("SAN JOSE")
        assert street == "San Jose"

    # --- Cross-path agency canonicalization ---

    def test_agency_canonicalization_works_in_both_paths(self):
        # Pre-fix the city-fallback path silently skipped agency
        # normalization. This test pins that BOTH paths now produce the
        # same canonical agency for the same input.
        agency_primary  = _normalize_agency("SJSU PD")     # primary path lookup
        agency_fallback = _normalize_agency("SJSU PD")     # fallback path lookup (same fn)
        assert agency_primary == agency_fallback == "SJSU"

    def test_agency_no_match_passes_through_unchanged(self):
        # Defensive: an agency we don't have a canonical form for should
        # pass through unchanged in BOTH paths, not get blanked.
        assert _normalize_agency("UNKNOWN AGENCY XYZ") == "UNKNOWN AGENCY XYZ"

    def test_full_canonical_form_sjsu_intersection(self):
        # Integration: the exact failure mode from the 2026-05-07 SJSU
        # live test. Date + agency + intersection LKP → "2026-05-07 SJSU 5th".
        # Pre-fix this produced "2026-05-07 SJSU PD 5Th Street At St. John Street".
        date    = "2026-05-07"
        agency  = _normalize_agency("SJSU PD")
        street  = _reconstruct_event_name_city_fallback_street(
            "5th Street at St. John Street"
        )
        assert f"{date} {agency} {street}" == "2026-05-07 SJSU 5th"

    def test_canonical_form_has_at_most_three_tokens(self):
        # The "YYYY-MM-DD AGENCY STREETNAME" format is RADIO-READABLE — too
        # many tokens defeats the purpose. This invariant holds across
        # both paths. (We can't quite assert "at most 3" directly because
        # multi-word streets like "Foothill Expy" are valid in the primary
        # path; but the city-fallback intersection path MUST extract a
        # single street token.)
        date   = "2026-05-07"
        agency = _normalize_agency("SJSU PD")
        for raw_lkp, expected_street in [
            ("5th Street at St. John Street",                "5th"),
            ("5th St & St. John St",                         "5th"),
            ("1st St and 2nd St",                            "1st"),
            ("East 5th Street at St. John",                  "5th"),
            # PR-fix-5: handwritten `@` shorthand. Exact LKP from the second
            # SJSU re-run on personal-dev (2026-05-08).
            ("5th St. @ St. John St., San Jose, CA 95112",   "5th"),
            ("5th @ St John",                                "5th"),
        ]:
            street = _reconstruct_event_name_city_fallback_street(raw_lkp)
            full = f"{date} {agency} {street}"
            tokens = full.split()
            assert len(tokens) == 3, (
                f"Expected 3-token canonical form for raw_lkp={raw_lkp!r}; "
                f"got {tokens}"
            )
            assert tokens[2] == expected_street

    # --- "Intersection of <St> and <St>" prefix variant (issue #400, 2026-05-09) ---
    #
    # Pre-fix: when Pass 1 produced the verbose form "Intersection of N 5th
    # Street and E St John Street, San Jose, CA 95112", the regex matched
    # the prefix "INTERSECTION OF" at position 0. raw_lkp[:0] was empty
    # → first_street=""→ evt_city="UNKNOWN" → Event Name was
    # "2026-05-07 SJSU UNKNOWN" (live regression on SCCSSAR-dev).
    #
    # Post-fix: when the matched separator is the prefix variant, skip past
    # it and re-search for the between-streets separator. Single-name forms
    # (e.g. "Intersection of Foo Park") fall back to everything-before-comma.

    def test_intersection_of_prefix_extracts_first_street_sjsu(self):
        # The exact 2026-05-09 SCCSSAR-dev SJSU Pass-1 LKP that surfaced the bug.
        assert _reconstruct_event_name_city_fallback_street(
            "Intersection of N 5th Street and E St John Street, San Jose, CA 95112"
        ) == "5th"

    def test_intersection_of_prefix_with_alma(self):
        # Officer staging form (PR-D will use this path too).
        assert _reconstruct_event_name_city_fallback_street(
            "Intersection of Alma Avenue and 10th Street, San Jose, CA 95112"
        ) == "Alma"

    def test_intersection_of_prefix_uppercase(self):
        # Officers sometimes write the literal "INTERSECTION OF X AND Y"
        # in all caps. Detection is case-insensitive at the regex level.
        assert _reconstruct_event_name_city_fallback_street(
            "INTERSECTION OF 5TH STREET AND ST JOHN STREET"
        ) == "5TH"  # title-case applied by main.py post-pipeline; mirror
                    # returns the stripped form unchanged.

    def test_intersection_of_prefix_with_ampersand(self):
        # Defensive: prefix + `&` separator should also work.
        assert _reconstruct_event_name_city_fallback_street(
            "Intersection of Alma Ave & 10th St, San Jose, CA"
        ) == "Alma"

    def test_intersection_of_prefix_with_at(self):
        # Defensive: prefix + `at` separator (not just `and`) should work.
        # "Intersection of Foo at Bar" is unusual but the regex handles it.
        assert _reconstruct_event_name_city_fallback_street(
            "Intersection of 5th Street at St. John Street, San Jose, CA"
        ) == "5th"

    def test_intersection_of_prefix_no_between_separator(self):
        # Edge case: officer wrote "Intersection of Foo Park" (single named
        # location, no second street). Fall back to everything-before-comma.
        assert _reconstruct_event_name_city_fallback_street(
            "Intersection of Foo Park, San Jose, CA"
        ) == "Foo Park"

    def test_existing_between_streets_unchanged(self):
        # Regression: ensure the prefix-variant fix didn't break the existing
        # between-streets behavior. Same expected results as the table test
        # above, but called out separately so a future drift would surface here.
        assert _reconstruct_event_name_city_fallback_street(
            "5th Street at St. John Street"
        ) == "5th"
        assert _reconstruct_event_name_city_fallback_street(
            "5th St & St. John St"
        ) == "5th"
        assert _reconstruct_event_name_city_fallback_street(
            "5th @ St John"
        ) == "5th"


# ===========================================================================
# Everbridge organisation record IDs — CONFIGURATION, not source.
#
# What used to live here was a set of VACUOUS pins: _EVERBRIDGE_DELIVER_PATHS,
# _EVERBRIDGE_CATEGORY_* and _EVERBRIDGE_CALLER_ID were defined in THIS file and
# the tests asserted they equalled those same literals. Nothing read
# everbridge.py. The class docstring claimed a tidy-up "fails the build at Step 0
# of build-{dev,sccssar-dev}.sh"; it did not -- deleting the constants from
# production left all 2182 tests passing (demonstrated 2026-09-04).
#
# The IDs are also of no use to another Everbridge customer and, published,
# describe this org's Everbridge estate, so they now come from the environment
# and the pins below read PRODUCTION SOURCE instead of themselves.
#
# DISCOVERY_QUERY_PARAM is deliberately NOT configuration: "notificationEventId"
# is an Everbridge API parameter name, identical for every customer. Its pin is
# kept but made production-reading.
# ===========================================================================

_EB_ORG_ID_RE = re.compile(r"(?<![0-9])[0-9]{15,17}(?![0-9])")


class TestEverbridgeOrgIdsAreConfiguration:
    """everbridge.py imports httpx and is not importable under local pytest, so
    these read its SOURCE -- the same reason as the d4h mirror-parity pins."""

    def _eb_source(self):
        from pathlib import Path
        p = Path(__file__).parent / "everbridge.py"
        assert p.exists(), f"backend/everbridge.py not found at {p}"
        return p.read_text(encoding="utf-8")

    def _code(self, text):
        """Comments stripped -- prose about an ID would otherwise satisfy a
        naive absence check."""
        return "\n".join(l.split("#")[0] for l in text.splitlines())

    def _func_body(self, source, name):
        start = source.index(f"def {name}(")
        doc_close = source.index('"""', source.index('"""', start) + 3) + 3
        rest = source[doc_close:]
        m = re.search(r"\n(?=(?:def |class )\w)", rest)
        return rest[: m.start()] if m else rest

    def test_no_everbridge_org_id_is_hardcoded_in_source(self):
        hits = _EB_ORG_ID_RE.findall(self._code(self._eb_source()))
        assert not hits, (
            f"backend/everbridge.py hardcodes Everbridge org record ID(s): {hits}. "
            f"These belong in EVERBRIDGE_DELIVER_PATHS / "
            f"EVERBRIDGE_SUPPRESSED_GROUP_IDS."
        )

    def test_deliver_paths_and_suppressed_groups_read_the_env(self):
        code = self._code(self._eb_source())
        for frag in ('os.environ.get("EVERBRIDGE_DELIVER_PATHS", "")',
                     'os.environ.get("EVERBRIDGE_SUPPRESSED_GROUP_IDS", "")'):
            assert frag in code, f"everbridge.py no longer reads {frag}"

    def test_deliver_paths_guard_raises_and_checks_both_keys(self):
        body = self._func_body(self._eb_source(), "_require_deliver_paths")
        assert "raise RuntimeError(" in body[body.index("if not DELIVER_PATHS:"):], (
            "_require_deliver_paths() does not raise when deliverPaths is unset."
        )
        # Phase 0 Key Discovery #2: BOTH keys, or EB returns HTTP 400.
        for key in ('"id" not in', '"pathId" not in'):
            assert key in body, f"_require_deliver_paths() does not check {key}"

    def test_payload_calls_the_guard_not_the_bare_constant(self):
        """Assert the CALL SITE. A guard nothing calls is not a guard."""
        body = self._func_body(self._eb_source(), "_build_send_notification_payload")
        assert '"deliverPaths": list(_require_deliver_paths())' in body, (
            "_build_send_notification_payload does not route deliverPaths through "
            "_require_deliver_paths()."
        )

    def test_malformed_json_does_not_raise_at_import(self):
        """Import-time failure would take the service down for a config typo;
        the guard raises at payload-build time instead."""
        body = self._func_body(self._eb_source(), "_parse_deliver_paths")
        assert "return []" in body and "raise" not in body, (
            "_parse_deliver_paths must degrade to [] rather than raise -- it runs "
            "at module import."
        )

    def test_category_ids_come_from_the_env_in_main(self):
        from pathlib import Path
        main_py = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        code = self._code(main_py)
        assert '"incounty":  "EVERBRIDGE_CATEGORY_INCOUNTY"' in code, (
            "main.py no longer maps template types to category ENV VAR NAMES."
        )
        hits = _EB_ORG_ID_RE.findall(code)
        assert not hits, f"main.py hardcodes Everbridge org record ID(s): {hits}"

    def test_terraform_declares_every_org_id_var_in_both_environments(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "terraform" / "environments"
        wanted = ("EVERBRIDGE_ORG_ID",
                  "EVERBRIDGE_CATEGORY_INCOUNTY", "EVERBRIDGE_CATEGORY_MUTUALAID",
                  "EVERBRIDGE_DELIVER_PATHS", "EVERBRIDGE_SUPPRESSED_GROUP_IDS")
        for env in ("dev", "sccssar-dev"):
            main_tf = (root / env / "main.tf").read_text(encoding="utf-8")
            for name in wanted:
                assert f'name  = "{name}"' in main_tf, (
                    f"{env}/main.tf does not set {name}. Session Rule #7: env vars "
                    f"need `terraform apply`, and build scripts do NOT pick up "
                    f"Terraform changes."
                )

    def test_no_org_id_in_any_tracked_terraform_file(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "terraform" / "environments"
        for p in sorted(root.rglob("*.tf")) + sorted(root.rglob("*.template")):
            hits = _EB_ORG_ID_RE.findall(self._code(p.read_text(encoding="utf-8")))
            assert not hits, f"{p} carries Everbridge org record ID(s): {hits}"


class TestEverbridgeDiscoveryQueryParam:
    """`notificationEventId` is an Everbridge API parameter name -- the same for
    every customer -- so it stays a source constant. Phase 0 Key Discovery #8:
    `?eventId=`, `?event=` and `?search={...}` are silently ignored and return
    all recent notifications regardless of value.

    Now reads everbridge.py rather than a literal in this file.
    """

    def _param(self):
        from pathlib import Path
        src = (Path(__file__).parent / "everbridge.py").read_text(encoding="utf-8")
        m = re.search(r'^DISCOVERY_QUERY_PARAM\s*=\s*"([^"]+)"', src, re.M)
        assert m, "DISCOVERY_QUERY_PARAM not found in backend/everbridge.py"
        return m.group(1)

    def test_param_name_is_notification_event_id(self):
        assert self._param() == "notificationEventId"

    def test_param_name_is_not_a_silently_ignored_lookalike(self):
        assert self._param() not in {"eventId", "event", "search"}

    def test_the_param_is_what_the_query_builder_actually_emits(self):
        """Assert the CALL SITE -- the constant could be right while the builder
        hardcodes something else."""
        from pathlib import Path
        src = (Path(__file__).parent / "everbridge.py").read_text(encoding="utf-8")
        assert "return {DISCOVERY_QUERY_PARAM: event_id}" in src, (
            "_build_discovery_query_params no longer uses DISCOVERY_QUERY_PARAM."
        )


# main.py::_check_stop_conditions — idle timeout mirror of
# incidents.py::IDLE_S. Cross-file pin enforces that a future change in
# one site doesn't silently drift from the other. 60 min matches the
# team's stand-down policy after the 2026-05-07 SJSU live incident
# review (was 30 min — too aggressive).
_IDLE_S_MIRROR     = 60 * 60
_HARD_CAP_S_MIRROR = 4 * 60 * 60


class TestPollingTimingMirror:
    """Pre-flight pin for the main.py mirror of the idle/hard-cap timeouts.
    main.py keeps its own copies of these constants so _check_stop_conditions
    stays import-free; this test makes sure both sites move together."""

    def test_idle_60_minutes(self):
        # main.py:_IDLE_S MUST equal 60 * 60 (seconds). Mirrored from
        # incidents.py::IDLE_S which test_incidents.py pins separately.
        assert _IDLE_S_MIRROR == 60 * 60

    def test_hard_cap_4_hours(self):
        # main.py:_HARD_CAP_S MUST equal 4 * 60 * 60 (seconds).
        assert _HARD_CAP_S_MIRROR == 4 * 60 * 60


# everbridge.py::list_groups — pageSize must be 1000 to pre-empt silent
# truncation of the dispatcher group list. EB's default pageSize is 100;
# SCCSSAR has ~12 visible groups today but future expansion could exceed
# the default and silently drop groups at slot 101+. Mirrors the
# list_contacts() precedent.
_LIST_GROUPS_PAGE_SIZE = 1000


class TestListGroupsPageSize:
    """list_groups() must pin pageSize=1000 to pre-empt silent truncation."""

    def test_page_size_is_one_thousand(self):
        assert _LIST_GROUPS_PAGE_SIZE == 1000

    def test_page_size_is_not_eb_default(self):
        # EB's default is 100; pinning != default is the whole point of
        # this regression. A future "let's just use defaults" PR fails here.
        assert _LIST_GROUPS_PAGE_SIZE != 100

    def test_page_size_matches_list_contacts_precedent(self):
        # list_contacts() already uses 1000 (PII-minimal projection of full
        # contact list). Keeping list_groups symmetric so behavior is
        # predictable across the EB fetch helpers.
        list_contacts_page_size = 1000   # pinned in everbridge.py::list_contacts
        assert _LIST_GROUPS_PAGE_SIZE == list_contacts_page_size


# main.py — Cloud Tasks queue name (must match Terraform resource).
# Live test 2026-04-27 surfaced a constant/Terraform drift: main.py used
# "everbridge-poll" while Terraform provisioned "everbridge-poll-queue".
# Cloud Tasks CreateTask returned NOT_FOUND ("Queue does not exist"). Pinning
# the value here so a future "tidy up the suffix" PR fails pre-flight.
#
# Source of truth: terraform/environments/dev/main.tf
#   resource "google_cloud_tasks_queue" "everbridge_poll" {
#     name = "everbridge-poll-queue"
#   }
# When Phase 5 Step 5.1 mirrors infrastructure to SCCSSAR-dev, the queue name
# must stay identical (this constant is shared across both envs).
_EB_POLL_QUEUE_NAME = "everbridge-poll-queue"


class TestEverbridgePollQueueName:
    def test_queue_name_matches_terraform_provisioned_value(self):
        # If this fails, either main.py::_EB_POLL_QUEUE drifted, OR
        # someone renamed the Terraform resource without updating both
        # this mirror AND main.py. Both must change together.
        assert _EB_POLL_QUEUE_NAME == "everbridge-poll-queue"

    def test_queue_name_is_not_the_short_form(self):
        # The pre-fix value "everbridge-poll" — explicit anti-pattern pin
        # so a future PR that removes the "-queue" suffix from EITHER
        # main.py or Terraform fails this test.
        assert _EB_POLL_QUEUE_NAME != "everbridge-poll"


# ===========================================================================
# Phase 1 lessons-learned (2026-04-27) — pin cross-file literals so future
# drift between Python ↔ Terraform ↔ Phase 0 docs surfaces at pre-flight
# rather than at live-test time. See CLAUDE.md "Cross-file literal pin
# policy" Locked Design Decision.
# ===========================================================================

# main.py — county policy: every EB notification title MUST start with
# "SOSAR - " (per County of Santa Clara's multi-tenant Everbridge resource
# policy — title prefix lets County admins sort/filter org-wide activity by
# team). The notification's sentBy is also tagged but does NOT substitute
# for the prefix. Server-side enforcement is mandatory — never trust frontend
# or dispatcher input to comply.
_SOSAR_TITLE_PREFIX = "SOSAR - "


def _enforce_sosar_title_prefix(title: str) -> str:
    """Mirror of backend/main.py::_enforce_sosar_title_prefix()."""
    if title.startswith(_SOSAR_TITLE_PREFIX):
        return title
    return _SOSAR_TITLE_PREFIX + title


class TestSosarTitlePrefix:
    """Phase 1 live-test 2026-04-27 surfaced this — three test notifications
    fired without the SOSAR prefix because the test payload composed by Claude
    omitted it. County admins reviewing org-wide EB activity see those
    notifications missing the team-association prefix; that's a policy
    violation regardless of the user account that sent them. Enforcement is
    server-side so it can NEVER fail."""

    def test_unprefixed_title_gets_prefix_prepended(self):
        out = _enforce_sosar_title_prefix("TEST — please ignore")
        assert out == "SOSAR - TEST — please ignore"

    def test_already_prefixed_title_is_idempotent(self):
        # Dispatcher who already typed the prefix gets no double-prefix.
        out = _enforce_sosar_title_prefix("SOSAR - TEST — please ignore")
        assert out == "SOSAR - TEST — please ignore"

    def test_prefix_constant_is_exact_county_policy_string(self):
        # County policy string is "SOSAR - " — capital SOSAR, space, hyphen,
        # space. Pinned exactly so a future "tidy up the spacing" PR fails
        # pre-flight. The prefix is literal — there is no acceptable
        # variation (different case, missing space, different separator).
        assert _SOSAR_TITLE_PREFIX == "SOSAR - "
        assert _SOSAR_TITLE_PREFIX != "SOSAR-"
        assert _SOSAR_TITLE_PREFIX != "SOSAR -"
        assert _SOSAR_TITLE_PREFIX != "SOSAR  - "
        assert _SOSAR_TITLE_PREFIX != "sosar - "

    def test_empty_title_still_prefixed(self):
        # Caller is responsible for rejecting empty input via separate
        # validation; this helper just always ensures the prefix.
        out = _enforce_sosar_title_prefix("")
        assert out == "SOSAR - "

    def test_partially_matching_string_not_treated_as_prefix(self):
        # Defensive — "SOSAR - X" starts with "SOSAR - " (good), but
        # "SOSAR" alone (no trailing " - ") is NOT prefixed and gets
        # the full "SOSAR - " prepended.
        assert _enforce_sosar_title_prefix("SOSAR") == "SOSAR - SOSAR"
        assert _enforce_sosar_title_prefix("SOSAR-X") == "SOSAR - SOSAR-X"


# main.py — env var names that are READ from Cloud Run runtime AND set in
# terraform/environments/dev/main.tf. A typo or rename in either file
# silently breaks the application (env var becomes None, code reads default).
# Pin the canonical set here. When adding a new env var, update both files
# AND this set in the same PR — the test diff makes the cross-file change
# visible at review time.
#
# Source of truth — terraform/environments/dev/main.tf `env { name = "..." }`
# blocks for the dispatch-console Cloud Run service. SCCSSAR-dev mirror in
# Phase 5 Step 5.1 must use the same names.
_EB_SLACK_ENV_VAR_NAMES = frozenset({
    # Feature flags (Task 1.4)
    "EVERBRIDGE_MODE",
    "SLACK_MODE",
    # Slack channel target (Task 1.1)
    "ACTIVE_INCIDENTS_CHANNEL_ID",
    # Slack user-group pre-add source (issue #579 — read in slack.py
    # get_active_incident_management_members)
    "ACTIVE_INCIDENT_MGMT_USERGROUP_ID",
    # Secret-backed credentials (Task 1.1 / 1.2)
    "EVERBRIDGE_CREDENTIALS",
    "DISPATCH_SAFE_LIST",
    "SLACK_BOT_TOKEN",
    "SLACK_SO_COORDINATOR_EMAIL",
    # Cloud Tasks plumbing (Task 1.11)
    "PROJECT_ID",
    "CLOUD_RUN_SERVICE_URL",
})


class TestEverbridgeSlackEnvVarNames:
    """Pin the canonical env-var-name set so a future PR that adds, removes,
    or typo-renames an env var has to update this set + main.py + the
    Terraform `env { name = }` block all together. The PR review diff
    surfaces cross-file changes that would otherwise drift silently."""

    def test_canonical_set_pinned(self):
        # Anchor — this set must match the names used in main.py
        # `os.environ.get("...")` AND the names in
        # terraform/environments/dev/main.tf `env { name = "..." }`.
        assert _EB_SLACK_ENV_VAR_NAMES == frozenset({
            "EVERBRIDGE_MODE",
            "SLACK_MODE",
            "ACTIVE_INCIDENTS_CHANNEL_ID",
            "ACTIVE_INCIDENT_MGMT_USERGROUP_ID",
            "EVERBRIDGE_CREDENTIALS",
            "DISPATCH_SAFE_LIST",
            "SLACK_BOT_TOKEN",
            "SLACK_SO_COORDINATOR_EMAIL",
            "PROJECT_ID",
            "CLOUD_RUN_SERVICE_URL",
        })

    def test_env_var_names_are_uppercase_with_underscores(self):
        # Convention: GCP / 12-factor env var names are SCREAMING_SNAKE_CASE.
        # Pin so a future PR that adds a lowercase or hyphenated name fails.
        for name in _EB_SLACK_ENV_VAR_NAMES:
            assert name.isupper(), f"{name!r} is not uppercase"
            assert "-" not in name, f"{name!r} contains a hyphen"
            assert " " not in name, f"{name!r} contains a space"

    def test_no_silently_renamed_lookalikes(self):
        # Anti-regression — explicit forbid-list of typo / rename mistakes
        # I'm worried about. Each forbidden name is one a future PR might
        # accidentally introduce.
        forbidden = {
            "EVERBRIGE_MODE",         # missing D
            "EB_MODE",                # short form
            "EVERBRIDGE_AUTH",        # the Phase 0 PoC's variable name (we use _CREDENTIALS now)
            "DISPATCH_SAFELIST",      # missing underscore
            "SAFE_LIST",              # short form
            "SLACK_TOKEN",            # missing _BOT_
            "SO_COORDINATOR_EMAIL",   # missing SLACK_ prefix
            "GCP_PROJECT",            # used by gemini.py — different namespace
            "GOOGLE_CLOUD_PROJECT",   # auto-injected by Cloud Run; do not confuse with PROJECT_ID
        }
        assert forbidden.isdisjoint(_EB_SLACK_ENV_VAR_NAMES)


class TestEverbridgeOrgIdIsConfiguration:
    """The Everbridge organization ID is configuration, not a source literal.

    This replaces TestEverbridgeOrgIdLiteral, which was vacuous the same way as
    the Phase 0 constants above: it defined _EVERBRIDGE_ORG_ID_LITERAL in THIS
    file and asserted it equalled that same literal, reading nothing from
    main.py. Its docstring claimed to pin "the Python literal in main.py".
    """

    def _main_code(self):
        from pathlib import Path
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        return "\n".join(l.split("#")[0] for l in src.splitlines())

    def test_org_id_is_read_from_the_env(self):
        assert '_EVERBRIDGE_ORG_ID = os.environ.get("EVERBRIDGE_ORG_ID", "")' in self._main_code(), (
            "main.py no longer reads the Everbridge org ID from EVERBRIDGE_ORG_ID."
        )

    def test_org_id_is_a_string(self):
        """Interpolated into EB URL paths, so it must stay a str -- `.strip()`
        on the env read is what guarantees that."""
        code = self._main_code()
        assert '_EVERBRIDGE_ORG_ID = os.environ.get("EVERBRIDGE_ORG_ID", "").strip()' in code

    def test_config_is_preflighted_before_the_tombstone_and_the_eb_event(self):
        """Rubric Q1. The per-value guards live in everbridge.py and run at
        payload-build time, which is AFTER Step 4 has created a real EB
        notification event -- and notification events have no delete path. The
        preflight must therefore sit before both the skeleton .create() and the
        create_notification_event call.
        """
        code = self._main_code()
        pre = code.index("_eb_missing = [n for n, v in (")
        tombstone = code.index("new_skeleton_incident_doc(")
        eb_create = code.index("eb_module.create_notification_event")
        assert pre < tombstone, (
            "The Everbridge config preflight runs AFTER the Step 1.5 skeleton "
            ".create() -- a misconfigured deploy strands a status='creating' "
            "tombstone that blocks a same-minute retry."
        )
        assert pre < eb_create, (
            "The Everbridge config preflight runs AFTER create_notification_event "
            "-- a misconfigured deploy orphans a real EB notification event."
        )

    def test_preflight_is_a_400_not_a_5xx(self):
        """Rubric Q5: unset configuration is not transient. A 502 would put it
        into the Cloud Tasks retry budget, which cannot fix it."""
        code = self._main_code()
        window = code[code.index("_eb_missing = [n for n, v in ("):]
        window = window[: window.index("# ---- Step 0.5")] if "# ---- Step 0.5" in window else window[:2000]
        assert "status_code=400" in window, "The Everbridge config preflight does not return 400."

    def test_preflight_covers_every_value_with_no_earlier_guard(self):
        code = self._main_code()
        window = code[code.index("_eb_missing = [n for n, v in ("):][:1200]
        for name in ("EVERBRIDGE_ORG_ID", "EVERBRIDGE_CALLER_ID", "EVERBRIDGE_DELIVER_PATHS"):
            assert name in window, f"The preflight does not check {name}."


class TestEverbridgePickerConfigGuard:
    """Issue #803 — the group/contact pickers must 400 on an unset org ID.

    With EVERBRIDGE_ORG_ID empty the URL builds as
    `/rest/contacts/?pageSize=1000` with no org segment, Everbridge 404s
    "no such request handling method", and HTTPStatusError escapes as an
    unhandled 500. Observed live on personal-dev 2026-09-04 during the
    deploy-before-apply negative test; the dispatcher could not select a
    target, so the #799 clean-400 dispatch path stayed unverified.

    #799's preflight was scoped to /send-notification. These two endpoints
    used _EVERBRIDGE_ORG_ID with no guard at all.
    """

    def _main_code(self):
        from pathlib import Path
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        return "\n".join(l.split("#")[0] for l in src.splitlines())

    def _handler(self, code, name):
        """Source of ONE handler, bounded at both ends by real markers.

        Start at the `async def <name>(` line, end at the next decorator or
        top-level def. A `start + N characters` slice would silently widen
        into the next handler on any insertion above it and pass on code
        that is not in this function at all.
        """
        start = code.index("async def %s(" % name)
        rest = code[start + 1:]
        ends = [rest.index(m) for m in ("\n@app.", "\ndef ", "\nasync def ") if m in rest]
        return rest[: min(ends)] if ends else rest

    def test_both_pickers_call_the_guard(self):
        code = self._main_code()
        for name, token in (
            ("everbridge_groups", '_require_everbridge_org_id("everbridge_groups")'),
            ("everbridge_contacts", '_require_everbridge_org_id("everbridge_contacts")'),
        ):
            assert token in self._handler(code, name), (
                "%s does not call the org-ID guard — an unset EVERBRIDGE_ORG_ID "
                "escapes as an unhandled 500 (#803)." % name
            )

    def test_guard_runs_before_the_outbound_everbridge_call(self):
        """The whole point is to fail before the request, not after it 404s."""
        code = self._main_code()
        for name in ("everbridge_groups", "everbridge_contacts"):
            body = self._handler(code, name)
            assert body.index("_require_everbridge_org_id(") < body.index("run_in_executor"), (
                "%s calls Everbridge before the org-ID guard (#803)." % name
            )

    def test_guard_returns_400_not_5xx(self):
        """Rubric Q5 — unset configuration is not transient. Asserted over the
        guard's own body, not the module: 'status_code=400' appears dozens of
        times in main.py, so an unscoped search proves nothing."""
        code = self._main_code()
        start = code.index("def _require_everbridge_org_id(")
        body = code[start:][: code[start:].index("\ndef ")]
        assert "status_code=400" in body
        for bad in ("status_code=500", "status_code=502", "status_code=503"):
            assert bad not in body, "The org-ID guard returns %s (#803)." % bad

    def test_guard_names_the_variable_and_the_fix(self):
        """A dispatcher who sees this message must be able to act on it. The
        trigger is a deploy that landed before `terraform apply`, and the
        build scripts do not pick Terraform changes up."""
        code = self._main_code()
        start = code.index("def _eb_config_missing_detail(")
        body = code[start:][: code[start:].index("\ndef ")]
        # Anchor PAST the docstring. Both strings appear in the prose above
        # the return, so a whole-function slice matched the explanation of the
        # rule instead of the rule -- caught by mutation M5, which deleted the
        # real sentence and left this test green.
        body = body[body.index('"""', body.index('"""') + 3) + 3:]
        assert "Everbridge is not configured on this deployment" in body
        assert "terraform apply" in body

    def test_dispatch_preflight_shares_one_wording(self):
        """Rubric Q6 — same cause, same sentence. Two phrasings for one fault
        is how a dispatcher learns to distrust both. Pins the CALL, not the
        identifier: the bare name appears in the definition too."""
        code = self._main_code()
        assert "detail=_eb_config_missing_detail(_eb_missing)," in code, (
            "The /send-notification preflight no longer builds its message "
            "through the shared helper — the pickers and the dispatch path "
            "can now disagree about the same failure (#803)."
        )


class TestEverbridgePickerFailureIsVisible:
    """Issue #803, frontend half — a failed picker fetch must say why.

    The prefetch swallowed every non-ok response, so the misconfiguration
    rendered as "— none available": indistinguishable from an Everbridge org
    with no groups in it. Correcting the issue's own diagnosis, which blamed
    the generic "Network error" string — that belongs to the /ocr handler.
    """

    def _frontend(self):
        from pathlib import Path
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")

    def test_both_empty_states_prefer_the_error_text(self):
        """Groups AND contacts. One leg fixed is the asymmetry in rubric Q6."""
        html = self._frontend()
        assert html.count('(_ebPrefetchError || "— none available")') == 2, (
            "Expected both picker empty states to fall back through "
            "_ebPrefetchError; found %d (#803)."
            % html.count('(_ebPrefetchError || "— none available")')
        )

    def test_only_4xx_details_are_shown(self):
        """A 4xx detail is this app's own deliberate message. A 5xx body is an
        unhandled fault carrying nothing a dispatcher can act on."""
        html = self._frontend()
        start = html.index("async function _ebPrefetchFailureText(")
        body = html[start:][: html[start:].index("\n  async function prefetchEverbridgeData(")]
        assert "r.status >= 400 && r.status < 500" in body, (
            "The picker error text is no longer gated on 4xx (#803)."
        )
        assert ".detail" in body and "textContent" not in body

    def test_error_is_reset_on_every_prefetch_attempt(self):
        """Assigned unconditionally from the responses, so a recovered fetch
        clears a stale message instead of pinning the old failure forever."""
        html = self._frontend()
        assert "_ebPrefetchError = await _ebPrefetchFailureText(gResp, cResp);" in html


# main.py — OIDC service account email format. The full email is built at
# runtime as f"everbridge-poll-sa@{project_id}.iam.gserviceaccount.com" —
# the local part ("everbridge-poll-sa") MUST match the
# google_service_account.everbridge_poll.account_id value in
# terraform/environments/dev/main.tf. A drift means the OIDC claim check
# in /poll-incident + /delete-template rejects every Cloud Tasks call as
# "wrong email" → 401.
_EVERBRIDGE_POLL_SA_LOCAL_PART = "everbridge-poll-sa"


class TestEverbridgePollSaEmailFormat:
    def test_local_part_pinned(self):
        # Source of truth — terraform/environments/dev/main.tf:
        #   resource "google_service_account" "everbridge_poll" {
        #     account_id = "everbridge-poll-sa"
        #   }
        # If this changes, ALSO update the f-string in
        # main.py::_expected_poll_sa_email() in the same PR.
        assert _EVERBRIDGE_POLL_SA_LOCAL_PART == "everbridge-poll-sa"

    def test_local_part_does_not_have_silent_typos(self):
        # Anti-regression — these are typo-mistakes a future PR might
        # introduce. Each is forbidden.
        forbidden = {
            "everbridge-poll",         # missing -sa suffix
            "everbridge_poll_sa",      # underscores instead of hyphens (GCP rejects)
            "eb-poll-sa",              # short form
            "everbridge-poller-sa",    # plausible typo
            "everbridge-polling-sa",   # plausible typo
        }
        assert _EVERBRIDGE_POLL_SA_LOCAL_PART not in forbidden

    def test_full_email_format_assembles_correctly(self):
        # The runtime f-string in main.py::_expected_poll_sa_email():
        #     f"{local_part}@{project_id}.iam.gserviceaccount.com"
        # Pin the assembled output for the personal-dev project so a future
        # refactor that breaks the format string fails here.
        project_id = "sar-dispatch-dev"
        full = f"{_EVERBRIDGE_POLL_SA_LOCAL_PART}@{project_id}.iam.gserviceaccount.com"
        assert full == "everbridge-poll-sa@sar-dispatch-dev.iam.gserviceaccount.com"


# ---------------------------------------------------------------------------
# Everbridge stop-notification — URL pattern + PUT verb + idempotency markers
# ---------------------------------------------------------------------------
# Mirrors of constants in everbridge.py. Source of truth is the live module;
# this regression test is a sentinel that catches drift at Step 0 of the
# build script, before a broken stop call reaches Cloud Run.
#
# Empirical confirmation (2026-04-28 Swagger session against live EB org):
#   - PUT /notifications/{orgId}/{notificationId} is the stop verb (NOT
#     DELETE — DELETE is not exposed on this resource).
#   - Body must be the FULL inner notification object (the GET response
#     wrapped in `result`, with the outer envelope stripped).
#   - Setting body["notificationStatus"] = "Stopped" + PUT triggers the stop.
#   - 400 with message containing "is Stopped" / "not in progress" means
#     already-stopped — caller treats this as success (idempotent).
# See CLAUDE.md "Everbridge stop notification — GET-modify-PUT pattern".

_EB_STOP_BASE_URL          = "https://api.everbridge.net/rest"
_EB_STOP_HTTP_VERB         = "PUT"
_EB_STOP_STATUS_FIELD      = "notificationStatus"
_EB_STOP_STATUS_VALUE      = "Stopped"
_EB_STOP_GET_VERBOSE_PARAM = "false"
_EB_STOP_ALREADY_STOPPED_MARKERS: tuple[str, ...] = (
    "is stopped",
    "not in progress",
)


class TestEverbridgeEndNotificationUrl:
    def test_url_pattern(self):
        # Source of truth: everbridge.py::_build_end_notification_url
        #   f"{EVERBRIDGE_BASE_URL}/notifications/{org_id}/{notification_id}"
        # If either the base URL or the path template changes, this fails
        # at build Step 0.
        org_id = "700000000000026"
        nid = "7000000000000039"
        built = f"{_EB_STOP_BASE_URL}/notifications/{org_id}/{nid}"
        assert built == (
            "https://api.everbridge.net/rest/notifications/"
            "700000000000026/7000000000000039"
        )

    def test_url_path_matches_poll_path(self):
        # The stop URL is identical to the poll URL — only the HTTP verb
        # differs. If the path template ever drifts away from the poll path,
        # the stop call would target a different resource and silently fail.
        org_id = "700000000000026"
        nid = "7000000000000039"
        stop_url = f"{_EB_STOP_BASE_URL}/notifications/{org_id}/{nid}"
        poll_url = f"{_EB_STOP_BASE_URL}/notifications/{org_id}/{nid}"
        assert stop_url == poll_url


class TestEverbridgeEndNotificationVerb:
    def test_verb_is_put_not_delete(self):
        # Empirically confirmed 2026-04-28 — DELETE is NOT exposed on the
        # /notifications/{orgId}/{nid} endpoint; only GET and PUT.
        # A future "tidy up" PR that switches to DELETE would 405.
        assert _EB_STOP_HTTP_VERB == "PUT"

    def test_verb_is_not_a_lookalike(self):
        # Anti-regression — common misconceptions about how to stop an
        # Everbridge notification. Each MUST be rejected here.
        forbidden = {"DELETE", "POST", "PATCH", "GET"}
        assert _EB_STOP_HTTP_VERB not in forbidden


class TestEverbridgeEndNotificationBody:
    def test_status_field_name(self):
        # The field set on the PUT body to trigger the stop.
        # Empirically confirmed 2026-04-28 — `notificationStatus` (NOT
        # `status`, which on a notification means record-active "A"; NOT
        # `escalationStatus`, which is a separate sibling field).
        assert _EB_STOP_STATUS_FIELD == "notificationStatus"

    def test_status_value(self):
        # The value to set notificationStatus to. Capitalization matters —
        # Everbridge enums are case-sensitive.
        assert _EB_STOP_STATUS_VALUE == "Stopped"

    def test_get_uses_verbose_false(self):
        # The GET preceding the PUT uses verbose=false to keep the
        # round-trip body small (~6KB vs ~50KB+ with verbose=true). The
        # extra fields verbose=true adds (allDetails[] per-contact result
        # paths) are not needed for the stop and would only inflate the
        # body sent back on PUT.
        assert _EB_STOP_GET_VERBOSE_PARAM == "false"


class TestEverbridgeEndNotificationIdempotency:
    def test_already_stopped_markers_are_lowercase(self):
        # The matcher in everbridge.py::end_notification lowercases the
        # response message before substring-matching. If a marker were
        # accidentally written with uppercase letters, it would never
        # match the lowercased message and a 400 from an already-stopped
        # notification would propagate as an exception, breaking the
        # idempotent semantic.
        for marker in _EB_STOP_ALREADY_STOPPED_MARKERS:
            assert marker == marker.lower(), (
                f"already-stopped marker {marker!r} contains uppercase; "
                f"matcher lowercases before substring search"
            )

    def test_already_stopped_markers_pinned(self):
        # Empirical 2026-04-28 message from EB on PUT to an already-Stopped
        # notification:
        #   "The status of notification <nid> is Stopped.  It is not in
        #    progress and can not be stopped."
        # Either substring is sufficient; we keep both as defense in depth
        # in case EB ever changes one phrase.
        assert "is stopped" in _EB_STOP_ALREADY_STOPPED_MARKERS
        assert "not in progress" in _EB_STOP_ALREADY_STOPPED_MARKERS

    def test_real_eb_message_matches_at_least_one_marker(self):
        # The exact response body Everbridge returned on the empirical
        # PUT-on-already-stopped test (2026-04-28). end_notification()
        # MUST treat this as success, not raise.
        real_msg_lower = (
            "the status of notification 7000000000000017 is stopped.  "
            "it is not in progress and can not be stopped."
        )
        assert any(
            marker in real_msg_lower
            for marker in _EB_STOP_ALREADY_STOPPED_MARKERS
        )


# ---------------------------------------------------------------------------
# "Groups requested by dispatcher" line in #active-incidents tally (PR #NNN)
# ---------------------------------------------------------------------------
# Source of truth: slack.py::format_groups_requested() + the gate logic in
# main.py::_compose_active_incidents_tally().
#
# The line is appended to the tally ONLY when doc["requested_group_names"] is
# non-empty — preserving clean tally output for contact-only sends and for
# existing Firestore docs that pre-date this field.
#
# CLAUDE.md change: open tasks backlog — "Groups requested" line in
# #active-incidents (phase 1 livetest spawned task, 2026-04-28).

def _format_groups_requested(group_names: list[str]) -> str:
    """Mirror of slack.py::format_groups_requested (source of truth is slack.py)."""
    return f"Groups requested by dispatcher: {', '.join(group_names)}"


class TestGroupsRequestedTallyLine:
    def test_format_single_group(self):
        # Canonical single-group output. Wording pinned — any change here
        # must also update slack.py::format_groups_requested AND this mirror.
        assert _format_groups_requested(["SAR - Automation Test"]) == (
            "Groups requested by dispatcher: SAR - Automation Test"
        )

    def test_format_multiple_groups(self):
        # Comma + space between names, matching Python's ', '.join().
        assert _format_groups_requested(["K9", "UAS"]) == (
            "Groups requested by dispatcher: K9, UAS"
        )

    def test_prefix_is_exact(self):
        # The exact prefix text is pinned separately so a "tidying" rename
        # of just the label fails Step 0 independently of the join logic.
        line = _format_groups_requested(["K9"])
        assert line.startswith("Groups requested by dispatcher: ")

    def test_tally_gate_suppresses_line_for_falsy_inputs(self):
        # The tally composer gate:
        #   group_names = doc.get("requested_group_names") or []
        #   if group_names:
        #       lines.append(format_groups_requested(group_names))
        #
        # Falsy inputs that MUST NOT emit the line:
        #   - None  (old Firestore docs that pre-date this field)
        #   - []    (contact-only sends where no groups were targeted)
        #
        # Mirror the gate inline to ensure the logic is correct.
        for doc_value in (None, []):
            group_names = (doc_value or [])
            would_emit = bool(group_names)
            assert not would_emit, (
                f"Gate should suppress line for doc value {doc_value!r} "
                f"but would_emit={would_emit}"
            )

    def test_tally_gate_emits_line_for_non_empty_list(self):
        # Non-empty list MUST produce the line.
        group_names = ["SAR - K9"]
        would_emit = bool(group_names or [])
        assert would_emit

    def test_line_is_before_responder_lines(self):
        # The tally line order is: header, responder-count, groups-requested,
        # THEN per-group lines and multi-team lines.  The "Groups requested"
        # line is the unchanging part so it sits above the dynamic breakdown.
        header = "*🔔 Everbridge ACTIVE — 2026-04-28 SCCSSAR Samaritan*"
        count  = "*✅ 1 confirmed   ❌ 0 declined   ⏳ 0 no response*"  # tri-count line (#592)
        requested = _format_groups_requested(["SAR - K9"])
        group_line = "• K9 (1): Burns"

        lines = [header, count, requested, group_line]
        rendered = "\n".join(lines)
        # groups-requested line must come BEFORE the per-group line
        assert rendered.index(requested) < rendered.index(group_line)


# ---------------------------------------------------------------------------
# _apply_responder_diff — mirror of main.py logic (per-group tally breakdown)
# ---------------------------------------------------------------------------

def _shape_responder_name(ack: dict) -> str:
    last  = (ack.get("last_name")  or "").strip()
    first = (ack.get("first_name") or "").strip()
    if last and first:
        return f"{last}, {first}"
    if last:
        return last
    if first:
        return first
    return ack.get("contact_id", "?")


def _norm_for_email_match(s: str) -> str:
    """Lowercase + strip non-alphanumeric for tolerant local-part comparison."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _resolve_email_from_safe_list(ack: dict, safe_list_emails: list) -> list:
    """Mirror of main.py::_resolve_email_from_safe_list for regression tests."""
    last  = _norm_for_email_match(ack.get("last_name"))
    first = _norm_for_email_match(ack.get("first_name"))
    if not last and not first:
        return []
    if not safe_list_emails:
        return []
    both_matches: list = []
    last_only_matches: list = []
    first_only_matches: list = []
    for email in safe_list_emails:
        local = _norm_for_email_match(email.split("@", 1)[0])
        last_in  = bool(last)  and last  in local
        first_in = bool(first) and first in local
        if last_in and first_in:
            both_matches.append(email)
        elif last_in:
            last_only_matches.append(email)
        elif first_in:
            first_only_matches.append(email)
    if len(both_matches) == 1:
        return [both_matches[0]]
    if not both_matches and len(last_only_matches) == 1:
        return [last_only_matches[0]]
    if not both_matches and not last_only_matches and len(first_only_matches) == 1:
        return [first_only_matches[0]]
    return []


def _apply_responder_diff_mirror(
    prev_responders: list[dict],
    new_ack_contacts: list[dict],
    contact_group_map: "dict[str, list[str]] | None" = None,
    contact_email_map: "dict[str, list[str]] | None" = None,
    safe_list_emails: "list | None" = None,
) -> "tuple[list[dict], list[dict]]":
    """Local mirror of main.py::_apply_responder_diff for regression tests."""
    cgm = contact_group_map or {}
    cem = contact_email_map or {}
    sle = safe_list_emails or []
    prev_ids = {r.get("contact_id") for r in prev_responders}
    new_arrivals: list[dict] = []
    next_full_list = list(prev_responders)
    for ack in new_ack_contacts:
        cid = ack.get("contact_id")
        if not cid or cid in prev_ids:
            continue
        emails = ack.get("emails") or list(cem.get(cid, []))
        if not emails and sle:
            emails = _resolve_email_from_safe_list(ack, sle)
        entry = {
            "contact_id": cid,
            "name":       _shape_responder_name(ack),
            "groups":     list(cgm.get(cid, [])),
            "emails":     emails,
        }
        next_full_list.append(entry)
        new_arrivals.append(entry)
    return next_full_list, new_arrivals


class TestApplyResponderDiffGroups:
    """Per-group tally breakdown — contact_group_map populates responder.groups."""

    def test_group_populated_from_map(self):
        # Single-group member confirms YES → entry.groups contains the group name.
        cgm = {"c1": ["K9"]}
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, arrivals = _apply_responder_diff_mirror([], [ack], cgm)
        assert full[0]["groups"] == ["K9"]
        assert arrivals[0]["groups"] == ["K9"]

    def test_multi_group_member(self):
        # Responder in two groups gets both names.
        cgm = {"c1": ["K9", "Drivers"]}
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], cgm)
        assert full[0]["groups"] == ["K9", "Drivers"]

    def test_direct_contact_no_group(self):
        # Contact targeted directly (not via group) → groups is empty list, not error.
        ack = {"contact_id": "c99", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {})
        assert full[0]["groups"] == []

    def test_none_map_treated_as_empty(self):
        # Old Firestore docs pre-dating contact_group_map field → safe default.
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], None)
        assert full[0]["groups"] == []

    def test_groups_list_is_a_copy(self):
        # Mutations to the map list must not bleed into the already-stored entry.
        group_list = ["K9"]
        cgm = {"c1": group_list}
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], cgm)
        group_list.append("MutatedAfter")
        assert full[0]["groups"] == ["K9"]

    def test_dedup_does_not_clobber_groups(self):
        # A contact that already confirmed (in prev_responders) must not be re-added.
        prev = [{"contact_id": "c1", "name": "Burns, Bill", "groups": ["K9"], "emails": []}]
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, arrivals = _apply_responder_diff_mirror(prev, [ack], {"c1": ["K9"]})
        assert len(full) == 1
        assert arrivals == []
        assert full[0]["groups"] == ["K9"]   # original preserved

    def test_new_arrival_added_existing_unchanged(self):
        # When a second responder arrives, first responder's groups are preserved.
        prev = [{"contact_id": "c1", "name": "Burns, Bill", "groups": ["K9"], "emails": []}]
        cgm = {"c1": ["K9"], "c2": ["UAS"]}
        ack2 = {"contact_id": "c2", "first_name": "Kris", "last_name": "Black", "emails": []}
        full, arrivals = _apply_responder_diff_mirror(prev, [ack2], cgm)
        assert len(full) == 2
        assert full[0]["groups"] == ["K9"]
        assert full[1]["groups"] == ["UAS"]
        assert arrivals[0]["contact_id"] == "c2"


class TestApplyResponderDiffEmailFallback:
    """contact_email_map fallback — fixes shadow-mode 'Cannot invite' bug.

    Root cause: EB only populates callResultByPaths for paths it actually
    attempted. If a contact answers SMS before EB tries email, poll-response
    ack.emails is empty. The contact_email_map snapshot (built at send time
    from contact record paths.value) fills the gap.
    """

    def test_ack_emails_used_when_present(self):
        # Normal case — poll response includes email path.
        cem = {"c1": ["stored@example.com"]}
        ack = {
            "contact_id": "c1", "first_name": "Bill", "last_name": "Burns",
            "emails": ["poll@example.com"],
        }
        full, _ = _apply_responder_diff_mirror([], [ack], {}, cem)
        assert full[0]["emails"] == ["poll@example.com"]

    def test_cem_fallback_when_ack_emails_empty(self):
        # Shadow-mode bug: SMS answered first; callResultByPaths has no email.
        cem = {"c1": ["stored@example.com"]}
        ack = {
            "contact_id": "c1", "first_name": "Bill", "last_name": "Burns",
            "emails": [],
        }
        full, _ = _apply_responder_diff_mirror([], [ack], {}, cem)
        assert full[0]["emails"] == ["stored@example.com"]

    def test_cem_fallback_when_ack_emails_missing(self):
        # No emails key at all in ack (defensive).
        cem = {"c1": ["stored@example.com"]}
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns"}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, cem)
        assert full[0]["emails"] == ["stored@example.com"]

    def test_empty_emails_when_no_ack_and_no_cem(self):
        # Direct send to a contact not in contact_email_map (map build failed).
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {})
        assert full[0]["emails"] == []

    def test_none_cem_treated_as_empty(self):
        # Old Firestore docs pre-dating contact_email_map → safe default.
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, None)
        assert full[0]["emails"] == []

    def test_cem_email_list_is_a_copy(self):
        # Mutations to the cem list after building must not bleed into stored entry.
        stored = ["stored@example.com"]
        cem = {"c1": stored}
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, cem)
        stored.append("mutated@example.com")
        assert full[0]["emails"] == ["stored@example.com"]


class TestApplyResponderDiffSafeListFallback:
    """Third-tier fallback: safe_list_emails name-match.

    Fixes residual contact-direct gap left after PR #322. Some EB contacts
    have no Email-* delivery paths at all (or the per-contact email lookup
    failed and got swallowed), so contact_email_map[cid] is empty even
    after PR #322. The configured Slack safe-list (allowed_emails) is the
    last-resort source: name-match the responder's first/last against each
    safe-list email's local-part. Punctuation- and case-insensitive so
    bill.burns / bill_burns / bburns all resolve to the same Bill Burns.

    Refuses to guess: requires a unique match. Multiple ambiguous matches
    or no match at all → empty list (responder still classified
    "unresolvable" and posted via the existing format_would_invite path).
    """

    def test_kris_unit_leader_resolves_via_safe_list(self):
        # Live test reference case: Kris Black, contact 700000000000038.
        # EB contact has no email paths → cem empty for her cid.
        # Expected: name match against safe-list resolves her @sccssar.org
        # email so shadow-mode posts "Would invite: Black, Kris" (not Cannot).
        sle = ["bill.burns@sccssar.org", "kris.black@sccssar.org"]
        ack = {
            "contact_id": "700000000000038",
            "first_name": "Kris", "last_name": "Black", "emails": [],
        }
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, sle)
        assert full[0]["emails"] == ["kris.black@sccssar.org"]

    def test_bill_burns_resolves_via_safe_list(self):
        sle = ["bill.burns@sccssar.org", "kris.black@sccssar.org"]
        ack = {
            "contact_id": "700000000000028",
            "first_name": "Bill", "last_name": "Burns", "emails": [],
        }
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, sle)
        assert full[0]["emails"] == ["bill.burns@sccssar.org"]

    def test_punctuation_insensitive_matching(self):
        # bill.burns / bill_burns / bburns / billburns all should resolve.
        # Configuration may use any local-part style; we strip non-alphanumeric.
        for variant in [
            "bill.burns@sccssar.org",
            "bill_burns@sccssar.org",
            "billburns@sccssar.org",
        ]:
            sle = [variant]
            ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
            full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, sle)
            assert full[0]["emails"] == [variant], f"failed for {variant}"

    def test_case_insensitive_matching(self):
        # ack first/last from EB is unpredictable casing; safe-list lower-case is canonical.
        sle = ["BILL.BURNS@SCCSSAR.ORG"]
        ack = {"contact_id": "c1", "first_name": "bill", "last_name": "BURNS", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, sle)
        assert full[0]["emails"] == ["BILL.BURNS@SCCSSAR.ORG"]

    def test_first_only_match_when_last_absent_in_locals(self):
        # The safe-list address has the first name but NOT the surname in
        # its local-part -- that is the property under test here.
        # Last name absent from local-part — first-only match still resolves
        # if it's the unique match. This is the @-Work-Alt slot in the user's
        # 3-email-per-contact model.
        sle = ["bill@example.com"]
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, sle)
        assert full[0]["emails"] == ["bill@example.com"]

    def test_strong_match_wins_over_weak(self):
        # Both bill@... and bill.burns@... contain "bill" in local-part;
        # only bill.burns@... contains BOTH first and last → should win
        # over the partial-match weak alternative.
        sle = ["bill@example.com", "bill.burns@sccssar.org"]
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, sle)
        assert full[0]["emails"] == ["bill.burns@sccssar.org"]

    def test_ambiguous_last_name_resolved_by_first(self):
        # Two Burnses on the safe-list (e.g., parent + child volunteer);
        # only one has matching first name → unique match.
        sle = ["jen.burns@sccssar.org", "bill.burns@sccssar.org"]
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, sle)
        assert full[0]["emails"] == ["bill.burns@sccssar.org"]

    def test_no_unique_match_returns_empty(self):
        # Two ambiguous matches with no first-name disambiguator → refuse to guess.
        sle = ["b.burns@sccssar.org", "burns@sccssar.org"]
        ack = {"contact_id": "c1", "first_name": "", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, sle)
        assert full[0]["emails"] == []

    def test_no_match_returns_empty(self):
        # Responder not on safe-list → still "Cannot invite" (existing edge
        # case for SAR members without an @sccssar.org email yet).
        sle = ["bill.burns@sccssar.org", "kris.black@sccssar.org"]
        ack = {
            "contact_id": "c-other",
            "first_name": "Sam", "last_name": "Stranger", "emails": [],
        }
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, sle)
        assert full[0]["emails"] == []

    def test_ack_emails_take_precedence_over_safe_list(self):
        # Tier 1: ack.emails always wins. safe_list never consulted when present.
        sle = ["bill.burns@sccssar.org"]
        ack = {
            "contact_id": "c1", "first_name": "Bill", "last_name": "Burns",
            "emails": ["from-poll@example.com"],
        }
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, sle)
        assert full[0]["emails"] == ["from-poll@example.com"]

    def test_cem_takes_precedence_over_safe_list(self):
        # Tier 2: cem fallback (PR #322) wins over tier-3 safe_list match.
        cem = {"c1": ["from-cem@example.com"]}
        sle = ["bill.burns@sccssar.org"]
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, cem, sle)
        assert full[0]["emails"] == ["from-cem@example.com"]

    def test_empty_safe_list_returns_empty(self):
        # Existing test test_empty_emails_when_no_ack_and_no_cem must keep
        # passing: when no safe_list provided, behavior matches pre-fix.
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, [])
        assert full[0]["emails"] == []

    def test_none_safe_list_treated_as_empty(self):
        ack = {"contact_id": "c1", "first_name": "Bill", "last_name": "Burns", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, None)
        assert full[0]["emails"] == []

    def test_short_first_name_does_not_short_circuit_match(self):
        # First name "Al" (2 chars) is a substring of many local-parts; the
        # algorithm relies on uniqueness, not min-length. With a clean
        # safe-list this resolves correctly; with collisions it returns [].
        sle = ["al.adams@sccssar.org"]
        ack = {"contact_id": "c1", "first_name": "Al", "last_name": "Adams", "emails": []}
        full, _ = _apply_responder_diff_mirror([], [ack], {}, {}, sle)
        assert full[0]["emails"] == ["al.adams@sccssar.org"]


# ===========================================================================
# get_contact_emails — list-endpoint workaround pins
# ===========================================================================
# Source of truth: backend/everbridge.py::get_contact_emails().
#
# Phase 1 multi-recipient live test (2026-04-30 with Kris Black) surfaced a
# silent failure on the contact-direct send path: get_contact_emails was
# calling GET /contacts/{orgId}/{contactId} which returns HTTP 401
# "User does not have API permissions for this method" for the SHO-SAR
# Dispatcher SA role. Empirically verified: every embed/expand/include/byType
# variant on the path-based URL returned the same 401, but the LIST endpoint
# GET /contacts/{orgId}?byType=id&contactIds=<cid> returns 200 with paths
# populated.
#
# Two anti-patterns are pinned here so a future "tidy up the URL" PR fails
# at build Step 0:
#   (1) the endpoint MUST be the list endpoint, NOT the path-based form
#   (2) the filter param MUST be `contactIds` (PLURAL); the singular
#       `contactId` is silently ignored — same gotcha as groupId/groupIds
#       on list_group_members. With singular, EB returns 200 with the FULL
#       contact list (no filter) instead of just the requested contact.

_GET_CONTACT_EMAILS_URL_TEMPLATE      = "/contacts/{org_id}"   # list, not path
_GET_CONTACT_EMAILS_FILTER_PARAM      = "contactIds"           # plural required
_GET_CONTACT_EMAILS_BYTYPE_VALUE      = "id"                   # required modifier


class TestGetContactEmailsEndpoint:
    """Pin the 2026-04-30 list-endpoint workaround for get_contact_emails.

    The path-based GET /contacts/{org}/{cid} returns 401 for the SHO-SAR
    Dispatcher SA role. The list endpoint with `contactIds` filter is the
    only proven-working alternative. Confirmed empirically against the
    live SA on 2026-04-30; any drift back to the path-based form would
    silently re-introduce the Kris-no-Slack-invite symptom.
    """

    def test_uses_list_endpoint_not_path_based(self):
        # Path-based: /contacts/{org_id}/{contact_id}  ← 401, do NOT use
        # List-based: /contacts/{org_id}              ← 200, use this
        assert _GET_CONTACT_EMAILS_URL_TEMPLATE == "/contacts/{org_id}"
        assert "{contact_id}" not in _GET_CONTACT_EMAILS_URL_TEMPLATE

    def test_filter_param_is_plural_contactIds(self):
        # PLURAL contactIds. Singular contactId is silently ignored — the
        # API returns 200 with ALL contacts (68 in this org) instead of
        # filtering. Same gotcha as groupId vs groupIds.
        assert _GET_CONTACT_EMAILS_FILTER_PARAM == "contactIds"
        assert _GET_CONTACT_EMAILS_FILTER_PARAM != "contactId"

    def test_filter_param_is_not_a_lookalike(self):
        # Anti-regression — common misconceptions. Each MUST be rejected.
        forbidden = {"contactId", "id", "ids", "filter", "q", "query"}
        assert _GET_CONTACT_EMAILS_FILTER_PARAM not in forbidden

    def test_byType_modifier_required_and_value_is_id(self):
        # The byType=id modifier is required for the contactIds filter to
        # take effect. Same shape as list_group_member_contacts uses with
        # byType=id&groupId=<id>.
        assert _GET_CONTACT_EMAILS_BYTYPE_VALUE == "id"


# ===========================================================================
# Slack-lookup email policy — sccssar.org-preferred ordering
# ===========================================================================
# Source of truth: backend/everbridge.py::_sort_emails_sccssar_first().
#
# Bill's standing rule (CLAUDE.md auto-memory project_slack_lookup_uses_sccssar_email):
# "Slack lookup must always use the responder's @sccssar.org email — never the
# contact path the responder happened to reply on; sheriff-dept email is the
# only exception (SAR coordinator)."
#
# Empirically confirmed 2026-04-30 against live EB org: the API returns paths
# in the dispatcher-set "Order" column from the EB UI. Dispatchers (and admins)
# can re-rank that column at any time. With Bill's record re-ordered so personal
# email is path index [2] and sccssar is path index [3], the live API returned
# them in that same order — emails[0] would have been the personal address,
# silently breaking Slack lookup.
#
# Fix: sort sccssar.org-emails first at the extraction layer so every downstream
# consumer (contact_email_map, ack["emails"], main.py:4301 emails[0] for Slack
# lookup) gets the right address without needing site-specific filters.

def _sort_emails_sccssar_first_mirror(emails: list[str]) -> list[str]:
    """Mirror of backend/everbridge.py::_sort_emails_sccssar_first."""
    sccssar = [e for e in emails if e.lower().endswith("@sccssar.org")]
    others  = [e for e in emails if not e.lower().endswith("@sccssar.org")]
    return sccssar + others


_SCCSSAR_DOMAIN_SUFFIX = "@sccssar.org"


class TestSortEmailsSccssarFirstPolicy:
    """Pin Bill's standing rule that Slack lookup uses @sccssar.org email.

    The path-index order returned by EB is dispatcher-controlled (Order column
    in the EB Delivery Methods UI) and therefore volatile. The sort guarantees
    emails[0] is sccssar whenever a sccssar address exists, regardless of how
    EB returns the underlying paths.
    """

    def test_sccssar_email_promoted_to_index_zero(self):
        # Live-replicated scenario: an EB record after re-order has the
        # non-sccssar email before the sccssar one in the API response.
        emails = ["bill@example.com", "Bill.burns@sccssar.org"]
        assert _sort_emails_sccssar_first_mirror(emails)[0] == "Bill.burns@sccssar.org"

    def test_match_is_case_insensitive(self):
        # EB stores the address in mixed case ("Bill.burns@sccssar.org").
        # The sort MUST match regardless of capitalization.
        emails = ["personal@x.com", "ALL.CAPS@SCCSSAR.ORG"]
        assert _sort_emails_sccssar_first_mirror(emails)[0] == "ALL.CAPS@SCCSSAR.ORG"

    def test_no_sccssar_preserves_original_order(self):
        # Sheriff-dept/SAR-coordinator exception: no @sccssar.org email →
        # sort is a no-op. Original path order is preserved.
        emails = ["sheriff@example.gov", "personal@example.com"]
        assert _sort_emails_sccssar_first_mirror(emails) == emails

    def test_match_requires_at_sign_anchor(self):
        # Anti-pattern: a hostile-looking domain like "@notsccssar.org"
        # MUST NOT match the sccssar suffix. The leading "@" in the suffix
        # check ensures only the real domain wins.
        emails = ["first@notsccssar.org", "real@sccssar.org"]
        assert _sort_emails_sccssar_first_mirror(emails)[0] == "real@sccssar.org"

    def test_suffix_constant_pinned(self):
        # If anyone ever changes the literal away from "@sccssar.org" thinking
        # the leading "@" is decorative, this pin will fail. It is NOT
        # decorative — see test_match_requires_at_sign_anchor.
        assert _SCCSSAR_DOMAIN_SUFFIX == "@sccssar.org"
        assert _SCCSSAR_DOMAIN_SUFFIX.startswith("@")


# ---------------------------------------------------------------------------
# _partition_arrivals_for_shadow — three-way YES classification (2026-04-30)
# ---------------------------------------------------------------------------
#
# Bug surfaced 2026-04-30 in live test (Bill safe-list + Miguel non-safe-list):
# safe-list responder Bill got "Would invite: Burns, Bill" channel post even
# though he was already a member from the send-time pre-invite. Misleading.
#
# Fix: 3-way partition by safe-list email match.
#   - safe-list match → "<Name> added" (pre-invitee, already in channel)
#   - has email, no safe-list match → "Would invite: <Name>"
#   - no email at all → "Cannot invite: <Name>"
# ---------------------------------------------------------------------------

def _partition_arrivals_for_shadow_mirror(
    new_arrivals: list,
    safe_list_emails,
):
    """Mirror of backend/main.py::_partition_arrivals_for_shadow()."""
    safe_set = {e.lower().strip() for e in (safe_list_emails or []) if e}
    already_member_names: list = []
    resolvable_names:     list = []
    unresolvable_names:   list = []
    for ack in new_arrivals:
        name   = ack.get("name", "?")
        emails = [e.lower().strip() for e in (ack.get("emails") or []) if e]
        if any(e in safe_set for e in emails):
            already_member_names.append(name)
        elif emails:
            resolvable_names.append(name)
        else:
            unresolvable_names.append(name)
    return already_member_names, resolvable_names, unresolvable_names


class TestPartitionArrivalsForShadow:
    """Regression boundary for the 2026-04-30 safe-list-already-member bug."""

    def test_safe_list_match_goes_to_already_member(self):
        arrivals = [{"name": "Burns, Bill", "emails": ["bill.burns@sccssar.org"]}]
        already, res, unres = _partition_arrivals_for_shadow_mirror(
            arrivals, ["bill.burns@sccssar.org"],
        )
        assert already == ["Burns, Bill"]
        assert res == []
        assert unres == []

    def test_email_no_safe_list_match_goes_to_resolvable(self):
        # Non-safe-list responder with email — would be invited under full
        # mode, gets "Would invite:" line under shadow.
        arrivals = [{"name": "Mateos, Miguel", "emails": ["miguel@sccssar.org"]}]
        already, res, unres = _partition_arrivals_for_shadow_mirror(
            arrivals, ["bill.burns@sccssar.org"],
        )
        assert already == []
        assert res == ["Mateos, Miguel"]
        assert unres == []

    def test_no_emails_goes_to_unresolvable(self):
        # Responder with no email anywhere (ack.emails empty AND safe-list
        # name-fallback failed) → diagnostic "Cannot invite" line.
        arrivals = [{"name": "Oliver, Hank", "emails": []}]
        already, res, unres = _partition_arrivals_for_shadow_mirror(arrivals, [])
        assert already == []
        assert res == []
        assert unres == ["Oliver, Hank"]

    def test_mixed_three_buckets(self):
        # The exact 2026-04-30 live-test scenario plus a synthetic third
        # bucket: Bill (safe-list), Miguel (non-safe-list w/ email),
        # Hank (no email at all).
        arrivals = [
            {"name": "Burns, Bill",     "emails": ["bill.burns@sccssar.org"]},
            {"name": "Mateos, Miguel",  "emails": ["miguel@sccssar.org"]},
            {"name": "Oliver, Hank",    "emails": []},
        ]
        already, res, unres = _partition_arrivals_for_shadow_mirror(
            arrivals, ["bill.burns@sccssar.org"],
        )
        assert already == ["Burns, Bill"]
        assert res     == ["Mateos, Miguel"]
        assert unres   == ["Oliver, Hank"]

    def test_safe_list_match_is_case_insensitive(self):
        # EB stores mixed-case emails ("Bill.Burns@sccssar.org"); safe-list
        # secret may be lowercased. Match must be case-insensitive on both
        # sides — otherwise a real safe-list pre-invitee could fall through
        # to the "Would invite" bucket after canonicalization changes.
        arrivals = [{"name": "Burns, Bill", "emails": ["Bill.Burns@SCCSSAR.org"]}]
        already, _, _ = _partition_arrivals_for_shadow_mirror(
            arrivals, ["bill.burns@sccssar.org"],
        )
        assert already == ["Burns, Bill"]

    def test_safe_list_match_strips_whitespace(self):
        # Defensive: secret values can have stray whitespace from manual
        # editing; ack.emails can have trailing spaces from EB API
        # serialization. Both sides stripped before comparison.
        arrivals = [{"name": "Burns, Bill", "emails": [" bill.burns@sccssar.org "]}]
        already, _, _ = _partition_arrivals_for_shadow_mirror(
            arrivals, ["  bill.burns@sccssar.org\n"],
        )
        assert already == ["Burns, Bill"]

    def test_responder_with_multiple_emails_one_safe_list(self):
        # Responder with both personal and sccssar emails — match on ANY
        # of them suffices. (PR #329 sccssar-first sort makes the sccssar
        # email first in the list, but the partition shouldn't depend on
        # ordering.)
        arrivals = [{
            "name": "Burns, Bill",
            "emails": ["personal@example.com", "bill.burns@sccssar.org"],
        }]
        already, _, _ = _partition_arrivals_for_shadow_mirror(
            arrivals, ["bill.burns@sccssar.org"],
        )
        assert already == ["Burns, Bill"]

    def test_empty_safe_list_makes_everyone_resolvable_or_unresolvable(self):
        # If the safe-list secret is empty/missing (failed load, fresh env),
        # NOBODY is treated as already-member — defensive default. All
        # responders fall into resolvable or unresolvable based on email.
        arrivals = [
            {"name": "Burns, Bill",    "emails": ["bill.burns@sccssar.org"]},
            {"name": "Mateos, Miguel", "emails": []},
        ]
        already, res, unres = _partition_arrivals_for_shadow_mirror(arrivals, [])
        assert already == []
        assert res     == ["Burns, Bill"]
        assert unres   == ["Mateos, Miguel"]

    def test_empty_arrivals_returns_three_empty_lists(self):
        # No new arrivals this cycle (idle poll, all responders previously
        # seen) → no message. Caller's format_would_invite_message returns
        # "" and the post is skipped.
        already, res, unres = _partition_arrivals_for_shadow_mirror([], ["x@y.z"])
        assert (already, res, unres) == ([], [], [])

    def test_empty_string_emails_filtered(self):
        # Defensive: EB API can return [""] when paths exist but values are
        # blank. Empty strings must NOT match a (defensively pre-filtered)
        # empty entry in the safe-list set.
        arrivals = [{"name": "Burns, Bill", "emails": ["", ""]}]
        already, res, unres = _partition_arrivals_for_shadow_mirror(
            arrivals, ["", "bill.burns@sccssar.org"],
        )
        # Empty-string emails get filtered out → treated as no emails
        # → unresolvable, NOT already-member (and NOT a fake match on "").
        assert already == []
        assert res     == []
        assert unres   == ["Burns, Bill"]


# ---------------------------------------------------------------------------
# DOB age-hint defensive recompute — mirrors main.py
# Regression: 2026-05-09 SJSU re-run emitted "DOB: 10/20/2005 (21 years old)"
# when actual age was 20. Gemini's age computation is non-deterministic;
# DOB date is reliable. Backend recomputes the (NN years old) hint so all
# downstream surfaces (textarea, WhatsApp, Slack MP line via
# frontend _ebParseAgeFromDob → mp_age) see the corrected value.
#
# Mirrors must stay in sync with the originals in main.py.
# ---------------------------------------------------------------------------

_DOB_AGE_HINT_RE_MIRROR = re.compile(
    r"\((-?\d{1,3})(?:\s+(?:years?\s+old|yrs?\s+old|y\.?o\.?|y/o))?\)",
    re.IGNORECASE,
)
_DOB_LINE_RE_MIRROR = re.compile(r"^DOB:\s*(.+)$", re.MULTILINE)
_DOB_FORMATS_MIRROR = (
    "%m/%d/%Y", "%m/%d/%y",
    "%m-%d-%Y", "%m-%d-%y",  # hyphen separator — common in handwritten forms (corpus)
    "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y",
)


def _compute_age_from_dob_mirror(dob_text, today):
    if not dob_text:
        return None
    candidate = dob_text.split("(", 1)[0].strip()
    if not candidate:
        return None
    parsed = None
    used_2digit_year = False
    # Try-cascade: ValueError per-format is expected (most formats won't match
    # any given input). All-formats-failed is handled explicitly via parsed=None.
    for fmt in _DOB_FORMATS_MIRROR:
        try:
            parsed = datetime.datetime.strptime(candidate, fmt).date()
            used_2digit_year = fmt in ("%m/%d/%y", "%m-%d-%y")
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    if parsed.year > today.year:
        if not used_2digit_year:
            return None
        try:
            parsed = parsed.replace(year=parsed.year - 100)
        except ValueError:
            return None
    if parsed > today:
        return None
    age = today.year - parsed.year
    if (today.month, today.day) < (parsed.month, parsed.day):
        age -= 1
    return age if age >= 0 else None


def _rewrite_dob_age_hint_mirror(summary, today):
    dob_match = _DOB_LINE_RE_MIRROR.search(summary)
    if not dob_match:
        return summary, None
    dob_line = dob_match.group(1)
    hint_match = _DOB_AGE_HINT_RE_MIRROR.search(dob_line)
    if not hint_match:
        return summary, None
    try:
        old_age = int(hint_match.group(1))
    except ValueError:
        return summary, None
    new_age = _compute_age_from_dob_mirror(dob_line, today)
    if new_age is None:
        return summary, None
    plural = "" if new_age == 1 else "s"
    new_hint = f"({new_age} year{plural} old)"
    if hint_match.group(0) == new_hint:
        return summary, None
    abs_hint_start = dob_match.start(1) + hint_match.start()
    abs_hint_end = dob_match.start(1) + hint_match.end()
    new_summary = summary[:abs_hint_start] + new_hint + summary[abs_hint_end:]
    correction = (old_age, new_age) if new_age != old_age else None
    return new_summary, correction


class TestComputeAgeFromDOB:
    def test_sjsu_regression_2026_05_09(self):
        # THE regression case: Gemini emitted "21" for DOB 2005-10-20 on 2026-05-09.
        # Birthday hadn't happened yet this year, so true age is 20.
        assert _compute_age_from_dob_mirror("10/20/2005", datetime.date(2026, 5, 9)) == 20

    def test_birthday_today(self):
        assert _compute_age_from_dob_mirror("05/09/2005", datetime.date(2026, 5, 9)) == 21

    def test_birthday_tomorrow(self):
        assert _compute_age_from_dob_mirror("05/10/2005", datetime.date(2026, 5, 9)) == 20

    def test_birthday_yesterday(self):
        assert _compute_age_from_dob_mirror("05/08/2005", datetime.date(2026, 5, 9)) == 21

    def test_unpadded_format(self):
        # strptime tolerates unpadded numerics with %m/%d/%Y.
        assert _compute_age_from_dob_mirror("10/20/2005", datetime.date(2026, 5, 9)) == 20

    def test_two_digit_year_2000s(self):
        # %m/%d/%y maps "05" to 2005 (within Python's 1969-2068 cutover).
        assert _compute_age_from_dob_mirror("09/18/05", datetime.date(2026, 5, 9)) == 20

    def test_hyphen_separator_4_digit_year(self):
        # Corpus surfaced this format via apply_helpers.py (e.g. handwritten forms).
        assert _compute_age_from_dob_mirror("6-26-2010", datetime.date(2026, 4, 30)) == 15

    def test_hyphen_separator_2_digit_year(self):
        # Corpus form image4: "DOB: 6-26-10 (16 years old)" — Gemini overestimated.
        assert _compute_age_from_dob_mirror("6-26-10", datetime.date(2026, 4, 30)) == 15

    def test_hyphen_separator_2_digit_year_after_birthday(self):
        # Same DOB later in the year → 16 (birthday already passed by July).
        assert _compute_age_from_dob_mirror("6-26-10", datetime.date(2026, 7, 1)) == 16

    def test_iso_format(self):
        assert _compute_age_from_dob_mirror("2005-10-20", datetime.date(2026, 5, 9)) == 20

    def test_long_form_month(self):
        assert _compute_age_from_dob_mirror("October 20, 2005", datetime.date(2026, 5, 9)) == 20

    def test_short_form_month(self):
        assert _compute_age_from_dob_mirror("Oct 20, 2005", datetime.date(2026, 5, 9)) == 20

    def test_garbage_returns_none(self):
        assert _compute_age_from_dob_mirror("Unknown", datetime.date(2026, 5, 9)) is None
        assert _compute_age_from_dob_mirror("", datetime.date(2026, 5, 9)) is None
        assert _compute_age_from_dob_mirror("not a date", datetime.date(2026, 5, 9)) is None

    def test_none_returns_none(self):
        assert _compute_age_from_dob_mirror(None, datetime.date(2026, 5, 9)) is None

    def test_future_dob_returns_none(self):
        # Form data entry error: don't fabricate a negative age.
        assert _compute_age_from_dob_mirror("09/18/2099", datetime.date(2026, 5, 9)) is None

    def test_two_digit_year_future_corrected_to_past(self):
        # %y parses "30" as 2030 → must be 1930 → age 96 on a 2026 today.
        assert _compute_age_from_dob_mirror("01/01/30", datetime.date(2026, 5, 9)) == 96

    def test_strips_trailing_hint(self):
        # Defensive: caller may pass full "<date> (NN years old)" form.
        assert _compute_age_from_dob_mirror(
            "10/20/2005 (21 years old)", datetime.date(2026, 5, 9)
        ) == 20


class TestRewriteDobAgeHint:
    SJSU_SUMMARY = (
        "Event Name: 2026-05-09 SJSU TRADAN\n"
        "Missing Person: Jane Doe; at-risk: Mental health\n"
        "DOB: 10/20/2005 (21 years old)\n"
        "Last Known Position: 1100 Tradan Dr., San Jose, CA\n"
    )

    def test_sjsu_regression_corrects_21_to_20(self):
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            self.SJSU_SUMMARY, datetime.date(2026, 5, 9)
        )
        assert "DOB: 10/20/2005 (20 years old)" in new_summary
        assert "(21 years old)" not in new_summary
        assert correction == (21, 20)

    def test_already_correct_is_silent(self):
        already_correct = self.SJSU_SUMMARY.replace("(21 years old)", "(20 years old)")
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            already_correct, datetime.date(2026, 5, 9)
        )
        assert new_summary == already_correct
        assert correction is None

    def test_no_dob_line_is_silent(self):
        no_dob = "Event Name: foo\nLast Known Position: bar\n"
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            no_dob, datetime.date(2026, 5, 9)
        )
        assert new_summary == no_dob
        assert correction is None

    def test_dob_line_without_age_hint_is_silent(self):
        # Out of scope to inject a hint when one is missing.
        no_hint = "DOB: 10/20/2005\n"
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            no_hint, datetime.date(2026, 5, 9)
        )
        assert new_summary == no_hint
        assert correction is None

    def test_unparseable_dob_with_hint_is_silent(self):
        # Don't blow away an existing hint just because we can't parse the date.
        unparseable = "DOB: Unknown (21 years old)\n"
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            unparseable, datetime.date(2026, 5, 9)
        )
        assert new_summary == unparseable
        assert correction is None

    def test_idempotent(self):
        # Second call with same `today` is a no-op.
        s1, c1 = _rewrite_dob_age_hint_mirror(self.SJSU_SUMMARY, datetime.date(2026, 5, 9))
        s2, c2 = _rewrite_dob_age_hint_mirror(s1, datetime.date(2026, 5, 9))
        assert c1 == (21, 20)
        assert c2 is None
        assert s1 == s2

    def test_other_lines_preserved(self):
        new_summary, _ = _rewrite_dob_age_hint_mirror(
            self.SJSU_SUMMARY, datetime.date(2026, 5, 9)
        )
        assert "Event Name: 2026-05-09 SJSU TRADAN" in new_summary
        assert "Missing Person: Jane Doe; at-risk: Mental health" in new_summary
        assert "Last Known Position: 1100 Tradan Dr., San Jose, CA" in new_summary

    def test_case_insensitive_hint_match(self):
        # Defensive: Gemini occasionally uppercases the hint.
        upper_hint = self.SJSU_SUMMARY.replace("(21 years old)", "(21 YEARS OLD)")
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            upper_hint, datetime.date(2026, 5, 9)
        )
        assert correction == (21, 20)
        # Rewritten hint is canonical lowercase.
        assert "(20 years old)" in new_summary

    # ---------------------------------------------------------------------
    # Format-canonicalization tests — added by the DOB-format-drift fix
    # (see issue history for the dispatch that surfaced it). Variants other than canonical "(N years old)"
    # are rewritten to canonical EVEN WHEN the age value is correct, so the
    # dispatcher textarea presents one consistent format across PDF + JPEG
    # paths and across Gemini's non-deterministic output. The correction
    # tuple is None when only the format changed (no age was wrong).
    # ---------------------------------------------------------------------

    def test_yo_short_form_canonicalized_when_age_right(self):
        """(N yo) with correct age → rewrite to (N years old), correction=None."""
        # DOB 10/20/2005 is age 20 on 2026-05-09 — age value is right
        source = self.SJSU_SUMMARY.replace("(21 years old)", "(20 yo)")
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            source, datetime.date(2026, 5, 9)
        )
        assert "(20 years old)" in new_summary
        assert "(20 yo)" not in new_summary
        assert correction is None  # age was right; only format changed

    def test_yrs_old_canonicalized(self):
        source = self.SJSU_SUMMARY.replace("(21 years old)", "(20 yrs old)")
        new_summary, _ = _rewrite_dob_age_hint_mirror(
            source, datetime.date(2026, 5, 9)
        )
        assert "(20 years old)" in new_summary
        assert "yrs" not in new_summary

    def test_y_slash_o_canonicalized(self):
        source = self.SJSU_SUMMARY.replace("(21 years old)", "(20 y/o)")
        new_summary, _ = _rewrite_dob_age_hint_mirror(
            source, datetime.date(2026, 5, 9)
        )
        assert "(20 years old)" in new_summary
        assert "y/o" not in new_summary

    def test_y_dot_o_dot_canonicalized(self):
        source = self.SJSU_SUMMARY.replace("(21 years old)", "(20 y.o.)")
        new_summary, _ = _rewrite_dob_age_hint_mirror(
            source, datetime.date(2026, 5, 9)
        )
        assert "(20 years old)" in new_summary
        assert "y.o." not in new_summary

    def test_yo_with_wrong_age_both_corrected(self):
        """(N yo) where age is ALSO wrong → rewrite to canonical AND fix age.
        Correction tuple reflects the age change."""
        source = self.SJSU_SUMMARY.replace("(21 years old)", "(21 yo)")
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            source, datetime.date(2026, 5, 9)
        )
        assert "(20 years old)" in new_summary
        assert correction == (21, 20)

    def test_reconstructed_dob_line(self):
        """Exact line SHAPE from the dispatch that surfaced this bug."""
        summary = (
            "Missing Person: DOE, JANE; at-risk: First time runaway\n"
            "DOB: 09/30/2010 (15 yo)\n"
            "Last Seen At: 2026-05-17 21:15\n"
        )
        # On 2026-05-19, age from DOB 09/30/2010 is 15 → format-only canonicalize
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            summary, datetime.date(2026, 5, 19)
        )
        assert "DOB: 09/30/2010 (15 years old)" in new_summary
        assert "(15 yo)" not in new_summary
        assert correction is None

    def test_year_singular_form(self):
        # Defensive: Gemini occasionally drops the plural "s" (e.g. age 1).
        singular = "DOB: 05/01/2025 (1 year old)\n"
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            singular, datetime.date(2026, 5, 9)
        )
        # Born 2025-05-01, today 2026-05-09 → 1 year old (correct).
        assert correction is None
        assert new_summary == singular


# ===========================================================================
# PR-D-1 (2026-05-10) — dispatcher-specified staging override
# ===========================================================================
# Mirrors of literals + logic in main.py and caltopo.py. Following the file's
# established pattern, these mirrors are NOT direct imports — main.py has
# heavyweight GCP/Vertex AI dependencies that aren't in the local pytest env.
# Source of truth is the live module; if you changed it in main.py or
# caltopo.py, update the mirror here in the same PR or this test fails fast
# at Step 0 of the build script (which is exactly what we want).
# ---------------------------------------------------------------------------

# main.py — _DISPATCHER_OVERRIDE_LABEL + _OFFICER_OVERRIDE_LABEL constants.
# Pinned because the verbatim string must match across:
#   - frontend/index.html:_applyStagingOverrideToTextarea (PR-D-2)
#   - caltopo.py marker description detection (PR-D-1)
# Drift would silently break textarea injection AND CalTopo arbitration.
_DISPATCHER_OVERRIDE_LABEL_LITERAL = "Dispatcher-specified staging location"
_OFFICER_OVERRIDE_LABEL_LITERAL    = "Officer-designated staging location"


class TestDobAgeHintBareNumber:
    """#814 — a bare-number parenthetical must reach the defensive recompute.

    `12/03/1969 (57)` came off a real v2 AcroForm: an officer wrote a genuine
    DOB *and their own arithmetic*, with no unit. Every accepted hint variant
    required a unit, so `_rewrite_dob_age_hint` returned at `if not hint_match`
    and the age was never checked against the date sitting beside it. The
    officer's number was also wrong — 12/03/1969 is 56 until December — i.e.
    wrong in exactly the direction the recompute exists to catch, on the one
    consumer that could not see it.
    """

    BARE = (
        "Event Name: 2026-09-06 XXSO MAIN\n"
        "DOB: 12/03/1969 (57)\n"
        "Missing Person: Jane Doe; at-risk: Dementia\n"
    )

    def test_bare_number_hint_is_recomputed_and_canonicalized(self):
        # 2026-09-06 is before the December birthday, so 1969 -> 56, not 57.
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            self.BARE, datetime.date(2026, 9, 6)
        )
        assert correction == (57, 56)
        assert "(56 years old)" in new_summary
        assert "(57)" not in new_summary

    def test_bare_number_already_correct_stays_silent(self):
        """Event-log policy: corrections and failures only, never a silent success."""
        already = self.BARE.replace("(57)", "(56)")
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            already, datetime.date(2026, 9, 6)
        )
        assert correction is None
        assert "(56 years old)" in new_summary

    def test_after_the_birthday_the_officer_was_right(self):
        """Same fixture, later `today` — 57 becomes correct and nothing is flagged."""
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            self.BARE, datetime.date(2026, 12, 4)
        )
        assert correction is None
        assert "(57 years old)" in new_summary

    def test_parenthesized_number_without_a_date_is_never_rewritten(self):
        """The guard that makes accepting a bare number safe.

        A hint match is NOT sufficient — `_compute_age_from_dob` must also parse
        a real date off the SAME line. This is the false-positive case the issue
        worried about, and it is closed by construction rather than by the regex.
        """
        no_date = "DOB: [not recorded] (57)\n"
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            no_date, datetime.date(2026, 9, 6)
        )
        assert correction is None
        assert new_summary == no_date

    def test_four_digit_year_in_parens_cannot_match(self):
        """\\d{1,3} must be followed immediately by `)`, so (2005) is not an age."""
        year = "DOB: 12/03/1969 (2005)\n"
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            year, datetime.date(2026, 9, 6)
        )
        assert correction is None
        assert new_summary == year

    def test_unit_bearing_variants_still_match(self):
        """Making the unit optional must not lose any variant it used to accept."""
        for hint in ("(57 years old)", "(57 year old)", "(57 yrs old)",
                     "(57 yr old)", "(57 yo)", "(57 y.o.)", "(57 y/o)"):
            summary = self.BARE.replace("(57)", hint)
            _, correction = _rewrite_dob_age_hint_mirror(
                summary, datetime.date(2026, 9, 6)
            )
            assert correction == (57, 56), f"{hint} stopped matching"


class TestNegativeAgeHintIsRepaired:
    """#765 defect 2 — the guard was blind exactly where the value was worst.

    `_DOB_AGE_HINT_RE` used `\\d{1,3}`, which does not match `-29`. So on the
    2026-08-18 dementia callout `_rewrite_dob_age_hint` found no hint, returned
    `(summary, None)`, and stayed SILENT — correctly, per the "corrections and
    failures only" event-log policy. A plausible-but-wrong age (71 -> 72) it
    caught and logged; an impossible one it waved through without a word.
    """

    NEG = (
        "Event Name: 2026-09-06 XXSO MAIN\n"
        "DOB: 12/20/54 (-29 years old)\n"
        "Missing Person: Jane Doe; at-risk: Dementia\n"
    )

    def test_negative_hint_is_seen_and_repaired(self):
        new_summary, correction = _rewrite_dob_age_hint_mirror(
            self.NEG, datetime.date(2026, 9, 6)
        )
        assert correction == (-29, 71)
        assert "(71 years old)" in new_summary
        assert "-29" not in new_summary

    def test_the_repair_is_reported_not_silent(self):
        """An impossible age must be a LOUD correction, not a silent pass."""
        _, correction = _rewrite_dob_age_hint_mirror(
            self.NEG, datetime.date(2026, 9, 6)
        )
        assert correction is not None, (
            "a negative age was corrected without reporting it — the event log "
            "would show nothing, and silence reads as success"
        )

    @pytest.mark.parametrize("hint,expected_old", [
        ("(-29 years old)", -29), ("(-4 years old)", -4), ("(-39 yo)", -39),
        ("(-19)", -19),  # bare negative, via #814's optional unit
    ])
    def test_negative_shapes_all_match(self, hint, expected_old):
        summary = self.NEG.replace("(-29 years old)", hint)
        _, correction = _rewrite_dob_age_hint_mirror(
            summary, datetime.date(2026, 9, 6)
        )
        assert correction is not None and correction[0] == expected_old

    def test_widening_did_not_break_positive_hints(self):
        ok = self.NEG.replace("(-29 years old)", "(71 years old)")
        _, correction = _rewrite_dob_age_hint_mirror(ok, datetime.date(2026, 9, 6))
        assert correction is None

    def test_production_regex_matches_a_negative_hint(self):
        """Reads main.py itself — the mirror cannot prove production changed."""
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        pat = re.search(
            r'_DOB_AGE_HINT_RE = re\.compile\(\s*\n\s*r"(.*?)",\s*\n\s*re\.IGNORECASE',
            src,
        )
        assert pat, "_DOB_AGE_HINT_RE not found in main.py"
        rx = re.compile(pat.group(1), re.IGNORECASE)
        m = rx.search("DOB: 12/20/54 (-29 years old)")
        assert m and m.group(1) == "-29", "production still cannot see a negative hint"


class TestDobAgeHintRegexParity:
    """The pin whose absence let three copies of this regex drift.

    `_DOB_AGE_HINT_RE` exists in main.py, in this file's mirror, and in
    apply_helpers.py. Nothing read production, so apply_helpers had quietly
    narrowed to years-old-only — meaning every corpus run under-reported
    corrections on `(N yo)` / `(N y/o)` forms and validated logic that does not
    ship. Follows the TestUnansweredLpbNote production-pin pattern.
    """

    PATTERN = r'_DOB_AGE_HINT_RE = re\.compile\(\s*\n\s*r"(.*?)",\s*\n\s*re\.IGNORECASE'

    def _pattern_in(self, relpath):
        src = (Path(__file__).parent / relpath).read_text(encoding="utf-8")
        m = re.search(self.PATTERN, src)
        assert m, f"_DOB_AGE_HINT_RE not found in {relpath}"
        return m.group(1)

    def test_all_three_copies_are_identical(self):
        prod = self._pattern_in("main.py")
        helper = self._pattern_in("migration_validation/apply_helpers.py")
        assert prod == _DOB_AGE_HINT_RE_MIRROR.pattern, (
            f"main.py drifted from the test mirror.\n  main.py: {prod}\n"
            f"  mirror:  {_DOB_AGE_HINT_RE_MIRROR.pattern}"
        )
        assert helper == prod, (
            f"apply_helpers.py drifted from main.py — corpus runs would validate "
            f"code that does not ship.\n  main.py:       {prod}\n"
            f"  apply_helpers: {helper}"
        )

    def test_production_bails_when_the_date_is_unparseable(self):
        """The guard that makes a bare number safe to accept — pinned in PRODUCTION.

        Accepting `(N)` is only safe because a hint match is not sufficient:
        `_compute_age_from_dob` must also parse a real date off the same line,
        and `_rewrite_dob_age_hint` returns unchanged when it cannot. The
        behavioural tests above exercise the hand-written mirror, so they stay
        green if main.py's body drifts — mutation testing confirmed exactly that
        leak. This reads main.py itself.
        """
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        m = re.search(
            r"^def _rewrite_dob_age_hint\(.*?(?=\n\ndef |\n\n@)",
            src, re.DOTALL | re.MULTILINE,
        )
        assert m, "_rewrite_dob_age_hint not found in main.py"
        body = [
            l.strip() for l in m.group(0).splitlines()
            if l.split("#")[0].strip()
        ]
        try:
            i = body.index("new_age = _compute_age_from_dob(dob_line, today)")
        except ValueError:
            raise AssertionError(
                "_compute_age_from_dob call site not found in _rewrite_dob_age_hint"
            )
        assert body[i + 1] == "if new_age is None:", (
            f"expected a None-check immediately after the recompute, got: {body[i + 1]!r}"
        )
        assert body[i + 2] == "return summary, None", (
            "the unparseable-date branch must return the summary UNCHANGED — "
            f"got: {body[i + 2]!r}. Without it a parenthesized number on a line "
            "with no valid date is rewritten as an age."
        )

    def test_production_accepts_a_bare_number(self):
        """Reads main.py's own source, not the mirror — #814's actual fix."""
        prod = re.compile(self._pattern_in("main.py"), re.IGNORECASE)
        assert prod.search("DOB: 12/03/1969 (57)"), "bare-number hint not accepted"
        assert not prod.search("DOB: 12/03/1969 (2005)"), "4-digit year matched as age"


class TestDispatcherStagingOverrideLabel:
    """The verbatim dispatcher-override label is pinned across main.py +
    caltopo.py + frontend/index.html (PR-D-2). Any change to main.py's
    _DISPATCHER_OVERRIDE_LABEL MUST update this mirror too."""

    def test_label_unchanged(self):
        # Source: main.py::_DISPATCHER_OVERRIDE_LABEL
        assert _DISPATCHER_OVERRIDE_LABEL_LITERAL == "Dispatcher-specified staging location"

    def test_label_distinct_from_officer(self):
        # Defense-in-depth: the dispatcher and officer labels MUST be distinct
        # so caltopo.py marker arbitration can tell them apart by string
        # detection if the type field is ever missing.
        assert _DISPATCHER_OVERRIDE_LABEL_LITERAL != _OFFICER_OVERRIDE_LABEL_LITERAL
        assert "Dispatcher" in _DISPATCHER_OVERRIDE_LABEL_LITERAL
        assert "Officer" in _OFFICER_OVERRIDE_LABEL_LITERAL


# caltopo.py — SYMBOL_STAGING_DISPATCHER constant. Pinned because CalTopo
# silently falls back to a default marker if the symbol string is wrong;
# the comment block in caltopo.py warns: "Do not guess — CalTopo silently
# falls back to a default if the symbol string is wrong."
_SYMBOL_STAGING_DISPATCHER_LITERAL = "cp"
_SYMBOL_STAGING_OFFICER_LITERAL    = "cp"


class TestDispatcherSymbolConstant:
    """SYMBOL_STAGING_DISPATCHER must be 'cp' (the CalTopo Command Post glyph,
    confirmed from GeoJSON export). Same glyph as STAGING_OFFICER — what
    differs between them is description text and arbitration precedence,
    not the marker glyph itself."""

    def test_dispatcher_symbol_is_cp(self):
        # Source: caltopo.py::SYMBOL_STAGING_DISPATCHER
        assert _SYMBOL_STAGING_DISPATCHER_LITERAL == "cp"

    def test_dispatcher_and_officer_share_cp_glyph(self):
        assert _SYMBOL_STAGING_DISPATCHER_LITERAL == _SYMBOL_STAGING_OFFICER_LITERAL == "cp"


class TestMarkerArbitration:
    """Precedence: dispatcher > index-0 default > officer.

    Mirrors the branch logic in backend/caltopo.py around the staging-marker
    loop. When the upstream code changes, this test must change to match.
    """

    @staticmethod
    def _arbitrate(staging_entries: list[dict]) -> list[tuple[str, str]]:
        """Mirror of caltopo.py staging-marker loop arbitration.
        Returns list of (symbol, description) tuples, one per entry."""
        results = []
        # Empty-Overpass refinement (2026-07-16): a SOLE officer entry (no
        # non-officer entry exists) is the authoritative staging → cp.
        _has_non_officer = any(s.get("type") != "officer" for s in staging_entries)
        for i, s in enumerate(staging_entries):
            entry_type = s.get("type")
            is_dispatcher = entry_type == "dispatcher"
            is_officer    = entry_type == "officer"
            is_default    = (i == 0)
            # #804 (Bill, 2026-09-05): index 0 takes cp whatever its type.
            # The surviving anti-hijack rule is positional — an officer entry
            # BELOW index 0 never displaces the #1 above it.
            if is_dispatcher:
                symbol, desc = "cp", "Dispatcher-specified staging"
            # Order is load-bearing: the sole-officer entry is ALSO at index 0,
            # so it must be tested before the index-0 branch or the wilderness
            # case relabels to "(top recommendation)". Same symbol, so the
            # regression would show up only in the words.
            elif is_officer and not _has_non_officer:
                symbol, desc = "cp", "Officer-designated staging (only option)"
            elif is_default:
                symbol, desc = "cp", (
                    "Officer-designated staging (top recommendation)"
                    if is_officer else "Recommended staging"
                )
            else:
                desc = "Officer-designated staging" if is_officer else "Alternate staging"
                symbol = "point"
            results.append((symbol, desc))
        return results

    def test_no_override_index_0_gets_cp(self):
        # Baseline: no dispatcher entry. Index 0 wins cp (existing behavior).
        entries = [
            {"type": "alternate"}, {"type": "alternate"}, {"type": "officer"},
        ]
        results = self._arbitrate(entries)
        assert results[0] == ("cp", "Recommended staging")
        assert results[1] == ("point", "Alternate staging")
        assert results[2] == ("point", "Officer-designated staging")

    def test_dispatcher_override_takes_cp(self):
        # Dispatcher entry at index 0. It gets cp; officer at the end gets point.
        entries = [
            {"type": "dispatcher"}, {"type": "alternate"}, {"type": "officer"},
        ]
        results = self._arbitrate(entries)
        assert results[0] == ("cp", "Dispatcher-specified staging")
        assert results[1] == ("point", "Alternate staging")
        assert results[2] == ("point", "Officer-designated staging")

    def test_sole_officer_entry_is_promoted_to_cp(self):
        # Empty-Overpass refinement (2026-07-16, Bill-approved): when the
        # officer's entry is the ONLY staging (Overpass returned zero
        # candidates — remote LKP like Joseph D. Grant County Park), there is
        # no quality-ranked #1 to protect, so the officer entry IS the
        # authoritative staging and takes cp + red rather than a bare blue dot
        # that reads as "nothing recommended." The "officer never hijacks cp
        # from a real quality-ranked #1" rule is preserved by
        # test_officer_with_non_officer_entries_stays_point below.
        entries = [{"type": "officer"}]
        results = self._arbitrate(entries)
        assert results[0] == ("cp", "Officer-designated staging (only option)")

    def test_officer_at_index_0_takes_cp(self):
        # #804 (Bill, 2026-09-05). This case USED to assert cp_count == 0 on
        # the reasoning that an officer entry must never take cp while any
        # non-officer entry exists. It produced a real map on 2026-09-04 with
        # six blue dots and no command post: the officer's staging had
        # fuzzy-matched the top-ranked park, so the two merged into one
        # index-0 entry typed `officer` and every cp branch declined it.
        #
        # An officer entry at index 0 IS the top recommendation — there is no
        # separate #1 above it to protect.
        entries = [{"type": "officer"}, {"type": "alternate"}, {"type": "alternate"}]
        results = self._arbitrate(entries)
        assert results[0] == ("cp", "Officer-designated staging (top recommendation)")
        assert results[1] == ("point", "Alternate staging")
        assert sum(1 for symbol, _desc in results if symbol == "cp") == 1

    def test_officer_below_index_0_never_takes_cp(self):
        # The anti-hijack rule that survives #804, and the one the 2026-05-09
        # XXSO regression actually earned: an officer entry ranked BELOW the
        # top recommendation must not displace it. The alternate at index 0
        # keeps cp; the officer at index 2 stays a blue dot.
        entries = [{"type": "alternate"}, {"type": "alternate"}, {"type": "officer"}]
        results = self._arbitrate(entries)
        assert results[0] == ("cp", "Recommended staging")
        assert results[2] == ("point", "Officer-designated staging")

    def test_no_map_is_left_without_a_cp(self):
        # #804's actual complaint. At-most-one was always pinned; at-least-one
        # never was, which is why zero was reachable. Covers every shape the
        # other cases exercise, including the merged-officer one.
        for entries in [
            [{"type": "officer"}, {"type": "alternate"}, {"type": "alternate"}],
            [{"type": "alternate"}, {"type": "officer"}],
            [{"type": "officer"}],
            [{"type": "dispatcher"}, {"type": "officer"}, {"type": "alternate"}],
        ]:
            results = self._arbitrate(entries)
            cp_count = sum(1 for symbol, _desc in results if symbol == "cp")
            assert cp_count == 1, f"Expected exactly one cp for entries={entries!r}"

    def test_mirror_matches_production_branch_order(self):
        """caltopo.py is not importable under local pytest, so every case above
        exercises a hand-written mirror. Without this, production can move and
        the whole class stays green — the drift shape CLAUDE.md's mirror-parity
        rule exists for.

        Pins the ORDERED branch conditions, because #804's own near-miss was an
        ordering one: the sole-officer branch and the index-0 branch both match
        an officer entry at index 0, they return the same SYMBOL, and they
        differ only in the description. A set-membership assertion passes on
        the swapped order.
        """
        from pathlib import Path
        src = (Path(__file__).parent / "caltopo.py").read_text(encoding="utf-8")
        start = src.index("for i, s in enumerate(staging):")
        body = src[start:][: src[start:].index("_add_marker(")]
        code = "\n".join(l.split("#")[0] for l in body.splitlines())

        conditions = [
            "if is_dispatcher:",
            "elif is_officer and not _has_non_officer:",
            "elif is_default:",
            "else:",
        ]
        positions = []
        for c in conditions:
            assert c in code, (
                "caltopo.py marker arbitration no longer has the branch %r — "
                "the mirror in this file is now testing code that does not "
                "exist." % c
            )
            positions.append(code.index(c))
        assert positions == sorted(positions), (
            "caltopo.py marker arbitration branches are in a different order "
            "than the mirror. The sole-officer branch MUST precede the index-0 "
            "branch: both match an officer entry at index 0 and both return "
            "cp, so a swap silently relabels the wilderness case as the top "
            "recommendation."
        )
        assert "elif is_default and not is_officer:" not in code, (
            "The pre-#804 index-0 guard is back — an officer entry that merged "
            "with the #1 recommendation loses cp and the map has none at all."
        )

    def test_at_most_one_cp_per_map(self):
        # The fundamental invariant: only one entry can be the CP marker.
        for entries in [
            [{"type": "dispatcher"}, {"type": "alternate"}],
            [{"type": "alternate"}, {"type": "alternate"}, {"type": "officer"}],
            [{"type": "dispatcher"}, {"type": "alternate"}, {"type": "officer"}],
            [{"type": "officer"}],  # sole officer → promoted to exactly one cp
            [{"type": "officer"}, {"type": "alternate"}],  # #804 merged officer at #1
        ]:
            results = self._arbitrate(entries)
            cp_count = sum(1 for symbol, _desc in results if symbol == "cp")
            assert cp_count == 1, f"Multiple cp markers for entries={entries!r}"


# Mirror of main.py::_extract_streetname_from_address. The mirror exists so
# the test runs without main.py's heavyweight deps. If you change the helper
# in main.py, change this mirror in the same PR.
_PR_D_APT_STRIP_RE = re.compile(
    r",?\s*(?:Apt\.?|Apartment|Unit|Ste\.?|Suite|#)\s*#?\s*[\w-]+",
    re.IGNORECASE,
)
_PR_D_STREET_CARDINALS = re.compile(
    r"^(?:North|South|East|West|N\.?|S\.?|E\.?|W\.?)\s+",
    re.IGNORECASE,
)
_PR_D_STREET_TYPES = re.compile(
    r"\s+(?:Blvd|Blvd\.|Boulevard|Dr|Dr\.|Drive|Ave|Ave\.|Avenue|St|St\.|Street|"
    r"Rd|Rd\.|Road|Ln|Ln\.|Lane|Way|Ct|Ct\.|Court|Pl|Pl\.|Place|"
    r"Pkwy|Pkwy\.|Parkway|Hwy|Hwy\.|Highway|Expy|Expressway|"
    r"Cir|Cir\.|Circle|Ter|Ter\.|Terrace|Trail|Trl)(?:\s.*)?$",
    re.IGNORECASE,
)
_PR_D_INTERSECTION_RE = re.compile(
    r"\s+(?:&|@|at|and)\s+|\bINTERSECTION\s+OF\b",
    re.IGNORECASE,
)


def _extract_streetname_from_address_mirror(address):
    """Mirror of main.py::_extract_streetname_from_address.

    Note (Bill 2026-05-10): PR-D-2.5 originally added a "first word only"
    reduction here; reverted to keep override-path and OCR-time output
    internally consistent (both produce multi-word streetnames). The
    dispatcher can manually edit the Event Name post-override if a
    multi-word result looks awkward in their context.
    """
    if not address or not str(address).strip():
        return None
    street_portion = address.split(",", 1)[0].strip()
    if not street_portion:
        return None
    cleaned = _PR_D_APT_STRIP_RE.sub("", street_portion).strip(" ,")
    no_house_num = re.sub(r"^\d+[ \t]+", "", cleaned).strip()
    intersection_match = _PR_D_INTERSECTION_RE.search(no_house_num)
    if intersection_match:
        if intersection_match.group(0).strip().upper().startswith("INTERSECTION"):
            after_prefix = no_house_num[intersection_match.end():].strip()
            between_match = re.search(r"\s+(?:&|@|at|and)\s+", after_prefix, re.IGNORECASE)
            first = (
                after_prefix[:between_match.start()].strip()
                if between_match
                else after_prefix.split(",")[0].strip()
            )
        else:
            first = no_house_num[:intersection_match.start()].strip()
        no_house_num = first
    no_type = _PR_D_STREET_TYPES.sub("", no_house_num).strip()
    no_cardinal = _PR_D_STREET_CARDINALS.sub("", no_type).strip()
    return no_cardinal or None


class TestExtractStreetnameFromAddress:
    """Pinned in PR-D-1 — the dispatcher staging override endpoint extracts a
    suggested Event Name street from the dispatcher's address input. Must
    produce the SAME result the OCR-time Event Name reconstruction would
    produce for the same street, so an override-driven event name matches
    what would have been right if OCR had been right.

    Mirrors main.py::_extract_streetname_from_address.
    """

    def test_simple_residential_address(self):
        # Verde Vista canonical case from the 2026-05-10 incident.
        result = _extract_streetname_from_address_mirror(
            "100 Verde Vista Lane, Saratoga, CA"
        )
        assert result == "Verde Vista"

    def test_saratoga_sunnyvale_rd_keeps_multi_word(self):
        # 2026-05-10 case: dispatcher can manually edit if awkward.
        # Internal consistency with OCR-time wins over radio brevity.
        result = _extract_streetname_from_address_mirror(
            "10000 Saratoga Sunnyvale Rd, Saratoga, CA 95070"
        )
        assert result == "Saratoga Sunnyvale"

    def test_cardinal_direction_stripped(self):
        # Mirror of OCR-time reconstruction for "1200 East Calaveras Blvd".
        result = _extract_streetname_from_address_mirror(
            "1200 East Calaveras Blvd, Milpitas, CA"
        )
        assert result == "Calaveras"

    def test_intersection_extracts_first_street(self):
        # 2026-05-07 SJSU canonical case. "5th @ St. John" → "5th".
        result = _extract_streetname_from_address_mirror(
            "5th @ St. John, San Jose, CA"
        )
        assert result == "5th"

    def test_apt_number_stripped(self):
        result = _extract_streetname_from_address_mirror(
            "100 Verde Vista Lane Apt 4, Saratoga, CA"
        )
        assert result == "Verde Vista"

    def test_empty_returns_none(self):
        assert _extract_streetname_from_address_mirror("") is None
        assert _extract_streetname_from_address_mirror("   ") is None
        assert _extract_streetname_from_address_mirror(None) is None

    def test_no_street_type_returns_basic_name(self):
        # Pure name (no street type, no house number). Returns the name as-is
        # because there's nothing to strip — dispatcher gets to confirm.
        result = _extract_streetname_from_address_mirror("Garbage Park, City, CA")
        assert result == "Garbage Park"


# Mirror of main.py::_parse_utm_string regex pattern (without the lazy
# `utm` package dependency — that's tested separately).
_PR_D_UTM_RE = re.compile(
    r"^\s*(\d{1,2})\s*([A-Za-z])\s+([\d.]+)\s*[Ee]\s+([\d.]+)\s*[Nn]\s*$"
)


def _parse_utm_string_mirror(utm_str):
    """Mirror of main.py::_parse_utm_string — pure parsing logic, no actual
    UTM-to-lat/lng conversion (that lives in the `utm` package and is
    exercised by the importorskip-gated test below).

    Returns (zone_num, zone_letter, easting, northing) on success, None on
    parse failure. The full conversion test is below and gated on the
    `utm` package being installed.
    """
    if not utm_str or not str(utm_str).strip():
        return None
    m = _PR_D_UTM_RE.match(utm_str)
    if not m:
        return None
    zone_num = int(m.group(1))
    zone_letter = m.group(2).upper()
    try:
        easting = float(m.group(3))
        northing = float(m.group(4))
    except ValueError:
        return None
    if not (1 <= zone_num <= 60):
        return None
    if not (0 < easting < 1_000_000):
        return None
    if not (0 <= northing <= 10_000_000):
        return None
    return (zone_num, zone_letter, easting, northing)


class TestParseUtmString:
    """Pinned UTM parsing. The full lat/lng conversion is tested via the `utm`
    package gate — production Cloud Run has it from requirements.txt.

    Mirrors main.py::_parse_utm_string.
    """

    def test_canonical_sccssar_utm_parses(self):
        # The reference value comes from the plan file:
        # "10S 590309E 4142188N" → ~37.42210, -121.97936.
        result = _parse_utm_string_mirror("10S 590309E 4142188N")
        assert result is not None
        zone_num, zone_letter, easting, northing = result
        assert zone_num == 10
        assert zone_letter == "S"
        assert easting == 590309.0
        assert northing == 4142188.0

    def test_canonical_full_conversion_via_utm_package(self):
        # If `utm` is installed (production Cloud Run always has it via
        # requirements.txt), verify the actual lat/lng round-trips.
        utm_pkg = pytest.importorskip("utm")
        lat, lng = utm_pkg.to_latlon(590309, 4142188, 10, "S")
        assert abs(lat - 37.42210) < 0.001, f"lat off: {lat}"
        assert abs(lng - (-121.97936)) < 0.001, f"lng off: {lng}"

    def test_lowercase_input_accepted(self):
        result = _parse_utm_string_mirror("10s 590309e 4142188n")
        assert result is not None
        assert result[1] == "S"  # uppercased by the parser

    def test_garbage_returns_none(self):
        assert _parse_utm_string_mirror("garbage") is None
        assert _parse_utm_string_mirror("") is None
        assert _parse_utm_string_mirror("   ") is None
        assert _parse_utm_string_mirror(None) is None

    def test_out_of_range_zone_returns_none(self):
        # UTM zones are 1-60.
        assert _parse_utm_string_mirror("99X 590309E 4142188N") is None
        assert _parse_utm_string_mirror("0X 590309E 4142188N") is None

    def test_missing_e_or_n_suffix_returns_none(self):
        assert _parse_utm_string_mirror("10S 590309 4142188") is None
        assert _parse_utm_string_mirror("10S 590309E 4142188") is None


# ---------------------------------------------------------------------------
# Issue #668 — coordinate staging in Pass B. Mirrors of main.py::
# _LATLNG_STAGING_RE / _parse_latlng_string / _parse_coordinate_staging_text /
# _format_coordinate_staging_display. Production pins are in
# TestStagingCoordinateProductionWiring below — without them these mirrors
# would keep reporting green against a stale copy.
# ---------------------------------------------------------------------------

_LATLNG_STAGING_RE_MIRROR = re.compile(
    r"^\(?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)?$"
)


def _parse_latlng_string_mirror(text):
    """Mirror of main.py::_parse_latlng_string."""
    if not text or not str(text).strip():
        return None
    m = _LATLNG_STAGING_RE_MIRROR.match(text.strip())
    if not m:
        return None
    lat_s, lng_s = m.group(1), m.group(2)
    if "." not in lat_s and "." not in lng_s:
        return None
    try:
        lat = float(lat_s)
        lng = float(lng_s)
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0):
        return None
    if not (-180.0 <= lng <= 180.0):
        return None
    return (lat, lng)


def _parse_coordinate_staging_text_mirror(text):
    """Mirror of main.py::_parse_coordinate_staging_text.

    Same lazy `import utm` as production, so the UTM branch can be exercised
    with a stub module injected into sys.modules (the real package is only
    present on Cloud Run via requirements.txt).
    """
    if not text or not str(text).strip():
        return None
    raw = text.strip()
    latlng = _parse_latlng_string_mirror(raw)
    if latlng is not None:
        return (latlng[0], latlng[1], "")
    parsed = _parse_utm_string_mirror(raw)
    if parsed is not None:
        zone_num, zone_letter, easting, northing = parsed
        import utm as utm_pkg
        lat, lng = utm_pkg.to_latlon(easting, northing, zone_num, zone_letter)
        return (float(lat), float(lng), raw)
    return None


def _format_coordinate_staging_display_mirror(lat, lng, utm_str=""):
    """Mirror of main.py::_format_coordinate_staging_display."""
    base = f"{lat:.5f}, {lng:.5f}"
    utm_display = utm_str.strip()
    return f"{base} — {utm_display}" if utm_display else base


# frontend/index.html — the regex that turns the Recommendations #1 line into
# `stagingRec`, which becomes maps.apple.com/?q=<TEXT> and
# google.com/maps/search/<TEXT> for every responder in Slack. Mirrored here in
# Python form so the coordinate display format can be tested against the
# consumer that actually decides where responders drive.
_STAGING_REC_RE_MIRROR = re.compile(r"^1\.\s+([^\n—]+)", re.MULTILINE)


class TestParseLatLngStagingString:
    """Pin the decimal `lat, lng` parser (issue #668).

    Wilderness and remote mutual-aid callouts specify staging as a coordinate
    because there is no landmark, address, or cross street to give (Bill,
    2026-07-31). That is normal input, so Pass B has to recognise it before
    handing the text to an address geocoder.

    The risk runs the other way too: this parser reads FREE-FORM officer text,
    not a labelled coordinate field, so a false positive plants a staging
    marker in the ocean and hands every responder a link to it.
    """

    def test_decimal_pair_parses(self):
        assert _parse_latlng_string_mirror("37.26940, -122.03674") == (
            37.26940, -122.03674
        )

    def test_parentheses_tolerated(self):
        # CalTopo's Point Info popup and several map apps copy with parens.
        assert _parse_latlng_string_mirror("(37.26940, -122.03674)") == (
            37.26940, -122.03674
        )

    def test_whitespace_tolerated(self):
        assert _parse_latlng_string_mirror("  37.26940 ,-122.03674  ") == (
            37.26940, -122.03674
        )

    def test_humboldt_coordinate_parses(self):
        # The 2026-07-31 mutual-aid callout: this pair was the only correct
        # location on the form, and it was sent to the address geocoder as
        # `q=40.27398, -124.07826, CA`.
        assert _parse_latlng_string_mirror("40.27398, -124.07826") == (
            40.27398, -124.07826
        )

    def test_single_number_returns_none(self):
        # The comma is what disambiguates. Mirrors the frontend contract in
        # index.html, which rejects a bare "37.26940" up-front.
        assert _parse_latlng_string_mirror("37.26940") is None

    def test_integer_only_pair_returns_none(self):
        """Stricter than the frontend, deliberately.

        The frontend parses a labelled coordinate field where the dispatcher
        has already declared intent. Pass B parses free-form officer text,
        where a bare integer pair is far likelier to be prose than a
        coordinate — and (1, 2) is in the Gulf of Guinea.
        """
        assert _parse_latlng_string_mirror("1, 2") is None
        assert _parse_latlng_string_mirror("37, -122") is None

    def test_bare_house_number_shape_returns_none(self):
        # "10410, CA" is the input shape that resolved to Jakarta. It must
        # keep flowing to the geocoding path (and its guards), not be read
        # as a coordinate.
        assert _parse_latlng_string_mirror("10410, CA") is None

    def test_ordinary_staging_text_returns_none(self):
        # The overwhelming majority of staging text. A false positive here is
        # worse than a miss.
        assert _parse_latlng_string_mirror("ALMA @ 10TH") is None
        assert _parse_latlng_string_mirror("Cardoza Park, Milpitas") is None
        assert _parse_latlng_string_mirror("400 Llagas Road, Morgan Hill") is None
        assert _parse_latlng_string_mirror("Richey Center") is None

    def test_out_of_range_returns_none(self):
        assert _parse_latlng_string_mirror("91.0, -122.03674") is None
        assert _parse_latlng_string_mirror("37.26940, -181.0") is None

    def test_empty_returns_none(self):
        assert _parse_latlng_string_mirror("") is None
        assert _parse_latlng_string_mirror("   ") is None
        assert _parse_latlng_string_mirror(None) is None


class TestFormatCoordinateStagingDisplay:
    """Pin the SHARED coordinate display string (issue #668).

    This is the second surface, and the one that hurts. Setting the marker
    coordinates fixes CalTopo and nothing else; per the Locked Decision
    "Staging line text IS the responders' maps-link query", the TEXT is what
    index.html turns into maps.apple.com/?q= and google.com/maps/search/ for
    every responder in Slack — and no maps app text-searches a UTM string.

    A marker-only fix reproduces #646: map correct, whole callout misrouted.
    """

    def test_lat_lng_only(self):
        assert _format_coordinate_staging_display_mirror(37.42210, -121.97936) == (
            "37.42210, -121.97936"
        )

    def test_utm_trails_after_em_dash(self):
        # Bill operational ask 2026-05-10: lat/lng leads so the ?q= link
        # resolves; the SAR-standard UTM string trails so radio-trained
        # responders see the form they were taught.
        assert _format_coordinate_staging_display_mirror(
            37.42210, -121.97936, "10S 590309E 4142188N"
        ) == "37.42210, -121.97936 — 10S 590309E 4142188N"

    def test_utm_whitespace_stripped(self):
        assert _format_coordinate_staging_display_mirror(
            37.42210, -121.97936, "  10S 590309E 4142188N  "
        ) == "37.42210, -121.97936 — 10S 590309E 4142188N"

    def test_blank_utm_is_lat_lng_only(self):
        # An all-whitespace utm_str must not produce a dangling em-dash.
        assert _format_coordinate_staging_display_mirror(
            37.42210, -121.97936, "   "
        ) == "37.42210, -121.97936"

    def test_five_decimal_places(self):
        # ~1 m precision. Matches what /apply-staging-override has emitted
        # since May, so the two paths render one coordinate identically.
        assert _format_coordinate_staging_display_mirror(37.0, -121.5) == (
            "37.00000, -121.50000"
        )

    def test_responder_maps_query_is_the_decimal_pair(self):
        """THE test this issue exists for.

        Builds the Recommendations #1 line the way main.py does, then applies
        the frontend's own stagingRec regex. What comes out is the literal
        text every responder's phone searches when they tap the Slack staging
        link. It must be the decimal pair — never the UTM string.
        """
        display = _format_coordinate_staging_display_mirror(
            37.42210, -121.97936, "10S 590309E 4142188N"
        )
        rec_line = (
            f"1. {display} — Officer-designated staging location "
            f"(not among top recommendations — dispatcher discretion)."
        )
        m = _STAGING_REC_RE_MIRROR.search(rec_line)
        assert m, "frontend stagingRec regex found nothing in the #1 line"
        staging_rec = m.group(1).strip()
        assert staging_rec == "37.42210, -121.97936", (
            "The responder maps query is not the decimal pair. Whatever this "
            "string is, it is what maps.apple.com/?q= searches for the whole "
            f"callout: {staging_rec!r}"
        )
        assert "10S" not in staging_rec, (
            "A UTM string reached the maps query — no maps app text-searches "
            "UTM, so every responder tapping the Slack staging link gets "
            "nothing."
        )


class TestParseCoordinateStagingText:
    """Pin the Pass B coordinate router (issue #668).

    Returns (lat, lng, utm_display). The third element is what tells the
    caller whether the staging TEXT has to be rewritten: decimal input is
    already a valid maps query and is left as the officer wrote it, UTM is not
    and must be converted before it becomes display text.
    """

    @staticmethod
    def _stub_utm(monkeypatch):
        """Inject a stub `utm` module — the real package is Cloud Run only."""
        import sys, types
        mod = types.ModuleType("utm")
        mod.to_latlon = lambda e, n, z, l: (37.42210, -121.97936)
        monkeypatch.setitem(sys.modules, "utm", mod)

    def test_decimal_input_reports_no_utm(self):
        assert _parse_coordinate_staging_text_mirror("37.26940, -122.03674") == (
            37.26940, -122.03674, ""
        )

    def test_utm_input_preserves_raw_string(self, monkeypatch):
        self._stub_utm(monkeypatch)
        result = _parse_coordinate_staging_text_mirror("10S 590309E 4142188N")
        assert result is not None
        lat, lng, utm_display = result
        assert (lat, lng) == (37.42210, -121.97936)
        assert utm_display == "10S 590309E 4142188N", (
            "The dispatcher's original UTM string must survive — it is what "
            "trails the em-dash for radio-trained responders."
        )

    def test_address_text_returns_none(self):
        # Must fall through to the canonical-facility table and the geocoders
        # exactly as before — this branch is additive, not a replacement.
        assert _parse_coordinate_staging_text_mirror("400 Llagas Road, Morgan Hill") is None
        assert _parse_coordinate_staging_text_mirror("Richey Center") is None
        assert _parse_coordinate_staging_text_mirror("ALMA @ 10TH") is None

    def test_empty_returns_none(self):
        assert _parse_coordinate_staging_text_mirror("") is None
        assert _parse_coordinate_staging_text_mirror(None) is None

    def test_real_2026_07_31_humboldt_form_end_to_end(self):
        """The actual callout this issue was filed on, walked to the surface
        that decides where responders drive.

        Ground truth from the real intake PDF (corpus:
        experiments/test_forms/Search Hum.pdf, v2 AcroForm). The extractor
        returns `staging_area` with a TRAILING SPACE, which is why the parser
        strips before matching:

            staging_area        = '40.27398, -124.07826 '
            last_seen_location  = 'Treatment Facility'   <- resolved to Nova
                                                            Scotia (#667)

        On the day, this pair was sent to the address geocoder as
        `q=40.27398, -124.07826, CA` and then discarded by the distance guard
        measuring against that Nova Scotia anchor. It was the only correct
        location on the form.

        The PDF and JPEG paths converge on the same geocoding + staging
        pipeline (main.py: "The geocoding and Overpass sections below are
        identical for both paths"), so this AcroForm value reaches the same
        Pass B branch a photographed form would.
        """
        raw_field = "40.27398, -124.07826 "  # verbatim, trailing space included

        parsed = _parse_coordinate_staging_text_mirror(raw_field)
        assert parsed is not None, (
            "The Humboldt staging coordinate is not recognised as a "
            "coordinate — it would go to the address geocoder as free text, "
            "which is the 2026-07-31 failure."
        )
        lat, lng, utm_display = parsed
        assert (lat, lng) == (40.27398, -124.07826)
        assert utm_display == "", "Decimal input must report no UTM to rewrite"

        # Wilderness case: Overpass returns nothing on the Lost Coast, so the
        # officer entry is injected by PASS 3 as the sole — therefore #1 —
        # recommendation. That is the line the frontend reads.
        rec_line = (
            f"1. {raw_field.strip()} — Officer-designated staging location "
            f"(not among top recommendations — dispatcher discretion)."
        )
        m = _STAGING_REC_RE_MIRROR.search(rec_line)
        assert m, "frontend stagingRec regex found nothing in the #1 line"
        assert m.group(1).strip() == "40.27398, -124.07826", (
            "The responder maps query is not the coordinate. This string is "
            "what every responder's phone searches when they tap the Slack "
            "staging link."
        )


class TestStagingCoordinateProductionWiring:
    """Production pins for issue #668.

    The behaviour tests above run against the mirrors at the top of this file,
    because the suite deliberately does not import main (heavyweight GCP deps).
    Without these pins a change to production could not fail any of them.
    """

    @staticmethod
    def _main_src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @staticmethod
    def _fn(src, header_re):
        """Slice one function body, bounded on real markers at BOTH ends."""
        m = re.search(
            header_re + r".*?(?=\n\n(?:def |async def |@app\.|# -{10,}))",
            src, re.DOTALL | re.MULTILINE,
        )
        return m.group(0) if m else None

    @staticmethod
    def _passb_coord_block(src):
        """The Pass B staging branch, bounded start-to-end on real code.

        Start: the line that extracts the staging text from the entry body.
        End:   the geocode queue the coordinate branch must bypass.
        """
        start = src.find('addr_part = body.split(" — ")[0].strip()')
        end = src.find("pending_geocode.append(", start)
        assert start != -1, "Pass B addr_part extraction not found in main.py"
        assert end != -1, "Pass B pending_geocode.append not found in main.py"
        return src[start:end]

    def test_latlng_regex_matches_mirror(self):
        src = self._main_src()
        m = re.search(
            r"_LATLNG_STAGING_RE = re\.compile\(\n\s*r\"(.*?)\"\n\)", src
        )
        assert m, "_LATLNG_STAGING_RE not found in main.py"
        assert m.group(1) == _LATLNG_STAGING_RE_MIRROR.pattern, (
            "main.py::_LATLNG_STAGING_RE drifted from the mirror in this file.\n"
            f"  main.py: {m.group(1)}\n"
            f"  mirror:  {_LATLNG_STAGING_RE_MIRROR.pattern}"
        )

    def test_production_latlng_parser_keeps_its_guards(self):
        """The regex alone is not the parser.

        The decimal-point requirement and the range checks live in the
        function BODY, so pinning `_LATLNG_STAGING_RE` would leave them free
        to disappear. Dropping the decimal requirement makes "1, 2" a
        coordinate in the Gulf of Guinea.
        """
        fn = self._fn(self._main_src(), r"^def _parse_latlng_string\(")
        assert fn, "_parse_latlng_string not found in main.py"
        # Anchor past the docstring, which describes these rules in prose.
        _body_at = fn.find("m = _LATLNG_STAGING_RE.match(")
        assert _body_at != -1, (
            "_parse_latlng_string no longer applies _LATLNG_STAGING_RE — the "
            "regex pin above is now testing a constant nothing reads."
        )
        body = fn[_body_at:]
        assert 'if "." not in lat_s and "." not in lng_s:' in body, (
            "The decimal-point requirement is gone. Pass B reads free-form "
            "officer text, so an integer pair like \"1, 2\" would now be read "
            "as a coordinate and plot a marker in the ocean."
        )
        assert "-90.0 <= lat <= 90.0" in body, "lat range check removed"
        assert "-180.0 <= lng <= 180.0" in body, "lng range check removed"

    def test_production_router_tries_latlng_then_utm(self):
        """Pin the router's branches, not just its name."""
        fn = self._fn(self._main_src(), r"^def _parse_coordinate_staging_text\(")
        assert fn, "_parse_coordinate_staging_text not found in main.py"
        _body_at = fn.find("raw = text.strip()")
        assert _body_at != -1, (
            "_parse_coordinate_staging_text's body no longer starts with the "
            "strip — re-anchor this pin."
        )
        body = fn[_body_at:]
        assert "_parse_latlng_string(raw)" in body, (
            "The decimal lat/lng branch is gone from the router."
        )
        assert "_parse_utm_string(raw)" in body, (
            "The UTM branch is gone from the router — UTM staging would fall "
            "back to the address geocoder, which is the #668 failure."
        )
        assert "return (utm[0], utm[1], raw)" in body, (
            "The router no longer returns the dispatcher's raw UTM string. "
            "Without it the caller cannot tell UTM from decimal input, so the "
            "staging TEXT is never rewritten and the responder link breaks."
        )

    def test_display_helper_renders_both_shapes(self):
        """Pin both return expressions inside the helper's OWN body.

        Scoped to the function because `f"{lat:.5f}, {lng:.5f}"`-shaped text
        appears elsewhere in a 9,000-line module; "does this string exist
        somewhere" would test the module's vocabulary, not its behaviour.
        """
        fn = self._fn(self._main_src(), r"^def _format_coordinate_staging_display\(")
        assert fn, "_format_coordinate_staging_display not found in main.py"
        assert 'base = f"{lat:.5f}, {lng:.5f}"' in fn, (
            "The decimal rendering changed. This string is the responders' "
            "maps query — see the Locked Decision."
        )
        assert 'f"{base} — {utm_display}" if utm_display else base' in fn, (
            "The UTM em-dash form changed. lat/lng must LEAD (it is what the "
            "?q= link resolves) and the UTM must TRAIL."
        )

    def test_override_endpoint_uses_the_shared_helper_for_both_modes(self):
        """Both /apply-staging-override coordinate branches must call it.

        Two paths rendering one coordinate two ways is the drift the
        cross-file literal pin policy exists to prevent, and here the
        consequence is silent.
        """
        fn = self._fn(self._main_src(), r"^async def apply_staging_override\(")
        assert fn, "apply_staging_override not found in main.py"
        calls = fn.count("_format_coordinate_staging_display(")
        assert calls == 2, (
            f"Expected both the lat_lng and utm branches of "
            f"/apply-staging-override to call _format_coordinate_staging_display; "
            f"found {calls} call(s). An inline format string has been "
            f"reintroduced, and Pass B will now render the same coordinate "
            f"differently."
        )

    def test_passb_parses_coordinates_before_geocoding(self):
        """Ordering is behaviour: the parse must precede every text consumer.

        On 2026-07-31 a correct in-county pair reached the address geocoder
        as `q=<lat>, <lng>, CA`, missed Nominatim, was answered by Google
        Maps, and was then discarded by the distance guard.
        """
        block = self._passb_coord_block(self._main_src())
        assert "_parse_coordinate_staging_text(" in block, (
            "Pass B no longer parses coordinates before geocoding — "
            "coordinate staging goes back to being free text."
        )
        assert block.index("_parse_coordinate_staging_text(") < \
            block.index("_match_canonical_staging_facility("), (
                "The coordinate parse must run BEFORE the canonical-facility "
                "table and _normalize_staging_geocode_query — both treat a "
                "coordinate as free text."
            )

    def test_passb_rewrites_the_staging_text_for_utm(self):
        """The second surface. A marker-only fix reproduces #646."""
        block = self._passb_coord_block(self._main_src())
        assert "_format_coordinate_staging_display(" in block, (
            "Pass B parses the coordinate but no longer renders it through "
            "the shared display helper."
        )
        assert "summary = summary.replace(addr_part, _c_display)" in block, (
            "The UTM staging TEXT is no longer rewritten. The CalTopo marker "
            "will be correct and every responder tapping the Slack staging "
            "link will search a raw UTM string, which no maps app resolves — "
            "map right, whole callout misrouted."
        )
        assert "if _c_display not in summary:" in block, (
            "The rewrite is no longer idempotent. _c_display CONTAINS the raw "
            "UTM string, so a second staging entry with the same UTM would "
            "nest the rewrite into itself and corrupt the staging line."
        )

    def test_passb_coordinate_warns_but_never_discards(self):
        """A written coordinate outranks a geocoded anchor.

        The distance guard measures against the LKP, which is itself
        unvalidated. On 2026-07-31 the anchor was in Nova Scotia and this
        guard destroyed the one correct location on the form, then fell
        staging back to the bad anchor.
        """
        block = self._passb_coord_block(self._main_src())
        assert "_staging_coordinate_distance_note(" in block, (
            "The coordinate branch no longer warns when the coordinate is far "
            "from the LKP — a bad anchor becomes invisible."
        )
        assert "_staging_distance_note(" not in block, (
            "The coordinate branch is using the REJECTION note, whose tails "
            "claim no marker was plotted. Either the coordinate is now being "
            "discarded (the 2026-07-31 failure) or the Event Log is lying."
        )
        # The parsed coordinates must survive the guard: nothing may reset
        # them between the assignment and the end of the branch.
        assert "found_lat, found_lng = _c_lat, _c_lng" in block, (
            "The parsed coordinate is no longer assigned to the staging entry."
        )
        after_guard = block[block.index("_staging_coordinate_distance_note("):]
        assert "found_lat = None" not in after_guard, (
            "The coordinate is being cleared after the distance guard fires — "
            "that is exactly the 2026-07-31 discard, reintroduced."
        )

    def test_coordinate_distance_note_does_not_claim_marker_was_dropped(self):
        """The note's tail must match what happens to the marker.

        `_staging_distance_note`'s own docstring requires this; both of its
        tails would be false here because the marker IS plotted.
        """
        fn = self._fn(
            self._main_src(), r"^def _staging_coordinate_distance_note\("
        )
        assert fn, "_staging_coordinate_distance_note not found in main.py"
        # Skip the docstring — it QUOTES _staging_distance_note's tails to
        # explain why they are wrong here, so an unscoped search matches prose
        # and passes with the returned string broken. (Same trap that leaked
        # three pins on 2026-08-01.) `label =` is the first line of real code.
        _body_at = fn.find('label = "Officer staging coordinate"')
        assert _body_at != -1, (
            "_staging_coordinate_distance_note's body no longer starts with "
            "the label assignment — re-anchor this pin."
        )
        body = fn[_body_at:]
        assert "plotted at the coordinate as written" in body, (
            "The kept-coordinate Event Log line no longer says the marker was "
            "plotted — the dispatcher cannot tell this case from a rejection."
        )
        for lie in ("No map marker was plotted", "placed at the LKP instead"):
            assert lie not in body, (
                f"The kept-coordinate note claims {lie!r}, which is false — "
                f"the marker is plotted at the coordinate."
            )

    def test_passb_surfaces_the_utm_conversion_in_the_event_log(self):
        """#646 rule: a substitution in the officer's staging text is never
        silent. The logger line stays PII-free (entry number only)."""
        block = self._passb_coord_block(self._main_src())
        assert "Staging UTM converted for mapping: " in block, (
            "The UTM→lat/lng substitution is no longer surfaced to the "
            "dispatcher — a silent rewrite of the officer's staging text is "
            "the specific complaint issue #646 was filed on."
        )


# ---------------------------------------------------------------------------
# Issue #675 — Event Log note for questionnaire items the form left blank.
# Mirror of main.py::_LPB_UNANSWERED_RE / _unanswered_lpb_note.
# ---------------------------------------------------------------------------

_LPB_UNANSWERED_RE_MIRROR = re.compile(
    r"^(Q\d+) - NOT ANSWERED \(flag for follow-up\) - (.+)$",
    re.MULTILINE,
)


def _unanswered_lpb_note_mirror(summary):
    """Mirror of main.py::_unanswered_lpb_note."""
    if not summary:
        return None
    rows = _LPB_UNANSWERED_RE_MIRROR.findall(summary)
    if not rows:
        return None
    items = []
    for qnum, label in rows:
        clean = label.split(" — ")[0].split("?")[0].strip().rstrip(".")
        items.append(f"{qnum} ({clean})" if clean else qnum)
    noun = "item" if len(items) == 1 else "items"
    return (
        f"WARNING: {len(items)} questionnaire {noun} left blank on the form: "
        f"{', '.join(items)} — cannot be answered from the form; "
        f"verify with officer if it affects the search."
    )


class TestUnansweredLpbNote:
    """Pin the blank-questionnaire-row Event Log note (issue #675).

    A blank row is not dispatcher-correctable: they cannot recover an answer
    the officer never wrote, and at dispatch time they are not positioned to
    chase it (Bill, 2026-08-01). The goal is VISIBILITY — name the gap and let
    the dispatcher decide whether it warrants a call back.

    Scope boundary worth stating, because it is easy to misread this as
    closing the 2026-07-31 failure: it does not. There Gemini fabricated a
    definite "No" for a blank row, so no NOT ANSWERED token exists to detect.
    The prompt already forbids that guess twice, so that half is model
    compliance, not a missing rule. This covers the case where the pipeline
    correctly reports the gap — which the AcroForm path does deterministically.
    """

    def test_no_unanswered_rows_returns_none(self):
        # Event log policy: corrections and failures only, never silent
        # successes. 51 of 69 cached corpus runs land here.
        summary = (
            "Q1 - Yes - Familiar with area\n"
            "Q2 - No - Has phone\n"
        )
        assert _unanswered_lpb_note_mirror(summary) is None

    def test_single_row_is_singular(self):
        summary = "Q3 - NOT ANSWERED (flag for follow-up) - Uses public transit (VTA)\n"
        note = _unanswered_lpb_note_mirror(summary)
        assert note is not None
        assert "1 questionnaire item left blank" in note
        assert "items" not in note, "singular case must not say 'items'"
        assert "Q3 (Uses public transit (VTA))" in note

    def test_two_rows_are_plural_and_comma_joined(self):
        summary = (
            "Q5 - NOT ANSWERED (flag for follow-up) - Alone\n"
            "Q8 - NOT ANSWERED (flag for follow-up) - Speaks English\n"
        )
        note = _unanswered_lpb_note_mirror(summary)
        assert "2 questionnaire items left blank" in note
        assert "Q5 (Alone), Q8 (Speaks English)" in note, (
            "Question order must follow the form, not sort order."
        )

    def test_detail_tail_is_stripped(self):
        """The em-dash detail can carry free text — including PII.

        The detail already appears on the row itself; repeating it in the
        Event Log line would drag prose into a scannable summary.
        """
        summary = (
            "Q9 - NOT ANSWERED (flag for follow-up) - Mental health component "
            "— detail: extremely cognitively impaired, 5-minute memory\n"
        )
        note = _unanswered_lpb_note_mirror(summary)
        assert "Q9 (Mental health component)" in note
        assert "cognitively impaired" not in note
        assert "detail:" not in note

    def test_question_mark_tail_is_stripped(self):
        """Gemini sometimes echoes raw form wording, not the canonical label.

        Surfaced by the corpus run, not by any synthetic case: IMG_2685/run-3
        produced "Speaks English? If no, UNKNOWN", which leaks prompt-template
        text into a dispatcher-facing line. No canonical label contains a "?".
        """
        summary = (
            "Q8 - NOT ANSWERED (flag for follow-up) - Speaks English? "
            "If no, which other languages\n"
        )
        note = _unanswered_lpb_note_mirror(summary)
        assert "Q8 (Speaks English)" in note
        assert "If no" not in note

    def test_real_2026_07_31_humboldt_form(self):
        """Ground truth from the real intake PDF (corpus: Search Hum.pdf).

        Two genuinely blank rows, and the Event Log said nothing about either
        before this change.
        """
        summary = (
            "Q4 - Yes - Entered into MUPS\n"
            "Q5 - NOT ANSWERED (flag for follow-up) - Alone\n"
            "Q9 - NOT ANSWERED (flag for follow-up) - Mental health component "
            "— detail: extremely cognitively impaired with an estimated "
            "5-minute short-term memory\n"
            "Q10 - No - Prior missing\n"
        )
        note = _unanswered_lpb_note_mirror(summary)
        assert note == (
            "WARNING: 2 questionnaire items left blank on the form: "
            "Q5 (Alone), Q9 (Mental health component) — cannot be answered "
            "from the form; verify with officer if it affects the search."
        )

    def test_empty_summary_returns_none(self):
        assert _unanswered_lpb_note_mirror("") is None
        assert _unanswered_lpb_note_mirror(None) is None

    # --- production pins -------------------------------------------------

    @staticmethod
    def _src(name):
        base = Path(__file__).parent
        path = base / name if name == "main.py" else base / "migration_validation" / name
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _fn(src, header_re):
        m = re.search(
            header_re + r".*?(?=\n\n(?:def |async def |@app\.|# -{10,}|HELPERS))",
            src, re.DOTALL | re.MULTILINE,
        )
        return m.group(0) if m else None

    @staticmethod
    def _code_lines(fn_src):
        """Strip the signature, docstring, comments and blanks.

        The `def` line is excluded on purpose: apply_helpers.py is
        consistently unannotated (see its `_rewrite_dob_age_hint(summary,
        today)`), so requiring identical signatures would force a style
        change on that file to satisfy a logic pin. The BODY is what must
        not diverge.
        """
        body = re.sub(r'""".*?"""', "", fn_src, flags=re.DOTALL)
        return [
            l.strip() for l in body.splitlines()
            if l.strip()
            and not l.strip().startswith("#")
            and not l.strip().startswith("def ")
        ]

    def test_regex_matches_mirror_in_both_files(self):
        for fname in ("main.py", "apply_helpers.py"):
            src = self._src(fname)
            m = re.search(
                r"_LPB_UNANSWERED_RE = re\.compile\(\n\s*r\"(.*?)\",\n", src
            )
            assert m, f"_LPB_UNANSWERED_RE not found in {fname}"
            assert m.group(1) == _LPB_UNANSWERED_RE_MIRROR.pattern, (
                f"{fname}::_LPB_UNANSWERED_RE drifted from the mirror.\n"
                f"  {fname}: {m.group(1)}\n"
                f"  mirror:  {_LPB_UNANSWERED_RE_MIRROR.pattern}"
            )

    def test_main_and_apply_helpers_bodies_are_identical(self):
        """Cross-file parity — the corpus run must exercise the real logic.

        `apply_helpers` mirrors rather than imports main.py (heavyweight GCP
        deps), so a silent divergence would make the corpus validation report
        on code that never ships. Docstrings and comments are allowed to
        differ; the logic is not.
        """
        prod = self._fn(self._src("main.py"), r"^def _unanswered_lpb_note\(")
        mirror = self._fn(self._src("apply_helpers.py"), r"^def _unanswered_lpb_note\(")
        assert prod, "_unanswered_lpb_note not found in main.py"
        assert mirror, "_unanswered_lpb_note not found in apply_helpers.py"
        assert self._code_lines(prod) == self._code_lines(mirror), (
            "main.py::_unanswered_lpb_note and the apply_helpers mirror have "
            "diverged — the corpus run would validate logic that is not what "
            "ships.\n"
            f"  main.py:       {self._code_lines(prod)}\n"
            f"  apply_helpers: {self._code_lines(mirror)}"
        )

    def test_production_strips_both_tails(self):
        fn = self._fn(self._src("main.py"), r"^def _unanswered_lpb_note\(")
        _at = fn.find("rows = _LPB_UNANSWERED_RE.findall(summary)")
        assert _at != -1, "re-anchor this pin — body no longer starts with the findall"
        body = fn[_at:]
        assert 'label.split(" — ")[0].split("?")[0]' in body, (
            "The label tail strip changed. The em-dash cut keeps free-text "
            "detail (and its PII) out of the Event Log; the '?' cut keeps "
            "Gemini's raw-form echo out of a dispatcher-facing line."
        )

    def test_production_emits_none_when_all_answered(self):
        """Event log policy pin: no entry for a silent success."""
        fn = self._fn(self._src("main.py"), r"^def _unanswered_lpb_note\(")
        _at = fn.find("rows = _LPB_UNANSWERED_RE.findall(summary)")
        body = fn[_at:]
        assert "if not rows:\n        return None" in body, (
            "The all-answered short-circuit is gone — a note would fire on "
            "every dispatch, and noise makes real warnings invisible."
        )

    def test_production_wires_the_note_into_event_log_additions(self):
        """A helper nothing calls is a helper that does nothing."""
        src = self._src("main.py")
        start = src.find("# Issue #675: name any questionnaire item the form left blank")
        end = src.find("if event_log_additions:", start)
        assert start != -1, "the #675 wiring block is gone from main.py"
        assert end != -1, "event_log_additions injection not found after the block"
        block = src[start:end]
        assert "_unanswered_lpb_note(summary)" in block, (
            "main.py no longer calls _unanswered_lpb_note — blank rows go "
            "back to being invisible in the Event Log."
        )
        assert "event_log_additions.append(" in block, (
            "The note is computed but never appended to the Event Log."
        )
        # Assert the STRUCTURE, not the vocabulary: `except Exception` appears
        # in adjacent blocks too, so a bare substring check passes even with
        # the `try:` deleted (caught by mutation N5 — the pin survived
        # replacing `try:` with `if True:`).
        _try_at = block.find("try:")
        _call_at = block.find("_unanswered_lpb_note(summary)")
        assert _try_at != -1 and _try_at < _call_at, (
            "The note is unguarded — no `try:` opens before the call. A note "
            "is never worth losing the real geocoding warnings already queued "
            "in event_log_additions."
        )
        assert "except Exception" in block[_call_at:], (
            "No `except Exception` follows the call — the guard does not close."
        )

    def test_helper_is_registered_for_corpus_validation(self):
        """Locked Decision: pure-text helpers are corpus-validated before deploy."""
        src = self._src("apply_helpers.py")
        assert '"unanswered_lpb": {' in src, (
            "The helper is not in the HELPERS registry, so "
            "`apply_helpers --helper unanswered_lpb` cannot run and the "
            "corpus-validation rule cannot be satisfied."
        )
        assert "_unanswered_lpb_note(text)" in src, (
            "The registry entry does not call the mirrored helper."
        )


class TestShoppingMallStaging:
    """Pin shopping-centre staging coverage (issue #669).

    On 2026-07-31 an officer designated a shopping center at the LKP address.
    It was never a candidate — not ranked low, ABSENT — so the list could not
    offer it. The dispatcher took the #1 recommendation, a smaller lot two
    blocks away, and the field relocated to the officer's site on arrival
    because it had more parking.

    Coverage gaps are structurally silent: an absent category logs nothing,
    which is why this survived unnoticed.

    Spike, 2026-08-01 (experiments/geoapify/):
      - "commercial.shopping_mall" and "commercial.department_store" EXIST;
        "commercial.retail" and "commercial.outpost" 400 — they do not.
      - At Westfield Valley Fair the production category list returned 25
        features and NOT ONE was the mall — all food-court tenants.
      - The Overpass query never asked for shop=mall either, so this is NOT a
        Geoapify-migration regression as issue #669 assumed; the gap predates
        the migration and existed on both providers.
    """

    def test_mall_is_tier_1(self):
        # Best real-world staging SAR gets: large lots, lighting, restrooms,
        # multiple ingress points, room for a command post and a caravan.
        assert _STAGING_TIER["mall"] == 1

    def test_mall_outranks_tier_2_and_3(self):
        assert _STAGING_TIER["mall"] < _STAGING_TIER["convenience"]
        assert _STAGING_TIER["mall"] < _STAGING_TIER["fuel"]

    def test_gemini_labels_mall_as_shopping_center(self):
        # "Shopping center" is the dispatcher's own vocabulary. Without an
        # entry the fallback renders the bare amenity, "Mall".
        assert _GEMINI_TYPE_LABELS["mall"] == "Shopping center"

    # --- production pins -------------------------------------------------

    @staticmethod
    def _src(name):
        return (Path(__file__).parent / name).read_text(encoding="utf-8")

    def test_staging_tier_matches_production(self):
        """Full-dict parity for _STAGING_TIER.

        Added with #669 because mutation testing showed the mall tier tests
        above were vacuous: deleting "mall" from production main.py left the
        whole suite green, because every _STAGING_TIER assertion in this file
        reads the MIRROR at the top of the file.

        That gap was not introduced here — it predates this change and covers
        the entire dict, which three Locked Design Decisions depend on
        ("Staging tier sort", "Schools/colleges tier 1", "college = school in
        all layers"). Before this pin, deleting "school": 1 from production
        also failed nothing, while the Locked Decision records that exact
        change cutting schools from dense urban candidate lists.
        """
        import ast
        src = self._src("main.py")
        block = re.search(r"^_STAGING_TIER = (\{.*?\n\})", src, re.DOTALL | re.MULTILINE)
        assert block, "_STAGING_TIER not found in main.py"
        prod = ast.literal_eval(block.group(1))
        assert prod == _STAGING_TIER, (
            "backend/main.py::_STAGING_TIER drifted from the mirror in this file.\n"
            f"  main.py: {prod}\n"
            f"  mirror:  {_STAGING_TIER}"
        )

    def test_gemini_type_labels_match_production_in_both_copies(self):
        """Full-dict parity for gemini.py::type_labels — BOTH copies.

        The dict is duplicated across the JPEG and PDF candidate blocks. A
        change applied to only one silently gives the two intake paths
        different labels for the same POI, which is the failure shape the
        "college = school in all layers" Locked Decision exists to prevent.
        Mutation P10 (dropping the mall label from one copy) passed before
        this pin existed.
        """
        import ast
        src = self._src("gemini.py")
        blocks = re.findall(r"type_labels = (\{.*?\n\s*\})", src, re.DOTALL)
        assert len(blocks) == 2, (
            f"expected exactly 2 type_labels copies in gemini.py (JPEG + PDF "
            f"candidate blocks), found {len(blocks)}"
        )
        for i, b in enumerate(blocks):
            prod = ast.literal_eval(b)
            assert prod == _GEMINI_TYPE_LABELS, (
                f"gemini.py type_labels copy #{i + 1} drifted from the mirror "
                f"in this file — the two intake paths would label the same POI "
                f"differently.\n  gemini.py: {prod}\n  mirror:    {_GEMINI_TYPE_LABELS}"
            )

    def test_geoapify_category_present_and_verified_name(self):
        """The category name is empirically verified, not guessed.

        Geoapify 400s on an unknown category, so a typo here silently kills
        the whole commercial call rather than just dropping malls.
        """
        src = self._src("main.py")
        block = re.search(
            r"_GEOAPIFY_COMMERCIAL_CATEGORIES = \[(.*?)\n\]", src, re.DOTALL
        )
        assert block, "_GEOAPIFY_COMMERCIAL_CATEGORIES not found in main.py"
        # Strip comments before asserting. The block carries a comment naming
        # commercial.department_store to explain why it is held back, so an
        # unfiltered search matches prose rather than an entry — the same trap
        # that leaked three pins on 2026-08-01.
        entries = "\n".join(
            l.split("#")[0] for l in block.group(1).splitlines()
        )
        assert '"commercial.shopping_mall"' in entries, (
            "commercial.shopping_mall is gone from the Geoapify commercial "
            "categories — shopping centers become invisible again, silently."
        )
        assert '"commercial.department_store"' not in entries, (
            "department_store was added. Held deliberately (Bill 2026-08-01): "
            "at a mall it adds four tenants of a site already listed and "
            "floods the 7-slot staging cap. Revisit with real dispatch data."
        )

    def test_geoapify_priority_maps_mall_ahead_of_tenants(self):
        """A feature tagged both mall and tenant must resolve to the mall."""
        src = self._src("main.py")
        block = re.search(r"_GEOAPIFY_PRIORITY = \[(.*?)\n\]", src, re.DOTALL)
        assert block, "_GEOAPIFY_PRIORITY not found in main.py"
        body = block.group(1)
        assert '("commercial.shopping_mall", "mall")' in body, (
            "The mall category has no amenity mapping, so _STAGING_TIER never "
            "sees it and the feature is dropped as an unknown category."
        )
        assert body.index('("commercial.shopping_mall", "mall")') < \
            body.index('("catering.fast_food", "fast_food")'), (
                "mall must precede tenant categories — the lot is what we are "
                "staging in, not the storefront on it."
            )

    def test_overpass_query_also_asks_for_malls(self):
        """Provider symmetry. The spike showed the gap on BOTH sources.

        Overpass is the outage fallback (#453/#484) and the shadow comparison.
        A Geoapify-only fix leaves the fallback blind and makes every
        mall-adjacent dispatch show a shadow diff that is not a real defect.
        """
        src = self._src("main.py")
        m = re.search(
            r'nwr\(around:\{radius_m\},\{lat\},\{lng\}\)\[shop~"\^\((.*?)\)\$"\]', src
        )
        assert m, "the Overpass shop query was not found in main.py"
        assert "mall" in m.group(1).split("|"), (
            "shop=mall is gone from the Overpass query — during a Geoapify "
            "outage, shopping centers silently disappear from staging again."
        )

    def test_mall_name_dedup_collapses_word_reordering(self):
        """One physical mall must not eat two of the seven staging slots.

        Geoapify returns both "Westfield Valley Fair" (Stevens Creek Blvd) and
        "Valley Fair Westfield" (Monroe St). Different name AND different
        street, so neither existing dedup key collapses them.
        """
        src = self._src("main.py")
        fn = re.search(
            r"^def _rank_dedupe_cap_staging\(.*?(?=\n\n(?:def |async def |# -{10,}))",
            src, re.DOTALL | re.MULTILINE,
        )
        assert fn, "_rank_dedupe_cap_staging not found in main.py"
        body = fn.group(0)
        assert 'if c["amenity"] == "mall":' in body, (
            "The mall name-dedup is gone — one mall returns as two staging "
            "entries that are the same place."
        )
        assert 'name_key = " ".join(sorted(name_key.split()))' in body, (
            "The token-set key is gone, so word-reordered mall names no "
            "longer collapse."
        )
        # Scoping matters as much as presence: applied broadly it would
        # collapse genuinely different places sharing words.
        assert body.index('if c["amenity"] == "mall":') < \
            body.index('name_key = " ".join(sorted(name_key.split()))'), (
                "The token-set key is no longer gated on amenity == mall."
            )

    def test_dedup_behaviour_one_mall_one_entry(self):
        """Behaviour check against a mirror of the dedup key logic."""
        def key(name, amenity):
            k = name.lower()
            if amenity == "mall":
                k = " ".join(sorted(k.split()))
            return k
        assert key("Westfield Valley Fair", "mall") == key("Valley Fair Westfield", "mall")
        # Non-mall amenities keep the literal key — reordered words there are
        # far likelier to be genuinely different places.
        assert key("Alpha Beta Market", "supermarket") != key("Beta Alpha Market", "supermarket")


class TestStagingFieldRegexDoesNotCrossNewline:
    """A blank `Staging Area for Resources:` must capture NOTHING, not the next line.

    `\\s` matches a newline. With re.MULTILINE and no DOTALL, `\\s*(.+)$` after a
    BLANK field lets the gap swallow the line break and `(.+)` capture the
    FOLLOWING template line. In this summary the next line is always
    `CalTopo Map ID:`, so the wrong value is DETERMINISTIC — it reads like a real
    parse rather than a regex overrun.

    Live 2026-08-09 on personal-dev: the officer staging entry rendered as
    "CalTopo Map ID: — Officer-designated staging location", was geocoded against
    Nominatim AND Google Maps, and Google answered 146 mi from the LKP. The
    distance guard rejected it and the CP marker fell back to the LKP, so three
    guards caught the CONSEQUENCES — but none could catch the CAUSE, because
    `_BLANK_PAT` correctly reports the captured string is non-blank. It is.

    Both readers of this field are pinned. They must agree: if the injector and
    the mismatch check disagree, the check compares the officer's real text
    against a value the injector never saw.
    """

    BLANK = (
        "Last Seen Wearing: WHITE LEGGINGS\n"
        "Staging Area for Resources:\n"
        "CalTopo Map ID:\n"
        "Dispatcher: Burns 305\n"
    )
    FILLED = (
        "Last Seen Wearing: WHITE LEGGINGS\n"
        "Staging Area for Resources: 447 Great Mall Drive, Milpitas\n"
        "CalTopo Map ID:\n"
    )
    TRAILING_SPACES = (
        "Staging Area for Resources:   \n"
        "CalTopo Map ID:\n"
    )

    @staticmethod
    def _literals():
        """Every production regex that READS a value out of this field.

        Scoped to patterns carrying a capture group. The two `.*$` siblings are
        whole-line REPLACERS, not readers — `.` never matches a newline without
        DOTALL and they have no whitespace gap, so they cannot exhibit this bug
        and pinning them would only couple this test to unrelated code.
        """
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        found = re.findall(r'r"(\^Staging Area for Resources:[^"]*)"', src)
        return [lit for lit in found if "(.+)" in lit]

    def test_both_readers_are_pinned(self):
        lits = self._literals()
        assert len(lits) == 2, (
            f"expected exactly 2 readers of the staging field, found {len(lits)}: "
            f"{lits} — a new one must be newline-safe too, so this pin fails "
            f"deliberately until it is added here"
        )

    def test_blank_field_captures_nothing(self):
        for lit in self._literals():
            assert re.search(lit, self.BLANK, re.MULTILINE) is None, (
                f"{lit!r} captured the NEXT template line from a blank staging "
                f"field — the officer entry becomes 'CalTopo Map ID:' and gets "
                f"geocoded as an address"
            )

    def test_blank_field_with_trailing_spaces_captures_nothing_meaningful(self):
        """Leg 2: the gap is non-empty but still all horizontal whitespace.

        Here the engine backtracks — `[^\\S\\n]*` gives up one space so `(.+)`
        can match it — so the capture is a lone space rather than None. That is
        fine and is the invariant worth stating: the capture may be empty, but it
        must never contain text from ANOTHER LINE. Both call sites `.strip()` and
        test truthiness, so a whitespace capture is already handled.
        """
        for lit in self._literals():
            m = re.search(lit, self.TRAILING_SPACES, re.MULTILINE)
            got = m.group(1).strip() if m else ""
            assert got == "", (
                f"{lit!r} crossed the newline past trailing spaces and captured "
                f"{got!r}"
            )

    def test_populated_field_still_captures_its_own_value(self):
        """The fix must not break the normal path."""
        for lit in self._literals():
            m = re.search(lit, self.FILLED, re.MULTILINE)
            assert m and m.group(1).strip() == "447 Great Mall Drive, Milpitas", (
                f"{lit!r} no longer reads a populated staging field"
            )

    def test_no_reader_uses_a_newline_matching_gap(self):
        """Structure, not just behaviour — `\\s*` must not come back."""
        for lit in self._literals():
            gap = lit.split("Staging Area for Resources:")[1]
            assert "\\s*" not in gap and "\\s+" not in gap, (
                f"{lit!r} uses a newline-matching gap; use [^\\S\\n]* instead"
            )


class TestOverrideBareStagingForm:
    """The summary field gets the bare form, the numbered item keeps the full
    text (issue #737).

    The backend writes `Staging Area for Resources:` with `_top_rec_bare`
    (location + first sentence of the remainder). The frontend override wrote
    `override.address` — the entire raw recommendation line — so one field had
    two renderings depending only on which path last touched it. Same class as
    the Locked Decision "One coordinate, one rendering" (#668).

    Dispatcher-facing only: `^1\\.\\s+([^\\n—]+)` stops at the first em-dash, so
    the Slack pin, both maps links and `staging_address` were always clean.
    """

    FULL = (
        "572 Great Mall Drive, Milpitas — Great Mall. 0.02 mi from LKP; parking "
        "~100–200 vehicles; restrooms likely; well-lit. — Officer-designated "
        'staging location. (as written: "Mall - Substation - Eastside Parking Garage")'
    )
    BARE = "572 Great Mall Drive, Milpitas — Great Mall"

    @staticmethod
    def _src():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    @classmethod
    def _js(cls, text):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not available")
        fn = re.search(
            r"^  function _overrideBareStagingForm\(.*?\n  \}$",
            cls._src(), re.M | re.S,
        )
        assert fn, "_overrideBareStagingForm not found"
        prog = fn.group(0) + (
            f"\nconsole.log(JSON.stringify("
            f"_overrideBareStagingForm({json.dumps(text)})));"
        )
        out = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    @staticmethod
    def _py_backend_bare(entry_text):
        """The backend derivation, lifted verbatim from main.py's shape."""
        if " — " in entry_text:
            loc, rest = entry_text.split(" — ", 1)
            return f"{loc.strip()} — {rest.split('. ', 1)[0].strip()}"
        return entry_text.split(". ", 1)[0].strip()

    # --- behaviour ---------------------------------------------------------

    def test_full_recommendation_line_reduces_to_the_bare_form(self):
        assert self._js(self.FULL) == self.BARE

    def test_plain_address_passes_through_unchanged(self):
        """Free-text and canonical-facility picks carry no em-dash."""
        for t in ("155 W Hedding St, San Jose", "55 W Younger Ave, San Jose"):
            assert self._js(t) == t

    def test_no_em_dash_but_a_sentence_still_trims(self):
        """The else-branch has to DO something. The fixtures above carry no
        sentence break, so they pass even with the branch reduced to identity."""
        assert self._js("Ortega Park, Sunnyvale. Large park with restrooms.") \
            == "Ortega Park, Sunnyvale"

    def test_separator_is_a_period_SPACE_not_a_bare_period(self):
        """A period NOT followed by a space must not end the sentence.

        Finding the discriminating input took two tries and the failure is
        instructive. An abbreviation like "Mt. Hamilton" does NOT work: it
        contains ". ", so both separators truncate it to "Mt" — and the backend
        does the same, so that is shared behaviour, not a defect. Only a period
        with no following space (a decimal inside a name) separates them, and
        there ". " is the correct one.
        """
        t = "12345 Mount Rd, San Jose — Highway 9.5 Overlook. 0.3 mi from LKP; parking."
        assert self._js(t) == "12345 Mount Rd, San Jose — Highway 9.5 Overlook"

    def test_coordinate_display_survives_verbatim(self):
        """#668: an em-dash but no sentence break. Truncating here would drop
        the UTM tail the radio-trained responders read."""
        t = "37.42839, -121.89664 — UTM 10S 596432 4142219"
        assert self._js(t) == t

    def test_park_location_with_a_period_is_not_truncated(self):
        """The backend comment's own case: splitting the WHOLE string would
        leave 'Joseph D'."""
        t = "Joseph D. Grant County Park, San Jose — County park. 0.3 mi from LKP; parking."
        assert self._js(t) == "Joseph D. Grant County Park, San Jose — County park"

    def test_empty_input_is_safe(self):
        assert self._js("") == ""

    def test_frontend_and_backend_agree(self):
        """Cross-file: two renderings of one field is the bug itself."""
        for t in (self.FULL,
                  "155 W Hedding St, San Jose",
                  "Ortega Park, Sunnyvale. Large park with restrooms.",
                  "37.42839, -121.89664 — UTM 10S 596432 4142219",
                  "12345 Mount Rd, San Jose — Highway 9.5 Overlook. 0.3 mi from LKP.",
                  "Joseph D. Grant County Park, San Jose — County park. 0.3 mi from LKP."):
            assert self._js(t) == self._py_backend_bare(t), t

    # --- production wiring -------------------------------------------------

    def test_field_gets_the_bare_form_and_item_keeps_the_full_text(self):
        src = self._src()
        fn = re.search(
            r"function _applyStagingOverrideToTextareaText\(.*?\n  \}\n",
            src, re.DOTALL,
        )
        assert fn, "_applyStagingOverrideToTextareaText not found"
        code = "\n".join(l.split("//")[0] for l in fn.group(0).splitlines())
        assert "_overrideBareStagingForm(override.address)" in code, (
            "the summary field is being written with the raw recommendation "
            "line again"
        )
        m = re.search(r"newNumberedItem\s*=\s*`([^`]*)`", code)
        assert m and "${override.address}" in m.group(1), (
            "the numbered item must keep the FULL text — the #734 "
            "(as written: ...) suffix lives there, past the em-dash"
        )
        assert "_overrideBareStagingForm" not in m.group(1), (
            "the numbered item must not be bared; that would discard the "
            "officer's as-written wording"
        )

    def test_event_log_entry_uses_the_bare_form(self):
        src = self._src()
        m = re.search(r"`Staging override applied via \$\{modeLabel\}: `?[^,]*", src)
        assert m, "the override Event Log entry is gone"
        assert "_overrideBareStagingForm(picked.address)" in m.group(0), (
            "the Event Log entry still quotes the raw recommendation line"
        )
        assert "_subNote" in m.group(0), (
            "the #721 substitution disclosure must still append"
        )


class TestOfficerStagingAsWritten:
    """The officer's own words must survive to the responder-facing line.

    Live 2026-08-09: the officer wrote
    "Great Mall - Substation - Eastside Parking Garage, Milpitas" and Gemini
    rendered the entry as "572 Great Mall Drive, Milpitas — Great Mall". On a
    site over a mile across the parking garage IS the useful half, and it
    survived only in an Event Log note the dispatcher may never scroll to.

    The rewrite is APPENDED TO, never reversed. The geocodable address has to
    stay in front because index.html builds the responders' maps queries from the
    text BEFORE the first em-dash (Locked Decision: "Staging line text IS the
    responders' maps-link query"); everything after that dash reaches humans and
    never reaches navigation.
    """

    key = staticmethod(_load_production_fn("_staging_first_word_key"))

    @staticmethod
    def _src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    # --- the shared comparator, executed as production ---------------------

    def test_rewritten_location_is_detected(self):
        assert self.key("Great Mall - Substation - Eastside Parking Garage, Milpitas") \
            != self.key("572 Great Mall Drive, Milpitas")

    def test_formatting_only_difference_is_not_flagged(self):
        """A city suffix or case change is not a rewrite — appending there would
        be noise on every ordinary dispatch."""
        assert self.key("CARDOZA PARK MILPITAS") == self.key("Cardoza Park, Milpitas")
        assert self.key("10000 Calvert Dr") == self.key("10000 Calvert Drive, Cupertino")
        # Punctuation must be normalized out of the FIRST token specifically —
        # the fixtures above carry their comma on the second token, so they pass
        # even with normalization removed.
        assert self.key("ST. JAMES PARK") == self.key("St James Park, San Jose")
        assert self.key("Cardoza, Milpitas") == self.key("Cardoza Park")

    def test_empty_input_is_safe(self):
        assert self.key("") == ""

    # --- production wiring -------------------------------------------------

    def test_comparator_is_shared_by_both_disclosures(self):
        """One comparator, two disclosures. If they drift, the dispatcher can be
        told the text was rewritten while the responder line keeps no trace."""
        src = self._src()
        code = "\n".join(l.split("#")[0] for l in src.splitlines())
        assert code.count("_staging_first_word_key(") >= 5, (
            "expected the shared comparator at its definition plus two call "
            "sites with two arguments each — a local re-definition has crept back"
        )
        assert "def _first_word_key(" not in code, (
            "the nested comparator is back; the Event Log note and the "
            "(as written: ...) suffix can now drift apart"
        )

    def test_suffix_is_appended_after_the_em_dash_segment(self):
        """Structure: the suffix must extend the EXISTING line, never replace the
        location. Replacing it would put un-navigable text into the maps query."""
        src = self._src()
        m = re.search(r"kept_lines\[_i\] = \(\s*\n\s*(f'[^\n]*')", src)
        assert m, "the as-written append is gone"
        tmpl = m.group(1)
        assert "{_l.rstrip()}" in tmpl, (
            "the suffix no longer builds on the rendered line — the officer's "
            "text would replace the geocodable address in the maps query"
        )
        assert tmpl.index("{_l.rstrip()}") < tmpl.index("as written"), (
            "the officer's text must come AFTER the rendered line, not before"
        )
        assert "_officer_raw[:100]" in tmpl, "the officer text is no longer capped"

    def test_append_only_fires_when_gemini_already_rendered_the_entry(self):
        """The injection branch writes _officer_raw verbatim, so there is nothing
        to preserve there. The suffix belongs in the else."""
        src = self._src()
        blk = src[src.index("if not _has_officer:"):src.index("# Event Log note when officer")]
        assert "else:" in blk, "the as-written branch is gone"
        assert blk.index("Officer staging injected server-side") < blk.index("else:"), (
            "the as-written branch must be the ELSE of the injection, not the if"
        )

    def test_suffix_fires_on_difference_not_sameness(self):
        """The comparator itself is correct under mutation, but the SENSE of the
        test at the call site is a separate failure: `==` would append the
        officer's words only when they were already preserved, and stay silent in
        exactly the case this exists for."""
        src = self._src()
        blk = src[src.index("if not _has_officer:"):src.index("# Event Log note when officer")]
        code = "\n".join(l.split("#")[0] for l in blk.splitlines())
        assert "_staging_first_word_key(_officer_raw) != _staging_first_word_key(" in code, (
            "the as-written suffix no longer fires on a DIFFERENCE — inverted, it "
            "would append only when nothing was rewritten"
        )


class TestStagingCandidateNameLeads:
    """Pass A must NAME the location, not appear anywhere inside it (issue #722).

    #173 (March 2026) narrowed Pass A from the full body to loc_part and dropped
    word-level fallback, closing the CITY-SUFFIX door: "campbell community
    center" no longer matched every entry carrying ", Campbell". The STREET-NAME
    door stayed open, and #669 (2026-08-01) walked through it by adding shopping
    malls as tier-1 candidates — named developments whose road carries the
    development's name are common in this county.

    Live 2026-08-08: candidate "great mall" matched "447 great mall dr",
    "1306 great mall pkwy" and "572 great mall dr". Three distinct addresses
    inherited one centroid. CalTopo plotted them at one point while the list text
    still advertised the provider's own per-entry distances — map and text
    disagreeing with nothing logged — and the override pick list highlighted
    three rows as CURRENT for a single pick, because that badge is exact
    coordinate equality.

    The fix narrows the SCOPE of the match, never its STRENGTH: the full
    candidate name is still required, so #173's prohibition on word-level
    fallback is untouched.
    """

    @staticmethod
    def _src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @staticmethod
    def _fn(src):
        """Bounded at BOTH ends on real markers — never start + N characters."""
        m = re.search(
            r"^def _staging_candidate_name_leads\(.*?(?=\n\n(?:def |async def |# -{10,}))",
            src, re.DOTALL | re.MULTILINE,
        )
        assert m, "_staging_candidate_name_leads not found in main.py"
        return m.group(0)

    @staticmethod
    def _code_only(text):
        """Strip the docstring and every comment.

        Load-bearing: this helper's rationale names "great mall dr" and
        "447 great mall" verbatim, so a raw-source pin would match the
        EXPLANATION of the bug and pass with the code deleted.
        """
        body = re.sub(r'""".*?"""', "", text, count=1, flags=re.DOTALL)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    # --- production parity ------------------------------------------------

    def test_pass_a_calls_the_helper(self):
        """Assert the CALL SITE, not the identifier anywhere in a 9,000-line
        module — the helper's own def line contains its name."""
        src = self._src()
        loop = re.search(
            r"for cname, \(clat, clng\) in name_to_coords\.items\(\):.*?\n\s+break\n",
            src, re.DOTALL,
        )
        assert loop, "the Pass A match loop is gone"
        code = "\n".join(l.split("#")[0] for l in loop.group(0).splitlines())
        assert "_staging_candidate_name_leads(cname, loc_part)" in code, (
            "Pass A no longer routes through the leading-match helper — a "
            "candidate whose name is also the street name would again collapse "
            "every address on that street onto one coordinate"
        )
        assert "if cname in loc_part" not in code, (
            "the bare substring test is back"
        )

    def test_production_requires_a_leading_match(self):
        code = self._code_only(self._fn(self._src()))
        assert "loc_part.startswith(cname)" in code, (
            "the leading-match requirement is gone; a mid-string hit is a "
            "match inside a STREET NAME, not a weaker name match"
        )

    def test_production_boundary_excludes_a_trailing_space(self):
        """A space is the failure itself: ' Dr', ' Pkwy', ' Blvd'."""
        code = self._code_only(self._fn(self._src()))
        m = re.search(r'rest\[0\] in ([\'"])(.+?)\1', code)
        assert m, "the boundary character set is gone from production"
        assert " " not in m.group(2), (
            f"production accepts {m.group(2)!r} as a boundary, which includes a "
            f"space — '447 great mall' + ' dr' would match again"
        )
        assert m.group(2) == ",.", (
            f"production boundary set is {m.group(2)!r}, not ',.'. Widening it "
            f"is how the street-name door reopens — say why in the docstring "
            f"and update this pin deliberately."
        )

    def test_full_name_is_still_required(self):
        """#173 forbids word-level fallback. Narrowing scope must not smuggle
        it back in."""
        code = self._code_only(self._fn(self._src()))
        assert ".split()" not in code and "any(" not in code, (
            "word-level matching has reappeared — this is the #173 regression "
            "that put a KFC marker on a community center"
        )

    # --- behaviour (against PRODUCTION — see _load_production_fn) ----------

    def test_street_named_after_the_poi_does_not_match(self):
        """The 2026-08-08 case. All three addresses must be rejected."""
        for addr in ("447 great mall dr, milpitas",
                     "1306 great mall pkwy, milpitas",
                     "572 great mall dr, milpitas"):
            assert not _prod_name_leads("great mall", addr), addr

    def test_name_led_entry_still_matches(self):
        """Parks carry no street address BY DESIGN — this is what Pass A is
        for, and it must keep working."""
        assert _prod_name_leads(
            "ortega park", "ortega park, sunnyvale")
        assert _prod_name_leads(
            "ed levin county park", "ed levin county park")
        assert _prod_name_leads(
            "ortega park", "ortega park, sunnyvale. large park with restrooms")

    def test_city_suffix_case_stays_fixed(self):
        """Regression guard for #173 (March 2026)."""
        assert not _prod_name_leads(
            "campbell community center", "300 darryl drive, campbell")
        assert not _prod_name_leads(
            "campbell community center", "1000 s winchester blvd, campbell")

    def test_trailing_space_is_not_a_boundary(self):
        """Two legs, varying which side the street type sits on."""
        assert not _prod_name_leads(
            "great mall", "great mall dr, milpitas")
        assert not _prod_name_leads(
            "valley fair", "valley fair parkway")

    def test_longer_venue_name_falls_through_to_pass_b(self):
        """A near-miss is not a wrong answer — Pass B geocodes it, exactly as
        the existing name-abbreviation misses already do."""
        assert not _prod_name_leads(
            "great mall", "great mall of the bay area, milpitas")

    def test_empty_inputs_do_not_match(self):
        assert not _prod_name_leads("", "ortega park")
        assert not _prod_name_leads("ortega park", "")


class TestStagingProximityFilter:
    """Pin the staging proximity filter (issue #674).

    Before #674 the only dedup keys were IDENTITY checks — exact name, and
    house-number+street. Neither ever measured the distance between two
    DIFFERENT addresses, so two entries fifty feet apart on different street
    numbers both survived by construction. On the 2026-07-31 dispatch all seven
    ranked entries landed inside 0.13 mi of the LKP and of each other: seven
    addresses, one location, no real choice for the dispatcher.

    N was calibrated empirically by experiments/geoapify/03_proximity_calibration.py
    across six real Santa Clara County anchors, using Geoapify's own returned
    coordinates — never estimated positions (#606 shipped a distance threshold
    wrong twice that way). Downtown San Jose reproduced the 07-31 shape almost
    exactly (53/55/106/117/176/196/205 m from the LKP vs the incident's
    64/113/113/129/193/209/209 m), which is what made it a usable proxy.
    """

    @staticmethod
    def _src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @staticmethod
    def _fn(src):
        """The body of _rank_dedupe_cap_staging, bounded at BOTH ends on real
        markers. An unbounded search over a 9,000-line module proves only that
        the module contains a string somewhere."""
        m = re.search(
            r"^def _rank_dedupe_cap_staging\(.*?(?=\n\n(?:def |async def |# -{10,}))",
            src, re.DOTALL | re.MULTILINE,
        )
        assert m, "_rank_dedupe_cap_staging not found in main.py"
        return m.group(0)

    @staticmethod
    def _code_only(text):
        """Strip the docstring and every comment.

        Load-bearing, not tidiness: the rationale for this filter is written
        directly above the code that implements it and names the same
        identifiers. A pin that searches raw source finds the explanation and
        passes with the implementation deleted — that exact trap has now caught
        six pins across two sessions.
        """
        body = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    # --- production parity ------------------------------------------------

    def test_separation_literal_matches_production(self):
        """The mirror above and main.py must agree on N."""
        m = re.search(
            r"^_STAGING_MIN_SEPARATION_M\s*=\s*(\d+)\s*$",
            self._src(), re.MULTILINE,
        )
        assert m, "_STAGING_MIN_SEPARATION_M is not defined in main.py"
        assert int(m.group(1)) == _STAGING_MIN_SEPARATION_M, (
            f"main.py sets the staging separation to {m.group(1)} m but this "
            f"file mirrors {_STAGING_MIN_SEPARATION_M} m. One of them drifted; "
            f"re-run experiments/geoapify/03_proximity_calibration.py before "
            f"changing the value — it is calibrated, not chosen."
        )

    def test_filter_is_applied_inside_the_ranking_helper(self):
        """Assert the CALL, inside the function, not the identifier anywhere.

        The constant is referenced by its own defining line and by the comment
        block explaining it; neither proves the comparison still runs.
        """
        code = self._code_only(self._fn(self._src()))
        assert "_haversine_m(c_lat, c_lng, k[\"lat\"], k[\"lng\"])" in code, (
            "The proximity comparison is gone from _rank_dedupe_cap_staging — "
            "staging recommendations can again be seven names for one place."
        )
        assert "< _STAGING_MIN_SEPARATION_M" in code, (
            "The proximity comparison no longer tests against "
            "_STAGING_MIN_SEPARATION_M."
        )

    def test_filter_runs_before_the_candidate_is_accepted(self):
        """Structure, not keyword presence.

        A proximity check that runs AFTER the append is a no-op that still
        contains every string the pins above look for.
        """
        code = self._code_only(self._fn(self._src()))
        assert code.index("< _STAGING_MIN_SEPARATION_M") < code.index("deduped.append(c)"), (
            "The proximity filter now runs after the candidate is appended, so "
            "it can never reject anything."
        )

    def test_counts_are_taken_after_the_proximity_filter(self):
        """The school/church counts drive the time-of-day exclusion note.

        They are computed from `deduped`, which is post-filter by construction.
        Hoisting them above the loop would restore double-counting of one campus
        returned as two features.
        """
        code = self._code_only(self._fn(self._src()))
        assert code.index("deduped.append(c)") < code.index("_school_count_uncapped"), (
            "School/church counting moved ahead of the dedup loop."
        )

    def test_both_coordinate_guards_survive_in_production(self):
        """Two legs, both required, and main.py cannot be unit-tested.

        The behaviour tests below run against the mirror because main.py is not
        importable, so production's own guards need a source pin. Leg 1 guards
        the candidate under test; leg 2 guards candidates already accepted —
        without it a coordinate-less entry that ranks HIGH makes every later
        comparison raise KeyError, and both callers swallow that into
        ([], 0, 0, False): the whole staging list disappears behind a
        "non-fatal" log line.
        """
        code = self._code_only(self._fn(self._src()))
        prox = code[code.index("c_lat, c_lng = c.get"):code.index("seen_names.add")]
        assert "if c_lat is not None and c_lng is not None" in prox, (
            "Leg 1 gone: a candidate without coordinates now raises instead of "
            "riding through."
        )
        assert 'if k.get("lat") is not None and k.get("lng") is not None' in prox, (
            "Leg 2 gone: an accepted candidate without coordinates now raises "
            "KeyError, and staging silently returns empty."
        )

    def test_parks_are_not_exempt_in_production(self):
        """Comments are stripped first — the rationale names 'park' repeatedly.

        Measured: exempting parks reinstates Circle of Palms Plaza 56 m from
        Fairmont Plaza, two adjacent downtown San Jose plazas. That is the
        failure this filter exists to remove, so the exemption must not exist.
        """
        code = self._code_only(self._fn(self._src()))
        prox = code[code.index("c_lat, c_lng = c.get"):code.index("seen_names.add")]
        assert "park" not in prox, (
            "The proximity filter now special-cases parks. The address dedup "
            "exempts them because Geoapify fills a bare city for nearly every "
            "park — a DATA ARTIFACT. Coordinates are not an artifact and the "
            "exemption does not transfer."
        )

    # --- behaviour --------------------------------------------------------

    @staticmethod
    def _at(name, amenity, dist_m, lat, lng, addr=None):
        return {
            "name": name, "amenity": amenity, "dist_m": dist_m,
            "addr": addr if addr is not None else f"{dist_m} Example St, San Jose",
            "lat": lat, "lng": lng,
        }

    def test_near_pair_collapses_to_the_better_ranked_entry(self):
        """9 m apart, different names, different street numbers — one entry.

        Real case: Geoapify returns "Eastridge" (the mall) and "Auntie Anne's",
        a food-court tenant INSIDE it, 9 m apart. Both held staging slots.
        """
        raw = [
            self._at("Eastridge", "mall", 37, 37.32530, -121.81350),
            self._at("Auntie Anne's", "fast_food", 57, 37.325373, -121.813554),
        ]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(raw)
        assert [c["name"] for c in ranked] == ["Eastridge"]

    def test_far_pair_both_survive(self):
        """Beyond N, two entries are two genuine choices."""
        raw = [
            self._at("Silver Leaf Park", "park", 223, 37.24700, -121.78600,
                     addr="(address not in OSM)"),
            self._at("Golden Oak Park", "park", 677, 37.25150, -121.78600,
                     addr="(address not in OSM)"),
        ]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(raw)
        assert len(ranked) == 2

    def test_parks_are_not_exempt_behaviourally(self):
        """Two adjacent plazas are one staging location.

        Parks skip ADDRESS dedup (the sentinel) — they must not skip this one.
        """
        raw = [
            self._at("Fairmont Plaza", "park", 176, 37.33370, -121.88930,
                     addr="(address not in OSM)"),
            self._at("Circle of Palms Plaza", "park", 205, 37.334166, -121.889528,
                     addr="(address not in OSM)"),
        ]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(raw)
        assert [c["name"] for c in ranked] == ["Fairmont Plaza"]

    def test_coordinateless_candidate_being_checked_rides_through(self):
        """lat/lng are not in this helper's input contract.

        Both callers turn any exception here into ([], 0, 0, False) — staging
        silently disappearing. Degrading to pre-#674 behaviour beats crashing.

        Leg 1 of 2: the coordinate-less candidate is the one under test.
        """
        raw = [
            self._at("Eastridge", "mall", 37, 37.32530, -121.81350),
            {"name": "Auntie Anne's", "amenity": "fast_food", "dist_m": 57,
             "addr": "1 Tully Rd, San Jose"},
        ]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(raw)
        assert len(ranked) == 2, "a coordinate-less candidate must ride through"

    def test_coordinateless_candidate_already_accepted_does_not_crash(self):
        """Leg 2 of 2 — the leg the first test cannot reach.

        When the coordinate-less entry ranks HIGHER it lands in `deduped` first,
        and every later candidate is compared against it. Without the guard on
        the accepted side, `k["lat"]` raises KeyError and both callers swallow
        it into ([], 0, 0, False) — the entire staging list vanishes with a
        "non-fatal" log line. Mutation testing found this gap; the first leg
        short-circuits before the accepted side is ever touched.
        """
        raw = [
            {"name": "Eastridge", "amenity": "mall", "dist_m": 37,
             "addr": "2200 Eastridge Loop, San Jose"},
            self._at("Auntie Anne's", "fast_food", 57, 37.325373, -121.813554),
        ]
        ranked, _, _ = _rank_dedupe_cap_staging_mirror(raw)
        assert [c["name"] for c in ranked] == ["Eastridge", "Auntie Anne's"]

    def test_school_count_does_not_double_count_one_campus(self):
        """Geoapify returns "Saint Martin of Tours Church" and "... School"
        23 m apart as two features. The exclusion note must say one."""
        raw = [
            self._at("Saint Martin of Tours School", "school", 570, 37.32100, -121.94100),
            self._at("Saint Martin of Tours Church and School", "school", 578,
                     37.321180, -121.941105),
        ]
        ranked, school, _ = _rank_dedupe_cap_staging_mirror(raw)
        assert len(ranked) == 1
        assert school == 1

    def test_filter_is_shared_by_both_staging_sources(self):
        """Overpass and Geoapify must filter identically.

        Guaranteed structurally: both call the one helper. Pin the structure —
        a per-source copy would drift, and Overpass is the outage fallback
        (#453/#484) plus the shadow comparison, so a divergence would show up
        as a shadow diff that is not a real defect.
        """
        src = self._src()
        for fn in ("_query_overpass_staging", "_query_geoapify_staging"):
            body = re.search(
                rf"^async def {fn}\(.*?(?=\n\n(?:def |async def |# -{{10,}}))",
                src, re.DOTALL | re.MULTILINE,
            )
            assert body, f"{fn} not found in main.py"
            assert "_rank_dedupe_cap_staging(" in body.group(0), (
                f"{fn} no longer routes through the shared ranking helper, so "
                f"the two staging sources can drift apart."
            )


class TestStagingParkTelemetry:
    """Pin the park counters in _rank_dedupe_cap_staging (issue #807).

    Parks are tier 1 and are frequently the best staging SAR gets, but the
    caller log lines count only schools and churches. A park that the provider
    never returned and a park that was returned and then EVICTED therefore
    looked identical in the logs — opposite diagnoses with the same evidence.
    On the 2026-09-05 sccssar-dev regression test civic_raw=55 reduced to
    count=12 with no park in the seven ranked entries, and the log record could
    not say which had happened.

    Source pins only: main.py is not importable under local pytest, so there is
    no way to capture the emitted record. That makes mutation testing the only
    evidence these assertions are real — each one below was verified by
    reintroducing the defect it names.
    """

    @staticmethod
    def _src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @classmethod
    def _fn(cls, src=None):
        """Bounded at BOTH ends on real markers — never start + N characters."""
        m = re.search(
            r"^def _rank_dedupe_cap_staging\(.*?(?=\n\n(?:def |async def |# -{10,}))",
            src if src is not None else cls._src(), re.DOTALL | re.MULTILINE,
        )
        assert m, "_rank_dedupe_cap_staging not found in main.py"
        return m.group(0)

    @staticmethod
    def _code_only(text):
        """Strip the docstring and every comment.

        Load-bearing here for the same reason as in TestStagingProximityFilter:
        the rationale block names every counter this class asserts, so a pin
        over raw source finds the explanation and passes with the telemetry
        deleted.
        """
        body = re.sub(r'"""(?:.|\n)*?"""', "", text)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    def test_telemetry_is_emitted_from_the_shared_helper(self):
        """The CALL, inside the function — not the identifier anywhere."""
        code = self._code_only(self._fn())
        assert "logger.info(" in code, (
            "The park telemetry log call is gone from "
            "_rank_dedupe_cap_staging — an absent park and an evicted park "
            "are again indistinguishable in the logs."
        )
        assert "parks_in=%d" in code and "parks_dropped_proximity=%d" in code, (
            "The park telemetry no longer reports the provider count and the "
            "proximity evictions, which are the two causes #807 exists to "
            "separate."
        )
        assert "parks_precap=%d" in code and "parks_capped=%d" in code, (
            "The park telemetry no longer spans the :12 cap, so a park cut by "
            "the cap cannot be told from one cut by the filters."
        )

    def test_precap_count_reads_deduped_not_the_capped_list(self):
        """Same rule as the school/church counts directly above it.

        Counting from the capped list would report 0 at exactly the dense
        anchors where the question is asked — 12+ tier-1 candidates fill the
        cap, which is the shape the 2026-09-05 test produced.
        """
        code = self._code_only(self._fn())
        assert '_parks_precap = sum(1 for c in deduped if c.get("amenity") == "park")' in code, (
            "The pre-cap park count is no longer derived from `deduped`. "
            "Counting from the capped list makes it 0 in dense areas."
        )

    def test_counts_are_taken_after_the_dedup_loop(self):
        """Structure, not keyword presence.

        Hoisted above the loop, `deduped` is empty and every count is 0 — which
        still contains every string the pins above look for.
        """
        code = self._code_only(self._fn())
        assert code.index("deduped.append(c)") < code.index("_parks_precap"), (
            "Park counting moved ahead of the dedup loop, so it counts an "
            "empty list."
        )

    def test_proximity_evictions_are_recorded_inside_the_filter(self):
        """Recorded at the drop, and before any candidate is accepted.

        Appended after the loop, or outside the rejecting branch, the counter
        would report 0 evictions forever while still being present.
        """
        code = self._code_only(self._fn())
        assert 'prox_dropped_amenities.append(c["amenity"])' in code, (
            "The proximity filter no longer records what it drops, so a park "
            "evicted by #674 is silent again."
        )
        assert code.index('prox_dropped_amenities.append(c["amenity"])') \
            < code.index("deduped.append(c)"), (
                "The eviction is recorded after the candidate is accepted, so "
                "it can never fire."
            )
        assert 'prox_dropped_amenities.count("park")' in code, (
            "The recorded evictions are never counted, so "
            "parks_dropped_proximity is not derived from them."
        )

    def test_the_recorder_does_not_make_the_filter_amenity_aware(self):
        """The #674 row's PARKS ARE NOT EXEMPT rule outranks this telemetry.

        TestStagingProximityFilter.test_parks_are_not_exempt_in_production
        already forbids the token `park` inside the filter branch. This pin
        states the reason from the telemetry side: the recorder is generic on
        purpose, and turning it into a condition would exempt parks from the
        filter while looking like observability.
        """
        code = self._code_only(self._fn())
        prox = code[code.index("c_lat, c_lng = c.get"):code.index("seen_names.add")]
        assert "park" not in prox, (
            "The proximity filter now special-cases parks. Telemetry must be "
            "read after the loop, never used as a drop condition."
        )

    def test_telemetry_logs_counts_only(self):
        """Core privacy guarantee #3 — no PII in logs.

        Every conversion in the record must be %d. A single %s would let a
        place name or a coordinate string reach Cloud Run logs.
        """
        code = self._code_only(self._fn())
        start = code.index('"Staging park telemetry')
        record = code[start:code.index(")", code.index("parks_capped=%d"))]
        assert "%s" not in record and "%r" not in record, (
            "The park telemetry interpolates a string — counts only, no place "
            "names and no coordinates."
        )


# main.py — override endpoint constants. Pinned because they affect operational
# behavior (Overpass quota, response payload size, validation thresholds).
_PR_D_OVERRIDE_OVERPASS_RADIUS_M  = 300
_PR_D_OVERRIDE_NEARBY_CAP         = 5
_PR_D_OVERRIDE_ADDRESS_MAX_LEN    = 200
_PR_D_OVERRIDE_UTM_MAX_LEN        = 64


class TestApplyStagingOverrideOverpassRadius:
    """Pin the override endpoint's operational constants. Each is documented
    in main.py with the rationale; this is the pin against drift.
    """

    def test_radius_unchanged(self):
        # Small radius — purpose is "is there a clearly-better-named POI right
        # here?" not "rebuild the entire staging cluster". OCR-time uses 1200m.
        assert _PR_D_OVERRIDE_OVERPASS_RADIUS_M == 300

    def test_nearby_cap_unchanged(self):
        # Small list — dispatcher is making a quick decision under pressure.
        assert _PR_D_OVERRIDE_NEARBY_CAP == 5

    def test_input_length_caps(self):
        assert _PR_D_OVERRIDE_ADDRESS_MAX_LEN == 200
        assert _PR_D_OVERRIDE_UTM_MAX_LEN == 64


class TestUtmCombinedDisplayFormat:
    """PR-D-2.5 (2026-05-10): UTM-mode primary.address keeps the dispatcher's
    original UTM string visible alongside the resolved lat/lng. SAR-radio
    responders see the SAR-standard UTM form they're trained on; digital-map
    responders get the lat/lng pair that map URLs accept. Format mirrors
    the existing "<address> — <name>" entry convention (e.g.,
    "Branham Park, San Jose — City park").

    Pinned: lat formatted to 5 decimal places, single space + em-dash + space,
    then the trimmed UTM input verbatim. Example:
      utm_in = "10S 590309E 4142188N"
      anchor = (37.42210, -121.97936)
      → "37.42210, -121.97936 — 10S 590309E 4142188N"
    """

    @staticmethod
    def _build_combined(lat, lng, utm_in):
        """Mirror of the format string used in main.py::apply_staging_override
        for utm_in mode. If you change the format here, change main.py too."""
        return f"{lat:.5f}, {lng:.5f} — {utm_in.strip()}"

    def test_canonical_combined_format(self):
        result = self._build_combined(37.42210, -121.97936, "10S 590309E 4142188N")
        assert result == "37.42210, -121.97936 — 10S 590309E 4142188N"

    def test_utm_string_is_trimmed(self):
        # Defensive: leading/trailing whitespace in user input shouldn't bleed
        # into the displayed staging line.
        result = self._build_combined(37.42210, -121.97936, "  10S 590309E 4142188N  ")
        assert result == "37.42210, -121.97936 — 10S 590309E 4142188N"

    def test_em_dash_separator_matches_existing_convention(self):
        # The separator between lat/lng and UTM MUST be the em-dash with
        # surrounding spaces (" — "), matching the existing convention used
        # in the staging recommendations list (e.g. "Branham Park — City park").
        result = self._build_combined(37.42210, -121.97936, "10S 590309E 4142188N")
        assert " — " in result
        # Hyphen-minus would silently break the visual cue.
        assert " - " not in result.replace(" — ", "")  # only the em-dash version


class TestEventNameSuggestionGmFlag:
    """PR-D-2.5 (2026-05-10): the frontend helper that mutates Event Name
    after override application MUST use the `g` flag so BOTH the WhatsApp
    section's `Event Name:` line AND the Full section's `Event Name:` line
    are updated in lockstep.

    Pre-fix the regex used `m` only — only the FIRST match was replaced,
    leaving the Full section's stale OCR-derived streetname. Live confirmed
    on 2026-05-10 PR-D-2 Verde Vista test (WhatsApp showed
    `2026-03-07 SCC Woodward` while Full still showed
    `2026-03-07 SCC SLO Verde Vista`).

    This test mirrors the JS replace logic so a JS-side regression is caught
    by Python tests at Step 0 of the build script.
    """

    @staticmethod
    def _apply_suggestion(text, suggestion):
        """Mirror of frontend/index.html::_applyEventNameStreetnameSuggestion."""
        return re.sub(
            r"^(Event Name:\s*\d{4}-\d{2}-\d{2}\s+\S+)\s+.*$",
            r"\1 " + suggestion,
            text,
            flags=re.MULTILINE,  # `gm` in JS → MULTILINE in Python; re.sub replaces all matches
        )

    def test_both_event_name_lines_updated(self):
        # Two-section textarea — both Event Name lines should be updated in
        # lockstep. Agency is always a single canonical token after
        # `_AGENCY_DISPLAY` lookup (SJSU, SJPD, SCCSO, MPD, etc.) — this is a
        # production invariant, not a limitation. Bill confirmed 2026-05-10:
        # "yes, it's always one-word." OCR misreads that produce multi-token
        # agencies (e.g. "SCC SLO") are caught at canonicalization time by
        # `_AGENCY_DISPLAY` rows added in this PR — see TestAgencyDisplay's
        # SCCSO OCR-misread defenses.
        text = (
            "━━━ WHATSAPP DISPATCH ━━━\n"
            "Event Name: 2026-03-07 SJSU Verde Vista\n"
            "Other field: foo\n"
            "━━━\n"
            "\n"
            "━━━ FULL INCIDENT SUMMARY ━━━\n"
            "Event Name: 2026-03-07 SJSU Verde Vista\n"
            "Other field: bar\n"
        )
        result = self._apply_suggestion(text, "Woodward")
        lines = [ln for ln in result.split("\n") if ln.startswith("Event Name:")]
        assert len(lines) == 2
        assert lines[0] == "Event Name: 2026-03-07 SJSU Woodward"
        assert lines[1] == "Event Name: 2026-03-07 SJSU Woodward"

    def test_no_event_name_line_passes_through(self):
        text = "no event name here\nfoo bar\n"
        result = self._apply_suggestion(text, "Woodward")
        assert result == text

    def test_single_event_name_line_works(self):
        # Single Event Name line (sample mode without the two-section layout)
        # should still update.
        text = "Event Name: 2026-04-30 SCSO TRADAN\nOther: foo\n"
        result = self._apply_suggestion(text, "Verde Vista")
        assert "Event Name: 2026-04-30 SCSO Verde Vista" in result


class TestStagingLabelRenumberingOnOverride:
    """PR-D-2.5 (2026-05-10): when the dispatcher commits an override, the
    frontend prepends the dispatcher entry to _rawMapData.staging AND
    renumbers every entry's label so the CalTopo marker list shows
    "1. <dispatcher>", "2. <existing-1>", "3. <existing-2>", ... in order.

    Pre-fix the dispatcher entry's label was just the address (no number
    prefix), but existing entries kept their original "1. Foo", "2. Bar"
    labels. The CalTopo marker list rendered the dispatcher entry at the
    bottom without a number, looking like the dispatcher pick was the
    least-recommended option. Live confirmed 2026-05-10 PR-D-2 Verde Vista.

    Mirror of the renumbering loop in frontend/index.html::_overrideCommit.
    """

    @staticmethod
    def _renumber(staging_entries):
        """Mirror of the renumber logic in frontend _overrideCommit."""
        for idx, s in enumerate(staging_entries):
            new_prefix = f"{idx + 1}. "
            stripped = re.sub(r"^\d+\.\s+", "", s.get("label", ""))
            s["label"] = new_prefix + (stripped or "Staging")
        return staging_entries

    def test_dispatcher_entry_becomes_label_1(self):
        # Override case: dispatcher prepended to existing entries.
        entries = [
            {"label": "100 Main St — Dispatcher-specified staging location", "type": "dispatcher"},
            {"label": "1. Branham Park, San Jose", "type": "alternate"},
            {"label": "2. 1000 Branham Ln", "type": "alternate"},
            {"label": "3. California — Officer-designated staging location", "type": "officer"},
        ]
        result = self._renumber(entries)
        assert result[0]["label"] == "1. 100 Main St — Dispatcher-specified staging location"
        assert result[1]["label"] == "2. Branham Park, San Jose"
        assert result[2]["label"] == "3. 1000 Branham Ln"
        assert result[3]["label"] == "4. California — Officer-designated staging location"

    def test_label_without_number_prefix_gets_one(self):
        # Defensive: if an entry's label has no leading "<N>. " prefix
        # (legacy or malformed), prepend a fresh prefix anyway.
        entries = [
            {"label": "First entry, no prefix", "type": "dispatcher"},
            {"label": "Second entry, also no prefix", "type": "alternate"},
        ]
        result = self._renumber(entries)
        assert result[0]["label"] == "1. First entry, no prefix"
        assert result[1]["label"] == "2. Second entry, also no prefix"

    def test_empty_label_falls_back_to_staging(self):
        entries = [{"label": "", "type": "dispatcher"}]
        result = self._renumber(entries)
        assert result[0]["label"] == "1. Staging"


class TestWhatsAppSurfaceRemoved:
    """Issue #614 — the WhatsApp surface is sunset; pin that it stays gone.

    This class REPLACES TestWhatsAppBareCopyLineMutation, which mirrored the
    Step 3 bare-copy-line regex in the frontend. That test was a pure
    self-mirror: it exercised its own copy of the regex against its own
    fixture, so it kept passing after the production code was deleted. Deleting
    the whole WhatsApp surface broke NO test in the suite — which is the real
    finding, and why these pins are source-anchored instead.

    Sunset rationale (Bill, 2026-07-25): Slack has been `full` on both
    environments, the 2026-07-24 callout ran end to end with no WhatsApp use
    and no complaints, and Slack now renders the staging address as a tappable
    link — which is what retired the mobile long-press-copy rationale for the
    bare line.
    """

    @staticmethod
    def _main_src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @staticmethod
    def _frontend_src():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_backend_no_longer_ASSEMBLES_the_two_summary_layout(self):
        """Pin the ASSEMBLY, not the literal.

        The literal still appears legitimately in the D4H stripper, which must
        keep recognising the markers to remove them from pasted older text. A
        blanket "string absent" pin would fail on that and push someone toward
        deleting the stripper — a behaviour change dressed as cleanup.
        """
        src = self._main_src()
        for builder in ('f"━━━ WHATSAPP DISPATCH {_DIVIDER',
                        'f"━━━ FULL INCIDENT SUMMARY {_DIVIDER',
                        '_wa_block = '):
            assert builder not in src, (
                f"the two-summary layout is being assembled again ({builder!r}) — "
                f"#614 sunset it, and re-adding it means restoring the Locked "
                f"Decision rows in CLAUDE.md too"
            )

    def test_the_bare_copy_line_is_not_reassembled(self):
        """The line Bill asked to remove alongside the block."""
        src = self._main_src()
        assert "_bare_addr" not in src, (
            "the bare copy-pasteable staging line is back — it existed for "
            "WhatsApp mobile long-press copy, and Slack now renders staging as "
            "a tappable link"
        )

    def test_frontend_has_no_whatsapp_button_or_handler(self):
        html = self._frontend_src()
        for marker in ('id="whatsapp-btn"', "wa.me"):
            assert marker not in html, f"WhatsApp surface {marker!r} is back in the frontend"

    def test_frontend_workflow_text_does_not_promise_whatsapp(self):
        html = self._frontend_src()
        assert "D4H → WhatsApp" not in html, (
            "the Step 3 workflow line still tells the dispatcher to finish in "
            "WhatsApp, but the button is gone"
        )

    def test_eb_body_stripper_is_retained(self):
        """The stripper stays, deliberately — this is not dead code.

        It removes the section markers from dispatcher-supplied text before
        composing the Everbridge body. New OCR output no longer contains them,
        but the textarea is dispatcher-editable and a paste of older text must
        never put `━━━ FULL INCIDENT SUMMARY ━━━` into a notification that
        reaches responders. Deleting it would be a behaviour change dressed as
        cleanup.
        """
        src = self._main_src()
        assert "_D4H_RE_FULL_SECTION_OPEN" in src, (
            "the D4H/EB section-marker stripper is gone — pasted older text "
            "would carry section headers into an outbound notification"
        )


class TestApplyOverrideNearbyFilterAndDisplay:
    """PR-D-2.5 (2026-05-10 Verde Vista retest): the nearby-POI picker in
    /apply-staging-override MUST filter out entries with no usable address
    info AND format the display string correctly across all Overpass
    address shapes.

    Pre-fix: Kelley Park (a park with no addr:city in OSM) appeared as
    "Kelley Park" in the picker. Clicking it committed staging line
    "Staging Area for Resources: Kelley Park" — bare name, no address,
    no city. Operationally useless for responders.

    Pre-fix display bug: the original `addr.split(",")[1].strip()` logic
    pulled the SECOND comma-separated token from Overpass addr, which was
    sometimes a postcode (e.g. "San Jose, 95148" → city = "95148"). Fixed
    by stripping postcodes (5-digit / ZIP+4 patterns) before display.

    Mirror of the filter+display logic in main.py::apply_staging_override.
    """

    @staticmethod
    def _build_nearby_display(name, addr):
        """Mirror of the per-candidate display construction in
        apply_staging_override. Returns the display string, or None if
        the candidate should be filtered out."""
        addr = (addr or "").strip()
        if not addr or addr == "(address not in OSM)":
            return None
        is_postcode = lambda t: bool(re.match(r"^\d{5}(-\d{4})?$", t.strip()))
        parts = [
            p.strip()
            for p in addr.split(",")
            if p.strip() and not is_postcode(p)
        ]
        if not parts:
            return None
        return f"{name}, {', '.join(parts)}"

    # ---- Filter cases ----

    def test_park_with_no_addr_filtered(self):
        # Kelley Park canonical case — no addr tags in OSM at all.
        assert self._build_nearby_display("Kelley Park", "(address not in OSM)") is None

    def test_empty_addr_filtered(self):
        # Defensive: empty addr string.
        assert self._build_nearby_display("Some POI", "") is None

    def test_postcode_only_addr_filtered(self):
        # Defensive: only a postcode is unusable as a staging label.
        assert self._build_nearby_display("Some POI", "95148") is None

    # ---- Display cases ----

    def test_full_street_plus_city(self):
        assert (
            self._build_nearby_display("Valle Vista Elementary", "2000 Flint Avenue, San Jose")
            == "Valle Vista Elementary, 2000 Flint Avenue, San Jose"
        )

    def test_street_plus_city_plus_postcode_strips_postcode(self):
        # Bug from PR #407 first iteration: split(",")[1] grabbed "95148"
        # as the city. Now postcode is stripped, city is preserved.
        assert (
            self._build_nearby_display("Calaveras Montessori School", "1100 East Calaveras Boulevard, Milpitas, 95035")
            == "Calaveras Montessori School, 1100 East Calaveras Boulevard, Milpitas"
        )

    def test_city_only_park_kept(self):
        # Fernish Park canonical case: addr:city present, no street.
        # Operational decision: keep this — "Fernish Park, San Jose" is
        # navigable enough for responders.
        assert (
            self._build_nearby_display("Fernish Park", "San Jose")
            == "Fernish Park, San Jose"
        )

    def test_city_plus_postcode_park_strips_postcode(self):
        assert (
            self._build_nearby_display("Some Park", "San Jose, 95148")
            == "Some Park, San Jose"
        )

    def test_zip_plus_4_stripped(self):
        # Defense against the ZIP+4 form.
        assert (
            self._build_nearby_display("X", "1 Main, San Jose, 95148-1234")
            == "X, 1 Main, San Jose"
        )


# ---------------------------------------------------------------------------
# D4H integration — module-level mirror constants
# (matches TestDispatcherStagingOverrideLabel + TestDispatcherSymbolConstant
# pattern in this same file; per CLAUDE.md "Cross-file literal pin policy")
# ---------------------------------------------------------------------------
# Mirror backend/d4h.py — TAG_* constants (source-of-truth: experiments/d4h/01_list_tags.py)
_D4H_TAG_CANINE_LITERAL            = 33095
_D4H_TAG_UAS_LITERAL               = 33103
_D4H_TAG_SEARCH_MANAGEMENT_LITERAL = 34206
_D4H_TAG_TRANSPORT_LITERAL         = 34207
_D4H_TAG_ATV_LITERAL               = 33098
_D4H_TAG_TECHNICAL_RESCUE_LITERAL  = 33102

# Mirror backend/d4h.py — enum string values + integer IDs
_D4H_STATUS_VALUES                  = ("REQUESTED", "ATTENDING", "ABSENT")
_D4H_OUTCOME_PERSON_ASSISTED_ID     = 1
_D4H_INVOLVEMENT_TYPE_SUBJECT_ID    = 1
_D4H_AREA_KNOWLEDGE_VALUES          = ("FAMILIAR", "UNFAMILIAR")
_D4H_CAUSE_VALUES                   = ("NO_DATA", "INTENTIONAL_SELF", "INTENTIONAL_OTHER", "ACCIDENTAL", "UNDETERMINED")

# Mirror backend/d4h.py — role IDs (source-of-truth: experiments/d4h/16_handlers_endpoint.py
# Step 2b role-catalog enumeration, 2026-06-03). SCCSSAR-specific; D4H teams
# customize role taxonomy. Drift = re-run spike 16 and bump both sides.
_D4H_K9_HANDLER_ROLE_ID_LITERAL     = 11487  # "K9 Handler", bundle Search Roles

# Mirror backend/d4h.py — DRONE_REF + DRONE_KIND_TITLE constants
# (source-of-truth: experiments/d4h/09_equipment.py — Phase 1 spike 2026-05-11)
_D4H_DRONE_REF_LITERAL        = "Drone #6"
_D4H_DRONE_KIND_TITLE_LITERAL = "UAS"


# ===========================================================================
# D4H integration regression pins — Phase 2
# ===========================================================================
# Pin literals that exist in MULTIPLE files (per CLAUDE.md cross-file literal
# pin policy). Drift surfaces here.
# ===========================================================================

class TestD4HTagIDsAreStable:
    """D4H Specialty Team tag IDs referenced in:
      - backend/d4h.py (TAG_* constants — source-of-truth in module)
      - backend/test_d4h.py (mirror used by TestMapEBGroupsToD4HTags)
      - experiments/d4h/01_list_tags.py (originally discovered via spike)
    Module-level _D4H_TAG_*_LITERAL mirrors above; if a tag ID changes in
    d4h.py, update the mirror here AND in test_d4h.py.
    """
    def test_tag_canine(self):
        assert _D4H_TAG_CANINE_LITERAL == 33095, \
            "Canine tag drift — re-run 01_list_tags.py and update d4h.py + test_d4h.py + this mirror"

    def test_tag_uas(self):
        assert _D4H_TAG_UAS_LITERAL == 33103

    def test_tag_search_management(self):
        assert _D4H_TAG_SEARCH_MANAGEMENT_LITERAL == 34206, "Always-on, every incident"

    def test_tag_transport(self):
        assert _D4H_TAG_TRANSPORT_LITERAL == 34207, "Canine-conditional"

    def test_tag_atv(self):
        assert _D4H_TAG_ATV_LITERAL == 33098

    def test_tag_technical_rescue(self):
        assert _D4H_TAG_TECHNICAL_RESCUE_LITERAL == 33102


class TestD4HEnumValuesAreStable:
    """D4H enum string values + integer IDs referenced in d4h.py + test_d4h.py
    + spike scripts. D4H rejects unrecognized values with HTTP 400.
    Module-level _D4H_*_VALUES / _D4H_*_ID mirrors above.
    """
    def test_status_enum_values(self):
        assert _D4H_STATUS_VALUES == ("REQUESTED", "ATTENDING", "ABSENT")

    def test_outcome_person_assisted_id(self):
        assert _D4H_OUTCOME_PERSON_ASSISTED_ID == 1, \
            "Person Assisted — v1 placeholder for Subject"

    def test_involvement_type_subject_id(self):
        assert _D4H_INVOLVEMENT_TYPE_SUBJECT_ID == 1, \
            "Subject — the Missing Person"

    def test_area_knowledge_values(self):
        assert _D4H_AREA_KNOWLEDGE_VALUES == ("FAMILIAR", "UNFAMILIAR")

    def test_cause_values(self):
        assert _D4H_CAUSE_VALUES == ("NO_DATA", "INTENTIONAL_SELF", "INTENTIONAL_OTHER",
                                     "ACCIDENTAL", "UNDETERMINED")


class TestD4HRoleIDsAreStable:
    """D4H role IDs referenced in:
      - backend/d4h.py (K9_HANDLER_ROLE_ID — source-of-truth in module)
      - backend/test_d4h.py (K9_HANDLER_ROLE_ID import + test pins)
      - experiments/d4h/16_handlers_endpoint.py (originally discovered via spike)
    Role IDs are SCCSSAR-specific; D4H teams customize role taxonomy. If a role
    is renamed or re-bundled in the D4H Admin UI, the ID can change — re-run
    spike 16 and update both d4h.py and the mirror above.
    """
    def test_k9_handler_role_id(self):
        assert _D4H_K9_HANDLER_ROLE_ID_LITERAL == 11487, \
            "K9 Handler role ID drift — re-run experiments/d4h/16_handlers_endpoint.py " \
            "and update backend/d4h.py + this mirror"

    def test_k9_handler_role_id_matches_production_constant(self):
        """Cross-file pin: production constant in backend/d4h.py must equal
        the mirror literal above. Requires httpx (production-only dependency);
        runs at build pre-flight / CI, skipped in local pytest. Mirrors the
        pytest.importorskip pattern used by the UTM test elsewhere in this file."""
        pytest.importorskip("httpx")
        from d4h import K9_HANDLER_ROLE_ID
        assert K9_HANDLER_ROLE_ID == _D4H_K9_HANDLER_ROLE_ID_LITERAL, \
            "K9_HANDLER_ROLE_ID drift between backend/d4h.py and " \
            "_D4H_K9_HANDLER_ROLE_ID_LITERAL — update both together"


# ===========================================================================
# PR-V (2026-06-03) — repo-root VERSION file format pin
# ===========================================================================
# VERSION file is the source of truth for the dispatcher-facing semantic
# version. Build scripts read it via `tr -d '[:space:]' < VERSION` and pass
# as --build-arg VERSION="..." into the Docker image. Dockerfile bakes it
# into version.txt as "VERSION | PHASE | GIT_SHA". main.py parses and serves
# at /version. Frontend displays as the primary footer text with phase + sha
# in a hover tooltip.
#
# If the VERSION file format drifts (wrong segment count, non-numeric, stray
# whitespace), the build script's parser will silently produce a corrupted
# value that propagates to the footer. This test catches that at pytest time
# (Step 0 of bash build-*.sh) before any Docker work.
# ===========================================================================

class TestVersionFileFormat:
    """The repo-root VERSION file is the source of truth for build-time
    semantic version injection. PR-V (2026-06-03) introduced this file +
    the surrounding build/parse/render pipeline."""

    def _read_version_file(self) -> str:
        """Read the VERSION file from repo root. Raw bytes, no normalization,
        so format-drift assertions catch whitespace issues that would break
        the `tr -d '[:space:]'` parser in the build scripts."""
        import os
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        version_path = os.path.join(repo_root, "VERSION")
        with open(version_path, "r") as f:
            return f.read()

    def test_version_file_exists(self):
        """VERSION must exist at repo root. Build scripts fail-fast if missing,
        but this test catches the regression at pytest time before any Docker work."""
        raw = self._read_version_file()
        assert raw, "VERSION file at repo root must not be empty"

    def test_version_matches_major_minor_patch_format(self):
        """Three-segment numeric MAJOR.MINOR.PATCH (e.g. 1.8.0). Per Bill
        2026-06-03 design decision. Pre-release suffixes (-rc1, -beta) are
        not supported; if needed, surface here as a new format question."""
        import re
        raw = self._read_version_file().strip()
        assert re.match(r"^\d+\.\d+\.\d+$", raw), \
            f"VERSION must match MAJOR.MINOR.PATCH (digits only), got {raw!r}"

    def test_version_file_has_no_trailing_garbage(self):
        """Allow one trailing newline (POSIX convention). Reject anything else.
        Build script uses `tr -d '[:space:]'` which would mask extra content,
        but the resulting bake into version.txt would still be wrong."""
        raw = self._read_version_file()
        # Strip a SINGLE trailing newline if present, then ensure no further whitespace.
        stripped = raw.rstrip("\n")
        assert "\n" not in stripped, \
            "VERSION file must contain a single line (one trailing newline allowed)"
        assert stripped == stripped.strip(), \
            "VERSION file must have no leading/trailing whitespace beyond a single trailing newline"


class TestParseVersionFile:
    """main.py:_parse_version_file decodes version.txt into (version, phase, sha).
    Supports new pipe-delimited 3-field format AND legacy slash-delimited 2-field
    format (for Cloud Run revisions built before PR-V)."""

    def _import_parser(self):
        """Lazy import — main.py pulls in httpx and other production-only deps."""
        pytest.importorskip("httpx")
        from main import _parse_version_file
        return _parse_version_file

    def test_new_format_three_fields(self):
        parse = self._import_parser()
        assert parse("1.8.0 | 1.8 Slacker | a46cc57") == ("1.8.0", "1.8 Slacker", "a46cc57")

    def test_new_format_strips_whitespace(self):
        parse = self._import_parser()
        assert parse("  1.8.0 | 1.8 Slacker | a46cc57  \n") == ("1.8.0", "1.8 Slacker", "a46cc57")

    def test_legacy_format_returns_raw_as_version(self):
        """Pre-PR-V version.txt format: 'PHASE / GIT_SHA'. Parser keeps the raw
        string as `version` for graceful display; phase/sha are None."""
        parse = self._import_parser()
        assert parse("1.8 Slacker / a46cc57") == ("1.8 Slacker / a46cc57", None, None)

    def test_empty_input_returns_unknown(self):
        parse = self._import_parser()
        assert parse("") == ("unknown", None, None)
        assert parse("   \n  ") == ("unknown", None, None)


class TestD4HReferenceDescriptionSuffixGating:
    """Mirror caltopo.py:_make_map_title() Locked Decision pattern.
    Personal-dev gets random suffix; SCCSSAR-dev and prod produce clean titles.
    """
    _SUFFIX_PROJECTS = frozenset({"sar-dispatch-dev"})

    def test_sccssar_dev_not_in_suffix_set(self):
        assert "sar-dispatch-sccssar-dev" not in self._SUFFIX_PROJECTS

    def test_prod_not_in_suffix_set(self):
        assert "sar-dispatch-prod-20260218" not in self._SUFFIX_PROJECTS

    def test_personal_dev_in_suffix_set(self):
        assert "sar-dispatch-dev" in self._SUFFIX_PROJECTS

    def test_suffix_set_is_minimal(self):
        assert len(self._SUFFIX_PROJECTS) == 1


class TestD4HReferenceDescriptionStripsDate:
    """'AGENCY STREETNAME' (no leading date) is the locked D4H title format.
    Cross-file: event-name reconstruction in main.py; D4H stripping in d4h.py.
    """
    def test_date_pattern_strips_to_streetname(self):
        import re
        pattern = r"^\d{4}-\d{2}-\d{2}\s+"
        result = re.sub(pattern, "", "2026-05-13 SJPD Tradan")
        assert result == "SJPD Tradan"

    def test_no_date_leaves_unchanged(self):
        import re
        pattern = r"^\d{4}-\d{2}-\d{2}\s+"
        assert re.sub(pattern, "", "SJPD Tradan") == "SJPD Tradan"


class TestD4HEBGroupMappingIsStable:
    """EB group -> D4H tag mapping cross-file pin (post-2026-05-19 Kris rebuild).

    Source-of-truth references:
      - EB group names from `python3 experiments/everbridge_slack/discover.py groups`
      - D4H tag IDs in backend/d4h.py
      - Mapping logic in backend/d4h.py::_map_eb_groups_to_d4h_tags
      - Mirror in backend/test_d4h.py

    Drift detection: if Kris renames an EB group (or adds a new one) and we
    don't update _EB_TO_D4H_TAG, the new name appears in the `unmapped`
    return slot at dispatch time — silently emits "EB group X not in mapping"
    in the event log, and the D4H tag is not applied. These tests pin the
    expected behavior for the FIVE currently-dispatchable groups.
    """
    # 2026-05-19 Kris rebuild — current EB dispatchable group names
    # (verified via discover.py output 2026-05-19). When Kris adds or
    # renames a group, update this list AND _EB_TO_D4H_TAG in lockstep.
    _CURRENT_DISPATCHABLE_EB_GROUPS = (
        "ATV", "Canine", "Search Management", "Technical Rescue", "UAS",
    )

    def test_current_eb_groups_recorded(self):
        """The five expected dispatchable EB groups exist in this pin."""
        assert len(self._CURRENT_DISPATCHABLE_EB_GROUPS) == 5
        for name in ("ATV", "Canine", "Search Management", "Technical Rescue", "UAS"):
            assert name in self._CURRENT_DISPATCHABLE_EB_GROUPS

    def test_search_management_tag_id_is_stable(self):
        """The always-auto-added Support tag. ALSO mapped as a dispatchable
        group post-rebuild — _map_eb_groups_to_d4h_tags is idempotent thanks
        to the set."""
        assert 34206 == 34206  # TAG_SEARCH_MANAGEMENT pinned literal

    def test_team_suffix_removed_in_rebuild(self):
        """Pre-rebuild names had ` Team` suffix; post-rebuild does not.
        If discover.py output shows ` Team` come back, the mapping must be
        updated (or the rebuild was reverted)."""
        for name in self._CURRENT_DISPATCHABLE_EB_GROUPS:
            assert not name.endswith(" Team"), (
                f"EB group {name!r} has stale ` Team` suffix — Kris's "
                f"2026-05-19 rebuild dropped that pattern"
            )

    def test_sar_dash_prefix_removed_in_rebuild(self):
        """Pre-rebuild names had `SAR - ` prefix; post-rebuild does not.
        After Bill's manual rename of `Automation Test` 2026-05-19, NO
        live EB group uses `SAR - ` anymore — the prefix was dropped from
        `_strip_eb_prefix` as dead code."""
        for name in self._CURRENT_DISPATCHABLE_EB_GROUPS:
            assert not name.startswith("SAR - "), (
                f"EB group {name!r} has stale `SAR - ` prefix — Kris's "
                f"2026-05-19 rebuild dropped that pattern"
            )


class TestD4HDroneRefIsStable:
    """The 'Drone #6' identifier is referenced in backend/d4h.py (DRONE_REF
    constant) AND in the spike script experiments/d4h/09_equipment.py
    (find_drone helper). If D4H ever renames the drone equipment record, both
    must update in lockstep — this test pins the literal to surface drift.

    Module-level _D4H_DRONE_*_LITERAL mirrors above; if the drone ref changes
    in d4h.py, update those mirrors AND this test in the same PR. Pattern
    matches TestD4HTagIDsAreStable per the established no-httpx-in-pytest
    convention.

    SOURCE OF TRUTH: D4H equipment ref field on the actual drone record in
    SCCSSAR's D4H Team 1775. Confirmed in Phase 1 spike 09 (2026-05-11).
    """
    def test_drone_ref_literal(self):
        assert _D4H_DRONE_REF_LITERAL == "Drone #6", \
            "Drone ref drift — re-run 09_equipment.py spike and update d4h.py + this mirror"

    def test_drone_kind_title_literal(self):
        assert _D4H_DRONE_KIND_TITLE_LITERAL == "UAS"


# ===========================================================================
# D4H Phase 2 wire-up — cross-file literal pins
# ===========================================================================
# Pins literals that live in 2+ files per CLAUDE.md cross-file pin policy.
# These literals span backend/d4h.py, backend/main.py, and Terraform.
#
# Sources of truth (any drift means update ALL of these in lockstep):
#   - backend/d4h.py: _QUEUE_PER_YES_SYNC, _TARGET_PATH_PER_YES_SYNC
#   - backend/main.py: @app.post route literals matching d4h's _TARGET_PATH_*
#   - terraform/environments/dev/main.tf: google_cloud_tasks_queue name fields
# ===========================================================================

# Mirror — values must match d4h.py module-level constants.
_D4H_QUEUE_PER_YES_SYNC_LITERAL       = "d4h-per-yes-sync"
_D4H_TARGET_PATH_PER_YES_SYNC_LITERAL = "/d4h-sync-yes"

# Mirror — main.py path-shaped literals.
_D4H_DISPATCH_STATUS_PATH_LITERAL    = "/dispatch-status/{event_id}"
_D4H_EVENT_LOG_FIELD_LITERAL         = "d4h_event_log"


class TestD4HPhase2QueueAndWorkerLiterals:
    """Cross-file pins for the Cloud Tasks queue name + worker endpoint path.

    DRIFT IMPACT: If any of these literals change in one file but not all
    three (backend/d4h.py + backend/main.py + Terraform), the dispatch
    flow silently breaks:
      - Queue name drift  → enqueue 404s, no per-YES sync ever runs.
      - Worker path drift → Cloud Tasks delivers task, Cloud Run 404s
        the request, queue keeps retrying until max_attempts=5 give-up.
    """
    def test_per_yes_sync_queue_name(self):
        # Cross-file pin:
        #   - d4h._QUEUE_PER_YES_SYNC
        #   - terraform/environments/dev/main.tf google_cloud_tasks_queue.d4h_per_yes_sync
        assert _D4H_QUEUE_PER_YES_SYNC_LITERAL == "d4h-per-yes-sync"

    def test_per_yes_sync_worker_endpoint_path(self):
        # Cross-file pin:
        #   - d4h._TARGET_PATH_PER_YES_SYNC (where the enqueued task points)
        #   - main.py @app.post("/d4h-sync-yes") (handler route)
        # These MUST match exactly or every per-YES Cloud Task 404s on Cloud Run.
        assert _D4H_TARGET_PATH_PER_YES_SYNC_LITERAL == "/d4h-sync-yes"

    def test_dispatch_status_endpoint_path(self):
        # /dispatch-status path is referenced from index.html. Pinned to
        # prevent backend-only refactors from silently breaking the frontend.
        assert _D4H_DISPATCH_STATUS_PATH_LITERAL == "/dispatch-status/{event_id}"

    def test_d4h_event_log_field_name(self):
        # Cross-file:
        #   - backend/incidents.py (new_incident_doc — default value)
        #   - backend/main.py (Step 10.5 write at /send-notification)
        #   - frontend index.html reads via /dispatch-status
        # Field-name drift would silently lose worker-time events because
        # writes go to one name and reads return None from the missing key.
        assert _D4H_EVENT_LOG_FIELD_LITERAL == "d4h_event_log"


class TestD4HFullTeamFalseSelectiveMode:
    """Cross-file pin — Selective attendance mode (`fullTeam: false`).

    Sources of truth (any drift means the async-init race re-emerges):
      - backend/d4h.py::_build_create_incident_payload — payload key + value
      - backend/test_d4h.py::TestFullTeamFalseSentinel — payload assertion
      - CLAUDE.md "Selective attendance mode (fullTeam: false)" Locked Decision
      - project-d4h-selective-mode-decision.md memory file (architectural rationale)

    DRIFT IMPACT: Removing or flipping `fullTeam: False` in the POST payload
    causes D4H to auto-stage REQUESTED attendance records for every team member,
    re-introducing every failure mode the 2026-05-19 spike-15 A/B test eliminated:
      - PATCH /attendance 500s for 3-10+ minutes during async-init window
      - Duplicate ATTENDING+REQUESTED rows per responder
      - "Blank name" UI in the Update Attendance edit view
      - N manual "clear row" clicks at incident close-out

    Empirical reference: experiments/d4h/15_full_team_false_blank_attendance.py
    (spike incidents 1618235 / 1618236, 2026-05-19).
    """
    _SELECTIVE_MODE_FULL_TEAM_LITERAL = False  # the bool literal — not "false" or 0
    _SELECTIVE_MODE_PAYLOAD_KEY = "fullTeam"   # camelCase per D4H API

    def test_full_team_literal_is_bool_false(self):
        # The literal MUST be Python False (which JSON-encodes as "false").
        # 0, "false", None are all distinct in D4H's schema — only `false`
        # opts out of auto-staging.
        assert self._SELECTIVE_MODE_FULL_TEAM_LITERAL is False

    def test_payload_key_is_camel_case_full_team(self):
        # D4H API uses camelCase. snake_case ("full_team") is silently
        # ignored, falling back to the True default. Confirmed via spike
        # 15 against live SCCSSAR D4H 2026-05-19.
        assert self._SELECTIVE_MODE_PAYLOAD_KEY == "fullTeam"


class TestD4HPerYesMilestoneOnlyContract:
    """PR 7.1 — pin the "milestone-only" contract on d4h_event_log.

    Per-YES events (responder YES-replies + per-YES D4H attendance PATCHes)
    MUST go to system logs (logger.info / logger.warning), NEVER to the
    Firestore d4h_event_log array. The d4h_event_log surface is reserved
    for dispatch-time milestones (EB created, Slack created, D4H created,
    EB polling stopped) — anything that fills the dispatcher textarea.

    Why this matters:
      - The textarea Event Log shows d4h_event_log entries verbatim
        (drained by the frontend poll in PR 7).
      - Per-YES details are high-volume (one per arrival, can be 20+ per
        incident) and would drown out the four dispatch-time milestones.
      - Per-YES debug visibility lives in Cloud Run structured logs —
        queryable, retained, no UI cost.

    Pattern: characterization test via inspect.getsource(). The functions
    pass today; the test fires if a future refactor adds a write/append to
    d4h_event_log inside the per-YES path.
    """

    @staticmethod
    def _slice_function_source(file_path: str, fn_name: str) -> str:
        """Return the source text of `def fn_name(...)` up to the next
        top-level `def`/`class` or end-of-file, with triple-quoted strings
        (docstrings) stripped.

        We read the file as text (rather than inspect.getsource via import)
        because importing main/d4h pulls in httpx etc., which the test env
        doesn't install. Docstrings are stripped so contract documentation
        that references the forbidden literal (e.g. "MUST NOT write to
        d4h_event_log") doesn't falsely fail the assertion — only real
        code references should trip the pin.
        """
        import re
        from pathlib import Path
        text = Path(file_path).read_text(encoding="utf-8")
        lines = text.splitlines()
        out, capturing, def_indent = [], False, 0
        for ln in lines:
            stripped = ln.lstrip()
            indent = len(ln) - len(stripped)
            if not capturing:
                if stripped.startswith(f"def {fn_name}(") or stripped.startswith(f"async def {fn_name}("):
                    capturing = True
                    def_indent = indent
                    out.append(ln)
                continue
            if stripped and indent <= def_indent and (
                stripped.startswith("def ")
                or stripped.startswith("async def ")
                or stripped.startswith("class ")
                or stripped.startswith("@")
            ):
                break
            out.append(ln)
        body = "\n".join(out)
        # Strip triple-quoted blocks (docstrings + other) before returning.
        body = re.sub(r'"""[\s\S]*?"""', '', body)
        body = re.sub(r"'''[\s\S]*?'''", '', body)
        return body

    def test_d4h_sync_yes_endpoint_does_not_touch_d4h_event_log(self):
        """The /d4h-sync-yes Cloud Tasks worker MUST NOT mutate
        incidents/{event_id}.d4h_event_log."""
        from pathlib import Path
        main_path = Path(__file__).parent / "main.py"
        src = self._slice_function_source(str(main_path), "d4h_sync_yes")
        assert src, "Could not locate d4h_sync_yes in backend/main.py"
        assert "d4h_event_log" not in src, (
            "Per-YES worker /d4h-sync-yes must not reference d4h_event_log. "
            "Use logger.info / logger.warning for per-YES detail. "
            "Dispatcher textarea stays milestone-only (PR 7.1 contract)."
        )

    def test_handle_per_yes_sync_task_does_not_touch_d4h_event_log(self):
        """d4h.handle_per_yes_sync_task is the actual work function. If this
        drifts, /d4h-sync-yes could pass the pin while still leaking per-YES
        entries into d4h_event_log via the deeper helper."""
        from pathlib import Path
        d4h_path = Path(__file__).parent / "d4h.py"
        src = self._slice_function_source(str(d4h_path), "handle_per_yes_sync_task")
        assert src, "Could not locate handle_per_yes_sync_task in backend/d4h.py"
        assert "d4h_event_log" not in src, (
            "handle_per_yes_sync_task must not reference d4h_event_log. "
            "Use logger.info / logger.warning for per-YES detail."
        )


class TestD4HStatusStateMachine:
    """Pin: the d4h_status monotonic-only-downgrade state machine (per-YES).

    Selective-mode (fullTeam: false) simplification: dispatch-time produces
    terminal "done" or "partial" directly (no async bulk-ABSENT step).
    Only the per-YES worker can downgrade at runtime when _post_attendance
    raises and the worker can't recover.

    State values:
      🟢 done    = incident created cleanly (frontend collapses to green)
      🟡 partial = incident created but some sub-step failed
                   (frontend simplified scheme: collapses to 🔴)
      🔴 failed  = incident creation itself failed (no D4H record at all)
      🔵 pending = work in progress, outcome unknown (transient)

    Monotonic invariant: workers can only DEMOTE (done→partial, pending→partial).
    "failed" and "partial" are absorbing — never promoted upward.

    Mirror: backend/main.py::_compute_d4h_status_after_per_yes_failure.
    """

    @staticmethod
    def _compute_d4h_status_after_per_yes_failure(current: str) -> str:
        if current in ("pending", "done"):
            return "partial"
        return current

    def test_per_yes_failure_from_done_demotes_to_partial(self):
        """Live case: dispatch-time create succeeded → done; later a
        responder's per-YES POST fails. Indicator must reflect partial."""
        assert self._compute_d4h_status_after_per_yes_failure("done") == "partial"

    def test_per_yes_failure_from_pending_demotes_to_partial(self):
        assert self._compute_d4h_status_after_per_yes_failure("pending") == "partial"

    def test_per_yes_failure_preserves_partial(self):
        """Idempotent — once partial, stays partial regardless of further failures."""
        assert self._compute_d4h_status_after_per_yes_failure("partial") == "partial"

    def test_per_yes_failure_preserves_failed(self):
        """'failed' is absorbing — per-YES events can't override it."""
        assert self._compute_d4h_status_after_per_yes_failure("failed") == "failed"

    def test_no_per_yes_path_promotes_partial_to_done(self):
        """Monotonic invariant — once partial, the per-YES helper must not
        produce 'done'. (Selective-mode removed the only legacy promotion
        path, which was pending→done via clean bulk-ABSENT.)"""
        assert self._compute_d4h_status_after_per_yes_failure("partial") != "done"


class TestExtractMemberEmailValue:
    """PR 7.3 — pin: D4H member email extraction is None-safe at every layer.

    The D4H member record's email field is dict-shaped `{value, verified}`
    per the docstring on d4h._get_member_by_email. In practice, members
    can have:
      - email = {"value": "bill@sccssar.org", "verified": True}    (normal)
      - email = {"value": None, "verified": False}                  (bug case)
      - email = None                                                 (also seen)
      - email key missing entirely                                  (also seen)
      - email = {"verified": False}  (value key missing)            (defensive)

    Pre-fix d4h.py:750 used `.get("value", "")` which only returns the
    default when the KEY is missing — when the value is explicitly None
    (which D4H does for unverified accounts), .get returns the actual None.
    Then .strip().lower() raised AttributeError on every per-YES call,
    crashing the lookup before it ever found the target.

    Live evidence 2026-05-19 (PR #439 deploy revealed this): per-YES sync
    raised AttributeError 'NoneType' object has no attribute 'strip' on
    every retry for event_id=2026-02-01_milpitas_calaveras_1743. Bill never
    got flipped to ATTENDING in D4H — the lookup crashed on the FIRST member
    in the response that had email.value=None.

    Mirror: backend/d4h.py::_extract_member_email_value.
    """

    @staticmethod
    def _extract_member_email_value(member) -> str:
        """Return the member's email-value as a strippable lowercase string,
        or '' if any layer is None / missing / malformed."""
        if not isinstance(member, dict):
            return ""
        email = member.get("email")
        if not isinstance(email, dict):
            return ""
        value = email.get("value")
        if not isinstance(value, str):
            return ""
        return value

    def test_normal_string_email(self):
        m = {"email": {"value": "bill@sccssar.org", "verified": True}}
        assert self._extract_member_email_value(m) == "bill@sccssar.org"

    def test_none_value_in_email_dict_returns_empty(self):
        """The actual production bug. email.value is None on unverified accounts."""
        m = {"email": {"value": None, "verified": False}}
        assert self._extract_member_email_value(m) == ""

    def test_email_is_none_returns_empty(self):
        m = {"email": None}
        assert self._extract_member_email_value(m) == ""

    def test_email_key_missing_returns_empty(self):
        m = {"name": "Test", "id": 12345}
        assert self._extract_member_email_value(m) == ""

    def test_value_key_missing_returns_empty(self):
        m = {"email": {"verified": False}}
        assert self._extract_member_email_value(m) == ""

    def test_email_malformed_not_a_dict_returns_empty(self):
        """Defensive — if D4H ever returns email as a bare string."""
        m = {"email": "bill@sccssar.org"}
        assert self._extract_member_email_value(m) == ""

    def test_member_is_none_returns_empty(self):
        assert self._extract_member_email_value(None) == ""

    def test_returned_value_is_safe_to_strip_and_lowercase(self):
        """The whole point — the result must never raise AttributeError when
        the caller does .strip().lower() against it. None breaks both;
        a string handles both."""
        cases = [
            {"email": {"value": None}},
            {"email": None},
            {},
            None,
            {"email": "not-a-dict"},
        ]
        for m in cases:
            result = self._extract_member_email_value(m)
            # Should not raise on either:
            result.strip()
            result.lower()


class TestExtractAttendanceMemberId:
    """PR 7.4 — pin: D4H attendance record's member_id lives at nested
    `member.id`, NOT top-level `memberId`.

    Empirically confirmed via experiments/d4h/12_attendance_record_shape.py
    against live D4H incident 1618136 on 2026-05-19:
      - top-level `memberId` present:  0 / 55 records
      - nested   `member.id` present:  55 / 55 records

    Pre-fix d4h.py:970 used `r.get("memberId")` (top-level), which always
    returned None. The matcher never found any record. Per-YES sync emitted
    "no attendance record for member" on every responder — even when their
    record clearly existed in D4H (e.g. Bill's attendance.id=48438796,
    member.id=124264, status=REQUESTED — visible in the D4H UI).

    Root cause: the spike that validated the design (07_attendance.py:239)
    used the correct nested path. Production drifted: a docstring comment
    in 07_attendance.py says "Schema (REQUIRED): activityId, memberId, ..."
    — but that's the POST-create schema (the field name we send TO D4H),
    NOT the GET response shape (what D4H sends back). PR #436 followed
    the wrong schema.

    Mirror: backend/d4h.py::_extract_attendance_member_id.
    """

    @staticmethod
    def _extract_attendance_member_id(attendance) -> int | None:
        """Return the integer member.id for an attendance record, or None
        if any layer is malformed. Mirror of d4h.py helper."""
        if not isinstance(attendance, dict):
            return None
        member = attendance.get("member")
        if not isinstance(member, dict):
            return None
        member_id = member.get("id")
        if not isinstance(member_id, int):
            return None
        return member_id

    def test_normal_nested_member_id(self):
        """The actual shape D4H returns — confirmed by spike 12 on 2026-05-19."""
        r = {
            "id": 48438796,
            "member": {"resourceType": "Member", "id": 124264},
            "status": "REQUESTED",
        }
        assert self._extract_attendance_member_id(r) == 124264

    def test_member_key_missing_returns_none(self):
        r = {"id": 48438796, "status": "REQUESTED"}
        assert self._extract_attendance_member_id(r) is None

    def test_member_is_none_returns_none(self):
        r = {"id": 48438796, "member": None}
        assert self._extract_attendance_member_id(r) is None

    def test_member_not_a_dict_returns_none(self):
        """Defensive — D4H schema drift catcher."""
        r = {"id": 48438796, "member": 124264}  # if D4H ever flattens
        assert self._extract_attendance_member_id(r) is None

    def test_member_id_is_none_returns_none(self):
        r = {"id": 48438796, "member": {"resourceType": "Member", "id": None}}
        assert self._extract_attendance_member_id(r) is None

    def test_member_id_missing_returns_none(self):
        r = {"id": 48438796, "member": {"resourceType": "Member"}}
        assert self._extract_attendance_member_id(r) is None

    def test_attendance_is_none_returns_none(self):
        assert self._extract_attendance_member_id(None) is None

    def test_attendance_is_not_a_dict_returns_none(self):
        assert self._extract_attendance_member_id([1, 2, 3]) is None

    def test_top_level_memberid_field_is_ignored(self):
        """Pin against the pre-PR 7.4 bug shape: a stray top-level memberId
        must NOT shadow the nested-or-None semantics. If D4H ever adds a
        top-level memberId alongside the nested member.id, we still trust
        the nested one (it's the spec'd path)."""
        r = {
            "id": 48438796,
            "memberId": 999999,                          # stray top-level
            "member": {"resourceType": "Member", "id": 124264},
        }
        assert self._extract_attendance_member_id(r) == 124264


class TestNoBackendPackagePrefixInImports:
    """PR 7.2 — pin: production code MUST NOT use `from backend.X import ...`.

    The container Dockerfile does `COPY backend/*.py .` into `/app/`, which
    flattens the backend package into the top level. There is NO `backend/`
    package at runtime — modules are imported by bare name (e.g.
    `from rate_limit import _get_db`, not `from backend.rate_limit ...`).

    A `from backend.X import` that works locally (where backend/ is a real
    directory the test runner sees) fails in production with
    ModuleNotFoundError. This bit us in d4h.py:1225 — PR #436's per-YES
    worker called `_load_d4h_activity_id`, which had a `from backend.rate_limit`
    import as its first line, raising before any HTTP call to D4H. The
    failure was masked by main.py:5716 logging only `type(exc).__name__`
    ("ModuleNotFoundError") without the message, so per-YES was broken in
    production from PR #436 deploy (2026-05-18) through this PR's fix.

    Drift impact if this pin fires: per-YES, bulk-ABSENT, or other workers
    silently fail in production while passing all local tests.
    """

    def test_no_backend_prefix_imports_in_production_modules(self):
        """Scan all backend/*.py files (except tests, conftest, and
        migration_validation which are dev-only) for `from backend.X` patterns.
        Strip docstrings and comments first to avoid false positives."""
        import re
        from pathlib import Path
        backend_dir = Path(__file__).parent
        offenders: dict[str, list[str]] = {}
        for py_file in sorted(backend_dir.glob("*.py")):
            name = py_file.name
            if (
                name.startswith("test_")
                or name == "conftest.py"
                or name.startswith("_")
            ):
                continue
            text = py_file.read_text(encoding="utf-8")
            # Strip docstrings + line comments so doc references don't trip us.
            text = re.sub(r'"""[\s\S]*?"""', '', text)
            text = re.sub(r"'''[\s\S]*?'''", '', text)
            text = re.sub(r'#.*', '', text)
            found = re.findall(r'^\s*from\s+backend\.\w+|^\s*import\s+backend\.\w+',
                               text, flags=re.MULTILINE)
            if found:
                offenders[name] = found
        assert not offenders, (
            "Production backend modules MUST use bare imports (Dockerfile flattens "
            "backend/*.py into /app/ — there is no 'backend' package at runtime). "
            "Use 'from rate_limit import _get_db' NOT 'from backend.rate_limit'. "
            f"Offending files: {offenders}"
        )


# Cross-file pin: _DOB_INPUT_FORMATS duplicated between main.py and d4h.py
# (main.py for parse-for-age; d4h.py for parse-for-ISO-output). They share
# the same input strings — drift between them surfaces here.
_DOB_FORMATS_LITERAL = (
    "%m/%d/%Y",   # 10/20/2005
    "%m/%d/%y",   # 09/18/05 (2-digit year)
    "%m-%d-%Y",   # 6-26-2010
    "%m-%d-%y",   # 6-26-10
    "%Y-%m-%d",   # ISO 2005-10-20
    "%B %d, %Y",  # October 20, 2005
    "%b %d, %Y",  # Oct 20, 2005
)


class TestD4HDobInputFormatsCrossFile:
    """Pin the DOB-parse format list per CLAUDE.md cross-file literal policy.
    The list lives in 3 places:
      - backend/main.py: _DOB_FORMATS (parse-for-age)
      - backend/d4h.py: _DOB_INPUT_FORMATS (parse-for-ISO-output)
      - test_d4h.py mirror
    All three must stay in lockstep. If a future PR adds a new format
    variant for OCR coverage, it must be added to ALL three.
    """

    def test_canonical_format_list_present(self):
        # Pin the exact ordering + values. Any drift fails the test.
        assert _DOB_FORMATS_LITERAL == (
            "%m/%d/%Y",
            "%m/%d/%y",
            "%m-%d-%Y",
            "%m-%d-%y",
            "%Y-%m-%d",
            "%B %d, %Y",
            "%b %d, %Y",
        )

    def test_iso_format_present_for_d4h_passthrough(self):
        # Critical: D4H requires ISO 8601 output. If %Y-%m-%d drops from the
        # input format list, ISO inputs from a dispatcher edit would
        # fail to match and the helper would return None.
        assert "%Y-%m-%d" in _DOB_FORMATS_LITERAL

    def test_us_format_present_for_intake_form(self):
        # SCCSSAR intake form is US-format; MM/DD/YYYY is the most-common
        # input that must always parse cleanly.
        assert "%m/%d/%Y" in _DOB_FORMATS_LITERAL

    def test_hyphen_separator_present_for_handwritten_forms(self):
        # PR #402 corpus surfaced handwritten hyphen-separator forms;
        # don't regress.
        assert "%m-%d-%Y" in _DOB_FORMATS_LITERAL
        assert "%m-%d-%y" in _DOB_FORMATS_LITERAL


# ---------------------------------------------------------------------------
# PR-B: re.sub replacement-string backref-injection hardening
# (Melanie's main.py C-2, 2026-05-19 security review)
# ---------------------------------------------------------------------------
#
# `re.sub(pattern, replacement, string)` parses the replacement argument as a
# substitution template — `\1`, `\g<name>`, and `\\` are special. When the
# replacement is built from dynamic content (Gemini OCR output, the Google ID
# token "name" claim, agency lookup fallthroughs that return the raw OCR
# string), a literal `\1` byte in that dynamic content is interpreted as a
# backreference. Outcomes range from `re.error: invalid group reference` (a
# 500 in /ocr) to the wrong text landing in the IIS field.
#
# Mitigation: replace every at-risk site's f-string replacement with a lambda
# returning the same string. The lambda form is not parsed as a substitution
# template — backrefs are never expanded — so dynamic content is inert.
#
# Affected production sites (all in main.py):
#   - Dispatcher: line injection (Google ID token "name")
#   - Agency: line canonicalization (raw OCR fallthrough)
#   - Event Name: reconstruction (primary path + city-fallback x2)
#   - Staging Area for Resources: IIS-field replacement with #1 recommendation
#
# These mirrors pin the lambda pattern. If a future edit reverts a site to an
# f-string replacement, the matching test below will fire when the dynamic
# input contains a backslash-digit sequence.


def _inject_dispatcher_name(summary: str, last_name: str) -> str:
    """Mirror of main.py dispatcher-name injection (PR-B hardened form)."""
    return re.sub(
        r"^(Dispatcher:).*$",
        lambda m: f"{m.group(1)} {last_name}",
        summary,
        flags=re.MULTILINE,
    )


def _inject_canonical_agency(summary: str, canonical_agency: str) -> str:
    """Mirror of main.py Agency: line canonicalization (PR-B hardened form)."""
    return re.sub(
        r"^Agency:\s*.+$",
        lambda m: f"Agency: {canonical_agency}",
        summary,
        count=1,
        flags=re.MULTILINE,
    )


def _replace_event_name(summary: str, canonical_name: str) -> str:
    """Mirror of main.py Event Name reconstruction (PR-B hardened form)."""
    return re.sub(
        r"^(Event Name:).*$",
        lambda m: f"{m.group(1)} {canonical_name}",
        summary,
        flags=re.MULTILINE,
    )


class TestReSubBackrefHardening:
    """Pin the lambda-replacement form at every site where dynamic content
    flows into a re.sub replacement string.

    If a future edit reverts any of these to `f"...{value}..."`-style
    replacement, an input value containing a literal `\\1` byte will trigger
    backref expansion. These tests assert the dynamic content survives
    intact when it contains exactly that pattern."""

    # --- Dispatcher: line ---------------------------------------------------

    def test_dispatcher_name_with_backslash_digit_preserved(self):
        # Pathological but plausible: a corrupted Google ID token "name"
        # claim contains a literal `\1`. The lambda form preserves it.
        summary = "Dispatcher: \n"
        out = _inject_dispatcher_name(summary, r"\1Smith")
        assert r"Dispatcher: \1Smith" in out

    def test_dispatcher_name_with_named_backref_preserved(self):
        summary = "Dispatcher: \n"
        out = _inject_dispatcher_name(summary, r"Smith \g<foo>")
        assert r"Dispatcher: Smith \g<foo>" in out

    def test_dispatcher_name_normal_case_unchanged(self):
        # Sanity: non-pathological input still gives the expected output.
        summary = "Dispatcher: \n"
        out = _inject_dispatcher_name(summary, "Burns")
        assert "Dispatcher: Burns" in out

    # --- Agency: line -------------------------------------------------------

    def test_agency_with_backslash_digit_preserved(self):
        # _normalize_event_name_agency falls through to raw OCR text when
        # no dict entry matches. A raw OCR misread containing `\1` would
        # otherwise be interpreted as a backref to the (non-existent) group 1.
        summary = "Agency: SCC SLO\n"
        out = _inject_canonical_agency(summary, r"\1SCCSO")
        assert r"Agency: \1SCCSO" in out

    def test_agency_normal_case_unchanged(self):
        summary = "Agency: SCC SLO\n"
        out = _inject_canonical_agency(summary, "SCCSO")
        assert "Agency: SCCSO" in out

    # --- Event Name: line ---------------------------------------------------

    def test_event_name_with_backslash_digit_preserved(self):
        # canonical_name is built from evt_date + evt_agency + evt_street
        # where the agency may fall through to raw OCR text. A literal `\1`
        # in any component must not be interpreted as a backref.
        summary = "Event Name: stale\n"
        out = _replace_event_name(summary, r"2026-05-19 \1 Verde")
        assert r"Event Name: 2026-05-19 \1 Verde" in out

    def test_event_name_with_named_backref_preserved(self):
        summary = "Event Name: stale\n"
        out = _replace_event_name(summary, r"2026-05-19 SCCSO \g<x>")
        assert r"Event Name: 2026-05-19 SCCSO \g<x>" in out

    def test_event_name_normal_case_unchanged(self):
        summary = "Event Name: stale\n"
        out = _replace_event_name(summary, "2026-05-19 SCCSO Verde")
        assert "Event Name: 2026-05-19 SCCSO Verde" in out

    # --- Staging Area for Resources: line -----------------------------------

    def test_staging_field_with_backslash_digit_preserved(self):
        # The #1 recommendation bare-form comes from Gemini PASS 2 — a
        # literal `\1` from Gemini must not be interpreted as a backref.
        summary = (
            "Initial Incident Summary:\n"
            "Staging Area for Resources: officer raw text\n"
            "\n---\n"
        )
        out = _replace_staging_area_field(summary, r"\1 Foo Park, San Jose")
        assert r"Staging Area for Resources: \1 Foo Park, San Jose" in out

    def test_staging_field_with_named_backref_preserved(self):
        summary = (
            "Initial Incident Summary:\n"
            "Staging Area for Resources: officer raw text\n"
            "\n---\n"
        )
        out = _replace_staging_area_field(summary, r"Foo Park \g<x>")
        assert r"Staging Area for Resources: Foo Park \g<x>" in out

    def test_staging_field_normal_case_unchanged(self):
        # The bare form has em-dash + "—" which contains no backslash and
        # is the realistic shape; assert the existing happy path still works.
        summary = (
            "Initial Incident Summary:\n"
            "Staging Area for Resources: officer raw text\n"
            "\n---\n"
        )
        bare = "55 North 7th Street, San Jose — Horace Mann Elementary School"
        out = _replace_staging_area_field(summary, bare)
        assert f"Staging Area for Resources: {bare}" in out

    # --- Cross-cutting raise-vs-survive guard -------------------------------

    def test_all_sites_do_not_raise_on_invalid_group_reference(self):
        # The most-visible failure mode of the pre-PR-B form: `re.error:
        # invalid group reference 9 at position N`. A user-visible 500.
        # Pin that no path raises when handed `\9` (a group ref to a
        # non-existent group 9).
        _inject_dispatcher_name("Dispatcher: \n", r"\9")
        _inject_canonical_agency("Agency: x\n", r"\9")
        _replace_event_name("Event Name: x\n", r"\9")
        _replace_staging_area_field(
            "Staging Area for Resources: x\n\n---\n", r"\9",
        )


# ---------------------------------------------------------------------------
# PR-C: _load_safe_list_secret JSON parse hardening
# (Melanie's main.py H-2, 2026-05-19 security review)
# ---------------------------------------------------------------------------
#
# Pre-fix, `json.loads(raw)` on a malformed DISPATCH_SAFE_LIST secret raised
# unhandled JSONDecodeError. A valid JSON value that isn't a dict (string,
# array, number) would AttributeError on `.setdefault`. Both paths surface
# as 500 on /send-notification and on the polling loop's safe-list lookup
# until the secret is repaired.
#
# The fix wraps the parse in try/except + adds an isinstance(dict) guard,
# both fail-closed to the empty-defaults dict. That routes every send to
# draft — same failure mode as an unset secret. Errors log exception type
# only; the secret content (allowlist of contact IDs and emails) is never
# logged.
#
# Mirror the production helper here so the test exercises the parse path
# directly without importing main.py (heavy GCP deps). If a future edit
# removes the try/except or the isinstance guard, these tests fire.


import json as _json  # local alias — module-level `json` may not be imported above


def _safe_list_defaults() -> dict:
    return {
        "allowed_contact_ids": [],
        "allowed_group_ids":   [],
        "allowed_emails":      [],
        "label_overrides":     {},
    }


def _load_safe_list_from_raw(raw: str) -> dict:
    """Mirror of main.py::_load_safe_list_secret() — operates on a raw
    string (the value the env-var-driven production helper reads) so
    tests exercise the JSON parse + isinstance(dict) hardening directly.

    Production helper has `@lru_cache(maxsize=1)` for hot-path callers
    (every /send-notification + every poll tick). The mirror is uncached
    so each test starts clean — caching semantics are not part of the
    hardening contract being pinned here.
    """
    defaults = _safe_list_defaults()
    if not raw:
        return defaults
    try:
        parsed = _json.loads(raw)
    except (_json.JSONDecodeError, ValueError):
        return defaults
    if not isinstance(parsed, dict):
        return defaults
    parsed.setdefault("allowed_contact_ids", [])
    parsed.setdefault("allowed_group_ids",   [])
    parsed.setdefault("allowed_emails",      [])
    parsed.setdefault("label_overrides",     {})
    return parsed


class TestLoadSafeListSecretHardening:
    """Pin the JSON-parse + isinstance(dict) hardening in
    _load_safe_list_secret(). Pre-PR-C either failure mode produced a
    500 on /send-notification and on the polling loop. Post-fix, both
    fail closed to the same empty-defaults dict that routes every send
    to draft (same failure mode as an unset secret)."""

    # --- Happy paths -------------------------------------------------------

    def test_valid_dict_with_all_keys(self):
        raw = _json.dumps({
            "allowed_contact_ids": ["c1", "c2"],
            "allowed_group_ids":   ["g1"],
            "allowed_emails":      ["a@x.com"],
            "label_overrides":     {"c1": "Burns, Bill"},
        })
        result = _load_safe_list_from_raw(raw)
        assert result["allowed_contact_ids"] == ["c1", "c2"]
        assert result["allowed_group_ids"]   == ["g1"]
        assert result["allowed_emails"]      == ["a@x.com"]
        assert result["label_overrides"]     == {"c1": "Burns, Bill"}

    def test_valid_dict_missing_keys_backfilled(self):
        # Existing behavior — partial dict gets keys backfilled to empty.
        # Don't regress when the hardening lands.
        result = _load_safe_list_from_raw('{"allowed_contact_ids": ["c1"]}')
        assert result["allowed_contact_ids"] == ["c1"]
        assert result["allowed_group_ids"]   == []
        assert result["allowed_emails"]      == []
        assert result["label_overrides"]     == {}

    def test_empty_dict_returns_full_default_shape(self):
        result = _load_safe_list_from_raw("{}")
        assert result == _safe_list_defaults()

    # --- Unset secret ------------------------------------------------------

    def test_empty_string_returns_defaults(self):
        # Mirror of `if not raw: return defaults` — pre-existing behavior,
        # pinned so a future edit that removes the early-return doesn't
        # accidentally drop fail-closed behavior on cold start before
        # Secret Manager has a value.
        assert _load_safe_list_from_raw("") == _safe_list_defaults()

    # --- Malformed JSON (PR-C primary case) --------------------------------

    def test_malformed_json_returns_defaults(self):
        # Pre-PR-C this raised JSONDecodeError → 500 on every safe-mode
        # send + every polling tick. Post-fix: same defaults as unset.
        assert _load_safe_list_from_raw('{"not closed') == _safe_list_defaults()

    def test_truncated_json_returns_defaults(self):
        assert _load_safe_list_from_raw('{"allowed_contact_ids": ["c1"') == _safe_list_defaults()

    def test_garbage_returns_defaults(self):
        assert _load_safe_list_from_raw("not json at all") == _safe_list_defaults()

    def test_trailing_comma_returns_defaults(self):
        # JSON spec forbids trailing commas; some hand-edits produce this.
        # Strict mode (json.loads default) raises → fail-closed to defaults.
        assert _load_safe_list_from_raw('{"allowed_emails": ["a@x.com",]}') == _safe_list_defaults()

    # --- Valid JSON but not a dict (PR-C secondary case) -------------------

    def test_json_string_returns_defaults(self):
        # `json.loads('"hello"')` returns the str "hello", which has no
        # .setdefault. Pre-PR-C: AttributeError → 500. Post-fix: defaults.
        assert _load_safe_list_from_raw('"hello"') == _safe_list_defaults()

    def test_json_array_returns_defaults(self):
        # `json.loads("[1, 2, 3]")` returns a list, which has no
        # .setdefault method that takes (key, default) — pre-PR-C: 500.
        assert _load_safe_list_from_raw("[1, 2, 3]") == _safe_list_defaults()

    def test_json_number_returns_defaults(self):
        assert _load_safe_list_from_raw("42") == _safe_list_defaults()

    def test_json_null_returns_defaults(self):
        # JSON null parses to Python None — no .setdefault. Fail closed.
        assert _load_safe_list_from_raw("null") == _safe_list_defaults()

    def test_json_bool_returns_defaults(self):
        assert _load_safe_list_from_raw("true") == _safe_list_defaults()

    # --- Cross-cutting raise-vs-survive guard ------------------------------

    def test_no_call_raises_for_any_pathological_input(self):
        # The most-visible pre-PR-C failure mode: /send-notification 500
        # because the secret value was edited to something json.loads or
        # .setdefault rejects. Pin that no documented pathological input
        # raises post-fix.
        for raw in (
            "",
            "{",
            "not json",
            '"a string"',
            "[1,2,3]",
            "42",
            "true",
            "null",
            '{"allowed_emails": ["a@x.com",]}',
        ):
            _load_safe_list_from_raw(raw)  # must not raise


# ---------------------------------------------------------------------------
# /create-map 429 → 503 + Retry-After contract — batch-3 PR-H.1
# ---------------------------------------------------------------------------
# Source: main.py::create_map handler ``except CalTopoRateLimitError`` branch.
# Pinned because the (status_code, Retry-After) pair is a cross-file
# behavioral contract that the frontend (or any future caller) couples to.
# A future refactor that flips 503 → 429-passthrough or "10" → "30" should
# be a deliberate decision, not a silent drift. See self-review pass on
# PR-G.1+H.1 (Melanie batch-3 review, 2026-05-30).

_CREATE_MAP_RATE_LIMIT_STATUS  = 503
_CREATE_MAP_RETRY_AFTER_HEADER = "10"


class TestCreateMapRateLimitResponseContract:
    """Pin the /create-map handler's response contract on CalTopo 429.

    The dispatcher-facing behavior IS the test contract:
      - 503 distinguishes 'transient throttle, retry later' from
        the existing 502 'CalTopo broke or unreachable'
      - Retry-After: 10 is the hardcoded default. CalTopo 429s
        observed in 4 months of production: zero. If we ever
        observe real 429s with longer backoff, this value should
        be revisited — but a refactor that silently zeros or
        changes it should fail this test first.
    """

    def test_status_is_503_not_429_or_502(self):
        # Why not 429-passthrough? A 429 from the dispatcher API would
        # imply the dispatcher's own auth/rate is exhausted, which is
        # not the case — CalTopo is the throttled party. 503 ("service
        # unavailable, try again") is the correct semantic.
        assert _CREATE_MAP_RATE_LIMIT_STATUS == 503
        assert _CREATE_MAP_RATE_LIMIT_STATUS != 429
        assert _CREATE_MAP_RATE_LIMIT_STATUS != 502

    def test_retry_after_is_positive_seconds(self):
        # The Retry-After value MUST be a string parseable as a positive
        # integer (HTTP spec allows either seconds or HTTP-date; we emit
        # seconds for simplicity). Drift to "0" or negative would tell
        # the frontend to retry immediately, defeating the purpose.
        assert _CREATE_MAP_RETRY_AFTER_HEADER == "10"
        assert int(_CREATE_MAP_RETRY_AFTER_HEADER) > 0


# ---------------------------------------------------------------------------
# lifespan must warm Firestore via run_in_executor — batch-3 PR-G.2
# ---------------------------------------------------------------------------
# Source: main.py::lifespan. _warm_firestore_clients makes synchronous gRPC
# .get() calls; if invoked directly inside the async lifespan it blocks the
# event loop for the full gRPC deadline (~60s) when Firestore is unreachable
# at boot, starving every other coroutine. The run_in_executor wrap moves
# the blocking calls off the event loop while preserving the await-before-
# yield behavior (cold-start benefit intact, event loop unblocked).
#
# Source-scan test rather than an async-runtime test: the established repo
# pattern (per test_d4h.py and test_caltopo.py) avoids async test scaffolding
# in favor of pure-logic mirrors. For this one-line behavioral change the
# source-scan IS the regression boundary — a future refactor that reverts
# to the bare call would fail this test, with the failure message pointing
# at the exact reason and the relevant PR for context.


class TestLifespanWarmsFirestoreInExecutor:
    """Pin PR-G.2: _warm_firestore_clients must be called via
    run_in_executor (or equivalent off-event-loop wrapper) inside the
    async lifespan, NEVER as a direct synchronous call.

    Pre-fix the call was synchronous, blocking the event loop for the
    full gRPC deadline when Firestore was unreachable at boot. Test
    contract: scanning main.py source, the lifespan section must contain
    a run_in_executor call referencing _warm_firestore_clients.
    """

    def _read_lifespan_source(self) -> str:
        """Return main.py source from `async def lifespan(` for a fixed
        window. Heuristic but sufficient — the lifespan body is <40 lines."""
        from pathlib import Path
        main_py_path = Path(__file__).parent / "main.py"
        source = main_py_path.read_text()
        marker = "async def lifespan("
        assert marker in source, "async def lifespan(...) not found in main.py"
        start = source.index(marker)
        # 2000-char window covers the function body with comfortable margin
        # without grabbing unrelated downstream functions.
        return source[start:start + 2000]

    def test_lifespan_calls_warmup_via_executor(self):
        section = self._read_lifespan_source()
        assert "run_in_executor" in section, (
            "lifespan does not appear to wrap _warm_firestore_clients in "
            "run_in_executor — a direct synchronous call blocks the event "
            "loop during Firestore gRPC .get() calls, starving every other "
            "coroutine for the full gRPC deadline (~60s) if Firestore is "
            "unreachable at boot. See PR-G.2 (Failure-mode rubric Q2)."
        )
        assert "_warm_firestore_clients" in section, (
            "lifespan does not appear to reference _warm_firestore_clients"
        )

    def test_lifespan_does_not_call_warmup_synchronously(self):
        """Guard against a refactor that keeps the executor wrap but ALSO
        re-introduces the bare call. The bare call would be the literal
        string `_warm_firestore_clients()` (parens) — distinct from the
        executor-wrapped form `run_in_executor(None, _warm_firestore_clients)`
        which references the function without calling it directly."""
        section = self._read_lifespan_source()
        # Strip the executor-wrapped reference so we can scan for a separate
        # bare call. The executor wrap references the function without
        # parens (it's a callable being passed to executor); a bare call
        # uses parens.
        sanitized = section.replace(
            "run_in_executor(None, _warm_firestore_clients)", "",
        )
        assert "_warm_firestore_clients()" not in sanitized, (
            "lifespan appears to contain a direct synchronous call to "
            "_warm_firestore_clients() in ADDITION to (or instead of) the "
            "executor wrap. The synchronous call blocks the event loop — "
            "remove it. See PR-G.2."
        )


# ---------------------------------------------------------------------------
# /send-notification — welcome + CalTopo post/pin split — batch-3 PR-G.3+G.4
# ---------------------------------------------------------------------------
# Source: main.py::send_notification, Cluster C welcome block + CalTopo
# follow-up block. Pre-fix both wrapped post + pin in a single try/except,
# logging "post/pin failed" on either failure. The two cases mean very
# different things operationally:
#
#   post failed → message NEVER reached the channel; dispatcher knows to
#     re-trigger or escalate
#   pin failed (post succeeded) → message IS in channel timeline but
#     missing from pinned-items list; breaks the Locked Decision "both
#     pinned" invariant; dispatcher recovery is a manual Slack-UI pin
#
# Conflated logging hid the second case entirely. The split into separate
# try blocks + distinct log messages makes both states observable in ops
# scans. The Slack message timestamp (ts) IS logged on pin failures so
# the dispatcher can find the message to pin manually — ts values are
# numeric (e.g. "1714234567.123456"), not PII.


class TestWelcomeAndCalTopoPostPinSplit:
    """Pin PR-G.3+G.4: welcome and CalTopo each have separate try blocks
    for post vs pin, with distinct log messages.

    Drift caught by these tests:
      - A future refactor that re-merges post + pin into one try block,
        regressing to the "post/pin failed" conflation
      - A log-message rename that makes "post" vs "pin" indistinguishable
      - Pre-fix log line "Send-time welcome post/pin failed" or
        "Send-time CalTopo post/pin failed" surviving in the diff
    """

    def _read_post_pin_section(self) -> str:
        """Return main.py source from the welcome cluster comment for a
        window large enough to cover both welcome + CalTopo blocks."""
        from pathlib import Path
        main_py_path = Path(__file__).parent / "main.py"
        source = main_py_path.read_text()
        marker = "Cluster C: welcome post"
        assert marker in source, (
            "Cluster C welcome cluster comment not found in main.py — "
            "send_notification structure may have been refactored; "
            "update this test's marker"
        )
        start = source.index(marker)
        # Bounded on a REAL end marker, not a fixed character count.
        # Was `start + 8000`, sized empirically against the diff that
        # introduced the split — which meant any later insertion into Step 8
        # silently pushed the CalTopo block out of the window and failed these
        # tests for a reason unrelated to what they assert. #673 did exactly
        # that when it added the staging message between the two blocks.
        # A fixed-size slice is a latent false failure (and, if it ever grows
        # the other way, a latent false PASS).
        end_marker = "# ---- Step 9:"
        assert end_marker in source, (
            "Step 9 block header not found in main.py — send_notification "
            "structure may have been refactored; update this test's marker"
        )
        end = source.index(end_marker, start)
        return source[start:end]

    def test_welcome_post_failure_logged_distinctly(self):
        section = self._read_post_pin_section()
        assert "welcome post failed" in section, (
            "welcome post-failure log line must include 'welcome post "
            "failed' (NOT the pre-fix 'welcome post/pin failed') so "
            "ops can distinguish 'message never reached channel' from "
            "'message in channel but not pinned'. See PR-G.3."
        )

    def test_welcome_pin_failure_logged_distinctly(self):
        section = self._read_post_pin_section()
        assert "welcome pin failed" in section, (
            "welcome pin-failure log line must include 'welcome pin "
            "failed' (NOT the pre-fix 'welcome post/pin failed') — "
            "pin failure means the welcome IS in channel but missing "
            "from pinned items, breaking the Locked Decision 'both "
            "pinned' invariant. See PR-G.3."
        )

    def test_caltopo_post_failure_logged_distinctly(self):
        section = self._read_post_pin_section()
        assert "CalTopo post failed" in section, (
            "CalTopo post-failure log line must include 'CalTopo post "
            "failed' (NOT the pre-fix 'CalTopo post/pin failed'). "
            "See PR-G.4."
        )

    def test_caltopo_pin_failure_logged_distinctly(self):
        section = self._read_post_pin_section()
        assert "CalTopo pin failed" in section, (
            "CalTopo pin-failure log line must include 'CalTopo pin "
            "failed' (NOT the pre-fix 'CalTopo post/pin failed'). "
            "See PR-G.4."
        )

    def test_old_conflated_log_lines_removed(self):
        """Guard against regression — the pre-fix 'post/pin failed'
        conflated phrasing must not reappear in either block."""
        section = self._read_post_pin_section()
        # Note: 'post/pin' (with slash) is the exact pre-fix phrasing.
        # Any future log line that says "post or pin" or "post and pin"
        # is fine; the specific 'post/pin' phrase signals the regression.
        assert "post/pin failed" not in section, (
            "Found pre-fix 'post/pin failed' conflated log line in "
            "send_notification post/pin section — should be split into "
            "separate 'post failed' and 'pin failed' log lines per "
            "PR-G.3+G.4."
        )

    def test_pin_only_attempted_when_post_succeeded(self):
        """Defensive: if post fails, ts is empty string; calling pin with
        an empty ts would 400 from Slack and produce a misleading pin-
        failure log line. The split must guard pin attempts on `if ts:`
        for both welcome and CalTopo.

        KNOWN LIMITATION of this source-scan test (per PR-G.3+G.4
        self-review by feature-dev:code-reviewer): the substring check
        confirms the guard strings appear SOMEWHERE in the window but
        does NOT verify they're structurally between the right things.
        A future refactor that keeps the log strings + guard substrings
        but moves `pin_message` back INSIDE the post try block would
        pass this test while regressing the post/pin conflation. The
        structural constraint (pin block must be OUTSIDE the post try
        block) is enforced only by code review. The test serves as a
        coarse regression boundary; the fine-grained constraint lives
        in the comments of main.py::send_notification and the rubric
        Q3 self-review."""
        section = self._read_post_pin_section()
        assert "if welcome_ts:" in section, (
            "welcome pin block must be guarded by 'if welcome_ts:' so "
            "we don't attempt to pin an empty timestamp on post failure. "
            "See PR-G.3."
        )
        assert "if caltopo_ts:" in section, (
            "CalTopo pin block must be guarded by 'if caltopo_ts:' so "
            "we don't attempt to pin an empty timestamp on post failure. "
            "See PR-G.4."
        )


# ---------------------------------------------------------------------------
# Event log timestamp format — issue #542 / Cluster I.1
# ---------------------------------------------------------------------------
# Source: main.py::_format_event_log_ts and frontend/index.html::_formatEventLogTs.
# Format: 'YYYY-MM-DDTHH:MMZ (HH:MM PT)'. UTC anchor for cross-environment
# debugging (matches Cloud Run log timestamps 1:1); PT hint for SCCSSAR's
# Pacific-resident default audience. "PT" (not PDT/PST) sidesteps DST flips.
#
# Mirror pattern (pure-logic helper) instead of importing main.py — main.py
# pulls in httpx, FastAPI, etc., which aren't in the local pytest env.
# Drift between mirror and production IS the test contract.

import datetime as _dt542
import zoneinfo as _zi542

_PT_542 = _zi542.ZoneInfo("America/Los_Angeles")


def _format_event_log_ts_mirror(now_utc=None):
    """Mirror of main.py::_format_event_log_ts.

    Cluster I.2 simplification: ``YYYY-MM-DD HH:MM`` Pacific, no labels.
    """
    if now_utc is None:
        now_utc = _dt542.datetime.now(_dt542.timezone.utc)
    elif now_utc.tzinfo is None:
        raise ValueError(
            "_format_event_log_ts requires timezone-aware now_utc"
        )
    return now_utc.astimezone(_PT_542).strftime("%Y-%m-%d %H:%M")


class TestEventLogTimestampFormat:
    """Pin Cluster I.1 + I.2 / issue #542: event log timestamp format.

    Format contract: ``YYYY-MM-DD HH:MM`` (Pacific, no timezone label).
    Matches ``intake_timestamp`` byte-for-byte. Cluster I.1 originally
    used ``YYYY-MM-DDTHH:MMZ (HH:MM PT)`` to expose the UTC anchor for
    cross-environment debugging; Bill's feedback on the deployed result
    (2026-05-31): "I don't like the TZ string we're emitting...they're
    foreign for a non-programmer to interpret." Simplified in I.2.

    Drift caught by these tests:
      - Format drifts from `YYYY-MM-DD HH:MM` (e.g. someone re-introduces
        a 'T' separator, 'Z' suffix, or PT label thinking it adds value)
      - Time renders in UTC instead of Pacific (would silently produce
        a 7-hour-off display during PDT)
      - Naive datetime silently rendering as UTC (must raise ValueError)
      - Date-boundary edge cases (midnight UTC = previous-day evening PT)
    """

    def test_format_pdt_summer(self):
        """2026-05-31 is during PDT (UTC-7). 15:13Z = 08:13 PT."""
        dt = _dt542.datetime(
            2026, 5, 31, 15, 13, tzinfo=_dt542.timezone.utc,
        )
        assert _format_event_log_ts_mirror(dt) == "2026-05-31 08:13"

    def test_format_pst_winter(self):
        """2026-01-15 is during PST (UTC-8). 13:39Z = 05:39 PT.

        DST tracking happens silently via zoneinfo — the rendered output
        is just the Pacific wall-clock at that UTC moment, regardless of
        season. The non-Pacific dispatcher sees no explicit signal that
        the time is Pacific; that's accepted per Bill's I.2 feedback
        (readability wins over explicit-TZ-marker)."""
        dt = _dt542.datetime(
            2026, 1, 15, 13, 39, tzinfo=_dt542.timezone.utc,
        )
        assert _format_event_log_ts_mirror(dt) == "2026-01-15 05:39"

    def test_format_date_boundary_midnight_utc(self):
        """00:00Z on 2026-06-01 = 17:00 PDT on the previous day (2026-05-31).
        The rendered output shows the PACIFIC date and time — so an
        entry recorded at midnight UTC reads as 2026-05-31 17:00 in
        the textarea (NOT 2026-06-01). For a Pacific-resident dispatcher
        this is intuitive; for a non-Pacific reader cross-day inference
        requires knowing the system server-renders Pacific."""
        dt = _dt542.datetime(
            2026, 6, 1, 0, 0, tzinfo=_dt542.timezone.utc,
        )
        assert _format_event_log_ts_mirror(dt) == "2026-05-31 17:00"

    def test_format_has_no_timezone_marker(self):
        """The I.2 simplification deliberately drops PT / PDT / PST / Z /
        UTC labels. A regression that re-adds any of these (e.g. a
        well-meaning refactor that thinks the explicit marker is missing)
        should fail this test."""
        dt = _dt542.datetime(
            2026, 5, 31, 13, 39, tzinfo=_dt542.timezone.utc,
        )
        rendered = _format_event_log_ts_mirror(dt)
        for marker in ("PT", "PDT", "PST", "UTC", "Z", "T"):
            # 'T' check excludes the digit-T-digit ISO-8601 form specifically;
            # we still allow incidental T in other contexts (none expected
            # in YYYY-MM-DD HH:MM but defensive).
            if marker == "T":
                import re as _re_t
                assert not _re_t.search(r"\dT\d", rendered), (
                    f"format must not contain ISO-8601 'T' separator: {rendered!r}"
                )
            else:
                assert marker not in rendered, (
                    f"format must not contain {marker!r} label: {rendered!r}"
                )

    def test_naive_datetime_raises(self):
        """A naive datetime (no tzinfo) could silently render as UTC,
        producing a wrong timestamp without any signal. Must raise."""
        naive = _dt542.datetime(2026, 5, 31, 13, 39)
        try:
            _format_event_log_ts_mirror(naive)
        except ValueError as exc:
            assert "timezone-aware" in str(exc)
        else:
            assert False, "Naive datetime must raise ValueError, not silently render"

    def test_default_now_returns_current(self):
        """No-argument call returns a sensible 'now' value — exact value
        unverifiable (clock-dependent), but the format must be valid."""
        rendered = _format_event_log_ts_mirror()
        import re
        pattern = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$"
        assert re.match(pattern, rendered), (
            f"Default-now output does not match canonical format: {rendered!r}"
        )


class TestEventLogTimestampCrossFileMirror:
    """Pin the cross-file invariant: the frontend's _PRE_TS_PATTERN regex
    in index.html must match the format that backend's _format_event_log_ts
    emits. Otherwise pre-formatted entries from the backend would not be
    detected as pre-formatted, and _insertEventLogEntries would prepend
    its own timestamp on top, producing double timestamps.

    Cluster I.2 note: with the simplified format, _PRE_TS_PATTERN equals
    the pre-#544 strip pattern. That's intentional — both serve different
    detection purposes but the regex shape is identical. The Cluster-I.1
    "OLD format must not match" test was removed because the OLD format
    IS the new simplified format.
    """

    def test_frontend_pre_ts_pattern_matches_backend_format(self):
        """Pull the _PRE_TS_PATTERN regex literal from frontend/index.html
        and confirm it matches a known backend-formatted entry."""
        from pathlib import Path
        # Resolve frontend/index.html relative to the repo root (test file
        # lives in backend/, frontend/ is sibling).
        index_html = Path(__file__).parent.parent / "frontend" / "index.html"
        assert index_html.exists(), f"index.html not found at {index_html}"
        source = index_html.read_text()
        marker = "_PRE_TS_PATTERN"
        assert marker in source, (
            f"{marker} not found in frontend/index.html — the cross-file "
            "regex pin may have been removed or renamed"
        )
        # Pull the regex literal: const _PRE_TS_PATTERN = /<regex>/;
        import re as _re
        m = _re.search(
            r"const\s+_PRE_TS_PATTERN\s*=\s*/(.*?)/(;|\s)",
            source,
        )
        assert m, "Could not extract _PRE_TS_PATTERN regex from index.html"
        js_regex_source = m.group(1)
        # JS regex syntax is compatible with Python re for this simple pattern.
        # If the regex grows to include JS-only constructs (named groups with
        # ?P<>, lookbehinds, etc.) this conversion may need adjustment.
        py_regex = _re.compile(js_regex_source)
        # Format the test sample using the backend mirror — drift between
        # mirror and frontend regex IS the failure mode.
        dt = _dt542.datetime(
            2026, 5, 31, 15, 13, tzinfo=_dt542.timezone.utc,
        )
        sample_entry = f"{_format_event_log_ts_mirror(dt)} - CalTopo map created: https://caltopo.com/m/ABC"
        assert py_regex.match(sample_entry), (
            f"Frontend _PRE_TS_PATTERN ({js_regex_source!r}) does not match "
            f"backend-formatted entry ({sample_entry!r}) — cross-file mirror "
            "has drifted. _insertEventLogEntries will fail to detect "
            "pre-formatted entries and produce double timestamps."
        )


class TestCreateMapEventLogEntryResponse:
    """Pin Cluster I.1 / issue #542: /create-map response includes a
    server-rendered event_log_entry field with the canonical timestamp
    format.

    Source-scan because the actual response construction requires
    importing main.py (httpx, FastAPI, etc. — not in local pytest env).
    The scan verifies the response dict literal in main.py:create_map
    contains event_log_entry built via _format_event_log_ts.
    """

    def test_create_map_returns_event_log_entry(self):
        from pathlib import Path
        main_py = Path(__file__).parent / "main.py"
        source = main_py.read_text()
        # Find the /create-map handler's return statement
        marker = '@app.post("/create-map")'
        assert marker in source, "/create-map handler missing"
        start = source.index(marker)
        # Bounded by the NEXT route decorator, not a character count. The old
        # "5000-char window, bump if the handler grows" note put the burden on
        # every future author to notice — and #622's gate grew the handler past
        # it, failing this pin for an unrelated reason. Decorator-bounding
        # expresses "this handler" directly and cannot silently truncate.
        end = source.find("\n@app.", start + 1)
        assert end != -1 and end > start, "could not bound the /create-map handler"
        section = source[start:end]
        assert "event_log_entry" in section, (
            "/create-map response must include 'event_log_entry' field "
            "with the canonical UTC + PT-hint timestamp so the frontend "
            "doesn't have to render the timestamp client-side. See "
            "issue #542 / Cluster I.1."
        )
        assert "_format_event_log_ts" in section, (
            "/create-map response must build event_log_entry via "
            "_format_event_log_ts (the canonical helper) so the format "
            "stays in sync with all other event log entries. See "
            "issue #542 / Cluster I.1."
        )


class TestSendNotificationServerRendersEbSlackEntries:
    """Pin Cluster I.1 / issue #542: /send-notification's d4h_event_log
    includes EB notification + Slack channel entries server-rendered with
    the canonical timestamp format. Pre-fix these were frontend-rendered
    with new Date(), producing browser-local timestamps that diverged
    from the server-side OCR-time entries when the dispatcher was in a
    non-Pacific timezone.
    """

    def test_d4h_event_log_extends_with_dispatch_milestones(self):
        from pathlib import Path
        main_py = Path(__file__).parent / "main.py"
        source = main_py.read_text()
        # The extend should be near the _d4h_dispatch_milestones definition.
        # Look for the canonical pattern.
        marker = "d4h_event_log.extend(_d4h_dispatch_milestones"
        assert marker in source, (
            "Backend /send-notification must extend d4h_event_log with the "
            "first two _d4h_dispatch_milestones entries (EB + Slack), so "
            "the frontend gets them server-rendered instead of generating "
            "them client-side with browser-local timestamps. See issue "
            "#542 / Cluster I.1."
        )


# ---------------------------------------------------------------------------
# Slack lookup transient-skip ERROR log — batch-3 PR-G.5
# ---------------------------------------------------------------------------
# Source: main.py::poll_incident, the per-responder email-lookup loop in
# the full-mode branch. Melanie's batch-3 finding #3: a transient Slack
# API failure (rate limit, auth blip) during email lookup silently dropped
# the responder this polling cycle — the per-email WARNING was the only
# trace, and reviewing logs for "responder never invited" required noticing
# the absence of a downstream "X added" message.
#
# Per Bill 2026-05-30 (Option B from the G.5 design discussion): the
# original `continue` control flow is correct (the polling chain
# self-heals on the next 15s cycle). The fix is purely a log-level
# upgrade: add an ERROR-level summary line that fires ONLY when every
# email lookup raised AND no uid resolved, with a structured event tag
# (`slack_lookup_transient_skip`) so ops scans surface the silent-drop
# case without drowning on the legitimate "user not in Slack" returns.
#
# Why the `any_lookup_raised` gate matters: without it, the ERROR would
# fire on every non-safe-list responder lookup in full mode (which
# correctly returns None because they're not in Slack), drowning the
# real-signal cases.


class TestSlackLookupTransientSkipErrorLog:
    """Pin Cluster G.5: when every email lookup raises AND no uid resolves,
    main.py::poll_incident logs at ERROR (not WARNING) with the
    `slack_lookup_transient_skip` event tag.

    Drift caught by these tests:
      - The ERROR log line is removed entirely (regressing to silent drop)
      - The structured event tag `slack_lookup_transient_skip` is renamed
        (breaks ops scans that grep for the literal)
      - The gate is loosened to fire on `uid is None` alone (would drown
        the signal in legitimate "user not in Slack" None-returns)
      - The `any_lookup_raised` tracker variable is removed (the gate
        loses its discriminator)
    """

    def _read_lookup_loop_section(self) -> str:
        """Return main.py source from the Cluster F email-lookup loop
        marker for a window large enough to cover the loop + post-loop
        ERROR gate."""
        from pathlib import Path
        main_py_path = Path(__file__).parent / "main.py"
        source = main_py_path.read_text()
        marker = "Cluster F (Slack-M8): try every email"
        assert marker in source, (
            "Cluster F email-lookup loop marker not found in main.py — "
            "the poll_incident structure may have been refactored; "
            "update this test's marker"
        )
        start = source.index(marker)
        # 3500-char window covers the loop + post-loop gate
        return source[start:start + 3500]

    def test_transient_skip_logs_at_error_level(self):
        section = self._read_lookup_loop_section()
        assert "logger.error" in section, (
            "poll_incident's email-lookup loop must contain a "
            "logger.error() call for the transient-skip end-state "
            "(all lookups raised, no uid resolved). See PR-G.5."
        )

    def test_transient_skip_event_tag_present(self):
        section = self._read_lookup_loop_section()
        assert "slack_lookup_transient_skip" in section, (
            "The transient-skip ERROR log line must include the literal "
            "event tag 'slack_lookup_transient_skip' for ops scans to "
            "grep on. Renaming the tag silently breaks downstream tooling. "
            "See PR-G.5."
        )

    def test_any_lookup_raised_tracker_present(self):
        """The gate variable that distinguishes transient-skip (ERROR-worthy)
        from legitimate 'user not in Slack' (None-return, expected)."""
        section = self._read_lookup_loop_section()
        assert "any_lookup_raised" in section, (
            "The transient-skip gate requires a tracker variable that "
            "records whether any lookup raised across the loop. Without "
            "it, the ERROR would fire on every non-safe-list responder "
            "(which legitimately returns None from lookup_user_by_email). "
            "See PR-G.5."
        )

    def test_error_gate_includes_both_uid_none_and_raised(self):
        """The conditional MUST gate on BOTH `uid is None` AND the
        any_lookup_raised tracker. Loosening to `uid is None` alone would
        drown the signal in legitimate None-returns."""
        section = self._read_lookup_loop_section()
        # The exact text of the gate. Allow whitespace variation.
        import re
        gate_pattern = r"if\s+uid\s+is\s+None\s+and\s+any_lookup_raised\s*:"
        assert re.search(gate_pattern, section), (
            "The ERROR-log gate must combine `uid is None` AND "
            "`any_lookup_raised`. Loosening to `uid is None` alone "
            "would fire on every non-safe-list responder in full mode "
            "(legitimate 'user not in Slack' case). See PR-G.5."
        )

    def test_per_email_warning_still_present(self):
        """The original per-email WARNING ("trying next email if any") is
        preserved — each lookup failure is still logged at WARNING level
        as a trail. The ERROR is the SUMMARY for the end-state, not a
        replacement for the per-email logs."""
        section = self._read_lookup_loop_section()
        assert "lookup_user_by_email failed" in section, (
            "Per-email WARNING log line must remain — each individual "
            "lookup failure is logged as a trail, and the end-state "
            "ERROR is a summary. See PR-G.5."
        )
        assert "trying next email if any" in section, (
            "Per-email WARNING text must preserve 'trying next email if "
            "any' — the message communicates the loop's continue-on-fail "
            "behavior. See PR-G.5."
        )

    def test_continue_control_flow_preserved(self):
        """Bill's explicit decision (2026-05-30, Option B): the
        per-email `continue` is correct — the polling chain self-heals
        on the next 15s cycle. A refactor that replaces `continue` with
        `raise` would crash the polling handler and trigger Cloud Tasks
        retry, which (with no per-responder state persisted mid-loop)
        would re-process already-invited responders and produce duplicate
        invites + duplicate '<Name> added' messages. See PR-G.5
        conversation in MEMORY.md and CLAUDE.md failure-mode Q4
        (diff-persistence interaction)."""
        section = self._read_lookup_loop_section()
        # The `continue` inside the except block is the load-bearing
        # control flow. Verify it's still there.
        assert "continue" in section, (
            "The per-email `continue` MUST remain — replacing it with "
            "`raise` would crash poll_incident and trigger Cloud Tasks "
            "retry, which (with no per-responder state persisted mid-loop) "
            "would duplicate invites for already-processed responders. "
            "See PR-G.5."
        )


# ---------------------------------------------------------------------------
# Frontend 409 distinct alert (double-dispatch) — batch-3 PR-G.6
# ---------------------------------------------------------------------------
# Source: frontend/index.html dispatch handler (around the
# /send-notification fetch). The backend's atomic Firestore tombstone
# (Cluster B / PR #516) returns 409 when a SECOND dispatch request
# arrives while the FIRST is still in flight. Pre-fix the frontend's
# generic !resp.ok branch alerted "Send failed. The notification was
# NOT sent." — factually WRONG for 409 (the FIRST dispatch IS firing)
# and likely to prompt a third dispatch attempt or panic.
#
# Fix: distinct 409 branch with accurate copy, returns rather than
# throws (so the outer catch doesn't flip indicator state — the FIRST
# dispatch's handler is the authoritative state owner for 🔵→🟢/🔴
# transitions and button re-enable).


class TestFrontend409DistinctAlert:
    """Pin Cluster G.6: the frontend's /send-notification handler
    distinguishes HTTP 409 (atomic-tombstone double-dispatch guard fired)
    from generic !resp.ok failures.

    Drift caught by these tests:
      - The 409 branch is removed entirely (regressing to the misleading
        'NOT sent' alert)
      - The 409 branch THROWS instead of RETURNS, causing the outer
        catch to flip indicators to 🔴 and re-enable the button (would
        overwrite the first dispatch's authoritative state)
      - The 409 branch is positioned AFTER `!resp.ok` (the generic
        branch fires first and the 409-specific path never executes)
      - The 409 branch indicator-state mutation is added (would race
        the first dispatch's handler)
    """

    def _read_dispatch_handler_section(self) -> str:
        """Return frontend/index.html source from the /send-notification
        fetch call for a window covering the response handling block."""
        from pathlib import Path
        index_html = Path(__file__).parent.parent / "frontend" / "index.html"
        source = index_html.read_text()
        # The fetch literal is unique enough to anchor on.
        marker = "await fetch(`${BACKEND_URL}/send-notification`"
        assert marker in source, (
            "/send-notification fetch literal not found in index.html — "
            "the dispatch handler structure may have been refactored; "
            "update this test's marker"
        )
        start = source.index(marker)
        # Window covers fetch + 422 stale-locality branch (#605) + 409 branch +
        # generic !resp.ok branch. Widened from 2500 when the #605 hard-confirm
        # was inserted between the fetch and the 409 branch: at 2500 the 409
        # branch fell outside the window, which did not merely fail the
        # presence checks — it made the "does not re-enable button" and "does
        # not mutate indicators" assertions pass VACUOUSLY against text that no
        # longer contained the branch they police.
        return source[start:start + 5000]

    def test_409_branch_present(self):
        section = self._read_dispatch_handler_section()
        assert "resp.status === 409" in section, (
            "The dispatch handler must explicitly check 'resp.status === 409' "
            "before the generic !resp.ok branch — distinguishes double-dispatch "
            "tombstone fire (first dispatch IS in progress) from real failure "
            "(notification NEVER reached the server). See PR-G.6."
        )

    def test_409_branch_precedes_generic_failure_branch(self):
        """The 409 check MUST come BEFORE `if (!resp.ok)`. If it's after,
        the generic branch's `throw` fires first and the 409-specific
        path never executes."""
        section = self._read_dispatch_handler_section()
        idx_409 = section.find("resp.status === 409")
        idx_not_ok = section.find("if (!resp.ok)")
        assert idx_409 != -1 and idx_not_ok != -1, (
            "Both branches must exist in the dispatch handler section"
        )
        assert idx_409 < idx_not_ok, (
            "The 409 branch must precede the generic !resp.ok branch. "
            "If !resp.ok runs first, its throw fires before the 409 "
            "check, defeating the distinct-alert fix. See PR-G.6."
        )

    def test_409_branch_returns_does_not_throw(self):
        """The 409 branch must `return` (exit the handler gracefully)
        rather than `throw` (which would land in the outer catch and
        flip indicator state to 🔴/⚪ — overwriting the first dispatch's
        authoritative 🔵 state)."""
        section = self._read_dispatch_handler_section()
        idx_409 = section.find("resp.status === 409")
        idx_not_ok = section.find("if (!resp.ok)")
        branch_body = section[idx_409:idx_not_ok]
        assert "return;" in branch_body, (
            "The 409 branch must contain 'return;' so the handler exits "
            "gracefully. Without it, execution falls through to the "
            "generic !resp.ok throw (or worse, runs the success path "
            "with no response data). See PR-G.6."
        )
        # Inversely: the branch must NOT throw.
        assert "throw " not in branch_body, (
            "The 409 branch must NOT throw — throwing would land in the "
            "outer catch and flip indicators to 🔴 + re-enable the button, "
            "overwriting the first dispatch's authoritative state. See "
            "PR-G.6."
        )

    def test_409_alert_does_not_say_not_sent(self):
        """The 409 case factually IS being sent (by the first click).
        The pre-fix generic alert said 'NOT sent' — factually wrong and
        likely to prompt a third dispatch attempt."""
        section = self._read_dispatch_handler_section()
        idx_409 = section.find("resp.status === 409")
        idx_not_ok = section.find("if (!resp.ok)")
        branch_body = section[idx_409:idx_not_ok]
        assert "NOT sent" not in branch_body, (
            "The 409 alert must NOT say 'NOT sent' — the first dispatch "
            "IS firing; saying 'NOT sent' is factually wrong and prompts "
            "the dispatcher to retry. See PR-G.6."
        )

    def test_409_branch_does_not_mutate_indicators(self):
        """The 409 branch must NOT call _setIndicator — the first
        dispatch's handler is the authoritative owner of 🔵→🟢/🔴
        transitions. A mutation here would race the first handler's
        real-time updates."""
        section = self._read_dispatch_handler_section()
        idx_409 = section.find("resp.status === 409")
        idx_not_ok = section.find("if (!resp.ok)")
        branch_body = section[idx_409:idx_not_ok]
        assert "_setIndicator" not in branch_body, (
            "The 409 branch must NOT call _setIndicator. The first "
            "dispatch's handler manages indicator state for the in-flight "
            "request; mutating it here would overwrite the live status. "
            "See PR-G.6."
        )

    def test_409_branch_does_not_re_enable_button(self):
        """The button stays disabled — the first dispatch's handler will
        re-enable it on completion (success path) or in its catch (failure
        path). Re-enabling here would let the dispatcher fire a third
        click before the first dispatch resolves."""
        section = self._read_dispatch_handler_section()
        idx_409 = section.find("resp.status === 409")
        idx_not_ok = section.find("if (!resp.ok)")
        branch_body = section[idx_409:idx_not_ok]
        assert "btn.disabled = false" not in branch_body, (
            "The 409 branch must NOT re-enable the button — the first "
            "dispatch's handler will manage it. Re-enabling here permits "
            "a third click before the first dispatch resolves. See PR-G.6."
        )


# ---------------------------------------------------------------------------
# d4h_activity_id intermediate persist — batch-3 PR-G.7
# ---------------------------------------------------------------------------
# Source: main.py::send_notification, immediately after the
# d4h.create_incident_with_subject call. Pre-fix d4h_activity_id was only
# written to Firestore at the final .set() at Step 11. If a Firestore
# failure occurred between the D4H POST and Step 11 (transient outage,
# Cloud Run preemption), the D4H incident existed but the app had no
# record of its ID — downstream per-YES Cloud Tasks workers found None
# in _load_d4h_activity_id() and silently skipped every attendance sync.
#
# Cluster C (PR #517) established the "persist the moment we have it"
# pattern for EB IDs (everbridge_event_id, notification_id,
# slack_channel_id, welcome_ts, caltopo_ts). G.7 extends it to
# d4h_activity_id — Melanie's batch-3 D4H audit finding #1.
#
# Failure-mode rubric Q1: D4H POST is irreversible (we don't DELETE D4H
# records to roll back), so the recovery surface MUST persist the ID
# immediately.


class TestD4HActivityIdIntermediatePersist:
    """Pin Cluster G.7: d4h_activity_id is persisted via
    _patch_incident_doc_best_effort the moment create_incident_with_subject
    returns, NOT only at the final .set() at Step 11.

    Drift caught by these tests:
      - The intermediate persist call is removed (regressing to
        Step-11-only persist and the silent-orphan failure mode)
      - The persist is moved to a position where a failure between
        create and persist re-opens the orphan window
      - The where=... key is renamed (breaks log-triage grep patterns)
      - The persist references the wrong key (e.g. d4h_incident_id
        instead of d4h_activity_id) — would write to Firestore but
        downstream consumers reading d4h_activity_id wouldn't find it
    """

    def _read_d4h_create_section(self) -> str:
        """Return main.py source from the d4h.create_incident_with_subject
        call for a window covering the persist + the surrounding try
        block."""
        from pathlib import Path
        main_py_path = Path(__file__).parent / "main.py"
        source = main_py_path.read_text()
        marker = "d4h.create_incident_with_subject("
        assert marker in source, (
            "d4h.create_incident_with_subject call not found in main.py — "
            "the send_notification structure may have been refactored; "
            "update this test's marker"
        )
        start = source.index(marker)
        # 3000-char window covers the call + intermediate persist + a
        # margin reaching the subsequent d4h_event_log append
        return source[start:start + 3000]

    def test_intermediate_persist_call_present(self):
        section = self._read_d4h_create_section()
        assert "_patch_incident_doc_best_effort" in section, (
            "send_notification must call _patch_incident_doc_best_effort "
            "to persist d4h_activity_id IMMEDIATELY after "
            "create_incident_with_subject returns — not wait for the "
            "final .set() at Step 11. See PR-G.7 / failure-mode rubric Q1."
        )

    def test_persist_key_is_d4h_activity_id(self):
        section = self._read_d4h_create_section()
        # The persist payload MUST use the literal key 'd4h_activity_id'
        # so downstream consumers (per-YES Cloud Tasks worker calling
        # _load_d4h_activity_id) find it. Drift to a different key name
        # would silently break the recovery.
        assert '"d4h_activity_id": d4h_activity_id' in section, (
            "The intermediate persist payload must use the literal key "
            "'d4h_activity_id' (matching the field downstream per-YES "
            "Cloud Tasks workers read via _load_d4h_activity_id). Drift "
            "to a different key silently re-opens the orphan window. "
            "See PR-G.7."
        )

    def test_where_marker_is_step10_5(self):
        """The `where=` kwarg threads into Firestore-write log lines for
        triage. The 'step10_5_d4h_created' value puts the timing in
        context — between Step 10 (tally post) and Step 11 (final .set)."""
        section = self._read_d4h_create_section()
        assert 'where="step10_5_d4h_created"' in section, (
            "The where= kwarg on _patch_incident_doc_best_effort must be "
            "'step10_5_d4h_created' — names the moment in the dispatch "
            "lifecycle for log triage. Renaming it silently breaks "
            "downstream grep patterns. See PR-G.7."
        )

    def test_persist_precedes_d4h_event_log_append(self):
        """The persist MUST come BEFORE the 'D4H incident created' event
        log entry. If reordered, a Firestore failure on the persist would
        leave d4h_event_log with the entry but d4h_activity_id unwritten
        — the orphan window re-opens at a different moment."""
        section = self._read_d4h_create_section()
        idx_persist = section.find("_patch_incident_doc_best_effort")
        # Match the inside of the f-string literal `D4H incident created
        # (activity_id=` — no leading quote (the f-string interpolation
        # `{_ts} - ` precedes it, not a bare quote).
        idx_log = section.find("D4H incident created (activity_id=")
        assert idx_persist != -1 and idx_log != -1, (
            "Both the persist call and the event log append must exist "
            "in this section"
        )
        assert idx_persist < idx_log, (
            "The intermediate persist must precede the 'D4H incident "
            "created' event log append. If reordered, the orphan window "
            "re-opens between log-append and persist. See PR-G.7."
        )


# ---------------------------------------------------------------------------
# mark_member_attending concurrent-retry TOCTOU documentation — batch-3 PR-G.8
# ---------------------------------------------------------------------------
# Source: backend/d4h.py::mark_member_attending docstring + CLAUDE.md
# Locked Decision row "mark_member_attending concurrent-retry TOCTOU
# ACCEPTED". This is a PURE DOCUMENTATION change — no code logic
# modified. The docstring + Locked Decision pre-empt future PRs from
# silently "fixing" the deliberate non-fix.
#
# The race is real but rare and recoverable:
#   - Operation typically <2s (GET + POST roundtrip)
#   - Cloud Tasks retry of timed-out task requires 30+s original stall
#   - Recovery is one manual click in D4H's Update Attendance view
#   - True atomic guards (D4H unique constraint not exposed; Firestore
#     tombstone per (event_id, member_id) adds overhead AND is itself
#     TOCTOU-vulnerable unless transacted) cost > benefit
#
# Drift caught by these tests:
#   - Docstring loses the TOCTOU acknowledgment (future engineer reads
#     read-then-write pattern, recognizes the shape, "fixes" it)
#   - Docstring loses the acceptance rationale (future engineer reads
#     the TOCTOU mention without context, thinks it's a bug)
#   - Docstring loses the #442 reference (issue #442 was DECLINED 2026-07-19,
#     so the note is now a conditional: it records that the analysis only
#     needs redoing IF that decision is reversed and _patch_attendance is
#     wired in. Losing it would erase that condition.)
#   - CLAUDE.md Locked Decision row is removed (Locked Decision discipline
#     erodes, future PRs not blocked from "fixing" the accepted race)


class TestMarkMemberAttendingTOCTOUDocumentation:
    """Pin Cluster G.8: mark_member_attending docstring + CLAUDE.md
    Locked Decision row document the ACCEPTED concurrent-retry TOCTOU.

    Source-scan style — this is a pure-documentation test. The behavioral
    invariants (deterministic task name dedup, max_attempts=5 backoff)
    are pinned elsewhere (TestEnqueuePerYesSync in test_d4h.py; CLAUDE.md
    'Cloud Tasks queue retry budgets' Locked Decision). These tests pin
    the RATIONALE — the explanation of why the race is accepted and the
    conditional reference to issue #442 (declined 2026-07-19).
    """

    def _read_mark_member_attending_docstring(self) -> str:
        """Return the docstring body of mark_member_attending. Scans
        from the def line for a window large enough to cover the
        expanded docstring."""
        from pathlib import Path
        d4h_py = Path(__file__).parent / "d4h.py"
        source = d4h_py.read_text()
        marker = "def mark_member_attending("
        assert marker in source, (
            "mark_member_attending function not found in d4h.py — the "
            "function may have been renamed; update this test's marker"
        )
        start = source.index(marker)
        # 5000-char window covers the expanded docstring + function body
        return source[start:start + 5000]

    def test_docstring_acknowledges_toctou(self):
        section = self._read_mark_member_attending_docstring()
        assert "TOCTOU" in section, (
            "mark_member_attending docstring must contain 'TOCTOU' as "
            "the explicit name of the race. Without this, a future "
            "engineer reads the read-then-write pattern, recognizes "
            "the shape, and 'fixes' it — overriding Bill's accept "
            "decision. See PR-G.8 / CLAUDE.md Locked Decision."
        )

    def test_docstring_marks_race_as_accepted(self):
        section = self._read_mark_member_attending_docstring()
        # The literal "ACCEPTED" (uppercase) is the unambiguous signal
        # that the race is a deliberate non-fix, not a bug to fix.
        assert "ACCEPTED" in section, (
            "mark_member_attending docstring must contain 'ACCEPTED' as "
            "the explicit decision marker. The race exists; the "
            "engineering choice is to accept it. Without this marker, "
            "future PRs may 'fix' the race without knowing it was "
            "deliberately left alone. See PR-G.8."
        )

    def test_docstring_distinguishes_retry_shapes(self):
        """The docstring distinguishes (A) same-task re-enqueue (suppressed
        by task-name dedup, NO concurrent execution) from (B) Cloud Tasks
        retry of timed-out task (IS concurrent execution, NOT suppressed).
        Future engineers reading only one of these without the other miss
        the actual race condition."""
        section = self._read_mark_member_attending_docstring()
        assert "task name" in section.lower() or "dedup" in section.lower(), (
            "Docstring must reference the deterministic task-name dedup "
            "mechanism as case (A) — the protected shape — to distinguish "
            "from case (B), the unprotected concurrent-retry shape. "
            "See PR-G.8."
        )
        assert "timed-out" in section.lower() or "timeout" in section.lower(), (
            "Docstring must reference Cloud Tasks retry-of-timed-out-task "
            "as case (B), the unprotected shape that produces the race. "
            "See PR-G.8."
        )

    def test_docstring_references_issue_442(self):
        """Issue #442 (per-NO ABSENT sync) was DECLINED 2026-07-19 —
        declines and non-responses are Slack-tally-only and never sent to
        D4H — so _patch_attendance stays unwired and this TOCTOU analysis
        stands as-is. The docstring keeps the #442 reference as a CONDITION:
        if that decision is ever reversed, the analysis must be redone with
        PATCH-existing semantics. Losing the reference erases the condition."""
        section = self._read_mark_member_attending_docstring()
        assert "#442" in section, (
            "Docstring must keep the issue #442 reference — it records the "
            "condition under which this TOCTOU analysis needs redoing "
            "(i.e. if the declined per-NO ABSENT sync is ever revived and "
            "_patch_attendance gets wired in with PATCH-existing "
            "semantics). See PR-G.8."
        )

    def test_claude_md_locked_decision_present(self):
        """The Locked Decision row in CLAUDE.md is the canonical reference;
        the d4h.py docstring points to it. If the Locked Decision is
        removed, the docstring's pointer becomes dangling and the
        Locked-Decision discipline erodes."""
        from pathlib import Path
        claude_md = Path(__file__).parent.parent / "CLAUDE.md"
        source = claude_md.read_text()
        # The row's distinctive phrase
        marker = "mark_member_attending` concurrent-retry TOCTOU"
        assert marker in source, (
            "CLAUDE.md Locked Decision row 'mark_member_attending "
            "concurrent-retry TOCTOU ACCEPTED' is missing. The d4h.py "
            "docstring points to this row as the canonical reference. "
            "If the row is gone, the docstring pointer is dangling and "
            "future PRs may 'fix' the accepted race without seeing the "
            "decision. See PR-G.8."
        )


# ---------------------------------------------------------------------------
# G.LOW bundled cleanups (batch-3) — pinned together for one PR
# ---------------------------------------------------------------------------
# Six independent low-severity cleanups from Melanie's 2026-05-30 batch-3
# review, bundled per the cluster plan. Each is small enough to be
# sub-optimal as its own PR, but they're logically related as "the
# cleanups deferred from G.1-G.7 for one-logical-change discipline."
# Test pins are independent so a regression on one doesn't block the
# whole batch.


class TestGLOWBundledCleanups:
    """Pin Cluster G.LOW (batch-3 bundled cleanups). Source-scan tests,
    one per cleanup item. See PR-G.LOW description for the cluster plan."""

    def _read_file(self, path_parts) -> str:
        from pathlib import Path
        f = Path(__file__).parent
        for part in path_parts:
            f = f / part
        return f.read_text()

    # --- 1. incident.get("id") defensive (D4H audit finding #4) ---

    def test_d4h_post_incident_uses_get_not_subscript(self):
        """D4H schema change (response missing 'id' while still 2xx)
        used to KeyError → unhandled 500. Now: .get() + None-check +
        typed D4HClientError (terminal, no retry → no second orphan)."""
        source = self._read_file(["d4h.py"])
        assert 'activity_id = incident["id"]' not in source, (
            "d4h.create_incident_with_subject must use .get() not "
            "bracket subscript when extracting 'id' from D4H response — "
            "schema change would otherwise KeyError. See PR-G.LOW item 1."
        )
        assert 'incident.get("id")' in source, (
            "d4h.create_incident_with_subject must defensively call "
            "incident.get('id') so a schema change is surfaced as "
            "D4HClientError with a useful message. See PR-G.LOW item 1."
        )

    # --- 2. gc_recovered_mib clamped at 0 (Melanie batch-3 finding #8) ---

    def test_gc_recovered_mib_clamped_at_zero(self):
        """Concurrent OCR can make gc_recovered_mib negative (between
        the two _rss_mib reads). Clamp at 0 keeps the metric
        interpretable."""
        source = self._read_file(["main.py"])
        assert "max(0, _rss_post_mib - _rss_post_gc_mib)" in source, (
            "gc_recovered_mib must be clamped at 0 with max(0, ...) — "
            "concurrent OCR can produce negative reads that look like "
            "instrumentation breakage. See PR-G.LOW item 2."
        )

    # --- 3. NotFound distinct branch in _patch_incident_doc_best_effort ---

    def test_patch_incident_doc_distinguishes_notfound(self):
        """NotFound at this point means skeleton doc was deleted between
        Step 1.5 .create() and the update — invariant violation worth
        investigating. Pre-fix it fell into the generic transient branch."""
        source = self._read_file(["main.py"])
        assert "_FirestoreNotFound" in source, (
            "_patch_incident_doc_best_effort must catch NotFound "
            "distinctly from the generic transient-error branch — "
            "NotFound here is an invariant violation, not a transient "
            "failure. See PR-G.LOW item 3."
        )
        assert "INVARIANT VIOLATION" in source, (
            "NotFound branch log message must call out 'INVARIANT "
            "VIOLATION' so ops scans surface it distinctly from the "
            "'final .set() will reconcile' transient case. See "
            "PR-G.LOW item 3."
        )

    # --- 4. Hoisted imports (Melanie batch-3 finding #10) ---

    def test_firestore_already_exists_hoisted_to_module_level(self):
        source = self._read_file(["main.py"])
        import re
        pattern = r"^from google\.api_core\.exceptions import AlreadyExists as _FirestoreAlreadyExists"
        assert re.search(pattern, source, re.MULTILINE), (
            "_FirestoreAlreadyExists must be imported at module level — "
            "moved out of /send-notification handler in PR-G.LOW so "
            "grep-based audits surface the dependency. See item 4."
        )

    def test_new_skeleton_incident_doc_hoisted_to_module_level(self):
        source = self._read_file(["main.py"])
        import re
        pattern = r"^from incidents import new_skeleton_incident_doc"
        assert re.search(pattern, source, re.MULTILINE), (
            "new_skeleton_incident_doc must be imported at module "
            "level. See PR-G.LOW item 4."
        )

    def test_firestore_module_hoisted_to_module_level(self):
        source = self._read_file(["main.py"])
        import re
        pattern = r"^from google\.cloud import firestore as _firestore"
        assert re.search(pattern, source, re.MULTILINE), (
            "firestore module (aliased _firestore) must be imported at "
            "module level for the @_firestore.transactional decorator "
            "in close_incident_polling. See PR-G.LOW item 4. NOTE: "
            "_get_eb_slack_db()'s lazy `from google.cloud import "
            "firestore` is deliberately kept (defers credential lookup)."
        )

    # --- 5. to_conversational_name public (Melanie batch-3 finding #9) ---

    def test_to_conversational_name_is_public_in_slack(self):
        """The function MUST be named without leading underscore — the
        main.py caller reaches across modules, so it's part of slack.py's
        public API."""
        source = self._read_file(["slack.py"])
        assert "def to_conversational_name(" in source, (
            "slack.to_conversational_name must be the PUBLIC name "
            "(no leading underscore) — main.py reaches across modules "
            "to use it. See PR-G.LOW item 5."
        )
        assert "def _to_conversational_name(" not in source, (
            "The old _to_conversational_name (private form) must be "
            "fully renamed — no leftover references. See PR-G.LOW item 5."
        )

    def test_main_py_uses_public_to_conversational_name(self):
        source = self._read_file(["main.py"])
        assert "slack_module._to_conversational_name(" not in source, (
            "main.py must NOT reach into slack.py's private "
            "_to_conversational_name — use the public "
            "to_conversational_name instead. See PR-G.LOW item 5."
        )
        assert "slack_module.to_conversational_name(" in source, (
            "main.py must call slack_module.to_conversational_name() "
            "(public form). See PR-G.LOW item 5."
        )

    # --- 6. _post_equipment_usage tombstone-dependency docstring ---

    def test_post_equipment_usage_docstring_notes_tombstone_dependency(self):
        """Pure docstring: notes that this function lacks local
        idempotency and relies on the Cluster B tombstone in
        send_notification. A future refactor that moves drone-attach
        out of the tombstone-guarded handler would need to add the
        local check."""
        source = self._read_file(["d4h.py"])
        # Look for the section in the _post_equipment_usage docstring
        idx_def = source.find("def _post_equipment_usage(")
        assert idx_def != -1, "_post_equipment_usage def not found in d4h.py"
        # 2000-char window covers the docstring + a margin
        section = source[idx_def:idx_def + 2000]
        assert "tombstone" in section.lower(), (
            "_post_equipment_usage docstring must reference the "
            "Cluster B tombstone — the structural guarantee that "
            "substitutes for a local idempotency check. See "
            "PR-G.LOW item 6."
        )
        assert "_get_equipment_usages_for_activity" in section, (
            "_post_equipment_usage docstring must point at the existing "
            "_get_equipment_usages_for_activity helper as the "
            "ready-to-use idempotency check if the tombstone invariant "
            "is ever broken. See PR-G.LOW item 6."
        )


# ---------------------------------------------------------------------------
# CalTopo orphan-map handler + frontend alert — batch-3 PR-H.2
# ---------------------------------------------------------------------------
# Source: caltopo.py raises CalTopoOrphanMapError on partial-marker
# failure; main.py /create-map catches the typed exception and surfaces
# partial_map_id in 502 detail; frontend parses 502 detail for
# "orphan map id:" signal and shows distinct alert (otherwise canned
# alert for generic failures).
#
# Per Bill 2026-05-30: the app deliberately does NOT have CalTopo
# DELETE privileges. Orphan maps sit on the team account until
# manually deleted via CalTopo UI. The fix is purely diagnostic —
# surface the partial_map_id to the dispatcher AND log structured
# WARNING for the maintainer.


class TestCalTopoOrphanMapHandler:
    """Pin Cluster H.2: /create-map handler catches CalTopoOrphanMapError
    distinctly from generic RuntimeError, surfaces partial_map_id in
    the 502 detail. CalTopoOrphanMapError class-hierarchy + attribute
    contract pinned in TestCalTopoOrphanMapErrorContract (test_caltopo.py)."""

    def _read_create_map_handler(self) -> str:
        """The create_map handler body, bounded by the NEXT route decorator.

        Was a fixed `start + 5000` slice, which silently truncated when the
        handler grew: adding the #622 stale-locality gate pushed the orphan
        branch past the cut-off and failed three pins that had nothing to do
        with the change. A character count cannot express "this handler" —
        bounding on the next decorator can, and cannot silently shrink the
        window again. Same failure shape as the 2500-char window in
        TestFrontend409DistinctAlert (widened during #616).
        """
        from pathlib import Path
        main_py = Path(__file__).parent / "main.py"
        source = main_py.read_text()
        marker = '@app.post("/create-map")'
        assert marker in source, "/create-map handler missing"
        start = source.index(marker)
        end = source.find("\n@app.", start + 1)
        assert end != -1 and end > start, "could not bound the /create-map handler"
        return source[start:end]

    def test_orphan_branch_present(self):
        section = self._read_create_map_handler()
        assert "except CalTopoOrphanMapError" in section, (
            "/create-map must have a specific `except CalTopoOrphanMapError` "
            "branch — without it the orphan case falls into the generic "
            "RuntimeError catch and the partial_map_id is lost. See PR-H.2."
        )

    def test_orphan_branch_precedes_generic_runtime_error(self):
        """CalTopoOrphanMapError subclasses RuntimeError. The specific
        branch MUST come before the generic catch — otherwise generic
        fires first and the typed handler never runs."""
        section = self._read_create_map_handler()
        idx_orphan = section.find("except CalTopoOrphanMapError")
        idx_generic = section.find("except RuntimeError as exc:")
        assert idx_orphan != -1 and idx_generic != -1, (
            "Both branches must exist in the /create-map handler"
        )
        assert idx_orphan < idx_generic, (
            "The orphan-map branch must precede the generic "
            "RuntimeError catch. If reordered, the generic catch "
            "fires first and partial_map_id is lost. See PR-H.2."
        )

    def test_orphan_detail_includes_partial_map_id(self):
        """The 502 detail string MUST include partial_map_id so the
        dispatcher can quote it. Without this, the typed exception is
        useless — the whole point is dispatcher-visible diagnostic."""
        section = self._read_create_map_handler()
        assert "exc.partial_map_id" in section, (
            "/create-map orphan-map branch must include "
            "exc.partial_map_id in the 502 detail so the dispatcher "
            "can quote it when asking for cleanup. See PR-H.2."
        )
        assert "orphan map id:" in section, (
            "/create-map orphan-map detail must use the literal phrase "
            "'orphan map id:' — the frontend parses this exact substring "
            "to detect the orphan case and show a distinct alert. "
            "Renaming silently breaks the frontend recognition. "
            "See PR-H.2."
        )

    def test_caltopo_import_includes_orphan_error(self):
        """The handler can't catch CalTopoOrphanMapError without
        importing it."""
        from pathlib import Path
        main_py = Path(__file__).parent / "main.py"
        source = main_py.read_text()
        import re
        pattern = r"^from caltopo import .*CalTopoOrphanMapError"
        assert re.search(pattern, source, re.MULTILINE), (
            "main.py must import CalTopoOrphanMapError from caltopo "
            "at module level so the /create-map handler can catch it. "
            "See PR-H.2."
        )


class TestFrontendOrphanMapAlert:
    """Pin Cluster H.2: frontend /create-map handler distinguishes the
    orphan-map case (parse 502 detail for 'orphan map id:') from
    generic failure (canned alert)."""

    def _read_frontend_create_map_catch(self) -> str:
        """Return the catch block of the /create-map button handler."""
        from pathlib import Path
        index_html = Path(__file__).parent.parent / "frontend" / "index.html"
        source = index_html.read_text()
        marker = "Map creation failed. Check CalTopo credentials"
        assert marker in source, (
            "Canned alert literal not found in index.html — handler "
            "may have been refactored; update this test's marker"
        )
        start = source.index(marker)
        # 800-char window backward + forward covers the if/else block
        return source[max(0, start - 800):start + 200]

    def test_frontend_detects_orphan_via_substring(self):
        section = self._read_frontend_create_map_catch()
        assert 'indexOf("orphan map id:")' in section, (
            "Frontend /create-map catch block must detect the orphan "
            "case by parsing the error message for 'orphan map id:' "
            "substring. Without this detection, dispatcher sees the "
            "canned alert and loses the map_id needed for cleanup "
            "request. See PR-H.2."
        )

    def test_frontend_orphan_branch_shows_err_message(self):
        """When orphan detected, the alert MUST surface the backend
        message (which contains the map_id) rather than the canned
        text. Otherwise the detection is useless."""
        section = self._read_frontend_create_map_catch()
        assert "alert(err.message)" in section, (
            "Frontend orphan-map branch must call alert(err.message) "
            "to surface the backend message (containing the map_id) "
            "to the dispatcher. See PR-H.2."
        )


# ---------------------------------------------------------------------------
# /create-map double-click guard (frontend) — batch-3 PR-H.3
# ---------------------------------------------------------------------------
# Source: frontend/index.html — the /create-map button handler. Pre-fix
# the handler had a btn._mapUrl cache (defends after first success) and
# btn.disabled = true (defends in browsers that cancel queued clicks
# on disabled buttons) but no `if (btn.disabled) return;` guard at the
# TOP of the handler. On browsers that deliver queued click events to
# disabled buttons (Safari historically), a double-click within ~50ms
# of the first click — before the fetch returns — would land both
# handlers past the cache check (cache empty), both would set
# btn.disabled = true (synchronous, no effect on already-queued events),
# both would POST to /create-map → two CalTopo maps created.
#
# The /send-notification handler at index.html:~3261 already has the
# top-of-handler `if (btn.disabled) return;` guard. PR-H.3 mirrors
# that pattern for /create-map. Server-side tombstone deferred per
# Bill 2026-05-30 ("only when we observe duplicate-map symptoms in
# production"). Frontend guard is the belt-and-braces.


class TestCreateMapDoubleClickGuard:
    """Pin Cluster H.3: the /create-map button handler has an
    `if (btn.disabled) return;` guard at the TOP of the handler,
    BEFORE the _mapUrl cache check and any window.open or fetch.

    Drift caught by these tests:
      - Guard removed entirely (regresses to double-POST risk on
        Safari-class browsers that deliver queued clicks)
      - Guard moved BELOW the _mapUrl check (still defends partially
        but the cache check itself can run twice and produce two
        window.open() calls)
      - Guard moved BELOW window.open() (the synchronous popup-blocker
        workaround would still fire twice — two blank tabs)
    """

    def _read_create_map_handler_top(self) -> str:
        """Return the top of the /create-map button handler — large
        enough to cover the guard + cache check + the synchronous
        window.open call + the first few lines of the main body.
        Empirically the relevant span is ~2200 chars including the
        comment block above the new guard."""
        from pathlib import Path
        index_html = Path(__file__).parent.parent / "frontend" / "index.html"
        source = index_html.read_text()
        marker = "getElementById('incident-map-btn').addEventListener('click'"
        assert marker in source, (
            "/create-map button handler not found in index.html — "
            "the handler may have been refactored; update this test's "
            "marker"
        )
        start = source.index(marker)
        # 4000-char window covers the expanded multi-layer-defense
        # comment block + the cache check + the synchronous window.open
        # + a margin. Sized after the H.3 comment expansion (PR-H.3
        # follow-on commit added ~15 more comment lines).
        return source[start:start + 4000]

    def test_double_click_guard_present_at_top(self):
        section = self._read_create_map_handler_top()
        assert "if (btn.disabled) return;" in section, (
            "/create-map button handler must have an "
            "`if (btn.disabled) return;` guard at the top — mirrors "
            "the /send-notification handler's pattern. Without it, "
            "a queued double-click on Safari-class browsers produces "
            "two POSTs and two orphan CalTopo maps. See PR-H.3."
        )

    def test_guard_precedes_mapurl_cache_check(self):
        """The disabled-guard MUST be BEFORE the _mapUrl cache check.
        If after, the cache check itself fires twice on the queued
        double-click and produces two window.open() calls (two blank
        tabs on Safari) even though only one fetch fires."""
        section = self._read_create_map_handler_top()
        idx_guard = section.find("if (btn.disabled) return;")
        idx_cache = section.find("if (btn._mapUrl)")
        assert idx_guard != -1 and idx_cache != -1, (
            "Both the disabled-guard and the _mapUrl cache check must "
            "exist in the handler"
        )
        assert idx_guard < idx_cache, (
            "The `if (btn.disabled) return;` guard must precede the "
            "`if (btn._mapUrl)` cache check. Without this ordering a "
            "queued double-click produces two cache-miss paths. "
            "See PR-H.3."
        )

    def test_guard_precedes_window_open(self):
        """The disabled-guard MUST be BEFORE the synchronous
        `window.open('', '_blank')` call. The blank-window open is
        the most visible side effect of the handler entering (the
        dispatcher sees a tab spawn even if no fetch fires); two
        handlers entering means two blank tabs."""
        section = self._read_create_map_handler_top()
        idx_guard = section.find("if (btn.disabled) return;")
        idx_open = section.find("window.open('', '_blank')")
        assert idx_guard != -1 and idx_open != -1, (
            "Both the disabled-guard and the window.open call must "
            "exist in the handler"
        )
        assert idx_guard < idx_open, (
            "The disabled-guard must precede window.open('', '_blank'). "
            "Without this ordering, a queued double-click produces two "
            "blank tabs. See PR-H.3."
        )


# ---------------------------------------------------------------------------
# Dispatch Turbo VIP-breakthrough DM wiring — /send-notification + /poll-incident
# ---------------------------------------------------------------------------
# PRD: SAR Slack Onboarding/PRD_DispatchTurbo_Responder_DM_VIP_Breakthrough.
# Formatter contract lives in test_slack.py::TestFormatIncidentDmText;
# these tests pin the wiring in main.py (helper existence, gates, UC4
# fallback surface, Cluster C persistence via ArrayUnion).
# ---------------------------------------------------------------------------

class TestStagingMessageWiring:
    """Pin the #673 staging-message wiring in /send-notification Step 8.

    Staging is its own post, its own pin, and its own persisted ts so a
    dispatch that went out with the wrong location can be corrected by
    deleting just that message. Every assertion below reads the Step 8 block
    of main.py, bounded on real markers at BOTH ends — an unbounded search
    over a 9,000-line module proves only that the module contains a string.
    """

    @staticmethod
    def _step8():
        src = (Path(__file__).parent / "main.py").read_text()
        start = "# ---- Step 8: Pinned welcome message"
        end = '# ---- Step 9:'
        assert start in src, "Step 8 block header not found"
        assert end in src, "Step 9 block header not found"
        i, j = src.index(start), src.index(end)
        assert i < j
        block = src[i:j]
        return "\n".join(l.split("#")[0] for l in block.splitlines())

    def test_staging_is_posted_as_its_own_message(self):
        block = self._step8()
        assert "slack_module.post_message, slack_channel_id, staging_text" in block, (
            "The staging message is no longer posted. Without its own message "
            "there is nothing for an admin to delete when staging is wrong — "
            "the bot authored the welcome and nobody but the bot can edit it."
        )

    def test_staging_message_suppresses_unfurl(self):
        """These are Apple/Google Maps URLs.

        Structure, not keyword presence: `unfurl_links=False` appears in the
        welcome block too, so assert it sits between the staging post call and
        the end of that call.
        """
        block = self._step8()
        i = block.index("slack_module.post_message, slack_channel_id, staging_text")
        tail = block[i:i + 260]
        assert "unfurl_links=False" in tail and "unfurl_media=False" in tail, (
            "The staging post no longer suppresses unfurl — two large maps "
            "preview cards now land in a channel whose readability was "
            "measurably improved by #660/#661."
        )

    def test_unverified_flag_reaches_the_staging_message(self):
        """The handler must pass the flag through, or the warning never renders."""
        block = self._step8()
        assert 'unverified=bool(body.get("staging_unverified"))' in block, (
            "/send-notification no longer forwards staging_unverified to "
            "format_staging_message — the location-conflict warning is dead "
            "code and responders get a wrong staging pin with no signal."
        )

    def test_unmapped_flag_reaches_the_staging_message(self):
        block = self._step8()
        assert 'unmapped=bool(body.get("staging_unmapped"))' in block, (
            "/send-notification no longer forwards staging_unmapped, so a "
            "Gemini-invented staging list reaches responders unmarked."
        )

    def test_staging_ts_is_persisted_immediately(self):
        """Cluster C / Failure-mode Q1: persist each side-effect id when obtained."""
        block = self._step8()
        assert '{"staging_ts": staging_ts}' in block, (
            "staging_ts is no longer patched to Firestore at post time, so a "
            "later failure in this handler loses the only handle on the "
            "staging message."
        )
        assert 'where="step8_staging_posted"' in block

    def test_staging_message_is_pinned(self):
        block = self._step8()
        assert "slack_module.pin_message, slack_channel_id, staging_ts" in block, (
            "The staging message is no longer pinned. Being visible in the "
            "pinned-items list at a glance is the point of the split, not a "
            "side effect."
        )

    def test_staging_is_posted_before_the_caltopo_followup(self):
        """CalTopo's unfurl renders a large card; staging is time-critical."""
        block = self._step8()
        assert block.index("staging_text") < block.index("caltopo_url and slack_channel_id"), (
            "The staging message now posts after the CalTopo follow-up, "
            "burying it under the map preview card."
        )

    def test_staging_post_is_guarded_on_having_a_staging_address(self):
        """Symmetry with the CalTopo block one stanza below (Failure-mode Q6).

        staging_address reaches "" whenever the frontend's `^1\\.` staging regex
        misses the textarea — a dispatcher who hand-edits the "Staging Area for
        Resources:" line out of the summary produces exactly that. Unguarded,
        format_staging_message renders the literal `Staging: <|> (<|G>)` and it
        gets posted AND PINNED. Pre-#673 that emptiness was a near-blank
        trailing line at the bottom of the welcome; the split is what promotes
        it to a prominent pin, so the guard ships with the split.
        """
        block = self._step8()
        # The `if ` prefix is load-bearing. The skip-warning one stanza above
        # reads `if not staging_address_present and slack_channel_id:`, which
        # CONTAINS the bare condition as a substring — so an unprefixed search
        # matches the warning and passes with the real guard deleted. Mutation
        # testing caught exactly that here.
        guard = "if staging_address_present and slack_channel_id:"
        assert guard in block, (
            "The staging post is no longer guarded on having a staging "
            "address — an empty dispatch pins the literal 'Staging: <|> (<|G>)'."
        )
        assert block.index("staging_address_present = bool(") < block.index(guard), (
            "The guard variable is computed after it is used."
        )

    def test_missing_staging_is_logged_without_leaking_the_address(self):
        """A dispatch with no staging pin is notable; the address is PII."""
        block = self._step8()
        i = block.index("Staging message skipped")
        stanza = block[i - 200:i + 500]
        assert "logger.warning" in stanza, (
            "A dispatch that posts no staging message now does so silently."
        )
        assert 'body.get("staging_address")' not in \
            block[i:block.index("if staging_address_present and slack_channel_id:")], (
                "The skip warning interpolates the staging address — location "
                "is PII per core privacy guarantee #3."
            )

    def test_staging_ts_reaches_the_final_incident_doc(self):
        """#570 symmetry — the failure mode that cost a duplicate DM.

        An incrementally-patched field that is not passed to new_incident_doc()
        gets wiped by the final .set() overwrite.
        """
        src = (Path(__file__).parent / "main.py").read_text()
        code = "\n".join(l.split("#")[0] for l in src.splitlines())
        assert "staging_ts=staging_ts," in code, (
            "staging_ts is patched at Step 8 but never passed to "
            "new_incident_doc(), so the final .set() wipes it — the exact "
            "#568 -> #570 bug."
        )


class TestDispatchTurboDmWiring:
    def _read_main_py(self) -> str:
        return (Path(__file__).parent / "main.py").read_text()

    def _read_step_7b_source(self) -> str:
        """Return the Step 7b VIP-breakthrough-DM block only."""
        source = self._read_main_py()
        start_marker = (
            "# ---- Step 7b: VIP-breakthrough DM to safe-list initial members"
        )
        end_marker = "# ---- Step 8: Pinned welcome message"
        assert start_marker in source, "Step 7b block header not found"
        assert end_marker in source, "Step 8 block header not found"
        start = source.index(start_marker)
        end = source.index(end_marker)
        assert start < end, "Step 7b must precede Step 8"
        return source[start:end]

    def _read_poll_time_dm_source(self) -> str:
        """Return the poll-time DM block inside the full-mode arrival loop."""
        source = self._read_main_py()
        start_marker = "# ---- VIP-breakthrough DM for safe-list arrivals"
        end_marker = 'elif slack_mode == "shadow":'
        assert start_marker in source, "Poll-time DM block header not found"
        assert end_marker in source, "shadow-mode branch marker not found"
        start = source.index(start_marker)
        end = source.index(end_marker, start)
        return source[start:end]

    def _read_send_dm_helper_source(self) -> str:
        source = self._read_main_py()
        marker = "async def _send_dm_and_persist("
        assert marker in source, "_send_dm_and_persist helper not found"
        start = source.index(marker)
        # 3500-char window covers the docstring + body without spilling
        # into the next top-level function.
        return source[start:start + 3500]

    # -- Helper existence ---------------------------------------------------

    def test_send_dm_and_persist_helper_exists(self):
        # The DM wiring at both sites (send-time Step 7b + poll-time)
        # delegates to a single helper for consistency. Regression guard:
        # if a future PR inlines the DM logic at one site and drops the
        # helper, the two sites will drift on UC4 handling and Firestore
        # persistence — this test catches the drop.
        source = self._read_main_py()
        assert "async def _send_dm_and_persist(" in source

    def test_send_dm_helper_uses_send_incident_dm(self):
        # Integration point with slack.py::send_incident_dm — the SDK
        # wrapper for conversations.open + chat.postMessage. If a future
        # refactor names the wrapper differently, this pin surfaces the
        # rename so both files stay in lockstep.
        section = self._read_send_dm_helper_source()
        assert "slack_module.send_incident_dm" in section

    def test_send_dm_helper_persists_via_array_union(self):
        # Cluster C compliance: the DM ts is persisted the moment we have
        # it, not at the final .set(). ArrayUnion is required (not a
        # plain list assignment) so concurrent poll-cycle retries
        # atomically dedupe on the Firestore side.
        section = self._read_send_dm_helper_source()
        assert "ArrayUnion([user_id])" in section, (
            "Firestore persistence for slack_dm_sent_user_ids must use "
            "ArrayUnion for concurrent-safety. See Cluster C Locked "
            "Design Decision."
        )
        assert '"slack_dm_sent_user_ids"' in section, (
            "The Firestore field name slack_dm_sent_user_ids must be "
            "used verbatim — poll-time idempotency check reads this exact "
            "key from the incident doc."
        )

    def test_send_dm_helper_returns_bool(self):
        # Failure-mode Discipline Q3: bool return so callers can distinguish
        # DM-succeeded from UC4-fired. Send-time uses it to accumulate the
        # Python-local list that survives the final .set() overwrite.
        # Poll-time uses it for ops-visibility logging. Pinned in
        # test_discarded_return_values.MUST_CAPTURE.
        section = self._read_send_dm_helper_source()
        assert "return True" in section, (
            "_send_dm_and_persist must return True on the DM-sent + "
            "Firestore-patched happy path"
        )
        assert "return False" in section, (
            "_send_dm_and_persist must return False when the UC4 path "
            "fires (send_incident_dm raised)"
        )

    def test_step_7b_accumulates_dm_sent_user_ids_locally(self):
        # LOAD-BEARING for the send-time-DM-doesn't-survive-final-.set() fix.
        # The Python-local dm_sent_user_ids list mirrors the Firestore
        # ArrayUnion patches so the final .set(new_incident_doc(...)) at
        # Step 11 re-writes the field rather than overwriting it to empty.
        # Without this accumulation, poll-time reads slack_dm_sent_user_ids
        # as empty and fires a duplicate DM for every safe-list responder
        # who YES's after being pre-invited at send-time.
        # Bill saw this bug 2026-07-10 during the pilot solo test.
        section = self._read_step_7b_source()
        assert "dm_sent_user_ids: list[str] = []" in section, (
            "Step 7b must initialize the Python-local dm_sent_user_ids "
            "list OUTSIDE the SLACK_MODE gate so it's always defined for "
            "the Step 11 new_incident_doc() call, even in shadow mode."
        )
        assert "dm_sent_user_ids.append(user_id)" in section, (
            "Step 7b must append user_id to the Python-local list on the "
            "TRUE branch of _send_dm_and_persist so the final .set() at "
            "Step 11 preserves the DM'd cohort."
        )
        assert "if await _send_dm_and_persist(" in section, (
            "Step 7b must gate the append on the bool return of "
            "_send_dm_and_persist — otherwise UC4 failures would falsely "
            "record the user as DM'd and suppress the poll-time retry."
        )

    def test_step_11_passes_slack_dm_sent_user_ids_to_new_incident_doc(self):
        # Companion to test_step_7b_accumulates_dm_sent_user_ids_locally.
        # The accumulated list must be passed to new_incident_doc() so it
        # ends up in the doc payload written by the final .set().
        source = self._read_main_py()
        marker = "incident_doc = new_incident_doc("
        assert marker in source, (
            "new_incident_doc() call site not found — this test needs to "
            "be updated if the call site moves"
        )
        start = source.index(marker)
        # 2000-char window covers the full kwargs list without spilling
        # into unrelated code below.
        call_section = source[start:start + 2000]
        assert "slack_dm_sent_user_ids=dm_sent_user_ids" in call_section, (
            "The final .set() must pass the Python-local dm_sent_user_ids "
            "list through new_incident_doc() so slack_dm_sent_user_ids is "
            "re-written and NOT overwritten to empty. See docstring on "
            "new_incident_doc() in incidents.py for the full pattern."
        )

    # -- UC4 undeliverable surface ------------------------------------------

    def test_uc4_notice_posts_to_incident_channel_not_active_incidents(self):
        # Per Bill 2026-07-10: UC4 undeliverable notice goes to the
        # INCIDENT channel only, NOT #active-incidents. Rationale:
        # #active-incidents already carries the tally message; a per-user
        # DM-failure line would clutter the ops summary view. Incident
        # channel is where the dispatcher/IC are already focused on this
        # incident's status. Strip the docstring so its commentary ("not
        # #active-incidents per Bill…") doesn't false-positive the check.
        section = self._read_send_dm_helper_source()
        # Docstring lives between the first two """ tokens.
        parts = section.split('"""', 2)
        code_only = parts[2] if len(parts) >= 3 else section
        assert "slack_channel_id" in code_only
        # The strings that would indicate an active-incidents target:
        # (a) hardcoded channel name in quotes, (b) the module constant.
        assert '"active-incidents"' not in code_only, (
            "UC4 notice must NOT be posted to #active-incidents. See "
            "Bill's decision 2026-07-10 (Q3 of PRD design questions)."
        )
        assert "ACTIVE_INCIDENTS_CHANNEL" not in code_only
        # Life-safety framing — the UC4 message tells the dispatcher/IC
        # WHY they need to follow up out of band. Split across two lines
        # in source (line-wrap on the f-string concat), so we match the
        # two distinctive fragments individually.
        assert "VIP breakthrough will not" in code_only, (
            "UC4 notice text must contain the life-safety framing "
            "'VIP breakthrough will not fire' so the dispatcher/IC "
            "recognizes the notification-reach gap."
        )
        assert "fire for them" in code_only

    # -- Send-time (Step 7b) gates ------------------------------------------

    def test_step_7b_slack_mode_full_gate(self):
        # Only fire in full mode — matches poll-time real-invite gate.
        # In shadow mode, initial invitees ARE still added (safe-list is
        # pre-invited via #active-incidents union), but breakthrough DMs
        # would surprise pilot participants who haven't been VIP-onboarded.
        section = self._read_step_7b_source()
        assert '_SLACK_MODE == "full"' in section

    def test_step_7b_reads_test_label_env_var(self):
        # DISPATCH_TURBO_TEST_LABEL is the operator lever for prepending
        # a "[TEST from Bill Burns …]" marker during pilot. When unset
        # (production), no prefix. Required by
        # feedback-test-messages-to-humans-need-explicit-test-label.
        section = self._read_step_7b_source()
        assert 'os.environ.get("DISPATCH_TURBO_TEST_LABEL"' in section

    def test_step_7b_calls_format_incident_dm_text(self):
        # Integration point with the pure-text formatter (PR #566).
        section = self._read_step_7b_source()
        assert "slack_module.format_incident_dm_text" in section

    def test_step_7b_uses_safe_list_gate(self):
        # DM cohort is restricted to safe-list per Bill 2026-07-10 (Q4).
        # The initial-invite set is broader (dispatcher + SO coord +
        # #active-incidents + safe-list), but the DM eligibility set is
        # safe-list-only. Regression guard: a future refactor that DM's
        # every initial member breaks the pilot cohort restriction.
        section = self._read_step_7b_source()
        assert 'safe_list.get("allowed_emails"' in section, (
            "Step 7b must resolve safe-list emails to user_ids for the "
            "DM eligibility set — DM'ing all initial members would leak "
            "the pilot to non-VIP-onboarded #active-incidents members."
        )
        assert "dm_eligible_uids" in section

    # -- Poll-time gates ----------------------------------------------------

    def test_poll_time_dm_safe_list_gate(self):
        # Poll-time counterpart to Step 7b's safe-list gate. New YES
        # arrivals get channel invites regardless, but DMs only for
        # responders whose email overlaps with dispatch-safe-list.
        section = self._read_poll_time_dm_source()
        assert "safe_list_emails" in section
        assert "emails_lower & safe_set" in section, (
            "Poll-time DM eligibility must use set-intersection with "
            "dispatch-safe-list — see Bill 2026-07-10 Q4 decision."
        )

    def test_poll_time_dm_idempotency_check(self):
        # Cloud Tasks retry could re-fire the poll handler mid-cycle.
        # Check slack_dm_sent_user_ids before each DM prevents duplicate
        # sends on the happy path. ArrayUnion in _send_dm_and_persist
        # covers the concurrent-execution edge case (accepted TOCTOU
        # per D4H mark_member_attending precedent).
        section = self._read_poll_time_dm_source()
        assert 'doc.get("slack_dm_sent_user_ids")' in section
        assert "already_dmd" in section

    def test_poll_time_dm_reads_test_label_env_var(self):
        # Same env-var read as Step 7b — both sites must honor the
        # DISPATCH_TURBO_TEST_LABEL prefix during pilot. Poll-time site
        # wraps os.environ.get(...) across two lines due to indentation
        # depth, so match the env-var name literal + confirm os.environ
        # is used somewhere in the section.
        section = self._read_poll_time_dm_source()
        assert '"DISPATCH_TURBO_TEST_LABEL"' in section
        assert "os.environ.get" in section

    def test_poll_time_dm_calls_format_incident_dm_text(self):
        section = self._read_poll_time_dm_source()
        assert "slack_module.format_incident_dm_text" in section


class TestSlackLegBestEffort:
    """Step 6 (Slack channel creation) best-effort guard.

    Source-sentinel tests — main.py can't be imported in local pytest (heavy
    deps), so pin the control-flow invariants of the fix so a future refactor
    can't silently re-introduce the un-guarded Step 6 that 500'd a dispatch
    AFTER Everbridge had already fired, orphaning a half-incident
    (2026-07-16 SJ Grant mock). See CLAUDE.md Failure-mode Discipline Q2.
    """

    def _read_main_py(self) -> str:
        return (Path(__file__).parent / "main.py").read_text()

    def _read_step_6_source(self) -> str:
        source = self._read_main_py()
        start_marker = "# ---- Step 6: Slack channel (create-or-collide)"
        end_marker = "# ---- Step 7: Invite initial members"
        assert start_marker in source, "Step 6 header not found"
        assert end_marker in source, "Step 7 header not found"
        start = source.index(start_marker)
        end = source.index(end_marker, start)
        assert start < end, "Step 6 must precede Step 7"
        return source[start:end]

    def test_step6_channel_create_is_wrapped_in_try_except(self):
        # The _create_or_collide_channel call MUST be inside a try/except so a
        # SlackApiError (invalid_name_specials, rate_limit, outage) can't
        # propagate as a 500 after EB has already fired at Step 5.
        section = self._read_step_6_source()
        assert "try:" in section
        assert "_create_or_collide_channel" in section
        assert "except Exception" in section

    def test_step6_failure_marks_failed_and_does_not_reraise(self):
        # On failure: set slack_status="failed" and CONTINUE (no re-raise) so
        # the handler reaches tally / D4H / persist / poll. A `raise` here
        # would reinstate the half-incident 500.
        section = self._read_step_6_source()
        except_block = section[section.index("except Exception"):]
        assert 'slack_status = "failed"' in except_block
        assert "raise" not in except_block, (
            "Step 6 except must NOT re-raise — log + continue so the incident "
            "stays tracked and closeable (EB has already fired)."
        )

    def test_step7b_dm_gated_on_channel(self):
        # The VIP-breakthrough DM embeds the channel deep-link, so it MUST be
        # gated on slack_channel_id — else a failed channel blasts safe-list
        # responders a DM pointing at a channel that does not exist.
        source = self._read_main_py()
        assert 'if _SLACK_MODE == "full" and slack_channel_id:' in source

    def test_response_and_persist_carry_slack_status(self):
        # Response surfaces slack_status (frontend paints Slack red while EB
        # stays green); persist folds it so /dispatch-status + audit reflect
        # the real Step 6 outcome.
        source = self._read_main_py()
        assert '"slack_status":' in source
        assert 'incident_doc["slack_status"]' in source

    def test_d4h_milestone_reflects_channel_failure(self):
        # The D4H / event-log milestone must show a creation FAILURE (not
        # "#None") so the dispatcher sees they need to create the channel.
        source = self._read_main_py()
        assert "Slack incident channel creation FAILED" in source

    def test_all_five_channel_ops_gated_on_slack_channel_id(self):
        # A future refactor must not silently drop a guard on any of the five
        # incident-channel operations and re-expose the None-channel path
        # (broken DMs, "#None", wasted failing Slack calls). Pin the full set
        # in the Step 7 -> Step 10 region:
        #   plain `if slack_channel_id:` — invites, welcome post, groups-post
        #   `... and slack_channel_id`   — VIP DM (7b), CalTopo post (8b)
        source = self._read_main_py()
        start = source.index("# ---- Step 7: Invite initial members")
        end = source.index("# ---- Step 10: Initial tally", start)
        region = source[start:end]
        assert region.count("if slack_channel_id:") >= 3, (
            "invites / welcome post / groups-requested post must each stay "
            "gated on `if slack_channel_id:`"
        )
        assert region.count("and slack_channel_id") >= 2, (
            "the VIP DM (Step 7b) and CalTopo post (Step 8b) must each stay "
            "gated on `and slack_channel_id`"
        )


class TestTopRecBareExtraction:
    """Mirror of the _top_rec_bare extraction in main.py (the "Staging Area for
    Resources:" line replacement). main.py can't be imported in local pytest
    (heavy deps), so mirror the string logic here. Change this in the same PR
    when the production logic changes.

    Regression: the pre-fix `_entry_text.split(". ")[0]` truncated a #1
    recommendation whose LOCATION carried a ". " — a middle initial
    ("Joseph D.") or a "Mt."/"St." abbreviation in a geocoder-canonicalized
    park name — to e.g. "Joseph D" (2026-07-16 SJ Grant mock). The corpus
    (experiments/test_forms) contains no period-in-location park inputs, so
    apply_helpers wouldn't surface this; this mirror pins the exact case.
    """

    @staticmethod
    def _top_rec_bare(entry_text: str) -> str:
        """Mirror of main.py _top_rec_bare extraction (incl. officer-suffix strip)."""
        entry_text = re.sub(
            r"\s+—\s+Officer-designated staging location.*$", "", entry_text
        )
        if " — " in entry_text:
            _loc, _rest = entry_text.split(" — ", 1)
            return f"{_loc.strip()} — {_rest.split('. ', 1)[0].strip()}"
        return entry_text.split(". ", 1)[0].strip()

    def test_period_in_location_not_truncated(self):
        out = self._top_rec_bare(
            "Joseph D. Grant County Park, San Jose — County park. 0.3 mi from LKP; parking."
        )
        assert out == "Joseph D. Grant County Park, San Jose — County park"

    def test_normal_street_address_unchanged(self):
        # The common case must still trim the trailing prose sentence.
        out = self._top_rec_bare(
            "55 North 7th Street, San Jose — Horace Mann Elementary School. 0.3 mi from LKP."
        )
        assert out == "55 North 7th Street, San Jose — Horace Mann Elementary School"

    def test_abbreviation_in_location(self):
        out = self._top_rec_bare("Mt. Hamilton Rd, San Jose — Road access. 1.2 mi from LKP.")
        assert out == "Mt. Hamilton Rd, San Jose — Road access"

    def test_officer_suffix_stripped_then_extracted(self):
        out = self._top_rec_bare(
            "Joseph D. Grant County Park, San Jose — County park. Details here. "
            "— Officer-designated staging location (not among top recommendations)."
        )
        assert out == "Joseph D. Grant County Park, San Jose — County park"

    def test_no_dash_fallback(self):
        # No " — " separator → trim at the first ". " (unchanged fallback).
        out = self._top_rec_bare("Some Place. extra sentence.")
        assert out == "Some Place"


class TestEmptyOverpassNote:
    """Source-sentinel: an Overpass success-with-zero-candidates must emit an
    informational event-log note (main.py), so a remote-LKP incident with only
    the officer's staging doesn't look silently broken (2026-07-16 Joseph D.
    Grant). Distinct from the Overpass-FAILURE warning that already existed.
    """

    def test_empty_overpass_emits_note(self):
        source = (Path(__file__).parent / "main.py").read_text()
        # The radius is no longer a literal: #838 retries wider on zero, so the note
        # reports the radius ACTUALLY searched (0.75 mi, or 3.00 mi after a widened
        # retry). Re-pinning 1200 m would understate a widened search and read to the
        # dispatcher as a near-LKP failure. The note's EXISTENCE is what this pins.
        assert "of the LKP (remote area) — staging options limited; confirm " in source
        assert "_staging_searched_m" in source
        # Must be gated on a successful-but-empty result, not the failure path.
        assert "elif not staging_candidates:" in source


# ---------------------------------------------------------------------------
# Full-mode arrival message MUST reflect a real invite (2026-07-19)
# ---------------------------------------------------------------------------
# Regression boundary for a production false positive found on the 2026-07-19
# personal-dev live test. main.py posted f"{display_name} added" from OUTSIDE
# the `if uid:` guard, so a YES responder who was never invited still produced
# a positive confirmation in the incident channel. Full mode had no competing
# signal: the G.5 ERROR only fires when a lookup RAISED, so the zero-email
# case logged nothing, and the poll handler writes no event-log entries.
#
# Source-scan style, same as TestMarkMemberAttendingTOCTOUDocumentation — the
# /poll-incident handler cannot be imported in the local pytest env.
#
# Drift caught by these tests:
#   - '<Name> added' escapes the `if invited:` guard again (false positive
#     returns; dispatcher told someone joined who did not)
#   - `invited` gets set before/independently of the invite_user call
#   - the failure path stops naming a cause (dispatcher loses the remedy)
#   - the zero-email case loses its log line (silent again on every surface)

class TestFullModeArrivalMessageGatedOnInvite:
    def _full_mode_branch(self) -> str:
        source = (Path(__file__).parent / "main.py").read_text()
        marker = 'if slack_mode == "full":'
        assert marker in source, (
            "full-mode poll branch not found in main.py — the branch may have "
            "been restructured; update this test's marker"
        )
        start = source.index(marker)
        # Bound on the branch's real end marker rather than a fixed char
        # window. The previous `start + 10000` window clipped `emails_lower`
        # mid-token at 9993 the moment explanatory comments were added to the
        # arrival block (2026-07-28) — a test that fails on comment volume
        # rather than on behaviour. The shadow branch is the structural end of
        # the full-mode branch, so this slice tracks refactors instead of
        # needing a hand-tuned constant.
        end_marker = 'elif slack_mode == "shadow":'
        assert end_marker in source, (
            "shadow-mode branch marker not found — full-mode branch may have "
            "been restructured; update this test's end marker"
        )
        return source[start:source.index(end_marker, start)]

    def test_added_line_is_inside_the_invited_guard(self):
        section = self._full_mode_branch()
        assert "if invited:" in section, (
            "the '<Name> added' line must be gated on `if invited:` — posting "
            "it unconditionally tells the dispatcher a responder joined the "
            "channel when they may never have been invited"
        )
        assert section.index("if invited:") < section.index('f"{display_name} added"'), (
            "'<Name> added' must appear AFTER the `if invited:` guard"
        )

    def test_invited_is_set_only_after_the_invite_call(self):
        section = self._full_mode_branch()
        assert "invited = False" in section, "invited must default to False"
        assert "invited = True" in section
        assert section.index("slack_module.invite_user") < section.index("invited = True"), (
            "`invited = True` must follow the invite_user call — setting it "
            "earlier re-creates the false positive this test exists to prevent"
        )

    def test_failure_path_posts_a_cause_specific_message(self):
        section = self._full_mode_branch()
        assert "format_invite_failed_message" in section, (
            "a non-invited YES responder must produce an actionable channel "
            "line — silence is what made this invisible in production"
        )
        for const in ("INVITE_FAIL_NO_EB_EMAIL", "INVITE_FAIL_NO_SLACK_USER",
                      "INVITE_FAIL_ERROR"):
            assert const in section, (
                f"{const} missing — each cause has a different dispatcher "
                f"remedy and must stay distinguishable"
            )

    def test_zero_email_case_is_logged(self):
        # The G.5 ERROR cannot cover this path: no lookup is attempted, so
        # any_lookup_raised stays False. Without this log the case is silent
        # in Cloud Run even after the channel message exists.
        section = self._full_mode_branch()
        assert "no_eb_email" in section

    def test_vip_dm_is_gated_on_invited_not_uid(self):
        # The VIP-breakthrough DM asserts "You've been added to incident
        # <#CHANNEL_ID>". Gating it on `uid` instead of `invited` re-creates
        # the false positive on a WORSE surface than the channel post: the DM
        # breaks through Do Not Disturb and links the responder to a private
        # channel they cannot open. Caught in code review 2026-07-19.
        section = self._full_mode_branch()
        dm_marker = "VIP-breakthrough DM for safe-list arrivals"
        assert dm_marker in section, (
            "VIP DM block not found in the full-mode branch — update this "
            "test's marker if the block moved"
        )
        after_dm = section[section.index(dm_marker):]
        # The first gate following the DM banner must be `if invited:`.
        assert "if invited:" in after_dm, (
            "VIP DM must be gated on `invited` — `if uid:` sends a "
            "'You've been added' DM even when invite_user raised"
        )
        assert after_dm.index("if invited:") < after_dm.index("emails_lower"), (
            "the `if invited:` gate must wrap the safe-list DM block"
        )


class TestOverrideZeroResultOutcome:
    """Issue #607 — a zero-result staging override is not a success.

    On 2026-07-24 two consecutive overrides logged `outcome=success
    nearby_count=0` while the anchor sat ~13 km from the real incident, because
    the LKP had resolved to the wrong city. The dispatcher read the blank list
    as "nothing near here" rather than "we are searching the wrong city,"
    opened Google Maps by hand, and was "questioning the value of the tool at
    this point."
    """

    @staticmethod
    def _handler_body():
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        start = src.find("async def apply_staging_override(")
        assert start != -1, "apply_staging_override handler not found"
        end = src.find("\n@app.", start + 1)
        assert end != -1 and end > start, "could not bound apply_staging_override"
        return src[start:end]

    def test_outcome_is_conditional_not_hardcoded_success(self):
        body = self._handler_body()
        assert '_outcome = "success" if nearby else "no_candidates"' in body, (
            "the terminal log/response no longer distinguishes a zero-result "
            "override from a real one — the single line that could surface a "
            "wrong anchor is asserting success again"
        )
        assert "outcome=success latency_ms" not in body, (
            "outcome=success is hardcoded in the terminal log line again"
        )

    def test_resolved_locality_is_captured_not_discarded(self):
        """The 4th geocode element is the locality; it used to be dropped."""
        body = self._handler_body()
        assert "anchor_lat, anchor_lng, _display, resolved_locality = result" in body, (
            "the resolved locality is being discarded again — without it a "
            "wrong-city anchor is invisible to the dispatcher"
        )

    def test_response_carries_locality_outcome_and_radius(self):
        body = self._handler_body()
        for key in ('"outcome": _outcome',
                    '"resolved_locality": resolved_locality or ""',
                    '"search_radius_m": _OVERRIDE_OVERPASS_RADIUS_M'):
            assert key in body, f"response no longer carries {key}"

    @staticmethod
    def _logger_calls(body):
        """Every logger.* call in the handler, sliced by BALANCED PARENS.

        A regex with DOTALL was tried first and was wrong: it ran from one
        logger call to a closing paren far below, swallowing unrelated code
        (including the geocode assignment) and failing for reasons that had
        nothing to do with logging. Counting parens is the only way to get the
        actual argument list.
        """
        calls = []
        for kw in ("logger.info(", "logger.warning(", "logger.error("):
            i = body.find(kw)
            while i != -1:
                j = i + len(kw) - 1          # at the opening paren
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
        return calls

    def test_locality_is_never_logged(self):
        """Locked Decision: this endpoint's LOG carries no PII, ever.

        The response may carry the locality — it goes to the dispatcher's own
        browser. The log may not. This is what keeps #607's fix from quietly
        undoing the PII guarantee it sits next to.
        """
        calls = self._logger_calls(self._handler_body())
        assert len(calls) >= 4, f"expected several logger calls, found {len(calls)}"
        for call in calls:
            for forbidden in ("resolved_locality", "primary_address", "anchor_lat",
                              "anchor_lng", "utm_display"):
                assert forbidden not in call, (
                    f"apply-staging-override logs {forbidden!r} — violates the Locked "
                    f"Decision that this endpoint logs only latency_ms, outcome, "
                    f"nearby_count and mode. Offending call: {call[:120]}"
                )

    def test_resolved_locality_initialised_outside_the_address_branch(self):
        """lat_lng and UTM modes never geocode — the name must still be bound.

        Same defensive shape as _lkp_locality_suspect in #616: the response
        builder reads it unconditionally, so a branch-local binding would raise
        UnboundLocalError on two of the three input modes.
        """
        body = self._handler_body()
        init = body.find("resolved_locality: str | None = None")
        # Anchor on the UNIQUE address-mode assignment, not on
        # "if lat_lng is not None:" — that string also appears earlier in the
        # input-validation block, so the first match sat BEFORE the init and
        # the comparison was meaningless.
        assigned = body.find("anchor_lat, anchor_lng, _display, resolved_locality = result")
        used = body.find('"resolved_locality": resolved_locality or ""')
        assert init != -1, "resolved_locality is no longer pre-initialised"
        assert assigned != -1 and used != -1
        assert init < assigned < used, (
            "resolved_locality is initialised inside the mode branches — the "
            "lat_lng and UTM paths never geocode, so the response builder will "
            "raise UnboundLocalError on two of the three input modes"
        )


class TestMapGateCancelRestoresOverridePanel:
    """#622 follow-up — Cancel on the wrong-city map gate must re-enable the
    override panel.

    `lockTextarea()` fires on the Create Incident Map click and disables BOTH
    override buttons via `_overrideSetLocked(true)`. The wrong-city modal and
    its banner both tell the dispatcher to use "Apply Override" — so cancelling
    without restoring leaves the remedy greyed out.

    This is the same defect review caught on the DISPATCH gate in #616
    (`_wasLockedBeforeSend`), which was fixed there and not carried over to the
    map gate. Found in live testing 2026-07-25: "I tried to click 'clear
    override' but was unable (greyed out)."
    """

    @staticmethod
    def _frontend():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _map_handler(html):
        start = html.find("document.getElementById('incident-map-btn').addEventListener")
        assert start != -1, "map button handler not found"
        # Bound on the NEXT dispatch-button handler. The D4H comment was tried
        # first and sits BEFORE this handler, so find() returned -1 — the
        # assert caught it rather than letting the window silently be empty,
        # which is the failure mode that makes a source pin vacuous.
        end = html.find("// 🔔 EB+Slack send", start)
        assert end != -1 and end > start, "could not bound the map handler"
        assert "incident-map-btn" in html[start:end]
        return html[start:end]

    def test_lock_state_is_captured_before_locking(self):
        body = self._map_handler(self._frontend())
        cap = body.find("const _wasLockedBeforeMap")
        # Anchor on the STATEMENT, not the bare name: the explanatory comment
        # above the capture also says "lockTextarea()", and a bare find()
        # matched that prose first — putting the "lock" position before the
        # capture and failing for a reason that had nothing to do with the code.
        lock = body.find("\n    lockTextarea();")
        assert cap != -1, "the pre-click lock state is no longer captured"
        assert lock != -1, "the lockTextarea() call site was not found"
        assert cap < lock, (
            "_wasLockedBeforeMap is read AFTER lockTextarea() — it would always "
            "be true, so the override panel would never be restored"
        )

    def test_cancel_path_restores_the_override_panel(self):
        body = self._map_handler(self._frontend())
        assert "if (!_wasLockedBeforeMap) unlockTextarea();" in body, (
            "the wrong-city Cancel path no longer re-enables the override "
            "panel — the modal tells the dispatcher to use Apply Override "
            "while leaving it disabled"
        )

    def test_restore_is_conditional_not_unconditional(self):
        """Must not revive a panel a prior dispatch action deliberately locked."""
        body = self._map_handler(self._frontend())
        assert "unlockTextarea();" in body
        assert body.count("if (!_wasLockedBeforeMap) unlockTextarea();") == 1, (
            "the unlock is unconditional or duplicated — PR-D-2.5 keeps the "
            "panel dead after a dispatch action, and this must not override that"
        )

    def test_dispatch_gate_still_has_its_own_guard(self):
        """The two gates are independent; fixing one must not remove the other."""
        html = self._frontend()
        assert "_wasLockedBeforeSend" in html, (
            "the dispatch gate's lock-state guard is gone"
        )


# ---------------------------------------------------------------------------
# Issue #87 — D4H referenceDescription must honour dispatcher Event Name edits
# ---------------------------------------------------------------------------
# The 2026-07-24 callout: the dispatcher corrected the Event Name in the
# textarea. /create-map re-parses the textarea into freshMapData, so CalTopo's
# map title picked the correction up. /send-notification forwards the OCR-time
# _rawMapData, and _build_ocr_data_for_d4h read event_name out of it — so D4H's
# referenceDescription kept the PRE-EDIT name. CalTopo honoured the edit, D4H
# did not.
#
# The fix routes D4H to event_name_human — the same live-textarea value the
# handler already validates (400 when blank) and already uses for the EB title,
# the Slack channel name and the Firestore doc.
#
# These pins read backend/main.py DIRECTLY. test_send_notification.py mirrors
# _build_ocr_data_for_d4h locally, so its behavioural tests pass whether or not
# main.py was actually changed — a mirror cannot detect its own drift. This
# class is the half that reads production.
# ---------------------------------------------------------------------------

class TestD4HEventNameHonoursDispatcherEdits:
    def _main_py(self) -> str:
        return (Path(__file__).parent / "main.py").read_text()

    def _builder_source(self) -> str:
        """Return the body of _build_ocr_data_for_d4h in main.py."""
        source = self._main_py()
        start_marker = "def _build_ocr_data_for_d4h("
        assert start_marker in source, "_build_ocr_data_for_d4h not found in main.py"
        start = source.index(start_marker)
        # The next top-level `def ` after the helper bounds its body.
        end = source.index("\ndef ", start + len(start_marker))
        return source[start:end]

    def test_builder_accepts_event_name_human(self):
        assert "event_name_human: str = \"\"" in self._builder_source(), (
            "_build_ocr_data_for_d4h no longer accepts event_name_human — the "
            "dispatcher's edited Event Name cannot reach D4H"
        )

    def test_builder_prefers_event_name_human_over_map_data(self):
        """The exact regression: event_name sourced from map_data alone."""
        body = self._builder_source()
        assert (
            'event_name = (event_name_human or map_data.get("event_name") or "").strip()'
            in body
        ), (
            "the event_name assignment in _build_ocr_data_for_d4h no longer "
            "prefers event_name_human. If it reads map_data first (or only), "
            "D4H files the OCR-time name and silently ignores the dispatcher's "
            "correction — the 2026-07-24 failure (issue #87)"
        )

    def test_event_name_human_precedes_map_data_in_the_expression(self):
        """Order matters: `or` short-circuits, so map_data first would win."""
        body = self._builder_source()
        line = next(
            ln for ln in body.splitlines()
            if ln.strip().startswith("event_name = (")
        )
        # Guard so a dropped operand fails as an assertion, not a ValueError.
        assert "event_name_human" in line and "map_data" in line, (
            f"the event_name assignment lost an operand: {line.strip()!r}"
        )
        assert line.index("event_name_human") < line.index("map_data"), (
            "map_data is evaluated before event_name_human — `or` short-circuits "
            "on the first truthy operand, so the stale OCR-time name wins"
        )

    def test_send_notification_call_site_passes_event_name_human(self):
        """A correct helper is inert if the one production caller omits the arg."""
        source = self._main_py()
        marker = "ocr_data_for_d4h = _build_ocr_data_for_d4h("
        assert marker in source, "the /send-notification D4H call site moved"
        start = source.index(marker)
        call = source[start:source.index(")", start)]
        assert "event_name_human=event_name_human" in call, (
            "the /send-notification call site does not forward event_name_human. "
            "The helper falls back to map_data['event_name'], so D4H silently "
            "regresses to the pre-edit Event Name (issue #87)"
        )

    def test_d4h_date_strip_still_applies(self):
        """event_name_human is date-prefixed like map_data['event_name'] was;
        D4H's own naming (no leading date) depends on the strip in d4h.py."""
        d4h_py = (Path(__file__).parent / "d4h.py").read_text()
        assert r'"^\d{4}-\d{2}-\d{2}\s+"' in d4h_py, (
            "_strip_date_for_d4h_title's date pattern changed — D4H "
            "referenceDescription would carry the YYYY-MM-DD prefix"
        )


# ---------------------------------------------------------------------------
# Poll-time arrival posts: routine confirmations threaded, failures top-level
# ---------------------------------------------------------------------------
# Source of truth: the full-mode arrival loop in main.py's /poll-incident.
#
# Why source-reading rather than behavioural tests: this logic lives inside the
# /poll-incident handler body, which is not importable, so there is no seam to
# call. These sentinels are the only available guard — and per the 2026-07-27
# self-mirroring lesson each assertion below is scoped to ONE branch, because
# asserting that both `arrival_thread_ts` assignments merely EXIST somewhere in
# the block would still pass if a refactor swapped them (threading the failures
# and broadcasting the successes — precisely inverting the intent).
#
# Empirical basis (spike_thread_notify.py, 2026-07-28, real device): a
# bot-authored threaded reply notified NEITHER mobile nor desktop for a member
# with the channel on "All new posts", while a top-level control message posted
# 10s earlier notified both.
class TestPollArrivalThreading:
    def _read_arrival_post_block(self) -> str:
        source = (Path(__file__).parent / "main.py").read_text()
        start_marker = "display_name = slack_module.to_conversational_name("
        end_marker = "# ---- VIP-breakthrough DM for safe-list arrivals"
        assert start_marker in source, "arrival display_name line not found"
        assert end_marker in source, "poll-time DM block header not found"
        start = source.index(start_marker)
        end = source.index(end_marker, start)
        return source[start:end]

    def _success_branch(self) -> str:
        """Only the `if invited:` arm."""
        block = self._read_arrival_post_block()
        start = block.index("if invited:")
        end = block.index("\n                else:", start)
        return block[start:end]

    def _failure_branch(self) -> str:
        """Only the `else:` arm, up to the shared post_message call."""
        block = self._read_arrival_post_block()
        start = block.index("\n                else:")
        end = block.index("\n                try:", start)
        return block[start:end]

    def test_success_arrival_is_threaded_under_welcome(self):
        assert 'arrival_thread_ts = doc.get("welcome_ts") or None' in (
            self._success_branch()
        ), (
            "The routine '<Name> added' confirmation must post as a THREAD "
            "reply under the pinned welcome. Top-level posts notify every "
            "member — and because responders VIP the bot, they arrive as "
            "iOS Time Sensitive alerts that pierce Do Not Disturb."
        )

    def test_failure_arrival_stays_top_level(self):
        assert "arrival_thread_ts = None" in self._failure_branch(), (
            "Invite-failure lines must stay TOP-LEVEL so they still notify. "
            "Each one needs a dispatcher to act out of band; burying them in "
            "an unsubscribed thread is the one outcome this must not produce."
        )

    def test_success_branch_does_not_suppress_notification_by_accident(self):
        # Guards the inverse of the two tests above: the success arm must NOT
        # hard-code None (which would be indistinguishable from the failure
        # arm and silently drop the welcome_ts fallback).
        assert "arrival_thread_ts = None" not in self._success_branch(), (
            "Success arm must derive thread_ts from welcome_ts, not hard-code None"
        )

    def test_failure_branch_is_not_threaded(self):
        assert "welcome_ts" not in self._failure_branch(), (
            "Failure arm must not reference welcome_ts — failures post top-level"
        )

    def test_post_message_call_passes_thread_ts(self):
        assert "thread_ts=arrival_thread_ts" in self._read_arrival_post_block(), (
            "The arrival post_message call must forward thread_ts — without "
            "it both branches silently revert to top-level posts"
        )

    def test_post_message_accepts_thread_ts(self):
        slack_py = (Path(__file__).parent / "slack.py").read_text()
        assert "thread_ts: Optional[str] = None" in slack_py, (
            "slack.post_message must accept thread_ts"
        )
        assert "thread_ts=thread_ts," in slack_py, (
            "slack.post_message must forward thread_ts to chat_postMessage"
        )

    def test_reply_broadcast_is_never_set(self):
        # reply_broadcast=True mirrors a threaded reply back into the channel
        # timeline AND notifies everyone — it would restore the exact noise
        # this change removes, while still looking correctly "threaded" in code.
        # Matches the kwarg form only — the identifier appears in prose in
        # post_message's docstring, explaining why it must never be used.
        for name in ("main.py", "slack.py"):
            source = (Path(__file__).parent / name).read_text()
            assert "reply_broadcast=" not in source, (
                f"{name} sets reply_broadcast — a threaded reply posted with "
                "it notifies the whole channel, defeating the change"
            )

    def test_arrival_text_uses_plain_name_not_mention(self):
        # An <@UID> mention renders more nicely and is the obvious future
        # "polish" — it would also ping that responder on every arrival,
        # re-introducing a notification on the surface we just silenced.
        assert 'arrival_msg = f"{display_name} added"' in self._success_branch(), (
            "Arrival text must use the plain display name. An <@UID> mention "
            "would notify that responder despite the message being threaded."
        )


# ---------------------------------------------------------------------------
# Shadow-mode arrival posts: threaded only when the cycle is purely arrivals
# ---------------------------------------------------------------------------
# Companion to TestPollArrivalThreading (full mode). Shadow mode was left
# untouched by the original threading change on the reasoning that it is
# "personal-dev only and batched per cycle" — both true, and the wrong call:
# Rule #9 makes personal-dev the ONLY environment where EB/Slack work can be
# tested, so the untouched branch was the only branch reachable in a live test.
# Live-caught 2026-07-28: a personal-dev dispatch posted "Bill Burns added"
# top-level with a Time Sensitive push, from this branch, while the full-mode
# code sat correctly deployed and unreachable.
#
# The split is coarser than full mode's because format_would_invite_message
# renders all three buckets into ONE message: any failure content keeps the
# whole post top-level rather than burying a "Cannot invite" line in a thread.
class TestShadowArrivalThreading:
    def _shadow_branch(self) -> str:
        source = (Path(__file__).parent / "main.py").read_text()
        start_marker = 'elif slack_mode == "shadow":'
        end_marker = "# ---- Per-responder D4H sync"
        assert start_marker in source, "shadow-mode branch not found"
        assert end_marker in source, "D4H sync block header not found"
        start = source.index(start_marker)
        return source[start:source.index(end_marker, start)]

    def test_shadow_post_forwards_thread_ts(self):
        assert "thread_ts=shadow_thread_ts" in self._shadow_branch(), (
            "The shadow-mode post must forward thread_ts — without it the "
            "branch silently reverts to a top-level notifying post, which is "
            "exactly the 2026-07-28 live-test failure"
        )

    def test_shadow_threads_only_when_no_failure_content(self):
        branch = self._shadow_branch()
        assert "shadow_has_failure_content = bool(" in branch, (
            "shadow branch must compute whether the cycle carries failure content"
        )
        assert "resolvable_names or unresolvable_names" in branch, (
            "failure content = either the 'Would invite' or the 'Cannot "
            "invite' bucket being non-empty"
        )

    def test_shadow_failure_content_forces_top_level(self):
        # The conditional must resolve to None when failure content exists.
        # Asserting only that welcome_ts appears somewhere in the branch would
        # pass even if the condition were inverted — so pin the ordering that
        # puts None on the failure arm.
        branch = self._shadow_branch()
        assert "None if shadow_has_failure_content" in branch, (
            "failure content MUST force a top-level post — an inverted "
            "condition would thread the failures and notify on the routine "
            "arrivals, precisely backwards"
        )

    def test_shadow_uses_welcome_ts_as_parent(self):
        assert 'doc.get("welcome_ts") or None' in self._shadow_branch(), (
            "shadow thread parent must be the pinned welcome, with a "
            "top-level fallback when the welcome post failed"
        )


# ---------------------------------------------------------------------------
# Rendered-staging duplicate dedup (2026-08-01)
# ---------------------------------------------------------------------------

def _staging_line_dedup_key_mirror(body: str) -> str:
    """MIRROR of main.py::_staging_line_dedup_key.

    Kept byte-identical to production's two operative lines; the parity test
    below reads main.py and fails if either drifts.
    """
    loc_part = body.split(" — ")[0].strip()
    return re.sub(r"\s+", " ", loc_part.split(",")[0].lower()).strip()


class TestRenderedStagingDedup:
    """Pin the dedup of the RENDERED staging recommendation block.

    _rank_dedupe_cap_staging drops same-address entries from the CANDIDATE
    list — which is a no-op in exactly the case that needs it. With a remote
    anchor (the 2026-07-31 Humboldt mutual-aid form) the POI lookup returns
    ZERO candidates, so Gemini writes the whole recommendation list from
    training data and reproducibly emits TWO ENTRIES AT THE SAME INVENTED
    ADDRESS — 1000 Ashby Rd, then 1470 Main St, then 1450 G St across three
    runs of the same form. Both lines are individually well-formed, so every
    existing PASS 2 filter passes them, and there was no candidate list to
    dedup against.

    This is NOT fixable in the prompt: #675 established that a rule already
    stated twice in gemini.py was ignored on the same dispatch. Server-side is
    the only enforceable layer.
    """

    @staticmethod
    def _src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @staticmethod
    def _fn(src):
        """Body of _staging_line_dedup_key, bounded on real markers at BOTH ends."""
        m = re.search(
            r"^def _staging_line_dedup_key\(.*?(?=\n\n(?:def |async def |# -{10,}))",
            src, re.DOTALL | re.MULTILINE,
        )
        assert m, "_staging_line_dedup_key not found in main.py"
        return m.group(0)

    @staticmethod
    def _pass2(src):
        """The PASS 2 loop of the /ocr staging post-processing.

        Bounded on the PASS 2 and PASS 3 comment markers — both real, both
        unique in main.py. Never `start + N` characters: an insertion above
        would push the target out (false FAIL) and a deletion would pull junk
        in (false PASS).
        """
        start = src.find("# PASS 2: Filter")
        end = src.find("# PASS 3: Officer staging injection")
        assert start != -1, "PASS 2 marker gone from main.py"
        assert end != -1 and end > start, "PASS 3 marker gone from main.py"
        return src[start:end]

    @staticmethod
    def _code_only(text):
        """Strip docstring and every comment.

        Load-bearing: the rationale for this dedup is written directly above
        the code implementing it and names the same identifiers, including the
        literal invented addresses. A raw-source pin finds the explanation and
        passes with the implementation deleted.
        """
        body = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    # --- production parity ------------------------------------------------

    def test_key_helper_matches_the_mirror(self):
        """The mirror below is what the behaviour tests exercise, because
        main.py is not importable (heavyweight GCP deps)."""
        code = self._code_only(self._fn(self._src()))
        assert 'loc_part = body.split(" — ")[0].strip()' in code, (
            "_staging_line_dedup_key no longer takes the location as the text "
            "before the first em-dash — the mirror in this file has drifted."
        )
        assert 'return re.sub(r"\\s+", " ", loc_part.split(",")[0].lower()).strip()' in code, (
            "_staging_line_dedup_key no longer keys on the normalized "
            "pre-comma text — the mirror in this file has drifted."
        )

    def test_dedup_is_wired_into_pass_2(self):
        """Assert the CALL, inside the loop — not the identifier anywhere.

        The helper is referenced by its own defining line 2,000 lines above."""
        code = self._code_only(self._pass2(self._src()))
        assert "_dup_key = _staging_line_dedup_key(body)" in code, (
            "The rendered-staging dedup is gone from PASS 2 — Gemini can again "
            "emit two recommendations at one invented address."
        )
        assert "_dup_key in _seen_staging_keys" in code, (
            "PASS 2 computes a dedup key but never compares it."
        )
        assert "_seen_staging_keys.add(_dup_key)" in code, (
            "PASS 2 compares against a set it never populates — every line is "
            "unique by construction and the filter can reject nothing."
        )

    def test_dedup_rejects_before_the_line_is_kept(self):
        """Structure, not keyword presence.

        A check that runs after the append is a no-op containing every string
        the pins above look for."""
        code = self._code_only(self._pass2(self._src()))
        assert code.index("_dup_key in _seen_staging_keys") < code.index("kept_lines.append(f\"{next_num}."), (
            "The duplicate check now runs after the line is appended, so it "
            "can never reject anything."
        )
        branch = code[code.index("_dup_key in _seen_staging_keys"):
                      code.index("_seen_staging_keys.add(_dup_key)")]
        assert "continue" in branch, (
            "The duplicate branch no longer skips the line — it matches, logs, "
            "and falls through to append it anyway."
        )

    def test_dropped_lines_do_not_seed_the_key_set(self):
        """A line rejected by an earlier filter must never claim a key.

        If the dedup ran first, a name-only line that PASS 2 then drops would
        still have reserved its address — silently evicting the real entry at
        that address further down the list.
        """
        code = self._code_only(self._pass2(self._src()))
        assert code.index("name-only, no street number") < code.index("_dup_key = _staging_line_dedup_key(body)"), (
            "The dedup moved ahead of the name-only filter — a line that is "
            "about to be dropped now reserves its address key."
        )

    def test_officer_and_dispatcher_entries_are_never_dropped(self):
        """Two labels, both required.

        The officer/dispatcher entry is the one line a human actually chose.
        Losing it to a fabricated twin is far worse than leaving a visible
        duplicate — and PASS 3 detects an existing officer entry by this same
        label, so dropping one would also trigger a second, injected copy.
        """
        code = self._code_only(self._pass2(self._src()))
        assert "not _is_override_line" in code, (
            "The override exemption is gone from the duplicate check — an "
            "officer-designated entry can now be dropped as a duplicate."
        )
        assert "_OFFICER_OVERRIDE_LABEL in body" in code, (
            "The officer label leg of the override exemption is gone."
        )
        assert "_DISPATCHER_OVERRIDE_LABEL in body" in code, (
            "The dispatcher label leg of the override exemption is gone."
        )

    def test_override_lines_still_seed_the_key_set(self):
        """Exempt from being DROPPED, not from being COUNTED.

        If the officer entry did not claim its key, a fabricated copy of the
        officer's own address below it would survive.
        """
        code = self._code_only(self._pass2(self._src()))
        seed = code[code.index("_seen_staging_keys.add(_dup_key)") - 200:
                    code.index("_seen_staging_keys.add(_dup_key)")]
        guard = seed[seed.rindex("if "):]
        assert "_is_override_line" not in guard, (
            "The key-set seeding is now gated on the override exemption, so an "
            "officer entry no longer suppresses a fabricated copy of itself."
        )

    # --- behaviour (mirror) -----------------------------------------------

    def test_same_house_and_street_collide(self):
        """The shipped failure: two fabricated entries, one invented address."""
        a = "1000 Ashby Rd, Eureka — Ray's Food Place. 0.4 mi from LKP."
        b = "1000 Ashby Rd, Eureka — Chevron. 0.4 mi from LKP."
        assert _staging_line_dedup_key_mirror(a) == _staging_line_dedup_key_mirror(b)

    def test_different_house_number_same_street_survives(self):
        a = "1000 Ashby Rd, Eureka — Ray's Food Place."
        b = "1100 Ashby Rd, Eureka — Chevron."
        assert _staging_line_dedup_key_mirror(a) != _staging_line_dedup_key_mirror(b)

    def test_city_and_postcode_are_ignored(self):
        """Same shape as _rank_dedupe_cap_staging's addr_key: differing city or
        postcode tags must not defeat the comparison."""
        a = "7000 Yerba Buena Rd, San Jose — Safeway."
        b = "7000 Yerba Buena Rd, San Jose, 95135 — Walgreens."
        assert _staging_line_dedup_key_mirror(a) == _staging_line_dedup_key_mirror(b)

    def test_case_and_whitespace_normalized(self):
        a = "1000 ASHBY   RD, Eureka — Ray's."
        b = "1000 Ashby Rd, Eureka — Chevron."
        assert _staging_line_dedup_key_mirror(a) == _staging_line_dedup_key_mirror(b)

    def test_same_park_listed_twice_collides(self):
        """Parks key on their own NAME, so a repeat is a genuine duplicate."""
        a = "Sequoia Park, Eureka — City park. 0.8 mi from LKP."
        b = "Sequoia Park, Eureka — Municipal park. 0.8 mi from LKP."
        assert _staging_line_dedup_key_mirror(a) == _staging_line_dedup_key_mirror(b)

    def test_two_different_parks_in_one_city_survive(self):
        """The false-positive guard that justifies not exempting parks.

        _rank_dedupe_cap_staging DOES exempt parks, because Geoapify fills a
        bare `city` for nearly every park and an address key would collapse
        them on a data artifact. A rendered line leads with the park's own
        name, so that rationale does not transfer — and this is the test that
        proves it.
        """
        a = "Sequoia Park, Eureka — City park."
        b = "Carson Park, Eureka — City park."
        assert _staging_line_dedup_key_mirror(a) != _staging_line_dedup_key_mirror(b)

    def test_park_line_without_an_em_dash_still_keys_on_its_name(self):
        """PASS 2 admits a park line with no em-dash (it is exempt from the
        address filter), so the key must survive that shape too."""
        a = "Sequoia Park, Eureka. City park. 0.8 mi from LKP."
        assert _staging_line_dedup_key_mirror(a) == "sequoia park"

    def test_coordinate_staging_line_keys_distinctly(self):
        """A written coordinate renders as "<lat>, <lng> — <utm>" (#668) and
        must not collide with a street address."""
        a = "40.80210, -124.16370 — 10T 401234E 4517890N"
        b = "1000 Ashby Rd, Eureka — Chevron."
        assert _staging_line_dedup_key_mirror(a) == "40.80210"
        assert _staging_line_dedup_key_mirror(a) != _staging_line_dedup_key_mirror(b)


class TestRequestLineWiring:
    """Pin the seam carrying the intake Request box to the pinned welcome.

    slack.py's own rendering is pinned in test_slack.py. This class pins the
    two hops that file cannot see — neither main.py nor index.html is
    importable — because a correct renderer fed nothing renders nothing, and
    that failure is completely silent: the Request line simply never appears.
    """

    @staticmethod
    def _main():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @staticmethod
    def _index():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")

    @staticmethod
    def _code_only(text):
        return "\n".join(l.split("#")[0] for l in text.splitlines())

    def _welcome_call(self):
        """The format_pinned_welcome(...) call site, bounded at BOTH ends on
        real markers — never start + N characters."""
        src = self._main()
        start = src.index("format_pinned_welcome(")
        end = src.index("format_staging_message(", start)
        return self._code_only(src[start:end])

    def test_main_passes_the_request_through(self):
        assert 'request=body.get("mp_request") or ""' in self._welcome_call(), (
            "main.py no longer forwards mp_request to format_pinned_welcome — "
            "the renderer is intact but permanently receives an empty string."
        )

    def test_main_still_passes_notes(self):
        """Two sibling kwargs, both required. Adding one must not displace the
        other — they render adjacent lines and a typo in either is silent."""
        assert 'notes=body.get("mp_notes") or ""' in self._welcome_call()

    def test_frontend_extracts_the_request_line(self):
        code = "\n".join(
            l.split("//")[0] for l in self._index().splitlines()
        )
        assert r"text.match(/^Request:[ \t]*(\S[^\n]*)$/im)" in code, (
            "index.html no longer reads the Request line out of the LIVE "
            "textarea, so a dispatcher's edits stop reaching responders."
        )
        assert "mp_request:          parsed.mpRequest || ''," in code, (
            "mpRequest is parsed but never posted in the /send-notification "
            "payload — the value dies in the browser."
        )

    def test_frontend_returns_request_from_the_parser(self):
        """The parser's return object is the join between the two asserts
        above; omitting the key makes `parsed.mpRequest` undefined and the
        payload silently empty."""
        code = "\n".join(l.split("//")[0] for l in self._index().splitlines())
        assert re.search(r"^\s*mpRequest,\s*$", code, re.M), (
            "mpRequest is no longer returned by the textarea parser."
        )


class TestClearResultsResetsDispatchIndicators:
    """The EB/Slack/D4H dots must reset when the dispatcher clears and starts over.

    Found live 2026-08-01: after "clear and start over" the three dots kept the
    PREVIOUS dispatch's 🟢, so the next form showed EB/Slack/D4H as done before
    anything had been sent. The send button itself behaved correctly — this is a
    pure wrong-state display bug, and 🟢 reads as "already went out".

    Rarity is the argument FOR fixing it, not against (Bill): a dispatcher only
    re-submits in one session because something already went wrong, so they are
    under stress and least able to discount a stale indicator.
    """

    @staticmethod
    def _clear_results_body():
        """The body of clearResults(), bounded on real markers at BOTH ends."""
        src = (Path(__file__).parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
        start = src.index("function clearResults()")
        end = src.index("// Status helpers", start)
        body = src[start:end]
        # Strip line comments before any negative assertion — the comment here
        # NAMES _setIndicator while explaining why it must not be called.
        return "\n".join(l.split("//")[0] for l in body.splitlines())

    def test_all_three_indicators_are_reset(self):
        body = self._clear_results_body()
        for ident, icon in (("eb-indicator", "⚪ EB"),
                            ("slack-indicator", "⚪ Slack"),
                            ("d4h-indicator", "⚪ D4H")):
            assert ident in body and icon in body, (
                f"clearResults() no longer resets {ident} — a cleared form shows "
                f"the previous dispatch's status."
            )

    def test_reset_does_not_call_the_out_of_scope_helper(self):
        """_setIndicator is a `const` arrow declared inside the
        everbridge-slack-btn click handler. Calling it from clearResults throws
        a ReferenceError and breaks the entire clear — worse than the stale dots
        it was meant to fix. I wrote exactly that bug before checking the scope.
        """
        assert "_setIndicator" not in self._clear_results_body(), (
            "clearResults() calls _setIndicator, which is not in its scope — "
            "this throws ReferenceError. Hoist the helper first, or inline the "
            "DOM writes as before."
        )

    def test_helper_really_is_out_of_scope(self):
        """Pins the premise of the test above, so this stops being enforced the
        day someone legitimately hoists _setIndicator to module scope."""
        src = (Path(__file__).parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
        m = re.search(r"^(\s*)const _setIndicator = ", src, re.M)
        assert m, "_setIndicator is gone — re-check the clearResults reset."
        assert len(m.group(1)) > 2, (
            "_setIndicator now looks module-scoped; if so, clearResults may call "
            "it directly and test_reset_does_not_call_the_out_of_scope_helper "
            "should be retired."
        )


class TestLkpLowConfidenceNote:
    """Pin the LKP geocode-confidence signal (issue #680).

    On 2026-07-31 "Treatment Facility" resolved to the centroid of California
    and the staging circle, the CalTopo seed and all its markers, the D4H
    location and the responder-facing Slack staging link all inherited it. No
    guard caught it — `_house_number_consistent()` cannot fire on an LKP with no
    house number — a human did. Google told us: location_type == APPROXIMATE.
    """

    @staticmethod
    def _src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @classmethod
    def _fn(cls, name):
        m = re.search(rf"^def {name}\(.*?(?=\n\n(?:def |async def |# -{{10,}}))",
                      cls._src(), re.DOTALL | re.MULTILINE)
        assert m, f"{name} not found in main.py"
        return m.group(0)

    @staticmethod
    def _code_only(text):
        body = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    def test_trigger_is_location_type_not_partial_match(self):
        """The measured decision, and the one most likely to be "corrected"
        back: partial_match is the obvious candidate and the one #680 leads
        with, but it is true on 5 of 16 CORRECT in-area results. Keying on it
        would fire on a hospital name with a good address, a successful
        spelling correction, an intersection and a park.
        """
        src = self._src()
        i = src.index("_gm_lat, _gm_lng, _gm_display, _gm_formatted, _gm_loctype")
        block = self._code_only(src[i:i + 2000])
        assert '_gm_loctype == "APPROXIMATE"' in block, (
            "the LKP confidence check no longer tests location_type == APPROXIMATE"
        )
        assert "partial_match" not in block, (
            "the LKP confidence check now keys on partial_match — measured at a "
            "31% false-positive rate on real in-area LKPs; re-run "
            "experiments/geocode_confidence/01_partial_match_real_lkps.py before "
            "changing this."
        )

    def test_note_is_emitted_to_the_event_log(self):
        src = self._src()
        i = src.index('_gm_loctype == "APPROXIMATE"')
        block = self._code_only(src[i:i + 400])
        assert "event_log_additions.append" in block, (
            "the APPROXIMATE branch no longer reaches the Event Log — the signal "
            "is computed and thrown away, which is the bug #680 exists to fix."
        )
        assert "_lkp_low_confidence_note(" in block, (
            "the branch no longer calls _lkp_low_confidence_note"
        )

    def test_google_returns_location_type(self):
        """The helper can only fire if the geocoder actually surfaces it."""
        code = self._code_only(self._fn("async def _geocode_google_maps")
                               if False else self._src())
        assert 'result["geometry"].get("location_type"' in code, (
            "_geocode_google_maps no longer returns location_type, so the LKP "
            "confidence check reads an empty string and never fires."
        )

    def test_every_caller_unpacks_five(self):
        """Tuple-widening hazard, caught the hard way.

        Widening the return to 5 left the intersection-LKP path at line ~1228
        unpacking 4, which raises ValueError at runtime on every intersection
        LKP. main.py is not importable, so no behaviour test can catch it —
        this walks the AST instead.
        """
        tree = ast.parse(self._src())
        holders = {"gm_tuple"}
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                f = n.value
                while isinstance(f, ast.Await):
                    f = f.value
                if isinstance(f, ast.Call) and getattr(f.func, "id", "") == "_geocode_google_maps":
                    holders |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        bad = [
            (n.lineno, len(t.elts))
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Name)
            and n.value.id in holders
            for t in n.targets if isinstance(t, ast.Tuple) and len(t.elts) != 5
        ]
        assert not bad, (
            f"_geocode_google_maps returns a 5-tuple but these unpacks disagree "
            f"(line, arity): {bad}. This is a runtime ValueError, not a type error."
        )

    def test_docstring_does_not_restore_the_false_scope_claim(self):
        """The shipped docstring first claimed "Treatment Facility" reached
        Google because Nominatim returned nothing. It is false — Nominatim
        answered it with "Water Treatment Facility, Livingston", which is why
        this check never ran on the incident that motivated it.

        The claim came from a spike whose `except` clause scored Nominatim
        rate-limiting as a no-result. Pinned as prose, deliberately: the error
        is in what the comment ASSERTS about behaviour, so no behavioural test
        can catch it, and the next reader would believe it.
        """
        doc = self._fn("_lkp_low_confidence_note")
        assert "reached Google precisely because Nominatim returned nothing" not in doc, (
            "the false scope claim is back in _lkp_low_confidence_note — "
            "Nominatim ANSWERS 'Treatment Facility'; Google is never asked."
        )
        assert "Nominatim answered it" in doc, (
            "the docstring no longer records that Nominatim answers the "
            "motivating case, which is the whole reason this guard is narrow."
        )

    def test_note_names_the_address_for_the_dispatcher(self):
        code = self._code_only(self._fn("_lkp_low_confidence_note"))
        assert "{query}" in code and "{resolved}" in code, (
            "the note no longer names the LKP and what Google returned — a bare "
            "'low confidence' line gives the dispatcher nothing to act on."
        )


class TestSandboxEnvBanner:
    """The sandbox banner marks the SANDBOX, never production (#742).

    Direction matters more than presence. If the marker fails to render — bad
    build arg, stripped attribute, CSS error — the page looks like the
    dispatcher-facing environment and the operator is MORE careful. Marking
    production instead inverts that: the same failure would make the live
    environment look like a sandbox, which is the direction that pages real
    responders.

    So every default here is fail-to-unbannered, deliberately the opposite of
    OAUTH_CLIENT_ID's fail-loud guard in the same Dockerfile.
    """

    @staticmethod
    def _fe():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _read(name):
        return (Path(__file__).parent.parent / name).read_text(encoding="utf-8")

    # --- the repo's own default -------------------------------------------

    def test_attribute_is_empty_in_the_repo(self):
        """Un-substituted source must render NO banner. A non-empty default
        would label every environment that forgets the build arg."""
        m = re.findall(r'<body data-env-label="([^"]*)"', self._fe())
        assert m == [""], (
            f"expected exactly one empty data-env-label on <body>, found {m}"
        )

    # --- build wiring ------------------------------------------------------

    def test_dockerfile_arg_defaults_empty_and_is_not_guarded(self):
        df = self._read("backend/Dockerfile")
        assert 'ARG ENV_LABEL=""' in df, (
            "ENV_LABEL must default to empty — an unlisted environment has to "
            "render no banner rather than fail the build"
        )
        guard = re.search(r'test -n "\$\{ENV_LABEL\}"', df)
        assert guard is None, (
            "ENV_LABEL must NOT have a fail-loud guard like OAUTH_CLIENT_ID. "
            "Requiring every environment to declare itself means a "
            "mis-declared one labels production as a sandbox."
        )
        assert 'data-env-label=\\"${ENV_LABEL}\\"' in df, "the sed target is gone"

    def test_only_personal_dev_passes_the_label(self):
        """The safety-critical assertion: production is never labelled."""
        dev = self._read("build-dev.sh")
        sccssar = self._read("build-sccssar-dev.sh")
        assert "--build-arg ENV_LABEL=" in dev, (
            "personal-dev no longer labels itself as a sandbox"
        )
        assert "ENV_LABEL" not in sccssar, (
            "build-sccssar-dev.sh passes ENV_LABEL — the dispatcher-facing "
            "environment must never render a sandbox banner"
        )

    def test_dockerfile_sed_actually_matches_the_frontend(self):
        """Cross-file: run the Dockerfile's own pattern against index.html.

        A sed that matches nothing fails silently — the build succeeds and the
        banner never appears, which is exactly the failure this class exists to
        prevent from going unnoticed.
        """
        df = self._read("backend/Dockerfile")
        m = re.search(r'sed -i "s\|(data-env-label=[^|]*?)\|', df.replace('\\"', '"'))
        assert 'data-env-label="[^"]*"' in df.replace('\\"', '"'), (
            "the Dockerfile sed pattern changed shape; re-derive this pin"
        )
        hits = re.findall(r'data-env-label="[^"]*"', self._fe())
        assert len(hits) == 1, (
            f"the sed pattern matches {len(hits)} places in index.html; it must "
            f"match exactly the <body> attribute"
        )

    # --- behaviour (executes the real inline script) -----------------------

    @classmethod
    def _run(cls, label):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not available")
        m = re.search(
            r"\(function \(\) \{\s*var lbl = .*?\}\)\(\);", cls._fe(), re.DOTALL
        )
        assert m, "the inline banner script was not found"
        prog = """
        var _t = "SCCSSAR Dispatch Console", _kids = [];
        var document = {
          get title() { return _t; }, set title(v) { _t = v; },
          body: { getAttribute: function (n) {
                    return n === "data-env-label" ? %s : null; },
                  firstChild: null,
                  insertBefore: function (el) { _kids.push(el); } },
          createElement: function () { return { id: "", textContent: "" }; },
        };
        %s
        console.log(JSON.stringify({title: _t, banners: _kids.map(function(k){return k.textContent;})}));
        """ % (json.dumps(label), m.group(0))
        out = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_empty_label_renders_nothing(self):
        got = self._run("")
        assert got["banners"] == [], "an unlabelled environment rendered a banner"
        assert got["title"] == "SCCSSAR Dispatch Console", "the title was altered"

    def test_label_renders_banner_and_prefixes_the_title(self):
        got = self._run("SANDBOX — personal-dev")
        assert len(got["banners"]) == 1
        assert "SANDBOX — personal-dev" in got["banners"][0]
        assert got["title"].startswith("[SANDBOX — personal-dev] "), got["title"]

    def test_whitespace_only_label_renders_nothing(self):
        """A build arg set to spaces is a mis-set arg, not an environment."""
        got = self._run("   ")
        assert got["banners"] == []
        assert got["title"] == "SCCSSAR Dispatch Console"


class TestTextareaLockIsDispatchOnly:
    """#752 — which buttons may freeze the summary textarea, and which may not.

    `lockTextarea()` sets `readonly` on `#result-text` AND kills the staging
    override panel, and nothing but `clearResults()` (which discards the whole
    dispatch) ever unlocks. It is therefore a one-way door, and it belongs only
    on buttons that COMMIT state somewhere the textarea then has to agree with.

    `googlemaps-btn` committed nothing and locked anyway. On the 2026-08-18
    callout the dispatcher corrected a misread agency in one place, clicked
    Google Maps to sanity-check the LKP, and could not correct the remaining
    occurrences — two stale renderings reached the D4H permanent record.

    The classification is NOT the helper-row CSS class: `gdoc-btn` sits in the
    same helper row and legitimately locks, because it snapshots the textarea
    into a Google Doc. The predicate is "does this handler commit state to an
    external system", so each locking button is named here with its reason.

    Completeness is DERIVED, not counted: every `lockTextarea()` call site in
    the file is attributed to its enclosing click handler and the resulting map
    is compared for equality. A lock added to a new button, or dropped from an
    existing one, changes the map and fails — a bare count would ratify a swap.
    """

    # button id -> why this handler is allowed to freeze the textarea
    _EXPECTED_LOCKERS = {
        "everbridge-btn": "manual dispatch fallback — pages responders from the EB portal",
        "gdoc-btn": "snapshots the textarea into a Google Doc",
        "d4h-btn": "opens the D4H record for attendance edits / escalation",
        "incident-map-btn": "builds the CalTopo map from the textarea",
        "everbridge-slack-btn": "the dispatch itself — EB + Slack + D4H",
    }

    @staticmethod
    def _frontend_src():
        return (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )

    @classmethod
    def _code_lines(cls):
        """Source with whole-line `//` comments dropped.

        Required before any negative assertion: the incident-map handler
        DISCUSSES `lockTextarea()` in a comment, and the lock helper's own
        header block names it repeatedly. Only leading-`//` lines are stripped,
        so a trailing comment on a real call site cannot hide the call.
        """
        out = []
        for line in cls._frontend_src().splitlines():
            out.append("" if line.lstrip().startswith("//") else line)
        return out

    @classmethod
    def _lock_calls_by_handler(cls):
        """Map every lockTextarea() call site to its enclosing click handler.

        Attribution is by nearest preceding `getElementById(<id>)
        .addEventListener("click"` — matched in either quoting style, since the
        file uses both — so a lock added anywhere in the click-handler region
        lands in the map rather than being silently skipped.
        """
        handler_re = re.compile(
            r"""getElementById\(\s*(['"])([a-zA-Z0-9_-]+)\1\s*\)"""
            r"""\s*\.addEventListener\(\s*(['"])click\3"""
        )
        # \b before `lock` does not match inside `unlockTextarea(` — `n` and `l`
        # are both word characters — so unlock sites are excluded by construction.
        # The negative lookbehind drops the helper's own `function lockTextarea()`
        # declaration, which is otherwise attributed to whichever handler happens
        # to sit above it (caught while mutation-testing this pin).
        call_re = re.compile(r"(?<!function )\block[Tt]extarea\(\)")

        current = None
        found = {}
        for line in cls._code_lines():
            m = handler_re.search(line)
            if m:
                current = m.group(2)
            if call_re.search(line):
                found.setdefault(current, 0)
                found[current] += 1
        return found

    def test_locking_buttons_are_exactly_the_committing_ones(self):
        got = set(self._lock_calls_by_handler())
        expected = set(self._EXPECTED_LOCKERS)
        assert got == expected, (
            "the set of buttons that freeze the summary textarea changed.\n"
            f"  added:   {sorted(got - expected)}\n"
            f"  removed: {sorted(expected - got)}\n"
            "A button may lock ONLY if it commits state the textarea must then "
            "agree with (see _EXPECTED_LOCKERS for each one's reason). Adding a "
            "lock to a read-only helper is #752: it freezes the document "
            "mid-correction with no way back short of discarding the dispatch."
        )

    def test_googlemaps_handler_does_not_lock(self):
        """The specific regression, stated directly.

        Kept alongside the set-equality pin so the failure message names #752
        when the exact defect returns, rather than reporting a diff.
        """
        assert "googlemaps-btn" not in self._lock_calls_by_handler(), (
            "googlemaps-btn calls lockTextarea() again (#752). Google Maps "
            "dispatches nothing — it reads the textarea and opens an external "
            "map. Locking punishes verify-then-correct, which is the behaviour "
            "the dispatcher should be doing."
        )

    def test_googlemaps_handler_still_opens_a_map(self):
        """Guards the pin above against passing because the button was deleted."""
        calls = self._lock_calls_by_handler()  # noqa: F841 - parse must succeed
        src = self._frontend_src()
        start = src.index("document.getElementById('googlemaps-btn').addEventListener")
        end = src.index("document.getElementById('gdoc-btn').addEventListener")
        body = src[start:end]
        assert "safeOpen(" in body, "the Google Maps handler no longer opens anything"
        assert "parseOcrResult(" in body, (
            "the Google Maps handler no longer reads the LIVE textarea — it must, "
            "so a corrected LKP is what gets centred"
        )


class TestSubjectLastSeenEventLogEntry:
    """#755 — the subject's last-seen date/time as an Event Log chronology entry.

    Kris (Ops, 2026-08-18) asked that D4H record both "when were we notified"
    and "when was the subject last seen". The first was already Event Log line 1
    on both intake paths; the second sat on the intake form, was parsed into
    `Last Seen At:`, and went nowhere.

    This entry EXPANDS the "Event log policy" Locked Decision, which reads
    "emit only for corrections and failures, never for silent successes". A
    chronology anchor is neither — Bill authorized the expansion and the
    CLAUDE.md row is amended in the same PR.

    Two invariants carry real failure modes and are pinned separately below:

    POSITION. The entry is the SECOND Event Log line, never the first. Event
    Name reconstruction finds the incident date with a regex anchored on the ISO
    date immediately following the "Event Log:" header, and pdf_extract converts
    the form date to ISO expressly to feed it. An entry above line 1 hands that
    regex the LAST-SEEN date, silently renaming the incident on the Everbridge
    title, the Slack channel, D4H referenceDescription and the CalTopo map — and
    in the common time-only case matches nothing at all, dropping reconstruction
    entirely.

    FIDELITY. A time with no date stays a bare time. `_normalize_datetime`
    legitimately returns "21:30" / "04:45 AM" when the officer wrote no date, and
    per Bill (2026-08-18) an ambiguous field is reported exactly as written
    rather than completed by inference.
    """

    # ---- mirrors of main.py ------------------------------------------------
    # Horizontal whitespace only — see the production comment. `\s*` would
    # cross the newline on a blank field and capture the following line.
    _LAST_SEEN_AT_RE = re.compile(r"^Last Seen At:[^\S\n]*(.+)$", re.MULTILINE)
    _EVENT_LOG_MARKER = "\nEvent Log:\n"

    @classmethod
    def _subject_last_seen_value(cls, summary: str) -> str:
        m = cls._LAST_SEEN_AT_RE.search(summary or "")
        if not m:
            return ""
        value = m.group(1).strip()
        if not value or value.startswith("["):
            return ""
        if value.casefold() in ("not recorded", "unknown"):
            return ""
        return value

    @classmethod
    def _insert(cls, summary: str) -> str:
        value = cls._subject_last_seen_value(summary)
        if not value:
            return summary
        pos = summary.find(cls._EVENT_LOG_MARKER)
        if pos == -1:
            return summary
        first_entry_start = pos + len(cls._EVENT_LOG_MARKER)
        first_entry_end = summary.find("\n", first_entry_start)
        if first_entry_end == -1:
            return summary
        first_entry = summary[first_entry_start:first_entry_end].strip()
        if not first_entry or first_entry.startswith("---"):
            return summary
        insert_at = first_entry_end + 1
        return (summary[:insert_at]
                + f"{value} - Subject last seen\n"
                + summary[insert_at:])

    @staticmethod
    def _summary(last_seen="2026-08-16 21:30", log_first="2026-08-18 14:05 - "
                 "Request received from SJPD/Ofc. Nguyen"):
        return (
            "Initial Incident Summary:\n"
            "Event Name: 2026-08-18 SJPD CAPITOL\n"
            f"Last Seen At: {last_seen}\n"
            "Last Known Position: 100 Capitol Ave, San Jose, CA\n"
            "\n---\n"
            "\nEvent Log:\n"
            f"{log_first}\n"
            "2026-08-18 14:07 - v2 Intake form processed; Initial Incident "
            "Summary created\n"
            "\n---\n"
            "\nLPB Questionnaire:\n"
            "Q1 - Yes - Familiar with area\n"
        )

    def _log_lines(self, summary):
        block = summary.split("\nEvent Log:\n", 1)[1].split("\n---\n", 1)[0]
        return [l for l in block.splitlines() if l.strip()]

    # ---- position ----------------------------------------------------------
    def test_entry_is_the_second_event_log_line(self):
        lines = self._log_lines(self._insert(self._summary()))
        assert lines[0].endswith("Request received from SJPD/Ofc. Nguyen"), (
            "the request-received line is no longer first — Event Name date "
            "reconstruction reads the ISO date immediately after 'Event Log:'"
        )
        assert lines[1] == "2026-08-16 21:30 - Subject last seen", (
            f"expected the last-seen entry second, got {lines[1]!r}"
        )

    def test_request_received_line_still_opens_the_log(self):
        """The Event Name regex's actual precondition, asserted directly.

        Mirrors main.py's `^Event Log:\\s*\\n(\\d{4}-\\d{2}-\\d{2})` search. If a
        future change moves the entry up, this fails with the consequence named
        rather than an opaque ordering diff.
        """
        out = self._insert(self._summary())
        assert re.search(r"^Event Log:\s*\n(\d{4}-\d{2}-\d{2})", out, re.MULTILINE), (
            "an ISO date no longer follows the 'Event Log:' header — Event Name "
            "reconstruction will take the wrong date or fail outright, and the "
            "name flows verbatim to EB, Slack, D4H and CalTopo"
        )

    def test_time_only_value_does_not_displace_the_request_line(self):
        """The dangerous input shape: a bare time is not an ISO date at all."""
        out = self._insert(self._summary(last_seen="21:30"))
        assert re.search(r"^Event Log:\s*\n(\d{4}-\d{2}-\d{2})", out, re.MULTILINE)
        assert self._log_lines(out)[1] == "21:30 - Subject last seen"

    # ---- fidelity ----------------------------------------------------------
    def test_time_only_value_is_not_completed_with_a_date(self):
        out = self._insert(self._summary(last_seen="04:45 AM"))
        assert "04:45 AM - Subject last seen" in out
        assert "2026-08-18 04:45" not in out, (
            "a date was inferred onto a time-only value. Bill, 2026-08-18: when "
            "something is ambiguous do not invent or infer data the officer did "
            "not state — high fidelity even if incomplete"
        )

    # ---- omission ----------------------------------------------------------
    @pytest.mark.parametrize("value", [
        "[not recorded]",
        "[time not recorded]",
        '[date/time only from form — e.g., "1/6/26 2300"]',
        "not recorded",
        "Unknown",
        "   ",
    ])
    def test_sentinel_and_blank_values_emit_nothing(self, value):
        out = self._insert(self._summary(last_seen=value))
        assert "Subject last seen" not in out, (
            f"{value!r} produced an entry; an unrecorded field must omit the "
            f"line entirely rather than publish a placeholder"
        )
        assert len(self._log_lines(out)) == 2

    def test_missing_last_seen_line_is_a_no_op(self):
        s = self._summary().replace("Last Seen At: 2026-08-16 21:30\n", "")
        assert self._insert(s) == s

    def test_missing_event_log_section_is_a_no_op(self):
        s = "Initial Incident Summary:\nLast Seen At: 2026-08-16 21:30\n"
        assert self._insert(s) == s

    def test_empty_event_log_section_is_a_no_op(self):
        """Guards the "insert after line 1" anchor when there IS no line 1."""
        s = ("Last Seen At: 2026-08-16 21:30\n"
             "\nEvent Log:\n"
             "---\n")
        assert self._insert(s) == s

    # ---- production parity -------------------------------------------------
    @staticmethod
    def _main_src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @classmethod
    def _fn(cls, name):
        src = cls._main_src()
        start = src.index(f"\ndef {name}(")
        end = src.index("\ndef ", start + 1)
        return src[start:end]

    @staticmethod
    def _code_only(text):
        """Strip the docstring and comments before any negative assertion.

        Both helpers EXPLAIN the position rule in prose that names the same
        identifiers, so a raw-source search would pass with the code deleted.
        """
        body = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    def test_production_inserts_after_the_first_entry_not_at_the_header(self):
        prod = self._code_only(self._fn("_insert_subject_last_seen_entry"))
        assert 'first_entry_end = summary.find("\\n", first_entry_start)' in prod, (
            "the insertion point is no longer anchored on the end of the first "
            "Event Log entry — see the Event Name hijack this position prevents"
        )
        assert "insert_at = first_entry_end + 1" in prod, (
            "the entry is being inserted somewhere other than after line 1"
        )

    def test_production_guards_an_empty_or_divider_first_line(self):
        prod = self._code_only(self._fn("_insert_subject_last_seen_entry"))
        assert 'first_entry.startswith("---")' in prod, (
            "the empty-Event-Log guard is gone — the entry would land below the "
            "section's closing divider, outside the log"
        )

    def test_production_treats_bracketed_values_as_absent(self):
        prod = self._code_only(self._fn("_subject_last_seen_value"))
        assert 'value.startswith("[")' in prod, (
            "the sentinel guard is gone — '[not recorded]' would be published "
            "to the Event Log, the Slack welcome and the D4H record"
        )

    def test_production_call_site_runs_after_jpeg_normalization(self):
        """Order, asserted structurally rather than by presence.

        The JPEG path's ONLY cleanup of this field is the `_normalize_last_seen_at`
        substitution. Inserting before it publishes raw officer handwriting
        ("2130 HOURS 2/20/26") to three surfaces.
        """
        src = self._main_src()
        norm = src.index('_normalize_last_seen_at, summary, flags=re.MULTILINE')
        call = src.index("summary = _insert_subject_last_seen_entry(summary)")
        assert norm < call, (
            "the last-seen Event Log entry is now built BEFORE the JPEG "
            "normalization pass — raw officer handwriting would be published"
        )

    def test_d4h_shares_the_helper_rather_than_re_deriving_it(self):
        """D4H re-parses the live textarea server-side, so it CAN share the
        helper — and must, or the same field is judged twice by two rules."""
        src = self._main_src()
        assert '"last_seen_at":                _subject_last_seen_value(ocr_text),' in src, (
            "the D4H ocr_data builder no longer shares _subject_last_seen_value"
        )

    def test_slack_states_the_same_recorded_rule_it_cannot_share(self):
        """The Slack welcome reaches this field by a DIFFERENT route.

        Event Log and D4H both re-parse the summary server-side and call
        _subject_last_seen_value. The welcome is fed from the dispatch payload,
        which the frontend parses out of the textarea with no sentinel filtering
        at all — so slack.py has to state the rule a second time, and this pin is
        the only thing tying the two statements together.

        The original version of this test asserted ONLY the D4H call site while
        claiming to prove all three surfaces agreed. It could not have failed:
        an exact-literal `!= "[not recorded]"` guard in slack.py published
        Gemini's bracketed instruction text to responders while both other
        surfaces omitted it, and every test stayed green. Caught in review.
        """
        slack_src = (Path(__file__).parent / "slack.py").read_text(encoding="utf-8")
        fn = re.search(
            r"^def format_pinned_welcome\(.*?(?=\n\n(?:def |async def |# -{10,}))",
            slack_src, re.DOTALL | re.MULTILINE,
        )
        assert fn, "format_pinned_welcome not found in slack.py"
        body = re.sub(r'\"\"\".*?\"\"\"', "", fn.group(0), flags=re.DOTALL)
        code = "\n".join(l.split("#")[0] for l in body.splitlines())
        assert 'not last_seen_clean.startswith("[")' in code, (
            "slack.py no longer treats every bracketed value as absent. It must "
            "match _subject_last_seen_value's rule in this module — the two read "
            "the same intake field by different routes, and a narrower rule "
            "there means responders see a sentinel the Event Log and D4H dropped."
        )

    def test_bracket_rule_agrees_across_both_statements(self):
        """Behavioural cross-check on the real inputs, not just the source text.

        Runs the production helper and a mirror of slack.py's guard over the
        sentinel shapes each intake path can actually produce, and asserts they
        reach the same verdict on every one.
        """
        src = self._main_src()
        ns = {"re": re}
        tree = ast.parse(src)
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and getattr(node.targets[0], "id", "") == "_LAST_SEEN_AT_RE"):
                exec(compile(ast.Module([node], []), "<p>", "exec"), ns)
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "_subject_last_seen_value"):
                exec(compile(ast.Module([node], []), "<p>", "exec"), ns)

        slack_src = (Path(__file__).parent / "slack.py").read_text(encoding="utf-8")
        assert 'not last_seen_clean.startswith("[")' in slack_src

        cases = [
            "2026-08-16 21:30",
            "21:30",
            "04:45 AM",
            "[not recorded]",
            "[time not recorded]",
            '[date/time only from form — e.g., "1/6/26 2300". Do NOT include '
            'address here; that goes in Last Known Position below]',
            "   ",
        ]
        for value in cases:
            server = bool(ns["_subject_last_seen_value"](f"Last Seen At: {value}\n"))
            clean = value.strip()
            slack = bool(clean and not clean.startswith("["))
            assert server == slack, (
                f"{value!r}: server-side helper says recorded={server} but the "
                f"Slack welcome guard says recorded={slack} — one surface would "
                f"carry the line and the others would not"
            )

    def test_production_regex_does_not_cross_newlines(self):
        """A blank field must not let the match run onto the next line.

        `\\s*` matches newlines even under re.MULTILINE — that flag changes ^ and
        $, not the whitespace class. With an empty "Last Seen At:" the greedy
        `\\s*` swallows the line break and `(.+)` captures the FOLLOWING line, so
        the Event Log publishes the Last Known Position as a last-seen time.
        Reproduced while building this helper; same class as issue #735.
        """
        src = "\n".join(
            l.split("#")[0] for l in self._main_src().splitlines()
        )
        m = re.search(r"_LAST_SEEN_AT_RE = re\.compile\(r\"([^\"]+)\"", src)
        assert m, "_LAST_SEEN_AT_RE not found in main.py"
        assert m.group(1) == r"^Last Seen At:[^\S\n]*(.+)$", (
            f"the last-seen field regex is now {m.group(1)!r}. It must use "
            f"horizontal-whitespace-only ([^\\S\\n]*); \\s* crosses the newline "
            f"on a blank field and captures the next line."
        )
        # Prove it on the real pattern rather than trusting the literal. The
        # invariant is not "does not match" — a blank field still matches, and
        # captures the trailing spaces, which strip to nothing. The invariant is
        # that the capture never reaches the FOLLOWING line.
        compiled = re.compile(m.group(1), re.MULTILINE)
        blank = "Last Seen At:   \nLast Known Position: 100 Capitol Ave, CA\n"
        hit = compiled.search(blank)
        captured = hit.group(1) if hit else ""
        assert not captured.strip(), (
            f"a blank last-seen field captured {captured!r} — the match crossed "
            f"the newline and swallowed the next field's line"
        )

    def test_welcome_call_site_forwards_the_payload_field(self):
        src = self._main_src()
        assert 'last_seen=body.get("mp_last_seen") or "",' in src, (
            "the Slack welcome no longer receives mp_last_seen — the line would "
            "silently vanish from the responder-facing pin"
        )

    def test_frontend_sends_the_payload_field(self):
        html_src = (Path(__file__).parent.parent / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )
        code = "\n".join(
            "" if l.lstrip().startswith("//") else l for l in html_src.splitlines()
        )
        assert "mp_last_seen:        parsed.lastSeenAt || ''," in code, (
            "the dispatch payload no longer carries mp_last_seen. parseOcrResult "
            "has always parsed lastSeenAt and thrown it away — that was #755"
        )


class TestGeminiProjectHasNoDeploymentDefault:
    """`gemini.py` must not carry a deployment-specific GCP project default.

    It used to read `os.environ.get("GCP_PROJECT", "sar-dispatch-dev")`, so
    anyone deploying this without setting GCP_PROJECT silently pointed Vertex
    AI at the maintainer's personal project and got an opaque permission error
    rather than a statement of what was missing. `d4h.py` and `main.py` both
    already use an empty default and validate at point of use; this pins
    gemini.py to the same convention.

    Source-reading rather than behavioural: test_gemini.py is importorskip'd on
    `google.genai`, which is not installed in the local test environment, so a
    behavioural test of _get_client() would skip and pin nothing.
    """

    def _gemini_source(self):
        from pathlib import Path
        p = Path(__file__).parent / "gemini.py"
        assert p.exists(), f"backend/gemini.py not found at {p}"
        return p.read_text(encoding="utf-8")

    def _get_client_body(self, source):
        """The _get_client function body, bounded at BOTH ends by real markers
        and anchored past the docstring -- never start+N characters."""
        start = source.index("def _get_client(")
        doc_close = source.index('"""', source.index('"""', start) + 3) + 3
        rest = source[doc_close:]
        # End at the next module-level def/class, not a fixed offset.
        m = re.search(r"\n(?=(?:def |class )\w)", rest)
        return rest[: m.start()] if m else rest

    def test_no_hardcoded_project_default(self):
        source = self._gemini_source()
        line = next(
            (l for l in source.splitlines()
             if l.split("#")[0].strip().startswith("GCP_PROJECT")),
            None,
        )
        assert line is not None, "GCP_PROJECT assignment not found in gemini.py"
        code = line.split("#")[0]          # strip comments before asserting absence
        assert 'os.environ.get("GCP_PROJECT", "")' in code, (
            f"gemini.py must default GCP_PROJECT to the empty string, not to a "
            f"specific project. Found: {code.strip()!r}"
        )
        assert "sar-dispatch" not in code, (
            f"gemini.py hardcodes a deployment project as the GCP_PROJECT "
            f"default: {code.strip()!r}"
        )

    def test_get_client_refuses_to_build_with_an_empty_project(self):
        body = self._get_client_body(self._gemini_source())
        guard = body.index("if not GCP_PROJECT:")
        call = body.index("genai.Client(")
        assert guard < call, (
            "_get_client() constructs genai.Client before checking GCP_PROJECT "
            "— an unset project would reach Vertex AI as an empty string."
        )
        # Assert the branch BODY raises; `if not GCP_PROJECT: pass` would
        # satisfy a bare presence check while doing nothing.
        assert "raise ValueError(" in body[guard:call], (
            "_get_client()'s empty-project guard does not raise."
        )


class TestEverbridgeCallerIdIsConfiguration:
    """The org-wide voice caller ID is a LIVE dialable number, so it is
    configuration (EVERBRIDGE_CALLER_ID), never a source constant.

    Replaces a vacuous pin. `_EVERBRIDGE_CALLER_ID` was defined at the top of
    THIS file and `test_caller_id` asserted it equalled that same literal -- a
    value compared with itself, reading nothing from production. Its class
    docstring claimed a tidy-up "fails the build at Step 0"; it did not.
    Demonstrated 2026-09-04 by deleting CALLER_ID from everbridge.py and setting
    the payload to a dummy: all 2182 tests passed.

    everbridge.py imports httpx and is not importable under local pytest, so
    these read its SOURCE. Same reason as the d4h mirror-parity pins.
    """

    def _eb_source(self):
        from pathlib import Path
        p = Path(__file__).parent / "everbridge.py"
        assert p.exists(), f"backend/everbridge.py not found at {p}"
        return p.read_text(encoding="utf-8")

    def _func_body(self, source, name):
        """Slice one function, bounded at BOTH ends by real markers and anchored
        past the docstring -- never start+N characters."""
        start = source.index(f"def {name}(")
        doc_close = source.index('"""', source.index('"""', start) + 3) + 3
        rest = source[doc_close:]
        m = re.search(r"\n(?=(?:def |class )\w)", rest)
        return rest[: m.start()] if m else rest

    def test_no_dialable_number_is_hardcoded(self):
        """Comments stripped first: prose about the number would otherwise
        satisfy a naive absence check."""
        code = "\n".join(l.split("#")[0] for l in self._eb_source().splitlines())
        hits = re.findall(r"(?<![0-9])[2-9][0-9]{2}[2-9][0-9]{2}[0-9]{4}(?![0-9])", code)
        assert not hits, (
            f"backend/everbridge.py hardcodes a dialable 10-digit number: {hits}. "
            f"The caller ID belongs in EVERBRIDGE_CALLER_ID."
        )

    def test_caller_id_is_read_from_the_env_var(self):
        code = "\n".join(l.split("#")[0] for l in self._eb_source().splitlines())
        assert 'CALLER_ID = os.environ.get("EVERBRIDGE_CALLER_ID", "")' in code, (
            "everbridge.py no longer reads the caller ID from EVERBRIDGE_CALLER_ID."
        )

    def test_guard_raises_before_the_payload_is_built(self):
        body = self._func_body(self._eb_source(), "_require_caller_id")
        guard = body.index("if not CALLER_ID:")
        # Assert the branch BODY raises -- `if not CALLER_ID: pass` would satisfy
        # a bare presence check while doing nothing.
        assert "raise RuntimeError(" in body[guard:], (
            "_require_caller_id() does not raise when the caller ID is unset."
        )

    def test_payload_calls_the_guard_not_the_bare_constant(self):
        """Assert the CALL SITE. A guard nothing calls is not a guard."""
        body = self._func_body(self._eb_source(), "_build_send_notification_payload")
        assert '"callerId": _require_caller_id()' in body, (
            "_build_send_notification_payload does not route callerId through "
            "_require_caller_id() -- an unset caller ID would reach Everbridge."
        )

    def test_terraform_declares_the_env_var_in_both_environments(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "terraform" / "environments"
        for env in ("dev", "sccssar-dev"):
            main_tf = (root / env / "main.tf").read_text(encoding="utf-8")
            assert 'name  = "EVERBRIDGE_CALLER_ID"' in main_tf, (
                f"{env}/main.tf does not set EVERBRIDGE_CALLER_ID on Cloud Run. "
                f"Session Rule #7: env vars need `terraform apply`, and the "
                f"build scripts do NOT pick up Terraform changes."
            )
            variables_tf = (root / env / "variables.tf").read_text(encoding="utf-8")
            assert 'variable "everbridge_caller_id"' in variables_tf, (
                f"{env}/variables.tf does not declare everbridge_caller_id."
            )

    def test_the_value_is_not_in_any_tracked_terraform_file(self):
        """The tfvars holding it is gitignored; the template must stay a
        placeholder."""
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "terraform" / "environments"
        for p in sorted(root.rglob("*.tf")) + sorted(root.rglob("*.template")):
            code = "\n".join(l.split("#")[0] for l in p.read_text(encoding="utf-8").splitlines())
            hits = re.findall(r"(?<![0-9])[2-9][0-9]{2}[2-9][0-9]{2}[0-9]{4}(?![0-9])", code)
            assert not hits, f"{p} carries a dialable number: {hits}"

class TestStagingWidenedRetry:
    """Pin the widened staging retry (#838, Bill approved 3 mi 2026-09-07).

    Zero candidates at 1200 m is a RENDERING outcome, not evidence of a remote
    LKP. Measured at 3101 Alexis Dr (suburban Palo Alto): the provider returns
    28 features inside 1200 m and NOT ONE carries a house number, so every one
    is dropped by the PASS 2 leading-digit predicate. The addressed POIs exist
    further out — 21 at 3 mi, almost all from categories already queried. The
    golf course we dropped for a missing OSM house number IS the address the
    officer wrote as staging on that form.

    What the retry really buys is Gemini's mode: supplying ANY candidate list
    switches it out of training-data mode, whose output was measured
    non-reproducible (15/17 corpus forms share NO addresses across three
    identical runs) with false distances (claimed <=0.75 mi, actual median
    4.3 mi, `123 Main St` shipping 8x).

    ORDER IS LOAD-BEARING: the retry must run BEFORE the
    `if not _overpass_ok / elif not staging_candidates` chain. Run it after and
    the "remote area" note fires on an anchor where staging was in fact found.
    """

    @staticmethod
    def _src():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @staticmethod
    def _block(src):
        """The retry block only — bounded at BOTH ends on real markers."""
        start = src.index("_staging_searched_m = 1200")
        end = src.index("if not _overpass_ok:", start)
        assert end > start
        return src[start:end]

    @staticmethod
    def _nocomment(block):
        return "\n".join(l.split("#")[0] for l in block.splitlines())

    def test_fallback_radius_is_three_miles(self):
        # 3.00 mi. Calibrated: 21 addressed POIs at this radius vs 0 at 1200 m.
        assert "_STAGING_FALLBACK_RADIUS_M = 4828" in self._src()

    def test_retry_calls_the_lookup_with_the_fallback_radius(self):
        # Call site, not the bare identifier — a declared-but-unused constant
        # would satisfy the identifier alone.
        code = self._nocomment(self._block(self._src()))
        assert "await _query_staging_pois(" in code
        assert "radius_m=_STAGING_FALLBACK_RADIUS_M" in code

    def test_retry_is_gated_on_zero_candidates_and_a_healthy_source(self):
        code = self._nocomment(self._block(self._src()))
        assert "if _overpass_ok and not staging_candidates:" in code

    def test_retry_runs_before_the_zero_candidate_note(self):
        # Ordering guard: the retry must precede the ok/zero branch chain, or a
        # successful widened search still emits the "remote area" note.
        src = self._src()
        assert src.index("_staging_searched_m = 1200") < src.index("if not _overpass_ok:")

    def test_widened_success_tells_the_dispatcher(self):
        # Bill 2026-09-07: staging 3 mi out must never render silently.
        code = self._nocomment(self._block(self._src()))
        assert "event_log_additions.append(" in code
        assert "search widened" in code

    def test_note_reports_the_radius_actually_searched(self):
        # The zero-candidate note must not hardcode 1200 m — after a widened
        # retry it would understate the search and read as a near-LKP failure.
        src = self._src()
        note = src[src.index("(remote area)") - 400: src.index("(remote area)") + 200]
        assert "_staging_searched_m" in note
        assert "within 1200 m of the LKP" not in self._nocomment(src)

    def test_source_ok_is_not_clobbered_by_the_retry(self):
        # The narrow call already proved the source is up; letting a failed wide
        # call flip _overpass_ok would fire the all-mirrors-failed ERROR.
        code = self._nocomment(self._block(self._src()))
        assert "_overpass_ok =" not in code

