"""
caltopo.py — CalTopo Team API integration for SCCSSAR Dispatch Turbo.

Creates SAR-mode incident maps with markers (LKP, Residence, Staging).

Auth: HMAC-SHA256 signed requests using Credential ID + Secret from Secret Manager.
API docs: https://training.caltopo.com/all_users/team-accounts/teamapi

DESIGN DECISION (do not revert without team discussion):
  We call the CalTopo REST API directly rather than using the caltopo_python library.
  The library is maintained by NCSSAR and is fragile to CalTopo API changes.
  Direct REST calls give us full control and no external dependency.

DESIGN DECISION (do not revert without team discussion):
  Maps are created with mode="sar" and sharing="SECRET".
  SAR mode enables ICS-standard icon sets. SECRET sharing means only team members
  with the link can view — not indexed or discoverable.

DESIGN DECISION (do not revert without team discussion):
  Map titles in personal-dev (`sar-dispatch-dev`) include a random 4-hex-char
  suffix so test maps can be identified and bulk-deleted. SCCSSAR-dev and prod
  produce real operational maps and must have clean titles — adding suffixes
  there would clutter the team's map inventory and make real incidents harder
  to find. Gated by PROJECT_ID (Cloud Run env var, set in terraform).
  See: _make_map_title() and _SUFFIX_PROJECTS.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — injected via env vars from GCP Secret Manager
# ---------------------------------------------------------------------------

CALTOPO_BASE_URL      = "https://caltopo.com"
CALTOPO_TEAM_ID       = os.environ.get("CALTOPO_TEAM_ID", "")
CALTOPO_CREDENTIAL_ID = os.environ.get("CALTOPO_CREDENTIAL_ID", "")
CALTOPO_CREDENTIAL_SECRET = os.environ.get("CALTOPO_CREDENTIAL_SECRET", "")

# ---------------------------------------------------------------------------
# Icon / color constants
# DESIGN DECISION: marker-symbol strings are not publicly documented by CalTopo.
# Using "point" as a safe universal fallback until we export a CalTopo map with
# SAR icons and read the actual symbol strings from the GeoJSON.
# TODO: replace placeholders after Bill exports a test map with SAR icons.
# ---------------------------------------------------------------------------

# Colors (6-char hex, no leading #  — CalTopo format)
COLOR_RED    = "FF0000"   # LKP, officer-designated staging, Residence
COLOR_BLUE   = "0000FF"   # Alternate staging locations

# Marker symbols — confirmed from CalTopo GeoJSON export (Feb 2026)
# DESIGN DECISION (do not revert without team discussion): these strings were
# identified by placing ICS markers in CalTopo and reading marker-symbol from
# the GeoJSON export. Do not guess — CalTopo silently falls back to a default
# if the symbol string is wrong.
SYMBOL_LKP             = "placemark2"  # Standard placemark / last known position
SYMBOL_RESIDENCE       = "hut"         # Home / residence icon
SYMBOL_STAGING_OFFICER = "cp"          # Command Post / ICS staging — officer-designated only
SYMBOL_STAGING_ALT     = "point"       # Plain dot — alternate staging locations
# PR-D-1 (2026-05-10): dispatcher-specified staging override gets the cp marker
# and red color, superseding any other staging entry. Officer entry, when a
# dispatcher override is present, falls through to alt (point + blue) — the
# dispatcher's pick is the authoritative team-facing staging.
SYMBOL_STAGING_DISPATCHER = "cp"


# ---------------------------------------------------------------------------
# HMAC-SHA256 request signing
# ---------------------------------------------------------------------------

def _sign(method: str, path: str, payload_json: str, expires_ms: int) -> str:
    """
    Build the HMAC-SHA256 signature for a CalTopo API request.

    Signing string format (from CalTopo API docs and caltopo_python source):
      POST: "POST {path}\\n{expires_ms}\\n{payload_json}"
      GET:  "GET  {path}\\n{expires_ms}\\n"

    The secret is Base64-encoded; decode before use.
    """
    if method.upper() == "POST":
        data = f"POST {path}\n{expires_ms}\n{payload_json}"
    else:
        data = f"GET {path}\n{expires_ms}\n"

    raw_secret = base64.b64decode(CALTOPO_CREDENTIAL_SECRET)
    token = hmac.new(raw_secret, data.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(token).decode("utf-8")


_MAX_CALTOPO_RESPONSE_BYTES = 1_000_000   # 1 MB — typical responses are <10 KB

# Bounded retry for TRANSIENT CalTopo 5xx. 2 retries = 3 attempts total.
#
# Why: a map is 8 CalTopo calls (1 create + 7 markers) and ANY single failure
# orphans the whole map. Live 2026-08-01 on personal-dev — 45 calls returned
# 200 and one returned 502, 2.6 s after a successful create, which cost the
# dispatcher an orphan map (LG1J0A2) and a manual re-click. Per-call failure is
# amplified 8x per map, so a rate that looks negligible per call is not.
#
# Retried on STATUS ONLY, never on timeout. A 5xx is CalTopo actively answering
# "I failed", so the write almost certainly did not land and a retry is safe. A
# timeout is the ambiguous case — the write may have succeeded — and it also
# costs the full 15 s connect budget, so retrying it would triple the worst-case
# wait on a request the dispatcher is watching. Timeouts still fail fast.
#
# 429 is deliberately NOT retried: it raises CalTopoRateLimitError, which the
# /create-map handler turns into a 503 + Retry-After. Retrying would fight the
# limiter and hide an actionable signal.
#
# Idempotency, accepted knowingly: CalTopo assigns its own feature IDs, so a
# 5xx returned AFTER the write landed would create a duplicate marker on retry.
# A visible duplicate the dispatcher deletes in one click beats an orphaned map
# rebuilt by hand mid-callout.
_CALTOPO_MAX_ATTEMPTS = 3
_CALTOPO_RETRY_BACKOFF_S = (0.5, 1.5)   # one entry per retry, not per attempt


class CalTopoOrphanMapError(RuntimeError):
    """Map was created on CalTopo but marker addition failed mid-way.

    Carries the partial ``map_id`` so the caller can surface it to the
    dispatcher for manual cleanup (the app deliberately does NOT have
    CalTopo DELETE privileges — per Bill 2026-05-30, the recovery
    path is a human deleting the orphan map via the CalTopo UI). The
    orphan map exists on CalTopo's team account with whatever
    markers WERE successfully added before the failure.

    Subclasses ``RuntimeError`` so the existing generic ``except
    RuntimeError`` fallback in main.py ``/create-map`` continues to
    catch it as a safety net. The ``/create-map`` handler adds a
    specific ``except CalTopoOrphanMapError`` branch BEFORE the
    generic catch to surface the ``partial_map_id`` in the 502 detail
    so the dispatcher can quote it when asking for cleanup.

    Attributes:
        partial_map_id: The CalTopo map ID that exists on the team
            account with incomplete markers.
        markers_added: Count of markers successfully added before
            the failure.
        markers_intended: Total markers the orchestrator intended to
            add (residence + staging).
        failure_step: Short description of which step failed (e.g.,
            'residence_marker', 'staging[3]').

    See CLAUDE.md Failure-mode Discipline Q1 ("What is the point of
    no return?") and batch-3 finding PR-H.2.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_map_id: str,
        markers_added: int,
        markers_intended: int,
        failure_step: str,
    ):
        super().__init__(message)
        self.partial_map_id = partial_map_id
        self.markers_added = markers_added
        self.markers_intended = markers_intended
        self.failure_step = failure_step


