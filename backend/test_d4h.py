# backend/test_d4h.py
"""test_d4h.py — pure-logic tests for d4h.py helpers.

Per backend/test_everbridge.py pattern: mirror constants + pure-logic functions
locally rather than importing d4h.py directly. httpx is not installed in the
local pytest env; importing d4h would fail at collection time.

When updating helpers in d4h.py, ALSO update the mirror here. The mirror IS the
test contract — drift surfaces in production behavior.

Design doc: docs/plans/2026-05-13-d4h-phase2-backend-design.md
"""
import ast
import html
import inspect
import re
import textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pytest


# --------------------------------------------------------------------------
# Mirrored helpers — kept in sync with backend/d4h.py
# --------------------------------------------------------------------------

def _format_d4h_datetime(dt: datetime) -> str:
    """Format datetime as D4H-compatible ISO 8601 with milliseconds + Z suffix.

    D4H rejects Python's default isoformat output (`+00:00` offset, microseconds).
    Required format: `YYYY-MM-DDTHH:MM:SS.000Z` (millis zeroed; UTC only).
    Naive datetimes raise ValueError.
    """
    if dt.tzinfo is None:
        raise ValueError("D4H requires timezone-aware datetimes (use timezone.utc)")
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

class TestFormatD4HDatetime:
    def test_utc_datetime_produces_z_suffix(self):
        dt = datetime(2026, 1, 7, 22, 30, 0, tzinfo=timezone.utc)
        assert _format_d4h_datetime(dt) == "2026-01-07T22:30:00.000Z"

    def test_microseconds_are_dropped(self):
        dt = datetime(2026, 5, 13, 14, 30, 22, 123456, tzinfo=timezone.utc)
        assert _format_d4h_datetime(dt) == "2026-05-13T14:30:22.000Z"

    def test_non_utc_tz_is_normalized(self):
        pdt = timezone(timedelta(hours=-7))
        dt = datetime(2026, 5, 13, 7, 30, 0, tzinfo=pdt)  # 7am PDT == 14:30 UTC
        assert _format_d4h_datetime(dt) == "2026-05-13T14:30:00.000Z"

    def test_naive_datetime_raises(self):
        dt = datetime(2026, 5, 13, 14, 30, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            _format_d4h_datetime(dt)


# --- Mirror ---
# DOB normalization — converts MM/DD/YYYY (US intake form convention) to
# ISO 8601 (D4H spike 06:38 convention). Audit fix 2026-05-16.
_DOB_INPUT_FORMATS = (
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%Y-%m-%d",
    "%B %d, %Y",
    "%b %d, %Y",
)


def _normalize_dob_to_iso8601(dob_text: str) -> Optional[str]:
    """Mirror of d4h._normalize_dob_to_iso8601()."""
    if not dob_text:
        return None
    candidate = str(dob_text).split("(", 1)[0].strip()
    if not candidate:
        return None
    parsed = None
    used_2digit_year = False
    for fmt in _DOB_INPUT_FORMATS:
        try:
            parsed = datetime.strptime(candidate, fmt).date()
            used_2digit_year = fmt in ("%m/%d/%y", "%m-%d-%y")
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    today = datetime.now().date()
    if parsed > today:
        if not used_2digit_year:
            return None
        try:
            parsed = parsed.replace(year=parsed.year - 100)
        except ValueError:
            return None
    return parsed.isoformat()


# --- Mirror ---
def _normalize_sex_for_d4h(raw: str) -> str:
    """Mirror of d4h._normalize_sex_for_d4h()."""
    if not raw:
        return "OTHER"
    v = str(raw).strip().upper()
    if v in ("M", "MALE"):
        return "MALE"
    if v in ("F", "FEMALE"):
        return "FEMALE"
    return "OTHER"


class TestNormalizeSexForD4H:
    """Pin the D4H enum normalization. Empirically verified 2026-05-16 via
    a live HTTP 400 response body — D4H's actual enum is
    MALE / FEMALE / OTHER (NOT UNKNOWN). Spike 06:40's inline comment
    listed "UNKNOWN" as a guessed valid value, but the spike's actual
    POST sent "FEMALE" — comment was aspirational, never tested.
    """

    def test_empty_returns_other(self):
        # 2026-05-16: D4H 400 invalid_enum_value confirmed the valid
        # options are MALE / FEMALE / OTHER. The previous version of this
        # helper returned UNKNOWN, which D4H rejected.
        assert _normalize_sex_for_d4h("") == "OTHER"

    def test_none_returns_other(self):
        assert _normalize_sex_for_d4h(None) == "OTHER"  # type: ignore[arg-type]

    def test_whitespace_returns_other(self):
        assert _normalize_sex_for_d4h("   ") == "OTHER"

    def test_m_normalizes_to_male(self):
        assert _normalize_sex_for_d4h("M") == "MALE"
        assert _normalize_sex_for_d4h("m") == "MALE"

    def test_male_passes_through(self):
        assert _normalize_sex_for_d4h("MALE") == "MALE"
        assert _normalize_sex_for_d4h("male") == "MALE"

    def test_f_normalizes_to_female(self):
        assert _normalize_sex_for_d4h("F") == "FEMALE"
        assert _normalize_sex_for_d4h("f") == "FEMALE"

    def test_female_passes_through(self):
        assert _normalize_sex_for_d4h("FEMALE") == "FEMALE"
        assert _normalize_sex_for_d4h("female") == "FEMALE"

    def test_non_binary_falls_through_to_other(self):
        # Anything unrecognized maps to OTHER rather than passing through
        # to D4H as a malformed enum. UNKNOWN is explicitly tested too —
        # it would have been the pre-fix bug's output, must NOT slip back.
        for v in ("NB", "X", "U", "UNKNOWN", "PREFER_NOT_TO_SAY"):
            assert _normalize_sex_for_d4h(v) == "OTHER", f"failed for {v!r}"

    def test_forbids_unknown_regression(self):
        """Explicit guard: UNKNOWN is the value that caused the 2026-05-16
        production 400. The helper must NEVER return it again."""
        for empty in ("", None, "   ", "X", "NB", "U", "UNKNOWN"):
            assert _normalize_sex_for_d4h(empty) != "UNKNOWN", \
                f"returned forbidden UNKNOWN for {empty!r}"


# --- Mirror ---
def _split_address_for_d4h(lkp_address: str) -> tuple[str, str]:
    """Mirror of d4h._split_address_for_d4h()."""
    if not lkp_address:
        return "", ""
    parts = [p.strip() for p in lkp_address.split(",")]
    street = parts[0] if parts else ""
    town = parts[1] if len(parts) > 1 else ""
    return street, town


# --- Mirror ---
def _extract_records(body: dict) -> list:
    """Mirror of d4h._extract_records().

    Returns the records list from a D4H paginated response. Uses key-existence
    check (not truthiness) so an empty `results` list is treated as the
    legitimate "no results" answer, not as a falsy trigger to fall through
    to `data`.
    """
    return body.get("results", body.get("data", []))


class TestExtractRecords:
    """Cluster D — D4H-L8 pin. Pre-fix the 4 paginated endpoints used
    `body.get("results") or body.get("data") or []` — falsy on empty list,
    so a schema change with results=[] + data=[<unrelated records>] would
    silently iterate the wrong list."""

    def test_returns_results_when_present(self):
        assert _extract_records({"results": [{"id": 1}], "totalSize": 1}) == [{"id": 1}]

    def test_empty_results_is_legitimate_no_results(self):
        """The pre-fix bug. results=[] should NOT fall through to data."""
        body = {"results": [], "data": [{"id": 99, "stale": True}], "totalSize": 1}
        assert _extract_records(body) == []

    def test_falls_through_to_data_when_results_key_absent(self):
        """For endpoints that return `data` instead of `results` (D4H is
        inconsistent across endpoints), the helper still finds the list."""
        assert _extract_records({"data": [{"id": 2}], "totalSize": 1}) == [{"id": 2}]

    def test_returns_empty_list_when_both_keys_absent(self):
        """Defensive default — unlikely against D4H v3 in practice but the
        helper shields callers from a malformed response."""
        assert _extract_records({}) == []
        assert _extract_records({"totalSize": 0}) == []


class TestSplitAddressForD4H:
    """Pin the street/town split. Spike 03:122-127 paired non-empty values
    for both. Pre-fix d4h.py stuffed the whole string into street, left
    town empty. Audit fix 2026-05-16."""

    def test_full_us_address_with_state_and_zip(self):
        street, town = _split_address_for_d4h("1000 Tradan Dr, San Jose, CA 95110")
        assert street == "1000 Tradan Dr"
        assert town   == "San Jose"

    def test_address_with_state_no_zip(self):
        street, town = _split_address_for_d4h("1000 Tradan Dr, San Jose, CA")
        assert street == "1000 Tradan Dr"
        assert town   == "San Jose"

    def test_address_with_city_only(self):
        street, town = _split_address_for_d4h("1000 Tradan Dr, San Jose")
        assert street == "1000 Tradan Dr"
        assert town   == "San Jose"

    def test_park_name_with_city(self):
        # Officer-staging-style entries from CalTopo
        street, town = _split_address_for_d4h("Cardoza Park, Milpitas")
        assert street == "Cardoza Park"
        assert town   == "Milpitas"

    def test_street_only_no_comma(self):
        # Rare but possible — e.g., officer wrote only the street.
        street, town = _split_address_for_d4h("1000 Tradan Dr")
        assert street == "1000 Tradan Dr"
        assert town   == ""

    def test_empty_input(self):
        street, town = _split_address_for_d4h("")
        assert street == ""
        assert town   == ""

    def test_extra_whitespace_stripped(self):
        street, town = _split_address_for_d4h("  1000 Tradan Dr  ,  San Jose  , CA")
        assert street == "1000 Tradan Dr"
        assert town   == "San Jose"


class TestNormalizeDobToIso8601:
    """Pin the DOB MM/DD/YYYY → ISO 8601 normalization required by spike 06:38.

    Pre-fix, _build_involved_person_payload passed mp_dob through verbatim.
    The US-format intake form produces "06/15/1980" — D4H rejected as 400.
    """

    def test_us_format_normalizes(self):
        assert _normalize_dob_to_iso8601("06/15/1980") == "1980-06-15"

    def test_us_format_unpadded_normalizes(self):
        assert _normalize_dob_to_iso8601("6/15/1980") == "1980-06-15"

    def test_iso_passes_through_unchanged(self):
        assert _normalize_dob_to_iso8601("1955-03-15") == "1955-03-15"

    def test_hyphen_separator_normalizes(self):
        # 2026-05-10 (PR #402): handwritten forms commonly use hyphens.
        assert _normalize_dob_to_iso8601("6-26-2010") == "2010-06-26"

    def test_two_digit_year_disambiguates_to_past(self):
        # "10" → 2010 (within %y default range) — same as %y default for past dates
        assert _normalize_dob_to_iso8601("6-26-10") == "2010-06-26"

    def test_long_month_name_normalizes(self):
        assert _normalize_dob_to_iso8601("October 20, 2005") == "2005-10-20"

    def test_short_month_name_normalizes(self):
        assert _normalize_dob_to_iso8601("Oct 20, 2005") == "2005-10-20"

    def test_strips_trailing_age_hint(self):
        # Real OCR output: "DOB: 06/15/1980 (45 years old)" — the regex in
        # main.py captures everything before "(", but defense-in-depth lets
        # callers pass through the full form.
        assert _normalize_dob_to_iso8601("06/15/1980 (45 years old)") == "1980-06-15"

    def test_empty_returns_none(self):
        assert _normalize_dob_to_iso8601("") is None
        assert _normalize_dob_to_iso8601(None) is None  # type: ignore[arg-type]
        assert _normalize_dob_to_iso8601("   ") is None

    def test_unparseable_returns_none(self):
        # D4H accepts None for "no DOB on file" — better than passing garbage
        # that triggers a malformed-string 400.
        assert _normalize_dob_to_iso8601("not a date") is None
        assert _normalize_dob_to_iso8601("99/99/9999") is None

    def test_future_year_returns_none_not_fabricated(self):
        # 4-digit future year is a data-entry error, not a 19xx/20xx ambiguity.
        # Don't fabricate a sensible-looking date.
        assert _normalize_dob_to_iso8601("06/15/2099") is None

    def test_two_digit_year_same_year_future_month_century_flip(self):
        """Cluster D — D4H-M5 regression pin.

        Pre-fix: condition was `parsed.year > today.year`, which missed the
        same-year-future-month case. Input "12/15/26" parsed on 2026-05-25
        yields date(2026, 12, 15); `2026 > 2026` is False; no century flip;
        the future DOB silently passed through to D4H.

        Fix: compare full dates (`parsed > today`) so any future-relative-to-
        today parse for a 2-digit-year input triggers the -100 year flip.
        The result here is "1926-12-15" (a centenarian missing person).

        Note: pinning the exact output here would only work if "today" stays
        before December 15 of every year. Instead pin (a) result is not
        None (parse succeeded), (b) result is in the past (not a future
        date), (c) the year was flipped to the prior century.
        """
        today = datetime.now().date()
        # Pick a month + day strictly after today within the same year so
        # the bug-pre-fix path would yield a future date.
        # Construct "MM/DD/YY" using today.year mod 100 as the 2-digit year.
        future_in_year = today.replace(month=12, day=31) if today.month < 12 else today.replace(month=12, day=31)
        # If today IS 12-31, shift to 12-30 next year's 2-digit format; that case is rare enough.
        if future_in_year == today:
            pytest.skip("Test undefined on 12-31; future-month-in-current-year is impossible.")
        yy = today.year % 100
        candidate = f"12/31/{yy:02d}"
        result = _normalize_dob_to_iso8601(candidate)
        assert result is not None
        result_date = datetime.strptime(result, "%Y-%m-%d").date()
        assert result_date < today  # Flipped to the past
        assert result_date.year == today.year - 100  # Specifically -100 years


# --- Mirror ---
def _strip_date_for_d4h_title(event_name: str) -> str:
    """Strip a leading `YYYY-MM-DD ` from the event name.
    D4H stores its own startsAt timestamp; duplicating the date is redundant.
    """
    return re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", event_name)


class TestStripDateForD4HTitle:
    def test_leading_date_is_stripped(self):
        assert _strip_date_for_d4h_title("2026-05-13 SJPD Tradan") == "SJPD Tradan"

    def test_no_leading_date_preserved(self):
        assert _strip_date_for_d4h_title("SJPD Tradan") == "SJPD Tradan"

    def test_partial_date_not_stripped(self):
        assert _strip_date_for_d4h_title("2026 Something") == "2026 Something"

    def test_empty_string(self):
        assert _strip_date_for_d4h_title("") == ""

    def test_date_with_multiple_spaces(self):
        assert _strip_date_for_d4h_title("2026-05-13   SJPD Tradan") == "SJPD Tradan"


# --- Mirror ---
_SUFFIX_PROJECTS: frozenset[str] = frozenset({"sar-dispatch-dev"})

# Mirror of d4h.py::D4H_REFERENCE_DESCRIPTION_MAX (#672). D4H's Zod schema caps
# referenceDescription at 100; confirmed empirically 2026-08-01 by
# experiments/d4h/22_reference_description_cap.py against live team 1775.
D4H_REFERENCE_DESCRIPTION_MAX = 100

def _d4h_reference_description(event_name: str, project_id) -> str:
    """Build the D4H referenceDescription from an Event Name.

    1. Strip leading YYYY-MM-DD (delegates to _strip_date_for_d4h_title)
    2. Append 4-hex-char [abcd] suffix ONLY on personal-dev (mirrors caltopo.py)
    3. Cap at D4H's undocumented Zod max(100) (#672)
    """
    import secrets
    stripped = _strip_date_for_d4h_title(event_name)
    if project_id in _SUFFIX_PROJECTS:
        suffix = secrets.token_hex(2)
        room = D4H_REFERENCE_DESCRIPTION_MAX - len(f" [{suffix}]")
        return f"{stripped[:room].rstrip()} [{suffix}]"
    return stripped[:D4H_REFERENCE_DESCRIPTION_MAX].rstrip()


class TestD4HReferenceDescription:
    def test_sccssar_dev_no_suffix(self):
        assert _d4h_reference_description("2026-05-13 SJPD Tradan", "sar-dispatch-sccssar-dev") == "SJPD Tradan"

    def test_prod_no_suffix(self):
        assert _d4h_reference_description("2026-05-13 SJPD Tradan", "sar-dispatch-prod-20260218") == "SJPD Tradan"

    def test_unset_project_no_suffix(self):
        assert _d4h_reference_description("2026-05-13 SJPD Tradan", None) == "SJPD Tradan"

    def test_unknown_project_no_suffix(self):
        assert _d4h_reference_description("2026-05-13 SJPD Tradan", "sar-dispatch-future-env") == "SJPD Tradan"

    def test_personal_dev_appends_suffix(self):
        result = _d4h_reference_description("2026-05-13 SJPD Tradan", "sar-dispatch-dev")
        assert re.match(r"^SJPD Tradan \[[0-9a-f]{4}\]$", result), f"Got: {result!r}"

    def test_personal_dev_no_leading_date(self):
        result = _d4h_reference_description("SJPD Tradan", "sar-dispatch-dev")
        assert re.match(r"^SJPD Tradan \[[0-9a-f]{4}\]$", result)


# --- Mirror ---
TAG_ATV              = 33098
TAG_CANINE           = 33095
TAG_TECHNICAL_RESCUE = 33102
TAG_UAS              = 33103
TAG_SEARCH_MANAGEMENT = 34206
TAG_TRANSPORT        = 34207

# --- Mirror of d4h.STATUS_* — Selective mode (fullTeam: false) leaves
# REQUESTED unused at the application layer (no auto-staged records), but
# the constant stays mirrored because D4H may still return REQUESTED in
# GET /attendance responses if an admin manually creates a record in the UI.
# STATUS_ABSENT is unused at the application layer: the per-decline (NO-reply)
# sync that would have used it, GH issue #442, was DECLINED 2026-07-19
# (declines are Slack-tally-only, never sent to D4H). ---
STATUS_REQUESTED = "REQUESTED"
STATUS_ATTENDING = "ATTENDING"
STATUS_ABSENT    = "ABSENT"

# --- Mirror of d4h.DRONE_REF / DRONE_KIND_TITLE (used by add_drone_if_uas_dispatched) ---
# Confirmed in Phase 1 spike 09 (2026-05-11). Cross-file pinned in
# test_main_regression.py::TestD4HDroneRefIsStable.
DRONE_REF        = "Drone #6"
DRONE_KIND_TITLE = "UAS"

# --- Mirror of d4h.K9_HANDLER_ROLE_ID — PR-1 K9 Handler role tagging (2026-06-03) ---
# Discovered via experiments/d4h/16_handlers_endpoint.py Step 2b role-catalog
# enumeration. SCCSSAR-specific. Cross-file pinned in
# test_main_regression.py::TestD4HRoleIDsAreStable.
K9_HANDLER_ROLE_ID = 11487

_EB_TO_D4H_TAG: dict[str, int] = {
    "atv":               TAG_ATV,
    "canine":            TAG_CANINE,
    "search management": TAG_SEARCH_MANAGEMENT,  # also auto-added; idempotent
    "technical rescue":  TAG_TECHNICAL_RESCUE,
    "uas":               TAG_UAS,
}

_EB_NON_DISPATCH_GROUPS: frozenset[str] = frozenset({"admin", "all members", "automation test"})


def _strip_eb_prefix(eb_group_name: str) -> str:
    """Strip common SCCSSAR EB prefixes. Only ALERTSCC survives in live EB
    as of 2026-05-19 (Kris dropped SAR- and DOGS- prefixes in the rebuild)."""
    for prefix in ("ALERTSCC ",):
        if eb_group_name.startswith(prefix):
            return eb_group_name[len(prefix):]
    return eb_group_name


def _map_eb_groups_to_d4h_tags(eb_group_names: list[str]) -> tuple[list[int], list[str]]:
    """Map EB group names to D4H Specialty Team tag IDs.
    Returns (mapped_tag_ids, unmapped_group_names).
    Always includes TAG_SEARCH_MANAGEMENT. Adds TAG_TRANSPORT for canine.
    """
    mapped_ids: set[int] = set()
    unmapped: list[str] = []
    has_canine = False

    for eb_name in eb_group_names:
        stripped = _strip_eb_prefix(eb_name.strip()).strip().lower()
        if stripped in _EB_NON_DISPATCH_GROUPS:
            continue
        tag_id = _EB_TO_D4H_TAG.get(stripped)
        if tag_id is None:
            unmapped.append(eb_name)
            continue
        mapped_ids.add(tag_id)
        if tag_id == TAG_CANINE:
            has_canine = True

    mapped_ids.add(TAG_SEARCH_MANAGEMENT)
    if has_canine:
        mapped_ids.add(TAG_TRANSPORT)

    return sorted(mapped_ids), unmapped


class TestMapEBGroupsToD4HTags:
    def test_canine_maps_with_transport(self):
        """`Canine` dispatch triggers TAG_CANINE + TAG_TRANSPORT + always-on
        TAG_SEARCH_MANAGEMENT. DOGS-* sub-groups were collapsed into the
        single `Canine` group in Kris's 2026-05-19 EB rebuild."""
        mapped, unmapped = _map_eb_groups_to_d4h_tags(["Canine"])
        assert TAG_CANINE in mapped
        assert TAG_SEARCH_MANAGEMENT in mapped
        assert TAG_TRANSPORT in mapped
        assert unmapped == []

    def test_uas_no_transport(self):
        """UAS dispatch must NOT trigger TAG_TRANSPORT (Transport is canine-conditional)."""
        mapped, unmapped = _map_eb_groups_to_d4h_tags(["UAS"])
        assert TAG_UAS in mapped
        assert TAG_SEARCH_MANAGEMENT in mapped
        assert TAG_TRANSPORT not in mapped
        assert unmapped == []

    def test_non_dispatch_groups_silently_skipped(self):
        mapped, unmapped = _map_eb_groups_to_d4h_tags([
            "ALERTSCC ADMIN", "All Members", "Automation Test",
        ])
        assert mapped == [TAG_SEARCH_MANAGEMENT]
        assert unmapped == []

    def test_unknown_group_surfaces_in_unmapped(self):
        mapped, unmapped = _map_eb_groups_to_d4h_tags(["SAR - NewGroup"])
        assert mapped == [TAG_SEARCH_MANAGEMENT]
        assert unmapped == ["SAR - NewGroup"]

    def test_empty_input_still_has_search_management(self):
        mapped, unmapped = _map_eb_groups_to_d4h_tags([])
        assert mapped == [TAG_SEARCH_MANAGEMENT]
        assert unmapped == []

    def test_trailing_whitespace_still_maps(self):
        """Hardening: if EB ever emits a group name with trailing whitespace,
        still map correctly (not drop into unmapped)."""
        mapped, unmapped = _map_eb_groups_to_d4h_tags(["Canine "])
        assert TAG_CANINE in mapped
        assert TAG_SEARCH_MANAGEMENT in mapped
        assert TAG_TRANSPORT in mapped
        assert unmapped == []

    def test_leading_whitespace_still_maps(self):
        mapped, unmapped = _map_eb_groups_to_d4h_tags([" UAS"])
        assert TAG_UAS in mapped
        assert unmapped == []

    def test_technical_rescue(self):
        mapped, unmapped = _map_eb_groups_to_d4h_tags(["Technical Rescue"])
        assert TAG_TECHNICAL_RESCUE in mapped
        assert TAG_SEARCH_MANAGEMENT in mapped
        assert unmapped == []

    def test_search_management_dispatchable_is_idempotent(self):
        """`Search Management` is BOTH a dispatchable EB group AND the
        always-auto-added tag — the set in _map_eb_groups_to_d4h_tags
        dedupes so the tag appears exactly once even when explicitly selected."""
        mapped, unmapped = _map_eb_groups_to_d4h_tags(["Search Management"])
        assert mapped.count(TAG_SEARCH_MANAGEMENT) == 1
        assert unmapped == []

    def test_atv(self):
        mapped, unmapped = _map_eb_groups_to_d4h_tags(["ATV"])
        assert TAG_ATV in mapped
        assert TAG_SEARCH_MANAGEMENT in mapped
        assert TAG_TRANSPORT not in mapped
        assert unmapped == []

    def test_full_dispatch_combo(self):
        """End-to-end: dispatching all five EB groups maps cleanly with no
        unmapped warnings and the expected D4H tag set."""
        mapped, unmapped = _map_eb_groups_to_d4h_tags([
            "ATV", "Canine", "Search Management", "Technical Rescue", "UAS",
        ])
        expected = sorted({
            TAG_ATV, TAG_CANINE, TAG_TECHNICAL_RESCUE, TAG_UAS,
            TAG_SEARCH_MANAGEMENT, TAG_TRANSPORT,  # Transport from canine
        })
        assert mapped == expected
        assert unmapped == []


class TestStripEbPrefix:
    """Direct unit tests for _strip_eb_prefix.

    Only `ALERTSCC ` remains in the prefix tuple after Kris's 2026-05-19
    EB rebuild dropped the `SAR - ` and `DOGS - ` conventions. The only
    live group hitting this branch is `ALERTSCC ADMIN`.
    """

    def test_strips_alertscc_prefix(self):
        # Note: no dash after ALERTSCC — the prefix is "ALERTSCC " (single space).
        assert _strip_eb_prefix("ALERTSCC ADMIN") == "ADMIN"

    def test_no_prefix_passthrough(self):
        # Group names that don't start with `ALERTSCC ` are returned unchanged.
        assert _strip_eb_prefix("Canine") == "Canine"
        assert _strip_eb_prefix("Custom Group") == "Custom Group"

    def test_empty_string(self):
        assert _strip_eb_prefix("") == ""


# --------------------------------------------------------------------------
# Task 3.1 — _build_create_incident_payload
# --------------------------------------------------------------------------
# Fixture inputs reflect what main.py's OCR pipeline already produces:
#   - event_name:   canonical YYYY-MM-DD AGENCY STREETNAME (CLAUDE.md Locked Decision)
#   - lkp_lat/lng:  geocoded floats (from map_data["lkp"] in main.py)
#   - lkp_address:  free-form address string (from "Last Known Position:" line)
#
# dispatch_dt is passed separately (NOT in ocr_data) — it's a runtime stamp,
# not OCR output.

# Mirror of d4h.py::D4H_TRACKING_NUMBER_MAX + _tracking_number_for_d4h (#676).
# Kept in lockstep by TestTrackingNumberMirrorParity below.
D4H_TRACKING_NUMBER_MAX = 50


def _top_level_block(src: str, start_marker: str) -> str:
    """Slice `src` from `start_marker` to the next TOP-LEVEL statement.

    The boundary is a blank line followed by a column-0 character, which is
    what actually ends a top-level definition. Two weaker markers have been
    used here before and both have failed:

      - `start + N` characters: a later insertion pushes the target out of
        the window (false FAIL) and a deletion pulls junk in (false PASS).
      - the NEXT FUNCTION'S NAME: stable only while nothing is ever inserted
        between the two. #830 added a constant and a helper between
        _tracking_number_for_d4h and _strip_eb_prefix, and every slice bounded
        that way silently swallowed both — one turned red, the rest just
        started scanning code they do not name.

    A neighbour's identity is not a structural boundary. This is.
    """
    start = src.find(start_marker)
    assert start != -1, f"{start_marker!r} not found — renamed? moved?"
    m = re.search(r"\n\n\S", src[start:])
    assert m, f"could not bound {start_marker!r}"
    return src[start:start + m.start() + 1]


def _tracking_number_for_d4h(event_number: str) -> str:
    """Mirror of backend/d4h.py::_tracking_number_for_d4h."""
    value = (event_number or "").strip()
    return value[:D4H_TRACKING_NUMBER_MAX]


# Mirror of backend/d4h.py::D4H_AGE_MIN_EXCLUSIVE. D4H's Zod schema for
# POST /incident-involved-persons rejects age with `inclusive: false`, so the
# constraint is age > 0 and ZERO IS REJECTED. Pinned against production by
# TestAgeForD4HMirrorParity.
D4H_AGE_MIN_EXCLUSIVE = 0


def _age_for_d4h(age_val: object) -> Optional[int]:
    """Mirror of backend/d4h.py::_age_for_d4h."""
    try:
        age = int(age_val) if age_val not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if age is not None and age <= D4H_AGE_MIN_EXCLUSIVE:
        return None
    return age


SAMPLE_OCR = {
    "event_name":  "2026-05-13 SJPD Tradan",
    "event_number": "26-00193",
    "lkp_lat":     37.3382,
    "lkp_lng":    -121.8863,
    "lkp_address": "123 Main St, San Jose, CA 95110",
}

SAMPLE_DISP = {
    "dispatcher_name":  "Bill Burns",
    "dispatcher_email": "bill@sccssar.org",
}

SAMPLE_DT = datetime(2026, 5, 13, 14, 30, 0, tzinfo=timezone.utc)


# --- Mirror ---
def _build_create_incident_payload(
    ocr_data: dict,
    dispatch_dt: datetime,
    dispatcher_metadata: dict,
    project_id: Optional[str],
) -> dict:
    """Build the POST /team/{teamId}/incidents body for a new D4H incident.

    Pure-logic — no I/O. Composes two pure helpers:
      - _d4h_reference_description: date-strip + personal-dev suffix gating
      - _format_d4h_datetime: strict .000Z UTC

    Input contract:
      ocr_data:
        - event_name (str): canonical "YYYY-MM-DD AGENCY STREETNAME"
        - lkp_lat (float), lkp_lng (float): geocoded LKP coords
        - lkp_address (str): free-form address (Pass 1 OCR "Last Known Position:" line)
      dispatch_dt (datetime): timezone-aware dispatch timestamp (NOT in ocr_data)
      dispatcher_metadata:
        - dispatcher_name (str), dispatcher_email (str)
      project_id (str | None): GCP project ID for personal-dev suffix gating

    Weather is intentionally omitted — D4H auto-derives from location at create
    time; PATCHing the incident body afterward wipes auto-weather (Phase 1 finding).
    """
    event_name = ocr_data["event_name"]
    ref_desc = _d4h_reference_description(event_name, project_id)
    starts_at = _format_d4h_datetime(dispatch_dt)

    lkp_address = ocr_data.get("lkp_address", "") or ""

    # Address sub-dict — keys verified 2026-05-15 against
    # experiments/d4h/03_create_full.py:122-127 (live spike against D4H v3 SCCSSAR 1775).
    # 2026-05-16 audit: town now parsed from the comma-separated lkp_address.
    street, town = _split_address_for_d4h(lkp_address)
    address = {
        "street":  street,
        "town":    town,
        "region":  "California",
        "country": "United States",
    }

    # Location sub-dict — Cluster D defensive access mirror. Production raises
    # D4HClientError when LKP coords are missing/empty/non-numeric so a
    # geocoding failure surfaces as a typed exception rather than bare
    # KeyError/ValueError. D4HClientError is mirrored later in this file.
    lkp_lat_raw = ocr_data.get("lkp_lat")
    lkp_lng_raw = ocr_data.get("lkp_lng")
    if lkp_lat_raw in (None, "") or lkp_lng_raw in (None, ""):
        raise D4HClientError(
            "Cannot create D4H incident: LKP coordinates missing"
        )
    try:
        location = {
            "latitude":  float(lkp_lat_raw),
            "longitude": float(lkp_lng_raw),
        }
    except (TypeError, ValueError) as exc:
        raise D4HClientError(
            f"Cannot create D4H incident: LKP coordinates not numeric "
            f"(lkp_lat={lkp_lat_raw!r}, lkp_lng={lkp_lng_raw!r})"
        ) from exc

    # Description layout per dispatcher design 2026-05-20 — mirror of d4h.py.
    description_html = (
        "<p><strong>Dispatcher-TODO</strong> "
        "(review then delete this block before saving):</p>"
        "<ul>"
        "<li>Set attendee roles and start/stop period</li>"
        "<li>Assign K9s to handlers</li>"
        "<li>Assign any trucks and set mileage</li>"
        "<li>Review the involved persons tab</li>"
        "<li>Set the LPB tab</li>"
        "</ul>"
        "<p><em>↑ Delete the block above before saving — the description "
        "should begin with \"Event Name:\" on the next line.</em></p>"
    )

    full_summary = ocr_data.get("full_summary", "") or ""
    if full_summary:
        full_summary_h = html.escape(full_summary).replace("\n", "<br>")
        description_html += f'<p>&nbsp;</p><p>{full_summary_h}</p>'

    payload = {
        "referenceDescription": ref_desc,
        "startsAt":             starts_at,
        "address":              address,
        "location":             location,
        "description":          description_html,
        # Selective mode — keep in lockstep with d4h.py.
        "fullTeam":             False,
    }

    # #676 — agency reference number, omitted rather than sent empty.
    tracking_number = _tracking_number_for_d4h(ocr_data.get("event_number", ""))
    if tracking_number:
        payload["trackingNumber"] = tracking_number

    return payload


class TestBuildCreateIncidentPayload:
    def test_full_payload_shape(self):
        payload = _build_create_incident_payload(
            SAMPLE_OCR, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        for key in (
            "referenceDescription", "startsAt", "address",
            "location", "description", "trackingNumber",
        ):
            assert key in payload, f"missing key {key!r} in payload"
        # Weather must NEVER be present — D4H auto-derives from location.
        assert "weather" not in payload

    def test_tracking_number_is_the_agency_event_number_not_our_event_name(self):
        """#676 — trackingNumber is D4H's AGENCY-REFERENCE field. It carried
        our event name until #676, which is both the wrong data and (at 62
        chars on the 2026-07-31 mutual-aid callout) a hard 400 that destroyed
        the whole incident record after EB and Slack had fired."""
        payload = _build_create_incident_payload(
            SAMPLE_OCR, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        assert payload["trackingNumber"] == "26-00193"
        assert SAMPLE_OCR["event_name"] not in payload["trackingNumber"]
        # The event name is not lost — it lives where it belongs.
        assert "SJPD Tradan" in payload["referenceDescription"]

    def test_missing_event_number_omits_the_key_entirely(self):
        """Omit, do not send "". It is D4H's field; let D4H default it.
        Not every requesting agency writes an incident number on the form."""
        ocr = {k: v for k, v in SAMPLE_OCR.items() if k != "event_number"}
        payload = _build_create_incident_payload(
            ocr, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        assert "trackingNumber" not in payload
        # ...and the create still has everything D4H requires.
        for key in ("referenceDescription", "startsAt", "address", "location"):
            assert key in payload

    def test_blank_event_number_omits_the_key(self):
        for blank in ("", "   ", None):
            ocr = {**SAMPLE_OCR, "event_number": blank}
            payload = _build_create_incident_payload(
                ocr, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
            )
            assert "trackingNumber" not in payload, f"blank={blank!r}"

    def test_over_long_event_number_is_capped_not_rejected(self):
        """Defensive. An agency could in principle supply something long,
        and on 2026-07-31 exceeding this cap cost the entire D4H record —
        a truncated cross-reference beats no incident at all."""
        ocr = {**SAMPLE_OCR, "event_number": "X" * 80}
        payload = _build_create_incident_payload(
            ocr, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        assert len(payload["trackingNumber"]) == D4H_TRACKING_NUMBER_MAX == 50

    def test_the_2026_07_31_payload_would_now_be_accepted(self):
        """Regression against the real failure. The 62-character value D4H
        rejected was the EVENT NAME; it must not reach trackingNumber under
        any input, capped or otherwise."""
        humboldt = "2026-08-01 Humboldt County Sheriff's Office Treatment Facility"
        assert len(humboldt) > D4H_TRACKING_NUMBER_MAX  # the original 400
        ocr = {**SAMPLE_OCR, "event_name": humboldt, "event_number": "26-212-071"}
        payload = _build_create_incident_payload(
            ocr, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        assert payload["trackingNumber"] == "26-212-071"
        assert len(payload["trackingNumber"]) <= D4H_TRACKING_NUMBER_MAX

    def test_personal_dev_appends_suffix(self):
        payload = _build_create_incident_payload(
            SAMPLE_OCR, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-dev"
        )
        assert re.match(r"^SJPD Tradan \[[0-9a-f]{4}\]$", payload["referenceDescription"]), \
            f"Got: {payload['referenceDescription']!r}"

    def test_address_structured_fields_present(self):
        payload = _build_create_incident_payload(
            SAMPLE_OCR, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        addr = payload["address"]
        assert isinstance(addr, dict)
        for key in ("street", "town", "region", "country"):
            assert key in addr, f"missing key {key!r} in address dict"

    def test_address_sub_dict_uses_spike_keys(self):
        """Per experiments/d4h/03_create_full.py:122 (live spike against
        D4H v3 SCCSSAR 1775) the address schema is {street, town, region,
        country} — NOT {line1, locality, postalCode}.

        Pre-flight fix for PR 5 dispatch-time wiring. Without this fix,
        the first live D4H create POST would be rejected with HTTP 400
        on an unknown-field error.

        NOTE: address.town is asserted to be "" here as the baseline; the
        town-parsing audit fix (separate commit) populates it from the
        comma-separated lkp_address. test_address_town_parsed_from_lkp_*
        covers the parser behavior.
        """
        payload = _build_create_incident_payload(
            SAMPLE_OCR, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        addr = payload["address"]
        assert addr["region"]  == "California"
        assert addr["country"] == "United States"
        # Guard against regression to old (pre-spike-verification) keys.
        for forbidden in ("line1", "locality", "postalCode"):
            assert forbidden not in addr, f"old key {forbidden!r} must not be present"

    def test_address_town_parsed_from_lkp_address(self):
        """2026-05-16 audit fix — town is parsed from the comma-separated
        lkp_address ("1000 Tradan Dr, San Jose, CA 95110" → "San Jose")
        instead of left empty. Pre-fix the operationally-useful city was
        thrown away despite being present in the OCR-extracted data.
        Spike 03:124 used "San Jose" as a non-empty value."""
        payload = _build_create_incident_payload(
            SAMPLE_OCR, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        addr = payload["address"]
        # SAMPLE_OCR's lkp_address = "123 Main St, San Jose, CA 95110"
        assert addr["street"] == "123 Main St"
        assert addr["town"]   == "San Jose"

    def test_location_sub_dict_uses_spike_keys(self):
        """Per experiments/d4h/03_create_full.py:128-131 (live spike against
        D4H v3 SCCSSAR 1775) the location schema is {latitude, longitude} —
        NOT {lat, lng}.

        ROOT CAUSE of PR 5 smoke test 400 (2026-05-16): d4h.py used
        {lat, lng} despite an inline comment claiming spike-confirmed.
        The comment was aspirational — the actual spike file uses the
        long-form keys. Same root cause as the address-keys bug fixed
        in PR 5 Task 5.0.
        """
        payload = _build_create_incident_payload(
            SAMPLE_OCR, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        loc = payload["location"]
        assert loc["latitude"]  == pytest.approx(SAMPLE_OCR["lkp_lat"])
        assert loc["longitude"] == pytest.approx(SAMPLE_OCR["lkp_lng"])
        # Guard against regression to short-form keys
        for forbidden in ("lat", "lng"):
            assert forbidden not in loc, f"short-form key {forbidden!r} must not be present"

    def test_description_html_contains_dispatcher_todo_bullets(self):
        """Per Bill 2026-05-20 — description begins with a structured TODO
        block: a Dispatcher-TODO header, 5 review bullets, and a reminder
        line telling the dispatcher to delete the block before saving."""
        payload = _build_create_incident_payload(
            SAMPLE_OCR, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        desc = payload["description"]
        assert isinstance(desc, str)
        assert "Dispatcher-TODO" in desc
        # The five review bullets, verbatim
        assert "Set attendee roles and start/stop period" in desc
        assert "Assign K9s to handlers" in desc
        assert "Assign any trucks and set mileage" in desc
        assert "Review the involved persons tab" in desc
        assert "Set the LPB tab" in desc
        # Delete-this-block reminder
        assert "Delete the block above" in desc
        assert "Event Name:" in desc

    def test_starts_at_format_is_d4h_compatible(self):
        payload = _build_create_incident_payload(
            SAMPLE_OCR, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$", payload["startsAt"]), \
            f"Got: {payload['startsAt']!r}"

    # ---- Cluster D — D4H-H2 defensive coordinate access ------------------

    def test_missing_lkp_lat_raises_d4h_client_error(self):
        """Pre-fix: `float(ocr_data["lkp_lat"])` raised bare KeyError that
        bypassed create_incident_with_subject's typed-exception routing.
        Now: D4HClientError with a clear message — typed handler in
        main.py catches as a 4xx, not an untyped 500."""
        ocr = {k: v for k, v in SAMPLE_OCR.items() if k != "lkp_lat"}
        with pytest.raises(D4HClientError, match="LKP coordinates missing"):
            _build_create_incident_payload(ocr, SAMPLE_DT, SAMPLE_DISP, None)

    def test_missing_lkp_lng_raises_d4h_client_error(self):
        ocr = {k: v for k, v in SAMPLE_OCR.items() if k != "lkp_lng"}
        with pytest.raises(D4HClientError, match="LKP coordinates missing"):
            _build_create_incident_payload(ocr, SAMPLE_DT, SAMPLE_DISP, None)

    def test_empty_string_lkp_lat_raises_d4h_client_error(self):
        """Geocoding failure path: main.py sometimes stores lkp_lat=""
        rather than omitting the key. Pre-fix: float("") raised ValueError
        — also untyped. Now: empty string treated same as missing."""
        ocr = {**SAMPLE_OCR, "lkp_lat": ""}
        with pytest.raises(D4HClientError, match="LKP coordinates missing"):
            _build_create_incident_payload(ocr, SAMPLE_DT, SAMPLE_DISP, None)

    def test_non_numeric_lkp_coords_raise_d4h_client_error(self):
        """A bizarre OCR/geocode bug could leave a non-numeric string in
        the coord field. Surfacing as D4HClientError keeps the typed-
        exception contract; pre-fix this was a bare ValueError."""
        ocr = {**SAMPLE_OCR, "lkp_lat": "north of the trail"}
        with pytest.raises(D4HClientError, match="not numeric"):
            _build_create_incident_payload(ocr, SAMPLE_DT, SAMPLE_DISP, None)

    def test_description_drops_dispatcher_and_lkp_from_todo_block(self):
        """Per Bill 2026-05-20 — dispatcher name/email + standalone LKP line
        are no longer injected into the description. The TODO block is
        boilerplate-only (5 bullets); the IIS body is the only path for
        per-incident content. Dispatcher attribution lives in EB/Slack and
        the SAR dispatch flow's own event log — D4H doesn't need it."""
        ocr = {**SAMPLE_OCR, "lkp_address": "123 Main St, San Jose"}
        disp = {"dispatcher_name": "Bill Burns", "dispatcher_email": "bill@sccssar.org"}
        payload = _build_create_incident_payload(ocr, SAMPLE_DT, disp, "sar-dispatch-sccssar-dev")
        desc = payload["description"]
        # Dispatcher attribution NOT in description
        assert "Bill Burns" not in desc
        assert "bill@sccssar.org" not in desc
        # No standalone LKP line — LKP appears only inside the IIS body if
        # the dispatcher kept the "Last Known Position:" line there
        assert "<p>LKP:" not in desc
        # Standalone "LKP: 123 Main..." line NOT present
        assert "LKP: 123 Main St" not in desc

    def test_description_includes_full_summary_when_present(self):
        """Per Bill 2026-05-20 — D4H description carries the canonical IIS
        body (provided by main.py._extract_iis_body_for_d4h) after a blank
        line below the TODO block."""
        full_iis = (
            "Event Name: 2026-02-20 MILPITAS Calaveras\n"
            "Missing Person: Doe, Jane\n"
            "Q1 - Yes - Familiar with area\n"
            "\n"
            "LPB Range Ring Analysis: 50% containment at 1.2 mi"
        )
        ocr = {**SAMPLE_OCR, "full_summary": full_iis}
        payload = _build_create_incident_payload(
            ocr, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        desc = payload["description"]
        # TODO block still present (no regression)
        assert "Dispatcher-TODO" in desc
        # IIS body content visible
        assert "Event Name: 2026-02-20 MILPITAS Calaveras" in desc
        assert "Q1 - Yes - Familiar with area" in desc
        assert "LPB Range Ring Analysis: 50% containment at 1.2 mi" in desc
        # Newlines preserved via <br>
        assert "<br>" in desc
        # Blank-line spacer between TODO block and IIS body
        assert "<p>&nbsp;</p>" in desc

    def test_description_escapes_full_summary_html(self):
        """IIS body MUST be HTML-escaped — text might legitimately contain
        '<' or '&' (e.g. landmark names, age ranges like 'Age < 12').
        Verify escaping happens before <br> insertion."""
        full_iis = "Landmark: <Old Town> Cafe & Grill\nQ1 - Yes - Age < 13"
        ocr = {**SAMPLE_OCR, "full_summary": full_iis}
        payload = _build_create_incident_payload(
            ocr, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        desc = payload["description"]
        # Raw tags must NOT appear in description (HTML-injection protection)
        assert "<Old Town>" not in desc
        # Escaped form MUST appear
        assert "&lt;Old Town&gt;" in desc
        assert "&amp;" in desc
        # Newline still converted to <br>, not escaped as &#x0A; etc.
        assert "<br>" in desc

    def test_description_falls_back_when_full_summary_missing(self):
        """Backward-compat — callers that don't pass full_summary get the
        TODO block alone (no blank-line spacer + body)."""
        assert "full_summary" not in SAMPLE_OCR
        payload = _build_create_incident_payload(
            SAMPLE_OCR, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        desc = payload["description"]
        assert "Dispatcher-TODO" in desc
        # No blank-line spacer (would only appear before the IIS body)
        assert "<p>&nbsp;</p>" not in desc

    def test_description_falls_back_when_full_summary_empty(self):
        """Empty-string full_summary treated identically to missing — no
        empty body block in the description."""
        ocr = {**SAMPLE_OCR, "full_summary": ""}
        payload = _build_create_incident_payload(
            ocr, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        assert "<p>&nbsp;</p>" not in payload["description"]


class TestFullTeamFalseSentinel:
    """Sentinel — pin the `fullTeam: False` literal in the POST /incidents
    payload. Selective attendance mode (CLAUDE.md Locked Decision, 2026-05-19).

    Why the literal must be False, not omitted:
      - D4H defaults `fullTeam` to True server-side when omitted, which
        triggers async auto-staging of REQUESTED attendance records for
        all 50+ eligible team members.
      - Auto-staging causes: the async-init race (PATCH /attendance 500s
        for 3-10+ minutes), duplicate ATTENDING+REQUESTED rows, blank-name
        UI rendering, and 50+ manual "clear row" clicks at close-out.
      - Selective mode (`fullTeam: false`) yields a 0-record attendance
        baseline; per-YES POST-new records ATTENDING rows as responders
        accept. Empirically validated 2026-05-19 via spike 15 A/B test
        (D4H incidents 1618235 vs 1618236).

    Cross-file pin: also enforced in backend/test_main_regression.py
    (literal must match d4h.py + CLAUDE.md + this test).
    """
    def test_full_team_false_literal_in_payload(self):
        payload = _build_create_incident_payload(
            SAMPLE_OCR, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-sccssar-dev"
        )
        assert "fullTeam" in payload, \
            "POST /incidents payload MUST include 'fullTeam' — omitting it " \
            "defaults to True server-side and re-introduces the async-init race"
        assert payload["fullTeam"] is False, \
            f"fullTeam MUST be the bool False (Selective mode), got: " \
            f"{payload['fullTeam']!r}"

    def test_personal_dev_payload_still_selective(self):
        """Personal-dev suffix path must not accidentally drop fullTeam."""
        payload = _build_create_incident_payload(
            SAMPLE_OCR, SAMPLE_DT, SAMPLE_DISP, "sar-dispatch-dev"
        )
        assert payload["fullTeam"] is False


# --------------------------------------------------------------------------
# Task 3.2 — _build_involved_person_payload (LPB-shaped)
# --------------------------------------------------------------------------
# Builds the Subject involved-person record from OCR. Maps:
#   - Q1 (familiar with area)  → areaKnowledge enum (FAMILIAR / UNFAMILIAR / None)
#   - Q9 (intentional self-harm) → cause enum (INTENTIONAL_SELF / NO_DATA)
# Bundles Q2-Q12 (skipping Q1+Q9 already mapped) + Koester narrative + at-risk
# indicators into the involvementNotes catch-all (\n\n-separated paragraphs).
#
# OCR key contract: qN_question + qN_answer for N in 1..12 (mirrors CLAUDE.md
# "LPB format" Locked Decision: Q# - ANSWER - QUESTION em-dash separators).
#
# Subject enum constants — confirmed in Phase 1 spike 06 (involved-person metadata).

INVOLVEMENT_TYPE_SUBJECT  = 1
OUTCOME_PERSON_ASSISTED   = 1
AREA_KNOWLEDGE_FAMILIAR   = "FAMILIAR"
AREA_KNOWLEDGE_UNFAMILIAR = "UNFAMILIAR"
CAUSE_NO_DATA             = "NO_DATA"
CAUSE_INTENTIONAL_SELF    = "INTENTIONAL_SELF"


SAMPLE_OCR_INVOLVED = {
    # MP full name (from main.py OCR pipeline — "Missing Person:" line in textarea)
    # Spike 06:159 confirms this is the "name" field on the involved-person POST.
    "mp_full_name": "Doe, Jane",
    # Demographics (from main.py OCR pipeline)
    "mp_dob": "06/26/2010",
    "mp_age": 15,
    "mp_sex": "M",
    # Officer contact (Pass 1 OCR "Reporting Party / Officer" block)
    "officer_name":  "Sgt. Jane Doe",
    "officer_phone": "408-555-1212",
    # LPB Q1..Q12 — each as (question, answer) pair
    "q1_question": "Familiar with the area?",                       "q1_answer": "YES",
    "q2_question": "Mode of travel?",                               "q2_answer": "On foot",
    "q3_question": "Has been here before?",                         "q3_answer": "YES",
    "q4_question": "Currently medicated?",                          "q4_answer": "NO",
    "q5_question": "Carrying a phone?",                             "q5_answer": "YES",
    "q6_question": "Wearing reflective clothing?",                  "q6_answer": "NO",
    "q7_question": "Wearing weather-appropriate clothing?",         "q7_answer": "YES",
    "q8_question": "History of getting lost?",                      "q8_answer": "NO",
    "q9_question": "Has indicated intent to harm self?",            "q9_answer": "NO",
    "q10_question": "Last contact methods attempted?",              "q10_answer": "Cell phone, no answer",
    "q11_question": "Vehicle description?",                         "q11_answer": "N/A — no vehicle",
    "q12_question": "Any other notable details?",                   "q12_answer": "Last seen wearing red jacket",
    # Q1 mapping field (mirrors q1_answer; main.py emits both for clarity)
    "q1_familiar_with_area": "YES",
    "q9_intentional_self_harm": "NO",
    # Koester narrative (already-formatted paragraph from main.py PASS 2)
    "koester_narrative": "Hiker bracket (age 15). 50% ring 1.2 mi; 75% ring 2.0 mi.",
    # At-risk indicators (each one-liner from Pass 2)
    "at_risk_indicators": [
        "Diabetic - insulin-dependent",
        "Mild cognitive impairment",
    ],
}


# --- Mirror ---
def _build_involved_person_payload(ocr_data: dict) -> dict:
    """Build the POST /incident-involved-persons body for the Subject record.

    Pure-logic — no I/O. LPB-shaped: maps Q1 → areaKnowledge, Q9 → cause,
    bundles Q2-Q12 (skipping Q1+Q9) + Koester narrative + at-risk indicators
    into the involvementNotes catch-all (\\n\\n-separated paragraphs).

    HTML-escape policy: D4H stores involvementNotes + contact as plain text
    (NOT HTML — only the incident.description field is HTML-context). So no
    html.escape is applied here.

    Input contract (selected keys from ocr_data):
      - q1_familiar_with_area (str): "YES" / "NO" / "NOT ANSWERED" / missing
      - q9_intentional_self_harm (str): "YES" maps to CAUSE_INTENTIONAL_SELF; else NO_DATA
      - qN_question / qN_answer for N in 1..12: text pairs (skip-on-empty)
      - koester_narrative (str): already-formatted paragraph (skip if empty)
      - at_risk_indicators (list[str]): one-liners (skip whole block if empty)
      - officer_name (str), officer_phone (str): joined with "; " into contact
      - mp_dob (str), mp_age (int|str), mp_sex (str): demographic fields

    NO "activityId" key — the orchestrator sets it after _post_incident returns.
    """
    # Q1 → areaKnowledge enum
    q1 = (ocr_data.get("q1_familiar_with_area") or "").strip().casefold()
    if q1 == "yes":
        area_knowledge = AREA_KNOWLEDGE_FAMILIAR
    elif q1 == "no":
        area_knowledge = AREA_KNOWLEDGE_UNFAMILIAR
    else:
        area_knowledge = None  # "NOT ANSWERED" or missing → None

    # Q9 → cause enum
    q9 = (ocr_data.get("q9_intentional_self_harm") or "").strip().casefold()
    cause = CAUSE_INTENTIONAL_SELF if q9 == "yes" else CAUSE_NO_DATA

    # Officer contact — joined string ("name; phone")
    officer_name  = (ocr_data.get("officer_name") or "").strip()
    officer_phone = (ocr_data.get("officer_phone") or "").strip()
    contact_parts = [p for p in (officer_name, officer_phone) if p]
    contact = "; ".join(contact_parts)

    # involvementNotes catch-all — Q2..Q12 (skip Q1+Q9 already mapped) +
    # koester_narrative + at-risk indicators. \n\n between paragraphs.
    paragraphs: list[str] = []

    # #755 mirror — subject fact, so it leads the notes.
    last_seen = (ocr_data.get("last_seen_at") or "").strip()
    if last_seen:
        paragraphs.append(f"Last seen: {last_seen}")

    qn_lines: list[str] = []
    for n in range(2, 13):  # 2..12 inclusive
        if n == 9:
            continue  # mapped elsewhere
        question = (ocr_data.get(f"q{n}_question") or "").strip()
        answer   = (ocr_data.get(f"q{n}_answer") or "").strip()
        if not question or not answer:
            continue  # require BOTH halves — partial pairs produce malformed output
        # CLAUDE.md "LPB format" Locked Decision: Q# - ANSWER - QUESTION em-dash
        qn_lines.append(f"Q{n} - {answer} - {question}")
    if qn_lines:
        paragraphs.append("\n".join(qn_lines))

    koester = (ocr_data.get("koester_narrative") or "").strip()
    if koester:
        paragraphs.append(koester)

    at_risk_items = [ind for ind in (ocr_data.get("at_risk_indicators") or []) if ind]
    if at_risk_items:
        paragraphs.append("At-risk indicators:\n" + "\n".join(f"- {ind}" for ind in at_risk_items))

    involvement_notes = "\n\n".join(paragraphs)

    # Demographics — see backend/d4h.py::_age_for_d4h for the age > 0 constraint.
    age = _age_for_d4h(ocr_data.get("mp_age"))

    # Cluster D — D4H-M4 pre-validation mirror. Empty mp_full_name would
    # produce {"name": ""} which D4H 400s on; the _post_involved_person
    # None-strip filter does NOT strip empty strings. D4HClientError lets
    # the caller surface "Missing Person name required" instead of the
    # generic "request is malformed" D4H returns.
    mp_full_name = (ocr_data.get("mp_full_name") or "").strip()
    if not mp_full_name:
        raise D4HClientError(
            "Cannot attach Subject involved-person to D4H incident: "
            "mp_full_name missing or empty"
        )

    return {
        "involvementTypeId": INVOLVEMENT_TYPE_SUBJECT,
        "outcomeId":         OUTCOME_PERSON_ASSISTED,
        "name":              mp_full_name,
        "areaKnowledge":     area_knowledge,
        "cause":             cause,
        "contact":           contact,
        "involvementNotes":  involvement_notes,
        "dateOfBirth":       _normalize_dob_to_iso8601(ocr_data.get("mp_dob", "")),
        "age":               age,
        "sex":               _normalize_sex_for_d4h(ocr_data.get("mp_sex", "")),
    }


class TestBuildInvolvedPersonPayload:
    def test_name_field_present_from_mp_full_name(self):
        """Spike 06:159 — D4H REQUIRES the "name" field on
        /incident-involved-persons POST. Without it, D4H rejects with HTTP 400.

        Bug found in 2026-05-16 audit: pre-fix, _build_involved_person_payload
        had NO "name" key at all. Activates the moment the create-incident
        path succeeds (C1 location-keys fix) — first call to the involved-
        person POST would have surfaced this as the NEXT 400.
        """
        payload = _build_involved_person_payload(SAMPLE_OCR_INVOLVED)
        assert "name" in payload, "name field MUST be present per spike 06:159"
        assert payload["name"] == "Doe, Jane"

    def test_name_field_strips_whitespace(self):
        ocr = {**SAMPLE_OCR_INVOLVED, "mp_full_name": "  Smith, John  "}
        payload = _build_involved_person_payload(ocr)
        assert payload["name"] == "Smith, John"

    def test_missing_mp_full_name_raises_d4h_client_error(self):
        """Cluster D — D4H-M4. Pre-fix: returned {"name": ""} which D4H 400s
        on; _post_involved_person's None-strip filter does NOT strip empty
        strings, so the empty value reached D4H and produced a generic
        "request is malformed" with no field signal. Now: D4HClientError
        with a clear message — caller (create_incident_with_subject)
        captures specific "Missing Person name required" in
        post_create_failures instead of the generic one."""
        ocr = {k: v for k, v in SAMPLE_OCR_INVOLVED.items() if k != "mp_full_name"}
        with pytest.raises(D4HClientError, match="mp_full_name missing or empty"):
            _build_involved_person_payload(ocr)

    def test_empty_string_mp_full_name_raises_d4h_client_error(self):
        """Same as missing-key case — whitespace-only counts as empty."""
        ocr = {**SAMPLE_OCR_INVOLVED, "mp_full_name": "   "}
        with pytest.raises(D4HClientError, match="mp_full_name missing or empty"):
            _build_involved_person_payload(ocr)

    def test_subject_involvement_type(self):
        payload = _build_involved_person_payload(SAMPLE_OCR_INVOLVED)
        assert payload["involvementTypeId"] == 1
        assert payload["involvementTypeId"] == INVOLVEMENT_TYPE_SUBJECT

    def test_outcome_placeholder(self):
        payload = _build_involved_person_payload(SAMPLE_OCR_INVOLVED)
        assert payload["outcomeId"] == 1
        assert payload["outcomeId"] == OUTCOME_PERSON_ASSISTED

    def test_area_knowledge_familiar_from_q1_yes(self):
        ocr = {**SAMPLE_OCR_INVOLVED, "q1_familiar_with_area": "YES"}
        payload = _build_involved_person_payload(ocr)
        assert payload["areaKnowledge"] == "FAMILIAR"

    def test_area_knowledge_unfamiliar_from_q1_no(self):
        ocr = {**SAMPLE_OCR_INVOLVED, "q1_familiar_with_area": "NO"}
        payload = _build_involved_person_payload(ocr)
        assert payload["areaKnowledge"] == "UNFAMILIAR"

    def test_area_knowledge_none_when_not_answered(self):
        # "NOT ANSWERED" string → None
        ocr1 = {**SAMPLE_OCR_INVOLVED, "q1_familiar_with_area": "NOT ANSWERED"}
        assert _build_involved_person_payload(ocr1)["areaKnowledge"] is None
        # Missing key → None
        ocr2 = {k: v for k, v in SAMPLE_OCR_INVOLVED.items() if k != "q1_familiar_with_area"}
        assert _build_involved_person_payload(ocr2)["areaKnowledge"] is None

    def test_cause_intentional_self_from_q9_yes(self):
        ocr = {**SAMPLE_OCR_INVOLVED, "q9_intentional_self_harm": "YES"}
        payload = _build_involved_person_payload(ocr)
        assert payload["cause"] == "INTENTIONAL_SELF"

    def test_cause_no_data_default(self):
        # Q9 missing → NO_DATA
        ocr1 = {k: v for k, v in SAMPLE_OCR_INVOLVED.items() if k != "q9_intentional_self_harm"}
        assert _build_involved_person_payload(ocr1)["cause"] == "NO_DATA"
        # Q9 == "NO" → NO_DATA
        ocr2 = {**SAMPLE_OCR_INVOLVED, "q9_intentional_self_harm": "NO"}
        assert _build_involved_person_payload(ocr2)["cause"] == "NO_DATA"

    def test_contact_includes_officer_name_and_phone(self):
        payload = _build_involved_person_payload(SAMPLE_OCR_INVOLVED)
        assert "Sgt. Jane Doe" in payload["contact"]
        assert "408-555-1212" in payload["contact"]

    def test_involvement_notes_catchall_includes_q2_through_q12_skipping_q1_q9(self):
        payload = _build_involved_person_payload(SAMPLE_OCR_INVOLVED)
        notes = payload["involvementNotes"]
        # Q2 present (not mapped to dedicated field)
        assert "Q2 -" in notes
        # Q1 NOT in catch-all (mapped to areaKnowledge)
        assert "Q1 -" not in notes
        # Q9 NOT in catch-all (mapped to cause)
        assert "Q9 -" not in notes
        # Koester narrative present
        assert "Hiker bracket" in notes
        # At-risk indicators present
        assert "Diabetic - insulin-dependent" in notes
        assert "At-risk indicators:" in notes

    def test_dob_age_sex_present(self):
        """DOB normalized to ISO 8601 per spike 06:38; sex normalized to
        D4H enum (MALE/FEMALE/UNKNOWN) per spike 06:40. Audit fix 2026-05-16."""
        payload = _build_involved_person_payload(SAMPLE_OCR_INVOLVED)
        assert payload["dateOfBirth"] == "2010-06-26", \
            "DOB must be ISO 8601 — pre-fix bug passed MM/DD/YYYY verbatim"
        assert payload["age"] == 15
        # SAMPLE_OCR_INVOLVED has "mp_sex": "M" — normalizes to "MALE"
        assert payload["sex"] == "MALE", \
            "sex must be D4H enum (MALE/FEMALE/UNKNOWN) — pre-fix passed raw OCR value"

    def test_contact_uses_semicolon_separator(self):
        """Pin the `; ` separator — matches CLAUDE.md Slack-name-format Locked Decision
        rationale (comma-joined fields ambiguous in human reading).
        """
        payload = _build_involved_person_payload(SAMPLE_OCR_INVOLVED)
        contact = payload["contact"]
        assert "; " in contact, f"Expected '; ' separator in contact; got {contact!r}"

    def test_at_risk_all_blank_strings_skipped(self):
        """Pin I2 cleanup: a list like ["", "", ""] must NOT produce an empty
        'At-risk indicators:' header block.
        """
        ocr = {**SAMPLE_OCR_INVOLVED, "at_risk_indicators": ["", "", ""]}
        payload = _build_involved_person_payload(ocr)
        assert "At-risk indicators:" not in payload["involvementNotes"], (
            "Empty/blank at-risk list should suppress the entire header — got: "
            f"{payload['involvementNotes']!r}"
        )

    def test_qn_skipped_when_only_answer_present(self):
        """Pin I1 fix: a Q-pair with answer but no question is skipped (not rendered
        as a malformed `Q# - ANS - ` line)."""
        ocr = {**SAMPLE_OCR_INVOLVED, "q3_question": "", "q3_answer": "YES"}
        payload = _build_involved_person_payload(ocr)
        # Q3 line should be absent
        assert "Q3 -" not in payload["involvementNotes"], (
            "Q3 should be skipped when question is blank — got: "
            f"{payload['involvementNotes']!r}"
        )

    def test_qn_skipped_when_only_question_present(self):
        """Symmetric: question but no answer also skips."""
        ocr = {**SAMPLE_OCR_INVOLVED, "q3_question": "Familiar with area?", "q3_answer": ""}
        payload = _build_involved_person_payload(ocr)
        assert "Q3 -" not in payload["involvementNotes"]


# --------------------------------------------------------------------------
# Task 3.3 — FakeD4HClient + create_incident_with_subject
# --------------------------------------------------------------------------
# This task introduces the test foundation reused by Tasks 3.4, 3.5, 3.8, 3.9:
#   - FakeD4HClient: recording-fake double for the 9 HTTP wrappers in d4h.py
#   - Mirror D4HClientError/D4HServerError exception classes
#   - Mirror _create_incident_with_subject that takes a `client` kwarg
#     (production version omits client and calls module-level wrappers directly)
#
# The ONLY divergence between mirror and production orchestrator bodies is the
# `client.` prefix on the 3 wrapper calls — same orchestration sequence,
# payload-build calls, and return shapes. Drift in the orchestration body IS
# the test contract.


# --- Mirror ---
# Mirror D4H exception classes locally so failure-injection tests can construct them
# without importing d4h.
class D4HClientError(Exception):
    """Mirror of d4h.D4HClientError — 4xx, not retried."""


class D4HServerError(Exception):
    """Mirror of d4h.D4HServerError — 5xx + transport, Cloud Tasks retries."""


class D4HRateLimitError(D4HServerError):
    """Mirror of d4h.D4HRateLimitError — 429, Cloud Tasks retries.

    Inheritance from D4HServerError (NOT D4HClientError) is the pinned
    behavior: the handler routing in main.py uses
    ``isinstance(exc, d4h.D4HClientError)`` to decide between 200-terminal
    and 502-retry. With D4HRateLimitError inheriting from D4HServerError,
    the isinstance check fails and 429 naturally retries — without
    touching the handler code. Drift in this class hierarchy IS the
    test contract; see TestLogAndRaiseForStatus429.
    """


# --------------------------------------------------------------------------
# Mirror of _log_and_raise_for_status — diagnostic-logging upgrade 2026-05-16
# --------------------------------------------------------------------------
# The PR 5 smoke test failure surfaced D4H 4xx error="The request is malformed"
# without field-level detail — that was the top-level "detail" key only. The
# full body has an "errors": [...] array pinpointing which field D4H rejected.
# This mirror pins the upgraded behavior: BOTH "detail" AND the full body
# fragment land in the raised exception message + log line.

_LOG_MIRROR_SWALLOWED: frozenset[str] = frozenset()


def _log_and_raise_for_status_mirror(resp, op_label: str) -> None:
    """Mirror of backend/d4h.py::_log_and_raise_for_status.

    Duck-typing: `resp` must expose .is_success, .status_code, .text, .url, .json().
    Tests pass a FakeHttpxResponse below.
    """
    if resp.is_success:
        return

    body_fragment = (resp.text or "")[:800]
    error_code = ""
    try:
        body = resp.json()
        error_code = body.get("detail") or body.get("error_code") or ""
    except Exception:
        pass

    log_url = str(resp.url).split("?")[0]  # noqa: F841 — mirrored for shape

    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "") if hasattr(resp, "headers") else ""
        raise D4HRateLimitError(
            f"{op_label} → HTTP 429: rate limited "
            f"(retry_after={retry_after or 'none'})"
        )

    if 400 <= resp.status_code < 500:
        if error_code in _LOG_MIRROR_SWALLOWED:
            return
        summary = error_code or "(no top-level detail)"
        raise D4HClientError(
            f"{op_label} → HTTP {resp.status_code}: {summary} | body: {body_fragment}"
        )

    raise D4HServerError(f"{op_label} → HTTP {resp.status_code}: {body_fragment[:200]}")


class FakeHttpxResponse:
    """Minimal duck-typed httpx.Response for testing the log helper without
    importing httpx (not installed in local pytest env)."""

    def __init__(self, *, status_code: int, text: str = "",
                 url: str = "https://api.team-manager.us.d4h.com/v3/team/1775/incidents",
                 json_body=None, headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = headers or {}
        self._json_body = json_body

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._json_body is None:
            raise ValueError("Response body is not JSON")
        return self._json_body


class TestLogAndRaiseForStatusDiagnostic:
    """Pin the 2026-05-16 diagnostic upgrade. PR 5 smoke test surfaced
    HTTP 400 "The request is malformed" without field-level detail; the
    body had an "errors" array pinpointing the field. After this upgrade,
    BOTH "detail" AND the full body fragment land in the raised exception
    message + log line, so the next 4xx pinpoints the rejected field
    instead of hiding it.
    """

    def test_2xx_returns_normally(self):
        resp = FakeHttpxResponse(status_code=200, text='{"ok": true}', json_body={"ok": True})
        # Should NOT raise.
        _log_and_raise_for_status_mirror(resp, "d4h._post_incident")

    def test_4xx_with_top_level_detail_and_field_errors_surfaces_both(self):
        """The realistic D4H 400 case PR 5 hit: top-level "detail" plus
        deeper "errors" array. Diagnostic upgrade ensures BOTH surface."""
        body = {
            "detail": "The request is malformed",
            "errors": [
                {"field": "address.town", "code": "required"},
                {"field": "location.lat", "code": "unknown_field"},
            ],
        }
        resp = FakeHttpxResponse(
            status_code=400,
            text=str(body),
            json_body=body,
        )
        with pytest.raises(D4HClientError) as exc_info:
            _log_and_raise_for_status_mirror(resp, "d4h._post_incident")

        msg = str(exc_info.value)
        # Backward-compat prefix preserved
        assert "→ HTTP 400" in msg
        # Top-level detail still surfaced
        assert "The request is malformed" in msg
        # Field-level errors NOW surface (the upgrade)
        assert "address.town" in msg
        assert "location.lat" in msg

    def test_4xx_without_top_level_detail_falls_back_to_body(self):
        """Some D4H 400s have no top-level "detail" — body is the only signal.
        The exception message should still surface the body fragment."""
        body = {"errors": [{"field": "name", "code": "required"}]}
        resp = FakeHttpxResponse(status_code=400, text=str(body), json_body=body)
        with pytest.raises(D4HClientError) as exc_info:
            _log_and_raise_for_status_mirror(resp, "d4h._post_involved_person")
        msg = str(exc_info.value)
        assert "→ HTTP 400" in msg
        assert "name" in msg
        assert "required" in msg

    def test_4xx_with_non_json_body_still_surfaces_text(self):
        """If D4H returns HTML or plain text (rare but possible on gateway
        errors), the body fragment should still be in the exception message."""
        resp = FakeHttpxResponse(
            status_code=400,
            text="<html><body>Cloudflare proxy error</body></html>",
            json_body=None,
        )
        with pytest.raises(D4HClientError) as exc_info:
            _log_and_raise_for_status_mirror(resp, "d4h._post_incident")
        msg = str(exc_info.value)
        assert "Cloudflare proxy error" in msg

    def test_5xx_raises_d4hservererror_with_body_fragment(self):
        resp = FakeHttpxResponse(
            status_code=503,
            text="Service Unavailable",
            json_body=None,
        )
        with pytest.raises(D4HServerError) as exc_info:
            _log_and_raise_for_status_mirror(resp, "d4h._post_incident")
        msg = str(exc_info.value)
        assert "HTTP 503" in msg
        assert "Service Unavailable" in msg

    def test_body_fragment_capped_at_800_chars(self):
        """Defense — D4H could return a multi-KB error body (unlikely but
        possible). Cap at 800 chars to keep logs + exception messages readable."""
        long_body = "x" * 5000
        resp = FakeHttpxResponse(
            status_code=400,
            text=long_body,
            json_body=None,
        )
        with pytest.raises(D4HClientError) as exc_info:
            _log_and_raise_for_status_mirror(resp, "d4h._post_incident")
        msg = str(exc_info.value)
        # 800-char fragment + "| body: " prefix + "→ HTTP 400: ... | body: "
        # total well under 1000 chars
        assert len(msg) < 1100, f"exception message too long: {len(msg)} chars"


# --------------------------------------------------------------------------
# Mirror of _zod_issues_for_log + _ZOD_SAFE_ISSUE_KEYS — issue #671
# --------------------------------------------------------------------------
# Kept byte-equivalent to backend/d4h.py by TestZodIssuesMirrorParity below.
# Do NOT edit one without the other.

_ZOD_SAFE_ISSUE_KEYS: tuple[str, ...] = (
    "code", "type", "expected", "validation",
    "maximum", "minimum", "inclusive", "exact",
)

_ZOD_MAX_ISSUES_LOGGED = 10


def _zod_issues_for_log(body: object) -> list[dict]:
    """Mirror of backend/d4h.py::_zod_issues_for_log."""
    try:
        issues = body["detailObj"]["data"]["issues"]  # type: ignore[index]
    except Exception:
        return []
    if not isinstance(issues, list):
        return []

    out: list[dict] = []
    for issue in issues[:_ZOD_MAX_ISSUES_LOGGED]:
        if not isinstance(issue, dict):
            continue
        entry: dict = {}
        path = issue.get("path")
        if isinstance(path, list):
            entry["path"] = ".".join(str(p) for p in path)
        for key in _ZOD_SAFE_ISSUE_KEYS:
            if key in issue:
                entry[key] = issue[key]
        if issue.get("code") == "invalid_type" and "received" in issue:
            entry["received"] = issue["received"]
        if entry:
            out.append(entry)
    return out


# The verbatim 400 body D4H returned on 2026-07-31, captured by spike 21
# (experiments/d4h/21_incident_400_isolation.py). This is the payload the
# extractor exists to read; every assertion below reads it rather than a
# hand-built approximation.
_SPIKE21_400_BODY = {
    "statusCode": 400,
    "detail": "The request is malformed",
    "detailObj": {
        "name": "ZodError",
        "data": {
            "issues": [
                {
                    "code": "too_big",
                    "maximum": 50,
                    "type": "string",
                    "inclusive": True,
                    "exact": False,
                    "message": "String must contain at most 50 character(s)",
                    "path": ["body", "trackingNumber"],
                }
            ],
            "name": "ZodError",
        },
    },
}


class TestZodIssuesForLog:
    """Pin #671 — D4H 4xx bodies carry field-level Zod detail and we now
    extract the value-free subset.

    The 2026-07-31 dispatch lost its D4H incident to a trackingNumber
    max(50) violation. D4H said exactly that in the response; the AAR
    concluded we could not diagnose it from logs. We could not, because
    the detail was put on the exception (PII discipline) and then stripped
    off the exception (PII discipline) before any surface read it.
    """

    def test_spike21_body_names_the_failing_field_and_constraint(self):
        issues = _zod_issues_for_log(_SPIKE21_400_BODY)
        assert len(issues) == 1
        assert issues[0]["path"] == "body.trackingNumber"
        assert issues[0]["code"] == "too_big"
        assert issues[0]["maximum"] == 50

    def test_message_is_never_extracted(self):
        """Zod interpolates received values into some message templates
        (invalid_enum_value, invalid_literal). The key is excluded as a
        class, not case by case — a value-free example must not be read
        as a license to include it."""
        issues = _zod_issues_for_log(_SPIKE21_400_BODY)
        assert "message" not in issues[0]
        assert "at most 50 character" not in repr(issues)

    def test_received_kept_only_for_invalid_type(self):
        """For invalid_type Zod sets received to a TYPE NAME, which
        distinguishes "field missing" from "field wrong type" — the most
        useful thing a 400 can say. For every other code it can be the
        request value itself."""
        body = {"detailObj": {"data": {"issues": [
            {"code": "invalid_type", "expected": "string",
             "received": "undefined", "path": ["body", "referenceDescription"]},
            {"code": "invalid_enum_value", "received": "Jane Doe",
             "path": ["body", "status"]},
        ]}}}
        issues = _zod_issues_for_log(body)
        assert issues[0]["received"] == "undefined"
        assert "received" not in issues[1]
        assert "Jane Doe" not in repr(issues)

    def test_non_zod_body_yields_nothing(self):
        """Most D4H 4xx are not validation errors. Absence of issues must
        be silence, not an empty-shaped log line."""
        assert _zod_issues_for_log({"detail": "The request is malformed"}) == []
        assert _zod_issues_for_log({"detailObj": {"data": {}}}) == []

    def test_hostile_shapes_never_raise(self):
        """Total function — the caller is a best-effort log line sitting
        between a failed request and the typed exception it must still
        raise. Failure-mode Discipline Q2."""
        for body in (None, "", 42, [], {"detailObj": None},
                     {"detailObj": {"data": {"issues": "nope"}}},
                     {"detailObj": {"data": {"issues": [None, 7, "x"]}}}):
            assert _zod_issues_for_log(body) == []

    def test_issue_count_is_bounded(self):
        many = [{"code": "too_big", "path": ["body", f"f{i}"]} for i in range(40)]
        issues = _zod_issues_for_log({"detailObj": {"data": {"issues": many}}})
        assert len(issues) == _ZOD_MAX_ISSUES_LOGGED

    def test_non_list_path_is_skipped_not_crashed(self):
        issues = _zod_issues_for_log(
            {"detailObj": {"data": {"issues": [{"code": "custom", "path": "body.x"}]}}}
        )
        assert issues == [{"code": "custom"}]


class TestReferenceDescriptionCap:
    """Pin D4H's undocumented Zod cap on referenceDescription (issue #672).

    Measured 2026-08-01 by experiments/d4h/22_reference_description_cap.py
    against live team 1775, in ONE request that created NOTHING: the probe
    poisons trackingNumber past its known max(50) so the create can never
    succeed, and reads referenceDescription's `maximum` out of the same 400's
    detailObj.data.issues[] (the #671 finding).

        path=body.referenceDescription  code=too_big  maximum=100
        path=body.trackingNumber        code=too_big  maximum=50

    This cap is the BELT to #672's braces and it is load-bearing, not
    decorative. #672 caps the event name at RECONSTRUCTION time, but the Event
    Name textarea is dispatcher-editable and #87 wires that edited value
    straight to D4H — a pasted long name bypasses the reconstruction cap
    entirely. A 400 here rejects the WHOLE incident create with Everbridge and
    Slack already fired, which is precisely the 2026-07-31 failure.
    """

    def test_long_name_is_capped_without_suffix(self):
        name = "2026-07-31 " + "X" * 300
        out = _d4h_reference_description(name, "sar-dispatch-sccssar-dev")
        assert len(out) <= D4H_REFERENCE_DESCRIPTION_MAX

    def test_long_name_is_capped_including_the_personal_dev_suffix(self):
        """The suffix is appended AFTER truncation, so it must be budgeted.

        Truncating to 100 and then appending ' [abcd]' yields 107 and 400s —
        the bug this test exists to prevent.
        """
        name = "2026-07-31 " + "X" * 300
        out = _d4h_reference_description(name, "sar-dispatch-dev")
        assert len(out) <= D4H_REFERENCE_DESCRIPTION_MAX
        assert re.search(r" \[[0-9a-f]{4}\]$", out), "suffix lost to truncation"

    def test_normal_names_are_untouched(self):
        assert _d4h_reference_description(
            "2026-05-13 SJPD Tradan", "sar-dispatch-sccssar-dev") == "SJPD Tradan"

    def test_the_2026_07_31_name_now_fits(self):
        """62-char event name — the one that lost an entire incident record."""
        name = "2026-07-31 HUMBOLDT COUNTY SHERIFF'S OFFICE Redwood Valley Ranch"
        out = _d4h_reference_description(name, "sar-dispatch-dev")
        assert len(out) <= D4H_REFERENCE_DESCRIPTION_MAX

    def test_cap_literal_matches_production(self):
        """Cross-file pin. Source of truth is D4H's own Zod schema.

        The constraint is UNDOCUMENTED — if this drifts, re-run the spike
        rather than guessing a new number.
        """
        src = (Path(__file__).parent / "d4h.py").read_text(encoding="utf-8")
        assert "D4H_REFERENCE_DESCRIPTION_MAX = 100" in src, (
            "D4H_REFERENCE_DESCRIPTION_MAX drifted from 100 in backend/d4h.py — "
            "re-run experiments/d4h/22_reference_description_cap.py to confirm "
            "D4H's actual max() before updating this mirror"
        )
        assert D4H_REFERENCE_DESCRIPTION_MAX == 100

    def test_production_actually_applies_the_cap(self):
        """Assert the SLICE, inside the function — not the identifier anywhere.

        The constant is named by its own defining line and by the comment block
        above it; neither proves the truncation still runs. Comments stripped
        first for the same reason.
        """
        src = (Path(__file__).parent / "d4h.py").read_text(encoding="utf-8")
        m = re.search(
            r"^def _d4h_reference_description\(.*?(?=\n\n(?:def |# -{10,}|[A-Z_]+ = ))",
            src, re.DOTALL | re.MULTILINE,
        )
        assert m, "_d4h_reference_description not found in d4h.py"
        code = "\n".join(l.split("#")[0] for l in re.sub(
            r'""".*?"""', "", m.group(0), flags=re.DOTALL).splitlines())
        assert "[:D4H_REFERENCE_DESCRIPTION_MAX]" in code, (
            "The no-suffix branch no longer truncates — a dispatcher-edited "
            "long Event Name 400s the whole D4H incident create."
        )
        assert "D4H_REFERENCE_DESCRIPTION_MAX - len(" in code, (
            "The suffix branch no longer budgets for ' [abcd]', so a capped "
            "name plus the 7-char suffix exceeds 100 and 400s."
        )


class TestTrackingNumberMirrorParity:
    """#676 — pin the mirror above against production, and pin the two facts
    about production that the mirror cannot express: that the 50 is real, and
    that `trackingNumber` no longer receives the event name.

    The second is the one that matters. Every test above runs against the
    mirror, so reverting only d4h.py would leave them all green.

    Shares TestZodIssuesMirrorParity._shape rather than carrying its own copy:
    two near-identical AST helpers in one file is what git spliced together
    when #671 and #676 merged.
    """

    @staticmethod
    def _d4h_source() -> str:
        return (Path(__file__).parent / "d4h.py").read_text(encoding="utf-8")

    def test_cap_literal_matches_production(self):
        """Cross-file pin. Source of truth: D4H's own Zod schema, captured by
        experiments/d4h/21_incident_400_isolation.py against live team 1775.
        The constraint is undocumented — if this drifts, re-run the spike."""
        src = self._d4h_source()
        assert "D4H_TRACKING_NUMBER_MAX = 50" in src, (
            "D4H_TRACKING_NUMBER_MAX drifted from 50 in backend/d4h.py — "
            "re-run experiments/d4h/21_incident_400_isolation.py to confirm "
            "D4H's actual max() before updating this mirror"
        )
        assert D4H_TRACKING_NUMBER_MAX == 50

    def test_helper_matches_production(self):
        src = self._d4h_source()
        m = re.search(
            # Bound on the next TOP-LEVEL statement, not on a neighbour's
            # NAME: #830 inserted a constant and a helper between these two
            # functions and this slice silently swallowed both.
            r"^def _tracking_number_for_d4h\(.*?(?=\n\n\S)",
            src, re.DOTALL | re.MULTILINE,
        )
        assert m, "_tracking_number_for_d4h not found in d4h.py (renamed? moved?)"
        shape = TestZodIssuesMirrorParity._shape
        assert shape(m.group(0)) == shape(
            inspect.getsource(_tracking_number_for_d4h)
        ), (
            "_tracking_number_for_d4h in test_d4h.py has drifted from "
            "backend/d4h.py — the tests above exercise the stale copy"
        )

    def test_production_payload_no_longer_assigns_the_event_name(self):
        """The actual regression guard. `"trackingNumber": event_name` is the
        line that cost the 2026-07-31 dispatch its D4H record."""
        body = _top_level_block(
            self._d4h_source(), "def _build_create_incident_payload("
        )
        # Match EITHER assignment shape. The original literal pinned a
        # dict-entry form with exact whitespace alignment
        # (`"trackingNumber":       event_name`) that production stopped using
        # when the field became a conditional subscript assignment — so it
        # asserted the absence of a string that could no longer occur, and
        # reassigning the event name passed it. Caught by mutation testing
        # during the #830 pin sweep, not by review.
        assert not re.search(r'trackingNumber"?\]?\s*[:=]\s*event_name', body), (
            "trackingNumber is assigned the event name again — that is D4H's "
            "AGENCY-REFERENCE field, and the event name blows its max(50) on "
            "any long out-of-county agency (issue #676)"
        )
        assert '_tracking_number_for_d4h(ocr_data.get("event_number", ""))' in body, (
            "the payload builder no longer sources trackingNumber from the "
            "agency Event Number"
        )
        assert "if tracking_number:" in body, (
            "trackingNumber is no longer conditionally omitted — an empty "
            "string is not the same as letting D4H default the field"
        )



class TestAgeForD4H:
    """#830 — D4H rejects age 0, so a subject under one year old must be sent
    with the field OMITTED rather than with a fabricated value.

    D4H's Zod schema uses `inclusive: false`, i.e. age > 0. The 400 it returns
    rejects the WHOLE involved-person POST, so a zero costs every other subject
    field — name, DOB, sex, involvementNotes — on an incident whose Everbridge
    and Slack legs have already fired.
    """

    def test_zero_is_omitted(self):
        """The defect. Nothing upstream stopped it: index.html's guard is
        `age >= 0`, the old coercion accepted 0 because `0 not in (None, "")`,
        and the None-strip preserves falsy values by design."""
        assert _age_for_d4h(0) is None

    def test_one_is_still_sent(self):
        """The boundary the constraint actually names. `inclusive: false`
        means 1 is valid — a guard written as `< 0` or `<= 1` is wrong in
        opposite directions and this is what separates them."""
        assert _age_for_d4h(1) == 1

    def test_negative_is_omitted(self):
        """Belt to #765's braces. #765 removed the 2-digit-year pivot that
        produced negative ages, but mp_age is dispatcher-editable and the
        frontend is not the only producer."""
        assert _age_for_d4h(-29) is None

    def test_string_zero_is_omitted(self):
        """Intake values arrive as strings. A gate applied before coercion
        would pass "0" through."""
        assert _age_for_d4h("0") is None

    def test_ordinary_ages_are_unchanged(self):
        assert _age_for_d4h(15) == 15
        assert _age_for_d4h("81") == 81

    def test_missing_and_unparseable_are_omitted(self):
        """Unchanged from the pre-#830 behaviour — regression guard on the
        coercion that moved into the helper."""
        assert _age_for_d4h(None) is None
        assert _age_for_d4h("") is None
        assert _age_for_d4h("unknown") is None
        assert _age_for_d4h([]) is None

    def test_zero_age_omits_the_key_end_to_end(self):
        """The whole point: None here becomes an ABSENT key at the wire.

        Runs the payload builder and then the same null-strip
        _post_involved_person applies, because "age": None would be as fatal
        as "age": 0 if the strip ever stopped removing it.
        """
        ocr = dict(SAMPLE_OCR_INVOLVED, mp_age=0)
        payload = _build_involved_person_payload(ocr)
        assert payload["age"] is None
        assert "age" not in _strip_nulls_for_test(payload)

    def test_subject_survives_a_zero_age(self):
        """A zero must cost the age field and nothing else — the failure being
        prevented is losing the entire subject record."""
        ocr = dict(SAMPLE_OCR_INVOLVED, mp_age=0)
        cleaned = _strip_nulls_for_test(_build_involved_person_payload(ocr))
        assert cleaned["name"] == SAMPLE_OCR_INVOLVED["mp_full_name"]
        assert cleaned["dateOfBirth"]
        assert cleaned["involvementNotes"]


class TestAgeForD4HMirrorParity:
    """#830 — pin the mirror above against production, and pin the fact the
    mirror cannot express: that the builder actually routes through the helper.

    Every test in TestAgeForD4H runs against the mirror, so reverting d4h.py
    alone would leave them all green. d4h.py is not importable under local
    pytest, which is exactly why this class exists.
    """

    @staticmethod
    def _d4h_source() -> str:
        return (Path(__file__).parent / "d4h.py").read_text(encoding="utf-8")

    def test_constraint_literal_matches_production(self):
        """Source of truth: D4H's own Zod schema, captured 2026-09-06 against
        live team 1775. Undocumented — if this drifts, re-probe before
        changing it."""
        src = self._d4h_source()
        assert "D4H_AGE_MIN_EXCLUSIVE = 0" in src, (
            "D4H_AGE_MIN_EXCLUSIVE drifted from 0 in backend/d4h.py — D4H's "
            "schema rejects age with inclusive:false, so the exclusive "
            "minimum is 0. Re-probe before updating this mirror."
        )
        assert D4H_AGE_MIN_EXCLUSIVE == 0

    def test_helper_matches_production(self):
        src = self._d4h_source()
        m = re.search(
            r"^def _age_for_d4h\(.*?(?=\n\n\S)",
            src, re.DOTALL | re.MULTILINE,
        )
        assert m, "_age_for_d4h not found in d4h.py (renamed? moved?)"
        shape = TestZodIssuesMirrorParity._shape
        assert shape(m.group(0)) == shape(inspect.getsource(_age_for_d4h)), (
            "_age_for_d4h in test_d4h.py has drifted from backend/d4h.py — "
            "the tests above exercise the stale copy"
        )

    def test_production_builder_routes_age_through_the_helper(self):
        """The actual regression guard.

        A helper that exists but is not called is the same as no helper, and
        the inline `int(age_val)` coercion it replaced is what let 0 through.
        """
        body = _top_level_block(
            self._d4h_source(), "def _build_involved_person_payload("
        )
        assert '_age_for_d4h(ocr_data.get("mp_age"))' in body, (
            "the involved-person builder no longer routes age through "
            "_age_for_d4h — a subject under one year old will 400 the POST "
            "and lose the entire subject record (issue #830)"
        )
        assert "age = int(age_val)" not in body, (
            "the inline age coercion is back in the builder, bypassing the "
            "age > 0 gate"
        )

    def test_production_does_not_substitute_a_fabricated_age(self):
        """Omit, never substitute.

        Sending 1 would satisfy D4H's schema and silently record a year of age
        on a missing-infant report. The helper must return None.
        """
        src = self._d4h_source()
        m = re.search(
            r"^def _age_for_d4h\(.*?(?=\n\n\S)",
            src, re.DOTALL | re.MULTILINE,
        )
        assert m, "_age_for_d4h not found in d4h.py"
        code = "\n".join(
            l.split("#")[0] for l in re.sub(r'"""(?:.|\n)*?"""', "", m.group(0)).splitlines()
        )
        assert "return 1" not in code and "= 1" not in code, (
            "_age_for_d4h substitutes a fabricated age instead of omitting "
            "the field — D4H cannot represent 'less than one year old', and a "
            "wrong age on a missing-infant record is worse than none."
        )


class TestZodIssuesMirrorParity:
    """The mirror above must still match production, compared by AST.

    Without this the tests exercise a stale copy and report green while
    production drifts. Same contract as TestMirrorParity in
    test_log_redaction.py. httpx is unavailable locally so d4h.py cannot be
    imported; the production side is read as source text.
    """

    @staticmethod
    def _shape(fn_src: str) -> str:
        tree = ast.parse(textwrap.dedent(fn_src))
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                node.annotation = None
            if isinstance(node, ast.FunctionDef):
                node.returns = None
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body = node.body[1:]
        return ast.dump(tree)

    def test_extractor_matches_production(self):
        src = (Path(__file__).parent / "d4h.py").read_text(encoding="utf-8")
        m = re.search(
            r"^def _zod_issues_for_log\(.*?(?=\n\n\S)",
            src, re.DOTALL | re.MULTILINE,
        )
        assert m, "_zod_issues_for_log not found in d4h.py (renamed? moved?)"
        assert self._shape(m.group(0)) == self._shape(inspect.getsource(_zod_issues_for_log)), (
            "_zod_issues_for_log in test_d4h.py has drifted from backend/d4h.py "
            "(source of truth) — the tests above are exercising the stale copy"
        )

    def test_safe_key_whitelist_matches_production(self):
        src = (Path(__file__).parent / "d4h.py").read_text(encoding="utf-8")
        start = src.find("_ZOD_SAFE_ISSUE_KEYS: tuple[str, ...] = (")
        assert start != -1, "_ZOD_SAFE_ISSUE_KEYS not found in d4h.py"
        end = src.find("\n)\n", start)
        assert end != -1 and end > start, "could not bound the whitelist assignment"
        prod = src[start:end]
        for key in _ZOD_SAFE_ISSUE_KEYS:
            assert f'"{key}"' in prod, f"production whitelist lost {key!r}"
        # The exclusions are the whole point of the whitelist — a future
        # "why not just log the issue verbatim" PR must fail here.
        assert '"message"' not in prod, (
            "'message' added to _ZOD_SAFE_ISSUE_KEYS — Zod interpolates "
            "received values into some message templates, so the key is not "
            "value-free as a class. See the comment above it in d4h.py."
        )
        assert '"received"' not in prod, (
            "'received' added to the unconditional whitelist — it is the "
            "request value for every code except invalid_type, which is "
            "handled by its own guarded branch."
        )


class TestZodIssuesWiredIntoErrorPath:
    """The extractor is only worth anything if _log_and_raise_for_status
    calls it. The 2026-05-16 body-on-exception upgrade was defeated by a
    later PII fix in a different file and nobody noticed for 14 months —
    a cross-file failure a single-file test could not catch.

    Source-scan style: httpx is unavailable locally, so the wiring is
    pinned by reading production text rather than executing it.
    """

    @staticmethod
    def _handler_source() -> str:
        src = (Path(__file__).parent / "d4h.py").read_text(encoding="utf-8")
        return _top_level_block(src, "def _log_and_raise_for_status(")

    def test_extractor_is_called_on_the_4xx_path(self):
        body = self._handler_source()
        assert "_zod_issues_for_log(parsed_body)" in body, (
            "_log_and_raise_for_status no longer extracts Zod issues — D4H "
            "4xx responses go back to being undiagnosable from logs (#671)"
        )
        # It must sit in the 4xx branch: after the swallowed-error early
        # return, before the raise.
        four_xx = body.index("if 400 <= resp.status_code < 500:")
        # Match the CALL, not the name — the docstring names the helper too,
        # and it sits above the branch.
        assert body.index("_zod_issues_for_log(parsed_body)") > four_xx, (
            "Zod extraction moved above the 4xx branch — it would run on "
            "swallowed idempotent failures and on the 429 fast path"
        )

    def test_extraction_is_logged_not_only_computed(self):
        body = self._handler_source()
        assert "D4H 4xx validation issues" in body, (
            "the Zod issues are extracted but no longer logged — this is the "
            "exact shape of the bug #671 fixes, where the detail was "
            "constructed, carried, and destroyed without being read"
        )

    def test_raw_body_is_still_not_logged(self):
        """The fix must not become a reason to relax the PII guard it works
        around. body_fragment stays off every log line."""
        body = self._handler_source()
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("logger."):
                assert "body_fragment" not in stripped, (
                    "body_fragment reached a log line — D4H 4xx bodies echo "
                    f"request fields (member emails, names): {stripped!r}"
                )


class TestLogAndRaiseForStatus429:
    """Pin the batch-3 PR-G.1 carve-out for HTTP 429 (rate-limited).

    Pre-fix: every 4xx — including 429 — raised D4HClientError, which the
    handler routing in main.py:handle_per_yes_sync_task mapped to 200
    (terminal-failed). A 429 burst during a high-YES-volume call-out
    would silently lose per-YES attendance records, since Cloud Tasks
    would see "task done" and never retry.

    Post-fix: 429 raises D4HRateLimitError (subclass of D4HServerError);
    the existing ``isinstance(exc, D4HClientError)`` check fails, so the
    handler falls through to ``raise HTTPException(status_code=502)`` and
    Cloud Tasks retries per max_attempts=5.

    Drift in any of these invariants IS the test contract:
      - 429 must raise D4HRateLimitError (NOT D4HClientError, NOT bare D4HServerError)
      - D4HRateLimitError must subclass D4HServerError (drives handler routing)
      - D4HRateLimitError must NOT subclass D4HClientError (would re-introduce silent drop)
    """

    def test_429_raises_rate_limit_error(self):
        resp = FakeHttpxResponse(status_code=429, text="rate limited",
                                  headers={"Retry-After": "5"})
        with pytest.raises(D4HRateLimitError) as exc_info:
            _log_and_raise_for_status_mirror(resp, "d4h._post_attendance")
        msg = str(exc_info.value)
        assert "HTTP 429" in msg
        assert "retry_after=5" in msg

    def test_429_without_retry_after_header_still_raises(self):
        """Some 429s omit Retry-After; the typed exception must still fire."""
        resp = FakeHttpxResponse(status_code=429, text="rate limited",
                                  headers={})
        with pytest.raises(D4HRateLimitError) as exc_info:
            _log_and_raise_for_status_mirror(resp, "d4h._post_attendance")
        msg = str(exc_info.value)
        assert "HTTP 429" in msg
        assert "retry_after=none" in msg

    def test_429_does_not_raise_client_error(self):
        """Routing invariant: a 429 must NOT raise D4HClientError. If it
        did, the handler's isinstance check would map it to 200-terminal
        and silently drop the task — exactly the failure mode this PR
        closes. Belt-and-braces test against accidental refactoring."""
        resp = FakeHttpxResponse(status_code=429, text="rate limited",
                                  headers={"Retry-After": "5"})
        with pytest.raises(D4HRateLimitError):
            _log_and_raise_for_status_mirror(resp, "d4h._post_attendance")
        # And inversely: a 429 result is NOT a D4HClientError instance
        try:
            _log_and_raise_for_status_mirror(resp, "d4h._post_attendance")
        except Exception as exc:
            assert not isinstance(exc, D4HClientError), (
                "D4HRateLimitError MUST NOT inherit from D4HClientError — "
                "doing so would re-introduce the 200-terminal silent-drop "
                "bug for 429 in main.py:handle_per_yes_sync_task"
            )

    def test_429_is_server_error_subclass(self):
        """Routing invariant: D4HRateLimitError must inherit from
        D4HServerError so the handler's isinstance(D4HClientError) check
        fails and 429 falls through to the 502-retry branch."""
        assert issubclass(D4HRateLimitError, D4HServerError), (
            "D4HRateLimitError MUST subclass D4HServerError so the handler "
            "routing in main.py:handle_per_yes_sync_task lets 429 fall "
            "through to 502 (Cloud Tasks retry) instead of 200 (terminal)"
        )

    def test_4xx_other_than_429_still_raises_client_error(self):
        """The 429 carve-out must NOT regress the 400/404/etc. paths —
        those remain D4HClientError → 200-terminal as the Cluster-D
        routing requires (malformed request payloads should not retry)."""
        for status in [400, 401, 403, 404, 422]:
            resp = FakeHttpxResponse(status_code=status, text="error",
                                      json_body={"detail": "bad request"})
            with pytest.raises(D4HClientError):
                _log_and_raise_for_status_mirror(resp, "d4h._post_incident")


# --------------------------------------------------------------------------
# FakeD4HClient — recording-fake test double for the 9 HTTP wrappers.
#
# Each method records (name, args, kwargs) in self.calls and returns a
# canned response. Per-method failure injection via *_raises kwargs lets
# the orchestrator tests exercise error paths.
#
# Used by Tasks 3.3, 3.4, 3.5, 3.8, 3.9. Not used by 3.1, 3.2 (pure-logic).
# --------------------------------------------------------------------------

class FakeD4HClient:
    def __init__(
        self,
        *,
        incident_id: int = 12345,
        attendance: list[dict] | None = None,
        member: dict | None = None,
        equipment: dict | None = None,
        equipment_usages: list[dict] | None = None,
        handlers: list[dict] | None = None,
        post_incident_raises: Exception | None = None,
        post_tags_raises: Exception | None = None,
        post_involved_person_raises: Exception | None = None,
        post_attendance_raises: Exception | None = None,
        patch_attendance_raises: Exception | None = None,
        patch_attendance_raises_for_ids: set[int] | None = None,
        get_handlers_raises: Exception | None = None,
        animal_attendance: list[dict] | None = None,
        post_animal_attendance_raises: Exception | None = None,
    ):
        self.calls: list[tuple] = []
        self._incident_id = incident_id
        self._attendance = attendance or []
        self._member = member
        self._equipment = equipment
        self._equipment_usages = equipment_usages or []
        self._handlers = handlers or []
        self._post_incident_raises = post_incident_raises
        self._post_tags_raises = post_tags_raises
        self._post_involved_person_raises = post_involved_person_raises
        self._post_attendance_raises = post_attendance_raises
        self._patch_attendance_raises = patch_attendance_raises
        self._patch_attendance_raises_for_ids = patch_attendance_raises_for_ids or set()
        self._get_handlers_raises = get_handlers_raises
        self._animal_attendance = animal_attendance or []
        self._post_animal_attendance_raises = post_animal_attendance_raises

    def _post_incident(self, payload):
        self.calls.append(("_post_incident", payload))
        if self._post_incident_raises:
            raise self._post_incident_raises
        return {"id": self._incident_id, "ref": f"#0{self._incident_id}"}

    def _post_tags(self, activity_id, tag_ids):
        self.calls.append(("_post_tags", activity_id, tag_ids))
        if self._post_tags_raises:
            raise self._post_tags_raises
        return {}

    def _post_involved_person(self, payload):
        self.calls.append(("_post_involved_person", payload))
        if self._post_involved_person_raises:
            raise self._post_involved_person_raises
        return {}

    def _get_attendance(self, activity_id):
        self.calls.append(("_get_attendance", activity_id))
        return self._attendance

    def _post_attendance(self, payload):
        self.calls.append(("_post_attendance", payload))
        if getattr(self, "_post_attendance_raises", None):
            raise self._post_attendance_raises
        # Return shape mirrors live D4H — a created record with a new id.
        return {"id": 999999, **payload}

    def _patch_attendance(self, attendance_id, status):
        self.calls.append(("_patch_attendance", attendance_id, status))
        # Per-ID set wins over global. When _patch_attendance_raises_for_ids is
        # non-empty, ONLY ids in that set raise — the global _patch_attendance_raises
        # supplies the exception instance (or fabricates D4HServerError as fallback).
        # When _patch_attendance_raises_for_ids is empty AND _patch_attendance_raises
        # is set, every id raises (all-fail injection).
        if self._patch_attendance_raises_for_ids:
            if attendance_id in self._patch_attendance_raises_for_ids:
                raise self._patch_attendance_raises or D4HServerError("simulated")
            return {}
        if self._patch_attendance_raises:
            raise self._patch_attendance_raises
        return {}

    def _get_member_by_email(self, email):
        self.calls.append(("_get_member_by_email", email))
        return self._member

    def _get_handlers_for_member(self, member_id):
        """Mirror of d4h._get_handlers_for_member. Returns the handlers list
        unless get_handlers_raises is set (then raises that exception).
        Used by _resolve_k9_handler_role_id mirror — PR-1 K9 sync (2026-06-03).
        """
        self.calls.append(("_get_handlers_for_member", member_id))
        if self._get_handlers_raises:
            raise self._get_handlers_raises
        return self._handlers

    def _get_animal_attendance(self, activity_id):
        self.calls.append(("_get_animal_attendance", activity_id))
        return self._animal_attendance

    def _post_animal_attendance(self, payload):
        self.calls.append(("_post_animal_attendance", payload))
        if self._post_animal_attendance_raises:
            raise self._post_animal_attendance_raises
        return {"id": 888888, **payload}

    def _get_equipment_by_ref(self, ref, kind_title):
        self.calls.append(("_get_equipment_by_ref", ref, kind_title))
        return self._equipment

    def _post_equipment_usage(self, activity_id, equipment_id, duration=0):
        self.calls.append(("_post_equipment_usage", activity_id, equipment_id, duration))
        return {}

    def _get_equipment_usages_for_activity(self, activity_id):
        self.calls.append(("_get_equipment_usages_for_activity", activity_id))
        return self._equipment_usages


# --- Mirror ---
def _create_incident_with_subject(ocr_data, dispatch_dt, eb_groups, dispatcher_metadata, project_id, *, client):
    """Mirror of d4h.create_incident_with_subject — takes `client` kwarg.

    Production version omits client kwarg and calls module-level wrappers.
    The ONLY divergence between mirror and production orchestrator bodies is
    the `client.` prefix on the 3 wrapper calls (_post_incident, _post_tags,
    _post_involved_person) — same orchestration sequence, payload-build calls,
    and return shapes. Drift in the orchestration body IS the test contract.

    Returns (activity_id, unmapped_eb_groups, post_create_failures).
    """
    payload = _build_create_incident_payload(ocr_data, dispatch_dt, dispatcher_metadata, project_id)
    incident = client._post_incident(payload)  # raises if this fails
    activity_id = incident["id"]

    mapped_tags, unmapped = _map_eb_groups_to_d4h_tags(eb_groups)
    post_create_failures: list[str] = []

    try:
        client._post_tags(activity_id, mapped_tags)
    except (D4HClientError, D4HServerError) as exc:
        post_create_failures.append(f"D4H tag-POST failed: {exc}")

    try:
        person_payload = _build_involved_person_payload(ocr_data)
        person_payload["incidentId"] = activity_id
        client._post_involved_person(person_payload)
    except (D4HClientError, D4HServerError) as exc:
        post_create_failures.append(f"D4H involved-person POST failed: {exc}")

    # 4th post-create step: drone resource for UAS group dispatches.
    try:
        _add_drone_if_uas_dispatched(activity_id, eb_groups, client=client)
    except (D4HClientError, D4HServerError) as exc:
        post_create_failures.append(f"D4H drone-attach failed: {exc}")

    return activity_id, unmapped, post_create_failures


class TestCreateIncidentWithSubject:
    def test_calls_post_incident_with_built_payload(self):
        client = FakeD4HClient(incident_id=99)
        _create_incident_with_subject(
            SAMPLE_OCR, SAMPLE_DT, ["UAS"], SAMPLE_DISP,
            "sar-dispatch-sccssar-dev", client=client,
        )
        assert client.calls[0][0] == "_post_incident"
        assert client.calls[0][1]["referenceDescription"] == "SJPD Tradan"

    def test_post_tags_receives_returned_activity_id_and_mapped_tags(self):
        client = FakeD4HClient(incident_id=99)
        _create_incident_with_subject(
            SAMPLE_OCR, SAMPLE_DT, ["UAS"], SAMPLE_DISP,
            "sar-dispatch-sccssar-dev", client=client,
        )
        post_tags_call = [c for c in client.calls if c[0] == "_post_tags"][0]
        assert post_tags_call[1] == 99  # activity_id
        assert post_tags_call[2] == sorted([TAG_UAS, TAG_SEARCH_MANAGEMENT])

    def test_post_involved_person_receives_incident_id_in_payload(self):
        """Spike 06:156 uses "incidentId" as the activity-linkage field on
        /incident-involved-persons POST. Pre-fix used "activityId" which
        D4H silently ignored, orphaning the record. Audit fix 2026-05-16."""
        ocr = {**SAMPLE_OCR, **SAMPLE_OCR_INVOLVED}
        client = FakeD4HClient(incident_id=99)
        _create_incident_with_subject(
            ocr, SAMPLE_DT, [], SAMPLE_DISP, "sar-dispatch-sccssar-dev",
            client=client,
        )
        ip_call = [c for c in client.calls if c[0] == "_post_involved_person"][0]
        assert ip_call[1]["incidentId"] == 99
        assert "activityId" not in ip_call[1], \
            "linkage field MUST be 'incidentId' per spike 06:156, NOT 'activityId'"

    def test_happy_path_returns_3tuple_with_empty_failures(self):
        client = FakeD4HClient(incident_id=99)
        ocr = {**SAMPLE_OCR, **SAMPLE_OCR_INVOLVED}
        activity_id, unmapped, post_failures = _create_incident_with_subject(
            ocr, SAMPLE_DT, ["SAR - NewGroup"], SAMPLE_DISP,
            "sar-dispatch-sccssar-dev", client=client,
        )
        assert activity_id == 99
        assert unmapped == ["SAR - NewGroup"]
        assert post_failures == []

    def test_propagates_post_incident_failure(self):
        """_post_incident failure is fatal — no activity_id to recover with."""
        client = FakeD4HClient(post_incident_raises=D4HServerError("boom"))
        ocr = {**SAMPLE_OCR, **SAMPLE_OCR_INVOLVED}
        with pytest.raises(D4HServerError, match="boom"):
            _create_incident_with_subject(
                ocr, SAMPLE_DT, [], SAMPLE_DISP, "sar-dispatch-sccssar-dev",
                client=client,
            )

    def test_post_tags_failure_captured_not_raised(self):
        """Pre-fix the orchestrator raised on tag-POST failure → caller lost
        activity_id → bulk-ABSENT skipped → 50+ attendance records orphaned
        in REQUESTED state. Post-fix the failure is captured in
        post_create_failures and the caller can still run bulk-ABSENT."""
        client = FakeD4HClient(
            incident_id=99,
            post_tags_raises=D4HClientError("tag failure"),
        )
        ocr = {**SAMPLE_OCR, **SAMPLE_OCR_INVOLVED}
        activity_id, unmapped, post_failures = _create_incident_with_subject(
            ocr, SAMPLE_DT, ["UAS"], SAMPLE_DISP,
            "sar-dispatch-sccssar-dev", client=client,
        )
        assert activity_id == 99
        assert any("tag-POST failed" in f for f in post_failures)
        # Crucially: orchestrator MUST still attempt involved-person POST
        # after tag failure — independent step.
        ip_calls = [c for c in client.calls if c[0] == "_post_involved_person"]
        assert len(ip_calls) == 1

    def test_post_involved_person_failure_captured_not_raised(self):
        """The 2026-05-16 smoke-test failure: involved-person 400 on sex
        enum. Pre-fix orchestrator raised → caller lost activity_id →
        bulk-ABSENT skipped. Post-fix captured as post_create_failure."""
        client = FakeD4HClient(
            incident_id=99,
            post_involved_person_raises=D4HClientError("sex enum 400"),
        )
        ocr = {**SAMPLE_OCR, **SAMPLE_OCR_INVOLVED}
        activity_id, unmapped, post_failures = _create_incident_with_subject(
            ocr, SAMPLE_DT, [], SAMPLE_DISP, "sar-dispatch-sccssar-dev",
            client=client,
        )
        assert activity_id == 99
        assert any("involved-person POST failed" in f for f in post_failures)
        assert any("sex enum 400" in f for f in post_failures)

    def test_both_tag_and_involved_person_failures_captured(self):
        """Defense — if BOTH post-create steps fail, the orchestrator
        still returns activity_id and surfaces both failures."""
        client = FakeD4HClient(
            incident_id=99,
            post_tags_raises=D4HServerError("tags down"),
            post_involved_person_raises=D4HClientError("ip down"),
        )
        ocr = {**SAMPLE_OCR, **SAMPLE_OCR_INVOLVED}
        activity_id, unmapped, post_failures = _create_incident_with_subject(
            ocr, SAMPLE_DT, [], SAMPLE_DISP, "sar-dispatch-sccssar-dev",
            client=client,
        )
        assert activity_id == 99
        assert len(post_failures) == 2

    # -- Dispatch-time drone-attach (moved from per-YES 2026-05-19) --
    def test_uas_group_dispatched_triggers_drone_attach(self):
        """When the dispatcher selects `UAS` as a paged group, drone-attach
        runs once at incident-create time as the 4th post-create step.
        Group-dependent, not pilot-dependent (per Bill 2026-05-19)."""
        client = FakeD4HClient(
            incident_id=99,
            equipment={"id": 42, "ref": "Drone #6", "kind": {"title": "UAS"}},
        )
        ocr = {**SAMPLE_OCR, **SAMPLE_OCR_INVOLVED}
        activity_id, unmapped, post_failures = _create_incident_with_subject(
            ocr, SAMPLE_DT, ["UAS"], SAMPLE_DISP, "sar-dispatch-sccssar-dev",
            client=client,
        )
        assert activity_id == 99
        assert post_failures == []
        # Drone-attach POST happened
        post_eq_calls = [c for c in client.calls if c[0] == "_post_equipment_usage"]
        assert len(post_eq_calls) == 1
        assert post_eq_calls[0][1:] == (99, 42, 0)

    def test_non_uas_dispatch_does_not_trigger_drone_attach(self):
        """Dispatching Canine + Tech Rescue (no UAS) → no drone-attach."""
        client = FakeD4HClient(
            incident_id=99,
            equipment={"id": 42, "ref": "Drone #6", "kind": {"title": "UAS"}},
        )
        ocr = {**SAMPLE_OCR, **SAMPLE_OCR_INVOLVED}
        _create_incident_with_subject(
            ocr, SAMPLE_DT, ["Canine", "Technical Rescue"],
            SAMPLE_DISP, "sar-dispatch-sccssar-dev", client=client,
        )
        assert not any(c[0] == "_post_equipment_usage" for c in client.calls)
        # Equipment lookup also skipped — early-exit short-circuit
        assert not any(c[0] == "_get_equipment_by_ref" for c in client.calls)

    def test_drone_attach_failure_captured_not_raised(self):
        """If drone-attach 4xx/5xx fires, capture as post_create_failure;
        orchestrator still returns activity_id (incident already exists)."""
        # Use a FakeD4HClient with a raising _post_equipment_usage
        client = FakeD4HClient(
            incident_id=99,
            equipment={"id": 42, "ref": "Drone #6", "kind": {"title": "UAS"}},
        )

        # Patch _post_equipment_usage to raise
        original = client._post_equipment_usage
        def raising_post(activity_id, equipment_id, duration=0):
            client.calls.append(("_post_equipment_usage", activity_id, equipment_id, duration))
            raise D4HClientError("drone down")
        client._post_equipment_usage = raising_post

        ocr = {**SAMPLE_OCR, **SAMPLE_OCR_INVOLVED}
        activity_id, unmapped, post_failures = _create_incident_with_subject(
            ocr, SAMPLE_DT, ["UAS"], SAMPLE_DISP, "sar-dispatch-sccssar-dev",
            client=client,
        )
        assert activity_id == 99
        assert any("drone-attach failed" in f for f in post_failures)
        assert any("drone down" in f for f in post_failures)


# --------------------------------------------------------------------------
# mark_member_attending orchestrator (Selective mode — POST-new)
# --------------------------------------------------------------------------
# Selective mode (fullTeam: false) — attendance starts EMPTY at incident
# creation; each YES-reply POSTs a fresh ATTENDING record.
#
# Composition: _get_member_by_email → _get_attendance (idempotency check) →
# _post_attendance(payload). Three return paths covered:
#   - "success" / detail=<new attendance_id>     — happy path, POST succeeded
#   - "already_attending" / detail=<existing id> — idempotent retry skip
#   - "member_not_found" / detail=<email>        — email not in D4H members
#
# Same architectural contract as Task 3.3: mirror body = production body
# EXCEPT for 3 `client.` prefixes on the wrapper calls.


def _extract_attendance_member_id(attendance):
    """Mirror of d4h._extract_attendance_member_id — nested member.id reader."""
    if not isinstance(attendance, dict):
        return None
    member = attendance.get("member")
    if not isinstance(member, dict):
        return None
    member_id = member.get("id")
    if not isinstance(member_id, int):
        return None
    return member_id


# --- Mirror ---
def _resolve_k9_handler_role_id(member_email, eb_groups, *, client):
    """Mirror of d4h._resolve_k9_handler_role_id — takes `client` kwarg.

    Production version omits client kwarg and calls module-level helpers.
    Same case-insensitive `canine` group match as production.
    """
    if "canine" not in {g.casefold() for g in eb_groups}:
        return None
    try:
        member = client._get_member_by_email(member_email)
        if member is None:
            return None
        handlers = client._get_handlers_for_member(member["id"])
    except (D4HClientError, D4HServerError):
        return None
    if handlers:
        return K9_HANDLER_ROLE_ID
    return None


# --- Mirror ---
def _mark_member_attending(activity_id, member_email, *, client, now=None, role_id=None):
    """Mirror of d4h.mark_member_attending — takes `client` kwarg.

    Production version omits client kwarg and calls module-level wrappers
    directly. `now` is injected by tests to make startsAt/endsAt deterministic.
    Production calls datetime.now(timezone.utc) internally.
    `role_id` is forwarded to the payload when non-None (PR-1, 2026-06-03 —
    K9 Handler role tagging on attendance).
    """
    member = client._get_member_by_email(member_email)
    if member is None:
        return {"status": "member_not_found", "detail": member_email}

    member_id = member["id"]

    existing = client._get_attendance(activity_id)
    for r in existing:
        if _extract_attendance_member_id(r) == member_id:
            return {"status": "already_attending", "detail": r.get("id")}

    now = now or datetime.now(timezone.utc)
    payload = {
        "activityId": activity_id,
        "memberId":   member_id,
        "status":     STATUS_ATTENDING,
        "startsAt":   _format_d4h_datetime(now),
        # +60s — smallest value satisfying D4H's endsAt > startsAt range
        # check while remaining an obvious placeholder for the dispatcher.
        "endsAt":     _format_d4h_datetime(now + timedelta(seconds=60)),
    }
    if role_id is not None:
        payload["roleId"] = role_id
    response = client._post_attendance(payload)
    return {"status": "success", "detail": response.get("id")}


class TestMarkMemberAttending:
    def test_success_path_posts_new_attendance_record(self):
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "bill@sccssar.org", "verified": True}},
            attendance=[],  # Selective mode — starts empty
        )
        fixed_now = datetime(2026, 5, 19, 22, 30, 0, tzinfo=timezone.utc)
        result = _mark_member_attending(
            99, "bill@sccssar.org", client=client, now=fixed_now,
        )
        assert result == {"status": "success", "detail": 999999}
        post_call = [c for c in client.calls if c[0] == "_post_attendance"][0]
        payload = post_call[1]
        assert payload["activityId"] == 99
        assert payload["memberId"]   == 555
        assert payload["status"]     == "ATTENDING"
        assert payload["startsAt"]   == "2026-05-19T22:30:00.000Z"
        # endsAt = startsAt + 60s — smallest value satisfying D4H's
        # `endsAt > startsAt` Zod schema (live-verified 2026-05-19: equal
        # values 400 with attendance:invalidDatetimeRange). A 60-second
        # window is operationally impossible for SAR attendance, so it
        # still visually screams "dispatcher must edit at close-out."
        assert payload["endsAt"]     == "2026-05-19T22:31:00.000Z"

    def test_idempotency_existing_attendance_skips_post(self):
        """Cloud Tasks may retry this task after a downstream step (drone-add)
        raises 5xx. Without an existence check, retries POST duplicate rows."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "bill@sccssar.org", "verified": True}},
            attendance=[
                # Note: GET shape nests member.id (NOT top-level memberId)
                {"id": 12345, "member": {"id": 555}, "status": "ATTENDING"},
            ],
        )
        result = _mark_member_attending(99, "bill@sccssar.org", client=client)
        assert result == {"status": "already_attending", "detail": 12345}
        # Must NOT have called _post_attendance — that would duplicate.
        assert not any(c[0] == "_post_attendance" for c in client.calls)

    def test_member_not_found_returns_status(self):
        client = FakeD4HClient(member=None)
        result = _mark_member_attending(99, "unknown@example.com", client=client)
        assert result["status"] == "member_not_found"
        assert result["detail"] == "unknown@example.com"
        # No POST or GET attendance — early-exit at member lookup.
        assert not any(c[0] == "_post_attendance" for c in client.calls)
        assert not any(c[0] == "_get_attendance" for c in client.calls)

    def test_other_members_present_do_not_block_this_post(self):
        """Existence check is per-member, not 'any attendance record exists'.
        Sibling responder records must NOT cause us to skip this responder."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "bill@sccssar.org", "verified": True}},
            attendance=[
                {"id": 1, "member": {"id": 100}, "status": "ATTENDING"},
                {"id": 2, "member": {"id": 200}, "status": "ATTENDING"},
            ],
        )
        result = _mark_member_attending(99, "bill@sccssar.org", client=client)
        assert result["status"] == "success"
        assert any(c[0] == "_post_attendance" for c in client.calls)

    def test_post_attendance_5xx_propagates(self):
        """D4HServerError from _post_attendance MUST propagate so the
        Cloud Tasks worker can return 502 → retries per max_attempts=5."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "bill@sccssar.org", "verified": True}},
            attendance=[],
            post_attendance_raises=D4HServerError("D4H down"),
        )
        with pytest.raises(D4HServerError, match="D4H down"):
            _mark_member_attending(99, "bill@sccssar.org", client=client)


class TestMarkMemberAttendingRoleId:
    """PR-1 (2026-06-03): mark_member_attending accepts optional role_id kwarg.
    When provided, it appears as 'roleId' in the POST payload — populates the
    Attendance tab Role column. When None (default), the payload key is omitted
    entirely so the prior payload shape is preserved (zero behavior change for
    non-K9 responders)."""

    def test_role_id_omitted_when_none(self):
        """Default behavior — no role_id passed, payload has no roleId key.
        Preserves the prior payload shape for non-K9 responders."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "bill@sccssar.org", "verified": True}},
            attendance=[],
        )
        _mark_member_attending(99, "bill@sccssar.org", client=client)
        post_call = [c for c in client.calls if c[0] == "_post_attendance"][0]
        assert "roleId" not in post_call[1], \
            "Default payload must NOT contain roleId key when role_id is None"

    def test_role_id_included_when_provided(self):
        """When role_id is set, 'roleId' appears in the payload with that value."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "kris@sccssar.org", "verified": True}},
            attendance=[],
        )
        _mark_member_attending(99, "kris@sccssar.org", client=client, role_id=K9_HANDLER_ROLE_ID)
        post_call = [c for c in client.calls if c[0] == "_post_attendance"][0]
        assert post_call[1]["roleId"] == K9_HANDLER_ROLE_ID
        assert post_call[1]["roleId"] == 11487  # cross-file literal pin


class TestResolveK9HandlerRoleId:
    """PR-1 (2026-06-03): _resolve_k9_handler_role_id decides whether to tag
    the YES-replier as a K9 Handler on the D4H Attendance tab.

    Precision signal: EB Canine group membership is NECESSARY but not SUFFICIENT
    — most Canine group members are qualified drivers/flankers, not handlers.
    The sufficient signal is the existence of /handlers rows for the member.
    """

    _KRIS = {"id": 555, "email": {"value": "kris@sccssar.org", "verified": True}}
    _KRIS_HANDLERS = [
        {"id": 3614, "member": {"id": 555}, "animal": {"id": 2929}},  # Aria
        {"id": 3679, "member": {"id": 555}, "animal": {"id": 2954}},  # Annie
    ]

    def test_canine_group_with_handlers_returns_role_id(self):
        """K9 handler in Canine group → K9_HANDLER_ROLE_ID."""
        client = FakeD4HClient(member=self._KRIS, handlers=self._KRIS_HANDLERS)
        result = _resolve_k9_handler_role_id("kris@sccssar.org", ["Canine"], client=client)
        assert result == K9_HANDLER_ROLE_ID
        assert result == 11487

    def test_canine_group_with_no_handlers_returns_none(self):
        """Canine group member with NO /handlers rows (driver/flanker case) → None.
        Tagging them as K9 Handler would be wrong — they're qualified for K9
        support but aren't actually a handler this incident."""
        client = FakeD4HClient(member=self._KRIS, handlers=[])
        result = _resolve_k9_handler_role_id("driver@sccssar.org", ["Canine"], client=client)
        assert result is None

    def test_non_canine_group_returns_none_without_http(self):
        """Non-K9 dispatch → None, zero HTTP. Most responders take this fast path."""
        client = FakeD4HClient(member=self._KRIS, handlers=self._KRIS_HANDLERS)
        result = _resolve_k9_handler_role_id("bill@sccssar.org", ["UAS", "ATV"], client=client)
        assert result is None
        # Critical: no member lookup, no handlers lookup. The Canine check is the
        # cheap gate — protects non-K9 dispatches from unnecessary HTTP.
        assert not any(c[0] == "_get_member_by_email" for c in client.calls)
        assert not any(c[0] == "_get_handlers_for_member" for c in client.calls)

    def test_canine_group_case_insensitive(self):
        """Group match is case-insensitive (mirrors add_drone_if_uas_dispatched
        pattern). EB rename to lowercase / mixed case should NOT silently
        break role tagging."""
        for variant in ("canine", "Canine", "CANINE", "CaNiNe"):
            client = FakeD4HClient(member=self._KRIS, handlers=self._KRIS_HANDLERS)
            result = _resolve_k9_handler_role_id("kris@sccssar.org", [variant], client=client)
            assert result == K9_HANDLER_ROLE_ID, f"variant {variant!r} should match"

    def test_member_not_found_returns_none(self):
        """Member not in D4H roster → None (graceful degrade). Attendance POST
        still happens via the caller; just no role tag."""
        client = FakeD4HClient(member=None, handlers=self._KRIS_HANDLERS)
        result = _resolve_k9_handler_role_id("unknown@example.com", ["Canine"], client=client)
        assert result is None

    def test_handlers_lookup_d4h_client_error_returns_none(self):
        """D4H 4xx on /handlers → None (graceful degrade), NOT propagated.
        Best-effort by design — don't block attendance POST on a role lookup."""
        client = FakeD4HClient(
            member=self._KRIS,
            get_handlers_raises=D4HClientError("D4H 404"),
        )
        result = _resolve_k9_handler_role_id("kris@sccssar.org", ["Canine"], client=client)
        assert result is None

    def test_handlers_lookup_d4h_server_error_returns_none(self):
        """D4H 5xx on /handlers → None (graceful degrade), NOT propagated.
        Mirrors the 4xx path — the role-lookup failure mode is best-effort
        regardless of HTTP class. (Q5 of the Failure-mode Discipline 6-question
        rubric for this PR.)"""
        client = FakeD4HClient(
            member=self._KRIS,
            get_handlers_raises=D4HServerError("D4H 503"),
        )
        result = _resolve_k9_handler_role_id("kris@sccssar.org", ["Canine"], client=client)
        assert result is None


# --------------------------------------------------------------------------
# add_drone_if_uas_dispatched orchestrator (dispatch-time, 2026-05-19+)
# --------------------------------------------------------------------------
# Per Bill 2026-05-19 — drone is group-dependent (was UAS dispatched?),
# not pilot-dependent (did a pilot YES?). Lives in create_incident_with_subject
# as the 4th post-create step.
#
# Composition: early-exit if `UAS` not in dispatched_eb_groups →
# _get_equipment_by_ref → _post_equipment_usage.
# Three return paths covered:
#   - False (early-exit) — `UAS` not in dispatched groups. NO HTTP calls.
#   - False (drone not found) — _get_equipment_by_ref returned None.
#   - True (successful attach) — _post_equipment_usage was called.
#
# The prior idempotency check (existing_usages) is gone — it was guarding
# against per-YES race conditions which no longer exist now that this runs
# once at dispatch-time.


# --- Mirror ---
def _add_drone_if_uas_dispatched(activity_id, dispatched_eb_groups, *, client):
    """Mirror of d4h.add_drone_if_uas_dispatched — takes `client` kwarg.
    Production version omits client kwarg and calls module-level wrappers.

    Renamed + simplified from prior _add_drone_if_uas_pilot (per Bill 2026-05-19):
    drone allocation is group-dependent (was UAS dispatched?), not
    pilot-dependent (did a pilot YES?). The prior idempotency check on
    existing_usages was guarding against per-YES race conditions which no
    longer exist now that this runs once at dispatch time.

    Cluster D — case-insensitive match. _map_eb_groups_to_d4h_tags already
    lowercases internally; defensive matching here covers a future EB rename."""
    if "uas" not in {g.casefold() for g in dispatched_eb_groups}:
        return False
    drone = client._get_equipment_by_ref(DRONE_REF, DRONE_KIND_TITLE)
    if drone is None:
        return False
    client._post_equipment_usage(activity_id, drone["id"], duration=0)
    return True


class TestAddDroneIfUasDispatched:
    def test_uas_not_dispatched_short_circuits(self):
        """Early-exit: no HTTP calls when UAS isn't in the dispatched groups."""
        client = FakeD4HClient()
        added = _add_drone_if_uas_dispatched(99, ["Canine", "ATV"], client=client)
        assert added is False
        assert client.calls == []  # no HTTP calls at all

    def test_drone_not_found_returns_false(self):
        client = FakeD4HClient(equipment=None)
        added = _add_drone_if_uas_dispatched(99, ["UAS"], client=client)
        assert added is False
        # _get_equipment_by_ref was called; nothing else after
        assert any(c[0] == "_get_equipment_by_ref" for c in client.calls)
        assert not any(c[0] == "_post_equipment_usage" for c in client.calls)

    def test_success_path_posts_equipment_usage(self):
        client = FakeD4HClient(
            equipment={"id": 42, "ref": "Drone #6", "kind": {"title": "UAS"}},
        )
        added = _add_drone_if_uas_dispatched(99, ["UAS"], client=client)
        assert added is True
        post_call = [c for c in client.calls if c[0] == "_post_equipment_usage"][0]
        assert post_call[1:] == (99, 42, 0)

    def test_uas_alongside_other_groups_still_triggers(self):
        """UAS in a multi-group dispatch (e.g., UAS + Canine + Tech Rescue)
        still triggers drone-attach."""
        client = FakeD4HClient(
            equipment={"id": 42, "ref": "Drone #6", "kind": {"title": "UAS"}},
        )
        added = _add_drone_if_uas_dispatched(
            99, ["Canine", "UAS", "Technical Rescue"], client=client,
        )
        assert added is True

    def test_uas_match_is_case_insensitive(self):
        """Cluster D — D4H-L7. Pre-fix: `"UAS" not in [...]` was case-sensitive.
        Kris's 2026-05-19 EB rebuild already changed group names once; a
        future rename to lowercase would silently skip drone-attach with
        no log or error. _map_eb_groups_to_d4h_tags already lowercases
        internally — this defensive match keeps the two layers in sync."""
        client = FakeD4HClient(
            equipment={"id": 42, "ref": "Drone #6", "kind": {"title": "UAS"}},
        )
        # All-lowercase variant (the failure mode Melanie flagged)
        assert _add_drone_if_uas_dispatched(99, ["uas"], client=client) is True
        # Mixed-case
        client2 = FakeD4HClient(
            equipment={"id": 42, "ref": "Drone #6", "kind": {"title": "UAS"}},
        )
        assert _add_drone_if_uas_dispatched(99, ["Uas"], client=client2) is True


# Mirrors of the two animal-attendance constants in d4h.py. Pinned against
# production by TestAnimalAttendanceRouteAndPayload below — d4h.py is not
# importable under local pytest (no httpx), so these values are only as
# trustworthy as that pin.
ANIMAL_ATTENDANCE_DURATION_MIN = 1
ACTIVITY_RESOURCE_TYPE_INCIDENT = "Incident"


# --- Mirror ---
def _extract_handler_animal_id(handler):
    """Mirror of d4h._extract_handler_animal_id."""
    if not isinstance(handler, dict):
        return None
    animal = handler.get("animal")
    if not isinstance(animal, dict):
        return None
    animal_id = animal.get("id")
    if not isinstance(animal_id, int):
        return None
    return animal_id


class TestExtractHandlerAnimalId:
    def test_reads_nested_animal_id(self):
        assert _extract_handler_animal_id({"animal": {"id": 3017}}) == 3017

    def test_top_level_animal_id_is_not_read(self):
        """Live /handlers rows carry `animal.id` and no top-level `animalId`.
        Reading the flat key is the defect that made _extract_attendance_member_id
        necessary; pinned here so the same bug cannot recur on animals."""
        assert _extract_handler_animal_id({"animalId": 3017}) is None

    @pytest.mark.parametrize("row", [
        None, "not-a-dict", {}, {"animal": None}, {"animal": "x"},
        {"animal": {}}, {"animal": {"id": None}}, {"animal": {"id": "3017"}},
    ])
    def test_malformed_layers_return_none(self, row):
        assert _extract_handler_animal_id(row) is None


# --- Mirror ---
def _sync_k9_attendance(activity_id, member_email, *, client):
    """Mirror of d4h.sync_k9_attendance — takes a `client` kwarg; production
    calls module-level helpers directly."""
    member = client._get_member_by_email(member_email)
    if member is None:
        return {"status": "member_not_found"}

    handlers = client._get_handlers_for_member(member["id"])
    animal_ids = [
        animal_id
        for animal_id in (_extract_handler_animal_id(h) for h in handlers)
        if animal_id is not None
    ]
    if not animal_ids:
        return {"status": "not_a_handler"}

    already_present = {
        animal_id
        for animal_id in (
            _extract_handler_animal_id(r)
            for r in client._get_animal_attendance(activity_id)
        )
        if animal_id is not None
    }

    created = 0
    skipped = 0
    for animal_id in animal_ids:
        if animal_id in already_present:
            skipped += 1
            continue
        client._post_animal_attendance({
            "animalId": animal_id,
            "memberId": member["id"],
            "activity": {
                "id": activity_id,
                "resourceType": ACTIVITY_RESOURCE_TYPE_INCIDENT,
            },
            "duration": ANIMAL_ATTENDANCE_DURATION_MIN,
        })
        created += 1
    return {"status": "success", "created": created, "skipped": skipped}


_KRIS = {"id": 120358, "email": {"value": "kris@sccssar.org", "verified": True}}


class TestSyncK9Attendance:
    """Issue #756. Contract measured against live team 1775 2026-08-18 by
    experiments/d4h/24_animal_attendance_shape.py, which created nothing."""

    def test_member_not_found_short_circuits_before_any_write(self):
        client = FakeD4HClient(member=None)
        assert _sync_k9_attendance(99, "ghost@sccssar.org", client=client) == {
            "status": "member_not_found"}
        assert [c[0] for c in client.calls] == ["_get_member_by_email"]

    def test_member_with_no_dogs_is_not_a_handler(self):
        client = FakeD4HClient(member=_KRIS, handlers=[])
        assert _sync_k9_attendance(99, "kris@sccssar.org", client=client) == {
            "status": "not_a_handler"}
        assert "_post_animal_attendance" not in [c[0] for c in client.calls]

    def test_one_row_posted_per_dog(self):
        """A handler with two dogs must produce TWO rows — the idempotency
        check is keyed on the ANIMAL, not on the member as it is for
        /attendance. Both of the team's K9 handlers on the 2026-08-18 callout
        had exactly this shape."""
        client = FakeD4HClient(
            member=_KRIS,
            handlers=[{"animal": {"id": 2865}}, {"animal": {"id": 3149}}],
            animal_attendance=[],
        )
        result = _sync_k9_attendance(1732308, "kris@sccssar.org", client=client)
        assert result == {"status": "success", "created": 2, "skipped": 0}
        posted = [c[1] for c in client.calls if c[0] == "_post_animal_attendance"]
        assert [p["animalId"] for p in posted] == [2865, 3149]

    def test_payload_shape_matches_the_measured_contract(self):
        client = FakeD4HClient(
            member=_KRIS, handlers=[{"animal": {"id": 2865}}], animal_attendance=[])
        _sync_k9_attendance(1732308, "kris@sccssar.org", client=client)
        payload = [c[1] for c in client.calls if c[0] == "_post_animal_attendance"][0]
        assert payload == {
            "animalId": 2865,
            "memberId": 120358,
            "activity": {"id": 1732308, "resourceType": "Incident"},
            "duration": 1,
        }

    def test_status_is_never_sent(self):
        """D4H's body schema is STRICT: `status` comes back as an
        unrecognized_keys 400, not a silent drop. Animal attendance has no
        ABSENT — the row's existence IS the attendance. Sending the member
        endpoint's status field would 400 every K9 sync."""
        client = FakeD4HClient(
            member=_KRIS, handlers=[{"animal": {"id": 2865}}], animal_attendance=[])
        _sync_k9_attendance(1732308, "kris@sccssar.org", client=client)
        payload = [c[1] for c in client.calls if c[0] == "_post_animal_attendance"][0]
        for rejected in ("status", "startsAt", "endsAt", "activityId"):
            assert rejected not in payload, (
                f"{rejected!r} is an unrecognized key on /animal-attendance "
                f"and 400s the whole request"
            )

    def test_existing_row_is_skipped(self):
        client = FakeD4HClient(
            member=_KRIS,
            handlers=[{"animal": {"id": 2865}}, {"animal": {"id": 3149}}],
            animal_attendance=[{"animal": {"id": 2865}}],
        )
        result = _sync_k9_attendance(1732308, "kris@sccssar.org", client=client)
        assert result == {"status": "success", "created": 1, "skipped": 1}
        posted = [c[1] for c in client.calls if c[0] == "_post_animal_attendance"]
        assert [p["animalId"] for p in posted] == [3149]

    def test_all_rows_present_posts_nothing(self):
        client = FakeD4HClient(
            member=_KRIS,
            handlers=[{"animal": {"id": 2865}}],
            animal_attendance=[{"animal": {"id": 2865}}],
        )
        result = _sync_k9_attendance(1732308, "kris@sccssar.org", client=client)
        assert result == {"status": "success", "created": 0, "skipped": 1}
        assert "_post_animal_attendance" not in [c[0] for c in client.calls]

    def test_malformed_handler_rows_do_not_crash_or_post(self):
        client = FakeD4HClient(
            member=_KRIS, handlers=[{"animal": None}, {}], animal_attendance=[])
        assert _sync_k9_attendance(99, "kris@sccssar.org", client=client) == {
            "status": "not_a_handler"}


class TestAnimalAttendanceRouteAndPayload:
    """Production-reading pins. The mirrors above are hand-written copies —
    they stay green no matter what d4h.py does, so these are what actually
    hold the contract."""

    @staticmethod
    def _d4h_source() -> str:
        return (Path(__file__).parent / "d4h.py").read_text(encoding="utf-8")

    @staticmethod
    def _func_src(src: str, name: str) -> str:
        """Slice one function, bounded at BOTH ends on a real end marker (the
        next top-level def), never start+N chars."""
        # End marker is the next TOP-LEVEL statement (a blank line then column-0
        # text), not the next `def` — sync_k9_attendance is followed by module
        # constants, so a def-only marker swallowed them into the slice.
        m = re.search(rf"^def {name}\(.*?(?=\n\n[^\s]|\Z)", src,
                      re.DOTALL | re.MULTILINE)
        assert m, f"{name} not found in d4h.py (renamed? moved?)"
        return m.group(0)

    @staticmethod
    def _code_only(text: str) -> str:
        """Strip comments AND the docstring, so a negative assertion cannot be
        satisfied by prose that merely discusses the literal."""
        body = re.sub(r'^\s*""".*?"""', "", text, count=1, flags=re.DOTALL | re.MULTILINE)
        return "\n".join(line.split("#")[0] for line in body.splitlines())

    def test_route_is_singular_in_both_helpers(self):
        """The plural spelling 404s. Our own stub documented it plural, which
        is why a ~10-path sweep concluded the endpoint had not shipped."""
        src = self._d4h_source()
        for name in ("_get_animal_attendance", "_post_animal_attendance"):
            code = self._code_only(self._func_src(src, name))
            assert '/animal-attendance"' in code, (
                f"{name} no longer builds the singular /animal-attendance URL")
            assert "animal-attendances" not in code, (
                f"{name} uses the PLURAL route, which 404s (issue #756)")

    def test_no_plural_route_anywhere_in_the_module(self):
        code = self._code_only(self._d4h_source())
        assert "animal-attendances" not in code

    def test_get_uses_snake_case_query_param(self):
        """camelCase `activityId` is a 400 on this endpoint — same
        inconsistency as /attendance."""
        code = self._code_only(
            self._func_src(self._d4h_source(), "_get_animal_attendance"))
        assert '"activity_id": activity_id' in code
        assert "activityId" not in code

    def test_sync_calls_the_post_helper(self):
        """Assert the CALL SITE, not the bare identifier — a name can appear
        in an import, a docstring, or a dead branch."""
        code = self._code_only(
            self._func_src(self._d4h_source(), "sync_k9_attendance"))
        assert "_post_animal_attendance({" in code
        assert "_get_animal_attendance(activity_id)" in code

    def test_sync_payload_uses_the_constants_not_inline_literals(self):
        code = self._code_only(
            self._func_src(self._d4h_source(), "sync_k9_attendance"))
        assert '"resourceType": ACTIVITY_RESOURCE_TYPE_INCIDENT' in code
        assert '"duration": ANIMAL_ATTENDANCE_DURATION_MIN' in code
        assert '"activity": {' in code, (
            "activity must be an OBJECT — D4H rejects a bare activityId int here")

    def test_sync_never_sends_status(self):
        code = self._code_only(
            self._func_src(self._d4h_source(), "sync_k9_attendance"))
        assert '"status": STATUS_' not in code
        assert "STATUS_ABSENT" not in code, (
            "animal attendance has no ABSENT — sending status 400s the request")

    def test_constants_match_the_mirrors(self):
        src = self._d4h_source()
        assert f"ANIMAL_ATTENDANCE_DURATION_MIN = {ANIMAL_ATTENDANCE_DURATION_MIN}" in src, (
            "the 1-minute placeholder drifted in d4h.py; 1 is the smallest "
            "value D4H accepts on an attendance record (Bill 2026-08-18)")
        assert (f'ACTIVITY_RESOURCE_TYPE_INCIDENT = "{ACTIVITY_RESOURCE_TYPE_INCIDENT}"'
                in src)

    def test_mirror_matches_production(self):
        """The behavioural tests above run against _sync_k9_attendance, a
        hand-written copy. Without this the mutation set that matters most —
        dropping the not_a_handler guard, the member_not_found guard, or the
        idempotency skip — mutates production and leaves every behavioural
        test green, because they never read production at all.

        Compared by AST after normalizing the two deliberate differences: the
        mirror takes a `client` kwarg and calls `client._helper(...)` where
        production calls the module-level `_helper(...)`.
        """
        prod = self._func_src(self._d4h_source(), "sync_k9_attendance")
        prod = prod.replace(
            "def sync_k9_attendance(activity_id: int, member_email: str) -> dict:",
            "def _sync_k9_attendance(activity_id, member_email, *, client):",
        )
        for helper in ("_get_member_by_email", "_get_handlers_for_member",
                       "_get_animal_attendance", "_post_animal_attendance"):
            prod = prod.replace(f"{helper}(", f"client.{helper}(")
        shape = TestZodIssuesMirrorParity._shape
        assert shape(prod) == shape(inspect.getsource(_sync_k9_attendance)), (
            "_sync_k9_attendance in test_d4h.py has drifted from "
            "sync_k9_attendance in backend/d4h.py — the behavioural tests "
            "above are exercising a stale copy"
        )

    def test_per_yes_caller_swallows_k9_failures(self):
        """Structure, not keyword presence: the try must OPEN before the call
        and the except must CLOSE after it. Attendance has already been POSTed
        by this point, so a K9 error must not fail the Cloud Task."""
        src = self._d4h_source()
        body = self._func_src(src, "handle_per_yes_sync_task")
        lines = body.splitlines()
        call_line = next(
            i for i, ln in enumerate(lines)
            if "sync_k9_attendance(activity_id, member_email)" in ln
        )
        after = "\n".join(lines[call_line:])
        preceding = [ln.strip() for ln in lines[:call_line]
                     if ln.strip() and not ln.strip().startswith("#")]
        assert preceding[-1] == "try:", (
            "the K9 sync call is no longer the first statement inside a try: — "
            f"preceding line is {preceding[-1]!r}")
        assert "except D4HServerError:" in after, (
            "transient K9 failures are no longer retriable — a 5xx would be "
            "swallowed and the row permanently lost (Failure-mode Q5)")
        assert "except Exception" in after, (
            "non-retriable K9 failures are no longer swallowed — a 4xx would "
            "fail the task and burn the retry budget to no effect")
        # The branch BODY, not just its presence. `except D4HServerError: pass`
        # satisfies every assertion above while silently discarding exactly the
        # failure the branch exists to retry — and the mirror cannot catch it,
        # because handle_per_yes_sync_task's mirror is hand-written.
        retry_body = after.split("except D4HServerError:", 1)[1].splitlines()
        first_stmt = next(ln.strip() for ln in retry_body
                          if ln.strip() and not ln.strip().startswith("#"))
        assert first_stmt == "raise", (
            f"the D4HServerError branch no longer re-raises (found {first_stmt!r}) "
            f"— a transient K9 failure would be swallowed and the row lost")
        assert after.index("except D4HServerError:") < after.index("except Exception"), (
            "the broad handler now shadows D4HServerError — order is "
            "load-bearing, Python takes the first matching except")
        assert "NotImplementedError" not in body, (
            "the v1.1 stub guard is stale: sync_k9_attendance is implemented")


# --- Mirror ---
def _enqueue_d4h_task(queue_name, task_name, target_path, payload):
    """Mirror of d4h._enqueue_d4h_task. Calls google.cloud.tasks_v2 via the
    module the test patches into sys.modules."""
    from google.cloud import tasks_v2
    import json as _json
    import os
    project_id  = os.environ.get("GCP_PROJECT", "").strip()
    region      = os.environ.get("GCP_REGION", "us-central1").strip()
    service_url = os.environ.get("CLOUD_RUN_SERVICE_URL", "").strip()
    sa_email    = os.environ.get("CLOUD_TASKS_SERVICE_ACCOUNT", "").strip()
    if not project_id or not service_url or not sa_email:
        raise ValueError(
            "Cloud Tasks env vars missing: requires GCP_PROJECT, "
            "CLOUD_RUN_SERVICE_URL, CLOUD_TASKS_SERVICE_ACCOUNT. "
            "Configured in Terraform per PR 4."
        )
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(project_id, region, queue_name)
    full_task_name = f"{parent}/tasks/{task_name}"
    base_audience = service_url.rstrip('/')
    task = {
        "name": full_task_name,
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{base_audience}{target_path}",
            "headers": {"Content-Type": "application/json"},
            "body": _json.dumps(payload).encode("utf-8"),
            "oidc_token": {
                "service_account_email": sa_email,
                "audience": base_audience,
            },
        },
    }
    client.create_task(request={"parent": parent, "task": task})


# --------------------------------------------------------------------------
# handle_per_yes_sync_task Cloud Tasks worker (Selective mode)
# --------------------------------------------------------------------------
# Composition: _load_d4h_activity_id (Firestore) → mark_member_attending →
# (drone-attach moved out 2026-05-19 — see create_incident_with_subject instead) →
# sync_k9_attendance (conditional on any "DOGS - *" group; swallows the v1.2
# NotImplementedError).
#
# Selective mode (fullTeam: false):
#   - No activity_id in Firestore → graceful-degrade (return, no D4H calls)
#   - mark_member_attending returns "success" / "already_attending" /
#     "member_not_found" — none trigger an explicit re-raise. Cloud Tasks
#     retry routing happens via D4HServerError raised by _post_attendance
#     (5xx) propagating up through this function naturally.
#   - K9 sync NotImplementedError swallowed (v1.1 contract; v1.2 will fill)
#
# Mirror divergences from production:
#   - `client` + `load_activity_id` kwargs (production calls module-level
#     helpers directly)
#   - 2 wrapper-call `client.` prefixes (mark_attending uses _mark_member_attending
#     mirror; drone-attach no longer per-YES — moved to create_incident_with_subject)
#   - production logs the graceful-degrade case via logger.info; mirror omits it
#   - sync_k9_attendance is the same call in both (no client kwarg — the stub
#     just raises NotImplementedError unconditionally)


# --- Mirror ---
_QUEUE_PER_YES_SYNC = "d4h-per-yes-sync"
_TARGET_PATH_PER_YES_SYNC = "/d4h-sync-yes"


def _sanitize_task_name(raw):
    """Mirror of d4h._sanitize_task_name — Cloud Tasks accepts only [A-Za-z0-9_-]."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", raw)


def enqueue_per_yes_sync(event_id, member_email, eb_groups):
    """Mirror of d4h.enqueue_per_yes_sync."""
    safe_email = member_email.replace("@", "-at-").replace(".", "-")
    task_name = _sanitize_task_name(f"yes-{event_id}-{safe_email}")
    _enqueue_d4h_task(
        queue_name=_QUEUE_PER_YES_SYNC,
        task_name=task_name,
        target_path=_TARGET_PATH_PER_YES_SYNC,
        payload={"event_id": event_id, "member_email": member_email, "eb_groups": list(eb_groups)},
    )


class TestEnqueuePerYesSync:
    """Mirror-based tests for d4h.enqueue_per_yes_sync Cloud Tasks enqueue."""

    def _install_fake_tasks_v2(self, monkeypatch, captured):
        import sys, types
        class FakeTasksClient:
            def queue_path(self, project, region, queue):
                return f"projects/{project}/locations/{region}/queues/{queue}"
            def create_task(self, request):
                captured["request"] = request
                return None
        fake_module = types.ModuleType("google.cloud.tasks_v2")
        fake_module.CloudTasksClient = FakeTasksClient
        fake_module.HttpMethod = types.SimpleNamespace(POST="POST_SENTINEL")
        monkeypatch.setitem(sys.modules, "google.cloud.tasks_v2", fake_module)
        if "google.cloud" not in sys.modules:
            parent = types.ModuleType("google.cloud")
            parent.tasks_v2 = fake_module
            monkeypatch.setitem(sys.modules, "google.cloud", parent)

    def _set_env(self, monkeypatch, service_url="https://dispatch-console-xyz.run.app"):
        monkeypatch.setenv("GCP_PROJECT", "sar-dispatch-sccssar-dev")
        monkeypatch.setenv("GCP_REGION", "us-central1")
        monkeypatch.setenv("CLOUD_RUN_SERVICE_URL", service_url)
        monkeypatch.setenv("CLOUD_TASKS_SERVICE_ACCOUNT", "sa@project.iam.gserviceaccount.com")

    def test_deterministic_task_name_and_payload(self, monkeypatch):
        captured = {}
        self._install_fake_tasks_v2(monkeypatch, captured)
        self._set_env(monkeypatch)
        enqueue_per_yes_sync("2026-05-18T14-00Z", "bill.burns@sccssar.org", ["K9 - Trailing"])
        req = captured["request"]
        import json as _json
        assert req["task"]["name"].endswith(
            "tasks/yes-2026-05-18T14-00Z-bill-burns-at-sccssar-org"
        )
        payload = _json.loads(req["task"]["http_request"]["body"])
        assert payload == {
            "event_id": "2026-05-18T14-00Z",
            "member_email": "bill.burns@sccssar.org",
            "eb_groups": ["K9 - Trailing"],
        }
        assert req["task"]["http_request"]["url"] == (
            "https://dispatch-console-xyz.run.app/d4h-sync-yes"
        )
        oidc = req["task"]["http_request"]["oidc_token"]
        assert oidc["service_account_email"] == "sa@project.iam.gserviceaccount.com"
        assert oidc["audience"] == "https://dispatch-console-xyz.run.app", (
            "OIDC audience MUST equal CLOUD_RUN_SERVICE_URL (no path) — same constraint as PR #432"
        )

    def test_audience_strips_trailing_slash(self, monkeypatch):
        captured = {}
        self._install_fake_tasks_v2(monkeypatch, captured)
        self._set_env(monkeypatch, service_url="https://dispatch-console-xyz.run.app/")
        enqueue_per_yes_sync("evt", "a@b.org", [])
        oidc = captured["request"]["task"]["http_request"]["oidc_token"]
        assert oidc["audience"] == "https://dispatch-console-xyz.run.app"

    def test_apostrophe_in_agency_name_does_not_break_the_enqueue(self, monkeypatch):
        """LIVE REGRESSION, personal-dev 2026-08-01.

        Verbatim event_id from the failing dispatch. Cloud Tasks task IDs accept
        only [A-Za-z0-9_-]; `_slugify_for_firestore` lowercases and joins on
        whitespace but does not strip punctuation, so "Humboldt County
        Sheriff's Office" carried an apostrophe into the task name and the
        enqueue failed with InvalidArgument.

        The caller logs and continues, so the dispatch completed and the
        responder was paged, added to Slack, and NEVER synced to D4H
        attendance — for every responder, silently. In-county agencies are
        canonicalized (SCCSO/SJPD/MPD) and have no punctuation, so only
        out-of-county MUTUAL AID hits it.
        """
        captured = {}
        self._install_fake_tasks_v2(monkeypatch, captured)
        self._set_env(monkeypatch)
        event_id = "2026-08-01_humboldt_county_sheriff's_office_treatm_1031"
        enqueue_per_yes_sync(event_id, "bill.burns@sccssar.org", [])
        task_name = captured["request"]["task"]["name"].split("/tasks/")[-1]
        assert "'" not in task_name, "the apostrophe is back — enqueue 400s"
        assert re.fullmatch(r"[A-Za-z0-9_-]+", task_name), (
            f"task name {task_name!r} is outside the Cloud Tasks charset"
        )
        # The TRUE event_id must still reach the worker, or it looks up the
        # wrong Firestore doc.
        import json as _json
        payload = _json.loads(captured["request"]["task"]["http_request"]["body"])
        assert payload["event_id"] == event_id

    def test_task_name_stays_deterministic_after_sanitising(self, monkeypatch):
        """The 24h dedup guard depends on the name being a pure function.

        Same event + same responder must produce the same task name, or a
        polling-loop double-fire enqueues twice and D4H gets a duplicate
        ATTENDING row.
        """
        captured = {}
        self._install_fake_tasks_v2(monkeypatch, captured)
        self._set_env(monkeypatch)
        enqueue_per_yes_sync("a'b c/d", "x@y.org", [])
        first = captured["request"]["task"]["name"]
        enqueue_per_yes_sync("a'b c/d", "x@y.org", [])
        second = captured["request"]["task"]["name"]
        assert first == second

    def test_distinct_event_ids_do_not_collide_after_sanitising(self, monkeypatch):
        """Mapping to '-' rather than stripping keeps these apart.

        A strip would turn "sheriff's" and "sheriffs" into the same task name,
        and the second incident's sync would be silently deduped away.
        """
        captured = {}
        self._install_fake_tasks_v2(monkeypatch, captured)
        self._set_env(monkeypatch)
        enqueue_per_yes_sync("sheriff's_office", "x@y.org", [])
        with_apostrophe = captured["request"]["task"]["name"]
        enqueue_per_yes_sync("sheriffs_office", "x@y.org", [])
        without = captured["request"]["task"]["name"]
        assert with_apostrophe != without

    def test_production_sanitises_the_task_name(self):
        """This mirror had NO production pin, which is how the bug shipped.

        Every test in this class exercises the local mirror above; nothing read
        backend/d4h.py, so production and mirror could diverge silently — and
        the Locked Decision for the per-YES sync cites this class as its pin.
        Same shape as _STAGING_TIER, gemini.py::type_labels and the whole of
        test_slack.py.
        """
        src = (Path(__file__).parent / "d4h.py").read_text(encoding="utf-8")
        fn = re.search(
            r"^def enqueue_per_yes_sync\(.*?(?=\n\n(?:def |# -{10,}|[A-Z_]+ = ))",
            src, re.DOTALL | re.MULTILINE,
        )
        assert fn, "enqueue_per_yes_sync not found in d4h.py"
        code = "\n".join(l.split("#")[0] for l in re.sub(
            r'""".*?"""', "", fn.group(0), flags=re.DOTALL).splitlines())
        assert "_sanitize_task_name(" in code, (
            "d4h.py builds the Cloud Tasks name without sanitising it — an "
            "apostrophe in a mutual-aid agency name fails the enqueue with "
            "InvalidArgument and NOTHING syncs to D4H attendance, silently."
        )

    def test_production_sanitiser_charset_matches_cloud_tasks(self):
        """The charset literal is the whole contract — pin it against drift."""
        src = (Path(__file__).parent / "d4h.py").read_text(encoding="utf-8")
        fn = re.search(
            r"^def _sanitize_task_name\(.*?(?=\n\n(?:def |# -{10,}|[A-Z_]+ = ))",
            src, re.DOTALL | re.MULTILINE,
        )
        assert fn, "_sanitize_task_name not found in d4h.py"
        code = "\n".join(l.split("#")[0] for l in re.sub(
            r'""".*?"""', "", fn.group(0), flags=re.DOTALL).splitlines())
        assert r'[^A-Za-z0-9_-]+' in code, (
            "The sanitiser charset drifted from Cloud Tasks' [A-Za-z0-9_-]."
        )
        assert '"-"' in code or "'-'" in code, (
            "The sanitiser no longer maps to '-'. Stripping instead would make "
            "\"sheriff's\" and \"sheriffs\" the same task name, so a second "
            "incident's sync would be silently deduped away."
        )

    def test_email_sanitized_for_task_name(self, monkeypatch):
        """@ and . in email must be replaced — Cloud Tasks task names are path segments."""
        captured = {}
        self._install_fake_tasks_v2(monkeypatch, captured)
        self._set_env(monkeypatch)
        enqueue_per_yes_sync("evt", "first.last@example.com", [])
        task_name = captured["request"]["task"]["name"].split("/tasks/")[-1]
        assert "@" not in task_name
        assert "." not in task_name
        assert task_name == "yes-evt-first-last-at-example-com"

    def test_eb_groups_empty_list_accepted(self, monkeypatch):
        captured = {}
        self._install_fake_tasks_v2(monkeypatch, captured)
        self._set_env(monkeypatch)
        enqueue_per_yes_sync("evt", "a@b.org", [])
        import json as _json
        payload = _json.loads(captured["request"]["task"]["http_request"]["body"])
        assert payload["eb_groups"] == []

    def test_missing_env_vars_raise_value_error(self, monkeypatch):
        captured = {}
        self._install_fake_tasks_v2(monkeypatch, captured)
        monkeypatch.setenv("GCP_PROJECT", "sar-dispatch-sccssar-dev")
        monkeypatch.delenv("CLOUD_RUN_SERVICE_URL", raising=False)
        monkeypatch.delenv("CLOUD_TASKS_SERVICE_ACCOUNT", raising=False)
        with pytest.raises(ValueError, match="env vars missing"):
            enqueue_per_yes_sync("evt", "a@b.org", [])


# --- Mirror ---
def _handle_per_yes_sync_task(
    event_id, member_email, eb_groups,
    *, client, load_activity_id,
):
    """Mirror of d4h.handle_per_yes_sync_task — takes `client` + `load_activity_id`
    kwargs. Production version omits both, calls module-level helpers."""
    activity_id = load_activity_id(event_id)
    if activity_id is None:
        return

    # PR-1 (2026-06-03): resolve K9 Handler role BEFORE attendance POST so the
    # Attendance tab Role column populates without dispatcher input. Best-effort.
    role_id = _resolve_k9_handler_role_id(member_email, eb_groups, client=client)

    _mark_member_attending(activity_id, member_email, client=client, role_id=role_id)
    # Selective mode (fullTeam: false): mark_member_attending no longer
    # returns "failed" — _post_attendance raises D4HServerError on 5xx,
    # which propagates here and surfaces to the Cloud Tasks worker for retry.

    # Post-2026-05-19 EB rebuild: `Canine` group (was `SAR - Canine Team`,
    # DOGS-* subgroups collapsed in). Drone-attach moved to dispatch-time
    # (create_incident_with_subject) per Bill — group-dependent, not pilot-
    # dependent. Not called from here anymore.
    if "Canine" in eb_groups:
        try:
            _sync_k9_attendance(activity_id, member_email, client=client)
        except D4HServerError:
            raise
        except Exception:  # noqa: BLE001 — non-retriable, mirrors production
            pass


class TestHandlePerYesSyncTask:
    def test_graceful_degrade_when_activity_id_missing(self):
        """No D4H calls when Firestore has no activity_id (D4H create failed at dispatch)."""
        client = FakeD4HClient()
        _handle_per_yes_sync_task(
            "evt-1", "bill@sccssar.org", ["UAS"],
            client=client, load_activity_id=lambda eid: None,
        )
        # Returns silently, no HTTP at all
        assert client.calls == []

    def test_mark_attending_only_non_uas_non_k9(self):
        """Standard responder (not UAS pilot, not K9 handler): just POST attendance."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "bill@sccssar.org", "verified": True}},
            attendance=[],  # Selective-mode baseline
        )
        _handle_per_yes_sync_task(
            "evt-1", "bill@sccssar.org", ["ATV"],
            client=client, load_activity_id=lambda eid: 99,
        )
        # mark_member_attending POSTed a new attendance record
        post_calls = [c for c in client.calls if c[0] == "_post_attendance"]
        assert post_calls and post_calls[0][1]["status"] == "ATTENDING"
        # No drone-add, no K9 sync attempt
        assert not any(c[0] == "_post_equipment_usage" for c in client.calls)

    def test_uas_responder_does_not_trigger_drone_add(self):
        """Post-2026-05-19 architecture: drone-attach lives in
        create_incident_with_subject (dispatch-time), NOT here. A UAS
        responder YES-replying mark-attendings normally but should NOT
        trigger an equipment-usage POST from this code path."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "bill@sccssar.org", "verified": True}},
            attendance=[],
            equipment={"id": 42, "ref": "Drone #6", "kind": {"title": "UAS"}},
        )
        _handle_per_yes_sync_task(
            "evt-1", "bill@sccssar.org", ["UAS"],
            client=client, load_activity_id=lambda eid: 99,
        )
        assert any(c[0] == "_post_attendance" for c in client.calls)
        # Crucially: no drone-attach happens here anymore — it ran at dispatch.
        assert not any(c[0] == "_post_equipment_usage" for c in client.calls)

    def test_k9_handler_notimplementederror_swallowed(self):
        """K9 handler YES-reply (post-rebuild `Canine` group): mark attending +
        try-catch NotImplementedError from sync_k9_attendance.
        Pre-rebuild this fired on `DOGS - *` names; collapsed into Canine."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "kris@sccssar.org", "verified": True}},
            attendance=[],
        )
        # Should NOT raise — v1.1 contract swallows the v1.2 NotImplementedError
        _handle_per_yes_sync_task(
            "evt-1", "kris@sccssar.org", ["Canine"],
            client=client, load_activity_id=lambda eid: 99,
        )
        assert any(c[0] == "_post_attendance" for c in client.calls)

    def test_k9_handler_with_registered_dogs_tags_role_in_attendance_post(self):
        """PR-1 (2026-06-03): K9 handler YES-reply with /handlers rows →
        roleId=K9_HANDLER_ROLE_ID appears in the attendance POST payload.

        Verifies the orchestrator wiring end-to-end: handle_per_yes_sync_task
        resolves the role (Canine + ≥1 handler) and forwards it to
        mark_member_attending, which appends it to the payload."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "kris@sccssar.org", "verified": True}},
            attendance=[],
            handlers=[
                {"id": 3614, "member": {"id": 555}, "animal": {"id": 2929}},  # Aria
                {"id": 3679, "member": {"id": 555}, "animal": {"id": 2954}},  # Annie
            ],
        )
        _handle_per_yes_sync_task(
            "evt-1", "kris@sccssar.org", ["Canine"],
            client=client, load_activity_id=lambda eid: 99,
        )
        post_call = [c for c in client.calls if c[0] == "_post_attendance"][0]
        assert post_call[1]["roleId"] == K9_HANDLER_ROLE_ID
        assert post_call[1]["roleId"] == 11487

    def test_canine_group_member_without_dogs_no_role_tag(self):
        """PR-1: Canine group member with NO /handlers rows (driver/flanker) →
        no roleId on attendance POST. Group membership alone is not the precision
        signal — only /handlers row existence is."""
        client = FakeD4HClient(
            member={"id": 777, "email": {"value": "driver@sccssar.org", "verified": True}},
            attendance=[],
            handlers=[],  # driver/flanker, not a registered handler
        )
        _handle_per_yes_sync_task(
            "evt-1", "driver@sccssar.org", ["Canine"],
            client=client, load_activity_id=lambda eid: 99,
        )
        post_call = [c for c in client.calls if c[0] == "_post_attendance"][0]
        assert "roleId" not in post_call[1], \
            "Canine group ≠ K9 Handler — driver/flanker must NOT get the role tag"

    def test_non_canine_responder_no_role_lookup_attempted(self):
        """PR-1: Non-K9 dispatch → no /handlers lookup. The Canine check is
        the cheap gate — protects the per-YES hot path for the majority case
        (most dispatches are not K9)."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "bill@sccssar.org", "verified": True}},
            attendance=[],
            handlers=[{"id": 1, "member": {"id": 555}, "animal": {"id": 2}}],  # would match if asked
        )
        _handle_per_yes_sync_task(
            "evt-1", "bill@sccssar.org", ["UAS"],  # NOT Canine
            client=client, load_activity_id=lambda eid: 99,
        )
        assert not any(c[0] == "_get_handlers_for_member" for c in client.calls), \
            "Non-K9 dispatch must NOT trigger /handlers lookup (hot-path latency)"

    def test_k9_role_lookup_failure_still_posts_attendance(self):
        """PR-1: If /handlers GET raises (D4H 5xx, network blip), role-lookup
        gracefully degrades to None and attendance POST still happens with no
        roleId. The role tag is best-effort — never block the attendance row
        creation on a role-tag failure. (Q2 of the 6-question Failure-mode
        Discipline rubric.)"""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "kris@sccssar.org", "verified": True}},
            attendance=[],
            get_handlers_raises=D4HServerError("D4H 503"),
        )
        # Role-lookup itself degrades silently. Since #756 the SAME /handlers
        # GET is also the first step of the K9 sync further down, where a 5xx
        # is retriable rather than cosmetic — so the task now propagates for
        # Cloud Tasks retry. The claim this test exists to make is unchanged
        # and asserted below: attendance is POSTed first, so it is never
        # blocked, and the retry re-drives it idempotently.
        with pytest.raises(D4HServerError):
            _handle_per_yes_sync_task(
                "evt-1", "kris@sccssar.org", ["Canine"],
                client=client, load_activity_id=lambda eid: 99,
            )
        post_call = [c for c in client.calls if c[0] == "_post_attendance"][0]
        assert post_call[1]["status"] == "ATTENDING"
        assert "roleId" not in post_call[1], \
            "Role-lookup failure must NOT block attendance — payload has no roleId"

    def test_k9_sync_4xx_is_swallowed_and_does_not_fail_the_task(self):
        """#756 Q5. A client error is not retriable — failing the task would
        burn the max_attempts=5 budget to no effect, and attendance has
        already landed."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "kris@sccssar.org", "verified": True}},
            attendance=[],
            handlers=[{"animal": {"id": 2865}}],
            animal_attendance=[],
            post_animal_attendance_raises=D4HClientError("D4H 400 bad payload"),
        )
        _handle_per_yes_sync_task(
            "evt-1", "kris@sccssar.org", ["Canine"],
            client=client, load_activity_id=lambda eid: 99,
        )
        assert [c for c in client.calls if c[0] == "_post_attendance"], \
            "attendance must still be recorded when K9 sync 4xxs"

    def test_k9_sync_5xx_propagates_for_retry(self):
        """#756 Q5. Transient — retry is cheap (both steps are idempotent) and
        swallowing it would permanently lose the row, handing the dispatcher
        back the close-out data entry this feature removes."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "kris@sccssar.org", "verified": True}},
            attendance=[],
            handlers=[{"animal": {"id": 2865}}],
            animal_attendance=[],
            post_animal_attendance_raises=D4HServerError("D4H 503"),
        )
        with pytest.raises(D4HServerError):
            _handle_per_yes_sync_task(
                "evt-1", "kris@sccssar.org", ["Canine"],
                client=client, load_activity_id=lambda eid: 99,
            )
        assert [c for c in client.calls if c[0] == "_post_attendance"], \
            "attendance is POSTed before K9 sync, so it is never blocked"

    def test_post_attendance_5xx_propagates_for_retry(self):
        """5xx from _post_attendance → D4HServerError propagates so the
        Cloud Tasks worker returns 502 and Cloud Tasks retries."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "bill@sccssar.org", "verified": True}},
            attendance=[],
            post_attendance_raises=D4HServerError("D4H down"),
        )
        with pytest.raises(D4HServerError, match="D4H down"):
            _handle_per_yes_sync_task(
                "evt-1", "bill@sccssar.org", [],
                client=client, load_activity_id=lambda eid: 99,
            )

    def test_member_not_found_does_not_raise(self):
        """If responder is not in D4H (member_not_found), no exception —
        graceful continuation, just no attendance row."""
        client = FakeD4HClient(member=None)  # not in D4H
        # Should NOT raise — graceful continuation
        _handle_per_yes_sync_task(
            "evt-1", "ghost@example.com", ["ATV"],
            client=client, load_activity_id=lambda eid: 99,
        )
        # No drone-attach path here regardless (moved to dispatch-time)
        assert not any(c[0] == "_post_equipment_usage" for c in client.calls)

    def test_idempotency_existing_attendance_skips_post(self):
        """Retry safety — existing ATTENDING record skips POST and continues
        with downstream steps (K9 sync; drone-attach moved to dispatch-time)."""
        client = FakeD4HClient(
            member={"id": 555, "email": {"value": "bill@sccssar.org", "verified": True}},
            attendance=[{"id": 1, "member": {"id": 555}, "status": "ATTENDING"}],
            equipment={"id": 42, "ref": "Drone #6", "kind": {"title": "UAS"}},
        )
        _handle_per_yes_sync_task(
            "evt-1", "bill@sccssar.org", ["UAS"],
            client=client, load_activity_id=lambda eid: 99,
        )
        # No POST attendance — already present
        assert not any(c[0] == "_post_attendance" for c in client.calls)
        # No drone-attach either — moved to dispatch-time
        assert not any(c[0] == "_post_equipment_usage" for c in client.calls)


# --------------------------------------------------------------------------
# exception_summary_no_body — mirror of d4h.exception_summary_no_body
# Strips the " | body: ..." suffix from D4H exception messages so log calls
# rendering str(exc) do not echo back response bodies (which can contain
# member emails / names per 4xx error envelopes). PR-A.6 of the 2026-05
# security review.
# --------------------------------------------------------------------------

def _exception_summary_no_body(exc: Exception) -> str:
    """Mirror of d4h.exception_summary_no_body — kept in sync with d4h.py."""
    msg = str(exc)
    sep = " | body: "
    if sep in msg:
        msg = msg.split(sep, 1)[0]
    return f"{type(exc).__name__}: {msg}".replace("\n", " ").strip()


class _FakeD4HClientErr(Exception):
    pass


class _FakeD4HServerErr(Exception):
    pass


class TestExceptionSummaryNoBody:
    """Pin: D4H exception rendering MUST strip the " | body: ..." suffix.

    Established by PR-A.6 of the 2026-05 security review after a real
    D4H involved-person 400 surfaced the body fragment in Cloud Logging
    via main.py:4919 logging str(exc) of a failed-operation message.
    """

    def test_strips_body_suffix_from_client_error(self):
        exc = _FakeD4HClientErr(
            "d4h._post_involved_person → HTTP 400: The request is malformed "
            "| body: {\"title\":\"Bad Request\",\"detail\":\"Member JOHN DOE already exists\"}"
        )
        result = _exception_summary_no_body(exc)
        assert "body:" not in result
        assert "JOHN DOE" not in result
        assert "HTTP 400" in result  # operational signal preserved
        assert "The request is malformed" in result

    def test_no_body_suffix_passes_through(self):
        """Non-D4H exceptions (ModuleNotFoundError, ValueError) have no body
        suffix; the helper should return them unchanged (with type prefix)."""
        exc = ModuleNotFoundError("No module named 'some_missing_module'")
        result = _exception_summary_no_body(exc)
        assert "ModuleNotFoundError" in result
        assert "some_missing_module" in result

    def test_newlines_stripped(self):
        exc = _FakeD4HServerErr("Line one\nLine two | body: secret\nstuff")
        result = _exception_summary_no_body(exc)
        assert "\n" not in result
        assert "secret" not in result

    def test_class_name_prefix_included(self):
        exc = _FakeD4HClientErr("test message")
        assert _exception_summary_no_body(exc).startswith("_FakeD4HClientErr: ")


# --------------------------------------------------------------------------
# Payload shape descriptor — mirror of d4h._payload_shape_for_log.
# Used by _post_involved_person to emit a PII-safe shape on 4xx responses.
# --------------------------------------------------------------------------

def _payload_shape_for_log(payload: dict) -> dict:
    """Mirror of d4h._payload_shape_for_log — keep in sync."""
    out: dict[str, str] = {}
    for k, v in payload.items():
        if v is None:
            out[k] = "<None>"
        elif isinstance(v, bool):
            out[k] = f"<bool:{v}>"
        elif isinstance(v, (int, float)):
            out[k] = f"<{type(v).__name__}:{v}>"
        elif isinstance(v, str):
            out[k] = f"<str:len={len(v)}>"
        elif isinstance(v, dict):
            out[k] = f"<dict:keys={len(v)}>"
        elif isinstance(v, list):
            out[k] = f"<list:len={len(v)}>"
        else:
            out[k] = f"<{type(v).__name__}>"
    return out


class TestPayloadShapeForLog:
    """Pin: shape descriptor emits non-PII output for all expected value types."""

    def test_string_value_logs_length_not_content(self):
        result = _payload_shape_for_log({"name": "DOE, JANE"})
        assert result == {"name": "<str:len=9>"}
        assert "DOE" not in str(result)
        assert "JANE" not in str(result)

    def test_int_value_logged_as_value(self):
        assert _payload_shape_for_log({"age": 16}) == {"age": "<int:16>"}

    def test_none_value_marked(self):
        assert _payload_shape_for_log({"areaKnowledge": None}) == {"areaKnowledge": "<None>"}

    def test_bool_value_logged(self):
        assert _payload_shape_for_log({"fullTeam": False}) == {"fullTeam": "<bool:False>"}

    def test_full_involved_person_shape_safe(self):
        payload = {
            "involvementTypeId": 1, "outcomeId": 1,
            "name": "DOE, JANE", "areaKnowledge": None,
            "cause": "INTENTIONAL_SELF",
            "contact": "OFFICER A. EXAMPLE; (555) 010-1234",
            "involvementNotes": "Q2 - On foot - Mode of travel...",
            "dateOfBirth": "2000-01-01", "age": 26, "sex": "OTHER",
            "incidentId": 1000000,
        }
        result_str = str(_payload_shape_for_log(payload))
        for pii_token in ("DOE", "JANE", "EXAMPLE", "010", "2000-01-01", "Mode of travel"):
            assert pii_token not in result_str, \
                f"PII token {pii_token!r} leaked into shape descriptor"


# --------------------------------------------------------------------------
# _post_involved_person null-stripping behavior
# --------------------------------------------------------------------------

def _strip_nulls_for_test(payload: dict) -> dict:
    """Mirror of the inline null-strip in d4h._post_involved_person."""
    return {k: v for k, v in payload.items() if v is not None}


class TestPostInvolvedPersonNullStripping:
    """Pin: explicit `None` values in the involved-person payload MUST be
    dropped before serialization (suspected cause of the 2026-05-19
    Hostetter D4H 400 — areaKnowledge=None when Q1 was unanswered)."""

    def test_areaknowledge_none_stripped(self):
        cleaned = _strip_nulls_for_test({"name": "X", "areaKnowledge": None, "age": 16})
        assert "areaKnowledge" not in cleaned
        assert cleaned == {"name": "X", "age": 16}

    def test_empty_string_preserved(self):
        cleaned = _strip_nulls_for_test({"name": "X", "contact": ""})
        assert cleaned["contact"] == ""

    def test_zero_preserved(self):
        cleaned = _strip_nulls_for_test({"age": 0, "areaKnowledge": None})
        assert cleaned == {"age": 0}


class TestInvolvedPersonLastSeen:
    """#755 — last-seen date/time on the D4H involved-person record.

    Kris (Ops, 2026-08-18) asked for both "when were we notified" and "when was
    the subject last seen" in D4H. The first already reaches the incident's long
    description via Event Log line 1; this is the second.

    involvementNotes is the destination because D4H has nowhere else to put it.
    The live involved-person schema was enumerated against team 1775 on
    2026-08-18 and exposes no last-seen equivalent — only createdAt/updatedAt —
    while the incident carries just startsAt/endsAt/createdAt. customFieldValues
    exists on both but was ruled out by Bill: no analytics capability reads it.
    incident.startsAt stays DISPATCH time and is not a substitute.
    """

    _BASE = {"mp_full_name": "Jane Doe"}

    def _notes(self, **kw):
        return _build_involved_person_payload({**self._BASE, **kw})["involvementNotes"]

    def test_last_seen_is_the_first_paragraph(self):
        """It is a plain fact about the person; everything below is analysis."""
        notes = self._notes(
            last_seen_at="2026-08-16 21:30",
            q2_question="Has phone", q2_answer="Yes",
            koester_narrative="Dementia, urban. 50% within 1.2 mi (1.9 km).",
        )
        assert notes.split("\n\n")[0] == "Last seen: 2026-08-16 21:30"

    def test_present_even_when_it_is_the_only_content(self):
        assert self._notes(last_seen_at="21:30") == "Last seen: 21:30"

    def test_time_only_value_renders_verbatim(self):
        """No date inferred — D4H records exactly what the officer wrote."""
        assert "Last seen: 04:45 AM" in self._notes(last_seen_at="04:45 AM")

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_absent_value_adds_no_paragraph(self, value):
        notes = self._notes(last_seen_at=value, q2_question="Has phone",
                            q2_answer="Yes")
        assert "Last seen" not in notes
        assert notes == "Q2 - Yes - Has phone"

    def test_missing_key_adds_no_paragraph(self):
        assert "Last seen" not in self._notes(q2_question="Has phone",
                                              q2_answer="Yes")

    def test_does_not_disturb_the_existing_paragraph_order(self):
        notes = self._notes(
            last_seen_at="2026-08-16 21:30",
            q2_question="Has phone", q2_answer="Yes",
            koester_narrative="Dementia, urban.",
            at_risk_indicators=["dementia", "alone"],
        ).split("\n\n")
        assert notes[1] == "Q2 - Yes - Has phone"
        assert notes[2] == "Dementia, urban."
        assert notes[3].startswith("At-risk indicators:")


class TestInvolvedPersonLastSeenProductionParity:
    """Ties the mirror above to backend/d4h.py — httpx is absent locally, so
    this file mirrors rather than imports, and a mirror alone tests nothing."""

    @staticmethod
    def _fn():
        src = (Path(__file__).parent / "d4h.py").read_text(encoding="utf-8")
        start = src.index("\ndef _build_involved_person_payload(")
        end = src.index("\ndef ", start + 1)
        body = re.sub(r'""".*?"""', "", src[start:end], flags=re.DOTALL)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    def test_production_appends_the_last_seen_paragraph(self):
        prod = self._fn()
        assert 'paragraphs.append(f"Last seen: {last_seen}")' in prod, (
            "the last-seen paragraph is gone from involvementNotes — D4H is the "
            "surface Kris actually asked for"
        )

    def test_production_reads_the_last_seen_at_key(self):
        assert 'last_seen = (ocr_data.get("last_seen_at") or "").strip()' in self._fn(), (
            "the ocr_data key changed; main.py's _build_ocr_data_for_d4h emits "
            "last_seen_at and a rename here fails silently"
        )

    def test_production_appends_it_before_the_questionnaire(self):
        """Position asserted by source order, since both appends are literals."""
        prod = self._fn()
        last_seen = prod.index('paragraphs.append(f"Last seen: {last_seen}")')
        qn = prod.index('paragraphs.append("\\n".join(qn_lines))')
        assert last_seen < qn, (
            "the last-seen paragraph no longer leads involvementNotes"
        )
