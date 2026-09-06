"""test_caltopo.py — pure-logic tests for caltopo.py helpers.

Per CLAUDE.md test file pattern: mirror the seed-selection logic from
`backend/caltopo.py` locally rather than importing the module directly.
`caltopo.py` imports `httpx` and reads CalTopo env vars at module load,
neither of which are present in the local pytest environment (they live
in the Cloud Run container only).

When updating `_build_seed_feature` in caltopo.py, ALSO update the mirror
here. The mirror IS the test contract — if caltopo.py drifts away from
this mirror, that drift will surface as wrong-marker placement in
production CalTopo maps, and these tests are the regression boundary.

Bug history pinned by these tests (PR-fix-1, 2026-05-08):
  - Pre-fix: when LKP geocoding failed, `build_incident_map` passed
    `seed_marker=None` to `_create_map`, which fell back to a placeholder
    GeoJSON Point at `[0.0, 0.0]` titled "SAR Incident". Result: a real
    marker on Null Island (off the coast of Africa) every time the
    dispatcher created a map after an LKP geocode failure. First
    surfaced in the 2026-05-07 SJSU live incident review when the LKP
    was an intersection ("5th St & St John St") that Nominatim cannot
    geocode and personal-dev had no Google Maps API key.
  - Post-fix: `_build_seed_feature` returns the highest-priority real
    geocoded feature (LKP > Residence > first staging) so `_create_map`
    always gets a real marker. If nothing geocoded at all, it returns
    `(None, None)` and `build_incident_map` raises rather than producing
    a phantom marker.
"""
from typing import Optional


# ---------------------------------------------------------------------------
# Mirrored constants — must be kept in sync with caltopo.py
# ---------------------------------------------------------------------------

_COLOR_RED               = "FF0000"
_SYMBOL_LKP              = "placemark2"
_SYMBOL_RESIDENCE        = "hut"
_SYMBOL_STAGING_OFFICER  = "cp"


# ---------------------------------------------------------------------------
# Mirror of _build_seed_feature() from caltopo.py
# ---------------------------------------------------------------------------

def _build_seed_feature(
    lkp: Optional[dict],
    residence: Optional[dict],
    staging: list,
) -> tuple[Optional[dict], Optional[str]]:
    """Pick the highest-priority real geocoded feature to seed map creation.

    Mirror of `caltopo.py::_build_seed_feature`. Priority order:
    LKP > Residence > first staging. Returns `(None, None)` if nothing
    geocoded.
    """
    if lkp:
        return (
            {
                "type": "Feature",
                "geometry": {
                    "type":        "Point",
                    "coordinates": [lkp["lng"], lkp["lat"]],
                },
                "properties": {
                    "class":             "Marker",
                    "title":             lkp.get("label", "LKP"),
                    "description":       "Last Known Position",
                    "marker-color":      _COLOR_RED,
                    "marker-symbol":     _SYMBOL_LKP,
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
                    "marker-color":      _COLOR_RED,
                    "marker-symbol":     _SYMBOL_RESIDENCE,
                    "marker-size":       1,
                    "marker-rotation":   None,
                    "marker-visibility": "visible",
                },
            },
            "residence",
        )
    if staging:
        s = staging[0]
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
                    "description":       "Recommended staging",
                    "marker-color":      _COLOR_RED,
                    "marker-symbol":     _SYMBOL_STAGING_OFFICER,
                    "marker-size":       1,
                    "marker-rotation":   None,
                    "marker-visibility": "visible",
                },
            },
            "staging_0",
        )
    return (None, None)


# ---------------------------------------------------------------------------
# Tests — seed-selection priority order
# ---------------------------------------------------------------------------