class CalTopoRateLimitError(RuntimeError):
    """HTTP 429 — CalTopo rate-limited; transient.

    Subclasses ``RuntimeError`` so the existing generic ``except RuntimeError``
    fallback in main.py ``/create-map`` continues to catch it as a safety net.
    The ``/create-map`` handler adds a specific ``except CalTopoRateLimitError``
    branch BEFORE the generic catch to return a 503 (with Retry-After hint)
    instead of the opaque 502 "CalTopo map creation failed" — distinct
    operator-facing signal so the dispatcher knows retrying in a few seconds
    is the right action rather than refreshing or reporting a broken
    integration. See CLAUDE.md Failure-mode Discipline Q5 ("Is the failure
    mode typed?") and batch-3 finding PR-H.1.
    """


def _post(client: httpx.Client, path: str, payload: dict) -> dict:
    """
    Sign and POST a JSON payload to the CalTopo API.
    Returns parsed JSON response body.
    Raises RuntimeError on non-2xx response.

    Defense-in-depth: bound the response body so a buggy or hostile
    upstream cannot OOM the Cloud Run container by returning arbitrarily
    large bytes. httpx has no built-in response-size limit on the
    default eager-read path (the body is buffered into memory before
    `.post()` returns). We use `client.stream()` + chunked iteration
    with a running counter to enforce the bound BEFORE the full body
    is buffered. The Content-Length header (when present) is a
    fast-reject optimization layered on top; the streaming counter is
    the authoritative check and also covers chunked-transfer responses
    that omit Content-Length entirely.
    """
    payload_json = json.dumps(payload)
    expires_ms   = int(time.time() * 1000) + 120_000   # now + 2 minutes
    signature    = _sign("POST", path, payload_json, expires_ms)

    form_data = {
        "id":        CALTOPO_CREDENTIAL_ID,
        "expires":   str(expires_ms),
        "signature": signature,
        "json":      payload_json,
    }

    url = CALTOPO_BASE_URL + path
    body_bytes = b""
    # The signature is deliberately computed ONCE, outside the loop: `expires`
    # is now + 120 s and the whole retry window is under 3 s, so it stays valid
    # for every attempt. Re-signing per attempt would be harmless but pointless.
    for attempt in range(_CALTOPO_MAX_ATTEMPTS):
        backoff_s = None
        with client.stream("POST", url, data=form_data, timeout=15.0) as resp:
            # Fast-reject when the upstream advertises an oversized body.
            # Malformed header falls through to the streaming counter
            # below, which is the authoritative bound.
            cl = resp.headers.get("content-length")
            if cl is not None:
                try:
                    if int(cl) > _MAX_CALTOPO_RESPONSE_BYTES:
                        logger.error(
                            "CalTopo response too large (header) | path=%s content_length=%s max=%d",
                            path, cl, _MAX_CALTOPO_RESPONSE_BYTES,
                        )
                        raise RuntimeError(
                            f"CalTopo response exceeds size limit for {path}"
                        )
                except ValueError:
                    pass   # streaming counter below catches it

            # Authoritative bound: count bytes as they arrive and bail
            # as soon as we exceed the cap. This is what actually protects
            # memory — the prior post-read Content-Length check fired too
            # late (Aikido finding on PR #474, line 133).
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > _MAX_CALTOPO_RESPONSE_BYTES:
                    logger.error(
                        "CalTopo response too large (stream) | path=%s received=%d max=%d",
                        path, total, _MAX_CALTOPO_RESPONSE_BYTES,
                    )
                    raise RuntimeError(
                        f"CalTopo response exceeds size limit for {path}"
                    )
                chunks.append(chunk)
            body_bytes = b"".join(chunks)

            # 429 ahead of the generic non-success branch: rate-limit is transient,
            # not a permanent integration failure. Distinct type lets the /create-map
            # handler surface a 503 with Retry-After so the dispatcher knows
            # retrying in a few seconds is the right action.
            #
            # NOT retried here, deliberately: retrying a rate limit fights the
            # limiter, and the dispatcher already gets an actionable 503.
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "")
                logger.warning(
                    "CalTopo 429 rate limited | path=%s retry_after=%s",
                    path, retry_after or "(none)",
                )
                raise CalTopoRateLimitError(
                    f"CalTopo rate limited at {path} "
                    f"(retry_after={retry_after or 'none'})"
                )

            if not resp.is_success:
                _is_last = attempt == _CALTOPO_MAX_ATTEMPTS - 1
                if 500 <= resp.status_code < 600 and not _is_last:
                    logger.warning(
                        "CalTopo %d — retrying | path=%s attempt=%d/%d",
                        resp.status_code, path, attempt + 1, _CALTOPO_MAX_ATTEMPTS,
                    )
                    backoff_s = _CALTOPO_RETRY_BACKOFF_S[attempt]
                else:
                    # NOTE: do not log body bytes — CalTopo error bodies can echo
                    # back marker titles (which embed staging addresses).
                    logger.error("CalTopo API error | path=%s status=%d", path, resp.status_code)
                    raise RuntimeError(f"CalTopo API returned {resp.status_code} for {path}")

        if backoff_s is None:
            break
        # Sleep AFTER the context manager exits so a retry does not hold the
        # connection open for the duration of the backoff.
        time.sleep(backoff_s)

    try:
        return json.loads(body_bytes)
    except Exception:
        return {"raw": body_bytes.decode("utf-8", errors="replace")}


