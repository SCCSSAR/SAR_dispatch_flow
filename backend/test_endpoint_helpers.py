"""test_endpoint_helpers.py — pure-logic tests for the helpers used by the
Task 1.10 Everbridge + Slack endpoints.

Per CLAUDE.md test file pattern: mirror the pure-logic functions from
`backend/main.py` locally rather than importing the module directly.
`backend/main.py` has heavyweight GCP / Vertex AI / httpx / google-api
dependencies not installed in local pytest.

When updating the helpers in main.py, ALSO update the mirror here. The
mirror IS the test contract — drift surfaces in production behavior,
and these tests are the regression boundary.

Test coverage (Task 1.10a):
  - _category_for() — template_type → Phase 0 Everbridge category ID
  - _is_group() / _strip_target_prefix() — frontend prefix scheme
  - _compose_event_name_with_hhmm() — Pacific-time HHMM uniqueness suffix
  - _slugify_for_firestore() — canonical Firestore doc ID

The 6 endpoint scaffolds themselves (gate behavior, 501 stubs) are NOT
exercised here — that requires the FastAPI TestClient + importing
backend.main, which transitively pulls in httpx/google.genai/googleapiclient
that aren't in the local env. Gating is verified by:
  1. test_feature_flags.py — the underlying _feature_enabled() function
     is exhaustively tested via local mirror (Task 1.4)
  2. Live test on personal-dev (flags ON → 501) and SCCSSAR-dev (flags
     OFF → 503) once Task 1.10a deploys.
End of Phase 1 (post Task 1.11): revisit with requirements-test.txt +
venv-based pytest env so TestClient tests can be added too.
"""
import datetime
import zoneinfo

import pytest


# ---------------------------------------------------------------------------
# Mirrored helpers — must be kept in sync with backend/main.py
# ---------------------------------------------------------------------------

# main.py — module-level constant (Phase 0 verbatim category IDs)
_TEMPLATE_TYPE_TO_CATEGORY: dict[str, int] = {
    "incounty":  7000000000000003,
    "mutualaid": 7000000000000004,
}


def _category_for(template_type: str) -> int:
    """Mirror of backend/main.py::_category_for()."""
    try:
        return _TEMPLATE_TYPE_TO_CATEGORY[template_type]
    except KeyError:
        raise ValueError(
            f"Unrecognized template_type: {template_type!r}. "
            f"Expected one of: {sorted(_TEMPLATE_TYPE_TO_CATEGORY)}"
        )


def _is_group(target_id: str) -> bool:
    """Mirror of backend/main.py::_is_group()."""
    return target_id.startswith("g:")


def _strip_target_prefix(target_id: str) -> str:
    """Mirror of backend/main.py::_strip_target_prefix()."""
    for prefix in ("g:", "c:"):
        if target_id.startswith(prefix):
            return target_id[len(prefix):]
    return target_id


def _compose_event_name_with_hhmm(
    event_name_human: str,
    *,
    now_utc: datetime.datetime | None = None,
) -> str:
    """Mirror of backend/main.py::_compose_event_name_with_hhmm()."""
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    pacific = now_utc.astimezone(zoneinfo.ZoneInfo("America/Los_Angeles"))
    return f"{event_name_human} {pacific.strftime('%H%M')}"


def _slugify_for_firestore(event_name_with_hhmm: str) -> str:
    """Mirror of backend/main.py::_slugify_for_firestore()."""
    return "_".join(event_name_with_hhmm.lower().split())