class TestBuildSeedFeaturePriority:
    """Pin the LKP > Residence > staging[0] priority order. Pre-fix bug
    was that NO seed at all → placeholder at [0.0, 0.0]."""

    _LKP       = {"lat": 37.3382, "lng": -121.8863, "label": "LKP — 5th & St John, San Jose"}
    _RESIDENCE = {"lat": 37.4000, "lng": -122.1000, "label": "Residence — 100 Example Rd, Palo Alto"}
    _STAGING_1 = {"lat": 37.3393, "lng": -121.8918, "label": "1. 100 N 4th St — 7-Eleven", "type": "alternate"}
    _STAGING_2 = {"lat": 37.3402, "lng": -121.8870, "label": "2. 100 N 1st St — Shell", "type": "alternate"}

    def test_lkp_present_wins(self):
        feature, source = _build_seed_feature(
            lkp=self._LKP, residence=self._RESIDENCE, staging=[self._STAGING_1, self._STAGING_2],
        )
        assert source == "lkp"
        # GeoJSON requires [lon, lat] not [lat, lon].
        assert feature["geometry"]["coordinates"] == [-121.8863, 37.3382]
        assert feature["properties"]["title"] == self._LKP["label"]
        assert feature["properties"]["marker-symbol"] == _SYMBOL_LKP

    def test_no_lkp_residence_wins(self):
        # Bill's 2026-05-07 SJSU case: LKP was an intersection that Nominatim
        # couldn't geocode and Google Maps wasn't available. Residence at the
        # Palo Alto address geocoded fine — should now seed the map.
        feature, source = _build_seed_feature(
            lkp=None, residence=self._RESIDENCE, staging=[self._STAGING_1, self._STAGING_2],
        )
        assert source == "residence"
        assert feature["geometry"]["coordinates"] == [-122.1000, 37.4000]
        assert feature["properties"]["title"] == self._RESIDENCE["label"]
        assert feature["properties"]["marker-symbol"] == _SYMBOL_RESIDENCE

    def test_no_lkp_no_residence_first_staging_wins(self):
        # Wilderness case: no Residence on form, LKP failed, but Overpass
        # found POIs near the city centroid. First staging marker becomes
        # the seed so the map can still be created.
        feature, source = _build_seed_feature(
            lkp=None, residence=None, staging=[self._STAGING_1, self._STAGING_2],
        )
        assert source == "staging_0"
        assert feature["geometry"]["coordinates"] == [-121.8918, 37.3393]
        assert feature["properties"]["marker-symbol"] == _SYMBOL_STAGING_OFFICER

    def test_nothing_available_returns_none(self):
        # Only happens if ALL of: LKP geocode failed, Residence geocode
        # failed (or absent), AND Overpass returned zero candidates. Caller
        # MUST raise rather than create a placeholder map (the pre-fix
        # behavior produced a [0.0, 0.0] phantom marker).
        feature, source = _build_seed_feature(lkp=None, residence=None, staging=[])
        assert feature is None
        assert source is None


class TestBuildSeedFeatureNoPhantomCoords:
    """Regression pin for the pre-fix [0.0, 0.0] placeholder bug.
    None of the seed paths should EVER return coordinates of [0, 0]
    unless the actual lat/lng input was 0 (which would be a real coord
    in the Atlantic — separate problem)."""

    def test_lkp_path_uses_real_coords(self):
        feature, _ = _build_seed_feature(
            lkp={"lat": 1.0, "lng": 1.0, "label": "x"},
            residence=None, staging=[],
        )
        assert feature["geometry"]["coordinates"] != [0.0, 0.0]

    def test_residence_path_uses_real_coords(self):
        feature, _ = _build_seed_feature(
            lkp=None,
            residence={"lat": 1.0, "lng": 1.0, "label": "y"},
            staging=[],
        )
        assert feature["geometry"]["coordinates"] != [0.0, 0.0]

    def test_staging_path_uses_real_coords(self):
        feature, _ = _build_seed_feature(
            lkp=None, residence=None,
            staging=[{"lat": 1.0, "lng": 1.0, "label": "z"}],
        )
        assert feature["geometry"]["coordinates"] != [0.0, 0.0]

    def test_no_seed_returns_none_not_phantom(self):
        # The CRITICAL regression: pre-fix this case fell through to a
        # placeholder Point at [0.0, 0.0] titled "SAR Incident". The fix
        # returns (None, None) so the caller raises instead.
        feature, source = _build_seed_feature(lkp=None, residence=None, staging=[])
        # Nothing should produce a marker named "SAR Incident" or coords [0,0].
        assert feature is None
        assert source is None