# ---------------------------------------------------------------------------
# Map lifecycle
# ---------------------------------------------------------------------------

# Project IDs that emit cleanup-friendly random-suffixed map titles. Personal-dev
# is the only environment where developers run repeated test dispatches that need
# to be bulk-deleted from CalTopo afterward. SCCSSAR-dev (real team incidents
# during the rollout) and prod (real ops) get clean titles so operational maps
# are easy to find in the team's CalTopo inventory.
#
# Opt-in design: any new (unlisted) PROJECT_ID defaults to NO suffix — safer
# default for any future production-like deployment. Add new dev sandboxes to
# this set explicitly.
_SUFFIX_PROJECTS = {"sar-dispatch-dev"}


def _make_map_title(event_name: str) -> str:
    """
    Build a map title from the event name, appending a random 4-hex-char suffix
    in personal-dev only so test maps can be identified and bulk-deleted.

    DESIGN DECISION (do not revert without team discussion):
    Suffix gating is by PROJECT_ID (Cloud Run env var). Real-ops projects
    (SCCSSAR-dev, prod) MUST produce clean titles — Bill flagged 2026-05-09
    that "everything has that string" makes the CalTopo inventory hard to
    triage. See `_SUFFIX_PROJECTS`.
    """
    project_id = os.environ.get("PROJECT_ID", "")
    if project_id not in _SUFFIX_PROJECTS:
        return event_name
    suffix = secrets.token_hex(2)   # e.g. "a3f7" — 4 hex chars from 2 random bytes
    return f"{event_name} [{suffix}]"


