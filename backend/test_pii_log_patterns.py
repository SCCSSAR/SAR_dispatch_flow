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
    # D4H off-call exclusion (2026-09): member/contact name carriers.
    "display_name", "excluded", "picked_off_call", "unmatched",
    "off_call", "_off_call_lines", "group_members",
    "plan", "off_call_plan", "d4h_event_log", "_d4h_dispatch_milestones",
})

# Key / attribute access that reads a person's name off a row or a plan:
# ``mc["display_name"]``, ``m.get("name")``, ``plan.excluded``. The AST walker
# cannot see these through PII_ARG_NAMES (the receiver is not a PII Name), so
# they are matched by the constant key or the attribute itself. A reference
# that sits inside a ``len(...)`` call is a COUNT, not the value, and is exempt
# — that is how _build_off_call_plan's summary line logs.
PII_KEY_CONSTS = frozenset({"name", "display_name", "firstName", "lastName", "ref"})
PII_ATTRS = frozenset({"excluded", "picked_off_call", "unmatched", "display_name", "__dict__"})
# Whole-object names: a bare reference is flagged (the repr carries names), but
# a field read such as ``plan.mode`` is judged by the field — its name-carrying
# fields are in PII_ATTRS. Scoped to these two so ``address.strip()`` and
# ``group_members.values()`` stay flagged by the plain Name rule.
PII_OBJECT_NAMES = frozenset({"plan", "off_call_plan"})

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
#
# Known limitations of the key/attribute extension (2026-09, off-call):
#   - ``plan`` / ``off_call_plan`` are caught as WHOLE objects only when the
#     variable is literally so named: ``logger.info("%s", plan)``, ``str(plan)``,
#     ``vars(plan)``, ``getattr(plan, "excluded")``, ``dataclasses.asdict(plan)``
#     and an f-string of it all flag; ``plan.mode`` does not. NOT caught, left
#     to review: the same shapes on an alias (``p = plan``) or on a member/
#     contact ROW logged whole (``str(mc)``, ``vars(m)``), and a name read
#     through a variable key (``m[key]``).
#   - ``["name"]`` / ``.get("name")`` / ``"ref"`` now also flag EB GROUP names
#     and OCEAN#s (zero sites today). A future legitimate ``g["name"]`` log is
#     a documented false positive to record HERE — never a reason to raise
#     BASELINE without one.
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
    # D4H off-call exclusion (2026-09): names may reach the Event Log, never a log.
    "display_name":          0,
    "excluded":              0,
    "picked_off_call":       0,
    "unmatched":             0,
    "off_call":              0,
    "_off_call_lines":       0,
    "group_members":         0,
    "plan":                  0,
    "off_call_plan":         0,
    "d4h_event_log":         0,
    "_d4h_dispatch_milestones": 0,
}


def _walk_outside_len(node):
    """ast.walk, but never descend into a ``len(...)`` call — a count of a PII
    list is not the list."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _walk_outside_len(child)


def _arg_pii_names(arg_node):
    """Yield PII var names referenced as Name nodes anywhere in this arg's subtree.

    Catches bare ``address``, ``address[:80]``, ``f"{lkp_address}"``, etc.
    Does NOT match the string literal ``"address"`` (no Name node), and does
    not look inside ``len(...)``.
    """
    field_reads = {id(sub.value) for sub in _walk_outside_len(arg_node)
                   if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                   and sub.value.id in PII_OBJECT_NAMES}
    for sub in _walk_outside_len(arg_node):
        if isinstance(sub, ast.Name) and sub.id in PII_ARG_NAMES and id(sub) not in field_reads:
            yield sub.id


def _is_pii_key_access(arg_node):
    """True if anywhere in the arg (outside ``len(...)``) a name is read off a
    row or a plan: ``.get(<PII_KEY_CONSTS>)``, ``[<PII_KEY_CONSTS>]``, or an
    attribute in PII_ATTRS."""
    for sub in _walk_outside_len(arg_node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if sub.func.attr == "get" and sub.args:
                first = sub.args[0]
                if isinstance(first, ast.Constant) and first.value in PII_KEY_CONSTS:
                    return True
        elif isinstance(sub, ast.Subscript):
            if isinstance(sub.slice, ast.Constant) and sub.slice.value in PII_KEY_CONSTS:
                return True
        elif isinstance(sub, ast.Attribute) and sub.attr in PII_ATTRS:
            return True
    return False


def _scan_file(path):
    """Yield (lineno, hit_labels, fmt_string) for every flagged logger call."""
    yield from _scan_tree(ast.parse(path.read_text()))


def _scan_tree(tree):
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
            if _is_pii_key_access(a):
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

    def test_scanner_sees_key_and_attribute_name_access(self):
        """The scanner itself: a name read through a subscript, an attribute
        or a join of one is flagged, and so is a whole plan; a len() of the
        list (attribute or bare Name) and a scalar field read are not."""
        src = (
            'logger.info("x %s", plan.excluded)\n'
            'logger.info("x %s", mc["display_name"])\n'
            'logger.info("x %s", ", ".join(p.unmatched))\n'
            'logger.info("x %d", len(plan.excluded))\n'
            'logger.info("x %d", len(excluded))\n'
            'logger.info("x %s", excluded)\n'
            'logger.info("x %s", plan)\n'
            'logger.info("x %s", plan.mode)\n'
            'logger.info("x %s", plan.__dict__)\n'
        )
        hits = {lineno: labels for lineno, labels, _ in _scan_tree(ast.parse(src))}
        assert hits == {1: ["responder_name_arg"], 2: ["responder_name_arg"],
                        3: ["responder_name_arg"], 6: ["excluded"], 7: ["plan"],
                        9: ["responder_name_arg"]}
