"""test_route_send.py — exhaustive matrix for the /send-notification routing decision.

Per CLAUDE.md test file pattern: mirror the pure-logic functions from
`backend/main.py` locally rather than importing the module directly.
`backend/main.py` has heavyweight GCP / Vertex AI / httpx / google-api
dependencies not installed in local pytest.

When updating the routing logic in main.py, ALSO update the mirror here.
The mirror IS the test contract — drift surfaces in production behavior,
and these tests are the regression boundary.

Design Section 4 §3 + integration plan Task 1.9. The mirror differs from
main.py in one stylistic respect: the mirror takes `everbridge_mode` as
an explicit parameter so each test can pin its own combo cleanly. main.py
reads `_EVERBRIDGE_MODE` from a module-level constant (loaded from env
var at import time). The two implementations are otherwise identical;
the body-of-the-function logic IS the contract.
"""
import dataclasses
import pytest


# ---------------------------------------------------------------------------
# Mirrored data shape + helpers — must be kept in sync with backend/main.py
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class SendDecision:
    action: str            # 'send_live' | 'send_draft'
    target_ids: list[str]


def _load_safe_list_from_dict(safe: dict | None) -> dict:
    """Mirror of the defensive backfill in main.py::_load_safe_list_secret().

    Tests pass the parsed dict directly (rather than going through the
    DISPATCH_SAFE_LIST env var) so the test arrange step is one line.
    """
    if not safe:
        return {
            "allowed_contact_ids": [],
            "allowed_group_ids":   [],
            "allowed_emails":      [],
            "label_overrides":     {},
        }
    parsed = dict(safe)
    parsed.setdefault("allowed_contact_ids", [])
    parsed.setdefault("allowed_group_ids",   [])
    parsed.setdefault("allowed_emails",      [])
    parsed.setdefault("label_overrides",     {})
    return parsed


def _route_send(
    *,
    everbridge_mode: str,
    safe: dict | None,
    selected_target_ids: list[str],
) -> SendDecision:
    """Mirror of backend/main.py::_route_send().

    Differences from main.py:
      - everbridge_mode is a parameter (main.py reads module constant)
      - safe is a pre-loaded dict (main.py calls _load_safe_list_secret
        which reads + parses DISPATCH_SAFE_LIST). The pure routing logic
        is identical.
    """
    if everbridge_mode == "full":
        return SendDecision(
            action="send_live",
            target_ids=list(selected_target_ids),
        )
    safe = _load_safe_list_from_dict(safe)
    allowed = (
        set(safe.get("allowed_contact_ids", []))
        | set(safe.get("allowed_group_ids", []))
    )
    # Strip frontend prefix (`c:` for contacts, `g:` for groups) before the
    # membership check — safe-list secret stores RAW Everbridge IDs, the
    # frontend sends prefixed IDs. Live test 2026-04-27 surfaced the bug.
    all_safe = all(_strip_prefix_for_route_send(tid) in allowed for tid in selected_target_ids)
    return SendDecision(
        action="send_live" if all_safe else "send_draft",
        target_ids=list(selected_target_ids),
    )


def _strip_prefix_for_route_send(target_id: str) -> str:
    """Mirror of backend/main.py::_strip_target_prefix() — local copy here
    rather than importing from test_endpoint_helpers, so test_route_send
    stays single-file readable."""
    for prefix in ("g:", "c:"):
        if target_id.startswith(prefix):
            return target_id[len(prefix):]
    return target_id


# ---------------------------------------------------------------------------
# EVERBRIDGE_MODE=full — unconditional live, safe-list NOT consulted
# ---------------------------------------------------------------------------

class TestRouteSendModeFull:
    def test_full_mode_unconditional_live(self):
        # Full mode bypasses the safe-list — any target ID, including ones
        # the dispatcher just typed in by hand, results in send_live.
        decision = _route_send(
            everbridge_mode="full",
            safe=None,                       # safe list MUST NOT be consulted
            selected_target_ids=["random_target_id"],
        )
        assert decision.action == "send_live"
        assert decision.target_ids == ["random_target_id"]

    def test_full_mode_with_populated_safe_list_still_unconditional(self):
        # Even when a safe list exists, full mode does NOT filter against it.
        # The dispatcher's selection is sent live as-is.
        decision = _route_send(
            everbridge_mode="full",
            safe={"allowed_contact_ids": ["c1"]},
            selected_target_ids=["OFF_LIST"],
        )
        assert decision.action == "send_live"
        assert decision.target_ids == ["OFF_LIST"]

    def test_full_mode_empty_selection_passes_through(self):
        # Defensive — empty selection is a frontend bug but must not crash.
        decision = _route_send(
            everbridge_mode="full", safe=None, selected_target_ids=[],
        )
        assert decision.action == "send_live"
        assert decision.target_ids == []


