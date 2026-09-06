"""d4h.py — D4H Team Manager v3 API client + helpers.

Phase 2 surface area:
- Constants: BASE_URL, TEAM_ID, endpoint paths, EB↔D4H tag map (from Phase 1 spike 01b),
  enum constants (STATUS_*, OUTCOME_*, INVOLVEMENT_TYPE_*, AREA_KNOWLEDGE_*, CAUSE_*),
  _SUFFIX_PROJECTS (personal-dev gating, mirrors caltopo.py pattern).
- Pure-logic helpers: _format_d4h_datetime, _strip_date_for_d4h_title,
  _d4h_reference_description, _map_eb_groups_to_d4h_tags, payload builders,
  response parsers.
- Auth header: _auth_header() reading D4H_ACCESS_TOKEN env var (set lazily on first call).
- Thin httpx wrappers (added in PR 2): _post_incident, _post_tags, _post_involved_person,
  _get_attendance, _patch_attendance, _get_member_by_email, _get_equipment,
  _post_equipment_usage.
- High-level orchestrators (added in PR 3): create_incident_with_subject,
  mark_member_attending, add_drone_if_uas_dispatched, sync_k9_attendance,
  enqueue_per_yes_sync, handle_per_yes_sync_task.

Test architecture: Tests mirror constants + pure-logic helpers locally per
backend/test_everbridge.py convention. httpx is not installed in local pytest;
wrappers covered at live-test time on personal-dev.

Design doc: docs/plans/2026-05-13-d4h-phase2-backend-design.md
Phase 1 spike findings: experiments/d4h/notes/spike-validation-2026-05-11.md
"""
import html
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://api.team-manager.us.d4h.com/v3"
TEAM_ID = 1775  # SCCSSAR — confirmed in Phase 1 spike 00 (whoami)

# D4H Specialty Team tag IDs — from experiments/d4h/tag_ids.json (cached by spike 01).
# Pinned in test_main_regression.py::TestD4HTagIDsAreStable.
TAG_ATV              = 33098
TAG_CANINE           = 33095
TAG_CSST             = 34608  # Canine Specialized Search Team — CA volunteer org
TAG_LOGISTICS        = 33099
TAG_MEDICAL          = 33705
TAG_PARABOLIC        = 33100
TAG_TECHNICAL_RESCUE = 33102
TAG_TRACKING         = 33101
TAG_UAS              = 33103
TAG_USAR             = 33104
TAG_SEARCH_MANAGEMENT = 34206  # Always-set Support tag (every incident, per Bill 2026-05-11)
TAG_TRANSPORT        = 34207   # Canine-conditional Support tag

# EB↔D4H tag mapping — keys are EB group names AFTER prefix-strip (SAR -,
# ALERTSCC) + lowercase. Source-of-truth is `discover.py groups` output;
# current live group names verified 2026-05-19 post Kris's EB rebuild
# (which dropped the " Team" suffix and the "SAR - " prefix on most groups,
# and collapsed DOGS-* K9 sub-groups into the single `Canine` group).
# Pinned in test_main_regression.py::TestD4HEBGroupMappingIsStable.
_EB_TO_D4H_TAG: dict[str, int] = {
    "atv":               TAG_ATV,
    "canine":            TAG_CANINE,
    "search management": TAG_SEARCH_MANAGEMENT,  # also auto-added; mapping is idempotent
    "technical rescue":  TAG_TECHNICAL_RESCUE,
    "uas":               TAG_UAS,
}

# Groups intentionally NOT in the dispatchable mapping (admin, superset, test).
# These return as silently-skipped (not "unmapped") from _map_eb_groups_to_d4h_tags.
_EB_NON_DISPATCH_GROUPS: frozenset[str] = frozenset({
    "admin",           # ALERTSCC ADMIN
    "all members",     # All Members
    "automation test", # Automation Test
})

# Status enums — confirmed in Phase 1 spike 07 (POST /attendance schema).
STATUS_REQUESTED = "REQUESTED"
STATUS_ATTENDING = "ATTENDING"
STATUS_ABSENT    = "ABSENT"

# Role IDs — discovered via spike 16 (experiments/d4h/16_handlers_endpoint.py)
# GET /v3/team/{teamId}/roles enumeration, 2026-06-03. SCCSSAR-specific; teams
# customize role taxonomy. If a future role rename or re-bundle drifts the ID,
# re-run spike 16 and bump both this constant and the cross-file pin in
# backend/test_main_regression.py (_D4H_K9_HANDLER_ROLE_ID_LITERAL).
K9_HANDLER_ROLE_ID = 11487  # D4H role title "K9 Handler" (bundle: Search Roles)

# Outcome enums — confirmed in Phase 1 spike 06 (involved-person metadata endpoint).
OUTCOME_PERSON_ASSISTED   = 1  # v1 placeholder for active incidents
OUTCOME_NOT_LOCATED       = 2
OUTCOME_DECEASED          = 3
OUTCOME_LIFE_SAVED        = 4

# Involvement type enums — confirmed in Phase 1 spike 06.
INVOLVEMENT_TYPE_SUBJECT         = 1  # The Missing Person
INVOLVEMENT_TYPE_WITNESS         = 2
INVOLVEMENT_TYPE_OTHER           = 3
INVOLVEMENT_TYPE_SUSPECT         = 4
INVOLVEMENT_TYPE_REPORTING_PARTY = 5

# Subject record LPB-shaped field enums — confirmed in Phase 1 spike 06.
AREA_KNOWLEDGE_FAMILIAR   = "FAMILIAR"
AREA_KNOWLEDGE_UNFAMILIAR = "UNFAMILIAR"
CAUSE_NO_DATA            = "NO_DATA"
CAUSE_INTENTIONAL_SELF   = "INTENTIONAL_SELF"
CAUSE_INTENTIONAL_OTHER  = "INTENTIONAL_OTHER"
CAUSE_ACCIDENTAL         = "ACCIDENTAL"
CAUSE_UNDETERMINED       = "UNDETERMINED"

# Personal-dev environment gating — mirrors caltopo.py:_make_map_title() Locked Decision.
# SCCSSAR-dev and prod produce clean D4H referenceDescription titles.
# Pinned in test_main_regression.py::TestD4HReferenceDescriptionSuffixGating.
_SUFFIX_PROJECTS: frozenset[str] = frozenset({"sar-dispatch-dev"})

# Drone identity — confirmed in Phase 1 spike 09.
# Animal attendance (K9). Unlike member attendance there is NO status field on
# this endpoint — see sync_k9_attendance — so `duration` is the only dial, and
# the row's existence IS the attendance.
#
# 1 minute is the smallest value D4H accepts on an attendance record and is
# already the team's placeholder convention for members and for the incident's
# own duration (Bill 2026-08-18). Equipment usage tolerates duration=0 because
# equipment is a resource, not an attendee — do not copy that 0 here.
ANIMAL_ATTENDANCE_DURATION_MIN = 1
# D4H requires the activity as an OBJECT carrying its own discriminator, not
# the bare `activityId` int every other endpoint in this module takes. Enum is
# 'Event' | 'Exercise' | 'Incident' (quoted verbatim from the 400 in
# experiments/d4h/24_animal_attendance_shape.py); Dispatch Turbo only ever
# creates Incidents.
ACTIVITY_RESOURCE_TYPE_INCIDENT = "Incident"

DRONE_REF        = "Drone #6"      # UI shows "#Drone #6" with leading hash; API ref is bare
DRONE_KIND_TITLE = "UAS"

# ---------------------------------------------------------------------------
# Pure-logic helpers — no I/O
# ---------------------------------------------------------------------------

def _format_d4h_datetime(dt) -> str:
    """Format datetime as D4H-compatible ISO 8601 with milliseconds + Z suffix.

    D4H rejects Python's default `.isoformat()` output. Required: `.000Z` style.
    Naive datetimes raise ValueError.

    Test mirror: backend/test_d4h.py::TestFormatD4HDatetime
    """
    from datetime import timezone
    if dt.tzinfo is None:
        raise ValueError("D4H requires timezone-aware datetimes (use timezone.utc)")
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# Input DOB formats this module knows how to normalize. Mirrors main.py's
# _DOB_FORMATS list — keep them in lockstep (cross-file pin in
# test_main_regression.py guards against drift). D4H's involved-person POST
# requires ISO 8601 (YYYY-MM-DD) per spike 06:38; the textarea typically
# carries MM/DD/YYYY because SCCSSAR's intake form is US-format.
_DOB_INPUT_FORMATS = (
    "%m/%d/%Y",   # 10/20/2005
    "%m/%d/%y",   # 09/18/05 (2-digit year)
    "%m-%d-%Y",   # 6-26-2010 (hyphen separator — common in handwritten forms)
    "%m-%d-%y",   # 6-26-10
    "%Y-%m-%d",   # 2005-10-20 (already ISO — pass-through)
    "%B %d, %Y",  # October 20, 2005
    "%b %d, %Y",  # Oct 20, 2005
)


def _normalize_dob_to_iso8601(dob_text: str) -> Optional[str]:
    """Parse a DOB date string in any of _DOB_INPUT_FORMATS and emit ISO 8601.

    Returns None for empty/unparseable input. None lets _build_involved_person_payload
    pass through D4H's "dateOfBirth": None which D4H accepts as "no DOB on file"
    rather than triggering a malformed-string 400.

    2-digit-year disambiguation: %y defaults to the 1969-2068 cutover. For DOBs
    we always prefer the past — if the parsed year ends up in the future relative
    to today, subtract 100 (mirrors main.py::_compute_age_from_dob convention).

    Spike reference: experiments/d4h/06_involved_person.py:38 uses "1955-03-15"
    (ISO 8601). Pre-fix d4h.py passed through whatever ocr_data["mp_dob"] held
    verbatim — typically MM/DD/YYYY from the US-format intake form — which D4H
    rejected with HTTP 400. Audit fix 2026-05-16.
    """
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
    # Year-only comparison misses the same-year-future-month case: e.g.,
    # input "12/15/26" on 2026-05-25 → date(2026, 12, 15), then 2026>2026
    # is False → no century flip → future DOB silently accepted. Compare
    # full dates so any future-relative-to-today parse triggers the flip
    # for 2-digit years (or returns None for explicit 4-digit data entry).
    if parsed > today:
        if not used_2digit_year:
            return None  # 4-digit future year is a data-entry error; don't fabricate
        try:
            parsed = parsed.replace(year=parsed.year - 100)
        except ValueError:
            return None
    return parsed.isoformat()


def _normalize_sex_for_d4h(raw: str) -> str:
    """Normalize an OCR-extracted sex/gender value to D4H's enum.

    D4H's accepted enum (verified empirically 2026-05-16 from a live 400
    response body) is MALE / FEMALE / OTHER. Spike 06:40 INLINE COMMENT
    claimed "MALE / FEMALE / UNKNOWN" but that was the spike author's
    guess — the spike's actual POST sent "FEMALE" and never tested
    "UNKNOWN". D4H rejected "UNKNOWN" with HTTP 400 invalid_enum_value
    on the first live dispatch attempt — surfaced by the diagnostic-
    logging upgrade in the prior audit-fix PR.

    The intake form's Gender field is free-text; common variants:
    "M" / "MALE" / "F" / "FEMALE" / "" / "U" / "NB" / "X".

    Returns "OTHER" for anything that's not a clear MALE or FEMALE
    match — including empty / missing / None / non-binary expressions.

    Currently the textarea doesn't expose a Sex field at all (per Task 5.2
    finding), so this defaults all current dispatches to OTHER until the
    intake form / OCR exposes the field.

    Pattern lesson: spike COMMENTS are not source-of-truth — only the
    spike's actual POST body is.
    """
    if not raw:
        return "OTHER"
    v = str(raw).strip().upper()
    if v in ("M", "MALE"):
        return "MALE"
    if v in ("F", "FEMALE"):
        return "FEMALE"
    return "OTHER"