class TestBuildSeedFeatureSkipSemantics:
    """The seed source string drives skip-logic in build_incident_map so
    we don't post the same marker twice. Pin the exact string values."""

    def test_lkp_source_string(self):
        _, source = _build_seed_feature(
            lkp={"lat": 1.0, "lng": 2.0, "label": "x"},
            residence={"lat": 3.0, "lng": 4.0, "label": "y"},
            staging=[],
        )
        # build_incident_map relies on this exact string to skip-or-add Residence.
        assert source == "lkp"

    def test_residence_source_string(self):
        _, source = _build_seed_feature(
            lkp=None,
            residence={"lat": 3.0, "lng": 4.0, "label": "y"},
            staging=[],
        )
        # build_incident_map checks `if seed_source != "residence"` before
        # the Residence _add_marker — drift here would re-add Residence.
        assert source == "residence"

    def test_staging_source_string(self):
        _, source = _build_seed_feature(
            lkp=None, residence=None,
            staging=[{"lat": 5.0, "lng": 6.0, "label": "z"}],
        )
        # build_incident_map checks `if seed_source == "staging_0" and i == 0`
        # to skip the first staging _add_marker — drift here would re-add it.
        assert source == "staging_0"


# ---------------------------------------------------------------------------
# caltopo.py::_make_map_title — PROJECT_ID-gated random-suffix appendage.
# Bug context: 2026-05-09 — Bill flagged that ALL CalTopo maps include a
# random 4-hex-char suffix (e.g. "[5cd6]"), originally meant to identify
# test maps for bulk cleanup. As SCCSSAR-dev now produces real operational
# maps during the rollout, having every title suffixed clutters the team's
# CalTopo inventory and makes real incidents harder to find.
#
# Fix: gate the suffix on PROJECT_ID — personal-dev (`sar-dispatch-dev`)
# keeps the suffix for cleanup; SCCSSAR-dev (`sar-dispatch-sccssar-dev`)
# and prod produce clean titles.
#
# Pinned by mirroring the gating logic. Drift here = either SCCSSAR-dev
# titles get noisy again, or personal-dev loses the cleanup suffix and
# test maps pile up untracked.
# ---------------------------------------------------------------------------

import os
import re
import secrets

# Mirror of caltopo.py::_SUFFIX_PROJECTS — keep in sync.
_SUFFIX_PROJECTS_MIRROR = {"sar-dispatch-dev"}


def _make_map_title_mirror(event_name: str, project_id: str) -> str:
    """Mirror of caltopo.py::_make_map_title() with project_id passed in
    explicitly so tests don't need to monkey-patch os.environ."""
    if project_id not in _SUFFIX_PROJECTS_MIRROR:
        return event_name
    suffix = secrets.token_hex(2)
    return f"{event_name} [{suffix}]"


