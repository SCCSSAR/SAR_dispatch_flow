"""
main.py — SCCSSAR Dispatch Console backend API.

Endpoints:
  GET  /health   — unauthenticated health check (Cloud Run startup probe)
  POST /ocr      — authenticated: upload JPEG → Gemini OCR → incident summary

Request pipeline for POST /ocr (money spent only at step 14):
  1.  Cloud Run IAM blocks unauthenticated requests at platform layer
  2.  Parse Bearer token from Authorization header
  3.  Verify Google ID token (signature, aud, iss, exp)
  4.  Check email in allowlist
  5.  Check email_verified == True
  6.  Check Content-Type == multipart/form-data
  7.  Check Content-Length <= 10 MB
  8.  Read bytes; verify magic bytes FF D8 FF (real JPEG)
  9.  Re-verify actual byte count
  10. Open with Pillow; check dimensions (100×100 min, 8000×8000 max)
  11. Firestore transaction: global daily cap
  12. Firestore transaction: per-user rate limits
  13. [Phase 2: resize to max 2000×2000]
  14. Call Vertex AI Gemini 1.5 Flash
  15. Log operational metadata (no PII)
  16. Return structured incident summary to browser
"""

import asyncio
import datetime
import functools
import gc
import hashlib
import json
import logging
import math
import os
import re
import time
import unicodedata
import zoneinfo
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from googleapiclient.errors import HttpError as GoogleApiHttpError

# Batch-3 G.LOW: hoisted from inside handler bodies to module level so
# grep-based audits surface the dependency. Pre-fix these imports lived
# inside /send-notification (AlreadyExists, new_skeleton_incident_doc) and
# /close-incident-polling (firestore for @firestore.transactional). The
# scatter made it easy to miss the dependency in audits; a future refactor
# that moves AlreadyExists to a different import path in a google-api-core
# version bump would have silently broken the double-dispatch 409 guard
# without any obvious signal. See Melanie's 2026-05-30 batch-3 finding #10.
#
# Note: _get_eb_slack_db()'s lazy `from google.cloud import firestore` is
# deliberately kept (defers credential lookup to first call so module
# imports cleanly in environments without GCP credentials — see the
# docstring there). This module-level import only loads the firestore
# module itself; it does NOT instantiate firestore.Client.
from google.api_core.exceptions import AlreadyExists as _FirestoreAlreadyExists
from google.api_core.exceptions import NotFound as _FirestoreNotFound
from google.cloud import firestore as _firestore

from auth import require_authorized_dispatcher
from caltopo import build_incident_map, CalTopoOrphanMapError, CalTopoRateLimitError
import d4h
from gdocs import create_incident_doc
# NOTE: kept as its own line — test_main_regression pins the exact literal
# "^from incidents import new_skeleton_incident_doc" (PR-G.LOW item 4).
from incidents import new_skeleton_incident_doc
from incidents import followup_tombstone_is_dead
from image_validation import validate_jpeg_bytes, validate_jpeg_upload
from rate_limit import check_rate_limits
from secret_health import get_all_token_health
from gemini import extract_incident_summary, extract_staging_and_koester
from pdf_extract import (
    is_pdf,
    # #765 — the ONE age-from-DOB implementation. Aliased to the historical
    # private name so every call site and pin here is unchanged by the move.
    compute_age_from_dob as _compute_age_from_dob,
    extract_acroform_fields,
    build_synthetic_summary,
    _normalize_datetime,
    MAX_PDF_BYTES,
)


# Google Maps API key — read at startup; empty string = feature disabled.
# Set via GOOGLE_MAPS_API_KEY Cloud Run env var (stored in Secret Manager).
# Enable "Geocoding API" on the key in GCP Console → APIs & Services.
_GOOGLE_MAPS_API_KEY: str = os.environ.get("GOOGLE_MAPS_API_KEY", "")

# Geoapify Places API key — read at startup; empty string = source unavailable.
# Set via GEOAPIFY_API_KEY Cloud Run env var (stored in Secret Manager, one key
# per env). Used only by _query_geoapify_staging for POI lookup; when empty the
# Geoapify source short-circuits with no HTTP call (same guard as the Maps key),
# so an env without this var runs on Overpass exactly as before.
_GEOAPIFY_API_KEY: str = os.environ.get("GEOAPIFY_API_KEY", "")

# Pacific timezone constant — defined once at module level; ZoneInfo objects are cheap
# but re-creating them on every request adds noise and hides the dependency.
_PT = zoneinfo.ZoneInfo("America/Los_Angeles")


def _format_event_log_ts(now_utc: "datetime.datetime | None" = None) -> str:
    """Return an event-log timestamp in canonical format: ``YYYY-MM-DD HH:MM``.

    Server-rendered Pacific time, no timezone label. Matches the format
    used by ``intake_timestamp`` (the other server-rendered timestamp the
    dispatcher already sees in the textarea, in the synthetic Pass-1
    summary for v2 PDF forms). Consistency wins over explicit-TZ-marker.

    Example:
      ``datetime(2026, 5, 31, 15, 13, tzinfo=timezone.utc)``
        → ``"2026-05-31 08:13"``  (08:13 PT on the same day)

    Issue #542 (and follow-up Cluster I.2) — the original problem was
    that OCR-time event log entries rendered in Pacific (server-side,
    via ``_PT``) while the post-OCR dispatch-time entries rendered in
    browser-local time (frontend ``_insertEventLogEntries`` using
    ``new Date().getHours()``). For a Pacific-resident dispatcher the
    divergence was invisible; for a traveling dispatcher (Bill on EDT
    during the 2026-05-31 #519 closure session) the same incident's
    event log looked as if OCR happened three hours before CalTopo
    map creation. This helper unifies both code paths on a single
    server-rendered Pacific format.

    Cluster I.1 originally tried ``YYYY-MM-DDTHH:MMZ (HH:MM PT)`` to
    expose the UTC anchor for cross-environment debugging; Bill's
    feedback on the deployed result (2026-05-31): "I don't like the TZ
    string we're emitting for the frontend time stamps...they're
    foreign for a non-programmer to interpret." Simplified to match
    ``intake_timestamp`` byte-for-byte. The trade-off: a non-Pacific
    dispatcher sees no explicit signal that the time is Pacific.
    Accepted because readability wins for the daily case.

    ``now_utc`` is exposed so tests can pin the timestamp deterministically.
    Production callers pass nothing. A naive datetime raises ``ValueError``
    rather than silently rendering as UTC.

    Frontend mirror: ``_formatEventLogTs`` in frontend/index.html. CLAUDE.md
    Locked Decision "Event log timestamp format" pins the cross-file
    invariant; the test ``TestEventLogTimestampFormat`` in
    test_main_regression.py pins this helper's output.
    """
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    elif now_utc.tzinfo is None:
        raise ValueError(
            "_format_event_log_ts requires timezone-aware now_utc "
            "(use datetime.timezone.utc)"
        )
    return now_utc.astimezone(_PT).strftime("%Y-%m-%d %H:%M")


# Issue #755 — the subject's last-seen date/time, emitted as an Event Log
# chronology entry. Kris (Ops) asked for both "when were we notified" and "when
# was the subject last seen" to reach D4H; the first was already Event Log line 1,
# the second was captured on the intake form and used nowhere.
# [^\S\n]* is horizontal whitespace ONLY, and that is load-bearing. `\s*`
# matches newlines even under re.MULTILINE (which changes ^ and $, not \s),
# so a BLANK "Last Seen At:" line lets the match run on and capture the
# NEXT line — publishing "…San Jose, CA - Subject last seen" to the Event
# Log. Caught by the blank-value case while building this helper. Same
# defect class as issue #735, which tracks the eight existing field-line
# regexes with this shape (including the `_normalize_last_seen_at`
# substitution above); this one is written immune rather than joining them.
_LAST_SEEN_AT_RE = re.compile(r"^Last Seen At:[^\S\n]*(.+)$", re.MULTILINE)
_EVENT_LOG_MARKER = "\nEvent Log:\n"


def _subject_last_seen_value(summary: str) -> str:
    """The officer's last-seen date/time, or "" when the form did not record one.

    Reads the summary's already-normalized ``Last Seen At:`` line, so both intake
    paths are served by one call site: the PDF path normalizes at AcroForm read
    time via ``_normalize_datetime``, and the JPEG path is normalized by the
    ``_normalize_last_seen_at`` substitution earlier in /ocr. Call AFTER that
    substitution or the JPEG path emits raw officer handwriting.

    The value is NOT guaranteed to be a full timestamp. ``_normalize_datetime``
    returns time-only strings ("21:30", "04:45 AM") when the officer wrote no
    date, and that is passed through verbatim: per Bill (2026-08-18) an ambiguous
    field is reported exactly as written rather than completed by inference —
    high fidelity beats convenient.

    A bracketed value is a sentinel, not data: both paths render "[not recorded]"
    for a blank field, and Gemini leaves its own bracketed instruction text in
    place when it extracts nothing. Any leading "[" is therefore treated as
    absent.

    ``slack.format_pinned_welcome`` states the SAME rule independently, because
    the Slack welcome reads this field by a different route — the frontend parses
    the textarea into the dispatch payload — rather than through this helper. The
    two must not drift: while they did, a Gemini template leak reached responders
    on the pinned welcome while the Event Log and the D4H record correctly
    omitted it. Note the rule is deliberately broader than the exact-literal
    sentinel check ``Request:`` uses, which is sufficient there only because
    Request is PDF-path-only and can never carry a Gemini leak.
    """
    m = _LAST_SEEN_AT_RE.search(summary or "")
    if not m:
        return ""
    value = m.group(1).strip()
    if not value or value.startswith("["):
        return ""
    if value.casefold() in ("not recorded", "unknown"):
        return ""
    return value


def _insert_subject_last_seen_entry(summary: str) -> str:
    """Insert "<last seen> - Subject last seen" as the SECOND Event Log entry.

    Second, not first, and that position is load-bearing. The Event Name's date
    is reconstructed by a regex anchored on the ISO date appearing immediately
    after the "Event Log:" header (see the ``date_match`` search farther down
    this module), and pdf_extract converts the form date to ISO expressly to
    satisfy it. Inserting above line 1 would hand that regex the LAST-SEEN date
    instead of the request date — silently renaming the incident on the
    Everbridge title, the Slack channel, D4H referenceDescription and the CalTopo
    map title — and in the common time-only case ("21:30") it would match nothing
    at all, dropping Event Name reconstruction entirely. Placement chosen by Bill
    (2026-08-18) over hardening that regex, as the smaller change.

    The anchor is the END OF THE FIRST ENTRY LINE, not a fixed offset, so the
    JPEG path (where Gemini renders line 1 from the prompt template) inserts at
    the same place as the PDF path. No-op when the form recorded no last-seen
    value, when there is no Event Log section, or when the section is empty —
    a chronology entry is never worth corrupting the summary structure for.

    Pure-logic — no I/O. Test mirror: TestSubjectLastSeenEventLogEntry.
    """
    value = _subject_last_seen_value(summary)
    if not value:
        return summary
    pos = summary.find(_EVENT_LOG_MARKER)
    if pos == -1:
        return summary
    first_entry_start = pos + len(_EVENT_LOG_MARKER)
    first_entry_end = summary.find("\n", first_entry_start)
    if first_entry_end == -1:
        return summary
    first_entry = summary[first_entry_start:first_entry_end].strip()
    if not first_entry or first_entry.startswith("---"):
        return summary
    insert_at = first_entry_end + 1
    return summary[:insert_at] + f"{value} - Subject last seen\n" + summary[insert_at:]


# Overpass API mirrors — tried in order; primary occasionally returns 504 under load.
# DESIGN DECISION (Phase 1.5i): do not reorder without testing each mirror's uptime.
#
# Source of truth: https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances
# (audit 2026-05-20 — re-check when adding/changing mirrors):
#   - overpass-api.de  → operational (free, FOSSGIS — <10k queries/day policy)
#   - overpass.private.coffee  → operational (free, no rate limit; previously known
#                                 as overpass.kumi.systems — domain renamed)
#   - maps.mail.ru (VK Maps)  → REMOVED: officially suspended since 2026-03-16
#                                (replies HTTP 403 by design — wiki: "temporarily
#                                suspended from March 16, 2026")
#   - overpass.geofabrik.de  → commercial, requires API key — out of scope
#
# Live-test 2026-05-20 (Bill, personal-dev 17:31 UTC) confirmed all 3 prior
# mirrors failed simultaneously (504 + ReadTimeout + 403). 2 of the 3 failures
# were structural (renamed + suspended), not transient — the previous list was
# effectively a 1-mirror configuration.
_OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Staging venue tier — lower number = higher priority.
# Candidates are sorted (tier, distance) before being sent to Gemini so the best
# options appear first in the list Gemini works top-to-bottom through.
# DESIGN DECISION (do not revert — issue #174): schools are tier 1 so they reach
# Gemini's candidate list in dense urban areas where 12+ tier-1 POIs fill the cap.
# DESIGN DECISION (do not revert): fuel is tier 3 — active vehicle traffic and limited
# seating make gas stations poor staging areas despite their 24-hr availability.
# DESIGN DECISION (do not revert — issue #669): shopping centers are tier 1.
# They are the best real-world staging sites SAR gets — large lots, lighting,
# restrooms, multiple ingress points, room for a command post and a caravan.
# Every other tier-1 commercial category is a TENANT of exactly this kind of
# site, so before #669 the tool ranked storefronts and could not see the
# parking lot they shared: measured 2026-08-01 at Westfield Valley Fair, the
# production category list returned 25 features and not one was the mall —
# all food-court tenants (Auntie Anne's, Jamba, Marugame Udon, Eataly).
_STAGING_TIER = {
    "park": 1, "fast_food": 1, "pharmacy": 1, "hotel": 1, "motel": 1,
    "supermarket": 1, "grocery": 1, "school": 1, "college": 1, "mall": 1,
    # ops #839. Tier 2, NOT tier 1, and this is measured — do not "fix" it up to
    # match the parks/schools/malls lot-quality argument that put it in the main
    # pass. OSM's amenity=community_centre does not reliably mean a big lot:
    # measured 2026-09-07 at downtown SJ (1200 m, identical 8/2/1 counts from
    # Overpass and Geoapify), 3 of the 5 NAVIGABLE hits are SJSU club rooms and a
    # bike clinic — "Computer Science Club SJSU" and "Software & Computer
    # Engineering Society", both at house number 1 on a campus paseo, so the
    # PASS 2 leading-digit predicate passes them and nothing downstream filters
    # them. Ranking is (tier, distance) then a 7-slot cap, so tier 1 would let
    # those displace real parks and schools in exactly the dense anchors that
    # have good staging — the commercial.department_store flooding failure in a
    # new costume. Tier 2 keeps the genuine ones (Diadem returns "Mayfair
    # Community Center") surfacing at sparse anchors, where thin competition
    # clears the cap anyway, and that is where Bill wanted them. Bill 2026-09-07.
    "convenience": 2, "chemist": 2, "place_of_worship": 2, "community_centre": 2,
    # Ops #839. fire_station/police are WIDE-PASS ONLY (see
    # _GEOAPIFY_WIDE_ONLY_CATEGORIES) and tier 3 on purpose: Bill 2026-09-07,
    # "we don't want to stage at a PD or FD". Tier 3 means they never outrank a
    # real option, so they surface only where little else exists — which is the
    # case they were added for. Measured at Grant County Park, 4828 m: the whole
    # production category bundle returns 0 navigable candidates and
    # service.fire_station returns 1, i.e. the difference between one real
    # address and the zero that flips Gemini into fabricating a list.
    # An amenity ABSENT from this table sorts at the .get(..., 2) default, so
    # omitting a row here silently ranks a police station above a gas station.
    "fuel": 3, "fire_station": 3, "police": 3,
}

# Geoapify Places category taxonomy — the staging POI source (Overpass→Geoapify
# migration). Two calls per lookup keep the civic categories under the 100-result
# limit that starves parks when all categories share one bundled call in dense
# areas (spike_04, 2026-07-14: downtown 59 / SJSU 54 raw when civic is split).
#   - CIVIC: parks + schools + places-of-worship, no proximity bias (complete).
#   - COMMERCIAL: fast_food/markets/pharmacy/hotels/gas, bias on (nearest desired;
#     truncation harmless — no civic to lose).
_GEOAPIFY_CIVIC_CATEGORIES = [
    "leisure.park",
    "education.school",
    "education.college",
    "education.university",
    "religion.place_of_worship",
    # Ops #839, Bill 2026-09-07: community centres go in the MAIN pass ("those
    # tend to have large parking lots") — the same lot-quality argument that
    # puts parks/schools/malls at tier 1. This does NOT repeat the
    # commercial.department_store exclusion: that one was held because its hits
    # were four TENANTS of a site already listed, all competing for the same 7
    # slots. Community centres are distinct sites. Measured downtown SJ at
    # 1200 m: 8 features / 5 navigable added to a pool of 82 navigable.
    "activity.community_center",
]

# Ops #839 — appended to the CIVIC list on the WIDENED retry ONLY (see
# _STAGING_FALLBACK_RADIUS_M). Deliberately absent from the 1200 m pass: Bill
# 2026-09-07, "we don't want to stage at a PD or FD". They earn their place only
# in the zero-candidate case, where the alternative is not a better location but
# Gemini inventing one. Verified to exist by spike 2026-09-07
# (experiments/staging_places/spike_06_civic_wide.py) — Geoapify 400s on an
# unknown category, and "emergency.ambulance_station" / "service.ambulance_station"
# are NOT real names despite appearing in the docs (spike 2026-09-06).
_GEOAPIFY_WIDE_ONLY_CATEGORIES = [
    "service.fire_station",
    "service.police",
]
_GEOAPIFY_COMMERCIAL_CATEGORIES = [
    # Issue #669. Verified to exist by spike 2026-08-01 (experiments/geoapify/):
    # Geoapify 400s on an unknown category, and "commercial.retail" /
    # "commercial.outpost" both 400 — only this name and
    # "commercial.department_store" are real. department_store is deliberately
    # NOT included yet (Bill 2026-08-01): at a mall it adds Macy's, Nordstrom,
    # Bloomingdale's and Macy's Backstage — four tenants of a site already
    # listed — which would flood the 7-slot staging cap. Standalone big-box is
    # a separate question; monitor real dispatches first.
    "commercial.shopping_mall",
    "catering.fast_food",
    "commercial.supermarket",
    "commercial.convenience",
    "commercial.marketplace",
    "healthcare.pharmacy",
    "accommodation.hotel",
    "accommodation.motel",
    "commercial.gas",
]

# Geoapify-category-substring → OSM amenity vocabulary (the vocabulary _STAGING_TIER
# and all downstream consumers already speak). Best-tier-first: the first substring
# present in a feature's `categories` array wins, so a place tagged both
# education.school and education.college resolves to "school", and a supermarket
# also tagged commercial.marketplace resolves to "supermarket" (tier 1) not
# "grocery". _STAGING_TIER stays the single tier authority — no Geoapify tier table.
_GEOAPIFY_PRIORITY = [
    ("leisure.park", "park"),
    ("education.school", "school"),
    ("education.kindergarten", "school"),
    ("education.college", "college"),
    ("education.university", "college"),
    # Ops #839. Ahead of religion.place_of_worship (tier 2) so a hall tagged as
    # both resolves to the tier-1 community centre; BELOW the education entries
    # so a school with a community-hall tag stays a school. NOTE the spelling:
    # the canonical amenity vocabulary here is OSM's, and OSM spells it
    # "community_centre" while Geoapify's category is "community_center". The
    # Overpass leg lifts the raw OSM tag value straight into `amenity`, so
    # canonicalising the American spelling would leave every Overpass-sourced
    # centre missing from _STAGING_TIER and silently sorted at the default 2.
    ("activity.community_center", "community_centre"),
    # Issue #669 — ahead of every tenant category on purpose. The spike found
    # no mall/tenant co-tagging at either anchor (the mall and its anchor
    # stores are separate features), so this ordering is defensive rather than
    # load-bearing today; if a future feature does carry both, it must resolve
    # to the mall, because the lot is what we are staging in.
    ("commercial.shopping_mall", "mall"),
    ("catering.fast_food", "fast_food"),
    ("healthcare.pharmacy", "pharmacy"),
    ("accommodation.hotel", "hotel"),
    ("accommodation.motel", "motel"),
    ("commercial.supermarket", "supermarket"),
    ("commercial.marketplace", "grocery"),
    ("commercial.convenience", "convenience"),
    ("religion.place_of_worship", "place_of_worship"),
    ("commercial.gas", "fuel"),
    # Ops #839 — reachable only via _GEOAPIFY_WIDE_ONLY_CATEGORIES. Last, so a
    # feature carrying any other known category resolves to that instead.
    ("service.fire_station", "fire_station"),
    ("service.police", "police"),
]

# Overpass leg of the same vocabulary — the `amenity=` alternation, split so the
# widened retry can add to it (ops #839). These are raw OSM tag VALUES: the
# element parser lifts tags.get("amenity") straight into `amenity`, so each string
# here must match a _STAGING_TIER key exactly. Hence "community_centre" (OSM's
# British spelling), NOT the "community_center" of Geoapify's category name.
# shop= and leisure= stay inline in the query — nothing wide-only is added there.
_OVERPASS_AMENITIES = [
    "fast_food", "fuel", "pharmacy", "hotel", "motel",
    "school", "college", "place_of_worship", "community_centre",
]
_OVERPASS_AMENITIES_WIDE_ONLY = ["fire_station", "police"]

# Agency abbreviation → "City, CA" — used when LKP field has no comma-separated city.
# Prevents wrong-country geocodes (e.g. "SAN FELIPE" resolving to Costa Rica).
# DESIGN DECISION (Phase 1.5h): SCCSSAR operates entirely in Santa Clara County, CA.
_AGENCY_CITY: dict[str, str] = {
    # Standard abbreviations
    "SJPD": "San Jose, CA",
    "SPD":  "San Jose, CA",   # SCCSSAR sometimes sees SPD for SJPD
    "MPD":  "Milpitas, CA",
    "SCPD": "Santa Clara, CA",
    "CUPD": "Cupertino, CA",
    "SVPD": "Sunnyvale, CA",
    "MVPD": "Mountain View, CA",
    "PAPD": "Palo Alto, CA",
    "LGPD": "Los Gatos, CA",
    "CGPD": "Campbell, CA",
    "MHPD": "Morgan Hill, CA",
    "GPPD": "Gilroy, CA",
    "SJSU": "San Jose, CA",   # San Jose State University PD — campus is in San Jose
    "SCSO":  "Santa Clara County, CA",
    "SCSD":  "Santa Clara County, CA",
    "SCCSO": "Santa Clara County, CA",
    "CHP":  "California",
    # Full city names — v2 PDF form has a free-text Agency field;
    # officers sometimes type the city name instead of PD abbreviation.
    "MILPITAS":      "Milpitas, CA",
    "SAN JOSE":      "San Jose, CA",
    "SANTA CLARA":   "Santa Clara, CA",
    "CUPERTINO":     "Cupertino, CA",
    "SUNNYVALE":     "Sunnyvale, CA",
    "MOUNTAIN VIEW": "Mountain View, CA",
    "PALO ALTO":     "Palo Alto, CA",
    "LOS GATOS":     "Los Gatos, CA",
    "CAMPBELL":      "Campbell, CA",
    "MORGAN HILL":   "Morgan Hill, CA",
    "GILROY":        "Gilroy, CA",
    "LOS ALTOS":     "Los Altos, CA",
    "SARATOGA":      "Saratoga, CA",
}

# Agency display normalization — used in BOTH the primary and city-fallback
# Event Name reconstruction paths. Pre-fix the dict was defined inside the
# primary path's `if date_match and agency_match and lkp_match:` block, which
# meant the city-fallback path (intersection LKPs, city-only, etc.) silently
# never normalized the agency — producing event names like
# "2026-05-07 SJSU PD 5Th Street At St. John Street" (PR-fix-4, post-merge
# regression discovered 2026-05-08 on personal-dev SJSU smoke test).
#
# Pinned in test_main_regression.py — drift between this dict and the helper
# call sites would silently re-introduce the bug.
_AGENCY_DISPLAY: dict[str, str] = {
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
    # San Jose State University campus PD — strip the trailing "PD" so the
    # event name reads "YYYY-MM-DD SJSU <street>" (radio-traffic canonical
    # form). Added 2026-05-08 (PR #387) after the 2026-05-07 SJSU 5th
    # incident review where the un-normalized "SJSU PD" landed in the Slack
    # welcome and EB title.
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
    # SCCSO OCR-misread defenses (PR-D-2.5, 2026-05-10): handwritten "SCCSO"
    # OCRs unreliably as space-split or letter-confused variants. Live
    # confirmed on the 2026-05-10 Verde Vista form which produced
    # "SCC SLO" (the second "S" misread as "SL"). Canonicalize at lookup
    # time so the event name reads "YYYY-MM-DD SCCSO <street>" (one-token
    # agency, radio-traffic canonical form). Same pattern as SJSU PD
    # canonicalization (PR #387).
    "SCC SLO":                 "SCCSO",
    "SCC SO":                  "SCCSO",
    "SCC S.O.":                "SCCSO",
}


# ---------------------------------------------------------------------------
# DOB / age hint helpers — defensive recompute.
#
# Gemini emits "DOB: <date> (NN years old)" but its age computation is
# non-deterministic — same form across runs has produced 21 vs 20 (regression
# discovered 2026-05-09 SJSU re-run; DOB 2005-10-20, today 2026-05-09, true age
# 20). The DOB date itself is reliable; only the parenthesized hint drifts.
#
# `_rewrite_dob_age_hint()` is called from /ocr post-processing to recompute
# the hint from the DOB date + today (Pacific). All downstream consumers read
# from the corrected hint:
#   - textarea display (frontend renders the summary verbatim)
#   - WhatsApp section (also rendered from summary)
#   - Slack welcome MP line: frontend `_ebParseAgeFromDob()` at
#     index.html:1055 extracts the age from the hint and passes it as
#     `mp_age`; backend slack.py:181 renders `{age}yo`.
#
# Mirror in test_main_regression.py — must stay in sync.
# ---------------------------------------------------------------------------

# Hint regex mirrors the frontend extractor at index.html:1062 (\s+ between
# digits and "year", case-insensitive). Cap to 3 digits to bound input.
# Matches the parenthesized age hint on a DOB line in several common forms:
#   "(N years old)"  canonical (Gemini emits this per system prompt)
#   "(N year old)"
#   "(N yrs old)"    common shorthand
#   "(N yr old)"
#   "(N yo)"         pdf_extract.py historically emitted this; Gemini also
#                    occasionally drifts to this form despite prompt instruction
#   "(N y/o)"        common medical shorthand
#   "(N y.o.)" / "(N y.o)" — dotted variants
#   "(N)"            bare number, no unit — an officer writing their own
#                    arithmetic beside the DOB (#814). Accepting it is safe
#                    because a match is NOT sufficient: _rewrite_dob_age_hint
#                    then requires _compute_age_from_dob to parse a real date
#                    off the SAME line, so a parenthesized number with no date
#                    beside it is never rewritten. A 4-digit year cannot match:
#                    \d{1,3} must be followed immediately by ")".
# All variants normalize to the canonical "(N years old)" via
# `_rewrite_dob_age_hint()` so the dispatcher textarea is consistent
# regardless of upstream path or Gemini non-determinism.
_DOB_AGE_HINT_RE = re.compile(
    r"\((-?\d{1,3})(?:\s+(?:years?\s+old|yrs?\s+old|y\.?o\.?|y/o))?\)",
    re.IGNORECASE,
)
_DOB_LINE_RE = re.compile(r"^DOB:\s*(.+)$", re.MULTILINE)

# strptime tolerates unpadded numerics with %m/%d (so "10/20/2005" matches
# %m/%d/%Y). %B and %b are locale-sensitive; Cloud Run defaults to C locale
# where they parse English month names — fine for SCCSSAR's English-only forms.




def _rewrite_dob_age_hint(
    summary: str,
    today: datetime.date,
) -> tuple[str, tuple[int, int] | None]:
    """Recompute the parenthesized age hint on the 'DOB:' line of `summary`.

    Returns `(new_summary, (old_age, new_age))` when an age was actually
    corrected. Returns `(summary, None)` when no correction applies — no DOB
    line present, no parenthesized hint on the DOB line, date portion
    unparseable, or hint already matches the recomputed value.

    Idempotent: a second call with the same `today` is a no-op. A call with
    a later `today` may legitimately re-correct an aged value (e.g. a
    dispatcher re-uploads the same form a year later) — that is intentional.
    """
    dob_match = _DOB_LINE_RE.search(summary)
    if not dob_match:
        return summary, None
    dob_line = dob_match.group(1)
    hint_match = _DOB_AGE_HINT_RE.search(dob_line)
    if not hint_match:
        return summary, None
    try:
        old_age = int(hint_match.group(1))
    except ValueError:
        return summary, None
    new_age = _compute_age_from_dob(dob_line, today)
    if new_age is None:
        return summary, None
    # Preserve grammatical singular for age 1: "(1 year old)" not "(1 years old)".
    # The existing test_year_singular_form pins this for the Gemini-emitted form;
    # we extend it to all canonicalized cases for consistency.
    plural = "" if new_age == 1 else "s"
    new_hint = f"({new_age} year{plural} old)"
    # Short-circuit when the existing hint is BOTH age-correct AND canonical-
    # format. This preserves the "no rewrite when already correct" contract
    # tested by TestRewriteDobAgeHint.test_already_correct_is_silent, while
    # still rewriting "(N yo)" / "(N yrs)" / etc. to canonical even when the
    # age value itself happens to be correct.
    if hint_match.group(0) == new_hint:
        return summary, None
    abs_hint_start = dob_match.start(1) + hint_match.start()
    abs_hint_end = dob_match.start(1) + hint_match.end()
    new_summary = summary[:abs_hint_start] + new_hint + summary[abs_hint_end:]
    # Age-correction tuple is only emitted when age ACTUALLY changed. A
    # format-only canonicalization returns None so the event-log policy
    # ("only emit on actual corrections / failures") is preserved.
    correction = (old_age, new_age) if new_age != old_age else None
    return new_summary, correction


# LPB rows the pipeline could not answer. Both intake paths render the same
# token: pdf_extract.NOT_ANSWERED for a blank AcroForm checkbox pair, and the
# JPEG path's UNCERTAIN, which main.py collapses to the identical display
# string. One regex therefore covers both.
_LPB_UNANSWERED_RE = re.compile(
    r"^(Q\d+) - NOT ANSWERED \(flag for follow-up\) - (.+)$",
    re.MULTILINE,
)


def _unanswered_lpb_note(summary: str) -> str | None:
    """Event Log line naming every questionnaire item the form left blank.

    Issue #675. A blank row is not a dispatcher-correctable defect — they
    cannot recover an answer the officer never wrote, and at dispatch time
    they are not in a position to chase it (Bill, 2026-08-01). So the goal is
    VISIBILITY, not correction: name the gap in the Event Log and let the
    dispatcher decide whether it is worth a call back to the officer.

    Deliberately NOT a fix for the 2026-07-31 failure, which was different in
    kind: there Gemini fabricated a definite "No" for a blank row, so no
    NOT ANSWERED token exists to detect and nothing server-side can recover
    it. The prompt already forbids that guess twice ("If no X is present, the
    answer is UNCERTAIN" / "If NO box is checked for a question, output
    UNCERTAIN"), so that half is a model-compliance problem, not a missing
    rule. This helper covers the case where the pipeline correctly reports the
    gap — which the AcroForm path does deterministically.

    Frequency is why this is one line rather than a per-row entry: measured
    across the cached genai corpus, 51/69 forms have zero unanswered rows and
    17 of the remaining 18 have exactly one. Emitting per row would still be
    quiet, but a single grouped line keeps the Event Log scannable and holds
    the "noise makes real warnings invisible" policy.

    The question LABEL is truncated at the first em-dash so the detail tail of
    rows like "Q9 - ... - Mental health component — detail: <free text>" does
    not drag PII-bearing prose into the note; the detail already appears on
    the row itself. It is also cut at a "?" because Gemini sometimes echoes the
    raw form wording instead of the canonical label — the corpus run produced
    "Q8 (Speaks English? If no, UNKNOWN)", which leaks prompt-template text
    into a dispatcher-facing line. No canonical label contains a "?".

    Returns the Event Log line, or None when every row was answered — the
    event-log policy is "corrections and failures only, never silent
    successes", same contract as _rewrite_dob_age_hint above.

    Pinned in test_main_regression.py; mirrored verbatim in
    backend/migration_validation/apply_helpers.py for corpus validation.
    """
    if not summary:
        return None
    rows = _LPB_UNANSWERED_RE.findall(summary)
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


def _normalize_event_name_agency(raw_agency: str) -> str:
    """Look up the canonical agency abbreviation for the Event Name.

    Applies the "P. D." → "P.D." normalization (so "SAN JOSE P. D." matches
    the same key as "SAN JOSE P.D.") then does the dict lookup with both
    normalized and uppercased forms. Returns `raw_agency` unchanged if no
    match — preserves the dispatcher's original text rather than discarding
    it silently.

    Used by BOTH the primary (LKP-with-house-number) and city-fallback
    (intersection / city-only / landmark LKP) Event Name reconstruction
    paths in the OCR endpoint, so agency canonicalization is consistent
    regardless of which path runs.
    """
    if not raw_agency:
        return raw_agency
    normed = re.sub(r"\.\s+", ".", raw_agency.upper())
    return _AGENCY_DISPLAY.get(normed, _AGENCY_DISPLAY.get(raw_agency.upper(), raw_agency))


# Maximum length of a reconstructed Event Name (issue #672).
#
# _AGENCY_DISPLAY canonicalizes in-county agencies; an out-of-county mutual-aid
# agency written in full legal form has no entry and passes through verbatim.
# On 2026-07-31 that produced a 62-character name that flowed to the Everbridge
# title, the CalTopo map title, the D4H record and the Slack channel name — a
# team member renamed the channel by hand afterwards. A cap is the durable fix
# because it holds regardless of which agency appears; an abbreviation table
# only helps agencies we have already met, and mutual aid is by definition the
# case where we have not.
#
# 50 is chosen from measurement, not taste. Across the 40-form corpus real event
# names run median 28 / p90 36 / max 41 characters, so 50 leaves nine characters
# of headroom and truncates NOTHING that has ever been dispatched — while still
# catching the 62-char outlier that opened the issue.
#
# It also clears every downstream consumer with room to spare:
#   Slack channel name       75 effective (80 minus the "_HHMM" suffix)
#   D4H referenceDescription 104 effective on personal-dev (Zod max 100, plus
#                            11 for the stripped date, minus 7 for the "[abcd]"
#                            suffix); 111 elsewhere
#   Everbridge title         255 documented ("Type a name for the Notification
#                            up to 255 characters" — EB Notification Fields
#                            help, v26.5). The "SOSAR - " prefix puts us at ~58,
#                            or ~63 with the HHMM suffix: a 4x margin, so EB
#                            needs no guard of its own.
# Slack is the tightest, exactly as issue #672 predicted — though note that its
# other premise went stale: #676 moved the event name off `trackingNumber` and
# onto `referenceDescription`.
#
# Only ONE of these truncations is ever meant to fire: this one, at the source.
# The event name is an IDENTITY — it is how a dispatcher correlates one callout
# across the EB page, the Slack channel, the D4H record and the CalTopo map — so
# it must be the SAME string everywhere. Per-app truncation would give one
# incident four different names. The downstream caps are boundary guards for
# input that BYPASSES this function (the dispatcher-editable Event Name
# textarea, wired straight to D4H by #87), not a second opinion on length.
_EVENT_NAME_MAX_LEN = 50


def _cap_event_name(evt_date: str, evt_agency: str, evt_location: str) -> str:
    """Assemble `YYYY-MM-DD AGENCY LOCATION`, capped at _EVENT_NAME_MAX_LEN.

    Takes the three parts SEPARATELY rather than capping an assembled string,
    because after assembly there is no way to tell a multi-word agency
    ("SANTA CLARA COUNTY SHERIFF") from a multi-word street ("Verde Vista") —
    both are just space-separated tokens. The parts are only distinguishable
    here, at the five construction sites.

    Truncation order per issue #672: the LOCATION goes first. The date is
    fixed-width and the agency identifies who is asking, which is the part a
    responder needs on the radio.

    Location is trimmed on a WORD boundary so the result reads as a place
    rather than a fragment; a single over-long first word is hard-cut as the
    last resort. If the date and agency alone already exceed the cap — a
    pathological out-of-county agency name — the whole string is hard-cut, so
    the cap is a guarantee rather than a best effort.
    """
    full = f"{evt_date} {evt_agency} {evt_location}".strip()
    if len(full) <= _EVENT_NAME_MAX_LEN:
        return full

    prefix = f"{evt_date} {evt_agency}".strip()
    budget = _EVENT_NAME_MAX_LEN - len(prefix) - 1  # -1 for the joining space
    if budget <= 0:
        # Date + agency alone overflow. Nothing of the location can survive.
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


# Intersection-style LKP detector for Event Name reconstruction. Used in
# the city-fallback path to extract the FIRST street name from intersections
# like "5th Street at St. John Street" → "5th". Pre-fix the regex only
# matched "&" / "INTERSECTION", missing the common "<St> at <St>" pattern
# that produced 8-token garbage event names (PR-fix-4, 2026-05-08).
#
# Note: this is a SEPARATE concern from `_is_intersection_query()` (added in
# PR #390), which routes intersection LKPs to Google Maps for geocoding.
# Geocoding cares about whether Nominatim CAN resolve the address (only "&"
# matters there); event-name extraction cares about ANY two-street pattern.
# Keeping them separate lets each evolve at its own pace.
_EVENT_NAME_INTERSECTION_RE = re.compile(
    # `@` added 2026-05-08 (PR-fix-5) after the second SJSU re-run produced
    # `5th St. @ St. John St.` from the OCR — the `@` is a common handwritten
    # shorthand for "at" on call-out forms ("5th @ St John") and was missed
    # by the original `&|at|and` set, falling through to title-case which
    # produced `5Th St. @ St. John St.` in the event name.
    r"\s+(?:&|@|at|and)\s+|\bINTERSECTION\s+OF\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Address-component regexes — promoted from function-local to module-level
# (PR-D-1, 2026-05-10) so the dispatcher staging override endpoint can reuse
# them for streetname extraction without duplicating literals. Both event-name
# reconstruction inside ocr() and `_extract_streetname_from_address()` MUST
# share the same regex bodies — drift would silently produce different street
# names from the two code paths.
# ---------------------------------------------------------------------------

# Strip apartment/suite qualifiers from address strings before geocoding or
# streetname extraction. Examples:
#   "123 Main St, Apt 4, City, CA" → "123 Main St, City, CA"
#   "123 Main St, #2"              → "123 Main St"
# The substitution removes the qualifier token plus any preceding comma/space.
#
# CRITICAL: \b (word boundary) on both sides of the keyword alternation prevents
# substring matching inside street names. Without it, `Ste` (case-insensitive)
# matches inside "Hostetter" and `Apt` matches inside "Aptos", silently
# corrupting "2000 Hostetter Rd" → "2000 Ho Rd" and similar. Surfaced on a
# real 2026-05-18 SJPD callout (Hostetter Rd / event name truncated to "Ho").
# `#` is excluded from the \b path because it is itself a non-word character.
_APT_STRIP_RE = re.compile(
    r",?\s*(?:\b(?:Apt|Apartment|Unit|Ste|Suite)\b\.?|#)\s*#?\s*[\w-]+",
    re.IGNORECASE,
)

# Strip leading cardinal direction word (North/South/East/West and abbrevs).
_STREET_CARDINALS = re.compile(
    r"^(?:North|South|East|West|N\.?|S\.?|E\.?|W\.?)\s+",
    re.IGNORECASE,
)

# Strip trailing street-type token (Blvd/Dr/Ave/etc.) plus any trailing words.
# The `(?:\s.*)?$` suffix strips embedded city/state when no comma separates
# them — e.g., "SAMARITAN DR. SAN JOSE" → "SAMARITAN". Standard addresses
# where the type IS at end-of-string are unaffected.
_STREET_TYPES = re.compile(
    r"\s+(?:Blvd|Blvd\.|Boulevard|Dr|Dr\.|Drive|Ave|Ave\.|Avenue|St|St\.|Street|"
    r"Rd|Rd\.|Road|Ln|Ln\.|Lane|Way|Ct|Ct\.|Court|Pl|Pl\.|Place|"
    r"Pkwy|Pkwy\.|Parkway|Hwy|Hwy\.|Highway|Expy|Expressway|"
    r"Cir|Cir\.|Circle|Ter|Ter\.|Terrace|Trail|Trl)(?:\s.*)?$",
    re.IGNORECASE,
)


def _extract_streetname_from_address(address: str) -> str | None:
    """Extract a clean street name from a free-form address for Event Name use.

    Used by the dispatcher staging override endpoint (`/apply-staging-override`)
    to suggest an updated Event Name street when the dispatcher provides an
    address override. Mirrors the streetname-extraction logic in the OCR-time
    Event Name reconstruction inside ocr() (line ~2310 onward) so that an
    override-driven event name matches what would have been produced by OCR
    if the form had been right — KEEP THE TWO PATHS CONSISTENT.

    Internal-consistency note (Bill 2026-05-10, post-PR-D-2.5 retest):
    earlier in PR-D-2.5 I added a "first word only" reduction here to satisfy
    a one-word-streetname rule. Reverted because OCR-time still produces
    multi-word streetnames (e.g. "Verde Vista" from real production
    incidents). Keeping the override path and OCR path in sync beats radio
    brevity in this codebase — the dispatcher can manually edit the Event
    Name if a multi-word result looks awkward.

    Algorithm (apply in order):
      1. Strip leading whitespace.
      2. Take everything before the first comma — the assumed street portion.
      3. Strip apartment/suite qualifier via _APT_STRIP_RE.
      4. Strip leading house number (digits + space).
      5. Strip trailing street-type token via _STREET_TYPES.
      6. Strip leading cardinal direction via _STREET_CARDINALS.
      7. Return the result if non-empty; otherwise None.

    Pinned in test_main_regression.py:
      "100 Verde Vista Lane, Saratoga, CA"          → "Verde Vista"
      "1200 East Calaveras Blvd, Milpitas, CA"      → "Calaveras"
      "5th @ St. John, San Jose, CA"                → "5th"
      "10000 Saratoga Sunnyvale Rd, Saratoga, CA"   → "Saratoga Sunnyvale"
      "Garbage Park"                                → "Garbage Park"
      ""                                            → None
    """
    if not address or not address.strip():
        return None
    # Take portion before first comma (the street portion).
    street_portion = address.split(",", 1)[0].strip()
    if not street_portion:
        return None
    # Apt/suite strip + comma-cleanup.
    cleaned = _APT_STRIP_RE.sub("", street_portion).strip(" ,")
    # Strip leading house number (digits followed by whitespace).
    no_house_num = re.sub(r"^\d+[ \t]+", "", cleaned).strip()
    # Detect intersection-style input: extract the FIRST street name (matches
    # event-name reconstruction's intersection-handling at line ~2374).
    intersection_match = _EVENT_NAME_INTERSECTION_RE.search(no_house_num)
    if intersection_match:
        if intersection_match.group(0).strip().upper().startswith("INTERSECTION"):
            after_prefix = no_house_num[intersection_match.end():].strip()
            between_match = re.search(r"\s+(?:&|@|at|and)\s+", after_prefix, re.IGNORECASE)
            first = after_prefix[:between_match.start()].strip() if between_match else after_prefix.split(",")[0].strip()
        else:
            first = no_house_num[:intersection_match.start()].strip()
        no_house_num = first
    # Strip trailing street type, then leading cardinal.
    no_type = _STREET_TYPES.sub("", no_house_num).strip()
    no_cardinal = _STREET_CARDINALS.sub("", no_type).strip()
    return no_cardinal or None


def _parse_utm_string(utm_str: str) -> tuple[float, float] | None:
    """Parse a SAR-standard UTM string and convert to decimal lat/lng.

    SAR convention format: `<zone><zone-letter> <easting>E <northing>N`.
    Example: "10S 590309E 4142188N" → (37.42210, -121.97936).

    Why not pyproj: this codebase does not import pyproj; the dedicated `utm`
    package is much smaller and matches what other SAR tools use.

    Returns (lat, lng) on success, None on parse failure or any conversion
    error. Never raises — the caller will return HTTP 400 to the dispatcher
    with a generic "invalid UTM" message.

    Lazy import of `utm` so module load doesn't fail in environments that
    don't have it installed (e.g. local dev without `pip install utm==0.8.1`).
    Production Cloud Run installs it via requirements.txt.

    Pinned in test_main_regression.py:
      "10S 590309E 4142188N"  → lat ≈ 37.42210, lng ≈ -121.97936
      "10s 590309e 4142188n"  → same (case-insensitive)
      "garbage"               → None
      ""                      → None
    """
    if not utm_str or not utm_str.strip():
        return None
    try:
        # Match: zone (digits) + zone-letter (single alpha) + space + easting +
        # E suffix + space + northing + N suffix. Tolerant of extra spaces and
        # case variations.
        m = re.match(
            r"^\s*(\d{1,2})\s*([A-Za-z])\s+([\d.]+)\s*[Ee]\s+([\d.]+)\s*[Nn]\s*$",
            utm_str,
        )
        if not m:
            return None
        zone_num = int(m.group(1))
        zone_letter = m.group(2).upper()
        easting = float(m.group(3))
        northing = float(m.group(4))
        # Sanity ranges — UTM zone numbers are 1-60; easting is 100000-999999 typically;
        # northing is 0-10000000.
        if not (1 <= zone_num <= 60):
            return None
        if not (zone_letter.isalpha() and len(zone_letter) == 1):
            return None
        if not (0 < easting < 1_000_000):
            return None
        if not (0 <= northing <= 10_000_000):
            return None
        # Lazy import — utm package is in requirements.txt but may be missing locally.
        import utm as utm_pkg  # type: ignore
        lat, lng = utm_pkg.to_latlon(easting, northing, zone_num, zone_letter)
        return (float(lat), float(lng))
    except Exception:
        return None


# Decimal `lat, lng` pair. Mirrors the frontend override contract in
# index.html (~line 2240): comma required, surrounding parentheses and
# whitespace tolerated. The comma is what makes a single ambiguous number
# ("37.26940") a non-match.
_LATLNG_STAGING_RE = re.compile(
    r"^\(?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)?$"
)


def _parse_latlng_string(text: str) -> tuple[float, float] | None:
    """Parse a decimal `lat, lng` pair from free-form staging text.

    Same input shape `/apply-staging-override`'s `lat_lng` mode accepts, but
    that mode receives two already-separated floats from the frontend — there
    is no server-side text parser to reuse, so this is the first one.

    Deliberately STRICTER than the frontend on one point: at least one
    component must carry a decimal point. The frontend parses a labelled
    coordinate field where the dispatcher has already declared intent; Pass B
    parses free-form officer text where a bare integer pair is far more likely
    to be prose than a coordinate. A false positive here writes a staging
    marker into the ocean and hands every responder a link to it.

    Returns (lat, lng) on success, None on any parse failure. Never raises.

    Pinned in test_main_regression.py:
      "37.26940, -122.03674"   → (37.26940, -122.03674)
      "(37.26940, -122.03674)" → same (parentheses tolerated)
      "37.26940"               → None (no comma — ambiguous)
      "1, 2"                   → None (no decimal point)
      "10410, CA"              → None
    """
    if not text or not text.strip():
        return None
    m = _LATLNG_STAGING_RE.match(text.strip())
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


def _parse_coordinate_staging_text(text: str) -> tuple[float, float, str] | None:
    """Parse staging text written as a coordinate. Returns (lat, lng, utm_display).

    Issue #668. Wilderness and remote mutual-aid callouts routinely specify
    staging as a coordinate because there are no landmarks, addresses, or
    cross streets to reference (Bill, 2026-07-31). That is normal input, not a
    data-entry error — so Pass B must recognise it BEFORE handing the text to
    an address geocoder.

    `utm_display` is the dispatcher's original UTM string, or "" when the
    input was already decimal lat/lng. The caller needs that distinction:
    decimal input already works as a maps query and is left alone, while UTM
    must be rewritten (see _format_coordinate_staging_display).

    Lat/lng is tried first because it is the cheaper, more common shape and
    the two grammars cannot collide — `_parse_utm_string` requires the
    `<zone><letter> <easting>E <northing>N` structure.

    Returns None for anything that is not a coordinate, so the existing
    canonical-facility / normalize / geocode path runs unchanged.
    """
    if not text or not text.strip():
        return None
    raw = text.strip()
    latlng = _parse_latlng_string(raw)
    if latlng is not None:
        return (latlng[0], latlng[1], "")
    utm = _parse_utm_string(raw)
    if utm is not None:
        return (utm[0], utm[1], raw)
    return None


def _format_coordinate_staging_display(lat: float, lng: float, utm_str: str = "") -> str:
    """Render a staging coordinate as the ONE display string every path uses.

    Extracted 2026-08-01 (#668) from the two inline renderings inside
    `/apply-staging-override` so the OCR-time Pass B coordinate path cannot
    drift from the dispatcher-override path. Two paths producing two coordinate
    renderings is exactly the drift the cross-file literal pin policy exists to
    prevent, and here the consequence is silent: per the Locked Decision
    "Staging line text IS the responders' maps-link query", this string is what
    every responder's phone searches when they tap the Slack staging link.

    Decimal lat/lng LEADS because `maps.apple.com/?q=` and
    `google.com/maps/search/` are TEXT searches — a decimal pair resolves, a
    raw UTM string does not. Converting to decimal before the value becomes
    display text is what makes UTM safe; it is not a property of the link.

    The dispatcher's original UTM TRAILS after an em-dash so SAR-radio
    responders still see the standard form they were trained on (Bill
    operational ask 2026-05-10). The frontend's `stagingRec` regex
    (`^1\\.\\s+([^\\n—]+)` in index.html) trims at that em-dash, so the UTM tail
    never reaches the maps query — one line serves both audiences.

    Pinned in test_main_regression.py:
      (37.42210, -121.97936)                        → "37.42210, -121.97936"
      (37.42210, -121.97936, "10S 590309E 4142188N")
                        → "37.42210, -121.97936 — 10S 590309E 4142188N"
    """
    base = f"{lat:.5f}, {lng:.5f}"
    utm_display = utm_str.strip()
    return f"{base} — {utm_display}" if utm_display else base


# ---------------------------------------------------------------------------
# Nominatim geocoder — free OpenStreetMap geocoding, no API key required
# ---------------------------------------------------------------------------

async def _geocode_nominatim(address: str) -> tuple[float, float, str, str | None] | None:
    """
    Geocode an address using OpenStreetMap Nominatim (free, no key needed).

    Returns:
        (lat, lng, display_name, resolved_city) tuple, or None if geocoding fails.
        Never raises — failures are non-fatal; caller falls back gracefully.

    The fourth element is the locality Nominatim actually resolved to, kept
    structured (rather than only folded into display_name) so callers can run
    the city-consistency guard — see _reconcile_geocode_city(). It is None when
    Nominatim returns no locality at all, which the guard treats as "can't
    check" rather than as a mismatch.

    `countrycodes=us` (issue #667) is the BINDING half of the California
    anchor. The `, CA` suffix appended by the caller is only a string hint
    Nominatim may weigh, and `CA` is simultaneously the USPS code for
    California and the ISO 3166-1 code for CANADA — so on a query with nothing
    else to go on, Nominatim resolves the country. The parameter is the
    constraint; the suffix still disambiguates WITHIN the US and stays.

    Deliberately `us`, not California. Out-of-state mutual aid is rare but
    real (Bill, 2026-07-31), and a state-level assertion would reject exactly
    the callout class that surfaced this bug.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": address, "format": "json", "limit": 1,
                        "addressdetails": 1, "countrycodes": "us"},
                headers={"User-Agent": "SCCSSAR-Dispatch/1.5c (SAR operations, contact dispatcher@sccssar.org)"},
            )
        data = r.json()
        if data:
            lat = float(data[0]["lat"])
            lng = float(data[0]["lon"])
            # Extract neighborhood/suburb + city for a readable anchor description
            addr_detail = data[0].get("address", {})
            # village/municipality are included here (but not in the display string
            # above) because rural Santa Clara County addresses frequently carry no
            # city/town tag — without them the guard would silently skip the check
            # on exactly the remote LKPs where a wrong anchor costs the most.
            resolved_city = (
                addr_detail.get("city")
                or addr_detail.get("town")
                or addr_detail.get("village")
                or addr_detail.get("municipality")
            )
            parts = [
                addr_detail.get("neighbourhood") or addr_detail.get("suburb") or addr_detail.get("quarter"),
                addr_detail.get("city") or addr_detail.get("town"),
                addr_detail.get("state"),
            ]
            display = ", ".join(p for p in parts if p)
            return lat, lng, display or data[0].get("display_name", address), resolved_city
    except Exception as exc:
        logger.warning("Nominatim geocoding failed: %s", type(exc).__name__)
    return None


# `@` added 2026-05-08 (PR-fix-5) — handwritten `5th @ St. John` is a common
# officer shorthand for "at" on call-out forms. Like `&`, the `@` symbol is a
# hard blocker for Nominatim (which only resolves single-address queries), so
# detecting it lets us skip the wasted ~5s Nominatim timeout. The whitespace
# requirement (`\s@\s`) avoids false-triggering on email addresses.
_INTERSECTION_RE = re.compile(r"\s(?:&|@)\s|\bINTERSECTION\b", re.IGNORECASE)


def _is_intersection_query(address: str) -> bool:
    """Detect intersection-style geocode queries (e.g. "5th St & St John St, City, CA").

    Nominatim's free OSM-backed geocoder cannot resolve intersections — it returns
    no results. Google Maps Geocoding API does support them. Use this helper to
    route intersection queries directly to Google Maps and skip the wasted
    Nominatim attempt.

    Matches " & " or " @ " (with surrounding whitespace, so we don't false-trigger
    on "Smith & Co" street names or `user@example.com` email addresses) and the
    literal word "INTERSECTION" (officers sometimes write "INTERSECTION OF ...").

    Pinned in test_main_regression.py — drift here would silently re-introduce
    the slow path on intersection LKPs.
    """
    if not address:
        return False
    return bool(_INTERSECTION_RE.search(address))


# Canonical addresses for recurring SAR staging facilities (issue #646).
#
# DESIGN DECISION: these venue names are answered from a table instead of being
# sent to free-text geocoding at all. Same rationale as the Cardoza Park OCR
# normalization — a known recurring string that prompt engineering cannot fix,
# normalized deterministically server-side.
#
# Measured 2026-07-26 against both live providers:
#
#   "Richey Training Center, San Jose, CA"        Nominatim MISS → Google correct
#   "Richey Center, San Jose, CA"                 Nominatim MISS → Google returns
#       ROOFTOP-precision "2850 Quimby Rd" — 24 km wrong, and confident enough
#       that NO existing guard fires: it is under _MAX_STAGING_DIST_M, it has a
#       house number, and its precision is the highest Google reports. This
#       silent-wrong-answer case is what the table exists to prevent.
#   "Sheriff's Office, Richey Center, 11am., CA"  (verbatim 2026-07-26 intake
#       text) → California state centroid, caught by the 50 km guard, so the
#       officer's real staging never appeared anywhere.
#
# EVERY phrasing misses Nominatim, so the outcome is decided entirely by the
# Google fallback's willingness to guess. The word "Training" is load-bearing
# for that guess — dropping it flips a correct answer to a wrong one with no
# change in reported confidence.
#
# Both canonical targets were verified the same day as ROOFTOP on Google AND
# exact on Nominatim, so they resolve on the normal path with no dependency on
# which provider answers.
#
# ORDERING IS SIGNIFICANT — first match wins. The real 2026-07-26 string contains
# BOTH "Sheriff's Office" and "Richey Center", and the team staged at Richey. The
# specific named facility must beat the generic agency descriptor.
#
# Bare "SO" is deliberately NOT a pattern (Bill, 2026-07-26): intake text is
# frequently ALL CAPS, so a \bSO\b match would fire inside ordinary prose
# ("...PARK SO WE CAN STAGE") and silently rewrite the officer's staging text.
#
# Second real-incident occurrence — 2026-07-18 (LACSO La Verne) needed the same
# manual mid-intake correction from "Richey Training Center" to "155 W Hedding St".
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
    """Match staging text against the known-facility table (issue #646).

    Returns `(facility_key, canonical_address)` on a hit, else None. The key is
    a fixed identifier safe to log; the raw staging text is not (PII).

    First match wins — see _CANONICAL_STAGING_FACILITIES for why the ordering
    is load-bearing.
    """
    if not addr:
        return None
    for key, pattern, canonical in _CANONICAL_STAGING_FACILITIES:
        if re.search(pattern, addr, re.IGNORECASE):
            return (key, canonical)
    return None


def _normalize_staging_geocode_query(addr: str, city_context: str | None) -> str:
    """Apply LKP-style geocoder hygiene to a staging address before Pass B geocode.

    Officer-written staging text frequently arrives without city/state context
    (e.g. "ALMA @ 10TH" — the officer knows what city he's in, but Nominatim
    doesn't). Without anchoring, Nominatim resolves bare street names to
    plausible-looking but wrong-country matches: the 2026-05-09 SJSU smoke test
    on SCCSSAR-dev geocoded "ALMA @ 10TH" to lat=49.26318, lng=-123.18535 —
    Vancouver, Canada. On a real incident the CP marker would land in another
    country.

    Mirrors the LKP/Residence CA-append rule (Locked Decision: Geocoding → CA
    append). City context comes from the agency lookup or the Residence field
    — same priority cascade the LKP geocoder uses. Both rules are idempotent:
    if "CA" or a comma-city is already present the input passes through.

    Known SAR staging facilities (issue #646) short-circuit this entirely: their
    canonical address is returned as-is, because free-text geocoding of the venue
    name is precisely what fails. Applying to both call sites — Pass B and
    /apply-staging-override — is why the check lives here rather than at either
    one. See _CANONICAL_STAGING_FACILITIES.

    Returns the (possibly enriched) query string. Never returns empty when given
    non-empty input.
    """
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


async def _geocode_lkp_smart(address: str) -> tuple[float, float, str, str | None] | None:
    """LKP geocoder that routes intersections directly to Google Maps.

    Why: Nominatim cannot geocode intersections like "5th St & St John St" — it
    returns None after the full ~5s timeout. Routing intersections straight to
    Google Maps saves the wait AND uses the only provider that can resolve them.

    Non-intersection addresses keep the standard Nominatim path (faster, free,
    no API quota usage). The existing Google Maps fallback at the call site still
    runs for non-intersection queries that Nominatim fails on (e.g. misspellings).

    Without `_GOOGLE_MAPS_API_KEY`, intersection queries return None immediately
    with a clear log line — the LKP-geocode-failure WARNING fires downstream.

    Returns Nominatim-shape 4-tuple (lat, lng, display, resolved_city) for
    compatibility with the existing `geo` consumer, or None on failure.

    First surfaced 2026-05-08 personal-dev smoke test of the 2026-05-07 SJSU
    incident (LKP "5th St & St John St" + no Google Maps key on personal-dev).
    """
    if not address:
        return None

    if not _is_intersection_query(address):
        # Standard street address — Nominatim is the right primary.
        return await _geocode_nominatim(address)

    # Intersection — skip Nominatim, go straight to Google Maps.
    if not _GOOGLE_MAPS_API_KEY:
        logger.warning("LKP intersection detected but no Google Maps API key — geocode will fail")
        return None

    logger.info("LKP intersection detected — routing directly to Google Maps")
    gm = await _geocode_google_maps(address)
    if not gm:
        logger.warning("Google Maps geocoding failed for intersection LKP")
        return None

    gm_lat, gm_lng, gm_display, gm_formatted, _ = gm
    # House-number consistency check — for intersections, neither has a house
    # number so this is trivially True. Kept here defensively in case a future
    # OCR path produces an intersection-with-house-number query.
    if not _house_number_consistent(address, gm_formatted):
        logger.warning("Google Maps changed house number on intersection — rejected")
        return None

    logger.info("Google Maps geocoded LKP intersection")
    return (gm_lat, gm_lng, gm_display, _extract_query_city(gm_formatted))


async def _geocode_google_maps(address: str) -> tuple[float, float, str, str, str] | None:
    """
    Geocode an address using Google Maps Geocoding API (fallback when Nominatim fails).

    Google Maps has significantly better spelling-correction tolerance than Nominatim —
    it resolves misspelled street names that Nominatim rejects entirely (e.g. the officer
    writing "TRADEN DR" instead of "TRADAN DR" on a call-out form).

    Returns (lat, lng, neighborhood, formatted_address) or None on failure.
    The fourth element, formatted_address, is the canonical Google-verified address string;
    it is used by _street_correction_note() to detect and log spelling corrections.

    No-ops and returns None immediately if _GOOGLE_MAPS_API_KEY is empty.

    DELIBERATELY NOT country-bound, unlike _geocode_nominatim (#667). This
    asymmetry is a decision, not an oversight — do not "fix" it for symmetry
    without re-running the spike. Measured against live Google 2026-07-31:

      - Google never left the US on any probe. `, CA` alone is sufficient here
        in a way it is not for Nominatim, so `components=country:US` fixes
        nothing Google was doing wrong.
      - It actively costs diagnostic signal. `"Treatment Facility, CA"` goes
        from `partial_match=True` / APPROXIMATE / "California, USA" — visibly
        a non-answer — to `partial_match=True` / ROOFTOP at a specific street
        address 300 km from the search area. Same wrongness, dressed as top
        precision, which is the failure shape the canonical-staging Locked
        Decision warns about.
      - On `"Community Center, CA"` the filter flipped `partial_match` from
        True to False, erasing the one "I guessed" signal Google gives us.

    `partial_match` is returned on every response and currently unread —
    issue #680, filed off this spike.
    """
    if not _GOOGLE_MAPS_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": address, "key": _GOOGLE_MAPS_API_KEY},
            )
        data = r.json()
        if data.get("status") == "OK" and data.get("results"):
            result = data["results"][0]
            loc = result["geometry"]["location"]
            lat = float(loc["lat"])
            lng = float(loc["lng"])
            formatted_address = result.get("formatted_address", address)
            # Build neighborhood display string (same format as Nominatim helper)
            comp = {
                c["types"][0]: c["long_name"]
                for c in result.get("address_components", [])
                if c.get("types")
            }
            display_parts = [
                comp.get("neighborhood") or comp.get("sublocality_level_1"),
                comp.get("locality"),
                comp.get("administrative_area_level_1"),
            ]
            display = ", ".join(p for p in display_parts if p)
            return (lat, lng, display or formatted_address, formatted_address,
                    result["geometry"].get("location_type", ""))
    except Exception as exc:
        logger.warning("Google Maps geocoding failed: %s", type(exc).__name__)
    return None


_HOUSE_NUMBER_PREFIX_RE = re.compile(r"^\d+\b")


def _correction_is_plausible_street(input_first: str, corrected_first: str) -> bool:
    """Reject a "correction" that is not a street address at all.

    LIVE REGRESSION, personal-dev 2026-08-01 (Humboldt run 2). Google was asked
    about "1000 E Childs Ave" and returned a formatted_address whose first
    component was the bare city, "Livingston". _street_correction_note had NO
    validity check — any difference in the normalised first component counted
    as a correction — so "E Childs Ave" was replaced by "Livingston"
    THROUGHOUT the summary, producing staging entries reading
    "1440 Livingston, Livingston, CA".

    That is worse than a wrong spelling: a house number followed by a city name
    is not navigable, and by the Locked Decision "Staging line text IS the
    responders' maps-link query" it is exactly what a responder's phone
    searches for. It also silently corrupted an address that had been CORRECT
    — the CalTopo marker, built before the correction runs, still read
    "1100 E Childs Ave" while the dispatcher textarea read "1440 Livingston".

    Two rejections, both about losing street-ness:

      * house number present on the way in, gone on the way out — Google
        resolved to something coarser than a street;
      * street type present on the way in, and the result has neither a street
        type nor a house number to justify it ("Winton Way" -> "Livingston").

    Deliberately permissive otherwise. Corrections that ADD a cardinal or
    expand a type are the common legitimate case and must still pass, and a
    genuine misspelling fix ("TRADEN DR" -> "Tradan Dr") keeps both signals.
    """
    in_house = bool(_HOUSE_NUMBER_PREFIX_RE.match(input_first))
    out_house = bool(_HOUSE_NUMBER_PREFIX_RE.match(corrected_first))
    if in_house and not out_house:
        return False
    in_type = bool(_STREET_TYPES.search(input_first))
    out_type = bool(_STREET_TYPES.search(corrected_first))
    if in_type and not out_type and not out_house:
        return False
    return True


def _street_correction_note(
    input_addr: str, gm_formatted: str, label: str = "Address"
) -> tuple[str | None, str | None]:
    """
    Detect whether Google Maps corrected the street name in input_addr.

    Compares only the first comma-delimited component of each address (house number +
    street name), so city/state additions that we appended to the query do not trigger
    a false-positive correction.  Both strings are normalized to lowercase alphanumeric
    before comparison so "TRADEN DR." and "Tradan Dr" are correctly flagged as different.

    Returns the corrected street string from gm_formatted if a correction is detected,
    or None if the street names match (no correction needed).

    Example:
      input_addr  = "1100 TRADEN DR, SAN JOSE, CA"
      gm_formatted = "1100 Tradan Dr, San Jose, CA 95124"
      → returns "1100 Tradan Dr"

      input_addr  = "1100 TRADAN DR"     (correct, city omitted by officer)
      gm_formatted = "1100 Tradan Dr, San Jose, CA 95124"
      → returns None  (street names match after normalization)
    """
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.split(",")[0].lower())

    if _norm(input_addr) == _norm(gm_formatted):
        return None, None

    input_first = input_addr.split(",")[0].strip()
    corrected_first = gm_formatted.split(",")[0].strip()
    if not _correction_is_plausible_street(input_first, corrected_first):
        # PII policy: the addresses themselves never reach the log (core
        # guarantee #3) — only the shape that made this implausible.
        logger.warning(
            "Street correction REJECTED as implausible | had_house_number=%s "
            "correction_has_house_number=%s had_street_type=%s "
            "correction_has_street_type=%s",
            bool(_HOUSE_NUMBER_PREFIX_RE.match(input_first)),
            bool(_HOUSE_NUMBER_PREFIX_RE.match(corrected_first)),
            bool(_STREET_TYPES.search(input_first)),
            bool(_STREET_TYPES.search(corrected_first)),
        )
        # Dispatcher-facing note (Bill, 2026-08-01). Rejecting the correction
        # means we KEEP the officer's original text, so without this the only
        # trace is a server log the dispatcher never sees — and a city-only
        # answer from Google is itself a signal the address may not exist.
        # Event Log entries are dispatcher-facing summary content and already
        # quote addresses; the "no PII" rule governs logger.* calls, not this.
        return None, (
            f"WARNING: {label} could not be verified: \"{input_first}\" — the map "
            f"service returned \"{corrected_first}\", which is not a street address. "
            f"Kept the address as written; confirm it with the officer."
        )
    return corrected_first, None


def _apply_street_correction_to_summary(
    summary: str, input_first_component: str, corrected_first_component: str
) -> str:
    """
    Propagate a Google Maps street spelling correction throughout the summary text.

    Called immediately after _street_correction_note() detects a correction so that all
    downstream processing — Event Name reconstruction, the Gemini staging call, CalTopo
    marker labels, and the dispatcher textarea — sees the verified spelling rather than
    the officer's misspelled original.

    Only the street-name portion (after stripping the leading house number) is replaced, so
    house numbers are never affected.  Matching is case-insensitive and handles an optional
    trailing period (e.g. "TRADEN DR." and "TRADEN DR" both match).  The replacement is
    idempotent: if the corrected spelling is already present (e.g. a previous correction
    fixed the same address), the pattern won't match and summary is returned unchanged.

    Args:
        summary:                  the current summary string
        input_first_component:    first comma-delimited component of the original address
                                  (e.g. "1100 TRADEN DR.")
        corrected_first_component: first comma-delimited component of Google's
                                  formatted_address (e.g. "1100 Tradan Dr")

    Returns:
        summary with the misspelled street replaced by the corrected street throughout.
    """
    # Strip leading house number (digits + whitespace) to isolate the street name + type
    misspelled_street = re.sub(r"^\d+\s+", "", input_first_component).strip()
    # Strip the CORRECTED side's house number only when the input had one.
    #
    # When both sides carry a number the replacement is street-name-only, so the
    # officer's original number survives in the summary text — that is the whole
    # point of stripping. But when the input has NO number and Google's answer
    # adds one, stripping discards it and leaves a street-only string.
    #
    # Live regression, personal-dev 2026-08-01: the correction
    # "Livingston Community Park" -> "600 B St" rendered as "B St, Livingston"
    # in the dispatcher's staging list. Same class as "1440 Livingston" — by the
    # Locked Decision "Staging line text IS the responders' maps-link query"
    # that is what a phone searches for, and a street with no number is
    # unnavigable. Park-to-address is the common shape here: parks arrive
    # without a house number by construction.
    if _HOUSE_NUMBER_PREFIX_RE.match(input_first_component.strip()):
        corrected_street = re.sub(r"^\d+\s+", "", corrected_first_component).strip()
    else:
        corrected_street = corrected_first_component.strip()

    if not misspelled_street or not corrected_street:
        return summary

    # Guard: if they normalise the same way there is nothing to replace
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    if _norm(misspelled_street) == _norm(corrected_street):
        return summary

    # Strip trailing punctuation/whitespace from the misspelled portion so the pattern
    # matches both "TRADEN DR." and "TRADEN DR" with the single suffix "\.?"
    misspelled_base = re.sub(r"[.\s]+$", "", misspelled_street)

    # The docstring's idempotency claim only holds when the corrected form does
    # NOT contain the misspelled form. It usually does: the most common
    # correction Google makes is ADDING a cardinal ("Winton Way" ->
    # "N Winton Way") or a street type. Re-running such a correction re-matches
    # INSIDE its own output and double-prefixes.
    #
    # Observed live on personal-dev 2026-08-01: two queued corrections
    # ("1100 Winton Way" and "1000 Winton Way") both reduce to the same
    # street-level replacement, the second pass re-matched, and the dispatcher
    # got "1100 N N Winton Way" / "1000 N N Winton Way" in the staging list.
    # That string is navigable-looking but wrong, and by the Locked Decision
    # "Staging line text IS the responders' maps-link query" it is exactly what
    # a phone would search for.
    #
    # Anchor the pattern so an already-corrected occurrence cannot match again.
    # Both affixes are literals, so the lookbehind stays fixed-width.
    lower_base, lower_corrected = misspelled_base.lower(), corrected_street.lower()
    guard = ""
    trailer = ""
    if lower_corrected.endswith(lower_base) and lower_corrected != lower_base:
        # Prefix added ("N " + "Winton Way") — do not match after that prefix.
        guard = f"(?<!{re.escape(corrected_street[:-len(misspelled_base)])})"
    elif lower_corrected.startswith(lower_base):
        # Suffix added ("Winton" + " Way") — do not match before that suffix.
        trailer = f"(?!{re.escape(corrected_street[len(misspelled_base):])})"

    pattern = re.compile(guard + re.escape(misspelled_base) + r"\.?" + trailer, re.IGNORECASE)
    return pattern.sub(corrected_street, summary)


# Canonical expanded forms for common street-type and direction abbreviations.
# Used by _is_substantive_street_correction() to distinguish real misspelling
# fixes (TRADEN → Tradan) from abbreviation-only normalisations (EAST → E,
# Boulevard → Blvd) that should NOT appear in the dispatcher Event Log.
_STREET_ABBREV_EXPAND: dict[str, str] = {
    # Cardinal directions
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
    # Street types
    "blvd": "boulevard",
    "st": "street",
    "dr": "drive",
    "ave": "avenue",
    "rd": "road",
    "ln": "lane",
    "ct": "court",
    "pl": "place",
    "hwy": "highway",
    "fwy": "freeway",
    "pkwy": "parkway",
    "expy": "expressway",
    "cir": "circle",
    "ter": "terrace",
    "trl": "trail",
    "sq": "square",
    "pt": "point",
}


def _is_substantive_street_correction(
    input_addr_part: str, corrected_addr_part: str
) -> bool:
    """
    Return True only when Google Maps changed an actual misspelling (e.g. TRADEN → Tradan),
    not when it merely normalised abbreviations (e.g. EAST CALAVERAS BLVD → E Calaveras Blvd).

    Abbreviation-only changes — cardinal directions (EAST → E), street types
    (Boulevard → Blvd, Street → St), and pure case shifts — are not officer errors
    and should NOT appear in the dispatcher Event Log or trigger summary replacements.

    Both inputs are the first comma-delimited component of their respective addresses
    (house number + street name, city/state already stripped).
    """
    def _expanded_words(s: str) -> frozenset:
        # Strip leading house number (digits + space)
        bare = re.sub(r"^\d+\s+", "", s).strip()
        # Tokenise on whitespace and periods, lowercase
        tokens = [t for t in re.split(r"[\s.]+", bare.lower()) if t]
        # Expand each token to its canonical long form (keeps unknown tokens as-is)
        return frozenset(_STREET_ABBREV_EXPAND.get(t, t) for t in tokens)

    return _expanded_words(input_addr_part) != _expanded_words(corrected_addr_part)


def _house_number_consistent(query_addr: str, gm_formatted_addr: str) -> bool:
    """
    Return True if Google Maps did NOT change the leading house number.

    When Google Maps changes the house number (e.g. "300 MORETTE LANE" → "447 Great Mall Dr"),
    it geocoded a different nearby address rather than correcting a street spelling.
    Such results must be rejected — geo stays None and the LKP-geocoding-failure WARNING
    fires instead of silently anchoring staging/CalTopo to the wrong location.

    Returns True (consistent / allow through) when:
    - Either address lacks a leading house number (parks, landmarks — can't check)
    - Both have house numbers AND they match

    # DESIGN DECISION (do not revert without team discussion): guard added in PR #215 to
    # fix Form 9 regression where Google Maps returned "447 Great Mall Dr" for
    # "300 MORETTE LANE MILPITAS" — different house number, completely different address.
    # _is_substantive_street_correction() does not catch this because it strips digits
    # before comparing word sets. House-number check must be separate and run first.
    """
    def _leading_num(addr: str) -> str | None:
        m = re.match(r"^(\d+)\b", addr.split(",")[0].strip())
        return m.group(1) if m else None

    q_num = _leading_num(query_addr)
    r_num = _leading_num(gm_formatted_addr)
    if q_num and r_num:
        return q_num == r_num
    return True  # Can't check (no house number in one or both) — allow through


# ---------------------------------------------------------------------------
# City-consistency guard (issue #604) — sibling of _house_number_consistent()
# ---------------------------------------------------------------------------

# Place names that geocoders resolve to a parent city. Officers write these in
# the city slot; the geocoder answers with the incorporating city, and without
# the alias the guard would fire a mismatch on every dispatch to one of them.
# Per the Event log policy (Locked Decision: Output Format), noise makes real
# warnings invisible — so this list exists to keep the WARNING meaningful, not
# to make the check stricter. Keys and values are already normalised form.
_CITY_ALIASES: dict[str, str] = {
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

# A component that is only the state, optionally trailed by a ZIP ("CA 95051").
_STATE_COMPONENT_RE = re.compile(r"^(?:CA|California)(?:\s+\d{5}(?:-\d{4})?)?$", re.IGNORECASE)


def _normalize_city_name(name: str | None) -> str | None:
    """Fold a city name to a comparable key: accent-stripped, lowercase, alias-resolved.

    Accent folding is load-bearing, not cosmetic: OSM tags San Jose as "San José",
    so Nominatim returns the accented form for the single most common dispatch
    city this team has. Without folding, the guard would fire a false mismatch on
    a large fraction of all real callouts and be switched off within a week.
    """
    if not name:
        return None
    # str() coercion: `name` may come straight from Nominatim's JSON, where a
    # locality field is not guaranteed to decode as a string. unicodedata.normalize
    # raises TypeError on a non-str, and this runs inside /ocr — an upstream JSON
    # quirk must not take down form processing.
    decomposed = unicodedata.normalize("NFKD", str(name))
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9 ]", " ", ascii_only.lower())
    cleaned = re.sub(r"^(?:city|town)\s+of\s+", "", cleaned.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    return _CITY_ALIASES.get(cleaned, cleaned)


def _extract_query_city(address: str | None) -> str | None:
    """Best-effort city component of a comma-delimited address. None when undeterminable.

    Handles both the query we send ("600 Parkview Dr, Santa Clara, CA 95051") and
    the Google Maps formatted_address we get back (same shape), so one extractor
    serves both sides of the comparison.

    The city is the component immediately BEFORE the state — not simply the
    second component. Geocode queries are not normalised to a fixed three-part
    shape: a landmark prefix ("GOOD SAM HOSPITAL, 2000 SAMARITAN DR, SAN JOSE,
    CA") or a unit qualifier outside `_APT_STRIP_RE`'s vocabulary ("123 Main St,
    Space 45, San Jose, CA" — routine at mobile-home parks) both push the city
    past position 1. Reading position 1 in those cases yields a *street line* as
    the "city", which then mismatches a perfectly correct geocode and prints a
    confidently-wrong WARNING at the dispatcher. On this project a false alarm
    costs trust on the same axis this guard is meant to protect.

    Anchoring on the state is safe: every LKP/Residence query reaching a geocoder
    has passed the CA-append rule, and Google Maps' formatted_address always
    carries the state for US results.

    Returns None — meaning "can't check" — when:
    - no state component is present
    - the state sits at index <= 1, leaving the street itself as the only
      candidate ("300 MORETTE LANE MILPITAS, CA" welds the city onto the street
      component; not recoverable, so skip rather than guess)
    - the candidate still looks like a street line (leading house number)
    - the component names a *county* rather than a city. `_AGENCY_CITY` maps the
      sheriff's office to "Santa Clara County, CA"; comparing that against a
      resolved city would flag every county-agency callout as a mismatch.
    """
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


def _city_consistent(query_addr: str | None, resolved_city: str | None) -> bool:
    """Return True unless the query names a city and the geocoder resolved a different one.

    Deliberately mirrors _house_number_consistent()'s contract: when either side
    is unknown the answer is True (allow through). Only a positive, confident
    disagreement returns False.
    """
    requested = _normalize_city_name(_extract_query_city(query_addr))
    resolved = _normalize_city_name(resolved_city)
    if not requested or not resolved:
        return True  # Can't check — allow through
    return requested == resolved


def _find_stale_locality(
    superseded_locality: str | None,
    current_locality: str | None,
    surfaces: tuple[tuple[str, str | None], ...],
) -> tuple[str | None, str | None]:
    """Return (superseded city, which surface it appeared on) — or (None, None).

    `surfaces` is an ordered sequence of (label, text) pairs. The label is
    reported back so the 422 can tell the dispatcher WHERE the stale name is,
    which changes the remedy: a name in the notification text is fixed by
    editing the text, a name in the staging address is fixed by overriding
    staging. A warning that does not say which is a warning they have to
    re-derive under time pressure.

    The other half of the 2026-07-24 wrong-city dispatch — and the half that
    actually reached responders. The LKP geocoded to Milpitas; the dispatcher
    spotted the bad staging list, overrode staging to Santa Clara, hand-edited
    the Everbridge text, and **missed one remaining "Milpitas"**. The
    notification went out and responders started driving to the wrong city. The
    SAR Unit Leader phoned to ask what had happened; recovery took a hand-issued
    follow-up EB notification and a hand-posted replacement pinned Slack message.

    The comparison deliberately needs no override bookkeeping: it asks the
    direct operational question — "does the outbound message still name a city
    that is not where we are sending people?" — by comparing the locality the
    LKP resolved to against the locality its own address specified, then
    scanning each outbound surface for the resolved (suspect) name. That covers
    the override case and any other route to the same divergence.

    `staging_address` is one of those SURFACES, never either side of the
    comparison. The distinction is the whole false-positive story:

        as a surface  -> "the staging address names the known-bad city"  (right)
        as a compare  -> "staging is in a different city than the LKP"   (fires
                          on every legitimate cross-city override)

    As a comparison it was also a silent no-op: production staging strings
    ("55 North 7th Street, San Jose") carry no state component, so
    _extract_query_city() returns None and the gate never armed at all. Pinned
    by TestStaleLocalityGateOrdering::
    test_gate_is_armed_only_by_the_604_suspect_flag.

    Matching folds accents and case (a dispatcher types "San Jose"; OSM says
    "San José") and requires word boundaries, so "Santa Clara" does not match
    inside "Santa Clarita".

    Returns the superseded name as given (for display) plus its surface label,
    or (None, None) when: either locality is unknown, the two agree, or the
    superseded name does not appear anywhere. Never raises.
    """
    superseded_key = _normalize_city_name(superseded_locality)
    current_key = _normalize_city_name(current_locality)
    if not superseded_key or not current_key or superseded_key == current_key:
        return None, None

    def _fold(s: str) -> str:
        decomposed = unicodedata.normalize("NFKD", s)
        return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()

    pattern = re.compile(rf"\b{re.escape(_fold(str(superseded_locality)))}\b")
    for label, text in surfaces:
        if text and pattern.search(_fold(text)):
            return str(superseded_locality), label
    return None, None


async def _reconcile_geocode_city(
    query: str,
    geo: tuple[float, float, str, str | None],
    label: str,
) -> tuple[tuple[float, float, str, str | None], str | None]:
    """Guard a *successful* geocode against a confidently-wrong city (issue #604).

    Nominatim answers a query for a street that does not exist in the requested
    city by matching the same-named street in a different city — confidently,
    with no error, ignoring both the city and the ZIP in the query. On 2026-07-24
    this anchored a real callout ~13 km outside the search area: all 12 staging
    candidates and the CalTopo LKP/Residence markers followed the bad coordinate,
    and responders were paged to the wrong city.

    Neither existing guard caught it. `_house_number_consistent()` passed, because
    the wrong city also has that house number. The Google Maps fallback — the
    designated spelling-correction path — never ran, because the cascade only
    fires on geocoder *failure*, never on a confident wrong answer. This helper
    closes exactly that gap: it re-runs Google Maps on a city mismatch, since
    Google's spelling tolerance resolves the street-name variant ("Parkview" vs
    "Park View") that sent Nominatim to the wrong city in the first place.

    DESIGN DECISION (Bill, 2026-07-24): when neither geocoder produces a
    city-consistent result, the coordinate is KEPT, not discarded. Rejecting it
    would cost the entire staging list and leave _build_seed_feature() with
    neither an LKP nor a Residence seed — on every false positive, of which the
    plausible sources (unincorporated county, neighborhood-as-city, mutual aid
    outside the county) are all routine. The dispatcher-facing WARNING is the
    actual fix: on 2026-07-24 the dispatcher saw the staging list was wrong but
    had no way to learn why, and reverted to searching Google Maps by hand.

    Returns (geo, event_log_entry_or_None, suspect) — geo unchanged unless
    Google Maps produced a better, city-consistent answer. `suspect` is True
    only when the mismatch could NOT be resolved, i.e. the coordinate is being
    kept despite disagreeing with the requested city. It arms the #605
    stale-locality gate at dispatch time; a mismatch that Google Maps fixed is
    not suspect, because the anchor is then correct. Never raises.

    NOTE: city names are returned in the event-log entry (dispatcher-facing,
    alongside the address in the summary) and never passed to logger.* — the
    "No PII in logs" guarantee treats a locality as address data.
    """
    if not query or not geo:
        return geo, None, False
    if _city_consistent(query, geo[3]):
        return geo, None, False

    requested = _extract_query_city(query)
    resolved = geo[3]
    logger.warning("%s geocode resolved a different city than requested — retrying via Google Maps", label)

    if _GOOGLE_MAPS_API_KEY:
        gm = await _geocode_google_maps(query)
        if gm:
            gm_lat, gm_lng, gm_display, gm_formatted, _ = gm
            gm_city = _extract_query_city(gm_formatted)
            # Both guards must hold: a retry that lands in the right city but at a
            # different house number is the PR #215 Form-9 failure wearing a hat.
            if _house_number_consistent(query, gm_formatted) and _city_consistent(query, gm_city):
                logger.info("%s city mismatch resolved via Google Maps", label)
                return (gm_lat, gm_lng, gm_display, gm_city), (
                    f"{label} re-geocoded: the first map lookup resolved to {resolved}, "
                    f"but the address specifies {requested} — corrected to {requested} "
                    f"(Google Maps verified). Verify the street name with the officer."
                ), False
        logger.warning("%s city mismatch NOT resolved by Google Maps — anchoring with warning", label)
    else:
        logger.warning("%s city mismatch and no Google Maps key — anchoring with warning", label)

    return geo, (
        f"WARNING: {label} may be geocoded to the wrong city. The address specifies "
        f"{requested}, but the map lookup resolved to {resolved} — staging "
        f"recommendations and the CalTopo marker are anchored there. Confirm the street "
        f"name with the officer before dispatching; a street written as one word "
        f"(\"Parkview\") when the real name is two (\"Park View\") is the usual cause."
    ), True


# ---------------------------------------------------------------------------
# Haversine distance — stdlib only, no new dependencies
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in meters between two WGS-84 lat/lng points."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Staging distance sanity guard
# ---------------------------------------------------------------------------

# Staging is near the missing person by definition. 50 km is far beyond any
# plausible staging area (Santa Clara County is ~65 km across; the Koester 75th
# percentile for the most far-ranging subject category is under 10 km) while
# still catching a state- or county-centroid geocode by a wide margin.
#
# MUTUAL AID IS SAFE: this is LKP-relative, not home-county-relative. On a
# mutual-aid callout the team stages far from home but close to THAT incident's
# LKP, so the distance stays small. It fires only when staging is far from the
# missing person, which is never legitimate. Confirmed with Bill 2026-07-25.
_MAX_STAGING_DIST_M = 50_000

# Widened retry radius, used ONLY when the 1200 m staging lookup returns zero
# candidates (#838). Zero at 1200 m does NOT imply a remote LKP: measured at
# 3101 Alexis Dr — suburban Palo Alto — the provider returns 28 features inside
# 1200 m and NOT ONE carries a house number, so every one is dropped by the
# PASS 2 leading-digit predicate. The addressed POIs exist further out: 21 of
# them at exactly this radius, almost all from categories already queried.
# 3 mi confirmed as acceptable field SOP by Bill 2026-09-07; the 1200 m cap's
# own comment scopes its rationale to URBAN incidents, which this is not.
# Well inside _MAX_STAGING_DIST_M, so the distance guard does not reject these.
_STAGING_FALLBACK_RADIUS_M = 4828  # 3.00 mi


def _staging_geocode_implausible(
    lkp_geo: tuple | None, lat: float | None, lng: float | None
) -> bool:
    """True when a staging coordinate is too far from the LKP to be real.

    On 2026-07-24 the officer staging value came back as "CalTopo Map ID:".
    Google Maps "corrected" that to "California", which geocoded to the state
    centroid ~250 km away and was plotted as a staging marker. Another
    dispatcher deleted it mid-callout as a distraction.

    CORRECTION (Bill, 2026-07-25) — this was NOT an OCR misread of the intake
    form. "CalTopo Map ID:" is not a field on the form at all; it is a line in
    the SUMMARY TEMPLATE this system emits (gemini.py:241, and pdf_extract.py
    for the PDF path). Gemini bled a label from its own output template into a
    value field. That makes it a template-structure failure, not handwriting
    OCR — a different class, and one that prompt tuning against form layout
    would never fix. It also means the trigger is not specific to messy
    handwriting: any form can produce it.

    No existing guard could catch it. `_house_number_consistent()` allows
    through when neither string has a house number, and
    `_is_substantive_street_correction()` only asks whether the words changed —
    neither can tell a spelling fix from a category error. The #604
    city-consistency guard is blind here too: `_extract_query_city()` returns
    None for "CalTopo Map ID:, CA", so it reports can't-check.

    Distance works because it tests the RESULT rather than the input. No amount
    of garbage text can produce a plausible coordinate near the LKP, so this one
    check covers every future variant of the same failure instead of the
    specific string that happened to appear.

    Same can't-check-means-allow contract as its sibling guards: with no LKP
    anchor there is nothing to measure against, so allow through.
    """
    if not lkp_geo or lat is None or lng is None:
        return False
    return _haversine_m(lkp_geo[0], lkp_geo[1], lat, lng) > _MAX_STAGING_DIST_M


def _lkp_low_confidence_note(query: str, resolved: str) -> str:
    """Event Log line when Google resolved the LKP to an AREA, not a place (#680).

    Fires on `geometry.location_type == "APPROXIMATE"`, which is Google saying
    it matched a city or a state rather than a specific location. On 2026-07-31
    "Treatment Facility" resolved that way to the centroid of California, and
    the staging POI circle, the CalTopo seed and every marker, the D4H location
    and the responder-facing Slack staging link all inherited it. No guard
    caught it — `_house_number_consistent()` cannot fire on an LKP with no house
    number — a human did.

    NOT keyed on `partial_match`, despite that being the obvious candidate and
    the one issue #680 leads with. Measured against the 18 real LKP strings in
    the corpus (experiments/geocode_confidence/01_partial_match_real_lkps.py):
    partial_match is true on 5 of 16 CORRECT in-area results — a hospital name
    carrying a valid street address, a successful misspelling correction, an
    intersection, and a park. Those are the pipeline working, and warning on
    them would spend the dispatcher's attention on nothing. APPROXIMATE fired
    on 2 of 18, both genuinely non-specific.

    SCOPE — NARROW, AND IT DOES NOT COVER THE INCIDENT THAT MOTIVATED IT.
    This is the Google fallback path only, i.e. it fires when Nominatim has
    already failed outright. Measured from 30 days of personal-dev logs:
    Nominatim answered 55 LKPs and failed 4, so Google is asked on roughly 7%
    of dispatches and this note is reachable on that 7% alone.

    "Treatment Facility" is NOT in that 7%. Nominatim answered it — with
    "Water Treatment Facility, Livingston, Merced County" — so Google was never
    called and this check never ran. Pre-#667 the Nova Scotia answer came from
    Nominatim as well. The class of failure that keeps reaching production is a
    CONFIDENTLY WRONG Nominatim result, and this note is blind to all of it.

    (An earlier version of this docstring claimed the opposite, off a spike
    whose `except` clause scored Nominatim rate-limiting as a no-result. The
    production log counts above replace it. Do not restore the old claim.)

    So this is a cheap, correct guard on a rare path — not a fix for #680's
    root problem. Nominatim exposes `importance` / `place_rank`, which are not
    the same signal and need their own spike (issue #680, design question 3).
    The more promising direction is a cross-check against an independent
    regional reference we already hold and currently ignore: the requesting
    agency's county, or the officer's staging coordinate — on 2026-07-31 that
    coordinate sat squarely in Humboldt while the LKP had resolved to Merced.

    Names the address, like the street-correction note does: the dispatcher can
    still fix the LKP before dispatching, and a line that says only "low
    confidence" gives them nothing to act on. `logger.*` stays PII-free.
    """
    return (
        f"WARNING: Last Known Position \"{query}\" resolved only to an area, not a "
        f"specific location — Google returned \"{resolved}\". Staging, the map and "
        f"the responder link are all anchored here. Confirm the LKP with the officer."
    )


def _staging_distance_note(addr: str, is_officer: bool, dist_m: float) -> str:
    """Dispatcher-facing Event Log line for a staging geocode rejected on distance.

    The tail must match what actually happens to the marker. An officer entry
    with no coordinates hits the last-resort LKP fallback, which plots the marker
    and logs its own line saying so — claiming "no marker was plotted" here would
    contradict the next line of the same Event Log. A non-officer entry really
    does end up with no marker.
    """
    label = "Officer staging" if is_officer else "Staging entry"
    tail = (
        "The Command Post marker was placed at the LKP instead."
        if is_officer
        else "No map marker was plotted for it."
    )
    miles = dist_m / 1609.344
    return (
        f"WARNING: {label} \"{addr.split(',')[0].strip()}\" could not be placed — it "
        f"resolved {miles:.0f} mi from the LKP, which is not a usable staging location. "
        f"{tail} Confirm the staging location with the officer."
    )


def _staging_coordinate_distance_note(coord_text: str, is_officer: bool, dist_m: float) -> str:
    """Event Log line for a written-out coordinate that is FAR from the LKP but KEPT.

    Issue #668 / the 2026-07-31 Humboldt callout. A relative guard is only as
    trustworthy as its anchor: the LKP had resolved to Nova Scotia, the intake
    form's Staging field held the one CORRECT in-county coordinate, and
    `_MAX_STAGING_DIST_M` measured the right answer against the wrong reference,
    found ~4,900 km, and discarded it — then fell staging back to the bad anchor.
    The only WARNING the whole cascade emitted pointed at the good data.

    So a coordinate the officer wrote out explicitly BEATS a geocoded anchor and
    is never discarded on distance. It still has to stay visible, which is what
    this note is for.

    Deliberately NOT `_staging_distance_note`: that function's own docstring
    requires the tail to match what actually happens to the marker, and its
    tails ("The Command Post marker was placed at the LKP instead." / "No map
    marker was plotted for it.") would both be false here. The marker IS placed,
    at the coordinate as written.
    """
    label = "Officer staging coordinate" if is_officer else "Staging coordinate"
    miles = dist_m / 1609.344
    return (
        f"WARNING: {label} \"{coord_text[:100]}\" is {miles:.0f} mi from the LKP. "
        f"The marker was plotted at the coordinate as written — a written coordinate "
        f"is trusted over a geocoded address. Verify BOTH the coordinate and the "
        f"Last Known Position with the officer; one of them is wrong."
    )


_GOOGLE_APPROXIMATE_LOCATION_TYPE = "APPROXIMATE"


def _staging_answer_is_region(location_type: str | None) -> bool:
    """True when the geocoder answered with an AREA rather than a specific place.

    Issue #773. Gates the "an officer-written address beats the distance guard"
    rule: the officer's own words are trusted over a geocoded LKP anchor, but
    only when the provider came back with somewhere you can actually stage.

    `APPROXIMATE` is Google saying it matched a city, county or state rather
    than a location. It is the same signal `_lkp_low_confidence_note()` keys on
    and carries that measurement: over the 18 real LKP strings in the corpus it
    fired on 2, both genuinely non-specific, and did NOT fire on the parks,
    intersections and hospital names among the other 16.

    That measurement is why this is NOT a street-type test. `_STREET_TYPES` has
    no `Park`, and a park is both tier-1 SAR staging and address-less by
    construction (Locked Decision: "Parks display"), so asking "does the answer
    look like a street" would reject the commonest legitimate staging shape
    while passing a state centroid that happens to sit on a named road.

    ONLY GOOGLE REPORTS THIS. `_geocode_nominatim` returns no specificity
    signal, so the Nominatim leg of the guard has nothing to test and trusts an
    officer address by default. That asymmetry is deliberate and evidence-
    scoped, the same shape as the country-binding asymmetry between the two
    providers (#667): do not close it for symmetry without a spike that
    establishes an equivalent Nominatim signal. `importance` / `place_rank` are
    candidates and are NOT the same thing — see issue #680, design question 3.

    KNOWN GAP (issue #779), stated rather than implied. "The Nominatim leg has no signal" is
    true of Nominatim and NOT true of every result that arrives on it:
    `_geocode_lkp_smart` routes INTERSECTION-shaped queries straight to Google
    (Nominatim cannot resolve "X & Y" at all), then unpacks and DISCARDS
    location_type to return a Nominatim-shape 4-tuple. So an officer staging
    intersection — the dominant intersection-bearing source, e.g. "ALMA @ 10TH"
    — is a Google answer whose specificity signal was thrown away one layer up,
    and it is kept here ungated. Closing it means widening
    `_geocode_lkp_smart`'s return shape, which is consumed at three call sites
    including the main `geo` anchor and is 4-tuple-pinned since #604; that is a
    separate change. Residual exposure is narrow: it needs an officer
    intersection that Google can only resolve to an AREA, more than
    `_MAX_STAGING_DIST_M` from the LKP.
    """
    return (location_type or "").strip().upper() == _GOOGLE_APPROXIMATE_LOCATION_TYPE


def _staging_address_distance_note(addr: str, dist_m: float) -> str:
    """Event Log line for an officer-written ADDRESS that is FAR from the LKP but KEPT.

    Issue #773, and the second occurrence of the class #668 was written for.
    On the 2026-08-23 Placer/Auburn mutual-aid callout a one-character city
    misspelling anchored the LKP ~370 mi away in a neighbouring state. The
    officer's staging address was correct, Google resolved it, and this guard
    measured the right answer against the wrong reference and threw it away —
    then fell the officer entry back to the bad anchor. The only WARNINGs the
    cascade emitted all pointed at the officer, and one of them was false.

    #668 had already established that a written-out COORDINATE outranks a
    geocoded anchor. An address the officer wrote is the same human assertion
    arriving in a different shape; only the input shape was ever coordinate-
    specific, never the rationale.

    Deliberately NOT `_staging_distance_note`: that function's docstring
    requires the tail to match what happens to the marker, and both of its
    tails would be false here. The marker IS placed, at the address as
    resolved.

    Wording is deliberately minimal and factual. The blame-framing across all
    three of this path's warnings — none of which suggests the LKP could be at
    fault, though the tool holds the evidence — is issue #633 and is being
    fixed as one coherent pass rather than piecemeal here.
    """
    miles = dist_m / 1609.344
    return (
        f"WARNING: Officer staging address \"{addr.split(',')[0].strip()}\" resolved "
        f"{miles:.0f} mi from the Last Known Position. The marker was plotted at the "
        f"address as written — an officer-written address is trusted over a geocoded "
        f"anchor. Verify BOTH the staging address and the Last Known Position."
    )


# ---------------------------------------------------------------------------
# Shared staging ranking — used by every staging source (Overpass, Geoapify)
# ---------------------------------------------------------------------------

# Minimum separation between two staging recommendations, in metres. Closer than
# this and the dispatcher is being shown one location twice under two names.
#
# CALIBRATED EMPIRICALLY (issue #674, 2026-08-01) by
# experiments/geoapify/03_proximity_calibration.py across six real Santa Clara
# County anchors spanning downtown San Jose to Joseph D. Grant Park. NEVER
# calibrate this from estimated coordinates — #606 shipped a distance threshold
# wrong twice that way; every number below came from Geoapify's own returned
# lat/lon.
#
# Why 100 and not more: at 100 m every populated anchor still fills all seven
# dispatcher-visible slots, and the drop list contains no false positives —
# a food-court tenant 9 m from its own mall, a church 23 m from its own school,
# a dog park 58 m inside its own park. At 150 m the low-density anchors start
# losing PARKS, which are the best staging we have; at 200 m Saratoga loses both
# of them and the downtown list backfills with distant fast food, trading
# category variety for raw separation. Bigger is not better here.
#
# Why not less: at 50 m the downtown pathology that opened #674 survives intact
# (closest pair 54 m, four fast-food outlets inside 120 m).
_STAGING_MIN_SEPARATION_M = 100


def _staging_line_dedup_key(body: str) -> str:
    """Identity key for one RENDERED staging line: house-number+street, or park name.

    The rendered form is "<address or park name>, <city> — <name>. <details>"
    (STAGING FORMAT RULE in gemini.py), so the location is everything before the
    first em-dash and the identity is everything before the first comma. Same
    key shape as the `addr_key` in _rank_dedupe_cap_staging — deliberately, so a
    strip-mall pair collapses identically whichever list it arrives on.

    Parks are NOT exempt here, unlike the address dedup in
    _rank_dedupe_cap_staging. That exemption exists because Geoapify fills a
    bare `city` for nearly every park, so an address key would collapse distinct
    parks on a DATA ARTIFACT. A rendered park line leads with the park's own
    NAME, which is not an artifact: two different parks produce two different
    keys, and two identical keys mean the same park was listed twice.
    """
    loc_part = body.split(" — ")[0].strip()
    return re.sub(r"\s+", " ", loc_part.split(",")[0].lower()).strip()


def _staging_first_word_key(s: str) -> str:
    """Normalized first token, for deciding whether a rendered staging location is
    still recognisably what the officer wrote.

    SHARED, and that sharing is load-bearing: the same comparison decides BOTH
    the "was matched to" Event Log note and the "(as written: …)" suffix on the
    officer's own recommendation line. If the two drifted apart the dispatcher
    could be told the text was rewritten while the responder-facing line kept no
    trace of the original, or the reverse.
    """
    toks = s.split()
    return re.sub(r"[^a-z0-9]", "", toks[0].lower()) if toks else ""


def _staging_candidate_name_leads(cname: str, loc_part: str) -> bool:
    """Does OSM candidate `cname` NAME the location `loc_part`, rather than merely
    appear somewhere inside it? (issue #722)

    Staging lines are rendered VENUE-LED — "<Venue>, <house#> <Street>, <City>
    — <details>" — so the location part normally OPENS with the candidate's own
    name. That is the match Pass A is for, and it is why Pass A carries most
    entries: on the 2026-08-08 dispatch, 6 of 7 entries took Pass A coordinates
    and only n=6 queued for Pass B.

    A bare `cname in loc_part` therefore matched on two different things at
    once. Live 2026-08-08, where the LKP was a shopping mall on a road named
    after it, the location part "popeyes, 1306 great mall pkwy, milpitas"
    contained BOTH "popeyes" and "great mall" — and which one won was decided by
    dict iteration order. Three entries drew the mall's centroid. CalTopo
    plotted three separate addresses at one point while the list text still
    advertised the provider's own distances, so the map and the text disagreed
    with nothing logged; and the override pick list, whose CURRENT badge is
    exact coordinate equality, highlighted three rows for a single pick.

    A candidate name found in the MIDDLE of a location part is not a weaker
    match, it is a DIFFERENT KIND of match: the name has landed inside a street
    name. Requiring it to LEAD makes the venue's own name win deterministically
    instead of by dict order.

    The name must also END at a comma, a period or the end of the string. A
    trailing SPACE is deliberately not a boundary: " Dr", " Pkwy", " Blvd" is
    precisely the failure. Near-misses (Gemini's "Morgan Park" vs OSM's
    "John D Morgan Park", or the reverse) fall through to Pass B exactly as the
    existing name-abbreviation misses already do.

    This narrows the SCOPE of the match, it does not weaken its STRENGTH — the
    full candidate name is still required, so the #173 prohibition on word-level
    fallback is untouched.
    """
    if not cname or not loc_part:
        return False
    if not loc_part.startswith(cname):
        return False
    rest = loc_part[len(cname):]
    return rest == "" or rest[0] in ",."


def _rank_dedupe_cap_staging(
    raw_candidates: list[dict],
) -> tuple[list[dict], int, int]:
    """Sort (tier, distance), dedup by name + house+street, drop candidates within
    _STAGING_MIN_SEPARATION_M of a better-ranked one, count schools/churches from
    the FULL deduped list (pre-cap), and cap at 12.

    Shared by _query_overpass_staging and _query_geoapify_staging so the two
    sources rank identically by construction — the tier authority (_STAGING_TIER),
    the strip-mall address dedup, the #674 proximity filter, and the pre-cap
    school/church counts live here once. Input dicts must carry "name",
    "amenity", "addr", and "dist_m"; any other key (e.g. "lat"/"lng" for CalTopo
    markers) rides through on the returned entries untouched. "lat"/"lng" are
    used by the proximity filter when present and it is skipped when they are
    not, so the contract above is unchanged.

    Returns (capped_candidates, school_count, church_count) where the two counts
    come from the pre-cap deduped list so the school/church time-of-day exclusion
    note fires correctly even when 12+ tier-1 candidates fill the cap. The counts
    are taken AFTER the proximity filter on purpose: the note answers "how many
    entries would you have had but for the time-of-day rule", and a school that
    the proximity filter would have dropped anyway was never one of them. It also
    stops one campus counting twice — Geoapify returns "Saint Martin of Tours
    Church" and "Saint Martin of Tours School" 23 m apart as two features.
    """
    # Sort by tier first, then distance within tier (uses module-level _STAGING_TIER).
    candidates = sorted(
        raw_candidates, key=lambda c: (_STAGING_TIER.get(c["amenity"], 2), c["dist_m"])
    )
    seen_names = set()
    seen_addrs = set()
    deduped = []
    # Issue #807 — TELEMETRY ONLY, read after the loop, never inside it.
    # Records the amenity of every candidate the proximity filter rejects so the
    # park counters below can attribute a drop to THIS filter rather than to the
    # name dedup or the cap. Deliberately generic: the filter itself must stay
    # blind to amenity (see PARKS ARE NOT EXEMPT below, pinned by
    # TestStagingProximityFilter.test_parks_are_not_exempt_in_production), so
    # nothing here may become a condition on the drop.
    prox_dropped_amenities = []
    for c in candidates:
        name_key = c["name"].lower()
        # DESIGN DECISION (issue #669): collapse word-reordered MALL names.
        # One physical mall routinely arrives as two features with the words
        # in a different order — measured 2026-08-01, Geoapify returns both
        # "Westfield Valley Fair" (Stevens Creek Boulevard) and "Valley Fair
        # Westfield" (Monroe Street) for the same site, and Overpass returns a
        # shop=mall and a landuse=retail copy. Different name AND different
        # street means neither existing key collapses them, so a single mall
        # would consume two of the seven staging slots and read to the
        # dispatcher as two options that are actually one place.
        # Scoped to amenity == "mall" ON PURPOSE: a token-set key applied
        # broadly would collapse genuinely different places whose names share
        # words in another order, and it cannot regress anything today because
        # no other source emits this amenity.
        if c["amenity"] == "mall":
            name_key = " ".join(sorted(name_key.split()))
        # Normalize address for dedup: collapse whitespace, drop commas/punctuation.
        # Prevents strip-mall pairs: McDonald's + Walgreens at "5000 Cottle Rd" both
        # passing through because name-only dedup only catches exact name matches.
        # Parks have "(address not in OSM)" — excluded from address dedup so multiple
        # parks in different locations are never incorrectly collapsed.
        raw_addr = c["addr"]
        # Dedup key: use only the house-number + street portion (everything before
        # the first comma), ignoring city/postcode. OSM nodes at the same strip-mall
        # address sometimes have different addr:city or addr:postcode tags, so a
        # full-string comparison ("7000 Yerba Buena Rd, San Jose, 95135" vs
        # "7000 Yerba Buena Rd, San Jose") would miss the duplicate.
        # Parks use "(address not in OSM)" — excluded from address dedup.
        addr_key = (
            re.sub(r"\s+", " ", raw_addr.split(",")[0].lower()).strip()
            if raw_addr != "(address not in OSM)"
            else None
        )
        if name_key in seen_names:
            continue
        # DESIGN DECISION (do not revert without team discussion):
        # Deduplicate by street address in addition to name. When a source returns
        # multiple businesses at the same strip-mall address, keep only the first
        # (best tier + shortest distance per the preceding sort). This prevents
        # staging recommendations showing pairs like "McDonald's" and "Walgreens"
        # both at "5000 Cottle Rd" — one address, one staging entry is sufficient.
        if addr_key and addr_key in seen_addrs:
            continue
        # DESIGN DECISION (issue #674): proximity filter. Drop a candidate that
        # sits within _STAGING_MIN_SEPARATION_M of an already-accepted, higher-
        # ranked one. The two dedup keys above are IDENTITY checks (same name,
        # same house+street) and neither has ever measured the distance between
        # two DIFFERENT addresses, so two entries fifty feet apart on different
        # street numbers both survived by construction. On 2026-07-31 that put
        # all seven ranked entries inside 0.13 mi of the LKP and of each other —
        # seven addresses, one location, no actual choice for the dispatcher.
        #
        # Arbitration is deliberately the SAME rule the address dedup already
        # uses: the list is pre-sorted (tier, then distance), so the first
        # candidate to arrive wins and later ones near it are dropped. No new
        # tie-break is invented here.
        #
        # PARKS ARE NOT EXEMPT, unlike the address dedup above. That exemption
        # exists because Geoapify populates a bare `city` for nearly every park,
        # so an address key would collapse distinct parks on a DATA ARTIFACT.
        # Coordinates are not an artifact, and the measurement is unambiguous:
        # exempting parks reinstates "Circle of Palms Plaza" 56 m from "Fairmont
        # Plaza" — two adjacent downtown San Jose plazas, which is exactly the
        # failure this filter exists to remove.
        #
        # lat/lng are NOT part of this function's input contract (the docstring
        # promises only name/amenity/addr/dist_m). A candidate without usable
        # coordinates skips the check and behaves as it did before #674 —
        # both callers turn any exception in here into ([], 0, 0, False), i.e.
        # staging silently disappearing, which is far worse than a missed drop.
        c_lat, c_lng = c.get("lat"), c.get("lng")
        if c_lat is not None and c_lng is not None and any(
            _haversine_m(c_lat, c_lng, k["lat"], k["lng"]) < _STAGING_MIN_SEPARATION_M
            for k in deduped
            if k.get("lat") is not None and k.get("lng") is not None
        ):
            prox_dropped_amenities.append(c["amenity"])
            continue
        seen_names.add(name_key)
        if addr_key:
            seen_addrs.add(addr_key)
        deduped.append(c)
    # DESIGN DECISION (do not revert without team discussion):
    # Count schools and churches from the FULL deduped list BEFORE applying the :12 cap.
    # In dense urban areas (e.g. Cupertino near Apple Park) there can be 12+ tier-1
    # candidates (fast food, parks, pharmacies) that fill the cap entirely, pushing
    # tier-2 schools and churches out of the capped list.  If we counted from the capped
    # list, _school_count would always be 0 in those areas and the time-of-day exclusion
    # note would never fire.  The uncapped counts are returned alongside the capped list
    # so the exclusion note logic can use them directly without touching staging_candidates.
    _school_count_uncapped  = sum(1 for c in deduped if c.get("amenity") in ("school", "college"))
    _church_count_uncapped  = sum(1 for c in deduped if c.get("amenity") == "place_of_worship")
    capped = deduped[:12]  # 7-entry cap + filtering headroom
    # DESIGN DECISION (issue #807): park telemetry, emitted here rather than in
    # the two callers so both POI sources are instrumented identically — the
    # same reason the ranking itself lives in this helper.
    #
    # Parks are tier 1 and are frequently the best staging SAR gets, but the
    # caller log lines count only schools and churches, so an absent park and an
    # EVICTED park looked identical in the logs. Those are opposite diagnoses:
    # a provider gap versus the eviction the #674 row explicitly warns about
    # ("it starts evicting parks in low-density anchors"). Four counters, each
    # the boundary of one stage:
    #     parks_in                 — returned by the provider
    #     parks_dropped_proximity  — evicted by the #674 filter
    #     parks_precap             — survived dedup + filter
    #     parks_capped             — survived the :12 cap, i.e. reached Gemini
    # Name-dedup drops are the remainder (in - proximity - precap); the address
    # dedup cannot drop a park while the "(address not in OSM)" sentinel holds.
    # Counted from `deduped`, never re-counted from `capped`, for the same
    # reason as the school/church counts directly above.
    #
    # Counts only — no names, no coordinates (core privacy guarantee #3).
    _parks_in = sum(1 for c in candidates if c.get("amenity") == "park")
    _parks_precap = sum(1 for c in deduped if c.get("amenity") == "park")
    logger.info(
        "Staging park telemetry | parks_in=%d parks_dropped_proximity=%d "
        "parks_precap=%d parks_capped=%d",
        _parks_in, prox_dropped_amenities.count("park"),
        _parks_precap, sum(1 for c in capped if c.get("amenity") == "park"),
    )
    return capped, _school_count_uncapped, _church_count_uncapped


# ---------------------------------------------------------------------------
# Overpass API — real geographic POI lookup for staging candidates
# ---------------------------------------------------------------------------

async def _query_overpass_staging(
    lat: float, lng: float, radius_m: int = 1200, wide: bool = False
) -> tuple[list[dict], int, int, bool]:
    """
    Query OpenStreetMap Overpass API for staging-suitable POIs within radius_m meters
    of the given coordinates.

    Returns (candidates, school_count, church_count, mirrors_ok) where:
      candidates    — deduped list capped at 12, sorted by (tier, distance), for Gemini
      school_count  — count of unique schools in the FULL deduped list (before the cap)
      church_count  — count of unique places of worship in the FULL deduped list
      mirrors_ok    — False when ALL mirrors failed (network/server errors); True when at
                      least one mirror responded (even if it returned zero results).
                      Callers use this to distinguish "no POIs nearby" from "servers down".

    school_count and church_count come from the pre-cap list so the school/church
    time-of-day exclusion note fires correctly even when 12+ tier-1 candidates (fast food,
    parks, pharmacies) fill the cap and push schools/churches out of the Gemini prompt.
    Never raises — on any failure returns ([], 0, 0, False) so that Gemini falls back gracefully.

    radius_m=1200 ≈ 0.75 miles — field SOP hard cap for urban incidents.
    """
    # DESIGN DECISION (PR #162): use nwr (node/way/relation) for ALL element types, not
    # just parks.  Schools and places of worship are almost always stored as way/relation
    # polygons in OSM — a node-only query returns zero results for them.  Confirmed by
    # manual Overpass check around 10000 Calvert Dr, Cupertino: 5 schools + 6 churches
    # were all 'way' elements, none appeared in the production candidate list, and the
    # school/church time-of-day exclusion note never fired.  The element parser already
    # handles center coords for non-node elements (lines below), so no parser change needed.
    # Ops #839. The wide-only amenities are appended for the widened retry ONLY,
    # mirroring _GEOAPIFY_WIDE_ONLY_CATEGORIES. Keeping the two provider legs in
    # step is not optional: Overpass is the outage fallback, so a Geoapify-only
    # category change goes blind exactly when the fallback is carrying the load,
    # and it also fabricates a shadow-compare diff on every affected dispatch.
    _amenities = "|".join(_OVERPASS_AMENITIES + (_OVERPASS_AMENITIES_WIDE_ONLY if wide else []))
    query = f"""
[out:json][timeout:15];
(
  nwr(around:{radius_m},{lat},{lng})[amenity~"^({_amenities})$"];
  nwr(around:{radius_m},{lat},{lng})[shop~"^(convenience|supermarket|grocery|chemist|mall)$"];
  nwr(around:{radius_m},{lat},{lng})[leisure=park][name];
);
out center body;
"""
    # Try each Overpass mirror in order; return first successful result.
    #
    # Per-mirror timeout: 12s. Data-driven cut from the original 18s based on
    # 30-day Cloud Run log analysis (issue #551, May 2026): p99 successful
    # response on overpass-api.de = 11.51s; max observed success = 11.92s; 0
    # successes ever reached the 12-18s band. Cutting from 18s to 12s loses no
    # observed real successes and saves ~6s per cascade-failure (primary 504
    # at ~10s + backup timeout). Full analysis:
    # research/overpass-timeout-analysis/README.md (gitignored).
    elements: list = []
    for _endpoint in _OVERPASS_MIRRORS:
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.post(
                    _endpoint,
                    data={"data": query},
                    headers={"User-Agent": "SCCSSAR-Dispatch/1.5i (SAR operations, contact dispatcher@sccssar.org)"},
                )
            if r.status_code == 200:
                elements = r.json().get("elements", [])
                break  # success
            logger.warning("Overpass %s returned HTTP %s — trying next mirror", _endpoint, r.status_code)
        except Exception as exc:
            logger.warning("Overpass %s failed: %s — trying next mirror", _endpoint, type(exc).__name__)
    else:
        logger.warning("All Overpass mirrors exhausted — staging candidates unavailable")
        return [], 0, 0, False

    try:
        candidates = []
        for el in elements:
            tags = el.get("tags", {})
            name = tags.get("name")
            if not name:
                continue  # Skip unnamed elements
            # amenity covers fast_food/fuel/pharmacy/etc; shop covers supermarket/grocery/etc;
            # leisure covers park. Parks have no street address — handled downstream by
            # _strip_park_address() and _is_park_line(), which display them as "Park Name, City".
            amenity = (
                tags.get("leisure")   # park
                or tags.get("amenity")
                or tags.get("shop", "")
            )
            # Build address string from OSM address tags (may be partial).
            # Parks (way/relation) never have addr: tags — addr_str will be empty,
            # which is expected and correct; Gemini formats them as name-only.
            addr_parts = [
                tags.get("addr:housenumber", ""),
                tags.get("addr:street", ""),
            ]
            addr_str = " ".join(p for p in addr_parts if p)
            city = tags.get("addr:city", "")
            postcode = tags.get("addr:postcode", "")
            if city:
                addr_str = f"{addr_str}, {city}" if addr_str else city
            if postcode:
                addr_str = f"{addr_str}, {postcode}" if addr_str else postcode

            # Nodes have direct lat/lon; ways and relations use the centroid from
            # "out center body" — needed for parks, schools, and churches which are
            # typically stored as polygons (way/relation) in OSM.
            el_type = el.get("type", "node")
            if el_type == "node":
                el_lat = el.get("lat", lat)
                el_lng = el.get("lon", lng)
            else:
                center = el.get("center", {})
                el_lat = center.get("lat", lat)
                el_lng = center.get("lon", lng)
            dist_m = _haversine_m(lat, lng, el_lat, el_lng)

            candidates.append({
                "name": name,
                "amenity": amenity,
                "addr": addr_str or "(address not in OSM)",
                "dist_m": round(dist_m),
                # lat/lng retained for CalTopo map markers — not passed to Gemini
                "lat": el_lat,
                "lng": el_lng,
            })

        # Sort / dedup / pre-cap counts / cap — shared with the Geoapify source via
        # _rank_dedupe_cap_staging() so both sources rank identically by construction.
        result, _school_count_uncapped, _church_count_uncapped = _rank_dedupe_cap_staging(candidates)
        logger.info(
            "Overpass staging candidates | count=%d radius_m=%d schools=%d churches=%d",
            len(result), radius_m, _school_count_uncapped, _church_count_uncapped,
        )
        return result, _school_count_uncapped, _church_count_uncapped, True

    except Exception as exc:
        logger.warning("Overpass API failed: %s", type(exc).__name__)
        return [], 0, 0, False


# ---------------------------------------------------------------------------
# Geoapify Places API — OSM POI lookup for staging (Overpass migration)
# ---------------------------------------------------------------------------

def _geoapify_resolve_amenity(categories: list) -> str | None:
    """Map a Geoapify feature's category list to the OSM amenity vocabulary,
    best-tier-first (_GEOAPIFY_PRIORITY). Returns None when no known category
    matched, so the caller drops the feature rather than guessing a tier."""
    for substr, amenity in _GEOAPIFY_PRIORITY:
        if any(substr in c for c in categories):
            return amenity
    return None


def _geoapify_addr_shape(props: dict, is_park: bool) -> str:
    """Normalize Geoapify address props into the OSM addr shapes downstream code
    already speaks: parks → the "(address not in OSM)" sentinel; others →
    "house street, city"; nothing usable → the same sentinel (both the PASS-2
    filter and the override navigability filter depend on that exact literal)."""
    if is_park:
        # Parks ALWAYS return the sentinel (never the city), so they behave exactly
        # like Overpass parks downstream. Geoapify populates `city` for nearly every
        # park (OSM/Overpass parks usually have no addr:city → they hit this sentinel);
        # if we returned the bare city here, _rank_dedupe_cap_staging would key two
        # distinct same-city parks to the same "<city>" address and drop all but the
        # nearest as false strip-mall duplicates (collapsing 5–6 real parks to 1 at a
        # typical SCC anchor). The sentinel exempts parks from address dedup and drops
        # them from the /apply-staging-override navigability filter — matching Overpass.
        # City for park display is added downstream from context, as today.
        return "(address not in OSM)"
    hn = props.get("housenumber", "")
    st = props.get("street", "")
    city = props.get("city", "")
    addr = " ".join(p for p in (hn, st) if p)
    if city:
        addr = f"{addr}, {city}" if addr else city
    return addr or "(address not in OSM)"


def _is_geoapify_junk_park(name: str, props: dict) -> bool:
    """Geoapify tags city/district-centroid nodes as leisure.park with a name equal
    to the city ("San Jose") or "<City>, <State>" ("Los Gatos, California"). These
    are unnavigable name-only entries that would survive the park addr exemption —
    drop them. Appeared at 4/5 low-density anchors in spike_03 (2026-07-13)."""
    n = (name or "").strip().lower()
    if not n:
        return True
    city = (props.get("city", "") or "").strip().lower()
    state = (props.get("state", "") or "").strip().lower()
    if city and (n == city or (state and n == f"{city}, {state}")):
        return True
    return False


def _geoapify_display_name(props: dict) -> str:
    """Geoapify returns purely-numeric POI names (e.g. the "76" gas-station brand)
    as JSON numbers, not strings; coerce to str so the downstream .lower() dedup,
    CalTopo marker labels, and Gemini injection never hit AttributeError. Overpass
    returns all OSM tag values as strings, so this typing seam is Geoapify-specific.
    Empty / missing → "" (the caller drops nameless features). Confirmed live on the
    2026-07-18 soak: a Union "76" station near a San Jose LKP raised AttributeError
    in the parse loop and silently failed the whole Geoapify lookup, falling back to
    Overpass (DEVTEST7)."""
    return str(props.get("name") or props.get("address_line1") or "")


async def _geoapify_places_call(
    client, lat: float, lng: float, radius_m: int, categories: list, use_bias: bool
) -> tuple[list, bool]:
    """One Geoapify /v2/places GET. Returns (features, ok); ok=False on any non-200
    or transport error. Never raises. filter/bias use lon,lat order (Geoapify
    convention)."""
    params = {
        "categories": ",".join(categories),
        "filter": f"circle:{lng},{lat},{radius_m}",
        "limit": 100,
    }
    if use_bias:
        params["bias"] = f"proximity:{lng},{lat}"
    try:
        # Auth via header, NOT the ?apiKey= query param: httpx logs the full request
        # URL at INFO, so a query-param key leaks the secret into Cloud Run logs
        # (30-day retention). The X-API-Key header keeps it out of the URL (httpx does
        # not log headers). Verified 2026-07-18: Geoapify /v2/places accepts the key
        # via X-API-Key (HTTP 200), same as ?apiKey.
        r = await client.get(
            "https://api.geoapify.com/v2/places",
            params=params,
            headers={"X-API-Key": _GEOAPIFY_API_KEY},
        )
        if r.status_code == 200:
            return r.json().get("features", []), True
        logger.warning("Geoapify /v2/places returned HTTP %s", r.status_code)
        return [], False
    except Exception as exc:
        logger.warning("Geoapify /v2/places failed: %s", type(exc).__name__)
        return [], False


async def _query_geoapify_staging(
    lat: float, lng: float, radius_m: int = 1200, wide: bool = False
) -> tuple[list[dict], int, int, bool]:
    """
    Geoapify Places staging lookup. SAME 4-tuple contract as _query_overpass_staging:
      (candidates, school_count, church_count, source_ok)

    Candidates use the OSM amenity vocabulary (via _GEOAPIFY_PRIORITY) and carry
    lat/lng, so every downstream consumer (tier sort, school/church counts, park
    handling, CalTopo markers, override filter) works unchanged. Two calls per
    lookup — civic (no bias) + commercial (bias) — unioned through the shared
    _rank_dedupe_cap_staging so ranking is identical to the Overpass path.

    source_ok is True when at least one of the two calls returned 200 (mirrors
    Overpass "≥1 mirror responded, even if zero results"). Never raises — any
    failure returns ([], 0, 0, False). Short-circuits with NO HTTP call when
    GEOAPIFY_API_KEY is unset, so an env without the key runs on Overpass as before.
    """
    if not _GEOAPIFY_API_KEY:
        return [], 0, 0, False
    # Ops #839: fire_station/police ride on the CIVIC call (unbiased, complete)
    # and only on the widened retry. They are appended rather than sent as a
    # third call so the wide pass still costs exactly two requests.
    _civic_cats = _GEOAPIFY_CIVIC_CATEGORIES + (
        _GEOAPIFY_WIDE_ONLY_CATEGORIES if wide else []
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            (civic_feats, civic_ok), (comm_feats, comm_ok) = await asyncio.gather(
                _geoapify_places_call(
                    client, lat, lng, radius_m, _civic_cats, use_bias=False
                ),
                _geoapify_places_call(
                    client, lat, lng, radius_m, _GEOAPIFY_COMMERCIAL_CATEGORIES, use_bias=True
                ),
            )
        if not (civic_ok or comm_ok):
            return [], 0, 0, False  # both calls failed — source is down

        raw: list[dict] = []
        for feat in civic_feats + comm_feats:
            props = feat.get("properties", {})
            amenity = _geoapify_resolve_amenity(props.get("categories", []))
            if amenity is None:
                continue  # unmapped category — drop, never guess a tier
            name = _geoapify_display_name(props)
            is_park = amenity == "park"
            if is_park and _is_geoapify_junk_park(name, props):
                continue  # city-centroid node tagged leisure.park — unnavigable
            if not name:
                continue  # no usable label
            el_lat = props.get("lat", lat)
            el_lng = props.get("lon", lng)
            dist = props.get("distance")
            dist_m = round(dist) if dist is not None else round(_haversine_m(lat, lng, el_lat, el_lng))
            raw.append({
                "name": name,
                "amenity": amenity,
                "addr": _geoapify_addr_shape(props, is_park),
                "dist_m": dist_m,
                # lat/lng retained for CalTopo map markers + the override endpoint
                "lat": el_lat,
                "lng": el_lng,
            })

        result, _school_count, _church_count = _rank_dedupe_cap_staging(raw)
        # civic_raw is the monitor (build-req): if it ever nears the 100-result limit
        # at an SCC anchor the civic bundle needs a further split (downtown 59 today).
        logger.info(
            "Geoapify staging candidates | count=%d radius_m=%d schools=%d churches=%d "
            "civic_raw=%d commercial_raw=%d civic_ok=%s commercial_ok=%s",
            len(result), radius_m, _school_count, _church_count,
            len(civic_feats), len(comm_feats), civic_ok, comm_ok,
        )
        return result, _school_count, _church_count, True

    except Exception as exc:
        logger.warning("Geoapify API failed: %s", type(exc).__name__)
        return [], 0, 0, False


# ---------------------------------------------------------------------------
# Logging — structured JSON for Cloud Logging
# ---------------------------------------------------------------------------
# DESIGN DECISION (do not revert without team discussion):
# We emit log records as real JSON objects written to stdout so that Cloud
# Logging parses them natively — giving us severity pills, filterable fields,
# and a clean summary line in Cloud Console.  The previous approach used
# logging.basicConfig with a JSON *format string*, which produced a JSON-shaped
# *textPayload string* that Cloud Logging treated as opaque text (raw JSON blobs
# in the console, no severity colouring, no field extraction).
#
# Key differences from basicConfig approach:
#   "severity"  (not "level")  — Cloud Logging maps this to severity levels
#   "message"   (not "msg")    — Cloud Logging uses this as the summary line
#   json.dumps() to stdout     — Cloud Logging receives a structured JSON object
#
# No new dependencies — stdlib json only.  All existing logger.info/error/...
# call sites are unchanged.
# ---------------------------------------------------------------------------

_CLOUD_LOGGING_SEVERITY = {
    "DEBUG":    "DEBUG",
    "INFO":     "INFO",
    "WARNING":  "WARNING",
    "ERROR":    "ERROR",
    "CRITICAL": "CRITICAL",
}


# Issue #608 — secrets must never reach the log sink, including when a
# third-party library formats the message.
#
# httpx logs every outbound request at INFO with the FULL URL including query
# string. Google Maps and Geoapify both pass their API key as a query
# parameter, so every geocode wrote a live key to Cloud Run in plaintext:
#
#   HTTP Request: GET https://maps.googleapis.com/...&key=AIzaSy... "200 OK"
#
# Observed on sccssar-dev (production) 2026-07-24. Anyone with log-read IAM on
# the project could read it. The key IS correctly held in Secret Manager — it
# was leaking at the log layer, which is why "credentials never in source" did
# not catch it.
#
# Redaction happens HERE, at the single handler every record passes through,
# rather than by silencing httpx. Two reasons: those request lines are
# genuinely useful (they are how the 2026-07-25 wrong-city investigation was
# resolved), and a library-specific fix would not cover the next library that
# logs a URL. One choke point, every logger, forever.
_SECRET_QS_RE = re.compile(
    r"(?i)([?&](?:key|api_?key|access_?token|token|secret|signature|sig|password|pwd)=)"
    r"[^&\s\"'\\]+"
)

# PII-bearing query parameters — the SAME leak one layer over. httpx logs
# every outbound URL at INFO, and the geocoders carry the subject's address in
# the query string: Nominatim `q=` and Google Maps `address=` (LKP and the
# subject's RESIDENCE), Geoapify `filter=` and `bias=` (LKP coordinates at full
# float precision). Measured 2026-09-06 on sccssar-dev: 42 / 7 / 12 such lines
# in seven days. Core privacy guarantee #3 ("no PII in logs") held at every
# logger.* call in this repo and failed here, in a library logger that the AST
# guard (test_pii_log_patterns.py) cannot see. Host and path survive so the
# line still says WHICH provider answered; the value does not. Pinned by
# test_log_redaction.py, which until this change asserted the address SURVIVED.
_PII_QS_RE = re.compile(
    r"(?i)([?&](?:q|address|filter|bias)=)"
    r"[^&\s\"'\\]+"
)


def _redact_secrets(text: str) -> str:
    """Replace the VALUE of any secret- or PII-bearing query parameter.

    Keeps the parameter NAME so a reader can still tell the call was
    authenticated and which field was sent, and leaves host and path intact so
    the line still identifies the provider. The address itself does not
    survive — it is the subject's. Never raises — a logging path that can
    throw is worse than the leak it prevents.
    """
    try:
        return _PII_QS_RE.sub(r"\1REDACTED", _SECRET_QS_RE.sub(r"\1REDACTED", text))
    except Exception:  # pragma: no cover — defensive; logging must not fail
        return text


class _StructuredJsonHandler(logging.StreamHandler):
    """Emit log records as JSON objects that Cloud Logging parses natively."""

    def emit(self, record: logging.LogRecord) -> None:
        entry: dict = {
            "severity": _CLOUD_LOGGING_SEVERITY.get(record.levelname, "DEFAULT"),
            "message":  _redact_secrets(self.format(record)),
            "logger":   record.name,
        }
        if record.exc_info:
            # Tracebacks carry request URLs too — an httpx exception repr
            # includes the full URL that failed.
            entry["exception"] = _redact_secrets(self.formatException(record.exc_info))
        # Write to stdout; flush immediately so Cloud Run captures each line.
        print(json.dumps(entry), flush=True)


_root_logger = logging.getLogger()
# KNOWN GAP (security review 2026-09-06): uvicorn installs its own handlers on
# `uvicorn`, `uvicorn.error` and `uvicorn.access` with propagate=False, so an
# UNHANDLED ASGI traceback ("Exception in ASGI application") goes to stderr
# WITHOUT passing through _redact_secrets. Every handled path is covered --
# both Maps call sites swallow exceptions and log only the type -- so nothing
# routes a secret or subject text into such a traceback today. A future
# raise_for_status() on a geocode call, or any exception whose str() embeds
# user text, would. Closing it means configuring uvicorn's loggers to
# propagate into this handler; not done because no live path needs it yet.
_root_logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
_root_logger.handlers.clear()
_root_logger.addHandler(_StructuredJsonHandler())

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Two-flag rollout model — Everbridge + Slack integration (Task 1.4)
# See docs/plans/2026-04-24-everbridge-slack-integration-design.md "Two-Flag Rollout Model"
# ---------------------------------------------------------------------------

_EVERBRIDGE_MODE = os.environ.get("EVERBRIDGE_MODE", "").lower()
_SLACK_MODE      = os.environ.get("SLACK_MODE",      "").lower()

# Staging POI source (Overpass→Geoapify migration). Per-env flag, same rollout
# pattern as EVERBRIDGE_MODE/SLACK_MODE: unset/"overpass" keeps Overpass driving;
# "geoapify" makes Geoapify primary with Overpass as the runtime fallback.
# Unset behaves exactly like today.
_STAGING_SOURCE  = os.environ.get("STAGING_SOURCE",  "").lower()

# Staging shadow mode. "on" (default): every lookup runs BOTH sources in parallel and
# emits the `Staging source compare` line — the active source drives, the other is a
# measured-only shadow (the personal-dev soak). "off": run ONLY the active source and
# fall back to the other sequentially on failure — no parallel wait, no compare log.
# Used to demote Overpass to a sequential secondary on sccssar-dev once the compare
# A/B is done, without re-imposing the slower source's latency on every /ocr.
_STAGING_SHADOW_RAW = os.environ.get("STAGING_SHADOW", "on").strip().lower()


def _feature_enabled() -> bool:
    """True iff both flags are set to a recognized value. Used to gate every new
    endpoint added by the Everbridge + Slack integration. When either flag is
    unset (typical on SCCSSAR-dev during Phase 1–3 before Phase 5 Step 5.1
    mirrors infrastructure), this returns False and all new endpoints 503.
    """
    return (
        _EVERBRIDGE_MODE in ("safe", "full")
        and _SLACK_MODE in ("shadow", "full")
    )


# DESIGN DECISION (do not revert without team discussion): SLACK_MODE=full with
# EVERBRIDGE_MODE=safe is rejected at startup. Real responders auto-invited to a
# Slack channel for an incident still in draft state is operationally incoherent.
# See design Section 4 + integration plan Task 1.4.
if _EVERBRIDGE_MODE == "safe" and _SLACK_MODE == "full":
    raise RuntimeError(
        "EVERBRIDGE_MODE=safe with SLACK_MODE=full is not allowed — "
        "Slack real invites cannot precede Everbridge real send."
    )

# STAGING_SOURCE must be a recognized value — fail fast like the EB/Slack guard so
# a typo (e.g. "geopify") can never silently fall through to Overpass unnoticed.
if _STAGING_SOURCE not in ("", "overpass", "geoapify"):
    raise RuntimeError(
        f"STAGING_SOURCE={_STAGING_SOURCE!r} is not allowed — "
        "use 'overpass' (default) or 'geoapify'."
    )

# STAGING_SHADOW must be "on" (default) or "off" — same fail-fast contract.
if _STAGING_SHADOW_RAW not in ("on", "off"):
    raise RuntimeError(
        f"STAGING_SHADOW={_STAGING_SHADOW_RAW!r} is not allowed — use 'on' (default) or 'off'."
    )
_STAGING_SHADOW = _STAGING_SHADOW_RAW == "on"


logger.info(
    "Feature flags loaded: EVERBRIDGE_MODE=%s SLACK_MODE=%s STAGING_SOURCE=%s STAGING_SHADOW=%s",
    _EVERBRIDGE_MODE or "<unset>",
    _SLACK_MODE or "<unset>",
    _STAGING_SOURCE or "overpass",
    "on" if _STAGING_SHADOW else "off",
)


# ---------------------------------------------------------------------------
# Staging source dispatcher — symmetric always-on shadow (Overpass↔Geoapify)
# ---------------------------------------------------------------------------

async def _timed(coro) -> tuple:
    """Await coro, returning (result, elapsed_ms). Lets _query_staging_pois log each
    staging source's wall-clock latency for the shadow comparison."""
    t0 = time.monotonic()
    res = await coro
    return res, round((time.monotonic() - t0) * 1000)


async def _query_staging_pois(
    lat: float, lng: float, radius_m: int = 1200, log_compare: bool = True,
    wide: bool = False,
) -> tuple[list[dict], int, int, bool]:
    """Staging POI lookup. STAGING_SHADOW="on" (default) runs a symmetric always-on
    shadow: BOTH the Overpass and Geoapify sources concurrently, returning the ACTIVE
    source's 4-tuple (per _STAGING_SOURCE) while the other is measured/logged only,
    never used. STAGING_SHADOW="off" runs sequentially: only the active source, with
    the other as a fallback-on-failure — no parallel wait, no compare log.

      - "geoapify": Geoapify drives; if it is unavailable, fall back to the Overpass
        result already gathered (no extra call). Overpass runs as the shadow.
      - "overpass" / unset (default): Overpass drives — today's behavior, including
        the all-mirrors-failed WARNING at the /ocr call site, is preserved exactly.
        Geoapify runs as a pure shadow and never affects the returned result.

    Same 4-tuple contract as each underlying source, so both call sites unpack it
    unchanged. When log_compare is True the compare log carries counts + name/tier1
    overlap + per-source latency + ok flags ONLY — never POI names, addresses, or
    coordinates (PII-safe by contract; the name sets are built locally and only
    their sizes are logged). log_compare=False at the /apply-staging-override site
    keeps that endpoint's log surface exactly as its Locked Decision pins it.
    """
    # Both underlying sources never raise by contract; the try/except is belt-and-
    # suspenders so a contract violation degrades to "source down" (graceful staging
    # WARNING) instead of crashing /ocr — whose enclosing try only catches RuntimeError.
    try:
        if not _STAGING_SHADOW:
            # Sequential mode (STAGING_SHADOW=off): the active source drives; the other
            # is a fallback called ONLY on failure — no parallel gather, no compare log.
            # Avoids re-imposing the slower source's latency on every /ocr once the
            # shadow A/B is done (sccssar-dev after the flip).
            if _STAGING_SOURCE == "geoapify":
                geoapify_res, g_ms = await _timed(_query_geoapify_staging(lat, lng, radius_m, wide=wide))
                if geoapify_res[3]:  # geoapify source_ok
                    # Sequential mode emits no compare line, so this is the ONLY
                    # per-dispatch Geoapify latency signal on a flipped env
                    # (sccssar-dev). Count + ms + radius only — PII-safe by the same
                    # contract as the compare line (no names/addresses/coords).
                    logger.info(
                        "Staging sequential | source=geoapify geoapify_count=%d "
                        "geoapify_ms=%d radius_m=%d",
                        len(geoapify_res[0]), g_ms, radius_m,
                    )
                    return geoapify_res
                overpass_res, o_ms = await _timed(_query_overpass_staging(lat, lng, radius_m, wide=wide))
                logger.warning(
                    "Staging fallback | primary=geoapify backup=overpass "
                    "overpass_ok=%s geoapify_ms=%d overpass_ms=%d (sequential)",
                    overpass_res[3], g_ms, o_ms,
                )
                return overpass_res
            return await _query_overpass_staging(lat, lng, radius_m, wide=wide)

        (overpass_res, o_ms), (geoapify_res, g_ms) = await asyncio.gather(
            _timed(_query_overpass_staging(lat, lng, radius_m, wide=wide)),
            _timed(_query_geoapify_staging(lat, lng, radius_m, wide=wide)),
        )
        if log_compare:
            o_cands, _o_sc, _o_cc, o_ok = overpass_res
            g_cands, _g_sc, _g_cc, g_ok = geoapify_res
            o_names = {c["name"].lower() for c in o_cands}
            g_names = {c["name"].lower() for c in g_cands}
            o_tier1 = {c["name"].lower() for c in o_cands if _STAGING_TIER.get(c["amenity"]) == 1}
            g_tier1 = {c["name"].lower() for c in g_cands if _STAGING_TIER.get(c["amenity"]) == 1}
            logger.info(
                "Staging source compare | active=%s overpass_count=%d geoapify_count=%d "
                "name_overlap=%d tier1_overlap=%d overpass_ok=%s geoapify_ok=%s "
                "overpass_ms=%d geoapify_ms=%d radius_m=%d",
                _STAGING_SOURCE or "overpass",
                len(o_cands), len(g_cands), len(o_names & g_names), len(o_tier1 & g_tier1),
                o_ok, g_ok, o_ms, g_ms, radius_m,
            )
        if _STAGING_SOURCE == "geoapify":
            if geoapify_res[3]:  # geoapify source_ok
                return geoapify_res
            logger.warning(
                "Staging fallback | primary=geoapify backup=overpass overpass_ok=%s",
                overpass_res[3],
            )
            return overpass_res
        return overpass_res
    except Exception as exc:
        logger.warning("Staging dispatcher failed: %s", type(exc).__name__)
        return [], 0, 0, False


# ---------------------------------------------------------------------------
# Mode-aware send routing — Task 1.9
# See design Section 4 §3 + integration plan Task 1.9.
#
# Two routing decisions:
#   - EVERBRIDGE_MODE=full   → unconditional send_live (safe-list NOT consulted)
#   - EVERBRIDGE_MODE=safe   → all selected target_ids must be in the safe list
#                              (allowed_contact_ids ∪ allowed_group_ids) for
#                              send_live; otherwise create a draft template the
#                              dispatcher reviews + sends manually
#
# DESIGN DECISION (do not revert without team discussion): _route_send() consults
# ONLY contact_ids + group_ids from the safe list — never allowed_emails. The
# emails list is the Slack-side allowlist (Item 1 unified safe-list); using it
# here would conflate "permitted Slack invitee" with "permitted EB target",
# which the design explicitly separates.
# ---------------------------------------------------------------------------

import dataclasses
from functools import lru_cache


@dataclasses.dataclass(frozen=True)
class SendDecision:
    """Result of _route_send(). action ∈ {'send_live', 'send_draft'}."""
    action: str
    target_ids: list[str]


@lru_cache(maxsize=1)
def _load_safe_list_secret() -> dict:
    """Parse DISPATCH_SAFE_LIST env var (Secret Manager-mounted JSON).

    Schema (design Section 4 unified safe-list — serves both EB and Slack):
        {
          "allowed_contact_ids": [...],   # Everbridge — consulted by _route_send
          "allowed_group_ids":   [...],   # Everbridge — consulted by _route_send
          "allowed_emails":      [...],   # Slack — consulted by channel-creation (Task 1.10)
          "label_overrides":     {...}    # display dict, keyed on either ID type
        }

    Defensive defaults: missing keys backfill to empty so callers don't need
    .get(...) per access. Empty/unset secret → every key is empty → every
    target is off-safe → every safe-mode send drafts (correct fail-closed).

    Fail-closed on malformed secret (PR-C, Melanie's main.py H-2):
    `json.loads()` on a malformed secret raises JSONDecodeError; a valid
    JSON value that isn't an object (e.g. a string or array) would
    AttributeError on `.setdefault`. Either path would surface as a 500
    on /send-notification and on the polling loop's safe-list lookup
    until the secret is repaired. Both are caught here and fall back to
    the empty-defaults dict, which routes every send to draft — same
    failure mode as an unset secret. Errors are logged with the
    exception type only; the secret content (allowlist of contact IDs
    and emails) is never logged.
    """
    defaults = {
        "allowed_contact_ids": [],
        "allowed_group_ids":   [],
        "allowed_emails":      [],
        "label_overrides":     {},
    }
    raw = os.environ.get("DISPATCH_SAFE_LIST", "")
    if not raw:
        return defaults
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error(
            "DISPATCH_SAFE_LIST JSON parse failed — fail-closed to empty defaults | error_type=%s",
            type(exc).__name__,
        )
        return defaults
    if not isinstance(parsed, dict):
        logger.error(
            "DISPATCH_SAFE_LIST is not a JSON object — fail-closed to empty defaults | parsed_type=%s",
            type(parsed).__name__,
        )
        return defaults
    parsed.setdefault("allowed_contact_ids", [])
    parsed.setdefault("allowed_group_ids",   [])
    parsed.setdefault("allowed_emails",      [])
    parsed.setdefault("label_overrides",     {})
    return parsed


def _route_send(selected_target_ids: list[str]) -> SendDecision:
    """Mode-aware routing decision for /send-notification.

    Full mode is unconditional live send — the safe list is NOT loaded
    (avoids unnecessary Secret Manager read at the cost of a slightly
    different code path; tested by mock-asserting `_load_safe_list_secret`
    is not called in full mode).

    Safe mode reads the safe-list and live-sends only when EVERY selected
    target is in `allowed_contact_ids ∪ allowed_group_ids`. A single
    off-safe ID in the selection drafts the entire send — partial-live is
    not permitted (would create per-recipient inconsistency that's hard
    to reason about during an incident).
    """
    if _EVERBRIDGE_MODE == "full":
        return SendDecision(
            action="send_live",
            target_ids=list(selected_target_ids),
        )

    safe = _load_safe_list_secret()
    allowed = (
        set(safe.get("allowed_contact_ids", []))
        | set(safe.get("allowed_group_ids", []))
    )
    # Frontend sends prefixed IDs (`c:<raw>` for contacts, `g:<raw>` for groups —
    # see _is_group / _strip_target_prefix in Task 1.10a); the safe-list secret
    # stores RAW Everbridge IDs without the prefix. Strip the prefix before the
    # membership check so a prefixed selection matches an unprefixed safe-list
    # entry (live-test 2026-04-27 surfaced this bug — every safe-list contact
    # was incorrectly drafted because "c:<id>" never matched "<id>").
    all_safe = all(_strip_target_prefix(tid) in allowed for tid in selected_target_ids)
    return SendDecision(
        action="send_live" if all_safe else "send_draft",
        target_ids=list(selected_target_ids),
    )


# ---------------------------------------------------------------------------
# Pure-logic helpers used by the /send-notification orchestration (Task 1.10b).
# Adding them now (Task 1.10a) so they're testable in isolation; the
# orchestration body wires them up in Task 1.10b.
# ---------------------------------------------------------------------------

# Map the dispatcher-selected template type to the Everbridge category ID. The
# frontend sends "incounty" or "mutualaid" — never the raw ID.
#
# The IDs are org-specific Everbridge record IDs and are CONFIGURATION, not
# source: they are of no use to another Everbridge customer, and published they
# describe this org's Everbridge estate. Read from the environment, resolved
# lazily inside _category_for() so importing this module never requires them.
_TEMPLATE_TYPE_TO_CATEGORY_ENV: dict[str, str] = {
    "incounty":  "EVERBRIDGE_CATEGORY_INCOUNTY",
    "mutualaid": "EVERBRIDGE_CATEGORY_MUTUALAID",
}


def _category_for(template_type: str) -> int:
    """Map 'incounty'|'mutualaid' → this org's Everbridge category ID.

    Raises ValueError on unrecognized input, and equally on an unset or
    non-integer env var, so the orchestration in /send-notification fails
    loudly rather than silently picking a default. Resolved here rather than at
    import so the module stays importable without the environment.
    """
    try:
        env_var = _TEMPLATE_TYPE_TO_CATEGORY_ENV[template_type]
    except KeyError:
        raise ValueError(
            f"Unrecognized template_type: {template_type!r}. "
            f"Expected one of: {sorted(_TEMPLATE_TYPE_TO_CATEGORY_ENV)}"
        )
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        raise ValueError(
            f"{env_var} env var not set. It is the Everbridge category ID for "
            f"{template_type!r} notifications; declare it in "
            f"terraform/environments/<env>/ and `terraform apply` (build scripts "
            f"do NOT pick up Terraform changes)."
        )
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"{env_var} must be an integer Everbridge category ID, got {raw!r}."
        )


# County of Santa Clara hosts SCCSSAR's Everbridge instance as a multi-tenant
# resource. County policy REQUIRES every notification title to begin with
# "SOSAR - " so County admins (auditing org-wide EB activity) can sort/filter
# by team. The notification's sentBy user is also tagged on every send, but
# that does not satisfy the policy — the prefix in the title is mandatory.
#
# DESIGN DECISION (do not revert without team discussion): server-side
# enforcement is mandatory. The frontend or a curl-harness dispatcher must
# never be trusted to comply. /send-notification prepends this prefix
# idempotently before any EB API call.
#
# History (2026-04-27): Three Phase 1 live-test notifications were sent
# without the prefix (titles like "TEST — please ignore — polling-chain
# smoke") because the test payloads composed by Claude omitted it. Bill
# flagged this as a county-policy violation. This enforcement was added
# in PR #<TBD> to ensure no future send can leave the prefix off.
_SOSAR_TITLE_PREFIX = "SOSAR - "


def _enforce_sosar_title_prefix(title: str) -> str:
    """Prepend the mandatory 'SOSAR - ' prefix idempotently.

    If the title already starts with the prefix, returns it unchanged
    (no double-prefixing). Empty/whitespace-only titles are still
    prefixed — caller is responsible for rejecting empty input via
    a separate validation step before this helper.
    """
    if title.startswith(_SOSAR_TITLE_PREFIX):
        return title
    return _SOSAR_TITLE_PREFIX + title


def _is_group(target_id: str) -> bool:
    """Heuristic: True iff `target_id` is a group ID (vs. a contact ID).

    SCCSSAR Everbridge group IDs and contact IDs are both numeric strings;
    they don't carry a type prefix. The frontend MUST tag IDs at selection
    time — for Phase 1 we use a literal `g:` prefix on group selections
    sent over the wire. This helper checks for that prefix.

    Why prefix-rather-than-numeric-range: group/contact ID ranges in
    Everbridge can overlap (and do — confirmed in PoC discover.py); only
    the selection source is authoritative.
    """
    return target_id.startswith("g:")


def _strip_target_prefix(target_id: str) -> str:
    """Strip the 'g:' or 'c:' frontend prefix to get the raw Everbridge ID."""
    for prefix in ("g:", "c:"):
        if target_id.startswith(prefix):
            return target_id[len(prefix):]
    return target_id


def _compose_event_name_with_hhmm(
    event_name_human: str,
    *,
    now_utc: datetime.datetime | None = None,
) -> str:
    """Append a Pacific-time HHMM uniqueness suffix to the human-form event name.

    Phase 0 Key Discovery: Everbridge event names MUST be unique per-org. The
    HHMM suffix guarantees uniqueness for same-day same-street incidents that
    happen within the same minute is vanishingly unlikely; if it ever does
    happen the second send will fail at create_notification_event time with
    a clear Everbridge error and the dispatcher can amend.

    `now_utc` parameter exists so tests can pin the time deterministically.
    Production callers pass nothing.
    """
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    pacific = now_utc.astimezone(zoneinfo.ZoneInfo("America/Los_Angeles"))
    return f"{event_name_human} {pacific.strftime('%H%M')}"


def _slugify_for_firestore(event_name_with_hhmm: str) -> str:
    """'2026-04-25 MPD CALAVERAS 1430' → '2026-04-25_mpd_calaveras_1430'.

    Same canonical key as Slack channel name (when not collision-suffixed) —
    keeps the cross-app identity 1:1. Used as the Firestore doc ID so a
    same-day same-street collision creates a different doc (HHMM differs).
    """
    return "_".join(event_name_with_hhmm.lower().split())


def _coerce_selected_target_ids(raw) -> list[str]:
    """Validate the `selected_target_ids` field from the request body.

    The frontend always sends a JSON array of strings, but a malformed
    or malicious client could send a string, dict, or list-of-mixed-
    types. Without this guard the downstream call
    `_route_send(selected_target_ids)` would iterate a string
    character-by-character (silent corruption of routing) or raise a
    generic AttributeError 500 on a dict.

    Returns the coerced list. Raises ValueError with a specific message
    on bad shape — caller translates to 400. Mirrored in
    test_endpoint_helpers.py.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("selected_target_ids must be a list")
    if not all(isinstance(t, str) for t in raw):
        raise ValueError("selected_target_ids elements must be strings")
    return raw


def _content_length_exceeds(header_value, max_bytes: int) -> bool:
    """Best-effort pre-check for oversized requests by Content-Length header.

    Returns True iff the header is present, parses to a valid integer, and
    exceeds max_bytes. Returns False on missing or malformed headers — those
    fall through to the post-parse authoritative length check at the call
    site. Content-Length is client-controlled and may be absent (chunked
    uploads) or spoofed, so this is a fast-reject for the common honest
    oversized-payload case. Mirrored in test_endpoint_helpers.py.
    """
    if header_value is None:
        return False
    try:
        return int(header_value) > max_bytes
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def _rss_mib() -> int:
    """Current resident set size in MiB.

    Reads /proc/self/statm directly — Linux-only, microsecond-fast, zero
    dependencies. On non-Linux (local macOS pytest) returns 0 so the
    instrumented log lines stay greppable without platform shims.

    Page size on Cloud Run (x86_64 Linux) is 4 KiB; the second statm field
    is resident pages. Multiply, divide to MiB. No PII — RSS is an int.
    """
    try:
        with open("/proc/self/statm", "r") as f:
            return int(f.read().split()[1]) * 4096 // (1024 * 1024)
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return 0


def _warm_firestore_clients() -> None:
    """Eagerly construct + touch both Firestore singletons (issue #522).

    Firestore transactions have a 60s lifetime. On a cold-deployed container
    the first request was occasionally racing that window — Firestore.Client()
    construction + ADC + gRPC handshake + transaction.commit exceeded 60s
    and the commit failed with InvalidArgument("transaction has expired").

    Warming both singletons here closes the race; the per-transaction retry
    in rate_limit.py is defense-in-depth. Warmup failures are non-fatal —
    the container must come up even if Firestore is briefly unhealthy at
    startup; rate_limit.py's retry catches whatever gets through.
    """
    from rate_limit import _get_db as _get_rate_limit_db
    try:
        _get_rate_limit_db().collection("ocr_usage_limits").limit(1).get()
        logger.info("rate_limit Firestore client warmed")
    except Exception as e:
        logger.warning(
            "rate_limit Firestore warmup failed (non-fatal): %s", type(e).__name__,
        )
    try:
        _get_eb_slack_db().collection("incidents").limit(1).get()
        logger.info("eb_slack Firestore client warmed")
    except Exception as e:
        logger.warning(
            "eb_slack Firestore warmup failed (non-fatal): %s", type(e).__name__,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # _warm_firestore_clients makes synchronous Firestore gRPC .get() calls
    # (the whole point — force lazy-init of the singleton + verify the
    # round-trip works before the first real request hits, per PR #527).
    # Wrap in run_in_executor so those blocking calls run in the default
    # thread pool rather than on the event loop. If Firestore is unreachable
    # at boot, each .get() can stall for the gRPC deadline (~60s default);
    # pre-fix that stall blocked the entire event loop, preventing any other
    # coroutine (including signal handlers) from running for the full
    # timeout window. The lifespan still awaits warmup before yielding, so
    # the cold-start benefit is preserved — only the event-loop-blocking
    # part is fixed. Batch-3 PR-G.2 (Failure-mode rubric Q2).
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _warm_firestore_clients)
    # Idle-baseline RSS after Firestore clients are warmed but before any
    # request runs. Diagnostic anchor for ongoing memory monitoring — drift
    # in this value across deploys indicates the import/init surface has
    # grown (new dependencies, larger module-level state). See scripts/oom-check.
    logger.info("startup rss_baseline_mib=%d", _rss_mib())
    # Config preflight. A WARNING, not a hard failure: OCR, CalTopo and Docs do
    # not need Everbridge, and refusing to boot would take the whole console down
    # for a missing EB variable. The individual EB guards still raise at use.
    for _name, _val in (
        ("EVERBRIDGE_ORG_ID", _EVERBRIDGE_ORG_ID),
        ("EVERBRIDGE_CATEGORY_INCOUNTY", os.environ.get("EVERBRIDGE_CATEGORY_INCOUNTY", "")),
        ("EVERBRIDGE_CATEGORY_MUTUALAID", os.environ.get("EVERBRIDGE_CATEGORY_MUTUALAID", "")),
        ("EVERBRIDGE_CALLER_ID", os.environ.get("EVERBRIDGE_CALLER_ID", "")),
        ("EVERBRIDGE_DELIVER_PATHS", os.environ.get("EVERBRIDGE_DELIVER_PATHS", "")),
    ):
        if not _val.strip():
            logger.warning(
                "startup config_missing=%s — Everbridge dispatch will fail until "
                "this env var is set (terraform apply; build scripts do NOT pick "
                "up Terraform changes)", _name
            )
    yield


app = FastAPI(
    title="SCCSSAR Dispatch Console",
    version="1.0.0-phase1",
    docs_url=None,   # Disable Swagger UI in production
    redoc_url=None,  # Disable ReDoc in production
    lifespan=lifespan,
)

# CORS — restrict to the deployed frontend origin in production.
# Set ALLOWED_ORIGINS env var to the Cloud Run frontend URL.
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:8080").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _allowed_origins],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def add_security_headers(request, call_next):
    """Inject security headers on every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    # Suppress FastAPI/uvicorn server header to reduce fingerprinting
    if "server" in response.headers:
        del response.headers["server"]
    return response

# ---------------------------------------------------------------------------
# Static frontend — serve index.html at "/"
# The frontend/ directory is copied into /app/frontend/ by the Dockerfile.
# ---------------------------------------------------------------------------

_frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
_index_html = os.path.join(_frontend_dir, "index.html")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_frontend():
    """Serve the dispatch console frontend (index.html).

    Cache-Control: no-cache tells the browser to always revalidate with the server
    before using a cached copy. Combined with Starlette's ETag header, this means:
    - Unchanged file → 304 Not Modified (fast, no re-download)
    - Changed file (new deploy) → 200 OK + new content (dispatcher gets new JS immediately)

    Without this header, browsers apply heuristic max-age caching and can silently
    serve stale JS for hours after a deploy — even while the /version footer correctly
    shows the new SHA (because /version is fetched fresh on every page load).
    """
    return FileResponse(
        _index_html,
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/health")
async def health():
    """
    Unauthenticated health check — required by Cloud Run startup/liveness probes.
    Returns no data beyond the 200 status code.
    """
    return PlainTextResponse("ok")


# Read version string once at startup from the file baked into the image by the Dockerfile.
#
# Two formats supported (PR-V 2026-06-03):
#   NEW:  "1.8.0 | 1.8 Slacker | a46cc57"  (VERSION | PHASE | GIT_SHA, pipe-delimited)
#   OLD:  "1.5o / a46cc57"                  (PHASE / GIT_SHA, slash-delimited)
#
# The " | " separator in the new format unambiguously distinguishes it from
# the old format. Old-format support stays in for transition safety — a
# Cloud Run revision built before this PR will still serve a parseable
# /version response (with version=<legacy_string>, phase/sha=None).
#
# The Dockerfile writes this file via:
#   echo "${VERSION} | ${PHASE} | ${GIT_SHA}" > /app/version.txt
# build-*.sh scripts pass:
#   --build-arg VERSION="$(cat VERSION)"   # repo-root VERSION file
#   --build-arg PHASE="1.8 Slacker"        # milestone codename
#   --build-arg GIT_SHA="$(git rev-parse --short HEAD)"
#
# DESIGN DECISION: version info is baked into the image at build time (not runtime
# env var) so it reflects the exact source that was compiled, not the environment
# it was deployed into.
_VERSION_FILE = os.path.join(os.path.dirname(__file__), "version.txt")


def _parse_version_file(raw: str) -> tuple[str, str | None, str | None]:
    """Parse version.txt content into (version, phase, sha).

    New 3-field format ("VERSION | PHASE | GIT_SHA"):  ("1.8.0", "1.8 Slacker", "a46cc57")
    Old 2-field format ("PHASE / GIT_SHA"):            (raw, None, None)   — fall-through display
    Empty/unknown:                                      ("unknown", None, None)

    Old-format fall-through returns the raw string as `version` so legacy footer
    display still renders something meaningful from an older Cloud Run revision.
    The frontend reads `phase` and `sha` separately when present.
    """
    raw = (raw or "").strip()
    if not raw:
        return ("unknown", None, None)
    if " | " in raw:
        parts = [p.strip() for p in raw.split(" | ")]
        if len(parts) >= 3:
            return (parts[0], parts[1], parts[2])
    # Legacy or unrecognized — return raw as version, no structured fields.
    return (raw, None, None)


try:
    with open(_VERSION_FILE) as _vf:
        _APP_VERSION_RAW = _vf.read()
except OSError:
    _APP_VERSION_RAW = ""              # local dev without a Docker build

_APP_VERSION, _APP_PHASE, _APP_SHA = _parse_version_file(_APP_VERSION_RAW)
logger.info(
    "App version: %s (phase=%s, sha=%s)",
    _APP_VERSION, _APP_PHASE or "n/a", _APP_SHA or "n/a",
)


@app.get("/version")
async def version():
    """
    Unauthenticated build version + feature-flag state.

    Returns JSON (PR-V 2026-06-03 shape):
      {
        "version":  "1.8.0",                   (dispatcher-facing semantic version)
        "phase":    "1.8 Slacker" | null,      (milestone codename)
        "sha":      "76605ad" | null,          (git short SHA — debug/tooltip use)
        "features": {"everbridge_slack": bool},
        "flags":    {"everbridge_mode": "safe|full|off",
                     "slack_mode":      "shadow|full|off"},
        "token_health": {...}
      }

    Backward-compat: when serving from an older Cloud Run revision (pre-PR-V
    build), `version` is the legacy "PHASE / GIT_SHA" string and `phase`/`sha`
    are null. The frontend handles both shapes.

    Used by the frontend footer (renders "<version> · EB: <mode> · Slack: <mode>"
    with phase + sha in a tooltip on hover) AND by the frontend feature-gate
    (hides new UI when features.everbridge_slack=false).

    No sensitive data exposed — version, phase, SHA, and flag values are
    already public (visible in the rendered footer).
    """
    project_id = os.environ.get("GCP_PROJECT", "")
    token_health = get_all_token_health(project_id) if project_id else {}
    return {
        "version": _APP_VERSION,
        "phase":   _APP_PHASE,
        "sha":     _APP_SHA,
        "features": {
            "everbridge_slack": _feature_enabled(),
        },
        "flags": {
            # Raw flag values for footer display. When unset (typical on SCCSSAR-dev
            # during Phase 1-3 before Phase 5 Step 5.1) → "off" so the footer reads
            # cleanly: "EB: off · Slack: off". This matches _feature_enabled() returning
            # False — the dispatcher knows the new feature isn't active in their env.
            "everbridge_mode": _EVERBRIDGE_MODE or "off",
            "slack_mode":      _SLACK_MODE      or "off",
            # Active staging POI source. Defaults to "overpass" (not "off") — staging
            # always has a source. The frontend footer shows the "Powered by Geoapify"
            # attribution only when this reads "geoapify".
            "staging_source":  _STAGING_SOURCE  or "overpass",
            # Shadow mode: "on" = parallel both + compare log (soak); "off" = sequential
            # active-source-only + fallback-on-failure (post-flip). Ops visibility only.
            "staging_shadow":  "on" if _STAGING_SHADOW else "off",
        },
        # Per-secret rotation expiry health (PR 4b). Empty dict locally / when
        # GCP_PROJECT unset; populated map at runtime per backend/secret_health.py
        # registry. Frontend reads this to show the yellow expiry banner.
        "token_health": token_health,
    }


@app.get("/verify")
async def verify(
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """
    Lightweight allowlist check called by the frontend immediately after Google Sign-In.

    Returns 200 OK if the ID token is valid and the email is in the dispatcher allowlist.
    Returns 401 (bad/expired token) or 403 (not allowlisted) via the dependency.

    Purpose: fail-fast at sign-in time so non-dispatchers see an access-denied message
    immediately after Google OAuth completes, rather than only when they attempt to submit
    a form. No expensive operations (no Gemini, no Firestore, no file I/O).
    """
    return JSONResponse({"ok": True})


@app.post("/ocr")
async def ocr(
    request: Request,
    file: UploadFile = File(...),
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """
    Upload a JPEG photo or typed PDF of a SAR call-out form.
    Returns a structured incident summary + map_data JSON.

    Supported formats:
      image/jpeg — v1 or v2 handwritten form photo → two-pass Gemini OCR pipeline
      application/pdf — v2 typed/digital form → AcroForm extraction (100% checkbox accuracy)
                        + single Gemini call for staging recommendations and Koester analysis

    DESIGN DECISION (do not revert): PDF and JPEG paths are separate accuracy tracks.
    PDF AcroForm extraction bypasses Gemini OCR for questionnaire parsing — 0% checkbox
    error rate. The two paths have different failure modes and must NOT be collapsed.
    See CLAUDE.md "v2 form ingest" for full rationale.

    Privacy guarantees (both paths):
    - File bytes exist only in memory during this request; never written anywhere.
    - Extracted text is returned to the caller only; never persisted server-side.
    - No PII is written to logs.
    """
    email = dispatcher.get("email", "")
    user_sub = dispatcher.get("sub", "unknown")  # stable Google user ID for logging

    t_ocr_start = time.monotonic()
    _rss_pre_mib = _rss_mib()  # Issue #519 instrumentation
    logger.info(
        "OCR request received | sub=%s content_length=%s rss_pre_mib=%d",
        user_sub,
        request.headers.get("content-length", "unknown"),
        _rss_pre_mib,
    )

    # Fast-reject oversized uploads before reading body into memory (M1).
    # Content-Length is client-supplied and may be absent or spoofed, so this is a
    # best-effort pre-check only. The authoritative enforcement is the post-read
    # len(raw_bytes) check in each path below.
    _cl_header = request.headers.get("content-length")
    if _cl_header:
        try:
            if int(_cl_header) > MAX_PDF_BYTES:  # 10 MB — same limit for both JPEG and PDF paths
                raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
        except ValueError:
            pass  # Malformed Content-Length — proceed; post-read check enforces the limit

    # Read all bytes so we can detect format before routing.
    raw_bytes = await file.read()
    is_pdf_upload = is_pdf(raw_bytes)

    # --- Format-specific validation and initial parsing ---
    # Both paths produce summary_pass1: the structured text block that feeds geocoding.
    # JPEG path: Gemini OCR (Pass 1) produces it.
    # PDF path:  AcroForm extraction + build_synthetic_summary() produces it server-side.
    image_bytes: bytes | None = None  # only set for JPEG path

    if is_pdf_upload:
        # PDF validation: size check (no dimension check — not an image)
        if len(raw_bytes) == 0:
            raise HTTPException(status_code=400, detail="Empty file")
        if len(raw_bytes) > MAX_PDF_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large (max {MAX_PDF_BYTES // 1_048_576} MB)",
            )

        # Rate limiting + AcroForm extraction wrapped in a single try/finally so
        # raw_bytes is always deleted — including when check_rate_limits raises 429/503 (L1).
        try:
            # Rate limiting (raises HTTPException 429/503 on limit exceeded)
            await check_rate_limits(email)
            fields = extract_acroform_fields(raw_bytes)
        except ValueError as exc:
            logger.warning("PDF AcroForm extraction failed | sub=%s", user_sub)
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            del raw_bytes

        # Build synthetic Pass 1 equivalent from extracted fields
        intake_timestamp = datetime.datetime.now(_PT).strftime("%Y-%m-%d %H:%M")
        full_name = dispatcher.get("name", "").strip()
        dispatcher_last_name = full_name.split()[-1] if full_name else ""
        summary_pass1 = build_synthetic_summary(fields, dispatcher_last_name, intake_timestamp)
        logger.info("PDF AcroForm parsed | sub=%s fields=%d", user_sub, len(fields))

    else:
        # JPEG path: validate as image, then do Pass 1 OCR
        try:
            image_bytes = validate_jpeg_bytes(raw_bytes)
        finally:
            del raw_bytes

        # Rate limiting (raises HTTPException 429/503 on limit exceeded)
        await check_rate_limits(email)

    # Step 14: Nominatim geocode + Overpass POI lookup + Gemini staging call.
    #
    # JPEG path: Pass 1 OCR (no coords) → geocode → Overpass → Pass 2 Gemini (with image + coords)
    # PDF path:  synthetic summary → geocode → Overpass → Gemini staging+Koester (text-only, 1 call)
    #
    # The geocoding and Overpass sections below are identical for both paths — both produce
    # the same labeled field format that the regex parsers expect.
    geo = None
    geo_res = None
    res_lat: float | None = None
    res_lng: float | None = None
    staging_candidates: list = []
    _overpass_school_count: int = 0
    _overpass_church_count: int = 0
    # Default True ("no failure observed") — set False only when all Overpass
    # mirrors return errors. Used to gate the advisory-form exclusion note
    # when we can't enumerate schools/churches but the time-policy still
    # applies (Bill 2026-05-20 live-test feedback: dispatcher needs to know
    # the policy is in force even when the Overpass count is unavailable).
    _overpass_ok: bool = True
    _lkp_from_residence = False  # True when Residence used as LKP geocode fallback (LKP not provided)

    try:
        if not is_pdf_upload:
            # --- JPEG Pass 1: OCR without geocode ---
            summary_pass1 = await extract_incident_summary(image_bytes, lkp_coords="")

        # Parse "Last Known Position:" from first-pass output (may be corrected address)
        loc_match = re.search(r"^Last Known Position:\s*(.+)", summary_pass1, re.MULTILINE)
        lkp_address = loc_match.group(1).strip() if loc_match else None

        # Handle "LOCATION_NAME NNNN STREET, ..." format — when the LKP starts with a
        # location name followed by a street address, strip the leading name so Nominatim
        # gets a clean geocodable address.  This arises on the PDF path when an officer
        # fills in a two-line LKP (location name on line 1, street on line 2) — those
        # lines are joined with a space in pdf_extract.py before building the summary.
        # Example: "GOOD SAM HOSPITAL 2000 SAMARITAN DR. SAN JOSE, CA 95124"
        #       →  "2000 SAMARITAN DR. SAN JOSE, CA 95124"
        if lkp_address and not re.match(r"^\d", lkp_address):
            _embedded = re.search(r"\b(\d+\s+\w)", lkp_address)
            if _embedded:
                lkp_address = lkp_address[_embedded.start():]
                logger.info("LKP location-name prefix stripped for geocoding")

        # --- Build geocode query: append city/state context to prevent global ambiguity ---
        # City/state extraction uses a priority cascade:
        #   1. Already embedded — lkp_address contains a comma (city likely already present)
        #   2. Residence Address field — subject's home address often includes city when LKP doesn't
        #   3. Agency abbreviation lookup — e.g. SJPD → San Jose, CA
        #   4. Fall back to bare street address (logs a warning; geocoding may fail or be wrong)
        city_context = None
        if lkp_address and "," in lkp_address:
            # City is already embedded in lkp_address — no need to append
            pass
        else:
            # Priority 2: Residence Address
            res_match = re.search(r"^Residence Address:\s*(.+)", summary_pass1, re.MULTILINE)
            res_address = res_match.group(1).strip() if res_match else None
            if res_address and res_address.lower() != "not recorded":
                parts = res_address.split(",")
                if len(parts) >= 2:
                    candidate = ",".join(parts[1:]).strip()  # "San Jose, CA" or "San Jose, CA 95132"
                    # Reject short abbreviations like "SJ", "SJC", "LA" — officers often write
                    # city abbreviations that Nominatim can't use. Short values block the better
                    # agency-lookup (Priority 3) from running and produce wrong-country results.
                    if len(candidate) > 3:
                        city_context = candidate

            # Priority 3: Agency abbreviation → city
            # Use (.+?) to capture the full Agency field value — supports both abbreviations
            # ("SJPD", "MPD") and full city names ("MILPITAS", "MOUNTAIN VIEW") that officers
            # may type into the free-text Agency field on the v2 PDF form.
            if not city_context:
                agency_match = re.search(r"^Agency:\s*(.+?)$", summary_pass1, re.MULTILINE)
                agency = agency_match.group(1).strip().upper() if agency_match else ""
                city_context = _AGENCY_CITY.get(agency)
                if city_context:
                    logger.info("City derived from agency %r → %r", agency, city_context)

        # Guard: skip geocoding when LKP is a placeholder value rather than a real address.
        # "[not recorded]" appears in the PDF path when the officer left the field blank.
        # Passing a placeholder to Nominatim wastes a network call and always fails, producing
        # a misleading log warning. Treat these values the same as a missing LKP.
        # Check lkp_address (not geocode_query) so city-context appended values are also caught.
        _LKP_PLACEHOLDER_RE = re.compile(
            r"^\[?(?:not\s+recorded|not\s+provided|unknown|n/?a)\]?$",
            re.IGNORECASE,
        )
        if lkp_address and _LKP_PLACEHOLDER_RE.match(lkp_address.strip()):
            logger.warning("LKP is a placeholder value — skipping geocode")
            lkp_address = None

        # Assemble final geocode query
        geocode_query = lkp_address or ""
        if lkp_address and city_context and "," not in lkp_address:
            geocode_query = f"{lkp_address}, {city_context}"

        # DESIGN DECISION: SCCSSAR operates entirely within Santa Clara County, CA.
        # If the geocode query has no state component, append ", CA" to anchor Nominatim
        # to California. Without this, Spanish-language street names (San Felipe, San Jose,
        # Santa Teresa, etc.) resolve to Costa Rica, Mexico, or Spain instead of California.
        # Officers should not need to write the state — it is always CA for this team.
        #
        # This suffix is the ADVISORY half only. `CA` is also the ISO 3166-1 code
        # for Canada, so on a query with no other signal Nominatim can resolve the
        # country: `"Treatment Facility, CA"` returned Bedford, Nova Scotia on a
        # real callout (2026-07-31), and `"10410, CA"` returns Jakarta — see the
        # bare-house-number guard immediately below, which was written to work
        # around that same ambiguity one input shape at a time. The BINDING half
        # is `countrycodes=us` on the request itself (#667, _geocode_nominatim).
        # Keep both: the parameter fixes the country, the suffix still
        # disambiguates within the US.
        # This fires when: (a) LKP has no city/state, (b) Residence had only a short
        # abbreviation and agency lookup also failed, or (c) officer typed city but no state.
        if geocode_query and not re.search(r"\b(CA|California)\b", geocode_query, re.IGNORECASE):
            geocode_query = f"{geocode_query}, CA"
            logger.info("State absent — appended CA to geocode query")

        # Guard: a bare house number (no street name) is not geocodable and Nominatim
        # may return plausible-looking but wrong results (e.g. resolving "10410, CA" to
        # coordinates in Jakarta because Nominatim interprets "CA" globally).
        # When LKP is just digits, substitute the Residence address if available — the
        # officer's staging location is typically the same street as the residence.
        # Geocode-query-only change: the LKP field in the dispatcher textarea is preserved.
        if lkp_address and re.match(r"^\d+\s*$", lkp_address):
            _res_subst_m = re.search(r"^Residence Address:\s*(.+)", summary_pass1, re.MULTILINE)
            _res_subst = _res_subst_m.group(1).strip() if _res_subst_m else None
            if _res_subst and _res_subst.lower() not in ("not recorded", "unknown", ""):
                logger.warning("LKP is bare house number — substituting Residence for geocoding")
                geocode_query = _res_subst
                if city_context and "," not in geocode_query:
                    geocode_query = f"{geocode_query}, {city_context}"
                if not re.search(r"\b(CA|California)\b", geocode_query, re.IGNORECASE):
                    geocode_query = f"{geocode_query}, CA"
            else:
                logger.warning("LKP is bare house number with no Residence fallback — geocoding may fail")

        if not geocode_query:
            logger.warning("No LKP address found in Pass 1 output — skipping geocode")

        # --- Nominatim geocoding — LKP and Residence in parallel (best-effort, non-fatal) ---
        # Residence geocoded in parallel with LKP to add ~0 wall-clock time.
        # DESIGN DECISION (do not revert): always geocode Residence even if same as LKP.
        # Two markers are shown on the map even if they overlap — belt-and-suspenders thoroughness.
        res_match_p1 = re.search(r"^Residence Address:\s*(.+)", summary_pass1, re.MULTILINE)
        res_address_p1 = res_match_p1.group(1).strip() if res_match_p1 else None

        # Strip apartment/unit qualifiers before passing to Nominatim.
        # Nominatim geocodes against OSM street data — unit numbers are not in OSM and
        # cause the geocode to fail, falling back to LKP coords (PR #13 fallback).
        # Stripping them gives Nominatim the base street address it can actually match.
        # Patterns covered:
        #   "123 Main St, Apt #2, ..."   → "123 Main St, ..."
        #   "123 Main St, Unit 5B, ..."  → "123 Main St, ..."
        #   "123 Main St, Suite 100, ..." → "123 Main St, ..."
        #   "123 Main St #2, ..."         → "123 Main St, ..."
        #   "123 Main St, #2, ..."        → "123 Main St, ..."
        # _APT_STRIP_RE is defined at module level (PR-D-1) so the dispatcher staging
        # override endpoint can reuse it for streetname extraction. Drift between the
        # two callers would silently produce different results.
        res_address_geocode = (
            re.sub(r"\s*,\s*,", ",", _APT_STRIP_RE.sub("", res_address_p1)).strip(" ,")
            if res_address_p1 else None
        )
        if res_address_p1 and res_address_geocode != res_address_p1:
            logger.info("Residence apt/unit stripped for geocode")

        residence_query = (
            res_address_geocode
            if res_address_geocode and res_address_geocode.lower() not in ("not recorded", "unknown", "")
            else None
        )

        # Apply the same city-abbreviation rejection and CA-append to Residence geocoding
        # that was applied to LKP geocoding (PR #145). Officers write city abbreviations
        # like "SJ" in the Residence field — these are ≤3 chars, pass apt-stripping
        # unchanged, and either fail Nominatim outright or produce wrong-country results.
        # "2000 GAZELLE DR, SJ" → drop "SJ" → "2000 GAZELLE DR" → append CA →
        # "2000 GAZELLE DR, CA" — gives Nominatim the best chance of resolving correctly.
        # SCCSSAR operates entirely in Santa Clara County, CA. Residences are always in CA.
        if residence_query:
            _res_parts = [p.strip() for p in residence_query.split(",")]
            _res_parts_clean = [
                p for p in _res_parts
                if p and not (len(p) <= 3 and p.replace(" ", "").isalpha())
            ]
            if _res_parts_clean:
                residence_query = ", ".join(_res_parts_clean)
            if not re.search(r"\b(CA|California)\b", residence_query, re.IGNORECASE):
                residence_query = f"{residence_query}, CA"
                logger.info("Residence: state absent — appended CA to geocode query")

        # Residence-as-LKP fallback: when the officer left the LKP field blank (PDF path
        # outputs "[not recorded]"), use the Residence address as the geocoding anchor so
        # staging, Koester, and the CalTopo map can still be generated.
        # "Something is better than nothing" — the residence is the most operationally
        # useful nearby anchor available when the officer hasn't specified an LKP.
        # A WARNING is added to the Event Log so the dispatcher confirms actual LKP.
        if not geocode_query and residence_query:
            geocode_query = residence_query
            _lkp_from_residence = True
            logger.warning("LKP not recorded — falling back to Residence address for geocoding/staging")

        # Apply the same apt/unit stripping to the LKP geocode query.
        # PR #25 added _APT_STRIP_RE for Residence but not LKP. When LKP == Residence
        # (e.g. "1200 East Calaveras Blvd Apt #2, Milpitas, CA"), the LKP geocode fails
        # intermittently because Nominatim can't resolve unit numbers in OSM — causing
        # geo=None, which silently drops the LKP marker, officer staging, and all
        # Overpass candidates from the CalTopo map (phantom "SAR Incident" at 0,0).
        if geocode_query:
            _lkp_stripped = re.sub(r"\s*,\s*,", ",", _APT_STRIP_RE.sub("", geocode_query)).strip(" ,")
            if _lkp_stripped != geocode_query:
                logger.info("LKP apt/unit stripped for geocode")
                geocode_query = _lkp_stripped

        # event_log_additions is initialised here (before geocoding) so that address
        # correction entries — detected during LKP / Residence geocoding — can be
        # accumulated and later injected into the Event Log alongside staging warnings.
        # The staging marker loop (below) appends further entries to the same list.
        event_log_additions: list[str] = []
        # _street_corrections accumulates (input_component, corrected_component) tuples
        # from Google Maps geocoding.  They are applied to `summary` in a single pass
        # AFTER `summary` is first assigned (post-Gemini), because `summary` does not
        # exist yet during the geocoding phase.
        _street_corrections: list[tuple[str, str]] = []
        # Initialised here, not inside the `if geo:` branch below — when
        # geocoding fails outright that branch never runs, and map_data reads
        # this unconditionally.
        _lkp_locality_suspect = False

        # _geocode_lkp_smart routes intersection queries (e.g. "5th St & St John St,
        # San Jose, CA") directly to Google Maps because Nominatim cannot resolve
        # intersections. Non-intersection LKPs use Nominatim primary (existing fast
        # path). Residence queries are vanishingly rarely intersections so they keep
        # the plain Nominatim path; the existing Google Maps fallback below catches
        # both LKP and Residence misspellings independently.
        geo_tasks = [
            _geocode_lkp_smart(geocode_query) if geocode_query else asyncio.sleep(0, result=None),
            _geocode_nominatim(residence_query) if residence_query else asyncio.sleep(0, result=None),
        ]
        geo, geo_res = await asyncio.gather(*geo_tasks)

        if geo:
            # City-consistency guard (issue #604) — runs on the Nominatim SUCCESS
            # path, which is where the 2026-07-24 wrong-city dispatch originated.
            # Must precede the staging lookup and CalTopo seeding below, which both
            # consume these coordinates.
            geo, _lkp_city_note, _lkp_locality_suspect = await _reconcile_geocode_city(
                geocode_query, geo, "LKP"
            )
            if _lkp_city_note:
                event_log_additions.append(_lkp_city_note)
            lat, lng, neighborhood, _ = geo
            logger.info("Geocoded LKP")
        else:
            logger.warning("Nominatim geocoding failed")
            # Google Maps fallback — better spelling-correction tolerance than Nominatim.
            # Resolves misspelled street names that Nominatim rejects entirely
            # (e.g. officer wrote "TRADEN DR" instead of "TRADAN DR").
            if geocode_query and _GOOGLE_MAPS_API_KEY:
                _gm_lkp = await _geocode_google_maps(geocode_query)
                if _gm_lkp:
                    _gm_lat, _gm_lng, _gm_display, _gm_formatted, _gm_loctype = _gm_lkp
                    # Issue #680: Google tells us how it resolved the query and
                    # we used to discard it. APPROXIMATE means it matched an
                    # AREA (a city or a state) rather than a place — which is
                    # what "Treatment Facility" did on 2026-07-31, landing on
                    # the centroid of California while every downstream
                    # consumer treated it as the search anchor.
                    #
                    # location_type, NOT partial_match — measured against the
                    # 18 real LKP strings in the corpus by
                    # experiments/geocode_confidence/01_partial_match_real_lkps.py.
                    # partial_match fires on 5 of 16 CORRECT in-area results:
                    # a hospital name carrying a good address, a successful
                    # spelling correction, an intersection, and a park. Warning
                    # on those is noise, and the Event Log is already crowded.
                    # APPROXIMATE fired on 2 of 18 and both genuinely lacked a
                    # specific location ("Treatment Facility", "SAN JOSE,CA").
                    if _gm_loctype == "APPROXIMATE":
                        event_log_additions.append(
                            _lkp_low_confidence_note(geocode_query, _gm_formatted)
                        )
                    # Guard: reject when Google Maps changed the house number.
                    # A house-number change (e.g. "300 MORETTE LANE" → "447 Great Mall Dr")
                    # means Google geocoded a different nearby address, not a spelling fix.
                    # Let geo stay None so the LKP-geocoding-failure WARNING fires below.
                    if not _house_number_consistent(geocode_query, _gm_formatted):
                        logger.warning("Google Maps changed house number — LKP geocode rejected")
                    else:
                        geo = (_gm_lat, _gm_lng, _gm_display, _extract_query_city(_gm_formatted))
                        logger.info("Google Maps geocoded LKP")
                        _correction, _rejected = _street_correction_note(
                            geocode_query, _gm_formatted, "LKP address"
                        )
                        if _rejected:
                            event_log_additions.append(_rejected)
                        if _correction:
                            _input_street = geocode_query.split(",")[0].strip()
                            # Only log and propagate substantive misspelling fixes (e.g. TRADEN →
                            # Tradan). Skip abbreviation-only normalisations (EAST → E, Blvd →
                            # Boulevard) — those are Google Maps format preferences, not officer
                            # errors, and should not appear in the dispatcher Event Log.
                            if _is_substantive_street_correction(_input_street, _correction):
                                event_log_additions.append(
                                    f"LKP address corrected: \"{_input_street}\" → \"{_correction}\" "
                                    f"(Google Maps verified spelling — verify with officer if incorrect)"
                                )
                                # Queue the correction for application after `summary` is assembled.
                                # `summary` doesn't exist yet at this point in the code flow
                                # (it's first assigned after the Gemini calls complete).
                                _street_corrections.append((_input_street, _correction))
                else:
                    logger.warning("Google Maps geocoding also failed for LKP")

        if geo_res:
            # Same guard for Residence — on 2026-07-24 both fields carried the same
            # address and both resolved to the wrong city. The note is suppressed when
            # the two queries are byte-identical (the common "last seen at home" case
            # and the LKP-from-Residence fallback), so one bad geocode produces one
            # warning rather than two near-duplicates.
            geo_res, _res_city_note, _ = await _reconcile_geocode_city(
                residence_query, geo_res, "Residence"
            )
            if _res_city_note and residence_query != geocode_query:
                event_log_additions.append(_res_city_note)
            res_lat, res_lng, _, _ = geo_res
            logger.info("Geocoded Residence")
        else:
            res_lat, res_lng = None, None
            if residence_query:
                logger.warning("Nominatim geocoding failed for Residence")
                # Google Maps fallback for Residence (same correction-detection logic as LKP)
                if _GOOGLE_MAPS_API_KEY:
                    _gm_res = await _geocode_google_maps(residence_query)
                    if _gm_res:
                        _gm_res_lat, _gm_res_lng, _gm_res_display, _gm_res_formatted, _ = _gm_res
                        # Guard: reject when Google Maps changed the house number —
                        # same rationale as LKP block above.
                        if not _house_number_consistent(residence_query, _gm_res_formatted):
                            logger.warning("Google Maps changed house number — Residence geocode rejected")
                        else:
                            res_lat, res_lng = _gm_res_lat, _gm_res_lng
                            logger.info("Google Maps geocoded Residence")
                            _res_correction, _res_rejected = _street_correction_note(
                                residence_query, _gm_res_formatted, "Residence address"
                            )
                            if _res_rejected and residence_query != geocode_query:
                                event_log_additions.append(_res_rejected)
                            if _res_correction:
                                _res_input_street = residence_query.split(",")[0].strip()
                                # Same abbreviation-only guard as LKP — skip non-substantive
                                # normalisations (EAST → E, Street → St, etc.).
                                if _is_substantive_street_correction(_res_input_street, _res_correction):
                                    event_log_additions.append(
                                        f"Residence address corrected: \"{_res_input_street}\" → "
                                        f"\"{_res_correction}\" "
                                        f"(Google Maps verified spelling — verify with officer if incorrect)"
                                    )
                                    # Queue for post-Gemini application (same reason as LKP above).
                                    _street_corrections.append((_res_input_street, _res_correction))
                    else:
                        logger.warning("Google Maps geocoding also failed for Residence")

        # --- Staging POI lookup (best-effort, non-fatal — needs geocode to succeed first) ---
        # Source-agnostic dispatcher: Overpass or Geoapify per STAGING_SOURCE, with a
        # symmetric shadow. Variable names keep the _overpass_ prefix — they now hold
        # the ACTIVE source's result; the WARNING semantics below are unchanged.
        if geo:
            lat, lng, neighborhood, _ = geo
            staging_candidates, _overpass_school_count, _overpass_church_count, _overpass_ok = (
                await _query_staging_pois(lat, lng, radius_m=1200)
            )
            # Zero candidates at 1200 m is a RENDERING outcome, not necessarily a
            # remote LKP — see _STAGING_FALLBACK_RADIUS_M. Retry once, wider, ONLY
            # on zero: the common path is untouched, so nothing new competes for the
            # 7-slot cap or the provider's 100-feature cap. Supplying candidates at
            # all is the real prize — it switches Gemini out of training-data mode,
            # whose fabricated list is not reproducible run-to-run (15/17 corpus
            # forms share NO addresses across identical runs) and whose distances
            # are false (claimed <=0.75 mi, actual median 4.3 mi). #838
            _staging_searched_m = 1200
            if _overpass_ok and not staging_candidates:
                # wide=True also widens the CATEGORY list, not just the radius
                # (ops #839): fire stations and PDs join the search here and
                # nowhere else. Bill 2026-09-07 — we do not want to stage at a
                # PD or FD, but at this point the alternative is not a better
                # location, it is Gemini inventing one.
                _wide_cands, _wide_sc, _wide_cc, _wide_ok = await _query_staging_pois(
                    lat, lng, radius_m=_STAGING_FALLBACK_RADIUS_M, wide=True
                )
                logger.info(
                    "Staging widened retry | radius_m=%d count=%d source_ok=%s",
                    _STAGING_FALLBACK_RADIUS_M, len(_wide_cands), _wide_ok,
                )
                if _wide_ok:
                    _staging_searched_m = _STAGING_FALLBACK_RADIUS_M
                    if _wide_cands:
                        staging_candidates = _wide_cands
                        _overpass_school_count = _wide_sc
                        _overpass_church_count = _wide_cc
                        event_log_additions.append(
                            "Note: No staging POIs within 0.75 mi of the LKP — search widened "
                            f"to {_STAGING_FALLBACK_RADIUS_M / 1609.34:.2f} mi. Recommendations "
                            "below are farther out than usual; confirm travel time with the "
                            "officer before dispatching."
                        )

            if not _overpass_ok:
                logger.error(
                    "All Overpass mirrors failed — staging recommendations will be based on "
                    "Gemini training data, not real-time POI lookup | sub=%s",
                    dispatcher.get("sub", "unknown"),
                )
                event_log_additions.append(
                    "WARNING: Nearby location servers unavailable — staging recommendations "
                    "below are estimates only and may not reflect the closest options. "
                    "Review carefully and confirm locations with the officer before dispatching."
                )
            elif not staging_candidates:
                # Overpass SUCCEEDED but found zero POIs within the radius — a
                # genuinely remote/rural LKP (e.g. Joseph D. Grant County Park in
                # the Mt. Hamilton foothills, 2026-07-16). Distinct from the
                # failure case above: without a note, a lone officer-designated
                # staging entry looks unexplained and the dispatcher can't tell
                # "no options exist here" from "the lookup silently broke."
                logger.info(
                    "Staging source returned zero candidates (remote LKP) | searched_m=%d",
                    _staging_searched_m,
                )
                event_log_additions.append(
                    f"Note: No staging POIs found within {_staging_searched_m / 1609.34:.2f} mi "
                    "of the LKP (remote area) — staging options limited; confirm "
                    "officer-designated staging or set manually."
                )

        # --- Pass 2 (JPEG) / Single Gemini call (PDF): staging + Koester analysis ---
        if geo:
            lat, lng, neighborhood, _ = geo
            lkp_coords_str = f"{lat:.5f},{lng:.5f} ({neighborhood})"
        else:
            lkp_coords_str = ""

        if is_pdf_upload:
            # PDF path: single text-only Gemini call — produces only Staging + Koester sections.
            # summary_pass1 already has Initial Incident Summary + Event Log + LPB Questionnaire.
            # Assemble the full output by appending the Gemini staging+Koester output.
            if geo:
                staging_output = await extract_staging_and_koester(
                    structured_context=summary_pass1,
                    lkp_coords=lkp_coords_str,
                    staging_candidates=staging_candidates if staging_candidates else None,
                )
                # Defensive strip: despite the "Do NOT reproduce" instruction, Gemini sometimes
                # echoes back the entire __SYNTHETIC_SUMMARY__ context before outputting the
                # staging sections.  Find the actual "Staging Area Recommendations:" header and
                # discard everything before it so the pre-extracted data is not duplicated.
                _sar_marker = "Staging Area Recommendations:"
                sar_start = staging_output.find(_sar_marker)
                if sar_start > 0:
                    logger.warning(
                        "PDF staging: Gemini reproduced pre-extracted data (%d chars) — stripping",
                        sar_start,
                    )
                    staging_output = staging_output[sar_start:]
                elif sar_start == -1:
                    logger.warning(
                        "PDF staging: Gemini response contained no 'Staging Area Recommendations:' — omitting staging"
                    )
                    staging_output = ""
                summary = f"{summary_pass1}\n---\n{staging_output.strip()}" if staging_output else summary_pass1
            else:
                # Geocoding failed — return summary_pass1 without staging/Koester
                summary = summary_pass1
                logger.warning(
                    "Geocoding unavailable — PDF path omitting staging/Koester | sub=%s", user_sub
                )
        else:
            # JPEG path: Pass 2 re-runs Gemini with the image + real coords + candidates.
            if geo:
                summary = await extract_incident_summary(
                    image_bytes,
                    lkp_coords=lkp_coords_str,
                    staging_candidates=staging_candidates if staging_candidates else None,
                )
            else:
                # Geocoding failed — use pass 1 result as-is (staging less accurate but not fatal)
                summary = summary_pass1
                logger.warning(
                    "Geocoding unavailable — using single-pass OCR result | sub=%s", user_sub
                )

    except RuntimeError:
        raise HTTPException(status_code=502, detail="OCR service temporarily unavailable")
    finally:
        # Release image bytes if the JPEG path allocated them
        if image_bytes is not None:
            del image_bytes

    # Apply any street spelling corrections queued during geocoding now that `summary`
    # has been fully assembled by the Gemini calls above.  Each correction is idempotent
    # (no-op if a prior correction already fixed the same misspelling).  This also covers
    # the JPEG path where summary_pass1 contains the Gemini-OCR'd text.
    for _sc_input, _sc_corrected in _street_corrections:
        summary = _apply_street_correction_to_summary(summary, _sc_input, _sc_corrected)

    # Strip any markdown code fences Gemini may have added despite the prompt prohibition.
    # Matches ```text ... ``` or ``` ... ``` at start/end of the response (with optional newline).
    summary = re.sub(r"^```[a-z]*\n?", "", summary.strip())
    summary = re.sub(r"\n?```$", "", summary.strip())

    # Collapse any leaked "UNCERTAIN → NOT ANSWERED (flag for follow-up)" to the display form.
    # Gemini is supposed to do this substitution itself, but sometimes outputs the raw template.
    summary = summary.replace(
        "UNCERTAIN → NOT ANSWERED (flag for follow-up)",
        "NOT ANSWERED (flag for follow-up)"
    )

    # Normalize parking range hyphens to en-dashes (Gemini sometimes outputs - instead of –).
    # Matches patterns like "~5-10 vehicles" or "~15-20 vehicles" inside parking estimates.
    summary = re.sub(r"(~\d+)-(\d+)", r"\1–\2", summary)

    # Defensive recompute of Gemini's parenthesized age hint on the DOB line.
    # Gemini's age computation is non-deterministic — same form, same prompt,
    # different runs produce different age numbers (2026-05-09 SJSU re-run
    # emitted "21 years old" for DOB 2005-10-20 when the actual age that day
    # was 20). The DOB date itself is reliable; only the (N years old) hint
    # drifts. The frontend's `_ebParseAgeFromDob()` at index.html:1055 reads
    # from this hint, so correcting it here propagates to mp_age → Slack
    # welcome MP line and every other surface that renders the summary.
    # Helper is silent unless it actually corrects an age (per "emit only for
    # corrections and failures" Locked Decision in CLAUDE.md).
    _today_pt = datetime.datetime.now(_PT).date()
    summary, _age_correction = _rewrite_dob_age_hint(summary, _today_pt)
    if _age_correction is not None:
        _old_age, _new_age = _age_correction
        event_log_additions.append(
            f"Age recomputed from DOB: {_old_age} → {_new_age}"
        )

    # Normalize "Last Seen At:" value for the JPEG path.
    # Officers write timestamps in many formats: "2130 HOURS", "2/20/26 2130 HOURS", "0445 AM".
    # The PDF path normalizes via _normalize_datetime() in pdf_extract.py at AcroForm read time.
    # For JPEG forms, Gemini returns the raw officer handwriting verbatim — apply the same
    # normalization server-side so both paths produce consistent output.
    def _normalize_last_seen_at(m: re.Match) -> str:
        normalized = _normalize_datetime(m.group(1).strip())
        return f"Last Seen At: {normalized}"
    summary = re.sub(r"^Last Seen At:\s*(.+)$", _normalize_last_seen_at, summary, flags=re.MULTILINE)

    # Issue #755 — chronology entry for when the SUBJECT was last seen. Must run
    # AFTER the normalization above, which is the JPEG path's only cleanup pass.
    summary = _insert_subject_last_seen_entry(summary)

    # "Staging Area for Resources:" arrives as part of Gemini's IIS output (or the
    # synthetic IIS from pdf_extract for the v2 PDF path) carrying the officer's
    # written staging text. It is consumed INTERNALLY by the staging pipeline (PASS 3
    # server-side injection at the bottom of this block, plus the staging-area
    # mismatch check farther down). After those consumers run, the line's content
    # is REPLACED with the #1 quality-ranked recommendation's bare form, so both
    # display surfaces (WhatsApp + Full) show the same staging answer (issue #244).
    # See the comment above the replacement (around the WhatsApp block) for the
    # full rationale and ordering constraint.

    # Normalize known Gemini OCR misreads of handwritten park/location names.
    # DESIGN DECISION: server-side normalization is the right place for known recurring misreads
    # that cannot be fixed purely through prompt engineering.
    #
    # Cardoza Park, Milpitas — HOURS of debugging history:
    #   The handwritten form writes "CARDOZA PARK MILPITAS" (no comma, all caps, cursive).
    #   Gemini consistently misreads the first letter cluster as "Carozza" or "Carocza".
    #   The correct park name is "Cardoza Park" — this is a real park in Milpitas, CA.
    #   The comma after "Park" is missing on the form; Gemini should add "Milpitas" as city.
    #   Server-side normalization handles this deterministically so prompt fixes aren't needed.
    #   If this park name ever changes, update both this normalization and the prompt example below.
    summary = re.sub(
        r"\bCar[ao]z+a\s+Park\b",
        "Cardoza Park",
        summary,
        flags=re.IGNORECASE,
    )

    # Staging section post-processing:
    #
    # DESIGN DECISION (issue #244): Dispatcher selects final staging location.
    # All entries are quality-ranked recommendations from Gemini; no structural slots.
    # Officer-designated entry (if present) is labeled by Gemini wherever it falls in
    # the ranked list, or appended at the end if the officer's location wasn't a candidate.
    # LKP/Residence no longer appears in the staging list — they are on CalTopo already.
    #
    # PASS 1 — Park address strip: if a numbered line has "NNN Street Address — Park Name",
    #   strip the street address so parks display as "Park Name, City" (name-only).
    #   Parks have multiple entrances; a specific street address is often wrong or misleading.
    #   DESIGN DECISION (do not revert): parks navigated by name, not street address.
    #
    # PASS 2 — Filter and renumber: all entries validated and renumbered 1–7.
    #   Keep: entries with a street address+name (" — " separator), or park/open space.
    #   Drop: entries with no " — " separator AND not a park (e.g. "Silicon Valley University").
    #   Cap: total list at 7 entries.
    #
    _PARK_AMENITY = re.compile(
        r"\b(?:park|sports center|recreation center|rec center|open space|"
        r"community center|field|athletic|baseball|soccer|picnic|trail|preserve|"
        r"reservoir|lake|creek|garden|greenbelt)\b",
        re.IGNORECASE,
    )

    def _is_park_line(body: str) -> bool:
        return bool(_PARK_AMENITY.search(body))

    def _strip_park_address(body: str) -> str:
        """If body is 'STREET ADDRESS — Park Name, City. details', strip the address portion."""
        if " — " not in body:
            return body
        parts = body.split(" — ", 1)
        addr_part = parts[0].strip()
        name_part = parts[1].strip()
        if re.match(r"^\d+", addr_part) and _PARK_AMENITY.search(name_part):
            return name_part
        return body

    try:
        staging_start = summary.find("\nStaging Area Recommendations:\n")
        staging_end   = summary.find("\n---\n", staging_start + 1) if staging_start != -1 else -1
        if staging_start != -1 and staging_end != -1:
            before  = summary[:staging_start + 1]
            section = summary[staging_start + 1:staging_end]
            after   = summary[staging_end:]

            # PASS 1: Park address stripping
            stripped_lines = []
            for line in section.splitlines():
                m = re.match(r"^(\d+)\.\s+(.+)$", line)
                if m:
                    body = _strip_park_address(m.group(2))
                    stripped_lines.append(f"{m.group(1)}. {body}")
                else:
                    stripped_lines.append(line)

            # PASS 2: Filter, dedup, validate, renumber
            kept_lines = []
            next_num = 1
            _seen_staging_keys: set[str] = set()
            for line in stripped_lines:
                m = re.match(r"^(\d+)\.\s+(.+)$", line)
                if m:
                    body = m.group(2)
                    if " — " not in body and not _is_park_line(body):
                        logger.info("Staging line dropped (no address, not a park)")
                        continue
                    if " — " in body:
                        _loc_part = body.split(" — ")[0].strip()
                        if not re.match(r"^\d", _loc_part) and not _is_park_line(body):
                            logger.info("Staging line dropped (name-only, no street number)")
                            continue
                    # DESIGN DECISION: dedup the RENDERED list, not just the candidate
                    # list. _rank_dedupe_cap_staging already drops same-address
                    # entries, but it keys off the CANDIDATE list — so it is a no-op
                    # in exactly the case that needs it. With a remote anchor the POI
                    # lookup returns ZERO candidates, Gemini writes the whole list
                    # from training data, and it emits TWO entries at the SAME
                    # invented address (1000 Ashby Rd / 1470 Main St / 1450 G St
                    # across runs of the 2026-07-31 Humboldt form). Nothing downstream
                    # could catch it: there were no candidates to dedup against, and
                    # the two lines are individually well-formed.
                    #
                    # Measured, not anecdotal: the cached corpus is generated with
                    # staging_candidates=[] (run_corpus.py:121/151), i.e. it IS this
                    # condition — 24 of its 69 staging lists (34%) carry at least one
                    # same-address duplicate, up to three in one run.
                    #
                    # False positives are structurally prevented rather than tuned
                    # away: a candidate-backed list has already passed
                    # _rank_dedupe_cap_staging, which collapses same-name and
                    # same-house+street candidates before Gemini ever sees them.
                    #
                    # Accept-first, the SAME arbitration the address dedup uses — the
                    # list arrives ranked and no new tie-break is invented here.
                    #
                    # An officer/dispatcher entry is never DROPPED: it is the one line
                    # a human actually chose, and losing it is far worse than leaving a
                    # visible duplicate. It still seeds the key set, so a fabricated
                    # copy of it below is dropped.
                    _dup_key = _staging_line_dedup_key(body)
                    _is_override_line = (
                        _OFFICER_OVERRIDE_LABEL in body
                        or _DISPATCHER_OVERRIDE_LABEL in body
                    )
                    if _dup_key and not _is_override_line and _dup_key in _seen_staging_keys:
                        logger.info("Staging line dropped (duplicate address)")
                        continue
                    if _dup_key:
                        _seen_staging_keys.add(_dup_key)
                    if next_num > 7:
                        logger.info("Staging line dropped (cap at 7 total)")
                        continue
                    kept_lines.append(f"{next_num}. {body}")
                    next_num += 1
                else:
                    kept_lines.append(line)

            # PASS 3: Officer staging injection (issue #244).
            # If the officer wrote a Staging Area for Resources and it doesn't appear
            # in the list, append it last. Gemini can suppress it when officer staging
            # matches the LKP/Residence address (our "do not include LKP/Residence"
            # prompt rule is applied by Gemini too broadly). Server-side injection is
            # the reliable fix — always include the officer's location so the dispatcher
            # can make the call, even if it's the same as LKP.
            # `[^\S\n]*`, NEVER `\s*`. `\s` matches a NEWLINE, so with re.MULTILINE
            # (and no DOTALL) a BLANK staging line lets the gap swallow the line
            # break and `(.+)$` capture the NEXT template line instead. The next
            # line here is always `CalTopo Map ID:`, so the wrong value is
            # deterministic and looks like a real parse rather than an overrun.
            # Live 2026-08-09 on personal-dev: the officer entry rendered as
            # "CalTopo Map ID: — Officer-designated staging location", was geocoded
            # against Nominatim AND Google, and Google answered 146 mi away. Three
            # guards caught the consequences; none could catch the cause, because
            # _BLANK_PAT below correctly reports the captured string is non-blank —
            # it genuinely is.
            _sar_m = re.search(r"^Staging Area for Resources:[^\S\n]*(.+)$", summary, re.MULTILINE)
            _officer_raw = (_sar_m.group(1).strip() if _sar_m else "")
            _BLANK_PAT = re.compile(
                r"^[\s\[\]]*(?:not\s+recorded|unknown|n/?a|tbd|none)?[\s\[\]]*$", re.I
            )
            if _officer_raw and not _BLANK_PAT.match(_officer_raw):
                _has_officer = any(
                    "Officer-designated staging" in l
                    for l in kept_lines
                    if re.match(r"^\d+\.", l)
                )
                if not _has_officer:
                    # Trim trailing blank lines so the injection doesn't create a gap
                    while kept_lines and not kept_lines[-1].strip():
                        kept_lines.pop()
                    _n = sum(1 for l in kept_lines if re.match(r"^\d+\.", l))
                    kept_lines.append(
                        f"{_n + 1}. {_officer_raw} — Officer-designated staging location "
                        f"(not among top recommendations — dispatcher discretion)."
                    )
                    logger.info("Officer staging injected server-side")
                else:
                    # Gemini rendered the officer's staging itself — and it may have
                    # REWRITTEN it to a street address. Live 2026-08-09: the officer
                    # wrote "Great Mall - Substation - Eastside Parking Garage,
                    # Milpitas" and the entry rendered as "572 Great Mall Drive,
                    # Milpitas — Great Mall". On a site over a mile across, the
                    # parking garage IS the useful half, and it survived only in an
                    # Event Log note the dispatcher may never scroll to.
                    #
                    # The officer's words are APPENDED, never substituted: the
                    # geocodable address has to stay in front because
                    # index.html's `^1\.\s+([^\n—]+)` builds the responders' Apple
                    # and Google maps queries from the text BEFORE the first
                    # em-dash (Locked Decision: "Staging line text IS the
                    # responders' maps-link query"). Everything after that dash
                    # reaches humans and never reaches navigation, which is
                    # exactly where a location the officer described but did not
                    # address belongs.
                    #
                    # Fires on the SAME condition as the "was matched to" Event Log
                    # note below, via the shared _staging_first_word_key — telling
                    # the dispatcher the text was rewritten while leaving the
                    # responder-facing line with no trace of the original would be
                    # half a disclosure.
                    for _i, _l in enumerate(kept_lines):
                        if not re.match(r"^\d+\.", _l):
                            continue
                        if "Officer-designated staging" not in _l:
                            continue
                        _rendered_loc = re.sub(
                            r"^\d+\.\s*", "", _l.split(" — ")[0]
                        ).strip()
                        if _staging_first_word_key(_officer_raw) != _staging_first_word_key(
                            _rendered_loc
                        ):
                            kept_lines[_i] = (
                                f'{_l.rstrip()} (as written: "{_officer_raw[:100]}")'
                            )
                            logger.info("Officer staging as-written text preserved")
                        break

            # Event Log note when officer's location is not the #1 recommendation.
            # Fires whether the officer entry was already in the list (ranked lower by Gemini)
            # or was just injected by PASS 3.  Gives the dispatcher situational awareness
            # without changing any prioritization — they make the final call.
            _first_numbered = next(
                (l for l in kept_lines if re.match(r"^\d+\.", l)), ""
            )
            if "Officer-designated staging" not in _first_numbered:
                _first_m = re.match(r"^\d+\.\s*(.+?)\s*—", _first_numbered)
                _first_rec = _first_m.group(1).strip() if _first_m else "the top recommendation"
                event_log_additions.append(
                    f"Note: Officer staging ({_officer_raw[:100]}) not #1 — top: {_first_rec}"
                )

            summary = before + "\n".join(kept_lines) + after
    except Exception as exc:
        logger.debug("staging section post-processing skipped: %s", type(exc).__name__)

    # Strip Gemini-generated internal implementation notes from the staging section.
    # These are inaccurate or irrelevant to dispatchers — server-side filters already ran;
    # operational decisions (staging count, geographic diversity) are not dispatcher concerns.
    # Strip unconditionally.
    #
    # DO NOT strip school/church time-of-day notes — those are operationally useful:
    # dispatcher needs to know schools are available after 3:30pm, churches after 12pm.
    # Those notes contain "schools excluded" / "churches excluded", not "locations excluded"
    # or "diversity rule", and are injected server-side below.
    summary = re.sub(
        # "Note: N location(s) excluded..." — generic count note (Build 21)
        r"^Note:[ \t]+\d+[ \t]+location(?:s)?[ \t]+excluded[^\n]*\n?",
        "",
        summary,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    summary = re.sub(
        # Any Note line mentioning "diversity rule" — Gemini uses varying wording:
        # "Note: N locations on STREET excluded due to geographic diversity rule."
        # "Note: N fast food/grocery excluded from STREET due to geographic diversity rule."
        r"^Note:[ \t]+[^\n]+diversity[ \t]+rule[^\n]*\n?",
        "",
        summary,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    # Server-side school/church time-of-day exclusion note injection.
    # DESIGN DECISION (PR #163): Gemini receives school and place_of_worship candidates
    # in its candidate list and correctly excludes them per the time-of-day rules in the
    # prompt, but non-deterministically omits the exclusion note at the bottom.
    # Fix: inject the note server-side using the known staging_candidates list and current
    # Pacific time — fully deterministic, same rules as the prompt:
    #   - Schools: excluded 7am–3:30pm weekdays
    #   - Churches: excluded Sunday 8am–12pm
    # The note is appended just before the \n---\n separator that precedes LPB Range Rings.
    # We also strip any Gemini-generated school/church notes first (in case Gemini does
    # produce one on some runs) to avoid duplicates.
    try:
        summary = re.sub(
            r"^Note:[ \t]+\d+[ \t]+school[^\n]*\n?",
            "", summary, flags=re.MULTILINE | re.IGNORECASE,
        )
        summary = re.sub(
            r"^Note:[ \t]+\d+[ \t]+church[^\n]*\n?",
            "", summary, flags=re.MULTILINE | re.IGNORECASE,
        )
        _now_pt = datetime.datetime.now(_PT)
        _wd = _now_pt.weekday()          # 0=Mon … 6=Sun
        _tm = _now_pt.hour * 60 + _now_pt.minute
        # DESIGN DECISION (do not revert without team discussion):
        # Use _overpass_school_count / _overpass_church_count — counts from the FULL deduped
        # Overpass list before the 12-element cap — instead of re-counting from staging_candidates.
        # In dense urban areas (e.g. Cupertino) 12+ tier-1 candidates fill the cap entirely,
        # leaving _school_count = 0 if we counted from the capped list.  The uncapped counts
        # are returned by _query_overpass_staging() alongside the capped list.
        _school_count = _overpass_school_count
        _church_count = _overpass_church_count
        _excl_notes: list[str] = []
        # School hours: 7:00am–3:30pm weekdays
        _in_school_window = _wd < 5 and 7 * 60 <= _tm <= 15 * 60 + 30
        # Church hours: Sunday 8am–12pm
        _in_church_window = _wd == 6 and 8 * 60 <= _tm <= 12 * 60

        # Per Bill 2026-05-20 live-test: when Overpass is unavailable we cannot
        # enumerate specific schools/churches, but the time-policy still applies
        # to whatever Gemini fell back to. The dispatcher needs to know the
        # policy is in force (and that the filter could not be verified
        # server-side). Two-branch logic per time window:
        #   - count > 0 (Overpass succeeded with hits): count-aware note
        #   - Overpass failed in time window: advisory note (policy + caveat)
        # When Overpass succeeded with 0 hits, no note (nothing to exclude).
        if _school_count > 0 and _in_school_window:
            _s = "s" if _school_count > 1 else ""
            _excl_notes.append(
                f"Note: {_school_count} school/college{_s} excluded — daytime weekday (available after 3:30pm)."
            )
        elif not _overpass_ok and _in_school_window:
            _excl_notes.append(
                "Note: Daytime weekday — schools/colleges excluded by policy (available after 3:30pm). "
                "Nearby location service unavailable, so this filter could not be applied server-side — "
                "review the list carefully."
            )

        if _church_count > 0 and _in_church_window:
            _ch = "es" if _church_count > 1 else ""
            _excl_notes.append(
                f"Note: {_church_count} church{_ch} excluded — Sunday morning service hours (available after ~12pm)."
            )
        elif not _overpass_ok and _in_church_window:
            _excl_notes.append(
                "Note: Sunday morning service hours — churches excluded by policy (available after ~12pm). "
                "Nearby location service unavailable, so this filter could not be applied server-side — "
                "review the list carefully."
            )
        if _excl_notes:
            # Insert after the last staging entry, before the --- separator for LPB rings.
            # DESIGN DECISION (PR #163 fix): use re.search with \n+ between --- and header
            # because the STAGING_KOESTER_PROMPT has a blank line between "---" and the
            # "LPB Range Ring Analysis" header.  str.find() with the exact string
            # "\n---\nLPB Range Ring Analysis:" always returns -1 because the actual text
            # is "\n---\n\nLPB Range Ring Analysis (Robert Koester — ...):" (blank line +
            # full parenthetical).  Using \n+ handles any number of blank lines, and
            # matching just "LPB Range Ring Analysis" (no colon, no parenthetical) covers
            # all Gemini formatting variants.
            _sep_m = re.search(r"\n---\n+LPB Range Ring Analysis", summary)
            if _sep_m:
                _sep_pos = _sep_m.start()
                summary = (
                    summary[:_sep_pos].rstrip("\n")
                    + "\n" + "\n".join(_excl_notes)
                    + summary[_sep_pos:]
                )
    except Exception as exc:
        logger.debug("school/church exclusion note injection skipped: %s", type(exc).__name__)

    # Normalize LPB Questionnaire section: replace plain hyphens used as field separators
    # with em-dashes in detail sub-fields. Gemini occasionally uses " - " instead of " — ".
    # Phase 1.5p format: "Q2 - No - Has phone — number: [X]"
    # The primary Q# - ANSWER - QUESTION separators use plain hyphens intentionally.
    # Only detail fields (e.g. "— number:", "— date:") use em-dashes.
    # This normalization catches cases where Gemini writes the detail as " - " instead of " — ".
    # Scoped to the LPB Questionnaire section only to avoid touching Event Log entries.
    try:
        q_start = summary.find("\nLPB Questionnaire:\n")
        q_end   = summary.find("\n---\n", q_start + 1) if q_start != -1 else -1
        if q_start != -1 and q_end != -1:
            q_section = summary[q_start:q_end]
            # After the answer (Yes/No/NOT ANSWERED...) and before a detail keyword,
            # replace " - " with " — " only when it precedes a known detail field name.
            # Pattern: "- [answer] - [question] - [detail]:" → normalize last " - " before detail keyword
            q_section = re.sub(
                r"( (?:Yes|No|Unknown|NOT ANSWERED[^)]*\))) - (number|date|reason|detail):",
                r"\1 — \2:",
                q_section,
            )
            summary = summary[:q_start] + q_section + summary[q_end:]
    except Exception as exc:
        logger.debug("LPB em-dash normalization skipped: %s", type(exc).__name__)

    # Revert Q4 MUPS date if Gemini over-applied ISO format (the ISO reformat rule is for Event Log
    # timestamps only, not for inline MUPS dates). iPad was outputting "2026-01-08" instead of "1/8/26".
    # Pattern: "date: YYYY-MM-DD" within Q4 line → convert back to short M/D/YY form.
    try:
        def _iso_to_short(m: re.Match) -> str:
            yyyy, mm, dd = m.group(1), m.group(2), m.group(3)
            yy = yyyy[2:]  # "2026" → "26"
            return f"date: {int(mm)}/{int(dd)}/{yy}"
        summary = re.sub(
            r"(Q4[^\n]*date: )(\d{4})-(\d{2})-(\d{2})",
            lambda m: m.group(1) + f"{int(m.group(3))}/{int(m.group(4))}/{m.group(2)[2:]}",
            summary,
        )
    except Exception as exc:
        logger.debug("Q4 date revert skipped: %s", type(exc).__name__)

    # Inject authenticated dispatcher's last name into the Dispatcher: field (Build 20).
    # The Google ID token "name" claim contains the full display name (e.g. "Bill Burns").
    # We extract the last word as the last name — covers "First Last" and "First M. Last".
    # Format matches SOP dispatcher identifier style: "Burns" (dispatcher adds badge # manually).
    try:
        full_name = dispatcher.get("name", "").strip()
        if full_name:
            last_name = full_name.split()[-1]  # Last word of Google display name
            # Lambda bypasses re.sub backref interpretation in the replacement
            # string — guards against `\1` / `\g<x>` sequences in dynamic
            # content (PR-B, Melanie's main.py C-2). Same pattern below.
            summary = re.sub(
                r"^(Dispatcher:).*$",
                lambda m: f"{m.group(1)} {last_name}",
                summary,
                flags=re.MULTILINE,
            )
            logger.info("Dispatcher name injected | sub=%s", user_sub)
    except Exception as exc:
        logger.debug("dispatcher name injection skipped: %s", type(exc).__name__)

    # Reconstruct Event Name from authoritative parsed fields to prevent OCR truncation.
    #
    # DESIGN DECISIONS (do not re-litigate without team discussion):
    #   - Format is: "YYYY-MM-DD AGENCY STREETNAME" — street name ONLY.
    #   - No street type suffix (no Blvd, Dr, Ave, Rd, etc.) — keeps it concise for SOP/radio.
    #   - No cardinal direction prefix (no North, South, East, West) — same reason.
    #   - Example correct output: "2026-02-20 MILPITAS Calaveras" (not "East Calaveras Blvd")
    #   - Example correct output: "2026-01-07 SJPD Tradan" (not "Tradan Dr.")
    #   - Agency uses the full Agency: field value (e.g. "MILPITAS", "SJPD")
    #   - Date comes from the first ISO date in the Event Log section (authoritative).
    #   - Street comes from Last Known Position (verified/corrected address, not raw form).
    #   - This field is server-reconstructed to prevent Gemini OCR truncation of long street names.
    #
    # LKP address format from Gemini: "[number] [direction?] [name] [type], [city], [state]"
    # e.g. "1200 East Calaveras Blvd, Milpitas, CA"
    # Steps:
    #   1. Non-greedy/non-comma capture between house number and FIRST comma → "East Calaveras Blvd"
    #      Using [^,]+ (no-comma character class) ensures we stop at the first comma, not the last.
    #      Previous bug: (.+), was greedy and captured "East Calaveras Blvd, Milpitas" (stopped at last comma).
    #   2. Strip leading cardinal direction word (North/South/East/West/N/S/E/W) → "Calaveras Blvd"
    #   3. Strip trailing street type suffix (Blvd/Dr/Ave/etc.) → "Calaveras"
    # DESIGN DECISION: Use [^,]+ (not .+) so city name after the first comma is never captured.
    # _STREET_CARDINALS and _STREET_TYPES are defined at module level (PR-D-1) so the
    # dispatcher staging override endpoint can reuse them for streetname extraction.
    try:
        # PR-D-2.5 (2026-05-10): canonicalize the `Agency:` line in the summary
        # BEFORE event-name reconstruction reads it. Same `_normalize_event_name_agency`
        # lookup the event name uses, so the displayed Agency matches the
        # canonical agency in the Event Name. Without this, the textarea showed
        # `Agency: SCC SLO` while the Event Name read `SCCSO Verde Vista` — the
        # raw OCR misread leaked through to both summary surfaces in spite of
        # the event-name path canonicalizing correctly. Live confirmed 2026-05-10
        # Verde Vista re-test.
        _agency_pre = re.search(r"^Agency:\s*(.+?)$", summary, re.MULTILINE)
        if _agency_pre:
            _raw_agency = _agency_pre.group(1).strip()
            _canonical_agency = _normalize_event_name_agency(_raw_agency)
            if _canonical_agency and _canonical_agency != _raw_agency:
                summary = re.sub(
                    r"^Agency:\s*.+$",
                    lambda m: f"Agency: {_canonical_agency}",
                    summary,
                    count=1,
                    flags=re.MULTILINE,
                )
                event_log_additions.append(
                    f"Agency normalized: {_raw_agency} → {_canonical_agency}"
                )

        # Strict first-line match: the ISO date MUST be on the first content line of the
        # Event Log section (immediately after the "Event Log:\n" header).
        #
        # JPEG path: Gemini reformats all Event Log dates to ISO — first line starts "YYYY-MM-DD …"
        # → matched immediately.
        #
        # PDF path: pdf_extract._to_iso_date() converts the officer-entered form date (e.g.
        # "2/20/26") to ISO format ("2026-02-20") before writing the Event Log, so line 1
        # also starts with an ISO date. If conversion fails (date missing or unrecognised),
        # event_name reconstruction gracefully falls through — leave the placeholder literal.
        #
        # Previous DOTALL attempt (PR #138 — reverted): DOTALL allowed .*? to scan across
        # the non-ISO form date on line 1 and capture the intake_timestamp from line 2,
        # which is always today's date — producing a wrong Event Name date.
        date_match    = re.search(r"^Event Log:\s*\n(\d{4}-\d{2}-\d{2})", summary, re.MULTILINE)
        agency_match  = re.search(r"^Agency:\s*(.+?)$", summary, re.MULTILINE)
        # [^,\n]+ stops at FIRST comma OR end-of-line (handles both formats):
        #   JPEG path: "1200 East Calaveras Blvd, Milpitas, CA" → stops at first comma → "East Calaveras Blvd"
        #   PDF path:  "1200 EAST CALVERAS BLVD" (no city embedded) → stops at end-of-line → "EAST CALVERAS BLVD"
        # The trailing (?:,|$) makes the comma optional so PDF-path bare LKPs still match.
        # DESIGN DECISION: [^,\n] (not .+) so city name and line content after first comma are never captured.
        # DESIGN DECISION: [ \t]+ (not \s+) after the house number — \s matches \n, so a bare
        # house number at end of line (e.g. "Last Known Position: 10410\n") would cause \s+ to
        # cross the newline and grab the NEXT line ("Residence Address:...") into group(1).
        # Using [ \t]+ (horizontal whitespace only) prevents the regex from crossing lines.
        lkp_match     = re.search(r"^Last Known Position:\s*\d+[ \t]+([^,\n]+?)(?:,|$)", summary, re.MULTILINE)
        if not lkp_match:
            # Fallback for "LOCATION_NAME NNNN STREET, ..." — location name precedes the
            # house number.  group(1) = street-with-type, same as the primary regex, so
            # the downstream _STREET_TYPES / _STREET_CARDINALS stripping is unchanged.
            # Example: "GOOD SAM HOSPITAL 2000 SAMARITAN DR. SAN JOSE, CA 95124"
            #   \S[^,\n]*?[ \t]  matches "GOOD SAM HOSPITAL " (lazy, stops at house number)
            #   \d+[ \t]+        matches "2425 "
            #   ([^,\n]+?)       captures "SAMARITAN DR. SAN JOSE" (group 1, stops at comma)
            lkp_match = re.search(
                r"^Last Known Position:\s*\S[^,\n]*?[ \t]\d+[ \t]+([^,\n]+?)(?:,|$)",
                summary,
                re.MULTILINE,
            )
        if date_match and agency_match and lkp_match:
            evt_date          = date_match.group(1)                   # e.g. "2026-01-07"
            evt_agency        = _normalize_event_name_agency(agency_match.group(1).strip())
            street_with_type  = lkp_match.group(1).strip()           # e.g. "East Calaveras Blvd Apt #2"
            street_with_type  = _APT_STRIP_RE.sub("", street_with_type).strip(" ,")  # → "East Calaveras Blvd"
            street_no_type    = _STREET_TYPES.sub("", street_with_type).strip()   # → "East Calaveras"
            evt_street        = _STREET_CARDINALS.sub("", street_no_type).strip() # → "Calaveras"
            canonical_name    = _cap_event_name(evt_date, evt_agency, evt_street)
            summary = re.sub(r"^(Event Name:).*$", lambda m: f"{m.group(1)} {canonical_name}", summary, flags=re.MULTILINE)
            logger.info("Event Name reconstructed")
        elif date_match and agency_match:
            # City-level fallback: LKP has no house number (e.g. "SAN JOSE,CA" from a
            # city-only officer entry, or an intersection/landmark). Use the city name so
            # the dispatcher gets something meaningful to correct rather than a placeholder.
            # Format: "YYYY-MM-DD AGENCY City" or "YYYY-MM-DD AGENCY UNKNOWN" for
            # intersections/landmarks/bare-numbers that would produce an unusable string.
            lkp_city_match = re.search(r"^Last Known Position:\s*([^,\n]+)", summary, re.MULTILINE)
            if lkp_city_match:
                evt_date   = date_match.group(1)
                # PR-fix-4 (2026-05-08): apply the same agency canonicalization
                # the primary path uses, so e.g. "SJSU PD" + intersection LKP
                # produces "2026-05-07 SJSU <street>" not "SJSU PD <street>".
                evt_agency = _normalize_event_name_agency(agency_match.group(1).strip())
                raw_lkp    = lkp_city_match.group(1).strip()
                # Placeholder fallback: LKP was not recorded (PDF path "[not recorded]").
                # Rather than producing "2026-03-07 SCCSO [Not Recorded]", fall back to the
                # Residence Address street name — identical stripping as the primary LKP path.
                _LKP_PH_RE = re.compile(
                    r"^\[?(?:not\s+recorded|not\s+provided|unknown|n/?a)\]?$",
                    re.IGNORECASE,
                )
                if _LKP_PH_RE.match(raw_lkp.strip()):
                    _res_evt_m = re.search(
                        r"^Residence Address:\s*(?:\S[^,\n]*?[ \t])?\d+[ \t]+([^,\n]+?)(?:,|$)",
                        summary, re.MULTILINE,
                    )
                    if _res_evt_m:
                        _sw = _APT_STRIP_RE.sub("", _res_evt_m.group(1).strip()).strip(" ,")
                        _sn = _STREET_TYPES.sub("", _sw).strip()
                        _st = _STREET_CARDINALS.sub("", _sn).strip()
                        canonical_name = _cap_event_name(evt_date, evt_agency, _st)
                        logger.info("Event Name reconstructed (residence fallback)")
                    else:
                        canonical_name = _cap_event_name(evt_date, evt_agency, "UNKNOWN")
                        logger.info("Event Name reconstructed (residence fallback, no address)")
                    summary = re.sub(r"^(Event Name:).*$", lambda m: f"{m.group(1)} {canonical_name}", summary, flags=re.MULTILINE)
                else:
                    # Intersection detection — PR-fix-4 (2026-05-08) broadened from
                    # `&|INTERSECTION` to also catch the common `<St> at <St>` and
                    # `<St> and <St>` patterns that landed in 2026-05-07 SJSU as
                    # "5th Street at St. John Street" → "5Th Street At St. John Street"
                    # (full title-cased garbage). Now extracts the FIRST street name
                    # using the same _STREET_TYPES + _STREET_CARDINALS strippers as
                    # the primary path, producing "5th" instead of UNKNOWN.
                    #
                    # Issue #400 (2026-05-09): the regex matches BOTH between-street
                    # separators (`& | @ | at | and`) AND the prefix variant
                    # (`INTERSECTION OF`). When Pass 1 produces a verbose form like
                    # "Intersection of N 5th Street and E St John Street, San Jose, CA",
                    # the prefix matched at position 0 and `raw_lkp[:0] = ""` fell
                    # through to UNKNOWN. Fix: when the matched separator IS the
                    # prefix, skip past it and re-search for the between-streets
                    # separator in the remainder. Live regression: 2026-05-09
                    # SCCSSAR-dev SJSU run produced "2026-05-07 SJSU UNKNOWN".
                    intersection_match = _EVENT_NAME_INTERSECTION_RE.search(raw_lkp)
                    if intersection_match:
                        if intersection_match.group(0).strip().upper().startswith("INTERSECTION"):
                            # Prefix variant — skip past it and find the between-
                            # streets separator on the remainder. Fall back to
                            # everything-before-comma if no separator found (e.g.
                            # "Intersection of Foo Park" — single-name case).
                            after_prefix = raw_lkp[intersection_match.end():].strip()
                            between_match = re.search(
                                r"\s+(?:&|@|at|and)\s+", after_prefix, re.IGNORECASE
                            )
                            if between_match:
                                first_street = after_prefix[:between_match.start()].strip()
                            else:
                                first_street = after_prefix.split(",")[0].strip()
                        else:
                            # Between-streets separator — take everything before it.
                            first_street = raw_lkp[:intersection_match.start()].strip()
                        # Strip type ("Street", "Blvd", ...) and cardinal direction
                        # ("East", "N", ...) — matches the primary path exactly.
                        first_street = _APT_STRIP_RE.sub("", first_street).strip(" ,")
                        first_street = _STREET_TYPES.sub("", first_street).strip()
                        first_street = _STREET_CARDINALS.sub("", first_street).strip()
                        evt_city = first_street if first_street else "UNKNOWN"
                    elif re.match(r"^\d+$", raw_lkp):
                        # Bare house number only (e.g. "10410") — officer forgot the street name.
                        # Title-casing a lone number is unhelpful; use UNKNOWN so the dispatcher
                        # knows to correct it rather than seeing "2026-01-25 SCCSO 10410".
                        evt_city = "UNKNOWN"
                    else:
                        # Title-case the city (PDF path stores it all-caps: "SAN JOSE" → "San Jose")
                        evt_city = raw_lkp.title()
                    canonical_name = _cap_event_name(evt_date, evt_agency, evt_city)
                    summary = re.sub(r"^(Event Name:).*$", lambda m: f"{m.group(1)} {canonical_name}", summary, flags=re.MULTILINE)
                    logger.info("Event Name reconstructed (city fallback)")
    except Exception as exc:
        logger.debug("event name reconstruction skipped: %s", type(exc).__name__)

    # Step 16: Build map_data for CalTopo incident map, then return JSON to browser.
    # map_data is never stored server-side — lives only in the browser session.
    #
    # Structured data extracted here:
    #   lkp        — geocoded Last Known Position (lat/lng from Nominatim)
    #   residence  — geocoded Residence Address (lat/lng from Nominatim, parallel geocode)
    #   staging    — lat/lng for each OSM staging candidate that survived the server filter
    #   event_name — canonical event name (used as CalTopo map title)
    map_data: dict = {}
    try:
        # --- LKP ---
        lkp_label_match = re.search(r"^Last Known Position:\s*(.+)", summary, re.MULTILINE)
        lkp_label = lkp_label_match.group(1).strip() if lkp_label_match else "LKP"
        if geo:
            lat, lng, _, _ = geo
            map_data["lkp"] = {"lat": lat, "lng": lng, "label": f"LKP — {lkp_label}"}

        # --- Residence ---
        # DESIGN DECISION (do not revert without team discussion):
        #   Residence marker is ALWAYS plotted on the CalTopo map, even when:
        #   (a) Nominatim geocoding fails for the residence address (e.g. apt numbers trip it up)
        #   (b) Residence == LKP (same physical location)
        #   Fallback: if residence geocoding failed, use LKP coords so the marker is visible.
        #   Two markers at the same spot is acceptable; a missing marker is not.
        #   Title is always clean ("Residence — <address>"); geocode-failed note goes in description.
        res_label_match = re.search(r"^Residence Address:\s*(.+)", summary, re.MULTILINE)
        res_label = res_label_match.group(1).strip() if res_label_match else "Residence"

        # Extract MP name (everything before the "; at-risk:" qualifier) and DOB for marker description
        mp_name_match = re.search(r"^Missing Person:\s*([^;]+)", summary, re.MULTILINE)
        mp_name = mp_name_match.group(1).strip() if mp_name_match else None
        dob_match = re.search(r"^DOB:\s*(.+)", summary, re.MULTILINE)
        dob_str = dob_match.group(1).strip() if dob_match else None

        # Build residence marker description: MP name + DOB at top, geocode note at bottom
        res_desc_lines = []
        if mp_name:
            res_desc_lines.append(f"MP: {mp_name}")
        if dob_str:
            res_desc_lines.append(f"DOB: {dob_str}")
        res_desc_lines.append("Subject's residence")

        if res_lat is not None and res_lng is not None:
            map_data["residence"] = {
                "lat": res_lat, "lng": res_lng,
                "label": f"Residence — {res_label}",
                "description": "\n".join(res_desc_lines),
            }
        elif map_data.get("lkp"):
            # Geocoding failed for residence — fall back to LKP coords; note in description
            lkp_coords = map_data["lkp"]
            res_desc_lines.append("(Note: geocoding failed for this address — plotted at LKP)")
            map_data["residence"] = {
                "lat": lkp_coords["lat"], "lng": lkp_coords["lng"],
                "label": f"Residence — {res_label}",
                "description": "\n".join(res_desc_lines),
            }

        # --- Staging locations: match surviving numbered lines to OSM candidate lat/lngs ---
        # Strategy: the surviving staging lines (post-filter, post-renumber) are matched back
        # to the OSM candidates by name. We build a name→{lat,lng} lookup from candidates.
        # Officer-designated staging (entry 1) is NOT in the OSM candidates list — it comes
        # from the form. Its lat/lng falls back to LKP coords (close enough for map placement).
        #
        # DESIGN DECISION (do not revert without team discussion):
        # The staging line loop MUST run even when staging_candidates is empty (e.g. Overpass
        # returned no results but Gemini still produced staging text). Without this, the
        # officer/LKP marker (#1) was silently dropped from the CalTopo map because its
        # LKP-coords fallback was unreachable inside the old `if staging_candidates:` gate.
        # Fix: build name_to_coords only when candidates exist, but always parse and emit
        # the staging lines — officer entry always gets LKP coords; alternates are skipped
        # if no candidate coords can be matched (correct behavior when Overpass returned nothing).
        staging_entries = []
        name_to_coords = {
            c["name"].lower(): (c["lat"], c["lng"])
            for c in staging_candidates
            if "lat" in c and "lng" in c
        } if staging_candidates else {}

        # Find all numbered staging lines in the processed summary
        staging_section_match = re.search(
            r"\nStaging Area Recommendations:\n(.*?)(?:\n---\n|$)",
            summary,
            re.DOTALL,
        )
        # DESIGN DECISION (do not revert without team discussion):
        # Two-pass approach for alternate staging marker coords:
        #
        # Pass A — OSM name-match (fast, no network): for each staging line, check if any
        # OSM candidate name appears as a substring of the line body. This works when Overpass
        # returned the same locations Gemini chose. Coords come directly from OSM.
        #
        # Pass B — Nominatim address geocode (fallback): when name-match fails for an alternate
        # entry, parse the street address from the line (text before " — ") and geocode it.
        # This handles the case where Gemini selected from training-data knowledge rather than
        # strictly from the Overpass candidate list, or where the Overpass radius/mirror shift
        # between runs returned different candidates. All fallback geocodes run in parallel.
        #
        # Officer entry always uses LKP coords (no OSM or geocode lookup needed).
        # Entries with no coords after both passes are silently dropped (no marker).
        pending_geocode = []        # list of (index_into_staging_entries, addr_str, is_officer)
        # event_log_additions was initialised before geocoding (above) — do NOT re-initialise
        # here or geocoding-stage corrections (LKP/Residence spelling fixes) will be discarded.

        if staging_section_match:
            staging_text = staging_section_match.group(1)
            for line in staging_text.splitlines():
                m = re.match(r"^(\d+)\.\s+(.+)$", line)
                if not m:
                    continue
                n    = int(m.group(1))
                body = m.group(2)
                # Determine marker type
                is_officer = "Officer-designated staging" in body
                s_type = "officer" if is_officer else "alternate"

                # Skip the structural LKP/Residence entry — it already has dedicated markers
                # (the LKP placemark2 and Residence hut icons). Adding a third "alternate"
                # marker at the same address creates a confusing overlap and a false-positive
                # from Pass A name-matching (address words like "Calaveras" can match nearby
                # OSM candidates on the same street, landing the marker at the wrong coords).
                #
                # DESIGN DECISION (do not revert — regression from Phase 1.5u):
                # The skip condition must be scoped to the structural LKP/Residence entry label
                # ONLY, and must NOT fire on officer entries or on alternate entries whose
                # description text happens to contain "LKP" (e.g. "nearest pharmacy to LKP").
                #
                # Structural entry label patterns (Gemini output):
                #   "... — Last Known Position / Residence"  (officer ≠ LKP case)
                #   "... — LKP / Residence"                  (abbreviated form)
                # Officer entry label always contains "Officer-designated staging" → is_officer=True.
                # Any entry where is_officer=True is kept; only non-officer entries with the
                # structural LKP/Residence label pattern are skipped.
                #
                # BUG that was fixed: the previous check used
                #   `"LKP" in body.split(" — ")[-1]`
                # which fired on:
                #   (a) "... — Officer-designated staging location and LKP"  (officer==LKP merge)
                #       → was_officer=True so is_officer check already handles this, BUT the
                #         combined label meant the skip fired on the merged officer entry too
                #   (b) alternate entries where Gemini puts "LKP" in the description text
                # Fix: check is_officer first (always keep officer entries), then check for the
                # specific structural label phrases "Last Known Position" or "/ Residence".
                if not is_officer and (
                    "Last Known Position" in body
                    or "/ Residence" in body
                ):
                    logger.debug("Staging marker loop: skipping structural LKP/Residence entry | n=%d", n)
                    continue

                # Pass A: OSM name-match — compare against the location portion only
                # (text before " — ") using full-name substring match only.
                #
                # DESIGN DECISION (do not revert without team discussion — issue #173):
                # The original implementation matched against the full body and used a
                # single-word fallback (any word >4 chars from cname in body).  This
                # caused city-name false-positives: when cname = "campbell community
                # center", the word "campbell" (8 chars) matched against ANY staging
                # entry that had ", Campbell" as a city suffix — officer entries, park
                # entries, restaurant entries — all received the Community Center's
                # coords.  Two confirmed failure modes from 2026-03-07 testing:
                #   • "300 Darryl Drive, Campbell — Officer-designated..." → CP marker
                #     placed at Community Center instead of 300 Darryl Dr
                #   • "1000 S Winchester Blvd, Campbell — KFC..." → KFC marker placed
                #     at Community Center
                # Fix: extract loc_part (text before " — "), then check only whether
                # the full candidate name is a substring of loc_part.  Name-abbreviation
                # misses (e.g. Gemini writes "Morgan Park" vs OSM "John D Morgan Park")
                # fall through to Pass B (Nominatim), which handles them correctly.
                #
                # Issue #722 narrowed that substring test to a LEADING match via
                # _staging_candidate_name_leads: closing the city-suffix door in
                # #173 left the street-name door open, and a candidate whose name
                # is also the street name matched every address on that street.
                # See the helper's docstring for the 2026-08-08 failure.
                found_lat, found_lng = None, None
                loc_part = body.split(" — ")[0].strip().lower()
                for cname, (clat, clng) in name_to_coords.items():
                    if _staging_candidate_name_leads(cname, loc_part):
                        found_lat, found_lng = clat, clng
                        break

                # Pass B setup: queue Nominatim geocode for ALL unmatched entries, including
                # officer entries. Officers frequently specify parks or locations that are not
                # in the Overpass candidates list (e.g. because the park was spelled differently
                # or is outside the Overpass radius). Trying Nominatim first gives us the actual
                # park coordinates; LKP fallback is reserved for when Nominatim also fails.
                #
                # DESIGN DECISION (do not revert without team discussion):
                # Previously, officer entries used an immediate LKP fallback (skipping Pass B).
                # This was changed because:
                #   1. Officers typically specify nearby parks — Nominatim can geocode them
                #      correctly when spelled right, placing the CP marker at the actual park.
                #   2. When misspelled, Nominatim fails gracefully → LKP fallback still fires
                #      (post-Pass-B, below) so the officer entry always appears on the map.
                #   3. Correct location > approximate location; LKP fallback is last resort.
                if found_lat is None:
                    addr_part = body.split(" — ")[0].strip()
                    # Issue #668: staging written out as a COORDINATE — decimal
                    # lat/lng or SAR-standard UTM. Wilderness and remote
                    # mutual-aid callouts routinely have no landmark, address,
                    # or cross street to give, so this is normal input rather
                    # than a dispatcher error. It must be recognised BEFORE the
                    # canonical-facility table and the address geocoders, both
                    # of which treat it as free text: on 2026-07-31 a correct
                    # in-county pair went out as `q=<lat>, <lng>, CA`, missed
                    # Nominatim, was answered by Google Maps, and was then
                    # thrown away by the distance guard.
                    _coord = _parse_coordinate_staging_text(addr_part) if addr_part else None
                    if _coord is not None:
                        _c_lat, _c_lng, _c_utm = _coord
                        found_lat, found_lng = _c_lat, _c_lng
                        # SECOND SURFACE — do not remove. Setting the marker
                        # coords fixes CalTopo and nothing else. The staging
                        # line TEXT is what index.html turns into
                        # maps.apple.com/?q= and google.com/maps/search/ for
                        # every responder in Slack (Locked Decision: "Staging
                        # line text IS the responders' maps-link query"), and no
                        # maps app text-searches a UTM string. Rewriting to the
                        # lat/lng-leading form is what makes the responder link
                        # resolve; a marker-only fix reproduces #646 — map
                        # correct, whole callout misrouted.
                        #
                        # Decimal input is already a valid maps query and is
                        # left exactly as the officer wrote it.
                        if _c_utm:
                            _c_display = _format_coordinate_staging_display(
                                _c_lat, _c_lng, _c_utm
                            )
                            body = body.replace(addr_part, _c_display)
                            # `body` is a fresh slice of the pre-loop staging
                            # snapshot each iteration, but `summary` accumulates.
                            # _c_display CONTAINS addr_part, so if a second entry
                            # carried the same UTM an unguarded replace would nest
                            # the rewrite into itself ("<lat,lng> — <lat,lng> —
                            # <utm>"). Idempotence is the guard.
                            if _c_display not in summary:
                                summary = summary.replace(addr_part, _c_display)
                            # #646 rule: a substitution in the officer's staging
                            # text is never silent — the dispatcher sees it in
                            # the Event Log. The logger line carries only the
                            # entry number; staging text is PII and stays out
                            # of Cloud Logging.
                            event_log_additions.append(
                                f"Staging UTM converted for mapping: {_c_utm} → {_c_display}"
                            )
                            logger.info("Staging Pass B UTM coordinate parsed | n=%d", n)
                        else:
                            logger.info("Staging Pass B lat/lng coordinate parsed | n=%d", n)
                        # The distance guard WARNS but never rejects here. Its
                        # reference point is the geocoded LKP, which is itself
                        # unvalidated — on the 2026-07-31 Humboldt callout the
                        # anchor was in Nova Scotia and this guard destroyed the
                        # one correct location on the form. An explicitly
                        # written coordinate outranks a geocoded guess, so it
                        # wins; a suspicious one still has to stay visible.
                        if _staging_geocode_implausible(geo, _c_lat, _c_lng):
                            event_log_additions.append(_staging_coordinate_distance_note(
                                addr_part, is_officer,
                                _haversine_m(geo[0], geo[1], _c_lat, _c_lng),
                            ))
                            logger.warning(
                                "Staging Pass B coordinate far from LKP — kept, anchor may be wrong"
                            )
                    elif addr_part:
                        # Apply LKP-style hygiene (city context + CA append). Officers
                        # frequently write staging text without city/state context;
                        # without anchoring, Nominatim resolves bare strings to
                        # wrong-country matches (e.g. "ALMA @ 10TH" → Vancouver, BC
                        # on the 2026-05-09 SJSU smoke test). Idempotent: already-
                        # canonical addresses pass through unchanged.
                        # Issue #646: known SAR facilities resolve from a table.
                        # Surface the substitution in the Event Log — a silent
                        # rewrite of the officer's staging text is exactly the
                        # complaint this issue was filed on. Facility KEY only in
                        # the log line; the raw text is PII and stays out of
                        # Cloud Logging.
                        _facility = _match_canonical_staging_facility(addr_part)
                        if _facility is not None:
                            event_log_additions.append(
                                f"Staging facility canonicalized: {addr_part} → {_facility[1]}"
                            )
                            logger.info(
                                "Staging facility canonicalized | n=%d facility=%s",
                                n, _facility[0],
                            )
                        addr_geo = _normalize_staging_geocode_query(addr_part, city_context)
                        if _facility is None and addr_geo != addr_part:
                            logger.info("Staging Pass B normalized | n=%d", n)
                        logger.info(
                            "Staging Pass B queued | n=%d is_officer=%s",
                            n, is_officer,
                        )
                        pending_geocode.append((len(staging_entries), addr_geo, is_officer))

                staging_entries.append({
                    "lat":   found_lat,   # may still be None — filled in by Pass B below
                    "lng":   found_lng,
                    "label": f"{n}. {body[:60]}",
                    "type":  s_type,
                })

        # Pass B: geocode unmatched entries (officer AND alternates) in parallel (non-fatal)
        # Two-phase approach:
        #   Phase 1 — _geocode_lkp_smart (Nominatim primary; intersections route directly
        #             to Google Maps because Nominatim cannot resolve "X & Y" queries —
        #             same hygiene the LKP path uses). Officer staging is the dominant
        #             intersection-bearing source here (e.g. "ALMA @ 10TH").
        #   Phase 2 — Google Maps fallback (parallel, only when Phase 1 returns None)
        #             Better spelling-correction tolerance; catches officer address typos.
        if pending_geocode:
            nom_results = await asyncio.gather(
                *[_geocode_lkp_smart(addr) for _, addr, _ in pending_geocode]
            )

            # Phase 2: for any entry that Nominatim failed, queue a Google Maps call.
            _gm_passb_tasks: list = []
            _gm_passb_indices: list[int] = []
            if _GOOGLE_MAPS_API_KEY:
                for i, nom_result in enumerate(nom_results):
                    if nom_result is None:
                        _, _addr_pb, _ = pending_geocode[i]
                        _gm_passb_tasks.append(_geocode_google_maps(_addr_pb))
                        _gm_passb_indices.append(i)
            _gm_passb_results = await asyncio.gather(*_gm_passb_tasks) if _gm_passb_tasks else []
            _gm_passb_by_idx = {
                _gm_passb_indices[j]: _gm_passb_results[j]
                for j in range(len(_gm_passb_tasks))
            }

            for i, ((entry_idx, addr, pg_is_officer), nom_result) in enumerate(
                zip(pending_geocode, nom_results)
            ):
                gm_tuple = _gm_passb_by_idx.get(i)  # 5-tuple or None

                if nom_result and _staging_geocode_implausible(geo, nom_result[0], nom_result[1]):
                    _pb_dist_m = _haversine_m(geo[0], geo[1], nom_result[0], nom_result[1])
                    if pg_is_officer:
                        # #773 — an address the OFFICER wrote beats the distance
                        # guard, exactly as a written-out coordinate does (#668).
                        # This guard is relative and its reference point is the
                        # geocoded LKP, which is itself unvalidated: on 2026-08-23
                        # the anchor was ~370 mi away in Nevada and rejecting here
                        # destroyed the only correct location on the form.
                        #
                        # Ungated on this leg: Nominatim reports no specificity
                        # signal to test. NOTE this also lets an intersection-
                        # shaped entry through ungated — those are Google answers
                        # whose location_type _geocode_lkp_smart discards. Known,
                        # narrow, documented in _staging_answer_is_region(), #779.
                        staging_entries[entry_idx]["lat"] = nom_result[0]
                        staging_entries[entry_idx]["lng"] = nom_result[1]
                        event_log_additions.append(
                            _staging_address_distance_note(addr, _pb_dist_m)
                        )
                        logger.warning(
                            "Staging Pass B Nominatim officer address far from LKP — kept, anchor may be wrong"
                        )
                    else:
                        # Too far from the LKP to be staging — treat exactly like a
                        # geocode failure so the officer last-resort fallback below
                        # still runs. See _staging_geocode_implausible().
                        event_log_additions.append(_staging_distance_note(
                            addr, pg_is_officer, _pb_dist_m,
                        ))
                        logger.warning("Staging Pass B Nominatim geocode rejected — implausibly far from LKP")
                elif gm_tuple and not nom_result and _staging_geocode_implausible(
                    geo, gm_tuple[0], gm_tuple[1]
                ):
                    # Same rejection for the Google Maps fallback — and crucially,
                    # SKIP the street "correction" below. That correction is what
                    # rewrote "CalTopo Map ID:" (a label from our own summary
                    # template that Gemini emitted as a staging VALUE — see
                    # _staging_geocode_implausible) to "California" throughout the
                    # summary on 2026-07-24, leaving a stray "California" line
                    # under Staging Area for Resources in both dispatch summaries.
                    # A geocode we do not trust must not be trusted to respell text.
                    _pb_dist_m = _haversine_m(geo[0], geo[1], gm_tuple[0], gm_tuple[1])
                    if pg_is_officer and not _staging_answer_is_region(gm_tuple[4]):
                        # #773 — see the Nominatim leg above. Google DOES report
                        # specificity, so this leg keeps the officer's address
                        # only when Google answered with a place rather than an
                        # area. That is what still rejects the 2026-07-24
                        # "CalTopo Map ID:" -> "California" state centroid, which
                        # arrived through this same officer field.
                        #
                        # The street correction below stays skipped. Trusting a
                        # coordinate enough to plot a marker the dispatcher can
                        # see is not the same as trusting it to rewrite the
                        # summary text, which propagates to the responders'
                        # maps-link query on every surface. Revisit: #780.
                        staging_entries[entry_idx]["lat"] = gm_tuple[0]
                        staging_entries[entry_idx]["lng"] = gm_tuple[1]
                        event_log_additions.append(
                            _staging_address_distance_note(addr, _pb_dist_m)
                        )
                        logger.warning(
                            "Staging Pass B Google Maps officer address far from LKP — kept, anchor may be wrong"
                        )
                    else:
                        event_log_additions.append(_staging_distance_note(
                            addr, pg_is_officer, _pb_dist_m,
                        ))
                        logger.warning("Staging Pass B Google Maps geocode rejected — implausibly far from LKP")
                elif nom_result:
                    # City-consistency guard (#736) — the SAME guard the LKP and
                    # Residence legs already run, on the same Nominatim SUCCESS
                    # path and scoped the same way. It is deliberately NOT applied
                    # to the Google fallback below: the guard's whole move is
                    # "Nominatim named the wrong city, ask Google", so an answer
                    # that already came from Google has nowhere to escalate.
                    #
                    # Without this the identical address string was city-checked
                    # on one path and accepted unchecked on the other. Live
                    # 2026-08-09: LKP and Residence were both corrected to the
                    # city the address specifies, while the officer's staging
                    # entry — the same address, same request — kept Nominatim's
                    # wrong-city answer and put the CP marker ~10 km away in a
                    # different city, on a map whose LKP pin was correct.
                    #
                    # No existing guard could catch it. _staging_geocode_implausible
                    # runs above with _MAX_STAGING_DIST_M = 50 km, so a 10 km error
                    # passes comfortably — the #646 Richey Center shape, where a
                    # wrong answer carrying a house number and high reported
                    # precision ships silently.
                    #
                    # A street-name comparison would NOT work here and must not be
                    # substituted for this: the same street exists in both cities
                    # and the providers disagree on its spelling (OSM renders it
                    # one word, Google two), so a name check compares rendering
                    # conventions rather than places. The city is the discriminator.
                    _pb_city_label = (
                        "Officer staging address" if pg_is_officer
                        else "Staging address"
                    )
                    # `suspect` is dropped, as on the Residence leg: it exists to
                    # arm the #605 stale-locality gate, which is an LKP-anchor
                    # concern with no staging equivalent. The dispatcher-facing
                    # note below is emitted on BOTH the corrected and the
                    # unresolved path, so nothing is silent either way.
                    nom_result, _pb_city_note, _ = await _reconcile_geocode_city(
                        addr, nom_result, _pb_city_label
                    )
                    if _pb_city_note:
                        event_log_additions.append(_pb_city_note)
                    # Deliberately NOT re-running the distance check on a
                    # corrected coordinate. _reconcile_geocode_city only replaces
                    # when Google agrees on BOTH the house number and the city, so
                    # the result is the address exactly as written; if that is far
                    # from the LKP the staging genuinely is far (mutual aid), and
                    # the pre-correction answer would already have been rejected
                    # above.
                    alt_lat, alt_lng, _, _ = nom_result
                    staging_entries[entry_idx]["lat"] = alt_lat
                    staging_entries[entry_idx]["lng"] = alt_lng
                    logger.info("Staging Pass B Nominatim geocoded")
                    # No event log entry on success — only corrections/failures reach dispatcher.
                elif gm_tuple:
                    gm_lat, gm_lng, _, gm_formatted, _ = gm_tuple
                    staging_entries[entry_idx]["lat"] = gm_lat
                    staging_entries[entry_idx]["lng"] = gm_lng
                    logger.info("Staging Pass B Google Maps geocoded")
                    _pb_label = "Officer staging address" if pg_is_officer else "Staging address"
                    _pb_correction, _pb_rejected = _street_correction_note(
                        addr, gm_formatted, _pb_label
                    )
                    if _pb_rejected:
                        event_log_additions.append(_pb_rejected)
                    if _pb_correction:
                        _label = _pb_label
                        _pb_input_street = addr.split(",")[0].strip()
                        # Same abbreviation-only guard as LKP/Residence — only log and
                        # propagate genuine misspelling corrections, not format normalisations.
                        if _is_substantive_street_correction(_pb_input_street, _pb_correction):
                            event_log_additions.append(
                                f"{_label} corrected: \"{_pb_input_street}\" → "
                                f"\"{_pb_correction}\" "
                                f"(Google Maps verified spelling — verify with officer if incorrect)"
                            )
                            # Propagate corrected spelling into summary (covers staging section
                            # already appended by Gemini; idempotent if LKP pass fixed it first).
                            summary = _apply_street_correction_to_summary(
                                summary, _pb_input_street, _pb_correction
                            )
                            # Also update the CalTopo pin label so it matches the corrected
                            # spelling in the dispatcher textarea (label is set in Pass A from
                            # Gemini's raw body text, before Pass B corrections run).
                            staging_entries[entry_idx]["label"] = _apply_street_correction_to_summary(
                                staging_entries[entry_idx]["label"], _pb_input_street, _pb_correction
                            )
                else:
                    logger.info("Staging Pass B geocode failed (Nominatim + Google Maps)")
                    # Officer entry that failed both geocoders — hits LKP fallback below.
                    # Event log entry added there (after fallback fires) so we only log once.

        # Post-Pass-B: officer entries still without coords fall back to LKP.
        # This is the guaranteed last-resort fallback — the officer CP marker MUST always
        # appear on the map even when the specified park name is misspelled or unknown to OSM.
        for entry in staging_entries:
            if entry["type"] == "officer" and entry["lat"] is None and geo:
                lat, lng, _, _ = geo
                entry["lat"] = lat
                entry["lng"] = lng
                # Extract the location name (text before the em-dash) from the label.
                # Use re.sub(r"^\d+\.\s*", "") — same fix as the comparison block below
                # (lstrip strips house numbers, not just the "N. " sequential prefix).
                officer_loc = re.sub(r"^\d+\.\s*", "", entry["label"].split(" — ")[0]).strip()
                logger.info(
                    "Staging officer entry: LKP fallback after Pass A + Pass B missed | label=%r",
                    entry["label"][:60],
                )
                # CRITICAL: dispatcher must know the officer's staging location could not be
                # found — they need to call the officer to verify spelling or get directions.
                event_log_additions.append(
                    f"WARNING: Officer-specified staging \"{officer_loc}\" could not be geocoded "
                    f"— verify spelling with officer; Command Post marker placed at LKP on map"
                )

        # Warn dispatcher if Residence geocoding failed.
        # Message is conditional on whether LKP geocoding also failed:
        #   - LKP OK  → marker plotted at LKP coords (fallback worked)
        #   - LKP bad → no map was created, so "plotted at LKP" would be misleading
        if res_lat is None and residence_query:
            if geo:
                event_log_additions.append(
                    f"WARNING: Residence address \"{res_address_p1}\" could not be geocoded "
                    f"— Residence marker plotted at LKP on map; verify address with officer"
                )
            else:
                event_log_additions.append(
                    f"WARNING: Residence address \"{res_address_p1}\" could not be geocoded "
                    f"— verify address with officer"
                )

        # Warn dispatcher when staging/map are based on Residence rather than a confirmed LKP.
        # This is distinct from a geocoding failure — we succeeded and generated staging, but
        # the officer never provided an LKP so the dispatcher must confirm with the officer.
        if _lkp_from_residence and geo:
            event_log_additions.append(
                "WARNING: Last Known Position not provided — staging and CalTopo map based on "
                "Residence address; confirm actual LKP with officer"
            )

        # Warn dispatcher if LKP geocoding failed — the LKP marker won't be placed on
        # the CalTopo map. The map itself IS still created (seeded with Residence or
        # first staging via _build_seed_feature in caltopo.py), and any Overpass
        # staging recommendations still render. The dispatcher must verify the LKP
        # location with the officer before dispatching.
        if not geo and geocode_query:
            event_log_additions.append(
                "WARNING: LKP address could not be geocoded — LKP marker not placed "
                "on CalTopo map; verify LKP with officer before dispatching"
            )
        elif geo and not any(e["lat"] is not None for e in staging_entries):
            # LKP geocoded but no staging entries survived (wilderness, Overpass returned
            # nothing, or all candidates were filtered out after dedup). Not as critical but
            # dispatcher should know staging is absent so they can ask the officer directly.
            # Note: map_data["staging"] is assigned after this block, so we check
            # staging_entries directly rather than map_data.get("staging").
            event_log_additions.append(
                "WARNING: No staging recommendations generated — verify staging location "
                "with officer"
            )

        # Staging area correction detection: if the officer wrote a staging area name that
        # was silently corrected (e.g. "CARDOLA PARK" → "Cardoza Park, Milpitas" via Gemini
        # and Overpass), log it so the dispatcher can verify the match is correct.
        # Only fires when the first significant word of the officer's text and the matched
        # OSM entry differ (case-insensitive, alphanumeric chars only) — catches misspellings
        # but ignores formatting differences like "CARDOZA PARK MILPITAS" → "Cardoza Park, Milpitas".
        try:
            # `[^\S\n]*` not `\s*` — see the PASS 3 note above. Both readers of this
            # field must agree, or the mismatch check compares the officer's real
            # text against a value the injector never saw.
            _sa_field_m = re.search(r"^Staging Area for Resources:[^\S\n]*(.+)$", summary, re.MULTILINE)
            if _sa_field_m:
                _sa_raw = _sa_field_m.group(1).strip()
                _officer_entry = next(
                    (e for e in staging_entries if e.get("type") == "officer"), None
                )
                if (
                    _officer_entry
                    and _sa_raw
                    and not re.match(r"^(none|tbd|n/?a)\b", _sa_raw, re.IGNORECASE)
                ):
                    # Extract the location name from the staging label (text before " — ").
                    # DESIGN DECISION (PR #193): use re.sub(r"^\d+\.\s*", "") to strip ONLY the
                    # sequential "N. " prefix that main.py prepends to every label.  The old
                    # lstrip("0123456789. ") was too aggressive — it stripped each character in
                    # the set, so a house number like "10410" was consumed along with the "1. "
                    # prefix, leaving "CALVERT DR..." as the first word.  When _sa_raw started
                    # with the house number ("10000 CALVERT DR."), first-word mismatch fired
                    # spuriously even though officer staging == LKP (same address both fields).
                    _matched = re.sub(r"^\d+\.\s*", "", _officer_entry["label"].split(" — ")[0]).strip()

                    # Shared with the PASS 3 "(as written: …)" suffix — the note and
                    # the suffix must fire together or the disclosure is half done.
                    if _staging_first_word_key(_sa_raw) != _staging_first_word_key(_matched):
                        event_log_additions.insert(0,
                            f"Staging area \"{_sa_raw[:100]}\" was matched to \"{_matched}\" "
                            f"— verify with officer if incorrect"
                        )
        except Exception as exc:
            logger.debug("staging mismatch check skipped: %s", type(exc).__name__)

        # Issue #675: name any questionnaire item the form left blank. Appended
        # LAST so it reads after the staging/geocoding corrections above — those
        # are things the dispatcher can act on now, this one usually needs the
        # officer. Wrapped because a note is never worth losing the real
        # geocoding warnings that are already queued in event_log_additions.
        try:
            _blank_note = _unanswered_lpb_note(summary)
            if _blank_note:
                event_log_additions.append(_blank_note)
                logger.info("Unanswered LPB rows flagged | count=%d",
                            len(_LPB_UNANSWERED_RE.findall(summary)))
        except Exception as exc:
            logger.debug("unanswered-LPB note skipped: %s", type(exc).__name__)

        # Inject any geocoding event log entries into the Event Log section of the summary.
        # These entries alert the dispatcher when the officer's staging location had issues.
        if event_log_additions:
            # Issue #542: canonical UTC + PT-hint format. See _format_event_log_ts.
            _ts = _format_event_log_ts()
            _el_marker = "\nEvent Log:\n"
            _sep = "\n---\n"
            _el_pos = summary.find(_el_marker)
            if _el_pos != -1:
                _sep_pos = summary.find(_sep, _el_pos)
                if _sep_pos != -1:
                    additions_text = "\n".join(f"{_ts} - {line}" for line in event_log_additions)
                    summary = summary[:_sep_pos].rstrip("\n") + "\n" + additions_text + summary[_sep_pos:]
                    logger.info(
                        "Event log additions injected | count=%d", len(event_log_additions)
                    )

        # Drop any entries that still have no coords after both passes + LKP fallback
        map_data["staging"] = [e for e in staging_entries if e["lat"] is not None]

        # --- Event name (map title) ---
        event_name_match = re.search(r"^Event Name:\s*(.+)", summary, re.MULTILINE)
        map_data["event_name"] = event_name_match.group(1).strip() if event_name_match else "SAR Incident"

        # Carried forward for the #605 stale-locality gate at dispatch time.
        # `lkp_locality` is what the LKP actually resolved to ("Milpitas" on
        # 2026-07-24); `lkp_locality_requested` is what the address said
        # ("Santa Clara"). `lkp_locality_suspect` arms the gate and is True ONLY
        # when #604 could not reconcile the two — a mismatch Google Maps fixed
        # leaves a correct anchor and must not arm anything.
        map_data["lkp_locality"] = (geo[3] or "") if geo else ""
        map_data["lkp_locality_requested"] = _extract_query_city(geocode_query) or ""
        map_data["lkp_locality_suspect"] = _lkp_locality_suspect

        res_info = "no"
        if map_data.get("residence"):
            res_info = "geocoded" if (res_lat is not None) else "fallback-to-lkp"
        logger.info(
            "map_data built | lkp=%s residence=%s staging_count=%d",
            "yes" if map_data.get("lkp") else "no",
            res_info,
            len(staging_entries),
        )

    except Exception as exc:
        logger.warning("map_data build failed (non-fatal): %s", type(exc).__name__)
        map_data = {}

    # Replace the "Staging Area for Resources:" line content with the #1
    # quality-ranked recommendation's bare form (LOCATION — TYPE), so both
    # display surfaces (WhatsApp + Full) show the SAME staging answer.
    #
    # DESIGN DECISION (do not revert without team discussion — issue #244):
    # The original "Staging Area for Resources" field copied the officer's
    # written staging verbatim. That encoded the pre-#244 rule "officer-
    # designated staging always wins." Issue #244 changed that rule — officer
    # staging is now ONE entry in the quality-ranked Recommendations list
    # with an "Officer-designated staging location" label, and is NOT
    # auto-promoted. The IIS field continued to show the officer's text,
    # silently re-introducing the old priority semantics: dispatchers reading
    # the IIS saw "Staging Area for Resources: Alma & 10th" while
    # WhatsApp/Recommendations showed "1. Horace Mann ES" — same payload,
    # two different staging answers (2026-05-09 SJSU smoke test).
    #
    # The fix is content-replacement (not strip): the field stays so dispatchers
    # have a single line to point teams at, but its value is now derived from
    # the same #1 recommendation that the WhatsApp summary and Recommendations
    # list use. Officer's preference is still preserved as a labeled entry in
    # the Recommendations list (PASS 3 ensures it appears even when Gemini
    # drops it), but no longer hijacks the top-level field.
    #
    # IMPORTANT ORDERING: this replacement MUST run AFTER the consumers that
    # parse the original (officer) value of the field — PASS 3 staging injection
    # (around line 1898) reads the officer text to inject the entry into
    # Recommendations, and the staging-area mismatch check (around line 2619)
    # reads it to log a "verify with officer" note when Gemini matched the
    # officer's misspelling to a different OSM entry. Both consumers run inside
    # the staging-handling try block above; this replacement is placed AFTER
    # that block so internal logic sees the original officer text and the
    # display surfaces see the unified #1 value.
    _top_rec_bare = ""
    _top_rec_match = re.search(
        r"\nStaging Area Recommendations:\n(.*?)(?=\n---\n)",
        summary,
        re.DOTALL,
    )
    if _top_rec_match:
        for _sline in _top_rec_match.group(1).splitlines():
            _m1 = re.match(r"^1\.\s+(.+)$", _sline)
            if _m1:
                _entry_text = _m1.group(1).strip()
                # Strip the officer-designated suffix if #1 happens to be the
                # officer's location — the bare form is the same either way.
                _entry_text = re.sub(
                    r"\s+—\s+Officer-designated staging location.*$", "", _entry_text
                )
                # "LOCATION — TYPE. details sentence." → "LOCATION — TYPE"
                # Split on " — " FIRST so a ". " inside the LOCATION (a middle
                # initial "Joseph D.", or "Mt."/"St." abbreviations in a
                # geocoder-canonicalized park name) is not mistaken for the
                # end-of-type sentence boundary. Pre-fix `split(". ")[0]` on
                # "Joseph D. Grant County Park, San Jose — County park. 0.3 mi…"
                # truncated to "Joseph D" (2026-07-16 SJ Grant mock). Only the
                # TYPE's trailing prose sentence is trimmed.
                if " — " in _entry_text:
                    _loc, _rest = _entry_text.split(" — ", 1)
                    _top_rec_bare = f"{_loc.strip()} — {_rest.split('. ', 1)[0].strip()}"
                else:
                    _top_rec_bare = _entry_text.split(". ", 1)[0].strip()
                break
    if _top_rec_bare:
        summary = re.sub(
            r"^Staging Area for Resources:.*$",
            lambda m: f"Staging Area for Resources: {_top_rec_bare}",
            summary,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        # No #1 recommendation available (Overpass + Gemini both failed). Blank
        # out the line rather than leaving stale officer text in place. The
        # dispatcher reads the (likely empty) Recommendations section directly.
        summary = re.sub(
            r"^Staging Area for Resources:.*$",
            "Staging Area for Resources:",
            summary,
            count=1,
            flags=re.MULTILINE,
        )

    # WhatsApp sunset (issue #614, 2026-07-25). The textarea used to carry a
    # `━━━ WHATSAPP DISPATCH ━━━` block above the full summary, plus a bare
    # copy-pasteable address line for mobile long-press-and-copy. Both existed
    # only to feed a manual paste into WhatsApp.
    #
    # WhatsApp is sunset: Slack has been `full` on both environments, a real
    # callout ran end to end on 2026-07-24 with no WhatsApp use and no
    # complaints, and Slack now renders the staging address as a tappable link
    # — which is what retired the long-press-copy rationale for the bare line
    # (Bill, 2026-07-25). The textarea is now the single full summary.

    # Force GC + emit RSS telemetry. Background polling leaves uncollected
    # gen2 garbage that Python's generational thresholds don't always sweep
    # on their own under polling-heavy workloads; explicit collection on
    # /ocr exit (the natural quiescent point) keeps the heap at steady
    # state. Without this, RSS drifted upward across hours of polling until
    # the container OOM'd at 512 MiB — that was the #519 pattern. The
    # collect adds ~50-200ms to an already 50-80s OCR call; negligible.
    # RSS fields let ops detect any regression — see scripts/oom-check.
    _rss_post_mib = _rss_mib()
    _gc_collected = gc.collect()
    _rss_post_gc_mib = _rss_mib()
    logger.info(
        "OCR request complete | sub=%s total_ms=%d "
        "rss_post_mib=%d delta_mib=%d rss_post_gc_mib=%d gc_recovered_mib=%d gc_collected=%d",
        user_sub,
        int((time.monotonic() - t_ocr_start) * 1000),
        _rss_post_mib,
        _rss_post_mib - _rss_pre_mib,
        _rss_post_gc_mib,
        # Batch-3 G.LOW: clamp at 0. Concurrent OCR requests can allocate
        # large Gemini protobuf buffers between the two _rss_mib() reads,
        # making _rss_post_gc_mib > _rss_post_mib and producing a negative
        # gc_recovered_mib that looks like instrumentation breakage in
        # ops scans. The metric's intent is "memory freed by gc.collect()";
        # a negative value just means "concurrent allocation outpaced
        # the collection" — clamp to 0 so the metric stays interpretable.
        # See Melanie's 2026-05-30 batch-3 finding #8.
        max(0, _rss_post_mib - _rss_post_gc_mib),
        _gc_collected,
    )
    return JSONResponse({"text": summary, "map_data": map_data})


# ---------------------------------------------------------------------------
# /create-map — create a CalTopo SAR incident map and return the URL
# ---------------------------------------------------------------------------

@app.post("/create-map")
async def create_map(
    request: Request,
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """
    Create a CalTopo SAR incident map from structured map_data.

    Accepts JSON body: {"map_data": {...}} (same schema as returned by /ocr).
    Returns: {"map_url": "https://caltopo.com/m/XXXXX"}

    Auth: requires valid Google ID token from authorized dispatcher (same as /ocr).
    Privacy: no incident data is stored server-side — passed through to CalTopo API only.
    """
    user_sub = dispatcher.get("sub", "unknown")
    # 50 KB ceiling — map_data is a structured dict (lat/lng, label text,
    # staging candidates list); typical body is <10 KB. Pre-rejects
    # accidentally-bloated payloads before httpx forwards to CalTopo.
    if _content_length_exceeds(request.headers.get("content-length"), 50_000):
        raise HTTPException(status_code=413, detail="Request body too large")
    try:
        body = await request.json()
        map_data = body.get("map_data", {})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not map_data:
        raise HTTPException(status_code=400, detail="map_data is required")

    # ---- Stale-locality hard-confirm BEFORE map creation (issue #622) -------
    # ARMED ONLY when #604 flagged the LKP geocode as suspect — the resolved
    # city contradicted the address AND Google Maps could not fix it.
    #
    # WHY THIS GATE EXISTS SEPARATELY FROM THE DISPATCH GATE (#605): map
    # creation is a point of no return that the app CANNOT undo. It
    # deliberately holds no CalTopo DELETE privileges (per Bill 2026-05-30 —
    # see caltopo.py: "the recovery path is a human deleting the orphan map via
    # the CalTopo UI"). Gating only at dispatch meant a fully-populated
    # wrong-city map already existed, permanently, before the dispatcher was
    # ever asked to confirm — observed in live testing 2026-07-25, 9 markers
    # anchored in Milpitas for a Santa Clara address.
    #
    # NO SURFACE SCAN HERE, unlike the dispatch gate. At map time the
    # dispatcher has not composed a notification title or body, so there is no
    # stale text to find; the question is simply whether to anchor a permanent
    # map on a geocode we already distrust. The dispatch gate still runs later
    # and scans the text written AFTER this point — which is where the
    # 2026-07-24 wrong-city dispatch actually survived to responders. Both
    # gates are required; neither subsumes the other.
    #
    # Ordering: before check_rate_limits() (the check is pure, and a correction
    # cycle must not burn the shared budget) and necessarily before the
    # build_incident_map() call below, which is the point of no return.
    #
    # Distinct code from the dispatch gate ("stale_locality_map" vs
    # "stale_locality") so the frontend branches unambiguously and a future
    # change to one message cannot silently retarget the other.
    if map_data.get("lkp_locality_suspect") and not bool(body.get("stale_locality_ack")):
        _resolved = map_data.get("lkp_locality") or ""
        _requested = map_data.get("lkp_locality_requested") or ""
        logger.warning(
            "create-map | suspect LKP locality — hard-confirm required | sub=%s", user_sub,
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "stale_locality_map",
                "locality": _resolved,
                "requested": _requested,
                # WRITTEN FOR SOMEONE MID-CALLOUT (Bill, live test 2026-07-25:
                # "go back to first principles when communicating during an
                # incident. Tell me what and how to fix it, now isn't the time
                # for lengthy why"). Impact in the first line, then the fix.
                # No history, no rationale — those live in the code comments
                # above and in the Event Log, not in a modal blocking a
                # dispatcher who needs to act.
                #
                # "resubmit that form to Dispatch Turbo" is deliberate: "correct
                # the form and re-upload" read as though the form could be
                # edited inside Turbo, which it cannot.
                "message": (
                    f"HOLD — this map would be anchored in the wrong city and may send "
                    f"responders to the wrong location.\n\n"
                    f"Address says {_requested or 'one city'}. "
                    f"Map lookup resolved to “{_resolved or 'somewhere else'}”. "
                    f"Every marker would be placed there.\n\n"
                    # LIVE-TEST CORRECTION 2026-07-25: this used to list "Apply
                    # Override" under "Fix it:", which is FALSE here. The gate is
                    # armed by lkp_locality_suspect, set once by /ocr; an override
                    # mutates only _rawMapData.staging and can NEVER clear it. Bill
                    # applied overrides in two different cities, got the same modal
                    # both times, and said "it's not clear which address/city I can
                    # pick from when I'm in this mode." The answer was: none of them.
                    # A remedy that cannot resolve the condition it is attached to is
                    # worse than no remedy — it burns the one thing a dispatcher has
                    # least of. Only a corrected form re-runs the lookup.
                    f"Only a corrected form clears this:\n"
                    f"• Fix the street name on the original intake form and resubmit it "
                    f"to Dispatch Turbo.\n\n"
                    f"“Apply Override” will NOT clear this warning — it moves staging "
                    f"only, and the LKP marker stays in {_resolved or 'the wrong city'}.\n\n"
                    f"Create the map anyway only if “{_resolved}” is correct."
                ),
            },
        )

    # Extract dispatcher last name for CalTopo map description — same pattern as /ocr (Build 20).
    dispatcher_name = ""
    full_name = dispatcher.get("name", "").strip()
    if full_name:
        dispatcher_name = full_name.split()[-1]  # Last word of Google display name

    email = dispatcher.get("email", "")
    await check_rate_limits(email)

    t_map_start = time.monotonic()
    logger.info("create-map request | sub=%s event=%r", user_sub, map_data.get("event_name", "?")[:40])

    try:
        # build_incident_map is synchronous (httpx.Client, not AsyncClient) — run in thread pool
        # to avoid blocking the async event loop during CalTopo API calls.
        loop = asyncio.get_running_loop()
        map_url = await loop.run_in_executor(
            None,
            functools.partial(build_incident_map, map_data, dispatcher_name=dispatcher_name),
        )
    except CalTopoRateLimitError:
        # 429 is transient. Surface 503 with Retry-After so the dispatcher
        # knows retrying in a few seconds is the right action — distinct
        # from the opaque 502 "map creation failed" that today is the only
        # signal regardless of why CalTopo failed. 4 months of operations
        # have seen zero CalTopo 429s; this branch is defensive
        # instrumentation per CLAUDE.md Failure-mode Discipline Q5 and
        # batch-3 finding PR-H.1.
        logger.warning("CalTopo rate limited | sub=%s", user_sub)
        raise HTTPException(
            status_code=503,
            detail="CalTopo is rate-limited; please wait a moment and try again.",
            headers={"Retry-After": "10"},
        )
    except CalTopoOrphanMapError as exc:
        # Batch-3 PR-H.2: the map WAS created on CalTopo but marker
        # addition failed mid-way. The app deliberately doesn't have
        # CalTopo DELETE privileges (per Bill 2026-05-30), so the
        # orphan map sits on the team account until manually deleted.
        # Surface the partial_map_id in the 502 detail so the
        # dispatcher can quote it when asking the maintainer for
        # cleanup. The structured WARNING already logged in caltopo.py
        # has the full diagnostic context.
        logger.warning(
            "CalTopo orphan map returned to dispatcher | sub=%s "
            "partial_map_id=%s markers_added=%d/%d failure_step=%s",
            user_sub, exc.partial_map_id, exc.markers_added,
            exc.markers_intended, exc.failure_step,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"CalTopo map partially created — orphan map id: "
                f"{exc.partial_map_id}. Please notify the maintainer "
                f"for cleanup. (Markers added: {exc.markers_added}/"
                f"{exc.markers_intended})"
            ),
        )
    except RuntimeError as exc:
        logger.error("CalTopo map creation failed | sub=%s error=%s", user_sub, exc)
        raise HTTPException(status_code=502, detail="CalTopo map creation failed")

    logger.info(
        "CalTopo map created | sub=%s total_ms=%d url=%s",
        user_sub,
        int((time.monotonic() - t_map_start) * 1000),
        map_url,
    )
    # Issue #542: return a pre-formatted event_log_entry so the frontend
    # doesn't have to render the timestamp client-side (which was producing
    # browser-local time, leading to TZ divergence vs the server-rendered
    # OCR-time entries). The frontend passes this string as-is to
    # _insertEventLogEntries, which detects the pre-formatted shape and
    # inserts without re-prepending.
    event_log_entry = f"{_format_event_log_ts()} - CalTopo map created: {map_url}"
    return JSONResponse({"map_url": map_url, "event_log_entry": event_log_entry})


# ---------------------------------------------------------------------------
# POST /create-doc — create a Google Doc "working notes" from textarea content
# ---------------------------------------------------------------------------

@app.post("/create-doc")
async def create_doc(
    request: Request,
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """
    Create a Google Doc pre-populated with the dispatcher's textarea content.
    Shares it silently (writer, no notification email) with all authorized
    dispatchers listed in AUTHORIZED_EMAILS.

    Accepts JSON body: {"event_name": str, "content": str}
    Returns: {"doc_url": "https://docs.google.com/document/d/.../edit"}

    Auth: requires valid Google ID token from authorized dispatcher.
    Rate-limited: same Firestore limiter as /ocr (shared quota).
    """
    t0 = time.monotonic()
    user_sub = dispatcher.get("sub", "unknown")
    email = dispatcher.get("email", "")

    # Parse and validate body BEFORE rate limiting — a malformed request or UI glitch
    # should not burn a rate-limit slot (dispatchers have a tight hourly budget).
    _MAX_DOC_BODY_BYTES = 100_000
    _cl_header = request.headers.get("content-length")
    if _cl_header:
        try:
            if int(_cl_header) > _MAX_DOC_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Request body too large")
        except ValueError:
            pass  # malformed header — let the post-parse check below catch it

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_name = (body.get("event_name") or "SAR Incident").strip()
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Missing 'content'")
    # Authoritative size check after parsing (Content-Length header is advisory).
    if len(content.encode()) > _MAX_DOC_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Content too large")

    # Rate limit — same cap as /ocr to prevent Drive quota exhaustion.
    # Applied after body validation so parse failures don't count against the user.
    await check_rate_limits(email)

    # Drive access token from the dispatcher's Google Sign-In session.
    # The frontend requests drive.file scope via initTokenClient() and forwards
    # the short-lived access token here — no service account required.
    drive_access_token = (body.get("drive_access_token") or "").strip()
    if not drive_access_token:
        raise HTTPException(status_code=400, detail="Missing 'drive_access_token'")

    # Dispatcher last name for doc header — same pattern as /create-map.
    dispatcher_name = ""
    full_name = dispatcher.get("name", "").strip()
    if full_name:
        dispatcher_name = full_name.split()[-1]

    # Build the sharing list — exclude the dispatcher who is creating the doc
    # because they already own it (their token was used) and adding them again
    # would cause a Drive API 400 error.
    dispatcher_email_lower = email.lower()
    authorized_emails = [
        e.strip()
        for e in os.environ.get("AUTHORIZED_EMAILS", "").split(",")
        if e.strip() and e.strip().lower() != dispatcher_email_lower
    ]

    logger.info("create-doc request | sub=%s event=%r", user_sub, event_name[:40])

    try:
        # create_incident_doc is synchronous (google-api-python-client uses httplib2)
        # — run in thread pool to avoid blocking the async event loop.
        loop = asyncio.get_running_loop()
        doc_url = await loop.run_in_executor(
            None,
            functools.partial(
                create_incident_doc,
                event_name,
                content,
                dispatcher_name,
                authorized_emails,
                drive_access_token,
            ),
        )
    except (RuntimeError, ValueError, GoogleApiHttpError) as exc:
        logger.error("Google Doc creation failed | sub=%s error=%s", user_sub, exc)
        raise HTTPException(status_code=502, detail="Google Doc creation failed")

    # Log only the document ID — not the full URL — to avoid a persistent pointer
    # to PII-containing content in Cloud Run logs.  The full URL is returned to
    # the frontend but should not appear in the operational log stream.
    doc_id = doc_url.split("/d/")[1].split("/")[0] if "/d/" in doc_url else "unknown"
    logger.info(
        "Google Doc created | sub=%s total_ms=%d doc_id=%s",
        user_sub,
        int((time.monotonic() - t0) * 1000),
        doc_id,
    )
    return JSONResponse({"doc_url": doc_url})


# ---------------------------------------------------------------------------
# POST /apply-staging-override (PR-D-1, 2026-05-10) — dispatcher-specified
# staging override.
#
# Why: the OCR pipeline can produce catastrophically wrong staging
# recommendations when handwriting is illegible (Verde Vista 2026-05-10:
# officer staging text geocoded 137 miles off in CA's geographic centroid)
# or when last-minute operational knowledge isn't on the form. No downstream
# defensive helper can rescue a fundamentally-wrong staging anchor — the
# right intervention layer is human-in-the-loop dispatcher override.
#
# Flow: dispatcher provides address / decimal lat-lng / SAR-standard UTM →
# backend resolves the anchor (geocoding the address, validating coords, or
# parsing UTM) → Overpass POI lookup at the anchor (300m, small radius) →
# returns primary entry + up to 5 nearby alternatives. Frontend mutates
# the textarea + cached map_data; downstream calls (CalTopo, EB+Slack,
# WhatsApp, Doc) pick up the override naturally.
#
# Security: auth via Google ID token + email allowlist (require_authorized_
# dispatcher); rate-limit per-email after auth; input validation rejects
# malformed requests with 400 (no PII echoed); PII logging hygiene logs
# only latency/outcome/nearby_count, never the address/coords.
#
# Pre-merge gate: SECURITY.md re-review trigger (new endpoint added).
# Assessment saved to research/security-assessment-2026-05-1X.md.
# ---------------------------------------------------------------------------

# Verbatim labels — pinned in test_main_regression.py per the cross-file
# literal pin policy (CLAUDE.md). Drift would silently break frontend and
# caltopo.py marker arbitration.
_DISPATCHER_OVERRIDE_LABEL = "Dispatcher-specified staging location"
_OFFICER_OVERRIDE_LABEL    = "Officer-designated staging location"

# Override anchor Overpass radius — small. Purpose is "is there a clearly-
# better-named POI right here?" not "rebuild the entire staging cluster
# around this point" (the OCR-time radius is 1200m for that). The dispatcher
# already knows the staging area; we're just helping them pick the right
# named POI.
_OVERRIDE_OVERPASS_RADIUS_M = 300

# Cap on nearby alternatives in the response. Keep small — the dispatcher
# is making a quick decision under pressure; a long list defeats the purpose.
_OVERRIDE_NEARBY_CAP = 5

# Issue #606 — warn (never block, never move anything) when a staging override
# lands implausibly far from the LKP.
#
# On 2026-07-24 the LKP geocoded to the wrong city. The dispatcher corrected
# staging, but the LKP and residence markers kept the bad coordinates, leaving
# markers ~13 km outside the search area. Another dispatcher texted to say he
# was deleting one himself because it was "a big distractor."
#
# 3 km. THIS NUMBER HAS BEEN WRONG TWICE — both times because the calibration
# used ESTIMATED coordinates instead of measured ones. Read this before changing it.
#
#   10 km (first pass)  — would not have fired on the incident that motivated #606
#    6 km (second pass) — same, and live testing 2026-07-25 proved it: the guard
#                         stayed silent on the exact scenario it was built for
#
# The real 2026-07-24 divergence is 4.99 km, NOT the ~13 km cited in the issue
# nor the 8.7 km I computed from guessed coordinates. Measured from the anchors
# the service actually used, recovered from the staging-POI query URLs in Cloud
# Logging:
#
#   LKP  447 Great Mall Dr -> resolved  37.4159771, -121.896571   (Milpitas)
#   override 600 Moreland Way, Santa Clara  37.3957815, -121.9469793
#   haversine                               4.99 km / 3.10 mi
#
# Measured reference points:
#
#     4.99 km  2026-07-24 override            MUST warn   (measured)
#     1.74 km  staging in the next town over  silent
#     0.08 km  what this team actually stages at          (measured, 2026-07-25 runs)
#
# 3 km gives 40% margin under the real case and is ~35x what normal staging
# measures. Agreed with Bill 2026-07-25 after the live test.
#
# ACCEPTED CONSEQUENCE: a large wilderness callout (LKP deep in a park, staging
# at the gate) can exceed 3 km and will warn. That is deliberate, not a bug to
# be "fixed" by raising the threshold — see
# test_wilderness_staging_may_warn_and_that_is_accepted. It is affordable
# ONLY because this warns and never blocks: a false positive costs a glance at
# a status line. If it is ever made blocking, this number must be revisited.
#
# Still far below the 50 km reject in _staging_geocode_implausible, pinned, so
# the two distance guards can never disagree.
_OVERRIDE_LKP_DIVERGENCE_M = 3_000

# Input length caps — defense against quota-abuse + log-bloat scenarios.
_OVERRIDE_ADDRESS_MAX_LEN = 200
_OVERRIDE_UTM_MAX_LEN     = 64


@app.post("/apply-staging-override")
async def apply_staging_override(
    request: Request,
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """Resolve a dispatcher-specified staging override + return nearby alternatives.

    Accepts JSON body with EXACTLY ONE of:
      - {"address": "100 Main St, City, CA"}
      - {"lat_lng": {"lat": 37.42, "lng": -121.97}}
      - {"utm": "10S 590309E 4142188N"}

    Returns on success:
      {
        "primary": {
          "address": "<input or geocoded display>",
          "lat": <float>, "lng": <float>,
          "label_verbatim": "Dispatcher-specified staging location"
        },
        "nearby": [
          {"display": "...", "lat": ..., "lng": ..., "amenity": "park"},
          ...
        ],
        "event_name_streetname_suggestion": "<extracted street>" | null,
        "outcome": "success" | "no_candidates",
        "resolved_locality": "<city the anchor resolved to>" | "",
        "search_radius_m": 300,
        "lkp_divergence": {"distance_mi": 8.1, "distance_km": 13.0} | null
      }

    Accepts an OPTIONAL {"lkp": {"lat": …, "lng": …}} alongside the coordinate
    input. It is context for the #606 divergence check, not a fourth input
    mode, and is exempt from the "exactly one of" rule.

    `outcome` distinguishes "found alternatives" from "anchor resolved but
    nothing within the radius" (issue #607) — the second is not a success and
    must not be reported as one. `resolved_locality` is empty for lat_lng and
    UTM modes, which never geocode.

    Returns 400 on input validation failure (no PII echoed).
    Returns 422 on geocoding failure (address could not be resolved).
    Returns 401/403 on auth failure (require_authorized_dispatcher).
    Returns 429 on rate-limit (check_rate_limits).

    PII logging: latency_ms, outcome, nearby_count only. Never the address,
    coords, UTM string, or any geocoded display string.
    """
    user_sub = dispatcher.get("sub", "unknown")
    email = dispatcher.get("email", "")
    t0 = time.monotonic()

    # Parse body. Reject malformed JSON before any expensive work.
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    address  = body.get("address")
    lat_lng  = body.get("lat_lng")
    utm_in   = body.get("utm")
    # Issue #606 — OPTIONAL {"lkp": {"lat": …, "lng": …}}. Absent on any older
    # frontend, and absent whenever LKP geocoding failed; both cases simply
    # skip the check. Same can't-check-means-allow contract the other guards
    # use. Not one of the three mutually-exclusive coordinate INPUTS — it is
    # context for the comparison, so it is excluded from the "exactly one of"
    # validation below.
    lkp_in   = body.get("lkp")

    # Normalize empty strings to None — frontend may send empty strings for
    # the inactive input modes.
    if isinstance(address, str) and not address.strip():
        address = None
    if isinstance(utm_in, str) and not utm_in.strip():
        utm_in = None
    if isinstance(lat_lng, dict) and not lat_lng:
        lat_lng = None

    # Set for the address mode only — the coordinate modes never geocode, so
    # there is no city to reconcile. Initialised here so the response can read
    # it unconditionally.
    _ov_city_note: str | None = None

    # Exactly one coordinate-input field must be present.
    provided = sum(1 for x in (address, lat_lng, utm_in) if x is not None)
    if provided == 0:
        raise HTTPException(status_code=400, detail="Provide one of: address, lat_lng, utm")
    if provided > 1:
        raise HTTPException(status_code=400, detail="Provide only one of: address, lat_lng, utm")

    # Per-input validation — applied BEFORE rate limiting so that obviously-bad
    # requests don't burn a rate-limit slot.
    if address is not None:
        if not isinstance(address, str):
            raise HTTPException(status_code=400, detail="address must be a string")
        if len(address) > _OVERRIDE_ADDRESS_MAX_LEN:
            raise HTTPException(status_code=400, detail="address too long")
    if utm_in is not None:
        if not isinstance(utm_in, str):
            raise HTTPException(status_code=400, detail="utm must be a string")
        if len(utm_in) > _OVERRIDE_UTM_MAX_LEN:
            raise HTTPException(status_code=400, detail="utm too long")
    if lat_lng is not None:
        if not isinstance(lat_lng, dict):
            raise HTTPException(status_code=400, detail="lat_lng must be an object")
        try:
            lat_v = float(lat_lng.get("lat"))
            lng_v = float(lat_lng.get("lng"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="lat_lng.lat and lat_lng.lng required as numbers")
        if not (-90.0 <= lat_v <= 90.0):
            raise HTTPException(status_code=400, detail="lat out of range")
        if not (-180.0 <= lng_v <= 180.0):
            raise HTTPException(status_code=400, detail="lng out of range")

    # Rate limit (per-email, async). Same pattern as /send-notification.
    await check_rate_limits(email)

    # Resolve anchor coordinates from the active input mode.
    anchor_lat: float | None = None
    anchor_lng: float | None = None
    primary_address: str = ""  # Display string returned to dispatcher.
    # Initialised here, not inside the address branch — the lat_lng and UTM
    # modes never geocode, so there is no locality to report and the response
    # builder below reads this unconditionally. Honest empty beats a crash.
    resolved_locality: str | None = None

    if lat_lng is not None:
        anchor_lat = lat_v
        anchor_lng = lng_v
        primary_address = _format_coordinate_staging_display(lat_v, lng_v)

    elif utm_in is not None:
        parsed = _parse_utm_string(utm_in)
        if parsed is None:
            logger.info(
                "apply-staging-override | sub=%s outcome=invalid_utm latency_ms=%d",
                user_sub, int((time.monotonic() - t0) * 1000),
            )
            raise HTTPException(status_code=400, detail="Invalid UTM string")
        anchor_lat, anchor_lng = parsed
        # PR-D-2.5 (2026-05-10): keep the dispatcher's original UTM string
        # visible in the displayed primary so SAR-radio responders see the
        # SAR-standard form they're trained on, while digital-map responders
        # get the lat/lng pair that map URLs accept. Same em-dash separator
        # convention as the existing "<address> — <name>" entries (e.g.,
        # "Branham Park, San Jose — City park"). Bill operational ask 2026-
        # 05-10: "include it in the staging as if it were a location name"
        # so both audiences are served by the same line.
        # #668: the rendering moved into _format_coordinate_staging_display so
        # the OCR-time Pass B coordinate path emits the identical string.
        primary_address = _format_coordinate_staging_display(
            anchor_lat, anchor_lng, utm_in
        )

    else:  # address mode
        # Apply the same hygiene as OCR-time staging geocoding: CA-append,
        # comma-city anchoring. city_context is None here because we don't
        # have one — but the CA-append rule alone prevents wrong-country
        # resolution for bare CA street names.
        normalized = _normalize_staging_geocode_query(address, None)
        # Defensive abbreviation rejection: bare 1-3 letter all-alpha tokens
        # like "SJ" or "LA" would match across the country. Same rule LKP +
        # Residence geocoding apply.
        bare_check = normalized.split(",")[0].strip()
        if len(bare_check) <= 3 and bare_check.replace(" ", "").isalpha():
            logger.info(
                "apply-staging-override | sub=%s outcome=abbreviation_rejected latency_ms=%d",
                user_sub, int((time.monotonic() - t0) * 1000),
            )
            raise HTTPException(status_code=422, detail="Address too short to geocode unambiguously")
        result = await _geocode_lkp_smart(normalized)
        if result is None:
            logger.info(
                "apply-staging-override | sub=%s outcome=no_match latency_ms=%d",
                user_sub, int((time.monotonic() - t0) * 1000),
            )
            raise HTTPException(status_code=422, detail="Address could not be geocoded")
        # City-consistency guard (#740) — the same one the LKP and Residence
        # legs run, and it must run BEFORE the anchor is unpacked, because
        # `resolved_locality` and the divergence distance below are both derived
        # from this coordinate. A warning computed from an uncorrected anchor is
        # measuring against the wrong reference (the #668 principle).
        #
        # Live 2026-08-09: a dispatcher typed the LKP's own address here and the
        # endpoint answered "resolved in <other city>, 4.3 mi from the LKP" —
        # correct detection, no remedy — while the LKP leg had already corrected
        # the IDENTICAL string via Google seconds earlier in the same request.
        # Retyping the street as two words resolved it, so the endpoint was
        # asking the dispatcher to find by trial and error the answer it could
        # fetch itself. A warning nobody can act on spends trust and returns
        # nothing.
        result, _ov_city_note, _ = await _reconcile_geocode_city(
            normalized, result, "Staging override address"
        )
        # Issue #607: the 4th element is the RESOLVED LOCALITY, and it used to
        # be discarded here. Showing it back is what makes a wrong anchor
        # visible. On 2026-07-24 the LKP resolved to the wrong city, so this
        # 300 m search was centred ~13 km from the real location and returned
        # nothing — and a bare empty list reads as "nothing near here" rather
        # than "we are looking in the wrong city." The dispatcher went to
        # Google Maps by hand, "questioning the value of the tool at this
        # point."
        anchor_lat, anchor_lng, _display, resolved_locality = result
        # Use the dispatcher's input as the display string — it's what they
        # typed and is the most operationally meaningful value to show back.
        # The geocoded display is internal verification only.
        primary_address = address.strip()

    # Run the staging source at the anchor (best-effort, non-fatal). Source-agnostic
    # dispatcher (Overpass/Geoapify per STAGING_SOURCE) with a symmetric shadow;
    # `mirrors_ok` now means "the active source returned" — the filter below is unchanged.
    nearby: list[dict] = []
    try:
        candidates, _sc, _cc, mirrors_ok = await _query_staging_pois(
            anchor_lat, anchor_lng, radius_m=_OVERRIDE_OVERPASS_RADIUS_M, log_compare=False
        )
        if mirrors_ok:
            for c in candidates[:_OVERRIDE_NEARBY_CAP * 2]:  # over-fetch; some get filtered
                if len(nearby) >= _OVERRIDE_NEARBY_CAP:
                    break
                # Overpass-returned `addr` shapes (from main.py:_query_overpass_staging):
                #   "1234 Main St, San Jose"            — full street + city
                #   "1234 Main St, San Jose, 95148"     — with postcode
                #   "San Jose"                          — city-only (parks w/ addr:city, no street)
                #   "San Jose, 95148"                   — city + postcode
                #   "(address not in OSM)"              — parks with no addr tags at all
                #
                # Filter: drop entries with no usable info. Picking a bare park
                # name (e.g. "Kelley Park") commits a staging line of just
                # "Staging Area for Resources: Kelley Park" — operationally
                # useless. The dispatcher's primary input is always usable;
                # we only offer nearby alternatives that bring their own
                # navigability. Live confirmed 2026-05-10 Verde Vista retest.
                addr = (c.get("addr") or "").strip()
                if not addr or addr == "(address not in OSM)":
                    continue
                # Strip postcode tokens — they add noise without value.
                # `_is_postcode_token` lives here as an inline lambda since
                # it's tiny and used only at this site.
                _is_postcode = lambda t: bool(re.match(r"^\d{5}(-\d{4})?$", t.strip()))
                addr_parts = [
                    p.strip()
                    for p in addr.split(",")
                    if p.strip() and not _is_postcode(p)
                ]
                if not addr_parts:
                    continue  # defensive: postcode-only or empty after filter
                addr_clean = ", ".join(addr_parts)
                # Display: "<Name>, <addr-clean>" — name first for picker
                # recognizability, addr after for operational navigability.
                # The committed staging line uses this exact string so
                # responders see "<Name>, <Street, City>" or "<Name>, <City>".
                display = f"{c['name']}, {addr_clean}"
                nearby.append({
                    "display": display,
                    "lat": c["lat"],
                    "lng": c["lng"],
                    "amenity": c.get("amenity", ""),
                })
    except Exception as exc:
        # Overpass failure is non-fatal — we still have the primary anchor.
        logger.warning(
            "apply-staging-override Overpass failed | sub=%s error=%s",
            user_sub, type(exc).__name__,
        )

    # Event name streetname suggestion (address mode only).
    event_name_suggestion: str | None = None
    if address is not None:
        event_name_suggestion = _extract_streetname_from_address(address)

    # Issue #606 — LKP/staging divergence. Warn only; nothing is moved and
    # nothing is blocked. See _OVERRIDE_LKP_DIVERGENCE_M for why re-anchoring
    # the LKP is the wrong answer.
    #
    # Bill's requirement (2026-07-25): the dispatcher must still be able to
    # pick another location after seeing this. That is why it is a field on a
    # normal 200 response rather than a 422 gate — the results list and every
    # input stay live, so the warning and the remedy are on screen together.
    # Nothing has been committed at this point: the override is not applied
    # until the dispatcher clicks a result, and THAT click is the acknowledgement.
    lkp_divergence: dict | None = None
    try:
        if isinstance(lkp_in, dict) and anchor_lat is not None and anchor_lng is not None:
            _lkp_lat = float(lkp_in.get("lat"))
            _lkp_lng = float(lkp_in.get("lng"))
            _dist_m = _haversine_m(_lkp_lat, _lkp_lng, anchor_lat, anchor_lng)
            if _dist_m > _OVERRIDE_LKP_DIVERGENCE_M:
                lkp_divergence = {
                    "distance_mi": round(_dist_m / 1609.344, 1),
                    "distance_km": round(_dist_m / 1000.0, 1),
                }
    except (TypeError, ValueError):
        # Malformed or missing lkp coords — can't check, so allow through
        # silently. A crash here would take out an override the dispatcher is
        # relying on mid-callout, to deliver a warning.
        lkp_divergence = None

    # Issue #607: a zero-result override is NOT a success. Both 2026-07-24
    # overrides logged outcome=success with nearby_count=0 while the anchor was
    # ~13 km from the real incident, so the one log line that could have
    # surfaced the problem asserted everything was fine.
    _outcome = "success" if nearby else "no_candidates"
    logger.info(
        "apply-staging-override | sub=%s outcome=%s latency_ms=%d nearby_count=%d mode=%s",
        user_sub,
        _outcome,
        int((time.monotonic() - t0) * 1000),
        len(nearby),
        "address" if address is not None else ("utm" if utm_in is not None else "lat_lng"),
    )
    # `resolved_locality` and `search_radius_m` go in the RESPONSE only, never
    # the log. The Locked Decision on this endpoint holds: the log carries
    # latency_ms, outcome, nearby_count, mode and nothing else — no address
    # text, no coords, no geocoded display string. The response travels to the
    # dispatcher's own browser, where the locality is the entire point: it is
    # what turns "nothing near here" into "we are searching the wrong city."
    return JSONResponse({
        "primary": {
            "address": primary_address,
            "lat": anchor_lat,
            "lng": anchor_lng,
            "label_verbatim": _DISPATCHER_OVERRIDE_LABEL,
        },
        "nearby": nearby,
        # DELIBERATELY NOT CONSUMED by the frontend since 2026-07-27 (Bill).
        # A staging override must not rewrite the Event Name: the Event Name
        # identifies the incident and derives from where the subject was last
        # seen, while staging is a logistics pick that can legitimately be far
        # away (a mutual-aid caravan point 30 km out). PR-D-1 coupled them;
        # picking "Richey Training Center" on a Mountain Home Drive incident
        # renamed the event after a building in another part of the county,
        # which then flows to D4H, the EB title, the Slack channel, and the
        # CalTopo map title.
        # Kept in the response (not removed) so the contract stays stable and a
        # future EXPLICIT "also update the Event Name" action has it available.
        # Do not re-wire it into the override commit path.
        "event_name_streetname_suggestion": event_name_suggestion,
        "outcome": _outcome,
        "resolved_locality": resolved_locality or "",
        # Disclosed, never silent: a rewrite the dispatcher cannot see is
        # the failure mode this area keeps producing. Empty for the
        # coordinate modes, which never geocode.
        "city_correction_note": _ov_city_note or "",
        "search_radius_m": _OVERRIDE_OVERPASS_RADIUS_M,
        # null when within range, when no LKP was supplied, or when LKP
        # geocoding failed — the dispatcher sees a warning only when there is
        # something real to warn about.
        "lkp_divergence": lkp_divergence,
    })


# ---------------------------------------------------------------------------
# Everbridge + Slack integration endpoints — Task 1.10a scaffolds
#
# Six new endpoints added behind the _feature_enabled() gate. Bodies are
# scaffolded with the auth dependency, the feature-flag gate, and a 501
# placeholder; orchestration logic lands in subsequent PRs:
#   - Task 1.10b: /send-notification 10-step orchestration
#   - Task 1.10c: /confirm-draft-sent manual-fallback flow (Phase 0 Task 9)
#   - Task 1.10d: /incident-status, /close-incident-polling,
#                 /everbridge-groups, /everbridge-contacts
#
# Feature-flag behavior:
#   - On SCCSSAR-dev (Phase 1-3 — flags unset)        → 503 with "feature not enabled"
#   - On personal-dev (flags set, body not yet written) → 501 "Not yet implemented"
#   - Both states carry no side effects — the gate is the FIRST statement in
#     each handler before any external call.
#
# Every handler also depends on require_authorized_dispatcher so an
# unauthenticated request gets a 401 from the auth layer regardless of
# feature-flag state, which is the correct ordering: auth before authz before
# capability gating.
# ---------------------------------------------------------------------------

_EB_SLACK_FEATURE_DISABLED_DETAIL = "Everbridge + Slack feature not enabled"
_EB_SLACK_NOT_IMPLEMENTED_DETAIL  = "Endpoint scaffolded; orchestration not yet implemented"

# Everbridge organization ID — this deployment's org, used in every EB API path.
# Previously hardcoded on the reasoning that it is "not a secret" (it appears in
# URLs). That is true and beside the point: it is org-specific intelligence about
# someone else's Everbridge estate, useless to any other EB customer, so it is
# configuration. Empty default so import never requires it; the lifespan logs a
# startup warning when unset, and the EB calls themselves fail visibly.
_EVERBRIDGE_ORG_ID = os.environ.get("EVERBRIDGE_ORG_ID", "").strip()

# Per Phase 0 Step 3: schedule unconditional template deletion at +30 min from
# creation. Templates are audit-safe (notification history is independent of
# the source template), so we delete regardless of whether the dispatcher sent.
_DELETE_TEMPLATE_DELAY_S = 30 * 60     # 30 minutes


def _eb_slack_gate_or_raise() -> None:
    """Single source of truth for the feature gate — keeps the message
    string and status code consistent across all 6 endpoints, and gives
    a single function for future-Claude to extend with finer-grained
    logging or an audit hook if needed."""
    if not _feature_enabled():
        raise HTTPException(
            status_code=503,
            detail=_EB_SLACK_FEATURE_DISABLED_DETAIL,
        )


def _eb_config_missing_detail(missing: list[str]) -> str:
    """Dispatcher-facing message for unset Everbridge configuration (#803).

    ONE wording for every endpoint that can hit this. The dispatch preflight
    and the two pickers fail for the same reason and must say the same thing;
    two phrasings for one cause is how a dispatcher learns to distrust both.
    Names the variable and names the fix, because the trigger is a deploy that
    landed before `terraform apply` (Session Rule #7) and the build scripts do
    not pick Terraform changes up.
    """
    return (
        "Everbridge is not configured on this deployment: "
        + ", ".join(missing)
        + ". Run `terraform apply` for this environment — build scripts "
          "do not pick up Terraform changes."
    )


def _require_everbridge_org_id(where: str) -> None:
    """400 before the outbound call when EVERBRIDGE_ORG_ID is unset (#803).

    Without it the URL builds as `/rest/contacts/?pageSize=1000` — no org
    segment — Everbridge 404s "no such request handling method", and the
    HTTPStatusError escapes as an unhandled 500. Observed live on personal-dev
    2026-09-04 during the deploy-before-apply negative test.

    400, not 5xx: unset configuration is not transient (rubric Q5). The pickers
    are read-only and have no point of no return, so unlike the dispatch
    preflight this guard protects the message, not a side effect.
    """
    if not _EVERBRIDGE_ORG_ID.strip():
        logger.error("%s config_missing=EVERBRIDGE_ORG_ID", where)
        raise HTTPException(
            status_code=400,
            detail=_eb_config_missing_detail(["EVERBRIDGE_ORG_ID"]),
        )


# ---------------------------------------------------------------------------
# Firestore client for the incidents collection. Lazy-initialized so module
# import doesn't require GCP credentials (matches the rate_limit.py pattern).
# ---------------------------------------------------------------------------

_eb_slack_firestore_db = None   # set on first call to _get_eb_slack_db()


def _get_eb_slack_db():
    """Lazy Firestore client for the incidents collection.

    Same pattern as rate_limit.py::_get_db() — defer the credential lookup
    until the first call so the module imports cleanly in environments that
    don't have GCP credentials configured (e.g. local pytest).
    """
    global _eb_slack_firestore_db
    if _eb_slack_firestore_db is None:
        from google.cloud import firestore
        _eb_slack_firestore_db = firestore.Client()
    return _eb_slack_firestore_db


def _patch_incident_doc_best_effort(event_id: str, patch: dict, *, where: str) -> None:
    """Cluster C: incremental Firestore .update() that never crashes dispatch.

    /send-notification persists each side-effect ID (everbridge_event_id,
    notification_id, slack_channel_id, welcome_ts, caltopo_ts, tally_ts)
    the moment it's obtained — NOT only at the final .set() — so a
    downstream failure doesn't lose every prior ID with no recovery path.

    Failures here log + continue. The diagnostic value is real but not
    worth aborting the dispatch over; the final .set() at the end of the
    handler re-writes the same values on the success path (idempotent),
    so this is a journal, not the primary write.

    The doc is guaranteed to exist by the time this is called: Cluster B's
    .create() at Step 1.5 of /send-notification runs first, so .update()
    has a target. NotFound here would be a violation of that invariant
    (e.g., concurrent deletion), so it's surfaced as a distinct
    warning (batch-3 G.LOW) rather than buried under the generic
    transient-error message.
    """
    try:
        _get_eb_slack_db().collection("incidents").document(event_id).update(patch)
    except _FirestoreNotFound as e:
        # Batch-3 G.LOW: distinguish skeleton-doc invariant violation
        # from transient network errors. NotFound at this point means
        # the Step 1.5 .create() doc was deleted between Step 1.5 and
        # this update — a concurrent-deletion that violates the
        # tombstone contract. Pre-fix this fell into the generic
        # "final .set() will reconcile" branch; the final .set() DOES
        # reconcile (it's an UPSERT) but the deletion event is itself
        # a signal worth investigating. See Melanie's 2026-05-30
        # batch-3 finding #7.
        logger.warning(
            "Firestore patch NotFound at %s (skeleton doc deleted between "
            ".create() at Step 1.5 and update — INVARIANT VIOLATION; "
            "final .set() will UPSERT but investigate the deletion event) "
            "— fields: %s",
            where, list(patch.keys()),
        )
    except Exception as e:
        logger.warning(
            "Firestore patch failed at %s (%s) — fields: %s; final .set() will reconcile",
            where, type(e).__name__, list(patch.keys()),
        )


async def _send_dm_and_persist(
    *,
    loop,
    slack_module,
    event_id: str,
    user_id: str,
    slack_channel_id: str,
    dm_text: str,
    where: str,
) -> bool:
    """Send the VIP-breakthrough responder DM + patch slack_dm_sent_user_ids.

    Returns True iff the DM was successfully sent (chat.postMessage returned
    without exception). False iff the send failed and the UC4 undeliverable
    path fired. Callers MUST capture the return value per Failure-mode
    Discipline Q3 — pinned in test_discarded_return_values.py MUST_CAPTURE.
    Send-time (Step 7b) uses the return value to decide whether to append
    to the Python-local dm_sent_user_ids list that survives the final .set().

    PRD: SAR Slack Onboarding/PRD_DispatchTurbo_Responder_DM_VIP_Breakthrough.

    Failure isolation (PRD FR#5): a DM failure for one user must never
    block channel creation or DMs to other users. Callers wrap invocations
    of this helper individually per user_id.

    UC4 (PRD): on chat.postMessage exception, post a notice to the
    INCIDENT channel (not #active-incidents per Bill 2026-07-10) plus an
    ERROR log so the dispatcher/IC sees the failure and can follow up out
    of band. UC4 notice uses <@USER_ID> so Slack renders the person's
    display name — no separate display_name plumbing needed.

    Firestore persistence uses ArrayUnion so concurrent poll cycles
    (Cloud Tasks retry racing with the original) atomically dedupe on the
    server side. The Slack chat.postMessage itself is NOT atomic across
    concurrent execution — an accepted TOCTOU in the same class as the
    D4H mark_member_attending case (Locked Design Decision; see also #442,
    declined 2026-07-19).
    Recovery cost is small (recipient sees two identical DMs); operation
    is short (~1-2s); Cloud Tasks retry backoff puts retries well after
    a normal DM completes.

    Cluster C pattern: patch Firestore the moment we have the outcome, not
    at the final .set(). See CLAUDE.md "Incremental Firestore persistence
    in /send-notification" Locked Design Decision.
    """
    try:
        await loop.run_in_executor(
            None,
            functools.partial(slack_module.send_incident_dm, user_id, dm_text),
        )
    except Exception as e:
        logger.error(
            "%s: send_incident_dm failed (%s) for user_id=%s — posting UC4 "
            "undeliverable notice to incident channel",
            where, type(e).__name__, user_id,
        )
        try:
            await loop.run_in_executor(
                None,
                functools.partial(
                    slack_module.post_message,
                    slack_channel_id,
                    f"Could not DM <@{user_id}> — VIP breakthrough will not "
                    f"fire for them; contact by other means.",
                ),
            )
        except Exception as post_e:
            logger.warning(
                "%s: UC4 post_message ALSO failed (%s) — DM failure only "
                "visible in logs for user_id=%s",
                where, type(post_e).__name__, user_id,
            )
        return False
    from google.cloud import firestore
    _patch_incident_doc_best_effort(
        event_id,
        {"slack_dm_sent_user_ids": firestore.ArrayUnion([user_id])},
        where=where,
    )
    return True


# ---------------------------------------------------------------------------
# Pure-logic orchestration helpers (mirror-tested in test_send_notification.py)
# ---------------------------------------------------------------------------

def _decide_collision(
    *,
    this_event_id: str,
    existing_channel_owner_event_id: str | None,
) -> str:
    """Decide whether to reuse an existing bare-name channel or create the
    collision-suffixed channel for a different incident.

    Returns 'reuse' or 'collide'.

    Cases:
      - existing_channel_owner_event_id is None → channel exists in Slack but
        no Firestore incident references it. Treat as reuse — most likely a
        manually-created channel (operator pre-staged it) or a leftover from
        a deleted incident doc. Failing the send because of an orphaned
        Slack channel would block the dispatcher unnecessarily.
      - owner == this_event_id → same incident retrying after a transient
        partial-failure (e.g. Firestore write succeeded but the response
        timed out and the dispatcher hit Send again). Reuse.
      - owner != this_event_id → real same-day same-street collision.
        Use the collision-suffixed channel name to keep the Slack channel
        1:1 with the Everbridge event.
    """
    if existing_channel_owner_event_id is None:
        return "reuse"
    if existing_channel_owner_event_id == this_event_id:
        return "reuse"
    return "collide"


def _compose_active_incidents_tally(doc: dict, header: str) -> str:
    """Render the live tally for #active-incidents.

    Format from PoC + design Section 5 "Live tally message", with the
    tri-count line added in issue #592:
        *<header> — <event_name_human>*
        *✅ <yes> confirmed   ❌ <declined> declined   ⏳ <no_response> no response*
        • K9 (3): Burns, Black, Cubeiro
        • UAS (2): Lee, Romard
        • Drivers (1): Burns
          ↳ Burns: K9, Drivers       ← multi-team callout (Item 6 — small ↳
                                         indent rather than larger 🔸 emoji)

    `header` is one of:
        "🔔 Everbridge ACTIVE"
        "📋 Awaiting dispatcher send"
        "⏹ Everbridge STOPPED — <reason>"

    Phase 0 Task 8 finding (the YES-count regression at natural expiry):
    `responders` may be transiently empty during the natural-expiry state
    transition. The poll handler caches the last non-empty list in
    `last_non_empty_responders`; this composer prefers that field, falling
    back to `responders` for the initial-post case where neither field is
    populated yet.
    """
    import slack as slack_module     # avoids top-level slack import in this file

    responders = doc.get("last_non_empty_responders") or doc.get("responders") or []
    by_group: dict[str, list[str]] = {}
    member_groups: dict[str, list[str]] = {}
    for r in responders:
        name = r["name"]
        for group in r.get("groups", []) or []:
            by_group.setdefault(group, []).append(name)
            member_groups.setdefault(name, []).append(group)

    # Tri-count line (issue #592): confirmed / declined / no-response. The
    # explicit-NO count is the go/no-go signal for mutual-aid callouts (can we
    # field a team to travel?). decline_count / no_response_count are persisted
    # by the /poll-incident handler from everbridge._parse_poll_response; both
    # default to 0 for the initial send-time post and old pre-#592 Firestore
    # docs. "confirmed" continues to mean the YES responders (len(responders)),
    # NOT EB's confirmedCount rollup — the latter conflates Yes and No.
    yes_count = len(responders)
    decline_count = doc.get("decline_count", 0)
    no_response_count = doc.get("no_response_count", 0)
    lines = [
        f"*{header} — {doc['event_name_human']}*",
        f"*✅ {yes_count} confirmed   ❌ {decline_count} declined   "
        f"⏳ {no_response_count} no response*",
    ]
    group_names = doc.get("requested_group_names") or []
    if group_names:
        lines.append(slack_module.format_groups_requested(group_names))
    for group_name, names in sorted(by_group.items()):
        lines.append(slack_module.format_tally_responder_line(group_name, sorted(names)))

    multi_team = sorted(
        (name, sorted(groups))
        for name, groups in member_groups.items()
        if len(groups) > 1
    )
    for name, groups in multi_team:
        lines.append(slack_module.format_tally_multi_team_line(name, groups))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /confirm-draft-sent validation (mirror-tested)
# ---------------------------------------------------------------------------

def _validate_confirm_draft_sent(
    doc: dict | None,
    user_email: str,
) -> tuple[int, str] | None:
    """Validate a /confirm-draft-sent request before any side-effects.

    Returns None when the request is valid. Returns (status_code, detail)
    when invalid; caller raises HTTPException(*result).

    Defense-in-depth — multiple failure modes get distinct status codes
    so an operator reading logs can tell them apart:

        404 — incident doc not found OR doc.dispatcher_email != user_email
              (combined into one detail to avoid leaking ownership info to
              an authorized-but-not-owning dispatcher: same response in
              both cases means "not yours" without disclosing whether the
              event_id even exists)

        409 — notification_id already set on the doc — nothing to confirm.
              Returning 409 (Conflict) rather than 200-no-op so the
              dispatcher's UI can show "already confirmed by you/another
              session" rather than silently swallowing.

        409 — incident is in a terminal state (status not in
              {polling, pre_discovery}). Manual-confirm only makes sense
              while the polling chain is still hunting for the notification;
              applying it to a stopped incident would resurrect a dead
              chain in confusing ways.
    """
    if not doc or doc.get("dispatcher_email") != user_email:
        return (404, "Incident not found or not yours")
    if doc.get("notification_id"):
        return (
            409,
            "Incident already has a notification_id; nothing to confirm",
        )
    status = doc.get("status")
    if status not in ("polling", "pre_discovery"):
        return (
            409,
            f"Incident is in terminal state {status!r}",
        )
    return None


# ---------------------------------------------------------------------------
# Read-side validation + serialization (used by /incident-status,
# /close-incident-polling — Task 1.10d). Mirror-tested.
# ---------------------------------------------------------------------------

def _validate_incident_ownership(
    doc: dict | None,
    user_email: str,
) -> tuple[int, str] | None:
    """Validate that the requester owns the incident doc.

    Returns None when ownership is valid. Returns (404, detail) otherwise.

    Same defense-in-depth pattern as _validate_confirm_draft_sent: missing
    doc and wrong-owner produce IDENTICAL responses so an authorized
    dispatcher querying someone else's event_id can't infer whether the
    event_id exists.
    """
    if not doc or doc.get("dispatcher_email") != user_email:
        return (404, "Incident not found or not yours")
    return None


def _validate_close_polling(
    doc: dict | None,
    user_email: str,
) -> tuple[int, str] | None:
    """Validate a /close-incident-polling request before any write.

    Composes _validate_incident_ownership with a terminal-state check.
    Closing an already-stopped incident is operationally pointless (the
    polling chain isn't running), so we return 409 rather than no-op so
    the dispatcher's UI can surface "already stopped" rather than silently
    accepting.
    """
    err = _validate_incident_ownership(doc, user_email)
    if err:
        return err
    status = doc.get("status", "") if doc else ""
    if isinstance(status, str) and status.startswith("stopped_"):
        return (409, f"Incident already in terminal state {status!r}")
    return None


def _serialize_incident_doc_for_api(value):
    """Recursively convert non-JSON-native types to JSON-safe equivalents.

    Firestore returns DatetimeWithNanoseconds (a datetime.datetime
    subclass) for timestamp fields like `created_at`. JSONResponse can't
    serialize datetime directly, so we walk the doc and convert any
    datetime to ISO 8601 string. Dicts and lists are recursed into.
    Other types pass through unchanged.

    Pure-logic — mirror-tested.
    """
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _serialize_incident_doc_for_api(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_incident_doc_for_api(v) for v in value]
    return value


def _manual_confirm_offered(
    doc: dict | None,
    now: datetime.datetime | None = None,
) -> bool:
    """True iff the dispatch console should render the "Did you press Send?" banner.

    The banner is the recovery affordance for safe-mode draft sends where the
    dispatcher pressed Send in the Everbridge UI but skipped the
    "Include as part of an event" + event-picker steps that would have linked
    the notification to our Firestore doc. When that happens, auto-discovery
    can't find the notification and the polling chain idles in pre-discovery
    until the 30-min hard timeout fires.

    Trigger conditions (per design Section 4 "Manual-confirm fallback"):
      1. status is `polling` or `pre_discovery` (the chain is still hunting).
         Both accepted to mirror _validate_confirm_draft_sent's tolerance —
         over-showing a UX banner is much safer than missing it.
      2. notification_id is None — auto-discovery hasn't found one yet
      3. template_id is not None — this is a safe-mode draft path
         (live-send path always has notification_id from send time, never
         needs the banner)
      4. ≥ MANUAL_CONFIRM_OFFER_DELAY_S elapsed since created_at — gives the
         happy path (typical discovery latency < 5s) plenty of room before
         offering recovery UI

    `now` parameter exists so mirror tests can pin time deterministically.

    Pure-logic — mirror-tested. Mirror in test_incident_endpoints.py.
    """
    if not doc:
        return False
    if doc.get("status") not in ("polling", "pre_discovery"):
        return False
    if doc.get("notification_id") is not None:
        return False
    if doc.get("template_id") is None:
        return False
    created_at = doc.get("created_at")
    if not isinstance(created_at, datetime.datetime):
        return False
    if now is None:
        from incidents import now_utc
        now = now_utc()
    from incidents import MANUAL_CONFIRM_OFFER_DELAY_S
    elapsed_s = (now - created_at).total_seconds()
    return elapsed_s >= MANUAL_CONFIRM_OFFER_DELAY_S


# ---------------------------------------------------------------------------
# OIDC validation for Cloud Tasks-driven endpoints (Task 1.11).
#
# /poll-incident and /delete-template are invoked by Cloud Tasks (NOT the
# dispatcher's browser), so they don't use require_authorized_dispatcher.
# Cloud Tasks signs each request with an OIDC token whose:
#   - issuer = "https://accounts.google.com" (Google's metadata server)
#   - email  = the everbridge-poll-sa service account
#   - aud    = the Cloud Run service URL
#
# The validator is split into two functions for testability:
#   - _check_oidc_claims(claims, expected_email, expected_audience) — pure
#     logic, mirror-tested
#   - _verify_oidc_request(request, expected_email, expected_audience) —
#     wraps google.oauth2.id_token.verify_oauth2_token + the pure check;
#     tested at live-test time
# ---------------------------------------------------------------------------

_GOOGLE_OIDC_ISSUERS = ("https://accounts.google.com", "accounts.google.com")


def _check_oidc_claims(
    claims: dict | None,
    expected_email: str,
    expected_audience: str,
) -> tuple[int, str] | None:
    """Validate OIDC token claims AFTER they've been decoded + signature-verified.

    Returns None when the token is from the expected service account and
    targets this service. Returns (status_code, detail) otherwise.

    All failures map to 401 (Unauthorized) — we don't distinguish between
    "wrong issuer" / "wrong email" / "wrong audience" in the response so
    an attacker probing the endpoint can't tell which check tripped.
    Cloud Run logs receive the specific reason for operator visibility.

    Defense-in-depth: signature verification AND issuer check happen
    upstream in the SDK call (verify_oauth2_token enforces issuer +
    signature). This helper is the policy check on top of that.

    Fail-closed when caller config is missing: an empty expected_email
    (PROJECT_ID env unset) or expected_audience (CLOUD_RUN_SERVICE_URL
    env unset) means the deployment is misconfigured. Refuse the
    request without consulting attacker-controlled token fields, rather
    than relying on string-equality coincidences. Mirrored in
    test_poll_incident.py.
    """
    if not expected_email or not expected_audience:
        return (401, "Unauthorized")
    if not claims:
        return (401, "Unauthorized")
    iss = claims.get("iss", "")
    if iss not in _GOOGLE_OIDC_ISSUERS:
        return (401, "Unauthorized")
    if not claims.get("email_verified"):
        return (401, "Unauthorized")
    if claims.get("email") != expected_email:
        return (401, "Unauthorized")
    if claims.get("aud") != expected_audience:
        return (401, "Unauthorized")
    return None


def _verify_oidc_request(
    request: Request,
    *,
    expected_email: str,
    expected_audience: str,
) -> None:
    """Decode + verify the OIDC token in the Authorization header.

    Raises HTTPException(401) on any failure. This is a thin wrapper
    around google.oauth2.id_token.verify_oauth2_token + _check_oidc_claims
    — the pure-logic claim check is tested separately; this function is
    covered at live-test time.
    """
    # Fail-closed on missing config BEFORE calling verify_oauth2_token —
    # passing audience="" to the SDK is library-version-dependent (some
    # historical versions skipped the audience check on falsy values).
    # Emits an operator-visible warning distinct from the per-request
    # claim-mismatch log so a misconfigured deployment is diagnosable.
    if not expected_email or not expected_audience:
        logger.warning(
            "OIDC config missing: expected_email_set=%s expected_audience_set=%s "
            "— check PROJECT_ID and CLOUD_RUN_SERVICE_URL env vars",
            bool(expected_email), bool(expected_audience),
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    auth_header = request.headers.get("authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = auth_header.split(" ", 1)[1].strip()

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token
        claims = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=expected_audience,
        )
    except Exception as e:
        logger.warning("OIDC verify failed: %s", type(e).__name__)
        raise HTTPException(status_code=401, detail="Unauthorized")

    err = _check_oidc_claims(
        claims, expected_email=expected_email, expected_audience=expected_audience,
    )
    if err:
        logger.warning(
            "OIDC claim check failed: claims_email=%s expected=%s claims_aud=%s expected=%s",
            claims.get("email"), expected_email,
            claims.get("aud"), expected_audience,
        )
        raise HTTPException(status_code=err[0], detail=err[1])


def _expected_oidc_audience() -> str:
    """The OIDC audience this service expects on Cloud Tasks-driven requests.

    Cloud Tasks signs tokens with audience=the URL of the Cloud Run
    service (NOT the per-endpoint URL). We read CLOUD_RUN_SERVICE_URL
    from the env (set in dev/main.tf via Terraform).
    """
    return os.environ.get("CLOUD_RUN_SERVICE_URL", "")


def _expected_poll_sa_email() -> str:
    """Email of the everbridge-poll-sa service account (the OIDC subject)."""
    project_id = os.environ.get("PROJECT_ID", "")
    if not project_id:
        return ""
    return f"everbridge-poll-sa@{project_id}.iam.gserviceaccount.com"


# ---------------------------------------------------------------------------
# Cloud Tasks enqueue helpers (Task 1.11 — replaces the Task 1.10b stubs).
#
# Both helpers share a single _enqueue_http_task() inner that builds the
# CreateTask request with HTTP target + OIDC. The two callers differ only
# in URL and delay.
#
# The queue (everbridge-poll), location (us-central1), and OIDC SA were
# provisioned by Task 1.3 Terraform. This module reads them from env vars
# (PROJECT_ID, CLOUD_RUN_SERVICE_URL) so the same code targets personal-dev
# now and SCCSSAR-dev once Phase 5 mirrors the infrastructure.
# ---------------------------------------------------------------------------

# Must match terraform/environments/dev/main.tf::google_cloud_tasks_queue.everbridge_poll.name
# (and its eventual SCCSSAR-dev mirror in Phase 5 Step 5.1). Pinned by
# test_main_regression.py::TestEverbridgePollQueueName so any drift between
# the Terraform-provisioned queue name and this constant is caught at
# pre-flight rather than at live-test time.
_EB_POLL_QUEUE    = "everbridge-poll-queue"
_EB_POLL_LOCATION = "us-central1"


def _enqueue_http_task(
    *,
    relative_path: str,
    delay_s: int,
    audit_label: str,
) -> None:
    """Common Cloud Tasks enqueue helper.

    relative_path is appended to CLOUD_RUN_SERVICE_URL (e.g.
    "/poll-incident/{event_id}" or "/delete-template/{template_id}").
    delay_s is the schedule offset in seconds.
    audit_label appears in the structured log line for ops grep.
    """
    project_id = os.environ.get("PROJECT_ID", "")
    service_url = os.environ.get("CLOUD_RUN_SERVICE_URL", "")
    sa_email = _expected_poll_sa_email()

    if not project_id or not service_url or not sa_email:
        logger.warning(
            "Cloud Tasks not configured (PROJECT_ID/CLOUD_RUN_SERVICE_URL/SA missing) — "
            "%s SKIPPED: relative_path=%s delay_s=%d",
            audit_label, relative_path, delay_s,
        )
        return

    from google.cloud import tasks_v2
    from google.protobuf import timestamp_pb2
    import time as time_module

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(project_id, _EB_POLL_LOCATION, _EB_POLL_QUEUE)
    task: dict = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{service_url}{relative_path}",
            "oidc_token": {
                "service_account_email": sa_email,
                "audience": service_url,
            },
        },
    }
    if delay_s > 0:
        ts = timestamp_pb2.Timestamp()
        ts.FromSeconds(int(time_module.time()) + delay_s)
        task["schedule_time"] = ts

    client.create_task(request={"parent": parent, "task": task})
    logger.info(
        "%s ENQUEUED: relative_path=%s delay_s=%d",
        audit_label, relative_path, delay_s,
    )


def _enqueue_poll_task(event_id: str, *, delay_s: int, mode: str) -> None:
    """Schedule the next /poll-incident cycle for the given incident.

    `mode` ('post_discovery' | 'pre_discovery') is logged for observability
    only — the polling endpoint reads the actual mode from the incident
    doc's notification_id/template_id fields, which is the authoritative
    source of truth (a stale enqueue with the wrong mode label still
    behaves correctly because the endpoint re-derives).
    """
    _enqueue_http_task(
        relative_path=f"/poll-incident/{event_id}",
        delay_s=delay_s,
        audit_label=f"poll_task[{mode}]",
    )


def _enqueue_template_delete_task(template_id: str, *, delay_s: int) -> None:
    """Schedule unconditional template deletion at +30 min from creation.

    Templates are audit-safe per Phase 0; this fires regardless of whether
    the dispatcher actually pressed Send in the EB UI.
    """
    _enqueue_http_task(
        relative_path=f"/delete-template/{template_id}",
        delay_s=delay_s,
        audit_label="template_delete_task",
    )


# ---------------------------------------------------------------------------
# Slack channel create-or-collide
# ---------------------------------------------------------------------------

def _create_or_collide_channel(
    *,
    event_id: str,
    event_name_with_hhmm: str,
) -> tuple[str, str]:
    """Return (channel_id, channel_name_used).

    Two-step protocol:
      1. Try the bare-name channel via slack.find_or_create_private_channel.
         Returns (channel_id, bare_name) — either the brand-new channel or
         the existing channel with that name (Slack's name_taken handling).
      2. If existing: query Firestore to see who owns it. _decide_collision()
         returns 'reuse' or 'collide'. On 'collide', call
         slack.create_collision_channel() to make the suffixed channel
         instead.
    """
    import slack as slack_module

    bare_id, bare_name = slack_module.find_or_create_private_channel(
        event_id=event_id,
        event_name_with_hhmm=event_name_with_hhmm,
    )
    # Was this our own brand-new channel? Quick check via Firestore — if no
    # incident doc references this channel name, treat as reuse (safe).
    db = _get_eb_slack_db()
    existing = (
        db.collection("incidents")
        .where("slack_channel_name", "==", bare_name)
        .limit(2)
        .get()
    )
    other_owner_event_id: str | None = None
    for d in existing:
        owner = d.to_dict().get("event_id")
        if owner and owner != event_id:
            other_owner_event_id = owner
            break

    decision = _decide_collision(
        this_event_id=event_id,
        existing_channel_owner_event_id=other_owner_event_id,
    )
    if decision == "reuse":
        return bare_id, bare_name
    # Collision — create the suffixed channel instead.
    return slack_module.create_collision_channel(event_name_with_hhmm)


@app.get("/everbridge-groups")
async def everbridge_groups(
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """List Everbridge groups for the dispatcher selection UI.

    Body: filtered (SUPPRESSED_GROUP_IDS removed) and normalized
    (SAR- prefix stripped). Returns a JSON array of {id, name}.
    """
    _eb_slack_gate_or_raise()
    _require_everbridge_org_id("everbridge_groups")
    await check_rate_limits(dispatcher.get("email", ""))
    import everbridge as eb_module
    loop = asyncio.get_running_loop()
    groups = await loop.run_in_executor(
        None,
        functools.partial(eb_module.list_groups, _EVERBRIDGE_ORG_ID),
    )
    return JSONResponse(groups)


@app.get("/everbridge-contacts")
async def everbridge_contacts(
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """List Everbridge contacts + the caller's OCEAN# (issue #363).

    Response shape:
        {
            "contacts": [{"contact_id": str, "display_name": str}, ...],
            "dispatcher_ocean": "305" | null
        }

    PII boundary (design Section 1): email and phone are NEVER surfaced over
    the wire for ANY contact. The caller's OCEAN# (3-digit radio call sign,
    encoded by SCCSSAR in the EB `externalId` field as e.g. "1O305") is
    resolved server-side by matching the caller's auth-token email against
    the contact `paths` field; only the parsed 3-digit tail is returned. If
    the caller has no matching EB contact or the externalId doesn't yield
    3 digits, ``dispatcher_ocean`` is null and the frontend falls back to
    the existing "##" placeholder (or a bare last name in summary blocks).
    """
    _eb_slack_gate_or_raise()
    _require_everbridge_org_id("everbridge_contacts")
    await check_rate_limits(dispatcher.get("email", ""))
    import everbridge as eb_module
    loop = asyncio.get_running_loop()
    contacts, dispatcher_ocean = await loop.run_in_executor(
        None,
        functools.partial(
            eb_module.list_contacts_with_dispatcher,
            _EVERBRIDGE_ORG_ID,
            dispatcher.get("email", ""),
        ),
    )
    return JSONResponse({"contacts": contacts, "dispatcher_ocean": dispatcher_ocean})


# ---------------------------------------------------------------------------
# D4H Phase 2 PR 5 Task 5.2 — _build_ocr_data_for_d4h()
# ---------------------------------------------------------------------------
# Re-parses the dispatcher-edited textarea + structured /ocr map_data into the
# ocr_data dict that d4h.create_incident_with_subject expects. The textarea is
# the source of truth — dispatcher edits land here, so they flow into D4H.
#
# Pure-logic (regex parsing only — no I/O). Mirrored test:
# backend/test_send_notification.py::TestBuildOcrDataForD4H.
# When updating either, update both — the mirror IS the contract.

_D4H_RE_LKP_FULL    = re.compile(r"^Last Known Position:\s*(.+)$", re.MULTILINE)
# DOB line: capture group 1 = date portion (stops before any `(`), group 2 =
# first digit-sequence inside an optional trailing parenthetical. Defensively
# accepts ANY parenthetical content (years old, yo, y/o, yrs, ...) so future
# format drift in upstream OCR / synthetic-summary output doesn't silently
# drop age. Surfaced by a real dispatch — pre-fix the regex required
# the literal "years old" form, but pdf_extract.py was emitting "(N yo)",
# so neither group matched and DOB + age were both dropped.
_D4H_RE_DOB         = re.compile(r"^DOB:\s*([^(\n]+?)\s*(?:\((\d+)\s*[^)]*\))?\s*$", re.MULTILINE)
_D4H_RE_MP_NAME     = re.compile(r"^Missing Person:\s*([^;\n]+?)(?:\s*;|\s*$)", re.MULTILINE)
_D4H_RE_AT_RISK     = re.compile(r"^Missing Person:.*?;\s*at-risk:\s*([^\n]+)$", re.MULTILINE)
_D4H_RE_CONTACT     = re.compile(r"^Contact:\s*(.+)$", re.MULTILINE)
# The requesting agency's own incident number ("26-212-071", "2026-LAW-54902292").
# Both intake paths already put it on this line — pdf_extract.py from the
# AcroForm `event_number` field, gemini.py from the form's `Event #:` box — but
# nothing downstream ever read it, so D4H's trackingNumber (which IS the
# agency-reference field) got the event NAME by default. Issue #676.
_D4H_RE_EVENT_NUM   = re.compile(r"^Event #:\s*(.+)$", re.MULTILINE)
# Both intake paths emit a bracketed placeholder when the field is blank
# ("[not recorded]" from pdf_extract.py; Gemini can echo its own "[from form]"
# prompt token). A real agency number never carries brackets, so treat any
# fully-bracketed value as absent rather than enumerating every placeholder
# string either path might produce.
_D4H_RE_EVENT_NUM_PLACEHOLDER = re.compile(
    r"^(?:\[.*\]|not\s+recorded|not\s+provided|unknown|n/?a)$",
    re.IGNORECASE,
)
_D4H_RE_LPB_LINE    = re.compile(r"^Q(\d{1,2})\s*-\s*([^\-\n]+?)\s*-\s*([^\n]+)$", re.MULTILINE)
_D4H_RE_KOESTER     = re.compile(
    r"^LPB Range Ring Analysis[^\n]*:\n+(.+?)(?=\n---\n|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Two-summary-layout markers (CLAUDE.md Locked Decision "Two-summary layout").
# The textarea contains a `━━━ WHATSAPP DISPATCH ━━━` block (mobile-copy
# convenience that duplicates the Full block) followed by a `━━━ FULL INCIDENT
# SUMMARY ━━━` block. For D4H we keep only the Full block — see
# _extract_iis_body_for_d4h below.
_D4H_RE_FULL_SECTION_OPEN  = re.compile(r"━━+\s*FULL\s+INCIDENT\s+SUMMARY\s*━━+\s*\n", re.IGNORECASE)
_D4H_RE_CLOSING_DIVIDER    = re.compile(r"\n━━+\s*$")
_D4H_RE_INITIAL_SUMMARY_H  = re.compile(r"^Initial Incident Summary:\s*\n")


# Matches the FULL INCIDENT SUMMARY block's `Event Log:` subsection, capturing
# (1) the header + existing entries and (2) the closing `---` divider. Used to
# inject dispatch-time milestones BEFORE the closing divider — see
# _inject_dispatch_milestones_into_event_log below. Non-greedy entry-line
# group `(?:.+\n)*?` ensures we stop at the FIRST `---` after `Event Log:`.
_D4H_RE_EVENT_LOG_BLOCK = re.compile(
    r"(^Event Log:\s*\n(?:.+\n)*?)(^---\s*$)",
    re.MULTILINE
)


def _inject_dispatch_milestones_into_event_log(text: str, milestones: list[str]) -> str:
    """Insert dispatch-time milestone lines just before the closing `---`
    of the Event Log section in the IIS body.

    Used to feed D4H's incident description the complete event timeline.
    D4H's POST happens BEFORE the frontend can append these milestones to
    the dispatcher's textarea (the textarea is only updated AFTER
    /send-notification returns), so without this helper D4H's description
    misses 3 entries: Everbridge notification, Slack channel, D4H request.

    Each milestone string MUST already include its timestamp prefix
    ("YYYY-MM-DD HH:MM - text"). Pure-logic — no I/O.

    Fallback: if the Event Log section isn't found (e.g., textarea is empty
    or doesn't carry the standard two-summary structure), returns `text`
    unchanged. Caller treats this as best-effort, no-op. Mutual-aid forms
    that skip the two-summary layout pass through cleanly.

    Test mirror: backend/test_send_notification.py. Pinned per the
    project's mirror-pattern (production helper + test mirror in lockstep).
    """
    if not text or not milestones:
        return text
    addition = "\n".join(milestones) + "\n"
    def _sub(m):
        return f"{m.group(1)}{addition}{m.group(2)}"
    return _D4H_RE_EVENT_LOG_BLOCK.sub(_sub, text, count=1)


def _extract_iis_body_for_d4h(text: str) -> str:
    """Extract the canonical IIS body for the D4H description field.

    Drops the `━━━ WHATSAPP DISPATCH ━━━` block (mobile-copy convenience
    that duplicates the Full block), the `━━━ FULL INCIDENT SUMMARY ━━━`
    section markers, and the leading `Initial Incident Summary:` title line
    so the body starts directly with `Event Name:`. Per dispatcher design
    2026-05-20 — D4H description gets a TODO block followed by a blank line
    followed by the canonical summary, so the dispatcher can select-and-
    delete the TODO and have the field reduce to our standard format.

    Fallback for missing marker: returns the text trimmed-as-is (older
    textarea formats without the two-summary layout still flow through).

    Pure-logic — no I/O. Mirror in test_send_notification.py kept in
    lockstep with this definition.
    """
    if not text:
        return ""
    m = _D4H_RE_FULL_SECTION_OPEN.search(text)
    if not m:
        return text.strip()
    body = text[m.end():].rstrip()
    body = _D4H_RE_CLOSING_DIVIDER.sub("", body)
    body = _D4H_RE_INITIAL_SUMMARY_H.sub("", body, count=1)
    return body.strip()


def _split_officer_contact(contact_line: str) -> tuple[str, str]:
    """Split 'John Smith 408-555-1234' into ('John Smith', '408-555-1234').
    Heuristic: phone is the trailing whitespace-delimited token that contains
    a digit; everything before is the name. Returns ('', '') for empty input.
    """
    contact_line = (contact_line or "").strip()
    if not contact_line:
        return "", ""
    m = re.match(r"^(.+?)\s+([\d\-\(\)\.\s]+\d[\d\-\(\)\.\s]*)$", contact_line)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return contact_line, ""


def _sanitize_d4h_error(exc: Exception) -> str:
    """Sanitize a D4H error for surfaces that may reach the dispatcher
    (event log, /dispatch-status response, Cloud Logging).

    Delegates to ``d4h.exception_summary_no_body`` so the ``" | body: ..."``
    suffix that D4HClientError / D4HServerError carry for callers is
    stripped before any rendering. The body fragment can echo back member
    emails or names from the failing request — see d4h.py for context.

    Returns ``"<ExceptionClass>: <msg without body, truncated to 200 chars>"``.
    """
    summary = d4h.exception_summary_no_body(exc)
    # exception_summary_no_body already strips newlines + applies the classname
    # prefix; cap at 200 chars for the dispatcher surface.
    return summary[:200]


def _build_ocr_data_for_d4h(*, ocr_text: str, map_data: dict,
                            event_name_human: str = "",
                            fallback_mp_at_risk: str = "",
                            fallback_mp_full_name: str = "") -> dict:
    """Re-parse the dispatcher-edited textarea + structured map_data into the
    ocr_data dict d4h.create_incident_with_subject expects.

    The textarea is the source of truth — dispatcher edits land here.
    map_data supplies geocoded coords that aren't reliably extractable from
    textarea prose.

    event_name_human is the live textarea `Event Name:` value (issue #87).
    map_data["event_name"] is frozen at OCR time, so reading it here meant a
    dispatcher edit reached CalTopo (which re-parses the textarea into
    freshMapData) but NOT D4H — the referenceDescription kept the pre-edit
    name. First live occurrence: 2026-07-24 callout. The caller already
    validates event_name_human as non-empty (400 otherwise) and uses it for
    the EB title, the Slack channel name and the Firestore doc, so passing it
    here makes D4H read the same single source as every other surface. The
    map_data fallback is retained only for direct callers that omit it.

    Comma-splits the at-risk segment so each indicator becomes its own bullet
    in the D4H INVOLVED tab. fallback_mp_at_risk / fallback_mp_full_name are
    used only when the textarea parse yields nothing (extra safety — the
    /send-notification payload also carries flat mp_at_risk and mp_name).

    Safe defaults on missing/empty input — never raises. Downstream
    d4h.create_incident_with_subject may itself fail on missing required
    keys (e.g., empty event_name or mp_full_name), which is caught at the
    call site.
    """
    ocr_text = ocr_text or ""
    map_data = map_data or {}
    lkp = map_data.get("lkp") or {}

    # MP full name — required by D4H involved-person POST (spike 06:159).
    mp_full_name = ""
    nm = _D4H_RE_MP_NAME.search(ocr_text)
    if nm:
        mp_full_name = nm.group(1).strip()
    if not mp_full_name:
        mp_full_name = (fallback_mp_full_name or "").strip()

    lkp_address = ""
    m = _D4H_RE_LKP_FULL.search(ocr_text)
    if m:
        lkp_address = m.group(1).strip()

    event_name = (event_name_human or map_data.get("event_name") or "").strip()

    try:
        lkp_lat = float(lkp.get("lat", 0.0) or 0.0)
    except (TypeError, ValueError):
        lkp_lat = 0.0
    try:
        lkp_lng = float(lkp.get("lng", 0.0) or 0.0)
    except (TypeError, ValueError):
        lkp_lng = 0.0

    mp_dob = ""
    mp_age: object = None
    dm = _D4H_RE_DOB.search(ocr_text)
    if dm:
        mp_dob = (dm.group(1) or "").strip()
        if dm.group(2):
            try:
                mp_age = int(dm.group(2))
            except (TypeError, ValueError):
                mp_age = None

    raw_at_risk = ""
    am = _D4H_RE_AT_RISK.search(ocr_text)
    if am:
        raw_at_risk = am.group(1).strip()
    if not raw_at_risk:
        raw_at_risk = (fallback_mp_at_risk or "").strip()
    at_risk_indicators = [x.strip() for x in raw_at_risk.split(",") if x.strip()]

    officer_name = ""
    officer_phone = ""
    cm = _D4H_RE_CONTACT.search(ocr_text)
    if cm:
        officer_name, officer_phone = _split_officer_contact(cm.group(1))

    qn: dict[str, str] = {}
    q1 = ""
    q9 = ""
    for line_m in _D4H_RE_LPB_LINE.finditer(ocr_text):
        n = line_m.group(1)
        answer = line_m.group(2).strip()
        question = line_m.group(3).strip()
        qn[f"q{n}_question"] = question
        qn[f"q{n}_answer"] = answer
        if n == "1":
            q1 = answer.split(" ")[0]
        if n == "9":
            q9 = answer.split(" ")[0]
    qn["q1_familiar_with_area"] = q1
    qn["q9_intentional_self_harm"] = q9

    koester_narrative = ""
    km = _D4H_RE_KOESTER.search(ocr_text)
    if km:
        koester_narrative = km.group(1).strip()

    # Read from the textarea, not from map_data — the dispatcher can correct a
    # misread Event # there, and the same reasoning that moved event_name onto
    # event_name_human (issue #87) applies: a value frozen at OCR time reaches
    # CalTopo but not D4H.
    event_number = ""
    em = _D4H_RE_EVENT_NUM.search(ocr_text)
    if em:
        candidate = em.group(1).strip()
        # The digit test is a POSITIVE shape check, deliberately cheaper and
        # broader than lengthening the placeholder list: every real agency
        # incident number carries digits ("26-212-071", "2026-LAW-54902292")
        # and prose never does. gemini.py's prompt gives this field no explicit
        # "if not on form" fallback (unlike Residence Address), so a
        # free-texted "Not visible on form" is unspecified upstream behaviour —
        # refuse it here rather than file it as the agency's case number.
        if (candidate
                and any(ch.isdigit() for ch in candidate)
                and not _D4H_RE_EVENT_NUM_PLACEHOLDER.match(candidate)):
            event_number = candidate

    return {
        "event_name":                  event_name,
        "event_number":                event_number,
        "mp_full_name":                mp_full_name,
        "lkp_lat":                     lkp_lat,
        "lkp_lng":                     lkp_lng,
        "lkp_address":                 lkp_address,
        "mp_dob":                      mp_dob,
        "mp_age":                      mp_age,
        "mp_sex":                      "",
        "officer_name":                officer_name,
        "officer_phone":               officer_phone,
        "at_risk_indicators":          at_risk_indicators,
        "koester_narrative":           koester_narrative,
        # #755 (Kris/Ops) — when the SUBJECT was last seen. D4H exposes no native
        # field for it: the involved-person schema was enumerated live against
        # team 1775 on 2026-08-18 and carries only createdAt/updatedAt, while the
        # incident carries startsAt/endsAt/createdAt (startsAt stays DISPATCH
        # time — decided, do not re-litigate). So it goes in involvementNotes.
        #
        # Shares _subject_last_seen_value with the Event Log and the Slack
        # welcome deliberately: "did the officer record a last-seen value" must
        # answer the same way on all three surfaces, or a dispatcher who sees the
        # line in Slack cannot tell why D4H lacks it.
        "last_seen_at":                _subject_last_seen_value(ocr_text),
        # Canonical IIS body for the D4H description field — the FULL INCIDENT
        # SUMMARY block stripped of section markers and the "Initial Incident
        # Summary:" header, starting at "Event Name:". WhatsApp Dispatch block
        # is dropped (mobile-copy duplicate of the Full block). Per dispatcher
        # design 2026-05-20 — D4H description shows a TODO list + blank line +
        # this body, so the dispatcher can select-delete the TODO and have the
        # field cleanly start with "Event Name:".
        "full_summary":                _extract_iis_body_for_d4h(ocr_text),
        **qn,
    }


@app.post("/send-notification")
async def send_notification(
    request: Request,
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """Orchestrate the 3-call Everbridge flow + Slack channel + initial tally.

    Live path (full mode + safe-mode-with-all-safe-targets): create event,
    send notification live, return monitor URL.
    Safe-mode draft path: create event, create template, schedule +30min
    template-delete Cloud Task, return template review URL.

    Both paths additionally: create the private incident channel, invite
    the IDENTICAL set in both slack_mode values (Item 3), pin the welcome
    message (Items 4 + 5), post groups-requested (Item 7), post initial
    tally to #active-incidents, persist the Firestore incident doc.

    Cloud Tasks enqueueing for the polling chain is STUBBED in Task 1.10b
    and lands in Task 1.11. /send-notification still returns 200 in the
    interim; the polling chain just doesn't fire until Task 1.11 deploys.

    Request body (JSON):
        event_name_human:    str   — HHMM-stripped form (e.g. "2026-04-25 MPD CALAVERAS")
        template_type:       str   — "incounty" | "mutualaid"
        title:               str   — notification subject line
        body:                str   — notification body text
        selected_target_ids: list  — frontend-prefixed (g:<id> for groups, c:<id> for contacts)
        mp_name:             str   — full name for the pinned welcome MP line; comma form ("Last, First") preserved verbatim
        mp_age:              int   — for the pinned welcome MP line
        mp_gender:           str   — "M" | "F" | "NB" | ""
        mp_at_risk:          str
        staging_address:     str
        staging_apple_url:   str
        staging_google_url:  str
        caltopo_url:         str
        officer_contact:     str   — officer name + phone from intake form Contact field; omitted from Slack welcome when blank
        ocr_text:            str   — full textarea content (= frontend _rawOcrText); D4H Phase 2 PR 5
        map_data:            dict  — structured /ocr map_data (= frontend _rawMapData); D4H Phase 2 PR 5

    Returns:
        {
          event_id, action, notification_id, template_id,
          slack_channel_id, slack_channel_name, deep_link_url
        }
    """
    _eb_slack_gate_or_raise()

    # ---- Body parse + validate ---------------------------------------------
    # 200 KB ceiling — typical body is <30 KB (event name + title + body text
    # + selected targets + ocr_text + map_data dict). Generous headroom for
    # long multi-paragraph notif bodies; pre-rejects accidental dispatcher-
    # paste-of-entire-pdf-text-into-body.
    if _content_length_exceeds(request.headers.get("content-length"), 200_000):
        raise HTTPException(status_code=413, detail="Request body too large")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_name_human = (body.get("event_name_human") or "").strip()
    template_type    = (body.get("template_type") or "").strip()
    notif_title      = (body.get("title") or "").strip()
    notif_body       = (body.get("body") or "").strip()
    try:
        selected_target_ids: list[str] = _coerce_selected_target_ids(
            body.get("selected_target_ids")
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # D4H Phase 2 PR 5 — frontend forwards the dispatcher-edited textarea and
    # the structured /ocr map_data so the backend can build the structured
    # ocr_data dict that d4h.create_incident_with_subject expects. These fields
    # are consumed downstream by the D4H create-incident step (Task 5.4); for
    # this task they are parsed but unused. Missing keys default to safe empty
    # values so EB+Slack flows remain unaffected.
    ocr_text: str   = body.get("ocr_text", "") or ""
    map_data: dict  = body.get("map_data", {}) or {}

    if not event_name_human:
        raise HTTPException(status_code=400, detail="Missing event_name_human")
    if not notif_title or not notif_body:
        raise HTTPException(status_code=400, detail="Missing title or body")
    if not selected_target_ids:
        raise HTTPException(status_code=400, detail="At least one target required")

    # County policy — enforce "SOSAR - " title prefix server-side BEFORE any
    # downstream EB call (live or template). Idempotent: dispatcher who
    # already typed the prefix gets no double-prefixing.
    notif_title = _enforce_sosar_title_prefix(notif_title)

    try:
        category_id = _category_for(template_type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # ---- Everbridge config preflight (Failure-mode rubric Q1) --------------
    # Every EB org value is validated HERE -- before the Step 1.5 tombstone and
    # before the Step 4 event create -- not where it is consumed.
    #
    # Validating at the consumption site was the bug: _require_caller_id() and
    # _require_deliver_paths() run inside _build_send_notification_payload at
    # Step 5, which is AFTER Step 4 has already created a real Everbridge
    # notification event. Notification events have no delete path (unlike
    # templates, which get a scheduled +30min delete), so a deploy that landed
    # before `terraform apply` would litter the live org with orphaned events
    # and leave the Firestore tombstone stuck at status="creating" -- and the
    # HHMM-derived event_id blocks a retry within the same minute.
    #
    # The per-value guards STAY: they are the last line for any future caller
    # that reaches the payload builder by another path. This is defence in
    # depth, not duplication.
    _eb_missing = [n for n, v in (
        ("EVERBRIDGE_ORG_ID", _EVERBRIDGE_ORG_ID),
        ("EVERBRIDGE_CALLER_ID", os.environ.get("EVERBRIDGE_CALLER_ID", "")),
        ("EVERBRIDGE_DELIVER_PATHS", os.environ.get("EVERBRIDGE_DELIVER_PATHS", "")),
    ) if not v.strip()]
    if _eb_missing:
        # 400, not 502: unset configuration is not transient and must not be
        # retried by Cloud Tasks (rubric Q5).
        logger.error("send_notification config_missing=%s", ",".join(_eb_missing))
        raise HTTPException(
            status_code=400,
            detail=_eb_config_missing_detail(_eb_missing),
        )

    # ---- Step 0.5: Stale-locality hard-confirm (issue #605) -----------------
    # ARMED ONLY when #604 flagged the LKP geocode as suspect — i.e. the
    # resolved city contradicted the address AND Google Maps could not fix it,
    # so we are dispatching against an anchor we already know is questionable.
    #
    # Arming on that flag rather than on "staging is in a different city than
    # the LKP" is deliberate. Staging is LEGITIMATELY across a city line often
    # (the nearest large lot), and _ebComposeBody() writes the LKP city into
    # every body by construction ("missing 45 year old in <city>"), so the
    # broader condition would fire on ordinary dispatches and train the
    # dispatcher to click through the one warning that matters.
    #
    # Runs BEFORE the Step 1.5 skeleton .create() on purpose. If it ran after,
    # a blocked dispatch would leave a tombstone behind and the dispatcher's
    # CORRECTED retry would be rejected as a duplicate ("dispatch already in
    # progress") — turning a helpful guard into a lockout during an active
    # callout. It also runs before check_rate_limits(): the check is pure and
    # touches nothing external, and charging a token per confirm would let a
    # correction cycle exhaust the shared 5/min budget at the worst moment.
    #
    # 422 (not 409) so the frontend's existing double-dispatch 409 branch is
    # untouched. Hard-confirm rather than hard-block, per Bill 2026-07-25: a
    # false-positive block on a real callout is the worst thing this tool could
    # do. The dispatcher acknowledges and re-sends with stale_locality_ack.
    if (map_data or {}).get("lkp_locality_suspect") and not bool(body.get("stale_locality_ack")):
        _requested_locality = (map_data or {}).get("lkp_locality_requested") or ""
        # staging_address is scanned alongside the notification text because it
        # is the surface that reaches responders EVEN IF the dispatcher never
        # edits the body: it becomes the Slack welcome's staging line and both
        # map links. On 2026-07-24, dispatching without the staging override
        # would have sent a wrong-city staging address to every responder while
        # the auto-composed body — which only ever names the city written on the
        # form, never the one the geocoder resolved — looked completely clean.
        _stale_locality, _stale_where = _find_stale_locality(
            (map_data or {}).get("lkp_locality"),
            _requested_locality,
            (
                ("notification title", notif_title),
                ("notification text", notif_body),
                ("staging address", body.get("staging_address") or ""),
            ),
        )
        if _stale_locality:
            logger.warning(
                "send-notification | stale locality present in outbound body — "
                "hard-confirm required | sub=%s",
                dispatcher.get("sub", "unknown"),
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "stale_locality",
                    "locality": _stale_locality,
                    "where": _stale_where,
                    # Same incident-comms rule as the create-map gate above:
                    # impact first, then what to do. The "On 2026-07-24 …" line
                    # was cut — a dispatcher mid-callout does not need the case
                    # history that motivated the guard, and every line they must
                    # read before acting is a line that makes the next warning
                    # easier to click through.
                    "message": (
                        f"HOLD — the map lookup for this incident could not be trusted and may "
                        f"send responders to the wrong location.\n\n"
                        f"Address says {_requested_locality or 'a different city'}. "
                        f"Map lookup resolved to “{_stale_locality}”. "
                        f"The {_stale_where} still says “{_stale_locality}”.\n\n"
                        f"Fix it:\n"
                        + (
                            f"• Use “Apply Override” to set staging in "
                            f"{_requested_locality or 'the right city'}, then send again.\n"
                            if _stale_where == "staging address"
                            else f"• Edit the {_stale_where} to remove “{_stale_locality}”, "
                                 f"then send again.\n"
                        )
                        + f"• Or correct the street name on the original intake form and "
                        f"resubmit that form to Dispatch Turbo — fixes staging, the map, and "
                        f"this text together.\n"
                        f"• Send anyway only if “{_stale_locality}” is correct."
                    ),
                },
            )

    # Rate limit (separate, tighter cap than /ocr per design Section 4 §5)
    await check_rate_limits(dispatcher.get("email", ""))

    # ---- Step 1: Compose canonical event_name + Firestore key --------------
    event_name = _compose_event_name_with_hhmm(event_name_human)
    event_id = _slugify_for_firestore(event_name)

    # ---- Step 1.5: Atomic double-dispatch guard (Cluster B, Melanie batch 2)
    # Before any EB / Slack / D4H side effect, .create() a skeleton doc
    # keyed by event_id. A double-click race produces the SAME event_id
    # (same dispatcher + same HHMM + same street); rate-limit (5/min)
    # does NOT prevent a 2nd click within the same second. AlreadyExists
    # from .create() means a sibling request is already in flight — 409
    # rejected here, BEFORE the EB notification fires twice. The full
    # incident doc OVERWRITES this skeleton at the existing .set() below.
    # G.LOW: _FirestoreAlreadyExists + new_skeleton_incident_doc imports
    # moved to module level so grep-based audits surface the dependency.
    _db_for_skeleton = _get_eb_slack_db()
    try:
        _db_for_skeleton.collection("incidents").document(event_id).create(
            new_skeleton_incident_doc(
                event_id=event_id,
                event_name=event_name,
                event_name_human=event_name_human,
                dispatcher_email=dispatcher.get("email", ""),
                created_at=datetime.datetime.now(datetime.timezone.utc),
            )
        )
    except _FirestoreAlreadyExists:
        raise HTTPException(
            status_code=409,
            detail=(
                "Dispatch already in progress for this event. "
                "If unexpected, wait for the next minute boundary (the "
                "event_id includes an HHMM stamp) and retry."
            ),
        )

    # ---- Step 2: Mode-aware routing decision (Task 1.9) --------------------
    decision = _route_send(selected_target_ids)

    # ---- Step 3: Split target IDs into contacts vs groups ------------------
    target_contact_ids = [
        _strip_target_prefix(t) for t in decision.target_ids if not _is_group(t)
    ]
    target_group_ids = [
        _strip_target_prefix(t) for t in decision.target_ids if _is_group(t)
    ]

    # ---- Step 4: Create Everbridge event (shared by both paths) ------------
    import everbridge as eb_module
    loop = asyncio.get_running_loop()
    everbridge_event_id = await loop.run_in_executor(
        None,
        functools.partial(
            eb_module.create_notification_event,
            org_id=_EVERBRIDGE_ORG_ID,
            event_name=event_name,
        ),
    )
    # Cluster C — persist the EB event_id immediately so a failure at Step 5
    # (send_notification_live) leaves a recoverable trail. Pre-Cluster-C this
    # finding (EB-H3) orphaned the EB event with no Firestore record at all,
    # leaving the dispatcher no path to identify it in the EB UI for manual
    # cleanup. The .update() is best-effort; the final .set() at Step 11
    # writes the same value idempotently on the success path.
    _patch_incident_doc_best_effort(
        event_id,
        {"everbridge_event_id": everbridge_event_id},
        where="step4_eb_event_created",
    )

    # ---- Step 5: Branch on decision (live vs safe-mode template) -----------
    notification_id: str | None = None
    template_id: str | None = None

    if decision.action == "send_live":
        notification_id = await loop.run_in_executor(
            None,
            functools.partial(
                eb_module.send_notification_live,
                org_id=_EVERBRIDGE_ORG_ID,
                event_id=everbridge_event_id,
                event_name=event_name,
                title=notif_title,
                body=notif_body,
                target_contact_ids=target_contact_ids,
                target_group_ids=target_group_ids,
                category_id=category_id,
            ),
        )
        deep_link_url = eb_module.monitor_active_url(notification_id)
        _patch_incident_doc_best_effort(
            event_id,
            {"notification_id": notification_id},
            where="step5_eb_send_live",
        )
    else:
        template_id = await loop.run_in_executor(
            None,
            functools.partial(
                eb_module.create_notification_template,
                org_id=_EVERBRIDGE_ORG_ID,
                event_id=everbridge_event_id,
                event_name=event_name,
                title=notif_title,
                body=notif_body,
                target_contact_ids=target_contact_ids,
                target_group_ids=target_group_ids,
                category_id=category_id,
            ),
        )
        deep_link_url = eb_module.review_draft_url(template_id)
        # Schedule unconditional template deletion at +30min (audit-safe per
        # Phase 0). Stub until Task 1.11 wires Cloud Tasks for real.
        _enqueue_template_delete_task(template_id, delay_s=_DELETE_TEMPLATE_DELAY_S)
        _patch_incident_doc_best_effort(
            event_id,
            {"template_id": template_id},
            where="step5_eb_create_template",
        )

    # ---- Step 6: Slack channel (create-or-collide) -------------------------
    # Failure-mode rubric Q2 ("what happens between line N and N+1?"): the EB
    # point of no return was crossed at Step 5. Channel creation is the ONE
    # post-EB step that was unguarded — every step below it is already
    # best-effort. A raise here (invalid_name_specials, rate_limit, Slack
    # outage) previously 500'd the dispatch AFTER EB had fired, orphaning a
    # half-incident (EB live, no D4H, no Firestore doc, no polling; both UI
    # indicators RED despite EB actually sending). Now: catch, mark
    # slack_status="failed", skip the incident-channel ops below, and continue
    # to the #active-incidents tally / D4H / persist / poll so the incident
    # stays tracked and closeable. The dispatcher creates the channel manually.
    slack_channel_id: str | None = None
    slack_channel_name: str | None = None
    slack_status: str = "ok"
    slack_error: str | None = None
    try:
        slack_channel_id, slack_channel_name = await loop.run_in_executor(
            None,
            functools.partial(
                _create_or_collide_channel,
                event_id=event_id,
                event_name_with_hhmm=event_name,
            ),
        )
        _patch_incident_doc_best_effort(
            event_id,
            {
                "slack_channel_id":   slack_channel_id,
                "slack_channel_name": slack_channel_name,
            },
            where="step6_slack_channel_created",
        )
    except Exception as e:
        slack_status = "failed"
        slack_error = type(e).__name__
        logger.warning(
            "Send-time Slack channel creation failed (%s) — EB already fired; "
            "skipping incident-channel ops (invites/DMs/welcome/CalTopo/"
            "groups-msg) and continuing to tally/D4H/persist/poll so the "
            "incident stays tracked and closeable. Dispatcher must create the "
            "Slack channel manually.",
            slack_error,
        )
        _patch_incident_doc_best_effort(
            event_id,
            {"slack_status": "failed", "slack_error": slack_error},
            where="step6_slack_channel_failed",
        )

    # ---- Step 7: Invite initial members (IDENTICAL set in both slack_modes per Item 3)
    # Pre-add set = dispatcher + SO coordinator + @active_incident_management
    # user-group members (issue #579). #active-incidents membership is NO LONGER
    # a source — it's an open watch-channel and lurkers were being pulled into
    # every incident. safe_list is still loaded here (NOT for pre-add anymore)
    # because Step 7b reads it for the VIP-DM cohort. slack_module + safe_list
    # stay OUTSIDE the channel guard: Step 10's #active-incidents tally uses
    # slack_module regardless, and Step 7b reads safe_list. The channel-dependent
    # work (member lookup + invites) is gated on slack_channel_id — when Step 6
    # failed there is no channel to invite to.
    import slack as slack_module
    safe_list = _load_safe_list_secret()
    so_coordinator_email = os.environ.get("SLACK_SO_COORDINATOR_EMAIL", "")
    initial_members: list[str] = []
    if slack_channel_id:
        initial_members = await loop.run_in_executor(
            None,
            functools.partial(
                slack_module.get_incident_channel_initial_members,
                dispatcher_email=dispatcher.get("email", ""),
                so_coordinator_email=so_coordinator_email,
            ),
        )
        # Per-user try/except: EB has already fired by this point. Any
        # SlackApiError that invite_user doesn't already swallow (rate_limit,
        # user_disabled, etc.) would otherwise propagate, return 500 to the
        # dispatcher, and orphan the live notification with no Firestore doc
        # and no closure path. Match the polling-loop pattern below (line ~6151).
        for user_id in initial_members:
            try:
                await loop.run_in_executor(
                    None,
                    functools.partial(slack_module.invite_user, slack_channel_id, user_id),
                )
            except Exception as e:
                logger.warning(
                    "Send-time invite_user failed (%s) for user_id=%s — continuing "
                    "so Firestore doc still gets written and polling chain runs",
                    type(e).__name__, user_id,
                )

    # ---- Step 7b: VIP-breakthrough DM to safe-list initial members ---------
    # PRD: SAR Slack Onboarding/PRD_DispatchTurbo_Responder_DM_VIP_Breakthrough.
    # Gate: SLACK_MODE=full only (matches poll-time real-invite gate).
    # Cohort: initial members whose email is on dispatch-safe-list. The
    # initial-invite set above is broader (dispatcher + SO coord +
    # @active_incident_management members, #579) — the safe-list is the
    # pilot DM cohort per Bill 2026-07-10, no longer a pre-add bucket.
    #
    # dm_sent_user_ids MUST be initialized outside the SLACK_MODE gate — in
    # shadow mode it stays empty and is still passed to new_incident_doc()
    # at Step 11 so the field exists in Firestore with a stable empty-list
    # value rather than being absent.
    dm_sent_user_ids: list[str] = []
    # `and slack_channel_id`: the DM body embeds the channel deep-link, so
    # sending it when Step 6 failed would blast safe-list responders a DM
    # pointing at a channel that does not exist. Skip entirely in that case.
    if _SLACK_MODE == "full" and slack_channel_id:
        dm_test_label = os.environ.get("DISPATCH_TURBO_TEST_LABEL", "")
        dm_text = slack_module.format_incident_dm_text(
            channel_id=slack_channel_id,
            test_label=dm_test_label,
        )
        # Resolve safe-list emails → user_ids for the DM eligibility set.
        # Redundant with the lookups inside get_incident_channel_initial_members
        # above, but that helper's contract lumps all emails together and
        # doesn't distinguish safe-list from active-incidents members. Keeping
        # its shape stable is worth the ~5-15 extra lookup calls here.
        dm_eligible_uids: set[str] = set()
        for email in safe_list.get("allowed_emails", []):
            try:
                uid = await loop.run_in_executor(
                    None,
                    functools.partial(slack_module.lookup_user_by_email, email),
                )
            except Exception as e:
                logger.warning(
                    "Send-time DM eligibility lookup failed (%s) — continuing",
                    type(e).__name__,
                )
                continue
            if uid:
                dm_eligible_uids.add(uid)
        for user_id in initial_members:
            if user_id not in dm_eligible_uids:
                continue
            if await _send_dm_and_persist(
                loop=loop, slack_module=slack_module,
                event_id=event_id, user_id=user_id,
                slack_channel_id=slack_channel_id, dm_text=dm_text,
                where="step7b_send_time_dm",
            ):
                # Accumulate Python-side so the field survives the final
                # .set(new_incident_doc(...)) at Step 11. Without this,
                # the ArrayUnion patch inside _send_dm_and_persist would be
                # OVERWRITTEN by the final .set() — leaving poll-time with
                # an empty slack_dm_sent_user_ids and firing a duplicate DM
                # when this responder YES's. Same "pass through to survive
                # the .set()" pattern as welcome_ts / caltopo_ts above.
                dm_sent_user_ids.append(user_id)

    # ---- Step 8: Pinned welcome message + CalTopo unfurl follow-up ---------
    # Welcome posted with unfurl suppressed so the noisy Google/Apple Maps
    # cards don't clutter the channel. The CalTopo URL is posted as a
    # SEPARATE follow-up message (also pinned) with default unfurl — Slack
    # then renders the map preview card. The CalTopo URL is intentionally
    # NOT in the welcome text: posting the same URL in two messages causes
    # Slack to dedupe and skip the unfurl on the second occurrence (verified
    # empirically 2026-04-29 even when the first message had
    # unfurl_links=False). Single-occurrence-per-channel = reliable unfurl.
    # Both messages are pinned so responders joining mid-incident see both
    # the structured incident text and the visual map preview at the top
    # of the pinned items list.
    caltopo_url = body.get("caltopo_url") or ""
    officer_contact = (body.get("officer_contact") or "").strip()
    welcome_text = slack_module.format_pinned_welcome(
        event_name=event_name_human,
        mp_name=body.get("mp_name") or "",
        age=body.get("mp_age"),
        gender=body.get("mp_gender") or "",
        at_risk=body.get("mp_at_risk") or "",
        # #670 — intake free text written beside a risk-factor question. Slack
        # ONLY; the Everbridge body is deliberately untouched (Bill, 2026-08-01)
        # because it is length-constrained and read on a locked phone.
        # slack.format_pinned_welcome drops snippets the at-risk line already
        # says, so this never echoes the row above it.
        notes=body.get("mp_notes") or "",
        # #755 (Kris/Ops) — when the SUBJECT was last seen, as the officer wrote
        # it. Slack ONLY, same rule as notes above. Rendered verbatim: a time
        # with no date stays a bare time rather than being completed by
        # inference (Bill, 2026-08-18).
        last_seen=body.get("mp_last_seen") or "",
        # The intake form's own Request box — what the requesting agency is
        # asking SAR to bring. Slack ONLY, same rule as notes above. Unlike
        # notes it is neither echo-filtered nor capped; format_pinned_welcome's
        # docstring carries the measurement behind both choices.
        request=body.get("mp_request") or "",
        officer_contact=officer_contact,
    )
    staging_text = slack_module.format_staging_message(
        staging_address=body.get("staging_address") or "",
        staging_apple_url=body.get("staging_apple_url") or "",
        staging_google_url=body.get("staging_google_url") or "",
        # Set by the frontend when the Event Log still carries the
        # officer-coordinate-vs-LKP distance WARNING. Read from the LIVE
        # textarea, so a dispatcher who resolves the conflict with the officer
        # and deletes that line also clears the responder-facing warning —
        # same principle as the staging text itself.
        unverified=bool(body.get("staging_unverified")),
        # Set when the staging POI lookup returned zero candidates, so the
        # recommendation list is Gemini training data with nothing behind it.
        unmapped=bool(body.get("staging_unmapped")),
    )
    # Cluster C: welcome post + Firestore persist of welcome_ts.
    # Try/except: EB has already fired (Cluster A pattern). Persist ts the
    # moment we have it so a later failure doesn't lose the dedup signal.
    welcome_ts: str = ""
    if slack_channel_id:
        try:
            welcome_ts = await loop.run_in_executor(
                None,
                functools.partial(
                    slack_module.post_message, slack_channel_id, welcome_text,
                    unfurl_links=False, unfurl_media=False,
                ),
            )
            _patch_incident_doc_best_effort(
                event_id,
                {"welcome_ts": welcome_ts},
                where="step8_welcome_posted",
            )
        except Exception as e:
            logger.warning(
                "Send-time welcome post failed (%s) — message NOT in channel; "
                "continuing so Firestore doc still gets written and polling "
                "chain runs",
                type(e).__name__,
            )
    # Batch-3 PR-G.3: pin is a SEPARATE try block. A pin-only failure
    # leaves the welcome message in the channel timeline but absent from
    # the pinned-items list, breaking the Locked Decision "both pinned"
    # invariant — distinct from a post failure (message never appears at
    # all). Pre-fix the two cases collapsed into a single conflated log
    # line that lost the operational signal; ops could not tell whether
    # the dispatcher needed to manually pin or re-trigger the welcome.
    # Recovery for a pin failure is a manual pin via Slack UI using the
    # ts logged below.
    if welcome_ts:
        try:
            await loop.run_in_executor(
                None,
                functools.partial(slack_module.pin_message, slack_channel_id, welcome_ts),
            )
        except Exception as e:
            logger.warning(
                "Send-time welcome pin failed (post succeeded, ts=%s) (%s) — "
                "welcome message in channel but NOT in pinned items; "
                "late-joining responders may miss it. Manual recovery: "
                "pin the message in Slack UI",
                welcome_ts, type(e).__name__,
            )
    # Staging message (issue #673) — its OWN post and its OWN pin, so a
    # dispatch that went out with the wrong staging location can be corrected
    # by deleting just this message and re-posting. A workspace admin can
    # delete a bot message; nobody but the bot can edit one, which is why the
    # pre-#673 single-welcome layout had no correction path at all.
    #
    # Posted BEFORE the CalTopo follow-up on purpose: CalTopo's unfurl renders
    # a large preview card, and staging is the more time-critical of the two.
    #
    # unfurl suppressed for the same reason the welcome suppresses it — these
    # are Apple/Google Maps URLs and their preview cards are pure clutter. The
    # CalTopo unfurl is unaffected: it is a different URL on a different
    # domain, and Slack's dedup is per-URL, so nothing here consumes the single
    # allowed occurrence that makes the map card render.
    #
    # staging_ts is hoisted to function scope (initialized "") so it is always
    # defined for the final new_incident_doc() call, and it is passed through
    # there — an incrementally-patched field that is NOT a param of that
    # builder gets wiped by the final .set() overwrite (the #568 -> #570 bug).
    #
    # Content guard mirrors the CalTopo block below (`if caltopo_url and ...`).
    # staging_address reaches "" whenever the frontend's `^1\.` staging regex
    # misses the textarea — a dispatcher who hand-edits or removes the
    # "Staging Area for Resources:" line produces exactly that. With empty
    # inputs format_staging_message renders the literal `Staging: <|> (<|G>)`,
    # and before this guard that string would have been posted AND PINNED.
    # Pre-#673 the same emptiness was a near-blank trailing line at the bottom
    # of an otherwise-useful welcome; splitting staging out is what promotes it
    # to a prominent pin, so the guard belongs with the split.
    # The WARNING carries no staging text — location is PII (core guarantee #3).
    staging_ts: str = ""
    staging_address_present = bool((body.get("staging_address") or "").strip())
    if not staging_address_present and slack_channel_id:
        logger.warning(
            "Staging message skipped — no staging address in the dispatch body; "
            "incident channel has NO staging pin and responders have no staging "
            "link. Dispatcher likely edited the 'Staging Area for Resources:' "
            "line out of the summary"
        )
    if staging_address_present and slack_channel_id:
        try:
            staging_ts = await loop.run_in_executor(
                None,
                functools.partial(
                    slack_module.post_message, slack_channel_id, staging_text,
                    unfurl_links=False, unfurl_media=False,
                ),
            )
            _patch_incident_doc_best_effort(
                event_id,
                {"staging_ts": staging_ts},
                where="step8_staging_posted",
            )
        except Exception as e:
            logger.warning(
                "Send-time staging post failed (%s) — staging NOT in channel; "
                "responders have no staging link. Continuing so Firestore doc "
                "still gets written and the polling chain runs",
                type(e).__name__,
            )
        # Pin is a SEPARATE try block, same rationale as the welcome and
        # CalTopo splits above: a pin-only failure leaves staging in the
        # timeline but absent from the pinned-items list, which is the exact
        # affordance #673 exists to create. Recovery is a manual pin via the
        # Slack UI using the ts logged below.
        if staging_ts:
            try:
                await loop.run_in_executor(
                    None,
                    functools.partial(slack_module.pin_message, slack_channel_id, staging_ts),
                )
            except Exception as e:
                logger.warning(
                    "Send-time staging pin failed (post succeeded, ts=%s) (%s) — "
                    "staging in channel but NOT in pinned items; responders "
                    "checking pinned messages for staging will not find it. "
                    "Manual recovery: pin in Slack UI",
                    staging_ts, type(e).__name__,
                )
    # CalTopo unfurl follow-up — only post when we have a URL. Pinned so
    # responders joining mid-incident see the map preview at the top of
    # the pinned items list alongside the welcome. caltopo_ts is hoisted
    # to function scope (initialized "") so it's always defined for the
    # final new_incident_doc() call below, regardless of which branch
    # executed or whether the post succeeded.
    caltopo_ts: str = ""
    if caltopo_url and slack_channel_id:  # channel guard (Step 6): no channel → no post
        try:
            caltopo_ts = await loop.run_in_executor(
                None,
                functools.partial(slack_module.post_message, slack_channel_id, caltopo_url),
            )
            _patch_incident_doc_best_effort(
                event_id,
                {"caltopo_ts": caltopo_ts},
                where="step8_caltopo_posted",
            )
        except Exception as e:
            logger.warning(
                "Send-time CalTopo post failed (%s) — map URL NOT posted to "
                "channel; continuing so Firestore doc still gets written and "
                "polling chain runs",
                type(e).__name__,
            )
        # Batch-3 PR-G.4: pin is a SEPARATE try block, same reason as the
        # welcome split above. A pin-only failure for CalTopo undermines
        # the explicit Locked Decision that CalTopo be pinned so late-
        # joining responders see the map preview at the top of pinned
        # items. Pre-fix this also collapsed with the post failure into
        # one conflated log line. Recovery for a pin failure is a manual
        # pin via Slack UI using the ts logged below.
        if caltopo_ts:
            try:
                await loop.run_in_executor(
                    None,
                    functools.partial(slack_module.pin_message, slack_channel_id, caltopo_ts),
                )
            except Exception as e:
                logger.warning(
                    "Send-time CalTopo pin failed (post succeeded, ts=%s) (%s) — "
                    "map URL in channel but NOT in pinned items; map preview "
                    "may not appear at top of pinned items for responders "
                    "joining mid-incident. Manual recovery: pin in Slack UI",
                    caltopo_ts, type(e).__name__,
                )

    # ---- Step 9: Item 7 "Groups requested by dispatcher" message -----------
    requested_names: list[str] = []
    contact_group_map: dict[str, list[str]] = {}
    contact_email_map: dict[str, list[str]] = {}
    if target_group_ids:
        # Look up canonical group names (SAR- prefix stripped) from EB.
        # Single API call per send; the dispatcher selection set is small.
        # try/except: EB has already fired. A 401 (token expiry) or 5xx
        # here would otherwise propagate and orphan the live notification
        # with no Firestore doc. Fall back to raw group IDs in the Slack
        # groups-requested message so dispatch can complete and the
        # polling chain runs. The per-group loop below is already guarded.
        gid_to_name: dict[str, str] = {}
        try:
            groups_list = await loop.run_in_executor(
                None,
                functools.partial(eb_module.list_groups, _EVERBRIDGE_ORG_ID),
            )
            gid_to_name = {g["id"]: g["name"] for g in groups_list}
        except Exception as e:
            logger.warning(
                "list_groups failed (%s) — Slack groups-requested message "
                "will show raw IDs; dispatch continues",
                type(e).__name__,
            )
        requested_names = [
            gid_to_name.get(gid, gid) for gid in target_group_ids
        ]
        # Cluster E (EB-M4): list_groups can succeed-but-empty (or partial)
        # when the SHO-SAR Dispatcher SA loses visibility on a group — see
        # ops-runbook "Everbridge group not visible in dispatch console."
        # Pre-fix the .get(gid, gid) fallback rendered raw EB IDs in the
        # Slack groups-requested message with no signal — looked identical
        # to a real group name to the dispatcher. Surface the mismatch as
        # a WARNING so on-call sees it and triggers the admin-grant flow.
        missing_gids = [gid for gid in target_group_ids if gid not in gid_to_name]
        if missing_gids:
            logger.warning(
                "list_groups returned %d groups but %d selected gid(s) "
                "not present — raw EB IDs will leak into Slack "
                "groups-requested message (missing_count=%d, total_visible=%d). "
                "Likely SA-visibility gap — see ops-runbook 'EB group not "
                "visible in dispatch console'.",
                len(gid_to_name), len(missing_gids), len(missing_gids), len(gid_to_name),
            )
        # Fetch group membership + emails for each targeted group.
        # list_group_member_contacts() returns [{contact_id, emails}] in a
        # single call, populating both maps:
        #   contact_group_map  → per-group tally breakdown in #active-incidents
        #   contact_email_map  → Slack invite fallback (avoids callResultByPaths gap)
        for gid in target_group_ids:
            gname = gid_to_name.get(gid, gid)
            try:
                member_contacts = await loop.run_in_executor(
                    None,
                    functools.partial(
                        eb_module.list_group_member_contacts, _EVERBRIDGE_ORG_ID, gid
                    ),
                )
                for mc in member_contacts:
                    cid = mc["contact_id"]
                    contact_group_map.setdefault(cid, []).append(gname)
                    if mc["emails"] and cid not in contact_email_map:
                        contact_email_map[cid] = mc["emails"]
            except Exception:
                logger.warning(
                    "list_group_member_contacts failed for group %s (%s) "
                    "— per-group tally and Slack invite fallback will be empty for this group",
                    gid, gname,
                )
        # Cluster C — Cluster-A pattern: post-EB Slack call must not crash
        # dispatch. The "Groups requested" message is informational; failure
        # here doesn't affect EB delivery, Slack channel state, or D4H.
        # Channel guard (Step 6): skip the post when channel creation failed —
        # the group lookups above still ran (they feed the tally + D4H).
        if slack_channel_id:
            try:
                await loop.run_in_executor(
                    None,
                    functools.partial(
                        slack_module.post_message,
                        slack_channel_id,
                        slack_module.format_groups_requested(requested_names),
                    ),
                )
            except Exception as e:
                logger.warning(
                    "Send-time groups-requested post failed (%s) — continuing",
                    type(e).__name__,
                )
    # Directly-targeted contacts (not via group) — fetch emails individually.
    for cid in target_contact_ids:
        if cid not in contact_email_map:
            try:
                emails = await loop.run_in_executor(
                    None,
                    functools.partial(
                        eb_module.get_contact_emails, _EVERBRIDGE_ORG_ID, cid
                    ),
                )
                if emails:
                    contact_email_map[cid] = emails
            except Exception:
                logger.warning(
                    "get_contact_emails failed for contact %s — Slack invite fallback unavailable",
                    cid,
                )

    # ---- Step 10: Initial tally to #active-incidents -----------------------
    initial_header = (
        "🔔 Everbridge ACTIVE" if decision.action == "send_live"
        else "📋 Awaiting dispatcher send"
    )
    initial_doc_for_tally = {
        "event_name_human":          event_name_human,
        "responders":                [],
        "last_non_empty_responders": [],
        "requested_group_names":     requested_names,
        "contact_group_map":         contact_group_map,
        "contact_email_map":         contact_email_map,
    }
    active_incidents_channel_id = os.environ.get("ACTIVE_INCIDENTS_CHANNEL_ID", "")
    tally_ts = ""
    if active_incidents_channel_id:
        # Cluster C: tally is the highest-value ts to persist because every
        # /poll-incident cycle and /close-incident-polling edit references
        # it. Pre-Cluster-C this finding (Slack-H4) orphaned the tally
        # permanently in #active-incidents when a later step (D4H, the
        # final .set()) raised — no edit/remove was possible. The
        # incremental .update() captures it the moment we have it; the
        # final .set() at Step 11 idempotently re-writes it on success.
        try:
            tally_ts = await loop.run_in_executor(
                None,
                functools.partial(
                    slack_module.post_message,
                    active_incidents_channel_id,
                    _compose_active_incidents_tally(initial_doc_for_tally, initial_header),
                ),
            )
            _patch_incident_doc_best_effort(
                event_id,
                {"active_incidents_ts": tally_ts},
                where="step10_tally_posted",
            )
        except Exception as e:
            logger.warning(
                "Send-time tally post failed (%s) — continuing; tally edits "
                "from /poll-incident will be no-ops but dispatch completes",
                type(e).__name__,
            )
    else:
        logger.warning(
            "ACTIVE_INCIDENTS_CHANNEL_ID not set — skipping #active-incidents tally"
        )

    # ---- Step 10.5: D4H create-incident (best-effort) ----------------------
    # D4H Phase 2 PR 5 — synchronously attempt to create the D4H incident.
    # Failure does NOT abort dispatch: EB has already fired and Slack channel
    # is already provisioned. D4H is post-incident-value, not real-time-critical.
    # Outcome lands in the Firestore incidents/{event_id} doc via the d4h_*
    # fields below. The /dispatch-status endpoint (Task 5.6) exposes them.
    d4h_status: str = "pending"
    d4h_activity_id: int | None = None
    d4h_error: str | None = None
    # Milestone-only contract (PR 7.1): d4h_event_log holds dispatch-time
    # milestones (D4H incident created / bulk-ABSENT result / dispatch-time
    # failures) that the frontend drains into the dispatcher textarea. Per-YES
    # detail (responder attendance PATCHes, K9 sync, drone-add) goes to system
    # logs (logger.info / logger.warning), NEVER this array. See
    # TestD4HPerYesMilestoneOnlyContract in test_main_regression.py.
    d4h_event_log: list[str] = []
    try:
        # Inject dispatch-time milestones into the Event Log section before
        # building the D4H payload — so D4H's incident description carries
        # the full event timeline. Without this, D4H sees the textarea
        # state at click-time (which ends at "CalTopo map created") and
        # misses 3 entries that the frontend only writes AFTER
        # /send-notification returns. See _inject_dispatch_milestones_into_event_log.
        # Issue #542: canonical UTC + PT-hint format. See _format_event_log_ts.
        _d4h_ts = _format_event_log_ts()
        _eb_mode_label = "LIVE" if decision.action == "send_live" else "DRAFT"
        _eb_groups_label = ", ".join(requested_names) if requested_names else "(none)"
        _eb_indiv_count = len(target_contact_ids)
        # Mirror the frontend's display behavior: strip the SOSAR prefix so
        # the textarea entry on the frontend and the D4H-injected entry
        # carry the same title text. The actual EB notification title (the
        # one EB sends) IS prefixed — that's enforced server-side. This is
        # only the cross-system audit-line cosmetic.
        _eb_title_display = (
            notif_title[len("SOSAR - "):]
            if notif_title.startswith("SOSAR - ")
            else notif_title
        )
        # Channel guard (Step 6): surface a creation failure in the milestone
        # (it flows into d4h_event_log → the dispatcher textarea) so the
        # dispatcher sees they must create the Slack channel manually.
        _slack_milestone = (
            f"{_d4h_ts} - Slack incident channel created: #{slack_channel_name}"
            if slack_channel_id else
            f"{_d4h_ts} - Slack incident channel creation FAILED ({slack_error}) "
            f"— EB sent; create the Slack channel manually"
        )
        _d4h_dispatch_milestones = [
            f"{_d4h_ts} - Everbridge notification sent ({_eb_mode_label}) — "
            f"{_eb_title_display} — groups: {_eb_groups_label} — individuals: {_eb_indiv_count}",
            _slack_milestone,
            f"{_d4h_ts} - D4H incident request filed at dispatch time",
        ]
        # Issue #542: EB + Slack entries also land in d4h_event_log so the
        # frontend renders them server-side with consistent UTC + PT-hint
        # format, regardless of the dispatcher's browser timezone. Pre-fix
        # these entries were generated frontend-side via new Date(), so a
        # traveling dispatcher (Bill on EDT during the 2026-05-31 #519
        # closure) saw them in browser-local time while the OCR-time
        # entries above rendered in Pacific — a 3-hour apparent gap on
        # the same incident. The third milestone ("D4H incident request
        # filed at dispatch time") stays D4H-description-only — it's a
        # placeholder for the upcoming create, not a dispatcher-facing
        # milestone (the actual "D4H incident created" entry is appended
        # later, after the D4H POST succeeds).
        d4h_event_log.extend(_d4h_dispatch_milestones[:2])
        _ocr_text_for_d4h = _inject_dispatch_milestones_into_event_log(
            ocr_text, _d4h_dispatch_milestones
        )
        ocr_data_for_d4h = _build_ocr_data_for_d4h(
            ocr_text=_ocr_text_for_d4h,
            map_data=map_data,
            event_name_human=event_name_human,
            fallback_mp_at_risk=body.get("mp_at_risk", "") or "",
            fallback_mp_full_name=body.get("mp_name", "") or "",
        )
        # eb_groups = the EB group names the dispatcher selected. d4h.py
        # maps these to D4H tag IDs via _map_eb_groups_to_d4h_tags, with
        # unmapped groups returned for event-log surfacing.
        d4h_eb_groups: list[str] = list(requested_names) if requested_names else []
        dispatch_dt = datetime.datetime.now(datetime.timezone.utc)
        # 2026-05-16 partial-failure recovery: create_incident_with_subject
        # now returns a 3-tuple. _post_incident failure still raises (no
        # activity_id to recover); _post_tags / _post_involved_person failures
        # are captured in post_create_failures so bulk-ABSENT can still run
        # against the created incident.
        d4h_activity_id, unmapped_groups, post_create_failures = d4h.create_incident_with_subject(
            ocr_data=ocr_data_for_d4h,
            dispatch_dt=dispatch_dt,
            eb_groups=d4h_eb_groups,
            dispatcher_metadata={
                "dispatcher_name":  dispatcher.get("name", "") or dispatcher.get("email", ""),
                "dispatcher_email": dispatcher.get("email", ""),
            },
            project_id=os.environ.get("PROJECT_ID"),
        )
        # Batch-3 PR-G.7: persist d4h_activity_id the moment it's obtained.
        # Pre-fix it was only written at the final .set() at Step 11. If a
        # Firestore failure occurred between this point and Step 11
        # (transient outage, Cloud Run preemption mid-handler), the D4H
        # incident existed but the app had no record of its ID. Downstream
        # per-YES Cloud Tasks would call _load_d4h_activity_id() → None →
        # silently skip every attendance sync, leaving the incident
        # orphaned with no signal beyond d4h_status (which itself wasn't
        # persisted until Step 11 either). Same belt-and-braces shape as
        # Cluster C (PR #517) for EB IDs (everbridge_event_id,
        # notification_id, slack_channel_id, welcome_ts, caltopo_ts).
        # Failure-mode rubric Q1 ("What is the point of no return?") —
        # the D4H POST is irreversible (we don't DELETE D4H records to
        # roll back), so the recovery surface MUST persist the ID
        # immediately. The final .set() at Step 11 idempotently re-writes
        # this same field on success, so the worst case is the persist
        # fails AND Step 11 also fails — same as pre-fix.
        _patch_incident_doc_best_effort(
            event_id,
            {"d4h_activity_id": d4h_activity_id},
            where="step10_5_d4h_created",
        )
        # Selective-mode (fullTeam: false) terminal status: "done" on a clean
        # create — attendance starts empty; no async bulk-ABSENT step to wait
        # for. Per-YES POST-new arrives later via the Cloud Tasks queue and
        # writes to system logs only (not d4h_event_log) per the milestone-only
        # contract. "partial" still applies when post-create steps (tags /
        # involved-person) failed despite the incident existing — frontend
        # collapses partial→🔴 under the simplified 🟢/🔴 indicator scheme.
        d4h_status = "partial" if post_create_failures else "done"
        # Issue #542: canonical UTC + PT-hint format. See _format_event_log_ts.
        _ts = _format_event_log_ts()
        d4h_event_log.append(f"{_ts} - D4H incident created (activity_id={d4h_activity_id})")
        for unmapped_name in unmapped_groups:
            d4h_event_log.append(
                f'{_ts} - D4H tag mapping: EB group "{unmapped_name}" not in mapping table '
                f"— D4H tag NOT applied; Slack sync proceeds normally; verify in D4H manually post-incident"
            )
        for fail in post_create_failures:
            d4h_event_log.append(f"{_ts} - {fail}")
            logger.warning("D4H post-create partial failure (activity_id=%s): %s",
                           d4h_activity_id, fail[:200])
    except (d4h.D4HClientError, d4h.D4HServerError) as exc:
        sanitized = _sanitize_d4h_error(exc)
        d4h_status = "failed"
        d4h_error = sanitized
        # Issue #542: canonical UTC + PT-hint format. See _format_event_log_ts.
        _ts = _format_event_log_ts()
        d4h_event_log.append(f"{_ts} - D4H sync failed: {sanitized}")
        logger.warning("D4H create failed for event_id=%s: %s", event_id, sanitized)
    except Exception as exc:
        # Defensive — d4h.py contract is to raise D4HClientError/D4HServerError, but
        # an unexpected ValueError (e.g., env var missing inside create_incident_with_subject)
        # must not crash the dispatch response.
        sanitized = _sanitize_d4h_error(exc)
        d4h_status = "failed"
        d4h_error = sanitized
        # Issue #542: canonical UTC + PT-hint format. See _format_event_log_ts.
        _ts = _format_event_log_ts()
        d4h_event_log.append(f"{_ts} - D4H sync failed (unexpected): {sanitized}")
        logger.warning("D4H create raised unexpected %s for event_id=%s: %s", type(exc).__name__, event_id, sanitized)

    # Selective-mode (fullTeam: false) removes the prior bulk-ABSENT step.
    # Attendance starts empty at incident creation; per-YES POST-new records
    # arrive via the d4h-per-yes-sync Cloud Tasks queue as responders accept.

    # ---- Step 11: Persist incident doc to Firestore ------------------------
    from incidents import new_incident_doc
    db = _get_eb_slack_db()
    incident_doc = new_incident_doc(
        event_id=event_id,
        event_name=event_name,
        event_name_human=event_name_human,
        dispatcher_email=dispatcher.get("email", ""),
        everbridge_event_id=everbridge_event_id,
        notification_id=notification_id,
        template_id=template_id,
        slack_channel_id=slack_channel_id,
        slack_channel_name=slack_channel_name,
        active_incidents_ts=tally_ts,
        welcome_ts=welcome_ts,
        staging_ts=staging_ts,
        caltopo_ts=caltopo_ts,
        everbridge_mode=_EVERBRIDGE_MODE,
        slack_mode=_SLACK_MODE,
        action=decision.action,
        selected_target_ids=decision.target_ids,
        requested_group_names=requested_names,
        contact_group_map=contact_group_map,
        contact_email_map=contact_email_map,
        # Pass the accumulated send-time DMs so the final .set() re-writes
        # them into slack_dm_sent_user_ids — otherwise the ArrayUnion patches
        # from _send_dm_and_persist would be overwritten, and poll-time's
        # idempotency check (doc.get("slack_dm_sent_user_ids")) would read
        # empty and fire duplicate DMs to already-DM'd responders.
        slack_dm_sent_user_ids=dm_sent_user_ids,
    )
    # Fold the Slack channel-provisioning outcome in (Step 6 guard). Like the
    # D4H fields, new_incident_doc() defaulted these to "ok"/None; overwrite
    # with the real Step 6 result so the persisted doc + /dispatch-status
    # reflect a channel-creation failure.
    incident_doc["slack_status"]    = slack_status
    incident_doc["slack_error"]     = slack_error
    # Fold the D4H outcome into the doc before persisting. new_incident_doc()
    # set "pending" / None / None / [] defaults; overwrite with the actual
    # post-dispatch state captured by Step 10.5.
    incident_doc["d4h_status"]      = d4h_status
    incident_doc["d4h_activity_id"] = d4h_activity_id
    incident_doc["d4h_error"]       = d4h_error
    incident_doc["d4h_event_log"]   = list(d4h_event_log)
    db.collection("incidents").document(event_id).set(incident_doc)

    # ---- Step 12: Enqueue first /poll-incident task (STUBBED until Task 1.11)
    poll_mode = "post_discovery" if decision.action == "send_live" else "pre_discovery"
    _enqueue_poll_task(event_id, delay_s=0, mode=poll_mode)

    # ---- Step 13: Audit log (no PII — only IDs and counts) -----------------
    logger.info(
        "Send: event_id=%s action=%s notif=%s tmpl=%s targets=%d channel=%s",
        event_id, decision.action,
        notification_id or "-",
        template_id or "-",
        len(decision.target_ids),
        slack_channel_id,
    )

    return JSONResponse({
        "event_id":           event_id,
        "action":             decision.action,
        "notification_id":    notification_id,
        "template_id":        template_id,
        "slack_channel_id":   slack_channel_id,
        "slack_channel_name": slack_channel_name,
        # Step 6 guard: "ok" | "failed". On "failed" slack_channel_id is null
        # and the frontend paints the Slack indicator 🔴 while EB stays 🟢.
        "slack_status":       slack_status,
        "slack_error":        slack_error,
        "deep_link_url":      deep_link_url,
        # D4H Phase 2 2026-05-16: the frontend uses these to append
        # D4H event-log entries to the textarea (parallel to how it
        # constructs the EB and Slack entries from notification_id /
        # slack_channel_name). d4h_event_log carries the rich pre-formatted
        # lines (one per D4H sub-step outcome); d4h_status / activity_id /
        # error supply the summary for the frontend's logic to choose
        # which lines to surface.
        "d4h_status":         d4h_status,
        "d4h_activity_id":    d4h_activity_id,
        "d4h_error":          d4h_error,
        "d4h_event_log":      list(d4h_event_log),
    })


def _build_dispatch_status_response(doc: dict | None) -> dict:
    """Return the 7-field dispatch-status response from a Firestore doc.

    Pure-logic — no I/O. Used by GET /dispatch-status/{event_id} (Task 5.6).
    Always returns exactly the 7 design-doc keys; missing doc fields
    default to None (status/error) or [] (d4h_event_log).

    d4h_event_log is exposed here (PR 7 amendment) so the frontend poll
    loop can drain async Cloud Tasks worker entries into the dispatcher
    textarea without a separate round-trip to /incident-status.

    Internal fields (d4h_activity_id) are still excluded.

    Mirror: backend/test_incident_endpoints.py::TestBuildDispatchStatusResponse.
    """
    d = doc or {}
    return {
        "eb_status":     d.get("eb_status"),
        "slack_status":  d.get("slack_status"),
        "d4h_status":    d.get("d4h_status"),
        "eb_error":      d.get("eb_error"),
        "slack_error":   d.get("slack_error"),
        "d4h_error":     d.get("d4h_error"),
        "d4h_event_log": d.get("d4h_event_log", []),
    }


@app.get("/dispatch-status/{event_id}")
async def dispatch_status(
    event_id: str,
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """Return per-system dispatch status for `event_id`.

    Lean read-only endpoint suitable for high-frequency polling (PR 7 will
    poll every ~500ms during the dispatch window). Auth-gated to the
    dispatcher allowlist but NOT ownership-gated — any authorized
    dispatcher can query (matches the "team can monitor an active
    dispatch" use case).

    Returns 404 if no incident doc exists for the given event_id.
    No PII — response contains status enums + sanitized error strings only.

    Design contract (§4.2, PR 7 amendment): seven fields:
      eb_status, slack_status, d4h_status,
      eb_error, slack_error, d4h_error, d4h_event_log
    """
    _eb_slack_gate_or_raise()
    db = _get_eb_slack_db()
    snap = db.collection("incidents").document(event_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Incident not found")
    return JSONResponse(_build_dispatch_status_response(snap.to_dict()))


@app.get("/incident-status/{event_id}")
async def incident_status(
    event_id: str,
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """Read the Firestore incident doc for the given event_id.

    Returns a JSON shape suitable for the dispatcher's "View Status"
    button. Datetime fields (created_at, manually_confirmed_at, etc.)
    are converted to ISO 8601 strings.

    Returns 404 for missing-or-not-owned (same response in both cases —
    defense-in-depth, prevents ownership-existence inference).
    """
    _eb_slack_gate_or_raise()
    user_email = dispatcher.get("email", "")
    db = _get_eb_slack_db()
    snap = db.collection("incidents").document(event_id).get()
    doc = snap.to_dict() if snap.exists else None

    err = _validate_incident_ownership(doc, user_email)
    if err:
        raise HTTPException(status_code=err[0], detail=err[1])

    # Compute the manual-confirm trigger BEFORE serialization so the bool
    # reflects the live `created_at` datetime (not the ISO string the
    # serializer produces). The dispatch console polls this endpoint every
    # 30s after a safe-mode draft and renders a yellow recovery banner when
    # this flag flips true. See _manual_confirm_offered() for the spec.
    serialized = _serialize_incident_doc_for_api(doc)
    serialized["manual_confirm_offered"] = _manual_confirm_offered(doc)
    return JSONResponse(serialized)


@app.post("/close-incident-polling/{event_id}")
async def close_incident_polling(
    event_id: str,
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """Dual-stop: set manual_stop_requested AND call DELETE on the EB notification.

    Two-part stop (see CLAUDE.md Locked Design Decision — dual-stop semantic):
      1. Call DELETE /notifications/{orgId}/{notificationId} — immediately halts
         SMS/voice/email escalation in Everbridge. Best-effort: if the EB call
         fails, we log a warning and continue. The dispatcher's close is recorded
         regardless. everbridge_end_status in the response indicates the outcome.
      2. Set manual_stop_requested=True on the Firestore doc — the polling chain's
         next cycle calls stop_incident('manual') and exits without re-enqueueing.

    Returns 404 on missing-or-not-owned, 409 if already in a terminal state,
    200 with {"status":"ok", "everbridge_end_status": str} on success.

    everbridge_end_status values:
      "ok"                        — EB stop call succeeded
      "failed"                    — EB stop call raised (see Cloud Run logs)
      "skipped_no_notification"   — no notification_id on doc (pre-discovery or
                                    template-only path — nothing to stop in EB)
    """
    _eb_slack_gate_or_raise()
    await check_rate_limits(dispatcher.get("email", ""))
    import everbridge as eb_module

    user_email = dispatcher.get("email", "")
    db = _get_eb_slack_db()
    doc_ref = db.collection("incidents").document(event_id)

    # Cluster F (Slack-M10): atomic read-validate-write to close the race
    # with a concurrent poll cycle. Pre-fix, the doc was read via .get()
    # (no transaction) → validated → written via .update(). Between read
    # and write, a poll cycle could independently transition the doc to a
    # terminal state, producing duplicate _edit_tally calls in quick
    # succession with potentially conflicting terminal headers.
    #
    # The transaction wraps the read + validate + write so only one writer
    # successfully sets manual_stop_requested. Side effects (EB
    # end_notification) run OUTSIDE the transaction, AFTER it commits —
    # the dispatcher's stop request always succeeds atomically before any
    # EB-side action runs. If end_notification then fails, manual_stop is
    # already persisted and the poll chain still stops on its next cycle.
    # G.LOW: `from google.cloud import firestore as _firestore` moved to
    # module level so grep-based audits surface the dependency.

    @_firestore.transactional
    def _txn_apply_manual_stop(transaction, ref):
        snap_txn = ref.get(transaction=transaction)
        if not snap_txn.exists:
            return None, (404, "Incident not found or not yours")
        doc_txn = snap_txn.to_dict() or {}
        err_txn = _validate_close_polling(doc_txn, user_email)
        if err_txn:
            return None, err_txn
        transaction.update(ref, {"manual_stop_requested": True})
        return doc_txn, None

    txn = db.transaction()
    doc, err = _txn_apply_manual_stop(txn, doc_ref)
    if err:
        raise HTTPException(status_code=err[0], detail=err[1])

    # ---- 1. Stop the Everbridge notification (best-effort) -------------------
    notification_id = (doc or {}).get("notification_id")
    eb_end_status = "skipped_no_notification"
    if notification_id:
        loop = asyncio.get_running_loop()
        try:
            # Cluster E (EB-H2): capture the return value. end_notification()
            # returns True on stopped/already-stopped, False on 404 (gone) or
            # non-JSON 200 (gateway error). Pre-fix, the return was discarded
            # and eb_end_status was unconditionally "ok" — masking the
            # distinction between "we actually stopped escalation" and
            # "the notification was already gone." The audit log + Firestore
            # doc + caller response now surface that distinction.
            eb_stopped = await loop.run_in_executor(
                None,
                functools.partial(
                    eb_module.end_notification,
                    org_id=_EVERBRIDGE_ORG_ID,
                    notification_id=notification_id,
                ),
            )
            eb_end_status = "ok" if eb_stopped else "already_gone"
            logger.info(
                "Everbridge stop outcome: event_id=%s nid=%s status=%s",
                event_id, notification_id, eb_end_status,
            )
        except Exception as e:
            eb_end_status = "failed"
            logger.warning(
                "Everbridge stop failed (best-effort — close continues): "
                "event_id=%s nid=%s err=%s",
                event_id, notification_id, type(e).__name__,
            )

    # Cluster F (Slack-M10): manual_stop_requested already persisted by the
    # transaction above. The polling chain's next cycle will see it and call
    # stop_incident('manual') + _edit_tally exactly once.
    logger.info(
        "Manual stop requested: event_id=%s by=%s eb_end_status=%s",
        event_id, user_email, eb_end_status,
    )
    return {"status": "ok", "event_id": event_id, "everbridge_end_status": eb_end_status}


# ---------------------------------------------------------------------------
# #612 — follow-up Everbridge notification
# Design: docs/plans/2026-07-25-dispatch-correction-recovery-design.md §3
# ---------------------------------------------------------------------------

def _followup_idempotency_key(event_id: str, title: str, body: str) -> str:
    """Deterministic key for the Cluster B double-click guard.

    Keyed on the CONTENT, not a timestamp: a double-click sends byte-identical
    title+body, so the second request collides and is rejected. A dispatcher who
    deliberately sends a SECOND, DIFFERENT correction gets a different key and is
    correctly allowed through — which is why the event_id alone is not the key.

    The digest is over the normalized text (what actually ships), so two requests
    differing only in smart quotes are correctly treated as the same send.
    """
    digest = hashlib.sha256(
        f"{event_id}\x00{title}\x00{body}".encode("utf-8")
    ).hexdigest()[:32]
    return f"{event_id}__{digest}"


@app.post("/send-followup-notification/{event_id}")
async def send_followup_notification(
    event_id: str,
    request: Request,
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """Send a Standard follow-up notification under an existing incident (#612).

    A follow-up is a NEW notification under the incident's EXISTING
    `everbridge_event_id` (§2.1) — not a new event. It is `type: "Standard"`, so
    it carries no Yes/No questionnaire and nothing polls it (§2.2). Recipients are
    derived, never dispatcher-selected: everyone who affirmatively replied plus
    everyone who has not yet replied, excluding decliners (§2.3).

    AUTH MODEL — allowlist-only, deliberately NOT ownership-gated.
    Any authorized dispatcher may send a follow-up for any incident, matching
    `GET /dispatch-status` rather than `GET /incident-status`. Rationale: on
    2026-07-24 the dispatcher who needed to send the correction was driving to
    collect drone gear, and the correction is the time-critical act. Gating on
    `dispatcher_email` would block precisely the backup dispatcher who is free to
    help during a shift handoff. Everyone reaching this handler is already on the
    allowlist and already trusted to originate a dispatch. The acting dispatcher
    is logged.

    CONFIRMED by Bill 2026-09-06 (security review, row 6): leave allowlist-only.
    Two reasons, the second decisive. (1) Gating on ownership would recreate the
    2026-07-24 failure above. (2) There is NO UI in Dispatch Turbo through which
    a non-owning dispatcher can reach this endpoint — every browser session is
    ephemeral and holds only its own dispatch — so an ownership gate would block
    nothing that is actually reachable while still costing the recovery path.
    Do not re-raise this as a hardening item without a new UI path to point at.

    Request body (JSON):
        title: str  — composed by the dispatcher; "SOSAR - " enforced server-side
        body:  str  — composed by the dispatcher; blank slate, no template (§3.3)

    Returns 200 with a PII-free summary of who it went to.
    """
    _eb_slack_gate_or_raise()

    if _content_length_exceeds(request.headers.get("content-length"), 20_000):
        raise HTTPException(status_code=413, detail="Request body too large")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_title = (payload.get("title") or "").strip()
    raw_body  = (payload.get("body") or "").strip()
    if not raw_title:
        raise HTTPException(status_code=400, detail="Missing title")
    if not raw_body:
        raise HTTPException(status_code=400, detail="Missing body")

    await check_rate_limits(dispatcher.get("email", ""))
    import everbridge as eb_module
    import slack as slack_module     # avoids top-level slack import in this file

    user_email = dispatcher.get("email", "")
    started = time.monotonic()

    # ---- Step 1: Load the incident -----------------------------------------
    db = _get_eb_slack_db()
    snap = db.collection("incidents").document(event_id).get()
    doc = snap.to_dict() if snap.exists else None
    if not doc:
        raise HTTPException(status_code=404, detail="Incident not found")

    everbridge_event_id = doc.get("everbridge_event_id")
    original_nid        = doc.get("notification_id")
    if not everbridge_event_id:
        raise HTTPException(
            status_code=409,
            detail="This incident has no Everbridge event — nothing to follow up on.",
        )
    if not original_nid:
        # Safe-mode draft that was never discovered, or a dispatch that failed
        # before the notification was created. There is no reply roster to derive
        # recipients from, and guessing one is not acceptable for a re-page.
        raise HTTPException(
            status_code=409,
            detail=(
                "This incident has no sent notification yet, so there is no "
                "reply history to derive recipients from."
            ),
        )

    # ---- Step 2: Compose + validate the message ----------------------------
    # Blank slate (§3.3): the dispatcher wrote every character. The only text the
    # server imposes is the county-mandated title prefix.
    notif_title = _enforce_sosar_title_prefix(raw_title)
    prepared = eb_module.prepare_followup_sms_body(raw_body)
    if not prepared["accepted"]:
        budget = prepared["budget"]
        raise HTTPException(
            status_code=422,
            detail=(
                f"Message is {budget['used']} characters; the limit is "
                f"{budget['limit']}. Shorten it by "
                f"{budget['used'] - budget['limit']} characters."
            ),
        )
    notif_body = prepared["text"]

    # ---- Step 3: Derive the recipient set from live Everbridge state -------
    # Fresh read rather than the Firestore cache (§3.2): this is a one-shot
    # dispatcher action that can afford it, and it reflects replies that arrived
    # since the last poll cycle. _parse_poll_response is deliberately untouched.
    #
    # MUST be fetch_notification_raw, NOT poll_notification. The two issue the
    # identical HTTP request but poll_notification returns _parse_poll_response's
    # projection, which consumes and DISCARDS allDetails[] — the very array the
    # partition needs. Passing the parsed shape yields an empty recipient set on
    # every call, silently, and the handler then refuses with a misleading
    # "everyone declined". Caught in review before shipping.
    try:
        poll_body = eb_module.fetch_notification_raw(
            org_id=_EVERBRIDGE_ORG_ID, notification_id=str(original_nid)
        )
    except Exception as e:
        logger.warning(
            "Follow-up recipient read failed: event_id=%s err=%s",
            event_id, type(e).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="Could not read reply status from Everbridge. Try again.",
        )

    buckets = eb_module.partition_contacts_for_followup(poll_body)
    recipients = buckets["followup_contact_ids"]
    if not recipients:
        raise HTTPException(
            status_code=422,
            detail=(
                "No one to send to — everyone on the original notification has "
                "already declined."
            ),
        )

    # ---- Step 4: Mode-aware routing ----------------------------------------
    # Safe mode with any off-safe-list recipient REFUSES rather than drafting.
    # /send-notification drafts in this situation, but a draft is wrong here:
    # #599/#600 established that a hand-send from the Everbridge UI IGNORES
    # target_contact_ids, and the precisely-derived recipient set is the entire
    # point of this feature. A draft would silently page the wrong people —
    # including the decliners this endpoint exists to exclude.
    decision = _route_send(recipients)
    if decision.action != "send_live":
        raise HTTPException(
            status_code=422,
            detail=(
                "Safe mode: one or more recipients are not on the safe list. "
                "A follow-up cannot be drafted for hand-sending, because an "
                "Everbridge-UI send ignores the recipient list. Restrict the "
                "original dispatch to safe-listed individuals and retry."
            ),
        )

    # ---- Step 5: Atomic double-click guard (Cluster B) ---------------------
    # BEFORE the point of no return. Keyed on content so a genuine second
    # correction is allowed while a double-click is not.
    followup_key = _followup_idempotency_key(event_id, notif_title, notif_body)
    try:
        db.collection("followup_sends").document(followup_key).create({
            "event_id":         event_id,
            "dispatcher_email": user_email,
            "recipient_count":  len(recipients),
            "created_at":       datetime.datetime.now(datetime.timezone.utc),
            "status":           "sending",
        })
    except _FirestoreAlreadyExists:
        # A prior attempt exists for this exact content. Distinguish a genuine
        # in-flight double-click from a DEAD tombstone, or the dispatcher is
        # locked out of resending a correction that never actually went.
        #
        # Two dead cases, both real:
        #   - "failed": Everbridge rejected the send. The obvious dispatcher
        #     response is to hit send again with the same wording, which without
        #     this branch would 409 forever.
        #   - "sending" older than the stall window: Cloud Run was preempted
        #     between the .create() and the send. Nothing fired, and nothing ever
        #     will, but the tombstone blocks the retry.
        #
        # A live double-click is "sending" and seconds old, so it still 409s —
        # the Cluster B guarantee is preserved.
        if not _followup_tombstone_is_dead(followup_key):
            raise HTTPException(
                status_code=409,
                detail=(
                    "An identical follow-up is already being sent. If you meant "
                    "to send a second, different correction, change the message."
                ),
            )
        _patch_followup_send_best_effort(followup_key, {
            "status":           "sending",
            "recipient_count":  len(recipients),
            "dispatcher_email": user_email,
            "retried_at":       datetime.datetime.now(datetime.timezone.utc),
        })

    # ---- Step 6: Point of no return — fire the notification ----------------
    try:
        followup_nid = eb_module.send_followup_notification_live(
            org_id=_EVERBRIDGE_ORG_ID,
            event_id=str(everbridge_event_id),
            event_name=doc.get("event_name") or event_id,
            title=notif_title,
            body=notif_body,
            target_contact_ids=recipients,
        )
    except Exception as e:
        _patch_followup_send_best_effort(
            followup_key, {"status": "failed", "error": type(e).__name__}
        )
        logger.error(
            "Follow-up send failed: event_id=%s recipients=%d err=%s",
            event_id, len(recipients), type(e).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="Everbridge rejected the follow-up. Nothing was sent.",
        )

    # ---- Step 7: Persist immediately (Cluster C) ---------------------------
    # ArrayUnion so concurrent/sequential follow-ups accumulate rather than
    # overwrite. Best-effort: the notification has ALREADY fired, so a Firestore
    # failure must not turn a delivered correction into a 500 for the dispatcher.
    from google.cloud import firestore
    _patch_incident_doc_best_effort(
        event_id,
        {"followup_notification_ids": firestore.ArrayUnion([str(followup_nid)])},
        where="followup_notification_id",
    )
    _patch_followup_send_best_effort(
        followup_key, {"status": "sent", "notification_id": str(followup_nid)}
    )

    # ---- Step 8: Courtesy copy into the Slack incident channel -------------
    # Responders in the channel see the same words the Everbridge message
    # carried, without having to find the text. No acknowledgement is asked for
    # here — the YES confirmation belongs to the Everbridge poll (Bill,
    # 2026-07-28).
    #
    # ⚠️ TOP-LEVEL, NEVER THREADED. #660/#661 threaded the routine YES-arrival
    # posts precisely BECAUSE a bot-authored threaded reply notifies nobody
    # (empirically verified on-device — see slack.post_message's docstring).
    # A correction is the exact inverse: it MUST reach people. Tidying this
    # under the pinned welcome would read as an obvious channel-noise
    # improvement and would silently make the correction invisible.
    #
    # NOT gated on _SLACK_MODE == "full". The incident channel and its pinned
    # welcome are created in shadow mode too, and personal-dev — the only
    # sanctioned EB/Slack test environment (Rule #9) — runs shadow. A full-only
    # gate would make this unreachable in the one place it gets tested, which is
    # exactly how #661's predecessor failed its live test on 2026-07-28.
    #
    # Best-effort, and last: the Everbridge notification has already gone out.
    # A Slack failure must never turn a delivered correction into an error for
    # the dispatcher.
    slack_channel_id = doc.get("slack_channel_id")
    slack_courtesy_posted = False
    if slack_channel_id:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                functools.partial(
                    slack_module.post_message,
                    slack_channel_id,
                    f"📣 *Dispatch Follow-Up Message*\n{notif_body}",
                ),
            )
            slack_courtesy_posted = True
        except Exception as e:
            logger.warning(
                "Follow-up Slack courtesy post failed (%s) — the Everbridge "
                "notification WAS sent: event_id=%s nid=%s",
                type(e).__name__, event_id, followup_nid,
            )

    # PII discipline: counts and ids only. No names, no contact ids, no message
    # text — the body is dispatcher-authored free text and may name the subject.
    logger.info(
        "Follow-up sent: event_id=%s nid=%s by=%s recipients=%d "
        "(affirmative=%d not_yet=%d excluded_declined=%d) latency_ms=%d",
        event_id, followup_nid, user_email, len(recipients),
        len(buckets["affirmatively_replied"]), len(buckets["not_yet_replied"]),
        len(buckets["declined"]), int((time.monotonic() - started) * 1000),
    )

    return {
        "status":                 "ok",
        "event_id":               event_id,
        "notification_id":        str(followup_nid),
        "recipient_count":        len(recipients),
        "affirmative_count":      len(buckets["affirmatively_replied"]),
        "not_yet_replied_count":  len(buckets["not_yet_replied"]),
        "excluded_declined":      len(buckets["declined"]),
        "normalized":             prepared["normalized"],
        "truncation_risk":        prepared["truncation_risk"],
        # Surfaced so the dispatcher knows whether the channel copy landed. The
        # Everbridge send is what matters; this is a courtesy, so a False here
        # is information, not an error.
        "slack_courtesy_posted":  slack_courtesy_posted,
        "characters_used":        prepared["budget"]["used"],
        "monitor_url":            eb_module.monitor_active_url(str(followup_nid)),
    }


def _followup_tombstone_is_dead(key: str) -> bool:
    """Read the tombstone and delegate the decision to incidents.py.

    Only the Firestore I/O lives here. The branching logic is in
    `incidents.followup_tombstone_is_dead` so it can be executed under pytest —
    main.py is not importable locally, and a source-reading pin cannot verify
    branching (the first draft of this logic lived here and its mutation test
    came back vacuous).

    A Firestore failure returns False, preserving the 409 and the Cluster B
    double-click guarantee.
    """
    try:
        snap = _get_eb_slack_db().collection("followup_sends").document(key).get()
        doc = snap.to_dict() if snap.exists else None
        return followup_tombstone_is_dead(
            doc, now=datetime.datetime.now(datetime.timezone.utc)
        )
    except Exception as e:
        logger.warning(
            "Follow-up tombstone liveness check failed (%s) — treating as live",
            type(e).__name__,
        )
        return False


def _patch_followup_send_best_effort(key: str, patch: dict) -> None:
    """Update the follow-up tombstone without ever failing the request.

    The tombstone is an audit/idempotency record, not the deliverable. Once the
    Everbridge notification has fired, no Firestore problem should surface to the
    dispatcher as an error — the correction is already on its way.
    """
    try:
        _get_eb_slack_db().collection("followup_sends").document(key).update(patch)
    except Exception as e:
        logger.warning(
            "Follow-up tombstone patch failed (%s) — fields: %s",
            type(e).__name__, list(patch.keys()),
        )


@app.post("/confirm-draft-sent/{event_id}")
async def confirm_draft_sent(
    event_id: str,
    request: Request,
    dispatcher: dict = Depends(require_authorized_dispatcher),
):
    """Phase 0 Task 9 manual fallback for safe-mode discovery failure.

    Accepts a dispatcher-supplied notification_id (extracted from the
    Everbridge UI URL bar) when the auto-discovery polling chain has
    timed out — i.e. the dispatcher pressed Send but skipped the
    "Include in event" checkbox + picker steps that would have linked
    the notification automatically.

    Side-effects on success:
      1. Patch the Firestore incident doc with the supplied
         notification_id, set status='polling', record
         manually_confirmed_at timestamp.
      2. Edit the #active-incidents tally header from
         "📋 Awaiting dispatcher send" → "🔔 Everbridge ACTIVE".
      3. The next /poll-incident cycle (Task 1.11) picks up the
         notification_id and switches from pre-discovery to standard
         post-discovery polling.

    Best-effort verification: we call everbridge.poll_notification once
    to confirm the notification_id is real. If the API doesn't return
    the notification (transient error, dispatcher typo, wrong
    notification entirely), we LOG a warning and trust the dispatcher
    anyway — refusing to record their input would block them in the
    exact failure mode this endpoint is designed to recover from.

    Request body (JSON):
        {"notification_id": str}

    Returns:
        {"status": "ok", "notification_id": str}
    """
    _eb_slack_gate_or_raise()
    await check_rate_limits(dispatcher.get("email", ""))

    # ---- Body parse -------------------------------------------------------
    # Body is a single small JSON object (one notification_id string) — cap
    # at 1 KB to deny garbage payloads cheaply before reading into memory.
    if _content_length_exceeds(request.headers.get("content-length"), 1_000):
        raise HTTPException(status_code=413, detail="Request body too large")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    nid = (body.get("notification_id") or "").strip()
    if not nid:
        raise HTTPException(status_code=400, detail="Missing notification_id")

    user_email = dispatcher.get("email", "")

    # ---- Load + validate the incident doc --------------------------------
    db = _get_eb_slack_db()
    doc_ref = db.collection("incidents").document(event_id)
    snap = doc_ref.get()
    doc = snap.to_dict() if snap.exists else None

    err = _validate_confirm_draft_sent(doc, user_email)
    if err:
        raise HTTPException(status_code=err[0], detail=err[1])

    # ---- Best-effort verification (Phase 0 Task 9) ------------------------
    # Trust the dispatcher even if Everbridge doesn't return the notification:
    # this endpoint exists precisely because Everbridge's auto-linkage failed,
    # and we'd rather record a slightly-wrong nid (recoverable — dispatcher
    # can correct via a second call) than refuse to record at all.
    import everbridge as eb_module
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            functools.partial(
                eb_module.poll_notification,
                org_id=_EVERBRIDGE_ORG_ID,
                notification_id=nid,
            ),
        )
    except Exception as e:
        logger.warning(
            "Manual-confirm: notification %s not retrievable (%s); trusting dispatcher",
            nid, type(e).__name__,
        )

    # ---- Patch incident doc ----------------------------------------------
    from incidents import now_utc
    confirmed_at = now_utc()
    doc_ref.update({
        "notification_id":       nid,
        "status":                "polling",
        "manually_confirmed_at": confirmed_at,
    })

    # ---- Edit the #active-incidents tally header to ACTIVE ---------------
    # Re-read the doc so the tally reflects the just-set notification_id +
    # any responder updates that happened between the .get() and the
    # .update() (small race window — re-read keeps rendering authoritative).
    doc = doc_ref.get().to_dict() or doc
    active_incidents_channel_id = os.environ.get("ACTIVE_INCIDENTS_CHANNEL_ID", "")
    active_incidents_ts = doc.get("active_incidents_ts", "")
    if active_incidents_channel_id and active_incidents_ts:
        import slack as slack_module
        try:
            await loop.run_in_executor(
                None,
                functools.partial(
                    slack_module.edit_message,
                    active_incidents_channel_id,
                    active_incidents_ts,
                    _compose_active_incidents_tally(doc, "🔔 Everbridge ACTIVE"),
                ),
            )
        except Exception as e:
            # The Firestore patch already succeeded; a Slack edit failure here
            # is recoverable (next /poll-incident cycle will edit the tally
            # again on the first responder change). Log + proceed rather than
            # failing the request.
            logger.warning(
                "Manual-confirm: tally edit failed (%s); doc patched, polling chain will recover on next cycle",
                type(e).__name__,
            )
    else:
        logger.warning(
            "Manual-confirm: ACTIVE_INCIDENTS_CHANNEL_ID or active_incidents_ts unset — tally not edited"
        )

    # ---- Audit log (no PII — dispatcher email logged for ownership trail) -
    logger.info(
        "Manual confirm: event_id=%s notif=%s by=%s",
        event_id, nid, user_email,
    )
    return {"status": "ok", "notification_id": nid}


# ---------------------------------------------------------------------------
# /poll-incident polling-chain helpers + endpoints (Task 1.11)
#
# /poll-incident/{event_id} and /delete-template/{template_id} are invoked by
# Cloud Tasks (NOT the dispatcher's browser). They use _verify_oidc_request
# instead of require_authorized_dispatcher, and they re-enqueue themselves
# (poll-incident only — template-delete fires once and exits).
#
# The polling loop has 5 stop conditions per design Section 4 §6:
#   - notificationStatus in {"Completed", "Stopped"} → terminal (Phase 0 Task 8)
#   - manual_stop_requested flag set on the doc      → "manual"
#   - 4 hour hard cap from created_at                 → "hard_cap"
#   - 60 min idle (no new responder)                  → "idle"
#   - errors-since-first-error exceeds mode tolerance → "error"
# Plus a 30 min pre-discovery timeout for safe-mode draft path → "draft_unsent"
# ---------------------------------------------------------------------------

def _render_terminal_header(stop_reason: str) -> str:
    """Map a stop_reason to its #active-incidents tally header string.

    Each reason gets a distinct phrase so the dispatcher (and search
    management watching #active-incidents) can tell at a glance what
    happened. Mirror-tested.
    """
    phrases = {
        "everbridge_closed": "Completed",
        "stopped":           "Stopped (manual EB UI)",
        "manual":            "Stopped (manual close)",
        "idle":              "idle (no new responders)",
        "hard_cap":          "4-hour cap reached",
        "error":             "polling errors exceeded tolerance",
        "draft_unsent":      "draft expired without send",
    }
    return f"⏹ Everbridge STOPPED — {phrases.get(stop_reason, stop_reason)}"


def _check_stop_conditions(
    *,
    doc: dict,
    poll_response: dict | None,
    mode_tolerance_s: int,
    now_dt: datetime.datetime,
) -> str | None:
    """Return a stop_reason string if any of the 5 stop conditions fired,
    or None if polling should continue.

    Order matters — each condition is checked in priority order:
      1. notificationStatus == Completed — EB notification ran to natural
         completion (everyone confirmed / template policy fully fired).
         Distinct from a manual close; no co-occurring "manual" intent.
      2. manual_stop_requested — dispatcher pressed close. Wins over
         notificationStatus == "Stopped" because our /close-incident-polling
         endpoint sets BOTH (it PUTs EB to Stopped AND sets the flag); the
         flag carries the truer "we initiated this" signal. A bare "Stopped"
         with no flag still maps to "stopped" (someone hit Stop in the EB UI
         directly).
      3. notificationStatus == Stopped — someone stopped the EB notification
         outside our app (manual EB UI action).
      4. hard_cap — 4 hours elapsed; bound runaway chains.
      5. idle — 60 min with no new responder; conclude an effectively-over
         incident.
      6. error — first_error_at + tolerance exceeded; mode-dependent
         (10 min full / 30 min safe; passed in as mode_tolerance_s).

    Mirror-tested.
    """
    notif_status = ""
    if poll_response is not None:
        notif_status = (poll_response.get("notif_status") or "")

    # 1. Everbridge ran to natural Completion — distinct from a manual close.
    if notif_status == "Completed":
        return "everbridge_closed"

    # 2. Manual close requested by dispatcher (wins over EB-state "Stopped"
    #    because /close-incident-polling sets BOTH flag and EB state).
    if doc.get("manual_stop_requested"):
        return "manual"

    # 3. Notification stopped externally (manual EB UI action, no flag).
    if notif_status == "Stopped":
        return "stopped"

    # 3. Hard cap (4 hours from created_at)
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime.datetime):
        if (now_dt - created_at).total_seconds() >= _HARD_CAP_S:
            return "hard_cap"

    # 4. Idle stop (60 min since last_responder_at; falls back to
    #    created_at if no responder ever arrived).
    last_resp = doc.get("last_responder_at") or created_at
    if isinstance(last_resp, datetime.datetime):
        if (now_dt - last_resp).total_seconds() >= _IDLE_S:
            return "idle"

    # 5. Error tolerance (mode-dependent).
    first_err = doc.get("first_error_at")
    if isinstance(first_err, datetime.datetime):
        if (now_dt - first_err).total_seconds() >= mode_tolerance_s:
            return "error"

    return None


# Mirror of incidents.py constants (kept here so _check_stop_conditions stays pure
# without an incidents.py import — same pattern as the test mirror).
# Pinned by test_main_regression.py — change here AND in incidents.py + test_poll_incident.py.
_HARD_CAP_S = 4 * 60 * 60
_IDLE_S     = 60 * 60


def _norm_for_email_match(s: str) -> str:
    """Lowercase + strip non-alphanumeric for tolerant local-part comparison.

    Used by _resolve_email_from_safe_list to match an ack contact's name
    against an email's local-part regardless of separator style:
      "Bill Burns" + "bill.burns" / "bill_burns" / "billburns" / "bburns"
    all normalize to substring-comparable forms.
    """
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _resolve_email_from_safe_list(ack: dict, safe_list_emails: list) -> list:
    """Last-resort email match: pair an ack contact to a safe-list email by name.

    Used by _apply_responder_diff when both poll-response ack.emails and the
    contact_email_map snapshot are empty for a contact (e.g., EB record has
    no Email-* paths configured, or per-contact email lookup raised and was
    swallowed). The configured Slack safe-list (allowed_emails) is the
    authoritative source for valid Slack-lookup emails (per team policy:
    @sccssar.org plus a SAR coordinator sheriff-dept exception), so a
    name-match into that list is both safe and operationally useful.

    Matching rules:
      1. Both first AND last name present in local-part → strongest signal;
         if exactly one such match, return it.
      2. Otherwise, if exactly one safe-list email's local-part contains the
         last name, return it (last-only match).
      3. Otherwise, if exactly one safe-list email's local-part contains the
         first name, return it (first-only match — typical for the "Email-Work
         Alt" slot where local-part is just the first name, e.g. bill@...).
      4. Any ambiguity (multiple matches at any tier) → return [] rather
         than guess. The responder is still posted to Slack via the existing
         format_would_invite "Cannot invite" path.

    Local-part comparison strips non-alphanumeric and lowercases, so a team's
    multi-email-per-contact slots (Email-Personal, Email-Work, Email-Work Alt)
    are all eligible regardless of separator style.
    """
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


def _apply_responder_diff(
    prev_responders: list[dict],
    new_ack_contacts: list[dict],
    contact_group_map: "dict[str, list[str]] | None" = None,
    contact_email_map: "dict[str, list[str]] | None" = None,
    safe_list_emails: "list | None" = None,
) -> tuple[list[dict], list[dict]]:
    """Compute the new full responder list + the list of new arrivals.

    Args:
      prev_responders: doc["responders"] (or last_non_empty_responders) —
        normalized list of {contact_id, name, groups, ...}
      new_ack_contacts: poll_response["ack_contacts"] from
        everbridge.poll_notification — {contact_id, first_name, last_name, emails}
      contact_group_map: contact_id → [group_names], built at send time from
        list_group_member_contacts() calls for each targeted group.  Empty (or
        None) for direct-contact-only sends — groups will be [] for all entries.
      contact_email_map: contact_id → [emails], built at send time from contact
        records (where paths.value is always populated).  Used as fallback when
        ack.emails is empty — EB only includes callResultByPaths for paths it
        actually attempted, so a contact who answers SMS before EB tries their
        email path may have an empty ack.emails list.
      safe_list_emails: configured Slack allowlist (safe_list.allowed_emails) —
        third-tier fallback when both ack.emails and contact_email_map[cid]
        are empty for a contact whose EB record has no Email-* delivery paths.
        Match is by name (see _resolve_email_from_safe_list).

    Returns:
      (next_full_list, new_arrivals)

    Identity is via contact_id. The "new arrivals" list lets the polling
    handler trigger per-responder Slack actions (full mode: invite + post
    "Burns added"; shadow mode: post "Would invite: Burns").

    Mirror-tested.
    """
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
        # Shape the entry to match what the tally composer expects.
        # emails: prefer poll-response (ack.emails) — use Firestore snapshot
        # (cem) as fallback when callResultByPaths didn't include an email
        # path. Last resort: name-match against the configured Slack safe-list
        # so unit-leader-class contacts (like Kris Black) whose EB record has
        # no Email-* delivery paths still resolve to "Would invite" instead of
        # "Cannot invite" in shadow mode and a real invite in full mode.
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


def _shape_responder_name(ack: dict) -> str:
    """Build a 'Last, First' display string from an ack contact dict.

    Falls back to whatever's available — never raises on partial data.
    """
    last  = (ack.get("last_name")  or "").strip()
    first = (ack.get("first_name") or "").strip()
    if last and first:
        return f"{last}, {first}"
    if last:
        return last
    if first:
        return first
    return ack.get("contact_id", "?")


def _partition_arrivals_for_shadow(
    new_arrivals: "list[dict]",
    safe_list_emails: "list[str] | None",
) -> "tuple[list[str], list[str], list[str]]":
    """Three-way partition for shadow-mode YES classification.

    Returns (already_member_names, resolvable_names, unresolvable_names).

    A responder is 'already_member' if any of their resolved emails matches
    a safe-list email. NOTE (#581): safe-list is a PROXY for "pre-invited at
    send-time" — accurate before #580, but since #580 the send-time invite
    set is dispatcher + SO coord + @active_incident_management members (NOT
    safe-list). So in shadow mode this proxy can now mislabel: a safe-list
    responder not in the group is falsely "added", and a group member not on
    the safe-list is falsely "Would invite" when they were actually invited.
    Shadow mode is personal-dev only, so the impact is a cosmetic transcript
    label; the real fix (classify against the actual send-time set) is #581.

    'resolvable' = has at least one email but no safe-list match.
    'unresolvable' = no emails at all (ack, contact_email_map, and
    safe-list name-match all came up empty).

    Match is case-insensitive on the full email; whitespace stripped.
    Mirror-tested in test_main_regression.py.
    """
    safe_set = {e.lower().strip() for e in (safe_list_emails or []) if e}
    already_member_names: list[str] = []
    resolvable_names:     list[str] = []
    unresolvable_names:   list[str] = []
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


@app.post("/delete-template/{template_id}")
async def delete_template(
    template_id: str,
    request: Request,
):
    """Cloud Tasks-driven endpoint: unconditionally delete a notification template.

    Authenticated by OIDC (Cloud Tasks), NOT dispatcher Google ID.
    Templates are audit-safe per Phase 0; deletion fires +30min from
    template creation regardless of whether the dispatcher pressed Send.

    Returns 204 on success / 200 on already-deleted / 5xx on EB error.
    The Cloud Tasks queue retry policy handles transient failures.
    """
    _eb_slack_gate_or_raise()
    _verify_oidc_request(
        request,
        expected_email=_expected_poll_sa_email(),
        expected_audience=_expected_oidc_audience(),
    )

    import everbridge as eb_module
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            functools.partial(
                eb_module.delete_notification_template,
                org_id=_EVERBRIDGE_ORG_ID,
                template_id=template_id,
            ),
        )
    except Exception as e:
        # 404 from EB → already deleted; treat as success (idempotent).
        msg = str(e).lower()
        if "404" in msg or "not found" in msg:
            logger.info("Template %s already deleted (idempotent)", template_id)
            return JSONResponse({"status": "already_deleted"}, status_code=200)
        # Other errors: log + propagate so Cloud Tasks retries.
        logger.error(
            "Template delete failed (will be retried by Cloud Tasks): template_id=%s err=%s",
            template_id, type(e).__name__,
        )
        raise HTTPException(status_code=502, detail="Template delete failed")

    logger.info("Template deleted: template_id=%s", template_id)
    return JSONResponse({"status": "ok"}, status_code=200)


# ===========================================================================
# d4h_status state machine helper (per-YES path only — Selective mode)
# ===========================================================================
# Selective-mode (fullTeam: false) collapses the prior bulk-ABSENT state
# machine: dispatch-time produces terminal "done" or "partial" directly
# (see /send-notification step 10.5). Only the per-YES worker can downgrade
# at runtime when a responder sync raises.
#
# State values:
#   "pending" 🔵 = work in progress, outcome unknown
#   "done"    🟢 = incident created cleanly (frontend: 🟢)
#   "partial" 🟡 = incident created but some sub-step failed
#                  (frontend: 🔴 under simplified indicator scheme)
#   "failed"  🔴 = incident creation itself failed (no D4H record at all)
#
# Invariants:
#   - "failed" is absorbing — set only at create-incident; never overwritten.
#   - Per-YES worker can only downgrade (done→partial).
#
# Mirror: backend/test_main_regression.py::TestD4HStatusStateMachine.
# ===========================================================================

def _compute_d4h_status_after_per_yes_failure(current: str) -> str:
    """Return the new d4h_status when a per-YES POST fails. Pure-logic.
    Demotes done/pending → partial. 'partial' and 'failed' are absorbing."""
    if current in ("pending", "done"):
        return "partial"
    return current


@app.post("/d4h-sync-yes")
async def d4h_sync_yes(request: Request):
    """Cloud Tasks worker — per-YES D4H attendance sync for one EB responder.

    Authenticated by OIDC (Cloud Tasks Service Account), NOT dispatcher
    Google ID. Mirrors the auth pattern used by /poll-incident.

    Called by the polling loop (/poll-incident) once per new YES-reply arrival
    that has a resolvable email. Enqueued by d4h.enqueue_per_yes_sync().

    Payload (POST body JSON):
      event_id:     str        — incidents/{event_id} for Firestore lookups
      member_email: str        — responder's @sccssar.org email
      eb_groups:    list[str]  — EB group names the responder belongs to

    Returns:
      200 {"ok": True}                  on success or graceful-degrade
      400 on missing/invalid payload
      502 on transient D4H error (triggers Cloud Tasks retry, max_attempts=5)

    Side effects:
      - Calls d4h.handle_per_yes_sync_task(event_id, member_email, eb_groups)
        which: (1) loads d4h_activity_id from Firestore, (2) POSTs a new
        ATTENDING attendance record for the responder under Selective mode
        (`fullTeam: false` — attendance starts empty; no PATCH path), (3) if
        EB Canine group was dispatched, invokes the K9 sync stub (v1.1 no-op
        until D4H ships /handlers + /animal-attendances per Dan Doyle 2026-05-13).
        Drone-attach is NOT in this path — it moved to dispatch-time
        (`create_incident_with_subject`) on 2026-05-19 to eliminate the
        per-YES fan-out and idempotency race.

    Milestone-only contract (PR 7.1):
      This worker MUST NOT write to incidents/{event_id}.d4h_event_log.
      Per-YES events are high-volume and would drown out the four
      dispatch-time milestones in the dispatcher textarea. Per-YES debug
      visibility lives in Cloud Run structured logs (logger.info /
      logger.warning). Pinned by TestD4HPerYesMilestoneOnlyContract in
      test_main_regression.py.
    """
    _eb_slack_gate_or_raise()
    _verify_oidc_request(
        request,
        expected_email=_expected_poll_sa_email(),
        expected_audience=_expected_oidc_audience(),
    )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_id     = body.get("event_id") or ""
    member_email = body.get("member_email") or ""
    eb_groups    = body.get("eb_groups") or []
    if not member_email:
        raise HTTPException(status_code=400, detail="member_email required")
    # Pre-Cluster-D, a malformed Cloud Tasks payload with empty event_id
    # silently no-op'd inside handle_per_yes_sync_task ("graceful-degrade —
    # no activity_id in Firestore for event_id=") — indistinguishable from
    # the legitimate post-dispatch-D4H-failure case. Surfacing 400 makes
    # an enqueue bug visible in Cloud Run logs as 400/non-retriable rather
    # than burying it under a normal-looking 200.
    if not event_id:
        raise HTTPException(status_code=400, detail="event_id required")

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            functools.partial(
                d4h.handle_per_yes_sync_task, event_id, member_email, eb_groups,
            ),
        )
    except Exception as exc:
        # PR 7.2: include the exception message so failures like
        # ModuleNotFoundError surface the missing module name in logs
        # (pre-fix logged only type(exc).__name__, which masked an
        # import-path bug for ~weeks).
        # PR-A.6: D4H exceptions carry a ' | body: ...' suffix that can
        # echo back member PII from the failing request — route through
        # _sanitize_d4h_error which strips the body. For non-D4H
        # exceptions the sanitizer is a no-op (no body suffix to find),
        # so ModuleNotFoundError + network errors still surface fully.
        logger.warning(
            "D4H per-YES sync raised %s for event_id=%s: %s",
            type(exc).__name__, event_id, _sanitize_d4h_error(exc)[:500],
        )
        # PR 7.2: demote d4h_status to "partial" so the dispatcher indicator
        # flips 🟢 → 🟡. Best-effort write (doc may be gone, Firestore may be
        # transiently unreachable). NEVER masks the underlying error — we
        # still raise 502 so Cloud Tasks retries per max_attempts=5.
        if event_id:
            try:
                db = _get_eb_slack_db()
                doc_ref = db.collection("incidents").document(event_id)
                snap = doc_ref.get()
                if snap.exists:
                    doc = snap.to_dict() or {}
                    current_status = doc.get("d4h_status", "pending") or "pending"
                    new_status = _compute_d4h_status_after_per_yes_failure(current_status)
                    if new_status != current_status:
                        update_fields = {"d4h_status": new_status}
                        # Preserve any pre-existing d4h_error (bulk-ABSENT
                        # may have set one); only set ours if blank.
                        if not doc.get("d4h_error"):
                            update_fields["d4h_error"] = (
                                f"D4H per-YES sync failed (event_id={event_id}); "
                                f"see Cloud Run logs for traceId"
                            )
                        doc_ref.update(update_fields)
            except Exception as _fs_exc:
                # Don't let a Firestore write failure shadow the original exc.
                logger.warning(
                    "d4h_status downgrade write failed (non-fatal) for event_id=%s: %s",
                    event_id, type(_fs_exc).__name__,
                )
        # Cluster D — route Cloud Tasks retry semantics by exception type:
        #   D4HClientError (4xx) → 200 = terminal-failed; Cloud Tasks stops
        #     retrying since the same payload would 400/404/etc. on every
        #     attempt. Dispatcher already sees the failure via the d4h_status
        #     downgrade above. Pre-fix this triggered 5 doomed retries before
        #     dead-lettering, polluting logs and slowing real-failure surfacing.
        #   D4HServerError (5xx/transport) and anything else (ModuleNotFoundError,
        #     unexpected ValueError, etc.) → 502 = retry per max_attempts=5.
        #     If persistent, retries fail identically and the task is dead-lettered.
        if isinstance(exc, d4h.D4HClientError):
            return JSONResponse(
                {"ok": False, "category": "non_retriable_client_error"},
                status_code=200,
            )
        raise HTTPException(status_code=502, detail="D4H per-YES sync error")

    logger.info(
        "D4H per-YES sync done | event_id=%s",
        event_id,
    )
    return JSONResponse({"ok": True}, status_code=200)


@app.post("/poll-incident/{event_id}")
async def poll_incident(
    event_id: str,
    request: Request,
):
    """Cloud Tasks-driven polling cycle for an incident.

    One cycle does:
      1. OIDC validation
      2. Load incident doc; bail if not found or already terminal
      3. Branch on (notification_id, template_id):
         - live / post-discovery: poll_notification (?verbose=true)
         - safe-mode pre-discovery: discover_notification_by_event
         - safe-mode post-manual-confirm: poll_notification
      4. Pre-discovery transition: on totalCount=1, set notification_id +
         flip tally header to ACTIVE; on 30min timeout → stop_incident('draft_unsent')
      5. Apply Phase 0 Task 8 last-non-empty cache to the responders list
      6. Diff responders against prev list — _apply_responder_diff
      7. Per-responder Slack action by slack_mode:
         - full: invite_user + post "<Name> added"
         - shadow: post format_would_invite_message
      8. Recompose + edit #active-incidents tally
      9. Check 5 stop conditions; if terminal:
         - stop_incident(reason); edit tally header to STOPPED; return 200
      10. Else: enqueue next cycle (15s in both modes per Item 2)

    Returns 200 on success regardless of whether the chain continues —
    Cloud Tasks treats anything in 2xx as "task completed". A 5xx triggers
    Cloud Tasks retry, which is what we want for transient failures.
    """
    _eb_slack_gate_or_raise()
    _verify_oidc_request(
        request,
        expected_email=_expected_poll_sa_email(),
        expected_audience=_expected_oidc_audience(),
    )

    import everbridge as eb_module
    import slack as slack_module
    from incidents import (
        FULL_MODE_ERROR_TOLERANCE_S, SAFE_MODE_ERROR_TOLERANCE_S,
        FULL_MODE_POLL_INTERVAL_S,   SAFE_MODE_POLL_INTERVAL_S,
        now_utc, stop_incident,
    )
    loop = asyncio.get_running_loop()

    db = _get_eb_slack_db()
    doc_ref = db.collection("incidents").document(event_id)
    snap = doc_ref.get()
    doc = snap.to_dict() if snap.exists else None
    if not doc:
        logger.warning("Poll-incident: doc not found event_id=%s — chain ends", event_id)
        return JSONResponse({"status": "doc_not_found"}, status_code=200)
    if isinstance(doc.get("status"), str) and doc["status"].startswith("stopped_"):
        logger.info("Poll-incident: doc already terminal status=%s — chain ends", doc["status"])
        return JSONResponse({"status": "already_stopped"}, status_code=200)

    # ---- Mode-derived constants -------------------------------------------
    mode = (doc.get("everbridge_mode") or "").lower()
    if mode == "full":
        tolerance_s    = FULL_MODE_ERROR_TOLERANCE_S
        poll_interval  = FULL_MODE_POLL_INTERVAL_S
    else:
        tolerance_s    = SAFE_MODE_ERROR_TOLERANCE_S
        poll_interval  = SAFE_MODE_POLL_INTERVAL_S

    # ---- Branch: pre-discovery vs post-discovery --------------------------
    notification_id     = doc.get("notification_id")
    everbridge_event_id = doc.get("everbridge_event_id", "")
    poll_response: dict | None = None
    pre_discovery_just_resolved = False

    if not notification_id and doc.get("template_id"):
        # Safe-mode pre-discovery — try to discover the dispatcher-sent notification
        try:
            discovered = await loop.run_in_executor(
                None,
                functools.partial(
                    eb_module.discover_notification_by_event,
                    org_id=_EVERBRIDGE_ORG_ID,
                    event_id=everbridge_event_id,
                ),
            )
        except Exception as e:
            logger.warning(
                "Poll-incident: discover failed event_id=%s err=%s",
                event_id, type(e).__name__,
            )
            discovered = None

        if discovered:
            notification_id = discovered
            pre_discovery_just_resolved = True
            doc_ref.update({"notification_id": notification_id, "status": "polling"})
            doc["notification_id"] = notification_id
            doc["status"] = "polling"
            logger.info(
                "Poll-incident: pre-discovery resolved event_id=%s nid=%s",
                event_id, notification_id,
            )
        else:
            # No match yet — check 30 min pre-discovery timeout.
            created_at = doc.get("created_at")
            if isinstance(created_at, datetime.datetime):
                elapsed = (now_utc() - created_at).total_seconds()
                if elapsed >= 30 * 60:
                    stop_incident(doc, "draft_unsent")
                    doc_ref.update({
                        "status":      doc["status"],
                        "stop_reason": doc["stop_reason"],
                        "expire_at":   doc["expire_at"],
                    })
                    await _edit_tally(loop, slack_module, doc,
                                      _render_terminal_header("draft_unsent"))
                    logger.info(
                        "Poll-incident: pre-discovery timeout event_id=%s — chain ends",
                        event_id,
                    )
                    return JSONResponse({"status": "stopped_draft_unsent"}, status_code=200)
            # Continue: enqueue next pre-discovery cycle
            doc_ref.update({"last_poll_at": now_utc()})
            _enqueue_poll_task(event_id, delay_s=poll_interval, mode="pre_discovery")
            return JSONResponse({"status": "pre_discovery_continuing"}, status_code=200)

    # At this point notification_id is known (either present from the start or
    # just discovered).  Run the standard post-discovery poll.
    if notification_id:
        try:
            poll_response = await loop.run_in_executor(
                None,
                functools.partial(
                    eb_module.poll_notification,
                    org_id=_EVERBRIDGE_ORG_ID,
                    notification_id=notification_id,
                ),
            )
        except Exception as e:
            logger.warning(
                "Poll-incident: poll failed event_id=%s err=%s",
                event_id, type(e).__name__,
            )
            # Mark first_error_at if not set, then continue to stop-condition check
            if doc.get("first_error_at") is None:
                doc_ref.update({"first_error_at": now_utc()})
                doc["first_error_at"] = now_utc()
            poll_response = None

    # ---- Apply Phase 0 Task 8 last-non-empty cache ------------------------
    new_arrivals: list[dict] = []
    counts_changed = False   # decline/no-response tally counts moved this cycle (#592)
    # Hoist safe_list_emails so the post-arrival shadow-mode classifier below
    # can reference it unconditionally (it partitions arrivals by safe-list
    # match: match → "<Name> added", others → "Would invite: <Name>" or
    # "Cannot invite: <Name>"). Safe-list is a proxy for send-time membership
    # that drifted post-#580 — see _partition_arrivals_for_shadow + #581.
    # @lru_cache makes the load essentially free.
    safe_list_emails: list[str] = _load_safe_list_secret().get("allowed_emails", [])
    if poll_response is not None:
        if not poll_response.get("all_details_empty", False):
            # Non-empty cycle — diff against prev responders. The safe-list
            # is also passed into _apply_responder_diff so name-based
            # safe-list email fallback can rescue contacts with no EB email
            # paths (the contact-direct gap left after PR #322).
            prev_responders = doc.get("last_non_empty_responders") or doc.get("responders") or []
            next_responders, new_arrivals = _apply_responder_diff(
                prev_responders,
                poll_response.get("ack_contacts", []),
                doc.get("contact_group_map") or {},
                doc.get("contact_email_map") or {},
                safe_list_emails,
            )
            update: dict = {}
            if next_responders != prev_responders:
                update["responders"] = next_responders
                update["last_non_empty_responders"] = next_responders
                if new_arrivals:
                    update["last_responder_at"] = now_utc()
                doc["responders"] = next_responders
                doc["last_non_empty_responders"] = next_responders
            # Persist the decline / no-response tally counts (#592). These move
            # independently of the YES responder list — in the La Verne MA
            # callout 50 NOs streamed in behind a single early YES with no
            # further YES arrivals — so they must be written (and re-trigger the
            # tally recompose below via counts_changed) even when
            # next_responders == prev_responders. Written only on non-empty
            # cycles, so the last good value survives the natural-expiry empty
            # cycles in Firestore (same caching as responders).
            new_decline = poll_response.get("decline_count", 0)
            new_no_response = poll_response.get("no_response_count", 0)
            if (new_decline != doc.get("decline_count", 0)
                    or new_no_response != doc.get("no_response_count", 0)):
                counts_changed = True
                update["decline_count"] = new_decline
                update["no_response_count"] = new_no_response
                doc["decline_count"] = new_decline
                doc["no_response_count"] = new_no_response
            if update:
                doc_ref.update(update)

    # ---- Per-responder Slack action (only on new arrivals) ----------------
    slack_mode = (doc.get("slack_mode") or "").lower()
    slack_channel_id = doc.get("slack_channel_id", "")
    if new_arrivals and slack_channel_id:
        if slack_mode == "full":
            for ack in new_arrivals:
                # Cluster F (Slack-M8): try every email in ack['emails'] until
                # one resolves to a Slack uid. Pre-fix only emails[0] was tried;
                # if a responder's personal email is at index 0 but their
                # @sccssar.org email is at index 1, lookup returned None and
                # they were silently NOT invited despite an account existing.
                # contact_email_map already populates ALL emails per contact.
                uid: str | None = None
                emails_to_try = [e for e in (ack.get("emails") or []) if e]
                # Batch-3 PR-G.5: track whether ANY lookup raised across the
                # loop. The end-state ERROR below distinguishes "all lookups
                # raised → transient skip, responder silently dropped" from
                # "all lookups returned None → user genuinely not in Slack"
                # (the legitimate case for non-safe-list responders in full
                # mode; never an ERROR). Per Bill 2026-05-30: keep the
                # per-email continue (the original self-heal-on-next-cycle
                # design is correct), but make the transient-skip end-state
                # visible to ops scans instead of swallowed.
                any_lookup_raised = False
                for email in emails_to_try:
                    try:
                        uid = await loop.run_in_executor(
                            None,
                            functools.partial(slack_module.lookup_user_by_email, email),
                        )
                    except Exception as e:
                        any_lookup_raised = True
                        logger.warning(
                            "Poll-incident: lookup_user_by_email failed (%s) for contact_id=%s — trying next email if any",
                            type(e).__name__, ack.get("contact_id", "?"),
                        )
                        continue
                    if uid:
                        break
                # G.5: if every lookup raised AND no uid resolved, the
                # responder is silently dropped this cycle. Log at ERROR
                # (not WARNING) with a structured event tag so ops scans
                # surface it. The polling chain self-heals — the next 15s
                # cycle will see the responder as a new arrival again and
                # re-attempt the lookup. If transient (rate limit, auth
                # blip), it clears within 1-2 cycles. If persistent, the
                # ERROR repeats per cycle and is visible as a recurring
                # signal in ops scans. Failure-mode rubric Q5.
                if uid is None and any_lookup_raised:
                    logger.error(
                        "Poll-incident: slack_lookup_transient_skip — all "
                        "email lookups raised for contact_id=%s; responder "
                        "NOT invited this cycle (self-heal: next polling "
                        "cycle will re-attempt)",
                        ack.get("contact_id", "?"),
                    )
                # `invited` MUST mean "responder is in the channel as a result
                # of this call" — it gates the '<Name> added' line below, which
                # used to be posted unconditionally and so gave the dispatcher a
                # false confirmation for responders who were never invited
                # (found 2026-07-19). invite_user is idempotent — it swallows
                # already_in_channel / cant_invite_self — so a responder
                # pre-added at send time via @active_incident_management returns
                # cleanly here and still correctly reports 'added'.
                invited = False
                if uid:
                    try:
                        await loop.run_in_executor(
                            None,
                            functools.partial(slack_module.invite_user, slack_channel_id, uid),
                        )
                        invited = True
                    except Exception as e:
                        logger.warning(
                            "Poll-incident: invite_user failed (%s) for contact_id=%s — continuing",
                            type(e).__name__, ack.get("contact_id", "?"),
                        )
                # Cluster F (Slack-M9): convert stored 'Last, First' to display
                # 'First Last' for the "<Name> added" line. Shadow-mode already
                # does this via format_would_invite_message; full-mode was
                # posting the raw sortable form. A channel that operated in
                # shadow then switched to full after an EVERBRIDGE_MODE flip
                # would otherwise show inconsistent name formats in one timeline.
                # G.LOW: was slack_module._to_conversational_name (private API
                # access). Renamed to public to_conversational_name as part of
                # batch-3 cleanups — Melanie's review noted that reaching into
                # private names risks silent breakage if slack.py renames the
                # function for any reason. See test_slack.py mirror.
                display_name = slack_module.to_conversational_name(ack.get("name") or "")
                if invited:
                    arrival_msg = f"{display_name} added"
                    # Routine confirmations go in a THREAD under the pinned
                    # welcome; only failures earn a top-level post. A 20-YES
                    # callout previously fired 20 top-level messages into a
                    # 24-member channel, and because most responders VIP the
                    # bot those arrived as iOS "Time Sensitive" alerts that
                    # pierce Do Not Disturb (2026-07-20 Hale Avenue: four
                    # mobile screens of them). Threaded replies notify only
                    # the thread starter (the BOT — it authors the welcome),
                    # repliers, @-mentions, and "Follow every thread" opt-ins;
                    # the arrival text is plain, so none apply. Verified on a
                    # real device 2026-07-28 — see post_message's docstring.
                    #
                    # Fall back to a top-level post when welcome_ts is absent
                    # (the welcome post failed): losing the notification is
                    # acceptable, losing the responder's name is not.
                    arrival_thread_ts = doc.get("welcome_ts") or None
                else:
                    # Failures stay TOP-LEVEL and keep notifying — each one
                    # needs a dispatcher to act out of band, and burying them
                    # in a thread nobody is subscribed to is exactly the
                    # outcome this must not produce. They are also rare, so
                    # they cost the channel almost nothing.
                    arrival_thread_ts = None
                    # Name the cause — each has a different dispatcher remedy.
                    # The zero-email branch logs here because the G.5 ERROR
                    # above CANNOT fire for it: no lookup was attempted, so
                    # any_lookup_raised stays False. Before this, that path had
                    # no signal on any surface — not the logs, not the event
                    # log, and the channel actively claimed "added".
                    if not emails_to_try:
                        fail_reason = slack_module.INVITE_FAIL_NO_EB_EMAIL
                        logger.warning(
                            "Poll-incident: no_eb_email — contact_id=%s replied "
                            "YES but has no email on their Everbridge contact; "
                            "NOT auto-invited (dispatcher notified in channel)",
                            ack.get("contact_id", "?"),
                        )
                    elif uid is None and not any_lookup_raised:
                        fail_reason = slack_module.INVITE_FAIL_NO_SLACK_USER
                    else:
                        fail_reason = slack_module.INVITE_FAIL_ERROR
                    arrival_msg = slack_module.format_invite_failed_message(
                        name=ack.get("name") or "", reason=fail_reason,
                    )
                try:
                    await loop.run_in_executor(
                        None,
                        functools.partial(
                            slack_module.post_message,
                            slack_channel_id,
                            arrival_msg,
                            thread_ts=arrival_thread_ts,
                        ),
                    )
                except Exception as e:
                    logger.warning(
                        "Poll-incident: post_message failed (%s) for contact_id=%s — continuing",
                        type(e).__name__, ack.get("contact_id", "?"),
                    )
                # ---- VIP-breakthrough DM for safe-list arrivals -----------
                # PRD: SAR Slack Onboarding/PRD_DispatchTurbo_Responder_DM_VIP_Breakthrough.
                # Gate: SLACK_MODE=full (already inside the full-branch) AND
                # responder's email is on dispatch-safe-list. Idempotency via
                # slack_dm_sent_user_ids array + ArrayUnion on Firestore.
                # Gated on `invited`, NOT `uid`: the DM text asserts "You've
                # been added to incident <#CHANNEL_ID>". If the lookup resolved
                # but invite_user raised, sending it would repeat the very
                # false positive this change removes from the channel post —
                # and on a worse surface, since this DM breaks through Do Not
                # Disturb and links the responder to a private channel they
                # cannot open. Strict narrowing: invited=True implies uid.
                if invited:
                    emails_lower = {e.lower().strip() for e in emails_to_try if e}
                    safe_set = {e.lower().strip() for e in safe_list_emails if e}
                    if emails_lower & safe_set:
                        already_dmd = set(doc.get("slack_dm_sent_user_ids") or [])
                        if uid in already_dmd:
                            logger.debug(
                                "Poll-incident: uid=%s already DM'd — skipping duplicate",
                                uid,
                            )
                        else:
                            dm_test_label = os.environ.get(
                                "DISPATCH_TURBO_TEST_LABEL", "",
                            )
                            dm_text = slack_module.format_incident_dm_text(
                                channel_id=slack_channel_id,
                                test_label=dm_test_label,
                            )
                            # Capture the bool return per Failure-mode Q3
                            # (also required by test_discarded_return_values
                            # MUST_CAPTURE). At poll-time the Firestore
                            # ArrayUnion inside _send_dm_and_persist is the
                            # authoritative persistence — subsequent poll
                            # cycles will re-read the field, so we don't
                            # accumulate a Python-local list here (unlike
                            # send-time Step 7b which needs the accumulation
                            # to survive the final .set()).
                            if await _send_dm_and_persist(
                                loop=loop, slack_module=slack_module,
                                event_id=event_id, user_id=uid,
                                slack_channel_id=slack_channel_id,
                                dm_text=dm_text,
                                where="poll_time_dm",
                            ):
                                logger.info(
                                    "Poll-incident: DM sent to uid=%s "
                                    "(contact_id=%s)",
                                    uid, ack.get("contact_id", "?"),
                                )
        elif slack_mode == "shadow":
            # Shadow mode: three-way partition by safe-list email match.
            # Safe-list match → post '<Name> added' (timeline marker, matches
            # full-mode wording); non-match with an email → 'Would invite:
            # <Name>' WITHOUT a real invite (dispatcher-confidence behavior);
            # email-less → 'Cannot invite'. NOTE (#581): safe-list is a proxy
            # for send-time membership that drifted post-#580 — the label can
            # be wrong for group-vs-safe-list mismatches. Shadow = personal-dev
            # only, so cosmetic; see _partition_arrivals_for_shadow + #581.
            try:
                already_member_names, resolvable_names, unresolvable_names = (
                    _partition_arrivals_for_shadow(new_arrivals, safe_list_emails)
                )
                msg = slack_module.format_would_invite_message(
                    already_member_names=already_member_names,
                    resolvable_names=resolvable_names,
                    unresolvable_names=unresolvable_names,
                )
                # Same rule as full mode: routine arrivals go in the thread
                # under the pinned welcome, anything a dispatcher must ACT on
                # stays top-level and keeps notifying. The split is coarser
                # here because format_would_invite_message renders all three
                # buckets into ONE message — so a single "Would invite" or
                # "Cannot invite" line keeps the whole post top-level rather
                # than burying that line in a thread nobody is subscribed to.
                # Purely-'<Name> added' cycles thread and stay silent.
                shadow_has_failure_content = bool(
                    resolvable_names or unresolvable_names
                )
                shadow_thread_ts = (
                    None if shadow_has_failure_content
                    else (doc.get("welcome_ts") or None)
                )
                if msg:
                    await loop.run_in_executor(
                        None,
                        functools.partial(
                            slack_module.post_message, slack_channel_id, msg,
                            thread_ts=shadow_thread_ts,
                        ),
                    )
            except Exception as e:
                logger.warning(
                    "Poll-incident: shadow-mode post_message failed (%s) — continuing",
                    type(e).__name__,
                )

    # ---- Per-responder D4H sync (non-blocking Cloud Task per YES) ----------
    # Enqueue a /d4h-sync-yes worker for each new arrival that has an email.
    # Graceful-degrade: skip when d4h_activity_id absent (D4H create failed at
    # dispatch — _load_d4h_activity_id inside handle_per_yes_sync_task would
    # also no-op, but skipping here avoids unnecessary Cloud Tasks enqueues).
    # Per-YES typically arrives >3 min after dispatch, well after D4H has
    # finalized the auto-populated attendance records, so the async-init timing
    # race that affects bulk-ABSENT does not apply here.
    if new_arrivals and doc.get("d4h_activity_id"):
        for _arr in new_arrivals:
            _arr_email = (_arr.get("emails") or [""])[0]
            if not _arr_email:
                continue
            try:
                await loop.run_in_executor(
                    None,
                    functools.partial(
                        d4h.enqueue_per_yes_sync,
                        event_id,
                        _arr_email,
                        _arr.get("groups", []),
                    ),
                )
            except Exception as _d4h_exc:
                logger.warning(
                    "Poll-incident: D4H per-YES enqueue failed event_id=%s err=%s — continuing",
                    event_id, type(_d4h_exc).__name__,
                )

    # ---- Recompose + edit the #active-incidents tally ---------------------
    active_incidents_channel_id = os.environ.get("ACTIVE_INCIDENTS_CHANNEL_ID", "")
    active_incidents_ts = doc.get("active_incidents_ts", "")
    if (new_arrivals or pre_discovery_just_resolved or counts_changed) and active_incidents_channel_id and active_incidents_ts:
        header = "🔔 Everbridge ACTIVE"
        try:
            await loop.run_in_executor(
                None,
                functools.partial(
                    slack_module.edit_message,
                    active_incidents_channel_id,
                    active_incidents_ts,
                    _compose_active_incidents_tally(doc, header),
                ),
            )
        except Exception as e:
            logger.warning(
                "Poll-incident: tally edit failed (%s) — chain continues",
                type(e).__name__,
            )

    # ---- Check 5 stop conditions ------------------------------------------
    stop_reason = _check_stop_conditions(
        doc=doc,
        poll_response=poll_response,
        mode_tolerance_s=tolerance_s,
        now_dt=now_utc(),
    )
    if stop_reason:
        stop_incident(doc, stop_reason)
        doc_ref.update({
            "status":       doc["status"],
            "stop_reason":  doc["stop_reason"],
            "expire_at":    doc["expire_at"],
            "last_poll_at": now_utc(),
        })
        await _edit_tally(loop, slack_module, doc,
                          _render_terminal_header(stop_reason))
        logger.info(
            "Poll-incident: stopped event_id=%s reason=%s — chain ends",
            event_id, stop_reason,
        )
        return JSONResponse({"status": f"stopped_{stop_reason}"}, status_code=200)

    # ---- Continue: record poll time + enqueue next cycle ------------------
    doc_ref.update({"last_poll_at": now_utc()})
    _enqueue_poll_task(event_id, delay_s=poll_interval, mode="post_discovery")
    return JSONResponse({"status": "continuing", "responders": len(doc.get("responders", []))},
                        status_code=200)


async def _edit_tally(loop, slack_module, doc: dict, header: str) -> None:
    """Common helper for terminal-state tally edits.  Wraps the edit in
    a try/except so a Slack failure during a stop transition doesn't
    break the chain — the doc is already in terminal state in Firestore."""
    active_incidents_channel_id = os.environ.get("ACTIVE_INCIDENTS_CHANNEL_ID", "")
    active_incidents_ts = doc.get("active_incidents_ts", "")
    if not active_incidents_channel_id or not active_incidents_ts:
        return
    import slack as _slack
    try:
        await loop.run_in_executor(
            None,
            functools.partial(
                _slack.edit_message,
                active_incidents_channel_id,
                active_incidents_ts,
                _compose_active_incidents_tally(doc, header),
            ),
        )
    except Exception as e:
        logger.warning(
            "Poll-incident: terminal-state tally edit failed (%s) — Firestore is authoritative",
            type(e).__name__,
        )


# ---------------------------------------------------------------------------
# Global error handler — prevents stack traces leaking to clients
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s", type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