def _split_address_for_d4h(lkp_address: str) -> tuple[str, str]:
    """Split a comma-separated address into (street, town) for the D4H
    address sub-dict.

    Examples:
      "1000 Tradan Dr, San Jose, CA 95110" → ("1000 Tradan Dr", "San Jose")
      "Cardoza Park, Milpitas"             → ("Cardoza Park", "Milpitas")
      "1000 Tradan Dr"                     → ("1000 Tradan Dr", "")
      ""                                   → ("", "")

    The spike at experiments/d4h/03_create_full.py:122-127 paired
    {street, town, region, country} with a non-empty town ("San Jose").
    Pre-fix, _build_create_incident_payload stuffed the whole address
    string into street and left town empty — D4H accepts empty town in
    principle but the data is operationally useful when present.

    State / ZIP (3rd+ comma-separated tokens) are discarded — the region
    field is hardcoded "California" and D4H doesn't accept a ZIP field
    in this sub-dict per the spike. Audit fix 2026-05-16.
    """
    if not lkp_address:
        return "", ""
    parts = [p.strip() for p in lkp_address.split(",")]
    street = parts[0] if parts else ""
    town = parts[1] if len(parts) > 1 else ""
    return street, town


def _strip_date_for_d4h_title(event_name: str) -> str:
    """Strip a leading `YYYY-MM-DD ` from the event name.

    D4H stores its own startsAt; duplicating the date in referenceDescription is
    redundant per CLAUDE.md Event Name format Locked Decision.

    Test mirror: backend/test_d4h.py::TestStripDateForD4HTitle
    """
    return re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", event_name)


# D4H's Zod schema for POST /incidents caps referenceDescription at 100:
#   {"code": "too_big", "maximum": 100, "type": "string",
#    "path": ["body", "referenceDescription"]}
# Empirically confirmed 2026-08-01 against live team 1775 by
# experiments/d4h/22_reference_description_cap.py — undocumented, exactly like
# trackingNumber's max(50), and a 400 rejects the WHOLE incident create with
# Everbridge and Slack already fired.
#
# This is the BELT to #672's braces, and it is load-bearing rather than
# decorative: #672 caps the event name at RECONSTRUCTION time, but the Event
# Name textarea is dispatcher-editable and #87 wires that edited value straight
# through to D4H. A dispatcher who pastes a long name bypasses the #672 cap
# entirely; only this boundary check makes the 400 impossible.
# Source of truth for this literal; mirrored in test_d4h.py.
D4H_REFERENCE_DESCRIPTION_MAX = 100


def _d4h_reference_description(event_name: str, project_id: Optional[str]) -> str:
    """Build the D4H referenceDescription from an Event Name.

    Two transforms:
      1. Strip leading `YYYY-MM-DD ` (D4H has its own startsAt — duplicate is noise)
      2. Append 4-hex-char [abcd] suffix ONLY on personal-dev
         (mirrors caltopo.py:_make_map_title() Locked Decision — prevents test
         clutter on SCCSSAR-dev/prod incident lists)

    Test mirror: backend/test_d4h.py::TestD4HReferenceDescription
    """
    stripped = _strip_date_for_d4h_title(event_name)
    if project_id in _SUFFIX_PROJECTS:
        suffix = secrets.token_hex(2)
        room = D4H_REFERENCE_DESCRIPTION_MAX - len(f" [{suffix}]")
        return f"{stripped[:room].rstrip()} [{suffix}]"
    return stripped[:D4H_REFERENCE_DESCRIPTION_MAX].rstrip()


# D4H's Zod schema for POST /incidents caps trackingNumber at 50 characters:
#   {"code": "too_big", "maximum": 50, "type": "string",
#    "path": ["body", "trackingNumber"]}
# Empirically confirmed 2026-07-31 against live team 1775 by
# experiments/d4h/21_incident_400_isolation.py (spike 21) — the constraint is
# not documented, and a 400 rejects the WHOLE incident create.
# Source of truth for this literal; mirrored in test_d4h.py.
D4H_TRACKING_NUMBER_MAX = 50


def _tracking_number_for_d4h(event_number: str) -> str:
    """Render the requesting agency's incident number for D4H's trackingNumber.

    Returns "" when there is nothing to send, which the caller turns into an
    OMITTED key rather than an empty string — D4H's own field, D4H's default.

    trackingNumber is D4H's agency-reference field: it is what cross-references
    our record to the Sheriff's Office incident at post-incident reporting time,
    and it is what the original Phase 1 spike put there ("26-00193"). Until
    #676 it carried the event NAME, because nothing extracted the Event # the
    intake form has always had.

    The cap is defensive and belt-and-braces with #672's event-name cap. The
    length that matters is now an agency-supplied number (a dozen characters in
    practice), so tripping 50 should be impossible — but on 2026-07-31 a
    62-character value silently destroyed a whole dispatch's D4H record while
    Everbridge and Slack had already fired, and a truncated cross-reference
    beats no incident at all.
    """
    value = (event_number or "").strip()
    return value[:D4H_TRACKING_NUMBER_MAX]


# D4H's Zod schema for POST /incident-involved-persons requires age > 0:
#   {"code": "too_small", "minimum": 0, "inclusive": false,
#    "message": "Number must be greater than 0", "path": ["body", "age"]}
# Note `inclusive: false` — the constraint is age > 0, NOT age >= 0. ZERO IS
# REJECTED. Empirically confirmed 2026-09-06 against live team 1775 while
# probing the age constraint for #765; the probe created nothing. Undocumented,
# exactly like trackingNumber's max(50) and referenceDescription's max(100).
#
# A 400 here loses the WHOLE involved-person record — every subject field, not
# just the age — on an incident whose Everbridge and Slack legs have already
# fired. Same blast radius as the 2026-07-31 trackingNumber failure.
#
# Zero is reachable: a subject under one year old is a plausible SAR subject,
# and nothing upstream stops it. index.html's sanity guard is `age >= 0`, so 0
# passes; the coercion below accepted it because `0 not in (None, "")`; and
# _post_involved_person's cleanup strips None, NOT falsy values — deliberately,
# because "" and 0 are legitimate for other fields (pinned by
# TestStripNulls::test_zero_preserved). So the gate has to be HERE, at the
# point the field is built, or 0 reaches the wire.
#
# WE OMIT RATHER THAN SUBSTITUTE. Sending age 1 would fabricate a year of age
# on a missing-infant record; D4H simply cannot represent "less than one year
# old". The age remains visible to D4H users in involvementNotes and in the
# summary text, so omitting costs a structured field, not the information.
#
# This is also the BELT to #765's braces: #765 removed the 2-digit-year pivot
# that produced NEGATIVE ages, but mp_age is dispatcher-editable and the
# frontend is not the only producer, so only a boundary check makes the 400
# impossible. `<= 0` covers both the zero and the negative case.
# Source of truth for this literal; mirrored in test_d4h.py.
D4H_AGE_MIN_EXCLUSIVE = 0


def _age_for_d4h(age_val: object) -> Optional[int]:
    """Coerce an intake age to D4H's `age`, or None to omit the field.

    Returns None — which _post_involved_person's None-strip turns into an
    OMITTED key — for anything D4H's schema would reject: a missing or blank
    value, a non-numeric one, and any value at or below
    D4H_AGE_MIN_EXCLUSIVE. See the constraint block above for why omitting
    beats substituting.

    Test mirror: backend/test_d4h.py::TestAgeForD4H
    """
    try:
        age = int(age_val) if age_val not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if age is not None and age <= D4H_AGE_MIN_EXCLUSIVE:
        return None
    return age


def _strip_eb_prefix(eb_group_name: str) -> str:
    """Strip common SCCSSAR EB prefixes for matching against D4H tag titles.

    Only `ALERTSCC ` survives in live EB as of 2026-05-19 (for `ALERTSCC ADMIN`).
    `SAR - ` and `DOGS - ` were dropped in Kris's rebuild — neither matches
    any current group, so they're not in the tuple.

    Test mirror: backend/test_d4h.py
    """
    for prefix in ("ALERTSCC ",):
        if eb_group_name.startswith(prefix):
            return eb_group_name[len(prefix):]
    return eb_group_name


def _map_eb_groups_to_d4h_tags(eb_group_names: list[str]) -> tuple[list[int], list[str]]:
    """Map EB group names to D4H Specialty Team tag IDs.

    Returns (mapped_tag_ids, unmapped_group_names). Caller emits event-log
    entries for unmapped names (dispatcher verifies D4H tags manually post-incident).

    Always includes TAG_SEARCH_MANAGEMENT (every incident, per Bill 2026-05-11).
    Adds TAG_TRANSPORT when any DOGS-* group is dispatched.

    Tag mapping derived from experiments/d4h/01b_tag_mapping_audit.py findings.
    Test mirror: backend/test_d4h.py::TestMapEBGroupsToD4HTags
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
        - event_number (str, optional): the REQUESTING AGENCY's own incident
          number off the intake form ("26-212-071"). Becomes trackingNumber;
          the key is omitted entirely when absent. NOT our event name — see
          _tracking_number_for_d4h and issue #676.
        - lkp_lat (float), lkp_lng (float): geocoded LKP coords
        - lkp_address (str): free-form address (Pass 1 OCR "Last Known Position:" line)
      dispatch_dt (datetime): timezone-aware dispatch timestamp (NOT in ocr_data —
        it's a runtime stamp, not OCR output)
      dispatcher_metadata:
        - dispatcher_name (str), dispatcher_email (str)
      project_id (str | None): GCP project ID for personal-dev suffix gating

    Weather is intentionally omitted — D4H auto-derives from location at create
    time; PATCHing the incident body afterward wipes auto-weather (Phase 1
    spike-validation finding, see experiments/d4h/notes/spike-validation-2026-05-11.md).

    Test mirror: backend/test_d4h.py::TestBuildCreateIncidentPayload
    Spike reference: experiments/d4h/03_create_full.py (gitignored).
    Field shape verified 2026-05-15 by PR 5 author against the spike.
    """
    event_name = ocr_data["event_name"]
    ref_desc = _d4h_reference_description(event_name, project_id)
    starts_at = _format_d4h_datetime(dispatch_dt)

    lkp_address = ocr_data.get("lkp_address", "") or ""

    # Address sub-dict — keys verified 2026-05-15 against
    # experiments/d4h/03_create_full.py:122-127 (live spike against D4H v3 SCCSSAR 1775).
    # 2026-05-16 audit: town now parsed from the lkp_address comma-separated form
    # (e.g., "1000 Tradan Dr, San Jose, CA 95110" → street + town pair).
    street, town = _split_address_for_d4h(lkp_address)
    address = {
        "street":  street,
        "town":    town,
        "region":  "California",
        "country": "United States",
    }

    # Location sub-dict — keys verified 2026-05-16 against
    # experiments/d4h/03_create_full.py:128-131 (live spike against D4H v3 SCCSSAR 1775).
    # NOT {lat, lng} — D4H rejects those with HTTP 400 (live smoke test 2026-05-16).
    # Coordinate access uses .get() + None check so a geocoding failure
    # surfaces as D4HClientError (categorized + handled by callers) rather
    # than a bare KeyError/ValueError that bypasses the typed-exception
    # routing in create_incident_with_subject.
    lkp_lat_raw = ocr_data.get("lkp_lat")
    lkp_lng_raw = ocr_data.get("lkp_lng")
    if lkp_lat_raw in (None, "") or lkp_lng_raw in (None, ""):
        raise D4HClientError(
            "Cannot create D4H incident: LKP coordinates missing "
            "(geocoding failed or not attempted). Both lkp_lat and lkp_lng "
            "must be present and non-empty in ocr_data."
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

    # Description layout per dispatcher design 2026-05-20:
    #   1. Dispatcher-TODO bullet list (5 review items)
    #   2. Reminder to delete the TODO block before saving
    #   3. Blank line
    #   4. Canonical IIS body starting with "Event Name:" (provided by
    #      main.py._extract_iis_body_for_d4h — WhatsApp section + section
    #      markers + "Initial Incident Summary:" header already stripped)
    #
    # This shape lets the dispatcher select-and-delete the TODO block (steps
    # 1-3) in one motion and have the description start cleanly with our
    # standard "Event Name:" line.
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

    # Append canonical IIS body. HTML-escape first, then convert newlines
    # to <br> so the structure (Q# alignment, --- dividers, etc.) renders
    # intact in D4H's HTML viewer.
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
        # Selective attendance mode — D4H starts attendance EMPTY; per-YES POST-new
        # records ATTENDING rows as responders accept. Omitting the field defaults
        # to True server-side, which triggers async auto-staging of REQUESTED
        # records for all 50+ team members and the cascade of failure modes
        # documented in the prior bulk-ABSENT path (PRs #430-#434, reverted).
        # See CLAUDE.md Locked Decision "Selective attendance mode (fullTeam: false)".
        "fullTeam":             False,
    }

    # trackingNumber is D4H's agency-reference field — the requesting agency's
    # own incident number, NOT our event name (issue #676). Omitted rather than
    # sent empty when the form carried no Event #: it is D4H's field and D4H's
    # default. The event name is already in referenceDescription above, so
    # omitting loses nothing.
    tracking_number = _tracking_number_for_d4h(ocr_data.get("event_number", ""))
    if tracking_number:
        payload["trackingNumber"] = tracking_number

    return payload