class TestMakeMapTitleSuffixGating:
    """Pin PROJECT_ID gating of the random-suffix on CalTopo map titles."""

    def test_personal_dev_appends_suffix(self):
        title = _make_map_title_mirror("2026-05-07 SJSU 5th", "sar-dispatch-dev")
        # Title should be "<event_name> [<4 hex chars>]"
        assert re.fullmatch(r"2026-05-07 SJSU 5th \[[0-9a-f]{4}\]", title)

    def test_sccssar_dev_no_suffix(self):
        # The exact 2026-05-09 cleanup ask — SCCSSAR-dev maps must be clean.
        title = _make_map_title_mirror("2026-05-07 SJSU 5th", "sar-dispatch-sccssar-dev")
        assert title == "2026-05-07 SJSU 5th"

    def test_prod_no_suffix(self):
        # Future-proof: prod must NOT emit suffixed titles either.
        title = _make_map_title_mirror("2026-05-07 SJSU 5th", "sar-dispatch-prod-20260218")
        assert title == "2026-05-07 SJSU 5th"

    def test_unknown_project_id_defaults_to_no_suffix(self):
        # Opt-in design: any unrecognized project ID falls through to clean
        # title (production-safe default for any future deployment).
        title = _make_map_title_mirror("2026-05-07 SJSU 5th", "some-future-env")
        assert title == "2026-05-07 SJSU 5th"

    def test_empty_project_id_defaults_to_no_suffix(self):
        # Defensive: missing PROJECT_ID env var (set "" by os.environ.get
        # default) must NOT accidentally trigger suffix mode.
        title = _make_map_title_mirror("2026-05-07 SJSU 5th", "")
        assert title == "2026-05-07 SJSU 5th"

    def test_suffix_is_random_per_call(self):
        # The suffix must vary across calls within personal-dev so multiple
        # test runs of the same form produce distinct map titles. Two calls
        # in a row should be statistically extremely unlikely to collide.
        a = _make_map_title_mirror("event", "sar-dispatch-dev")
        b = _make_map_title_mirror("event", "sar-dispatch-dev")
        # Rare false-positive odds: 1 in 65,536 — acceptable for this test.
        assert a != b


# ---------------------------------------------------------------------------
# Mirror of CalTopoRateLimitError — batch-3 PR-H.1
# ---------------------------------------------------------------------------
# Pins the class-hierarchy contract for CalTopo 429 routing. The /create-map
# handler in main.py adds a specific ``except CalTopoRateLimitError`` branch
# that returns 503 + Retry-After. If the mirror class hierarchy here drifts
# from caltopo.py (e.g., someone makes CalTopoRateLimitError no longer
# subclass RuntimeError), the production fallback ``except RuntimeError`` in
# main.py would also need to change — and would silently miss 429s if it
# didn't. This mirror IS that test contract.

class CalTopoRateLimitError(RuntimeError):
    """Mirror of caltopo.CalTopoRateLimitError."""


class TestCalTopoRateLimitErrorContract:
    """Pin the class hierarchy + message-format contract for CalTopo 429.

    Drift in any of these invariants IS the test contract:
      - CalTopoRateLimitError must subclass RuntimeError (preserves the
        generic ``except RuntimeError`` fallback in main.py /create-map
        as a safety net if a future caller misses the specific branch)
      - Specific routing (503 + Retry-After) is the /create-map handler's
        responsibility; this test ensures the type discrimination works
    """

    def test_is_runtime_error_subclass(self):
        """The /create-map handler keeps ``except RuntimeError`` as a
        fallback. CalTopoRateLimitError MUST subclass RuntimeError so
        that path still catches it if the specific branch is ever removed
        or ordering changes. Defensive against accidental refactoring."""
        assert issubclass(CalTopoRateLimitError, RuntimeError), (
            "CalTopoRateLimitError MUST subclass RuntimeError so the "
            "generic ``except RuntimeError`` fallback in main.py "
            "/create-map keeps catching 429 if the specific branch is "
            "removed in a future refactor"
        )

    def test_message_includes_path_and_retry_after(self):
        """The exception message is the primary log-triage signal when a
        429 fires (no PII, no body — just rate-limit context). Format
        pinned: path + retry_after value or 'none'."""
        exc = CalTopoRateLimitError(
            "CalTopo rate limited at /api/v1/acct/SCCS/CollaborativeMap "
            "(retry_after=5)"
        )
        msg = str(exc)
        assert "/api/v1/acct/SCCS/CollaborativeMap" in msg
        assert "retry_after=5" in msg

    def test_message_handles_missing_retry_after(self):
        """429 with no Retry-After header — the exception message should
        still include 'retry_after=none' so log scans don't see a blank."""
        exc = CalTopoRateLimitError(
            "CalTopo rate limited at /api/v1/map/ABC/Marker "
            "(retry_after=none)"
        )
        msg = str(exc)
        assert "retry_after=none" in msg

    def test_raise_and_catch_via_runtime_error_fallback(self):
        """Belt-and-braces: a caller that uses the generic fallback
        ``except RuntimeError`` (e.g., a future helper that doesn't
        import CalTopoRateLimitError) must still catch a 429 — same
        path the legacy /create-map fallback exercises in production."""
        try:
            raise CalTopoRateLimitError("CalTopo rate limited at /path (retry_after=5)")
        except RuntimeError as exc:
            assert "rate limited" in str(exc)
        else:
            assert False, "RuntimeError fallback failed to catch CalTopoRateLimitError"


