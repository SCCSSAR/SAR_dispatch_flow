"""
CI floor that prevents PII patterns from accreting in ``logger.*`` calls.

Background (2026-05 security review): a multi-source review found ~30 sites
across ``backend/main.py`` and helpers where ``logger.*`` calls interpolate
PII (addresses, lat/lng coordinates to ~1m precision, responder names). This
violates CLAUDE.md's "Critical Privacy & Security" hard rule:

    "No PII in logs — only latency, finish_reason, error type.
     No names, DOBs, addresses."

Strategy: this test counts current-known PII-pattern occurrences and asserts
that the counts do not grow. Cleanup PRs (PR-A.2 / A.3 / A.4) reduce the
``BASELINE`` values as offending lines are removed. When a count hits 0, that
pattern is fully cleared. A second test asserts that no new pattern category
appears — a new key in ``counts`` that isn't in ``BASELINE`` means a new
flavor of PII has crept in.

The scanner walks the Python AST rather than regexing raw source so that
string literals that happen to contain a PII variable's name — e.g.
``mode="address"`` in ``/apply-staging-override``, which is CORRECT redaction
— are not false-positive flagged.
"""

import ast
import re
from pathlib import Path


# Backend modules to scan. Tests + migration helpers excluded.
BACKEND_FILES = [
    "main.py", "gemini.py", "everbridge.py", "slack.py", "caltopo.py",
    "gdocs.py", "d4h.py", "auth.py", "rate_limit.py",
]

# Format-string patterns that indicate PII in a logger call's first arg.
# `addr|enriched|original` added 2026-05 (PR-A.3) after Pass B logs surfaced as
# false-negatives — those calls used `addr=%r` / `original=%r` / `enriched=%r`,
# none of which were in the original keyword list.
# `title|body` added 2026-05 (PR-A.5) after live-test surfaced caltopo.py marker
# logs leaking the staging address via `title=%r` and the CalTopo API error path
# leaking the response payload via `body=%s` (which echoes the marker title).
FORMAT_PATTERNS = {
    "lat_lng_precision":     re.compile(r"%\.[5-6]f"),
    "name_truncation":       re.compile(r"%\.\d+s"),
    "pii_var_in_format_str": re.compile(
        r"\b(query|address|addr|lkp|residence|canonical|officer|"
        r"enriched|original|title|body)\w*\s*=\s*%[rs]"
    ),
}

# Variable names whose Name-reference in logger args indicates PII.
# Strings that happen to contain these names (e.g. the literal "address" as
# a mode flag) are not flagged because the AST walker only inspects Name
# nodes — not string-literal contents.
PII_ARG_NAMES = frozenset({
    "address", "lkp_address", "residence_address",
    "res_address", "res_address_p1", "res_address_geocode",
    "residence_query", "geocode_query",
    "addr", "addr_part", "addr_geo",
    "canonical_name", "_officer_raw", "officer_raw",
    "gm_lat", "gm_lng", "res_lat", "res_lng",
})

# Baseline counts. PR-A.1 (2026-05) established this floor; each cleanup PR
# in cluster A reduces it.
#   PR-A.2 cleared the geocoder helpers (``main.py`` lines ~539-735),
#     -15 hits / 7 sites.
#   PR-A.3 cleared the inline /ocr pipeline (``main.py`` lines ~1549-3219),
#     -54 hits / 23 sites.
#   PR-A.4 cleared the responder-name leaks in /poll-incident,
#     -3 hits / 3 sites.
#   PR-A.5 cleared caltopo.py marker/title logs + d4h.py 4xx/5xx body logs,
#     surfaced by live-test of the Hostetter callout post-A.4. Scanner
#     keyword list extended with `title|body` to catch the class.
#     -5 sites: caltopo.py 124/246/287, d4h.py 630/643. CLUSTER A COMPLETE.
#
# Remaining 3 hits are STABLE FALSE POSITIVES — they match the scanner regex
# but the args are non-PII status flags / mode tags. Documented rather than
# refactored to avoid scope creep:
#   - main.py:3585 ("apply-staging-override | ... mode=%s") — the ``address``
#     Name reference is in the conditional test ``address is not None``, not in
#     the logged value (the mode tag string).
#   - main.py:3009 ("map_data built | lkp=%s residence=%s staging_count=%d") —
#     args are status strings ("yes"/"no"/"geocoded"), not address values.
#   - caltopo.py:439 ("CalTopo seed source | ... residence=%s ...") — args are
#     bool() flags, not address values.
BASELINE = {
    # Format-string patterns
    "lat_lng_precision":     0,
    "name_truncation":       0,
    "pii_var_in_format_str": 2,    # main.py:3009 + caltopo.py:439 (false positives)
    # Arg-reference patterns
    "responder_name_arg":    0,    # cluster A complete (was 3, cleared by PR-A.4)
    "address":               1,    # apply-staging-override conditional (false positive)
    "lkp_address":           0,
    "residence_query":       0,
    "geocode_query":         0,
    "canonical_name":        0,
    "_officer_raw":          0,
    "gm_lat":                0,
    "gm_lng":                0,
    "res_lat":               0,
    "res_lng":               0,
    "res_address_p1":        0,
    "res_address_geocode":   0,
}