def _coerce_selected_target_ids(raw) -> list[str]:
    """Mirror of backend/main.py::_coerce_selected_target_ids().

    Validate the `selected_target_ids` field from the request body. The
    frontend always sends a JSON array of strings, but a malformed or
    malicious client could send a string, dict, or list-of-mixed-types.
    Without this guard the downstream call `_route_send(selected_target_ids)`
    would iterate a string character-by-character (silent corruption) or
    raise a generic AttributeError 500 on a dict.

    Returns the coerced list. Raises ValueError with a specific message
    on bad shape — caller translates to 400.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("selected_target_ids must be a list")
    if not all(isinstance(t, str) for t in raw):
        raise ValueError("selected_target_ids elements must be strings")
    return raw


def _content_length_exceeds(header_value, max_bytes: int) -> bool:
    """Mirror of backend/main.py::_content_length_exceeds().

    Best-effort pre-check for oversized requests using the client-supplied
    Content-Length header. Returns True iff the header is present, parses
    to a valid integer, and exceeds max_bytes. Returns False on missing or
    malformed headers — those cases fall through to the post-parse
    authoritative length check at the call site.

    The header value is intentionally typed loosely (str | None) to match
    the result of `request.headers.get("content-length")` so call sites
    can be a single-line guard.
    """
    if header_value is None:
        return False
    try:
        return int(header_value) > max_bytes
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# _category_for() — template_type → Phase 0 verbatim category ID
# ---------------------------------------------------------------------------

class TestCategoryFor:
    def test_incounty_maps_to_phase_0_id(self):
        # Phase 0 Org-Wide Constants verbatim — same value pinned by
        # test_main_regression.py + test_everbridge.py.
        assert _category_for("incounty") == 7000000000000003

    def test_mutualaid_maps_to_phase_0_id(self):
        assert _category_for("mutualaid") == 7000000000000004

    def test_unrecognized_raises_value_error_with_helpful_message(self):
        # Fail loud rather than silently picking a default — the wrong
        # category would page the wrong group of responders. The error
        # message includes the expected values so the dispatcher (or
        # operator reading logs) sees what the choices are.
        with pytest.raises(ValueError, match="Unrecognized template_type"):
            _category_for("emergency")
        with pytest.raises(ValueError, match="incounty"):
            _category_for("emergency")
        with pytest.raises(ValueError, match="mutualaid"):
            _category_for("emergency")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _category_for("")

    def test_case_sensitive(self):
        # Frontend MUST send lowercase. Capitalized variants are a
        # frontend bug — fail loud rather than silently lowercase.
        with pytest.raises(ValueError):
            _category_for("InCounty")
        with pytest.raises(ValueError):
            _category_for("INCOUNTY")


# ---------------------------------------------------------------------------
# _is_group() / _strip_target_prefix() — frontend prefix scheme
# ---------------------------------------------------------------------------

class TestIsGroup:
    def test_group_prefix_detected(self):
        assert _is_group("g:700000000000033") is True

    def test_contact_prefix_detected_as_not_group(self):
        assert _is_group("c:1234567890") is False

    def test_no_prefix_treated_as_not_group(self):
        # Defensive — if the frontend somehow forgets the prefix, the
        # ID is treated as a contact ID, which means /send-notification
        # will route it through `target_contact_ids`. That's the safer
        # of the two failure modes (paging an individual is recoverable;
        # accidentally paging a whole group is not).
        assert _is_group("700000000000033") is False

    def test_empty_string(self):
        assert _is_group("") is False


class TestStripTargetPrefix:
    def test_strips_group_prefix(self):
        assert _strip_target_prefix("g:700000000000033") == "700000000000033"

    def test_strips_contact_prefix(self):
        assert _strip_target_prefix("c:1234567890") == "1234567890"

    def test_no_prefix_passthrough(self):
        assert _strip_target_prefix("700000000000033") == "700000000000033"

    def test_only_strips_first_prefix(self):
        # Defensive — `g:c:123` is malformed but should only strip ONE
        # prefix (left-most match). Caller decides what to do with the
        # remainder.
        assert _strip_target_prefix("g:c:123") == "c:123"


# ---------------------------------------------------------------------------
# _compose_event_name_with_hhmm() — Pacific HHMM uniqueness suffix
# ---------------------------------------------------------------------------

class TestComposeEventNameWithHhmm:
    def _utc(self, *args, **kwargs) -> datetime.datetime:
        return datetime.datetime(*args, **kwargs, tzinfo=datetime.timezone.utc)

    def test_pacific_standard_time_winter(self):
        # 2026-01-15 18:30 UTC = 2026-01-15 10:30 PST → "1030"
        # Pinned in winter (PST = UTC-8) so DST changes don't break this.
        out = _compose_event_name_with_hhmm(
            "2026-01-15 MPD CALAVERAS",
            now_utc=self._utc(2026, 1, 15, 18, 30),
        )
        assert out == "2026-01-15 MPD CALAVERAS 1030"

    def test_pacific_daylight_time_summer(self):
        # 2026-07-04 22:00 UTC = 2026-07-04 15:00 PDT → "1500"
        # Pinned in summer (PDT = UTC-7) — confirms zoneinfo handles DST.
        out = _compose_event_name_with_hhmm(
            "2026-07-04 MPD INDEPENDENCE",
            now_utc=self._utc(2026, 7, 4, 22, 0),
        )
        assert out == "2026-07-04 MPD INDEPENDENCE 1500"

    def test_zero_padded_minute(self):
        # 2026-04-25 17:05 UTC = 2026-04-25 10:05 PDT (April → DST → UTC-7)
        out = _compose_event_name_with_hhmm(
            "2026-04-25 MPD CALAVERAS",
            now_utc=self._utc(2026, 4, 25, 17, 5),
        )
        assert out == "2026-04-25 MPD CALAVERAS 1005"

    def test_zero_padded_hour(self):
        # 0:30 PT (after DST start, April → PDT → UTC-7) = 7:30 UTC
        out = _compose_event_name_with_hhmm(
            "2026-04-25 MPD CALAVERAS",
            now_utc=self._utc(2026, 4, 25, 7, 30),
        )
        assert out == "2026-04-25 MPD CALAVERAS 0030"

    def test_midnight_pacific(self):
        # 2026-04-25 07:00 UTC = 2026-04-25 00:00 PDT → "0000"
        out = _compose_event_name_with_hhmm(
            "X",
            now_utc=self._utc(2026, 4, 25, 7, 0),
        )
        assert out == "X 0000"

    def test_default_now_utc_does_not_crash(self):
        # Smoke test for the default-arg path — exercise the live clock.
        out = _compose_event_name_with_hhmm("2026-04-25 MPD CALAVERAS")
        assert out.startswith("2026-04-25 MPD CALAVERAS ")
        # Trailing token is exactly 4 digits.
        suffix = out.rsplit(" ", 1)[1]
        assert len(suffix) == 4
        assert suffix.isdigit()


# ---------------------------------------------------------------------------
# _slugify_for_firestore() — canonical Firestore doc ID
# ---------------------------------------------------------------------------

class TestSlugifyForFirestore:
    def test_basic_lowercase_underscores(self):
        # Same canonical key as the Slack channel name — keeps cross-app
        # identity 1:1 in the non-collision case.
        assert _slugify_for_firestore("2026-04-25 MPD CALAVERAS 1430") == \
            "2026-04-25_mpd_calaveras_1430"

    def test_collapses_extra_whitespace(self):
        # split()+join handles arbitrary internal whitespace cleanly.
        assert _slugify_for_firestore("2026-04-25  MPD   CALAVERAS 1430") == \
            "2026-04-25_mpd_calaveras_1430"

    def test_already_lowercase_passthrough(self):
        # Firestore is case-sensitive; an already-canonical input stays
        # canonical (idempotent).
        assert _slugify_for_firestore("2026-04-25 mpd calaveras 1430") == \
            "2026-04-25_mpd_calaveras_1430"

    def test_punctuation_in_street_name_preserved(self):
        # Apostrophes, hyphens, etc. in California street names ARE legal
        # Firestore doc-ID characters (Firestore rejects only / and a few
        # control chars). We don't strip them — keeping them preserves
        # round-trip readability.
        assert _slugify_for_firestore("2026-04-25 MPD O'CONNOR 1430") == \
            "2026-04-25_mpd_o'connor_1430"

    def test_idempotent(self):
        # Double-slugify is a no-op — useful invariant if a future caller
        # accidentally double-applies the helper.
        once = _slugify_for_firestore("2026-04-25 MPD CALAVERAS 1430")
        assert _slugify_for_firestore(once) == once


# _coerce_selected_target_ids() — request body shape guard
# ---------------------------------------------------------------------------

class TestCoerceSelectedTargetIds:
    def test_happy_path_list_of_strings(self):
        out = _coerce_selected_target_ids(["c:123", "g:456"])
        assert out == ["c:123", "g:456"]

    def test_empty_list_passes_through(self):
        # The downstream `if not selected_target_ids: raise 400` handles
        # the empty case — this helper only validates shape, not emptiness.
        assert _coerce_selected_target_ids([]) == []

    def test_none_normalizes_to_empty_list(self):
        # Body parsing produces None when the key is missing. The original
        # code used `body.get(...) or []` for this — keep the behavior.
        assert _coerce_selected_target_ids(None) == []

    def test_string_input_rejected(self):
        # Without this guard, `for tid in "c:123"` iterates "c", ":", "1",
        # "2", "3" and silently corrupts downstream routing.
        with pytest.raises(ValueError, match="must be a list"):
            _coerce_selected_target_ids("c:123")

    def test_dict_input_rejected(self):
        # Without this guard, `_route_send` raises AttributeError or
        # iterates dict keys — both are confusing 500-class failures.
        with pytest.raises(ValueError, match="must be a list"):
            _coerce_selected_target_ids({"c:123": True})

    def test_integer_input_rejected(self):
        with pytest.raises(ValueError, match="must be a list"):
            _coerce_selected_target_ids(42)

    def test_list_with_non_string_element_rejected(self):
        # Frontend bug or malicious client sending a list of ints — caught
        # at the boundary rather than crashing in _strip_target_prefix.
        with pytest.raises(ValueError, match="elements must be strings"):
            _coerce_selected_target_ids(["c:123", 456])

    def test_list_with_none_element_rejected(self):
        with pytest.raises(ValueError, match="elements must be strings"):
            _coerce_selected_target_ids(["c:123", None])

    def test_list_with_nested_list_element_rejected(self):
        with pytest.raises(ValueError, match="elements must be strings"):
            _coerce_selected_target_ids([["c:123"]])


# ---------------------------------------------------------------------------
# _content_length_exceeds() — body-size pre-check guard
# ---------------------------------------------------------------------------

class TestContentLengthExceeds:
    def test_under_limit_returns_false(self):
        assert _content_length_exceeds("1000", 100_000) is False

    def test_at_limit_returns_false(self):
        # The check is `>` not `>=` — a body exactly at the limit is fine.
        assert _content_length_exceeds("100000", 100_000) is False

    def test_over_limit_returns_true(self):
        assert _content_length_exceeds("100001", 100_000) is True

    def test_missing_header_returns_false(self):
        # When the client omits Content-Length (common with chunked uploads),
        # the pre-check passes — the post-parse authoritative length check
        # catches oversized payloads.
        assert _content_length_exceeds(None, 100_000) is False

    def test_empty_string_returns_false(self):
        # An empty header is treated as missing.
        assert _content_length_exceeds("", 100_000) is False

    def test_malformed_header_returns_false(self):
        # Garbage Content-Length should not crash the request — the
        # post-parse length check is the authoritative enforcement.
        assert _content_length_exceeds("not-a-number", 100_000) is False

    def test_negative_header_treated_as_under_limit(self):
        # int("-5") < 100_000 → returns False. Defensive: a negative
        # Content-Length is malformed but never "exceeds" a positive limit.
        assert _content_length_exceeds("-5", 100_000) is False

    def test_huge_header_returns_true(self):
        # Defends against the client claiming a multi-GB body.
        assert _content_length_exceeds("99999999999", 100_000) is True

    def test_non_string_header_handled_defensively(self):
        # Defensive against unexpected types (the production path always
        # passes str|None from headers.get(), but exercise the safety net).
        # int(123456) = 123456 > 100_000 → True (int → int is permissive).
        assert _content_length_exceeds(123456, 100_000) is True
        # int([...]) raises TypeError → caught → False.
        assert _content_length_exceeds([1, 2, 3], 100_000) is False