# ---------------------------------------------------------------------------
# Mirror of CalTopoOrphanMapError — batch-3 PR-H.2
# ---------------------------------------------------------------------------
# Pins the class-hierarchy + attribute contract for the orphan-map
# typed exception. The /create-map handler in main.py adds a specific
# ``except CalTopoOrphanMapError`` branch that returns 502 with the
# partial_map_id in the detail so the dispatcher can quote it when
# asking for manual cleanup. The app deliberately does NOT have
# CalTopo DELETE privileges (per Bill 2026-05-30) — orphan maps sit
# on the team account until manually deleted via CalTopo UI.

class CalTopoOrphanMapError(RuntimeError):
    """Mirror of caltopo.CalTopoOrphanMapError."""
    def __init__(self, message, *, partial_map_id, markers_added,
                 markers_intended, failure_step):
        super().__init__(message)
        self.partial_map_id = partial_map_id
        self.markers_added = markers_added
        self.markers_intended = markers_intended
        self.failure_step = failure_step


class TestCalTopoOrphanMapErrorContract:
    """Pin PR-H.2 class-hierarchy + attribute contract for orphan-map
    handling.

    Drift caught by these tests:
      - CalTopoOrphanMapError no longer subclasses RuntimeError (would
        bypass the generic fallback in main.py /create-map)
      - Required attributes (partial_map_id, markers_added,
        markers_intended, failure_step) renamed or removed (would
        break the handler that interpolates them into the 502 detail)
      - Constructor signature changes (would break callers in
        caltopo.py::build_incident_map)
    """

    def test_is_runtime_error_subclass(self):
        """Generic `except RuntimeError` fallback in main.py /create-map
        keeps catching CalTopoOrphanMapError if the specific branch
        is ever removed or ordering changes. Defensive contract."""
        assert issubclass(CalTopoOrphanMapError, RuntimeError), (
            "CalTopoOrphanMapError MUST subclass RuntimeError so the "
            "generic ``except RuntimeError`` fallback in main.py "
            "/create-map keeps catching it as a safety net. See PR-H.2."
        )

    def test_required_attributes_present(self):
        """The /create-map handler interpolates partial_map_id,
        markers_added, markers_intended, failure_step into the 502
        detail. Renaming any of these breaks the dispatcher-facing
        error message."""
        exc = CalTopoOrphanMapError(
            "test", partial_map_id="ABC123",
            markers_added=3, markers_intended=7,
            failure_step="staging[3] (ReadTimeout)",
        )
        assert exc.partial_map_id == "ABC123"
        assert exc.markers_added == 3
        assert exc.markers_intended == 7
        assert exc.failure_step == "staging[3] (ReadTimeout)"

    def test_message_includes_map_id_for_log_triage(self):
        """The exception message itself (not just the attribute) should
        include the map_id so a logger.warning() that just renders
        type(exc).__name__ + str(exc) still surfaces the ID in Cloud
        Run logs."""
        exc = CalTopoOrphanMapError(
            "CalTopo map partially created — orphan map id: ABC123. "
            "Markers added: 3/7. Failure at: staging[3]. "
            "Manual cleanup required.",
            partial_map_id="ABC123",
            markers_added=3, markers_intended=7,
            failure_step="staging[3]",
        )
        msg = str(exc)
        assert "orphan map id: ABC123" in msg
        assert "Markers added: 3/7" in msg
        assert "Manual cleanup required" in msg

    def test_raise_and_catch_via_runtime_error_fallback(self):
        """Belt-and-braces: a caller that uses the generic fallback
        ``except RuntimeError`` must still catch the orphan-map case.
        Same path the legacy /create-map fallback exercises in
        production."""
        try:
            raise CalTopoOrphanMapError(
                "CalTopo map partially created — orphan map id: XYZ.",
                partial_map_id="XYZ",
                markers_added=2, markers_intended=5,
                failure_step="residence_marker",
            )
        except RuntimeError as exc:
            assert "orphan map id" in str(exc)
        else:
            assert False, "RuntimeError fallback failed to catch CalTopoOrphanMapError"