def _arg_pii_names(arg_node):
    """Yield PII var names referenced as Name nodes anywhere in this arg's subtree.

    Catches bare ``address``, ``address[:80]``, ``f"{lkp_address}"``, etc.
    Does NOT match the string literal ``"address"`` (no Name node).
    """
    for sub in ast.walk(arg_node):
        if isinstance(sub, ast.Name) and sub.id in PII_ARG_NAMES:
            yield sub.id


def _is_ack_name_arg(arg_node):
    """True if arg is a ``<obj>.get("name")`` call shape."""
    if isinstance(arg_node, ast.Call) and isinstance(arg_node.func, ast.Attribute):
        if arg_node.func.attr == "get" and len(arg_node.args) >= 1:
            first = arg_node.args[0]
            if isinstance(first, ast.Constant) and first.value == "name":
                return True
    return False


def _scan_file(path):
    """Yield (lineno, hit_labels, fmt_string) for every flagged logger call."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        obj = node.func.value
        if not (isinstance(obj, ast.Name) and obj.id == "logger"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        fmt = (
            first.value
            if isinstance(first, ast.Constant) and isinstance(first.value, str)
            else ""
        )
        hits = []
        for label, pat in FORMAT_PATTERNS.items():
            if pat.search(fmt):
                hits.append(label)
        for a in node.args[1:]:
            if _is_ack_name_arg(a):
                hits.append("responder_name_arg")
            for name in _arg_pii_names(a):
                hits.append(name)
                break  # one PII-name hit per arg is enough
        if hits:
            yield (node.lineno, hits, fmt)


def _scan_backend():
    """Aggregate hit counts across BACKEND_FILES."""
    counts: dict[str, int] = {}
    backend_dir = Path(__file__).parent
    for f in BACKEND_FILES:
        path = backend_dir / f
        if not path.exists():
            continue
        for _lineno, hits, _fmt in _scan_file(path):
            for h in hits:
                counts[h] = counts.get(h, 0) + 1
    return counts


class TestPIILogPatternFloor:
    """CI pin established by PR-A.1 (2026-05 security review).

    Counts of PII patterns in ``logger.*`` calls must stay at or below
    BASELINE. Cleanup PRs reduce BASELINE values as offending lines are
    removed.
    """

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
                "PII log pattern counts diverged from BASELINE.\n"
                "  ↑ If you ADDED a logger.* call with PII — remove the PII argument.\n"
                "  ↓ If you REMOVED PII from a logger.* call — update BASELINE in\n"
                "    backend/test_pii_log_patterns.py to the new (lower) count.\n"
                "Diff:\n" + "\n".join(diff)
            )

    def test_no_new_pattern_categories(self):
        observed_full = _scan_backend()
        new_keys = set(observed_full) - set(BASELINE)
        assert not new_keys, (
            f"New PII pattern category appeared: {sorted(new_keys)}. "
            f"Either remove the PII from the new logger call, or — if the "
            f"category was reviewed and accepted — add it to BASELINE in "
            f"backend/test_pii_log_patterns.py."
        )