# ---------------------------------------------------------------------------
# EVERBRIDGE_MODE=safe — every selected target must be in the safe list
# ---------------------------------------------------------------------------

class TestRouteSendModeSafe:
    def test_all_safe_targets_send_live(self):
        # Every selected target is in allowed_contact_ids ∪ allowed_group_ids
        # → unanimously safe → send_live.
        decision = _route_send(
            everbridge_mode="safe",
            safe={
                "allowed_contact_ids": ["c1", "c2"],
                "allowed_group_ids":   ["g1"],
                "allowed_emails":      ["bill@sccssar.org"],   # Slack-side, ignored here
            },
            selected_target_ids=["c1", "c2", "g1"],
        )
        assert decision.action == "send_live"
        assert decision.target_ids == ["c1", "c2", "g1"]

    def test_any_off_safe_target_creates_draft(self):
        # Single off-safe ID drafts the entire send. No partial-live: that
        # would create per-recipient inconsistency that's hard to reason
        # about during an incident.
        decision = _route_send(
            everbridge_mode="safe",
            safe={"allowed_contact_ids": ["c1"]},
            selected_target_ids=["c1", "OFF_LIST"],
        )
        assert decision.action == "send_draft"
        assert decision.target_ids == ["c1", "OFF_LIST"]

    def test_empty_safe_list_drafts_everything(self):
        # Item 1 fail-closed: empty safe-list secret means every target is
        # off-safe → every safe-mode send drafts. This is the explicit
        # behavior on personal-dev when the secret is unset/empty.
        decision = _route_send(
            everbridge_mode="safe",
            safe=None,
            selected_target_ids=["c1"],
        )
        assert decision.action == "send_draft"

    def test_empty_selection_in_safe_mode_sends_live(self):
        # Vacuously safe — no IDs to check means "all" pass. This is a
        # fallback edge case (frontend should require at least one
        # target); the routing decision is well-defined.
        decision = _route_send(
            everbridge_mode="safe",
            safe={"allowed_contact_ids": ["c1"]},
            selected_target_ids=[],
        )
        assert decision.action == "send_live"
        assert decision.target_ids == []

    def test_group_ids_count_for_safe_check(self):
        # The allowlist is the union of contacts AND groups — group-only
        # selections work the same way.
        decision = _route_send(
            everbridge_mode="safe",
            safe={"allowed_group_ids": ["g1", "g2"]},
            selected_target_ids=["g1", "g2"],
        )
        assert decision.action == "send_live"


# ---------------------------------------------------------------------------
# Frontend prefix scheme — REGRESSION (live test 2026-04-27)
#
# The safe-list secret stores RAW Everbridge IDs ("700000000000028"). The
# frontend sends prefixed IDs ("c:700000000000028" for contacts, "g:..."
# for groups — see _is_group / _strip_target_prefix in Task 1.10a). The
# original Task 1.9 _route_send compared the prefixed selection against the
# unprefixed allowlist directly — every safe-list contact was incorrectly
# drafted (because "c:700000000000028" never matched "700000000000028").
# Surfaced by the first live test of /send-notification on personal-dev:
# safe-list contact got drafted instead of live-sent.
# ---------------------------------------------------------------------------

class TestPrefixedTargetsAgainstUnprefixedSafeList:
    def test_live_shape_safe_list_contact_returns_live(self):
        # Mirrors the EXACT shape from the 2026-04-27 live test —
        # prefixed selection + unprefixed safe-list. After the fix, this
        # MUST return send_live.
        decision = _route_send(
            everbridge_mode="safe",
            safe={
                "allowed_contact_ids": ["700000000000028"],
                "allowed_group_ids":   [],
                "allowed_emails":      ["dispatcher@example.com"],
            },
            selected_target_ids=["c:700000000000028"],
        )
        assert decision.action == "send_live"
        # target_ids preserved verbatim — the prefix-stripping is for
        # safe-list comparison only, not for downstream EB API calls.
        assert decision.target_ids == ["c:700000000000028"]

    def test_live_shape_safe_list_group_returns_live(self):
        decision = _route_send(
            everbridge_mode="safe",
            safe={
                "allowed_contact_ids": [],
                "allowed_group_ids":   ["700000000000036"],
            },
            selected_target_ids=["g:700000000000036"],
        )
        assert decision.action == "send_live"

    def test_mixed_prefixes_all_safe_returns_live(self):
        decision = _route_send(
            everbridge_mode="safe",
            safe={
                "allowed_contact_ids": ["c1", "c2"],
                "allowed_group_ids":   ["g1"],
            },
            selected_target_ids=["c:c1", "c:c2", "g:g1"],
        )
        assert decision.action == "send_live"

    def test_one_off_safe_prefixed_id_drafts(self):
        # Bug-not-recurring sanity: a single off-safe prefixed contact
        # in an otherwise safe selection still drafts (no partial-live).
        decision = _route_send(
            everbridge_mode="safe",
            safe={"allowed_contact_ids": ["700000000000028"]},
            selected_target_ids=["c:700000000000028", "c:NOT_ON_SAFE_LIST"],
        )
        assert decision.action == "send_draft"

    def test_unprefixed_id_in_selection_still_works(self):
        # Defensive — backwards-compat with code paths that don't add
        # the prefix (test fixtures, hand-typed admin requests, etc.).
        # The strip is a no-op for IDs without the prefix.
        decision = _route_send(
            everbridge_mode="safe",
            safe={"allowed_contact_ids": ["c1"]},
            selected_target_ids=["c1"],
        )
        assert decision.action == "send_live"