class TestCalTopoOrphanRespectsRateLimitRouting:
    """Pin self-review pass HIGH finding on PR-H.2: a 429 raised by a
    marker POST inside build_incident_map's try block MUST propagate
    unwrapped (as CalTopoRateLimitError), NOT get absorbed into
    CalTopoOrphanMapError. Otherwise /create-map's typed 503+Retry-After
    routing is lost and the dispatcher sees the orphan alert (502)
    instead of the transient-retry alert (503).

    Pre-fix (caught by self-review before this commit): the
    ``except Exception`` branch absorbed CalTopoRateLimitError (since
    it's a RuntimeError → Exception). The orphan branch in /create-map
    fired instead of the rate-limit branch.

    Fix: explicit ``except CalTopoRateLimitError: raise`` BEFORE the
    generic ``except Exception``. The 429 propagates unwrapped; the
    orphan WARNING is still logged for maintainer cleanup tracking.
    """

    def _read_build_incident_map_section(self) -> str:
        """Return the build_incident_map function body. Window large
        enough to cover the function (the marker-loop comments + the
        two except clauses span ~200 lines — empirically the function
        is just under 15000 chars including comments)."""
        from pathlib import Path
        caltopo_py = Path(__file__).parent / "caltopo.py"
        source = caltopo_py.read_text()
        marker = "def build_incident_map("
        assert marker in source, "build_incident_map function not found"
        start = source.index(marker)
        return source[start:start + 15000]

    def test_build_incident_map_has_rate_limit_except_clause(self):
        """Source-scan: build_incident_map MUST contain an explicit
        ``except CalTopoRateLimitError:`` clause."""
        section = self._read_build_incident_map_section()
        assert "except CalTopoRateLimitError:" in section, (
            "build_incident_map must have an explicit "
            "`except CalTopoRateLimitError:` to let 429s propagate "
            "unwrapped — otherwise /create-map routes 429 to the "
            "orphan branch (502) instead of the rate-limit branch "
            "(503 + Retry-After). See PR-H.2 self-review pass."
        )

    def test_rate_limit_clause_body_raises_unwrapped(self):
        """The body of the CalTopoRateLimitError except clause MUST
        contain a bare `raise` statement — re-raising the 429 unwrapped
        so the outer /create-map handler routes it via its specific
        CalTopoRateLimitError branch. Constructing a new exception
        (e.g., `raise CalTopoOrphanMapError(...)`) would lose the
        original 429 type and break the routing."""
        section = self._read_build_incident_map_section()
        idx_clause = section.find("except CalTopoRateLimitError:")
        assert idx_clause != -1, "rate-limit except clause missing"
        # Use the exact Python-indent literal for the next except clause
        # so we don't accidentally match `except Exception` inside a
        # comment (which my own added comment in caltopo.py does — the
        # less-specific `find("except ")` matched the in-comment
        # reference and truncated the clause body).
        idx_next_except = section.find("        except Exception as exc:", idx_clause + 30)
        assert idx_next_except != -1, "next except clause missing"
        clause_body = section[idx_clause:idx_next_except]
        # Must contain the bare-raise literal (4 chars: 'raise' + a
        # newline OR a comment marker). A re-raise of a new exception
        # would be `raise X(...)` with an identifier and open-paren
        # after 'raise'.
        assert ("raise\n" in clause_body or "raise " in clause_body), (
            "except CalTopoRateLimitError clause body MUST contain "
            "`raise` (followed by newline or comment, not by `(` or "
            "an exception class). Constructing a new exception would "
            "lose the original 429 type and break the routing. See "
            "PR-H.2 self-review pass."
        )
        # Defensive: must NOT contain `raise CalTopoOrphan` (would
        # re-wrap the 429, defeating the fix).
        assert "raise CalTopoOrphan" not in clause_body, (
            "except CalTopoRateLimitError clause body MUST NOT raise "
            "CalTopoOrphanMapError — that would re-wrap the 429 and "
            "lose the 503-routing semantics this clause exists to "
            "preserve. See PR-H.2 self-review pass."
        )

    def test_rate_limit_passthrough_precedes_generic_except(self):
        """The CalTopoRateLimitError passthrough MUST come BEFORE
        the generic except Exception — order matters. Python
        evaluates except clauses top-down; the first match wins.
        If generic is first, CalTopoRateLimitError gets caught as
        Exception and wrapped into CalTopoOrphanMapError."""
        section = self._read_build_incident_map_section()
        idx_ratelimit = section.find("except CalTopoRateLimitError")
        idx_generic = section.find("except Exception as exc:")
        assert idx_ratelimit != -1 and idx_generic != -1, (
            "Both except clauses must exist in build_incident_map"
        )
        assert idx_ratelimit < idx_generic, (
            "except CalTopoRateLimitError MUST precede except Exception. "
            "If reordered, the 429 routing is lost. See PR-H.2."
        )