def _build_involved_person_payload(ocr_data: dict) -> dict:
    """Build the POST /incident-involved-persons body for the Subject record.

    Pure-logic — no I/O. LPB-shaped: maps Q1 → areaKnowledge, Q9 → cause,
    bundles last-seen + Q2-Q12 (skipping Q1+Q9) + Koester narrative + at-risk
    indicators into the involvementNotes catch-all (\\n\\n-separated paragraphs).

    HTML-escape policy: D4H stores involvementNotes + contact as plain text
    (NOT HTML — only the incident.description field is HTML-context). So no
    html.escape is applied here. This is the inverse of
    _build_create_incident_payload's description field, which IS escaped
    because it lives in an HTML-rendered surface.

    Input contract (selected keys from ocr_data):
      - mp_full_name (str): the missing person's full name → "name" field.
        REQUIRED by D4H — empty value will cause HTTP 400 on POST.
      - q1_familiar_with_area (str): "YES" / "NO" / "NOT ANSWERED" / missing
      - q9_intentional_self_harm (str): "YES" maps to CAUSE_INTENTIONAL_SELF; else NO_DATA
      - qN_question / qN_answer for N in 1..12: text pairs (skip-on-empty)
      - last_seen_at (str): officer's last-seen date/time, verbatim (skip if empty)
      - koester_narrative (str): already-formatted paragraph (skip if empty)
      - at_risk_indicators (list[str]): one-liners (skip whole block if empty)
      - officer_name (str), officer_phone (str): joined with "; " into contact
      - mp_dob (str), mp_age (int|str), mp_sex (str): demographic fields

    NO "incidentId" key — the orchestrator (create_incident_with_subject)
    sets it after _post_incident returns the activity_id. Per spike 06:156
    the field name is "incidentId", not "activityId".

    Test mirror: backend/test_d4h.py::TestBuildInvolvedPersonPayload
    Spike reference: experiments/d4h/06_involved_person.py (gitignored).
    All fields verified 2026-05-16 against the live spike's payload at
    lines 155-189; see audit findings on those commits for details.
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

    # involvementNotes catch-all — last-seen + Q2..Q12 (skip Q1+Q9 already
    # mapped) + koester_narrative + at-risk indicators. \n\n between paragraphs.
    paragraphs: list[str] = []

    # Issue #755 (Kris/Ops, 2026-08-18) — when the SUBJECT was last seen. FIRST
    # paragraph: it is a plain fact about the person this record describes,
    # whereas everything below it is questionnaire output and analysis.
    #
    # involvementNotes rather than a native field or a custom field: the live
    # involved-person schema exposes no last-seen equivalent (verified against
    # team 1775, 2026-08-18), and customFieldValues was ruled out by Bill — we
    # have no analytics capability that would read it. Incident.startsAt stays
    # DISPATCH time and is not the place for this.
    #
    # Rendered verbatim. main._subject_last_seen_value has already dropped the
    # "[not recorded]" sentinel, so a value arriving here is one the officer
    # actually wrote — including a bare time with no date, which is passed
    # through rather than completed by inference.
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

    # Demographics. _age_for_d4h coerces AND enforces D4H's undocumented
    # age > 0 Zod constraint — see the block above its definition. A subject
    # under one year old yields None here and the key is omitted, rather than
    # 400ing the POST and losing every other subject field with it.
    age = _age_for_d4h(ocr_data.get("mp_age"))

    # Pre-validate the required name field. _post_involved_person's None-strip
    # filter at line ~788 does NOT strip empty strings, so an OCR failure
    # (illegible handwriting, layout mismatch) that leaves mp_full_name=""
    # would produce {"name": ""} → D4H 400 with no clear field signal in
    # the response body. Raise here so the caller (create_incident_with_subject)
    # captures a specific "Missing Person name required" failure in
    # post_create_failures instead of a generic "request is malformed".
    mp_full_name = (ocr_data.get("mp_full_name") or "").strip()
    if not mp_full_name:
        raise D4HClientError(
            "Cannot attach Subject involved-person to D4H incident: "
            "mp_full_name missing or empty (OCR likely failed to extract). "
            "D4H requires a non-empty name on incident-involved-persons."
        )

    return {
        "involvementTypeId": INVOLVEMENT_TYPE_SUBJECT,
        "outcomeId":         OUTCOME_PERSON_ASSISTED,
        "name":              mp_full_name,
        "areaKnowledge":     area_knowledge,
        "cause":             cause,
        "contact":           contact,
        "involvementNotes":  involvement_notes,
        # DOB normalized to ISO 8601 per spike 06:38. Pre-fix passed through
        # MM/DD/YYYY from US-format intake forms which D4H rejected.
        "dateOfBirth":       _normalize_dob_to_iso8601(ocr_data.get("mp_dob", "")),
        "age":               age,
        # Sex normalized to D4H enum (MALE/FEMALE/UNKNOWN) per spike 06:40.
        # Pre-fix passed through raw OCR value (often empty) which D4H rejected.
        "sex":               _normalize_sex_for_d4h(ocr_data.get("mp_sex", "")),
    }


# ---------------------------------------------------------------------------
# Auth + HTTP helpers
# ---------------------------------------------------------------------------

def _auth_header() -> dict:
    """Return Bearer auth header from D4H_ACCESS_TOKEN env var.

    Lazy — read on first call, not at module import. Raises with a clear
    message if missing so deploy-time misconfiguration surfaces early.

    Mirrors backend/everbridge.py::_auth_header() pattern.
    """
    token = os.environ.get("D4H_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "D4H_ACCESS_TOKEN not set. In Cloud Run sourced from GCP Secret "
            "Manager 'd4h-access-token' via Terraform secret_key_ref. See GH issue #419."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


class D4HClientError(Exception):
    """4xx response — caller should log + continue, not retry."""


class D4HServerError(Exception):
    """5xx response or transport error — should trigger Cloud Tasks retry."""


class D4HRateLimitError(D4HServerError):
    """HTTP 429 — D4H rate-limited; transient, should trigger Cloud Tasks retry.

    Subclasses ``D4HServerError`` (NOT ``D4HClientError``) so the existing
    ``isinstance(exc, D4HClientError)`` check in ``handle_per_yes_sync_task``
    (main.py — Cluster D from batch-2 PR #518) naturally routes 429 to the
    502-retry branch instead of the 200-terminal branch. Pre-existing
    routing pinned ALL 4xx as non-retriable; 429 is the documented
    exception (transient throttle, not a malformed request). A 429 burst
    during a high-YES-volume call-out would otherwise silently drop per-YES
    attendance records — Cloud Tasks would see the task return 200 and
    never retry. See CLAUDE.md Failure-mode Discipline Q5 ("Is the failure
    mode typed?") and batch-3 finding PR-G.1.
    """


def _payload_shape_for_log(payload: dict) -> dict:
    """Return a PII-safe shape descriptor of a D4H request payload.

    Per-key value renderings:
      - str: ``"<str:len=N>"`` — length only, no content
      - int / float / bool: ``"<int:5>"`` — value (operational, non-PII)
      - None: ``"<None>"``
      - dict / list: ``"<dict:keys=N>"`` / ``"<list:len=N>"`` — count only
      - other: ``"<TypeName>"``

    Used by ``_post_involved_person`` to emit a payload shape log line
    on D4H 4xx responses, since D4H's generic "malformed request" body
    doesn't surface the failing field. The shape log lets us narrow
    down "which field tripped validation" without ever logging the
    field's actual value.
    """
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


# Zod issue keys that describe the SCHEMA, never the request value.
#
# D4H validates request bodies with Zod and returns the whole ZodError under
# detailObj.data.issues[] on 400. Each entry names the failing field path, the
# violated rule, and the rule's parameter:
#
#   {"code": "too_big", "maximum": 50, "type": "string",
#    "message": "String must contain at most 50 character(s)",
#    "path": ["body", "trackingNumber"]}
#
# That is exactly what diagnosing a 4xx needs, and it is PII-free by
# construction: it describes the contract, not the payload.
#
# "message" is deliberately EXCLUDED. It is value-free for too_big but Zod
# interpolates the received value into other message templates
# (invalid_enum_value, invalid_literal), so the key is not safe as a class.
# "received" is included ONLY for code == "invalid_type", where Zod sets it to
# a type name ("undefined", "string", "null") rather than the value — that
# distinguishes "field missing" from "field wrong type", which is the single
# most useful thing a 400 can tell us. For every other code it can be the
# request value itself.
_ZOD_SAFE_ISSUE_KEYS: tuple[str, ...] = (
    "code", "type", "expected", "validation",
    "maximum", "minimum", "inclusive", "exact",
)

# Bound the log line — a payload that violates 40 rules is a shape problem the
# first few issues already describe.
_ZOD_MAX_ISSUES_LOGGED = 10


def _zod_issues_for_log(body: object) -> list[dict]:
    """Extract PII-free field-level validation detail from a D4H 4xx body.

    Returns one dict per Zod issue, carrying the dotted field path plus the
    whitelisted schema-side keys (see _ZOD_SAFE_ISSUE_KEYS). Returns [] for
    any body that is not a Zod validation error, including bodies from
    non-D4H-v3 error classes.

    TOTAL FUNCTION — never raises. The only caller is a best-effort log line
    sitting between a failed request and the typed exception it still has to
    raise; an unexpected body shape must not become the thing that breaks
    error handling (Failure-mode Discipline Q2).

    Issue #671. Live case: a 2026-07-31 dispatch lost its D4H incident to a
    trackingNumber max(50) violation and no log line could name the field,
    even though D4H had said so in the response.
    """
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


def exception_summary_no_body(exc: Exception) -> str:
    """Render a D4H exception for logs without the ' | body: ...' suffix.

    D4HClientError and D4HServerError messages are constructed with a
    ``" | body: <fragment>"`` suffix so callers that catch the exception
    can read the response body for debugging. That body can echo back
    member emails or names from the request that D4H 4xx'd on, which
    leaks PII into Cloud Logging if any caller renders ``str(exc)``.

    This helper extracts the part of the message BEFORE the body suffix
    so all log lines can use a single consistent rendering. The body
    fragment remains on the exception object's args for any caller that
    explicitly needs it.

    Established by PR-A.6 of the 2026-05 security review after a live
    D4H involved-person 400 surfaced the leak through main.py:4919.
    """
    msg = str(exc)
    sep = " | body: "
    if sep in msg:
        msg = msg.split(sep, 1)[0]
    return f"{type(exc).__name__}: {msg}".replace("\n", " ").strip()


# Idempotent error codes — entries added empirically once we observe real D4H
# 4xx codes representing "desired end state already reached".
# Mirrors slack.py's _SWALLOWED_INVITE_ERRORS pattern.
_SWALLOWED_ERRORS: frozenset[str] = frozenset()


def _log_and_raise_for_status(resp: "httpx.Response", op_label: str) -> None:
    """Log + raise typed exception based on response status.

    - 2xx: return (caller proceeds)
    - 4xx in _SWALLOWED_ERRORS: log info, return (idempotent failure swallowed)
    - 4xx (other): log warning + raise D4HClientError (FULL body fragment surfaced)
    - 5xx response: log error + raise D4HServerError (body fragment surfaced)
    (Network errors / timeouts are handled by _safe_http_call before reaching this function.)

    Diagnostic policy on 4xx — UPGRADED 2026-05-16 after the PR 5 smoke test 400:
      The full sanitized response body (800-char cap) is included BOTH in the
      log line AND in the raised exception message. D4H 400 responses usually
      have a top-level "detail" string ("The request is malformed") plus an
      "errors" array with field-level rejection details. Before this upgrade,
      only "detail" was logged — losing the field-level info that would have
      pinpointed the location-keys bug directly. Now both surface, so the
      NEXT 400 (whether from one of these audit fixes or unknown drift) gives
      field-level diagnostic info from the start.

    Diagnostic policy on 4xx — AMENDED 2026-08-01 (issue #671) after the
    2026-07-31 dispatch that lost its D4H incident to an undiagnosable 400:
      The 2026-05-16 upgrade above put the body on the exception so the next
      400 would name its field. It never did, because a later PII fix
      (main.py::_sanitize_d4h_error → exception_summary_no_body) strips the
      " | body: " suffix before any surface renders it. Both decisions are
      correct in isolation; together they destroy the diagnostic. The fix is
      not to relax either one — it is to log the value-free SUBSET directly:
      _zod_issues_for_log extracts D4H's Zod issue array (field path +
      violated rule + rule parameter), which is schema metadata and carries
      no request values. It lives here rather than in any one _post_* wrapper
      so every D4H call inherits it.

    Body fragments capped at 800 chars, query params stripped from logged URLs (no PII).
    Path-segment IDs (activity_id, attendance_id, member_id, etc.) are D4H-
    internal numeric IDs that aid log triage and are NOT direct PII —
    intentionally retained.
    """
    if resp.is_success:
        return

    body_fragment = (resp.text or "")[:800]
    error_code = ""
    parsed_body: object = None
    try:
        parsed_body = resp.json()
        error_code = parsed_body.get("detail") or parsed_body.get("error_code") or ""
    except Exception as e:
        logger.debug("%s: response body not JSON-parseable (%s), using raw text", op_label, e)

    # Strip query params (may include emails or other PII via filter params).
    log_url = str(resp.url).split("?")[0]

    # 429 ahead of the generic 4xx branch: rate-limit is transient (retry),
    # not a malformed-payload signal (drop). Without this carve-out, the
    # Cluster-D handler routing (isinstance D4HClientError → return 200)
    # would silently lose per-YES attendance records on a Cloud Tasks dead
    # end. D4HRateLimitError inherits from D4HServerError so the isinstance
    # check fails and the handler falls through to its 502-retry branch.
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "")
        logger.warning(
            "%s: D4H 429 rate limited url=%s retry_after=%s",
            op_label, log_url, retry_after or "(none)",
        )
        raise D4HRateLimitError(
            f"{op_label} → HTTP 429: rate limited "
            f"(retry_after={retry_after or 'none'})"
        )

    if 400 <= resp.status_code < 500:
        if error_code in _SWALLOWED_ERRORS:
            logger.info("%s: idempotent failure swallowed (HTTP %s, %s)",
                        op_label, resp.status_code, error_code)
            return
        # NOTE: do not log body_fragment — D4H 4xx responses can echo back
        # request fields (member emails, names) per the error envelope.
        # The fragment still travels via the exception message below for
        # callers that need it; the project's exception handlers log only
        # type(exc).__name__.
        logger.warning("%s: D4H 4xx HTTP %s url=%s",
                       op_label, resp.status_code, log_url)
        # Field-level diagnostic — logged where the body deliberately is not.
        # The body can echo request values, so it travels only on the
        # exception; but main.py::_sanitize_d4h_error strips the " | body: "
        # suffix before the exception reaches any surface. The two guards are
        # each correct and compose into a blind spot: the field-level detail
        # is constructed, carried, and destroyed without ever being read.
        # The Zod issues are the value-free subset of that detail, so they can
        # be logged directly and every D4H call inherits it from here. #671.
        zod_issues = _zod_issues_for_log(parsed_body)
        if zod_issues:
            logger.warning("%s: D4H 4xx validation issues: %s", op_label, zod_issues)
        # Include error_code (parsed top-level "detail") AND body_fragment in
        # the exception message — error_code is the human-readable summary,
        # body_fragment is the diagnostic gold (field-level errors live here).
        # Format: "<op_label> → HTTP <status>: <summary> | body: <fragment>"
        # Backward-compat: prefix "→ HTTP <status>:" preserved so callers
        # grepping for that pattern still match.
        summary = error_code or "(no top-level detail)"
        raise D4HClientError(
            f"{op_label} → HTTP {resp.status_code}: {summary} | body: {body_fragment}"
        )

    # NOTE: do not log body_fragment — D4H 5xx responses can echo back
    # request fields. Fragment still travels via the exception below.
    logger.error("%s: D4H 5xx HTTP %s url=%s",
                 op_label, resp.status_code, log_url)
    raise D4HServerError(f"{op_label} → HTTP {resp.status_code}: {body_fragment[:200]}")


def _safe_http_call(method, url: str, op_label: str, **kwargs) -> "httpx.Response":
    """Execute an httpx call; convert RequestError into D4HServerError.

    httpx.RequestError covers connection errors, timeouts, DNS failures —
    anything preventing a response. Pairs with _log_and_raise_for_status
    (handles 4xx/5xx response cases) so every D4H failure becomes a typed
    exception that Cloud Tasks can route correctly.

    No-PII discipline: only the URL (no query params at this layer — params
    are passed via kwargs) and the exception class name are logged. Aikido
    PR #424 review finding addressed here.
    """
    try:
        return method(url, **kwargs)
    except httpx.RequestError as e:
        logger.warning("%s: D4H transport error url=%s exc=%r", op_label, url, e)
        raise D4HServerError(f"{op_label} → transport error: {type(e).__name__}") from e


def _extract_records(body: dict) -> list:
    """Return the records list from a D4H paginated response.

    Uses key-existence check (not truthiness) so an empty ``results`` list
    is treated as the legitimate "no results" answer, not as a falsy
    trigger to fall through to ``data``. Pre-fix (Cluster D), a schema
    change that added both keys with ``results=[]`` and
    ``data=[<unrelated records>]`` would silently iterate the wrong list.

    The default ``[]`` shields the caller from a response missing both
    keys — defensive, but unlikely against D4H v3 in practice.
    """
    return body.get("results", body.get("data", []))


# ---------------------------------------------------------------------------
# HTTP wrappers — sync httpx, body-build/HTTP/parse layers separated
# ---------------------------------------------------------------------------

def _post_incident(payload: dict) -> dict:
    """POST a new D4H incident. Returns the created record dict.

    Spike reference: experiments/d4h/03_create_full.py
    """
    op_label = "d4h._post_incident"
    url = f"{BASE_URL}/team/{TEAM_ID}/incidents"
    resp = _safe_http_call(httpx.post, url, op_label, headers=_auth_header(), json=payload, timeout=30.0)
    _log_and_raise_for_status(resp, op_label)
    return resp.json()


def _post_tags(activity_id: int, tag_ids: list[int]) -> dict:
    """POST tags onto an existing D4H incident. Returns the API response.

    Payload key is `tagIds` (camelCase) per spike 05_set_tags.py — D4H's
    actual API contract, NOT `tag_ids` as the plan template suggested.

    Spike reference: experiments/d4h/05_set_tags.py
    """
    op_label = "d4h._post_tags"
    url = f"{BASE_URL}/team/{TEAM_ID}/incidents/{activity_id}/tags"
    resp = _safe_http_call(httpx.post, url, op_label, headers=_auth_header(), json={"tagIds": tag_ids}, timeout=30.0)
    _log_and_raise_for_status(resp, op_label)
    return resp.json()


def _post_involved_person(payload: dict) -> dict:
    """POST an involved-person record (Subject/Witness/etc.) to an incident.

    Defensive payload hygiene: keys whose value is ``None`` are omitted
    before serialization. D4H has historically 400'd "The request is
    malformed" without field detail when optional fields are sent as
    explicit ``null`` rather than omitted (suspected — surfaced by the
    2026-05-19 Hostetter callout where two dispatchers across both form
    versions hit a reproducible involved-person 400). Omitting the key
    entirely lets D4H apply its server-side default.

    Diagnostic logging: on any 4xx response from this endpoint we emit a
    PII-safe "payload shape" log line (field name → type + length only,
    no values) so the next failure surfaces actual field-level metadata
    for diagnosis. D4H's 4xx envelope is generic ``{"detail":"The request
    is malformed"}`` with no field info, so the shape log is the only
    way to narrow down which field tripped validation.

    Spike reference: experiments/d4h/06_involved_person.py
    """
    op_label = "d4h._post_involved_person"
    url = f"{BASE_URL}/team/{TEAM_ID}/incident-involved-persons"

    # Defensive: strip None values. See docstring above.
    cleaned_payload = {k: v for k, v in payload.items() if v is not None}

    resp = _safe_http_call(httpx.post, url, op_label, headers=_auth_header(),
                           json=cleaned_payload, timeout=30.0)

    # Diagnostic: on 4xx, log PII-safe payload shape before letting
    # _log_and_raise_for_status do its thing. Strings → length only;
    # ints / bools / floats → value (non-PII). Other types → typename.
    if 400 <= resp.status_code < 500:
        shape = _payload_shape_for_log(cleaned_payload)
        logger.warning("%s 4xx payload shape: %s", op_label, shape)

    _log_and_raise_for_status(resp, op_label)
    return resp.json()


def _get_attendance(activity_id: int) -> list[dict]:
    """GET all attendance records for an incident.

    Pagination: pages are 0-indexed; server caps size at ~148 even when 500
    requested; use totalSize (not len < size) to detect end-of-data.

    Query param is snake_case `activity_id` (NOT camelCase `activityId`) per
    spike 07 — D4H is inconsistent across endpoints. Spike reference:
    experiments/d4h/07_attendance.py::get_attendance_list.
    """
    op_label = "d4h._get_attendance"
    url = f"{BASE_URL}/team/{TEAM_ID}/attendance"
    all_records: list[dict] = []
    page = 0
    seen_total = None
    while True:
        resp = _safe_http_call(httpx.get, url, op_label,
                               headers=_auth_header(),
                               params={"activity_id": activity_id, "page": page, "size": 500},
                               timeout=30.0)
        _log_and_raise_for_status(resp, op_label)
        body = resp.json()
        items = _extract_records(body)
        if seen_total is None:
            seen_total = body.get("totalSize")
        all_records.extend(items)
        if not items or (seen_total is not None and len(all_records) >= seen_total):
            break
        page += 1
    return all_records


def _post_attendance(payload: dict) -> dict:
    """POST a new attendance record. Returns the created record dict.

    Used by Selective-mode per-YES sync (mark_member_attending). Attendance
    starts empty under `fullTeam: false`; each YES-reply triggers one POST
    here to record the responder as ATTENDING.

    Payload shape (verified via experiments/d4h/15_full_team_false_blank_attendance.py
    spike against live SCCSSAR D4H 2026-05-19):
        {
            "activityId": <int>,
            "memberId":   <int>,
            "status":     "ATTENDING",
            "startsAt":   "YYYY-MM-DDTHH:MM:SS.000Z",
            "endsAt":     "YYYY-MM-DDTHH:MM:SS.000Z",
        }
    """
    op_label = "d4h._post_attendance"
    url = f"{BASE_URL}/team/{TEAM_ID}/attendance"
    resp = _safe_http_call(httpx.post, url, op_label, headers=_auth_header(), json=payload, timeout=30.0)
    _log_and_raise_for_status(resp, op_label)
    return resp.json()


def _patch_attendance(attendance_id: int, status: str) -> dict:
    """PATCH the status of an existing attendance record.

    UNUSED — retained deliberately, NOT dead-by-accident. Its only intended
    caller was the per-decline (NO-reply) ABSENT sync, GH issue #442, which
    was DECLINED per Bill 2026-07-19: EB declines and non-responses surface
    in the Slack #active-incidents tally only and are never sent to D4H.
    Kept because it is spike-validated and cheap to hold — wiring it up would
    mean reversing that decision. Spike ref: experiments/d4h/07_attendance.py.
    """
    op_label = "d4h._patch_attendance"
    url = f"{BASE_URL}/team/{TEAM_ID}/attendance/{attendance_id}"
    resp = _safe_http_call(httpx.patch, url, op_label, headers=_auth_header(), json={"status": status}, timeout=30.0)
    _log_and_raise_for_status(resp, op_label)
    return resp.json()


def _get_handlers_for_member(member_id: int) -> list[dict]:
    """GET handler-animal association rows for one member.

    GET /v3/team/{teamId}/handlers?member_id={member_id}

    A "handler" in D4H is a member↔animal association — basically an id pair
    plus metadata (per Dan Doyle 2026-05-14: "that is basically all a 'handler'
    is in the system, just an id that points to a member and animal id pair").
    A member with no handler rows is not a K9 handler. A member with ≥1 row
    is a K9 handler for those animal(s).

    Endpoint shipped 2026-06-03 (Dan Doyle release). Empirically validated via
    experiments/d4h/16_handlers_endpoint.py — Step 3 returned 3 rows for Kris
    (Aria/Annie/Allie). GET is in our PAT's scope; POST returns 403 (separate
    permission rollout pending — does not affect this code path).

    Raises D4HClientError/D4HServerError on HTTP failure (caller decides
    whether to swallow). Pagination is defensive — no SCCSSAR handler has
    >10 dogs registered, but the endpoint follows the same paged shape as
    /attendance so we mirror that pattern.
    """
    op_label = "d4h._get_handlers_for_member"
    url = f"{BASE_URL}/team/{TEAM_ID}/handlers"
    all_records: list[dict] = []
    page = 0
    seen_total = None
    while True:
        resp = _safe_http_call(httpx.get, url, op_label,
                               headers=_auth_header(),
                               params={"member_id": member_id, "page": page, "size": 100},
                               timeout=30.0)
        _log_and_raise_for_status(resp, op_label)
        body = resp.json()
        items = _extract_records(body)
        if seen_total is None:
            seen_total = body.get("totalSize")
        all_records.extend(items)
        if not items or (seen_total is not None and len(all_records) >= seen_total):
            break
        page += 1
    return all_records


def _extract_handler_animal_id(handler) -> Optional[int]:
    """Return the integer `animal.id` for a /handlers row, or None if any
    layer is malformed.

    Same nesting shape — and same defensive treatment — as
    _extract_attendance_member_id: the row carries `{"animal": {"id": N}}`
    with no top-level `animalId`. Verified against live team 1775 in
    experiments/d4h/24_animal_attendance_shape.py.

    Test mirror: backend/test_d4h.py::TestExtractHandlerAnimalId.
    """
    if not isinstance(handler, dict):
        return None
    animal = handler.get("animal")
    if not isinstance(animal, dict):
        return None
    animal_id = animal.get("id")
    if not isinstance(animal_id, int):
        return None
    return animal_id


def _get_animal_attendance(activity_id: int) -> list[dict]:
    """GET all animal-attendance records for an incident.

    The route is SINGULAR — `/animal-attendance`. The plural spelling this
    module's v1.1 stub documented 404s (issue #756). Query param is snake_case
    `activity_id`; camelCase is a 400, same inconsistency as /attendance.

    Filter verified with a positive control rather than assumed: unfiltered
    totalSize 605 vs 1 for a known activity, because a 200 with zero rows is
    indistinguishable from an ignored parameter. Spike:
    experiments/d4h/24_animal_attendance_shape.py.

    Pagination mirrors _get_attendance / _get_handlers_for_member.
    """
    op_label = "d4h._get_animal_attendance"
    url = f"{BASE_URL}/team/{TEAM_ID}/animal-attendance"
    all_records: list[dict] = []
    page = 0
    seen_total = None
    while True:
        resp = _safe_http_call(httpx.get, url, op_label,
                               headers=_auth_header(),
                               params={"activity_id": activity_id, "page": page, "size": 100},
                               timeout=30.0)
        _log_and_raise_for_status(resp, op_label)
        body = resp.json()
        items = _extract_records(body)
        if seen_total is None:
            seen_total = body.get("totalSize")
        all_records.extend(items)
        if not items or (seen_total is not None and len(all_records) >= seen_total):
            break
        page += 1
    return all_records


def _post_animal_attendance(payload: dict) -> dict:
    """POST a new animal-attendance record. Returns the created record dict.

    Payload shape (verified against live team 1775 2026-08-18 by
    experiments/d4h/24_animal_attendance_shape.py, which created nothing):
        {
            "animalId": <int>,
            "memberId": <int>,
            "activity": {"id": <int>, "resourceType": "Incident"},
            "duration": <int minutes>,
        }

    The body schema is STRICT — an unrecognized key is a 400, not a silent
    drop. In particular `status`, `startsAt` and `endsAt` are all rejected
    here even though /attendance requires them.
    """
    op_label = "d4h._post_animal_attendance"
    url = f"{BASE_URL}/team/{TEAM_ID}/animal-attendance"
    resp = _safe_http_call(httpx.post, url, op_label, headers=_auth_header(), json=payload, timeout=30.0)
    _log_and_raise_for_status(resp, op_label)
    return resp.json()


def _extract_member_email_value(member) -> str:
    """Return the member's email-value as a strippable, lowercaseable string,
    or '' if any layer is None / missing / malformed.

    D4H's member.email field is dict-shaped `{value, verified}` per the spec,
    but in practice `value` is None on unverified accounts. Pre-PR 7.3 the
    consumer at _get_member_by_email did `.get("value", "")` — that only
    returns the default when the KEY is missing; when value=None (real
    production case), .get returns the actual None, then .strip() raises
    AttributeError. Discovered 2026-05-19 by a live per-YES sync that
    crashed on the first member with email.value=None before ever finding
    the responder.

    Test mirror: backend/test_main_regression.py::TestExtractMemberEmailValue.
    """
    if not isinstance(member, dict):
        return ""
    email = member.get("email")
    if not isinstance(email, dict):
        return ""
    value = email.get("value")
    if not isinstance(value, str):
        return ""
    return value


def _get_member_by_email(email: str) -> Optional[dict]:
    """GET the D4H member matching this email address, or None if not found.

    Paginates through /team/{teamId}/members and filters client-side because
    D4H doesn't support email-equality filter on the members endpoint.
    Email field is dict-shaped: {"value": "...", "verified": bool} — see
    _extract_member_email_value for the None-safe extraction. Pre-PR 7.3
    this function crashed with AttributeError when any member in the
    response had email.value=None (which D4H does for unverified accounts).

    Spike reference: experiments/d4h/07_attendance.py (member lookup helper).
    """
    op_label = "d4h._get_member_by_email"
    url = f"{BASE_URL}/team/{TEAM_ID}/members"
    target = email.strip().lower()
    page = 0
    seen_total = None
    seen_so_far = 0
    while True:
        resp = _safe_http_call(httpx.get, url, op_label,
                               headers=_auth_header(),
                               params={"page": page, "size": 500},
                               timeout=30.0)
        _log_and_raise_for_status(resp, op_label)
        body = resp.json()
        items = _extract_records(body)
        if seen_total is None:
            seen_total = body.get("totalSize")
        for m in items:
            # PR 7.3: was `m_email = (m.get("email") or {}).get("value", "")`
            # which crashed when email.value=None. The helper returns "" in
            # that case (and every other malformed shape).
            m_email = _extract_member_email_value(m)
            if m_email.strip().lower() == target:
                return m
        seen_so_far += len(items)
        if not items or (seen_total is not None and seen_so_far >= seen_total):
            return None
        page += 1


def _get_equipment_by_ref(ref: str, kind_title: str) -> Optional[dict]:
    """GET the D4H equipment record matching ref + kind.title, or None.

    Paginates through /team/{teamId}/equipment and filters client-side because
    D4H doesn't support a server-side ref+kind composite filter. Used to find
    Drone #6 (ref="Drone #6", kind.title="UAS"). Spike reference:
    experiments/d4h/09_equipment.py::find_drone.
    """
    op_label = "d4h._get_equipment_by_ref"
    url = f"{BASE_URL}/team/{TEAM_ID}/equipment"
    page = 0
    seen_total = None
    seen_so_far = 0
    while True:
        resp = _safe_http_call(httpx.get, url, op_label,
                               headers=_auth_header(),
                               params={"page": page, "size": 500},
                               timeout=30.0)
        _log_and_raise_for_status(resp, op_label)
        body = resp.json()
        items = _extract_records(body)
        if seen_total is None:
            seen_total = body.get("totalSize")
        for eq in items:
            kind = eq.get("kind") or {}
            if eq.get("ref") == ref and kind.get("title") == kind_title:
                return eq
        seen_so_far += len(items)
        if not items or (seen_total is not None and seen_so_far >= seen_total):
            return None
        page += 1


def _post_equipment_usage(activity_id: int, equipment_id: int, duration: int = 0) -> dict:
    """POST an equipment-usage record (drone attached to an incident).

    Payload uses camelCase `activityId`, `equipmentId`, `duration` (minutes)
    per spike 09 — D4H's actual API contract. The plan template suggested
    snake_case `activity_id`/`equipment_id`/`duration_min`; the spike was
    authoritative. `duration` is the canonical key for kind.type=EQUIPMENT
    items (cf. `distance` for VEHICLE, `used` for SUPPLY).

    Spike reference: experiments/d4h/09_equipment.py::add_equipment_to_incident.

    Idempotency note (batch-3 G.LOW): This function has NO LOCAL existence
    check before POSTing — unlike `mark_member_attending` which does a
    GET-before-POST guard. The structural guarantee that prevents duplicate
    equipment-usage records is the Cluster B Firestore tombstone at Step 1.5
    of /send-notification: it ensures `create_incident_with_subject` (the
    sole caller chain that eventually invokes this function via
    `add_drone_if_uas_dispatched`) runs at most once per event_id. If that
    tombstone invariant is ever broken (e.g., a future refactor moves
    drone-attach out of the tombstone-guarded handler), this function will
    need its own existence check — `_get_equipment_usages_for_activity`
    already exists for that purpose. See Melanie's 2026-05-30 D4H audit
    finding #3.
    """
    op_label = "d4h._post_equipment_usage"
    url = f"{BASE_URL}/team/{TEAM_ID}/equipment-usages"
    payload = {
        "activityId":  activity_id,
        "equipmentId": equipment_id,
        "duration":    duration,
    }
    resp = _safe_http_call(httpx.post, url, op_label, headers=_auth_header(), json=payload, timeout=30.0)
    _log_and_raise_for_status(resp, op_label)
    return resp.json()


def _get_equipment_usages_for_activity(activity_id: int) -> list[dict]:
    """GET all equipment-usage records attached to an incident.

    Used for drone-attach idempotency check — orchestrator skips POST
    if drone already attached. Spike reference: NEW (no PR 1 spike;
    pattern mirrors _get_attendance from Task 2.7 — snake_case
    `activity_id` filter param, 0-indexed pages, totalSize end-signal).
    """
    op_label = "d4h._get_equipment_usages_for_activity"
    url = f"{BASE_URL}/team/{TEAM_ID}/equipment-usages"
    all_records: list[dict] = []
    page = 0
    seen_total = None
    while True:
        resp = _safe_http_call(httpx.get, url, op_label,
                               headers=_auth_header(),
                               params={"activity_id": activity_id, "page": page, "size": 500},
                               timeout=30.0)
        _log_and_raise_for_status(resp, op_label)
        body = resp.json()
        items = _extract_records(body)
        if seen_total is None:
            seen_total = body.get("totalSize")
        all_records.extend(items)
        if not items or (seen_total is not None and len(all_records) >= seen_total):
            break
        page += 1
    return all_records


# ---------------------------------------------------------------------------
# High-level orchestrators — public API consumed by main.py
# ---------------------------------------------------------------------------

def create_incident_with_subject(
    ocr_data: dict,
    dispatch_dt: datetime,
    eb_groups: list[str],
    dispatcher_metadata: dict,
    project_id: Optional[str],
) -> tuple[int, list[str], list[str]]:
    """Create a D4H incident + apply tags + attach the Subject involved-person.

    Returns (activity_id, unmapped_eb_groups, post_create_failures).
    Caller (main.py) emits one event-log entry per unmapped group per
    design doc §6.2 plus one per post_create_failure.

    Composition: _build_create_incident_payload → _post_incident →
    _map_eb_groups_to_d4h_tags → _post_tags → _build_involved_person_payload
    → _post_involved_person.

    Failure semantics — UPGRADED 2026-05-16 after the partial-failure
    orphan pattern surfaced live:

      - _post_incident failure: NO recovery possible (no activity_id),
        raises D4HClientError / D4HServerError. Caller catches as before.
      - _post_tags failure: incident exists, just untagged. Captured as a
        post_create_failure string; orchestrator continues to involved-person.
      - _post_involved_person failure: incident exists, no Subject record.
        Captured as a post_create_failure string; orchestrator returns.

    Pre-fix, the orchestrator was all-or-nothing: any failure after
    _post_incident raised an exception, the caller never saw activity_id,
    and bulk-ABSENT enqueue was skipped — leaving 50+ attendance records
    in REQUESTED state ("Unconfirmed") on the orphaned incident. The new
    return shape lets the caller run bulk-ABSENT against the partially-
    successful activity_id while surfacing the post-create failures to
    the dispatcher event log.

    Test mirror: backend/test_d4h.py::_create_incident_with_subject (takes
    `client` kwarg; production calls module-level wrappers directly).
    """
    payload = _build_create_incident_payload(ocr_data, dispatch_dt, dispatcher_metadata, project_id)
    incident = _post_incident(payload)  # raises if this fails — no recovery
    # Batch-3 G.LOW: defensive .get() instead of [] subscript. Pre-fix
    # a D4H schema change (response missing 'id' field while still
    # returning 2xx) would raise KeyError, bubble up as an unhandled
    # exception, and surface as a generic 500. The incident DOES exist
    # in D4H at this point (the POST succeeded with 2xx); we just have
    # no way to retrieve the activity_id. Best we can do is surface the
    # failure clearly as D4HClientError (terminal — don't retry, which
    # would create a second orphaned D4H incident) and log enough
    # diagnostic to ID the schema drift. See Melanie's 2026-05-30 D4H
    # audit finding #4.
    activity_id = incident.get("id") if isinstance(incident, dict) else None
    if activity_id is None:
        resp_shape = (
            f"dict(keys={sorted(incident.keys())})"
            if isinstance(incident, dict)
            else type(incident).__name__
        )
        logger.error(
            "D4H _post_incident returned 2xx with no 'id' field — incident "
            "may exist orphaned in D4H; manual cleanup required. "
            "Response shape: %s",
            resp_shape,
        )
        raise D4HClientError(
            f"D4H _post_incident response missing 'id' field — incident "
            f"may exist orphaned; check D4H Console. Response shape: {resp_shape}"
        )

    mapped_tags, unmapped = _map_eb_groups_to_d4h_tags(eb_groups)
    post_create_failures: list[str] = []

    try:
        _post_tags(activity_id, mapped_tags)
    except (D4HClientError, D4HServerError) as exc:
        post_create_failures.append(f"D4H tag-POST failed: {exception_summary_no_body(exc)}")
        logger.warning("D4H tag-POST partial failure (activity_id=%s): %s",
                       activity_id, type(exc).__name__)

    try:
        person_payload = _build_involved_person_payload(ocr_data)
        # Spike 06:156 uses "incidentId" (not "activityId") as the linkage
        # field on /incident-involved-persons POST. Pre-fix used "activityId"
        # which D4H silently ignored, leaving the involved-person record
        # unlinked. Audit verified against the live spike 2026-05-16.
        person_payload["incidentId"] = activity_id
        _post_involved_person(person_payload)
    except (D4HClientError, D4HServerError) as exc:
        post_create_failures.append(f"D4H involved-person POST failed: {exception_summary_no_body(exc)}")
        logger.warning("D4H involved-person POST partial failure (activity_id=%s): %s",
                       activity_id, type(exc).__name__)

    # 4th post-create step: drone resource for UAS group dispatches.
    # Per Bill 2026-05-19 — group-dependent, not pilot-dependent. Runs once
    # at dispatch time instead of N times via per-YES fan-out.
    try:
        add_drone_if_uas_dispatched(activity_id, eb_groups)
    except (D4HClientError, D4HServerError) as exc:
        post_create_failures.append(f"D4H drone-attach failed: {exception_summary_no_body(exc)}")
        logger.warning("D4H drone-attach partial failure (activity_id=%s): %s",
                       activity_id, type(exc).__name__)

    return activity_id, unmapped, post_create_failures


def _extract_attendance_member_id(attendance) -> Optional[int]:
    """Return the integer `member.id` for an attendance record, or None if
    any layer is malformed.

    D4H's GET /attendance?activity_id=... response nests the member ID:
        {"id": <attendance_id>, "member": {"id": <member_id>, ...}, ...}
    There is NO top-level `memberId` field in the response (confirmed by
    spike experiments/d4h/12_attendance_record_shape.py against live data
    on 2026-05-19 — 0/55 records had top-level `memberId`, 55/55 had
    nested `member.id`).

    Pre-PR 7.4 mark_member_attending used `r.get("memberId")` which always
    returned None → matching list always empty → every per-YES sync emitted
    "no attendance record for member" even when the record clearly existed.

    Test mirror: backend/test_main_regression.py::TestExtractAttendanceMemberId.
    """
    if not isinstance(attendance, dict):
        return None
    member = attendance.get("member")
    if not isinstance(member, dict):
        return None
    member_id = member.get("id")
    if not isinstance(member_id, int):
        return None
    return member_id


def _resolve_k9_handler_role_id(member_email: str, eb_groups: list[str]) -> Optional[int]:
    """Return K9_HANDLER_ROLE_ID iff this YES-replier is a K9 handler for THIS dispatch.

    Decision:
      - Canine group NOT in eb_groups       → None (K9 not dispatched)
      - Member not found in D4H             → None (graceful degrade)
      - Member has 0 handler rows            → None (driver/flanker/etc., not a handler)
      - Member has ≥1 handler rows           → K9_HANDLER_ROLE_ID

    Per Bill (2026-06-03): the EB Canine group is broad — drivers, flankers,
    and handlers all share it. The precision signal for "is this responder
    actually a K9 handler" is the existence of /handlers rows for them.
    Using group membership alone would mis-tag drivers/flankers as handlers.

    Case-insensitive group match — mirrors the existing pattern in
    add_drone_if_uas_dispatched / handle_per_yes_sync_task (Canine, canine,
    CANINE all match). Renaming the EB group to lowercase or different casing
    should NOT silently break role assignment.

    Best-effort by design: HTTP failures on the /handlers GET are caught and
    return None, so the attendance POST still happens with no role rather
    than being blocked. The dispatcher can manually pick the role in D4H UI
    on the rare miss. See Failure-mode Discipline Q2 (PR-1 self-review).

    Test mirror: backend/test_d4h.py::_resolve_k9_handler_role_id (takes
    `client` kwarg; production calls module-level helpers).
    """
    if "canine" not in {g.casefold() for g in eb_groups}:
        return None
    try:
        member = _get_member_by_email(member_email)
        if member is None:
            return None
        handlers = _get_handlers_for_member(member["id"])
    except (D4HClientError, D4HServerError) as e:
        # Best-effort. Don't block the attendance POST on a role lookup.
        # No PII — only the exception type name, per the "No PII in logs"
        # core privacy guarantee.
        logger.warning(
            "_resolve_k9_handler_role_id: lookup failed (%s); proceeding with no role",
            type(e).__name__,
        )
        return None
    if handlers:
        return K9_HANDLER_ROLE_ID
    return None


def mark_member_attending(
    activity_id: int,
    member_email: str,
    *,
    role_id: Optional[int] = None,
) -> dict:
    """Mark a member ATTENDING by resolving their email → member_id →
    POST a new attendance record.

    Selective-mode (fullTeam: false) behavior: attendance starts empty;
    each YES-reply POSTs a fresh ATTENDING record. Replaces the prior
    PATCH-existing path used when D4H auto-staged REQUESTED records for
    every team member at incident creation. See CLAUDE.md Locked Decision
    "Attendance writes are POST-new, never PATCH-existing".

    Returns {"status": "success"|"already_attending"|"member_not_found",
              "detail": <attendance_id or email>}.
    Caller (per-YES Cloud Task — handle_per_yes_sync_task) emits the
    appropriate event-log entry.

    Idempotency: Cloud Tasks max_attempts=5 means this task may run up to
    five times if a downstream step (drone-add) raises 5xx after a
    successful attendance POST. Without an existence check we'd POST
    duplicate ATTENDING rows on every retry. GET /attendance first; if a
    record already exists for this member, return "already_attending"
    without POSTing. (The prior `add_drone_if_uas_pilot` used the same
    existing-check pattern; that function no longer needs it because
    drone-attach moved to dispatch-time and no longer fans out per YES.)

    Concurrent-retry TOCTOU (Cluster G.8, ACCEPTED — see CLAUDE.md
    Locked Decision "mark_member_attending concurrent-retry TOCTOU"):
    The existing-check above is a non-atomic read-then-write —
    `_get_attendance` returns the current list, the for-loop checks for
    `member_id`, and `_post_attendance` writes a new record. Two
    CONCURRENT executions could both pass the check and both POST,
    creating duplicate ATTENDING rows.

    Cloud Tasks has two retry shapes:
      (A) Same-task re-enqueue (e.g., polling-loop double-fire detects
          the same YES twice and calls `enqueue_per_yes_sync` again):
          the deterministic task name `yes-{event_id}-{safe_email}`
          with the 24h Cloud Tasks dedup window suppresses the second
          enqueue. NO concurrent execution. Pinned by
          `TestEnqueuePerYesSync` in test_d4h.py.
      (B) Cloud Tasks RETRY of a timed-out task: same task, retry
          attempt N, original worker may still be alive (e.g., the
          GET took 31s, exceeding the 30s task deadline; original
          POST is still in flight when retry attempt 2 fires). This
          IS concurrent execution, NOT protected by the task-name
          dedup (it's by-design same-task behavior).

    Why we accept the (B) race (per Bill 2026-05-30, batch-3 Cluster G.8):
    - The operation is short (typically <2s for GET + POST roundtrip);
      timeout-triggered concurrent retry requires both attempts to be
      alive simultaneously, which means the original must be hung for
      30+s — extremely rare absent a D4H outage
    - max_attempts=5 with default exponential backoff means retries
      fire ~10s, ~30s, ~90s after the original — well after a normal
      <2s operation completes
    - Recovery cost is one manual click in D4H to remove the duplicate
      attendance row at incident close-out (D4H's "Update Attendance"
      view shows duplicates and lets the dispatcher delete with one
      click each)
    - True atomic guards would require either:
      (i)  A D4H-side unique constraint on (activity_id, member_id) —
           not exposed in the API; we'd have to ask D4H to add it
      (ii) A Firestore-side tombstone per (event_id, member_id) checked
           BEFORE the POST — adds a Firestore read per per-YES sync;
           the read itself is TOCTOU-vulnerable unless wrapped in a
           transaction (which adds further round-trips); the
           engineering cost exceeds the operational impact
      Neither is worth the cost given the recovery is one manual click.

    Issue #442 (per-NO ABSENT sync, the only thing that would wire in
    `_patch_attendance`) was DECLINED per Bill 2026-07-19 — declines and
    non-responses are Slack-tally-only and never reach D4H — so this
    analysis stands as-is today. It must be REVISITED only if that decision
    is ever reversed: PATCH-existing has different idempotency semantics
    than POST-new (same TOCTOU shape, different recovery — the
    duplicate-detection logic for PATCH would need its own analysis).

    Does NOT catch D4H exceptions — caller (handle_per_yes_sync_task)
    decides retry policy. D4HServerError from _post_attendance triggers
    Cloud Tasks retry; D4HClientError surfaces as "failed" via Cloud Tasks
    giving up after max_attempts.

    Timing: startsAt = now (UTC), endsAt = startsAt + 60 seconds.
    The 60s offset is the smallest unambiguous placeholder that satisfies
    D4H's `endsAt > startsAt` Zod schema constraint (verified live 2026-05-19:
    `endsAt == startsAt` returns 400 attendance:invalidDatetimeRange).
    Per Bill: any guessed endsAt would be wrong because the dispatcher
    always sets the real end time manually at incident close-out. A 60-second
    window is operationally impossible for SAR attendance — it visually
    screams "edit me." A 12h guess would look plausible and get trusted,
    which is worse.

    Test mirror: backend/test_d4h.py::_mark_member_attending (takes
    `client` kwarg; production calls module-level wrappers directly).
    """
    member = _get_member_by_email(member_email)
    if member is None:
        return {"status": "member_not_found", "detail": member_email}

    member_id = member["id"]

    existing = _get_attendance(activity_id)
    for r in existing:
        if _extract_attendance_member_id(r) == member_id:
            return {"status": "already_attending", "detail": r.get("id")}

    now = datetime.now(timezone.utc)
    payload = {
        "activityId": activity_id,
        "memberId":   member_id,
        "status":     STATUS_ATTENDING,
        "startsAt":   _format_d4h_datetime(now),
        # +60s — smallest value satisfying D4H's endsAt > startsAt range
        # check while remaining an obvious placeholder for the dispatcher.
        "endsAt":     _format_d4h_datetime(now + timedelta(seconds=60)),
    }
    # Optional roleId — used by the per-YES caller to tag K9 handlers as
    # "K9 Handler" on the Attendance tab (PR-1, 2026-06-03). Omitted from
    # the payload entirely when None to preserve the prior payload shape
    # for non-K9 responders (zero behavior change for them).
    if role_id is not None:
        payload["roleId"] = role_id
    response = _post_attendance(payload)
    return {"status": "success", "detail": response.get("id")}


def add_drone_if_uas_dispatched(activity_id: int, dispatched_eb_groups: list[str]) -> bool:
    """Attach the SAR drone to this incident if the UAS group was dispatched.

    Per Bill 2026-05-19: drone allocation is group-dependent, not
    pilot-dependent. If the dispatcher pages out the `UAS` EB group, the
    drone is the right resource to log on the incident regardless of which
    specific pilot YES-replies. Previously this lived per-YES (each UAS
    responder triggered an attempt with an idempotency check); moving it
    to dispatch-time eliminates the fan-out + defensive-guard pattern.

    Returns True only on successful attach. False for:
    - `UAS` not in the dispatched group list (early-exit, no HTTP)
    - drone equipment record not found in D4H

    The "drone already attached" idempotency check that previously guarded
    against per-YES race conditions is gone — this function runs exactly
    once at incident-create time inside create_incident_with_subject, so
    there's nothing to race against.

    Composition: early-exit → _get_equipment_by_ref → _post_equipment_usage.

    Does NOT catch D4H exceptions — caller (create_incident_with_subject)
    captures the failure in post_create_failures for dispatcher event-log surfacing.

    Test mirror: backend/test_d4h.py::_add_drone_if_uas_dispatched (takes
    `client` kwarg; production calls module-level wrappers directly).
    """
    # Case-insensitive: _map_eb_groups_to_d4h_tags lowercases internally,
    # and Kris's 2026-05-19 EB rebuild already changed group names once
    # (dropped prefixes). A future rename to lowercase would silently
    # skip the drone attach with no log or error. Defensive match against
    # the canonical "uas" lowercased form covers any future casing variation.
    if "uas" not in {g.casefold() for g in dispatched_eb_groups}:
        return False
    drone = _get_equipment_by_ref(DRONE_REF, DRONE_KIND_TITLE)
    if drone is None:
        return False
    _post_equipment_usage(activity_id, drone["id"], duration=0)
    return True


def sync_k9_attendance(activity_id: int, member_email: str) -> dict:
    """Record this K9 handler's dog(s) on the incident's D4H animal attendance.

    D4H shipped the endpoint 2026-08-18 (Dan Doyle). The route is SINGULAR —
    `/animal-attendance`; the plural spelling the v1.1 stub documented here
    404s, which is why earlier probing concluded the endpoint had not shipped
    (issue #756).

    ** There is no ABSENT for animals. **  The v1.1 plan was to POST every dog
    with `status=ABSENT` and let the dispatcher flip the ones that rolled. That
    is not implementable: the body schema is strict and rejects `status`
    outright — animal attendance has no status column at all. A row's existence
    IS the attendance, so the only dial is `duration`, and this posts the
    1-minute placeholder the team already uses for members and for the
    incident's own duration (Bill 2026-08-18). The dispatcher corrects the
    duration at close-out exactly as they do for members; that correction is
    the step that was previously an entire hand-entry.

    Composition: _get_member_by_email → _get_handlers_for_member →
    _get_animal_attendance (idempotency) → _post_animal_attendance per dog.

    The GET-before-POST check is the same shape as mark_member_attending's and
    carries the same accepted TOCTOU caveat: a Cloud Tasks retry of a task that
    timed out mid-flight can double-post, costing one manual row deletion.
    Unlike member attendance the check is keyed on the ANIMAL, not the member —
    a handler with two dogs must produce two rows.

    Returns a summary dict rather than None so the caller can log what happened
    (Failure-mode Discipline Q3 — no discarded return values):
      {"status": "member_not_found"}                     — no D4H account
      {"status": "not_a_handler"}                        — member has no dogs
      {"status": "success", "created": N, "skipped": M}

    Raises D4HClientError/D4HServerError on HTTP failure. The per-YES caller
    swallows those: attendance is already recorded by the time this runs, and a
    K9 failure must not fail the task and re-drive the whole sync.

    Test mirror: backend/test_d4h.py::sync_k9_attendance.
    """
    member = _get_member_by_email(member_email)
    if member is None:
        return {"status": "member_not_found"}

    handlers = _get_handlers_for_member(member["id"])
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
            _extract_handler_animal_id(r) for r in _get_animal_attendance(activity_id)
        )
        if animal_id is not None
    }

    created = 0
    skipped = 0
    for animal_id in animal_ids:
        if animal_id in already_present:
            skipped += 1
            continue
        _post_animal_attendance({
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


# Cloud Tasks queue names — TF resources in terraform/environments/dev/main.tf
_QUEUE_PER_YES_SYNC = "d4h-per-yes-sync"
_TARGET_PATH_PER_YES_SYNC = "/d4h-sync-yes"


def _enqueue_d4h_task(
    queue_name: str,
    task_name: str,
    target_path: str,
    payload: dict,
) -> None:
    """Common Cloud Tasks enqueue helper for D4H workers.

    Mirrors backend/main.py::_enqueue_http_task structure. Deterministic
    task name dedupes re-enqueues (Cloud Tasks dedupe window: 24 hours).

    Required env vars: GCP_PROJECT, CLOUD_RUN_SERVICE_URL (the publicly-
    routable URL of this Cloud Run service so Cloud Tasks can POST back
    to /d4h-* endpoints), CLOUD_TASKS_SERVICE_ACCOUNT (the SA email that
    Cloud Tasks uses to OIDC-token-sign requests). GCP_REGION is optional
    (defaults to "us-central1").

    Raises ValueError if any required env var is missing.
    """
    from google.cloud import tasks_v2
    import json as _json
    project_id = os.environ.get("GCP_PROJECT", "").strip()
    region     = os.environ.get("GCP_REGION", "us-central1").strip()
    service_url = os.environ.get("CLOUD_RUN_SERVICE_URL", "").strip()
    sa_email   = os.environ.get("CLOUD_TASKS_SERVICE_ACCOUNT", "").strip()
    if not project_id or not service_url or not sa_email:
        raise ValueError(
            "Cloud Tasks env vars missing: requires GCP_PROJECT, "
            "CLOUD_RUN_SERVICE_URL, CLOUD_TASKS_SERVICE_ACCOUNT. "
            "Configured in Terraform per PR 4."
        )

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(project_id, region, queue_name)
    full_task_name = f"{parent}/tasks/{task_name}"
    # `audience` MUST be set explicitly to match what the worker endpoint's
    # _verify_oidc_request expects. When omitted, Cloud Tasks defaults the
    # audience claim to the FULL URL including path; the worker checks
    # against CLOUD_RUN_SERVICE_URL (base URL, no path) and 401s on the
    # mismatch. The /poll-incident and /delete-template enqueues set
    # `audience: service_url` for this reason — D4H must match.
    # Live verification: smoke test 2026-05-17 hit 401 × 5 retries until
    # the bulk-ABSENT task died, surfaced in Cloud Run logs.
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


def _sanitize_task_name(raw: str) -> str:
    """Coerce a Cloud Tasks task ID to the only charset Cloud Tasks accepts.

    Cloud Tasks validates task IDs against [A-Za-z0-9_-]; anything else is
    rejected with InvalidArgument at enqueue time. Every disallowed run
    collapses to a single "-" so the result stays deterministic — the whole
    dedup guarantee (same event + same responder = same task name, suppressed
    inside the 24h window) depends on this being a pure function of the input.

    Deliberately NOT a strip: mapping to "-" keeps two names distinct that
    would otherwise collide once punctuation is removed.
    """
    return re.sub(r"[^A-Za-z0-9_-]+", "-", raw)


def enqueue_per_yes_sync(event_id: str, member_email: str, eb_groups: list[str]) -> None:
    """Enqueue a per-YES D4H attendance sync task for one responder who accepted in EB.

    Deterministic task name `yes-{event_id}-{safe_email}` — re-enqueues within the
    Cloud Tasks 24h dedupe window are no-ops (guards against polling-loop double-fire).
    @ and . replaced with -at- and - so the name is a valid resource-path segment.

    Worker: /d4h-sync-yes → handle_per_yes_sync_task (PR 6).
    Per-YES typically arrives >3 min after dispatch — well after D4H finalizes the
    auto-populated attendance records, so the async-init timing race does not apply.
    If a responder YES-replies unusually fast, mark_member_attending raises
    D4HServerError → 502 → Cloud Tasks retries (max_attempts=5).

    Test mirror: backend/test_d4h.py::TestEnqueuePerYesSync.
    """
    safe_email = member_email.replace("@", "-at-").replace(".", "-")
    # Cloud Tasks task IDs accept ONLY [A-Za-z0-9_-]. `event_id` is
    # _slugify_for_firestore(event_name), which lowercases and joins on
    # whitespace but does NOT strip punctuation — so an agency name like
    # "Humboldt County Sheriff's Office" carries an apostrophe straight into
    # the task name and the enqueue fails with InvalidArgument. The caller
    # logs and continues, so the dispatch completes and NOTHING syncs to D4H
    # attendance: silent, and for every responder on the callout.
    #
    # Observed live on personal-dev 2026-08-01. In-county agencies are
    # canonicalized to SCCSO / SJPD / MPD and have no punctuation, so only
    # out-of-county MUTUAL AID hits it — the same untested path as #672 and
    # #676. slack.py::_slugify_channel_name already strips apostrophes, which
    # is why the Slack channel was created fine while D4H silently did not.
    #
    # Sanitised HERE rather than in _slugify_for_firestore on purpose:
    # event_id is the Firestore doc ID and the cross-app identity key, and
    # changing its shape would orphan in-flight incidents. The task name only
    # has to be deterministic and unique; the true event_id still travels in
    # the payload, so the worker looks up the right doc.
    task_name = _sanitize_task_name(f"yes-{event_id}-{safe_email}")
    _enqueue_d4h_task(
        queue_name=_QUEUE_PER_YES_SYNC,
        task_name=task_name,
        target_path=_TARGET_PATH_PER_YES_SYNC,
        payload={"event_id": event_id, "member_email": member_email, "eb_groups": list(eb_groups)},
    )


def _load_d4h_activity_id(event_id: str) -> Optional[int]:
    """Read d4h_activity_id from incidents/{event_id} Firestore doc.

    Returns None if doc missing or field unset (D4H create-incident failed at
    dispatch — graceful-degrade per design doc §5.3). Uses the same lazy
    firestore.Client singleton pattern as backend/rate_limit.py::_get_db().

    Test mirror: backend/test_d4h.py — production calls this directly;
    mirror takes a `load_activity_id` lambda as kwarg for test injection.

    Import note: bare `from rate_limit` — NOT `from backend.rate_limit`. The
    container Dockerfile flattens backend/*.py into /app/, so there is no
    `backend` package at runtime. The `backend.` prefix passes local tests
    (where backend/ is a real directory) but raises ModuleNotFoundError in
    Cloud Run. Pinned by TestNoBackendPackagePrefixInImports in
    test_main_regression.py.
    """
    from rate_limit import _get_db
    db = _get_db()
    if db is None:
        return None
    doc_ref = db.collection("incidents").document(event_id)
    doc = doc_ref.get()
    if not doc.exists:
        return None
    return doc.to_dict().get("d4h_activity_id")


def handle_per_yes_sync_task(
    event_id: str,
    member_email: str,
    eb_groups: list[str],
) -> None:
    """Cloud Tasks worker: per-YES D4H sync for one responder.

    Called by /d4h-sync-yes endpoint (PR 6) when a responder YES-replies in
    Everbridge. Composes:
      1. Load d4h_activity_id from Firestore — graceful-degrade if missing
         (D4H create-incident failed at dispatch; nothing to sync)
      2. mark_member_attending — PATCH the responder's attendance to ATTENDING
         - member_not_found: log + continue (drone-add may still apply)
         - failed:           raise D4HServerError (Cloud Tasks retry)
         - success:          continue
      3. Conditionally trigger K9 sync if responder is in `Canine` EB
         group (renamed from `SAR - Canine Team`; DOGS-* sub-groups
         collapsed into this single group in Kris's 2026-05-19 rebuild).
         K9 sync stays per-YES because it populates the K9 sub-tab with
         per-handler data (which dogs each handler brought) — inherently
         per-arrival, not per-dispatch. Best-effort: attendance has already
         been POSTed by then, so a K9 failure is logged and swallowed.

    Drone-attach moved OUT of this function 2026-05-19: it's now a
    dispatch-time step inside create_incident_with_subject. Per Bill —
    group-dependent (was UAS group dispatched?), not pilot-dependent
    (did a pilot specifically YES?).

    Returns None on success or graceful-degrade. Raises D4HServerError on
    transient attendance-PATCH failure (Cloud Tasks max_attempts=5 per
    Phase 1 lock-in).

    Test mirror: backend/test_d4h.py::_handle_per_yes_sync_task (takes
    `client` + `load_activity_id` kwargs; production calls module-level helpers).

    Milestone-only contract (PR 7.1):
      This function MUST NOT write to incidents/{event_id}.d4h_event_log.
      Per-YES events are high-volume; the textarea Event Log stays scannable
      with just the four dispatch-time milestones. Debug visibility lives in
      logger.info / logger.warning. Pinned by TestD4HPerYesMilestoneOnlyContract
      in backend/test_main_regression.py.
    """
    activity_id = _load_d4h_activity_id(event_id)
    if activity_id is None:
        logger.info(
            "d4h.handle_per_yes_sync_task: graceful-degrade — no activity_id "
            "in Firestore for event_id=%s (D4H create failed at dispatch)",
            event_id,
        )
        return

    # PR-1 (2026-06-03): resolve K9 Handler role for the attendance POST so the
    # Attendance tab Role column populates without dispatcher input. Best-effort —
    # _resolve_k9_handler_role_id swallows D4H errors and returns None on
    # failure so the attendance POST still happens. Non-K9 responders are a
    # one-line early-exit inside the resolver (no extra HTTP).
    role_id = _resolve_k9_handler_role_id(member_email, eb_groups)

    attendance_result = mark_member_attending(activity_id, member_email, role_id=role_id)
    # "member_not_found" continues — drone-add may still apply if responder is
    # somehow in EB UAS group despite no D4H membership (rare; logged below by
    # the caller via the event log catalog).
    # "already_attending" continues — idempotent retry; downstream steps still apply.
    # "success" continues normally. No "failed" status under Selective mode:
    # _post_attendance raises D4HClientError/D4HServerError on failure, which
    # propagates here for Cloud Tasks retry routing — no explicit re-raise needed.
    #
    # Pre-Cluster-D the return value was discarded — "member_not_found"
    # left zero diagnostic signal in Cloud Run logs even though the EB
    # responder was confirmed and D4H had no attendance row. The "member_not_found"
    # branch surfaces a warning so the dispatcher / on-call can investigate
    # the EB-confirmed-but-no-D4H mismatch (typically a missing D4H account
    # or email alias the membership-lookup paginates past). The other statuses
    # log at debug so operational success-counts are derivable from logs
    # without raising the noise floor on the per-YES path.
    status_value = attendance_result.get("status") if isinstance(attendance_result, dict) else None
    if status_value == "member_not_found":
        logger.warning(
            "d4h.handle_per_yes_sync_task: member_not_found for event_id=%s "
            "activity_id=%s — EB-confirmed responder has no D4H account "
            "(or email alias mismatch); no attendance row created. "
            "Verify the responder's D4H membership + email alignment.",
            event_id, activity_id,
        )
    else:
        logger.debug(
            "d4h.handle_per_yes_sync_task: attendance status=%s for event_id=%s",
            status_value, event_id,
        )

    # Post-2026-05-19 EB rebuild: `Canine` group (was `SAR - Canine Team`,
    # DOGS-* subgroups collapsed in). Drone-attach moved to dispatch-time
    # (create_incident_with_subject); not here anymore.
    # Case-insensitive match for the same reason as the UAS check in
    # add_drone_if_uas_dispatched — a future EB rename to lowercase
    # would silently skip K9 sync.
    if "canine" in {g.casefold() for g in eb_groups}:
        try:
            k9_result = sync_k9_attendance(activity_id, member_email)
        except D4HServerError:
            # Transient (Failure-mode Discipline Q5): let it propagate so the
            # Cloud Task retries. Re-driving the whole per-YES sync is cheap
            # and safe — mark_member_attending short-circuits on
            # already_attending and sync_k9_attendance skips animals already
            # present — and this is the exact case the queue's max_attempts=5
            # budget exists to absorb. Swallowing it would permanently lose
            # the row and hand the dispatcher back the close-out data entry
            # this feature exists to remove. Same routing mark_member_attending
            # already relies on one step above.
            raise
        except Exception as exc:  # noqa: BLE001 — non-retriable, best-effort
            # 4xx (a payload-shape regression) plus anything unexpected from
            # our own code: retrying cannot fix either, and the attendance POST
            # above has already landed, so failing the task would burn the
            # retry budget to no effect. Warn rather than debug — a silent K9
            # gap is exactly the class of miss that costs a hand-entry at
            # close-out. event_id only, no member_email, per the No-PII-in-logs
            # rule; exception_summary_no_body keeps any D4H response body out
            # of the log line.
            logger.warning(
                "d4h.handle_per_yes_sync_task: K9 sync failed for event_id=%s "
                "activity_id=%s (%s); attendance is unaffected — add the dog "
                "manually on the incident's K9 tab if needed.",
                event_id, activity_id, exception_summary_no_body(exc),
            )
        else:
            # Captured, not discarded (Failure-mode Discipline Q3).
            # "member_not_found" already produced a warning on the attendance
            # path above, so it is not re-warned here.
            logger.debug(
                "d4h.handle_per_yes_sync_task: K9 sync %s for event_id=%s",
                k9_result, event_id,
            )
