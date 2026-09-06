"""
CI floor that prevents return-value-discarding bugs from accreting in calls to
designated "must-capture" backend functions.

Background (2026-05 Melanie batch-2 review): clusters D + E surfaced multiple
sites where a ``bool`` / ``dict``-returning function was called as a bare
expression statement and its return value discarded. The dispatcher / oncall
got no diagnostic signal when those returns indicated a partial failure (e.g.
``end_notification(...) == False`` means "the EB notification was already
gone", not "stopped successfully").

Strategy: walk each backend module's AST. For every ``ast.Expr`` whose value
is an ``ast.Call`` (possibly wrapped in ``await``) targeting a name in
``MUST_CAPTURE``, count it as a "discarded return" hit. The
monotone-non-increasing ``BASELINE`` ratchet matches
``backend/test_pii_log_patterns.py``.

The scanner does NOT detect every shape of "discarded result" — only bare
expression statements. Wrapping a call inside ``functools.partial(...)`` or a
``loop.run_in_executor(None, ..., partial(...))`` is correctly NOT flagged
because the inner ``Call`` is an argument to an outer ``Call``, not a bare
``Expr``. The two known production call sites at ``backend/main.py``:5600
(``end_notification`` via ``run_in_executor``) and ``backend/d4h.py``:1492
(``mark_member_attending`` assigned to ``attendance_result``) are correctly
NOT hits.

Adding a new function to ``MUST_CAPTURE``: insert into the set + bump
``BASELINE`` to whatever count the scanner currently observes. Removing a
discard: lower ``BASELINE``.
"""

import ast
from pathlib import Path


BACKEND_FILES = [
    "main.py", "gemini.py", "everbridge.py", "slack.py", "caltopo.py",
    "gdocs.py", "d4h.py", "auth.py", "rate_limit.py",
]


# Functions whose return value carries failure-mode signal the caller MUST
# act on. Source: Melanie batch-2 cluster D + E findings (PRs #518, #523).
MUST_CAPTURE = frozenset({
    "end_notification",       # everbridge.py — bool: True=stopped, False=already-gone/non-JSON-200
    "mark_member_attending",  # d4h.py — dict with "status" key: success/member_not_found/already_attending
    "_send_dm_and_persist",   # main.py — bool: True=DM+patch succeeded, False=UC4 fired
                              # (send-time accumulates on True; poll-time logs on True)
})


# Established 2026-05-28. Cluster D + E cleared all known discarded sites.
# Any new bare-expression hit is a regression per CLAUDE.md Failure-mode
# Discipline question #3.
BASELINE = {name: 0 for name in MUST_CAPTURE}


def _call_target_name(call_node):
    """Resolve the leaf name of a ``Call``'s func, whether bare or attribute."""
    fn = call_node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _scan_file(path):
    """Yield (lineno, fn_name) for every bare-expression ``Call`` hitting MUST_CAPTURE."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        value = node.value
        # Unwrap a leading ``await`` — the bare statement ``await foo()`` also
        # discards the return.
        if isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call):
            continue
        name = _call_target_name(value)
        if name in MUST_CAPTURE:
            yield (node.lineno, name)


def _scan_backend():
    """Aggregate hit counts across BACKEND_FILES."""
    counts: dict[str, int] = {}
    backend_dir = Path(__file__).parent
    for f in BACKEND_FILES:
        path = backend_dir / f
        if not path.exists():
            continue
        for _lineno, name in _scan_file(path):
            counts[name] = counts.get(name, 0) + 1
    return counts


class TestDiscardedReturnFloor:
    """CI pin established 2026-05-28 (post Melanie batch-2).

    Bare-expression call sites of MUST_CAPTURE functions must stay at or below
    BASELINE. Lowering BASELINE is encouraged when a discard is removed.
    """

    def test_baseline_covers_must_capture(self):
        # Drift guard: any MUST_CAPTURE addition without a matching BASELINE
        # entry would silently un-cover the new function.
        assert set(BASELINE) == set(MUST_CAPTURE), (
            f"MUST_CAPTURE / BASELINE drift: "
            f"in MUST_CAPTURE only={sorted(set(MUST_CAPTURE) - set(BASELINE))}, "
            f"in BASELINE only={sorted(set(BASELINE) - set(MUST_CAPTURE))}"
        )

    def test_counts_match_baseline(self):
        observed_full = _scan_backend()
        observed = {k: observed_full.get(k, 0) for k in BASELINE}
        if observed != BASELINE:
            diff = []
            for k in sorted(BASELINE):
                if observed[k] != BASELINE[k]:
                    direction = "↑ ADDED" if observed[k] > BASELINE[k] else "↓ removed"
                    diff.append(
                        f"  {k}: baseline={BASELINE[k]} observed={observed[k]} ({direction})"
                    )
            raise AssertionError(
                "Discarded-return counts diverged from BASELINE.\n"
                "  ↑ If you ADDED a bare call — capture the return and act on\n"
                "    its failure modes per CLAUDE.md Failure-mode Discipline #3.\n"
                "  ↓ If you REMOVED a discard — update BASELINE in\n"
                "    backend/test_discarded_return_values.py to the new (lower) count.\n"
                "Diff:\n" + "\n".join(diff)
            )