from pathlib import Path

# ---------------------------------------------------------------------------
# Transient 5xx retry (live 2026-08-01: CalTopo 502 orphaned map LG1J0A2)
# ---------------------------------------------------------------------------

_CALTOPO_MAX_ATTEMPTS = 3
_CALTOPO_RETRY_BACKOFF_S = (0.5, 1.5)


def _retry_decision(status_code, attempt, max_attempts=_CALTOPO_MAX_ATTEMPTS):
    """MIRROR of the response-handling branch in caltopo.py::_post.

    Returns "ok" | "rate_limit" | "retry" | "raise". Kept to the DECISION only;
    the streaming/size-bound machinery around it is unchanged by the retry and
    is not re-mirrored here.
    """
    if status_code == 429:
        return "rate_limit"
    if 200 <= status_code < 300:
        return "ok"
    is_last = attempt == max_attempts - 1
    if 500 <= status_code < 600 and not is_last:
        return "retry"
    return "raise"


def _run_attempts(statuses):
    """Drive the mirror over a sequence of upstream statuses; return
    (outcome, attempts_made)."""
    for attempt in range(_CALTOPO_MAX_ATTEMPTS):
        d = _retry_decision(statuses[min(attempt, len(statuses) - 1)], attempt)
        if d != "retry":
            return d, attempt + 1
    return "raise", _CALTOPO_MAX_ATTEMPTS


class TestCalTopoTransientRetry:
    """A map is 8 CalTopo calls and any one failure orphans the whole thing.

    Live on personal-dev 2026-08-01: 45 calls returned 200 and one returned
    502, 2.6 s after a successful create. Markers added 0/7, map LG1J0A2
    orphaned. The dispatcher's manual re-click succeeded immediately, which is
    what confirms the failure was transient and a retry is the right answer.
    """

    def test_the_live_failure_now_recovers(self):
        """502 then 200 — the exact shape seen on 2026-08-01."""
        assert _run_attempts([502, 200]) == ("ok", 2)

    def test_two_failures_then_success(self):
        assert _run_attempts([502, 503, 200]) == ("ok", 3)

    def test_persistent_5xx_still_raises_after_three_attempts(self):
        """Retry is bounded — a real CalTopo outage must surface, not spin."""
        assert _run_attempts([502]) == ("raise", 3)

    def test_4xx_is_not_retried(self):
        """Bill, 2026-08-01: leave 4xx alone. A 404/400 is a permanent
        integration failure and retrying only delays the error."""
        for code in (400, 401, 403, 404, 422):
            assert _run_attempts([code]) == ("raise", 1), code

    def test_429_short_circuits_ahead_of_the_retry(self):
        """Rate limiting is transient but must NOT be retried here — it raises
        CalTopoRateLimitError so /create-map can return a 503 + Retry-After.
        Retrying would fight the limiter and hide an actionable signal."""
        assert _run_attempts([429]) == ("rate_limit", 1)

    def test_success_makes_no_extra_calls(self):
        assert _run_attempts([200]) == ("ok", 1)

    def test_backoff_has_one_entry_per_retry(self):
        """Indexed by `attempt`, which reaches MAX_ATTEMPTS-2 on the last
        retry. A short tuple would raise IndexError on the very path the
        retry exists to serve."""
        assert len(_CALTOPO_RETRY_BACKOFF_S) == _CALTOPO_MAX_ATTEMPTS - 1