def _create_map(client: httpx.Client, title: str, seed_marker: dict, description: str = "") -> str:
    """
    Create a new SAR-mode CollaborativeMap and return the map ID.

    DESIGN DECISION: mode="sar" enables ICS icon set. sharing="SECRET" means only
    team members with the direct link can view. The API requires at least one feature
    in the initial state (returns HTTP 500 if features list is empty).

    To avoid the placeholder-deletion problem (CalTopo assigns its own server-side IDs,
    not the ones we specify), we pass a real geocoded marker as the seed feature.

    seed_marker: GeoJSON Feature dict to embed as the initial state feature.  REQUIRED —
    callers MUST provide a real marker (typically LKP, with Residence or first staging
    as a fallback).  The previous `[0.0, 0.0]` placeholder fallback was removed because
    it produced a real "SAR Incident" marker on the wrong continent any time LKP
    geocoding failed (2026-05-08 SJSU live test, intersection LKP).  See
    `_build_seed_feature()` below.
    """
    if seed_marker is None:
        raise ValueError(
            "_create_map: seed_marker is required — pass a real geocoded "
            "feature (LKP, Residence, or first staging marker)"
        )

    # DESIGN DECISION (do not revert without team discussion):
    # Two-part approach required to set OpenStreetMap as the default base layer:
    #
    # Part 1 — properties.mapConfig (official, confirmed by CalTopo support Feb 2026):
    #   "om" is the OpenStreetMap layer alias. mapConfig MUST be a JSON *string*, not a
    #   dict — CalTopo silently ignores dict values. json.dumps() produces the correct
    #   form: '{"activeLayers": [["om", 1]]}'.  Matches the docs example exactly.
    #
    # Part 2 — state.upstream: False (undocumented but required):
    #   CalTopo maps default to upstream:True, which causes the map to inherit the team's
    #   default base layer configuration, silently overriding properties.mapConfig.
    #   Setting upstream:False tells CalTopo to use the map's own mapConfig instead.
    #   Confirmed working in PRs #56/#58 (reverted #59 pending official docs). CalTopo
    #   support did not mention upstream in their Feb 2026 response — but without it,
    #   mapConfig is ignored at creation time regardless of correct string encoding.
    OSM_MAP_CONFIG = json.dumps({"activeLayers": [["om", 1]]})

    path    = f"/api/v1/acct/{CALTOPO_TEAM_ID}/CollaborativeMap"
    payload = {
        "properties": {
            "title":       title,
            "mode":        "sar",
            "sharing":     "SECRET",
            "mapConfig":   OSM_MAP_CONFIG,
            **({"description": description} if description else {}),
        },
        "state": {
            "type":     "FeatureCollection",
            "features": [seed_marker],
            "upstream": False,    # must be False or team default overrides mapConfig
        },
    }

    result = _post(client, path, payload)

    # The API response carries the map ID in result["result"]["id"]
    map_id = None
    if isinstance(result, dict):
        r = result.get("result", {})
        if isinstance(r, dict):
            map_id = r.get("id")
        if not map_id:
            map_id = result.get("id") or result.get("mapId")

    if not map_id:
        # Fallback: some API versions return the map URL as a plain string
        raw = result.get("raw", "")
        for part in raw.rstrip("/").split("/"):
            if part and 3 <= len(part) <= 10 and part.isalnum():
                map_id = part

    if not map_id:
        raise RuntimeError(f"Could not extract map ID from CalTopo response: {result}")

    # NOTE: do not log title — the map title is the event name and embeds
    # the LKP street name (Locked Design Decision: Event Name format).
    logger.info("CalTopo map created | map_id=%s", map_id)
    return map_id


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------

