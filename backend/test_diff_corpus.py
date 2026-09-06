"""
test_diff_corpus.py — tests for backend/migration_validation/diff_corpus.py
_majority(), the issue #334 fix for the arbitrary-tie-breaker bug.

Without the strict >50% threshold, three unique runs would yield an arbitrary
"winner" via max(set, key=count) and the diff would compare two arbitrary
winners between SDKs — flagging drift that doesn't exist. The fix returns
None when there's no actual majority, and the caller treats None as no signal.
"""

from backend.migration_validation.diff_corpus import _majority


def test_majority_clear_winner():
    """Standard majority case — 2 of 3 runs agree."""
    assert _majority(["A", "A", "B"]) == "A"


def test_majority_unanimous():
    """All runs agree → that value is the majority."""
    assert _majority(["A", "A", "A"]) == "A"


def test_majority_three_unique_returns_none():
    """The actual #334 bug: 3 unique values would have picked an arbitrary winner.

    Now returns None ("no signal") so the caller can skip the comparison
    instead of declaring false-positive drift between two arbitrary picks.
    """
    assert _majority(["A", "B", "C"]) is None


def test_majority_two_pair_tie_returns_none():
    """[A, A, B, B] — neither value clears 50%, so no majority."""
    assert _majority(["A", "A", "B", "B"]) is None


def test_majority_empty_list_returns_none():
    """Defensive: empty input is None, not an exception."""
    assert _majority([]) is None


def test_majority_single_run_returns_value():
    """One run → that value is trivially >50%."""
    assert _majority(["only"]) == "only"


def test_majority_none_is_a_valid_majority_value():
    """If 2 of 3 runs returned None (field missing), None IS the majority.

    Note: _majority returns None both for "no majority" AND "majority is
    literally None" — they're indistinguishable at this layer. The caller
    in diff_form() resolves the ambiguity conservatively: if either side's
    majority is None (for any reason), we skip the comparison rather than
    flag drift. That's the right call here: a high-extraction-noise SDK
    shouldn't be diff-flagged against a stable one — the noise is captured
    in the per-run arrays in the report.
    """
    assert _majority([None, None, "A"]) is None  # majority IS None (count=2 > 1.5)


def test_majority_strict_threshold_two_of_four():
    """4-run case: 2 of 4 is exactly 50%, NOT majority. Returns None."""
    assert _majority(["A", "A", "B", "C"]) is None


def test_majority_strict_threshold_three_of_four():
    """4-run case: 3 of 4 IS majority (75% > 50%)."""
    assert _majority(["A", "A", "A", "B"]) == "A"