class TestCalTopoRetryProductionParity:
    """Tie the mirror above to backend/caltopo.py."""

    @staticmethod
    def _prod():
        return (Path(__file__).parent / "caltopo.py").read_text(encoding="utf-8")

    @classmethod
    def _post_fn(cls):
        m = re.search(r"^def _post\(.*?(?=\n\n(?:def |async def |# -{10,}))",
                      cls._prod(), re.DOTALL | re.MULTILINE)
        assert m, "_post not found in caltopo.py"
        return m.group(0)

    @staticmethod
    def _code_only(text):
        body = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
        return "\n".join(l.split("#")[0] for l in body.splitlines())

    def test_constants_match_the_mirror(self):
        src = self._prod()
        m = re.search(r"^_CALTOPO_MAX_ATTEMPTS\s*=\s*(\d+)\s*$", src, re.M)
        assert m and int(m.group(1)) == _CALTOPO_MAX_ATTEMPTS, (
            "caltopo.py and this mirror disagree on the attempt count"
        )
        b = re.search(r"^_CALTOPO_RETRY_BACKOFF_S\s*=\s*\(([^)]*)\)", src, re.M)
        assert b, "_CALTOPO_RETRY_BACKOFF_S is gone from caltopo.py"
        vals = tuple(float(x) for x in b.group(1).split(",") if x.strip())
        assert vals == _CALTOPO_RETRY_BACKOFF_S, (
            f"backoff drifted: caltopo.py {vals} vs mirror {_CALTOPO_RETRY_BACKOFF_S}"
        )

    def test_retry_is_bounded_to_5xx_and_not_the_last_attempt(self):
        code = self._code_only(self._post_fn())
        assert "500 <= resp.status_code < 600 and not _is_last" in code, (
            "the retry is no longer scoped to 5xx-and-not-final — either 4xx is "
            "being retried, or the bound is gone and a real outage spins."
        )

    def test_the_loop_actually_exists(self):
        code = self._code_only(self._post_fn())
        assert "for attempt in range(_CALTOPO_MAX_ATTEMPTS)" in code, (
            "the retry loop is gone from _post — a single transient 5xx orphans "
            "the whole map again."
        )

    def test_429_is_raised_before_the_retry_branch(self):
        """Order is behaviour: a 429 reaching the 5xx branch would be retried
        (it is not 5xx, so it would fall to `raise`) but more importantly the
        CalTopoRateLimitError must keep its distinct type for the 503 path."""
        code = self._code_only(self._post_fn())
        assert code.index("CalTopoRateLimitError") < code.index("_is_last"), (
            "the 429 short-circuit no longer precedes the retry decision"
        )

    def test_sleep_happens_outside_the_response_context(self):
        """Sleeping inside `with client.stream(...)` holds the connection open
        for the whole backoff."""
        code = self._code_only(self._post_fn())
        assert re.search(r"^        if backoff_s is None:\n            break\n", code, re.M), (
            "the break-on-success guard moved; the sleep may now run inside the "
            "response context manager."
        )
        assert code.index("time.sleep(backoff_s)") > code.index("if backoff_s is None:"), (
            "time.sleep no longer follows the context-manager exit"
        )