def _add_marker(
    client: httpx.Client,
    map_id: str,
    lat: float,
    lng: float,
    title: str,
    description: str = "",
    color: str = COLOR_RED,
    symbol: str = SYMBOL_LKP,
) -> None:
    """
    Add a point marker to the map.
    NOTE: CalTopo GeoJSON coordinates are [longitude, latitude] — lon first.
    """
    path    = f"/api/v1/map/{map_id}/Marker"
    payload = {
        "type": "Feature",
        "geometry": {
            "type":        "Point",
            "coordinates": [lng, lat],   # GeoJSON: lon, lat
        },
        "properties": {
            "class":               "Marker",
            "title":               title,
            "description":         description,
            "marker-color":        color,
            "marker-symbol":       symbol,
            "marker-size":         1,
            "marker-rotation":     None,
            "marker-visibility":   "visible",
        },
    }
    _post(client, path, payload)
    # NOTE: do not log title — marker titles embed the staging-candidate
    # address (e.g. "8. 2000 Hostetter Rd — Officer-designated staging").
    logger.info("CalTopo marker added | map_id=%s", map_id)


# ---------------------------------------------------------------------------
# Seed feature picker — pure helper (no httpx) so it's directly testable.
# ---------------------------------------------------------------------------

def _build_seed_feature(
    lkp: Optional[dict],
    residence: Optional[dict],
    staging: list,
) -> tuple[Optional[dict], Optional[str]]:
    """Pick the highest-priority real geocoded feature to seed map creation.

    CalTopo requires at least one feature when creating a map (returns HTTP 500
    on empty features list). The previous fallback was a `[0.0, 0.0]` placeholder
    titled "SAR Incident" — but that produced a real marker on the wrong continent
    every time LKP geocoding failed (2026-05-08 SJSU live test).

    Priority: LKP (preferred — the dispatcher's primary anchor) → Residence (always
    geocoded when present, often near the LKP) → first staging marker (last resort
    when neither LKP nor Residence geocoded but Overpass found POIs).

    Returns:
      (feature, source) where source is "lkp" | "residence" | "staging_0" — or
      (None, None) if nothing geocoded at all (caller should raise).

    The `source` return value tells `build_incident_map` which entity to skip
    re-adding via `_add_marker` so we don't post the same marker twice.
    """
    if lkp:
        return (
            {
                "type": "Feature",
                "geometry": {
                    "type":        "Point",
                    "coordinates": [lkp["lng"], lkp["lat"]],   # GeoJSON: lon, lat
                },
                "properties": {
                    "class":             "Marker",
                    "title":             lkp.get("label", "LKP"),
                    "description":       "Last Known Position",
                    "marker-color":      COLOR_RED,
                    "marker-symbol":     SYMBOL_LKP,
                    "marker-size":       1,
                    "marker-rotation":   None,
                    "marker-visibility": "visible",
                },
            },
            "lkp",
        )
    if residence:
        return (
            {
                "type": "Feature",
                "geometry": {
                    "type":        "Point",
                    "coordinates": [residence["lng"], residence["lat"]],
                },
                "properties": {
                    "class":             "Marker",
                    "title":             residence.get("label", "Residence"),
                    "description":       residence.get("description", "Subject's residence"),
                    "marker-color":      COLOR_RED,
                    "marker-symbol":     SYMBOL_RESIDENCE,
                    "marker-size":       1,
                    "marker-rotation":   None,
                    "marker-visibility": "visible",
                },
            },
            "residence",
        )
    if staging:
        s = staging[0]
        # Cosmetic: when staging[0] is a dispatcher override, label the seed feature
        # accordingly. Marker symbol/color stay the same (cp + red) — both dispatcher
        # and default-index-0 use those styling values.
        seed_desc = (
            "Dispatcher-specified staging"
            if s.get("type") == "dispatcher"
            else "Recommended staging"
        )
        return (
            {
                "type": "Feature",
                "geometry": {
                    "type":        "Point",
                    "coordinates": [s["lng"], s["lat"]],
                },
                "properties": {
                    "class":             "Marker",
                    "title":             s.get("label", "Staging"),
                    "description":       seed_desc,
                    "marker-color":      COLOR_RED,       # entry #0 always gets the CP styling
                    "marker-symbol":     SYMBOL_STAGING_OFFICER,
                    "marker-size":       1,
                    "marker-rotation":   None,
                    "marker-visibility": "visible",
                },
            },
            "staging_0",
        )
    return (None, None)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def build_incident_map(map_data: dict, dispatcher_name: str = "") -> str:
    """
    Create a CalTopo SAR incident map from structured map_data and return
    the shareable URL ("https://caltopo.com/m/{map_id}").

    map_data schema:
      {
        "event_name":  str,              # used as map title
        "lkp":         {"lat": float, "lng": float, "label": str},
        "residence":   {"lat": float, "lng": float, "label": str} | None,
        "staging":     [
          {"lat": float, "lng": float, "label": str, "type": "officer"|"alternate"}
        ],
        "rings":       [
          {"radius_miles": float, "label": str}   # e.g. {"radius_miles": 0.2, "label": "25th percentile — 0.2 mi"}
        ],
      }

    Raises RuntimeError if map creation fails.
    Credential env vars (CALTOPO_TEAM_ID, CALTOPO_CREDENTIAL_ID,
    CALTOPO_CREDENTIAL_SECRET) must be set.
    """
    if not all([CALTOPO_TEAM_ID, CALTOPO_CREDENTIAL_ID, CALTOPO_CREDENTIAL_SECRET]):
        raise RuntimeError("CalTopo credentials not configured (missing env vars)")

    event_name = map_data.get("event_name", "SAR Incident")
    lkp        = map_data.get("lkp")
    residence  = map_data.get("residence")
    staging    = map_data.get("staging", [])
    rings      = map_data.get("rings", [])

    title       = _make_map_title(event_name)
    map_desc    = f"Created by SCCSSAR Dispatch Turbo for {dispatcher_name}" if dispatcher_name else "Created by SCCSSAR Dispatch Turbo"

    # Pick the seed feature (LKP preferred; Residence then first staging as fallback
    # when LKP geocoding failed). Raises if nothing is geocoded — we never want a
    # placeholder marker at [0.0, 0.0] like the old behavior.
    seed_feature, seed_source = _build_seed_feature(lkp, residence, staging)
    if seed_feature is None:
        raise RuntimeError(
            "Cannot create CalTopo map — no LKP, Residence, or staging coordinates "
            "available; verify at least one address geocoded successfully"
        )
    logger.info(
        "CalTopo seed source | source=%s (lkp=%s residence=%s staging=%d)",
        seed_source, bool(lkp), bool(residence), len(staging),
    )

    with httpx.Client() as client:
        # 1. Create the map — seeded with the highest-priority real feature
        map_id = _create_map(client, title, seed_marker=seed_feature, description=map_desc)

        # 2. The seed is already on the map — skip re-adding whichever entity we used.
        #    LKP-as-seed: nothing else to add for LKP (no _add_marker for it elsewhere).
        #    Residence-as-seed: skip the Residence _add_marker below.
        #    Staging[0]-as-seed: skip i==0 in the staging loop below.

        # Batch-3 PR-H.2: from this point on, the map EXISTS on CalTopo's
        # team account. If a downstream marker POST fails, the map is
        # orphaned (incomplete markers, no rollback because the app
        # deliberately doesn't have CalTopo DELETE privileges — per Bill
        # 2026-05-30). Wrap the marker work in try/except so we can log
        # a structured WARNING with the partial map_id + diagnostic
        # context, then raise the typed CalTopoOrphanMapError so the
        # /create-map handler can surface the map_id in the 502 detail
        # for the dispatcher to quote when asking for manual cleanup.
        # Failure-mode rubric Q1 ("What is the point of no return?") —
        # the _create_map POST above is the point of no return.
        markers_added = 0  # The seed counts as the first marker on the map.
        # Compute markers_intended for the diagnostic (residence + non-seed
        # staging entries). The seed itself is implicit (always added).
        markers_intended = (
            (1 if (residence and seed_source != "residence") else 0)
            + sum(
                1 for i in range(len(staging))
                if not (seed_source == "staging_0" and i == 0)
            )
        )
        try:
            # 3. Residence marker (red — distinct location from LKP). Skip if it was the seed.
            if residence and seed_source != "residence":
                _add_marker(
                    client, map_id,
                    lat=residence["lat"], lng=residence["lng"],
                    title=residence.get("label", "Residence"),
                    description=residence.get("description", "Subject's residence"),
                    color=COLOR_RED,
                    symbol=SYMBOL_RESIDENCE,
                )
                markers_added += 1

        # 4. Staging markers
        # DESIGN DECISION (issue #244): entry #1 (index 0) gets the CP icon + red —
        # it is the default dispatcher choice (top recommendation). All other entries
        # get blue dot. The dispatcher can reassign staging on the map if needed, but
        # this sets the right default without requiring any extra click.
        # The officer entry (if server-injected as the last entry) is always a blue dot
        # because it appears last and is explicitly labeled "not among top recommendations".
        #
        # PR-D-1 (2026-05-10): dispatcher-specified override (s["type"] == "dispatcher")
        # takes the cp + red markers when present. The frontend prepends the override
        # at index 0, so in the override case `is_default` is also True and the markers
        # match — the explicit type check guards against any future code path that
        # places the dispatcher entry at a different index. Officer entry never gets cp
        # regardless of position. Precedence: dispatcher > index-0 default > officer.
        # Pinned by TestMarkerArbitration in test_main_regression.py.
            # 4. Staging markers
            # DESIGN DECISION (issue #244): entry #1 (index 0) gets the CP icon + red —
            # it is the default dispatcher choice (top recommendation). All other entries
            # get blue dot. The dispatcher can reassign staging on the map if needed, but
            # this sets the right default without requiring any extra click.
            # The officer entry (if server-injected as the last entry) is always a blue dot
            # because it appears last and is explicitly labeled "not among top recommendations".
            #
            # PR-D-1 (2026-05-10): dispatcher-specified override (s["type"] == "dispatcher")
            # takes the cp + red markers when present. The frontend prepends the override
            # at index 0, so in the override case `is_default` is also True and the markers
            # match — the explicit type check guards against any future code path that
            # places the dispatcher entry at a different index. Officer entry never gets cp
            # regardless of position. Precedence: dispatcher > index-0 default > officer.
            # Pinned by TestMarkerArbitration in test_main_regression.py.
            #
            # "Wilderness case" refinement (2026-07-16, Bill-approved): when the
            # ONLY staging entry is the officer's (Overpass returned zero
            # candidates — a remote/rural LKP like Joseph D. Grant County Park),
            # there is no quality-ranked #1 to protect, so the officer entry IS
            # the authoritative staging and takes cp + red. The "officer never
            # hijacks cp from a real quality-ranked #1" rule is preserved
            # wherever a non-officer entry exists (see _has_non_officer guard).
            _has_non_officer = any(s.get("type") != "officer" for s in staging)
            for i, s in enumerate(staging):
                if seed_source == "staging_0" and i == 0:
                    continue  # already on map as the seed feature
                entry_type = s.get("type")
                is_dispatcher = entry_type == "dispatcher"
                is_officer    = entry_type == "officer"
                is_default    = (i == 0)
                # AMENDED for #804 (Bill, 2026-09-05): the index-0 branch no
                # longer excludes officer entries. The `and not is_officer`
                # guard it used to carry came from the PR #404 (PR-D-1) Aikido
                # review, on the reasoning that officer entries must never
                # hijack cp from a real quality-ranked #1 — and its own comment
                # said the guard was "redundant in practice" because PASS-3
                # appends officer last.
                #
                # That assumption failed live on 2026-09-04: the officer's
                # free-text staging fuzzy-matched a park that was ALSO the
                # top-ranked recommendation, so the two MERGED into one entry
                # at index 0 typed `officer`. Every cp branch then declined it
                # — index 0 but officer; officer but alternates exist — and the
                # map came out with six blue dots and no command post. The
                # Locked Decision guarantees at most one cp; it never
                # guaranteed at least one.
                #
                # The rule that REMAINS is the one that was actually earned by
                # the 2026-05-09 XXSO regression: an officer entry BELOW index 0
                # never takes cp, so it cannot displace the quality-ranked #1
                # sitting above it. At index 0 there is no #1 above it to
                # protect. Pinned by TestMarkerArbitration.
                if is_dispatcher:
                    marker_color  = COLOR_RED
                    marker_symbol = SYMBOL_STAGING_DISPATCHER
                    marker_desc   = "Dispatcher-specified staging"
                elif is_officer and not _has_non_officer:
                    # Sole staging is the officer's (empty Overpass) — it is the
                    # authoritative staging, so give it cp + red rather than a
                    # bare blue dot that reads as "nothing recommended."
                    #
                    # ORDER IS LOAD-BEARING: this sole-officer entry is ALSO at
                    # index 0, so once #804 let officer entries through the
                    # index-0 branch it had to be tested FIRST or the wilderness
                    # case silently relabels to "(top recommendation)" — which
                    # is not what happened. Nothing was ranked; nothing was
                    # found. Same symbol either way, so the regression would
                    # have been invisible on the map and wrong only in the words.
                    marker_color  = COLOR_RED
                    marker_symbol = SYMBOL_STAGING_OFFICER
                    marker_desc   = "Officer-designated staging (only option)"
                elif is_default:
                    marker_color  = COLOR_RED
                    marker_symbol = SYMBOL_STAGING_OFFICER
                    marker_desc   = (
                        "Officer-designated staging (top recommendation)"
                        if is_officer else "Recommended staging"
                    )
                else:
                    marker_color  = COLOR_BLUE
                    marker_symbol = SYMBOL_STAGING_ALT
                    marker_desc   = "Officer-designated staging" if is_officer else "Alternate staging"
                _add_marker(
                    client, map_id,
                    lat=s["lat"], lng=s["lng"],
                    title=s.get("label", "Staging"),
                    description=marker_desc,
                    color=marker_color,
                    symbol=marker_symbol,
                )
                markers_added += 1
        except CalTopoRateLimitError:
            # Self-review pass on PR-H.2 caught this: 429 mid-marker-loop
            # would otherwise be absorbed into CalTopoOrphanMapError below
            # via `except Exception` (since CalTopoRateLimitError IS-A
            # RuntimeError IS-A Exception), losing the 503+Retry-After
            # routing in /create-map. Two concerns at once:
            #   1. Routing: 429 IS transient, dispatcher retry IS right
            #      action (503 + Retry-After) — let the 429 propagate
            #      so /create-map's specific CalTopoRateLimitError branch
            #      fires correctly
            #   2. Orphan tracking: the map IS partial at this point
            #      (seed + some markers, then 429 stopped us) — log the
            #      WARNING so the maintainer can find the orphan post-hoc
            #      via Cloud Run logs even though the dispatcher's retry
            #      will create a new full map
            logger.warning(
                "CalTopo partial-map orphan via rate-limit | "
                "partial_map_id=%s markers_added=%d markers_intended=%d "
                "failure_step=rate_limit_at_marker_step_%d",
                map_id, markers_added, markers_intended, markers_added,
            )
            raise  # propagate the 429 unwrapped → /create-map routes to 503
        except Exception as exc:
            # Identify which step failed. markers_added counts only post-seed
            # markers; the seed was added at map-create time and always
            # succeeds (otherwise _create_map would have raised earlier).
            # The residence step (if present) is the first thing tried in the
            # try block; if markers_added == 0 and residence was intended,
            # residence failed. Otherwise we're mid-staging.
            residence_intended = bool(residence) and seed_source != "residence"
            if residence_intended and markers_added == 0:
                failure_step = "residence_marker"
            else:
                # We're mid-staging. Compute the staging index that failed.
                # Subtract 1 for the residence step if it was completed.
                completed_staging = markers_added - (1 if residence_intended else 0)
                # The staging loop skips index 0 when seed is staging_0, so
                # the FIRST entry attempted by the loop is staging[1] in that
                # case (self-review pass on PR-H.2 LOW finding). Account for
                # the seed-skip so the maintainer-visible index matches the
                # dispatcher's staging list.
                seed_skip_offset = 1 if seed_source == "staging_0" else 0
                failure_idx = completed_staging + seed_skip_offset
                failure_step = f"staging[{failure_idx}] ({type(exc).__name__})"
            logger.warning(
                "CalTopo partial-map orphan | partial_map_id=%s "
                "markers_added=%d markers_intended=%d failure_step=%s exc=%s",
                map_id, markers_added, markers_intended, failure_step, type(exc).__name__,
            )
            raise CalTopoOrphanMapError(
                f"CalTopo map partially created — orphan map id: {map_id}. "
                f"Markers added: {markers_added}/{markers_intended}. "
                f"Failure at: {failure_step}. Manual cleanup required.",
                partial_map_id=map_id,
                markers_added=markers_added,
                markers_intended=markers_intended,
                failure_step=failure_step,
            ) from exc

    map_url = f"https://caltopo.com/m/{map_id}"
    logger.info("CalTopo incident map ready | url=%s", map_url)
    return map_url