# ---------------------------------------------------------------------------
# allowed_emails MUST NOT influence Everbridge routing (Item 1 unified safe-list)
# ---------------------------------------------------------------------------

class TestEmailsDoNotInfluenceEbRouting:
    """Regression test — _route_send() consults ONLY contact_ids + group_ids.

    The unified safe-list also carries `allowed_emails`, but those are the
    Slack-side allowlist (Task 1.10 channel-creation logic). Using emails
    in the EB routing decision would conflate "permitted Slack invitee"
    with "permitted EB target", which the design explicitly separates.
    """

    def test_emails_alone_do_not_grant_safe_status(self):
        # An email-as-target is never a real frontend payload (the frontend
        # sends contact IDs and group IDs only). But if one ever leaked
        # through, _route_send must treat it as off-safe — emails are NOT
        # in the EB allowlist set.
        decision = _route_send(
            everbridge_mode="safe",
            safe={
                "allowed_contact_ids": [],
                "allowed_group_ids":   [],
                "allowed_emails":      ["bill@sccssar.org"],   # Slack-side
            },
            selected_target_ids=["bill@sccssar.org"],
        )
        assert decision.action == "send_draft"

    def test_email_present_does_not_force_live(self):
        # An off-safe contact ID in the selection still drafts even when
        # the email allowlist is non-empty — emails play no role here.
        decision = _route_send(
            everbridge_mode="safe",
            safe={
                "allowed_contact_ids": ["c1"],
                "allowed_group_ids":   [],
                "allowed_emails":      ["bill@sccssar.org"],
            },
            selected_target_ids=["c1", "OFF_LIST"],
        )
        assert decision.action == "send_draft"


# ---------------------------------------------------------------------------
# Defensive backfill — _load_safe_list_from_dict()
# ---------------------------------------------------------------------------

class TestSafeListBackfill:
    def test_empty_dict_backfills_all_keys(self):
        out = _load_safe_list_from_dict({})
        assert out["allowed_contact_ids"] == []
        assert out["allowed_group_ids"]   == []
        assert out["allowed_emails"]      == []
        assert out["label_overrides"]     == {}

    def test_partial_dict_keeps_existing_and_backfills_rest(self):
        # The Secret Manager value is dispatcher-edited JSON — partial keys
        # are realistic. Backfill MUST preserve the keys that ARE there.
        out = _load_safe_list_from_dict({"allowed_contact_ids": ["c1"]})
        assert out["allowed_contact_ids"] == ["c1"]   # preserved
        assert out["allowed_group_ids"]   == []       # backfilled
        assert out["allowed_emails"]      == []
        assert out["label_overrides"]     == {}

    def test_none_treated_as_empty(self):
        out = _load_safe_list_from_dict(None)
        assert out["allowed_contact_ids"] == []


# ---------------------------------------------------------------------------
# Decision dataclass — frozen, comparable, immutable target_ids semantics
# ---------------------------------------------------------------------------

class TestSendDecision:
    def test_target_ids_is_a_copy_not_shared_reference(self):
        # Mutating the caller's list must NOT mutate the decision.
        ids = ["c1", "c2"]
        decision = _route_send(
            everbridge_mode="full", safe=None, selected_target_ids=ids,
        )
        ids.append("MUTATED")
        assert decision.target_ids == ["c1", "c2"]

    def test_decision_is_frozen(self):
        decision = _route_send(
            everbridge_mode="full", safe=None, selected_target_ids=["c1"],
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.action = "send_draft"   # type: ignore[misc]
